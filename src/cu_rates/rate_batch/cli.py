"""Operator CLI for offline rate batch intake (M4 / T046).

Enforces bounded local input reads, nonmutating dry runs, safe fixed reason codes,
and clean redaction of private payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from cu_rates.artifact_store import ArtifactStore
from cu_rates.db.engine import open_operational_database
from cu_rates.rate_batch.models import (
    MAX_BATCH_BYTES,
    BatchConflictError,
    BatchError,
    ForbiddenFieldError,
    HashTamperingError,
    OversizedInputError,
)
from cu_rates.rate_batch.staging import BatchImportReport, RateBatchStager


def _parse_now(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO 8601 timestamp {s!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(f"timestamp {s!r} must include a UTC timezone offset")
    return dt.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cu_rates.rate_batch",
        description="Offline rate batch intake stager (candidate-only).",
    )
    parser.add_argument(
        "batch_path",
        type=Path,
        help="Path to local JSON or JSONL batch payload.",
    )
    parser.add_argument(
        "--database",
        "--db",
        type=Path,
        default=Path("operational.sqlite"),
        help="Path to SQLite operational database (default: operational.sqlite).",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("evidence"),
        help="Path to private content-addressed ArtifactStore root (default: evidence).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform reconciliation and validation without mutating the database.",
    )
    parser.add_argument(
        "--now",
        type=_parse_now,
        default=None,
        help="Evaluation UTC timestamp for rejecting future dates (ISO 8601).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit reconciliation summary as JSON to stdout.",
    )
    return parser


def run_intake(
    batch_path: Path,
    db_path: Path,
    store_path: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> BatchImportReport:
    """Run batch intake with strict bounded input checking."""
    # Type/size checks apply to the opened descriptor; a swapped path cannot
    # turn the bounded read into a blocking FIFO or a followed symlink.
    fd = os.open(batch_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("batch must be a regular file")
        if info.st_size > MAX_BATCH_BYTES:
            raise OversizedInputError("batch exceeds the file size limit")
        content = handle.read(MAX_BATCH_BYTES + 1)
    if len(content) > MAX_BATCH_BYTES:
        raise OversizedInputError(f"file exceeds maximum allowed {MAX_BATCH_BYTES} bytes")

    # Intake never creates the database, schema, or evidence store (dry run or not).
    if not db_path.is_file():
        raise FileNotFoundError(f"operational database does not exist: {db_path}")
    store_root = store_path.resolve()
    for sub in ("", "blobs", "observations", "staging"):
        if not (store_root / sub).is_dir():
            raise FileNotFoundError(f"evidence store does not exist or is incomplete: {store_path}")

    store = ArtifactStore(store_root)
    database = db_path.resolve()

    def connect_readonly() -> sqlite3.Connection:
        return sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, autocommit=False)

    engine = (
        create_engine("sqlite://", creator=connect_readonly, poolclass=NullPool)
        if dry_run
        else open_operational_database(database)
    )
    try:
        return RateBatchStager(engine, store).stage(content, dry_run=dry_run, now=now)
    finally:
        engine.dispose()


def main(args: Sequence[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(args)

    try:
        report = run_intake(
            opts.batch_path,
            opts.database,
            opts.store,
            dry_run=opts.dry_run,
            now=opts.now,
        )
    except Exception as exc:
        # Validation and database exceptions can embed private input or paths.
        if isinstance(exc, BatchConflictError):
            err_code = "batch_id_conflict"
        elif isinstance(exc, HashTamperingError):
            err_code = "batch_hash_mismatch"
        elif isinstance(exc, ForbiddenFieldError):
            err_code = "forbidden_publication_fields"
        elif isinstance(exc, OversizedInputError):
            err_code = "oversized_input"
        elif isinstance(exc, (BatchError, ValueError)):
            err_code = "invalid_batch"
        elif isinstance(exc, OSError):
            err_code = "input_unavailable"
        else:
            err_code = "intake_failed"
        if opts.json:
            sys.stdout.write(json.dumps({"error": err_code}) + "\n")
        else:
            sys.stderr.write(f"Error: {err_code}\n")
        return 1

    if opts.json:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        status = (
            "NO-OP (already imported)"
            if report.is_noop
            else ("DRY-RUN" if report.dry_run else "SUCCESS")
        )
        sys.stdout.write(f"Batch Intake: {status}\n")
        sys.stdout.write(f"  Batch ID:   {report.batch_id}\n")
        sys.stdout.write(f"  Digest:     {report.batch_digest[:16]}...\n")
        sys.stdout.write(f"  Submitted:  {report.submitted_count}\n")
        sys.stdout.write(f"  Staged:     {report.staged_count}\n")
        sys.stdout.write(f"  Held:       {report.held_count}\n")
        sys.stdout.write(f"  Rejected:   {report.rejected_count}\n")
        if report.hold_reasons:
            sys.stdout.write("  Hold Reasons:\n")
            for reason, count in sorted(report.hold_reasons.items()):
                sys.stdout.write(f"    - {reason}: {count}\n")
        if report.reject_reasons:
            sys.stdout.write("  Reject Reasons:\n")
            for reason, count in sorted(report.reject_reasons.items()):
                sys.stdout.write(f"    - {reason}: {count}\n")

    if report.rejected_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

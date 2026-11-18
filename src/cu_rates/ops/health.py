"""Read-only health check for cu-rates snapshot, denylist, backup, and database state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cu_rates.contracts.publication import DEGRADE_AFTER, WITHDRAW_AFTER
from cu_rates.ops.backup import verify_backup
from cu_rates.snapshot import Denylist, SnapshotError, SnapshotReader
from cu_rates.snapshot._database import POINTER, VERSIONS, read_version, verify_database
from cu_rates.snapshot._models import SnapshotView


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


@dataclass(frozen=True)
class HealthReport:
    checks: tuple[Check, ...]
    ok: bool


def _check_snapshot_readable(
    snapshot_root: Path,
    denylist_path: Path,
    now: datetime,
) -> tuple[Check, SnapshotView | None]:
    if not snapshot_root.is_absolute():
        return (
            Check(
                name="snapshot_readable",
                status="fail",
                detail="snapshot_root must be an absolute path",
            ),
            None,
        )
    if not denylist_path.is_absolute():
        return (
            Check(
                name="snapshot_readable",
                status="fail",
                detail="denylist_path must be an absolute path",
            ),
            None,
        )
    try:
        reader = SnapshotReader(snapshot_root, denylist_path)
        view = reader.read(at=now)
        return (
            Check(
                name="snapshot_readable",
                status="ok",
                detail=(
                    f"snapshot {view.info.snapshot_id} readable ({len(view.offers)} visible offers)"
                ),
            ),
            view,
        )
    except SnapshotError as exc:
        return (
            Check(
                name="snapshot_readable",
                status="fail",
                detail=str(exc),
            ),
            None,
        )
    except Exception as exc:
        return (
            Check(
                name="snapshot_readable",
                status="fail",
                detail=str(exc),
            ),
            None,
        )


def _check_snapshot_fresh(
    snapshot_root: Path,
    view: SnapshotView | None,
    now: datetime,
) -> Check:
    try:
        if view is None:
            return Check(
                name="snapshot_fresh",
                status="fail",
                detail="snapshot unavailable",
            )

        version = read_version(snapshot_root / POINTER)
        _, raw_offers = verify_database(snapshot_root / VERSIONS / version.snapshot_id, version)
        total_raw = len(raw_offers)

        if total_raw == 0:
            # A site that has published no rates yet is a legitimate state, not an outage.
            return Check(
                name="snapshot_fresh",
                status="warn",
                detail="snapshot lists zero offers (no rates are published)",
            )

        if len(view.offers) == 0:
            return Check(
                name="snapshot_fresh",
                status="fail",
                detail=(
                    f"zero visible offers while snapshot lists {total_raw} offer(s) "
                    "(everything withdrawn/expired)"
                ),
            )

        newest_observed = max(offer.observed_at for offer in view.offers)
        age = now - newest_observed

        if age > WITHDRAW_AFTER:
            return Check(
                name="snapshot_fresh",
                status="fail",
                detail=f"newest offer observed {age} ago (exceeds {WITHDRAW_AFTER})",
            )
        elif age > DEGRADE_AFTER:
            return Check(
                name="snapshot_fresh",
                status="warn",
                detail=f"newest offer observed {age} ago (exceeds {DEGRADE_AFTER})",
            )
        else:
            return Check(
                name="snapshot_fresh",
                status="ok",
                detail=f"newest offer observed {age} ago",
            )
    except Exception as exc:
        return Check(name="snapshot_fresh", status="fail", detail=str(exc))


def _check_denylist_readable(
    denylist_path: Path,
    now: datetime,
) -> Check:
    try:
        if not denylist_path.is_absolute():
            return Check(
                name="denylist_readable",
                status="fail",
                detail="denylist_path must be an absolute path",
            )
        denylist = Denylist(denylist_path)
        entries = denylist.entries()
        in_force = [e for e in entries if e.in_force_at(now)]
        return Check(
            name="denylist_readable",
            status="ok",
            detail=f"{len(in_force)} in-force suppression(s)",
        )
    except Exception as exc:
        return Check(
            name="denylist_readable",
            status="fail",
            detail=str(exc),
        )


def _check_backup_recent(
    backup_dir: Path,
    now: datetime,
) -> Check:
    try:
        if not backup_dir.is_absolute():
            return Check(
                name="backup_recent",
                status="fail",
                detail="backup_dir must be an absolute path",
            )
        if not backup_dir.is_dir():
            return Check(
                name="backup_recent",
                status="fail",
                detail=f"backup directory does not exist or is not a directory: {backup_dir.name}",
            )

        candidates: list[Path] = []
        if backup_dir.name.startswith("backup-") and (backup_dir / "manifest.json").is_file():
            candidates.append(backup_dir)
        else:
            for item in backup_dir.iterdir():
                if item.is_symlink() or not item.is_dir():
                    continue
                if not item.name.startswith("backup-"):
                    continue
                candidates.append(item)

        if not candidates:
            return Check(
                name="backup_recent",
                status="fail",
                detail="no backup directories found",
            )

        # Backup names embed a UTC timestamp, so the newest sorts last. Verify ONLY the
        # newest (verification hashes every artifact, far too heavy to repeat for each
        # retained backup on a timer), and never let an older good backup hide a bad one.
        newest_candidate = max(candidates, key=lambda path: path.name)
        try:
            verify_backup(newest_candidate)
            manifest_data = json.loads(
                (newest_candidate / "manifest.json").read_text(encoding="utf-8")
            )
            newest_created_at = datetime.fromisoformat(str(manifest_data["created_at"]))
        except Exception as exc:
            return Check(
                name="backup_recent",
                status="fail",
                detail=f"newest backup ({newest_candidate.name}) failed verification: {exc}",
            )
        if newest_created_at.tzinfo is None:
            newest_created_at = newest_created_at.replace(tzinfo=UTC)
        backup_age = now - newest_created_at

        if backup_age > timedelta(days=7):
            return Check(
                name="backup_recent",
                status="fail",
                detail=(
                    f"newest backup ({newest_candidate.name}) is {backup_age} old (exceeds 7 days)"
                ),
            )
        elif backup_age > timedelta(hours=36):
            return Check(
                name="backup_recent",
                status="warn",
                detail=(
                    f"newest backup ({newest_candidate.name}) is {backup_age} old "
                    "(exceeds 36 hours)"
                ),
            )
        else:
            return Check(
                name="backup_recent",
                status="ok",
                detail=f"newest backup ({newest_candidate.name}) is {backup_age} old",
            )
    except Exception as exc:
        return Check(
            name="backup_recent",
            status="fail",
            detail=str(exc),
        )


def _check_database_integrity(
    database: Path,
) -> Check:
    try:
        if not database.is_absolute():
            return Check(
                name="database_integrity",
                status="fail",
                detail="database must be an absolute path",
            )
        if not database.is_file():
            return Check(
                name="database_integrity",
                status="fail",
                detail="database file not found",
            )
        uri = f"file:{database.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            if rows == [("ok",)]:
                return Check(
                    name="database_integrity",
                    status="ok",
                    detail="PRAGMA quick_check returned ok",
                )
            else:
                return Check(
                    name="database_integrity",
                    status="fail",
                    detail=f"PRAGMA quick_check returned {rows}",
                )
    except Exception as exc:
        return Check(
            name="database_integrity",
            status="fail",
            detail=str(exc),
        )


def check_health(
    *,
    snapshot_root: Path,
    denylist_path: Path,
    backup_dir: Path | None = None,
    database: Path | None = None,
    now: datetime,
) -> HealthReport:
    """Run a read-only health check across public snapshot, denylist, backup, and database."""
    checks: list[Check] = []
    try:
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

        readable_check, view = _check_snapshot_readable(snapshot_root, denylist_path, now_utc)
        checks.append(readable_check)

        fresh_check = _check_snapshot_fresh(snapshot_root, view, now_utc)
        checks.append(fresh_check)

        denylist_check = _check_denylist_readable(denylist_path, now_utc)
        checks.append(denylist_check)

        if backup_dir is not None:
            backup_check = _check_backup_recent(backup_dir, now_utc)
            checks.append(backup_check)

        if database is not None:
            db_check = _check_database_integrity(database)
            checks.append(db_check)

    except Exception as exc:
        checks.append(Check(name="health_check", status="fail", detail=str(exc)))

    ok = not any(c.status == "fail" for c in checks)
    return HealthReport(checks=tuple(checks), ok=ok)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cu_rates.ops.health",
        description="Read-only health check for cu-rates public and operational state",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        required=True,
        help="Public snapshot root directory",
    )
    parser.add_argument(
        "--denylist",
        type=Path,
        required=True,
        help="Path to denylist/withdrawal ledger JSON",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Backup destination directory",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Operational SQLite database path",
    )
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="Evaluation timestamp (ISO8601)",
    )

    args = parser.parse_args(argv)

    now = args.now or datetime.now(UTC)
    report = check_health(
        snapshot_root=args.snapshot_root,
        denylist_path=args.denylist,
        backup_dir=args.backup_dir,
        database=args.database,
        now=now,
    )

    output = {
        "ok": report.ok,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in report.checks],
    }
    print(json.dumps(output))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

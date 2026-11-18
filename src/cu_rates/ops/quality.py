"""Read-only data-quality report for cu-rates operational database state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

QUARANTINED_WARN_AFTER: timedelta = timedelta(hours=24)
QUARANTINED_FAIL_AFTER: timedelta = timedelta(days=7)

STALE_WARN_AFTER: timedelta = timedelta(hours=72)
STALE_FAIL_AFTER: timedelta = timedelta(days=7)
STALE_SOURCE_IDS_LIMIT: int = 5

BLOCKED_LOOKBACK: timedelta = timedelta(days=7)
BLOCKED_FAIL_THRESHOLD: int = 3
BLOCKED_SOURCE_IDS_LIMIT: int = 5


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    count: int | None = None


@dataclass(frozen=True)
class QualityReport:
    checks: tuple[Check, ...]
    ok: bool


def _open_read_only(database: Path) -> sqlite3.Connection:
    """The only way this module opens a database: strictly read-only, enforced by SQLite itself
    (a write attempt raises), not merely by the file's permissions."""
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _iso(dt: datetime) -> str:
    utc_dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return utc_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _run_check(name: str, check_fn: Callable[[], Check]) -> Check:
    try:
        return check_fn()
    except Exception as exc:
        return Check(
            name=name,
            status="fail",
            detail=f"check failed: {type(exc).__name__}: {exc}",
            count=None,
        )


def _check_quarantined_offers(conn: sqlite3.Connection, now: datetime) -> Check:
    cutoff_24h = _iso(now - QUARANTINED_WARN_AFTER)
    cutoff_7d = _iso(now - QUARANTINED_FAIL_AFTER)

    cursor = conn.execute(
        """
        SELECT
            COUNT(*) AS total_over_24h,
            COUNT(CASE WHEN observed_at < :cutoff_7d THEN 1 END) AS total_over_7d
        FROM normalized_offers
        WHERE status = 'quarantined' AND observed_at < :cutoff_24h
        """,
        {"cutoff_24h": cutoff_24h, "cutoff_7d": cutoff_7d},
    )
    row = cursor.fetchone()
    total_over_24h = row[0]
    total_over_7d = row[1]

    if total_over_24h == 0:
        return Check(
            name="quarantined_offers",
            status="ok",
            detail="0 quarantined offer(s) older than 24 hours",
            count=0,
        )
    elif total_over_7d > 0:
        return Check(
            name="quarantined_offers",
            status="fail",
            detail=(
                f"{total_over_24h} quarantined offer(s) older than 24 hours "
                f"({total_over_7d} older than 7 days)"
            ),
            count=total_over_24h,
        )
    else:
        return Check(
            name="quarantined_offers",
            status="warn",
            detail=f"{total_over_24h} quarantined offer(s) older than 24 hours",
            count=total_over_24h,
        )


def _check_stale_sources(conn: sqlite3.Connection, now: datetime) -> Check:
    now_iso = _iso(now)
    cutoff_72h = _iso(now - STALE_WARN_AFTER)
    cutoff_7d = _iso(now - STALE_FAIL_AFTER)

    query = """
    WITH latest_approvals AS (
        SELECT
            source_id,
            decision,
            ROW_NUMBER() OVER (
                PARTITION BY source_id
                ORDER BY decided_at DESC, decision DESC, approval_id DESC
            ) AS rn
        FROM source_approvals
        WHERE decided_at <= :now
    ),
    current_approved_sources AS (
        SELECT rs.source_id
        FROM rate_sources rs
        JOIN latest_approvals la ON rs.source_id = la.source_id
        WHERE la.rn = 1 AND la.decision = 'approved'
    ),
    newest_succeeded_runs AS (
        SELECT
            source_id,
            MAX(finished_at) AS last_finished_at
        FROM fetch_runs
        WHERE status = 'succeeded' AND finished_at <= :now
        GROUP BY source_id
    )
    SELECT
        cas.source_id,
        nsr.last_finished_at
    FROM current_approved_sources cas
    LEFT JOIN newest_succeeded_runs nsr ON cas.source_id = nsr.source_id
    ORDER BY cas.source_id
    """
    cursor = conn.execute(query, {"now": now_iso})
    rows = cursor.fetchall()

    failed_ids: list[str] = []
    warned_ids: list[str] = []

    for source_id, last_finished_at in rows:
        if last_finished_at is None or last_finished_at < cutoff_7d:
            failed_ids.append(source_id)
        elif last_finished_at < cutoff_72h:
            warned_ids.append(source_id)

    stale_ids = failed_ids + warned_ids
    stale_count = len(stale_ids)
    sample_ids = ", ".join(stale_ids[:STALE_SOURCE_IDS_LIMIT])

    if failed_ids:
        return Check(
            name="stale_sources",
            status="fail",
            detail=f"{stale_count} stale source(s): {sample_ids}",
            count=stale_count,
        )
    elif warned_ids:
        return Check(
            name="stale_sources",
            status="warn",
            detail=f"{stale_count} stale source(s): {sample_ids}",
            count=stale_count,
        )
    else:
        return Check(
            name="stale_sources",
            status="ok",
            detail="0 stale source(s)",
            count=0,
        )


def _check_blocked_sources(conn: sqlite3.Connection, now: datetime) -> Check:
    now_iso = _iso(now)
    cutoff_7d = _iso(now - BLOCKED_LOOKBACK)

    query = """
    SELECT DISTINCT source_id
    FROM fetch_runs
    WHERE status = 'blocked'
      AND finished_at >= :cutoff_7d
      AND finished_at <= :now
    ORDER BY source_id
    """
    cursor = conn.execute(query, {"cutoff_7d": cutoff_7d, "now": now_iso})
    rows = cursor.fetchall()
    blocked_ids = [row[0] for row in rows]
    count = len(blocked_ids)
    sample_ids = ", ".join(blocked_ids[:BLOCKED_SOURCE_IDS_LIMIT])

    if count == 0:
        return Check(
            name="blocked_sources",
            status="ok",
            detail="0 blocked source(s) in the last 7 days",
            count=0,
        )
    elif count > BLOCKED_FAIL_THRESHOLD:
        return Check(
            name="blocked_sources",
            status="fail",
            detail=f"{count} blocked source(s) in the last 7 days: {sample_ids}",
            count=count,
        )
    else:
        return Check(
            name="blocked_sources",
            status="warn",
            detail=f"{count} blocked source(s) in the last 7 days: {sample_ids}",
            count=count,
        )


def _check_open_withdrawals(conn: sqlite3.Connection, now: datetime) -> Check:
    now_iso = _iso(now)

    query = """
    SELECT COUNT(*)
    FROM withdrawal_entries we
    LEFT JOIN withdrawal_lifts wl ON we.withdrawal_id = wl.withdrawal_id
    WHERE we.withdrawn_at <= :now
      AND wl.withdrawal_id IS NULL
    """
    cursor = conn.execute(query, {"now": now_iso})
    count = cursor.fetchone()[0]

    return Check(
        name="open_withdrawals",
        status="ok",
        detail=f"{count} open withdrawal(s) in force",
        count=count,
    )


def _check_unresolved_corrections(conn: sqlite3.Connection, now: datetime) -> Check:
    now_iso = _iso(now)

    query = """
    SELECT COUNT(*)
    FROM corrections c
    WHERE c.corrected_at <= :now
      AND NOT EXISTS (
        SELECT 1
        FROM review_decisions rd
        WHERE rd.subject = 'offer'
          AND rd.subject_id = c.offer_id
          AND rd.decided_at > c.corrected_at
          AND rd.decided_at <= :now
      )
    """
    cursor = conn.execute(query, {"now": now_iso})
    count = cursor.fetchone()[0]

    if count > 0:
        return Check(
            name="unresolved_corrections",
            status="warn",
            detail=f"{count} unresolved correction(s)",
            count=count,
        )
    else:
        return Check(
            name="unresolved_corrections",
            status="ok",
            detail="0 unresolved correction(s)",
            count=0,
        )


def quality_report(database: Path, *, now: datetime) -> QualityReport:
    """Run a read-only data-quality report across the operational database."""
    if not database.is_absolute():
        return QualityReport(
            checks=(
                Check(
                    name="database_readable",
                    status="fail",
                    detail="database must be an absolute path",
                    count=None,
                ),
            ),
            ok=False,
        )
    if not database.is_file():
        return QualityReport(
            checks=(
                Check(
                    name="database_readable",
                    status="fail",
                    detail=f"database file not found: {database}",
                    count=None,
                ),
            ),
            ok=False,
        )

    try:
        conn = _open_read_only(database)
        # Test read access
        conn.execute("SELECT 1")
    except Exception as exc:
        return QualityReport(
            checks=(
                Check(
                    name="database_readable",
                    status="fail",
                    detail=f"unable to open database: {exc}",
                    count=None,
                ),
            ),
            ok=False,
        )

    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    with closing(conn):
        checks: list[Check] = [
            _run_check("quarantined_offers", lambda: _check_quarantined_offers(conn, now_utc)),
            _run_check("stale_sources", lambda: _check_stale_sources(conn, now_utc)),
            _run_check("blocked_sources", lambda: _check_blocked_sources(conn, now_utc)),
            _run_check("open_withdrawals", lambda: _check_open_withdrawals(conn, now_utc)),
            _run_check(
                "unresolved_corrections", lambda: _check_unresolved_corrections(conn, now_utc)
            ),
        ]

    ok = not any(c.status == "fail" for c in checks)
    return QualityReport(checks=tuple(checks), ok=ok)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cu_rates.ops.quality",
        description="Read-only data-quality report for cu-rates operational database",
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Operational SQLite database path (must be absolute)",
    )
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="Evaluation timestamp (ISO8601)",
    )

    args = parser.parse_args(argv)

    now = args.now or datetime.now(UTC)
    report = quality_report(args.database, now=now)

    output = {
        "ok": report.ok,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "detail": c.detail,
                "count": c.count,
            }
            for c in report.checks
        ],
    }
    print(json.dumps(output))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

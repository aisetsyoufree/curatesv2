"""Tests for cu-rates read-only data-quality report and CLI."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.schema import (
    corrections,
    fetch_profiles,
    fetch_runs,
    institution_source_relationships,
    institutions,
    normalized_offers,
    products,
    rate_sources,
    review_decisions,
    source_approvals,
    withdrawal_entries,
    withdrawal_lifts,
)
from cu_rates.ops.quality import (
    BLOCKED_FAIL_THRESHOLD,
    BLOCKED_LOOKBACK,
    QUARANTINED_FAIL_AFTER,
    QUARANTINED_WARN_AFTER,
    STALE_FAIL_AFTER,
    STALE_WARN_AFTER,
    Check,
    QualityReport,
    main,
    quality_report,
)

CHARTER_NORTH = 1000001
CHARTER_SOUTH = 1000002
SHA = "aa" * 32
GIT = "bb" * 20
EVAL_TIME = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _hash_tree(root: Path) -> dict[str, str]:
    """Record SHA-256 digests of all files under a directory tree."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            tree[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "operational.sqlite"
    upgrade_database(db)
    # Open once to initialize database state/WAL in operational mode
    engine = open_operational_database(db)
    engine.dispose()
    return db


@pytest.fixture
def seeded_db(empty_db: Path) -> Iterator[Path]:
    engine = open_operational_database(empty_db)
    with engine.begin() as conn:
        conn.execute(
            insert(institutions),
            [
                {
                    "charter_number": CHARTER_NORTH,
                    "schema_version": "1.0",
                    "legal_name": "North Charter CU",
                    "status": "active",
                    "successor_charter_number": None,
                    "charter_verified": 1,
                },
                {
                    "charter_number": CHARTER_SOUTH,
                    "schema_version": "1.0",
                    "legal_name": "South Charter CU",
                    "status": "active",
                    "successor_charter_number": None,
                    "charter_verified": 1,
                },
            ],
        )
        conn.execute(
            insert(rate_sources),
            [
                {
                    "source_id": "source-1",
                    "schema_version": "1.0",
                    "url": "https://north.example/rates",
                    "evidence_kind": "html",
                    "description": "north rates",
                },
                {
                    "source_id": "source-2",
                    "schema_version": "1.0",
                    "url": "https://south.example/rates",
                    "evidence_kind": "html",
                    "description": "south rates",
                },
            ],
        )
        conn.execute(
            insert(products),
            [
                {
                    "product_id": "prod-1",
                    "schema_version": "1.0",
                    "institution_charter": CHARTER_NORTH,
                    "family": "mortgage",
                    "name": "North 30Y Fixed",
                    "lifecycle": "active",
                    "rate_availability": "public_rate",
                    "conforming_status": "conforming",
                    "government_guaranty": "none",
                    "notes_json": [],
                },
                {
                    "product_id": "prod-2",
                    "schema_version": "1.0",
                    "institution_charter": CHARTER_SOUTH,
                    "family": "mortgage",
                    "name": "South 30Y Fixed",
                    "lifecycle": "active",
                    "rate_availability": "public_rate",
                    "conforming_status": "conforming",
                    "government_guaranty": "none",
                    "notes_json": [],
                },
            ],
        )
        conn.execute(
            insert(institution_source_relationships),
            [
                {
                    "relationship_id": "rel-1",
                    "schema_version": "1.0",
                    "institution_charter": CHARTER_NORTH,
                    "source_id": "source-1",
                    "product_id": "prod-1",
                    "provider_id": None,
                    "kind": "first_party",
                    "approved_by": None,
                    "approved_at": None,
                },
                {
                    "relationship_id": "rel-2",
                    "schema_version": "1.0",
                    "institution_charter": CHARTER_SOUTH,
                    "source_id": "source-2",
                    "product_id": "prod-2",
                    "provider_id": None,
                    "kind": "first_party",
                    "approved_by": None,
                    "approved_at": None,
                },
            ],
        )
        conn.execute(
            insert(fetch_profiles),
            [
                {
                    "profile_id": "prof-1",
                    "schema_version": "1.0",
                    "source_id": "source-1",
                    "method": "http_get",
                    "min_interval_seconds": 5,
                    "timeout_seconds": 30,
                    "respect_robots": 1,
                },
                {
                    "profile_id": "prof-2",
                    "schema_version": "1.0",
                    "source_id": "source-2",
                    "method": "http_get",
                    "min_interval_seconds": 5,
                    "timeout_seconds": 30,
                    "respect_robots": 1,
                },
            ],
        )
    engine.dispose()
    yield empty_db


def test_empty_database_all_ok(empty_db: Path) -> None:
    """Empty migrated database passes all checks with status 'ok' and count 0."""
    report = quality_report(empty_db, now=EVAL_TIME)

    assert isinstance(report, QualityReport)
    assert report.ok is True
    check_map = {c.name: c for c in report.checks}
    assert set(check_map.keys()) == {
        "quarantined_offers",
        "stale_sources",
        "blocked_sources",
        "open_withdrawals",
        "unresolved_corrections",
    }
    for c in report.checks:
        assert c.status == "ok"
        assert c.count == 0


def test_threshold_constants() -> None:
    """Module constants define the required thresholds."""
    assert timedelta(hours=24) == QUARANTINED_WARN_AFTER
    assert timedelta(days=7) == QUARANTINED_FAIL_AFTER
    assert timedelta(hours=72) == STALE_WARN_AFTER
    assert timedelta(days=7) == STALE_FAIL_AFTER
    assert timedelta(days=7) == BLOCKED_LOOKBACK
    assert BLOCKED_FAIL_THRESHOLD == 3


def test_quarantined_offers_warn_and_fail(seeded_db: Path) -> None:
    """Quarantined offer older than 24h warns; older than 7 days fails."""
    engine = open_operational_database(seeded_db)

    # 1. Quarantined offer aged 1 hour -> ok (count 0)
    with engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-fresh",
                "schema_version": "1.0",
                "institution_charter": CHARTER_NORTH,
                "product_id": "prod-1",
                "source_id": "source-1",
                "status": "quarantined",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": EVAL_TIME - timedelta(hours=1),
                "published_at": None,
                "valid_until": None,
            },
        )
    engine.dispose()

    report_fresh = quality_report(seeded_db, now=EVAL_TIME)
    check_fresh = next(c for c in report_fresh.checks if c.name == "quarantined_offers")
    assert check_fresh.status == "ok"
    assert check_fresh.count == 0
    assert report_fresh.ok is True

    # 2. Quarantined offer aged 25 hours (> 24h, <= 7d) -> warn
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-warn",
                "schema_version": "1.0",
                "institution_charter": CHARTER_NORTH,
                "product_id": "prod-1",
                "source_id": "source-1",
                "status": "quarantined",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": EVAL_TIME - timedelta(hours=25),
                "published_at": None,
                "valid_until": None,
            },
        )
    engine.dispose()

    report_warn = quality_report(seeded_db, now=EVAL_TIME)
    check_warn = next(c for c in report_warn.checks if c.name == "quarantined_offers")
    assert check_warn.status == "warn"
    assert check_warn.count == 1
    assert "older than 24 hours" in check_warn.detail
    assert report_warn.ok is True  # Warnings do not fail overall report

    # 3. Quarantined offer aged 8 days (> 7d) -> fail
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-fail",
                "schema_version": "1.0",
                "institution_charter": CHARTER_NORTH,
                "product_id": "prod-1",
                "source_id": "source-1",
                "status": "quarantined",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": EVAL_TIME - timedelta(days=8),
                "published_at": None,
                "valid_until": None,
            },
        )
    engine.dispose()

    report_fail = quality_report(seeded_db, now=EVAL_TIME)
    check_fail = next(c for c in report_fail.checks if c.name == "quarantined_offers")
    assert check_fail.status == "fail"
    assert check_fail.count == 2  # offer-warn and offer-fail are both > 24h
    assert "older than 7 days" in check_fail.detail
    assert report_fail.ok is False


def test_stale_sources_warn_and_fail(seeded_db: Path) -> None:
    """Approved sources warn when >72h stale; fail when >7d stale or never fetched."""
    engine = open_operational_database(seeded_db)

    # Approve source-1
    with engine.begin() as conn:
        conn.execute(
            insert(source_approvals),
            {
                "approval_id": "appr-1",
                "schema_version": "1.0",
                "source_id": "source-1",
                "source_url": "https://north.example/rates",
                "decision": "approved",
                "actor": "reviewer-1",
                "decided_at": EVAL_TIME - timedelta(days=10),
                "policy_id": "pol-1",
                "policy_version": "1.0",
                "rationale": "approved test source",
            },
        )
    engine.dispose()

    # 1. Never fetched -> fail
    report_never = quality_report(seeded_db, now=EVAL_TIME)
    check_never = next(c for c in report_never.checks if c.name == "stale_sources")
    assert check_never.status == "fail"
    assert check_never.count == 1
    assert "source-1" in check_never.detail
    assert "https://" not in check_never.detail  # No URLs
    assert report_never.ok is False

    # 2. Succeeded fetch finished 75 hours before now -> warn
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_runs),
            {
                "run_id": "run-75h",
                "schema_version": "1.0",
                "source_id": "source-1",
                "profile_id": "prof-1",
                "profile_sha256": SHA,
                "status": "succeeded",
                "started_at": EVAL_TIME - timedelta(hours=75),
                "finished_at": EVAL_TIME - timedelta(hours=75),
                "git_sha": GIT,
                "error": None,
                "validator_kind": None,
                "validator_value": None,
            },
        )
    engine.dispose()

    report_warn = quality_report(seeded_db, now=EVAL_TIME)
    check_warn = next(c for c in report_warn.checks if c.name == "stale_sources")
    assert check_warn.status == "warn"
    assert check_warn.count == 1
    assert "source-1" in check_warn.detail
    assert report_warn.ok is True

    # 3. If evaluated 8 days after that run -> fail
    now_8d_later = EVAL_TIME - timedelta(hours=75) + timedelta(days=8)
    report_old = quality_report(seeded_db, now=now_8d_later)
    check_old = next(c for c in report_old.checks if c.name == "stale_sources")
    assert check_old.status == "fail"
    assert check_old.count == 1
    assert "source-1" in check_old.detail
    assert report_old.ok is False

    # 4. Succeeded fetch finished 5 hours before now -> ok
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_runs),
            {
                "run_id": "run-5h",
                "schema_version": "1.0",
                "source_id": "source-1",
                "profile_id": "prof-1",
                "profile_sha256": SHA,
                "status": "succeeded",
                "started_at": EVAL_TIME - timedelta(hours=5),
                "finished_at": EVAL_TIME - timedelta(hours=5),
                "git_sha": GIT,
                "error": None,
                "validator_kind": None,
                "validator_value": None,
            },
        )
    engine.dispose()

    report_fresh = quality_report(seeded_db, now=EVAL_TIME)
    check_fresh = next(c for c in report_fresh.checks if c.name == "stale_sources")
    assert check_fresh.status == "ok"
    assert check_fresh.count == 0


def test_stale_sources_detail_truncates_at_five(seeded_db: Path) -> None:
    """Detail lists at most 5 source IDs even when more are stale."""
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        # Insert 6 additional approved sources with no fetch runs
        for i in range(10, 16):
            s_id = f"source-{i}"
            conn.execute(
                insert(rate_sources),
                {
                    "source_id": s_id,
                    "schema_version": "1.0",
                    "url": f"https://source{i}.example/rates",
                    "evidence_kind": "html",
                },
            )
            conn.execute(
                insert(source_approvals),
                {
                    "approval_id": f"appr-{i}",
                    "schema_version": "1.0",
                    "source_id": s_id,
                    "source_url": f"https://source{i}.example/rates",
                    "decision": "approved",
                    "actor": "reviewer-1",
                    "decided_at": EVAL_TIME - timedelta(days=1),
                    "policy_id": "pol-1",
                    "policy_version": "1.0",
                    "rationale": "approved test",
                },
            )
    engine.dispose()

    report = quality_report(seeded_db, now=EVAL_TIME)
    check = next(c for c in report.checks if c.name == "stale_sources")
    assert check.status == "fail"
    assert check.count == 6
    # Only 5 IDs appear in the detail string
    parts = check.detail.split(": ")[1].split(", ")
    assert len(parts) == 5


def test_blocked_sources_warn_and_fail(seeded_db: Path) -> None:
    """1 to 3 distinct blocked sources warn; > 3 distinct sources fail."""
    engine = open_operational_database(seeded_db)

    # 1. Insert 1 blocked fetch run for source-1 within last 7 days -> warn
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_runs),
            {
                "run_id": "run-blocked-1",
                "schema_version": "1.0",
                "source_id": "source-1",
                "profile_id": "prof-1",
                "profile_sha256": SHA,
                "status": "blocked",
                "started_at": EVAL_TIME - timedelta(days=2),
                "finished_at": EVAL_TIME - timedelta(days=2),
                "git_sha": GIT,
                "error": "HTTP 403 Forbidden",
                "validator_kind": None,
                "validator_value": None,
            },
        )
    engine.dispose()

    report_warn = quality_report(seeded_db, now=EVAL_TIME)
    check_warn = next(c for c in report_warn.checks if c.name == "blocked_sources")
    assert check_warn.status == "warn"
    assert check_warn.count == 1
    assert "source-1" in check_warn.detail
    assert report_warn.ok is True

    # 2. Add 3 more distinct sources with blocked fetch runs (total 4) -> fail
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        for i in (3, 4, 5):
            s_id = f"source-{i}"
            p_id = f"prof-{i}"
            conn.execute(
                insert(rate_sources),
                {
                    "source_id": s_id,
                    "schema_version": "1.0",
                    "url": f"https://example{i}.org/rates",
                    "evidence_kind": "html",
                },
            )
            conn.execute(
                insert(fetch_profiles),
                {
                    "profile_id": p_id,
                    "schema_version": "1.0",
                    "source_id": s_id,
                    "method": "http_get",
                    "min_interval_seconds": 5,
                    "timeout_seconds": 30,
                    "respect_robots": 1,
                },
            )
            conn.execute(
                insert(fetch_runs),
                {
                    "run_id": f"run-blocked-{i}",
                    "schema_version": "1.0",
                    "source_id": s_id,
                    "profile_id": p_id,
                    "profile_sha256": SHA,
                    "status": "blocked",
                    "started_at": EVAL_TIME - timedelta(days=1),
                    "finished_at": EVAL_TIME - timedelta(days=1),
                    "git_sha": GIT,
                    "error": "Captcha detected",
                    "validator_kind": None,
                    "validator_value": None,
                },
            )
    engine.dispose()

    report_fail = quality_report(seeded_db, now=EVAL_TIME)
    check_fail = next(c for c in report_fail.checks if c.name == "blocked_sources")
    assert check_fail.status == "fail"
    assert check_fail.count == 4
    assert report_fail.ok is False


def test_open_withdrawals_counting_and_lifts(empty_db: Path) -> None:
    """In-force withdrawals are counted; lifted or future withdrawals are not."""
    engine = open_operational_database(empty_db)
    with engine.begin() as conn:
        # In-force withdrawal 1
        conn.execute(
            insert(withdrawal_entries),
            {
                "withdrawal_id": "wd-active",
                "schema_version": "1.0",
                "scope": WithdrawalScope.OFFER,
                "target_id": "offer-1",
                "actor": "operator-1",
                "withdrawn_at": EVAL_TIME - timedelta(days=1),
                "reason": "rate error",
            },
        )
        # Lifted withdrawal 2
        conn.execute(
            insert(withdrawal_entries),
            {
                "withdrawal_id": "wd-lifted",
                "schema_version": "1.0",
                "scope": WithdrawalScope.OFFER,
                "target_id": "offer-2",
                "actor": "operator-1",
                "withdrawn_at": EVAL_TIME - timedelta(days=2),
                "reason": "temporary",
            },
        )
        conn.execute(
            insert(withdrawal_lifts),
            {
                "withdrawal_id": "wd-lifted",
                "lifted_at": EVAL_TIME - timedelta(hours=12),
                "lifted_by": "operator-2",
            },
        )
        # Future withdrawal 3 (not in force yet)
        conn.execute(
            insert(withdrawal_entries),
            {
                "withdrawal_id": "wd-future",
                "schema_version": "1.0",
                "scope": WithdrawalScope.OFFER,
                "target_id": "offer-3",
                "actor": "operator-1",
                "withdrawn_at": EVAL_TIME + timedelta(days=1),
                "reason": "scheduled withdrawal",
            },
        )
    engine.dispose()

    report = quality_report(empty_db, now=EVAL_TIME)
    check = next(c for c in report.checks if c.name == "open_withdrawals")
    assert check.status == "ok"
    assert check.count == 1
    assert "1 open withdrawal(s) in force" in check.detail


def test_unresolved_corrections_warn_then_resolve(seeded_db: Path) -> None:
    """Correction without later review decision warns; resolves when reviewed."""
    engine = open_operational_database(seeded_db)

    # Insert an offer first (FK for corrections)
    with engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-corr",
                "schema_version": "1.0",
                "institution_charter": CHARTER_NORTH,
                "product_id": "prod-1",
                "source_id": "source-1",
                "status": "candidate",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": EVAL_TIME - timedelta(hours=5),
                "published_at": None,
                "valid_until": None,
            },
        )
        # Correction at EVAL_TIME - 3 hours
        conn.execute(
            insert(corrections),
            {
                "correction_id": "corr-1",
                "schema_version": "1.0",
                "offer_id": "offer-corr",
                "field_path": "note_rate",
                "previous_value": "6.000",
                "corrected_value": "6.125",
                "actor": "operator-corr",
                "corrected_at": EVAL_TIME - timedelta(hours=3),
                "reason": "transcription error",
                "fingerprints_json": {},
            },
        )
    engine.dispose()

    # 1. No later review decision -> warn
    report_unresolved = quality_report(seeded_db, now=EVAL_TIME)
    check_unresolved = next(
        c for c in report_unresolved.checks if c.name == "unresolved_corrections"
    )
    assert check_unresolved.status == "warn"
    assert check_unresolved.count == 1
    assert "1 unresolved correction(s)" in check_unresolved.detail
    assert "transcription error" not in check_unresolved.detail  # No free text
    assert report_unresolved.ok is True

    # 2. Later review decision (at EVAL_TIME - 1 hour) -> resolves
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(review_decisions),
            {
                "review_id": "rev-1",
                "schema_version": "1.0",
                "subject": "offer",
                "subject_id": "offer-corr",
                "verdict": "approved",
                "actor": "reviewer-final",
                "decided_at": EVAL_TIME - timedelta(hours=1),
                "rationale": "corrected note rate verified",
                "fingerprints_json": {},
                "reviewed_product_sha256": None,
                "reviewed_evidence_sha256": None,
            },
        )
    engine.dispose()

    report_resolved = quality_report(seeded_db, now=EVAL_TIME)
    check_resolved = next(c for c in report_resolved.checks if c.name == "unresolved_corrections")
    assert check_resolved.status == "ok"
    assert check_resolved.count == 0


def test_report_never_modifies_any_file(seeded_db: Path) -> None:
    """The quality report is strictly read-only and never modifies any file."""
    # Ensure all WAL/SHM files are established before taking initial hash
    report_prime = quality_report(seeded_db, now=EVAL_TIME)
    assert report_prime.ok is True

    before_hashes = _hash_tree(seeded_db.parent)

    report = quality_report(seeded_db, now=EVAL_TIME)
    assert report.ok is True

    after_hashes = _hash_tree(seeded_db.parent)
    assert before_hashes == after_hashes


def test_missing_database_fails_as_single_check(tmp_path: Path) -> None:
    """A missing database produces a single fail check."""
    missing_path = tmp_path / "nonexistent.sqlite"
    report = quality_report(missing_path, now=EVAL_TIME)

    assert isinstance(report, QualityReport)
    assert report.ok is False
    assert len(report.checks) == 1
    assert report.checks[0].name == "database_readable"
    assert report.checks[0].status == "fail"
    assert "not found" in report.checks[0].detail
    assert report.checks[0].count is None


def test_check_exception_becomes_fail_check(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any unhandled exception inside a check becomes a fail check."""

    def _boom(*args: object, **kwargs: object) -> Check:
        raise RuntimeError("simulated check crash")

    monkeypatch.setattr("cu_rates.ops.quality._check_quarantined_offers", _boom)

    report = quality_report(seeded_db, now=EVAL_TIME)
    assert report.ok is False
    failed = next(c for c in report.checks if c.name == "quarantined_offers")
    assert failed.status == "fail"
    assert "simulated check crash" in failed.detail
    assert failed.count is None


def test_cli_exit_codes_and_json_shape(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI prints one JSON object and exits 0 on success/warn, 1 on fail."""
    # 1. OK case -> exit code 0
    exit_code = main(["--database", str(seeded_db), "--now", EVAL_TIME.isoformat()])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert isinstance(payload["checks"], list)
    assert len(payload["checks"]) == 5
    for item in payload["checks"]:
        assert "name" in item
        assert "status" in item
        assert "detail" in item
        assert "count" in item

    # 2. Warning case -> exit code 0
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-warn-cli",
                "schema_version": "1.0",
                "institution_charter": CHARTER_NORTH,
                "product_id": "prod-1",
                "source_id": "source-1",
                "status": "quarantined",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": EVAL_TIME - timedelta(hours=25),
                "published_at": None,
                "valid_until": None,
            },
        )
    engine.dispose()

    exit_code_warn = main(["--database", str(seeded_db), "--now", EVAL_TIME.isoformat()])
    assert exit_code_warn == 0
    payload_warn = json.loads(capsys.readouterr().out)
    assert payload_warn["ok"] is True
    q_check = next(c for c in payload_warn["checks"] if c["name"] == "quarantined_offers")
    assert q_check["status"] == "warn"

    # 3. Failure case -> exit code 1
    missing_db = seeded_db.parent / "missing.sqlite"
    exit_code_fail = main(["--database", str(missing_db), "--now", EVAL_TIME.isoformat()])
    assert exit_code_fail == 1
    payload_fail = json.loads(capsys.readouterr().out)
    assert payload_fail["ok"] is False


def test_relative_path_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Relative paths fail closed without crashing."""
    report = quality_report(Path("relative/db.sqlite"), now=EVAL_TIME)
    assert report.ok is False
    assert len(report.checks) == 1
    assert report.checks[0].status == "fail"
    assert "absolute path" in report.checks[0].detail

    exit_code = main(["--database", "relative/db.sqlite"])
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"][0]["status"] == "fail"
    assert "absolute path" in payload["checks"][0]["detail"]


def test_the_report_opens_the_database_strictly_read_only(seeded_db: Path) -> None:
    """Enforced by SQLite, not file permissions: any write through the tool's connection fails."""
    import sqlite3

    from cu_rates.ops.quality import _open_read_only

    with closing(_open_read_only(seeded_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rate_sources").fetchone() is not None
        for statement in (
            "CREATE TABLE injected (a)",
            "DELETE FROM rate_sources",
            "UPDATE rate_sources SET url = 'x'",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(statement)


def test_an_approval_dated_after_now_does_not_make_a_source_current(seeded_db: Path) -> None:
    """Only decisions already in force count: a future-dated approval cannot create an alert."""
    engine = open_operational_database(seeded_db)
    with engine.begin() as conn:
        conn.execute(
            insert(source_approvals),
            {
                "approval_id": "appr-future",
                "schema_version": "1.0",
                "source_id": "source-1",
                "source_url": "https://north.example/rates",
                "decision": "approved",
                "actor": "reviewer-1",
                "decided_at": EVAL_TIME + timedelta(days=1),
                "policy_id": "pol-1",
                "policy_version": "1.0",
                "rationale": "dated in the future",
            },
        )
    engine.dispose()
    report = quality_report(seeded_db, now=EVAL_TIME)
    stale = next(c for c in report.checks if c.name == "stale_sources")
    assert stale.status == "ok" and stale.count == 0

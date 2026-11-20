"""Comprehensive tests for offline rate batch intake and staging (M4 / T046).

Verifies:
1. Synthetic cases:
   - 30-year fixed purchase mortgage candidate staging
   - HELOC candidate staging and stable hold on unsupported semantic tier data
   - Quoted/no-rate and blocked coverage findings
   - Unknown points (None) vs explicitly zero points (Decimal("0"))
   - Ambiguous evidence locators fail closed to held
   - Unsupported locator kinds (PdfLocator with bbox and extracted_text_sha256) fail closed to held
   - Isolated missing store artifact fails closed to held
   - Independent review disagreement fails closed to auditable hold
   - Staging remains strictly candidate-only (public normalized tables untouched)
2. Replay, conflict, dry-run, atomic rollback, and engine durability:
   - Dry run reconciles counters without mutating SQLite or creating dirs
   - Identical byte replay is an idempotent no-op
   - Reused batch ID with altered contents raises BatchConflictError
   - Atomic rollback under injected mid-import failure leaves zero records
   - Engine re-open durability preserves staged state across engine disposal
3. Registry bindings & isolation:
   - Rejection of unbound institution charter (RejectReason.INSTITUTION_NOT_FOUND)
   - Rejection of unbound product (RejectReason.PRODUCT_NOT_FOUND)
   - Rejection of unbound relationship (RejectReason.RELATIONSHIP_NOT_FOUND)
   - Rejection of source URL mismatch (RejectReason.CROSS_REFERENCE_MISMATCH)
   - Outsider no cross-run rebind (RejectReason.FETCH_RUN_NOT_FOUND)
   - Staging is approval-independent (unverified charter and unapproved relationship stage)
4. Contract boundaries & security:
   - Rejection of binary floats, future timestamps, tampered digests, forbidden publication keys,
     oversized payloads, and null bytes
5. Migration & guarded downgrade:
   - Fresh upgrade creates tables and triggers
   - Guarded downgrade refuses while staged records exist
   - Guarded downgrade succeeds once records are cleared
   - Append-only triggers prevent UPDATE and DELETE
6. CLI interface:
   - Dry run, live import with --json, and replay
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, insert, select, text

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import (
    ConformingStatus,
    EvidenceKind,
    FetchMethod,
    GovernmentGuaranty,
    InstitutionStatus,
    ProductFamily,
    ProductLifecycle,
    RateAvailability,
    RunStatus,
    SourceRelationshipKind,
)
from cu_rates.db import (
    AppendOnlyError,
    downgrade_database,
    open_operational_database,
    upgrade_database,
)
from cu_rates.db.schema import (
    fetch_profiles,
    fetch_runs,
    institution_source_relationships,
    institutions,
    normalized_offers,
    products,
    rate_sources,
    staged_rate_batches,
    staged_rate_candidates,
    staged_rate_findings,
)
from cu_rates.rate_batch.cli import main as cli_main
from cu_rates.rate_batch.models import (
    BATCH_SCHEMA_VERSION,
    BatchConflictError,
    ForbiddenFieldError,
    HashTamperingError,
    HoldReason,
    OversizedInputError,
    RejectReason,
)
from cu_rates.rate_batch.parser import compute_batch_digest, parse_rate_batch
from cu_rates.rate_batch.staging import RateBatchStager

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
CHARTER = 12345
SOURCE_ID = "src-synthetic-rates"
SOURCE_URL = "https://example.invalid/synthetic/rates"
PRODUCT_MORTGAGE = "prod-30y-fixed"
PRODUCT_HELOC = "prod-heloc"
RUN_ID = "run-20261115-001"
PROFILE_ID = "profile-synthetic-001"

# Structural synthetic HTML verified against extract.py and locators.py semantics
HTML_CONTENT = (
    b"""<!DOCTYPE html>
<html>
<head><title>Synthetic Rates</title></head>
<body>
<div id="mortgage-rates">
  <h1 id="heading-mortgage">Mortgage and HELOC Rates</h1>
  <table id="tbl-fixed">
    <thead>
      <tr>
        <th>Product</th>
        <th>Term</th>
        <th>Note Rate</th>
        <th>APR</th>
        <th>Points</th>
      </tr>
    </thead>
    <tbody>
      <tr id="row-001">
        <td class="prod">30-Year Fixed Purchase</td>
        <td class="term">30 Years</td>
        <td class="rate">6.125%</td>
        <td class="apr">6.250%</td>
        <td class="pts">0 points</td>
      </tr>
      <tr id="row-15y">
        <td class="prod">15-Year Fixed Purchase</td>
        <td class="term">15 Years</td>
        <td class="rate">5.875%</td>
        <td class="apr">5.950%</td>
        <td class="pts">0 points</td>
      </tr>
      <tr id="row-ambiguous-rates">
        <td class="prod">Adjustable Rate Purchase</td>
        <td class="term">30 Years</td>
        <td class="rate">Rate: 6.125% APR: 6.250%</td>
        <td class="apr">6.250%</td>
        <td class="pts">0 points</td>
      </tr>
    </tbody>
  </table>
</div>
<div id="heloc-rates">
  <table id="tbl-heloc">
    <thead>
      <tr>
        <th>Product</th>
        <th>Note Rate</th>
        <th>APR</th>
      </tr>
    </thead>
    <tbody>
      <tr id="row-heloc-001">
        <td class="prod">Home Equity Line of Credit Prime</td>
        <td class="rate">7.500%</td>
        <td class="apr">7.750%</td>
      </tr>
      <tr id="row-heloc-tier">
        <td class="prod">Home Equity Line of Credit Tier 1</td>
        <td class="rate">7.250%</td>
        <td class="apr">7.250%</td>
      </tr>
    </tbody>
  </table>
</div>
<div id="coverage-findings">
  <p id="finding-quote">Mortgage rates available by phone or online application only. """
    b"""Call 800-555-0199 for a quote.</p>
  <p id="finding-blocked">Rate viewing is restricted to authenticated members.</p>
</div>
</body>
</html>
"""
)
HTML_SHA256 = hashlib.sha256(HTML_CONTENT).hexdigest()

NOTE_RATE_TEXT_SHA = hashlib.sha256(b"6.125%").hexdigest()
APR_TEXT_SHA = hashlib.sha256(b"6.250%").hexdigest()
POINTS_TEXT_SHA = hashlib.sha256(b"0 points").hexdigest()

HELOC_NOTE_RATE_SHA = hashlib.sha256(b"7.500%").hexdigest()
HELOC_APR_SHA = hashlib.sha256(b"7.750%").hexdigest()

FINDING_QUOTE_TEXT = (
    "Mortgage rates available by phone or online application only. Call 800-555-0199 for a quote."
)
FINDING_QUOTE_SHA = hashlib.sha256(FINDING_QUOTE_TEXT.encode("utf-8")).hexdigest()

FINDING_BLOCKED_TEXT = "Rate viewing is restricted to authenticated members."
FINDING_BLOCKED_SHA = hashlib.sha256(FINDING_BLOCKED_TEXT.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Engine, ArtifactStore, Path, Path]]:
    db_path = tmp_path / "operational.sqlite"
    upgrade_database(db_path)
    engine = open_operational_database(db_path)
    store_path = tmp_path / "evidence"
    store = ArtifactStore(store_path)

    # Seed DB identity bindings (approval-independent)
    with engine.begin() as conn:
        conn.execute(
            insert(institutions).values(
                charter_number=CHARTER,
                schema_version="1.0",
                legal_name="Synthetic Credit Union",
                status=InstitutionStatus.ACTIVE.value,
                charter_verified=0,  # Unverified charter: staging is approval-independent
            )
        )
        conn.execute(
            insert(rate_sources).values(
                source_id=SOURCE_ID,
                schema_version="1.0",
                url=SOURCE_URL,
                evidence_kind=EvidenceKind.HTML.value,
                description="Synthetic rate source",
            )
        )
        conn.execute(
            insert(fetch_profiles).values(
                profile_id=PROFILE_ID,
                schema_version="1.0",
                source_id=SOURCE_ID,
                method=FetchMethod.HTTP_GET.value,
                min_interval_seconds=60,
                timeout_seconds=30,
                respect_robots=1,
            )
        )
        conn.execute(
            insert(fetch_runs).values(
                run_id=RUN_ID,
                schema_version="1.0",
                source_id=SOURCE_ID,
                profile_id=PROFILE_ID,
                status=RunStatus.SUCCEEDED.value,
                started_at=T0 - timedelta(minutes=5),
                finished_at=T0,
                git_sha="0123456789abcdef0123456789abcdef01234567",
            )
        )
        conn.execute(
            insert(products).values(
                product_id=PRODUCT_MORTGAGE,
                schema_version="1.0",
                institution_charter=CHARTER,
                family=ProductFamily.MORTGAGE.value,
                name="30-Year Fixed Mortgage",
                lifecycle=ProductLifecycle.ACTIVE.value,
                rate_availability=RateAvailability.PUBLIC_RATE.value,
                conforming_status=ConformingStatus.CONFORMING.value,
                government_guaranty=GovernmentGuaranty.NONE.value,
                notes_json=[],
            )
        )
        conn.execute(
            insert(products).values(
                product_id=PRODUCT_HELOC,
                schema_version="1.0",
                institution_charter=CHARTER,
                family=ProductFamily.HELOC.value,
                name="Home Equity Line of Credit",
                lifecycle=ProductLifecycle.ACTIVE.value,
                rate_availability=RateAvailability.PUBLIC_RATE.value,
                conforming_status=ConformingStatus.CONFORMING.value,
                government_guaranty=GovernmentGuaranty.NONE.value,
                notes_json=[],
            )
        )
        # Relationship exists without approved_by / approved_at
        conn.execute(
            insert(institution_source_relationships).values(
                relationship_id="rel-synthetic-001",
                schema_version="1.0",
                institution_charter=CHARTER,
                source_id=SOURCE_ID,
                kind=SourceRelationshipKind.FIRST_PARTY.value,
                approved_by=None,
                approved_at=None,
            )
        )

    # Put observation into ArtifactStore
    store.put(
        HTML_CONTENT,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id=RUN_ID,
    )

    yield engine, store, db_path, store_path
    engine.dispose()


def make_envelope_payload(
    *,
    batch_id: str = "batch-test-001",
    candidates: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    independent_review: dict[str, Any] | None = None,
    tamper_digest: bool = False,
    submitted_at: datetime = T0,
) -> bytes:
    if observations is None:
        observations = [
            {
                "institution_charter": CHARTER,
                "source_id": SOURCE_ID,
                "source_url": SOURCE_URL,
                "product_id": PRODUCT_MORTGAGE,
                "fetch_run_id": RUN_ID,
                "observed_at": T0.isoformat(),
                "artifact_id": HTML_SHA256,
                "media_type": "text/html; charset=utf-8",
            }
        ]

    data: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "operator": "Synthetic Operator",
        "model_name": "gemini-3.8-flash",
        "tool_name": "extractor-v2",
        "recipe_version": "recipe-2026-11",
        "submitted_at": submitted_at.isoformat(),
        "observations": observations,
        "candidates": candidates or [],
        "findings": findings or [],
    }
    if independent_review:
        data["independent_review"] = independent_review

    digest = compute_batch_digest(data)
    if tamper_digest:
        digest = "0" * 64
    data["batch_digest"] = digest
    return json.dumps(data, indent=2).encode("utf-8")


def _build_valid_30y_candidate(
    candidate_id: str = "cand-30y-001",
    row_id: str = "row-001",
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "row_id": row_id,
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "apr": "6.250",
        "points": "0",
        "raw_text": "30-Year Fixed Purchase 30 Years 6.125% 6.250% 0 points",
        "field_raw_texts": {
            "note_rate": "6.125%",
            "apr": "6.250%",
            "points": "0 points",
        },
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(4)",
                "normalized_text_sha256": APR_TEXT_SHA,
            },
            "points": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(5)",
                "normalized_text_sha256": POINTS_TEXT_SHA,
            },
        },
        "observed_at": T0.isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. Synthetic Cases & Candidate-Only Staging
# ---------------------------------------------------------------------------


def test_stage_30y_mortgage_candidate(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate()
    payload = make_envelope_payload(candidates=[cand])
    report = stager.stage(payload)

    assert not report.is_noop
    assert not report.dry_run
    assert report.submitted == 1
    assert report.staged == 1
    assert report.held == 0
    assert report.rejected == 0
    assert report.already_imported == 0

    # Verify rows in private staging table
    with engine.connect() as conn:
        batches = conn.execute(select(staged_rate_batches)).all()
        assert len(batches) == 1
        assert batches[0].batch_id == "batch-test-001"
        assert batches[0].staged_count == 1

        cands = conn.execute(select(staged_rate_candidates)).all()
        assert len(cands) == 1
        assert cands[0].candidate_id == "cand-30y-001"
        assert cands[0].disposition == "staged"
        assert cands[0].hold_reason is None
        assert cands[0].note_rate == Decimal("6.125")
        assert cands[0].apr == Decimal("6.250")
        assert cands[0].points == Decimal("0")

        # Public tables remain completely untouched
        public_offers = conn.execute(select(normalized_offers)).all()
        assert len(public_offers) == 0


def test_stage_heloc_with_lower_cltv_tier_holds_unsupported(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    """Semantic tier data without verified evidence locator must assert stable hold."""
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = {
        "candidate_id": "cand-heloc-001",
        "row_id": "row-heloc-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_HELOC,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "heloc",
        "note_rate": "7.500",
        "apr": "7.750",
        "heloc_terms": {
            "index_name": "Prime",
            "index_rate": "7.500",
            "margin": "0.250",
            "max_cltv": "80",
            "lien_position": "second",
        },
        "tiers": [
            {
                "tier_id": "tier-cltv-70",
                "bounds": [{"dimension": "cltv", "lower": "0", "upper": "70"}],
                "apr": "7.250",
                "raw_text": "CLTV <= 70% gets 7.250% APR",
            }
        ],
        "raw_text": "Home Equity Line of Credit Prime 7.500% 7.750%",
        "field_raw_texts": {
            "note_rate": "7.500%",
            "apr": "7.750%",
        },
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-heloc-001 > td:nth-child(2)",
                "normalized_text_sha256": HELOC_NOTE_RATE_SHA,
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-heloc-001 > td:nth-child(3)",
                "normalized_text_sha256": HELOC_APR_SHA,
            },
        },
        "observed_at": T0.isoformat(),
    }

    obs = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "product_id": PRODUCT_HELOC,
            "fetch_run_id": RUN_ID,
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]

    payload = make_envelope_payload(batch_id="batch-heloc-001", candidates=[cand], observations=obs)
    report = stager.stage(payload)

    # Fails closed to held because tier lacks evidence locator
    assert report.staged == 0
    assert report.held == 1
    assert report.rejected == 0
    assert report.hold_reasons.get(HoldReason.EVIDENCE_MISSING.value) == 1

    with engine.connect() as conn:
        row = conn.execute(
            select(staged_rate_candidates).where(
                staged_rate_candidates.c.candidate_id == "cand-heloc-001"
            )
        ).first()
        assert row is not None
        assert row.disposition == "held"
        assert row.hold_reason == HoldReason.EVIDENCE_MISSING.value
        assert row.product_family == "heloc"
        assert row.payload_json["heloc_terms"]["index_name"] == "Prime"
        assert len(row.payload_json["tiers"]) == 1


def test_stage_heloc_candidate_clean_staging(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    """HELOC candidate with exact structural locators stages cleanly."""
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = {
        "candidate_id": "cand-heloc-clean",
        "row_id": "row-heloc-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_HELOC,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "heloc",
        "note_rate": "7.500",
        "apr": "7.750",
        "heloc_terms": {
            "index_name": "Prime",
            "index_rate": "7.500",
            "margin": "0.250",
            "max_cltv": "80",
            "lien_position": "second",
        },
        "raw_text": "Home Equity Line of Credit Prime 7.500% 7.750%",
        "field_raw_texts": {
            "note_rate": "7.500%",
            "apr": "7.750%",
        },
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-heloc-001 > td:nth-child(2)",
                "normalized_text_sha256": HELOC_NOTE_RATE_SHA,
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-heloc-001 > td:nth-child(3)",
                "normalized_text_sha256": HELOC_APR_SHA,
            },
        },
        "observed_at": T0.isoformat(),
    }

    obs = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "product_id": PRODUCT_HELOC,
            "fetch_run_id": RUN_ID,
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]

    payload = make_envelope_payload(
        batch_id="batch-heloc-clean", candidates=[cand], observations=obs
    )
    report = stager.stage(payload)

    assert report.staged == 1
    assert report.held == 0
    assert report.rejected == 0

    with engine.connect() as conn:
        row = conn.execute(
            select(staged_rate_candidates).where(
                staged_rate_candidates.c.candidate_id == "cand-heloc-clean"
            )
        ).first()
        assert row is not None
        assert row.disposition == "staged"
        assert row.hold_reason is None
        assert row.note_rate == Decimal("7.500")
        assert row.apr == Decimal("7.750")


def test_stage_quote_only_and_blocked_findings(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    findings = [
        {
            "finding_id": "find-quote-001",
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "fetch_run_id": RUN_ID,
            "artifact_id": HTML_SHA256,
            "observed_at": T0.isoformat(),
            "label": "Mortgage quote only",
            "scope": "in_scope",
            "family": "mortgage",
            "rate_availability": "quote_only",
            "raw_text": FINDING_QUOTE_TEXT,
            "locators": {
                "availability": {
                    "kind": "html",
                    "selector_type": "css",
                    "selector": "#finding-quote",
                    "normalized_text_sha256": FINDING_QUOTE_SHA,
                }
            },
        },
        {
            "finding_id": "find-blocked-002",
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "fetch_run_id": RUN_ID,
            "artifact_id": HTML_SHA256,
            "observed_at": T0.isoformat(),
            "label": "HELOC blocked",
            "scope": "in_scope",
            "family": "heloc",
            "rate_availability": "blocked",
            "raw_text": FINDING_BLOCKED_TEXT,
            "locators": {
                "availability": {
                    "kind": "html",
                    "selector_type": "css",
                    "selector": "#finding-blocked",
                    "normalized_text_sha256": FINDING_BLOCKED_SHA,
                }
            },
        },
    ]

    payload = make_envelope_payload(batch_id="batch-find-001", findings=findings)
    report = stager.stage(payload)

    assert report.submitted == 2
    assert report.staged == 2
    assert report.held == 0
    assert report.rejected == 0

    with engine.connect() as conn:
        rows = conn.execute(select(staged_rate_findings)).all()
        assert len(rows) == 2
        availabilities = {r.rate_availability for r in rows}
        assert availabilities == {"quote_only", "blocked"}


def test_unknown_points_vs_explicit_zero_points(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand_unknown_points = {
        "candidate_id": "cand-unknown-pts",
        "row_id": "row-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "apr": "6.250",
        "points": None,  # Preserves missing / unknown points
        "raw_text": "30-Year Fixed Purchase 30 Years 6.125% 6.250%",
        "field_raw_texts": {"note_rate": "6.125%", "apr": "6.250%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(4)",
                "normalized_text_sha256": APR_TEXT_SHA,
            },
        },
        "observed_at": T0.isoformat(),
    }

    cand_zero_points = _build_valid_30y_candidate(candidate_id="cand-zero-pts", row_id="row-001")

    payload = make_envelope_payload(
        batch_id="batch-pts-001",
        candidates=[cand_unknown_points, cand_zero_points],
    )
    report = stager.stage(payload)

    assert report.staged == 2
    with engine.connect() as conn:
        u_row = conn.execute(
            select(staged_rate_candidates).where(
                staged_rate_candidates.c.candidate_id == "cand-unknown-pts"
            )
        ).first()
        assert u_row is not None
        assert u_row.points is None

        z_row = conn.execute(
            select(staged_rate_candidates).where(
                staged_rate_candidates.c.candidate_id == "cand-zero-pts"
            )
        ).first()
        assert z_row is not None
        assert z_row.points == Decimal("0")


def test_holds_for_ambiguous_and_unsupported_locators(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    # 1. Ambiguous evidence: node text contains multiple rates
    ambiguous_sha = hashlib.sha256(b"Rate: 6.125% APR: 6.250%").hexdigest()
    cand_ambiguous = {
        "candidate_id": "cand-ambiguous",
        "row_id": "row-ambiguous-rates",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "raw_text": "Adjustable Rate Purchase 6.125%",
        "field_raw_texts": {"note_rate": "6.125%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-ambiguous-rates > td:nth-child(3)",
                "normalized_text_sha256": ambiguous_sha,
            }
        },
        "observed_at": T0.isoformat(),
    }

    # 2. Unsupported locator kind with actual PdfLocator keys: bbox and extracted_text_sha256
    cand_unsupported_pdf = {
        "candidate_id": "cand-unsupported-pdf",
        "row_id": "row-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "raw_text": "6.125%",
        "field_raw_texts": {"note_rate": "6.125%"},
        "locators": {
            "note_rate": {
                "kind": "pdf",
                "page": 1,
                "bbox": {"x0": "10", "y0": "10", "x1": "50", "y1": "50"},
                "extracted_text_sha256": NOTE_RATE_TEXT_SHA,
            }
        },
        "observed_at": T0.isoformat(),
    }

    payload = make_envelope_payload(
        batch_id="batch-holds-001",
        candidates=[cand_ambiguous, cand_unsupported_pdf],
    )
    report = stager.stage(payload)

    assert report.staged == 0
    assert report.held == 2
    assert report.rejected == 0
    assert report.hold_reasons.get(HoldReason.EVIDENCE_AMBIGUOUS.value) == 1
    assert report.hold_reasons.get(HoldReason.LOCATOR_UNSUPPORTED.value) == 1

    with engine.connect() as conn:
        held_rows = conn.execute(
            select(staged_rate_candidates).where(staged_rate_candidates.c.disposition == "held")
        ).all()
        assert len(held_rows) == 2


def test_missing_artifact_held_at_storage_layer(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    """Isolated test for missing artifact in store: fails closed to held without parser reject."""
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    missing_run_id = "run-20261115-miss"
    missing_content = b"<html><body><p id='sample'>Valid</p></body></html>"
    missing_sha = hashlib.sha256(missing_content).hexdigest()

    # Seed run in DB
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_runs).values(
                run_id=missing_run_id,
                schema_version="1.0",
                source_id=SOURCE_ID,
                profile_id=PROFILE_ID,
                status=RunStatus.SUCCEEDED.value,
                started_at=T0 - timedelta(minutes=5),
                finished_at=T0,
                git_sha="0123456789abcdef0123456789abcdef01234567",
            )
        )

    store.put(
        missing_content,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id=missing_run_id,
    )

    # Unlink the blob to simulate missing artifact body in store
    blob_path = store._blob_path(missing_sha)
    blob_path.unlink()

    cand = {
        "candidate_id": "cand-missing-blob",
        "row_id": "row-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": missing_run_id,
        "artifact_id": missing_sha,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "apr": "6.250",
        "raw_text": "30-Year Fixed Purchase 6.125% 6.250%",
        "field_raw_texts": {"note_rate": "6.125%", "apr": "6.250%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(4)",
                "normalized_text_sha256": APR_TEXT_SHA,
            },
        },
        "observed_at": T0.isoformat(),
    }

    obs = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "fetch_run_id": missing_run_id,
            "observed_at": T0.isoformat(),
            "artifact_id": missing_sha,
            "media_type": "text/html; charset=utf-8",
        }
    ]

    payload = make_envelope_payload(
        batch_id="batch-miss-001",
        candidates=[cand],
        observations=obs,
    )
    report = stager.stage(payload)

    assert report.staged == 0
    assert report.held == 1
    assert report.rejected == 0
    assert report.hold_reasons.get(HoldReason.ARTIFACT_NOT_FOUND.value) == 1

    with engine.connect() as conn:
        row = conn.execute(
            select(staged_rate_candidates).where(
                staged_rate_candidates.c.candidate_id == "cand-missing-blob"
            )
        ).first()
        assert row is not None
        assert row.disposition == "held"
        assert row.hold_reason == "artifact_not_found"


@pytest.mark.parametrize(
    ("concurrence", "disagreements"),
    [(True, ["apr"]), (False, ["points"]), (False, [])],
)
def test_independent_review_disagreement_causes_auditable_hold(
    env: tuple[Engine, ArtifactStore, Path, Path],
    concurrence: bool,
    disagreements: list[str],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-review-disagree")

    review = {
        "reviewer_model": "gpt-6.1-sol",
        "recipe_version": "recipe-check-v1",
        "reviewed_at": T0.isoformat(),
        "concurrence": concurrence,
        "disagreements": disagreements,
        "notes": "Reviewer disagrees with the extracted APR.",
    }

    payload = make_envelope_payload(
        batch_id="batch-rev-001",
        candidates=[cand],
        independent_review=review,
    )
    report = stager.stage(payload)

    assert report.held == 1
    assert report.staged == 0
    assert report.rejected == 0
    assert report.hold_reasons.get(HoldReason.INDEPENDENT_REVIEW_DISAGREEMENT.value) == 1


# ---------------------------------------------------------------------------
# 2. Replay, Conflict, Dry Run, Atomic Rollback & Engine Durability
# ---------------------------------------------------------------------------


def test_identical_byte_replay_is_noop(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-replay-001")
    payload = make_envelope_payload(batch_id="batch-replay-001", candidates=[cand])

    first = stager.stage(payload)
    assert not first.is_noop
    assert first.staged == 1

    # Second import with identical bytes
    second = stager.stage(payload)
    assert second.is_noop is True
    assert second.already_imported == 1
    assert second.staged == 0
    assert second.batch_id == first.batch_id
    assert second.batch_digest == first.batch_digest

    with engine.connect() as conn:
        count = conn.execute(select(staged_rate_batches)).all()
        assert len(count) == 1


def test_reused_id_with_altered_contents_conflicts(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand1 = _build_valid_30y_candidate(candidate_id="cand-c-001")
    payload1 = make_envelope_payload(batch_id="batch-conflict-001", candidates=[cand1])
    stager.stage(payload1)

    # Different content using the SAME batch_id
    cand2 = dict(cand1, term_months=180)
    payload2 = make_envelope_payload(batch_id="batch-conflict-001", candidates=[cand2])

    with pytest.raises(BatchConflictError, match="different contents"):
        stager.stage(payload2)


def test_dry_run_is_strictly_nonmutating(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-dry-001")
    payload = make_envelope_payload(batch_id="batch-dry-001", candidates=[cand])
    report = stager.stage(payload, dry_run=True)

    assert report.dry_run is True
    assert report.submitted == 1
    assert report.staged == 1
    assert report.held == 0
    assert report.rejected == 0

    # Database has zero rows in staging and normalized tables
    with engine.connect() as conn:
        assert len(conn.execute(select(staged_rate_batches)).all()) == 0
        assert len(conn.execute(select(staged_rate_candidates)).all()) == 0
        assert len(conn.execute(select(staged_rate_findings)).all()) == 0
        assert len(conn.execute(select(normalized_offers)).all()) == 0


def test_injected_mid_import_failure_rolls_back_atomically(
    env: tuple[Engine, ArtifactStore, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-rollback-001")
    payload = make_envelope_payload(batch_id="batch-rollback-001", candidates=[cand])

    with engine.connect() as conn:
        original_execute = conn.execute

        def flaky_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            sql_str = str(statement)
            if "staged_rate_candidates" in sql_str:
                raise RuntimeError("simulated database crash mid-transaction")
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", flaky_execute)

        with pytest.raises(RuntimeError, match="simulated database crash"):
            stager._stage_with_connection(
                conn,
                parse_rate_batch(payload, now=T1),
                hashlib.sha256(payload).hexdigest(),
                dry_run=False,
                eval_now=T1,
            )

    # Verify no partial SQLite state
    with engine.connect() as conn:
        assert len(conn.execute(select(staged_rate_batches)).all()) == 0
        assert len(conn.execute(select(staged_rate_candidates)).all()) == 0
        assert len(conn.execute(select(staged_rate_findings)).all()) == 0
        assert len(conn.execute(select(normalized_offers)).all()) == 0


def test_strong_engine_reopen_durability(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    """Verifies that staged records persist cleanly across engine disposal and re-opening."""
    engine, store, db_path, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-durable-001")
    finding = {
        "finding_id": "find-durable-001",
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "observed_at": T0.isoformat(),
        "label": "Mortgage quote only",
        "scope": "in_scope",
        "family": "mortgage",
        "rate_availability": "quote_only",
        "raw_text": FINDING_QUOTE_TEXT,
        "locators": {
            "availability": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#finding-quote",
                "normalized_text_sha256": FINDING_QUOTE_SHA,
            }
        },
    }

    payload = make_envelope_payload(
        batch_id="batch-durability-001",
        candidates=[cand],
        findings=[finding],
    )
    report = stager.stage(payload)
    assert report.staged == 2
    assert report.held == 0

    # Re-open engine
    engine.dispose()
    reopened_engine = open_operational_database(db_path)

    with reopened_engine.connect() as conn:
        batches = conn.execute(select(staged_rate_batches)).all()
        assert len(batches) == 1
        assert batches[0].batch_id == "batch-durability-001"
        assert batches[0].staged_count == 2

        candidates = conn.execute(select(staged_rate_candidates)).all()
        assert len(candidates) == 1
        assert candidates[0].candidate_id == "cand-durable-001"
        assert candidates[0].disposition == "staged"
        assert candidates[0].note_rate == Decimal("6.125")

        findings_rows = conn.execute(select(staged_rate_findings)).all()
        assert len(findings_rows) == 1
        assert findings_rows[0].finding_id == "find-durable-001"
        assert findings_rows[0].disposition == "staged"

        # Public tables remain untouched
        assert len(conn.execute(select(normalized_offers)).all()) == 0

    reopened_engine.dispose()


# ---------------------------------------------------------------------------
# 3. Identity Bindings, Cross-Run Rebind Protection, & Approval Independence
# ---------------------------------------------------------------------------


def test_rejection_of_unbound_institution_product_and_source_url(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    # 1. Unknown institution charter (99999)
    cand_unknown_charter = {
        "candidate_id": "cand-bad-charter",
        "row_id": "row-001",
        "institution_charter": 99999,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "raw_text": "30-Year Fixed Purchase 6.125%",
        "field_raw_texts": {"note_rate": "6.125%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            }
        },
        "observed_at": T0.isoformat(),
    }
    obs_bad_charter = [
        {
            "institution_charter": 99999,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "fetch_run_id": RUN_ID,
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]

    p1 = make_envelope_payload(
        batch_id="batch-bad-charter",
        candidates=[cand_unknown_charter],
        observations=obs_bad_charter,
    )
    r1 = stager.stage(p1)
    assert r1.rejected == 1
    assert r1.reject_reasons.get(RejectReason.INSTITUTION_NOT_FOUND.value) == 1

    # 2. Unknown product in DB: let the stager check it instead of the batch parser.
    cand_bad_prod = {
        "candidate_id": "cand-bad-prod",
        "row_id": "row-001",
        "institution_charter": CHARTER,
        "product_id": "nonexistent-product-id",
        "source_id": SOURCE_ID,
        "fetch_run_id": RUN_ID,
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "raw_text": "30-Year Fixed Purchase 6.125%",
        "field_raw_texts": {"note_rate": "6.125%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            }
        },
        "observed_at": T0.isoformat(),
    }
    obs_no_prod = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "fetch_run_id": RUN_ID,
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]
    p2 = make_envelope_payload(
        batch_id="batch-bad-prod",
        candidates=[cand_bad_prod],
        observations=obs_no_prod,
    )
    r2 = stager.stage(p2)
    assert r2.rejected == 1
    assert r2.reject_reasons.get(RejectReason.PRODUCT_NOT_FOUND.value) == 1

    # 3. Source URL mismatch
    obs_bad_url = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": "https://different.invalid/rates",
            "fetch_run_id": RUN_ID,
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]
    p3 = make_envelope_payload(
        batch_id="batch-bad-url",
        candidates=[
            dict(
                cand_unknown_charter,
                candidate_id="cand-url-mismatch",
                institution_charter=CHARTER,
                product_id=PRODUCT_MORTGAGE,
            )
        ],
        observations=obs_bad_url,
    )
    r3 = stager.stage(p3)
    assert r3.rejected == 1
    assert r3.reject_reasons.get(RejectReason.CROSS_REFERENCE_MISMATCH.value) == 1


def test_outsider_no_cross_run_rebind(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    """Outsider attempting to bind an undeclared/foreign fetch run is rejected."""
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = {
        "candidate_id": "cand-foreign-run",
        "row_id": "row-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_MORTGAGE,
        "source_id": SOURCE_ID,
        "fetch_run_id": "run-foreign-999",
        "artifact_id": HTML_SHA256,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.125",
        "raw_text": "30-Year Fixed Purchase 6.125%",
        "field_raw_texts": {"note_rate": "6.125%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#row-001 > td:nth-child(3)",
                "normalized_text_sha256": NOTE_RATE_TEXT_SHA,
            }
        },
        "observed_at": T0.isoformat(),
    }
    obs = [
        {
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "fetch_run_id": "run-foreign-999",
            "observed_at": T0.isoformat(),
            "artifact_id": HTML_SHA256,
            "media_type": "text/html; charset=utf-8",
        }
    ]

    payload = make_envelope_payload(
        batch_id="batch-outsider-run",
        candidates=[cand],
        observations=obs,
    )
    report = stager.stage(payload)

    assert report.rejected == 1
    assert report.staged == 0
    assert report.held == 0
    assert report.reject_reasons.get(RejectReason.FETCH_RUN_NOT_FOUND.value) == 1

    with engine.connect() as conn:
        rejected = conn.execute(select(staged_rate_candidates)).one()
        assert rejected.disposition == "rejected"
        assert rejected.reject_reason == RejectReason.FETCH_RUN_NOT_FOUND.value
        assert conn.execute(select(normalized_offers)).first() is None


def test_staging_is_approval_independent(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    """Unverified charter and unapproved relationship still stage successfully."""
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    # Confirm fixture has unverified charter and unapproved relationship
    with engine.connect() as conn:
        charter_verified = conn.execute(
            select(institutions.c.charter_verified).where(institutions.c.charter_number == CHARTER)
        ).scalar_one()
        assert charter_verified == 0

        approved_by = conn.execute(
            select(institution_source_relationships.c.approved_by).where(
                institution_source_relationships.c.institution_charter == CHARTER
            )
        ).scalar_one()
        assert approved_by is None

    cand = _build_valid_30y_candidate(candidate_id="cand-approval-indep")
    payload = make_envelope_payload(batch_id="batch-approval-indep", candidates=[cand])
    report = stager.stage(payload)

    assert report.staged == 1
    assert report.held == 0
    assert report.rejected == 0


def test_mortgage_cannot_borrow_a_heloc_product_id(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    candidate = _build_valid_30y_candidate(candidate_id="cand-wrong-registered-family")
    candidate["product_id"] = PRODUCT_HELOC
    observation = {
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "product_id": PRODUCT_HELOC,
        "fetch_run_id": RUN_ID,
        "observed_at": T0.isoformat(),
        "artifact_id": HTML_SHA256,
        "media_type": "text/html; charset=utf-8",
    }
    report = RateBatchStager(engine, store, clock=lambda: T1).stage(
        make_envelope_payload(candidates=[candidate], observations=[observation])
    )
    assert report.staged == 0
    assert report.reject_reasons == {RejectReason.CROSS_REFERENCE_MISMATCH.value: 1}


def test_caller_legacy_savepoint_cannot_commit_early(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, database, _ = env
    legacy = create_engine(f"sqlite:///{database}")
    try:
        with (
            legacy.connect() as conn,
            conn.begin(),
            pytest.raises(ValueError, match="already hold a SQLite transaction"),
        ):
            RateBatchStager(conn, store, clock=lambda: T1).stage(make_envelope_payload())
    finally:
        legacy.dispose()
    with engine.connect() as conn:
        assert conn.execute(select(staged_rate_batches)).first() is None


def test_reobservation_cannot_stage_as_full_body(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    revalidation_run_id = "run-synthetic-304"
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_runs).values(
                run_id=revalidation_run_id,
                schema_version="1.0",
                source_id=SOURCE_ID,
                profile_id=PROFILE_ID,
                status=RunStatus.SUCCEEDED.value,
                started_at=T0 - timedelta(minutes=1),
                finished_at=T0,
                git_sha="0123456789abcdef0123456789abcdef01234567",
            )
        )
    store.put(
        HTML_CONTENT,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=304,
        fetch_run_id=revalidation_run_id,
    )
    candidate = _build_valid_30y_candidate(candidate_id="cand-304-only")
    candidate["fetch_run_id"] = revalidation_run_id
    observation = {
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "fetch_run_id": revalidation_run_id,
        "observed_at": T0.isoformat(),
        "artifact_id": HTML_SHA256,
        "media_type": "text/html; charset=utf-8",
    }
    report = RateBatchStager(engine, store, clock=lambda: T1).stage(
        make_envelope_payload(candidates=[candidate], observations=[observation])
    )
    assert report.staged == 0
    assert report.reject_reasons == {RejectReason.CROSS_REFERENCE_MISMATCH.value: 1}


def test_explicit_zero_is_not_proof_points_are_not_applicable(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    candidate = _build_valid_30y_candidate(candidate_id="cand-na-disagrees")
    candidate["points_not_applicable"] = True
    candidate["points"] = None

    report = RateBatchStager(engine, store, clock=lambda: T1).stage(
        make_envelope_payload(candidates=[candidate])
    )
    assert report.held == 1
    assert report.hold_reasons == {HoldReason.RAW_TEXT_MISMATCH.value: 1}
    with engine.connect() as conn:
        saved = conn.execute(select(staged_rate_candidates)).one()
        assert saved.payload_json["points_not_applicable"] is True
        assert saved.points is None


# ---------------------------------------------------------------------------
# 4. Security & Contract Boundaries
# ---------------------------------------------------------------------------


def test_reject_floats_in_rate_batch(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    raw = (
        b'{\n  "schema_version": "1.0",\n  "batch_id": "b-float",\n'
        b'  "batch_digest": "0000000000000000000000000000000000000000000000000000000000000000",\n'
        b'  "operator": "op",\n  "model_name": "m",\n  "tool_name": "t",\n'
        b'  "recipe_version": "r",\n  "submitted_at": "2026-11-15T12:00:00Z",\n'
        b'  "observations": [],\n  "candidates": [{"note_rate": 6.125}]\n}'
    )
    with pytest.raises(ValueError, match="binary floats are rejected"):
        stager.stage(raw)


def test_reject_future_timestamps(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T0)

    future_time = T0 + timedelta(days=5)
    payload = make_envelope_payload(batch_id="b-future", submitted_at=future_time)
    with pytest.raises(ValueError, match="in the future"):
        stager.stage(payload, now=T0)


def test_reject_hash_tampering(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    payload = make_envelope_payload(batch_id="b-tampered", tamper_digest=True)
    with pytest.raises(HashTamperingError, match="does not match computed"):
        stager.stage(payload)


def test_reject_forbidden_publication_fields(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-forbidden")
    cand["published"] = True  # Forbidden field

    raw_dict = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": "batch-forbidden",
        "operator": "Op",
        "model_name": "M",
        "tool_name": "T",
        "recipe_version": "R",
        "submitted_at": T0.isoformat(),
        "observations": [],
        "candidates": [cand],
        "findings": [],
    }
    raw_dict["batch_digest"] = compute_batch_digest(raw_dict)
    raw_bytes = json.dumps(raw_dict).encode("utf-8")

    with pytest.raises(ForbiddenFieldError, match="attempt to set publication"):
        stager.stage(raw_bytes)


def test_reject_oversized_batch_payload(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    oversized = b" " * (10 * 1024 * 1024 + 10)
    with pytest.raises(OversizedInputError, match="exceeds limit"):
        stager.stage(oversized)


def test_reject_null_bytes(env: tuple[Engine, ArtifactStore, Path, Path]) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    with pytest.raises(ValueError, match="null bytes"):
        stager.stage(b"{\x00}")


# ---------------------------------------------------------------------------
# 5. Migration & Guarded Downgrade
# ---------------------------------------------------------------------------


def test_migration_and_guarded_downgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "test_migration.sqlite"
    upgrade_database(db_path)
    engine = open_operational_database(db_path)

    # 1. Insert a staged batch
    with engine.begin() as conn:
        conn.execute(
            insert(staged_rate_batches).values(
                batch_id="b-migration-guard",
                schema_version="1.0",
                batch_digest="d" * 64,
                operator="Op",
                model_name="M",
                tool_name="T",
                recipe_version="R",
                submitted_at=T0,
                imported_at=T0,
                raw_bytes_hash="h" * 64,
                item_count=0,
                staged_count=0,
                held_count=0,
                rejected_count=0,
            )
        )

    # 2. Guarded downgrade MUST refuse while records exist
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        downgrade_database(db_path, "t056a_source_review_requests")

    # 3. Clean up records and retry downgrade
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TRIGGER staged_rate_batches_no_delete")
    raw.execute("DELETE FROM staged_rate_batches")
    raw.commit()
    raw.close()

    # Now downgrade succeeds
    downgrade_database(db_path, "t056a_source_review_requests")

    # Verify table dropped
    with engine.connect() as conn:
        res = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='staged_rate_batches'")
        ).first()
        assert res is None

    engine.dispose()


def test_append_only_triggers_reject_mutations(
    env: tuple[Engine, ArtifactStore, Path, Path],
) -> None:
    engine, store, _, _ = env
    stager = RateBatchStager(engine, store, clock=lambda: T1)

    cand = _build_valid_30y_candidate(candidate_id="cand-trigger-001")
    payload = make_envelope_payload(batch_id="b-trig-001", candidates=[cand])
    stager.stage(payload)

    # Attempt UPDATE
    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE staged_rate_batches SET operator = 'Tampered' WHERE batch_id = 'b-trig-001'"
            )
        )

    # Attempt DELETE
    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(text("DELETE FROM staged_rate_batches WHERE batch_id = 'b-trig-001'"))


# ---------------------------------------------------------------------------
# 6. CLI Execution
# ---------------------------------------------------------------------------


def test_cli_execution_dry_run_import_and_json(
    env: tuple[Engine, ArtifactStore, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, db_path, store_path = env

    cand = _build_valid_30y_candidate(candidate_id="cand-cli-001")
    batch_file = tmp_path / "cli_batch.json"
    batch_file.write_bytes(make_envelope_payload(batch_id="batch-cli-001", candidates=[cand]))

    # 1. CLI Dry Run
    exit_code = cli_main(
        [
            str(batch_file),
            "--database",
            str(db_path),
            "--store",
            str(store_path),
            "--dry-run",
            "--now",
            T1.isoformat(),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Submitted:  1" in out
    assert "Staged:     1" in out

    # 2. CLI Live Import with --json
    exit_code = cli_main(
        [
            str(batch_file),
            "--database",
            str(db_path),
            "--store",
            str(store_path),
            "--now",
            T1.isoformat(),
            "--json",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    report_data = json.loads(out)
    assert report_data["batch_id"] == "batch-cli-001"
    assert report_data["staged_count"] == 1
    assert report_data["is_noop"] is False

    # 3. CLI Replay
    exit_code = cli_main(
        [
            str(batch_file),
            "--database",
            str(db_path),
            "--store",
            str(store_path),
            "--now",
            T1.isoformat(),
            "--json",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    replay_data = json.loads(out)
    assert replay_data["is_noop"] is True
    assert replay_data["already_imported_count"] == 1

"""Acceptance tests for offline model/operator rate batch intake (contract version 1.0).

Verifies:
1. Strict, bounded UTF-8 JSON and JSONL parsing, digest computation, and hash tampering rejection.
2. Rejection of duplicate keys/identities, floats, constants, and invalid timestamps.
3. Forbidden publication, approval, and confidence fields.
4. Path escape and cross-reference validation across charter, product, source, and capture.
5. Evidence verification via verify_batch_item_locators against the private content-addressed store:
   - Synthetic 30-year mortgage with note_rate, apr, and points.
   - HELOC with lower-CLTV tier.
   - Coverage findings (quote-only, blocked, unknown).
   - Unknown points (None) vs explicitly zero points (Decimal("0")).
   - Substring rejection: 16.375% never proves 6.375%.
   - Extra unrelated text rejection: selector must resolve one full field.
   - Cross-row mixing rejection: same string in unrelated row cannot prove a different tier.
   - Unsupported formats (PDF, Image OCR, Rendered DOM) fail closed to held.
   - Corrupt or missing artifacts fail closed to held.
   - JSON pointer resolution and scalar exact-match verification.
   - Independent review disagreement handling.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Ensure staging.py absence does not prevent imports during parallel integration
if "cu_rates.rate_batch.staging" not in sys.modules:
    try:
        import cu_rates.rate_batch.staging  # noqa: F401
    except ModuleNotFoundError:
        _staging_stub = types.ModuleType("cu_rates.rate_batch.staging")
        _staging_stub.BatchImportReport = object  # type: ignore[attr-defined]
        _staging_stub.RateBatchStager = object  # type: ignore[attr-defined]
        sys.modules["cu_rates.rate_batch.staging"] = _staging_stub

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import (
    ConditionKind,
    LienPosition,
    LoanPurpose,
    ProductFamily,
    ProductScope,
    RateAvailability,
    TierDimension,
)
from cu_rates.contracts.evidence import (
    BoundingBox,
    HtmlLocator,
    ImageOcrLocator,
    JsonLocator,
    PdfLocator,
    RenderedDomLocator,
)
from cu_rates.rate_batch.locators import (
    verify_batch_item_locators,
)
from cu_rates.rate_batch.models import (
    BATCH_SCHEMA_VERSION,
    BatchArmTerms,
    BatchCandidate,
    BatchCondition,
    BatchDisposition,
    BatchFinding,
    BatchHelocTerms,
    BatchIndependentReview,
    BatchTier,
    BatchTierBound,
    CrossReferenceError,
    ForbiddenFieldError,
    HashTamperingError,
    HoldReason,
    OversizedInputError,
)
from cu_rates.rate_batch.parser import (
    compute_batch_digest,
    parse_rate_batch,
)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
EVAL_TIME = T0 + timedelta(hours=1)
CHARTER = 12345
SOURCE_ID = "src-official-rates"
SOURCE_URL = "https://example.test/rates"
FETCH_RUN = "run-20261119-01"

HTML_FIXTURE = (
    b"""<!DOCTYPE html>
<html>
<head><title>Synthetic Rates</title></head>
<body>
<div id="mortgage-rates">
  <h1>Mortgage Rates</h1>
  <table id="tbl-fixed">
    <thead>
      <tr><th>Product</th><th>Term</th><th>Rate</th><th>APR</th><th>Points</th></tr>
    </thead>
    <tbody>
      <tr id="row-30y">
        <td class="prod">30-Year Fixed Conforming Purchase</td>
        <td class="term">360 Months</td>
        <td class="rate">6.375%</td>
        <td class="apr">6.450%</td>
        <td class="pts">0 Points</td>
      </tr>
      <tr id="row-15y">
        <td class="prod">15-Year Fixed Conforming</td>
        <td class="term">180 Months</td>
        <td class="rate">5.875%</td>
        <td class="apr">5.950%</td>
        <td class="pts">0 Points</td>
      </tr>
      <tr id="row-composite">
        <td class="prod">Special Jumbo 30Y</td>
        <td class="term">360 Months</td>
        <td class="rate">16.375%</td>
        <td class="apr">16.450%</td>
        <td class="pts">1.0 Points</td>
      </tr>
      <tr id="row-extra-text">
        <td class="prod">Adjustable Tier</td>
        <td class="term">360 Months</td>
        <td class="rate">Rate: 6.375% APR: 6.450%</td>
        <td class="apr">6.450%</td>
        <td class="pts">0</td>
      </tr>
    </tbody>
  </table>
</div>
<div id="heloc-rates">
  <table id="tbl-heloc">
    <tbody>
      <tr id="row-heloc-t1">
        <td class="prod">Home Equity Line of Credit &lt;= 70% CLTV</td>
        <td class="rate">7.250%</td>
        <td class="apr">7.250%</td>
      </tr>
      <tr id="row-heloc-t2">
        <td class="prod">Home Equity Line of Credit 70.01% - 80% CLTV</td>
        <td class="rate">7.750%</td>
        <td class="apr">7.750%</td>
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
</html>"""
)

HTML_ARTIFACT_ID = hashlib.sha256(HTML_FIXTURE).hexdigest()

JSON_FIXTURE = b"""{
  "credit_union": "Synthetic Credit Union",
  "effective_date": "2026-11-19",
  "products": {
    "mortgage_30y": {
      "note_rate": "6.375%",
      "apr": "6.450%",
      "points": "0.0"
    },
    "composite_cell": {
      "combined_rates": "16.375%"
    },
    "nested_container": {
      "rate": "6.375%",
      "apr": "6.450%"
    }
  }
}"""

JSON_ARTIFACT_ID = hashlib.sha256(JSON_FIXTURE).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    s = ArtifactStore(tmp_path / "store")
    s.put(
        HTML_FIXTURE,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id=FETCH_RUN,
    )
    s.put(
        JSON_FIXTURE,
        media_type="application/json",
        source_url="https://example.test/api/rates.json",
        fetched_at=T0,
        http_status=200,
        fetch_run_id=FETCH_RUN,
    )
    return s


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _css(selector: str, text: str) -> HtmlLocator:
    return HtmlLocator(selector_type="css", selector=selector, normalized_text_sha256=_sha(text))


# Cells of HTML_FIXTURE row-30y by claim path: (column index, exact cell text).
_ROW30Y_CELLS: dict[str, tuple[int, str]] = {
    "loan_purpose": (1, "30-Year Fixed Conforming Purchase"),
    "term_months": (2, "360 Months"),
    "note_rate": (3, "6.375%"),
    "apr": (4, "6.450%"),
    "points": (5, "0 Points"),
}


def _row30y_locators(*paths: str) -> dict[str, Any]:
    return {
        path: _css(f"#mortgage-rates tr:nth-child(1) td:nth-child({col})", text)
        for path in paths
        for col, text in (_ROW30Y_CELLS[path],)
    }


def _row30y_texts(*paths: str) -> dict[str, str]:
    return {path: _ROW30Y_CELLS[path][1] for path in paths}


def _row30y_candidate(candidate_id: str, paths: tuple[str, ...], **claims: Any) -> BatchCandidate:
    """A 30-year purchase mortgage on HTML_FIXTURE row-30y; ``paths`` choose located claims."""
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "row_id": "row-30y",
        "institution_charter": CHARTER,
        "product_id": "mortgage-30y-fixed",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": ProductFamily.MORTGAGE,
        "raw_text": "30-Year Fixed Conforming Purchase 360 Months 6.375% 6.450% 0 Points",
        "field_raw_texts": _row30y_texts(*paths),
        "locators": _row30y_locators(*paths),
        "observed_at": T0,
    }
    base.update(claims)
    return BatchCandidate(**base)


def build_sample_envelope_dict(
    *,
    candidates: list[dict[str, object]] | None = None,
    findings: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
    independent_review: dict[str, object] | None = None,
    batch_id: str = "batch-20261119-01",
    submitted_at: str = "2026-11-19T12:00:00Z",
) -> dict[str, object]:
    if observations is None:
        observations = [
            {
                "institution_charter": CHARTER,
                "source_id": SOURCE_ID,
                "source_url": SOURCE_URL,
                "product_id": "mortgage-30y-fixed",
                "fetch_run_id": FETCH_RUN,
                "observed_at": "2026-11-19T12:00:00Z",
                "artifact_id": HTML_ARTIFACT_ID,
                "media_type": "text/html; charset=utf-8",
            }
        ]
    if candidates is None:
        candidates = []
    if findings is None:
        findings = []

    payload: dict[str, object] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "operator": "operator-01",
        "model_name": "gemini-3.8-flash",
        "tool_name": "batch-intake-cli",
        "recipe_version": "v1.0.0",
        "submitted_at": submitted_at,
        "observations": observations,
        "candidates": candidates,
        "findings": findings,
    }
    if independent_review is not None:
        payload["independent_review"] = independent_review

    payload["batch_digest"] = compute_batch_digest(payload)
    return payload


# ---------------------------------------------------------------------------
# Acceptance Test Group 1: Safe Bounded Parser, Rejections, and Tampering
# ---------------------------------------------------------------------------


def test_parse_valid_json_envelope() -> None:
    data = build_sample_envelope_dict()
    text = json.dumps(data)
    env = parse_rate_batch(text, now=EVAL_TIME)
    assert env.batch_id == "batch-20261119-01"
    assert env.schema_version == "1.0"
    assert env.batch_digest == compute_batch_digest(data)


def test_parse_valid_jsonl_envelope() -> None:
    env_line = {
        "type": "envelope",
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": "batch-jsonl-01",
        "operator": "operator-01",
        "model_name": "gemini-3.8-flash",
        "tool_name": "batch-intake-cli",
        "recipe_version": "v1.0.0",
        "submitted_at": "2026-11-19T12:00:00Z",
    }
    obs_line = {
        "type": "observation",
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "fetch_run_id": FETCH_RUN,
        "observed_at": "2026-11-19T12:00:00Z",
        "artifact_id": HTML_ARTIFACT_ID,
        "media_type": "text/html; charset=utf-8",
    }
    cand_line = {
        "type": "candidate",
        "candidate_id": "cand-01",
        "row_id": "row-01",
        "institution_charter": CHARTER,
        "product_id": "prod-01",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.375",
        "apr": "6.450",
        "points": "0",
        "raw_text": "30-Year Fixed 6.375% 6.450% APR 0 Points",
        "field_raw_texts": {"note_rate": "6.375%", "apr": "6.450%"},
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#tbl-fixed tr:nth-child(1) td:nth-child(3)",
                "normalized_text_sha256": hashlib.sha256(b"6.375%").hexdigest(),
            },
            "apr": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#tbl-fixed tr:nth-child(1) td:nth-child(4)",
                "normalized_text_sha256": hashlib.sha256(b"6.450%").hexdigest(),
            },
        },
        "observed_at": "2026-11-19T12:00:00Z",
    }
    full_dict: dict[str, object] = dict(env_line)
    del full_dict["type"]
    obs_copy = dict(obs_line)
    del obs_copy["type"]
    cand_copy = dict(cand_line)
    del cand_copy["type"]
    full_dict["observations"] = [obs_copy]
    full_dict["candidates"] = [cand_copy]
    digest = compute_batch_digest(full_dict)
    env_line["batch_digest"] = digest

    jsonl_text = "\n".join([json.dumps(env_line), json.dumps(obs_line), json.dumps(cand_line)])
    env = parse_rate_batch(jsonl_text, now=EVAL_TIME)
    assert env.batch_id == "batch-jsonl-01"
    assert len(env.candidates) == 1
    assert env.candidates[0].note_rate == Decimal("6.375")


def test_reject_tampered_digest() -> None:
    data = build_sample_envelope_dict()
    data["batch_digest"] = "a" * 64  # Invalid digest
    with pytest.raises(HashTamperingError, match="does not match computed content digest"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_oversized_payload() -> None:
    data = build_sample_envelope_dict()
    text = json.dumps(data)
    with pytest.raises(OversizedInputError, match="exceeds limit"):
        parse_rate_batch(text, max_bytes=50, now=EVAL_TIME)


def test_reject_null_bytes() -> None:
    data = build_sample_envelope_dict()
    raw = json.dumps(data).encode("utf-8") + b"\x00"
    with pytest.raises(ValueError, match="prohibited null bytes"):
        parse_rate_batch(raw, now=EVAL_TIME)


def test_reject_float_in_json() -> None:
    raw_json = (
        '{"schema_version": "1.0", "batch_id": "batch-float", "batch_digest": "'
        + ("0" * 64)
        + '", "operator": "op", "model_name": "m", "tool_name": "t", "recipe_version": "r", '
        + '"submitted_at": "2026-11-19T12:00:00Z", "note_rate": 6.375}'
    )
    with pytest.raises(ValueError, match="binary floats are rejected"):
        parse_rate_batch(raw_json, now=EVAL_TIME)


def test_reject_constants_nan_and_infinity() -> None:
    raw_nan = '{"schema_version": "1.0", "rate": NaN}'
    with pytest.raises(ValueError, match="prohibited JSON constant"):
        parse_rate_batch(raw_nan, now=EVAL_TIME)

    raw_inf = '{"schema_version": "1.0", "rate": Infinity}'
    with pytest.raises(ValueError, match="prohibited JSON constant"):
        parse_rate_batch(raw_inf, now=EVAL_TIME)


def test_reject_duplicate_keys_in_json() -> None:
    raw_dup = '{"schema_version": "1.0", "batch_id": "b1", "batch_id": "b2"}'
    with pytest.raises(ValueError, match="duplicate key in JSON object"):
        parse_rate_batch(raw_dup, now=EVAL_TIME)


def test_reject_naive_timestamp() -> None:
    data = build_sample_envelope_dict(submitted_at="2026-11-19T12:00:00")  # Naive without tz
    with pytest.raises(ValueError):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_future_timestamp() -> None:
    future_time = EVAL_TIME + timedelta(days=2)
    data = build_sample_envelope_dict(submitted_at=future_time.isoformat())
    with pytest.raises(ValueError, match="is in the future"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_path_escape_in_identifier() -> None:
    # Leading traversal rejected by Pydantic / parser (permissive assertion)
    data_escape = build_sample_envelope_dict(batch_id="../escape")
    with pytest.raises(ValueError):
        parse_rate_batch(json.dumps(data_escape), now=EVAL_TIME)

    # Internal traversal rejected with explicit path traversal message
    data_internal = build_sample_envelope_dict(batch_id="a..b")
    with pytest.raises(ValueError, match="path traversal or null byte"):
        parse_rate_batch(json.dumps(data_internal), now=EVAL_TIME)


def test_reject_forbidden_publication_fields() -> None:
    data = build_sample_envelope_dict()
    data["status"] = "published"  # Forbidden key
    with pytest.raises(ForbiddenFieldError, match="attempt to set publication/approval/confidence"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_duplicate_candidate_ids() -> None:
    cand_1 = {
        "candidate_id": "cand-dup",
        "row_id": "row-01",
        "institution_charter": CHARTER,
        "product_id": "mortgage-30y-fixed",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.375",
        "raw_text": "6.375%",
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#tbl-fixed td.rate",
                "normalized_text_sha256": "0" * 64,
            }
        },
        "observed_at": "2026-11-19T12:00:00Z",
    }
    cand_2 = dict(cand_1)
    cand_2["row_id"] = "row-02"

    data = build_sample_envelope_dict(candidates=[cand_1, cand_2])
    with pytest.raises(ValueError, match="duplicate candidate_id 'cand-dup'"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_cross_reference_mismatch() -> None:
    cand = {
        "candidate_id": "cand-mismatch",
        "row_id": "row-01",
        "institution_charter": 99999,  # Mismatch with observation charter 12345
        "product_id": "mortgage-30y-fixed",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.375",
        "raw_text": "6.375%",
        "locators": {
            "note_rate": {
                "kind": "html",
                "selector_type": "css",
                "selector": "#tbl-fixed td.rate",
                "normalized_text_sha256": "0" * 64,
            }
        },
        "observed_at": "2026-11-19T12:00:00Z",
    }
    data = build_sample_envelope_dict(candidates=[cand])
    with pytest.raises(CrossReferenceError, match="does not match observation charter"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_reject_ungrounded_rate_claim() -> None:
    cand = {
        "candidate_id": "cand-ungrounded",
        "row_id": "row-01",
        "institution_charter": CHARTER,
        "product_id": "mortgage-30y-fixed",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": "mortgage",
        "loan_purpose": "purchase",
        "term_months": 360,
        "note_rate": "6.375",
        "raw_text": "6.375%",
        "locators": {},  # Missing required note_rate locator
        "observed_at": "2026-11-19T12:00:00Z",
    }
    data = build_sample_envelope_dict(candidates=[cand])
    with pytest.raises(ValueError, match="ungrounded note_rate claim without evidence locator"):
        parse_rate_batch(json.dumps(data), now=EVAL_TIME)


def test_candidate_inherits_neither_freshness_nor_unstated_conditions() -> None:
    candidate: dict[str, object] = {
        "candidate_id": "candidate-ts",
        "row_id": "row-ts",
        "institution_charter": CHARTER,
        "product_id": "mortgage-30y-fixed",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": "mortgage",
        "raw_text": "Fictional mortgage details",
        "observed_at": T0.isoformat(),
    }
    batch = build_sample_envelope_dict(candidates=[candidate])
    parsed = parse_rate_batch(json.dumps(batch), now=EVAL_TIME).candidates[0]
    assert parsed.headline_is_as_low_as is None
    assert parsed.conforming_status.value == "unknown"

    candidate["observed_at"] = (T0 + timedelta(minutes=15)).isoformat()
    batch = build_sample_envelope_dict(candidates=[candidate])
    with pytest.raises(CrossReferenceError, match="timestamp does not match"):
        parse_rate_batch(json.dumps(batch), now=EVAL_TIME)


# ---------------------------------------------------------------------------
# Acceptance Test Group 2: Evidence Locator Verification & False-Positive Traps
# ---------------------------------------------------------------------------


def test_verify_30y_mortgage_staged(store: ArtifactStore) -> None:
    """Every present claim (rates, points, term, purpose) is individually located and stated."""
    candidate = _row30y_candidate(
        "cand-30y-fixed",
        ("loan_purpose", "term_months", "note_rate", "apr", "points"),
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.450"),
        points=Decimal("0"),
    )

    disposition, reason, msg = verify_batch_item_locators(candidate, store)
    assert disposition is BatchDisposition.STAGED
    assert reason is None
    assert msg is None


def test_false_positive_substring_never_proves_value(store: ArtifactStore) -> None:
    """Critical safety test: 16.375% in evidence must NEVER prove a claimed 6.375% note rate."""
    comp_sha = hashlib.sha256(b"16.375%").hexdigest()

    # Model claims note_rate 6.375, pointing to row-composite which contains 16.375%
    candidate = BatchCandidate(
        candidate_id="cand-substring-trap",
        row_id="row-composite",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),  # 6.375 claimed
        raw_text="Special Jumbo 30Y 16.375%",
        field_raw_texts={"note_rate": "6.375%"},  # Model extracts 6.375%
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(3) td:nth-child(3)",  # Resolves to 16.375%
                normalized_text_sha256=comp_sha,
            )
        },
        observed_at=T0,
    )

    disposition, reason, msg = verify_batch_item_locators(candidate, store)
    assert disposition is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "substring" in str(msg) or "does not equal" in str(msg)


def test_reject_extra_unrelated_text_in_selector(store: ArtifactStore) -> None:
    """Selector resolving to extra unrelated text (multiple rates) must be held."""
    node_text = "Rate: 6.375% APR: 6.450%"
    node_sha = hashlib.sha256(node_text.encode("utf-8")).hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-extra-text",
        row_id="row-extra-text",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="Adjustable Tier 6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(4) td:nth-child(3)",
                normalized_text_sha256=node_sha,
            )
        },
        observed_at=T0,
    )

    disposition, reason, msg = verify_batch_item_locators(candidate, store)
    assert disposition is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_AMBIGUOUS
    assert "multiple rate percentages" in str(msg) or "extra unrelated text" in str(msg)


def test_reject_cross_row_mixing(store: ArtifactStore) -> None:
    """Field locators for the same candidate pointing to different table rows must be held."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    apr_sha = hashlib.sha256(b"5.950%").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-cross-row",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        apr=Decimal("5.950"),
        raw_text="6.375% and 5.950%",
        field_raw_texts={"note_rate": "6.375%", "apr": "5.950%"},
        locators={
            # note_rate points to row 1
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            # apr points to row 2!
            "apr": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(2) td:nth-child(4)",
                normalized_text_sha256=apr_sha,
            ),
        },
        observed_at=T0,
    )

    disposition, reason, msg = verify_batch_item_locators(candidate, store)
    assert disposition is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_AMBIGUOUS
    assert "multiple conflicting table rows" in str(msg)


HELOC_T1_PRODUCT = "Home Equity Line of Credit <= 70% CLTV"
HELOC_T1_SELECTOR = "#heloc-rates tr:nth-child(1) td:nth-child({col})"


def _heloc_t1_tier(
    *,
    note_rate: Decimal | None = Decimal("7.250"),
    apr: Decimal | None = None,
    upper: Decimal = Decimal("70"),
    dimension: TierDimension = TierDimension.CLTV,
    lower: Decimal | None = Decimal("0"),
    col: int = 2,
    text: str = "7.250%",
) -> BatchTier:
    """A tier whose rate cell is in row-heloc-t1; its CLTV bound lives in the product cell."""
    return BatchTier(
        tier_id="tier-cltv-70",
        bounds=(BatchTierBound(dimension=dimension, lower=lower, upper=upper),),
        note_rate=note_rate,
        apr=apr,
        raw_text=text,
        locator=_css(HELOC_T1_SELECTOR.format(col=col), text),
    )


def _heloc_candidate(candidate_id: str, **overrides: Any) -> BatchCandidate:
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "row_id": "row-heloc-t1",
        "institution_charter": CHARTER,
        "product_id": "heloc-prime",
        "source_id": SOURCE_ID,
        "fetch_run_id": FETCH_RUN,
        "artifact_id": HTML_ARTIFACT_ID,
        "product_family": ProductFamily.HELOC,
        "tiers": (_heloc_t1_tier(),),
        "raw_text": "Home Equity Line of Credit <= 70% CLTV 7.250%",
        "observed_at": T0,
    }
    base.update(overrides)
    return BatchCandidate(**base)


def test_heloc_with_lower_cltv_tier(store: ArtifactStore) -> None:
    """The tier bound is proven by the row's explicit labeled CLTV criterion, not the rate cell."""
    candidate = _heloc_candidate("cand-heloc")
    disposition, reason, _msg = verify_batch_item_locators(candidate, store)
    assert disposition is BatchDisposition.STAGED
    assert reason is None

    # The second tier (70.01% - 80% CLTV) stages from its own row.
    tier_2 = BatchTier(
        tier_id="tier-cltv-80",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV, lower=Decimal("70.01"), upper=Decimal("80")
            ),
        ),
        note_rate=Decimal("7.750"),
        raw_text="7.750%",
        locator=_css("#heloc-rates tr:nth-child(2) td:nth-child(2)", "7.750%"),
    )
    candidate_2 = _heloc_candidate(
        "cand-heloc-t2",
        row_id="row-heloc-t2",
        tiers=(tier_2,),
        raw_text="Home Equity Line of Credit 70.01% - 80% CLTV 7.750%",
    )
    result_2 = verify_batch_item_locators(candidate_2, store)
    assert result_2 == (BatchDisposition.STAGED, None, None)


def test_tier_bound_must_match_the_rows_labeled_criterion(store: ArtifactStore) -> None:
    """A tier bound the row does not state (here 80; the row says <= 70% CLTV) is held."""
    wrong_upper = _heloc_candidate(
        "cand-tier-wrong-bound", tiers=(_heloc_t1_tier(upper=Decimal("80")),)
    )
    disp, reason, msg = verify_batch_item_locators(wrong_upper, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "does not match the row's disclosed criterion" in str(msg)


def test_tier_bound_without_labeled_criterion_is_not_proven_by_rate_fragment(
    store: ArtifactStore,
) -> None:
    """The rate fragment (7.250%) never proves a typed bound; a missing FICO criterion holds."""
    fico_bound = _heloc_candidate(
        "cand-tier-fico",
        tiers=(
            _heloc_t1_tier(
                dimension=TierDimension.CREDIT_SCORE,
                lower=Decimal("700"),
                upper=Decimal("850"),
            ),
        ),
    )
    disp, reason, msg = verify_batch_item_locators(fico_bound, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING
    assert "no explicit labeled criterion" in str(msg)

    unsupported = _heloc_candidate(
        "cand-tier-amount",
        tiers=(
            _heloc_t1_tier(
                dimension=TierDimension.LOAN_AMOUNT,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
    )
    disp2, reason2, _msg2 = verify_batch_item_locators(unsupported, store)
    assert disp2 is BatchDisposition.HELD
    assert reason2 is HoldReason.LOCATOR_UNSUPPORTED


def test_tier_cannot_borrow_sibling_cells_for_a_second_rate_claim(store: ArtifactStore) -> None:
    """One tier locator is one cell; claiming both rate and APR from it is held."""
    both = _heloc_candidate(
        "cand-tier-both",
        tiers=(_heloc_t1_tier(note_rate=Decimal("7.250"), apr=Decimal("7.250")),),
    )
    disp, reason, msg = verify_batch_item_locators(both, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_AMBIGUOUS
    assert "own verified cell" in str(msg)

    # An APR claim located on the note-rate column is a column rebinding.
    rebound = _heloc_candidate(
        "cand-tier-apr-on-rate",
        tiers=(_heloc_t1_tier(note_rate=None, apr=Decimal("7.250"), col=2),),
    )
    disp2, reason2, msg2 = verify_batch_item_locators(rebound, store)
    assert disp2 is BatchDisposition.HELD
    assert reason2 is HoldReason.RAW_TEXT_MISMATCH
    assert "non-APR" in str(msg2)


def test_heloc_terms_need_their_own_labeled_locator(store: ArtifactStore) -> None:
    """HELOC attributes (max CLTV, lien, index) are material claims: no locator, no staging."""
    unlocated = _heloc_candidate(
        "cand-heloc-unlocated",
        heloc_terms=BatchHelocTerms(max_cltv=Decimal("70"), lien_position=LienPosition.SECOND),
    )
    disp, reason, msg = verify_batch_item_locators(unlocated, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING
    assert "heloc_terms.max_cltv" in str(msg)

    product_cell = _css(HELOC_T1_SELECTOR.format(col=1), HELOC_T1_PRODUCT)
    located = _heloc_candidate(
        "cand-heloc-located",
        heloc_terms=BatchHelocTerms(max_cltv=Decimal("70")),
        locators={"heloc_terms.max_cltv": product_cell},
        field_raw_texts={"heloc_terms.max_cltv": HELOC_T1_PRODUCT},
    )
    assert verify_batch_item_locators(located, store) == (BatchDisposition.STAGED, None, None)

    # The same cell cannot prove a different value.
    wrong_value = located.model_copy(
        update={"heloc_terms": BatchHelocTerms(max_cltv=Decimal("80"))}
    )
    disp2, reason2, msg2 = verify_batch_item_locators(wrong_value, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not equal claimed" in str(msg2)

    # The rate cell has no CLTV label at all, so it is not sibling evidence for max CLTV.
    rate_cell = _css(HELOC_T1_SELECTOR.format(col=2), "7.250%")
    on_rate_cell = located.model_copy(
        update={
            "locators": {"heloc_terms.max_cltv": rate_cell},
            "field_raw_texts": {"heloc_terms.max_cltv": "7.250%"},
        }
    )
    disp3, reason3, msg3 = verify_batch_item_locators(on_rate_cell, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "lacks an explicit label" in str(msg3)

    # A lien claim whose cell never states the lien position is held.
    lien = located.model_copy(
        update={
            "heloc_terms": BatchHelocTerms(lien_position=LienPosition.SECOND),
            "locators": {"heloc_terms.lien_position": product_cell},
            "field_raw_texts": {"heloc_terms.lien_position": HELOC_T1_PRODUCT},
        }
    )
    disp4, reason4, _msg4 = verify_batch_item_locators(lien, store)
    assert (disp4, reason4) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)


def test_condition_needs_its_own_located_text_in_the_candidate_row(store: ArtifactStore) -> None:
    """Conditions are material: no locator holds; a located condition must state its kind."""
    description = "CLTV at or below 70%"
    unlocated = _heloc_candidate(
        "cand-condition-unlocated",
        conditions=(
            BatchCondition(
                kind=ConditionKind.TIER, description=description, raw_text=HELOC_T1_PRODUCT
            ),
        ),
    )
    disp, reason, msg = verify_batch_item_locators(unlocated, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "lacks evidence locator" in str(msg)

    product_locator = _css(HELOC_T1_SELECTOR.format(col=1), HELOC_T1_PRODUCT)
    located = _heloc_candidate(
        "cand-condition-located",
        conditions=(
            BatchCondition(
                kind=ConditionKind.TIER,
                description=description,
                raw_text=HELOC_T1_PRODUCT,
                locator=product_locator,
            ),
        ),
    )
    assert verify_batch_item_locators(located, store) == (BatchDisposition.STAGED, None, None)

    # The same located text does not state an autopay discount, so the kind is not credited.
    wrong_kind = _heloc_candidate(
        "cand-condition-wrong-kind",
        conditions=(
            BatchCondition(
                kind=ConditionKind.AUTOPAY,
                description="Autopay discount",
                raw_text=HELOC_T1_PRODUCT,
                locator=product_locator,
            ),
        ),
    )
    disp2, reason2, msg2 = verify_batch_item_locators(wrong_kind, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not state a autopay condition" in str(msg2)

    # A condition located in a different row cannot be attributed to this offer.
    other_text = "Home Equity Line of Credit 70.01% - 80% CLTV"
    other_row = _heloc_candidate(
        "cand-condition-other-row",
        conditions=(
            BatchCondition(
                kind=ConditionKind.TIER,
                description=description,
                raw_text=other_text,
                locator=_css("#heloc-rates tr:nth-child(2) td:nth-child(1)", other_text),
            ),
        ),
    )
    disp3, reason3, _msg3 = verify_batch_item_locators(other_row, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.EVIDENCE_AMBIGUOUS)


def test_coverage_findings_verification(store: ArtifactStore) -> None:
    quote_text = (
        "Mortgage rates available by phone or online application only. Call 800-555-0199 for a "
        "quote."
    )
    quote_sha = hashlib.sha256(quote_text.encode("utf-8")).hexdigest()

    # Valid quote-only finding with verified locator
    finding_quote = BatchFinding(
        finding_id="find-quote",
        institution_charter=CHARTER,
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        observed_at=T0,
        label="Quote only mortgage",
        scope=ProductScope.IN_SCOPE,
        family=ProductFamily.MORTGAGE,
        rate_availability=RateAvailability.QUOTE_ONLY,
        raw_text=quote_text,
        locators={
            "availability": HtmlLocator(
                selector_type="css",
                selector="#coverage-findings p:nth-child(1)",
                normalized_text_sha256=quote_sha,
            )
        },
    )
    disp, reason, _ = verify_batch_item_locators(finding_quote, store)
    assert disp is BatchDisposition.STAGED

    # In-scope finding missing locator must be held
    finding_missing_loc = BatchFinding(
        finding_id="find-missing-loc",
        institution_charter=CHARTER,
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        observed_at=T0,
        label="Blocked rates",
        scope=ProductScope.IN_SCOPE,
        family=ProductFamily.MORTGAGE,
        rate_availability=RateAvailability.BLOCKED,
        raw_text="Rate viewing is restricted to authenticated members.",
        locators={},
    )
    disp, reason, _ = verify_batch_item_locators(finding_missing_loc, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING

    # Unknown finding without locators is valid and staged
    finding_unknown = BatchFinding(
        finding_id="find-unknown",
        institution_charter=CHARTER,
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        observed_at=T0,
        label="Unknown page coverage",
        scope=ProductScope.UNKNOWN,
        raw_text="Unknown rates status",
        locators={},
    )
    disp, reason, _ = verify_batch_item_locators(finding_unknown, store)
    assert disp is BatchDisposition.STAGED


def test_separate_unknown_points_vs_explicit_zero(store: ArtifactStore) -> None:
    # Explicit zero points
    cand_zero_pts = _row30y_candidate(
        "cand-zero-points",
        ("loan_purpose", "term_months", "note_rate", "points"),
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        points=Decimal("0"),  # Explicit zero
    )
    disp, _, _ = verify_batch_item_locators(cand_zero_pts, store)
    assert disp is BatchDisposition.STAGED

    # Unknown points (None)
    cand_unknown_pts = _row30y_candidate(
        "cand-unknown-points",
        ("loan_purpose", "term_months", "note_rate"),
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        points=None,  # Unknown points preserved as None
    )
    assert cand_unknown_pts.points is None
    disp, _, _ = verify_batch_item_locators(cand_unknown_pts, store)
    assert disp is BatchDisposition.STAGED

    # Mismatched points: claims zero, but evidence states 1.0 Points
    pts_1_sha = hashlib.sha256(b"1.0 Points").hexdigest()
    cand_mismatch_pts = BatchCandidate(
        candidate_id="cand-mismatch-points",
        row_id="row-composite",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("16.375"),
        points=Decimal("0"),  # Claims 0, but text has 1.0 Points
        raw_text="Special Jumbo 16.375% 1.0 Points",
        field_raw_texts={"note_rate": "16.375%", "points": "1.0 Points"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(3) td:nth-child(3)",
                normalized_text_sha256=hashlib.sha256(b"16.375%").hexdigest(),
            ),
            "points": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(3) td:nth-child(5)",
                normalized_text_sha256=pts_1_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(cand_mismatch_pts, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    msg_str = str(msg)
    assert "1.0" in msg_str and "0" in msg_str
    assert "points" in msg_str


def test_unsupported_evidence_formats_held(store: ArtifactStore) -> None:
    dummy_box = BoundingBox(x0=Decimal("10"), y0=Decimal("10"), x1=Decimal("50"), y1=Decimal("50"))

    for loc in (
        PdfLocator(page=1, bbox=dummy_box, extracted_text_sha256="0" * 64),
        ImageOcrLocator(region=dummy_box, image_sha256="0" * 64, recognized_text_sha256="0" * 64),
        RenderedDomLocator(
            selector_type="css",
            selector="#rates",
            rendered_text_sha256="0" * 64,
        ),
    ):
        candidate = BatchCandidate(
            candidate_id=f"cand-unsupported-{loc.kind}",
            row_id="row-01",
            institution_charter=CHARTER,
            product_id="mortgage-30y-fixed",
            source_id=SOURCE_ID,
            fetch_run_id=FETCH_RUN,
            artifact_id=HTML_ARTIFACT_ID,
            product_family=ProductFamily.MORTGAGE,
            loan_purpose=LoanPurpose.PURCHASE,
            term_months=360,
            note_rate=Decimal("6.375"),
            raw_text="6.375%",
            field_raw_texts={"note_rate": "6.375%"},
            locators={"note_rate": loc},
            observed_at=T0,
        )
        disp, reason, msg = verify_batch_item_locators(candidate, store)
        assert disp is BatchDisposition.HELD
        assert reason is HoldReason.LOCATOR_UNSUPPORTED
        assert "unsupported for automated candidate verification" in str(msg)


def test_missing_and_corrupt_artifact_held(store: ArtifactStore, tmp_path: Path) -> None:
    non_existent_hash = "f" * 64
    candidate = BatchCandidate(
        candidate_id="cand-missing-art",
        row_id="row-01",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=non_existent_hash,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#tbl-fixed td.rate",
                normalized_text_sha256="0" * 64,
            )
        },
        observed_at=T0,
    )
    disp, reason, _msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.ARTIFACT_NOT_FOUND

    # Corrupt artifact on disk (tampered bytes under store blob)
    blob_file = store._blob_path(HTML_ARTIFACT_ID)
    blob_file.chmod(0o600)
    blob_file.write_bytes(b"corrupted contents")
    blob_file.chmod(0o400)

    disp2, reason2, msg2 = verify_batch_item_locators(
        candidate.model_copy(update={"artifact_id": HTML_ARTIFACT_ID}), store
    )
    assert disp2 is BatchDisposition.HELD
    assert reason2 is HoldReason.ARTIFACT_HASH_MISMATCH
    assert "corrupt" in str(msg2)


def test_json_locator_resolution_and_exact_match(store: ArtifactStore) -> None:
    # 1. Valid scalar resolution
    cand_valid_json = BatchCandidate(
        candidate_id="cand-json-valid",
        row_id="row-json-01",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=JSON_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": JsonLocator(
                pointer="/products/mortgage_30y/note_rate",
                response_sha256=JSON_ARTIFACT_ID,
            )
        },
        observed_at=T0,
    )
    # A JSON scalar proves the value text but cannot bind a product row or term, so a candidate
    # located only by JSON pointers is held as unsupported rather than staged.
    disp, reason, msg = verify_batch_item_locators(cand_valid_json, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.LOCATOR_UNSUPPORTED
    assert "cannot be bound to a product row and term" in str(msg)

    # 2. Container pointer resolution (resolves to dict instead of scalar)
    cand_container_json = cand_valid_json.model_copy(
        update={
            "locators": {
                "note_rate": JsonLocator(
                    pointer="/products/nested_container",
                    response_sha256=JSON_ARTIFACT_ID,
                )
            }
        }
    )
    disp2, reason2, msg2 = verify_batch_item_locators(cand_container_json, store)
    assert disp2 is BatchDisposition.HELD
    assert reason2 is HoldReason.EVIDENCE_AMBIGUOUS
    assert "resolves to container" in str(msg2)

    # 3. JSON value mismatch (no substring match allowed)
    cand_mismatch_json = cand_valid_json.model_copy(
        update={
            "locators": {
                "note_rate": JsonLocator(
                    pointer="/products/composite_cell/combined_rates",  # Contains "16.375%"
                    response_sha256=JSON_ARTIFACT_ID,
                )
            }
        }
    )
    disp3, reason3, msg3 = verify_batch_item_locators(cand_mismatch_json, store)
    assert disp3 is BatchDisposition.HELD
    assert reason3 is HoldReason.RAW_TEXT_MISMATCH
    assert "does not match fragment" in str(msg3)


def test_independent_review_disagreement_held(store: ArtifactStore) -> None:
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()

    review_with_disagreement = BatchIndependentReview(
        reviewer_model="claude-3.5-sonnet",
        recipe_version="audit-v1.0",
        reviewed_at=T0,
        concurrence=False,
        disagreements=("note_rate discloses 6.500% in footnote",),
    )

    candidate = BatchCandidate(
        candidate_id="cand-review-dispute",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="30-Year Fixed 6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            )
        },
        independent_review=review_with_disagreement,
        observed_at=T0,
    )

    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.INDEPENDENT_REVIEW_DISAGREEMENT
    assert "unresolved disagreement" in str(msg)


# ---------------------------------------------------------------------------
# Targeted Negative Tests for Sonnet Review Blocking Bugs
# ---------------------------------------------------------------------------


def test_tier_json_locator_held_unsupported(store: ArtifactStore) -> None:
    """Tiers using JsonLocator must fail closed to HELD with LOCATOR_UNSUPPORTED."""
    tier = BatchTier(
        tier_id="tier-json-unsupported",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
        note_rate=Decimal("6.375"),
        raw_text="6.375%",
        locator=JsonLocator(
            pointer="/products/mortgage_30y/note_rate",
            response_sha256=JSON_ARTIFACT_ID,
        ),
    )
    candidate = BatchCandidate(
        candidate_id="cand-tier-json",
        row_id="row-json-01",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=JSON_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        tiers=(tier,),
        raw_text="30-Year Fixed 6.375%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.LOCATOR_UNSUPPORTED
    assert "unsupported" in str(msg)


def test_tier_canonical_hash_mismatch_held(store: ArtifactStore) -> None:
    """Tier HTML locator normalized_text_sha256 mismatch must fail closed to HELD."""
    bad_hash = "0" * 64
    tier = BatchTier(
        tier_id="tier-bad-hash",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
        note_rate=Decimal("7.250"),
        raw_text="7.250%",
        locator=HtmlLocator(
            selector_type="css",
            selector="#heloc-rates tr:nth-child(1) td:nth-child(2)",
            normalized_text_sha256=bad_hash,
        ),
    )
    candidate = BatchCandidate(
        candidate_id="cand-tier-bad-hash",
        row_id="row-heloc-t1",
        institution_charter=CHARTER,
        product_id="heloc-prime",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.HELOC,
        tiers=(tier,),
        raw_text="Home Equity Line of Credit <= 70% CLTV 7.250%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.LOCATOR_UNVERIFIED
    assert "SHA-256 mismatch" in str(msg)


def test_tier_apr_mismatch_held(store: ArtifactStore) -> None:
    """Tier claiming mismatched APR must fail closed to HELD with RAW_TEXT_MISMATCH."""
    t1_apr_sha = hashlib.sha256(b"7.250%").hexdigest()
    tier = BatchTier(
        tier_id="tier-apr-mismatch",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
        apr=Decimal("8.500"),  # Claims 8.500%, but the APR cell discloses 7.250%
        raw_text="7.250%",
        locator=HtmlLocator(
            selector_type="css",
            selector="#heloc-rates tr:nth-child(1) td:nth-child(3)",
            normalized_text_sha256=t1_apr_sha,
        ),
    )
    candidate = BatchCandidate(
        candidate_id="cand-tier-apr-mismatch",
        row_id="row-heloc-t1",
        institution_charter=CHARTER,
        product_id="heloc-prime",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.HELOC,
        tiers=(tier,),
        raw_text="Home Equity Line of Credit <= 70% CLTV 7.250%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "APR 7.250% does not match claimed 8.500" in str(msg)


def test_tier_missing_locator_held(store: ArtifactStore) -> None:
    """Tier lacking an evidence locator must fail closed to HELD with EVIDENCE_MISSING."""
    tier = BatchTier(
        tier_id="tier-missing-locator",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
        note_rate=Decimal("7.250"),
        raw_text="7.250%",
        locator=None,  # Missing locator
    )
    candidate = BatchCandidate(
        candidate_id="cand-tier-no-loc",
        row_id="row-heloc-t1",
        institution_charter=CHARTER,
        product_id="heloc-prime",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.HELOC,
        tiers=(tier,),
        raw_text="Home Equity Line of Credit <= 70% CLTV 7.250%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING
    assert "lacks evidence locator" in str(msg)


def test_field_locator_mismatched_row_id_held(store: ArtifactStore) -> None:
    """Field locator resolving to a different table row than candidate row_id must be held."""
    rate_15y_sha = hashlib.sha256(b"5.875%").hexdigest()
    candidate = BatchCandidate(
        candidate_id="cand-wrong-row",
        row_id="row-30y",  # Claims row-30y
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        note_rate=Decimal("5.875"),
        raw_text="30-Year Fixed Conforming 5.875%",
        field_raw_texts={"note_rate": "5.875%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(2) td:nth-child(3)",  # Resolves to row-15y
                normalized_text_sha256=rate_15y_sha,
            )
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "does not match candidate row_id" in str(msg)


def test_tier_locator_mismatched_row_id_held(store: ArtifactStore) -> None:
    """Tier locator resolving to a different table row than candidate row_id must be held."""
    t2_rate_sha = hashlib.sha256(b"7.750%").hexdigest()
    tier = BatchTier(
        tier_id="tier-wrong-row",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("70.01"),
                upper=Decimal("80"),
            ),
        ),
        note_rate=Decimal("7.750"),
        raw_text="7.750%",
        locator=HtmlLocator(
            selector_type="css",
            selector="#heloc-rates tr:nth-child(2) td:nth-child(2)",  # Resolves to row-heloc-t2
            normalized_text_sha256=t2_rate_sha,
        ),
    )
    candidate = BatchCandidate(
        candidate_id="cand-heloc-wrong-tier-row",
        row_id="row-heloc-t1",  # Claims row-heloc-t1
        institution_charter=CHARTER,
        product_id="heloc-prime",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.HELOC,
        tiers=(tier,),
        raw_text="Home Equity Line of Credit <= 70% CLTV 7.750%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "does not match candidate row_id" in str(msg)


def test_term_code_rejects_missing_unit_conversion(store: ArtifactStore) -> None:
    """Term code must reject values missing explicit months/years unit in evidence."""
    unitless_html = b"""<!DOCTYPE html>
<html>
<head><title>Unitless Term Rates</title></head>
<body>
<div id="unitless-rates">
  <table>
    <thead>
      <tr><th>Product</th><th>Term</th><th>Rate</th></tr>
    </thead>
    <tbody>
      <tr id="row-unitless-30">
        <td class="prod">30-Year Fixed Conforming</td>
        <td class="term">30</td>
        <td class="rate">6.375%</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>"""
    unitless_capture = store.put(
        unitless_html,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-unitless-term",
    )

    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    term_30_sha = hashlib.sha256(b"30").hexdigest()

    # Unitless source cell '30' fails closed to HELD
    candidate_unitless = BatchCandidate(
        candidate_id="cand-term-unitless-30",
        row_id="row-unitless-30",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=unitless_capture.fetch_run_id,
        artifact_id=unitless_capture.artifact_id,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="30-Year Fixed Conforming 30 6.375%",
        field_raw_texts={"note_rate": "6.375%", "term_months": "30"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#unitless-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            "term_months": HtmlLocator(
                selector_type="css",
                selector="#unitless-rates tr:nth-child(1) td:nth-child(2)",
                normalized_text_sha256=term_30_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate_unitless, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "does not disclose an explicit months/years unit" in str(msg)

    # Unit-bearing valid term ("360 Months") in HTML_FIXTURE stages successfully
    candidate_valid_term = _row30y_candidate(
        "cand-term-unit-bearing",
        ("loan_purpose", "term_months", "note_rate", "apr", "points"),
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.450"),
        points=Decimal("0"),
    )
    disp_valid, reason_valid, msg_valid = verify_batch_item_locators(candidate_valid_term, store)
    assert disp_valid is BatchDisposition.STAGED
    assert reason_valid is None
    assert msg_valid is None


def test_term_locator_cannot_point_to_points_column(store: ArtifactStore) -> None:
    """A term claim cannot use a cell in the points column as evidence."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    pts_sha = hashlib.sha256(b"0 Points").hexdigest()
    candidate = BatchCandidate(
        candidate_id="cand-term-non-matching",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="30-Year Fixed Conforming 6.375% 0 Points",
        field_raw_texts={"note_rate": "6.375%", "term_months": "0 Points"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            "term_months": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(5)",
                normalized_text_sha256=pts_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "non-term column" in str(msg)


def test_whole_container_selector_held(store: ArtifactStore) -> None:
    """Selector resolving to a container element (<div id='mortgage-rates'>) must be held."""
    candidate = BatchCandidate(
        candidate_id="cand-container-div",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        note_rate=Decimal("6.375"),
        raw_text="30-Year Fixed Conforming 6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates",  # Entire div container!
                normalized_text_sha256="0" * 64,
            )
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_AMBIGUOUS
    assert "resolves to container" in str(msg)


def test_claimed_points_missing_locator_held(store: ArtifactStore) -> None:
    """Claiming explicit points without a points locator must fail closed to HELD."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    candidate = BatchCandidate(
        candidate_id="cand-points-no-loc",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        note_rate=Decimal("6.375"),
        points=Decimal("0"),  # Explicit points claimed
        raw_text="30-Year Fixed Conforming 6.375% 0 Points",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            # Missing "points" locator!
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING
    assert "claimed points without points locator" in str(msg)


def test_claimed_apr_missing_locator_held(store: ArtifactStore) -> None:
    """Claiming APR without an apr locator must fail closed to HELD."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    candidate = BatchCandidate(
        candidate_id="cand-apr-no-loc",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.450"),  # Explicit APR claimed
        raw_text="30-Year Fixed Conforming 6.375% 6.450%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            # Missing "apr" locator!
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.EVIDENCE_MISSING
    assert "claimed apr without apr locator" in str(msg)


# ---------------------------------------------------------------------------
# Semantic Binding and Column Integrity Regression Tests
# ---------------------------------------------------------------------------


def test_reject_wrong_column_apr_rebound_to_rate_cell(store: ArtifactStore) -> None:
    """APR claimed from a note-rate cell (even if value matches) must be held."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    pts_sha = hashlib.sha256(b"0 Points").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-wrong-col-apr",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.375"),  # Value matches the Rate cell!
        points=Decimal("0"),
        raw_text="30-Year Fixed Conforming 360 Months 6.375% 0 Points",
        field_raw_texts={
            "note_rate": "6.375%",
            "apr": "6.375%",
            "points": "0 Points",
        },
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            # APR points to column 3 (Rate column, heading "Rate")!
            "apr": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            "points": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(5)",
                normalized_text_sha256=pts_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "non-APR column" in str(msg)


def test_reject_wrong_column_note_rate_rebound_to_apr_cell(store: ArtifactStore) -> None:
    """Note rate claimed from an APR cell must be held."""
    apr_sha = hashlib.sha256(b"6.450%").hexdigest()
    pts_sha = hashlib.sha256(b"0 Points").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-wrong-col-rate",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.450"),  # Value matches the APR cell!
        apr=Decimal("6.450"),
        points=Decimal("0"),
        raw_text="30-Year Fixed Conforming 360 Months 6.450% 0 Points",
        field_raw_texts={
            "note_rate": "6.450%",
            "apr": "6.450%",
            "points": "0 Points",
        },
        locators={
            # note_rate points to column 4 (APR column, heading "APR")!
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(4)",
                normalized_text_sha256=apr_sha,
            ),
            "apr": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(4)",
                normalized_text_sha256=apr_sha,
            ),
            "points": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(5)",
                normalized_text_sha256=pts_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "APR column" in str(msg)


def test_reject_product_family_semantic_rebinding_mortgage_to_heloc(store: ArtifactStore) -> None:
    """Mortgage candidate cannot be semantically bound to a HELOC row."""
    t1_rate_sha = hashlib.sha256(b"7.250%").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-mortgage-on-heloc",
        row_id="row-heloc-t1",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,  # Claims Mortgage!
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("7.250"),
        apr=Decimal("7.250"),
        raw_text="Home Equity Line of Credit <= 70% CLTV 7.250%",
        field_raw_texts={"note_rate": "7.250%", "apr": "7.250%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#heloc-rates tr:nth-child(1) td:nth-child(2)",
                normalized_text_sha256=t1_rate_sha,
            ),
            "apr": HtmlLocator(
                selector_type="css",
                selector="#heloc-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=t1_rate_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "cannot bind to product_family mortgage" in str(msg)


def test_reject_product_family_semantic_rebinding_heloc_to_mortgage(store: ArtifactStore) -> None:
    """HELOC candidate cannot be semantically bound to a fixed mortgage row."""
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    tier = BatchTier(
        tier_id="tier-cltv-70",
        bounds=(
            BatchTierBound(
                dimension=TierDimension.CLTV,
                lower=Decimal("0"),
                upper=Decimal("70"),
            ),
        ),
        note_rate=Decimal("6.375"),
        raw_text="6.375%",
        locator=HtmlLocator(
            selector_type="css",
            selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
            normalized_text_sha256=rate_sha,
        ),
    )
    candidate = BatchCandidate(
        candidate_id="cand-heloc-on-mortgage",
        row_id="row-30y",
        institution_charter=CHARTER,
        product_id="heloc-prime",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.HELOC,  # Claims HELOC!
        tiers=(tier,),
        raw_text="30-Year Fixed Conforming 6.375%",
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "cannot bind to product_family heloc" in str(msg)


def test_reject_loan_purpose_semantic_rebinding_purchase_to_refi(store: ArtifactStore) -> None:
    """Purchase candidate cannot be semantically bound to a refinance row."""
    refi_html = b"""<!DOCTYPE html>
<html>
<body>
<div id="mortgage-rates">
  <table>
    <thead>
      <tr><th>Product</th><th>Term</th><th>Rate</th><th>APR</th><th>Points</th></tr>
    </thead>
    <tbody>
      <tr id="row-refi-30y">
        <td class="prod">30-Year Fixed Refinance</td>
        <td class="term">360 Months</td>
        <td class="rate">6.375%</td>
        <td class="apr">6.450%</td>
        <td class="pts">0 Points</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>"""
    refi_capture = store.put(
        refi_html,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-refi-semantic",
    )

    rate_sha = hashlib.sha256(b"6.375%").hexdigest()
    apr_sha = hashlib.sha256(b"6.450%").hexdigest()
    pts_sha = hashlib.sha256(b"0 Points").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-purchase-on-refi",
        row_id="row-refi-30y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=refi_capture.fetch_run_id,
        artifact_id=refi_capture.artifact_id,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,  # Claims PURCHASE, row says Refinance!
        term_months=360,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.450"),
        points=Decimal("0"),
        raw_text="30-Year Fixed Refinance 360 Months 6.375% 6.450% 0 Points",
        field_raw_texts={
            "note_rate": "6.375%",
            "apr": "6.450%",
            "points": "0 Points",
        },
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
            "apr": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(4)",
                normalized_text_sha256=apr_sha,
            ),
            "points": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(1) td:nth-child(5)",
                normalized_text_sha256=pts_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "specifies refinance, cannot bind to loan_purpose purchase" in str(msg)


def test_reject_term_semantic_rebinding_30y_to_15y(store: ArtifactStore) -> None:
    """30-Year mortgage candidate cannot be semantically bound to a 15-Year row."""
    rate_15y_sha = hashlib.sha256(b"5.875%").hexdigest()
    apr_15y_sha = hashlib.sha256(b"5.950%").hexdigest()
    pts_sha = hashlib.sha256(b"0 Points").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-30y-on-15y",
        row_id="row-15y",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",  # Claims 30Y fixed product
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,  # Claims 360 months, but row is 15-Year (180 months)
        note_rate=Decimal("5.875"),
        apr=Decimal("5.950"),
        points=Decimal("0"),
        raw_text="15-Year Fixed Conforming 180 Months 5.875% 5.950% 0 Points",
        field_raw_texts={
            "note_rate": "5.875%",
            "apr": "5.950%",
            "points": "0 Points",
        },
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(2) td:nth-child(3)",
                normalized_text_sha256=rate_15y_sha,
            ),
            "apr": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(2) td:nth-child(4)",
                normalized_text_sha256=apr_15y_sha,
            ),
            "points": HtmlLocator(
                selector_type="css",
                selector="#mortgage-rates tr:nth-child(2) td:nth-child(5)",
                normalized_text_sha256=pts_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "conflicts with candidate term_months 360" in str(
        msg
    ) or "conflicts with 30-year product_id" in str(msg)


def test_reject_table_without_headings_or_attributes(store: ArtifactStore) -> None:
    """Without a header or cell attributes, the column is not verified."""
    plain_html = b"""<!DOCTYPE html>
<html>
<body>
<div id="plain-rates">
  <table>
    <tbody>
      <tr id="row-plain">
        <td>30-Year Fixed Conforming</td>
        <td>360 Months</td>
        <td>6.375%</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>"""
    plain_capture = store.put(
        plain_html,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-plain-table",
    )
    rate_sha = hashlib.sha256(b"6.375%").hexdigest()

    candidate = BatchCandidate(
        candidate_id="cand-plain-table",
        row_id="row-plain",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=plain_capture.fetch_run_id,
        artifact_id=plain_capture.artifact_id,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        term_months=360,
        note_rate=Decimal("6.375"),
        raw_text="30-Year Fixed Conforming 360 Months 6.375%",
        field_raw_texts={"note_rate": "6.375%"},
        locators={
            "note_rate": HtmlLocator(
                selector_type="css",
                selector="#plain-rates tr:nth-child(1) td:nth-child(3)",
                normalized_text_sha256=rate_sha,
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert disp is BatchDisposition.HELD
    assert reason is HoldReason.RAW_TEXT_MISMATCH
    assert "lacks explicit column headings or cell attributes" in str(msg)


# ---------------------------------------------------------------------------
# Opus Review Remediation: every material claim verified, explicit paths, row/column binding
# ---------------------------------------------------------------------------

RATE_CELL = "#mortgage-rates tr:nth-child(1) td:nth-child(3)"


def test_term_and_purpose_claims_need_their_own_verified_locators(store: ArtifactStore) -> None:
    """A headline-only locator no longer carries an unlocated term or purpose claim."""
    term_unlocated = _row30y_candidate(
        "cand-term-unlocated",
        ("note_rate",),
        note_rate=Decimal("6.375"),
        term_months=360,
    )
    disp, reason, msg = verify_batch_item_locators(term_unlocated, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "claims term_months without a verified term_months locator" in str(msg)

    purpose_unlocated = _row30y_candidate(
        "cand-purpose-unlocated",
        ("term_months", "note_rate"),
        note_rate=Decimal("6.375"),
        term_months=360,
        loan_purpose=LoanPurpose.PURCHASE,
    )
    disp2, reason2, msg2 = verify_batch_item_locators(purpose_unlocated, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "claims loan_purpose without a verified loan_purpose locator" in str(msg2)

    # The 15-year row never says "purchase": a purchase claim located on its product cell is
    # not credited, even though the row is otherwise a plain conforming mortgage.
    product_15y = "15-Year Fixed Conforming"
    purpose_unstated = BatchCandidate(
        candidate_id="cand-purpose-unstated",
        row_id="row-15y",
        institution_charter=CHARTER,
        product_id="mortgage-15y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=HTML_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        loan_purpose=LoanPurpose.PURCHASE,
        note_rate=Decimal("5.875"),
        raw_text=f"{product_15y} 5.875%",
        field_raw_texts={"loan_purpose": product_15y, "note_rate": "5.875%"},
        locators={
            "loan_purpose": _css("#mortgage-rates tr:nth-child(2) td:nth-child(1)", product_15y),
            "note_rate": _css("#mortgage-rates tr:nth-child(2) td:nth-child(3)", "5.875%"),
        },
        observed_at=T0,
    )
    disp3, reason3, msg3 = verify_batch_item_locators(purpose_unstated, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not explicitly disclose loan_purpose" in str(msg3)


def test_explicit_false_zero_and_attribute_claims_are_still_claims(store: ArtifactStore) -> None:
    """False/zero/optional attributes are present claims: unlocated holds, unprovable holds."""
    as_low_as_false = _row30y_candidate(
        "cand-low-false", ("note_rate",), note_rate=Decimal("6.375"), headline_is_as_low_as=False
    )
    disp, reason, msg = verify_batch_item_locators(as_low_as_false, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "headline_is_as_low_as" in str(msg)

    rate_cell = _css(RATE_CELL, "6.375%")
    located_false = _row30y_candidate(
        "cand-low-false-located",
        ("note_rate",),
        note_rate=Decimal("6.375"),
        headline_is_as_low_as=False,
        locators={**_row30y_locators("note_rate"), "headline_is_as_low_as": rate_cell},
        field_raw_texts={**_row30y_texts("note_rate"), "headline_is_as_low_as": "6.375%"},
    )
    disp2, reason2, msg2 = verify_batch_item_locators(located_false, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "cannot be proven" in str(msg2)

    # A true "as low as" claim needs those words in the located text, not just a rate.
    located_true = located_false.model_copy(update={"headline_is_as_low_as": True})
    disp3, reason3, msg3 = verify_batch_item_locators(located_true, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "as low as" in str(msg3)

    arm_unlocated = _row30y_candidate(
        "cand-arm-unlocated",
        ("note_rate",),
        note_rate=Decimal("6.375"),
        arm_terms=BatchArmTerms(adjustment_interval_months=12),
    )
    disp4, reason4, msg4 = verify_batch_item_locators(arm_unlocated, store)
    assert (disp4, reason4) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "arm_terms.adjustment_interval_months" in str(msg4)

    # The term cell is sibling evidence, not proof of the ARM adjustment interval.
    arm_on_term_cell = arm_unlocated.model_copy(
        update={
            "locators": {
                **_row30y_locators("note_rate"),
                "arm_terms.adjustment_interval_months": _css(
                    "#mortgage-rates tr:nth-child(1) td:nth-child(2)", "360 Months"
                ),
            },
            "field_raw_texts": {
                **_row30y_texts("note_rate"),
                "arm_terms.adjustment_interval_months": "360 Months",
            },
        }
    )
    disp5, reason5, msg5 = verify_batch_item_locators(arm_on_term_cell, store)
    assert (disp5, reason5) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "lacks an explicit label" in str(msg5)

    # Loan amount limits have no typed parser yet: held as unsupported, never inferred.
    amount_unlocated = _row30y_candidate(
        "cand-amount-unlocated",
        ("note_rate",),
        note_rate=Decimal("6.375"),
        max_amount=Decimal("500000"),
    )
    disp6, reason6, _msg6 = verify_batch_item_locators(amount_unlocated, store)
    assert (disp6, reason6) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)


DATED_HTML = b"""<!DOCTYPE html>
<html>
<body>
<div id="dated-rates">
  <table>
    <thead>
      <tr><th>Product</th><th>Term</th><th>Rate</th><th>Effective</th></tr>
    </thead>
    <tbody>
      <tr id="row-dated">
        <td class="prod">30-Year Fixed Purchase</td>
        <td class="term">360 Months</td>
        <td class="rate">6.375%</td>
        <td class="eff">Effective 11/19/2026</td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>"""


def test_effective_date_claim_needs_a_stated_matching_date(store: ArtifactStore) -> None:
    capture = store.put(
        DATED_HTML,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-dated-rates",
    )
    effective_text = "Effective 11/19/2026"

    def dated(effective_at: datetime, *, located: bool = True) -> BatchCandidate:
        locators: dict[str, Any] = {
            "note_rate": _css("#dated-rates tr:nth-child(1) td:nth-child(3)", "6.375%")
        }
        raw_texts = {"note_rate": "6.375%"}
        if located:
            locators["effective_at"] = _css(
                "#dated-rates tr:nth-child(1) td:nth-child(4)", effective_text
            )
            raw_texts["effective_at"] = effective_text
        return BatchCandidate(
            candidate_id="cand-dated",
            row_id="row-dated",
            institution_charter=CHARTER,
            product_id="mortgage-30y-fixed",
            source_id=SOURCE_ID,
            fetch_run_id=capture.fetch_run_id,
            artifact_id=capture.artifact_id,
            product_family=ProductFamily.MORTGAGE,
            note_rate=Decimal("6.375"),
            effective_at=effective_at,
            raw_text="30-Year Fixed Purchase 360 Months 6.375% Effective 11/19/2026",
            field_raw_texts=raw_texts,
            locators=locators,
            observed_at=T0,
        )

    stated = datetime(2026, 11, 19, tzinfo=UTC)
    assert verify_batch_item_locators(dated(stated), store) == (
        BatchDisposition.STAGED,
        None,
        None,
    )

    disp, reason, msg = verify_batch_item_locators(dated(datetime(2026, 11, 20, tzinfo=UTC)), store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not equal claimed effective_at" in str(msg)

    # A date-only disclosure cannot prove a time of day the model made up.
    disp2, reason2, msg2 = verify_batch_item_locators(
        dated(datetime(2026, 11, 19, 12, 0, tzinfo=UTC)), store
    )
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "time of day" in str(msg2)

    disp3, reason3, msg3 = verify_batch_item_locators(dated(stated, located=False), store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)
    assert "effective_at" in str(msg3)


def test_unknown_and_unbound_locator_paths_are_held(store: ArtifactStore) -> None:
    """Only explicit supported paths bound to a claimed field can verify; no generic text match."""
    valid = _row30y_candidate("cand-paths", ("note_rate",), note_rate=Decimal("6.375"))
    assert verify_batch_item_locators(valid, store) == (BatchDisposition.STAGED, None, None)

    rate_cell = _css(RATE_CELL, "6.375%")
    unknown_path = valid.model_copy(
        update={
            "locators": {**valid.locators, "bogus_field": rate_cell},
            "field_raw_texts": {**valid.field_raw_texts, "bogus_field": "6.375%"},
        }
    )
    disp, reason, msg = verify_batch_item_locators(unknown_path, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "not a supported material field path" in str(msg)

    # A points locator for a candidate that claims no points is not silently ignored.
    absent_field = valid.model_copy(
        update={
            "locators": {
                **valid.locators,
                "points": _css("#mortgage-rates tr:nth-child(1) td:nth-child(5)", "0 Points"),
            },
            "field_raw_texts": {**valid.field_raw_texts, "points": "0 Points"},
        }
    )
    disp2, reason2, msg2 = verify_batch_item_locators(absent_field, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "does not claim" in str(msg2)

    # The rate cell is sibling evidence, not proof of a max CLTV: the value needs its own label.
    sibling = valid.model_copy(
        update={
            "max_cltv": Decimal("6.375"),
            "locators": {**valid.locators, "max_cltv": rate_cell},
            "field_raw_texts": {**valid.field_raw_texts, "max_cltv": "6.375%"},
        }
    )
    disp3, reason3, msg3 = verify_batch_item_locators(sibling, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "lacks an explicit label" in str(msg3)

    # An allowed alias verifies the same claim as its canonical path.
    aliased = _row30y_candidate(
        "cand-alias",
        ("note_rate",),
        note_rate=Decimal("6.375"),
        term_months=360,
        locators={
            **_row30y_locators("note_rate"),
            "details.term_months": _row30y_locators("term_months")["term_months"],
        },
        field_raw_texts={**_row30y_texts("note_rate"), "details.term_months": "360 Months"},
    )
    assert verify_batch_item_locators(aliased, store) == (BatchDisposition.STAGED, None, None)


def test_finding_paths_and_text_must_state_the_claimed_availability(store: ArtifactStore) -> None:
    blocked_text = "Rate viewing is restricted to authenticated members."
    blocked_cell = _css("#coverage-findings p:nth-child(2)", blocked_text)

    def finding(**overrides: Any) -> BatchFinding:
        base: dict[str, Any] = {
            "finding_id": "find-paths",
            "institution_charter": CHARTER,
            "source_id": SOURCE_ID,
            "fetch_run_id": FETCH_RUN,
            "artifact_id": HTML_ARTIFACT_ID,
            "observed_at": T0,
            "label": "Blocked mortgage rates",
            "scope": ProductScope.IN_SCOPE,
            "family": ProductFamily.MORTGAGE,
            "rate_availability": RateAvailability.BLOCKED,
            "raw_text": blocked_text,
            "locators": {"availability": blocked_cell},
        }
        base.update(overrides)
        return BatchFinding(**base)

    assert verify_batch_item_locators(finding(), store) == (BatchDisposition.STAGED, None, None)

    unknown_path = finding(locators={"anything": blocked_cell})
    disp, reason, _ = verify_batch_item_locators(unknown_path, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)

    # Located text about a login wall does not prove a quote-only claim.
    wrong_availability = finding(rate_availability=RateAvailability.QUOTE_ONLY)
    disp2, reason2, msg2 = verify_batch_item_locators(wrong_availability, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not disclose rate_availability" in str(msg2)

    # A locator for a field the finding does not claim is unbound.
    absent = finding(scope=ProductScope.UNKNOWN, family=None, rate_availability=None)
    disp3, reason3, msg3 = verify_batch_item_locators(absent, store)
    assert (disp3, reason3) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "does not claim" in str(msg3)

    # An availability claim on an unknown-scope finding still needs its locator.
    unlocated = finding(scope=ProductScope.UNKNOWN, family=None, locators={})
    disp4, reason4, _ = verify_batch_item_locators(unlocated, store)
    assert (disp4, reason4) == (BatchDisposition.HELD, HoldReason.EVIDENCE_MISSING)


LINK_HTML = b"""<!DOCTYPE html>
<html>
<body>
<p><a href="https://example.test/rate/6.375">Rates</a></p>
</body>
</html>"""
LINK_TARGET = "https://example.test/rate/6.375"


def test_xpath_candidate_locator_cannot_stand_in_for_row_product_and_term(
    store: ArtifactStore,
) -> None:
    """XPath resolves no table cell, so it can never prove a row, product, or term for a value."""
    capture = store.put(
        LINK_HTML,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-link-xpath",
    )
    xpath = f"//a[@href='{LINK_TARGET}']/@href"
    candidate = BatchCandidate(
        candidate_id="cand-xpath",
        row_id="row-xpath",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=capture.fetch_run_id,
        artifact_id=capture.artifact_id,
        product_family=ProductFamily.MORTGAGE,
        note_rate=Decimal("6.375"),
        raw_text=LINK_TARGET,
        field_raw_texts={"note_rate": LINK_TARGET},
        locators={
            "note_rate": HtmlLocator(
                selector_type="xpath", selector=xpath, normalized_text_sha256=_sha(LINK_TARGET)
            )
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "xpath cannot be bound to a table row and column" in str(msg)

    # The same holds for a tier located by XPath.
    xpath_tier = BatchTier(
        tier_id="tier-xpath",
        bounds=(
            BatchTierBound(dimension=TierDimension.CLTV, lower=Decimal("0"), upper=Decimal("70")),
        ),
        note_rate=Decimal("7.250"),
        raw_text=LINK_TARGET,
        locator=HtmlLocator(
            selector_type="xpath", selector=xpath, normalized_text_sha256=_sha(LINK_TARGET)
        ),
    )
    heloc = _heloc_candidate("cand-xpath-tier", tiers=(xpath_tier,))
    disp2, reason2, msg2 = verify_batch_item_locators(heloc, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "xpath cannot be bound" in str(msg2)


def test_json_candidate_is_held_even_when_every_scalar_matches(store: ArtifactStore) -> None:
    """JSON pointers prove value text only; the candidate is retained under an unsupported hold."""
    candidate = BatchCandidate(
        candidate_id="cand-json-two-fields",
        row_id="row-json-01",
        institution_charter=CHARTER,
        product_id="mortgage-30y-fixed",
        source_id=SOURCE_ID,
        fetch_run_id=FETCH_RUN,
        artifact_id=JSON_ARTIFACT_ID,
        product_family=ProductFamily.MORTGAGE,
        note_rate=Decimal("6.375"),
        apr=Decimal("6.450"),
        raw_text="6.375% 6.450%",
        field_raw_texts={"note_rate": "6.375%", "apr": "6.450%"},
        locators={
            "note_rate": JsonLocator(
                pointer="/products/mortgage_30y/note_rate", response_sha256=JSON_ARTIFACT_ID
            ),
            "apr": JsonLocator(
                pointer="/products/mortgage_30y/apr", response_sha256=JSON_ARTIFACT_ID
            ),
        },
        observed_at=T0,
    )
    disp, reason, msg = verify_batch_item_locators(candidate, store)
    assert (disp, reason) == (BatchDisposition.HELD, HoldReason.LOCATOR_UNSUPPORTED)
    assert "cannot be bound to a product row and term" in str(msg)

    # A JSON scalar that is not a unit-bearing term cannot prove a term claim either: the typed
    # mismatch is reported first, before the unsupported-binding hold.
    with_term = candidate.model_copy(
        update={
            "term_months": 360,
            "locators": {
                **candidate.locators,
                "term_months": JsonLocator(
                    pointer="/effective_date", response_sha256=JSON_ARTIFACT_ID
                ),
            },
            "field_raw_texts": {**candidate.field_raw_texts, "term_months": "2026-11-19"},
        }
    )
    disp2, reason2, msg2 = verify_batch_item_locators(with_term, store)
    assert (disp2, reason2) == (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH)
    assert "does not disclose an explicit months/years unit" in str(msg2)

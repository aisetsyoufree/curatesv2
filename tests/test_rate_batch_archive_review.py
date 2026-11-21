"""Known failing synthetic review probes, preserved on the archive branch only."""

import hashlib
from decimal import Decimal

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import ConditionKind, ProductFamily, ProductScope, RateAvailability
from cu_rates.rate_batch.locators import verify_batch_item_locators
from cu_rates.rate_batch.models import BatchCondition, BatchDisposition, BatchFinding
from tests.test_rate_batch_evidence import (
    CHARTER,
    HELOC_T1_PRODUCT,
    HELOC_T1_SELECTOR,
    HTML_FIXTURE,
    SOURCE_ID,
    SOURCE_URL,
    T0,
    _css,
    _heloc_candidate,
)
from tests.test_rate_batch_evidence import (
    store as store,
)


def _save(store: ArtifactStore, body: bytes, fetch_run: str) -> str:
    store.put(
        body,
        media_type="text/html; charset=utf-8",
        source_url=SOURCE_URL,
        fetched_at=T0,
        http_status=200,
        fetch_run_id=fetch_run,
    )
    return hashlib.sha256(body).hexdigest()


def test_ordinary_ltv_does_not_prove_combined_ltv(store: ArtifactStore) -> None:
    visible = HELOC_T1_PRODUCT.replace("CLTV", "LTV")
    body = HTML_FIXTURE.replace(b"70% CLTV", b"70% LTV")
    assert body != HTML_FIXTURE
    artifact_id = _save(store, body, "review-ltv")
    candidate = _heloc_candidate(
        "review-ltv",
        artifact_id=artifact_id,
        fetch_run_id="review-ltv",
        tiers=(),
        max_cltv=Decimal("70"),
        raw_text=visible,
        locators={"max_cltv": _css(HELOC_T1_SELECTOR.format(col=1), visible)},
        field_raw_texts={"max_cltv": visible},
    )
    assert verify_batch_item_locators(candidate, store)[0] is BatchDisposition.HELD


def test_verified_raw_text_does_not_verify_conflicting_condition_description(
    store: ArtifactStore,
) -> None:
    candidate = _heloc_candidate(
        "review-tier-description",
        conditions=(
            BatchCondition(
                kind=ConditionKind.TIER,
                description="CLTV of at least 90 percent",
                raw_text=HELOC_T1_PRODUCT,
                locator=_css(HELOC_T1_SELECTOR.format(col=1), HELOC_T1_PRODUCT),
            ),
        ),
    )
    assert verify_batch_item_locators(candidate, store)[0] is BatchDisposition.HELD


def test_affirmative_rate_access_is_not_blocked_availability(store: ArtifactStore) -> None:
    visible = "Access to current mortgage rates is available."
    body = f'<html><body><p id="rate-access">{visible}</p></body></html>'.encode()
    artifact_id = _save(store, body, "review-open-access")
    finding = BatchFinding(
        finding_id="review-open-access",
        institution_charter=CHARTER,
        source_id=SOURCE_ID,
        fetch_run_id="review-open-access",
        artifact_id=artifact_id,
        observed_at=T0,
        label="Mortgage rate access",
        scope=ProductScope.IN_SCOPE,
        family=ProductFamily.MORTGAGE,
        rate_availability=RateAvailability.BLOCKED,
        raw_text=visible,
        locators={"availability": _css("#rate-access", visible)},
    )
    assert verify_batch_item_locators(finding, store)[0] is BatchDisposition.HELD

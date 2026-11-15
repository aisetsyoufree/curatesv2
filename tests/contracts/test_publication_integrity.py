"""Temporal and uniqueness integrity of the publication inputs."""

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    Correction,
    OfferSnapshot,
    PriorPublication,
    PublicationOutcome,
    ReasonCode,
    WithdrawalScope,
    evaluate_publication,
)

from .factories import (
    EVALUATED_AT,
    IMAGE_ARTIFACT_ID,
    PARSER,
    SV,
    T0,
    context,
    fetch_run,
    prior,
    published_snapshot,
    purchase_offer,
    raw_artifact,
    review,
    validation,
    withdrawal,
)

R = ReasonCode


def test_legitimate_old_value_change_triggers_source_abandoned() -> None:
    offer = purchase_offer(effective_at=T0 - timedelta(days=200))
    decision = evaluate_publication(offer, context(offer, prior=prior(offer, changed_days_ago=120)))
    assert R.SOURCE_ABANDONED in decision.reasons
    assert R.CLOCK_SKEW not in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW


def test_value_change_after_publication_is_rejected() -> None:
    offer = purchase_offer()
    published = published_snapshot(offer)
    with pytest.raises(ValidationError, match="postdate the publication"):
        PriorPublication(
            snapshot=OfferSnapshot.from_offer(offer),
            published=published,
            review=review(offer),
            last_value_change_at=published.published_at + timedelta(seconds=1),
        )


def test_future_value_change_cannot_auto_publish() -> None:
    offer = purchase_offer()
    future = EVALUATED_AT + timedelta(days=1)
    skewed = PriorPublication(
        snapshot=OfferSnapshot.from_offer(offer),
        published=published_snapshot(offer, published_at=future),
        last_value_change_at=future,
    )
    decision = evaluate_publication(offer, context(offer, prior=skewed))
    assert R.CLOCK_SKEW in decision.reasons
    assert not decision.publishable_now


def test_duplicate_context_records_are_rejected() -> None:
    offer = purchase_offer()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("known artifact", {"known_artifacts": (raw_artifact(), raw_artifact())}),
        ("fetch run", {"fetch_runs": (fetch_run(), fetch_run())}),
        ("approved parser", {"approved_parsers": (PARSER, PARSER)}),
        (
            "validation rule",
            {"validations": (validation("rule.rate-range", offer),) * 2},
        ),
        (
            "withdrawal",
            {
                "withdrawals": (
                    withdrawal(WithdrawalScope.OFFER, "offer-001"),
                    withdrawal(WithdrawalScope.PRODUCT, "prod-001"),
                )
            },
        ),
        (
            "correction",
            {
                "corrections": (
                    Correction(
                        schema_version=SV,
                        correction_id="corr-1",
                        offer_id=offer.offer_id,
                        field_path="note_rate",
                        previous_value="1",
                        corrected_value="2",
                        actor="Sid",
                        corrected_at=T0,
                        reason="typo",
                    ),
                )
                * 2
            },
        ),
    ]
    for label, overrides in cases:
        with pytest.raises(ValidationError, match=f"duplicate {label}"):
            context(offer, **overrides)


def test_distinct_context_records_are_accepted() -> None:
    offer = purchase_offer()
    ctx = context(
        offer,
        known_artifacts=(raw_artifact(), raw_artifact(IMAGE_ARTIFACT_ID)),
        validations=(
            validation("rule.rate-range", offer),
            validation("rule.other", offer),
        ),
    )
    assert len(ctx.known_artifacts) == 2

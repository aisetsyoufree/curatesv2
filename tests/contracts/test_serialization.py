"""Versioned fixture parsing and JSON round-trips for every product kind."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    CONTRACT_VERSION,
    ArmTerms,
    ConditionKind,
    HelocDetails,
    IntroRate,
    LoanPurpose,
    NormalizedOffer,
    OfferStatus,
    PurchaseMortgageDetails,
    RateType,
    TierDimension,
)

from .factories import (
    T0,
    adjustable_details,
    condition,
    equity_offer,
    grid_tier,
    heloc_details,
    heloc_offer,
    margin,
    points,
    purchase_offer,
    rate,
    refinance_offer,
)

FIXTURE = Path(__file__).parent / "fixtures" / "v1_purchase_offer.json"


def _load() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text())
    return loaded


def test_v1_fixture_parses_with_exact_decimals_and_utc() -> None:
    offer = NormalizedOffer.model_validate_json(FIXTURE.read_text())
    assert offer.schema_version == CONTRACT_VERSION == "1.0"
    assert isinstance(offer.details, PurchaseMortgageDetails)
    assert offer.note_rate is not None
    assert offer.apr is not None
    assert offer.points is not None
    assert isinstance(offer.note_rate.value, Decimal)
    assert str(offer.apr.value) == "6.250"  # scale survives; never a float
    assert offer.points.value == 0
    assert offer.published_at == datetime(2026, 11, 15, 12, 30, tzinfo=UTC)  # from -05:00
    for moment in (offer.observed_at, offer.published_at, offer.valid_until, offer.effective_at):
        assert moment is not None
        assert moment.utcoffset() == timedelta(0)
    assert {e.path for e in offer.field_provenance} == offer.material_paths()
    assert offer.apr.provenance.locator.kind == "json"


def test_v1_fixture_matches_the_current_model_exactly() -> None:
    offer = NormalizedOffer.model_validate_json(FIXTURE.read_text())
    assert NormalizedOffer.model_validate(offer.model_dump(mode="json")) == offer


@pytest.mark.parametrize("version", ["2.0", "3.0", "1.1", "0.9", "01.0", "1.00", "1"])
def test_fixture_with_unsupported_version_is_rejected(version: str) -> None:
    payload = {**_load(), "schema_version": version}
    with pytest.raises(ValidationError, match="schema_version"):
        NormalizedOffer.model_validate(payload)
    with pytest.raises(ValidationError, match="schema_version"):
        NormalizedOffer.model_validate_json(json.dumps(payload))


def test_fixture_rejects_unknown_fields_and_float_rates() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NormalizedOffer.model_validate({**_load(), "surprise": True})
    payload = _load()
    payload["note_rate"]["value"] = 6.125
    with pytest.raises(ValidationError, match="binary floats"):
        NormalizedOffer.model_validate(payload)


def _rich_offers() -> dict[str, NormalizedOffer]:
    arm = ArmTerms(
        initial_period_months=84,
        adjustment_interval_months=12,
        index_name="SOFR",
        margin=margin("-0.250"),
        initial_cap=Decimal("5.000"),
        periodic_cap=Decimal("2"),
        lifetime_cap=Decimal("5"),
    )
    return {
        "purchase": purchase_offer(
            apr=rate("6.250"),
            points=points("0.000"),
            conditions=(condition("c1", ConditionKind.DISCOUNT),),
            tiers=(
                grid_tier(
                    "t1", (TierDimension.LTV, 0, 80), (TierDimension.CREDIT_SCORE, 740, None)
                ),
            ),
            status=OfferStatus.PUBLISHED,
            observed_at=T0 + timedelta(hours=2),
            published_at=T0 + timedelta(hours=3),
            valid_until=T0 + timedelta(hours=40),
        ),
        "purchase-arm": purchase_offer(
            details=adjustable_details(arm), scenario_id=None, scenario_matches=()
        ),
        "refinance": refinance_offer(LoanPurpose.RATE_TERM_REFINANCE),
        "equity": equity_offer(),
        "heloc": heloc_offer(
            heloc_details(
                intro=IntroRate(rate=rate("3.990"), duration_months=6),
                margin=margin("-0.250"),
                ceiling=rate("18.000"),
            )
        ),
    }


@pytest.mark.parametrize("name", sorted(_rich_offers()))
def test_json_round_trip_is_lossless(name: str) -> None:
    offer = _rich_offers()[name]
    text = offer.model_dump_json()
    parsed = NormalizedOffer.model_validate_json(text)
    assert parsed == offer
    assert parsed.model_dump_json() == text  # stable, byte-for-byte
    assert parsed.model_dump(mode="json") == offer.model_dump(mode="json")
    assert NormalizedOffer.model_validate(json.loads(text)) == offer
    assert type(parsed.details) is type(offer.details)
    assert parsed.family is offer.family


def test_round_trip_preserves_decimal_scale_and_sign() -> None:
    offer = _rich_offers()["heloc"]
    parsed = NormalizedOffer.model_validate_json(offer.model_dump_json())
    assert isinstance(parsed.details, HelocDetails)
    assert parsed.details.margin is not None
    assert parsed.details.margin.value == Decimal("-0.250")
    assert str(parsed.details.margin.value) == "-0.250"
    assert isinstance(parsed.details.margin.value, Decimal)
    assert parsed.details.ceiling is not None
    assert str(parsed.details.ceiling.value) == "18.000"
    arm_offer = _rich_offers()["purchase-arm"]
    reparsed = NormalizedOffer.model_validate_json(arm_offer.model_dump_json())
    assert isinstance(reparsed.details, PurchaseMortgageDetails)
    assert reparsed.details.rate_type is RateType.ADJUSTABLE
    assert reparsed.details.arm is not None
    assert str(reparsed.details.arm.initial_cap) == "5.000"


def test_round_trip_preserves_utc_for_every_timestamp() -> None:
    offer = _rich_offers()["purchase"]
    parsed = NormalizedOffer.model_validate_json(offer.model_dump_json())
    assert parsed.observed_at == T0 + timedelta(hours=2)
    moments = (parsed.observed_at, parsed.published_at, parsed.valid_until, parsed.effective_at)
    for moment in moments:
        assert moment is not None
        assert moment.tzinfo is not None
        assert moment.utcoffset() == timedelta(0)
    assert parsed.model_dump_json().count("Z") >= 4  # emitted as UTC, not local offsets

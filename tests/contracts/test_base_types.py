from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    CONTRACT_VERSION,
    Institution,
    InstitutionStatus,
    SourcedMargin,
    SourcedRate,
    canonical_decimal,
)

from .factories import T0, heloc_details, institution, margin, provenance, purchase_offer, rate


def _rate_input(value: object) -> dict[str, Any]:
    return {"value": value, "provenance": provenance().model_dump()}


def _assign(obj: object, name: str, value: object) -> None:
    setattr(obj, name, value)


def test_models_are_frozen() -> None:
    inst = institution()
    with pytest.raises(ValidationError):
        _assign(inst, "legal_name", "Other")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Institution.model_validate({**institution().model_dump(mode="json"), "surprise": 1})


def test_schema_version_is_explicit_not_defaulted() -> None:
    assert institution().schema_version == CONTRACT_VERSION == "1.1"
    payload = institution().model_dump(mode="json")
    del payload["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        Institution.model_validate(payload)


def test_current_version_parses_including_in_nested_models() -> None:
    assert institution(schema_version="1.0").schema_version == "1.0"
    assert purchase_offer(schema_version="1.0").schema_version == "1.0"


@pytest.mark.parametrize(
    "value",
    [
        "2.0",
        "3.1",
        "0.9",
        "0.0",  # other majors
        "1.2",
        "1.9",
        "1.10",
        "1.100",  # newer minor: models forbid unknown fields, so unreadable
        "01.0",
        "1.00",
        "1.01",
        "001.000",
        "+1.0",
        "1.+0",
        "-1.0",  # malformed integers
        " 1.0",
        "1.0 ",
        " 1.0 ",
        "1.0\n",
        "\t1.0",  # padding is rejected, never normalized
        "1",
        "v1",
        "1.x",
        "1.0.0",
        "",
        "\uff11.\uff10",  # non-canonical text (full-width)
    ],
)
def test_malformed_or_unsupported_versions_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        institution(schema_version=value)


@pytest.mark.parametrize("value", ["-0.25", "0", "7.5", "-100", "100"])
def test_signed_margin_accepts_prime_minus(value: str) -> None:
    assert margin(value).value == Decimal(value)


@pytest.mark.parametrize("value", ["-100.00001", "100.00001", "NaN", "-Infinity", "1.123456"])
def test_signed_margin_bounds(value: str) -> None:
    with pytest.raises(ValidationError):
        margin(value)


def test_signed_margin_rejects_floats_and_unsigned_rate_rejects_negatives() -> None:
    with pytest.raises(ValidationError, match="binary floats"):
        SourcedMargin.model_validate({"value": -0.25, "provenance": provenance().model_dump()})
    with pytest.raises(ValidationError):
        rate("-0.25")


def test_prime_minus_a_quarter_is_representable_and_derives() -> None:
    details = heloc_details(margin=margin("-0.250"))  # Prime - 0.25
    post = details.post_intro()
    assert post.rate == Decimal("7.250")  # 7.500 + (-0.250), above the 4.000 floor


@pytest.mark.parametrize("value", [6.125, 0.1])
def test_binary_floats_are_rejected_for_rates(value: float) -> None:
    with pytest.raises(ValidationError, match="binary floats"):
        SourcedRate.model_validate(_rate_input(value))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "100.00001", "1.123456"])
def test_invalid_percent_values_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        SourcedRate.model_validate(_rate_input(value))


@pytest.mark.parametrize("value", ["0", "100", "6.12345"])
def test_percent_boundaries_accepted(value: str) -> None:
    assert SourcedRate.model_validate(_rate_input(value)).value == Decimal(value)


def test_decimal_precision_is_preserved_exactly() -> None:
    rate = SourcedRate.model_validate(_rate_input("6.12500"))
    assert str(rate.value) == "6.12500"
    assert rate.model_dump(mode="json")["value"] == "6.12500"


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError):
        purchase_offer(observed_at=datetime(2026, 11, 15, 12, 0))


def test_non_utc_datetimes_normalize_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    offer = purchase_offer(observed_at=datetime(2026, 11, 15, 7, 0, tzinfo=eastern))
    assert offer.observed_at == T0
    assert offer.observed_at.tzinfo is UTC


def test_status_enum_validates_values() -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "charter_number": 1,
        "legal_name": "X",
        "status": "retired",
        "charter_verified": True,
    }
    with pytest.raises(ValidationError):
        Institution.model_validate(payload)
    payload["status"] = "active"
    assert Institution.model_validate(payload).status is InstitutionStatus.ACTIVE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("6.250"), "6.25"),
        (Decimal("6.25"), "6.25"),
        (Decimal("100"), "100"),
        (Decimal("0.00"), "0"),
        (Decimal("0.5E+1"), "5"),
    ],
)
def test_canonical_decimal(value: Decimal, expected: str) -> None:
    assert canonical_decimal(value) == expected

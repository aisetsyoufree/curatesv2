"""Deterministic checks bound to the offer fingerprints. No inferred rates."""

from decimal import Decimal

from cu_rates.contracts.base import UtcDatetime
from cu_rates.contracts.evidence import ParserVersion
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import APR_RULE_ID, ValidationPolicy, ValidationResult

VALIDATOR = ParserVersion(name="t014-slice-validator", version="0.1.0")
RATE_RULE = "rule.rate-in-percent-domain"
POINTS_RULE = "rule.points-non-negative"
REQUIRED_RULES = (RATE_RULE, APR_RULE_ID, POINTS_RULE)


def validation_policy(approved_at: UtcDatetime) -> ValidationPolicy:
    return ValidationPolicy(
        schema_version="1.0",
        policy_id="t014-slice-validation",
        policy_version="1",
        required_rules=REQUIRED_RULES,
        approved_by="example-reviewer",
        approved_at=approved_at,
    )


def _result(
    offer: NormalizedOffer,
    rule_id: str,
    passed: bool,
    detail: str | None,
    paths: tuple[str, ...],
    evaluated_at: UtcDatetime,
) -> ValidationResult:
    return ValidationResult(
        rule_id=rule_id,
        passed=passed,
        detail=detail,
        offer_id=offer.offer_id,
        fingerprints=offer_fingerprints(offer),
        evaluated_at=evaluated_at,
        validator=VALIDATOR,
        checked_paths=paths,
    )


def validate_offer(
    offer: NormalizedOffer, evaluated_at: UtcDatetime
) -> tuple[ValidationResult, ...]:
    """Rate domain, APR at least the note rate, and explicit points. Failures stay visible."""
    note = None if offer.note_rate is None else offer.note_rate.value
    apr = None if offer.apr is None else offer.apr.value
    points = None if offer.points is None else offer.points.value
    if note is not None and apr is not None:
        rate_ok = Decimal(0) <= note <= Decimal(100) and Decimal(0) <= apr <= Decimal(100)
        apr_ok = apr >= note
    else:
        rate_ok = False
        apr_ok = False
    points_ok = points is not None and Decimal(0) <= points <= Decimal(10)
    return (
        _result(
            offer,
            RATE_RULE,
            rate_ok,
            None if rate_ok else "note rate or APR is missing or outside 0 to 100 percent",
            ("note_rate", "apr"),
            evaluated_at,
        ),
        _result(
            offer,
            APR_RULE_ID,
            apr_ok,
            None if apr_ok else "APR is missing or below the note rate",
            ("apr", "note_rate"),
            evaluated_at,
        ),
        _result(
            offer,
            POINTS_RULE,
            points_ok,
            None if points_ok else "points are missing or outside 0 to 10",
            ("points",),
            evaluated_at,
        ),
    )

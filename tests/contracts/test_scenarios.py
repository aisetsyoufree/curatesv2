from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    CANONICAL_SCENARIOS,
    REQUIRED_DIMENSIONS,
    CanonicalScenarioId,
    ComparisonScenario,
    DimensionMatch,
    LoanPurpose,
    MatchState,
    ProductFamily,
    ScenarioDimension,
    aggregate_match,
    evaluate_scenario_match,
    match_numeric,
)

from .factories import matches_all

D = Decimal


def test_four_canonical_scenarios_match_master_spec() -> None:
    assert set(CANONICAL_SCENARIOS) == set(CanonicalScenarioId)
    purchase = CANONICAL_SCENARIOS[CanonicalScenarioId.PURCHASE_30Y_FIXED]
    assert (purchase.property_value, purchase.loan_amount, purchase.ltv) == (
        D(500000),
        D(400000),
        D(80),
    )
    assert (purchase.credit_score, purchase.lock_days, purchase.term_months) == (780, 30, 360)
    assert purchase.points == 0
    assert not purchase.relationship_discount_applied
    assert not purchase.autopay_discount_applied

    refi = CANONICAL_SCENARIOS[CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED]
    assert refi.purpose is LoanPurpose.RATE_TERM_REFINANCE
    assert refi.loan_amount == 400000

    equity = CANONICAL_SCENARIOS[CanonicalScenarioId.FIXED_HOME_EQUITY_10Y]
    assert equity.family is ProductFamily.HOME_EQUITY_LOAN
    assert (equity.first_lien_balance, equity.loan_amount, equity.cltv) == (
        D(350000),
        D(50000),
        D(80),
    )
    assert equity.term_months == 120

    heloc = CANONICAL_SCENARIOS[CanonicalScenarioId.HELOC]
    assert heloc.family is ProductFamily.HELOC
    assert heloc.term_months is None
    assert heloc.cltv == 80


def _payload(**overrides: Any) -> dict[str, Any]:
    base = CANONICAL_SCENARIOS[CanonicalScenarioId.PURCHASE_30Y_FIXED].model_dump(mode="json")
    base.update(overrides)
    return base


def test_scenario_leverage_must_agree_with_amounts() -> None:
    with pytest.raises(ValidationError, match="disagrees"):
        ComparisonScenario.model_validate(_payload(ltv="75"))


def test_mortgage_scenario_requires_purpose_and_rejects_cltv() -> None:
    with pytest.raises(ValidationError, match="purpose"):
        ComparisonScenario.model_validate(_payload(purpose=None))
    with pytest.raises(ValidationError, match="ltv only"):
        ComparisonScenario.model_validate(_payload(cltv="80"))


def test_equity_scenario_requires_first_lien_and_cltv() -> None:
    heloc = CANONICAL_SCENARIOS[CanonicalScenarioId.HELOC].model_dump(mode="json")
    with pytest.raises(ValidationError, match="first_lien_balance"):
        ComparisonScenario.model_validate({**heloc, "first_lien_balance": None})
    with pytest.raises(ValidationError, match="purpose"):
        ComparisonScenario.model_validate({**heloc, "purpose": "purchase"})


@pytest.mark.parametrize("score", [299, 851])
def test_credit_score_bounds(score: int) -> None:
    with pytest.raises(ValidationError):
        ComparisonScenario.model_validate(_payload(credit_score=score))


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"exact": D(780)}, MatchState.MATCHES),
        ({"exact": D(760)}, MatchState.CONFLICTS),
        ({"lower": D(740), "upper": D(799)}, MatchState.MATCHES),
        ({"lower": D(780), "upper": D(799)}, MatchState.MATCHES),  # inclusive lower
        ({"lower": D(700), "upper": D(780)}, MatchState.MATCHES),  # inclusive upper
        ({"lower": D(781), "upper": D(799)}, MatchState.CONFLICTS),
        ({"lower": D(700), "upper": D(779)}, MatchState.CONFLICTS),
        ({"lower": D(740)}, MatchState.MATCHES),  # open-ended above
        ({"upper": D(779)}, MatchState.CONFLICTS),  # open-ended below
        ({}, MatchState.UNSTATED),
    ],
)
def test_match_numeric_tri_state(kwargs: dict[str, Decimal], expected: MatchState) -> None:
    assert match_numeric(D(780), **kwargs) is expected


def test_match_numeric_rejects_ambiguous_inputs() -> None:
    with pytest.raises(ValueError, match="either"):
        match_numeric(D(780), exact=D(780), lower=D(700))
    with pytest.raises(ValueError, match="lower bound"):
        match_numeric(D(780), lower=D(800), upper=D(700))


def test_aggregate_match_precedence() -> None:
    m, c, u = MatchState.MATCHES, MatchState.CONFLICTS, MatchState.UNSTATED
    assert aggregate_match([m, m]) is m
    assert aggregate_match([m, u]) is u
    assert aggregate_match([m, u, c]) is c
    assert aggregate_match([]) is u


def test_dimension_match_text_rules() -> None:
    with pytest.raises(ValidationError, match="cite"):
        DimensionMatch(dimension=ScenarioDimension.LTV, state=MatchState.MATCHES)
    with pytest.raises(ValidationError, match="unstated"):
        DimensionMatch(
            dimension=ScenarioDimension.LTV, state=MatchState.UNSTATED, source_text="80% LTV"
        )
    unstated = DimensionMatch(dimension=ScenarioDimension.LTV, state=MatchState.UNSTATED)
    assert unstated.source_text is None


@pytest.mark.parametrize("scenario_id", list(CanonicalScenarioId))
def test_scenario_match_requires_every_dimension(scenario_id: CanonicalScenarioId) -> None:
    full = matches_all(scenario_id)
    assert evaluate_scenario_match(scenario_id, full) is MatchState.MATCHES
    # Dropping any required dimension makes the whole thing unstated.
    for index in range(len(full)):
        partial = full[:index] + full[index + 1 :]
        assert evaluate_scenario_match(scenario_id, partial) is MatchState.UNSTATED


def test_conflict_dominates_unstated() -> None:
    scenario_id = CanonicalScenarioId.PURCHASE_30Y_FIXED
    full = list(matches_all(scenario_id))
    full[0] = DimensionMatch(dimension=full[0].dimension, state=MatchState.UNSTATED)
    full[1] = DimensionMatch(
        dimension=full[1].dimension, state=MatchState.CONFLICTS, source_text="stated otherwise"
    )
    assert evaluate_scenario_match(scenario_id, full) is MatchState.CONFLICTS


def test_required_dimensions_reflect_scenario_semantics() -> None:
    refi = REQUIRED_DIMENSIONS[CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED]
    purchase = REQUIRED_DIMENSIONS[CanonicalScenarioId.PURCHASE_30Y_FIXED]
    assert ScenarioDimension.CASH_OUT in refi - purchase
    assert ScenarioDimension.POINTS in purchase
    assert ScenarioDimension.CLTV in REQUIRED_DIMENSIONS[CanonicalScenarioId.HELOC]
    assert ScenarioDimension.TERM_MONTHS not in REQUIRED_DIMENSIONS[CanonicalScenarioId.HELOC]

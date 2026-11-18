"""AUD7-8 review: a ranked stored row must prove EVERY canonical assumption, in all scenarios.

Exercises the storage-boundary check directly so the equity and HELOC scenarios
(first-lien balance) and the mortgage scenarios (autopay) are each covered.
"""

from decimal import Decimal

import pytest

from cu_rates.contracts import CanonicalScenarioId, ScenarioDimension
from cu_rates.contracts.scenarios import REQUIRED_DIMENSIONS, StatedBound
from cu_rates.snapshot._models import PublicAssumption, require_ranked_assumptions

from . import factories as f

CASES = [
    (scenario_id, dimension)
    for scenario_id in CanonicalScenarioId
    for dimension in sorted(REQUIRED_DIMENSIONS[scenario_id])
]


def _good(scenario_id: CanonicalScenarioId) -> list[PublicAssumption]:
    return [
        PublicAssumption(dimension=m.dimension, state=m.state, stated=m.stated)
        for m in f.matches_all(scenario_id)
    ]


@pytest.mark.parametrize("scenario_id", list(CanonicalScenarioId))
def test_a_fully_proven_ranked_row_is_accepted(scenario_id: CanonicalScenarioId) -> None:
    assumptions = _good(scenario_id)
    assert {a.dimension for a in assumptions} == REQUIRED_DIMENSIONS[scenario_id]
    require_ranked_assumptions(scenario_id, tuple(assumptions))


def test_new_canonical_dimensions_are_required_where_the_issue_says() -> None:
    mortgage = (
        CanonicalScenarioId.PURCHASE_30Y_FIXED,
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED,
    )
    equity = (CanonicalScenarioId.FIXED_HOME_EQUITY_10Y, CanonicalScenarioId.HELOC)
    for scenario_id in mortgage:
        assert ScenarioDimension.AUTOPAY in REQUIRED_DIMENSIONS[scenario_id]
    for scenario_id in equity:
        assert ScenarioDimension.FIRST_LIEN_BALANCE in REQUIRED_DIMENSIONS[scenario_id]
    for scenario_id in CanonicalScenarioId:
        assert ScenarioDimension.PROPERTY_VALUE in REQUIRED_DIMENSIONS[scenario_id]


@pytest.mark.parametrize(("scenario_id", "dimension"), CASES)
@pytest.mark.parametrize("defect", ["missing", "conflicts", "untyped", "duplicate-conflict"])
def test_each_required_dimension_must_be_proven(
    scenario_id: CanonicalScenarioId, dimension: ScenarioDimension, defect: str
) -> None:
    kept = [a for a in _good(scenario_id) if a.dimension is not dimension]
    target = next(a for a in _good(scenario_id) if a.dimension is dimension)
    if defect == "conflicts":
        kept.append(target.model_copy(update={"state": "conflicts"}))
    elif defect == "untyped":
        kept.append(target.model_copy(update={"stated": None}))
    elif defect == "duplicate-conflict":
        kept.extend((target, target.model_copy(update={"state": "conflicts"})))
    with pytest.raises(ValueError, match=r"canonical assumption|duplicate"):
        require_ranked_assumptions(scenario_id, tuple(kept))


@pytest.mark.parametrize(("scenario_id", "dimension"), CASES)
def test_a_wrong_stated_value_cannot_pass_as_a_match(
    scenario_id: CanonicalScenarioId, dimension: ScenarioDimension
) -> None:
    assumptions = _good(scenario_id)
    index = next(i for i, a in enumerate(assumptions) if a.dimension is dimension)
    original = assumptions[index].stated
    assert original is not None
    if original.token is not None:
        wrong = StatedBound(token="definitely-not-the-canonical-token")
    else:
        assert original.exact is not None
        wrong = StatedBound(exact=original.exact + Decimal(1))
    assumptions[index] = assumptions[index].model_copy(update={"stated": wrong})
    # The stored label still says "matches": only recomputation can refuse it.
    assert assumptions[index].state.value == "matches"
    with pytest.raises(ValueError, match="canonical assumption"):
        require_ranked_assumptions(scenario_id, tuple(assumptions))

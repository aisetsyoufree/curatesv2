"""#116: contract arithmetic must not depend on the process's ambient Decimal precision.

An embedder or library that lowers ``decimal.getcontext().prec`` must not change an LTV check,
a HELOC post-intro derivation, or the snapshot reader's re-derivation (and so a publication
verdict). Each case runs at several ambient precisions; every one must behave as at the default.
"""

from decimal import Decimal, localcontext

import pytest

from cu_rates.contracts import CanonicalScenarioId
from cu_rates.contracts.base import ARITHMETIC_PRECISION, contract_arithmetic
from cu_rates.contracts.enums import LienPosition, PostIntroState
from cu_rates.contracts.offers import HelocDetails
from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, ComparisonScenario
from cu_rates.snapshot._models import derived_post_intro_rate

from . import factories as f

PRECISIONS = [1, 3, 6, 10, 50]


@pytest.fixture(params=PRECISIONS)
def ambient(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    with localcontext() as context:
        context.prec = request.param
        yield request.param


def _scenario(**changes: object) -> ComparisonScenario:
    base = CANONICAL_SCENARIOS[CanonicalScenarioId.PURCHASE_30Y_FIXED].model_dump()
    return ComparisonScenario.model_validate(base | changes)


def test_the_helper_pins_the_documented_precision_whatever_the_ambient_one(ambient: int) -> None:
    with contract_arithmetic() as context:
        assert context.prec == ARITHMETIC_PRECISION
        assert Decimal(1) / Decimal(3) == Decimal("0.3333333333333333333333333333")


def test_every_canonical_scenario_validates_at_any_ambient_precision(ambient: int) -> None:
    for scenario in CANONICAL_SCENARIOS.values():
        ComparisonScenario.model_validate(scenario.model_dump())


def test_a_repeating_ltv_is_judged_the_same_at_any_ambient_precision(ambient: int) -> None:
    # 333,333 / 500,000 = 66.6666%: 66.67 is within a cent of it, 66.70 is not.
    assert _scenario(loan_amount="333333", ltv="66.67").ltv == Decimal("66.67")
    with pytest.raises(ValueError, match=r"disagrees with computed"):
        _scenario(loan_amount="333333", ltv="66.70")


def test_heloc_derivation_keeps_every_digit_at_any_ambient_precision(ambient: int) -> None:
    details = HelocDetails(
        index_name="Prime",
        index_rate=f.rate("6.12345"),
        margin=f.margin("0.00001"),
        floor=f.rate("4.00000"),
        stated_post_intro_apr=f.rate("6.12346"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    derived = details.post_intro()
    assert derived.state is PostIntroState.STATED
    assert details.post_intro_conflicts() is False  # 6.12345 + 0.00001 == 6.12346 exactly
    unstated = details.model_copy(update={"stated_post_intro_apr": None})
    result = unstated.post_intro()
    assert result.state is PostIntroState.DERIVED and result.rate == Decimal("6.12346")


def test_a_real_one_digit_difference_is_still_a_conflict(ambient: int) -> None:
    details = HelocDetails(
        index_name="Prime",
        index_rate=f.rate("6.12345"),
        margin=f.margin("0.00001"),
        floor=f.rate("4.00000"),
        stated_post_intro_apr=f.rate("6.12347"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    assert details.post_intro_conflicts() is True


def test_the_snapshot_readers_rederivation_is_context_independent(ambient: int) -> None:
    index, margin, floor = Decimal("6.12345"), Decimal("0.00001"), Decimal("4.00000")
    assert derived_post_intro_rate(index, margin, floor, None) == Decimal("6.12346")
    assert derived_post_intro_rate(index, margin, Decimal("7"), None) == Decimal("7")  # floor wins
    assert derived_post_intro_rate(index, margin, floor, Decimal("6.12340")) == Decimal("6.12340")


def test_a_difference_just_inside_the_tolerance_is_not_rounded_over_it(ambient: int) -> None:
    """200,000 / 600,000 is 33.3333...; 33.343 is 0.00967 away (valid). Unpinned arithmetic at
    precision 1 rounds that difference to 0.01 and wrongly rejects it."""
    scenario = _scenario(property_value="600000", loan_amount="200000", ltv="33.343")
    assert scenario.ltv == Decimal("33.343")
    with pytest.raises(ValueError, match=r"disagrees with computed"):
        _scenario(property_value="600000", loan_amount="200000", ltv="33.344")  # 0.01067 away

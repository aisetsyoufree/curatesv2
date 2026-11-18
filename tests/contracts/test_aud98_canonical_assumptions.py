"""Issue #98 (AUD7-8): every canonical scenario assumption must be enforced in
the publication/ranking path.  Property value, autopay, and first-lien balance
are canonical assumptions of their respective scenarios but were absent from
REQUIRED_DIMENSIONS before this fix, so an offer could be ranked without any
source-stated evidence for them.

Each test below verifies the fix is in force.  Without the fix REQUIRED_DIMENSIONS
did not include the new dimensions, so ``evaluate_scenario_match`` would not
count the missing dimension as UNSTATED, the overall result would be MATCHES,
and the test would fail (the offer would rank when it must not).
"""

import pytest

from cu_rates.contracts import (
    CanonicalScenarioId,
    DimensionMatch,
    MatchState,
    Placement,
    PublicationOutcome,
    ReasonCode,
    ScenarioDimension,
    evaluate_publication,
)
from cu_rates.contracts.scenarios import REQUIRED_DIMENSIONS

from .factories import (
    context,
    equity_offer,
    heloc_offer,
    matches_all,
    product,
    provenance,
    purchase_offer,
    refinance_offer,
)

R = ReasonCode


def _drop_dimension(
    matches: tuple[DimensionMatch, ...], dim: ScenarioDimension
) -> tuple[DimensionMatch, ...]:
    """Return a copy of ``matches`` with the given dimension removed."""
    return tuple(m for m in matches if m.dimension is not dim)


# ---------------------------------------------------------------------------
# PROPERTY_VALUE is now a required dimension for every canonical scenario.
# ---------------------------------------------------------------------------


def test_property_value_required_in_purchase_scenario() -> None:
    """PROPERTY_VALUE must be in REQUIRED_DIMENSIONS for purchase."""
    assert ScenarioDimension.PROPERTY_VALUE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.PURCHASE_30Y_FIXED
    ]


def test_property_value_required_in_refi_scenario() -> None:
    assert ScenarioDimension.PROPERTY_VALUE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED
    ]


def test_property_value_required_in_home_equity_scenario() -> None:
    assert ScenarioDimension.PROPERTY_VALUE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.FIXED_HOME_EQUITY_10Y
    ]


def test_property_value_required_in_heloc_scenario() -> None:
    assert ScenarioDimension.PROPERTY_VALUE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.HELOC
    ]


# ---------------------------------------------------------------------------
# AUTOPAY is now a required dimension for mortgage scenarios.
# ---------------------------------------------------------------------------


def test_autopay_required_in_purchase_scenario() -> None:
    assert ScenarioDimension.AUTOPAY in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.PURCHASE_30Y_FIXED
    ]


def test_autopay_required_in_refi_scenario() -> None:
    assert ScenarioDimension.AUTOPAY in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED
    ]


# ---------------------------------------------------------------------------
# FIRST_LIEN_BALANCE is now a required dimension for equity/HELOC scenarios.
# ---------------------------------------------------------------------------


def test_first_lien_balance_required_in_home_equity_scenario() -> None:
    assert ScenarioDimension.FIRST_LIEN_BALANCE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.FIXED_HOME_EQUITY_10Y
    ]


def test_first_lien_balance_required_in_heloc_scenario() -> None:
    assert ScenarioDimension.FIRST_LIEN_BALANCE in REQUIRED_DIMENSIONS[
        CanonicalScenarioId.HELOC
    ]


# ---------------------------------------------------------------------------
# Mutation-proof: omitting each required assumption blocks ranking.
# Without the fix, each of these would see the offer ranked (PASSED when
# RANKED) because the missing dimension was not checked.
# ---------------------------------------------------------------------------


def test_omitting_property_value_blocks_purchase_ranking() -> None:
    """An offer missing property-value evidence must not be ranked."""
    base_matches = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    reduced = _drop_dimension(base_matches, ScenarioDimension.PROPERTY_VALUE)
    offer = purchase_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_autopay_blocks_purchase_ranking() -> None:
    """An offer missing autopay evidence must not be ranked."""
    base_matches = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    reduced = _drop_dimension(base_matches, ScenarioDimension.AUTOPAY)
    offer = purchase_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_property_value_blocks_refi_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED)
    reduced = _drop_dimension(base_matches, ScenarioDimension.PROPERTY_VALUE)
    offer = refinance_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_autopay_blocks_refi_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED)
    reduced = _drop_dimension(base_matches, ScenarioDimension.AUTOPAY)
    offer = refinance_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_property_value_blocks_equity_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y)
    reduced = _drop_dimension(base_matches, ScenarioDimension.PROPERTY_VALUE)
    offer = equity_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_first_lien_balance_blocks_equity_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y)
    reduced = _drop_dimension(base_matches, ScenarioDimension.FIRST_LIEN_BALANCE)
    offer = equity_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_property_value_blocks_heloc_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.HELOC)
    reduced = _drop_dimension(base_matches, ScenarioDimension.PROPERTY_VALUE)
    offer = heloc_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


def test_omitting_first_lien_balance_blocks_heloc_ranking() -> None:
    base_matches = matches_all(CanonicalScenarioId.HELOC)
    reduced = _drop_dimension(base_matches, ScenarioDimension.FIRST_LIEN_BALANCE)
    offer = heloc_offer(scenario_matches=reduced)
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert R.SCENARIO_UNSTATED in decision.reasons
    assert decision.placement is not Placement.RANKED


# ---------------------------------------------------------------------------
# Positive case: all required assumptions present → offer is ranked.
# ---------------------------------------------------------------------------


def test_purchase_offer_with_all_assumptions_is_ranked() -> None:
    """The full matches_all set including the new dimensions allows ranking."""
    offer = purchase_offer(scenario_matches=matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED))
    decision = evaluate_publication(offer, context(offer, subject=offer))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED


def test_equity_offer_with_all_assumptions_is_ranked() -> None:
    from cu_rates.contracts import ProductFamily

    offer = equity_offer(scenario_matches=matches_all(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y))
    prod = product(family=ProductFamily.HOME_EQUITY_LOAN)
    decision = evaluate_publication(offer, context(offer, subject=offer, product=prod))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED

"""Borrower comparison scenarios and tri-state scenario matching."""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Self

from pydantic import model_validator

from cu_rates.contracts.base import (
    ContractModel,
    CreditScore,
    Days,
    Measure,
    Money,
    Months,
    Percent,
    Points,
    Text,
    VersionedModel,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    LienPosition,
    LoanPurpose,
    MatchState,
    Occupancy,
    ProductFamily,
    PropertyType,
    ScenarioDimension,
)

_D = ScenarioDimension

_MORTGAGE_DIMENSIONS = frozenset(
    {
        _D.OCCUPANCY,
        _D.PROPERTY_TYPE,
        _D.RATE_TYPE,
        _D.TERM_MONTHS,
        _D.LTV,
        _D.LOAN_AMOUNT,
        _D.CREDIT_SCORE,
        _D.LOCK_DAYS,
        _D.POINTS,
        _D.CASH_OUT,
        _D.RELATIONSHIP_DISCOUNT,
    }
)
_EQUITY_DIMENSIONS = frozenset(
    {
        _D.OCCUPANCY,
        _D.PROPERTY_TYPE,
        _D.TERM_MONTHS,
        _D.CLTV,
        _D.LOAN_AMOUNT,
        _D.CREDIT_SCORE,
        _D.RELATIONSHIP_DISCOUNT,
    }
)
_HELOC_DIMENSIONS = _EQUITY_DIMENSIONS - {_D.TERM_MONTHS}

REQUIRED_DIMENSIONS: Mapping[CanonicalScenarioId, frozenset[ScenarioDimension]] = MappingProxyType(
    {
        CanonicalScenarioId.PURCHASE_30Y_FIXED: _MORTGAGE_DIMENSIONS - {_D.CASH_OUT},
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED: _MORTGAGE_DIMENSIONS,
        CanonicalScenarioId.FIXED_HOME_EQUITY_10Y: _EQUITY_DIMENSIONS,
        CanonicalScenarioId.HELOC: _HELOC_DIMENSIONS,
    }
)


class ComparisonScenario(VersionedModel):
    """One declared borrower scenario. Exactly one of ``ltv`` (first lien) or ``cltv`` is set."""

    scenario_id: CanonicalScenarioId
    family: ProductFamily
    purpose: LoanPurpose | None = None
    occupancy: Occupancy
    property_type: PropertyType
    property_value: Money
    loan_amount: Money
    first_lien_balance: Money | None = None
    ltv: Percent | None = None
    cltv: Percent | None = None
    credit_score: CreditScore
    term_months: Months | None = None
    lock_days: Days | None = None
    points: Points | None = None
    relationship_discount_applied: bool = False
    autopay_discount_applied: bool = False

    @property
    def required_lien_position(self) -> LienPosition:
        """Fixed-equity and HELOC scenarios are second-lien (a first lien is declared);
        mortgage scenarios are first-lien."""
        if self.family is ProductFamily.MORTGAGE:
            return LienPosition.FIRST
        return LienPosition.SECOND

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.family is ProductFamily.MORTGAGE:
            if self.purpose is None:
                raise ValueError("mortgage scenarios require a purpose")
            if self.first_lien_balance is not None or self.cltv is not None:
                raise ValueError("mortgage scenarios use ltv only, with no separate first lien")
            if self.ltv is None:
                raise ValueError("mortgage scenarios require ltv")
            stated, expected = self.ltv, self.loan_amount * 100 / self.property_value
        else:
            if self.purpose is not None:
                raise ValueError("purpose applies to mortgage scenarios only")
            if self.first_lien_balance is None or self.cltv is None or self.ltv is not None:
                raise ValueError("equity scenarios require first_lien_balance and cltv, not ltv")
            stated = self.cltv
            expected = (self.first_lien_balance + self.loan_amount) * 100 / self.property_value
        if abs(stated - expected) >= Decimal("0.01"):
            raise ValueError(f"stated leverage {stated} disagrees with computed {expected:.4f}")
        return self


def _scenario(**kwargs: object) -> ComparisonScenario:
    return ComparisonScenario.model_validate(
        {
            "schema_version": "1.0",
            "occupancy": "owner_occupied",
            "property_type": "single_family",
            "property_value": "500000",
            "credit_score": 780,
            **kwargs,
        }
    )


CANONICAL_SCENARIOS: Mapping[CanonicalScenarioId, ComparisonScenario] = MappingProxyType(
    {
        CanonicalScenarioId.PURCHASE_30Y_FIXED: _scenario(
            scenario_id="purchase_30y_fixed",
            family="mortgage",
            purpose="purchase",
            loan_amount="400000",
            ltv="80",
            term_months=360,
            lock_days=30,
            points="0",
        ),
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED: _scenario(
            scenario_id="rate_term_refi_30y_fixed",
            family="mortgage",
            purpose="rate_term_refinance",
            loan_amount="400000",
            ltv="80",
            term_months=360,
            lock_days=30,
            points="0",
        ),
        CanonicalScenarioId.FIXED_HOME_EQUITY_10Y: _scenario(
            scenario_id="fixed_home_equity_10y",
            family="home_equity_loan",
            first_lien_balance="350000",
            loan_amount="50000",
            cltv="80",
            term_months=120,
        ),
        CanonicalScenarioId.HELOC: _scenario(
            scenario_id="heloc",
            family="heloc",
            first_lien_balance="350000",
            loan_amount="50000",
            cltv="80",
        ),
    }
)


class DimensionMatch(ContractModel):
    """Whether one scenario dimension matches what the source states."""

    dimension: ScenarioDimension
    state: MatchState
    source_text: Text | None = None

    @model_validator(mode="after")
    def _text_iff_stated(self) -> Self:
        stated = self.state is not MatchState.UNSTATED
        if stated and self.source_text is None:
            raise ValueError("matches/conflicts must cite the source text")
        if not stated and self.source_text is not None:
            raise ValueError("an unstated dimension cannot carry source text")
        return self


def match_numeric(
    scenario_value: Decimal,
    *,
    exact: Measure | None = None,
    lower: Measure | None = None,
    upper: Measure | None = None,
) -> MatchState:
    """Tri-state match of a scenario value against an exact value or a published range.

    Bounds are inclusive and may be open-ended. Nothing stated yields ``unstated``.
    """
    if exact is not None:
        if lower is not None or upper is not None:
            raise ValueError("pass either an exact value or a range, not both")
        return MatchState.MATCHES if scenario_value == exact else MatchState.CONFLICTS
    if lower is None and upper is None:
        return MatchState.UNSTATED
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("range lower bound exceeds upper bound")
    below = lower is not None and scenario_value < lower
    above = upper is not None and scenario_value > upper
    return MatchState.CONFLICTS if below or above else MatchState.MATCHES


def aggregate_match(states: Iterable[MatchState]) -> MatchState:
    """Conflicts dominate unstated, which dominates matches; no input is unstated."""
    seen = set(states)
    if MatchState.CONFLICTS in seen:
        return MatchState.CONFLICTS
    if MatchState.UNSTATED in seen or not seen:
        return MatchState.UNSTATED
    return MatchState.MATCHES


def evaluate_scenario_match(
    scenario_id: CanonicalScenarioId, matches: Sequence[DimensionMatch]
) -> MatchState:
    """Aggregate over every dimension the scenario requires; missing dimensions are unstated."""
    by_dimension = {m.dimension: m.state for m in matches}
    return aggregate_match(
        by_dimension.get(dimension, MatchState.UNSTATED)
        for dimension in REQUIRED_DIMENSIONS[scenario_id]
    )

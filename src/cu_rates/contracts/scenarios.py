"""Borrower comparison scenarios and tri-state scenario matching."""

import re
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
    contract_arithmetic,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    LienPosition,
    LoanPurpose,
    MatchState,
    Occupancy,
    ProductFamily,
    PropertyType,
    RateType,
    ScenarioDimension,
)
from cu_rates.contracts.evidence import Provenance

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
        _D.PROPERTY_VALUE,
        _D.AUTOPAY,
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
        _D.PROPERTY_VALUE,
        _D.FIRST_LIEN_BALANCE,
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
            if self.purpose in {None, LoanPurpose.UNSPECIFIED}:
                raise ValueError("mortgage scenarios require a stated purpose")
            if self.first_lien_balance is not None or self.cltv is not None:
                raise ValueError("mortgage scenarios use ltv only, with no separate first lien")
            if self.ltv is None:
                raise ValueError("mortgage scenarios require ltv")
            with contract_arithmetic():
                stated, expected = self.ltv, self.loan_amount * 100 / self.property_value
        else:
            if self.purpose is not None:
                raise ValueError("purpose applies to mortgage scenarios only")
            if self.first_lien_balance is None or self.cltv is None or self.ltv is not None:
                raise ValueError("equity scenarios require first_lien_balance and cltv, not ltv")
            stated = self.cltv
            with contract_arithmetic():
                expected = (self.first_lien_balance + self.loan_amount) * 100 / self.property_value
        with contract_arithmetic():
            off_by = abs(stated - expected)
        if off_by >= Decimal("0.01"):
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


class StatedBound(ContractModel):
    """A typed source statement: an exact value, an inclusive range, or a token.

    ``source_text`` on a match is the excerpt. This is the value publication compares
    to the canonical scenario. A token and a number are never both set.
    """

    exact: Measure | None = None
    lower: Measure | None = None
    upper: Measure | None = None
    token: Text | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        numeric = self.exact is not None or self.lower is not None or self.upper is not None
        if self.token is not None and numeric:
            raise ValueError("a stated dimension is a token or a number, not both")
        if self.exact is not None and (self.lower is not None or self.upper is not None):
            raise ValueError("pass either an exact value or a range, not both")
        if self.token is None and not numeric:
            raise ValueError("a stated dimension needs a token, exact value, or range")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("range lower bound exceeds upper bound")
        return self


class DimensionMatch(ContractModel):
    """Whether one scenario dimension matches what the source states.

    A match or conflict still quotes ``source_text``. Publication does not trust that
    prose: a claimed match counts only when ``stated`` is typed and ``provenance``
    locates it. Both fields are optional so an unstated dimension and older records
    still load.
    """

    dimension: ScenarioDimension
    state: MatchState
    source_text: Text | None = None
    stated: StatedBound | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _text_iff_stated(self) -> Self:
        stated = self.state is not MatchState.UNSTATED
        if stated and self.source_text is None:
            raise ValueError("matches/conflicts must cite the source text")
        if not stated and self.source_text is not None:
            raise ValueError("an unstated dimension cannot carry source text")
        return self


_NUM = r"[0-9]+(?:\.[0-9]+)?"
_MONEY = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_TERM = re.compile(
    r"([0-9]{1,3}|ten|fifteen|twenty|thirty)[ -](years?|months?)"
    r"(?: fixed term)?(?: \(([0-9]{1,3}) months?\))?"
)
_EXPLICIT_TOKENS: Mapping[ScenarioDimension, Mapping[str, str]] = {
    _D.OCCUPANCY: {
        "owner-occupied primary residence": "owner_occupied",
        "owner-occupied single-family primary residence": "owner_occupied",
    },
    _D.PROPERTY_TYPE: {"single-family": "single_family", "single family": "single_family"},
    _D.RATE_TYPE: {
        "fixed": "fixed",
        "fixed rate": "fixed",
        "fixed-rate": "fixed",
        "conventional conforming fixed purchase": "fixed",
    },
    _D.CASH_OUT: {"no cash out": "no", "no cash-out": "no", "cash out": "yes"},
    _D.RELATIONSHIP_DISCOUNT: {
        "no relationship discount": "no",
        "relationship discount required": "yes",
    },
    _D.AUTOPAY: {
        "no autopay discount": "no",
        "no autopay": "no",
        "autopay discount required": "yes",
        "autopay required": "yes",
    },
}


def _dimension_number(text: str, dimension: ScenarioDimension) -> Decimal | None:
    """Parse an exact excerpt's dimension-specific unit and physically valid bounds."""
    if dimension is _D.TERM_MONTHS:
        term = _TERM.fullmatch(text)
        if term is None:
            return None
        quantity, unit, equivalent = term.groups()
        count = (
            int(quantity)
            if quantity.isdigit()
            else {"ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30}[quantity]
        )
        months = count * 12 if unit.startswith("year") else count
        if not 1 <= months <= 720 or (equivalent is not None and int(equivalent) != months):
            return None
        return Decimal(months)
    patterns = {
        _D.LTV: (rf"({_NUM})\s*%", 0, 100),
        _D.CLTV: (rf"({_NUM})\s*%", 0, 100),
        _D.LOAN_AMOUNT: (rf"(?:\$\s*|usd\s+)({_MONEY})", 0, 999_999_999_999),
        _D.CREDIT_SCORE: (r"([0-9]{3})", 300, 850),
        _D.LOCK_DAYS: (r"([0-9]{1,3})[ -]days?(?: lock)?", 1, 366),
        _D.POINTS: (rf"({_NUM})(?:\s*%|\s+points?)?", 0, 10),
        _D.PROPERTY_VALUE: (rf"(?:\$\s*|usd\s+)({_MONEY})", 0, 999_999_999_999),
        _D.FIRST_LIEN_BALANCE: (rf"(?:\$\s*|usd\s+)({_MONEY})", 0, 999_999_999_999),
    }
    spec = patterns.get(dimension)
    if spec is None:
        return None
    pattern, lower, upper = spec
    match = re.fullmatch(pattern, text)
    if match is None:
        return None
    value = Decimal(match[1].replace(",", ""))
    fractional = match[1].partition(".")[2].rstrip("0")
    if dimension is _D.LOAN_AMOUNT and (value == 0 or len(fractional) > 2):
        return None
    return value if lower <= value <= upper else None


def statement_matches_excerpt(
    dimension: ScenarioDimension, stated: StatedBound, excerpt: str
) -> bool:
    """Check units, domain, and literal agreement before treating a statement as evidence."""
    text = " ".join(excerpt.lower().split())
    if stated.token is not None:
        return _EXPLICIT_TOKENS.get(dimension, {}).get(text) == stated.token
    if stated.exact is not None:
        return _dimension_number(text, dimension) == stated.exact
    if stated.lower is not None and stated.upper is not None:
        match = re.fullmatch(r"(.+?)(?:\s+to\s+|\s*[\u2013\u2014]\s*)(.+)", text)
        return match is not None and (
            _dimension_number(match[1], dimension),
            _dimension_number(match[2], dimension),
        ) == (stated.lower, stated.upper)
    if stated.lower is not None:
        match = re.fullmatch(r"(?:>=|at least\s+)\s*(.+)", text)
        return match is not None and _dimension_number(match[1], dimension) == stated.lower
    match = re.fullmatch(r"(?:<=|up to\s+)\s*(.+)", text)
    return (
        stated.upper is not None
        and match is not None
        and _dimension_number(match[1], dimension) == stated.upper
    )


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


def expected_dimension_number(
    scenario: ComparisonScenario, dimension: ScenarioDimension
) -> Decimal | None:
    """The canonical numeric value for ``dimension``, if the scenario states one."""
    raw: object
    if dimension is _D.TERM_MONTHS:
        raw = scenario.term_months
    elif dimension is _D.LTV:
        raw = scenario.ltv
    elif dimension is _D.CLTV:
        raw = scenario.cltv
    elif dimension is _D.LOAN_AMOUNT:
        raw = scenario.loan_amount
    elif dimension is _D.CREDIT_SCORE:
        raw = scenario.credit_score
    elif dimension is _D.LOCK_DAYS:
        raw = scenario.lock_days
    elif dimension is _D.POINTS:
        raw = scenario.points
    elif dimension is _D.PROPERTY_VALUE:
        raw = scenario.property_value
    elif dimension is _D.FIRST_LIEN_BALANCE:
        raw = scenario.first_lien_balance
    else:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    return None


def expected_dimension_token(
    scenario: ComparisonScenario, dimension: ScenarioDimension
) -> str | None:
    """The canonical token for a non-numeric dimension. Cash-out and discounts use yes/no."""
    if dimension is _D.OCCUPANCY:
        return scenario.occupancy.value
    if dimension is _D.PROPERTY_TYPE:
        return scenario.property_type.value
    if dimension is _D.RATE_TYPE:
        fixed = {
            CanonicalScenarioId.PURCHASE_30Y_FIXED,
            CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED,
        }
        return RateType.FIXED.value if scenario.scenario_id in fixed else None
    if dimension is _D.CASH_OUT:
        return "no"
    if dimension is _D.RELATIONSHIP_DISCOUNT:
        return "yes" if scenario.relationship_discount_applied else "no"
    if dimension is _D.AUTOPAY:
        return "yes" if scenario.autopay_discount_applied else "no"
    return None


def matching_statement(scenario: ComparisonScenario, dimension: ScenarioDimension) -> StatedBound:
    """The typed statement that exactly matches this canonical dimension."""
    number = expected_dimension_number(scenario, dimension)
    if number is not None:
        return StatedBound(exact=number)
    token = expected_dimension_token(scenario, dimension)
    if token is None:
        raise ValueError(f"{dimension.value} has no canonical statement on {scenario.scenario_id}")
    return StatedBound(token=token)


def computed_match_state(
    scenario: ComparisonScenario, stated: StatedBound, dimension: ScenarioDimension
) -> MatchState:
    """Match ``stated`` against the scenario. Unrelated excerpt text is not an input."""
    if stated.token is not None:
        expected = expected_dimension_token(scenario, dimension)
        if expected is None:
            return MatchState.UNSTATED
        return MatchState.MATCHES if stated.token == expected else MatchState.CONFLICTS
    number = expected_dimension_number(scenario, dimension)
    if number is None:
        return MatchState.UNSTATED
    return match_numeric(number, exact=stated.exact, lower=stated.lower, upper=stated.upper)


def evaluate_scenario_match(
    scenario_id: CanonicalScenarioId, matches: Sequence[DimensionMatch]
) -> MatchState:
    """Aggregate over every dimension the scenario requires; missing dimensions are unstated."""
    by_dimension = {m.dimension: m.state for m in matches}
    return aggregate_match(
        by_dimension.get(dimension, MatchState.UNSTATED)
        for dimension in REQUIRED_DIMENSIONS[scenario_id]
    )

"""Normalized offers: shared base with distinct typed product details."""

from collections.abc import Iterator, Mapping
from decimal import Decimal
from operator import attrgetter
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from cu_rates.contracts.base import (
    CharterNumber,
    ContractModel,
    Days,
    FieldPath,
    Identifier,
    Measure,
    Money,
    Months,
    Percent,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    LienPosition,
    LoanPurpose,
    MatchState,
    OfferStatus,
    PostIntroState,
    ProductFamily,
    RateType,
    TierDimension,
)
from cu_rates.contracts.evidence import (
    FieldEvidence,
    Provenance,
    SourcedMargin,
    SourcedPoints,
    SourcedRate,
)
from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, DimensionMatch

_EVER_PUBLISHED = frozenset({OfferStatus.PUBLISHED, OfferStatus.WITHDRAWN, OfferStatus.SUPERSEDED})
HELOC_POST_INTRO_RULE_ID = "heloc.post_intro.max_index_plus_margin_floor.v1"


class OfferCondition(ContractModel):
    """A qualifier on the advertised value: 'as low as', discount, tier, or requirement."""

    condition_id: Identifier
    kind: ConditionKind
    description: Text
    provenance: Provenance
    scenario_state: MatchState


class TierBound(ContractModel):
    """Inclusive range on one dimension; a missing end is open (unbounded)."""

    dimension: TierDimension
    lower: Measure | None = None
    upper: Measure | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("a tier bound needs at least one end")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("tier lower bound exceeds upper bound")
        return self

    def contains(self, value: Decimal) -> bool:
        return (self.lower is None or value >= self.lower) and (
            self.upper is None or value <= self.upper
        )

    def overlaps(self, other: Self) -> bool:
        left = other.upper is None or self.lower is None or self.lower <= other.upper
        right = self.upper is None or other.lower is None or other.lower <= self.upper
        return left and right


def _sort_bounds(bounds: tuple[TierBound, ...]) -> tuple[TierBound, ...]:
    return tuple(sorted(bounds, key=attrgetter("dimension")))


class RateTier(ContractModel):
    """A published rate for a cell of the grid: one inclusive range per constrained dimension.

    A one-dimensional schedule (LTV bands) has one bound per tier; a grid (LTV by credit
    score) has one bound per axis. A tier covers a borrower only if *every* bound holds.
    """

    tier_id: Identifier
    bounds: Annotated[tuple[TierBound, ...], Field(min_length=1), AfterValidator(_sort_bounds)]
    note_rate: SourcedRate | None = None
    apr: SourcedRate | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        dimensions = [b.dimension for b in self.bounds]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("a tier may bound each dimension only once")
        if self.note_rate is None and self.apr is None:
            raise ValueError("a tier needs a note rate or an APR")
        return self

    def contains(self, values: Mapping[TierDimension, Decimal | None]) -> bool:
        """True only when every bounded dimension has a known value inside its range."""
        for bound in self.bounds:
            value = values.get(bound.dimension)
            if value is None or not bound.contains(value):
                return False
        return True

    def overlaps(self, other: "RateTier") -> bool:
        """Two tiers overlap when some borrower could satisfy both.

        Dimensions bounded by only one tier are unbounded for the other, so they never
        separate the two; every shared dimension must have intersecting ranges.
        """
        theirs = {b.dimension: b for b in other.bounds}
        return all(
            mine.overlaps(theirs[mine.dimension])
            for mine in self.bounds
            if mine.dimension in theirs
        )


class ArmTerms(ContractModel):
    """Adjustable-rate terms. Anything the source does not state stays null."""

    initial_period_months: Months | None = None
    adjustment_interval_months: Months | None = None
    index_name: Text | None = None
    margin: SourcedMargin | None = None
    initial_cap: Percent | None = None
    periodic_cap: Percent | None = None
    lifetime_cap: Percent | None = None

    @property
    def is_complete(self) -> bool:
        return None not in (
            self.initial_period_months,
            self.adjustment_interval_months,
            self.index_name,
            self.margin,
            self.initial_cap,
            self.periodic_cap,
            self.lifetime_cap,
        )


RateItems = tuple[tuple[str, SourcedRate | SourcedMargin], ...]

_ARM_SCALARS = (
    "initial_period_months",
    "adjustment_interval_months",
    "index_name",
    "initial_cap",
    "periodic_cap",
    "lifetime_cap",
)


def _present(prefix: str, model: ContractModel, names: tuple[str, ...]) -> frozenset[str]:
    """Paths of the named fields that the source actually stated (non-null)."""
    return frozenset(f"{prefix}.{n}" for n in names if getattr(model, n) is not None)


class _MortgageDetails(ContractModel):
    term_months: Months
    rate_type: RateType
    arm: ArmTerms | None = None
    lock_days: Days | None = None

    @model_validator(mode="after")
    def _arm_iff_adjustable(self) -> Self:
        adjustable = self.rate_type is RateType.ADJUSTABLE
        if adjustable and self.arm is None:
            raise ValueError("adjustable mortgages require ARM terms (fields may be null)")
        if not adjustable and self.arm is not None:
            raise ValueError("fixed-rate mortgages cannot carry ARM terms")
        return self

    def rate_items(self) -> RateItems:
        margin = self.arm.margin if self.arm is not None else None
        return (("details.arm.margin", margin),) if margin is not None else ()

    def material_paths(self) -> frozenset[str]:
        paths = _present("details", self, ("term_months", "rate_type", "lock_days"))
        if self.arm is not None:
            paths |= _present("details.arm", self.arm, _ARM_SCALARS)
        return paths


class PurchaseMortgageDetails(_MortgageDetails):
    kind: Literal["purchase_mortgage"] = "purchase_mortgage"

    @property
    def purpose(self) -> LoanPurpose:
        return LoanPurpose.PURCHASE


class RefinanceMortgageDetails(_MortgageDetails):
    kind: Literal["refinance_mortgage"] = "refinance_mortgage"
    purpose: LoanPurpose

    @model_validator(mode="after")
    def _refinance_purpose(self) -> Self:
        if self.purpose is LoanPurpose.PURCHASE:
            raise ValueError("a refinance offer cannot have purchase purpose")
        return self

    def material_paths(self) -> frozenset[str]:
        return super().material_paths() | {"details.purpose"}


class FixedEquityDetails(ContractModel):
    """Closed-end fixed-rate home-equity loan."""

    kind: Literal["fixed_equity"] = "fixed_equity"
    term_months: Months
    lien_position: LienPosition
    max_cltv: Percent | None = None
    min_amount: Money | None = None
    max_amount: Money | None = None

    @model_validator(mode="after")
    def _amount_order(self) -> Self:
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("min_amount exceeds max_amount")
        return self

    def rate_items(self) -> RateItems:
        return ()

    def material_paths(self) -> frozenset[str]:
        return _present(
            "details",
            self,
            ("term_months", "lien_position", "max_cltv", "min_amount", "max_amount"),
        )


class IntroRate(ContractModel):
    rate: SourcedRate
    duration_months: Months | None = None


class HelocPostIntro(ContractModel):
    """Post-introductory variable rate: stated, derived by rule, or incomplete."""

    state: PostIntroState
    rate: Percent | None = None
    rule_id: Identifier | None = None
    missing_inputs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.state is PostIntroState.INCOMPLETE:
            if self.rate is not None or not self.missing_inputs:
                raise ValueError("incomplete post-intro needs missing inputs and no rate")
            return self
        if self.rate is None or self.missing_inputs:
            raise ValueError("stated/derived post-intro needs a rate and no missing inputs")
        if (self.state is PostIntroState.DERIVED) != (self.rule_id is not None):
            raise ValueError("a derived rate must cite its rule id; a stated rate must not")
        return self


class HelocDetails(ContractModel):
    """Open-end line. Intro rate is distinct from the post-intro variable rate."""

    kind: Literal["heloc"] = "heloc"
    intro: IntroRate | None = None
    index_name: Text | None = None
    index_rate: SourcedRate | None = None
    margin: SourcedMargin | None = None
    floor: SourcedRate | None = None
    ceiling: SourcedRate | None = None
    stated_post_intro_apr: SourcedRate | None = None
    draw_period_months: Months | None = None
    repayment_period_months: Months | None = None
    lien_position: LienPosition
    max_cltv: Percent | None = None

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> Self:
        if (
            self.floor is not None
            and self.ceiling is not None
            and self.floor.value > self.ceiling.value
        ):
            raise ValueError("floor exceeds ceiling")
        return self

    def _derivation(self) -> tuple[Decimal | None, tuple[str, ...]]:
        """(derived rate, missing inputs) from explicit components only; never guessed.

        ``max(index + margin, floor)``, capped by the ceiling when one is stated.
        """
        index, margin, floor = self.index_rate, self.margin, self.floor
        missing = tuple(
            name
            for name, present in (
                ("index_name", self.index_name is not None),
                ("index_rate", index is not None),
                ("margin", margin is not None),
                ("floor", floor is not None),
            )
            if not present
        )
        if missing or index is None or margin is None or floor is None:
            return None, missing
        rate = max(index.value + margin.value, floor.value)
        if self.ceiling is not None:
            rate = min(rate, self.ceiling.value)
        if rate > 100:
            return None, ("derived_rate_out_of_range",)
        return rate, ()

    def post_intro(self) -> HelocPostIntro:
        """Post-intro rate: the stated APR, else derived from components, else incomplete."""
        if self.stated_post_intro_apr is not None:
            return HelocPostIntro(
                state=PostIntroState.STATED, rate=self.stated_post_intro_apr.value
            )
        rate, missing = self._derivation()
        if rate is None:
            return HelocPostIntro(state=PostIntroState.INCOMPLETE, missing_inputs=missing)
        return HelocPostIntro(
            state=PostIntroState.DERIVED, rate=rate, rule_id=HELOC_POST_INTRO_RULE_ID
        )

    def post_intro_conflicts(self) -> bool:
        """A stated post-intro APR that disagrees with fully stated components.

        Both source facts are retained; the disagreement itself is the finding. Exact
        decimal equality is required, so a rounding difference also surfaces.
        """
        if self.stated_post_intro_apr is None:
            return False
        derived, _ = self._derivation()
        return derived is not None and derived != self.stated_post_intro_apr.value

    def rate_items(self) -> RateItems:
        candidates = (
            ("details.intro.rate", self.intro.rate if self.intro else None),
            ("details.index_rate", self.index_rate),
            ("details.margin", self.margin),
            ("details.floor", self.floor),
            ("details.ceiling", self.ceiling),
            ("details.stated_post_intro_apr", self.stated_post_intro_apr),
        )
        return tuple((path, value) for path, value in candidates if value is not None)

    def material_paths(self) -> frozenset[str]:
        paths = _present(
            "details",
            self,
            (
                "index_name",
                "draw_period_months",
                "repayment_period_months",
                "lien_position",
                "max_cltv",
            ),
        )
        if self.intro is not None and self.intro.duration_months is not None:
            paths |= {"details.intro.duration_months"}
        return paths


OfferDetails = Annotated[
    PurchaseMortgageDetails | RefinanceMortgageDetails | FixedEquityDetails | HelocDetails,
    Field(discriminator="kind"),
]


class NormalizedOffer(VersionedModel):
    """One normalized, evidence-backed advertised offer.

    ``note_rate``/``apr`` are the advertised headline values. For HELOCs the introductory
    rate lives in ``details.intro`` and the post-intro rate in ``details.post_intro()``.
    ``points`` null means unknown; an explicit zero is a sourced statement.
    """

    offer_id: Identifier
    institution_charter: CharterNumber
    product_id: Identifier
    source_id: Identifier
    status: OfferStatus
    details: OfferDetails
    scenario_id: CanonicalScenarioId | None = None
    scenario_matches: tuple[DimensionMatch, ...] = ()
    note_rate: SourcedRate | None = None
    apr: SourcedRate | None = None
    points: SourcedPoints | None = None
    conditions: tuple[OfferCondition, ...] = ()
    tiers: tuple[RateTier, ...] = ()
    headline_is_as_low_as: bool = False
    effective_at: UtcDatetime | None = None
    observed_at: UtcDatetime
    published_at: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    supporting_evidence: Annotated[tuple[Provenance, ...], Field(min_length=1)]
    field_provenance: tuple[FieldEvidence, ...] = ()

    @property
    def family(self) -> ProductFamily:
        if isinstance(self.details, PurchaseMortgageDetails | RefinanceMortgageDetails):
            return ProductFamily.MORTGAGE
        if isinstance(self.details, FixedEquityDetails):
            return ProductFamily.HOME_EQUITY_LOAN
        return ProductFamily.HELOC

    @property
    def purpose(self) -> LoanPurpose | None:
        if isinstance(self.details, PurchaseMortgageDetails | RefinanceMortgageDetails):
            return self.details.purpose
        return None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        self._check_rates_and_times()
        self._check_scenario()
        self._check_collections()
        self._check_field_provenance()
        return self

    def _check_rates_and_times(self) -> None:
        has_rate = (
            self.note_rate is not None
            or self.apr is not None
            or any(t.note_rate or t.apr for t in self.tiers)
            or bool(self.details.rate_items())
        )
        if not has_rate:
            raise ValueError("an offer must state at least one rate")
        was_published = self.status in _EVER_PUBLISHED
        if self.published_at is None and self.status is OfferStatus.PUBLISHED:
            raise ValueError("a published offer requires published_at")
        if self.published_at is not None and not was_published:
            raise ValueError("only a published, withdrawn, or superseded offer has published_at")
        if self.published_at is not None and self.published_at < self.observed_at:
            raise ValueError("published_at cannot precede observed_at")
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValueError("valid_until must follow observed_at")

    def _check_scenario(self) -> None:
        if self.scenario_id is None:
            if self.scenario_matches:
                raise ValueError("scenario_matches require a scenario_id")
            return
        scenario = CANONICAL_SCENARIOS[self.scenario_id]
        if scenario.family is not self.family or scenario.purpose != self.purpose:
            raise ValueError("offer family/purpose does not belong to the declared scenario")
        dimensions = [m.dimension for m in self.scenario_matches]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("duplicate scenario dimension in scenario_matches")

    def _check_collections(self) -> None:
        for label, ids in (
            ("condition_id", [c.condition_id for c in self.conditions]),
            ("tier_id", [t.tier_id for t in self.tiers]),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {label}")
        if self.headline_is_as_low_as and not any(
            c.kind is ConditionKind.AS_LOW_AS for c in self.conditions
        ):
            raise ValueError("an 'as low as' headline requires an as_low_as condition")
        for index, tier in enumerate(self.tiers):
            if any(tier.overlaps(other) for other in self.tiers[index + 1 :]):
                raise ValueError(f"tier {tier.tier_id} overlaps another tier")

    def _check_field_provenance(self) -> None:
        paths = [e.path for e in self.field_provenance]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate field_provenance path")
        unknown = set(paths) - self.material_paths()
        if unknown:
            raise ValueError(f"field_provenance names unknown paths: {sorted(unknown)}")

    def material_paths(self) -> frozenset[FieldPath]:
        """Every publicly material non-rate field that needs a field-level evidence mapping.

        Sourced rates and points carry their own provenance inline, so they are not listed.
        Scenario matches carry their own stated value and locator; publication recomputes
        the match and does not treat the excerpt alone as evidence.
        """
        paths = set(self.details.material_paths())
        paths |= {f"conditions.{c.condition_id}" for c in self.conditions}
        paths |= {f"tiers.{t.tier_id}.bounds" for t in self.tiers}
        return frozenset(paths)

    def rate_items(self) -> RateItems:
        """Every sourced rate magnitude with its stable semantic path (points excluded)."""
        items: list[tuple[str, SourcedRate | SourcedMargin]] = []
        for name in ("note_rate", "apr"):
            value = getattr(self, name)
            if value is not None:
                items.append((name, value))
        for tier in self.tiers:
            for name in ("note_rate", "apr"):
                value = getattr(tier, name)
                if value is not None:
                    items.append((f"tiers.{tier.tier_id}.{name}", value))
        items.extend(self.details.rate_items())
        return tuple(items)

    def rate_values(self) -> Mapping[str, Decimal]:
        """Path -> value for every sourced rate, plus the computed HELOC post-intro rate."""
        values = {path: item.value for path, item in self.rate_items()}
        if isinstance(self.details, HelocDetails):
            post_intro = self.details.post_intro().rate
            if post_intro is not None:
                values["details.post_intro_rate"] = post_intro
        return values

    def iter_provenance(self) -> Iterator[Provenance]:
        """Every provenance record backing a value or condition on this offer."""
        yield from self.supporting_evidence
        for _, item in self.rate_items():
            yield item.provenance
        if self.points is not None:
            yield self.points.provenance
        for condition in self.conditions:
            yield condition.provenance
        for evidence in self.field_provenance:
            yield evidence.provenance

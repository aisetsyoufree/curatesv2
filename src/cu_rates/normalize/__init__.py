"""Offline normalization of one adapter-grouped rate row into a candidate.

Retain the result's traces with the canonical offer: the offer alone cannot
encode why a nullable field is empty or link evidence to a scenario dimension.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from typing import cast

from pydantic import ValidationError

from cu_rates.contracts.base import (
    CONTRACT_VERSION,
    CharterNumber,
    ContractModel,
    Identifier,
    UtcDatetime,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    LienPosition,
    LoanPurpose,
    MatchState,
    OfferStatus,
    OutOfScopeProduct,
    ProductFamily,
    RateAvailability,
    RateType,
    ScenarioDimension,
)
from cu_rates.contracts.evidence import (
    FieldEvidence,
    Provenance,
    SourcedMargin,
    SourcedPoints,
    SourcedRate,
)
from cu_rates.contracts.offers import HelocDetails, NormalizedOffer, OfferCondition
from cu_rates.contracts.scenarios import (
    CANONICAL_SCENARIOS,
    REQUIRED_DIMENSIONS,
    ComparisonScenario,
    DimensionMatch,
    aggregate_match,
)
from cu_rates.contracts.sources import ExtractedCandidate
from cu_rates.normalize import _values

NORMALIZATION_VERSION = "t044.1"
_D = ScenarioDimension
_MORTGAGE = frozenset({"details.term_months", "details.rate_type", "details.lock_days"})
_ARM = frozenset(
    f"details.arm.{name}"
    for name in (
        "initial_period_months",
        "adjustment_interval_months",
        "index_name",
        "margin",
        "initial_cap",
        "periodic_cap",
        "lifetime_cap",
    )
)
_EQUITY = frozenset({"details.lien_position", "details.max_cltv"})
_FIXED_EQUITY = _EQUITY | {"details.term_months", "details.min_amount", "details.max_amount"}
_HELOC = _EQUITY | {
    f"details.{name}"
    for name in (
        "intro.rate",
        "intro.duration_months",
        "index_name",
        "index_rate",
        "margin",
        "floor",
        "ceiling",
        "stated_post_intro_apr",
        "draw_period_months",
        "repayment_period_months",
    )
}
_INLINE_RATES = frozenset(
    {
        "note_rate",
        "apr",
        "details.intro.rate",
        "details.index_rate",
        "details.floor",
        "details.ceiling",
        "details.stated_post_intro_apr",
    }
)
_TOP_LEVEL_RATES = frozenset({"note_rate", "apr", "points"})
_EXTRA_STATEMENTS = frozenset({"property_value", "first_lien_balance", "autopay_discount"})
_EQUITY_QUALIFIERS = frozenset({"lien_position", "max_cltv", "min_amount", "max_amount"})
# Equity qualifiers have no contract dimension; every canonical assumption does.
_SUPPLEMENTAL = _EQUITY_QUALIFIERS
_DIMENSIONS = frozenset(item.value for item in ScenarioDimension) | _EXTRA_STATEMENTS
_DIRECT: dict[str, str] = {
    _D.TERM_MONTHS: "details.term_months",
    _D.RATE_TYPE: "details.rate_type",
    _D.LOCK_DAYS: "details.lock_days",
    _D.POINTS: "points",
}
_FAMILY_ALIASES: dict[str, ProductFamily | OutOfScopeProduct] = {
    "conventional conforming fixed purchase": ProductFamily.MORTGAGE,
    "first mortgage": ProductFamily.MORTGAGE,
    "closed-end fixed second mortgage": ProductFamily.HOME_EQUITY_LOAN,
    "new vehicle loan": OutOfScopeProduct.VEHICLE,
    "used vehicle loan": OutOfScopeProduct.VEHICLE,
    "unsecured personal loan": OutOfScopeProduct.UNSECURED_PERSONAL,
}
_MARGIN_PATHS = frozenset({"details.margin", "details.arm.margin"})

type Value = str | int | bool | Decimal | datetime | _values.Range


class FieldState(StrEnum):
    MISSING = "missing"  # the adapter supplied no record
    NULL = "null"  # explicit null token; no value to normalize
    UNKNOWN = "unknown"  # source explicitly says it is unknown/not stated
    NOT_APPLICABLE = "not_applicable"
    STATED = "stated"  # includes explicit numeric zero


_EMPTY = {
    "null": FieldState.NULL,
    "unknown": FieldState.UNKNOWN,
    "not stated": FieldState.UNKNOWN,
    "not disclosed": FieldState.UNKNOWN,
    "n/a": FieldState.NOT_APPLICABLE,
    "not applicable": FieldState.NOT_APPLICABLE,
    "not_applicable": FieldState.NOT_APPLICABLE,
}


class NoOfferReason(StrEnum):
    NO_CANDIDATES = "no_candidates"
    OUT_OF_SCOPE = "out_of_scope"
    NON_PUBLIC_RATE = "non_public_rate"
    PARTNER_LINK = "partner_link"
    NO_RATE = "no_rate"


class NormalizationError(ValueError):
    """A rejected row, naming the path and reason instead of choosing a value."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class OfferContext(ContractModel):
    """Caller-bound identity and observation; comparison_target is not source evidence."""

    offer_id: Identifier
    institution_charter: CharterNumber
    product_id: Identifier
    source_id: Identifier
    observed_at: UtcDatetime
    comparison_target: CanonicalScenarioId | None = None


@dataclass(frozen=True)
class FieldTrace:
    path: str
    state: FieldState
    value: Value | None
    candidate: ExtractedCandidate
    provenance: Provenance


@dataclass(frozen=True)
class ScenarioCheck:
    dimension: str
    state: MatchState
    provenance: Provenance | None


@dataclass(frozen=True)
class NormalizationResult:
    offer: NormalizedOffer | None = None
    no_offer_reason: NoOfferReason | None = None
    traces: tuple[FieldTrace, ...] = ()
    scenario_checks: tuple[ScenarioCheck, ...] = ()
    scenario_state: MatchState | None = None

    def field_state(self, path: str) -> FieldState:
        return next(
            (trace.state for trace in self.traces if trace.path == path), FieldState.MISSING
        )


def _provenance(original: Provenance, rule: str) -> Provenance:
    return original.model_copy(
        update={
            "extraction": original.extraction.model_copy(
                update={"normalization_version": NORMALIZATION_VERSION, "rule_id": rule}
            )
        }
    )


def _trace(candidate: ExtractedCandidate, path: str, value: Value | None, rule: str) -> FieldTrace:
    state = _EMPTY.get(candidate.raw_value.lower(), FieldState.STATED)
    return FieldTrace(path, state, value, candidate, _provenance(candidate.provenance, rule))


def _collect(candidates: Iterable[ExtractedCandidate]) -> dict[str, ExtractedCandidate]:
    inputs: dict[str, ExtractedCandidate] = {}
    ids: set[str] = set()
    run: str | None = None
    for candidate in candidates:
        path = candidate.proposed_path
        if path is None:
            raise NormalizationError(candidate.candidate_id, "missing proposed path")
        path = "details.purpose" if path == "purpose" else path
        if path in inputs or candidate.candidate_id in ids:
            raise NormalizationError(path, "duplicate candidate path or identifier")
        if run is not None and run != candidate.extraction_run_id:
            raise NormalizationError(path, "a row cannot mix extraction runs")
        if candidate.raw_value != candidate.provenance.source_text:
            raise NormalizationError(path, "raw value must equal the cited source fragment")
        run = candidate.extraction_run_id
        ids.add(candidate.candidate_id)
        inputs[path] = candidate
    return dict(sorted(inputs.items()))


def _family(text: str) -> ProductFamily | OutOfScopeProduct:
    word = text.lower()
    if word in _FAMILY_ALIASES:
        return _FAMILY_ALIASES[word]
    try:
        return ProductFamily(word)
    except ValueError:
        return OutOfScopeProduct(word)


def _purpose(text: str) -> LoanPurpose:
    word = text.lower()
    if word == "conventional conforming fixed purchase":
        return LoanPurpose.PURCHASE
    return LoanPurpose(word)


def _availability(text: str) -> RateAvailability:
    if text.lower().rstrip(".") in (
        "call for a quote",
        "call for a quote. this page does not advertise a first-mortgage note rate or apr",
    ):
        return RateAvailability.QUOTE_ONLY
    return RateAvailability(text.lower())


def _date(text: str) -> datetime:
    timestamp = datetime.fromisoformat(text)
    if timestamp.utcoffset() is None:
        raise ValueError("an effective time requires an explicit UTC offset")
    return timestamp.astimezone(UTC)


def _parse(path: str, text: str) -> tuple[Value | None, str]:
    if path == "product_family":
        return _family(text), "t044.family.v1"
    if path == "details.purpose":
        return _purpose(text), "t044.purpose.v1"
    if path == "rate_availability":
        return _availability(text), "t044.availability.v1"
    if path == "partner_link":
        return text, "t044.non-rate-link.v1"
    if path == "effective_at":
        return _date(text), "t044.iso-utc.v1"
    if path.startswith("scenario."):
        return _values.comparison_statement(
            path.removeprefix("scenario."), text
        ), "t044.statement.v1"
    if path.endswith("_months"):
        return _values.months(text), "t044.term-months.v1"
    if path == "details.lock_days":
        return _values.days(text), "t044.lock-days.v1"
    if path == "details.lien_position":
        return _values.lien(text), "t044.lien.v1"
    if path == "details.rate_type":
        return _values.rate_type(text), "t044.rate-type.v1"
    if path.endswith("index_name"):
        return text, "t044.literal.v1"
    if path in _MARGIN_PATHS:
        return _values.margin(text), "t044.signed-percent.v1"
    if path in {"details.min_amount", "details.max_amount"}:
        return _values.money(text), "t044.money.v1"
    if path == "points":
        return _values.points(text), "t044.points.v1"
    return _values.percent(text), "t044.percent.v1"


def _comparison_target(scenario: ComparisonScenario, dimension: str) -> Value | None:
    match dimension:
        case _D.RATE_TYPE:
            return RateType.FIXED if scenario.family is ProductFamily.MORTGAGE else None
        case _D.CASH_OUT:
            return False if scenario.purpose is LoanPurpose.RATE_TERM_REFINANCE else None
        case _D.RELATIONSHIP_DISCOUNT:
            return scenario.relationship_discount_applied
        case "autopay_discount":
            return scenario.autopay_discount_applied
        case _:
            return cast("Value | None", getattr(scenario, dimension))


def _compare(actual: Value | None, target: Value | None) -> MatchState:
    if actual is None or target is None:
        return MatchState.UNSTATED
    if isinstance(actual, _values.Range):
        matches = (
            isinstance(target, Decimal | int)
            and not isinstance(target, bool)
            and actual.contains(Decimal(target))
        )
    else:
        matches = actual == target
    return MatchState.MATCHES if matches else MatchState.CONFLICTS


def _scenario_checks(
    context: OfferContext, traces: dict[str, FieldTrace]
) -> tuple[ScenarioCheck, ...]:
    target = context.comparison_target
    if target is None:
        return ()
    scenario = CANONICAL_SCENARIOS[target]
    dimensions = {dim.value for dim in REQUIRED_DIMENSIONS[target]} | {"property_value"}
    dimensions.add(
        "autopay_discount" if scenario.family is ProductFamily.MORTGAGE else "first_lien_balance"
    )
    dimensions.update(
        path.removeprefix("scenario.") for path in traces if path.startswith("scenario.")
    )
    checks: list[ScenarioCheck] = []
    for dim in sorted(dimensions):
        direct = traces.get(_DIRECT.get(dim, ""))
        stated = traces.get(f"scenario.{dim}")
        if direct is not None and stated is not None:
            raise NormalizationError(f"scenario.{dim}", "competing statements for one dimension")
        evidence = stated or direct
        state = _compare(evidence.value if evidence else None, _comparison_target(scenario, dim))
        checks.append(
            ScenarioCheck(
                dim,
                state,
                _provenance(evidence.provenance, "t044.scenario-match.v1") if evidence else None,
            )
        )
    if scenario.family is not ProductFamily.MORTGAGE:
        checks.extend(_equity_checks(scenario, traces))
    return tuple(sorted(checks, key=lambda check: check.dimension))


def _equity_checks(
    scenario: ComparisonScenario, traces: dict[str, FieldTrace]
) -> Iterable[ScenarioCheck]:
    for name in sorted(_EQUITY_QUALIFIERS):
        evidence = traces.get(f"details.{name}")
        if name != "lien_position" and (evidence is None or evidence.value is None):
            continue
        actual = evidence.value if evidence else None
        expected: Value | None
        if name == "lien_position":
            expected = scenario.required_lien_position
        else:
            expected = scenario.cltv if name == "max_cltv" else scenario.loan_amount
            if isinstance(actual, Decimal):
                actual = (
                    _values.Range(actual, None)
                    if name == "min_amount"
                    else _values.Range(None, actual)
                )
        yield ScenarioCheck(
            name,
            _compare(actual, expected),
            _provenance(evidence.provenance, "t044.scenario-match.v1") if evidence else None,
        )


def _conditions(
    inputs: dict[str, ExtractedCandidate], context: OfferContext
) -> tuple[tuple[OfferCondition, ...], tuple[FieldTrace, ...]]:
    conditions: list[OfferCondition] = []
    traces: list[FieldTrace] = []
    target = CANONICAL_SCENARIOS[context.comparison_target] if context.comparison_target else None
    for path, candidate in inputs.items():
        if not path.startswith("conditions."):
            continue
        parts = path.split(".")
        if len(parts) not in (3, 4):
            raise NormalizationError(path, "expected conditions.<id>.<kind>[.<dimension>]")
        _, condition_id, raw_kind, *explicit = parts
        try:
            kind = ConditionKind(raw_kind)
            implicit = {
                ConditionKind.RELATIONSHIP: _D.RELATIONSHIP_DISCOUNT.value,
                ConditionKind.AUTOPAY: "autopay_discount",
            }.get(kind)
            dimension = explicit[0] if explicit else implicit
            if dimension is not None and (
                dimension not in _DIMENSIONS or implicit not in (None, dimension)
            ):
                raise ValueError("invalid condition dimension")
            raw = candidate.raw_value
            actual = (
                None
                if raw.lower() in _EMPTY
                else (
                    _values.comparison_statement(dimension, raw) if dimension is not None else raw
                )
            )
            trace = _trace(candidate, path, actual, "t044.condition-match.v1")
            expected = _comparison_target(target, dimension) if target and dimension else None
            conditions.append(
                OfferCondition(
                    condition_id=condition_id,
                    kind=kind,
                    description=raw,
                    provenance=trace.provenance,
                    scenario_state=_compare(actual, expected),
                )
            )
            traces.append(trace)
        except ValueError as exc:
            raise NormalizationError(path, str(exc)) from exc
    return tuple(conditions), tuple(traces)


def _get(traces: dict[str, FieldTrace], path: str) -> Value | None:
    return traces[path].value if path in traces else None


def _assemble(
    traces: dict[str, FieldTrace], kind: str, purpose: LoanPurpose | None
) -> tuple[dict[str, object], dict[str, object]]:
    details: dict[str, object] = {"kind": kind}
    headline: dict[str, object] = {}
    arm: dict[str, object] = {}
    intro: dict[str, object] = {}
    for path, trace in traces.items():
        value: object = trace.value
        if value is None or not (path in _TOP_LEVEL_RATES or path.startswith("details.")):
            continue
        if path == "details.purpose" and purpose is LoanPurpose.PURCHASE:
            continue
        if path in _INLINE_RATES:
            value = SourcedRate(value=cast("Decimal", value), provenance=trace.provenance)
        elif path in _MARGIN_PATHS:
            value = SourcedMargin(value=cast("Decimal", value), provenance=trace.provenance)
        elif path == "points":
            value = SourcedPoints(value=cast("Decimal", value), provenance=trace.provenance)
        if path in _TOP_LEVEL_RATES:
            headline[path] = value
        elif path.startswith("details.arm."):
            arm[path.removeprefix("details.arm.")] = value
        elif path.startswith("details.intro."):
            intro[path.removeprefix("details.intro.")] = value
        else:
            details[path.removeprefix("details.")] = value
    if arm or details.get("rate_type") is RateType.ADJUSTABLE:
        details["arm"] = arm
    if intro:
        details["intro"] = intro
    if kind in {"fixed_equity", "heloc"}:
        details.setdefault("lien_position", LienPosition.UNKNOWN)
    return details, headline


def _skip(
    reason: NoOfferReason, inputs: dict[str, ExtractedCandidate], traces: dict[str, FieldTrace]
) -> NormalizationResult:
    # Non-offers retain the original record without interpreting out-of-scope financial values.
    kept = tuple(
        traces[path]
        if path in traces
        else _trace(
            candidate,
            path,
            None if candidate.raw_value.lower() in _EMPTY else candidate.raw_value,
            "t044.non-offer.v1",
        )
        for path, candidate in inputs.items()
    )
    return NormalizationResult(no_offer_reason=reason, traces=kept)


def normalize_offer(
    candidates: Iterable[ExtractedCandidate], context: OfferContext
) -> NormalizationResult:
    """Normalize one row. Paths refer to offer fields, scenario statements, or conditions.

    Supply product_family and, for mortgage offers, details.purpose from the source.
    Scenario comparison uses explicit source fragments on scenario.<dimension> and
    shared facts such as details.term_months; it never fills a dimension from the target.
    Condition paths are conditions.<id>.<kind>[.<dimension>]; untyped qualifiers remain
    unstated rather than being assumed compatible. Send partner links as separate rows.
    Values must be the exact cited source fragments. Unknown paths, duplicate inputs,
    unrecognized units and contradictory row data are errors, not silent defaults.
    """
    inputs = _collect(candidates)
    traces: dict[str, FieldTrace] = {}
    if not inputs:
        return _skip(NoOfferReason.NO_CANDIDATES, inputs, traces)
    metadata = {"product_family", "rate_availability", "partner_link"}
    for path in sorted(metadata & inputs.keys()):
        traces[path] = _parse_trace(path, inputs[path])
    if "partner_link" in inputs:
        if len(inputs) != 1 or _get(traces, "partner_link") is None:
            raise NormalizationError("partner_link", "a link must be a separate non-rate record")
        return _skip(NoOfferReason.PARTNER_LINK, inputs, traces)
    family = _get(traces, "product_family")
    if isinstance(family, OutOfScopeProduct):
        return _skip(NoOfferReason.OUT_OF_SCOPE, inputs, traces)
    if not isinstance(family, ProductFamily):
        raise NormalizationError("product_family", "an explicit product family is required")
    availability = _get(traces, "rate_availability")
    if "rate_availability" in inputs and availability is not RateAvailability.PUBLIC_RATE:
        for path, candidate in inputs.items():
            if (
                path in _INLINE_RATES or path in _MARGIN_PATHS
            ) and candidate.raw_value.lower() not in _EMPTY:
                raise NormalizationError(path, "a stated rate contradicts non-public availability")
        return _skip(NoOfferReason.NON_PUBLIC_RATE, inputs, traces)

    allowed = _TOP_LEVEL_RATES | {"effective_at", "product_family", "rate_availability"}
    if family is ProductFamily.MORTGAGE:
        allowed |= _MORTGAGE | _ARM | {"details.purpose"}
    elif family is ProductFamily.HOME_EQUITY_LOAN:
        allowed |= _FIXED_EQUITY
    else:
        allowed |= _HELOC
    for path, candidate in inputs.items():
        if path.startswith("conditions."):
            continue
        if path not in allowed and not (
            path.startswith("scenario.") and path.removeprefix("scenario.") in _DIMENSIONS
        ):
            raise NormalizationError(path, "unsupported field for this product")
        if path not in traces:
            traces[path] = _parse_trace(path, candidate)
    purpose = cast("LoanPurpose | None", _get(traces, "details.purpose"))
    kind = {
        ProductFamily.MORTGAGE: "purchase_mortgage"
        if purpose is LoanPurpose.PURCHASE
        else "refinance_mortgage",
        ProductFamily.HOME_EQUITY_LOAN: "fixed_equity",
        ProductFamily.HELOC: "heloc",
    }[family]
    rate_paths = _INLINE_RATES - {
        "details.index_rate",
        "details.floor",
        "details.ceiling",
    }
    has_formula = family is ProductFamily.HELOC and all(
        _get(traces, f"details.{field}") is not None
        for field in ("index_name", "index_rate", "margin", "floor")
    )
    if not has_formula and not any(_get(traces, path) is not None for path in rate_paths):
        if availability is RateAvailability.PUBLIC_RATE:
            raise NormalizationError("rate_availability", "public rate declared without a rate")
        return _skip(NoOfferReason.NO_RATE, inputs, traces)
    if family is ProductFamily.MORTGAGE and purpose is None:
        raise NormalizationError("details.purpose", "mortgage offers require a stated loan purpose")
    conditions, condition_traces = _conditions(inputs, context)
    target = context.comparison_target
    if target is not None:
        scenario = CANONICAL_SCENARIOS[target]
        if (family, purpose) != (scenario.family, scenario.purpose):
            raise NormalizationError(
                "scenario_id", "comparison target has a different family or purpose"
            )
    checks = _scenario_checks(context, traces)
    # An explicit deal-breaker is also shown to the borrower as an evidence-backed
    # condition, in addition to its scenario match.
    conditions += tuple(
        OfferCondition(
            condition_id=f"t044.scenario.{check.dimension}",
            kind=ConditionKind.BORROWER_REQUIREMENT,
            description=check.provenance.source_text,
            provenance=check.provenance,
            scenario_state=check.state,
        )
        for check in checks
        if check.dimension in (_SUPPLEMENTAL | _EXTRA_STATEMENTS)
        and check.state is MatchState.CONFLICTS
        and check.provenance is not None
    )
    fields, headline = _assemble(traces, kind, purpose)
    kind_origin = traces.get("details.purpose", traces["product_family"])
    kind_trace = _trace(kind_origin.candidate, "details.kind", kind, "t044.offer-kind.v1")
    return _make_offer(
        context, traces, (*condition_traces, kind_trace), conditions, checks, fields, headline
    )


def _parse_trace(path: str, candidate: ExtractedCandidate) -> FieldTrace:
    try:
        if candidate.raw_value.lower() in _EMPTY:
            return _trace(candidate, path, None, "t044.absence.v1")
        value, rule = _parse(path, candidate.raw_value)
        return _trace(candidate, path, value, rule)
    except ValueError as exc:
        raise NormalizationError(path, str(exc)) from exc


def _make_offer(
    context: OfferContext,
    traces: dict[str, FieldTrace],
    extra_traces: tuple[FieldTrace, ...],
    conditions: tuple[OfferCondition, ...],
    checks: tuple[ScenarioCheck, ...],
    details: dict[str, object],
    headline: dict[str, object],
) -> NormalizationResult:
    ordered = tuple(sorted((*traces.values(), *extra_traces), key=lambda t: t.path))
    condition_map = {
        f"conditions.{condition.condition_id}": condition.provenance for condition in conditions
    }
    payload = {
        **context.model_dump(exclude={"comparison_target"}),
        **headline,
        "schema_version": CONTRACT_VERSION,
        "status": OfferStatus.CANDIDATE,
        "details": details,
        "scenario_id": context.comparison_target,
        "scenario_matches": tuple(
            DimensionMatch(
                dimension=ScenarioDimension(check.dimension),
                state=check.state,
                source_text=(
                    check.provenance.source_text
                    if check.provenance and check.state is not MatchState.UNSTATED
                    else None
                ),
            )
            for check in checks
            if check.dimension not in _SUPPLEMENTAL
        ),
        "conditions": conditions,
        "headline_is_as_low_as": any(c.kind is ConditionKind.AS_LOW_AS for c in conditions),
        "effective_at": _get(traces, "effective_at"),
        "supporting_evidence": tuple(
            dict.fromkeys(
                [t.provenance for t in ordered]
                + [c.provenance for c in checks if c.provenance is not None]
            )
        ),
    }
    try:
        offer = NormalizedOffer.model_validate(payload)
        provenance = {trace.path: trace.provenance for trace in ordered} | condition_map
        offer = NormalizedOffer.model_validate(
            {
                **payload,
                "field_provenance": tuple(
                    FieldEvidence(path=path, provenance=provenance[path])
                    for path in sorted(offer.material_paths() & provenance.keys())
                    if path in condition_map or traces[path].state is FieldState.STATED
                ),
            }
        )
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        raise NormalizationError(".".join(map(str, error["loc"])) or "offer", error["msg"]) from exc
    if offer.note_rate and offer.apr and offer.apr.value < offer.note_rate.value:
        raise NormalizationError("apr", "APR is below the stated note rate")
    if isinstance(offer.details, HelocDetails):
        _check_heloc_formula(offer.details)
    return NormalizationResult(
        offer=offer,
        traces=ordered,
        scenario_checks=checks,
        scenario_state=aggregate_match(check.state for check in checks) if checks else None,
    )


def _check_heloc_formula(details: HelocDetails) -> None:
    with localcontext(Context(prec=28)):
        if "derived_rate_out_of_range" in details.post_intro().missing_inputs:
            raise NormalizationError("details.post_intro_rate", "derived rate is outside bounds")
        stated = details.stated_post_intro_apr
        if stated is not None and (
            details.post_intro_conflicts()
            or (details.floor is not None and stated.value < details.floor.value)
            or (details.ceiling is not None and stated.value > details.ceiling.value)
        ):
            raise NormalizationError(
                "details.stated_post_intro_apr", "APR conflicts with the formula or its bounds"
            )

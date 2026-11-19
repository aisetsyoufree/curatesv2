"""T045 rule implementations; no approvals or publication actions."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from cu_rates.contracts import (
    APR_RULE_ID,
    CANONICAL_SCENARIOS,
    ConditionKind,
    FixedEquityDetails,
    HelocDetails,
    LienPosition,
    MatchState,
    NormalizedOffer,
    OfferStatus,
    Provenance,
    PurchaseMortgageDetails,
    RateTier,
    RateType,
    RefinanceMortgageDetails,
    RunStatus,
    ScenarioDimension,
    TierBound,
    TierDimension,
    evaluate_scenario_match,
)
from cu_rates.contracts.enums import EvidenceKind, PostIntroState
from cu_rates.contracts.evidence import (
    ExtractionProvenance,
    RenderedDomLocator,
    locator_fits_media_type,
)
from cu_rates.contracts.publication import DEGRADE_AFTER, WITHDRAW_AFTER
from cu_rates.contracts.scenarios import (
    StatedBound,
    computed_match_state,
    statement_matches_excerpt,
)
from cu_rates.normalize import _EMPTY, FieldState, _parse, _values

if TYPE_CHECKING:
    from . import ValidationContext

RULE_IDS = (
    APR_RULE_ID,
    "t045.product-shape",
    "t045.scenario-consistency",
    "t045.heloc-periods",
    "t045.freshness",
    "t045.field-evidence",
    "t045.capture-binding",
    "t045.condition-tier-completeness",
    "t045.field-contradiction",
    "t045.effective-date",
)

Problems = list[tuple[str, str]]
Check = tuple[Iterable[str], Problems]

_CONTEXT_PATHS = {"status", "observed_at", "valid_until", "effective_at", "source_id"}
_MORTGAGE = PurchaseMortgageDetails | RefinanceMortgageDetails
_LOCATORS = {
    EvidenceKind.HTML: {"html"},
    EvidenceKind.RENDERED_DOM: {"rendered_dom"},
    EvidenceKind.PDF: {"pdf"},
    EvidenceKind.JSON: {"json"},
    EvidenceKind.IMAGE_OCR: {"image_ocr"},
}


class Checks:
    def __init__(self, offer: NormalizedOffer, context: ValidationContext) -> None:
        self.offer = offer
        self.context = context
        self.artifacts = {a.artifact_id: a for a in context.artifacts}
        self.runs = {r.run_id: r for r in context.fetch_runs}
        self.ambiguous_artifacts = {
            a.artifact_id for a in context.artifacts if self.artifacts[a.artifact_id] != a
        }
        self.ambiguous_runs = {r.run_id for r in context.fetch_runs if self.runs[r.run_id] != r}
        self.rates = dict(offer.rate_items())
        self.evidence: dict[str, list[Provenance]] = defaultdict(list)
        for path, provenance in offer.iter_labeled_provenance():
            self.evidence[path.removeprefix("fields.")].append(provenance)
        self.claims: dict[str, list[tuple[str, Provenance]]] = defaultdict(list)
        for candidate in context.candidates:
            if candidate.proposed_path:
                path = candidate.proposed_path
                if path.startswith("scenario."):
                    path = "scenario_matches." + path.removeprefix("scenario.")
                elif path.startswith("conditions."):
                    path = next(
                        (
                            f"conditions.{condition.condition_id}"
                            for condition in offer.conditions
                            if path == f"conditions.{condition.condition_id}.{condition.kind.value}"
                            or path.startswith(
                                f"conditions.{condition.condition_id}.{condition.kind.value}."
                            )
                        ),
                        path,
                    )
                self.claims[path].append((candidate.raw_value, candidate.provenance))
        self.matches = {m.dimension: m for m in offer.scenario_matches}
        self.rate_captures = tuple(
            capture
            for rate in self.rates.values()
            if (capture := self.artifacts.get(rate.provenance.artifact_id)) is not None
            and not self._capture_problem(rate.provenance)
        )
        self.rate_runs = {a.fetch_run_id for a in self.rate_captures}

    def rules(self) -> Iterator[tuple[str, Iterable[str], Problems]]:
        methods = (
            self.apr,
            self.product_shape,
            self.scenario_consistency,
            self.heloc_periods,
            self.freshness,
            self.field_evidence,
            self.capture_binding,
            self.condition_tiers,
            self.contradictions,
            self.effective_date,
        )
        for rule_id, check in zip(RULE_IDS, methods, strict=True):
            paths, problems = check()
            yield rule_id, paths, problems

    def apr(self) -> Check:
        paths = {"note_rate", "apr"}
        problems = []
        for prefix, note, apr in [
            ("", self.offer.note_rate, self.offer.apr),
            *((f"tiers.{tier.tier_id}.", tier.note_rate, tier.apr) for tier in self.offer.tiers),
        ]:
            if note is not None and apr is not None:
                paths.update((f"{prefix}note_rate", f"{prefix}apr"))
                if apr.value < note.value:
                    problems.append((f"{prefix}apr", "APR is below its note rate"))
        return paths, problems

    def product_shape(self) -> Check:
        offer, product = self.offer, self.context.product
        details = offer.details
        paths = {"status", "details.kind", "product_id"}
        problems = []
        if offer.status is not OfferStatus.CANDIDATE:
            problems.append(("status", "pre-publication validation requires a candidate"))
        if product is not None and (
            product.product_id != offer.product_id
            or product.institution_charter != offer.institution_charter
            or product.family is not offer.family
        ):
            problems.append(("product_id", "product identity, charter or family disagrees"))
        if isinstance(details, _MORTGAGE | FixedEquityDetails):
            paths.add("details.term_months")
            if not 1 <= details.term_months <= 720:
                problems.append(("details.term_months", "closed-end loan requires a valid term"))
        if isinstance(details, _MORTGAGE) and details.arm is not None:
            paths.add("details.arm")
            if not details.arm.is_complete:
                problems.append(
                    ("details.arm", "adjustable-rate mortgage disclosures are incomplete")
                )
            if details.arm.initial_period_months is not None and (
                details.arm.initial_period_months >= details.term_months
            ):
                problems.append(
                    ("details.arm.initial_period_months", "fixed period spans the loan")
                )
        if isinstance(details, FixedEquityDetails | HelocDetails):
            paths.add("details.lien_position")
            if details.lien_position is LienPosition.UNKNOWN:
                problems.append(
                    ("details.lien_position", "equity product lien position is unknown")
                )
        return paths, problems

    def _direct_fit(self) -> dict[ScenarioDimension, MatchState]:
        offer = self.offer
        if offer.scenario_id is None:
            return {}
        scenario = CANONICAL_SCENARIOS[offer.scenario_id]
        details = offer.details
        actuals: dict[ScenarioDimension, tuple[object, object]] = {}
        if isinstance(details, _MORTGAGE | FixedEquityDetails):
            actuals[ScenarioDimension.TERM_MONTHS] = (details.term_months, scenario.term_months)
        if isinstance(details, _MORTGAGE):
            actuals[ScenarioDimension.LOCK_DAYS] = (details.lock_days, scenario.lock_days)
            actuals[ScenarioDimension.POINTS] = (
                offer.points.value if offer.points else None,
                scenario.points,
            )
        return {
            dimension: MatchState.UNSTATED
            if actual is None
            else MatchState.MATCHES
            if actual == target
            else MatchState.CONFLICTS
            for dimension, (actual, target) in actuals.items()
        }

    def scenario_consistency(self) -> Check:
        offer = self.offer
        paths = {"scenario_id"}
        problems: Problems = []
        if offer.scenario_id is None:
            return paths, problems
        scenario = CANONICAL_SCENARIOS[offer.scenario_id]
        direct = self._direct_fit()
        for match in offer.scenario_matches:
            path = f"scenario_matches.{match.dimension.value}"
            paths.add(path)
            if match.state is MatchState.MATCHES and direct.get(match.dimension) in (
                MatchState.CONFLICTS,
                MatchState.UNSTATED,
            ):
                problems.append((path, "reported match contradicts or lacks the modeled value"))
            if match.stated is None and match.provenance is None:
                # Older normalization emits an excerpt without a typed claim. It is not proof
                # of comparable terms, but does not invalidate an otherwise accurate offer.
                continue
            if (
                match.stated is None
                or match.provenance is None
                or match.source_text != match.provenance.source_text
                or not statement_matches_excerpt(
                    match.dimension, match.stated, match.provenance.source_text
                )
                or computed_match_state(scenario, match.stated, match.dimension) is not match.state
            ):
                problems.append(
                    (path, "typed scenario claim does not agree with its source excerpt")
                )
        return paths, problems

    def heloc_periods(self) -> Check:
        details = self.offer.details
        paths = {"details.kind"}
        problems = []
        if isinstance(details, HelocDetails):
            for period in ("draw_period_months", "repayment_period_months"):
                path = f"details.{period}"
                paths.add(path)
                value = getattr(details, period)
                if value is None or not 1 <= value <= 720:
                    problems.append((path, "HELOC period must be stated and valid"))
            if details.intro is not None:
                path = "details.intro.duration_months"
                paths.add(path)
                duration = details.intro.duration_months
                draw = details.draw_period_months
                if duration is None or (draw is not None and duration > draw):
                    problems.append(
                        (path, "intro duration must be stated and fit in the draw period")
                    )
            paths.add("details.post_intro_rate")
            if (
                details.post_intro_conflicts()
                or details.post_intro().state is PostIntroState.INCOMPLETE
            ):
                problems.append(
                    ("details.post_intro_rate", "post-intro rate conflicts or is incomplete")
                )
        return paths, problems

    def freshness(self) -> Check:
        offer, now = self.offer, self.context.evaluated_at
        age = now - offer.observed_at
        problems = []
        if age < timedelta(0):
            problems.append(("observed_at", "observation is in the future"))
        elif age > WITHDRAW_AFTER:
            problems.append(("observed_at", "observation exceeds 96-hour withdrawal threshold"))
        elif age > DEGRADE_AFTER:
            problems.append(("observed_at", "observation exceeds 72-hour degradation threshold"))
        expiry = offer.valid_until
        if expiry is None:
            problems.append(("valid_until", "independently enforceable expiry is missing"))
        elif expiry <= max(offer.observed_at, now):
            problems.append(("valid_until", "expiry must follow both observation and evaluation"))
        elif expiry - offer.observed_at > WITHDRAW_AFTER:
            problems.append(("valid_until", "expiry extends beyond the 96-hour validity window"))
        return {"observed_at", "valid_until"}, problems

    def field_evidence(self) -> Check:
        offer = self.offer
        mapped = {item.path for item in offer.field_provenance}
        # Conditions already carry evidence inline; if a second mapping exists it must agree.
        inline = {f"conditions.{c.condition_id}" for c in offer.conditions}
        missing = offer.material_paths() - mapped - inline
        problems = [(path, "material field lacks field-specific evidence") for path in missing]
        for path in inline:
            if len(set(self.evidence[path])) > 1:
                problems.append((path, "inline and mapped condition evidence disagree"))
        return offer.material_paths() | self.rates.keys(), problems

    def _capture_problem(self, provenance: Provenance) -> str | None:
        offer = self.offer
        artifact = self.artifacts.get(provenance.artifact_id)
        if artifact is None or artifact.artifact_id in self.ambiguous_artifacts:
            return "capture is missing or ambiguous"
        run = self.runs.get(artifact.fetch_run_id)
        if (
            run is None
            or run.run_id in self.ambiguous_runs
            or run.status is not RunStatus.SUCCEEDED
            or run.source_id != offer.source_id
            or run.source_id != self.context.source.source_id
            or artifact.artifact_id not in run.artifact_ids
            or not run.started_at
            <= artifact.fetched_at
            <= run.finished_at
            <= self.context.evaluated_at
        ):
            return "capture is not bound to a successful completed fetch of this source"
        if artifact.source_url != self.context.source.url:
            return "capture URL differs from the offer source"
        if artifact.http_status != 200 or artifact.byte_length == 0:
            return "capture is not a nonempty direct HTTP 200"
        if not (
            self.context.evaluated_at - WITHDRAW_AFTER <= artifact.fetched_at <= offer.observed_at
        ):
            return "capture is stale or later than the offer observation"
        locator = provenance.locator
        if provenance.is_ocr:
            return "OCR evidence is not eligible for deterministic promotion"
        if not locator_fits_media_type(
            locator, artifact.media_type
        ) or locator.kind not in _LOCATORS.get(self.context.source.evidence_kind, set()):
            return "locator is incompatible with the stored media or source"
        if isinstance(locator, RenderedDomLocator):
            for response_id in locator.response_ids:
                response = self.artifacts.get(response_id)
                if (
                    response is None
                    or response_id in self.ambiguous_artifacts
                    or response_id not in run.artifact_ids
                    or response.fetch_run_id != run.run_id
                    or not run.started_at <= response.fetched_at <= run.finished_at
                    or response.http_status != 200
                    or response.byte_length == 0
                ):
                    return "rendered response is not bound to the same successful fetch"
        return None

    def capture_binding(self) -> Check:
        offer = self.offer
        paths = {"source_id", "observed_at"} | self.evidence.keys()
        problems = []
        if (
            not self.rate_captures
            or max(a.fetched_at for a in self.rate_captures) != offer.observed_at
        ):
            problems.append(("observed_at", "observation is not anchored to a direct rate capture"))
        for path, provenances in self.evidence.items():
            for provenance in provenances:
                if reason := self._capture_problem(provenance):
                    problems.append((path, reason))
                    continue
                capture = self.artifacts[provenance.artifact_id]
                if (
                    path in self.rates
                    and self.context.evaluated_at - capture.fetched_at > DEGRADE_AFTER
                ):
                    problems.append((path, "a rate capture exceeds the degradation threshold"))
                if path.startswith(("scenario_matches.", "conditions.")) and (
                    capture.fetch_run_id not in self.rate_runs
                    or capture.fetched_at != offer.observed_at
                ):
                    problems.append(
                        (path, "comparison evidence is not from the current rate capture")
                    )
        return paths, problems

    def condition_tiers(self) -> Check:
        offer = self.offer
        paths = {"headline_is_as_low_as"}
        problems = []
        as_low_as = [c for c in offer.conditions if c.kind is ConditionKind.AS_LOW_AS]
        if bool(as_low_as) != offer.headline_is_as_low_as or (as_low_as and not offer.tiers):
            problems.append(
                ("headline_is_as_low_as", "an 'as low as' claim requires a labeled tier")
            )
        for condition in offer.conditions:
            path = f"conditions.{condition.condition_id}"
            paths.add(path)
            if condition.description != condition.provenance.source_text:
                problems.append((path, "condition description is not its stated source text"))
        for tier in offer.tiers:
            path = f"tiers.{tier.tier_id}.bounds"
            paths.add(path)
            if not tier.bounds or any(
                not _valid_bound(bound.dimension, value)
                or (
                    bound.dimension is TierDimension.TERM_MONTHS
                    and isinstance(offer.details, HelocDetails)
                )
                for bound in tier.bounds
                for value in (bound.lower, bound.upper)
                if value is not None
            ):
                problems.append(
                    (path, "tier bound has invalid units, range or product applicability")
                )
            elif any(
                not _tier_bounds_match(tier.bounds, evidence.source_text)
                for evidence in self.evidence.get(path, ())
            ):
                problems.append((path, "tier bounds disagree with their field evidence"))
            if (offer.apr is not None and tier.apr is None) or (
                offer.note_rate is not None and tier.note_rate is None
            ):
                problems.append((path, "tier omits a rate type used by the headline"))
        if (
            as_low_as
            and offer.tiers
            and not any(self._tier_agrees_with_headline(tier) for tier in offer.tiers)
        ):
            problems.append(("headline_is_as_low_as", "no tier substantiates the advertised rate"))
        return paths, problems

    def _tier_agrees_with_headline(self, tier: RateTier) -> bool:
        return all(
            headline is None or (cell is not None and cell.value == headline.value)
            for cell, headline in (
                (tier.note_rate, self.offer.note_rate),
                (tier.apr, self.offer.apr),
            )
        )

    def _covering_tiers(self) -> tuple[RateTier, ...]:
        scenario_id = self.offer.scenario_id
        if scenario_id is None:
            return ()
        scenario = CANONICAL_SCENARIOS[scenario_id]
        values = {
            TierDimension.LTV: scenario.ltv,
            TierDimension.CLTV: scenario.cltv,
            TierDimension.CREDIT_SCORE: Decimal(scenario.credit_score),
            TierDimension.LOAN_AMOUNT: scenario.loan_amount,
            TierDimension.TERM_MONTHS: Decimal(scenario.term_months)
            if scenario.term_months
            else None,
        }
        return tuple(tier for tier in self.offer.tiers if tier.contains(values))

    def contradictions(self) -> Check:
        offer = self.offer
        fields: dict[str, object] = {path: value.value for path, value in self.rates.items()}
        fields["points"] = offer.points.value if offer.points else None
        fields["effective_at"] = offer.effective_at
        for path in offer.material_paths():
            if path.startswith("details."):
                value: object = offer.details
                for name in path.removeprefix("details.").split("."):
                    value = getattr(value, name, None)
                fields[path] = value
        fields.update(
            {f"conditions.{item.condition_id}": item.description for item in offer.conditions}
        )
        for match in offer.scenario_matches:
            if match.provenance is not None and match.stated is not None:
                path = f"scenario_matches.{match.dimension.value}"
                try:
                    fields[path] = _claim_value(path, match.provenance.source_text)
                except ValueError:
                    # Scenario consistency independently checks a stricter grammar.
                    continue
        problems = []
        for path, actual in fields.items():
            for provenance in self.evidence.get(path, ()):
                if _has_contradiction(path, provenance.source_text, actual, provenance.extraction):
                    problems.append((path, "normalized value disagrees with its stated evidence"))
        for path, claims in self.claims.items():
            values: set[object] = set()
            for raw, evidence in claims:
                if raw != evidence.source_text or self._capture_problem(evidence):
                    problems.append((path, "candidate lacks matching source-bound raw evidence"))
                    continue
                try:
                    value = _claim_value(path, raw, evidence.extraction)
                except ValueError:
                    problems.append(
                        (path, "candidate contains malformed or unverifiable field text")
                    )
                    continue
                values.add(value)
                if (
                    path in fields
                    and value != fields[path]
                    and not (isinstance(value, FieldState) and fields[path] is None)
                ):
                    problems.append((path, "source candidate contradicts the normalized value"))
            if len(values) > 1:
                problems.append((path, "source candidates state conflicting values for this field"))
        return {"supporting"} | fields.keys() | self.claims.keys(), problems

    def effective_date(self) -> Check:
        effective = self.offer.effective_at
        problems = []
        if effective is not None:
            if effective > self.offer.observed_at:
                problems.append(("effective_at", "effective date is later than the observed rate"))
            if not any(
                not self._capture_problem(item)
                and raw == item.source_text
                and not _has_contradiction("effective_at", raw, effective, item.extraction)
                and any(
                    original.artifact_id == item.artifact_id
                    and original.locator == item.locator
                    and original.source_text == raw
                    for original in self.evidence["supporting"]
                )
                for raw, item in self.claims["effective_at"]
            ):
                problems.append(
                    ("effective_at", "effective date lacks its field-bound source candidate")
                )
        return {"effective_at", "observed_at"}, problems

    def relevant_evidence(self, paths: Iterable[str]) -> Iterator[Provenance]:
        for path in paths:
            yield from self.evidence.get(path, ())
            yield from (evidence for _, evidence in self.claims.get(path, ()))
            if path.endswith(".apr") or path == "apr":
                yield from self.evidence.get(path.removesuffix("apr") + "note_rate", ())
            if path in _CONTEXT_PATHS or path == "headline_is_as_low_as":
                yield from self.evidence["supporting"]
                yield from (rate.provenance for rate in self.rates.values())
            if path == "details.post_intro_rate":
                yield from (
                    item
                    for evidence_path, item in self.offer.iter_labeled_provenance()
                    if evidence_path.startswith(("details.", "fields.details."))
                )

    def comparison_blockers(self) -> set[str]:
        offer = self.offer
        if offer.scenario_id is None:
            return {"no_comparison_scenario"}
        reasons = set()
        details, scenario = offer.details, CANONICAL_SCENARIOS[offer.scenario_id]
        direct = self._direct_fit()
        if MatchState.CONFLICTS in direct.values():
            reasons.add("modeled_scenario_conflict")
        if MatchState.UNSTATED in direct.values():
            reasons.add("modeled_scenario_unstated")
        if isinstance(details, _MORTGAGE) and details.rate_type is not RateType.FIXED:
            reasons.add("rate_type_conflicts")
        if isinstance(details, FixedEquityDetails | HelocDetails):
            if details.lien_position is not scenario.required_lien_position:
                reasons.add("lien_position_conflicts")
            if (
                details.max_cltv is not None
                and scenario.cltv is not None
                and details.max_cltv < scenario.cltv
            ):
                reasons.add("cltv_conflicts")
        if isinstance(details, FixedEquityDetails) and (
            (details.min_amount is not None and details.min_amount > scenario.loan_amount)
            or (details.max_amount is not None and details.max_amount < scenario.loan_amount)
        ):
            reasons.add("loan_amount_conflicts")
        if evaluate_scenario_match(
            offer.scenario_id, offer.scenario_matches
        ) is not MatchState.MATCHES or any(
            m.state is MatchState.MATCHES and m.stated is None for m in offer.scenario_matches
        ):
            reasons.add("scenario_not_proven")
        if offer.conditions:
            reasons.add("conditions_require_independent_verification")
        covering = self._covering_tiers()
        if offer.tiers and not covering:
            reasons.add("scenario_not_covered_by_tiers")
        if any(not self._tier_agrees_with_headline(tier) for tier in covering):
            reasons.add("covering_tier_differs_from_headline")
        if offer.apr is None and not isinstance(details, HelocDetails):
            reasons.add("headline_apr_missing")
        return reasons


def _valid_bound(dimension: TierDimension, value: Decimal) -> bool:
    if dimension in (TierDimension.LTV, TierDimension.CLTV):
        return 0 <= value <= 100
    if dimension is TierDimension.CREDIT_SCORE:
        return value == value.to_integral_value() and 300 <= value <= 850
    if dimension is TierDimension.TERM_MONTHS:
        return value == value.to_integral_value() and 1 <= value <= 720
    with localcontext() as context:  # ambient precision must not change the verdict
        context.prec = max(len(value.as_tuple().digits) + 4, 28)
        return value > 0 and value == value.quantize(Decimal("0.01"))


def _tier_bounds_match(bounds: tuple[TierBound, ...], text: str) -> bool:
    # A cell can state one range; a multiaxis footnote must label every range explicitly.
    if len(bounds) == 1 and ":" not in text:
        excerpts = {bounds[0].dimension.value: text}
    else:
        excerpts = {}
        for section in text.split(";"):
            label, separator, excerpt = section.partition(":")
            key = label.strip().lower().replace(" ", "_")
            if not separator or key in excerpts:
                return False
            excerpts[key] = excerpt.strip()
    if set(excerpts) != {bound.dimension.value for bound in bounds}:
        return False
    for bound in bounds:
        if bound.lower is None and bound.upper is None:
            return False
        numeric = (
            StatedBound(exact=bound.lower)
            if bound.lower == bound.upper
            else StatedBound(lower=bound.lower, upper=bound.upper)
        )
        if not statement_matches_excerpt(
            ScenarioDimension(bound.dimension.value), numeric, excerpts[bound.dimension.value]
        ):
            return False
    return True


def _claim_value(path: str, text: str, extraction: ExtractionProvenance | None = None) -> object:
    if text.lower() in _EMPTY:
        return _EMPTY[text.lower()]
    if path == "effective_at" and (instant := _values.prose_instant(text, extraction)) is not None:
        return instant  # prose date: exact start of day in the zone its own rule recorded
    if path.startswith("scenario_matches."):
        path = "scenario." + path.removeprefix("scenario_matches.")
    if path.startswith("conditions.") or path.endswith(".bounds"):
        return text
    if path.endswith(("apr", "note_rate")):
        text = re.sub(r"(?<=%)\s+(?:apr|rate|note rate)$", "", text, flags=re.IGNORECASE)
    return _parse(path, text)[0]


def _has_contradiction(
    path: str, excerpt: str, actual: object, extraction: ExtractionProvenance | None = None
) -> bool:
    try:
        return _claim_value(path, excerpt, extraction) != actual
    except ValueError:
        # No heuristic interpretation of arbitrary headings.
        return True

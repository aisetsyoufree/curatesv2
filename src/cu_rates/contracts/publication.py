"""Deterministic publication eligibility with stable reason codes.

``evaluate_publication`` is a pure function of the offer and an explicit context
(including the evaluation time). It fails closed: anything unproven suppresses,
quarantines, or routes to review; only a fully proven, previously reviewed, unchanged-
structure offer receives ``AUTO_PUBLISH``. Review and withdrawal inputs are auditable
records (``ReviewDecision``, ``WithdrawalEntry``), not booleans. No model output has
authority here.
"""

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from cu_rates.contracts.audit import Correction, PublishedSnapshot, ReviewDecision, WithdrawalEntry
from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    Identifier,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import (
    ChangeKind,
    ConformingStatus,
    EvidenceKind,
    InstitutionStatus,
    MatchState,
    OfferStatus,
    PostIntroState,
    ProductFamily,
    ProductLifecycle,
    RateAvailability,
    RateType,
    ReviewSubject,
    RunStatus,
    TierDimension,
    WithdrawalScope,
)
from cu_rates.contracts.evidence import (
    ImageOcrLocator,
    ParserVersion,
    RawArtifact,
    RenderedDomLocator,
    locator_fits_media_type,
)
from cu_rates.contracts.fingerprints import (
    OfferFingerprints,
    OfferSnapshot,
    offer_fingerprints,
    product_content_sha256,
    prove_rate_change,
)
from cu_rates.contracts.offers import (
    FixedEquityDetails,
    HelocDetails,
    NormalizedOffer,
    OfferDetails,
    PurchaseMortgageDetails,
    RefinanceMortgageDetails,
)
from cu_rates.contracts.registry import Institution, InstitutionSourceRelationship, ProductIdentity
from cu_rates.contracts.scenarios import (
    CANONICAL_SCENARIOS,
    ComparisonScenario,
    evaluate_scenario_match,
)
from cu_rates.contracts.sources import FetchRun, RateSource, SourceApproval

MAX_AUTO_MOVEMENT_BP = Decimal(50)
DEGRADE_AFTER = timedelta(hours=72)
WITHDRAW_AFTER = timedelta(hours=96)
ABANDONED_EFFECTIVE_AGE = timedelta(days=90)
ABANDONED_NO_CHANGE = timedelta(days=60)
APR_RULE_ID = "rule.apr-ge-note-rate"
_APR_RULE_PATHS = frozenset({"apr", "note_rate"})
_PUBLISHABLE_STATUSES = frozenset({OfferStatus.APPROVED, OfferStatus.PUBLISHED})
# Locator kinds a source of each evidence kind may legitimately yield (a page may embed a
# rate image, which is OCR evidence and always reviewed).
_SOURCE_LOCATORS: dict[EvidenceKind, frozenset[str]] = {
    EvidenceKind.HTML: frozenset({"html", "image_ocr"}),
    EvidenceKind.RENDERED_DOM: frozenset({"rendered_dom", "html", "image_ocr"}),
    EvidenceKind.PDF: frozenset({"pdf", "image_ocr"}),
    EvidenceKind.IMAGE_OCR: frozenset({"image_ocr"}),
    EvidenceKind.JSON: frozenset({"json"}),
}


class ReasonCode(StrEnum):
    # Suppress: never public.
    DENYLISTED = "denylisted"
    INSTITUTION_NOT_ACTIVE = "institution_not_active"
    CHARTER_UNVERIFIED = "charter_unverified"
    OFFER_NOT_CURRENT = "offer_not_current"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    ARTIFACT_STALE = "artifact_stale"
    VALIDITY_EXPIRED = "validity_expired"
    STALE_WITHDRAWN = "stale_withdrawn"
    # Quarantine: evidence, identity, or validation cannot be trusted.
    OFFER_CANDIDATE = "offer_candidate"
    OFFER_QUARANTINED = "offer_quarantined"
    RELATIONSHIP_UNVERIFIED = "relationship_unverified"
    RELATIONSHIP_MISMATCH = "relationship_mismatch"
    RELATIONSHIP_NOT_PRODUCT_BOUND = "relationship_not_product_bound"
    PRODUCT_MISMATCH = "product_mismatch"
    PRODUCT_NOT_ACTIVE = "product_not_active"
    RATE_NOT_PUBLIC = "rate_not_public"
    SOURCE_NOT_APPROVED = "source_not_approved"
    PARSER_NOT_APPROVED = "parser_not_approved"
    ARTIFACT_NOT_STORED = "artifact_not_stored"
    ARTIFACT_LOCATOR_MISMATCH = "artifact_locator_mismatch"
    ARTIFACT_SOURCE_MISMATCH = "artifact_source_mismatch"
    ARTIFACT_HTTP_STATUS = "artifact_http_status"
    FETCH_NOT_VERIFIED = "fetch_not_verified"
    OBSERVATION_UNBOUND = "observation_unbound"
    FIELD_EVIDENCE_MISSING = "field_evidence_missing"
    VALIDITY_MISSING = "validity_missing"
    VALIDITY_TOO_LONG = "validity_too_long"
    HELOC_POST_INTRO_INCOMPLETE = "heloc_post_intro_incomplete"
    HELOC_POST_INTRO_CONFLICT = "heloc_post_intro_conflict"
    VALIDATIONS_MISSING = "validations_missing"
    VALIDATION_FAILED = "validation_failed"
    VALIDATION_UNBOUND = "validation_unbound"
    CLOCK_SKEW = "clock_skew"
    # Review: a human must approve.
    FIRST_PUBLICATION = "first_publication"
    PRIOR_NOT_REVIEWED = "prior_not_reviewed"
    PRODUCT_NOT_REVIEWED = "product_not_reviewed"
    PRODUCT_IDENTITY_CHANGED = "product_identity_changed"
    STRUCTURAL_CHANGE = "structural_change"
    CORRECTION_PENDING = "correction_pending"
    POINTS_CHANGED = "points_changed"
    RATE_MOVEMENT_EXCEEDS_LIMIT = "rate_movement_exceeds_limit"
    RATE_MOVEMENT_UNBOUNDED = "rate_movement_unbounded"
    EFFECTIVE_DATE_MISSING = "effective_date_missing"
    OCR_VALUE = "ocr_value"
    SOURCE_ABANDONED = "source_abandoned"
    CONDITIONS_INCOMPLETE = "conditions_incomplete"
    ARM_TERMS_INCOMPLETE = "arm_terms_incomplete"
    # Placement limits.
    NO_APR = "no_apr"
    NO_NOTE_RATE = "no_note_rate"
    NON_CONVENTIONAL_PROGRAM = "non_conventional_program"
    NOT_CONFORMING = "not_conforming"
    SCENARIO_NOT_DECLARED = "scenario_not_declared"
    SCENARIO_CONFLICT = "scenario_conflict"
    SCENARIO_UNSTATED = "scenario_unstated"
    SCENARIO_TERMS_DIFFER = "scenario_terms_differ"
    POINTS_UNKNOWN = "points_unknown"
    POINTS_NOT_SCENARIO_VALUE = "points_not_scenario_value"
    CONDITION_NOT_SATISFIED = "condition_not_satisfied"
    TIER_NOT_COVERING_SCENARIO = "tier_not_covering_scenario"
    TIER_RATE_MISMATCH = "tier_rate_mismatch"
    # Informational.
    STALE_DEGRADED = "stale_degraded"


R = ReasonCode

_STATUS_REASON = {
    OfferStatus.CANDIDATE: R.OFFER_CANDIDATE,
    OfferStatus.QUARANTINED: R.OFFER_QUARANTINED,
}

SUPPRESS_REASONS = frozenset(
    {
        R.DENYLISTED,
        R.INSTITUTION_NOT_ACTIVE,
        R.CHARTER_UNVERIFIED,
        R.OFFER_NOT_CURRENT,
        R.NOT_YET_EFFECTIVE,
        R.ARTIFACT_STALE,
        R.VALIDITY_EXPIRED,
        R.STALE_WITHDRAWN,
    }
)
QUARANTINE_REASONS = frozenset(
    {
        R.OFFER_CANDIDATE,
        R.OFFER_QUARANTINED,
        R.RELATIONSHIP_UNVERIFIED,
        R.RELATIONSHIP_MISMATCH,
        R.RELATIONSHIP_NOT_PRODUCT_BOUND,
        R.PRODUCT_MISMATCH,
        R.PRODUCT_NOT_ACTIVE,
        R.RATE_NOT_PUBLIC,
        R.SOURCE_NOT_APPROVED,
        R.PARSER_NOT_APPROVED,
        R.ARTIFACT_NOT_STORED,
        R.ARTIFACT_LOCATOR_MISMATCH,
        R.ARTIFACT_SOURCE_MISMATCH,
        R.ARTIFACT_HTTP_STATUS,
        R.FETCH_NOT_VERIFIED,
        R.OBSERVATION_UNBOUND,
        R.FIELD_EVIDENCE_MISSING,
        R.VALIDITY_MISSING,
        R.VALIDITY_TOO_LONG,
        R.HELOC_POST_INTRO_INCOMPLETE,
        R.HELOC_POST_INTRO_CONFLICT,
        R.VALIDATIONS_MISSING,
        R.VALIDATION_FAILED,
        R.VALIDATION_UNBOUND,
        R.CLOCK_SKEW,
    }
)
REVIEW_REASONS = frozenset(
    {
        R.FIRST_PUBLICATION,
        R.PRIOR_NOT_REVIEWED,
        R.PRODUCT_NOT_REVIEWED,
        R.PRODUCT_IDENTITY_CHANGED,
        R.STRUCTURAL_CHANGE,
        R.CORRECTION_PENDING,
        R.POINTS_CHANGED,
        R.RATE_MOVEMENT_EXCEEDS_LIMIT,
        R.RATE_MOVEMENT_UNBOUNDED,
        R.EFFECTIVE_DATE_MISSING,
        R.OCR_VALUE,
        R.SOURCE_ABANDONED,
        R.CONDITIONS_INCOMPLETE,
        R.ARM_TERMS_INCOMPLETE,
    }
)
DETAIL_ONLY_REASONS = frozenset({R.NO_APR})
BROWSE_ONLY_REASONS = frozenset(
    {
        R.NO_NOTE_RATE,
        R.NON_CONVENTIONAL_PROGRAM,
        R.NOT_CONFORMING,
        R.SCENARIO_NOT_DECLARED,
        R.SCENARIO_CONFLICT,
        R.SCENARIO_UNSTATED,
        R.SCENARIO_TERMS_DIFFER,
        R.POINTS_UNKNOWN,
        R.POINTS_NOT_SCENARIO_VALUE,
        R.CONDITION_NOT_SATISFIED,
        R.TIER_NOT_COVERING_SCENARIO,
        R.TIER_RATE_MISMATCH,
        R.CONDITIONS_INCOMPLETE,
        R.ARM_TERMS_INCOMPLETE,
    }
)


class PublicationOutcome(StrEnum):
    AUTO_PUBLISH = "auto_publish"
    REQUIRES_REVIEW = "requires_review"
    QUARANTINE = "quarantine"
    SUPPRESS = "suppress"


class Placement(StrEnum):
    RANKED = "ranked"
    BROWSE_ONLY = "browse_only"
    INSTITUTION_DETAIL_ONLY = "institution_detail_only"
    NONE = "none"


class ValidationResult(ContractModel):
    """One deterministic rule outcome, bound to the exact offer content it examined.

    ``checked_paths`` must be non-empty (a rule that examined nothing proves nothing) and
    name paths that exist on the offer; ``evaluated_at`` can be neither before the offer's
    observation nor after the publication evaluation.
    """

    rule_id: Identifier
    passed: bool
    detail: Text | None = None
    offer_id: Identifier
    fingerprints: OfferFingerprints
    evaluated_at: UtcDatetime
    validator: ParserVersion
    checked_paths: Annotated[tuple[FieldPath, ...], Field(min_length=1), SortedUnique]

    @model_validator(mode="after")
    def _failure_is_explained(self) -> Self:
        if not self.passed and self.detail is None:
            raise ValueError("a failed validation must say why")
        return self


class ValidationPolicy(VersionedModel):
    """The approved rule set that must have run and passed before publication."""

    policy_id: Identifier
    policy_version: Text
    required_rules: Annotated[tuple[Identifier, ...], Field(min_length=1), SortedUnique]
    approved_by: Text
    approved_at: UtcDatetime


class PriorPublication(ContractModel):
    """The previously published state an auto-republication is compared against.

    ``published`` is the audited snapshot that went live and must list this offer with the
    same fingerprints. ``review`` is the human decision that approved exactly this
    snapshot's content for this offer, made no later than publication.
    """

    snapshot: OfferSnapshot
    published: PublishedSnapshot
    review: ReviewDecision | None = None
    last_value_change_at: UtcDatetime

    @model_validator(mode="after")
    def _links_and_review_cover_snapshot(self) -> Self:
        offer_id = self.snapshot.offer_id
        entry = next((e for e in self.published.entries if e.offer_id == offer_id), None)
        fingerprints = self.snapshot.fingerprints
        if (
            entry is None
            or entry.structural_sha256 != fingerprints.structural
            or entry.value_sha256 != fingerprints.value
        ):
            raise ValueError("the published snapshot does not list this offer's fingerprints")
        review = self.review
        if review is None:
            return self
        if review.subject is not ReviewSubject.OFFER:
            raise ValueError("a prior publication is reviewed by an offer review")
        if review.subject_id != self.snapshot.offer_id:
            raise ValueError("the review is for a different offer")
        if review.reviewed_fingerprints != fingerprints:
            raise ValueError("the review does not cover this snapshot's fingerprints")
        if review.decided_at > self.published.published_at:
            raise ValueError("the review cannot postdate the publication it approved")
        return self

    @model_validator(mode="after")
    def _value_change_not_after_publication(self) -> Self:
        if self.last_value_change_at > self.published.published_at:
            raise ValueError("the last value change cannot postdate the publication")
        return self

    @property
    def reviewed(self) -> bool:
        return self.review is not None and self.review.approved


class PublicationContext(ContractModel):
    institution: Institution
    product: ProductIdentity
    relationship: InstitutionSourceRelationship
    source: RateSource
    source_approval: SourceApproval | None = None
    validation_policy: ValidationPolicy | None = None
    approved_parsers: tuple[ParserVersion, ...] = ()
    known_artifacts: tuple[RawArtifact, ...] = ()
    fetch_runs: tuple[FetchRun, ...] = ()
    validations: tuple[ValidationResult, ...] = ()
    prior: PriorPublication | None = None
    product_review: ReviewDecision | None = None
    corrections: tuple[Correction, ...] = ()
    withdrawals: tuple[WithdrawalEntry, ...] = ()
    snapshot_id: Identifier | None = None
    evaluated_at: UtcDatetime

    @model_validator(mode="after")
    def _records_are_unique(self) -> Self:
        """Duplicates would be silently collapsed or shadowed by lookups; reject them."""
        keyed: dict[str, list[object]] = {
            "approved parser": [(p.name, p.version) for p in self.approved_parsers],
            "known artifact": [a.artifact_id for a in self.known_artifacts],
            "fetch run": [r.run_id for r in self.fetch_runs],
            "validation rule result": [(v.offer_id, v.rule_id) for v in self.validations],
            "withdrawal": [w.withdrawal_id for w in self.withdrawals],
            "correction": [c.correction_id for c in self.corrections],
        }
        for label, keys in keyed.items():
            if len(set(keys)) != len(keys):
                raise ValueError(f"duplicate {label} in publication context")
        return self


class PublicationDecision(ContractModel):
    """Outcome plus placement.

    ``placement`` is what the offer may be used for *if published*; it is only live when
    ``outcome`` is AUTO_PUBLISH, or after a reviewer approves a REQUIRES_REVIEW offer.
    Suppressed and quarantined offers always have placement NONE.
    """

    outcome: PublicationOutcome
    placement: Placement
    reasons: Annotated[tuple[ReasonCode, ...], Field(max_length=len(ReasonCode))]

    @property
    def publishable_now(self) -> bool:
        return self.outcome is PublicationOutcome.AUTO_PUBLISH

    @property
    def ranking_eligible(self) -> bool:
        return self.publishable_now and self.placement is Placement.RANKED

    @property
    def degraded(self) -> bool:
        return ReasonCode.STALE_DEGRADED in self.reasons


def _withdrawn(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    targets = {
        (WithdrawalScope.OFFER, offer.offer_id),
        (WithdrawalScope.PRODUCT, offer.product_id),
        (WithdrawalScope.INSTITUTION, str(offer.institution_charter)),
        (WithdrawalScope.INSTITUTION, str(ctx.institution.charter_number)),
    }
    if ctx.snapshot_id is not None:
        targets.add((WithdrawalScope.SNAPSHOT, ctx.snapshot_id))
    for entry in ctx.withdrawals:
        if not entry.in_force_at(ctx.evaluated_at):
            continue
        if (entry.scope, entry.target_id) in targets:
            return True
        if entry.scope is WithdrawalScope.SNAPSHOT and ctx.snapshot_id is None:
            return True  # which snapshot is being built is unknown: fail closed
    return False


def _snapshot_withdrawn(snapshot_id: str, ctx: PublicationContext) -> bool:
    return any(
        e.scope is WithdrawalScope.SNAPSHOT
        and e.target_id == snapshot_id
        and e.in_force_at(ctx.evaluated_at)
        for e in ctx.withdrawals
    )


def _lifecycle_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    institution = ctx.institution
    if _withdrawn(offer, ctx):
        reasons.add(R.DENYLISTED)
    if institution.status is not InstitutionStatus.ACTIVE:
        reasons.add(R.INSTITUTION_NOT_ACTIVE)
    if not institution.charter_verified:
        reasons.add(R.CHARTER_UNVERIFIED)
    if offer.status not in _PUBLISHABLE_STATUSES:  # only approved/published may proceed
        reasons.add(_STATUS_REASON.get(offer.status, R.OFFER_NOT_CURRENT))
    if offer.effective_at is not None and offer.effective_at > ctx.evaluated_at:
        reasons.add(R.NOT_YET_EFFECTIVE)
    if offer.published_at is not None and offer.published_at > ctx.evaluated_at:
        reasons.add(R.CLOCK_SKEW)
    if offer.valid_until is None:
        reasons.add(R.VALIDITY_MISSING)  # expiry must be enforceable independently
    else:
        if ctx.evaluated_at >= offer.valid_until:
            reasons.add(R.VALIDITY_EXPIRED)
        if offer.valid_until - offer.observed_at > WITHDRAW_AFTER:
            reasons.add(R.VALIDITY_TOO_LONG)
    age = ctx.evaluated_at - offer.observed_at
    if age < timedelta(0):
        reasons.add(R.CLOCK_SKEW)
    elif age > WITHDRAW_AFTER:
        reasons.add(R.STALE_WITHDRAWN)
    elif age > DEGRADE_AFTER:
        reasons.add(R.STALE_DEGRADED)
    return reasons


def _source_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    approval, source = ctx.source_approval, ctx.source
    if (
        approval is None
        or not approval.approved
        or source.source_id != offer.source_id
        or approval.source_id != source.source_id
        or approval.source_url != source.url
        or approval.decided_at > ctx.evaluated_at
    ):
        return {R.SOURCE_NOT_APPROVED}
    return set()


def _artifact_reasons(
    artifact: RawArtifact, offer: NormalizedOffer, ctx: PublicationContext, *, primary: bool
) -> set[ReasonCode]:
    """Whether one stored response came from a successful, current fetch of this source."""
    reasons: set[ReasonCode] = set()
    run = next((r for r in ctx.fetch_runs if r.run_id == artifact.fetch_run_id), None)
    if (
        run is None
        or run.status is not RunStatus.SUCCEEDED
        or run.source_id != offer.source_id
        or artifact.artifact_id not in run.artifact_ids
        or not run.started_at <= artifact.fetched_at <= run.finished_at
        or run.finished_at > ctx.evaluated_at
    ):
        reasons.add(R.FETCH_NOT_VERIFIED)
    if not 200 <= artifact.http_status < 300:
        reasons.add(R.ARTIFACT_HTTP_STATUS)
    if artifact.fetched_at > ctx.evaluated_at:
        reasons.add(R.CLOCK_SKEW)
    elif ctx.evaluated_at - artifact.fetched_at > WITHDRAW_AFTER:
        reasons.add(R.ARTIFACT_STALE)
    # The source contract models no redirects, so a cited page must be the exact URL.
    if primary and artifact.source_url != ctx.source.url:
        reasons.add(R.ARTIFACT_SOURCE_MISMATCH)
    return reasons


def _evidence_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    approved = set(ctx.approved_parsers)
    used: dict[str, RawArtifact] = {}
    for provenance in offer.iter_provenance():
        locator = provenance.locator
        referenced = [(provenance.artifact_id, True)]
        if isinstance(locator, RenderedDomLocator):
            referenced += [(response_id, False) for response_id in locator.response_ids]
        for artifact_id, primary in referenced:
            artifact = stored.get(artifact_id)
            if artifact is None:
                reasons.add(R.ARTIFACT_NOT_STORED)
                continue
            reasons |= _artifact_reasons(artifact, offer, ctx, primary=primary)
            if not primary:
                continue
            used[artifact_id] = artifact
            if not locator_fits_media_type(locator, artifact.media_type) or (
                locator.kind not in _SOURCE_LOCATORS[ctx.source.evidence_kind]
            ):
                reasons.add(R.ARTIFACT_LOCATOR_MISMATCH)
            if (
                isinstance(locator, ImageOcrLocator)
                and artifact.media_type.lower().startswith("image/")
                and locator.image_sha256 != artifact.artifact_id
            ):
                reasons.add(R.ARTIFACT_LOCATOR_MISMATCH)
        if provenance.extraction.parser not in approved:
            reasons.add(R.PARSER_NOT_APPROVED)
        if provenance.is_ocr:
            reasons.add(R.OCR_VALUE)
    # Observation is the newest cited fetch, not a free-standing claim.
    if used and offer.observed_at != max(a.fetched_at for a in used.values()):
        reasons.add(R.OBSERVATION_UNBOUND)
    return reasons


def _validation_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    """Every required rule must have a passing result bound to this exact offer content."""
    reasons: set[ReasonCode] = set()
    policy = ctx.validation_policy
    required = set(policy.required_rules) if policy is not None else set()
    if policy is None or policy.approved_at > ctx.evaluated_at:
        reasons.add(R.VALIDATIONS_MISSING)
    if offer.apr is not None and offer.note_rate is not None:
        required.add(APR_RULE_ID)  # APR below the note rate is rejected by this rule
    current = offer_fingerprints(offer)
    known_paths = set(offer.rate_values()) | offer.material_paths() | {"points"}
    passed: set[str] = set()
    for result in ctx.validations:
        paths = set(result.checked_paths)
        if (
            result.offer_id != offer.offer_id
            or result.fingerprints != current
            or not offer.observed_at <= result.evaluated_at <= ctx.evaluated_at
            or not paths <= known_paths
        ):
            reasons.add(R.VALIDATION_UNBOUND)
        elif not result.passed:
            reasons.add(R.VALIDATION_FAILED)
        elif result.rule_id != APR_RULE_ID or paths >= _APR_RULE_PATHS:
            passed.add(result.rule_id)  # the APR rule only counts if it examined both rates
    if not required or required - passed:
        reasons.add(R.VALIDATIONS_MISSING)
    return reasons


def _identity_and_evidence_reasons(
    offer: NormalizedOffer, ctx: PublicationContext
) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    charter = offer.institution_charter
    relationship, product = ctx.relationship, ctx.product
    if (
        not relationship.verified
        or relationship.approved_at is None
        or relationship.approved_at > ctx.evaluated_at
    ):
        reasons.add(R.RELATIONSHIP_UNVERIFIED)
    if (
        ctx.institution.charter_number != charter
        or relationship.institution_charter != charter
        or relationship.source_id != offer.source_id
        or relationship.product_id not in (None, offer.product_id)
    ):
        reasons.add(R.RELATIONSHIP_MISMATCH)
    if relationship.product_id is None:
        reasons.add(R.RELATIONSHIP_NOT_PRODUCT_BOUND)  # a source-wide binding covers no product
    if (
        product.institution_charter != charter
        or product.product_id != offer.product_id
        or product.family is not offer.family
    ):
        reasons.add(R.PRODUCT_MISMATCH)
    if product.lifecycle is not ProductLifecycle.ACTIVE:
        reasons.add(R.PRODUCT_NOT_ACTIVE)
    if product.rate_availability is not RateAvailability.PUBLIC_RATE:
        reasons.add(R.RATE_NOT_PUBLIC)
    reasons |= _source_reasons(offer, ctx)
    reasons |= _evidence_reasons(offer, ctx)
    if offer.material_paths() - {e.path for e in offer.field_provenance}:
        reasons.add(R.FIELD_EVIDENCE_MISSING)
    reasons |= _validation_reasons(offer, ctx)
    return reasons


def _corrections_cleared(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    """Each correction needs a matching content-bound record or a later review of this state."""
    current = offer_fingerprints(offer)
    review = ctx.prior.review if ctx.prior is not None else None
    reviewed_current = (
        review is not None
        and review.approved
        and review.reviewed_fingerprints == current
        and review.decided_at <= ctx.evaluated_at
    )
    for correction in ctx.corrections:
        if correction.offer_id != offer.offer_id:
            continue
        if correction.corrected_at > ctx.evaluated_at:
            return False
        bound = correction.corrected_fingerprints == current
        if not bound and not (
            reviewed_current and review is not None and review.decided_at >= correction.corrected_at
        ):
            return False
    return True


def _change_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    review = ctx.product_review
    if not (
        review is not None
        and review.approved
        and review.subject is ReviewSubject.PRODUCT
        and review.subject_id == ctx.product.product_id
        and review.reviewed_product_sha256 == product_content_sha256(ctx.product)
        and review.decided_at <= ctx.evaluated_at
    ):
        reasons.add(R.PRODUCT_NOT_REVIEWED)
    if offer.effective_at is None:
        reasons.add(R.EFFECTIVE_DATE_MISSING)  # abandonment cannot be assessed silently
    if not _corrections_cleared(offer, ctx):
        reasons.add(R.CORRECTION_PENDING)
    prior = ctx.prior
    if prior is None:
        reasons.add(R.FIRST_PUBLICATION)
        return reasons
    prior_trusted = (
        prior.reviewed
        and prior.review is not None
        and prior.review.decided_at <= ctx.evaluated_at
        and prior.published.published_at <= ctx.evaluated_at
        and not _snapshot_withdrawn(prior.published.snapshot_id, ctx)
    )
    if not prior_trusted:
        reasons.add(R.PRIOR_NOT_REVIEWED)
    current = OfferSnapshot.from_offer(offer)
    if (
        prior.snapshot.product_id != current.product_id
        or prior.snapshot.offer_id != current.offer_id
    ):
        reasons.add(R.PRODUCT_IDENTITY_CHANGED)
    proof = prove_rate_change(prior.snapshot, current)
    if proof.change is ChangeKind.STRUCTURAL:
        reasons.add(R.STRUCTURAL_CHANGE)
    if proof.points_changed:
        reasons.add(R.POINTS_CHANGED)
    if proof.max_abs_delta_bp > MAX_AUTO_MOVEMENT_BP:
        reasons.add(R.RATE_MOVEMENT_EXCEEDS_LIMIT)
    if proof.unbounded:
        reasons.add(R.RATE_MOVEMENT_UNBOUNDED)
    last_change = prior.last_value_change_at
    if last_change > ctx.evaluated_at:
        reasons.add(R.CLOCK_SKEW)
    if proof.change is not ChangeKind.UNCHANGED:
        last_change = ctx.evaluated_at
    if (
        offer.effective_at is not None
        and ctx.evaluated_at - offer.effective_at > ABANDONED_EFFECTIVE_AGE
        and ctx.evaluated_at - last_change >= ABANDONED_NO_CHANGE
    ):
        reasons.add(R.SOURCE_ABANDONED)
    return reasons


def _shape_reasons(offer: NormalizedOffer) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    details = offer.details
    if (
        isinstance(details, PurchaseMortgageDetails | RefinanceMortgageDetails)
        and details.arm is not None
        and not details.arm.is_complete
    ):
        reasons.add(R.ARM_TERMS_INCOMPLETE)
    if offer.headline_is_as_low_as and not offer.tiers:
        reasons.add(R.CONDITIONS_INCOMPLETE)
    return reasons


def _tier_reasons(offer: NormalizedOffer) -> set[ReasonCode]:
    if offer.scenario_id is None:
        return set()
    scenario = CANONICAL_SCENARIOS[offer.scenario_id]
    scenario_values = {
        TierDimension.LTV: scenario.ltv,
        TierDimension.CLTV: scenario.cltv,
        TierDimension.CREDIT_SCORE: Decimal(scenario.credit_score),
        TierDimension.LOAN_AMOUNT: scenario.loan_amount,
        TierDimension.TERM_MONTHS: (
            Decimal(scenario.term_months) if scenario.term_months is not None else None
        ),
    }
    if not offer.tiers:
        return set()
    covering = [t for t in offer.tiers if t.contains(scenario_values)]
    if not covering:
        return {R.TIER_NOT_COVERING_SCENARIO}
    reasons: set[ReasonCode] = set()
    for tier in covering:  # tiers never overlap, so at most one
        for stated, advertised in ((tier.note_rate, offer.note_rate), (tier.apr, offer.apr)):
            if stated is not None and advertised is not None and stated.value != advertised.value:
                reasons.add(R.TIER_RATE_MISMATCH)
    return reasons


def _terms_match_scenario(details: OfferDetails, scenario: ComparisonScenario) -> bool:
    """Every directly comparable modeled field must equal or fit the canonical scenario.

    Unknown values never match where the scenario depends on them (lock days, lien); an
    unstated optional bound (CLTV cap, amount limits) cannot contradict the scenario.
    """
    if isinstance(details, PurchaseMortgageDetails | RefinanceMortgageDetails):
        return (
            details.rate_type is RateType.FIXED
            and details.term_months == scenario.term_months
            and details.lock_days == scenario.lock_days
        )
    if details.lien_position is not scenario.required_lien_position:
        return False  # unknown or first lien is not comparable
    if (
        details.max_cltv is not None
        and scenario.cltv is not None
        and details.max_cltv < scenario.cltv
    ):
        return False
    if isinstance(details, FixedEquityDetails):
        amount = scenario.loan_amount
        return (
            details.term_months == scenario.term_months
            and (details.min_amount is None or details.min_amount <= amount)
            and (details.max_amount is None or amount <= details.max_amount)
        )
    return True


def _placement_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons = _tier_reasons(offer)
    product, details = ctx.product, offer.details
    if isinstance(details, HelocDetails):
        if details.post_intro().state is PostIntroState.INCOMPLETE:
            reasons.add(R.HELOC_POST_INTRO_INCOMPLETE)
        if details.post_intro_conflicts():
            reasons.add(R.HELOC_POST_INTRO_CONFLICT)
    else:
        if offer.apr is None:
            reasons.add(R.NO_APR)
        if offer.note_rate is None:
            reasons.add(R.NO_NOTE_RATE)
    if not product.is_conventional:
        reasons.add(R.NON_CONVENTIONAL_PROGRAM)
    if offer.family is ProductFamily.MORTGAGE and product.conforming_status is not (
        ConformingStatus.CONFORMING
    ):
        reasons.add(R.NOT_CONFORMING)
    if any(c.scenario_state is not MatchState.MATCHES for c in offer.conditions):
        reasons.add(R.CONDITION_NOT_SATISFIED)
    if offer.scenario_id is None:
        reasons.add(R.SCENARIO_NOT_DECLARED)
        return reasons
    scenario = CANONICAL_SCENARIOS[offer.scenario_id]
    overall = evaluate_scenario_match(offer.scenario_id, offer.scenario_matches)
    if overall is MatchState.CONFLICTS:
        reasons.add(R.SCENARIO_CONFLICT)
    elif overall is MatchState.UNSTATED:
        reasons.add(R.SCENARIO_UNSTATED)
    # Compare the contract's own fields to the canonical scenario, not the extractor's claims.
    if not _terms_match_scenario(details, scenario):
        reasons.add(R.SCENARIO_TERMS_DIFFER)
    if scenario.points is not None:
        if offer.points is None:
            reasons.add(R.POINTS_UNKNOWN)
        elif offer.points.value != scenario.points:
            reasons.add(R.POINTS_NOT_SCENARIO_VALUE)
    return reasons


def evaluate_publication(offer: NormalizedOffer, ctx: PublicationContext) -> PublicationDecision:
    """Deterministically decide publication outcome, placement, and reason codes."""
    reasons = _lifecycle_reasons(offer, ctx)
    reasons |= _identity_and_evidence_reasons(offer, ctx)
    reasons |= _change_reasons(offer, ctx)
    reasons |= _shape_reasons(offer)
    reasons |= _placement_reasons(offer, ctx)
    ordered = tuple(code for code in ReasonCode if code in reasons)
    if reasons & SUPPRESS_REASONS:
        return PublicationDecision(
            outcome=PublicationOutcome.SUPPRESS, placement=Placement.NONE, reasons=ordered
        )
    if reasons & QUARANTINE_REASONS:
        return PublicationDecision(
            outcome=PublicationOutcome.QUARANTINE, placement=Placement.NONE, reasons=ordered
        )
    outcome = (
        PublicationOutcome.REQUIRES_REVIEW
        if reasons & REVIEW_REASONS
        else PublicationOutcome.AUTO_PUBLISH
    )
    if reasons & DETAIL_ONLY_REASONS:
        placement = Placement.INSTITUTION_DETAIL_ONLY
    elif reasons & BROWSE_ONLY_REASONS:
        placement = Placement.BROWSE_ONLY
    else:
        placement = Placement.RANKED
    return PublicationDecision(outcome=outcome, placement=placement, reasons=ordered)

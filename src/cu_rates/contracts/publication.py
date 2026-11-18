"""Deterministic publication eligibility with stable reason codes.

``evaluate_publication`` is a pure function of the offer and an explicit context
(including the evaluation time). It fails closed: anything unproven suppresses,
quarantines, or routes to review; only a fully proven, previously reviewed, unchanged-
structure offer receives ``AUTO_PUBLISH``. Review and withdrawal inputs are auditable
records (``ReviewDecision``, ``WithdrawalEntry``), not booleans. No model output has
authority here.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from ipaddress import IPv6Address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from cu_rates.contracts.audit import Correction, PublishedSnapshot, ReviewDecision, WithdrawalEntry
from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    Identifier,
    Sha256Hex,
    SignedPercent,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
    canonical_decimal,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ChangeKind,
    ConformingStatus,
    EvidenceKind,
    FetchMethod,
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
    ScenarioDimension,
    TierDimension,
    WithdrawalScope,
)
from cu_rates.contracts.evidence import (
    ImageOcrLocator,
    ParserVersion,
    Provenance,
    RawArtifact,
    RenderedDomLocator,
    locator_fits_media_type,
)
from cu_rates.contracts.fingerprints import (
    OfferFingerprints,
    OfferSnapshot,
    offer_evidence_sha256,
    offer_fingerprints,
    product_content_sha256,
    prove_rate_change,
)
from cu_rates.contracts.offers import (
    FixedEquityDetails,
    HelocDetails,
    NormalizedOffer,
    OfferCondition,
    OfferDetails,
    PurchaseMortgageDetails,
    RefinanceMortgageDetails,
)
from cu_rates.contracts.registry import (
    Institution,
    InstitutionIdentityReview,
    InstitutionSourceRelationship,
    ProductIdentity,
)
from cu_rates.contracts.scenarios import (
    CANONICAL_SCENARIOS,
    ComparisonScenario,
    DimensionMatch,
    StatedBound,
    computed_match_state,
    evaluate_scenario_match,
    statement_matches_excerpt,
)
from cu_rates.contracts.sources import (
    FetchProfile,
    FetchRun,
    NotModifiedObservation,
    RateSource,
    SourceApproval,
    SourceFingerprint,
    is_strong_etag,
    profile_sha256,
)

MAX_AUTO_MOVEMENT_BP = Decimal(50)
DEGRADE_AFTER = timedelta(hours=72)
WITHDRAW_AFTER = timedelta(hours=96)
ABANDONED_EFFECTIVE_AGE = timedelta(days=90)
ABANDONED_NO_CHANGE = timedelta(days=60)

# D-18: Two separately captured authoritative evidence paths must corroborate every
# published value. Captures within this window of each other may corroborate.
DUAL_EVIDENCE_TIME_WINDOW = timedelta(hours=96)
# D-20: two captures of the same approved page count as two evidence paths when they are
# separate fetches at least this far apart; identical bytes are acceptable.
DUAL_EVIDENCE_MIN_SPACING = timedelta(hours=3)
# Source-of-record classes: every evidence kind the system models is authoritative.
# Model output or LLM/web-search agreement is NEVER evidence and has no EvidenceKind.
AUTHORITATIVE_SOURCE_CLASSES: frozenset[str] = frozenset(kind.value for kind in EvidenceKind)
_AUTHORITATIVE_EVIDENCE_KINDS: frozenset[EvidenceKind] = frozenset(EvidenceKind)
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
    IDENTITY_REVIEW_MISSING = "identity_review_missing"
    IDENTITY_NOT_CONFIRMED = "identity_not_confirmed"
    SCOPE_NOT_CONFIRMED = "scope_not_confirmed"
    IDENTITY_REVIEW_STALE = "identity_review_stale"
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
    PROFILE_UNAPPROVED = "profile_unapproved"
    REOBSERVATION_UNVERIFIED = "reobservation_unverified"
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
    # Dual-evidence (D-18): two separately captured authoritative paths required.
    INSUFFICIENT_EVIDENCE_PATHS = "insufficient_evidence_paths"
    DUPLICATE_EVIDENCE_CAPTURE = "duplicate_evidence_capture"
    EVIDENCE_PATH_NON_AUTHORITATIVE = "evidence_path_non_authoritative"
    EVIDENCE_PATH_PRODUCT_MISMATCH = "evidence_path_product_mismatch"
    EVIDENCE_PATH_SCENARIO_MISMATCH = "evidence_path_scenario_mismatch"
    EVIDENCE_PATH_TIME_OUTSIDE_WINDOW = "evidence_path_time_outside_window"
    EVIDENCE_PATH_VALUE_MISMATCH = "evidence_path_value_mismatch"
    # Review: a human must approve.
    FIRST_PUBLICATION = "first_publication"
    PRIOR_NOT_REVIEWED = "prior_not_reviewed"
    PRODUCT_NOT_REVIEWED = "product_not_reviewed"
    PRODUCT_IDENTITY_CHANGED = "product_identity_changed"
    STRUCTURAL_CHANGE = "structural_change"
    LAYOUT_UNREVIEWED = "layout_unreviewed"
    REOBSERVATION_CLAIM_CHANGED = "reobservation_claim_changed"
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
    HELOC_PERIODS_INCOMPLETE = "heloc_periods_incomplete"
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
        R.IDENTITY_REVIEW_MISSING,
        R.IDENTITY_NOT_CONFIRMED,
        R.SCOPE_NOT_CONFIRMED,
        R.IDENTITY_REVIEW_STALE,
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
        R.PROFILE_UNAPPROVED,
        R.REOBSERVATION_UNVERIFIED,
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
        R.INSUFFICIENT_EVIDENCE_PATHS,
        R.DUPLICATE_EVIDENCE_CAPTURE,
        R.EVIDENCE_PATH_NON_AUTHORITATIVE,
        R.EVIDENCE_PATH_PRODUCT_MISMATCH,
        R.EVIDENCE_PATH_SCENARIO_MISMATCH,
        R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW,
        R.EVIDENCE_PATH_VALUE_MISMATCH,
    }
)
REVIEW_REASONS = frozenset(
    {
        R.FIRST_PUBLICATION,
        R.PRIOR_NOT_REVIEWED,
        R.PRODUCT_NOT_REVIEWED,
        R.PRODUCT_IDENTITY_CHANGED,
        R.STRUCTURAL_CHANGE,
        R.LAYOUT_UNREVIEWED,
        R.REOBSERVATION_CLAIM_CHANGED,
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
        R.HELOC_PERIODS_INCOMPLETE,
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
    # Legacy single-artifact metadata. 304 authority now comes from the human review digest.
    verified_artifact_id: Sha256Hex | None = None
    verified_configuration_sha256: Sha256Hex | None = None
    verified_parser: ParserVersion | None = None

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
    def _verified_extraction_is_complete(self) -> Self:
        present = (
            self.verified_artifact_id is not None,
            self.verified_configuration_sha256 is not None,
            self.verified_parser is not None,
        )
        if any(present) and not all(present):
            raise ValueError("verified artifact, parser, and configuration must be set together")
        return self

    @model_validator(mode="after")
    def _value_change_not_after_publication(self) -> Self:
        if self.last_value_change_at > self.published.published_at:
            raise ValueError("the last value change cannot postdate the publication")
        return self

    @property
    def reviewed(self) -> bool:
        return self.review is not None and self.review.approved


class VerifiedScenarioStatement(ContractModel):
    """T014 supplies this from a trusted verifier that resolved the stored artifact locator.

    A claim copied from the proposed offer is not independent evidence.
    """

    offer_id: Identifier
    dimension: ScenarioDimension
    fetch_run_id: Identifier
    stated: StatedBound
    provenance: Provenance


class VerifiedScenarioCondition(ContractModel):
    """An independently verified condition match for this offer, scenario, and capture."""

    offer_id: Identifier
    scenario_id: CanonicalScenarioId
    fetch_run_id: Identifier
    condition: OfferCondition


class EvidencePath(ContractModel):
    """An authoritative evidence path supporting a published financial value.

    D-18 requires at least two distinct, corroborating, authoritative evidence paths
    matching product, scenario, and observation time window for every published value.
    """

    path: FieldPath
    value: SignedPercent
    artifact_id: Sha256Hex
    capture_id: Identifier
    observed_at: UtcDatetime
    product_id: Identifier
    scenario_id: CanonicalScenarioId | None = None
    source_class: Text
    content_sha256: Sha256Hex
    source_id: Identifier
    provenance: Provenance
    scenario_statements: tuple[VerifiedScenarioStatement, ...] = ()
    derived_from: tuple[Sha256Hex, ...] = ()


# Publication protocol 2.0 is separate from the frozen entity schema/taxonomy 1.0.
# Old readers forbid the new version marker/fields and reject these messages. New
# readers accept old writer inputs (absent marker/paths) but always enforce D-18;
# missing evidence is quarantined. Never strip the marker for an old reader.
PUBLICATION_VERSION = "2.0"


class PublicationContext(ContractModel):
    publication_version: Literal["2.0"] = "2.0"
    institution: Institution
    product: ProductIdentity
    relationship: InstitutionSourceRelationship
    identity_review: InstitutionIdentityReview | None = None
    source: RateSource
    source_approval: SourceApproval | None = None
    validation_policy: ValidationPolicy | None = None
    approved_parsers: tuple[ParserVersion, ...] = ()
    known_artifacts: tuple[RawArtifact, ...] = ()
    fetch_runs: tuple[FetchRun, ...] = ()
    reobservations: tuple[NotModifiedObservation, ...] = ()
    approved_profile: FetchProfile | None = None
    source_fingerprints: tuple[SourceFingerprint, ...] = ()
    verified_scenario_statements: tuple[VerifiedScenarioStatement, ...] = ()
    verified_scenario_conditions: tuple[VerifiedScenarioCondition, ...] = ()
    evidence_paths: tuple[EvidencePath, ...] = ()
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
            "fetch run": [
                *(r.run_id for r in self.fetch_runs),
                *(o.run_id for o in self.reobservations),
            ],
            "validation rule result": [(v.offer_id, v.rule_id) for v in self.validations],
            "source fingerprint": [
                (item.source_id, item.fetch_run_id, item.artifact_id)
                for item in self.source_fingerprints
            ],
            "verified scenario statement": [
                (item.offer_id, item.dimension, item.fetch_run_id)
                for item in self.verified_scenario_statements
            ],
            "verified scenario condition": [
                (item.offer_id, item.scenario_id, item.condition.condition_id)
                for item in self.verified_scenario_conditions
            ],
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

    publication_version: Literal["2.0"] = "2.0"
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


def _origin(url: str) -> tuple[str, str, int] | None:
    """Canonical HTTP origin; malformed authorities never count as approvals."""
    if any(ord(ch) < 33 or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    default = {"http": 80, "https": 443}.get(parts.scheme)
    if (
        default is None
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.netloc.endswith(":")
        or "%" in host
        or port == 0
    ):
        return None
    if ":" in host:
        if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parts.netloc):
            return None
        try:
            host = IPv6Address(host).compressed
        except ValueError:
            return None
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*\.?", host):
            return None
    return parts.scheme, host, default if port is None else port


def _origin_approved(url: str, ctx: PublicationContext) -> bool:
    """The source page's origin is approved. Any other origin must be named on the approval."""
    approval = ctx.source_approval
    if approval is None:
        return False
    allowed = {_origin(ctx.source.url)}
    allowed.update(_origin(item) for item in approval.approved_origins)
    origin = _origin(url)
    return origin is not None and origin in allowed


def _profile_is_approved(run: FetchRun, ctx: PublicationContext) -> bool:
    """The run must carry the digest stored on the human approval.

    T014 supplies ``SourceApproval.approved_profile_sha256`` from the approval record.
    A caller-supplied ``approved_profile``, or a fetch that started before that approval,
    does not qualify. Missing digest fails closed.
    """
    approval, profile = ctx.source_approval, ctx.approved_profile
    if (
        approval is None
        or not approval.approved
        or approval.approved_profile_sha256 is None
        or profile is None
        or run.profile_sha256 is None
    ):
        return False
    digest = profile_sha256(profile)
    return (
        approval.approved_profile_sha256 == digest
        and run.profile_sha256 == digest
        and run.profile_id == profile.profile_id
        and profile.source_id == run.source_id
        and approval.decided_at <= run.started_at
        and (
            ctx.source.evidence_kind is not EvidenceKind.RENDERED_DOM
            or profile.method is FetchMethod.RENDERED_BROWSER
        )
    )


def _withdrawn(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    targets = {
        (WithdrawalScope.OFFER, offer.offer_id),
        (WithdrawalScope.PRODUCT, offer.product_id),
        (WithdrawalScope.INSTITUTION, str(offer.institution_charter)),
        (WithdrawalScope.INSTITUTION, str(ctx.institution.charter_number)),
        (WithdrawalScope.SOURCE, offer.source_id),
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


IDENTITY_REVIEW_MAX_AGE = timedelta(days=400)


def _identity_reasons(institution: Institution, ctx: PublicationContext) -> set[ReasonCode]:
    """The flags on the institution are not proof: a current approving review is required."""
    review = ctx.identity_review
    if (
        review is None
        or review.institution_charter != institution.charter_number
        or review.decided_at > ctx.evaluated_at
    ):
        return {R.IDENTITY_REVIEW_MISSING}
    reasons: set[ReasonCode] = set()
    # A review is a human claim; the text it cites must itself say what it claims.
    if (
        not review.identity_confirmed
        or review.legal_name != institution.legal_name
        or not review.evidence_states_identity
    ):
        reasons.add(R.IDENTITY_NOT_CONFIRMED)
    if not review.massachusetts_scope_confirmed or not review.evidence_states_massachusetts:
        reasons.add(R.SCOPE_NOT_CONFIRMED)
    if ctx.evaluated_at - review.decided_at > IDENTITY_REVIEW_MAX_AGE:
        reasons.add(R.IDENTITY_REVIEW_STALE)
    return reasons


def _lifecycle_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    institution = ctx.institution
    if _withdrawn(offer, ctx):
        reasons.add(R.DENYLISTED)
    if institution.status is not InstitutionStatus.ACTIVE:
        reasons.add(R.INSTITUTION_NOT_ACTIVE)
    if not institution.charter_verified:
        reasons.add(R.CHARTER_UNVERIFIED)
    reasons |= _identity_reasons(institution, ctx)
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


def _reobservation_proven(
    observation: NotModifiedObservation,
    artifact: RawArtifact,
    offer: NormalizedOffer,
    ctx: PublicationContext,
) -> bool:
    """Whether a 304 proves the same immutable body was still served, under approval.

    It must name this artifact and its successful original run and the approved source.
    Both observations must carry the digest of the actual approved profile (a changed
    profile with the same ID fails), and one strong ETag must match exactly across the
    original 200, the conditional request, and the returned 304: weak, wildcard,
    list, Last-Modified-only, missing, or mismatched validators prove nothing. It must
    be collected after the original run finished and the approval was decided, and
    cannot lie in the future. Freshness only: nothing here approves publication.
    """
    run = next((r for r in ctx.fetch_runs if r.run_id == artifact.fetch_run_id), None)
    profile, approval = ctx.approved_profile, ctx.source_approval
    validator = run.conditional_validator if run is not None else None
    return (
        run is not None
        and run.status is RunStatus.SUCCEEDED
        and artifact.http_status == 200
        and profile is not None
        and approval is not None
        and approval.approved
        and observation.original_run_id == run.run_id
        and observation.original_artifact_id == artifact.artifact_id
        and observation.source_id == offer.source_id == ctx.source.source_id == run.source_id
        and profile.source_id == observation.source_id
        and observation.profile_id == run.profile_id == profile.profile_id
        and run.profile_sha256 == observation.profile_sha256 == profile_sha256(profile)
        and approval.approved_profile_sha256 == profile_sha256(profile)
        and approval.decided_at <= run.started_at
        and validator is not None
        and validator.kind == "etag"
        and is_strong_etag(validator.value)
        and observation.validator == validator
        and observation.response_etag == validator.value
        and run.finished_at <= observation.started_at
        and approval.decided_at <= observation.started_at
        and observation.observed_at <= ctx.evaluated_at
    )


def _reobservation_basis(
    artifact: RawArtifact, offer: NormalizedOffer, ctx: PublicationContext
) -> tuple[datetime, bool]:
    """The newest proven observation of ``artifact`` the offer claims, and any bad claim.

    A 304 counts only when the offer's own observation time is exactly that 304's; an
    unclaimed record is ignored, and a claimed one that is not fully proven fails closed.
    """
    claimed = [
        o
        for o in ctx.reobservations
        if o.original_artifact_id == artifact.artifact_id
        and o.observed_at == offer.observed_at
        and o.observed_at != artifact.fetched_at
    ]
    if not claimed:
        return artifact.fetched_at, False
    if all(_reobservation_proven(o, artifact, offer, ctx) for o in claimed):
        return offer.observed_at, False
    return artifact.fetched_at, True


def _artifact_reasons(
    artifact: RawArtifact,
    offer: NormalizedOffer,
    ctx: PublicationContext,
    *,
    primary: bool,
    observed: datetime | None = None,
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
    elif not _profile_is_approved(run, ctx):
        # Every qualifying fetch, including an ordinary 200, must match the approved profile.
        reasons.add(R.PROFILE_UNAPPROVED)
    if not 200 <= artifact.http_status < 300:
        reasons.add(R.ARTIFACT_HTTP_STATUS)
    if artifact.fetched_at > ctx.evaluated_at:
        reasons.add(R.CLOCK_SKEW)
    elif ctx.evaluated_at - (observed or artifact.fetched_at) > WITHDRAW_AFTER:
        reasons.add(R.ARTIFACT_STALE)
    # The source contract models no redirects, so a cited page must be the exact URL.
    if _origin(artifact.source_url) is None or (primary and artifact.source_url != ctx.source.url):
        reasons.add(R.ARTIFACT_SOURCE_MISMATCH)
    return reasons


def _evidence_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    approved = set(ctx.approved_parsers)
    used: dict[str, datetime] = {}
    for provenance in offer.iter_provenance():
        locator = provenance.locator
        primary_artifact = stored.get(provenance.artifact_id)
        referenced = [(provenance.artifact_id, True)]
        if isinstance(locator, RenderedDomLocator):
            if ctx.approved_profile is None or (
                ctx.approved_profile.method is not FetchMethod.RENDERED_BROWSER
            ):
                reasons.add(R.PROFILE_UNAPPROVED)
            referenced += [(response_id, False) for response_id in locator.response_ids]
        for artifact_id, primary in referenced:
            artifact = stored.get(artifact_id)
            if artifact is None:
                reasons.add(R.ARTIFACT_NOT_STORED)
                continue
            observed, unproven = (
                _reobservation_basis(artifact, offer, ctx) if primary else (None, False)
            )
            if unproven:
                reasons.add(R.REOBSERVATION_UNVERIFIED)
            reasons |= _artifact_reasons(artifact, offer, ctx, primary=primary, observed=observed)
            if not primary:
                # A linked XHR must be from this render run and an approved origin.
                if (
                    primary_artifact is None
                    or artifact.fetch_run_id != primary_artifact.fetch_run_id
                ):
                    reasons.add(R.FETCH_NOT_VERIFIED)
                if not _origin_approved(artifact.source_url, ctx):
                    reasons.add(R.ARTIFACT_SOURCE_MISMATCH)
                continue
            used[artifact_id] = observed or artifact.fetched_at
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
    # A new supporting document cannot advance the observation time of an older rate.
    rate_ids = {rate.provenance.artifact_id for _, rate in offer.rate_items()}
    rate_times = [used[artifact_id] for artifact_id in rate_ids & used.keys()]
    if used and (
        offer.observed_at != max(used.values())
        or offer.observed_at != max(rate_times, default=None)
    ):
        reasons.add(R.OBSERVATION_UNBOUND)
    if any(DEGRADE_AFTER < ctx.evaluated_at - time <= WITHDRAW_AFTER for time in rate_times):
        reasons.add(R.STALE_DEGRADED)
    return reasons


def financial_items(offer: NormalizedOffer) -> tuple[tuple[str, Decimal, Provenance], ...]:
    """Every modeled published financial claim (there is no fee scalar in schema 1.0)."""
    items = [(path, item.value, item.provenance) for path, item in offer.rate_items()]
    if offer.points is not None:
        items.append(("points", offer.points.value, offer.points.provenance))
    return tuple(items)


def scenario_signature(
    statements: tuple[VerifiedScenarioStatement, ...],
) -> tuple[tuple[str, str], ...] | None:
    """A present signature of source-stated assumptions, independent of capture identity."""
    if not statements or len({p.dimension for p in statements}) != len(statements):
        return None
    return tuple(sorted((p.dimension.value, p.stated.model_dump_json()) for p in statements))


def publication_evidence_sha256(offer: NormalizedOffer, paths: tuple[EvidencePath, ...]) -> str:
    """Review digest v2 binds the complete pair, including runs, times and locators."""
    payload = {
        "offer": offer_evidence_sha256(offer),
        "paths": sorted(p.model_dump_json() for p in paths),
    }
    return hashlib.sha256(
        ("publication-evidence-v2:" + json.dumps(payload, sort_keys=True)).encode()
    ).hexdigest()


def _path_reasons(
    p: EvidencePath, offer: NormalizedOffer, ctx: PublicationContext
) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    artifact = next((a for a in ctx.known_artifacts if a.artifact_id == p.artifact_id), None)
    if (
        artifact is None
        or p.source_id != offer.source_id
        or p.source_id != ctx.source.source_id
        or p.source_class != ctx.source.evidence_kind.value
        or p.source_class not in AUTHORITATIVE_SOURCE_CLASSES
        or p.content_sha256 != artifact.artifact_id
        or p.capture_id != artifact.fetch_run_id
        or p.observed_at != artifact.fetched_at
        or p.provenance.artifact_id != artifact.artifact_id
        or not locator_fits_media_type(p.provenance.locator, artifact.media_type)
        or p.provenance.locator.kind not in _SOURCE_LOCATORS[ctx.source.evidence_kind]
        or p.provenance.extraction.parser not in ctx.approved_parsers
        or _artifact_reasons(artifact, offer, ctx, primary=True)
    ):
        reasons.add(R.EVIDENCE_PATH_NON_AUTHORITATIVE)
    if not _field_excerpt_matches(offer, p.path, format(p.value, "f"), p.provenance.source_text):
        reasons.add(R.EVIDENCE_PATH_VALUE_MISMATCH)
    if p.product_id != offer.product_id:
        reasons.add(R.EVIDENCE_PATH_PRODUCT_MISMATCH)
    if (
        p.scenario_id is None
        or offer.scenario_id is None
        or p.scenario_id != offer.scenario_id
        or scenario_signature(p.scenario_statements) is None
        or scenario_signature(p.scenario_statements)
        != tuple(
            sorted(
                (m.dimension.value, m.stated.model_dump_json())
                for m in offer.scenario_matches
                if m.stated is not None and m.provenance is not None
            )
        )
        or any(
            proof not in ctx.verified_scenario_statements
            or proof.offer_id != offer.offer_id
            or proof.fetch_run_id != p.capture_id
            or proof.provenance.artifact_id != p.artifact_id
            or not statement_matches_excerpt(
                proof.dimension, proof.stated, proof.provenance.source_text
            )
            for proof in p.scenario_statements
        )
    ):
        reasons.add(R.EVIDENCE_PATH_SCENARIO_MISMATCH)
    # Both captures must be real past observations within the offer's trailing
    # observation window AND the existing 96-hour publication freshness bound.
    if (
        not offer.observed_at - DUAL_EVIDENCE_TIME_WINDOW <= p.observed_at <= offer.observed_at
        or not ctx.evaluated_at - WITHDRAW_AFTER <= p.observed_at <= ctx.evaluated_at
    ):
        reasons.add(R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW)
    return reasons


def _pair_reasons(p1: EvidencePath, p2: EvidencePath) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    # Separate guards deliberately: each identity is independently necessary.
    # D-20: identical bytes are fine in principle (a static page); what must differ is the fetch,
    # NOTE: the trusted loader and artifact lookups still key on artifact_id, so identical bytes
    # from two runs fail closed end to end until that lookup is capture-aware (issue filed).
    # and the two fetches must be far enough apart to be independent observations.
    if p1.capture_id == p2.capture_id:
        reasons.add(R.DUPLICATE_EVIDENCE_CAPTURE)
    if abs(p1.observed_at - p2.observed_at) < DUAL_EVIDENCE_MIN_SPACING:
        reasons.add(R.DUPLICATE_EVIDENCE_CAPTURE)
    if p1.derived_from or p2.derived_from:
        reasons.add(R.DUPLICATE_EVIDENCE_CAPTURE)
    if scenario_signature(p1.scenario_statements) != scenario_signature(p2.scenario_statements):
        reasons.add(R.EVIDENCE_PATH_SCENARIO_MISMATCH)
    if abs(p1.observed_at - p2.observed_at) > DUAL_EVIDENCE_TIME_WINDOW:
        reasons.add(R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW)
    return reasons


def corroborated_paths(offer: NormalizedOffer, ctx: PublicationContext) -> tuple[EvidencePath, ...]:
    """The deterministic valid pair per field; exports/reviews use exactly these paths."""
    selected: list[EvidencePath] = []
    for path, value, _ in financial_items(offer):
        # Canonical order: the selected pair, and the review digest bound to it, must not
        # depend on the order in which captures happen to be loaded.
        paths = sorted(
            (
                p
                for p in ctx.evidence_paths
                if p.path == path and p.value == value and not _path_reasons(p, offer, ctx)
            ),
            key=lambda p: (
                p.observed_at,
                p.capture_id,
                p.artifact_id,
                p.content_sha256,
                p.model_dump_json(),  # total order: ties can never fall back to input order
            ),
        )
        for i, first in enumerate(paths):
            second = next((p for p in paths[i + 1 :] if not _pair_reasons(first, p)), None)
            if second is not None:
                selected.extend((first, second))
                break
    return tuple(selected)


def _dual_evidence_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons: set[ReasonCode] = set()
    selected = {p.path for p in corroborated_paths(offer, ctx)}
    for path, value, _ in financial_items(offer):
        if path in selected:
            continue
        paths = [p for p in ctx.evidence_paths if p.path == path]
        field_reasons: set[ReasonCode] = set()
        if len(paths) < 2:
            field_reasons.add(R.INSUFFICIENT_EVIDENCE_PATHS)
        for i, p in enumerate(paths):
            field_reasons |= _path_reasons(p, offer, ctx)
            if p.value != value:
                field_reasons.add(R.EVIDENCE_PATH_VALUE_MISMATCH)
            for other in paths[i + 1 :]:
                field_reasons |= _pair_reasons(p, other)
        reasons |= field_reasons or {R.INSUFFICIENT_EVIDENCE_PATHS}
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
    # A recorded pass does not outrank any advertised pair, including browse-only tiers.
    pairs = [(offer.note_rate, offer.apr), *((tier.note_rate, tier.apr) for tier in offer.tiers)]
    if any(note is not None and apr is not None and apr.value < note.value for note, apr in pairs):
        reasons.add(R.VALIDATION_FAILED)
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
    reasons |= _dual_evidence_reasons(offer, ctx)
    if offer.material_paths() - {e.path for e in offer.field_provenance}:
        reasons.add(R.FIELD_EVIDENCE_MISSING)
    reasons |= _validation_reasons(offer, ctx)
    return reasons


def _field_text(offer: NormalizedOffer, path: str) -> str | None:
    """Canonical text of one offer field, or None when the path is not on the offer."""
    if path == "points" and offer.points is not None:
        return canonical_decimal(offer.points.value)
    rates = offer.rate_values()
    if path in rates:
        return canonical_decimal(rates[path])
    if path.startswith("details.") and path in offer.material_paths():
        value: object = offer.details
        for name in path.removeprefix("details.").split("."):
            value = getattr(value, name, None)
        if isinstance(value, Decimal):
            return canonical_decimal(value)
        if isinstance(value, str | int):
            return str(value)
        return None
    return None


def _same_stated_value(actual: str, claimed: str) -> bool:
    if actual == claimed:
        return True
    try:
        return Decimal(actual) == Decimal(claimed)
    except InvalidOperation:
        return False


def _correction_value_matches(offer: NormalizedOffer, path: str, corrected_value: str) -> bool:
    actual = _field_text(offer, path)
    return actual is not None and _field_excerpt_matches(offer, path, actual, corrected_value)


def _field_excerpt_matches(offer: NormalizedOffer, path: str, actual: str, excerpt: str) -> bool:
    if _same_stated_value(actual, excerpt):
        return True
    text = excerpt.lower()
    if path == "points":
        number = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:\s*%|\s+points?)", text)
        return number is not None and _same_stated_value(actual, number[1])
    if path in offer.rate_values() or path.endswith(("_cap", ".max_cltv")):
        number = re.fullmatch(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*%(?:\s+(?:apr|note rate|rate))?", text)
        return number is not None and _same_stated_value(actual, number[1])
    unit = (
        ScenarioDimension.TERM_MONTHS
        if path.endswith("_months")
        else ScenarioDimension.LOCK_DAYS
        if path.endswith(".lock_days")
        else ScenarioDimension.LOAN_AMOUNT
        if path.endswith((".min_amount", ".max_amount"))
        else None
    )
    return unit is not None and statement_matches_excerpt(
        unit, StatedBound(exact=Decimal(actual)), text
    )


def _correction_evidence_is_current(
    correction: Correction, offer: NormalizedOffer, ctx: PublicationContext
) -> bool:
    if not correction.evidence:
        return False
    path = correction.field_path
    actual = _field_text(offer, path)
    if actual is None:
        return False
    bound = {
        item for label, item in offer.iter_labeled_provenance() if label in (path, f"fields.{path}")
    }
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    for item in correction.evidence:
        capture = stored.get(item.artifact_id)
        if (
            item not in bound
            or not _field_excerpt_matches(offer, path, actual, item.source_text)
            or capture is None
            or capture.fetched_at != offer.observed_at
            or capture.fetched_at < correction.corrected_at
            or _artifact_reasons(capture, offer, ctx, primary=True)
        ):
            return False
    return True


def _proven_refresh(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    """True when a fully proven 304, and no unproven one, is this offer's observation."""
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    proven = False
    for item in offer.iter_provenance():
        artifact = stored.get(item.artifact_id)
        if artifact is None:
            continue
        claimed = [
            observation
            for observation in ctx.reobservations
            if observation.original_artifact_id == artifact.artifact_id
            and observation.observed_at == offer.observed_at
            and observation.observed_at != artifact.fetched_at
        ]
        if not claimed:
            continue
        if not all(
            _reobservation_proven(observation, artifact, offer, ctx) for observation in claimed
        ):
            return False
        proven = True
    return proven


def _claims_extend(
    offer: NormalizedOffer, prior: PriorPublication, ctx: PublicationContext
) -> bool:
    """A 304 extends only the same claims with the exact evidence the human reviewed."""
    review = prior.review
    return (
        review is not None
        and review.approved
        and review.reviewed_evidence_sha256
        == publication_evidence_sha256(offer, corroborated_paths(offer, ctx))
        and offer_fingerprints(offer) == prior.snapshot.fingerprints
    )


def _layout_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    """Require the layout digest reviewed for this source and adapter.

    T040/T045 compute ``structure_sha256`` and name the adapter. T014 stores that pair
    on the human ``SourceApproval`` and passes the artifact's ``SourceFingerprint``.
    This function does not parse HTML. A missing record, a different adapter, or a
    layout hash other than the one reviewed goes to review.
    """
    approval = ctx.source_approval
    if (
        approval is None
        or approval.approved_layout_sha256 is None
        or approval.adapter is None
        or approval.adapter not in ctx.approved_parsers
    ):
        return {R.LAYOUT_UNREVIEWED}
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    cited = {
        item.artifact_id
        for item in offer.iter_provenance()
        if (artifact := stored.get(item.artifact_id)) is not None
        and artifact.source_url == ctx.source.url
    }
    if not cited:
        return {R.LAYOUT_UNREVIEWED}
    if any(item.extraction.parser != approval.adapter for item in offer.iter_provenance()):
        return {R.LAYOUT_UNREVIEWED}
    for artifact_id in cited:
        artifact = stored[artifact_id]
        match = next(
            (
                fingerprint
                for fingerprint in ctx.source_fingerprints
                if fingerprint.source_id == offer.source_id
                and fingerprint.artifact_id == artifact_id
                and fingerprint.fetch_run_id == artifact.fetch_run_id
                and fingerprint.content_sha256 == artifact.artifact_id
                and fingerprint.structure_sha256 == approval.approved_layout_sha256
                and fingerprint.adapter == approval.adapter
                and fingerprint.observed_at == artifact.fetched_at <= ctx.evaluated_at
            ),
            None,
        )
        if match is None:
            return {R.LAYOUT_UNREVIEWED}
    return set()


def _corrections_cleared(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    """Each correction needs a matching content-bound record or a later review of this state."""
    current = offer_fingerprints(offer)
    review = ctx.prior.review if ctx.prior is not None else None
    reviewed_current = (
        review is not None
        and review.approved
        and review.reviewed_fingerprints == current
        # Protocol 2: a review binds the corroborated evidence pair (the same digest every other
        # human-review check uses), not the bare offer evidence of protocol 1.
        and review.reviewed_evidence_sha256
        == publication_evidence_sha256(offer, corroborated_paths(offer, ctx))
        and review.subject_id == offer.offer_id
        and review.decided_at <= ctx.evaluated_at
    )
    for correction in ctx.corrections:
        if correction.offer_id != offer.offer_id:
            continue
        if correction.corrected_at > ctx.evaluated_at:
            return False
        # Neither a fingerprint nor a review substitutes for new evidence of the replacement.
        if not _correction_value_matches(offer, correction.field_path, correction.corrected_value):
            return False
        if not _correction_evidence_is_current(correction, offer, ctx):
            return False
        if correction.corrected_fingerprints != current and not (
            reviewed_current and review is not None and review.decided_at >= offer.observed_at
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
    if _proven_refresh(offer, ctx) and not _claims_extend(offer, prior, ctx):
        reasons.add(R.REOBSERVATION_CLAIM_CHANGED)
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


def _verified_scenario_matches(
    offer: NormalizedOffer, ctx: PublicationContext
) -> tuple[tuple[DimensionMatch, ...], set[ReasonCode]]:
    """Rank only independently verified statements tied to the current rate capture."""
    assert offer.scenario_id is not None
    stored = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    verified = {
        (proof.dimension, proof.fetch_run_id): proof
        for proof in ctx.verified_scenario_statements
        if proof.offer_id == offer.offer_id
        and any(m.provenance == proof.provenance for m in offer.scenario_matches)
    }
    rate_runs = {
        capture.fetch_run_id
        for _, rate in offer.rate_items()
        if (capture := stored.get(rate.provenance.artifact_id)) is not None
    }
    scenario = CANONICAL_SCENARIOS[offer.scenario_id]
    reasons: set[ReasonCode] = set()
    adjusted: list[DimensionMatch] = []
    for match in offer.scenario_matches:
        if match.state is not MatchState.MATCHES:
            adjusted.append(match)
            continue
        provenance = match.provenance
        capture = stored.get(provenance.artifact_id) if provenance is not None else None
        proof = (
            verified.get((match.dimension, capture.fetch_run_id)) if capture is not None else None
        )
        if (
            match.stated is None
            or provenance is None
            or capture is None
            or proof is None
            or proof.provenance != provenance
            or proof.stated != match.stated
            or proof.fetch_run_id != capture.fetch_run_id
            or capture.fetch_run_id not in rate_runs
            or match.source_text != provenance.source_text
            or not statement_matches_excerpt(match.dimension, match.stated, provenance.source_text)
            or _reobservation_basis(capture, offer, ctx) != (offer.observed_at, False)
        ):
            if provenance is not None and provenance.artifact_id not in stored:
                reasons.add(R.ARTIFACT_NOT_STORED)
            adjusted.append(
                match.model_copy(update={"state": MatchState.UNSTATED, "source_text": None})
            )
            continue
        computed = computed_match_state(scenario, match.stated, match.dimension)
        source_text = None if computed is MatchState.UNSTATED else match.source_text
        adjusted.append(match.model_copy(update={"state": computed, "source_text": source_text}))
    return tuple(adjusted), reasons


def _verified_conditions(offer: NormalizedOffer, ctx: PublicationContext) -> bool:
    captures = {artifact.artifact_id: artifact for artifact in ctx.known_artifacts}
    rate_runs = {
        capture.fetch_run_id
        for _, rate in offer.rate_items()
        if (capture := captures.get(rate.provenance.artifact_id)) is not None
    }
    proofs = {
        item.condition.condition_id: item
        for item in ctx.verified_scenario_conditions
        if item.offer_id == offer.offer_id and item.scenario_id == offer.scenario_id
    }
    for condition in offer.conditions:
        proven = proofs.get(condition.condition_id)
        capture = captures.get(condition.provenance.artifact_id)
        if (
            condition.scenario_state is not MatchState.MATCHES
            or proven is None
            or proven.condition != condition
            or capture is None
            or proven.fetch_run_id != capture.fetch_run_id
            or proven.fetch_run_id not in rate_runs
            or _reobservation_basis(capture, offer, ctx) != (offer.observed_at, False)
        ):
            return False
    return True


def _placement_reasons(offer: NormalizedOffer, ctx: PublicationContext) -> set[ReasonCode]:
    reasons = _tier_reasons(offer)
    product, details = ctx.product, offer.details
    if isinstance(details, HelocDetails):
        if details.post_intro().state is PostIntroState.INCOMPLETE:
            reasons.add(R.HELOC_POST_INTRO_INCOMPLETE)
        if details.post_intro_conflicts():
            reasons.add(R.HELOC_POST_INTRO_CONFLICT)
        if details.draw_period_months is None or details.repayment_period_months is None:
            reasons.add(R.HELOC_PERIODS_INCOMPLETE)
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
    if not _verified_conditions(offer, ctx):
        reasons.add(R.CONDITION_NOT_SATISFIED)
    if offer.scenario_id is None:
        reasons.add(R.SCENARIO_NOT_DECLARED)
        return reasons
    scenario = CANONICAL_SCENARIOS[offer.scenario_id]
    verified, evidence_gaps = _verified_scenario_matches(offer, ctx)
    reasons |= evidence_gaps
    overall = evaluate_scenario_match(offer.scenario_id, verified)
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
    reasons |= _layout_reasons(offer, ctx)
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

"""Domain models for offline model/operator rate batch intake (contract version 1.0).

Strict Pydantic v2 schemas for the batch envelope, observations, candidates,
findings, conditions, tiers, independent extraction checks, and hold/reject reasons.
Binary floats are rejected; all financial decimals use Decimal or decimal strings.
Timestamps require UTC awareness. Attempts to set publication/approval/confidence
are forbidden.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    model_validator,
)

from cu_rates.contracts.base import (
    CharterNumber,
    ContractModel,
    CreditScore,
    Days,
    FieldPath,
    HttpUrlText,
    Months,
    SchemaVersion,
    Sha256Hex,
    Text,
    UtcDatetime,
    VersionedModel,
    _reject_float,
)
from cu_rates.contracts.enums import (
    ConditionKind,
    ConformingStatus,
    LienPosition,
    LoanPurpose,
    Occupancy,
    OutOfScopeProduct,
    ProductFamily,
    ProductScope,
    PropertyType,
    RateAvailability,
    TierDimension,
)
from cu_rates.contracts.evidence import EvidenceLocator
from cu_rates.contracts.offers import ArmTerms
from cu_rates.contracts.taxonomy import PURPOSES_BY_FAMILY

BATCH_SCHEMA_VERSION = "1.0"
MAX_BATCH_BYTES = 10 * 1024 * 1024  # 10 MiB raw payload limit
MAX_BATCH_ITEMS = 5000

FORBIDDEN_PUBLICATION_KEYS = frozenset(
    {
        "status",
        "published",
        "published_at",
        "approved",
        "approval_id",
        "approved_at",
        "decision",
        "confidence",
        "review_verdict",
        "publish",
        "is_published",
        "publication_decision",
        "review_state",
        "gate_verdict",
        "publication_status",
    }
)


class BatchError(Exception):
    """Base exception for rate batch processing."""


class OversizedInputError(BatchError):
    """Batch payload exceeds byte or item count boundaries."""


class HashTamperingError(BatchError):
    """Envelope digest or artifact hash does not match computed content."""


class CrossReferenceError(BatchError):
    """Inconsistent references across charter, product, source, or capture."""


class ForbiddenFieldError(BatchError):
    """Attempted to inject publication, approval, or confidence state."""


class BatchConflictError(BatchError):
    """Batch ID already exists with different contents."""


class BatchDisposition(StrEnum):
    """Auditable disposition of an imported item."""

    STAGED = "staged"
    HELD = "held"
    REJECTED = "rejected"


class BatchItemType(StrEnum):
    CANDIDATE = "candidate"
    FINDING = "finding"


class HoldReason(StrEnum):
    """Auditable stable reason codes for held rows."""

    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    LOCATOR_UNVERIFIED = "locator_unverified"
    LOCATOR_UNSUPPORTED = "locator_unsupported"
    EVIDENCE_AMBIGUOUS = "evidence_ambiguous"
    EVIDENCE_MISSING = "evidence_missing"
    RAW_TEXT_MISMATCH = "raw_text_mismatch"
    UNGROUNDED_CLAIM = "ungrounded_claim"
    INDEPENDENT_REVIEW_DISAGREEMENT = "independent_review_disagreement"


class RejectReason(StrEnum):
    """Auditable reason codes for rejected rows or batches."""

    CROSS_REFERENCE_MISMATCH = "cross_reference_mismatch"
    INSTITUTION_NOT_FOUND = "institution_not_found"
    PRODUCT_NOT_FOUND = "product_not_found"
    SOURCE_NOT_FOUND = "source_not_found"
    RELATIONSHIP_NOT_FOUND = "relationship_not_found"
    FETCH_RUN_NOT_FOUND = "fetch_run_not_found"
    HASH_TAMPERING = "hash_tampering"
    FLOAT_PROHIBITED = "float_prohibited"
    DATETIME_INVALID = "datetime_invalid"
    MALFORMED_INPUT = "malformed_input"
    OVERSIZED_INPUT = "oversized_input"
    PATH_ESCAPE = "path_escape"
    FORBIDDEN_PUBLICATION_FIELDS = "forbidden_publication_fields"
    BATCH_ID_CONFLICT = "batch_id_conflict"


def _check_no_path_escape(value: str) -> str:
    if ".." in value or "\\ " in value or "\x00" in value:
        raise ValueError(f"path traversal or null byte detected: {value!r}")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError(f"absolute path escape detected: {value!r}")
    return value


def _check_text_safe(value: str) -> str:
    if "\x00" in value:
        raise ValueError(f"prohibited null byte in text: {value!r}")
    if ".." in value:
        raise ValueError(f"path traversal sequence detected in text: {value!r}")
    return value


SafeIdentifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$"),
    AfterValidator(_check_no_path_escape),
]

SafeText = Annotated[Text, AfterValidator(_check_text_safe)]

StrictDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class BatchCondition(ContractModel):
    """A qualifier or condition proposed on an offer candidate."""

    kind: ConditionKind
    description: Text
    raw_text: Text
    locator: EvidenceLocator | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in condition: {key!r}")
        return data


class BatchTierBound(ContractModel):
    """One constrained axis of a rate tier."""

    dimension: TierDimension
    lower: StrictDecimal | None = None
    upper: StrictDecimal | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if self.lower is None and self.upper is None:
            raise ValueError("a tier bound needs at least one end")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("tier lower bound exceeds upper bound")
        return self


class BatchTier(ContractModel):
    """A tiered rate or APR corresponding to specific borrower constraints."""

    tier_id: SafeIdentifier
    bounds: tuple[BatchTierBound, ...]
    note_rate: StrictDecimal | None = None
    apr: StrictDecimal | None = None
    points: StrictDecimal | None = None
    raw_text: Text
    locator: EvidenceLocator | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in tier: {key!r}")
        return data


class BatchHelocTerms(ContractModel):
    """HELOC-specific attributes and post-intro terms."""

    intro_rate: StrictDecimal | None = None
    intro_duration_months: Months | None = None
    index_name: Text | None = None
    index_rate: StrictDecimal | None = None
    margin: StrictDecimal | None = None  # Signed decimal
    floor: StrictDecimal | None = None
    ceiling: StrictDecimal | None = None
    stated_post_intro_apr: StrictDecimal | None = None
    draw_period_months: Months | None = None
    repayment_period_months: Months | None = None
    max_cltv: StrictDecimal | None = None
    lien_position: LienPosition | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(
                        f"forbidden publication/approval field in heloc_terms: {key!r}"
                    )
        return data


class BatchArmTerms(ContractModel):
    """Adjustable-rate mortgage terms for offline intake."""

    initial_period_months: Months | None = None
    adjustment_interval_months: Months | None = None
    index_name: Text | None = None
    margin: StrictDecimal | None = None
    initial_cap: StrictDecimal | None = None
    periodic_cap: StrictDecimal | None = None
    lifetime_cap: StrictDecimal | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in arm_terms: {key!r}")
        return data


class BatchObservation(ContractModel):
    """One distinct capture event cited by the batch."""

    institution_charter: CharterNumber
    source_id: SafeIdentifier
    source_url: HttpUrlText
    product_id: SafeIdentifier | None = None
    fetch_run_id: SafeIdentifier
    observed_at: UtcDatetime
    artifact_id: Sha256Hex
    media_type: Text

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(
                        f"forbidden publication/approval field in observation: {key!r}"
                    )
        return data


class BatchIndependentReview(ContractModel):
    """Audit record from a second model or human cross-checking the artifact."""

    reviewer_model: SafeText
    recipe_version: SafeText
    reviewed_at: UtcDatetime
    concurrence: bool = True
    agreed_fields: tuple[Text, ...] = ()
    disagreements: tuple[Text, ...] = ()
    notes: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in review: {key!r}")
        return data


class BatchCandidate(ContractModel):
    """One candidate rate offer extracted from offline evidence.

    Stays strictly candidate-only: carries no approval, publication, or confidence flags.
    Preserves missing (None) versus explicitly zero (Decimal("0")) points.
    """

    candidate_id: SafeIdentifier
    row_id: SafeIdentifier
    institution_charter: CharterNumber
    product_id: SafeIdentifier
    source_id: SafeIdentifier
    fetch_run_id: SafeIdentifier
    artifact_id: Sha256Hex
    product_family: ProductFamily
    loan_purpose: LoanPurpose | None = None
    term_months: Months | None = None
    note_rate: StrictDecimal | None = None
    apr: StrictDecimal | None = None
    points: StrictDecimal | None = None
    points_not_applicable: bool = False
    headline_is_as_low_as: bool | None = None
    conforming_status: ConformingStatus = ConformingStatus.UNKNOWN
    property_type: PropertyType | None = None
    occupancy: Occupancy | None = None
    lien_position: LienPosition | None = None
    max_cltv: StrictDecimal | None = None
    min_amount: StrictDecimal | None = None
    max_amount: StrictDecimal | None = None
    credit_score_min: CreditScore | None = None
    lock_days: Days | None = None
    arm_terms: BatchArmTerms | ArmTerms | None = None
    heloc_terms: BatchHelocTerms | None = None
    conditions: tuple[BatchCondition, ...] = ()
    tiers: tuple[BatchTier, ...] = ()
    raw_text: Text
    field_raw_texts: dict[FieldPath, Text] = Field(default_factory=dict)
    locators: dict[FieldPath, EvidenceLocator] = Field(default_factory=dict)
    observed_at: UtcDatetime
    effective_at: UtcDatetime | None = None
    imported_at: UtcDatetime | None = None
    independent_review: BatchIndependentReview | None = None

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in input: {key!r}")
        return data

    @model_validator(mode="after")
    def _purpose_belongs_to_family(self) -> Self:
        if self.points_not_applicable and self.points is not None:
            raise ValueError("not-applicable points cannot also have a numeric value")
        allowed = PURPOSES_BY_FAMILY.get(self.product_family, frozenset())
        if self.loan_purpose is not None and self.loan_purpose not in allowed:
            raise ValueError(
                f"{self.product_family.value} cannot have loan_purpose {self.loan_purpose.value}; "
                "purposes are mortgage-only"
            )
        return self


class BatchFinding(ContractModel):
    """What a page established about coverage without proposing a rate.

    Records quote-only, no-public-rate, login-required, or blocked findings.
    """

    finding_id: SafeIdentifier
    institution_charter: CharterNumber
    source_id: SafeIdentifier
    fetch_run_id: SafeIdentifier
    artifact_id: Sha256Hex
    observed_at: UtcDatetime
    label: Text
    scope: ProductScope
    family: ProductFamily | None = None
    purposes: tuple[LoanPurpose, ...] = ()
    out_of_scope_product: OutOfScopeProduct | None = None
    rate_availability: RateAvailability | None = None
    raw_text: Text
    field_raw_texts: dict[FieldPath, Text] = Field(default_factory=dict)
    locators: dict[FieldPath, EvidenceLocator] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _forbid_publication_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in input: {key!r}")
        return data

    @model_validator(mode="after")
    def _validate_scope_consistency(self) -> Self:
        if self.scope is ProductScope.IN_SCOPE:
            if self.family is None or self.rate_availability is None:
                raise ValueError("in-scope finding requires family and rate_availability")
            if self.out_of_scope_product is not None:
                raise ValueError("in-scope finding cannot carry out_of_scope_product")
        elif self.scope is ProductScope.OUT_OF_SCOPE:
            if self.out_of_scope_product is None:
                raise ValueError("out-of-scope finding requires out_of_scope_product")
            if self.family is not None or self.rate_availability is not None:
                raise ValueError("out-of-scope finding cannot carry family or rate_availability")
        elif self.scope is ProductScope.UNKNOWN and (
            self.out_of_scope_product is not None or self.family is not None
        ):
            raise ValueError("unknown finding cannot specify family or out_of_scope_product")
        return self


class RateBatchEnvelope(VersionedModel):
    """The root envelope for an offline rate batch file (JSON or JSONL header)."""

    schema_version: SchemaVersion
    batch_id: SafeIdentifier
    batch_digest: Sha256Hex
    operator: SafeText
    model_name: SafeText
    tool_name: SafeText
    recipe_version: SafeText
    prompt_version: SafeText | None = None
    submitted_at: UtcDatetime
    observations: tuple[BatchObservation, ...] = ()
    candidates: tuple[BatchCandidate, ...] = ()
    findings: tuple[BatchFinding, ...] = ()
    independent_review: BatchIndependentReview | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_forbidden(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).lower() in FORBIDDEN_PUBLICATION_KEYS:
                    raise ValueError(f"forbidden publication/approval field in envelope: {key!r}")
        return data

    @model_validator(mode="after")
    def _check_item_limits(self) -> Self:
        total = len(self.candidates) + len(self.findings)
        if total > MAX_BATCH_ITEMS:
            raise OversizedInputError(
                f"batch contains {total} items, exceeding maximum of {MAX_BATCH_ITEMS}"
            )
        return self


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "FORBIDDEN_PUBLICATION_KEYS",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_ITEMS",
    "BatchArmTerms",
    "BatchCandidate",
    "BatchCondition",
    "BatchConflictError",
    "BatchDisposition",
    "BatchError",
    "BatchFinding",
    "BatchHelocTerms",
    "BatchIndependentReview",
    "BatchItemType",
    "BatchObservation",
    "BatchTier",
    "BatchTierBound",
    "CrossReferenceError",
    "ForbiddenFieldError",
    "HashTamperingError",
    "HoldReason",
    "OversizedInputError",
    "RateBatchEnvelope",
    "RejectReason",
    "SafeIdentifier",
    "SafeText",
    "StrictDecimal",
]

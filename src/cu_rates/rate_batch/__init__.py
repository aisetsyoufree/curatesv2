"""Offline rate batch intake package (M4).

Provides bounded, versioned envelope parsing, locator verification against the
private content-addressed store, and candidate-only staging in the operational database.
"""

from cu_rates.rate_batch.locators import (
    resolve_json_pointer,
    verify_batch_item_locators,
)
from cu_rates.rate_batch.models import (
    BATCH_SCHEMA_VERSION,
    MAX_BATCH_BYTES,
    MAX_BATCH_ITEMS,
    BatchCandidate,
    BatchCondition,
    BatchConflictError,
    BatchDisposition,
    BatchError,
    BatchFinding,
    BatchHelocTerms,
    BatchIndependentReview,
    BatchItemType,
    BatchObservation,
    BatchTier,
    BatchTierBound,
    CrossReferenceError,
    ForbiddenFieldError,
    HashTamperingError,
    HoldReason,
    OversizedInputError,
    RateBatchEnvelope,
    RejectReason,
)
from cu_rates.rate_batch.parser import (
    compute_batch_digest,
    parse_rate_batch,
)
from cu_rates.rate_batch.staging import (
    BatchImportReport,
    RateBatchStager,
)

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_ITEMS",
    "BatchCandidate",
    "BatchCondition",
    "BatchConflictError",
    "BatchDisposition",
    "BatchError",
    "BatchFinding",
    "BatchHelocTerms",
    "BatchImportReport",
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
    "RateBatchStager",
    "RejectReason",
    "compute_batch_digest",
    "parse_rate_batch",
    "resolve_json_pointer",
    "verify_batch_item_locators",
]

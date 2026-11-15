"""Immutable audit records: who decided, withdrew, corrected, or published what, and when.

These are inputs to deterministic publication, never outputs of an LLM: every record names
a human ``actor`` and a UTC time, and review decisions are bound to the exact content
fingerprints they approved so an approval cannot be reused for a changed offer.
"""

import hashlib
from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator

from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    Identifier,
    Sha256Hex,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import ReviewSubject, ReviewVerdict, WithdrawalScope
from cu_rates.contracts.evidence import Provenance
from cu_rates.contracts.fingerprints import OfferFingerprints


class ReviewDecision(VersionedModel):
    """A human verdict on an offer (bound to its fingerprints) or on a product."""

    review_id: Identifier
    subject: ReviewSubject
    subject_id: Identifier
    verdict: ReviewVerdict
    actor: Text
    decided_at: UtcDatetime
    rationale: Text
    evidence: tuple[Provenance, ...] = ()
    reviewed_fingerprints: OfferFingerprints | None = None
    reviewed_product_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def _reviews_are_content_bound(self) -> Self:
        is_offer = self.subject is ReviewSubject.OFFER
        if is_offer and self.reviewed_fingerprints is None:
            raise ValueError("an offer review must record the fingerprints it reviewed")
        if not is_offer and self.reviewed_fingerprints is not None:
            raise ValueError("only offer reviews carry fingerprints")
        if not is_offer and self.reviewed_product_sha256 is None:
            raise ValueError("a product review must record the product content digest")
        if is_offer and self.reviewed_product_sha256 is not None:
            raise ValueError("only product reviews carry a product digest")
        return self

    @property
    def approved(self) -> bool:
        return self.verdict is ReviewVerdict.APPROVED


class WithdrawalEntry(VersionedModel):
    """Emergency denylist entry suppressing an offer, product, institution, or snapshot.

    ``target_id`` is the offer/product/snapshot identifier, or the charter number as text
    for institution scope. An entry is in force from ``withdrawn_at`` until ``lifted_at``.
    """

    withdrawal_id: Identifier
    scope: WithdrawalScope
    target_id: Identifier
    actor: Text
    withdrawn_at: UtcDatetime
    reason: Text
    lifted_at: UtcDatetime | None = None
    lifted_by: Text | None = None

    @model_validator(mode="after")
    def _lift_is_complete_and_later(self) -> Self:
        if (self.lifted_at is None) != (self.lifted_by is None):
            raise ValueError("lifted_at and lifted_by must be set together")
        if self.lifted_at is not None and self.lifted_at <= self.withdrawn_at:
            raise ValueError("a withdrawal must be lifted after it was made")
        return self

    def in_force_at(self, moment: UtcDatetime) -> bool:
        return self.withdrawn_at <= moment and (self.lifted_at is None or moment < self.lifted_at)


class Correction(VersionedModel):
    """Public correction of a published value; the prior text is retained, never erased.

    Without ``corrected_fingerprints`` the correction is not bound to any content, so
    publication requires a fresh review of the corrected state.
    """

    correction_id: Identifier
    offer_id: Identifier
    field_path: FieldPath
    previous_value: Text
    corrected_value: Text
    actor: Text
    corrected_at: UtcDatetime
    reason: Text
    evidence: tuple[Provenance, ...] = ()
    corrected_fingerprints: OfferFingerprints | None = None  # content-binds the corrected state


class SnapshotEntry(ContractModel):
    offer_id: Identifier
    structural_sha256: Sha256Hex
    value_sha256: Sha256Hex


def snapshot_manifest_sha256(entries: tuple[SnapshotEntry, ...]) -> str:
    """Digest of the sorted entries; the snapshot's integrity anchor."""
    lines = (f"{e.offer_id}:{e.structural_sha256}:{e.value_sha256}" for e in entries)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _sorted_unique_entries(entries: tuple[SnapshotEntry, ...]) -> tuple[SnapshotEntry, ...]:
    ordered = tuple(sorted(entries, key=lambda e: e.offer_id))
    if len({e.offer_id for e in ordered}) != len(ordered):
        raise ValueError("duplicate offer in snapshot")
    return ordered


class PublishedSnapshot(VersionedModel):
    """An atomic public snapshot: exactly which offer contents were live, and who published."""

    snapshot_id: Identifier
    published_at: UtcDatetime
    published_by: Text
    entries: Annotated[
        tuple[SnapshotEntry, ...], Field(min_length=1), AfterValidator(_sorted_unique_entries)
    ]
    manifest_sha256: Sha256Hex
    supersedes: Identifier | None = None
    review_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _manifest_matches_entries(self) -> Self:
        if self.manifest_sha256 != snapshot_manifest_sha256(self.entries):
            raise ValueError("manifest_sha256 does not match the snapshot entries")
        if self.supersedes == self.snapshot_id:
            raise ValueError("a snapshot cannot supersede itself")
        return self

"""Institutions, membership, providers, source relationships, and product identity."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from cu_rates.contracts.base import (
    CharterNumber,
    HttpUrlText,
    Identifier,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import (
    BorrowerProgram,
    ConformingStatus,
    GovernmentGuaranty,
    InstitutionStatus,
    MembershipKind,
    PartnerProgram,
    ProductFamily,
    ProductLifecycle,
    ProviderKind,
    RateAvailability,
    SourceRelationshipKind,
)
from cu_rates.contracts.evidence import Provenance


class Institution(VersionedModel):
    """A credit union keyed by NCUA charter number."""

    charter_number: CharterNumber
    legal_name: Text
    status: InstitutionStatus
    successor_charter_number: CharterNumber | None = None
    charter_verified: bool

    @model_validator(mode="after")
    def _successor_iff_merged(self) -> Self:
        merged = self.status is InstitutionStatus.MERGED
        if merged and self.successor_charter_number is None:
            raise ValueError("a merged institution must record its successor charter")
        if not merged and self.successor_charter_number is not None:
            raise ValueError("only a merged institution may have a successor charter")
        if self.successor_charter_number == self.charter_number:
            raise ValueError("successor charter must differ from the institution's own")
        return self

    @property
    def may_publish(self) -> bool:
        return self.status is InstitutionStatus.ACTIVE and self.charter_verified


class MembershipRule(VersionedModel):
    """Who may join; separate from the institution record and from product eligibility."""

    membership_rule_id: Identifier
    institution_charter: CharterNumber
    kind: MembershipKind
    description: Text
    source_url: HttpUrlText | None = None


class ServiceProvider(VersionedModel):
    """A third party that hosts or publishes rates on an institution's behalf."""

    provider_id: Identifier
    name: Text
    kind: ProviderKind


class InstitutionSourceRelationship(VersionedModel):
    """Human-approved binding of a source to an institution (and optionally product/provider)."""

    relationship_id: Identifier
    institution_charter: CharterNumber
    source_id: Identifier
    product_id: Identifier | None = None
    provider_id: Identifier | None = None
    kind: SourceRelationshipKind
    approved_by: Text | None = None
    approved_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _provider_matches_kind_and_approval_complete(self) -> Self:
        hosted = self.kind is not SourceRelationshipKind.FIRST_PARTY
        if hosted and self.provider_id is None:
            raise ValueError("provider-hosted or partner relationships require a provider_id")
        if not hosted and self.provider_id is not None:
            raise ValueError("a first-party relationship cannot name a provider")
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("approved_by and approved_at must be set together")
        return self

    @property
    def verified(self) -> bool:
        return self.approved_by is not None


class ProductIdentity(VersionedModel):
    """A cataloged product. Classification, guaranty, and programs are independent axes."""

    product_id: Identifier
    institution_charter: CharterNumber
    family: ProductFamily
    name: Text
    lifecycle: ProductLifecycle
    rate_availability: RateAvailability
    conforming_status: ConformingStatus
    government_guaranty: GovernmentGuaranty
    borrower_programs: Annotated[tuple[BorrowerProgram, ...], SortedUnique] = ()
    partner_programs: Annotated[tuple[PartnerProgram, ...], SortedUnique] = ()
    notes: Annotated[tuple[Text, ...], Field(max_length=20)] = ()

    @property
    def is_conventional(self) -> bool:
        """No guaranty, no borrower or partner program restricting eligibility."""
        return (
            self.government_guaranty is GovernmentGuaranty.NONE
            and not self.borrower_programs
            and not self.partner_programs
        )


class InstitutionIdentityReview(VersionedModel):
    """A human's evidence-backed decision about who an institution is and where it operates.

    ``Institution.charter_verified`` and a relationship's ``approved_by`` are bare flags.
    This record is the proof behind them: it names the charter and legal name that were
    confirmed against an official source, states whether the institution is in scope for
    this Massachusetts directory, and cites located evidence that can be re-verified
    against stored bytes. Publication requires a current, approving review.
    """

    review_id: Identifier
    institution_charter: CharterNumber
    legal_name: Text
    identity_confirmed: bool
    massachusetts_scope_confirmed: bool
    evidence: Annotated[tuple[Provenance, ...], Field(min_length=1, max_length=20)]
    reviewer: Text
    decided_at: UtcDatetime
    rationale: Text

    @property
    def approves(self) -> bool:
        return self.identity_confirmed and self.massachusetts_scope_confirmed

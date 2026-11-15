import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    BorrowerProgram,
    ConformingStatus,
    GovernmentGuaranty,
    InstitutionStatus,
    MembershipKind,
    MembershipRule,
    PartnerProgram,
    ProviderKind,
    RateAvailability,
    ServiceProvider,
    SourceRelationshipKind,
)

from .factories import CHARTER, SV, T0, institution, product, relationship


def test_active_verified_institution_may_publish() -> None:
    assert institution().may_publish


@pytest.mark.parametrize(
    "status",
    [InstitutionStatus.LIQUIDATED, InstitutionStatus.CONVERTED, InstitutionStatus.UNDER_REVIEW],
)
def test_non_active_institution_may_not_publish(status: InstitutionStatus) -> None:
    assert not institution(status=status).may_publish


def test_unverified_charter_may_not_publish() -> None:
    assert not institution(charter_verified=False).may_publish


def test_merged_institution_requires_successor() -> None:
    with pytest.raises(ValidationError, match="successor"):
        institution(status=InstitutionStatus.MERGED)
    merged = institution(status=InstitutionStatus.MERGED, successor_charter_number=999)
    assert merged.successor_charter_number == 999
    assert not merged.may_publish


def test_successor_only_allowed_when_merged() -> None:
    with pytest.raises(ValidationError, match="only a merged"):
        institution(successor_charter_number=999)


def test_successor_cannot_be_self() -> None:
    with pytest.raises(ValidationError, match="differ"):
        institution(status=InstitutionStatus.MERGED, successor_charter_number=CHARTER)


@pytest.mark.parametrize("charter", [0, -1, 10**8])
def test_charter_number_bounds(charter: int) -> None:
    with pytest.raises(ValidationError):
        institution(charter_number=charter)


def test_membership_is_separate_from_institution_and_product() -> None:
    rule = MembershipRule(
        schema_version=SV,
        membership_rule_id="mem-1",
        institution_charter=CHARTER,
        kind=MembershipKind.GEOGRAPHIC,
        description="Live, work, or worship in a synthetic county",
    )
    assert rule.kind is MembershipKind.GEOGRAPHIC
    assert "membership" not in type(institution()).model_fields
    assert "membership" not in type(product()).model_fields


def test_first_party_relationship_has_no_provider() -> None:
    assert relationship().verified
    with pytest.raises(ValidationError, match="first-party"):
        relationship(provider_id="prov-1")


def test_provider_hosted_relationship_requires_provider() -> None:
    with pytest.raises(ValidationError, match="provider_id"):
        relationship(kind=SourceRelationshipKind.PROVIDER_HOSTED)
    hosted = relationship(kind=SourceRelationshipKind.PROVIDER_HOSTED, provider_id="prov-1")
    assert hosted.provider_id == "prov-1"
    provider = ServiceProvider(
        schema_version=SV,
        provider_id="prov-1",
        name="Synthetic Platform",
        kind=ProviderKind.LOAN_PLATFORM,
    )
    assert provider.provider_id == hosted.provider_id


def test_relationship_approval_fields_pair_up() -> None:
    with pytest.raises(ValidationError, match="together"):
        relationship(approved_at=None)
    with pytest.raises(ValidationError, match="together"):
        relationship(approved_by=None)
    unapproved = relationship(approved_by=None, approved_at=None)
    assert not unapproved.verified
    assert relationship(approved_at=T0).verified


def test_product_identity_axes_are_independent() -> None:
    fha_jumbo = product(
        conforming_status=ConformingStatus.JUMBO, government_guaranty=GovernmentGuaranty.FHA
    )
    assert fha_jumbo.conforming_status is ConformingStatus.JUMBO
    assert fha_jumbo.government_guaranty is GovernmentGuaranty.FHA
    assert not fha_jumbo.is_conventional


def test_conventional_requires_no_guaranty_or_programs() -> None:
    assert product().is_conventional
    assert not product(government_guaranty=GovernmentGuaranty.UNKNOWN).is_conventional
    assert not product(borrower_programs=(BorrowerProgram.FIRST_TIME_BUYER,)).is_conventional
    assert not product(partner_programs=(PartnerProgram.MASSHOUSING,)).is_conventional


def test_programs_are_deduplicated() -> None:
    dup = (BorrowerProgram.FIRST_TIME_BUYER, BorrowerProgram.FIRST_TIME_BUYER)
    assert product(borrower_programs=dup).borrower_programs == (BorrowerProgram.FIRST_TIME_BUYER,)


def test_explicit_absence_states_are_representable() -> None:
    for state in (
        RateAvailability.QUOTE_ONLY,
        RateAvailability.LOGIN_REQUIRED,
        RateAvailability.BLOCKED,
        RateAvailability.NO_PUBLIC_RATE,
        RateAvailability.NOT_OFFERED,
    ):
        assert product(rate_availability=state).rate_availability is state


def test_guaranty_and_conforming_have_no_implicit_default() -> None:
    with pytest.raises(ValidationError):
        type(product()).model_validate(
            {
                "product_id": "p",
                "institution_charter": 1,
                "family": "mortgage",
                "name": "x",
                "lifecycle": "active",
                "rate_availability": "public_rate",
            }
        )


@pytest.mark.parametrize(
    "url", ["ftp://example.invalid/x", "example.invalid", "https://", "http://a b"]
)
def test_membership_source_url_uses_http_url_type(url: str) -> None:
    with pytest.raises(ValidationError):
        MembershipRule(
            schema_version=SV,
            membership_rule_id="mem-1",
            institution_charter=CHARTER,
            kind=MembershipKind.OPEN,
            description="Anyone may join.",
            source_url=url,
        )
    ok = MembershipRule(
        schema_version=SV,
        membership_rule_id="mem-1",
        institution_charter=CHARTER,
        kind=MembershipKind.OPEN,
        description="Anyone may join.",
        source_url="https://example.invalid/join",
    )
    assert ok.source_url == "https://example.invalid/join"


def test_two_institutions_share_one_provider_through_distinct_relationships() -> None:
    provider = ServiceProvider(
        schema_version=SV,
        provider_id="prov-shared",
        name="Synthetic Loan Platform",
        kind=ProviderKind.LOAN_PLATFORM,
    )
    first_cu = institution()
    second_cu = institution(charter_number=54321, legal_name="Other Synthetic Credit Union")
    first = relationship(
        relationship_id="rel-a",
        institution_charter=first_cu.charter_number,
        source_id="src-a",
        kind=SourceRelationshipKind.PROVIDER_HOSTED,
        provider_id=provider.provider_id,
    )
    second = relationship(
        relationship_id="rel-b",
        institution_charter=second_cu.charter_number,
        source_id="src-b",
        kind=SourceRelationshipKind.PROVIDER_HOSTED,
        provider_id=provider.provider_id,
    )
    # One provider, two institutions: identities stay separate and nothing collapses.
    assert first.provider_id == second.provider_id == provider.provider_id
    assert first.institution_charter != second.institution_charter
    assert first.relationship_id != second.relationship_id
    assert first != second
    assert first_cu != second_cu
    assert {first.institution_charter, second.institution_charter} == {
        first_cu.charter_number,
        second_cu.charter_number,
    }


def test_shared_provider_relationship_cannot_authorize_the_other_institution() -> None:
    from cu_rates.contracts import ReasonCode, evaluate_publication

    from .factories import context, purchase_offer

    other = relationship(
        relationship_id="rel-b",
        institution_charter=54321,
        kind=SourceRelationshipKind.PROVIDER_HOSTED,
        provider_id="prov-shared",
    )
    offer = purchase_offer()  # belongs to CHARTER, not 54321
    decision = evaluate_publication(offer, context(offer, relationship=other))
    assert ReasonCode.RELATIONSHIP_MISMATCH in decision.reasons

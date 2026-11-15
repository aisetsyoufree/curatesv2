"""Synthetic builders for contract tests. No real institution or rate data."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from cu_rates.contracts import (
    APR_RULE_ID,
    CONTRACT_VERSION,
    REQUIRED_DIMENSIONS,
    ArmTerms,
    CanonicalScenarioId,
    ConditionKind,
    ConformingStatus,
    DimensionMatch,
    EvidenceKind,
    ExtractionProvenance,
    FetchRun,
    FieldEvidence,
    FixedEquityDetails,
    GovernmentGuaranty,
    HelocDetails,
    HtmlLocator,
    Institution,
    InstitutionSourceRelationship,
    InstitutionStatus,
    LienPosition,
    LoanPurpose,
    MatchState,
    NormalizedOffer,
    OfferCondition,
    OfferSnapshot,
    OfferStatus,
    ParserVersion,
    PriorPublication,
    ProductFamily,
    ProductIdentity,
    ProductLifecycle,
    Provenance,
    PublicationContext,
    PublishedSnapshot,
    PurchaseMortgageDetails,
    RateAvailability,
    RateSource,
    RateTier,
    RateType,
    RawArtifact,
    RefinanceMortgageDetails,
    ReviewDecision,
    ReviewSubject,
    ReviewVerdict,
    SnapshotEntry,
    SourceApproval,
    SourcedMargin,
    SourcedPoints,
    SourcedRate,
    SourceRelationshipKind,
    TierBound,
    TierDimension,
    ValidationPolicy,
    ValidationResult,
    WithdrawalEntry,
    WithdrawalScope,
    offer_fingerprints,
    product_content_sha256,
    snapshot_manifest_sha256,
)

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
EVALUATED_AT = T0 + timedelta(hours=1)
HEX16 = "0123456789abcdef"
ARTIFACT_ID = HEX16 * 4
TEXT_HASH = "fedcba9876543210" * 4
CONFIG_HASH = "1111222233334444" * 4
GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
PARSER = ParserVersion(name="synthetic-html", version="1.0.0")
CHARTER = 12345
PRODUCT_ID = "prod-30yr-fixed"
SOURCE_ID = "src-rates-page"
SOURCE_URL = "https://example.invalid/rates"
SV = CONTRACT_VERSION
VALIDATOR = ParserVersion(name="synthetic-validator", version="1.0.0")
IMAGE_ARTIFACT_ID = "abcdef0123456789" * 4


def extraction(rule_id: str | None = None, parser: ParserVersion = PARSER) -> ExtractionProvenance:
    return ExtractionProvenance(
        git_sha=GIT_SHA,
        parser=parser,
        configuration_sha256=CONFIG_HASH,
        normalization_version="norm-1",
        rule_id=rule_id,
    )


def provenance(
    text: str = "6.125%", artifact_id: str = ARTIFACT_ID, parser: ParserVersion = PARSER
) -> Provenance:
    return Provenance(
        artifact_id=artifact_id,
        locator=HtmlLocator(
            selector_type="css", selector="#rates td.rate", normalized_text_sha256=TEXT_HASH
        ),
        source_text=text,
        extraction=extraction(parser=parser),
    )


def rate(value: str, text: str | None = None) -> SourcedRate:
    return SourcedRate(value=Decimal(value), provenance=provenance(text or f"{value}%"))


def margin(value: str, text: str | None = None) -> SourcedMargin:
    return SourcedMargin(value=Decimal(value), provenance=provenance(text or f"{value}%"))


def raw_artifact(
    artifact_id: str = ARTIFACT_ID, media_type: str = "text/html; charset=utf-8", **overrides: Any
) -> RawArtifact:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "artifact_id": artifact_id,
        "media_type": media_type,
        "source_url": SOURCE_URL,
        "fetched_at": T0,
        "http_status": 200,
        "byte_length": 1024,
        "fetch_run_id": "fetch-001",
    }
    kwargs.update(overrides)
    return RawArtifact(**kwargs)


def rate_source(**overrides: Any) -> RateSource:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "source_id": SOURCE_ID,
        "url": SOURCE_URL,
        "evidence_kind": EvidenceKind.HTML,
    }
    kwargs.update(overrides)
    return RateSource(**kwargs)


def fetch_run(*artifact_ids: str, **overrides: Any) -> FetchRun:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "run_id": "fetch-001",
        "source_id": SOURCE_ID,
        "profile_id": "profile-1",
        "status": "succeeded",
        "started_at": T0 - timedelta(minutes=1),
        "finished_at": T0 + timedelta(minutes=1),
        "git_sha": GIT_SHA,
        "artifact_ids": artifact_ids or (ARTIFACT_ID,),
    }
    kwargs.update(overrides)
    return FetchRun(**kwargs)


def source_approval(**overrides: Any) -> SourceApproval:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "approval_id": "src-approval-1",
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "decision": ReviewVerdict.APPROVED,
        "actor": "Sid",
        "decided_at": T0 - timedelta(days=30),
        "policy_id": "source-policy",
        "policy_version": "1",
        "rationale": "Official first-party rates page.",
    }
    kwargs.update(overrides)
    return SourceApproval(**kwargs)


def points(value: str) -> SourcedPoints:
    return SourcedPoints(value=Decimal(value), provenance=provenance(f"{value} points"))


def matches_all(scenario_id: CanonicalScenarioId) -> tuple[DimensionMatch, ...]:
    return tuple(
        DimensionMatch(
            dimension=dimension, state=MatchState.MATCHES, source_text=f"{dimension.value} stated"
        )
        for dimension in sorted(REQUIRED_DIMENSIONS[scenario_id])
    )


def condition(
    condition_id: str = "cond-1",
    kind: ConditionKind = ConditionKind.DISCOUNT,
    state: MatchState = MatchState.MATCHES,
) -> OfferCondition:
    return OfferCondition(
        condition_id=condition_id,
        kind=kind,
        description="Autopay discount may apply",
        provenance=provenance("autopay discount"),
        scenario_state=state,
    )


def tier(tier_id: str, lower: int | None, upper: int | None, dimension: TierDimension) -> RateTier:
    return grid_tier(tier_id, (dimension, lower, upper))


def grid_tier(
    tier_id: str, *axes: tuple[TierDimension, int | None, int | None], rate_value: str = "6.125"
) -> RateTier:
    return RateTier(
        tier_id=tier_id,
        bounds=tuple(
            TierBound(
                dimension=dimension,
                lower=None if lower is None else Decimal(lower),
                upper=None if upper is None else Decimal(upper),
            )
            for dimension, lower, upper in axes
        ),
        note_rate=rate(rate_value),
    )


def purchase_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "offer_id": "offer-001",
        "institution_charter": CHARTER,
        "product_id": PRODUCT_ID,
        "source_id": SOURCE_ID,
        "status": OfferStatus.APPROVED,
        "details": PurchaseMortgageDetails(term_months=360, rate_type=RateType.FIXED, lock_days=30),
        "scenario_id": CanonicalScenarioId.PURCHASE_30Y_FIXED,
        "scenario_matches": matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED),
        "note_rate": rate("6.125"),
        "apr": rate("6.250"),
        "points": points("0"),
        "effective_at": T0 - timedelta(days=1),
        "observed_at": T0,
        "published_at": None,
        "valid_until": T0 + timedelta(days=3),
        "supporting_evidence": (provenance("Rates as of 11/14"),),
    }
    kwargs.update(overrides)
    return kwargs


def purchase_offer(**overrides: Any) -> NormalizedOffer:
    """Offer with field-level evidence for every material path unless overridden."""
    kwargs = purchase_kwargs(**overrides)
    offer = NormalizedOffer(**kwargs)
    if "field_provenance" not in overrides:
        kwargs["field_provenance"] = tuple(
            FieldEvidence(path=path, provenance=provenance(path))
            for path in sorted(offer.material_paths())
        )
        offer = NormalizedOffer(**kwargs)
    return offer


def refinance_offer(
    purpose: LoanPurpose = LoanPurpose.RATE_TERM_REFINANCE, **overrides: Any
) -> NormalizedOffer:
    scenario = CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED
    kwargs: dict[str, Any] = {
        "details": RefinanceMortgageDetails(
            term_months=360, rate_type=RateType.FIXED, lock_days=30, purpose=purpose
        ),
        "scenario_id": scenario,
        "scenario_matches": matches_all(scenario),
    }
    kwargs.update(overrides)
    return purchase_offer(**kwargs)


def adjustable_details(arm: ArmTerms | None) -> PurchaseMortgageDetails:
    return PurchaseMortgageDetails(term_months=360, rate_type=RateType.ADJUSTABLE, arm=arm)


def equity_offer(**overrides: Any) -> NormalizedOffer:
    scenario = CanonicalScenarioId.FIXED_HOME_EQUITY_10Y
    kwargs: dict[str, Any] = {
        "details": FixedEquityDetails(term_months=120, lien_position=LienPosition.SECOND),
        "scenario_id": scenario,
        "scenario_matches": matches_all(scenario),
        "points": None,
    }
    kwargs.update(overrides)
    return purchase_offer(**kwargs)


def heloc_details(**overrides: Any) -> HelocDetails:
    kwargs: dict[str, Any] = {
        "index_name": "Prime",
        "index_rate": rate("7.500"),
        "margin": margin("0.500"),
        "floor": rate("4.000"),
        "lien_position": LienPosition.SECOND,
    }
    kwargs.update(overrides)
    return HelocDetails(**kwargs)


def heloc_offer(details: HelocDetails | None = None, **overrides: Any) -> NormalizedOffer:
    scenario = CanonicalScenarioId.HELOC
    kwargs: dict[str, Any] = {
        "details": details or heloc_details(),
        "scenario_id": scenario,
        "scenario_matches": matches_all(scenario),
        "note_rate": None,
        "apr": None,
        "points": None,
    }
    kwargs.update(overrides)
    return purchase_offer(**kwargs)


def institution(**overrides: Any) -> Institution:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "charter_number": CHARTER,
        "legal_name": "Example Synthetic Credit Union",
        "status": InstitutionStatus.ACTIVE,
        "charter_verified": True,
    }
    kwargs.update(overrides)
    return Institution(**kwargs)


def product(**overrides: Any) -> ProductIdentity:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "product_id": PRODUCT_ID,
        "institution_charter": CHARTER,
        "family": ProductFamily.MORTGAGE,
        "name": "30-Year Fixed",
        "lifecycle": ProductLifecycle.ACTIVE,
        "rate_availability": RateAvailability.PUBLIC_RATE,
        "conforming_status": ConformingStatus.CONFORMING,
        "government_guaranty": GovernmentGuaranty.NONE,
    }
    kwargs.update(overrides)
    return ProductIdentity(**kwargs)


def relationship(**overrides: Any) -> InstitutionSourceRelationship:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "relationship_id": "rel-001",
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "product_id": PRODUCT_ID,
        "kind": SourceRelationshipKind.FIRST_PARTY,
        "approved_by": "Sid",
        "approved_at": T0 - timedelta(days=30),
    }
    kwargs.update(overrides)
    return InstitutionSourceRelationship(**kwargs)


def review(
    offer: NormalizedOffer, verdict: ReviewVerdict = ReviewVerdict.APPROVED
) -> ReviewDecision:
    return ReviewDecision(
        schema_version=SV,
        review_id="review-001",
        subject=ReviewSubject.OFFER,
        subject_id=offer.offer_id,
        verdict=verdict,
        actor="Sid",
        decided_at=T0 - timedelta(days=1),
        rationale="Matches the stored source page.",
        reviewed_fingerprints=offer_fingerprints(offer),
    )


def product_review(
    product_id: str = PRODUCT_ID,
    verdict: ReviewVerdict = ReviewVerdict.APPROVED,
    reviewed: ProductIdentity | None = None,
    **overrides: Any,
) -> ReviewDecision:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "review_id": "review-prod-001",
        "subject": ReviewSubject.PRODUCT,
        "subject_id": product_id,
        "verdict": verdict,
        "actor": "Sid",
        "decided_at": T0 - timedelta(days=30),
        "rationale": "Product identity confirmed against the institution's catalog.",
        "reviewed_product_sha256": product_content_sha256(
            reviewed or product(product_id=product_id)
        ),
    }
    kwargs.update(overrides)
    return ReviewDecision(**kwargs)


def published_snapshot(offer: NormalizedOffer, **overrides: Any) -> PublishedSnapshot:
    fingerprints = offer_fingerprints(offer)
    entries = (
        SnapshotEntry(
            offer_id=offer.offer_id,
            structural_sha256=fingerprints.structural,
            value_sha256=fingerprints.value,
        ),
    )
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "snapshot_id": "snap-prior",
        "published_at": T0 - timedelta(days=1),
        "published_by": "Sid",
        "entries": entries,
        "manifest_sha256": snapshot_manifest_sha256(entries),
    }
    kwargs.update(overrides)
    return PublishedSnapshot(**kwargs)


def prior(
    offer: NormalizedOffer, *, reviewed: bool = True, changed_days_ago: int = 1
) -> PriorPublication:
    return PriorPublication(
        snapshot=OfferSnapshot.from_offer(offer),
        published=published_snapshot(offer),
        review=review(offer) if reviewed else None,
        last_value_change_at=T0 - timedelta(days=changed_days_ago),
    )


def withdrawal(scope: WithdrawalScope, target_id: str, **overrides: Any) -> WithdrawalEntry:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "withdrawal_id": "wd-001",
        "scope": scope,
        "target_id": target_id,
        "actor": "Sid",
        "withdrawn_at": T0,
        "reason": "Source reported an error.",
    }
    kwargs.update(overrides)
    return WithdrawalEntry(**kwargs)


def validation(
    rule_id: str = "rule.rate-range",
    offer: NormalizedOffer | None = None,
    *,
    passed: bool = True,
    **overrides: Any,
) -> ValidationResult:
    """A rule result bound to ``offer`` (default: the baseline purchase offer)."""
    subject = offer or purchase_offer()
    paths = ("apr", "note_rate") if rule_id == APR_RULE_ID else tuple(subject.rate_values())
    kwargs: dict[str, Any] = {
        "rule_id": rule_id,
        "passed": passed,
        "detail": None if passed else "rule failed",
        "offer_id": subject.offer_id,
        "fingerprints": offer_fingerprints(subject),
        "evaluated_at": T0,
        "validator": VALIDATOR,
        "checked_paths": paths,
    }
    kwargs.update(overrides)
    return ValidationResult(**kwargs)


def validations_for(offer: NormalizedOffer, **overrides: Any) -> tuple[ValidationResult, ...]:
    """The required rate-range rule, plus the APR rule when both rates are present."""
    results = [validation("rule.rate-range", offer, **overrides)]
    if offer.apr is not None and offer.note_rate is not None:
        results.append(validation(APR_RULE_ID, offer, **overrides))
    return tuple(results)


def validation_policy(**overrides: Any) -> ValidationPolicy:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "policy_id": "validation-policy",
        "policy_version": "1",
        "required_rules": ("rule.rate-range",),
        "approved_by": "Sid",
        "approved_at": T0 - timedelta(days=30),
    }
    kwargs.update(overrides)
    return ValidationPolicy(**kwargs)


def context(
    offer: NormalizedOffer | None = None,
    *,
    subject: NormalizedOffer | None = None,
    **overrides: Any,
) -> PublicationContext:
    """A context under which ``purchase_offer()`` auto-publishes as ranked.

    ``offer`` is the previously published state; ``subject`` (default: ``offer``) is the
    offer being evaluated, to which validations are bound.
    """
    baseline = offer or purchase_offer()
    current = subject or baseline
    kwargs: dict[str, Any] = {
        "institution": institution(),
        "product": product(),
        "relationship": relationship(),
        "source": rate_source(),
        "source_approval": source_approval(),
        "validation_policy": validation_policy(),
        "approved_parsers": (PARSER,),
        "known_artifacts": (raw_artifact(),),
        "fetch_runs": (fetch_run(),),
        "validations": validations_for(current),
        "prior": prior(baseline),
        "evaluated_at": EVALUATED_AT,
    }
    kwargs.update(overrides)
    if "product_review" not in kwargs:
        kwargs["product_review"] = product_review(reviewed=kwargs["product"])
    return PublicationContext(**kwargs)

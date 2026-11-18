"""Synthetic builders for contract tests. No real institution or rate data."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from cu_rates.contracts import (
    APR_RULE_ID,
    CANONICAL_SCENARIOS,
    CONTRACT_VERSION,
    REQUIRED_DIMENSIONS,
    ArmTerms,
    CanonicalScenarioId,
    ConditionKind,
    ConformingStatus,
    DimensionMatch,
    EvidenceKind,
    ExtractionProvenance,
    FetchMethod,
    FetchProfile,
    FetchRun,
    FieldEvidence,
    FixedEquityDetails,
    GovernmentGuaranty,
    HelocDetails,
    HtmlLocator,
    Institution,
    InstitutionIdentityReview,
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
    ScenarioDimension,
    SnapshotEntry,
    SourceApproval,
    SourcedMargin,
    SourcedPoints,
    SourcedRate,
    SourceFingerprint,
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
from cu_rates.contracts.fingerprints import offer_evidence_sha256
from cu_rates.contracts.publication import (
    EvidencePath,
    VerifiedScenarioCondition,
    VerifiedScenarioStatement,
)
from cu_rates.contracts.scenarios import matching_statement
from cu_rates.contracts.sources import profile_sha256

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
LAYOUT_SHA = "ab" * 32
# D-18: a corroborating second capture from a different fetch run.
ARTIFACT_ID_2 = "bb" * 32


def evidence_path(
    path: str = "note_rate",
    value: str | Decimal = "6.125",
    artifact_id: str = ARTIFACT_ID,
    capture_id: str = "fetch-001",
    observed_at: datetime = T0,
    product_id: str = PRODUCT_ID,
    scenario_id: CanonicalScenarioId | None = CanonicalScenarioId.PURCHASE_30Y_FIXED,
    source_class: str = "html",
    **overrides: Any,
) -> EvidencePath:
    kwargs: dict[str, Any] = {
        "path": path,
        "value": Decimal(str(value)),
        "artifact_id": artifact_id,
        "capture_id": capture_id,
        "observed_at": observed_at,
        "product_id": product_id,
        "scenario_id": scenario_id,
        "source_class": source_class,
        "content_sha256": artifact_id,
        "source_id": SOURCE_ID,
        "provenance": provenance(f"{value}%", artifact_id=artifact_id),
        "scenario_statements": fixture_statements(purchase_offer(), artifact_id, capture_id),
    }
    kwargs.update(overrides)
    return EvidencePath(**kwargs)


def corroborating_path(
    path: str = "note_rate",
    value: str | Decimal = "6.125",
    artifact_id: str = ARTIFACT_ID_2,
    capture_id: str = "fetch-002",
    observed_at: datetime = T0 - timedelta(hours=4),
    product_id: str = PRODUCT_ID,
    scenario_id: CanonicalScenarioId | None = CanonicalScenarioId.PURCHASE_30Y_FIXED,
    source_class: str = "html",
    **overrides: Any,
) -> EvidencePath:
    return evidence_path(
        path=path,
        value=value,
        artifact_id=artifact_id,
        capture_id=capture_id,
        observed_at=observed_at,
        product_id=product_id,
        scenario_id=scenario_id,
        source_class=source_class,
        **overrides,
    )


def fixture_statements(
    offer: NormalizedOffer, artifact_id: str, run_id: str
) -> tuple[VerifiedScenarioStatement, ...]:
    return tuple(
        VerifiedScenarioStatement(
            offer_id=offer.offer_id,
            dimension=match.dimension,
            fetch_run_id=run_id,
            stated=match.stated,
            provenance=match.provenance.model_copy(update={"artifact_id": artifact_id}),
        )
        for match in offer.scenario_matches
        if match.stated is not None and match.provenance is not None
    )


def fixture_captures() -> tuple[tuple[RawArtifact, FetchRun], ...]:
    """Two explicit fixture observations, using the same records as DB/pipeline tests."""
    return (
        (raw_artifact(), fetch_run()),
        (
            raw_artifact(
                artifact_id=ARTIFACT_ID_2,
                fetch_run_id="fetch-002",
                fetched_at=T0 - timedelta(hours=4),
            ),
            fetch_run(
                ARTIFACT_ID_2,
                run_id="fetch-002",
                started_at=T0 - timedelta(hours=4, minutes=1),
                finished_at=T0 - timedelta(hours=3, minutes=59),
            ),
        ),
    )


def evidence_paths_for(offer: NormalizedOffer) -> tuple[EvidencePath, ...]:
    from cu_rates.contracts.publication import financial_items

    return tuple(
        evidence_path(
            path=path,
            value=value,
            product_id=offer.product_id,
            scenario_id=offer.scenario_id,
            artifact_id=artifact.artifact_id,
            capture_id=artifact.fetch_run_id,
            observed_at=artifact.fetched_at,
            provenance=ref.model_copy(update={"artifact_id": artifact.artifact_id}),
            scenario_statements=fixture_statements(offer, artifact.artifact_id, run.run_id),
        )
        for artifact, run in fixture_captures()
        for path, value, ref in financial_items(offer)
    )


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


def fetch_profile(**overrides: Any) -> FetchProfile:
    """The one profile a valid publication context treats as human-approved."""
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "profile_id": "profile-1",
        "source_id": SOURCE_ID,
        "method": FetchMethod.HTTP_GET,
        "min_interval_seconds": 60,
        "timeout_seconds": 30,
    }
    kwargs.update(overrides)
    return FetchProfile(**kwargs)


def fetch_run(*artifact_ids: str, **overrides: Any) -> FetchRun:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "run_id": "fetch-001",
        "source_id": SOURCE_ID,
        "profile_id": "profile-1",
        "profile_sha256": profile_sha256(fetch_profile()),
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
        "approved_profile_sha256": profile_sha256(fetch_profile()),
        "approved_layout_sha256": LAYOUT_SHA,
        "adapter": PARSER,
    }
    kwargs.update(overrides)
    return SourceApproval(**kwargs)


def source_fingerprint(**overrides: Any) -> SourceFingerprint:
    """Layout digest of the baseline artifact, tied to the approved adapter."""
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "source_id": SOURCE_ID,
        "fetch_run_id": "fetch-001",
        "artifact_id": ARTIFACT_ID,
        "observed_at": T0,
        "content_sha256": ARTIFACT_ID,
        "structure_sha256": LAYOUT_SHA,
        "adapter": PARSER,
    }
    kwargs.update(overrides)
    return SourceFingerprint(**kwargs)


def points(value: str) -> SourcedPoints:
    return SourcedPoints(value=Decimal(value), provenance=provenance(f"{value} points"))


def matches_all(scenario_id: CanonicalScenarioId) -> tuple[DimensionMatch, ...]:
    scenario = CANONICAL_SCENARIOS[scenario_id]
    texts = {
        ScenarioDimension.OCCUPANCY: "Owner-occupied primary residence",
        ScenarioDimension.PROPERTY_TYPE: "Single-family",
        ScenarioDimension.CREDIT_SCORE: str(scenario.credit_score),
        ScenarioDimension.RATE_TYPE: "Fixed",
        ScenarioDimension.TERM_MONTHS: f"{scenario.term_months} months",
        ScenarioDimension.LTV: f"{scenario.ltv}%",
        ScenarioDimension.CLTV: f"{scenario.cltv}%",
        ScenarioDimension.LOCK_DAYS: f"{scenario.lock_days} days",
        ScenarioDimension.LOAN_AMOUNT: "$" + str(scenario.loan_amount),
        ScenarioDimension.POINTS: f"{scenario.points} points",
        ScenarioDimension.CASH_OUT: "No cash out",
        ScenarioDimension.RELATIONSHIP_DISCOUNT: "No relationship discount",
        ScenarioDimension.PROPERTY_VALUE: "$" + str(scenario.property_value),
        ScenarioDimension.AUTOPAY: "No autopay discount",
        ScenarioDimension.FIRST_LIEN_BALANCE: (
            "$" + str(scenario.first_lien_balance)
            if scenario.first_lien_balance is not None
            else None
        ),
    }
    return tuple(
        DimensionMatch(
            dimension=dimension,
            state=MatchState.MATCHES,
            source_text=text,
            stated=matching_statement(scenario, dimension),
            provenance=provenance(text),
        )
        for dimension in sorted(REQUIRED_DIMENSIONS[scenario_id])
        if (text := texts.get(dimension)) is not None
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
        "draw_period_months": 120,
        "repayment_period_months": 240,
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


def identity_review(**overrides: Any) -> InstitutionIdentityReview:
    """A current, approving identity and Massachusetts-scope review for ``institution()``."""
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "review_id": "identity-review-001",
        "institution_charter": CHARTER,
        "legal_name": institution().legal_name,
        "identity_confirmed": True,
        "massachusetts_scope_confirmed": True,
        "evidence": (
            provenance(f"Example Synthetic Credit Union - NCUA charter {CHARTER} - Massachusetts"),
        ),
        "reviewer": "Sid",
        "decided_at": T0 - timedelta(days=30),
        "rationale": "Charter number, legal name and Massachusetts location match the cited page.",
    }
    kwargs.update(overrides)
    return InstitutionIdentityReview(**kwargs)


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
        reviewed_evidence_sha256=offer_evidence_sha256(offer),
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
        verified_artifact_id=ARTIFACT_ID,
        verified_configuration_sha256=CONFIG_HASH,
        verified_parser=PARSER,
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
        "known_artifacts": tuple(a for a, _ in fixture_captures()),
        "fetch_runs": tuple(r for _, r in fixture_captures()),
        "approved_profile": fetch_profile(),
        "source_fingerprints": (source_fingerprint(),),
        "validations": validations_for(current),
        "prior": prior(baseline),
        "evidence_paths": evidence_paths_for(current),
        "evaluated_at": EVALUATED_AT,
    }
    kwargs.update(overrides)
    if "identity_review" not in overrides:
        # By default the review describes the institution this context carries; tests of
        # a stale, mismatched or missing review pass ``identity_review`` explicitly.
        named = kwargs["institution"]
        kwargs["identity_review"] = identity_review(
            institution_charter=named.charter_number,
            legal_name=named.legal_name,
            evidence=(
                provenance(
                    f"{named.legal_name} - NCUA charter {named.charter_number} - Massachusetts"
                ),
            ),
        )
    # Most contract tests override the primary record to exercise a separate rule.
    # Keep the explicitly modeled second fixture capture unless paths are supplied
    # explicitly (single-capture tests use that lane or remove records on the model).
    if "evidence_paths" not in overrides:
        artifacts = tuple(kwargs["known_artifacts"])
        fetches = tuple(kwargs["fetch_runs"])
        if artifacts and not any(a.artifact_id == ARTIFACT_ID_2 for a in artifacts):
            second, second_run = fixture_captures()[1]
            artifacts += (second,)
            kwargs["known_artifacts"] = artifacts
        if fetches and not any(r.run_id == "fetch-002" for r in fetches):
            second_run = fixture_captures()[1][1].model_copy(
                update={
                    "profile_sha256": fetches[0].profile_sha256,
                    "profile_id": fetches[0].profile_id,
                }
            )
            kwargs["fetch_runs"] = (*fetches, second_run)
        kwargs["evidence_paths"] = tuple(
            p.model_copy(update={"source_class": kwargs["source"].evidence_kind.value})
            for p in evidence_paths_for(current)
        )
    if "source_fingerprints" not in overrides:
        kwargs["source_fingerprints"] = tuple(
            source_fingerprint(
                artifact_id=artifact.artifact_id,
                content_sha256=artifact.artifact_id,
                fetch_run_id=artifact.fetch_run_id,
                observed_at=artifact.fetched_at,
            )
            for artifact in kwargs["known_artifacts"]
            if artifact.source_url == kwargs["source"].url
        )
    runs = {art.artifact_id: art.fetch_run_id for art in kwargs["known_artifacts"]}
    if "verified_scenario_statements" not in overrides:
        kwargs["verified_scenario_statements"] = tuple(
            VerifiedScenarioStatement(
                offer_id=current.offer_id,
                dimension=match.dimension,
                fetch_run_id=runs[match.provenance.artifact_id],
                provenance=match.provenance,
                stated=match.stated,
            )
            for match in current.scenario_matches
            if match.stated is not None
            and match.provenance is not None
            and match.provenance.artifact_id in runs
        )
    if "verified_scenario_statements" not in overrides:
        extra = fixture_statements(current, ARTIFACT_ID_2, "fetch-002")
        kwargs["verified_scenario_statements"] = tuple(
            dict.fromkeys((*kwargs["verified_scenario_statements"], *extra))
        )
    if "verified_scenario_conditions" not in overrides and current.scenario_id is not None:
        kwargs["verified_scenario_conditions"] = tuple(
            VerifiedScenarioCondition(
                offer_id=current.offer_id,
                scenario_id=current.scenario_id,
                fetch_run_id=runs[item.provenance.artifact_id],
                condition=item,
            )
            for item in current.conditions
            if item.provenance.artifact_id in runs
        )
    if "product_review" not in kwargs:
        kwargs["product_review"] = product_review(reviewed=kwargs["product"])
    return PublicationContext(**kwargs)

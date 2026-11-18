from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from cu_rates.contracts import (
    ArmTerms,
    BorrowerProgram,
    CanonicalScenarioId,
    ConditionKind,
    ConformingStatus,
    DimensionMatch,
    GovernmentGuaranty,
    ImageOcrLocator,
    InstitutionStatus,
    IntroRate,
    LoanPurpose,
    MatchState,
    NormalizedOffer,
    OfferStatus,
    ParserVersion,
    PartnerProgram,
    Placement,
    ProductFamily,
    ProductLifecycle,
    Provenance,
    PublicationDecision,
    PublicationOutcome,
    PurchaseMortgageDetails,
    RateAvailability,
    RateType,
    ReasonCode,
    SourcedRate,
    TierDimension,
    WithdrawalScope,
    evaluate_publication,
)
from cu_rates.contracts.publication import (
    BROWSE_ONLY_REASONS,
    DETAIL_ONLY_REASONS,
    QUARANTINE_REASONS,
    REVIEW_REASONS,
    SUPPRESS_REASONS,
)

from .factories import (
    ARTIFACT_ID,
    EVALUATED_AT,
    IMAGE_ARTIFACT_ID,
    PRODUCT_ID,
    T0,
    TEXT_HASH,
    adjustable_details,
    condition,
    context,
    equity_offer,
    extraction,
    fetch_run,
    heloc_details,
    heloc_offer,
    identity_review,
    institution,
    margin,
    matches_all,
    points,
    product,
    provenance,
    purchase_offer,
    rate,
    raw_artifact,
    refinance_offer,
    relationship,
    tier,
    validation,
    withdrawal,
)
from .factories import (
    prior as prior_of,
)

R = ReasonCode
D = Decimal
WD = withdrawal(WithdrawalScope.OFFER, "offer-001")
WD_PROD = withdrawal(WithdrawalScope.PRODUCT, PRODUCT_ID)
WD_INST = withdrawal(WithdrawalScope.INSTITUTION, "12345")


def decide(offer: NormalizedOffer | None = None, **ctx: Any) -> PublicationDecision:
    subject = offer or purchase_offer()
    return evaluate_publication(subject, context(subject, **ctx))


def ocr_rate() -> SourcedRate:
    provenance = Provenance(
        artifact_id=IMAGE_ARTIFACT_ID,
        locator=ImageOcrLocator.model_validate(
            {
                "region": {"x0": "1", "y0": "1", "x1": "50", "y1": "20"},
                "image_sha256": IMAGE_ARTIFACT_ID,
                "recognized_text_sha256": TEXT_HASH,
            }
        ),
        source_text="6.125%",
        extraction=extraction(),
    )
    return SourcedRate(value=D("6.125"), provenance=provenance)


# --- baseline & determinism ----------------------------------------------------------------


def test_fully_proven_unchanged_offer_auto_publishes_as_ranked() -> None:
    decision = decide()
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED
    assert decision.reasons == ()
    assert decision.publishable_now
    assert decision.ranking_eligible
    assert not decision.degraded


def test_decision_is_deterministic_and_reasons_follow_enum_order() -> None:
    offer = purchase_offer(apr=None, points=None)
    ctx = context(offer, prior=None, withdrawals=(WD,), validations=())
    first = evaluate_publication(offer, ctx)
    second = evaluate_publication(offer, ctx)
    assert first == second
    assert first.reasons == tuple(code for code in ReasonCode if code in first.reasons)
    assert len(set(first.reasons)) == len(first.reasons)


def test_every_reason_code_has_a_category() -> None:
    categorized = (
        SUPPRESS_REASONS
        | QUARANTINE_REASONS
        | REVIEW_REASONS
        | DETAIL_ONLY_REASONS
        | BROWSE_ONLY_REASONS
    )
    assert set(ReasonCode) - categorized == {R.STALE_DEGRADED}
    assert not SUPPRESS_REASONS & QUARANTINE_REASONS
    assert not (SUPPRESS_REASONS | QUARANTINE_REASONS) & (REVIEW_REASONS | BROWSE_ONLY_REASONS)


def test_suppress_outranks_quarantine_outranks_review() -> None:
    both = decide(withdrawals=(WD,), validations=(), prior=None)
    assert both.outcome is PublicationOutcome.SUPPRESS
    assert {R.DENYLISTED, R.VALIDATIONS_MISSING, R.FIRST_PUBLICATION} <= set(both.reasons)
    quarantine = decide(validations=(), prior=None)
    assert quarantine.outcome is PublicationOutcome.QUARANTINE


# --- unsafe publication: nothing unproven may auto-publish -----------------------------------

_BAD_PARSER = (ParserVersion(name="synthetic-html", version="2.0.0"),)
_FAILED = (
    validation("rule.rate-range"),
    validation("rule.apr-ge-note-rate", passed=False, detail="APR below note rate"),
)
_PAST_96H = T0 + timedelta(hours=96, seconds=1)

UNSAFE_CASES: list[tuple[str, dict[str, Any], dict[str, Any], ReasonCode, PublicationOutcome]] = [
    ("withdrawn-offer", {}, {"withdrawals": (WD,)}, R.DENYLISTED, PublicationOutcome.SUPPRESS),
    (
        "withdrawn-product",
        {},
        {"withdrawals": (WD_PROD,)},
        R.DENYLISTED,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "withdrawn-institution",
        {},
        {"withdrawals": (WD_INST,)},
        R.DENYLISTED,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "withdrawn-snapshot",
        {},
        {"snapshot_id": "snap-1", "withdrawals": (withdrawal(WithdrawalScope.SNAPSHOT, "snap-1"),)},
        R.DENYLISTED,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "merged",
        {},
        {"institution": institution(status=InstitutionStatus.MERGED, successor_charter_number=9)},
        R.INSTITUTION_NOT_ACTIVE,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "under-review",
        {},
        {"institution": institution(status=InstitutionStatus.UNDER_REVIEW)},
        R.INSTITUTION_NOT_ACTIVE,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "charter-unverified",
        {},
        {"institution": institution(charter_verified=False)},
        R.CHARTER_UNVERIFIED,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "identity-review-absent",
        {},
        {"identity_review": None},
        R.IDENTITY_REVIEW_MISSING,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "identity-review-for-another-charter",
        {},
        {"identity_review": identity_review(institution_charter=777)},
        R.IDENTITY_REVIEW_MISSING,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "identity-review-dated-in-the-future",
        {},
        {"identity_review": identity_review(decided_at=EVALUATED_AT + timedelta(days=1))},
        R.IDENTITY_REVIEW_MISSING,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "identity-not-confirmed",
        {},
        {"identity_review": identity_review(identity_confirmed=False)},
        R.IDENTITY_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "legal-name-drifted-since-review",
        {},
        {"identity_review": identity_review(legal_name="A Different Credit Union")},
        R.IDENTITY_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "massachusetts-scope-not-confirmed",
        {},
        {"identity_review": identity_review(massachusetts_scope_confirmed=False)},
        R.SCOPE_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "evidence-does-not-name-the-charter",
        {},
        {
            "identity_review": identity_review(
                evidence=(provenance("Example Synthetic Credit Union - Massachusetts"),)
            )
        },
        R.IDENTITY_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "evidence-names-another-institution",
        {},
        {
            "identity_review": identity_review(
                evidence=(provenance("Other Credit Union - NCUA charter 12345 - Massachusetts"),)
            )
        },
        R.IDENTITY_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "evidence-is-an-unrelated-rate-page",
        {},
        {"identity_review": identity_review(evidence=(provenance("30 days"),))},
        R.IDENTITY_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "evidence-does-not-place-it-in-massachusetts",
        {},
        {
            "identity_review": identity_review(
                evidence=(provenance("Example Synthetic Credit Union - NCUA charter 12345 - Ohio"),)
            )
        },
        R.SCOPE_NOT_CONFIRMED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "identity-review-stale",
        {},
        {"identity_review": identity_review(decided_at=EVALUATED_AT - timedelta(days=401))},
        R.IDENTITY_REVIEW_STALE,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "withdrawn",
        {"status": OfferStatus.WITHDRAWN},
        {},
        R.OFFER_NOT_CURRENT,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "superseded",
        {"status": OfferStatus.SUPERSEDED},
        {},
        R.OFFER_NOT_CURRENT,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "valid-until-reached",
        {},
        {"evaluated_at": T0 + timedelta(days=3)},
        R.VALIDITY_EXPIRED,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "stale-96h",
        {"valid_until": None},
        {"evaluated_at": _PAST_96H},
        R.STALE_WITHDRAWN,
        PublicationOutcome.SUPPRESS,
    ),
    (
        "relationship-unverified",
        {},
        {"relationship": relationship(approved_by=None, approved_at=None)},
        R.RELATIONSHIP_UNVERIFIED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "relationship-other-source",
        {},
        {"relationship": relationship(source_id="src-other")},
        R.RELATIONSHIP_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "relationship-other-institution",
        {},
        {"relationship": relationship(institution_charter=999)},
        R.RELATIONSHIP_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "relationship-other-product",
        {},
        {"relationship": relationship(product_id="prod-other")},
        R.RELATIONSHIP_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "relationship-covers-no-specific-product",
        {},
        {"relationship": relationship(product_id=None)},
        R.RELATIONSHIP_NOT_PRODUCT_BOUND,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "validity-missing",
        {"valid_until": None},
        {},
        R.VALIDITY_MISSING,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "institution-record-mismatch",
        {},
        {"institution": institution(charter_number=777)},
        R.RELATIONSHIP_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "product-id-mismatch",
        {},
        {"product": product(product_id="prod-other")},
        R.PRODUCT_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "product-family-mismatch",
        {},
        {"product": product(family=ProductFamily.HELOC)},
        R.PRODUCT_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "product-other-institution",
        {},
        {"product": product(institution_charter=999)},
        R.PRODUCT_MISMATCH,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "product-discontinued",
        {},
        {"product": product(lifecycle=ProductLifecycle.DISCONTINUED)},
        R.PRODUCT_NOT_ACTIVE,
        PublicationOutcome.QUARANTINE,
    ),
    *[
        (
            f"availability-{state.value}",
            {},
            {"product": product(rate_availability=state)},
            R.RATE_NOT_PUBLIC,
            PublicationOutcome.QUARANTINE,
        )
        for state in RateAvailability
        if state is not RateAvailability.PUBLIC_RATE
    ],
    (
        "source-unapproved",
        {},
        {"source_approval": None},
        R.SOURCE_NOT_APPROVED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "no-parsers-approved",
        {},
        {"approved_parsers": ()},
        R.PARSER_NOT_APPROVED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "parser-version-unapproved",
        {},
        {"approved_parsers": _BAD_PARSER},
        R.PARSER_NOT_APPROVED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "artifact-not-stored",
        {},
        {"known_artifacts": ()},
        R.ARTIFACT_NOT_STORED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "no-validations",
        {},
        {"validations": ()},
        R.VALIDATIONS_MISSING,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "failed-validation",
        {},
        {"validations": _FAILED},
        R.VALIDATION_FAILED,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "observed-in-future",
        {},
        {"evaluated_at": T0 - timedelta(seconds=1)},
        R.CLOCK_SKEW,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "first-publication",
        {},
        {"prior": None},
        R.FIRST_PUBLICATION,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "prior-unreviewed",
        {},
        {"prior": prior_of(purchase_offer(), reviewed=False)},
        R.PRIOR_NOT_REVIEWED,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "product-unreviewed",
        {},
        {"product_review": None},
        R.PRODUCT_NOT_REVIEWED,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "ocr-value",
        {"note_rate": ocr_rate()},
        {
            "known_artifacts": (raw_artifact(), raw_artifact(IMAGE_ARTIFACT_ID, "image/png")),
            "fetch_runs": (fetch_run(ARTIFACT_ID, IMAGE_ARTIFACT_ID),),
        },
        R.OCR_VALUE,
        PublicationOutcome.QUARANTINE,
    ),
    (
        "changed-product-identity",
        {},
        {"prior": prior_of(purchase_offer(product_id="prod-old"))},
        R.PRODUCT_IDENTITY_CHANGED,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "changed-structure",
        {},
        {"prior": prior_of(purchase_offer(conditions=(condition(),)))},
        R.STRUCTURAL_CHANGE,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "changed-points",
        {},
        {"prior": prior_of(purchase_offer(points=points("0.500")))},
        R.POINTS_CHANGED,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "points-became-known",
        {},
        {"prior": prior_of(purchase_offer(points=None))},
        R.STRUCTURAL_CHANGE,
        PublicationOutcome.REQUIRES_REVIEW,
    ),
    (
        "note-rate-jump",
        {"note_rate": rate("6.63001")},
        {},
        R.RATE_MOVEMENT_EXCEEDS_LIMIT,
        # The jump also puts APR under the note rate, which the gate recomputes.
        PublicationOutcome.QUARANTINE,
    ),
    (
        "apr-drop",
        {"apr": rate("5.700")},
        {},
        R.RATE_MOVEMENT_EXCEEDS_LIMIT,
        PublicationOutcome.QUARANTINE,
    ),
]


@pytest.mark.parametrize(
    ("offer_overrides", "ctx_overrides", "reason", "outcome"),
    [case[1:] for case in UNSAFE_CASES],
    ids=[case[0] for case in UNSAFE_CASES],
)
def test_unsafe_publication_never_auto_publishes(
    offer_overrides: dict[str, Any],
    ctx_overrides: dict[str, Any],
    reason: ReasonCode,
    outcome: PublicationOutcome,
) -> None:
    baseline = purchase_offer()
    # Rate-movement cases compare against the unmodified baseline snapshot.
    offer = purchase_offer(**offer_overrides)
    ctx = context(baseline, subject=offer, **ctx_overrides)
    decision = evaluate_publication(offer, ctx)
    assert reason in decision.reasons
    assert decision.outcome is outcome
    assert decision.outcome is not PublicationOutcome.AUTO_PUBLISH
    assert not decision.publishable_now
    assert not decision.ranking_eligible
    if outcome in {PublicationOutcome.SUPPRESS, PublicationOutcome.QUARANTINE}:
        assert decision.placement is Placement.NONE


def test_unsafe_cases_cover_every_blocking_reason() -> None:
    covered = {case[3] for case in UNSAFE_CASES}
    assert (SUPPRESS_REASONS | QUARANTINE_REASONS | REVIEW_REASONS) - covered == {
        R.SOURCE_ABANDONED,
        R.CONDITIONS_INCOMPLETE,
        R.ARM_TERMS_INCOMPLETE,
        R.EFFECTIVE_DATE_MISSING,
        R.RATE_MOVEMENT_UNBOUNDED,
        R.FIELD_EVIDENCE_MISSING,
        R.ARTIFACT_LOCATOR_MISMATCH,
        R.HELOC_POST_INTRO_INCOMPLETE,
        R.HELOC_POST_INTRO_CONFLICT,
        R.VALIDITY_TOO_LONG,
        R.OFFER_CANDIDATE,
        R.OFFER_QUARANTINED,
        R.NOT_YET_EFFECTIVE,
        R.ARTIFACT_STALE,
        R.ARTIFACT_SOURCE_MISMATCH,
        R.ARTIFACT_HTTP_STATUS,
        R.FETCH_NOT_VERIFIED,
        R.OBSERVATION_UNBOUND,
        R.REOBSERVATION_UNVERIFIED,
        R.VALIDATION_UNBOUND,
        R.CORRECTION_PENDING,
        R.PROFILE_UNAPPROVED,
        R.LAYOUT_UNREVIEWED,
        R.REOBSERVATION_CLAIM_CHANGED,
        R.INSUFFICIENT_EVIDENCE_PATHS,
        R.DUPLICATE_EVIDENCE_CAPTURE,
        R.EVIDENCE_PATH_NON_AUTHORITATIVE,
        R.EVIDENCE_PATH_PRODUCT_MISMATCH,
        R.EVIDENCE_PATH_SCENARIO_MISMATCH,
        R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW,
        R.EVIDENCE_PATH_VALUE_MISMATCH,
    }  # covered by dedicated tests below and in test_publication_gate.py


# --- review-trigger boundaries -------------------------------------------------------------


def _moved(note: str, apr: str = "6.250") -> NormalizedOffer:
    return purchase_offer(note_rate=rate(note), apr=rate(apr))


@pytest.mark.parametrize(
    ("note", "apr", "flagged"),
    [
        ("6.625", "6.250", False),  # exactly +50 bp is allowed
        ("6.62501", "6.250", True),
        ("5.625", "6.250", False),  # exactly -50 bp
        ("5.62499", "6.250", True),
        ("6.125", "6.750", False),
        ("6.125", "6.75001", True),
        ("6.125", "5.700", True),
    ],
)
def test_fifty_basis_point_boundary(note: str, apr: str, flagged: bool) -> None:
    decision = decide(_moved(note, apr), prior=prior_of(purchase_offer()))
    assert (R.RATE_MOVEMENT_EXCEEDS_LIMIT in decision.reasons) is flagged
    if D(apr) < D(note):
        assert decision.outcome is PublicationOutcome.QUARANTINE
        assert R.VALIDATION_FAILED in decision.reasons
        return
    expected = PublicationOutcome.REQUIRES_REVIEW if flagged else PublicationOutcome.AUTO_PUBLISH
    assert decision.outcome is expected


def test_small_rate_move_with_unchanged_structure_still_auto_publishes() -> None:
    decision = decide(_moved("6.250", "6.375"), prior=prior_of(purchase_offer()))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED


def test_source_abandonment_thresholds() -> None:
    def run(effective_days: int, changed_days: int) -> PublicationDecision:
        offer = purchase_offer(effective_at=T0 - timedelta(days=effective_days))
        return decide(offer, prior=prior_of(offer, changed_days_ago=changed_days))

    # evaluated_at is T0 + 1h, so ages are the given days + 1 hour.
    assert R.SOURCE_ABANDONED in run(91, 60).reasons
    assert R.SOURCE_ABANDONED not in run(89, 60).reasons  # effective date not old enough
    assert R.SOURCE_ABANDONED not in run(91, 58).reasons  # value changed recently
    abandoned = run(120, 120)
    assert abandoned.outcome is PublicationOutcome.REQUIRES_REVIEW


def test_current_value_change_resets_abandonment_clock() -> None:
    offer = purchase_offer(effective_at=T0 - timedelta(days=200), note_rate=rate("6.250"))
    unchanged = purchase_offer(effective_at=T0 - timedelta(days=200))
    stale_prior = prior_of(unchanged, changed_days_ago=150)
    assert R.SOURCE_ABANDONED not in decide(offer, prior=stale_prior).reasons


def test_unknown_effective_date_requires_review_instead_of_skipping_abandonment() -> None:
    offer = purchase_offer(effective_at=None)
    decision = decide(offer)
    assert R.EFFECTIVE_DATE_MISSING in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert not decision.publishable_now
    # Even a first publication (no prior to compare) must surface the missing date.
    first = decide(offer, prior=None)
    assert {R.EFFECTIVE_DATE_MISSING, R.FIRST_PUBLICATION} <= set(first.reasons)


def test_incomplete_conditions_and_arm_terms_need_review_and_browse_only() -> None:
    as_low_as = purchase_offer(
        headline_is_as_low_as=True, conditions=(condition("c1", ConditionKind.AS_LOW_AS),)
    )
    decision = decide(as_low_as)
    assert R.CONDITIONS_INCOMPLETE in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert decision.placement is Placement.BROWSE_ONLY

    partial_arm = purchase_offer(
        details=adjustable_details(ArmTerms(initial_period_months=84)),
        scenario_id=None,
        scenario_matches=(),
    )
    arm_decision = decide(partial_arm)
    assert R.ARM_TERMS_INCOMPLETE in arm_decision.reasons
    assert arm_decision.outcome is PublicationOutcome.QUARANTINE
    assert arm_decision.placement is Placement.NONE


def test_review_placement_is_reported_but_not_live() -> None:
    decision = decide(prior=None)
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert decision.placement is Placement.RANKED  # what a reviewer approval would permit
    assert not decision.ranking_eligible


# --- freshness & validity boundaries ----------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "degraded", "outcome", "reason"),
    [
        (timedelta(hours=71, minutes=59, seconds=59), False, PublicationOutcome.AUTO_PUBLISH, None),
        (timedelta(hours=72), False, PublicationOutcome.AUTO_PUBLISH, None),
        (timedelta(hours=72, seconds=1), True, PublicationOutcome.AUTO_PUBLISH, None),
        (
            timedelta(hours=95, minutes=59, seconds=59),
            True,
            PublicationOutcome.QUARANTINE,
            R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW,
        ),
        # valid_until is observed_at + 96h, so the offer is expired at exactly 96h.
        (timedelta(hours=96), True, PublicationOutcome.SUPPRESS, R.VALIDITY_EXPIRED),
        (timedelta(hours=96, seconds=1), False, PublicationOutcome.SUPPRESS, R.STALE_WITHDRAWN),
    ],
)
def test_freshness_boundaries(
    age: timedelta, degraded: bool, outcome: PublicationOutcome, reason: ReasonCode | None
) -> None:
    offer = purchase_offer(valid_until=T0 + timedelta(hours=96))
    decision = decide(offer, evaluated_at=T0 + age)
    assert decision.outcome is outcome
    if outcome is PublicationOutcome.AUTO_PUBLISH:
        assert decision.degraded is degraded
        assert decision.placement is Placement.RANKED  # degraded is flagged, still comparable
    else:
        assert decision.placement is Placement.NONE
        assert reason in decision.reasons


def test_validity_boundary_is_exclusive_of_valid_until() -> None:
    last_ok = T0 + timedelta(days=3) - timedelta(seconds=1)
    assert decide(evaluated_at=last_ok).outcome is PublicationOutcome.AUTO_PUBLISH
    expired = decide(evaluated_at=T0 + timedelta(days=3))
    assert expired.outcome is PublicationOutcome.SUPPRESS
    assert R.VALIDITY_EXPIRED in expired.reasons


def test_expiry_is_enforced_even_if_everything_else_is_perfect() -> None:
    decision = decide(evaluated_at=EVALUATED_AT + timedelta(days=30))
    assert decision.outcome is PublicationOutcome.SUPPRESS
    assert not decision.ranking_eligible


# --- placement ------------------------------------------------------------------------------------


def test_note_rate_without_apr_is_institution_detail_only() -> None:
    decision = decide(purchase_offer(apr=None))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.INSTITUTION_DETAIL_ONLY
    assert R.NO_APR in decision.reasons
    assert not decision.ranking_eligible


def test_apr_without_note_rate_is_browse_only() -> None:
    decision = decide(purchase_offer(note_rate=None))
    assert decision.placement is Placement.BROWSE_ONLY
    assert R.NO_NOTE_RATE in decision.reasons


def test_points_null_unknown_is_not_rankable_but_explicit_zero_is() -> None:
    unknown = decide(purchase_offer(points=None))
    assert unknown.placement is Placement.BROWSE_ONLY
    assert R.POINTS_UNKNOWN in unknown.reasons
    assert decide(purchase_offer(points=points("0.00"))).placement is Placement.RANKED
    nonzero = decide(purchase_offer(points=points("0.250")))
    assert nonzero.placement is Placement.BROWSE_ONLY
    assert R.POINTS_NOT_SCENARIO_VALUE in nonzero.reasons


def test_undeclared_scenario_is_browse_only() -> None:
    decision = decide(purchase_offer(scenario_id=None, scenario_matches=()))
    assert decision.placement is Placement.NONE
    assert R.SCENARIO_NOT_DECLARED in decision.reasons


def test_unstated_and_conflicting_dimensions_block_ranking() -> None:
    full = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    unstated = decide(purchase_offer(scenario_matches=full[1:]))
    assert unstated.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_UNSTATED in unstated.reasons
    conflict_match = DimensionMatch(
        dimension=full[0].dimension, state=MatchState.CONFLICTS, source_text="stated otherwise"
    )
    conflicting = decide(purchase_offer(scenario_matches=(conflict_match, *full[1:])))
    assert conflicting.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_CONFLICT in conflicting.reasons


def test_terms_that_differ_from_scenario_block_ranking() -> None:
    fifteen = purchase_offer(
        details=PurchaseMortgageDetails(term_months=180, rate_type=RateType.FIXED, lock_days=30)
    )
    decision = decide(fifteen)
    assert decision.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_TERMS_DIFFER in decision.reasons


@pytest.mark.parametrize("state", [MatchState.UNSTATED, MatchState.CONFLICTS])
def test_unsatisfied_conditions_block_ranking(state: MatchState) -> None:
    offer = purchase_offer(conditions=(condition(state=state),))
    decision = decide(offer)
    assert decision.placement is Placement.BROWSE_ONLY
    assert R.CONDITION_NOT_SATISFIED in decision.reasons


def test_satisfied_conditions_do_not_block_ranking() -> None:
    decision = decide(purchase_offer(conditions=(condition(state=MatchState.MATCHES),)))
    assert decision.placement is Placement.RANKED


@pytest.mark.parametrize(
    ("product_overrides", "reason"),
    [
        ({"government_guaranty": GovernmentGuaranty.FHA}, R.NON_CONVENTIONAL_PROGRAM),
        ({"government_guaranty": GovernmentGuaranty.VA}, R.NON_CONVENTIONAL_PROGRAM),
        ({"government_guaranty": GovernmentGuaranty.USDA}, R.NON_CONVENTIONAL_PROGRAM),
        ({"government_guaranty": GovernmentGuaranty.UNKNOWN}, R.NON_CONVENTIONAL_PROGRAM),
        ({"borrower_programs": (BorrowerProgram.FIRST_TIME_BUYER,)}, R.NON_CONVENTIONAL_PROGRAM),
        ({"partner_programs": (PartnerProgram.MASSHOUSING,)}, R.NON_CONVENTIONAL_PROGRAM),
        ({"conforming_status": ConformingStatus.JUMBO}, R.NOT_CONFORMING),
        ({"conforming_status": ConformingStatus.HIGH_BALANCE_CONFORMING}, R.NOT_CONFORMING),
        ({"conforming_status": ConformingStatus.UNKNOWN}, R.NOT_CONFORMING),
    ],
)
def test_only_conventional_conforming_mortgages_rank(
    product_overrides: dict[str, Any], reason: ReasonCode
) -> None:
    decision = decide(product=product(**product_overrides))
    assert decision.placement is Placement.BROWSE_ONLY
    assert reason in decision.reasons
    assert not decision.ranking_eligible


def test_refinance_ranks_against_its_own_scenario() -> None:
    offer = refinance_offer()
    assert decide(offer).placement is Placement.RANKED
    assert decide(offer).outcome is PublicationOutcome.AUTO_PUBLISH


def test_cash_out_refinance_has_no_canonical_scenario_so_is_browse_only() -> None:
    offer = refinance_offer(LoanPurpose.CASH_OUT_REFINANCE, scenario_id=None, scenario_matches=())
    decision = decide(offer)
    assert decision.placement is Placement.NONE
    assert R.SCENARIO_NOT_DECLARED in decision.reasons


def test_tier_must_cover_scenario_and_agree_with_headline() -> None:
    covering = purchase_offer(tiers=(tier("t", 0, 80, TierDimension.LTV),))
    assert decide(covering).placement is Placement.RANKED

    not_covering = decide(purchase_offer(tiers=(tier("t", 0, 70, TierDimension.LTV),)))
    assert not_covering.placement is Placement.BROWSE_ONLY
    assert R.TIER_NOT_COVERING_SCENARIO in not_covering.reasons

    boundary_lower = decide(purchase_offer(tiers=(tier("t", 80, 90, TierDimension.LTV),)))
    assert boundary_lower.placement is Placement.RANKED  # inclusive bounds

    mismatched = decide(
        purchase_offer(
            note_rate=rate("6.500"),
            apr=rate("6.625"),
            tiers=(tier("t", 0, 80, TierDimension.LTV),),
        ),
        prior=None,
    )
    assert R.TIER_RATE_MISMATCH in mismatched.reasons
    assert mismatched.placement is Placement.BROWSE_ONLY


def test_equity_loan_ranks_without_conforming_requirement() -> None:
    offer = equity_offer()
    decision = decide(offer, product=product(family=ProductFamily.HOME_EQUITY_LOAN))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED


def test_equity_loan_tier_on_ltv_cannot_cover_a_cltv_scenario() -> None:
    offer = equity_offer(tiers=(tier("t", 0, 80, TierDimension.LTV),))
    decision = decide(offer, product=product(family=ProductFamily.HOME_EQUITY_LOAN))
    assert R.TIER_NOT_COVERING_SCENARIO in decision.reasons


def test_heloc_ranks_only_with_complete_post_intro() -> None:
    heloc_product = product(family=ProductFamily.HELOC)
    ranked = decide(heloc_offer(), product=heloc_product)
    assert ranked.outcome is PublicationOutcome.AUTO_PUBLISH
    assert ranked.placement is Placement.RANKED

    incomplete_offer = heloc_offer(heloc_details(floor=None))
    incomplete = decide(incomplete_offer, product=heloc_product)
    assert incomplete.outcome is PublicationOutcome.QUARANTINE  # unpublishable, not browsable
    assert incomplete.placement is Placement.NONE
    assert not incomplete.publishable_now
    assert R.HELOC_POST_INTRO_INCOMPLETE in incomplete.reasons
    assert R.NO_APR not in incomplete.reasons


def test_heloc_with_stated_post_intro_apr_ranks() -> None:
    details = heloc_details(
        index_name=None,
        index_rate=None,
        margin=None,
        floor=None,
        stated_post_intro_apr=rate("8.250"),
    )
    decision = decide(heloc_offer(details), product=product(family=ProductFamily.HELOC))
    assert decision.placement is Placement.RANKED


def test_heloc_intro_rate_alone_is_never_ranked() -> None:
    details = heloc_details(
        index_name=None,
        index_rate=None,
        margin=None,
        floor=None,
        intro=IntroRate(rate=rate("3.990"), duration_months=6),
    )
    decision = decide(heloc_offer(details), product=product(family=ProductFamily.HELOC))
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE
    assert R.HELOC_POST_INTRO_INCOMPLETE in decision.reasons


def test_adjustable_mortgage_is_not_ranked_against_a_fixed_scenario() -> None:
    arm = ArmTerms(
        initial_period_months=84,
        adjustment_interval_months=12,
        index_name="SOFR",
        margin=margin("2.750"),
        initial_cap=D(5),
        periodic_cap=D(2),
        lifetime_cap=D(5),
    )
    offer = purchase_offer(details=adjustable_details(arm))
    decision = decide(offer)
    assert decision.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_TERMS_DIFFER in decision.reasons
    assert R.ARM_TERMS_INCOMPLETE not in decision.reasons

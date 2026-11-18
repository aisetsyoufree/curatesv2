"""Fail-closed binding of publication to status, fetches, sources, validations, and audit."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    APR_RULE_ID,
    ArmTerms,
    FixedEquityDetails,
    ImageOcrLocator,
    LienPosition,
    NormalizedOffer,
    OfferSnapshot,
    OfferStatus,
    Placement,
    PriorPublication,
    ProductFamily,
    Provenance,
    PublicationDecision,
    PublicationOutcome,
    PurchaseMortgageDetails,
    ReasonCode,
    RenderedDomLocator,
    ReviewDecision,
    ReviewVerdict,
    RunStatus,
    SourcedRate,
    ValidationResult,
    WithdrawalScope,
    evaluate_publication,
    offer_fingerprints,
    value_fingerprint,
)
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import EvidenceKind, FetchMethod, RateType
from cu_rates.contracts.fingerprints import offer_evidence_sha256
from cu_rates.contracts.publication import (
    SUPPRESS_REASONS,
    corroborated_paths,
    publication_evidence_sha256,
)
from cu_rates.contracts.sources import profile_sha256

from .factories import (
    ARTIFACT_ID,
    EVALUATED_AT,
    IMAGE_ARTIFACT_ID,
    SV,
    T0,
    TEXT_HASH,
    adjustable_details,
    context,
    equity_offer,
    extraction,
    fetch_profile,
    fetch_run,
    heloc_details,
    heloc_offer,
    margin,
    prior,
    product,
    product_review,
    provenance,
    published_snapshot,
    purchase_offer,
    rate,
    rate_source,
    raw_artifact,
    refinance_offer,
    relationship,
    review,
    source_approval,
    validation,
    validation_policy,
    validations_for,
    withdrawal,
)

R = ReasonCode
D = Decimal
EQUITY_PRODUCT = product(family=ProductFamily.HOME_EQUITY_LOAN)
HELOC_PRODUCT = product(family=ProductFamily.HELOC)
OTHER_ARTIFACT = "c" * 64
RENDER_PROFILE = fetch_profile(method=FetchMethod.RENDERED_BROWSER)
RENDER_DIGEST = profile_sha256(RENDER_PROFILE)


def decide(offer: NormalizedOffer | None = None, **ctx: Any) -> PublicationDecision:
    subject = offer or purchase_offer()
    return evaluate_publication(subject, context(subject, **ctx))


def blocked(decision: PublicationDecision, reason: ReasonCode, outcome: PublicationOutcome) -> None:
    assert reason in decision.reasons
    assert decision.outcome is outcome
    assert not decision.publishable_now
    assert decision.placement is Placement.NONE or outcome is PublicationOutcome.REQUIRES_REVIEW


Q = PublicationOutcome.QUARANTINE
S = PublicationOutcome.SUPPRESS
RV = PublicationOutcome.REQUIRES_REVIEW


# --- 1. only approved or published offers proceed -----------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "outcome"),
    [
        (OfferStatus.CANDIDATE, R.OFFER_CANDIDATE, Q),
        (OfferStatus.QUARANTINED, R.OFFER_QUARANTINED, Q),
        (OfferStatus.WITHDRAWN, R.OFFER_NOT_CURRENT, S),
        (OfferStatus.SUPERSEDED, R.OFFER_NOT_CURRENT, S),
    ],
)
def test_only_approved_or_published_offers_publish(
    status: OfferStatus, reason: ReasonCode, outcome: PublicationOutcome
) -> None:
    kwargs: dict[str, Any] = {"status": status}
    if status in {OfferStatus.WITHDRAWN, OfferStatus.SUPERSEDED}:
        kwargs["published_at"] = T0
    blocked(decide(purchase_offer(**kwargs)), reason, outcome)


def test_approved_and_published_offers_are_publishable() -> None:
    assert decide(purchase_offer(status=OfferStatus.APPROVED)).publishable_now
    published = purchase_offer(status=OfferStatus.PUBLISHED, published_at=T0)
    assert decide(published).publishable_now


def test_status_reasons_are_categorized_and_exclusive() -> None:
    assert R.OFFER_NOT_CURRENT in SUPPRESS_REASONS
    candidate = decide(purchase_offer(status=OfferStatus.CANDIDATE))
    assert R.OFFER_NOT_CURRENT not in candidate.reasons


# --- 2. evidence is bound to a successful current fetch of the right source -----------------


@pytest.mark.parametrize(
    "runs",
    [
        (),
        (fetch_run(status=RunStatus.FAILED, error="timeout", artifact_ids=()),),
        (fetch_run(status=RunStatus.BLOCKED, error="robots", artifact_ids=()),),
        (fetch_run(source_id="src-other"),),
        (fetch_run(OTHER_ARTIFACT),),  # run does not list the cited artifact
        (fetch_run(run_id="fetch-002"),),  # artifact belongs to another run
        (fetch_run(started_at=T0 + timedelta(seconds=1), finished_at=T0 + timedelta(minutes=1)),),
        (fetch_run(finished_at=T0 - timedelta(seconds=1)),),  # fetched after its run ended
        (fetch_run(finished_at=EVALUATED_AT + timedelta(seconds=1)),),  # run ends in the future
    ],
)
def test_evidence_requires_a_matching_successful_fetch_run(runs: Any) -> None:
    blocked(decide(fetch_runs=runs), R.FETCH_NOT_VERIFIED, Q)


def test_fetch_run_lists_artifact_among_several() -> None:
    assert decide(fetch_runs=(fetch_run(ARTIFACT_ID, OTHER_ARTIFACT),)).publishable_now


def test_artifact_from_another_source_url_is_rejected() -> None:
    attacker = raw_artifact(source_url="https://attacker.invalid/rates")
    blocked(decide(known_artifacts=(attacker,)), R.ARTIFACT_SOURCE_MISMATCH, Q)
    elsewhere = raw_artifact(source_url="https://example.invalid/rates/")  # exact URL only
    blocked(decide(known_artifacts=(elsewhere,)), R.ARTIFACT_SOURCE_MISMATCH, Q)


def test_source_record_must_be_the_offers_source() -> None:
    blocked(decide(source=rate_source(source_id="src-other")), R.SOURCE_NOT_APPROVED, Q)


@pytest.mark.parametrize("status", [100, 199, 301, 304, 403, 404, 429, 500, 503])
def test_non_success_http_status_is_not_evidence(status: int) -> None:
    blocked(decide(known_artifacts=(raw_artifact(http_status=status),)), R.ARTIFACT_HTTP_STATUS, Q)


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_success_http_status_is_evidence(status: int) -> None:
    assert decide(known_artifacts=(raw_artifact(http_status=status),)).publishable_now


def test_artifact_fetched_after_evaluation_is_clock_skew() -> None:
    future = EVALUATED_AT + timedelta(seconds=1)
    offer = purchase_offer(observed_at=T0)
    runs = (fetch_run(started_at=T0, finished_at=future),)
    decision = decide(offer, known_artifacts=(raw_artifact(fetched_at=future),), fetch_runs=runs)
    assert R.CLOCK_SKEW in decision.reasons
    assert not decision.publishable_now


def test_observation_time_equals_the_newest_used_artifact_fetch() -> None:
    later = purchase_offer(observed_at=T0 + timedelta(minutes=10))
    blocked(decide(later), R.OBSERVATION_UNBOUND, Q)
    earlier = purchase_offer(observed_at=T0 - timedelta(minutes=10))
    blocked(decide(earlier), R.OBSERVATION_UNBOUND, Q)
    assert R.OBSERVATION_UNBOUND not in decide().reasons


def test_stale_artifact_cannot_ride_along_with_fresh_evidence() -> None:
    old = T0 - timedelta(days=10)
    old_run = fetch_run(OTHER_ARTIFACT, run_id="fetch-000", started_at=old, finished_at=old)
    offer = purchase_offer(
        supporting_evidence=(provenance("Rates as of last week", artifact_id=OTHER_ARTIFACT),)
    )
    decision = decide(
        offer,
        known_artifacts=(
            raw_artifact(),
            raw_artifact(OTHER_ARTIFACT, fetch_run_id="fetch-000", fetched_at=old),
        ),
        fetch_runs=(fetch_run(), old_run),
    )
    assert R.ARTIFACT_STALE in decision.reasons
    assert decision.outcome is S


# --- 5. immutable source approval ----------------------------------------------------------


@pytest.mark.parametrize(
    "approval",
    [
        None,
        source_approval(decision=ReviewVerdict.REJECTED),
        source_approval(source_id="src-other"),
        source_approval(source_url="https://example.invalid/moved"),
        source_approval(decided_at=EVALUATED_AT + timedelta(seconds=1)),
    ],
)
def test_source_approval_must_be_an_approval_of_this_source_and_url(approval: Any) -> None:
    blocked(decide(source_approval=approval), R.SOURCE_NOT_APPROVED, Q)


def test_source_approval_is_immutable_and_audited() -> None:
    approval = source_approval()
    assert approval.approved
    with pytest.raises(ValidationError):
        approval.actor = "someone else"  # type: ignore[misc]
    payload = approval.model_dump(mode="json")
    for missing in ("actor", "decided_at", "policy_id", "policy_version", "rationale", "decision"):
        with pytest.raises(ValidationError):
            type(approval).model_validate({k: v for k, v in payload.items() if k != missing})
    with pytest.raises(ValidationError, match="schema_version"):
        type(approval).model_validate({k: v for k, v in payload.items() if k != "schema_version"})
    # The approval must predate the fetch. A decision at evaluation time is too late.
    started = fetch_run().started_at - timedelta(hours=5)
    assert decide(source_approval=source_approval(decided_at=started)).publishable_now
    late = decide(source_approval=source_approval(decided_at=EVALUATED_AT))
    assert R.PROFILE_UNAPPROVED in late.reasons
    assert not late.publishable_now


# --- 3. contract fields are compared with the canonical scenario -----------------------------


def _mortgage(**kwargs: Any) -> NormalizedOffer:
    details = PurchaseMortgageDetails(rate_type=RateType.FIXED, **{"term_months": 360, **kwargs})
    return purchase_offer(details=details)


@pytest.mark.parametrize("lock_days", [15, 29, 31, 45, 60, None])
def test_mortgage_lock_days_must_equal_the_scenario(lock_days: int | None) -> None:
    decision = decide(_mortgage(lock_days=lock_days))
    assert R.SCENARIO_TERMS_DIFFER in decision.reasons
    assert decision.placement is Placement.BROWSE_ONLY
    assert not decision.ranking_eligible


def test_mortgage_matching_lock_days_ranks_despite_nothing_else_changing() -> None:
    assert decide(_mortgage(lock_days=30)).ranking_eligible


def test_extractor_claims_cannot_override_a_conflicting_contract_field() -> None:
    # matches_all() claims every dimension matches, including lock_days and term.
    for term, lock in ((180, 30), (360, 45)):
        decision = decide(_mortgage(term_months=term, lock_days=lock))
        assert R.SCENARIO_TERMS_DIFFER in decision.reasons


def test_adjustable_rate_type_differs_from_the_fixed_scenario() -> None:
    offer = purchase_offer(details=adjustable_details(ArmTerms()))
    assert R.SCENARIO_TERMS_DIFFER in decide(offer).reasons


def _equity(**kwargs: Any) -> NormalizedOffer:
    details = FixedEquityDetails(term_months=120, lien_position=LienPosition.SECOND, **kwargs)
    return equity_offer(details=details)


@pytest.mark.parametrize(
    ("kwargs", "differs"),
    [
        ({"max_cltv": D("79.99999")}, True),
        ({"max_cltv": D("80")}, False),  # boundary: cap equals the scenario CLTV
        ({"max_cltv": D("100")}, False),
        ({}, False),  # an unstated cap cannot contradict the scenario
        ({"min_amount": D("50000.01")}, True),
        ({"min_amount": D("50000.00")}, False),  # boundary: inclusive
        ({"min_amount": D("10000")}, False),
        ({"max_amount": D("49999.99")}, True),
        ({"max_amount": D("50000.00")}, False),
        ({"min_amount": D("25000"), "max_amount": D("40000")}, True),
        ({"min_amount": D("25000"), "max_amount": D("75000")}, False),
    ],
)
def test_fixed_equity_cltv_cap_and_amount_bounds(kwargs: dict[str, Any], differs: bool) -> None:
    decision = decide(_equity(**kwargs), product=EQUITY_PRODUCT)
    assert (R.SCENARIO_TERMS_DIFFER in decision.reasons) is differs
    assert decision.ranking_eligible is (not differs)


@pytest.mark.parametrize(("cap", "differs"), [("79.99999", True), ("80", False), ("90", False)])
def test_heloc_cltv_cap(cap: str, differs: bool) -> None:
    offer = heloc_offer(heloc_details(max_cltv=D(cap)))
    decision = decide(offer, product=HELOC_PRODUCT)
    assert (R.SCENARIO_TERMS_DIFFER in decision.reasons) is differs
    assert decision.ranking_eligible is (not differs)


def test_fixed_equity_term_and_lien_still_checked() -> None:
    wrong = equity_offer(
        details=FixedEquityDetails(term_months=180, lien_position=LienPosition.SECOND)
    )
    assert R.SCENARIO_TERMS_DIFFER in decide(wrong, product=EQUITY_PRODUCT).reasons


# --- 4. value fingerprint is bound to the listed rates and points --------------------------


def _every_rate_type() -> dict[str, NormalizedOffer]:
    arm = adjustable_details(ArmTerms())
    return {
        "purchase": purchase_offer(),
        "refinance": refinance_offer(),
        "arm": purchase_offer(details=arm, scenario_id=None, scenario_matches=()),
        "equity": equity_offer(),
        "heloc": heloc_offer(heloc_details(margin=margin("-0.250"))),
    }


@pytest.mark.parametrize("name", sorted(_every_rate_type()))
def test_snapshot_fingerprint_is_recomputed_from_rates_and_points(name: str) -> None:
    offer = _every_rate_type()[name]
    snapshot = OfferSnapshot.from_offer(offer)
    points = offer.points.value if offer.points else None
    assert snapshot.fingerprints.value == value_fingerprint(offer.rate_values(), points)
    assert snapshot.fingerprints == offer_fingerprints(offer)
    assert OfferSnapshot.model_validate(snapshot.model_dump()) == snapshot


@pytest.mark.parametrize("name", sorted(_every_rate_type()))
def test_forged_snapshot_cannot_list_other_numbers(name: str) -> None:
    offer = _every_rate_type()[name]
    honest = OfferSnapshot.from_offer(offer)
    first = honest.rates[0]
    nine = first.model_copy(update={"value": D("9.000")})
    for forged in (
        {"rates": (nine, *honest.rates[1:])},  # one rate changed
        {"rates": honest.rates[1:]},  # a rate dropped
        {"rates": ()},
        {"points": D("1.5")},  # points changed
    ):
        with pytest.raises(ValidationError, match="value fingerprint"):
            OfferSnapshot(**{**honest.model_dump(), **forged})


def test_hand_built_snapshot_cannot_claim_a_nine_percent_offers_fingerprints() -> None:
    nine = purchase_offer(note_rate=rate("9.000"), apr=rate("9.100"))
    six = OfferSnapshot.from_offer(purchase_offer(note_rate=rate("6.100"), apr=rate("6.200")))
    with pytest.raises(ValidationError, match="value fingerprint"):
        OfferSnapshot(
            offer_id=six.offer_id,
            product_id=six.product_id,
            fingerprints=offer_fingerprints(nine),
            rates=six.rates,
            points=six.points,
        )


def test_value_fingerprint_distinguishes_null_from_zero_points_and_ignores_scale() -> None:
    rates = {"note_rate": D("6.125")}
    assert value_fingerprint(rates, None) != value_fingerprint(rates, D("0"))
    assert value_fingerprint(rates, D("0")) == value_fingerprint(rates, D("0.000"))
    assert value_fingerprint({"note_rate": D("6.1250")}, None) == value_fingerprint(rates, None)
    assert value_fingerprint({"apr": D("6.125")}, None) != value_fingerprint(rates, None)


# --- 6. validation records are bound, current, and required -----------------------------------


def test_validation_result_rejects_no_ops_and_unexplained_failures() -> None:
    good = validation().model_dump()
    with pytest.raises(ValidationError):
        ValidationResult(**{**good, "checked_paths": ()})
    with pytest.raises(ValidationError, match="must say why"):
        ValidationResult(**{**good, "passed": False, "detail": None})
    with pytest.raises(ValidationError):
        ValidationResult(**{k: v for k, v in good.items() if k != "fingerprints"})


@pytest.mark.parametrize(
    "result",
    [
        validation(offer_id="offer-other"),
        validation(offer=purchase_offer(note_rate=rate("6.250"))),  # another content version
        validation(evaluated_at=EVALUATED_AT + timedelta(seconds=1)),  # from the future
        validation(evaluated_at=T0 - timedelta(seconds=1)),  # before the observation
        validation(checked_paths=("details.not_a_field",)),
    ],
)
def test_mismatched_or_future_validation_does_not_count(result: ValidationResult) -> None:
    decision = decide(validations=(result,))
    blocked(decision, R.VALIDATION_UNBOUND, Q)
    assert R.VALIDATIONS_MISSING in decision.reasons


def test_validation_boundaries_are_inclusive() -> None:
    for moment in (T0, EVALUATED_AT):
        results = validations_for(purchase_offer(), evaluated_at=moment)
        assert decide(validations=results).publishable_now


def test_one_arbitrary_passing_rule_cannot_authorize_publication() -> None:
    arbitrary = validation("rule.anything")
    blocked(decide(validations=(arbitrary,)), R.VALIDATIONS_MISSING, Q)
    policy = validation_policy(required_rules=("rule.rate-range", "rule.sanity"))
    blocked(decide(validation_policy=policy), R.VALIDATIONS_MISSING, Q)
    both = (*validations_for(purchase_offer()), validation("rule.sanity"))
    assert decide(validation_policy=policy, validations=both).publishable_now


def test_a_failed_required_rule_blocks_even_beside_a_pass() -> None:
    failed = (*validations_for(purchase_offer()), validation("rule.other", passed=False))
    blocked(decide(validations=failed), R.VALIDATION_FAILED, Q)
    with pytest.raises(ValidationError, match="duplicate validation"):
        decide(validations=(validation(), validation(passed=False)))


def test_no_policy_or_future_policy_means_no_required_rules() -> None:
    blocked(decide(validation_policy=None), R.VALIDATIONS_MISSING, Q)
    future = validation_policy(approved_at=EVALUATED_AT + timedelta(seconds=1))
    blocked(decide(validation_policy=future), R.VALIDATIONS_MISSING, Q)


def test_policy_needs_at_least_one_required_rule() -> None:
    with pytest.raises(ValidationError):
        validation_policy(required_rules=())


def test_apr_rule_must_have_run_when_both_rates_are_present() -> None:
    offer = purchase_offer()  # note rate and APR both present
    only_range = (validation("rule.rate-range", offer),)
    blocked(decide(offer, validations=only_range), R.VALIDATIONS_MISSING, Q)
    # An APR result that never examined both rates does not prove the rule ran.
    narrow = validation(APR_RULE_ID, offer, checked_paths=("apr",))
    blocked(decide(offer, validations=(*only_range, narrow)), R.VALIDATIONS_MISSING, Q)
    full = validation(APR_RULE_ID, offer)
    assert decide(offer, validations=(*only_range, full)).publishable_now


def test_apr_rule_is_not_required_without_both_rates() -> None:
    offer = purchase_offer(apr=None)
    assert R.VALIDATIONS_MISSING not in decide(offer).reasons
    failing = validation(APR_RULE_ID, purchase_offer(), passed=False)
    assert R.VALIDATION_UNBOUND in decide(offer, validations=(failing,)).reasons


def test_failed_apr_rule_quarantines() -> None:
    offer = purchase_offer(apr=rate("6.000"))  # APR below the 6.125 note rate
    failing = (validation("rule.rate-range", offer), validation(APR_RULE_ID, offer, passed=False))
    blocked(decide(offer, validations=failing), R.VALIDATION_FAILED, Q)


# --- 7. review, approval, and relationship times and bindings ---------------------------------


def test_relationship_approved_after_evaluation_is_unverified() -> None:
    late = relationship(approved_at=EVALUATED_AT + timedelta(seconds=1))
    blocked(decide(relationship=late), R.RELATIONSHIP_UNVERIFIED, Q)
    assert decide(relationship=relationship(approved_at=EVALUATED_AT)).publishable_now


def test_product_review_must_be_current_and_bind_the_product_content() -> None:
    future = product_review(decided_at=EVALUATED_AT + timedelta(seconds=1))
    blocked(decide(product_review=future), R.PRODUCT_NOT_REVIEWED, RV)
    renamed = product(name="Renamed Product")
    stale = product_review(reviewed=product())  # reviewed the old content
    blocked(decide(product=renamed, product_review=stale), R.PRODUCT_NOT_REVIEWED, RV)
    assert decide(product=renamed).publishable_now  # a review of the renamed content works


def test_product_review_records_a_digest_and_offer_reviews_do_not() -> None:
    payload = product_review().model_dump(mode="json")
    with pytest.raises(ValidationError, match="content digest"):
        ReviewDecision.model_validate({**payload, "reviewed_product_sha256": None})
    offer_payload = review(purchase_offer()).model_dump(mode="json")
    with pytest.raises(ValidationError, match="only product reviews"):
        ReviewDecision.model_validate({**offer_payload, "reviewed_product_sha256": TEXT_HASH})


def test_prior_review_after_evaluation_does_not_count() -> None:
    offer = purchase_offer()
    future_review = review(offer).model_copy(
        update={"decided_at": EVALUATED_AT + timedelta(days=1)}
    )
    published = published_snapshot(offer, published_at=EVALUATED_AT + timedelta(days=2))
    late = PriorPublication(
        snapshot=OfferSnapshot.from_offer(offer),
        published=published,
        review=future_review,
        last_value_change_at=T0,
    )
    blocked(decide(offer, prior=late), R.PRIOR_NOT_REVIEWED, RV)


def test_prior_review_must_be_for_the_prior_offer() -> None:
    offer = purchase_offer()
    other = review(offer).model_copy(update={"subject_id": "offer-other"})
    with pytest.raises(ValidationError, match="different offer"):
        PriorPublication(
            snapshot=OfferSnapshot.from_offer(offer),
            published=published_snapshot(offer),
            review=other,
            last_value_change_at=T0,
        )


def test_prior_review_cannot_postdate_the_publication_it_approved() -> None:
    offer = purchase_offer()
    with pytest.raises(ValidationError, match="postdate"):
        PriorPublication(
            snapshot=OfferSnapshot.from_offer(offer),
            published=published_snapshot(offer, published_at=T0 - timedelta(days=2)),
            review=review(offer),  # decided one day before T0, after that publication
            last_value_change_at=T0,
        )


def test_prior_publication_must_link_to_a_matching_snapshot_entry() -> None:
    offer, moved = purchase_offer(), purchase_offer(note_rate=rate("6.250"))
    for published in (
        published_snapshot(moved),  # lists the same offer with other fingerprints
        published_snapshot(purchase_offer(offer_id="offer-other")),  # lists another offer
    ):
        with pytest.raises(ValidationError, match="published snapshot does not list"):
            PriorPublication(
                snapshot=OfferSnapshot.from_offer(offer),
                published=published,
                review=review(offer),
                last_value_change_at=T0,
            )


def test_withdrawn_prior_snapshot_is_not_a_trusted_baseline() -> None:
    entry = withdrawal(WithdrawalScope.SNAPSHOT, "snap-prior")
    decision = decide(withdrawals=(entry,), snapshot_id="snap-next")
    blocked(decision, R.PRIOR_NOT_REVIEWED, RV)
    assert R.DENYLISTED not in decision.reasons


def test_prior_for_a_different_offer_id_changes_identity() -> None:
    base = purchase_offer(offer_id="offer-old")
    blocked(decide(prior=prior(base)), R.PRODUCT_IDENTITY_CHANGED, RV)


# --- 8. refinance purpose evidence -----------------------------------------------------------


def test_refinance_purpose_is_material_and_needs_field_evidence() -> None:
    offer = refinance_offer()
    assert "details.purpose" in offer.material_paths()
    assert "details.purpose" not in purchase_offer().material_paths()
    kept = tuple(e for e in offer.field_provenance if e.path != "details.purpose")
    broken = offer.model_copy(update={"field_provenance": kept})
    blocked(decide(broken), R.FIELD_EVIDENCE_MISSING, Q)
    assert decide(offer).publishable_now


# --- 9. future dates and published_at lifecycle ----------------------------------------------


def test_future_effective_date_fails_closed() -> None:
    soon = purchase_offer(effective_at=EVALUATED_AT + timedelta(seconds=1))
    blocked(decide(soon), R.NOT_YET_EFFECTIVE, S)
    assert decide(purchase_offer(effective_at=EVALUATED_AT)).publishable_now


def test_future_published_at_fails_closed() -> None:
    future = purchase_offer(
        status=OfferStatus.PUBLISHED, published_at=EVALUATED_AT + timedelta(seconds=1)
    )
    blocked(decide(future), R.CLOCK_SKEW, Q)
    now = purchase_offer(status=OfferStatus.PUBLISHED, published_at=EVALUATED_AT)
    assert decide(now).publishable_now


@pytest.mark.parametrize(
    "status", [OfferStatus.CANDIDATE, OfferStatus.QUARANTINED, OfferStatus.APPROVED]
)
def test_never_published_offers_cannot_carry_published_at(status: OfferStatus) -> None:
    with pytest.raises(ValidationError, match="only a published, withdrawn, or superseded"):
        purchase_offer(status=status, published_at=T0)
    assert purchase_offer(status=status).published_at is None


@pytest.mark.parametrize("status", [OfferStatus.WITHDRAWN, OfferStatus.SUPERSEDED])
def test_formerly_published_offers_may_keep_published_at(status: OfferStatus) -> None:
    assert purchase_offer(status=status, published_at=T0).published_at == T0
    assert purchase_offer(status=status).published_at is None


# --- 10. audit records: snapshots and corrections --------------------------------------------


def test_snapshot_withdrawal_without_a_context_snapshot_fails_closed() -> None:
    for target in ("snap-1", "snap-unrelated"):
        entry = withdrawal(WithdrawalScope.SNAPSHOT, target)
        blocked(decide(withdrawals=(entry,)), R.DENYLISTED, S)
    lifted = withdrawal(
        WithdrawalScope.SNAPSHOT, "snap-1", lifted_at=T0 + timedelta(minutes=1), lifted_by="Sid"
    )
    assert decide(withdrawals=(lifted,)).publishable_now  # no longer in force


def _correction(**overrides: Any) -> Any:
    from cu_rates.contracts import Correction

    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "correction_id": "corr-1",
        "offer_id": "offer-001",
        "field_path": "note_rate",
        "previous_value": "6.125",
        "corrected_value": "6.250",
        "actor": "Sid",
        "corrected_at": T0 - timedelta(days=2),
        "reason": "Typo in the source table.",
    }
    kwargs.update(overrides)
    return Correction(**kwargs)


def test_unbound_correction_forces_review_until_the_state_is_reviewed() -> None:
    offer = purchase_offer()
    correction = _correction(corrected_value="6.125", evidence=(provenance("6.125%"),))
    # The review from before the new capture does not cover its fresh evidence.
    blocked(decide(offer, corrections=(correction,)), R.CORRECTION_PENDING, RV)
    v2_digest = publication_evidence_sha256(offer, corroborated_paths(offer, context(offer)))
    after_capture = prior(offer).model_copy(
        update={
            "review": review(offer).model_copy(
                update={"decided_at": T0, "reviewed_evidence_sha256": v2_digest}
            ),
            "published": published_snapshot(offer, published_at=T0),
        }
    )
    assert decide(offer, prior=after_capture, corrections=(correction,)).publishable_now
    # AUD7-12(b): a review bound only to the protocol-1 offer digest does not clear it.
    v1_only = after_capture.model_copy(
        update={
            "review": review(offer).model_copy(
                update={"decided_at": T0, "reviewed_evidence_sha256": offer_evidence_sha256(offer)}
            )
        }
    )
    blocked(decide(offer, prior=v1_only, corrections=(correction,)), R.CORRECTION_PENDING, RV)
    no_evidence = correction.model_copy(update={"evidence": ()})
    blocked(
        decide(offer, prior=after_capture, corrections=(no_evidence,)), R.CORRECTION_PENDING, RV
    )
    changed = purchase_offer(note_rate=rate("6.250"))  # review covers other content
    blocked(
        decide(changed, prior=prior(offer), corrections=(_correction(),)), R.CORRECTION_PENDING, RV
    )


def test_unreviewed_prior_cannot_clear_a_correction() -> None:
    offer = purchase_offer()
    decision = decide(offer, prior=prior(offer, reviewed=False), corrections=(_correction(),))
    assert R.CORRECTION_PENDING in decision.reasons


def test_correction_bound_to_the_current_content_clears() -> None:
    offer = purchase_offer()
    recent = {"corrected_at": T0 - timedelta(hours=1)}  # newer than the prior review
    unbound = _correction(**recent)
    blocked(decide(offer, corrections=(unbound,)), R.CORRECTION_PENDING, RV)
    # A fingerprint alone does not clear a replacement that is not on the offer.
    contradictory = _correction(corrected_fingerprints=offer_fingerprints(offer), **recent)
    blocked(decide(offer, corrections=(contradictory,)), R.CORRECTION_PENDING, RV)
    bound = _correction(
        corrected_fingerprints=offer_fingerprints(offer),
        corrected_value="6.125",
        evidence=(provenance("6.125%"),),
        **recent,
    )
    assert decide(offer, corrections=(bound,)).publishable_now
    other = offer_fingerprints(purchase_offer(note_rate=rate("6.200")))
    mismatched = _correction(corrected_fingerprints=other, **recent)
    blocked(decide(offer, corrections=(mismatched,)), R.CORRECTION_PENDING, RV)


def test_future_dated_correction_never_clears() -> None:
    offer = purchase_offer()
    bound = _correction(
        corrected_at=EVALUATED_AT + timedelta(seconds=1),
        corrected_fingerprints=offer_fingerprints(offer),
    )
    blocked(decide(offer, corrections=(bound,)), R.CORRECTION_PENDING, RV)


def test_corrections_for_other_offers_are_ignored() -> None:
    other = _correction(offer_id="offer-other")
    assert decide(corrections=(other,)).publishable_now


# --- 11. schema_version is explicit ----------------------------------------------------------


def test_every_versioned_record_requires_an_explicit_schema_version() -> None:
    records = (
        purchase_offer(),
        raw_artifact(),
        rate_source(),
        fetch_run(),
        source_approval(),
        validation_policy(),
        review(purchase_offer()),
        _correction(),
    )
    for record in records:
        payload = record.model_dump(mode="json")
        assert payload["schema_version"] == CONTRACT_VERSION
        del payload["schema_version"]
        with pytest.raises(ValidationError, match="schema_version"):
            type(record).model_validate(payload)
        padded = {**payload, "schema_version": f" {CONTRACT_VERSION}"}
        with pytest.raises(ValidationError, match="schema_version"):
            type(record).model_validate(padded)


# --- 12. locator, source kind, and response consistency ---------------------------------------


def _ocr(artifact_id: str, image_sha256: str) -> SourcedRate:
    locator = ImageOcrLocator.model_validate(
        {
            "region": {"x0": "1", "y0": "1", "x1": "50", "y1": "20"},
            "image_sha256": image_sha256,
            "recognized_text_sha256": TEXT_HASH,
        }
    )
    prov = Provenance(
        artifact_id=artifact_id, locator=locator, source_text="6.125%", extraction=extraction()
    )
    return SourcedRate(value=D("6.125"), provenance=prov)


def _with_image(
    artifact_id: str, image_sha256: str, media: str = "image/png"
) -> PublicationDecision:
    return decide(
        purchase_offer(note_rate=_ocr(artifact_id, image_sha256)),
        known_artifacts=(raw_artifact(), raw_artifact(artifact_id, media)),
        fetch_runs=(fetch_run(ARTIFACT_ID, artifact_id),),
    )


def test_image_artifact_id_must_match_the_ocr_locator_image_hash() -> None:
    ok = _with_image(IMAGE_ARTIFACT_ID, IMAGE_ARTIFACT_ID)
    assert R.ARTIFACT_LOCATOR_MISMATCH not in ok.reasons
    assert R.OCR_VALUE in ok.reasons
    bad = _with_image(IMAGE_ARTIFACT_ID, OTHER_ARTIFACT)
    blocked(bad, R.ARTIFACT_LOCATOR_MISMATCH, Q)


def test_ocr_of_a_pdf_page_may_hash_the_rendered_image_separately() -> None:
    # Only an image artifact must carry its own hash; a PDF's rendered page has another.
    pdf = _with_image(IMAGE_ARTIFACT_ID, OTHER_ARTIFACT, media="application/pdf")
    assert R.ARTIFACT_LOCATOR_MISMATCH not in pdf.reasons
    assert R.OCR_VALUE in pdf.reasons


@pytest.mark.parametrize("kind", [EvidenceKind.PDF, EvidenceKind.JSON, EvidenceKind.IMAGE_OCR])
def test_source_evidence_kind_must_admit_the_locator(kind: EvidenceKind) -> None:
    blocked(decide(source=rate_source(evidence_kind=kind)), R.ARTIFACT_LOCATOR_MISMATCH, Q)


def test_rendered_dom_source_admits_dom_and_html_locators() -> None:
    assert decide(
        source=rate_source(evidence_kind=EvidenceKind.RENDERED_DOM),
        approved_profile=RENDER_PROFILE,
        source_approval=source_approval(approved_profile_sha256=RENDER_DIGEST),
        fetch_runs=(fetch_run(profile_sha256=RENDER_DIGEST),),
    ).publishable_now


def _dom_offer(response_ids: tuple[str, ...]) -> NormalizedOffer:
    locator = RenderedDomLocator(
        selector_type="css",
        selector="div.rate",
        rendered_text_sha256=TEXT_HASH,
        response_ids=response_ids,
    )
    evidence = Provenance(
        artifact_id=ARTIFACT_ID, locator=locator, source_text="6.125%", extraction=extraction()
    )
    return purchase_offer(supporting_evidence=(evidence,))


def _dom_decision(response_ids: tuple[str, ...], **ctx: Any) -> PublicationDecision:
    ctx.setdefault("source", rate_source(evidence_kind=EvidenceKind.RENDERED_DOM))
    ctx.setdefault("approved_profile", RENDER_PROFILE)
    ctx.setdefault("source_approval", source_approval(approved_profile_sha256=RENDER_DIGEST))
    ctx.setdefault("fetch_runs", (fetch_run(profile_sha256=RENDER_DIGEST),))
    return decide(_dom_offer(response_ids), **ctx)


def test_rendered_dom_response_ids_must_be_stored_artifacts_of_the_same_fetch() -> None:
    xhr = raw_artifact(OTHER_ARTIFACT, "application/json", source_url="https://example.invalid/api")
    both = {
        "known_artifacts": (raw_artifact(), xhr),
        "fetch_runs": (fetch_run(ARTIFACT_ID, OTHER_ARTIFACT, profile_sha256=RENDER_DIGEST),),
    }
    assert _dom_decision((OTHER_ARTIFACT,), **both).publishable_now
    assert _dom_decision(
        (),
    ).publishable_now
    blocked(_dom_decision((OTHER_ARTIFACT,)), R.ARTIFACT_NOT_STORED, Q)  # never stored
    unlisted = {"known_artifacts": (raw_artifact(), xhr)}  # stored but not in the run
    blocked(_dom_decision((OTHER_ARTIFACT,), **unlisted), R.FETCH_NOT_VERIFIED, Q)
    failed = raw_artifact(OTHER_ARTIFACT, "application/json", http_status=500)
    bad_status = {"known_artifacts": (raw_artifact(), failed), "fetch_runs": both["fetch_runs"]}
    blocked(_dom_decision((OTHER_ARTIFACT,), **bad_status), R.ARTIFACT_HTTP_STATUS, Q)


def test_response_ids_are_artifact_ids_sorted_and_unique() -> None:
    locator = RenderedDomLocator(
        selector_type="css",
        selector="x",
        rendered_text_sha256=TEXT_HASH,
        response_ids=(OTHER_ARTIFACT, ARTIFACT_ID, OTHER_ARTIFACT),
    )
    assert locator.response_ids == (ARTIFACT_ID, OTHER_ARTIFACT)
    with pytest.raises(ValidationError):
        RenderedDomLocator(
            selector_type="css",
            selector="x",
            rendered_text_sha256=TEXT_HASH,
            response_ids=("resp-1",),
        )


def test_baseline_context_is_fully_bound_and_publishes() -> None:
    decision = decide()
    assert decision.reasons == ()
    assert decision.ranking_eligible

"""Publication gates added after review: movement, validity, evidence, HELOC, audit inputs."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    ArmTerms,
    CanonicalScenarioId,
    FieldEvidence,
    FixedEquityDetails,
    HelocDetails,
    IntroRate,
    LienPosition,
    MatchState,
    NormalizedOffer,
    OfferSnapshot,
    Placement,
    PriorPublication,
    ProductFamily,
    PublicationDecision,
    PublicationOutcome,
    ReasonCode,
    ReviewSubject,
    ReviewVerdict,
    TierDimension,
    WithdrawalScope,
    evaluate_publication,
)

from .factories import (
    ARTIFACT_ID,
    ARTIFACT_ID_2,
    EVALUATED_AT,
    IMAGE_ARTIFACT_ID,
    T0,
    adjustable_details,
    condition,
    context,
    equity_offer,
    evidence_paths_for,
    fetch_run,
    grid_tier,
    heloc_details,
    heloc_offer,
    margin,
    matches_all,
    prior,
    product,
    product_review,
    provenance,
    published_snapshot,
    purchase_offer,
    rate,
    raw_artifact,
    relationship,
    review,
    validation,
    withdrawal,
)

R = ReasonCode
D = Decimal
HELOC_PRODUCT = product(family=ProductFamily.HELOC)
EQUITY_PRODUCT = product(family=ProductFamily.HOME_EQUITY_LOAN)


def decide(
    offer: NormalizedOffer, base: NormalizedOffer | None = None, **ctx: Any
) -> PublicationDecision:
    """Decide ``offer`` against a prior built from ``base`` (default: ``offer`` itself)."""
    return evaluate_publication(offer, context(base or offer, subject=offer, **ctx))


# --- corroborated rate movement has no magnitude limit (D-20) -----------------------------


def _heloc(**overrides: Any) -> NormalizedOffer:
    return heloc_offer(heloc_details(**overrides))


@pytest.mark.parametrize(
    ("changed", "structural"),
    [
        (_heloc(margin=margin("1.000")), False),  # +50bp margin and post-intro: allowed
        (_heloc(margin=margin("1.001")), False),
        (_heloc(margin=margin("-0.100")), False),  # Prime minus 0.10: -60bp
        (_heloc(index_rate=rate("8.001")), False),
        (_heloc(floor=rate("4.500")), False),  # floor not binding: post-intro unmoved, +50bp
        (_heloc(floor=rate("9.000")), False),  # +500bp is allowed with corroboration
        (_heloc(ceiling=rate("18")), True),  # added path -> structural, no pairing
    ],
)
def test_heloc_value_moves_anywhere_publish_but_structure_still_requires_review(
    changed: NormalizedOffer, structural: bool
) -> None:
    decision = decide(changed, heloc_offer(), product=HELOC_PRODUCT)
    assert R.RATE_MOVEMENT_EXCEEDS_LIMIT not in decision.reasons
    if structural:
        assert R.STRUCTURAL_CHANGE in decision.reasons
        assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
        return
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH


def test_heloc_intro_rate_jump_publishes() -> None:
    def with_intro(value: str) -> NormalizedOffer:
        return _heloc(intro=IntroRate(rate=rate(value), duration_months=6))

    ok = decide(with_intro("3.500"), with_intro("3.000"), product=HELOC_PRODUCT)
    assert ok.outcome is PublicationOutcome.AUTO_PUBLISH
    jump = decide(with_intro("4.000"), with_intro("3.000"), product=HELOC_PRODUCT)
    assert jump.outcome is PublicationOutcome.AUTO_PUBLISH
    assert R.RATE_MOVEMENT_EXCEEDS_LIMIT not in jump.reasons


def test_tier_rate_jump_preserves_headline_mismatch_placement() -> None:
    def tiered(value: str) -> NormalizedOffer:
        return purchase_offer(tiers=(grid_tier("t", (TierDimension.LTV, 0, 80), rate_value=value),))

    # The tier disagrees with the headline (a TIER_RATE_MISMATCH) but the movement is the point.
    decision = decide(tiered("6.700"), tiered("6.125"))
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.BROWSE_ONLY
    assert R.TIER_RATE_MISMATCH in decision.reasons
    assert R.RATE_MOVEMENT_EXCEEDS_LIMIT not in decision.reasons
    assert decide(tiered("6.125"), tiered("6.125")).outcome is PublicationOutcome.AUTO_PUBLISH


def test_arm_margin_jump_preserves_missing_scenario_quarantine() -> None:
    def arm_offer(value: str) -> NormalizedOffer:
        arm = ArmTerms(
            initial_period_months=84,
            adjustment_interval_months=12,
            index_name="SOFR",
            margin=margin(value),
            initial_cap=D(5),
            periodic_cap=D(2),
            lifetime_cap=D(5),
        )
        return purchase_offer(
            details=adjustable_details(arm), scenario_id=None, scenario_matches=()
        )

    assert decide(arm_offer("2.800"), arm_offer("2.750")).outcome is (
        PublicationOutcome.QUARANTINE  # No evidence-backed scenario for this ARM.
    )
    jump = decide(arm_offer("3.500"), arm_offer("2.750"))
    assert jump.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_SCENARIO_MISMATCH in jump.reasons
    assert R.RATE_MOVEMENT_EXCEEDS_LIMIT not in jump.reasons


def test_added_or_removed_rate_path_requires_review() -> None:
    base = purchase_offer()
    for changed in (purchase_offer(apr=None), purchase_offer(points=None)):
        decision = decide(changed, base)
        assert R.STRUCTURAL_CHANGE in decision.reasons
        assert not decision.publishable_now


def test_unpairable_value_change_requires_review() -> None:
    offer = purchase_offer(note_rate=rate("6.125"))
    moved = purchase_offer(note_rate=rate("6.150"))
    # A prior snapshot that lost its path detail cannot bound a changed value fingerprint.
    # Validated construction forbids this (see test_forged_snapshot_*), so skip validation.
    blind = OfferSnapshot.from_offer(offer).model_copy(update={"rates": ()})
    lossy_prior = PriorPublication.model_construct(
        snapshot=blind,
        published=published_snapshot(offer),
        review=review(offer),
        last_value_change_at=T0 - timedelta(days=1),
    )
    decision = evaluate_publication(moved, context(moved, prior=lossy_prior))
    assert R.RATE_MOVEMENT_UNBOUNDED in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert not decision.publishable_now


@pytest.mark.parametrize("missing_path", ["note_rate", "apr", "points"])
def test_large_movement_needs_two_captures_of_each_new_value(missing_path: str) -> None:
    baseline = purchase_offer()
    moved = purchase_offer(note_rate=rate("12.125"), apr=rate("12.250"))
    paths = tuple(
        path
        for path in evidence_paths_for(moved)
        if not (path.path == missing_path and path.capture_id == "fetch-002")
    )
    decision = decide(moved, baseline, evidence_paths=paths)
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.INSUFFICIENT_EVIDENCE_PATHS in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize("path", ["note_rate", "apr"])
def test_old_value_does_not_corroborate_a_large_new_value(path: str) -> None:
    baseline = purchase_offer()
    moved = purchase_offer(note_rate=rate("12.125"), apr=rate("12.250"))
    old = next(
        p for p in evidence_paths_for(baseline) if p.path == path and p.capture_id == "fetch-002"
    )
    paths = tuple(
        old if p.path == path and p.capture_id == "fetch-002" else p
        for p in evidence_paths_for(moved)
    )
    decision = decide(moved, baseline, evidence_paths=paths)
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_VALUE_MISMATCH in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize("spacing", [timedelta(hours=3), timedelta(hours=3, seconds=-1)])
def test_large_movement_keeps_three_hour_capture_boundary(spacing: timedelta) -> None:
    moved = purchase_offer(note_rate=rate("12.125"), apr=rate("12.250"))
    seen = T0 - spacing
    second = raw_artifact(artifact_id=ARTIFACT_ID_2, fetch_run_id="fetch-002", fetched_at=seen)
    paths = tuple(
        p.model_copy(update={"observed_at": seen}) if p.capture_id == "fetch-002" else p
        for p in evidence_paths_for(moved)
    )
    ctx = context(
        purchase_offer(),
        subject=moved,
        evidence_paths=paths,
        known_artifacts=(raw_artifact(), second),
        fetch_runs=(
            fetch_run(ARTIFACT_ID),
            fetch_run(
                ARTIFACT_ID_2,
                run_id="fetch-002",
                started_at=seen - timedelta(seconds=1),
                finished_at=seen,
            ),
        ),
    )
    decision = evaluate_publication(moved, ctx)
    if spacing == timedelta(hours=3):
        assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
        assert decision.reasons == ()
    else:
        assert decision.outcome is PublicationOutcome.QUARANTINE
        assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
        assert not decision.publishable_now


def test_failed_range_validation_still_quarantines_a_corroborated_large_movement() -> None:
    moved = purchase_offer(note_rate=rate("12.125"), apr=rate("12.250"))
    decision = decide(
        moved,
        purchase_offer(),
        validations=(validation("rule.rate-range", moved, passed=False),),
    )
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.VALIDATION_FAILED in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize("value", ["-0.00001", "100.00001", "NaN", "Infinity"])
def test_rate_range_remains_enforced_before_publication(value: str) -> None:
    with pytest.raises(ValidationError):
        purchase_offer(note_rate=rate(value))


def test_large_movement_does_not_bypass_withdrawn_automatic_prior() -> None:
    moved = purchase_offer(note_rate=rate("12.125"), apr=rate("12.250"))
    baseline = prior(purchase_offer(), reviewed=False)
    decision = decide(
        moved,
        purchase_offer(),
        prior=baseline,
        withdrawals=(withdrawal(WithdrawalScope.SNAPSHOT, baseline.published.snapshot_id),),
    )
    assert decision.outcome is PublicationOutcome.SUPPRESS
    assert R.DENYLISTED in decision.reasons
    assert R.PRIOR_NOT_REVIEWED in decision.reasons
    assert not decision.publishable_now


# --- validity window ----------------------------------------------------------------------


def test_validity_window_boundary_is_96_hours_after_observation() -> None:
    at_limit = purchase_offer(valid_until=T0 + timedelta(hours=96))
    assert R.VALIDITY_TOO_LONG not in decide(at_limit).reasons
    too_long = purchase_offer(valid_until=T0 + timedelta(hours=96, seconds=1))
    decision = decide(too_long)
    assert R.VALIDITY_TOO_LONG in decision.reasons
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE


def test_missing_validity_never_publishes_even_if_fresh() -> None:
    decision = decide(purchase_offer(valid_until=None))
    assert decision.reasons == (R.VALIDITY_MISSING,)
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE


# --- HELOC post-intro ---------------------------------------------------------------------


def test_incomplete_heloc_is_unpublishable_even_with_reviewed_history() -> None:
    offer = _heloc(floor=None)
    decision = decide(offer, product=HELOC_PRODUCT)
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE
    assert not decision.publishable_now


def test_stated_post_intro_that_disagrees_with_components_is_quarantined() -> None:
    conflicting = _heloc(stated_post_intro_apr=rate("8.250"))  # components derive 8.000
    decision = decide(conflicting, product=HELOC_PRODUCT)
    assert R.HELOC_POST_INTRO_CONFLICT in decision.reasons
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE
    # Both source facts are retained, and the model does not pick one.
    details = conflicting.details
    assert isinstance(details, HelocDetails)
    assert details.stated_post_intro_apr is not None
    assert details.index_rate is not None
    agreeing = decide(_heloc(stated_post_intro_apr=rate("8.000")), product=HELOC_PRODUCT)
    assert R.HELOC_POST_INTRO_CONFLICT not in agreeing.reasons
    assert agreeing.outcome is PublicationOutcome.AUTO_PUBLISH


def test_stated_apr_with_partial_components_is_not_a_conflict() -> None:
    partial = _heloc(stated_post_intro_apr=rate("8.250"), floor=None)
    assert R.HELOC_POST_INTRO_CONFLICT not in decide(partial, product=HELOC_PRODUCT).reasons


def test_prime_minus_margin_derives_and_publishes() -> None:
    offer = _heloc(margin=margin("-0.250"))
    assert isinstance(offer.details, HelocDetails)
    assert offer.details.post_intro().rate == D("7.250")
    decision = decide(offer, product=HELOC_PRODUCT)
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED


# --- second-lien scenarios ----------------------------------------------------------------


@pytest.mark.parametrize("lien", [LienPosition.FIRST, LienPosition.UNKNOWN])
def test_equity_and_heloc_scenarios_require_second_lien(lien: LienPosition) -> None:
    equity = equity_offer(details=FixedEquityDetails(term_months=120, lien_position=lien))
    equity_decision = decide(equity, product=EQUITY_PRODUCT)
    assert R.SCENARIO_TERMS_DIFFER in equity_decision.reasons
    assert not equity_decision.ranking_eligible
    heloc = heloc_offer(heloc_details(lien_position=lien))
    heloc_decision = decide(heloc, product=HELOC_PRODUCT)
    assert R.SCENARIO_TERMS_DIFFER in heloc_decision.reasons
    assert heloc_decision.placement is Placement.BROWSE_ONLY


def test_second_lien_equity_and_heloc_rank() -> None:
    assert decide(equity_offer(), product=EQUITY_PRODUCT).ranking_eligible
    assert decide(heloc_offer(), product=HELOC_PRODUCT).ranking_eligible


# --- scenario promotion needs review ------------------------------------------------------


def test_promoting_unstated_scenario_dimension_to_match_requires_review() -> None:
    full = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    unstated = full[0].model_copy(update={"state": MatchState.UNSTATED, "source_text": None})
    before = purchase_offer(scenario_matches=(unstated, *full[1:]))
    decision = decide(purchase_offer(), before)
    assert R.STRUCTURAL_CHANGE in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW


def test_promoting_a_condition_to_matched_requires_review() -> None:
    before = purchase_offer(conditions=(condition(state=MatchState.UNSTATED),))
    after = purchase_offer(conditions=(condition(state=MatchState.MATCHES),))
    decision = decide(after, before)
    assert R.STRUCTURAL_CHANGE in decision.reasons
    assert not decision.publishable_now


# --- relationships ------------------------------------------------------------------------


def test_source_wide_relationship_does_not_cover_every_product() -> None:
    decision = decide(purchase_offer(), relationship=relationship(product_id=None))
    assert R.RELATIONSHIP_NOT_PRODUCT_BOUND in decision.reasons
    assert decision.outcome is PublicationOutcome.QUARANTINE


# --- field-level evidence -----------------------------------------------------------------


def _without(offer: NormalizedOffer, path: str) -> NormalizedOffer:
    kept = tuple(e for e in offer.field_provenance if e.path != path)
    return offer.model_copy(update={"field_provenance": kept})  # skips validation on purpose


def test_fully_mapped_offer_has_no_field_evidence_reason() -> None:
    offer = purchase_offer(
        conditions=(condition(),), tiers=(grid_tier("t", (TierDimension.LTV, 0, 80)),)
    )
    assert offer.material_paths() >= {
        "details.term_months",
        "details.rate_type",
        "details.lock_days",
        "conditions.cond-1",
        "tiers.t.bounds",
    }
    assert {e.path for e in offer.field_provenance} == offer.material_paths()
    assert R.FIELD_EVIDENCE_MISSING not in decide(offer).reasons


@pytest.mark.parametrize(
    "path", ["details.term_months", "details.lock_days", "conditions.cond-1", "tiers.t.bounds"]
)
def test_missing_field_mapping_fails_closed(path: str) -> None:
    offer = purchase_offer(
        conditions=(condition(),), tiers=(grid_tier("t", (TierDimension.LTV, 0, 80)),)
    )
    broken = _without(offer, path)
    decision = decide(broken, offer)
    assert R.FIELD_EVIDENCE_MISSING in decision.reasons
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE


def test_offer_with_no_field_mappings_at_all_fails_closed() -> None:
    bare = purchase_offer(field_provenance=())
    decision = decide(bare)
    assert R.FIELD_EVIDENCE_MISSING in decision.reasons
    assert not decision.publishable_now


def test_every_product_kind_lists_its_material_details() -> None:
    assert equity_offer().material_paths() == {"details.term_months", "details.lien_position"}
    heloc = heloc_offer(
        heloc_details(
            intro=IntroRate(rate=rate("3.990"), duration_months=6),
            draw_period_months=120,
            max_cltv=D(90),
        )
    )
    assert heloc.material_paths() == {
        "details.index_name",
        "details.lien_position",
        "details.draw_period_months",
        "details.repayment_period_months",
        "details.max_cltv",
        "details.intro.duration_months",
    }


def test_field_evidence_rejects_dangling_and_duplicate_paths() -> None:
    evidence = FieldEvidence(path="details.term_months", provenance=provenance())
    ghost = FieldEvidence(path="details.not_a_field", provenance=provenance())
    with pytest.raises(ValueError, match="unknown paths"):
        purchase_offer(field_provenance=(ghost,))
    with pytest.raises(ValueError, match="duplicate field_provenance"):
        purchase_offer(field_provenance=(evidence, evidence))


def test_field_evidence_is_checked_for_artifacts_like_any_provenance() -> None:
    elsewhere = provenance(artifact_id="e" * 64)
    offer = purchase_offer(
        field_provenance=tuple(
            FieldEvidence(path=path, provenance=elsewhere)
            for path in sorted(purchase_offer().material_paths())
        )
    )
    assert R.ARTIFACT_NOT_STORED in decide(offer).reasons


# --- artifact/locator consistency ----------------------------------------------------------


def test_locator_must_fit_the_stored_artifact_media_type() -> None:
    offer = purchase_offer()
    pdf = raw_artifact(media_type="application/pdf")  # html locators cannot address a PDF
    decision = decide(offer, known_artifacts=(pdf,))
    assert R.ARTIFACT_LOCATOR_MISMATCH in decision.reasons
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.ARTIFACT_LOCATOR_MISMATCH not in decide(offer).reasons
    xhtml = raw_artifact(media_type="application/xhtml+xml")
    assert R.ARTIFACT_LOCATOR_MISMATCH not in decide(offer, known_artifacts=(xhtml,)).reasons
    assert raw_artifact().artifact_id != IMAGE_ARTIFACT_ID


# --- auditable review and withdrawal inputs ------------------------------------------------


def test_prior_review_must_cover_the_prior_snapshot() -> None:
    offer, other = purchase_offer(), purchase_offer(note_rate=rate("6.250"))
    with pytest.raises(ValueError, match="does not cover"):
        PriorPublication(
            snapshot=OfferSnapshot.from_offer(offer),
            published=published_snapshot(offer),
            review=review(other),
            last_value_change_at=T0,
        )
    with pytest.raises(ValueError, match="offer review"):
        PriorPublication(
            snapshot=OfferSnapshot.from_offer(offer),
            published=published_snapshot(offer),
            review=product_review(),
            last_value_change_at=T0,
        )


def test_rejected_prior_review_is_not_a_review() -> None:
    offer = purchase_offer()
    rejected = PriorPublication(
        snapshot=OfferSnapshot.from_offer(offer),
        published=published_snapshot(offer),
        review=review(offer, ReviewVerdict.REJECTED),
        last_value_change_at=T0 - timedelta(days=1),
    )
    decision = decide(offer, prior=rejected)
    assert R.PRIOR_NOT_REVIEWED in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize(
    "bad_review",
    [
        None,
        product_review(verdict=ReviewVerdict.REJECTED),
        product_review(product_id="prod-other"),
    ],
)
def test_product_review_must_be_an_approval_of_this_product(bad_review: Any) -> None:
    decision = decide(purchase_offer(), product_review=bad_review)
    assert R.PRODUCT_NOT_REVIEWED in decision.reasons
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW


def test_offer_review_cannot_stand_in_for_a_product_review() -> None:
    offer = purchase_offer()
    assert review(offer).subject is ReviewSubject.OFFER
    decision = decide(offer, product_review=review(offer))
    assert R.PRODUCT_NOT_REVIEWED in decision.reasons


def test_withdrawals_by_scope_and_target() -> None:
    offer = purchase_offer()
    exact = withdrawal(WithdrawalScope.OFFER, "offer-001")
    assert R.DENYLISTED in decide(offer, withdrawals=(exact,)).reasons
    for scope, target in (
        (WithdrawalScope.OFFER, "offer-other"),
        (WithdrawalScope.PRODUCT, "prod-other"),
        (WithdrawalScope.INSTITUTION, "999"),
        (WithdrawalScope.SNAPSHOT, "snap-other"),
    ):
        entry = withdrawal(scope, target)
        assert R.DENYLISTED not in decide(offer, withdrawals=(entry,), snapshot_id="snap-1").reasons
    # A snapshot-scoped entry is never skipped: with no snapshot id the offer fails closed.
    snap = withdrawal(WithdrawalScope.SNAPSHOT, "snap-1")
    assert R.DENYLISTED in decide(offer, withdrawals=(snap,)).reasons
    assert R.DENYLISTED in decide(offer, withdrawals=(snap,), snapshot_id="snap-1").reasons


def test_withdrawal_windows() -> None:
    offer = purchase_offer()
    future = withdrawal(
        WithdrawalScope.OFFER, "offer-001", withdrawn_at=EVALUATED_AT + timedelta(days=1)
    )
    assert R.DENYLISTED not in decide(offer, withdrawals=(future,)).reasons
    lifted = withdrawal(
        WithdrawalScope.OFFER,
        "offer-001",
        lifted_at=EVALUATED_AT - timedelta(minutes=1),
        lifted_by="Sid",
    )
    assert R.DENYLISTED not in decide(offer, withdrawals=(lifted,)).reasons
    ends_now = withdrawal(
        WithdrawalScope.OFFER, "offer-001", lifted_at=EVALUATED_AT, lifted_by="Sid"
    )
    assert R.DENYLISTED not in decide(offer, withdrawals=(ends_now,)).reasons
    still = withdrawal(
        WithdrawalScope.OFFER, "offer-001", lifted_at=EVALUATED_AT + timedelta(1), lifted_by="Sid"
    )
    assert R.DENYLISTED in decide(offer, withdrawals=(still,)).reasons

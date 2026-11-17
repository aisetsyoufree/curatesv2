"""Publication-gate gaps from the AUD1 audit. Each test fails closed."""

from datetime import timedelta
from decimal import Decimal

from cu_rates.contracts import (
    CanonicalScenarioId,
    Correction,
    DimensionMatch,
    EvidenceKind,
    FetchMethod,
    MatchState,
    ParserVersion,
    Placement,
    ProductFamily,
    PublicationOutcome,
    ReasonCode,
    RenderedDomLocator,
    WithdrawalScope,
    evaluate_publication,
    offer_fingerprints,
)
from cu_rates.contracts.evidence import Provenance
from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, StatedBound, computed_match_state
from cu_rates.contracts.sources import profile_sha256

from .factories import (
    ARTIFACT_ID,
    CONFIG_HASH,
    LAYOUT_SHA,
    PARSER,
    SOURCE_ID,
    SOURCE_URL,
    T0,
    TEXT_HASH,
    context,
    extraction,
    fetch_profile,
    fetch_run,
    heloc_details,
    heloc_offer,
    matches_all,
    prior,
    product,
    provenance,
    purchase_offer,
    rate,
    rate_source,
    raw_artifact,
    source_approval,
    source_fingerprint,
    withdrawal,
)
from .test_publication import decide
from .test_publication_reobservation import decide as reobserved
from .test_publication_reobservation import reobserved_offer

R = ReasonCode
OTHER = "c" * 64


def test_source_withdrawal_suppresses_that_source() -> None:
    entry = withdrawal(WithdrawalScope.SOURCE, SOURCE_ID)
    decision = decide(withdrawals=(entry,))
    assert decision.outcome is PublicationOutcome.SUPPRESS
    assert R.DENYLISTED in decision.reasons
    other = withdrawal(WithdrawalScope.SOURCE, "src-other", withdrawal_id="wd-other")
    assert decide(withdrawals=(other,)).publishable_now


def test_ordinary_fetch_must_match_the_approved_profile_digest() -> None:
    """T014 stores the digest. A caller-supplied profile or a missing digest fails closed."""
    assert decide().publishable_now
    missing = decide(
        source_approval=source_approval(approved_profile_sha256=None),
        fetch_runs=(fetch_run(profile_sha256=None),),
    )
    assert missing.outcome is PublicationOutcome.QUARANTINE
    assert R.PROFILE_UNAPPROVED in missing.reasons
    invented = decide(approved_profile=fetch_profile(timeout_seconds=31))
    assert R.PROFILE_UNAPPROVED in invented.reasons
    assert not invented.publishable_now
    early = source_approval(decided_at=T0)  # the baseline run started a minute before T0
    predates = decide(source_approval=early)
    assert R.PROFILE_UNAPPROVED in predates.reasons
    assert not predates.publishable_now


def test_304_cannot_publish_a_rate_the_original_extraction_did_not_verify() -> None:
    changed = reobserved_offer(note_rate=rate("6.130"))
    decision = reobserved(changed)
    assert decision.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert R.REOBSERVATION_CLAIM_CHANGED in decision.reasons
    verified = prior(purchase_offer())
    assert verified.review is not None
    unbound = verified.model_copy(
        update={"review": verified.review.model_copy(update={"reviewed_evidence_sha256": "0" * 64})}
    )
    extracted = reobserved(prior=unbound)
    assert R.REOBSERVATION_CLAIM_CHANGED in extracted.reasons
    assert not extracted.publishable_now
    missing = verified.model_copy(
        update={"review": verified.review.model_copy(update={"reviewed_evidence_sha256": None})}
    )
    assert R.REOBSERVATION_CLAIM_CHANGED in reobserved(prior=missing).reasons
    assert verified.verified_configuration_sha256 == CONFIG_HASH
    assert reobserved().publishable_now


def test_fingerprint_does_not_clear_a_contradictory_correction_without_evidence() -> None:
    offer = purchase_offer()
    recent = T0 - timedelta(hours=1)
    bad = Correction(
        schema_version=offer.schema_version,
        correction_id="corr-bad",
        offer_id=offer.offer_id,
        field_path="note_rate",
        previous_value="6.000",
        corrected_value="6.250",
        actor="Sid",
        corrected_at=recent,
        reason="Claimed typo.",
        corrected_fingerprints=offer_fingerprints(offer),
    )
    assert R.CORRECTION_PENDING in decide(offer, corrections=(bad,)).reasons
    good = bad.model_copy(
        update={
            "correction_id": "corr-good",
            "corrected_value": "6.125",
            "evidence": (provenance("6.125%"),),
        }
    )
    assert decide(offer, corrections=(good,)).publishable_now


def test_recorded_pass_cannot_publish_apr_below_the_note_rate() -> None:
    offer = purchase_offer(note_rate=rate("6.125"), apr=rate("6.000"))
    decision = decide(offer, prior=prior(offer))
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.VALIDATION_FAILED in decision.reasons
    assert not decision.publishable_now


def test_scenario_match_needs_a_located_statement_and_is_computed() -> None:
    scenario_id = CanonicalScenarioId.PURCHASE_30Y_FIXED
    bare = tuple(
        DimensionMatch(dimension=item.dimension, state=MatchState.MATCHES, source_text="unrelated")
        for item in matches_all(scenario_id)
    )
    unlocated = decide(purchase_offer(scenario_matches=bare))
    assert unlocated.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_UNSTATED in unlocated.reasons
    assert not unlocated.ranking_eligible

    scenario = CANONICAL_SCENARIOS[scenario_id]
    full = list(matches_all(scenario_id))
    ltv = next(item for item in full if item.dimension.value == "ltv")
    conflict = ltv.model_copy(
        update={
            "stated": StatedBound(exact=Decimal(50)),
            "source_text": "50%",
            "provenance": provenance("50%"),
        }
    )
    assert conflict.stated is not None
    assert (
        computed_match_state(scenario, conflict.stated, conflict.dimension) is MatchState.CONFLICTS
    )
    full[full.index(ltv)] = conflict
    conflicting = decide(purchase_offer(scenario_matches=tuple(full)))
    assert conflicting.placement is Placement.BROWSE_ONLY
    assert R.SCENARIO_CONFLICT in conflicting.reasons

    contained = ltv.model_copy(
        update={
            "stated": StatedBound(lower=Decimal(70), upper=Decimal(90)),
            "source_text": "70% to 90%",
            "provenance": provenance("70% to 90%"),
        }
    )
    full[full.index(conflict)] = contained
    assert decide(purchase_offer(scenario_matches=tuple(full))).ranking_eligible


def test_linked_response_must_share_the_render_run_and_an_approved_origin() -> None:
    locator = RenderedDomLocator(
        selector_type="css",
        selector="div.rate",
        rendered_text_sha256=TEXT_HASH,
        response_ids=(OTHER,),
    )
    evidence = Provenance(
        artifact_id=ARTIFACT_ID, locator=locator, source_text="6.125%", extraction=extraction()
    )
    offer = purchase_offer(supporting_evidence=(evidence,))
    page = raw_artifact()
    profile = fetch_profile(method=FetchMethod.RENDERED_BROWSER)
    digest = profile_sha256(profile)
    rendered = rate_source(evidence_kind=EvidenceKind.RENDERED_DOM)

    other_run = raw_artifact(
        OTHER, "application/json", source_url=SOURCE_URL, fetch_run_id="fetch-xhr"
    )
    crossed_decision = evaluate_publication(
        offer,
        context(
            offer,
            source=rendered,
            approved_profile=profile,
            source_approval=source_approval(approved_profile_sha256=digest),
            known_artifacts=(page, other_run),
            fetch_runs=(
                fetch_run(ARTIFACT_ID, profile_sha256=digest),
                fetch_run(OTHER, run_id="fetch-xhr", profile_sha256=digest),
            ),
        ),
    )
    assert crossed_decision.outcome is PublicationOutcome.QUARANTINE
    assert R.FETCH_NOT_VERIFIED in crossed_decision.reasons

    vendor_url = "https://vendor.example/rates.json"
    vendor = raw_artifact(
        OTHER, "application/json", source_url=vendor_url, fetch_run_id="fetch-001"
    )
    unapproved = evaluate_publication(
        offer,
        context(
            offer,
            source=rendered,
            approved_profile=profile,
            source_approval=source_approval(approved_profile_sha256=digest),
            known_artifacts=(page, vendor),
            fetch_runs=(fetch_run(ARTIFACT_ID, OTHER, profile_sha256=digest),),
        ),
    )
    assert R.ARTIFACT_SOURCE_MISMATCH in unapproved.reasons
    assert not unapproved.publishable_now

    approved = evaluate_publication(
        offer,
        context(
            offer,
            source=rendered,
            approved_profile=profile,
            source_approval=source_approval(
                approved_origins=(vendor_url,), approved_profile_sha256=digest
            ),
            known_artifacts=(page, vendor),
            fetch_runs=(fetch_run(ARTIFACT_ID, OTHER, profile_sha256=digest),),
        ),
    )
    assert approved.publishable_now


def test_heloc_ranking_requires_draw_and_repayment_periods() -> None:
    heloc_product = product(family=ProductFamily.HELOC)
    ranked = decide(heloc_offer(), product=heloc_product)
    assert ranked.placement is Placement.RANKED
    missing_draw = heloc_offer(heloc_details(draw_period_months=None))
    draw = decide(missing_draw, product=heloc_product)
    assert draw.outcome is PublicationOutcome.AUTO_PUBLISH
    assert draw.placement is Placement.BROWSE_ONLY
    assert R.HELOC_PERIODS_INCOMPLETE in draw.reasons
    missing_repay = heloc_offer(heloc_details(repayment_period_months=None))
    repay = decide(missing_repay, product=heloc_product)
    assert R.HELOC_PERIODS_INCOMPLETE in repay.reasons
    assert not repay.ranking_eligible


def test_layout_fingerprint_must_match_the_reviewed_source_and_adapter() -> None:
    """T040/T045 emit the digest. T014 stores it on the approval. The gate only compares."""
    assert decide().publishable_now
    assert source_approval().approved_layout_sha256 == LAYOUT_SHA
    missing = decide(source_fingerprints=())
    assert missing.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert R.LAYOUT_UNREVIEWED in missing.reasons
    redesigned = decide(source_fingerprints=(source_fingerprint(structure_sha256="cd" * 32),))
    assert R.LAYOUT_UNREVIEWED in redesigned.reasons
    assert not redesigned.publishable_now
    other_adapter = ParserVersion(name="synthetic-html", version="9.0.0")
    moved = decide(
        source_fingerprints=(source_fingerprint(adapter=other_adapter),),
        approved_parsers=(PARSER, other_adapter),
    )
    assert R.LAYOUT_UNREVIEWED in moved.reasons
    unrecorded = decide(source_approval=source_approval(approved_layout_sha256=None, adapter=None))
    assert R.LAYOUT_UNREVIEWED in unrecorded.reasons
    assert not unrecorded.publishable_now

"""Independent regression cases for publication trust boundaries (AUD1)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cu_rates.contracts import (
    Correction,
    EvidenceKind,
    FetchMethod,
    MatchState,
    NormalizedOffer,
    ParserVersion,
    PublicationOutcome,
    RawArtifact,
    ReasonCode,
    ScenarioDimension,
    SourcedRate,
    TierDimension,
    WithdrawalScope,
    offer_evidence_sha256,
    offer_fingerprints,
)
from cu_rates.contracts.scenarios import StatedBound
from cu_rates.contracts.sources import profile_sha256

from . import factories as f
from .test_publication import decide
from .test_publication_binding import _dom_decision
from .test_publication_reobservation import decide as reobserve
from .test_publication_reobservation import observation, original_run, reobserved_offer

OTHER = "c" * 64


def _failed_foreign_capture() -> RawArtifact:
    return f.raw_artifact(
        OTHER,
        source_url="https://elsewhere.example/rates",
        fetched_at=f.T0 - timedelta(days=10),
        fetch_run_id="different-run",
        http_status=403,
    )


@pytest.mark.parametrize(
    ("text", "stated"),
    [
        ("Unrelated text", StatedBound(exact=Decimal(80))),
        ("80 points", StatedBound(exact=Decimal(80))),
        ("0% to 1000%", StatedBound(lower=Decimal(0), upper=Decimal(1000))),
    ],
)
def test_scenario_does_not_rank_a_mismatched_excerpt_unit_or_bound(
    text: str, stated: StatedBound
) -> None:
    offer = f.purchase_offer()
    matches = tuple(
        m.model_copy(
            update={"source_text": text, "stated": stated, "provenance": f.provenance(text)}
        )
        if m.dimension is ScenarioDimension.LTV
        else m
        for m in offer.scenario_matches
    )
    assert not decide(f.purchase_offer(scenario_matches=matches)).ranking_eligible


def test_scenario_does_not_rank_locators_from_failed_foreign_captures() -> None:
    offer = f.purchase_offer()
    matches = tuple(
        m.model_copy(update={"provenance": f.provenance(m.source_text or "", artifact_id=OTHER)})
        for m in offer.scenario_matches
    )
    assert not decide(
        f.purchase_offer(scenario_matches=matches),
        known_artifacts=(f.raw_artifact(), _failed_foreign_capture()),
    ).ranking_eligible


def test_scenario_requires_independent_verification_and_current_rate_run() -> None:
    assert decide().ranking_eligible
    assert not decide(verified_scenario_statements=()).ranking_eligible
    offer = f.purchase_offer()
    matches = tuple(
        m.model_copy(update={"provenance": f.provenance(m.source_text or "", artifact_id=OTHER)})
        if m.dimension is ScenarioDimension.LTV
        else m
        for m in offer.scenario_matches
    )
    older = f.T0 - timedelta(hours=1)
    result = decide(
        f.purchase_offer(scenario_matches=matches),
        known_artifacts=(
            f.raw_artifact(),
            f.raw_artifact(OTHER, fetched_at=older, fetch_run_id="earlier-run"),
        ),
        fetch_runs=(
            f.fetch_run(),
            f.fetch_run(
                OTHER,
                run_id="earlier-run",
                started_at=older - timedelta(minutes=1),
                finished_at=older + timedelta(minutes=1),
            ),
        ),
    )
    assert result.publishable_now
    assert ReasonCode.SCENARIO_UNSTATED in result.reasons
    assert not result.ranking_eligible


def test_latest_supporting_capture_cannot_make_old_rate_observation_current() -> None:
    older = f.T0 - timedelta(hours=1)
    offer = f.purchase_offer(supporting_evidence=(f.provenance("new page", artifact_id=OTHER),))
    result = decide(
        offer,
        known_artifacts=(
            f.raw_artifact(fetched_at=older),
            f.raw_artifact(OTHER, fetch_run_id="new-run"),
        ),
        fetch_runs=(
            f.fetch_run(
                started_at=older - timedelta(minutes=1), finished_at=older + timedelta(minutes=1)
            ),
            f.fetch_run(OTHER, run_id="new-run"),
        ),
    )
    assert ReasonCode.OBSERVATION_UNBOUND in result.reasons
    assert not result.publishable_now


def test_unverified_condition_cannot_rank_by_self_declaring_match() -> None:
    offer = f.purchase_offer(conditions=(f.condition(state=MatchState.MATCHES),))
    assert decide(offer).ranking_eligible
    result = decide(offer, verified_scenario_conditions=())
    assert ReasonCode.CONDITION_NOT_SATISFIED in result.reasons
    assert not result.ranking_eligible


@pytest.mark.parametrize("edit", ["locator", "rule"])
def test_304_cannot_change_reviewed_evidence_for_unchanged_numbers(edit: str) -> None:
    offer = reobserved_offer()
    assert offer.note_rate is not None
    original = offer.note_rate.provenance
    changed = (
        original.model_copy(
            update={"locator": original.locator.model_copy(update={"selector": "#unreviewed"})}
        )
        if edit == "locator"
        else original.model_copy(update={"extraction": f.extraction("different-rule")})
    )
    result = reobserve(
        reobserved_offer(note_rate=SourcedRate(value=offer.note_rate.value, provenance=changed))
    )
    assert ReasonCode.REOBSERVATION_CLAIM_CHANGED in result.reasons
    assert not result.publishable_now


def test_304_can_extend_multiple_exactly_reviewed_artifacts_but_not_unproven_ones() -> None:
    supporting = (
        f.provenance("Primary capture"),
        f.provenance("Second capture", artifact_id=OTHER),
    )
    before = f.purchase_offer(supporting_evidence=supporting)
    after = reobserved_offer(supporting_evidence=supporting)
    second_run = "second-run"
    history = f.prior(before)
    ctx = {
        "prior": history,
        "known_artifacts": (f.raw_artifact(), f.raw_artifact(OTHER, fetch_run_id=second_run)),
        "fetch_runs": (original_run(), original_run(run_id=second_run, artifact_ids=(OTHER,))),
    }
    observations = (
        observation(),
        observation(run_id="second-304", original_run_id=second_run, original_artifact_id=OTHER),
    )
    assert offer_evidence_sha256(before) == offer_evidence_sha256(after)
    assert reobserve(after, reobservations=observations, **ctx).ranking_eligible
    assert not reobserve(after, reobservations=observations[:1], **ctx).publishable_now
    withdrawn = f.withdrawal(WithdrawalScope.SOURCE, f.SOURCE_ID)
    suppressed = reobserve(after, reobservations=observations, withdrawals=(withdrawn,), **ctx)
    assert suppressed.outcome is PublicationOutcome.SUPPRESS


@pytest.mark.parametrize("later_review", [False, True])
def test_correction_needs_a_current_source_capture_even_with_review(later_review: bool) -> None:
    offer = f.purchase_offer()
    correction = Correction(
        schema_version=f.SV,
        correction_id="corr-aud1",
        offer_id=offer.offer_id,
        field_path="note_rate",
        previous_value="6.000",
        corrected_value="6.999" if later_review else "6.125",
        actor="synthetic-reviewer",
        corrected_at=f.T0 - timedelta(days=2) if later_review else f.T0 - timedelta(hours=1),
        reason="Synthetic correction",
        evidence=() if later_review else (f.provenance("Unrelated text", artifact_id=OTHER),),
        corrected_fingerprints=None if later_review else offer_fingerprints(offer),
    )
    result = decide(
        offer,
        corrections=(correction,),
        known_artifacts=(f.raw_artifact(), _failed_foreign_capture()),
    )
    assert ReasonCode.CORRECTION_PENDING in result.reasons
    assert not result.publishable_now


@pytest.mark.parametrize("excerpt", ["Unrelated text", "6.125%"])
def test_correction_requires_a_field_excerpt_that_states_the_replacement(excerpt: str) -> None:
    rate = f.rate("6.125", excerpt)
    offer = f.purchase_offer(note_rate=rate)
    correction = Correction(
        schema_version=f.SV,
        correction_id="corr-quoted",
        offer_id=offer.offer_id,
        field_path="note_rate",
        previous_value="6.000",
        corrected_value="6.125%",
        actor="synthetic-reviewer",
        corrected_at=f.T0 - timedelta(minutes=1),
        reason="Synthetic correction",
        evidence=(rate.provenance,),
        corrected_fingerprints=offer_fingerprints(offer),
    )
    result = decide(offer, corrections=(correction,))
    assert result.publishable_now is (excerpt == "6.125%")


def test_tier_apr_cannot_be_below_its_note_rate() -> None:
    broken = f.tier("low", 0, 80, TierDimension.LTV).model_copy(update={"apr": f.rate("6.000")})
    result = decide(f.purchase_offer(note_rate=None, apr=None, tiers=(broken,)))
    assert result.outcome is PublicationOutcome.QUARANTINE
    assert ReasonCode.VALIDATION_FAILED in result.reasons


@pytest.mark.parametrize(
    ("port", "accepted"), [("", True), (":443", True), (":0", False), (":99999", False)]
)
def test_xhr_origins_check_default_and_invalid_ports(port: str, accepted: bool) -> None:
    profile = f.fetch_profile(method=FetchMethod.RENDERED_BROWSER)
    digest = profile_sha256(profile)
    result = _dom_decision(
        (OTHER,),
        known_artifacts=(
            f.raw_artifact(),
            f.raw_artifact(
                OTHER, "application/json", source_url=f"https://vendor.example{port}/rates"
            ),
        ),
        fetch_runs=(f.fetch_run(f.ARTIFACT_ID, OTHER, profile_sha256=digest),),
        approved_profile=profile,
        source_approval=f.source_approval(
            approved_profile_sha256=digest, approved_origins=("https://vendor.example",)
        ),
    )
    assert result.publishable_now is accepted


def test_xhr_cannot_use_an_http_get_profile() -> None:
    profile = f.fetch_profile(method=FetchMethod.HTTP_GET)
    digest = profile_sha256(profile)
    result = _dom_decision(
        (OTHER,),
        source=f.rate_source(evidence_kind=EvidenceKind.RENDERED_DOM),
        known_artifacts=(f.raw_artifact(), f.raw_artifact(OTHER, "application/json")),
        fetch_runs=(f.fetch_run(f.ARTIFACT_ID, OTHER, profile_sha256=digest),),
        approved_profile=profile,
        source_approval=f.source_approval(approved_profile_sha256=digest),
    )
    assert not result.publishable_now


@pytest.mark.parametrize("wrong_capture", [False, True])
def test_reviewed_layout_requires_the_current_capture_and_extractor(wrong_capture: bool) -> None:
    alternate = ParserVersion(name="alternate", version="1")
    kwargs = (
        {}
        if wrong_capture
        else {
            "note_rate": f.rate("6.125").model_copy(
                update={"provenance": f.provenance("6.125%", parser=alternate)}
            )
        }
    )
    fingerprint = f.source_fingerprint(
        observed_at=f.T0 - timedelta(days=1) if wrong_capture else f.T0
    )
    result = decide(
        f.purchase_offer(**kwargs),
        approved_parsers=(f.PARSER, alternate),
        source_fingerprints=(fingerprint,),
    )
    assert ReasonCode.LAYOUT_UNREVIEWED in result.reasons
    assert not result.publishable_now


def test_pre_aud1_offer_preserves_exact_fp1_golden_hashes() -> None:
    # Generated with the actual pre-AUD1 implementation at 348114e, not this algorithm.
    fixture = Path(__file__).parent / "fixtures" / "v1_purchase_offer.json"
    fp = offer_fingerprints(NormalizedOffer.model_validate_json(fixture.read_text()))
    assert fp.algorithm == "fp1"
    assert fp.structural == "6bb2fd5b429d08404b9258d87b0597b118e90eccb8b11b2103013a770ead1af6"
    assert fp.value == "e06ab403059f74c9b436ee3d204d842a3ca9132ce468bf5b2a6a01924715806a"
    assert offer_fingerprints(f.purchase_offer()).algorithm == "fp2"

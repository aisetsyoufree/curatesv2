"""Tests for dual-evidence corroboration in the publication gate (D-18).

Every published rate/APR/points value requires at least two separately captured,
corroborating, authoritative evidence paths matching product, scenario, and observation
time window.
"""

from datetime import timedelta

from cu_rates.contracts import (
    CanonicalScenarioId,
    Placement,
    PublicationOutcome,
    ReasonCode,
    evaluate_publication,
)
from cu_rates.contracts.publication import DUAL_EVIDENCE_TIME_WINDOW

from .factories import (
    ARTIFACT_ID,
    T0,
    context,
    corroborating_path,
    evidence_path,
    purchase_offer,
)

R = ReasonCode


def test_negative_one_evidence_path_only() -> None:
    offer = purchase_offer(apr=None, points=None)
    path = evidence_path(path="note_rate", value="6.125")
    ctx = context(offer, evidence_paths=(path,))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.INSUFFICIENT_EVIDENCE_PATHS in decision.reasons
    assert not decision.publishable_now
    assert not decision.ranking_eligible


def test_negative_same_capture_cited_twice() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = evidence_path(path="note_rate", value="6.125")
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
    assert not decision.publishable_now


def test_negative_two_paths_identical_hashes() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(
        path="note_rate",
        value="6.125",
        artifact_id=ARTIFACT_ID,
        capture_id="fetch-001",
        observed_at=T0,
    )
    p2 = evidence_path(
        path="note_rate",
        value="6.125",
        artifact_id=ARTIFACT_ID,  # same hash
        capture_id="fetch-002",
        observed_at=T0 - timedelta(hours=1),
    )
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
    assert not decision.publishable_now


def test_negative_two_paths_mismatching_scenario() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(
        path="note_rate",
        value="6.125",
        scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED,
    )
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_SCENARIO_MISMATCH in decision.reasons
    assert not decision.publishable_now


def test_negative_two_paths_mismatching_product() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(path="note_rate", value="6.125", product_id="prod-other-product")
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_PRODUCT_MISMATCH in decision.reasons
    assert not decision.publishable_now


def test_negative_two_paths_outside_time_window() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125", observed_at=T0)
    p2 = corroborating_path(
        path="note_rate",
        value="6.125",
        observed_at=T0 - DUAL_EVIDENCE_TIME_WINDOW - timedelta(hours=2),
    )
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in decision.reasons
    assert not decision.publishable_now


def test_negative_two_paths_disagreeing_value() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(path="note_rate", value="6.250")
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_VALUE_MISMATCH in decision.reasons
    assert not decision.publishable_now


def test_negative_model_sourced_or_unclassified_path_not_evidence() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(path="note_rate", value="6.125", source_class="model")
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in decision.reasons
    assert not decision.publishable_now

    p3 = corroborating_path(path="note_rate", value="6.125", source_class="unclassified")
    ctx3 = context(offer, evidence_paths=(p1, p3))
    decision3 = evaluate_publication(offer, ctx3)

    assert decision3.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in decision3.reasons
    assert not decision3.publishable_now


def test_negative_non_authoritative_source_class() -> None:
    offer = purchase_offer(apr=None, points=None)
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(path="note_rate", value="6.125", source_class="web_search")
    ctx = context(offer, evidence_paths=(p1, p2))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in decision.reasons
    assert not decision.publishable_now


def test_positive_two_valid_distinct_captures_publishes() -> None:
    offer = purchase_offer()
    p1 = evidence_path(path="note_rate", value="6.125")
    p2 = corroborating_path(path="note_rate", value="6.125")
    p3 = evidence_path(path="apr", value="6.250")
    p4 = corroborating_path(path="apr", value="6.250")
    p5 = evidence_path(path="points", value="0")
    p6 = corroborating_path(path="points", value="0")

    ctx = context(offer, evidence_paths=(p1, p2, p3, p4, p5, p6))
    decision = evaluate_publication(offer, ctx)

    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED
    assert decision.publishable_now
    assert decision.ranking_eligible
    assert decision.reasons == ()


def test_property_no_combination_fewer_than_two_valid_distinct_paths_publishes() -> None:
    offer = purchase_offer()
    # Fully valid pairs for all three values:
    p_note_1 = evidence_path(path="note_rate", value="6.125")
    p_note_2 = corroborating_path(path="note_rate", value="6.125")
    p_apr_1 = evidence_path(path="apr", value="6.250")
    p_apr_2 = corroborating_path(path="apr", value="6.250")
    p_pts_1 = evidence_path(path="points", value="0")
    p_pts_2 = corroborating_path(path="points", value="0")

    # Incomplete combinations: any scenario where at least one value has fewer than 2 paths
    incomplete_combinations = [
        (),  # 0 paths total
        (p_note_1,),  # only 1 path for note_rate
        (p_note_1, p_note_2),  # note_rate corroborating, but apr and points have 0
        (p_note_1, p_note_2, p_apr_1),  # note_rate complete, apr has 1, points has 0
        (p_note_1, p_note_2, p_apr_1, p_apr_2),  # note_rate and apr complete, points has 0
        (p_note_1, p_note_2, p_apr_1, p_apr_2, p_pts_1),  # points has only 1 path
        (p_apr_1, p_apr_2, p_pts_1, p_pts_2),  # note_rate has 0 paths
        (p_note_1, p_apr_1, p_pts_1),  # each has exactly 1 path (total 3 paths, but 1 per value)
    ]

    for combo in incomplete_combinations:
        ctx = context(offer, evidence_paths=combo)
        decision = evaluate_publication(offer, ctx)
        assert decision.outcome is not PublicationOutcome.AUTO_PUBLISH
        assert not decision.publishable_now
        assert not decision.ranking_eligible
        assert R.INSUFFICIENT_EVIDENCE_PATHS in decision.reasons

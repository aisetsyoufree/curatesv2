"""A verified HTTP 304 is fresh evidence of an unchanged body, never publication approval."""

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    ConditionalValidator,
    FetchProfile,
    FetchRun,
    NormalizedOffer,
    NotModifiedObservation,
    Placement,
    PublicationDecision,
    PublicationOutcome,
    ReasonCode,
    RunStatus,
    WithdrawalScope,
    evaluate_publication,
)
from cu_rates.contracts.enums import FetchMethod
from cu_rates.contracts.publication import QUARANTINE_REASONS

from .factories import (
    ARTIFACT_ID,
    GIT_SHA,
    SOURCE_ID,
    SV,
    T0,
    context,
    fetch_run,
    prior,
    purchase_offer,
    source_approval,
    validation,
    validations_for,
    withdrawal,
)

R = ReasonCode
ETAG = ConditionalValidator(kind="etag", value='"v1"')
OBSERVED_AT = T0 + timedelta(hours=98)  # 304 time: the original 200 is by now past 96 hours
NOW = T0 + timedelta(hours=100)
OTHER_ARTIFACT = "c" * 64


def profile(**overrides: Any) -> FetchProfile:
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


def observation(**overrides: Any) -> NotModifiedObservation:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "run_id": "reobs-001",
        "source_id": SOURCE_ID,
        "profile_id": "profile-1",
        "git_sha": GIT_SHA,
        "started_at": OBSERVED_AT - timedelta(seconds=2),
        "observed_at": OBSERVED_AT,
        "original_run_id": "fetch-001",
        "original_artifact_id": ARTIFACT_ID,
        "validator": ETAG,
    }
    kwargs.update(overrides)
    return NotModifiedObservation(**kwargs)


def reobserved_offer(**overrides: Any) -> NormalizedOffer:
    """The same content as the prior state, observed again by 304 instead of a fresh 200."""
    kwargs: dict[str, Any] = {
        "observed_at": OBSERVED_AT,
        "valid_until": OBSERVED_AT + timedelta(days=3),
    }
    kwargs.update(overrides)
    return purchase_offer(**kwargs)


def decide(offer: NormalizedOffer | None = None, **overrides: Any) -> PublicationDecision:
    subject = offer or reobserved_offer()
    kwargs: dict[str, Any] = {
        "subject": subject,
        "evaluated_at": NOW,
        "fetch_runs": (fetch_run(conditional_validator=ETAG),),
        "reobservations": (observation(),),
        "approved_profile": profile(),
        "validations": validations_for(subject, evaluated_at=OBSERVED_AT),
    }
    kwargs.update(overrides)
    return evaluate_publication(subject, context(purchase_offer(), **kwargs))


def blocked(
    decision: PublicationDecision, outcome: PublicationOutcome, *reasons: ReasonCode
) -> None:
    assert decision.outcome is outcome
    assert not decision.publishable_now
    for reason in reasons:
        assert reason in decision.reasons


# --- a valid 304 -----------------------------------------------------------------------


def test_valid_304_keeps_an_unchanged_offer_fresh() -> None:
    decision = decide()
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH
    assert decision.placement is Placement.RANKED
    assert decision.reasons == ()


def test_without_the_304_the_old_fetch_is_stale() -> None:
    """The control: the identical offer is suppressed once the original 200 is too old."""
    stale = decide(
        reobserved_offer(observed_at=T0, valid_until=T0 + timedelta(days=3)), reobservations=()
    )
    blocked(stale, PublicationOutcome.SUPPRESS, R.STALE_WITHDRAWN, R.ARTIFACT_STALE)


def test_a_304_not_claimed_by_the_offer_does_not_refresh_it() -> None:
    old = reobserved_offer(observed_at=T0, valid_until=T0 + timedelta(days=3))
    blocked(decide(old), PublicationOutcome.SUPPRESS, R.STALE_WITHDRAWN, R.ARTIFACT_STALE)


def test_a_304_does_not_rewrite_the_original_artifact_or_run() -> None:
    obs, run = observation(), fetch_run(conditional_validator=ETAG)
    assert obs.http_status == 304
    assert obs.run_id != run.run_id
    assert run.artifact_ids == (ARTIFACT_ID,)
    assert decide().publishable_now


def test_304_is_inert_when_the_offer_rests_on_a_normal_200() -> None:
    fresh = purchase_offer()
    baseline = evaluate_publication(fresh, context(fresh))
    with_noise = evaluate_publication(
        fresh,
        context(fresh, reobservations=(observation(),), approved_profile=profile()),
    )
    assert baseline.outcome is PublicationOutcome.AUTO_PUBLISH
    assert with_noise == baseline


# --- missing or mismatched evidence ----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved_profile": None},
        {"approved_profile": profile(profile_id="profile-2")},
        {"approved_profile": profile(source_id="other-source")},
        {"reobservations": (observation(source_id="other-source"),)},
        {"reobservations": (observation(profile_id="profile-2"),)},
        {"reobservations": (observation(original_run_id="fetch-999"),)},
        {
            "reobservations": (
                observation(validator=ConditionalValidator(kind="etag", value='"v2"')),
            )
        },
        {
            "reobservations": (
                observation(validator=ConditionalValidator(kind="last_modified", value='"v1"')),
            )
        },
        {"fetch_runs": (fetch_run(),)},  # the original response recorded no validator
        {
            "fetch_runs": (
                fetch_run(conditional_validator=ConditionalValidator(kind="etag", value='"v0"')),
            )
        },
        {"fetch_runs": (fetch_run(conditional_validator=ETAG, profile_id="profile-2"),)},
        {"reobservations": (observation(started_at=T0),)},  # began before the original run ended
        {"source_approval": source_approval(decided_at=OBSERVED_AT)},  # began before approval
    ],
    ids=[
        "no-approved-profile",
        "profile-id-differs",
        "profile-for-another-source",
        "observation-for-another-source",
        "observation-under-another-profile",
        "observation-names-another-run",
        "validator-value-differs",
        "validator-kind-differs",
        "original-recorded-no-validator",
        "original-validator-differs",
        "original-run-used-another-profile",
        "started-before-original-finished",
        "started-before-approval",
    ],
)
def test_unproven_304_binding_is_blocked(overrides: dict[str, Any]) -> None:
    decision = decide(**overrides)
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )
    assert decision.placement is Placement.NONE  # and the stale original is not rescued


def test_304_for_another_artifact_is_not_a_reobservation_of_this_one() -> None:
    decision = decide(reobservations=(observation(original_artifact_id=OTHER_ARTIFACT),))
    blocked(decision, PublicationOutcome.SUPPRESS, R.OBSERVATION_UNBOUND, R.ARTIFACT_STALE)


def test_304_over_a_failed_original_run_is_not_fresh() -> None:
    failed = fetch_run(status=RunStatus.FAILED, error="timeout", artifact_ids=())
    decision = decide(fetch_runs=(failed,))
    blocked(decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.FETCH_NOT_VERIFIED)


def test_304_over_a_non_200_original_is_not_fresh() -> None:
    from .factories import raw_artifact

    decision = decide(known_artifacts=(raw_artifact(http_status=203),))
    blocked(decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED)


def test_conflicting_claims_at_the_same_time_fail_closed() -> None:
    other = observation(
        run_id="reobs-002", validator=ConditionalValidator(kind="etag", value='"v2"')
    )
    decision = decide(reobservations=(observation(), other))
    blocked(decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED)


# --- stale or future timestamps --------------------------------------------------------


def test_future_304_is_not_fresh_evidence() -> None:
    future = NOW + timedelta(hours=1)
    offer = reobserved_offer(observed_at=future, valid_until=future + timedelta(days=3))
    decision = decide(
        offer,
        reobservations=(observation(started_at=future - timedelta(seconds=2), observed_at=future),),
        validations=validations_for(offer, evaluated_at=NOW),
    )
    blocked(decision, PublicationOutcome.SUPPRESS, R.CLOCK_SKEW, R.REOBSERVATION_UNVERIFIED)


def test_stale_304_is_withdrawn_like_any_old_observation() -> None:
    stale_at = NOW - timedelta(hours=97)
    offer = reobserved_offer(observed_at=stale_at, valid_until=stale_at + timedelta(days=3))
    decision = decide(
        offer,
        reobservations=(
            observation(started_at=stale_at - timedelta(seconds=2), observed_at=stale_at),
        ),
        validations=validations_for(offer, evaluated_at=stale_at),
    )
    blocked(decision, PublicationOutcome.SUPPRESS, R.STALE_WITHDRAWN)


def test_304_between_72_and_96_hours_is_degraded_not_withdrawn() -> None:
    degraded_at = NOW - timedelta(hours=80)
    offer = reobserved_offer(observed_at=degraded_at, valid_until=degraded_at + timedelta(days=4))
    decision = decide(
        offer,
        reobservations=(
            observation(started_at=degraded_at - timedelta(seconds=2), observed_at=degraded_at),
        ),
        validations=validations_for(offer, evaluated_at=degraded_at),
    )
    assert decision.publishable_now and decision.degraded


def test_a_304_cannot_outlive_the_offers_own_expiry() -> None:
    offer = reobserved_offer(valid_until=NOW)
    blocked(decide(offer), PublicationOutcome.SUPPRESS, R.VALIDITY_EXPIRED)


def test_304_observation_time_must_not_precede_its_start() -> None:
    with pytest.raises(ValidationError):
        observation(started_at=OBSERVED_AT, observed_at=OBSERVED_AT - timedelta(seconds=1))


# --- an otherwise-ineligible offer stays blocked ---------------------------------------


def test_304_does_not_approve_a_first_publication() -> None:
    decision = decide(prior=None)
    blocked(decision, PublicationOutcome.REQUIRES_REVIEW, R.FIRST_PUBLICATION)


def test_304_does_not_override_a_denylist() -> None:
    entry = withdrawal(WithdrawalScope.OFFER, "offer-001")
    blocked(decide(withdrawals=(entry,)), PublicationOutcome.SUPPRESS, R.DENYLISTED)


def test_304_does_not_approve_changed_points() -> None:
    from .factories import points

    changed = reobserved_offer(points=points("1"))
    decision = decide(changed)
    assert not decision.publishable_now
    assert R.POINTS_CHANGED in decision.reasons or R.VALIDATION_UNBOUND in decision.reasons


def test_304_does_not_override_an_unapproved_source() -> None:
    rejected = source_approval(decision="rejected")
    blocked(decide(source_approval=rejected), PublicationOutcome.SUPPRESS, R.SOURCE_NOT_APPROVED)


def test_304_does_not_cure_a_failed_validation() -> None:
    offer = reobserved_offer()
    failing = (validation("rule.rate-range", offer, passed=False, evaluated_at=OBSERVED_AT),)
    blocked(decide(offer, validations=failing), PublicationOutcome.QUARANTINE, R.VALIDATION_FAILED)


def test_304_does_not_approve_an_unreviewed_prior() -> None:
    decision = decide(prior=prior(purchase_offer(), reviewed=False))
    blocked(decision, PublicationOutcome.REQUIRES_REVIEW, R.PRIOR_NOT_REVIEWED)


def test_every_new_quarantine_reason_is_categorized() -> None:
    assert R.REOBSERVATION_UNVERIFIED in QUARANTINE_REASONS


# --- contracts -------------------------------------------------------------------------


def test_observation_is_its_own_run() -> None:
    with pytest.raises(ValidationError, match="its own run"):
        observation(run_id="fetch-001")


def test_only_a_successful_run_records_a_validator() -> None:
    with pytest.raises(ValidationError, match="conditional validator"):
        FetchRun.model_validate(
            fetch_run(status=RunStatus.FAILED, error="timeout", artifact_ids=()).model_dump()
            | {"conditional_validator": ETAG.model_dump()}
        )


def test_reobservation_run_ids_cannot_duplicate() -> None:
    with pytest.raises(ValidationError, match="duplicate fetch run"):
        context(reobservations=(observation(), observation()))


def test_observation_cannot_reuse_a_fetch_run_id() -> None:
    reused = observation(run_id="fetch-001", original_run_id="fetch-000")
    with pytest.raises(ValidationError, match="duplicate fetch run"):
        context(fetch_runs=(fetch_run(),), reobservations=(reused,))

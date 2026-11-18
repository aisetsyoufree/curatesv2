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
from cu_rates.contracts.publication import QUARANTINE_REASONS
from cu_rates.contracts.sources import is_strong_etag, profile_sha256

from .factories import (
    ARTIFACT_ID,
    GIT_SHA,
    SOURCE_ID,
    SV,
    T0,
    context,
    fetch_profile,
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
    return fetch_profile(**overrides)


def original_run(**overrides: Any) -> FetchRun:
    """The original successful 200: ETag validator, under the approved profile's digest."""
    kwargs: dict[str, Any] = {
        "conditional_validator": ETAG,
        "profile_sha256": profile_sha256(profile()),
    }
    kwargs.update(overrides)
    return fetch_run(**kwargs)


def observation(**overrides: Any) -> NotModifiedObservation:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "run_id": "reobs-001",
        "source_id": SOURCE_ID,
        "profile_id": "profile-1",
        "profile_sha256": profile_sha256(profile()),
        "git_sha": GIT_SHA,
        "started_at": OBSERVED_AT - timedelta(seconds=2),
        "observed_at": OBSERVED_AT,
        "original_run_id": "fetch-001",
        "original_artifact_id": ARTIFACT_ID,
        "validator": ETAG,
        "response_etag": ETAG.value,
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
        "fetch_runs": (original_run(),),
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
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert decision.placement is Placement.NONE
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in decision.reasons


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
    obs, run = observation(), original_run()
    assert obs.http_status == 304
    assert obs.run_id != run.run_id
    assert run.artifact_ids == (ARTIFACT_ID,)
    assert not decide().publishable_now
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in decide().reasons


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
        {"fetch_runs": (original_run(conditional_validator=None),)},  # recorded no validator
        {
            "fetch_runs": (
                original_run(conditional_validator=ConditionalValidator(kind="etag", value='"v0"')),
            )
        },
        {"fetch_runs": (original_run(profile_id="profile-2"),)},
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
    assert not decision.publishable_now
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in decision.reasons


def test_a_304_cannot_outlive_the_offers_own_expiry() -> None:
    offer = reobserved_offer(valid_until=NOW)
    blocked(decide(offer), PublicationOutcome.SUPPRESS, R.VALIDITY_EXPIRED)


def test_304_observation_time_must_not_precede_its_start() -> None:
    with pytest.raises(ValidationError):
        observation(started_at=OBSERVED_AT, observed_at=OBSERVED_AT - timedelta(seconds=1))


# --- an otherwise-ineligible offer stays blocked ---------------------------------------


def test_304_does_not_approve_a_first_publication() -> None:
    decision = decide(prior=None)
    blocked(decision, PublicationOutcome.QUARANTINE)  # D-21: no FIRST_PUBLICATION reason


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
    blocked(decision, PublicationOutcome.QUARANTINE)  # D-21: an unreviewed prior is fine


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


# --- the validator must be one strong ETag, identical on the 200 and the 304 -----------------


def _shared(value: str) -> dict[str, Any]:
    validator = ConditionalValidator(kind="etag", value=value)
    return {
        "fetch_runs": (original_run(conditional_validator=validator),),
        "reobservations": (observation(validator=validator, response_etag=value),),
    }


@pytest.mark.parametrize(
    "value",
    ["*", 'W/"v1"', 'w/"v1"', '"a", "b"', "v1", '"v1', '""" ', '"a b"', '"a"b"', ""],
)
def test_only_a_single_strong_etag_refreshes_evidence(value: str) -> None:
    if not value:
        with pytest.raises(ValidationError):
            ConditionalValidator(kind="etag", value=value)
        return
    decision = decide(**_shared(value))
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )


def test_the_same_strong_etag_refreshes_evidence() -> None:
    assert not decide(**_shared('"abc-123_~"')).publishable_now
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in decide(**_shared('"abc-123_~"')).reasons


@pytest.mark.parametrize(
    ("value", "strong"),
    [
        ('"v1"', True),
        ('""', True),
        ('"a/b=c+d"', True),
        ("*", False),
        ('W/"v1"', False),
        ('"a","b"', False),
        ('"a", "b"', False),
        ('"a" ', False),
        ("v1", False),
        ('"a"b"', False),
    ],
)
def test_strong_etag_syntax(value: str, strong: bool) -> None:
    assert is_strong_etag(value) is strong


def test_last_modified_only_never_refreshes_evidence() -> None:
    stamp = ConditionalValidator(kind="last_modified", value="Tue, 01 Sep 2026 00:00:00 GMT")
    decision = decide(
        fetch_runs=(original_run(conditional_validator=stamp),),
        reobservations=(observation(validator=stamp, response_etag=None),),
    )
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )


@pytest.mark.parametrize(
    "response_etag",
    [None, '"v2"', 'W/"v1"', "*", '"v1", "v2"'],
    ids=["missing", "other", "weak", "wildcard", "list"],
)
def test_the_304_must_return_the_original_etag_exactly(response_etag: str | None) -> None:
    decision = decide(reobservations=(observation(response_etag=response_etag),))
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )


def test_the_request_validator_must_equal_the_original_etag() -> None:
    sent = ConditionalValidator(kind="etag", value='"v2"')
    decision = decide(reobservations=(observation(validator=sent, response_etag='"v1"'),))
    blocked(decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED)


def test_a_weak_or_wildcard_validator_leaves_a_normal_200_unchanged() -> None:
    """Validator rules govern only 304 freshness; a fresh 200 publishes as before."""
    fresh = purchase_offer()
    weak = ConditionalValidator(kind="etag", value='W/"v1"')
    plain = evaluate_publication(fresh, context(fresh))
    with_weak = evaluate_publication(
        fresh, context(fresh, fetch_runs=(fetch_run(conditional_validator=weak),))
    )
    assert plain.outcome is PublicationOutcome.AUTO_PUBLISH
    assert with_weak == plain


# --- both observations bind to the actual approved profile, not just its ID ---------------


def test_a_changed_profile_with_the_same_id_fails() -> None:
    changed = profile(timeout_seconds=31)
    assert changed.profile_id == profile().profile_id
    assert profile_sha256(changed) != profile_sha256(profile())
    decision = decide(approved_profile=changed)
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetch_runs": (original_run(profile_sha256=None),)},
        {"fetch_runs": (original_run(profile_sha256=profile_sha256(profile(timeout_seconds=31))),)},
        {
            "reobservations": (
                observation(profile_sha256=profile_sha256(profile(min_interval_seconds=61))),
            )
        },
        {"reobservations": (observation(profile_sha256="d" * 64),)},
    ],
    ids=[
        "original-has-no-digest",
        "original-other-digest",
        "304-other-digest",
        "304-unknown-digest",
    ],
)
def test_both_observations_must_carry_the_approved_profile_digest(
    overrides: dict[str, Any],
) -> None:
    decision = decide(**overrides)
    blocked(
        decision, PublicationOutcome.SUPPRESS, R.REOBSERVATION_UNVERIFIED, R.OBSERVATION_UNBOUND
    )


def test_profile_digest_is_stable_and_covers_every_field() -> None:
    base = profile()
    assert profile_sha256(base) == profile_sha256(profile())
    for change in (
        {"profile_id": "profile-2"},
        {"source_id": "other"},
        {"min_interval_seconds": 61},
        {"timeout_seconds": 31},
    ):
        assert profile_sha256(profile(**change)) != profile_sha256(base)

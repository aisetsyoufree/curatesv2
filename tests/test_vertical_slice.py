"""T014 offline slice. Each regression fails if its stored guard is removed."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import OfferStatus, ProductFamily, ReviewVerdict, WithdrawalScope
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.offers import PurchaseMortgageDetails
from cu_rates.contracts.publication import (
    APR_RULE_ID,
    Placement,
    PublicationOutcome,
    ReasonCode,
    evaluate_publication,
)
from cu_rates.db import open_operational_database
from cu_rates.pipeline.extract import FIXTURES
from cu_rates.pipeline.slice import (
    CHARTER,
    PRODUCT_ID,
    REVIEWER,
    SOURCE_ID,
    claim_text,
    classify_fixture,
    finding_from_non_offer,
    run_fixture,
)
from cu_rates.pipeline.trusted import insert_approval, insert_withdrawal, load_publication_context

R = ReasonCode


def test_athol_slice_is_reproducible_and_links_every_claim(tmp_path: Path) -> None:
    first = run_fixture(tmp_path / "one")
    second = run_fixture(tmp_path / "two")
    assert set(first.offers) == {"purchase-30-year", "purchase-15-year"}
    thirty_offer = first.offers["purchase-30-year"]
    fifteen_offer = first.offers["purchase-15-year"]
    assert thirty_offer.note_rate is not None
    assert isinstance(fifteen_offer.details, PurchaseMortgageDetails)
    assert fifteen_offer.details.term_months == 180
    thirty = first.first["purchase-30-year"]
    fifteen = first.first["purchase-15-year"]
    assert thirty.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert thirty.placement is Placement.RANKED
    assert R.FIRST_PUBLICATION in thirty.reasons
    assert fifteen.outcome is not PublicationOutcome.AUTO_PUBLISH
    assert fifteen.placement is not Placement.RANKED
    assert not fifteen.ranking_eligible
    assert first.refresh is not None
    assert first.reviewed_snapshot is not None
    assert R.REOBSERVATION_UNVERIFIED not in first.refresh.reasons
    assert R.EFFECTIVE_DATE_MISSING in first.refresh.reasons
    assert first.refresh.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert thirty_offer.apr is not None and thirty_offer.points is not None
    for sourced in (thirty_offer.note_rate, thirty_offer.apr, thirty_offer.points):
        assert claim_text(first.store, sourced.provenance) == sourced.provenance.source_text
    second_offer = second.offers["purchase-30-year"]
    assert second_offer.note_rate is not None
    assert claim_text(second.store, second_offer.note_rate.provenance)
    assert thirty_offer.note_rate.value == second_offer.note_rate.value


def test_other_fixtures_produce_no_mortgage_offer() -> None:
    for case_id, name in (
        ("stoughton-style", "stoughton_style_consumer_loans.html"),
        ("worcester-style", "worcester_style_home_lending.html"),
    ):
        body = (FIXTURES / name).read_bytes()
        offers, non_offers = classify_fixture(case_id, body)
        assert all(offer.family is not ProductFamily.MORTGAGE for offer in offers)
        assert non_offers
        for item in non_offers:
            observed = offers[0].observed_at if offers else datetime(2026, 11, 16, tzinfo=UTC)
            finding = finding_from_non_offer(item, observed_at=observed)
            assert finding.finding_id
            assert not hasattr(finding, "note_rate")


def test_superseded_or_rejected_approval_fails_closed(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    engine = open_operational_database(run.database)
    offer_id = run.offers["purchase-30-year"].offer_id
    assert run.reviewed_snapshot is not None
    with engine.begin() as conn:
        view = load_publication_context(conn, offer_id, run.reviewed_snapshot.published_at)
        approval = view.context.source_approval
        assert approval is not None
        rejected = approval.model_copy(
            update={
                "approval_id": "athol-style.approval-rejected",
                "decision": ReviewVerdict.REJECTED,
                "decided_at": approval.decided_at + timedelta(minutes=1),
                "rationale": "Superseded by a rejected fixture decision.",
            }
        )
        insert_approval(conn, rejected, CHARTER)
        blocked = load_publication_context(
            conn, offer_id, rejected.decided_at + timedelta(minutes=1)
        )
    decision = evaluate_publication(blocked.offer, blocked.context)
    assert decision.outcome is not PublicationOutcome.AUTO_PUBLISH
    assert R.SOURCE_NOT_APPROVED in decision.reasons or R.PROFILE_UNAPPROVED in decision.reasons


def test_fresh_200_stale_evidence_and_missing_proof(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    fresh = run.first["purchase-30-year"]
    assert R.FIRST_PUBLICATION in fresh.reasons
    assert fresh.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert run.refresh is not None
    engine = open_operational_database(run.database)
    from cu_rates.pipeline.trusted import insert_offer

    unproven = offer.model_copy(update={"offer_id": "athol-style.no-proof"})
    with engine.begin() as conn:
        stale = load_publication_context(
            conn, offer.offer_id, offer.observed_at + timedelta(hours=97)
        )
        insert_offer(conn, unproven)
        missing = load_publication_context(
            conn, unproven.offer_id, offer.observed_at + timedelta(minutes=1)
        )
    assert R.STALE_WITHDRAWN in evaluate_publication(stale.offer, stale.context).reasons
    assert R.VALIDATIONS_MISSING in evaluate_publication(missing.offer, missing.context).reasons


def test_denylist_suppresses_source_offer_and_product(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    engine = open_operational_database(run.database)
    moment = offer.observed_at + timedelta(minutes=2)
    cases = (
        (WithdrawalScope.SOURCE, SOURCE_ID, "wd-source"),
        (WithdrawalScope.OFFER, offer.offer_id, "wd-offer"),
        (WithdrawalScope.PRODUCT, PRODUCT_ID, "wd-product"),
    )
    for scope, target, withdrawal_id in cases:
        with engine.begin() as conn:
            insert_withdrawal(
                conn,
                WithdrawalEntry(
                    schema_version=CONTRACT_VERSION,
                    withdrawal_id=withdrawal_id,
                    scope=scope,
                    target_id=target,
                    actor=REVIEWER,
                    withdrawn_at=moment,
                    reason="Fixture denylist.",
                ),
            )
            view = load_publication_context(conn, offer.offer_id, moment + timedelta(minutes=1))
        decision = evaluate_publication(view.offer, view.context)
        assert decision.outcome is PublicationOutcome.SUPPRESS
        assert R.DENYLISTED in decision.reasons


def test_weak_etag_changed_rate_and_profile_digest_do_not_refresh(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    assert offer.note_rate is not None
    engine = open_operational_database(run.database)
    from cu_rates.contracts.sources import ConditionalValidator
    from cu_rates.pipeline.trusted import insert_reobservation

    with engine.connect() as conn:
        current = load_publication_context(conn, offer.offer_id, offer.observed_at)
    observation = current.context.reobservations[0]
    weak = observation.model_copy(
        update={
            "run_id": "athol-style.weak-etag",
            "started_at": observation.observed_at + timedelta(minutes=5),
            "observed_at": observation.observed_at + timedelta(minutes=6),
            "validator": ConditionalValidator(kind="etag", value='W/"athol-slice-v1"'),
            "response_etag": 'W/"athol-slice-v1"',
        }
    )
    missing = observation.model_copy(
        update={
            "run_id": "athol-style.missing-etag",
            "started_at": observation.observed_at + timedelta(minutes=7),
            "observed_at": observation.observed_at + timedelta(minutes=8),
            "validator": ConditionalValidator(
                kind="last_modified", value="Mon, 16 Nov 2026 15:00:00 GMT"
            ),
            "response_etag": None,
        }
    )
    with engine.begin() as conn:
        insert_reobservation(conn, weak)
        claimed = load_publication_context(
            conn, offer.offer_id, weak.observed_at + timedelta(minutes=1)
        )
        insert_reobservation(conn, missing)
        later = load_publication_context(
            conn, offer.offer_id, missing.observed_at + timedelta(minutes=1)
        )
        approval = claimed.context.source_approval
        assert approval is not None
        insert_approval(
            conn,
            approval.model_copy(
                update={
                    "approval_id": "athol-style.approval-other-profile",
                    "decided_at": approval.decided_at + timedelta(minutes=1),
                    "approved_profile_sha256": "cd" * 32,
                    "rationale": "A different profile digest supersedes the fixture profile.",
                }
            ),
            CHARTER,
        )
        drifted = load_publication_context(
            conn, offer.offer_id, missing.observed_at + timedelta(minutes=2)
        )
    assert (
        R.REOBSERVATION_UNVERIFIED in evaluate_publication(claimed.offer, claimed.context).reasons
    )
    assert R.REOBSERVATION_UNVERIFIED in evaluate_publication(later.offer, later.context).reasons
    assert R.PROFILE_UNAPPROVED in evaluate_publication(drifted.offer, drifted.context).reasons
    changed = offer.model_copy(
        update={
            "note_rate": offer.note_rate.model_copy(update={"value": offer.note_rate.value + 1})
        }
    )
    assert offer_fingerprints(changed).value != offer_fingerprints(offer).value
    assert run.reviewed_snapshot is not None
    assert run.reviewed_snapshot.entries[0].value_sha256 != offer_fingerprints(changed).value


def test_validation_rules_are_bound_to_fingerprints(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    engine = open_operational_database(run.database)
    from cu_rates.db.schema import validation_results

    with engine.connect() as conn:
        rows = conn.execute(
            select(validation_results).where(validation_results.c.offer_id == offer.offer_id)
        ).all()
    fingerprints = offer_fingerprints(offer).model_dump(mode="json")
    assert {row.rule_id for row in rows} >= {
        APR_RULE_ID,
        "rule.rate-in-percent-domain",
        "rule.points-non-negative",
    }
    assert all(row.fingerprints_json == fingerprints for row in rows)
    assert all(row.passed == 1 for row in rows)
    assert offer.status is OfferStatus.APPROVED

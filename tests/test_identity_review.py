"""AUD7-4: publication requires a persisted, evidence-backed identity and scope review.

``Institution.charter_verified`` is a bare flag. These tests exercise the real persistence,
the trusted loader that re-verifies the cited evidence against stored bytes, and the gate.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cu_rates.contracts import InstitutionIdentityReview, evaluate_publication
from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.contracts.evidence import HtmlLocator
from cu_rates.contracts.publication import ReasonCode as R
from cu_rates.db import open_operational_database
from cu_rates.db.records import load_identity_reviews, persist_identity_review
from cu_rates.db.schema import institution_identity_evidence, institution_identity_reviews
from cu_rates.pipeline.slice import SliceRun, run_fixture
from cu_rates.pipeline.trusted import TrustedView, insert_withdrawal, load_publication_context
from cu_rates.snapshot import Denylist

OFFER = "purchase-30-year"


@pytest.fixture
def run(tmp_path: Path) -> Iterator[SliceRun]:
    yield run_fixture(tmp_path / "run")


def _view(run: SliceRun, *, after: timedelta = timedelta(minutes=4)) -> TrustedView:
    offer = run.offers[OFFER]
    with open_operational_database(run.database).connect() as conn:
        return load_publication_context(
            conn, offer.offer_id, offer.observed_at + after, store=run.store
        )


def _current(run: SliceRun) -> InstitutionIdentityReview:
    offer = run.offers[OFFER]
    with open_operational_database(run.database).connect() as conn:
        (review, *_) = load_identity_reviews(conn, offer.institution_charter)
    return review


def _newer(run: SliceRun, review_id: str, **changes: object) -> InstitutionIdentityReview:
    base = _current(run)
    review = base.model_copy(
        update={"review_id": review_id, "decided_at": base.decided_at + timedelta(minutes=1)}
        | changes
    )
    with open_operational_database(run.database).begin() as conn:
        persist_identity_review(conn, review)
    return review


def test_the_fixture_pipeline_publishes_only_with_a_persisted_review(run: SliceRun) -> None:
    view = _view(run)
    assert view.context.identity_review == _current(run)
    assert view.context.institution.charter_verified is True
    decision = evaluate_publication(view.offer, view.context)
    assert not {
        R.IDENTITY_REVIEW_MISSING,
        R.IDENTITY_NOT_CONFIRMED,
        R.SCOPE_NOT_CONFIRMED,
        R.IDENTITY_REVIEW_STALE,
    } & set(decision.reasons)


def test_a_verified_flag_without_a_loadable_review_is_not_enough(run: SliceRun) -> None:
    """charter_verified stays true, but a review whose evidence cannot be verified is absent."""
    review = _current(run)
    blob = run.store._blob_path(review.evidence[0].artifact_id)
    blob.chmod(0o600)
    blob.write_bytes(b"<html><body><p id='identity'>a different page</p></body></html>")
    view = _view(run)
    assert view.context.institution.charter_verified is True
    assert view.context.identity_review is None
    decision = evaluate_publication(view.offer, view.context)
    assert R.IDENTITY_REVIEW_MISSING in decision.reasons
    assert not decision.publishable_now


def test_a_review_citing_text_the_stored_page_does_not_contain_is_refused(run: SliceRun) -> None:
    review = _current(run)
    lie = review.evidence[0].model_copy(
        update={
            "source_text": "Some Other Credit Union - NCUA charter 1 - Massachusetts",
            "locator": HtmlLocator(
                selector_type="css",
                selector="#identity",
                normalized_text_sha256=sha256(b"something else").hexdigest(),
            ),
        }
    )
    _newer(run, "identity-review-lie", evidence=(lie,))
    view = _view(run, after=timedelta(minutes=5))
    assert view.context.identity_review is None
    assert R.IDENTITY_REVIEW_MISSING in evaluate_publication(view.offer, view.context).reasons


def test_a_newer_rejection_is_never_hidden_behind_an_older_approval(run: SliceRun) -> None:
    _newer(run, "identity-review-rejection", identity_confirmed=False)
    view = _view(run, after=timedelta(minutes=5))
    assert view.context.identity_review is not None
    assert view.context.identity_review.review_id == "identity-review-rejection"
    decision = evaluate_publication(view.offer, view.context)
    assert R.IDENTITY_NOT_CONFIRMED in decision.reasons
    assert not decision.publishable_now


def test_out_of_scope_institutions_are_not_published(run: SliceRun) -> None:
    _newer(run, "identity-review-out-of-state", massachusetts_scope_confirmed=False)
    view = _view(run, after=timedelta(minutes=5))
    decision = evaluate_publication(view.offer, view.context)
    assert R.SCOPE_NOT_CONFIRMED in decision.reasons
    assert not decision.publishable_now


def test_a_review_decided_after_the_evaluation_time_is_not_in_force(run: SliceRun) -> None:
    base = _current(run)
    _newer(run, "identity-review-future", decided_at=base.decided_at + timedelta(days=3))
    view = _view(run)  # evaluated minutes after the offer, long before the new decision
    assert view.context.identity_review is not None
    assert view.context.identity_review.review_id == base.review_id


def test_a_stale_review_blocks_publication(run: SliceRun) -> None:
    view = _view(run, after=timedelta(days=500))
    decision = evaluate_publication(view.offer, view.context)
    assert R.IDENTITY_REVIEW_STALE in decision.reasons


def test_the_database_refuses_reviews_of_nothing_or_backed_by_nothing(run: SliceRun) -> None:
    base = _current(run)
    with (
        pytest.raises((IntegrityError, ValueError)),
        open_operational_database(run.database).begin() as conn,
    ):
        persist_identity_review(
            conn, base.model_copy(update={"review_id": "r-ghost", "institution_charter": 999})
        )
    ghost = base.evidence[0].model_copy(update={"artifact_id": "ab" * 32})
    with pytest.raises(IntegrityError), open_operational_database(run.database).begin() as conn:
        persist_identity_review(
            conn, base.model_copy(update={"review_id": "r-no-bytes", "evidence": (ghost,)})
        )
    with (
        pytest.raises(ValueError, match="same evidence twice"),
        open_operational_database(run.database).begin() as conn,
    ):
        persist_identity_review(
            conn,
            base.model_copy(update={"review_id": "r-twice", "evidence": (base.evidence[0],) * 2}),
        )


def test_identity_reviews_are_append_only(run: SliceRun) -> None:
    with closing(sqlite3.connect(run.database)) as raw:
        for statement in (
            "UPDATE institution_identity_reviews SET reviewer = 'x'",
            "DELETE FROM institution_identity_reviews",
            "UPDATE institution_identity_evidence SET ordinal = 5",
            "DELETE FROM institution_identity_evidence",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
                raw.execute(statement)
        # REPLACE cannot rewrite history either.
        row = raw.execute("SELECT * FROM institution_identity_reviews").fetchone()
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            raw.execute(
                "INSERT OR REPLACE INTO institution_identity_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                (row[0], row[1], row[2], "Forged Name", 1, 1, row[6], row[7], row[8]),
            )
    with open_operational_database(run.database).connect() as conn:
        assert len(conn.execute(select(institution_identity_reviews)).all()) == 1
        assert len(conn.execute(select(institution_identity_evidence)).all()) == 1


def test_a_rejection_decided_at_the_same_moment_is_never_hidden_by_an_approval(
    run: SliceRun,
) -> None:
    """Review HIGH: ties on decided_at must fail closed whatever the review ids sort as."""
    base = _current(run)
    for review_id in ("a-first", "zzz-last"):
        # Same instant: the id order must not decide whether the rejection is seen.
        review = base.model_copy(
            update={"review_id": f"tie-{review_id}", "identity_confirmed": False}
        )
        with open_operational_database(run.database).begin() as conn:
            persist_identity_review(conn, review)
        view = _view(run)
        assert view.context.identity_review is not None
        assert not view.context.identity_review.approves
        decision = evaluate_publication(view.offer, view.context)
        assert R.IDENTITY_NOT_CONFIRMED in decision.reasons
        assert not decision.publishable_now


def test_evidence_that_does_not_name_the_institution_does_not_prove_it(run: SliceRun) -> None:
    """Review HIGH: a verified-but-unrelated stored page (a rate page) is not identity proof."""
    base = _current(run)
    offer_evidence = next(iter(run.offers[OFFER].iter_provenance()))
    _newer(run, "identity-review-rate-page", evidence=(offer_evidence,))
    view = _view(run, after=timedelta(minutes=5))
    review = view.context.identity_review
    assert review is not None and review.review_id == "identity-review-rate-page"
    assert not review.evidence_states_identity
    decision = evaluate_publication(view.offer, view.context)
    assert R.IDENTITY_NOT_CONFIRMED in decision.reasons
    assert not decision.publishable_now
    assert base.evidence_states_identity and base.evidence_states_massachusetts


def test_a_review_cannot_cite_another_institutions_evidence(run: SliceRun) -> None:
    """Review HIGH: provenance recorded for charter A cannot back a review of charter B."""
    base = _current(run)
    with open_operational_database(run.database).begin() as conn:
        persist_identity_review(conn, base.model_copy(update={"review_id": "same-owner"}))
    # Same stored provenance cited for a different institution is refused by the writer...
    with (
        pytest.raises(ValueError, match="only cite evidence recorded for its institution"),
        open_operational_database(run.database).begin() as conn,
    ):
        persist_identity_review(
            conn,
            base.model_copy(update={"review_id": "stolen", "institution_charter": 777}),
        )
    # ...and by the database itself, so a raw writer cannot bypass the check.
    with closing(sqlite3.connect(run.database)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        provenance_id = raw.execute(
            "SELECT provenance_id FROM institution_identity_evidence"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError), raw:
            raw.execute(
                "INSERT INTO institution_identity_evidence VALUES ('same-owner', ?, 777, 5)",
                (provenance_id,),
            )


def test_a_review_older_than_a_roster_clearing_cannot_vouch_for_a_reflagged_charter(
    run: SliceRun, tmp_path: Path
) -> None:
    """Review MEDIUM: when the roster importer clears verification, old reviews are void."""
    base = _current(run)
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()
    entry = WithdrawalEntry(
        schema_version=base.schema_version,
        withdrawal_id="roster-test.suppress.clearing",
        scope=WithdrawalScope.INSTITUTION,
        target_id=str(base.institution_charter),
        actor="roster-importer",
        withdrawn_at=base.decided_at + timedelta(minutes=2),
        reason="reported merged in a roster import",
    )
    with open_operational_database(run.database).begin() as conn:
        insert_withdrawal(conn, entry, denylist=denylist)
    view = _view(run, after=timedelta(minutes=5))
    assert view.context.identity_review is None  # the old approval no longer counts
    assert R.IDENTITY_REVIEW_MISSING in evaluate_publication(view.offer, view.context).reasons
    # A human re-review AFTER the clearing restores it.
    _newer(
        run, "identity-review-after-clearing", decided_at=entry.withdrawn_at + timedelta(minutes=1)
    )
    view = _view(run, after=timedelta(minutes=6))
    assert view.context.identity_review is not None
    assert view.context.identity_review.review_id == "identity-review-after-clearing"

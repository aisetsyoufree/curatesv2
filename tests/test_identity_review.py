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
from cu_rates.contracts.evidence import HtmlLocator
from cu_rates.contracts.publication import ReasonCode as R
from cu_rates.db import open_operational_database
from cu_rates.db.records import load_identity_reviews, persist_identity_review
from cu_rates.db.schema import institution_identity_evidence, institution_identity_reviews
from cu_rates.pipeline.slice import SliceRun, run_fixture
from cu_rates.pipeline.trusted import TrustedView, load_publication_context

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
    with pytest.raises(IntegrityError), open_operational_database(run.database).begin() as conn:
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

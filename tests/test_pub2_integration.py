"""Publication uses stored capture history, never an inferred second observation."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from cu_rates.contracts.publication import ReasonCode as R
from cu_rates.contracts.publication import (
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.db import open_operational_database
from cu_rates.db.schema import slice_snapshot_baselines
from cu_rates.pipeline.slice import run_fixture
from cu_rates.pipeline.trusted import load_publication_context, publish
from cu_rates.snapshot import SnapshotError
from cu_rates.snapshot.builder import SnapshotCandidate, SnapshotPublisher, _public_projection

from .pub2_fixtures import reviewed_fixture
from .test_public_snapshot import StoredFixture, release
from .test_public_snapshot import persisted_seed as persisted_seed
from .test_public_snapshot import stored as stored


def test_one_real_capture_alone_never_publishes(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    assert run.reviewed_snapshot is None and run.refreshed_snapshot is None
    offer = run.offers["purchase-30-year"]
    now = offer.observed_at + timedelta(minutes=4)
    with open_operational_database(run.database).begin() as conn:
        view = load_publication_context(conn, offer.offer_id, now, store=run.store)
        assert len(view.context.known_artifacts) == 1
        for field in ("note_rate", "apr", "points"):
            paths = [p for p in view.context.evidence_paths if p.path == field]
            assert len(paths) == 1
            p = paths[0]
            artifact = view.context.known_artifacts[0]
            assert (p.artifact_id, p.content_sha256, p.capture_id, p.observed_at) == (
                artifact.artifact_id,
                artifact.artifact_id,
                artifact.fetch_run_id,
                artifact.fetched_at,
            )
            assert p.provenance in tuple(view.offer.iter_provenance())
        decision = evaluate_publication(view.offer, view.context)
        assert R.INSUFFICIENT_EVIDENCE_PATHS in decision.reasons
        assert not decision.publishable_now
        assert (
            publish(
                conn,
                offer.offer_id,
                store=run.store,
                evaluated_at=now,
                snapshot_id="must-not-exist",
                reviewed=True,
            )
            is None
        )


def test_two_stored_captures_export_and_audit_both_chains(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).connect() as conn:
        assert run.reviewed_snapshot is not None
        view = load_publication_context(
            conn, offer.offer_id, run.reviewed_snapshot.published_at, store=run.store
        )
        captures = {a.artifact_id: a for a in view.context.known_artifacts}
        assert len(captures) == 2
        for p in view.context.evidence_paths:
            artifact = captures[p.artifact_id]
            assert run.store.get_observation(p.capture_id, view.context.source.url) == artifact
            assert p.content_sha256 == artifact.artifact_id
            assert p.observed_at == artifact.fetched_at
        published = _public_projection(
            SnapshotCandidate(offer=view.offer, context=view.context, candidates=()),
            evaluate_publication(view.offer, view.context).placement,
            True,
        )
        assert {p.artifact_id for p in published.evidence if p.path == "note_rate"} == set(captures)
        exported = published.details["publication_evidence"]
        assert isinstance(exported, list) and len(exported) == 6
        for claim in exported:
            assert isinstance(claim, dict)
            artifact_id = claim["artifact_id"]
            assert isinstance(artifact_id, str)
            artifact = captures[artifact_id]
            assert claim["capture_id"] == artifact.fetch_run_id
            assert isinstance(claim["observed_at"], str)
            assert datetime.fromisoformat(claim["observed_at"]) == artifact.fetched_at
            assert claim["content_sha256"] == artifact_id
            assert claim["locator"]
            assert "source_text" not in claim
        audit = (
            conn.execute(
                select(slice_snapshot_baselines).where(
                    slice_snapshot_baselines.c.snapshot_id == run.reviewed_snapshot.snapshot_id
                )
            )
            .one()
            .baseline_json
        )
        assert audit["publication_evidence"] == [
            p.model_dump(mode="json") for p in corroborated_paths(view.offer, view.context)
        ]
        assert audit["publication_evidence_sha256"] == publication_evidence_sha256(
            view.offer, corroborated_paths(view.offer, view.context)
        )
        assert view.current_review is not None
        assert view.current_review.reviewed_evidence_sha256 == audit["publication_evidence_sha256"]


def test_review_of_one_capture_cannot_approve_the_pair(
    tmp_path: Path, stored: StoredFixture
) -> None:
    from cu_rates.contracts.fingerprints import offer_evidence_sha256

    item = stored.row("purchase-15-year")
    stored.approve(item.offer.offer_id, evidence_hash=offer_evidence_sha256(item.offer))
    item = stored.row("purchase-15-year")
    publisher = SnapshotPublisher(tmp_path / "legacy-review")
    with pytest.raises(SnapshotError, match="publication gate refused"):
        publisher.build(release("legacy-review", item), conn=stored.conn, store=stored.run.store)


def test_repeated_bytes_in_two_runs_do_not_make_independent_evidence(tmp_path: Path) -> None:
    from .test_vertical_slice import _dated_body, _later_200

    run = run_fixture(tmp_path)
    _, artifact = _later_200(run, _dated_body())
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).connect() as conn:
        view = load_publication_context(
            conn, offer.offer_id, artifact.fetched_at + timedelta(minutes=1), store=run.store
        )
    decision = evaluate_publication(view.offer, view.context)
    assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
    assert not decision.publishable_now


def test_unverified_current_capture_cannot_borrow_verified_history(tmp_path: Path) -> None:
    from cu_rates.contracts.enums import OfferStatus
    from cu_rates.pipeline.trusted import insert_artifact, insert_capture, insert_run

    run = reviewed_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).begin() as conn:
        view = load_publication_context(
            conn, offer.offer_id, offer.observed_at + timedelta(minutes=4), store=run.store
        )
        primary = view.context.known_artifacts[0]
        original_run = next(r for r in view.context.fetch_runs if r.run_id == primary.fetch_run_id)
        # A real capture can contain a bad extraction. Neither an old pair nor an
        # old review proves this proposed claim's locator.
        artifact = run.store.put(
            run.store.read(primary),
            source_url=primary.source_url,
            media_type=primary.media_type,
            fetched_at=primary.fetched_at,
            http_status=200,
            fetch_run_id="unverified-extraction",
        )
        fetch = original_run.model_copy(
            update={
                "run_id": artifact.fetch_run_id,
                "finished_at": original_run.finished_at + timedelta(seconds=1),
            }
        )
        assert offer.note_rate is not None and offer.apr is not None
        forged = offer.model_copy(
            update={"apr": offer.apr.model_copy(update={"provenance": offer.note_rate.provenance})}
        )
        insert_run(conn, fetch)
        insert_artifact(conn, artifact)
        insert_capture(conn, forged, fetch.run_id)
        result = load_publication_context(
            conn, offer.offer_id, offer.observed_at + timedelta(minutes=4), store=run.store
        )
    assert result.offer.status is OfferStatus.QUARANTINED
    assert not evaluate_publication(result.offer, result.context).publishable_now

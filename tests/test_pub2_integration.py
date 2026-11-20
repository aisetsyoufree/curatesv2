"""Publication uses stored capture history, never an inferred second observation."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.artifact_store import ArtifactNotFoundError, ArtifactStore
from cu_rates.contracts.audit import Correction, PublishedSnapshot
from cu_rates.contracts.enums import OfferStatus
from cu_rates.contracts.evidence import RawArtifact
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.publication import ReasonCode as R
from cu_rates.contracts.publication import (
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.db import open_operational_database
from cu_rates.db.schema import slice_snapshot_baselines
from cu_rates.fetch_policy import FetchRegistry, TermsLedger
from cu_rates.pipeline.slice import _Clock, _worker, record_fixture_capture, run_fixture
from cu_rates.pipeline.trusted import insert_reobservation, load_publication_context, publish
from cu_rates.snapshot import Denylist, SnapshotError, SnapshotReader, builder
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
        view = load_publication_context(
            conn, offer.offer_id, now, store=run.store, allow_fixture=True
        )
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
                allow_fixture=True,
            )
            is None
        )


def test_two_stored_captures_export_and_audit_both_chains(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).connect() as conn:
        assert run.reviewed_snapshot is not None
        view = load_publication_context(
            conn,
            offer.offer_id,
            run.reviewed_snapshot.published_at,
            store=run.store,
            allow_fixture=True,
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


def test_a_stale_single_capture_review_is_not_attached_to_an_automatic_publication(
    tmp_path: Path, stored: StoredFixture
) -> None:
    from cu_rates.contracts.fingerprints import offer_evidence_sha256

    item = stored.row("purchase-15-year")
    stored.approve(item.offer.offer_id, evidence_hash=offer_evidence_sha256(item.offer))
    item = stored.row("purchase-15-year")
    publisher = SnapshotPublisher(tmp_path / "legacy-review", allow_fixture=True)
    # D-21: the stale single-capture review is simply not attached; the offer, which has two
    # corroborating captures, publishes automatically and is never labelled human-reviewed.
    info = publisher.build(release("legacy-review", item), conn=stored.conn, store=stored.run.store)
    from cu_rates.snapshot import Denylist, SnapshotReader

    denylist = Denylist(tmp_path / "ind" / "d.json")
    denylist.initialize()
    view = SnapshotReader(publisher.root, denylist.path).read(at=info.published_at)
    assert {o.review_status for o in view.offers} == {"automatic"}


def test_repeated_bytes_one_hour_apart_are_not_independent_evidence(tmp_path: Path) -> None:
    from .test_vertical_slice import _dated_body, _later_200

    run = run_fixture(tmp_path)
    _, artifact = _later_200(run, _dated_body())
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).connect() as conn:
        view = load_publication_context(
            conn,
            offer.offer_id,
            artifact.fetched_at + timedelta(minutes=1),
            store=run.store,
            allow_fixture=True,
        )
    decision = evaluate_publication(view.offer, view.context)
    assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
    assert not decision.publishable_now


@pytest.fixture
def repeated_capture(
    tmp_path: Path,
) -> Iterator[tuple[StoredFixture, SnapshotCandidate]]:
    worker_root = tmp_path / "worker"
    run = run_fixture(worker_root)
    offer = run.offers["purchase-30-year"]
    engine = open_operational_database(run.database)
    with engine.begin() as conn:
        first = load_publication_context(
            conn,
            offer.offer_id,
            offer.observed_at + timedelta(minutes=1),
            store=run.store,
            allow_fixture=True,
        )
        original = first.context.known_artifacts[0]
        profile = first.context.approved_profile
        assert profile is not None
    # A cold worker issues a separate unconditional request through the offline
    # transport. Do not create a second FetchRun by cloning the original record.
    now = original.fetched_at + timedelta(hours=3)
    registry = FetchRegistry(
        run.database,
        TermsLedger(worker_root / "private" / "terms.json"),
        Denylist(worker_root / "independent" / "withdrawals.json"),
        automatic=None,
    )
    cold_root = tmp_path / "cold-worker"
    cold_root.mkdir()
    worker = _worker(cold_root, run.store, _Clock(now), run.store.read(original), registry)
    direct = worker.fetch(first.context.source, profile)
    assert direct.artifact is not None and direct.run is not None
    assert direct.artifact.artifact_id == original.artifact_id
    assert direct.run.run_id != original.fetch_run_id
    with engine.begin() as conn:
        record_fixture_capture(conn, "athol-style-dated", run.store, direct.run, direct.artifact)
        stored = StoredFixture(run, conn)
        yield stored, stored.row(now=direct.run.finished_at + timedelta(minutes=1))
    engine.dispose()


def test_identical_bodies_publish_with_two_independent_captures(
    tmp_path: Path, repeated_capture: tuple[StoredFixture, SnapshotCandidate]
) -> None:
    stored, row = repeated_capture
    offer, ctx = row.offer, row.context
    assert len(ctx.known_artifacts) == 2
    newest, oldest = ctx.known_artifacts
    assert newest.artifact_id == oldest.artifact_id
    assert newest.fetch_run_id == ctx.primary_capture_id
    assert newest.fetched_at - oldest.fetched_at >= timedelta(hours=3)
    assert offer.observed_at == newest.fetched_at
    assert ctx.artifact_for(newest.artifact_id) == newest
    paths = corroborated_paths(offer, ctx)
    assert len(paths) == 6
    assert {p.capture_id for p in paths} == {newest.fetch_run_id, oldest.fetch_run_id}
    assert evaluate_publication(offer, ctx).ranking_eligible

    public_root = tmp_path / "public"
    publisher = SnapshotPublisher(public_root, allow_fixture=True)
    publisher.build(release("identical-bodies", row), conn=stored.conn, store=stored.run.store)
    denylist = Denylist(tmp_path / "public-denylist" / "withdrawals.json")
    denylist.initialize()
    (visible,) = SnapshotReader(public_root, denylist.path).read(at=ctx.evaluated_at).offers
    assert {p.capture_id for p in visible.require_protocol2().publication_evidence} == {
        newest.fetch_run_id,
        oldest.fetch_run_id,
    }


def test_shared_blob_cannot_replace_a_missing_older_capture_record(
    tmp_path: Path,
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, row = repeated_capture
    primary, earlier = row.context.known_artifacts
    assert primary.artifact_id == earlier.artifact_id
    load = builder._load_candidate

    def load_then_remove(
        conn: Connection,
        store: ArtifactStore,
        offer_id: str,
        manifest: PublishedSnapshot,
        *,
        allow_fixture: bool,
    ) -> SnapshotCandidate:
        candidate = load(conn, store, offer_id, manifest, allow_fixture=allow_fixture)
        paths = corroborated_paths(candidate.offer, candidate.context)
        assert {path.capture_id for path in paths} == {earlier.fetch_run_id, primary.fetch_run_id}
        store._observation_path(earlier.fetch_run_id, earlier.source_url).unlink()
        assert store.get_observation(primary.fetch_run_id, primary.source_url) == primary
        assert store.read(primary)
        return candidate

    monkeypatch.setattr(builder, "_load_candidate", load_then_remove)
    publisher = SnapshotPublisher(tmp_path / "public", allow_fixture=True)
    with pytest.raises(SnapshotError, match="could not verify persisted evidence"):
        publisher.build(release("bad-pair", row), conn=stored.conn, store=stored.run.store)
    assert not (publisher.root / "current.json").exists()


@pytest.mark.parametrize("removed", ["blob", "observation"])
def test_primary_must_survive_admission_when_corroboration_uses_older_pair(
    tmp_path: Path,
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
    monkeypatch: pytest.MonkeyPatch,
    removed: str,
) -> None:
    stored, row = repeated_capture
    previous, oldest = row.context.known_artifacts
    profile = row.context.approved_profile
    assert profile is not None
    worker_root = stored.run.database.parent
    registry = FetchRegistry(
        stored.run.database,
        TermsLedger(worker_root / "private" / "terms.json"),
        Denylist(worker_root / "independent" / "withdrawals.json"),
        automatic=None,
    )
    cold_root = tmp_path / "third-worker"
    cold_root.mkdir()
    store = stored.run.store
    worker = _worker(
        cold_root,
        store,
        _Clock(previous.fetched_at + timedelta(hours=3)),
        store.read(previous) + b"\n",
        registry,
    )
    direct = worker.fetch(row.context.source, profile)
    assert direct.run is not None and direct.artifact is not None
    primary = direct.artifact
    assert primary.artifact_id != previous.artifact_id
    record_fixture_capture(stored.conn, "athol-style-dated", store, direct.run, primary)
    row = stored.row(now=direct.run.finished_at + timedelta(minutes=1))
    assert row.context.primary_capture_id == primary.fetch_run_id
    assert len(row.context.known_artifacts) == 3
    pair = {path.capture_id for path in corroborated_paths(row.offer, row.context)}
    assert pair == {previous.fetch_run_id, oldest.fetch_run_id}

    publisher = SnapshotPublisher(tmp_path / "public", allow_fixture=True)
    publisher.build(release("intact-primary", row), conn=stored.conn, store=store)
    load = builder._load_candidate

    def load_then_remove_primary(
        conn: Connection,
        store: ArtifactStore,
        offer_id: str,
        manifest: PublishedSnapshot,
        *,
        allow_fixture: bool,
    ) -> SnapshotCandidate:
        candidate = load(conn, store, offer_id, manifest, allow_fixture=allow_fixture)
        assert candidate.context.primary_capture_id == primary.fetch_run_id
        if removed == "blob":
            store._blob_path(primary.artifact_id).unlink()
        else:
            store._observation_path(primary.fetch_run_id, primary.source_url).unlink()
        for earlier in (previous, oldest):
            assert store.get_observation(earlier.fetch_run_id, earlier.source_url) == earlier
            assert store.read(earlier)
        return candidate

    monkeypatch.setattr(builder, "_load_candidate", load_then_remove_primary)
    with pytest.raises(SnapshotError, match="could not verify persisted evidence"):
        publisher.build(
            release("missing-primary", row, supersedes="intact-primary"),
            conn=stored.conn,
            store=store,
        )
    assert not (publisher.root / "versions" / "missing-primary").exists()


def test_content_lookup_cannot_guess_or_swap_primary_capture(
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
) -> None:
    _, row = repeated_capture
    offer, ctx = row.offer, row.context
    latest, earlier = ctx.known_artifacts
    assert evaluate_publication(
        offer, ctx.model_copy(update={"known_artifacts": (earlier, latest)})
    ).ranking_eligible
    for run_id, reason in (
        (None, R.ARTIFACT_NOT_STORED),
        (earlier.fetch_run_id, R.OBSERVATION_UNBOUND),
    ):
        decision = evaluate_publication(
            offer, ctx.model_copy(update={"primary_capture_id": run_id})
        )
        assert reason in decision.reasons
        assert not decision.publishable_now
    with pytest.raises(ValidationError, match="duplicate known artifact"):
        type(ctx).model_validate({**dict(ctx), "known_artifacts": (latest, latest)})


def test_unverifiable_basis_cannot_fall_back_to_an_older_identical_capture(
    repeated_capture: tuple[StoredFixture, SnapshotCandidate], monkeypatch: pytest.MonkeyPatch
) -> None:
    stored, row = repeated_capture
    latest, earlier = row.context.known_artifacts
    get_observation = stored.run.store.get_observation

    def without_latest(fetch_run_id: str, source_url: str) -> RawArtifact:
        if fetch_run_id == latest.fetch_run_id:
            raise ArtifactNotFoundError("latest observation record is missing")
        return get_observation(fetch_run_id, source_url)

    monkeypatch.setattr(stored.run.store, "get_observation", without_latest)
    view = load_publication_context(
        stored.conn,
        row.offer.offer_id,
        row.context.evaluated_at,
        store=stored.run.store,
        allow_fixture=True,
    )
    assert view.offer.status is OfferStatus.QUARANTINED
    assert view.context.primary_capture_id == latest.fetch_run_id
    assert view.context.known_artifacts == (earlier,)
    assert view.context.artifact_for(latest.artifact_id) is None
    assert not view.context.source_fingerprints
    assert not evaluate_publication(view.offer, view.context).publishable_now


def test_identical_body_cannot_borrow_a_foreign_capture(
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
) -> None:
    _, row = repeated_capture
    offer, ctx = row.offer, row.context
    latest, earlier = ctx.known_artifacts
    for artifacts, runs in (
        (
            (latest, earlier.model_copy(update={"source_url": "https://other.example/rates"})),
            ctx.fetch_runs,
        ),
        (
            (latest, earlier),
            tuple(
                run.model_copy(update={"source_id": "other-source"})
                if run.run_id == earlier.fetch_run_id
                else run
                for run in ctx.fetch_runs
            ),
        ),
    ):
        decision = evaluate_publication(
            offer, ctx.model_copy(update={"known_artifacts": artifacts, "fetch_runs": runs})
        )
        assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in decision.reasons
        assert not decision.publishable_now


def test_identical_body_cannot_borrow_older_layout(
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
) -> None:
    _, row = repeated_capture
    offer, ctx = row.offer, row.context
    _, earlier = ctx.known_artifacts
    (fingerprint,) = ctx.source_fingerprints
    old_layout = fingerprint.model_copy(
        update={"fetch_run_id": earlier.fetch_run_id, "observed_at": earlier.fetched_at}
    )
    decision = evaluate_publication(
        offer, ctx.model_copy(update={"source_fingerprints": (old_layout,)})
    )
    assert R.LAYOUT_UNREVIEWED in decision.reasons
    assert not decision.publishable_now


def test_correction_uses_primary_capture_even_with_an_identical_older_blob(
    repeated_capture: tuple[StoredFixture, SnapshotCandidate],
) -> None:
    _, row = repeated_capture
    offer, ctx = row.offer, row.context
    assert offer.note_rate is not None
    _, earlier = ctx.known_artifacts
    correction = Correction(
        schema_version="1.0",
        correction_id="fixture-correction",
        offer_id=offer.offer_id,
        field_path="note_rate",
        previous_value="6.000",
        corrected_value=format(offer.note_rate.value, "f"),
        actor="fixture-reviewer",
        corrected_at=earlier.fetched_at + timedelta(minutes=1),
        reason="Require direct evidence after the correction.",
        evidence=(offer.note_rate.provenance,),
        corrected_fingerprints=offer_fingerprints(offer),
    )
    decision = evaluate_publication(offer, ctx.model_copy(update={"corrections": (correction,)}))
    assert R.CORRECTION_PENDING not in decision.reasons
    assert decision.publishable_now


def test_independent_worker_304_cannot_be_second_body(tmp_path: Path) -> None:
    run = run_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    now = offer.observed_at + timedelta(hours=4)
    engine = open_operational_database(run.database)
    with engine.connect() as conn:
        first = load_publication_context(
            conn, offer.offer_id, now, store=run.store, allow_fixture=True
        )
    artifact = first.context.known_artifacts[0]
    profile = first.context.approved_profile
    assert profile is not None
    registry = FetchRegistry(
        run.database,
        TermsLedger(tmp_path / "private" / "terms.json"),
        Denylist(tmp_path / "independent" / "withdrawals.json"),
        automatic=None,
    )
    # Reopen the original worker state: the same strong validator gives a real mock 304.
    worker = _worker(tmp_path, run.store, _Clock(now), run.store.read(artifact), registry)
    result = worker.fetch(first.context.source, profile)
    assert result.reobserved is not None and result.artifact is None
    with engine.begin() as conn:
        observation = result.reobserved.record()
        insert_reobservation(conn, observation)
        view = load_publication_context(
            conn, offer.offer_id, observation.observed_at, store=run.store, allow_fixture=True
        )
    assert view.context.reobservations == (observation,)
    assert len(view.context.known_artifacts) == 1
    assert {p.capture_id for p in view.context.evidence_paths} == {artifact.fetch_run_id}
    assert R.INSUFFICIENT_EVIDENCE_PATHS in evaluate_publication(view.offer, view.context).reasons
    engine.dispose()


def test_unverified_current_capture_cannot_borrow_verified_history(tmp_path: Path) -> None:
    from cu_rates.pipeline.trusted import insert_artifact, insert_capture, insert_run

    run = reviewed_fixture(tmp_path)
    offer = run.offers["purchase-30-year"]
    with open_operational_database(run.database).begin() as conn:
        view = load_publication_context(
            conn,
            offer.offer_id,
            offer.observed_at + timedelta(minutes=4),
            store=run.store,
            allow_fixture=True,
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
            conn,
            offer.offer_id,
            offer.observed_at + timedelta(minutes=4),
            store=run.store,
            allow_fixture=True,
        )
    assert result.offer.status is OfferStatus.QUARANTINED
    assert not evaluate_publication(result.offer, result.context).publishable_now


def test_automatic_publication_records_automatic_publisher(tmp_path: Path) -> None:
    from cu_rates.pipeline.trusted import AUTOMATIC_PUBLISHER

    run = reviewed_fixture(tmp_path)
    assert run.refreshed_snapshot is not None
    assert run.refreshed_snapshot.published_by == AUTOMATIC_PUBLISHER
    assert run.reviewed_snapshot is not None
    assert run.reviewed_snapshot.published_by != AUTOMATIC_PUBLISHER

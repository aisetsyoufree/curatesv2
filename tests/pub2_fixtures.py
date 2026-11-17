"""Two actual private fixture captures; no production acquisition or live data."""

from datetime import timedelta
from pathlib import Path

from cu_rates.contracts.audit import ReviewDecision
from cu_rates.contracts.enums import ReviewSubject, ReviewVerdict
from cu_rates.contracts.publication import (
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.contracts.sources import NotModifiedObservation
from cu_rates.db import open_operational_database
from cu_rates.pipeline.slice import CHARTER, SliceRun, record_fixture_capture, run_fixture
from cu_rates.pipeline.trusted import (
    insert_reobservation,
    insert_review,
    insert_validations,
    load_publication_context,
    publish,
)
from cu_rates.pipeline.validate import validate_offer


def reviewed_fixture(root: Path) -> SliceRun:
    run = run_fixture(root)
    chosen = run.offers["purchase-30-year"]
    engine = open_operational_database(run.database)
    now = chosen.observed_at + timedelta(minutes=1, seconds=1)
    with engine.begin() as conn:
        first = load_publication_context(conn, chosen.offer_id, now, store=run.store)
        original = first.context.known_artifacts[0]
        original_run = next(
            r for r in first.context.fetch_runs if r.run_id == original.fetch_run_id
        )
        earlier = original.fetched_at - timedelta(minutes=1)
        # Authored whitespace variation gives a second actual stored body; the parser
        # checks the same financial facts independently in both captured documents.
        artifact = run.store.put(
            run.store.read(original) + b"\n",
            source_url=original.source_url,
            media_type=original.media_type,
            fetched_at=earlier,
            http_status=200,
            fetch_run_id="fixture-earlier-capture",
        )
        fetch = original_run.model_copy(
            update={
                "run_id": artifact.fetch_run_id,
                "artifact_ids": (artifact.artifact_id,),
                "started_at": earlier,
                "finished_at": earlier,
            }
        )
        record_fixture_capture(conn, "athol-style-dated", run.store, fetch, artifact)
        view = load_publication_context(conn, chosen.offer_id, now, store=run.store)
        pair = corroborated_paths(view.offer, view.context)
        assert len(pair) == 6
        review = ReviewDecision(
            schema_version="1.0",
            review_id="fixture-dual-review",
            subject=ReviewSubject.OFFER,
            subject_id=chosen.offer_id,
            verdict=ReviewVerdict.APPROVED,
            actor="fixture-human-reviewer",
            decided_at=now - timedelta(microseconds=1),
            rationale="Review both actual fixture captures.",
            evidence=tuple(dict.fromkeys(p.provenance for p in pair)),
            reviewed_fingerprints=view.context.validations[0].fingerprints,
            reviewed_evidence_sha256=publication_evidence_sha256(view.offer, pair),
        )
        insert_review(conn, review, CHARTER)
        run.first = {
            key: evaluate_publication(
                (v := load_publication_context(conn, offer.offer_id, now, store=run.store)).offer,
                v.context,
            )
            for key, offer in run.offers.items()
        }
        run.reviewed_snapshot = publish(
            conn,
            chosen.offer_id,
            store=run.store,
            evaluated_at=now,
            snapshot_id="fixture-reviewed",
            reviewed=True,
        )
        assert run.reviewed_snapshot is not None
        moment = original.fetched_at + timedelta(minutes=2)
        assert (
            original_run.profile_sha256 is not None
            and original_run.conditional_validator is not None
        )
        observation = NotModifiedObservation(
            schema_version="1.0",
            run_id="fixture-304",
            source_id=original_run.source_id,
            profile_id=original_run.profile_id,
            profile_sha256=original_run.profile_sha256,
            git_sha=original_run.git_sha,
            started_at=moment,
            observed_at=moment,
            original_run_id=original_run.run_id,
            original_artifact_id=original.artifact_id,
            validator=original_run.conditional_validator,
            response_etag=original_run.conditional_validator.value,
        )
        insert_reobservation(conn, observation)
        refreshed = load_publication_context(conn, chosen.offer_id, moment, store=run.store)
        insert_validations(conn, validate_offer(refreshed.offer, moment))
        refreshed = load_publication_context(conn, chosen.offer_id, moment, store=run.store)
        run.refresh = evaluate_publication(refreshed.offer, refreshed.context)
        run.refreshed_snapshot = publish(
            conn,
            chosen.offer_id,
            store=run.store,
            evaluated_at=moment,
            snapshot_id="fixture-refreshed",
        )
    engine.dispose()
    return run

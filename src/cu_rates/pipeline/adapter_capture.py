"""Extract an already stored, approved direct capture without opening any connection.

Uses existing append-only extraction/capture/failure tables. Failure diagnostics carry
confidence; no schema/contract changes or automatic approval decisions are introduced.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import NoResultFound

from cu_rates.adapters.registry import approved_adapter, load_configuration, normalize_row
from cu_rates.adapters.static_html import Confidence, configuration_parser, extract
from cu_rates.artifact_store import ArtifactStore, ArtifactStoreError
from cu_rates.contracts.enums import RunStatus
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import VerifiedScenarioStatement
from cu_rates.contracts.sources import FetchRun, RateSource, SourceFingerprint
from cu_rates.db.schema import products, slice_normalization_failures
from cu_rates.pipeline.trusted import (
    _product,
    insert_artifact,
    insert_capture,
    insert_extraction_run,
    insert_fingerprint,
    insert_run,
    insert_statements,
    insert_validations,
    load_source_approval,
    persist_candidate,
)
from cu_rates.validation import ValidationContext, validate_offer


@dataclass(frozen=True)
class CaptureExtraction:
    offers: tuple[NormalizedOffer, ...]
    confidence: Confidence
    diagnostics: tuple[str, ...]


def extract_stored_capture(
    conn: Connection,
    store: ArtifactStore,
    source: RateSource,
    run: FetchRun,
) -> CaptureExtraction:
    """Persist candidates, proofs and checks from stored bytes; never publish or fetch.

    Source identity, product relationship and collection approval still belong
    to their existing gates. Calling this function cannot make any of them approved.
    """
    approval = load_source_approval(conn, source.source_id, run.finished_at)
    adapter = approved_adapter(source, approval)  # production adapters only
    config = load_configuration(source)
    if (
        approval is None
        or approval.approved_layout_sha256 != config.layout_sha256
        or approval.adapter != configuration_parser(config)
        or adapter.fixture
        or run.source_id != source.source_id
        or run.status is not RunStatus.SUCCEEDED
        or run.profile_sha256 != approval.approved_profile_sha256
    ):
        raise ValueError("capture is not bound to the approved source/profile/configuration")
    artifact = store.get_observation(run.run_id, source.url)
    if (
        artifact.artifact_id not in run.artifact_ids
        or artifact.http_status != 200
        or not run.started_at <= artifact.fetched_at <= run.finished_at
    ):
        raise ValueError("stored artifact is not a direct capture of this run")
    insert_run(conn, run)
    insert_artifact(conn, artifact)
    extraction_id = f"extract-{run.run_id}"
    stage = dict(
        run_id=extraction_id,
        schema_version="1.0",
        fetch_run_id=run.run_id,
        parser_name=adapter.parser.name,
        parser_version=adapter.parser.version,
        git_sha=run.git_sha,
        configuration_sha256=config.sha256,
        started_at=run.finished_at,
        finished_at=run.finished_at,
    )
    try:
        result = extract(
            config,
            source,
            store.read(artifact),
            artifact,
            extraction_run_id=extraction_id,
            git_sha=run.git_sha,
        )
    except (ArtifactStoreError, OSError, ValueError) as exc:
        insert_extraction_run(
            conn,
            {**stage, "status": "failed", "candidate_count": 0, "error": f"low confidence: {exc}"},
        )
        return CaptureExtraction((), "low", (str(exc),))
    # Prepare every row before persisting any candidate, fingerprint, offer or proof.
    prepared = []
    errors = list(result.diagnostics)
    row_errors = []
    if result.confidence != "low":
        for row in result.rows:
            try:
                offer = normalize_row(row, config, artifact)
                product = _product(
                    conn.execute(
                        select(products).where(products.c.product_id == offer.product_id)
                    ).one()
                )
                proof = validate_offer(
                    offer,
                    ValidationContext(
                        evaluated_at=run.finished_at,
                        source=source,
                        product=product,
                        artifacts=(artifact,),
                        fetch_runs=(run,),
                        candidates=row.candidates,
                    ),
                )
                if proof.quarantine is not None:
                    raise ValueError(
                        "row validation failed: "
                        + "; ".join(
                            result.detail or result.rule_id
                            for result in proof.results
                            if not result.passed
                        )
                    )
                prepared.append((row, offer, proof))
            except (ValueError, NoResultFound) as exc:
                errors.append(f"{row.row.offer_id}: {exc}")
                row_errors.append((row.row.offer_id, str(exc)))
    low = result.confidence == "low" or bool(errors)
    insert_extraction_run(
        conn,
        {
            **stage,
            "status": "failed" if low else "succeeded",
            "candidate_count": 0 if low else sum(len(r.candidates) for r in result.rows),
            "error": "; ".join(errors) if low else None,
        },
    )
    for row_id, reason in row_errors:
        conn.execute(
            slice_normalization_failures.insert(),
            dict(extraction_run_id=extraction_id, row_id=row_id, reason=reason),
        )
    if low:
        return CaptureExtraction((), "low", tuple(errors))
    insert_fingerprint(
        conn,
        SourceFingerprint(
            schema_version="1.0",
            source_id=source.source_id,
            fetch_run_id=run.run_id,
            artifact_id=artifact.artifact_id,
            observed_at=artifact.fetched_at,
            content_sha256=artifact.artifact_id,
            structure_sha256=config.layout_sha256,
            adapter=adapter.parser,
        ),
        f"layout-{run.run_id}",
    )
    offers = []
    for row, offer, proof in prepared:
        for candidate in row.candidates:
            persist_candidate(conn, candidate, config.institution_charter)
        insert_capture(conn, offer, run.run_id)
        insert_validations(conn, proof.for_publication(offer))
        insert_statements(
            conn,
            config.institution_charter,
            tuple(
                VerifiedScenarioStatement(
                    offer_id=offer.offer_id,
                    dimension=m.dimension,
                    fetch_run_id=run.run_id,
                    stated=m.stated,
                    provenance=m.provenance,
                )
                for m in offer.scenario_matches
                if m.stated is not None and m.provenance is not None
            ),
        )
        offers.append(offer)
    return CaptureExtraction(tuple(offers), result.confidence, ())

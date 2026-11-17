"""Load publication inputs only from rows this process stored. Missing rows fail closed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.artifact_store import ArtifactStore, ArtifactStoreError
from cu_rates.contracts.audit import (
    Correction,
    PublishedSnapshot,
    ReviewDecision,
    SnapshotEntry,
    WithdrawalEntry,
    snapshot_manifest_sha256,
)
from cu_rates.contracts.base import CONTRACT_VERSION, UtcDatetime
from cu_rates.contracts.enums import OfferStatus, RunStatus
from cu_rates.contracts.evidence import HtmlLocator, ParserVersion, Provenance, RawArtifact
from cu_rates.contracts.fingerprints import (
    OfferFingerprints,
    OfferSnapshot,
    offer_fingerprints,
)
from cu_rates.contracts.offers import NormalizedOffer, OfferCondition
from cu_rates.contracts.publication import (
    REVIEW_REASONS,
    EvidencePath,
    Placement,
    PriorPublication,
    PublicationContext,
    PublicationOutcome,
    ReasonCode,
    ValidationPolicy,
    ValidationResult,
    VerifiedScenarioCondition,
    VerifiedScenarioStatement,
    corroborated_paths,
    evaluate_publication,
    financial_items,
    publication_evidence_sha256,
)
from cu_rates.contracts.registry import (
    Institution,
    InstitutionSourceRelationship,
    ProductIdentity,
)
from cu_rates.contracts.scenarios import StatedBound
from cu_rates.contracts.sources import (
    ConditionalValidator,
    ExtractedCandidate,
    FetchProfile,
    FetchRun,
    NotModifiedObservation,
    RateSource,
    SourceApproval,
    SourceFingerprint,
    is_strong_etag,
    profile_sha256,
)
from cu_rates.contracts.taxonomy import ProductFinding
from cu_rates.db.records import (
    _insert_provenance,
    _load_provenance,
    load_normalized_offer,
    load_review_decision,
    persist_normalized_offer,
    persist_review_decision,
    provenance_id,
)
from cu_rates.db.schema import (
    artifact_blobs,
    artifact_observations,
    correction_evidence,
    corrections,
    extracted_candidates,
    extraction_runs,
    fetch_profiles,
    fetch_runs,
    finding_evidence,
    finding_purposes,
    institution_source_relationships,
    institutions,
    normalized_offers,
    not_modified_observations,
    product_findings,
    products,
    published_snapshots,
    rate_sources,
    review_decisions,
    slice_offer_captures,
    slice_snapshot_baselines,
    snapshot_entries,
    snapshot_reviews,
    source_approval_evidence,
    source_approvals,
    source_fingerprints,
    validation_policies,
    validation_results,
    verified_scenario_conditions,
    verified_scenario_statements,
    withdrawal_entries,
    withdrawal_lifts,
)
from cu_rates.pipeline.extract import excerpt_is_located, layout_sha256, resolve_text

_FRESHNESS = timedelta(hours=71)


@dataclass(frozen=True)
class TrustedView:
    """The offer as the stored 304 claims it, plus a context built only from tables."""

    offer: NormalizedOffer
    context: PublicationContext
    current_review: ReviewDecision | None


def insert_institution(conn: Connection, institution: Institution) -> None:
    conn.execute(
        institutions.insert(),
        {
            "charter_number": institution.charter_number,
            "schema_version": institution.schema_version,
            "legal_name": institution.legal_name,
            "status": institution.status.value,
            "successor_charter_number": institution.successor_charter_number,
            "charter_verified": int(institution.charter_verified),
        },
    )


def insert_source(conn: Connection, source: RateSource) -> None:
    conn.execute(
        rate_sources.insert(),
        {
            "source_id": source.source_id,
            "schema_version": source.schema_version,
            "url": source.url,
            "evidence_kind": source.evidence_kind.value,
            "description": source.description,
        },
    )


def insert_product(conn: Connection, product: ProductIdentity) -> None:
    conn.execute(
        products.insert(),
        {
            "product_id": product.product_id,
            "schema_version": product.schema_version,
            "institution_charter": product.institution_charter,
            "family": product.family.value,
            "name": product.name,
            "lifecycle": product.lifecycle.value,
            "rate_availability": product.rate_availability.value,
            "conforming_status": product.conforming_status.value,
            "government_guaranty": product.government_guaranty.value,
            "notes_json": list(product.notes),
        },
    )


def insert_relationship(conn: Connection, relationship: InstitutionSourceRelationship) -> None:
    conn.execute(
        institution_source_relationships.insert(),
        {
            "relationship_id": relationship.relationship_id,
            "schema_version": relationship.schema_version,
            "institution_charter": relationship.institution_charter,
            "source_id": relationship.source_id,
            "product_id": relationship.product_id,
            "provider_id": relationship.provider_id,
            "kind": relationship.kind.value,
            "approved_by": relationship.approved_by,
            "approved_at": relationship.approved_at,
        },
    )


def insert_profile(conn: Connection, profile: FetchProfile) -> None:
    conn.execute(
        fetch_profiles.insert(),
        {
            "profile_id": profile.profile_id,
            "schema_version": profile.schema_version,
            "source_id": profile.source_id,
            "method": profile.method.value,
            "min_interval_seconds": profile.min_interval_seconds,
            "timeout_seconds": profile.timeout_seconds,
            "respect_robots": 1,
        },
    )


def insert_approval(conn: Connection, approval: SourceApproval, charter: int) -> None:
    adapter = approval.adapter
    conn.execute(
        source_approvals.insert(),
        {
            "approval_id": approval.approval_id,
            "schema_version": approval.schema_version,
            "source_id": approval.source_id,
            "source_url": approval.source_url,
            "decision": approval.decision.value,
            "actor": approval.actor,
            "decided_at": approval.decided_at,
            "policy_id": approval.policy_id,
            "policy_version": approval.policy_version,
            "rationale": approval.rationale,
            "approved_profile_sha256": approval.approved_profile_sha256,
            "approved_layout_sha256": approval.approved_layout_sha256,
            "adapter_name": None if adapter is None else adapter.name,
            "adapter_version": None if adapter is None else adapter.version,
            "approved_origins_json": list(approval.approved_origins),
        },
    )
    for record in approval.evidence:
        conn.execute(
            source_approval_evidence.insert(),
            {
                "approval_id": approval.approval_id,
                "provenance_id": _insert_provenance(conn, record, charter),
            },
        )


def insert_run(conn: Connection, run: FetchRun) -> None:
    validator = run.conditional_validator
    conn.execute(
        fetch_runs.insert(),
        {
            "run_id": run.run_id,
            "schema_version": run.schema_version,
            "source_id": run.source_id,
            "profile_id": run.profile_id,
            "profile_sha256": run.profile_sha256,
            "status": run.status.value,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "git_sha": run.git_sha,
            "error": run.error,
            "validator_kind": None if validator is None else validator.kind,
            "validator_value": None if validator is None else validator.value,
        },
    )


def insert_artifact(conn: Connection, artifact: RawArtifact) -> None:
    stored = conn.execute(
        select(artifact_blobs.c.byte_length).where(
            artifact_blobs.c.artifact_id == artifact.artifact_id
        )
    ).scalar()
    if stored is None:
        conn.execute(
            artifact_blobs.insert(),
            {"artifact_id": artifact.artifact_id, "byte_length": artifact.byte_length},
        )
    elif stored != artifact.byte_length:
        raise ValueError("stored artifact bytes do not match this observation")
    conn.execute(
        artifact_observations.insert(),
        {
            "fetch_run_id": artifact.fetch_run_id,
            "source_url": artifact.source_url,
            "schema_version": artifact.schema_version,
            "artifact_id": artifact.artifact_id,
            "media_type": artifact.media_type,
            "fetched_at": artifact.fetched_at,
            "http_status": artifact.http_status,
        },
    )


def insert_reobservation(conn: Connection, observation: NotModifiedObservation) -> None:
    conn.execute(
        not_modified_observations.insert(),
        {
            "run_id": observation.run_id,
            "schema_version": observation.schema_version,
            "source_id": observation.source_id,
            "profile_id": observation.profile_id,
            "profile_sha256": observation.profile_sha256,
            "git_sha": observation.git_sha,
            "started_at": observation.started_at,
            "observed_at": observation.observed_at,
            "original_run_id": observation.original_run_id,
            "original_artifact_id": observation.original_artifact_id,
            "validator_kind": observation.validator.kind,
            "validator_value": observation.validator.value,
            "response_etag": observation.response_etag,
            "http_status": observation.http_status,
        },
    )


def insert_fingerprint(
    conn: Connection, fingerprint: SourceFingerprint, fingerprint_id: str
) -> None:
    adapter = fingerprint.adapter
    conn.execute(
        source_fingerprints.insert(),
        {
            "fingerprint_id": fingerprint_id,
            "schema_version": fingerprint.schema_version,
            "source_id": fingerprint.source_id,
            "fetch_run_id": fingerprint.fetch_run_id,
            "artifact_id": fingerprint.artifact_id,
            "observed_at": fingerprint.observed_at,
            "content_sha256": fingerprint.content_sha256,
            "structure_sha256": fingerprint.structure_sha256,
            "adapter_name": None if adapter is None else adapter.name,
            "adapter_version": None if adapter is None else adapter.version,
        },
    )


def insert_policy(conn: Connection, policy: ValidationPolicy) -> None:
    conn.execute(
        validation_policies.insert(),
        {
            "policy_id": policy.policy_id,
            "schema_version": policy.schema_version,
            "policy_version": policy.policy_version,
            "required_rules_json": list(policy.required_rules),
            "approved_by": policy.approved_by,
            "approved_at": policy.approved_at,
        },
    )


def insert_offer(conn: Connection, offer: NormalizedOffer) -> NormalizedOffer:
    return persist_normalized_offer(conn, offer)


def insert_validations(conn: Connection, results: tuple[ValidationResult, ...]) -> None:
    for result in results:
        validation_id = sha256(result.model_dump_json().encode()).hexdigest()
        if (
            conn.execute(
                select(validation_results.c.validation_id).where(
                    validation_results.c.validation_id == validation_id
                )
            ).first()
            is not None
        ):
            continue
        conn.execute(
            validation_results.insert(),
            {
                "validation_id": validation_id,
                "rule_id": result.rule_id,
                "passed": int(result.passed),
                "detail": result.detail,
                "offer_id": result.offer_id,
                "fingerprints_json": result.fingerprints.model_dump(mode="json"),
                "evaluated_at": result.evaluated_at,
                "validator_name": result.validator.name,
                "validator_version": result.validator.version,
                "checked_paths_json": list(result.checked_paths),
            },
        )


def insert_review(conn: Connection, review: ReviewDecision, charter: int) -> ReviewDecision:
    return persist_review_decision(conn, review, institution_charter=charter)


def insert_statements(
    conn: Connection, charter: int, statements: tuple[VerifiedScenarioStatement, ...]
) -> None:
    for statement in statements:
        conn.execute(
            verified_scenario_statements.insert(),
            {
                "statement_id": (
                    f"{statement.offer_id}.{statement.dimension.value}.{statement.fetch_run_id}"
                ),
                "schema_version": CONTRACT_VERSION,
                "offer_id": statement.offer_id,
                "dimension": statement.dimension.value,
                "fetch_run_id": statement.fetch_run_id,
                "stated_json": statement.stated.model_dump(mode="json"),
                "provenance_id": _insert_provenance(conn, statement.provenance, charter),
            },
        )


def insert_condition_proofs(
    conn: Connection, proofs: tuple[VerifiedScenarioCondition, ...]
) -> None:
    for proof in proofs:
        conn.execute(
            verified_scenario_conditions.insert(),
            {
                "offer_id": proof.offer_id,
                "scenario_id": proof.scenario_id.value,
                "condition_id": proof.condition.condition_id,
                "schema_version": CONTRACT_VERSION,
                "fetch_run_id": proof.fetch_run_id,
                "condition_json": proof.condition.model_dump(mode="json"),
            },
        )


def insert_finding(conn: Connection, finding: ProductFinding) -> None:
    conn.execute(
        product_findings.insert(),
        {
            "finding_id": finding.finding_id,
            "schema_version": finding.schema_version,
            "taxonomy_version": finding.taxonomy_version,
            "institution_charter": finding.institution_charter,
            "source_id": finding.source_id,
            "product_id": finding.product_id,
            "observed_at": finding.observed_at,
            "label": finding.label,
            "scope": finding.scope.value,
            "family": None if finding.family is None else finding.family.value,
            "out_of_scope_product": None
            if finding.out_of_scope_product is None
            else finding.out_of_scope_product.value,
            "rate_availability": None
            if finding.rate_availability is None
            else finding.rate_availability.value,
            "review_state": finding.review_state.value,
            "reviewed_by": finding.reviewed_by,
            "reviewed_at": finding.reviewed_at,
            "review_rationale": finding.review_rationale,
        },
    )
    if finding.purposes:
        conn.execute(
            finding_purposes.insert(),
            [
                {"finding_id": finding.finding_id, "purpose": purpose.value}
                for purpose in finding.purposes
            ],
        )
    if finding.evidence:
        conn.execute(
            finding_evidence.insert(),
            [
                {
                    "finding_id": finding.finding_id,
                    "provenance_id": _insert_provenance(conn, record, finding.institution_charter),
                }
                for record in finding.evidence
            ],
        )


def insert_withdrawal(conn: Connection, entry: WithdrawalEntry) -> None:
    conn.execute(
        withdrawal_entries.insert(),
        {
            "withdrawal_id": entry.withdrawal_id,
            "schema_version": entry.schema_version,
            "scope": entry.scope.value,
            "target_id": entry.target_id,
            "actor": entry.actor,
            "withdrawn_at": entry.withdrawn_at,
            "reason": entry.reason,
        },
    )
    if entry.lifted_at is not None:
        conn.execute(
            withdrawal_lifts.insert(),
            {
                "withdrawal_id": entry.withdrawal_id,
                "lifted_at": entry.lifted_at,
                "lifted_by": entry.lifted_by,
            },
        )


def _insert_snapshot(conn: Connection, snapshot: PublishedSnapshot) -> None:
    if snapshot.manifest_sha256 != snapshot_manifest_sha256(snapshot.entries):
        raise ValueError("snapshot manifest does not match its entries")
    conn.execute(
        published_snapshots.insert(),
        {
            "snapshot_id": snapshot.snapshot_id,
            "schema_version": snapshot.schema_version,
            "published_at": snapshot.published_at,
            "published_by": snapshot.published_by,
            "manifest_sha256": snapshot.manifest_sha256,
            "supersedes": snapshot.supersedes,
        },
    )
    conn.execute(
        snapshot_entries.insert(),
        [
            {
                "snapshot_id": snapshot.snapshot_id,
                "offer_id": entry.offer_id,
                "structural_sha256": entry.structural_sha256,
                "value_sha256": entry.value_sha256,
            }
            for entry in snapshot.entries
        ],
    )
    if snapshot.review_ids:
        conn.execute(
            snapshot_reviews.insert(),
            [
                {"snapshot_id": snapshot.snapshot_id, "review_id": review_id}
                for review_id in snapshot.review_ids
            ],
        )


def insert_extraction_run(conn: Connection, row: dict[str, object]) -> None:
    conn.execute(extraction_runs.insert(), row)


def persist_candidate(conn: Connection, candidate: ExtractedCandidate, charter: int) -> None:
    conn.execute(
        extracted_candidates.insert(),
        {
            "candidate_id": f"{candidate.extraction_run_id}.{candidate.candidate_id}",
            "schema_version": candidate.schema_version,
            "extraction_run_id": candidate.extraction_run_id,
            "proposed_path": candidate.proposed_path,
            "raw_value": candidate.raw_value,
            "provenance_id": _insert_provenance(conn, candidate.provenance, charter),
        },
    )


def insert_capture(conn: Connection, offer: NormalizedOffer, run_id: str) -> None:
    present = conn.execute(
        select(slice_offer_captures.c.offer_id).where(
            slice_offer_captures.c.offer_id == offer.offer_id
        )
    ).first()
    if present is None:
        insert_offer(conn, offer)
    conn.execute(
        slice_offer_captures.insert(),
        {
            "offer_id": offer.offer_id,
            "fetch_run_id": run_id,
            "offer_json": offer.model_dump(mode="json"),
        },
    )


def verify_located_claim(body: bytes, provenance: Provenance) -> str:
    locator = provenance.locator
    if not isinstance(locator, HtmlLocator):
        raise ValueError("slice claims use HTML locators")
    node = resolve_text(body, locator.selector, locator.selector_type)
    if sha256(
        node.encode()
    ).hexdigest() != locator.normalized_text_sha256 or not excerpt_is_located(
        node, provenance.source_text
    ):
        raise ValueError("stored locator or excerpt differs from source")
    return provenance.source_text


def publish(
    conn: Connection,
    offer_id: str,
    *,
    store: ArtifactStore,
    evaluated_at: UtcDatetime,
    snapshot_id: str,
    reviewed: bool = False,
) -> PublishedSnapshot | None:
    """Atomically persist only a gate-approved automatic or explicitly reviewed publication."""
    view = load_publication_context(conn, offer_id, evaluated_at, store=store)
    offer = view.offer
    ctx = view.context.model_copy(update={"snapshot_id": snapshot_id})
    review = view.current_review
    decision = evaluate_publication(offer, ctx)
    prior = ctx.prior
    fp = offer_fingerprints(offer)
    if reviewed:
        # A first-publication lane is deliberately narrow: a human signature is not
        # an override for missing dates, policy, corrections, withdrawn sources, or drift.
        if not (
            decision.outcome is PublicationOutcome.REQUIRES_REVIEW
            and set(decision.reasons) & REVIEW_REASONS == {ReasonCode.FIRST_PUBLICATION}
            and not decision.degraded
            and decision.placement is not Placement.NONE
            and review is not None
            and review.approved
            and offer.observed_at <= review.decided_at <= evaluated_at
            and review.reviewed_fingerprints == fp
            and review.reviewed_evidence_sha256
            == publication_evidence_sha256(offer, corroborated_paths(offer, ctx))
        ):
            return None
    elif not decision.publishable_now:
        return None
    elif review is not None and (
        review.reviewed_fingerprints != fp
        or review.reviewed_evidence_sha256
        != publication_evidence_sha256(offer, corroborated_paths(offer, ctx))
    ):
        # A legitimate small rate change can auto-publish, but is not a new human review.
        review = None

    entry = SnapshotEntry(offer_id=offer_id, structural_sha256=fp.structural, value_sha256=fp.value)
    snapshot = PublishedSnapshot(
        schema_version=CONTRACT_VERSION,
        snapshot_id=snapshot_id,
        published_at=evaluated_at,
        published_by=review.actor if reviewed and review is not None else "t014-fixture-publisher",
        entries=(entry,),
        manifest_sha256=snapshot_manifest_sha256((entry,)),
        supersedes=prior.published.snapshot_id if prior else None,
        review_ids=(review.review_id,) if review is not None else (),
    )
    baseline = OfferSnapshot.from_offer(offer)
    _insert_snapshot(conn, snapshot)
    changed_at = evaluated_at
    if prior is not None and prior.snapshot.fingerprints.value == baseline.fingerprints.value:
        changed_at = prior.last_value_change_at
    conn.execute(
        slice_snapshot_baselines.insert(),
        {
            "snapshot_id": snapshot_id,
            "offer_id": offer_id,
            "baseline_json": {
                **baseline.model_dump(mode="json"),
                "publication_version": "2.0",
                "publication_evidence": [
                    p.model_dump(mode="json") for p in corroborated_paths(offer, ctx)
                ],
                "publication_evidence_sha256": publication_evidence_sha256(
                    offer, corroborated_paths(offer, ctx)
                ),
            },
            "last_value_change_at": changed_at,
        },
    )
    conn.execute(
        normalized_offers.update()
        .where(normalized_offers.c.offer_id == offer_id, normalized_offers.c.published_at.is_(None))
        .values(status=OfferStatus.PUBLISHED.value, published_at=evaluated_at)
    )
    return snapshot


def _parser(name: str | None, version: str | None) -> ParserVersion | None:
    if name is None or version is None:
        return None
    return ParserVersion(name=name, version=version)


def _approval(row: Any) -> SourceApproval:
    record = row
    return SourceApproval.model_validate(
        {
            "schema_version": record.schema_version,
            "approval_id": record.approval_id,
            "source_id": record.source_id,
            "source_url": record.source_url,
            "decision": record.decision,
            "actor": record.actor,
            "decided_at": record.decided_at,
            "policy_id": record.policy_id,
            "policy_version": record.policy_version,
            "rationale": record.rationale,
            "approved_profile_sha256": record.approved_profile_sha256,
            "approved_layout_sha256": record.approved_layout_sha256,
            "adapter": None
            if record.adapter_name is None
            else {"name": record.adapter_name, "version": record.adapter_version},
            "approved_origins": record.approved_origins_json or [],
        }
    )


def load_publication_context(
    conn: Connection, offer_id: str, evaluated_at: UtcDatetime, *, store: ArtifactStore
) -> TrustedView:
    """Rebuild a publication candidate from verified private bytes and append-only history."""
    registered = load_normalized_offer(conn, offer_id)
    capture = conn.execute(
        select(slice_offer_captures)
        .join(fetch_runs, fetch_runs.c.run_id == slice_offer_captures.c.fetch_run_id)
        .where(
            slice_offer_captures.c.offer_id == offer_id,
            fetch_runs.c.finished_at <= evaluated_at,
        )
        .order_by(fetch_runs.c.finished_at.desc(), fetch_runs.c.run_id.desc())
    ).first()
    offer = registered if capture is None else NormalizedOffer.model_validate(capture.offer_json)
    if (offer.offer_id, offer.source_id, offer.product_id, offer.institution_charter) != (
        registered.offer_id,
        registered.source_id,
        registered.product_id,
        registered.institution_charter,
    ):
        raise ValueError("stored observation is not bound to the registered offer")
    source = _source(
        conn.execute(select(rate_sources).where(rate_sources.c.source_id == offer.source_id)).one()
    )
    institution = _institution(
        conn.execute(
            select(institutions).where(institutions.c.charter_number == offer.institution_charter)
        ).one()
    )
    product = _product(
        conn.execute(select(products).where(products.c.product_id == offer.product_id)).one()
    )
    relationship = _relationship(
        conn.execute(
            select(institution_source_relationships).where(
                institution_source_relationships.c.institution_charter == offer.institution_charter,
                institution_source_relationships.c.source_id == offer.source_id,
                institution_source_relationships.c.product_id == offer.product_id,
            )
        ).one()
    )
    approval_row = conn.execute(
        select(source_approvals)
        .where(
            source_approvals.c.source_id == source.source_id,
            source_approvals.c.decided_at <= evaluated_at,
        )
        .order_by(
            source_approvals.c.decided_at.desc(),
            source_approvals.c.decision.desc(),
            source_approvals.c.approval_id.desc(),
        )
    ).first()
    approval = None if approval_row is None else _approval(approval_row)
    run_rows = conn.execute(
        select(fetch_runs).where(
            fetch_runs.c.source_id == source.source_id, fetch_runs.c.finished_at <= evaluated_at
        )
    ).all()
    observations = conn.execute(
        select(artifact_observations).where(
            artifact_observations.c.fetch_run_id.in_([row.run_id for row in run_rows])
        )
    ).all()
    ids_by_run: dict[str, list[str]] = {}
    for item in observations:
        ids_by_run.setdefault(item.fetch_run_id, []).append(item.artifact_id)
    runs = tuple(_run(row, tuple(set(ids_by_run.get(row.run_id, ())))) for row in run_rows)
    cited = {item.artifact_id for item in offer.iter_provenance()}
    matching = [
        item
        for item in observations
        if item.source_url == source.url
        and item.artifact_id in cited
        and item.fetched_at == offer.observed_at
        and (capture is None or capture.fetch_run_id == item.fetch_run_id)
    ]
    basis = next(
        (run for run in runs if len(matching) == 1 and run.run_id == matching[0].fetch_run_id), None
    )
    verified: tuple[RawArtifact, ...] = ()
    body: bytes | None = None
    comparable = False
    if basis is not None and not any(
        item.source_url == source.url
        and item.http_status == 200
        and item.fetched_at > offer.observed_at
        for item in observations
    ):
        item = matching[0]
        artifact = RawArtifact(
            schema_version=item.schema_version,
            artifact_id=item.artifact_id,
            media_type=item.media_type,
            source_url=item.source_url,
            fetched_at=item.fetched_at,
            http_status=item.http_status,
            byte_length=blob_length(conn, item.artifact_id),
            fetch_run_id=item.fetch_run_id,
        )
        try:
            raw = store.read(artifact)
            if store.get_observation(artifact.fetch_run_id, artifact.source_url) != artifact:
                raise ValueError("stored observation differs from the database")
            if cited != {artifact.artifact_id}:
                raise ValueError("slice claims must cite the single source artifact")
            from cu_rates.pipeline.slice import verify_offer_bytes

            comparable = verify_offer_bytes(source, offer, raw, artifact)
            body, verified = raw, (artifact,)
        except (ArtifactStoreError, ValueError, OSError):
            pass

    primary_verified = bool(verified)
    profile = None
    if basis is not None:
        row = conn.execute(
            select(fetch_profiles).where(fetch_profiles.c.profile_id == basis.profile_id)
        ).one_or_none()
        if row is not None:
            profile = FetchProfile(
                schema_version=row.schema_version,
                profile_id=row.profile_id,
                source_id=row.source_id,
                method=row.method,
                min_interval_seconds=row.min_interval_seconds,
                timeout_seconds=row.timeout_seconds,
            )
    reobservations = tuple(
        NotModifiedObservation(
            schema_version=row.schema_version,
            run_id=row.run_id,
            source_id=row.source_id,
            profile_id=row.profile_id,
            profile_sha256=row.profile_sha256,
            git_sha=row.git_sha,
            started_at=row.started_at,
            observed_at=row.observed_at,
            original_run_id=row.original_run_id,
            original_artifact_id=row.original_artifact_id,
            validator=ConditionalValidator(kind=row.validator_kind, value=row.validator_value),
            response_etag=row.response_etag,
        )
        for row in conn.execute(
            select(not_modified_observations).where(
                not_modified_observations.c.source_id == source.source_id,
                not_modified_observations.c.observed_at <= evaluated_at,
            )
        ).all()
    )
    prior = _prior(conn, offer.offer_id, evaluated_at)
    current_review = _subject_review(conn, "offer", offer_id, evaluated_at)
    if current_review is not None and not current_review.approved:
        offer = offer.model_copy(update={"status": OfferStatus.QUARANTINED, "published_at": None})
    elif registered.status in {
        OfferStatus.QUARANTINED,
        OfferStatus.WITHDRAWN,
        OfferStatus.SUPERSEDED,
    }:
        offer = offer.model_copy(update={"status": registered.status, "published_at": None})
    latest: dict[str, ValidationResult] = {}
    current = offer_fingerprints(offer)
    for row in conn.execute(
        select(validation_results)
        .where(
            validation_results.c.offer_id == offer_id,
            validation_results.c.evaluated_at >= offer.observed_at,
            validation_results.c.evaluated_at <= evaluated_at,
        )
        .order_by(
            validation_results.c.evaluated_at.desc(),
            validation_results.c.passed.asc(),
            validation_results.c.validation_id.desc(),
        )
    ).all():
        result = _validation(row)
        if result.fingerprints == current:
            latest.setdefault(result.rule_id, result)
    policy_row = conn.execute(
        select(validation_policies)
        .where(validation_policies.c.approved_at <= evaluated_at)
        .order_by(validation_policies.c.approved_at.desc(), validation_policies.c.policy_id.desc())
    ).first()
    policy = (
        None
        if policy_row is None
        else ValidationPolicy(
            schema_version=policy_row.schema_version,
            policy_id=policy_row.policy_id,
            policy_version=policy_row.policy_version,
            required_rules=tuple(policy_row.required_rules_json),
            approved_by=policy_row.approved_by,
            approved_at=policy_row.approved_at,
        )
    )
    if not primary_verified:
        offer = offer.model_copy(update={"status": OfferStatus.QUARANTINED, "published_at": None})
    if (
        offer.status is OfferStatus.CANDIDATE
        and policy is not None
        and set(policy.required_rules) <= latest.keys()
        and all(item.passed for item in latest.values())
    ):
        # Preflight only: the domain gate accepts approved-shaped offers. This projection
        # is never persisted and is not human approval or permission to write a snapshot.
        offer = offer.model_copy(update={"status": OfferStatus.APPROVED})
    layouts = set()
    statements: tuple[VerifiedScenarioStatement, ...] = ()
    conditions: tuple[VerifiedScenarioCondition, ...] = ()
    if basis is not None and verified and body is not None:
        artifact = verified[0]
        layout = layout_sha256(body)
        layouts = {
            _fingerprint(row)
            for row in conn.execute(
                select(source_fingerprints).where(
                    source_fingerprints.c.source_id == source.source_id,
                    source_fingerprints.c.fetch_run_id == basis.run_id,
                    source_fingerprints.c.artifact_id == artifact.artifact_id,
                    source_fingerprints.c.structure_sha256 == layout,
                )
            ).all()
        }
        if comparable:
            statements = tuple(
                proof for proof in _statements(conn, offer_id) if proof.fetch_run_id == basis.run_id
            )
        conditions = tuple(
            proof
            for proof in _conditions(conn, offer_id)
            if proof.fetch_run_id == basis.run_id and proof.condition in offer.conditions
        )
    # Read separate stored offer observations. No inferred captures: missing or
    # unverifiable history remains absent and the publication gate fails closed.
    paths: list[EvidencePath] = []
    artifacts = {item.artifact_id: item for item in verified}
    all_statements = list(statements)
    captures = conn.execute(
        select(slice_offer_captures).where(slice_offer_captures.c.offer_id == offer_id)
    ).all()
    for saved in captures:
        observed_offer = NormalizedOffer.model_validate(saved.offer_json)
        if (
            observed_offer.offer_id != offer.offer_id
            or observed_offer.product_id != offer.product_id
            or observed_offer.source_id != offer.source_id
            or observed_offer.institution_charter != offer.institution_charter
        ):
            continue
        records = [
            item
            for item in observations
            if item.fetch_run_id == saved.fetch_run_id
            and item.source_url == source.url
            and item.fetched_at == observed_offer.observed_at
        ]
        if len(records) != 1:
            continue
        item = records[0]
        art = RawArtifact(
            schema_version=item.schema_version,
            artifact_id=item.artifact_id,
            media_type=item.media_type,
            source_url=item.source_url,
            fetched_at=item.fetched_at,
            http_status=item.http_status,
            byte_length=blob_length(conn, item.artifact_id),
            fetch_run_id=item.fetch_run_id,
        )
        try:
            raw = store.read(art)
            if store.get_observation(art.fetch_run_id, art.source_url) != art:
                continue
            from cu_rates.pipeline.slice import verify_offer_bytes

            if not verify_offer_bytes(source, observed_offer, raw, art):
                continue
        except (ArtifactStoreError, ValueError, OSError):
            continue
        proofs = tuple(
            proof
            for proof in _statements(conn, offer_id)
            if proof.fetch_run_id == art.fetch_run_id
            and proof.provenance.artifact_id == art.artifact_id
            and any(
                match.dimension == proof.dimension
                and match.stated == proof.stated
                and match.provenance == proof.provenance
                for match in observed_offer.scenario_matches
            )
        )
        for proof in proofs:
            if proof not in all_statements:
                all_statements.append(proof)
        # artifact_id is the stored content address, not a newly computed hash.
        artifacts.setdefault(art.artifact_id, art)
        wanted = {path: value for path, value, _ in financial_items(offer)}
        for path, value, provenance in financial_items(observed_offer):
            if wanted.get(path) != value:
                continue
            paths.append(
                EvidencePath(
                    path=path,
                    value=value,
                    artifact_id=art.artifact_id,
                    content_sha256=art.artifact_id,
                    capture_id=art.fetch_run_id,
                    observed_at=art.fetched_at,
                    product_id=observed_offer.product_id,
                    scenario_id=observed_offer.scenario_id,
                    source_id=source.source_id,
                    source_class=source.evidence_kind.value,
                    provenance=provenance,
                    scenario_statements=proofs,
                )
            )
    verified = tuple(artifacts.values())
    statements = tuple(all_statements)
    view = TrustedView(
        offer,
        PublicationContext(
            institution=institution,
            product=product,
            relationship=relationship,
            source=source,
            source_approval=approval,
            validation_policy=policy,
            approved_parsers=()
            if approval is None or approval.adapter is None
            else (approval.adapter,),
            known_artifacts=verified,
            fetch_runs=runs,
            reobservations=reobservations,
            approved_profile=profile,
            source_fingerprints=tuple(layouts) if len(layouts) == 1 else (),
            verified_scenario_statements=statements,
            verified_scenario_conditions=conditions,
            evidence_paths=tuple(paths),
            validations=tuple(latest[key] for key in sorted(latest)),
            prior=prior,
            product_review=_subject_review(conn, "product", offer.product_id, evaluated_at),
            corrections=_corrections(conn, offer_id, evaluated_at, store),
            withdrawals=_withdrawals(conn),
            evaluated_at=evaluated_at,
        ),
        current_review,
    )

    if (
        basis is not None
        and verified
        and profile is not None
        and approval is not None
        and prior is not None
        and prior.review is not None
        and prior.review.approved
        and prior.review.reviewed_evidence_sha256
        == publication_evidence_sha256(offer, corroborated_paths(offer, view.context))
        and prior.snapshot.fingerprints == offer_fingerprints(offer)
        and offer.valid_until is not None
    ):
        span = offer.valid_until - offer.observed_at
        if timedelta(0) < span <= _FRESHNESS:
            for when in sorted({item.observed_at for item in reobservations}, reverse=True):
                claims = [
                    item
                    for item in reobservations
                    if item.observed_at == when and item.original_artifact_id in cited
                ]
                if (
                    when > offer.observed_at
                    and claims
                    and all(
                        _refresh_allowed(item, verified[0], basis, profile, approval)
                        for item in claims
                    )
                ):
                    offer = offer.model_copy(
                        update={"observed_at": when, "valid_until": when + span}
                    )
                    break
    return TrustedView(offer, view.context, current_review)


def _refresh_allowed(
    item: NotModifiedObservation,
    artifact: RawArtifact,
    run: FetchRun,
    profile: FetchProfile,
    approval: SourceApproval,
) -> bool:
    """Only an identical, strong ETag on the exact approved capture can advance time."""
    validator = run.conditional_validator
    if validator is None or item.validator != validator:
        return False
    if validator.kind == "etag" and not is_strong_etag(validator.value):
        return False
    if validator.kind != "etag":
        return False
    return (
        approval.approved
        and approval.source_url == artifact.source_url
        and run.status is RunStatus.SUCCEEDED
        and artifact.http_status == 200
        and item.original_run_id == run.run_id
        and item.original_artifact_id == artifact.artifact_id
        and item.source_id == run.source_id == profile.source_id == approval.source_id
        and item.profile_id == run.profile_id == profile.profile_id
        and item.profile_sha256
        == run.profile_sha256
        == approval.approved_profile_sha256
        == profile_sha256(profile)
        and approval.decided_at <= run.started_at <= run.finished_at <= item.started_at
        and run.started_at <= artifact.fetched_at <= run.finished_at
        and item.response_etag == validator.value
    )


def blob_length(conn: Connection, artifact_id: str) -> int:
    value = conn.execute(
        select(artifact_blobs.c.byte_length).where(artifact_blobs.c.artifact_id == artifact_id)
    ).scalar()
    if value is None:
        raise ValueError(f"artifact {artifact_id} has no stored blob")
    return int(value)


def _institution(row: Any) -> Institution:
    return Institution(
        schema_version=row.schema_version,
        charter_number=row.charter_number,
        legal_name=row.legal_name,
        status=row.status,
        successor_charter_number=row.successor_charter_number,
        charter_verified=bool(row.charter_verified),
    )


def _product(row: Any) -> ProductIdentity:
    return ProductIdentity(
        schema_version=row.schema_version,
        product_id=row.product_id,
        institution_charter=row.institution_charter,
        family=row.family,
        name=row.name,
        lifecycle=row.lifecycle,
        rate_availability=row.rate_availability,
        conforming_status=row.conforming_status,
        government_guaranty=row.government_guaranty,
        notes=tuple(row.notes_json or ()),
    )


def _relationship(row: Any) -> InstitutionSourceRelationship:
    return InstitutionSourceRelationship(
        schema_version=row.schema_version,
        relationship_id=row.relationship_id,
        institution_charter=row.institution_charter,
        source_id=row.source_id,
        product_id=row.product_id,
        provider_id=row.provider_id,
        kind=row.kind,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
    )


def _source(row: Any) -> RateSource:
    return RateSource(
        schema_version=row.schema_version,
        source_id=row.source_id,
        url=row.url,
        evidence_kind=row.evidence_kind,
        description=row.description,
    )


def _run(row: Any, artifact_ids: tuple[str, ...]) -> FetchRun:
    validator = None
    if row.validator_kind is not None:
        validator = {"kind": row.validator_kind, "value": row.validator_value}
    return FetchRun.model_validate(
        {
            "schema_version": row.schema_version,
            "run_id": row.run_id,
            "source_id": row.source_id,
            "profile_id": row.profile_id,
            "profile_sha256": row.profile_sha256,
            "status": row.status,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "git_sha": row.git_sha,
            "artifact_ids": artifact_ids,
            "error": row.error,
            "conditional_validator": validator,
        }
    )


def _fingerprint(row: Any) -> SourceFingerprint:
    return SourceFingerprint(
        schema_version=row.schema_version,
        source_id=row.source_id,
        fetch_run_id=row.fetch_run_id,
        artifact_id=row.artifact_id,
        observed_at=row.observed_at,
        content_sha256=row.content_sha256,
        structure_sha256=row.structure_sha256,
        adapter=_parser(row.adapter_name, row.adapter_version),
    )


def _validation(row: Any) -> ValidationResult:
    return ValidationResult.model_validate(
        {
            "rule_id": row.rule_id,
            "passed": bool(row.passed),
            "detail": row.detail,
            "offer_id": row.offer_id,
            "fingerprints": row.fingerprints_json,
            "evaluated_at": row.evaluated_at,
            "validator": {"name": row.validator_name, "version": row.validator_version},
            "checked_paths": row.checked_paths_json,
        }
    )


def _subject_review(
    conn: Connection, subject: str, subject_id: str, evaluated_at: UtcDatetime
) -> ReviewDecision | None:
    row = conn.execute(
        select(review_decisions.c.review_id)
        .where(
            review_decisions.c.subject == subject,
            review_decisions.c.subject_id == subject_id,
            review_decisions.c.decided_at <= evaluated_at,
        )
        .order_by(
            review_decisions.c.decided_at.desc(),
            review_decisions.c.verdict.desc(),
            review_decisions.c.review_id.desc(),
        )
    ).first()
    if row is None:
        return None
    return load_review_decision(conn, str(row.review_id))


def _corrections(
    conn: Connection, offer_id: str, evaluated_at: UtcDatetime, store: ArtifactStore
) -> tuple[Correction, ...]:
    results: list[Correction] = []
    for row in conn.execute(
        select(corrections).where(
            corrections.c.offer_id == offer_id, corrections.c.corrected_at <= evaluated_at
        )
    ).all():
        ids = set(
            conn.execute(
                select(correction_evidence.c.provenance_id).where(
                    correction_evidence.c.correction_id == row.correction_id
                )
            ).scalars()
        )
        evidence = tuple(_load_provenance(conn, ids)[key] for key in sorted(ids))
        try:
            for item in evidence:
                verify_located_claim(store.read_body(item.artifact_id), item)
        except (ArtifactStoreError, OSError, ValueError):
            # Keep the correction in force even if its supporting bytes are unavailable.
            evidence = ()
        results.append(
            Correction(
                schema_version=row.schema_version,
                correction_id=row.correction_id,
                offer_id=offer_id,
                field_path=row.field_path,
                previous_value=row.previous_value,
                corrected_value=row.corrected_value,
                actor=row.actor,
                corrected_at=row.corrected_at,
                reason=row.reason,
                evidence=evidence,
                corrected_fingerprints=(
                    None
                    if row.fingerprints_json is None
                    else OfferFingerprints.model_validate(row.fingerprints_json)
                ),
            )
        )
    return tuple(results)


def _statements(conn: Connection, offer_id: str) -> tuple[VerifiedScenarioStatement, ...]:
    rows = conn.execute(
        select(verified_scenario_statements).where(
            verified_scenario_statements.c.offer_id == offer_id
        )
    ).all()
    loaded = _load_provenance(conn, {row.provenance_id for row in rows})
    return tuple(
        VerifiedScenarioStatement(
            offer_id=row.offer_id,
            dimension=row.dimension,
            fetch_run_id=row.fetch_run_id,
            stated=StatedBound.model_validate(row.stated_json),
            provenance=loaded[row.provenance_id],
        )
        for row in rows
    )


def _conditions(conn: Connection, offer_id: str) -> tuple[VerifiedScenarioCondition, ...]:
    rows = conn.execute(
        select(verified_scenario_conditions).where(
            verified_scenario_conditions.c.offer_id == offer_id
        )
    ).all()
    return tuple(
        VerifiedScenarioCondition(
            offer_id=row.offer_id,
            scenario_id=row.scenario_id,
            fetch_run_id=row.fetch_run_id,
            condition=OfferCondition.model_validate(row.condition_json),
        )
        for row in rows
    )


def _withdrawals(conn: Connection) -> tuple[WithdrawalEntry, ...]:
    lifts = {row.withdrawal_id: row for row in conn.execute(select(withdrawal_lifts)).all()}
    entries: list[WithdrawalEntry] = []
    for row in conn.execute(select(withdrawal_entries)).all():
        lift = lifts.get(row.withdrawal_id)
        entries.append(
            WithdrawalEntry(
                schema_version=row.schema_version,
                withdrawal_id=row.withdrawal_id,
                scope=row.scope,
                target_id=row.target_id,
                actor=row.actor,
                withdrawn_at=row.withdrawn_at,
                reason=row.reason,
                lifted_at=None if lift is None else lift.lifted_at,
                lifted_by=None if lift is None else lift.lifted_by,
            )
        )
    return tuple(entries)


def _prior(conn: Connection, offer_id: str, evaluated_at: UtcDatetime) -> PriorPublication | None:
    row = conn.execute(
        select(snapshot_entries, published_snapshots)
        .join(
            published_snapshots, published_snapshots.c.snapshot_id == snapshot_entries.c.snapshot_id
        )
        .where(
            snapshot_entries.c.offer_id == offer_id,
            published_snapshots.c.published_at <= evaluated_at,
        )
        .order_by(
            published_snapshots.c.published_at.desc(), published_snapshots.c.snapshot_id.desc()
        )
    ).first()
    if row is None:
        return None
    historical = conn.execute(
        select(slice_snapshot_baselines).where(
            slice_snapshot_baselines.c.snapshot_id == row.snapshot_id,
            slice_snapshot_baselines.c.offer_id == offer_id,
        )
    ).one_or_none()
    if historical is None:
        return None
    snapshot = _snapshot(conn, row.snapshot_id)
    baseline = OfferSnapshot.model_validate(
        {
            key: value
            for key, value in historical.baseline_json.items()
            if key
            not in {"publication_version", "publication_evidence", "publication_evidence_sha256"}
        }
    )
    reviewed = [
        found
        for review_id in snapshot.review_ids
        if (found := load_review_decision(conn, review_id)).subject_id == offer_id
        and found.reviewed_fingerprints == baseline.fingerprints
        and found.reviewed_evidence_sha256
        == historical.baseline_json.get("publication_evidence_sha256")
        and found.decided_at <= snapshot.published_at
    ]
    review = max(
        reviewed,
        key=lambda item: (item.decided_at, not item.approved, item.review_id),
        default=None,
    )
    latest = _subject_review(conn, "offer", offer_id, evaluated_at)
    if latest is not None and not latest.approved:
        review = None
    return PriorPublication(
        snapshot=baseline,
        published=snapshot,
        review=review,
        last_value_change_at=historical.last_value_change_at,
    )


def _snapshot(conn: Connection, snapshot_id: str) -> PublishedSnapshot:
    row = conn.execute(
        select(published_snapshots).where(published_snapshots.c.snapshot_id == snapshot_id)
    ).one()
    entries = conn.execute(
        select(snapshot_entries).where(snapshot_entries.c.snapshot_id == snapshot_id)
    ).all()
    reviews = conn.execute(
        select(snapshot_reviews.c.review_id).where(snapshot_reviews.c.snapshot_id == snapshot_id)
    ).all()
    return PublishedSnapshot(
        schema_version=row.schema_version,
        snapshot_id=row.snapshot_id,
        published_at=row.published_at,
        published_by=row.published_by,
        entries=tuple(
            SnapshotEntry(
                offer_id=entry.offer_id,
                structural_sha256=entry.structural_sha256,
                value_sha256=entry.value_sha256,
            )
            for entry in entries
        ),
        manifest_sha256=row.manifest_sha256,
        supersedes=row.supersedes,
        review_ids=tuple(item.review_id for item in reviews),
    )


def stored_provenance(record: Provenance) -> str:
    return provenance_id(record)

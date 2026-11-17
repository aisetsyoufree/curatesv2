"""Load publication inputs only from rows this process stored. Missing rows fail closed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.contracts.audit import (
    PublishedSnapshot,
    ReviewDecision,
    SnapshotEntry,
    WithdrawalEntry,
    snapshot_manifest_sha256,
)
from cu_rates.contracts.base import CONTRACT_VERSION, UtcDatetime
from cu_rates.contracts.enums import RunStatus
from cu_rates.contracts.evidence import ParserVersion, Provenance, RawArtifact
from cu_rates.contracts.fingerprints import OfferSnapshot, offer_fingerprints
from cu_rates.contracts.offers import NormalizedOffer, OfferCondition
from cu_rates.contracts.publication import (
    PriorPublication,
    PublicationContext,
    ValidationPolicy,
    ValidationResult,
    VerifiedScenarioCondition,
    VerifiedScenarioStatement,
)
from cu_rates.contracts.registry import (
    Institution,
    InstitutionSourceRelationship,
    ProductIdentity,
)
from cu_rates.contracts.scenarios import StatedBound
from cu_rates.contracts.sources import (
    ConditionalValidator,
    FetchProfile,
    FetchRun,
    NotModifiedObservation,
    RateSource,
    SourceApproval,
    SourceFingerprint,
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
    extraction_runs,
    fetch_profiles,
    fetch_runs,
    finding_evidence,
    finding_purposes,
    institution_source_relationships,
    institutions,
    not_modified_observations,
    product_findings,
    products,
    published_snapshots,
    rate_sources,
    review_decisions,
    snapshot_entries,
    snapshot_reviews,
    source_approvals,
    source_fingerprints,
    validation_policies,
    validation_results,
    verified_scenario_conditions,
    verified_scenario_statements,
    withdrawal_entries,
    withdrawal_lifts,
)

_FRESHNESS = timedelta(hours=71)


@dataclass(frozen=True)
class TrustedView:
    """The offer as the stored 304 claims it, plus a context built only from tables."""

    offer: NormalizedOffer
    context: PublicationContext


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
        _insert_provenance(conn, record, charter)


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
        conn.execute(
            validation_results.insert(),
            {
                "validation_id": (
                    f"{result.offer_id}.{result.rule_id}.{int(result.evaluated_at.timestamp())}"
                ),
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
                "statement_id": f"{statement.offer_id}.{statement.dimension.value}",
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


def insert_snapshot(conn: Connection, snapshot: PublishedSnapshot) -> None:
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
    conn: Connection, offer_id: str, evaluated_at: UtcDatetime
) -> TrustedView:
    """Rebuild one offer and its gate context from the operational tables.

    A 304 observation time is applied only when that observation row is stored.
    A snapshot that does not list this offer's current fingerprints is ignored.
    """
    offer = load_normalized_offer(conn, offer_id)
    institution_row = conn.execute(
        select(institutions).where(institutions.c.charter_number == offer.institution_charter)
    ).one()
    product_row = conn.execute(
        select(products).where(products.c.product_id == offer.product_id)
    ).one()
    relationship_row = conn.execute(
        select(institution_source_relationships).where(
            institution_source_relationships.c.institution_charter == offer.institution_charter,
            institution_source_relationships.c.source_id == offer.source_id,
            institution_source_relationships.c.product_id == offer.product_id,
        )
    ).one()
    source_row = conn.execute(
        select(rate_sources).where(rate_sources.c.source_id == offer.source_id)
    ).one()
    approval_rows = conn.execute(
        select(source_approvals)
        .where(source_approvals.c.source_id == offer.source_id)
        .order_by(source_approvals.c.decided_at.desc(), source_approvals.c.approval_id.desc())
    ).all()
    approval = _approval(approval_rows[0]) if approval_rows else None
    run_rows = conn.execute(
        select(fetch_runs).where(fetch_runs.c.source_id == offer.source_id)
    ).all()
    observation_rows = conn.execute(
        select(artifact_observations).where(
            artifact_observations.c.fetch_run_id.in_([row.run_id for row in run_rows] or [""])
        )
    ).all()
    ids_by_run: dict[str, list[str]] = {}
    for observation in observation_rows:
        ids_by_run.setdefault(str(observation.fetch_run_id), []).append(
            str(observation.artifact_id)
        )
    runs = tuple(_run(row, tuple(ids_by_run.get(str(row.run_id), ()))) for row in run_rows)
    succeeded = [run for run in runs if run.status is RunStatus.SUCCEEDED]
    basis = min(succeeded, key=lambda run: run.started_at) if succeeded else None
    profile = None
    if basis is not None:
        profile_row = conn.execute(
            select(fetch_profiles).where(fetch_profiles.c.profile_id == basis.profile_id)
        ).one_or_none()
        if profile_row is not None:
            profile = FetchProfile(
                schema_version=profile_row.schema_version,
                profile_id=profile_row.profile_id,
                source_id=profile_row.source_id,
                method=profile_row.method,
                min_interval_seconds=profile_row.min_interval_seconds,
                timeout_seconds=profile_row.timeout_seconds,
            )
    artifacts = tuple(
        RawArtifact(
            schema_version=row.schema_version,
            artifact_id=row.artifact_id,
            media_type=row.media_type,
            source_url=row.source_url,
            fetched_at=row.fetched_at,
            http_status=row.http_status,
            byte_length=blob_length(conn, row.artifact_id),
            fetch_run_id=row.fetch_run_id,
        )
        for row in observation_rows
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
                not_modified_observations.c.source_id == offer.source_id
            )
        ).all()
    )
    fingerprints = tuple(
        _fingerprint(row)
        for row in conn.execute(
            select(source_fingerprints).where(source_fingerprints.c.source_id == offer.source_id)
        ).all()
    )
    cited = {item.artifact_id for item in offer.iter_provenance()}
    claimed = [item for item in reobservations if item.original_artifact_id in cited]
    original_observed = offer.observed_at
    if claimed and offer.valid_until is not None:
        newest = max(claimed, key=lambda item: item.observed_at)
        span = offer.valid_until - original_observed
        if span <= _FRESHNESS:
            offer = offer.model_copy(
                update={
                    "observed_at": newest.observed_at,
                    "valid_until": newest.observed_at + span,
                }
            )
    validations = tuple(
        result
        for result in (
            _validation(row)
            for row in conn.execute(
                select(validation_results).where(validation_results.c.offer_id == offer_id)
            ).all()
        )
        if result.evaluated_at >= offer.observed_at
    )
    policy_row = conn.execute(
        select(validation_policies).order_by(validation_policies.c.approved_at.desc())
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
    product_review = _subject_review(conn, "product", offer.product_id)
    statements = _statements(conn, offer.offer_id)
    conditions = _conditions(conn, offer.offer_id)
    withdrawals = _withdrawals(conn)
    prior = _prior(conn, offer, original_observed)
    context = PublicationContext.model_validate(
        {
            "institution": _institution(institution_row),
            "product": _product(product_row),
            "relationship": _relationship(relationship_row),
            "source": _source(source_row),
            "source_approval": approval,
            "validation_policy": policy,
            "approved_parsers": ()
            if approval is None or approval.adapter is None
            else (approval.adapter,),
            "known_artifacts": artifacts,
            "fetch_runs": runs,
            "reobservations": reobservations,
            "approved_profile": profile,
            "source_fingerprints": fingerprints,
            "verified_scenario_statements": statements,
            "verified_scenario_conditions": conditions,
            "validations": validations,
            "prior": prior,
            "product_review": product_review,
            "withdrawals": withdrawals,
            "evaluated_at": evaluated_at,
        }
    )
    return TrustedView(offer, context)


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


def _subject_review(conn: Connection, subject: str, subject_id: str) -> ReviewDecision | None:
    row = conn.execute(
        select(review_decisions.c.review_id)
        .where(
            review_decisions.c.subject == subject,
            review_decisions.c.subject_id == subject_id,
        )
        .order_by(review_decisions.c.decided_at.desc())
    ).first()
    if row is None:
        return None
    return load_review_decision(conn, str(row.review_id))


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


def _prior(
    conn: Connection, offer: NormalizedOffer, original_observed: datetime
) -> PriorPublication | None:
    fingerprints = offer_fingerprints(offer)
    rows = conn.execute(
        select(snapshot_entries, published_snapshots)
        .join(
            published_snapshots,
            published_snapshots.c.snapshot_id == snapshot_entries.c.snapshot_id,
        )
        .where(snapshot_entries.c.offer_id == offer.offer_id)
        .order_by(published_snapshots.c.published_at.desc())
    ).all()
    for row in rows:
        if (
            row.structural_sha256 != fingerprints.structural
            or row.value_sha256 != fingerprints.value
        ):
            continue
        review_row = conn.execute(
            select(snapshot_reviews.c.review_id).where(
                snapshot_reviews.c.snapshot_id == row.snapshot_id
            )
        ).first()
        review = (
            None if review_row is None else load_review_decision(conn, str(review_row.review_id))
        )
        snapshot = _snapshot(conn, str(row.snapshot_id))
        try:
            return PriorPublication(
                snapshot=OfferSnapshot.from_offer(offer),
                published=snapshot,
                review=review,
                last_value_change_at=original_observed,
            )
        except ValueError:
            return None
    return None


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

"""Write and read operational rows from validated contract objects.

Typed writes re-validate the contract before any insert. A mapping that would
not be a legal ``NormalizedOffer`` or ``ReviewDecision`` never reaches SQLite.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.contracts import (
    FetchProfile,
    FetchRun,
    Institution,
    InstitutionIdentityReview,
    InstitutionSourceRelationship,
    NormalizedOffer,
    ProductIdentity,
    Provenance,
    RateSource,
    RawArtifact,
    ReviewDecision,
    ReviewSubject,
    ServiceProvider,
    SourcedPoints,
    SourcedRate,
)
from cu_rates.db.schema import (
    artifact_blobs,
    artifact_observations,
    fetch_profiles,
    fetch_runs,
    institution_identity_evidence,
    institution_identity_reviews,
    institution_source_relationships,
    institutions,
    normalized_offers,
    offer_evidence,
    products,
    rate_sources,
    review_decisions,
    review_evidence,
    service_providers,
)
from cu_rates.db.schema import (
    provenance as provenance_table,
)


def provenance_id(record: Provenance) -> str:
    """Stable id for one provenance value. The contract itself has no id."""
    payload = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def persist_normalized_offer(
    conn: Connection, offer: NormalizedOffer | Mapping[str, Any]
) -> NormalizedOffer:
    """Validate ``offer`` against the contract, then insert it and its evidence."""
    checked = NormalizedOffer.model_validate(
        offer.model_dump() if isinstance(offer, NormalizedOffer) else offer
    )
    _write_offer(conn, checked)
    return checked


def persist_review_decision(
    conn: Connection,
    review: ReviewDecision | Mapping[str, Any],
    *,
    institution_charter: int,
) -> ReviewDecision:
    """Validate ``review`` against the contract, then insert it and its evidence."""
    checked = ReviewDecision.model_validate(
        review.model_dump() if isinstance(review, ReviewDecision) else review
    )
    _write_review(conn, checked, institution_charter)
    return checked


def persist_chain(
    conn: Connection,
    *,
    institution: Institution,
    provider: ServiceProvider,
    source: RateSource,
    product: ProductIdentity,
    relationship: InstitutionSourceRelationship,
    profile: FetchProfile,
    run: FetchRun,
    artifact: RawArtifact,
    offer: NormalizedOffer | Mapping[str, Any],
    review: ReviewDecision | Mapping[str, Any],
) -> tuple[NormalizedOffer, ReviewDecision]:
    """Insert one provider-backed chain, validating every contract object first."""
    institution = Institution.model_validate(institution.model_dump())
    provider = ServiceProvider.model_validate(provider.model_dump())
    source = RateSource.model_validate(source.model_dump())
    product = ProductIdentity.model_validate(product.model_dump())
    relationship = InstitutionSourceRelationship.model_validate(relationship.model_dump())
    profile = FetchProfile.model_validate(profile.model_dump())
    run = FetchRun.model_validate(run.model_dump())
    artifact = RawArtifact.model_validate(artifact.model_dump())
    checked_offer = NormalizedOffer.model_validate(
        offer.model_dump() if isinstance(offer, NormalizedOffer) else offer
    )
    checked_review = ReviewDecision.model_validate(
        review.model_dump() if isinstance(review, ReviewDecision) else review
    )
    _require_consistent_chain(
        institution,
        provider,
        source,
        product,
        relationship,
        profile,
        run,
        artifact,
        checked_offer,
        checked_review,
    )
    _insert_institution(conn, institution)
    _insert_provider(conn, provider)
    _insert_source(conn, source)
    _insert_product(conn, product)
    _insert_relationship(conn, relationship)
    _insert_profile(conn, profile)
    _insert_run(conn, run)
    _insert_artifact(conn, artifact)
    _write_offer(conn, checked_offer)
    _write_review(conn, checked_review, institution.charter_number)
    return checked_offer, checked_review


def load_normalized_offer(conn: Connection, offer_id: str) -> NormalizedOffer:
    """Rebuild an offer, including field paths, from the evidence chain."""
    row = conn.execute(
        select(normalized_offers).where(normalized_offers.c.offer_id == offer_id)
    ).one()
    evidence = conn.execute(
        select(offer_evidence)
        .where(offer_evidence.c.offer_id == offer_id)
        .order_by(offer_evidence.c.ordinal)
    ).all()
    needed = {item.provenance_id for item in evidence}
    for column in (
        row.note_rate_provenance_id,
        row.apr_provenance_id,
        row.points_provenance_id,
    ):
        if column is not None:
            needed.add(column)
    loaded = _load_provenance(conn, needed)

    def sourced(value: object, provenance_key: str | None) -> dict[str, Any] | None:
        if value is None or provenance_key is None:
            return None
        return {"value": value, "provenance": loaded[provenance_key].model_dump()}

    supporting = [
        loaded[item.provenance_id].model_dump() for item in evidence if item.role == "supporting"
    ]
    fields = [
        {"path": item.field_path, "provenance": loaded[item.provenance_id].model_dump()}
        for item in evidence
        if item.role == "field"
    ]
    return NormalizedOffer.model_validate(
        {
            "schema_version": row.schema_version,
            "offer_id": row.offer_id,
            "institution_charter": row.institution_charter,
            "product_id": row.product_id,
            "source_id": row.source_id,
            "status": row.status,
            "details": row.details_json,
            "scenario_id": row.scenario_id,
            "scenario_matches": row.scenario_matches_json,
            "note_rate": sourced(row.note_rate, row.note_rate_provenance_id),
            "apr": sourced(row.apr, row.apr_provenance_id),
            "points": sourced(row.points, row.points_provenance_id),
            "conditions": row.conditions_json,
            "tiers": row.tiers_json,
            "headline_is_as_low_as": bool(row.headline_is_as_low_as),
            "effective_at": row.effective_at,
            "observed_at": row.observed_at,
            "published_at": row.published_at,
            "valid_until": row.valid_until,
            "supporting_evidence": supporting,
            "field_provenance": fields,
        }
    )


def load_review_decision(conn: Connection, review_id: str) -> ReviewDecision:
    """Rebuild a review decision, including the provenance it cited."""
    row = conn.execute(
        select(review_decisions).where(review_decisions.c.review_id == review_id)
    ).one()
    evidence_rows = conn.execute(
        select(review_evidence)
        .where(review_evidence.c.review_id == review_id)
        .order_by(review_evidence.c.ordinal)
    ).all()
    loaded = _load_provenance(conn, {item.provenance_id for item in evidence_rows})
    return ReviewDecision.model_validate(
        {
            "schema_version": row.schema_version,
            "review_id": row.review_id,
            "subject": row.subject,
            "subject_id": row.subject_id,
            "verdict": row.verdict,
            "actor": row.actor,
            "decided_at": row.decided_at,
            "rationale": row.rationale,
            "evidence": [loaded[item.provenance_id].model_dump() for item in evidence_rows],
            "reviewed_fingerprints": row.fingerprints_json,
            "reviewed_product_sha256": row.reviewed_product_sha256,
            "reviewed_evidence_sha256": row.reviewed_evidence_sha256,
        }
    )


def persist_identity_review(
    conn: Connection, review: InstitutionIdentityReview | Mapping[str, Any]
) -> InstitutionIdentityReview:
    """Validate, then insert an identity review and the provenance it cites.

    The cited artifact must already be registered (``provenance.artifact_id`` is a foreign
    key), and the institution must exist: a review of nothing, or backed by nothing stored,
    is refused by the database rather than accepted on trust.
    """
    checked = InstitutionIdentityReview.model_validate(
        review.model_dump() if isinstance(review, InstitutionIdentityReview) else review
    )
    identifiers = [
        _insert_provenance(conn, record, checked.institution_charter) for record in checked.evidence
    ]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("an identity review cannot cite the same evidence twice")
    owners = {
        row.provenance_id: row.institution_charter
        for row in conn.execute(
            select(provenance_table.c.provenance_id, provenance_table.c.institution_charter).where(
                provenance_table.c.provenance_id.in_(identifiers)
            )
        )
    }
    if any(owners.get(identifier) != checked.institution_charter for identifier in identifiers):
        raise ValueError("an identity review can only cite evidence recorded for its institution")
    conn.execute(
        institution_identity_reviews.insert(),
        {
            "review_id": checked.review_id,
            "schema_version": checked.schema_version,
            "institution_charter": checked.institution_charter,
            "legal_name": checked.legal_name,
            "identity_confirmed": int(checked.identity_confirmed),
            "massachusetts_scope_confirmed": int(checked.massachusetts_scope_confirmed),
            "reviewer": checked.reviewer,
            "decided_at": checked.decided_at,
            "rationale": checked.rationale,
        },
    )
    conn.execute(
        institution_identity_evidence.insert(),
        [
            {
                "review_id": checked.review_id,
                "provenance_id": identifier,
                "institution_charter": checked.institution_charter,
                "ordinal": ordinal,
            }
            for ordinal, identifier in enumerate(identifiers)
        ],
    )
    return checked


def load_identity_reviews(
    conn: Connection, institution_charter: int
) -> tuple[InstitutionIdentityReview, ...]:
    """Every identity review of one institution, newest decision first."""
    rows = conn.execute(
        select(institution_identity_reviews)
        .where(institution_identity_reviews.c.institution_charter == institution_charter)
        .order_by(
            institution_identity_reviews.c.decided_at.desc(),
            institution_identity_reviews.c.review_id,
        )
    ).all()
    reviews: list[InstitutionIdentityReview] = []
    for row in rows:
        evidence_rows = conn.execute(
            select(institution_identity_evidence)
            .where(institution_identity_evidence.c.review_id == row.review_id)
            .order_by(institution_identity_evidence.c.ordinal)
        ).all()
        if any(item.institution_charter != row.institution_charter for item in evidence_rows):
            continue  # evidence recorded for another institution: the review is not trusted
        loaded = _load_provenance(conn, {item.provenance_id for item in evidence_rows})
        reviews.append(
            InstitutionIdentityReview.model_validate(
                {
                    "schema_version": row.schema_version,
                    "review_id": row.review_id,
                    "institution_charter": row.institution_charter,
                    "legal_name": row.legal_name,
                    "identity_confirmed": bool(row.identity_confirmed),
                    "massachusetts_scope_confirmed": bool(row.massachusetts_scope_confirmed),
                    "evidence": [loaded[i.provenance_id].model_dump() for i in evidence_rows],
                    "reviewer": row.reviewer,
                    "decided_at": row.decided_at,
                    "rationale": row.rationale,
                }
            )
        )
    return tuple(reviews)


def _require_consistent_chain(
    institution: Institution,
    provider: ServiceProvider,
    source: RateSource,
    product: ProductIdentity,
    relationship: InstitutionSourceRelationship,
    profile: FetchProfile,
    run: FetchRun,
    artifact: RawArtifact,
    offer: NormalizedOffer,
    review: ReviewDecision,
) -> None:
    charter = institution.charter_number
    if (
        product.institution_charter != charter
        or relationship.institution_charter != charter
        or offer.institution_charter != charter
    ):
        raise ValueError("chain records must share one institution")
    if relationship.provider_id != provider.provider_id or relationship.kind.value == "first_party":
        raise ValueError("chain must be provider-backed")
    if (
        relationship.source_id != source.source_id
        or relationship.product_id != product.product_id
        or profile.source_id != source.source_id
        or run.source_id != source.source_id
        or run.profile_id != profile.profile_id
        or offer.source_id != source.source_id
        or offer.product_id != product.product_id
    ):
        raise ValueError("chain source, product, profile, and run must agree")
    if artifact.fetch_run_id != run.run_id or artifact.artifact_id not in run.artifact_ids:
        raise ValueError("artifact must belong to this fetch run")
    if artifact.byte_length < 0:
        raise ValueError("artifact size must be non-negative")
    cited = tuple(offer.iter_provenance()) + tuple(review.evidence)
    if any(item.artifact_id != artifact.artifact_id for item in cited):
        raise ValueError("evidence must cite this chain's artifact")
    if review.subject is not ReviewSubject.OFFER or review.subject_id != offer.offer_id:
        raise ValueError("review must decide this offer")


def _exists(conn: Connection, column: Any, value: object) -> bool:
    return conn.execute(select(column).where(column == value)).first() is not None


def _insert_institution(conn: Connection, institution: Institution) -> None:
    if _exists(conn, institutions.c.charter_number, institution.charter_number):
        return
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


def _insert_provider(conn: Connection, provider: ServiceProvider) -> None:
    if _exists(conn, service_providers.c.provider_id, provider.provider_id):
        return
    conn.execute(
        service_providers.insert(),
        {
            "provider_id": provider.provider_id,
            "schema_version": provider.schema_version,
            "name": provider.name,
            "kind": provider.kind.value,
        },
    )


def _insert_source(conn: Connection, source: RateSource) -> None:
    if _exists(conn, rate_sources.c.source_id, source.source_id):
        return
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


def _insert_product(conn: Connection, product: ProductIdentity) -> None:
    if _exists(conn, products.c.product_id, product.product_id):
        return
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


def _insert_relationship(conn: Connection, relationship: InstitutionSourceRelationship) -> None:
    if _exists(
        conn,
        institution_source_relationships.c.relationship_id,
        relationship.relationship_id,
    ):
        return
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


def _insert_profile(conn: Connection, profile: FetchProfile) -> None:
    if _exists(conn, fetch_profiles.c.profile_id, profile.profile_id):
        return
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


def _insert_run(conn: Connection, run: FetchRun) -> None:
    if _exists(conn, fetch_runs.c.run_id, run.run_id):
        return
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


def _insert_artifact(conn: Connection, artifact: RawArtifact) -> None:
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
        raise ValueError("blob size does not match the stored body")
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


def _remember(conn: Connection, offer: NormalizedOffer, record: Provenance) -> str:
    return _insert_provenance(conn, record, offer.institution_charter)


def _evidence(
    offer: NormalizedOffer, identifier: str, role: str, field_path: str, ordinal: int
) -> dict[str, object]:
    return {
        "offer_id": offer.offer_id,
        "provenance_id": identifier,
        "role": role,
        "field_path": field_path,
        "institution_charter": offer.institution_charter,
        "ordinal": ordinal,
    }


def _write_offer(conn: Connection, offer: NormalizedOffer) -> None:
    note_id = (
        _remember(conn, offer, offer.note_rate.provenance) if offer.note_rate is not None else None
    )
    apr_id = _remember(conn, offer, offer.apr.provenance) if offer.apr is not None else None
    points_id = (
        _remember(conn, offer, offer.points.provenance) if offer.points is not None else None
    )
    conn.execute(
        normalized_offers.insert(),
        {
            "offer_id": offer.offer_id,
            "schema_version": offer.schema_version,
            "institution_charter": offer.institution_charter,
            "product_id": offer.product_id,
            "source_id": offer.source_id,
            "status": offer.status.value,
            "details_json": offer.details.model_dump(mode="json"),
            "scenario_id": None if offer.scenario_id is None else offer.scenario_id.value,
            "scenario_matches_json": [
                item.model_dump(mode="json") for item in offer.scenario_matches
            ],
            "note_rate": None if offer.note_rate is None else offer.note_rate.value,
            "apr": None if offer.apr is None else offer.apr.value,
            "points": None if offer.points is None else offer.points.value,
            "note_rate_provenance_id": note_id,
            "apr_provenance_id": apr_id,
            "points_provenance_id": points_id,
            "conditions_json": [item.model_dump(mode="json") for item in offer.conditions],
            "tiers_json": [item.model_dump(mode="json") for item in offer.tiers],
            "headline_is_as_low_as": int(offer.headline_is_as_low_as),
            "effective_at": offer.effective_at,
            "observed_at": offer.observed_at,
            "published_at": offer.published_at,
            "valid_until": offer.valid_until,
        },
    )
    ordinal = 0
    rows: list[dict[str, object]] = []
    for record in offer.supporting_evidence:
        rows.append(_evidence(offer, _remember(conn, offer, record), "supporting", "", ordinal))
        ordinal += 1
    for role, sourced in (
        ("note_rate", offer.note_rate),
        ("apr", offer.apr),
        ("points", offer.points),
    ):
        if isinstance(sourced, SourcedRate | SourcedPoints):
            rows.append(
                _evidence(offer, _remember(conn, offer, sourced.provenance), role, "", ordinal)
            )
            ordinal += 1
    for field in offer.field_provenance:
        rows.append(
            _evidence(offer, _remember(conn, offer, field.provenance), "field", field.path, ordinal)
        )
        ordinal += 1
    if rows:
        conn.execute(offer_evidence.insert(), rows)


def _write_review(conn: Connection, review: ReviewDecision, institution_charter: int) -> None:
    conn.execute(
        review_decisions.insert(),
        {
            "review_id": review.review_id,
            "schema_version": review.schema_version,
            "subject": review.subject.value,
            "subject_id": review.subject_id,
            "verdict": review.verdict.value,
            "actor": review.actor,
            "decided_at": review.decided_at,
            "rationale": review.rationale,
            "fingerprints_json": None
            if review.reviewed_fingerprints is None
            else review.reviewed_fingerprints.model_dump(mode="json"),
            "reviewed_product_sha256": review.reviewed_product_sha256,
            "reviewed_evidence_sha256": review.reviewed_evidence_sha256,
        },
    )
    if review.evidence:
        conn.execute(
            review_evidence.insert(),
            [
                {
                    "review_id": review.review_id,
                    "provenance_id": _insert_provenance(conn, record, institution_charter),
                    "ordinal": ordinal,
                }
                for ordinal, record in enumerate(review.evidence)
            ],
        )


def _insert_provenance(conn: Connection, record: Provenance, institution_charter: int) -> str:
    identifier = provenance_id(record)
    if _exists(conn, provenance_table.c.provenance_id, identifier):
        return identifier
    extraction = record.extraction
    conn.execute(
        provenance_table.insert(),
        {
            "provenance_id": identifier,
            "institution_charter": institution_charter,
            "artifact_id": record.artifact_id,
            "locator_kind": record.locator.kind,
            "locator_json": record.locator.model_dump(mode="json"),
            "source_text": record.source_text,
            "git_sha": extraction.git_sha,
            "parser_name": extraction.parser.name,
            "parser_version": extraction.parser.version,
            "configuration_sha256": extraction.configuration_sha256,
            "normalization_version": extraction.normalization_version,
            "rule_id": extraction.rule_id,
        },
    )
    return identifier


def _load_provenance(conn: Connection, identifiers: set[str]) -> dict[str, Provenance]:
    if not identifiers:
        return {}
    rows = conn.execute(
        select(provenance_table).where(provenance_table.c.provenance_id.in_(identifiers))
    ).all()
    found: dict[str, Provenance] = {}
    for row in rows:
        found[row.provenance_id] = Provenance.model_validate(
            {
                "artifact_id": row.artifact_id,
                "locator": row.locator_json,
                "source_text": row.source_text,
                "extraction": {
                    "git_sha": row.git_sha,
                    "parser": {"name": row.parser_name, "version": row.parser_version},
                    "configuration_sha256": row.configuration_sha256,
                    "normalization_version": row.normalization_version,
                    "rule_id": row.rule_id,
                },
            }
        )
    return found

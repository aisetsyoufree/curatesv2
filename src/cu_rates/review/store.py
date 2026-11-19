"""Queue projection and content-bound decisions using existing audit persistence.

Source requests are NOT approvals. Routing code calls enqueue_source_review in its
own transaction; only an explicit operator decision writes a SourceApproval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select

from cu_rates.contracts import Provenance, ReviewDecision, ReviewSubject, ReviewVerdict
from cu_rates.contracts.fingerprints import product_content_sha256
from cu_rates.contracts.sources import FetchProfile, SourceApproval, profile_sha256
from cu_rates.db import schema as s
from cu_rates.db.records import (
    _insert_provenance,
    load_normalized_offer,
    persist_review_decision,
)
from cu_rates.pipeline.trusted import _product, insert_approval

KINDS = ("product", "source", "quarantined", "correction")


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Item:
    kind: str
    id: str
    since: datetime | None
    summary: str
    sha256: str
    detail: dict[str, Any]
    evidence: tuple[Provenance, ...] = ()
    decided: bool = False


def _rows(conn: Connection, statement: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(statement).mappings()]


def _source_state(conn: Connection, source_id: str, profile_id: str) -> dict[str, Any]:
    source = _rows(conn, select(s.rate_sources).where(s.rate_sources.c.source_id == source_id))
    profiles = _rows(
        conn, select(s.fetch_profiles).where(s.fetch_profiles.c.profile_id == profile_id)
    )
    relationships = _rows(
        conn,
        select(s.institution_source_relationships)
        .where(s.institution_source_relationships.c.source_id == source_id)
        .order_by(s.institution_source_relationships.c.relationship_id),
    )
    charters = {row["institution_charter"] for row in relationships}
    institutions = _rows(
        conn,
        select(s.institutions)
        .where(s.institutions.c.charter_number.in_(charters))
        .order_by(s.institutions.c.charter_number),
    )
    decisions = _rows(
        conn,
        select(s.source_approvals)
        .where(s.source_approvals.c.source_id == source_id)
        .order_by(
            s.source_approvals.c.decided_at.desc(),
            s.source_approvals.c.decision.desc(),
            s.source_approvals.c.approval_id.desc(),
        )
        .limit(1),
    )
    return dict(
        source=source,
        profiles=profiles,
        relationships=relationships,
        institutions=institutions,
        prior_decision=decisions,
    )


def enqueue_source_review(
    conn: Connection,
    proposal: SourceApproval,
    *,
    charter: int,
    profile_id: str,
    reason: str,
    at: datetime,
) -> str:
    """Route uncertainty to the owner, idempotently; never persist a proposed decision.

    The SourceApproval is a validated template. Its id/actor/time/verdict/rationale
    are discarded: the CLI supplies them only when the owner explicitly decides.
    Evidence must already reference registered artifacts for this institution.
    """
    proposal = SourceApproval.model_validate(proposal.model_dump())
    state = _source_state(conn, proposal.source_id, profile_id)
    if not reason.strip() or not state["source"] or not state["profiles"]:
        raise ValueError("source, profile and routing reason are required")
    profile = FetchProfile.model_validate(state["profiles"][0])
    if (
        profile.source_id != proposal.source_id
        or profile_sha256(profile) != proposal.approved_profile_sha256
        or state["source"][0]["url"] != proposal.source_url
        or not any(r["institution_charter"] == charter for r in state["relationships"])
    ):
        raise ValueError("proposal must match the stored source, institution and profile")
    for evidence in proposal.evidence:
        key = _insert_provenance(conn, evidence, charter)
        owner = conn.execute(
            select(s.provenance.c.institution_charter).where(s.provenance.c.provenance_id == key)
        ).scalar_one()
        if owner != charter:
            raise ValueError("source evidence belongs to another institution")
    payload = proposal.model_dump(
        mode="json", exclude={"approval_id", "decision", "actor", "decided_at", "rationale"}
    )
    sha = digest(dict(proposal=payload, state=state, charter=charter, reason=reason.strip()))
    request_id = f"source-review-{sha}"
    if not conn.execute(
        select(s.source_review_requests.c.request_id).where(
            s.source_review_requests.c.request_id == request_id
        )
    ).first():
        conn.execute(
            s.source_review_requests.insert().values(
                request_id=request_id,
                source_id=proposal.source_id,
                institution_charter=charter,
                profile_id=profile_id,
                requested_at=at,
                reason=reason.strip(),
                proposal_json=payload,
                content_sha256=sha,
            )
        )
    return request_id


def product_item(conn: Connection, product_id: str) -> Item:
    row = conn.execute(select(s.products).where(s.products.c.product_id == product_id)).one()
    product = _product(row)  # The SAME reconstruction and digest used by the gate.
    offers = [
        load_normalized_offer(conn, key)
        for key in conn.execute(
            select(s.normalized_offers.c.offer_id)
            .where(s.normalized_offers.c.product_id == product_id)
            .order_by(s.normalized_offers.c.offer_id)
        ).scalars()
    ]
    evidence = tuple(dict.fromkeys(p for offer in offers for p in offer.iter_provenance()))
    product_sha = product_content_sha256(product)
    reviews = _rows(
        conn,
        select(s.review_decisions)
        .where(
            s.review_decisions.c.subject == "product", s.review_decisions.c.subject_id == product_id
        )
        .order_by(
            s.review_decisions.c.decided_at.desc(),
            s.review_decisions.c.verdict.desc(),
            s.review_decisions.c.review_id.desc(),
        ),
    )
    # An unchanged rejection is a decision too; never let a retry turn it into approval.
    decided = bool(reviews and reviews[0]["reviewed_product_sha256"] == product_sha)
    detail = dict(
        product=product.model_dump(mode="json"),
        reviewed_product_sha256=product_sha,
        evidence=[p.model_dump(mode="json") for p in evidence],
        captures=_rows(
            conn,
            select(s.artifact_observations)
            .where(s.artifact_observations.c.artifact_id.in_({p.artifact_id for p in evidence}))
            .order_by(s.artifact_observations.c.fetch_run_id, s.artifact_observations.c.source_url),
        ),
        latest_decision=reviews[:1],
        approves="Only this product definition; no source or offer approval/publication.",
    )
    return Item(
        "product",
        product_id,
        min((o.observed_at for o in offers), default=None),
        f"{product.institution_charter}: {product.name}",
        digest(detail),
        detail,
        evidence,
        decided,
    )


def source_item(conn: Connection, request_id: str) -> Item:
    row = conn.execute(
        select(s.source_review_requests).where(s.source_review_requests.c.request_id == request_id)
    ).one()
    state = _source_state(conn, row.source_id, row.profile_id)
    decided = bool(
        conn.execute(
            select(s.source_approvals.c.approval_id).where(
                s.source_approvals.c.approval_id == request_id
            )
        ).first()
    )
    current = digest(
        dict(
            proposal=row.proposal_json,
            state=state,
            charter=row.institution_charter,
            reason=row.reason,
        )
    )
    detail = dict(
        request_id=request_id,
        charter=row.institution_charter,
        proposal=row.proposal_json,
        state=state,
        reason=row.reason,
        stale=current != row.content_sha256,
        approves="Exact source URL, policy, profile, layout, adapter and origins in proposal; "
        "does not approve identity, robots/terms, restore a denylist, or publish.",
    )
    return Item(
        "source",
        request_id,
        row.requested_at,
        f"{row.source_id}: {row.reason}"
        + (" [STALE: route a new request]" if detail["stale"] else ""),
        current,
        detail,
        tuple(Provenance.model_validate(p) for p in row.proposal_json["evidence"]),
        decided,
    )


def quarantined_item(conn: Connection, offer_id: str) -> Item:
    offer = load_normalized_offer(conn, offer_id)
    failures = _rows(
        conn,
        select(s.validation_results)
        .where(s.validation_results.c.offer_id == offer_id, s.validation_results.c.passed == 0)
        .order_by(s.validation_results.c.evaluated_at, s.validation_results.c.validation_id),
    )
    reviews = _rows(
        conn,
        select(s.review_decisions).where(
            s.review_decisions.c.subject == "offer",
            s.review_decisions.c.subject_id == offer_id,
            s.review_decisions.c.verdict != "approved",
        ),
    )
    reasons = [f"{r['rule_id']}: {r['detail']}" for r in failures]
    reasons += [r["rationale"] for r in reviews]
    reason = (
        "; ".join(reasons)
        or "quarantined lifecycle; no persisted reason (confidence tiers not yet implemented)"
    )
    detail = dict(
        offer=offer.model_dump(mode="json"),
        failures=failures,
        reviews=reviews,
        rerun="manual: re-run owning adapter/validation; console cannot release quarantine",
    )
    return Item(
        "quarantined",
        offer_id,
        offer.observed_at,
        reason + "; rerun: manual",
        digest(detail),
        detail,
    )


def operational_items(conn: Connection) -> list[Item]:
    items = [
        product_item(conn, key)
        for key in conn.execute(
            select(s.products.c.product_id).order_by(s.products.c.product_id)
        ).scalars()
    ]
    items += [
        source_item(conn, key)
        for key in conn.execute(
            select(s.source_review_requests.c.request_id).order_by(
                s.source_review_requests.c.requested_at, s.source_review_requests.c.request_id
            )
        ).scalars()
    ]
    items += [
        quarantined_item(conn, key)
        for key in conn.execute(
            select(s.normalized_offers.c.offer_id)
            .where(s.normalized_offers.c.status == "quarantined")
            .order_by(s.normalized_offers.c.observed_at, s.normalized_offers.c.offer_id)
        ).scalars()
    ]
    return [item for item in items if not item.decided]


def decide(
    conn: Connection,
    kind: str,
    identifier: str,
    *,
    sha256: str,
    actor: str,
    note: str,
    approve: bool,
    at: datetime,
) -> str:
    """Caller must hold BEGIN IMMEDIATE through reload, CAS and append."""
    if not actor.strip() or not note.strip():
        raise ValueError("actor and note must be nonempty")
    if kind not in {"product", "source"}:
        raise ValueError("read-only kind; use cu_rates.intake for correction decisions")
    item = product_item(conn, identifier) if kind == "product" else source_item(conn, identifier)
    if item.decided:
        raise ValueError("already decided; refusing another decision")
    if sha256 != item.sha256:
        raise ValueError("content hash changed; run show and review again")
    verdict = ReviewVerdict.APPROVED if approve else ReviewVerdict.REJECTED
    if kind == "product":
        review_id = "console-product-" + digest(
            [identifier, item.detail["reviewed_product_sha256"]]
        )
        persist_review_decision(
            conn,
            ReviewDecision(
                schema_version="1.0",
                review_id=review_id,
                subject=ReviewSubject.PRODUCT,
                subject_id=identifier,
                verdict=verdict,
                actor=actor,
                decided_at=at,
                rationale=note,
                evidence=item.evidence,
                reviewed_product_sha256=item.detail["reviewed_product_sha256"],
            ),
            institution_charter=item.detail["product"]["institution_charter"],
        )
        return review_id
    if item.detail["stale"] and approve:
        raise ValueError("stale source proposal; route a new request for current content")
    row = conn.execute(
        select(s.source_review_requests).where(s.source_review_requests.c.request_id == identifier)
    ).one()
    insert_approval(
        conn,
        SourceApproval.model_validate(
            dict(
                row.proposal_json,
                approval_id=identifier,
                decision=verdict,
                actor=actor,
                decided_at=at,
                rationale=note,
            )
        ),
        row.institution_charter,
    )
    return identifier

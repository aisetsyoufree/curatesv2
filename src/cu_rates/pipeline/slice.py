"""One synthetic credit union, from fixture bytes to a publication decision."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.audit import (
    PublishedSnapshot,
    ReviewDecision,
    SnapshotEntry,
    snapshot_manifest_sha256,
)
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConformingStatus,
    EvidenceKind,
    FetchMethod,
    GovernmentGuaranty,
    InstitutionStatus,
    OfferStatus,
    OutOfScopeProduct,
    ProductFamily,
    ProductLifecycle,
    ProductScope,
    RateAvailability,
    RateType,
    ReviewSubject,
    ReviewVerdict,
    RunStatus,
    ScenarioDimension,
    SourceRelationshipKind,
)
from cu_rates.contracts.evidence import HtmlLocator, Provenance
from cu_rates.contracts.fingerprints import (
    offer_evidence_sha256,
    offer_fingerprints,
    product_content_sha256,
)
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import PublicationDecision, evaluate_publication
from cu_rates.contracts.registry import Institution, InstitutionSourceRelationship, ProductIdentity
from cu_rates.contracts.scenarios import StatedBound, statement_matches_excerpt
from cu_rates.contracts.sources import (
    FetchProfile,
    RateSource,
    SourceApproval,
    SourceFingerprint,
    profile_sha256,
)
from cu_rates.contracts.taxonomy import TAXONOMY_VERSION, ProductFinding
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.fetch_worker import FetchApproval, FetchWorker, TermsReview
from cu_rates.normalize import OfferContext, ScenarioCheck, normalize_offer
from cu_rates.normalize._values import Range, comparison_statement
from cu_rates.pipeline.extract import (
    FIXTURES,
    SLICE_PARSER,
    FixtureExtraction,
    NonOffer,
    extract_fixture,
    layout_sha256,
    resolve_text,
)
from cu_rates.pipeline.trusted import (
    TrustedView,
    insert_approval,
    insert_artifact,
    insert_fingerprint,
    insert_institution,
    insert_offer,
    insert_policy,
    insert_product,
    insert_profile,
    insert_relationship,
    insert_reobservation,
    insert_review,
    insert_run,
    insert_snapshot,
    insert_source,
    insert_statements,
    insert_validations,
    load_publication_context,
)
from cu_rates.pipeline.validate import validate_offer, validation_policy

REVIEWER = "example-reviewer"
CHARTER = 9000001
SOURCE_ID = "athol-style.mortgage"
PRODUCT_ID = "athol-style.purchase-fixed"
PROFILE_ID = "athol-style.http"
GIT_SHA = "0" * 40
_STRONG_ETAG = '"athol-slice-v1"'
_TARGETS = {
    "athol-style": CanonicalScenarioId.PURCHASE_30Y_FIXED,
    "worcester-style": CanonicalScenarioId.FIXED_HOME_EQUITY_10Y,
}


@dataclass
class SliceRun:
    database: Path
    store: ArtifactStore
    offers: dict[str, NormalizedOffer]
    findings: tuple[ProductFinding, ...]
    first: dict[str, PublicationDecision]
    reviewed_snapshot: PublishedSnapshot | None
    refresh: PublicationDecision | None


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.value = moment.timestamp()

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def moment(self) -> datetime:
        return datetime.fromtimestamp(self.value, UTC)

    def advance(self, seconds: float) -> None:
        self.value += seconds


def stated_bound(dimension: str, excerpt: str) -> StatedBound | None:
    """Type the excerpt. A statement the excerpt does not literally support is dropped."""
    try:
        value = comparison_statement(dimension, excerpt)
    except ValueError:
        return None
    if isinstance(value, Range) or value is None:
        return None
    if isinstance(value, Decimal):
        bound = StatedBound(exact=value)
    elif isinstance(value, bool):
        bound = StatedBound(token="yes" if value else "no")
    elif isinstance(value, RateType):
        bound = StatedBound(token=value.value)
    elif isinstance(value, str):
        bound = StatedBound(token=value)
    else:
        return None
    if not statement_matches_excerpt(ScenarioDimension(dimension), bound, excerpt):
        return None
    return bound


def classify_fixture(
    case_id: str, body: bytes
) -> tuple[tuple[NormalizedOffer, ...], tuple[NonOffer, ...]]:
    """Normalize one fixture file. Mortgage rows are the only offers this slice ranks."""
    artifact_id = hashlib.sha256(body).hexdigest()
    extracted = extract_fixture(
        case_id,
        body,
        artifact_id=artifact_id,
        extraction_run_id=f"slice-{case_id}",
        git_sha=GIT_SHA,
    )
    offers: list[NormalizedOffer] = []
    target = _TARGETS.get(case_id)
    for row in extracted.rows:
        result = normalize_offer(
            row.candidates,
            OfferContext(
                offer_id=f"{case_id}.{row.row_id}",
                institution_charter=CHARTER,
                product_id=PRODUCT_ID,
                source_id=SOURCE_ID,
                observed_at=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
                comparison_target=target,
            ),
        )
        if result.offer is not None:
            offers.append(result.offer)
    return tuple(offers), extracted.non_offers


def claim_text(store: ArtifactStore, provenance: Provenance) -> str:
    """Read the stored bytes and return the locator text, or raise if it does not match."""
    locator = provenance.locator
    if not isinstance(locator, HtmlLocator):
        raise ValueError("slice claims use an HTML locator")
    body = store.read_body(provenance.artifact_id)
    node = resolve_text(body, locator.selector)
    digest = hashlib.sha256(node.encode()).hexdigest()
    if digest != locator.normalized_text_sha256 or provenance.source_text not in node:
        raise ValueError("locator text is not in the stored artifact")
    if provenance.source_text.encode() not in body:
        raise ValueError("source text is not in the stored artifact")
    return provenance.source_text


def run_fixture(root: Path) -> SliceRun:
    """Fetch the Athol-style fixture offline and record the publication decisions."""
    root.mkdir(parents=True, exist_ok=True)
    database = root / "operational.sqlite"
    upgrade_database(database)
    store = ArtifactStore(root / "evidence")
    body = (FIXTURES / "athol_style_mortgage.html").read_bytes()
    opened = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    clock = _Clock(opened)
    profile = FetchProfile(
        schema_version=CONTRACT_VERSION,
        profile_id=PROFILE_ID,
        source_id=SOURCE_ID,
        method=FetchMethod.HTTP_GET,
        min_interval_seconds=60,
        timeout_seconds=10,
    )
    source = RateSource(
        schema_version=CONTRACT_VERSION,
        source_id=SOURCE_ID,
        url="https://athol-style.example/mortgage-rates",
        evidence_kind=EvidenceKind.HTML,
        description="Synthetic Athol-style mortgage table",
    )
    approval = SourceApproval(
        schema_version=CONTRACT_VERSION,
        approval_id="athol-style.approval",
        source_id=SOURCE_ID,
        source_url=source.url,
        decision=ReviewVerdict.APPROVED,
        actor=REVIEWER,
        decided_at=opened - timedelta(minutes=5),
        policy_id="t014-slice-source",
        policy_version="1",
        rationale="Offline fixture approval.",
        approved_profile_sha256=profile_sha256(profile),
        approved_layout_sha256=layout_sha256(body),
        adapter=SLICE_PARSER,
    )
    institution = Institution(
        schema_version=CONTRACT_VERSION,
        charter_number=CHARTER,
        legal_name="Example North Charter",
        status=InstitutionStatus.ACTIVE,
        charter_verified=True,
    )
    product = ProductIdentity(
        schema_version=CONTRACT_VERSION,
        product_id=PRODUCT_ID,
        institution_charter=CHARTER,
        family=ProductFamily.MORTGAGE,
        name="Synthetic fixed purchase mortgage",
        lifecycle=ProductLifecycle.ACTIVE,
        rate_availability=RateAvailability.PUBLIC_RATE,
        conforming_status=ConformingStatus.CONFORMING,
        government_guaranty=GovernmentGuaranty.NONE,
    )
    relationship = InstitutionSourceRelationship(
        schema_version=CONTRACT_VERSION,
        relationship_id="athol-style.relationship",
        institution_charter=CHARTER,
        source_id=SOURCE_ID,
        product_id=PRODUCT_ID,
        kind=SourceRelationshipKind.FIRST_PARTY,
        approved_by=REVIEWER,
        approved_at=opened - timedelta(minutes=5),
    )
    engine = open_operational_database(database)
    with engine.begin() as conn:
        insert_institution(conn, institution)
        insert_source(conn, source)
        insert_product(conn, product)
        insert_relationship(conn, relationship)
        insert_profile(conn, profile)
        insert_approval(conn, approval, CHARTER)
        insert_policy(conn, validation_policy(opened - timedelta(minutes=5)))
        insert_review(
            conn,
            ReviewDecision(
                schema_version=CONTRACT_VERSION,
                review_id="athol-style.product-review",
                subject=ReviewSubject.PRODUCT,
                subject_id=PRODUCT_ID,
                verdict=ReviewVerdict.APPROVED,
                actor=REVIEWER,
                decided_at=opened - timedelta(minutes=4),
                rationale="Fixture product review.",
                reviewed_product_sha256=product_content_sha256(product),
            ),
            CHARTER,
        )
    worker = _worker(root, store, clock, body)
    fetched = worker.fetch(source, profile, _fetch_approval(approval, profile), _terms(approval))
    if (
        fetched.run is None
        or fetched.artifact is None
        or fetched.run.status is not RunStatus.SUCCEEDED
    ):
        raise RuntimeError(f"fixture fetch failed: {fetched.reason}")
    artifact = fetched.artifact
    with engine.begin() as conn:
        insert_run(conn, fetched.run)
        insert_artifact(conn, artifact)
        insert_fingerprint(
            conn,
            SourceFingerprint(
                schema_version=CONTRACT_VERSION,
                source_id=SOURCE_ID,
                fetch_run_id=fetched.run.run_id,
                artifact_id=artifact.artifact_id,
                observed_at=artifact.fetched_at,
                content_sha256=artifact.artifact_id,
                structure_sha256=layout_sha256(store.read_body(artifact.artifact_id)),
                adapter=SLICE_PARSER,
            ),
            f"layout-{artifact.artifact_id[:16]}",
        )
    stored = store.read_body(artifact.artifact_id)
    extracted = extract_fixture(
        "athol-style",
        stored,
        artifact_id=artifact.artifact_id,
        extraction_run_id=f"slice-{fetched.run.run_id}",
        git_sha=GIT_SHA,
    )
    offers = _persist_rows(
        engine, extracted, stored, artifact.fetched_at, artifact.fetch_run_id, CHARTER
    )
    first_at = artifact.fetched_at + timedelta(minutes=1)
    first = {row_id: _decide(engine, offer.offer_id, first_at) for row_id, offer in offers.items()}
    chosen = offers["purchase-30-year"]
    review = ReviewDecision(
        schema_version=CONTRACT_VERSION,
        review_id="athol-style.review-30",
        subject=ReviewSubject.OFFER,
        subject_id=chosen.offer_id,
        verdict=ReviewVerdict.APPROVED,
        actor=REVIEWER,
        decided_at=first_at,
        rationale="Fixture reviewer confirms the unchanged 30-year row.",
        reviewed_fingerprints=offer_fingerprints(chosen),
        reviewed_evidence_sha256=offer_evidence_sha256(chosen),
    )
    fingerprints = offer_fingerprints(chosen)
    published_at = first_at + timedelta(seconds=1)
    entries = (
        SnapshotEntry(
            offer_id=chosen.offer_id,
            structural_sha256=fingerprints.structural,
            value_sha256=fingerprints.value,
        ),
    )
    snapshot = PublishedSnapshot(
        schema_version=CONTRACT_VERSION,
        snapshot_id="athol-style.snapshot-1",
        published_at=published_at,
        published_by=REVIEWER,
        entries=entries,
        manifest_sha256=snapshot_manifest_sha256(entries),
        review_ids=(review.review_id,),
    )
    with engine.begin() as conn:
        insert_review(conn, review, CHARTER)
        insert_snapshot(conn, snapshot)
    clock.advance(120)
    refreshed = worker.fetch(source, profile, _fetch_approval(approval, profile), _terms(approval))
    if refreshed.reobserved is None:
        raise RuntimeError(f"expected a 304 re-observation, got {refreshed.reason}")
    observation = refreshed.reobserved.record()
    with engine.begin() as conn:
        insert_reobservation(conn, observation)
        assert chosen.valid_until is not None
        refreshed_offer = chosen.model_copy(
            update={
                "observed_at": observation.observed_at,
                "valid_until": observation.observed_at + (chosen.valid_until - chosen.observed_at),
            }
        )
        insert_validations(conn, validate_offer(refreshed_offer, observation.observed_at))
    refresh = _decide(engine, chosen.offer_id, observation.observed_at + timedelta(minutes=1))
    return SliceRun(database, store, offers, (), first, snapshot, refresh)


def _persist_rows(
    engine: object,
    extracted: FixtureExtraction,
    body: bytes,
    observed_at: datetime,
    fetch_run_id: str,
    charter: int,
) -> dict[str, NormalizedOffer]:
    from sqlalchemy.engine import Engine

    assert isinstance(engine, Engine)
    offers: dict[str, NormalizedOffer] = {}
    with engine.begin() as conn:
        for row in extracted.rows:
            result = normalize_offer(
                row.candidates,
                OfferContext(
                    offer_id=f"athol-style.{row.row_id}",
                    institution_charter=CHARTER,
                    product_id=PRODUCT_ID,
                    source_id=SOURCE_ID,
                    observed_at=observed_at,
                    comparison_target=CanonicalScenarioId.PURCHASE_30Y_FIXED,
                ),
            )
            if result.offer is None:
                continue
            offer = _with_statements(result.offer, result.scenario_checks, observed_at)
            results = validate_offer(offer, observed_at)
            status = (
                OfferStatus.APPROVED
                if all(item.passed for item in results)
                else OfferStatus.QUARANTINED
            )
            offer = offer.model_copy(update={"status": status})
            results = validate_offer(offer, observed_at)
            insert_offer(conn, offer)
            insert_validations(conn, results)
            proofs = []
            for match in offer.scenario_matches:
                if match.stated is None or match.provenance is None or match.source_text is None:
                    continue
                locator = match.provenance.locator
                if not isinstance(locator, HtmlLocator):
                    continue
                node = resolve_text(body, locator.selector)
                digest = hashlib.sha256(node.encode()).hexdigest()
                if digest != locator.normalized_text_sha256 or match.source_text not in node:
                    continue
                proofs.append(
                    {
                        "offer_id": offer.offer_id,
                        "dimension": match.dimension,
                        "fetch_run_id": fetch_run_id,
                        "stated": match.stated,
                        "provenance": match.provenance,
                    }
                )
            from cu_rates.contracts.publication import VerifiedScenarioStatement

            insert_statements(
                conn,
                charter,
                tuple(VerifiedScenarioStatement.model_validate(item) for item in proofs),
            )
            offers[row.row_id] = offer
    return offers


def _with_statements(
    offer: NormalizedOffer,
    checks: tuple[ScenarioCheck, ...],
    observed_at: datetime,
) -> NormalizedOffer:
    by_dimension = {check.dimension: check for check in checks}
    matches = []
    for match in offer.scenario_matches:
        check = by_dimension.get(match.dimension.value)
        text = match.source_text
        bound = None if text is None else stated_bound(match.dimension.value, text)
        provenance = check.provenance if check is not None and bound is not None else None
        matches.append(match.model_copy(update={"stated": bound, "provenance": provenance}))
    return offer.model_copy(
        update={
            "scenario_matches": tuple(matches),
            "valid_until": observed_at + timedelta(hours=71),
        }
    )


def _decide(engine: object, offer_id: str, evaluated_at: datetime) -> PublicationDecision:
    from sqlalchemy.engine import Engine

    assert isinstance(engine, Engine)
    with engine.connect() as conn:
        view: TrustedView = load_publication_context(conn, offer_id, evaluated_at)
    return evaluate_publication(view.offer, view.context)


def _worker(root: Path, store: ArtifactStore, clock: _Clock, body: bytes) -> FetchWorker:
    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                content=b"User-agent: *\nDisallow:\n",
                headers={"Content-Type": "text/plain"},
            )
        if request.headers.get("If-None-Match") == _STRONG_ETAG:
            return httpx.Response(304, headers={"ETag": _STRONG_ETAG})
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/html; charset=utf-8", "ETag": _STRONG_ETAG},
        )

    def transport(_addresses: object) -> httpx.BaseTransport:
        return httpx.MockTransport(send)

    return FetchWorker(
        root / "state",
        store,
        transport,
        git_sha=GIT_SHA,
        clock=clock.now,
        monotonic=clock.now,
        sleep=clock.sleep,
        resolve=lambda _host: ("8.8.8.8",),
    )


def _fetch_approval(approval: SourceApproval, profile: FetchProfile) -> FetchApproval:
    return FetchApproval(
        schema_version=approval.schema_version,
        approval_id=approval.approval_id,
        source_id=approval.source_id,
        source_url=approval.source_url,
        decision=approval.decision,
        actor=approval.actor,
        decided_at=approval.decided_at,
        policy_id=approval.policy_id,
        policy_version=approval.policy_version,
        rationale=approval.rationale,
        approved_profile_sha256=approval.approved_profile_sha256,
        approved_layout_sha256=approval.approved_layout_sha256,
        adapter=approval.adapter,
        profile=profile,
        evidence_kind=EvidenceKind.HTML,
    )


def _terms(approval: SourceApproval) -> TermsReview:
    return TermsReview.model_validate(
        {
            **approval.model_dump(mode="json"),
            "approval_id": "athol-style.terms",
            "terms_url": "https://athol-style.example/terms",
            "terms_sha256": "ab" * 32,
        }
    )


def finding_from_non_offer(
    item: NonOffer, *, observed_at: datetime, source_id: str = SOURCE_ID
) -> ProductFinding:
    return ProductFinding(
        schema_version=CONTRACT_VERSION,
        finding_id=item.finding_id,
        taxonomy_version=TAXONOMY_VERSION,
        institution_charter=CHARTER,
        source_id=source_id,
        observed_at=observed_at,
        label=item.label,
        scope=ProductScope(item.scope),
        family=None if item.family is None else ProductFamily(item.family),
        out_of_scope_product=(
            None
            if item.out_of_scope_product is None
            else OutOfScopeProduct(item.out_of_scope_product)
        ),
        rate_availability=(
            None if item.rate_availability is None else RateAvailability(item.rate_availability)
        ),
        evidence=(item.provenance,),
    )

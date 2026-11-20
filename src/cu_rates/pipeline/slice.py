"""One synthetic credit union, from fixture bytes to a publication decision."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy.engine import Connection, Engine

from cu_rates.adapters.normalization import stated_bound as stated_bound
from cu_rates.adapters.normalization import with_statements as _with_statements
from cu_rates.artifact_store import ArtifactStore, ArtifactStoreError
from cu_rates.contracts.audit import (
    PublishedSnapshot,
    ReviewDecision,
)
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConformingStatus,
    EvidenceKind,
    FetchMethod,
    GovernmentGuaranty,
    InstitutionStatus,
    OutOfScopeProduct,
    ProductFamily,
    ProductLifecycle,
    ProductScope,
    RateAvailability,
    ReviewSubject,
    ReviewVerdict,
    RunStatus,
    SourceRelationshipKind,
)
from cu_rates.contracts.evidence import (
    ExtractionProvenance,
    HtmlLocator,
    Provenance,
    RawArtifact,
)
from cu_rates.contracts.fingerprints import (
    OfferFingerprints,
)
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import (
    PublicationDecision,
    VerifiedScenarioStatement,
    evaluate_publication,
)
from cu_rates.contracts.registry import (
    Institution,
    InstitutionIdentityReview,
    InstitutionSourceRelationship,
    ProductIdentity,
)
from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS
from cu_rates.contracts.sources import (
    ExtractedCandidate,
    FetchProfile,
    FetchRun,
    RateSource,
    SourceApproval,
    SourceFingerprint,
)
from cu_rates.contracts.taxonomy import TAXONOMY_VERSION, ProductFinding
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.records import persist_identity_review
from cu_rates.db.schema import artifact_blobs, slice_normalization_failures
from cu_rates.fetch_policy import FetchRegistry, TermsLedger, TermsReview
from cu_rates.fetch_worker import FetchWorker
from cu_rates.normalize import OfferContext, normalize_offer
from cu_rates.normalize._values import comparison_statement
from cu_rates.pipeline.extract import (
    FIXTURES,
    SLICE_CONFIGURATION,
    SLICE_PARSER,
    ExtractedRow,
    NonOffer,
    extract_fixture,
    layout_sha256,
    resolve_text,
)
from cu_rates.pipeline.trusted import (
    insert_approval,
    insert_artifact,
    insert_capture,
    insert_extraction_run,
    insert_finding,
    insert_fingerprint,
    insert_institution,
    insert_policy,
    insert_product,
    insert_profile,
    insert_relationship,
    insert_reobservation,
    insert_review,
    insert_run,
    insert_source,
    insert_statements,
    insert_validations,
    load_publication_context,
    persist_candidate,
    publish,
    verify_located_claim,
)
from cu_rates.pipeline.validate import validate_offer, validation_policy
from cu_rates.snapshot import Denylist

REVIEWER = "example-reviewer"
CHARTER = 9000001
SOURCE_ID = "athol-style.mortgage"
PRODUCT_ID = "athol-style.purchase-fixed"
PROFILE_ID = "athol-style.http"
GIT_SHA = "0" * 40
_STRONG_ETAG = '"athol-slice-v1"'


@dataclass
class SliceRun:
    database: Path
    store: ArtifactStore
    offers: dict[str, NormalizedOffer]
    findings: tuple[ProductFinding, ...]
    first: dict[str, PublicationDecision]
    reviewed_snapshot: PublishedSnapshot | None
    refresh: PublicationDecision | None
    refreshed_snapshot: PublishedSnapshot | None


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


@dataclass(frozen=True)
class _Identity:
    case_id: str
    filename: str
    key: str
    charter: int
    name: str
    url: str
    layout: str
    family: ProductFamily | None
    target: CanonicalScenarioId | None

    @property
    def source_id(self) -> str:
        return SOURCE_ID if self.key == "athol-style" else f"{self.key}.lending"

    @property
    def product_id(self) -> str:
        return PRODUCT_ID if self.key == "athol-style" else f"{self.key}.fixed-equity"


_CASES = {
    item.case_id: item
    for item in (
        _Identity(
            "athol-style",
            "athol_style_mortgage.html",
            "athol-style",
            CHARTER,
            "Example North Charter",
            "https://athol-style.example/mortgage-rates",
            "94a54601efd57dcaf440f1ec42bf436c6ceca77bfd5ce7eae248da00e382e965",
            ProductFamily.MORTGAGE,
            CanonicalScenarioId.PURCHASE_30Y_FIXED,
        ),
        _Identity(
            "athol-style-dated",
            "athol_style_mortgage_dated.html",
            "athol-style",
            CHARTER,
            "Example North Charter",
            "https://athol-style.example/mortgage-rates",
            "1648ba57c8ceadfff7f86a46d31dfa1b6cd16cb00b18d70a4fb2b43c78db13cd",
            ProductFamily.MORTGAGE,
            CanonicalScenarioId.PURCHASE_30Y_FIXED,
        ),
        _Identity(
            "worcester-style",
            "worcester_style_home_lending.html",
            "worcester-style",
            9000002,
            "Example Central Charter",
            "https://worcester-style.example/home-lending",
            "a223ebdad32b9b4329fe3dbe66a11ce7cb388210e025af86c82e57fb771ef9cd",
            ProductFamily.HOME_EQUITY_LOAN,
            CanonicalScenarioId.FIXED_HOME_EQUITY_10Y,
        ),
        _Identity(
            "stoughton-style",
            "stoughton_style_consumer_loans.html",
            "stoughton-style",
            9000003,
            "Example South Charter",
            "https://stoughton-style.example/auto-and-personal-loans",
            "fd7e43a29598ac367b640fff65259ac1766b527364dba3c59e857c6e230e34ac",
            None,
            None,
        ),
    )
}
_OPENED = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
_PROFILE_PIN = "3cd971eee40da83da83cb6a8c1c338d24eea333657e90e4f9a2dc75e9f5e313d"
_OTHER_PROFILE_PINS = {
    "worcester-style": "e758f79f9a6ae23391c54d4665e0073ab3337963cff373b74df3028562f3ef69",
    "stoughton-style": "c1830d4a4254763686cc4208b609d2986321e7066b593e9c43cdcc3647836079",
}
_PRODUCT_PIN = "c4473ef511c1c1217d386cd781e0fb5c7a53d357e07ecb5e7645c93c0cbcc53c"
# Authored offline approval of the one dated fixture; never derived from the fetched body.
_OFFER_REVIEW_FP = {
    "algorithm": "fp2",
    "structural": "685e540bad68b5006b30313ff10f09afe81055143dfe93335a8f32f2f7c58925",
    "value": "ce8998c1b1f90dcb028e11c5f929d6b0a302d3b1b2a3f837e8950480e4ef6e2c",
}
_OFFER_REVIEW_EVIDENCE = "2ab26110154702f475f32ee3211911c108ff6b12bd983bf66807151f42d00f40"


def _normalize(row: ExtractedRow, identity: _Identity, observed_at: datetime) -> NormalizedOffer:
    result = normalize_offer(
        row.candidates,
        OfferContext(
            offer_id=f"{identity.key}.{row.row_id}",
            institution_charter=identity.charter,
            product_id=identity.product_id,
            source_id=identity.source_id,
            observed_at=observed_at,
            comparison_target=identity.target,
        ),
    )
    if result.offer is None:
        raise ValueError(f"normalization yielded {result.no_offer_reason}")
    return _with_statements(result.offer, result.scenario_checks, observed_at)


def classify_fixture(
    case_id: str, body: bytes
) -> tuple[tuple[NormalizedOffer, ...], tuple[NonOffer, ...]]:
    """Classify a page using its own identity, without persisting or making a request."""
    identity = _CASES[case_id]
    extracted = extract_fixture(
        case_id,
        body,
        artifact_id=hashlib.sha256(body).hexdigest(),
        extraction_run_id=f"slice-{case_id}",
        git_sha=GIT_SHA,
    )
    return tuple(_normalize(row, identity, _OPENED) for row in extracted.rows), extracted.non_offers


def claim_text(store: ArtifactStore, provenance: Provenance) -> str:
    return verify_located_claim(store.read_body(provenance.artifact_id), provenance)


def verify_offer_bytes(
    source: RateSource, offer: NormalizedOffer, body: bytes, artifact: RawArtifact
) -> bool:
    """Require all locators and normalized values to agree with the actual captured row."""
    _, comparable = verified_offer_candidates(source, offer, body, artifact)
    return comparable


def verified_offer_candidates(
    source: RateSource, offer: NormalizedOffer, body: bytes, artifact: RawArtifact
) -> tuple[tuple[ExtractedCandidate, ...], bool]:
    """Re-extract the exact stored row; assertions supplied by callers are not evidence."""
    if offer.effective_at is not None and offer.effective_at > artifact.fetched_at:
        raise ValueError("effective date follows the source observation")
    candidates = [
        identity
        for identity in _CASES.values()
        if (identity.source_id == source.source_id and identity.url == source.url)
    ]
    for provenance in offer.iter_provenance():
        verify_located_claim(body, provenance)
    lifecycle = {"status", "published_at", "valid_until"}
    for identity in candidates:
        if identity.key == "athol-style" and (
            (identity.case_id == "athol-style-dated") != (offer.effective_at is not None)
        ):
            continue
        extraction = extract_fixture(
            identity.case_id,
            body,
            artifact_id=artifact.artifact_id,
            extraction_run_id=f"slice-{artifact.fetch_run_id}",
            git_sha=GIT_SHA,
        )
        for row in extraction.rows:
            if offer.offer_id == f"{identity.key}.{row.row_id}":
                expected = _normalize(row, identity, artifact.fetched_at)
                if offer.model_dump(exclude=lifecycle) == expected.model_dump(exclude=lifecycle):
                    return row.candidates, _supplemental_assumptions_match(row, identity)
    raise ValueError("offer fields do not match the stored artifact row")


def _supplemental_assumptions_match(row: ExtractedRow, identity: _Identity) -> bool:
    """The contract does not carry every canonical dimension as a scenario Match."""
    if identity.target is None:
        return False
    scenario = CANONICAL_SCENARIOS[identity.target]
    required: dict[str, Decimal | bool | None] = {"property_value": scenario.property_value}
    if scenario.family is ProductFamily.MORTGAGE:
        required["autopay_discount"] = scenario.autopay_discount_applied
    else:
        required["first_lien_balance"] = scenario.first_lien_balance
    raw = {item.proposed_path: item.raw_value for item in row.candidates}
    return all(
        (value := raw.get(f"scenario.{name}")) is not None
        and comparison_statement(name, value) == expected
        for name, expected in required.items()
    )


def _profile(identity: _Identity) -> FetchProfile:
    return FetchProfile(
        schema_version=CONTRACT_VERSION,
        profile_id=PROFILE_ID if identity.key == "athol-style" else f"{identity.key}.http",
        source_id=identity.source_id,
        method=FetchMethod.HTTP_GET,
        min_interval_seconds=60,
        timeout_seconds=10,
    )


def _product(identity: _Identity) -> ProductIdentity:
    if identity.family is None:
        raise ValueError("negative fixture does not identify a product")
    return ProductIdentity(
        schema_version=CONTRACT_VERSION,
        product_id=identity.product_id,
        institution_charter=identity.charter,
        family=identity.family,
        name=(
            "Synthetic fixed purchase mortgage"
            if identity.family is ProductFamily.MORTGAGE
            else "Synthetic closed-end equity loan"
        ),
        lifecycle=ProductLifecycle.ACTIVE,
        rate_availability=RateAvailability.PUBLIC_RATE,
        conforming_status=ConformingStatus.CONFORMING,
        government_guaranty=GovernmentGuaranty.NONE,
    )


_IDENTITY_PAGE = (
    '<html><body><p id="identity">{name} - NCUA charter {charter} - Massachusetts</p></body></html>'
)


def _record_identity_review(conn: Connection, store: ArtifactStore, identity: _Identity) -> None:
    """Offline fixture identity proof: a stored synthetic page, a located claim, a review.

    The review cites bytes that are really stored and re-verified by the trusted loader; it is
    never a flag. Real reviews cite a captured official NCUA page in the same way.
    """
    body = _IDENTITY_PAGE.format(name=identity.name, charter=identity.charter).encode()
    artifact = store.put(
        body,
        media_type="text/html; charset=utf-8",
        source_url="https://ncua.example/synthetic/charter-page",
        fetched_at=_OPENED - timedelta(days=1),
        http_status=200,
        fetch_run_id=f"{identity.key}.identity-evidence",
    )
    conn.execute(
        artifact_blobs.insert(),
        {"artifact_id": artifact.artifact_id, "byte_length": artifact.byte_length},
    )
    node = resolve_text(body, "#identity", "css")
    persist_identity_review(
        conn,
        InstitutionIdentityReview(
            schema_version=CONTRACT_VERSION,
            review_id=f"{identity.key}.identity-review",
            institution_charter=identity.charter,
            legal_name=identity.name,
            identity_confirmed=True,
            massachusetts_scope_confirmed=True,
            evidence=(
                Provenance(
                    artifact_id=artifact.artifact_id,
                    locator=HtmlLocator(
                        selector_type="css",
                        selector="#identity",
                        normalized_text_sha256=hashlib.sha256(node.encode()).hexdigest(),
                    ),
                    source_text=node,
                    extraction=ExtractionProvenance(
                        git_sha=GIT_SHA,
                        parser=SLICE_PARSER,
                        configuration_sha256=SLICE_CONFIGURATION,
                        normalization_version="t014.slice",
                        rule_id="aud94.identity-evidence",
                    ),
                ),
            ),
            reviewer=REVIEWER,
            decided_at=_OPENED - timedelta(hours=6, minutes=10),
            rationale="Offline synthetic fixture: charter number, name and Massachusetts match.",
        ),
    )


def _register(
    conn: Connection, identity: _Identity
) -> tuple[RateSource, FetchProfile, SourceApproval]:
    profile = _profile(identity)
    source = RateSource(
        schema_version=CONTRACT_VERSION,
        source_id=identity.source_id,
        url=identity.url,
        evidence_kind=EvidenceKind.HTML,
        description="Original offline synthetic source",
    )
    # Separate fixture decisions are fixed before any collection or artifact is opened.
    approval = SourceApproval(
        schema_version=CONTRACT_VERSION,
        approval_id=f"{identity.key}.approval",
        source_id=source.source_id,
        source_url=source.url,
        decision=ReviewVerdict.APPROVED,
        actor=REVIEWER,
        decided_at=_OPENED - timedelta(hours=6, minutes=5),
        policy_id="t014-slice-source",
        policy_version="1",
        rationale="Offline synthetic fixture only.",
        approved_profile_sha256=_PROFILE_PIN
        if identity.key == "athol-style"
        else (_OTHER_PROFILE_PINS[identity.key]),
        approved_layout_sha256=identity.layout,
        adapter=SLICE_PARSER,
    )
    insert_institution(
        conn,
        Institution(
            schema_version=CONTRACT_VERSION,
            charter_number=identity.charter,
            legal_name=identity.name,
            status=InstitutionStatus.ACTIVE,
            charter_verified=True,
        ),
    )
    insert_source(conn, source)
    if identity.family is not None:
        product = _product(identity)
        insert_product(conn, product)
        insert_relationship(
            conn,
            InstitutionSourceRelationship(
                schema_version=CONTRACT_VERSION,
                relationship_id=f"{identity.key}.relationship",
                institution_charter=identity.charter,
                source_id=source.source_id,
                product_id=product.product_id,
                kind=SourceRelationshipKind.FIRST_PARTY,
                approved_by=REVIEWER,
                approved_at=approval.decided_at,
            ),
        )
        if identity.key == "athol-style":
            insert_review(
                conn,
                ReviewDecision(
                    schema_version=CONTRACT_VERSION,
                    review_id="athol-style.product-review",
                    subject=ReviewSubject.PRODUCT,
                    subject_id=PRODUCT_ID,
                    verdict=ReviewVerdict.APPROVED,
                    actor=REVIEWER,
                    decided_at=_OPENED - timedelta(hours=6, minutes=4),
                    rationale="Fixture product review.",
                    reviewed_product_sha256=_PRODUCT_PIN,
                ),
                CHARTER,
            )
    insert_profile(conn, profile)
    insert_approval(conn, approval, identity.charter)
    insert_policy(conn, validation_policy(approval.decided_at))
    return source, profile, approval


def record_fixture_capture(
    conn: Connection, case_id: str, store: ArtifactStore, run: FetchRun, artifact: RawArtifact
) -> tuple[dict[str, NormalizedOffer], tuple[ProductFinding, ...]]:
    """Keep raw/extraction/normalization history even when this page yields no safe offer."""
    identity = _CASES[case_id]
    if (
        run.source_id != identity.source_id
        or artifact.source_url != identity.url
        or artifact.fetch_run_id != run.run_id
        or artifact.artifact_id not in run.artifact_ids
        or run.status is not RunStatus.SUCCEEDED
        or artifact.http_status != 200
    ):
        raise ValueError("direct fixture observation does not belong to this source/run")
    insert_run(conn, run)
    insert_artifact(conn, artifact)
    extraction_run_id = f"slice-{run.run_id}"
    stage: dict[str, object] = {
        "run_id": extraction_run_id,
        "schema_version": CONTRACT_VERSION,
        "fetch_run_id": run.run_id,
        "parser_name": SLICE_PARSER.name,
        "parser_version": SLICE_PARSER.version,
        "git_sha": GIT_SHA,
        "configuration_sha256": SLICE_CONFIGURATION,
        "started_at": run.finished_at,
        "finished_at": run.finished_at,
    }
    try:
        body = store.read(artifact)
        if store.get_observation(run.run_id, identity.url) != artifact:
            raise ValueError("fixture metadata and stored observation differ")
        extracted = extract_fixture(
            case_id,
            body,
            artifact_id=artifact.artifact_id,
            extraction_run_id=extraction_run_id,
            git_sha=GIT_SHA,
        )
        structure = layout_sha256(body)
    except (ArtifactStoreError, OSError, ValueError) as exc:
        insert_extraction_run(
            conn, {**stage, "status": "failed", "candidate_count": 0, "error": str(exc)}
        )
        return {}, ()
    insert_fingerprint(
        conn,
        SourceFingerprint(
            schema_version=CONTRACT_VERSION,
            source_id=run.source_id,
            fetch_run_id=run.run_id,
            artifact_id=artifact.artifact_id,
            observed_at=artifact.fetched_at,
            content_sha256=artifact.artifact_id,
            structure_sha256=structure,
            adapter=SLICE_PARSER,
        ),
        f"layout-{run.run_id}",
    )
    insert_extraction_run(
        conn,
        {
            **stage,
            "status": "succeeded",
            "error": None,
            "candidate_count": sum(len(row.candidates) for row in extracted.rows)
            + len(extracted.non_offers),
        },
    )
    findings: list[ProductFinding] = []
    for item in extracted.non_offers:
        candidate = ExtractedCandidate(
            schema_version=CONTRACT_VERSION,
            candidate_id=item.finding_id,
            extraction_run_id=extraction_run_id,
            raw_value=item.provenance.source_text,
            provenance=item.provenance,
        )
        persist_candidate(conn, candidate, identity.charter)
        finding = finding_from_non_offer(item, observed_at=artifact.fetched_at, case_id=case_id)
        finding = finding.model_copy(update={"finding_id": f"{finding.finding_id}.{run.run_id}"})
        insert_finding(conn, finding)
        findings.append(finding)
    offers: dict[str, NormalizedOffer] = {}
    for row in extracted.rows:
        for candidate in row.candidates:
            persist_candidate(conn, candidate, identity.charter)
        try:
            offer = _normalize(row, identity, artifact.fetched_at)
        except ValueError as exc:
            conn.execute(
                slice_normalization_failures.insert(),
                {
                    "extraction_run_id": extraction_run_id,
                    "row_id": row.row_id,
                    "reason": str(exc),
                },
            )
            continue
        insert_capture(conn, offer, run.run_id)
        insert_validations(conn, validate_offer(offer, run.finished_at))
        proofs = tuple(
            VerifiedScenarioStatement(
                offer_id=offer.offer_id,
                dimension=match.dimension,
                fetch_run_id=run.run_id,
                stated=match.stated,
                provenance=match.provenance,
            )
            for match in offer.scenario_matches
            if match.stated is not None
            and match.provenance is not None
            and verify_located_claim(body, match.provenance) == match.source_text
        )
        insert_statements(conn, identity.charter, proofs)
        offers[row.row_id] = offer
    return offers, tuple(findings)


class FixtureWorkspaceError(ValueError):
    """The fixture root is not a fresh, unaliased, disposable directory."""


def _claim_fixture_workspace(root: Path) -> Path:
    """Fail closed, before any write, unless ``root`` is a new or empty real directory.

    Accepted: a nonexistent child of an existing directory (created here, without parents), or
    an existing empty directory. Rejected: any symlink or junction in the path, a non-directory,
    any existing entry (including hidden files or prior operational state), or a missing parent.
    These are path checks only: they cannot detect bind mounts, hard links, or a directory that
    another process populates after the check.
    """
    lexical = Path(os.path.abspath(root))
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise FixtureWorkspaceError("fixture workspace path cannot be resolved") from exc
    if os.path.normcase(resolved) != os.path.normcase(lexical):
        raise FixtureWorkspaceError("fixture workspace path is aliased through a link")
    try:
        status = os.lstat(lexical)
    except FileNotFoundError:
        if not lexical.parent.is_dir():
            raise FixtureWorkspaceError("fixture workspace parent does not exist") from None
        try:
            lexical.mkdir()
        except FileExistsError as exc:
            raise FixtureWorkspaceError("fixture workspace appeared concurrently") from exc
        return lexical
    if stat.S_ISLNK(status.st_mode) or lexical.is_junction():
        raise FixtureWorkspaceError("fixture workspace is a link")
    if not stat.S_ISDIR(status.st_mode):
        raise FixtureWorkspaceError("fixture workspace is not a directory")
    with os.scandir(lexical) as entries:
        if next(entries, None) is not None:
            raise FixtureWorkspaceError("fixture workspace is not empty")
    return lexical


def run_fixture(
    root: Path, case_id: str = "athol-style-dated", *, body: bytes | None = None
) -> SliceRun:
    """Run the offline smoke test; unknown or altered captures cannot self-approve.

    ``root`` must be a disposable workspace: a new child of an existing directory or an existing
    empty directory, with no links in its path. Anything else is refused before any write.
    """
    identity = _CASES[case_id]
    root = _claim_fixture_workspace(root)
    database = root / "operational.sqlite"
    upgrade_database(database)
    store = ArtifactStore(root / "evidence")
    engine = open_operational_database(database)
    clock = _Clock(_OPENED)
    with engine.begin() as conn:
        source, profile, approval = _register(conn, identity)
        _record_identity_review(conn, store, identity)
    denylist = Denylist(root / "independent" / "withdrawals.json")
    denylist.initialize()
    terms = TermsLedger(root / "private" / "terms.json")
    terms.initialize()
    terms.record(_terms(approval), denylist=denylist)
    registry = FetchRegistry(database, terms, denylist, automatic=None)
    worker = _worker(
        root,
        store,
        clock,
        (FIXTURES / identity.filename).read_bytes() if body is None else body,
        registry,
    )
    result = worker.fetch(source, profile)
    if result.run is None or result.artifact is None:
        raise RuntimeError(f"fixture fetch failed: {result.reason}")
    with engine.begin() as conn:
        offers, findings = record_fixture_capture(conn, case_id, store, result.run, result.artifact)
    first_at = result.run.finished_at + timedelta(minutes=1)
    first = {
        row_id: _decide(engine, store, offer.offer_id, first_at) for row_id, offer in offers.items()
    }
    reviewed_snapshot = None
    refresh = None
    refreshed_snapshot = None
    if case_id == "athol-style-dated" and (chosen := offers.get("purchase-30-year")) is not None:
        review = ReviewDecision(
            schema_version=CONTRACT_VERSION,
            review_id="athol-style.review-30",
            subject=ReviewSubject.OFFER,
            subject_id="athol-style.purchase-30-year",
            verdict=ReviewVerdict.APPROVED,
            actor=REVIEWER,
            decided_at=_OPENED + timedelta(minutes=1),
            rationale="Explicit synthetic review for the independently pinned dated fixture.",
            reviewed_fingerprints=OfferFingerprints.model_validate(_OFFER_REVIEW_FP),
            reviewed_evidence_sha256=_OFFER_REVIEW_EVIDENCE,
        )
        with engine.begin() as conn:
            insert_review(conn, review, CHARTER)
            reviewed_snapshot = publish(
                conn,
                chosen.offer_id,
                store=store,
                evaluated_at=first_at + timedelta(seconds=1),
                snapshot_id="athol-style.snapshot-1",
                reviewed=True,
                allow_fixture=True,
            )
        if reviewed_snapshot is not None:
            clock.advance(120)
            observed = worker.fetch(source, profile).reobserved
            if observed is None:
                raise RuntimeError("expected a stored strong-ETag 304")
            observation = observed.record()
            with engine.begin() as conn:
                insert_reobservation(conn, observation)
                current = load_publication_context(
                    conn, chosen.offer_id, observation.observed_at, store=store, allow_fixture=True
                ).offer
                if current.observed_at == observation.observed_at:
                    insert_validations(conn, validate_offer(current, observation.observed_at))
                publish_at = observation.observed_at + timedelta(minutes=1)
                ready = load_publication_context(
                    conn, chosen.offer_id, publish_at, store=store, allow_fixture=True
                )
                refresh = evaluate_publication(ready.offer, ready.context)
                refreshed_snapshot = publish(
                    conn,
                    chosen.offer_id,
                    store=store,
                    evaluated_at=publish_at,
                    snapshot_id="athol-style.snapshot-2",
                    allow_fixture=True,
                )
    return SliceRun(
        database, store, offers, findings, first, reviewed_snapshot, refresh, refreshed_snapshot
    )


def _decide(
    engine: Engine, store: ArtifactStore, offer_id: str, evaluated_at: datetime
) -> PublicationDecision:
    with engine.connect() as conn:
        view = load_publication_context(
            conn, offer_id, evaluated_at, store=store, allow_fixture=True
        )
    return evaluate_publication(view.offer, view.context)


def _worker(
    root: Path, store: ArtifactStore, clock: _Clock, body: bytes, registry: FetchRegistry
) -> FetchWorker:
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
        registry=registry,
        clock=clock.now,
        monotonic=clock.now,
        sleep=clock.sleep,
        resolve=lambda _host: ("8.8.8.8",),
    )


def _terms(approval: SourceApproval) -> TermsReview:
    return TermsReview.model_validate(
        {
            **approval.model_dump(mode="json"),
            "approval_id": f"{approval.source_id}.terms",
            "terms_url": f"{approval.source_url.rsplit('/', 1)[0]}/terms",
            "terms_sha256": "ab" * 32,
        }
    )


def finding_from_non_offer(
    item: NonOffer, *, observed_at: datetime, case_id: str
) -> ProductFinding:
    identity = _CASES[case_id]
    return ProductFinding(
        schema_version=CONTRACT_VERSION,
        finding_id=item.finding_id,
        taxonomy_version=TAXONOMY_VERSION,
        institution_charter=identity.charter,
        source_id=identity.source_id,
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

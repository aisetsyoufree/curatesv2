"""Original synthetic layouts exercise the production adapter without live collection."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.adapters import registry
from cu_rates.adapters.registry import (
    Verification,
    normalize_row,
    verify_offer_against_stored_bytes,
)
from cu_rates.adapters.static_html import (
    PARSER,
    Extraction,
    RowExtraction,
    SourceConfiguration,
    configuration_parser,
    extract,
)
from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.audit import PublishedSnapshot, ReviewDecision
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    EvidenceKind,
    FetchMethod,
    InstitutionStatus,
    OfferStatus,
    ProductFamily,
    ReviewSubject,
    ReviewVerdict,
    SourceRelationshipKind,
)
from cu_rates.contracts.evidence import ExtractionProvenance, HtmlLocator, Provenance, RawArtifact
from cu_rates.contracts.fingerprints import offer_fingerprints, product_content_sha256
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import (
    PublicationOutcome,
    ValidationPolicy,
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.contracts.registry import (
    Institution,
    InstitutionIdentityReview,
    InstitutionSourceRelationship,
    ProductIdentity,
)
from cu_rates.contracts.sources import (
    FetchProfile,
    FetchRun,
    RateSource,
    SourceApproval,
    profile_sha256,
)
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.records import persist_identity_review
from cu_rates.db.schema import extraction_runs
from cu_rates.pipeline.adapter_capture import extract_stored_capture
from cu_rates.pipeline.extract import SLICE_PARSER, layout_sha256
from cu_rates.pipeline.trusted import (
    TrustedView,
    insert_approval,
    insert_artifact,
    insert_institution,
    insert_policy,
    insert_product,
    insert_profile,
    insert_relationship,
    insert_review,
    insert_run,
    insert_source,
    load_publication_context,
    publish,
)
from cu_rates.snapshot import SnapshotError
from cu_rates.snapshot.builder import SnapshotPublisher

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
NOW = datetime(2026, 11, 18, 16, tzinfo=UTC)
GIT = "a" * 40


def inputs(
    name: str = "table", body: bytes | None = None
) -> tuple[SourceConfiguration, RateSource, bytes, RawArtifact, SourceApproval]:
    config = SourceConfiguration.model_validate_json((FIXTURES / f"{name}.rates.json").read_bytes())
    source = RateSource(
        schema_version="1.0",
        source_id=config.source_id,
        url=config.source_url,
        evidence_kind=EvidenceKind.HTML,
    )
    body = (FIXTURES / f"{name}.html").read_bytes() if body is None else body
    artifact = RawArtifact(
        schema_version="1.0",
        artifact_id=sha256(body).hexdigest(),
        source_url=source.url,
        media_type="text/html",
        byte_length=len(body),
        http_status=200,
        fetched_at=NOW,
        fetch_run_id="capture-1",
    )
    approval = SourceApproval(
        schema_version="1.0",
        approval_id="source-approval",
        source_id=source.source_id,
        source_url=source.url,
        decision=ReviewVerdict.APPROVED,
        actor="synthetic-human",
        decided_at=NOW - timedelta(days=1),
        policy_id="test-policy",
        policy_version="1",
        rationale="Synthetic configuration approval only.",
        approved_layout_sha256=config.layout_sha256,
        adapter=configuration_parser(config),
    )
    return config, source, body, artifact, approval


@pytest.fixture(autouse=True)
def configured_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "CONFIGURATIONS", FIXTURES)


@pytest.mark.parametrize(
    ("name", "confidence", "count"),
    [("table", "high", 1), ("prose", "medium", 1), ("multi", "high", 2)],
)
def test_configured_layouts(name: str, confidence: str, count: int) -> None:
    config, source, body, artifact, approval = inputs(name)
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == confidence and not result.diagnostics
    assert len(result.rows) == count
    for row in result.rows:
        offer = normalize_row(row, config, artifact)
        assert offer.note_rate and offer.apr and offer.points
        assert offer.effective_at == datetime(2026, 11, 18, 10, tzinfo=UTC)
        assert all(c.raw_value == c.provenance.source_text for c in row.candidates)
        verified = verify_offer_against_stored_bytes(source, approval, offer, body, artifact)
        assert verified.confidence == confidence and verified.comparable
        if row.row.comparison_target:
            assert all(
                m.stated is not None and m.provenance is not None for m in offer.scenario_matches
            )
    if name == "multi":
        offers = [normalize_row(r, config, artifact) for r in result.rows]
        assert [o.note_rate.value for o in offers if o.note_rate] == [
            Decimal("4.111"),
            Decimal("5.111"),
        ]
        assert [o.family for o in offers] == [
            ProductFamily.MORTGAGE,
            ProductFamily.HOME_EQUITY_LOAN,
        ]


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (b'<tr id="mortgage">', b'<tr id="moved">', "layout drift"),
        (b"Note rate</th>", b"Rate or APR</th>", "ambiguous or changed label"),
        (b"<td>4.222%</td>", b"<td></td>", "required fields missing"),
        (b'<dd id="score">780</dd>', b'<dd id="score">unknown</dd>', "required"),
    ],
)
def test_layout_label_and_missing_fields_fail_low(before: bytes, after: bytes, reason: str) -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(before, after)
    _, _, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="bad", git_sha=GIT)
    assert result.confidence == "low"
    assert reason in "; ".join(result.diagnostics)


def test_wrong_value_in_excerpt_and_partial_number_are_low() -> None:
    config, source, body, artifact, _ = inputs()
    for excerpt in ("4.112%", "111%"):
        fields = tuple(
            f.model_copy(update={"excerpt": excerpt, "context_text": "4.111%"})
            if f.path == "note_rate"
            else f
            for f in config.rows[0].fields
        )
        changed = config.model_copy(
            update={"rows": (config.rows[0].model_copy(update={"fields": fields}),)}
        )
        result = extract(changed, source, body, artifact, extraction_run_id="bad", git_sha=GIT)
        assert result.confidence == "low" and any("excerpt" in d for d in result.diagnostics)


def test_duplicate_label_locator_fails_even_with_matching_layout_pin() -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(b"</body>", b'<p id="rate-label">Note rate</p></body>')
    _, _, _, artifact, _ = inputs(body=body)
    config = config.model_copy(update={"layout_sha256": layout_sha256(body)})
    result = extract(config, source, body, artifact, extraction_run_id="bad", git_sha=GIT)
    assert result.confidence == "low" and any("uniquely" in d for d in result.diagnostics)


@pytest.mark.parametrize(
    "change",
    [
        "unknown adapter",
        "unknown version",
        "rejected",
        "wrong URL",
        "missing approval",
        "config hash",
        "wrong value",
        "artifact hash",
        "layout pin",
        "locator hash",
        "excerpt",
    ],
)
def test_trusted_reextraction_refuses_forged_claims(change: str) -> None:
    config, source, body, artifact, original_approval = inputs()
    approval: SourceApproval | None = original_approval
    row = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT).rows[0]
    offer = normalize_row(row, config, artifact)
    assert approval is not None
    if change == "unknown adapter":
        approval = approval.model_copy(
            update={"adapter": PARSER.model_copy(update={"name": "unknown"})}
        )
    elif change == "unknown version":
        approval = approval.model_copy(
            update={"adapter": PARSER.model_copy(update={"version": "999"})}
        )
    elif change == "rejected":
        approval = approval.model_copy(update={"decision": ReviewVerdict.REJECTED})
    elif change == "wrong URL":
        approval = approval.model_copy(update={"source_url": "https://other.example/rates"})
    elif change == "missing approval":
        approval = None
    elif change == "layout pin":
        approval = approval.model_copy(update={"approved_layout_sha256": "b" * 64})
    elif change in {"locator hash", "excerpt"}:
        assert offer.note_rate
        ref = offer.note_rate.provenance
        forged = (
            ref.model_copy(update={"source_text": "4.112%"})
            if change == "excerpt"
            else ref.model_copy(
                update={
                    "locator": ref.locator.model_copy(update={"normalized_text_sha256": "b" * 64})
                }
            )
        )
        offer = offer.model_copy(
            update={"note_rate": offer.note_rate.model_copy(update={"provenance": forged})}
        )
    elif change == "config hash":
        assert offer.note_rate
        ref = offer.note_rate.provenance
        forged = ref.model_copy(
            update={
                "extraction": ref.extraction.model_copy(update={"configuration_sha256": "b" * 64})
            }
        )
        offer = offer.model_copy(
            update={"note_rate": offer.note_rate.model_copy(update={"provenance": forged})}
        )
    elif change == "wrong value":
        assert offer.note_rate
        offer = offer.model_copy(
            update={"note_rate": offer.note_rate.model_copy(update={"value": Decimal("4.112")})}
        )
    else:
        body += b" "
    with pytest.raises(ValueError):
        verify_offer_against_stored_bytes(source, approval, offer, body, artifact)


def test_schema_1_0_offer_replays_with_current_normalizer() -> None:
    config, source, body, artifact, approval = inputs()
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    current = normalize_row(result.rows[0], config, artifact)
    legacy = NormalizedOffer.model_validate({**current.model_dump(), "schema_version": "1.0"})
    assert current.schema_version == "1.1" and legacy.schema_version == "1.0"
    assert offer_fingerprints(legacy) == offer_fingerprints(current)
    verified = verify_offer_against_stored_bytes(source, approval, legacy, body, artifact)
    assert verified.comparable and verified.confidence == "high"
    # Compatibility cannot mask an actual financial change.
    assert legacy.note_rate
    forged = legacy.model_copy(
        update={"note_rate": legacy.note_rate.model_copy(update={"value": Decimal("4.112")})}
    )
    with pytest.raises(ValueError, match="offer fields do not match"):
        verify_offer_against_stored_bytes(source, approval, forged, body, artifact)


def prepare(
    conn: Connection, store: ArtifactStore, name: str = "table", family: str = "mortgage"
) -> tuple[RateSource, FetchProfile]:
    config, source, _, _, approval = inputs(name)
    before = NOW - timedelta(days=1)
    profile = FetchProfile(
        schema_version="1.0",
        profile_id="table.http",
        source_id=source.source_id,
        method=FetchMethod.HTTP_GET,
        min_interval_seconds=10800,
        timeout_seconds=10,
    )
    insert_institution(
        conn,
        Institution(
            schema_version="1.0",
            charter_number=config.institution_charter,
            legal_name="Synthetic Cedar CU",
            status=InstitutionStatus.ACTIVE,
            charter_verified=True,
        ),
    )
    insert_source(conn, source)
    insert_profile(conn, profile)
    insert_approval(
        conn,
        approval.model_copy(update={"approved_profile_sha256": profile_sha256(profile)}),
        config.institution_charter,
    )
    product = ProductIdentity.model_validate(
        dict(
            schema_version="1.0",
            product_id=config.rows[0].product_id,
            institution_charter=config.institution_charter,
            family=family,
            name="Synthetic purchase",
            lifecycle="active",
            rate_availability="public_rate",
            conforming_status="conforming",
            government_guaranty="none",
        )
    )
    insert_product(conn, product)
    insert_relationship(
        conn,
        InstitutionSourceRelationship(
            schema_version="1.0",
            relationship_id="table.relationship",
            institution_charter=config.institution_charter,
            source_id=source.source_id,
            product_id=product.product_id,
            kind=SourceRelationshipKind.FIRST_PARTY,
            approved_by="synthetic-human",
            approved_at=before,
        ),
    )
    insert_review(
        conn,
        ReviewDecision(
            schema_version="1.0",
            review_id="table.product-review",
            subject=ReviewSubject.PRODUCT,
            subject_id=product.product_id,
            verdict=ReviewVerdict.APPROVED,
            actor="synthetic-human",
            decided_at=before,
            rationale="Synthetic product review",
            reviewed_product_sha256=product_content_sha256(product),
        ),
        config.institution_charter,
    )
    insert_policy(
        conn,
        ValidationPolicy(
            schema_version="1.0",
            policy_id="table.validation",
            policy_version="1",
            required_rules=("rule.apr-ge-note-rate",),
            approved_by="synthetic-human",
            approved_at=before,
        ),
    )
    text = "Synthetic Cedar CU - NCUA charter 8000401 - Massachusetts"
    artifact = store.put(
        f'<html><body><p id="identity">{text}</p></body></html>'.encode(),
        source_url=source.url,
        media_type="text/html",
        fetched_at=before,
        http_status=200,
        fetch_run_id="identity-capture",
    )
    insert_run(conn, fetch_run(profile, artifact))
    insert_artifact(conn, artifact)
    persist_identity_review(
        conn,
        InstitutionIdentityReview(
            schema_version="1.0",
            review_id="identity-review",
            institution_charter=config.institution_charter,
            legal_name="Synthetic Cedar CU",
            identity_confirmed=True,
            massachusetts_scope_confirmed=True,
            reviewer="synthetic-human",
            decided_at=before,
            rationale="Synthetic identity review",
            evidence=(
                Provenance(
                    artifact_id=artifact.artifact_id,
                    source_text=text,
                    locator=HtmlLocator(
                        selector_type="css",
                        selector="#identity",
                        normalized_text_sha256=sha256(text.encode()).hexdigest(),
                    ),
                    extraction=ExtractionProvenance(
                        git_sha=GIT,
                        parser=PARSER,
                        configuration_sha256=config.sha256,
                        normalization_version="test",
                        rule_id="test.identity",
                    ),
                ),
            ),
        ),
    )
    return source, profile


def fetch_run(profile: FetchProfile, artifact: RawArtifact) -> FetchRun:
    return FetchRun.model_validate(
        dict(
            schema_version="1.0",
            run_id=artifact.fetch_run_id,
            source_id=profile.source_id,
            profile_id=profile.profile_id,
            profile_sha256=profile_sha256(profile),
            status="succeeded",
            started_at=artifact.fetched_at,
            finished_at=artifact.fetched_at,
            git_sha=GIT,
            artifact_ids=(artifact.artifact_id,),
        )
    )


def capture(
    conn: Connection,
    store: ArtifactStore,
    source: RateSource,
    profile: FetchProfile,
    when: datetime,
    run_id: str,
    body: bytes | None = None,
    *,
    expected_confidence: str | None = None,
) -> None:
    body = (
        (FIXTURES / f"{source.source_id.split('.')[0]}.html").read_bytes() if body is None else body
    )
    artifact = store.put(
        body,
        source_url=source.url,
        media_type="text/html",
        fetched_at=when,
        http_status=200,
        fetch_run_id=run_id,
    )
    result = extract_stored_capture(conn, store, source, fetch_run(profile, artifact))
    expected = expected_confidence or ("medium" if source.source_id == "prose.rates" else "high")
    assert result.confidence == expected, result.diagnostics
    assert len(result.offers) == 1 and result.offers[0].status is OfferStatus.CANDIDATE


def reviewed_adapter(
    conn: Connection, store: ArtifactStore, name: str = "table"
) -> tuple[RateSource, FetchProfile, PublishedSnapshot]:
    source, profile = prepare(conn, store, name)
    capture(
        conn,
        store,
        source,
        profile,
        NOW - timedelta(hours=4),
        "first",
        (FIXTURES / f"{name}.html").read_bytes() + b"\n",
    )
    capture(conn, store, source, profile, NOW, "second")
    view = load_publication_context(conn, f"{name}.purchase", NOW, store=store)
    assert view.offer.status is OfferStatus.APPROVED
    decision = evaluate_publication(view.offer, view.context)
    assert decision.outcome is PublicationOutcome.AUTO_PUBLISH, decision.reasons  # D-21
    pair = corroborated_paths(view.offer, view.context)
    assert len(pair) == 6
    assert {p.capture_id for p in pair} == {"first", "second"}
    insert_review(
        conn,
        ReviewDecision(
            schema_version="1.0",
            review_id="table.offer-review",
            subject=ReviewSubject.OFFER,
            subject_id=view.offer.offer_id,
            verdict=ReviewVerdict.APPROVED,
            actor="synthetic-human",
            decided_at=NOW,
            rationale="Review both synthetic static-adapter captures",
            evidence=tuple(dict.fromkeys(p.provenance for p in pair)),
            reviewed_fingerprints=offer_fingerprints(view.offer),
            reviewed_evidence_sha256=publication_evidence_sha256(view.offer, pair),
        ),
        8000401,
    )
    manifest = publish(
        conn,
        view.offer.offer_id,
        store=store,
        evaluated_at=NOW,
        snapshot_id="static-reviewed",
        reviewed=True,
    )
    assert manifest is not None
    return source, profile, manifest


@pytest.mark.parametrize("name", ["table", "prose"])
def test_stored_static_captures_reach_verified_snapshot(tmp_path: Path, name: str) -> None:
    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        _, _, manifest = reviewed_adapter(conn, store, name)
        publisher = SnapshotPublisher(tmp_path / "public", allow_fixture=True)
        info = publisher.build(manifest, conn=conn, store=store)
        assert info.snapshot_id == "static-reviewed"
        view = load_publication_context(conn, f"{name}.purchase", NOW, store=store)
        assert (
            view.context.source_approval
            and view.context.source_approval.adapter == configuration_parser(inputs(name)[0])
        )
        assert len(view.context.verified_scenario_statements) >= 24
        with pytest.raises(SnapshotError, match="fixture or reserved"):
            SnapshotPublisher(tmp_path / "production").build(manifest, conn=conn, store=store)
    engine.dispose()


def test_low_confidence_current_capture_cannot_borrow_reviewed_history(tmp_path: Path) -> None:
    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        source, profile, _ = reviewed_adapter(conn, store)
        old = load_publication_context(conn, "table.purchase", NOW, store=store)
        later = NOW + timedelta(hours=4)
        body = (
            (FIXTURES / "table.html").read_bytes().replace(b"Note rate</th>", b"Rate or APR</th>")
        )
        art = store.put(
            body,
            source_url=source.url,
            media_type="text/html",
            fetched_at=later,
            http_status=200,
            fetch_run_id="drifted",
        )
        result = extract_stored_capture(conn, store, source, fetch_run(profile, art))
        assert result.confidence == "low" and not result.offers
        row = conn.execute(
            select(extraction_runs).where(extraction_runs.c.run_id == "extract-drifted")
        ).one()
        assert row.status == "failed" and "layout drift" in row.error
        view = load_publication_context(conn, old.offer.offer_id, later, store=store)
        assert view.offer.status is OfferStatus.QUARANTINED
        assert not evaluate_publication(view.offer, view.context).publishable_now
        assert (
            publish(conn, view.offer.offer_id, store=store, evaluated_at=later, snapshot_id="bad")
            is None
        )
    engine.dispose()


def test_drift_guard_checks_unselected_structure() -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(b"</body>", b"<aside>New unreviewed layout</aside></body>")
    _, _, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="drift", git_sha=GIT)
    assert result.confidence == "low" and result.diagnostics == ("layout drift",)


def test_label_check_is_independent_of_structure_pin() -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(b"Note rate</th>", b"Rate or APR</th>")
    _, _, _, artifact, _ = inputs(body=body)
    config = config.model_copy(update={"layout_sha256": layout_sha256(body)})
    result = extract(config, source, body, artifact, extraction_run_id="label", git_sha=GIT)
    assert result.confidence == "low" and any("label" in d for d in result.diagnostics)


def test_configuration_cannot_omit_required_field() -> None:
    config, source, body, artifact, _ = inputs()
    row = config.rows[0].model_copy(
        update={"fields": tuple(f for f in config.rows[0].fields if f.path != "apr")}
    )
    config = config.model_copy(update={"rows": (row,)})
    result = extract(config, source, body, artifact, extraction_run_id="missing", git_sha=GIT)
    assert result.confidence == "low" and result.diagnostics == ("required fields missing: apr",)


def test_prose_cannot_drop_negation_or_new_qualification() -> None:
    config, source, body, _, _ = inputs("prose")
    body = body.replace(b"Note rate: 4.111%", b"Not a note rate: 4.111%")
    _, _, _, artifact, _ = inputs("prose", body)
    result = extract(config, source, body, artifact, extraction_run_id="negation", git_sha=GIT)
    assert result.confidence == "low" and any("context" in d for d in result.diagnostics)


def test_trusted_loader_quarantines_low_reextraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        reviewed_adapter(conn, store)
        config, _, _, _, _ = inputs()
        row = config.rows[0].model_copy(
            update={"fields": tuple(f for f in config.rows[0].fields if f.path != "apr")}
        )
        changed = config.model_copy(update={"rows": (row,)})
        monkeypatch.setattr(registry, "load_configuration", lambda _: changed)
        view = load_publication_context(conn, "table.purchase", NOW, store=store)
        assert view.offer.status is OfferStatus.QUARANTINED
        assert not view.context.known_artifacts
    engine.dispose()


def test_production_publisher_refuses_fixture_adapter_on_public_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate the adapter guard from the independent reserved-host and fixture-identity
    # guards. Every capture is still original synthetic content, with no network access.
    from cu_rates.pipeline import slice
    from cu_rates.snapshot import builder
    from tests.pub2_fixtures import reviewed_fixture
    from tests.test_public_snapshot import StoredFixture, release

    run = reviewed_fixture(tmp_path / "fixture-worker")
    engine = open_operational_database(run.database)
    with engine.connect() as conn:
        row = StoredFixture(run, conn).row()
        public = builder._public_projection(
            row, evaluate_publication(row.offer, row.context).placement, True
        )
        source = row.context.source.model_copy(update={"url": "https://public-host.org/rates"})
        approval = row.context.source_approval
        assert approval is not None and approval.adapter == SLICE_PARSER
        approval = approval.model_copy(update={"source_url": source.url})
        artifacts = tuple(
            a.model_copy(update={"source_url": source.url}) for a in row.context.known_artifacts
        )
        ctx = row.context.model_copy(
            update={"source": source, "source_approval": approval, "known_artifacts": artifacts}
        )
        monkeypatch.setattr(
            builder,
            "load_publication_context",
            lambda *a, **k: TrustedView(row.offer, ctx, row.current_review),
        )
        monkeypatch.setattr(slice, "verified_offer_candidates", lambda *a: (row.candidates, True))
        monkeypatch.setattr(builder, "_admit", lambda *a: public)
        publisher = SnapshotPublisher(tmp_path / "production")
        with pytest.raises(SnapshotError) as exc:
            publisher.build(release("guard-proof", row), conn=conn, store=run.store)
        assert exc.value.__cause__ is not None
        assert "fixture adapter refused in production mode" in str(exc.value.__cause__)
        assert not (publisher.root / "current.json").exists()
    engine.dispose()


def test_source_stated_range_produces_typed_proof() -> None:
    config, source, body, _, approval = inputs()
    body = body.replace(b'<dd id="score">780</dd>', b'<dd id="score">780 to 800</dd>')
    _, _, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="range", git_sha=GIT)
    assert result.confidence == "high"
    offer = normalize_row(result.rows[0], config, artifact)
    match = next(m for m in offer.scenario_matches if m.dimension.value == "credit_score")
    assert match.stated is not None and match.stated.lower == Decimal(780)
    assert match.stated.upper == Decimal(800) and match.source_text == "780 to 800"
    assert verify_offer_against_stored_bytes(source, approval, offer, body, artifact).comparable


def test_snapshot_rechecks_low_confidence_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cu_rates.snapshot import builder

    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        _, _, manifest = reviewed_adapter(conn, store)
        view = load_publication_context(conn, "table.purchase", NOW, store=store)
        row = builder._load_candidate(
            conn, store, view.offer.offer_id, manifest, allow_fixture=True
        )
        public = builder._public_projection(
            row, evaluate_publication(view.offer, view.context).placement, True
        )
        config, _, _, _, _ = inputs()
        fields = tuple(f for f in config.rows[0].fields if f.path != "apr")
        changed = config.model_copy(
            update={"rows": (config.rows[0].model_copy(update={"fields": fields}),)}
        )
        monkeypatch.setattr(registry, "load_configuration", lambda _: changed)
        # Simulate a stale trusted view. Snapshot construction must still re-extract.
        monkeypatch.setattr(builder, "load_publication_context", lambda *a, **k: view)
        monkeypatch.setattr(builder, "_admit", lambda *a: public)
        publisher = SnapshotPublisher(tmp_path / "public", allow_fixture=True)
        with pytest.raises(SnapshotError, match="verify persisted evidence"):
            publisher.build(manifest, conn=conn, store=store)
        assert not (publisher.root / "current.json").exists()
    engine.dispose()


def test_unproven_occupancy_is_low() -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(b"Owner-occupied single-family primary residence", b"owner_occupied")
    _, _, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="unstated", git_sha=GIT)
    assert result.confidence == "low"
    assert any("literal proof" in d for d in result.diagnostics)


def test_low_page_cannot_be_verified_through_healthy_row() -> None:
    config, source, body, _, approval = inputs("multi")
    body = body.replace(b"<dd>5.222%</dd>", b"<dd></dd>")
    _, _, _, artifact, _ = inputs("multi", body)
    result = extract(config, source, body, artifact, extraction_run_id="partial", git_sha=GIT)
    assert result.confidence == "low" and result.rows[0].confidence == "high"
    offer = normalize_row(result.rows[0], config, artifact)
    assert (
        verify_offer_against_stored_bytes(source, approval, offer, body, artifact).confidence
        == "low"
    )


def test_table_rate_changes_use_same_configuration() -> None:
    config, source, body, _, approval = inputs()
    body = body.replace(b"<td>4.111%</td>", b"<td>4.112%</td>")
    _, _, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="changed-rate", git_sha=GIT)
    assert result.confidence == "high"
    offer = normalize_row(result.rows[0], config, artifact)
    assert offer.note_rate and offer.note_rate.value == Decimal("4.112")
    assert verify_offer_against_stored_bytes(source, approval, offer, body, artifact).comparable


@pytest.mark.parametrize("change", ["selector", "column", "label", "target", "paths", "excerpt"])
@pytest.mark.parametrize("boundary", ["dispatch", "extract", "old offer", "new offer"])
def test_configuration_edits_require_new_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, boundary: str
) -> None:
    from cu_rates.pipeline import adapter_capture

    config, source, body, artifact, approval = inputs()
    original = normalize_row(
        extract(config, source, body, artifact, extraction_run_id="original", git_sha=GIT).rows[0],
        config,
        artifact,
    )
    row = config.rows[0]
    fields = list(row.fields)
    rate_index = next(i for i, f in enumerate(fields) if f.path == "note_rate")
    if change in {"selector", "column"}:
        index = (
            rate_index
            if change == "selector"
            else next(i for i, f in enumerate(fields) if f.path == "apr")
        )
        selector = (
            "#mortgage > td:nth-child(6)" if change == "selector" else "#mortgage > td:nth-child(5)"
        )
        fields[index] = fields[index].model_copy(update={"selector": selector})
    elif change == "label":
        fields[rate_index] = fields[rate_index].model_copy(
            update={
                "label": fields[rate_index].label.model_copy(
                    update={"selector": "#apr-label", "text": "APR"}
                )
            }
        )
    elif change == "target":
        row = row.model_copy(update={"comparison_target": None})
    elif change == "paths":
        fields = [f for f in fields if f.path != "apr"]
    else:
        fields[0] = fields[0].model_copy(update={"excerpt": "mortgage", "context_text": "mortgage"})
    changed = config.model_copy(
        update={"rows": (row.model_copy(update={"fields": tuple(fields)}),)}
    )
    assert changed.sha256 != config.sha256
    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        source, profile = prepare(conn, store)
        monkeypatch.setattr(registry, "load_configuration", lambda _: changed)
        monkeypatch.setattr(adapter_capture, "load_configuration", lambda _: changed)
        if boundary == "dispatch":
            with pytest.raises(ValueError, match="configuration digest"):
                registry.approved_adapter(source, approval)
        elif boundary == "extract":
            art = store.put(
                body,
                source_url=source.url,
                media_type="text/html",
                fetched_at=NOW,
                http_status=200,
                fetch_run_id="edited",
            )
            with pytest.raises(ValueError, match="configuration digest"):
                extract_stored_capture(conn, store, source, fetch_run(profile, art))
        else:
            offer = original
            if boundary == "new offer":
                offer = normalize_row(
                    extract(
                        changed, source, body, artifact, extraction_run_id="changed", git_sha=GIT
                    ).rows[0],
                    changed,
                    artifact,
                )
            with pytest.raises(ValueError, match="configuration digest"):
                verify_offer_against_stored_bytes(source, approval, offer, body, artifact)
    engine.dispose()


def test_prose_rate_changes_keep_configuration_digest() -> None:
    config, source, body, _, approval = inputs("prose")
    digest = config.sha256
    assert "4.111" not in config.model_dump_json() and "4.222" not in config.model_dump_json()
    body = body.replace(b"4.111%", b"4.112%").replace(b"4.222%", b"4.223%")
    _, _, _, artifact, _ = inputs("prose", body)
    result = extract(config, source, body, artifact, extraction_run_id="changed", git_sha=GIT)
    assert result.confidence == "medium" and config.sha256 == digest
    offer = normalize_row(result.rows[0], config, artifact)
    assert offer.note_rate and offer.note_rate.value == Decimal("4.112")
    assert verify_offer_against_stored_bytes(source, approval, offer, body, artifact).comparable


@pytest.mark.parametrize("kind", ["oversized", "deep"])
def test_html_resource_limits_fail_closed(kind: str) -> None:
    from cu_rates.pipeline.extract import resolve_text

    body = b"<div>" * 5000 + b"4.1%" + b"</div>" * 5000
    if kind == "oversized":
        body = b'<p id="note">' + b"x" * (2 * 1024 * 1024 + 1) + b"</p>"
    with pytest.raises(ValueError, match="limit"):
        resolve_text(body, "#note")
    config, source, _, artifact, _ = inputs(body=body)
    result = extract(config, source, body, artifact, extraction_run_id="limited", git_sha=GIT)
    assert result.confidence == "low" and any("limit" in d for d in result.diagnostics)


@pytest.mark.parametrize("tag", ["script", "style", "template", "noscript"])
def test_nonvisible_text_is_not_rate_evidence(tag: str) -> None:
    from cu_rates.pipeline.extract import resolve_text

    body = f'<p id="note">4.1%<{tag}>9.9%</{tag}></p>'.encode()
    assert resolve_text(body, "#note") == "4.1%"


@pytest.mark.parametrize("failure", ["normalize", "missing product", "validation"])
def test_row_errors_prevent_all_page_inserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from cu_rates.db.schema import extracted_candidates, slice_offer_captures, source_fingerprints
    from cu_rates.pipeline import adapter_capture

    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        source, profile = prepare(conn, store, "multi")
        if failure == "validation":
            insert_product(
                conn,
                ProductIdentity.model_validate(
                    dict(
                        schema_version="1.0",
                        product_id=inputs("multi")[0].rows[1].product_id,
                        institution_charter=8000401,
                        family="mortgage",
                        name="Wrong family",
                        lifecycle="active",
                        rate_availability="public_rate",
                        conforming_status="conforming",
                        government_guaranty="none",
                    )
                ),
            )
            insert_relationship(
                conn,
                InstitutionSourceRelationship(
                    schema_version="1.0",
                    relationship_id="multi.equity.relationship",
                    institution_charter=8000401,
                    source_id=source.source_id,
                    product_id=inputs("multi")[0].rows[1].product_id,
                    kind=SourceRelationshipKind.FIRST_PARTY,
                    approved_by="synthetic-human",
                    approved_at=NOW - timedelta(days=1),
                ),
            )

        def fail_second(
            row: RowExtraction, config: SourceConfiguration, artifact: RawArtifact
        ) -> NormalizedOffer:
            if row.row.offer_id != "multi.purchase":
                raise ValueError("second row failure")
            return normalize_row(row, config, artifact)

        if failure == "normalize":
            monkeypatch.setattr(adapter_capture, "normalize_row", fail_second)
        body = (FIXTURES / "multi.html").read_bytes()
        art = store.put(
            body,
            source_url=source.url,
            media_type="text/html",
            fetched_at=NOW,
            http_status=200,
            fetch_run_id="partial",
        )
        result = extract_stored_capture(conn, store, source, fetch_run(profile, art))
        assert result.confidence == "low" and not result.offers
        run = conn.execute(
            select(extraction_runs).where(extraction_runs.c.run_id == "extract-partial")
        ).one()
        assert run.status == "failed" and run.candidate_count == 0
        for table in (extracted_candidates, slice_offer_captures, source_fingerprints):
            assert not conn.execute(select(table)).all()
    engine.dispose()


def test_mortgage_alias_requires_purpose_without_configured_purpose() -> None:
    config, source, body, _, _ = inputs()
    body = body.replace(b"<td>mortgage</td>", b"<td>First mortgage</td>")
    _, _, _, artifact, _ = inputs(body=body)
    row = config.rows[0].model_copy(
        update={
            "comparison_target": None,
            "fields": tuple(
                f
                for f in config.rows[0].fields
                if f.path not in {"details.purpose", "details.rate_type", "details.lock_days"}
            ),
        }
    )
    config = config.model_copy(update={"rows": (row,)})
    result = extract(
        config, source, body, artifact, extraction_run_id="missing-purpose", git_sha=GIT
    )
    assert result.confidence == "low"
    assert any(
        "required fields missing: details.lock_days, details.purpose, details.rate_type" in d
        for d in result.diagnostics
    )


def test_located_claim_uses_approved_adapter() -> None:
    from cu_rates.pipeline.trusted import verify_located_claim

    config, _, body, artifact, approval = inputs("prose")
    claim = (
        extract(config, inputs("prose")[1], body, artifact, extraction_run_id="claim", git_sha=GIT)
        .rows[0]
        .candidates[6]
        .provenance
    )
    forged = claim.model_copy(
        update={"extraction": claim.extraction.model_copy(update={"parser": SLICE_PARSER})}
    )
    assert verify_located_claim(body, claim, approval=approval) == "4.111%"
    with pytest.raises(ValueError):  # a claim from another parser keeps the strict check
        verify_located_claim(body, forged, approval=approval)
    slice_approval = approval.model_copy(update={"adapter": SLICE_PARSER})
    with pytest.raises(ValueError):
        verify_located_claim(body, claim, approval=slice_approval)


def test_trusted_loader_skips_capture_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cu_rates.pipeline import trusted

    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        reviewed_adapter(conn, store)

        def fail_first(
            source: RateSource,
            approval: SourceApproval | None,
            offer: NormalizedOffer,
            body: bytes,
            artifact: RawArtifact,
            *,
            allow_fixture: bool = False,
        ) -> Verification:
            if artifact.fetch_run_id == "first":
                raise RecursionError("bad historical capture")
            return verify_offer_against_stored_bytes(
                source, approval, offer, body, artifact, allow_fixture=allow_fixture
            )

        monkeypatch.setattr(trusted, "verify_offer_against_stored_bytes", fail_first)
        view = load_publication_context(conn, "table.purchase", NOW, store=store)
        assert not evaluate_publication(view.offer, view.context).publishable_now
        assert {p.capture_id for p in view.context.evidence_paths} == {"second"}
    engine.dispose()


def test_extraction_parses_html_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from cu_rates.pipeline import extract as html

    config, source, body, artifact, _ = inputs("multi")
    calls = 0
    original = html._Tree.feed

    def counted(parser: html._Tree, data: str) -> None:
        nonlocal calls
        calls += 1
        original(parser, data)

    monkeypatch.setattr(html._Tree, "feed", counted)
    assert (
        extract(config, source, body, artifact, extraction_run_id="once", git_sha=GIT).confidence
        == "high"
    )
    assert calls == 1


@pytest.mark.parametrize("kind", ["oversized", "deep"])
@pytest.mark.parametrize("latest", [False, True])
def test_trusted_loader_isolates_resource_limited_captures(
    tmp_path: Path, kind: str, latest: bool
) -> None:
    from cu_rates.pipeline.trusted import insert_capture

    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        source, profile, _ = reviewed_adapter(conn, store)
        original = load_publication_context(conn, "table.purchase", NOW, store=store).offer
        body = b"<div>" * 5000 + b"x" + b"</div>" * 5000
        if kind == "oversized":
            body = b"x" * (2 * 1024 * 1024 + 1)
        when = NOW + timedelta(hours=4) if latest else NOW - timedelta(hours=2)
        art = store.put(
            body,
            source_url=source.url,
            media_type="text/html",
            fetched_at=when,
            http_status=200,
            fetch_run_id="malformed",
        )
        insert_run(conn, fetch_run(profile, art))
        insert_artifact(conn, art)
        old_artifact = next(original.iter_provenance()).artifact_id
        forged = NormalizedOffer.model_validate_json(
            original.model_copy(update={"observed_at": when})
            .model_dump_json()
            .replace(old_artifact, art.artifact_id)
        )
        insert_capture(conn, forged, "malformed")
        view = load_publication_context(conn, original.offer_id, max(NOW, when), store=store)
        assert "malformed" not in {p.capture_id for p in view.context.evidence_paths}
        if latest:
            assert view.offer.status is OfferStatus.QUARANTINED
            assert not evaluate_publication(view.offer, view.context).publishable_now
        else:
            assert {p.capture_id for p in view.context.evidence_paths} == {"first", "second"}
            assert evaluate_publication(view.offer, view.context).publishable_now
    engine.dispose()


def test_tree_text_and_walk_are_iterative() -> None:
    from cu_rates.pipeline.extract import _Node

    root = _Node("document", {})
    current = root
    for _ in range(5000):
        child = _Node("div", {}, current)
        current.parts.append(child)
        current = child
    current.parts.append("4.1%")
    assert root.text() == "4.1%"
    assert sum(1 for _ in root.walk()) == 5001


@pytest.mark.parametrize(
    "replacement",
    [
        b"Note rate: 4.111% (invented; autopay required)",
        b"Note rate: 4.111% 9.9% (invented)",
        b"4.111% Note rate: (invented)",
    ],
)
def test_prose_template_rejects_qualifiers_and_value_movement(replacement: bytes) -> None:
    config, source, body, _, _ = inputs("prose")
    body = body.replace(b"Note rate: 4.111% (invented)", replacement)
    _, _, _, artifact, _ = inputs("prose", body)
    result = extract(config, source, body, artifact, extraction_run_id="context", git_sha=GIT)
    assert result.confidence == "low" and any("context" in d for d in result.diagnostics)


def test_fixture_adapter_identity_requires_exact_version() -> None:
    _, source, _, _, approval = inputs()
    approval = approval.model_copy(
        update={
            "adapter": SLICE_PARSER.model_copy(
                update={"version": f"{SLICE_PARSER.version}+cfg.{'a' * 64}"}
            )
        }
    )
    with pytest.raises(ValueError, match="unknown adapter"):
        registry.approved_adapter(source, approval, allow_fixture=True)


def test_prose_literal_rate_configuration_fails_closed() -> None:
    config, source, body, artifact, _ = inputs("prose")
    fields = tuple(
        f.model_copy(
            update={
                "context_template": None,
                "excerpt": "4.111%",
                "context_text": "Note rate: 4.111% (invented)",
            }
        )
        if f.path == "note_rate"
        else f
        for f in config.rows[0].fields
    )
    config = config.model_copy(
        update={"rows": (config.rows[0].model_copy(update={"fields": fields}),)}
    )
    result = extract(config, source, body, artifact, extraction_run_id="literal", git_sha=GIT)
    assert result.confidence == "low" and any("rate excerpts" in d for d in result.diagnostics)


# ---- T040b: tolerant, section-scoped extraction of pages without ids -------------------


REAL = "real"


def real_inputs(
    body: bytes | None = None, **changes: object
) -> tuple[SourceConfiguration, RateSource, bytes, RawArtifact, SourceApproval]:
    config, source, page, artifact, approval = inputs(REAL, body)
    if changes:
        config = config.model_copy(update=changes)
        approval = approval.model_copy(update={"adapter": configuration_parser(config)})
    return config, source, page, artifact, approval


def run(
    body: bytes | None = None, **changes: object
) -> tuple[SourceConfiguration, "Extraction", RawArtifact]:
    config, source, page, artifact, _ = real_inputs(body, **changes)
    return (
        config,
        extract(config, source, page, artifact, extraction_run_id="r", git_sha=GIT),
        artifact,
    )


def with_rule(config: SourceConfiguration, path: str, **update: object) -> SourceConfiguration:
    rows = tuple(
        row.model_copy(
            update={
                "fields": tuple(
                    f.model_copy(update=update) if f.path == path else f for f in row.fields
                )
            }
        )
        for row in config.rows
    )
    return config.model_copy(update={"rows": rows})


def diagnostics(result: "Extraction") -> str:
    return "; ".join(result.diagnostics)


def test_real_page_shape_extracts_tolerantly_as_browse_only() -> None:
    config, result, artifact = run()
    assert result.confidence == "medium" and not result.diagnostics
    _, source, body, _, approval = real_inputs()
    for row in result.rows:
        offer = normalize_row(row, config, artifact)
        # the page states a term and nothing about the borrower: every assumption is unstated
        states = {m.dimension.value: m.state.value for m in offer.scenario_matches}
        assert states.pop("term_months") in {"matches", "conflicts"}
        assert states and set(states.values()) == {"unstated"}
        assert all(m.stated is None for m in offer.scenario_matches if m.state.value == "unstated")
        assert offer.details.kind == "fixed_equity"
        assert offer.effective_at == datetime(2026, 11, 18, 5, tzinfo=UTC)  # start of day, NY
        verified = verify_offer_against_stored_bytes(source, approval, offer, body, artifact)
        assert verified.confidence == "medium" and verified.comparable
    labels = [getattr(c.provenance.locator, "selector", "") for c in result.rows[0].candidates]
    assert "#loan-rates tbody tr:nth-child(3) > td:nth-child(1)" in labels
    # footnote markers are stripped from label and value text deterministically
    terms = {c.proposed_path: c.raw_value for c in result.rows[0].candidates}
    assert terms["details.term_months"] == "10 Year"


def test_imbalance_outside_the_section_is_tolerated_but_whole_document_mode_rejects_it() -> None:
    _, result, _ = run()
    assert result.confidence == "medium"
    legacy = real_inputs(sections=())[0].model_copy(update={"sections": ()})
    _, source, body, artifact, _ = real_inputs()
    strict = extract(legacy, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert strict.confidence == "low" and "invalid HTML" in diagnostics(strict)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"<tr><td>15 Year</td><td>5.333%</td><td>5.444%</td></tr>", b"<tr><td>15 Year</td>"),
        (b"<td>5.111%</td>", b"<td>5.111%</span></td>"),
        (b"</tbody>", b"</tbody></tbody>"),
        (b"<td>10 Year", b"<br></br><td>10 Year"),
    ],
)
def test_imbalance_inside_the_section_still_fails_closed(before: bytes, after: bytes) -> None:
    _, _, body, _, _ = real_inputs()
    assert before in body
    _, result, _ = run(body.replace(before, after))
    assert result.confidence == "low" and "invalid HTML" in diagnostics(result)


def test_unclosed_section_and_duplicate_section_id_fail_closed() -> None:
    _, _, body, _, _ = real_inputs()
    _, result, _ = run(body.replace(b'</section>\n<section id="mortgage-rates">', b"\n", 1)[:-30])
    assert result.confidence == "low"
    twin = body.replace(
        b"</footer>", b'<p id="rates-note">Rates accurate as of November 18, 2026.</p></footer>'
    )
    _, result, _ = run(twin)
    assert result.confidence == "low" and "uniquely" in diagnostics(result)


def test_layout_drift_inside_sections_only() -> None:
    _, _, body, _, _ = real_inputs()
    _, result, _ = run(body.replace(b"<footer>", b"<aside>new banner</aside><footer>"))
    assert result.confidence == "medium"  # outside the pinned sections: free to change
    _, result, _ = run(body.replace(b"<td>15 Year</td>", b"<td>15 Year</td><td>x</td>"))
    assert result.confidence == "low" and "layout drift" in diagnostics(result)
    _, result, _ = run(body.replace(b"<th>APR<sup>2</sup></th>", b"<th>APY<sup>2</sup></th>"))
    assert result.confidence == "low" and "layout drift" in diagnostics(result)


def test_selector_must_resolve_to_one_element_and_stay_in_sections() -> None:
    from cu_rates.pipeline.extract import resolve_text

    _, _, body, _, _ = real_inputs()
    ok = "#loan-rates tbody tr:nth-child(3) > td:nth-child(2)"
    assert resolve_text(body, ok, tolerant=True) == "5.111%"
    assert resolve_text(body, "#loan-rates tr:nth-child(4) td:nth-child(3)", tolerant=True) == (
        "5.444%"
    )
    assert resolve_text(body, "#loan-rates caption:nth-of-type(1)", tolerant=True)
    for ambiguous in (
        "#loan-rates tbody tr > td:nth-child(2)",  # many rows
        "#loan-rates td:nth-child(1)",
        "#loan-rates tbody tr:nth-child(40) > td:nth-child(1)",  # none
        "#loan-rates tbody tr:nth-child(3) > td:nth-child(9)",
        "#loan-rates tbody > td",  # td is not a direct child of tbody
    ):
        with pytest.raises(ValueError):
            resolve_text(body, ambiguous, tolerant=True)
    for malformed in ("#loan-rates tbody tr:nth-child(0)", "#loan-rates >", "#loan-rates  tr"):
        with pytest.raises(ValueError):
            resolve_text(body, malformed, tolerant=True)


def test_configuration_selectors_must_lie_inside_the_declared_sections() -> None:
    config = real_inputs()[0]
    outside = config.rows[0].fields[0].model_copy(update={"selector": "#mortgage-rates caption"})
    row = config.rows[0].model_copy(update={"fields": (outside, *config.rows[0].fields[1:])})
    payload = config.model_copy(update={"rows": (row,)}).model_dump(mode="json")
    with pytest.raises(ValueError, match="outside the configured sections"):
        SourceConfiguration.model_validate(payload)


def test_whole_document_mode_resolves_structural_selectors_after_balanced_parse() -> None:
    from cu_rates.pipeline.extract import resolve_text

    body = (
        b'<html><body><div id="a"><table><tbody><tr><td>x</td><td>y</td></tr></tbody></table></div>'
        b"</body></html>"
    )
    assert resolve_text(body, "#a tbody tr:nth-child(1) > td:nth-child(2)") == "y"
    with pytest.raises(ValueError, match="balanced"):
        resolve_text(body.replace(b"</div>", b""), "#a td:nth-child(1)")


def test_footnote_markers_are_stripped_only_by_tag_not_by_guessing() -> None:
    from cu_rates.pipeline.extract import resolve_text

    _, _, body, _, _ = real_inputs()
    assert resolve_text(body, "#loan-rates thead th:nth-child(2)", tolerant=True) == "Rate"
    # a bare '*' is not a recognised footnote marker: the label (and the pin) no longer match
    _, result, _ = run(body.replace(b"<sup>1</sup>", b"*"))
    assert result.confidence == "low"
    # nor is one inside a value cell removed
    _, result, _ = run(body.replace(b"<td>5.111%</td>", b"<td>5.111%*</td>"))
    assert result.confidence == "low"


def test_group_header_context_and_prose_date_cap_confidence_at_medium() -> None:
    config, result, _ = run()
    assert result.confidence == "medium"
    # No group header and no prose date anywhere: the tier is decided by literal evidence.
    plain = config.rows[0].model_copy(
        update={
            "fields": tuple(
                f.model_copy(update={"group_header": False, "date": None, "path": f.path})
                for f in config.rows[0].fields
                if f.path != "effective_at"
            )
        }
    )
    changed = config.model_copy(update={"rows": (plain,)})
    changed = changed.model_copy(update={"layout_sha256": changed.layout_of(real_inputs()[2])})
    _, source, body, artifact, _ = real_inputs()
    again = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert again.confidence == "high" and not again.diagnostics
    # the group_header marker alone must lower it
    flagged = plain.model_copy(
        update={
            "fields": tuple(
                f.model_copy(update={"group_header": f.path == "product_family"})
                for f in plain.fields
            )
        }
    )
    cfg = changed.model_copy(update={"rows": (flagged,)})
    assert (
        extract(cfg, source, body, artifact, extraction_run_id="r", git_sha=GIT).confidence
        == "medium"
    )


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (b"November 18, 2026", b"Nov 18, 2026", "date context"),
        (b"November 18, 2026", b"November 18 2026", "date context"),
        (b"November 18, 2026", b"November 31, 2026", "out of range"),
        (b"November 18, 2026", b"November 18, 2027", "follows source observation"),
        (b"November 18, 2026", b"November 018, 2026", "date context"),
        (b"Rates and APR accurate as of", b"Rates were accurate as of", "date context"),
        (b"Rates and APR accurate", b"Preliminary: Rates and APR accurate", "date context"),
        (b"2026.</p>", b"2026, subject to change.</p>", "date context"),
        (b"November 18, 2026", b"November 18, 2026 and November 19, 2026", "date context"),
    ],
)
def test_prose_date_is_strict_and_never_inferred(before: bytes, after: bytes, reason: str) -> None:
    _, _, body, _, _ = real_inputs()
    _, result, _ = run(body.replace(before, after))
    assert result.confidence == "low" and reason in diagnostics(result)


def test_prose_date_formats_and_timezone_are_reviewed_configuration() -> None:
    from cu_rates.adapters.static_html import DateRule

    config = real_inputs()[0]
    rule = config.rows[0].fields[-1].date
    assert rule is not None
    iso = (FIXTURES / "real.html").read_bytes().replace(b"November 18, 2026", b"2026-11-18")
    changed = with_rule(
        config, "effective_at", date=rule.model_copy(update={"format": "YYYY-MM-DD"})
    )
    changed = changed.model_copy(update={"layout_sha256": changed.layout_of(iso)})
    _, source, body, artifact, _ = inputs(REAL, iso)
    result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert result.confidence == "medium", diagnostics(result)
    effective = normalize_row(result.rows[0], changed, artifact).effective_at
    assert effective == datetime(2026, 11, 18, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="unknown timezone"):
        DateRule(template=rule.template, format="Month D, YYYY", timezone="Mars/Base")


def test_date_rule_requires_one_slot_and_plain_effective_field() -> None:
    from cu_rates.adapters.static_html import DateRule, Field

    for template in ("accurate as of", "{value} and {value}"):
        with pytest.raises(ValueError, match="exactly one"):
            DateRule(template=template, format="Month D, YYYY", timezone="UTC")
    config = real_inputs()[0]
    rule = config.rows[0].fields[-1].date
    assert rule is not None
    rate = config.rows[0].fields[2].model_dump(mode="json")
    with pytest.raises(ValueError, match="plain effective_at"):
        Field.model_validate(rate | {"date": rule.model_dump(mode="json")})


def test_no_prose_date_means_no_effective_time_and_no_invention() -> None:
    config, source, body, artifact, _ = real_inputs()
    row = config.rows[0]
    row = row.model_copy(
        update={"fields": tuple(f for f in row.fields if f.path != "effective_at")}
    )
    changed = config.model_copy(update={"rows": (row,)})
    changed = changed.model_copy(update={"layout_sha256": changed.layout_of(body)})
    result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert result.confidence == "medium", diagnostics(result)  # gate's review path owns this
    assert normalize_row(result.rows[0], changed, artifact).effective_at is None
    # ... but a row that is not browse-only still requires its effective time.
    comparable = row.model_copy(update={"browse_only": False})
    assert "effective_at" in required_paths(comparable, ())


def required_paths(row: object, candidates: tuple[object, ...]) -> set[str]:
    from cu_rates.adapters.static_html import required_paths as real

    return real(row, candidates)  # type: ignore[arg-type]


def test_browse_only_rows_never_carry_a_scenario_or_assumptions() -> None:
    from cu_rates.adapters.static_html import Row

    config = real_inputs()[0]
    base = config.rows[0].model_dump()
    with pytest.raises(ValueError, match="browse-only"):
        Row.model_validate(base | {"comparison_target": None})
    pinned = dict(base["fields"][1], path="scenario.credit_score")
    with pytest.raises(ValueError, match="browse-only"):
        Row.model_validate(base | {"fields": [*base["fields"], pinned]})


def test_assumptions_the_page_does_not_state_are_not_required_or_invented() -> None:
    config = real_inputs()[0]
    browse = config.rows[0]
    equity = {"product_family": "home_equity_loan"}
    cands: tuple[object, ...] = ()
    paths = required_paths(browse, cands)
    assert not any(p.startswith("scenario.") for p in paths)
    assert "details.lock_days" not in paths and "points" not in paths
    mortgage = type("C", (), {"proposed_path": "product_family", "raw_value": "mortgage"})()
    mortgage_paths = required_paths(browse, (mortgage,))
    # purpose and rate type are contract-required for a mortgage; lock days are not
    assert {"details.purpose", "details.rate_type"} <= mortgage_paths
    assert "details.lock_days" not in mortgage_paths
    ranked = browse.model_copy(update={"browse_only": False})
    assert {"details.lock_days", "points", "effective_at"} <= required_paths(ranked, (mortgage,))
    assert equity


def test_mortgage_row_with_unstated_purpose_is_refused_not_defaulted() -> None:
    """Contract: a mortgage offer's kind depends on a stated purpose. Never guessed."""
    config, source, body, artifact, _ = real_inputs()
    section = "#mortgage-rates tbody tr:nth-child"
    base = config.rows[0]

    def f(path: str, selector: str, label: str, text: str, **kw: object) -> object:
        return type(base.fields[0]).model_validate(
            dict(path=path, selector=selector, label=dict(selector=label, text=text), **kw)
        )

    row = base.model_copy(
        update={
            "offer_id": "real.mortgage",
            "product_id": "real.mortgage-product",
            "comparison_target": CanonicalScenarioId.PURCHASE_30Y_FIXED,
            "fields": (
                f(
                    "product_family",
                    f"{section}(1) > td:nth-child(1)",
                    "#mortgage-rates caption",
                    "First mortgage rates",
                    group_header=True,
                ),
                f(
                    "details.rate_type",
                    f"{section}(2) > td:nth-child(1)",
                    "#mortgage-rates caption",
                    "First mortgage rates",
                    group_header=True,
                ),
                f(
                    "details.term_months",
                    f"{section}(3) > td:nth-child(1)",
                    "#mortgage-rates thead th:nth-child(1)",
                    "Term",
                ),
                f(
                    "note_rate",
                    f"{section}(3) > td:nth-child(2)",
                    "#mortgage-rates thead th:nth-child(2)",
                    "Rate",
                ),
                f(
                    "apr",
                    f"{section}(3) > td:nth-child(3)",
                    "#mortgage-rates thead th:nth-child(3)",
                    "APR",
                ),
            ),
        }
    )
    changed = config.model_copy(
        update={"sections": (*config.sections, "mortgage-rates"), "rows": (row,)}
    )
    changed = changed.model_copy(update={"layout_sha256": changed.layout_of(body)})
    result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert result.confidence == "low"
    assert "details.purpose" in diagnostics(result)
    # configuring a purpose from a node that does not state one is also refused
    with_purpose = row.model_copy(
        update={
            "fields": (
                *row.fields,
                f(
                    "details.purpose",
                    f"{section}(1) > td:nth-child(1)",
                    "#mortgage-rates caption",
                    "First mortgage rates",
                ),
            )
        }
    )
    changed = changed.model_copy(update={"rows": (with_purpose,)})
    result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert result.confidence == "low"


def test_ranked_row_cannot_be_declared_on_a_page_that_states_no_assumptions() -> None:
    config, source, body, artifact, _ = real_inputs()
    row = config.rows[0].model_copy(update={"browse_only": False})
    changed = config.model_copy(update={"rows": (row,)})
    result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
    assert result.confidence == "low"
    assert "required fields missing" in diagnostics(result)


def zone_rule(config: SourceConfiguration, zone: str) -> object:
    rule = config.rows[0].fields[-1].date
    assert rule is not None
    return rule.model_copy(update={"timezone": zone})


def test_new_configuration_fields_are_bound_into_the_approved_identity() -> None:
    config = real_inputs()[0]
    digests = {config.sha256}
    variants = [
        config.model_copy(
            update={"sections": ("loan-rates", "rates-heading", "rates-note", "mortgage-rates")}
        ),
        with_rule(config, "product_family", group_header=False),
        with_rule(config, "effective_at", date=zone_rule(config, "UTC")),
        config.model_copy(
            update={"rows": (config.rows[0].model_copy(update={"browse_only": False}),)}
        ),
    ]
    for variant in variants:
        digests.add(variant.sha256)
        assert configuration_parser(variant) != configuration_parser(config)
    assert len(digests) == 5
    assert configuration_parser(config).version.startswith(f"{PARSER.version}+cfg.")
    _, source, body, artifact, approval = real_inputs()
    row = extract(config, source, body, artifact, extraction_run_id="r", git_sha=GIT).rows[0]
    offer = normalize_row(row, config, artifact)
    stale = approval.model_copy(update={"adapter": configuration_parser(variants[2])})
    with pytest.raises(ValueError, match="digest is not approved"):
        verify_offer_against_stored_bytes(source, stale, offer, body, artifact)


def test_scoped_parse_keeps_size_and_depth_caps() -> None:
    from cu_rates.pipeline.extract import resolve_text

    deep = b'<p id="note">' + b"<div>" * 5000 + b"4.1%" + b"</div>" * 5000 + b"</p>"
    with pytest.raises(ValueError, match="limit"):
        resolve_text(deep, "#note", tolerant=True)
    big = b"<span>" + b"x" * (2 * 1024 * 1024) + b'</span><p id="note">4.1%</p>'
    with pytest.raises(ValueError, match="limit"):
        resolve_text(big, "#note", tolerant=True)
    # unbounded depth OUTSIDE the section is never built into a tree
    assert (
        resolve_text(b"<div>" * 5000 + b'<p id="note">4.1%</p>', "#note", tolerant=True) == "4.1%"
    )
    config, source, _, artifact, _ = real_inputs()
    for hostile in (deep, big):
        art = artifact.model_copy(
            update={"artifact_id": sha256(hostile).hexdigest(), "byte_length": len(hostile)}
        )
        out = extract(config, source, hostile, art, extraction_run_id="r", git_sha=GIT)
        assert out.confidence == "low"


def test_scoped_parse_reads_only_the_section_once_per_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cu_rates.pipeline import extract as html

    calls = 0
    original = html._ScopedTree.feed

    def counted(parser: html._ScopedTree, data: str) -> None:
        nonlocal calls
        calls += 1
        original(parser, data)

    monkeypatch.setattr(html._ScopedTree, "feed", counted)
    config, result, _ = run()
    assert result.confidence == "medium" and calls == len(config.sections)


def test_text_outside_sections_and_script_content_is_never_evidence() -> None:
    _, _, body, _, _ = real_inputs()
    poisoned = body.replace(b'var tracker = "<div></span>";', b'var rate = "9.999%";')
    _, result, _ = run(poisoned)
    assert result.confidence == "medium"
    values = {c.raw_value for r in result.rows for c in r.candidates}
    assert not any("9.999" in v for v in values)


def test_layout_for_trusted_snapshot_uses_the_adapters_own_reading() -> None:
    _, source, body, _, approval = real_inputs()
    assert registry.body_layout_sha256(source, approval, body) == real_inputs()[0].layout_sha256
    with pytest.raises(ValueError):
        registry.body_layout_sha256(source, approval, body.replace(b"</table>", b"", 1))


def test_stored_locators_resolve_tolerantly_only_for_the_approved_adapter() -> None:
    from cu_rates.pipeline.trusted import verify_located_claim

    _, result, _ = run()
    _, _, body, _, approval = real_inputs()
    provenance = result.rows[0].candidates[1].provenance
    assert verify_located_claim(body, provenance, approval=approval)
    with pytest.raises(ValueError):  # identity/correction evidence keeps the strict parse
        verify_located_claim(body, provenance)


def test_fragment_boundary_allows_a_sentence_period_but_not_a_joined_number() -> None:
    from cu_rates.adapters.static_html import fragment_is_located

    assert fragment_is_located("accurate as of November 18, 2026.", "November 18, 2026")
    assert fragment_is_located("Rate 4.1% today.", "4.1%")
    assert not fragment_is_located("Rate 4.1%.5", "4.1%")
    assert not fragment_is_located("Rate 14.1%", "4.1%")
    assert not fragment_is_located("5.1.5%", "1.5%")
    assert not fragment_is_located("11%", "1%") and not fragment_is_located("1.5%", "1")


def test_browse_only_capture_reaches_the_gate_as_browse_only_never_ranked(tmp_path: Path) -> None:
    from cu_rates.contracts.publication import ReasonCode

    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "private")
    engine = open_operational_database(database)
    with engine.begin() as conn:
        source, profile = prepare(conn, store, REAL, family="home_equity_loan")
        body = (FIXTURES / "real.html").read_bytes()
        for when, run_id, tail in (
            (NOW - timedelta(hours=4), "first", b"\n"),
            (NOW, "second", b""),
        ):
            artifact = store.put(
                body + tail,
                source_url=source.url,
                media_type="text/html",
                fetched_at=when,
                http_status=200,
                fetch_run_id=run_id,
            )
            captured = extract_stored_capture(conn, store, source, fetch_run(profile, artifact))
            assert captured.confidence == "medium", captured.diagnostics
            assert len(captured.offers) == 2
        view = load_publication_context(conn, "real.ten", NOW, store=store)
        decision = evaluate_publication(view.offer, view.context)
        assert ReasonCode.SCENARIO_UNSTATED in decision.reasons, decision
        assert decision.placement.value == "browse_only", decision
    engine.dispose()


# ---- T040b review fixes ---------------------------------------------------------------


def test_superscript_is_stripped_only_as_an_unambiguous_trailing_marker() -> None:
    from cu_rates.pipeline.extract import resolve_text

    def read(markup: bytes) -> str:
        return resolve_text(b'<p id="n">' + markup + b"</p>", "#n", tolerant=True)

    assert read(b"<span>6<sup>.125</sup>%</span>") == "6.125%"  # not a marker: ordinary text
    assert read(b"6.125%<sup>1</sup>") == "6.125%"
    assert read(b"6.125% <sup>*</sup> ") == "6.125%"
    assert read(b"<b>Rate</b><sup>12</sup>") == "Rate"
    assert read(b"<span>6.125%<sup>1</sup></span>&nbsp;<!-- c -->") == "6.125%"
    assert read(b"6.125%<sup>123</sup>") == "6.125%123"  # not a 1-2 digit marker: kept
    assert read(b"6.125%<sup>a</sup>") == "6.125%a"
    for ambiguous in (
        b"6.12<sup>5</sup>",
        b"6.12<sup>5</sup>&nbsp;",
        b"<span>6.12<sup>5</sup></span>%",
        b"<b>6.12<sup>5</sup></b>",
        b"6.125<sup>1</sup>%",
        b"5<sup>1</sup>.5%",
        b"6.<sup>1</sup>",
        b"Rate<sup>1</sup> and more",
        b"<sup>1</sup>5%",
        b"<span>6.12</span><sup>5</sup>",
    ):
        with pytest.raises(ValueError, match="ambiguous footnote"):
            read(ambiguous)
    _, _, body, _, _ = real_inputs()
    _, result, _ = run(body.replace(b"<td>5.111%</td>", b"<td>5.11<sup>1</sup>%</td>"))
    assert result.confidence == "low" and "ambiguous footnote" in diagnostics(result)


@pytest.mark.parametrize(
    "wrapper",
    [b"noscript", b"template", b"details", b'div hidden=""', b'div aria-hidden="true"'],
)
def test_sections_inside_hidden_or_nonvisible_ancestors_are_refused(wrapper: bytes) -> None:
    from cu_rates.pipeline.extract import resolve_text

    close = wrapper.split()[0]
    body = b"<html><body><" + wrapper + b'><p id="n">4.1%</p></' + close + b"></body></html>"
    with pytest.raises(ValueError, match="non-visible or hidden"):
        resolve_text(body, "#n", tolerant=True)
    assert resolve_text(b'<div><p id="n">4.1%</p></div>', "#n", tolerant=True) == "4.1%"


def test_section_ancestors_are_pinned_by_the_layout_hash() -> None:
    config, _, body, _, _ = real_inputs()
    other = body.replace(b'<div class="wrap">', b'<div class="other">')
    assert config.layout_of(other) != config.layout_of(body)  # only an ancestor differs
    _, result, _ = run(other)
    assert result.confidence == "low" and "layout drift" in diagnostics(result)
    _, result, _ = run(body.replace(b'<div class="wrap">', b'<div class="wrap" hidden="">'))
    assert result.confidence == "low"


def test_scoped_resolution_follows_the_approved_configuration() -> None:
    from cu_rates.pipeline.trusted import verify_located_claim

    config, source, body, artifact, approval = inputs("table")
    claim = (
        extract(config, source, body, artifact, extraction_run_id="c", git_sha=GIT)
        .rows[0]
        .candidates[0]
        .provenance
    )
    assert registry.approved_sections(approval) == ()
    assert verify_located_claim(body, claim, approval=approval)
    # Whole-document configuration: an unbalanced page is not re-resolved tolerantly.
    broken = body.replace(b"</head>", b"</link></head>")
    with pytest.raises(ValueError):
        verify_located_claim(broken, claim, approval=approval)
    assert registry.approved_sections(real_inputs()[4]) == real_inputs()[0].sections


def test_prose_date_claims_are_read_only_under_their_own_rule_and_zone() -> None:
    from cu_rates.normalize._values import prose_rule_id
    from cu_rates.validation._rules import _claim_value

    _, result, _ = run()
    dated = next(c for c in result.rows[0].candidates if c.proposed_path == "effective_at")
    adapter = dated.provenance.extraction
    text = "November 18, 2026"
    assert adapter.rule_id == prose_rule_id("America/New_York")
    with pytest.raises(ValueError):
        _claim_value("effective_at", text)  # ordinary claim: no date-only reading
    assert _claim_value("effective_at", text, adapter) == datetime(2026, 11, 18, 5, tzinfo=UTC)
    utc = adapter.model_copy(update={"rule_id": prose_rule_id("UTC")})
    assert _claim_value("effective_at", text, utc) == datetime(2026, 11, 18, tzinfo=UTC)
    assert _claim_value("effective_at", "November 19, 2026", adapter) != datetime(
        2026, 11, 18, 5, tzinfo=UTC
    )
    # Other evidence cannot claim the rule: it needs the static-html adapter and a digest.
    other = adapter.model_copy(update={"parser": SLICE_PARSER})
    with pytest.raises(ValueError):
        _claim_value("effective_at", text, other)
    unbound = adapter.model_copy(update={"configuration_sha256": ""})
    with pytest.raises(ValueError):
        _claim_value("effective_at", text, unbound)


def test_label_and_value_must_share_the_table_row() -> None:
    config, source, body, artifact, _ = real_inputs()
    field = config.rows[0].fields[2]  # note_rate, value in tbody row 3
    for label in (
        "#loan-rates tbody tr:nth-child(4) > td:nth-child(2)",  # the next body row
        "#loan-rates tbody tr:nth-of-type(4) > td:nth-of-type(2)",  # written differently
        "#loan-rates tr:nth-child(4) td:nth-child(2)",  # descendant spelling
    ):
        moved = field.model_copy(
            update={"label": field.label.model_copy(update={"selector": label, "text": "5.333%"})}
        )
        row = config.rows[0].model_copy(
            update={"fields": tuple(moved if f is field else f for f in config.rows[0].fields)}
        )
        changed = config.model_copy(update={"rows": (row,)})
        changed = changed.model_copy(update={"layout_sha256": changed.layout_of(body)})
        result = extract(changed, source, body, artifact, extraction_run_id="r", git_sha=GIT)
        assert result.confidence == "low" and "different table rows" in diagnostics(result)
    same = field.model_copy(
        update={
            "label": field.label.model_copy(
                update={
                    "selector": "#loan-rates tbody tr:nth-of-type(3) > td:nth-of-type(1)",
                    "text": "10 Year",
                }
            )
        }
    )
    row = config.rows[0].model_copy(
        update={"fields": tuple(same if f is field else f for f in config.rows[0].fields)}
    )
    ok = config.model_copy(update={"rows": (row,)})
    ok = ok.model_copy(update={"layout_sha256": ok.layout_of(body)})
    assert extract(ok, source, body, artifact, extraction_run_id="r", git_sha=GIT).confidence == (
        "medium"
    )


def test_verification_tokenizes_a_stored_body_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from cu_rates.pipeline import extract as html
    from cu_rates.pipeline.trusted import verify_located_claim

    _, result, _ = run()
    _, _, body, _, approval = real_inputs()
    html._DOCUMENTS.clear()
    calls = 0
    original = html._ScopedTree.feed

    def counted(parser: html._ScopedTree, data: str) -> None:
        nonlocal calls
        calls += 1
        original(parser, data)

    monkeypatch.setattr(html._ScopedTree, "feed", counted)
    claims = [c.provenance for c in result.rows[0].candidates if c.proposed_path != "effective_at"]
    for claim in claims * 3:
        verify_located_claim(body, claim, approval=approval)
    assert calls == 1  # all claims lie in the one loan-rates section


# ---- T040b round 2 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "markup",
    [
        b"<span hidden>9%</span>4.1%",
        b'<span aria-hidden="TRUE">9%</span>4.1%',
        b'<span style="color:red; Display : None">9%</span>4.1%',
        b'<span style="visibility:hidden">9%</span>4.1%',
        b"<details>9%</details>4.1%",
    ],
)
def test_hidden_content_inside_a_selected_node_is_refused(markup: bytes) -> None:
    from cu_rates.pipeline.extract import resolve_text

    with pytest.raises(ValueError, match="hidden"):
        resolve_text(b'<p id="n">' + markup + b"</p>", "#n", tolerant=True)
    assert resolve_text(b'<p id="n"><span>4.1%</span></p>', "#n", tolerant=True) == "4.1%"


@pytest.mark.parametrize(
    "outer",
    [
        b'<p id="n" hidden>4.1%</p>',
        b'<p id="n" aria-hidden="True">4.1%</p>',
        b'<p id="n" style="DISPLAY:NONE">4.1%</p>',
        b'<div style="display: none"><p id="n">4.1%</p></div>',
    ],
)
def test_hidden_section_or_ancestor_is_refused(outer: bytes) -> None:
    from cu_rates.pipeline.extract import resolve_text

    with pytest.raises(ValueError, match="hidden"):
        resolve_text(b"<body>" + outer + b"</body>", "#n", tolerant=True)


def test_a_subscript_inside_selected_text_is_refused() -> None:
    from cu_rates.pipeline.extract import resolve_text

    with pytest.raises(ValueError, match="subscript"):
        resolve_text(b'<p id="n">6.12<sub>5</sub>%</p>', "#n", tolerant=True)


def test_hidden_wrapper_inside_a_section_cannot_be_unhidden_by_a_stray_end_tag() -> None:
    from cu_rates.pipeline.extract import resolve_text

    for body in (
        b'<div id="s"><div hidden><b>4.1%</div></b></div>',  # stray/misordered end tags inside
        b'<div id="s"><div hidden>4.1%</div></span></div>',
    ):
        with pytest.raises(ValueError):
            resolve_text(body, "#s div", tolerant=True)
    with pytest.raises(ValueError, match="hidden"):
        resolve_text(b'<div id="s"><div hidden><p>4.1%</p></div></div>', "#s div p", tolerant=True)
    with pytest.raises(ValueError, match="balanced"):  # a stray end tag is itself an error
        resolve_text(b'<div id="s"><div hidden></span><p>4.1%</p></div></div>', "#s", tolerant=True)
    # Outside: an end tag that skips open tags may be ignored by a browser, so it never unhides.
    outside = b'<div hidden><table><tr><td></div><p id="n">4.1%</p></td></tr></table></div>'
    with pytest.raises(ValueError, match="non-visible or hidden"):
        resolve_text(outside, "#n", tolerant=True)


def test_documents_cache_is_thread_safe() -> None:
    import threading

    from cu_rates.pipeline import extract as html

    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            for turn in range(40):
                body = b'<p id="n">v%d</p>' % ((index + turn) % 5)
                assert html.resolve_text(body, "#n", tolerant=True).startswith("v")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors


def test_approved_sections_reads_the_configuration_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, _, approval = real_inputs()
    registry._sections_for.cache_clear()
    reads = 0
    original = SourceConfiguration.model_validate_json

    def counted(*args: object, **kwargs: object) -> SourceConfiguration:
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        SourceConfiguration,
        "model_validate_json",
        classmethod(lambda cls, *a, **k: counted(*a, **k)),
    )
    for _ in range(5):
        assert registry.approved_sections(approval)
    assert reads == 1

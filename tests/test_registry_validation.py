"""Isolated registry rules over real migrated DB rows and synthetic contracts."""

import hashlib
import json
import random
import socket
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts import (
    FindingReviewState,
    ProductFamily,
    ProductFinding,
    ProductScope,
    ProviderKind,
    RateAvailability,
    ServiceProvider,
    SourceRelationshipKind,
)
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db import schema as s
from cu_rates.db.records import persist_chain, persist_review_decision
from cu_rates.pipeline.trusted import insert_approval, insert_finding
from cu_rates.registry_validation import (
    CoverageState,
    Reason,
    Registry,
    RegistryReport,
    main,
    read_registry,
    report_from_database,
    valid_source_url,
    validate_registry,
)
from cu_rates.roster import RosterImporter
from tests.contracts import factories as f

NOW = f.T0 + timedelta(hours=1)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    path = tmp_path / "registry.sqlite"
    upgrade_database(path)
    engine = open_operational_database(path)
    yield engine, path
    engine.dispose()


def seed(
    engine: Engine,
    tmp_path: Path,
    *,
    availability: RateAvailability = RateAvailability.PUBLIC_RATE,
    finding: bool = True,
    charter_verified: bool = False,
    approved_offer: bool = False,
    approved_product: bool = False,
    approved_source: bool = True,
) -> Registry:
    """One minimal provider chain, plus an actual local MA roster import."""
    raw = (
        "Charter,Credit Union Name,State,Status,City,Date Inactive,Successor Charter,As of Date\n"
        f"{f.CHARTER},Example Synthetic Credit Union,MA,Active,Boston,,,2026-11-15\n"
    ).encode()
    RosterImporter(engine, ArtifactStore(tmp_path / "evidence"), clock=lambda: f.T0).import_roster(
        raw, source_version="synthetic-v1"
    )
    product = f.product(rate_availability=availability)
    offer = f.purchase_offer(status="approved" if approved_offer else "candidate")
    provider = ServiceProvider(
        schema_version=f.SV,
        provider_id="provider-1",
        name="Synthetic",
        kind=ProviderKind.LOAN_PLATFORM,
    )
    with engine.begin() as conn:
        # Import deliberately leaves the charter unverified.
        if charter_verified:
            conn.execute(s.institutions.update().values(charter_verified=1))
        persist_chain(
            conn,
            institution=f.institution(charter_verified=charter_verified),
            provider=provider,
            source=f.rate_source(),
            product=product,
            relationship=f.relationship(
                kind=SourceRelationshipKind.PROVIDER_HOSTED, provider_id=provider.provider_id
            ),
            profile=f.fetch_profile(),
            run=f.fetch_run(),
            artifact=f.raw_artifact(),
            offer=offer,
            review=f.review(offer),
        )
        if finding:
            item = ProductFinding(
                schema_version=f.SV,
                taxonomy_version="1.0",
                finding_id="finding-1",
                institution_charter=f.CHARTER,
                source_id=f.SOURCE_ID,
                product_id=f.PRODUCT_ID,
                observed_at=f.T0,
                label="Synthetic catalog",
                scope=ProductScope.IN_SCOPE,
                family=ProductFamily.MORTGAGE,
                rate_availability=availability,
                evidence=(f.provenance(),),
                review_state=FindingReviewState.VERIFIED,
                reviewed_by="Reviewer",
                reviewed_at=f.T0,
                review_rationale="Synthetic evidence checked",
            )
            insert_finding(conn, item)
        if approved_source:
            insert_approval(
                conn, f.source_approval(evidence=(f.provenance(),), decided_at=f.T0), f.CHARTER
            )
        if approved_product:
            persist_review_decision(
                conn,
                f.product_review(reviewed=product, evidence=(f.provenance(),)),
                institution_charter=f.CHARTER,
            )
    with engine.connect() as conn:
        return read_registry(conn)


def alter(registry: Registry, table: str, **changes: Any) -> Registry:
    """Change a detached DB row, including corruptions SQL constraints prohibit."""
    rows = list(registry.rows(table))
    assert rows, table
    rows[0] = dict(rows[0], **changes)
    return Registry(dict(registry.tables, **{table: tuple(rows)}))


def without(registry: Registry, table: str) -> Registry:
    return Registry(dict(registry.tables, **{table: ()}))


def audit(registry: Registry) -> RegistryReport:
    return validate_registry(registry, as_of=NOW)


def has(report: RegistryReport, reason: Reason, entity: str | None = None) -> bool:
    return any(
        item.reason == reason and (entity is None or item.entity == entity)
        for item in report.findings
    )


def mortgage_state(report: RegistryReport) -> CoverageState:
    return next(slot.state for slot in report.institutions[0].coverage if slot.family == "mortgage")


def test_required_identifiers(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.INVALID_IDENTIFIER)
    invalid: tuple[dict[str, Any], ...] = (
        {"legal_name": "  "},
        {"charter_number": 0},
        {"charter_number": -1},
        {"charter_number": 10**8},
        {"charter_number": None},
    )
    for changes in invalid:
        assert has(audit(alter(registry, "institutions", **changes)), Reason.INVALID_IDENTIFIER)
    row = dict(registry.rows("institutions")[0])
    for field in ("charter_number", "legal_name"):
        row = dict(registry.rows("institutions")[0])
        del row[field]
        missing = Registry(dict(registry.tables, institutions=(row,)))
        assert has(audit(missing), Reason.INVALID_IDENTIFIER)


def test_ma_scope_requires_roster(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.MA_SCOPE_UNPROVEN)
    assert has(audit(without(registry, "roster_import_entries")), Reason.MA_SCOPE_UNPROVEN)
    assert has(audit(without(registry, "roster_imports")), Reason.MA_SCOPE_UNPROVEN)


INVALID_URLS = [
    "http://example.invalid/rates",
    "https://user:pass@example.invalid/rates",
    "https://example.invalid/rates?token=secret",
    "https://example.invalid/rates?",
    "https://example.invalid/rates#fragment",
    "https://example.invalid/rates#",
    "https://127.0.0.1/rates",
    "https://10.0.0.1/rates",
    "https://192.168.0.1/",
    "https://169.254.1.1/",
    "https://[::1]/",
    "https://[fe80::1]/",
    "https://8.8.8.8/",
    "https://localhost/",
    "https://rates.localhost/",
    "https://rates.local/",
    "https://example.invalid./rates",
    "https://EXAMPLE.invalid/rates",
    "HTTPS://example.invalid/",
    "https://ex\u0430mple.invalid/",
    "https://xn--exmple-4nf.invalid/",
    "https://example.invalid:443/",
    "https://example.invalid\\@localhost/",
    "https://2130706433/",
    "https://0x7f000001/",
    "https://127.1/",
    "https://bad_host.invalid/",
    "https://-bad.invalid/",
    "https://example.invalid/\n",
]


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_source_urls(database: tuple[Engine, Path], tmp_path: Path, url: str) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.INVALID_SOURCE_URL)
    assert has(audit(alter(registry, "rate_sources", url=url)), Reason.INVALID_SOURCE_URL)
    assert not valid_source_url(url)


def test_urls_do_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert valid_source_url("https://rates.example.invalid/mortgage")
    assert all(not valid_source_url(url) for url in INVALID_URLS)


def test_verified_requires_evidence(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert mortgage_state(audit(registry)) == CoverageState.VERIFIED
    assert not has(audit(registry), Reason.EVIDENCE_MISSING, "product_findings")
    report = audit(without(registry, "finding_evidence"))
    assert has(report, Reason.EVIDENCE_MISSING, "product_findings")
    assert mortgage_state(report) == CoverageState.UNKNOWN


def test_charter_verification_requires_identity_proof(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.IDENTITY_EVIDENCE_MISSING)
    assert has(
        audit(alter(registry, "institutions", charter_verified=1)), Reason.IDENTITY_EVIDENCE_MISSING
    )


def test_relationship_verification_requires_proof(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    registry = seed(database[0], tmp_path)
    assert has(audit(registry), Reason.RELATIONSHIP_EVIDENCE_MISSING)
    proposed = alter(
        registry, "institution_source_relationships", approved_by=None, approved_at=None
    )
    assert not has(audit(proposed), Reason.RELATIONSHIP_EVIDENCE_MISSING)


def test_blocked_and_approved_conflict(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(
        database[0],
        tmp_path,
        availability=RateAvailability.BLOCKED,
        approved_product=True,
        approved_offer=True,
    )
    assert has(audit(registry), Reason.BLOCKED_AND_VERIFIED, "products")
    assert has(audit(registry), Reason.BLOCKED_AND_VERIFIED, "normalized_offers")
    clean = without(without(registry, "review_decisions"), "normalized_offers")
    assert not has(audit(clean), Reason.BLOCKED_AND_VERIFIED)
    assert mortgage_state(audit(clean)) == CoverageState.BLOCKED


def test_publication_requires_relationship(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    assert not has(audit(registry), Reason.RELATIONSHIP_UNVERIFIED, "normalized_offers")
    proposed = alter(
        registry, "institution_source_relationships", approved_by=None, approved_at=None
    )
    assert has(audit(proposed), Reason.RELATIONSHIP_UNVERIFIED, "normalized_offers")
    wrong_product = alter(registry, "institution_source_relationships", product_id=None)
    assert has(audit(wrong_product), Reason.RELATIONSHIP_UNVERIFIED, "normalized_offers")


def test_freshness_bound(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    registry = alter(registry, "normalized_offers", valid_until=f.T0 + timedelta(hours=96))
    assert not has(audit(registry), Reason.STALE)
    at_boundary = validate_registry(registry, as_of=f.T0 + timedelta(hours=72))
    assert not has(at_boundary, Reason.STALE)
    expired = validate_registry(registry, as_of=f.T0 + timedelta(hours=72, microseconds=1))
    assert has(expired, Reason.STALE, "product_findings")
    assert has(expired, Reason.STALE, "normalized_offers")
    assert mortgage_state(expired) == CoverageState.STALE


def test_offer_expiry_and_maximum_validity(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    assert not has(audit(registry), Reason.STALE, "normalized_offers")
    assert has(audit(alter(registry, "normalized_offers", valid_until=NOW)), Reason.STALE)
    assert not has(
        audit(alter(registry, "normalized_offers", valid_until=f.T0 + timedelta(hours=96))),
        Reason.STALE,
    )
    assert has(
        audit(
            alter(
                registry,
                "normalized_offers",
                valid_until=f.T0 + timedelta(hours=96, microseconds=1),
            )
        ),
        Reason.STALE,
    )


@pytest.mark.parametrize(
    "table,key",
    [
        ("institutions", "charter_number"),
        ("products", "product_id"),
        ("rate_sources", "source_id"),
        ("institution_source_relationships", "relationship_id"),
    ],
)
def test_duplicate_identifiers(
    database: tuple[Engine, Path], tmp_path: Path, table: str, key: str
) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.DUPLICATE_IDENTIFIER)
    duplicate = Registry(dict(registry.tables, **{table: tuple(registry.rows(table)) * 2}))
    report = audit(duplicate)
    assert has(report, Reason.DUPLICATE_IDENTIFIER, table)
    assert any(
        item.entity_id == str(registry.rows(table)[0][key])
        for item in report.findings
        if item.reason == Reason.DUPLICATE_IDENTIFIER
    )


def test_active_roster_closed_institution(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.ROSTER_STATUS_CONFLICT)
    assert has(
        audit(alter(registry, "institutions", status="liquidated")), Reason.ROSTER_STATUS_CONFLICT
    )


def test_latest_roster_not_historical_status(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    old = dict(
        registry.rows("roster_import_entries")[0],
        source_as_of_date="2026-01-01",
        status="liquidated",
    )
    registry = Registry(
        dict(registry.tables, roster_import_entries=(old, *registry.rows("roster_import_entries")))
    )
    assert not has(audit(registry), Reason.ROSTER_STATUS_CONFLICT)


def test_unsupported_offer_publication(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.UNSUPPORTED_PUBLICATION, "normalized_offers")
    for status in ("approved", "published"):
        report = audit(alter(registry, "normalized_offers", status=status))
        assert has(report, Reason.UNSUPPORTED_PUBLICATION, "normalized_offers")
    # Snapshot membership is a publication claim even if the mutable status lies.
    snapshot = Registry(
        dict(
            registry.tables,
            snapshot_entries=({"offer_id": registry.rows("normalized_offers")[0]["offer_id"]},),
        )
    )
    assert has(audit(snapshot), Reason.UNSUPPORTED_PUBLICATION, "normalized_offers")


def test_unsupported_product_publication(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_product=True)
    assert has(audit(registry), Reason.UNSUPPORTED_PUBLICATION, "products")
    assert not has(
        audit(without(registry, "review_decisions")), Reason.UNSUPPORTED_PUBLICATION, "products"
    )


@pytest.mark.parametrize(
    "table,change",
    [
        ("artifact_blobs", "remove"),
        ("artifact_observations", "remove"),
        ("provenance", "remove"),
        ("fetch_runs", "remove"),
        ("provenance", "wrong_institution"),
        ("artifact_observations", "wrong_url"),
        ("fetch_runs", "wrong_source"),
        ("artifact_observations", "wrong_media"),
        ("artifact_observations", "failed_capture"),
    ],
)
def test_stored_provenance_chain(
    database: tuple[Engine, Path], tmp_path: Path, table: str, change: str
) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.EVIDENCE_MISSING, "product_findings")
    updates: dict[str, dict[str, Any]] = {
        "wrong_institution": {"institution_charter": f.CHARTER + 1},
        "wrong_url": {"source_url": "https://other.invalid/"},
        "wrong_source": {"source_id": "another-source"},
        "wrong_media": {"media_type": "application/pdf"},
        "failed_capture": {"http_status": 403},
    }
    broken = (
        without(registry, table)
        if change == "remove"
        else alter(registry, table, **updates[change])
    )
    assert has(audit(broken), Reason.EVIDENCE_MISSING, "product_findings")
    assert mortgage_state(audit(broken)) == CoverageState.UNKNOWN


def test_verified_history_still_needs_provenance(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    registry = seed(database[0], tmp_path)
    old = dict(
        registry.rows("product_findings")[0],
        finding_id="old-finding",
        observed_at=f.T0 - timedelta(days=30),
    )
    registry = Registry(
        dict(registry.tables, product_findings=(old, *registry.rows("product_findings")))
    )
    assert has(audit(registry), Reason.EVIDENCE_MISSING, "product_findings")
    assert mortgage_state(audit(registry)) == CoverageState.VERIFIED


def test_coverage_all_four_states(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    # Preserve the actual DB chains and use disjoint charter/product IDs for slots.
    unknown = f.product(product_id="unknown-product", institution_charter=f.CHARTER + 1)
    blocked = f.product(
        product_id="blocked-product",
        institution_charter=f.CHARTER + 2,
        rate_availability=RateAvailability.BLOCKED,
    )
    products = list(registry.rows("products"))
    for item in (unknown, blocked):
        row = item.model_dump(exclude={"notes", "borrower_programs", "partner_programs"})
        products.append(dict(row, notes_json=[]))
    institutions = list(registry.rows("institutions")) + [
        dict(registry.rows("institutions")[0], charter_number=f.CHARTER + n) for n in (1, 2, 3)
    ]
    # Blocked coverage needs evidence belonging to the new institution/source chain.
    pid = registry.rows("finding_evidence")[0]["provenance_id"]
    prov = dict(
        next(r for r in registry.rows("provenance") if r["provenance_id"] == pid),
        provenance_id="new-proof",
        institution_charter=f.CHARTER + 2,
    )
    finding = dict(
        registry.rows("product_findings")[0],
        finding_id="blocked-finding",
        product_id=blocked.product_id,
        institution_charter=f.CHARTER + 2,
        rate_availability="blocked",
    )
    rel = dict(
        registry.rows("institution_source_relationships")[0],
        relationship_id="blocked-rel",
        product_id=blocked.product_id,
        institution_charter=f.CHARTER + 2,
    )
    registry = Registry(
        dict(
            registry.tables,
            institutions=institutions,
            products=products,
            provenance=(*registry.rows("provenance"), prov),
            product_findings=(*registry.rows("product_findings"), finding),
            finding_evidence=(
                *registry.rows("finding_evidence"),
                {"finding_id": "blocked-finding", "provenance_id": "new-proof"},
            ),
            institution_source_relationships=(
                *registry.rows("institution_source_relationships"),
                rel,
            ),
        )
    )
    report = audit(registry)
    assert report.coverage_counts["absent"] == 9  # 4 * 3 families minus 3 catalog rows
    assert report.coverage_counts["unknown"] == 1
    assert report.coverage_counts["blocked"] == 1
    assert report.coverage_counts["verified"] == 1
    assert sum(report.coverage_counts.values()) == 12


@pytest.mark.parametrize(
    "state",
    [
        RateAvailability.NOT_OFFERED,
        RateAvailability.NO_PUBLIC_RATE,
        RateAvailability.QUOTE_ONLY,
        RateAvailability.LOGIN_REQUIRED,
    ],
)
def test_explicit_absence_is_not_absent(
    database: tuple[Engine, Path], tmp_path: Path, state: RateAvailability
) -> None:
    registry = seed(database[0], tmp_path, availability=state)
    assert mortgage_state(audit(registry)).value == state.value


def test_determinism_shuffled_rows(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True, approved_product=True)
    extra = [
        f.institution(charter_number=f.CHARTER + n, charter_verified=False).model_dump()
        for n in (1, 2)
    ]
    registry = Registry(
        dict(registry.tables, institutions=(*registry.rows("institutions"), *extra))
    )
    expected = audit(registry)
    assert [r.charter_number for r in expected.institutions] == [
        f.CHARTER,
        f.CHARTER + 1,
        f.CHARTER + 2,
    ]
    assert expected.canonical_json() == json.dumps(
        expected.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    rng = random.Random(24)
    for _ in range(8):
        names = list(registry.tables)
        rng.shuffle(names)
        tables = {}
        for name in names:
            rows = list(registry.rows(name))
            rng.shuffle(rows)
            tables[name] = rows
        actual = audit(Registry(tables))
        assert actual.canonical_json() == expected.canonical_json()
        assert actual.sha256 == expected.sha256
    assert hashlib.sha256(expected.canonical_json()).hexdigest() == expected.sha256
    assert b" " not in expected.canonical_json()
    assert (
        validate_registry(registry, as_of=NOW.astimezone()).canonical_json()
        == expected.canonical_json()
    )
    with pytest.raises(ValueError, match="timezone"):
        validate_registry(registry, as_of=NOW.replace(tzinfo=None))


def test_readonly_cli_rebuild(
    database: tuple[Engine, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = seed(database[0], tmp_path)
    path = database[1]
    before = path.read_bytes()
    names = {p.name for p in tmp_path.iterdir()}
    report = report_from_database(path, as_of=NOW)
    assert report.canonical_json() == audit(registry).canonical_json()
    assert {p.name for p in tmp_path.iterdir()} == names
    assert path.read_bytes() == before
    assert main([str(path), "--as-of", NOW.isoformat()]) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["sha256"] == report.sha256
    assert envelope["report"] == json.loads(report.canonical_json())
    output = tmp_path / "report.json"
    assert main([str(path), "--as-of", NOW.isoformat(), "--output", str(output)]) == 1
    assert output.read_text() == capsys.readouterr().out
    assert path.read_bytes() == before
    with pytest.raises(SystemExit):
        main([str(path), "--as-of", NOW.isoformat(), "--output", str(path)])
    absent = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        report_from_database(absent, as_of=NOW)
    assert not absent.exists()


def test_publication_institution_approval(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    assert has(audit(registry), Reason.PUBLICATION_INSTITUTION_UNVERIFIED)
    verified = alter(registry, "institutions", charter_verified=1)
    assert not has(audit(verified), Reason.PUBLICATION_INSTITUTION_UNVERIFIED)
    assert has(
        audit(alter(verified, "institutions", status="liquidated")),
        Reason.PUBLICATION_INSTITUTION_UNVERIFIED,
    )


def test_publication_source_approval(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    assert not has(audit(registry), Reason.PUBLICATION_SOURCE_UNAPPROVED)
    for broken in (
        without(registry, "source_approvals"),
        without(registry, "source_approval_evidence"),
        alter(registry, "source_approvals", decision="rejected"),
        alter(registry, "source_approvals", source_url="https://other.invalid/"),
    ):
        assert has(audit(broken), Reason.PUBLICATION_SOURCE_UNAPPROVED)


def test_publication_product_approval(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True, approved_product=True)
    assert not has(audit(registry), Reason.PUBLICATION_PRODUCT_UNAPPROVED)
    assert has(audit(without(registry, "review_decisions")), Reason.PUBLICATION_PRODUCT_UNAPPROVED)
    product_reviews = [r for r in registry.rows("review_decisions") if r["subject"] == "product"]
    assert len(product_reviews) == 1
    rejected = dict(product_reviews[0], verdict="rejected")
    rows = tuple(
        rejected if r["subject"] == "product" else r for r in registry.rows("review_decisions")
    )
    assert has(
        audit(Registry(dict(registry.tables, review_decisions=rows))),
        Reason.PUBLICATION_PRODUCT_UNAPPROVED,
    )


def test_source_approval_provenance(database: tuple[Engine, Path], tmp_path: Path) -> None:
    registry = seed(database[0], tmp_path)
    assert not has(audit(registry), Reason.EVIDENCE_MISSING, "source_approvals")
    assert has(
        audit(without(registry, "source_approval_evidence")),
        Reason.EVIDENCE_MISSING,
        "source_approvals",
    )


def test_cli_refuses_wal_and_hardlink_output(database: tuple[Engine, Path], tmp_path: Path) -> None:
    seed(database[0], tmp_path)
    path = database[1]
    wal = Path(str(path) + "-wal")
    wal.write_bytes(b"pending changes")
    with pytest.raises(ValueError, match="WAL"):
        report_from_database(path, as_of=NOW)
    wal.unlink()
    alias = tmp_path / "alias.sqlite"
    alias.hardlink_to(path)
    with pytest.raises(SystemExit):
        main([str(path), "--as-of", NOW.isoformat(), "--output", str(alias)])


def test_source_block_conflicts_with_approval(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    registry = seed(database[0], tmp_path, approved_offer=True)
    block = {
        "withdrawal_id": "block-1",
        "scope": "source",
        "target_id": f.SOURCE_ID,
        "withdrawn_at": f.T0,
        "actor": "Reviewer",
        "reason": "Synthetic stop",
    }
    assert not has(audit(registry), Reason.BLOCKED_AND_VERIFIED)
    registry = Registry(dict(registry.tables, withdrawal_entries=(block,)))
    assert has(audit(registry), Reason.BLOCKED_AND_VERIFIED, "rate_sources")
    assert has(audit(registry), Reason.PUBLICATION_SOURCE_UNAPPROVED)
    lifted = Registry(
        dict(
            registry.tables,
            withdrawal_lifts=(
                {"withdrawal_id": "block-1", "lifted_at": NOW, "lifted_by": "Owner"},
            ),
        )
    )
    assert not has(audit(lifted), Reason.BLOCKED_AND_VERIFIED)
    future = alter(lifted, "withdrawal_lifts", lifted_at=NOW + timedelta(seconds=1))
    assert has(audit(future), Reason.BLOCKED_AND_VERIFIED)


def test_unknown_finding_does_not_invent_absence(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    seed(database[0], tmp_path, finding=False)
    unknown = ProductFinding(
        schema_version=f.SV,
        taxonomy_version="1.0",
        finding_id="unknown-page",
        institution_charter=f.CHARTER,
        source_id=f.SOURCE_ID,
        observed_at=f.T0,
        label="Ambiguous page",
        scope=ProductScope.UNKNOWN,
    )
    with database[0].begin() as conn:
        insert_finding(conn, unknown)
    with database[0].connect() as conn:
        report = audit(read_registry(conn))
    assert report.coverage_counts["unknown"] == 3
    assert report.coverage_counts["absent"] == 0


def test_partially_valid_evidence_is_not_accepted(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    """One dangling evidence link voids the whole evidence set; nothing is silently dropped."""
    registry = seed(database[0], tmp_path)
    assert mortgage_state(audit(registry)) == CoverageState.VERIFIED
    link = registry.rows("finding_evidence")[0]
    extra = dict(link, provenance_id="dangling-provenance-id")
    partial = Registry(
        dict(registry.tables, finding_evidence=(*registry.rows("finding_evidence"), extra))
    )
    report = audit(partial)
    assert has(report, Reason.EVIDENCE_MISSING, "product_findings")
    assert mortgage_state(report) != CoverageState.VERIFIED


def test_malformed_product_does_not_count_as_absent(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    """A product row that cannot be parsed must make its family unknown, never absent."""
    registry = seed(database[0], tmp_path, finding=False)
    broken = alter(registry, "products", lifecycle="not-a-lifecycle")
    report = audit(broken)
    assert has(report, Reason.INVALID_RECORD, "products")
    assert mortgage_state(report) == CoverageState.UNKNOWN
    assert mortgage_state(report) != CoverageState.ABSENT


def test_findings_are_emitted_in_canonical_sorted_order(
    database: tuple[Engine, Path], tmp_path: Path
) -> None:
    """Report ordering never depends on set iteration order."""
    registry = seed(database[0], tmp_path, approved_offer=True, approved_product=True)
    damaged = without(without(registry, "finding_evidence"), "offer_evidence")
    damaged = without(damaged, "source_approval_evidence")
    damaged = alter(damaged, "institutions", charter_verified=1)
    report = audit(damaged)
    assert len(report.findings) >= 6

    def key(item: Any) -> tuple[Any, ...]:
        charter = -1 if item.institution_charter is None else item.institution_charter
        return (charter, item.entity, item.entity_id, item.reason)

    assert list(report.findings) == sorted(report.findings, key=key)


def test_canonical_json_is_pure_ascii() -> None:
    """Non-ASCII identifiers are escaped, so the hashed bytes are portable and stable."""
    from datetime import UTC, datetime

    from cu_rates.registry_validation import Finding

    report = RegistryReport(
        as_of=datetime(2026, 11, 17, tzinfo=UTC),
        institutions=(),
        findings=(
            Finding(
                institution_charter=None,
                entity="registry",
                entity_id="café",
                reason=Reason.INVALID_RECORD,
            ),
        ),
        reason_counts={},
        coverage_counts={},
    )
    raw = report.canonical_json()
    assert raw.isascii()
    assert b"caf\\u00e9" in raw

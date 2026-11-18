"""Public snapshot integration: real stored bytes from the offline fixture pipeline."""

import inspect
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from shutil import copytree
from threading import Event
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Connection

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.audit import (
    PublishedSnapshot,
    ReviewDecision,
    SnapshotEntry,
    snapshot_manifest_sha256,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    OfferStatus,
    ReviewSubject,
    ReviewVerdict,
    WithdrawalScope,
)
from cu_rates.contracts.fingerprints import OfferFingerprints, offer_fingerprints
from cu_rates.contracts.publication import (
    Placement,
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.db import metadata, open_operational_database
from cu_rates.db.schema import normalized_offers, withdrawal_entries
from cu_rates.pipeline.slice import CHARTER, SliceRun, verified_offer_candidates
from cu_rates.pipeline.trusted import insert_review, insert_withdrawal, load_publication_context
from cu_rates.snapshot import Denylist, PublicOffer, SnapshotError, SnapshotReader, builder
from cu_rates.snapshot._database import database_digest
from cu_rates.snapshot._models import (
    PublicEvidencePath,
    SnapshotInfo,
    Version,
    public_evidence_sha256,
)
from cu_rates.snapshot.builder import SnapshotCandidate, SnapshotPublisher

from .contracts import factories as make
from .pub2_fixtures import reviewed_fixture

_BIRTH = datetime(2026, 11, 16, 15, 1, 6, tzinfo=UTC)


@dataclass
class StoredFixture:
    run: SliceRun
    conn: Connection

    def row(self, name: str = "purchase-30-year", *, now: datetime = _BIRTH) -> SnapshotCandidate:
        offer_id = self.run.offers[name].offer_id
        view = load_publication_context(self.conn, offer_id, now, store=self.run.store)
        primary = next(
            (a for a in view.context.known_artifacts if a.fetched_at == view.offer.observed_at),
            None,
        )
        candidates = (
            ()
            if primary is None
            else verified_offer_candidates(
                view.context.source, view.offer, self.run.store.read(primary), primary
            )[0]
        )
        return SnapshotCandidate(
            offer=view.offer,
            context=view.context,
            candidates=candidates,
            current_review=view.current_review,
        )

    def approve(self, offer_id: str, *, evidence_hash: str | None = None) -> None:
        view = load_publication_context(self.conn, offer_id, _BIRTH, store=self.run.store)
        paths = corroborated_paths(view.offer, view.context)
        assert paths
        insert_review(
            self.conn,
            ReviewDecision(
                schema_version=make.SV,
                review_id=f"test-review-{offer_id}",
                subject=ReviewSubject.OFFER,
                subject_id=offer_id,
                verdict=ReviewVerdict.APPROVED,
                actor="fixture-reviewer",
                decided_at=_BIRTH,
                rationale="Review the two stored authored fixture documents.",
                evidence=tuple(dict.fromkeys(path.provenance for path in paths)),
                reviewed_fingerprints=offer_fingerprints(view.offer),
                reviewed_evidence_sha256=evidence_hash
                or publication_evidence_sha256(view.offer, paths),
            ),
            CHARTER,
        )


@pytest.fixture(scope="module")
def persisted_seed(tmp_path_factory: pytest.TempPathFactory) -> SliceRun:
    # Run the actual pipeline once, then isolate tests with copies of its closed store.
    return reviewed_fixture(tmp_path_factory.mktemp("persisted-admission"))


@pytest.fixture
def stored(tmp_path: Path, persisted_seed: SliceRun) -> Iterator[StoredFixture]:
    root = tmp_path / "worker"
    copytree(persisted_seed.database.parent, root)
    run = replace(
        persisted_seed, database=root / "operational.sqlite", store=ArtifactStore(root / "evidence")
    )
    engine = open_operational_database(run.database)
    with engine.begin() as conn:
        yield StoredFixture(run, conn)
    engine.dispose()


def release(
    name: str,
    *rows: SnapshotCandidate,
    supersedes: str | None = None,
    review_ids: tuple[str, ...] | None = None,
) -> PublishedSnapshot:
    entries = tuple(
        sorted(
            (
                SnapshotEntry(
                    offer_id=row.offer.offer_id,
                    structural_sha256=offer_fingerprints(row.offer).structural,
                    value_sha256=offer_fingerprints(row.offer).value,
                )
                for row in rows
            ),
            key=lambda e: e.offer_id,
        )
    )
    return PublishedSnapshot(
        schema_version=make.SV,
        snapshot_id=name,
        published_at=rows[0].context.evaluated_at,
        published_by="offline-fixture-publisher",
        entries=entries,
        manifest_sha256=snapshot_manifest_sha256(entries),
        supersedes=supersedes,
        review_ids=review_ids
        if review_ids is not None
        else tuple(row.current_review.review_id for row in rows if row.current_review is not None),
    )


def setup_storage(tmp_path: Path) -> tuple[SnapshotPublisher, SnapshotReader, Denylist]:
    publisher = SnapshotPublisher(tmp_path / "snapshots with spaces")
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()
    return publisher, SnapshotReader(publisher.root, denylist.path), denylist


def test_snapshot_records_provenance_generation_and_immutable_format(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    assert evaluate_publication(item.offer, item.context).ranking_eligible
    manifest = release("first", item)
    info = publisher.build(manifest, conn=stored.conn, store=stored.run.store)
    view = reader.read(at=_BIRTH)
    assert view.info == info
    assert info.manifest_sha256 == manifest.manifest_sha256
    (visible,) = view.offers
    assert visible.review_status == "automatic" and visible.placement is Placement.RANKED
    assert visible.source_url == item.context.source.url
    assert visible.observed_at == item.offer.observed_at
    assert {part.artifact_id for part in visible.evidence} == {
        a.artifact_id for a in item.context.known_artifacts
    }
    assert {part.path for part in visible.evidence} >= {
        "note_rate",
        "apr",
        "fields.details.term_months",
    }
    assert {value.path: value.value for value in visible.rates} == {
        "note_rate": Decimal("6.375"),
        "apr": Decimal("6.441"),
    }
    database = publisher.root / "versions" / "first" / "published.sqlite"
    assert database.is_file() and database.stat().st_mode & 0o222 == 0
    assert not list(database.parent.glob("published.sqlite-*"))
    assert b"source_text" not in database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM public_offers")


def test_a_dated_t014_human_approval_is_exported_without_web_access_to_worker(
    tmp_path: Path, stored: StoredFixture
) -> None:
    as_of = _BIRTH - timedelta(microseconds=1)
    item = stored.row(now=as_of)
    assert item.current_review is not None
    publisher, reader, _ = setup_storage(tmp_path)
    publisher.build(release("reviewed", item), conn=stored.conn, store=stored.run.store)
    stored.run.database.rename(stored.run.database.with_name("unavailable.sqlite"))
    (visible,) = reader.read(at=as_of).offers
    assert visible.review_status == "reviewed" and visible.placement is Placement.RANKED
    assert visible.effective_at == item.offer.effective_at


def test_t014_refresh_without_t045_compatible_capture_does_not_activate(
    tmp_path: Path, stored: StoredFixture
) -> None:
    assert stored.run.refreshed_snapshot is not None
    item = stored.row(now=stored.run.refreshed_snapshot.published_at)
    assert evaluate_publication(item.offer, item.context).publishable_now
    publisher, _, _ = setup_storage(tmp_path)
    with pytest.raises(SnapshotError, match="T045 refused"):
        publisher.build(release("not-validated", item), conn=stored.conn, store=stored.run.store)
    assert not (publisher.root / "current.json").exists()


def test_review_must_bind_exact_stored_offer_evidence(
    tmp_path: Path, stored: StoredFixture
) -> None:
    item = stored.row("purchase-15-year")
    stored.approve(item.offer.offer_id, evidence_hash="d0" * 32)
    item = stored.row("purchase-15-year")
    publisher, _, _ = setup_storage(tmp_path)
    with pytest.raises(SnapshotError, match="publication gate refused"):
        publisher.build(release("not-reviewed", item), conn=stored.conn, store=stored.run.store)


def test_reader_never_sees_a_partial_version_during_build_and_activation(
    tmp_path: Path, stored: StoredFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    originals = [stored.row()]
    stored.approve(stored.run.offers["purchase-15-year"].offer_id)
    replacements = [stored.row(), stored.row("purchase-15-year")]
    publisher.build(release("first", *originals), conn=stored.conn, store=stored.run.store)
    expected = {
        "first": tuple(x.offer.offer_id for x in originals),
        "second": tuple(sorted(x.offer.offer_id for x in replacements)),
    }
    staged, swap, observed_old, observed_new, stop = (Event() for _ in range(5))
    activate = publisher._activate

    def wait_to_activate(version: Version) -> None:
        staged.set()
        assert swap.wait(8)
        activate(version)

    def reading() -> None:
        while not stop.is_set():
            view = reader.read(at=_BIRTH)
            assert tuple(offer.offer_id for offer in view.offers) == expected[view.info.snapshot_id]
            (observed_old if view.info.snapshot_id == "first" else observed_new).set()

    monkeypatch.setattr(publisher, "_activate", wait_to_activate)
    # SQLite connections stay in the thread that owns them.
    stored.conn.commit()
    engine = open_operational_database(stored.run.database)

    def writing() -> SnapshotInfo:
        with engine.connect() as conn:
            return publisher.build(
                release("second", *replacements, supersedes="first"),
                conn=conn,
                store=stored.run.store,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(writing)
        read_loop = pool.submit(reading)
        try:
            assert staged.wait(8) and observed_old.wait(8)
            assert reader.read(at=_BIRTH).info.snapshot_id == "first"
            assert not observed_new.is_set()
            swap.set()
            assert writer.result(timeout=8).snapshot_id == "second"
            assert observed_new.wait(8)
        finally:
            stop.set()
            swap.set()
        read_loop.result(timeout=8)
    engine.dispose()


@pytest.mark.parametrize("mode", ["pending review", "rejected review", "quarantined"])
def test_ineligible_stored_rows_never_activate_or_partially_publish(
    tmp_path: Path, stored: StoredFixture, mode: str
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    clean = stored.row()
    publisher.build(release("first", clean), conn=stored.conn, store=stored.run.store)
    pending = stored.row("purchase-15-year")
    if mode == "rejected review":
        insert_review(
            stored.conn,
            ReviewDecision(
                schema_version=make.SV,
                review_id="rejected-15",
                subject=ReviewSubject.OFFER,
                subject_id=pending.offer.offer_id,
                verdict=ReviewVerdict.REJECTED,
                actor="fixture-reviewer",
                decided_at=_BIRTH,
                rationale="Reject the stored offer.",
                reviewed_fingerprints=offer_fingerprints(pending.offer),
                reviewed_evidence_sha256=publication_evidence_sha256(
                    pending.offer, corroborated_paths(pending.offer, pending.context)
                ),
            ),
            CHARTER,
        )
    elif mode == "quarantined":
        stored.conn.execute(
            normalized_offers.update()
            .where(normalized_offers.c.offer_id == pending.offer.offer_id)
            .values(status=OfferStatus.QUARANTINED.value)
        )
    pointer = publisher.root / "current.json"
    before = pointer.read_bytes()
    with pytest.raises(SnapshotError, match=r"refused|publishable|approved"):
        publisher.build(
            release("second", clean, pending, supersedes="first"),
            conn=stored.conn,
            store=stored.run.store,
        )
    assert pointer.read_bytes() == before
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"
    assert not (publisher.root / "versions" / "second").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "no stored bytes",
        "tampered primary bytes",
        "tampered secondary bytes",
        "missing secondary bytes",
        "missing observation",
    ],
)
def test_builder_rechecks_stored_bytes_before_activation(
    tmp_path: Path, stored: StoredFixture, failure: str
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    if "conn" in inspect.signature(publisher.build).parameters:
        publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    else:
        publisher.build(release("first", item), [item])  # type: ignore[call-arg, arg-type]
    pointer = publisher.root / "current.json"
    before = pointer.read_bytes()
    artifacts = sorted(item.context.known_artifacts, key=lambda a: a.fetched_at, reverse=True)
    primary, secondary = artifacts
    if failure == "missing observation":
        import hashlib

        metadata = (
            (stored.run.database.parent / "evidence")
            / "observations"
            / secondary.fetch_run_id
            / (hashlib.sha256(secondary.source_url.encode()).hexdigest() + ".json")
        )
        metadata.unlink()
    else:
        affected = (
            artifacts
            if failure == "no stored bytes"
            else [primary if "primary" in failure else secondary]
        )
        for art in affected:
            blob = (
                (stored.run.database.parent / "evidence")
                / "blobs"
                / art.artifact_id[:2]
                / art.artifact_id
            )
            if "tampered" in failure:
                blob.chmod(0o600)
                body = blob.read_bytes()
                blob.write_bytes(body.replace(b"6.375%", b"9.375%"))
                blob.chmod(0o400)
            else:
                blob.unlink()
    # Compatibility lane deliberately reproduces the original exploit if the old
    # implementation is restored: it accepts this cached, approved-looking assertion.
    if "conn" not in inspect.signature(publisher.build).parameters:
        with pytest.raises(SnapshotError, match=r"stored|evidence"):
            publisher.build(release("second", item, supersedes="first"), [item])  # type: ignore[call-arg, arg-type]
    else:
        with pytest.raises(SnapshotError, match=r"stored|evidence|corroboration"):
            publisher.build(
                release("second", item, supersedes="first"),
                conn=stored.conn,
                store=stored.run.store,
            )
    assert pointer.read_bytes() == before
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"
    assert not (publisher.root / "versions" / "second").exists()


def test_builder_rejects_caller_supplied_admission_inputs(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, _, _ = setup_storage(tmp_path)
    item = stored.row()
    # Review IDs in the manifest select records; they cannot supply a decision.
    with pytest.raises(TypeError):
        publisher.build(release("asserted", item), [item])  # type: ignore[call-arg, arg-type]
    for keyword, asserted in (("captures", [item]), ("reviews", [item.current_review])):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            publisher.build(
                release("asserted", item),
                conn=stored.conn,
                store=stored.run.store,
                **{keyword: asserted},
            )
    assert not (publisher.root / "current.json").exists()


def test_probe_with_asserted_proofs_but_no_stored_bytes_is_refused(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, _, _ = setup_storage(tmp_path)
    item = stored.row()
    empty_store = ArtifactStore(tmp_path / "empty-evidence")
    # The legacy API published this cached context even when the store had no bytes.
    with pytest.raises(SnapshotError, match="stored evidence bytes missing or unverifiable"):
        if "conn" in inspect.signature(publisher.build).parameters:
            publisher.build(release("probe", item), conn=stored.conn, store=empty_store)
        else:
            publisher.build(release("probe", item), [item])  # type: ignore[call-arg, arg-type]
    assert not (publisher.root / "current.json").exists()
    assert not (publisher.root / "versions" / "probe").exists()


def _change_manifest_in_database(path: Path) -> None:
    path.chmod(0o644)
    with closing(sqlite3.connect(path)) as conn, conn:
        (payload,) = conn.execute("SELECT payload FROM snapshot_info").fetchone()
        wrong = SnapshotInfo.model_validate_json(payload).model_copy(
            update={"manifest_sha256": "0" * 64}
        )
        conn.execute("UPDATE snapshot_info SET payload=?", (wrong.model_dump_json(),))
    path.chmod(0o444)


def test_bad_manifest_at_input_and_after_staging_preserves_live_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    clean = stored.row()
    publisher.build(release("first", clean), conn=stored.conn, store=stored.run.store)
    second = release("second", clean, supersedes="first")
    pointer = publisher.root / "current.json"
    before = pointer.read_bytes()
    with pytest.raises(SnapshotError):
        publisher.build(
            second.model_copy(update={"manifest_sha256": "0" * 64}),
            conn=stored.conn,
            store=stored.run.store,
        )
    write_real = builder._write_database

    def invalid_staging(path: Path, info: SnapshotInfo, rows: tuple[PublicOffer, ...]) -> None:
        write_real(path, info, rows)
        _change_manifest_in_database(path)

    monkeypatch.setattr(builder, "_write_database", invalid_staging)
    with pytest.raises(SnapshotError, match="manifest"):
        publisher.build(second, conn=stored.conn, store=stored.run.store)
    assert pointer.read_bytes() == before
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"
    assert not list((publisher.root / "versions").glob(".build-*"))


def test_read_rebuilds_manifest_and_rollback_refuses_a_bad_version(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    publisher.build(
        release("second", item, supersedes="first"), conn=stored.conn, store=stored.run.store
    )
    second = publisher.root / "versions" / "second"
    _change_manifest_in_database(second / "published.sqlite")
    # Re-anchor the file digest deliberately; the *contract* manifest must still catch it.
    pointer = publisher.root / "current.json"
    updated = Version(
        version=1,
        snapshot_id="second",
        database_sha256=database_digest(second / "published.sqlite"),
    )
    pointer.write_text(updated.model_dump_json())
    (second / "version.json").chmod(0o644)
    (second / "version.json").write_text(updated.model_dump_json())
    with pytest.raises(SnapshotError, match="manifest"):
        reader.read(at=_BIRTH)
    assert publisher.rollback("first").snapshot_id == "first"
    active = pointer.read_bytes()
    with pytest.raises(SnapshotError, match="manifest"):
        publisher.rollback("second")
    assert pointer.read_bytes() == active
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"


def test_read_rejects_nonmanifest_field_tampering_and_sidecars(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    db = publisher.root / "versions" / "first" / "published.sqlite"
    sidecar = db.with_name("published.sqlite-wal")
    sidecar.touch()
    with pytest.raises(SnapshotError, match="sidecars"):
        reader.read(at=_BIRTH)
    sidecar.unlink()
    db.chmod(0o644)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE public_offers SET payload=replace(payload, 'institution_name', 'renamed')"
        )
    db.chmod(0o444)
    with pytest.raises(SnapshotError, match="digest mismatch"):
        reader.read(at=_BIRTH)


def test_rollback_preserves_sealed_versions_and_refuses_reusing_an_id(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    second = release("second", item, supersedes="first")
    publisher.build(second, conn=stored.conn, store=stored.run.store)
    database = publisher.root / "versions" / "second" / "published.sqlite"
    original = database.read_bytes()
    publisher.rollback("first")
    with pytest.raises(SnapshotError, match="already exists"):
        publisher.build(second, conn=stored.conn, store=stored.run.store)
    assert database.read_bytes() == original
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"


def test_pointer_cannot_select_an_operational_database_path(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    stable = publisher.root / "current.json"
    stable.write_bytes(stable.read_bytes().replace(b'"first"', b'"../../operational.sqlite"'))
    with pytest.raises(SnapshotError, match="pointer"):
        reader.read(at=_BIRTH)
    with pytest.raises(SnapshotError, match="rollback"):
        publisher.rollback("../../operational.sqlite")


@pytest.mark.parametrize("scope", list(WithdrawalScope))
def test_independent_emergency_withdrawal_enforces_every_scope_at_read_time(
    tmp_path: Path, scope: WithdrawalScope, stored: StoredFixture
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    moment = _BIRTH + timedelta(minutes=1)
    targets = {
        WithdrawalScope.SOURCE: item.offer.source_id,
        WithdrawalScope.OFFER: item.offer.offer_id,
        WithdrawalScope.PRODUCT: item.offer.product_id,
        WithdrawalScope.INSTITUTION: str(item.offer.institution_charter),
        WithdrawalScope.SNAPSHOT: "first",
    }
    entry = make.withdrawal(scope, targets[scope], withdrawn_at=moment)
    assert reader.read(at=moment).offers
    denylist.withdraw(entry)
    assert reader.read(at=moment - timedelta(microseconds=1)).offers
    assert reader.read(at=moment).offers == ()
    denylist.lift(entry.withdrawal_id, at=moment + timedelta(minutes=1), actor="Synthetic reviewer")
    assert reader.read(at=moment).offers == ()
    assert reader.read(at=moment + timedelta(minutes=1)).offers


def test_rollback_preserves_withdrawals_and_expiry_and_invalid_denylist_fails_closed(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    soon = stored.row()
    held = stored.row("purchase-15-year")
    stored.approve(held.offer.offer_id)
    held = stored.row("purchase-15-year")
    publisher.build(release("first", soon, held), conn=stored.conn, store=stored.run.store)
    publisher.build(
        release("second", held, supersedes="first"), conn=stored.conn, store=stored.run.store
    )
    publisher.rollback("first")
    expiry = soon.offer.valid_until
    assert expiry is not None
    denylist.withdraw(
        make.withdrawal(WithdrawalScope.OFFER, held.offer.offer_id, withdrawn_at=_BIRTH)
    )
    assert tuple(x.offer_id for x in reader.read(at=expiry - timedelta(microseconds=1)).offers) == (
        soon.offer.offer_id,
    )
    assert reader.read(at=expiry).offers == ()
    denylist.path.write_bytes(b"broken")
    with pytest.raises(SnapshotError, match="withdrawal ledger"):
        reader.read(at=expiry)
    denylist.path.unlink()
    with pytest.raises(SnapshotError, match="withdrawal ledger"):
        reader.read(at=expiry)


def test_read_degrades_ranking_before_hard_expiry(
    tmp_path: Path, stored: StoredFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    # Isolate reader expiry policy from the slice's shorter 71-hour publication policy.
    # These in-memory read results are never sent to the publisher or written to disk.
    initial = reader.read(at=_BIRTH)
    expiry = item.offer.observed_at + timedelta(hours=90)
    monkeypatch.setattr(
        "cu_rates.snapshot.reader.verify_database",
        lambda *_: (
            initial.info,
            tuple(o.model_copy(update={"valid_until": expiry}) for o in initial.offers),
        ),
    )
    assert (
        reader.read(at=item.offer.observed_at + timedelta(hours=72)).offers[0].placement
        is Placement.RANKED
    )
    assert (
        reader.read(at=item.offer.observed_at + timedelta(hours=73)).offers[0].placement
        is Placement.BROWSE_ONLY
    )
    assert reader.read(at=expiry).offers == ()


@pytest.mark.parametrize("operation", ["read", "rollback"])
@pytest.mark.parametrize(
    "defect",
    [
        "legacy",
        "protocol-1",
        "snapshot-legacy",
        "snapshot-protocol-1",
        "missing-digest",
        "bad-digest",
        "single-note_rate",
        "single-apr",
        "single-points",
        "missing-apr",
        "missing-reference",
        "duplicate-capture",
        "duplicate-time",
        "duplicate-artifact",
        "wrong-value",
        "wrong-product",
        "wrong-scenario",
        "wrong-source",
        "non-authoritative",
        "wrong-content-hash",
        "future-capture",
        "outside-window",
        "stale-at-release",
    ],
)
def test_protocol2_reader_and_rollback_refuse_ineligible_sealed_rows(
    tmp_path: Path, stored: StoredFixture, defect: str, operation: str
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("retained", item), conn=stored.conn, store=stored.run.store)
    publisher.build(
        release("good", item, supersedes="retained"), conn=stored.conn, store=stored.run.store
    )
    pointer = publisher.root / "current.json"
    good_pointer = pointer.read_bytes()
    directory = publisher.root / "versions" / "retained"
    database = directory / "published.sqlite"
    # Corrupt only the admission evidence of a gate-admitted snapshot. Re-seal
    # its outer digest so rejection must come from protocol/corroboration checks.
    database.chmod(0o644)
    with closing(sqlite3.connect(database)) as conn, conn:
        (serialized,) = conn.execute("SELECT payload FROM public_offers").fetchone()
        payload = json.loads(serialized)
        details = payload["details"]
        if defect == "legacy":
            for key in (
                "publication_version",
                "publication_evidence",
                "publication_evidence_sha256",
            ):
                details.pop(key, None)
        elif defect == "protocol-1":
            details["publication_version"] = "1.0"
        elif defect.startswith("snapshot-"):
            (metadata,) = conn.execute("SELECT payload FROM snapshot_info").fetchone()
            info = json.loads(metadata)
            if defect == "snapshot-legacy":
                info.pop("publication_version")
            else:
                info["publication_version"] = "1.0"
            conn.execute("UPDATE snapshot_info SET payload=?", (json.dumps(info),))
        elif defect == "missing-digest":
            details.pop("publication_evidence_sha256")
        elif defect == "bad-digest":
            details["publication_evidence_sha256"] = "0" * 64
        else:
            paths = details["publication_evidence"]
            if defect.startswith("single-"):
                field = defect.removeprefix("single-")
                retained_artifact = next(p["artifact_id"] for p in paths if p["path"] == field)
                # Other fields retain both references: corroboration is per value.
                paths[:] = [
                    p for p in paths if p["path"] != field or p["artifact_id"] == retained_artifact
                ]
                assert len([p for p in paths if p["path"] == field]) == 1
            elif defect == "missing-apr":
                paths[:] = [p for p in paths if p["path"] != "apr"]
            elif defect == "missing-reference":
                payload["evidence"] = [p for p in payload["evidence"] if p["path"] != "apr"]
            else:
                first, second = [p for p in paths if p["path"] == "note_rate"]
                if defect == "duplicate-capture":
                    second["capture_id"] = first["capture_id"]
                elif defect == "duplicate-time":
                    second["observed_at"] = first["observed_at"]
                elif defect == "duplicate-artifact":
                    second["artifact_id"] = first["artifact_id"]
                    second["content_sha256"] = first["content_sha256"]
                elif defect == "wrong-value":
                    second["value"] = "9.125"
                elif defect == "wrong-product":
                    second["product_id"] = "another-product"
                elif defect == "wrong-scenario":
                    second["scenario_id"] = CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED.value
                elif defect == "wrong-source":
                    second["source_id"] = "another-source"
                elif defect == "non-authoritative":
                    second["source_class"] = "web_search"
                elif defect == "wrong-content-hash":
                    second["content_sha256"] = "0" * 64
                elif defect == "future-capture":
                    second["observed_at"] = (
                        item.offer.observed_at + timedelta(seconds=1)
                    ).isoformat()
                elif defect == "outside-window":
                    second["observed_at"] = (
                        item.offer.observed_at - timedelta(hours=97)
                    ).isoformat()
                else:
                    assert defect == "stale-at-release"
                    # The persisted release is only a minute after observation.
                    # Keep the pair within its observation-time window, but stale
                    # at release, so only the release-time freshness gate refuses it.
                    stale = _BIRTH - timedelta(hours=96, microseconds=1)
                    assert item.offer.observed_at - timedelta(hours=96) <= stale
                    second["observed_at"] = stale.isoformat()
            # Rebind the public receipt too: an invalid pair must fail even when
            # its receipt digest and the sealed file digest are both consistent.
            details["publication_evidence_sha256"] = public_evidence_sha256(
                payload["offer_id"],
                OfferFingerprints.model_validate(payload["fingerprints"]),
                tuple(PublicEvidencePath.model_validate(p) for p in paths),
            )
            if defect == "stale-at-release":
                PublicOffer.model_validate(payload).require_protocol2()
        conn.execute("UPDATE public_offers SET payload=?", (json.dumps(payload),))
    database.chmod(0o444)
    version = Version(version=1, snapshot_id="retained", database_sha256=database_digest(database))
    descriptor = directory / "version.json"
    descriptor.chmod(0o644)
    descriptor.write_text(version.model_dump_json())
    descriptor.chmod(0o444)
    if operation == "read":
        pointer.write_text(version.model_dump_json())
        with pytest.raises(SnapshotError):
            reader.read(at=_BIRTH)
        pointer.write_bytes(good_pointer)
    else:
        with pytest.raises(SnapshotError):
            publisher.rollback("retained")
    assert pointer.read_bytes() == good_pointer
    assert reader.read(at=_BIRTH).info.snapshot_id == "good"


def test_protocol2_admission_survives_public_read_and_rollback(
    tmp_path: Path, stored: StoredFixture
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    publisher.build(
        release("second", item, supersedes="first"), conn=stored.conn, store=stored.run.store
    )
    assert publisher.rollback("first").snapshot_id == "first"
    (visible,) = reader.read(at=_BIRTH).offers
    assert visible.details["publication_version"] == "2.0"
    assert visible.details["publication_evidence_sha256"]
    evidence = visible.details["publication_evidence"]
    assert isinstance(evidence, list)
    assert visible.points is not None
    for path in (*(r.path for r in visible.rates), "points"):
        pair = [p for p in evidence if isinstance(p, dict) and p["path"] == path]
        assert len(pair) == 2
        for key in ("artifact_id", "content_sha256", "capture_id", "observed_at"):
            assert pair[0][key] != pair[1][key]


@pytest.mark.parametrize("scope", list(WithdrawalScope))
def test_database_withdrawal_suppresses_public_snapshot_before_sql_and_survives_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: WithdrawalScope,
    stored: StoredFixture,
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    moment = _BIRTH + timedelta(minutes=1)
    target = {
        WithdrawalScope.SOURCE: item.offer.source_id,
        WithdrawalScope.OFFER: item.offer.offer_id,
        WithdrawalScope.PRODUCT: item.offer.product_id,
        WithdrawalScope.INSTITUTION: str(item.offer.institution_charter),
        WithdrawalScope.SNAPSHOT: "first",
    }[scope]
    entry = make.withdrawal(scope, target, withdrawn_at=moment)
    engine = open_operational_database(tmp_path / "operational.sqlite")
    metadata.create_all(engine)
    assert reader.read(at=moment).offers
    with engine.begin() as conn:
        original = conn.execute

        def crash(*args: Any, **kwargs: Any) -> Any:
            assert reader.read(at=moment).offers == ()
            raise RuntimeError("crash before any database withdrawal record")

        monkeypatch.setattr(conn, "execute", crash)
        with pytest.raises(RuntimeError, match="crash before"):
            insert_withdrawal(conn, entry, denylist=denylist)
        monkeypatch.setattr(conn, "execute", original)
    assert reader.read(at=moment).offers == ()
    with pytest.raises(RuntimeError, match="transaction rollback"), engine.begin() as conn:
        insert_withdrawal(conn, entry, denylist=denylist)
        raise RuntimeError("transaction rollback")
    with engine.connect() as conn:
        assert not conn.execute(select(withdrawal_entries)).all()
    ledger = denylist.path.read_bytes()
    for _ in range(2):
        with engine.begin() as conn:
            insert_withdrawal(conn, entry, denylist=denylist)
    assert denylist.path.read_bytes() == ledger
    with engine.connect() as conn:
        assert len(conn.execute(select(withdrawal_entries)).all()) == 1
    assert reader.read(at=moment).offers == ()
    engine.dispose()


def test_denylist_idempotency_cannot_overwrite_or_reactivate_a_lifted_entry(tmp_path: Path) -> None:
    _, _, denylist = setup_storage(tmp_path)
    entry = make.withdrawal(WithdrawalScope.SOURCE, "source-one", withdrawn_at=_BIRTH)
    denylist.withdraw(entry)
    before = denylist.path.read_bytes()
    denylist.withdraw(entry)
    assert denylist.path.read_bytes() == before
    with pytest.raises(ValueError, match="different content"):
        denylist.withdraw(entry.model_copy(update={"target_id": "source-two"}))
    denylist.lift(entry.withdrawal_id, at=_BIRTH + timedelta(seconds=1), actor="Synthetic owner")
    before = denylist.path.read_bytes()
    with pytest.raises(ValueError, match="different content"):
        denylist.withdraw(entry)
    assert denylist.path.read_bytes() == before

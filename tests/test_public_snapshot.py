"""Public snapshot integration checks (fictional rates and sources only)."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import select

from cu_rates.contracts.audit import PublishedSnapshot, SnapshotEntry, snapshot_manifest_sha256
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    OfferStatus,
    ReviewVerdict,
    WithdrawalScope,
)
from cu_rates.contracts.fingerprints import OfferFingerprints, offer_fingerprints
from cu_rates.contracts.publication import Placement, evaluate_publication
from cu_rates.db import metadata, open_operational_database
from cu_rates.db.schema import withdrawal_entries
from cu_rates.pipeline.extract import extract_fixture
from cu_rates.pipeline.slice import GIT_SHA
from cu_rates.pipeline.trusted import insert_withdrawal, load_publication_context
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
from .test_validation import claim, draft

_BIRTH = make.EVALUATED_AT


def row(
    offer_id: str = "offer-001", *, now: datetime = _BIRTH, validity: timedelta = timedelta(days=3)
) -> SnapshotCandidate:
    effective = make.T0 - timedelta(days=1)
    date = claim("effective_at", effective.isoformat())
    offer = draft(
        offer_id=offer_id,
        status=OfferStatus.APPROVED,
        effective_at=effective,
        supporting_evidence=(date.provenance, make.provenance("PRIVATE CAPTURE TEXT")),
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED,
        scenario_matches=make.purchase_offer().scenario_matches,
        valid_until=make.T0 + validity,
    )
    ctx = make.context(offer, evaluated_at=now)
    return SnapshotCandidate(offer=offer, context=ctx, candidates=(date,))


def release(
    name: str,
    *rows: SnapshotCandidate,
    supersedes: str | None = None,
    review_ids: tuple[str, ...] = (),
) -> PublishedSnapshot:
    entries = []
    for item in rows:
        fp = offer_fingerprints(item.offer)
        entries.append(
            SnapshotEntry(
                offer_id=item.offer.offer_id, structural_sha256=fp.structural, value_sha256=fp.value
            )
        )
    ordered = tuple(sorted(entries, key=lambda entry: entry.offer_id))
    return PublishedSnapshot(
        schema_version=make.SV,
        snapshot_id=name,
        published_at=rows[0].context.evaluated_at,
        published_by="synthetic-publication-runner",
        entries=ordered,
        manifest_sha256=snapshot_manifest_sha256(ordered),
        supersedes=supersedes,
        review_ids=review_ids,
    )


def setup_storage(tmp_path: Path) -> tuple[SnapshotPublisher, SnapshotReader, Denylist]:
    publisher = SnapshotPublisher(tmp_path / "snapshots with spaces")
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()
    return publisher, SnapshotReader(publisher.root, denylist.path), denylist


def test_snapshot_records_provenance_generation_and_immutable_format(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    assert evaluate_publication(item.offer, item.context).ranking_eligible
    manifest = release("first", item)
    info = publisher.build(manifest, [item])
    view = reader.read(at=_BIRTH)
    assert view.info == info
    assert info.manifest_sha256 == manifest.manifest_sha256
    assert info.published_at == _BIRTH and info.published_by == manifest.published_by
    (visible,) = view.offers
    assert visible.review_status == "automatic" and visible.placement is Placement.RANKED
    assert visible.source_url == make.SOURCE_URL
    assert visible.observed_at == item.offer.observed_at
    assert visible.evidence and {part.artifact_id for part in visible.evidence} == {
        make.ARTIFACT_ID,
        make.ARTIFACT_ID_2,
    }
    assert {part.path for part in visible.evidence} >= {
        "note_rate",
        "apr",
        "fields.details.term_months",
    }
    assert {value.path: value.value for value in visible.rates} == {
        "note_rate": Decimal("6.125"),
        "apr": Decimal("6.250"),
    }
    database = publisher.root / "versions" / "first" / "published.sqlite"
    assert database.is_file() and database.stat().st_mode & 0o222 == 0
    assert not list(database.parent.glob("published.sqlite-*"))
    assert b"PRIVATE CAPTURE TEXT" not in database.read_bytes()
    assert b"source_text" not in database.read_bytes()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM public_offers")


def test_a_dated_t014_human_approval_is_exported_without_web_access_to_worker(
    tmp_path: Path,
) -> None:
    pilot = reviewed_fixture(tmp_path / "pilot")
    assert pilot.reviewed_snapshot is not None
    as_of = pilot.reviewed_snapshot.published_at - timedelta(microseconds=1)
    worker = open_operational_database(pilot.database)
    offer_id = pilot.offers["purchase-30-year"].offer_id
    with worker.connect() as conn:
        view = load_publication_context(conn, offer_id, as_of, store=pilot.store)
    worker.dispose()
    assert view.current_review is not None
    artifact = view.context.known_artifacts[0]
    extracted = extract_fixture(
        "athol-style-dated",
        pilot.store.read(artifact),
        artifact_id=artifact.artifact_id,
        extraction_run_id=f"slice-{artifact.fetch_run_id}",
        git_sha=GIT_SHA,
    )
    candidates = next(row.candidates for row in extracted.rows if row.row_id == "purchase-30-year")
    item = SnapshotCandidate(
        offer=view.offer,
        context=view.context,
        candidates=candidates,
        current_review=view.current_review,
    )
    manifest = release("reviewed", item, review_ids=(view.current_review.review_id,))
    publisher, reader, _ = setup_storage(tmp_path)
    publisher.build(manifest, [item])
    # The web API has no worker path or connection and still reads after the worker is gone.
    pilot.database.rename(pilot.database.with_name("unavailable.sqlite"))
    (visible,) = reader.read(at=as_of).offers
    assert visible.review_status == "reviewed" and visible.placement is Placement.RANKED
    assert visible.effective_at == view.offer.effective_at


def test_t014_refresh_without_t045_compatible_capture_does_not_activate(tmp_path: Path) -> None:
    pilot = reviewed_fixture(tmp_path / "pilot")
    assert pilot.refreshed_snapshot is not None
    as_of = pilot.refreshed_snapshot.published_at
    worker = open_operational_database(pilot.database)
    offer_id = pilot.offers["purchase-30-year"].offer_id
    with worker.connect() as conn:
        view = load_publication_context(conn, offer_id, as_of, store=pilot.store)
    assert evaluate_publication(view.offer, view.context).publishable_now
    artifact = view.context.known_artifacts[0]
    assert artifact.fetched_at != view.offer.observed_at
    extracted = extract_fixture(
        "athol-style-dated",
        pilot.store.read(artifact),
        artifact_id=artifact.artifact_id,
        extraction_run_id=f"slice-{artifact.fetch_run_id}",
        git_sha=GIT_SHA,
    )
    candidates = next(row.candidates for row in extracted.rows if row.row_id == "purchase-30-year")
    item = SnapshotCandidate(offer=view.offer, context=view.context, candidates=candidates)
    publisher, _, _ = setup_storage(tmp_path)
    with pytest.raises(SnapshotError, match="T045 refused"):
        publisher.build(release("not-validated", item), [item])
    assert not (publisher.root / "current.json").exists()


def test_review_must_bind_exact_offer_evidence(tmp_path: Path) -> None:
    publisher, _, _ = setup_storage(tmp_path)
    initial = row()
    review = make.review(initial.offer).model_copy(
        update={
            "decided_at": _BIRTH,
            "reviewed_evidence_sha256": "d0" * 32,
        }
    )
    item = initial.model_copy(
        update={
            "context": initial.context.model_copy(update={"prior": None}),
            "current_review": review,
        }
    )
    with pytest.raises(SnapshotError, match="publication gate refused"):
        publisher.build(release("not-reviewed", item, review_ids=(review.review_id,)), [item])


def test_reader_never_sees_a_partial_version_during_build_and_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    originals = [row("old-a"), row("old-b")]
    replacements = [row("new-a"), row("new-b"), row("new-c")]
    publisher.build(release("first", *originals), originals)
    expected = {
        "first": ("old-a", "old-b"),
        "second": ("new-a", "new-b", "new-c"),
    }
    staged = Event()
    swap = Event()
    observed_old = Event()
    observed_new = Event()
    stop = Event()
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(
            publisher.build, release("second", *replacements, supersedes="first"), replacements
        )
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


@pytest.mark.parametrize(
    "mode",
    [
        "no worker validation",
        "pending review",
        "rejected review",
        "quarantined",
        "t045 contradiction",
        "missing date candidate",
    ],
)
def test_ineligible_rows_never_activate_or_partially_publish(tmp_path: Path, mode: str) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    clean = row()
    publisher.build(release("first", clean), [clean])
    pending = row("pending")
    if mode == "no worker validation":
        pending = pending.model_copy(
            update={"context": pending.context.model_copy(update={"validations": ()})}
        )
    elif mode == "pending review":
        pending = pending.model_copy(
            update={"context": pending.context.model_copy(update={"prior": None})}
        )
    elif mode == "rejected review":
        rejected = make.review(pending.offer, ReviewVerdict.REJECTED)
        pending = pending.model_copy(update={"current_review": rejected})
    elif mode == "quarantined":
        pending = pending.model_copy(
            update={
                "offer": pending.offer.model_copy(update={"status": OfferStatus.QUARANTINED}),
            }
        )
    elif mode == "t045 contradiction":
        assert evaluate_publication(pending.offer, pending.context).publishable_now
        pending = pending.model_copy(
            update={
                "candidates": (*pending.candidates, claim("apr", "8.125%", suffix="contradiction")),
            }
        )
    else:
        assert evaluate_publication(pending.offer, pending.context).publishable_now
        pending = pending.model_copy(update={"candidates": ()})
    pointer = publisher.root / "current.json"
    before = pointer.read_bytes()
    error = (
        "T045 refused"
        if mode in ("t045 contradiction", "missing date candidate")
        else "refused|publishable|approved"
    )
    with pytest.raises(SnapshotError, match=error):
        publisher.build(release("second", clean, pending, supersedes="first"), [clean, pending])
    assert pointer.read_bytes() == before
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"
    assert not (publisher.root / "versions" / "second").exists()


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    clean = row()
    publisher.build(release("first", clean), [clean])
    second = release("second", clean, supersedes="first")
    pointer = publisher.root / "current.json"
    before = pointer.read_bytes()
    with pytest.raises(SnapshotError):
        publisher.build(second.model_copy(update={"manifest_sha256": "0" * 64}), [clean])
    write_real = builder._write_database

    def invalid_staging(path: Path, info: SnapshotInfo, rows: tuple[PublicOffer, ...]) -> None:
        write_real(path, info, rows)
        _change_manifest_in_database(path)

    monkeypatch.setattr(builder, "_write_database", invalid_staging)
    with pytest.raises(SnapshotError, match="manifest"):
        publisher.build(second, [clean])
    assert pointer.read_bytes() == before
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"
    assert not list((publisher.root / "versions").glob(".build-*"))


def test_read_rebuilds_manifest_and_rollback_refuses_a_bad_version(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
    publisher.build(release("second", item, supersedes="first"), [item])
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


def test_read_rejects_nonmanifest_field_tampering_and_sidecars(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
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


def test_rollback_preserves_sealed_versions_and_refuses_reusing_an_id(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
    second = release("second", item, supersedes="first")
    publisher.build(second, [item])
    database = publisher.root / "versions" / "second" / "published.sqlite"
    original = database.read_bytes()
    publisher.rollback("first")
    with pytest.raises(SnapshotError, match="already exists"):
        publisher.build(second, [item])
    assert database.read_bytes() == original
    assert reader.read(at=_BIRTH).info.snapshot_id == "first"


def test_pointer_cannot_select_an_operational_database_path(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
    stable = publisher.root / "current.json"
    stable.write_bytes(stable.read_bytes().replace(b'"first"', b'"../../operational.sqlite"'))
    with pytest.raises(SnapshotError, match="pointer"):
        reader.read(at=_BIRTH)
    with pytest.raises(SnapshotError, match="rollback"):
        publisher.rollback("../../operational.sqlite")


@pytest.mark.parametrize("scope", list(WithdrawalScope))
def test_independent_emergency_withdrawal_enforces_every_scope_at_read_time(
    tmp_path: Path,
    scope: WithdrawalScope,
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
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
    tmp_path: Path,
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    soon = row("soon", validity=timedelta(hours=2))
    held = row("held", validity=timedelta(hours=90))
    publisher.build(release("first", soon, held), [soon, held])
    publisher.build(release("second", held, supersedes="first"), [held])
    publisher.rollback("first")
    expiry = soon.offer.valid_until
    assert expiry is not None
    denylist.withdraw(make.withdrawal(WithdrawalScope.OFFER, "held", withdrawn_at=_BIRTH))
    assert tuple(x.offer_id for x in reader.read(at=expiry - timedelta(microseconds=1)).offers) == (
        "soon",
    )
    assert reader.read(at=expiry).offers == ()
    denylist.path.write_bytes(b"broken")
    with pytest.raises(SnapshotError, match="withdrawal ledger"):
        reader.read(at=expiry)
    denylist.path.unlink()
    with pytest.raises(SnapshotError, match="withdrawal ledger"):
        reader.read(at=expiry)


def test_read_degrades_ranking_before_hard_expiry(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row(validity=timedelta(hours=90))
    publisher.build(release("first", item), [item])
    assert reader.read(at=make.T0 + timedelta(hours=72)).offers[0].placement is Placement.RANKED
    assert (
        reader.read(at=make.T0 + timedelta(hours=73)).offers[0].placement is Placement.BROWSE_ONLY
    )
    assert reader.read(at=make.T0 + timedelta(hours=90)).offers == ()


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
    tmp_path: Path, defect: str, operation: str
) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("retained", item), [item])
    publisher.build(release("good", item, supersedes="retained"), [item])
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
                # Other fields retain both references: corroboration is per value.
                paths[:] = [
                    p for p in paths if p["path"] != field or p["artifact_id"] == make.ARTIFACT_ID
                ]
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
                    second["observed_at"] = (
                        item.offer.observed_at - timedelta(hours=95, minutes=30)
                    ).isoformat()
            # Rebind the public receipt too: an invalid pair must fail even when
            # its receipt digest and the sealed file digest are both consistent.
            details["publication_evidence_sha256"] = public_evidence_sha256(
                payload["offer_id"],
                OfferFingerprints.model_validate(payload["fingerprints"]),
                tuple(PublicEvidencePath.model_validate(p) for p in paths),
            )
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


def test_protocol2_admission_survives_public_read_and_rollback(tmp_path: Path) -> None:
    publisher, reader, _ = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
    publisher.build(release("second", item, supersedes="first"), [item])
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: WithdrawalScope
) -> None:
    publisher, reader, denylist = setup_storage(tmp_path)
    item = row()
    publisher.build(release("first", item), [item])
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

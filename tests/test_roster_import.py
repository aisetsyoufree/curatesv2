"""Tests for NCUA credit union roster import, provenance, and isolation."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import InstitutionStatus
from cu_rates.db import (
    AppendOnlyError,
    open_operational_database,
    upgrade_database,
)
from cu_rates.db.schema import institutions, roster_import_entries, roster_imports
from cu_rates.roster import (
    RosterColumnMapping,
    RosterImporter,
    import_roster,
)
from cu_rates.snapshot import Denylist


def _denylist(tmp_path: Path) -> Denylist:
    ledger = Denylist(tmp_path / "independent" / "withdrawals.json")
    ledger.initialize()
    return ledger


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "roster"
V1_PATH = FIXTURES_DIR / "ncua_roster_v1.csv"
V2_PATH = FIXTURES_DIR / "ncua_roster_v2.csv"
FROZEN_TIME = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "operational.sqlite"
    upgrade_database(db_path)
    return db_path


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "evidence")


def test_import_first_version_and_contract_validation(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    assert not report.is_noop
    assert report.total_rows == 12
    assert report.ma_rows == 10
    assert report.skipped_non_ma_count == 2
    assert len(report.malformed_rows) == 3
    assert report.collapsed_duplicate_charters == (99001,)
    assert report.rejected_duplicate_charters == (99005,)
    assert len(report.inserted_charters) == 4
    assert set(report.inserted_charters) == {99001, 99002, 99003, 99004}

    with engine.connect() as conn:
        inst_rows = conn.execute(
            select(
                institutions.c.charter_number,
                institutions.c.legal_name,
                institutions.c.status,
                institutions.c.successor_charter_number,
                institutions.c.charter_verified,
            ).order_by(institutions.c.charter_number)
        ).all()

        assert len(inst_rows) == 4
        # Charter verified is NEVER set to True during roster import (T021 human decision).
        for r in inst_rows:
            assert r.charter_verified == 0

        # Check roster_imports record
        import_row = conn.execute(select(roster_imports)).first()
        assert import_row is not None
        assert import_row.import_id == report.import_id
        assert import_row.artifact_id == report.artifact_id
        assert import_row.source_version == "2026-09-01"
        assert import_row.source_as_of_date == "2026-09-01"
        assert import_row.row_count == 12
        assert import_row.imported_count == 4

        # Check roster_import_entries composite key rows
        entries = conn.execute(
            select(roster_import_entries).where(
                roster_import_entries.c.import_id == report.import_id
            )
        ).all()
        assert len(entries) == 4
        charters = {e.charter_number for e in entries}
        assert charters == {99001, 99002, 99003, 99004}


def test_repeat_import_identical_bytes_is_noop(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    first = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )
    assert not first.is_noop

    second = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )
    assert second.is_noop
    assert second.import_id == first.import_id
    assert second.artifact_id == first.artifact_id

    with engine.connect() as conn:
        import_count = conn.execute(select(roster_imports)).scalars().all()
        assert len(import_count) == 1
        inst_count = conn.execute(select(institutions)).scalars().all()
        assert len(inst_count) == 4


def test_second_source_version_updates_institutions_and_retains_dates(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    v1_report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )
    assert not v1_report.is_noop

    time_v2 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    v2_report = import_roster(
        V2_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-10-01",
        clock=lambda: time_v2,
    )
    assert not v2_report.is_noop
    assert v2_report.updated_charters == (99002,)
    assert v2_report.inserted_charters == (99010,)
    assert set(v2_report.unchanged_charters) == {99001, 99003, 99004}

    with engine.connect() as conn:
        # Check institution 99002 was updated with new name
        row_99002 = conn.execute(
            select(institutions.c.legal_name).where(institutions.c.charter_number == 99002)
        ).scalar_one()
        assert row_99002 == "Minuteman Renamed Credit Union"

        # Check 99010 was newly inserted
        row_99010 = conn.execute(
            select(institutions.c.legal_name).where(institutions.c.charter_number == 99010)
        ).scalar_one()
        assert row_99010 == "Cape Cod Synthetic Credit Union"

        # Check total institutions is now 5
        total_insts = conn.execute(select(institutions)).all()
        assert len(total_insts) == 5

        # Check roster_import_entries retains each row's source as-of date across versions
        v1_entries = conn.execute(
            select(roster_import_entries).where(
                roster_import_entries.c.import_id == v1_report.import_id
            )
        ).all()
        assert len(v1_entries) == 4
        for e in v1_entries:
            assert e.source_as_of_date == "2026-09-01"

        v2_entries = conn.execute(
            select(roster_import_entries).where(
                roster_import_entries.c.import_id == v2_report.import_id
            )
        ).all()
        assert len(v2_entries) == 5
        for e in v2_entries:
            assert e.source_as_of_date == "2026-10-01"


def test_raw_artifact_round_trip(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    stored_bytes = store.read_body(report.artifact_id)
    assert stored_bytes == V1_PATH.read_bytes()


def test_duplicate_charter_resolution(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    # 99001 had exact duplicate rows: collapsed to one
    assert report.collapsed_duplicate_charters == (99001,)
    # 99005 had conflicting duplicate rows: rejected
    assert report.rejected_duplicate_charters == (99005,)
    assert len(report.rejected_duplicates) == 1
    conflict = report.rejected_duplicates[0]
    assert conflict.charter_number == 99005
    assert "Credit Union Name" in conflict.differing_fields
    assert "Status" in conflict.differing_fields

    with engine.connect() as conn:
        assert (
            conn.execute(select(institutions).where(institutions.c.charter_number == 99005)).first()
            is None
        )
        assert (
            conn.execute(select(institutions).where(institutions.c.charter_number == 99001)).first()
            is not None
        )


def test_closures_and_mergers_explicitly_handled(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    assert report.mergers == (99003,)
    assert report.closures == (99004,)

    with engine.connect() as conn:
        merged = conn.execute(
            select(institutions).where(institutions.c.charter_number == 99003)
        ).first()
        assert merged is not None
        assert merged.status == InstitutionStatus.MERGED.value
        assert merged.successor_charter_number == 99001

        closed = conn.execute(
            select(institutions).where(institutions.c.charter_number == 99004)
        ).first()
        assert closed is not None
        assert closed.status == InstitutionStatus.LIQUIDATED.value
        assert closed.successor_charter_number is None


def test_malformed_rows_reported_and_not_dropped(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    assert len(report.malformed_rows) == 3
    reasons = [m.reason for m in report.malformed_rows]
    assert any("Invalid charter number" in r for r in reasons)
    assert any("Merged institution requires successor charter" in r for r in reasons)
    assert any("Unrecognized status" in r for r in reasons)


def test_non_ma_rows_skipped_from_registry(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    assert report.skipped_non_ma_count == 2
    assert "99006" in report.skipped_non_ma_charters
    assert "99007" in report.skipped_non_ma_charters

    with engine.connect() as conn:
        for non_ma_charter in (99006, 99007):
            assert (
                conn.execute(
                    select(institutions).where(institutions.c.charter_number == non_ma_charter)
                ).first()
                is None
            )


def test_custom_column_mapping(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    custom_csv = (
        b"CU_ID,NAME,ST,STUS,TOWN,INACTIVE_DT,SUCC_ID,AS_OF\n"
        b"99100,Custom Mapped CU,MA,Active,Newton,,,2026-09-01\n"
    )

    mapping = RosterColumnMapping(
        charter_number="CU_ID",
        legal_name="NAME",
        state="ST",
        status="STUS",
        city="TOWN",
        closure_or_merger_date="INACTIVE_DT",
        successor_charter="SUCC_ID",
        source_as_of_date="AS_OF",
    )

    importer = RosterImporter(
        engine,
        store,
        mapping=mapping,
        clock=lambda: FROZEN_TIME,
    )
    report = importer.import_roster(custom_csv, source_version="custom-v1")

    assert not report.is_noop
    assert report.inserted_charters == (99100,)
    with engine.connect() as conn:
        row = conn.execute(
            select(institutions).where(institutions.c.charter_number == 99100)
        ).first()
        assert row is not None
        assert row.legal_name == "Custom Mapped CU"


def test_append_only_guards_on_roster_tables(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    store = _store(tmp_path)
    engine = open_operational_database(db_path)

    report = import_roster(
        V1_PATH,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN_TIME,
    )

    iid = report.import_id
    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(text(f"UPDATE roster_imports SET row_count = 999 WHERE import_id = '{iid}'"))

    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(text(f"DELETE FROM roster_imports WHERE import_id = '{iid}'"))

    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE roster_import_entries SET legal_name = 'Changed' WHERE import_id = '{iid}'"
            )
        )

    with pytest.raises(AppendOnlyError), engine.begin() as conn:
        conn.execute(text(f"DELETE FROM roster_import_entries WHERE import_id = '{iid}'"))

    # Raw connection also rejects mutation via triggers
    raw = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(f"UPDATE roster_imports SET row_count = 999 WHERE import_id = '{iid}'")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(f"DELETE FROM roster_imports WHERE import_id = '{iid}'")
    raw.close()

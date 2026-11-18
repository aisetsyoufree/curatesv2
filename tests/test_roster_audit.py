"""Offline regression tests for the AUD4 roster-importer findings."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select

from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.schema import institutions, roster_imports
from cu_rates.roster import RosterImporter, import_roster
from cu_rates.roster.importer import MAX_FIELD_LENGTH, MAX_ROSTER_BYTES, MAX_ROSTER_ROWS
from tests.test_roster_import import FIXTURES_DIR, _database, _denylist, _store

FROZEN = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
HEADER = "Charter,Credit Union Name,State,Status,City,Date Inactive,Successor Charter,As of Date"


def _csv(*lines: str) -> bytes:
    return (HEADER + "\n" + "\n".join(lines) + "\n").encode()


def _line(
    charter: int | str,
    name: str = "Synthetic Credit Union",
    *,
    state: str = "MA",
    status: str = "Active",
    city: str = "Boston",
    inactive: str = "",
    successor: str = "",
    as_of: str = "2026-09-01",
) -> str:
    return f"{charter},{name},{state},{status},{city},{inactive},{successor},{as_of}"


def _verify(engine: object, charter: int) -> None:
    with engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            institutions.update()
            .where(institutions.c.charter_number == charter)
            .values(charter_verified=1)
        )


def _institution(engine: object, charter: int) -> object:
    with engine.connect() as conn:  # type: ignore[attr-defined]
        return conn.execute(
            select(institutions).where(institutions.c.charter_number == charter)
        ).one()


def test_older_snapshot_does_not_reactivate_a_verified_merger(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    newer = _csv(
        _line(88001, status="Merged", inactive="2026-01-15", successor="88002", as_of="2026-10-01"),
        _line(88002, "Successor Synthetic CU", as_of="2026-10-01"),
    )
    import_roster(
        newer,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-10-01",
        clock=lambda: FROZEN,
    )
    _verify(engine, 88001)
    older = _csv(
        _line(88001, as_of="2026-09-01"),
        _line(88002, "Successor Synthetic CU", as_of="2026-09-01"),
    )
    report = import_roster(
        older,
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert 88001 in report.stale_charters
    row = _institution(engine, 88001)
    assert row.status == "merged"  # type: ignore[attr-defined]
    assert row.successor_charter_number == 88002  # type: ignore[attr-defined]
    assert row.charter_verified == 1  # type: ignore[attr-defined]


def test_verified_identity_change_is_not_applied(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    import_roster(
        _csv(_line(88010, "Original Synthetic CU")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-09-01",
        clock=lambda: FROZEN,
    )
    _verify(engine, 88010)
    report = import_roster(
        _csv(_line(88010, "Renamed Synthetic CU", as_of="2026-10-01")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-10-01",
        clock=lambda: FROZEN,
    )
    assert report.review_charters == (88010,)
    assert report.updated_charters == ()
    row = _institution(engine, 88010)
    assert row.legal_name == "Original Synthetic CU"  # type: ignore[attr-defined]
    assert row.charter_verified == 1  # type: ignore[attr-defined]


def test_malformed_duplicate_conflicts_and_unverified_charter_is_not_closed(
    tmp_path: Path,
) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    import_roster(
        _csv(_line(88020)),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-09-01",
        clock=lambda: FROZEN,
    )
    _verify(engine, 88020)
    report = import_roster(
        _csv(
            _line(88020, as_of="2026-10-01"),
            _line(88020, status="NotAStatus", as_of="2026-10-01"),
            _line(88021, "Other Synthetic CU", as_of="2026-10-01"),
        ),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-10-01",
        source_as_of_date="2026-10-01",
        clock=lambda: FROZEN,
    )
    assert 88020 in report.rejected_duplicate_charters
    assert 88020 in report.quarantined_charters
    row = _institution(engine, 88020)
    assert row.status == "active"  # type: ignore[attr-defined]
    assert row.charter_verified == 0  # type: ignore[attr-defined]


def test_omitted_and_non_ma_rows_do_not_infer_closure(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    import_roster(
        _csv(_line(88030), _line(88031, "Kept Synthetic CU")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-09-01",
        clock=lambda: FROZEN,
    )
    _verify(engine, 88030)
    omitted = import_roster(
        _csv(_line(88031, "Kept Synthetic CU", as_of="2026-10-01")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-10-01",
        clock=lambda: FROZEN,
    )
    assert 88030 in omitted.quarantined_charters
    omitted_row = _institution(engine, 88030)
    assert omitted_row.status == "active"  # type: ignore[attr-defined]
    assert omitted_row.charter_verified == 0  # type: ignore[attr-defined]

    _verify(engine, 88031)
    non_ma = import_roster(
        _csv(_line(88031, "Kept Synthetic CU", state="NH", as_of="2026-11-01")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="2026-11-01",
        source_as_of_date="2026-11-01",
        clock=lambda: FROZEN,
    )
    assert 88031 in non_ma.quarantined_charters
    kept = _institution(engine, 88031)
    assert kept.status == "active"  # type: ignore[attr-defined]
    assert kept.charter_verified == 0  # type: ignore[attr-defined]


def test_failed_import_inside_caller_transaction_does_not_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "operational.sqlite"
    upgrade_database(db_path)
    engine = open_operational_database(db_path)
    store = _store(tmp_path)
    seen = {"inserts": 0}

    def fail_second(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if "insert into institutions" in statement.lower():
            seen["inserts"] += 1
            if seen["inserts"] == 2:
                raise RuntimeError("forced write failure")

    payload = _csv(_line(88040), _line(88041, "Second Synthetic CU"))
    importer = RosterImporter(engine, store, clock=lambda: FROZEN)
    event.listen(engine, "before_cursor_execute", fail_second)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            caught = RosterImporter(conn, store, clock=lambda: FROZEN)
            with pytest.raises(RuntimeError, match="forced write failure"):
                caught.import_roster(payload, source_version="2026-09-01")
            outer.commit()
        assert caught.failed_artifacts
    finally:
        event.remove(engine, "before_cursor_execute", fail_second)

    with engine.connect() as conn:
        assert conn.execute(select(institutions)).all() == []
        assert conn.execute(select(roster_imports)).all() == []
    restored = importer.import_roster(payload, source_version="2026-09-01")
    assert set(restored.inserted_charters) == {88040, 88041}


def test_identical_reimport_checks_stored_evidence(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    body = (FIXTURES_DIR / "ncua_roster_v1.csv").read_bytes()
    import_roster(
        body,
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="2026-09-01",
        clock=lambda: FROZEN,
    )
    with pytest.raises(ValueError, match="replay metadata"):
        import_roster(
            body,
            engine_or_connection=engine,
            artifact_store=store,
            denylist=_denylist(tmp_path),
            source_version="different-version",
            clock=lambda: FROZEN,
        )
    for path in (tmp_path / "evidence" / "observations").rglob("*.json"):
        path.unlink()
    with pytest.raises(ValueError, match="stored roster evidence"):
        import_roster(
            body,
            engine_or_connection=engine,
            artifact_store=store,
            denylist=_denylist(tmp_path),
            source_version="2026-09-01",
            clock=lambda: FROZEN,
        )


def test_strict_csv_rejects_bad_shape_without_crashing(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="duplicate column"):
        import_roster(
            b"Charter,Charter,State,Status\n1,1,MA,Active\n",
            engine_or_connection=engine,
            artifact_store=store,
            source_version="dup-header",
            source_as_of_date="2026-09-01",
            clock=lambda: FROZEN,
        )
    with pytest.raises(ValueError, match="unterminated quote"):
        quoted = HEADER.encode() + b'\n"88050,Open\n'
        import_roster(
            quoted,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="quote",
            source_as_of_date="2026-09-01",
            clock=lambda: FROZEN,
        )
    surplus = import_roster(
        _csv(_line(88051) + ",extra"),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="surplus",
        source_as_of_date="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert any("surplus" in row.reason for row in surplus.malformed_rows)
    assert surplus.inserted_charters == ()
    short = import_roster(
        HEADER.encode() + b"\n88052,Only\n",
        engine_or_connection=engine,
        artifact_store=store,
        source_version="short",
        source_as_of_date="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert any("short row" in row.reason for row in short.malformed_rows)
    assert short.inserted_charters == ()


def test_budgets_and_oversized_fields_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    monkeypatch.setattr("cu_rates.roster.importer.MAX_ROSTER_BYTES", 20)
    tiny = import_roster(
        _csv(_line(88060)),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="bytes",
        source_as_of_date="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert tiny.errors
    assert "bytes" in tiny.errors[0]
    monkeypatch.setattr("cu_rates.roster.importer.MAX_ROSTER_BYTES", MAX_ROSTER_BYTES)
    monkeypatch.setattr("cu_rates.roster.importer.MAX_ROSTER_ROWS", 1)
    too_many = import_roster(
        _csv(_line(88061), _line(88062, "Second Synthetic CU")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="rows",
        source_as_of_date="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert too_many.errors
    assert too_many.inserted_charters == ()
    monkeypatch.setattr("cu_rates.roster.importer.MAX_ROSTER_ROWS", MAX_ROSTER_ROWS)
    monkeypatch.setattr("cu_rates.roster.importer.MAX_FIELD_LENGTH", 5)
    huge = import_roster(
        _csv(_line(88063, "Synthetic Credit Union")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="field",
        source_as_of_date="2026-09-01",
        clock=lambda: FROZEN,
    )
    assert any("characters" in row.reason for row in huge.malformed_rows)
    assert huge.inserted_charters == ()
    with engine.connect() as conn:
        assert conn.execute(select(institutions)).all() == []
    assert MAX_FIELD_LENGTH == 500


def test_dates_are_explicit_and_do_not_inherit(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    header = HEADER.encode() + b"\n"
    with pytest.raises(ValueError, match="source_as_of_date"):
        import_roster(
            header,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="empty",
            clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        )
    with engine.connect() as conn:
        assert conn.execute(select(roster_imports)).all() == []
    with pytest.raises(ValueError, match="source_version"):
        import_roster(
            _csv(_line(88070)),
            engine_or_connection=engine,
            artifact_store=store,
            source_version="  ",
            clock=lambda: FROZEN,
        )
    invalid = import_roster(
        _csv(_line(88071, as_of="2026-02-30")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="bad-date",
        clock=lambda: FROZEN,
    )
    assert any("Invalid as-of date" in row.reason for row in invalid.malformed_rows)
    assert invalid.inserted_charters == ()
    mixed = import_roster(
        _csv(_line(88072, as_of="2026-09-01"), _line(88073, "Other", as_of="2026-10-01")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="mixed",
        clock=lambda: FROZEN,
    )
    assert mixed.errors
    assert mixed.inserted_charters == ()
    missing = import_roster(
        _csv(_line(88074), _line(88075, "Undated", as_of="")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="missing-date",
        clock=lambda: FROZEN,
    )
    assert 88074 in missing.inserted_charters
    assert 88075 not in missing.inserted_charters
    assert any("Missing source as-of date" in row.reason for row in missing.malformed_rows)


def test_merger_cycles_and_inactive_dates_are_rejected(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    cycle = import_roster(
        _csv(
            _line(88080, status="Merged", inactive="2026-01-01", successor="88081"),
            _line(88081, "Cycle B", status="Merged", inactive="2026-01-01", successor="88080"),
        ),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="cycle",
        clock=lambda: FROZEN,
    )
    assert cycle.inserted_charters == ()
    assert any("merger cycle" in row.reason for row in cycle.malformed_rows)
    dated = import_roster(
        _csv(_line(88082, inactive="2026-01-01")),
        engine_or_connection=engine,
        artifact_store=store,
        source_version="dated-active",
        clock=lambda: FROZEN,
    )
    assert dated.inserted_charters == ()
    assert any("inactive date" in row.reason for row in dated.malformed_rows)
    chained = import_roster(
        _csv(
            _line(88083, "Closed", status="Liquidated", inactive="2026-01-01"),
            _line(88084, status="Merged", inactive="2026-02-01", successor="88083"),
        ),
        engine_or_connection=engine,
        artifact_store=store,
        denylist=_denylist(tmp_path),
        source_version="chain",
        clock=lambda: FROZEN,
    )
    assert chained.inserted_charters == (88083,)
    assert any("successor" in row.reason for row in chained.malformed_rows)
    with engine.connect() as conn:
        assert (
            conn.execute(select(institutions).where(institutions.c.charter_number == 88084)).first()
            is None
        )

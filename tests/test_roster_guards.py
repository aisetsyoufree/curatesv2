"""Roster history guards: every unique key, frozen SQL, and downgrade refusal.

Finding 4: INSERT OR REPLACE through artifact_id must not delete a completed
import or orphan its entries. The engine rejects the statement, and a raw
sqlite3 connection that never sets recursive_triggers is stopped by the
BEFORE INSERT trigger.

Finding 10: deployed roster trigger text is the literal SQL in
aud4b_roster_guards, compared here to sqlite_master. Each trigger is also
required to abort its own mutation, so a no-op body fails even if the names
still match.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text, update

from cu_rates.db import (
    AppendOnlyError,
    current_revision,
    downgrade_database,
    open_operational_database,
    upgrade_database,
)
from cu_rates.db.migrate import VERSIONS_DIR
from cu_rates.db.schema import (
    institutions,
    roster_import_entries,
    roster_imports,
    schema_triggers,
)

ARTIFACT = "ab" * 32
OTHER_ARTIFACT = "cd" * 32
WHEN = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
WHEN_TEXT = "2026-11-15T15:00:00.000000+00:00"
CHARTER = 1000001

# Oracle copied from the revision's literals. Importing those constants would
# let a no-op edit of the revision update the expectation along with the DDL.
EXPECTED_ROSTER_TRIGGERS = {
    "roster_imports_no_update": (
        "CREATE TRIGGER roster_imports_no_update BEFORE UPDATE ON roster_imports "
        "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
    ),
    "roster_imports_no_delete": (
        "CREATE TRIGGER roster_imports_no_delete BEFORE DELETE ON roster_imports "
        "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
    ),
    "roster_imports_no_replace": (
        "CREATE TRIGGER roster_imports_no_replace BEFORE INSERT ON roster_imports "
        "WHEN EXISTS (SELECT 1 FROM roster_imports "
        "WHERE import_id = NEW.import_id OR artifact_id = NEW.artifact_id) "
        "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
    ),
    "roster_import_entries_no_update": (
        "CREATE TRIGGER roster_import_entries_no_update "
        "BEFORE UPDATE ON roster_import_entries "
        "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
    ),
    "roster_import_entries_no_delete": (
        "CREATE TRIGGER roster_import_entries_no_delete "
        "BEFORE DELETE ON roster_import_entries "
        "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
    ),
    "roster_import_entries_no_replace": (
        "CREATE TRIGGER roster_import_entries_no_replace "
        "BEFORE INSERT ON roster_import_entries "
        "WHEN EXISTS (SELECT 1 FROM roster_import_entries "
        "WHERE import_id = NEW.import_id AND charter_number = NEW.charter_number) "
        "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
    ),
}
PREVIOUS_IMPORT_REPLACE = (
    "CREATE TRIGGER roster_imports_no_replace BEFORE INSERT ON roster_imports "
    "WHEN EXISTS (SELECT 1 FROM roster_imports WHERE import_id = NEW.import_id) "
    "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
)


def _database(tmp_path: Path) -> Path:
    return tmp_path / "operational.sqlite"


def _seed(path: Path) -> None:
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(roster_imports),
            {
                "import_id": "import-1",
                "schema_version": "1.0",
                "source_version": "v1",
                "source_as_of_date": "2026-09-01",
                "retrieved_at": WHEN,
                "artifact_id": ARTIFACT,
                "row_count": 1,
                "imported_count": 1,
            },
        )
        conn.execute(
            insert(roster_import_entries),
            {
                "import_id": "import-1",
                "charter_number": CHARTER,
                "schema_version": "1.0",
                "legal_name": "Example North Charter",
                "status": "active",
                "successor_charter_number": None,
                "city": "Boston",
                "closure_or_merger_date": None,
                "source_as_of_date": "2026-09-01",
            },
        )
    engine.dispose()


def _history(raw: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    imports = raw.execute(
        "SELECT import_id, artifact_id, row_count FROM roster_imports ORDER BY import_id"
    ).fetchall()
    entries = raw.execute(
        "SELECT import_id, charter_number, legal_name FROM roster_import_entries "
        "ORDER BY import_id, charter_number"
    ).fetchall()
    return imports, entries


def _replace_import(import_id: str, artifact_id: str, row_count: int) -> str:
    return (
        "INSERT OR REPLACE INTO roster_imports ("
        "import_id, schema_version, source_version, source_as_of_date, "
        "retrieved_at, artifact_id, row_count, imported_count"
        ") VALUES ("
        f"'{import_id}', '1.0', 'v2', '2026-10-01', '{WHEN_TEXT}', "
        f"'{artifact_id}', {row_count}, 1)"
    )


def test_roster_trigger_definitions_match_frozen_sql(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path)
    source = (VERSIONS_DIR / "aud4b_roster_guards.py").read_text(encoding="utf-8")
    assert "schema_triggers" not in source
    assert "cu_rates.db.schema" not in source
    assert "artifact_id = NEW.artifact_id" in source
    for name, statement in EXPECTED_ROSTER_TRIGGERS.items():
        assert name in source
        assert "RAISE(ABORT" in statement

    raw = sqlite3.connect(path)
    try:
        stored = {
            name: sql
            for name, sql in raw.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE 'roster_%'"
            )
        }
    finally:
        raw.close()
    assert stored == EXPECTED_ROSTER_TRIGGERS
    assert dict(schema_triggers(("roster_imports", "roster_import_entries"))) == (
        EXPECTED_ROSTER_TRIGGERS
    )


def test_raw_sqlite_blocks_every_roster_unique_key(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path)
    _seed(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        assert raw.execute("PRAGMA foreign_keys").fetchone() == (0,)
        kept = _history(raw)
        forbidden = (
            "UPDATE roster_imports SET row_count = 0 WHERE import_id = 'import-1'",
            "DELETE FROM roster_imports WHERE import_id = 'import-1'",
            _replace_import("import-1", ARTIFACT, 9),
            _replace_import("import-2", ARTIFACT, 9),
            "UPDATE roster_import_entries SET legal_name = 'Changed' WHERE import_id = 'import-1'",
            "DELETE FROM roster_import_entries WHERE import_id = 'import-1'",
            "INSERT OR REPLACE INTO roster_import_entries ("
            "import_id, charter_number, schema_version, legal_name, status, "
            "successor_charter_number, city, closure_or_merger_date, source_as_of_date"
            ") VALUES ("
            "'import-1', 1000001, '1.0', 'Changed', 'active', NULL, 'Boston', NULL, "
            "'2026-09-01')",
        )
        for statement in forbidden:
            try:
                raw.execute(statement)
            except sqlite3.IntegrityError as exc:
                assert "append-only" in str(exc)
            else:
                raise AssertionError(statement)
            raw.rollback()
            assert _history(raw) == kept
        raw.execute(_replace_import("import-2", OTHER_ARTIFACT, 1))
        raw.execute(
            "INSERT INTO roster_import_entries ("
            "import_id, charter_number, schema_version, legal_name, status, "
            "successor_charter_number, city, closure_or_merger_date, source_as_of_date"
            ") VALUES ("
            "'import-2', 1000002, '1.0', 'Example South Charter', 'active', NULL, "
            "'Worcester', NULL, '2026-10-01')"
        )
        raw.commit()
        imports, entries = _history(raw)
        assert ("import-1", ARTIFACT, 1) in imports
        assert ("import-1", CHARTER, "Example North Charter") in entries
        assert ("import-2", OTHER_ARTIFACT, 1) in imports
    finally:
        raw.close()


def test_engine_guard_seals_completed_roster_imports(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path)
    _seed(path)
    engine = open_operational_database(path)
    forbidden = (
        _replace_import("import-2", ARTIFACT, 9),
        "REPLACE INTO \"roster_imports\" (import_id) VALUES ('import-1')",
        "UPDATE roster_imports SET row_count = 0 WHERE import_id = 'import-1'",
        "DELETE FROM roster_imports WHERE import_id = 'import-1'",
        "UPDATE roster_import_entries SET legal_name = 'Changed' WHERE import_id = 'import-1'",
        "DELETE FROM roster_import_entries WHERE import_id = 'import-1'",
        "INSERT OR REPLACE INTO roster_import_entries ("
        "import_id, charter_number, schema_version, legal_name, status, source_as_of_date"
        ") VALUES ('import-1', 1000001, '1.0', 'Changed', 'active', '2026-09-01')",
    )
    for statement in forbidden:
        with pytest.raises(AppendOnlyError), engine.begin() as conn:
            conn.execute(text(statement))
    with engine.begin() as conn:
        conn.execute(
            insert(roster_imports),
            {
                "import_id": "import-2",
                "schema_version": "1.0",
                "source_version": "v2",
                "source_as_of_date": "2026-10-01",
                "retrieved_at": WHEN,
                "artifact_id": OTHER_ARTIFACT,
                "row_count": 1,
                "imported_count": 1,
            },
        )
        imports = conn.execute(
            select(
                roster_imports.c.import_id,
                roster_imports.c.artifact_id,
                roster_imports.c.row_count,
            ).order_by(roster_imports.c.import_id)
        ).all()
        entries = conn.execute(
            select(roster_import_entries.c.import_id, roster_import_entries.c.legal_name)
        ).all()
    engine.dispose()
    assert imports == [("import-1", ARTIFACT, 1), ("import-2", OTHER_ARTIFACT, 1)]
    assert entries == [("import-1", "Example North Charter")]


def _institution_update(path: Path) -> None:
    """A roster-derived legal-name change. charter_verified stays human-set."""
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(institutions),
            {
                "charter_number": CHARTER,
                "schema_version": "1.0",
                "legal_name": "Human Name",
                "status": "active",
                "successor_charter_number": None,
                "charter_verified": 1,
            },
        )
        conn.execute(
            update(institutions)
            .where(institutions.c.charter_number == CHARTER)
            .values(legal_name="Roster Derived Name")
        )
    engine.dispose()


def test_populated_downgrade_keeps_roster_derived_institution_updates(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path)
    _seed(path)
    _institution_update(path)
    for target in ("t014_trusted_records", "base"):
        with pytest.raises(RuntimeError, match="roster-derived institution updates"):
            downgrade_database(path, target)
        assert current_revision(path) == "aud4b_roster_guards"
    engine = open_operational_database(path)
    with engine.connect() as conn:
        institution = conn.execute(
            select(institutions.c.legal_name, institutions.c.charter_verified)
        ).one()
        imports = conn.execute(select(roster_imports.c.import_id)).scalars().all()
        trigger = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'roster_imports_no_replace'"
            )
        ).scalar_one()
    engine.dispose()
    assert institution == ("Roster Derived Name", 1)
    assert imports == ["import-1"]
    assert trigger == EXPECTED_ROSTER_TRIGGERS["roster_imports_no_replace"]


def test_empty_downgrade_restores_previous_roster_triggers(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path)
    downgrade_database(path, "t014_trusted_records")
    assert current_revision(path) == "t014_trusted_records"
    raw = sqlite3.connect(path)
    try:
        stored = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'roster_imports_no_replace'"
        ).fetchone()
        assert stored is not None
        assert stored[0] == PREVIOUS_IMPORT_REPLACE
        assert "artifact_id" not in stored[0]
    finally:
        raw.close()
    upgrade_database(path)
    assert current_revision(path) == "aud4b_roster_guards"
    raw = sqlite3.connect(path)
    try:
        stored = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'roster_imports_no_replace'"
        ).fetchone()
    finally:
        raw.close()
    assert stored is not None
    assert stored[0] == EXPECTED_ROSTER_TRIGGERS["roster_imports_no_replace"]

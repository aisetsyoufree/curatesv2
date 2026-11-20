"""AUD7-4: the identity-review migration adds two guarded tables and removes only them."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from cu_rates.db import current_revision, downgrade_database, upgrade_database
from cu_rates.db import schema as s
from cu_rates.db.migrate import VERSIONS_DIR, _config
from tests.test_append_only_unique_guards import _rows, _seed_history, _triggers

REVISION = "aud94_identity_reviews"
PREVIOUS = "aud98_canonical_dimensions"
NEW_TABLES = ("institution_identity_reviews", "institution_identity_evidence")
_LATER_TABLES = frozenset(
    {
        "source_review_requests",
        "staged_rate_batches",
        "staged_rate_candidates",
        "staged_rate_findings",
    }
)


def _expected_triggers() -> dict[str, str]:
    """Guards for the tables that exist at this revision (later tables arrive later)."""
    return {
        name: sql
        for name, sql in s.schema_triggers()
        if not any(name.startswith(f"{table}_") for table in _LATER_TABLES)
    }


def _deployed(path: Path) -> None:
    upgrade_database(path, PREVIOUS)
    _seed_history(path)


def _tables(raw: sqlite3.Connection) -> set[str]:
    return {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_head_chain_and_frozen_sql(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.sqlite"))
    assert len(script.get_heads()) == 1
    assert REVISION in {r.revision for r in script.walk_revisions()}
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    source = (VERSIONS_DIR / f"{REVISION}.py").read_text(encoding="utf-8")
    assert "cu_rates.db.schema" not in source  # frozen literal SQL, never the live builder


def test_upgrade_adds_guarded_tables_and_preserves_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    with closing(sqlite3.connect(path)) as raw:
        before_tables = _tables(raw)
        kept = {name: _rows(raw, name) for name in before_tables if name != "alembic_version"}
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        assert _tables(raw) - before_tables == set(NEW_TABLES)
        assert {name: _rows(raw, name) for name in kept} == kept
        assert _triggers(raw) == _expected_triggers()
        for table in NEW_TABLES:
            for action in ("update", "delete", "replace"):
                assert f"{table}_no_{action}" in _triggers(raw)


def test_downgrade_removes_only_the_new_tables_and_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    with closing(sqlite3.connect(path)) as raw:
        before = _triggers(raw)
        before_tables = _tables(raw)
    upgrade_database(path, REVISION)
    downgrade_database(path, PREVIOUS)
    assert current_revision(path) == PREVIOUS
    with closing(sqlite3.connect(path)) as raw:
        assert _tables(raw) == before_tables
        assert _triggers(raw) == before
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION


def test_the_new_tables_enforce_their_checks_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    upgrade_database(path, REVISION)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        columns = "(review_id, schema_version, institution_charter, legal_name, identity_confirmed,"
        columns += " massachusetts_scope_confirmed, reviewer, decided_at, rationale)"
        with pytest.raises(sqlite3.IntegrityError), raw:  # unknown institution
            raw.execute(
                f"INSERT INTO institution_identity_reviews {columns} "
                "VALUES ('r', '1.0', 999999, 'n', 1, 1, 'x', '2026-01-01T00:00:00Z', 'y')"
            )
        charter = raw.execute("SELECT charter_number FROM institutions").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError), raw:  # flag outside 0/1
            raw.execute(
                f"INSERT INTO institution_identity_reviews {columns} "
                f"VALUES ('r', '1.0', {charter}, 'n', 2, 1, 'x', '2026-01-01T00:00:00Z', 'y')"
            )


def _insert_dummy_row(raw: sqlite3.Connection, table: str) -> None:
    """Populate one row of arbitrary content (FKs off) to prove the guard keys on row count."""
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("PRAGMA ignore_check_constraints=ON")
    columns = [c for c in raw.execute(f"PRAGMA table_info({table})") if c[3] or c[5]]
    names = ", ".join(c[1] for c in columns)
    values = ", ".join("1" if "INT" in (c[2] or "").upper() else "'x'" for c in columns)
    raw.execute(f"INSERT INTO {table} ({names}) VALUES ({values})")
    raw.commit()


@pytest.mark.parametrize("table", NEW_TABLES)
def test_downgrade_refuses_while_identity_reviews_exist(tmp_path: Path, table: str) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    upgrade_database(path, REVISION)
    with closing(sqlite3.connect(path)) as raw:
        _insert_dummy_row(raw, table)
    with pytest.raises(RuntimeError, match="refusing downgrade of aud94"):
        downgrade_database(path, PREVIOUS)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize(
    "table",
    ("slice_offer_captures", "slice_snapshot_baselines", "verified_scenario_statements"),
)
def test_t014_downgrade_refuses_while_trusted_history_exists(tmp_path: Path, table: str) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path, "t014_trusted_records")
    with closing(sqlite3.connect(path)) as raw:
        _insert_dummy_row(raw, table)
    with pytest.raises(RuntimeError, match="refusing downgrade of t014"):
        downgrade_database(path, "t020_roster_import")
    assert current_revision(path) == "t014_trusted_records"


@pytest.mark.parametrize(
    "table",
    ("roster_imports", "roster_import_entries"),
)
def test_t020_downgrade_refuses_while_roster_history_exists(tmp_path: Path, table: str) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path, "t020_roster_import")
    with closing(sqlite3.connect(path)) as raw:
        _insert_dummy_row(raw, table)
    with pytest.raises(
        RuntimeError,
        match="refusing downgrade of t020_roster_import while roster import history exists",
    ):
        downgrade_database(path, "t012_initial")
    assert current_revision(path) == "t020_roster_import"
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_t020_downgrade_succeeds_when_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    upgrade_database(path, "t020_roster_import")
    assert current_revision(path) == "t020_roster_import"
    downgrade_database(path, "t012_initial")
    assert current_revision(path) == "t012_initial"

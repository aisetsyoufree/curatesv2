"""Frozen source-request migration, safe downgrade and append-only guards."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from cu_rates.db import (
    current_revision,
    downgrade_database,
    open_operational_database,
    upgrade_database,
)
from cu_rates.db import schema as s
from cu_rates.db.migrate import VERSIONS_DIR, _config
from tests.test_append_only_unique_guards import _rows, _seed_history, _triggers

HEAD = "t046_rate_batch_staging"
REVISION = "t056a_source_review_requests"
PREVIOUS = "aud94_identity_reviews"
TABLE = "source_review_requests"
_LATER_TABLES = frozenset(
    {
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


def seed_request(raw: sqlite3.Connection) -> None:
    raw.execute(
        "INSERT INTO source_review_requests VALUES "
        "('request', 'source-north', 1000001, 'profile-north', "
        "'2026-11-18T00:00:00+00:00', 'uncertain', '{}', ?)",
        ("ab" * 32,),
    )
    raw.commit()


def test_frozen_linear_migration_upgrade_preserves_history(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path, PREVIOUS)
    _seed_history(path)
    with closing(sqlite3.connect(path)) as raw:
        before = {
            name: _rows(raw, name)
            for name in s.APPEND_ONLY_TABLES
            if name != TABLE and name not in _LATER_TABLES
        }
    script = ScriptDirectory.from_config(_config(path))
    assert script.get_heads() == [HEAD]
    head_revision = script.get_revision(HEAD)
    assert head_revision is not None and head_revision.down_revision == REVISION
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    source = (VERSIONS_DIR / f"{REVISION}.py").read_text()
    assert "cu_rates.db.schema" not in source and "schema_triggers" not in source
    upgrade_database(path, REVISION)
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == _expected_triggers()
        assert {name: _rows(raw, name) for name in before} == before
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    engine = open_operational_database(path)
    try:
        with engine.connect() as conn:
            assert TABLE in inspect(conn).get_table_names()
            context = MigrationContext.configure(
                conn,
                opts={
                    "include_object": lambda obj, name, type_, *_: (
                        not (type_ == "table" and name in _LATER_TABLES)
                    ),
                },
            )
            assert compare_metadata(context, s.metadata) == []
    finally:
        engine.dispose()
    downgrade_database(path, PREVIOUS)
    assert current_revision(path) == PREVIOUS
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION


@pytest.mark.parametrize("action", ["update", "delete", "replace"])
def test_append_only_guards_on_raw_connection(tmp_path: Path, action: str) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path)
    _seed_history(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        assert raw.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        seed_request(raw)
        before = _rows(raw, TABLE)
        statement = dict(
            update="UPDATE source_review_requests SET reason='changed'",
            delete="DELETE FROM source_review_requests",
            replace="INSERT OR REPLACE INTO source_review_requests "
            "SELECT * FROM source_review_requests",
        )[action]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(statement)
        raw.commit()
        assert _rows(raw, TABLE) == before
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        downgrade_database(path, PREVIOUS)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        assert _rows(raw, TABLE) == before


def test_foreign_keys_and_engine_guard(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            seed_request(raw)
    engine = open_operational_database(path)
    try:
        from cu_rates.db import AppendOnlyError

        with engine.begin() as conn, pytest.raises(AppendOnlyError):
            conn.execute(s.source_review_requests.update().values(reason="changed"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ["aud98_canonical_dimensions", "aud73_unique_guards"])
@pytest.mark.parametrize("populated", [False, True])
def test_downgrade_past_queue_requires_empty_queue_and_preserves_earlier_history(
    tmp_path: Path, target: str, populated: bool
) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path, target)
    _seed_history(path)
    with closing(sqlite3.connect(path)) as raw:
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        before = {name: _rows(raw, name) for name in tables if name != "alembic_version"}
        triggers = _triggers(raw)
    upgrade_database(path)
    if populated:
        with closing(sqlite3.connect(path)) as raw:
            seed_request(raw)
        with pytest.raises(RuntimeError, match="source review requests exist"):
            downgrade_database(path, target)
        assert current_revision(path) == REVISION
        with closing(sqlite3.connect(path)) as raw:
            assert len(_rows(raw, TABLE)) == 1
            assert _triggers(raw) == _expected_triggers()
            assert {name: _rows(raw, name) for name in before} == before
    else:
        downgrade_database(path, target)
        assert current_revision(path) == target
        with closing(sqlite3.connect(path)) as raw:
            assert {
                row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } == tables
            assert _triggers(raw) == triggers
            assert {name: _rows(raw, name) for name in before} == before

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

REVISION = "t056a_source_review_requests"
PREVIOUS = "aud94_identity_reviews"
TABLE = "source_review_requests"


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
        before = {name: _rows(raw, name) for name in s.APPEND_ONLY_TABLES if name != TABLE}
    script = ScriptDirectory.from_config(_config(path))
    assert script.get_heads() == [REVISION]
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    source = (VERSIONS_DIR / f"{REVISION}.py").read_text()
    assert "cu_rates.db.schema" not in source and "schema_triggers" not in source
    upgrade_database(path)
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == dict(s.schema_triggers())
        assert {name: _rows(raw, name) for name in before} == before
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    engine = open_operational_database(path)
    try:
        with engine.connect() as conn:
            assert TABLE in inspect(conn).get_table_names()
            assert compare_metadata(MigrationContext.configure(conn), s.metadata) == []
    finally:
        engine.dispose()
    downgrade_database(path, PREVIOUS)
    assert current_revision(path) == PREVIOUS
    upgrade_database(path)
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

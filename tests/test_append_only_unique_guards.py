"""AUD7-3: unique conflicts must abort before SQLite can replace history.

These use existing synthetic database fixtures, never publication approval.
Raw connections have foreign keys enabled and recursive triggers disabled.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint

from cu_rates.db import current_revision, downgrade_database, upgrade_database
from cu_rates.db import schema as s
from cu_rates.db.migrate import VERSIONS_DIR, _config
from tests.test_db_schema import _seed
from tests.test_roster_guards import _seed as _seed_roster

REVISION = "aud73_unique_guards"
PREVIOUS = "aud4b_roster_guards"
# Inspection inventory, independent of the guard builder. The fetch/provenance
# alternate keys contain their PK, so a conflict there necessarily hits it too.
ALTERNATE_KEYS = {
    "fetch_runs": (("run_id", "source_id"),),
    "artifact_observations": (("fetch_run_id", "artifact_id"),),
    "provenance": (("institution_charter", "provenance_id"),),
    "verified_scenario_statements": (("offer_id", "dimension", "fetch_run_id"),),
    "roster_imports": (("artifact_id",),),
}
# Frozen predecessor SQL: earlier migrations call the live builder, so merely
# upgrading to the predecessor with today's code cannot emulate a deployed DB.
PREVIOUS_TRIGGERS = {
    "fetch_runs_no_replace": (
        "CREATE TRIGGER fetch_runs_no_replace BEFORE INSERT ON fetch_runs "
        "WHEN EXISTS (SELECT 1 FROM fetch_runs WHERE run_id = NEW.run_id) "
        "BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END"
    ),
    "artifact_observations_no_replace": (
        "CREATE TRIGGER artifact_observations_no_replace BEFORE INSERT ON artifact_observations "
        "WHEN EXISTS (SELECT 1 FROM artifact_observations "
        "WHERE fetch_run_id = NEW.fetch_run_id AND source_url = NEW.source_url) "
        "BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END"
    ),
    "provenance_no_replace": (
        "CREATE TRIGGER provenance_no_replace BEFORE INSERT ON provenance "
        "WHEN EXISTS (SELECT 1 FROM provenance WHERE provenance_id = NEW.provenance_id) "
        "BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END"
    ),
    "verified_scenario_statements_no_replace": (
        "CREATE TRIGGER verified_scenario_statements_no_replace "
        "BEFORE INSERT ON verified_scenario_statements "
        "WHEN EXISTS (SELECT 1 FROM verified_scenario_statements "
        "WHERE statement_id = NEW.statement_id) "
        "BEGIN SELECT RAISE(ABORT, 'verified_scenario_statements is append-only'); END"
    ),
}


def _seed_history(path: Path) -> None:
    _seed(path)
    _seed_roster(path)
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("INSERT INTO artifact_blobs VALUES (?, 12)", ("ef" * 32,))
        raw.execute(
            "INSERT INTO verified_scenario_statements VALUES "
            "('statement-1', '1.0', 'offer-north', 'credit_score', 'run-north', "
            '\'{"kind":"exact","value":780}\', \'prov-note\')'
        )


def _triggers(raw: sqlite3.Connection) -> dict[str, str]:
    return dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))


def _deployed_predecessor(path: Path) -> None:
    upgrade_database(path, PREVIOUS)
    _seed_history(path)
    with closing(sqlite3.connect(path)) as raw, raw:
        for name, statement in PREVIOUS_TRIGGERS.items():
            raw.execute(f"DROP TRIGGER {name}")
            raw.execute(statement)


@contextmanager
def _connection(path: Path, table: str, mode: str) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as source:
        if mode == "ON CONFLICT REPLACE":
            # Exercise SQLite's table-level conflict policy using the deployed
            # schema/data/guards in a scratch DB. No constraints are removed.
            dump = "\n".join(source.iterdump())
            ddl = source.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            replacement, count = re.subn(r"(UNIQUE\s*\([^)]*\))", r"\1 ON CONFLICT REPLACE", ddl)
            assert count == 1
            with closing(sqlite3.connect(":memory:")) as raw:
                raw.executescript(dump.replace(ddl, replacement, 1))
                raw.execute("PRAGMA foreign_keys=ON")
                assert (
                    "ON CONFLICT REPLACE"
                    in raw.execute(
                        "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
                    ).fetchone()[0]
                )
                yield raw
        else:
            source.execute("PRAGMA foreign_keys=ON")
            yield source


def _rows(raw: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return raw.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def _attempt(
    raw: sqlite3.Connection,
    table: str,
    mode: str,
    *,
    conflicting: bool,
    key: str = "alternate",
) -> None:
    columns = [row[1] for row in raw.execute(f"PRAGMA table_info({table})")]
    values = dict(zip(columns, _rows(raw, table)[0], strict=True))
    if table == "artifact_observations":
        values["media_type"] = "application/json"
        if key == "alternate" or not conflicting:
            values["source_url"] = "https://north.example/changed"
        if key == "primary" or not conflicting:
            values["artifact_id"] = "ef" * 32
    elif table == "verified_scenario_statements":
        values["stated_json"] = '{"kind":"exact","value":800}'
        if key == "alternate" or not conflicting:
            values["statement_id"] = "statement-2"
        if key == "primary" or not conflicting:
            values["dimension"] = "lock_days"
    elif table == "roster_imports":
        values["row_count"] = 9
        if key == "alternate" or not conflicting:
            values["import_id"] = "import-2"
        if key == "primary" or not conflicting:
            values["artifact_id"] = "ef" * 32
    elif table == "fetch_runs":
        values["git_sha"] = "ef" * 20
        if conflicting and key == "primary":
            values["source_id"] = "source-south"
        if not conflicting:
            values["run_id"] = "run-new"
    elif table == "provenance":
        values["source_text"] = "changed synthetic text"
        if conflicting and key == "primary":
            values["institution_charter"] = 1000002
        if not conflicting:
            values["provenance_id"] = "prov-new"
    else:
        raise AssertionError(table)
    prefix = "INSERT" if mode == "ON CONFLICT REPLACE" else mode
    raw.execute(
        f"{prefix} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(values.values()),
    )


@pytest.mark.parametrize("table", ALTERNATE_KEYS)
@pytest.mark.parametrize("key", ("primary", "alternate"))
@pytest.mark.parametrize("mode", ("INSERT OR REPLACE", "REPLACE", "ON CONFLICT REPLACE"))
@pytest.mark.parametrize("deployed", (False, True), ids=("fresh", "deployed-upgrade"))
def test_unique_conflicts_abort_and_preserve_original_rows(
    tmp_path: Path, table: str, key: str, mode: str, deployed: bool
) -> None:
    path = tmp_path / "history.sqlite"
    if deployed:
        _deployed_predecessor(path)
        upgrade_database(path)
    else:
        upgrade_database(path)
        _seed_history(path)
    with _connection(path, table, mode) as raw:
        assert raw.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        assert raw.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        kept = {name: _rows(raw, name) for name in s.APPEND_ONLY_TABLES}
        # Do not rollback before checking: ABORT must preserve the original row
        # within the same transaction as well as after a caller commits.
        with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
            _attempt(raw, table, mode, conflicting=True, key=key)
        assert {name: _rows(raw, name) for name in kept} == kept
        raw.commit()
        assert {name: _rows(raw, name) for name in kept} == kept
        _attempt(raw, table, mode, conflicting=False)
        raw.commit()
        assert len(_rows(raw, table)) == len(kept[table]) + 1
        assert all(row in _rows(raw, table) for row in kept[table])
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unique_key_inventory_matches_metadata_and_deployed_indexes(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    upgrade_database(path)
    declared = {
        table.name: tuple(
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        for table in s.metadata.tables.values()
        if table.name in s.APPEND_ONLY_TABLES
        and any(isinstance(c, UniqueConstraint) for c in table.constraints)
    }
    assert declared == ALTERNATE_KEYS == s.APPEND_ONLY_ALTERNATE_KEYS
    with closing(sqlite3.connect(path)) as raw:
        for table in s.APPEND_ONLY_TABLES:
            unique_keys = {
                tuple(row[2] for row in raw.execute(f'PRAGMA index_info("{index[1]}")'))
                for index in raw.execute(f"PRAGMA index_list({table})").fetchall()
                if index[2]
            }
            assert unique_keys == {
                s.APPEND_ONLY_PRIMARY_KEYS[table],
                *ALTERNATE_KEYS.get(table, ()),
            }


def test_deployed_upgrade_downgrade_restores_frozen_sql_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite"
    _deployed_predecessor(path)
    with closing(sqlite3.connect(path)) as raw:
        before = _triggers(raw)
        kept = {
            table: _rows(raw, table)
            for table in s.APPEND_ONLY_TABLES
            if not table.startswith(("institution_identity", "source_review_requests"))
        }
    expected = {
        name: sql
        for name, sql in s.schema_triggers()
        if not name.startswith(
            (
                "institution_identity_reviews",
                "institution_identity_evidence",
                "source_review_requests",
            )
        )
    }

    def no_live_builder(*args: object, **kwargs: object) -> None:
        raise AssertionError("migration must use frozen SQL")

    monkeypatch.setattr(s, "schema_triggers", no_live_builder)
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == expected
        assert {table: _rows(raw, table) for table in kept} == kept
    downgrade_database(path, PREVIOUS)
    assert current_revision(path) == PREVIOUS
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == before
        assert {table: _rows(raw, table) for table in kept} == kept
    upgrade_database(path, REVISION)
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == expected
        assert {table: _rows(raw, table) for table in kept} == kept


def test_migration_order_is_linear_and_sql_is_frozen(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.sqlite"))
    assert len(script.get_heads()) == 1
    assert REVISION in {r.revision for r in script.walk_revisions()}
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == PREVIOUS
    source = (VERSIONS_DIR / f"{REVISION}.py").read_text(encoding="utf-8")
    assert "cu_rates.db.schema" not in source
    assert "schema_triggers" not in source

"""AUD7-8: the aud98 migration widens one CHECK on an append-only table safely.

It rebuilds verified_scenario_statements, so rows, the CHECK set, and every append-only
guard (including the aud73 alternate-key guard) must survive upgrade and downgrade.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from cu_rates.db import current_revision, downgrade_database, upgrade_database
from cu_rates.db import schema as s
from cu_rates.db.migrate import _config
from tests.test_append_only_unique_guards import _rows, _seed_history, _triggers

REVISION = "aud98_canonical_dimensions"
PREVIOUS = "aud73_unique_guards"
NEW_DIMENSIONS = ("property_value", "autopay_discount", "first_lien_balance")
TABLE = "verified_scenario_statements"


_LATER_TABLES = frozenset(
    {
        "institution_identity_reviews",
        "institution_identity_evidence",
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


def _insert(raw: sqlite3.Connection, statement_id: str, dimension: str, run: str) -> None:
    raw.execute(
        f"INSERT INTO {TABLE} VALUES (?, '1.0', 'offer-north', ?, ?, "
        '\'{"kind":"exact","value":500000}\', \'prov-note\')',
        (statement_id, dimension, run),
    )


def test_head_is_this_migration_on_top_of_the_unique_key_guards(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.sqlite"))
    assert len(script.get_heads()) == 1
    assert REVISION in {r.revision for r in script.walk_revisions()}
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS


def test_upgrade_keeps_rows_and_guards_and_accepts_only_the_three_new_dimensions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    with closing(sqlite3.connect(path)) as raw:
        kept = _rows(raw, TABLE)
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        assert _rows(raw, TABLE) == kept
        assert _triggers(raw) == _expected_triggers()
        for index, dimension in enumerate(NEW_DIMENSIONS):
            with raw:
                _insert(raw, f"statement-new-{index}", dimension, "run-north")
        with pytest.raises(sqlite3.IntegrityError), raw:
            _insert(raw, "statement-bogus", "not_a_dimension", "run-north")
        # Append-only guards survived the rebuild: no update, delete or replace.
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            raw.execute(f"UPDATE {TABLE} SET provenance_id = provenance_id")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            raw.execute(f"DELETE FROM {TABLE}")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            # Alternate key (offer_id, dimension, fetch_run_id) with a new statement_id.
            raw.execute(
                f"INSERT OR REPLACE INTO {TABLE} VALUES ('statement-replace', '1.0', "
                "'offer-north', 'credit_score', 'run-north', "
                '\'{"kind":"exact","value":1}\', \'prov-note\')'
            )


def test_downgrade_restores_the_predecessor_guards_and_data(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    with closing(sqlite3.connect(path)) as raw:
        before = _triggers(raw)
        kept = _rows(raw, TABLE)
    upgrade_database(path, REVISION)
    downgrade_database(path, PREVIOUS)
    assert current_revision(path) == PREVIOUS
    with closing(sqlite3.connect(path)) as raw:
        assert _rows(raw, TABLE) == kept
        assert _triggers(raw) == before
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            raw.execute(f"DELETE FROM {TABLE}")
    upgrade_database(path, REVISION)
    assert current_revision(path) == REVISION


def test_downgrade_refuses_to_drop_rows_that_use_a_new_dimension(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    _deployed(path)
    upgrade_database(path, REVISION)
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("PRAGMA foreign_keys=ON")
        _insert(raw, "statement-new", "property_value", "run-north")
    with pytest.raises(IntegrityError):
        downgrade_database(path, PREVIOUS)
    assert current_revision(path) == REVISION  # atomic: nothing half-applied
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == _expected_triggers()

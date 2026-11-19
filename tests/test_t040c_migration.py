"""T040c migration keeps history, enum checks, FKs, and append-only guards."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from cu_rates.db import current_revision, downgrade_database, upgrade_database
from cu_rates.db import schema as s
from cu_rates.db.migrate import _config
from tests.test_append_only_unique_guards import _rows, _seed_history, _triggers
from tests.test_review_migration import seed_request

REVISION = "t040c_unspecified_purpose"
PREVIOUS = "t056a_source_review_requests"


def deployed(path: Path) -> None:
    upgrade_database(path, PREVIOUS)
    _seed_history(path)
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("PRAGMA foreign_keys=ON")
        seed_request(raw)
        raw.execute(
            "INSERT INTO product_findings (finding_id, schema_version, taxonomy_version, "
            "institution_charter, source_id, observed_at, label, scope, family, "
            "rate_availability, review_state) VALUES ('purpose-finding', '1.1', '1.1', "
            "1000001, 'source-north', '2026-11-18T00:00:00+00:00', 'Mortgage', "
            "'in_scope', 'mortgage', 'public_rate', 'proposed')"
        )
        raw.execute("INSERT INTO finding_purposes VALUES ('purpose-finding', 'purchase')")


def test_revision_has_one_head_and_frozen_sql(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(tmp_path / "unused.sqlite"))
    assert script.get_heads() == [REVISION]
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert revision.path is not None
    text = Path(revision.path).read_text()
    assert "cu_rates.db.schema" not in text and "LoanPurpose" not in text


def test_upgrade_downgrade_preserves_rows_constraints_and_every_guard(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite"
    deployed(path)
    with closing(sqlite3.connect(path)) as raw:
        before = _rows(raw, "finding_purposes")
        queue = _rows(raw, "source_review_requests")
        guards = _triggers(raw)
        with pytest.raises(sqlite3.IntegrityError), raw:
            raw.execute("INSERT INTO finding_purposes VALUES ('purpose-finding', 'unspecified')")
    upgrade_database(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        assert _rows(raw, "finding_purposes") == before
        assert _rows(raw, "source_review_requests") == queue
        assert _triggers(raw) == guards == dict(s.schema_triggers())
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError), raw:
            raw.execute("INSERT INTO finding_purposes VALUES ('purpose-finding', 'invented')")
        with pytest.raises(sqlite3.IntegrityError), raw:
            raw.execute("INSERT INTO finding_purposes VALUES ('missing-finding', 'unspecified')")
        for query in (
            "UPDATE finding_purposes SET purpose = purpose",
            "DELETE FROM finding_purposes",
            "INSERT OR REPLACE INTO finding_purposes VALUES ('purpose-finding', 'purchase')",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
                raw.execute(query)
    downgrade_database(path, PREVIOUS)
    with closing(sqlite3.connect(path)) as raw:
        assert _rows(raw, "finding_purposes") == before
        assert _rows(raw, "source_review_requests") == queue
        assert _triggers(raw) == guards
        with pytest.raises(sqlite3.IntegrityError), raw:
            raw.execute("INSERT INTO finding_purposes VALUES ('purpose-finding', 'unspecified')")
    upgrade_database(path)
    assert current_revision(path) == REVISION


@pytest.mark.parametrize("table", ["finding_purposes", "normalized_offers"])
def test_downgrade_guard_is_atomic_for_each_new_representation(tmp_path: Path, table: str) -> None:
    path = tmp_path / "worker.sqlite"
    deployed(path)
    upgrade_database(path)
    with closing(sqlite3.connect(path)) as raw, raw:
        raw.execute("PRAGMA foreign_keys=ON")
        if table == "finding_purposes":
            raw.execute("INSERT INTO finding_purposes VALUES ('purpose-finding', 'unspecified')")
        else:
            # Clone a seeded immutable row under a new ID; no history edits.
            columns = [r[1] for r in raw.execute("PRAGMA table_info(normalized_offers)")]
            row = dict(
                zip(
                    columns,
                    raw.execute("SELECT * FROM normalized_offers LIMIT 1").fetchone(),
                    strict=True,
                )
            )
            row.update(
                offer_id="unstated-offer",
                schema_version="1.1",
                details_json='{"kind":"unspecified_mortgage","rate_type":"fixed","term_months":360}',
                scenario_id=None,
            )
            raw.execute(
                f"INSERT INTO normalized_offers ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(row.values()),
            )
        guards = _triggers(raw)
        before = _rows(raw, table)
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        downgrade_database(path, PREVIOUS)
    assert current_revision(path) == REVISION
    with closing(sqlite3.connect(path)) as raw:
        assert _triggers(raw) == guards and _rows(raw, table) == before
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []

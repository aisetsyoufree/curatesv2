"""AUD9 (#102 h): the first two revisions never regenerate guard SQL from live schema code."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from cu_rates.db import current_revision, upgrade_database
from cu_rates.db import schema as live_schema
from cu_rates.db.migrate import VERSIONS_DIR


@pytest.mark.parametrize("name", ("t012_initial", "t020_roster_import"))
def test_early_revisions_do_not_import_the_live_schema(name: str) -> None:
    source = (VERSIONS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert "cu_rates.db.schema" not in source and "schema_triggers" not in source


def test_early_revisions_upgrade_even_if_live_guard_generation_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an early revision consulted the live guard generator")

    monkeypatch.setattr(live_schema, "schema_triggers", boom)
    path = tmp_path / "early.sqlite"
    upgrade_database(path, "t020_roster_import")
    assert current_revision(path) == "t020_roster_import"
    with closing(sqlite3.connect(path)) as raw:
        names = {r[0] for r in raw.execute("select name from sqlite_master where type='trigger'")}
    assert {"fetch_runs_no_update", "roster_imports_no_replace"} <= names

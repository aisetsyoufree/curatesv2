"""Why the backup and health units may write the data directory.

The operational database is in WAL mode. Even a read-only open (``mode=ro``) must create the
``-shm`` file next to it, so a unit that mounts the data directory read-only fails with
"attempt to write a readonly database". This pins that SQLite behaviour: if it ever changes,
the ReadWritePaths entries in the two units can be tightened again.
"""

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from cu_rates.db import upgrade_database

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user"
)


def _quick_check(path: Path) -> list[tuple[str]]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as raw:
        return raw.execute("PRAGMA quick_check").fetchall()


def test_a_wal_database_cannot_be_opened_read_only_in_a_non_writable_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    database = directory / "operational.sqlite"
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    # Every connection is closed, so SQLite has removed the -wal/-shm side files again.
    assert sorted(p.name for p in directory.iterdir()) == ["operational.sqlite"]
    original = directory.stat().st_mode
    try:
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            _quick_check(database)
        directory.chmod(original)
        assert _quick_check(database) == [("ok",)]  # writable directory: the open succeeds
    finally:
        directory.chmod(original)

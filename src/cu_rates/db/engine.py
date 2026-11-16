"""SQLite operational database connections.

Every connection turns on foreign keys, WAL, and recursive triggers. History
tables have no update, delete, or replace path through this connection.

SQLite implements ``REPLACE`` / ``INSERT OR REPLACE`` as a delete plus an
insert, and it does not fire the delete trigger unless ``recursive_triggers``
is on. The pragma closes that hole on this connection. ``BEFORE INSERT``
triggers in the schema close it for every other connection, including a raw
``sqlite3`` connection that never sets the pragma.
"""

import re
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool

from cu_rates.db.schema import APPEND_ONLY_TABLES, REPLACE_GUARDED_TABLES

_TABLE = r"""["'`]?(?P<table>[A-Za-z_][A-Za-z0-9_]*)["'`]?"""
_MUTATION = re.compile(
    rf"""^\s*(update|delete)\s+(?:from\s+)?{_TABLE}""",
    re.IGNORECASE,
)
_REPLACE = re.compile(
    rf"""^\s*(?:insert\s+or\s+replace|replace)\s+into\s+{_TABLE}""",
    re.IGNORECASE,
)


class AppendOnlyError(Exception):
    """An update or delete targeted an append-only history table."""


def sqlite_url(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("operational database path must be absolute")
    return "sqlite:///" + path.as_posix()


def open_operational_database(path: Path) -> Engine:
    """Open the single-writer operational database.

    ``connect_args={'autocommit': False}`` selects Python 3.12's transactional
    SQLite mode so a rolled-back migration can undo DDL. WAL and foreign keys
    are set on connect, outside that transaction, because SQLite rejects
    ``journal_mode=WAL`` from inside one.
    """
    engine = create_engine(sqlite_url(path), connect_args={"autocommit": False}, poolclass=NullPool)
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "before_cursor_execute", _reject_history_mutation)
    return engine


def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
    del connection_record
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA recursive_triggers=ON")
    finally:
        cursor.close()
        dbapi_connection.autocommit = previous


def _reject_history_mutation(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    del conn, cursor, parameters, context, executemany
    replace = _REPLACE.match(statement)
    if replace is not None and replace.group("table") in REPLACE_GUARDED_TABLES:
        raise AppendOnlyError(f"{replace.group('table')} is append-only")
    match = _MUTATION.match(statement)
    if match is not None and match.group("table") in APPEND_ONLY_TABLES:
        raise AppendOnlyError(f"{match.group('table')} is append-only")

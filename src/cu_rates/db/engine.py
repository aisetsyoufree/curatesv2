"""SQLite operational database connections.

Every connection turns on foreign keys and WAL. History tables have no update
or delete path through this connection.
"""

import re
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool

from cu_rates.db.schema import APPEND_ONLY_TABLES

_MUTATION = re.compile(
    r"""^\s*(update|delete)\s+(?:from\s+)?["'`]?(?P<table>[A-Za-z_][A-Za-z0-9_]*)""",
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
    match = _MUTATION.match(statement)
    if match is not None and match.group("table") in APPEND_ONLY_TABLES:
        raise AppendOnlyError(f"{match.group('table')} is append-only")

"""Alembic upgrade and downgrade for the operational database."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from cu_rates.db.engine import open_operational_database, sqlite_url

_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = _ROOT / "alembic.ini"
VERSIONS_DIR = _ROOT / "alembic" / "versions"


def upgrade_database(
    path: Path,
    revision: str = "head",
    *,
    version_locations: tuple[Path, ...] | None = None,
) -> None:
    command.upgrade(_config(path, version_locations), revision)


def downgrade_database(path: Path, revision: str = "base") -> None:
    command.downgrade(_config(path), revision)


def current_revision(path: Path) -> str | None:
    engine = open_operational_database(path)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()


def script_head(version_locations: tuple[Path, ...] | None = None) -> str:
    """Head revision id from the migration scripts. No database is opened."""
    script = ScriptDirectory.from_config(_config(version_locations=version_locations))
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("migration script has no head")
    return head


def _config(
    path: Path | None = None,
    version_locations: tuple[Path, ...] | None = None,
) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    if path is not None:
        cfg.set_main_option("sqlalchemy.url", sqlite_url(path))
    if version_locations is not None:
        cfg.set_main_option(
            "version_locations",
            os.pathsep.join(item.as_posix() for item in version_locations),
        )
    return cfg

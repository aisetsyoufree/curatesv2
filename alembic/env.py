"""Alembic environment for the operational SQLite database."""

from pathlib import Path

from alembic import context

from cu_rates.db.engine import open_operational_database
from cu_rates.db.schema import metadata

config = context.config
target_metadata = metadata


def _database_path() -> Path:
    url = config.get_main_option("sqlalchemy.url")
    if url is None or not url.startswith("sqlite:///"):
        raise RuntimeError("operational migrations require a sqlite:/// URL")
    return Path(url.removeprefix("sqlite:///"))


def run_migrations_online() -> None:
    engine = open_operational_database(_database_path())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transactional_ddl=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline SQL migrations are not used for the operational database")
run_migrations_online()

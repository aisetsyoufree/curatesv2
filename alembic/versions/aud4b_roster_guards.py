"""Seal roster history and freeze its guard SQL.

Revision ID: aud4b_roster_guards
Revises: t014_trusted_records
Create Date: 2026-11-16

t020 built roster triggers from live Python while upgrading, so editing
that function rewrote deployed guards. This revision drops those triggers
and recreates them from the literal SQL below. It does not import or call
the live builder.

A downgrade refuses while roster_imports or roster_import_entries has any
row. Those rows are the only record of roster-derived institution updates
(legal name, status, successor). Dropping them would leave the institution
changes in place with no history. An empty roster restores the previous
literal trigger text, which guards only the primary key. charter_verified
is not read or written here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aud4b_roster_guards"
down_revision: str | None = "t014_trusted_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMPORTS_UPDATE = (
    "CREATE TRIGGER roster_imports_no_update BEFORE UPDATE ON roster_imports "
    "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
)
_IMPORTS_DELETE = (
    "CREATE TRIGGER roster_imports_no_delete BEFORE DELETE ON roster_imports "
    "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
)
_IMPORTS_REPLACE = (
    "CREATE TRIGGER roster_imports_no_replace BEFORE INSERT ON roster_imports "
    "WHEN EXISTS (SELECT 1 FROM roster_imports "
    "WHERE import_id = NEW.import_id OR artifact_id = NEW.artifact_id) "
    "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
)
_ENTRIES_UPDATE = (
    "CREATE TRIGGER roster_import_entries_no_update "
    "BEFORE UPDATE ON roster_import_entries "
    "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
)
_ENTRIES_DELETE = (
    "CREATE TRIGGER roster_import_entries_no_delete "
    "BEFORE DELETE ON roster_import_entries "
    "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
)
_ENTRIES_REPLACE = (
    "CREATE TRIGGER roster_import_entries_no_replace "
    "BEFORE INSERT ON roster_import_entries "
    "WHEN EXISTS (SELECT 1 FROM roster_import_entries "
    "WHERE import_id = NEW.import_id AND charter_number = NEW.charter_number) "
    "BEGIN SELECT RAISE(ABORT, 'roster_import_entries is append-only'); END"
)
# Pre-AUD4b body. REPLACE on artifact_id was not guarded.
_IMPORTS_REPLACE_PK_ONLY = (
    "CREATE TRIGGER roster_imports_no_replace BEFORE INSERT ON roster_imports "
    "WHEN EXISTS (SELECT 1 FROM roster_imports WHERE import_id = NEW.import_id) "
    "BEGIN SELECT RAISE(ABORT, 'roster_imports is append-only'); END"
)

# Frozen deployed guards. Do not rebuild these from the live builder.
FROZEN_ROSTER_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("roster_imports_no_update", _IMPORTS_UPDATE),
    ("roster_imports_no_delete", _IMPORTS_DELETE),
    ("roster_imports_no_replace", _IMPORTS_REPLACE),
    ("roster_import_entries_no_update", _ENTRIES_UPDATE),
    ("roster_import_entries_no_delete", _ENTRIES_DELETE),
    ("roster_import_entries_no_replace", _ENTRIES_REPLACE),
)
PREVIOUS_ROSTER_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("roster_imports_no_update", _IMPORTS_UPDATE),
    ("roster_imports_no_delete", _IMPORTS_DELETE),
    ("roster_imports_no_replace", _IMPORTS_REPLACE_PK_ONLY),
    ("roster_import_entries_no_update", _ENTRIES_UPDATE),
    ("roster_import_entries_no_delete", _ENTRIES_DELETE),
    ("roster_import_entries_no_replace", _ENTRIES_REPLACE),
)

_POPULATED = (
    "refusing downgrade of aud4b_roster_guards while roster import history "
    "is populated; dropping it would leave roster-derived institution updates behind"
)


def _install(triggers: tuple[tuple[str, str], ...]) -> None:
    for name, _statement in triggers:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for _name, statement in triggers:
        op.execute(statement)


def upgrade() -> None:
    _install(FROZEN_ROSTER_TRIGGERS)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("roster_imports", "roster_import_entries"):
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if int(count) > 0:
            raise RuntimeError(_POPULATED)
    _install(PREVIOUS_ROSTER_TRIGGERS)

"""Add roster import tables and append-only guards.

Revision ID: t020_roster_import
Revises: t012_initial
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cu_rates.db.types
from cu_rates.db.schema import schema_triggers

revision: str = "t020_roster_import"
down_revision: str | None = "t012_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROSTER_TABLES = ("roster_imports", "roster_import_entries")


def _append_only_triggers(present: bool) -> None:
    for name, statement in schema_triggers(ROSTER_TABLES):
        if present:
            op.execute(statement)
        else:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    op.create_table(
        "roster_imports",
        sa.Column("import_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("source_as_of_date", sa.Text(), nullable=False),
        sa.Column("retrieved_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(artifact_id) = 64 AND artifact_id = lower(artifact_id)",
            name="ck_roster_imports_artifact_id",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_roster_imports_row_count"),
        sa.CheckConstraint("imported_count >= 0", name="ck_roster_imports_imported_count"),
        sa.PrimaryKeyConstraint("import_id"),
        sa.UniqueConstraint("artifact_id", name="uq_roster_imports_artifact_id"),
    )
    op.create_table(
        "roster_import_entries",
        sa.Column("import_id", sa.Text(), nullable=False),
        sa.Column("charter_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("successor_charter_number", sa.Integer(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("closure_or_merger_date", sa.Text(), nullable=True),
        sa.Column("source_as_of_date", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "charter_number > 0 AND charter_number < 100000000",
            name="ck_roster_import_entries_charter_number",
        ),
        sa.CheckConstraint(
            "(status = 'merged' AND successor_charter_number IS NOT NULL AND successor_charter_number != charter_number AND successor_charter_number > 0 AND successor_charter_number < 100000000) OR (status != 'merged' AND successor_charter_number IS NULL)",  # noqa: E501
            name="ck_roster_import_entries_successor",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'merged', 'liquidated', 'converted', 'under_review')",
            name="ck_roster_import_entries_status",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["roster_imports.import_id"],
        ),
        sa.PrimaryKeyConstraint("import_id", "charter_number"),
    )
    _append_only_triggers(True)


def downgrade() -> None:
    _append_only_triggers(False)
    op.drop_table("roster_import_entries")
    op.drop_table("roster_imports")

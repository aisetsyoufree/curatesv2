"""Initial operational schema.

Revision ID: t012_initial
Revises:
Create Date: 2026-11-15
"""

from alembic import op

from cu_rates.db.schema import APPEND_ONLY_TABLES, metadata

revision = "t012_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())
    for table_name in sorted(APPEND_ONLY_TABLES):
        for action in ("update", "delete"):
            op.execute(
                f"CREATE TRIGGER {table_name}_no_{action} "
                f"BEFORE {action.upper()} ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
            )


def downgrade() -> None:
    metadata.drop_all(op.get_bind())

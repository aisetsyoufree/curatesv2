"""Extend verified_scenario_statements.dimension CHECK to include the three new
canonical scenario dimensions: property_value, autopay_discount, first_lien_balance.

These dimensions were added to ScenarioDimension (issue #98 / AUD7-8) so that
publication can require source-stated evidence for every canonical assumption.
The prior CHECK constraint (from t014_trusted_records) covered only the original
twelve values; this migration replaces it with the full fifteen-value set.

SQLite cannot add or drop CHECK constraints in-place; a table rebuild via
batch_alter_table is required.  The rebuild is performed offline (no live
collection is approved or enabled) and the triggers are restored after.

Revision ID: aud98_canonical_dimensions
Revises: aud73_unique_guards
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "aud98_canonical_dimensions"
down_revision: str | None = "aud73_unique_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "verified_scenario_statements"
_PK = "statement_id"
# Triggers that must be temporarily removed while the table is rebuilt.
_TRIGGER_ACTIONS = ("update", "delete", "replace")

_OLD_DIMENSIONS = (
    "occupancy",
    "property_type",
    "rate_type",
    "term_months",
    "ltv",
    "cltv",
    "loan_amount",
    "credit_score",
    "lock_days",
    "points",
    "cash_out",
    "relationship_discount",
)
_NEW_DIMENSIONS = (*_OLD_DIMENSIONS, "property_value", "autopay_discount", "first_lien_balance")

_CK_OLD = "ck_verified_scenario_statements_dimension"
_CK_NEW = "ck_verified_scenario_statements_dimension"


def _dimension_check(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"dimension IN ({quoted})"


def _drop_triggers() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_update")
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_replace")


def _create_triggers() -> None:
    message = f"{_TABLE} is append-only"
    for action in ("update", "delete"):
        op.execute(
            f"CREATE TRIGGER {_TABLE}_no_{action} BEFORE {action.upper()} ON {_TABLE} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
        )
    # Covers both the primary key (statement_id) and the alternate key
    # (offer_id, dimension, fetch_run_id), matching schema_triggers() output.
    match = (
        "statement_id = NEW.statement_id "
        "OR offer_id = NEW.offer_id AND dimension = NEW.dimension "
        "AND fetch_run_id = NEW.fetch_run_id"
    )
    op.execute(
        f"CREATE TRIGGER {_TABLE}_no_replace BEFORE INSERT ON {_TABLE} "
        f"WHEN EXISTS (SELECT 1 FROM {_TABLE} WHERE {match}) "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
    )


def upgrade() -> None:
    _drop_triggers()
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CK_OLD, type_="check")
        batch.create_check_constraint(_CK_NEW, _dimension_check(_NEW_DIMENSIONS))
    _create_triggers()


def downgrade() -> None:
    _drop_triggers()
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CK_OLD, type_="check")
        batch.create_check_constraint(_CK_OLD, _dimension_check(_OLD_DIMENSIONS))
    _create_triggers()

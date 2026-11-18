"""Persist source-approval digests and accept source withdrawals.

Revision ID: t014_trusted_records
Revises: t020_roster_import
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import cu_rates.db.types

revision: str = "t014_trusted_records"
down_revision: str | None = "t020_roster_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_TABLES = ("verified_scenario_statements", "verified_scenario_conditions")
_SLICE_TABLES = ("slice_offer_captures", "slice_snapshot_baselines", "slice_normalization_failures")
_TRIGGER_KEYS = {
    "source_approvals": ("approval_id",),
    "source_fingerprints": ("fingerprint_id",),
    "withdrawal_entries": ("withdrawal_id",),
    "verified_scenario_statements": ("statement_id",),
    "verified_scenario_conditions": ("offer_id", "scenario_id", "condition_id", "fetch_run_id"),
    "slice_offer_captures": ("offer_id", "fetch_run_id"),
    "slice_snapshot_baselines": ("snapshot_id", "offer_id"),
    "slice_normalization_failures": ("extraction_run_id", "row_id"),
}
_WITHDRAWAL_SCOPE = "scope IN ('offer', 'product', 'institution', 'source', 'snapshot')"
_FROZEN_SCOPE = "scope IN ('offer', 'product', 'institution', 'snapshot')"


def _triggers(tables: tuple[str, ...], *, present: bool) -> None:
    for table in tables:
        message = f"{table} is append-only"
        for action in ("update", "delete"):
            name = f"{table}_no_{action}"
            if present:
                op.execute(
                    f"CREATE TRIGGER {name} BEFORE {action.upper()} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
                )
            else:
                op.execute(f"DROP TRIGGER IF EXISTS {name}")
        name = f"{table}_no_replace"
        if present:
            match = " AND ".join(f"{column} = NEW.{column}" for column in _TRIGGER_KEYS[table])
            op.execute(
                f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {match}) "
                f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
            )
        else:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    _triggers(("source_approvals",), present=False)
    with op.batch_alter_table("source_approvals") as batch:
        batch.add_column(sa.Column("approved_profile_sha256", sa.Text(), nullable=True))
        batch.add_column(sa.Column("approved_layout_sha256", sa.Text(), nullable=True))
        batch.add_column(sa.Column("adapter_name", sa.Text(), nullable=True))
        batch.add_column(sa.Column("adapter_version", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("approved_origins_json", cu_rates.db.types.JsonDocument(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_source_approvals_adapter",
            "(adapter_name IS NULL AND adapter_version IS NULL)"
            " OR (adapter_name IS NOT NULL AND adapter_version IS NOT NULL)",
        )
    with op.batch_alter_table("source_fingerprints") as batch:
        batch.add_column(sa.Column("adapter_name", sa.Text(), nullable=True))
        batch.add_column(sa.Column("adapter_version", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_source_fingerprints_adapter",
            "(adapter_name IS NULL AND adapter_version IS NULL)"
            " OR (adapter_name IS NOT NULL AND adapter_version IS NOT NULL)",
        )
    _triggers(("source_approvals", "source_fingerprints"), present=True)
    op.add_column(
        "review_decisions",
        sa.Column("reviewed_evidence_sha256", sa.Text(), nullable=True),
    )
    _triggers(("withdrawal_entries",), present=False)
    with op.batch_alter_table("withdrawal_entries") as batch:
        batch.drop_constraint("ck_withdrawal_entries_scope", type_="check")
        batch.create_check_constraint("ck_withdrawal_entries_scope", _WITHDRAWAL_SCOPE)
    _triggers(("withdrawal_entries",), present=True)

    op.create_table(
        "verified_scenario_statements",
        sa.Column("statement_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("fetch_run_id", sa.Text(), nullable=False),
        sa.Column("stated_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('occupancy', 'property_type', 'rate_type', 'term_months', "
            "'ltv', 'cltv', 'loan_amount', 'credit_score', 'lock_days', 'points', "
            "'cash_out', 'relationship_discount')",
            name="ck_verified_scenario_statements_dimension",
        ),
        sa.ForeignKeyConstraint(["fetch_run_id"], ["fetch_runs.run_id"]),
        sa.ForeignKeyConstraint(["offer_id"], ["normalized_offers.offer_id"]),
        sa.ForeignKeyConstraint(["provenance_id"], ["provenance.provenance_id"]),
        sa.PrimaryKeyConstraint("statement_id"),
        sa.UniqueConstraint(
            "offer_id", "dimension", "fetch_run_id", name="uq_verified_statements_dimension"
        ),
    )
    op.create_table(
        "verified_scenario_conditions",
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=False),
        sa.Column("condition_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("fetch_run_id", sa.Text(), nullable=False),
        sa.Column("condition_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.CheckConstraint(
            "scenario_id IN ("
            "'purchase_30y_fixed', 'rate_term_refi_30y_fixed', "
            "'fixed_home_equity_10y', 'heloc'"
            ")",
            name="ck_verified_scenario_conditions_scenario_id",
        ),
        sa.ForeignKeyConstraint(["fetch_run_id"], ["fetch_runs.run_id"]),
        sa.ForeignKeyConstraint(["offer_id"], ["normalized_offers.offer_id"]),
        sa.PrimaryKeyConstraint("offer_id", "scenario_id", "condition_id", "fetch_run_id"),
    )
    _triggers(_NEW_TABLES, present=True)
    op.create_table(
        "slice_offer_captures",
        sa.Column(
            "offer_id", sa.Text(), sa.ForeignKey("normalized_offers.offer_id"), primary_key=True
        ),
        sa.Column("fetch_run_id", sa.Text(), sa.ForeignKey("fetch_runs.run_id"), primary_key=True),
        sa.Column("offer_json", cu_rates.db.types.JsonDocument(), nullable=False),
    )
    op.create_table(
        "slice_snapshot_baselines",
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("offer_id", sa.Text(), primary_key=True),
        sa.Column("baseline_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("last_value_change_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "offer_id"],
            ["snapshot_entries.snapshot_id", "snapshot_entries.offer_id"],
        ),
    )
    op.create_table(
        "slice_normalization_failures",
        sa.Column(
            "extraction_run_id",
            sa.Text(),
            sa.ForeignKey("extraction_runs.run_id"),
            primary_key=True,
        ),
        sa.Column("row_id", sa.Text(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
    )
    _triggers(_SLICE_TABLES, present=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        *_SLICE_TABLES,
        *_NEW_TABLES,
        "verified_scenario_conditions",
        "verified_scenario_statements",
    ):
        if int(bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()) > 0:
            raise RuntimeError(
                f"refusing downgrade of t014_trusted_records while {table} holds rows: "
                "append-only captures, snapshots and baselines must not be dropped"
            )
    _triggers(_SLICE_TABLES, present=False)
    for table in reversed(_SLICE_TABLES):
        op.drop_table(table)
    _triggers(_NEW_TABLES, present=False)
    op.drop_table("verified_scenario_conditions")
    op.drop_table("verified_scenario_statements")
    _triggers(("withdrawal_entries",), present=False)
    with op.batch_alter_table("withdrawal_entries") as batch:
        batch.drop_constraint("ck_withdrawal_entries_scope", type_="check")
        batch.create_check_constraint("ck_withdrawal_entries_scope", _FROZEN_SCOPE)
    _triggers(("withdrawal_entries",), present=True)
    op.drop_column("review_decisions", "reviewed_evidence_sha256")
    _triggers(("source_approvals", "source_fingerprints"), present=False)
    with op.batch_alter_table("source_fingerprints") as batch:
        batch.drop_constraint("ck_source_fingerprints_adapter", type_="check")
        batch.drop_column("adapter_version")
        batch.drop_column("adapter_name")
    with op.batch_alter_table("source_approvals") as batch:
        batch.drop_constraint("ck_source_approvals_adapter", type_="check")
        batch.drop_column("approved_origins_json")
        batch.drop_column("adapter_version")
        batch.drop_column("adapter_name")
        batch.drop_column("approved_layout_sha256")
        batch.drop_column("approved_profile_sha256")
    _triggers(("source_approvals",), present=True)

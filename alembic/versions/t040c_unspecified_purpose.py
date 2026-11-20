"""Add explicit unstated mortgage purpose; refuse lossy downgrades.

Frozen literal SQL, independent of live enums/schema. Only finding_purposes has
an enum CHECK; normalized offer details are JSON. Preserve all history/guards.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "t040c_unspecified_purpose"
down_revision: str | None = "t056a_source_review_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'purchase', 'rate_term_refinance', 'cash_out_refinance'"
_NEW = _OLD + ", 'unspecified'"


def _rebuild(values: str) -> None:
    for action in ("update", "delete", "replace"):
        op.execute(f"DROP TRIGGER finding_purposes_no_{action}")
    op.execute(
        "CREATE TABLE finding_purposes_t040c ("
        "finding_id TEXT NOT NULL, purpose TEXT NOT NULL, "
        "PRIMARY KEY (finding_id, purpose), "
        f"CONSTRAINT ck_finding_purposes_purpose CHECK (purpose IN ({values})), "
        "FOREIGN KEY(finding_id) REFERENCES product_findings (finding_id))"
    )
    op.execute(
        "INSERT INTO finding_purposes_t040c SELECT finding_id, purpose FROM finding_purposes"
    )
    op.execute("DROP TABLE finding_purposes")
    op.execute("ALTER TABLE finding_purposes_t040c RENAME TO finding_purposes")
    for action in ("update", "delete"):
        op.execute(
            f"CREATE TRIGGER finding_purposes_no_{action} BEFORE {action.upper()} "
            "ON finding_purposes BEGIN SELECT RAISE(ABORT, 'finding_purposes is append-only'); END"
        )
    op.execute(
        "CREATE TRIGGER finding_purposes_no_replace BEFORE INSERT ON finding_purposes "
        "WHEN EXISTS (SELECT 1 FROM finding_purposes "
        "WHERE finding_id = NEW.finding_id AND purpose = NEW.purpose) "
        "BEGIN SELECT RAISE(ABORT, 'finding_purposes is append-only'); END"
    )


def upgrade() -> None:
    _rebuild(_NEW)


def downgrade() -> None:
    # Check *before* any DDL; never discard history or relabel an unstated purpose.
    if (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM finding_purposes WHERE purpose = 'unspecified' "
            "UNION ALL SELECT 1 FROM normalized_offers "
            "WHERE json_extract(details_json, '$.kind') = 'unspecified_mortgage' "
            "OR json_extract(details_json, '$.purpose') = 'unspecified' LIMIT 1"
        )
        .first()
    ):
        raise RuntimeError("cannot downgrade: rows use unspecified mortgage purpose")
    _rebuild(_OLD)

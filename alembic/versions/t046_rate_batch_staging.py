"""Append-only private staging tables for offline rate batch intake (M4 / T046).

Revision ID: t046_rate_batch_staging
Revises: t056a_source_review_requests
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "t046_rate_batch_staging"
down_revision: str | None = "t056a_source_review_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    """
    CREATE TABLE staged_rate_batches (
        batch_id TEXT NOT NULL PRIMARY KEY,
        schema_version TEXT NOT NULL,
        batch_digest TEXT NOT NULL,
        operator TEXT NOT NULL,
        model_name TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        recipe_version TEXT NOT NULL,
        prompt_version TEXT,
        submitted_at TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        raw_bytes_hash TEXT NOT NULL,
        item_count INTEGER NOT NULL,
        staged_count INTEGER NOT NULL,
        held_count INTEGER NOT NULL,
        rejected_count INTEGER NOT NULL,
        independent_review_json TEXT,
        CONSTRAINT ck_staged_rate_batches_item_count CHECK (item_count >= 0),
        CONSTRAINT ck_staged_rate_batches_staged_count CHECK (staged_count >= 0),
        CONSTRAINT ck_staged_rate_batches_held_count CHECK (held_count >= 0),
        CONSTRAINT ck_staged_rate_batches_rejected_count CHECK (rejected_count >= 0)
    )
    """,
    """
    CREATE TABLE staged_rate_candidates (
        candidate_id TEXT NOT NULL PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES staged_rate_batches (batch_id),
        row_id TEXT NOT NULL,
        institution_charter INTEGER NOT NULL,
        product_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        fetch_run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        hold_reason TEXT,
        reject_reason TEXT,
        detail TEXT,
        product_family TEXT NOT NULL,
        loan_purpose TEXT,
        term_months INTEGER,
        note_rate TEXT,
        apr TEXT,
        points TEXT,
        raw_text TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        staged_at TEXT NOT NULL,
        CONSTRAINT ck_staged_rate_candidates_disposition
            CHECK (disposition IN ('staged', 'held', 'rejected')),
        CONSTRAINT ck_staged_rate_candidates_note_rate_finite
            CHECK (note_rate IS NULL OR lower(note_rate) NOT IN (
                'nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity',
                '-infinity', '+infinity', 'snan', '-snan', '+snan'
            )),
        CONSTRAINT ck_staged_rate_candidates_apr_finite
            CHECK (apr IS NULL OR lower(apr) NOT IN (
                'nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity',
                '-infinity', '+infinity', 'snan', '-snan', '+snan'
            )),
        CONSTRAINT ck_staged_rate_candidates_points_finite
            CHECK (points IS NULL OR lower(points) NOT IN (
                'nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity',
                '-infinity', '+infinity', 'snan', '-snan', '+snan'
            ))
    )
    """,
    """
    CREATE TABLE staged_rate_findings (
        finding_id TEXT NOT NULL PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES staged_rate_batches (batch_id),
        institution_charter INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        fetch_run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        disposition TEXT NOT NULL,
        hold_reason TEXT,
        reject_reason TEXT,
        detail TEXT,
        label TEXT NOT NULL,
        scope TEXT NOT NULL,
        family TEXT,
        rate_availability TEXT,
        raw_text TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        staged_at TEXT NOT NULL,
        CONSTRAINT ck_staged_rate_findings_disposition
            CHECK (disposition IN ('staged', 'held', 'rejected'))
    )
    """,
)

_TRIGGERS = (
    (
        "staged_rate_batches_no_update",
        "CREATE TRIGGER staged_rate_batches_no_update BEFORE UPDATE ON staged_rate_batches "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_batches is append-only'); END",
    ),
    (
        "staged_rate_batches_no_delete",
        "CREATE TRIGGER staged_rate_batches_no_delete BEFORE DELETE ON staged_rate_batches "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_batches is append-only'); END",
    ),
    (
        "staged_rate_batches_no_replace",
        "CREATE TRIGGER staged_rate_batches_no_replace BEFORE INSERT ON staged_rate_batches "
        "WHEN EXISTS (SELECT 1 FROM staged_rate_batches WHERE batch_id = NEW.batch_id) "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_batches is append-only'); END",
    ),
    (
        "staged_rate_candidates_no_update",
        "CREATE TRIGGER staged_rate_candidates_no_update BEFORE UPDATE ON staged_rate_candidates "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_candidates is append-only'); END",
    ),
    (
        "staged_rate_candidates_no_delete",
        "CREATE TRIGGER staged_rate_candidates_no_delete BEFORE DELETE ON staged_rate_candidates "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_candidates is append-only'); END",
    ),
    (
        "staged_rate_candidates_no_replace",
        "CREATE TRIGGER staged_rate_candidates_no_replace BEFORE INSERT ON staged_rate_candidates "
        "WHEN EXISTS (SELECT 1 FROM staged_rate_candidates WHERE candidate_id = NEW.candidate_id) "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_candidates is append-only'); END",
    ),
    (
        "staged_rate_findings_no_update",
        "CREATE TRIGGER staged_rate_findings_no_update BEFORE UPDATE ON staged_rate_findings "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_findings is append-only'); END",
    ),
    (
        "staged_rate_findings_no_delete",
        "CREATE TRIGGER staged_rate_findings_no_delete BEFORE DELETE ON staged_rate_findings "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_findings is append-only'); END",
    ),
    (
        "staged_rate_findings_no_replace",
        "CREATE TRIGGER staged_rate_findings_no_replace BEFORE INSERT ON staged_rate_findings "
        "WHEN EXISTS (SELECT 1 FROM staged_rate_findings WHERE finding_id = NEW.finding_id) "
        "BEGIN SELECT RAISE(ABORT, 'staged_rate_findings is append-only'); END",
    ),
)


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for _, statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("staged_rate_candidates", "staged_rate_findings", "staged_rate_batches"):
        count = int(bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one())
        if count > 0:
            raise RuntimeError(
                f"refusing downgrade of t046_rate_batch_staging while append-only {table} "
                f"records exist"
            )
    for name, _ in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS staged_rate_candidates")
    op.execute("DROP TABLE IF EXISTS staged_rate_findings")
    op.execute("DROP TABLE IF EXISTS staged_rate_batches")

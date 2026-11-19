"""Append-only owner-routing queue, separate from frozen SourceApproval decisions."""

from collections.abc import Sequence

from alembic import op

revision: str = "t056a_source_review_requests"
down_revision: str | None = "aud94_identity_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = """
CREATE TABLE source_review_requests (
    request_id TEXT NOT NULL PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES rate_sources (source_id),
    institution_charter INTEGER NOT NULL REFERENCES institutions (charter_number),
    profile_id TEXT NOT NULL REFERENCES fetch_profiles (profile_id),
    requested_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
)
"""
_TRIGGERS = (
    "CREATE TRIGGER source_review_requests_no_update BEFORE UPDATE ON source_review_requests "
    "BEGIN SELECT RAISE(ABORT, 'source_review_requests is append-only'); END",
    "CREATE TRIGGER source_review_requests_no_delete BEFORE DELETE ON source_review_requests "
    "BEGIN SELECT RAISE(ABORT, 'source_review_requests is append-only'); END",
    "CREATE TRIGGER source_review_requests_no_replace BEFORE INSERT ON source_review_requests "
    "WHEN EXISTS (SELECT 1 FROM source_review_requests WHERE request_id = NEW.request_id) "
    "BEGIN SELECT RAISE(ABORT, 'source_review_requests is append-only'); END",
)


def upgrade() -> None:
    op.execute(_TABLE)
    for statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    count = (
        op.get_bind().exec_driver_sql("SELECT COUNT(*) FROM source_review_requests").scalar_one()
    )
    if count:
        raise RuntimeError("refusing downgrade while append-only source review requests exist")
    for action in ("replace", "delete", "update"):
        op.execute(f"DROP TRIGGER source_review_requests_no_{action}")
    op.execute("DROP TABLE source_review_requests")

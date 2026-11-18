"""Add append-only institution identity and Massachusetts-scope reviews.

Revision ID: aud94_identity_reviews
Revises: aud98_canonical_dimensions
Create Date: 2026-11-18

``institutions.charter_verified`` and a relationship's ``approved_by`` are bare flags. These
tables hold the human decision behind them and the located evidence it cites; publication
requires a current approving review. Frozen literal SQL: this migration never imports the
live schema builder, so it replays identically on a deployed database.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "aud94_identity_reviews"
down_revision: str | None = "aud98_canonical_dimensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    """
    CREATE TABLE institution_identity_reviews (
        review_id TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        institution_charter INTEGER NOT NULL,
        legal_name TEXT NOT NULL,
        identity_confirmed INTEGER NOT NULL,
        massachusetts_scope_confirmed INTEGER NOT NULL,
        reviewer TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        rationale TEXT NOT NULL,
        PRIMARY KEY (review_id),
        CONSTRAINT ck_institution_identity_reviews_identity
            CHECK (identity_confirmed IN (0, 1)),
        CONSTRAINT ck_institution_identity_reviews_scope
            CHECK (massachusetts_scope_confirmed IN (0, 1)),
        FOREIGN KEY(institution_charter) REFERENCES institutions (charter_number)
    )
    """,
    """
    CREATE TABLE institution_identity_evidence (
        review_id TEXT NOT NULL,
        provenance_id TEXT NOT NULL,
        institution_charter INTEGER NOT NULL,
        ordinal INTEGER NOT NULL,
        PRIMARY KEY (review_id, provenance_id),
        CONSTRAINT ck_institution_identity_evidence_ordinal CHECK (ordinal >= 0),
        CONSTRAINT fk_institution_identity_evidence_provenance_institution
            FOREIGN KEY(institution_charter, provenance_id)
            REFERENCES provenance (institution_charter, provenance_id),
        FOREIGN KEY(review_id) REFERENCES institution_identity_reviews (review_id)
    )
    """,
)

_TRIGGERS = (
    (
        "institution_identity_reviews_no_update",
        "CREATE TRIGGER institution_identity_reviews_no_update BEFORE UPDATE ON "
        "institution_identity_reviews BEGIN SELECT RAISE(ABORT, "
        "'institution_identity_reviews is append-only'); END",
    ),
    (
        "institution_identity_reviews_no_delete",
        "CREATE TRIGGER institution_identity_reviews_no_delete BEFORE DELETE ON "
        "institution_identity_reviews BEGIN SELECT RAISE(ABORT, "
        "'institution_identity_reviews is append-only'); END",
    ),
    (
        "institution_identity_reviews_no_replace",
        "CREATE TRIGGER institution_identity_reviews_no_replace BEFORE INSERT ON "
        "institution_identity_reviews WHEN EXISTS (SELECT 1 FROM "
        "institution_identity_reviews WHERE review_id = NEW.review_id) BEGIN SELECT "
        "RAISE(ABORT, 'institution_identity_reviews is append-only'); END",
    ),
    (
        "institution_identity_evidence_no_update",
        "CREATE TRIGGER institution_identity_evidence_no_update BEFORE UPDATE ON "
        "institution_identity_evidence BEGIN SELECT RAISE(ABORT, "
        "'institution_identity_evidence is append-only'); END",
    ),
    (
        "institution_identity_evidence_no_delete",
        "CREATE TRIGGER institution_identity_evidence_no_delete BEFORE DELETE ON "
        "institution_identity_evidence BEGIN SELECT RAISE(ABORT, "
        "'institution_identity_evidence is append-only'); END",
    ),
    (
        "institution_identity_evidence_no_replace",
        "CREATE TRIGGER institution_identity_evidence_no_replace BEFORE INSERT ON "
        "institution_identity_evidence WHEN EXISTS (SELECT 1 FROM "
        "institution_identity_evidence WHERE review_id = NEW.review_id AND "
        "provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, "
        "'institution_identity_evidence is append-only'); END",
    ),
)


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for _, statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("institution_identity_reviews", "institution_identity_evidence"):
        if int(bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()) > 0:
            raise RuntimeError(
                "refusing downgrade of aud94_identity_reviews while human identity reviews "
                "exist: append-only review history must not be dropped"
            )
    for name, _ in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS institution_identity_evidence")
    op.execute("DROP TABLE IF EXISTS institution_identity_reviews")

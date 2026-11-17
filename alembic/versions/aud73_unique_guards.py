"""Guard every append-only unique conflict before SQLite can replace a row.

Revision ID: aud73_unique_guards
Revises: aud4b_roster_guards
Create Date: 2026-11-17

Inspection found alternate UNIQUE constraints on fetch_runs (run_id, source_id),
artifact_observations (fetch_run_id, artifact_id), provenance
(institution_charter, provenance_id), verified_scenario_statements
(offer_id, dimension, fetch_run_id), and roster_imports (artifact_id).
The roster guard already covers its alternate key in aud4b. The fetch_runs and
provenance keys contain their primary keys; name them explicitly here too.
No other append-only table has an alternate UNIQUE constraint or unique index.

BEFORE INSERT with RAISE(ABORT) protects even raw connections with recursive
triggers disabled, including table-level ON CONFLICT REPLACE. UPDATE/DELETE
guards are unchanged. Both directions install literal SQL without rebuilding
tables or modifying data. Downgrade restores the predecessor's weaker guards.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "aud73_unique_guards"
down_revision: str | None = "aud4b_roster_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen SQL for deployed databases; do not import the live schema builder.
_GUARDS: tuple[tuple[str, str], ...] = (
    (
        "fetch_runs_no_replace",
        "CREATE TRIGGER fetch_runs_no_replace BEFORE INSERT ON fetch_runs "
        "WHEN EXISTS (SELECT 1 FROM fetch_runs "
        "WHERE run_id = NEW.run_id OR run_id = NEW.run_id AND source_id = NEW.source_id) "
        "BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END",
    ),
    (
        "artifact_observations_no_replace",
        "CREATE TRIGGER artifact_observations_no_replace BEFORE INSERT ON artifact_observations "
        "WHEN EXISTS (SELECT 1 FROM artifact_observations "
        "WHERE fetch_run_id = NEW.fetch_run_id AND source_url = NEW.source_url "
        "OR fetch_run_id = NEW.fetch_run_id AND artifact_id = NEW.artifact_id) "
        "BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END",
    ),
    (
        "provenance_no_replace",
        "CREATE TRIGGER provenance_no_replace BEFORE INSERT ON provenance "
        "WHEN EXISTS (SELECT 1 FROM provenance WHERE provenance_id = NEW.provenance_id "
        "OR institution_charter = NEW.institution_charter AND provenance_id = NEW.provenance_id) "
        "BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END",
    ),
    (
        "verified_scenario_statements_no_replace",
        "CREATE TRIGGER verified_scenario_statements_no_replace "
        "BEFORE INSERT ON verified_scenario_statements "
        "WHEN EXISTS (SELECT 1 FROM verified_scenario_statements "
        "WHERE statement_id = NEW.statement_id OR offer_id = NEW.offer_id "
        "AND dimension = NEW.dimension AND fetch_run_id = NEW.fetch_run_id) "
        "BEGIN SELECT RAISE(ABORT, 'verified_scenario_statements is append-only'); END",
    ),
)
_PREVIOUS_GUARDS: tuple[tuple[str, str], ...] = (
    (
        "fetch_runs_no_replace",
        "CREATE TRIGGER fetch_runs_no_replace BEFORE INSERT ON fetch_runs "
        "WHEN EXISTS (SELECT 1 FROM fetch_runs WHERE run_id = NEW.run_id) "
        "BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END",
    ),
    (
        "artifact_observations_no_replace",
        "CREATE TRIGGER artifact_observations_no_replace BEFORE INSERT ON artifact_observations "
        "WHEN EXISTS (SELECT 1 FROM artifact_observations "
        "WHERE fetch_run_id = NEW.fetch_run_id AND source_url = NEW.source_url) "
        "BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END",
    ),
    (
        "provenance_no_replace",
        "CREATE TRIGGER provenance_no_replace BEFORE INSERT ON provenance "
        "WHEN EXISTS (SELECT 1 FROM provenance WHERE provenance_id = NEW.provenance_id) "
        "BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END",
    ),
    (
        "verified_scenario_statements_no_replace",
        "CREATE TRIGGER verified_scenario_statements_no_replace "
        "BEFORE INSERT ON verified_scenario_statements "
        "WHEN EXISTS (SELECT 1 FROM verified_scenario_statements "
        "WHERE statement_id = NEW.statement_id) "
        "BEGIN SELECT RAISE(ABORT, 'verified_scenario_statements is append-only'); END",
    ),
)


def _install(guards: tuple[tuple[str, str], ...]) -> None:
    for name, statement in guards:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
        op.execute(statement)


def upgrade() -> None:
    _install(_GUARDS)


def downgrade() -> None:
    _install(_PREVIOUS_GUARDS)

"""Initial operational schema.

Revision ID: t012_initial
Revises:
Create Date: 2026-11-15
"""

# ruff: noqa: E501  (frozen guard SQL is stored verbatim)

import sqlalchemy as sa
from alembic import op

import cu_rates.db.types

revision = "t012_initial"
down_revision = None
branch_labels = None
depends_on = None

# Frozen at T012. Do not rebuild this revision from live metadata.
# Trigger text is the schema guard for this revision (append-only keys,
# replace guards, and the offer lifecycle path). CREATE TABLE stays hand-written.


# Guard SQL frozen as it stood when AUD4b sealed roster history (AUD9 #102 h): this revision
# must never regenerate guards from the live schema module, which keeps gaining keys.
_FROZEN_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "artifact_blobs_no_delete",
        "CREATE TRIGGER artifact_blobs_no_delete BEFORE DELETE ON artifact_blobs BEGIN SELECT RAISE(ABORT, 'artifact_blobs is append-only'); END",
    ),
    (
        "artifact_blobs_no_replace",
        "CREATE TRIGGER artifact_blobs_no_replace BEFORE INSERT ON artifact_blobs WHEN EXISTS (SELECT 1 FROM artifact_blobs WHERE artifact_id = NEW.artifact_id) BEGIN SELECT RAISE(ABORT, 'artifact_blobs is append-only'); END",
    ),
    (
        "artifact_blobs_no_update",
        "CREATE TRIGGER artifact_blobs_no_update BEFORE UPDATE ON artifact_blobs BEGIN SELECT RAISE(ABORT, 'artifact_blobs is append-only'); END",
    ),
    (
        "artifact_observations_no_delete",
        "CREATE TRIGGER artifact_observations_no_delete BEFORE DELETE ON artifact_observations BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END",
    ),
    (
        "artifact_observations_no_replace",
        "CREATE TRIGGER artifact_observations_no_replace BEFORE INSERT ON artifact_observations WHEN EXISTS (SELECT 1 FROM artifact_observations WHERE fetch_run_id = NEW.fetch_run_id AND source_url = NEW.source_url) BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END",
    ),
    (
        "artifact_observations_no_update",
        "CREATE TRIGGER artifact_observations_no_update BEFORE UPDATE ON artifact_observations BEGIN SELECT RAISE(ABORT, 'artifact_observations is append-only'); END",
    ),
    (
        "correction_evidence_no_delete",
        "CREATE TRIGGER correction_evidence_no_delete BEFORE DELETE ON correction_evidence BEGIN SELECT RAISE(ABORT, 'correction_evidence is append-only'); END",
    ),
    (
        "correction_evidence_no_replace",
        "CREATE TRIGGER correction_evidence_no_replace BEFORE INSERT ON correction_evidence WHEN EXISTS (SELECT 1 FROM correction_evidence WHERE correction_id = NEW.correction_id AND provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, 'correction_evidence is append-only'); END",
    ),
    (
        "correction_evidence_no_update",
        "CREATE TRIGGER correction_evidence_no_update BEFORE UPDATE ON correction_evidence BEGIN SELECT RAISE(ABORT, 'correction_evidence is append-only'); END",
    ),
    (
        "corrections_no_delete",
        "CREATE TRIGGER corrections_no_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT, 'corrections is append-only'); END",
    ),
    (
        "corrections_no_replace",
        "CREATE TRIGGER corrections_no_replace BEFORE INSERT ON corrections WHEN EXISTS (SELECT 1 FROM corrections WHERE correction_id = NEW.correction_id) BEGIN SELECT RAISE(ABORT, 'corrections is append-only'); END",
    ),
    (
        "corrections_no_update",
        "CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT, 'corrections is append-only'); END",
    ),
    (
        "extracted_candidates_no_delete",
        "CREATE TRIGGER extracted_candidates_no_delete BEFORE DELETE ON extracted_candidates BEGIN SELECT RAISE(ABORT, 'extracted_candidates is append-only'); END",
    ),
    (
        "extracted_candidates_no_replace",
        "CREATE TRIGGER extracted_candidates_no_replace BEFORE INSERT ON extracted_candidates WHEN EXISTS (SELECT 1 FROM extracted_candidates WHERE candidate_id = NEW.candidate_id) BEGIN SELECT RAISE(ABORT, 'extracted_candidates is append-only'); END",
    ),
    (
        "extracted_candidates_no_update",
        "CREATE TRIGGER extracted_candidates_no_update BEFORE UPDATE ON extracted_candidates BEGIN SELECT RAISE(ABORT, 'extracted_candidates is append-only'); END",
    ),
    (
        "extraction_runs_no_delete",
        "CREATE TRIGGER extraction_runs_no_delete BEFORE DELETE ON extraction_runs BEGIN SELECT RAISE(ABORT, 'extraction_runs is append-only'); END",
    ),
    (
        "extraction_runs_no_replace",
        "CREATE TRIGGER extraction_runs_no_replace BEFORE INSERT ON extraction_runs WHEN EXISTS (SELECT 1 FROM extraction_runs WHERE run_id = NEW.run_id) BEGIN SELECT RAISE(ABORT, 'extraction_runs is append-only'); END",
    ),
    (
        "extraction_runs_no_update",
        "CREATE TRIGGER extraction_runs_no_update BEFORE UPDATE ON extraction_runs BEGIN SELECT RAISE(ABORT, 'extraction_runs is append-only'); END",
    ),
    (
        "fetch_runs_no_delete",
        "CREATE TRIGGER fetch_runs_no_delete BEFORE DELETE ON fetch_runs BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END",
    ),
    (
        "fetch_runs_no_replace",
        "CREATE TRIGGER fetch_runs_no_replace BEFORE INSERT ON fetch_runs WHEN EXISTS (SELECT 1 FROM fetch_runs WHERE run_id = NEW.run_id) BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END",
    ),
    (
        "fetch_runs_no_update",
        "CREATE TRIGGER fetch_runs_no_update BEFORE UPDATE ON fetch_runs BEGIN SELECT RAISE(ABORT, 'fetch_runs is append-only'); END",
    ),
    (
        "finding_evidence_no_delete",
        "CREATE TRIGGER finding_evidence_no_delete BEFORE DELETE ON finding_evidence BEGIN SELECT RAISE(ABORT, 'finding_evidence is append-only'); END",
    ),
    (
        "finding_evidence_no_replace",
        "CREATE TRIGGER finding_evidence_no_replace BEFORE INSERT ON finding_evidence WHEN EXISTS (SELECT 1 FROM finding_evidence WHERE finding_id = NEW.finding_id AND provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, 'finding_evidence is append-only'); END",
    ),
    (
        "finding_evidence_no_update",
        "CREATE TRIGGER finding_evidence_no_update BEFORE UPDATE ON finding_evidence BEGIN SELECT RAISE(ABORT, 'finding_evidence is append-only'); END",
    ),
    (
        "finding_purposes_no_delete",
        "CREATE TRIGGER finding_purposes_no_delete BEFORE DELETE ON finding_purposes BEGIN SELECT RAISE(ABORT, 'finding_purposes is append-only'); END",
    ),
    (
        "finding_purposes_no_replace",
        "CREATE TRIGGER finding_purposes_no_replace BEFORE INSERT ON finding_purposes WHEN EXISTS (SELECT 1 FROM finding_purposes WHERE finding_id = NEW.finding_id AND purpose = NEW.purpose) BEGIN SELECT RAISE(ABORT, 'finding_purposes is append-only'); END",
    ),
    (
        "finding_purposes_no_update",
        "CREATE TRIGGER finding_purposes_no_update BEFORE UPDATE ON finding_purposes BEGIN SELECT RAISE(ABORT, 'finding_purposes is append-only'); END",
    ),
    (
        "normalized_offers_lifecycle_only",
        "CREATE TRIGGER normalized_offers_lifecycle_only BEFORE UPDATE ON normalized_offers WHEN OLD.offer_id IS NOT NEW.offer_id OR OLD.schema_version IS NOT NEW.schema_version OR OLD.institution_charter IS NOT NEW.institution_charter OR OLD.product_id IS NOT NEW.product_id OR OLD.source_id IS NOT NEW.source_id OR OLD.details_json IS NOT NEW.details_json OR OLD.scenario_id IS NOT NEW.scenario_id OR OLD.scenario_matches_json IS NOT NEW.scenario_matches_json OR OLD.note_rate IS NOT NEW.note_rate OR OLD.apr IS NOT NEW.apr OR OLD.points IS NOT NEW.points OR OLD.note_rate_provenance_id IS NOT NEW.note_rate_provenance_id OR OLD.apr_provenance_id IS NOT NEW.apr_provenance_id OR OLD.points_provenance_id IS NOT NEW.points_provenance_id OR OLD.conditions_json IS NOT NEW.conditions_json OR OLD.tiers_json IS NOT NEW.tiers_json OR OLD.headline_is_as_low_as IS NOT NEW.headline_is_as_low_as OR OLD.effective_at IS NOT NEW.effective_at OR OLD.observed_at IS NOT NEW.observed_at OR OLD.valid_until IS NOT NEW.valid_until OR (OLD.published_at IS NOT NULL AND OLD.published_at IS NOT NEW.published_at) BEGIN SELECT RAISE(ABORT, 'normalized_offers allows only a lifecycle status change'); END",
    ),
    (
        "normalized_offers_no_delete",
        "CREATE TRIGGER normalized_offers_no_delete BEFORE DELETE ON normalized_offers BEGIN SELECT RAISE(ABORT, 'normalized_offers is append-only'); END",
    ),
    (
        "normalized_offers_no_replace",
        "CREATE TRIGGER normalized_offers_no_replace BEFORE INSERT ON normalized_offers WHEN EXISTS (SELECT 1 FROM normalized_offers WHERE offer_id = NEW.offer_id) BEGIN SELECT RAISE(ABORT, 'normalized_offers is append-only'); END",
    ),
    (
        "not_modified_observations_no_delete",
        "CREATE TRIGGER not_modified_observations_no_delete BEFORE DELETE ON not_modified_observations BEGIN SELECT RAISE(ABORT, 'not_modified_observations is append-only'); END",
    ),
    (
        "not_modified_observations_no_replace",
        "CREATE TRIGGER not_modified_observations_no_replace BEFORE INSERT ON not_modified_observations WHEN EXISTS (SELECT 1 FROM not_modified_observations WHERE run_id = NEW.run_id) BEGIN SELECT RAISE(ABORT, 'not_modified_observations is append-only'); END",
    ),
    (
        "not_modified_observations_no_update",
        "CREATE TRIGGER not_modified_observations_no_update BEFORE UPDATE ON not_modified_observations BEGIN SELECT RAISE(ABORT, 'not_modified_observations is append-only'); END",
    ),
    (
        "offer_evidence_no_delete",
        "CREATE TRIGGER offer_evidence_no_delete BEFORE DELETE ON offer_evidence BEGIN SELECT RAISE(ABORT, 'offer_evidence is append-only'); END",
    ),
    (
        "offer_evidence_no_replace",
        "CREATE TRIGGER offer_evidence_no_replace BEFORE INSERT ON offer_evidence WHEN EXISTS (SELECT 1 FROM offer_evidence WHERE offer_id = NEW.offer_id AND provenance_id = NEW.provenance_id AND role = NEW.role AND field_path = NEW.field_path) BEGIN SELECT RAISE(ABORT, 'offer_evidence is append-only'); END",
    ),
    (
        "offer_evidence_no_update",
        "CREATE TRIGGER offer_evidence_no_update BEFORE UPDATE ON offer_evidence BEGIN SELECT RAISE(ABORT, 'offer_evidence is append-only'); END",
    ),
    (
        "product_findings_no_delete",
        "CREATE TRIGGER product_findings_no_delete BEFORE DELETE ON product_findings BEGIN SELECT RAISE(ABORT, 'product_findings is append-only'); END",
    ),
    (
        "product_findings_no_replace",
        "CREATE TRIGGER product_findings_no_replace BEFORE INSERT ON product_findings WHEN EXISTS (SELECT 1 FROM product_findings WHERE finding_id = NEW.finding_id) BEGIN SELECT RAISE(ABORT, 'product_findings is append-only'); END",
    ),
    (
        "product_findings_no_update",
        "CREATE TRIGGER product_findings_no_update BEFORE UPDATE ON product_findings BEGIN SELECT RAISE(ABORT, 'product_findings is append-only'); END",
    ),
    (
        "provenance_no_delete",
        "CREATE TRIGGER provenance_no_delete BEFORE DELETE ON provenance BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END",
    ),
    (
        "provenance_no_replace",
        "CREATE TRIGGER provenance_no_replace BEFORE INSERT ON provenance WHEN EXISTS (SELECT 1 FROM provenance WHERE provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END",
    ),
    (
        "provenance_no_update",
        "CREATE TRIGGER provenance_no_update BEFORE UPDATE ON provenance BEGIN SELECT RAISE(ABORT, 'provenance is append-only'); END",
    ),
    (
        "published_snapshots_no_delete",
        "CREATE TRIGGER published_snapshots_no_delete BEFORE DELETE ON published_snapshots BEGIN SELECT RAISE(ABORT, 'published_snapshots is append-only'); END",
    ),
    (
        "published_snapshots_no_replace",
        "CREATE TRIGGER published_snapshots_no_replace BEFORE INSERT ON published_snapshots WHEN EXISTS (SELECT 1 FROM published_snapshots WHERE snapshot_id = NEW.snapshot_id) BEGIN SELECT RAISE(ABORT, 'published_snapshots is append-only'); END",
    ),
    (
        "published_snapshots_no_update",
        "CREATE TRIGGER published_snapshots_no_update BEFORE UPDATE ON published_snapshots BEGIN SELECT RAISE(ABORT, 'published_snapshots is append-only'); END",
    ),
    (
        "review_decisions_no_delete",
        "CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END",
    ),
    (
        "review_decisions_no_replace",
        "CREATE TRIGGER review_decisions_no_replace BEFORE INSERT ON review_decisions WHEN EXISTS (SELECT 1 FROM review_decisions WHERE review_id = NEW.review_id) BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END",
    ),
    (
        "review_decisions_no_update",
        "CREATE TRIGGER review_decisions_no_update BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END",
    ),
    (
        "review_evidence_no_delete",
        "CREATE TRIGGER review_evidence_no_delete BEFORE DELETE ON review_evidence BEGIN SELECT RAISE(ABORT, 'review_evidence is append-only'); END",
    ),
    (
        "review_evidence_no_replace",
        "CREATE TRIGGER review_evidence_no_replace BEFORE INSERT ON review_evidence WHEN EXISTS (SELECT 1 FROM review_evidence WHERE review_id = NEW.review_id AND provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, 'review_evidence is append-only'); END",
    ),
    (
        "review_evidence_no_update",
        "CREATE TRIGGER review_evidence_no_update BEFORE UPDATE ON review_evidence BEGIN SELECT RAISE(ABORT, 'review_evidence is append-only'); END",
    ),
    (
        "snapshot_entries_no_delete",
        "CREATE TRIGGER snapshot_entries_no_delete BEFORE DELETE ON snapshot_entries BEGIN SELECT RAISE(ABORT, 'snapshot_entries is append-only'); END",
    ),
    (
        "snapshot_entries_no_replace",
        "CREATE TRIGGER snapshot_entries_no_replace BEFORE INSERT ON snapshot_entries WHEN EXISTS (SELECT 1 FROM snapshot_entries WHERE snapshot_id = NEW.snapshot_id AND offer_id = NEW.offer_id) BEGIN SELECT RAISE(ABORT, 'snapshot_entries is append-only'); END",
    ),
    (
        "snapshot_entries_no_update",
        "CREATE TRIGGER snapshot_entries_no_update BEFORE UPDATE ON snapshot_entries BEGIN SELECT RAISE(ABORT, 'snapshot_entries is append-only'); END",
    ),
    (
        "snapshot_reviews_no_delete",
        "CREATE TRIGGER snapshot_reviews_no_delete BEFORE DELETE ON snapshot_reviews BEGIN SELECT RAISE(ABORT, 'snapshot_reviews is append-only'); END",
    ),
    (
        "snapshot_reviews_no_replace",
        "CREATE TRIGGER snapshot_reviews_no_replace BEFORE INSERT ON snapshot_reviews WHEN EXISTS (SELECT 1 FROM snapshot_reviews WHERE snapshot_id = NEW.snapshot_id AND review_id = NEW.review_id) BEGIN SELECT RAISE(ABORT, 'snapshot_reviews is append-only'); END",
    ),
    (
        "snapshot_reviews_no_update",
        "CREATE TRIGGER snapshot_reviews_no_update BEFORE UPDATE ON snapshot_reviews BEGIN SELECT RAISE(ABORT, 'snapshot_reviews is append-only'); END",
    ),
    (
        "source_approval_evidence_no_delete",
        "CREATE TRIGGER source_approval_evidence_no_delete BEFORE DELETE ON source_approval_evidence BEGIN SELECT RAISE(ABORT, 'source_approval_evidence is append-only'); END",
    ),
    (
        "source_approval_evidence_no_replace",
        "CREATE TRIGGER source_approval_evidence_no_replace BEFORE INSERT ON source_approval_evidence WHEN EXISTS (SELECT 1 FROM source_approval_evidence WHERE approval_id = NEW.approval_id AND provenance_id = NEW.provenance_id) BEGIN SELECT RAISE(ABORT, 'source_approval_evidence is append-only'); END",
    ),
    (
        "source_approval_evidence_no_update",
        "CREATE TRIGGER source_approval_evidence_no_update BEFORE UPDATE ON source_approval_evidence BEGIN SELECT RAISE(ABORT, 'source_approval_evidence is append-only'); END",
    ),
    (
        "source_approvals_no_delete",
        "CREATE TRIGGER source_approvals_no_delete BEFORE DELETE ON source_approvals BEGIN SELECT RAISE(ABORT, 'source_approvals is append-only'); END",
    ),
    (
        "source_approvals_no_replace",
        "CREATE TRIGGER source_approvals_no_replace BEFORE INSERT ON source_approvals WHEN EXISTS (SELECT 1 FROM source_approvals WHERE approval_id = NEW.approval_id) BEGIN SELECT RAISE(ABORT, 'source_approvals is append-only'); END",
    ),
    (
        "source_approvals_no_update",
        "CREATE TRIGGER source_approvals_no_update BEFORE UPDATE ON source_approvals BEGIN SELECT RAISE(ABORT, 'source_approvals is append-only'); END",
    ),
    (
        "source_fingerprints_no_delete",
        "CREATE TRIGGER source_fingerprints_no_delete BEFORE DELETE ON source_fingerprints BEGIN SELECT RAISE(ABORT, 'source_fingerprints is append-only'); END",
    ),
    (
        "source_fingerprints_no_replace",
        "CREATE TRIGGER source_fingerprints_no_replace BEFORE INSERT ON source_fingerprints WHEN EXISTS (SELECT 1 FROM source_fingerprints WHERE fingerprint_id = NEW.fingerprint_id) BEGIN SELECT RAISE(ABORT, 'source_fingerprints is append-only'); END",
    ),
    (
        "source_fingerprints_no_update",
        "CREATE TRIGGER source_fingerprints_no_update BEFORE UPDATE ON source_fingerprints BEGIN SELECT RAISE(ABORT, 'source_fingerprints is append-only'); END",
    ),
    (
        "validation_results_no_delete",
        "CREATE TRIGGER validation_results_no_delete BEFORE DELETE ON validation_results BEGIN SELECT RAISE(ABORT, 'validation_results is append-only'); END",
    ),
    (
        "validation_results_no_replace",
        "CREATE TRIGGER validation_results_no_replace BEFORE INSERT ON validation_results WHEN EXISTS (SELECT 1 FROM validation_results WHERE validation_id = NEW.validation_id) BEGIN SELECT RAISE(ABORT, 'validation_results is append-only'); END",
    ),
    (
        "validation_results_no_update",
        "CREATE TRIGGER validation_results_no_update BEFORE UPDATE ON validation_results BEGIN SELECT RAISE(ABORT, 'validation_results is append-only'); END",
    ),
    (
        "withdrawal_entries_no_delete",
        "CREATE TRIGGER withdrawal_entries_no_delete BEFORE DELETE ON withdrawal_entries BEGIN SELECT RAISE(ABORT, 'withdrawal_entries is append-only'); END",
    ),
    (
        "withdrawal_entries_no_replace",
        "CREATE TRIGGER withdrawal_entries_no_replace BEFORE INSERT ON withdrawal_entries WHEN EXISTS (SELECT 1 FROM withdrawal_entries WHERE withdrawal_id = NEW.withdrawal_id) BEGIN SELECT RAISE(ABORT, 'withdrawal_entries is append-only'); END",
    ),
    (
        "withdrawal_entries_no_update",
        "CREATE TRIGGER withdrawal_entries_no_update BEFORE UPDATE ON withdrawal_entries BEGIN SELECT RAISE(ABORT, 'withdrawal_entries is append-only'); END",
    ),
    (
        "withdrawal_lifts_no_delete",
        "CREATE TRIGGER withdrawal_lifts_no_delete BEFORE DELETE ON withdrawal_lifts BEGIN SELECT RAISE(ABORT, 'withdrawal_lifts is append-only'); END",
    ),
    (
        "withdrawal_lifts_no_replace",
        "CREATE TRIGGER withdrawal_lifts_no_replace BEFORE INSERT ON withdrawal_lifts WHEN EXISTS (SELECT 1 FROM withdrawal_lifts WHERE withdrawal_id = NEW.withdrawal_id) BEGIN SELECT RAISE(ABORT, 'withdrawal_lifts is append-only'); END",
    ),
    (
        "withdrawal_lifts_no_update",
        "CREATE TRIGGER withdrawal_lifts_no_update BEFORE UPDATE ON withdrawal_lifts BEGIN SELECT RAISE(ABORT, 'withdrawal_lifts is append-only'); END",
    ),
)


def _append_only_triggers(present: bool) -> None:
    for name, statement in _FROZEN_TRIGGERS:
        if present:
            op.execute(statement)
        else:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    op.create_table(
        "institutions",
        sa.Column("charter_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("successor_charter_number", sa.Integer(), nullable=True),
        sa.Column("charter_verified", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(status = 'merged' AND successor_charter_number IS NOT NULL AND successor_charter_number != charter_number AND successor_charter_number > 0 AND successor_charter_number < 100000000) OR (status != 'merged' AND successor_charter_number IS NULL)",
            name="ck_institutions_successor",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'merged', 'liquidated', 'converted', 'under_review')",
            name="ck_institutions_status",
        ),
        sa.CheckConstraint(
            "charter_number > 0 AND charter_number < 100000000",
            name="ck_institutions_charter_number",
        ),
        sa.CheckConstraint("charter_verified IN (0, 1)", name="ck_institutions_charter_verified"),
        sa.PrimaryKeyConstraint("charter_number"),
    )
    op.create_table(
        "published_snapshots",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("published_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("published_by", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("supersedes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "supersedes IS NULL OR supersedes != snapshot_id", name="ck_snapshots_supersedes"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes"],
            ["published_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_table(
        "rate_sources",
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "evidence_kind IN ('html', 'rendered_dom', 'pdf', 'image_ocr', 'json')",
            name="ck_rate_sources_evidence_kind",
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_table(
        "review_decisions",
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("decided_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("fingerprints_json", cu_rates.db.types.JsonDocument(), nullable=True),
        sa.Column("reviewed_product_sha256", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(subject = 'offer' AND fingerprints_json IS NOT NULL AND reviewed_product_sha256 IS NULL) OR (subject = 'product' AND fingerprints_json IS NULL AND reviewed_product_sha256 IS NOT NULL)",
            name="ck_review_decisions_binding",
        ),
        sa.CheckConstraint("subject IN ('offer', 'product')", name="ck_review_decisions_subject"),
        sa.CheckConstraint(
            "verdict IN ('approved', 'rejected')", name="ck_review_decisions_verdict"
        ),
        sa.PrimaryKeyConstraint("review_id"),
    )
    op.create_table(
        "service_providers",
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('loan_platform', 'rate_calculator', 'partner_lender', 'other')",
            name="ck_service_providers_kind",
        ),
        sa.PrimaryKeyConstraint("provider_id"),
    )
    op.create_table(
        "validation_policies",
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("required_rules_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("policy_id"),
    )
    op.create_table(
        "withdrawal_entries",
        sa.Column("withdrawal_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("withdrawn_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('offer', 'product', 'institution', 'snapshot')",
            name="ck_withdrawal_entries_scope",
        ),
        sa.PrimaryKeyConstraint("withdrawal_id"),
    )
    op.create_table(
        "fetch_profiles",
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("min_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("respect_robots", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "method IN ('http_get', 'rendered_browser')", name="ck_fetch_profiles_method"
        ),
        sa.CheckConstraint(
            "min_interval_seconds BETWEEN 1 AND 604800",
            name="ck_fetch_profiles_min_interval_seconds",
        ),
        sa.CheckConstraint("respect_robots = 1", name="ck_fetch_profiles_respect_robots"),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 300", name="ck_fetch_profiles_timeout_seconds"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.create_table(
        "membership_rules",
        sa.Column("membership_rule_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('open', 'geographic', 'employer', 'association', 'other')",
            name="ck_membership_rules_kind",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.PrimaryKeyConstraint("membership_rule_id"),
    )
    op.create_table(
        "products",
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lifecycle", sa.Text(), nullable=False),
        sa.Column("rate_availability", sa.Text(), nullable=False),
        sa.Column("conforming_status", sa.Text(), nullable=False),
        sa.Column("government_guaranty", sa.Text(), nullable=False),
        sa.Column("notes_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.CheckConstraint(
            "conforming_status IN ('conforming', 'high_balance_conforming', 'jumbo', 'unknown')",
            name="ck_products_conforming_status",
        ),
        sa.CheckConstraint(
            "family IN ('mortgage', 'home_equity_loan', 'heloc')", name="ck_products_family"
        ),
        sa.CheckConstraint(
            "government_guaranty IN ('none', 'fha', 'va', 'usda', 'unknown')",
            name="ck_products_government_guaranty",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'discontinued', 'under_review')", name="ck_products_lifecycle"
        ),
        sa.CheckConstraint(
            "rate_availability IN ('public_rate', 'quote_only', 'login_required', 'blocked', 'no_public_rate', 'not_offered', 'unknown')",
            name="ck_products_rate_availability",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.PrimaryKeyConstraint("product_id"),
        sa.UniqueConstraint("institution_charter", "product_id", name="uq_products_institution"),
    )
    op.create_table(
        "snapshot_reviews",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["review_decisions.review_id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["published_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "review_id"),
    )
    op.create_table(
        "source_approvals",
        sa.Column("approval_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("decided_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')", name="ck_source_approvals_decision"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_table(
        "withdrawal_lifts",
        sa.Column("withdrawal_id", sa.Text(), nullable=False),
        sa.Column("lifted_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("lifted_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["withdrawal_id"],
            ["withdrawal_entries.withdrawal_id"],
        ),
        sa.PrimaryKeyConstraint("withdrawal_id"),
    )
    op.create_table(
        "fetch_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("finished_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("validator_kind", sa.Text(), nullable=True),
        sa.Column("validator_value", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error IS NULL) OR (status IN ('failed', 'blocked') AND error IS NOT NULL)",
            name="ck_fetch_runs_error",
        ),
        sa.CheckConstraint(
            "(validator_kind IS NULL AND validator_value IS NULL) OR (validator_kind IN ('etag', 'last_modified') AND validator_value IS NOT NULL AND status = 'succeeded')",
            name="ck_fetch_runs_validator",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'blocked')", name="ck_fetch_runs_status"
        ),
        sa.CheckConstraint("finished_at >= started_at", name="ck_fetch_runs_order"),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["fetch_profiles.profile_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("run_id", "source_id", name="uq_fetch_runs_source"),
    )
    op.create_table(
        "institution_source_relationships",
        sa.Column("relationship_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", cu_rates.db.types.UtcDateTime(), nullable=True),
        sa.CheckConstraint(
            "(kind = 'first_party' AND provider_id IS NULL) OR (kind IN ('provider_hosted', 'partner_published') AND provider_id IS NOT NULL)",
            name="ck_relationships_provider",
        ),
        sa.CheckConstraint(
            "kind IN ('first_party', 'provider_hosted', 'partner_published')",
            name="ck_institution_source_relationships_kind",
        ),
        sa.CheckConstraint(
            "(approved_by IS NULL AND approved_at IS NULL) OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_relationships_approval",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter", "product_id"],
            ["products.institution_charter", "products.product_id"],
            name="fk_relationships_product_same_institution",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["service_providers.provider_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("relationship_id"),
        sa.UniqueConstraint(
            "institution_charter", "source_id", "product_id", name="uq_relationships_binding"
        ),
    )
    op.create_table(
        "product_borrower_programs",
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("program", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "program IN ('first_time_buyer')", name="ck_product_borrower_programs_program"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
        ),
        sa.PrimaryKeyConstraint("product_id", "program"),
    )
    op.create_table(
        "product_findings",
        sa.Column("finding_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=True),
        sa.Column("observed_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=True),
        sa.Column("out_of_scope_product", sa.Text(), nullable=True),
        sa.Column("rate_availability", sa.Text(), nullable=True),
        sa.Column("review_state", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", cu_rates.db.types.UtcDateTime(), nullable=True),
        sa.Column("review_rationale", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(review_state = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL AND review_rationale IS NULL) OR (review_state IN ('verified', 'rejected') AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND review_rationale IS NOT NULL AND NOT (review_state = 'verified' AND scope = 'unknown'))",
            name="ck_product_findings_review",
        ),
        sa.CheckConstraint(
            "(scope = 'in_scope' AND family IS NOT NULL AND rate_availability IS NOT NULL AND out_of_scope_product IS NULL) OR (scope = 'out_of_scope' AND family IS NULL AND product_id IS NULL AND out_of_scope_product IS NOT NULL AND rate_availability IS NULL) OR (scope = 'unknown' AND family IS NULL AND product_id IS NULL AND out_of_scope_product IS NULL AND (rate_availability IS NULL OR rate_availability = 'unknown'))",
            name="ck_product_findings_scope",
        ),
        sa.CheckConstraint(
            "family IN ('mortgage', 'home_equity_loan', 'heloc')", name="ck_product_findings_family"
        ),
        sa.CheckConstraint(
            "out_of_scope_product IN ('vehicle', 'unsecured_personal', 'construction', 'renovation', 'land', 'reverse_mortgage', 'commercial', 'deposit', 'other_consumer')",
            name="ck_product_findings_out_of_scope_product",
        ),
        sa.CheckConstraint(
            "rate_availability IN ('public_rate', 'quote_only', 'login_required', 'blocked', 'no_public_rate', 'not_offered', 'unknown')",
            name="ck_product_findings_rate_availability",
        ),
        sa.CheckConstraint(
            "review_state IN ('proposed', 'verified', 'rejected')",
            name="ck_product_findings_review_state",
        ),
        sa.CheckConstraint(
            "scope IN ('in_scope', 'out_of_scope', 'unknown')", name="ck_product_findings_scope"
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter", "product_id"],
            ["products.institution_charter", "products.product_id"],
            name="fk_findings_product_same_institution",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("finding_id"),
    )
    op.create_table(
        "product_partner_programs",
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("program", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "program IN ('masshousing')", name="ck_product_partner_programs_program"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
        ),
        sa.PrimaryKeyConstraint("product_id", "program"),
    )
    op.create_table(
        "extraction_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("fetch_run_id", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("configuration_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("finished_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error IS NULL) OR (status IN ('failed', 'blocked') AND error IS NOT NULL AND candidate_count = 0)",
            name="ck_extraction_runs_error",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'blocked')", name="ck_extraction_runs_status"
        ),
        sa.CheckConstraint("candidate_count >= 0", name="ck_extraction_runs_candidate_count"),
        sa.CheckConstraint("finished_at >= started_at", name="ck_extraction_runs_order"),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["fetch_runs.run_id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "finding_purposes",
        sa.Column("finding_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('purchase', 'rate_term_refinance', 'cash_out_refinance')",
            name="ck_finding_purposes_purpose",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["product_findings.finding_id"],
        ),
        sa.PrimaryKeyConstraint("finding_id", "purpose"),
    )
    op.create_table(
        "artifact_blobs",
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.CheckConstraint("byte_length >= 0", name="ck_artifact_blobs_byte_length"),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_table(
        "artifact_observations",
        sa.Column("fetch_run_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("fetched_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599", name="ck_artifact_observations_http_status"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact_blobs.artifact_id"],
        ),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["fetch_runs.run_id"],
            name="fk_observations_fetch_run",
        ),
        sa.PrimaryKeyConstraint("fetch_run_id", "source_url"),
        sa.UniqueConstraint("fetch_run_id", "artifact_id", name="uq_observations_run_artifact"),
    )
    op.create_table(
        "not_modified_observations",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("started_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("observed_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("original_run_id", sa.Text(), nullable=False),
        sa.Column("original_artifact_id", sa.Text(), nullable=False),
        sa.Column("validator_kind", sa.Text(), nullable=False),
        sa.Column("validator_value", sa.Text(), nullable=False),
        sa.Column("response_etag", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "validator_kind IN ('etag', 'last_modified')",
            name="ck_not_modified_observations_validator_kind",
        ),
        sa.CheckConstraint("http_status = 304", name="ck_not_modified_observations_http_status"),
        sa.CheckConstraint("observed_at >= started_at", name="ck_not_modified_observations_order"),
        sa.CheckConstraint(
            "run_id != original_run_id", name="ck_not_modified_observations_distinct_run"
        ),
        sa.ForeignKeyConstraint(
            ["original_run_id", "source_id"],
            ["fetch_runs.run_id", "fetch_runs.source_id"],
            name="fk_not_modified_same_source",
        ),
        sa.ForeignKeyConstraint(
            ["original_run_id", "original_artifact_id"],
            ["artifact_observations.fetch_run_id", "artifact_observations.artifact_id"],
            name="fk_not_modified_run_artifact",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["fetch_profiles.profile_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "provenance",
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("locator_kind", sa.Text(), nullable=False),
        sa.Column("locator_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("configuration_sha256", sa.Text(), nullable=False),
        sa.Column("normalization_version", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "locator_kind IN ('html', 'rendered_dom', 'pdf', 'image_ocr', 'json')",
            name="ck_provenance_locator_kind",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact_blobs.artifact_id"],
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.PrimaryKeyConstraint("provenance_id"),
        sa.UniqueConstraint(
            "institution_charter", "provenance_id", name="uq_provenance_institution"
        ),
    )
    op.create_table(
        "review_evidence",
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_review_evidence_ordinal"),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["review_decisions.review_id"],
        ),
        sa.PrimaryKeyConstraint("review_id", "provenance_id"),
    )
    op.create_table(
        "source_fingerprints",
        sa.Column("fingerprint_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("fetch_run_id", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("observed_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("structure_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact_blobs.artifact_id"],
        ),
        sa.ForeignKeyConstraint(
            ["fetch_run_id"],
            ["fetch_runs.run_id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["rate_sources.source_id"],
        ),
        sa.PrimaryKeyConstraint("fingerprint_id"),
    )
    op.create_table(
        "extracted_candidates",
        sa.Column("candidate_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("extraction_run_id", sa.Text(), nullable=False),
        sa.Column("proposed_path", sa.Text(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_runs.run_id"],
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.PrimaryKeyConstraint("candidate_id"),
    )
    op.create_table(
        "finding_evidence",
        sa.Column("finding_id", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["product_findings.finding_id"],
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.PrimaryKeyConstraint("finding_id", "provenance_id"),
    )
    op.create_table(
        "normalized_offers",
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("details_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=True),
        sa.Column("scenario_matches_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("note_rate", cu_rates.db.types.DecimalText(), nullable=True),
        sa.Column("apr", cu_rates.db.types.DecimalText(), nullable=True),
        sa.Column("points", cu_rates.db.types.DecimalText(), nullable=True),
        sa.Column("note_rate_provenance_id", sa.Text(), nullable=True),
        sa.Column("apr_provenance_id", sa.Text(), nullable=True),
        sa.Column("points_provenance_id", sa.Text(), nullable=True),
        sa.Column("conditions_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("tiers_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("headline_is_as_low_as", sa.Integer(), nullable=False),
        sa.Column("effective_at", cu_rates.db.types.UtcDateTime(), nullable=True),
        sa.Column("observed_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("published_at", cu_rates.db.types.UtcDateTime(), nullable=True),
        sa.Column("valid_until", cu_rates.db.types.UtcDateTime(), nullable=True),
        sa.CheckConstraint(
            "scenario_id IN ('purchase_30y_fixed', 'rate_term_refi_30y_fixed', 'fixed_home_equity_10y', 'heloc')",
            name="ck_normalized_offers_scenario_id",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'quarantined', 'approved', 'published', 'withdrawn', 'superseded')",
            name="ck_normalized_offers_status",
        ),
        sa.CheckConstraint(
            "(apr IS NULL) = (apr_provenance_id IS NULL)",
            name="ck_normalized_offers_apr_provenance",
        ),
        sa.CheckConstraint(
            "(note_rate IS NULL) = (note_rate_provenance_id IS NULL)",
            name="ck_normalized_offers_note_rate_provenance",
        ),
        sa.CheckConstraint(
            "(points IS NULL) = (points_provenance_id IS NULL)",
            name="ck_normalized_offers_points_provenance",
        ),
        sa.CheckConstraint(
            "status NOT IN ('published', 'withdrawn') OR published_at IS NOT NULL",
            name="ck_normalized_offers_published_at",
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR status IN ('published', 'withdrawn', 'superseded')",
            name="ck_normalized_offers_published_at_status",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > observed_at",
            name="ck_normalized_offers_valid_until",
        ),
        sa.CheckConstraint(
            "note_rate IS NULL OR lower(note_rate) NOT IN ('nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity', '-infinity', '+infinity', 'snan', '-snan', '+snan')",
            name="ck_normalized_offers_note_rate_finite",
        ),
        sa.CheckConstraint(
            "apr IS NULL OR lower(apr) NOT IN ('nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity', '-infinity', '+infinity', 'snan', '-snan', '+snan')",
            name="ck_normalized_offers_apr_finite",
        ),
        sa.CheckConstraint(
            "points IS NULL OR lower(points) NOT IN ('nan', '-nan', '+nan', 'inf', '-inf', '+inf', 'infinity', '-infinity', '+infinity', 'snan', '-snan', '+snan')",
            name="ck_normalized_offers_points_finite",
        ),
        sa.CheckConstraint(
            "headline_is_as_low_as IN (0, 1)", name="ck_normalized_offers_as_low_as"
        ),
        sa.ForeignKeyConstraint(
            ["apr_provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter", "product_id"],
            ["products.institution_charter", "products.product_id"],
            name="fk_offers_product_same_institution",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter", "source_id", "product_id"],
            [
                "institution_source_relationships.institution_charter",
                "institution_source_relationships.source_id",
                "institution_source_relationships.product_id",
            ],
            name="fk_offers_relationship_same_institution",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter"],
            ["institutions.charter_number"],
        ),
        sa.ForeignKeyConstraint(
            ["note_rate_provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.ForeignKeyConstraint(
            ["points_provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.PrimaryKeyConstraint("offer_id"),
        sa.UniqueConstraint("institution_charter", "offer_id", name="uq_offers_institution"),
    )
    op.create_table(
        "source_approval_evidence",
        sa.Column("approval_id", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["source_approvals.approval_id"],
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.PrimaryKeyConstraint("approval_id", "provenance_id"),
    )
    op.create_table(
        "corrections",
        sa.Column("correction_id", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=False),
        sa.Column("corrected_value", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("corrected_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("fingerprints_json", cu_rates.db.types.JsonDocument(), nullable=True),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["normalized_offers.offer_id"],
        ),
        sa.PrimaryKeyConstraint("correction_id"),
    )
    op.create_table(
        "offer_evidence",
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("institution_charter", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "role IN ('supporting', 'note_rate', 'apr', 'points', 'field')",
            name="ck_offer_evidence_role",
        ),
        sa.CheckConstraint(
            "(role = 'field' AND field_path != '') OR (role != 'field' AND field_path = '')",
            name="ck_offer_evidence_field_path",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_offer_evidence_ordinal"),
        sa.ForeignKeyConstraint(
            ["institution_charter", "offer_id"],
            ["normalized_offers.institution_charter", "normalized_offers.offer_id"],
            name="fk_offer_evidence_same_institution",
        ),
        sa.ForeignKeyConstraint(
            ["institution_charter", "provenance_id"],
            ["provenance.institution_charter", "provenance.provenance_id"],
            name="fk_offer_evidence_provenance_institution",
        ),
        sa.PrimaryKeyConstraint("offer_id", "provenance_id", "role", "field_path"),
    )
    op.create_table(
        "snapshot_entries",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("structural_sha256", sa.Text(), nullable=False),
        sa.Column("value_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["normalized_offers.offer_id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["published_snapshots.snapshot_id"],
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "offer_id"),
    )
    op.create_table(
        "validation_results",
        sa.Column("validation_id", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("offer_id", sa.Text(), nullable=False),
        sa.Column("fingerprints_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.Column("evaluated_at", cu_rates.db.types.UtcDateTime(), nullable=False),
        sa.Column("validator_name", sa.Text(), nullable=False),
        sa.Column("validator_version", sa.Text(), nullable=False),
        sa.Column("checked_paths_json", cu_rates.db.types.JsonDocument(), nullable=False),
        sa.CheckConstraint(
            "passed = 1 OR detail IS NOT NULL", name="ck_validation_results_failure_detail"
        ),
        sa.CheckConstraint("passed IN (0, 1)", name="ck_validation_results_passed"),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["normalized_offers.offer_id"],
        ),
        sa.PrimaryKeyConstraint("validation_id"),
    )
    op.create_table(
        "correction_evidence",
        sa.Column("correction_id", sa.Text(), nullable=False),
        sa.Column("provenance_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["correction_id"],
            ["corrections.correction_id"],
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            ["provenance.provenance_id"],
        ),
        sa.PrimaryKeyConstraint("correction_id", "provenance_id"),
    )
    _append_only_triggers(True)


def downgrade() -> None:
    _append_only_triggers(False)
    op.drop_table("correction_evidence")
    op.drop_table("validation_results")
    op.drop_table("snapshot_entries")
    op.drop_table("offer_evidence")
    op.drop_table("corrections")
    op.drop_table("source_approval_evidence")
    op.drop_table("normalized_offers")
    op.drop_table("finding_evidence")
    op.drop_table("extracted_candidates")
    op.drop_table("source_fingerprints")
    op.drop_table("review_evidence")
    op.drop_table("provenance")
    op.drop_table("not_modified_observations")
    op.drop_table("artifact_observations")
    op.drop_table("artifact_blobs")
    op.drop_table("finding_purposes")
    op.drop_table("extraction_runs")
    op.drop_table("product_partner_programs")
    op.drop_table("product_findings")
    op.drop_table("product_borrower_programs")
    op.drop_table("institution_source_relationships")
    op.drop_table("fetch_runs")
    op.drop_table("withdrawal_lifts")
    op.drop_table("source_approvals")
    op.drop_table("snapshot_reviews")
    op.drop_table("products")
    op.drop_table("membership_rules")
    op.drop_table("fetch_profiles")
    op.drop_table("withdrawal_entries")
    op.drop_table("validation_policies")
    op.drop_table("service_providers")
    op.drop_table("review_decisions")
    op.drop_table("rate_sources")
    op.drop_table("published_snapshots")
    op.drop_table("institutions")

"""Operational schema for the canonical domain model.

Enum checks use the T010 and T011 values. Successor charters are attributes,
not foreign keys, because a successor may not be in this roster. Canonical
comparison scenarios stay in code. Nested offer details, conditions, tiers,
and scenario matches are JSON text; headline rates and provenance are columns.
A withdrawal lift is its own append-only row so the original entry is not updated.
"""

from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
)
from sqlalchemy import Text as SqlText

from cu_rates.contracts.enums import (
    BorrowerProgram,
    CanonicalScenarioId,
    ConformingStatus,
    EvidenceKind,
    FetchMethod,
    FindingReviewState,
    GovernmentGuaranty,
    InstitutionStatus,
    LoanPurpose,
    MembershipKind,
    OfferStatus,
    OutOfScopeProduct,
    PartnerProgram,
    ProductFamily,
    ProductLifecycle,
    ProductScope,
    ProviderKind,
    RateAvailability,
    ReviewSubject,
    ReviewVerdict,
    RunStatus,
    SourceRelationshipKind,
    WithdrawalScope,
)
from cu_rates.db.types import DecimalText, JsonDocument, UtcDateTime

metadata = MetaData()

# Observations, reviews, withdrawals, and findings. Child rows that belong to
# those records are included so their evidence cannot be rewritten either.
APPEND_ONLY_TABLES = frozenset(
    {
        "fetch_runs",
        "not_modified_observations",
        "review_decisions",
        "withdrawal_entries",
        "withdrawal_lifts",
        "product_findings",
        "finding_evidence",
        "finding_purposes",
        "raw_artifacts",
        "provenance",
        "source_approvals",
        "source_approval_evidence",
        "corrections",
        "published_snapshots",
        "snapshot_entries",
    }
)

ENUM_CHECKS: list[tuple[str, str, type[StrEnum]]] = []


def _enum_check(table: str, column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    ENUM_CHECKS.append((table, column, enum_cls))
    literals = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({literals})", name=f"ck_{table}_{column}")


def _id(name: str, *, primary_key: bool = False) -> Column[str]:
    return Column(name, SqlText, primary_key=primary_key)


def _schema_version() -> Column[str]:
    return Column("schema_version", SqlText, nullable=False)


def _charter(name: str, *, primary_key: bool = False) -> Column[int]:
    return Column(
        name,
        Integer,
        primary_key=primary_key,
        nullable=False,
    )


institutions = Table(
    "institutions",
    metadata,
    _charter("charter_number", primary_key=True),
    _schema_version(),
    Column("legal_name", SqlText, nullable=False),
    Column("status", SqlText, nullable=False),
    Column("successor_charter_number", Integer),
    Column("charter_verified", Integer, nullable=False),
    CheckConstraint(
        "charter_number > 0 AND charter_number < 100000000",
        name="ck_institutions_charter_number",
    ),
    CheckConstraint("charter_verified IN (0, 1)", name="ck_institutions_charter_verified"),
    CheckConstraint(
        "(status = 'merged' AND successor_charter_number IS NOT NULL"
        " AND successor_charter_number != charter_number"
        " AND successor_charter_number > 0 AND successor_charter_number < 100000000)"
        " OR (status != 'merged' AND successor_charter_number IS NULL)",
        name="ck_institutions_successor",
    ),
    _enum_check("institutions", "status", InstitutionStatus),
)

membership_rules = Table(
    "membership_rules",
    metadata,
    _id("membership_rule_id", primary_key=True),
    _schema_version(),
    Column(
        "institution_charter", Integer, ForeignKey("institutions.charter_number"), nullable=False
    ),
    Column("kind", SqlText, nullable=False),
    Column("description", SqlText, nullable=False),
    Column("source_url", SqlText),
    _enum_check("membership_rules", "kind", MembershipKind),
)

service_providers = Table(
    "service_providers",
    metadata,
    _id("provider_id", primary_key=True),
    _schema_version(),
    Column("name", SqlText, nullable=False),
    Column("kind", SqlText, nullable=False),
    _enum_check("service_providers", "kind", ProviderKind),
)

rate_sources = Table(
    "rate_sources",
    metadata,
    _id("source_id", primary_key=True),
    _schema_version(),
    Column("url", SqlText, nullable=False),
    Column("evidence_kind", SqlText, nullable=False),
    Column("description", SqlText),
    _enum_check("rate_sources", "evidence_kind", EvidenceKind),
)

products = Table(
    "products",
    metadata,
    _id("product_id", primary_key=True),
    _schema_version(),
    Column(
        "institution_charter", Integer, ForeignKey("institutions.charter_number"), nullable=False
    ),
    Column("family", SqlText, nullable=False),
    Column("name", SqlText, nullable=False),
    Column("lifecycle", SqlText, nullable=False),
    Column("rate_availability", SqlText, nullable=False),
    Column("conforming_status", SqlText, nullable=False),
    Column("government_guaranty", SqlText, nullable=False),
    Column("notes_json", JsonDocument, nullable=False),
    UniqueConstraint("institution_charter", "product_id", name="uq_products_institution"),
    _enum_check("products", "family", ProductFamily),
    _enum_check("products", "lifecycle", ProductLifecycle),
    _enum_check("products", "rate_availability", RateAvailability),
    _enum_check("products", "conforming_status", ConformingStatus),
    _enum_check("products", "government_guaranty", GovernmentGuaranty),
)

product_borrower_programs = Table(
    "product_borrower_programs",
    metadata,
    Column("product_id", SqlText, ForeignKey("products.product_id"), primary_key=True),
    Column("program", SqlText, primary_key=True),
    _enum_check("product_borrower_programs", "program", BorrowerProgram),
)

product_partner_programs = Table(
    "product_partner_programs",
    metadata,
    Column("product_id", SqlText, ForeignKey("products.product_id"), primary_key=True),
    Column("program", SqlText, primary_key=True),
    _enum_check("product_partner_programs", "program", PartnerProgram),
)

institution_source_relationships = Table(
    "institution_source_relationships",
    metadata,
    _id("relationship_id", primary_key=True),
    _schema_version(),
    Column(
        "institution_charter", Integer, ForeignKey("institutions.charter_number"), nullable=False
    ),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("product_id", SqlText),
    Column("provider_id", SqlText, ForeignKey("service_providers.provider_id")),
    Column("kind", SqlText, nullable=False),
    Column("approved_by", SqlText),
    Column("approved_at", UtcDateTime),
    UniqueConstraint(
        "institution_charter",
        "source_id",
        "product_id",
        name="uq_relationships_binding",
    ),
    ForeignKeyConstraint(
        ["institution_charter", "product_id"],
        ["products.institution_charter", "products.product_id"],
        name="fk_relationships_product_same_institution",
    ),
    CheckConstraint(
        "(kind = 'first_party' AND provider_id IS NULL)"
        " OR (kind IN ('provider_hosted', 'partner_published') AND provider_id IS NOT NULL)",
        name="ck_relationships_provider",
    ),
    CheckConstraint(
        "(approved_by IS NULL AND approved_at IS NULL)"
        " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
        name="ck_relationships_approval",
    ),
    _enum_check("institution_source_relationships", "kind", SourceRelationshipKind),
)

fetch_profiles = Table(
    "fetch_profiles",
    metadata,
    _id("profile_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("method", SqlText, nullable=False),
    Column("min_interval_seconds", Integer, nullable=False),
    Column("timeout_seconds", Integer, nullable=False),
    Column("respect_robots", Integer, nullable=False),
    CheckConstraint(
        "min_interval_seconds BETWEEN 1 AND 604800",
        name="ck_fetch_profiles_min_interval_seconds",
    ),
    CheckConstraint(
        "timeout_seconds BETWEEN 1 AND 300",
        name="ck_fetch_profiles_timeout_seconds",
    ),
    CheckConstraint("respect_robots = 1", name="ck_fetch_profiles_respect_robots"),
    _enum_check("fetch_profiles", "method", FetchMethod),
)

fetch_runs = Table(
    "fetch_runs",
    metadata,
    _id("run_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("profile_id", SqlText, ForeignKey("fetch_profiles.profile_id"), nullable=False),
    Column("profile_sha256", SqlText),
    Column("status", SqlText, nullable=False),
    Column("started_at", UtcDateTime, nullable=False),
    Column("finished_at", UtcDateTime, nullable=False),
    Column("git_sha", SqlText, nullable=False),
    Column("error", SqlText),
    Column("validator_kind", SqlText),
    Column("validator_value", SqlText),
    CheckConstraint("finished_at >= started_at", name="ck_fetch_runs_order"),
    CheckConstraint(
        "(status = 'succeeded' AND error IS NULL)"
        " OR (status IN ('failed', 'blocked') AND error IS NOT NULL)",
        name="ck_fetch_runs_error",
    ),
    CheckConstraint(
        "(validator_kind IS NULL AND validator_value IS NULL)"
        " OR (validator_kind IN ('etag', 'last_modified') AND validator_value IS NOT NULL"
        " AND status = 'succeeded')",
        name="ck_fetch_runs_validator",
    ),
    _enum_check("fetch_runs", "status", RunStatus),
)

raw_artifacts = Table(
    "raw_artifacts",
    metadata,
    _id("artifact_id", primary_key=True),
    _schema_version(),
    Column("media_type", SqlText, nullable=False),
    Column("source_url", SqlText, nullable=False),
    Column("fetched_at", UtcDateTime, nullable=False),
    Column("http_status", Integer, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("fetch_run_id", SqlText, ForeignKey("fetch_runs.run_id"), nullable=False),
    CheckConstraint("http_status BETWEEN 100 AND 599", name="ck_raw_artifacts_http_status"),
    CheckConstraint("byte_length >= 0", name="ck_raw_artifacts_byte_length"),
)

not_modified_observations = Table(
    "not_modified_observations",
    metadata,
    _id("run_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("profile_id", SqlText, ForeignKey("fetch_profiles.profile_id"), nullable=False),
    Column("profile_sha256", SqlText, nullable=False),
    Column("git_sha", SqlText, nullable=False),
    Column("started_at", UtcDateTime, nullable=False),
    Column("observed_at", UtcDateTime, nullable=False),
    Column("original_run_id", SqlText, ForeignKey("fetch_runs.run_id"), nullable=False),
    Column(
        "original_artifact_id", SqlText, ForeignKey("raw_artifacts.artifact_id"), nullable=False
    ),
    Column("validator_kind", SqlText, nullable=False),
    Column("validator_value", SqlText, nullable=False),
    Column("response_etag", SqlText),
    Column("http_status", Integer, nullable=False),
    CheckConstraint("observed_at >= started_at", name="ck_not_modified_observations_order"),
    CheckConstraint("run_id != original_run_id", name="ck_not_modified_observations_distinct_run"),
    CheckConstraint("http_status = 304", name="ck_not_modified_observations_http_status"),
    CheckConstraint(
        "validator_kind IN ('etag', 'last_modified')",
        name="ck_not_modified_observations_validator_kind",
    ),
)

provenance = Table(
    "provenance",
    metadata,
    _id("provenance_id", primary_key=True),
    Column("artifact_id", SqlText, ForeignKey("raw_artifacts.artifact_id"), nullable=False),
    Column("locator_kind", SqlText, nullable=False),
    Column("locator_json", JsonDocument, nullable=False),
    Column("source_text", SqlText, nullable=False),
    Column("git_sha", SqlText, nullable=False),
    Column("parser_name", SqlText, nullable=False),
    Column("parser_version", SqlText, nullable=False),
    Column("configuration_sha256", SqlText, nullable=False),
    Column("normalization_version", SqlText, nullable=False),
    Column("rule_id", SqlText),
    _enum_check("provenance", "locator_kind", EvidenceKind),
)

source_fingerprints = Table(
    "source_fingerprints",
    metadata,
    _id("fingerprint_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("fetch_run_id", SqlText, ForeignKey("fetch_runs.run_id"), nullable=False),
    Column("artifact_id", SqlText, ForeignKey("raw_artifacts.artifact_id"), nullable=False),
    Column("observed_at", UtcDateTime, nullable=False),
    Column("content_sha256", SqlText, nullable=False),
    Column("structure_sha256", SqlText, nullable=False),
)

source_approvals = Table(
    "source_approvals",
    metadata,
    _id("approval_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("source_url", SqlText, nullable=False),
    Column("decision", SqlText, nullable=False),
    Column("actor", SqlText, nullable=False),
    Column("decided_at", UtcDateTime, nullable=False),
    Column("policy_id", SqlText, nullable=False),
    Column("policy_version", SqlText, nullable=False),
    Column("rationale", SqlText, nullable=False),
    _enum_check("source_approvals", "decision", ReviewVerdict),
)

source_approval_evidence = Table(
    "source_approval_evidence",
    metadata,
    Column("approval_id", SqlText, ForeignKey("source_approvals.approval_id"), primary_key=True),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), primary_key=True),
)

extraction_runs = Table(
    "extraction_runs",
    metadata,
    _id("run_id", primary_key=True),
    _schema_version(),
    Column("fetch_run_id", SqlText, ForeignKey("fetch_runs.run_id"), nullable=False),
    Column("parser_name", SqlText, nullable=False),
    Column("parser_version", SqlText, nullable=False),
    Column("git_sha", SqlText, nullable=False),
    Column("configuration_sha256", SqlText, nullable=False),
    Column("status", SqlText, nullable=False),
    Column("started_at", UtcDateTime, nullable=False),
    Column("finished_at", UtcDateTime, nullable=False),
    Column("candidate_count", Integer, nullable=False),
    Column("error", SqlText),
    CheckConstraint("finished_at >= started_at", name="ck_extraction_runs_order"),
    CheckConstraint("candidate_count >= 0", name="ck_extraction_runs_candidate_count"),
    CheckConstraint(
        "(status = 'succeeded' AND error IS NULL)"
        " OR (status IN ('failed', 'blocked') AND error IS NOT NULL AND candidate_count = 0)",
        name="ck_extraction_runs_error",
    ),
    _enum_check("extraction_runs", "status", RunStatus),
)

extracted_candidates = Table(
    "extracted_candidates",
    metadata,
    _id("candidate_id", primary_key=True),
    _schema_version(),
    Column("extraction_run_id", SqlText, ForeignKey("extraction_runs.run_id"), nullable=False),
    Column("proposed_path", SqlText),
    Column("raw_value", SqlText, nullable=False),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), nullable=False),
)

normalized_offers = Table(
    "normalized_offers",
    metadata,
    _id("offer_id", primary_key=True),
    _schema_version(),
    Column(
        "institution_charter", Integer, ForeignKey("institutions.charter_number"), nullable=False
    ),
    Column("product_id", SqlText, nullable=False),
    Column("source_id", SqlText, nullable=False),
    Column("status", SqlText, nullable=False),
    Column("details_json", JsonDocument, nullable=False),
    Column("scenario_id", SqlText),
    Column("scenario_matches_json", JsonDocument, nullable=False),
    Column("note_rate", DecimalText),
    Column("apr", DecimalText),
    Column("points", DecimalText),
    Column("note_rate_provenance_id", SqlText, ForeignKey("provenance.provenance_id")),
    Column("apr_provenance_id", SqlText, ForeignKey("provenance.provenance_id")),
    Column("points_provenance_id", SqlText, ForeignKey("provenance.provenance_id")),
    Column("conditions_json", JsonDocument, nullable=False),
    Column("tiers_json", JsonDocument, nullable=False),
    Column("headline_is_as_low_as", Integer, nullable=False),
    Column("effective_at", UtcDateTime),
    Column("observed_at", UtcDateTime, nullable=False),
    Column("published_at", UtcDateTime),
    Column("valid_until", UtcDateTime),
    UniqueConstraint("institution_charter", "offer_id", name="uq_offers_institution"),
    ForeignKeyConstraint(
        ["institution_charter", "product_id"],
        ["products.institution_charter", "products.product_id"],
        name="fk_offers_product_same_institution",
    ),
    ForeignKeyConstraint(
        ["institution_charter", "source_id", "product_id"],
        [
            "institution_source_relationships.institution_charter",
            "institution_source_relationships.source_id",
            "institution_source_relationships.product_id",
        ],
        name="fk_offers_relationship_same_institution",
    ),
    CheckConstraint("headline_is_as_low_as IN (0, 1)", name="ck_normalized_offers_as_low_as"),
    CheckConstraint(
        "(note_rate IS NULL) = (note_rate_provenance_id IS NULL)",
        name="ck_normalized_offers_note_rate_provenance",
    ),
    CheckConstraint(
        "(apr IS NULL) = (apr_provenance_id IS NULL)",
        name="ck_normalized_offers_apr_provenance",
    ),
    CheckConstraint(
        "(points IS NULL) = (points_provenance_id IS NULL)",
        name="ck_normalized_offers_points_provenance",
    ),
    _enum_check("normalized_offers", "status", OfferStatus),
    _enum_check("normalized_offers", "scenario_id", CanonicalScenarioId),
)

offer_evidence = Table(
    "offer_evidence",
    metadata,
    Column("offer_id", SqlText, ForeignKey("normalized_offers.offer_id"), primary_key=True),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), primary_key=True),
    Column("role", SqlText, primary_key=True),
    CheckConstraint(
        "role IN ('supporting', 'note_rate', 'apr', 'points', 'field')",
        name="ck_offer_evidence_role",
    ),
)

validation_policies = Table(
    "validation_policies",
    metadata,
    _id("policy_id", primary_key=True),
    _schema_version(),
    Column("policy_version", SqlText, nullable=False),
    Column("required_rules_json", JsonDocument, nullable=False),
    Column("approved_by", SqlText, nullable=False),
    Column("approved_at", UtcDateTime, nullable=False),
)

validation_results = Table(
    "validation_results",
    metadata,
    _id("validation_id", primary_key=True),
    Column("rule_id", SqlText, nullable=False),
    Column("passed", Integer, nullable=False),
    Column("detail", SqlText),
    Column("offer_id", SqlText, ForeignKey("normalized_offers.offer_id"), nullable=False),
    Column("fingerprints_json", JsonDocument, nullable=False),
    Column("evaluated_at", UtcDateTime, nullable=False),
    Column("validator_name", SqlText, nullable=False),
    Column("validator_version", SqlText, nullable=False),
    Column("checked_paths_json", JsonDocument, nullable=False),
    CheckConstraint("passed IN (0, 1)", name="ck_validation_results_passed"),
    CheckConstraint(
        "passed = 1 OR detail IS NOT NULL",
        name="ck_validation_results_failure_detail",
    ),
)

review_decisions = Table(
    "review_decisions",
    metadata,
    _id("review_id", primary_key=True),
    _schema_version(),
    Column("subject", SqlText, nullable=False),
    Column("subject_id", SqlText, nullable=False),
    Column("verdict", SqlText, nullable=False),
    Column("actor", SqlText, nullable=False),
    Column("decided_at", UtcDateTime, nullable=False),
    Column("rationale", SqlText, nullable=False),
    Column("fingerprints_json", JsonDocument),
    Column("reviewed_product_sha256", SqlText),
    CheckConstraint(
        "(subject = 'offer' AND fingerprints_json IS NOT NULL AND reviewed_product_sha256 IS NULL)"
        " OR (subject = 'product' AND fingerprints_json IS NULL"
        " AND reviewed_product_sha256 IS NOT NULL)",
        name="ck_review_decisions_binding",
    ),
    _enum_check("review_decisions", "subject", ReviewSubject),
    _enum_check("review_decisions", "verdict", ReviewVerdict),
)

withdrawal_entries = Table(
    "withdrawal_entries",
    metadata,
    _id("withdrawal_id", primary_key=True),
    _schema_version(),
    Column("scope", SqlText, nullable=False),
    Column("target_id", SqlText, nullable=False),
    Column("actor", SqlText, nullable=False),
    Column("withdrawn_at", UtcDateTime, nullable=False),
    Column("reason", SqlText, nullable=False),
    _enum_check("withdrawal_entries", "scope", WithdrawalScope),
)

withdrawal_lifts = Table(
    "withdrawal_lifts",
    metadata,
    Column(
        "withdrawal_id",
        SqlText,
        ForeignKey("withdrawal_entries.withdrawal_id"),
        primary_key=True,
    ),
    Column("lifted_at", UtcDateTime, nullable=False),
    Column("lifted_by", SqlText, nullable=False),
)

corrections = Table(
    "corrections",
    metadata,
    _id("correction_id", primary_key=True),
    _schema_version(),
    Column("offer_id", SqlText, ForeignKey("normalized_offers.offer_id"), nullable=False),
    Column("field_path", SqlText, nullable=False),
    Column("previous_value", SqlText, nullable=False),
    Column("corrected_value", SqlText, nullable=False),
    Column("actor", SqlText, nullable=False),
    Column("corrected_at", UtcDateTime, nullable=False),
    Column("reason", SqlText, nullable=False),
    Column("fingerprints_json", JsonDocument),
)

correction_evidence = Table(
    "correction_evidence",
    metadata,
    Column("correction_id", SqlText, ForeignKey("corrections.correction_id"), primary_key=True),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), primary_key=True),
)

product_findings = Table(
    "product_findings",
    metadata,
    _id("finding_id", primary_key=True),
    _schema_version(),
    Column("taxonomy_version", SqlText, nullable=False),
    Column(
        "institution_charter", Integer, ForeignKey("institutions.charter_number"), nullable=False
    ),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("product_id", SqlText),
    Column("observed_at", UtcDateTime, nullable=False),
    Column("label", SqlText, nullable=False),
    Column("scope", SqlText, nullable=False),
    Column("family", SqlText),
    Column("out_of_scope_product", SqlText),
    Column("rate_availability", SqlText),
    Column("review_state", SqlText, nullable=False),
    Column("reviewed_by", SqlText),
    Column("reviewed_at", UtcDateTime),
    Column("review_rationale", SqlText),
    ForeignKeyConstraint(
        ["institution_charter", "product_id"],
        ["products.institution_charter", "products.product_id"],
        name="fk_findings_product_same_institution",
    ),
    CheckConstraint(
        "(scope = 'in_scope' AND family IS NOT NULL AND rate_availability IS NOT NULL"
        " AND out_of_scope_product IS NULL)"
        " OR (scope = 'out_of_scope' AND family IS NULL AND product_id IS NULL"
        " AND out_of_scope_product IS NOT NULL AND rate_availability IS NULL)"
        " OR (scope = 'unknown' AND family IS NULL AND product_id IS NULL"
        " AND out_of_scope_product IS NULL"
        " AND (rate_availability IS NULL OR rate_availability = 'unknown'))",
        name="ck_product_findings_scope",
    ),
    CheckConstraint(
        "(review_state = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL"
        " AND review_rationale IS NULL)"
        " OR (review_state IN ('verified', 'rejected') AND reviewed_by IS NOT NULL"
        " AND reviewed_at IS NOT NULL AND review_rationale IS NOT NULL"
        " AND NOT (review_state = 'verified' AND scope = 'unknown'))",
        name="ck_product_findings_review",
    ),
    _enum_check("product_findings", "scope", ProductScope),
    _enum_check("product_findings", "family", ProductFamily),
    _enum_check("product_findings", "out_of_scope_product", OutOfScopeProduct),
    _enum_check("product_findings", "rate_availability", RateAvailability),
    _enum_check("product_findings", "review_state", FindingReviewState),
)

finding_purposes = Table(
    "finding_purposes",
    metadata,
    Column("finding_id", SqlText, ForeignKey("product_findings.finding_id"), primary_key=True),
    Column("purpose", SqlText, primary_key=True),
    _enum_check("finding_purposes", "purpose", LoanPurpose),
)

finding_evidence = Table(
    "finding_evidence",
    metadata,
    Column("finding_id", SqlText, ForeignKey("product_findings.finding_id"), primary_key=True),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), primary_key=True),
)

published_snapshots = Table(
    "published_snapshots",
    metadata,
    _id("snapshot_id", primary_key=True),
    _schema_version(),
    Column("published_at", UtcDateTime, nullable=False),
    Column("published_by", SqlText, nullable=False),
    Column("manifest_sha256", SqlText, nullable=False),
    Column("supersedes", SqlText, ForeignKey("published_snapshots.snapshot_id")),
    CheckConstraint(
        "supersedes IS NULL OR supersedes != snapshot_id", name="ck_snapshots_supersedes"
    ),
)

snapshot_entries = Table(
    "snapshot_entries",
    metadata,
    Column("snapshot_id", SqlText, ForeignKey("published_snapshots.snapshot_id"), primary_key=True),
    Column("offer_id", SqlText, ForeignKey("normalized_offers.offer_id"), primary_key=True),
    Column("structural_sha256", SqlText, nullable=False),
    Column("value_sha256", SqlText, nullable=False),
)

snapshot_reviews = Table(
    "snapshot_reviews",
    metadata,
    Column("snapshot_id", SqlText, ForeignKey("published_snapshots.snapshot_id"), primary_key=True),
    Column("review_id", SqlText, ForeignKey("review_decisions.review_id"), primary_key=True),
)

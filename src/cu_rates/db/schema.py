"""Operational schema for the canonical domain model.

Enum checks use the T010 and T011 values. Successor charters are attributes,
not foreign keys, because a successor may not be in this roster. Canonical
comparison scenarios stay in code. Nested offer details, conditions, tiers,
and scenario matches are JSON text; headline rates and provenance are columns.
A withdrawal lift is its own append-only row so the original entry is not updated.

Fetched bodies live once in ``artifact_blobs`` (content hash, size). Every fetch
writes its own ``artifact_observations`` row, so a second fetch of identical
bytes is a new observation of the same blob.

Rows an audited record cites are append-only, including extraction history and
evidence links. Offer lifecycle is the one exception, and it is narrow: see
the comment on ``normalized_offers``.
"""

from collections import Counter
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

# Primary-key columns of every append-only table, in constraint order. The
# replace guard aborts when this key is already present. SQLite otherwise
# implements REPLACE as a delete plus an insert and skips the delete trigger
# unless recursive_triggers is enabled.
APPEND_ONLY_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "fetch_runs": ("run_id",),
    "not_modified_observations": ("run_id",),
    "review_decisions": ("review_id",),
    "review_evidence": ("review_id", "provenance_id"),
    "withdrawal_entries": ("withdrawal_id",),
    "withdrawal_lifts": ("withdrawal_id",),
    "product_findings": ("finding_id",),
    "finding_evidence": ("finding_id", "provenance_id"),
    "finding_purposes": ("finding_id", "purpose"),
    "artifact_blobs": ("artifact_id",),
    "artifact_observations": ("fetch_run_id", "source_url"),
    "provenance": ("provenance_id",),
    "source_approvals": ("approval_id",),
    "source_approval_evidence": ("approval_id", "provenance_id"),
    "extraction_runs": ("run_id",),
    "extracted_candidates": ("candidate_id",),
    "offer_evidence": ("offer_id", "provenance_id", "role", "field_path"),
    "validation_results": ("validation_id",),
    "corrections": ("correction_id",),
    "correction_evidence": ("correction_id", "provenance_id"),
    "published_snapshots": ("snapshot_id",),
    "snapshot_entries": ("snapshot_id", "offer_id"),
    "snapshot_reviews": ("snapshot_id", "review_id"),
}
APPEND_ONLY_TABLES = frozenset(APPEND_ONLY_PRIMARY_KEYS)
# Offers may change lifecycle columns. They still cannot be replaced, because
# REPLACE would delete the cited row and insert a different one.
REPLACE_GUARDED_TABLES = APPEND_ONLY_TABLES | frozenset({"normalized_offers"})
# ``published_at`` may be filled once; ``status`` may move. Nothing else may.
OFFER_MUTABLE_COLUMNS = frozenset({"status", "published_at"})

ENUM_CHECKS: list[tuple[str, str, type[StrEnum]]] = []


# Text that canonical_decimal never emits for a finite Decimal, plus the
# spellings SQLite would store if a writer bypassed the type decorator.
_NONFINITE_DECIMALS = (
    "'nan', '-nan', '+nan', 'inf', '-inf', '+inf', "
    "'infinity', '-infinity', '+infinity', 'snan', '-snan', '+snan'"
)


def _finite_decimal(table: str, column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR lower({column}) NOT IN ({_NONFINITE_DECIMALS})",
        name=f"ck_{table}_{column}_finite",
    )


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
    UniqueConstraint("run_id", "source_id", name="uq_fetch_runs_source"),
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

# One row per distinct body. ``artifact_id`` is the SHA-256 of the bytes and
# ``byte_length`` is their size. A later fetch of the same bytes inserts an
# observation, not another blob.
artifact_blobs = Table(
    "artifact_blobs",
    metadata,
    _id("artifact_id", primary_key=True),
    Column("byte_length", Integer, nullable=False),
    CheckConstraint("byte_length >= 0", name="ck_artifact_blobs_byte_length"),
)

# One row per fetch of a URL, including a repeat of bytes already stored.
# ``(fetch_run_id, artifact_id)`` is unique so a 304 can cite the run and body
# that belong together. One run therefore records each body once.
artifact_observations = Table(
    "artifact_observations",
    metadata,
    Column("fetch_run_id", SqlText, primary_key=True),
    Column("source_url", SqlText, primary_key=True),
    _schema_version(),
    Column("artifact_id", SqlText, ForeignKey("artifact_blobs.artifact_id"), nullable=False),
    Column("media_type", SqlText, nullable=False),
    Column("fetched_at", UtcDateTime, nullable=False),
    Column("http_status", Integer, nullable=False),
    UniqueConstraint("fetch_run_id", "artifact_id", name="uq_observations_run_artifact"),
    CheckConstraint(
        "http_status BETWEEN 100 AND 599",
        name="ck_artifact_observations_http_status",
    ),
    ForeignKeyConstraint(
        ["fetch_run_id"],
        ["fetch_runs.run_id"],
        name="fk_observations_fetch_run",
    ),
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
    Column("original_run_id", SqlText, nullable=False),
    Column("original_artifact_id", SqlText, nullable=False),
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
    ForeignKeyConstraint(
        ["original_run_id", "source_id"],
        ["fetch_runs.run_id", "fetch_runs.source_id"],
        name="fk_not_modified_same_source",
    ),
    ForeignKeyConstraint(
        ["original_run_id", "original_artifact_id"],
        ["artifact_observations.fetch_run_id", "artifact_observations.artifact_id"],
        name="fk_not_modified_run_artifact",
    ),
)

provenance = Table(
    "provenance",
    metadata,
    _id("provenance_id", primary_key=True),
    Column(
        "institution_charter",
        Integer,
        ForeignKey("institutions.charter_number"),
        nullable=False,
    ),
    Column("artifact_id", SqlText, ForeignKey("artifact_blobs.artifact_id"), nullable=False),
    Column("locator_kind", SqlText, nullable=False),
    Column("locator_json", JsonDocument, nullable=False),
    Column("source_text", SqlText, nullable=False),
    Column("git_sha", SqlText, nullable=False),
    Column("parser_name", SqlText, nullable=False),
    Column("parser_version", SqlText, nullable=False),
    Column("configuration_sha256", SqlText, nullable=False),
    Column("normalization_version", SqlText, nullable=False),
    Column("rule_id", SqlText),
    UniqueConstraint("institution_charter", "provenance_id", name="uq_provenance_institution"),
    _enum_check("provenance", "locator_kind", EvidenceKind),
)

source_fingerprints = Table(
    "source_fingerprints",
    metadata,
    _id("fingerprint_id", primary_key=True),
    _schema_version(),
    Column("source_id", SqlText, ForeignKey("rate_sources.source_id"), nullable=False),
    Column("fetch_run_id", SqlText, ForeignKey("fetch_runs.run_id"), nullable=False),
    Column("artifact_id", SqlText, ForeignKey("artifact_blobs.artifact_id"), nullable=False),
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

# Lifecycle status is not a new offer row. Evidence, reviews, validations,
# corrections, and snapshots cite ``offer_id``, so a later status keeps that
# identity. UPDATE may change only ``status`` and ``published_at``, and
# ``published_at`` may be filled in once, never rewritten or cleared. DELETE
# and REPLACE are rejected. Withdrawal and publication audit detail stay in
# ``withdrawal_entries`` and ``published_snapshots``; this row is the current
# lifecycle state, not a history of previous states.
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
    # Published and withdrawn offers must name when they were published.
    # Superseded may still carry published_at, but a pre-publication status
    # cannot. valid_until, when stated, is strictly after the observation.
    CheckConstraint(
        "status NOT IN ('published', 'withdrawn') OR published_at IS NOT NULL",
        name="ck_normalized_offers_published_at",
    ),
    CheckConstraint(
        "published_at IS NULL OR status IN ('published', 'withdrawn', 'superseded')",
        name="ck_normalized_offers_published_at_status",
    ),
    CheckConstraint(
        "valid_until IS NULL OR valid_until > observed_at",
        name="ck_normalized_offers_valid_until",
    ),
    _finite_decimal("normalized_offers", "note_rate"),
    _finite_decimal("normalized_offers", "apr"),
    _finite_decimal("normalized_offers", "points"),
    _enum_check("normalized_offers", "status", OfferStatus),
    _enum_check("normalized_offers", "scenario_id", CanonicalScenarioId),
)

# ``field_path`` is part of the key. It is empty for rate and supporting rows
# because a NULL primary-key column would not keep those rows unique. Field
# evidence stores the semantic path (``details.term_months`` and the rest).
offer_evidence = Table(
    "offer_evidence",
    metadata,
    Column("offer_id", SqlText, primary_key=True),
    Column("provenance_id", SqlText, primary_key=True),
    Column("role", SqlText, primary_key=True),
    Column("field_path", SqlText, primary_key=True),
    Column("institution_charter", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False),
    CheckConstraint(
        "role IN ('supporting', 'note_rate', 'apr', 'points', 'field')",
        name="ck_offer_evidence_role",
    ),
    CheckConstraint(
        "(role = 'field' AND field_path != '') OR (role != 'field' AND field_path = '')",
        name="ck_offer_evidence_field_path",
    ),
    CheckConstraint("ordinal >= 0", name="ck_offer_evidence_ordinal"),
    ForeignKeyConstraint(
        ["institution_charter", "offer_id"],
        ["normalized_offers.institution_charter", "normalized_offers.offer_id"],
        name="fk_offer_evidence_same_institution",
    ),
    ForeignKeyConstraint(
        ["institution_charter", "provenance_id"],
        ["provenance.institution_charter", "provenance.provenance_id"],
        name="fk_offer_evidence_provenance_institution",
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

# Provenance cited by a review decision. The decision row alone does not keep it.
review_evidence = Table(
    "review_evidence",
    metadata,
    Column("review_id", SqlText, ForeignKey("review_decisions.review_id"), primary_key=True),
    Column("provenance_id", SqlText, ForeignKey("provenance.provenance_id"), primary_key=True),
    Column("ordinal", Integer, nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_review_evidence_ordinal"),
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


def offer_frozen_columns() -> tuple[str, ...]:
    """Columns on ``normalized_offers`` that a lifecycle update must not touch."""
    return tuple(
        column.name for column in normalized_offers.c if column.name not in OFFER_MUTABLE_COLUMNS
    )


def _append_only_message(table: str) -> str:
    return f"{table} is append-only"


def schema_triggers() -> tuple[tuple[str, str], ...]:
    """``(name, CREATE TRIGGER statement)`` for the operational database.

    Update and delete triggers reject ordinary mutation. The insert trigger
    rejects ``REPLACE`` even when ``recursive_triggers`` is off, because the
    old primary key is still visible to ``BEFORE INSERT``.
    """
    statements: list[tuple[str, str]] = []
    for table_name, keys in APPEND_ONLY_PRIMARY_KEYS.items():
        message = _append_only_message(table_name)
        for action in ("update", "delete"):
            name = f"{table_name}_no_{action}"
            statements.append(
                (
                    name,
                    f"CREATE TRIGGER {name} BEFORE {action.upper()} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
                )
            )
        match = " AND ".join(f"{column} = NEW.{column}" for column in keys)
        replace_name = f"{table_name}_no_replace"
        statements.append(
            (
                replace_name,
                f"CREATE TRIGGER {replace_name} BEFORE INSERT ON {table_name} "
                f"WHEN EXISTS (SELECT 1 FROM {table_name} WHERE {match}) "
                f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            )
        )
    frozen = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in offer_frozen_columns())
    published = "OLD.published_at IS NOT NULL AND OLD.published_at IS NOT NEW.published_at"
    lifecycle = "normalized_offers_lifecycle_only"
    statements.append(
        (
            lifecycle,
            "CREATE TRIGGER normalized_offers_lifecycle_only BEFORE UPDATE ON normalized_offers "
            f"WHEN {frozen} OR ({published}) "
            "BEGIN SELECT RAISE(ABORT, 'normalized_offers allows only a lifecycle status change'); "
            "END",
        )
    )
    for action, timing in (("delete", "DELETE"), ("replace", "INSERT")):
        name = f"normalized_offers_no_{action}"
        if action == "delete":
            body = (
                f"CREATE TRIGGER {name} BEFORE {timing} ON normalized_offers "
                "BEGIN SELECT RAISE(ABORT, 'normalized_offers is append-only'); END"
            )
        else:
            body = (
                f"CREATE TRIGGER {name} BEFORE {timing} ON normalized_offers "
                "WHEN EXISTS (SELECT 1 FROM normalized_offers WHERE offer_id = NEW.offer_id) "
                "BEGIN SELECT RAISE(ABORT, 'normalized_offers is append-only'); END"
            )
        statements.append((name, body))
    return tuple(statements)


def expected_trigger_names() -> frozenset[str]:
    return frozenset(name for name, _statement in schema_triggers())


def expected_check_names() -> Counter[tuple[str, str]]:
    """Multiset of ``(table, check name)`` declared on the metadata."""
    counts: Counter[tuple[str, str]] = Counter()
    for table in metadata.tables.values():
        for constraint in table.constraints:
            name = constraint.name
            if isinstance(constraint, CheckConstraint) and isinstance(name, str):
                counts[(table.name, name)] += 1
    return counts

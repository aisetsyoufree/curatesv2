"""Operational schema: migrations, provenance, isolation, foreign keys, rollback."""

import hashlib
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, StatementError

from cu_rates.contracts import (
    CanonicalScenarioId,
    FetchMethod,
    FetchProfile,
    FieldEvidence,
    NormalizedOffer,
    Provenance,
    ReviewDecision,
    SourcedPoints,
    SourcedRate,
    SourceRelationshipKind,
)
from cu_rates.db import (
    ENUM_CHECKS,
    AppendOnlyError,
    current_revision,
    downgrade_database,
    metadata,
    open_operational_database,
    script_head,
    upgrade_database,
)
from cu_rates.db.migrate import VERSIONS_DIR, _config
from cu_rates.db.records import load_normalized_offer, load_review_decision, persist_chain
from cu_rates.db.records import persist_normalized_offer as write_offer
from cu_rates.db.schema import (
    artifact_blobs,
    artifact_observations,
    expected_check_names,
    expected_trigger_names,
    extracted_candidates,
    extraction_runs,
    fetch_profiles,
    fetch_runs,
    institution_source_relationships,
    institutions,
    membership_rules,
    normalized_offers,
    not_modified_observations,
    offer_evidence,
    product_findings,
    products,
    provenance,
    rate_sources,
    review_decisions,
    review_evidence,
    service_providers,
    validation_results,
    withdrawal_entries,
)
from tests.contracts import factories as make

GIT = "cd" * 20
SHA = "ab" * 32
WHEN = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
NORTH = 1000001
SOUTH = 1000002


def _database(tmp_path: Path) -> Path:
    return tmp_path / "operational.sqlite"


def _migrate(path: Path) -> None:
    upgrade_database(path)


def _seed(path: Path) -> None:
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(institutions),
            [
                {
                    "charter_number": NORTH,
                    "schema_version": "1.0",
                    "legal_name": "Example North Charter",
                    "status": "active",
                    "successor_charter_number": None,
                    "charter_verified": 1,
                },
                {
                    "charter_number": SOUTH,
                    "schema_version": "1.0",
                    "legal_name": "Example South Charter",
                    "status": "active",
                    "successor_charter_number": None,
                    "charter_verified": 1,
                },
            ],
        )
        conn.execute(
            insert(service_providers),
            {
                "provider_id": "provider-example",
                "schema_version": "1.0",
                "name": "Example Rate Platform",
                "kind": "loan_platform",
            },
        )
        conn.execute(
            insert(rate_sources),
            [
                {
                    "source_id": "source-north",
                    "schema_version": "1.0",
                    "url": "https://north.example/rates",
                    "evidence_kind": "html",
                    "description": "synthetic north page",
                },
                {
                    "source_id": "source-south",
                    "schema_version": "1.0",
                    "url": "https://south.example/rates",
                    "evidence_kind": "html",
                    "description": "synthetic south page",
                },
            ],
        )
        conn.execute(
            insert(products),
            [
                _product("product-north", NORTH, "Example North Purchase"),
                _product("product-south", SOUTH, "Example South Purchase"),
            ],
        )
        conn.execute(
            insert(institution_source_relationships),
            [
                _relationship("rel-north", NORTH, "source-north", "product-north"),
                _relationship("rel-south", SOUTH, "source-south", "product-south"),
            ],
        )
        conn.execute(
            insert(fetch_profiles),
            {
                "profile_id": "profile-north",
                "schema_version": "1.0",
                "source_id": "source-north",
                "method": "http_get",
                "min_interval_seconds": 5,
                "timeout_seconds": 30,
                "respect_robots": 1,
            },
        )
        conn.execute(
            insert(fetch_runs),
            {
                "run_id": "run-north",
                "schema_version": "1.0",
                "source_id": "source-north",
                "profile_id": "profile-north",
                "profile_sha256": SHA,
                "status": "succeeded",
                "started_at": WHEN,
                "finished_at": WHEN,
                "git_sha": GIT,
                "error": None,
                "validator_kind": None,
                "validator_value": None,
            },
        )
        conn.execute(
            insert(artifact_blobs),
            {"artifact_id": SHA, "byte_length": 12},
        )
        conn.execute(
            insert(artifact_observations),
            {
                "fetch_run_id": "run-north",
                "source_url": "https://north.example/rates",
                "schema_version": "1.0",
                "artifact_id": SHA,
                "media_type": "text/html; charset=utf-8",
                "fetched_at": WHEN,
                "http_status": 200,
            },
        )
        conn.execute(
            insert(provenance),
            {
                "provenance_id": "prov-note",
                "institution_charter": NORTH,
                "artifact_id": SHA,
                "locator_kind": "html",
                "locator_json": {
                    "kind": "html",
                    "selector_type": "css",
                    "selector": "#row",
                    "normalized_text_sha256": SHA,
                },
                "source_text": "6.125%",
                "git_sha": GIT,
                "parser_name": "example-parser",
                "parser_version": "0",
                "configuration_sha256": SHA,
                "normalization_version": "1",
                "rule_id": None,
            },
        )
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-north",
                "schema_version": "1.0",
                "institution_charter": NORTH,
                "product_id": "product-north",
                "source_id": "source-north",
                "status": "candidate",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": "purchase_30y_fixed",
                "scenario_matches_json": [],
                "note_rate": Decimal("6.125"),
                "apr": None,
                "points": Decimal("0"),
                "note_rate_provenance_id": "prov-note",
                "apr_provenance_id": None,
                "points_provenance_id": "prov-note",
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": WHEN,
                "published_at": None,
                "valid_until": None,
            },
        )


def _product(product_id: str, charter: int, name: str) -> dict[str, object]:
    return {
        "product_id": product_id,
        "schema_version": "1.0",
        "institution_charter": charter,
        "family": "mortgage",
        "name": name,
        "lifecycle": "active",
        "rate_availability": "public_rate",
        "conforming_status": "conforming",
        "government_guaranty": "none",
        "notes_json": [],
    }


def _relationship(
    relationship_id: str, charter: int, source_id: str, product_id: str
) -> dict[str, object]:
    return {
        "relationship_id": relationship_id,
        "schema_version": "1.0",
        "institution_charter": charter,
        "source_id": source_id,
        "product_id": product_id,
        "provider_id": "provider-example",
        "kind": "provider_hosted",
        "approved_by": "example-reviewer",
        "approved_at": WHEN,
    }


def test_fresh_database_migrates_downgrades_and_verifies_revision(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        names = set(inspect(conn).get_table_names())
    assert "institutions" in names
    assert "alembic_version" in names
    assert current_revision(path) == script_head()
    script = ScriptDirectory.from_config(_config(path))
    assert script.get_current_head() == script_head()

    downgrade_database(path)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert "institutions" not in names
    assert current_revision(path) is None

    _migrate(path)
    assert current_revision(path) == script_head()
    with engine.connect() as conn:
        assert "products" in inspect(conn).get_table_names()


def test_provenance_and_decimals_round_trip(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        offer = conn.execute(
            select(normalized_offers).where(normalized_offers.c.offer_id == "offer-north")
        ).one()
        stored = conn.execute(
            text("SELECT typeof(note_rate), typeof(points) FROM normalized_offers")
        ).one()
        evidence = conn.execute(
            select(provenance).where(provenance.c.provenance_id == "prov-note")
        ).one()
        fetched = conn.execute(select(artifact_observations.c.fetched_at)).scalar_one()
    assert stored == ("text", "text")
    assert isinstance(offer.note_rate, Decimal)
    assert isinstance(offer.points, Decimal)
    assert offer.note_rate == Decimal("6.125")
    assert offer.points == Decimal("0")
    assert evidence.source_text == "6.125%"
    assert evidence.locator_kind == "html"
    assert evidence.locator_json["selector"] == "#row"
    assert evidence.artifact_id == SHA
    assert fetched == WHEN
    with pytest.raises(StatementError, match="binary floats"), engine.begin() as conn:
        conn.execute(
            update(normalized_offers)
            .where(normalized_offers.c.offer_id == "offer-north")
            .values(note_rate=6.125)
        )


def test_shared_provider_does_not_leak_across_institutions(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    query = text(
        """
        SELECT p.product_id
        FROM products AS p
        JOIN institution_source_relationships AS r
          ON r.institution_charter = p.institution_charter
         AND r.product_id = p.product_id
        WHERE r.provider_id = :provider
          AND p.institution_charter = :charter
        ORDER BY p.product_id
        """
    )
    with engine.connect() as conn:
        north = (
            conn.execute(query, {"provider": "provider-example", "charter": NORTH}).scalars().all()
        )
        south = (
            conn.execute(query, {"provider": "provider-example", "charter": SOUTH}).scalars().all()
        )
        shared = (
            conn.execute(
                select(institution_source_relationships.c.institution_charter).where(
                    institution_source_relationships.c.provider_id == "provider-example"
                )
            )
            .scalars()
            .all()
        )
    assert north == ["product-north"]
    assert south == ["product-south"]
    assert sorted(shared) == [NORTH, SOUTH]


def test_foreign_keys_reject_invalid_and_cross_institution_bindings(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    with engine.connect() as first, engine.connect() as second:
        assert first.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert second.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(membership_rules),
            {
                "membership_rule_id": "rule-missing",
                "schema_version": "1.0",
                "institution_charter": 424242,
                "kind": "open",
                "description": "missing institution",
                "source_url": None,
            },
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(institution_source_relationships),
            _relationship("rel-cross", SOUTH, "source-south", "product-north"),
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(normalized_offers),
            {
                "offer_id": "offer-cross",
                "schema_version": "1.0",
                "institution_charter": SOUTH,
                "product_id": "product-north",
                "source_id": "source-north",
                "status": "candidate",
                "details_json": {"kind": "purchase_mortgage"},
                "scenario_id": None,
                "scenario_matches_json": [],
                "note_rate": None,
                "apr": None,
                "points": None,
                "note_rate_provenance_id": None,
                "apr_provenance_id": None,
                "points_provenance_id": None,
                "conditions_json": [],
                "tiers_json": [],
                "headline_is_as_low_as": 0,
                "effective_at": None,
                "observed_at": WHEN,
                "published_at": None,
                "valid_until": None,
            },
        )
    with engine.connect() as conn:
        offers = conn.execute(select(normalized_offers.c.offer_id)).scalars().all()
    assert offers == ["offer-north"]


def test_failed_migration_leaves_no_partial_schema_or_revision(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    failing = tmp_path / "t012_fail.py"
    failing.write_text(
        "\n".join(
            [
                '"""Deliberate failure for rollback coverage."""',
                'revision = "t012_fail"',
                'down_revision = "t012_initial"',
                "branch_labels = None",
                "depends_on = None",
                "from alembic import op",
                "import sqlalchemy as sa",
                "def upgrade():",
                "    op.create_table(",
                '        "partial_should_not_exist",',
                "        sa.Column('id', sa.Integer, primary_key=True),",
                "    )",
                '    raise RuntimeError("deliberate migration failure")',
                "def downgrade():",
                '    op.drop_table("partial_should_not_exist")',
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="deliberate migration failure"):
        upgrade_database(path, "t012_fail", version_locations=(VERSIONS_DIR, tmp_path))
    engine = open_operational_database(path)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert "partial_should_not_exist" not in names
    assert "institutions" in names
    assert current_revision(path) == script_head()


def test_enum_checks_use_contract_values(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        checker = inspect(conn)
        for table_name, column, enum_cls in ENUM_CHECKS:
            sql = " ".join(item["sqltext"] for item in checker.get_check_constraints(table_name))
            assert column in sql
            for member in enum_cls:
                assert f"'{member.value}'" in sql
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(institutions),
                {
                    "charter_number": 1000003,
                    "schema_version": "1.0",
                    "legal_name": "Example Rejected Charter",
                    "status": "not-a-status",
                    "successor_charter_number": None,
                    "charter_verified": 1,
                },
            )
            conn.commit()


def test_append_only_history_rejects_update_and_delete(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(review_decisions),
            {
                "review_id": "review-1",
                "schema_version": "1.0",
                "subject": "product",
                "subject_id": "product-north",
                "verdict": "approved",
                "actor": "example-reviewer",
                "decided_at": WHEN,
                "rationale": "synthetic review",
                "fingerprints_json": None,
                "reviewed_product_sha256": SHA,
            },
        )
        conn.execute(
            insert(withdrawal_entries),
            {
                "withdrawal_id": "withdrawal-1",
                "schema_version": "1.0",
                "scope": "offer",
                "target_id": "offer-north",
                "actor": "example-reviewer",
                "withdrawn_at": WHEN,
                "reason": "synthetic withdrawal",
            },
        )
        conn.execute(
            insert(product_findings),
            {
                "finding_id": "finding-1",
                "schema_version": "1.0",
                "taxonomy_version": "1.0",
                "institution_charter": NORTH,
                "source_id": "source-north",
                "product_id": None,
                "observed_at": WHEN,
                "label": "page did not identify a product",
                "scope": "unknown",
                "family": None,
                "out_of_scope_product": None,
                "rate_availability": None,
                "review_state": "proposed",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_rationale": None,
            },
        )
    statements = (
        "UPDATE fetch_runs SET error = 'changed' WHERE run_id = 'run-north'",
        "DELETE FROM fetch_runs WHERE run_id = 'run-north'",
        "UPDATE review_decisions SET rationale = 'changed' WHERE review_id = 'review-1'",
        "DELETE FROM review_decisions WHERE review_id = 'review-1'",
        "UPDATE withdrawal_entries SET reason = 'changed' WHERE withdrawal_id = 'withdrawal-1'",
        "DELETE FROM withdrawal_entries WHERE withdrawal_id = 'withdrawal-1'",
        "UPDATE product_findings SET label = 'changed' WHERE finding_id = 'finding-1'",
        "DELETE FROM product_findings WHERE finding_id = 'finding-1'",
    )
    for statement in statements:
        with pytest.raises(AppendOnlyError), engine.begin() as conn:
            conn.execute(text(statement))
    with engine.connect() as conn:
        assert conn.execute(select(fetch_runs.c.run_id)).scalar_one() == "run-north"
        assert conn.execute(select(review_decisions.c.rationale)).scalar_one() == "synthetic review"
        assert (
            conn.execute(select(withdrawal_entries.c.reason)).scalar_one() == "synthetic withdrawal"
        )
        assert (
            conn.execute(select(product_findings.c.label)).scalar_one()
            == "page did not identify a product"
        )


def test_migrated_database_matches_live_metadata(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diff = compare_metadata(context, metadata)
    assert diff == []


def test_later_migration_upgrades_from_initial(tmp_path: Path) -> None:
    path = _database(tmp_path)
    upgrade_database(path, "t012_initial")
    assert current_revision(path) == "t012_initial"
    later = tmp_path / "t012_later.py"
    later.write_text(
        "\n".join(
            [
                '"""A later revision that adds one table."""',
                'revision = "t012_later"',
                'down_revision = "t012_initial"',
                "branch_labels = None",
                "depends_on = None",
                "from alembic import op",
                "import sqlalchemy as sa",
                "def upgrade():",
                "    op.create_table(",
                '        "later_added",',
                "        sa.Column('id', sa.Text(), primary_key=True),",
                "    )",
                "def downgrade():",
                '    op.drop_table("later_added")',
                "",
            ]
        ),
        encoding="utf-8",
    )
    upgrade_database(path, "t012_later", version_locations=(VERSIONS_DIR, tmp_path))
    engine = open_operational_database(path)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert "later_added" in names
    assert "institutions" in names
    assert current_revision(path) == "t012_later"


def _replace_sql(raw: sqlite3.Connection, statement: str) -> None:
    row = raw.execute("SELECT * FROM fetch_runs WHERE run_id = 'run-north'").fetchone()
    assert row is not None
    columns = list(row.keys())
    values = ["f" * 40 if column == "git_sha" else row[column] for column in columns]
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    raw.execute(statement.format(names=names, marks=marks), values)


def test_replace_cannot_overwrite_append_only_history(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 1
    for statement in (
        "INSERT OR REPLACE INTO fetch_runs (run_id) VALUES ('run-north')",
        "REPLACE INTO \"fetch_runs\" (run_id) VALUES ('run-north')",
    ):
        with pytest.raises(AppendOnlyError), engine.begin() as conn:
            conn.execute(text(statement))
    with engine.connect() as conn:
        assert conn.execute(select(fetch_runs.c.git_sha)).scalar_one() == GIT
    engine.dispose()

    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    for statement in (
        "INSERT OR REPLACE INTO fetch_runs ({names}) VALUES ({marks})",
        "REPLACE INTO fetch_runs ({names}) VALUES ({marks})",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _replace_sql(raw, statement)
        raw.rollback()
    kept = raw.execute("SELECT git_sha FROM fetch_runs WHERE run_id = 'run-north'").fetchone()
    assert kept is not None
    assert kept[0] == GIT
    raw.close()


def test_cited_rows_are_immutable_and_offer_status_is_narrow(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(extraction_runs),
            {
                "run_id": "extract-1",
                "schema_version": "1.0",
                "fetch_run_id": "run-north",
                "parser_name": "example-parser",
                "parser_version": "0",
                "git_sha": GIT,
                "configuration_sha256": SHA,
                "status": "succeeded",
                "started_at": WHEN,
                "finished_at": WHEN,
                "candidate_count": 1,
                "error": None,
            },
        )
        conn.execute(
            insert(extracted_candidates),
            {
                "candidate_id": "cand-1",
                "schema_version": "1.0",
                "extraction_run_id": "extract-1",
                "proposed_path": "note_rate",
                "raw_value": "6.125%",
                "provenance_id": "prov-note",
            },
        )
        conn.execute(
            insert(offer_evidence),
            {
                "offer_id": "offer-north",
                "provenance_id": "prov-note",
                "role": "note_rate",
                "field_path": "",
                "institution_charter": NORTH,
                "ordinal": 0,
            },
        )
        conn.execute(
            insert(validation_results),
            {
                "validation_id": "validation-1",
                "rule_id": "rule.rate-range",
                "passed": 1,
                "detail": None,
                "offer_id": "offer-north",
                "fingerprints_json": {"algorithm": "fp1", "structural": SHA, "value": SHA},
                "evaluated_at": WHEN,
                "validator_name": "example-validator",
                "validator_version": "0",
                "checked_paths_json": ["note_rate"],
            },
        )
        conn.execute(
            insert(review_decisions),
            {
                "review_id": "review-cited",
                "schema_version": "1.0",
                "subject": "product",
                "subject_id": "product-north",
                "verdict": "approved",
                "actor": "example-reviewer",
                "decided_at": WHEN,
                "rationale": "synthetic review",
                "fingerprints_json": None,
                "reviewed_product_sha256": SHA,
            },
        )
        conn.execute(
            insert(review_evidence),
            {"review_id": "review-cited", "provenance_id": "prov-note", "ordinal": 0},
        )
    statements = (
        "UPDATE extraction_runs SET candidate_count = 0 WHERE run_id = 'extract-1'",
        "DELETE FROM extraction_runs WHERE run_id = 'extract-1'",
        "UPDATE extracted_candidates SET raw_value = 'changed' WHERE candidate_id = 'cand-1'",
        "DELETE FROM extracted_candidates WHERE candidate_id = 'cand-1'",
        "UPDATE offer_evidence SET ordinal = 9 WHERE offer_id = 'offer-north'",
        "DELETE FROM offer_evidence WHERE offer_id = 'offer-north'",
        "UPDATE validation_results SET detail = 'changed' WHERE validation_id = 'validation-1'",
        "DELETE FROM validation_results WHERE validation_id = 'validation-1'",
        "UPDATE review_evidence SET ordinal = 3 WHERE review_id = 'review-cited'",
        "DELETE FROM review_evidence WHERE review_id = 'review-cited'",
    )
    for statement in statements:
        with pytest.raises(AppendOnlyError), engine.begin() as conn:
            conn.execute(text(statement))
    engine.dispose()
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE extraction_runs SET candidate_count = 0 WHERE run_id = 'extract-1'")
    raw.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM offer_evidence WHERE offer_id = 'offer-north'")
    raw.rollback()
    assert raw.execute("SELECT candidate_count FROM extraction_runs").fetchone()[0] == 1
    assert raw.execute("SELECT COUNT(*) FROM offer_evidence").fetchone()[0] == 1
    raw.close()

    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            update(normalized_offers)
            .where(normalized_offers.c.offer_id == "offer-north")
            .values(status="approved")
        )
    with engine.connect() as conn:
        assert conn.execute(select(normalized_offers.c.status)).scalar_one() == "approved"
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(normalized_offers)
            .where(normalized_offers.c.offer_id == "offer-north")
            .values(note_rate=Decimal("6.500"))
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("DELETE FROM normalized_offers WHERE offer_id = 'offer-north'"))
    published = WHEN + timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(
            update(normalized_offers)
            .where(normalized_offers.c.offer_id == "offer-north")
            .values(status="published", published_at=published)
        )
    with engine.connect() as conn:
        row = conn.execute(
            select(normalized_offers.c.status, normalized_offers.c.published_at)
        ).one()
    assert row == ("published", published)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            update(normalized_offers)
            .where(normalized_offers.c.offer_id == "offer-north")
            .values(published_at=published + timedelta(days=1))
        )


def test_identical_bytes_yield_two_observations_and_one_blob(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    body = b"<html>same rates</html>"
    digest = hashlib.sha256(body).hexdigest()
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(fetch_profiles),
            {
                "profile_id": "profile-south",
                "schema_version": "1.0",
                "source_id": "source-south",
                "method": "http_get",
                "min_interval_seconds": 5,
                "timeout_seconds": 30,
                "respect_robots": 1,
            },
        )
        conn.execute(
            insert(fetch_runs),
            {
                "run_id": "run-south",
                "schema_version": "1.0",
                "source_id": "source-south",
                "profile_id": "profile-south",
                "profile_sha256": SHA,
                "status": "succeeded",
                "started_at": WHEN,
                "finished_at": WHEN,
                "git_sha": GIT,
                "error": None,
                "validator_kind": None,
                "validator_value": None,
            },
        )
        conn.execute(insert(artifact_blobs), {"artifact_id": digest, "byte_length": len(body)})
        observations = [
            {
                "fetch_run_id": run_id,
                "source_url": url,
                "schema_version": "1.0",
                "artifact_id": digest,
                "media_type": "text/html; charset=utf-8",
                "fetched_at": WHEN + timedelta(minutes=offset),
                "http_status": 200,
            }
            for run_id, url, offset in (
                ("run-north", "https://north.example/rates-again", 1),
                ("run-south", "https://south.example/rates", 2),
            )
        ]
        conn.execute(insert(artifact_observations), observations)
        with pytest.raises(IntegrityError):
            conn.execute(insert(artifact_blobs), {"artifact_id": digest, "byte_length": len(body)})
        blob_count = conn.execute(
            select(artifact_blobs.c.artifact_id).where(artifact_blobs.c.artifact_id == digest)
        ).all()
        seen = (
            conn.execute(
                select(artifact_observations.c.fetch_run_id)
                .where(artifact_observations.c.artifact_id == digest)
                .order_by(artifact_observations.c.fetch_run_id)
            )
            .scalars()
            .all()
        )
    assert len(blob_count) == 1
    assert seen == ["run-north", "run-south"]


def _provider_chain(prefix: str, charter: int, body: bytes) -> dict[str, object]:
    artifact_id = hashlib.sha256(body).hexdigest()
    source_id = f"src-{prefix}"
    product_id = f"prod-{prefix}"
    run_id = f"fetch-{prefix}"
    url = f"https://{prefix}.example/rates"

    def prov(quote: str) -> Provenance:
        return make.provenance(quote, artifact_id=artifact_id)

    scenario_matches = tuple(
        match.model_copy(update={"provenance": prov(match.source_text or "")})
        for match in make.matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    )
    draft = make.purchase_offer(
        offer_id=f"offer-{prefix}",
        institution_charter=charter,
        product_id=product_id,
        source_id=source_id,
        note_rate=SourcedRate(value=Decimal("6.125"), provenance=prov(f"{prefix} note")),
        apr=SourcedRate(value=Decimal("6.250"), provenance=prov(f"{prefix} apr")),
        points=SourcedPoints(value=Decimal("0"), provenance=prov(f"{prefix} points")),
        supporting_evidence=(prov(f"{prefix} supporting"),),
        scenario_matches=scenario_matches,
        field_provenance=(),
    )
    offer = make.purchase_offer(
        offer_id=draft.offer_id,
        institution_charter=charter,
        product_id=product_id,
        source_id=source_id,
        note_rate=draft.note_rate,
        apr=draft.apr,
        points=draft.points,
        supporting_evidence=draft.supporting_evidence,
        scenario_matches=draft.scenario_matches,
        field_provenance=tuple(
            FieldEvidence(path=field_path, provenance=prov(field_path))
            for field_path in sorted(draft.material_paths())
        ),
    )
    decided = make.review(offer)
    review = ReviewDecision(
        schema_version=decided.schema_version,
        review_id=f"review-{prefix}",
        subject=decided.subject,
        subject_id=decided.subject_id,
        verdict=decided.verdict,
        actor=decided.actor,
        decided_at=decided.decided_at,
        rationale=decided.rationale,
        evidence=(prov(f"{prefix} reviewed the rate table"),),
        reviewed_fingerprints=decided.reviewed_fingerprints,
    )
    from cu_rates.contracts import ProviderKind, ServiceProvider

    return {
        "institution": make.institution(
            charter_number=charter, legal_name=f"Example {prefix.title()} Charter"
        ),
        "provider": ServiceProvider(
            schema_version=make.SV,
            provider_id="provider-example",
            name="Example Rate Platform",
            kind=ProviderKind.LOAN_PLATFORM,
        ),
        "source": make.rate_source(source_id=source_id, url=url, description=f"{prefix} page"),
        "product": make.product(
            product_id=product_id,
            institution_charter=charter,
            name=f"Example {prefix.title()} Purchase",
        ),
        "relationship": make.relationship(
            relationship_id=f"rel-{prefix}",
            institution_charter=charter,
            source_id=source_id,
            product_id=product_id,
            provider_id="provider-example",
            kind=SourceRelationshipKind.PROVIDER_HOSTED,
        ),
        "profile": FetchProfile(
            schema_version=make.SV,
            profile_id=f"profile-{prefix}",
            source_id=source_id,
            method=FetchMethod.HTTP_GET,
            min_interval_seconds=5,
            timeout_seconds=30,
        ),
        "run": make.fetch_run(
            artifact_id,
            run_id=run_id,
            source_id=source_id,
            profile_id=f"profile-{prefix}",
        ),
        "artifact": make.raw_artifact(
            artifact_id=artifact_id,
            source_url=url,
            byte_length=len(body),
            fetch_run_id=run_id,
        ),
        "offer": offer,
        "review": review,
    }


def _not_modified(
    run_id: str, source_id: str, original_run_id: str, artifact_id: str
) -> dict[str, object]:
    moment = make.T0 + timedelta(hours=2)
    return {
        "run_id": run_id,
        "schema_version": make.SV,
        "source_id": source_id,
        "profile_id": "profile-north",
        "profile_sha256": make.CONFIG_HASH,
        "git_sha": make.GIT_SHA,
        "started_at": moment,
        "observed_at": moment,
        "original_run_id": original_run_id,
        "original_artifact_id": artifact_id,
        "validator_kind": "etag",
        "validator_value": '"example-etag"',
        "response_etag": None,
        "http_status": 304,
    }


def test_composite_lineage_rejects_crossed_artifact_and_institution(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    north_body = b"north-rates-v1"
    south_body = b"south-rates-v1"
    north = _provider_chain("north", NORTH, north_body)
    south = _provider_chain("south", SOUTH, south_body)
    engine = open_operational_database(path)
    with engine.begin() as conn:
        persist_chain(conn, **north)  # type: ignore[arg-type]
        persist_chain(conn, **south)  # type: ignore[arg-type]
    north_artifact = hashlib.sha256(north_body).hexdigest()
    south_artifact = hashlib.sha256(south_body).hexdigest()
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(not_modified_observations),
            _not_modified("reobs-crossed", "src-north", "fetch-north", south_artifact),
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(not_modified_observations),
            _not_modified("reobs-other-source", "src-south", "fetch-north", north_artifact),
        )
    with engine.begin() as conn:
        conn.execute(
            insert(not_modified_observations),
            _not_modified("reobs-ok", "src-north", "fetch-north", north_artifact),
        )
        south_provenance = (
            conn.execute(
                select(provenance.c.provenance_id).where(provenance.c.institution_charter == SOUTH)
            )
            .scalars()
            .first()
        )
        assert south_provenance is not None
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(offer_evidence),
                {
                    "offer_id": "offer-north",
                    "provenance_id": south_provenance,
                    "role": "supporting",
                    "field_path": "",
                    "institution_charter": NORTH,
                    "ordinal": 40,
                },
            )
        stored = (
            conn.execute(
                select(not_modified_observations.c.run_id).order_by(
                    not_modified_observations.c.run_id
                )
            )
            .scalars()
            .all()
        )
    assert stored == ["reobs-ok"]


def test_offer_field_paths_and_review_evidence_round_trip(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    north = _provider_chain("north", NORTH, b"north-rates-v1")
    south = _provider_chain("south", SOUTH, b"south-rates-v1")
    engine = open_operational_database(path)
    with engine.begin() as conn:
        persist_chain(conn, **north)  # type: ignore[arg-type]
        persist_chain(conn, **south)  # type: ignore[arg-type]
        for prefix, chain in (("north", north), ("south", south)):
            loaded_offer = load_normalized_offer(conn, f"offer-{prefix}")
            loaded_review = load_review_decision(conn, f"review-{prefix}")
            assert loaded_offer == chain["offer"]
            assert loaded_review == chain["review"]
            offer = chain["offer"]
            review = chain["review"]
            assert isinstance(offer, NormalizedOffer)
            assert isinstance(review, ReviewDecision)
            assert loaded_offer.field_provenance == offer.field_provenance
            assert loaded_review.evidence == review.evidence
        paths = (
            conn.execute(
                select(offer_evidence.c.field_path).where(offer_evidence.c.role == "field")
            )
            .scalars()
            .all()
        )
        evidence_count = conn.execute(select(review_evidence.c.provenance_id)).all()
    assert "details.term_months" in paths
    assert "details.lock_days" in paths
    assert len(evidence_count) == 2


def test_utc_datetime_preserves_microseconds(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    moment = datetime(2026, 11, 15, 15, 4, 5, 123456, tzinfo=UTC)
    later = moment.replace(microsecond=123457)
    zero = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
    engine = open_operational_database(path)
    with engine.begin() as conn:
        conn.execute(
            insert(withdrawal_entries),
            [
                {
                    "withdrawal_id": "wd-micro",
                    "schema_version": "1.0",
                    "scope": "offer",
                    "target_id": "offer-micro",
                    "actor": "example-reviewer",
                    "withdrawn_at": moment,
                    "reason": "microsecond one",
                },
                {
                    "withdrawal_id": "wd-later",
                    "schema_version": "1.0",
                    "scope": "offer",
                    "target_id": "offer-later",
                    "actor": "example-reviewer",
                    "withdrawn_at": later,
                    "reason": "microsecond two",
                },
                {
                    "withdrawal_id": "wd-zero",
                    "schema_version": "1.0",
                    "scope": "offer",
                    "target_id": "offer-zero",
                    "actor": "example-reviewer",
                    "withdrawn_at": zero,
                    "reason": "whole second",
                },
            ],
        )
    with engine.connect() as conn:
        loaded = conn.execute(
            select(withdrawal_entries.c.withdrawal_id, withdrawal_entries.c.withdrawn_at).order_by(
                withdrawal_entries.c.withdrawal_id
            )
        ).all()
        raw_times = conn.execute(
            text(
                "SELECT withdrawal_id, withdrawn_at FROM withdrawal_entries ORDER BY withdrawal_id"
            )
        ).all()
    by_id = {row.withdrawal_id: row.withdrawn_at for row in loaded}
    assert by_id["wd-micro"] == moment
    assert by_id["wd-later"] == later
    assert by_id["wd-micro"] != by_id["wd-later"]
    assert by_id["wd-micro"].microsecond == 123456
    assert by_id["wd-zero"] == zero
    stored = {row[0]: row[1] for row in raw_times}
    assert stored["wd-micro"] == "2026-11-15T15:04:05.123456+00:00"
    assert stored["wd-later"] == "2026-11-15T15:04:05.123457+00:00"
    assert stored["wd-zero"] == "2026-11-15T15:00:00.000000+00:00"


def _raw_offer(conn: object, **overrides: object) -> None:
    values: dict[str, object] = {
        "offer_id": "offer-check",
        "schema_version": "1.0",
        "institution_charter": NORTH,
        "product_id": "product-north",
        "source_id": "source-north",
        "status": "candidate",
        "details_json": '{"kind":"purchase_mortgage"}',
        "scenario_id": None,
        "scenario_matches_json": "[]",
        "note_rate": None,
        "apr": None,
        "points": None,
        "note_rate_provenance_id": None,
        "apr_provenance_id": None,
        "points_provenance_id": None,
        "conditions_json": "[]",
        "tiers_json": "[]",
        "headline_is_as_low_as": 0,
        "effective_at": None,
        "observed_at": "2026-11-15T15:00:00.000000+00:00",
        "published_at": None,
        "valid_until": None,
    }
    values.update(overrides)
    columns = ", ".join(values)
    statement = text(
        f"INSERT INTO normalized_offers ({columns}) VALUES ("
        + ", ".join(f":{name}" for name in values)
        + ")"
    )
    conn.execute(statement, values)  # type: ignore[attr-defined]


def test_published_offer_rejects_nonfinite_apr_and_bad_times(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    _seed(path)
    engine = open_operational_database(path)
    rejected = (
        {"offer_id": "offer-nan", "apr": "NaN", "apr_provenance_id": "prov-note"},
        {"offer_id": "offer-inf", "apr": "Infinity", "apr_provenance_id": "prov-note"},
        {"offer_id": "offer-unpublished", "status": "published"},
        {"offer_id": "offer-withdrawn", "status": "withdrawn"},
        {"offer_id": "offer-early", "valid_until": "2026-11-14T15:00:00.000000+00:00"},
        {"offer_id": "offer-same", "valid_until": "2026-11-15T15:00:00.000000+00:00"},
        {
            "offer_id": "offer-all-three",
            "status": "published",
            "apr": "NaN",
            "apr_provenance_id": "prov-note",
            "published_at": None,
            "valid_until": "2026-11-14T15:00:00.000000+00:00",
        },
    )
    for overrides in rejected:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _raw_offer(conn, **overrides)
    with engine.begin() as conn:
        _raw_offer(
            conn,
            offer_id="offer-published-ok",
            status="published",
            apr="6.25",
            apr_provenance_id="prov-note",
            published_at="2026-11-15T15:00:00.000000+00:00",
            valid_until="2026-11-18T15:00:00.000000+00:00",
        )
        stored = conn.execute(
            text(
                "SELECT typeof(apr), apr FROM normalized_offers "
                "WHERE offer_id = 'offer-published-ok'"
            )
        ).one()
        with pytest.raises(StatementError, match="finite"):
            conn.execute(
                insert(normalized_offers),
                {
                    "offer_id": "offer-typed-nan",
                    "schema_version": "1.0",
                    "institution_charter": NORTH,
                    "product_id": "product-north",
                    "source_id": "source-north",
                    "status": "candidate",
                    "details_json": {"kind": "purchase_mortgage"},
                    "scenario_id": None,
                    "scenario_matches_json": [],
                    "note_rate": None,
                    "apr": Decimal("NaN"),
                    "points": None,
                    "note_rate_provenance_id": None,
                    "apr_provenance_id": "prov-note",
                    "points_provenance_id": None,
                    "conditions_json": [],
                    "tiers_json": [],
                    "headline_is_as_low_as": 0,
                    "effective_at": None,
                    "observed_at": WHEN,
                    "published_at": None,
                    "valid_until": None,
                },
            )
    assert stored == ("text", "6.25")
    payload = make.purchase_offer().model_dump(mode="json")
    payload["status"] = "published"
    payload["published_at"] = None
    payload["valid_until"] = "2020-01-01T00:00:00+00:00"
    payload["apr"]["value"] = "NaN"
    with engine.connect() as conn:
        before = conn.execute(select(normalized_offers.c.offer_id)).scalars().all()
        with pytest.raises(ValidationError):
            write_offer(conn, payload)
        after = conn.execute(select(normalized_offers.c.offer_id)).scalars().all()
    assert after == before


def test_migrated_triggers_and_checks_match_expected_set(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _migrate(path)
    engine = open_operational_database(path)
    with engine.connect() as conn:
        triggers = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        }
        inspector = inspect(conn)
        actual: Counter[tuple[str, str]] = Counter()
        for table_name in inspector.get_table_names():
            if table_name == "alembic_version":
                continue
            for check in inspector.get_check_constraints(table_name):
                actual[(table_name, str(check["name"]))] += 1
    assert triggers == expected_trigger_names()
    assert actual == expected_check_names()

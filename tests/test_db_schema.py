"""Operational schema: migrations, provenance, isolation, foreign keys, rollback."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, StatementError

from cu_rates.db import (
    ENUM_CHECKS,
    HEAD_REVISION,
    AppendOnlyError,
    current_revision,
    downgrade_database,
    open_operational_database,
    upgrade_database,
)
from cu_rates.db.migrate import VERSIONS_DIR, _config, script_head
from cu_rates.db.schema import (
    fetch_profiles,
    fetch_runs,
    institution_source_relationships,
    institutions,
    membership_rules,
    normalized_offers,
    product_findings,
    products,
    provenance,
    rate_sources,
    raw_artifacts,
    review_decisions,
    service_providers,
    withdrawal_entries,
)

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
            insert(raw_artifacts),
            {
                "artifact_id": SHA,
                "schema_version": "1.0",
                "media_type": "text/html; charset=utf-8",
                "source_url": "https://north.example/rates",
                "fetched_at": WHEN,
                "http_status": 200,
                "byte_length": 12,
                "fetch_run_id": "run-north",
            },
        )
        conn.execute(
            insert(provenance),
            {
                "provenance_id": "prov-note",
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
    assert current_revision(path) == HEAD_REVISION
    script = ScriptDirectory.from_config(_config(path))
    assert script.get_current_head() == script_head() == HEAD_REVISION

    downgrade_database(path)
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert "institutions" not in names
    assert current_revision(path) is None

    _migrate(path)
    assert current_revision(path) == HEAD_REVISION
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
        fetched = conn.execute(select(raw_artifacts.c.fetched_at)).scalar_one()
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
    assert current_revision(path) == HEAD_REVISION


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

"""Synthetic seed fixtures and the outcomes a later parser must not invent.

These tests store the fixture bytes in the evidence store and check the authored
expectations against those bytes. They do not parse HTML, extract offers, or
decide publication.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cu_rates.artifact_store import ArtifactStore

SEED = Path(__file__).parent / "fixtures" / "seed"
CATALOG_PATH = SEED / "expected_outcomes.json"
OBSERVED_AT = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_TEXT = ("@", "mailto:", "<form", "ssn", "social security", "tel:", "apply now")
_PHONE_RE = re.compile(r"\d{3}-\d{3}-\d{4}")


def _catalog() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert loaded["catalog_id"] == "t009-seed-fixtures"
    assert loaded["offline_testing_only"] is True
    assert loaded["verifies_extraction"] is False
    return loaded


def _cases() -> list[dict[str, Any]]:
    cases = _catalog()["cases"]
    assert isinstance(cases, list)
    assert [case["case_id"] for case in cases] == [
        "athol-style",
        "stoughton-style",
        "worcester-style",
    ]
    return cases


def _case(case_id: str) -> dict[str, Any]:
    return next(case for case in _cases() if case["case_id"] == case_id)


def _body(case: dict[str, Any]) -> bytes:
    fixture = case["fixture"]
    assert isinstance(fixture, str)
    return (SEED / fixture).read_bytes()


def _text(case: dict[str, Any]) -> str:
    return _body(case).decode("utf-8")


def _region(page: str, name: str) -> str:
    start = f"<!-- region:{name} -->"
    end = f"<!-- end:{name} -->"
    assert page.count(start) == 1
    assert page.count(end) == 1
    inner = page.split(start, 1)[1].split(end, 1)[0]
    assert start not in inner
    return inner


def _reserved_https_example(url: str) -> None:
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname is not None
    assert parts.hostname == "example" or parts.hostname.endswith(".example")
    assert parts.username is None and parts.password is None


def test_catalog_identifies_three_distinct_fixture_artifacts() -> None:
    cases = _cases()
    artifact_ids = [case["artifact_id"] for case in cases]
    fixtures = [case["fixture"] for case in cases]
    assert len(set(artifact_ids)) == 3
    assert len(set(fixtures)) == 3
    for artifact_id in artifact_ids:
        assert isinstance(artifact_id, str)
        assert _SHA256_RE.fullmatch(artifact_id)


def test_fixture_bytes_match_committed_sha256_ids() -> None:
    for case in _cases():
        body = _body(case)
        assert hashlib.sha256(body).hexdigest() == case["artifact_id"]
        assert len(body) == case["byte_length"]
        assert body.endswith(b"\n")
        assert b"\r\n" not in body


def test_authored_phrases_remain_in_the_fixture_bytes() -> None:
    """Bind each expectation to its artifact. This is not extraction."""
    for case in _cases():
        page = _text(case)
        phrases = case["evidence_phrases"]
        assert isinstance(phrases, list) and phrases
        for phrase in phrases:
            assert isinstance(phrase, str) and phrase
            assert phrase in page


def test_seed_fixtures_round_trip_through_the_evidence_store(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    stored_ids: list[str] = []
    for case in _cases():
        body = _body(case)
        source_url = case["source_url"]
        fetch_run_id = case["fetch_run_id"]
        assert isinstance(source_url, str)
        assert isinstance(fetch_run_id, str)
        _reserved_https_example(source_url)
        record = store.put(
            body,
            media_type=case["media_type"],
            source_url=source_url,
            fetched_at=OBSERVED_AT,
            http_status=case["http_status"],
            fetch_run_id=fetch_run_id,
        )
        assert record.artifact_id == case["artifact_id"]
        assert record.byte_length == len(body)
        assert store.read(record) == body
        assert store.read_body(record.artifact_id) == body
        assert store.put(
            body,
            media_type=case["media_type"],
            source_url=source_url,
            fetched_at=OBSERVED_AT,
            http_status=case["http_status"],
            fetch_run_id=fetch_run_id,
        ) == record
        repeat = store.put(
            body,
            media_type=case["media_type"],
            source_url=source_url,
            fetched_at=OBSERVED_AT,
            http_status=case["http_status"],
            fetch_run_id=f"{fetch_run_id}-repeat",
        )
        assert repeat.artifact_id == record.artifact_id
        stored_ids.append(record.artifact_id)
    assert {record.artifact_id for record in store.observations()} == set(stored_ids)
    assert len(list(store.observations())) == 6


def test_changed_fixture_bytes_receive_a_different_artifact_id(tmp_path: Path) -> None:
    case = _case("athol-style")
    body = _body(case)
    mutated = body.replace(b"6.375%", b"6.376%", 1)
    assert mutated != body
    mutated_id = hashlib.sha256(mutated).hexdigest()
    assert mutated_id != case["artifact_id"]
    store = ArtifactStore(tmp_path / "store")
    record = store.put(
        mutated,
        media_type=case["media_type"],
        source_url=case["source_url"],
        fetched_at=OBSERVED_AT,
        http_status=case["http_status"],
        fetch_run_id="t009-athol-style-mutated",
    )
    assert record.artifact_id == mutated_id
    assert store.read(record) == mutated


def test_athol_style_keeps_one_matching_row_and_one_nonranking_row() -> None:
    case = _case("athol-style")
    assert case["page_kind"] == "static_mortgage_rate_table"
    rows = case["rows"]
    assert isinstance(rows, list) and len(rows) == 2
    by_ranking = {row["ranking"]: row for row in rows}
    matching = by_ranking["assumptions_match"]
    blocked = by_ranking["must_not_rank"]
    assert matching["row_id"] == "purchase-30-year"
    assert matching["canonical_scenario"] == "purchase_30y_fixed"
    assert matching["term_months"] == 360
    assert matching["note_rate"] and matching["apr"]
    assert blocked["row_id"] == "purchase-15-year"
    assert blocked["canonical_scenario"] == "purchase_30y_fixed"
    assert blocked["term_months"] == 180
    assert blocked["note_rate"] and blocked["apr"]
    assert "180 months" in blocked["ranking_reason"]
    assert "must not rank" in blocked["ranking_reason"]
    page = _text(case)
    assert 'id="purchase-30-year"' in page
    assert 'id="purchase-15-year"' in page


def test_stoughton_style_expects_zero_mortgage_offers_from_this_page() -> None:
    case = _case("stoughton-style")
    assert case["page_kind"] == "auto_and_personal_loans"
    assert case["mortgage_offers_from_this_page"] == 0
    assert case["institution_offering"] == "not_inferred"
    assert "any other page" in case["institution_offering_reason"]
    rows = case["rows"]
    assert isinstance(rows, list) and len(rows) == 3
    assert {row["product_family"] for row in rows} == {"vehicle", "unsecured_personal"}
    page = _text(case).lower()
    for absent in ("mortgage", "heloc", "home equity", "home-equity"):
        assert absent not in page


def test_worcester_style_separates_quote_partner_link_and_second_mortgage() -> None:
    case = _case("worcester-style")
    assert case["page_kind"] == "quote_partner_and_second_mortgage"
    first = case["first_mortgage"]
    partner = case["partner_link"]
    table = case["second_mortgage_table"]
    assert first == {
        "availability": "quote_only",
        "published_rate": False,
        "note_rate": None,
        "apr": None,
    }
    assert partner["is_rate"] is False
    assert partner["is_institution_rate"] is False
    _reserved_https_example(partner["url"])
    assert table["published_rate"] is True
    rows = table["rows"]
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert row["product_family"] == "home_equity_loan"
    assert row["lien"] == "second"
    assert row["term_months"] == 120
    assert row["canonical_scenario"] == "fixed_home_equity_10y"
    assert row["ranking"] == "assumptions_match"
    assert row["note_rate"] and row["apr"]
    page = _text(case)
    quote = _region(page, "first-mortgage")
    link = _region(page, "partner-link")
    second = _region(page, "second-mortgage")
    assert "does not advertise a first-mortgage note rate or APR" in quote
    assert "%" not in quote
    assert partner["url"] in link
    assert "not a note rate or an APR" in link
    assert "%" not in link
    assert "7.250%" in second and "7.418%" in second


def test_fixtures_contain_no_personal_or_lead_data() -> None:
    for case in _cases():
        page = _text(case).lower()
        for forbidden in _FORBIDDEN_TEXT:
            assert forbidden not in page
        assert _PHONE_RE.search(page) is None

"""#100: the public correction form only ever ADDS a case, and protects the reporter."""

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.intake import IntakeStore
from cu_rates.web import views
from cu_rates.web.app import create_app
from cu_rates.web.config import validated_config

ORIGIN = "https://cu-rates.org"
SECRET = "private.reporter@example.test"
GOOD = {
    "category": "consumer",
    "kind": "rate",
    "url": "https://cu-rates.org/compare",
    "field": "APR",
    "reason": "The APR shown differs from the credit union's own rate sheet.",
    "evidence": "https://example.test/rates",
    "contact": SECRET,
    "website": "",
}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "intake" / "intake.sqlite"
    IntakeStore(path).initialize()
    return path


@pytest.fixture
def app(db: Path) -> Flask:
    return create_app({"INTAKE_DB": str(db)})


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def post(client: FlaskClient, data: dict[str, str] | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
    headers = kwargs.pop("headers", {"Origin": ORIGIN})
    return client.post(
        "/corrections", data=GOOD if data is None else data, headers=headers, **kwargs
    )


def cases(db: Path) -> list[tuple[str, ...]]:
    with closing(sqlite3.connect(db)) as raw:
        return list(raw.execute("SELECT * FROM cases"))


def test_the_page_states_the_rules_and_shows_the_form(client: FlaskClient) -> None:
    response = client.get("/corrections")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "do not send applications, account numbers" in html.lower()
    assert "<form" in html and 'name="reason"' in html
    assert "deleted 90 days after the case is resolved" in html
    assert "one business day" in html and "five business days" in html
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "Set-Cookie" not in response.headers


def test_the_page_is_discoverable_and_the_privacy_page_discloses_it(client: FlaskClient) -> None:
    assert 'href="/corrections"' in client.get("/").get_data(as_text=True)
    privacy = client.get("/privacy").get_data(as_text=True)
    assert "Reporting a Correction" in privacy and "90 days" in privacy
    assert "/corrections" in client.get("/sitemap.xml").get_data(as_text=True)


def test_without_an_intake_store_the_form_is_closed_and_nothing_is_created(tmp_path: Path) -> None:
    client = create_app({}).test_client()
    page = client.get("/corrections")
    assert page.status_code == 200 and "<form" not in page.get_data(as_text=True)
    assert "not open yet" in page.get_data(as_text=True)
    assert post(client).status_code == 503
    assert list(tmp_path.iterdir()) == []


def test_a_valid_report_is_stored_and_acknowledged(client: FlaskClient, db: Path) -> None:
    response = post(client)
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and "your report was received" in html
    ((case_id, *_),) = cases(db)
    assert case_id in html  # the reference the reporter can quote
    case = IntakeStore(db).get(case_id)
    assert case.affected_url == GOOD["url"] and case.reason == GOOD["reason"]
    assert case.reply_contact == SECRET
    assert SECRET not in html
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert "Set-Cookie" not in response.headers


def test_the_reply_contact_is_only_in_the_restricted_table(client: FlaskClient, db: Path) -> None:
    post(client)
    with closing(sqlite3.connect(db)) as raw:
        for table in ("cases", "case_events", "meta"):
            assert SECRET not in str(list(raw.execute(f"SELECT * FROM {table}")))
        assert SECRET in str(list(raw.execute("SELECT * FROM reporter_contacts")))


def test_the_reply_contact_is_never_logged_or_echoed(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        accepted = post(client)
        rejected = post(client, GOOD | {"url": "not a url"})
    assert SECRET not in accepted.get_data(as_text=True)
    assert SECRET not in rejected.get_data(as_text=True)  # the form is re-shown without it
    assert SECRET not in caplog.text


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://cu-rates.org", ""])
def test_a_missing_or_foreign_origin_cannot_submit(
    client: FlaskClient, db: Path, origin: str | None
) -> None:
    headers = {} if origin is None else {"Origin": origin}
    assert post(client, headers=headers).status_code == 400
    assert cases(db) == []


def test_only_form_encoded_bodies_are_accepted(client: FlaskClient, db: Path) -> None:
    response = client.post("/corrections", json=GOOD, headers={"Origin": ORIGIN})
    assert response.status_code == 415
    multipart = client.post(
        "/corrections", data=GOOD, content_type="multipart/form-data", headers={"Origin": ORIGIN}
    )
    assert multipart.status_code in {400, 415}
    assert cases(db) == []


def test_oversized_bodies_are_refused(client: FlaskClient, db: Path) -> None:
    assert post(client, GOOD | {"reason": "x" * 20_000}).status_code == 413
    assert cases(db) == []


def test_unknown_and_repeated_fields_are_refused(client: FlaskClient, db: Path) -> None:
    assert post(client, GOOD | {"is_admin": "1"}).status_code == 400
    repeated = client.post(
        "/corrections",
        data="category=consumer&category=vendor&kind=rate&url=https%3A%2F%2Fx.test&reason=r",
        content_type="application/x-www-form-urlencoded",
        headers={"Origin": ORIGIN},
    )
    assert repeated.status_code == 400
    assert cases(db) == []


def test_the_honeypot_looks_like_success_but_stores_nothing(client: FlaskClient, db: Path) -> None:
    response = post(client, GOOD | {"website": "http://spam.example"})
    assert response.status_code == 200 and "your report was received" in response.get_data(
        as_text=True
    )
    assert cases(db) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "javascript:alert(1)"},
        {"url": ""},
        {"reason": "   "},
        {"evidence": "ftp://example.test/x"},
        {"contact": "not-an-email"},
        {"category": "admin"},
        {"kind": "nonsense"},
    ],
)
def test_invalid_reports_are_rejected_with_a_helpful_page(
    client: FlaskClient, db: Path, changes: dict[str, str]
) -> None:
    response = post(client, GOOD | changes)
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "We could not accept that" in html or "Please choose" in html
    assert "<form" in html  # the reporter can correct and retry
    assert cases(db) == []


def test_the_form_is_rate_limited_for_everyone_at_once(
    client: FlaskClient, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "CORRECTIONS_PER_HOUR", 2)
    assert [post(client).status_code for _ in range(2)] == [200, 200]
    blocked = post(client)
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "3600"
    assert len(cases(db)) == 2


def test_a_broken_or_missing_store_fails_closed_without_creating_one(
    tmp_path: Path, db: Path
) -> None:
    missing = tmp_path / "gone" / "intake.sqlite"
    client = create_app({"INTAKE_DB": str(missing)}).test_client()
    response = post(client)
    assert response.status_code == 503 and "cannot be accepted right now" in response.get_data(
        as_text=True
    )
    assert not missing.exists() and not missing.parent.exists()
    db.chmod(0o644)  # a world-readable store is refused too
    assert post(create_app({"INTAKE_DB": str(db)}).test_client()).status_code == 503


def test_a_full_store_refuses_new_reports(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cu_rates.intake import store as store_module

    monkeypatch.setattr(store_module, "MAX_OPEN_CASES", 0)
    assert post(client).status_code == 503


def test_the_content_security_policy_still_confines_the_form(client: FlaskClient) -> None:
    csp = client.get("/corrections").headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp and "default-src 'self'" in csp


@pytest.mark.parametrize("bad", ["relative/intake.sqlite", "/x/../y.sqlite", "//host/x", ""])
def test_the_intake_path_must_be_absolute_and_normalized(bad: str) -> None:
    with pytest.raises(ValueError, match="INTAKE_DB"):
        validated_config({"INTAKE_DB": bad})


def test_one_client_cannot_use_up_everyone_elses_allowance(
    client: FlaskClient, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review MEDIUM: a per-client cap sits in front of the global one."""
    monkeypatch.setattr(views, "CORRECTIONS_PER_CLIENT_PER_HOUR", 2)
    monkeypatch.setattr(views, "CORRECTIONS_PER_HOUR", 5)
    attacker = {"Origin": ORIGIN, "CF-Connecting-IP": "203.0.113.7"}
    assert [post(client, headers=attacker).status_code for _ in range(2)] == [200, 200]
    assert post(client, headers=attacker).status_code == 429
    other = {"Origin": ORIGIN, "CF-Connecting-IP": "198.51.100.9"}
    assert post(client, headers=other).status_code == 200  # a legitimate reporter still gets in
    assert len(cases(db)) == 3


def test_a_forged_client_header_cannot_crash_or_bypass_the_cap(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "CORRECTIONS_PER_CLIENT_PER_HOUR", 1)
    junk = {"Origin": ORIGIN, "CF-Connecting-IP": "not-an-ip"}
    assert post(client, headers=junk).status_code == 200
    assert post(client, headers=junk).status_code == 429  # falls back to the socket address


def test_a_locked_store_shows_a_message_instead_of_an_error_page(
    client: FlaskClient, db: Path
) -> None:
    blocker = sqlite3.connect(db, timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        response = post(client)
    finally:
        blocker.rollback()
        blocker.close()
    assert response.status_code == 503
    html = response.get_data(as_text=True)
    assert "cannot be accepted right now" in html and 'name="reason"' in html


def test_only_the_information_page_is_indexable(client: FlaskClient) -> None:
    assert "X-Robots-Tag" not in client.get("/corrections").headers
    assert "noindex" in post(client).headers["X-Robots-Tag"]
    assert "noindex" in post(client, GOOD | {"url": "nope"}).headers["X-Robots-Tag"]

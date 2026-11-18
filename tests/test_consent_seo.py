"""T055 isolated privacy, configuration, SEO and readiness guards (entirely offline)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import MultiDict
from werkzeug.test import TestResponse

from cu_rates.contracts.enums import CanonicalScenarioId
from cu_rates.web import create_app
from cu_rates.web.config import config_from_environment
from cu_rates.web.views import ARTICLES
from tests.contracts import factories as make
from tests.site_audit import (
    automatic_request_urls,
    consent_default_calls,
    executable_inline_scripts,
    parse_csp,
    same_origin,
)
from tests.test_web import _json_ld, _make_candidate, _mock_public_reader, parse_page

ROOT = Path(__file__).resolve().parents[1]
GA_ID = "G-ABC1234567"
ANALYTICS = {"TESTING": True, "ENABLE_ANALYTICS": True, "GA_MEASUREMENT_ID": GA_ID}
BASE = "https://cu-rates.org"
DENIED = {
    "analytics_storage": "denied",
    "ad_storage": "denied",
    "ad_user_data": "denied",
    "ad_personalization": "denied",
}


def _get(client: FlaskClient, path: str = "/") -> TestResponse:
    return client.get(path, base_url=BASE)


def _choose(client: FlaskClient, choice: str) -> TestResponse:
    return client.post(
        "/privacy/consent", base_url=BASE, headers={"Origin": BASE}, data={"choice": choice}
    )


def _require_node() -> str:
    # Optional developer runtime, never installed by this suite. In CI, execution
    # is required; locally, absent Node skips with an informative notice.
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI") == "true":
            pytest.fail("Node runtime is required in CI for consent-script execution")
        pytest.skip("optional Node runtime unavailable for inert consent-script execution")
    return node


def _run_script(consent: str) -> dict[str, Any]:
    node = _require_node()
    source = (ROOT / "src/cu_rates/web/static/consent.js").read_text()
    result = subprocess.run(
        [node, str(ROOT / "tests/consent_harness.cjs")],
        input=json.dumps(
            {"source": source, "dataset": {"measurementId": GA_ID, "consent": consent}}
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_analytics_defaults_off_with_valid_id() -> None:
    app = create_app({"TESTING": True, "GA_MEASUREMENT_ID": GA_ID})
    response = _get(app.test_client())
    assert app.config["ENABLE_ANALYTICS"] is False
    assert "/static/consent.js" not in response.text
    assert "googletagmanager" not in response.headers["Content-Security-Policy"]
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize(
    "value",
    [
        "G-123",
        "G-ABCDEFGHIJK",
        "G-abcdefghij",
        "G-ABC1234567\n",
        " G-ABC1234567",
        "0",
        0,
        False,
        "G-ABC12345<>",
    ],
)
def test_measurement_id_validation_rejects_invalid(value: object) -> None:
    with pytest.raises(ValueError, match="GA_MEASUREMENT_ID"):
        create_app({"GA_MEASUREMENT_ID": value})


@pytest.mark.parametrize("value", [None, "", GA_ID, "G-0000000000"])
def test_measurement_id_accepts_absent_or_exact_pattern(value: object) -> None:
    app = create_app({"ENABLE_ANALYTICS": True, "GA_MEASUREMENT_ID": value})
    response = _get(app.test_client())
    assert ("/static/consent.js" in response.text) is (value not in (None, ""))


@pytest.mark.parametrize("flag", ["ENABLE_ANALYTICS", "ADS_ENABLED"])
@pytest.mark.parametrize("value", ["0", "false", "", 0, 1, None])
def test_config_flags_require_actual_booleans(flag: str, value: object) -> None:
    with pytest.raises(ValueError, match=flag):
        create_app({flag: value})


@pytest.mark.parametrize(
    "enabled,measurement_id",
    [(False, None), (False, GA_ID), (True, None), (True, ""), (True, GA_ID)],
)
def test_csp_hosts_require_enabled_and_configured(
    enabled: bool, measurement_id: str | None
) -> None:
    response = _get(
        create_app({"ENABLE_ANALYTICS": enabled, "GA_MEASUREMENT_ID": measurement_id}).test_client()
    )
    directives = parse_csp(response.headers["Content-Security-Policy"])
    ready = enabled and measurement_id == GA_ID
    assert directives["script-src"] == [
        "'self'",
        *(["https://www.googletagmanager.com"] if ready else []),
    ]
    assert directives["connect-src"] == [
        "'self'",
        *(
            ["https://www.google-analytics.com", "https://region1.google-analytics.com"]
            if ready
            else []
        ),
    ]
    assert directives["img-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    external = {
        source
        for sources in directives.values()
        for source in sources
        if source.startswith("https:")
    }
    expected = (
        {
            "https://www.googletagmanager.com",
            "https://www.google-analytics.com",
            "https://region1.google-analytics.com",
        }
        if ready
        else set()
    )
    assert external == expected
    assert "unsafe-" not in response.headers["Content-Security-Policy"]
    assert "*" not in response.headers["Content-Security-Policy"]
    assert executable_inline_scripts(response.text) == 0
    assert all(same_origin(url, BASE) for url in automatic_request_urls(response.text))


def test_consent_defaults_are_denied() -> None:
    client = create_app(ANALYTICS).test_client()
    source = client.get("/static/consent.js").text
    assert consent_default_calls(source) == [DENIED]
    assert source.index('"default"') < source.index('"update"') < source.index('"config"')


@pytest.mark.parametrize("consent", ["denied", "unset", "", "true", "GRANTED"])
def test_tag_requires_explicit_saved_optin(consent: str) -> None:
    runtime = _run_script(consent)
    assert runtime["requests"] == []
    assert runtime["calls"] == [["consent", "default", DENIED]]


def test_optin_loads_one_tag_after_denied_defaults() -> None:
    runtime = _run_script("granted")
    assert runtime["requests"] == [f"https://www.googletagmanager.com/gtag/js?id={GA_ID}"]
    assert runtime["calls"][0] == ["consent", "default", DENIED]
    assert runtime["calls"][1] == ["consent", "update", {"analytics_storage": "granted"}]
    config = runtime["calls"][3]
    assert config[:2] == ["config", GA_ID]
    assert config[2]["allow_google_signals"] is False
    assert config[2]["allow_ad_personalization_signals"] is False
    assert config[2]["page_location"] == "https://cu-rates.org/compare"
    assert config[2]["page_referrer"] == ""
    assert config[2]["cookie_domain"] == "none"


@pytest.mark.parametrize(
    "ci_value,expected_exception,match",
    [
        ("true", pytest.fail.Exception, "Node runtime is required in CI"),
        ("false", pytest.skip.Exception, "optional Node runtime unavailable"),
        (None, pytest.skip.Exception, "optional Node runtime unavailable"),
    ],
)
def test_missing_node_policy_fails_in_ci_skips_locally(
    monkeypatch: pytest.MonkeyPatch,
    ci_value: str | None,
    expected_exception: type[BaseException],
    match: str,
) -> None:
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(expected_exception, match=match):
        _require_node()


def test_choice_cookie_only_after_explicit_post_and_nojs_controls() -> None:
    client = create_app(ANALYTICS).test_client()
    for path in ["/", "/compare", "/privacy", "/learn/mortgage-basics"]:
        response = _get(client, path)
        assert "Set-Cookie" not in response.headers
        assert 'data-consent="denied"' in response.text
        assert '<main id="main-content"' in response.text
        assert '<h2 id="analytics-choice" tabindex="-1">' in response.text
        assert (
            'method="post" action="/privacy/consent" aria-labelledby="analytics-choice" '
            'aria-describedby="analytics-help"' in response.text
        )
        assert (
            'type="submit" name="choice" value="granted">Allow analytics</button>' in response.text
        )
        assert (
            'type="submit" name="choice" value="denied">Decline or withdraw analytics</button>'
            in response.text
        )
        assert '"/privacy"' in response.text
    saved = _choose(client, "granted")
    assert saved.status_code == 303
    assert saved.headers["Location"] == "/privacy#analytics-choice"
    cookie = saved.headers["Set-Cookie"]
    assert "cu_rates_analytics=granted" in cookie
    assert all(
        attr in cookie
        for attr in ["Secure", "HttpOnly", "SameSite=Strict", "Max-Age=15552000", "Path=/"]
    )
    assert 'data-consent="granted"' in _get(client, "/privacy").text
    assert "Set-Cookie" not in _get(client).headers


@pytest.mark.parametrize(
    "origin", [None, "https://evil.example", "null", "https://cu-rates.org.evil.example"]
)
def test_consent_rejects_missing_or_foreign_origin(origin: str | None) -> None:
    client = create_app(ANALYTICS).test_client()
    response = client.post(
        "/privacy/consent",
        base_url=BASE,
        data={"choice": "granted"},
        headers={} if origin is None else {"Origin": origin},
    )
    assert response.status_code == 400
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"choice": ""},
        {"choice": "yes"},
        {"choice": "granted", "extra": "1"},
        MultiDict([("choice", "granted"), ("choice", "denied")]),
    ],
)
def test_consent_rejects_invalid_choices(data: Any) -> None:
    response = (
        create_app(ANALYTICS)
        .test_client()
        .post("/privacy/consent", base_url=BASE, headers={"Origin": BASE}, data=data)
    )
    assert response.status_code == 400
    assert "Set-Cookie" not in response.headers


def test_consent_endpoint_disabled_without_configuration() -> None:
    response = _choose(create_app({}).test_client(), "granted")
    assert response.status_code == 404
    assert "Set-Cookie" not in response.headers


def test_withdrawal_removes_ga_cookies_and_stops_next_page() -> None:
    client = create_app(ANALYTICS).test_client()
    _choose(client, "granted")
    client.set_cookie("_ga", "prior", domain="cu-rates.org")
    client.set_cookie("_ga_ABC1234567", "prior", domain="cu-rates.org")
    withdrawn = _choose(client, "denied")
    assert withdrawn.status_code == 303
    cookies = withdrawn.headers.getlist("Set-Cookie")
    for name in ["_ga", "_ga_ABC1234567"]:
        assert any(f"{name}=;" in cookie and "Max-Age=0" in cookie for cookie in cookies)
        assert client.get_cookie(name, domain="cu-rates.org") is None
    assert 'data-consent="denied"' in _get(client).text
    preference = client.get_cookie("cu_rates_analytics", domain="cu-rates.org")
    assert preference is not None and preference.value == "denied"


def test_consent_state_is_never_shared_cached() -> None:
    client = create_app(ANALYTICS).test_client()
    for response in [
        _get(client),
        _choose(client, "granted"),
        _get(client),
        _choose(client, "denied"),
        _get(client),
    ]:
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "Cookie" in response.vary


@pytest.fixture
def comparison_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    candidates = [
        _make_candidate("a", "Alpha CU", apr_str="7.000"),
        _make_candidate("z", "Zeta CU", note_rate_str="3.875", apr_str="4.000"),
        _make_candidate(
            "refi", "Refi CU", scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED
        ),
        _make_candidate("incomplete", "Incomplete CU", points_str="1"),
    ]
    publisher, denylist = _mock_public_reader(tmp_path, monkeypatch, candidates)
    return {
        "TESTING": True,
        "SNAPSHOT_ROOT": str(publisher.root),
        "DENYLIST_PATH": str(denylist.path),
        "SNAPSHOT_CLOCK": make.EVALUATED_AT,
    }


def test_ad_readiness_defaults_off(comparison_config: dict[str, Any]) -> None:
    app = create_app(comparison_config)
    assert app.config["ADS_ENABLED"] is False
    assert "data-ad-slot" not in app.test_client().get("/compare").text


def test_ad_slot_only_after_tables_assumptions_and_disclosures(
    comparison_config: dict[str, Any],
) -> None:
    html = create_app({**comparison_config, "ADS_ENABLED": True}).test_client().get("/compare").text
    assert html.count('data-ad-slot="footer-after-disclosures"') == 1
    slot = html.index('data-ad-slot="footer-after-disclosures"')
    assert len(re.findall("<table ", html)) >= 3
    assert slot > html.rindex("</table>")
    assert slot > html.rindex('class="scenario-assumptions-panel')
    assert slot > html.rindex('class="coverage-disclosure')
    assert slot > html.index("</main>")
    assert html.index('<footer class="site-footer">') < slot < html.index("</footer>")
    assert slot > html.index('aria-label="Educational disclaimer"')
    assert slot > html.index('<nav aria-label="Footer navigation">')
    assert "<p>Advertisement</p>" in html[slot:]
    assert 'aria-label="Advertisement"' in html[slot:]
    assert not re.search(r"<a|<script|<iframe", html[slot : html.index("</aside>", slot)])


def test_ad_config_never_changes_comparison_order_or_content(
    comparison_config: dict[str, Any],
) -> None:
    off = create_app({**comparison_config, "ADS_ENABLED": False}).test_client()
    on = create_app({**comparison_config, "ADS_ENABLED": True}).test_client()
    for sort in [
        "institution",
        "institution_desc",
        "apr",
        "apr_desc",
        "rate",
        "rate_desc",
        "freshness",
    ]:
        path = f"/compare?sort={sort}"
        main_off = off.get(path).text.split('<main id="main-content"', 1)[1].split("</main>", 1)[0]
        main_on = on.get(path).text.split('<main id="main-content"', 1)[1].split("</main>", 1)[0]
        assert main_on == main_off


def test_no_ad_slots_on_thin_draft_error_or_filtered_empty_pages(
    comparison_config: dict[str, Any],
) -> None:
    client = create_app({**comparison_config, "ADS_ENABLED": True}).test_client()
    for path in [
        "/",
        "/privacy",
        "/methodology",
        "/learn",
        "/learn/mortgage-basics",
        "/missing",
        "/compare?scenario=heloc",
        "/compare?sort=invalid",
    ]:
        assert "data-ad-slot" not in client.get(path).text
    empty_client = create_app({"ADS_ENABLED": True}).test_client()
    assert "data-ad-slot" not in empty_client.get("/compare").text


def test_ad_slots_on_reviewed_articles_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ARTICLES["mortgage-basics"], "reviewed", True)
    client = create_app({"ADS_ENABLED": True}).test_client()
    response = client.get("/learn/mortgage-basics")
    assert "data-ad-slot" in response.text
    assert "noindex" not in response.headers.get("X-Robots-Tag", "")
    assert response.text.index("data-ad-slot") > response.text.index(
        'aria-label="Educational disclaimer"'
    )
    assert client.get("/ads.txt").status_code == 404


@pytest.mark.parametrize("flag", ["ENABLE_ANALYTICS", "ADS_ENABLED"])
@pytest.mark.parametrize("value", ["", "false", "true", "yes", "2", " 1", "1\n"])
def test_env_booleans_fail_closed(flag: str, value: str) -> None:
    with pytest.raises(ValueError, match=flag):
        config_from_environment({flag: value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("CANONICAL_ORIGIN", ""),
        ("CANONICAL_ORIGIN", "http://example.org"),
        ("CANONICAL_ORIGIN", "https://bad-.example.org"),
        ("CANONICAL_ORIGIN", "https://example.org:0"),
        ("CANONICAL_ORIGIN", "https://example.org:"),
        ("CANONICAL_ORIGIN", "https://example.org?"),
        ("GA_MEASUREMENT_ID", "bad"),
        ("SNAPSHOT_ROOT", ""),
        ("DENYLIST_PATH", ""),
        ("SNAPSHOT_ROOT", "relative"),
        ("DENYLIST_PATH", "/public/../private"),
        ("SNAPSHOT_ROOT", "/public/white space"),
        ("DENYLIST_PATH", "/public/\t"),
        ("SNAPSHOT_ROOT", "//server/path"),
    ],
)
def test_env_invalid_settings_abort_startup(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        create_app()


def test_env_unset_safe_defaults_and_zero_not_truthy() -> None:
    config = config_from_environment({})
    assert config["ENABLE_ANALYTICS"] is False
    assert config["ADS_ENABLED"] is False
    assert config["GA_MEASUREMENT_ID"] is None
    assert config["SNAPSHOT_ROOT"] is None
    assert config["DENYLIST_PATH"] is None
    config = config_from_environment(
        {"ENABLE_ANALYTICS": "0", "ADS_ENABLED": "0", "GA_MEASUREMENT_ID": ""}
    )
    assert config["ENABLE_ANALYTICS"] is False
    assert config["ADS_ENABLED"] is False
    assert config["GA_MEASUREMENT_ID"] is None


def test_env_entrypoint_wiring_and_explicit_config_isolation(
    monkeypatch: pytest.MonkeyPatch, comparison_config: dict[str, Any]
) -> None:
    settings = {
        "CANONICAL_ORIGIN": "https://preview.example:8443/",
        "ENABLE_ANALYTICS": "1",
        "GA_MEASUREMENT_ID": GA_ID,
        "ADS_ENABLED": "1",
        "SNAPSHOT_ROOT": comparison_config["SNAPSHOT_ROOT"],
        "DENYLIST_PATH": comparison_config["DENYLIST_PATH"],
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    for key in settings:
        expected: Any = settings[key]
        if key in {"ENABLE_ANALYTICS", "ADS_ENABLED"}:
            expected = True
        if key == "CANONICAL_ORIGIN":
            expected = "https://preview.example:8443"
        assert app.config[key] == expected
    isolated = create_app({})
    assert isolated.config["ENABLE_ANALYTICS"] is False
    assert isolated.config["SNAPSHOT_ROOT"] is None
    assert isolated.config["CANONICAL_ORIGIN"] == BASE
    # Bad environment cannot contaminate explicit test dictionaries either.
    monkeypatch.setenv("CANONICAL_ORIGIN", "invalid")
    assert create_app({"TESTING": True}).testing is True


def test_seo_stays_tied_to_displayed_content_with_optional_features(
    comparison_config: dict[str, Any],
) -> None:
    client = create_app({**comparison_config, **ANALYTICS, "ADS_ENABLED": True}).test_client()
    for path in ["/compare?sort=apr", "/compare?scenario=heloc"]:
        response = client.get(
            path, headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
        )
        html = response.text
        doc = parse_page(html)
        assert 'href="https://cu-rates.org/compare"' in html
        assert 'property="og:url" content="https://cu-rates.org/compare"' in html
        assert any(
            meta.get("name") == "description" and meta.get("content") for meta in doc.meta_tags
        )
        ld = _json_ld(html)
        assert ld["@type"] == "WebPage"
        assert ld["url"] == f"{BASE}/compare"
        assert "offers" not in ld and "aggregateRating" not in ld
        assert ("noindex" in response.headers.get("X-Robots-Tag", "")) is ("heloc" in path)
        assert any(meta.get("name") == "robots" for meta in doc.meta_tags) is ("heloc" in path)
    urls = [
        item.text
        for item in ElementTree.fromstring(client.get("/sitemap.xml").data).iter(
            "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
        )
    ]
    assert f"{BASE}/compare" in urls
    assert all(url is not None and "?" not in url for url in urls)
    assert not any(url is not None and "/learn/" in url for url in urls)
    assert f"Sitemap: {BASE}/sitemap.xml" in client.get("/robots.txt").text


def test_config_paths_reject_nul() -> None:
    with pytest.raises(ValueError, match="DENYLIST_PATH"):
        create_app({"DENYLIST_PATH": "/public/\x00"})


@pytest.mark.parametrize(
    "key,value",
    [
        ("CANONICAL_ORIGIN", "http://example.org"),
        ("GA_MEASUREMENT_ID", "bad"),
        ("SNAPSHOT_ROOT", "/public/../private"),
        ("DENYLIST_PATH", "/public/\t"),
    ],
)
def test_env_loader_validates_non_boolean_settings(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        config_from_environment({key: value})


def test_ad_slot_style_stays_in_document_flow() -> None:
    css = (ROOT / "src/cu_rates/web/static/style.css").read_text()
    rule = re.search(r"\.ad-slot\s*\{([^}]+)\}", css)
    assert rule is not None
    declarations = dict(
        (key.strip(), value.strip())
        for key, value in (part.split(":", 1) for part in rule.group(1).split(";") if ":" in part)
    )
    assert declarations.get("position", "static") == "static"
    assert "z-index" not in declarations
    assert "transform" not in declarations
    assert "order" not in declarations
    assert declarations["min-height"] == "6rem"
    assert declarations["width"] == "100%"

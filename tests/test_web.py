"""Offline acceptance and foundation tests for the CU-Rates.org website."""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.contracts.audit import PublishedSnapshot, SnapshotEntry, snapshot_manifest_sha256
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    LienPosition,
    LoanPurpose,
    MatchState,
    OfferStatus,
    ProductFamily,
    RateType,
    ScenarioDimension,
    WithdrawalScope,
)
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.offers import (
    FixedEquityDetails,
    HelocDetails,
    OfferCondition,
    PurchaseMortgageDetails,
    RefinanceMortgageDetails,
)
from cu_rates.contracts.publication import Placement
from cu_rates.contracts.scenarios import DimensionMatch
from cu_rates.snapshot import Denylist
from cu_rates.snapshot.builder import SnapshotCandidate, SnapshotPublisher
from cu_rates.web import create_app
from cu_rates.web.app import validate_canonical_origin
from cu_rates.web.views import ARTICLES
from tests.contracts import factories as make
from tests.site_audit import (
    RATE_FIGURE,
    automatic_request_urls,
    consent_default_calls,
    consent_update_calls,
    csp_problems,
    database_findings,
    executable_inline_scripts,
    forbidden_ranking_problems,
    load_cu_rates_sources,
    publication_problems,
    script_request_urls,
)
from tests.test_validation import claim, draft, material_evidence

VERIFIED_PAGES = ["/", "/methodology", "/privacy"]


class PageInspector(HTMLParser):
    """Simple, dependency-free HTML inspector using Python standard library."""

    def __init__(self) -> None:
        super().__init__()
        self.html_lang: str | None = None
        self.meta_tags: list[dict[str, str]] = []
        self.h1_list: list[str] = []
        self.current_h1: list[str] = []
        self.in_h1 = False
        self.landmarks: list[str] = []
        self.main_id: str | None = None
        self.nav_attrs: list[dict[str, str]] = []
        self.links: list[tuple[dict[str, str], str]] = []
        self.current_link_attrs: dict[str, str] | None = None
        self.current_link_text: list[str] = []
        self.images: list[dict[str, str]] = []
        self.resource_urls: list[tuple[str, str]] = []
        self.headings: list[str] = []
        self.tables: list[dict[str, str]] = []
        self.table_captions: list[str] = []
        self.current_caption: list[str] = []
        self.in_caption = False
        self.th_tags: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: v or "" for k, v in attrs}

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append(tag)

        if tag == "html":
            self.html_lang = attr_dict.get("lang")
        elif tag == "meta":
            self.meta_tags.append(attr_dict)
        elif tag == "h1":
            self.in_h1 = True
            self.current_h1 = []
        elif tag == "table":
            self.tables.append(attr_dict)
        elif tag == "caption":
            self.in_caption = True
            self.current_caption = []
        elif tag == "th":
            self.th_tags.append(attr_dict)
        elif tag in ("header", "main", "footer"):
            self.landmarks.append(tag)
            if tag == "main":
                self.main_id = attr_dict.get("id")
        elif tag == "nav":
            self.landmarks.append("nav")
            self.nav_attrs.append(attr_dict)
        elif tag == "a":
            self.current_link_attrs = attr_dict
            self.current_link_text = []
            if "href" in attr_dict:
                self.resource_urls.append(("a", attr_dict["href"]))
        elif tag == "img":
            self.images.append(attr_dict)
            if "src" in attr_dict:
                self.resource_urls.append(("img", attr_dict["src"]))
        elif tag == "script" and "src" in attr_dict:
            self.resource_urls.append(("script", attr_dict["src"]))
        elif tag == "link" and "href" in attr_dict:
            self.resource_urls.append(("link", attr_dict["href"]))
        elif tag == "form" and "action" in attr_dict:
            self.resource_urls.append(("form", attr_dict["action"]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.in_h1 = False
            self.h1_list.append("".join(self.current_h1).strip())
        elif tag == "caption":
            self.in_caption = False
            self.table_captions.append("".join(self.current_caption).strip())
        elif tag == "a" and self.current_link_attrs is not None:
            self.links.append((self.current_link_attrs, "".join(self.current_link_text).strip()))
            self.current_link_attrs = None
            self.current_link_text = []

    def handle_data(self, data: str) -> None:
        if self.in_h1:
            self.current_h1.append(data)
        if self.in_caption:
            self.current_caption.append(data)
        if self.current_link_attrs is not None:
            self.current_link_text.append(data)


def parse_page(html: str) -> PageInspector:
    """Parse HTML and return inspector."""
    parser = PageInspector()
    parser.feed(html)
    return parser


@pytest.fixture
def app() -> Flask:
    """Create test application in test mode."""
    return create_app({"TESTING": True})


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Create Flask test client."""
    return app.test_client()


def test_verified_pages_return_200_and_headers(client: FlaskClient) -> None:
    """Every verified page returns 200 with strict CSP and security headers."""
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200
        assert "text/html" in resp.content_type

        # Content-Security-Policy
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp
        assert csp_problems(csp) == []

        # Standard security headers
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "geolocation=()" in resp.headers.get("Permissions-Policy", "")
        assert "max-age=31536000" in resp.headers.get("Strict-Transport-Security", "")


def test_404_page_and_noindex_rules(client: FlaskClient) -> None:
    """404 error page returns 404, has noindex meta and header, and keeps out of sitemap."""
    resp = client.get("/non-existent-page-for-testing")
    assert resp.status_code == 404
    assert "text/html" in resp.content_type

    # Header noindex check
    robots_header = resp.headers.get("X-Robots-Tag", "")
    assert "noindex" in robots_header

    # HTML meta noindex check
    doc = parse_page(resp.get_data(as_text=True))
    robots_meta = [m for m in doc.meta_tags if m.get("name") == "robots"]
    assert len(robots_meta) == 1
    assert "noindex" in robots_meta[0].get("content", "")

    # Single h1 and disclaimer still present
    assert len(doc.h1_list) == 1


def test_accessibility_landmarks_and_structure(client: FlaskClient) -> None:
    """Every page provides lang='en', landmarks, skip link, single h1, and labeled navs."""
    test_paths = [*VERIFIED_PAGES, "/compare", "/arbitrary-404-path"]
    for path in test_paths:
        resp = client.get(path)
        doc = parse_page(resp.get_data(as_text=True))

        # HTML lang attribute
        assert doc.html_lang == "en"

        # Responsive viewport meta
        viewport = next((m for m in doc.meta_tags if m.get("name") == "viewport"), None)
        assert viewport is not None
        content = viewport.get("content", "")
        assert "width=device-width" in content
        assert "initial-scale=1" in content

        # Exactly one h1 per page
        assert len(doc.h1_list) == 1, f"Expected 1 h1 on {path}, found {len(doc.h1_list)}"
        assert doc.h1_list[0] != ""

        # Landmarks
        assert "header" in doc.landmarks
        assert "main" in doc.landmarks
        assert doc.main_id == "main-content"
        assert "footer" in doc.landmarks
        assert len(doc.nav_attrs) >= 1
        for nav in doc.nav_attrs:
            assert nav.get("aria-label"), f"Nav on {path} lacks aria-label"

        # Skip link
        skip_links = [
            (attrs, text)
            for attrs, text in doc.links
            if "skip-link" in attrs.get("class", "").split()
        ]
        assert len(skip_links) == 1
        skip_attrs, skip_text = skip_links[0]
        assert skip_attrs.get("href") == "#main-content"
        assert "skip" in skip_text.lower()

        # Links are non-empty
        assert len(doc.links) > 0
        for attrs, text in doc.links:
            assert text != "", f"Empty link found on {path}: {attrs}"

        # Images have alt attribute if any exist
        for img in doc.images:
            assert "alt" in img, f"Image without alt found on {path}: {img}"


def test_educational_disclaimer_on_every_page(client: FlaskClient) -> None:
    """Every page clearly states that the site is educational and not financial advice."""
    disclaimer_phrases = [
        "educational",
        "not a lender",
        "not personalized financial advice",
    ]
    test_paths = [*VERIFIED_PAGES, "/compare", "/missing-page"]
    for path in test_paths:
        resp = client.get(path)
        text = resp.get_data(as_text=True).lower()
        for phrase in disclaimer_phrases:
            assert phrase in text, f"Missing disclaimer phrase '{phrase}' on {path}"


def test_no_external_urls_in_rendered_pages_or_headers(client: FlaskClient) -> None:
    """No rendered page loads a foreign origin through markup, headers, or a loose CSP."""
    origin = str(client.application.config["CANONICAL_ORIGIN"])
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        headers = {key: value for key, value in resp.headers.items()}
        assert publication_problems(resp.get_data(as_text=True), headers, origin) == []


def test_analytics_disabled_by_default(client: FlaskClient) -> None:
    """Analytics stays off: no consent script runs and no third-party URL is requested."""
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        html = resp.get_data(as_text=True)
        assert executable_inline_scripts(html) == 0
        assert consent_default_calls(html) == []
        assert all("consent.js" not in url for url in automatic_request_urls(html))
        assert "googletagmanager" not in html
        assert "google-analytics" not in html
        headers = {key: value for key, value in resp.headers.items()}
        assert publication_problems(html, headers, "https://cu-rates.org") == []


def test_optional_analytics_consent_integration_point() -> None:
    """Enabled analytics serves denied consent from a same-origin file and loads no tag."""
    analytics_app = create_app(
        {
            "TESTING": True,
            "ENABLE_ANALYTICS": True,
            "GA_MEASUREMENT_ID": "G-MEASURETEST",
        }
    )
    test_client = analytics_app.test_client()
    resp = test_client.get("/")
    html = resp.get_data(as_text=True)
    assert executable_inline_scripts(html) == 0
    assert "G-MEASURETEST" not in html
    assert "googletagmanager" not in html
    assert "google-analytics" not in html
    sources = [
        url
        for url in automatic_request_urls(html)
        if url.endswith("/static/consent.js") or url.endswith("consent.js")
    ]
    assert sources == ["/static/consent.js"]
    script_resp = test_client.get(sources[0])
    assert script_resp.status_code == 200
    script = script_resp.get_data(as_text=True)
    assert consent_default_calls(script) == [
        {
            "analytics_storage": "denied",
            "ad_storage": "denied",
            "ad_user_data": "denied",
            "ad_personalization": "denied",
        }
    ]
    assert consent_update_calls(script) == 0
    assert script_request_urls(script) == []
    assert csp_problems(resp.headers.get("Content-Security-Policy", "")) == []


def test_robots_txt_and_sitemap_xml(client: FlaskClient) -> None:
    """Robots.txt points to sitemap; sitemap.xml lists only verified pages."""
    # robots.txt
    robots_resp = client.get("/robots.txt")
    assert robots_resp.status_code == 200
    assert "text/plain" in robots_resp.content_type
    robots_text = robots_resp.get_data(as_text=True)
    assert "User-agent: *" in robots_text
    assert "Allow: /" in robots_text
    assert "Sitemap: https://cu-rates.org/sitemap.xml" in robots_text

    # sitemap.xml
    sitemap_resp = client.get("/sitemap.xml")
    assert sitemap_resp.status_code == 200
    assert "application/xml" in sitemap_resp.content_type or "text/xml" in sitemap_resp.content_type

    root = ElementTree.fromstring(sitemap_resp.get_data())
    namespaces = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    loc_elements = root.findall(".//ns:loc", namespaces)
    sitemap_urls = [loc.text for loc in loc_elements if loc.text is not None]

    expected_urls = [
        "https://cu-rates.org/",
        "https://cu-rates.org/methodology",
        "https://cu-rates.org/privacy",
    ]
    assert sitemap_urls == expected_urls
    # 404 and unverified paths must not appear
    assert "https://cu-rates.org/404" not in sitemap_urls


def test_no_ads_and_no_ads_txt(client: FlaskClient) -> None:
    """No ads or ads.txt file exist; request to /ads.txt returns 404."""
    resp = client.get("/ads.txt")
    assert resp.status_code == 404


def test_no_rates_aprs_or_financial_figures(client: FlaskClient) -> None:
    """Site shows no rates, APRs, or financial figures until data is verified."""
    apr_pattern = re.compile(r"(?i)\bapr\b\s*:?\s*\d+")

    for path in VERIFIED_PAGES:
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert not RATE_FIGURE.search(text), f"Found rate/percentage pattern on {path}"
        assert not apr_pattern.search(text), f"Found APR with digits pattern on {path}"


def test_web_package_does_not_import_database() -> None:
    """The web layer never imports or opens the database, including through another module."""
    sources = load_cu_rates_sources(Path(__file__).resolve().parents[1] / "src")
    findings = database_findings(
        sources,
        ["cu_rates.web", "cu_rates.web.app", "cu_rates.web.views"],
    )
    assert findings == []


def test_no_present_tense_operational_claims(client: FlaskClient) -> None:
    """Public pages describe collection, freshness, and withdrawal as planned, not live."""
    forbidden_operational_phrases = [
        "are re-observed",
        "undergo scheduled re-observation",
        "automatically withdrawn",
        "runs daily",
        "run daily",
        "are archived",
        "parsers extract",
        "is quarantined and never published",
        "entry traces to",
    ]
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        text_lower = resp.get_data(as_text=True).lower()
        for phrase in forbidden_operational_phrases:
            assert phrase not in text_lower, (
                f"Found present-tense operational claim '{phrase}' on {path}"
            )

    # Verify planned and conditional wording on key pages
    home_text = client.get("/").get_data(as_text=True).lower()
    assert "will be governed" in home_text
    assert "will be quarantined" in home_text

    methodology_text = client.get("/methodology").get_data(as_text=True).lower()
    assert "is designed around" in methodology_text
    assert "will be archived" in methodology_text
    assert "planned freshness expiry" in methodology_text


def _json_ld(html: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert match is not None
    loaded = json.loads(match.group(1))
    assert isinstance(loaded, dict)
    return loaded


def _json_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found.update(_json_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_json_keys(item))
    return found


def _file_label(file: object) -> str:
    if isinstance(file, str):
        return file
    name = getattr(file, "name", None)
    if isinstance(name, str):
        return name
    return str(file)


def _runtime_db_path(label: str) -> bool:
    normalized = label.replace("\\", "/").lower()
    filename = normalized.rsplit("/", 1)[-1]
    return "cu_rates.db" in normalized or filename.endswith((".db", ".sqlite", ".sqlite3"))


def test_rendered_pages_do_not_open_a_database(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page render does not open cu_rates.db or call sqlite3.connect."""
    import builtins
    import pathlib

    real_open: Any = builtins.open

    def guarded_open(*args: Any, **kwargs: Any) -> Any:
        if args and _runtime_db_path(_file_label(args[0])):
            raise AssertionError(f"web opened database path {args[0]}")
        return real_open(*args, **kwargs)

    real_path_open = pathlib.Path.open

    def guarded_path_open(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        if _runtime_db_path(str(self)):
            raise AssertionError(f"web opened database path {self}")
        return real_path_open(self, *args, **kwargs)

    def refuse_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("web request called sqlite3.connect")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(pathlib.Path, "open", guarded_path_open)
    monkeypatch.setattr(sqlite3, "connect", refuse_connect)
    for path in (
        "/",
        "/compare",
        "/methodology",
        "/privacy",
        "/learn",
        "/learn/mortgage-basics",
        "/missing",
    ):
        client.get(path)


def test_html_and_not_found_require_revalidation(client: FlaskClient) -> None:
    """HTML responses, including the 404 page, must be revalidated before reuse."""
    for path in (
        "/",
        "/compare",
        "/methodology",
        "/privacy",
        "/learn",
        "/learn/mortgage-basics",
        "/missing-page",
    ):
        response = client.get(path)
        assert "text/html" in response.content_type
        assert response.headers.get("Cache-Control") == "no-cache, must-revalidate"
    missing = client.get("/missing-page")
    assert missing.status_code == 404
    assert 'rel="canonical"' not in missing.get_data(as_text=True)
    stylesheet = client.get("/static/style.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers.get("Cache-Control") != "no-cache, must-revalidate"


def test_public_pages_expose_canonical_open_graph_breadcrumbs_and_json_ld(
    client: FlaskClient,
) -> None:
    """Verified pages publish conservative metadata from the configured origin."""
    methodology = client.get("/methodology")
    html = methodology.get_data(as_text=True)
    assert 'rel="canonical" href="https://cu-rates.org/methodology"' in html
    assert 'property="og:url" content="https://cu-rates.org/methodology"' in html
    assert 'property="og:title"' in html
    assert 'property="og:description"' in html
    assert 'property="og:type" content="website"' in html
    assert 'property="og:site_name" content="CU-Rates.org"' in html
    assert 'aria-label="Breadcrumb"' in html
    payload = _json_ld(html)
    assert payload["@type"] == "WebPage"
    assert payload["url"] == "https://cu-rates.org/methodology"
    forbidden = {"aggregateRating", "offers", "review", "price", "ratingValue"}
    assert _json_keys(payload).isdisjoint(forbidden)
    crumbs = payload["breadcrumb"]["itemListElement"]
    assert [item["name"] for item in crumbs] == ["Home", "Methodology and Comparison Principles"]

    home = _json_ld(client.get("/").get_data(as_text=True))
    assert home["url"] == "https://cu-rates.org/"
    assert [item["name"] for item in home["breadcrumb"]["itemListElement"]] == ["Home"]

    article = client.get("/learn/mortgage-basics")
    article_html = article.get_data(as_text=True)
    assert "noindex" in article.headers.get("X-Robots-Tag", "")
    assert 'rel="canonical" href="https://cu-rates.org/learn/mortgage-basics"' in article_html
    article_ld = _json_ld(article_html)
    names = [item["name"] for item in article_ld["breadcrumb"]["itemListElement"]]
    assert names == ["Home", "Learn", "Mortgage Basics and Core Principles"]
    assert _json_keys(article_ld).isdisjoint(forbidden)
    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/learn/mortgage-basics" not in sitemap


def test_canonical_origin_is_validated_and_used_by_robots_and_sitemap() -> None:
    """robots.txt, sitemap.xml, and canonical links follow a validated origin."""
    preview = create_app({"TESTING": True, "CANONICAL_ORIGIN": "https://preview.example"})
    preview_client = preview.test_client()
    robots = preview_client.get("/robots.txt").get_data(as_text=True)
    assert "Sitemap: https://preview.example/sitemap.xml" in robots
    home = preview_client.get("/").get_data(as_text=True)
    assert 'rel="canonical" href="https://preview.example/"' in home
    sitemap = preview_client.get("/sitemap.xml").get_data(as_text=True)
    assert "https://preview.example/methodology" in sitemap
    assert "https://cu-rates.org" not in sitemap
    for slug in ARTICLES:
        assert f"/learn/{slug}" not in sitemap

    for bad in (
        "http://cu-rates.org",
        "https://cu-rates.org/extra",
        "https://user:pass@cu-rates.org",
        "https://cu-rates.org/?q=1",
        "https://cu-rates.org#fragment",
        "cu-rates.org",
        "",
        "https://cu-rates.org.evil.example/phish",
    ):
        with pytest.raises(ValueError):
            validate_canonical_origin(bad)


def test_privacy_describes_consent_as_planned(client: FlaskClient) -> None:
    """Privacy copy does not claim the consent choice is already implemented."""
    text = client.get("/privacy").get_data(as_text=True).lower()
    assert "not implemented yet" in text
    assert "planned" in text
    assert "implements google consent mode" not in text
    assert "explicit consent is provided" not in text


def test_aria_current_matches_the_open_page(client: FlaskClient) -> None:
    """The main navigation marks only the page that is actually open."""
    expectations = {
        "/": "Home",
        "/compare": "Compare",
        "/methodology": "Methodology",
        "/privacy": "Privacy",
    }
    for path, label in expectations.items():
        document = parse_page(client.get(path).get_data(as_text=True))
        current = [text for attrs, text in document.links if attrs.get("aria-current") == "page"]
        assert current == [label]


def _matches_for_points(
    scenario_id: CanonicalScenarioId, points_str: str
) -> tuple[DimensionMatch, ...]:
    """Scenario matches; non-zero points are a stated, conflicting POINTS dimension."""
    matches = make.matches_all(scenario_id)
    if Decimal(points_str) == 0:
        return matches
    conflict = DimensionMatch(
        dimension=ScenarioDimension.POINTS,
        state=MatchState.CONFLICTS,
        source_text=f"{points_str} points",
    )
    return tuple(conflict if m.dimension is ScenarioDimension.POINTS else m for m in matches)


def _make_candidate(
    offer_id: str,
    legal_name: str,
    *,
    note_rate_str: str | None = "6.125",
    apr_str: str | None = "6.250",
    points_str: str = "0",
    term_months: int = 360,
    validity: timedelta = timedelta(days=3),
    conditions: tuple[Any, ...] = (),
    scenario_id: CanonicalScenarioId = CanonicalScenarioId.PURCHASE_30Y_FIXED,
    now: datetime = make.EVALUATED_AT,
) -> SnapshotCandidate:
    effective = make.T0 - timedelta(days=1)
    date = claim("effective_at", effective.isoformat())
    details: PurchaseMortgageDetails | RefinanceMortgageDetails
    if scenario_id is CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED:
        details = RefinanceMortgageDetails(
            term_months=term_months,
            rate_type=RateType.FIXED,
            lock_days=30,
            purpose=LoanPurpose.RATE_TERM_REFINANCE,
        )
    else:
        details = PurchaseMortgageDetails(
            term_months=term_months, rate_type=RateType.FIXED, lock_days=30
        )
    overrides: dict[str, Any] = {
        "offer_id": offer_id,
        "status": OfferStatus.APPROVED,
        "effective_at": effective,
        "observed_at": make.T0,
        "valid_until": make.T0 + validity,
        "scenario_id": scenario_id,
        "points": make.points(points_str),
        "supporting_evidence": (date.provenance, make.provenance("CAPTURE " + offer_id)),
        "scenario_matches": _matches_for_points(scenario_id, points_str),
        "details": details,
        "conditions": conditions,
    }
    if note_rate_str is not None:
        overrides["note_rate"] = make.rate(note_rate_str)
    else:
        overrides["note_rate"] = None
    if apr_str is not None:
        overrides["apr"] = make.rate(apr_str)
    else:
        overrides["apr"] = None

    offer = draft(**overrides)
    if scenario_id is CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED:
        offer = material_evidence(
            offer,
            {
                "details.term_months": "30 years",
                "details.rate_type": "fixed",
                "details.lock_days": "30 days",
                "details.purpose": "rate_term_refinance",
            },
        )
    inst = make.institution(legal_name=legal_name)
    ctx = make.context(offer, evaluated_at=now, institution=inst)
    return SnapshotCandidate(offer=offer, context=ctx, candidates=(date,))


def _build_test_snapshot(
    tmp_path: Path,
    candidates: list[SnapshotCandidate],
    now: datetime = make.EVALUATED_AT,
) -> tuple[SnapshotPublisher, Denylist]:
    publisher = SnapshotPublisher(tmp_path / "fixture_snapshots")
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()

    entries = []
    for item in candidates:
        fp = offer_fingerprints(item.offer)
        entries.append(
            SnapshotEntry(
                offer_id=item.offer.offer_id,
                structural_sha256=fp.structural,
                value_sha256=fp.value,
            )
        )
    ordered = tuple(sorted(entries, key=lambda entry: entry.offer_id))
    manifest = PublishedSnapshot(
        schema_version=make.SV,
        snapshot_id="test-fixture-snap",
        published_at=now,
        published_by="test-runner",
        entries=ordered,
        manifest_sha256=snapshot_manifest_sha256(ordered),
    )
    publisher.build(manifest, candidates)
    return publisher, denylist


def test_compare_page_empty_state_and_noindex(client: FlaskClient) -> None:
    """Empty or missing snapshot serves honest empty state, noindex, and canonical scenarios."""
    resp = client.get("/compare")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type

    # Header and meta noindex
    assert "noindex" in resp.headers.get("X-Robots-Tag", "")
    html = resp.get_data(as_text=True)
    doc = parse_page(html)
    robots_meta = [m for m in doc.meta_tags if m.get("name") == "robots"]
    assert len(robots_meta) == 1
    assert "noindex" in robots_meta[0].get("content", "")

    # Honest empty state copy
    assert "No Verified Rates Published Yet" in html
    assert "strict provenance" in html.lower()
    assert "canonical comparison scenarios" in html.lower()

    # Discloses all 4 canonical scenarios and their assumptions
    assert "30-Year Fixed Mortgage — Home Purchase" in html
    assert "30-Year Fixed Mortgage — Rate and Term Refinance" in html
    assert "Fixed-Rate Home Equity Loan — 10-Year" in html
    assert "Home Equity Line of Credit (HELOC)" in html
    assert "$500,000" in html
    assert "$400,000" in html
    assert "80.000%" in html
    assert "780" in html

    # Excluded from sitemap when no verified data is published
    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/compare" not in sitemap

    # Forbidden ranking words guard
    assert forbidden_ranking_problems(html) == []

    # Exactly one h1
    assert len(doc.h1_list) == 1


def test_compare_query_parameter_allow_list(client: FlaskClient) -> None:
    """Unknown query parameters or values fail closed with HTTP 400."""
    # Unknown parameter key
    bad_param = client.get("/compare?tracking_code=abc123")
    assert bad_param.status_code == 400

    bad_extra = client.get("/compare?scenario=purchase_30y_fixed&extra=1")
    assert bad_extra.status_code == 400

    # Unknown scenario value
    bad_scenario = client.get("/compare?scenario=jumbo_conforming")
    assert bad_scenario.status_code == 400

    # Unknown sort value (including forbidden ranking words)
    bad_sort = client.get("/compare?sort=cheapest")
    assert bad_sort.status_code == 400

    bad_sort_best = client.get("/compare?sort=best")
    assert bad_sort_best.status_code == 400

    # Reject duplicate query parameters (Finding 8)
    bad_duplicate = client.get("/compare?scenario=all&scenario=purchase_30y_fixed")
    assert bad_duplicate.status_code == 400

    bad_duplicate_sort = client.get("/compare?sort=apr&sort=rate")
    assert bad_duplicate_sort.status_code == 400

    # Query length bound (Finding 8)
    bad_long = client.get("/compare?" + "&" * 2100)
    assert bad_long.status_code == 400

    # Valid parameters pass with 200
    valid_scenario = client.get("/compare?scenario=purchase_30y_fixed")
    assert valid_scenario.status_code == 200
    valid_html = valid_scenario.get_data(as_text=True)
    assert "Active scenario:" in valid_html
    assert "30-Year Fixed Purchase" in valid_html

    valid_sort = client.get("/compare?sort=apr")
    assert valid_sort.status_code == 200
    valid_sort_html = valid_sort.get_data(as_text=True)
    assert "Active sort:" in valid_sort_html
    assert "APR (Low to High)" in valid_sort_html


def test_compare_fixture_snapshot_rendering_and_incomparable_separation(
    tmp_path: Path,
) -> None:
    """Render rate comparison from a temporary fixture snapshot with full guarantees."""
    now = make.EVALUATED_AT

    # 1. Comparable Alpha CU
    alpha = _make_candidate(
        "offer-alpha",
        "Alpha Federal Credit Union",
        note_rate_str="6.125",
        apr_str="6.250",
        points_str="0",
        term_months=360,
        now=now,
    )

    # 2. Comparable Beta CU (higher APR)
    beta = _make_candidate(
        "offer-beta",
        "Beta State Credit Union",
        note_rate_str="6.375",
        apr_str="6.500",
        points_str="0",
        term_months=360,
        now=now,
    )

    # 3. Incomparable: note rate only, no APR (Decision D-05) -> Placement.INSTITUTION_DETAIL_ONLY
    gamma_no_apr = _make_candidate(
        "offer-gamma",
        "Gamma Community Credit Union",
        note_rate_str="5.990",
        apr_str=None,
        points_str="0",
        term_months=360,
        now=now,
    )

    # 4. Incomparable: conditional autopay discount -> Placement.BROWSE_ONLY
    autopay_cond = OfferCondition(
        condition_id="cond-autopay",
        kind=ConditionKind.AUTOPAY,
        description="autopay discount",
        provenance=make.provenance("autopay discount"),
        scenario_state=MatchState.CONFLICTS,
    )
    delta_autopay = _make_candidate(
        "offer-delta",
        "Delta Credit Union",
        note_rate_str="6.050",
        apr_str="6.150",
        points_str="0",
        term_months=360,
        conditions=(autopay_cond,),
        now=now,
    )

    # 5. Incomparable: relationship condition -> Placement.BROWSE_ONLY
    rel_cond = OfferCondition(
        condition_id="cond-rel",
        kind=ConditionKind.RELATIONSHIP,
        description="existing member discount",
        provenance=make.provenance("existing member discount"),
        scenario_state=MatchState.CONFLICTS,
    )
    zeta_rel = _make_candidate(
        "offer-zeta",
        "Zeta State Credit Union",
        note_rate_str="6.200",
        apr_str="6.300",
        points_str="0",
        term_months=360,
        conditions=(rel_cond,),
        now=now,
    )

    # 6. Withdrawn / Stale on denylist
    epsilon_withdrawn = _make_candidate(
        "offer-epsilon",
        "Epsilon Expired Credit Union",
        note_rate_str="5.500",
        apr_str="5.600",
        now=now,
    )

    publisher, denylist = _build_test_snapshot(
        tmp_path,
        [alpha, beta, gamma_no_apr, delta_autopay, zeta_rel, epsilon_withdrawn],
        now=now,
    )

    # Withdraw epsilon on independent emergency denylist
    denylist.withdraw(make.withdrawal(WithdrawalScope.OFFER, "offer-epsilon", withdrawn_at=make.T0))

    fixture_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    client = fixture_app.test_client()

    resp = client.get("/compare")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # When verified rates are published, noindex is NOT set on response
    assert "noindex" not in resp.headers.get("X-Robots-Tag", "")

    # And /compare IS included in sitemap.xml
    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/compare" in sitemap

    # Section-specific assertions proving strict separation (Finding 11)
    assert "incomparable-section" in html
    comp_html = html[: html.index("incomparable-section")]
    incomp_html = html[html.index("incomparable-section") :]

    # 1. Main Grid contains ONLY comparable offers: Alpha and Beta
    assert "Alpha Federal Credit Union" in comp_html
    assert "Beta State Credit Union" in comp_html
    assert "Delta Credit Union" not in comp_html
    assert "Zeta State Credit Union" not in comp_html
    assert "Gamma Community Credit Union" not in comp_html
    assert "Epsilon Expired Credit Union" not in comp_html

    assert "6.125%" in comp_html
    assert "6.250%" in comp_html
    assert "6.375%" in comp_html
    assert "6.500%" in comp_html
    assert "30 years (360 mos)" in comp_html
    assert "Fixed-Rate Mortgage (Purchase)" in comp_html
    assert "Fixed-Rate Conforming Mortgage" not in comp_html  # Conforming not inferred (Finding 5)

    # Source reference links rendered in every comparable row (Finding 7)
    assert 'class="source-link"' in comp_html
    assert 'aria-label="Source reference for Alpha Federal Credit Union"' in comp_html
    assert 'aria-label="Source reference for Beta State Credit Union"' in comp_html

    # Discloses scenario assumptions next to grid
    assert "$500,000" in html
    assert "$400,000" in html
    assert "80.000%" in html
    assert "780" in html

    # 2. Incomparable / Incomplete section contains Delta and Zeta with plain reasons
    assert "Incomparable or Incomplete Offers" in incomp_html
    assert "Delta Credit Union" in incomp_html
    assert "Zeta State Credit Union" in incomp_html
    assert "Alpha Federal Credit Union" not in incomp_html
    assert "Beta State Credit Union" not in incomp_html

    # Incomplete table renders Loan Type column and source attribution (Finding 7)
    assert '<th scope="col">Loan Type</th>' in incomp_html
    assert "Fixed-Rate Mortgage (Purchase)" in incomp_html
    assert 'aria-label="Source reference for Delta Credit Union"' in incomp_html
    assert 'aria-label="Source reference for Zeta State Credit Union"' in incomp_html

    # Discloses conditions precisely (Finding 6)
    assert "Autopay discount condition" in incomp_html
    assert "Account relationship condition" in incomp_html

    # 3. Decision D-05 and Denylist exclusions:
    # Gamma (INSTITUTION_DETAIL_ONLY under D-05) and Epsilon (withdrawn)
    # MUST NOT appear anywhere on page (Finding 3)
    assert "Gamma Community Credit Union" not in html
    assert "offer-gamma" not in html
    assert "Epsilon Expired Credit Union" not in html
    assert "offer-epsilon" not in html

    # 4. Sorting behavior
    # Default sort: institution alphabetical ascending (Alpha before Beta in comparable grid)
    table_default = comp_html[comp_html.index("<tbody>") : comp_html.index("</tbody>")]
    a_idx = table_default.index("Alpha Federal Credit Union")
    b_idx = table_default.index("Beta State Credit Union")
    assert a_idx < b_idx

    # Sort by APR descending (Beta 6.500% before Alpha 6.250% in comparable grid)
    apr_desc_resp = client.get("/compare?sort=apr_desc")
    apr_desc_html = apr_desc_resp.get_data(as_text=True)
    comp_desc = apr_desc_html[: apr_desc_html.index("incomparable-section")]
    table_desc = comp_desc[comp_desc.index("<tbody>") : comp_desc.index("</tbody>")]
    b_desc_idx = table_desc.index("Beta State Credit Union")
    a_desc_idx = table_desc.index("Alpha Federal Credit Union")
    assert b_desc_idx < a_desc_idx

    # Incomparable section ALWAYS maintains neutral institution alphabetical order (Finding 4)
    incomp_desc = apr_desc_html[apr_desc_html.index("incomparable-section") :]
    incomp_table_desc = incomp_desc[incomp_desc.index("<tbody>") : incomp_desc.index("</tbody>")]
    assert incomp_table_desc.index("Delta Credit Union") < incomp_table_desc.index(
        "Zeta State Credit Union"
    )

    # Sort by APR ascending (Alpha 6.250% before Beta 6.500%)
    apr_asc_resp = client.get("/compare?sort=apr")
    apr_asc_html = apr_asc_resp.get_data(as_text=True)
    comp_asc = apr_asc_html[: apr_asc_html.index("incomparable-section")]
    table_asc = comp_asc[comp_asc.index("<tbody>") : comp_asc.index("</tbody>")]
    assert table_asc.index("Alpha Federal Credit Union") < table_asc.index(
        "Beta State Credit Union"
    )
    incomp_asc = apr_asc_html[apr_asc_html.index("incomparable-section") :]
    incomp_table_asc = incomp_asc[incomp_asc.index("<tbody>") : incomp_asc.index("</tbody>")]
    assert incomp_table_asc.index("Delta Credit Union") < incomp_table_asc.index(
        "Zeta State Credit Union"
    )

    # 5. Accessibility requirements
    doc = parse_page(html)
    assert len(doc.tables) >= 2
    assert len(doc.table_captions) == len(doc.tables)
    for caption in doc.table_captions:
        assert caption.strip() != ""
    assert len(doc.th_tags) >= 16
    for th in doc.th_tags:
        assert th.get("scope") in ("col", "row")

    # Heading hierarchy
    assert len(doc.h1_list) == 1
    assert doc.headings[0] == "h1"

    # 6. Forbidden ranking words absent
    assert forbidden_ranking_problems(html) == []


def test_compare_freshness_degradation_and_expiry(tmp_path: Path) -> None:
    """Offers degrade to browse-only after 72h and withdraw after 96h independent guard."""
    now = make.EVALUATED_AT
    item = _make_candidate(
        "offer-decay",
        "Decay Federal Credit Union",
        note_rate_str="6.125",
        apr_str="6.250",
        validity=timedelta(hours=96),  # Valid for 96h (WITHDRAW_AFTER max validity)
        now=now,
    )
    publisher, denylist = _build_test_snapshot(tmp_path, [item], now=now)

    # 1. Fresh read (< 72h): appears in main comparable grid
    fresh_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": make.T0 + timedelta(hours=24),
        }
    )
    fresh_html = fresh_app.test_client().get("/compare").get_data(as_text=True)
    assert "Decay Federal Credit Union" in fresh_html
    assert "Incomparable or Incomplete Offers" not in fresh_html

    # 2. Degraded read (> 72h, < 96h): degraded to BROWSE_ONLY and separated
    degraded_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": make.T0 + timedelta(hours=73),
        }
    )
    degraded_html = degraded_app.test_client().get("/compare").get_data(as_text=True)
    assert "Decay Federal Credit Union" in degraded_html
    assert "Incomparable or Incomplete Offers" in degraded_html
    assert "Degraded freshness (observed over 72 hours ago)" in degraded_html

    # Still visible at 95h as degraded
    active_95_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": make.T0 + timedelta(hours=95),
        }
    )
    active_95_html = active_95_app.test_client().get("/compare").get_data(as_text=True)
    assert "Decay Federal Credit Union" in active_95_html

    # 3. Withdrawn read (> 96h): excluded completely by the independent 96-hour guard (Finding 11)
    withdrawn_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": make.T0 + timedelta(hours=97),
        }
    )
    withdrawn_html = withdrawn_app.test_client().get("/compare").get_data(as_text=True)
    assert "Decay Federal Credit Union" not in withdrawn_html
    assert "No Verified Rates Published Yet" in withdrawn_html


def test_guard_forbidden_ranking_words_catches_injected_claim_in_compare(
    client: FlaskClient,
) -> None:
    """Guard catches injected forbidden ranking claim on the comparison page."""
    clean_html = client.get("/compare").get_data(as_text=True)
    assert forbidden_ranking_problems(clean_html) == []

    # Injected defects must fail the guard
    injected_best = clean_html.replace(
        "Compare Residential Lending Terms", "Find the Best Loan Rates"
    )
    assert "forbidden ranking word 'best'" in forbidden_ranking_problems(injected_best)

    injected_cheapest = clean_html.replace(
        "All comparisons use declared canonical borrower scenarios",
        "Browse the cheapest available mortgage options",
    )
    assert "forbidden ranking word 'cheapest'" in forbidden_ranking_problems(injected_cheapest)

    injected_top = clean_html.replace(
        "All comparisons use declared canonical borrower scenarios",
        "See our top recommended lender rates",
    )
    problems = forbidden_ranking_problems(injected_top)
    assert "forbidden ranking word 'top'" in problems
    assert "forbidden ranking word 'recommended'" in problems

    injected_lowest = clean_html.replace(
        "All comparisons use declared canonical borrower scenarios",
        "Find the lowest loan rates available",
    )
    assert "forbidden ranking word 'lowest'" in forbidden_ranking_problems(injected_lowest)


def test_guard_incomparable_offers_never_classified_as_comparable() -> None:
    """Offers without APR, with degraded freshness, or with conditions never enter main grid."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    # 1. Missing APR offer -> Placement.INSTITUTION_DETAIL_ONLY is excluded completely (Finding 3)
    candidate_no_apr = _make_candidate(
        "no-apr-guard", "Test CU", note_rate_str="6.000", apr_str=None, now=now
    )
    pub_detail_only = _public_projection(candidate_no_apr, Placement.INSTITUTION_DETAIL_ONLY, False)
    assert classify_and_format_offer(pub_detail_only, now) is None

    # 2. Conditional autopay discount -> Placement.BROWSE_ONLY
    cond = OfferCondition(
        condition_id="c-guard",
        kind=ConditionKind.AUTOPAY,
        description="autopay discount",
        provenance=make.provenance("autopay discount"),
        scenario_state=MatchState.CONFLICTS,
    )
    candidate_cond = _make_candidate(
        "cond-guard",
        "Test CU",
        note_rate_str="6.000",
        apr_str="6.100",
        conditions=(cond,),
        now=now,
    )
    pub_cond = _public_projection(candidate_cond, Placement.BROWSE_ONLY, False)
    formatted_cond = classify_and_format_offer(pub_cond, now)
    assert formatted_cond is not None
    assert not formatted_cond.is_comparable
    assert "Autopay discount condition" in formatted_cond.conditions_display

    # 3. Degraded freshness (> 72 hours)
    candidate_degraded = _make_candidate(
        "degraded-guard",
        "Test CU",
        note_rate_str="6.000",
        apr_str="6.100",
        validity=timedelta(hours=120),
        now=now,
    )
    pub_degraded = _public_projection(candidate_degraded, Placement.BROWSE_ONLY, False)
    formatted_degraded = classify_and_format_offer(pub_degraded, make.T0 + timedelta(hours=73))
    assert formatted_degraded is not None
    assert not formatted_degraded.is_comparable
    assert "Degraded freshness" in formatted_degraded.separation_reason

    # 4. Independent 96-hour expiry guard: excluded completely at 97 hours (Finding 11)
    formatted_stale = classify_and_format_offer(pub_degraded, make.T0 + timedelta(hours=97))
    assert formatted_stale is None


def test_guard_expiry_isolated() -> None:
    """Isolate valid_until and WITHDRAW_AFTER guards: each fails when only that guard is removed."""
    from cu_rates.contracts.publication import WITHDRAW_AFTER
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT

    # 1. Isolate valid_until: fresh offer observed 24h ago (< 96h) but valid_until was 12h
    cand_valid = _make_candidate(
        "cand-valid-until",
        "Valid Until Expired CU",
        validity=timedelta(hours=12),
        now=now,
    )
    pub_valid = _public_projection(cand_valid, Placement.RANKED, True)
    at_24h = make.T0 + timedelta(hours=24)
    assert at_24h >= pub_valid.valid_until
    assert at_24h - pub_valid.observed_at < WITHDRAW_AFTER
    assert classify_and_format_offer(pub_valid, at_24h) is None

    # 2. Isolate WITHDRAW_AFTER: offer valid_until is 30 days away, but age is 97 hours
    cand_withdraw = _make_candidate(
        "cand-withdraw-after",
        "Withdraw After CU",
        validity=timedelta(days=30),
        now=now,
    )
    pub_withdraw = _public_projection(cand_withdraw, Placement.RANKED, True)
    at_97h = make.T0 + timedelta(hours=97)
    assert at_97h < pub_withdraw.valid_until
    assert at_97h - pub_withdraw.observed_at > WITHDRAW_AFTER
    assert classify_and_format_offer(pub_withdraw, at_97h) is None


def test_guard_placement_detail_only_and_none_isolated() -> None:
    """Isolate Placement.INSTITUTION_DETAIL_ONLY and Placement.NONE guards with full APR."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    # Both offers have full APR disclosure (6.250%) and note rate (6.125%),
    # are fresh (< 24h), and are not expired.
    cand = _make_candidate(
        "cand-placement-guards",
        "Placement Guard CU",
        note_rate_str="6.125",
        apr_str="6.250",
        now=now,
    )

    # 1. INSTITUTION_DETAIL_ONLY with full APR is excluded
    pub_detail = _public_projection(cand, Placement.INSTITUTION_DETAIL_ONLY, True)
    assert classify_and_format_offer(pub_detail, now) is None

    # 2. NONE with full APR is excluded
    pub_none = pub_detail.model_copy(update={"placement": Placement.NONE})
    assert classify_and_format_offer(pub_none, now) is None


def test_guard_missing_note_rate_isolated() -> None:
    """Isolate missing note rate guard: mortgage with APR but no note rate is not comparable."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    # Mortgage candidate with valid APR, RANKED placement, matching scenario assumptions,
    # but note_rate is None
    cand = _make_candidate(
        "cand-missing-note",
        "Missing Note CU",
        note_rate_str=None,
        apr_str="6.250",
        now=now,
    )
    pub = _public_projection(cand, Placement.RANKED, True)
    formatted = classify_and_format_offer(pub, now)
    assert formatted is not None
    assert formatted.is_comparable is False
    assert "Missing note rate disclosure" in formatted.separation_reason


def test_guard_ranked_and_conflicts_isolated() -> None:
    """Isolate conflicting assumptions and RANKED placement requirement."""
    from cu_rates.contracts.enums import ScenarioDimension
    from cu_rates.contracts.scenarios import DimensionMatch, StatedBound
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    # 1. Conflicting assumption on a RANKED offer prevents comparable classification
    cand = _make_candidate(
        "cand-conflict-assumption",
        "Conflict Assumption CU",
        note_rate_str="6.125",
        apr_str="6.250",
        now=now,
    )
    conflict_assumption = DimensionMatch(
        dimension=ScenarioDimension.LTV,
        state=MatchState.CONFLICTS,
        stated=StatedBound(exact=Decimal("90.000")),
        source_text="90.000% LTV",
    )
    updated_offer = draft(
        offer_id=cand.offer.offer_id,
        status=cand.offer.status,
        effective_at=cand.offer.effective_at,
        observed_at=cand.offer.observed_at,
        valid_until=cand.offer.valid_until,
        scenario_id=cand.offer.scenario_id,
        note_rate=cand.offer.note_rate,
        apr=cand.offer.apr,
        points=cand.offer.points,
        supporting_evidence=cand.offer.supporting_evidence,
        scenario_matches=(conflict_assumption,),
        details=cand.offer.details,
    )
    updated_cand = SnapshotCandidate(
        offer=updated_offer,
        context=cand.context,
        candidates=cand.candidates,
    )
    pub_conflict = _public_projection(updated_cand, Placement.RANKED, True)
    formatted_conflict = classify_and_format_offer(pub_conflict, now)
    assert formatted_conflict is not None
    assert formatted_conflict.is_comparable is False
    assert (
        "Borrower requirements conflict with canonical scenario assumptions"
        in formatted_conflict.separation_reason
    )
    assert "Conflicts with scenario ltv (90.000)" in formatted_conflict.conditions_display

    # 2. BROWSE_ONLY offer with matching assumptions is not comparable
    pub_browse = _public_projection(cand, Placement.BROWSE_ONLY, False)
    formatted_browse = classify_and_format_offer(pub_browse, now)
    assert formatted_browse is not None
    assert formatted_browse.is_comparable is False
    assert "Browse-only publication status" in formatted_browse.separation_reason


def test_compare_all_four_canonical_scenarios_and_heloc_grouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test all four canonical scenarios render comparable offers and HELOC groups structures."""
    now = make.EVALUATED_AT

    # 1. Purchase mortgage
    purchase = _make_candidate(
        "offer-purch",
        "Purchase Credit Union",
        note_rate_str="6.125",
        apr_str="6.250",
        points_str="0",
        term_months=360,
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED,
        now=now,
    )

    # 2. Refinance mortgage
    effective = make.T0 - timedelta(days=1)
    refi_offer = draft(
        offer_id="offer-refi",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED,
        note_rate=make.rate("6.000"),
        apr=make.rate("6.150"),
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE refi"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED),
        details=RefinanceMortgageDetails(
            term_months=360,
            rate_type=RateType.FIXED,
            lock_days=30,
            purpose=LoanPurpose.RATE_TERM_REFINANCE,
        ),
    )
    refi_cand = SnapshotCandidate(
        offer=refi_offer,
        context=make.context(
            refi_offer,
            evaluated_at=now,
            institution=make.institution(legal_name="Refi Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )

    # 3. Fixed equity 10Y
    equity_offer = draft(
        offer_id="offer-eq",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.FIXED_HOME_EQUITY_10Y,
        note_rate=make.rate("7.250"),
        apr=make.rate("7.350"),
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE eq"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y),
        details=FixedEquityDetails(
            term_months=120,
            lien_position=LienPosition.SECOND,
        ),
    )
    equity_cand = SnapshotCandidate(
        offer=equity_offer,
        context=make.context(
            equity_offer,
            evaluated_at=now,
            institution=make.institution(legal_name="Equity Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )

    # 4. HELOC Structure A: 10Y draw / 20Y repay
    heloc_a_details = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.500"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("8.000"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    heloc_a_offer = draft(
        offer_id="offer-heloc-a",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE heloc-a"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_a_details,
    )
    heloc_a_cand = SnapshotCandidate(
        offer=heloc_a_offer,
        context=make.context(
            heloc_a_offer,
            evaluated_at=now,
            institution=make.institution(legal_name="Heloc Alpha Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )

    # 5. HELOC Structure B: 5Y draw / 15Y repay (different structure!)
    heloc_b_details = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.250"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("7.750"),
        lien_position=LienPosition.SECOND,
        draw_period_months=60,
        repayment_period_months=180,
    )
    heloc_b_offer = draft(
        offer_id="offer-heloc-b",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE heloc-b"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_b_details,
    )
    heloc_b_cand = SnapshotCandidate(
        offer=heloc_b_offer,
        context=make.context(
            heloc_b_offer,
            evaluated_at=now,
            institution=make.institution(legal_name="Heloc Beta Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )

    from cu_rates.snapshot._models import SnapshotInfo
    from cu_rates.snapshot.builder import _public_projection

    info = SnapshotInfo(
        schema_version=make.SV,
        snapshot_id="mock-multi-snap",
        published_at=now,
        published_by="Publisher",
        manifest_sha256="0" * 64,
        supersedes=None,
    )
    all_offers = (
        _public_projection(purchase, Placement.RANKED, False),
        _public_projection(refi_cand, Placement.RANKED, False),
        _public_projection(equity_cand, Placement.RANKED, False),
        _public_projection(heloc_a_cand, Placement.RANKED, False),
        _public_projection(heloc_b_cand, Placement.RANKED, False),
    )
    monkeypatch.setattr(
        "cu_rates.web.views.read_snapshot_offers",
        lambda at=None: (info, all_offers),
    )

    app = create_app({"TESTING": True, "SNAPSHOT_CLOCK": now})
    html = app.test_client().get("/compare").get_data(as_text=True)

    # All four canonical scenario titles present
    assert "30-Year Fixed Mortgage — Home Purchase" in html
    assert "30-Year Fixed Mortgage — Rate and Term Refinance" in html
    assert "Fixed-Rate Home Equity Loan — 10-Year" in html
    assert "Home Equity Line of Credit (HELOC)" in html

    # Offers appear strictly under their respective scenarios
    purch_sec = html[
        html.index("heading-purchase_30y_fixed") : html.index("heading-rate_term_refi_30y_fixed")
    ]
    refi_sec = html[
        html.index("heading-rate_term_refi_30y_fixed") : html.index("heading-fixed_home_equity_10y")
    ]
    equity_sec = html[html.index("heading-fixed_home_equity_10y") : html.index("heading-heloc")]
    heloc_sec = html[html.index("heading-heloc") :]

    assert "Purchase Credit Union" in purch_sec
    assert "Purchase Credit Union" not in refi_sec
    assert "Refi Credit Union" in refi_sec
    assert "Refi Credit Union" not in purch_sec
    assert "Equity Credit Union" in equity_sec
    assert "Equity Credit Union" not in purch_sec
    assert "Heloc Alpha Credit Union" in heloc_sec
    assert "Heloc Beta Credit Union" in heloc_sec

    # Finding 2: HELOC structures are grouped separately and never mixed in one grid
    assert "Structure: 10Y Draw / 20Y Repay (120/240 mos)" in html
    assert "Structure: 5Y Draw / 15Y Repay (60/180 mos)" in html

    doc = parse_page(html)
    # Each scenario + the 2 distinct HELOC structures have separate tables with captions
    assert len(doc.tables) >= 5
    assert len(doc.table_captions) == len(doc.tables)
    for c in doc.table_captions:
        assert c.strip() != ""


def test_heloc_post_intro_proven_figures_and_labeling(tmp_path: Path) -> None:
    """Probe test: HELOC post-intro rate uses proven details.post_intro_rate, not index."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    effective = make.T0 - timedelta(days=1)

    # Derived post-intro rate = 8.000% (index 7.500% + margin 0.500%), no stated APR
    heloc_derived = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.500"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=None,
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    offer_derived = draft(
        offer_id="heloc-probe-derived",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE heloc-probe"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_derived,
    )
    cand_derived = SnapshotCandidate(
        offer=offer_derived,
        context=make.context(
            offer_derived,
            evaluated_at=now,
            institution=make.institution(legal_name="Probe Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )

    pub = _public_projection(cand_derived, Placement.BROWSE_ONLY, False)
    # Decision D-05 and Finding 2: Rate-only HELOCs (no APR) are excluded
    # from comparison tables entirely
    assert classify_and_format_offer(pub, now) is None

    # When stated APR 8.000% is present, it is labeled as APR
    heloc_stated = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.500"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("8.000"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    offer_stated = draft(
        offer_id="heloc-probe-stated",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE heloc-stated"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_stated,
    )
    cand_stated = SnapshotCandidate(
        offer=offer_stated,
        context=make.context(
            offer_stated,
            evaluated_at=now,
            institution=make.institution(legal_name="Stated Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )
    pub_stated = _public_projection(cand_stated, Placement.RANKED, False)
    formatted_stated = classify_and_format_offer(pub_stated, now)
    assert formatted_stated is not None
    assert formatted_stated.apr_display == "8.000%"
    assert formatted_stated.note_rate_display == "8.000%"
    assert formatted_stated.sort_apr == Decimal("8.000")
    assert formatted_stated.sort_rate == Decimal("8.000")
    assert formatted_stated.is_comparable

    # Finding 1: HELOC with intro rate 2.000% (6 mos) and post-intro rate 8.000% / stated APR 8.000%
    from cu_rates.contracts.offers import IntroRate

    heloc_intro = HelocDetails(
        intro=IntroRate(rate=make.rate("2.000"), duration_months=6),
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.500"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("8.000"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    offer_intro = draft(
        offer_id="heloc-probe-intro",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=make.rate("2.000"),  # Generic intro APR must NOT drive post-intro comparison
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE heloc-intro"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_intro,
    )
    cand_intro = SnapshotCandidate(
        offer=offer_intro,
        context=make.context(
            offer_intro,
            evaluated_at=now,
            institution=make.institution(legal_name="Intro Credit Union"),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )
    pub_intro = _public_projection(cand_intro, Placement.RANKED, False)
    formatted_intro = classify_and_format_offer(pub_intro, now)
    assert formatted_intro is not None
    # Finding 1: generic 2% APR does not rank; post-intro APR 8.000% is used
    assert formatted_intro.apr_display == "8.000%"
    assert formatted_intro.sort_apr == Decimal("8.000")
    # Finding 1: sort_rate is post-intro rate 8.000%, NEVER intro rate 2.000%
    assert formatted_intro.sort_rate == Decimal("8.000")
    # Finding 1: intro rate and its duration are displayed separately and labeled
    assert "8.000%" in formatted_intro.note_rate_display
    assert "Intro: 2.000% (6 mos)" in formatted_intro.note_rate_display
    assert formatted_intro.intro_rate_display == "Intro: 2.000% (6 mos)"


def test_compare_fixture_scenario_isolation_disclosures_zero_values_and_sorting(
    tmp_path: Path,
) -> None:
    """Fixture-backed test built via real snapshot builder verifying:
    - Purchase and refinance isolation (scenario-specific rows).
    - Coverage disclosure text with counts.
    - Conditions column content.
    - Freshness display text.
    - Points value.
    - Zero APR, zero rate, zero points rendered as 0 (not 'Not published').
    - Rate and APR sorting actually reordering rows vs neutral default institution order.
    - Updated copy asserting missing-APR excluded (Finding 5).
    """
    now = make.EVALUATED_AT

    # 1. Purchase offer Alpha: 6.500% rate, 6.600% APR, 0 points, standard conditions
    purch_alpha = _make_candidate(
        "offer-p-alpha",
        "Alpha Purchase CU",
        note_rate_str="6.500",
        apr_str="6.600",
        points_str="0",
        term_months=360,
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED,
        now=now,
    )

    # 2. Purchase offer Beta: 6.100% rate, 6.200% APR, 0 points
    purch_beta = _make_candidate(
        "offer-p-beta",
        "Beta Purchase CU",
        note_rate_str="6.100",
        apr_str="6.200",
        points_str="0",
        term_months=360,
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED,
        now=now,
    )

    # 3. Purchase offer Zero: 0.000% rate, 0.000% APR, 0.000 points
    purch_zero = _make_candidate(
        "offer-p-zero",
        "Zero Purchase CU",
        note_rate_str="0.000",
        apr_str="0.000",
        points_str="0",
        term_months=360,
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED,
        now=now,
    )

    # 4. Refinance offer Gamma: 5.875% rate, 5.990% APR, 0 points
    refi_gamma = _make_candidate(
        "offer-r-gamma",
        "Gamma Refinance CU",
        note_rate_str="5.875",
        apr_str="5.990",
        points_str="0",
        term_months=360,
        scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED,
        now=now,
    )

    # 5. Incomparable Purchase offer Delta: 1.250 points, conditional autopay discount
    delta_cond = OfferCondition(
        condition_id="cond-autopay-delta",
        kind=ConditionKind.AUTOPAY,
        description="autopay discount",
        provenance=make.provenance("autopay discount"),
        scenario_state=MatchState.CONFLICTS,
    )
    purch_delta_cond = _make_candidate(
        "offer-p-delta",
        "Delta Purchase CU",
        note_rate_str="6.000",
        apr_str="6.100",
        points_str="1.250",
        conditions=(delta_cond,),
        now=now,
    )

    publisher, denylist = _build_test_snapshot(
        tmp_path, [purch_alpha, purch_beta, purch_zero, refi_gamma, purch_delta_cond], now=now
    )

    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now + timedelta(hours=2),
        }
    )
    client = app.test_client()

    resp = client.get("/compare")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Finding 5: Updated copy stating missing-APR offers are excluded
    assert (
        "Offers without full APR disclosure are excluded from comparison tables; "
        "offers without matching assumptions are labeled and separated."
    ) in html
    assert (
        "Offers without full APR disclosure or matching assumptions are labeled and separated"
        not in html
    )

    # Scenario-specific rows: purchase offers only under the purchase grid,
    # refinance offers only under the refinance grid.
    purch_sec = html[
        html.index("heading-purchase_30y_fixed") : html.index("heading-rate_term_refi_30y_fixed")
    ]
    refi_sec = html[html.index("heading-rate_term_refi_30y_fixed") :]

    assert "Alpha Purchase CU" in purch_sec
    assert "Alpha Purchase CU" not in refi_sec
    assert "Beta Purchase CU" in purch_sec
    assert "Beta Purchase CU" not in refi_sec
    assert "Zero Purchase CU" in purch_sec
    assert "Zero Purchase CU" not in refi_sec
    assert "Gamma Refinance CU" in refi_sec
    assert "Gamma Refinance CU" not in purch_sec

    # Coverage disclosure text with counts
    assert "Coverage disclosure:" in purch_sec
    assert "Representing <strong>3</strong> comparable offers" in purch_sec
    assert "out of 4 total offers evaluated for this scenario." in purch_sec
    assert "Representing <strong>1</strong> comparable offer" in refi_sec
    assert "out of 1 total offer evaluated for this scenario." in refi_sec

    # Conditions column content
    assert "Autopay discount condition" in purch_sec
    assert "Published credit score: 780" in purch_sec

    # Freshness text with observation age
    assert "(3h ago)" in purch_sec
    assert "(3h ago)" in refi_sec

    # Points value disclosure
    assert "<td>0.000</td>" in purch_sec
    assert "<td>1.250</td>" in purch_sec

    # ZERO APR, zero note rate, and zero points rendered as 0 (not "Not published")
    zero_row_start = purch_sec.index('<span class="inst-name">Zero Purchase CU</span>')
    zero_row_end = purch_sec.index("</tr>", zero_row_start)
    zero_row = purch_sec[zero_row_start:zero_row_end]
    assert "0.000%" in zero_row
    assert "<td>0.000</td>" in zero_row
    assert "Not published" not in zero_row

    # Rate and APR sorting reordering rows vs neutral default institution order
    table_purch_default = purch_sec[purch_sec.index("<tbody>") : purch_sec.index("</tbody>")]
    assert table_purch_default.index("Alpha Purchase CU") < table_purch_default.index(
        "Beta Purchase CU"
    )
    assert table_purch_default.index("Beta Purchase CU") < table_purch_default.index(
        "Zero Purchase CU"
    )

    # sort=rate (ascending: Zero 0.000% < Beta 6.100% < Alpha 6.500%)
    html_rate = client.get("/compare?sort=rate").get_data(as_text=True)
    purch_rate_sec = html_rate[
        html_rate.index("heading-purchase_30y_fixed") : html_rate.index(
            "heading-rate_term_refi_30y_fixed"
        )
    ]
    table_rate = purch_rate_sec[purch_rate_sec.index("<tbody>") : purch_rate_sec.index("</tbody>")]
    assert table_rate.index("Zero Purchase CU") < table_rate.index("Beta Purchase CU")
    assert table_rate.index("Beta Purchase CU") < table_rate.index("Alpha Purchase CU")

    # sort=rate_desc (descending: Alpha 6.500% < Beta 6.100% < Zero 0.000%)
    html_rate_desc = client.get("/compare?sort=rate_desc").get_data(as_text=True)
    purch_rate_desc_sec = html_rate_desc[
        html_rate_desc.index("heading-purchase_30y_fixed") : html_rate_desc.index(
            "heading-rate_term_refi_30y_fixed"
        )
    ]
    table_rate_desc = purch_rate_desc_sec[
        purch_rate_desc_sec.index("<tbody>") : purch_rate_desc_sec.index("</tbody>")
    ]
    assert table_rate_desc.index("Alpha Purchase CU") < table_rate_desc.index("Beta Purchase CU")
    assert table_rate_desc.index("Beta Purchase CU") < table_rate_desc.index("Zero Purchase CU")

    # sort=apr (ascending: Zero 0.000% < Beta 6.200% < Alpha 6.600%)
    html_apr = client.get("/compare?sort=apr").get_data(as_text=True)
    purch_apr_sec = html_apr[
        html_apr.index("heading-purchase_30y_fixed") : html_apr.index(
            "heading-rate_term_refi_30y_fixed"
        )
    ]
    table_apr = purch_apr_sec[purch_apr_sec.index("<tbody>") : purch_apr_sec.index("</tbody>")]
    assert table_apr.index("Zero Purchase CU") < table_apr.index("Beta Purchase CU")
    assert table_apr.index("Beta Purchase CU") < table_apr.index("Alpha Purchase CU")

    # sort=apr_desc (descending: Alpha 6.600% < Beta 6.200% < Zero 0.000%)
    html_apr_desc = client.get("/compare?sort=apr_desc").get_data(as_text=True)
    purch_apr_desc_sec = html_apr_desc[
        html_apr_desc.index("heading-purchase_30y_fixed") : html_apr_desc.index(
            "heading-rate_term_refi_30y_fixed"
        )
    ]
    table_apr_desc = purch_apr_desc_sec[
        purch_apr_desc_sec.index("<tbody>") : purch_apr_desc_sec.index("</tbody>")
    ]
    assert table_apr_desc.index("Alpha Purchase CU") < table_apr_desc.index("Beta Purchase CU")
    assert table_apr_desc.index("Beta Purchase CU") < table_apr_desc.index("Zero Purchase CU")


def test_compare_empty_filtered_results_have_noindex(tmp_path: Path) -> None:
    """Empty filtered results receive noindex header (Finding 9)."""
    now = make.EVALUATED_AT
    # Snapshot contains only purchase mortgages
    item = _make_candidate(
        "offer-only-purch",
        "Purchase Only Credit Union",
        note_rate_str="6.125",
        apr_str="6.250",
        now=now,
    )
    publisher, denylist = _build_test_snapshot(tmp_path, [item], now=now)

    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    client = app.test_client()

    # Full compare page with published data has NO noindex
    resp_all = client.get("/compare")
    assert resp_all.status_code == 200
    assert "noindex" not in resp_all.headers.get("X-Robots-Tag", "")

    # Filtered scenario with NO offers receives noindex header (Finding 9)
    resp_empty = client.get("/compare?scenario=heloc")
    assert resp_empty.status_code == 200
    assert "noindex" in resp_empty.headers.get("X-Robots-Tag", "")
    assert "No Verified Rates Published for This Scenario" in resp_empty.get_data(as_text=True)


def test_format_conditions_precise_tier_and_conflict_disclosure() -> None:
    """Finding 6: Condition disclosures with precise tier bounds and conflict flags."""
    from cu_rates.contracts.enums import ScenarioDimension, TierDimension
    from cu_rates.contracts.offers import OfferCondition, RateTier, TierBound
    from cu_rates.contracts.scenarios import DimensionMatch, StatedBound
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import format_conditions

    now = make.EVALUATED_AT
    cand = _make_candidate("offer-cond-test", "Condition CU", now=now)

    tier = RateTier(
        tier_id="tier-1",
        bounds=(
            TierBound(
                dimension=TierDimension.LTV,
                lower=None,
                upper=Decimal("80.000"),
            ),
        ),
        note_rate=make.rate("6.125"),
    )
    conflict_match = DimensionMatch(
        dimension=ScenarioDimension.LTV,
        state=MatchState.CONFLICTS,
        stated=StatedBound(exact=Decimal("90.000")),
        source_text="90% LTV",
        provenance=make.provenance("90% LTV"),
    )
    ala_cond = OfferCondition(
        condition_id="c-ala",
        kind=ConditionKind.AS_LOW_AS,
        description="as low as",
        provenance=make.provenance("as low as"),
        scenario_state=MatchState.MATCHES,
    )
    updated_offer = cand.offer.model_copy(
        update={
            "tiers": (tier,),
            "scenario_matches": (conflict_match,),
            "headline_is_as_low_as": True,
            "conditions": (ala_cond,),
        }
    )
    updated_cand = SnapshotCandidate(
        offer=updated_offer,
        context=cand.context,
        candidates=cand.candidates,
    )
    pub = _public_projection(updated_cand, Placement.BROWSE_ONLY, False)
    cond_text = format_conditions(pub)

    assert "Advertised as 'as low as'" in cond_text
    assert "Tier qualification (LTV <= 80.000)" in cond_text
    assert "Conflicts with scenario ltv (90.000)" in cond_text


def test_compare_query_param_count_guard(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queries exceeding MAX_COMPARE_QUERY_PARAMS are rejected with 400 in isolation."""
    from cu_rates.web.views import MAX_COMPARE_QUERY_PARAMS

    # Allow arbitrary parameter names so the parameter-count limit is tested in isolation
    allowed = frozenset(
        {f"p{i}" for i in range(MAX_COMPARE_QUERY_PARAMS + 2)} | {"scenario", "sort"}
    )
    monkeypatch.setattr("cu_rates.web.views.ALLOWED_PARAMS", allowed)

    # 1. Exactly MAX_COMPARE_QUERY_PARAMS allowed parameters pass the count check
    params_at_limit = "&".join(f"p{i}={i}" for i in range(MAX_COMPARE_QUERY_PARAMS))
    resp_ok = client.get(f"/compare?{params_at_limit}")
    assert resp_ok.status_code == 200

    # 2. Exceeding MAX_COMPARE_QUERY_PARAMS by 1 triggers 400 purely due to parameter count
    params_exceeded = "&".join(f"p{i}={i}" for i in range(MAX_COMPARE_QUERY_PARAMS + 1))
    resp = client.get(f"/compare?{params_exceeded}")
    assert resp.status_code == 400


def test_compare_zero_guard_filtered_scenario_noindex(tmp_path: Path) -> None:
    """Finding 7: Filtered scenario returning 0 offers activates the zero-guard noindex."""
    now = make.EVALUATED_AT
    item = _make_candidate("offer-purch-only", "Purchase CU", now=now)
    publisher, denylist = _build_test_snapshot(tmp_path, [item], now=now)
    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    client = app.test_client()

    resp = client.get("/compare?scenario=heloc")
    assert resp.status_code == 200
    assert "noindex, follow" in resp.headers.get("X-Robots-Tag", "")
    assert "No Verified Rates Published for This Scenario" in resp.get_data(as_text=True)


def test_compare_eligibility_guard_browse_only_not_comparable(tmp_path: Path) -> None:
    """Finding 7: Browse-only offers without conditions never enter the comparable grid."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    cand = _make_candidate("offer-browse-only", "Browse CU", now=now)
    # BROWSE_ONLY placement with matching criteria
    pub = _public_projection(cand, Placement.BROWSE_ONLY, False)

    formatted = classify_and_format_offer(pub, now)
    assert formatted is not None
    assert formatted.is_comparable is False
    assert "Browse-only publication status" in formatted.separation_reason


def test_compare_loan_type_no_inferred_purchase_purpose() -> None:
    """Finding 7: Loan type does not infer 'Purchase' when purpose is not published."""
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import format_loan_type
    from tests.test_validation import material_evidence

    now = make.EVALUATED_AT
    effective = make.T0 - timedelta(days=1)
    equity_details = FixedEquityDetails(
        term_months=120,
        lien_position=LienPosition.SECOND,
    )
    offer_equity = draft(
        offer_id="offer-no-purp-equity",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.FIXED_HOME_EQUITY_10Y,
        note_rate=make.rate("6.000"),
        apr=make.rate("6.000"),
        points=make.points("0"),
        supporting_evidence=(make.provenance("CAPTURE equity"),),
        scenario_matches=make.matches_all(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y),
        details=equity_details,
    )
    offer_equity = material_evidence(
        offer_equity,
        {"details.term_months": "120", "details.lien_position": "second"},
    )
    cand_equity = SnapshotCandidate(
        offer=offer_equity,
        context=make.context(
            offer_equity,
            evaluated_at=now,
            institution=make.institution(legal_name="No Purpose Equity CU"),
            product=make.product(
                family=ProductFamily.HOME_EQUITY_LOAN,
                product_id=offer_equity.product_id,
            ),
        ),
        candidates=(claim("effective_at", effective.isoformat()),),
    )
    pub = _public_projection(cand_equity, Placement.RANKED, False)
    assert pub.purpose is None

    loan_type = format_loan_type(pub)
    assert "Fixed-Rate Home Equity Loan" in loan_type
    assert "Purchase" not in loan_type
    assert "Second Lien" in loan_type


def test_compare_structure_separation_not_mixed_or_swapped(tmp_path: Path) -> None:
    """Finding 7: Distinct HELOC structures are in separate tables and never mixed or swapped."""
    from tests.test_validation import material_evidence

    now = make.EVALUATED_AT
    effective = make.T0 - timedelta(days=1)
    date_10y = claim("effective_at", effective.isoformat())

    # 10Y draw / 20Y repay
    heloc_10y = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.500"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("8.000"),
        lien_position=LienPosition.SECOND,
        draw_period_months=120,
        repayment_period_months=240,
    )
    offer_10y = draft(
        offer_id="offer-heloc-10y",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(date_10y.provenance, make.provenance("CAPTURE heloc-10y")),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_10y,
    )
    offer_10y = material_evidence(
        offer_10y,
        {
            "details.index_name": "Prime",
            "details.lien_position": "second",
            "details.draw_period_months": "120 months",
            "details.repayment_period_months": "240 months",
        },
    )
    cand_10y = SnapshotCandidate(
        offer=offer_10y,
        context=make.context(
            offer_10y,
            evaluated_at=now,
            institution=make.institution(legal_name="TenYear Draw CU"),
            product=make.product(
                family=ProductFamily.HELOC,
                product_id=offer_10y.product_id,
            ),
        ),
        candidates=(date_10y,),
    )

    # 5Y draw / 15Y repay
    date_5y = claim("effective_at", effective.isoformat())
    heloc_5y = HelocDetails(
        index_name="Prime",
        index_rate=make.rate("7.500"),
        margin=make.margin("0.250"),
        floor=make.rate("4.000"),
        stated_post_intro_apr=make.rate("7.750"),
        lien_position=LienPosition.SECOND,
        draw_period_months=60,
        repayment_period_months=180,
    )
    offer_5y = draft(
        offer_id="offer-heloc-5y",
        status=OfferStatus.APPROVED,
        effective_at=effective,
        observed_at=make.T0,
        valid_until=make.T0 + timedelta(days=3),
        scenario_id=CanonicalScenarioId.HELOC,
        note_rate=None,
        apr=None,
        points=make.points("0"),
        supporting_evidence=(date_5y.provenance, make.provenance("CAPTURE heloc-5y")),
        scenario_matches=make.matches_all(CanonicalScenarioId.HELOC),
        details=heloc_5y,
    )
    offer_5y = material_evidence(
        offer_5y,
        {
            "details.index_name": "Prime",
            "details.lien_position": "second",
            "details.draw_period_months": "60 months",
            "details.repayment_period_months": "180 months",
        },
    )
    cand_5y = SnapshotCandidate(
        offer=offer_5y,
        context=make.context(
            offer_5y,
            evaluated_at=now,
            institution=make.institution(legal_name="FiveYear Draw CU"),
            product=make.product(
                family=ProductFamily.HELOC,
                product_id=offer_5y.product_id,
            ),
        ),
        candidates=(date_5y,),
    )

    publisher, denylist = _build_test_snapshot(tmp_path, [cand_10y, cand_5y], now=now)
    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    html = app.test_client().get("/compare?scenario=heloc").get_data(as_text=True)

    # Two separate structure headings
    assert "Structure: 5Y Draw / 15Y Repay (60/180 mos)" in html
    assert "Structure: 10Y Draw / 20Y Repay (120/240 mos)" in html

    pos_5y = html.index("Structure: 5Y Draw / 15Y Repay (60/180 mos)")
    pos_10y = html.index("Structure: 10Y Draw / 20Y Repay (120/240 mos)")
    assert pos_5y < pos_10y

    section_5y = html[pos_5y:pos_10y]
    section_10y = html[pos_10y:]

    assert "FiveYear Draw CU" in section_5y
    assert "TenYear Draw CU" not in section_5y

    assert "TenYear Draw CU" in section_10y
    assert "FiveYear Draw CU" not in section_10y


def test_compare_default_sort_is_alphabetical_not_apr(tmp_path: Path) -> None:
    """Finding 7: Default compare sort is strictly institution alphabetical, not APR."""
    now = make.EVALUATED_AT
    # Alpha has higher APR (7.000%)
    alpha = _make_candidate(
        "offer-alpha-sort",
        "Alpha Mutual Credit Union",
        note_rate_str="6.875",
        apr_str="7.000",
        now=now,
    )
    # Zeta has lower APR (4.000%)
    zeta = _make_candidate(
        "offer-zeta-sort",
        "Zeta State Credit Union",
        note_rate_str="3.875",
        apr_str="4.000",
        now=now,
    )

    publisher, denylist = _build_test_snapshot(tmp_path, [alpha, zeta], now=now)
    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    resp = app.test_client().get("/compare")
    html = resp.get_data(as_text=True)

    tbody = html[html.index("<tbody>") : html.index("</tbody>")]
    # Alphabetical order: Alpha (7%) before Zeta (4%)
    assert tbody.index("Alpha Mutual Credit Union") < tbody.index("Zeta State Credit Union")


def test_compare_active_sort_labels_accurate(client: FlaskClient) -> None:
    """Finding 7: Active sort labels accurately reflect the active parameter."""
    expected = {
        "/compare": "Institution Name (A-Z)",
        "/compare?sort=institution": "Institution Name (A-Z)",
        "/compare?sort=institution_desc": "Institution Name (Z-A)",
        "/compare?sort=apr": "APR (Low to High)",
        "/compare?sort=apr_desc": "APR (High to Low)",
        "/compare?sort=rate": "Note Rate (Low to High)",
        "/compare?sort=rate_desc": "Note Rate (High to Low)",
        "/compare?sort=freshness": "Observed Time (Newest First)",
    }
    for url, label in expected.items():
        resp = client.get(url)
        assert resp.status_code == 200
        assert f"Active sort: <strong>{label}</strong>" in resp.get_data(as_text=True)


def test_sitemap_excludes_detail_only_snapshots(tmp_path: Path) -> None:
    """Finding 5: Snapshots with only detail-only (no APR) offers stay out of sitemap.xml."""
    now = make.EVALUATED_AT
    detail_only = _make_candidate(
        "offer-detail-only",
        "Detail Only Credit Union",
        note_rate_str="5.500",
        apr_str=None,  # No APR -> Detail only under D-05
        now=now,
    )
    publisher, denylist = _build_test_snapshot(tmp_path, [detail_only], now=now)
    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    client = app.test_client()

    # /compare receives noindex header
    resp_compare = client.get("/compare")
    assert resp_compare.status_code == 200
    assert "noindex, follow" in resp_compare.headers.get("X-Robots-Tag", "")

    # /sitemap.xml must NOT include /compare
    resp_sitemap = client.get("/sitemap.xml")
    assert resp_sitemap.status_code == 200
    assert "/compare" not in resp_sitemap.get_data(as_text=True)


def test_format_conditions_discloses_sourced_credit_score_minimum() -> None:
    """Finding 3: Sourced credit score minimum is disclosed and does not disappear."""
    from cu_rates.contracts.enums import ScenarioDimension
    from cu_rates.contracts.scenarios import DimensionMatch, StatedBound
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import format_conditions

    now = make.EVALUATED_AT
    cand = _make_candidate("offer-cs-min", "Credit Score CU", now=now)
    cs_match = DimensionMatch(
        dimension=ScenarioDimension.CREDIT_SCORE,
        state=MatchState.MATCHES,
        stated=StatedBound(lower=Decimal("700")),
        source_text="Minimum credit score 700",
        provenance=make.provenance("Credit score 700"),
    )
    updated_offer = cand.offer.model_copy(update={"scenario_matches": (cs_match,)})
    updated_cand = SnapshotCandidate(
        offer=updated_offer,
        context=cand.context,
        candidates=cand.candidates,
    )
    pub = _public_projection(updated_cand, Placement.RANKED, False)
    cond_text = format_conditions(pub)
    assert "Published min credit score: 700" in cond_text
    assert "None published" not in cond_text


def test_borrower_only_condition_accurate_exclusion_reason() -> None:
    """Finding 3: Borrower-only requirement is accurately labeled, not as discount/relationship."""
    from cu_rates.contracts.offers import OfferCondition
    from cu_rates.snapshot.builder import _public_projection
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    borrower_cond = OfferCondition(
        condition_id="c-fthb",
        kind=ConditionKind.BORROWER_REQUIREMENT,
        description="First-time homebuyer requirement",
        provenance=make.provenance("First-time homebuyer requirement"),
        scenario_state=MatchState.CONFLICTS,
    )
    cand = _make_candidate(
        "offer-fthb",
        "Borrower Req CU",
        conditions=(borrower_cond,),
        now=now,
    )
    pub = _public_projection(cand, Placement.BROWSE_ONLY, False)
    formatted = classify_and_format_offer(pub, now)
    assert formatted is not None
    assert "Borrower eligibility requirement" in formatted.separation_reason
    assert "discount or relationship" not in formatted.separation_reason


def _sort_stub(institution: str, observed_at: datetime) -> Any:
    """Minimal stand-in exposing only the attributes ``sort_offers`` reads."""
    return SimpleNamespace(sort_institution=institution, sort_freshness=observed_at)


def test_institution_and_freshness_sorts_have_the_documented_direction() -> None:
    """institution_desc is Z-A, freshness is newest first, the default is A-Z."""
    from cu_rates.web.comparison import sort_offers

    old, mid, new = make.T0, make.T0 + timedelta(hours=1), make.T0 + timedelta(hours=2)
    alpha, beta, gamma = (
        _sort_stub("alpha", mid),
        _sort_stub("beta", new),
        _sort_stub("gamma", old),
    )
    offers = [beta, gamma, alpha]

    assert [o.sort_institution for o in sort_offers(offers, "institution")] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert [o.sort_institution for o in sort_offers(offers, "institution_desc")] == [
        "gamma",
        "beta",
        "alpha",
    ]
    # Newest observation first; the institution name breaks ties deterministically.
    assert [o.sort_institution for o in sort_offers(offers, "freshness")] == [
        "beta",
        "alpha",
        "gamma",
    ]
    # An unknown key falls back to the neutral default, never a ranking.
    assert [o.sort_institution for o in sort_offers(offers, "best")] == ["alpha", "beta", "gamma"]


def _tier(*bounds: tuple[str, Decimal | None, Decimal | None]) -> Any:
    return SimpleNamespace(
        bounds=tuple(
            SimpleNamespace(dimension=SimpleNamespace(value=dim), lower=lower, upper=upper)
            for dim, lower, upper in bounds
        )
    )


def test_tier_qualifications_disclose_the_exact_published_bounds() -> None:
    """Tier bounds are shown as range, lower-only or upper-only, never merged or dropped."""
    from cu_rates.web.comparison import format_conditions

    offer = SimpleNamespace(
        headline_is_as_low_as=True,
        tiers=(
            _tier(
                ("ltv", Decimal("60"), Decimal("80")),
                ("credit_score", Decimal("740"), None),
                ("loan_amount", None, Decimal("726200")),
            ),
        ),
        assumptions=(),
        conditions=(),
    )
    text = format_conditions(offer)  # type: ignore[arg-type]
    assert "Advertised as 'as low as'" in text
    assert "Tier qualification (LTV: 60 to 80)" in text
    assert "Tier qualification (CREDIT_SCORE >= 740)" in text
    assert "Tier qualification (LOAN_AMOUNT <= 726200)" in text

"""Offline acceptance and foundation tests for the CU-Rates.org website."""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.contracts.audit import PublishedSnapshot, SnapshotEntry, snapshot_manifest_sha256
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    MatchState,
    OfferStatus,
    RateType,
    WithdrawalScope,
)
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.offers import OfferCondition, PurchaseMortgageDetails
from cu_rates.contracts.publication import Placement
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
from tests.test_validation import claim, draft

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

    overrides: dict[str, Any] = {
        "offer_id": offer_id,
        "status": OfferStatus.APPROVED,
        "effective_at": effective,
        "observed_at": make.T0,
        "valid_until": make.T0 + validity,
        "scenario_id": scenario_id,
        "points": make.points(points_str),
        "supporting_evidence": (date.provenance, make.provenance("CAPTURE " + offer_id)),
        "scenario_matches": make.purchase_offer().scenario_matches,
        "details": PurchaseMortgageDetails(
            term_months=term_months,
            rate_type=RateType.FIXED,
            lock_days=30,
        ),
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
    assert "APR (Lowest First)" in valid_sort_html


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

    # 3. Incomparable: note rate only, no APR (Decision D-05)
    gamma_no_apr = _make_candidate(
        "offer-gamma",
        "Gamma Community Credit Union",
        note_rate_str="5.990",
        apr_str=None,
        points_str="0",
        term_months=360,
        now=now,
    )

    # 4. Incomparable: conditional autopay discount
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

    # 5. Withdrawn / Stale on denylist
    epsilon_withdrawn = _make_candidate(
        "offer-epsilon",
        "Epsilon Expired Credit Union",
        note_rate_str="5.500",
        apr_str="5.600",
        now=now,
    )

    publisher, denylist = _build_test_snapshot(
        tmp_path,
        [alpha, beta, gamma_no_apr, delta_autopay, epsilon_withdrawn],
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

    # 1. Main Grid contains comparable offers: Alpha and Beta
    assert "Alpha Federal Credit Union" in html
    assert "Beta State Credit Union" in html
    assert "6.125%" in html
    assert "6.250%" in html
    assert "6.375%" in html
    assert "6.500%" in html
    assert "30 years (360 mos)" in html
    assert "Fixed-Rate Conforming Mortgage (Purchase)" in html

    # Discloses scenario assumptions next to grid
    assert "$500,000" in html
    assert "$400,000" in html
    assert "80.000%" in html
    assert "780" in html

    # 2. Incomparable / Incomplete section contains Gamma and Delta with plain reasons
    assert "Incomparable or Incomplete Offers" in html
    assert "Gamma Community Credit Union" in html
    assert "Decision D-05" in html or "Lacks published APR disclosure" in html

    assert "Delta Credit Union" in html
    assert "Requires automatic payment discount" in html or "Conditional discount" in html

    # 3. Withdrawn offer Epsilon MUST NOT appear anywhere on the page
    assert "Epsilon Expired Credit Union" not in html
    assert "offer-epsilon" not in html

    # 4. Sorting behavior
    # Default sort: institution alphabetical ascending (Alpha before Beta)
    alpha_pos = html.index("Alpha Federal Credit Union")
    beta_pos = html.index("Beta State Credit Union")
    assert alpha_pos < beta_pos

    # Sort by APR descending (Beta 6.500% before Alpha 6.250%)
    apr_desc_resp = client.get("/compare?sort=apr_desc")
    apr_desc_html = apr_desc_resp.get_data(as_text=True)
    b_pos = apr_desc_html.index("Beta State Credit Union")
    a_pos = apr_desc_html.index("Alpha Federal Credit Union")
    assert b_pos < a_pos

    # Sort by APR ascending (Alpha 6.250% before Beta 6.500%)
    apr_asc_resp = client.get("/compare?sort=apr")
    apr_asc_html = apr_asc_resp.get_data(as_text=True)
    assert apr_asc_html.index("Alpha Federal Credit Union") < apr_asc_html.index(
        "Beta State Credit Union"
    )

    # 5. Accessibility requirements
    doc = parse_page(html)
    assert len(doc.tables) >= 1
    assert len(doc.table_captions) >= 1
    for caption in doc.table_captions:
        assert caption != ""
    assert len(doc.th_tags) >= 8
    for th in doc.th_tags:
        assert th.get("scope") in ("col", "row")

    # Heading hierarchy
    assert len(doc.h1_list) == 1
    assert doc.headings[0] == "h1"

    # 6. Forbidden ranking words absent
    assert forbidden_ranking_problems(html) == []


def test_compare_freshness_degradation_and_expiry(tmp_path: Path) -> None:
    """Offers degrade to browse-only after 72h and withdraw after 96h or valid_until."""
    now = make.EVALUATED_AT
    item = _make_candidate(
        "offer-decay",
        "Decay Federal Credit Union",
        note_rate_str="6.125",
        apr_str="6.250",
        validity=timedelta(hours=90),
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

    # 2. Degraded read (> 72h, < 90h): degraded to BROWSE_ONLY and separated
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

    # 3. Withdrawn / expired read (>= valid_until or > 96h): excluded completely from page
    withdrawn_app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(publisher.root),
            "DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": make.T0 + timedelta(hours=90),
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


def test_guard_incomparable_offers_never_classified_as_comparable() -> None:
    """Offers without APR, with degraded freshness, or with conditions never enter main grid."""
    from cu_rates.web.comparison import classify_and_format_offer

    now = make.EVALUATED_AT
    # 1. Missing APR offer
    candidate_no_apr = _make_candidate(
        "no-apr-guard", "Test CU", note_rate_str="6.000", apr_str=None, now=now
    )
    # Convert candidate's NormalizedOffer to PublicOffer-like inspection
    from cu_rates.snapshot.builder import _public_projection

    pub_no_apr = _public_projection(candidate_no_apr, Placement.BROWSE_ONLY, False)
    formatted_no_apr = classify_and_format_offer(pub_no_apr, now)
    assert formatted_no_apr is not None
    assert not formatted_no_apr.is_comparable
    assert "Decision D-05" in formatted_no_apr.separation_reason

    # 2. Conditional autopay discount
    cond = OfferCondition(
        condition_id="c-guard",
        kind=ConditionKind.AUTOPAY,
        description="autopay discount",
        provenance=make.provenance("autopay discount"),
        scenario_state=MatchState.CONFLICTS,
    )
    candidate_cond = _make_candidate(
        "cond-guard", "Test CU", note_rate_str="6.000", apr_str="6.100", conditions=(cond,), now=now
    )
    pub_cond = _public_projection(candidate_cond, Placement.BROWSE_ONLY, False)
    formatted_cond = classify_and_format_offer(pub_cond, now)
    assert formatted_cond is not None
    assert not formatted_cond.is_comparable
    assert "Conditional discount" in formatted_cond.separation_reason

    # 3. Degraded freshness (> 72 hours)
    candidate_degraded = _make_candidate(
        "degraded-guard",
        "Test CU",
        note_rate_str="6.000",
        apr_str="6.100",
        validity=timedelta(hours=90),
        now=now,
    )
    pub_degraded = _public_projection(candidate_degraded, Placement.BROWSE_ONLY, False)
    # Evaluate at 73 hours after observation
    formatted_degraded = classify_and_format_offer(pub_degraded, make.T0 + timedelta(hours=73))
    assert formatted_degraded is not None
    assert not formatted_degraded.is_comparable
    assert "Degraded freshness" in formatted_degraded.separation_reason

    # 4. Stale offer (> 96 hours): excluded completely
    formatted_stale = classify_and_format_offer(pub_degraded, make.T0 + timedelta(hours=97))
    assert formatted_stale is None

"""Offline acceptance and foundation tests for the CU-Rates.org website."""

import importlib
import re
from html.parser import HTMLParser
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.web import create_app

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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k: v or "" for k, v in attrs}

        if tag == "html":
            self.html_lang = attr_dict.get("lang")
        elif tag == "meta":
            self.meta_tags.append(attr_dict)
        elif tag == "h1":
            self.in_h1 = True
            self.current_h1 = []
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
        elif tag == "a" and self.current_link_attrs is not None:
            self.links.append((self.current_link_attrs, "".join(self.current_link_text).strip()))
            self.current_link_attrs = None
            self.current_link_text = []

    def handle_data(self, data: str) -> None:
        if self.in_h1:
            self.current_h1.append(data)
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
    test_paths = [*VERIFIED_PAGES, "/arbitrary-404-path"]
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
    test_paths = [*VERIFIED_PAGES, "/missing-page"]
    for path in test_paths:
        resp = client.get(path)
        text = resp.get_data(as_text=True).lower()
        for phrase in disclaimer_phrases:
            assert phrase in text, f"Missing disclaimer phrase '{phrase}' on {path}"


def test_no_external_urls_in_rendered_pages_or_headers(client: FlaskClient) -> None:
    """No rendered page contains external links, scripts, or assets."""
    canonical_origin = "https://cu-rates.org"
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        doc = parse_page(resp.get_data(as_text=True))

        for tag, url in doc.resource_urls:
            if (
                url
                and not url.startswith("#")
                and (url.startswith("http://") or url.startswith("https://"))
            ):
                assert url.startswith(canonical_origin), (
                    f"External URL found in <{tag}> on {path}: {url}"
                )


def test_analytics_disabled_by_default(client: FlaskClient) -> None:
    """Analytics and tracking tags are off by default with zero external requests."""
    for path in VERIFIED_PAGES:
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert "gtag" not in text
        assert "googletagmanager" not in text
        assert "google-analytics" not in text


def test_optional_analytics_consent_integration_point() -> None:
    """When enabled by config, Consent Mode v2 default denials are rendered without live tags."""
    analytics_app = create_app(
        {
            "TESTING": True,
            "ENABLE_ANALYTICS": True,
            "GA_MEASUREMENT_ID": "G-MEASURETEST",
        }
    )
    test_client = analytics_app.test_client()
    resp = test_client.get("/")
    text = resp.get_data(as_text=True)
    assert "gtag('consent', 'default'" in text
    assert "'analytics_storage': 'denied'" in text
    # No live external google script loaded even when enabled
    assert "googletagmanager.com/gtag/js" not in text


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
    rate_pattern = re.compile(r"\b\d+(\.\d+)?%\b")
    apr_pattern = re.compile(r"(?i)\bapr\b\s*:?\s*\d+")

    for path in VERIFIED_PAGES:
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert not rate_pattern.search(text), f"Found rate/percentage pattern on {path}"
        assert not apr_pattern.search(text), f"Found APR with digits pattern on {path}"


def test_web_package_does_not_import_database() -> None:
    """The web layer never imports sqlite, sqlalchemy, or worker database components."""
    forbidden = ["sqlite3", "sqlalchemy", "cu_rates.artifact_store"]

    importlib.invalidate_caches()
    import cu_rates.web.app
    import cu_rates.web.views

    web_modules = [
        cu_rates.web,
        cu_rates.web.app,
        cu_rates.web.views,
    ]

    for mod in web_modules:
        mod_file = getattr(mod, "__file__", None)
        if mod_file and mod_file.endswith(".py"):
            with open(mod_file, encoding="utf-8") as f:
                content = f.read()
            for term in forbidden:
                assert term not in content, f"Forbidden database import '{term}' in {mod_file}"

    for mod in web_modules:
        for term in ["sqlite3", "sqlalchemy"]:
            assert term not in mod.__dict__

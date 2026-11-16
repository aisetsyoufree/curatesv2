"""Offline acceptance and educational content tests for CU-Rates.org."""

import re
from html.parser import HTMLParser
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.web import create_app
from cu_rates.web.views import ARTICLES

FORBIDDEN_ADVICE_PHRASES = [
    "you should",
    "best rate",
    "we recommend",
    "guaranteed",
]

UNVERIFIED_CITATION_MARKER = "unverified - reviewer must confirm the link and the claim"


class LearnPageInspector(HTMLParser):
    """HTML inspector for educational pages testing accessibility, landmarks, and assets."""

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
        self.asset_urls: list[tuple[str, str]] = []

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
        elif tag == "img":
            self.images.append(attr_dict)
            if "src" in attr_dict:
                self.asset_urls.append(("img", attr_dict["src"]))
        elif tag == "script" and "src" in attr_dict:
            self.asset_urls.append(("script", attr_dict["src"]))
        elif tag == "link" and attr_dict.get("rel") in ("stylesheet", "icon", "preload"):
            if "href" in attr_dict:
                self.asset_urls.append(("link", attr_dict["href"]))
        elif tag == "iframe" and "src" in attr_dict:
            self.asset_urls.append(("iframe", attr_dict["src"]))

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


def parse_learn_page(html: str) -> LearnPageInspector:
    """Parse HTML and return inspector."""
    parser = LearnPageInspector()
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


def test_learn_index_and_articles_return_200_with_headers(client: FlaskClient) -> None:
    """The learn index and articles return 200 with strict CSP and security headers."""
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]

    for path in routes:
        resp = client.get(path)
        assert resp.status_code == 200, f"Expected 200 for {path}, got {resp.status_code}"
        assert "text/html" in resp.content_type

        # Content-Security-Policy
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp

        # Security headers
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "geolocation=()" in resp.headers.get("Permissions-Policy", "")
        assert "max-age=31536000" in resp.headers.get("Strict-Transport-Security", "")


def test_draft_banner_and_noindex_on_unreviewed_articles(client: FlaskClient) -> None:
    """Unreviewed articles and learn index display a draft review banner and send noindex."""
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]

    for path in routes:
        resp = client.get(path)
        # Header check
        assert "noindex" in resp.headers.get("X-Robots-Tag", "")

        # HTML meta tag check
        doc = parse_learn_page(resp.get_data(as_text=True))
        robots_meta = [m for m in doc.meta_tags if m.get("name") == "robots"]
        assert len(robots_meta) == 1
        assert "noindex" in robots_meta[0].get("content", "")

        # Draft banner check
        text = resp.get_data(as_text=True)
        assert "Draft" in text
        assert "awaiting factual review" in text


def test_unreviewed_articles_absent_from_sitemap(client: FlaskClient) -> None:
    """Unreviewed articles stay out of /sitemap.xml."""
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    root = ElementTree.fromstring(resp.get_data())
    namespaces = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    loc_elements = root.findall(".//ns:loc", namespaces)
    sitemap_urls = [loc.text for loc in loc_elements if loc.text is not None]

    for slug in ARTICLES:
        article_url = f"https://cu-rates.org/learn/{slug}"
        assert article_url not in sitemap_urls, f"Unreviewed {article_url} found in sitemap"

    assert "https://cu-rates.org/learn" not in sitemap_urls


def test_reviewed_article_included_in_sitemap_and_indexed(client: FlaskClient) -> None:
    """When an article is marked reviewed: True, it appears in sitemap and removes noindex."""
    target_slug = "mortgage-basics"
    try:
        ARTICLES[target_slug]["reviewed"] = True

        resp = client.get(f"/learn/{target_slug}")
        assert "noindex" not in resp.headers.get("X-Robots-Tag", "")
        doc = parse_learn_page(resp.get_data(as_text=True))
        robots_meta = [m for m in doc.meta_tags if m.get("name") == "robots"]
        assert len(robots_meta) == 0
        assert "Draft &mdash; awaiting factual review" not in resp.get_data(as_text=True)

        sitemap_resp = client.get("/sitemap.xml")
        root = ElementTree.fromstring(sitemap_resp.get_data())
        namespaces = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [loc.text for loc in root.findall(".//ns:loc", namespaces) if loc.text is not None]
        assert f"https://cu-rates.org/learn/{target_slug}" in locs
    finally:
        ARTICLES[target_slug]["reviewed"] = False


def test_no_rates_aprs_or_rate_digits_in_articles(client: FlaskClient) -> None:
    """No digit patterns that look like rates or APRs appear in any educational content."""
    rate_pattern = re.compile(r"\b\d+(\.\d+)?%\b")
    apr_pattern = re.compile(r"(?i)\bapr\b\s*:?\s*\d+")

    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]
    for path in routes:
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert not rate_pattern.search(text), f"Found rate/percentage pattern on {path}"
        assert not apr_pattern.search(text), f"Found APR with digits pattern on {path}"


def test_no_advice_phrases_in_articles(client: FlaskClient) -> None:
    """Articles maintain neutral educational tone and contain no prescriptive advice phrases."""
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]
    for path in routes:
        resp = client.get(path)
        text_lower = resp.get_data(as_text=True).lower()
        for phrase in FORBIDDEN_ADVICE_PHRASES:
            assert phrase not in text_lower, f"Forbidden advice phrase '{phrase}' found on {path}"


def test_every_article_has_marked_citations(client: FlaskClient) -> None:
    """Every article cites authoritative public sources marked for reviewer verification."""
    for slug in ARTICLES:
        path = f"/learn/{slug}"
        resp = client.get(path)
        text = resp.get_data(as_text=True)

        # Check for citation notice and tag
        assert UNVERIFIED_CITATION_MARKER in text, f"Missing unverified citation marker on {path}"
        assert "Authoritative Sources" in text or "Regulatory Guidance" in text

        # Check for at least one outbound citation link
        doc = parse_learn_page(text)
        citation_links = [
            (attrs, text)
            for attrs, text in doc.links
            if attrs.get("href", "").startswith("http://")
            or attrs.get("href", "").startswith("https://")
            or attrs.get("href", "") == "/methodology"
        ]
        assert len(citation_links) >= 1, f"No citation links found on {path}"


def test_correction_process_explained_without_personal_contact_data(
    client: FlaskClient,
) -> None:
    """Data collection article explains the correction procedure without inventing emails/phones."""
    resp = client.get("/learn/collection-and-corrections")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    # Required process elements
    assert "GitHub" in text
    assert "issue tracker" in text.lower() or "discrepancy" in text.lower()
    assert "freshness" in text.lower()
    assert "seventy-two hours" in text.lower() or "72" in text
    assert "ninety-six hours" in text.lower() or "96" in text

    # No invented email addresses, phone numbers, or physical mailing addresses
    email_pattern = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
    phone_pattern = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
    assert not email_pattern.search(text), "Found email address in correction text"
    assert not phone_pattern.search(text), "Found phone number in correction text"


def test_accessibility_basics_on_all_learn_pages(client: FlaskClient) -> None:
    """All educational pages meet basic accessibility standards."""
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]

    for path in routes:
        resp = client.get(path)
        doc = parse_learn_page(resp.get_data(as_text=True))

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

        # Links non-empty
        assert len(doc.links) > 0
        for attrs, text in doc.links:
            assert text != "", f"Empty link found on {path}: {attrs}"

        # Images have alt attribute if any exist
        for img in doc.images:
            assert "alt" in img, f"Image without alt found on {path}: {img}"


def test_no_external_assets_loaded(client: FlaskClient) -> None:
    """Educational articles do not embed external scripts, styles, images, or iframes."""
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]

    for path in routes:
        resp = client.get(path)
        doc = parse_learn_page(resp.get_data(as_text=True))

        for tag, url in doc.asset_urls:
            assert not url.startswith("http://") and not url.startswith("https://"), (
                f"External asset found in <{tag}> on {path}: {url}"
            )


def test_unknown_article_returns_404(client: FlaskClient) -> None:
    """Requesting an unknown educational article returns 404 with noindex."""
    resp = client.get("/learn/non-existent-article-slug")
    assert resp.status_code == 404
    assert "noindex" in resp.headers.get("X-Robots-Tag", "")

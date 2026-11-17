"""Offline acceptance and educational content tests for CU-Rates.org."""

import re
from html.parser import HTMLParser
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.web import create_app
from cu_rates.web.views import ARTICLES
from tests.site_audit import (
    RATE_FIGURE,
    csp_problems,
    publication_problems,
    source_section_problems,
)

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
        assert csp_problems(csp) == []

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
    apr_pattern = re.compile(r"(?i)\bapr\b\s*:?\s*\d+")

    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]
    for path in routes:
        resp = client.get(path)
        text = resp.get_data(as_text=True)
        assert not RATE_FIGURE.search(text), f"Found rate/percentage pattern on {path}"
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

        # The sources section must keep a marked external citation.
        assert UNVERIFIED_CITATION_MARKER in text, f"Missing unverified citation marker on {path}"
        assert "Authoritative Sources" in text or "Regulatory Guidance" in text
        origin = str(client.application.config["CANONICAL_ORIGIN"])
        assert source_section_problems(text, origin) == [], path


def test_correction_process_and_planned_channel_specifications(
    client: FlaskClient,
) -> None:
    """Data collection page describes planned policies, correction channel, and warnings."""
    resp = client.get("/learn/collection-and-corrections")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    text_lower = text.lower()

    # Warning against submitting personal financial data
    assert "must not submit" in text_lower
    assert "account numbers" in text_lower
    assert "financial records" in text_lower
    assert "personal data" in text_lower

    # No GitHub issue-tracker instructions
    assert "github" not in text_lower

    # No present-tense claims regarding active collection
    assert "parser halts" not in text_lower
    assert "are inspected on a daily schedule" not in text_lower

    # Future / conditional planned wording and availability status
    assert "will" in text_lower
    assert "is designed to" in text_lower or "once collection is live" in text_lower
    assert "not yet available" in text_lower

    # Bounded freshness policy from methodology
    assert "freshness" in text_lower
    assert "seventy-two hours" in text_lower or "72-hour" in text_lower
    assert "degrade" in text_lower
    assert "ninety-six hours" in text_lower or "96-hour" in text_lower
    assert "withdrawal" in text_lower

    # Planned correction channel specifications per MASTER section 5
    assert "source url" in text_lower
    assert "affected field" in text_lower
    assert "reason" in text_lower
    assert "evidence" in text_lower
    assert "correction case" in text_lower or "recorded case" in text_lower
    assert "one business day" in text_lower
    assert "five business days" in text_lower
    assert "operational target" in text_lower

    # No invented contact info (emails, phone numbers, physical addresses, external forms)
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
    """Educational articles do not embed external scripts, styles, images, or header preloads."""
    origin = str(client.application.config["CANONICAL_ORIGIN"])
    routes = ["/learn", *[f"/learn/{slug}" for slug in ARTICLES]]

    for path in routes:
        resp = client.get(path)
        headers = {key: value for key, value in resp.headers.items()}
        assert (
            publication_problems(
                resp.get_data(as_text=True),
                headers,
                origin,
                allow_external_anchors=True,
            )
            == []
        )


def test_unknown_article_returns_404(client: FlaskClient) -> None:
    """Requesting an unknown educational article returns 404 with noindex."""
    resp = client.get("/learn/non-existent-article-slug")
    assert resp.status_code == 404
    assert "noindex" in resp.headers.get("X-Robots-Tag", "")


def test_collection_page_matches_source_approval_and_retention_limits(
    client: FlaskClient,
) -> None:
    """Collection copy allows approved providers and does not promise instant deletion."""
    text = client.get("/learn/collection-and-corrections").get_data(as_text=True)
    lowered = text.lower()
    assert "approved provider" in lowered
    assert "exclusively from original institution" not in lowered
    assert "never linger" not in lowered
    assert "seventy-two hours" in lowered
    assert "ninety-six hours" in lowered
    assert "as soon as it can be handled safely" in lowered
    assert "deleted immediately" not in lowered


def test_loan_estimate_deadline_names_covered_loans(client: FlaskClient) -> None:
    """The Loan Estimate sentence uses the covered-loan timing from the disclosure rule."""
    text = client.get("/learn/limits-of-comparison").get_data(as_text=True).lower()
    assert "within three business days" in text
    assert "covered loans" in text
    assert "three business days after" not in text


def test_learn_index_headings_draft_labels_and_aria_current(client: FlaskClient) -> None:
    """Article titles are nested h3s, draft labels follow review, and aria-current is exact."""
    html = client.get("/learn").get_data(as_text=True)
    assert html.count("<h3>") == len(ARTICLES)
    assert "<h2><a" not in html
    document = parse_learn_page(html)
    current = [text for attrs, text in document.links if attrs.get("aria-current") == "page"]
    assert current == ["Learn"]

    article = client.get("/learn/limits-of-comparison")
    article_html = article.get_data(as_text=True)
    article_doc = parse_learn_page(article_html)
    assert all(attrs.get("aria-current") != "page" for attrs, _text in article_doc.links)
    assert '<span aria-current="page">' in article_html


def test_reviewed_article_drops_its_draft_label(client: FlaskClient) -> None:
    """A reviewed article is not labeled as a draft on the index or on its page."""
    slug = "mortgage-basics"
    try:
        ARTICLES[slug]["reviewed"] = True
        html = client.get("/learn").get_data(as_text=True)
        for part in html.split('<li class="article-item">')[1:]:
            chunk = part.split("</li>", 1)[0]
            if f"/learn/{slug}" in chunk:
                assert "Draft - awaiting factual review" not in chunk
            else:
                assert "Draft - awaiting factual review" in chunk
        page = client.get(f"/learn/{slug}").get_data(as_text=True)
        assert "Draft - awaiting factual review" not in page
    finally:
        ARTICLES[slug]["reviewed"] = False

"""Web views for CU-Rates.org."""

from typing import Any, TypedDict

from flask import Blueprint, Response, abort, current_app, render_template, request

web_bp = Blueprint("web", __name__)

# Review registry for educational articles: drafting only awaiting human factual review.
# Each entry is unreviewed by default (reviewed: False) and carries noindex until verified.
ARTICLES: dict[str, dict[str, Any]] = {
    "mortgage-basics": {
        "slug": "mortgage-basics",
        "title": "Mortgage Basics and Core Principles",
        "summary": (
            "An introduction to residential mortgage loans, principal, "
            "interest, terms, and amortization."
        ),
        "template": "learn/mortgage_basics.html",
        "reviewed": False,
    },
    "purchase-vs-refinance": {
        "slug": "purchase-vs-refinance",
        "title": "Purchase vs. Refinance: Why Refinance Is a Purpose, Not a Product",
        "summary": (
            "Understanding why refinancing describes the purpose of a mortgage "
            "rather than a distinct product family."
        ),
        "template": "learn/purchase_vs_refinance.html",
        "reviewed": False,
    },
    "home-equity-vs-heloc": {
        "slug": "home-equity-vs-heloc",
        "title": "Home Equity Loans vs. HELOCs: Closed-End Loans vs. Open-End Lines",
        "summary": (
            "Examining the differences between lump-sum closed-end fixed equity loans "
            "and revolving lines of credit."
        ),
        "template": "learn/home_equity_vs_heloc.html",
        "reviewed": False,
    },
    "mortgage-points": {
        "slug": "mortgage-points",
        "title": "Mortgage Points and Upfront Financing Costs",
        "summary": (
            "How discount points and origination points affect upfront cash requirements "
            "and interest rates."
        ),
        "template": "learn/mortgage_points.html",
        "reviewed": False,
    },
    "apr-vs-note-rate": {
        "slug": "apr-vs-note-rate",
        "title": "APR vs. Note Rate: Why They Are Never Interchangeable",
        "summary": "Distinguishing the underlying note rate from the Annual Percentage Rate (APR).",
        "template": "learn/apr_vs_note_rate.html",
        "reviewed": False,
    },
    "loan-eligibility": {
        "slug": "loan-eligibility",
        "title": "Loan Eligibility: Why an Advertised Rate May Not Apply to You",
        "summary": (
            "How credit scores, debt-to-income ratios, equity, and underwriting criteria "
            "affect loan terms."
        ),
        "template": "learn/loan_eligibility.html",
        "reviewed": False,
    },
    "limits-of-comparison": {
        "slug": "limits-of-comparison",
        "title": "The Limits of Shopping and Comparing Advertised Rates",
        "summary": (
            "Why advertised headline rates reflect specific hypothetical borrower "
            "scenarios rather than universal quotes."
        ),
        "template": "learn/limits_of_comparison.html",
        "reviewed": False,
    },
    "collection-and-corrections": {
        "slug": "collection-and-corrections",
        "title": "Data Collection, Freshness, and Requesting Corrections",
        "summary": (
            "How CU-Rates.org collects public data, enforces freshness limits, "
            "and processes data corrections."
        ),
        "template": "learn/collection_and_corrections.html",
        "reviewed": False,
    },
}


class Crumb(TypedDict):
    name: str
    path: str


class Seo(TypedDict):
    title: str
    description: str
    canonical: str
    breadcrumbs: list[Crumb]
    json_ld: dict[str, Any]


_PAGE_META: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    "web.home": (
        "Massachusetts Credit Union Residential Lending Directory",
        "An educational directory of publicly advertised Massachusetts credit union "
        "residential lending terms. Not a lender and not personalized financial advice.",
        (("Home", "/"),),
    ),
    "web.methodology": (
        "Methodology and Comparison Principles",
        "How CU-Rates.org plans to compare residential lending terms using provenance, "
        "declared scenarios, freshness windows, and corrections.",
        (("Home", "/"), ("Methodology and Comparison Principles", "/methodology")),
    ),
    "web.privacy": (
        "Privacy Policy and Consent Notice",
        "How CU-Rates.org handles privacy and server logs, and the planned status of "
        "analytics consent.",
        (("Home", "/"), ("Privacy Policy and Consent Notice", "/privacy")),
    ),
    "web.learn_index": (
        "Educational Guides and Lending Concepts",
        "Educational guides to residential mortgage and home equity concepts. "
        "Unreviewed articles stay out of the search index.",
        (("Home", "/"), ("Learn", "/learn")),
    ),
}


def _origin() -> str:
    origin = current_app.config.get("CANONICAL_ORIGIN")
    if not isinstance(origin, str) or not origin:
        raise RuntimeError("CANONICAL_ORIGIN is not configured")
    return origin


def _json_ld(
    origin: str,
    title: str,
    description: str,
    canonical: str,
    crumbs: list[Crumb],
) -> dict[str, Any]:
    """Conservative WebPage markup: identity, description, and breadcrumbs only."""
    items: list[dict[str, Any]] = []
    for position, crumb in enumerate(crumbs, start=1):
        items.append(
            {
                "@type": "ListItem",
                "position": position,
                "name": crumb["name"],
                "item": f"{origin}{crumb['path']}",
            }
        )
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "url": canonical,
        "description": description,
        "inLanguage": "en",
        "isPartOf": {
            "@type": "WebSite",
            "name": "CU-Rates.org",
            "url": f"{origin}/",
        },
        "breadcrumb": {
            "@type": "BreadcrumbList",
            "itemListElement": items,
        },
    }


def seo_for_request() -> Seo | None:
    """Build canonical, Open Graph, breadcrumb, and JSON-LD data for a real page."""
    endpoint = request.endpoint
    title: str
    description: str
    crumb_pairs: tuple[tuple[str, str], ...]
    if endpoint == "web.learn_article":
        slug = str((request.view_args or {}).get("slug", ""))
        article = ARTICLES.get(slug)
        if article is None:
            return None
        title = str(article["title"])
        description = str(article["summary"])
        path = f"/learn/{slug}"
        crumb_pairs = (("Home", "/"), ("Learn", "/learn"), (title, path))
    elif endpoint in _PAGE_META:
        title, description, crumb_pairs = _PAGE_META[endpoint]
    else:
        return None

    origin = _origin()
    crumbs = [Crumb(name=name, path=path) for name, path in crumb_pairs]
    canonical = f"{origin}{crumbs[-1]['path']}"
    return Seo(
        title=title,
        description=description,
        canonical=canonical,
        breadcrumbs=crumbs,
        json_ld=_json_ld(origin, title, description, canonical, crumbs),
    )


@web_bp.route("/")
def home() -> str:
    """Render directory home page."""
    return render_template("home.html")


@web_bp.route("/methodology")
def methodology() -> str:
    """Render comparison methodology and principles page."""
    return render_template("methodology.html")


@web_bp.route("/privacy")
def privacy() -> str:
    """Render privacy policy and consent page."""
    return render_template("privacy.html")


@web_bp.route("/learn")
def learn_index() -> Response:
    """Render educational content index."""
    articles = list(ARTICLES.values())
    rendered = render_template(
        "learn/index.html",
        articles=articles,
        is_draft=any(not bool(item.get("reviewed", False)) for item in articles),
    )
    return Response(rendered, headers={"X-Robots-Tag": "noindex, follow"})


@web_bp.route("/learn/<slug>")
def learn_article(slug: str) -> Response:
    """Render an individual educational article."""
    article = ARTICLES.get(slug)
    if article is None:
        abort(404)

    is_reviewed = bool(article.get("reviewed", False))
    headers: dict[str, str] = {}
    if not is_reviewed:
        headers["X-Robots-Tag"] = "noindex, follow"

    rendered = render_template(
        article["template"],
        article=article,
        is_draft=not is_reviewed,
    )
    return Response(rendered, headers=headers)


@web_bp.route("/robots.txt")
def robots_txt() -> Response:
    """Serve robots.txt file pointing to sitemap."""
    origin = _origin()
    content = f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n"
    return Response(content, mimetype="text/plain")


@web_bp.route("/sitemap.xml")
def sitemap_xml() -> Response:
    """Serve sitemap.xml with verified canonical pages only."""
    origin = _origin()
    urls = [
        f"{origin}/",
        f"{origin}/methodology",
        f"{origin}/privacy",
    ]
    # Unreviewed articles remain excluded from sitemap until verified
    for slug, meta in ARTICLES.items():
        if meta.get("reviewed", False):
            urls.append(f"{origin}/learn/{slug}")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        lines.append(f"  <url><loc>{url}</loc></url>")
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml")

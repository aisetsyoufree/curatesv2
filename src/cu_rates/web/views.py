"""Web views for CU-Rates.org."""

import ipaddress
import secrets
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    render_template,
    request,
    url_for,
)

from cu_rates.contracts.enums import ProductFamily
from cu_rates.intake import IntakeError, IntakeStore, NewCase, ReporterCategory, ReportKind
from cu_rates.web.comparison import (
    ALLOWED_PARAMS,
    ALLOWED_SCENARIOS,
    ALLOWED_SORTS,
    CANONICAL_SCENARIO_SPECS,
    SCENARIOS_BY_ID,
    SORT_LABELS,
    FormattedOffer,
    classify_and_format_offer,
    read_snapshot_offers,
    sort_offers,
)
from cu_rates.web.consent import CONSENT_COOKIE, analytics_ready

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
    "web.corrections": (
        "Report a Correction or Problem",
        "How to tell CU-Rates.org that a rate, credit union, or product shown here is wrong, "
        "and what happens to your report. Please do not send personal or account details.",
        (("Home", "/"), ("Report a Correction or Problem", "/corrections")),
    ),
    "web.methodology": (
        "Methodology and Comparison Principles",
        "How CU-Rates.org plans to compare residential lending terms using provenance, "
        "declared scenarios, freshness windows, and corrections.",
        (("Home", "/"), ("Methodology and Comparison Principles", "/methodology")),
    ),
    "web.privacy": (
        "Privacy Policy and Consent Notice",
        "How CU-Rates.org handles privacy, server logs, and optional analytics consent.",
        (("Home", "/"), ("Privacy Policy and Consent Notice", "/privacy")),
    ),
    "web.learn_index": (
        "Educational Guides and Lending Concepts",
        "Educational guides to residential mortgage and home equity concepts. "
        "Unreviewed articles stay out of the search index.",
        (("Home", "/"), ("Learn", "/learn")),
    ),
    "web.compare": (
        "Compare Residential Lending Terms",
        "Compare publicly advertised mortgage and home equity terms across Massachusetts "
        "credit unions under declared canonical scenarios.",
        (("Home", "/"), ("Compare", "/compare")),
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


@web_bp.post("/privacy/consent")
def analytics_consent() -> Response:
    """Record a deliberate same-origin choice without sessions or pre-choice cookies."""
    if not analytics_ready():
        abort(404)
    # HTTPS form POSTs supply Origin. Missing/foreign origins cannot opt a visitor in.
    if request.headers.get("Origin") != current_app.config["CANONICAL_ORIGIN"]:
        abort(400)
    if set(request.form) != {"choice"} or len(request.form.getlist("choice")) != 1:
        abort(400)
    choice = request.form["choice"]
    if choice not in {"granted", "denied"}:
        abort(400)
    response = current_app.response_class(status=303)
    response.headers["Location"] = url_for("web.privacy", _anchor="analytics-choice")
    response.set_cookie(
        CONSENT_COOKIE,
        choice,
        max_age=60 * 60 * 24 * 180,
        secure=True,
        httponly=True,
        samesite="Strict",
    )
    if choice == "denied":
        # Clear GA cookies scoped to this host and its parent domains. No tag is loaded
        # on the destination page; do not send a cookieless consent-update ping.
        host = request.host.split(":", 1)[0]
        labels = host.split(".")
        domains: list[str | None] = [None]
        domains.extend(".".join(labels[index:]) for index in range(len(labels) - 1))
        for name in request.cookies:
            if name == "_ga" or name.startswith("_ga_"):
                for domain in domains:
                    response.delete_cookie(name, domain=domain, secure=True, samesite="Strict")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, follow"
    return response


# -- Public corrections ----------------------------------------------------------------------
# The route can only ADD a case to its own small private store (INTAKE_DB). It never opens the
# operational database, the artifact store or the snapshot. No sessions and no cookies: the
# only CSRF defence needed is the same Origin check the consent form uses.
CORRECTIONS_MAX_BODY = 16 * 1024
CORRECTIONS_PER_HOUR = 30
CORRECTIONS_PER_CLIENT_PER_HOUR = 3
_CORRECTION_FIELDS = frozenset(
    {"category", "kind", "url", "field", "reason", "evidence", "contact", "website"}
)


def _correction_response(html: str, status: int = 200, *, index: bool = False) -> Response:
    response = current_app.response_class(html, status=status)
    response.headers["Cache-Control"] = "private, no-store"
    if not index:  # only the plain information page is indexable, never a result or an error
        response.headers["X-Robots-Tag"] = "noindex, follow"
    return response


def _render_corrections(
    *,
    error: str | None = None,
    values: Mapping[str, str] | None = None,
    status: int = 200,
    index: bool = False,
) -> Response:
    html = render_template(
        "corrections.html",
        form_open=current_app.config["INTAKE_DB"] is not None,
        error=error,
        values=values or {},
        categories=[(c.value, c.value.replace("_", " ")) for c in ReporterCategory],
        kinds=[(k.value, _KIND_LABELS[k]) for k in ReportKind],
    )
    return _correction_response(html, status, index=index)


_KIND_LABELS = {
    ReportKind.RATE: "A rate, APR, points or fee looks wrong or out of date",
    ReportKind.IDENTITY: "The credit union or product shown is wrong",
    ReportKind.PRIVACY: "A privacy concern",
    ReportKind.SECURITY: "A security concern with this website",
    ReportKind.OTHER: "Something else",
}


@web_bp.get("/corrections")
def corrections() -> Response:
    """Explain how to report a problem and show the form (when intake is configured)."""
    return _render_corrections(index=True)


def _client_key() -> str:
    """Who is posting. Behind the Cloudflare Tunnel (D-09, loopback only) the real client is in
    CF-Connecting-IP; the origin is not reachable any other way, so it cannot be forged. Without
    the header (tests, local runs) fall back to the socket address."""
    forwarded = request.headers.get("CF-Connecting-IP", "").strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return request.remote_addr or "unknown"


def _allow_submission() -> bool:
    """Per-client cap first (one visitor cannot use up everyone's allowance), then a global
    cap so an unmonitored form cannot flood the inbox or the disk."""
    now = time.monotonic()
    clients: dict[str, deque[float]] = current_app.extensions.setdefault("corrections_clients", {})
    recent: deque[float] = current_app.extensions.setdefault("corrections_submissions", deque())
    while recent and now - recent[0] > 3600:
        recent.popleft()
    for stale in [k for k, v in clients.items() if not v or now - v[-1] > 3600]:
        del clients[stale]  # bounded memory: only clients active in the last hour are kept
    mine = clients.setdefault(_client_key(), deque())
    if len(mine) >= CORRECTIONS_PER_CLIENT_PER_HOUR or len(recent) >= CORRECTIONS_PER_HOUR:
        return False
    mine.append(now)
    recent.append(now)
    return True


@web_bp.post("/corrections")
def corrections_submit() -> Response:
    """Accept one correction report. Never echoes or logs the reporter's contact address."""
    db = current_app.config["INTAKE_DB"]
    if db is None:
        return _render_corrections(status=503)
    if request.headers.get("Origin") != current_app.config["CANONICAL_ORIGIN"]:
        abort(400)
    if request.mimetype != "application/x-www-form-urlencoded":
        abort(415)
    if request.content_length is None or request.content_length > CORRECTIONS_MAX_BODY:
        abort(413)
    request.max_content_length = CORRECTIONS_MAX_BODY
    submitted = request.form
    if not set(submitted) <= _CORRECTION_FIELDS or any(
        len(submitted.getlist(name)) != 1 for name in submitted
    ):
        abort(400)
    if submitted.get("website", ""):
        # A hidden field only automation fills in: answer as if it worked, store nothing.
        return _correction_response(
            render_template("corrections_received.html", reference=secrets.token_hex(16))
        )
    echo = {
        name: submitted.get(name, "")
        for name in ("category", "kind", "url", "field", "reason", "evidence")
    }  # the reply contact is deliberately never echoed back
    try:
        case = NewCase(
            reporter_category=ReporterCategory(submitted.get("category", "")),
            kind=ReportKind(submitted.get("kind", "")),
            affected_url=submitted.get("url", ""),
            affected_field=submitted.get("field", ""),
            reason=submitted.get("reason", ""),
            evidence_url=submitted.get("evidence", ""),
            reply_contact=submitted.get("contact", "").strip(),
        )
    except IntakeError as exc:
        return _render_corrections(error=str(exc), values=echo, status=400)
    except ValueError:
        return _render_corrections(
            error="Please choose who you are and what the report is about.",
            values=echo,
            status=400,
        )
    if not _allow_submission():
        response = _render_corrections(
            error="Too many reports were received just now. Please try again in an hour.",
            values=echo,
            status=429,
        )
        response.headers["Retry-After"] = "3600"
        return response
    try:
        reference = IntakeStore(Path(db)).record_case(case, now=datetime.now(UTC))
    except IntakeError:
        return _render_corrections(
            error="Reports cannot be accepted right now. Please try again later.",
            values=echo,
            status=503,
        )
    return _correction_response(render_template("corrections_received.html", reference=reference))


MAX_COMPARE_QUERY_BYTES = 2048
MAX_COMPARE_QUERY_PARAMS = 10


@web_bp.route("/compare")
def compare() -> Response:
    """Render the rate comparison experience reading only from the published snapshot."""
    if len(request.query_string) > MAX_COMPARE_QUERY_BYTES:
        abort(400)

    if len(request.args) > MAX_COMPARE_QUERY_PARAMS:
        abort(400)

    for param in request.args:
        if len(request.args.getlist(param)) > 1:
            abort(400)
        if param not in ALLOWED_PARAMS:
            abort(400)

    scenario_param = request.args.get("scenario", "all")
    if scenario_param not in ALLOWED_SCENARIOS:
        abort(400)

    sort_param = request.args.get("sort", "institution")
    if sort_param not in ALLOWED_SORTS:
        abort(400)

    now = datetime.now(UTC)
    clock_override = current_app.config.get("SNAPSHOT_CLOCK")
    if isinstance(clock_override, datetime):
        now = clock_override
    if now.utcoffset() is None:
        now = now.replace(tzinfo=UTC)

    info, raw_offers = read_snapshot_offers(at=now)

    formatted_offers = []
    for raw in raw_offers:
        item = classify_and_format_offer(raw, now)
        if item is not None:
            formatted_offers.append(item)

    has_published_data = len(formatted_offers) > 0

    selected_specs = (
        CANONICAL_SCENARIO_SPECS
        if scenario_param == "all"
        else ((SCENARIOS_BY_ID[scenario_param],) if scenario_param in SCENARIOS_BY_ID else ())
    )

    scenario_sections: list[dict[str, Any]] = []
    for spec in selected_specs:
        scenario_offers = [
            o
            for o in formatted_offers
            if o.scenario_id == spec.scenario_id
            or (o.scenario_id is None and o.family == spec.family.value)
        ]
        comparable = [o for o in scenario_offers if o.is_comparable]
        incomparable = [o for o in scenario_offers if not o.is_comparable]

        # Group identical structures for HELOC before sorting; never mix structures in one grid
        structure_groups = []
        if spec.family is ProductFamily.HELOC:
            groups_map: dict[tuple[int | None, int | None], list[FormattedOffer]] = {}
            for o in comparable:
                key = (o.draw_period_months, o.repayment_period_months)
                groups_map.setdefault(key, []).append(o)
            for _key, group_offers in sorted(
                groups_map.items(),
                key=lambda item: (item[0][0] or 0, item[0][1] or 0),
            ):
                label = group_offers[0].structure_label if group_offers else ""
                structure_groups.append(
                    {
                        "label": label,
                        "offers": sort_offers(group_offers, sort_param),
                    }
                )
        else:
            if comparable:
                structure_groups.append(
                    {
                        "label": "",
                        "offers": sort_offers(comparable, sort_param),
                    }
                )

        # Incomparable/incomplete sections keep neutral institution ordering
        # regardless of sort parameters (Finding 4)
        sorted_incomparable = sort_offers(incomparable, "institution")
        represented_institutions = tuple(sorted({o.institution_name for o in comparable}))

        scenario_sections.append(
            {
                "spec": spec,
                "structure_groups": structure_groups,
                "comparable": sort_offers(comparable, sort_param),
                "incomparable": sorted_incomparable,
                "comparable_count": len(comparable),
                "incomparable_count": len(incomparable),
                "total_count": len(scenario_offers),
                "represented_institutions": represented_institutions,
                "institution_count": len(represented_institutions),
            }
        )

    total_displayed = sum(int(s["total_count"]) for s in scenario_sections)
    noindex = (not has_published_data) or (total_displayed == 0)

    headers: dict[str, str] = {}
    if noindex:
        headers["X-Robots-Tag"] = "noindex, follow"

    rendered = render_template(
        "compare.html",
        has_published_data=has_published_data,
        scenario_param=scenario_param,
        sort_param=sort_param,
        active_sort_label=SORT_LABELS.get(sort_param, "Institution Name (A-Z)"),
        sort_options=SORT_LABELS,
        scenario_sections=scenario_sections,
        all_scenario_specs=CANONICAL_SCENARIO_SPECS,
        snapshot_info=info,
        noindex=noindex,
        total_displayed=total_displayed,
    )
    return Response(rendered, headers=headers)


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
        f"{origin}/corrections",
    ]
    # Include /compare only when display-eligible offers exist (same predicate as noindex)
    now = datetime.now(UTC)
    clock_override = current_app.config.get("SNAPSHOT_CLOCK")
    if isinstance(clock_override, datetime):
        now = clock_override
    if now.utcoffset() is None:
        now = now.replace(tzinfo=UTC)

    _info, raw_offers = read_snapshot_offers(at=now)
    if any(classify_and_format_offer(raw, now) is not None for raw in raw_offers):
        urls.append(f"{origin}/compare")

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

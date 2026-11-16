"""Web views for CU-Rates.org."""

from flask import Blueprint, Response, render_template

web_bp = Blueprint("web", __name__)


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


@web_bp.route("/robots.txt")
def robots_txt() -> Response:
    """Serve robots.txt file pointing to sitemap."""
    content = "User-agent: *\nAllow: /\nSitemap: https://cu-rates.org/sitemap.xml\n"
    return Response(content, mimetype="text/plain")


@web_bp.route("/sitemap.xml")
def sitemap_xml() -> Response:
    """Serve sitemap.xml with verified canonical pages only."""
    urls = [
        "https://cu-rates.org/",
        "https://cu-rates.org/methodology",
        "https://cu-rates.org/privacy",
    ]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        lines.append(f"  <url><loc>{url}</loc></url>")
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml")

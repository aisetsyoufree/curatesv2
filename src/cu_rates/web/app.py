"""Application factory for the CU-Rates.org website."""

import re
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, render_template

from cu_rates.web.views import seo_for_request, web_bp

_ORIGIN_HOST = re.compile(r"[a-z0-9.-]+")


def validate_canonical_origin(value: object) -> str:
    """Return a bare https origin, or raise ValueError if the setting is unusable."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("CANONICAL_ORIGIN must be an https origin")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("CANONICAL_ORIGIN must be an https origin")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("CANONICAL_ORIGIN must not include credentials, a query, or a fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("CANONICAL_ORIGIN must not include a path")
    host = parts.hostname
    if (
        _ORIGIN_HOST.fullmatch(host) is None
        or "." not in host
        or ".." in host
        or host.startswith((".", "-"))
        or host.endswith((".", "-"))
    ):
        raise ValueError("CANONICAL_ORIGIN host is not a bare DNS name")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("CANONICAL_ORIGIN port is invalid") from exc
    if port == 443:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return f"https://{netloc}"


def create_app(config: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask web application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.config.setdefault("TESTING", False)
    app.config.setdefault("ENABLE_ANALYTICS", False)
    app.config.setdefault("GA_MEASUREMENT_ID", None)
    app.config.setdefault("CANONICAL_ORIGIN", "https://cu-rates.org")
    app.config.setdefault("SNAPSHOT_ROOT", None)
    app.config.setdefault("DENYLIST_PATH", None)
    app.config.setdefault("SNAPSHOT_CLOCK", None)

    if config:
        app.config.update(config)
    app.config["CANONICAL_ORIGIN"] = validate_canonical_origin(app.config.get("CANONICAL_ORIGIN"))

    app.register_blueprint(web_bp)

    @app.context_processor
    def inject_seo() -> dict[str, object]:
        return {"seo": seo_for_request()}

    @app.errorhandler(404)
    def page_not_found(error: object) -> tuple[str, int, dict[str, str]]:
        return render_template("404.html"), 404, {"X-Robots-Tag": "noindex, follow"}

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self'; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), camera=(), microphone=(), payment=()",
        )
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    return app

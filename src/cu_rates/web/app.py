"""Application factory for the CU-Rates.org website."""

from typing import Any

from flask import Flask, Response, render_template, request

from cu_rates.web.config import config_from_environment, validated_config
from cu_rates.web.config import validate_canonical_origin as validate_canonical_origin
from cu_rates.web.consent import CONSENT_COOKIE, analytics_ready
from cu_rates.web.views import seo_for_request, web_bp


def create_app(config: dict[str, Any] | None = None) -> Flask:
    """Create and configure the Flask web application."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # Gunicorn calls create_app(); tests and embedders retain isolated explicit config.
    values = config_from_environment() if config is None else validated_config(config)
    app.config.update(values)

    app.register_blueprint(web_bp)

    @app.context_processor
    def inject_seo() -> dict[str, object]:
        return {
            "seo": seo_for_request(),
            "analytics_ready": analytics_ready(),
            "analytics_consent": request.cookies.get(CONSENT_COOKIE, "unset"),
        }

    @app.errorhandler(404)
    def page_not_found(error: object) -> tuple[str, int, dict[str, str]]:
        return render_template("404.html"), 404, {"X-Robots-Tag": "noindex, follow"}

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        script_hosts = " https://www.googletagmanager.com" if analytics_ready() else ""
        connect_hosts = (
            " https://www.google-analytics.com https://region1.google-analytics.com"
            if analytics_ready()
            else ""
        )
        csp = (
            "default-src 'self'; "
            f"script-src 'self'{script_hosts}; "
            "style-src 'self'; "
            "img-src 'self'; "
            "font-src 'self'; "
            f"connect-src 'self'{connect_hosts}; "
            "object-src 'none'; "
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
            if analytics_ready() or request.endpoint in {
                "web.analytics_consent",
                "web.corrections",
                "web.corrections_submit",
            }:
                response.headers["Cache-Control"] = "private, no-store"
                response.vary.add("Cookie")
        return response

    return app

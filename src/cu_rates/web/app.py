"""Application factory for the CU-Rates.org website."""

from typing import Any

from flask import Flask, Response, render_template

from cu_rates.web.views import web_bp


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

    if config:
        app.config.update(config)

    app.register_blueprint(web_bp)

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
        return response

    return app

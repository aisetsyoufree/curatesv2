"""Consent helpers; a preference cookie is written only by an explicit choice POST."""

from flask import current_app

CONSENT_COOKIE = "cu_rates_analytics"


def analytics_ready() -> bool:
    """Both validated settings are required, independently of the visitor's choice."""
    return (
        current_app.config["ENABLE_ANALYTICS"] is True
        and current_app.config["GA_MEASUREMENT_ID"] is not None
    )

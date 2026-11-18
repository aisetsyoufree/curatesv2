"""Validated web configuration; no environment access for explicit factory dictionaries."""

import os
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

_ORIGIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def validate_canonical_origin(value: object) -> str:
    """Return a bare https origin, or raise ValueError if the setting is unusable."""
    if (
        not isinstance(value, str)
        or value == ""
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ValueError("CANONICAL_ORIGIN must be an https origin")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("CANONICAL_ORIGIN must be an https origin")
    if parts.username is not None or parts.password is not None or "?" in value or "#" in value:
        raise ValueError("CANONICAL_ORIGIN must not include credentials, a query, or a fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("CANONICAL_ORIGIN must not include a path")
    host = parts.hostname
    if (
        len(host) > 253
        or "." not in host
        or any(_ORIGIN_LABEL.fullmatch(label) is None for label in host.split("."))
        or parts.netloc.endswith(":")
    ):
        raise ValueError("CANONICAL_ORIGIN host is not a bare DNS name")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("CANONICAL_ORIGIN port is invalid") from exc
    if port == 0:
        raise ValueError("CANONICAL_ORIGIN port is invalid")
    if port == 443:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return f"https://{netloc}"


DEFAULTS: dict[str, Any] = {
    "ENABLE_ANALYTICS": False,
    "GA_MEASUREMENT_ID": None,
    "ADS_ENABLED": False,
    "CANONICAL_ORIGIN": "https://cu-rates.org",
    "SNAPSHOT_ROOT": None,
    "DENYLIST_PATH": None,
    "AUTOMATIC_DENYLIST_PATH": None,
    "INTAKE_DB": None,
    "SNAPSHOT_CLOCK": None,
}
_GA_ID = re.compile(r"G-[A-Z0-9]{10}")


def validated_config(values: Mapping[str, Any]) -> dict[str, Any]:
    """Reject malformed settings at startup rather than widening public behavior."""
    config = {**DEFAULTS, **values}
    config["CANONICAL_ORIGIN"] = validate_canonical_origin(config["CANONICAL_ORIGIN"])
    for key in ("ENABLE_ANALYTICS", "ADS_ENABLED"):
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} must be a boolean")
    measurement_id = config["GA_MEASUREMENT_ID"]
    if measurement_id == "":
        measurement_id = None
    if measurement_id is not None and (
        not isinstance(measurement_id, str) or _GA_ID.fullmatch(measurement_id) is None
    ):
        raise ValueError("GA_MEASUREMENT_ID must match G-XXXXXXXXXX (uppercase letters/digits)")
    config["GA_MEASUREMENT_ID"] = measurement_id
    for key in ("SNAPSHOT_ROOT", "DENYLIST_PATH", "AUTOMATIC_DENYLIST_PATH", "INTAKE_DB"):
        value = config[key]
        if value is not None and (
            not isinstance(value, str)
            or value == ""
            or not value.startswith("/")
            or value.startswith("//")
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or ".." in PurePosixPath(value).parts
        ):
            raise ValueError(f"{key} must be an absolute path without whitespace or traversal")
    return config


def config_from_environment(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read only the named web settings. Invalid values abort startup; unset is safe."""
    source = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    for key in DEFAULTS:
        if key == "SNAPSHOT_CLOCK" or key not in source:
            continue
        value = source[key]
        if key in {"ENABLE_ANALYTICS", "ADS_ENABLED"}:
            if value not in {"0", "1"}:
                raise ValueError(f"{key} must be 0 or 1")
            values[key] = value == "1"
        else:
            values[key] = value
    return validated_config(values)

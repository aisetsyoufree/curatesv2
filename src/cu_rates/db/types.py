"""SQLite column types that preserve contract values.

Rates are text, never binary floats. Nested documents are JSON text and reject
floats before they are stored.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from cu_rates.contracts.base import canonical_decimal


class DecimalText(TypeDecorator[Decimal]):
    """Lossless decimal storage. Floats are rejected rather than coerced."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, float) or not isinstance(value, Decimal):
            raise TypeError("binary floats are rejected; pass Decimal")
        return canonical_decimal(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamp stored as text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("stored datetime is missing a timezone")
        return parsed.astimezone(UTC)


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("binary floats are rejected")
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {str(key): _reject_float(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reject_float(item) for item in value]
    return value


class JsonDocument(TypeDecorator[dict[str, Any] | list[Any]]):
    """JSON text. Decimal becomes canonical text; float is an error."""

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: dict[str, Any] | list[Any] | None, dialect: Any
    ) -> str | None:
        if value is None:
            return None
        cleaned = _reject_float(value)
        return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))

    def process_result_value(
        self, value: str | None, dialect: Any
    ) -> dict[str, Any] | list[Any] | None:
        if value is None:
            return None
        loaded: dict[str, Any] | list[Any] = json.loads(value)
        return loaded

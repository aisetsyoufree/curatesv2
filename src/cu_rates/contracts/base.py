"""Shared base model, scalar types, and contract versioning."""

import re
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

CONTRACT_MAJOR = 1
CONTRACT_MINOR = 0
CONTRACT_VERSION = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"

# Version policy: "<major>.<minor>" with canonical ASCII integers (no leading zeros, no
# signs, no whitespace; padding is rejected before the model's whitespace stripping can
# hide it). The field is required, so a record never silently claims the current version.
# A reader accepts exactly its own major version and any minor up to CONTRACT_MINOR. Models
# forbid unknown fields, so a record from a *newer* minor could carry fields this reader
# would reject anyway; claiming forward compatibility would be false. Newer minors and
# every other major are therefore rejected. Bumping the minor is additive-only; bumping the
# major is a breaking change.
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def _reject_padding(value: Any) -> Any:
    if isinstance(value, str) and value != value.strip():
        raise ValueError("schema_version must not have surrounding whitespace")
    return value


def _check_schema_version(value: str) -> str:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"schema_version must look like '<major>.<minor>', got {value!r}")
    major, minor = int(match[1]), int(match[2])
    if major != CONTRACT_MAJOR:
        raise ValueError(
            f"unsupported contract major version {major}; "
            f"this package reads major version {CONTRACT_MAJOR} only"
        )
    if minor > CONTRACT_MINOR:
        raise ValueError(
            f"unsupported contract minor version {major}.{minor}; "
            f"this package reads up to {CONTRACT_VERSION}"
        )
    return value


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary floats are rejected; pass Decimal or a decimal string")
    return value


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _check_http_url(value: str) -> str:
    if re.fullmatch(r"https?://[^\s/?#]+[^\s]*", value) is None:
        raise ValueError("must be an absolute http(s) URL without whitespace")
    return value


def _sorted_unique(value: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(sorted(set(value)))


SchemaVersion = Annotated[
    str, BeforeValidator(_reject_padding), AfterValidator(_check_schema_version)
]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
HttpUrlText = Annotated[str, AfterValidator(_check_http_url)]

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
FieldPath = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.:-]{0,299}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
CharterNumber = Annotated[int, Field(gt=0, lt=10**8)]
Months = Annotated[int, Field(gt=0, le=720)]
Days = Annotated[int, Field(gt=0, le=366)]
CreditScore = Annotated[int, Field(ge=300, le=850)]

# Financial decimals: floats are rejected before coercion; NaN/Inf never validate.
Percent = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0, le=100, max_digits=9, decimal_places=5, allow_inf_nan=False),
]
# Signed spread over an index (e.g. Prime minus 0.25 is -0.25); never a standalone rate.
SignedPercent = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=-100, le=100, max_digits=9, decimal_places=5, allow_inf_nan=False),
]
Points = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0, le=10, max_digits=9, decimal_places=5, allow_inf_nan=False),
]
Money = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False),
]
Measure = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0, max_digits=14, decimal_places=5, allow_inf_nan=False),
]

SortedUnique = AfterValidator(_sorted_unique)


class ContractModel(BaseModel):
    """Frozen, strict-keyed base: unknown fields are errors, instances are immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class VersionedModel(ContractModel):
    """Top-level record carrying an explicit contract version."""

    schema_version: SchemaVersion


def canonical_decimal(value: Decimal) -> str:
    """Stable text for a Decimal: numerically equal values yield identical text."""
    # normalize() rounds to the *ambient* context precision, so an application that
    # lowers it would collapse 6.125 and 6.124. Use a precision that always holds
    # every digit of this value, independent of ambient state.
    with localcontext() as context:
        context.prec = max(len(value.as_tuple().digits) + 2, 28)
        text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


ARITHMETIC_PRECISION = 28


def contract_arithmetic() -> AbstractContextManager[Context]:
    """A fixed decimal context for every contract computation that can round.

    Python's ambient context is process-wide state: an embedder (or a library) that lowers it
    would change the result of ``max(index + margin, floor)`` or of an LTV division, and so the
    publication verdict. Contract arithmetic therefore always runs under this one context
    (28 digits, the library defaults for rounding and traps), whatever the ambient one is.
    """
    return localcontext(Context(prec=ARITHMETIC_PRECISION))

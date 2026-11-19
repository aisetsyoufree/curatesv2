"""Whole-value parsers for source fragments selected by an extraction adapter."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from typing import Literal
from zoneinfo import ZoneInfo, available_timezones

from pydantic import TypeAdapter

from cu_rates.contracts.base import CreditScore, Days, Money, Months, Percent, Points, SignedPercent
from cu_rates.contracts.enums import LienPosition, RateType, ScenarioDimension

_NUMBER = r"[0-9]+(?:\.[0-9]+)?"
_MONEY = r"\$?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"

_PERCENT = TypeAdapter[Percent](Percent)
_SIGNED = TypeAdapter[SignedPercent](SignedPercent)
_POINTS = TypeAdapter[Points](Points)
_DOLLARS = TypeAdapter[Money](Money)
_MONTHS = TypeAdapter[Months](Months)
_DAYS = TypeAdapter[Days](Days)
_SCORE = TypeAdapter[CreditScore](CreditScore)

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty")
_TERM = re.compile(
    r"([0-9]{1,3}|[a-z]+(?:-[a-z]+)?)[ -](years?|months?)"
    r"(?: fixed term)?(?: \(([0-9]{1,3}) months?\))?\.?"
)


_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
DateFormat = Literal["Month D, YYYY", "YYYY-MM-DD", "M/D/YYYY"]
_DAY = r"(?:[1-9]|[12][0-9]|3[01])"
_DATE_PATTERNS: dict[str, tuple[str, tuple[str, str, str]]] = {
    "Month D, YYYY": (rf"({'|'.join(_MONTHS_EN)}) ({_DAY}), (20[0-9]{{2}})", ("m", "d", "y")),
    "YYYY-MM-DD": (r"(20[0-9]{2})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", ("y", "m", "d")),
    "M/D/YYYY": (rf"(1[0-2]|[1-9])/({_DAY})/(20[0-9]{{2}})", ("m", "d", "y")),
}


def date_pattern(fmt: str) -> str:
    """One strict, unanchored pattern (a single capture group wraps the whole date)."""
    return f"({_DATE_PATTERNS[fmt][0]})"


def _day(text: str, fmt: str) -> date:
    pattern, order = _DATE_PATTERNS[fmt]
    match = re.fullmatch(pattern, text)
    if match is None:
        raise ValueError(f"date does not match the configured format {fmt}")
    parts = dict(zip(order, match.groups(), strict=True))
    month = _MONTHS_EN.index(parts["m"]) + 1 if fmt == "Month D, YYYY" else int(parts["m"])
    return date(int(parts["y"]), month, int(parts["d"]))  # impossible dates raise, never clamp


def prose_date(text: str, fmt: str, timezone: str) -> datetime:
    """A calendar date exactly as written, as the start of that day in the configured zone.

    The source gives no clock time, so none is invented: the earliest instant of the
    stated day is the conservative reading and can never lie in the future of a capture
    taken that day. Impossible dates (February 30) are errors, not corrections.
    """
    day = _day(text, fmt)
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(timezone)).astimezone(UTC)


PROSE_RULE = "t040.prose-date.v1"


def prose_rule_id(timezone: str) -> str:
    """Candidate rule id that records the reviewed zone (identifiers are lowercase, no '/')."""
    return f"{PROSE_RULE}:{timezone.lower().replace('/', ':')}"


@cache
def _zones() -> dict[str, str]:
    return {zone.lower().replace("/", ":"): zone for zone in available_timezones()}


def prose_instant(text: str, rule_id: str | None) -> datetime | None:
    """The exact instant a prose-date candidate stands for, from its own recorded zone.

    Only a candidate whose rule id is the prose-date rule is read this way; any other
    claim returns None and keeps strict timestamp parsing.
    """
    if rule_id is None or not rule_id.startswith(PROSE_RULE + ":"):
        return None
    zone = _zones().get(rule_id.removeprefix(PROSE_RULE + ":"))
    if zone is None:
        raise ValueError("unknown timezone in prose-date rule")
    for fmt in _DATE_PATTERNS:
        try:
            return prose_date(text, fmt, zone)
        except ValueError:
            continue
    raise ValueError("not a recognised prose date")


def percent(text: str) -> Decimal:
    return _PERCENT.validate_python(_decimal(text, rf"{_NUMBER}\s*%?", "%"))


def margin(text: str) -> Decimal:
    return _SIGNED.validate_python(_decimal(text, rf"[+-]?{_NUMBER}\s*%?", "%"))


def points(text: str) -> Decimal:
    return _POINTS.validate_python(
        _decimal(text.lower(), rf"{_NUMBER}(?:\s*%|\s+points?)?", "point[s]?|%")
    )


def money(text: str) -> Decimal:
    return _DOLLARS.validate_python(_decimal(text, _MONEY).replace(",", "").replace("$", ""))


def _decimal(text: str, pattern: str, suffix: str = "") -> str:
    if re.fullmatch(pattern, text) is None:
        raise ValueError("expected a single decimal amount with the field's unit")
    return re.sub(rf"\s*(?:{suffix})$", "", text) if suffix else text


def months(text: str) -> int:
    match = _TERM.fullmatch(text.lower())
    if match is None:
        raise ValueError("expected a whole-number term in years or months")
    quantity, unit, equivalent = match.groups()
    if quantity.isdecimal():
        count = int(quantity)
    elif quantity in _ONES:
        count = _ONES.index(quantity)
    else:
        tens, _, ones = quantity.partition("-")
        if tens not in _TENS or (ones and ones not in _ONES[1:10]):
            raise ValueError("unsupported number in term")
        count = 20 + 10 * _TENS.index(tens) + (_ONES.index(ones) if ones else 0)
    result = count * 12 if unit.startswith("year") else count
    if equivalent is not None and int(equivalent) != result:
        raise ValueError("term and parenthetical month count conflict")
    return _MONTHS.validate_python(result)


def days(text: str) -> int:
    match = re.fullmatch(r"([0-9]{1,3})(?:[ -]days?)?", text.lower())
    if match is None:
        raise ValueError("expected a whole number of days")
    return _DAYS.validate_python(int(match[1]))


def credit_score(text: str) -> int:
    if not re.fullmatch(r"[0-9]{3}", text):
        raise ValueError("expected a three-digit credit score")
    return _SCORE.validate_python(int(text))


def rate_type(text: str) -> RateType:
    match text.lower():
        case "fixed" | "fixed rate" | "fixed-rate" | "conventional conforming fixed purchase":
            return RateType.FIXED
        case "adjustable" | "adjustable rate" | "adjustable-rate" | "arm":
            return RateType.ADJUSTABLE
        case _:
            raise ValueError("expected an explicitly fixed or adjustable rate")


def lien(text: str) -> LienPosition:
    match text.lower():
        case "first" | "first lien" | "first-lien":
            return LienPosition.FIRST
        case "second" | "second lien" | "second-lien" | "closed-end fixed second mortgage":
            return LienPosition.SECOND
        case _:
            raise ValueError("expected an explicitly first or second lien")


@dataclass(frozen=True)
class Range:
    lower: Decimal | None
    upper: Decimal | None

    def contains(self, value: Decimal) -> bool:
        return (self.lower is None or value >= self.lower) and (
            self.upper is None or value <= self.upper
        )


def numeric_statement(dimension: str, text: str) -> Decimal | Range:
    def number(raw: str) -> Decimal:
        match dimension:
            case ScenarioDimension.LTV | ScenarioDimension.CLTV:
                return percent(raw)
            case ScenarioDimension.CREDIT_SCORE:
                return Decimal(credit_score(raw))
            case ScenarioDimension.TERM_MONTHS:
                return Decimal(months(raw))
            case ScenarioDimension.LOCK_DAYS:
                return Decimal(days(raw))
            case ScenarioDimension.POINTS:
                return points(raw)
            case _:
                return money(raw)

    match = re.fullmatch(r"(>=|<=)\s*(.+)", text)
    if match:
        value = number(match[2])
        return Range(value, None) if match[1] == ">=" else Range(None, value)
    match = re.fullmatch(r"(.+?)(?:\s+to\s+|\s*[\u2013\u2014]\s*)(.+)", text)
    if match:
        lo, hi = number(match[1]), number(match[2])
        if lo > hi:
            raise ValueError("range lower bound exceeds upper bound")
        return Range(lo, hi)
    return number(text)


def comparison_statement(dimension: str, text: str) -> Decimal | Range | str | bool | None:
    normalized = text.lower()
    if dimension == ScenarioDimension.OCCUPANCY:
        if normalized in (
            "owner-occupied primary residence",
            "owner-occupied single-family primary residence",
        ):
            return "owner_occupied"
        if normalized in ("owner-occupied", "owner_occupied"):
            return None  # owner occupancy alone does not establish primary residence
        if normalized in ("non-owner-occupied", "investment property", "second home"):
            return normalized
        raise ValueError("expected an explicit occupancy")
    if dimension == ScenarioDimension.PROPERTY_TYPE:
        if normalized in ("single-family", "single_family", "single family"):
            return "single_family"
        if normalized in ("condominium", "condo", "townhouse", "multifamily"):
            return normalized
        raise ValueError("expected an explicit property type")
    if dimension == ScenarioDimension.RATE_TYPE:
        return rate_type(text)
    qualifier = {
        ScenarioDimension.RELATIONSHIP_DISCOUNT: "relationship discount",
        ScenarioDimension.CASH_OUT: "cash out",
        "autopay_discount": "autopay discount",
    }.get(dimension)
    if qualifier is not None:
        if normalized in ("no", "false", f"no {qualifier}"):
            return False
        if normalized in ("yes", "true", f"{qualifier} applies", f"{qualifier} required"):
            return True
        raise ValueError(f"expected an explicit statement about {qualifier}")
    return numeric_statement(dimension, normalized)

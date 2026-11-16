"""Whole-value parsers for source fragments selected by an extraction adapter."""

import re
from dataclasses import dataclass
from decimal import Decimal

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

"""Verification of rate candidate and finding evidence locators against stored bytes.

Checks that claimed artifacts exist, digests match, and field locators (HTML,
JSON) accurately pinpoint exact source text without ambiguity. Unsupported formats
fail closed to held status.

Every material claim a candidate makes (headline rate fields, term, purpose,
assumptions, HELOC/ARM attributes, conditions, tier bounds, effective date) needs its
own locator on an explicit supported path whose resolved text discloses that value
with the right type, units, and label. A claim with no verified locator, a locator on
an unknown path or for a field the item does not claim, or a format that cannot be
bound to one table row and column (XPath, JSON) holds under a stable reason code. The
model-supplied values are never dropped; the stager keeps them in the private payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from cu_rates.adapters.static_html import fragment_is_located
from cu_rates.artifact_store import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactStoreError,
)
from cu_rates.contracts.base import _reject_float
from cu_rates.contracts.enums import (
    ConditionKind,
    ConformingStatus,
    LienPosition,
    LoanPurpose,
    OutOfScopeProduct,
    ProductFamily,
    ProductScope,
    RateAvailability,
    TierDimension,
)
from cu_rates.contracts.evidence import (
    HtmlLocator,
    ImageOcrLocator,
    JsonLocator,
    PdfLocator,
    RenderedDomLocator,
)
from cu_rates.pipeline.extract import (
    _cached_document,
    _Node,
    nearest_row,
    resolve_node,
    resolve_text,
)
from cu_rates.rate_batch.models import (
    BatchCandidate,
    BatchDisposition,
    BatchFinding,
    BatchIndependentReview,
    HoldReason,
)


def resolve_json_pointer(doc: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer against a deserialized Python data structure."""
    if not pointer:
        return doc
    if not pointer.startswith("/"):
        raise ValueError(f"RFC 6901 JSON pointer must start with '/': {pointer!r}")
    tokens = pointer.split("/")[1:]
    curr = doc
    for raw_token in tokens:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(curr, dict):
            if token not in curr:
                raise KeyError(f"token {token!r} not found in JSON object")
            curr = curr[token]
        elif isinstance(curr, (list, tuple)):
            try:
                idx = int(token)
            except ValueError as exc:
                raise KeyError(f"JSON array index must be an integer, got {token!r}") from exc
            if idx < 0 or idx >= len(curr):
                raise IndexError(f"JSON array index {idx} out of range (length {len(curr)})")
            curr = curr[idx]
        else:
            raise KeyError(f"cannot index into scalar {type(curr).__name__} with {token!r}")
    return curr


def _extract_numeric_percent(text: str) -> Decimal | None:
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        try:
            return Decimal(match.group(1))
        except Exception:
            return None
    return None


def _extract_points_value(text: str) -> Decimal | None:
    clean = text.strip().lower()
    if clean in ("0", "0%", "0.0", "0.00", "0 points", "zero", "zero points", "no points"):
        return Decimal("0")
    match = re.search(r"(\d+(?:\.\d+)?)", clean)
    if match:
        try:
            return Decimal(match.group(1))
        except Exception:
            return None
    return None


def _parse_disclosed_term_months(text: str) -> int | None:
    """Parse disclosed term into months, requiring explicit months or years unit.

    Rejects numbers without explicit unit, invalid units (days, weeks),
    or implicit conversions.
    """
    clean = text.strip()
    # Explicitly reject days, weeks or other non-month/year units
    if re.search(r"\b(?:days?|d|weeks?|w)\b", clean, re.IGNORECASE):
        return None

    # Explicit months: e.g. "360 Months", "360 mo", "360 mos", "360-month"
    months_match = re.search(r"\b(\d+)\s*(?:months?|mos?|m)\b", clean, re.IGNORECASE)
    if months_match:
        return int(months_match.group(1))
    months_hyphen = re.search(r"\b(\d+)-(?:month|mo)\b", clean, re.IGNORECASE)
    if months_hyphen:
        return int(months_hyphen.group(1))

    # Explicit years: e.g. "30 Years", "30 yr", "30 yrs", "30-year"
    years_match = re.search(r"\b(\d+)\s*(?:years?|yrs?|y)\b", clean, re.IGNORECASE)
    if years_match:
        return int(years_match.group(1)) * 12
    years_hyphen = re.search(r"\b(\d+)-(?:year|yr)\b", clean, re.IGNORECASE)
    if years_hyphen:
        return int(years_hyphen.group(1)) * 12

    return None


def _is_container_node(node: _Node) -> bool:
    """Check if node is a container element rather than a single field/cell."""
    tag = node.tag
    if tag in (
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "body",
        "section",
        "article",
        "main",
        "ul",
        "ol",
        "form",
    ):
        return True
    if any(
        child.tag in ("table", "tbody", "thead", "tfoot", "tr", "td", "th")
        for child in node.walk()
        if child is not node
    ):
        return True
    if tag == "div":
        parent = node.parent
        if parent is None or parent.tag not in ("td", "th"):
            return True
        if len(node.children) > 1:
            return True
    return False


def _nearest_cell(node: _Node) -> _Node | None:
    curr: _Node | None = node
    while curr is not None and curr.tag not in ("td", "th"):
        curr = curr.parent
    return curr


def _nearest_table(node: _Node) -> _Node | None:
    curr: _Node | None = node
    while curr is not None and curr.tag != "table":
        curr = curr.parent
    return curr


def _find_column_header(table: _Node | None, col_idx: int) -> str | None:
    if table is None or col_idx < 0:
        return None
    # 1. Search for thead first
    for n in table.walk():
        if n.tag == "thead":
            for tr in n.walk():
                if tr.tag == "tr":
                    ths = [c for c in tr.children if c.tag in ("th", "td")]
                    if col_idx < len(ths):
                        return ths[col_idx].text().strip()
    # 2. Check first row of table or tbody for th elements or header keywords
    for tr in table.walk():
        if tr.tag == "tr":
            ths = [c for c in tr.children if c.tag in ("th", "td")]
            if (
                any(c.tag == "th" for c in ths)
                or any(
                    re.search(r"\b(product|rate|apr|term|points)\b", c.text(), re.IGNORECASE)
                    for c in ths
                )
            ) and col_idx < len(ths):
                return ths[col_idx].text().strip()
            break
    return None


def _verify_column_semantics(
    path: str,
    col_idx: int,
    header_text: str | None,
    cell: _Node | None,
) -> tuple[bool, str | None]:
    """Verify that a candidate locator resolves to a semantically matching column.

    Prevents column rebinding (e.g. claiming APR from a note-rate cell, or note_rate
    from an APR column) even when the numeric values match.
    """
    if header_text is not None:
        h = header_text.lower()
        if path == "note_rate":
            if re.search(r"\bapr\b|\bannual\s+percentage\b", h) and not re.search(
                r"\bnote\b|\binterest\b", h
            ):
                return False, f"locator for note_rate resolves to APR column {header_text!r}"
            if not re.search(r"\brate\b|\binterest\b|\binitial\b", h):
                return False, f"locator for note_rate resolves to non-rate column {header_text!r}"
            return True, None
        if path == "apr":
            if not re.search(r"\bapr\b|\bannual\s+percentage\b", h):
                return False, f"locator for apr resolves to non-APR column {header_text!r}"
            return True, None
        if path == "points":
            if not re.search(r"\bpoints?\b|\bpts\b|\bdiscount\b", h):
                return False, f"locator for points resolves to non-points column {header_text!r}"
            return True, None
        if path in ("term_months", "details.term_months"):
            # A term may sit in its own column or in an explicit product label ("30-Year Fixed").
            if not re.search(
                r"\bterms?\b|\bmonths?\b|\byears?\b|\byrs?\b|\bperiod\b"
                r"|\bproduct\b|\bloan\b|\bprogram\b",
                h,
            ):
                return False, f"locator for term_months resolves to non-term column {header_text!r}"
            return True, None
        if path == "loan_purpose":
            if not re.search(r"\b(product|loan|program|description|desc|type|purpose)\b", h):
                return (
                    False,
                    f"locator for loan_purpose resolves to non-product column {header_text!r}",
                )
            return True, None
        if path in ("product", "product_name"):
            if not re.search(r"\b(product|loan|program|desc)\b", h):
                return False, f"locator for product resolves to non-product column {header_text!r}"
            return True, None
        return True, None

    # Fall back to cell classes/attributes if header row is absent
    attributes = cell.attrs if cell is not None else {}
    class_attribute = attributes.get("class", "")
    classes = class_attribute.lower().split()
    classes_set = set(classes)
    cell_id = attributes.get("id", "").lower()

    if not classes_set and not cell_id:
        return (
            False,
            (
                f"table lacks explicit column headings or cell attributes to verify {path} column "
                f"binding"
            ),
        )

    if path == "note_rate":
        if "apr" in classes_set and not any(
            r in classes_set for r in ("rate", "note-rate", "noterate")
        ):
            return (
                False,
                f"locator for note_rate resolves to APR cell (class={class_attribute!r})",
            )
        if any(
            c in classes_set for c in ("apr", "points", "pts", "term", "prod", "product")
        ) and not any(r in classes_set for r in ("rate", "note-rate", "noterate", "interest-rate")):
            return (
                False,
                f"locator for note_rate resolves to non-rate cell (class={class_attribute!r})",
            )
        if (
            any(r in classes_set for r in ("rate", "note-rate", "noterate", "interest-rate"))
            or "rate" in cell_id
        ):
            return True, None
        return (
            False,
            f"locator for note_rate cannot be verified against cell (class={class_attribute!r})",
        )

    if path == "apr":
        if "apr" not in classes_set and "apr" not in cell_id:
            return (
                False,
                f"locator for apr resolves to non-APR cell (class={class_attribute!r})",
            )
        return True, None

    if path == "points":
        if (
            not any(c in classes_set for c in ("points", "pts", "discount"))
            and "points" not in cell_id
            and "pts" not in cell_id
        ):
            return (
                False,
                f"locator for points resolves to non-points cell (class={class_attribute!r})",
            )
        return True, None

    if path in ("term_months", "details.term_months"):
        if (
            not any(c in classes_set for c in ("term", "months", "years", "prod", "product"))
            and "term" not in cell_id
        ):
            return (
                False,
                f"locator for term_months resolves to non-term cell (class={class_attribute!r})",
            )
        return True, None

    if path == "loan_purpose":
        if (
            not any(
                c in classes_set for c in ("prod", "product", "loan-name", "program", "purpose")
            )
            and "product" not in cell_id
            and "purpose" not in cell_id
        ):
            return (
                False,
                (
                    f"locator for loan_purpose resolves to non-product cell "
                    f"(class={class_attribute!r})"
                ),
            )
        return True, None

    return True, None


# ---------------------------------------------------------------------------
# Supported material paths and per-claim semantics
# ---------------------------------------------------------------------------

_Verdict = tuple[BatchDisposition, HoldReason | None, str | None]
_Fail = tuple[HoldReason, str]


def _hold(reason: HoldReason, detail: str) -> _Verdict:
    return (BatchDisposition.HELD, reason, detail)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_CORE_KEYS = frozenset({"note_rate", "apr", "points", "term_months"})
_ARM_FIELDS = (
    "initial_period_months",
    "adjustment_interval_months",
    "index_name",
    "margin",
    "initial_cap",
    "periodic_cap",
    "lifetime_cap",
)
_HELOC_FIELDS = (
    "intro_rate",
    "intro_duration_months",
    "index_name",
    "index_rate",
    "margin",
    "floor",
    "ceiling",
    "stated_post_intro_apr",
    "draw_period_months",
    "repayment_period_months",
    "max_cltv",
    "lien_position",
)
_TOP_LEVEL_CLAIMS = (
    "term_months",
    "loan_purpose",
    "max_cltv",
    "min_amount",
    "max_amount",
    "credit_score_min",
    "lock_days",
    "property_type",
    "occupancy",
    "headline_is_as_low_as",
    "effective_at",
)


def _build_candidate_paths() -> dict[str, str]:
    paths = {"note_rate": "note_rate", "apr": "apr", "points": "points"}
    paths.update({name: name for name in _TOP_LEVEL_CLAIMS})
    paths.update({"lien_position": "lien_position", "conforming_status": "conforming_status"})
    paths["details.term_months"] = "term_months"
    paths["details.purpose"] = "loan_purpose"
    paths["effective_date"] = "effective_at"
    paths.update({f"heloc_terms.{name}": f"heloc_terms.{name}" for name in _HELOC_FIELDS})
    paths.update({f"arm_terms.{name}": f"arm_terms.{name}" for name in _ARM_FIELDS})
    return paths


# Allowed candidate locator paths (including aliases) mapped to the claim they can verify.
_CANDIDATE_PATHS = _build_candidate_paths()
_FINDING_PATHS = {
    "availability": "availability",
    "rate_availability": "availability",
    "out_of_scope_product": "out_of_scope_product",
}


@dataclass(frozen=True)
class _Spec:
    kind: str
    label: re.Pattern[str] | None = None


_CLTV_LABEL = _rx(r"\b(?:cltv|ltv|loan[- ]to[- ]value)\b")
_INTRO_LABEL = _rx(r"\bintro(?:ductory)?\b|\bpromo(?:tional)?\b|\bteaser\b")
_INDEX_LABEL = _rx(
    r"\bindex(?:ed)?\b|\btied\s+to\b|\bbased\s+on\b|\bwsj\b|\bwall\s+street\s+journal\b|\bsofr\b"
)
_MARGIN_LABEL = _rx(r"\bmargin\b")

_SPECS: dict[str, _Spec] = {
    "max_cltv": _Spec("percent", _CLTV_LABEL),
    "credit_score_min": _Spec("credit_score", _rx(r"\b(?:fico|credit\s+scores?)\b")),
    "lock_days": _Spec("lock_days", _rx(r"\block\b")),
    "effective_at": _Spec("date", _rx(r"\beffective\b|\bas\s+of\b")),
    "headline_is_as_low_as": _Spec("as_low_as"),
    "loan_purpose": _Spec("enum"),
    "lien_position": _Spec("enum"),
    "property_type": _Spec("enum"),
    "occupancy": _Spec("enum"),
    "conforming_status": _Spec("enum"),
    # No safe typed parser for loan amounts yet: hold as unsupported, never infer.
    "min_amount": _Spec("unsupported"),
    "max_amount": _Spec("unsupported"),
    "heloc_terms.intro_rate": _Spec("percent", _INTRO_LABEL),
    "heloc_terms.intro_duration_months": _Spec("months", _INTRO_LABEL),
    "heloc_terms.index_name": _Spec("text", _INDEX_LABEL),
    "heloc_terms.index_rate": _Spec("percent", _rx(r"\bindex\b|\bprime\b|\bwsj\b|\bsofr\b")),
    "heloc_terms.margin": _Spec("percent", _MARGIN_LABEL),
    "heloc_terms.floor": _Spec("percent", _rx(r"\bfloor\b|\bminimum\s+rate\b")),
    "heloc_terms.ceiling": _Spec(
        "percent", _rx(r"\bceiling\b|\bmaximum\s+rate\b|\blifetime\s+cap\b|\bmax(?:imum)?\s+apr\b")
    ),
    "heloc_terms.stated_post_intro_apr": _Spec(
        "percent",
        _rx(
            r"\bpost[- ]?intro(?:ductory)?\b"
            r"|\bafter\s+(?:the\s+)?(?:intro(?:ductory)?|promo\w*)\b"
        ),
    ),
    "heloc_terms.draw_period_months": _Spec("months", _rx(r"\bdraw\b")),
    "heloc_terms.repayment_period_months": _Spec("months", _rx(r"\brepay(?:ment)?\b")),
    "heloc_terms.max_cltv": _Spec("percent", _CLTV_LABEL),
    "heloc_terms.lien_position": _Spec("enum"),
    "arm_terms.initial_period_months": _Spec("months", _rx(r"\binitial\b|\bfixed\b")),
    "arm_terms.adjustment_interval_months": _Spec(
        "months", _rx(r"\badjust(?:s|ment|ments|ing)?\b|\bchanges?\b|\bresets?\b")
    ),
    "arm_terms.index_name": _Spec("text", _INDEX_LABEL),
    "arm_terms.margin": _Spec("percent", _MARGIN_LABEL),
    "arm_terms.initial_cap": _Spec(
        "percent", _rx(r"\b(?:initial|first)\s+(?:adjustment\s+)?cap\b")
    ),
    "arm_terms.periodic_cap": _Spec(
        "percent", _rx(r"\b(?:periodic|subsequent)\s+(?:adjustment\s+)?cap\b")
    ),
    "arm_terms.lifetime_cap": _Spec("percent", _rx(r"\b(?:lifetime|life)\s+cap\b")),
}


def _present_claims(item: BatchCandidate) -> dict[str, Any]:
    """Every material value the candidate asserts, including explicit zero and false."""
    claims: dict[str, Any] = {}
    if item.note_rate is not None:
        claims["note_rate"] = item.note_rate
    if item.apr is not None:
        claims["apr"] = item.apr
    if item.points_not_applicable:
        claims["points"] = "not applicable"
    elif item.points is not None:
        claims["points"] = item.points
    for name in _TOP_LEVEL_CLAIMS:
        value = getattr(item, name)
        if value is not None:
            claims[name] = value
    if item.lien_position not in (None, LienPosition.UNKNOWN):
        claims["lien_position"] = item.lien_position
    if item.conforming_status is not ConformingStatus.UNKNOWN:
        claims["conforming_status"] = item.conforming_status
    for prefix, group, fields in (
        ("arm_terms", item.arm_terms, _ARM_FIELDS),
        ("heloc_terms", item.heloc_terms, _HELOC_FIELDS),
    ):
        if group is None:
            continue
        for name in fields:
            value = getattr(group, name, None)
            if value is None or value is LienPosition.UNKNOWN:
                continue
            claims[f"{prefix}.{name}"] = value
    return claims


_PERCENT_TOKEN = re.compile(
    r"(?<![\w.])(?:(?P<word>minus|plus)\s+|(?P<sign>[+-])\s?)?(?P<num>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_DURATION_TOKEN = re.compile(r"(?<![\w.])(\d+)\s*-?\s*(months?|mos?|years?|yrs?)\b", re.IGNORECASE)
_SCORE_TOKEN = re.compile(r"(?<![\w.$,])(\d{3})(?![\w.,]*\d|%)")
_LOCK_TOKEN = re.compile(r"(?<![\w.])(\d{1,3})[- ]days?\b", re.IGNORECASE)
# Non-ASCII comparison signs and dashes are built from code points to keep the source ASCII.
_LE_SIGN = chr(0x2264)
_GE_SIGN = chr(0x2265)
_DASHES = "-" + chr(0x2013) + chr(0x2014)
_MIN_INDICATOR = _rx(
    r"\+|"
    + _GE_SIGN
    + r"|>=|\bat\s+least\b|\bor\s+(?:higher|more|above|greater)\b|\bmin(?:imum)?\b"
)
_MAX_INDICATOR = _rx(r"\b(?:up\s+to|under|below|less\s+than|maximum|max)\b|<=|" + _LE_SIGN + "|<")
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_US_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})(?![\d/])")
_LONG_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?"
    r"\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _percent_tokens(text: str) -> list[Decimal]:
    tokens: list[Decimal] = []
    for match in _PERCENT_TOKEN.finditer(text):
        value = Decimal(match["num"])
        if (match["word"] or "").lower() == "minus" or match["sign"] == "-":
            value = -value
        tokens.append(value)
    return tokens


def _duration_tokens(text: str) -> list[int]:
    return [
        int(match[1]) * (12 if match[2].lower().startswith("y") else 1)
        for match in _DURATION_TOKEN.finditer(text)
    ]


def _dates_in(text: str) -> set[date]:
    found: set[date] = set()

    def add(year: int, month: int, day: int) -> None:
        try:
            found.add(date(year, month, day))
        except ValueError:
            return

    for match in _ISO_DATE.finditer(text):
        add(int(match[1]), int(match[2]), int(match[3]))
    for match in _US_DATE.finditer(text):
        add(int(match[3]), int(match[1]), int(match[2]))
    for match in _LONG_DATE.finditer(text):
        add(int(match[3]), _MONTH_NUMBERS[match[1].lower()], int(match[2]))
    return found


def _enum_disclosed(key: str, value: str, text: str) -> bool:
    """True only when the text explicitly states the enum value (no inference)."""
    t = text.lower()
    name = key.rsplit(".", 1)[-1]
    if name == "loan_purpose":
        cash_out = re.search(r"\bcash[- ]out\b", t) is not None
        rate_term = re.search(r"\brate\s*[/&-]?\s*(?:and\s+)?term\b", t) is not None
        refi = re.search(r"\b(?:refinance|refi)\b", t) is not None
        if value == "purchase":
            return re.search(r"\bpurchase\b", t) is not None and not (refi or cash_out or rate_term)
        if value == "rate_term_refinance":
            return rate_term and not cash_out
        if value == "cash_out_refinance":
            return cash_out and not rate_term and re.search(r"\blimited\b", t) is None
        return False
    if name == "lien_position":
        first = re.search(r"\b(?:first|1st)\s+(?:lien|mortgage|position)\b", t) is not None
        second = re.search(r"\b(?:second|2nd)\s+(?:lien|mortgage|position)\b", t) is not None
        return (value == "first" and first and not second) or (
            value == "second" and second and not first
        )
    if name == "property_type":
        return value == "single_family" and (
            re.search(r"\bsingle[- ]family\b|\b1[- ]unit\b", t) is not None
        )
    if name == "occupancy":
        return value == "owner_occupied" and (
            re.search(r"\bowner[- ]occupied\b|\bprimary\s+residen(?:ce|cy)\b", t) is not None
        )
    if name == "conforming_status":
        if value == "conforming":
            return re.search(r"\bconforming\b", t) is not None and (
                re.search(r"\bnon[- ]?conforming\b|\bhigh[- ]balance\b|\bjumbo\b", t) is None
            )
        if value == "high_balance_conforming":
            return re.search(r"\bhigh[- ]balance\b", t) is not None
        if value == "jumbo":
            return re.search(r"\bjumbo\b", t) is not None
    return False


def _spec_error(key: str, value: Any, text: str, haystack: str) -> _Fail | None:
    """Verify one non-core claim against the full resolved cell text (never a model fragment)."""
    spec = _SPECS.get(key)
    if spec is None or spec.kind == "unsupported":
        return (HoldReason.LOCATOR_UNSUPPORTED, f"claim {key} has no supported semantic check")
    if spec.label is not None and not spec.label.search(haystack):
        return (
            HoldReason.RAW_TEXT_MISMATCH,
            f"locator text {text!r} lacks an explicit label for {key}",
        )
    kind = spec.kind
    if kind == "percent":
        if not isinstance(value, Decimal):
            return (HoldReason.LOCATOR_UNSUPPORTED, f"claim {key} is not a plain decimal")
        tokens = _percent_tokens(text)
        if len(tokens) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"text {text!r} must disclose exactly one percentage for {key}",
            )
        if tokens[0] != value:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"disclosed {key} {tokens[0]}% does not equal claimed {value}",
            )
        return None
    if kind == "months":
        if not isinstance(value, int) or isinstance(value, bool):
            return (HoldReason.LOCATOR_UNSUPPORTED, f"claim {key} is not a whole month count")
        if re.search(r"\b(?:days?|weeks?)\b", text, re.IGNORECASE):
            return (HoldReason.RAW_TEXT_MISMATCH, f"{key} text {text!r} uses a non-month unit")
        months = _duration_tokens(text)
        if len(months) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"text {text!r} must disclose exactly one explicit months/years duration for {key}",
            )
        if months[0] != value:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"disclosed {key} {months[0]} months does not equal claimed {value}",
            )
        return None
    if kind == "credit_score":
        scores = [int(m[1]) for m in _SCORE_TOKEN.finditer(text) if 300 <= int(m[1]) <= 850]
        if len(scores) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"text {text!r} must disclose exactly one credit score for {key}",
            )
        if _MAX_INDICATOR.search(text) or not _MIN_INDICATOR.search(haystack):
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"text {text!r} does not explicitly disclose a minimum credit score",
            )
        if scores[0] != value:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"disclosed credit score {scores[0]} does not equal claimed {value}",
            )
        return None
    if kind == "lock_days":
        lock_days = [int(m[1]) for m in _LOCK_TOKEN.finditer(text)]
        if len(lock_days) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"text {text!r} must disclose exactly one lock period in days",
            )
        if lock_days[0] != value:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"disclosed lock {lock_days[0]} days does not equal claimed {value}",
            )
        return None
    if kind == "date":
        if not isinstance(value, datetime):
            return (HoldReason.LOCATOR_UNSUPPORTED, f"claim {key} is not a timestamp")
        dates = _dates_in(text)
        if len(dates) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"text {text!r} must disclose exactly one date for {key}",
            )
        if value.hour or value.minute or value.second or value.microsecond:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"a date-only disclosure cannot prove the time of day claimed for {key}",
            )
        if next(iter(dates)) != value.date():
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"disclosed date {next(iter(dates))} does not equal claimed {key} {value.date()}",
            )
        return None
    if kind == "as_low_as":
        if value is not True:
            return (
                HoldReason.LOCATOR_UNSUPPORTED,
                f"an explicit {key}=false cannot be proven from stored text without inference",
            )
        if re.search(r"\bas\s+low\s+as\b", text, re.IGNORECASE) is None:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"text {text!r} does not disclose an 'as low as' qualifier",
            )
        return None
    if kind == "text":
        name = str(value).strip()
        pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
        if not name or re.search(pattern, text, re.IGNORECASE) is None:
            return (HoldReason.RAW_TEXT_MISMATCH, f"text {text!r} does not name {key} {name!r}")
        return None
    if kind == "enum":
        enum_value = getattr(value, "value", None)
        if not isinstance(enum_value, str) or not _enum_disclosed(key, enum_value, text):
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"text {text!r} does not explicitly disclose {key} {enum_value!r}",
            )
        return None
    return (HoldReason.LOCATOR_UNSUPPORTED, f"claim {key} has no supported semantic check")


def _core_value_error(key: str, item: BatchCandidate, frag: str, src: str) -> _Fail | None:
    """Typed headline/term verification shared by HTML ("disclosed") and JSON ("JSON")."""
    if key == "note_rate" and item.note_rate is not None:
        disclosed = _extract_numeric_percent(frag)
        if disclosed != item.note_rate:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"{src} rate {disclosed}% does not equal claimed note_rate {item.note_rate}",
            )
    elif key == "apr" and item.apr is not None:
        disclosed = _extract_numeric_percent(frag)
        if disclosed != item.apr:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"{src} APR {disclosed}% does not equal claimed apr {item.apr}",
            )
    elif key == "points":
        if item.points_not_applicable:
            if frag.casefold() not in {"n/a", "not applicable"}:
                return (
                    HoldReason.RAW_TEXT_MISMATCH,
                    f"{src} points value does not explicitly state not applicable",
                )
        elif item.points is not None:
            disclosed_pts = _extract_points_value(frag)
            if disclosed_pts != item.points:
                return (
                    HoldReason.RAW_TEXT_MISMATCH,
                    f"{src} points {disclosed_pts} does not equal claimed points {item.points}",
                )
    elif key == "term_months" and item.term_months is not None:
        disclosed_term = _parse_disclosed_term_months(frag)
        if disclosed_term is None:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"{src} term {frag!r} does not disclose an explicit months/years unit",
            )
        if len(_duration_tokens(frag)) != 1:
            return (
                HoldReason.EVIDENCE_AMBIGUOUS,
                f"{src} term {frag!r} must disclose exactly one duration",
            )
        if disclosed_term != item.term_months:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                (
                    f"{src} term {disclosed_term} months does not match candidate "
                    f"term_months {item.term_months}"
                ),
            )
    return None


_AVAILABILITY_KEYWORDS: dict[RateAvailability, re.Pattern[str]] = {
    RateAvailability.QUOTE_ONLY: _rx(
        r"\bquotes?\b|\bcall\b|\bphone\b|\bapplication\b|\bapply\b|\bcontact\b"
    ),
    RateAvailability.LOGIN_REQUIRED: _rx(
        r"\blog\s*in\b|\blogin\b|\bsign\s*in\b|\bauthenticated\b|\bmembers?\s+only\b"
        r"|\bonline\s+banking\b"
    ),
    RateAvailability.BLOCKED: _rx(
        r"\brestricted\b|\bblocked\b|\bdenied\b|\bforbidden\b|\bauthenticated\b"
        r"|\baccess\b|\bunavailable\b"
    ),
    RateAvailability.NO_PUBLIC_RATE: _rx(
        r"\bno\s+(?:public(?:ly)?\s+)?rates?\b"
        r"|\bnot\s+(?:publicly\s+)?(?:posted|published|available|listed|disclosed)\b"
    ),
    RateAvailability.NOT_OFFERED: _rx(
        r"\bnot\s+offer(?:ed|ing)\b|\bno\s+longer\s+offer|\bdo(?:es)?\s+not\s+offer\b"
        r"|\bdiscontinued\b"
    ),
}
_OUT_OF_SCOPE_KEYWORDS: dict[OutOfScopeProduct, re.Pattern[str]] = {
    OutOfScopeProduct.VEHICLE: _rx(r"\b(?:auto|vehicle|car|truck|motorcycle|boat|rv)\b"),
    OutOfScopeProduct.UNSECURED_PERSONAL: _rx(r"\bpersonal\s+loans?\b|\bunsecured\b"),
    OutOfScopeProduct.CONSTRUCTION: _rx(r"\bconstruction\b"),
    OutOfScopeProduct.RENOVATION: _rx(r"\brenovation\b|\bremodel\w*\b|\bhome\s+improvement\b"),
    OutOfScopeProduct.LAND: _rx(r"\bland\b|\blot\b"),
    OutOfScopeProduct.REVERSE_MORTGAGE: _rx(r"\breverse\s+mortgage\b"),
    OutOfScopeProduct.COMMERCIAL: _rx(r"\bcommercial\b"),
}


def _finding_error(item: BatchFinding, key: str, text: str) -> _Fail | None:
    """A coverage finding's located text must actually state the claimed availability/product."""
    if key == "availability" and item.rate_availability is not None:
        availability = item.rate_availability
        availability_pattern = _AVAILABILITY_KEYWORDS.get(availability)
        if availability_pattern is None:
            return (
                HoldReason.LOCATOR_UNSUPPORTED,
                f"rate_availability {availability.value!r} has no supported semantic check",
            )
        if availability_pattern.search(text) is None:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"text {text!r} does not disclose rate_availability {availability.value!r}",
            )
        return None
    if key == "out_of_scope_product" and item.out_of_scope_product is not None:
        product = item.out_of_scope_product
        product_pattern = _OUT_OF_SCOPE_KEYWORDS.get(product)
        if product_pattern is None:
            return (
                HoldReason.LOCATOR_UNSUPPORTED,
                f"out_of_scope_product {product.value!r} has no supported semantic check",
            )
        if product_pattern.search(text) is None:
            return (
                HoldReason.RAW_TEXT_MISMATCH,
                f"text {text!r} does not disclose out_of_scope_product {product.value!r}",
            )
        return None
    return (HoldReason.LOCATOR_UNSUPPORTED, f"finding path {key!r} has no supported semantic check")


_CONDITION_KEYWORDS: dict[ConditionKind, re.Pattern[str] | None] = {
    ConditionKind.AS_LOW_AS: _rx(r"\bas\s+low\s+as\b"),
    ConditionKind.DISCOUNT: _rx(r"\bdiscounts?\b"),
    ConditionKind.RELATIONSHIP: _rx(
        r"\brelationship\b|\bmember(?:ship)?\b|\bchecking\b|\bdirect\s+deposit\b"
    ),
    ConditionKind.AUTOPAY: _rx(r"\bauto[- ]?pay\b|\bautomatic\b"),
    ConditionKind.TIER: _rx(r"\b(?:tier|cltv|ltv|fico|credit\s+score|loan\s+amount)\b"),
    ConditionKind.BORROWER_REQUIREMENT: _rx(
        r"\b(?:borrower|credit|score|fico|income|owner|occupan\w*|residen\w*|first[- ]time"
        r"|member|required|requires|must|minimum)\b"
    ),
    # Uncategorized: only the grounded raw text is verified, never the model's description.
    ConditionKind.OTHER: None,
}


def _selector_names_container(selector: str) -> bool:
    tokens = selector.strip().split(">")[-1].strip().split()
    if not tokens:
        return True
    return re.search(r"\b(tr|table|tbody|thead|section|body)(?:$|\s|:)", tokens[-1]) is not None


def _verify_condition(
    index: int,
    condition: Any,
    body: bytes,
    candidate_row: _Node | None,
    item: BatchCandidate,
) -> _Verdict | None:
    """Each condition needs its own css locator in the candidate's row that states it."""
    label = f"condition {index} ({condition.kind.value})"
    locator = condition.locator
    if locator is None:
        return _hold(HoldReason.EVIDENCE_MISSING, f"{label} lacks evidence locator")
    if not isinstance(locator, HtmlLocator) or locator.selector_type != "css":
        return _hold(
            HoldReason.LOCATOR_UNSUPPORTED,
            f"{label} needs an html css locator that binds to the candidate row",
        )
    if _selector_names_container(locator.selector):
        return _hold(
            HoldReason.EVIDENCE_AMBIGUOUS,
            f"{label} selector {locator.selector!r} resolves to container, not one full field",
        )
    try:
        node = resolve_node(_cached_document(body), locator.selector)
    except Exception as exc:
        return _hold(
            HoldReason.LOCATOR_UNVERIFIED,
            f"{label} selector {locator.selector!r} failed to resolve: {exc}",
        )
    if _is_container_node(node):
        return _hold(
            HoldReason.EVIDENCE_AMBIGUOUS,
            f"{label} selector {locator.selector!r} resolves to container <{node.tag}>",
        )
    node_text = node.text()
    if hashlib.sha256(node_text.encode("utf-8")).hexdigest() != locator.normalized_text_sha256:
        return _hold(
            HoldReason.LOCATOR_UNVERIFIED,
            f"{label} normalized text SHA-256 mismatch for selector {locator.selector!r}",
        )
    row = nearest_row(node)
    if row is None or candidate_row is None or row is not candidate_row:
        return _hold(
            HoldReason.EVIDENCE_AMBIGUOUS,
            f"{label} does not resolve inside the candidate's proven table row",
        )
    clean_node = node_text.strip()
    clean_raw = condition.raw_text.strip()
    if clean_node != clean_raw and not fragment_is_located(clean_node, clean_raw):
        return _hold(
            HoldReason.RAW_TEXT_MISMATCH,
            f"{label} raw text {clean_raw!r} is not located in {clean_node!r}",
        )
    keywords = _CONDITION_KEYWORDS.get(condition.kind)
    if keywords is not None and keywords.search(clean_node) is None:
        return _hold(
            HoldReason.RAW_TEXT_MISMATCH,
            f"{label} text {clean_node!r} does not state a {condition.kind.value} condition",
        )
    if condition.kind is ConditionKind.AS_LOW_AS and item.headline_is_as_low_as is False:
        return _hold(
            HoldReason.RAW_TEXT_MISMATCH,
            "candidate states headline_is_as_low_as=false but a located condition says 'as low as'",
        )
    return None


# Tier bound criteria: an explicit labeled number or range in a cell of the proven tier row.
_BOUND_NUMBER = r"\d+(?:\.\d+)?"
_BOUND_SEP = r"\s*(?:[" + _DASHES + r"]|to|through)\s*"
_BOUND_UP = (
    r"(?:<=|" + _LE_SIGN + r"|up\s+to|at\s+or\s+below|not\s+to\s+exceed|max(?:imum)?(?:\s+of)?)\s*"
)
_BOUND_LOW = r"(?:>=|" + _GE_SIGN + r"|at\s+least|min(?:imum)?(?:\s+of)?)\s*"
_BOUND_LOW_PLUS = r"\s*(?:\+|or\s+(?:more|higher|above|greater))"
_BOUND_LABELS: dict[TierDimension, tuple[str, str]] = {
    TierDimension.CLTV: (r"(?:cltv|combined\s+loan[- ]to[- ]value)", r"\s*%?"),
    TierDimension.LTV: (r"(?:(?<![a-z])ltv|(?<!combined )loan[- ]to[- ]value)", r"\s*%?"),
    TierDimension.CREDIT_SCORE: (r"(?:fico|credit\s+scores?)", r"(?!\s*%)"),
}


def _bound_patterns(label: str, unit: str) -> list[re.Pattern[str]]:
    lo = "(?P<lo>" + _BOUND_NUMBER + ")" + unit
    hi = "(?P<hi>" + _BOUND_NUMBER + ")" + unit
    before = label + r"\s*:?\s*"
    after = r"\s*" + label
    forms = [
        lo + _BOUND_SEP + hi + after,
        before + lo + _BOUND_SEP + hi,
        _BOUND_UP + hi + after,
        before + _BOUND_UP + hi,
        hi + after + r"\s*or\s+(?:less|below|lower)",
        _BOUND_LOW + lo + after,
        before + _BOUND_LOW + lo,
        lo + _BOUND_LOW_PLUS + after,
        before + lo + _BOUND_LOW_PLUS,
    ]
    return [re.compile(form, re.IGNORECASE) for form in forms]


_BOUND_PATTERNS = {
    dimension: _bound_patterns(label, unit) for dimension, (label, unit) in _BOUND_LABELS.items()
}


_Bounds = tuple[Decimal | None, Decimal | None]


def _is_credit_score(value: Decimal | None) -> bool:
    return value is None or (value == value.to_integral_value() and 300 <= value <= 850)


def _parse_bounds(dimension: TierDimension, text: str) -> set[_Bounds]:
    found: set[_Bounds] = set()
    for pattern in _BOUND_PATTERNS.get(dimension, []):
        for match in pattern.finditer(text):
            groups = match.groupdict()
            lower = Decimal(groups["lo"]) if groups.get("lo") else None
            upper = Decimal(groups["hi"]) if groups.get("hi") else None
            if dimension is TierDimension.CREDIT_SCORE and not (
                _is_credit_score(lower) and _is_credit_score(upper)
            ):
                continue
            found.add((lower, upper))
    return found


def _tier_bounds_error(tier: Any, row: _Node) -> _Verdict | None:
    """Typed tier bounds need explicit labeled criteria in the same proven tier row."""
    cells = [c for c in row.children if c.tag in ("td", "th")]
    for bound in tier.bounds:
        if bound.dimension not in _BOUND_PATTERNS:
            return _hold(
                HoldReason.LOCATOR_UNSUPPORTED,
                (
                    f"tier {tier.tier_id} bound dimension {bound.dimension.value!r} has no "
                    f"supported criteria verification"
                ),
            )
        parsed: set[_Bounds] = set()
        for cell in cells:
            parsed |= _parse_bounds(bound.dimension, cell.text())
        if not parsed:
            return _hold(
                HoldReason.EVIDENCE_MISSING,
                (
                    f"tier {tier.tier_id} {bound.dimension.value} bound has no explicit labeled "
                    f"criterion in its table row"
                ),
            )
        if len(parsed) > 1:
            return _hold(
                HoldReason.EVIDENCE_AMBIGUOUS,
                (f"tier {tier.tier_id} row states conflicting {bound.dimension.value} criteria"),
            )
        lower, upper = next(iter(parsed))
        zero_floor = bound.dimension in (TierDimension.LTV, TierDimension.CLTV)
        lower_ok = (
            bound.lower == lower
            if lower is not None
            else (bound.lower is None or (zero_floor and bound.lower == 0))
        )
        if not (lower_ok and bound.upper == upper):
            return _hold(
                HoldReason.RAW_TEXT_MISMATCH,
                (
                    f"tier {tier.tier_id} claimed {bound.dimension.value} bound "
                    f"[{bound.lower}, {bound.upper}] does not match the row's disclosed criterion "
                    f"[{lower}, {upper}]"
                ),
            )
    return None


def verify_batch_item_locators(
    item: BatchCandidate | BatchFinding,
    store: ArtifactStore,
    *,
    review: BatchIndependentReview | None = None,
) -> tuple[BatchDisposition, HoldReason | None, str | None]:
    """Verify all locators in a candidate or finding against the content-addressed store.

    Checks:
    1. Artifact exists and is uncorrupted in the content-addressed store.
    2. Candidate headline grounding: note_rate, apr, and explicit points require locators.
    3. Independent review concurrence: disagreements result in held disposition.
    4. Locator paths: only explicit supported material paths (and aliases) bound to a claim
       the item actually makes; unknown paths and locators for absent fields are held.
    5. Table row isolation: selectors for an offer must not mix unrelated rows.
    6. Locator resolution: resolves one full field and matches the disclosed typed value,
       units, and label for that path exactly.
    7. Unbounded substrings (for example, 6.375% inside 16.375%) are held.
    8. Formats that cannot bind a value to one table row and column (XPath, JSON, PDF, OCR,
       Rendered DOM) fail closed to held status for candidates.
    9. Tier bounds need explicit labeled criteria in the proven tier row; conditions need
       their own locator in that row; every present claim needs a verified locator.

    Returns:
        (disposition, hold_reason, detail_message)
    """
    # 1. Stored artifact resolution
    try:
        body = store.read_body(item.artifact_id)
    except ArtifactNotFoundError:
        return (
            BatchDisposition.HELD,
            HoldReason.ARTIFACT_NOT_FOUND,
            f"artifact {item.artifact_id} not found in store",
        )
    except ArtifactCorruptError as exc:
        return (
            BatchDisposition.HELD,
            HoldReason.ARTIFACT_HASH_MISMATCH,
            f"artifact {item.artifact_id} is corrupt: {exc}",
        )
    except (ArtifactStoreError, OSError) as exc:
        return (
            BatchDisposition.HELD,
            HoldReason.ARTIFACT_NOT_FOUND,
            f"store access failure for {item.artifact_id}: {exc}",
        )

    # 2. Check candidate / finding headline grounding and raw text claims
    if isinstance(item, BatchCandidate):
        if item.note_rate is not None and "note_rate" not in item.locators:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "candidate has claimed note_rate without note_rate locator",
            )
        if item.apr is not None and "apr" not in item.locators:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "candidate has claimed apr without apr locator",
            )
        if (
            item.points is not None or item.points_not_applicable
        ) and "points" not in item.locators:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "candidate has claimed points without points locator",
            )
        if not item.locators and not item.tiers:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "candidate lacks evidence locators",
            )
        if not item.raw_text:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "candidate lacks raw text claim",
            )
    elif isinstance(item, BatchFinding):
        if item.scope is ProductScope.UNKNOWN and not item.locators:
            if item.rate_availability is not None:
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_MISSING,
                    "finding claims rate_availability without an evidence locator",
                )
            return (BatchDisposition.STAGED, None, None)
        if item.scope is not ProductScope.UNKNOWN and not item.locators:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "finding other than unknown must keep evidence locators",
            )
        if not item.raw_text:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_MISSING,
                "finding lacks raw text claim",
            )

    # 3. Check independent review record if attached or provided
    reviews = (review, item.independent_review if isinstance(item, BatchCandidate) else None)
    if any(r is not None and (not r.concurrence or r.disagreements) for r in reviews):
        return (
            BatchDisposition.HELD,
            HoldReason.INDEPENDENT_REVIEW_DISAGREEMENT,
            "independent review reported an unresolved disagreement",
        )

    # 4. Field locators verification
    if not item.locators and not (isinstance(item, BatchCandidate) and item.tiers):
        return (BatchDisposition.STAGED, None, None)

    # 4a. Only explicit supported material paths bound to a claim the item actually makes
    claims: dict[str, Any] = _present_claims(item) if isinstance(item, BatchCandidate) else {}
    path_keys: dict[str, str] = {}
    for path in item.locators:
        absent = False
        if isinstance(item, BatchCandidate):
            key = _CANDIDATE_PATHS.get(path)
            absent = key is not None and key not in claims
        else:
            key = _FINDING_PATHS.get(path)
            absent = (key == "availability" and item.rate_availability is None) or (
                key == "out_of_scope_product" and item.out_of_scope_product is None
            )
        if key is None:
            return _hold(
                HoldReason.LOCATOR_UNSUPPORTED,
                f"locator path {path!r} is not a supported material field path",
            )
        if absent:
            return _hold(
                HoldReason.LOCATOR_UNSUPPORTED,
                f"locator path {path!r} is bound to a field the item does not claim",
            )
        path_keys[path] = key

    candidate_row: _Node | None = None
    verified: set[str] = set()
    json_candidate_locator = False
    fail: _Fail | None

    for path, locator in item.locators.items():
        key = path_keys[path]
        haystack_context = ""
        raw_fragment = item.field_raw_texts.get(path)
        if raw_fragment is None:
            if len(item.locators) == 1:
                raw_fragment = item.raw_text
            else:
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_MISSING,
                    f"field {path!r} missing specific raw text in field_raw_texts",
                )

        if isinstance(locator, HtmlLocator):
            if locator.selector_type not in ("css", "xpath"):
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNSUPPORTED,
                    f"locator for {path}: selector type {locator.selector_type!r} is unsupported",
                )
            if locator.selector_type == "xpath" and isinstance(item, BatchCandidate):
                # The XPath subset cannot address a table cell, so a candidate value located by
                # XPath can never be bound to one row, its product, or its term.
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNSUPPORTED,
                    (
                        f"locator for {path}: xpath cannot be bound to a table row and column, "
                        f"so product and term proof is unsupported"
                    ),
                )

            # Check that selector does not address an entire row/table container token
            last_token = locator.selector.strip().split(">")[-1].strip().split()[-1]
            if re.search(r"\b(tr|table|tbody|thead|section|body)(?:$|\s|:)", last_token):
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    f"selector {locator.selector!r} resolves to container, not one full field",
                )

            if locator.selector_type == "css":
                try:
                    doc = _cached_document(body)
                    node = resolve_node(doc, locator.selector)
                except Exception as exc:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.LOCATOR_UNVERIFIED,
                        f"selector {locator.selector!r} could not be resolved: {exc}",
                    )

                if _is_container_node(node):
                    return (
                        BatchDisposition.HELD,
                        HoldReason.EVIDENCE_AMBIGUOUS,
                        (
                            f"selector {locator.selector!r} resolves to container <{node.tag}>, "
                            f"not one full field"
                        ),
                    )
                node_text = node.text()

                # Table row binding for candidate
                if isinstance(item, BatchCandidate):
                    row = nearest_row(node)
                    if row is None:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.LOCATOR_UNVERIFIED,
                            (
                                f"locator for {path} ({locator.selector!r}) does not resolve "
                                f"inside a table row"
                            ),
                        )
                    if candidate_row is not None and row is not candidate_row:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.EVIDENCE_AMBIGUOUS,
                            "candidate locators reference multiple conflicting table rows",
                        )
                    row_id_attr = row.attrs.get("id")
                    if row_id_attr and item.row_id and row_id_attr != item.row_id:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"locator for {path} resolves in row {row_id_attr!r}, does not "
                                f"match candidate row_id {item.row_id!r}"
                            ),
                        )
                    candidate_row = row

                    # Column semantic verification
                    cell = _nearest_cell(node)
                    table = _nearest_table(row)
                    row_cells = [c for c in row.children if c.tag in ("td", "th")]
                    col_idx = (
                        row_cells.index(cell) if (cell is not None and cell in row_cells) else -1
                    )
                    header_text = _find_column_header(table, col_idx) if col_idx >= 0 else None
                    col_ok, col_err = _verify_column_semantics(key, col_idx, header_text, cell)
                    haystack_context = " ".join(
                        [
                            header_text or "",
                            cell.attrs.get("class", "") if cell is not None else "",
                            cell.attrs.get("id", "") if cell is not None else "",
                        ]
                    )
                    if not col_ok:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            col_err,
                        )
            else:
                try:
                    node_text = resolve_text(
                        body, locator.selector, locator.selector_type, tolerant=True
                    )
                except Exception as exc:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.LOCATOR_UNVERIFIED,
                        f"xpath {locator.selector!r} could not be resolved: {exc}",
                    )

            computed_sha = hashlib.sha256(node_text.encode("utf-8")).hexdigest()
            if computed_sha != locator.normalized_text_sha256:
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNVERIFIED,
                    f"normalized text SHA-256 mismatch for selector {locator.selector!r}",
                )

            clean_node = node_text.strip()
            clean_frag = raw_fragment.strip()

            # Rate fields: check that node text resolves one full field rather than multiple rates
            if path in ("note_rate", "apr"):
                found_percents = re.findall(r"\b\d+(?:\.\d+)?%", clean_node)
                if len(found_percents) > 1:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.EVIDENCE_AMBIGUOUS,
                        (
                            f"node text contains multiple rate percentages ({found_percents}); "
                            f"does not resolve one full field"
                        ),
                    )

            # Compare raw fragment against node text
            if clean_node != clean_frag:
                if not fragment_is_located(clean_node, clean_frag):
                    if clean_frag in clean_node:
                        # Substring without unique token boundaries, e.g. "6.375%" inside "16.375%"
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"fragment {clean_frag!r} in {clean_node!r} is only an ungrounded "
                                f"substring"
                            ),
                        )
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        f"fragment {clean_frag!r} not found in node text at {locator.selector!r}",
                    )
                # Fragment is located with boundaries, but node_text has extra text
                if path in ("note_rate", "apr", "points") and len(clean_node.split()) > 3:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.EVIDENCE_AMBIGUOUS,
                        f"selector for {path} has extra unrelated text: {clean_node!r}",
                    )

            # Verify the located text discloses the claimed typed value with the right semantics
            if isinstance(item, BatchCandidate):
                if key in _CORE_KEYS:
                    fail = _core_value_error(key, item, clean_frag, "disclosed")
                else:
                    haystack = f"{clean_node} {haystack_context}"
                    fail = _spec_error(key, claims[key], clean_node, haystack)
            else:
                fail = _finding_error(item, key, clean_node)
            if fail is not None:
                return _hold(fail[0], fail[1])

        elif isinstance(locator, JsonLocator):
            if locator.response_sha256 != item.artifact_id:
                return (
                    BatchDisposition.HELD,
                    HoldReason.ARTIFACT_HASH_MISMATCH,
                    (
                        f"JSON locator response hash {locator.response_sha256} does not equal "
                        f"artifact {item.artifact_id}"
                    ),
                )
            try:
                json_doc = json.loads(body.decode("utf-8"), parse_float=_reject_float)
                val = resolve_json_pointer(json_doc, locator.pointer)
            except Exception as exc:
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNVERIFIED,
                    f"JSON pointer {locator.pointer!r} failed to resolve: {exc}",
                )

            if isinstance(val, (dict, list)):
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    (
                        f"JSON pointer {locator.pointer!r} resolves to container "
                        f"{type(val).__name__}, not one full field"
                    ),
                )

            val_str = str(val).strip() if val is not None else ""
            clean_frag = raw_fragment.strip()
            # Exact scalar match in JSON (no substring)
            if val_str != clean_frag:
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"JSON value {val_str!r} at pointer {locator.pointer!r} does not match "
                        f"fragment {clean_frag!r}"
                    ),
                )

            # Typed value verification for JSON
            if isinstance(item, BatchCandidate):
                if key not in _CORE_KEYS:
                    return _hold(
                        HoldReason.LOCATOR_UNSUPPORTED,
                        f"JSON locator for {path}: no typed semantic check for this claim",
                    )
                fail = _core_value_error(key, item, val_str, "JSON")
                json_candidate_locator = True
            else:
                fail = _finding_error(item, key, val_str)
            if fail is not None:
                return _hold(fail[0], fail[1])

        elif isinstance(locator, (PdfLocator, ImageOcrLocator, RenderedDomLocator)):
            return (
                BatchDisposition.HELD,
                HoldReason.LOCATOR_UNSUPPORTED,
                (
                    f"locator kind {locator.kind!r} is unsupported for automated candidate "
                    f"verification"
                ),
            )
        else:
            return (
                BatchDisposition.HELD,
                HoldReason.LOCATOR_UNSUPPORTED,
                f"unsupported evidence locator kind: {type(locator).__name__}",
            )
        verified.add(key)

    if json_candidate_locator:
        # JSON scalars carry no table row/column: product and term proof cannot be bound, and
        # nothing may be inferred from key names. Raw/opaque facts stay private under hold.
        return _hold(
            HoldReason.LOCATOR_UNSUPPORTED,
            "JSON candidate locators cannot be bound to a product row and term; held unsupported",
        )

    # 5. Verify candidate tier locators if present
    tier_rows: list[tuple[Any, _Node]] = []
    if isinstance(item, BatchCandidate) and item.tiers:
        for tier in item.tiers:
            if tier.locator is None:
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_MISSING,
                    f"tier {tier.tier_id} lacks evidence locator",
                )
            if not isinstance(tier.locator, HtmlLocator):
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNSUPPORTED,
                    f"tier {tier.tier_id} has unsupported locator kind {tier.locator.kind!r}",
                )

            if tier.locator.selector_type not in ("css", "xpath"):
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNSUPPORTED,
                    (
                        f"tier {tier.tier_id}: selector type {tier.locator.selector_type!r} is "
                        f"unsupported"
                    ),
                )
            if tier.locator.selector_type == "xpath":
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNSUPPORTED,
                    (
                        f"tier {tier.tier_id}: xpath cannot be bound to a table row and column, "
                        f"so tier proof is unsupported"
                    ),
                )

            last_token = tier.locator.selector.strip().split(">")[-1].strip().split()[-1]
            if re.search(r"\b(tr|table|tbody|thead|section|body)(?:$|\s|:)", last_token):
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    (
                        f"tier {tier.tier_id} selector {tier.locator.selector!r} resolves to "
                        f"container, not one full field"
                    ),
                )

            try:
                doc = _cached_document(body)
                tier_node = resolve_node(doc, tier.locator.selector)
            except Exception as exc:
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNVERIFIED,
                    (
                        f"tier {tier.tier_id} selector {tier.locator.selector!r} failed to "
                        f"resolve: {exc}"
                    ),
                )

            if _is_container_node(tier_node):
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    (
                        f"tier {tier.tier_id} selector {tier.locator.selector!r} resolves to "
                        f"container <{tier_node.tag}>, not one full field"
                    ),
                )

            tier_node_text = tier_node.text()
            computed_sha = hashlib.sha256(tier_node_text.encode("utf-8")).hexdigest()
            if computed_sha != tier.locator.normalized_text_sha256:
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNVERIFIED,
                    (
                        f"tier {tier.tier_id} normalized text SHA-256 mismatch for selector "
                        f"{tier.locator.selector!r}"
                    ),
                )

            tier_row = nearest_row(tier_node)
            if tier_row is None:
                return (
                    BatchDisposition.HELD,
                    HoldReason.LOCATOR_UNVERIFIED,
                    (
                        f"tier {tier.tier_id} selector {tier.locator.selector!r} does not resolve "
                        f"inside a table row"
                    ),
                )
            tier_row_id = tier_row.attrs.get("id")
            if tier_row_id and item.row_id and tier_row_id != item.row_id:
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"tier {tier.tier_id} is in row {tier_row_id!r}, does not match candidate "
                        f"row_id {item.row_id!r}"
                    ),
                )
            if candidate_row is not None and tier_row is not candidate_row:
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    (
                        f"tier {tier.tier_id} is in a different table row ({tier_row_id!r}) from "
                        f"candidate offer ({candidate_row.attrs.get('id')!r})"
                    ),
                )
            candidate_row = tier_row

            clean_tier_node = tier_node_text.strip()
            clean_tier_frag = tier.raw_text.strip()
            if clean_tier_node != clean_tier_frag and not fragment_is_located(
                clean_tier_node, clean_tier_frag
            ):
                if clean_tier_frag in clean_tier_node:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        (
                            f"tier {tier.tier_id} fragment {clean_tier_frag!r} in "
                            f"{clean_tier_node!r} is only an ungrounded substring"
                        ),
                    )
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"tier {tier.tier_id} fragment {clean_tier_frag!r} not found in node "
                        f"text at {tier.locator.selector!r}"
                    ),
                )

            # One tier locator is one cell: it can prove only one rate-type claim, in the right
            # column. A second claim would borrow evidence from a sibling cell.
            tier_rate_claims = [
                name
                for name, claimed in (
                    ("note_rate", tier.note_rate),
                    ("apr", tier.apr),
                    ("points", tier.points),
                )
                if claimed is not None
            ]
            if len(tier_rate_claims) > 1:
                return (
                    BatchDisposition.HELD,
                    HoldReason.EVIDENCE_AMBIGUOUS,
                    (
                        f"tier {tier.tier_id} claims {tier_rate_claims} from one locator; each "
                        f"rate-type claim needs its own verified cell"
                    ),
                )
            if tier_rate_claims:
                tier_cell = _nearest_cell(tier_node)
                tier_cells = [c for c in tier_row.children if c.tag in ("td", "th")]
                tier_col = (
                    tier_cells.index(tier_cell)
                    if (tier_cell is not None and tier_cell in tier_cells)
                    else -1
                )
                tier_header = (
                    _find_column_header(_nearest_table(tier_row), tier_col)
                    if tier_col >= 0
                    else None
                )
                tier_col_ok, tier_col_err = _verify_column_semantics(
                    tier_rate_claims[0], tier_col, tier_header, tier_cell
                )
                if not tier_col_ok:
                    return (BatchDisposition.HELD, HoldReason.RAW_TEXT_MISMATCH, tier_col_err)

            if tier.note_rate is not None:
                disclosed = _extract_numeric_percent(clean_tier_frag)
                if disclosed != tier.note_rate:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        (
                            f"tier {tier.tier_id} disclosed rate {disclosed}% does not match "
                            f"claimed {tier.note_rate}"
                        ),
                    )
            if tier.apr is not None:
                disclosed_apr = _extract_numeric_percent(clean_tier_frag)
                if disclosed_apr != tier.apr:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        (
                            f"tier {tier.tier_id} disclosed APR {disclosed_apr}% does not match "
                            f"claimed {tier.apr}"
                        ),
                    )
            if getattr(tier, "points", None) is not None:
                disclosed_pts = _extract_points_value(clean_tier_frag)
                if disclosed_pts != tier.points:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        (
                            f"tier {tier.tier_id} disclosed points {disclosed_pts} does not match "
                            f"claimed {tier.points}"
                        ),
                    )
            tier_rows.append((tier, tier_row))

    # 6. Check product and purpose binding to the resolved table row
    if isinstance(item, BatchCandidate) and candidate_row is not None:
        table = _nearest_table(candidate_row)
        row_cells = [c for c in candidate_row.children if c.tag in ("td", "th")]
        if not row_cells:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_AMBIGUOUS,
                "candidate table row contains no cells",
            )

        # 6a. Identify product cell and term cell
        prod_cell: _Node | None = None
        term_cell: _Node | None = None

        for idx in range(len(row_cells)):
            h_text = _find_column_header(table, idx)
            if h_text:
                hl = h_text.lower()
                if (
                    re.search(r"\b(product|loan|program|description|type)\b", hl)
                    and prod_cell is None
                ):
                    prod_cell = row_cells[idx]
                elif re.search(r"\b(term|months|years|period)\b", hl) and term_cell is None:
                    term_cell = row_cells[idx]

        if prod_cell is None:
            for c in row_cells:
                classes = c.attrs.get("class", "").lower().split()
                if any(cls in ("prod", "product", "loan-name", "program") for cls in classes):
                    prod_cell = c
                    break
        if term_cell is None:
            for c in row_cells:
                classes = c.attrs.get("class", "").lower().split()
                if any(cls in ("term", "duration", "period") for cls in classes):
                    term_cell = c
                    break

        if prod_cell is None and row_cells[0].text():
            first_text = row_cells[0].text().strip()
            if not re.search(r"^\d+(?:\.\d+)?%?$", first_text):
                prod_cell = row_cells[0]

        if prod_cell is None:
            return (
                BatchDisposition.HELD,
                HoldReason.EVIDENCE_AMBIGUOUS,
                "candidate table row has no identifiable product heading or cell",
            )

        prod_text = prod_cell.text().strip()
        prod_text_lower = prod_text.lower()

        # 6b. Product family semantic binding
        if item.product_family is ProductFamily.MORTGAGE:
            if re.search(
                r"\b(heloc|home\s+equity\s+line|line\s+of\s+credit|second\s+mortgage)\b",
                prod_text_lower,
            ):
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"row product {prod_text!r} discloses HELOC/second mortgage, cannot bind "
                        f"to product_family mortgage"
                    ),
                )
        elif item.product_family is ProductFamily.HELOC:
            if re.search(
                r"\b(fixed\s+conforming|first\s+mortgage|purchase\s+mortgage)\b", prod_text_lower
            ):
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"row product {prod_text!r} discloses fixed mortgage, cannot bind to "
                        f"product_family heloc"
                    ),
                )
        elif item.product_family is ProductFamily.HOME_EQUITY_LOAN and re.search(
            r"\b(line\s+of\s+credit|heloc)\b", prod_text_lower
        ):
            return (
                BatchDisposition.HELD,
                HoldReason.RAW_TEXT_MISMATCH,
                f"row product {prod_text!r} discloses line of credit, cannot bind to "
                "product_family home_equity_loan",
            )

        # 6c. Loan purpose semantic binding
        if item.loan_purpose is LoanPurpose.PURCHASE:
            if re.search(r"\b(refinance|refi|cash-out)\b", prod_text_lower):
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"row product {prod_text!r} specifies refinance, cannot bind to "
                        f"loan_purpose purchase"
                    ),
                )
        elif (
            item.loan_purpose in (LoanPurpose.RATE_TERM_REFINANCE, LoanPurpose.CASH_OUT_REFINANCE)
            and re.search(r"\bpurchase\b", prod_text_lower)
            and not re.search(r"\b(refinance|refi)\b", prod_text_lower)
        ):
            return (
                BatchDisposition.HELD,
                HoldReason.RAW_TEXT_MISMATCH,
                f"row product {prod_text!r} specifies purchase, cannot bind to "
                f"loan_purpose {item.loan_purpose.value}",
            )

        # 6d. Term semantic binding
        prod_term_months = _parse_disclosed_term_months(prod_text)
        if (
            prod_term_months is not None
            and item.term_months is not None
            and prod_term_months != item.term_months
        ):
            return (
                BatchDisposition.HELD,
                HoldReason.RAW_TEXT_MISMATCH,
                f"row product {prod_text!r} specifies {prod_term_months} months, conflicts "
                f"with candidate term_months {item.term_months}",
            )

        if term_cell is not None and item.term_months is not None:
            cell_term_months = _parse_disclosed_term_months(term_cell.text().strip())
            if cell_term_months is not None and cell_term_months != item.term_months:
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    (
                        f"row term cell {term_cell.text()!r} specifies {cell_term_months} months, "
                        f"conflicts with candidate term_months {item.term_months}"
                    ),
                )

        # 6e. Product ID semantic consistency
        if item.product_id:
            pid_lower = item.product_id.lower()
            if re.search(r"\b30y\b|30-year", pid_lower):
                if prod_term_months is not None and prod_term_months != 360:
                    return (
                        BatchDisposition.HELD,
                        HoldReason.RAW_TEXT_MISMATCH,
                        (
                            f"row product {prod_text!r} conflicts with 30-year product_id "
                            f"{item.product_id!r}"
                        ),
                    )
            elif (
                re.search(r"\b15y\b|15-year", pid_lower)
                and prod_term_months is not None
                and prod_term_months != 180
            ):
                return (
                    BatchDisposition.HELD,
                    HoldReason.RAW_TEXT_MISMATCH,
                    f"row product {prod_text!r} conflicts with 15-year product_id "
                    f"{item.product_id!r}",
                )

        # 6f. Raw text grounding check
        first_prod_token = prod_text.split()[0] if prod_text.split() else ""
        if not (
            fragment_is_located(item.raw_text, prod_text)
            or fragment_is_located(
                prod_text, item.raw_text.split()[0] if item.raw_text.split() else ""
            )
            or first_prod_token in item.raw_text
        ):
            return (
                BatchDisposition.HELD,
                HoldReason.RAW_TEXT_MISMATCH,
                (
                    f"row product {prod_text!r} cannot be bound to candidate raw_text "
                    f"{item.raw_text!r}"
                ),
            )

    # 7. Tier bounds, conditions, and coverage: every present material claim is verified
    if isinstance(item, BatchCandidate):
        for bound_tier, bound_row in tier_rows:
            bounds_verdict = _tier_bounds_error(bound_tier, bound_row)
            if bounds_verdict is not None:
                return bounds_verdict
        for condition_index, condition in enumerate(item.conditions):
            condition_verdict = _verify_condition(
                condition_index, condition, body, candidate_row, item
            )
            if condition_verdict is not None:
                return condition_verdict
        for claim_key in claims:
            if claim_key not in verified:
                return _hold(
                    HoldReason.EVIDENCE_MISSING,
                    f"candidate claims {claim_key} without a verified {claim_key} locator",
                )
    else:
        for finding_key, finding_claimed in (
            ("availability", item.rate_availability is not None),
            ("out_of_scope_product", item.out_of_scope_product is not None),
        ):
            if finding_claimed and finding_key not in verified:
                return _hold(
                    HoldReason.EVIDENCE_MISSING,
                    f"finding claims {finding_key} without a verified {finding_key} locator",
                )

    return (BatchDisposition.STAGED, None, None)

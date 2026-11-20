"""Verification of rate candidate and finding evidence locators against stored bytes.

Checks that claimed artifacts exist, digests match, and field locators (HTML,
JSON) accurately pinpoint exact source text without ambiguity. Unsupported formats
fail closed to held status.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    LoanPurpose,
    ProductFamily,
    ProductScope,
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
            if not re.search(r"\bterms?\b|\bmonths?\b|\byears?\b|\byrs?\b|\bperiod\b", h):
                return False, f"locator for term_months resolves to non-term column {header_text!r}"
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
        if not any(c in classes_set for c in ("term", "months", "years")) and "term" not in cell_id:
            return (
                False,
                f"locator for term_months resolves to non-term cell (class={class_attribute!r})",
            )
        return True, None

    return True, None


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
    4. Table row isolation: selectors for an offer must not mix unrelated rows.
    5. Locator resolution: resolves one full field and matches disclosed typed value exactly.
    6. Unbounded substrings (for example, 6.375% inside 16.375%) are held.
    7. Unsupported formats (PDF, OCR, Rendered DOM, and JSON tiers) fail closed to held status.

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

    candidate_row: _Node | None = None

    for path, locator in item.locators.items():
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
                    col_ok, col_err = _verify_column_semantics(path, col_idx, header_text, cell)
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

            # Verify disclosed typed value matches candidate field claim
            if isinstance(item, BatchCandidate):
                if path == "note_rate" and item.note_rate is not None:
                    disclosed = _extract_numeric_percent(clean_frag)
                    if disclosed != item.note_rate:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"disclosed rate {disclosed}% does not equal claimed note_rate "
                                f"{item.note_rate}"
                            ),
                        )
                elif path == "apr" and item.apr is not None:
                    disclosed = _extract_numeric_percent(clean_frag)
                    if disclosed != item.apr:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            f"disclosed APR {disclosed}% does not equal claimed apr {item.apr}",
                        )
                elif path == "points":
                    if item.points_not_applicable:
                        if clean_frag.casefold() not in {"n/a", "not applicable"}:
                            return (
                                BatchDisposition.HELD,
                                HoldReason.RAW_TEXT_MISMATCH,
                                "points cell does not explicitly state not applicable",
                            )
                    elif item.points is not None:
                        disclosed_pts = _extract_points_value(clean_frag)
                        if disclosed_pts != item.points:
                            return (
                                BatchDisposition.HELD,
                                HoldReason.RAW_TEXT_MISMATCH,
                                (
                                    f"disclosed points {disclosed_pts} does not equal claimed "
                                    f"points {item.points}"
                                ),
                            )
                elif (
                    path in ("term_months", "details.term_months") and item.term_months is not None
                ):
                    disclosed_term = _parse_disclosed_term_months(clean_frag)
                    if disclosed_term is None:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"disclosed term {clean_frag!r} does not disclose an explicit "
                                f"months/years unit"
                            ),
                        )
                    if disclosed_term != item.term_months:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"disclosed term {disclosed_term} months does not match candidate "
                                f"term_months {item.term_months}"
                            ),
                        )

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
                if path == "note_rate" and item.note_rate is not None:
                    disclosed = _extract_numeric_percent(val_str)
                    if disclosed != item.note_rate:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"JSON rate {disclosed}% does not equal claimed note_rate "
                                f"{item.note_rate}"
                            ),
                        )
                elif path == "apr" and item.apr is not None:
                    disclosed = _extract_numeric_percent(val_str)
                    if disclosed != item.apr:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            f"JSON APR {disclosed}% does not equal claimed apr {item.apr}",
                        )
                elif path == "points":
                    if item.points_not_applicable and val_str.casefold() not in {
                        "n/a",
                        "not applicable",
                    }:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            "JSON points does not explicitly state not applicable",
                        )
                    if item.points is not None and _extract_points_value(val_str) != item.points:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            f"JSON points does not equal claimed points {item.points}",
                        )
                elif (
                    path in ("term_months", "details.term_months") and item.term_months is not None
                ):
                    disclosed_term = _parse_disclosed_term_months(val_str)
                    if disclosed_term is None:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"JSON term {val_str!r} does not disclose an explicit months/years "
                                f"unit"
                            ),
                        )
                    if disclosed_term != item.term_months:
                        return (
                            BatchDisposition.HELD,
                            HoldReason.RAW_TEXT_MISMATCH,
                            (
                                f"JSON term {disclosed_term} months does not equal claimed "
                                f"term_months {item.term_months}"
                            ),
                        )

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

    # 5. Verify candidate tier locators if present
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

    return (BatchDisposition.STAGED, None, None)

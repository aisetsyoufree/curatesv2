"""Slice-only extractor for the three committed synthetic fixtures.

T040 builds the real adapter. This parser knows these fixtures and nothing else.
Every candidate cites a CSS locator whose text is taken from the artifact bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.evidence import ExtractionProvenance, HtmlLocator, ParserVersion, Provenance
from cu_rates.contracts.sources import ExtractedCandidate

SLICE_PARSER = ParserVersion(name="t014-slice-html", version="0.1.0")
SLICE_CONFIGURATION = hashlib.sha256(b"t014-slice-html:0.1.0").hexdigest()
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "seed"

_ATHOL_LABEL = "Conventional conforming fixed purchase"
_EQUITY_LABEL = "Closed-end fixed second mortgage"


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node]
    parts: list[str]

    def text(self) -> str:
        own = "".join(self.parts)
        return own + "".join(child.text() for child in self.children)


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, [], [])
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs}, [], [])
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].parts.append(data)


def layout_sha256(body: bytes) -> str:
    """Slice-only stand-in for the T040 layout digest: the first table's bytes."""
    start = body.find(b"<table>")
    end = body.find(b"</table>")
    chunk = body[start : end + len(b"</table>")] if 0 <= start < end else body
    return hashlib.sha256(chunk).hexdigest()


def _parse(body: bytes) -> _Node:
    parser = _Tree()
    parser.feed(body.decode("utf-8"))
    return parser.root


def _walk(node: _Node) -> list[_Node]:
    found = [node]
    for child in node.children:
        found.extend(_walk(child))
    return found


def _by_id(root: _Node, element_id: str) -> _Node:
    for node in _walk(root):
        if node.attrs.get("id") == element_id:
            return node
    raise ValueError(f"fixture row {element_id} is not in the artifact")


def _cell(row: _Node, index: int) -> str:
    elements = [child for child in row.children if child.tag]
    if index < 1 or index > len(elements):
        raise ValueError(f"row has no cell {index}")
    return " ".join(elements[index - 1].text().split())


def resolve_text(body: bytes, selector: str) -> str:
    """Return the artifact text a slice locator names. Unknown selectors fail closed."""
    root = _parse(body)
    cell = re.fullmatch(r"#([A-Za-z0-9_-]+) td:nth-child\(([0-9]+)\)", selector)
    if cell:
        return _cell(_by_id(root, cell.group(1)), int(cell.group(2)))
    heading = re.fullmatch(r"h2:nth-of-type\(([0-9]+)\) \+ p", selector)
    if heading:
        headings = [node for node in _walk(root) if node.tag == "h2"]
        index = int(heading.group(1)) - 1
        if index < 0 or index >= len(headings):
            raise ValueError(f"selector {selector} does not resolve")
        return " ".join(_paragraph_after(root, headings[index].text()).text().split())
    if selector == "a[href]":
        link = next(
            (
                node
                for node in _walk(root)
                if node.tag == "a" and node.attrs.get("href", "").startswith("https://")
            ),
            None,
        )
        if link is None:
            raise ValueError("selector a[href] does not resolve")
        return link.attrs["href"]
    raise ValueError(f"unsupported slice selector {selector}")


def _locator(selector: str, node_text: str) -> HtmlLocator:
    return HtmlLocator(
        selector_type="css",
        selector=selector,
        normalized_text_sha256=hashlib.sha256(node_text.encode()).hexdigest(),
    )


def _candidate(
    *,
    path: str,
    text: str,
    selector: str,
    node_text: str,
    body: bytes,
    artifact_id: str,
    extraction_run_id: str,
    git_sha: str,
    candidate_id: str,
) -> ExtractedCandidate:
    if resolve_text(body, selector) != node_text:
        raise ValueError(f"{selector} does not resolve to the cited node")
    if text.encode() not in body or node_text.encode() not in body or text not in node_text:
        raise ValueError(f"{path} is not in the artifact bytes")
    provenance = Provenance(
        artifact_id=artifact_id,
        locator=_locator(selector, node_text),
        source_text=text,
        extraction=ExtractionProvenance(
            git_sha=git_sha,
            parser=SLICE_PARSER,
            configuration_sha256=SLICE_CONFIGURATION,
            normalization_version="t014.slice",
            rule_id="t014.slice-extract",
        ),
    )
    return ExtractedCandidate(
        schema_version=CONTRACT_VERSION,
        candidate_id=candidate_id,
        extraction_run_id=extraction_run_id,
        proposed_path=path,
        raw_value=text,
        provenance=provenance,
    )


@dataclass(frozen=True)
class ExtractedRow:
    row_id: str
    candidates: tuple[ExtractedCandidate, ...]


@dataclass(frozen=True)
class NonOffer:
    """A visible fixture fact that must not become a mortgage offer."""

    finding_id: str
    label: str
    scope: str
    family: str | None
    out_of_scope_product: str | None
    rate_availability: str | None
    provenance: Provenance


@dataclass(frozen=True)
class FixtureExtraction:
    rows: tuple[ExtractedRow, ...]
    non_offers: tuple[NonOffer, ...]


def _catalog() -> dict[str, Any]:
    payload = json.loads((FIXTURES / "expected_outcomes.json").read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert isinstance(cases, list)
    return {str(case["case_id"]): case for case in cases}


def _cite(
    body: bytes,
    artifact_id: str,
    extraction_run_id: str,
    git_sha: str,
    row_id: str,
    path: str,
    text: str,
    cell: int,
    node_text: str,
) -> ExtractedCandidate:
    return _candidate(
        path=path,
        text=text,
        selector=f"#{row_id} td:nth-child({cell})",
        node_text=node_text,
        body=body,
        artifact_id=artifact_id,
        extraction_run_id=extraction_run_id,
        git_sha=git_sha,
        candidate_id=f"{row_id}.{path.replace('.', '-')}",
    )


def _rate_row(
    body: bytes,
    root: _Node,
    case_row: dict[str, object],
    *,
    artifact_id: str,
    extraction_run_id: str,
    git_sha: str,
    mortgage: bool,
) -> ExtractedRow:
    row_id = str(case_row["row_id"])
    node = _by_id(root, row_id)
    months = int(str(case_row["term_months"]))
    term = f"{months // 12} years ({months} months)"
    label = _ATHOL_LABEL if mortgage else _EQUITY_LABEL
    assumptions = _cell(node, 6)
    cells = {
        "product_family": (label, 1),
        "details.term_months": (term, 2),
        "note_rate": (f"{case_row['note_rate']}%", 3),
        "apr": (f"{case_row['apr']}%", 4),
        "points": (str(case_row["points"]), 5),
    }
    if mortgage:
        cells["details.purpose"] = (label, 1)
        cells["details.rate_type"] = (label, 1)
    else:
        cells["details.lien_position"] = (label, 1)
    candidates = [
        _cite(
            body,
            artifact_id,
            extraction_run_id,
            git_sha,
            row_id,
            path,
            text,
            cell,
            _cell(node, cell),
        )
        for path, (text, cell) in cells.items()
    ]
    statements = _statements(mortgage, assumptions)
    candidates.extend(
        _cite(
            body,
            artifact_id,
            extraction_run_id,
            git_sha,
            row_id,
            path,
            text,
            6,
            assumptions,
        )
        for path, text in statements.items()
    )
    return ExtractedRow(row_id, tuple(candidates))


def _statements(mortgage: bool, assumptions: str) -> dict[str, str]:
    if mortgage:
        required = {
            "details.lock_days": "30 days",
            "scenario.occupancy": "Owner-occupied single-family primary residence",
            "scenario.property_type": "single-family",
            "scenario.property_value": "$500,000",
            "scenario.loan_amount": "$400,000",
            "scenario.ltv": "80%",
            "scenario.credit_score": "780",
            "scenario.relationship_discount": "No relationship discount",
            "scenario.autopay_discount": "No autopay discount",
        }
    else:
        required = {
            "scenario.occupancy": "Owner-occupied single-family primary residence",
            "scenario.property_type": "single-family",
            "scenario.property_value": "$500,000",
            "scenario.first_lien_balance": "$350,000",
            "scenario.loan_amount": "$50,000",
            "scenario.cltv": "80%",
            "scenario.credit_score": "780",
            "scenario.relationship_discount": "No relationship discount",
        }
    missing = [text for text in required.values() if text not in assumptions]
    if missing:
        raise ValueError(f"assumption cell does not contain {missing[0]}")
    return required


def _paragraph_after(root: _Node, heading: str) -> _Node:
    nodes = _walk(root)
    for index, node in enumerate(nodes):
        if node.tag == "h2" and heading in node.text():
            for follower in nodes[index + 1 :]:
                if follower.tag == "p":
                    return follower
    raise ValueError(f"no paragraph follows {heading}")


def _non_offer(
    *,
    finding_id: str,
    label: str,
    scope: str,
    family: str | None,
    out_of_scope_product: str | None,
    rate_availability: str | None,
    text: str,
    selector: str,
    node_text: str,
    body: bytes,
    artifact_id: str,
    extraction_run_id: str,
    git_sha: str,
) -> NonOffer:
    candidate = _candidate(
        path="product_family",
        text=text,
        selector=selector,
        node_text=node_text,
        body=body,
        artifact_id=artifact_id,
        extraction_run_id=extraction_run_id,
        git_sha=git_sha,
        candidate_id=finding_id,
    )
    return NonOffer(
        finding_id=finding_id,
        label=label,
        scope=scope,
        family=family,
        out_of_scope_product=out_of_scope_product,
        rate_availability=rate_availability,
        provenance=candidate.provenance,
    )


def extract_fixture(
    case_id: str,
    body: bytes,
    *,
    artifact_id: str,
    extraction_run_id: str,
    git_sha: str,
) -> FixtureExtraction:
    """Extract one committed fixture. Unknown pages yield no rows and no offers."""
    case = _catalog().get(case_id)
    if case is None:
        raise ValueError(f"unknown fixture {case_id}")
    root = _parse(body)
    rows: list[ExtractedRow] = []
    non_offers: list[NonOffer] = []
    if case_id == "athol-style":
        for case_row in case["rows"]:
            assert isinstance(case_row, dict)
            rows.append(
                _rate_row(
                    body,
                    root,
                    case_row,
                    artifact_id=artifact_id,
                    extraction_run_id=extraction_run_id,
                    git_sha=git_sha,
                    mortgage=True,
                )
            )
    elif case_id == "stoughton-style":
        labels = {
            "auto-new-36": ("New vehicle loan", "vehicle"),
            "auto-used-60": ("Used vehicle loan", "vehicle"),
            "personal-unsecured-36": ("Unsecured personal loan", "unsecured_personal"),
        }
        for case_row in case["rows"]:
            assert isinstance(case_row, dict)
            row_id = str(case_row["row_id"])
            label, kind = labels[row_id]
            node = _by_id(root, row_id)
            non_offers.append(
                _non_offer(
                    finding_id=f"finding-{row_id}",
                    label=label,
                    scope="out_of_scope",
                    family=None,
                    out_of_scope_product=kind,
                    rate_availability=None,
                    text=label,
                    selector=f"#{row_id} td:nth-child(1)",
                    node_text=_cell(node, 1),
                    body=body,
                    artifact_id=artifact_id,
                    extraction_run_id=extraction_run_id,
                    git_sha=git_sha,
                )
            )
    elif case_id == "worcester-style":
        quote = _paragraph_after(root, "First mortgage")
        quote_text = " ".join(quote.text().split())
        non_offers.append(
            _non_offer(
                finding_id="finding-worcester-quote",
                label="First mortgage is quote-only on this page",
                scope="in_scope",
                family="mortgage",
                out_of_scope_product=None,
                rate_availability="quote_only",
                text=quote_text,
                selector="h2:nth-of-type(1) + p",
                node_text=quote_text,
                body=body,
                artifact_id=artifact_id,
                extraction_run_id=extraction_run_id,
                git_sha=git_sha,
            )
        )
        link = next(
            node
            for node in _walk(root)
            if node.tag == "a" and node.attrs.get("href", "").startswith("https://")
        )
        href = link.attrs["href"]
        non_offers.append(
            _non_offer(
                finding_id="finding-worcester-partner",
                label="Partner link is not an institution rate",
                scope="unknown",
                family=None,
                out_of_scope_product=None,
                rate_availability="unknown",
                text=href,
                selector="a[href]",
                node_text=href,
                body=body,
                artifact_id=artifact_id,
                extraction_run_id=extraction_run_id,
                git_sha=git_sha,
            )
        )
        second = case["second_mortgage_table"]
        assert isinstance(second, dict)
        for case_row in second["rows"]:
            assert isinstance(case_row, dict)
            rows.append(
                _rate_row(
                    body,
                    root,
                    case_row,
                    artifact_id=artifact_id,
                    extraction_run_id=extraction_run_id,
                    git_sha=git_sha,
                    mortgage=False,
                )
            )
    return FixtureExtraction(tuple(rows), tuple(non_offers))

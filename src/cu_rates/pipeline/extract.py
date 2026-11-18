"""Offline extractor for explicitly identified synthetic pages; never reads the test oracle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.evidence import ExtractionProvenance, HtmlLocator, ParserVersion, Provenance
from cu_rates.contracts.sources import ExtractedCandidate
from cu_rates.normalize._values import months

SLICE_PARSER = ParserVersion(name="t014-slice-html", version="0.2.0")
SLICE_CONFIGURATION = hashlib.sha256(b"t014-slice-html:0.2.0").hexdigest()
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "seed"
_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_PAGE = {
    "athol-style": "Example North Charter mortgage rates",
    "athol-style-dated": "Example North Charter mortgage rates",
    "worcester-style": "Example Central Charter home lending",
    "stoughton-style": "Example South Charter consumer loans",
}
_HEADERS = ("Product", "Term", "Note rate", "APR", "Points", "Assumptions")
_PURCHASE = "Conventional conforming fixed purchase"
_EQUITY = "Closed-end fixed second mortgage"
_QUOTE = "Call for a quote. This page does not advertise a first-mortgage note rate or APR."
_PARTNER = "https://housing-partner.example/programs/example-housing"
_ID = r"[a-z][a-z0-9_-]*"


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    parts: list[str | _Node] = field(default_factory=list)

    @property
    def children(self) -> list[_Node]:
        return [part for part in self.parts if isinstance(part, _Node)]

    def raw(self) -> str:
        return "".join(item if isinstance(item, str) else item.raw() for item in self.parts)

    def text(self) -> str:
        return " ".join(self.raw().split())

    def walk(self) -> Iterator[_Node]:
        yield self
        for child in self.children:
            yield from child.walk()


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if len(values) != len(attrs):
            raise ValueError("duplicate HTML attributes")
        node = _Node(tag, values, self.current)
        self.current.parts.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_endtag(self, tag: str) -> None:
        if self.current.tag != tag or self.current.parent is None:
            raise ValueError("fixture HTML is not explicitly balanced")
        self.current = self.current.parent

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            raise ValueError("self-closing non-void tag in fixture HTML")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.current.parts.append(data)


def _parse(body: bytes) -> _Node:
    parser = _Tree()
    parser.feed(body.decode("utf-8"))
    parser.close()
    if parser.current is not parser.root:
        raise ValueError("fixture HTML is incomplete")
    return parser.root


def _only(nodes: Iterator[_Node], description: str) -> _Node:
    first = next(nodes, None)
    if first is None or next(nodes, None) is not None:
        raise ValueError(f"{description} does not resolve uniquely")
    return first


def _by_id(root: _Node, element_id: str) -> _Node:
    return _only((node for node in root.walk() if node.attrs.get("id") == element_id), element_id)


def resolve_text(body: bytes, selector: str, selector_type: str = "css") -> str:
    """Resolve only the slice's explicit CSS subset and XPath attribute form."""
    root = _parse(body)
    if selector_type == "xpath":
        match = re.fullmatch(r"//a\[@href='([^']+)'\]/@href", selector)
        if match:
            href = match[1]
            return _only(
                (n for n in root.walk() if n.tag == "a" and n.attrs.get("href") == href), selector
            ).attrs["href"]
    elif selector_type == "css":
        match = re.fullmatch(
            rf"#({_ID})(?: > ([a-z][a-z0-9]*):nth-child\(([1-9][0-9]*)\))?", selector
        )
        if match:
            node = _by_id(root, match[1])
            if match[2] is None:
                return node.text()
            index = int(match[3]) - 1
            children = node.children
            if index < len(children) and children[index].tag == match[2]:
                return children[index].text()
        match = re.fullmatch(r"h2:nth-of-type\(([1-9][0-9]*)\) \+ p", selector)
        if match:
            number = int(match[1])
            return _only(
                (
                    following
                    for parent in root.walk()
                    for index, (previous, following) in enumerate(
                        zip(parent.children, parent.children[1:], strict=False)
                    )
                    if previous.tag == "h2"
                    and following.tag == "p"
                    and sum(sibling.tag == "h2" for sibling in parent.children[: index + 1])
                    == number
                ),
                selector,
            ).text()
        if selector == "a[href]":
            return _only(
                (n for n in root.walk() if n.tag == "a" and "href" in n.attrs), selector
            ).text()
    raise ValueError(f"unsupported or unresolvable {selector_type} selector {selector}")


def layout_sha256(body: bytes) -> str:
    """Bind the approved tag/column/locator structure, excluding rate and date values."""

    def shape(node: _Node) -> object:
        label = node.text() if node.tag in {"h1", "h2", "caption", "th", "title"} else None
        return [node.tag, node.attrs, label, [shape(child) for child in node.children]]

    payload = json.dumps(shape(_parse(body)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _supporting_excerpts(text: str) -> dict[str, str]:
    """Exactly labeled sentences, never partial numbers, inferred values, or negation."""
    prefixes = {
        "Property value ": "scenario.property_value",
        "Loan amount ": "scenario.loan_amount",
        "Second-lien loan ": "scenario.loan_amount",
        "Existing first lien ": "scenario.first_lien_balance",
        "Loan-to-value ": "scenario.ltv",
        "Combined loan-to-value ": "scenario.cltv",
        "Credit score ": "scenario.credit_score",
        "Lock period ": "details.lock_days",
        "Points ": "points",
        "Term ": "details.term_months",
    }
    found: dict[str, str] = {}
    for sentence in text.rstrip(".").split(". "):
        match: tuple[str, str] | None = None
        if sentence == "Owner-occupied single-family primary residence":
            match = ("scenario.occupancy", sentence)
            found["scenario.property_type"] = "single-family"
        elif sentence in ("No relationship discount", "Relationship discount required"):
            match = ("scenario.relationship_discount", sentence)
        elif sentence in ("No autopay discount", "Autopay discount required"):
            match = ("scenario.autopay_discount", sentence)
        elif sentence in ("Conventional conforming purchase", "Ten-year fixed term (120 months)"):
            continue
        else:
            match = next(
                (
                    (path, sentence[len(prefix) :])
                    for prefix, path in prefixes.items()
                    if sentence.startswith(prefix)
                ),
                None,
            )
        if match is None or match[0] in found:
            raise ValueError("ambiguous or unsupported fixture assumption")
        found[match[0]] = match[1]
    return found


def excerpt_is_located(node: str, excerpt: str) -> bool:
    if node == excerpt:
        return True
    try:
        return excerpt in _supporting_excerpts(node).values()
    except ValueError:
        return False


@dataclass(frozen=True)
class ExtractedRow:
    row_id: str
    candidates: tuple[ExtractedCandidate, ...]


@dataclass(frozen=True)
class NonOffer:
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


def extract_fixture(
    case_id: str, body: bytes, *, artifact_id: str, extraction_run_id: str, git_sha: str
) -> FixtureExtraction:
    if hashlib.sha256(body).hexdigest() != artifact_id:
        raise ValueError("fixture bytes do not match the stored artifact hash")
    root = _parse(body)
    heading = _only((n for n in root.walk() if n.tag == "h1"), "fixture heading")
    if heading.text() != _PAGE.get(case_id):
        raise ValueError("wrong source page for the requested fixture")
    banner = "Synthetic fixture for offline tests. Invented rates. Not a live credit-union page."
    _only((n for n in root.walk() if n.tag == "p" and n.text() == banner), "synthetic marker")

    def cite(
        row: str, path: str, selector: str, text: str | None = None, *, attribute: bool = False
    ) -> ExtractedCandidate:
        selector_type: Literal["xpath", "css"] = "xpath" if attribute else "css"
        node = resolve_text(body, selector, selector_type)
        if text is None:
            text = node
        if not excerpt_is_located(node, text):
            raise ValueError(f"{path} is not stated at {selector}")
        return ExtractedCandidate(
            schema_version=CONTRACT_VERSION,
            candidate_id=f"{row}.{path}",
            extraction_run_id=extraction_run_id,
            proposed_path=path,
            raw_value=text,
            provenance=Provenance(
                artifact_id=artifact_id,
                locator=HtmlLocator(
                    selector_type=selector_type,
                    selector=selector,
                    normalized_text_sha256=hashlib.sha256(node.encode()).hexdigest(),
                ),
                source_text=text,
                extraction=ExtractionProvenance(
                    git_sha=git_sha,
                    parser=SLICE_PARSER,
                    configuration_sha256=SLICE_CONFIGURATION,
                    normalization_version="t014.slice",
                    rule_id="t014.slice-extract",
                ),
            ),
        )

    def rate_row(row: _Node, *, mortgage: bool) -> ExtractedRow:
        row_id = row.attrs.get("id", "")
        if not re.fullmatch(_ID, row_id) or tuple(n.tag for n in row.children) != ("td",) * 6:
            raise ValueError("invalid fixture rate row")
        label, term, note, apr, points, assumptions = (n.text() for n in row.children)
        if label != (_PURCHASE if mortgage else _EQUITY):
            raise ValueError("fixture row does not identify the declared product")
        if "Ten-year fixed term (120 months)" in assumptions.split(". ") and months(term) != 120:
            raise ValueError("stated equity term disagrees with the rate row")
        if not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", rate) for rate in (note, apr)):
            raise ValueError("rate cell must contain exactly one percentage")
        fields = {
            "product_family": (1, label),
            "details.term_months": (2, term),
            "note_rate": (3, note),
            "apr": (4, apr),
            "points": (5, points),
        }
        if mortgage:
            fields.update({"details.purpose": (1, label), "details.rate_type": (1, label)})
        else:
            fields["details.lien_position"] = (1, label)
        statements = _supporting_excerpts(assumptions)
        for path, text in statements.items():
            if path in fields:
                if text != fields[path][1]:
                    raise ValueError(f"{path} disagrees with the rate row")
            else:
                fields[path] = (6, text)
        if statements.get("scenario.autopay_discount") == "Autopay discount required":
            fields["conditions.autopay.autopay"] = (6, "Autopay discount required")
        if statements.get("scenario.relationship_discount") == "Relationship discount required":
            fields["conditions.relationship.relationship"] = (6, "Relationship discount required")
        records = [
            cite(row_id, path, f"#{row_id} > td:nth-child({index})", text)
            for path, (index, text) in fields.items()
        ]
        if case_id == "athol-style-dated":
            records.append(cite(row_id, "effective_at", "#rates-effective"))
        return ExtractedRow(row_id, tuple(records))

    if case_id == "stoughton-style":
        findings = []
        labels = {
            "New vehicle loan": "vehicle",
            "Used vehicle loan": "vehicle",
            "Unsecured personal loan": "unsecured_personal",
        }
        for node in root.walk():
            if node.tag != "tr" or "id" not in node.attrs:
                continue
            row_id = node.attrs["id"]
            evidence = cite(row_id, "product_family", f"#{row_id} > td:nth-child(1)")
            label = evidence.raw_value
            if label not in labels:
                raise ValueError("unknown consumer-loan fixture row")
            findings.append(
                NonOffer(
                    f"finding-{row_id}",
                    label,
                    "out_of_scope",
                    None,
                    labels[label],
                    None,
                    evidence.provenance,
                )
            )
        if not findings:
            raise ValueError("no classified consumer rows")
        return FixtureExtraction((), tuple(findings))

    mortgage = case_id.startswith("athol-style")
    dated = tuple(n for n in root.walk() if n.attrs.get("id") == "rates-effective")
    if case_id == "athol-style-dated":
        if len(dated) != 1 or dated[0].tag != "time":
            raise ValueError("dated fixture requires an explicit effective time")
    elif dated:
        raise ValueError("unexpected effective-date marker on an undated fixture")
    table = _only((n for n in root.walk() if n.tag == "table"), "rate table")
    if tuple(n.text() for n in table.walk() if n.tag == "th") != _HEADERS:
        raise ValueError("unexpected rate table column order")
    tbody = _only((n for n in iter(table.children) if n.tag == "tbody"), "rate table body")
    if not tbody.children or any(n.tag != "tr" for n in tbody.children):
        raise ValueError("invalid rate table body")
    rows = tuple(rate_row(node, mortgage=mortgage) for node in tbody.children)
    if mortgage:
        return FixtureExtraction(rows, ())
    quote = cite("quote", "rate_availability", "h2:nth-of-type(1) + p")
    header = _only(
        (n for n in root.walk() if n.tag == "h2" and n.text() == "First mortgage"), "first mortgage"
    )
    siblings = [] if header.parent is None else header.parent.children
    adjacent = siblings[siblings.index(header) + 1 :] if siblings else []
    if (
        not adjacent
        or adjacent[0].tag != "p"
        or adjacent[0].text() != _QUOTE
        or quote.raw_value != _QUOTE
    ):
        raise ValueError("first-mortgage quote was not stated in its own section")
    link = cite("partner", "partner_link", f"//a[@href='{_PARTNER}']/@href", attribute=True)
    return FixtureExtraction(
        rows,
        (
            NonOffer(
                "finding-worcester-quote",
                "First mortgage is quote-only on this page",
                "in_scope",
                "mortgage",
                None,
                "quote_only",
                quote.provenance,
            ),
            NonOffer(
                "finding-worcester-partner",
                "Partner link is not an institution rate",
                "unknown",
                None,
                None,
                "unknown",
                link.provenance,
            ),
        ),
    )

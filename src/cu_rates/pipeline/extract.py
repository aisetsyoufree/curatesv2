"""Offline extractor for explicitly identified synthetic pages; never reads the test oracle."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
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
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_NESTING_DEPTH = 128
_NONVISIBLE = frozenset({"script", "style", "template", "noscript"})
# Footnote markers are removed from the text of tolerant (section-scoped) parses only,
# deterministically by tag: a superscript is never part of a label or a value.
_FOOTNOTE = frozenset({"sup"})
_SECTION_ID = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
MAX_SECTIONS = 16
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
    hidden: bool = False  # footnote candidate (sup): dropped from text only if it is a marker
    ancestors: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()  # section root only

    @property
    def children(self) -> list[_Node]:
        return [part for part in self.parts if isinstance(part, _Node)]

    def raw(self) -> str:
        items: list[str | _Node] = []  # visible strings and footnote candidates, in order
        pending: list[str | _Node] = [self]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                items.append(item)
            elif item.tag in _NONVISIBLE:
                continue
            elif item.hidden and item is not self:
                items.append(item)
            else:
                pending.extend(reversed(item.parts))
        if all(isinstance(item, str) for item in items):
            return "".join(str(item) for item in items)
        return _with_footnotes(items)

    def text(self) -> str:
        return " ".join(self.raw().split())

    def walk(self) -> Iterator[_Node]:
        pending = [self]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed(node.children))


_MARKER = re.compile(r"[0-9]{1,2}|[*\u2020\u2021\u00a7]")


def _with_footnotes(items: list[str | _Node]) -> str:
    """Strip a superscript only when it is unambiguously a trailing footnote marker.

    Its text must be a marker; the visible text before it must not end in a digit or '.',
    and nothing visible may follow it (across element boundaries) up to the selected node's
    end. A marker that fails any of this could be part of a number (6.12<sup>5</sup>), so the
    whole text is refused. A superscript that is not a marker is ordinary text and is kept.
    """
    out: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            out.append(item)
            continue
        inner = item.raw()
        if _MARKER.fullmatch(inner.strip()) is None:
            out.append(inner)
            continue
        before = "".join(out).rstrip()
        after = "".join(x if isinstance(x, str) else x.raw() for x in items[index + 1 :])
        if (before and (before[-1].isdigit() or before[-1] == ".")) or after.strip():
            raise ValueError("ambiguous footnote marker next to a value")
    return "".join(out)


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if len(values) != len(attrs):
            raise ValueError("duplicate HTML attributes")
        if self.depth + 1 > MAX_NESTING_DEPTH:
            raise ValueError("HTML nesting depth limit exceeded")
        node = _Node(tag, values, self.current)
        self.current.parts.append(node)
        if tag not in _VOID:
            self.current = node
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.current.tag != tag or self.current.parent is None:
            raise ValueError("fixture HTML is not explicitly balanced")
        self.current = self.current.parent
        self.depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            raise ValueError("self-closing non-void tag in fixture HTML")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.current.parts.append(data)


def _parse(body: bytes) -> _Node:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("HTML body size limit exceeded")
    parser = _Tree()
    parser.feed(body.decode("utf-8"))
    parser.close()
    if parser.current is not parser.root:
        raise ValueError("fixture HTML is incomplete")
    return parser.root


class _ScopedTree(HTMLParser):
    """Build only the subtree of the one element whose id is ``section_id``.

    Everything outside that element is tokenized and discarded, so stray or void end
    tags there (``</meta>``, an unclosed body) cannot matter. Inside it the strict
    rules are unchanged: any imbalance, duplicate attribute or depth overflow raises.
    The id must occur on exactly one element anywhere in the document.
    """

    def __init__(self, section_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.section_id = section_id
        self.matches = 0
        self.section: _Node | None = None
        self.current: _Node | None = None
        self.depth = 0
        self.closed = False
        self.open_tags: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.poisoned = False  # an end tag skipped over open tags that hid content

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], *, selfclosing: bool) -> None:
        is_target = any(key == "id" and value == self.section_id for key, value in attrs)
        self.matches += is_target
        if self.current is None and not (is_target and self.matches == 1):
            if not self.closed and tag not in _VOID and not selfclosing:
                self.open_tags.append((tag, _attr_pairs(attrs)))
            return
        if self.closed:
            return
        values = {key: value or "" for key, value in attrs}
        if len(values) != len(attrs):
            raise ValueError("duplicate HTML attributes")
        if selfclosing and tag not in _VOID:
            raise ValueError("self-closing non-void tag in section HTML")
        if self.depth + 1 > MAX_NESTING_DEPTH:
            raise ValueError("HTML nesting depth limit exceeded")
        node = _Node(tag, values, self.current, hidden=tag in _FOOTNOTE)
        if self.current is None:
            self.section = node
            node.ancestors = tuple(self.open_tags)
        else:
            self.current.parts.append(node)
        if tag in _VOID:
            self.closed |= self.current is None
        else:
            self.current = node
            self.depth += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, selfclosing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, selfclosing=True)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            # Outside the section: tolerated. Track only what encloses it, leniently.
            for index in range(len(self.open_tags) - 1, -1, -1):
                if self.open_tags[index][0] == tag:
                    skipped = self.open_tags[index:]
                    if len(skipped) > 1 and any(
                        t in _HIDING_TAGS or _attr_hidden(dict(a)) for t, a in skipped
                    ):
                        # Browsers may ignore such an end tag, so the hidden element could
                        # still enclose what follows: never un-hide on our own reading.
                        self.poisoned = True
                    del self.open_tags[index:]
                    break
            return
        if self.current.tag != tag:
            raise ValueError("section HTML is not explicitly balanced")
        self.depth -= 1
        if self.current is self.section:
            self.current, self.closed = None, True
        else:
            assert self.current.parent is not None
            self.current = self.current.parent

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.parts.append(data)


def _attr_pairs(attrs: list[tuple[str, str | None]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, value or "") for key, value in attrs))


_HIDING_TAGS = _NONVISIBLE | {"details", "dialog"}


def _attr_hidden(attrs: Mapping[str, str]) -> bool:
    style = re.sub(r"\s+", "", attrs.get("style", "").lower())
    return (
        "hidden" in attrs
        or attrs.get("aria-hidden", "").strip().lower() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _require_visible(node: _Node) -> None:
    """Refuse text that a browser would not show: hidden above or inside the selected node."""
    parent: _Node | None = node
    while parent is not None:
        if _attr_hidden(parent.attrs) or parent.tag in _HIDING_TAGS:
            raise ValueError("selected text lies in hidden content")
        parent = parent.parent
    for item in node.walk():
        if _attr_hidden(item.attrs) or item.tag in {"details", "dialog"}:
            raise ValueError("selected text contains hidden content")


def _parse_section(body: bytes, section_id: str) -> _Node:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("HTML body size limit exceeded")
    if not re.fullmatch(_SECTION_ID, section_id):
        raise ValueError("invalid section id")
    parser = _ScopedTree(section_id)
    parser.feed(body.decode("utf-8"))
    parser.close()
    if parser.matches != 1 or parser.section is None:
        raise ValueError(f"section {section_id} does not resolve uniquely")
    if not parser.closed:
        raise ValueError("section HTML is incomplete")
    if parser.poisoned:
        raise ValueError("section lies inside non-visible or hidden content")
    for tag, attrs in parser.section.ancestors:
        if tag in _HIDING_TAGS or _attr_hidden(dict(attrs)):
            raise ValueError("section lies inside non-visible or hidden content")
    return parser.section


class ScopedDocument:
    """A page read only through explicitly configured section ids (tolerant elsewhere)."""

    def __init__(self, body: bytes, section_ids: Sequence[str] = ()) -> None:
        if len(set(section_ids)) != len(section_ids) or len(section_ids) > MAX_SECTIONS:
            raise ValueError("invalid section list")
        self.body = body
        self._sections: dict[str, _Node] = {}
        for section_id in section_ids:  # eager: imbalance inside any section fails the page
            self.section(section_id)

    def section(self, section_id: str) -> _Node:
        if section_id not in self._sections:
            self._sections[section_id] = _parse_section(self.body, section_id)
        return self._sections[section_id]


_DOCUMENTS: OrderedDict[bytes, ScopedDocument] = OrderedDict()


_LOCK = threading.Lock()


def _cached_document(body: bytes) -> ScopedDocument:
    """One lazily parsed document per distinct body, so many claims do not re-tokenize it."""
    key = hashlib.sha256(body).digest()
    with _LOCK:
        document = _DOCUMENTS.get(key)
        if document is None:
            document = _DOCUMENTS[key] = ScopedDocument(body)
            while len(_DOCUMENTS) > 2:
                _DOCUMENTS.popitem(last=False)
        else:
            _DOCUMENTS.move_to_end(key)
    return document


_NTH = r"(?::nth-(?:child|of-type)\([1-9][0-9]*\))?"
_STEP = re.compile(
    r" (?P<child>> )?(?P<tag>[a-z][a-z0-9]*)(?::nth-(?P<kind>child|of-type)\((?P<n>[1-9][0-9]*)\))?"
)
_STRUCTURAL = re.compile(rf"#(?P<id>{_SECTION_ID})(?P<rest>(?: (?:> )?[a-z][a-z0-9]*{_NTH})*)")


def selector_section(selector: str) -> str | None:
    """The id that leads a structural selector, or None when the selector is another kind."""
    match = _STRUCTURAL.fullmatch(selector)
    return match["id"] if match else None


def _structural(start: _Node, rest: str, selector: str) -> _Node:
    """`#id`, then child/descendant steps with optional :nth-child/:nth-of-type(N).

    Every step keeps all matches and the final set must be exactly one element, so a
    selector that matches several tables or rows fails instead of picking one.
    """
    current: list[_Node] = [start]
    positions: dict[int, dict[int, tuple[int, int]]] = {}

    def position(item: _Node) -> tuple[int, int]:
        assert item.parent is not None
        table = positions.get(id(item.parent))
        if table is None:
            seen: dict[str, int] = {}
            table = {}
            for index, sibling in enumerate(item.parent.children, start=1):
                seen[sibling.tag] = seen.get(sibling.tag, 0) + 1
                table[id(sibling)] = (index, seen[sibling.tag])
            positions[id(item.parent)] = table
        return table[id(item)]

    at = 0
    while at < len(rest):
        step = _STEP.match(rest, at)
        if step is None:
            raise ValueError(f"unsupported selector {selector}")
        at = step.end()
        found: dict[int, _Node] = {}
        for node in current:
            pool: Iterable[_Node] = (
                node.children if step["child"] else (n for n in node.walk() if n is not node)
            )
            for item in pool:
                if item.tag != step["tag"]:
                    continue
                if step["n"] is not None:
                    index, typed = position(item)
                    if (typed if step["kind"] == "of-type" else index) != int(step["n"]):
                        continue
                found[id(item)] = item
        current = list(found.values())
    found_node = _only(iter(current), selector)
    _require_visible(found_node)
    return found_node


def _only(nodes: Iterator[_Node], description: str) -> _Node:
    first = next(nodes, None)
    if first is None or next(nodes, None) is not None:
        raise ValueError(f"{description} does not resolve uniquely")
    return first


def _by_id(root: _Node, element_id: str) -> _Node:
    return _only((node for node in root.walk() if node.attrs.get("id") == element_id), element_id)


def resolve_node(body: _Node | ScopedDocument, selector: str) -> _Node:
    """The one element a structural css selector addresses (same rules as resolve_text)."""
    structural = _STRUCTURAL.fullmatch(selector)
    if structural is None:
        raise ValueError(f"unsupported selector {selector}")
    start = (
        body.section(structural["id"])
        if isinstance(body, ScopedDocument)
        else _by_id(body, structural["id"])
    )
    return _structural(start, structural["rest"], selector)


def nearest_row(node: _Node) -> _Node | None:
    current: _Node | None = node
    while current is not None and current.tag != "tr":
        current = current.parent
    return current


def resolve_text(
    body: bytes | _Node | ScopedDocument,
    selector: str,
    selector_type: str = "css",
    *,
    tolerant: bool = False,
) -> str:
    """Resolve only the slice's explicit CSS subset and XPath attribute form.

    ``tolerant`` (bytes only) parses just the section a structural selector names.
    A ScopedDocument is always tolerant, one section per selector.
    """
    if selector_type == "css":
        structural = _STRUCTURAL.fullmatch(selector)
        if structural is not None and (tolerant or isinstance(body, ScopedDocument)):
            if isinstance(body, ScopedDocument):
                document = body
            elif isinstance(body, bytes):
                document = _cached_document(body)
            else:
                raise ValueError("tolerant resolution requires stored bytes")
            return _structural(
                document.section(structural["id"]), structural["rest"], selector
            ).text()
    if isinstance(body, ScopedDocument):
        raise ValueError(f"selector {selector} is outside the configured sections")
    root = _parse(body) if isinstance(body, bytes) else body
    if selector_type == "xpath":
        match = re.fullmatch(r"//a\[@href='([^']+)'\]/@href", selector)
        if match:
            href = match[1]
            return _only(
                (n for n in root.walk() if n.tag == "a" and n.attrs.get("href") == href), selector
            ).attrs["href"]
    elif selector_type == "css":
        structural = _STRUCTURAL.fullmatch(selector)
        if structural is not None:
            return _structural(_by_id(root, structural["id"]), structural["rest"], selector).text()
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


def _shape(root: _Node) -> list[object]:
    shapes: dict[_Node, list[object]] = {}
    for node in reversed(list(root.walk())):
        label = node.text() if node.tag in {"h1", "h2", "caption", "th", "title"} else None
        shapes[node] = [node.tag, node.attrs, label, [shapes[child] for child in node.children]]
    return shapes[root]


def layout_sha256(body: bytes | _Node) -> str:
    """Bind the approved tag/column/locator structure, excluding rate and date values."""
    root = _parse(body) if isinstance(body, bytes) else body
    payload = json.dumps(_shape(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def scoped_layout_sha256(document: ScopedDocument, section_ids: Sequence[str]) -> str:
    """Bind the structure of exactly the configured sections; the rest of the page is free."""
    payload = json.dumps(
        {
            section_id: {
                "ancestors": document.section(section_id).ancestors,
                "shape": _shape(document.section(section_id)),
            }
            for section_id in section_ids
        },
        sort_keys=True,
        separators=(",", ":"),
    )
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
        node = resolve_text(root, selector, selector_type)
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

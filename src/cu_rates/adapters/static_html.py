"""Strict static HTML extraction configured with data, never institution-specific code.

Selectors use the deliberately small, uniquely resolving CSS subset in pipeline.extract.
A configuration either reads the whole document (which must be explicitly balanced) or
declares ``sections``: only those id-addressed subtrees are parsed, anything outside them
may be imbalanced, and imbalance inside them still fails the capture. Unsupported HTML
requires a reviewed adapter revision, never parser recovery.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from cu_rates.adapters.normalization import with_statements
from cu_rates.contracts.base import (
    CONTRACT_VERSION,
    CharterNumber,
    FieldPath,
    HttpUrlText,
    Identifier,
    Sha256Hex,
)
from cu_rates.contracts.enums import CanonicalScenarioId, MatchState, ProductFamily
from cu_rates.contracts.evidence import (
    ExtractionProvenance,
    HtmlLocator,
    ParserVersion,
    Provenance,
    RawArtifact,
)
from cu_rates.contracts.scenarios import evaluate_scenario_match
from cu_rates.contracts.sources import ExtractedCandidate, RateSource
from cu_rates.normalize import FieldState, OfferContext, _family, normalize_offer
from cu_rates.normalize._values import DateFormat, date_pattern, prose_rule_id
from cu_rates.pipeline.extract import (
    _NONVISIBLE,
    _SECTION_ID,
    MAX_BODY_BYTES,
    MAX_NESTING_DEPTH,
    MAX_SECTIONS,
    ScopedDocument,
    _Node,
    _parse,
    layout_sha256,
    nearest_row,
    resolve_node,
    resolve_text,
    scoped_layout_sha256,
    selector_section,
)

PARSER = ParserVersion(name="static-html", version="1.2.0")
Confidence = Literal["high", "medium", "low"]


class ConfigurationData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Label(ConfigurationData):
    selector: str
    text: str


class DateRule(ConfigurationData):
    """A date written in prose, e.g. ``accurate as of <date>``, read under a strict format.

    ``template`` must contain exactly one ``{value}`` slot; all other text of the located
    node must match literally. The date has no clock time or zone in the source, so the
    reviewed ``timezone`` fixes the start of that day; nothing else is ever inferred.
    """

    template: str
    format: DateFormat
    timezone: str

    @model_validator(mode="after")
    def valid(self) -> DateRule:
        if len(self.template.split("{value}")) != 2:
            raise ValueError("a date template needs exactly one {value} position")
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError, OSError) as exc:
            raise ValueError("unknown timezone") from exc
        return self


class Field(ConfigurationData):
    path: FieldPath
    selector: str
    label: Label
    # A literal, verbatim fragment, for prose and stated assumptions. Never a replacement value.
    excerpt: str | None = None
    # Pin the complete normalized prose context so negation/qualifier changes cannot
    # leave a still-matching numerical fragment publishable.
    context_text: str | None = None
    # A single numeric value slot; surrounding text is escaped and matched exactly.
    context_template: str | None = None
    # Effective time written as prose; only valid on effective_at.
    date: DateRule | None = None
    # The value is stated by a group sub-header row that precedes the row, not by the row
    # itself. The page states it, but the row/group association is structural, so the
    # extraction can never be better than medium confidence.
    group_header: bool = False

    @model_validator(mode="after")
    def date_only_for_effective_time(self) -> Field:
        if self.date is not None and (
            self.path != "effective_at"
            or self.excerpt is not None
            or self.context_text is not None
            or self.context_template is not None
        ):
            raise ValueError("a date rule applies only to a plain effective_at field")
        return self


class Row(ConfigurationData):
    offer_id: Identifier
    product_id: Identifier
    comparison_target: CanonicalScenarioId | None = None
    # The source does not state all scenario assumptions (lock days, loan amount,
    # credit score...). A target names the dimensions to mark unstated, never source
    # evidence. Only reviewed unspecified-purpose rows omit it. No scenario.* statements.
    browse_only: bool = False
    fields: tuple[Field, ...]
    # Explicit reviewed declaration of absence, never a literal source value.
    allow_unspecified_purpose: bool = False

    @model_validator(mode="after")
    def unique_paths(self) -> Row:
        if len({f.path for f in self.fields}) != len(self.fields):
            raise ValueError("duplicate configured field paths")
        if self.browse_only and (
            (self.comparison_target is None and not self.allow_unspecified_purpose)
            or any(f.path.startswith("scenario.") for f in self.fields)
        ):
            raise ValueError(
                "a browse-only row declares a comparison target (unless purpose is explicitly "
                "unspecified) and no assumption statements"
            )
        if self.allow_unspecified_purpose and (
            self.comparison_target is not None
            or any(f.path == "details.purpose" for f in self.fields)
        ):
            raise ValueError("unspecified purpose requires no purpose field or comparison target")
        return self


class SourceConfiguration(ConfigurationData):
    source_id: Identifier
    source_url: HttpUrlText
    institution_charter: CharterNumber
    # Authored/reviewed structural pin, never learned automatically from a capture.
    layout_sha256: Sha256Hex
    # Empty: the whole document is parsed and must be balanced. Otherwise only these
    # id-addressed subtrees are parsed (tolerant outside them) and the layout pin covers
    # just them; every selector must start with one of these ids.
    sections: tuple[str, ...] = ()
    rows: tuple[Row, ...]

    @model_validator(mode="after")
    def unique_offers(self) -> SourceConfiguration:
        if not self.rows or len({r.offer_id for r in self.rows}) != len(self.rows):
            raise ValueError("configuration needs unique offers")
        if len(set(self.sections)) != len(self.sections) or len(self.sections) > MAX_SECTIONS:
            raise ValueError("sections must be unique and few")
        if any(not re.fullmatch(_SECTION_ID, section) for section in self.sections):
            raise ValueError("invalid section id")
        if self.sections:
            for row in self.rows:
                for rule in row.fields:
                    for selector in (rule.selector, rule.label.selector):
                        if selector_section(selector) not in self.sections:
                            raise ValueError("a selector lies outside the configured sections")
        return self

    def layout_of(self, body: bytes) -> str:
        """The structural pin of what this configuration reads (raises on bad HTML)."""
        if self.sections:
            return scoped_layout_sha256(ScopedDocument(body, self.sections), self.sections)
        return layout_sha256(body)

    @property
    def sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def configuration_parser(config: SourceConfiguration) -> ParserVersion:
    """The approval identity binds executable version and canonical configuration."""
    return ParserVersion(name=PARSER.name, version=f"{PARSER.version}+cfg.{config.sha256}")


@dataclass(frozen=True)
class RowExtraction:
    row: Row
    candidates: tuple[ExtractedCandidate, ...]
    confidence: Confidence
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class Extraction:
    rows: tuple[RowExtraction, ...]
    confidence: Confidence
    diagnostics: tuple[str, ...]


def fragment_is_located(node: str, excerpt: str) -> bool:
    """A unique literal fragment with token boundaries; 1% cannot cite 11% or 1.5%.

    A sentence-ending period may follow the fragment; a period joined to a digit may not.
    """
    return (
        bool(excerpt)
        and len(re.findall(r"(?<![\w.])" + re.escape(excerpt) + r"(?!\w|\.[0-9])", node)) == 1
    )


def required_paths(row: Row, candidates: tuple[ExtractedCandidate, ...]) -> set[str]:
    core = {"product_family", "note_rate", "apr", "details.term_months"}
    if not row.browse_only:
        # A browse-only row may omit what a comparison needs. Unknown points stay null
        # (the gate's points_unknown) and a missing effective time is the gate's own
        # effective_date_missing review path; neither is ever filled in here.
        core |= {"points", "effective_at"}
    family = next((c.raw_value for c in candidates if c.proposed_path == "product_family"), "")
    try:
        mortgage = _family(family) is ProductFamily.MORTGAGE
    except ValueError:
        mortgage = False  # missing/invalid family is independently a required-field failure
    if mortgage:
        core.add("details.rate_type")
        if not row.browse_only:
            core.add("details.lock_days")
        if not row.allow_unspecified_purpose:
            core.add("details.purpose")
    if row.comparison_target is not None:
        from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, REQUIRED_DIMENSIONS

        shared = {
            "term_months": "details.term_months",
            "rate_type": "details.rate_type",
            "lock_days": "details.lock_days",
            "points": "points",
        }
        if not row.browse_only:  # browse-only: unstated assumptions are the point, not an error
            core |= {
                shared.get(d.value, f"scenario.{d.value}")
                for d in REQUIRED_DIMENSIONS[row.comparison_target]
            }
        if CANONICAL_SCENARIOS[row.comparison_target].family is not ProductFamily.MORTGAGE:
            core.add("details.lien_position")
    return core


def _check_same_row(root: _Node | ScopedDocument, rule: Field) -> None:
    """A body-row label must sit in the very row of its value; header rows are exempt."""
    value_row = nearest_row(resolve_node(root, rule.selector))
    label_row = nearest_row(resolve_node(root, rule.label.selector))
    if (
        value_row is not None
        and label_row is not None
        and value_row is not label_row
        and not (label_row.parent is not None and label_row.parent.tag in {"thead", "tfoot"})
    ):
        raise ValueError("label and value lie in different table rows")


def offer_context(config: SourceConfiguration, row: Row, observed_at: datetime) -> OfferContext:
    """The one place a row's normalization context is built (extraction and re-extraction)."""
    rule = next((f.date for f in row.fields if f.path == "effective_at"), None)
    return OfferContext(
        offer_id=row.offer_id,
        product_id=row.product_id,
        source_id=config.source_id,
        institution_charter=config.institution_charter,
        observed_at=observed_at,
        comparison_target=row.comparison_target,
        allow_unspecified_purpose=row.allow_unspecified_purpose,
        effective_date_format=None if rule is None else rule.format,
        effective_timezone=None if rule is None else rule.timezone,
    )


class _PurposeText(HTMLParser):
    """Whole-page absence veto, never evidence or a replacement for scoped parsing.

    Tolerate outside-section imbalance without losing headings, navigation or footnotes.
    Inert subtrees match the normal text reader's exclusions; ambiguous/incomplete inert
    markup fails closed rather than hiding a possible purpose. No tree is built.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inert: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NONVISIBLE:
            if len(self.inert) >= MAX_NESTING_DEPTH:
                raise ValueError("HTML nesting depth limit exceeded")
            self.inert.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NONVISIBLE:
            raise ValueError("self-closing non-visible HTML")

    def handle_endtag(self, tag: str) -> None:
        if tag in _NONVISIBLE:
            if not self.inert or self.inert[-1] != tag:
                raise ValueError("ambiguous non-visible HTML")
            self.inert.pop()

    def handle_data(self, data: str) -> None:
        if not self.inert:
            self.parts.append(data)


def _page_mentions_purpose(body: bytes) -> bool:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("HTML body size limit exceeded")
    parser = _PurposeText()
    parser.feed(body.decode("utf-8"))
    parser.close()
    if parser.inert:
        raise ValueError("incomplete non-visible HTML")
    pattern = r"\b(?:purchas\w*|refi\w*|cash[\s_-]*out|home[\s_-]*buy\w*|buy\w*)\b"
    # Check both readings: inline splits must not conceal a word, and adjacent block
    # elements must not concatenate a purpose with unrelated text and conceal it.
    return any(
        re.search(pattern, text, re.IGNORECASE) is not None
        for text in ("".join(parser.parts), " ".join(parser.parts))
    )


def extract(
    config: SourceConfiguration,
    source: RateSource,
    body: bytes,
    artifact: RawArtifact,
    *,
    extraction_run_id: str,
    git_sha: str,
) -> Extraction:
    """Return low plus diagnostics on every layout, label, excerpt or required-field failure."""
    errors: list[str] = []
    if (source.source_id, source.url) != (config.source_id, config.source_url):
        errors.append("configuration source binding differs")
    if (
        artifact.source_url != source.url
        or artifact.http_status != 200
        or hashlib.sha256(body).hexdigest() != artifact.artifact_id
        or len(body) != artifact.byte_length
    ):
        errors.append("artifact bytes or source binding differs")
    root: _Node | ScopedDocument | None = None
    try:
        if config.sections:
            root = ScopedDocument(body, config.sections)
            layout = scoped_layout_sha256(root, config.sections)
        else:
            root = _parse(body)
            layout = layout_sha256(root)
        if layout != config.layout_sha256:
            errors.append("layout drift")
    except (ValueError, UnicodeError) as exc:
        errors.append(f"invalid HTML: {exc}")
    rows: list[RowExtraction] = []
    for row in config.rows:
        failures = list(errors)
        candidates: list[ExtractedCandidate] = []
        fragmented = False  # anything not the whole stated cell lowers confidence to medium
        # Conservative whole-page refusal, including headings, footnotes and link text.
        # A mention anywhere needs a stated-purpose configuration or human clarification.
        if row.allow_unspecified_purpose and root is not None:
            try:
                if _page_mentions_purpose(body):
                    failures.append("loan purpose is stated or ambiguous on the page")
            except (ValueError, UnicodeError) as exc:
                failures.append(f"cannot verify absent loan purpose: {exc}")
        for rule in row.fields if root is not None else ():
            assert root is not None
            try:
                if resolve_text(root, rule.label.selector) != rule.label.text:
                    raise ValueError("ambiguous or changed label")
                if not rule.group_header:
                    _check_same_row(root, rule)
                node = resolve_text(root, rule.selector)
                if rule.path in {"note_rate", "apr"} and (
                    rule.excerpt is not None or rule.context_text is not None
                ):
                    raise ValueError("rate excerpts require a context template")
                inferred = rule.group_header or rule.date is not None
                if rule.date is not None:
                    before, after = rule.date.template.split("{value}")
                    match = re.fullmatch(
                        re.escape(before) + date_pattern(rule.date.format) + re.escape(after),
                        node,
                    )
                    if match is None:
                        raise ValueError("date context template changed")
                    value = match[1]
                elif rule.context_template is not None:
                    if rule.excerpt is not None or rule.context_text is not None:
                        raise ValueError("template cannot include a literal excerpt/context")
                    parts = rule.context_template.split("{value}")
                    if len(parts) != 2:
                        raise ValueError("context template requires exactly one value position")
                    pattern = re.escape(parts[0]) + r"([0-9]+(?:\.[0-9]+)?%)" + re.escape(parts[1])
                    match = re.fullmatch(pattern, node)
                    if match is None:
                        raise ValueError("excerpt context template changed")
                    value = match[1]
                else:
                    if rule.excerpt is not None and rule.context_text != node:
                        raise ValueError("excerpt context changed or is not configured")
                    value = node if rule.excerpt is None else rule.excerpt
                if not fragment_is_located(node, value):
                    raise ValueError("excerpt does not uniquely match the located value")
                fragmented |= inferred or value != node
                candidates.append(
                    ExtractedCandidate(
                        schema_version=CONTRACT_VERSION,
                        candidate_id=f"{row.offer_id}.{rule.path}",
                        extraction_run_id=extraction_run_id,
                        proposed_path=rule.path,
                        raw_value=value,
                        provenance=Provenance(
                            artifact_id=artifact.artifact_id,
                            locator=HtmlLocator(
                                selector_type="css",
                                selector=rule.selector,
                                normalized_text_sha256=hashlib.sha256(node.encode()).hexdigest(),
                            ),
                            source_text=value,
                            extraction=ExtractionProvenance(
                                git_sha=git_sha,
                                parser=configuration_parser(config),
                                configuration_sha256=config.sha256,
                                normalization_version="t040.static-html.v1",
                                rule_id="t040.literal.v1"
                                if rule.date is None
                                else prose_rule_id(rule.date.timezone),
                            ),
                        ),
                    )
                )
            except (ValueError, UnicodeError) as exc:
                failures.append(f"{rule.path}: {exc}")
        required = required_paths(row, tuple(candidates))
        missing = required - {c.proposed_path for c in candidates}
        if not failures and not missing:
            try:
                normalized = normalize_offer(
                    candidates,
                    offer_context(config, row, artifact.fetched_at),
                )
                missing |= {
                    t.path
                    for t in normalized.traces
                    if t.path in required and (t.state is not FieldState.STATED or t.value is None)
                }
                if normalized.offer is None:
                    raise ValueError("required offer cannot be normalized")
                stated = with_statements(
                    normalized.offer, normalized.scenario_checks, artifact.fetched_at
                )
                if row.browse_only and row.comparison_target is not None:
                    if (
                        evaluate_scenario_match(row.comparison_target, stated.scenario_matches)
                        is MatchState.MATCHES
                    ):
                        failures.append("a browse-only row cannot match every assumption")
                    if any(
                        m.state is not MatchState.UNSTATED
                        and (m.stated is None or m.provenance is None)
                        for m in stated.scenario_matches
                    ):
                        failures.append("stated assumptions lack literal proof")
                elif row.comparison_target is not None and any(
                    m.stated is None or m.provenance is None for m in stated.scenario_matches
                ):
                    failures.append("required assumptions lack literal proof")
                if (
                    normalized.offer.effective_at is not None
                    and normalized.offer.effective_at > artifact.fetched_at
                ):
                    raise ValueError("effective date follows source observation")
            except ValueError as exc:
                failures.append(f"invalid stated values: {exc}")
        if missing:
            failures.append(f"required fields missing: {', '.join(sorted(missing))}")
        level: Confidence = "low" if failures else "medium" if fragmented else "high"
        rows.append(RowExtraction(row, tuple(candidates), level, tuple(failures)))
    level = (
        "low"
        if any(r.confidence == "low" for r in rows)
        else ("medium" if any(r.confidence == "medium" for r in rows) else "high")
    )
    return Extraction(
        tuple(rows), level, tuple(dict.fromkeys(e for r in rows for e in r.diagnostics))
    )

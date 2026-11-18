"""Strict static HTML extraction configured with data, never institution-specific code.

Selectors use the deliberately small, uniquely resolving CSS subset in pipeline.extract.
Unsupported/malformed HTML requires a reviewed adapter revision, never parser recovery.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

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
from cu_rates.contracts.enums import CanonicalScenarioId, ProductFamily
from cu_rates.contracts.evidence import (
    ExtractionProvenance,
    HtmlLocator,
    ParserVersion,
    Provenance,
    RawArtifact,
)
from cu_rates.contracts.sources import ExtractedCandidate, RateSource
from cu_rates.normalize import FieldState, OfferContext, normalize_offer
from cu_rates.pipeline.extract import layout_sha256, resolve_text

PARSER = ParserVersion(name="static-html", version="1.0.0")
Confidence = Literal["high", "medium", "low"]


class ConfigurationData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Label(ConfigurationData):
    selector: str
    text: str


class Field(ConfigurationData):
    path: FieldPath
    selector: str
    label: Label
    # A literal, verbatim fragment, for prose and stated assumptions. Never a replacement value.
    excerpt: str | None = None
    # Pin the complete normalized prose context so negation/qualifier changes cannot
    # leave a still-matching numerical fragment publishable.
    context_text: str | None = None


class Row(ConfigurationData):
    offer_id: Identifier
    product_id: Identifier
    comparison_target: CanonicalScenarioId | None = None
    fields: tuple[Field, ...]

    @model_validator(mode="after")
    def unique_paths(self) -> Row:
        if len({f.path for f in self.fields}) != len(self.fields):
            raise ValueError("duplicate configured field paths")
        return self


class SourceConfiguration(ConfigurationData):
    source_id: Identifier
    source_url: HttpUrlText
    institution_charter: CharterNumber
    # Authored/reviewed structural pin, never learned automatically from a capture.
    layout_sha256: Sha256Hex
    rows: tuple[Row, ...]

    @model_validator(mode="after")
    def unique_offers(self) -> SourceConfiguration:
        if not self.rows or len({r.offer_id for r in self.rows}) != len(self.rows):
            raise ValueError("configuration needs unique offers")
        return self

    @property
    def sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


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
    """A unique literal fragment with token boundaries; 1% cannot cite 11%."""
    return (
        bool(excerpt)
        and len(re.findall(r"(?<![\w.])" + re.escape(excerpt) + r"(?![\w.])", node)) == 1
    )


def required_paths(row: Row, candidates: tuple[ExtractedCandidate, ...]) -> set[str]:
    core = {"product_family", "note_rate", "apr", "points", "details.term_months", "effective_at"}
    raw = {c.proposed_path: c.raw_value.lower() for c in candidates}
    # A mortgage is also identified by its configured purpose; a missing family cannot
    # turn off the mortgage requirements.
    if "details.purpose" in {f.path for f in row.fields} or raw.get("product_family") in {
        "mortgage",
        "conventional conforming fixed purchase",
    }:
        core |= {"details.purpose", "details.rate_type", "details.lock_days"}
    if row.comparison_target is not None:
        from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, REQUIRED_DIMENSIONS

        shared = {
            "term_months": "details.term_months",
            "rate_type": "details.rate_type",
            "lock_days": "details.lock_days",
            "points": "points",
        }
        core |= {
            shared.get(d.value, f"scenario.{d.value}")
            for d in REQUIRED_DIMENSIONS[row.comparison_target]
        }
        if CANONICAL_SCENARIOS[row.comparison_target].family is not ProductFamily.MORTGAGE:
            core.add("details.lien_position")
    return core


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
    try:
        if layout_sha256(body) != config.layout_sha256:
            errors.append("layout drift")
    except (ValueError, UnicodeError) as exc:
        errors.append(f"invalid HTML: {exc}")
    rows: list[RowExtraction] = []
    for row in config.rows:
        failures = list(errors)
        candidates: list[ExtractedCandidate] = []
        fragmented = False
        for rule in row.fields:
            try:
                if resolve_text(body, rule.label.selector) != rule.label.text:
                    raise ValueError("ambiguous or changed label")
                node = resolve_text(body, rule.selector)
                if rule.excerpt is not None and rule.context_text != node:
                    raise ValueError("excerpt context changed or is not configured")
                value = node if rule.excerpt is None else rule.excerpt
                if not fragment_is_located(node, value):
                    raise ValueError("excerpt does not uniquely match the located value")
                fragmented |= value != node
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
                                parser=PARSER,
                                configuration_sha256=config.sha256,
                                normalization_version="t040.static-html.v1",
                                rule_id="t040.literal.v1",
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
                    OfferContext(
                        offer_id=row.offer_id,
                        product_id=row.product_id,
                        source_id=config.source_id,
                        institution_charter=config.institution_charter,
                        observed_at=artifact.fetched_at,
                        comparison_target=row.comparison_target,
                    ),
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
                if row.comparison_target is not None and any(
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

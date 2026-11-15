"""Raw artifacts, evidence locators, and value provenance."""

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    GitSha,
    HttpUrlText,
    Identifier,
    Measure,
    Percent,
    Points,
    Sha256Hex,
    SignedPercent,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
)

_JSON_POINTER_RE = re.compile(r"(/([^~/]|~[01])*)*")

Selector = Annotated[str, Field(min_length=1, max_length=2000)]


class BoundingBox(ContractModel):
    """Axis-aligned region (PDF points or image pixels) with positive extent."""

    x0: Measure
    y0: Measure
    x1: Measure
    y1: Measure

    @model_validator(mode="after")
    def _positive_extent(self) -> "BoundingBox":
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("bounding box must have x1 > x0 and y1 > y0")
        return self


class HtmlLocator(ContractModel):
    kind: Literal["html"] = "html"
    selector_type: Literal["xpath", "css"]
    selector: Selector
    normalized_text_sha256: Sha256Hex


class RenderedDomLocator(ContractModel):
    kind: Literal["rendered_dom"] = "rendered_dom"
    selector_type: Literal["xpath", "css"]
    selector: Selector
    frame_path: tuple[Selector, ...] = ()
    rendered_text_sha256: Sha256Hex
    response_ids: Annotated[tuple[Sha256Hex, ...], SortedUnique] = ()  # artifact ids


class PdfLocator(ContractModel):
    kind: Literal["pdf"] = "pdf"
    page: Annotated[int, Field(ge=1)]
    bbox: BoundingBox
    extracted_text_sha256: Sha256Hex


class ImageOcrLocator(ContractModel):
    kind: Literal["image_ocr"] = "image_ocr"
    region: BoundingBox
    image_sha256: Sha256Hex
    recognized_text_sha256: Sha256Hex


class JsonLocator(ContractModel):
    kind: Literal["json"] = "json"
    pointer: str
    response_sha256: Sha256Hex

    @model_validator(mode="after")
    def _rfc6901(self) -> "JsonLocator":
        if _JSON_POINTER_RE.fullmatch(self.pointer) is None:
            raise ValueError("pointer must be an RFC 6901 JSON Pointer")
        return self


EvidenceLocator = Annotated[
    HtmlLocator | RenderedDomLocator | PdfLocator | ImageOcrLocator | JsonLocator,
    Field(discriminator="kind"),
]


class RawArtifact(VersionedModel):
    """Metadata for one immutable, content-addressed raw response."""

    artifact_id: Sha256Hex
    media_type: Text
    source_url: HttpUrlText
    fetched_at: UtcDatetime
    http_status: Annotated[int, Field(ge=100, le=599)]
    byte_length: Annotated[int, Field(ge=0)]
    fetch_run_id: Identifier


class ParserVersion(ContractModel):
    name: Identifier
    version: Text


class ExtractionProvenance(ContractModel):
    git_sha: GitSha
    parser: ParserVersion
    configuration_sha256: Sha256Hex
    normalization_version: Text
    rule_id: Identifier | None = None


class Provenance(ContractModel):
    """Evidence for one normalized value: artifact, locator, original text, extraction.

    Artifact/locator consistency (typed limitation): the only locator hash that is
    guaranteed to be the artifact's own content address is the JSON ``response_sha256``,
    so that one is enforced here. HTML/DOM/PDF/OCR locator hashes cover *derived* text or
    image regions, which cannot be checked without the artifact bytes; they are only
    format-checked. Media-type compatibility is checked at publication time against the
    stored ``RawArtifact`` (see ``locator_fits_media_type``).
    """

    artifact_id: Sha256Hex
    locator: EvidenceLocator
    source_text: Text
    extraction: ExtractionProvenance

    @model_validator(mode="after")
    def _json_locator_names_its_artifact(self) -> "Provenance":
        locator = self.locator
        if isinstance(locator, JsonLocator) and locator.response_sha256 != self.artifact_id:
            raise ValueError("a JSON locator's response hash must equal its artifact id")
        return self

    @property
    def is_ocr(self) -> bool:
        return self.locator.kind == "image_ocr"


class SourcedRate(ContractModel):
    """A percentage value together with the evidence it came from."""

    value: Percent
    provenance: Provenance


class SourcedMargin(ContractModel):
    """A signed spread over an index with its evidence (Prime minus 0.25 is -0.25)."""

    value: SignedPercent
    provenance: Provenance


class SourcedPoints(ContractModel):
    """Discount points with evidence; zero is an explicit source statement, null is unknown."""

    value: Points
    provenance: Provenance


class FieldEvidence(ContractModel):
    """Maps one semantic field path (e.g. ``details.term_months``) to its evidence.

    Compact by design: the raw artifact is referenced by id inside ``provenance`` and is
    never embedded, so many fields can cite one stored response.
    """

    path: FieldPath
    provenance: Provenance


_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def locator_fits_media_type(locator: EvidenceLocator, media_type: str) -> bool:
    """Whether a locator kind can address content of this media type."""
    base = media_type.split(";", 1)[0].strip().lower()
    match locator.kind:
        case "html" | "rendered_dom":
            return base in _HTML_TYPES
        case "pdf":
            return base == "application/pdf"
        case "json":
            return base == "application/json" or base.endswith("+json")
        case "image_ocr":
            return base.startswith("image/") or base == "application/pdf"

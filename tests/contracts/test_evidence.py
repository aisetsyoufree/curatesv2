from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cu_rates.contracts import (
    BoundingBox,
    EvidenceLocator,
    HtmlLocator,
    ImageOcrLocator,
    JsonLocator,
    PdfLocator,
    Provenance,
    RawArtifact,
    RenderedDomLocator,
    locator_fits_media_type,
)

from .factories import ARTIFACT_ID, GIT_SHA, T0, TEXT_HASH, extraction, provenance


class _Holder(BaseModel):
    locator: EvidenceLocator


class _Locator:
    """Thin parse helper going through a real field so the discriminator is exercised."""

    @staticmethod
    def validate_python(payload: dict[str, Any]) -> Any:
        return _Holder.model_validate({"locator": payload}).locator

    @staticmethod
    def round_trip(payload: dict[str, Any]) -> tuple[Any, Any]:
        holder = _Holder.model_validate({"locator": payload})
        return holder.locator, _Holder.model_validate_json(holder.model_dump_json()).locator


LOCATOR = _Locator()
BOX: dict[str, Any] = {"x0": "10", "y0": "20", "x1": "110.5", "y1": "40"}

LOCATOR_PAYLOADS: dict[str, dict[str, Any]] = {
    "html": {
        "kind": "html",
        "selector_type": "xpath",
        "selector": "//table[1]//td[2]",
        "normalized_text_sha256": TEXT_HASH,
    },
    "rendered_dom": {
        "kind": "rendered_dom",
        "selector_type": "css",
        "selector": "div.rate",
        "frame_path": ["iframe#calc"],
        "rendered_text_sha256": TEXT_HASH,
        "response_ids": ["a" * 64, "b" * 64],
    },
    "pdf": {"kind": "pdf", "page": 2, "bbox": BOX, "extracted_text_sha256": TEXT_HASH},
    "image_ocr": {
        "kind": "image_ocr",
        "region": BOX,
        "image_sha256": ARTIFACT_ID,
        "recognized_text_sha256": TEXT_HASH,
    },
    "json": {"kind": "json", "pointer": "/rates/0/apr", "response_sha256": TEXT_HASH},
}
EXPECTED_TYPES = {
    "html": HtmlLocator,
    "rendered_dom": RenderedDomLocator,
    "pdf": PdfLocator,
    "image_ocr": ImageOcrLocator,
    "json": JsonLocator,
}


@pytest.mark.parametrize("kind", sorted(LOCATOR_PAYLOADS))
def test_each_evidence_kind_parses_to_its_own_model(kind: str) -> None:
    locator, round_tripped = LOCATOR.round_trip(LOCATOR_PAYLOADS[kind])
    assert type(locator) is EXPECTED_TYPES[kind]
    assert round_tripped == locator


def test_unknown_locator_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        LOCATOR.validate_python({**LOCATOR_PAYLOADS["html"], "kind": "screenshot"})


@pytest.mark.parametrize("pointer", ["", "/", "/a/b", "/a~0b/c~1d", "/0"])
def test_valid_json_pointers(pointer: str) -> None:
    assert JsonLocator(pointer=pointer, response_sha256=TEXT_HASH).pointer == pointer


@pytest.mark.parametrize("pointer", ["a/b", "/a~2", "/a~", "$.rates[0]"])
def test_invalid_json_pointers(pointer: str) -> None:
    with pytest.raises(ValidationError, match="RFC 6901"):
        JsonLocator(pointer=pointer, response_sha256=TEXT_HASH)


@pytest.mark.parametrize("digest", ["", "abc", "G" * 64, "A" * 64, "a" * 63, "a" * 65])
def test_hashes_must_be_lowercase_sha256_hex(digest: str) -> None:
    with pytest.raises(ValidationError):
        HtmlLocator(selector_type="css", selector="p", normalized_text_sha256=digest)


def test_pdf_page_is_one_based() -> None:
    with pytest.raises(ValidationError):
        PdfLocator.model_validate({**LOCATOR_PAYLOADS["pdf"], "page": 0})


@pytest.mark.parametrize(
    "box",
    [
        {"x0": "10", "y0": "20", "x1": "10", "y1": "40"},
        {"x0": "10", "y0": "20", "x1": "11", "y1": "20"},
        {"x0": "10", "y0": "20", "x1": "9", "y1": "40"},
        {"x0": "-1", "y0": "20", "x1": "9", "y1": "40"},
    ],
)
def test_bounding_box_needs_positive_extent(box: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        BoundingBox.model_validate(box)


def test_ocr_provenance_is_flagged() -> None:
    ocr = Provenance(
        artifact_id=ARTIFACT_ID,
        locator=LOCATOR.validate_python(LOCATOR_PAYLOADS["image_ocr"]),
        source_text="6.125%",
        extraction=extraction(),
    )
    assert ocr.is_ocr
    html = Provenance(
        artifact_id=ARTIFACT_ID,
        locator=LOCATOR.validate_python(LOCATOR_PAYLOADS["html"]),
        source_text="6.125%",
        extraction=extraction(),
    )
    assert not html.is_ocr


def test_derived_values_cite_rule_ids() -> None:
    assert extraction(rule_id="heloc.post_intro.v1").rule_id == "heloc.post_intro.v1"
    assert extraction().rule_id is None
    assert extraction().git_sha == GIT_SHA


def test_provenance_requires_source_text_and_extraction() -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate(
            {
                "artifact_id": ARTIFACT_ID,
                "locator": LOCATOR_PAYLOADS["html"],
                "source_text": "  ",
                "extraction": extraction().model_dump(),
            }
        )


@pytest.mark.parametrize("sha", ["abc", "g" * 40, "A" * 40])
def test_git_sha_must_be_40_lowercase_hex(sha: str) -> None:
    payload = extraction().model_dump()
    payload["git_sha"] = sha
    with pytest.raises(ValidationError):
        type(extraction()).model_validate(payload)


def _artifact(**overrides: Any) -> RawArtifact:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_id": ARTIFACT_ID,
        "media_type": "text/html",
        "source_url": "https://example.invalid/rates",
        "fetched_at": T0,
        "http_status": 200,
        "byte_length": 1024,
        "fetch_run_id": "run-1",
    }
    payload.update(overrides)
    return RawArtifact.model_validate(payload)


def test_raw_artifact_valid_and_url_checked() -> None:
    assert _artifact().http_status == 200
    for bad in ("ftp://example.invalid/x", "example.invalid", "https://", "https://a b"):
        with pytest.raises(ValidationError):
            _artifact(source_url=bad)


@pytest.mark.parametrize(
    ("field", "value"), [("http_status", 99), ("http_status", 600), ("byte_length", -1)]
)
def test_raw_artifact_numeric_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _artifact(**{field: value})


def test_json_locator_must_hash_its_own_artifact() -> None:
    def json_provenance(response_sha256: str) -> Provenance:
        return Provenance.model_validate(
            {
                "artifact_id": ARTIFACT_ID,
                "locator": {
                    "kind": "json",
                    "pointer": "/rates/0",
                    "response_sha256": response_sha256,
                },
                "source_text": "6.125",
                "extraction": extraction().model_dump(),
            }
        )

    assert json_provenance(ARTIFACT_ID).locator.kind == "json"
    with pytest.raises(ValidationError, match="response hash must equal"):
        json_provenance(TEXT_HASH)


def test_derived_text_hashes_are_a_documented_unchecked_limitation() -> None:
    """HTML/DOM/PDF/OCR locator hashes cover derived text, so no artifact-equality check
    exists for them (see ``Provenance``); only their shape is validated."""
    assert provenance().locator.kind == "html"
    locator = provenance().locator
    assert isinstance(locator, HtmlLocator)
    assert locator.normalized_text_sha256 != provenance().artifact_id


@pytest.mark.parametrize(
    ("kind", "media", "fits"),
    [
        ("html", "text/html; charset=utf-8", True),
        ("html", "application/pdf", False),
        ("rendered_dom", "application/xhtml+xml", True),
        ("rendered_dom", "application/json", False),
        ("pdf", "application/pdf", True),
        ("pdf", "text/html", False),
        ("json", "application/json", True),
        ("json", "application/vnd.api+json", True),
        ("json", "text/html", False),
        ("image_ocr", "image/png", True),
        ("image_ocr", "application/pdf", True),
        ("image_ocr", "text/html", False),
    ],
)
def test_locator_media_type_compatibility(kind: str, media: str, fits: bool) -> None:
    locator = LOCATOR.validate_python(LOCATOR_PAYLOADS[kind])
    assert locator_fits_media_type(locator, media) is fits

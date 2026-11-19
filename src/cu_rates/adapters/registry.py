"""One approved-adapter dispatch and trusted re-extraction boundary.

Configuration files are reviewed deployment data shipped with the application. Their
canonical hashes are bound into every provenance record. There are deliberately no
real-source approvals or rate fixtures shipped here. Unknown sources/versions fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from cu_rates.adapters.normalization import with_statements
from cu_rates.adapters.static_html import (
    PARSER,
    Confidence,
    RowExtraction,
    SourceConfiguration,
    configuration_parser,
    extract,
    offer_context,
)
from cu_rates.contracts.enums import OfferStatus
from cu_rates.contracts.evidence import ParserVersion, RawArtifact
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.sources import ExtractedCandidate, RateSource, SourceApproval
from cu_rates.normalize import normalize_offer
from cu_rates.pipeline.extract import SLICE_PARSER, layout_sha256

CONFIGURATIONS = Path(__file__).with_name("configurations")


@dataclass(frozen=True)
class Adapter:
    parser: ParserVersion
    fixture: bool = False


ADAPTERS = MappingProxyType(
    {
        (PARSER.name, PARSER.version): Adapter(PARSER),
        (SLICE_PARSER.name, SLICE_PARSER.version): Adapter(SLICE_PARSER, fixture=True),
    }
)


def approved_adapter(
    source: RateSource,
    approval: SourceApproval | None,
    *,
    allow_fixture: bool = False,
) -> Adapter:
    if (
        approval is None
        or not approval.approved
        or approval.adapter is None
        or approval.source_id != source.source_id
        or approval.source_url != source.url
    ):
        raise ValueError("source has no applicable approved adapter")
    name, version = approval.adapter.name, approval.adapter.version
    adapter = ADAPTERS.get((name, version))
    if adapter is None and name == PARSER.name:
        adapter = ADAPTERS.get((name, version.split("+cfg.", 1)[0]))
    if adapter is None:
        raise ValueError("unknown adapter name/version")
    if adapter.fixture and not allow_fixture:
        raise ValueError("fixture adapter refused in production mode")
    if not adapter.fixture:
        identity = configuration_parser(load_configuration(source))
        if approval.adapter != identity:
            raise ValueError("adapter configuration digest is not approved")
        return Adapter(identity)
    return adapter


def load_configuration(source: RateSource) -> SourceConfiguration:
    # Do not interpolate an untrusted source ID into a filesystem path.
    matches = [p for p in CONFIGURATIONS.glob("*.json") if p.stem == source.source_id]
    if len(matches) != 1:
        raise ValueError("source has no unique static HTML configuration")
    config = SourceConfiguration.model_validate_json(matches[0].read_bytes())
    if (config.source_id, config.source_url) != (source.source_id, source.url):
        raise ValueError("configuration source binding differs")
    return config


@lru_cache(maxsize=64)
def _sections_for(path: str, mtime_ns: int, size: int, adapter_version: str) -> tuple[str, ...]:
    config = SourceConfiguration.model_validate_json(Path(path).read_bytes())
    identity = configuration_parser(config)
    return config.sections if identity.version == adapter_version else ()


def approved_sections(approval: SourceApproval | None) -> tuple[str, ...]:
    """Sections of the configuration the approval identity binds (empty: whole-document mode).

    Cached per (file, its mtime and size, approved adapter version) so verifying many
    claims does not re-read and re-validate the configuration JSON.
    """
    if approval is None or approval.adapter is None or approval.adapter.name != PARSER.name:
        return ()
    matches = [p for p in CONFIGURATIONS.glob("*.json") if p.stem == approval.source_id]
    if len(matches) != 1:
        return ()
    info = matches[0].stat()
    return _sections_for(str(matches[0]), info.st_mtime_ns, info.st_size, approval.adapter.version)


def body_layout_sha256(
    source: RateSource,
    approval: SourceApproval | None,
    body: bytes,
    *,
    allow_fixture: bool = False,
) -> str:
    """The structural pin of stored bytes as the approved adapter reads them."""
    adapter = approved_adapter(source, approval, allow_fixture=allow_fixture)
    if adapter.fixture:
        return layout_sha256(body)
    return load_configuration(source).layout_of(body)


def normalize_row(
    row: RowExtraction, config: SourceConfiguration, artifact: RawArtifact
) -> NormalizedOffer:
    result = normalize_offer(row.candidates, offer_context(config, row.row, artifact.fetched_at))
    if result.offer is None:
        raise ValueError(f"normalization yielded {result.no_offer_reason}")
    offer = with_statements(result.offer, result.scenario_checks, artifact.fetched_at)
    if row.confidence == "low":
        offer = offer.model_copy(update={"status": OfferStatus.QUARANTINED})
    return offer


@dataclass(frozen=True)
class Verification:
    candidates: tuple[ExtractedCandidate, ...]
    comparable: bool
    confidence: Confidence


def verify_offer_against_stored_bytes(
    source: RateSource,
    approval: SourceApproval | None,
    offer: NormalizedOffer,
    body: bytes,
    artifact: RawArtifact,
    *,
    allow_fixture: bool = False,
) -> Verification:
    """Re-extract using the DB approval, never the offered claim's parser identity.

    Caller must load approval from the operational DB and read artifact from ArtifactStore.
    Low confidence is an extraction result, not an exception; the trusted loader quarantines it.
    """
    adapter = approved_adapter(source, approval, allow_fixture=allow_fixture)
    if sha256(body).hexdigest() != artifact.artifact_id or len(body) != artifact.byte_length:
        raise ValueError("stored bytes differ from artifact")
    if approval is None:  # narrowed by approved_adapter
        raise ValueError("missing source approval")
    if (
        artifact.source_url != source.url
        or artifact.fetched_at != offer.observed_at
        or artifact.http_status != 200
        or (offer.effective_at is not None and offer.effective_at > artifact.fetched_at)
    ):
        raise ValueError("offer is not bound to this direct observation")
    if adapter.fixture:
        from cu_rates.pipeline.slice import verified_offer_candidates

        candidates, comparable = verified_offer_candidates(source, offer, body, artifact)
        return Verification(candidates, comparable, "high")
    config = load_configuration(source)
    if (
        approval.approved_layout_sha256 != config.layout_sha256
        or approval.adapter != configuration_parser(config)
    ):
        raise ValueError("configuration layout is not approved")
    # Use the original extraction identity, then compare the entire offer including all
    # locators, excerpts, parser/config/code provenance. Status/expiry are lifecycle policy;
    # schema_version is an envelope version (1.0 remains readable under the 1.1 normalizer).
    origins = {p.extraction.git_sha for p in offer.iter_provenance()}
    if len(origins) != 1:
        raise ValueError("mixed extraction code identities")
    result = extract(
        config,
        source,
        body,
        artifact,
        extraction_run_id=f"extract-{artifact.fetch_run_id}",
        git_sha=origins.pop(),
    )
    for row in result.rows:
        if row.row.offer_id != offer.offer_id:
            continue
        if result.confidence == "low":
            return Verification(row.candidates, False, "low")
        expected = normalize_row(row, config, artifact)
        excluded = {"status", "published_at", "valid_until", "schema_version"}
        if offer.model_dump(exclude=excluded) != expected.model_dump(exclude=excluded):
            raise ValueError("offer fields do not match the stored artifact row")
        return Verification(row.candidates, True, row.confidence)
    raise ValueError("offer is not configured for the approved source")

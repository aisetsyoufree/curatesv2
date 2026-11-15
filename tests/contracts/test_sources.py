from datetime import timedelta

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    EvidenceKind,
    ExtractedCandidate,
    ExtractionRun,
    FetchMethod,
    FetchProfile,
    FetchRun,
    RateSource,
    RunStatus,
    SourceFingerprint,
)

from .factories import ARTIFACT_ID, CONFIG_HASH, GIT_SHA, PARSER, SV, T0, TEXT_HASH, provenance


def _fetch_run(**overrides: object) -> FetchRun:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": "fetch-001",
        "source_id": "src-rates-page",
        "profile_id": "profile-1",
        "status": RunStatus.SUCCEEDED,
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=3),
        "git_sha": GIT_SHA,
        "artifact_ids": (ARTIFACT_ID,),
    }
    payload.update(overrides)
    return FetchRun.model_validate(payload)


def _extraction_run(**overrides: object) -> ExtractionRun:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": "extract-001",
        "fetch_run_id": "fetch-001",
        "parser": PARSER,
        "git_sha": GIT_SHA,
        "configuration_sha256": CONFIG_HASH,
        "status": RunStatus.SUCCEEDED,
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=1),
        "candidate_count": 3,
    }
    payload.update(overrides)
    return ExtractionRun.model_validate(payload)


def test_rate_source_url_is_http_and_unapproved_by_itself() -> None:
    source = RateSource(
        schema_version=SV,
        source_id="src-rates-page",
        url="https://example.invalid/rates",
        evidence_kind=EvidenceKind.HTML,
    )
    assert source.url.startswith("https://")
    for bad in ("ftp://example.invalid", "example.invalid", "https://a b"):
        with pytest.raises(ValidationError):
            RateSource(schema_version=SV, source_id="s", url=bad, evidence_kind=EvidenceKind.HTML)


def test_fetch_profile_is_polite_by_construction() -> None:
    profile = FetchProfile(
        schema_version=SV,
        profile_id="profile-1",
        source_id="src-rates-page",
        method=FetchMethod.HTTP_GET,
        min_interval_seconds=86400,
        timeout_seconds=30,
    )
    assert profile.respect_robots is True
    base = profile.model_dump()
    with pytest.raises(ValidationError):
        FetchProfile.model_validate(base | {"respect_robots": False})
    with pytest.raises(ValidationError):
        FetchProfile.model_validate(base | {"min_interval_seconds": 0})
    with pytest.raises(ValidationError):
        FetchProfile.model_validate(base | {"timeout_seconds": 301})


def test_fetch_run_rules() -> None:
    assert _fetch_run().status is RunStatus.SUCCEEDED
    with pytest.raises(ValidationError, match="at least one artifact"):
        _fetch_run(artifact_ids=())
    with pytest.raises(ValidationError, match="needs an error"):
        _fetch_run(error="timeout")
    failed = _fetch_run(status=RunStatus.FAILED, artifact_ids=(), error="timeout")
    assert failed.error == "timeout"
    with pytest.raises(ValidationError, match="needs an error"):
        _fetch_run(status=RunStatus.BLOCKED, artifact_ids=())
    with pytest.raises(ValidationError, match="cannot precede"):
        _fetch_run(finished_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _fetch_run(started_at=T0.replace(tzinfo=None))


def test_source_fingerprint_record() -> None:
    record = SourceFingerprint(
        schema_version=SV,
        source_id="src-rates-page",
        fetch_run_id="fetch-001",
        artifact_id=ARTIFACT_ID,
        observed_at=T0,
        content_sha256=TEXT_HASH,
        structure_sha256=CONFIG_HASH,
    )
    assert record.structure_sha256 != record.content_sha256
    with pytest.raises(ValidationError):
        SourceFingerprint.model_validate(record.model_dump() | {"content_sha256": "xyz"})


def test_extraction_run_rules() -> None:
    assert _extraction_run().candidate_count == 3
    with pytest.raises(ValidationError, match="cannot report candidates"):
        _extraction_run(status=RunStatus.FAILED, error="parser crashed")
    assert _extraction_run(status=RunStatus.FAILED, error="parser crashed", candidate_count=0)
    with pytest.raises(ValidationError, match="cannot precede"):
        _extraction_run(finished_at=T0 - timedelta(hours=1))


def test_extracted_candidate_is_evidence_addressed_and_proposes_a_path() -> None:
    candidate = ExtractedCandidate(
        schema_version=SV,
        candidate_id="cand-1",
        extraction_run_id="extract-001",
        proposed_path="note_rate",
        raw_value="6.125%",
        provenance=provenance(),
    )
    assert candidate.provenance.artifact_id == ARTIFACT_ID
    assert ExtractedCandidate.model_validate_json(candidate.model_dump_json()) == candidate
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExtractedCandidate.model_validate(candidate.model_dump() | {"approved": True})
    with pytest.raises(ValidationError):
        ExtractedCandidate.model_validate(candidate.model_dump() | {"proposed_path": "Not A Path"})

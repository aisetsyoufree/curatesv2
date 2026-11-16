"""Collection-pipeline records: sources, fetch profiles, runs, fingerprints, candidates.

Domain contracts only; no I/O, storage, or worker logic. Extraction output is a
*candidate*: it is evidence-addressed but carries no authority until deterministic
normalization, validation, and (where required) human review have run.
"""

import hashlib
import json
import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    GitSha,
    HttpUrlText,
    Identifier,
    Sha256Hex,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import EvidenceKind, FetchMethod, ReviewVerdict, RunStatus
from cu_rates.contracts.evidence import ParserVersion, Provenance


class RateSource(VersionedModel):
    """A URL that may publish rates. Untrusted until a relationship approves it."""

    source_id: Identifier
    url: HttpUrlText
    evidence_kind: EvidenceKind
    description: Text | None = None


class SourceApproval(VersionedModel):
    """Immutable human decision that one source URL may feed publication.

    Bound to the exact ``source_id`` and ``source_url`` it reviewed, so a later URL change
    on the ``RateSource`` is not covered. ``policy_id``/``policy_version`` name the review
    policy applied.

    ``approved_profile_sha256`` is the digest of the fetch profile the human approved.
    T014 must copy it from that approval record. The fetch worker does not get to supply
    it, and publication fails closed when it is absent. ``approved_layout_sha256`` and
    ``adapter`` are the page layout and parser reviewed with the source. T040/T045 compute
    the layout digest; T014 stores it here. ``approved_origins`` lists extra origins a
    rendered page may call. The source URL's own origin is approved without being listed.
    """

    approval_id: Identifier
    source_id: Identifier
    source_url: HttpUrlText
    decision: ReviewVerdict
    actor: Text
    decided_at: UtcDatetime
    policy_id: Identifier
    policy_version: Text
    rationale: Text
    evidence: tuple[Provenance, ...] = ()
    approved_profile_sha256: Sha256Hex | None = None
    approved_layout_sha256: Sha256Hex | None = None
    adapter: ParserVersion | None = None
    approved_origins: tuple[HttpUrlText, ...] = ()

    @property
    def approved(self) -> bool:
        return self.decision is ReviewVerdict.APPROVED


class FetchProfile(VersionedModel):
    """How a source is politely collected; robots are always honored."""

    profile_id: Identifier
    source_id: Identifier
    method: FetchMethod
    min_interval_seconds: Annotated[int, Field(ge=1, le=7 * 24 * 3600)]
    timeout_seconds: Annotated[int, Field(ge=1, le=300)]
    respect_robots: Literal[True] = True


class ConditionalValidator(ContractModel):
    """The HTTP validator a 200 response carried and a later conditional request echoed.

    ``etag`` maps to ``If-None-Match`` and ``last_modified`` to ``If-Modified-Since``.
    """

    kind: Literal["etag", "last_modified"]
    value: Text


# RFC 9110 entity-tag: one quoted opaque value; no W/ prefix, wildcard, or list.
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')


def is_strong_etag(value: str) -> bool:
    """Whether ``value`` is exactly one strong entity-tag (not weak, ``*``, or a list)."""
    return _STRONG_ETAG.fullmatch(value) is not None


def profile_sha256(profile: "FetchProfile") -> str:
    """Digest of the exact request profile, so a changed profile never shares an identity."""
    canonical = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _check_run(
    status: RunStatus, started: UtcDatetime, finished: UtcDatetime, error: str | None
) -> None:
    if finished < started:
        raise ValueError("finished_at cannot precede started_at")
    if (status is RunStatus.SUCCEEDED) != (error is None):
        raise ValueError("a failed or blocked run needs an error; a successful one has none")


class FetchRun(VersionedModel):
    """One completed attempt to collect a source; its artifacts are content-addressed."""

    run_id: Identifier
    source_id: Identifier
    profile_id: Identifier
    profile_sha256: Sha256Hex | None = None
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime
    git_sha: GitSha
    artifact_ids: Annotated[tuple[Sha256Hex, ...], SortedUnique] = ()
    error: Text | None = None
    conditional_validator: ConditionalValidator | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_run(self.status, self.started_at, self.finished_at, self.error)
        if self.status is RunStatus.SUCCEEDED and not self.artifact_ids:
            raise ValueError("a successful fetch must store at least one artifact")
        if self.conditional_validator is not None and self.status is not RunStatus.SUCCEEDED:
            raise ValueError("only a successful fetch records a conditional validator")
        return self


class NotModifiedObservation(VersionedModel):
    """A real HTTP 304 re-observing the unchanged body of one earlier successful fetch.

    It is neither a new fetch run nor a new artifact: it names the original run and
    artifact, the profile it was collected under, and the validator the server confirmed.
    Publication accepts it only as proof of freshness; it approves nothing.
    """

    run_id: Identifier
    source_id: Identifier
    profile_id: Identifier
    profile_sha256: Sha256Hex
    git_sha: GitSha
    started_at: UtcDatetime
    observed_at: UtcDatetime
    original_run_id: Identifier
    original_artifact_id: Sha256Hex
    validator: ConditionalValidator
    response_etag: Text | None = None
    http_status: Literal[304] = 304

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.observed_at < self.started_at:
            raise ValueError("observed_at cannot precede started_at")
        if self.run_id == self.original_run_id:
            raise ValueError("a 304 re-observation is its own run, not the original fetch")
        return self


class SourceFingerprint(VersionedModel):
    """Content and layout digests of one fetched artifact, for drift detection.

    ``adapter`` names the parser that produced ``structure_sha256``. T040/T045 must fill
    both. Publication compares them to the human ``SourceApproval`` and does not derive
    a layout digest itself. Absent adapter or digest fails closed.
    """

    source_id: Identifier
    fetch_run_id: Identifier
    artifact_id: Sha256Hex
    observed_at: UtcDatetime
    content_sha256: Sha256Hex
    structure_sha256: Sha256Hex
    adapter: ParserVersion | None = None


class ExtractionRun(VersionedModel):
    """One parser pass over one fetch run's artifacts."""

    run_id: Identifier
    fetch_run_id: Identifier
    parser: ParserVersion
    git_sha: GitSha
    configuration_sha256: Sha256Hex
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime
    candidate_count: Annotated[int, Field(ge=0)] = 0
    error: Text | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_run(self.status, self.started_at, self.finished_at, self.error)
        if self.status is not RunStatus.SUCCEEDED and self.candidate_count:
            raise ValueError("a failed extraction cannot report candidates")
        return self


class ExtractedCandidate(VersionedModel):
    """A source-grounded value proposal; ``proposed_path`` is where it would normalize to."""

    candidate_id: Identifier
    extraction_run_id: Identifier
    proposed_path: FieldPath | None = None
    raw_value: Text
    provenance: Provenance

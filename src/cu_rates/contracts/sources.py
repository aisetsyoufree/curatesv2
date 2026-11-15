"""Collection-pipeline records: sources, fetch profiles, runs, fingerprints, candidates.

Domain contracts only; no I/O, storage, or worker logic. Extraction output is a
*candidate*: it is evidence-addressed but carries no authority until deterministic
normalization, validation, and (where required) human review have run.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from cu_rates.contracts.base import (
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
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime
    git_sha: GitSha
    artifact_ids: Annotated[tuple[Sha256Hex, ...], SortedUnique] = ()
    error: Text | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_run(self.status, self.started_at, self.finished_at, self.error)
        if self.status is RunStatus.SUCCEEDED and not self.artifact_ids:
            raise ValueError("a successful fetch must store at least one artifact")
        return self


class SourceFingerprint(VersionedModel):
    """Content and layout digests of one fetched artifact, for drift detection."""

    source_id: Identifier
    fetch_run_id: Identifier
    artifact_id: Sha256Hex
    observed_at: UtcDatetime
    content_sha256: Sha256Hex
    structure_sha256: Sha256Hex


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

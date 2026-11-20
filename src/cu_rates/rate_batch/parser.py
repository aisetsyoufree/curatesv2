"""Strict, bounded parser for offline rate batch payloads (JSON and JSONL).

Enforces schema versioning, payload byte size boundaries, float prohibition,
UTC timestamps, non-future dates, digest consistency, and structural integrity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cu_rates.rate_batch.models import (
    BATCH_SCHEMA_VERSION,
    FORBIDDEN_PUBLICATION_KEYS,
    MAX_BATCH_BYTES,
    MAX_BATCH_ITEMS,
    BatchObservation,
    CrossReferenceError,
    ForbiddenFieldError,
    HashTamperingError,
    OversizedInputError,
    RateBatchEnvelope,
    _check_no_path_escape,
)


def _reject_float_in_json(s: str) -> None:
    raise ValueError(f"binary floats are rejected; pass Decimal or a decimal string, got {s!r}")


def _reject_constant_in_json(s: str) -> None:
    raise ValueError(f"prohibited JSON constant: {s!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, val in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in JSON object: {key!r}")
        seen.add(key)
        result[key] = val
    return result


def compute_batch_digest(envelope_data: Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 digest over envelope payload excluding batch_digest."""
    filtered = {k: v for k, v in envelope_data.items() if k != "batch_digest"}
    canonical = json.dumps(
        filtered,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda o: str(o) if isinstance(o, (Decimal, datetime)) else o,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recursively_check_forbidden(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_PUBLICATION_KEYS:
                raise ForbiddenFieldError(f"attempt to set publication/approval/confidence: {k!r}")
            _recursively_check_forbidden(v)
    elif isinstance(obj, list):
        for item in obj:
            _recursively_check_forbidden(item)


def _check_datetime_safe(dt: datetime, name: str, evaluation_time: datetime) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime, got naive {dt!r}")
    if dt.year < 2000 or dt.year > 2100:
        raise ValueError(f"{name} has invalid/fake timestamp year {dt.year}")
    if dt > evaluation_time:
        raise ValueError(
            f"{name} {dt.isoformat()} is in the future relative to {evaluation_time.isoformat()}"
        )


def parse_rate_batch(
    content: str | bytes,
    *,
    max_bytes: int = MAX_BATCH_BYTES,
    now: datetime | None = None,
) -> RateBatchEnvelope:
    """Parse and validate an offline rate batch from JSON or JSONL format.

    Args:
        content: UTF-8 JSON text, bytes, or JSONL lines.
        max_bytes: Strict upper limit on raw payload byte length.
        now: Reference instant for rejecting future timestamps (defaults to current UTC).

    Returns:
        Validated `RateBatchEnvelope`.

    Raises:
        OversizedInputError: If content byte length exceeds max_bytes or item limits.
        HashTamperingError: If batch_digest does not match content digest.
        CrossReferenceError: If candidates/findings mismatch observation bindings.
        ForbiddenFieldError: If publication/approval flags are present.
        ValueError: On schema, syntax, float, constant, duplicate keys, or datetime violations.
    """
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw_bytes) > max_bytes:
        raise OversizedInputError(
            f"batch payload size {len(raw_bytes)} bytes exceeds limit {max_bytes}"
        )
    if b"\x00" in raw_bytes:
        raise ValueError("raw payload contains prohibited null bytes")

    try:
        text = raw_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"malformed UTF-8 input: {exc}") from exc

    if not text:
        raise ValueError("batch payload is empty")

    evaluation_time = now if now is not None else datetime.now(UTC)

    # Detect JSON vs JSONL
    first_char = text[0]
    is_jsonl = False
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if len(lines) > 1 and not (first_char == "{" and text.endswith("}")):
        is_jsonl = True
    elif len(lines) > 1:
        # Check if line 1 has "type": "envelope"
        try:
            sample = json.loads(
                lines[0],
                object_pairs_hook=_reject_duplicate_keys,
                parse_float=_reject_float_in_json,
                parse_constant=_reject_constant_in_json,
            )
            if isinstance(sample, dict) and sample.get("type") == "envelope":
                is_jsonl = True
        except Exception:
            is_jsonl = False

    if is_jsonl:
        raw_data = _parse_jsonl(lines)
    else:
        try:
            parsed = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_float=_reject_float_in_json,
                parse_constant=_reject_constant_in_json,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON input: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"batch envelope must be a JSON object, got {type(parsed).__name__}")
        raw_data = parsed

    _recursively_check_forbidden(raw_data)

    claimed_digest = raw_data.get("batch_digest")
    if not isinstance(claimed_digest, str) or len(claimed_digest) != 64:
        raise ValueError("batch_digest must be a 64-character SHA-256 hex string")

    expected_digest = compute_batch_digest(raw_data)
    if claimed_digest.lower() != expected_digest.lower():
        raise HashTamperingError(
            f"batch_digest {claimed_digest!r} does not match computed content digest "
            f"{expected_digest!r}"
        )

    # Validate against Pydantic schema
    envelope = RateBatchEnvelope.model_validate(raw_data)

    if envelope.schema_version != BATCH_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported batch schema_version {envelope.schema_version!r}; expected "
            f"{BATCH_SCHEMA_VERSION}"
        )

    # Item limit check
    total_items = len(envelope.candidates) + len(envelope.findings)
    if total_items > MAX_BATCH_ITEMS:
        raise OversizedInputError(
            f"batch contains {total_items} items, exceeding maximum of {MAX_BATCH_ITEMS}"
        )

    # Path escape check on envelope identifiers and metadata
    _check_no_path_escape(envelope.batch_id)
    _check_no_path_escape(envelope.operator)
    _check_no_path_escape(envelope.model_name)
    _check_no_path_escape(envelope.tool_name)
    _check_no_path_escape(envelope.recipe_version)
    if envelope.prompt_version is not None:
        _check_no_path_escape(envelope.prompt_version)

    # Check timestamps against future and naive limit
    _check_datetime_safe(envelope.submitted_at, "envelope submitted_at", evaluation_time)

    if envelope.independent_review is not None:
        _check_datetime_safe(
            envelope.independent_review.reviewed_at,
            "independent_review reviewed_at",
            evaluation_time,
        )

    for obs in envelope.observations:
        _check_datetime_safe(
            obs.observed_at, f"observation {obs.source_id} observed_at", evaluation_time
        )

    # Check for duplicate identities
    cand_ids: set[str] = set()
    for cand in envelope.candidates:
        if cand.candidate_id in cand_ids:
            raise ValueError(f"duplicate candidate_id {cand.candidate_id!r}")
        cand_ids.add(cand.candidate_id)

        # Check tier IDs within candidate
        tier_ids: set[str] = set()
        for tier in cand.tiers:
            if tier.tier_id in tier_ids:
                raise ValueError(
                    f"duplicate tier_id {tier.tier_id!r} in candidate {cand.candidate_id}"
                )
            tier_ids.add(tier.tier_id)

    finding_ids: set[str] = set()
    for finding in envelope.findings:
        if finding.finding_id in finding_ids:
            raise ValueError(f"duplicate finding_id {finding.finding_id!r}")
        finding_ids.add(finding.finding_id)

    overlap = cand_ids.intersection(finding_ids)
    if overlap:
        raise ValueError(f"candidate_id and finding_id collision: {overlap}")

    obs_keys: set[tuple[str, str]] = set()
    for obs in envelope.observations:
        key = (obs.source_id, obs.fetch_run_id)
        if key in obs_keys:
            raise ValueError(
                f"duplicate observation for source_id {obs.source_id!r} and fetch_run_id "
                f"{obs.fetch_run_id!r}"
            )
        obs_keys.add(key)

    # Check candidate timestamp and ungrounded claims
    for cand in envelope.candidates:
        _check_datetime_safe(
            cand.observed_at, f"candidate {cand.candidate_id} observed_at", evaluation_time
        )
        if cand.effective_at is not None:
            _check_datetime_safe(
                cand.effective_at, f"candidate {cand.candidate_id} effective_at", evaluation_time
            )
        if cand.imported_at is not None:
            _check_datetime_safe(
                cand.imported_at, f"candidate {cand.candidate_id} imported_at", evaluation_time
            )
        if cand.independent_review is not None:
            _check_datetime_safe(
                cand.independent_review.reviewed_at,
                f"candidate {cand.candidate_id} review reviewed_at",
                evaluation_time,
            )

        # Rankable-looking ungrounded claims check:
        # A claimed headline rate or APR must have a corresponding field locator
        if cand.note_rate is not None and "note_rate" not in cand.locators:
            raise ValueError(
                f"candidate {cand.candidate_id} has ungrounded note_rate claim without "
                f"evidence locator"
            )
        if cand.apr is not None and "apr" not in cand.locators:
            raise ValueError(
                f"candidate {cand.candidate_id} has ungrounded apr claim without evidence locator"
            )

    for finding in envelope.findings:
        _check_datetime_safe(
            finding.observed_at, f"finding {finding.finding_id} observed_at", evaluation_time
        )

    # Cross-reference integrity between observations and items
    obs_index: dict[tuple[str, str], BatchObservation] = {
        (obs.source_id, obs.fetch_run_id): obs for obs in envelope.observations
    }

    for cand in envelope.candidates:
        key = (cand.source_id, cand.fetch_run_id)
        if key not in obs_index:
            raise CrossReferenceError(
                f"candidate {cand.candidate_id} references source_id {cand.source_id!r} and "
                f"fetch_run_id {cand.fetch_run_id!r} not declared in observations"
            )
        obs = obs_index[key]
        if obs.institution_charter != cand.institution_charter:
            raise CrossReferenceError(
                f"candidate {cand.candidate_id} charter {cand.institution_charter} does not match "
                f"observation charter {obs.institution_charter}"
            )
        if obs.artifact_id != cand.artifact_id:
            raise CrossReferenceError(
                f"candidate {cand.candidate_id} artifact {cand.artifact_id} does not match "
                f"observation artifact {obs.artifact_id}"
            )
        if obs.observed_at != cand.observed_at:
            raise CrossReferenceError("candidate timestamp does not match its capture")
        if obs.product_id is not None and obs.product_id != cand.product_id:
            raise CrossReferenceError(
                f"candidate {cand.candidate_id} product_id {cand.product_id!r} does not match "
                f"observation product_id {obs.product_id!r}"
            )

    for finding in envelope.findings:
        key = (finding.source_id, finding.fetch_run_id)
        if key not in obs_index:
            raise CrossReferenceError(
                f"finding {finding.finding_id} references source_id {finding.source_id!r} and "
                f"fetch_run_id {finding.fetch_run_id!r} not declared in observations"
            )
        obs = obs_index[key]
        if obs.institution_charter != finding.institution_charter:
            raise CrossReferenceError(
                f"finding {finding.finding_id} charter {finding.institution_charter} "
                "does not match "
                f"observation charter {obs.institution_charter}"
            )
        if obs.artifact_id != finding.artifact_id:
            raise CrossReferenceError(
                f"finding {finding.finding_id} artifact {finding.artifact_id} does not match "
                f"observation artifact {obs.artifact_id}"
            )
        if obs.observed_at != finding.observed_at:
            raise CrossReferenceError("finding timestamp does not match its capture")

    return envelope


def _parse_jsonl(lines: list[str]) -> dict[str, Any]:
    """Parse JSONL lines into an envelope structure."""
    envelope_dict: dict[str, Any] | None = None
    observations: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    independent_review: dict[str, Any] | None = None

    for i, line in enumerate(lines, start=1):
        try:
            entry = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_float=_reject_float_in_json,
                parse_constant=_reject_constant_in_json,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON on line {i}: {exc}") from exc
        if not isinstance(entry, dict):
            raise ValueError(f"line {i} must be a JSON object, got {type(entry).__name__}")

        item_type = entry.get("type")
        if item_type == "envelope":
            if envelope_dict is not None:
                raise ValueError("duplicate envelope header line in JSONL")
            envelope_dict = {k: v for k, v in entry.items() if k != "type"}
        elif item_type == "observation":
            observations.append({k: v for k, v in entry.items() if k != "type"})
        elif item_type == "candidate":
            candidates.append({k: v for k, v in entry.items() if k != "type"})
        elif item_type == "finding":
            findings.append({k: v for k, v in entry.items() if k != "type"})
        elif item_type == "independent_review":
            if independent_review is not None:
                raise ValueError("duplicate independent_review in JSONL")
            independent_review = {k: v for k, v in entry.items() if k != "type"}
        elif envelope_dict is None and i == 1:
            # Assume first line is envelope even if 'type' omitted
            envelope_dict = entry
        else:
            raise ValueError(f"unknown line type {item_type!r} on line {i}")

    if envelope_dict is None:
        raise ValueError("JSONL batch missing envelope header line")

    # Combine
    combined = dict(envelope_dict)
    if observations:
        combined["observations"] = observations
    if candidates:
        combined["candidates"] = candidates
    if findings:
        combined["findings"] = findings
    if independent_review:
        combined["independent_review"] = independent_review

    return combined

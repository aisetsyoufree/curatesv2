"""Offline T045 validation. The publication gate still owns approval and ranking."""

from __future__ import annotations

import json
from hashlib import sha256

from cu_rates.contracts import (
    ExtractedCandidate,
    FetchRun,
    NormalizedOffer,
    OfferFingerprints,
    OfferStatus,
    ParserVersion,
    ProductIdentity,
    Provenance,
    RateSource,
    RawArtifact,
    ValidationResult,
    offer_fingerprints,
)
from cu_rates.contracts.base import ContractModel, FieldPath, Sha256Hex, UtcDatetime

from ._rules import RULE_IDS, Checks

__all__ = [
    "RULE_IDS",
    "VALIDATOR",
    "QuarantineFinding",
    "QuarantineReason",
    "QuarantineRecord",
    "ValidationContext",
    "ValidationRun",
    "validate_offer",
]

VALIDATOR = ParserVersion(name="t045-validation", version="1.0.0")
_HANDOFF_STATUSES = frozenset({OfferStatus.CANDIDATE, OfferStatus.APPROVED, OfferStatus.PUBLISHED})


class ValidationContext(ContractModel):
    """Supply stored, hash-verified captures and all competing candidates for this offer row.

    Scope candidates to the same offer (not other rows in a grid). The module checks metadata
    and locators without reading bytes or making requests. Approval, layout, and independent
    scenario verification stay with the publication gate. This stage accepts direct captures;
    it does not reconstruct or extend freshness from HTTP 304 receipts.

    Material evidence quotes the original field text, not a generic page or synthesized value.
    A stated effective date also requires its matching extracted candidate. Bounds evidence
    quotes a single range or all labeled ranges (e.g. "ltv: <=80%; credit score: >=780").
    The caller supplies a policy expiry, not a made-up lender expiration.
    """

    evaluated_at: UtcDatetime
    source: RateSource
    artifacts: tuple[RawArtifact, ...]
    fetch_runs: tuple[FetchRun, ...]
    candidates: tuple[ExtractedCandidate, ...] = ()
    product: ProductIdentity | None = None


class QuarantineFinding(ContractModel):
    path: FieldPath
    explanation: str


class QuarantineReason(ContractModel):
    result: ValidationResult
    findings: tuple[QuarantineFinding, ...]
    evidence: tuple[Provenance, ...]


class QuarantineRecord(ContractModel):
    """Internal-only evidence receipt; persist with a unique key on quarantine_id."""

    quarantine_id: Sha256Hex
    run_id: Sha256Hex
    reasons: tuple[QuarantineReason, ...]


class ValidationRun(ContractModel):
    run_id: Sha256Hex
    offer_binding_sha256: Sha256Hex
    fingerprints: OfferFingerprints
    validator: ParserVersion
    evaluated_at: UtcDatetime
    results: tuple[ValidationResult, ...]
    comparison_blockers: tuple[str, ...]
    quarantine: QuarantineRecord | None

    def for_publication(self, offer: NormalizedOffer) -> tuple[ValidationResult, ...]:
        """Return eligible receipts for this exact offer, even after a status transition.

        The caller must stop on quarantine (empty handoff), then run the publication gate.
        That gate's ValidationPolicy may only require receipt IDs it accepts; age, capture,
        and contradiction checks remain mandatory in this stage even when not handed off.
        """
        if (
            self.quarantine is not None
            or offer.status not in _HANDOFF_STATUSES
            or self.offer_binding_sha256 != _offer_binding(offer)
        ):
            return ()
        known = set(offer.rate_values()) | offer.material_paths() | {"points"}
        return tuple(result for result in self.results if set(result.checked_paths) <= known)


def _json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(domain: str, payload: object) -> str:
    return sha256(f"{domain}:{_json(payload)}".encode()).hexdigest()


def _offer_binding(offer: NormalizedOffer) -> str:
    return _digest(
        "t045-offer-v1",
        offer.model_dump(mode="json", exclude={"status", "published_at"}),
    )


def validate_offer(offer: NormalizedOffer, context: ValidationContext) -> ValidationRun:
    """Validate a candidate as of the supplied clock; no mutations, persistence or I/O."""
    offer_binding = _offer_binding(offer)
    fingerprints = offer_fingerprints(offer)
    context_json = context.model_dump(mode="json")
    for field in ("artifacts", "fetch_runs", "candidates"):
        context_json[field] = sorted({_json(item) for item in context_json[field]})
    run_id = _digest(
        "t045-run-v1",
        [offer_binding, offer.status.value, VALIDATOR.version, RULE_IDS, context_json],
    )
    checks = Checks(offer, context)
    results: list[ValidationResult] = []
    failures: list[QuarantineReason] = []
    for rule_id, paths, problems in checks.rules():
        problems = sorted(set(problems))
        problem_paths = sorted({path for path, _ in problems})
        detail = "; ".join(f"{path}: {reason}" for path, reason in problems)
        if len(detail) > 4000:
            detail = detail[:3900] + " … (see complete quarantine findings)"
        result = ValidationResult(
            rule_id=rule_id,
            checked_paths=tuple(sorted(set(paths) | set(problem_paths))),
            passed=not problems,
            detail=detail or None,
            offer_id=offer.offer_id,
            fingerprints=fingerprints,
            evaluated_at=context.evaluated_at,
            validator=VALIDATOR,
        )
        results.append(result)
        if problems:
            evidence_by_key: dict[str, Provenance] = {}
            for item in checks.relevant_evidence(problem_paths):
                evidence_by_key[_json(item.model_dump(mode="json"))] = item
            failures.append(
                QuarantineReason(
                    result=result,
                    findings=tuple(
                        QuarantineFinding(path=path, explanation=reason)
                        for path, reason in problems
                    ),
                    evidence=tuple(evidence_by_key[key] for key in sorted(evidence_by_key)),
                )
            )
    quarantine = (
        QuarantineRecord(
            quarantine_id=_digest(
                "t045-quarantine-v1",
                [
                    offer_binding,
                    VALIDATOR.version,
                    [
                        [
                            failure.result.rule_id,
                            [item.model_dump(mode="json") for item in failure.findings],
                            [item.model_dump(mode="json") for item in failure.evidence],
                        ]
                        for failure in failures
                    ],
                ],
            ),
            run_id=run_id,
            reasons=tuple(failures),
        )
        if failures
        else None
    )
    comparison = checks.comparison_blockers()
    if quarantine is not None:
        comparison.add("quarantined")
    return ValidationRun(
        run_id=run_id,
        offer_binding_sha256=offer_binding,
        fingerprints=fingerprints,
        validator=VALIDATOR,
        evaluated_at=context.evaluated_at,
        results=tuple(results),
        comparison_blockers=tuple(sorted(comparison)),
        quarantine=quarantine,
    )

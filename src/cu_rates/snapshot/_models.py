"""Public projections: financial facts and locator references, without captured text."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import JsonValue, model_validator

from cu_rates.contracts.base import (
    CharterNumber,
    ContractModel,
    FieldPath,
    HttpUrlText,
    Identifier,
    Points,
    SchemaVersion,
    Sha256Hex,
    SignedPercent,
    Text,
    UtcDatetime,
    contract_arithmetic,
)
from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    LoanPurpose,
    MatchState,
    ProductFamily,
    ScenarioDimension,
)
from cu_rates.contracts.evidence import EvidenceLocator
from cu_rates.contracts.fingerprints import OfferFingerprints, RateValue, value_fingerprint
from cu_rates.contracts.offers import TierBound
from cu_rates.contracts.publication import (
    AUTHORITATIVE_SOURCE_CLASSES,
    DUAL_EVIDENCE_TIME_WINDOW,
    Placement,
)
from cu_rates.contracts.scenarios import (
    CANONICAL_SCENARIOS,
    REQUIRED_DIMENSIONS,
    StatedBound,
    computed_match_state,
)


class PublicEvidence(ContractModel):
    path: FieldPath
    artifact_id: Sha256Hex
    locator: EvidenceLocator


class PublicAssumption(ContractModel):
    dimension: ScenarioDimension
    state: MatchState
    stated: StatedBound | None


class PublicCondition(ContractModel):
    condition_id: Identifier
    kind: ConditionKind
    scenario_state: MatchState


class PublicTier(ContractModel):
    tier_id: Identifier
    bounds: tuple[TierBound, ...]


class PublicEvidencePath(PublicEvidence):
    """Public portion of a path already verified by the worker admission gate."""

    value: SignedPercent
    content_sha256: Sha256Hex
    capture_id: Identifier
    observed_at: UtcDatetime
    source_id: Identifier
    product_id: Identifier
    scenario_id: CanonicalScenarioId
    source_class: Text


class PublicAdmission(ContractModel):
    publication_version: Literal["2.0"]
    publication_evidence: tuple[PublicEvidencePath, ...]
    publication_evidence_sha256: Sha256Hex


def public_evidence_sha256(
    offer_id: str, fingerprints: OfferFingerprints, paths: tuple[PublicEvidencePath, ...]
) -> str:
    """Bind the public admission receipt, separately from the private review digest.

    The private publication_evidence_sha256 includes excerpts/extraction records
    that must never be exported. This domain binds their admitted public projection.
    """
    payload = {
        "publication_version": "2.0",
        "offer_id": offer_id,
        "fingerprints": fingerprints.model_dump(mode="json"),
        "paths": sorted(p.model_dump_json() for p in paths),
    }
    return hashlib.sha256(
        ("public-publication-evidence-v2:" + json.dumps(payload, sort_keys=True)).encode()
    ).hexdigest()


def derived_post_intro_rate(
    index: Decimal, margin: Decimal, floor: Decimal, ceiling: Decimal | None
) -> Decimal:
    """``max(index + margin, floor)`` capped by the ceiling, in the fixed contract context."""
    with contract_arithmetic():
        rate = max(index + margin, floor)
        return rate if ceiling is None else min(rate, ceiling)


def require_ranked_assumptions(
    scenario_id: CanonicalScenarioId, assumptions: tuple[PublicAssumption, ...]
) -> None:
    """A RANKED row must prove every required canonical assumption from its stated bounds.

    A stored state label is not evidence: the match is recomputed from the stated bound, a
    duplicated dimension is refused, and a missing, conflicting or untyped one fails closed.
    """
    dimensions = [a.dimension for a in assumptions]
    if len(dimensions) != len(set(dimensions)):
        raise ValueError("duplicate assumption dimension")
    scenario = CANONICAL_SCENARIOS[scenario_id]
    required = REQUIRED_DIMENSIONS[scenario_id]
    for assumption in assumptions:
        if assumption.dimension in required and (
            assumption.stated is None
            or assumption.state is not MatchState.MATCHES
            or computed_match_state(scenario, assumption.stated, assumption.dimension)
            is not MatchState.MATCHES
        ):
            raise ValueError("ranked offer has an unproven canonical assumption")
    if not required <= set(dimensions):
        raise ValueError("ranked offer lacks evidence for a canonical assumption")


class PublicOffer(ContractModel):
    offer_id: Identifier
    product_id: Identifier
    source_id: Identifier
    institution_charter: CharterNumber
    institution_name: Text
    source_url: HttpUrlText
    family: ProductFamily
    purpose: LoanPurpose | None
    scenario_id: CanonicalScenarioId | None
    placement: Placement
    review_status: Literal["automatic", "reviewed"]
    effective_at: UtcDatetime | None
    observed_at: UtcDatetime
    valid_until: UtcDatetime
    rates: tuple[RateValue, ...]
    points: Points | None
    details: dict[str, JsonValue]
    assumptions: tuple[PublicAssumption, ...]
    conditions: tuple[PublicCondition, ...]
    tiers: tuple[PublicTier, ...]
    headline_is_as_low_as: bool
    evidence: tuple[PublicEvidence, ...]
    fingerprints: OfferFingerprints

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        rates = {rate.path: rate.value for rate in self.rates}
        if (
            not rates
            or len(rates) != len(self.rates)
            or value_fingerprint(rates, self.points) != self.fingerprints.value
            or self.placement is Placement.NONE
            or self.valid_until <= self.observed_at
            or not self.evidence
        ):
            raise ValueError("invalid public offer or value fingerprint")
        return self

    def require_protocol2(self) -> PublicAdmission:
        """Enforce admission at the storage trust boundary, including retained versions.

        A projection alone is not publication approval. Both public reads and
        rollback must call this before exposing or activating any stored row.
        """
        admission = PublicAdmission.model_validate(
            {name: self.details.get(name) for name in PublicAdmission.model_fields}
        )
        paths = admission.publication_evidence
        if admission.publication_evidence_sha256 != public_evidence_sha256(
            self.offer_id, self.fingerprints, paths
        ):
            raise ValueError("public publication evidence digest mismatch")
        rates = {rate.path: rate.value for rate in self.rates}
        # The HELOC post-intro value is a deterministic alias/derivation of sourced
        # values, not a separately captured claim. Require corroboration of all
        # its inputs and recompute it rather than inventing evidence for the output.
        if self.family is ProductFamily.HELOC and "details.post_intro_rate" in rates:
            post_intro = rates.pop("details.post_intro_rate")
            expected = rates.get("details.stated_post_intro_apr")
            if expected is None:
                index = rates.get("details.index_rate")
                margin = rates.get("details.margin")
                floor = rates.get("details.floor")
                if (
                    index is None
                    or margin is None
                    or floor is None
                    or not self.details.get("index_name")
                ):
                    raise ValueError("HELOC derivation requires all sourced inputs")
                expected = derived_post_intro_rate(
                    index, margin, floor, rates.get("details.ceiling")
                )
            if post_intro != expected:
                raise ValueError("HELOC post-intro value does not match its sourced inputs")
        if self.placement is Placement.RANKED:
            # A ranked row must carry evidence for every canonical assumption. Absence,
            # a conflict or an untyped statement can never be re-admitted from storage.
            if self.scenario_id is None:
                raise ValueError("ranked offer has no canonical scenario")
            require_ranked_assumptions(self.scenario_id, self.assumptions)
        values = {**rates, **({"points": self.points} if self.points is not None else {})}
        if {p.path for p in paths} != values.keys():
            raise ValueError("publication evidence must cover every financial value")
        for p in paths:
            if (
                p.value != values[p.path]
                or p.source_id != self.source_id
                or p.product_id != self.product_id
                or p.scenario_id != self.scenario_id
                or p.source_class not in AUTHORITATIVE_SOURCE_CLASSES
                or p.content_sha256 != p.artifact_id
                or not self.observed_at - DUAL_EVIDENCE_TIME_WINDOW
                <= p.observed_at
                <= self.observed_at
                or PublicEvidence(path=p.path, artifact_id=p.artifact_id, locator=p.locator)
                not in self.evidence
            ):
                raise ValueError("publication evidence is not bound to the public value")
        for path in values:
            pair = [p for p in paths if p.path == path]
            if len(pair) != 2 or any(
                getattr(pair[0], key) == getattr(pair[1], key)
                for key in ("artifact_id", "content_sha256", "capture_id", "observed_at")
            ):
                raise ValueError("public value requires two independent evidence paths")
        return admission


class SnapshotInfo(ContractModel):
    publication_version: Literal["2.0"]
    schema_version: SchemaVersion
    snapshot_id: Identifier
    published_at: UtcDatetime
    published_by: Text
    manifest_sha256: Sha256Hex
    supersedes: Identifier | None


class SnapshotView(ContractModel):
    info: SnapshotInfo
    offers: tuple[PublicOffer, ...]


class Version(ContractModel):
    """Small pointer/descriptor. The file digest also covers fields outside the manifest."""

    version: Literal[1]
    snapshot_id: Identifier
    database_sha256: Sha256Hex


def aware_time(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("snapshot clocks must include a timezone")
    return value

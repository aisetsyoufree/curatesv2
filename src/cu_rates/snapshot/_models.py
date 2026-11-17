"""Public projections: financial facts and locator references, without captured text."""

from __future__ import annotations

from datetime import datetime
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
    Text,
    UtcDatetime,
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
from cu_rates.contracts.publication import Placement
from cu_rates.contracts.scenarios import StatedBound


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


class SnapshotInfo(ContractModel):
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

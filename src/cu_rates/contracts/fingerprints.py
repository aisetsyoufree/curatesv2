"""Structural versus value fingerprints for offers, and rate-change proof.

The *structural* fingerprint covers everything that defines what an offer is (product,
terms, conditions, tier boundaries, which values are present). The *value* fingerprint
covers only the numeric rate/points values, defined as the stable path -> value map
(``NormalizedOffer.rate_values()``) plus explicit points, so an ``OfferSnapshot`` can be
validated by recomputation. A pure rate move therefore changes the value
fingerprint while leaving the structural fingerprint identical; any other edit changes the
structural fingerprint. Provenance, timestamps, status, and identifiers are excluded.
Scenario-match *states* and condition *scenario states* are structural: promoting a
dimension from unstated/conflict to match changes ranking eligibility, so it needs review.
The quoted ``source_text`` of a match is noise and excluded.

Rate movement is proved per semantic path (every sourced rate, tier, margin, and the
computed HELOC post-intro rate), never from headline values alone.
"""

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from operator import attrgetter
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator

from cu_rates.contracts.base import (
    ContractModel,
    FieldPath,
    Identifier,
    Points,
    Sha256Hex,
    SignedPercent,
    canonical_decimal,
)
from cu_rates.contracts.enums import ChangeKind
from cu_rates.contracts.evidence import Provenance, SourcedMargin, SourcedPoints, SourcedRate
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.registry import ProductIdentity

FINGERPRINT_ALGORITHM = "fp1"

_EXCLUDED: dict[str, frozenset[str]] = {
    "NormalizedOffer": frozenset(
        {
            "schema_version",
            "offer_id",
            "status",
            "effective_at",
            "observed_at",
            "published_at",
            "valid_until",
            "supporting_evidence",
            "field_provenance",
        }
    ),
    "DimensionMatch": frozenset({"source_text"}),
    "ProductIdentity": frozenset({"schema_version"}),
}
_ID_SORTED = {
    "conditions": "condition_id",
    "tiers": "tier_id",
    "scenario_matches": "dimension",
}

SignedBasisPoints = Annotated[Decimal, Field(allow_inf_nan=False)]


def _structure(obj: object) -> object:
    """Canonical structural view of ``obj``; sourced values contribute only their presence."""
    if isinstance(obj, SourcedRate | SourcedMargin | SourcedPoints):
        return True
    if isinstance(obj, BaseModel):
        excluded = _EXCLUDED.get(type(obj).__name__, frozenset())
        structure: dict[str, object] = {}
        for name in type(obj).model_fields:
            raw = getattr(obj, name)
            if name in excluded or isinstance(raw, Provenance):
                continue
            if name in _ID_SORTED:
                raw = tuple(sorted(raw, key=attrgetter(_ID_SORTED[name])))
            structure[name] = _structure(raw)
        return structure
    if isinstance(obj, tuple):
        return [_structure(item) for item in obj]
    if isinstance(obj, Decimal):
        return canonical_decimal(obj)
    if isinstance(obj, Enum):
        return obj.value
    return obj


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{FINGERPRINT_ALGORITHM}:{text}".encode()).hexdigest()


class OfferFingerprints(ContractModel):
    algorithm: Literal["fp1"] = "fp1"
    structural: Sha256Hex
    value: Sha256Hex


def value_fingerprint(rates: Mapping[str, Decimal], points: Decimal | None) -> str:
    """Digest of the path -> rate map and explicit points; null points differ from zero."""
    return _digest(
        {
            "rates": {path: canonical_decimal(value) for path, value in sorted(rates.items())},
            "points": None if points is None else canonical_decimal(points),
        }
    )


def offer_fingerprints(offer: NormalizedOffer) -> OfferFingerprints:
    points = offer.points.value if offer.points else None
    return OfferFingerprints(
        structural=_digest(_structure(offer)),
        value=value_fingerprint(offer.rate_values(), points),
    )


def product_content_sha256(product: ProductIdentity) -> str:
    """Digest of a product's canonical content, so a review binds to what was reviewed."""
    return _digest(_structure(product))


def classify_change(prior: OfferFingerprints, current: OfferFingerprints) -> ChangeKind:
    if prior.structural != current.structural:
        return ChangeKind.STRUCTURAL
    if prior.value != current.value:
        return ChangeKind.VALUE_ONLY
    return ChangeKind.UNCHANGED


class RateValue(ContractModel):
    path: FieldPath
    value: SignedPercent


def _sorted_unique_paths(values: tuple[RateValue, ...]) -> tuple[RateValue, ...]:
    ordered = tuple(sorted(values, key=lambda v: v.path))
    if len({v.path for v in ordered}) != len(ordered):
        raise ValueError("duplicate rate path in snapshot")
    return ordered


class OfferSnapshot(ContractModel):
    """The minimum needed to prove a later change: fingerprints plus every rate by path.

    The value fingerprint is recomputed from ``rates`` and ``points``, so a snapshot cannot
    list one set of numbers while claiming another offer's fingerprints.
    """

    offer_id: Identifier
    product_id: Identifier
    fingerprints: OfferFingerprints
    rates: Annotated[tuple[RateValue, ...], AfterValidator(_sorted_unique_paths)] = ()
    points: Points | None = None

    @model_validator(mode="after")
    def _values_match_fingerprint(self) -> Self:
        rates = {r.path: r.value for r in self.rates}
        if value_fingerprint(rates, self.points) != self.fingerprints.value:
            raise ValueError("snapshot rates/points do not match the value fingerprint")
        return self

    @classmethod
    def from_offer(cls, offer: NormalizedOffer) -> Self:
        return cls(
            offer_id=offer.offer_id,
            product_id=offer.product_id,
            fingerprints=offer_fingerprints(offer),
            rates=tuple(RateValue(path=p, value=v) for p, v in offer.rate_values().items()),
            points=offer.points.value if offer.points else None,
        )


class PathDelta(ContractModel):
    path: FieldPath
    delta_bp: SignedBasisPoints


class RateChangeProof(ContractModel):
    """Evidence that a movement is a pure rate change (or why it is not).

    ``deltas`` cover every path present on both sides. ``unbounded`` is set when the
    value fingerprint moved (or the path sets differ without a structural change) yet no
    paired path or points change accounts for it: the movement cannot be bounded, so it
    must not be trusted as small.
    """

    change: ChangeKind
    structural_unchanged: bool
    deltas: tuple[PathDelta, ...] = ()
    points_changed: bool
    unbounded: bool = False

    @property
    def max_abs_delta_bp(self) -> Decimal:
        return max((abs(d.delta_bp) for d in self.deltas), default=Decimal(0))

    def delta_for(self, path: str) -> Decimal | None:
        return next((d.delta_bp for d in self.deltas if d.path == path), None)


def prove_rate_change(prior: OfferSnapshot, current: OfferSnapshot) -> RateChangeProof:
    change = classify_change(prior.fingerprints, current.fingerprints)
    before = {r.path: r.value for r in prior.rates}
    after = {r.path: r.value for r in current.rates}
    deltas = tuple(
        PathDelta(path=path, delta_bp=(after[path] - before[path]) * 100)
        for path in sorted(before.keys() & after.keys())
    )
    points_changed = prior.points != current.points
    explained = points_changed or any(d.delta_bp != 0 for d in deltas)
    value_moved = prior.fingerprints.value != current.fingerprints.value
    unbounded = (value_moved and not explained) or (
        before.keys() != after.keys() and change is not ChangeKind.STRUCTURAL
    )
    return RateChangeProof(
        change=change,
        structural_unchanged=change is not ChangeKind.STRUCTURAL,
        deltas=deltas,
        points_changed=points_changed,
        unbounded=unbounded,
    )

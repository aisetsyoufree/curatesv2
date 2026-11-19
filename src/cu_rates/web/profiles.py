"""Credit-union profile and coverage read model, derived only from the public snapshot.

The public snapshot carries verified offers and nothing else about an institution, so this
read model can express exactly two coverage states: ``verified`` (a display-eligible offer
observed within the degrade window) and ``stale`` (display-eligible offers exist but all are
older than that window). Every other MASTER section 5 state (no product, no public rate,
blocked, unsupported) is NOT derivable from public data today and is rendered as ``unknown``
rather than guessed. Nothing here opens a database or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from flask import current_app, g

from cu_rates.contracts.enums import ProductFamily
from cu_rates.contracts.publication import DEGRADE_AFTER
from cu_rates.snapshot import SnapshotInfo
from cu_rates.web.comparison import (
    FormattedOffer,
    classify_and_format_offer,
    read_snapshot_offers,
)

FAMILY_LABELS: dict[ProductFamily, str] = {
    ProductFamily.MORTGAGE: "First mortgage",
    ProductFamily.HOME_EQUITY_LOAN: "Fixed-rate home equity loan",
    ProductFamily.HELOC: "Home equity line of credit (HELOC)",
}

STATE_LABELS: dict[str, str] = {
    "verified": "Verified",
    "stale": "Stale",
    "unknown": "Unknown",
}


@dataclass(frozen=True)
class CoverageRow:
    family: str
    label: str
    state: str
    state_label: str
    offers: tuple[FormattedOffer, ...]


@dataclass(frozen=True)
class Institution:
    charter: int
    name: str
    coverage: tuple[CoverageRow, ...]

    @property
    def offer_count(self) -> int:
        return sum(len(row.offers) for row in self.coverage)

    @property
    def has_verified(self) -> bool:
        """Indexable content: at least one fresh, display-eligible verified offer."""
        return any(row.state == "verified" for row in self.coverage)


def snapshot_clock() -> datetime:
    now = datetime.now(UTC)
    override = current_app.config.get("SNAPSHOT_CLOCK")
    if isinstance(override, datetime):
        now = override
    return now if now.utcoffset() is not None else now.replace(tzinfo=UTC)


def _coverage_for(
    offers: list[FormattedOffer], now: datetime, family: ProductFamily
) -> CoverageRow:
    mine = tuple(sorted((o for o in offers if o.family == family.value), key=_offer_key))
    if not mine:
        state = "unknown"
    elif any(now - o.sort_freshness <= DEGRADE_AFTER for o in mine):
        state = "verified"
    else:
        state = "stale"
    return CoverageRow(family.value, FAMILY_LABELS[family], state, STATE_LABELS[state], mine)


def _offer_key(offer: FormattedOffer) -> tuple[str, str]:
    return (offer.scenario_id or "", offer.offer_id)


def build_directory(
    now: datetime,
) -> tuple[SnapshotInfo | None, dict[int, Institution]]:
    """Institutions present in the verified snapshot, keyed by charter.

    A charter whose offers disagree on the legal name is omitted (fail closed: identity is
    not verified), as is any charter the reader already withheld (denylist, expiry, withdrawal).
    """
    info, raw_offers = read_snapshot_offers(at=now)
    names: dict[int, set[str]] = {}
    for raw in raw_offers:
        names.setdefault(raw.institution_charter, set()).add(raw.institution_name)
    eligible: dict[int, list[FormattedOffer]] = {}
    for raw in raw_offers:
        item = classify_and_format_offer(raw, now)
        if item is not None:
            eligible.setdefault(raw.institution_charter, []).append(item)
    directory: dict[int, Institution] = {}
    for charter, name_set in names.items():
        if len(name_set) != 1:
            continue
        offers = eligible.get(charter, [])
        coverage = tuple(_coverage_for(offers, now, family) for family in FAMILY_LABELS)
        directory[charter] = Institution(charter, next(iter(name_set)), coverage)
    return info, directory


def request_directory() -> tuple[SnapshotInfo | None, dict[int, Institution]]:
    """One snapshot read per request (the SEO context processor and the view share it)."""
    cached: Any = g.get("_directory")
    if cached is None:
        cached = build_directory(snapshot_clock())
        g._directory = cached
    result: tuple[SnapshotInfo | None, dict[int, Institution]] = cached
    return result

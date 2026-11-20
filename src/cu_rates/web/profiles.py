"""Credit-union profile and coverage read model, derived only from the public snapshot.

The public snapshot carries verified offers and nothing else about an institution, so this
read model can express three coverage states: ``verified`` (a display-eligible offer
observed within the degrade window), ``detail_only`` (only fresh note rates without APR,
shown on this profile alone under D-05 and never counted as verified for comparison or SEO)
and ``stale`` (offers exist but all are older than that window). Other MASTER section 5 states
(no product, no public rate, blocked, unsupported) cannot be inferred from public data and
are rendered as ``unknown``. Nothing here opens a database or the network.
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
    format_institution_detail_offer,
    read_snapshot_offers,
)

FAMILY_LABELS: dict[ProductFamily, str] = {
    ProductFamily.MORTGAGE: "First mortgage",
    ProductFamily.HOME_EQUITY_LOAN: "Fixed-rate home equity loan",
    ProductFamily.HELOC: "Home equity line of credit (HELOC)",
}

STATE_LABELS: dict[str, str] = {
    "verified": "Verified",
    # D-05: fresh verified note rate(s) with no APR. Not "verified" for comparison or SEO.
    "detail_only": "Institution detail only (APR not published)",
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
        """Indexable content: at least one fresh, display-eligible verified offer.

        Detail-only (no APR) rows never count, so they cannot make a profile indexable
        or put it in the sitemap on their own.
        """
        return any(row.state == "verified" for row in self.coverage)

    @property
    def has_detail_only(self) -> bool:
        """At least one profile-only note rate without APR is shown (D-05)."""
        return any(o.detail_only for row in self.coverage for o in row.offers)


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
    fresh = [o for o in mine if now - o.sort_freshness <= DEGRADE_AFTER]
    if not mine:
        state = "unknown"
    elif any(not o.detail_only for o in fresh):
        state = "verified"
    elif fresh:
        state = "detail_only"
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
        # The two projections are disjoint by placement. The detail-only one is used only
        # here, for the institution's own profile (D-05); comparison callers never see it.
        item = classify_and_format_offer(raw, now) or format_institution_detail_offer(raw, now)
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

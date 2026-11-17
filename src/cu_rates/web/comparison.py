"""Comparison logic, canonical scenario disclosures, and offer presentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app

from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    MatchState,
    ProductFamily,
)
from cu_rates.contracts.publication import DEGRADE_AFTER, WITHDRAW_AFTER, Placement
from cu_rates.snapshot import PublicOffer, SnapshotError, SnapshotInfo, SnapshotReader

EASTERN_TZ = ZoneInfo("America/New_York")

ALLOWED_PARAMS = frozenset({"scenario", "sort"})

ALLOWED_SCENARIOS = frozenset(
    {
        "all",
        CanonicalScenarioId.PURCHASE_30Y_FIXED.value,
        CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED.value,
        CanonicalScenarioId.FIXED_HOME_EQUITY_10Y.value,
        CanonicalScenarioId.HELOC.value,
    }
)

ALLOWED_SORTS = frozenset(
    {
        "institution",
        "institution_desc",
        "apr",
        "apr_desc",
        "rate",
        "rate_desc",
        "freshness",
    }
)

SORT_LABELS = {
    "institution": "Institution Name (A-Z)",
    "institution_desc": "Institution Name (Z-A)",
    "apr": "APR (Lowest First)",
    "apr_desc": "APR (Highest First)",
    "rate": "Note Rate (Lowest First)",
    "rate_desc": "Note Rate (Highest First)",
    "freshness": "Observed Time (Newest First)",
}


@dataclass(frozen=True)
class ScenarioAssumption:
    label: str
    value: str


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    title: str
    short_title: str
    description: str
    family: ProductFamily
    assumptions: tuple[ScenarioAssumption, ...]


CANONICAL_SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        scenario_id=CanonicalScenarioId.PURCHASE_30Y_FIXED.value,
        title="30-Year Fixed Mortgage — Home Purchase",
        short_title="30-Year Fixed Purchase",
        description=(
            "Conventional conforming mortgage for owner-occupied single-family home purchase."
        ),
        family=ProductFamily.MORTGAGE,
        assumptions=(
            ScenarioAssumption("Loan Purpose", "Home purchase"),
            ScenarioAssumption("Property Value", "$500,000"),
            ScenarioAssumption("Loan Amount", "$400,000"),
            ScenarioAssumption("Loan-to-Value (LTV)", "80.000% (First lien)"),
            ScenarioAssumption("Credit Score", "780"),
            ScenarioAssumption("Term", "30 years (360 months) fixed"),
            ScenarioAssumption("Rate Lock", "30 days"),
            ScenarioAssumption("Points", "0.000 (Zero points)"),
            ScenarioAssumption("Occupancy", "Owner-occupied single-family primary residence"),
            ScenarioAssumption("Discounts", "None (No relationship or automatic payment discount)"),
        ),
    ),
    ScenarioSpec(
        scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED.value,
        title="30-Year Fixed Mortgage — Rate and Term Refinance",
        short_title="30-Year Fixed Refinance",
        description="Conventional conforming mortgage refinance without cash out.",
        family=ProductFamily.MORTGAGE,
        assumptions=(
            ScenarioAssumption("Loan Purpose", "Rate-and-term refinance (no cash out)"),
            ScenarioAssumption("Property Value", "$500,000"),
            ScenarioAssumption("Loan Balance", "$400,000"),
            ScenarioAssumption("Loan-to-Value (LTV)", "80.000% (First lien)"),
            ScenarioAssumption("Credit Score", "780"),
            ScenarioAssumption("Term", "30 years (360 months) fixed"),
            ScenarioAssumption("Rate Lock", "30 days"),
            ScenarioAssumption("Points", "0.000 (Zero points)"),
            ScenarioAssumption("Occupancy", "Owner-occupied single-family primary residence"),
            ScenarioAssumption("Discounts", "None (No relationship or automatic payment discount)"),
        ),
    ),
    ScenarioSpec(
        scenario_id=CanonicalScenarioId.FIXED_HOME_EQUITY_10Y.value,
        title="Fixed-Rate Home Equity Loan — 10-Year",
        short_title="10-Year Fixed Equity",
        description="Closed-end fixed-rate second-lien home equity loan.",
        family=ProductFamily.HOME_EQUITY_LOAN,
        assumptions=(
            ScenarioAssumption("Loan Purpose", "Closed-end home equity loan (Second lien)"),
            ScenarioAssumption("Property Value", "$500,000"),
            ScenarioAssumption("First Lien Balance", "$350,000"),
            ScenarioAssumption("Loan Amount", "$50,000"),
            ScenarioAssumption("Combined LTV (CLTV)", "80.000%"),
            ScenarioAssumption("Credit Score", "780"),
            ScenarioAssumption("Term", "10 years (120 months) fixed"),
            ScenarioAssumption("Occupancy", "Owner-occupied single-family primary residence"),
            ScenarioAssumption("Discounts", "None (No relationship or automatic payment discount)"),
        ),
    ),
    ScenarioSpec(
        scenario_id=CanonicalScenarioId.HELOC.value,
        title="Home Equity Line of Credit (HELOC)",
        short_title="Variable HELOC",
        description="Open-end revolving variable-rate second-lien line of credit.",
        family=ProductFamily.HELOC,
        assumptions=(
            ScenarioAssumption("Loan Purpose", "Revolving line of credit (Second lien)"),
            ScenarioAssumption("Property Value", "$500,000"),
            ScenarioAssumption("First Lien Balance", "$350,000"),
            ScenarioAssumption("Line Amount", "$50,000"),
            ScenarioAssumption("Combined LTV (CLTV)", "80.000%"),
            ScenarioAssumption("Credit Score", "780"),
            ScenarioAssumption("Rate Structure", "Post-introductory variable APR"),
            ScenarioAssumption("Occupancy", "Owner-occupied single-family primary residence"),
            ScenarioAssumption("Discounts", "None (No relationship or automatic payment discount)"),
        ),
    ),
)

SCENARIOS_BY_ID = {spec.scenario_id: spec for spec in CANONICAL_SCENARIO_SPECS}


@dataclass(frozen=True)
class FormattedOffer:
    offer_id: str
    institution_name: str
    institution_charter: int
    source_url: str
    scenario_id: str | None
    family: str
    is_comparable: bool
    separation_reason: str
    note_rate_display: str
    apr_display: str
    points_display: str
    term_display: str
    loan_type_display: str
    conditions_display: str
    freshness_display: str
    effective_display: str
    review_status_display: str
    sort_institution: str
    sort_apr: Decimal | None
    sort_rate: Decimal | None
    sort_freshness: datetime


def format_age(delta: timedelta) -> str:
    if delta.total_seconds() < 0:
        return "current"
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = int(delta.total_seconds() // 3600)
    if hours > 0:
        return f"{hours}h ago"
    minutes = max(1, int(delta.total_seconds() // 60))
    return f"{minutes}m ago"


def format_conditions(offer: PublicOffer) -> str:
    items: list[str] = []
    if offer.headline_is_as_low_as:
        items.append("Advertised as 'as low as'")
    for cond in offer.conditions:
        if cond.kind is ConditionKind.AUTOPAY:
            items.append("Requires automatic payment discount")
        elif cond.kind is ConditionKind.RELATIONSHIP:
            items.append("Requires account relationship")
        elif cond.kind is ConditionKind.TIER:
            items.append("Tier-based pricing")
        elif cond.kind is ConditionKind.DISCOUNT:
            items.append("Special discount applied")
        elif cond.kind is ConditionKind.BORROWER_REQUIREMENT:
            items.append("Borrower eligibility requirement")
        elif cond.kind is ConditionKind.AS_LOW_AS:
            if not offer.headline_is_as_low_as:
                items.append("Tier or qualification condition")
        else:
            items.append(f"Condition: {cond.kind.value.replace('_', ' ')}")
    return ", ".join(items) if items else "None published"


def format_term(offer: PublicOffer) -> str:
    term_months = offer.details.get("term_months")
    if isinstance(term_months, int) and term_months > 0:
        if term_months % 12 == 0:
            return f"{term_months // 12} years ({term_months} mos)"
        return f"{term_months} mos"
    if offer.family is ProductFamily.HELOC:
        draw = offer.details.get("draw_period_months")
        repay = offer.details.get("repayment_period_months")
        if draw or repay:
            return f"Draw: {draw or 'not published'}, Repay: {repay or 'not published'}"
        return "Revolving line"
    return "Not published"


def format_loan_type(offer: PublicOffer) -> str:
    if offer.family is ProductFamily.MORTGAGE:
        rate_type = offer.details.get("rate_type", "fixed")
        type_str = "Fixed-Rate" if rate_type == "fixed" else "Adjustable-Rate (ARM)"
        purpose_val = offer.purpose.value if offer.purpose else "purchase"
        purpose_str = "Purchase" if purpose_val == "purchase" else "Refinance"
        return f"{type_str} Conforming Mortgage ({purpose_str})"
    if offer.family is ProductFamily.HOME_EQUITY_LOAN:
        return "Fixed-Rate Home Equity Loan (Second Lien)"
    if offer.family is ProductFamily.HELOC:
        return "Variable-Rate HELOC (Second Lien)"
    return "Not published"


def classify_and_format_offer(offer: PublicOffer, now: datetime) -> FormattedOffer | None:
    """Return a formatted offer projection or None if stale/withdrawn/denylisted."""
    if now >= offer.valid_until or now - offer.observed_at > WITHDRAW_AFTER:
        return None

    rates_by_path = {r.path: r.value for r in offer.rates}
    note_rate = rates_by_path.get("note_rate")
    apr = rates_by_path.get("apr")
    if apr is None and offer.family is ProductFamily.HELOC:
        apr = rates_by_path.get("details.stated_post_intro_apr") or rates_by_path.get(
            "details.index_rate"
        )

    reasons: list[str] = []
    if apr is None:
        reasons.append(
            "Lacks published APR disclosure (excluded from rankings under Decision D-05)"
        )
    if note_rate is None and offer.family is not ProductFamily.HELOC:
        reasons.append("Missing note rate disclosure")
    if offer.placement is Placement.BROWSE_ONLY:
        if now - offer.observed_at > DEGRADE_AFTER:
            reasons.append("Degraded freshness (observed over 72 hours ago)")
        elif any(a.state is MatchState.UNSTATED for a in offer.assumptions):
            reasons.append("Unstated borrower criteria in source publication")
        elif offer.conditions:
            reasons.append("Conditional discount or relationship requirement")
        else:
            reasons.append("Browse-only publication status")
    if any(a.state is MatchState.CONFLICTS for a in offer.assumptions):
        reasons.append("Borrower requirements conflict with canonical scenario assumptions")

    is_comparable = len(reasons) == 0 and offer.placement is Placement.RANKED
    separation_reason = "; ".join(reasons) if reasons else ""

    note_rate_display = (
        f"As low as {note_rate:.3f}%"
        if (note_rate is not None and offer.headline_is_as_low_as)
        else (f"{note_rate:.3f}%" if note_rate is not None else "Not published")
    )
    apr_display = f"{apr:.3f}%" if apr is not None else "Not published"
    points_display = f"{offer.points:.3f}" if offer.points is not None else "Not published"
    term_display = format_term(offer)
    loan_type_display = format_loan_type(offer)
    conditions_display = format_conditions(offer)

    obs_ny = offer.observed_at.astimezone(EASTERN_TZ)
    age = format_age(now - offer.observed_at)
    freshness_display = f"{obs_ny.strftime('%b %d, %Y %I:%M %p %Z')} ({age})"

    if offer.effective_at:
        eff_ny = offer.effective_at.astimezone(EASTERN_TZ)
        effective_display = eff_ny.strftime("%b %d, %Y")
    else:
        effective_display = "Not published"

    review_status_display = (
        "Reviewed by human" if offer.review_status == "reviewed" else "Automated check"
    )

    scenario_val = offer.scenario_id.value if offer.scenario_id else None

    return FormattedOffer(
        offer_id=offer.offer_id,
        institution_name=offer.institution_name,
        institution_charter=offer.institution_charter,
        source_url=offer.source_url,
        scenario_id=scenario_val,
        family=offer.family.value,
        is_comparable=is_comparable,
        separation_reason=separation_reason,
        note_rate_display=note_rate_display,
        apr_display=apr_display,
        points_display=points_display,
        term_display=term_display,
        loan_type_display=loan_type_display,
        conditions_display=conditions_display,
        freshness_display=freshness_display,
        effective_display=effective_display,
        review_status_display=review_status_display,
        sort_institution=offer.institution_name.lower(),
        sort_apr=apr,
        sort_rate=note_rate,
        sort_freshness=offer.observed_at,
    )


def sort_offers(offers: list[FormattedOffer], sort_key: str) -> list[FormattedOffer]:
    """Sort offers neutrally based on validated parameter. Unknown keys fall back to institution."""
    if sort_key == "institution_desc":
        return sorted(offers, key=lambda o: o.sort_institution, reverse=True)
    if sort_key == "apr":
        return sorted(
            offers,
            key=lambda o: (
                (0, o.sort_apr, o.sort_institution)
                if o.sort_apr is not None
                else (1, Decimal(0), o.sort_institution)
            ),
        )
    if sort_key == "apr_desc":
        return sorted(
            offers,
            key=lambda o: (
                (0, -o.sort_apr, o.sort_institution)
                if o.sort_apr is not None
                else (1, Decimal(0), o.sort_institution)
            ),
        )
    if sort_key == "rate":
        return sorted(
            offers,
            key=lambda o: (
                (0, o.sort_rate, o.sort_institution)
                if o.sort_rate is not None
                else (1, Decimal(0), o.sort_institution)
            ),
        )
    if sort_key == "rate_desc":
        return sorted(
            offers,
            key=lambda o: (
                (0, -o.sort_rate, o.sort_institution)
                if o.sort_rate is not None
                else (1, Decimal(0), o.sort_institution)
            ),
        )
    if sort_key == "freshness":
        return sorted(offers, key=lambda o: (o.sort_freshness, o.sort_institution), reverse=True)
    # Default: neutral institution alphabetical ascending
    return sorted(offers, key=lambda o: o.sort_institution)


def read_snapshot_offers(
    at: datetime | None = None,
) -> tuple[SnapshotInfo | None, tuple[PublicOffer, ...]]:
    """Read public offers from the sealed public snapshot reader only.

    Never opens the operational database or accesses network resources.
    Returns (None, ()) if snapshot is unconfigured, missing, or empty.
    """
    root_cfg: Any = current_app.config.get("SNAPSHOT_ROOT")
    denylist_cfg: Any = current_app.config.get("DENYLIST_PATH")

    if at is None:
        at_cfg = current_app.config.get("SNAPSHOT_CLOCK")
        if isinstance(at_cfg, datetime):
            at = at_cfg

    root = Path(root_cfg) if root_cfg is not None else Path("data/snapshots")
    denylist_path = (
        Path(denylist_cfg)
        if denylist_cfg is not None
        else Path("data/independent/withdrawals.json")
    )

    if not root.is_dir() or not denylist_path.is_file():
        return None, ()

    try:
        reader = SnapshotReader(root=root, denylist_path=denylist_path)
        view = reader.read(at=at)
        return view.info, view.offers
    except (SnapshotError, OSError, ValueError):
        return None, ()

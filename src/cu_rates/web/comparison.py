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
    LoanPurpose,
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
    "apr": "APR (Low to High)",
    "apr_desc": "APR (High to Low)",
    "rate": "Note Rate (Low to High)",
    "rate_desc": "Note Rate (High to Low)",
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
    draw_period_months: int | None = None
    repayment_period_months: int | None = None
    structure_label: str = ""
    intro_rate_display: str = ""


def exact_decimal(value: Decimal, minimum_places: int = 3) -> str:
    """Show a published value exactly: at least three places, never silently rounded."""
    if not value.is_finite():
        raise ValueError("a non-finite value cannot be displayed")
    places = max(minimum_places, -int(value.as_tuple().exponent))
    return f"{abs(value) if value.is_zero() else value:.{places}f}"  # never "-0.000"


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


def format_heloc_structure(draw: int | None, repay: int | None) -> str:
    if draw is not None and repay is not None:
        draw_yrs = f"{draw // 12}Y" if draw % 12 == 0 else f"{draw}m"
        repay_yrs = f"{repay // 12}Y" if repay % 12 == 0 else f"{repay}m"
        return f"{draw_yrs} Draw / {repay_yrs} Repay ({draw}/{repay} mos)"
    if draw is not None:
        return f"{draw} mos draw"
    if repay is not None:
        return f"{repay} mos repay"
    return "Structure not published"


def format_conditions(offer: PublicOffer) -> str:
    items: list[str] = []
    if offer.headline_is_as_low_as:
        items.append("Advertised as 'as low as'")

    # Precise tier qualifications
    for tier in offer.tiers:
        for bound in tier.bounds:
            dim = bound.dimension.value.upper()
            if bound.lower is not None and bound.upper is not None:
                items.append(f"Tier qualification ({dim}: {bound.lower} to {bound.upper})")
            elif bound.lower is not None:
                items.append(f"Tier qualification ({dim} >= {bound.lower})")
            elif bound.upper is not None:
                items.append(f"Tier qualification ({dim} <= {bound.upper})")

    # Flag conflicting scenario assumptions and disclose published bounds
    for assumption in offer.assumptions:
        dim_name = assumption.dimension.value.replace("_", " ")
        if assumption.state is MatchState.CONFLICTS:
            if assumption.stated is not None:
                st = assumption.stated
                if st.exact is not None:
                    val_str = str(st.exact)
                elif st.lower is not None and st.upper is not None:
                    val_str = f"{st.lower} to {st.upper}"
                elif st.lower is not None:
                    val_str = f">= {st.lower}"
                elif st.upper is not None:
                    val_str = f"<= {st.upper}"
                elif st.token is not None:
                    val_str = str(st.token)
                else:
                    val_str = "conflicting bound"
                items.append(f"Conflicts with scenario {dim_name} ({val_str})")
            else:
                items.append(f"Conflicts with scenario {dim_name}")
        elif assumption.stated is not None:
            st = assumption.stated
            if st.lower is not None and st.upper is not None:
                items.append(f"Published {dim_name}: {st.lower} to {st.upper}")
            elif st.lower is not None:
                items.append(f"Published min {dim_name}: {st.lower}")
            elif st.upper is not None:
                items.append(f"Published max {dim_name}: {st.upper}")
            elif st.exact is not None:
                items.append(f"Published {dim_name}: {st.exact}")
            elif st.token is not None:
                items.append(f"Published {dim_name}: {st.token}")

    # Condition kinds as condition disclosures (never asserted requirements)
    for cond in offer.conditions:
        if cond.kind is ConditionKind.AUTOPAY:
            items.append("Autopay discount condition")
        elif cond.kind is ConditionKind.RELATIONSHIP:
            items.append("Account relationship condition")
        elif cond.kind is ConditionKind.TIER:
            if not offer.tiers:
                items.append("Tier pricing condition")
        elif cond.kind is ConditionKind.DISCOUNT:
            items.append("Discount condition")
        elif cond.kind is ConditionKind.BORROWER_REQUIREMENT:
            items.append("Borrower eligibility condition")
        elif cond.kind is ConditionKind.AS_LOW_AS:
            if not offer.headline_is_as_low_as:
                items.append("As-low-as qualification condition")
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
        if draw is not None or repay is not None:
            draw_str = f"{draw} mos" if draw is not None else "not published"
            repay_str = f"{repay} mos" if repay is not None else "not published"
            return f"Draw: {draw_str}, Repay: {repay_str}"
        return "Revolving line"
    return "Not published"


def format_loan_type(offer: PublicOffer) -> str:
    """Format loan type using only verified attributes present in snapshot data."""
    parts: list[str] = []

    rate_type = offer.details.get("rate_type")
    if rate_type == "fixed":
        parts.append("Fixed-Rate")
    elif rate_type == "adjustable":
        parts.append("Adjustable-Rate (ARM)")
    elif (
        offer.family is ProductFamily.HOME_EQUITY_LOAN
        and offer.details.get("kind") == "fixed_equity"
    ):
        parts.append("Fixed-Rate")
    elif offer.family is ProductFamily.HELOC:
        parts.append("Variable-Rate")

    conforming = offer.details.get("conforming_status")
    if conforming == "conforming":
        parts.append("Conforming")
    elif conforming == "jumbo":
        parts.append("Jumbo")
    elif conforming == "high_balance_conforming":
        parts.append("High-Balance Conforming")

    if offer.family is ProductFamily.MORTGAGE:
        parts.append("Mortgage")
    elif offer.family is ProductFamily.HOME_EQUITY_LOAN:
        parts.append("Home Equity Loan")
    elif offer.family is ProductFamily.HELOC:
        parts.append("HELOC")

    purpose_str: str | None = None
    if offer.purpose is not None:
        if offer.purpose is LoanPurpose.PURCHASE:
            purpose_str = "Purchase"
        elif offer.purpose is LoanPurpose.RATE_TERM_REFINANCE:
            purpose_str = "Rate/Term Refinance"
        elif offer.purpose is LoanPurpose.CASH_OUT_REFINANCE:
            purpose_str = "Cash-Out Refinance"
        else:
            purpose_str = offer.purpose.value.replace("_", " ").title()

    lien = offer.details.get("lien_position")
    lien_str: str | None = None
    if lien == "second":
        lien_str = "Second Lien"
    elif lien == "first":
        lien_str = "First Lien"

    if not parts and purpose_str is None and lien_str is None:
        return "Not published"

    base = " ".join(parts) if parts else "Loan"
    qualifiers: list[str] = []
    if purpose_str is not None:
        qualifiers.append(purpose_str)
    if lien_str is not None:
        qualifiers.append(lien_str)

    if qualifiers:
        return f"{base} ({', '.join(qualifiers)})"
    return base


def classify_and_format_offer(offer: PublicOffer, now: datetime) -> FormattedOffer | None:
    """Return a formatted offer projection or None if stale/withdrawn/denylisted/detail-only."""
    if (
        offer.placement is Placement.INSTITUTION_DETAIL_ONLY
        or offer.placement is Placement.NONE
        or now >= offer.valid_until
        or now - offer.observed_at > WITHDRAW_AFTER
    ):
        return None

    rates_by_path = {r.path: r.value for r in offer.rates}
    note_rate = rates_by_path.get("note_rate")
    apr = rates_by_path.get("apr")

    draw_val = offer.details.get("draw_period_months")
    draw_period: int | None = draw_val if isinstance(draw_val, int) else None
    repay_val = offer.details.get("repayment_period_months")
    repay_period: int | None = repay_val if isinstance(repay_val, int) else None
    structure_label = (
        format_heloc_structure(draw_period, repay_period)
        if offer.family is ProductFamily.HELOC
        else ""
    )

    intro_label = ""
    if offer.family is ProductFamily.HELOC:
        # Use ONLY the proven details.post_intro_rate for post-intro figures
        post_intro = rates_by_path.get("details.post_intro_rate")
        stated_apr = rates_by_path.get("details.stated_post_intro_apr")
        # For HELOC, post-intro APR is required for comparison; generic root APR does not apply
        apr = stated_apr
        note_rate = post_intro

        # Disclose intro rate and duration separately and labeled
        intro_rate = rates_by_path.get("details.intro.rate")
        if intro_rate is not None:
            intro_duration = None
            intro_raw = offer.details.get("intro")
            if isinstance(intro_raw, dict):
                dur_val = intro_raw.get("duration_months")
                if isinstance(dur_val, int):
                    intro_duration = dur_val
            if intro_duration is not None:
                intro_label = f"Intro: {exact_decimal(intro_rate)}% ({intro_duration} mos)"
            else:
                intro_label = f"Intro: {exact_decimal(intro_rate)}% (duration not published)"

    # Decision D-05: Rate without APR is excluded from comparison tables entirely
    if apr is None:
        return None

    reasons: list[str] = []
    if note_rate is None and offer.family is not ProductFamily.HELOC:
        reasons.append("Missing note rate disclosure")
    if offer.placement is Placement.BROWSE_ONLY:
        if now - offer.observed_at > DEGRADE_AFTER:
            reasons.append("Degraded freshness (observed over 72 hours ago)")
        elif any(a.state is MatchState.UNSTATED for a in offer.assumptions):
            reasons.append("Unstated borrower criteria in source publication")
        elif offer.conditions:
            cond_reasons: list[str] = []
            for cond in offer.conditions:
                if cond.kind is ConditionKind.BORROWER_REQUIREMENT:
                    cond_reasons.append("Borrower eligibility requirement")
                elif cond.kind is ConditionKind.AUTOPAY:
                    cond_reasons.append("Autopay discount condition")
                elif cond.kind is ConditionKind.RELATIONSHIP:
                    cond_reasons.append("Account relationship condition")
                elif cond.kind is ConditionKind.DISCOUNT:
                    cond_reasons.append("Conditional discount")
                elif cond.kind is ConditionKind.TIER:
                    cond_reasons.append("Tier qualification condition")
                elif cond.kind is ConditionKind.AS_LOW_AS:
                    cond_reasons.append("As-low-as qualification condition")
                else:
                    cond_reasons.append(f"Condition: {cond.kind.value.replace('_', ' ')}")
            for r in dict.fromkeys(cond_reasons):
                reasons.append(r)
        else:
            reasons.append("Browse-only publication status")
    if any(a.state is MatchState.CONFLICTS for a in offer.assumptions):
        reasons.append("Borrower requirements conflict with canonical scenario assumptions")

    is_comparable = len(reasons) == 0 and offer.placement is Placement.RANKED
    separation_reason = "; ".join(reasons) if reasons else ""

    if note_rate is not None:
        base_rate_str = (
            f"As low as {exact_decimal(note_rate)}%"
            if offer.headline_is_as_low_as
            else f"{exact_decimal(note_rate)}%"
        )
        note_rate_display = f"{base_rate_str} ({intro_label})" if intro_label else base_rate_str
    elif intro_label:
        note_rate_display = f"Post-intro not published ({intro_label})"
    else:
        note_rate_display = "Not published"
    apr_display = f"{exact_decimal(apr)}%" if apr is not None else "Not published"
    points_display = (
        f"{exact_decimal(offer.points)}" if offer.points is not None else "Not published"
    )
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
        draw_period_months=draw_period,
        repayment_period_months=repay_period,
        structure_label=structure_label,
        intro_rate_display=intro_label,
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

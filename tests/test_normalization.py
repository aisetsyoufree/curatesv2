"""Offline contract-to-contract normalization tests; no HTML is parsed."""

import json
from collections.abc import Iterable
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from cu_rates.contracts import (
    CONTRACT_VERSION,
    CanonicalScenarioId,
    ExtractedCandidate,
    FixedEquityDetails,
    HelocDetails,
    HtmlLocator,
    LoanPurpose,
    MatchState,
    NormalizedOffer,
    OfferStatus,
    ProductFamily,
    PurchaseMortgageDetails,
    RefinanceMortgageDetails,
    ScenarioDimension,
    evaluate_scenario_match,
)
from cu_rates.normalize import (
    NORMALIZATION_VERSION,
    FieldState,
    NoOfferReason,
    NormalizationError,
    NormalizationResult,
    OfferContext,
    normalize_offer,
)

from .contracts.factories import T0, extraction, provenance

_FIXTURES = Path(__file__).parent / "fixtures" / "seed"
_PURCHASE = CanonicalScenarioId.PURCHASE_30Y_FIXED


def context(target: CanonicalScenarioId | None = None) -> OfferContext:
    return OfferContext(
        offer_id="synthetic-offer",
        institution_charter=12345,
        product_id="synthetic-product",
        source_id="synthetic-source",
        observed_at=T0,
        comparison_target=target,
    )


def candidate(path: str, text: str) -> ExtractedCandidate:
    return ExtractedCandidate(
        schema_version=CONTRACT_VERSION,
        candidate_id=path,
        extraction_run_id="synthetic-run",
        proposed_path=path,
        raw_value=text,
        provenance=provenance(text).model_copy(update={"extraction": extraction("raw.adapter.v1")}),
    )


def inputs(values: dict[str, str]) -> tuple[ExtractedCandidate, ...]:
    return tuple(candidate(path, text) for path, text in values.items())


def purchase(**overrides: str) -> dict[str, str]:
    return {
        "product_family": "mortgage",
        "details.purpose": "purchase",
        "details.term_months": "30 years (360 months)",
        "details.rate_type": "fixed",
        "note_rate": "6.375%",
        "apr": "6.441%",
        **overrides,
    }


def require_offer(result: NormalizationResult) -> NormalizedOffer:
    assert result.offer is not None and result.no_offer_reason is None
    assert result.offer.status is OfferStatus.CANDIDATE
    assert result.offer.published_at is None and result.offer.valid_until is None
    return result.offer


class Seed:
    """Build adapter output by hand; CSS/XPath and hashes cite actual fixture fragments."""

    def __init__(self, case_id: str) -> None:
        catalog = json.loads((_FIXTURES / "expected_outcomes.json").read_text())
        self.case: dict[str, Any] = next(c for c in catalog["cases"] if c["case_id"] == case_id)
        self.body = (_FIXTURES / self.case["fixture"]).read_bytes()
        assert sha256(self.body).hexdigest() == self.case["artifact_id"]

    def cited(
        self,
        path: str,
        text: str,
        selector: str,
        *,
        selected: str | None = None,
        xpath: bool = False,
    ) -> ExtractedCandidate:
        node_text = selected or text
        assert text in node_text
        assert node_text.encode() in self.body
        return ExtractedCandidate(
            schema_version=CONTRACT_VERSION,
            candidate_id=path,
            extraction_run_id=self.case["fetch_run_id"],
            proposed_path=path,
            raw_value=text,
            provenance=provenance(text, artifact_id=self.case["artifact_id"]).model_copy(
                update={
                    "locator": HtmlLocator(
                        selector_type="xpath" if xpath else "css",
                        selector=selector,
                        normalized_text_sha256=sha256(node_text.encode()).hexdigest(),
                    )
                }
            ),
        )

    def row(
        self,
        row: dict[str, Any],
        label: str,
        term: str,
        assumptions: str,
        statements: dict[str, str],
        *,
        mortgage: bool,
    ) -> Iterable[ExtractedCandidate]:
        root = f"#{row['row_id']}"
        assert f'id="{row["row_id"]}"'.encode() in self.body
        cells = {
            "product_family": (label, 1),
            "details.term_months": (term, 2),
            "note_rate": (f"{row['note_rate']}%", 3),
            "apr": (f"{row['apr']}%", 4),
            "points": (row["points"], 5),
        }
        if mortgage:
            cells.update({"details.purpose": (label, 1), "details.rate_type": (label, 1)})
        else:
            cells["details.lien_position"] = (label, 1)
        yield from (
            self.cited(path, text, f"{root} td:nth-child({cell})")
            for path, (text, cell) in cells.items()
        )
        yield from (
            self.cited(path, text, f"{root} td:nth-child(6)", selected=assumptions)
            for path, text in statements.items()
        )


@pytest.mark.parametrize("row_index", [0, 1])
def test_athol_seed_matches_only_the_stated_30_year_row(row_index: int) -> None:
    seed = Seed("athol-style")
    row = seed.case["rows"][row_index]
    months = row["term_months"]
    term = f"{months // 12} years ({months} months)"
    label = "Conventional conforming fixed purchase"
    assumptions = (
        "Owner-occupied single-family primary residence. Conventional conforming purchase. "
        "Property value $500,000. Loan amount $400,000. Loan-to-value 80%. Credit score 780. "
        f"Lock period 30 days. Points {row['points']}. No relationship discount. "
        f"No autopay discount. Term {term}."
    )
    statements = {
        "details.lock_days": "30 days",
        "scenario.occupancy": "Owner-occupied single-family primary residence",
        "scenario.property_type": "single-family",
        "scenario.property_value": "$500,000",
        "scenario.loan_amount": "$400,000",
        "scenario.ltv": "80%",
        "scenario.credit_score": "780",
        "scenario.relationship_discount": "No relationship discount",
        "scenario.autopay_discount": "No autopay discount",
    }
    target = CanonicalScenarioId(row["canonical_scenario"])
    records = tuple(seed.row(row, label, term, assumptions, statements, mortgage=True))
    result = normalize_offer(records, context(target))
    assert result == normalize_offer(reversed(records), context(target))
    offer = require_offer(result)
    assert isinstance(offer.details, PurchaseMortgageDetails)
    assert offer.family.value == row["product_family"] and offer.purpose == row["purpose"]
    assert offer.details.term_months == months
    assert offer.note_rate is not None and offer.note_rate.value == Decimal(row["note_rate"])
    assert offer.apr is not None and offer.apr.value == Decimal(row["apr"])
    assert offer.points is not None and offer.points.value == Decimal(row["points"])
    expected = MatchState.MATCHES if row["ranking"] == "assumptions_match" else MatchState.CONFLICTS
    assert result.scenario_state is expected
    assert evaluate_scenario_match(target, offer.scenario_matches) is expected
    assert {d.dimension: d.state for d in offer.scenario_matches}[
        ScenarioDimension.TERM_MONTHS
    ] is expected
    assert {entry.path for entry in offer.field_provenance} == offer.material_paths()
    for trace in result.traces:
        assert trace.provenance.artifact_id == seed.case["artifact_id"]
        assert trace.provenance.locator == trace.candidate.provenance.locator
        assert trace.provenance.source_text == trace.candidate.raw_value
        assert trace.provenance.extraction.normalization_version == NORMALIZATION_VERSION
        assert trace.provenance.extraction.rule_id
    term_trace = next(t for t in result.traces if t.path == "details.term_months")
    assert term_trace.provenance.extraction.rule_id == "t044.term-months.v1"
    assert term_trace.candidate.raw_value == term
    assert NormalizedOffer.model_validate_json(offer.model_dump_json()) == offer


def test_worcester_seed_second_mortgage_and_non_rate_regions() -> None:
    seed = Seed("worcester-style")
    quote = seed.case["first_mortgage"]
    assert quote["availability"] == "quote_only" and not quote["published_rate"]
    raw_quote = "Call for a quote. This page does not advertise a first-mortgage note rate or APR."
    first = normalize_offer(
        (
            seed.cited("product_family", "First mortgage", "h2:nth-of-type(1)"),
            seed.cited("rate_availability", raw_quote, "h2:nth-of-type(1) + p"),
        ),
        context(),
    )
    assert first.offer is None and first.no_offer_reason is NoOfferReason.NON_PUBLIC_RATE
    url = seed.case["partner_link"]["url"]
    link = normalize_offer(
        (seed.cited("partner_link", url, f"//a[@href='{url}']/@href", xpath=True),), context()
    )
    assert not seed.case["partner_link"]["is_rate"]
    assert link.offer is None and link.no_offer_reason is NoOfferReason.PARTNER_LINK
    assert link.traces[0].candidate.raw_value == url

    row = seed.case["second_mortgage_table"]["rows"][0]
    assumptions = (
        "Owner-occupied single-family primary residence. Property value $500,000. "
        "Existing first lien $350,000. Second-lien loan $50,000. Combined loan-to-value 80%. "
        "Credit score 780. Ten-year fixed term (120 months). No relationship discount."
    )
    result = normalize_offer(
        seed.row(
            row,
            "Closed-end fixed second mortgage",
            "10 years (120 months)",
            assumptions,
            {
                "scenario.occupancy": "Owner-occupied single-family primary residence",
                "scenario.property_type": "single-family",
                "scenario.property_value": "$500,000",
                "scenario.first_lien_balance": "$350,000",
                "scenario.loan_amount": "$50,000",
                "scenario.cltv": "80%",
                "scenario.credit_score": "780",
                "scenario.relationship_discount": "No relationship discount",
            },
            mortgage=False,
        ),
        context(CanonicalScenarioId(row["canonical_scenario"])),
    )
    offer = require_offer(result)
    assert isinstance(offer.details, FixedEquityDetails)
    assert offer.details.term_months == row["term_months"]
    assert offer.details.lien_position.value == row["lien"]
    assert offer.family.value == row["product_family"] and offer.purpose is None
    assert offer.note_rate is not None and offer.note_rate.value == Decimal(row["note_rate"])
    assert offer.apr is not None and offer.apr.value == Decimal(row["apr"])
    assert result.scenario_state is MatchState.MATCHES


def test_stoughton_seed_produces_no_offer_from_any_consumer_row() -> None:
    seed = Seed("stoughton-style")
    labels = ["New vehicle loan", "Used vehicle loan", "Unsecured personal loan"]
    for row, label in zip(seed.case["rows"], labels, strict=True):
        root = f"#{row['row_id']}"
        result = normalize_offer(
            (
                seed.cited("product_family", label, f"{root} td:nth-child(1)"),
                seed.cited("note_rate", f"{row['note_rate']}%", f"{root} td:nth-child(3)"),
                seed.cited("apr", f"{row['apr']}%", f"{root} td:nth-child(4)"),
            ),
            context(_PURCHASE),
        )
        assert result.offer is None and result.no_offer_reason is NoOfferReason.OUT_OF_SCOPE
        assert (
            next(t.value for t in result.traces if t.path == "product_family")
            == row["product_family"]
        )
    assert seed.case["mortgage_offers_from_this_page"] == 0
    assert seed.case["institution_offering"] == "not_inferred"


@pytest.mark.parametrize(
    ("text", "state"),
    [
        (None, FieldState.MISSING),
        ("null", FieldState.NULL),
        ("not disclosed", FieldState.UNKNOWN),
        ("N/A", FieldState.NOT_APPLICABLE),
        ("0.000", FieldState.STATED),
    ],
)
def test_distinct_points_states(text: str | None, state: FieldState) -> None:
    values = purchase()
    if text is not None:
        values["points"] = text
    result = normalize_offer(inputs(values), context(_PURCHASE))
    offer = require_offer(result)
    assert result.field_state("points") is state
    if state is FieldState.STATED:
        assert offer.points is not None and offer.points.value == Decimal("0.000")
        assert offer.points.provenance.source_text == "0.000"
    else:
        assert offer.points is None
    if text is not None:
        assert next(t.candidate.raw_value for t in result.traces if t.path == "points") == text


@pytest.mark.parametrize(
    ("raw", "months"),
    [
        ("30 years (360 months)", 360),
        ("ten-year fixed term", 120),
        ("Thirty-year", 360),
        ("15 months", 15),
    ],
)
def test_terms_preserve_explicit_conversion(raw: str, months: int) -> None:
    result = normalize_offer(inputs(purchase(**{"details.term_months": raw})), context())
    details = require_offer(result).details
    assert isinstance(details, PurchaseMortgageDetails) and details.term_months == months
    trace = next(t for t in result.traces if t.path == "details.term_months")
    assert trace.candidate.raw_value == raw and trace.value == months
    assert trace.provenance.extraction.rule_id == "t044.term-months.v1"


def test_exact_decimals_and_provenance_survive_normalization() -> None:
    records = inputs(purchase(note_rate="6.12345%", apr="6.12346%"))
    result = normalize_offer(records, context())
    offer = require_offer(result)
    assert offer.note_rate is not None and offer.apr is not None
    assert offer.note_rate.value == Decimal("6.12345")
    assert offer.apr.value - offer.note_rate.value == Decimal("0.00001")
    assert offer.note_rate.provenance.extraction.rule_id == "t044.percent.v1"
    trace = next(item for item in result.traces if item.path == "note_rate")
    assert trace.candidate.provenance.extraction.rule_id == "raw.adapter.v1"
    assert trace.provenance.source_text == trace.candidate.raw_value == "6.12345%"


@pytest.mark.parametrize(
    ("path", "bad", "message"),
    [
        ("note_rate", "6.4% to 7.1%", "decimal amount"),
        ("note_rate", "NaN", "decimal amount"),
        ("note_rate", "6e0", "decimal amount"),
        ("apr", "6.0%", "APR is below"),
        ("details.term_months", "30 years (180 months)", "count conflict"),
        ("details.term_months", "2.5 years", "whole-number term"),
        ("details.term_months", "30 weeks", "whole-number term"),
        ("points", "0.0000001", "decimal places"),
        ("apy", "7.000%", "unsupported field"),
        ("status", "approved", "unsupported field"),
        ("scenario.credit_score", "830 to 780", "lower bound"),
        ("effective_at", "2026-11-15", "explicit UTC offset"),
    ],
)
def test_rejects_conflicting_or_malformed_inputs(path: str, bad: str, message: str) -> None:
    with pytest.raises(NormalizationError, match=message) as error:
        normalize_offer(inputs(purchase(**{path: bad})), context(_PURCHASE))
    assert error.value.path == path


def test_duplicate_fields_forged_source_fragments_and_mixed_runs_are_rejected() -> None:
    records = inputs(purchase())
    with pytest.raises(NormalizationError, match="duplicate"):
        normalize_offer((*records, candidate("purpose", "rate_term_refinance")), context())
    rec = candidate("points", "0.000")
    with pytest.raises(NormalizationError, match="cited source fragment"):
        normalize_offer((*records, rec.model_copy(update={"raw_value": "1.500"})), context())
    with pytest.raises(NormalizationError, match="mix extraction runs"):
        normalize_offer(
            (*records, rec.model_copy(update={"extraction_run_id": "other"})), context()
        )


def test_scenario_target_does_not_fill_unstated_assumptions_and_conditions_are_checked() -> None:
    values = purchase(
        **{
            "scenario.credit_score": "780 to 820",
            "scenario.ltv": "<= 75%",
            "conditions.a.borrower_requirement.credit_score": ">= 780",
            "conditions.b.autopay": "autopay discount required",
            "conditions.c.other": "See membership rules",
            "conditions.d.as_low_as": "As low as",
        }
    )
    result = normalize_offer(inputs(values), context(_PURCHASE))
    offer = require_offer(result)
    assert offer.scenario_id is _PURCHASE and result.scenario_state is MatchState.CONFLICTS
    matches = {entry.dimension: entry for entry in offer.scenario_matches}
    assert matches[ScenarioDimension.CREDIT_SCORE].state is MatchState.MATCHES
    assert matches[ScenarioDimension.LTV].state is MatchState.CONFLICTS
    assert matches[ScenarioDimension.LOCK_DAYS].state is MatchState.UNSTATED
    assert matches[ScenarioDimension.LOCK_DAYS].source_text is None
    assert {c.condition_id: c.scenario_state for c in offer.conditions} == {
        "a": MatchState.MATCHES,
        "b": MatchState.CONFLICTS,
        "c": MatchState.UNSTATED,
        "d": MatchState.UNSTATED,
    }
    assert offer.headline_is_as_low_as
    assert {entry.path for entry in offer.field_provenance} == offer.material_paths()
    target_only = normalize_offer(inputs(purchase()), context(_PURCHASE))
    assert target_only.scenario_state is MatchState.UNSTATED
    with pytest.raises(NormalizationError, match="competing statements"):
        normalize_offer(
            inputs(purchase(**{"scenario.term_months": "15 years"})), context(_PURCHASE)
        )


@pytest.mark.parametrize(
    "purpose", [LoanPurpose.RATE_TERM_REFINANCE, LoanPurpose.CASH_OUT_REFINANCE]
)
def test_refinance_has_explicit_purpose_and_preserves_arm_terms(purpose: LoanPurpose) -> None:
    result = normalize_offer(
        inputs(
            purchase(
                **{
                    "details.purpose": purpose.value,
                    "details.rate_type": "ARM",
                    "details.arm.initial_period_months": "5 years",
                    "details.arm.margin": "+2.250%",
                }
            )
        ),
        context(),
    )
    offer = require_offer(result)
    assert isinstance(offer.details, RefinanceMortgageDetails)
    assert offer.purpose is purpose and offer.family is ProductFamily.MORTGAGE
    assert offer.details.arm is not None and offer.details.arm.initial_period_months == 60
    assert offer.details.arm.margin is not None and offer.details.arm.margin.value == Decimal(
        "2.25"
    )
    assert offer.details.arm.adjustment_interval_months is None
    assert offer.scenario_id is None and result.scenario_state is None


def test_heloc_keeps_intro_post_intro_and_formula_components_separate() -> None:
    values = {
        "product_family": "heloc",
        "details.lien_position": "second-lien",
        "details.intro.rate": "2.125%",
        "details.intro.duration_months": "6 months",
        "details.index_name": "Prime",
        "details.index_rate": "7.500%",
        "details.margin": "-0.250%",
        "details.floor": "4.000%",
        "details.draw_period_months": "ten years",
        "details.repayment_period_months": "120 months",
        "points": "not applicable",
    }
    result = normalize_offer(inputs(values), context(CanonicalScenarioId.HELOC))
    offer = require_offer(result)
    assert isinstance(offer.details, HelocDetails)
    assert offer.note_rate is None and offer.apr is None and offer.purpose is None
    assert offer.details.intro is not None
    assert offer.details.intro.rate.value == Decimal("2.125")
    assert offer.details.intro.duration_months == 6
    assert offer.details.post_intro().rate == Decimal("7.250")
    assert offer.details.post_intro().rule_id
    assert offer.details.draw_period_months == offer.details.repayment_period_months == 120
    assert result.field_state("points") is FieldState.NOT_APPLICABLE
    with pytest.raises(NormalizationError, match="APR conflicts with the formula"):
        normalize_offer(
            inputs({**values, "details.stated_post_intro_apr": "8.000%"}),
            context(CanonicalScenarioId.HELOC),
        )


def test_no_offer_when_no_usable_rate_and_explicit_no_rate_cannot_be_overridden() -> None:
    assert normalize_offer([], context()).no_offer_reason is NoOfferReason.NO_CANDIDATES
    result = normalize_offer(inputs({"product_family": "heloc", "details.floor": "4%"}), context())
    assert result.offer is None and result.no_offer_reason is NoOfferReason.NO_RATE
    with pytest.raises(NormalizationError, match="contradicts non-public"):
        normalize_offer(inputs(purchase(rate_availability="quote_only")), context())
    with pytest.raises(NormalizationError, match="declared without a rate"):
        normalize_offer(
            inputs({"product_family": "heloc", "rate_availability": "public_rate"}), context()
        )


@pytest.mark.parametrize(
    ("path", "value", "dimension", "state"),
    [
        ("details.lien_position", "first lien", "lien_position", MatchState.CONFLICTS),
        ("details.lien_position", "unknown", "lien_position", MatchState.UNSTATED),
        ("details.max_cltv", "75%", "max_cltv", MatchState.CONFLICTS),
        ("details.min_amount", "$60,000", "min_amount", MatchState.CONFLICTS),
        ("details.max_amount", "$49,999", "max_amount", MatchState.CONFLICTS),
        ("scenario.property_value", "$600,000", "property_value", MatchState.CONFLICTS),
    ],
)
def test_additional_equity_assumptions_are_not_silently_dropped(
    path: str, value: str, dimension: str, state: MatchState
) -> None:
    values = {
        "product_family": "home_equity_loan",
        "note_rate": "7.125%",
        "details.term_months": "ten years",
        "details.lien_position": "second lien",
        "scenario.property_value": "$500,000",
        "scenario.first_lien_balance": "$350,000",
        "scenario.loan_amount": "$50,000",
        "scenario.cltv": "80%",
        "scenario.occupancy": "owner-occupied primary residence",
        "scenario.property_type": "single-family",
        "scenario.credit_score": "780",
        "scenario.relationship_discount": "no relationship discount",
        path: value,
    }
    result = normalize_offer(inputs(values), context(CanonicalScenarioId.FIXED_HOME_EQUITY_10Y))
    offer = require_offer(result)
    check = next(check for check in result.scenario_checks if check.dimension == dimension)
    assert result.scenario_state is state and check.state is state
    assert check.provenance is not None and check.provenance.source_text == value
    if state is MatchState.CONFLICTS:
        condition = next(
            c for c in offer.conditions if c.condition_id == f"t044.scenario.{dimension}"
        )
        assert condition.scenario_state is MatchState.CONFLICTS
        assert condition.provenance == check.provenance
        assert f"conditions.{condition.condition_id}" in offer.material_paths()


def test_heloc_rejects_impossible_formula_even_with_partial_disclosures() -> None:
    with pytest.raises(NormalizationError, match="conflicts with the formula or its bounds"):
        normalize_offer(
            inputs(
                {
                    "product_family": "heloc",
                    "details.stated_post_intro_apr": "6%",
                    "details.floor": "7%",
                }
            ),
            context(),
        )

    with pytest.raises(NormalizationError, match="derived rate is outside bounds"):
        normalize_offer(
            inputs(
                {
                    "product_family": "heloc",
                    "details.index_name": "Prime",
                    "details.index_rate": "99%",
                    "details.margin": "+2%",
                    "details.floor": "0%",
                }
            ),
            context(),
        )


def test_owner_occupied_without_primary_residence_is_not_a_match() -> None:
    result = normalize_offer(
        inputs(purchase(**{"scenario.occupancy": "owner-occupied"})), context(_PURCHASE)
    )
    offer = require_offer(result)
    occupancy = next(
        m for m in offer.scenario_matches if m.dimension is ScenarioDimension.OCCUPANCY
    )
    assert occupancy.state is MatchState.UNSTATED
    check = next(c for c in result.scenario_checks if c.dimension == "occupancy")
    assert check.provenance is not None and check.provenance.source_text == "owner-occupied"

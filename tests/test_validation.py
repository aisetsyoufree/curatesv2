"""Regression checks for the standalone validator using fictional evidence only."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from cu_rates.contracts import (
    APR_RULE_ID,
    CanonicalScenarioId,
    ConditionKind,
    DimensionMatch,
    ExtractedCandidate,
    FieldEvidence,
    IntroRate,
    LienPosition,
    MatchState,
    NormalizedOffer,
    OfferCondition,
    OfferStatus,
    ProductFamily,
    RateTier,
    ScenarioDimension,
    TierBound,
    TierDimension,
    ValidationResult,
    evaluate_publication,
    offer_fingerprints,
)
from cu_rates.contracts.publication import ReasonCode
from cu_rates.normalize import normalize_offer
from cu_rates.validation import (
    RULE_IDS,
    VALIDATOR,
    ValidationContext,
    ValidationRun,
    validate_offer,
)

from .contracts.factories import (
    ARTIFACT_ID,
    EVALUATED_AT,
    SV,
    T0,
    equity_offer,
    fetch_run,
    heloc_details,
    heloc_offer,
    points,
    product,
    provenance,
    purchase_offer,
    rate,
    rate_source,
    raw_artifact,
    refinance_offer,
    validation_policy,
)
from .contracts.factories import context as publication_context
from .test_normalization import Seed
from .test_normalization import context as normalization_context

_OTHER = "12" * 32
_PURCHASE = CanonicalScenarioId.PURCHASE_30Y_FIXED


def draft(**overrides: Any) -> NormalizedOffer:
    offer = purchase_offer(
        **{
            "status": OfferStatus.CANDIDATE,
            "effective_at": None,
            "scenario_id": None,
            "scenario_matches": (),
            **overrides,
        }
    )
    if "field_provenance" in overrides:
        return offer
    sources = {
        "details.term_months": "30 years",
        "details.rate_type": "fixed",
        "details.lock_days": "30 days",
    }
    sources.update(
        {
            f"conditions.{item.condition_id}": item.provenance.source_text
            for item in offer.conditions
        }
    )
    sources.update({f"tiers.{item.tier_id}.bounds": "<=80%" for item in offer.tiers})
    return material_evidence(offer, sources)


def material_evidence(offer: NormalizedOffer, sources: dict[str, str]) -> NormalizedOffer:
    evidence = {path: provenance(text) for path, text in sources.items()}
    evidence.update(
        {
            f"conditions.{condition.condition_id}": condition.provenance
            for condition in offer.conditions
        }
    )
    return offer.model_copy(
        update={
            "field_provenance": tuple(
                FieldEvidence(path=path, provenance=evidence[path])
                for path in sorted(offer.material_paths() & evidence.keys())
            ),
        }
    )


def evidence_context(**overrides: Any) -> ValidationContext:
    return ValidationContext(
        **{
            "evaluated_at": EVALUATED_AT,
            "source": rate_source(),
            "artifacts": (raw_artifact(),),
            "fetch_runs": (fetch_run(),),
            **overrides,
        }
    )


def claim(
    path: str, value: str, artifact_id: str = ARTIFACT_ID, suffix: str = "a"
) -> ExtractedCandidate:
    return ExtractedCandidate(
        schema_version=SV,
        candidate_id=f"claim-{suffix}",
        extraction_run_id=f"extract-{suffix}",
        proposed_path=path,
        raw_value=value,
        provenance=provenance(value, artifact_id),
    )


def result(run: ValidationRun, rule: str) -> ValidationResult:
    return next(item for item in run.results if item.rule_id == rule)


def fail(run: ValidationRun, rule: str, reason: str) -> None:
    assert run.quarantine is not None
    assert not result(run, rule).passed
    assert reason in (result(run, rule).detail or "")
    assert any(item.result == result(run, rule) for item in run.quarantine.reasons)
    assert "quarantined" in run.comparison_blockers


def test_clean_receipt_covers_all_rules_and_handoff_is_exact() -> None:
    candidate = draft(scenario_id=_PURCHASE, scenario_matches=purchase_offer().scenario_matches)
    run = validate_offer(candidate, evidence_context(product=product()))
    assert tuple(item.rule_id for item in run.results) == RULE_IDS
    assert all(
        item.passed and item.checked_paths and item.validator == VALIDATOR for item in run.results
    )
    assert {item.fingerprints for item in run.results} == {offer_fingerprints(candidate)}
    assert run.quarantine is None and not run.comparison_blockers
    approved = candidate.model_copy(update={"status": OfferStatus.APPROVED})
    accepted = run.for_publication(approved)
    assert tuple(item.rule_id for item in accepted) == (APR_RULE_ID, "t045.field-evidence")
    decision = evaluate_publication(
        approved,
        publication_context(
            approved,
            validations=accepted,
            validation_policy=validation_policy(required_rules=("t045.field-evidence",)),
        ),
    )
    assert ReasonCode.VALIDATION_UNBOUND not in decision.reasons
    assert ReasonCode.VALIDATIONS_MISSING not in decision.reasons
    assert run.for_publication(draft(apr=rate("6.500"))) == ()
    modified = candidate.model_copy(
        update={"supporting_evidence": (provenance("different table"),)}
    )
    assert offer_fingerprints(modified) == offer_fingerprints(candidate)
    assert run.for_publication(modified) == ()
    assert (
        run.for_publication(candidate.model_copy(update={"valid_until": T0 + timedelta(days=2)}))
        == ()
    )


@pytest.mark.parametrize("in_tier", [False, True])
def test_apr_checks_headline_and_noncovering_tiers(in_tier: bool) -> None:
    if in_tier:
        tier = RateTier(
            tier_id="other-tier",
            bounds=(TierBound(dimension=TierDimension.LTV, upper=Decimal("60")),),
            note_rate=rate("7.000"),
            apr=rate("6.990"),
        )
        offer = draft(tiers=(tier,))
        path = "tiers.other-tier.apr"
    else:
        offer = draft(apr=rate("6.100"))
        path = "apr"
    run = validate_offer(offer, evidence_context())
    fail(run, APR_RULE_ID, "APR is below its note rate")
    assert path in result(run, APR_RULE_ID).checked_paths
    assert run.for_publication(offer) == ()
    assert run.quarantine is not None
    evidence = next(
        item.evidence for item in run.quarantine.reasons if item.result.rule_id == APR_RULE_ID
    )
    assert {item.source_text for item in evidence} == (
        {"7.000%", "6.990%"} if in_tier else {"6.125%", "6.100%"}
    )


def test_product_family_and_lien_cannot_be_inferred() -> None:
    run = validate_offer(draft(), evidence_context(product=product(family=ProductFamily.HELOC)))
    fail(run, "t045.product-shape", "product identity, charter or family disagrees")
    offer = equity_offer(status=OfferStatus.CANDIDATE, effective_at=None)
    offer = material_evidence(
        offer, {"details.term_months": "10 years", "details.lien_position": "second"}
    )
    alternate = offer.model_copy(
        update={"details": offer.details.model_copy(update={"lien_position": LienPosition.UNKNOWN})}
    )
    fail(
        validate_offer(alternate, evidence_context()),
        "t045.product-shape",
        "lien position is unknown",
    )
    assert validate_offer(offer, evidence_context()).quarantine is None
    refi = refinance_offer(status=OfferStatus.CANDIDATE, effective_at=None)
    refi = material_evidence(
        refi,
        {
            "details.term_months": "30 years",
            "details.rate_type": "fixed",
            "details.lock_days": "30 days",
            "details.purpose": "rate_term_refinance",
        },
    )
    assert validate_offer(refi, evidence_context()).quarantine is None


@pytest.mark.parametrize(
    ("dimension", "actual", "text"),
    [
        (ScenarioDimension.POINTS, points("0.125"), "0 points"),
        (ScenarioDimension.POINTS, None, "0 points"),
        (ScenarioDimension.LTV, points("0"), "80 months"),
    ],
)
def test_comparison_claims_do_not_override_modeled_values_or_units(
    dimension: ScenarioDimension, actual: object, text: str
) -> None:
    item = next(m for m in purchase_offer().scenario_matches if m.dimension is dimension)
    item = item.model_copy(update={"source_text": text, "provenance": provenance(text)})
    offer = draft(scenario_id=_PURCHASE, scenario_matches=(item,), points=actual)
    fail(validate_offer(offer, evidence_context()), "t045.scenario-consistency", dimension.value)


def test_valid_nonzero_points_are_browse_only_not_changed_into_zero() -> None:
    offer = draft(
        scenario_id=_PURCHASE,
        points=points("0.125"),
        scenario_matches=(
            DimensionMatch(
                dimension=ScenarioDimension.POINTS,
                state=MatchState.CONFLICTS,
                source_text="0.125 points",
            ),
        ),
    )
    run = validate_offer(offer, evidence_context())
    assert run.quarantine is None
    assert "modeled_scenario_conflict" in run.comparison_blockers
    assert offer.points is not None and offer.points.value == Decimal("0.125")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"repayment_period_months": None}, "repayment_period_months"),
        ({"intro": IntroRate(rate=rate("3.000"), duration_months=121)}, "intro.duration_months"),
        ({"stated_post_intro_apr": rate("7.900")}, "post-intro rate conflicts"),
    ],
)
def test_heloc_periods_and_post_intro_must_be_supported(
    changes: dict[str, object], reason: str
) -> None:
    offer = heloc_offer(heloc_details(), status=OfferStatus.CANDIDATE, effective_at=None)
    clean = material_evidence(
        offer,
        {
            "details.index_name": "Prime",
            "details.lien_position": "second",
            "details.draw_period_months": "10 years",
            "details.repayment_period_months": "20 years",
        },
    )
    assert validate_offer(clean, evidence_context()).quarantine is None
    modified = heloc_offer(
        heloc_details(**changes), status=OfferStatus.CANDIDATE, effective_at=None
    )
    fail(validate_offer(modified, evidence_context()), "t045.heloc-periods", reason)


@pytest.mark.parametrize(
    ("hours", "expiry_hours", "reason"),
    [
        (-1, 96, "future"),
        (72, 96, ""),
        (73, 96, "degradation"),
        (96, 96, "expiry must follow"),
        (97, 96, "withdrawal"),
        (1, None, "expiry is missing"),
        (1, 97, "96-hour validity"),
    ],
)
def test_freshness_boundaries(hours: int, expiry_hours: int | None, reason: str) -> None:
    now = T0 + timedelta(hours=hours)
    expiry = None if expiry_hours is None else T0 + timedelta(hours=expiry_hours)
    run = validate_offer(draft(valid_until=expiry), evidence_context(evaluated_at=now))
    if reason:
        fail(run, "t045.freshness", reason)
    else:
        assert result(run, "t045.freshness").passed and run.quarantine is None


def test_supporting_evidence_cannot_replace_material_field_evidence() -> None:
    offer = draft(field_provenance=())
    run = validate_offer(offer, evidence_context())
    fail(run, "t045.field-evidence", "details.term_months")
    assert result(run, "t045.capture-binding").passed


def test_note_only_offer_handoff_does_not_invent_an_apr_result() -> None:
    offer = draft(apr=None, scenario_id=_PURCHASE)
    run = validate_offer(offer, evidence_context())
    assert run.quarantine is None
    assert "headline_apr_missing" in run.comparison_blockers
    assert [item.rule_id for item in run.for_publication(offer)] == ["t045.field-evidence"]


@pytest.mark.parametrize(
    ("artifact_edit", "run_edit", "offer_edit", "reason"),
    [
        ({}, {"source_id": "other-source"}, {}, "successful completed fetch"),
        (
            {},
            {"status": "failed", "error": "synthetic failure", "artifact_ids": ()},
            {},
            "successful completed fetch",
        ),
        ({"source_url": "https://different.example/rates"}, {}, {}, "URL differs"),
        ({"media_type": "application/pdf"}, {}, {}, "locator is incompatible"),
        ({"http_status": 304}, {}, {}, "direct HTTP 200"),
        ({}, {}, {"observed_at": T0 + timedelta(minutes=2)}, "not anchored"),
    ],
)
def test_capture_binding_rejects_wrong_fetch_even_with_valid_looking_provenance(
    artifact_edit: dict[str, Any],
    run_edit: dict[str, Any],
    offer_edit: dict[str, Any],
    reason: str,
) -> None:
    ctx = evidence_context(
        artifacts=(raw_artifact(**artifact_edit),), fetch_runs=(fetch_run(**run_edit),)
    )
    run = validate_offer(draft(**offer_edit), ctx)
    fail(run, "t045.capture-binding", reason)


def test_fresh_rate_does_not_mask_stale_rate_in_another_artifact() -> None:
    old_capture = T0 - timedelta(hours=72)
    older = raw_artifact(_OTHER, fetched_at=old_capture, fetch_run_id="older")
    old_run = fetch_run(_OTHER, run_id="older", started_at=old_capture, finished_at=old_capture)
    old_apr = rate("6.250").model_copy(update={"provenance": provenance("6.250%", _OTHER)})
    context = evidence_context(artifacts=(raw_artifact(), older), fetch_runs=(fetch_run(), old_run))
    run = validate_offer(draft(apr=old_apr), context)
    assert result(run, "t045.freshness").passed
    fail(run, "t045.capture-binding", "a rate capture exceeds the degradation threshold")


def test_condition_and_tier_completeness() -> None:
    condition = OfferCondition(
        condition_id="advertised-minimum",
        kind=ConditionKind.AS_LOW_AS,
        description="As low as 6.125%",
        provenance=provenance("As low as 6.125%"),
        scenario_state=MatchState.UNSTATED,
    )
    tier = RateTier(
        tier_id="published-tier",
        bounds=(TierBound(dimension=TierDimension.LTV, upper=Decimal("80")),),
        note_rate=rate("6.125"),
        apr=rate("6.250"),
    )
    valid = draft(headline_is_as_low_as=True, conditions=(condition,), tiers=(tier,))
    assert validate_offer(valid, evidence_context()).quarantine is None
    no_tier = draft(headline_is_as_low_as=True, conditions=(condition,))
    fail(
        validate_offer(no_tier, evidence_context()),
        "t045.condition-tier-completeness",
        "requires a labeled tier",
    )
    wrong_units = tier.model_copy(
        update={
            "bounds": (TierBound(dimension=TierDimension.CREDIT_SCORE, lower=Decimal("770.5")),)
        }
    )
    misleading = valid.model_copy(update={"tiers": (wrong_units,)})
    fail(
        validate_offer(misleading, evidence_context()),
        "t045.condition-tier-completeness",
        "invalid units",
    )
    forged_bound = tier.model_copy(
        update={"bounds": (TierBound(dimension=TierDimension.LTV, upper=Decimal("90")),)}
    )
    misleading = valid.model_copy(update={"tiers": (forged_bound,)})
    fail(
        validate_offer(misleading, evidence_context()),
        "t045.condition-tier-completeness",
        "bounds disagree",
    )
    invented_description = condition.model_copy(update={"description": "Applies to every member"})
    misleading = draft(
        headline_is_as_low_as=True, conditions=(invented_description,), tiers=(tier,)
    )
    fail(
        validate_offer(misleading, evidence_context()),
        "t045.condition-tier-completeness",
        "not its stated",
    )


def test_cross_capture_contradictions_and_replay_are_evidence_bound() -> None:
    offer = draft()
    first = claim("apr", "6.250%")
    same = claim("apr", "6.25000%", _OTHER, "b")
    conflict = claim("apr", "6.750%", _OTHER, "b")
    artifacts = (raw_artifact(), raw_artifact(_OTHER))
    runs = (fetch_run(ARTIFACT_ID, _OTHER),)
    clean = evidence_context(artifacts=artifacts, fetch_runs=runs, candidates=(first, same))
    assert validate_offer(offer, clean).quarantine is None
    changed = clean.model_copy(update={"candidates": (first, conflict)})
    run = validate_offer(offer, changed)
    fail(run, "t045.field-contradiction", "conflicting values")
    assert run.quarantine is not None
    reason = next(
        item for item in run.quarantine.reasons if item.result.rule_id == "t045.field-contradiction"
    )
    assert {item.artifact_id for item in reason.evidence} == {ARTIFACT_ID, _OTHER}
    assert {item.source_text for item in reason.evidence} == {"6.250%", "6.750%", "6.125%"}
    reordered = changed.model_copy(
        update={"artifacts": tuple(reversed(artifacts)), "candidates": (conflict, first, first)}
    )
    assert validate_offer(offer, reordered) == run
    assert ValidationRun.model_validate_json(run.model_dump_json()) == run
    runs_by_id = {
        item.quarantine.quarantine_id
        for item in (run, validate_offer(offer, changed))
        if item.quarantine
    }
    assert len(runs_by_id) == 1
    assert validate_offer(offer, clean).run_id != run.run_id
    later = changed.model_copy(update={"evaluated_at": EVALUATED_AT + timedelta(minutes=5)})
    replay = validate_offer(offer, later)
    assert replay.quarantine is not None and replay.run_id != run.run_id
    assert replay.quarantine.quarantine_id == run.quarantine.quarantine_id


def test_many_invalid_claims_preserve_all_reasons_without_overflowing_the_contract() -> None:
    candidates = tuple(
        claim(f"details.unknown_{'a' * 278}_{index}", "not a financial field", suffix=str(index))
        for index in range(15)
    )
    run = validate_offer(draft(), evidence_context(candidates=candidates))
    fail(run, "t045.field-contradiction", "complete quarantine findings")
    assert run.quarantine is not None
    finding = next(
        item for item in run.quarantine.reasons if item.result.rule_id == "t045.field-contradiction"
    )
    assert {item.path for item in finding.findings} == {item.proposed_path for item in candidates}


@pytest.mark.parametrize(
    ("text", "passes"), [("0 points", False), ("unknown", True), ("N/A", True)]
)
def test_unknown_and_not_applicable_do_not_overwrite_explicit_zero(text: str, passes: bool) -> None:
    ctx = evidence_context(candidates=(claim("points", text),))
    assert (
        result(validate_offer(draft(points=None), ctx), "t045.field-contradiction").passed is passes
    )
    assert (
        result(validate_offer(draft(points=points("0")), ctx), "t045.field-contradiction").passed
        is not passes
    )


def test_effective_date_cannot_be_future_or_borrowed_from_unrelated_support() -> None:
    date = T0 - timedelta(days=1)
    item = claim("effective_at", date.isoformat())
    offer = draft(effective_at=date, supporting_evidence=(item.provenance,))
    ctx = evidence_context(candidates=(item,))
    assert validate_offer(offer, ctx).quarantine is None
    fail(
        validate_offer(offer, evidence_context()),
        "t045.effective-date",
        "field-bound source candidate",
    )
    future = T0 + timedelta(minutes=30)  # before evaluation, but not yet in force at observation
    future_claim = claim("effective_at", future.isoformat())
    offer = draft(effective_at=future, supporting_evidence=(future_claim.provenance,))
    run = validate_offer(offer, evidence_context(candidates=(future_claim,)))
    fail(run, "t045.effective-date", "later than the observed rate")


def test_seed_fifteen_year_row_stays_browse_only_and_cannot_claim_thirty_year_fit() -> None:
    seed = Seed("athol-style")
    row = seed.case["rows"][1]
    term = f"{row['term_months'] // 12} years ({row['term_months']} months)"
    candidates = tuple(
        seed.row(row, "Conventional conforming fixed purchase", term, "", {}, mortgage=True)
    )
    output = normalize_offer(candidates, normalization_context(_PURCHASE))
    assert output.offer is not None and row["ranking"] != "assumptions_match"
    offer = output.offer.model_copy(update={"valid_until": T0 + timedelta(days=2)})
    artifact = raw_artifact(
        seed.case["artifact_id"], source_url=seed.case["source_url"], byte_length=len(seed.body)
    )
    context = evidence_context(
        source=rate_source(source_id=offer.source_id, url=artifact.source_url),
        artifacts=(artifact,),
        fetch_runs=(fetch_run(artifact.artifact_id, source_id=offer.source_id),),
        candidates=candidates,
    )
    checked = validate_offer(offer, context)
    assert checked.quarantine is None
    assert "modeled_scenario_conflict" in checked.comparison_blockers
    fake = next(m for m in offer.scenario_matches if m.dimension is ScenarioDimension.TERM_MONTHS)
    tampered = offer.model_copy(
        update={"scenario_matches": (fake.model_copy(update={"state": MatchState.MATCHES}),)}
    )
    fail(validate_offer(tampered, context), "t045.scenario-consistency", "contradicts")

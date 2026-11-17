from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    HELOC_POST_INTRO_RULE_ID,
    ArmTerms,
    CanonicalScenarioId,
    ConditionKind,
    FixedEquityDetails,
    HelocDetails,
    HelocPostIntro,
    IntroRate,
    LienPosition,
    LoanPurpose,
    NormalizedOffer,
    OfferStatus,
    PostIntroState,
    ProductFamily,
    PurchaseMortgageDetails,
    RateTier,
    RateType,
    RefinanceMortgageDetails,
    TierBound,
    TierDimension,
)

from .factories import (
    T0,
    adjustable_details,
    condition,
    equity_offer,
    grid_tier,
    heloc_details,
    heloc_offer,
    margin,
    matches_all,
    points,
    provenance,
    purchase_kwargs,
    purchase_offer,
    rate,
    refinance_offer,
    tier,
)

D = Decimal
_tier = tier


# --- distinct detail models -------------------------------------------------------------


def test_each_product_shape_has_its_own_detail_model_and_family() -> None:
    assert purchase_offer().family is ProductFamily.MORTGAGE
    assert isinstance(purchase_offer().details, PurchaseMortgageDetails)
    assert refinance_offer().family is ProductFamily.MORTGAGE
    assert isinstance(refinance_offer().details, RefinanceMortgageDetails)
    assert equity_offer().family is ProductFamily.HOME_EQUITY_LOAN
    assert isinstance(equity_offer().details, FixedEquityDetails)
    assert heloc_offer().family is ProductFamily.HELOC
    assert isinstance(heloc_offer().details, HelocDetails)


def test_purpose_is_separate_from_family() -> None:
    assert purchase_offer().purpose is LoanPurpose.PURCHASE
    assert refinance_offer().purpose is LoanPurpose.RATE_TERM_REFINANCE
    cash_out = refinance_offer(
        LoanPurpose.CASH_OUT_REFINANCE, scenario_id=None, scenario_matches=()
    )
    assert cash_out.purpose is LoanPurpose.CASH_OUT_REFINANCE
    assert equity_offer().purpose is None
    assert heloc_offer().purpose is None


def test_refinance_details_reject_purchase_purpose() -> None:
    with pytest.raises(ValidationError, match="cannot have purchase"):
        RefinanceMortgageDetails(
            term_months=360, rate_type=RateType.FIXED, purpose=LoanPurpose.PURCHASE
        )


def test_details_discriminated_by_kind_from_json_shape() -> None:
    payload = purchase_offer().model_dump(mode="json")
    payload["scenario_id"] = None
    payload["scenario_matches"] = []
    payload["field_provenance"] = []
    payload["details"] = {"kind": "heloc", "lien_position": "second"}
    assert isinstance(NormalizedOffer.model_validate(payload).details, HelocDetails)
    payload["details"] = {"kind": "mystery"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        NormalizedOffer.model_validate(payload)


def test_fixed_equity_amount_order() -> None:
    with pytest.raises(ValidationError, match="min_amount"):
        FixedEquityDetails(
            term_months=120,
            lien_position=LienPosition.SECOND,
            min_amount=D(50000),
            max_amount=D(10000),
        )


def test_scenario_must_belong_to_offer_family_and_purpose() -> None:
    with pytest.raises(ValidationError, match="does not belong"):
        purchase_offer(scenario_id=CanonicalScenarioId.HELOC, scenario_matches=())
    with pytest.raises(ValidationError, match="does not belong"):
        purchase_offer(scenario_id=CanonicalScenarioId.RATE_TERM_REFI_30Y_FIXED)
    with pytest.raises(ValidationError, match="does not belong"):
        refinance_offer(LoanPurpose.CASH_OUT_REFINANCE)


def test_scenario_matches_need_a_scenario_and_unique_dimensions() -> None:
    with pytest.raises(ValidationError, match="require a scenario_id"):
        purchase_offer(scenario_id=None)
    matches = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    with pytest.raises(ValidationError, match="duplicate scenario dimension"):
        purchase_offer(scenario_matches=(*matches, matches[0]))


# --- ARM ---------------------------------------------------------------------------------


def test_arm_terms_required_iff_adjustable_and_fields_may_be_null() -> None:
    with pytest.raises(ValidationError, match="require ARM"):
        adjustable_details(None)
    with pytest.raises(ValidationError, match="cannot carry ARM"):
        PurchaseMortgageDetails(term_months=360, rate_type=RateType.FIXED, arm=ArmTerms())
    partial = adjustable_details(ArmTerms(initial_period_months=84))
    assert partial.arm is not None
    assert partial.arm.index_name is None  # never inferred
    assert not partial.arm.is_complete


def test_complete_arm_terms() -> None:
    arm = ArmTerms(
        initial_period_months=84,
        adjustment_interval_months=12,
        index_name="SOFR",
        margin=margin("2.750"),
        initial_cap=D(5),
        periodic_cap=D(2),
        lifetime_cap=D(5),
    )
    assert arm.is_complete
    offer = purchase_offer(details=adjustable_details(arm), scenario_id=None, scenario_matches=())
    assert arm.margin is not None
    assert arm.margin.provenance in list(offer.iter_provenance())


# --- points null vs zero ----------------------------------------------------------------


def test_points_null_and_zero_are_distinct() -> None:
    unknown = purchase_offer(points=None)
    zero = purchase_offer(points=points("0"))
    one = purchase_offer(points=points("0.875"))
    assert unknown.points is None
    assert zero.points is not None
    assert zero.points.value == 0
    assert unknown.points != zero.points
    assert one.points is not None
    assert one.points.value == D("0.875")


@pytest.mark.parametrize("value", ["-0.001", "10.00001"])
def test_points_bounds(value: str) -> None:
    with pytest.raises(ValidationError):
        points(value)


# --- conditions, tiers ------------------------------------------------------------------


def test_tier_requires_bound_and_rate_and_ordered_bounds() -> None:
    with pytest.raises(ValidationError, match="at least one end"):
        TierBound(dimension=TierDimension.LTV)
    with pytest.raises(ValidationError):
        RateTier(tier_id="t", bounds=(), note_rate=rate("6"))
    with pytest.raises(ValidationError, match="note rate or an APR"):
        RateTier(
            tier_id="t",
            bounds=(TierBound(dimension=TierDimension.LTV, lower=D(0), upper=D(80)),),
        )
    with pytest.raises(ValidationError, match="exceeds"):
        TierBound(dimension=TierDimension.LTV, lower=D(80), upper=D(60))
    with pytest.raises(ValidationError, match="only once"):
        grid_tier("t", (TierDimension.LTV, 0, 60), (TierDimension.LTV, 61, 80))


def test_tier_boundaries_are_inclusive() -> None:
    tier = _tier("t", 60, 80, TierDimension.LTV)
    ltv = TierDimension.LTV
    assert tier.contains({ltv: D(60)})
    assert tier.contains({ltv: D(80)})
    assert not tier.contains({ltv: D("59.99")})
    assert not tier.contains({ltv: D("80.01")})
    assert not tier.contains({})  # an unknown value never satisfies a bound
    assert not tier.contains({ltv: None})
    open_ended = _tier("open", 740, None, TierDimension.CREDIT_SCORE)
    assert open_ended.contains({TierDimension.CREDIT_SCORE: D(850)})
    assert _tier("floor", None, 60, ltv).contains({ltv: D(0)})


def test_overlapping_tiers_rejected_adjacent_allowed() -> None:
    ok = (
        _tier("a", 0, 60, TierDimension.LTV),
        _tier("b", 61, 80, TierDimension.LTV),
    )
    assert len(purchase_offer(tiers=ok).tiers) == 2
    with pytest.raises(ValidationError, match="overlaps"):
        purchase_offer(
            tiers=(_tier("a", 0, 60, TierDimension.LTV), _tier("b", 60, 80, TierDimension.LTV))
        )
    with pytest.raises(ValidationError, match="overlaps"):
        purchase_offer(
            tiers=(_tier("a", None, 70, TierDimension.LTV), _tier("b", 50, None, TierDimension.LTV))
        )


_LTV, _SCORE = TierDimension.LTV, TierDimension.CREDIT_SCORE


def test_two_dimensional_grid_covers_cells_and_rejects_real_overlap() -> None:
    grid = (
        grid_tier("lo-lo", (_LTV, 0, 80), (_SCORE, 300, 739)),
        grid_tier("lo-hi", (_LTV, 0, 80), (_SCORE, 740, None)),
        grid_tier("hi-lo", (_LTV, 81, 95), (_SCORE, 300, 739)),
        grid_tier("hi-hi", (_LTV, 81, 95), (_SCORE, 740, None)),
    )
    offer = purchase_offer(tiers=grid)
    assert len(offer.tiers) == 4

    def covering(ltv: str, score: str) -> list[str]:
        values = {_LTV: D(ltv), _SCORE: D(score)}
        return [t.tier_id for t in offer.tiers if t.contains(values)]

    assert covering("80", "780") == ["lo-hi"]  # inclusive upper bound, open-ended score
    assert covering("80.5", "780") == []  # falls in the gap between 80 and 81
    assert covering("81", "739") == ["hi-lo"]
    assert covering("95", "850") == ["hi-hi"]
    # A real overlap in both axes is rejected ...
    with pytest.raises(ValidationError, match="overlaps"):
        purchase_offer(
            tiers=(
                grid_tier("a", (_LTV, 0, 80), (_SCORE, 700, None)),
                grid_tier("b", (_LTV, 80, 90), (_SCORE, 740, None)),
            )
        )
    # ... while sharing one axis range but disjoint on the other is fine.
    assert purchase_offer(
        tiers=(
            grid_tier("a", (_LTV, 0, 80), (_SCORE, 300, 699)),
            grid_tier("b", (_LTV, 0, 80), (_SCORE, 700, None)),
        )
    )


def test_tier_axis_order_is_normalized_and_missing_axis_value_does_not_cover() -> None:
    forward = grid_tier("t", (_LTV, 0, 80), (_SCORE, 700, None))
    backward = grid_tier("t", (_SCORE, 700, None), (_LTV, 0, 80))
    assert forward == backward
    assert not forward.contains({_LTV: D(70)})  # credit score unknown -> not covered


def test_tiers_on_different_axes_overlap_because_one_borrower_satisfies_both() -> None:
    with pytest.raises(ValidationError, match="overlaps"):
        purchase_offer(
            tiers=(_tier("ltv", 0, 80, _LTV), _tier("score", 740, None, _SCORE)),
        )


def test_as_low_as_headline_requires_as_low_as_condition() -> None:
    with pytest.raises(ValidationError, match="as_low_as condition"):
        purchase_offer(headline_is_as_low_as=True)
    with pytest.raises(ValidationError, match="as_low_as condition"):
        purchase_offer(headline_is_as_low_as=True, conditions=(condition(),))
    offer = purchase_offer(
        headline_is_as_low_as=True,
        conditions=(condition("c1", ConditionKind.AS_LOW_AS),),
    )
    assert offer.headline_is_as_low_as


def test_duplicate_condition_and_tier_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate condition_id"):
        purchase_offer(conditions=(condition("c"), condition("c")))
    with pytest.raises(ValidationError, match="duplicate tier_id"):
        purchase_offer(
            tiers=(_tier("t", 0, 10, TierDimension.LTV), _tier("t", 11, 20, TierDimension.LTV))
        )


# --- offer-level invariants ---------------------------------------------------------------


def test_offer_must_state_some_rate() -> None:
    with pytest.raises(ValidationError, match="at least one rate"):
        purchase_offer(note_rate=None, apr=None)
    assert purchase_offer(apr=None).apr is None  # note rate alone is representable
    assert purchase_offer(note_rate=None).note_rate is None  # APR alone too


def test_note_rate_and_apr_are_separate_fields() -> None:
    offer = purchase_offer(note_rate=rate("6.125"), apr=rate("6.500"))
    assert offer.note_rate is not None
    assert offer.apr is not None
    assert offer.note_rate.value != offer.apr.value


def test_time_ordering_invariants() -> None:
    with pytest.raises(ValidationError, match="published_at cannot precede"):
        purchase_offer(status=OfferStatus.PUBLISHED, published_at=T0 - timedelta(seconds=1))
    published = purchase_offer(status=OfferStatus.PUBLISHED, published_at=T0)
    assert published.published_at == T0
    with pytest.raises(ValidationError, match="valid_until"):
        purchase_offer(valid_until=T0)
    assert purchase_offer(valid_until=T0 + timedelta(seconds=1)).valid_until is not None
    with pytest.raises(ValidationError, match="requires published_at"):
        purchase_offer(status=OfferStatus.PUBLISHED, published_at=None)


def test_source_effective_observed_and_published_times_are_independent() -> None:
    offer = purchase_offer(
        status=OfferStatus.PUBLISHED,
        effective_at=T0 - timedelta(days=10),
        observed_at=T0,
        published_at=T0 + timedelta(hours=6),
    )
    assert offer.effective_at != offer.observed_at != offer.published_at


def test_original_source_text_is_retained_alongside_normalized_values() -> None:
    offer = purchase_offer(note_rate=rate("6.125", text="6.125% (6.250% APR*)"))
    assert offer.note_rate is not None
    assert offer.note_rate.provenance.source_text == "6.125% (6.250% APR*)"
    assert offer.note_rate.value == D("6.125")


def test_offer_requires_supporting_evidence() -> None:
    with pytest.raises(ValidationError):
        purchase_offer(supporting_evidence=())


def test_iter_provenance_covers_every_value() -> None:
    offer = purchase_offer(
        conditions=(condition(),),
        tiers=(_tier("t", 0, 80, TierDimension.LTV),),
    )
    items = list(offer.iter_provenance())
    assert offer.note_rate is not None
    assert offer.points is not None
    assert offer.conditions[0].provenance in items
    assert offer.tiers[0].note_rate is not None
    assert offer.tiers[0].note_rate.provenance in items
    assert offer.points.provenance in items
    # Evidence + note/APR/points + condition + tier + mapped fields + scenario statements.
    assert len(items) == 1 + 3 + 1 + 1 + len(offer.field_provenance) + len(offer.scenario_matches)
    assert all(match.provenance in items for match in offer.scenario_matches)
    assert {e.path for e in offer.field_provenance} == offer.material_paths()


def test_purchase_kwargs_factory_is_valid_baseline() -> None:
    kwargs: dict[str, Any] = purchase_kwargs()
    assert NormalizedOffer(**kwargs) == purchase_offer(field_provenance=())
    assert provenance().source_text == "6.125%"


# --- HELOC post-intro derivation ---------------------------------------------------------


def test_heloc_intro_is_distinct_from_post_intro() -> None:
    details = heloc_details(intro=IntroRate(rate=rate("3.990"), duration_months=12))
    assert details.intro is not None
    assert details.intro.rate.value == D("3.990")
    post = details.post_intro()
    assert post.rate == D("8.000")  # 7.500 + 0.500, above the 4.000 floor
    assert post.rate != details.intro.rate.value


def test_post_intro_derived_from_index_margin_and_floor() -> None:
    post = heloc_details().post_intro()
    assert post.state is PostIntroState.DERIVED
    assert post.rate == D("8.000")
    assert post.rule_id == HELOC_POST_INTRO_RULE_ID
    assert post.missing_inputs == ()


def test_post_intro_floor_binds_when_index_plus_margin_is_lower() -> None:
    post = heloc_details(index_rate=rate("3.000")).post_intro()
    assert post.state is PostIntroState.DERIVED
    assert post.rate == D("4.000")


def test_post_intro_equal_to_floor_boundary() -> None:
    post = heloc_details(index_rate=rate("3.500")).post_intro()  # 3.500 + 0.500 == floor
    assert post.rate == D("4.000")


def test_post_intro_ceiling_caps_when_stated() -> None:
    post = heloc_details(index_rate=rate("12.000"), ceiling=rate("18.000")).post_intro()
    assert post.rate == D("12.500")
    capped = heloc_details(index_rate=rate("12.000"), ceiling=rate("12.250")).post_intro()
    assert capped.rate == D("12.250")


@pytest.mark.parametrize("missing", ["index_name", "index_rate", "margin", "floor"])
def test_post_intro_incomplete_when_any_required_input_missing(missing: str) -> None:
    post = heloc_details(**{missing: None}).post_intro()
    assert post.state is PostIntroState.INCOMPLETE
    assert post.rate is None
    assert post.rule_id is None
    assert post.missing_inputs == (missing,)


def test_post_intro_reports_every_missing_input_in_order() -> None:
    post = HelocDetails(lien_position=LienPosition.SECOND, margin=margin("0.5")).post_intro()
    assert post.state is PostIntroState.INCOMPLETE
    assert post.missing_inputs == ("index_name", "index_rate", "floor")


def test_intro_rate_alone_never_yields_a_post_intro_rate() -> None:
    details = HelocDetails(
        lien_position=LienPosition.SECOND, intro=IntroRate(rate=rate("3.990"), duration_months=6)
    )
    assert details.post_intro().state is PostIntroState.INCOMPLETE
    assert details.post_intro().rate is None


def test_explicitly_stated_post_intro_apr_is_reported_as_stated() -> None:
    post = HelocDetails(
        lien_position=LienPosition.SECOND, stated_post_intro_apr=rate("8.250")
    ).post_intro()
    assert post.state is PostIntroState.STATED
    assert post.rate == D("8.250")
    assert post.rule_id is None


def test_floor_cannot_exceed_ceiling() -> None:
    with pytest.raises(ValidationError, match="floor exceeds ceiling"):
        heloc_details(floor=rate("9"), ceiling=rate("8"))


def test_post_intro_out_of_range_is_incomplete_not_invalid() -> None:
    post = heloc_details(index_rate=rate("99.000"), margin=margin("5.000")).post_intro()
    assert post.state is PostIntroState.INCOMPLETE
    assert post.missing_inputs == ("derived_rate_out_of_range",)


def test_post_intro_result_shape_validation() -> None:
    with pytest.raises(ValidationError):
        HelocPostIntro(state=PostIntroState.DERIVED, rate=D(5))  # no rule id
    with pytest.raises(ValidationError):
        HelocPostIntro(state=PostIntroState.STATED, rate=D(5), rule_id="r.1")
    with pytest.raises(ValidationError):
        HelocPostIntro(state=PostIntroState.INCOMPLETE, rate=D(5), missing_inputs=("x",))
    with pytest.raises(ValidationError):
        HelocPostIntro(state=PostIntroState.INCOMPLETE)
    with pytest.raises(ValidationError):
        HelocPostIntro(state=PostIntroState.STATED)

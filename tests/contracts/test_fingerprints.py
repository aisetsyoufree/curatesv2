from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel

from cu_rates.contracts import (
    ArmTerms,
    CanonicalScenarioId,
    ChangeKind,
    ConditionKind,
    IntroRate,
    MatchState,
    NormalizedOffer,
    OfferFingerprints,
    OfferSnapshot,
    OfferStatus,
    PurchaseMortgageDetails,
    RateTier,
    RateType,
    TierBound,
    TierDimension,
    classify_change,
    offer_fingerprints,
    prove_rate_change,
)

from .factories import (
    T0,
    condition,
    equity_offer,
    grid_tier,
    heloc_details,
    heloc_offer,
    margin,
    matches_all,
    points,
    provenance,
    purchase_offer,
    rate,
    tier,
)

D = Decimal
margin_ = margin


def test_fingerprints_are_deterministic_and_hex() -> None:
    first = offer_fingerprints(purchase_offer())
    second = offer_fingerprints(purchase_offer())
    assert first == second
    assert len(first.structural) == len(first.value) == 64
    assert first.structural != first.value
    assert first.algorithm == "fp1"


def test_pure_rate_change_moves_value_fingerprint_only() -> None:
    before = offer_fingerprints(purchase_offer())
    after = offer_fingerprints(purchase_offer(note_rate=rate("6.250"), apr=rate("6.375")))
    assert before.structural == after.structural
    assert before.value != after.value
    assert classify_change(before, after) is ChangeKind.VALUE_ONLY


def test_rate_change_proof_reports_basis_points() -> None:
    before = OfferSnapshot.from_offer(purchase_offer())
    after = OfferSnapshot.from_offer(purchase_offer(note_rate=rate("6.250"), apr=rate("6.000")))
    proof = prove_rate_change(before, after)
    assert proof.change is ChangeKind.VALUE_ONLY
    assert proof.structural_unchanged
    assert proof.delta_for("note_rate") == D("12.5")
    assert proof.delta_for("apr") == D("-25")
    assert proof.max_abs_delta_bp == D("25")
    assert not proof.points_changed


def test_unchanged_offer_proves_no_change() -> None:
    snapshot = OfferSnapshot.from_offer(purchase_offer())
    proof = prove_rate_change(snapshot, snapshot)
    assert proof.change is ChangeKind.UNCHANGED
    assert proof.delta_for("note_rate") == 0
    assert not proof.unbounded
    assert proof.max_abs_delta_bp == 0


def test_structural_edits_change_structural_fingerprint() -> None:
    base = offer_fingerprints(purchase_offer())
    shorter_term = purchase_offer(
        details=PurchaseMortgageDetails(term_months=180, rate_type=RateType.FIXED, lock_days=30)
    )
    other_product = purchase_offer(product_id="prod-other")
    other_institution = purchase_offer(institution_charter=54321)
    other_lock = purchase_offer(
        details=PurchaseMortgageDetails(term_months=360, rate_type=RateType.FIXED, lock_days=45)
    )
    with_condition = purchase_offer(conditions=(condition(),))
    for variant in (shorter_term, other_product, other_institution, other_lock, with_condition):
        fingerprints = offer_fingerprints(variant)
        assert classify_change(base, fingerprints) is ChangeKind.STRUCTURAL
        assert fingerprints.structural != base.structural


def test_family_change_is_structural() -> None:
    change = classify_change(
        offer_fingerprints(purchase_offer()), offer_fingerprints(equity_offer())
    )
    assert change is ChangeKind.STRUCTURAL


def test_condition_kind_and_scenario_state_are_structural() -> None:
    base = offer_fingerprints(purchase_offer(conditions=(condition(),)))
    new_kind = offer_fingerprints(
        purchase_offer(conditions=(condition(kind=ConditionKind.RELATIONSHIP),))
    )
    new_state = offer_fingerprints(
        purchase_offer(conditions=(condition(state=MatchState.CONFLICTS),))
    )
    assert classify_change(base, new_kind) is ChangeKind.STRUCTURAL
    assert classify_change(base, new_state) is ChangeKind.STRUCTURAL


def test_points_null_versus_zero_is_a_structural_distinction() -> None:
    unknown = offer_fingerprints(purchase_offer(points=None))
    zero = offer_fingerprints(purchase_offer(points=points("0")))
    assert classify_change(unknown, zero) is ChangeKind.STRUCTURAL


def test_points_value_change_is_value_only_and_flagged() -> None:
    zero = OfferSnapshot.from_offer(purchase_offer(points=points("0")))
    one = OfferSnapshot.from_offer(purchase_offer(points=points("1.000")))
    proof = prove_rate_change(zero, one)
    assert proof.change is ChangeKind.VALUE_ONLY
    assert proof.points_changed


def test_rate_appearing_or_disappearing_is_structural_with_no_delta() -> None:
    both = OfferSnapshot.from_offer(purchase_offer())
    note_only = OfferSnapshot.from_offer(purchase_offer(apr=None))
    proof = prove_rate_change(both, note_only)
    assert proof.change is ChangeKind.STRUCTURAL
    assert not proof.structural_unchanged
    assert proof.delta_for("apr") is None  # no pairing, hence no bogus delta


def test_noise_does_not_affect_either_fingerprint() -> None:
    base = offer_fingerprints(purchase_offer())
    noisy = purchase_offer(
        offer_id="offer-999",
        status=OfferStatus.PUBLISHED,
        observed_at=T0 + timedelta(hours=5),
        published_at=T0 + timedelta(hours=6),
        valid_until=T0 + timedelta(days=9),
        effective_at=None,
        supporting_evidence=(provenance("different text", artifact_id="f" * 64),),
        note_rate=rate("6.125", text="six and an eighth percent"),
    )
    assert offer_fingerprints(noisy) == base


def test_equal_decimals_with_different_scale_fingerprint_identically() -> None:
    a = offer_fingerprints(purchase_offer(note_rate=rate("6.125"), apr=rate("6.25")))
    b = offer_fingerprints(purchase_offer(note_rate=rate("6.12500"), apr=rate("6.250")))
    assert a == b


def test_condition_and_tier_order_do_not_matter() -> None:
    conds = (condition("a"), condition("b", ConditionKind.AUTOPAY))
    tiers = (tier("t1", 0, 60, TierDimension.LTV), tier("t2", 61, 80, TierDimension.LTV))
    one = offer_fingerprints(purchase_offer(conditions=conds, tiers=tiers))
    two = offer_fingerprints(purchase_offer(conditions=conds[::-1], tiers=tiers[::-1]))
    assert one == two


def test_tier_rate_change_is_value_only_but_tier_boundary_change_is_structural() -> None:
    def bounded(upper: int, value: str) -> RateTier:
        return RateTier(
            tier_id="t",
            bounds=(TierBound(dimension=TierDimension.LTV, lower=D(0), upper=D(upper)),),
            note_rate=rate(value),
        )

    base = offer_fingerprints(purchase_offer(tiers=(bounded(80, "6.000"),)))
    moved_rate = offer_fingerprints(purchase_offer(tiers=(bounded(80, "6.125"),)))
    moved_bound = offer_fingerprints(purchase_offer(tiers=(bounded(85, "6.000"),)))
    assert classify_change(base, moved_rate) is ChangeKind.VALUE_ONLY
    assert classify_change(base, moved_bound) is ChangeKind.STRUCTURAL


def _arm_offer(margin: str, index: str) -> NormalizedOffer:
    arm = ArmTerms(index_name=index, margin=margin_(margin), initial_period_months=84)
    details = PurchaseMortgageDetails(term_months=360, rate_type=RateType.ADJUSTABLE, arm=arm)
    return purchase_offer(details=details, scenario_id=None, scenario_matches=())


def test_arm_margin_is_value_but_arm_index_is_structure() -> None:
    base = offer_fingerprints(_arm_offer("2.750", "SOFR"))
    new_margin = offer_fingerprints(_arm_offer("3.000", "SOFR"))
    new_index = offer_fingerprints(_arm_offer("2.750", "CMT"))
    assert classify_change(base, new_margin) is ChangeKind.VALUE_ONLY
    assert classify_change(base, new_index) is ChangeKind.STRUCTURAL


def test_heloc_value_fields_versus_structure() -> None:
    base = offer_fingerprints(heloc_offer())
    new_margin = offer_fingerprints(heloc_offer(heloc_details(margin=margin("0.750"))))
    new_index = offer_fingerprints(heloc_offer(heloc_details(index_name="SOFR")))
    no_floor = offer_fingerprints(heloc_offer(heloc_details(floor=None)))
    assert classify_change(base, new_margin) is ChangeKind.VALUE_ONLY
    assert classify_change(base, new_index) is ChangeKind.STRUCTURAL
    assert classify_change(base, no_floor) is ChangeKind.STRUCTURAL


def test_fingerprint_hex_validation() -> None:
    with pytest.raises(ValueError, match="String should match"):
        OfferFingerprints(structural="nothex", value="0" * 64)


# --- scenario matches are structural ---------------------------------------------------------


def test_promoting_a_scenario_dimension_to_match_is_structural() -> None:
    scenario = CanonicalScenarioId.PURCHASE_30Y_FIXED
    full = matches_all(scenario)
    unstated = full[0].model_copy(update={"state": MatchState.UNSTATED, "source_text": None})
    conflict = full[0].model_copy(update={"state": MatchState.CONFLICTS})
    base = offer_fingerprints(purchase_offer(scenario_matches=(unstated, *full[1:])))
    promoted = offer_fingerprints(purchase_offer())
    conflicted = offer_fingerprints(purchase_offer(scenario_matches=(conflict, *full[1:])))
    assert classify_change(base, promoted) is ChangeKind.STRUCTURAL
    assert classify_change(conflicted, promoted) is ChangeKind.STRUCTURAL


def test_match_order_and_quoted_text_are_not_structural() -> None:
    full = matches_all(CanonicalScenarioId.PURCHASE_30Y_FIXED)
    reworded = tuple(m.model_copy(update={"source_text": "reworded"}) for m in full)
    base = offer_fingerprints(purchase_offer())
    assert offer_fingerprints(purchase_offer(scenario_matches=full[::-1])) == base
    assert offer_fingerprints(purchase_offer(scenario_matches=reworded)) == base


# --- per-path rate movement ----------------------------------------------------------------


def _moves(before: NormalizedOffer, after: NormalizedOffer) -> tuple[Decimal, bool]:
    proof = prove_rate_change(OfferSnapshot.from_offer(before), OfferSnapshot.from_offer(after))
    return proof.max_abs_delta_bp, proof.unbounded


def test_every_sourced_rate_is_in_the_snapshot_paths() -> None:
    """Guard: a new sourced-rate field cannot silently escape movement checks."""
    from cu_rates.contracts import SourcedMargin, SourcedRate

    def count(obj: object) -> int:
        if isinstance(obj, SourcedRate | SourcedMargin):
            return 1
        if isinstance(obj, BaseModel):
            return sum(count(getattr(obj, name)) for name in type(obj).model_fields)
        if isinstance(obj, tuple):
            return sum(count(item) for item in obj)
        return 0

    arm = ArmTerms(index_name="SOFR", margin=margin("2.750"))
    arm_details = PurchaseMortgageDetails(term_months=360, rate_type=RateType.ADJUSTABLE, arm=arm)
    offers = (
        purchase_offer(tiers=(tier("t", 0, 80, TierDimension.LTV),)),
        purchase_offer(details=arm_details, scenario_id=None, scenario_matches=()),
        heloc_offer(
            heloc_details(
                intro=IntroRate(rate=rate("3.990"), duration_months=6),
                ceiling=rate("18"),
                stated_post_intro_apr=rate("8.000"),
            )
        ),
    )
    for offer in offers:
        sourced = count(offer)
        derived = 1 if "details.post_intro_rate" in offer.rate_values() else 0
        assert len(offer.rate_values()) == sourced + derived


@pytest.mark.parametrize(
    ("after", "expected_bp"),
    [
        (heloc_offer(heloc_details(margin=margin("1.000"))), D(50)),
        (heloc_offer(heloc_details(margin=margin("1.750"))), D(125)),
        (heloc_offer(heloc_details(intro=IntroRate(rate=rate("3.990"), duration_months=6))), None),
        (heloc_offer(heloc_details(index_rate=rate("8.500"))), D(100)),
        (heloc_offer(heloc_details(floor=rate("9.000"))), D(500)),  # floor 4 -> 9 binds post-intro
    ],
)
def test_heloc_component_movement_is_bounded_per_path(
    after: NormalizedOffer, expected_bp: Decimal | None
) -> None:
    before = heloc_offer()
    if expected_bp is None:  # intro rate appearing is structural, with nothing to pair
        proof = prove_rate_change(OfferSnapshot.from_offer(before), OfferSnapshot.from_offer(after))
        assert proof.change is ChangeKind.STRUCTURAL
        assert proof.delta_for("details.intro.rate") is None
        return
    moved, unbounded = _moves(before, after)
    assert moved == expected_bp
    assert not unbounded


def test_heloc_margin_move_is_visible_even_though_headline_values_are_absent() -> None:
    proof = prove_rate_change(
        OfferSnapshot.from_offer(heloc_offer()),
        OfferSnapshot.from_offer(heloc_offer(heloc_details(margin=margin("2.500")))),
    )
    assert proof.delta_for("details.margin") == D(200)
    assert proof.delta_for("details.post_intro_rate") == D(200)
    assert proof.max_abs_delta_bp == D(200)


def test_heloc_intro_rate_move_is_bounded() -> None:
    def with_intro(value: str) -> NormalizedOffer:
        return heloc_offer(heloc_details(intro=IntroRate(rate=rate(value), duration_months=6)))

    assert _moves(with_intro("3.000"), with_intro("4.000")) == (D(100), False)


def test_tier_rate_move_is_bounded_per_tier() -> None:
    def tiered(value: str) -> NormalizedOffer:
        return purchase_offer(tiers=(grid_tier("t", (TierDimension.LTV, 0, 80), rate_value=value),))

    assert _moves(tiered("6.125"), tiered("6.925")) == (D(80), False)


def test_arm_margin_move_is_bounded() -> None:
    assert _moves(_arm_offer("2.750", "SOFR"), _arm_offer("3.500", "SOFR")) == (D(75), False)


def test_added_or_removed_value_paths_are_structural_not_paired() -> None:
    base = OfferSnapshot.from_offer(heloc_offer())
    no_ceiling_to_ceiling = OfferSnapshot.from_offer(heloc_offer(heloc_details(ceiling=rate("18"))))
    proof = prove_rate_change(base, no_ceiling_to_ceiling)
    assert proof.change is ChangeKind.STRUCTURAL
    assert proof.delta_for("details.ceiling") is None
    assert proof.max_abs_delta_bp == 0


def test_value_change_that_cannot_be_paired_is_unbounded() -> None:
    """A snapshot whose value fingerprint moved without any pairable path is not 'small'."""
    prior = OfferSnapshot.from_offer(purchase_offer())
    moved = OfferSnapshot.from_offer(purchase_offer(note_rate=rate("9.000")))
    stripped = moved.model_copy(update={"rates": ()})
    proof = prove_rate_change(prior, stripped)
    assert proof.change is ChangeKind.VALUE_ONLY
    assert proof.unbounded
    shifted_paths = moved.model_copy(update={"rates": moved.rates[:1]})
    assert prove_rate_change(prior, shifted_paths).unbounded


def test_snapshot_rejects_duplicate_paths() -> None:
    snapshot = OfferSnapshot.from_offer(purchase_offer())
    with pytest.raises(ValueError, match="duplicate rate path"):
        OfferSnapshot(
            offer_id=snapshot.offer_id,
            product_id=snapshot.product_id,
            fingerprints=snapshot.fingerprints,
            rates=(snapshot.rates[0], snapshot.rates[0]),
        )


def test_negative_margin_movement_is_signed() -> None:
    def prime_minus(value: str) -> NormalizedOffer:
        return heloc_offer(heloc_details(margin=margin(value)))

    proof = prove_rate_change(
        OfferSnapshot.from_offer(prime_minus("-0.250")),
        OfferSnapshot.from_offer(prime_minus("0.250")),
    )
    assert proof.delta_for("details.margin") == D(50)

"""Taxonomy: separate axes, evidence-only findings, and the merged seed-fixture expectations.

The seed fixtures are inputs, not proof of extraction: nothing here parses HTML. Phrases are
checked to occur in the fixture bytes only so each finding cites text that is really there.
"""

import hashlib
import inspect
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    PURPOSES_BY_FAMILY,
    TAXONOMY_VERSION,
    CanonicalScenarioId,
    CollectionMethod,
    DimensionMatch,
    EvidenceKind,
    FetchMethod,
    FetchProfile,
    FindingReviewState,
    HtmlLocator,
    InstitutionSourceRelationship,
    LoanPurpose,
    MatchState,
    NormalizedOffer,
    OutOfScopeProduct,
    PostIntroState,
    ProductFamily,
    ProductFinding,
    ProductScope,
    Provenance,
    PublicationContext,
    PurchaseMortgageDetails,
    RateAvailability,
    RateSource,
    RateType,
    ReasonCode,
    ServiceProvider,
    SourceRelationshipKind,
    collection_method,
    evaluate_publication,
    evaluate_scenario_match,
    evidence_kinds_for,
    fetch_method_for,
)
from cu_rates.contracts.enums import OfferStatus, ProviderKind, ScenarioDimension
from cu_rates.contracts.scenarios import REQUIRED_DIMENSIONS, match_numeric

from .factories import (
    CHARTER,
    SOURCE_ID,
    SV,
    T0,
    context,
    extraction,
    heloc_details,
    purchase_offer,
    relationship,
)

SEED = Path(__file__).parents[1] / "fixtures" / "seed"
CATALOG: dict[str, Any] = json.loads((SEED / "expected_outcomes.json").read_text(encoding="utf-8"))
R = ReasonCode


def case(case_id: str) -> dict[str, Any]:
    return next(c for c in CATALOG["cases"] if c["case_id"] == case_id)


def cited(case_id: str, phrase: str) -> Provenance:
    """Evidence for ``phrase`` in the case's artifact (bound to the fixture bytes, not parsed)."""
    entry = case(case_id)
    page = (SEED / entry["fixture"]).read_text(encoding="utf-8")
    assert phrase in page and phrase in entry["evidence_phrases"]
    return Provenance(
        artifact_id=entry["artifact_id"],
        locator=HtmlLocator(
            selector_type="css",
            selector="body",
            normalized_text_sha256=hashlib.sha256(phrase.encode()).hexdigest(),
        ),
        source_text=phrase,
        extraction=extraction(),
    )


def finding(**overrides: Any) -> ProductFinding:
    kwargs: dict[str, Any] = {
        "schema_version": SV,
        "taxonomy_version": TAXONOMY_VERSION,
        "finding_id": "finding-1",
        "institution_charter": CHARTER,
        "source_id": SOURCE_ID,
        "observed_at": T0,
        "label": "First mortgage",
        "scope": ProductScope.IN_SCOPE,
        "family": ProductFamily.MORTGAGE,
        "rate_availability": RateAvailability.QUOTE_ONLY,
        "evidence": (
            cited(
                "worcester-style",
                "Call for a quote. This page does not advertise a first-mortgage note rate or APR.",
            ),
        ),
    }
    kwargs.update(overrides)
    return ProductFinding(**kwargs)


def reviewed(**overrides: Any) -> dict[str, Any]:
    return {
        "review_state": FindingReviewState.VERIFIED,
        "reviewed_by": "Sid",
        "reviewed_at": T0 + timedelta(hours=1),
        "review_rationale": "Page states a quote is required.",
    } | overrides


# --- persisted vocabulary is unchanged; the new axes are separate -----------------------


def test_existing_persisted_values_are_unchanged() -> None:
    assert {m.value for m in ProductFamily} == {"mortgage", "home_equity_loan", "heloc"}
    assert {m.value for m in LoanPurpose} == {
        "purchase",
        "rate_term_refinance",
        "cash_out_refinance",
    }
    assert {m.value for m in SourceRelationshipKind} == {
        "first_party",
        "provider_hosted",
        "partner_published",
    }
    assert "unknown" not in {m.value for m in ProductFamily}  # ambiguity lives only in findings


def test_axes_share_no_values() -> None:
    axes = [
        {m.value for m in ProductFamily},
        {m.value for m in LoanPurpose},
        {m.value for m in SourceRelationshipKind},
        {m.value for m in CollectionMethod},
        {m.value for m in OutOfScopeProduct},
    ]
    for i, left in enumerate(axes):
        for right in axes[i + 1 :]:
            assert left.isdisjoint(right)


def test_purpose_is_mortgage_only_and_refinance_is_not_a_family() -> None:
    assert PURPOSES_BY_FAMILY[ProductFamily.MORTGAGE] == set(LoanPurpose)
    assert PURPOSES_BY_FAMILY[ProductFamily.HOME_EQUITY_LOAN] == frozenset()
    assert PURPOSES_BY_FAMILY[ProductFamily.HELOC] == frozenset()
    assert not any("refinance" in m.value for m in ProductFamily)
    assert set(PURPOSES_BY_FAMILY) == set(ProductFamily)


@pytest.mark.parametrize("family", [ProductFamily.HOME_EQUITY_LOAN, ProductFamily.HELOC])
def test_equity_products_reject_a_loan_purpose(family: ProductFamily) -> None:
    with pytest.raises(ValidationError, match="mortgage-only"):
        finding(family=family, purposes=(LoanPurpose.PURCHASE,))


def test_a_mortgage_finding_keeps_purchase_and_refinance_as_purposes() -> None:
    both = finding(purposes=(LoanPurpose.RATE_TERM_REFINANCE, LoanPurpose.PURCHASE))
    assert both.purposes == (LoanPurpose.PURCHASE, LoanPurpose.RATE_TERM_REFINANCE)
    assert both.family is ProductFamily.MORTGAGE


# --- relationship and collection method are independent --------------------------------


def test_collection_mapping_and_fail_closed_default() -> None:
    expected = {
        (FetchMethod.HTTP_GET, EvidenceKind.HTML): CollectionMethod.STATIC_HTML,
        (FetchMethod.HTTP_GET, EvidenceKind.PDF): CollectionMethod.PDF,
        (FetchMethod.HTTP_GET, EvidenceKind.JSON): CollectionMethod.JSON_API,
        (FetchMethod.RENDERED_BROWSER, EvidenceKind.RENDERED_DOM): CollectionMethod.BROWSER_XHR,
        (FetchMethod.RENDERED_BROWSER, EvidenceKind.JSON): CollectionMethod.BROWSER_XHR,
    }
    for fetch in FetchMethod:
        for kind in EvidenceKind:
            assert collection_method(fetch, kind) is expected.get(
                (fetch, kind), CollectionMethod.UNSUPPORTED
            )
    assert collection_method(FetchMethod.HTTP_GET, EvidenceKind.IMAGE_OCR) is (
        CollectionMethod.UNSUPPORTED
    )


def test_every_supported_method_names_its_fetch_method_and_evidence() -> None:
    for method in CollectionMethod:
        if method is CollectionMethod.UNSUPPORTED:
            assert fetch_method_for(method) is None
            assert evidence_kinds_for(method) == frozenset()
            continue
        fetch, kinds = fetch_method_for(method), evidence_kinds_for(method)
        assert fetch is not None and kinds
        assert all(collection_method(fetch, kind) is method for kind in kinds)


@pytest.mark.parametrize("kind", list(SourceRelationshipKind))
@pytest.mark.parametrize(
    ("fetch", "evidence"),
    [
        (FetchMethod.HTTP_GET, EvidenceKind.HTML),
        (FetchMethod.HTTP_GET, EvidenceKind.PDF),
        (FetchMethod.RENDERED_BROWSER, EvidenceKind.RENDERED_DOM),
    ],
)
def test_who_hosts_a_source_does_not_choose_how_it_is_collected(
    kind: SourceRelationshipKind, fetch: FetchMethod, evidence: EvidenceKind
) -> None:
    hosted = kind is not SourceRelationshipKind.FIRST_PARTY
    rel = relationship(kind=kind, provider_id="vendor-1" if hosted else None)
    source = RateSource(
        schema_version=SV,
        source_id=SOURCE_ID,
        url="https://rates.example/page",
        evidence_kind=evidence,
    )
    profile = FetchProfile(
        schema_version=SV,
        profile_id="profile-1",
        source_id=SOURCE_ID,
        method=fetch,
        min_interval_seconds=60,
        timeout_seconds=30,
    )
    # The relationship is a different record and cannot reach the classifier: the method
    # follows only the profile's fetch method and the source's evidence kind.
    assert list(inspect.signature(collection_method).parameters) == [
        "fetch_method",
        "evidence_kind",
    ]
    assert rel.kind is kind and (rel.provider_id is not None) is hosted
    assert collection_method(profile.method, source.evidence_kind) is (
        collection_method(fetch, evidence)
    )
    assert collection_method(fetch, evidence) is not CollectionMethod.UNSUPPORTED


def test_relationship_and_collection_live_on_different_records() -> None:
    assert "method" not in InstitutionSourceRelationship.model_fields
    assert "evidence_kind" not in InstitutionSourceRelationship.model_fields
    assert "kind" not in RateSource.model_fields and "provider_id" not in RateSource.model_fields
    assert "provider_id" not in FetchProfile.model_fields
    # Provider ownership lives on the relationship (and on ServiceProvider itself), never on
    # the finding, the source, or the fetch profile.
    assert "provider_id" in InstitutionSourceRelationship.model_fields
    assert "provider_id" not in ProductFinding.model_fields
    with pytest.raises(ValidationError, match="require a provider_id"):
        relationship(kind=SourceRelationshipKind.PROVIDER_HOSTED, provider_id=None)
    with pytest.raises(ValidationError, match="cannot name a provider"):
        relationship(kind=SourceRelationshipKind.FIRST_PARTY, provider_id="vendor-1")
    assert ServiceProvider.model_fields["provider_id"] is not None
    assert ProviderKind.LOAN_PLATFORM in ProviderKind


# --- findings: evidence without an offer ------------------------------------------------


def test_a_finding_has_no_rate_fields_so_it_cannot_become_an_offer() -> None:
    rate_fields = {"note_rate", "apr", "points", "valid_until", "status", "tiers", "details"}
    assert rate_fields.isdisjoint(ProductFinding.model_fields)
    assert OfferStatus.PUBLISHED.value not in {m.value for m in FindingReviewState}


def test_scope_fixes_the_classification() -> None:
    with pytest.raises(ValidationError, match="in-scope finding names"):
        finding(family=None)
    with pytest.raises(ValidationError, match="in-scope finding names"):
        finding(rate_availability=None)
    with pytest.raises(ValidationError, match="cannot carry an out-of-scope"):
        finding(out_of_scope_product=OutOfScopeProduct.VEHICLE)
    with pytest.raises(ValidationError, match="only an in-scope"):
        finding(scope=ProductScope.UNKNOWN, rate_availability=None)  # keeps its family
    with pytest.raises(ValidationError, match="only an in-scope"):
        finding(
            scope=ProductScope.OUT_OF_SCOPE,
            family=None,
            rate_availability=None,
            out_of_scope_product=OutOfScopeProduct.VEHICLE,
            product_id="prod-1",
        )


def test_out_of_scope_names_a_product_kind_and_makes_no_institution_claim() -> None:
    kwargs: dict[str, Any] = {
        "scope": ProductScope.OUT_OF_SCOPE,
        "family": None,
        "rate_availability": None,
    }
    with pytest.raises(ValidationError, match="names the consumer product kind"):
        finding(**kwargs)
    with pytest.raises(ValidationError, match="no rate-availability claim"):
        finding(
            **kwargs
            | {
                "out_of_scope_product": OutOfScopeProduct.VEHICLE,
                "rate_availability": RateAvailability.NOT_OFFERED,
            }
        )


@pytest.mark.parametrize(
    "availability",
    [
        RateAvailability.QUOTE_ONLY,
        RateAvailability.NO_PUBLIC_RATE,
        RateAvailability.PUBLIC_RATE,
        RateAvailability.NOT_OFFERED,
    ],
)
def test_ambiguous_family_stays_unknown_even_if_the_page_says_call_for_a_quote(
    availability: RateAvailability,
) -> None:
    with pytest.raises(ValidationError, match="stays unknown"):
        finding(scope=ProductScope.UNKNOWN, family=None, rate_availability=availability)
    ok = finding(
        scope=ProductScope.UNKNOWN, family=None, rate_availability=RateAvailability.UNKNOWN
    )
    assert ok.rate_availability is RateAvailability.UNKNOWN and not ok.verified_quote_only


def test_every_finding_but_unknown_keeps_its_evidence() -> None:
    with pytest.raises(ValidationError, match="keep the evidence"):
        finding(evidence=())
    unknown = finding(scope=ProductScope.UNKNOWN, family=None, rate_availability=None, evidence=())
    assert unknown.evidence == () and unknown.scope is ProductScope.UNKNOWN


def test_review_states_distinguish_proposed_from_verified_quote_only() -> None:
    proposed = finding()
    assert proposed.review_state is FindingReviewState.PROPOSED and not proposed.verified_quote_only
    verified = finding(**reviewed())
    assert verified.verified_quote_only
    rejected = finding(**reviewed(review_state=FindingReviewState.REJECTED))
    assert not rejected.verified_quote_only
    no_rate = finding(rate_availability=RateAvailability.NO_PUBLIC_RATE, **reviewed())
    assert not no_rate.verified_quote_only  # verified, but a different conclusion


@pytest.mark.parametrize(
    "overrides",
    [
        {"reviewed_by": None},
        {"reviewed_at": None},
        {"review_rationale": None},
        {"reviewed_at": T0 - timedelta(seconds=1)},
    ],
)
def test_a_decided_review_is_complete_and_follows_the_observation(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        finding(**reviewed(**overrides))


def test_a_proposed_finding_carries_no_review_fields_and_unknown_cannot_be_verified() -> None:
    with pytest.raises(ValidationError, match="exactly when decided"):
        finding(reviewed_by="Sid")
    with pytest.raises(ValidationError, match="cannot be verified"):
        finding(scope=ProductScope.UNKNOWN, family=None, rate_availability=None, **reviewed())


def test_taxonomy_version_is_required_and_checked_and_findings_round_trip() -> None:
    assert finding().taxonomy_version == TAXONOMY_VERSION == "1.0"
    for bad in ("2.0", "1.1", "1", "01.0", "x", " 1.0", "1.0 ", "1.0\n", ""):
        with pytest.raises(ValidationError, match="taxonomy"):
            finding(taxonomy_version=bad)
    payload = finding().model_dump(mode="json")
    del payload["taxonomy_version"]  # a record never silently claims the reader's version
    with pytest.raises(ValidationError, match="taxonomy_version"):
        ProductFinding.model_validate(payload)
    original = finding(**reviewed())
    assert ProductFinding.model_validate_json(original.model_dump_json()) == original


# --- merged seed-fixture expectations ---------------------------------------------------


def row_matches(row: dict[str, Any], scenario_term: int) -> tuple[DimensionMatch, ...]:
    """Every dimension matches except the term, compared to the row's stated term."""
    term = match_numeric(Decimal(scenario_term), exact=Decimal(row["term_months"]))
    return tuple(
        DimensionMatch(
            dimension=d,
            state=term if d is ScenarioDimension.TERM_MONTHS else MatchState.MATCHES,
            source_text=f"{d.value} stated",
        )
        for d in sorted(REQUIRED_DIMENSIONS[CanonicalScenarioId.PURCHASE_30Y_FIXED])
    )


def test_athol_style_is_a_static_html_public_mortgage_table() -> None:
    entry = case("athol-style")
    row = entry["rows"][0]  # the catalog's own family and purpose, not re-typed here
    found = finding(
        label="Fixed-rate first mortgages",
        family=ProductFamily(row["product_family"]),
        rate_availability=RateAvailability.PUBLIC_RATE,
        purposes=(LoanPurpose(row["purpose"]),),
        evidence=(cited("athol-style", "6.375%"), cited("athol-style", "6.441%")),
    )
    assert found.evidence[0].artifact_id == entry["artifact_id"]
    assert (
        collection_method(FetchMethod.HTTP_GET, EvidenceKind.HTML) is CollectionMethod.STATIC_HTML
    )
    assert not found.verified_quote_only


def test_a_stated_target_is_not_a_match_and_a_match_is_not_approval() -> None:
    rows = {row["row_id"]: row for row in case("athol-style")["rows"]}
    target = CanonicalScenarioId(rows["purchase-15-year"]["canonical_scenario"])
    assert target is CanonicalScenarioId.PURCHASE_30Y_FIXED  # stated comparison target

    off_target = row_matches(rows["purchase-15-year"], 360)
    assert evaluate_scenario_match(target, off_target) is MatchState.CONFLICTS
    on_target = row_matches(rows["purchase-30-year"], 360)
    assert evaluate_scenario_match(target, on_target) is MatchState.MATCHES

    conflicting = purchase_offer(
        scenario_matches=off_target,
        details=PurchaseMortgageDetails(term_months=180, rate_type=RateType.FIXED, lock_days=30),
    )
    decision = evaluate_publication(conflicting, context(conflicting))
    assert {R.SCENARIO_CONFLICT, R.SCENARIO_TERMS_DIFFER} <= set(decision.reasons)
    assert not decision.ranking_eligible

    matching = purchase_offer(scenario_matches=on_target)
    first = evaluate_publication(matching, context(matching, prior=None))
    assert evaluate_scenario_match(target, matching.scenario_matches) is MatchState.MATCHES
    assert not first.publishable_now and R.FIRST_PUBLICATION in first.reasons  # not approval

    candidate = purchase_offer(scenario_matches=on_target, status=OfferStatus.CANDIDATE)
    assert not evaluate_publication(candidate, context(candidate)).publishable_now


def test_stoughton_style_yields_only_out_of_scope_findings_and_no_mortgage_claim() -> None:
    entry = case("stoughton-style")
    findings = [
        finding(
            finding_id=f"finding-{row['row_id']}",
            label=row["row_id"],
            scope=ProductScope.OUT_OF_SCOPE,
            family=None,
            rate_availability=None,
            out_of_scope_product=OutOfScopeProduct(row["product_family"]),
            evidence=(cited("stoughton-style", f'id="{row["row_id"]}"'),),
        )
        for row in entry["rows"]
    ]
    assert len(findings) == 3
    assert (
        sum(f.scope is ProductScope.IN_SCOPE for f in findings)
        == entry["mortgage_offers_from_this_page"]
        == 0
    )
    assert entry["institution_offering"] == "not_inferred"
    assert all(f.rate_availability is None and f.family is None for f in findings)
    assert {f.out_of_scope_product for f in findings} == {
        OutOfScopeProduct.VEHICLE,
        OutOfScopeProduct.UNSECURED_PERSONAL,
    }


def test_worcester_style_keeps_quote_partner_link_and_second_mortgage_apart() -> None:
    entry = case("worcester-style")
    quote_phrase = (
        "Call for a quote. This page does not advertise a first-mortgage note rate or APR."
    )
    first = finding(
        rate_availability=RateAvailability(entry["first_mortgage"]["availability"]),
        evidence=(cited("worcester-style", quote_phrase),),
    )
    assert first.rate_availability is RateAvailability.QUOTE_ONLY
    assert entry["first_mortgage"]["published_rate"] is False
    assert finding(evidence=first.evidence, **reviewed()).verified_quote_only

    partner = finding(
        finding_id="finding-partner",
        label="Partner program link",
        scope=ProductScope.UNKNOWN,
        family=None,
        rate_availability=RateAvailability.UNKNOWN,
        evidence=(cited("worcester-style", "This link is not a note rate or an APR."),),
    )
    assert entry["partner_link"]["is_rate"] is False
    assert (
        partner.scope is ProductScope.UNKNOWN
        and partner.rate_availability is RateAvailability.UNKNOWN
    )

    row = entry["second_mortgage_table"]["rows"][0]
    second = finding(
        finding_id="finding-second",
        label="Closed-end fixed second mortgages",
        family=ProductFamily(row["product_family"]),
        rate_availability=RateAvailability.PUBLIC_RATE,
        evidence=(cited("worcester-style", "7.250%"), cited("worcester-style", "7.418%")),
    )
    assert second.family is not first.family and second.purposes == ()
    assert second.evidence[0].artifact_id == first.evidence[0].artifact_id == entry["artifact_id"]


def test_a_heloc_formula_without_a_verified_index_value_stays_incomplete() -> None:
    post = heloc_details(index_rate=None).post_intro()
    assert post.state is PostIntroState.INCOMPLETE and "index_rate" in post.missing_inputs


def test_a_finding_cannot_become_an_offer_or_enter_the_publication_context() -> None:
    """Structural, not behavioral: nothing in publication can accept a finding."""
    verified = finding(**reviewed())
    with pytest.raises(ValidationError):
        NormalizedOffer.model_validate(verified.model_dump())
    assert not any("finding" in name for name in PublicationContext.model_fields)
    for model in (PublicationContext, NormalizedOffer):
        assert not any("ProductFinding" in str(f.annotation) for f in model.model_fields.values())

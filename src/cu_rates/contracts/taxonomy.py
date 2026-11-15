"""Controlled vocabulary for what a page is, how it is collected, and what was concluded.

Five questions stay separate; none is encoded in another's values:

* **Product family** (``ProductFamily``) is what the loan *is*: a first-lien ``mortgage``, a
  closed-end fixed ``home_equity_loan``, or an open-end ``heloc``. **Purpose**
  (``LoanPurpose``: purchase, rate/term refinance, cash-out refinance) is why a borrower takes
  it and exists only for mortgages (``PURPOSES_BY_FAMILY``). Refinance is never a family.
* **Source relationship** (``SourceRelationshipKind`` on ``InstitutionSourceRelationship``)
  says who publishes the rate: the institution, a hosting provider, or a partner. Provider
  ownership stays on that relationship, not on the source or the finding.
* **Collection method** (``CollectionMethod``) says how bytes are obtained: static HTML,
  PDF, JSON API, browser/XHR, or unsupported. It maps onto the existing ``FetchMethod`` and
  ``EvidenceKind`` and never encodes hosting.
* **Finding** (``ProductFinding``) records what a page established about a product,
  with evidence, *without* any rate fields and therefore without creating an offer:
  unknown, quote-only, login-required, blocked, no public rate, or out of scope.
* **Review state** (``FindingReviewState``) says whether a human verified the finding.

Two guardrails are structural. A stated comparison target (an offer's ``scenario_id``) is not a
scenario match (``evaluate_scenario_match``), and a scenario match is not publication
approval (``evaluate_publication``); this module defines neither and approves nothing. An
ambiguous product family stays ``unknown`` even if the page says to call for a quote.

Versioning: ``TAXONOMY_VERSION`` is independent of the contract schema version. Adding a
value or a mapping is a minor bump; renaming or removing a persisted value, or changing
what a value means, is a major bump. Readers accept their own major and any minor up to their
own. This addition keeps every existing enum value unchanged (``ProductFamily`` gains no
``unknown``: ambiguity lives only in findings, so it can never reach a product or offer) and
adds only new types, so the contract schema stays 1.0.
"""

import re
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import Field, model_validator

from cu_rates.contracts.base import (
    CharterNumber,
    Identifier,
    SortedUnique,
    Text,
    UtcDatetime,
    VersionedModel,
)
from cu_rates.contracts.enums import (
    CollectionMethod,
    EvidenceKind,
    FetchMethod,
    FindingReviewState,
    LoanPurpose,
    OutOfScopeProduct,
    ProductFamily,
    ProductScope,
    RateAvailability,
)
from cu_rates.contracts.evidence import Provenance

TAXONOMY_MAJOR = 1
TAXONOMY_MINOR = 0
TAXONOMY_VERSION = f"{TAXONOMY_MAJOR}.{TAXONOMY_MINOR}"
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)

# Purpose is a mortgage concept. Home-equity loans and HELOCs are distinct products, not
# refinance flavors, so they accept no purpose.
PURPOSES_BY_FAMILY: Mapping[ProductFamily, frozenset[LoanPurpose]] = {
    ProductFamily.MORTGAGE: frozenset(LoanPurpose),
    ProductFamily.HOME_EQUITY_LOAN: frozenset(),
    ProductFamily.HELOC: frozenset(),
}

# (fetch method, evidence kind) -> collection method. Anything absent is unsupported, so an
# unknown pairing fails closed instead of being collected by guesswork. Image/OCR-only
# sources have no collector yet.
_COLLECTION: Mapping[tuple[FetchMethod, EvidenceKind], CollectionMethod] = {
    (FetchMethod.HTTP_GET, EvidenceKind.HTML): CollectionMethod.STATIC_HTML,
    (FetchMethod.HTTP_GET, EvidenceKind.PDF): CollectionMethod.PDF,
    (FetchMethod.HTTP_GET, EvidenceKind.JSON): CollectionMethod.JSON_API,
    (FetchMethod.RENDERED_BROWSER, EvidenceKind.RENDERED_DOM): CollectionMethod.BROWSER_XHR,
    (FetchMethod.RENDERED_BROWSER, EvidenceKind.JSON): CollectionMethod.BROWSER_XHR,
}


def collection_method(fetch_method: FetchMethod, evidence_kind: EvidenceKind) -> CollectionMethod:
    """Classify a profile's fetch method and a source's evidence kind; unknown pairs fail closed."""
    return _COLLECTION.get((fetch_method, evidence_kind), CollectionMethod.UNSUPPORTED)


def fetch_method_for(method: CollectionMethod) -> FetchMethod | None:
    """The fetch method a collection method uses; ``None`` for unsupported."""
    for (fetch, _), candidate in _COLLECTION.items():
        if candidate is method:
            return fetch
    return None


def evidence_kinds_for(method: CollectionMethod) -> frozenset[EvidenceKind]:
    return frozenset(kind for (_, kind), candidate in _COLLECTION.items() if candidate is method)


def _check_taxonomy_version(value: str) -> str:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"taxonomy_version must look like '<major>.<minor>', got {value!r}")
    if int(match[1]) != TAXONOMY_MAJOR or int(match[2]) > TAXONOMY_MINOR:
        raise ValueError(
            f"unsupported taxonomy version {value}; this package reads up to {TAXONOMY_VERSION}"
        )
    return value


class ProductFinding(VersionedModel):
    """What one page established about one product, with its evidence and review state.

    A finding has no rate, APR, points, validity, or offer status: it can record a quote-only
    first mortgage, a missing public rate, or an out-of-scope consumer product and keep the
    evidence, but it can never be published as, or converted into, an offer. Publication
    still requires a ``NormalizedOffer``, deterministic validation, and the publication gate.
    """

    finding_id: Identifier
    taxonomy_version: Text = TAXONOMY_VERSION
    institution_charter: CharterNumber
    source_id: Identifier
    product_id: Identifier | None = None
    observed_at: UtcDatetime
    label: Text
    scope: ProductScope
    family: ProductFamily | None = None
    purposes: Annotated[tuple[LoanPurpose, ...], SortedUnique] = ()
    out_of_scope_product: OutOfScopeProduct | None = None
    rate_availability: RateAvailability | None = None
    evidence: Annotated[tuple[Provenance, ...], Field(max_length=50)] = ()
    review_state: FindingReviewState = FindingReviewState.PROPOSED
    reviewed_by: Text | None = None
    reviewed_at: UtcDatetime | None = None
    review_rationale: Text | None = None

    @model_validator(mode="after")
    def _version_supported(self) -> Self:
        _check_taxonomy_version(self.taxonomy_version)
        return self

    @model_validator(mode="after")
    def _scope_fixes_classification(self) -> Self:
        in_scope = self.scope is ProductScope.IN_SCOPE
        if in_scope:
            if self.family is None or self.rate_availability is None:
                raise ValueError(
                    "an in-scope finding names its product family and rate availability"
                )
            if self.out_of_scope_product is not None:
                raise ValueError("an in-scope finding cannot carry an out-of-scope product kind")
        elif self.family is not None or self.purposes or self.product_id is not None:
            raise ValueError("only an in-scope finding names a family, purpose, or catalog product")
        if self.scope is ProductScope.OUT_OF_SCOPE:
            if self.out_of_scope_product is None:
                raise ValueError("an out-of-scope finding names the consumer product kind")
            if self.rate_availability is not None:
                raise ValueError(
                    "an out-of-scope finding makes no rate-availability claim about the institution"
                )
        elif self.scope is ProductScope.UNKNOWN:
            if self.out_of_scope_product is not None:
                raise ValueError("an unknown finding cannot name a product kind")
            if self.rate_availability not in (None, RateAvailability.UNKNOWN):
                raise ValueError(
                    "an ambiguous product stays unknown even when the page says to call for a quote"
                )
        return self

    @model_validator(mode="after")
    def _purpose_belongs_to_family(self) -> Self:
        if self.family is not None and not set(self.purposes) <= PURPOSES_BY_FAMILY[self.family]:
            raise ValueError(f"{self.family.value} has no loan purpose; purposes are mortgage-only")
        return self

    @model_validator(mode="after")
    def _findings_keep_their_evidence(self) -> Self:
        unknown = self.scope is ProductScope.UNKNOWN
        if not unknown and not self.evidence:
            raise ValueError("a finding other than unknown must keep the evidence for it")
        return self

    @model_validator(mode="after")
    def _review_is_complete_and_never_invents_knowledge(self) -> Self:
        decided = self.review_state is not FindingReviewState.PROPOSED
        parts = (self.reviewed_by, self.reviewed_at, self.review_rationale)
        if decided != all(part is not None for part in parts) or (
            not decided and any(part is not None for part in parts)
        ):
            raise ValueError("a review records actor, time, and rationale exactly when decided")
        if self.review_state is FindingReviewState.VERIFIED and self.scope is ProductScope.UNKNOWN:
            raise ValueError("an unknown finding cannot be verified")
        if self.reviewed_at is not None and self.reviewed_at < self.observed_at:
            raise ValueError("a review cannot precede the observation it reviews")
        return self

    @property
    def verified_quote_only(self) -> bool:
        return (
            self.scope is ProductScope.IN_SCOPE
            and self.rate_availability is RateAvailability.QUOTE_ONLY
            and self.review_state is FindingReviewState.VERIFIED
        )

"""Closed vocabularies for the V1 contract. Values are persisted; do not rename."""

from enum import StrEnum


class InstitutionStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"
    LIQUIDATED = "liquidated"
    CONVERTED = "converted"
    UNDER_REVIEW = "under_review"


class MembershipKind(StrEnum):
    OPEN = "open"
    GEOGRAPHIC = "geographic"
    EMPLOYER = "employer"
    ASSOCIATION = "association"
    OTHER = "other"


class ProductFamily(StrEnum):
    MORTGAGE = "mortgage"
    HOME_EQUITY_LOAN = "home_equity_loan"
    HELOC = "heloc"


class LoanPurpose(StrEnum):
    PURCHASE = "purchase"
    RATE_TERM_REFINANCE = "rate_term_refinance"
    CASH_OUT_REFINANCE = "cash_out_refinance"


class RateType(StrEnum):
    FIXED = "fixed"
    ADJUSTABLE = "adjustable"


class ConformingStatus(StrEnum):
    CONFORMING = "conforming"
    HIGH_BALANCE_CONFORMING = "high_balance_conforming"
    JUMBO = "jumbo"
    UNKNOWN = "unknown"


class GovernmentGuaranty(StrEnum):
    NONE = "none"
    FHA = "fha"
    VA = "va"
    USDA = "usda"
    UNKNOWN = "unknown"


class BorrowerProgram(StrEnum):
    FIRST_TIME_BUYER = "first_time_buyer"


class PartnerProgram(StrEnum):
    MASSHOUSING = "masshousing"


class RateAvailability(StrEnum):
    """Explicit presence/absence of a public rate for a cataloged product."""

    PUBLIC_RATE = "public_rate"
    QUOTE_ONLY = "quote_only"
    LOGIN_REQUIRED = "login_required"
    BLOCKED = "blocked"
    NO_PUBLIC_RATE = "no_public_rate"
    NOT_OFFERED = "not_offered"
    UNKNOWN = "unknown"


class ProductLifecycle(StrEnum):
    ACTIVE = "active"
    DISCONTINUED = "discontinued"
    UNDER_REVIEW = "under_review"


class ProviderKind(StrEnum):
    LOAN_PLATFORM = "loan_platform"
    RATE_CALCULATOR = "rate_calculator"
    PARTNER_LENDER = "partner_lender"
    OTHER = "other"


class SourceRelationshipKind(StrEnum):
    FIRST_PARTY = "first_party"
    PROVIDER_HOSTED = "provider_hosted"
    PARTNER_PUBLISHED = "partner_published"


class OfferStatus(StrEnum):
    CANDIDATE = "candidate"
    QUARANTINED = "quarantined"
    APPROVED = "approved"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class LienPosition(StrEnum):
    FIRST = "first"
    SECOND = "second"
    UNKNOWN = "unknown"


class ConditionKind(StrEnum):
    AS_LOW_AS = "as_low_as"
    TIER = "tier"
    DISCOUNT = "discount"
    RELATIONSHIP = "relationship"
    AUTOPAY = "autopay"
    BORROWER_REQUIREMENT = "borrower_requirement"
    OTHER = "other"


class TierDimension(StrEnum):
    LTV = "ltv"
    CLTV = "cltv"
    CREDIT_SCORE = "credit_score"
    LOAN_AMOUNT = "loan_amount"
    TERM_MONTHS = "term_months"


class MatchState(StrEnum):
    MATCHES = "matches"
    CONFLICTS = "conflicts"
    UNSTATED = "unstated"


class CanonicalScenarioId(StrEnum):
    PURCHASE_30Y_FIXED = "purchase_30y_fixed"
    RATE_TERM_REFI_30Y_FIXED = "rate_term_refi_30y_fixed"
    FIXED_HOME_EQUITY_10Y = "fixed_home_equity_10y"
    HELOC = "heloc"


class ScenarioDimension(StrEnum):
    OCCUPANCY = "occupancy"
    PROPERTY_TYPE = "property_type"
    RATE_TYPE = "rate_type"
    TERM_MONTHS = "term_months"
    LTV = "ltv"
    CLTV = "cltv"
    LOAN_AMOUNT = "loan_amount"
    CREDIT_SCORE = "credit_score"
    LOCK_DAYS = "lock_days"
    POINTS = "points"
    CASH_OUT = "cash_out"
    RELATIONSHIP_DISCOUNT = "relationship_discount"


class Occupancy(StrEnum):
    OWNER_OCCUPIED = "owner_occupied"


class PropertyType(StrEnum):
    SINGLE_FAMILY = "single_family"


class EvidenceKind(StrEnum):
    HTML = "html"
    RENDERED_DOM = "rendered_dom"
    PDF = "pdf"
    IMAGE_OCR = "image_ocr"
    JSON = "json"


class PostIntroState(StrEnum):
    STATED = "stated"
    DERIVED = "derived"
    INCOMPLETE = "incomplete"


class ChangeKind(StrEnum):
    UNCHANGED = "unchanged"
    VALUE_ONLY = "value_only"
    STRUCTURAL = "structural"


class ReviewVerdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewSubject(StrEnum):
    OFFER = "offer"
    PRODUCT = "product"


class WithdrawalScope(StrEnum):
    OFFER = "offer"
    PRODUCT = "product"
    INSTITUTION = "institution"
    SNAPSHOT = "snapshot"


class FetchMethod(StrEnum):
    HTTP_GET = "http_get"
    RENDERED_BROWSER = "rendered_browser"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"

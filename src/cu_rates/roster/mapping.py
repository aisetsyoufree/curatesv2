"""Documented column mapping and status normalization for official NCUA credit union rosters.

The real NCUA FOIA/call-report/roster column layout will be confirmed when that
source approval is granted (T007/T021). The mapping is centralized here so column
names and status translations can be adjusted without modifying parser logic.
"""

from dataclasses import dataclass

from cu_rates.contracts.enums import InstitutionStatus


@dataclass(frozen=True)
class RosterColumnMapping:
    """Column header names expected in the roster CSV file."""

    charter_number: str = "Charter"
    legal_name: str = "Credit Union Name"
    state: str = "State"
    status: str = "Status"
    city: str = "City"
    closure_or_merger_date: str = "Date Inactive"
    successor_charter: str = "Successor Charter"
    source_as_of_date: str = "As of Date"


DEFAULT_NCUA_MAPPING = RosterColumnMapping()

# Official status translations into canonical InstitutionStatus
_RAW_STATUS_MAP: dict[str, InstitutionStatus] = {
    "active": InstitutionStatus.ACTIVE,
    "merged": InstitutionStatus.MERGED,
    "liquidated": InstitutionStatus.LIQUIDATED,
    "closed": InstitutionStatus.LIQUIDATED,
    "converted": InstitutionStatus.CONVERTED,
    "under_review": InstitutionStatus.UNDER_REVIEW,
    "under review": InstitutionStatus.UNDER_REVIEW,
}


def map_institution_status(raw_status: str) -> InstitutionStatus | None:
    """Normalize and map a source status string to InstitutionStatus."""
    clean = raw_status.strip().lower().replace("-", "_")
    return _RAW_STATUS_MAP.get(clean)

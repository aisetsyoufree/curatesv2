"""Documented column mapping and status normalization for official NCUA credit union rosters.

The real NCUA FOIA/call-report/roster column layout will be confirmed when that
source approval is granted (T007/T021). The mapping is centralized here so column
names and status translations can be adjusted without modifying parser logic.
"""

from dataclasses import dataclass
from datetime import datetime

from cu_rates.contracts.enums import InstitutionStatus


@dataclass(frozen=True)
class RosterColumnMapping:
    """Column header names expected in the roster CSV file.

    Some official files carry no status, closure date or successor column (the NCUA FOICU file
    lists the credit unions ACTIVE in a cycle and nothing else). For those, ``status`` is None
    and ``implied_status`` states what every listed row means; absence from a later file is
    then the only closure signal, and the importer already treats a verified charter that
    disappears as unresolved (suppressed pending human reconciliation), never as a closure.
    ``date_format`` is a ``strptime`` format when the file's dates are not ISO ``YYYY-MM-DD``.
    """

    charter_number: str = "Charter"
    legal_name: str = "Credit Union Name"
    state: str = "State"
    status: str | None = "Status"
    city: str = "City"
    closure_or_merger_date: str | None = "Date Inactive"
    successor_charter: str | None = "Successor Charter"
    source_as_of_date: str = "As of Date"
    implied_status: InstitutionStatus | None = None
    date_format: str | None = None

    def __post_init__(self) -> None:
        if (self.status is None) != (self.implied_status is not None):
            raise ValueError("a mapping has either a status column or an implied status, not both")

    def iso_date(self, raw: str) -> str | None:
        """The file's date as ISO ``YYYY-MM-DD``, or None when it does not parse."""
        if self.date_format is None:
            return raw
        try:
            return datetime.strptime(raw, self.date_format).strftime("%Y-%m-%d")
        except ValueError:
            return None


DEFAULT_NCUA_MAPPING = RosterColumnMapping()

# The real NCUA 5300 call-report quarterly data (FOICU.txt), confirmed against the 2026-06 cycle
# (issue #76): 4,299 credit unions nationwide, 121 with STATE == MA. It lists active credit
# unions only; CYCLE_DATE looks like "6/30/2026 0:00:00".
NCUA_FOICU_MAPPING = RosterColumnMapping(
    charter_number="CU_NUMBER",
    legal_name="CU_NAME",
    state="STATE",
    status=None,
    city="CITY",
    closure_or_merger_date=None,
    successor_charter=None,
    source_as_of_date="CYCLE_DATE",
    implied_status=InstitutionStatus.ACTIVE,
    date_format="%m/%d/%Y %H:%M:%S",
)

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

"""Repeatable, source-versioned credit union roster importer.

Parses official-style NCUA CSV files into canonical Institution contracts,
stores raw byte evidence in ArtifactStore, and tracks versioned imports in
the operational database with full provenance.
"""

import csv
import hashlib
import io
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Connection, Engine, insert, select, update

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import InstitutionStatus
from cu_rates.contracts.registry import Institution
from cu_rates.db.schema import institutions, roster_import_entries, roster_imports
from cu_rates.roster.mapping import (
    DEFAULT_NCUA_MAPPING,
    RosterColumnMapping,
    map_institution_status,
)


@dataclass(frozen=True)
class MalformedRow:
    """Record of a row that failed parsing or contract validation."""

    line_number: int
    raw_row: dict[str, str]
    reason: str


@dataclass(frozen=True)
class DuplicateConflict:
    """Record of conflicting duplicate rows sharing a charter number."""

    charter_number: int
    rows: tuple[dict[str, str], ...]
    differing_fields: tuple[str, ...]


@dataclass(frozen=True)
class RosterImportReport:
    """Detailed summary of a roster import run."""

    import_id: str
    artifact_id: str
    source_version: str
    source_as_of_date: str
    retrieved_at: datetime
    is_noop: bool
    total_rows: int = 0
    ma_rows: int = 0
    skipped_non_ma_count: int = 0
    skipped_non_ma_charters: tuple[str, ...] = field(default_factory=tuple)
    malformed_rows: tuple[MalformedRow, ...] = field(default_factory=tuple)
    collapsed_duplicate_charters: tuple[int, ...] = field(default_factory=tuple)
    rejected_duplicate_charters: tuple[int, ...] = field(default_factory=tuple)
    rejected_duplicates: tuple[DuplicateConflict, ...] = field(default_factory=tuple)
    inserted_charters: tuple[int, ...] = field(default_factory=tuple)
    updated_charters: tuple[int, ...] = field(default_factory=tuple)
    unchanged_charters: tuple[int, ...] = field(default_factory=tuple)
    mergers: tuple[int, ...] = field(default_factory=tuple)
    closures: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ParsedRow:
    charter_number: int
    legal_name: str
    status: InstitutionStatus
    successor_charter_number: int | None
    city: str | None
    closure_or_merger_date: str | None
    source_as_of_date: str
    raw_dict: dict[str, str]


class RosterImporter:
    """Imports NCUA credit union rosters into the operational database."""

    def __init__(
        self,
        engine_or_connection: Engine | Connection,
        artifact_store: ArtifactStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        mapping: RosterColumnMapping = DEFAULT_NCUA_MAPPING,
        source_url: str = "https://www.ncua.gov/data/credit-union-roster.csv",
    ) -> None:
        self._target = engine_or_connection
        self._store = artifact_store
        self._clock = clock
        self._mapping = mapping
        self._source_url = source_url

    def import_roster(
        self,
        csv_source: bytes | str | Path,
        *,
        source_version: str,
        source_as_of_date: str | None = None,
    ) -> RosterImportReport:
        """Import the roster CSV, recording evidence and updating institutions."""
        if isinstance(csv_source, bytes):
            raw_bytes = csv_source
        elif isinstance(csv_source, Path):
            raw_bytes = csv_source.read_bytes()
        else:
            raw_bytes = csv_source.encode("utf-8")

        if isinstance(self._target, Engine):
            with self._target.begin() as conn:
                return self._run(conn, raw_bytes, source_version, source_as_of_date)
        else:
            if self._target.in_transaction():
                return self._run(self._target, raw_bytes, source_version, source_as_of_date)
            with self._target.begin():
                return self._run(self._target, raw_bytes, source_version, source_as_of_date)

    def _run(
        self,
        conn: Connection,
        raw_bytes: bytes,
        source_version: str,
        source_as_of_date: str | None,
    ) -> RosterImportReport:
        artifact_id = hashlib.sha256(raw_bytes).hexdigest()

        # Idempotence: re-importing identical bytes is a no-op.
        existing_import = conn.execute(
            select(
                roster_imports.c.import_id,
                roster_imports.c.source_version,
                roster_imports.c.source_as_of_date,
                roster_imports.c.retrieved_at,
            ).where(roster_imports.c.artifact_id == artifact_id)
        ).first()

        if existing_import is not None:
            return RosterImportReport(
                import_id=existing_import.import_id,
                artifact_id=artifact_id,
                source_version=existing_import.source_version,
                source_as_of_date=existing_import.source_as_of_date,
                retrieved_at=existing_import.retrieved_at,
                is_noop=True,
            )

        now = self._clock()
        import_id = f"roster-import-{uuid4().hex}"

        # Preserve the raw file bytes in the ArtifactStore.
        self._store.put(
            raw_bytes,
            media_type="text/csv; charset=utf-8",
            source_url=self._source_url,
            fetched_at=now,
            http_status=200,
            fetch_run_id=import_id,
        )

        # Parse CSV
        text = raw_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        m = self._mapping
        required_headers = {m.charter_number, m.legal_name, m.state, m.status}
        if reader.fieldnames is None or not required_headers.issubset(set(reader.fieldnames)):
            missing = required_headers - set(reader.fieldnames or ())
            raise ValueError(f"CSV header missing required mapping columns: {sorted(missing)}")

        total_rows = 0
        ma_rows = 0
        skipped_non_ma_charters: list[str] = []
        malformed_rows: list[MalformedRow] = []
        parsed_rows_by_charter: dict[int, list[_ParsedRow]] = defaultdict(list)
        fallback_as_of_date = source_as_of_date or ""

        for line_num, row in enumerate(reader, start=2):
            total_rows += 1
            raw_state = row.get(m.state, "").strip().upper()
            if raw_state != "MA":
                charter_hint = row.get(m.charter_number, "").strip() or f"row-{line_num}"
                skipped_non_ma_charters.append(charter_hint)
                continue

            ma_rows += 1

            # Validate charter number
            raw_charter = row.get(m.charter_number, "").strip()
            try:
                charter_num = int(raw_charter)
                if not (0 < charter_num < 100_000_000):
                    raise ValueError
            except ValueError:
                malformed_rows.append(
                    MalformedRow(line_num, row, f"Invalid charter number: {raw_charter!r}")
                )
                continue

            # Validate legal name
            legal_name = row.get(m.legal_name, "").strip()
            if not legal_name:
                malformed_rows.append(MalformedRow(line_num, row, "Missing legal name"))
                continue

            # Validate status
            raw_status = row.get(m.status, "").strip()
            status = map_institution_status(raw_status)
            if status is None:
                malformed_rows.append(
                    MalformedRow(line_num, row, f"Unrecognized status: {raw_status!r}")
                )
                continue

            # Validate successor charter
            raw_succ = row.get(m.successor_charter, "").strip()
            successor_num: int | None = None
            if status is InstitutionStatus.MERGED:
                if not raw_succ:
                    malformed_rows.append(
                        MalformedRow(line_num, row, "Merged institution requires successor charter")
                    )
                    continue
                try:
                    successor_num = int(raw_succ)
                    if not (0 < successor_num < 100_000_000) or successor_num == charter_num:
                        raise ValueError
                except ValueError:
                    malformed_rows.append(
                        MalformedRow(
                            line_num,
                            row,
                            f"Invalid successor charter {raw_succ!r} for merged institution",
                        )
                    )
                    continue
            else:
                if raw_succ:
                    malformed_rows.append(
                        MalformedRow(
                            line_num,
                            row,
                            f"Non-merged institution cannot have successor charter {raw_succ!r}",
                        )
                    )
                    continue

            # As-of date
            row_date = row.get(m.source_as_of_date, "").strip() or fallback_as_of_date
            if not row_date:
                malformed_rows.append(MalformedRow(line_num, row, "Missing source as-of date"))
                continue

            if not fallback_as_of_date:
                fallback_as_of_date = row_date

            city = row.get(m.city, "").strip() or None
            date_inactive = row.get(m.closure_or_merger_date, "").strip() or None

            parsed_rows_by_charter[charter_num].append(
                _ParsedRow(
                    charter_number=charter_num,
                    legal_name=legal_name,
                    status=status,
                    successor_charter_number=successor_num,
                    city=city,
                    closure_or_merger_date=date_inactive,
                    source_as_of_date=row_date,
                    raw_dict=row,
                )
            )

        # Deduplicate charter numbers
        collapsed_duplicate_charters: list[int] = []
        rejected_duplicate_charters: list[int] = []
        rejected_duplicates: list[DuplicateConflict] = []
        valid_rows: list[_ParsedRow] = []

        for charter_num, rows in parsed_rows_by_charter.items():
            if len(rows) == 1:
                valid_rows.append(rows[0])
                continue

            # Check if all rows are identical across material columns
            first = rows[0]
            differing: set[str] = set()
            for other in rows[1:]:
                if other.legal_name != first.legal_name:
                    differing.add(m.legal_name)
                if other.status != first.status:
                    differing.add(m.status)
                if other.successor_charter_number != first.successor_charter_number:
                    differing.add(m.successor_charter)
                if other.city != first.city:
                    differing.add(m.city)
                if other.closure_or_merger_date != first.closure_or_merger_date:
                    differing.add(m.closure_or_merger_date)
                if other.source_as_of_date != first.source_as_of_date:
                    differing.add(m.source_as_of_date)

            if not differing:
                collapsed_duplicate_charters.append(charter_num)
                valid_rows.append(first)
            else:
                rejected_duplicate_charters.append(charter_num)
                rejected_duplicates.append(
                    DuplicateConflict(
                        charter_number=charter_num,
                        rows=tuple(r.raw_dict for r in rows),
                        differing_fields=tuple(sorted(differing)),
                    )
                )

        # Validate with Institution contract
        accepted_institutions: list[tuple[Institution, _ParsedRow]] = []
        for row_item in valid_rows:
            # Charter verified is NEVER set to True during roster import (T021 human decision).
            inst = Institution(
                schema_version=CONTRACT_VERSION,
                charter_number=row_item.charter_number,
                legal_name=row_item.legal_name,
                status=row_item.status,
                successor_charter_number=row_item.successor_charter_number,
                charter_verified=False,
            )
            accepted_institutions.append((inst, row_item))

        # Record import in operational database
        file_source_as_of_date = fallback_as_of_date or datetime.now(UTC).strftime("%Y-%m-%d")

        conn.execute(
            insert(roster_imports).values(
                import_id=import_id,
                schema_version=CONTRACT_VERSION,
                source_version=source_version,
                source_as_of_date=file_source_as_of_date,
                retrieved_at=now,
                artifact_id=artifact_id,
                row_count=total_rows,
                imported_count=len(accepted_institutions),
            )
        )

        inserted_charters: list[int] = []
        updated_charters: list[int] = []
        unchanged_charters: list[int] = []
        mergers: list[int] = []
        closures: list[int] = []

        for inst, row_item in accepted_institutions:
            # Append to roster_import_entries
            conn.execute(
                insert(roster_import_entries).values(
                    import_id=import_id,
                    charter_number=inst.charter_number,
                    schema_version=CONTRACT_VERSION,
                    legal_name=inst.legal_name,
                    status=inst.status.value,
                    successor_charter_number=inst.successor_charter_number,
                    city=row_item.city,
                    closure_or_merger_date=row_item.closure_or_merger_date,
                    source_as_of_date=row_item.source_as_of_date,
                )
            )

            if inst.status is InstitutionStatus.MERGED:
                mergers.append(inst.charter_number)
            elif inst.status is InstitutionStatus.LIQUIDATED:
                closures.append(inst.charter_number)

            # Insert or update institutions table
            existing = conn.execute(
                select(
                    institutions.c.legal_name,
                    institutions.c.status,
                    institutions.c.successor_charter_number,
                    institutions.c.charter_verified,
                ).where(institutions.c.charter_number == inst.charter_number)
            ).first()

            if existing is None:
                conn.execute(
                    insert(institutions).values(
                        charter_number=inst.charter_number,
                        schema_version=CONTRACT_VERSION,
                        legal_name=inst.legal_name,
                        status=inst.status.value,
                        successor_charter_number=inst.successor_charter_number,
                        charter_verified=0,  # NEVER set to True
                    )
                )
                inserted_charters.append(inst.charter_number)
            else:
                # Update if material fields changed
                if (
                    existing.legal_name != inst.legal_name
                    or existing.status != inst.status.value
                    or existing.successor_charter_number != inst.successor_charter_number
                ):
                    conn.execute(
                        update(institutions)
                        .where(institutions.c.charter_number == inst.charter_number)
                        .values(
                            legal_name=inst.legal_name,
                            status=inst.status.value,
                            successor_charter_number=inst.successor_charter_number,
                            # Preserve existing charter_verified
                        )
                    )
                    updated_charters.append(inst.charter_number)
                else:
                    unchanged_charters.append(inst.charter_number)

        return RosterImportReport(
            import_id=import_id,
            artifact_id=artifact_id,
            source_version=source_version,
            source_as_of_date=file_source_as_of_date,
            retrieved_at=now,
            is_noop=False,
            total_rows=total_rows,
            ma_rows=ma_rows,
            skipped_non_ma_count=len(skipped_non_ma_charters),
            skipped_non_ma_charters=tuple(skipped_non_ma_charters),
            malformed_rows=tuple(malformed_rows),
            collapsed_duplicate_charters=tuple(collapsed_duplicate_charters),
            rejected_duplicate_charters=tuple(rejected_duplicate_charters),
            rejected_duplicates=tuple(rejected_duplicates),
            inserted_charters=tuple(inserted_charters),
            updated_charters=tuple(updated_charters),
            unchanged_charters=tuple(unchanged_charters),
            mergers=tuple(mergers),
            closures=tuple(closures),
        )


def import_roster(
    csv_source: bytes | str | Path,
    *,
    engine_or_connection: Engine | Connection,
    artifact_store: ArtifactStore,
    source_version: str,
    source_as_of_date: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    mapping: RosterColumnMapping = DEFAULT_NCUA_MAPPING,
    source_url: str = "https://www.ncua.gov/data/credit-union-roster.csv",
) -> RosterImportReport:
    """Convenience helper to import a roster using RosterImporter."""
    importer = RosterImporter(
        engine_or_connection=engine_or_connection,
        artifact_store=artifact_store,
        clock=clock,
        mapping=mapping,
        source_url=source_url,
    )
    return importer.import_roster(
        csv_source,
        source_version=source_version,
        source_as_of_date=source_as_of_date,
    )

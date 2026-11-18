"""Repeatable, source-versioned credit union roster importer.

Parses official-style NCUA CSV files into canonical Institution contracts,
stores raw byte evidence in ArtifactStore, and tracks versioned imports in
the operational database with full provenance.

The importer never sets ``charter_verified``. A newer well-formed snapshot may
update an unverified charter. An older snapshot, a verified identity change,
or an unresolved charter is reported and is not applied as a silent fact.
"""

import csv
import hashlib
import io
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Connection, Engine, insert, select, update

from cu_rates.artifact_store import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import InstitutionStatus, WithdrawalScope
from cu_rates.contracts.registry import Institution
from cu_rates.db.schema import (
    institutions,
    roster_import_entries,
    roster_imports,
    withdrawal_lifts,
)
from cu_rates.pipeline.trusted import insert_withdrawal
from cu_rates.roster.mapping import (
    DEFAULT_NCUA_MAPPING,
    RosterColumnMapping,
    map_institution_status,
)
from cu_rates.snapshot import Denylist

# Budgets keep one roster file from exhausting memory. They are far above the
# committed fixtures and are reported instead of raising.
MAX_ROSTER_BYTES = 2_000_000
MAX_ROSTER_ROWS = 20_000
MAX_FIELD_LENGTH = 500

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOSED_SUCCESSORS = frozenset({InstitutionStatus.LIQUIDATED, InstitutionStatus.CONVERTED})
_DATED_STATUSES = frozenset({InstitutionStatus.MERGED, InstitutionStatus.LIQUIDATED})
_UNDATED_STATUSES = frozenset(
    {
        InstitutionStatus.ACTIVE,
        InstitutionStatus.UNDER_REVIEW,
        InstitutionStatus.CONVERTED,
    }
)
# Statuses that make an institution ineligible for publication.  A roster
# import reporting an institution in one of these clears eligibility
# and suppresses existing offers pending human reconciliation.
_INELIGIBLE_STATUSES = frozenset(
    {
        InstitutionStatus.MERGED,
        InstitutionStatus.LIQUIDATED,
        InstitutionStatus.CONVERTED,
    }
)

_ROSTER_PREFIX = "roster-"


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
    quarantined_charters: tuple[int, ...] = field(default_factory=tuple)
    suppressed_charters: tuple[int, ...] = field(default_factory=tuple)
    review_charters: tuple[int, ...] = field(default_factory=tuple)
    stale_charters: tuple[int, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


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


@dataclass(frozen=True)
class _Attempt:
    line_number: int
    raw: dict[str, str]
    parsed: _ParsedRow | None
    reason: str | None


@dataclass
class _Classified:
    total_rows: int = 0
    ma_rows: int = 0
    skipped_non_ma: list[str] = field(default_factory=list)
    malformed_rows: list[MalformedRow] = field(default_factory=list)
    collapsed: list[int] = field(default_factory=list)
    rejected_charters: list[int] = field(default_factory=list)
    rejected: list[DuplicateConflict] = field(default_factory=list)
    valid_rows: list[tuple[Institution, _ParsedRow]] = field(default_factory=list)
    unresolved_charters: set[int] = field(default_factory=set)
    inactive_rows: dict[int, _ParsedRow] = field(default_factory=dict)
    file_as_of: str | None = None
    errors: tuple[str, ...] = ()


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
        denylist: Denylist | None = None,
    ) -> None:
        self._denylist = denylist
        self._target = engine_or_connection
        self._store = artifact_store
        self._clock = clock
        self._mapping = mapping
        self._source_url = source_url
        self._failed_artifacts: set[str] = set()

    @property
    def failed_artifacts(self) -> frozenset[str]:
        """Artifact hashes whose import rolled back. A later attempt may retry."""
        return frozenset(self._failed_artifacts)

    def import_roster(
        self,
        csv_source: bytes | str | Path,
        *,
        source_version: str,
        source_as_of_date: str | None = None,
    ) -> RosterImportReport:
        """Import the roster CSV, recording evidence and updating institutions."""
        if not source_version.strip():
            raise ValueError("source_version is required")
        if source_as_of_date is not None and _parse_date(source_as_of_date) is None:
            raise ValueError(f"invalid source_as_of_date: {source_as_of_date!r}")
        raw_bytes = _read_source(csv_source)
        digest = hashlib.sha256(raw_bytes).hexdigest()

        def invoke(conn: Connection) -> RosterImportReport:
            try:
                with conn.begin_nested():
                    return self._run(conn, raw_bytes, source_version, source_as_of_date)
            except Exception:
                self._failed_artifacts.add(digest)
                raise

        if isinstance(self._target, Engine):
            with self._target.begin() as conn:
                return invoke(conn)
        if self._target.in_transaction():
            return invoke(self._target)
        with self._target.begin():
            return invoke(self._target)

    def _run(
        self,
        conn: Connection,
        raw_bytes: bytes,
        source_version: str,
        source_as_of_date: str | None,
    ) -> RosterImportReport:
        artifact_id = hashlib.sha256(raw_bytes).hexdigest()
        existing = self._existing_import(
            conn, artifact_id, raw_bytes, source_version, source_as_of_date
        )
        now = self._clock()
        if len(raw_bytes) > MAX_ROSTER_BYTES:
            return _rejected(
                artifact_id,
                source_version,
                source_as_of_date or "",
                now,
                errors=(f"roster file exceeds {MAX_ROSTER_BYTES} bytes",),
            )

        try:
            headers, table_rows, overflow = _read_table(raw_bytes)
        except csv.Error as exc:
            raise ValueError("unterminated quote or unreadable roster CSV") from exc
        if overflow:
            return _rejected(
                artifact_id,
                source_version,
                source_as_of_date or "",
                now,
                errors=(f"roster CSV exceeds {MAX_ROSTER_ROWS} rows",),
                total_rows=len(table_rows),
            )

        required = {
            name
            for name in (
                self._mapping.charter_number,
                self._mapping.legal_name,
                self._mapping.state,
                self._mapping.status,
                self._mapping.source_as_of_date,
            )
            if name is not None
        }
        if not required.issubset(headers):
            missing = sorted(required - set(headers))
            raise ValueError(f"CSV header missing required mapping columns: {missing}")

        parsed = _classify_rows(headers, table_rows, self._mapping, source_as_of_date)
        if parsed.errors:
            return _rejected(
                artifact_id,
                source_version,
                source_as_of_date or "",
                now,
                errors=parsed.errors,
                total_rows=parsed.total_rows,
                malformed_rows=tuple(parsed.malformed_rows),
            )

        file_as_of = parsed.file_as_of or source_as_of_date
        if file_as_of is None and parsed.malformed_rows:
            return _rejected(
                artifact_id,
                source_version,
                source_as_of_date or "",
                now,
                errors=parsed.errors,
                total_rows=parsed.total_rows,
                malformed_rows=tuple(parsed.malformed_rows),
            )
        if file_as_of is None:
            raise ValueError("source_as_of_date is required when the file states no as-of date")

        import_id = existing.import_id if existing else f"roster-import-{uuid4().hex}"
        if existing is None:
            self._store.put(
                raw_bytes,
                media_type="text/csv; charset=utf-8",
                source_url=self._source_url,
                fetched_at=now,
                http_status=200,
                fetch_run_id=import_id,
            )

        latest_dates = conn.execute(select(roster_imports.c.source_as_of_date)).scalars().all()
        latest_as_of = max(latest_dates) if latest_dates else None
        stale_file = latest_as_of is not None and file_as_of < latest_as_of
        stored_dates: dict[int, str] = {}
        for charter, as_of in conn.execute(
            select(
                roster_import_entries.c.charter_number,
                roster_import_entries.c.source_as_of_date,
            )
        ).all():
            known_as_of = stored_dates.get(int(charter))
            if known_as_of is None or as_of > known_as_of:
                stored_dates[int(charter)] = str(as_of)

        verified = {
            int(row.charter_number)
            for row in conn.execute(
                select(institutions.c.charter_number).where(institutions.c.charter_verified == 1)
            ).all()
        }
        # A charter whose roster suppression a human has not lifted is still awaiting
        # reconciliation: a LATER import must not apply an unreviewed identity either.
        awaiting_reconciliation = (
            set()
            if self._denylist is None
            else {
                int(e.target_id)
                for e in self._denylist.entries()
                if e.scope is WithdrawalScope.INSTITUTION
                and e.withdrawal_id.startswith(_ROSTER_PREFIX)
                and e.lifted_at is None
                and e.target_id.isdigit()
            }
        )

        inserted: list[int] = []
        updated: list[int] = []
        unchanged: list[int] = []
        mergers: list[int] = []
        closures: list[int] = []
        review: list[int] = []
        stale: list[int] = []
        suppressed: list[int] = []
        clean: set[int] = set()

        # Inactivity is credible independently of successor identity. Persist the
        # entire batch's independent protection before ANY operational DB mutation:
        # even the first roster/withdrawal insert may fail. Stale reports retain
        # their existing no-change guard.
        suppressions: dict[int, WithdrawalEntry] = {}
        valid_charters = {inst.charter_number for inst, _ in parsed.valid_rows}
        if not stale_file:
            for charter, row in parsed.inactive_rows.items():
                if row.source_as_of_date < stored_dates.get(charter, row.source_as_of_date):
                    continue
                known = conn.execute(
                    select(institutions.c.charter_number).where(
                        institutions.c.charter_number == charter
                    )
                ).first()
                if known is None and charter not in valid_charters:
                    continue
                entry = self._prepare_suppression(
                    charter, f"reported {row.status.value}", artifact_id, now
                )
                if entry is not None:
                    suppressions[charter] = entry

        # A verified charter that is no longer on this roster (or whose identity cannot be
        # resolved) loses its verification too. That event must be datable, so a review
        # decided before it cannot vouch for a later re-flag, and its already-published
        # offers must stop being shown: same independent protection, same ordering.
        departed: set[int] = set()
        if not stale_file:
            unresolved = (verified - valid_charters) | (parsed.unresolved_charters & verified)
            for charter in sorted(unresolved - set(suppressions)):
                entry = self._prepare_suppression(
                    charter,
                    "is no longer verifiable on the Massachusetts roster (absent or unresolved)",
                    artifact_id,
                    now,
                )
                if entry is not None:
                    suppressions[charter] = entry
                    departed.add(charter)

        # All DB changes share the import's transaction/savepoint. Independent
        # suppression deliberately survives rollback so existing snapshots stay safe.
        if existing is None:
            conn.execute(
                insert(roster_imports).values(
                    import_id=import_id,
                    schema_version=CONTRACT_VERSION,
                    source_version=source_version,
                    source_as_of_date=file_as_of,
                    retrieved_at=now,
                    artifact_id=artifact_id,
                    row_count=parsed.total_rows,
                    imported_count=len(parsed.valid_rows),
                )
            )

        for charter, entry in suppressions.items():
            assert self._denylist is not None  # Required by _prepare_suppression.
            insert_withdrawal(conn, entry, denylist=self._denylist)
            conn.execute(
                update(institutions)
                .where(institutions.c.charter_number == charter)
                .values(charter_verified=0)
            )
            suppressed.append(charter)
            review.append(charter)

        if existing is not None:
            return replace(
                existing,
                suppressed_charters=tuple(suppressed),
                review_charters=tuple(dict.fromkeys(review)),
            )

        for inst, row_item in parsed.valid_rows:
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
            clean.add(inst.charter_number)
            if stale_file or (
                (prior := stored_dates.get(inst.charter_number)) is not None
                and row_item.source_as_of_date < prior
            ):
                stale.append(inst.charter_number)
                continue

            current = conn.execute(
                select(
                    institutions.c.legal_name,
                    institutions.c.status,
                    institutions.c.successor_charter_number,
                    institutions.c.charter_verified,
                ).where(institutions.c.charter_number == inst.charter_number)
            ).first()
            changed = current is not None and (
                current.legal_name != inst.legal_name
                or current.status != inst.status.value
                or current.successor_charter_number != inst.successor_charter_number
            )

            # Use the verification state from BEFORE this import's suppression cleared it:
            # a verified record's changed identity is queued for review, never applied.
            if (
                current is not None
                and (
                    inst.charter_number in verified
                    or inst.charter_number in awaiting_reconciliation
                )
                and changed
            ):
                review.append(inst.charter_number)
                continue
            if current is None:
                conn.execute(
                    insert(institutions).values(
                        charter_number=inst.charter_number,
                        schema_version=CONTRACT_VERSION,
                        legal_name=inst.legal_name,
                        status=inst.status.value,
                        successor_charter_number=inst.successor_charter_number,
                        charter_verified=0,
                    )
                )
                inserted.append(inst.charter_number)
            elif changed:
                conn.execute(
                    update(institutions)
                    .where(institutions.c.charter_number == inst.charter_number)
                    .values(
                        legal_name=inst.legal_name,
                        status=inst.status.value,
                        successor_charter_number=inst.successor_charter_number,
                    )
                )
                updated.append(inst.charter_number)
            else:
                unchanged.append(inst.charter_number)

        # Departed charters were suppressed and unverified above; report them as quarantined.
        quarantined: list[int] = sorted(departed)

        return RosterImportReport(
            import_id=import_id,
            artifact_id=artifact_id,
            source_version=source_version,
            source_as_of_date=file_as_of,
            retrieved_at=now,
            is_noop=False,
            total_rows=parsed.total_rows,
            ma_rows=parsed.ma_rows,
            skipped_non_ma_count=len(parsed.skipped_non_ma),
            skipped_non_ma_charters=tuple(parsed.skipped_non_ma),
            malformed_rows=tuple(parsed.malformed_rows),
            collapsed_duplicate_charters=tuple(parsed.collapsed),
            rejected_duplicate_charters=tuple(parsed.rejected_charters),
            rejected_duplicates=tuple(parsed.rejected),
            inserted_charters=tuple(inserted),
            updated_charters=tuple(updated),
            unchanged_charters=tuple(unchanged),
            mergers=tuple(mergers),
            closures=tuple(closures),
            quarantined_charters=tuple(quarantined),
            suppressed_charters=tuple(suppressed),
            review_charters=tuple(dict.fromkeys(review)),
            stale_charters=tuple(stale),
            errors=parsed.errors,
        )

    def _prepare_suppression(
        self,
        charter: int,
        detail: str,
        artifact_id: str,
        now: datetime,
    ) -> WithdrawalEntry | None:
        if self._denylist is None:
            raise ValueError("a denylist is required to import a change that clears verification")
        withdrawal_id = f"{_ROSTER_PREFIX}{artifact_id}.suppress.{charter}"
        entries = self._denylist.entries()
        # A retry must reuse every immutable field, especially the original time.
        # Every roster artifact owns its own deterministic entry: a human lift of one
        # artifact's evidence must never be undone by replaying another artifact.
        entry = next((e for e in entries if e.withdrawal_id == withdrawal_id), None)
        if entry is not None and entry.lifted_at is not None:
            # A human already reconciled this exact evidence. Replaying it must
            # neither re-suppress the institution nor clear its verification.
            return None
        if entry is None:
            entry = WithdrawalEntry(
                schema_version=CONTRACT_VERSION,
                withdrawal_id=withdrawal_id,
                scope=WithdrawalScope.INSTITUTION,
                target_id=str(charter),
                actor="roster-importer",
                withdrawn_at=now,
                reason=f"Institution {charter} {detail} in roster artifact {artifact_id}",
            )
        self._denylist.withdraw(entry)
        return entry

    def _existing_import(
        self,
        conn: Connection,
        artifact_id: str,
        raw_bytes: bytes,
        source_version: str,
        source_as_of_date: str | None,
    ) -> RosterImportReport | None:
        existing = conn.execute(
            select(
                roster_imports.c.import_id,
                roster_imports.c.source_version,
                roster_imports.c.source_as_of_date,
                roster_imports.c.retrieved_at,
            ).where(roster_imports.c.artifact_id == artifact_id)
        ).first()
        if existing is None:
            return None
        if source_version != existing.source_version or (
            source_as_of_date is not None and source_as_of_date != existing.source_as_of_date
        ):
            raise ValueError("replay metadata conflicts with the stored roster import")
        try:
            stored = self._store.read_body(artifact_id)
            observation = self._store.get_observation(str(existing.import_id), self._source_url)
        except (ArtifactNotFoundError, ArtifactCorruptError) as exc:
            raise ValueError("stored roster evidence is missing or does not match") from exc
        if (
            stored != raw_bytes
            or observation.artifact_id != artifact_id
            or observation.byte_length != len(raw_bytes)
        ):
            raise ValueError("stored roster evidence is missing or does not match")
        return RosterImportReport(
            import_id=existing.import_id,
            artifact_id=artifact_id,
            source_version=existing.source_version,
            source_as_of_date=existing.source_as_of_date,
            retrieved_at=existing.retrieved_at,
            is_noop=True,
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
    denylist: Denylist | None = None,
) -> RosterImportReport:
    """Convenience helper to import a roster using RosterImporter."""
    importer = RosterImporter(
        engine_or_connection=engine_or_connection,
        artifact_store=artifact_store,
        clock=clock,
        mapping=mapping,
        source_url=source_url,
        denylist=denylist,
    )
    return importer.import_roster(
        csv_source,
        source_version=source_version,
        source_as_of_date=source_as_of_date,
    )


def _rejected(
    artifact_id: str,
    source_version: str,
    source_as_of_date: str,
    now: datetime,
    *,
    errors: tuple[str, ...],
    total_rows: int = 0,
    malformed_rows: tuple[MalformedRow, ...] = (),
) -> RosterImportReport:
    return RosterImportReport(
        import_id="",
        artifact_id=artifact_id,
        source_version=source_version,
        source_as_of_date=source_as_of_date,
        retrieved_at=now,
        is_noop=False,
        total_rows=total_rows,
        malformed_rows=malformed_rows,
        errors=errors,
    )


def _read_source(csv_source: bytes | str | Path) -> bytes:
    if isinstance(csv_source, bytes):
        return csv_source
    if isinstance(csv_source, Path):
        return csv_source.read_bytes()
    return csv_source.encode("utf-8")


def _read_table(raw_bytes: bytes) -> tuple[list[str], list[tuple[int, list[str]]], bool]:
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("roster CSV has no header") from exc
    headers = [cell.strip() for cell in header]
    if any(not name for name in headers):
        raise ValueError("roster CSV header has an empty column name")
    if len(headers) != len(set(headers)):
        raise ValueError("roster CSV header has duplicate column names")
    rows: list[tuple[int, list[str]]] = []
    overflow = False
    for line_number, cells in enumerate(reader, start=2):
        rows.append((line_number, cells))
        if len(rows) > MAX_ROSTER_ROWS:
            overflow = True
            break
    return headers, rows, overflow


def _classify_rows(
    headers: list[str],
    table_rows: list[tuple[int, list[str]]],
    mapping: RosterColumnMapping,
    declared_as_of: str | None,
) -> _Classified:
    result = _Classified()
    if len(table_rows) > MAX_ROSTER_ROWS:
        result.errors = (f"roster CSV exceeds {MAX_ROSTER_ROWS} rows",)
        result.total_rows = len(table_rows)
        return result

    attempts: dict[int, list[_Attempt]] = defaultdict(list)
    non_ma: set[int] = set()
    for line_number, cells in table_rows:
        result.total_rows += 1
        if len(cells) != len(headers):
            kind = "short" if len(cells) < len(headers) else "surplus"
            result.malformed_rows.append(
                MalformedRow(
                    line_number,
                    _partial_row(headers, cells),
                    f"{kind} row has {len(cells)} cells; expected {len(headers)}",
                )
            )
            charter = _charter_hint(headers, cells, mapping)
            if charter is not None:
                result.unresolved_charters.add(charter)
                attempts[charter].append(
                    _Attempt(line_number, _partial_row(headers, cells), None, f"{kind} row")
                )
            continue
        if any(len(cell) > MAX_FIELD_LENGTH for cell in cells):
            row = dict(zip(headers, cells, strict=True))
            result.malformed_rows.append(
                MalformedRow(line_number, row, f"field exceeds {MAX_FIELD_LENGTH} characters")
            )
            charter = _optional_charter(row.get(mapping.charter_number, ""))
            if charter is not None:
                result.unresolved_charters.add(charter)
                attempts[charter].append(_Attempt(line_number, row, None, "oversized field"))
            continue
        row = {key: value.strip() for key, value in zip(headers, cells, strict=True)}
        state = row.get(mapping.state, "").upper()
        if state != "MA":
            hint = row.get(mapping.charter_number, "") or f"row-{line_number}"
            result.skipped_non_ma.append(hint)
            charter = _optional_charter(hint)
            if charter is not None:
                non_ma.add(charter)
            continue
        result.ma_rows += 1
        # Successor errors cannot invalidate a credible inactivity report.
        inactive, _ = _parse_ma_row(row, mapping, line_number, validate_successor=False)
        if inactive is not None and inactive.status in _INELIGIBLE_STATUSES:
            result.inactive_rows.setdefault(inactive.charter_number, inactive)
        parsed, reason = _parse_ma_row(row, mapping, line_number)
        if parsed is None:
            result.malformed_rows.append(MalformedRow(line_number, row, reason or "invalid row"))
            charter = _optional_charter(row.get(mapping.charter_number, ""))
            if charter is not None:
                result.unresolved_charters.add(charter)
                attempts[charter].append(_Attempt(line_number, row, None, reason))
            continue
        attempts[parsed.charter_number].append(_Attempt(line_number, row, parsed, None))

    collapsed: list[int] = []
    rejected_charters: list[int] = []
    rejected: list[DuplicateConflict] = []
    chosen: list[_ParsedRow] = []
    for charter, group in attempts.items():
        parsed_rows = [item.parsed for item in group if item.parsed is not None]
        failed = [item for item in group if item.parsed is None]
        if failed and len(group) > 1:
            rejected_charters.append(charter)
            rejected.append(
                DuplicateConflict(
                    charter_number=charter,
                    rows=tuple(item.raw for item in group),
                    differing_fields=("malformed",),
                )
            )
            result.unresolved_charters.add(charter)
            continue
        if len(parsed_rows) > 1:
            fields = _difference_fields(parsed_rows, mapping)
            if fields:
                rejected_charters.append(charter)
                rejected.append(
                    DuplicateConflict(
                        charter_number=charter,
                        rows=tuple(item.raw for item in group),
                        differing_fields=fields,
                    )
                )
                result.unresolved_charters.add(charter)
            else:
                collapsed.append(charter)
                chosen.append(parsed_rows[0])
            continue
        if len(parsed_rows) == 1:
            chosen.append(parsed_rows[0])

    chosen = _without_bad_successors(chosen, result)
    dates = {row.source_as_of_date for row in chosen} | {
        row.source_as_of_date for row in result.inactive_rows.values()
    }
    if declared_as_of is not None and dates and dates != {declared_as_of}:
        result.errors = ("row as-of dates do not match the declared source_as_of_date",)
    elif len(dates) > 1:
        result.errors = ("roster rows use more than one as-of date",)
    elif len(dates) == 1:
        result.file_as_of = next(iter(dates))
    elif declared_as_of is not None:
        result.file_as_of = declared_as_of

    if not result.errors:
        for parsed_row in chosen:
            result.valid_rows.append((_institution(parsed_row), parsed_row))
    result.collapsed = collapsed
    result.rejected_charters = rejected_charters
    result.rejected = rejected
    result.unresolved_charters.update(non_ma)
    return result


def _parse_ma_row(
    row: dict[str, str],
    mapping: RosterColumnMapping,
    line_number: int,
    *,
    validate_successor: bool = True,
) -> tuple[_ParsedRow | None, str | None]:
    del line_number
    raw_charter = row.get(mapping.charter_number, "")
    charter = _optional_charter(raw_charter)
    if charter is None:
        return None, f"Invalid charter number: {raw_charter!r}"
    legal_name = row.get(mapping.legal_name, "")
    if not legal_name:
        return None, "Missing legal name"
    if mapping.status is None:
        status = mapping.implied_status  # the file lists active institutions only
        raw_status = ""
    else:
        raw_status = row.get(mapping.status, "")
        status = map_institution_status(raw_status)
    if status is None:
        return None, f"Unrecognized status: {raw_status!r}"
    raw_successor = (
        "" if mapping.successor_charter is None else row.get(mapping.successor_charter, "")
    )
    successor: int | None = None
    if validate_successor and status is InstitutionStatus.MERGED:
        successor = _optional_charter(raw_successor)
        if successor is None or successor == charter:
            return None, (
                "Merged institution requires successor charter"
                if not raw_successor
                else f"Invalid successor charter {raw_successor!r} for merged institution"
            )
    elif validate_successor and raw_successor:
        return None, f"Non-merged institution cannot have successor charter {raw_successor!r}"
    raw_as_of = row.get(mapping.source_as_of_date, "")
    if not raw_as_of:
        return None, "Missing source as-of date"
    as_of = mapping.iso_date(raw_as_of)
    if as_of is None or _parse_date(as_of) is None:
        return None, f"Invalid as-of date: {raw_as_of!r}"
    raw_inactive = (
        ""
        if mapping.closure_or_merger_date is None
        else row.get(mapping.closure_or_merger_date, "")
    )
    inactive: str | None
    if raw_inactive:
        inactive = mapping.iso_date(raw_inactive)
        if inactive is None or _parse_date(inactive) is None:
            return None, f"Invalid inactive date: {raw_inactive!r}"
    else:
        inactive = None
    if status in _UNDATED_STATUSES and inactive is not None:
        return None, "active institution cannot carry an inactive date"
    if status in _DATED_STATUSES and inactive is None:
        return None, "inactive institution requires an inactive date"
    return (
        _ParsedRow(
            charter_number=charter,
            legal_name=legal_name,
            status=status,
            successor_charter_number=successor,
            city=row.get(mapping.city, "") or None,
            closure_or_merger_date=inactive,
            source_as_of_date=as_of,
            raw_dict=row,
        ),
        None,
    )


def _without_bad_successors(rows: list[_ParsedRow], result: _Classified) -> list[_ParsedRow]:
    by_charter = {row.charter_number: row for row in rows}
    bad: set[int] = set()

    def walk(start: int) -> bool:
        seen: list[int] = []
        visiting: set[int] = set()
        current = start
        while current in by_charter and by_charter[current].successor_charter_number is not None:
            if current in visiting:
                bad.update(seen[seen.index(current) :])
                return True
            visiting.add(current)
            seen.append(current)
            successor = by_charter[current].successor_charter_number
            if successor is None:
                break
            current = successor
        return False

    for charter, row in by_charter.items():
        if row.status is InstitutionStatus.MERGED:
            walk(charter)
    changed = True
    while changed:
        changed = False
        for row in by_charter.values():
            if row.charter_number in bad or row.status is not InstitutionStatus.MERGED:
                continue
            successor = row.successor_charter_number
            target = by_charter.get(successor or -1)
            if successor in bad or (target is not None and target.status in _CLOSED_SUCCESSORS):
                bad.add(row.charter_number)
                changed = True
    kept: list[_ParsedRow] = []
    for row in rows:
        if row.charter_number not in bad:
            kept.append(row)
            continue
        result.malformed_rows.append(
            MalformedRow(
                0,
                row.raw_dict,
                "merger cycle or successor chain is not a continuing institution",
            )
        )
        result.unresolved_charters.add(row.charter_number)
    return kept


def _institution(row: _ParsedRow) -> Institution:
    return Institution(
        schema_version=CONTRACT_VERSION,
        charter_number=row.charter_number,
        legal_name=row.legal_name,
        status=row.status,
        successor_charter_number=row.successor_charter_number,
        charter_verified=False,
    )


def _difference_fields(rows: list[_ParsedRow], mapping: RosterColumnMapping) -> tuple[str, ...]:
    if len(rows) < 2:
        return ()
    first = rows[0]
    fields: set[str] = set()
    for other in rows[1:]:
        for differs, column in (
            (other.legal_name != first.legal_name, mapping.legal_name),
            (other.status != first.status, mapping.status),
            (
                other.successor_charter_number != first.successor_charter_number,
                mapping.successor_charter,
            ),
            (other.city != first.city, mapping.city),
            (
                other.closure_or_merger_date != first.closure_or_merger_date,
                mapping.closure_or_merger_date,
            ),
            (other.source_as_of_date != first.source_as_of_date, mapping.source_as_of_date),
        ):
            if differs and column is not None:
                fields.add(column)
    return tuple(sorted(fields))


def _partial_row(headers: list[str], cells: list[str]) -> dict[str, str]:
    width = min(len(headers), len(cells))
    row = {headers[index]: cells[index] for index in range(width)}
    if len(cells) > len(headers):
        row["surplus"] = ",".join(cells[len(headers) :])
    return row


def _charter_hint(headers: list[str], cells: list[str], mapping: RosterColumnMapping) -> int | None:
    if mapping.charter_number not in headers:
        return None
    index = headers.index(mapping.charter_number)
    if index >= len(cells):
        return None
    return _optional_charter(cells[index])


def _optional_charter(raw: str) -> int | None:
    text = raw.strip()
    if not text or not text.isdigit():
        return None
    number = int(text)
    if 0 < number < 100_000_000:
        return number
    return None


def _parse_date(value: str) -> str | None:
    if _DATE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def lift_roster_suppressions(
    conn: Connection, denylist: Denylist, charter: int, *, at: datetime, actor: str
) -> tuple[str, ...]:
    """Human reconciliation: lift EVERY in-force roster suppression for one charter.

    Each roster artifact owns its own entry, so restoring an institution means lifting
    all of them, in the independent ledger and in the operational record. This restores
    nothing else: ``charter_verified`` and the institution's status stay as they are
    until identity is separately re-verified by a human.
    """
    lifted: list[str] = []
    for entry in denylist.entries():
        if (
            entry.scope is WithdrawalScope.INSTITUTION
            and entry.target_id == str(charter)
            and entry.withdrawal_id.startswith(_ROSTER_PREFIX)
            and entry.lifted_at is None
        ):
            denylist.lift(entry.withdrawal_id, at=at, actor=actor)
            already = conn.execute(
                select(withdrawal_lifts.c.withdrawal_id).where(
                    withdrawal_lifts.c.withdrawal_id == entry.withdrawal_id
                )
            ).first()
            if already is None:
                conn.execute(
                    insert(withdrawal_lifts).values(
                        withdrawal_id=entry.withdrawal_id, lifted_at=at, lifted_by=actor
                    )
                )
            lifted.append(entry.withdrawal_id)
    return tuple(lifted)

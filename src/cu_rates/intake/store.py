"""The correction-intake store: one small private SQLite file, separate from every other store.

MASTER.md section 5 (Corrections, disputes, and emergency withdrawal) requires that each request
is recorded as an append-only case with its received time, reporter category, affected entity,
evidence, decision, actor and resolution time; that the reporter's reply contact lives only in a
restricted, mutable field that is deleted 90 days after resolution and is never copied into the
append-only history; and that unsolicited personal data can be removed. This module is exactly
that, and nothing more. It never touches the operational database, the artifact store, or the
public snapshot: the public web process may write here and can reach nothing else of value.

Design notes:
* ``cases`` and ``case_events`` are append-only (database triggers). The single exception is
  redaction: a reviewer may blank the free-text columns of a case (and only those) so that
  unsolicited personal data can be removed without rewriting history.
* ``reporter_contacts`` is the only mutable table. Its ``contact`` is set to NULL by
  ``purge_expired_contacts`` once ``delete_after`` has passed.
* Case identifiers are random capabilities (128 bits, lower-case hex). A reporter may quote
  one; nothing public ever lists or reveals a case.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

INTAKE_SCHEMA_VERSION = "1"
CONTACT_RETENTION = timedelta(days=90)
REDACTED = "[redacted]"
MAX_REASON = 2000
MAX_FIELD = 200
MAX_URL = 2000
MAX_CONTACT = 254
# Refuse new cases rather than let an unmonitored form fill the disk.
MAX_OPEN_CASES = 2000
MAX_DB_BYTES = 64 * 1024 * 1024


class IntakeError(Exception):
    """A case could not be stored or changed; the message is safe to show an operator."""


class ReporterCategory(StrEnum):
    CONSUMER = "consumer"
    CREDIT_UNION = "credit_union"
    VENDOR = "vendor"
    OTHER = "other"


class ReportKind(StrEnum):
    """What the report is about; the first four are 'credible' categories with a short SLA."""

    RATE = "rate"
    IDENTITY = "identity"
    PRIVACY = "privacy"
    SECURITY = "security"
    OTHER = "other"


URGENT_KINDS = frozenset(
    {ReportKind.RATE, ReportKind.IDENTITY, ReportKind.PRIVACY, ReportKind.SECURITY}
)


class Resolution(StrEnum):
    """Decisions that close a case (MASTER.md: confirm, amend, quarantine, withdraw, reject)."""

    CONFIRMED = "confirmed"
    AMENDED = "amended"
    QUARANTINED = "quarantined"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"


_EVENT_ACTIONS = ("triaged", "note", "redacted", "contact_deleted", *(r.value for r in Resolution))


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_RESOLUTIONS_SQL = _quoted(tuple(r.value for r in Resolution))


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    reporter_category TEXT NOT NULL CHECK (reporter_category IN
        ({_quoted(tuple(c.value for c in ReporterCategory))})),
    kind TEXT NOT NULL CHECK (kind IN ({_quoted(tuple(k.value for k in ReportKind))})),
    affected_url TEXT NOT NULL,
    affected_field TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases (case_id),
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ({_quoted(_EVENT_ACTIONS)})),
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS case_events_case ON case_events (case_id);
-- The ONLY mutable table: restricted, never copied into history, expires.
CREATE TABLE IF NOT EXISTS reporter_contacts (
    case_id TEXT PRIMARY KEY REFERENCES cases (case_id),
    contact TEXT,
    delete_after TEXT
);
CREATE TRIGGER IF NOT EXISTS cases_no_delete BEFORE DELETE ON cases
BEGIN SELECT RAISE(ABORT, 'cases is append-only'); END;
-- Redaction is the one permitted change: only the free-text columns, only to the marker.
CREATE TRIGGER IF NOT EXISTS cases_redact_only BEFORE UPDATE ON cases
WHEN NOT (
    NEW.case_id = OLD.case_id AND NEW.received_at = OLD.received_at
    AND NEW.reporter_category = OLD.reporter_category AND NEW.kind = OLD.kind
    AND NEW.affected_url IN (OLD.affected_url, '{REDACTED}')
    AND NEW.affected_field IN (OLD.affected_field, '{REDACTED}')
    AND NEW.reason IN (OLD.reason, '{REDACTED}')
    AND NEW.evidence_url IN (OLD.evidence_url, '{REDACTED}')
)
BEGIN SELECT RAISE(ABORT, 'cases is append-only (only redaction is allowed)'); END;
CREATE TRIGGER IF NOT EXISTS case_events_no_update BEFORE UPDATE ON case_events
BEGIN SELECT RAISE(ABORT, 'case_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS case_events_no_delete BEFORE DELETE ON case_events
BEGIN SELECT RAISE(ABORT, 'case_events is append-only'); END;
"""

_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}")


def business_days_after(start: datetime, days: int) -> datetime:
    """``start`` plus whole business days (Mon-Fri). Holidays are not modelled."""
    moment = start
    remaining = days
    while remaining > 0:
        moment += timedelta(days=1)
        if moment.weekday() < 5:
            remaining -= 1
    return moment


@dataclass(frozen=True)
class NewCase:
    """Validated input. Free text is bounded and stripped; the contact is optional."""

    reporter_category: ReporterCategory
    kind: ReportKind
    affected_url: str
    affected_field: str
    reason: str
    evidence_url: str = ""
    reply_contact: str = ""

    def __post_init__(self) -> None:
        for name, value, limit in (
            ("affected_url", self.affected_url, MAX_URL),
            ("affected_field", self.affected_field, MAX_FIELD),
            ("reason", self.reason, MAX_REASON),
            ("evidence_url", self.evidence_url, MAX_URL),
            ("reply_contact", self.reply_contact, MAX_CONTACT),
        ):
            if len(value) > limit or any(ord(char) < 32 and char not in "\n\t" for char in value):
                raise IntakeError(f"{name} is too long or contains control characters")
        if not self.reason.strip():
            raise IntakeError("a reason is required")
        if not self.affected_url.strip():
            raise IntakeError("the affected page or source address is required")
        for name, value in (
            ("affected_url", self.affected_url),
            ("evidence_url", self.evidence_url),
        ):
            if value and not _is_plain_http_url(value):
                raise IntakeError(f"{name} must be a web address starting with http:// or https://")
        if self.reply_contact and not _EMAIL.fullmatch(self.reply_contact):
            raise IntakeError("reply_contact must be an email address (or left empty)")


def _is_plain_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and " " not in value
    )


@dataclass(frozen=True)
class CaseView:
    case_id: str
    received_at: datetime
    reporter_category: ReporterCategory
    kind: ReportKind
    affected_url: str
    affected_field: str
    reason: str
    evidence_url: str
    triage_due: datetime
    resolution: Resolution | None
    resolved_at: datetime | None
    events: tuple[tuple[datetime, str, str, str], ...]
    reply_contact: str | None


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise IntakeError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class IntakeStore:
    """One intake database. ``initialize`` is an operator action; the web app never creates it."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise IntakeError("the intake database path must be absolute and normalized")
        self.path = path

    # -- lifecycle -----------------------------------------------------------------------
    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.executescript(_SCHEMA)
            raw.execute(
                "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (INTAKE_SCHEMA_VERSION,)
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # The file must already exist and be a regular, private file: a missing or odd store
        # means "intake unavailable", never "create one on demand from a public request".
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise IntakeError("the intake database is not available") from exc
        if not self.path.is_file() or self.path.is_symlink() or info.st_mode & 0o077:
            raise IntakeError("the intake database is not a private regular file")
        with closing(sqlite3.connect(self.path, timeout=5)) as raw:
            raw.execute("PRAGMA foreign_keys=ON")
            raw.row_factory = sqlite3.Row
            yield raw

    # -- intake (the only call the public web process makes) ------------------------------
    def record_case(self, case: NewCase, *, now: datetime) -> str:
        """Store a new case and return its reference. Raises IntakeError when it cannot."""
        with self._connect() as raw:
            if self.path.stat().st_size > MAX_DB_BYTES:
                raise IntakeError("the intake store is full")
            (open_cases,) = raw.execute(
                "SELECT COUNT(*) FROM cases c WHERE NOT EXISTS (SELECT 1 FROM case_events e "
                f"WHERE e.case_id = c.case_id AND e.action IN ({_RESOLUTIONS_SQL}))"
            ).fetchone()
            if open_cases >= MAX_OPEN_CASES:
                raise IntakeError("too many unresolved cases")
            case_id = secrets.token_hex(16)
            with raw:
                raw.execute(
                    "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        case_id,
                        _iso(now),
                        case.reporter_category.value,
                        case.kind.value,
                        case.affected_url.strip(),
                        case.affected_field.strip(),
                        case.reason.strip(),
                        case.evidence_url.strip(),
                    ),
                )
                if case.reply_contact:
                    raw.execute(
                        "INSERT INTO reporter_contacts VALUES (?, ?, NULL)",
                        (case_id, case.reply_contact),
                    )
        return case_id

    # -- operator actions ----------------------------------------------------------------
    def _add_event(
        self,
        raw: sqlite3.Connection,
        case_id: str,
        action: str,
        actor: str,
        note: str,
        at: datetime,
    ) -> None:
        if not actor.strip():
            raise IntakeError("an actor is required")
        raw.execute(
            "INSERT INTO case_events (case_id, occurred_at, actor, action, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (case_id, _iso(at), actor.strip(), action, note.strip()[:MAX_REASON]),
        )

    def _require_case(self, raw: sqlite3.Connection, case_id: str) -> None:
        if raw.execute("SELECT 1 FROM cases WHERE case_id = ?", (case_id,)).fetchone() is None:
            raise IntakeError(f"no such case: {case_id}")

    def add_note(
        self, case_id: str, *, actor: str, note: str, at: datetime, triage: bool = False
    ) -> None:
        with self._connect() as raw, raw:
            self._require_case(raw, case_id)
            self._add_event(raw, case_id, "triaged" if triage else "note", actor, note, at)

    def resolve(
        self, case_id: str, resolution: Resolution, *, actor: str, note: str, at: datetime
    ) -> None:
        """Record the decision and start the 90-day clock on any reply contact."""
        with self._connect() as raw, raw:
            self._require_case(raw, case_id)
            if self._resolution(raw, case_id) is not None:
                raise IntakeError("this case is already resolved")
            self._add_event(raw, case_id, resolution.value, actor, note, at)
            raw.execute(
                "UPDATE reporter_contacts SET delete_after = ? WHERE case_id = ?",
                (_iso(at + CONTACT_RETENTION), case_id),
            )

    def redact(self, case_id: str, *, actor: str, reason: str, at: datetime) -> None:
        """Blank free text and contact (unsolicited personal data); log that it happened."""
        with self._connect() as raw, raw:
            self._require_case(raw, case_id)
            raw.execute(
                "UPDATE cases SET affected_url = ?, affected_field = ?, reason = ?, "
                "evidence_url = ? WHERE case_id = ?",
                (REDACTED, REDACTED, REDACTED, REDACTED, case_id),
            )
            raw.execute("UPDATE reporter_contacts SET contact = NULL WHERE case_id = ?", (case_id,))
            self._add_event(raw, case_id, "redacted", actor, reason, at)

    def purge_expired_contacts(
        self, *, now: datetime, actor: str = "retention-job"
    ) -> tuple[str, ...]:
        """Delete reply contacts whose 90 days after resolution have passed."""
        with self._connect() as raw, raw:
            rows = raw.execute(
                "SELECT case_id FROM reporter_contacts "
                "WHERE contact IS NOT NULL AND delete_after IS NOT NULL AND delete_after <= ?",
                (_iso(now),),
            ).fetchall()
            purged = tuple(row["case_id"] for row in rows)
            for case_id in purged:
                raw.execute(
                    "UPDATE reporter_contacts SET contact = NULL WHERE case_id = ?", (case_id,)
                )
                self._add_event(
                    raw, case_id, "contact_deleted", actor, "retention period elapsed", now
                )
        return purged

    # -- reading (operators only) ---------------------------------------------------------
    @staticmethod
    def _resolution(raw: sqlite3.Connection, case_id: str) -> tuple[Resolution, datetime] | None:
        row = raw.execute(
            "SELECT action, occurred_at FROM case_events WHERE case_id = ? AND action IN "
            f"({_RESOLUTIONS_SQL}) ORDER BY event_id LIMIT 1",
            (case_id,),
        ).fetchone()
        return None if row is None else (Resolution(row["action"]), _parse(row["occurred_at"]))

    def _view(self, raw: sqlite3.Connection, row: sqlite3.Row, *, with_contact: bool) -> CaseView:
        case_id = row["case_id"]
        received = _parse(row["received_at"])
        kind = ReportKind(row["kind"])
        resolved = self._resolution(raw, case_id)
        events = tuple(
            (_parse(e["occurred_at"]), e["actor"], e["action"], e["note"])
            for e in raw.execute(
                "SELECT occurred_at, actor, action, note FROM case_events "
                "WHERE case_id = ? ORDER BY event_id",
                (case_id,),
            )
        )
        contact = None
        if with_contact:
            found = raw.execute(
                "SELECT contact FROM reporter_contacts WHERE case_id = ?", (case_id,)
            ).fetchone()
            contact = None if found is None else found["contact"]
        return CaseView(
            case_id=case_id,
            received_at=received,
            reporter_category=ReporterCategory(row["reporter_category"]),
            kind=kind,
            affected_url=row["affected_url"],
            affected_field=row["affected_field"],
            reason=row["reason"],
            evidence_url=row["evidence_url"],
            triage_due=business_days_after(received, 1 if kind in URGENT_KINDS else 5),
            resolution=None if resolved is None else resolved[0],
            resolved_at=None if resolved is None else resolved[1],
            events=events,
            reply_contact=contact,
        )

    def get(self, case_id: str) -> CaseView:
        with self._connect() as raw:
            row = raw.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if row is None:
                raise IntakeError(f"no such case: {case_id}")
            return self._view(raw, row, with_contact=True)

    def list_cases(self, *, only_open: bool = True) -> tuple[CaseView, ...]:
        with self._connect() as raw:
            rows = raw.execute("SELECT * FROM cases ORDER BY received_at, case_id").fetchall()
            views = tuple(self._view(raw, row, with_contact=False) for row in rows)
        return tuple(v for v in views if v.resolution is None) if only_open else views

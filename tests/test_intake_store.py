"""#100: the correction-intake store keeps history append-only and the contact restricted."""

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cu_rates.intake import (
    IntakeError,
    IntakeStore,
    NewCase,
    ReporterCategory,
    ReportKind,
    Resolution,
    business_days_after,
)
from cu_rates.intake import store as store_module

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # a Monday


@pytest.fixture
def intake(tmp_path: Path) -> IntakeStore:
    store = IntakeStore(tmp_path / "intake" / "intake.sqlite")
    store.initialize()
    return store


def _case(**changes: str) -> NewCase:
    base = {
        "reporter_category": ReporterCategory.CONSUMER,
        "kind": ReportKind.RATE,
        "affected_url": "https://cu-rates.org/compare",
        "affected_field": "APR",
        "reason": "The posted APR is different on the credit union's page.",
        "evidence_url": "https://example.test/rates",
        "reply_contact": "",
    }
    return NewCase(**(base | changes))  # type: ignore[arg-type]


def test_initialize_creates_a_private_file(intake: IntakeStore) -> None:
    assert intake.path.stat().st_mode & 0o777 == 0o600
    assert intake.path.parent.stat().st_mode & 0o077 == 0


def test_the_store_is_never_created_on_demand(tmp_path: Path) -> None:
    store = IntakeStore(tmp_path / "missing.sqlite")
    with pytest.raises(IntakeError, match="not available"):
        store.record_case(_case(), now=T0)
    assert not (tmp_path / "missing.sqlite").exists()


def test_a_group_or_world_accessible_store_is_refused(intake: IntakeStore) -> None:
    os.chmod(intake.path, 0o644)
    with pytest.raises(IntakeError, match="private regular file"):
        intake.record_case(_case(), now=T0)


def test_a_symlinked_store_is_refused(tmp_path: Path, intake: IntakeStore) -> None:
    link = tmp_path / "link.sqlite"
    link.symlink_to(intake.path)
    with pytest.raises(IntakeError, match="private regular file"):
        IntakeStore(link).record_case(_case(), now=T0)


def test_record_and_read_back_a_case(intake: IntakeStore) -> None:
    case_id = intake.record_case(_case(reply_contact="reporter@example.test"), now=T0)
    assert len(case_id) >= 20  # a random capability, not a sequence
    view = intake.get(case_id)
    assert view.received_at == T0 and view.kind is ReportKind.RATE
    assert view.reply_contact == "reporter@example.test"
    assert view.resolution is None and view.events == ()
    assert intake.list_cases()[0].case_id == case_id
    assert intake.list_cases()[0].reply_contact is None  # listings never expose contacts


def test_case_references_are_unique_and_unguessable(intake: IntakeStore) -> None:
    ids = {intake.record_case(_case(), now=T0) for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"reason": "   "}, "reason is required"),
        ({"reason": "x" * 2001}, "too long"),
        ({"affected_url": ""}, "required"),
        ({"affected_url": "javascript:alert(1)"}, "web address"),
        ({"affected_url": "https://user:pw@example.test/"}, "web address"),
        ({"evidence_url": "ftp://example.test/file"}, "web address"),
        ({"affected_field": "y" * 201}, "too long"),
        ({"reply_contact": "not-an-email"}, "email"),
        ({"reply_contact": "a@b"}, "email"),
        ({"reason": "bad\x00byte"}, "control characters"),
    ],
)
def test_invalid_input_is_refused(changes: dict[str, str], message: str) -> None:
    with pytest.raises(IntakeError, match=message):
        _case(**changes)


def test_history_is_append_only_in_the_database(intake: IntakeStore) -> None:
    case_id = intake.record_case(_case(), now=T0)
    intake.add_note(case_id, actor="reviewer", note="looking", at=T0 + timedelta(hours=1))
    with closing(sqlite3.connect(intake.path)) as raw:
        for statement in (
            "DELETE FROM cases",
            "UPDATE cases SET kind = 'security'",
            "UPDATE cases SET received_at = '2020-01-01T00:00:00+00:00'",
            "UPDATE cases SET reason = 'a different reason'",
            "UPDATE case_events SET note = 'edited'",
            "DELETE FROM case_events",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
                raw.execute(statement)


def test_contact_is_never_copied_into_history(intake: IntakeStore) -> None:
    secret = "private.reporter@example.test"
    case_id = intake.record_case(_case(reply_contact=secret), now=T0)
    intake.add_note(case_id, actor="reviewer", note="triaged", at=T0, triage=True)
    intake.resolve(case_id, Resolution.CONFIRMED, actor="reviewer", note="confirmed", at=T0)
    with closing(sqlite3.connect(intake.path)) as raw:
        for table in ("cases", "case_events", "meta"):
            dumped = " ".join(str(row) for row in raw.execute(f"SELECT * FROM {table}"))
            assert secret not in dumped
        assert secret in str(list(raw.execute("SELECT * FROM reporter_contacts")))


def test_resolution_starts_the_ninety_day_contact_clock_and_purge_honors_it(
    intake: IntakeStore,
) -> None:
    case_id = intake.record_case(_case(reply_contact="r@example.test"), now=T0)
    resolved_at = T0 + timedelta(days=2)
    intake.resolve(
        case_id, Resolution.REJECTED, actor="reviewer", note="not credible", at=resolved_at
    )
    assert intake.get(case_id).resolution is Resolution.REJECTED
    day_89 = resolved_at + timedelta(days=89)
    assert intake.purge_expired_contacts(now=day_89) == ()
    assert intake.get(case_id).reply_contact == "r@example.test"
    assert intake.purge_expired_contacts(now=resolved_at + timedelta(days=90)) == (case_id,)
    assert intake.get(case_id).reply_contact is None
    assert intake.get(case_id).events[-1][2] == "contact_deleted"
    assert intake.purge_expired_contacts(now=resolved_at + timedelta(days=200)) == ()  # idempotent


def test_an_unresolved_case_keeps_its_contact_indefinitely(intake: IntakeStore) -> None:
    case_id = intake.record_case(_case(reply_contact="r@example.test"), now=T0)
    assert intake.purge_expired_contacts(now=T0 + timedelta(days=3650)) == ()
    assert intake.get(case_id).reply_contact == "r@example.test"


def test_a_case_resolves_only_once(intake: IntakeStore) -> None:
    case_id = intake.record_case(_case(), now=T0)
    intake.resolve(case_id, Resolution.AMENDED, actor="a", note="n", at=T0)
    with pytest.raises(IntakeError, match="already resolved"):
        intake.resolve(case_id, Resolution.REJECTED, actor="a", note="n", at=T0)
    assert intake.list_cases() == ()
    assert len(intake.list_cases(only_open=False)) == 1


def test_an_actor_is_required_for_every_decision(intake: IntakeStore) -> None:
    case_id = intake.record_case(_case(), now=T0)
    with pytest.raises(IntakeError, match="actor"):
        intake.resolve(case_id, Resolution.CONFIRMED, actor=" ", note="n", at=T0)
    with pytest.raises(IntakeError, match="no such case"):
        intake.add_note("nope", actor="a", note="n", at=T0)


def test_redaction_removes_personal_data_but_keeps_the_audit_trail(intake: IntakeStore) -> None:
    case_id = intake.record_case(
        _case(
            reason="My SSN is 000-00-0000 and my account is 12345", reply_contact="r@example.test"
        ),
        now=T0,
    )
    intake.redact(case_id, actor="reviewer", reason="unsolicited personal data", at=T0)
    view = intake.get(case_id)
    assert view.reason == view.affected_url == view.evidence_url == "[redacted]"
    assert view.reply_contact is None
    assert view.received_at == T0 and view.kind is ReportKind.RATE  # structure is untouched
    assert view.events[-1][2] == "redacted"
    with closing(sqlite3.connect(intake.path)) as raw:
        assert "000-00-0000" not in " ".join(str(r) for r in raw.execute("SELECT * FROM cases"))
        # Redaction cannot be used to write arbitrary text.
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), raw:
            raw.execute("UPDATE cases SET reason = 'rewritten history'")


@pytest.mark.parametrize(
    ("kind", "due"),
    [
        (ReportKind.RATE, datetime(2026, 11, 17, 15, 0, tzinfo=UTC)),  # Mon -> Tue
        (ReportKind.IDENTITY, datetime(2026, 11, 17, 15, 0, tzinfo=UTC)),
        (ReportKind.PRIVACY, datetime(2026, 11, 17, 15, 0, tzinfo=UTC)),
        (ReportKind.SECURITY, datetime(2026, 11, 17, 15, 0, tzinfo=UTC)),
        (ReportKind.OTHER, datetime(2026, 11, 23, 15, 0, tzinfo=UTC)),  # five business days
    ],
)
def test_triage_targets_follow_the_policy(
    intake: IntakeStore, kind: ReportKind, due: datetime
) -> None:
    case_id = intake.record_case(_case(kind=kind), now=T0)
    assert intake.get(case_id).triage_due == due


def test_business_days_skip_weekends() -> None:
    friday = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    assert business_days_after(friday, 1) == datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    assert business_days_after(friday, 5) == datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def test_naive_timestamps_are_refused(intake: IntakeStore) -> None:
    with pytest.raises(IntakeError, match="timezone-aware"):
        intake.record_case(_case(), now=datetime(2026, 11, 16, 15, 0))


def test_the_store_refuses_new_cases_when_full(
    intake: IntakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_OPEN_CASES", 2)
    intake.record_case(_case(), now=T0)
    second = intake.record_case(_case(), now=T0)
    with pytest.raises(IntakeError, match="too many unresolved"):
        intake.record_case(_case(), now=T0)
    intake.resolve(second, Resolution.REJECTED, actor="a", note="n", at=T0)
    intake.record_case(_case(), now=T0)  # resolving frees capacity
    monkeypatch.setattr(store_module, "MAX_DB_BYTES", 1)
    with pytest.raises(IntakeError, match="full"):
        intake.record_case(_case(), now=T0)


def test_the_intake_package_can_reach_nothing_but_its_own_store_and_the_denylist() -> None:
    """The web process may import this package, so it must not reach any other storage.

    tests/site_audit.py exempts cu_rates.intake from the "web never opens a database" rule
    only because of this guard.
    """
    import ast

    forbidden = ("sqlalchemy", "cu_rates.db", "cu_rates.artifact_store", "cu_rates.pipeline")
    root = Path(__file__).resolve().parents[1] / "src" / "cu_rates" / "intake"
    seen = 0
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                seen += 1
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
    assert seen > 10

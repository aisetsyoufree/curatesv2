"""#100: the operator tool is the only way to read, decide, withdraw and redact cases."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cu_rates.intake import IntakeStore, NewCase, ReporterCategory, ReportKind, Resolution
from cu_rates.intake.cli import main
from cu_rates.snapshot import Denylist

T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)  # Monday


def _case(kind: ReportKind = ReportKind.RATE, contact: str = "reporter@example.test") -> NewCase:
    return NewCase(
        reporter_category=ReporterCategory.CONSUMER,
        kind=kind,
        affected_url="https://cu-rates.org/compare",
        affected_field="APR",
        reason="The APR shown is not what the credit union publishes.",
        reply_contact=contact,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "intake" / "intake.sqlite"
    assert main(["--db", str(path), "init"], now=T0) == 0
    return path


def run(db: Path, *args: str, now: datetime = T0) -> int:
    return main(["--db", str(db), *args], now=now)


def test_relative_paths_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", "intake.sqlite", "list"], now=T0) == 1
    assert "absolute" in capsys.readouterr().err


def test_list_flags_overdue_cases_with_a_distinct_exit_code(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    assert run(db, "list", now=T0 + timedelta(hours=1)) == 0  # inside the one-business-day target
    out = capsys.readouterr()
    assert case_id in out.out and "OVERDUE" not in out.out
    assert run(db, "list", now=T0 + timedelta(days=2)) == 3  # past it: a timer can alert on this
    assert "OVERDUE" in capsys.readouterr().out
    run(db, "resolve", case_id, "--decision", "rejected", "--actor", "rev", "--note", "n")
    assert run(db, "list", now=T0 + timedelta(days=9)) == 0  # resolved cases are never overdue


def test_show_never_prints_the_contact_unless_asked(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_id = IntakeStore(db).record_case(_case(), now=T0)
    assert run(db, "show", case_id) == 0
    assert "reporter@example.test" not in capsys.readouterr().out
    assert run(db, "show", case_id, "--with-contact") == 0
    assert "reporter@example.test" in capsys.readouterr().out


def test_triage_note_and_resolution_are_recorded_in_order(db: Path) -> None:
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    assert run(db, "triage", case_id, "--actor", "rev", "--note", "looked") == 0
    assert run(db, "note", case_id, "--actor", "rev", "--note", "asked CU") == 0
    assert (
        run(db, "resolve", case_id, "--decision", "amended", "--actor", "rev", "--note", "fixed")
        == 0
    )
    view = store.get(case_id)
    assert [e[2] for e in view.events] == ["triaged", "note", "amended"]
    assert view.resolution is Resolution.AMENDED
    assert (
        run(db, "resolve", case_id, "--decision", "rejected", "--actor", "rev", "--note", "n") == 1
    )


def test_withdraw_suppresses_on_the_independent_denylist_and_resolves(
    db: Path, tmp_path: Path
) -> None:
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    denylist_path = tmp_path / "independent" / "withdrawals.json"
    denylist = Denylist(denylist_path)
    denylist.initialize()
    code = run(
        db, "withdraw", case_id, "--denylist", str(denylist_path), "--scope", "offer",
        "--target", "offer-123", "--actor", "rev", "--note", "wrong APR",
        now=T0 + timedelta(hours=1),
    )  # fmt: skip
    assert code == 0
    assert ("offer", "offer-123") in denylist.active_targets(T0 + timedelta(hours=1))
    (entry,) = denylist.entries()
    assert case_id in entry.reason
    view = store.get(case_id)
    assert view.resolution is Resolution.WITHDRAWN
    assert view.events[-1][3].startswith("offer offer-123")


def test_withdraw_for_an_unknown_case_suppresses_nothing(db: Path, tmp_path: Path) -> None:
    denylist_path = tmp_path / "independent" / "withdrawals.json"
    denylist = Denylist(denylist_path)
    denylist.initialize()
    code = run(
        db, "withdraw", "no-such-case", "--denylist", str(denylist_path), "--scope", "offer",
        "--target", "offer-123", "--actor", "rev", "--note", "n",
    )  # fmt: skip
    assert code == 1
    assert denylist.entries() == ()


def test_withdraw_writes_the_denylist_before_the_decision(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the two steps must leave the public protected and the case open."""
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    denylist_path = tmp_path / "independent" / "withdrawals.json"
    denylist = Denylist(denylist_path)
    denylist.initialize()

    def crash(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(IntakeStore, "resolve", crash)
    code = run(
        db, "withdraw", case_id, "--denylist", str(denylist_path), "--scope", "source",
        "--target", "source-1", "--actor", "rev", "--note", "n",
    )  # fmt: skip
    assert code == 1
    assert ("source", "source-1") in denylist.active_targets(T0 + timedelta(minutes=1))
    assert store.get(case_id).resolution is None  # still open, so the operator retries


def test_redact_and_purge_contacts(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = IntakeStore(db)
    redacted = store.record_case(_case(), now=T0)
    resolved = store.record_case(_case(), now=T0)
    assert run(db, "redact", redacted, "--actor", "rev", "--reason", "contained an SSN") == 0
    assert store.get(redacted).reason == "[redacted]" and store.get(redacted).reply_contact is None
    run(db, "resolve", resolved, "--decision", "confirmed", "--actor", "rev", "--note", "n")
    assert run(db, "purge-contacts", now=T0 + timedelta(days=89)) == 0
    assert "deleted 0" in capsys.readouterr().out
    assert store.get(resolved).reply_contact == "reporter@example.test"
    assert run(db, "purge-contacts", now=T0 + timedelta(days=91)) == 0
    assert "deleted 1" in capsys.readouterr().out
    assert store.get(resolved).reply_contact is None


def test_a_missing_database_is_an_error_not_a_silent_create(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "intake.sqlite"
    assert run(missing, "list") == 1
    assert not missing.exists()


def _denylist(tmp_path: Path) -> tuple[Path, Denylist]:
    path = tmp_path / "independent" / "withdrawals.json"
    denylist = Denylist(path)
    denylist.initialize()
    return path, denylist


def test_a_failed_withdraw_can_be_retried_without_a_second_entry(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review MEDIUM: a retry later in time must reuse the entry already written."""
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    path, denylist = _denylist(tmp_path)
    args = (
        "withdraw", case_id, "--denylist", str(path), "--scope", "source",
        "--target", "source-1", "--actor", "rev", "--note", "n",
    )  # fmt: skip
    real = IntakeStore.resolve
    monkeypatch.setattr(IntakeStore, "resolve", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert run(db, *args, now=T0 + timedelta(hours=1)) == 1
    monkeypatch.setattr(IntakeStore, "resolve", real)
    assert run(db, *args, now=T0 + timedelta(hours=2)) == 0  # a later retry succeeds
    assert len(denylist.entries()) == 1
    assert store.get(case_id).resolution is Resolution.WITHDRAWN


def test_one_case_can_withdraw_more_than_one_target(db: Path, tmp_path: Path) -> None:
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    path, denylist = _denylist(tmp_path)
    for target in ("offer-1", "offer-2"):
        code = run(
            db, "withdraw", case_id, "--denylist", str(path), "--scope", "offer",
            "--target", target, "--actor", "rev", "--note", "n",
        )  # fmt: skip
        assert code == 0
    active = denylist.active_targets(T0 + timedelta(minutes=1))
    assert {("offer", "offer-1"), ("offer", "offer-2")} <= active
    assert store.get(case_id).resolution is Resolution.WITHDRAWN


def test_a_triaged_open_case_does_not_keep_the_overdue_alarm_ringing(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review MEDIUM: triage meets the target; only an untriaged case is overdue."""
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    assert run(db, "list", now=T0 + timedelta(days=3)) == 3
    run(db, "triage", case_id, "--actor", "rev", "--note", "looked", now=T0 + timedelta(days=3))
    assert run(db, "list", now=T0 + timedelta(days=9)) == 0
    assert "triaged" in capsys.readouterr().out


def test_bulk_reject_is_a_dry_run_until_confirmed(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = IntakeStore(db)
    ids = [store.record_case(_case(), now=T0 + timedelta(hours=h)) for h in range(3)]
    window = ("--from", "2026-11-16T14:00:00+00:00", "--to", "2026-11-16T23:00:00+00:00")
    base = ("bulk-reject", *window, "--actor", "rev", "--note", "junk flood")
    assert run(db, *base) == 0
    assert "would be rejected" in capsys.readouterr().out
    assert all(store.get(c).resolution is None for c in ids)
    assert run(db, *base, "--yes") == 0
    assert "rejected 3 case(s)" in capsys.readouterr().out
    assert all(store.get(c).resolution is Resolution.REJECTED for c in ids)
    naive = ("bulk-reject", "--from", "2026-11-16T14:00", "--to", "2026-11-16T23:00")
    assert run(db, *naive, "--actor", "r", "--note", "n", "--yes") == 1


def test_an_unusable_denylist_is_an_error_message_not_a_traceback(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case_id = IntakeStore(db).record_case(_case(), now=T0)
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    code = run(
        db, "withdraw", case_id, "--denylist", str(bad), "--scope", "offer",
        "--target", "o", "--actor", "rev", "--note", "n",
    )  # fmt: skip
    assert code == 1
    assert "error:" in capsys.readouterr().err
    assert IntakeStore(db).get(case_id).resolution is None


def test_a_lifted_suppression_can_be_renewed_with_a_new_entry(db: Path, tmp_path: Path) -> None:
    """Review LOW: an emergency re-suppression must work after the earlier one was lifted."""
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    path, denylist = _denylist(tmp_path)
    args = (
        "withdraw", case_id, "--denylist", str(path), "--scope", "offer",
        "--target", "offer-9", "--actor", "rev", "--note", "n",
    )  # fmt: skip
    assert run(db, *args) == 0
    (first,) = denylist.entries()
    denylist.lift(first.withdrawal_id, at=T0 + timedelta(hours=1), actor="owner")
    assert ("offer", "offer-9") not in denylist.active_targets(T0 + timedelta(hours=2))
    assert run(db, *args, now=T0 + timedelta(hours=3)) == 0
    assert ("offer", "offer-9") in denylist.active_targets(T0 + timedelta(hours=4))
    ids = [e.withdrawal_id for e in denylist.entries()]
    assert len(ids) == 2 and len(set(ids)) == 2 and ids[1].endswith(".r1")


def test_withdrawing_on_an_already_decided_case_is_recorded_in_its_history(
    db: Path, tmp_path: Path
) -> None:
    store = IntakeStore(db)
    case_id = store.record_case(_case(), now=T0)
    store.resolve(case_id, Resolution.REJECTED, actor="rev", note="first look: not credible", at=T0)
    path, denylist = _denylist(tmp_path)
    code = run(
        db, "withdraw", case_id, "--denylist", str(path), "--scope", "offer",
        "--target", "offer-1", "--actor", "rev", "--note", "new evidence",
    )  # fmt: skip
    assert code == 0
    assert ("offer", "offer-1") in denylist.active_targets(T0 + timedelta(minutes=1))
    view = store.get(case_id)
    assert view.resolution is Resolution.REJECTED  # the original decision stands
    assert view.events[-1][2] == "note" and "suppressed offer offer-1" in view.events[-1][3]

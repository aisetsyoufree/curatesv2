"""Operator tool for the correction inbox: ``python -m cu_rates.intake``.

Run as the service user (or root) on the host. It is the ONLY way cases are read, triaged,
resolved or redacted. The public web process can only add cases.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.snapshot import Denylist

from .store import CaseView, IntakeError, IntakeStore, Resolution


def _line(view: CaseView, now: datetime) -> str:
    state = view.resolution.value if view.resolution else "OPEN"
    flag = ""
    if view.resolution is None and view.triage_due < now:
        flag = "  OVERDUE"
    return (
        f"{view.case_id}  {view.received_at:%Y-%m-%d %H:%M}Z  {view.kind.value:<8} "
        f"{state:<12} due {view.triage_due:%Y-%m-%d}{flag}"
    )


def _show(view: CaseView) -> str:
    lines = [
        f"case:      {view.case_id}",
        f"received:  {view.received_at.isoformat()}",
        f"reporter:  {view.reporter_category.value}",
        f"kind:      {view.kind.value}",
        f"page:      {view.affected_url}",
        f"field:     {view.affected_field}",
        f"evidence:  {view.evidence_url or '(none)'}",
        f"triage by: {view.triage_due.isoformat()}",
        "reason:",
        *(f"  {text}" for text in (view.reason.splitlines() or [""])),
        "history:",
        *(
            f"  {at.isoformat()}  {actor}  {action}  {note}"
            for at, actor, action, note in view.events
        ),
    ]
    return "\n".join(lines)


def _absolute(value: Path, name: str) -> Path:
    if not value.is_absolute():
        raise IntakeError(f"{name} must be an absolute path")
    return value


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu_rates.intake", description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="intake database (absolute path)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the intake database (private, mode 0600)")
    listing = sub.add_parser("list", help="list cases (open by default)")
    listing.add_argument("--all", action="store_true", help="include resolved cases")
    show = sub.add_parser("show", help="show one case")
    show.add_argument("case_id")
    show.add_argument("--with-contact", action="store_true", help="also print the reply contact")
    for name, help_text in (("note", "add a note"), ("triage", "record that a case was triaged")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("case_id")
        item.add_argument("--actor", required=True)
        item.add_argument("--note", required=True)
    resolve = sub.add_parser("resolve", help="close a case with a decision")
    resolve.add_argument("case_id")
    resolve.add_argument("--decision", required=True, choices=[r.value for r in Resolution])
    resolve.add_argument("--actor", required=True)
    resolve.add_argument("--note", required=True)
    withdraw = sub.add_parser(
        "withdraw", help="immediately suppress something on the independent denylist"
    )
    withdraw.add_argument("case_id")
    withdraw.add_argument("--denylist", type=Path, required=True)
    withdraw.add_argument("--scope", required=True, choices=[s.value for s in WithdrawalScope])
    withdraw.add_argument("--target", required=True)
    withdraw.add_argument("--actor", required=True)
    withdraw.add_argument("--note", required=True)
    redact = sub.add_parser("redact", help="remove unsolicited personal data from a case")
    redact.add_argument("case_id")
    redact.add_argument("--actor", required=True)
    redact.add_argument("--reason", required=True)
    sub.add_parser("purge-contacts", help="delete reply contacts 90 days after resolution")

    args = parser.parse_args(argv)
    moment = now or datetime.now(UTC)
    try:
        store = IntakeStore(_absolute(args.db, "--db"))
        if args.command == "init":
            store.initialize()
            print(f"initialized {store.path}")
        elif args.command == "list":
            cases = store.list_cases(only_open=not args.all)
            for view in cases:
                print(_line(view, moment))
            overdue = sum(1 for v in cases if v.resolution is None and v.triage_due < moment)
            print(f"{len(cases)} case(s), {overdue} overdue", file=sys.stderr)
            return 3 if overdue else 0
        elif args.command == "show":
            view = store.get(args.case_id)
            print(_show(view))
            if args.with_contact:
                print(f"contact:   {view.reply_contact or '(none or deleted)'}")
        elif args.command in {"note", "triage"}:
            store.add_note(
                args.case_id,
                actor=args.actor,
                note=args.note,
                at=moment,
                triage=args.command == "triage",
            )
            print("recorded")
        elif args.command == "resolve":
            store.resolve(
                args.case_id,
                Resolution(args.decision),
                actor=args.actor,
                note=args.note,
                at=moment,
            )
            print(f"resolved: {args.decision}")
        elif args.command == "withdraw":
            store.get(args.case_id)  # the case must exist before anything is suppressed
            # Independent suppression first, then the decision: a crash between the two
            # leaves the public protected and the case still open for retry.
            Denylist(_absolute(args.denylist, "--denylist")).withdraw(
                WithdrawalEntry(
                    schema_version=CONTRACT_VERSION,
                    withdrawal_id=f"case-{args.case_id}.withdraw",
                    scope=WithdrawalScope(args.scope),
                    target_id=args.target,
                    actor=args.actor,
                    withdrawn_at=moment,
                    reason=f"correction case {args.case_id}: {args.note}"[:500],
                )
            )
            store.resolve(
                args.case_id,
                Resolution.WITHDRAWN,
                actor=args.actor,
                note=f"{args.scope} {args.target}: {args.note}",
                at=moment,
            )
            print(f"withdrawn: {args.scope} {args.target}")
        elif args.command == "redact":
            store.redact(args.case_id, actor=args.actor, reason=args.reason, at=moment)
            print("redacted")
        elif args.command == "purge-contacts":
            purged = store.purge_expired_contacts(now=moment)
            print(f"deleted {len(purged)} expired reply contact(s)")
    except (IntakeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

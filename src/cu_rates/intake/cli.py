"""Operator tool for the correction inbox: ``python -m cu_rates.intake``.

Run as the service user (or root) on the host. It is the ONLY way cases are read, triaged,
resolved or redacted. The public web process can only add cases.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.snapshot import Denylist, SnapshotError

from .store import MAX_OPEN_CASES, CaseView, IntakeError, IntakeStore, Resolution


def _line(view: CaseView, now: datetime) -> str:
    state = view.resolution.value if view.resolution else "OPEN"
    flag = ""
    if view.resolution is None and view.triaged_at is None and view.triage_due < now:
        flag = "  OVERDUE"  # a triaged case has met its target even while it is still open
    elif view.triaged_at is not None and view.resolution is None:
        flag = "  triaged"
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
    listing = sub.add_parser(
        "list",
        help="list cases (open by default); exit 3 means a case missed its triage target",
    )
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
    bulk = sub.add_parser(
        "bulk-reject", help="reject every OPEN case received in a window (junk flooding)"
    )
    bulk.add_argument("--from", dest="received_from", type=datetime.fromisoformat, required=True)
    bulk.add_argument("--to", dest="received_to", type=datetime.fromisoformat, required=True)
    bulk.add_argument("--actor", required=True)
    bulk.add_argument("--note", required=True)
    bulk.add_argument("--yes", action="store_true", help="actually do it (otherwise a dry run)")
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
            overdue = sum(
                1
                for v in cases
                if v.resolution is None and v.triaged_at is None and v.triage_due < moment
            )
            print(
                f"{len(cases)} case(s), {overdue} overdue; intake accepts new reports until "
                f"{MAX_OPEN_CASES} are open",
                file=sys.stderr,
            )
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
            denylist = Denylist(_absolute(args.denylist, "--denylist"))
            # One id per case, scope and target: a second target needs no id clash, and a
            # retry after a failed decision reuses the entry already written (same content),
            # not a new timestamp that the append-only ledger would reject.
            digest = hashlib.sha256(args.target.encode()).hexdigest()[:12]
            base_id = f"case-{args.case_id}.{args.scope}.{digest}"
            # Earlier entries for this same case, scope and target (a lift leaves its entry in
            # the ledger forever, so a renewed suppression needs a new id: base, base.r1, ...).
            history = [
                e
                for e in denylist.entries()
                if e.withdrawal_id == base_id or e.withdrawal_id.startswith(f"{base_id}.r")
            ]
            latest = max(history, key=lambda e: e.withdrawn_at, default=None)
            if latest is not None and latest.lifted_at is None:
                entry = latest  # a retry after a failed decision: reuse it unchanged
            else:
                entry = WithdrawalEntry(
                    schema_version=CONTRACT_VERSION,
                    withdrawal_id=base_id if not history else f"{base_id}.r{len(history)}",
                    scope=WithdrawalScope(args.scope),
                    target_id=args.target,
                    actor=args.actor,
                    withdrawn_at=moment,
                    reason=f"correction case {args.case_id}: {args.note}"[:500],
                )
            # Independent suppression first, then the decision: a crash between the two
            # leaves the public protected and the case still open for retry.
            denylist.withdraw(entry)
            detail = f"{args.scope} {args.target}: {args.note}"
            if store.get(args.case_id).resolution is None:
                store.resolve(
                    args.case_id, Resolution.WITHDRAWN, actor=args.actor, note=detail, at=moment
                )
            else:  # already decided: the suppression still belongs in the case history
                store.add_note(
                    args.case_id, actor=args.actor, note=f"suppressed {detail}", at=moment
                )
            print(f"withdrawn: {args.scope} {args.target}")
        elif args.command == "bulk-reject":
            window = {"received_from": args.received_from, "received_to": args.received_to}
            if any(value.tzinfo is None for value in window.values()):
                raise IntakeError("--from and --to must include a timezone, e.g. 2026-11-18T00:00Z")
            if not args.yes:
                matching = [
                    c.case_id
                    for c in store.list_cases(only_open=True)
                    if args.received_from <= c.received_at <= args.received_to
                ]
                print(f"dry run: {len(matching)} open case(s) would be rejected (add --yes)")
            else:
                done = store.bulk_reject(actor=args.actor, note=args.note, at=moment, **window)
                print(f"rejected {len(done)} case(s)")
        elif args.command == "redact":
            store.redact(args.case_id, actor=args.actor, reason=args.reason, at=moment)
            print("redacted")
        elif args.command == "purge-contacts":
            purged = store.purge_expired_contacts(now=moment)
            print(f"deleted {len(purged)} expired reply contact(s)")
    except (IntakeError, ValueError, OSError, sqlite3.Error, SnapshotError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

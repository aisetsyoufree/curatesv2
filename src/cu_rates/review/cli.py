"""Local operator console: python -m cu_rates.review --db /absolute/path <command>."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, Response, render_template_string
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from cu_rates.db import open_operational_database
from cu_rates.intake import IntakeError, IntakeStore
from cu_rates.review.store import (
    KINDS,
    Item,
    decide,
    digest,
    operational_items,
    product_item,
    quarantined_item,
    source_item,
)


def _database(path: Path) -> Path:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("database must be an existing absolute file; migrate it before use")
    return path


def read_engine(path: Path) -> Engine:
    _database(path)

    def connect() -> sqlite3.Connection:
        raw = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, autocommit=False)
        raw.execute("PRAGMA query_only=ON")
        return raw

    return create_engine("sqlite://", creator=connect, poolclass=NullPool)


def correction_items(path: Path | None) -> list[Item]:
    if path is None:
        return []
    # list_cases uses mode=ro and never reads reporter_contacts. Do not use get().
    result = []
    for case in IntakeStore(path).list_cases():
        detail = dict(
            case_id=case.case_id,
            kind=case.kind.value,
            field=_correction_field(case.affected_field),
            commands=f"python -m cu_rates.intake --db {path} show {case.case_id}",
        )
        result.append(
            Item(
                "correction",
                case.case_id,
                case.received_at,
                f"{case.kind.value}: {detail['field']}; " + detail["commands"],
                digest(detail),
                detail,
            )
        )
    return result


def queue(database: Path, intake_db: Path | None) -> list[Item]:
    engine = read_engine(database)
    try:
        with engine.connect() as conn:
            return operational_items(conn) + correction_items(intake_db)
    finally:
        engine.dispose()


def age(item: Item, now: datetime) -> str:
    if item.since is None:
        return "unknown (no stored creation/observation time)"
    return f"{max(0, int((now - item.since).total_seconds() // 3600))}h"


def _line(text: str) -> str:
    # Untrusted source excerpts/labels must not add terminal control sequences or rows.
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _correction_field(text: str) -> str:
    # The full reporter submission belongs in the restricted intake tool. Even
    # field labels may contain pasted links; redact before truncation.
    return re.sub(
        r"(?i)(?:\b[a-z][a-z0-9+.-]*://|//|\bwww\.|\b[\w-]+\.[a-z]{2,}\b)\S*",
        "[URL omitted]",
        _line(text),
    )[:80]


_PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Private operator review queue</title>
<h1>Awaiting human decisions</h1>
<p>Read-only. Inspect evidence and decide using python -m cu_rates.review on the host.</p>
{% if not intake %}<p>Correction inbox not configured (--intake-db).</p>{% endif %}
<table><caption>Pending reviews and quarantined offers</caption>
<thead><tr><th>Kind</th><th>ID</th><th>Age</th><th>Summary</th><th>SHA-256</th></tr></thead>
<tbody>{% for item in items %}<tr><td>{{ item.kind }}</td><td>{{ item.id }}</td>
<td>{{ age(item, now) }}</td><td>{{ item.summary }}</td><td>{{ item.sha256 }}</td></tr>{% endfor %}
</tbody></table></html>"""


def create_app(database: Path, intake_db: Path | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Loopback Host allowlist also prevents DNS-rebinding access to the private queue.
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index() -> str | tuple[str, int]:
        try:
            return render_template_string(
                _PAGE,
                items=queue(database, intake_db),
                intake=intake_db,
                now=datetime.now(UTC),
                age=age,
            )
        except (ValueError, OSError, sqlite3.Error, SQLAlchemyError, IntakeError):
            return "Queue unavailable; inspect configuration on the host.", 503

    return app


def serve(database: Path, intake_db: Path | None, *, host: str, port: int) -> None:
    if host != "127.0.0.1":
        raise ValueError("review console must bind to 127.0.0.1 only")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    # No debug reloader, interactive debugger, proxy headers, public app or actions.
    create_app(database, intake_db).run(host=host, port=port, debug=False, use_reloader=False)


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="existing operational DB (absolute)")
    parser.add_argument("--intake-db", type=Path, help="optional private intake DB (absolute)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    showing = sub.add_parser("show")
    showing.add_argument("kind", choices=KINDS)
    showing.add_argument("id")
    for command in ("approve", "reject"):
        deciding = sub.add_parser(command)
        deciding.add_argument("kind", choices=KINDS)
        deciding.add_argument("id")
        deciding.add_argument("--sha256", required=True, help="content hash copied from show")
        deciding.add_argument("--actor", required=True)
        deciding.add_argument("--note", required=True)
    serving = sub.add_parser("serve")
    serving.add_argument("--port", default=8765, type=int)
    serving.add_argument("--host", default="127.0.0.1", help="only 127.0.0.1 is accepted")
    args = parser.parse_args(argv)
    moment = now or datetime.now(UTC)
    try:
        _database(args.db)
        if args.intake_db is not None:
            _database(args.intake_db)
        if args.command == "serve":
            serve(args.db, args.intake_db, host=args.host, port=args.port)
        elif args.command == "list":
            items = queue(args.db, args.intake_db)
            for kind in KINDS:
                print(f"{kind}:")
                for item in items:
                    if item.kind == kind:
                        print(
                            f"  {item.id}  age={age(item, moment)}  {_line(item.summary)}  "
                            f"sha256={item.sha256}"
                        )
            if args.intake_db is None:
                print("Correction inbox not configured (--intake-db).")
        elif args.command == "show":
            if args.kind == "correction":
                correction = next(
                    (i for i in correction_items(args.intake_db) if i.id == args.id), None
                )
                if correction is None:
                    raise ValueError("no such open correction case (configure --intake-db)")
                item = correction
            else:
                engine = read_engine(args.db)
                try:
                    with engine.connect() as conn:
                        item = {
                            "product": product_item,
                            "source": source_item,
                            "quarantined": quarantined_item,
                        }[args.kind](conn, args.id)
                finally:
                    engine.dispose()
            # JSON escapes terminal controls in all stored excerpts and labels.
            print(
                json.dumps(
                    dict(
                        kind=item.kind,
                        id=item.id,
                        sha256=item.sha256,
                        decided=item.decided,
                        detail=item.detail,
                    ),
                    indent=2,
                    default=str,
                )
            )
        else:
            engine = open_operational_database(args.db)
            try:
                with engine.connect() as conn:
                    # Python 3.12 autocommit=False already opened a deferred transaction.
                    # Replace it before ANY reads; serialize CAS + append against all writers.
                    conn.exec_driver_sql("ROLLBACK")
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                    review_id = decide(
                        conn,
                        args.kind,
                        args.id,
                        sha256=args.sha256,
                        actor=args.actor,
                        note=args.note,
                        approve=args.command == "approve",
                        at=moment,
                    )
                    conn.commit()
                print(f"{args.command} recorded: {review_id}")
            finally:
                engine.dispose()
    except (ValueError, OSError, sqlite3.Error, SQLAlchemyError, IntakeError) as exc:
        print(f"error: {_line(str(exc))}", file=sys.stderr)
        return 1
    return 0

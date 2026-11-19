"""Private operator queue: real persistence, gate integration and refusal paths."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from sqlalchemy import select

from cu_rates.contracts import ReviewSubject
from cu_rates.contracts.publication import ReasonCode, evaluate_publication
from cu_rates.contracts.sources import SourceApproval
from cu_rates.db import open_operational_database
from cu_rates.db import schema as s
from cu_rates.db.records import load_review_decision
from cu_rates.intake import IntakeStore, Resolution
from cu_rates.pipeline.slice import CHARTER, SliceRun, run_fixture
from cu_rates.pipeline.trusted import _approval, load_publication_context
from cu_rates.review.cli import create_app, main, queue, read_engine, serve
from cu_rates.review.store import Item, enqueue_source_review, product_item, source_item
from tests.test_intake_cli import _case

NOW = datetime(2026, 11, 18, 20, tzinfo=UTC)
ACTOR = "owner-human"
NOTE = "Inspected the exact stored evidence and policy."


@pytest.fixture
def run(tmp_path: Path) -> SliceRun:
    return run_fixture(tmp_path / "slice")


def call(run: SliceRun, *args: str) -> int:
    return main(["--db", str(run.database), *args], now=NOW)


def pending_product(run: SliceRun, *, name: str = "Owner review product") -> Item:
    product_id = run.offers["purchase-30-year"].product_id
    engine = open_operational_database(run.database)
    try:
        with engine.begin() as conn:
            conn.execute(
                s.products.update().where(s.products.c.product_id == product_id).values(name=name)
            )
            return product_item(conn, product_id)
    finally:
        engine.dispose()


def pending_source(run: SliceRun, reason: str = "Uncertain automation suitability") -> Item:
    engine = open_operational_database(run.database)
    try:
        with engine.begin() as conn:
            source_id = run.offers["purchase-30-year"].source_id
            template = _approval(
                conn.execute(
                    select(s.source_approvals).where(s.source_approvals.c.source_id == source_id)
                ).first()
            )
            evidence = tuple(run.offers["purchase-30-year"].iter_provenance())[:1]
            template = SourceApproval.model_validate(
                template.model_copy(update={"evidence": evidence}).model_dump()
            )
            profile_id = conn.execute(
                select(s.fetch_profiles.c.profile_id).where(
                    s.fetch_profiles.c.source_id == source_id
                )
            ).scalar_one()
            args: dict[str, Any] = dict(
                charter=CHARTER, profile_id=profile_id, reason=reason, at=NOW - timedelta(hours=1)
            )
            request_id = enqueue_source_review(conn, template, **args)
            assert enqueue_source_review(conn, template, **args) == request_id
            return source_item(conn, request_id)
    finally:
        engine.dispose()


def decision(run: SliceRun, item: Item, command: str = "approve", **options: str) -> int:
    flags = dict(sha256=item.sha256, actor=ACTOR, note=NOTE) | options
    return call(
        run,
        command,
        item.kind,
        item.id,
        *(part for key, value in flags.items() for part in (f"--{key}", value)),
    )


def test_product_list_show_and_approval_satisfies_existing_gate(
    run: SliceRun, capsys: pytest.CaptureFixture[str]
) -> None:
    item = pending_product(run)
    assert call(run, "list") == 0
    listing = capsys.readouterr().out
    assert "product:" in listing and item.id in listing and "age=" in listing
    assert call(run, "show", "product", item.id) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["sha256"] == item.sha256
    assert shown["detail"]["evidence"]
    assert shown["detail"]["evidence"][0]["source_text"]
    assert "Only this product definition" in shown["detail"]["approves"]
    engine = open_operational_database(run.database)
    try:
        with engine.connect() as conn:
            before = load_publication_context(
                conn, run.offers["purchase-30-year"].offer_id, NOW, store=run.store
            )
            assert (
                ReasonCode.PRODUCT_NOT_REVIEWED
                in evaluate_publication(before.offer, before.context).reasons
            )
        assert decision(run, item) == 0
        with engine.connect() as conn:
            after = load_publication_context(conn, before.offer.offer_id, NOW, store=run.store)
            assert (
                ReasonCode.PRODUCT_NOT_REVIEWED
                not in evaluate_publication(after.offer, after.context).reasons
            )
            review = after.context.product_review
            assert review is not None and review.actor == ACTOR and review.rationale == NOTE
            assert review.subject == ReviewSubject.PRODUCT
            assert review.reviewed_product_sha256 == item.detail["reviewed_product_sha256"]
            assert load_review_decision(conn, review.review_id).evidence == item.evidence
        assert item.id not in {i.id for i in queue(run.database, None)}
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind", ["product", "source"])
@pytest.mark.parametrize("command", ["approve", "reject"])
def test_decision_persists_actor_note_and_refuses_second_decision(
    run: SliceRun, kind: str, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    item = pending_product(run) if kind == "product" else pending_source(run)
    assert decision(run, item, command) == 0
    assert decision(run, item) == 1
    assert "already decided" in capsys.readouterr().err
    assert item.id not in {i.id for i in queue(run.database, None)}
    engine = read_engine(run.database)
    try:
        with engine.connect() as conn:
            if kind == "source":
                row = conn.execute(
                    select(s.source_approvals).where(s.source_approvals.c.approval_id == item.id)
                ).one()
                assert row.actor == ACTOR and row.rationale == NOTE
                assert row.decision == ("approved" if command == "approve" else "rejected")
                assert conn.execute(
                    select(s.source_approval_evidence).where(
                        s.source_approval_evidence.c.approval_id == item.id
                    )
                ).first()
            else:
                review = product_item(conn, item.id).detail["latest_decision"][0]
                assert review["actor"] == ACTOR and review["rationale"] == NOTE
                assert review["verdict"] == ("approved" if command == "approve" else "rejected")
    finally:
        engine.dispose()


def test_source_list_show_and_routing_never_approves(
    run: SliceRun, capsys: pytest.CaptureFixture[str]
) -> None:
    item = pending_source(run)
    assert item.id in {i.id for i in queue(run.database, None)}
    assert call(run, "list") == 0
    assert item.id in capsys.readouterr().out
    assert call(run, "show", "source", item.id) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["detail"]["proposal"]["evidence"][0]["source_text"]
    assert not shown["decided"] and not shown["detail"]["stale"]
    assert "actor" not in shown["detail"]["proposal"]
    assert "decision" not in shown["detail"]["proposal"]


@pytest.mark.parametrize("kind", ["product", "source"])
@pytest.mark.parametrize("command", ["approve", "reject"])
def test_hash_cas_refuses_wrong_or_changed_content(
    run: SliceRun, kind: str, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    item = pending_product(run) if kind == "product" else pending_source(run)
    assert decision(run, item, command, sha256="0" * 64) == 1
    engine = open_operational_database(run.database)
    try:
        with engine.begin() as conn:
            if kind == "product":
                conn.execute(
                    s.products.update()
                    .where(s.products.c.product_id == item.id)
                    .values(name="Changed after listing")
                )
            else:
                conn.execute(
                    s.rate_sources.update()
                    .where(s.rate_sources.c.source_id == item.detail["proposal"]["source_id"])
                    .values(url="https://changed.example/rates")
                )
        assert decision(run, item, command) == 1
        assert "content hash changed" in capsys.readouterr().err
        if kind == "source":
            with engine.connect() as conn:
                fresh = source_item(conn, item.id)
            assert fresh.detail["stale"]
            assert decision(run, fresh) == 1
            assert "stale source proposal" in capsys.readouterr().err
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind", ["product", "source"])
@pytest.mark.parametrize("missing", ["actor", "note", "sha256"])
def test_required_decision_arguments(run: SliceRun, kind: str, missing: str) -> None:
    item = pending_product(run) if kind == "product" else pending_source(run)
    flags = dict(actor=ACTOR, note=NOTE, sha256=item.sha256)
    flags.pop(missing)
    with pytest.raises(SystemExit) as exc:
        call(run, "approve", kind, item.id, *(p for k, v in flags.items() for p in (f"--{k}", v)))
    assert exc.value.code == 2
    if missing != "sha256":
        assert decision(run, item, **{missing: "  "}) == 1


def test_quarantined_is_visible_with_reason_and_rerun_but_read_only(
    run: SliceRun, capsys: pytest.CaptureFixture[str]
) -> None:
    offer = run.offers["purchase-30-year"]
    engine = open_operational_database(run.database)
    try:
        with engine.begin() as conn:
            conn.execute(
                s.normalized_offers.update()
                .where(s.normalized_offers.c.offer_id == offer.offer_id)
                .values(status="quarantined")
            )
            # Failed checks are append-only history. Preserve and display their recorded reason.
            row = dict(
                conn.execute(
                    select(s.validation_results).where(
                        s.validation_results.c.offer_id == offer.offer_id
                    )
                )
                .mappings()
                .first()
                or {}
            )
            row.update(
                validation_id="console-test-failed", passed=0, detail="synthetic failed validation"
            )
            conn.execute(s.validation_results.insert().values(**row))
        item = next(i for i in queue(run.database, None) if i.kind == "quarantined")
        assert "synthetic failed validation" in item.summary and "rerun" in item.summary
        assert call(run, "list") == 0
        assert item.id in capsys.readouterr().out
        assert call(run, "show", item.kind, item.id) == 0
        assert "synthetic failed validation" in capsys.readouterr().out
        assert decision(run, item) == 1
        assert decision(run, item, "reject") == 1
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(s.normalized_offers.c.status).where(
                        s.normalized_offers.c.offer_id == offer.offer_id
                    )
                ).scalar_one()
                == "quarantined"
            )
    finally:
        engine.dispose()


def test_corrections_read_only_no_contacts_and_existing_commands(
    run: SliceRun, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "intake" / "intake.sqlite"
    store = IntakeStore(path)
    store.initialize()
    case_id = store.record_case(_case(contact="NEVER-PRINT@example.test"), now=NOW)
    before = path.read_bytes()
    args = ["--db", str(run.database), "--intake-db", str(path)]
    assert main([*args, "list"], now=NOW) == 0
    listing = capsys.readouterr().out
    assert case_id in listing and "cu_rates.intake" in listing
    assert main([*args, "show", "correction", case_id], now=NOW) == 0
    shown = capsys.readouterr().out
    html = create_app(run.database, path).test_client().get("/").get_data(as_text=True)
    assert "NEVER-PRINT" not in listing + shown + html
    assert "reply_contact" not in shown and "reporter_contacts" not in shown
    for command in ("approve", "reject"):
        assert (
            main(
                [
                    *args,
                    command,
                    "correction",
                    case_id,
                    "--sha256",
                    "0" * 64,
                    "--actor",
                    ACTOR,
                    "--note",
                    NOTE,
                ],
                now=NOW,
            )
            == 1
        )
    assert path.read_bytes() == before
    store.resolve(case_id, Resolution.REJECTED, actor=ACTOR, note=NOTE, at=NOW)
    assert case_id not in {i.id for i in queue(run.database, path)}


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::1", "localhost", "192.168.1.2"])
def test_bind_refuses_everything_except_127_0_0_1(
    run: SliceRun, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_start(*args: Any, **kwargs: Any) -> None:
        pytest.fail("non-loopback server started")

    monkeypatch.setattr(Flask, "run", must_not_start)
    with pytest.raises(ValueError, match=r"127\.0\.0\.1 only"):
        serve(run.database, None, host=host, port=8765)


def test_serve_loopback_and_no_debug(run: SliceRun, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: calls.append(kwargs))
    assert call(run, "serve", "--port", "8765") == 0
    assert calls == [dict(host="127.0.0.1", port=8765, debug=False, use_reloader=False)]


def test_html_escapes_all_content_csp_no_actions_or_external_requests(run: SliceRun) -> None:
    item = pending_product(run, name='<script src="https://evil.example/x">&</script>')
    pending_source(run, reason='<img src="https://evil.example/pixel">')
    client = create_app(run.database).test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "&lt;script" in html and "&lt;img" in html and "&amp;" in html
    assert "<script" not in html and "<img" not in html and "<form" not in html
    assert item.id in html
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/").status_code == 405
    assert client.get("/approve").status_code == 404
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_read_only_engine_and_missing_db(run: SliceRun, tmp_path: Path) -> None:
    engine = read_engine(run.database)
    try:
        with engine.connect() as conn, pytest.raises(Exception, match="readonly"):
            conn.execute(s.products.update().values(name="forbidden"))
    finally:
        engine.dispose()
    missing = tmp_path / "missing.sqlite"
    assert main(["--db", str(missing), "list"]) == 1
    assert not missing.exists()


@pytest.mark.parametrize("kind", ["product", "source"])
def test_concurrent_decisions_only_append_once(run: SliceRun, kind: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    item = pending_product(run) if kind == "product" else pending_source(run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decision(run, item), range(2)))
    assert sorted(results) == [0, 1]
    engine = read_engine(run.database)
    try:
        with engine.connect() as conn:
            if kind == "product":
                reviews = conn.execute(
                    select(s.review_decisions).where(
                        s.review_decisions.c.subject_id == item.id,
                        s.review_decisions.c.actor == ACTOR,
                    )
                ).all()
            else:
                reviews = conn.execute(
                    select(s.source_approvals).where(s.source_approvals.c.approval_id == item.id)
                ).all()
            assert len(reviews) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("binding", ["profile", "relationship", "institution"])
def test_source_hash_covers_all_mutable_bindings(run: SliceRun, binding: str) -> None:
    item = pending_source(run)
    engine = open_operational_database(run.database)
    try:
        with engine.begin() as conn:
            source_id = item.detail["proposal"]["source_id"]
            if binding == "profile":
                conn.execute(
                    s.fetch_profiles.update()
                    .where(s.fetch_profiles.c.source_id == source_id)
                    .values(timeout_seconds=299)
                )
            elif binding == "relationship":
                conn.execute(
                    s.institution_source_relationships.update()
                    .where(s.institution_source_relationships.c.source_id == source_id)
                    .values(approved_by="different reviewer")
                )
            else:
                conn.execute(
                    s.institutions.update()
                    .where(s.institutions.c.charter_number == CHARTER)
                    .values(legal_name="Changed name")
                )
        assert decision(run, item) == 1
    finally:
        engine.dispose()


def test_queue_summaries_cannot_inject_terminal_rows(
    run: SliceRun, capsys: pytest.CaptureFixture[str]
) -> None:
    pending_source(run, reason="Reason\nforged row\x1b[31m")
    assert call(run, "list") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\nforged row" not in out

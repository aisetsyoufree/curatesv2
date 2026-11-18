"""Offline T032 tests. The only HTTP transport is synthetic."""

import fcntl
import gzip
import ipaddress
import sqlite3
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from cu_rates import fetch_worker
from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import (
    EvidenceKind,
    FetchMethod,
    ReviewVerdict,
    RunStatus,
    WithdrawalScope,
)
from cu_rates.contracts.sources import (
    ConditionalValidator,
    FetchProfile,
    RateSource,
    SourceApproval,
    profile_sha256,
)
from cu_rates.db import metadata, open_operational_database
from cu_rates.db.schema import withdrawal_entries
from cu_rates.fetch_dialer import TOTAL_TIMEOUT_EXTENSION, LoopbackOverride, PinnedTransport
from cu_rates.fetch_policy import FetchRegistry, TermsLedger
from cu_rates.fetch_worker import (
    ROBOTS_TTL,
    USER_AGENT,
    FetchApproval,
    FetchResult,
    FetchWorker,
    TermsReview,
)
from cu_rates.pipeline.trusted import (
    insert_approval,
    insert_profile,
    insert_source,
)
from cu_rates.snapshot import Denylist

URL = "https://credit-union.test/rates"
BODY = b"<html>Synthetic fixture</html>"
PUBLIC = ("8.8.8.8",)
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def public_resolver(host: str) -> tuple[str, ...]:
    del host
    return PUBLIC


@dataclass
class Clock:
    value: float = datetime(2026, 9, 1, tzinfo=UTC).timestamp()

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class Pilot:
    def __init__(self, root: Path):
        self.path = root
        self.clock = Clock()
        self.calls: list[tuple[float, httpx.Request]] = []
        self.dialed: list[tuple[_IPAddress, ...]] = []
        self.store = ArtifactStore(root / "evidence")
        stamp = datetime.fromtimestamp(self.clock.value, UTC)
        self.source = RateSource(
            schema_version=CONTRACT_VERSION,
            source_id="fixture",
            url=URL,
            evidence_kind=EvidenceKind.HTML,
        )
        self.profile = FetchProfile(
            schema_version=CONTRACT_VERSION,
            profile_id="fixture.http",
            source_id="fixture",
            method=FetchMethod.HTTP_GET,
            min_interval_seconds=60,
            timeout_seconds=10,
        )
        self.approval = FetchApproval(
            schema_version=CONTRACT_VERSION,
            approval_id="fixture.approval",
            source_id="fixture",
            source_url=URL,
            decision=ReviewVerdict.APPROVED,
            actor="Synthetic reviewer",
            decided_at=stamp,
            policy_id="d-12",
            policy_version="1",
            rationale="Offline test only",
            approved_profile_sha256=profile_sha256(self.profile),
            profile=self.profile,
            evidence_kind=EvidenceKind.HTML,
        )
        self.terms = TermsReview.model_validate(
            {
                **self.approval.model_dump(mode="json", exclude={"profile", "evidence_kind"}),
                "approval_id": "fixture.terms",
                "terms_url": "https://credit-union.test/terms",
                "terms_sha256": "a" * 64,
            }
        )
        self.database = root / "registry.sqlite"
        self.denylist = Denylist(root / "independent" / "withdrawals.json")
        self.denylist.initialize()
        self.terms_ledger = TermsLedger(root / "private" / "terms.json")
        self.terms_ledger.initialize()
        self.registry = FetchRegistry(
            self.database, self.terms_ledger, self.denylist, automatic=None
        )
        self.persist_policy()

    def persist_policy(self) -> None:
        """Seed a fresh synthetic registry; never run this in worker/fetch calls."""
        self.database.unlink(missing_ok=True)
        engine = open_operational_database(self.database)
        metadata.create_all(engine)
        with engine.begin() as conn:
            insert_source(conn, self.source)
            insert_profile(conn, self.profile)
            insert_approval(
                conn,
                SourceApproval.model_validate(
                    self.approval.model_dump(exclude={"profile", "evidence_kind"})
                ),
                9000001,
            )
        engine.dispose()
        self.terms_ledger.path.unlink()
        self.terms_ledger.initialize()
        self.terms_ledger.record(self.terms, denylist=self.denylist)

    def worker(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        resolve: Callable[[str], Iterable[str | _IPAddress]] | None = None,
        *,
        monotonic: Callable[[], float] | None = None,
        boot_id: Callable[[], str] | None = None,
    ) -> FetchWorker:
        def transport_factory(addresses: tuple[_IPAddress, ...]) -> httpx.BaseTransport:
            self.dialed.append(addresses)
            return httpx.MockTransport(send)

        def send(request: httpx.Request) -> httpx.Response:
            self.calls.append((self.clock.now(), request))
            return handler(request)

        return FetchWorker(
            self.path / "state",
            self.store,
            transport_factory,
            git_sha="0" * 40,
            registry=self.registry,
            clock=self.clock.now,
            monotonic=self.clock.now if monotonic is None else monotonic,
            sleep=self.clock.sleep,
            resolve=public_resolver if resolve is None else resolve,
            boot_id=boot_id,
        )

    def fetch(self, handler: Callable[[httpx.Request], httpx.Response]) -> FetchResult:
        return self.worker(handler).fetch(self.source, self.profile)


@pytest.fixture
def pilot(tmp_path: Path) -> Pilot:
    return Pilot(tmp_path)


def robots(body: bytes = b"User-agent: *\nDisallow:\n") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "text/plain"})


def page(**headers: str) -> httpx.Response:
    return httpx.Response(200, content=BODY, headers={"Content-Type": "text/html", **headers})


def ok(request: httpx.Request) -> httpx.Response:
    return robots() if request.url.path == "/robots.txt" else page()


def test_approved_fetch_stores_actual_body_and_spaces_requests(pilot: Pilot) -> None:
    result = pilot.fetch(ok)
    assert result.run is not None and result.run.status is RunStatus.SUCCEEDED
    assert result.artifact is not None and result.artifact.fetch_run_id == result.run.run_id
    assert pilot.store.read(result.artifact) == BODY
    assert result.run.artifact_ids == (result.artifact.artifact_id,)
    assert [req.url.path for _, req in pilot.calls] == ["/robots.txt", "/rates"]
    assert pilot.calls[1][0] - pilot.calls[0][0] >= 5
    for _, req in pilot.calls:
        assert req.headers["user-agent"] == USER_AGENT
        assert "cookie" not in req.headers and "authorization" not in req.headers
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 2


@pytest.mark.parametrize("kind", ["rejected", "withdrawn", "expired", "wrong-source", "future"])
def test_no_http_without_approval(pilot: Pilot, kind: str) -> None:
    if kind == "rejected":
        pilot.approval = pilot.approval.model_copy(update={"decision": ReviewVerdict.REJECTED})
    if kind == "wrong-source":
        pilot.approval = pilot.approval.model_copy(update={"source_url": URL + "-other"})
    if kind == "expired":
        pilot.terms = pilot.terms.model_copy(
            update={"decided_at": pilot.terms.decided_at - timedelta(days=91)},
        )
    if kind == "future":
        pilot.approval = pilot.approval.model_copy(
            update={"decided_at": pilot.approval.decided_at + timedelta(days=1)},
        )
    pilot.persist_policy()
    if kind == "withdrawn":
        pilot.denylist.withdraw(
            WithdrawalEntry(
                schema_version=CONTRACT_VERSION,
                withdrawal_id="test-withdrawn",
                scope=WithdrawalScope.SOURCE,
                target_id=pilot.source.source_id,
                actor="Synthetic reviewer",
                withdrawn_at=pilot.approval.decided_at,
                reason="Synthetic withdrawal",
            )
        )
    result = pilot.worker(ok).fetch(pilot.source, pilot.profile)
    assert result.artifact is None and result.run is None
    assert not pilot.calls


@pytest.mark.parametrize(
    ("code", "expected_reason"),
    [
        (301, "robots-unavailable"),
        (401, "robots-unavailable"),
        (403, "http-403"),
        (429, "http-429"),
        (503, "robots-unavailable"),
    ],
)
def test_inaccessible_robots_stops_before_source(
    pilot: Pilot, code: int, expected_reason: str
) -> None:
    assert pilot.fetch(lambda _: httpx.Response(code)).reason == expected_reason
    assert [req.url.path for _, req in pilot.calls] == ["/robots.txt"]


def test_disallow_and_allow_overlap_fails_closed(pilot: Pilot) -> None:
    result = pilot.fetch(
        lambda _: robots(
            b"User-agent: *\nAllow: /rates\nDisallow: /rates\n",
        )
    )
    assert result.reason == "robots-disallowed" and result.artifact is None
    assert len(pilot.calls) == 1
    pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"
    assert len(pilot.calls) == 1


@pytest.mark.parametrize("target", ["robots", "robots-403", "body-200", "body-403", "body-503"])
def test_challenge_prefix_immediately_pauses_target(pilot: Pilot, target: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt" and not target.startswith("robots"):
            return robots()
        code = int(target.split("-")[-1]) if "-" in target else 503
        return httpx.Response(
            code,
            content=b"<html><div class='g-recaptcha'></div></html>",
            headers={"Content-Type": "text/html"},
        )

    result = pilot.fetch(handler)
    assert result.reason == "access-challenge"
    assert result.run is not None and result.run.status is RunStatus.BLOCKED
    assert len(pilot.calls) == (1 if target.startswith("robots") else 2)
    assert list(pilot.store.observations()) == []
    pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_challenge_header_stops_without_decoding_body(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(
            403, headers={"Content-Encoding": "gzip", "CF-Mitigated": "challenge"}
        )

    assert pilot.fetch(handler).reason == "access-challenge"
    pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_changed_profile_or_reused_terms_decision_does_not_fetch(pilot: Pilot) -> None:
    profile = pilot.profile.model_copy(update={"min_interval_seconds": 5})
    assert pilot.worker(ok).fetch(pilot.source, profile).run is None
    pilot.terms = pilot.terms.model_copy(update={"approval_id": pilot.approval.approval_id})
    pilot.persist_policy()
    assert pilot.worker(ok).fetch(pilot.source, pilot.profile).run is None
    assert not pilot.calls


def test_three_terminal_403_runs_pause_without_retry(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return robots() if req.url.path == "/robots.txt" else httpx.Response(403)

    for _ in range(3):
        assert pilot.fetch(handler).reason == "http-403"
        pilot.clock.sleep(60)
    assert len(pilot.calls) == 4
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_forward_wall_clock_jump_does_not_shorten_spacing(pilot: Pilot) -> None:
    tick = Clock()

    def wait(delay: float) -> None:
        pilot.clock.sleep(delay)
        tick.sleep(delay)

    calls: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(tick.now())
        if req.url.path == "/robots.txt":
            pilot.clock.sleep(600)
            return robots(b"User-agent: *\nCrawl-delay: 15\n")
        return page()

    worker = FetchWorker(
        pilot.path / "state",
        pilot.store,
        lambda _: httpx.MockTransport(handler),
        git_sha="0" * 40,
        registry=pilot.registry,
        clock=pilot.clock.now,
        monotonic=tick.now,
        sleep=wait,
        resolve=public_resolver,
    )
    assert worker.fetch(pilot.source, pilot.profile).reason == "fetched"
    assert calls[1] - calls[0] >= 15


def test_robots_crawl_delay_and_expiry(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path != "/robots.txt":
            return page()
        if pilot.clock.value > initial + ROBOTS_TTL:
            return httpx.Response(503)
        return robots(b"User-agent: *\nCrawl-delay: 15\n")

    initial = pilot.clock.now()
    assert pilot.fetch(handler).reason == "fetched"
    assert pilot.calls[1][0] - pilot.calls[0][0] == 15
    pilot.clock.sleep(ROBOTS_TTL + 1)
    assert pilot.fetch(handler).reason == "robots-unavailable"
    assert [req.url.path for _, req in pilot.calls] == ["/robots.txt", "/rates", "/robots.txt"]


@pytest.mark.parametrize("code", [302, 401, 403])
def test_target_denial_does_not_retry_follow_or_store(pilot: Pilot, code: int) -> None:
    result = pilot.fetch(
        lambda req: (
            robots()
            if req.url.path == "/robots.txt"
            else httpx.Response(code, headers={"Location": "https://elsewhere.test/"})
        )
    )
    assert result.reason == f"http-{code}"
    assert len(pilot.calls) == 2 and list(pilot.store.observations()) == []


def test_challenge_pauses_across_worker_instances(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return robots() if req.url.path == "/robots.txt" else page(**{"CF-Mitigated": "challenge"})

    assert pilot.fetch(handler).reason == "access-challenge"
    pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"
    assert len(pilot.calls) == 2


def test_retry_and_long_defer_survive_a_new_instance(pilot: Pilot) -> None:
    statuses = iter((503, 429))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        code = next(statuses)
        return httpx.Response(code, headers={"Retry-After": "1200"} if code == 429 else {})

    assert pilot.fetch(handler).reason == "http-429-deferred"
    assert pilot.calls[2][0] - pilot.calls[1][0] >= 30
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 3
    pilot.clock.sleep(601)
    assert pilot.fetch(ok).reason == "fetched"


def test_reobserved_304_preserves_the_original_success(pilot: Pilot) -> None:
    first = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else page(ETag='"v1"')
    )
    pilot.clock.sleep(60)

    def unchanged(req: httpx.Request) -> httpx.Response:
        assert req.headers["if-none-match"] == '"v1"'
        return httpx.Response(304, headers={"ETag": '"v1"'})

    result = pilot.fetch(unchanged)
    item = result.reobserved
    assert item is not None and first.artifact is not None
    assert item.original == first.artifact and item.http_status == 304
    assert item.observed_at > first.artifact.fetched_at
    assert item.run_id != first.artifact.fetch_run_id
    assert item.profile_id == pilot.profile.profile_id
    assert item.started_at <= item.observed_at
    assert result.run is None and result.artifact is None
    assert list(pilot.store.observations()) == [first.artifact]


def test_304_records_the_validator_the_original_200_carried(pilot: Pilot) -> None:
    first = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else page(ETag='"v1"')
    )
    assert first.run is not None and first.artifact is not None
    assert first.run.conditional_validator == ConditionalValidator(kind="etag", value='"v1"')
    pilot.clock.sleep(60)
    result = pilot.fetch(lambda _: httpx.Response(304, headers={"ETag": '"v1"'}))
    item = result.reobserved
    assert item is not None
    assert item.validator == first.run.conditional_validator
    record = item.record()
    assert record.original_run_id == first.run.run_id
    assert record.original_artifact_id == first.artifact.artifact_id
    assert record.profile_id == pilot.profile.profile_id and record.http_status == 304
    assert record.observed_at == item.observed_at
    digest = profile_sha256(pilot.profile)
    assert first.run.profile_sha256 == digest == record.profile_sha256
    assert record.response_etag == '"v1"' == first.run.conditional_validator.value


def test_304_without_an_echoed_etag_is_recorded_but_not_publication_proof(pilot: Pilot) -> None:
    first = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else page(ETag='"v1"')
    )
    assert first.run is not None
    pilot.clock.sleep(60)
    result = pilot.fetch(lambda _: httpx.Response(304))
    item = result.reobserved
    assert item is not None
    assert item.response_etag is None and item.record().response_etag is None
    assert item.validator == first.run.conditional_validator


def test_every_run_records_the_profile_digest(pilot: Pilot) -> None:
    run = pilot.fetch(ok).run
    assert run is not None and run.profile_sha256 == profile_sha256(pilot.profile)


def test_last_modified_is_the_validator_when_there_is_no_etag(pilot: Pilot) -> None:
    stamp = "Tue, 01 Sep 2026 00:00:00 GMT"
    first = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else page(**{"Last-Modified": stamp})
    )
    assert first.run is not None
    assert first.run.conditional_validator == ConditionalValidator(
        kind="last_modified", value=stamp
    )


def test_200_without_a_validator_records_none(pilot: Pilot) -> None:
    first = pilot.fetch(ok)
    assert first.run is not None and first.run.conditional_validator is None


def test_bare_304_is_not_fresh_evidence(pilot: Pilot) -> None:
    result = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else httpx.Response(304)
    )
    assert result.reason == "unexpected-304"
    assert result.reobserved is None


def test_equal_200s_keep_both_fetch_events(pilot: Pilot) -> None:
    first = pilot.fetch(ok).artifact
    pilot.clock.sleep(60)
    second = pilot.fetch(ok).artifact
    assert first is not None and second is not None
    assert first.artifact_id == second.artifact_id and first.fetch_run_id != second.fetch_run_id
    assert list(pilot.store.observations()) == [first, second]


def test_worker_lock_blocks_a_second_collector(pilot: Pilot) -> None:
    worker = pilot.worker(ok)
    with (pilot.path / "state" / "worker.lock").open("wb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "worker-busy"
    assert not pilot.calls


def test_size_limit_does_not_write_evidence(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_worker, "MAX_BODY", 10)
    assert pilot.fetch(ok).reason == "body-too-large"
    assert list(pilot.store.observations()) == []


def test_304_rejects_tampered_cached_body(pilot: Pilot) -> None:
    first = pilot.fetch(
        lambda req: robots() if req.url.path == "/robots.txt" else page(ETag='"v1"')
    )
    assert first.artifact is not None
    digest = first.artifact.artifact_id
    blob = pilot.path / "evidence" / "blobs" / digest[:2] / digest
    blob.chmod(0o600)
    blob.write_bytes(b"not the fetched response")
    blob.chmod(0o400)
    pilot.clock.sleep(60)
    result = pilot.fetch(lambda _: httpx.Response(304, headers={"ETag": '"v1"'}))
    assert result.reason == "cache-invalid" and result.reobserved is None


def test_http_date_backoff_is_honored(pilot: Pilot) -> None:
    sent = False

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal sent
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if sent:
            return page()
        sent = True
        after = datetime.fromtimestamp(pilot.clock.now() + 90, UTC)
        return httpx.Response(429, headers={"Retry-After": format_datetime(after, usegmt=True)})

    assert pilot.fetch(handler).reason == "fetched"
    assert pilot.calls[2][0] - pilot.calls[1][0] >= 90


@pytest.mark.parametrize(
    "answers",
    [
        ("127.0.0.1",),
        ("10.1.2.3",),
        ("192.168.1.1",),
        ("169.254.1.1",),
        ("169.254.169.254",),
        ("::ffff:127.0.0.1",),
        ("::ffff:10.0.0.1",),
        ("::ffff:169.254.169.254",),
        ("8.8.8.8", "10.0.0.8"),
    ],
)
def test_non_routable_resolution_is_refused_before_connect(
    pilot: Pilot, answers: tuple[str, ...]
) -> None:
    result = pilot.worker(ok, resolve=lambda host: answers).fetch(pilot.source, pilot.profile)
    assert result.reason == "non-routable-address"
    assert pilot.calls == []


@pytest.mark.parametrize("failure", [OSError("dns failed"), ()])
def test_resolution_failure_is_refused(pilot: Pilot, failure: OSError | tuple[()]) -> None:
    def resolve(host: str) -> tuple[str, ...]:
        del host
        if isinstance(failure, OSError):
            raise failure
        return failure

    result = pilot.worker(ok, resolve=resolve).fetch(pilot.source, pilot.profile)
    assert result.reason == "address-resolution-failed"
    assert pilot.calls == []


def test_redirect_does_not_resolve_destination_and_is_rejected(pilot: Pilot) -> None:
    def resolve(host: str) -> tuple[str, ...]:
        if host != "credit-union.test":
            raise AssertionError(f"Redirect destination {host} must not be resolved")
        return PUBLIC

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/latest"})

    result = pilot.worker(handler, resolve=resolve).fetch(pilot.source, pilot.profile)
    assert result.reason == "http-302"
    assert [req.url.host for _, req in pilot.calls] == ["credit-union.test", "credit-union.test"]


def test_redirect_with_challenge_header_is_access_challenge(pilot: Pilot) -> None:
    def resolve(host: str) -> tuple[str, ...]:
        if host != "credit-union.test":
            raise AssertionError(f"Redirect destination {host} must not be resolved")
        return PUBLIC

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(
            302,
            headers={"Location": "https://elsewhere.test/login", "CF-Mitigated": "challenge"},
        )

    result = pilot.worker(handler, resolve=resolve).fetch(pilot.source, pilot.profile)
    assert result.reason == "access-challenge"
    assert result.run is not None and result.run.status is RunStatus.BLOCKED
    pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_transport_receives_validated_addresses(pilot: Pilot) -> None:
    result = pilot.fetch(ok)
    assert result.reason == "fetched"
    assert len(pilot.dialed) == 2
    for addrs in pilot.dialed:
        assert addrs == (ipaddress.IPv4Address("8.8.8.8"),)


def pinned_worker(
    pilot: Pilot, pinned: Callable[[tuple[_IPAddress, ...]], PinnedTransport]
) -> FetchWorker:
    return FetchWorker(
        pilot.path / "state",
        pilot.store,
        pinned,
        git_sha="0" * 40,
        registry=pilot.registry,
        clock=pilot.clock.now,
        monotonic=pilot.clock.now,
        sleep=pilot.clock.sleep,
        resolve=public_resolver,
    )


def test_worker_dispatches_the_gateway_dialer_pinned_to_validated_addresses(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[str] = []

    def stub(transport: PinnedTransport, request: httpx.Request) -> httpx.Response:
        served.append(request.url.path)
        return ok(request)

    monkeypatch.setattr(PinnedTransport, "handle_request", stub)  # never touches a network
    worker = pinned_worker(pilot, PinnedTransport)
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "fetched"
    assert served == ["/robots.txt", "/rates"]


def test_worker_passes_the_profile_deadline_to_the_dialer(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def stub(transport: PinnedTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get(TOTAL_TIMEOUT_EXTENSION))
        return ok(request)

    monkeypatch.setattr(PinnedTransport, "handle_request", stub)
    pinned_worker(pilot, PinnedTransport).fetch(pilot.source, pilot.profile)
    assert seen == [pilot.profile.timeout_seconds] * 2


class NoChunks(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        return iter(())


def test_empty_body_past_the_profile_deadline_is_stopped(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        pilot.clock.sleep(pilot.profile.timeout_seconds + 5)
        # A stream that yields no chunk at all, as a real empty body does.
        return httpx.Response(200, stream=NoChunks(), headers={"Content-Type": "text/html"})

    result = pilot.fetch(handler)
    assert result.reason == "response-time-limit" and result.artifact is None


def test_late_304_past_the_profile_deadline_is_not_reobserved(pilot: Pilot) -> None:
    pilot.fetch(lambda req: robots() if req.url.path == "/robots.txt" else page(ETag='"v1"'))
    pilot.clock.sleep(60)

    def late(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        pilot.clock.sleep(pilot.profile.timeout_seconds + 5)
        return httpx.Response(304, headers={"ETag": '"v1"'})

    result = pilot.fetch(late)
    assert result.reason == "response-time-limit" and result.reobserved is None


def test_resolver_rebinding_never_reaches_the_dialer(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([("8.8.8.8",), ("127.0.0.1",)])
    dialed: list[tuple[_IPAddress, ...]] = []

    def resolve(host: str) -> tuple[str, ...]:
        return next(answers)

    def factory(addresses: tuple[_IPAddress, ...]) -> PinnedTransport:
        dialed.append(addresses)
        return PinnedTransport(addresses)

    monkeypatch.setattr(PinnedTransport, "handle_request", lambda t, request: ok(request))
    worker = FetchWorker(
        pilot.path / "state",
        pilot.store,
        factory,
        git_sha="0" * 40,
        registry=pilot.registry,
        clock=pilot.clock.now,
        monotonic=pilot.clock.now,
        sleep=pilot.clock.sleep,
        resolve=resolve,
    )
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "non-routable-address"
    assert dialed == [(ipaddress.IPv4Address("8.8.8.8"),)]  # the rebound answer never got a dialer


def test_worker_refuses_a_dialer_not_pinned_to_the_validated_set(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(transport: PinnedTransport, request: httpx.Request) -> httpx.Response:
        pytest.fail("a mispinned dialer reached dispatch")

    monkeypatch.setattr(PinnedTransport, "handle_request", forbidden)
    worker = pinned_worker(
        pilot, lambda addresses: PinnedTransport([ipaddress.ip_address("8.8.4.4")])
    )
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "pin-mismatch"


def test_worker_refuses_a_dialer_with_the_test_override(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(transport: PinnedTransport, request: httpx.Request) -> httpx.Response:
        pytest.fail("an overridden dialer reached dispatch")

    monkeypatch.setattr(PinnedTransport, "handle_request", forbidden)
    worker = pinned_worker(
        pilot, lambda addresses: PinnedTransport(addresses, test_override=LoopbackOverride(8443))
    )
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "live-transport-disabled"


def test_default_worker_has_no_resolver_so_nothing_can_dial(pilot: Pilot) -> None:
    def factory(addresses: tuple[_IPAddress, ...]) -> httpx.BaseTransport:
        pytest.fail("a transport was built without a configured resolver")

    worker = FetchWorker(
        pilot.path / "state",
        pilot.store,
        factory,
        git_sha="0" * 40,
        registry=pilot.registry,
        clock=pilot.clock.now,
        monotonic=pilot.clock.now,
        sleep=pilot.clock.sleep,
    )
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "address-resolution-failed"


@pytest.mark.parametrize("after_robots", [False, True])
def test_network_transport_is_refused_before_dispatch(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch, after_robots: bool
) -> None:
    def should_not_dispatch(
        transport: httpx.HTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        pytest.fail("a network-capable transport reached dispatch")

    factories_called = 0

    def factory(addresses: tuple[_IPAddress, ...]) -> httpx.BaseTransport:
        nonlocal factories_called
        factories_called += 1
        if after_robots and factories_called == 1:
            return httpx.MockTransport(ok)
        return httpx.HTTPTransport()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", should_not_dispatch)
    worker = FetchWorker(
        pilot.path / "state",
        pilot.store,
        factory,
        git_sha="0" * 40,
        registry=pilot.registry,
        clock=pilot.clock.now,
        monotonic=pilot.clock.now,
        sleep=pilot.clock.sleep,
        resolve=public_resolver,
    )
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "live-transport-disabled"
    assert factories_called == (2 if after_robots else 1)


@pytest.mark.parametrize(
    "denied_ip",
    [
        "fec0::1",
        "fec0::ffff",
        "100.64.0.1",
        "100.127.255.254",
        "192.0.0.1",
        "198.18.0.1",
        "198.19.255.254",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "2001:db8::1",
        "240.0.0.1",
        "250.1.2.3",
        "64:ff9b::127.0.0.1",
        "64:ff9b::10.0.0.1",
        "64:ff9b::192.168.1.1",
        "64:ff9b::169.254.1.1",
        "64:ff9b::100.64.0.1",
        "64:ff9b:1::1",
        "2002:7f00:0001::",
        "2002:0a00:0001::",
        "2002:c0a8:0101::",
        "2002:a9fe:0101::",
        "2002:6440:0001::",
        "2001:0000:4136:e378:8000:63bf:f5ff:ffee",
        "fc00::1",
        "fd00::1",
        "fd12:3456:789a::1",
        "fe80::1",
        "2001:2::1",
        "100::1",
        "4000::1",
        "3fff::1",
        "100:0:0:1::1",
        "64:ff9b::8.8.8.8",
        "2002:0808:0808::",
        "2607:f8b0:4004:800::200e%test",
        "2001:20::1",
    ],
)
def test_special_use_denylist_rejects_non_routable_addresses(pilot: Pilot, denied_ip: str) -> None:
    result = pilot.worker(ok, resolve=lambda host: (denied_ip,)).fetch(pilot.source, pilot.profile)
    assert result.reason == "non-routable-address"
    assert pilot.calls == []


@pytest.mark.parametrize(
    "allowed_ip",
    [
        "8.8.8.8",
        "2607:f8b0:4004:800::200e",
        "::ffff:8.8.8.8",
    ],
)
def test_globally_routable_public_addresses_are_permitted(pilot: Pilot, allowed_ip: str) -> None:
    result = pilot.worker(ok, resolve=lambda host: (allowed_ip,)).fetch(pilot.source, pilot.profile)
    assert result.reason == "fetched"
    assert len(pilot.calls) == 2


@pytest.mark.parametrize("code", [403, 429])
@pytest.mark.parametrize("from_robots", [True, False])
def test_denial_counts_once_per_scheduled_run(pilot: Pilot, code: int, from_robots: bool) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt" and not from_robots:
            return robots()
        return httpx.Response(code, headers={"Content-Encoding": "gzip", "Retry-After": "10"})

    for expected in (RunStatus.FAILED, RunStatus.FAILED, RunStatus.BLOCKED):
        result = pilot.fetch(handler)
        assert result.reason == f"http-{code}"
        assert result.run is not None and result.run.status is expected
        pilot.clock.sleep(180)
    if from_robots:
        assert [req.url.path for _, req in pilot.calls] == ["/robots.txt"] * 3
    else:
        attempts = 3 if code == 429 else 1
        assert [req.url.path for _, req in pilot.calls] == [
            "/robots.txt",
            *["/rates"] * (3 * attempts),
        ]
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_robots_403_honors_retry_after(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Retry-After": "1200"})

    result = pilot.fetch(handler)
    assert result.reason == "http-403-deferred"
    assert len(pilot.calls) == 1
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 1
    pilot.clock.sleep(601)
    assert pilot.fetch(ok).reason == "fetched"
    assert len(pilot.calls) == 3


def test_source_403_honors_retry_after_without_retry(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(403, headers={"Retry-After": "1200"})

    result = pilot.fetch(handler)
    assert result.reason == "http-403-deferred"
    assert len(pilot.calls) == 2
    assert [req.url.path for _, req in pilot.calls] == ["/robots.txt", "/rates"]
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 2


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate"])
def test_compressed_503_records_status_and_retry_after(pilot: Pilot, encoding: str) -> None:
    if encoding == "gzip":
        content = gzip.compress(b"service unavailable")
    elif encoding == "deflate":
        content = zlib.compress(b"service unavailable")
    else:
        content = b"service unavailable"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(
            503,
            headers={"Content-Encoding": encoding, "Retry-After": "1200"},
            content=content,
        )

    result = pilot.fetch(handler)
    assert result.reason == "http-503-deferred"
    assert len(pilot.calls) == 2
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 2


def test_failed_denial_body_read_preserves_status_and_retry_after(pilot: Pilot) -> None:
    class FaultyStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise httpx.ReadError("stream read failure")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(
            429,
            headers={"Retry-After": "1200"},
            stream=FaultyStream(),
        )

    result = pilot.fetch(handler)
    assert result.reason == "http-429-deferred"
    assert len(pilot.calls) == 2
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 2


@pytest.mark.parametrize("challenge", [False, True])
def test_robots_error_records_backoff_before_classification(pilot: Pilot, challenge: bool) -> None:
    expected_until = pilot.clock.now() + 1200
    headers = {"Content-Encoding": "gzip", "Retry-After": "1200"}
    if challenge:
        headers["CF-Mitigated"] = "challenge"
    result = pilot.fetch(lambda _: httpx.Response(429 if challenge else 503, headers=headers))
    assert result.reason == ("access-challenge" if challenge else "robots-unavailable")
    pilot.clock.sleep(61)
    if not challenge:
        assert pilot.fetch(ok).reason == "not-due"
    else:
        with pilot.worker(ok)._state() as state:
            deadline = state.execute(
                "SELECT value FROM state WHERE key = 'global-pressure'"
            ).fetchone()
            assert deadline is not None and float(deadline[0]) == expected_until
    assert len(pilot.calls) == 1


@pytest.mark.parametrize("code", [429, 502])
def test_partial_denial_body_keeps_challenge_and_does_not_retry(pilot: Pilot, code: int) -> None:
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"<title>just a moment</title>"
            raise httpx.ReadError("synthetic stream interruption")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(code, headers={"Retry-After": "30"}, stream=InterruptedStream())

    result = pilot.fetch(handler)
    assert result.reason == "access-challenge"
    assert result.run is not None and result.run.status is RunStatus.BLOCKED
    assert len(pilot.calls) == 2


def test_invalid_redirect_location_does_not_hide_challenge(pilot: Pilot) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(302, headers={"Location": "https://[", "CF-Mitigated": "challenge"})

    result = pilot.fetch(handler)
    assert result.reason == "access-challenge"
    assert result.run is not None and result.run.status is RunStatus.BLOCKED
    assert len(pilot.calls) == 2


@pytest.mark.parametrize("encoding", [{}, {"Content-Encoding": "gzip"}])
def test_compressed_403_still_counts_toward_the_block_rule(
    pilot: Pilot, encoding: dict[str, str]
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(403, headers=encoding)

    for _ in range(3):
        assert pilot.worker(handler).fetch(pilot.source, pilot.profile).reason == "http-403"
        pilot.clock.sleep(60)
    assert pilot.fetch(ok).reason == "source-withdrawn"


@pytest.mark.parametrize("encoding", [{}, {"Content-Encoding": "gzip"}])
def test_compressed_429_honors_retry_after(pilot: Pilot, encoding: dict[str, str]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(429, headers={"Retry-After": "1200", **encoding})

    assert pilot.worker(handler).fetch(pilot.source, pilot.profile).reason == "http-429-deferred"
    pilot.clock.sleep(600)
    assert pilot.fetch(ok).reason == "not-due"
    assert len(pilot.calls) == 2


def _withdrawal(pilot: Pilot, withdrawal_id: str = "operator-stop") -> WithdrawalEntry:
    return WithdrawalEntry(
        schema_version=CONTRACT_VERSION,
        withdrawal_id=withdrawal_id,
        scope=WithdrawalScope.SOURCE,
        target_id=pilot.source.source_id,
        actor="Synthetic operator",
        withdrawn_at=datetime.fromtimestamp(pilot.clock.now(), UTC),
        reason="Synthetic stop request",
    )


def _revoke(pilot: Pilot) -> None:
    pilot.clock.sleep(1)
    rejected = SourceApproval.model_validate(
        {
            **pilot.approval.model_dump(exclude={"profile", "evidence_kind"}),
            "approval_id": "registry-revocation",
            "decision": "rejected",
            "decided_at": datetime.fromtimestamp(pilot.clock.now(), UTC),
        }
    )
    with open_operational_database(pilot.database).begin() as conn:
        insert_approval(conn, rejected, 9000001)


def test_caller_supplied_approval_and_withdrawn_flags_cannot_authorize(pilot: Pilot) -> None:
    worker = pilot.worker(ok)
    _revoke(pilot)
    # Exercise the old attack surface, including an otherwise valid stale approval.
    with pytest.raises(TypeError):
        worker.fetch(  # type: ignore[call-arg]
            pilot.source, pilot.profile, pilot.approval, pilot.terms, withdrawn=False
        )
    assert worker.fetch(pilot.source, pilot.profile).reason == "source-not-approved"
    assert not pilot.calls and not pilot.dialed


def test_registry_revocation_is_read_again_by_an_existing_worker(pilot: Pilot) -> None:
    worker = pilot.worker(ok)
    assert worker.fetch(pilot.source, pilot.profile).reason == "fetched"
    pilot.clock.sleep(60)
    _revoke(pilot)
    before = len(pilot.calls)
    assert worker.fetch(pilot.source, pilot.profile).reason == "source-not-approved"
    assert len(pilot.calls) == before


def test_approval_expiry_is_enforced_even_with_current_terms(pilot: Pilot) -> None:
    worker = pilot.worker(ok)
    pilot.clock.sleep(90 * ROBOTS_TTL)
    pilot.terms_ledger.record(
        pilot.terms.model_copy(
            update={
                "approval_id": "fresh-terms",
                "decided_at": datetime.fromtimestamp(pilot.clock.now(), UTC),
            }
        ),
        denylist=pilot.denylist,
    )
    assert worker.fetch(pilot.source, pilot.profile).reason == "source-not-approved"
    assert not pilot.calls


@pytest.mark.parametrize("store", ["database", "terms", "denylist"])
@pytest.mark.parametrize("failure", ["missing", "invalid", "unreadable"])
def test_missing_or_unreadable_authoritative_state_fails_closed(
    pilot: Pilot, store: str, failure: str
) -> None:
    worker = pilot.worker(ok)
    path = {
        "database": pilot.database,
        "terms": pilot.terms_ledger.path,
        "denylist": pilot.denylist.path,
    }[store]
    path.unlink()
    if failure == "invalid":
        path.write_bytes(b"invalid persisted state")
    elif failure == "unreadable":
        path.mkdir()  # reliably unreadable as a record, including when tests run as root
    result = worker.fetch(pilot.source, pilot.profile)
    assert result.reason == "policy-state-unavailable"
    assert result.run is None and result.artifact is None
    assert not pilot.calls and not pilot.dialed
    if failure == "missing":
        assert not path.exists()  # reads must never recreate missing authority


@pytest.mark.parametrize("missing", ["source", "profile", "approval", "terms"])
def test_missing_persisted_record_fails_closed(pilot: Pilot, missing: str) -> None:
    worker = pilot.worker(ok)
    if missing == "terms":
        pilot.terms_ledger.path.unlink()
        pilot.terms_ledger.initialize()
    else:
        pilot.database.unlink()
        engine = open_operational_database(pilot.database)
        metadata.create_all(engine)
        with engine.begin() as conn:
            if missing != "source":
                insert_source(conn, pilot.source)
            if missing not in {"source", "profile"}:
                insert_profile(conn, pilot.profile)
        engine.dispose()
    assert worker.fetch(pilot.source, pilot.profile).run is None
    assert not pilot.calls and not pilot.dialed


@pytest.mark.parametrize("independent", [True, False])
def test_withdrawal_is_read_from_each_authority_independently(
    pilot: Pilot, independent: bool
) -> None:
    worker = pilot.worker(ok)
    assert worker.fetch(pilot.source, pilot.profile).reason == "fetched"
    pilot.clock.sleep(60)
    entry = _withdrawal(pilot)
    if independent:
        pilot.denylist.withdraw(entry)
    else:
        # Legacy/database-only withdrawals must stop acquisition too.
        with open_operational_database(pilot.database).begin() as conn:
            conn.execute(
                withdrawal_entries.insert(), entry.model_dump(exclude={"lifted_at", "lifted_by"})
            )
        assert not pilot.denylist.active_targets(entry.withdrawn_at)
    before = len(pilot.calls)
    assert worker.fetch(pilot.source, pilot.profile).reason == "source-withdrawn"
    assert len(pilot.calls) == before


def test_revocation_during_spacing_wait_stops_before_dispatch(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = pilot.worker(ok)

    def sleep(seconds: float) -> None:
        pilot.clock.sleep(seconds)
        _revoke(pilot)

    monkeypatch.setattr(worker, "_sleep", sleep)
    assert worker.fetch(pilot.source, pilot.profile).reason == "source-not-approved"
    assert [request.url.path for _, request in pilot.calls] == ["/robots.txt"]


def test_challenge_denylist_survives_crash_before_database_block(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = pilot.worker(lambda _: httpx.Response(403, headers={"CF-Mitigated": "challenge"}))
    original = worker._set

    def crash(db: sqlite3.Connection, key: str, value: str) -> None:
        if key.startswith("paused:"):
            assert ("source", pilot.source.source_id) in pilot.denylist.active_targets(
                datetime.fromtimestamp(pilot.clock.now(), UTC)
            )
            raise RuntimeError("crash between independent suppression and block record")
        original(db, key, value)

    monkeypatch.setattr(worker, "_set", crash)
    with pytest.raises(RuntimeError, match="crash between"):
        worker.fetch(pilot.source, pilot.profile)
    with worker._state() as db:
        assert not db.execute("SELECT * FROM events").fetchall()
        assert worker._get(db, "paused:fixture") is None
    before = pilot.denylist.path.read_bytes()
    assert pilot.fetch(ok).reason == "source-withdrawn"
    assert pilot.denylist.path.read_bytes() == before
    assert len(pilot.calls) == 1


@pytest.mark.parametrize("reason", ["challenge", "robots", "403", "429", "failures"])
def test_every_block_rule_updates_independent_denylist(pilot: Pilot, reason: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if reason == "challenge":
            return httpx.Response(403, headers={"CF-Mitigated": "challenge"})
        if reason == "robots":
            return robots(b"User-agent: *\nDisallow: /rates\n")
        if req.url.path == "/robots.txt":
            return robots()
        return httpx.Response(503 if reason == "failures" else int(reason))

    runs = 5 if reason == "failures" else 3 if reason in {"403", "429"} else 1
    for index in range(runs):
        result = pilot.fetch(handler)
        assert result.run is not None
        targets = pilot.denylist.active_targets(datetime.fromtimestamp(pilot.clock.now(), UTC))
        assert (("source", pilot.source.source_id) in targets) == (index == runs - 1)
        pilot.clock.sleep(180)
    assert result.run is not None and result.run.status is RunStatus.BLOCKED
    assert pilot.fetch(ok).reason == "source-withdrawn"


def test_failed_denylist_write_prevents_database_block(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = pilot.worker(lambda _: httpx.Response(403, headers={"CF-Mitigated": "challenge"}))

    def fail(entry: WithdrawalEntry) -> None:
        raise OSError("synthetic ledger storage failure")

    monkeypatch.setattr(pilot.denylist, "withdraw", fail)
    assert worker.fetch(pilot.source, pilot.profile).reason == "policy-or-state-unavailable"
    with worker._state() as db:
        assert worker._get(db, "paused:fixture") is None
        assert not db.execute("SELECT * FROM events").fetchall()


def test_terms_prohibition_suppresses_before_record_and_retries_idempotently(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = pilot.terms.model_copy(
        update={"approval_id": "terms-prohibition", "decision": ReviewVerdict.REJECTED}
    )
    original = pilot.terms_ledger.read

    def crash() -> tuple[TermsReview, ...]:
        assert ("source", pilot.source.source_id) in pilot.denylist.active_targets(
            review.decided_at
        )
        raise RuntimeError("crash before terms record")

    monkeypatch.setattr(pilot.terms_ledger, "read", crash)
    with pytest.raises(RuntimeError, match="crash before"):
        pilot.terms_ledger.record(review, denylist=pilot.denylist)
    monkeypatch.setattr(pilot.terms_ledger, "read", original)
    pilot.terms_ledger.record(review, denylist=pilot.denylist)
    before = pilot.denylist.path.read_bytes(), pilot.terms_ledger.path.read_bytes()
    pilot.terms_ledger.record(review, denylist=pilot.denylist)
    assert (pilot.denylist.path.read_bytes(), pilot.terms_ledger.path.read_bytes()) == before
    assert pilot.fetch(ok).reason == "source-withdrawn"


def _with_automatic_ledger(pilot: Pilot, root: Path) -> Denylist:
    automatic = Denylist(root / "automatic" / "denylist.json")
    automatic.initialize()
    pilot.registry = FetchRegistry(
        pilot.database, pilot.terms_ledger, pilot.denylist, automatic=automatic
    )
    return automatic


def test_a_block_is_recorded_in_the_workers_own_ledger_not_the_operators(
    pilot: Pilot, tmp_path: Path
) -> None:
    """#118: the worker never writes the operator ledger; its blocks go to the automatic one."""
    automatic = _with_automatic_ledger(pilot, tmp_path)
    operator_before = pilot.denylist.path.read_bytes()
    result = pilot.fetch(lambda _: httpx.Response(403, headers={"CF-Mitigated": "challenge"}))
    assert result.run is not None
    now = datetime.fromtimestamp(pilot.clock.now(), UTC)
    assert ("source", pilot.source.source_id) in automatic.active_targets(now)
    assert pilot.denylist.path.read_bytes() == operator_before  # the operator ledger is untouched


def test_an_automatic_block_stops_the_next_fetch_for_every_worker(
    pilot: Pilot, tmp_path: Path
) -> None:
    _with_automatic_ledger(pilot, tmp_path)
    pilot.fetch(lambda _: httpx.Response(403, headers={"CF-Mitigated": "challenge"}))
    calls = len(pilot.calls)
    assert pilot.fetch(ok).reason == "source-withdrawn"
    assert len(pilot.calls) == calls  # no request was made


def test_an_automatic_entry_alone_withdraws_the_source_at_authorization(
    pilot: Pilot, tmp_path: Path
) -> None:
    automatic = _with_automatic_ledger(pilot, tmp_path)
    now = datetime.fromtimestamp(pilot.clock.now(), UTC)
    automatic.withdraw(
        WithdrawalEntry(
            schema_version=CONTRACT_VERSION,
            withdrawal_id="auto-manual",
            scope=WithdrawalScope.SOURCE,
            target_id=pilot.source.source_id,
            actor="fetch-worker",
            withdrawn_at=now - timedelta(seconds=1),
            reason="test",
        )
    )
    assert pilot.fetch(ok).reason == "source-withdrawn"
    assert not pilot.calls


def test_an_unreadable_automatic_ledger_stops_collection_fail_closed(
    pilot: Pilot, tmp_path: Path
) -> None:
    automatic = _with_automatic_ledger(pilot, tmp_path)
    automatic.path.write_text("{not json")
    assert pilot.fetch(ok).reason == "policy-state-unavailable"
    assert not pilot.calls


def _uptime(pilot: Pilot, start: float) -> Callable[[], float]:
    """A monotonic clock that starts at ``start`` and advances with the simulated time."""
    origin = pilot.clock.now()
    return lambda: start + (pilot.clock.now() - origin)


def _fetched(result: FetchResult) -> bool:
    return result.reason == "fetched" and result.run is not None and result.artifact is not None


def test_monotonic_state_from_an_earlier_boot_does_not_block_collection(pilot: Pilot) -> None:
    """AUD7-11(g): after a reboot the monotonic clock restarts near zero. A tick persisted by a
    long-running previous boot must not look like a deadline days in the future."""
    before = pilot.worker(ok, monotonic=_uptime(pilot, 9_000_000.0), boot_id=lambda: "boot-1")
    assert _fetched(before.fetch(pilot.source, pilot.profile))
    pilot.clock.value += 3600  # an hour of wall-clock time passes, across the reboot
    after = pilot.worker(ok, monotonic=_uptime(pilot, 50.0), boot_id=lambda: "boot-2")
    result = after.fetch(pilot.source, pilot.profile)
    assert _fetched(result), result.reason
    with after._state() as db:
        assert after._get(db, "boot-id") == "boot-2"


def test_monotonic_state_is_still_enforced_within_one_boot(pilot: Pilot) -> None:
    first = pilot.worker(ok, monotonic=_uptime(pilot, 1_000.0), boot_id=lambda: "boot-1")
    assert _fetched(first.fetch(pilot.source, pilot.profile))
    # Same boot, same instant: the persisted ticks (and wall-clock deadlines) still apply.
    second = pilot.worker(ok, monotonic=_uptime(pilot, 1_000.0), boot_id=lambda: "boot-1")
    assert second.fetch(pilot.source, pilot.profile).reason == "not-due"


def test_wall_clock_politeness_survives_a_reboot(pilot: Pilot) -> None:
    """Only the monotonic ticks are discarded: spacing by wall clock still holds after a restart."""
    before = pilot.worker(ok, monotonic=_uptime(pilot, 9_000_000.0), boot_id=lambda: "boot-1")
    assert _fetched(before.fetch(pilot.source, pilot.profile))
    after = pilot.worker(ok, monotonic=_uptime(pilot, 50.0), boot_id=lambda: "boot-2")
    assert after.fetch(pilot.source, pilot.profile).reason == "not-due"  # no wall time elapsed


def test_a_platform_without_a_boot_id_trusts_ticks_only_within_a_process(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cu_rates import fetch_worker

    monkeypatch.setattr(fetch_worker, "_BOOT_ID_PATH", tmp_path / "no-boot-id")  # absent
    monkeypatch.setattr(fetch_worker, "_PROCESS_TOKEN", "process-a")
    a = pilot.worker(ok, monotonic=_uptime(pilot, 9_000_000.0))  # default boot_id: this process
    assert _fetched(a.fetch(pilot.source, pilot.profile))
    pilot.clock.value += 3600
    monkeypatch.setattr(fetch_worker, "_PROCESS_TOKEN", "process-b")  # a new process, later
    b = pilot.worker(ok, monotonic=_uptime(pilot, 50.0))
    assert _fetched(b.fetch(pilot.source, pilot.profile))


def test_the_boot_identity_is_the_linux_boot_id_when_the_platform_has_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cu_rates import fetch_worker

    present = tmp_path / "boot_id"
    present.write_text("3f2c1b9e-0000-4000-8000-123456789abc\n", encoding="ascii")
    monkeypatch.setattr(fetch_worker, "_BOOT_ID_PATH", present)
    assert fetch_worker._read_boot_id() == "3f2c1b9e-0000-4000-8000-123456789abc"
    present.write_text("\n", encoding="ascii")  # empty: fall back rather than trust nothing
    assert fetch_worker._read_boot_id() == fetch_worker._PROCESS_TOKEN
    monkeypatch.setattr(fetch_worker, "_BOOT_ID_PATH", tmp_path / "missing")
    assert fetch_worker._read_boot_id() == fetch_worker._PROCESS_TOKEN

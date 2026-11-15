"""Offline T032 tests. The only HTTP transport is synthetic."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import EvidenceKind, FetchMethod, ReviewVerdict, RunStatus
from cu_rates.contracts.sources import FetchProfile, RateSource, SourceApproval
from cu_rates.fetch_worker import ROBOTS_TTL, USER_AGENT, FetchResult, FetchWorker, TermsReview

URL = "https://credit-union.test/rates"
BODY = b"<html>Synthetic fixture</html>"


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
        self.store = ArtifactStore(root / "evidence")
        stamp = datetime.fromtimestamp(self.clock.value, UTC)
        self.source = RateSource(
            schema_version=CONTRACT_VERSION,
            source_id="fixture",
            url=URL,
            evidence_kind=EvidenceKind.HTML,
        )
        self.approval = SourceApproval(
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
        )
        self.terms = TermsReview.model_validate(
            {
                **self.approval.model_dump(mode="json"),
                "terms_url": "https://credit-union.test/terms",
                "terms_sha256": "a" * 64,
            }
        )
        self.profile = FetchProfile(
            schema_version=CONTRACT_VERSION,
            profile_id="fixture.http",
            source_id="fixture",
            method=FetchMethod.HTTP_GET,
            min_interval_seconds=60,
            timeout_seconds=10,
        )

    def worker(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> FetchWorker:
        def send(request: httpx.Request) -> httpx.Response:
            self.calls.append((self.clock.now(), request))
            return handler(request)

        return FetchWorker(
            self.path / "state",
            self.store,
            lambda: httpx.MockTransport(send),
            git_sha="0" * 40,
            clock=self.clock.now,
            sleep=self.clock.sleep,
        )

    def fetch(self, handler: Callable[[httpx.Request], httpx.Response]) -> FetchResult:
        return self.worker(handler).fetch(self.source, self.profile, self.approval, self.terms)


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
    result = pilot.worker(ok).fetch(
        pilot.source,
        pilot.profile,
        pilot.approval,
        pilot.terms,
        withdrawn=kind == "withdrawn",
    )
    assert result.artifact is None and result.run is None
    assert not pilot.calls


@pytest.mark.parametrize("code", [301, 401, 403, 429, 503])
def test_inaccessible_robots_stops_before_source(pilot: Pilot, code: int) -> None:
    assert pilot.fetch(lambda _: httpx.Response(code)).reason == "robots-unavailable"
    assert [req.url.path for _, req in pilot.calls] == ["/robots.txt"]


def test_disallow_and_allow_overlap_fails_closed(pilot: Pilot) -> None:
    result = pilot.fetch(
        lambda _: robots(
            b"User-agent: *\nAllow: /rates\nDisallow: /rates\n",
        )
    )
    assert result.reason == "robots-disallowed" and result.artifact is None
    assert len(pilot.calls) == 1


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
    assert pilot.fetch(ok).reason == "source-paused"
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
    assert result.run is None and result.artifact is None
    assert list(pilot.store.observations()) == [first.artifact]


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

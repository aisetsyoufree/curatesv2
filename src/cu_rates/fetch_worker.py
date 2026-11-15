"""Private, serial HTTP collector. Explicit transport; no scheduler or publication."""

import fcntl
import ipaddress
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from cu_rates.artifact_store import ArtifactStore, ArtifactStoreError
from cu_rates.contracts.base import CONTRACT_VERSION, HttpUrlText, Sha256Hex
from cu_rates.contracts.enums import EvidenceKind, FetchMethod, RunStatus
from cu_rates.contracts.evidence import RawArtifact
from cu_rates.contracts.sources import (
    ConditionalValidator,
    FetchProfile,
    FetchRun,
    NotModifiedObservation,
    RateSource,
    SourceApproval,
)
from cu_rates.fetch_robots import PRODUCT, RobotsPolicy, parse_robots

USER_AGENT = f"{PRODUCT}/2 (+https://github.com/aisetsyoufree/curatesv2)"
ROBOTS_TTL = 24 * 60 * 60
TERMS_TTL = 90 * ROBOTS_TTL
MAX_RETRY_DELAY = 15 * 60
MAX_BODY = 8 * 1024 * 1024
MAX_ROBOTS = 512 * 1024
_CHALLENGE_PREFIX_BYTES = 64_000
_MEDIA = {
    EvidenceKind.HTML: "text/html",
    EvidenceKind.PDF: "application/pdf",
    EvidenceKind.JSON: "application/json",
}


class FetchApproval(SourceApproval):
    """Human-approved source, media kind, and exact request profile."""

    profile: FetchProfile
    evidence_kind: EvidenceKind


class TermsReview(SourceApproval):
    """A separate human terms decision, directly bound to the source."""

    terms_url: HttpUrlText
    terms_sha256: Sha256Hex


@dataclass(frozen=True)
class Reobservation:
    """Real HTTP 304 with a pointer to the still-valid stored 200 response.

    Not a replacement RawArtifact or a publication-authorizing FetchRun.
    """

    run_id: str
    source_id: str
    profile_id: str
    git_sha: str
    started_at: datetime
    observed_at: datetime
    original: RawArtifact
    validator: ConditionalValidator
    http_status: int = 304

    def record(self) -> NotModifiedObservation:
        """The contract form publication verifies against the original run and artifact."""
        return NotModifiedObservation(
            schema_version=CONTRACT_VERSION,
            run_id=self.run_id,
            source_id=self.source_id,
            profile_id=self.profile_id,
            git_sha=self.git_sha,
            started_at=self.started_at,
            observed_at=self.observed_at,
            original_run_id=self.original.fetch_run_id,
            original_artifact_id=self.original.artifact_id,
            validator=self.validator,
        )


@dataclass(frozen=True)
class FetchResult:
    reason: str
    run: FetchRun | None = None
    artifact: RawArtifact | None = None
    reobserved: Reobservation | None = None


@dataclass(frozen=True)
class _Response:
    status: int
    headers: httpx.Headers
    body: bytes


class _Stopped(Exception):
    pass


def _public_url(text: str) -> httpx.URL:
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL as exc:
        raise _Stopped("unsupported-url") from exc
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.userinfo
        or url.query
        or url.fragment
        or str(url) != text
        or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", url.host)
        or url.host.endswith((".local", ".internal", ".localhost"))
        or "%" in url.raw_path.decode("ascii")
        or any(piece in (".", "..") for piece in url.path.split("/"))
    ):
        raise _Stopped("unsupported-url")
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        return url
    raise _Stopped("unsupported-url")


def _validator(etag: str | None, last_modified: str | None) -> ConditionalValidator | None:
    """The validator a later conditional request uses: the ETag, else Last-Modified."""
    try:
        if etag:
            return ConditionalValidator(kind="etag", value=etag)
        if last_modified:
            return ConditionalValidator(kind="last_modified", value=last_modified)
    except ValidationError:
        pass
    return None


def _retry_after(headers: httpx.Headers, now: float) -> float:
    value = headers.get("retry-after", "").strip()
    if re.fullmatch(r"[0-9]+", value):
        # Refuse to retry if the field is unreasonably large or unrepresentable.
        return float(value) if len(value) <= 12 else float("inf")
    if value:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(0, date.timestamp() - now)
        except (ValueError, OverflowError, OSError):
            pass
    return 0


def _is_challenge(response: _Response) -> bool:
    head = response.body[:_CHALLENGE_PREFIX_BYTES].lower()
    return (
        bool(response.headers.get("www-authenticate"))
        or "challenge" in response.headers.get("cf-mitigated", "").lower()
        or any(
            marker in head
            for marker in (
                b"/cdn-cgi/challenge-platform",
                b"<title>just a moment",
                b"g-recaptcha",
                b"gstatic.com/recaptcha/",
                b"hcaptcha.com/1/api.js",
                b'class="h-captcha',
                b"captcha-delivery.com/",
                b"_pxcaptcha",
                b"incapsula incident id",
                b"verify you are human",
                b"verify that you are a human",
            )
        )
    )


class FetchWorker:
    """Caller supplies a transport factory; tests use MockTransport exclusively.

    The configured parent is trusted and the state directory is owner-only.
    No network transport is selected by this package or wired into the app.
    """

    def __init__(
        self,
        root: Path,
        store: ArtifactStore,
        transport: Callable[[], httpx.BaseTransport],
        *,
        git_sha: str,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
            raise ValueError("invalid Git revision")
        root.mkdir(mode=0o700, exist_ok=True)
        st = root.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("fetch state directory must be a private real directory")
        self._root, self._store, self._transport = root, store, transport
        self._git_sha, self._clock, self._sleep = git_sha, clock, sleep
        self._monotonic = monotonic

    @contextmanager
    def _state(self) -> Iterator[sqlite3.Connection]:
        # Separate lock from SQLite commits. Reserve each request durably before
        # performing I/O; interruption cannot roll back cross-restart spacing.
        fd = os.open(self._root / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            db_path = self._root / "state.sqlite3"
            db_fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                for entry in (os.fstat(fd), os.fstat(db_fd)):
                    if (
                        not stat.S_ISREG(entry.st_mode)
                        or entry.st_uid != os.getuid()
                        or entry.st_mode & 0o077
                    ):
                        raise _Stopped("unsafe-state-file")
            finally:
                os.close(db_fd)
            db = sqlite3.connect(db_path, isolation_level=None)
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, value TEXT)")
                yield db
            finally:
                db.close()
        finally:
            os.close(fd)

    @staticmethod
    def _get(db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _set(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))

    def _wait(self, until: float, tick_until: float) -> None:
        remaining = max(until - self._clock(), tick_until - self._monotonic())
        if remaining > MAX_RETRY_DELAY:
            raise _Stopped("deferred")
        if remaining > 0:
            self._sleep(remaining)
        if self._clock() < until or self._monotonic() < tick_until:
            raise _Stopped("clock-not-ready")

    def _send(
        self,
        db: sqlite3.Connection,
        url: httpx.URL,
        headers: dict[str, str],
        timeout: int,
        *,
        limit: int,
        spacing: int = 5,
    ) -> _Response:
        origin = f"https://{url.host}"
        self._wait(
            max(
                float(self._get(db, "next-global") or 0),
                float(self._get(db, f"started:{origin}") or 0) + spacing,
                float(self._get(db, f"defer:{origin}") or 0),
                float(self._get(db, "global-pressure") or 0),
            ),
            max(
                float(self._get(db, "next-tick") or 0),
                float(self._get(db, f"tick:{origin}") or 0) + spacing,
                float(self._get(db, f"defer-tick:{origin}") or 0),
                float(self._get(db, "pressure-tick") or 0),
            ),
        )
        started = self._clock()
        tick = self._monotonic()
        self._set(db, "next-global", str(started + 5))
        self._set(db, f"started:{origin}", str(started))
        self._set(db, "next-tick", str(tick + 5))
        self._set(db, f"tick:{origin}", str(tick))
        with (
            httpx.Client(
                transport=self._transport(),
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **headers},
            ) as response,
        ):
            # A server can signal a challenge even when its body uses an
            # unsupported encoding. Preserve the stop signal without decoding.
            headers_only = _Response(response.status_code, response.headers, b"")
            if _is_challenge(headers_only):
                return headers_only
            if response.status_code != 200 and response.status_code not in (401, 403, 429, 503):
                return headers_only
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise _Stopped("unsupported-encoding")
            body = bytearray()
            chunks: Iterator[bytes] = (
                iter((response.content,)) if response.is_stream_consumed else response.iter_raw()
            )
            for chunk in chunks:
                if self._monotonic() - tick > timeout:
                    raise _Stopped("response-time-limit")
                if response.status_code != 200:
                    body.extend(chunk[: _CHALLENGE_PREFIX_BYTES - len(body)])
                    if len(body) >= _CHALLENGE_PREFIX_BYTES:
                        break
                    continue
                if len(body) + len(chunk) > limit:
                    raise _Stopped("body-too-large")
                body.extend(chunk)
            return _Response(response.status_code, response.headers, bytes(body))

    def _robots(self, db: sqlite3.Connection, url: httpx.URL, timeout: int) -> RobotsPolicy:
        origin = f"https://{url.host}"
        key = f"robots:{origin}"
        cached = self._get(db, key)
        spacing = 5
        if cached:
            at, tick, body = json.loads(cached)
            policy = parse_robots(bytes.fromhex(body))
            spacing = max(spacing, policy.delay)
            if 0 <= self._clock() - at < ROBOTS_TTL and 0 <= self._monotonic() - tick < ROBOTS_TTL:
                return policy
        try:
            response = self._send(
                db,
                httpx.URL(f"{origin}/robots.txt"),
                {},
                timeout,
                limit=MAX_ROBOTS,
                spacing=spacing,
            )
        except httpx.HTTPError as exc:
            raise _Stopped("robots-unavailable") from exc
        if _is_challenge(response):
            raise _Stopped("access-challenge")
        if response.status in (401, 403, 429):
            if response.status == 429:
                self._defer(db, origin, response.headers, minimum=30, pressure=True)
            raise _Stopped("robots-unavailable")
        if 400 <= response.status < 500:
            body = b""
        elif (
            response.status == 200
            and response.headers.get("content-type", "").split(";")[0].strip() == "text/plain"
        ):
            body = response.body
        else:
            raise _Stopped("robots-unavailable")
        try:
            policy = parse_robots(body)
        except ValueError as exc:
            raise _Stopped("robots-unsupported") from exc
        self._set(db, key, json.dumps((self._clock(), self._monotonic(), body.hex())))
        return policy

    def _defer(
        self,
        db: sqlite3.Connection,
        origin: str,
        headers: httpx.Headers,
        *,
        minimum: float,
        pressure: bool = False,
    ) -> float:
        now = self._clock()
        delay = max(minimum, _retry_after(headers, now))
        self._set(db, f"defer:{origin}", str(now + delay))
        tick = self._monotonic()
        self._set(db, f"defer-tick:{origin}", str(tick + delay))
        # Deliberately stricter than domain-wide pressure. An HTTP Retry-After
        # can slow the entire serial collector, including sibling hostnames.
        if pressure or "retry-after" in headers:
            self._set(db, "global-pressure", str(now + delay))
            self._set(db, "pressure-tick", str(tick + delay))
        return delay

    def fetch(
        self,
        source: RateSource,
        profile: FetchProfile,
        approval: FetchApproval,
        terms: TermsReview,
        *,
        withdrawn: bool = False,
    ) -> FetchResult:
        now = datetime.fromtimestamp(self._clock(), UTC)
        for decision in (approval, terms):
            if (
                not decision.approved
                or decision.source_id != source.source_id
                or decision.source_url != source.url
                or decision.decided_at > now
            ):
                return FetchResult("source-not-approved")
        if (now - terms.decided_at).total_seconds() >= TERMS_TTL:
            return FetchResult("terms-review-expired")
        if approval.approval_id == terms.approval_id:
            return FetchResult("terms-review-not-independent-record")
        if (
            withdrawn
            or profile.source_id != source.source_id
            or profile.method is not FetchMethod.HTTP_GET
            or source.evidence_kind not in _MEDIA
            or approval.profile != profile
            or approval.evidence_kind is not source.evidence_kind
        ):
            return FetchResult("withdrawn-or-unsupported-profile")
        try:
            url = _public_url(source.url)
            _public_url(terms.terms_url)
            with self._state() as db:
                return self._fetch(db, url, source, profile)
        except _Stopped as exc:
            return FetchResult(str(exc))
        except BlockingIOError:
            return FetchResult("worker-busy")

    def _fetch(
        self,
        db: sqlite3.Connection,
        url: httpx.URL,
        source: RateSource,
        profile: FetchProfile,
    ) -> FetchResult:
        key = source.source_id
        if self._get(db, f"paused:{key}"):
            return FetchResult("source-paused")
        origin = f"https://{url.host}"
        now = self._clock()
        if now < max(
            float(self._get(db, f"scheduled:{key}") or 0),
            float(self._get(db, f"defer:{origin}") or 0),
            float(self._get(db, "global-pressure") or 0),
        ) or self._monotonic() < max(
            float(self._get(db, f"scheduled-tick:{key}") or 0),
            float(self._get(db, f"defer-tick:{origin}") or 0),
            float(self._get(db, "pressure-tick") or 0),
        ):
            return FetchResult("not-due")
        run_id = uuid4().hex
        self._set(db, f"scheduled:{key}", str(now + max(5, profile.min_interval_seconds)))
        self._set(
            db,
            f"scheduled-tick:{key}",
            str(self._monotonic() + max(5, profile.min_interval_seconds)),
        )
        status, reason = RunStatus.BLOCKED, "robots-disallowed"
        artifact = None
        observed = None
        validator = None
        terminal = 0
        try:
            robots = self._robots(db, url, profile.timeout_seconds)
            if robots.permits(url.path):
                try:
                    artifact, observed, validator = self._retrieve(
                        db,
                        source,
                        profile,
                        url,
                        run_id,
                        max(5, robots.delay),
                    )
                    status, reason = RunStatus.SUCCEEDED, "fetched"
                except _Stopped as exc:
                    reason = str(exc)
                    status = RunStatus.FAILED
                    if reason in {"http-403", "http-429", "http-429-deferred"}:
                        terminal = 403 if reason == "http-403" else 429
                    if reason == "access-challenge":
                        status = RunStatus.BLOCKED
                except (httpx.HTTPError, ArtifactStoreError, OSError):
                    status, reason = RunStatus.FAILED, "transport-or-storage-failure"
        except _Stopped as exc:
            reason = str(exc)

        succeeded = status is RunStatus.SUCCEEDED
        failures = 0 if succeeded else 1 + int(self._get(db, f"failures:{key}") or 0)
        barred = 1 + int(self._get(db, f"barred:{key}") or 0) if terminal in (403, 429) else 0
        self._set(db, f"failures:{key}", str(failures))
        self._set(db, f"barred:{key}", str(barred))
        if reason in {"access-challenge", "robots-disallowed"} or barred >= 3 or failures >= 5:
            self._set(db, f"paused:{key}", reason)
        end = datetime.fromtimestamp(max(now, self._clock()), UTC)
        if observed is not None and validator is not None:
            db.execute(
                "INSERT INTO events VALUES (?, ?)",
                (
                    run_id,
                    json.dumps(
                        {
                            "http_status": 304,
                            "source_id": key,
                            "profile_id": profile.profile_id,
                            "git_sha": self._git_sha,
                            "started_at": datetime.fromtimestamp(now, UTC).isoformat(),
                            "observed_at": end.isoformat(),
                            "original": observed.model_dump(mode="json"),
                            "validator": validator.model_dump(mode="json") if validator else None,
                        }
                    ),
                ),
            )
            return FetchResult(
                "reobserved",
                reobserved=Reobservation(
                    run_id,
                    key,
                    profile.profile_id,
                    self._git_sha,
                    datetime.fromtimestamp(now, UTC),
                    end,
                    observed,
                    validator,
                ),
            )
        run = FetchRun(
            schema_version=CONTRACT_VERSION,
            run_id=run_id,
            source_id=key,
            profile_id=profile.profile_id,
            status=status,
            started_at=datetime.fromtimestamp(now, UTC),
            finished_at=end,
            git_sha=self._git_sha,
            artifact_ids=(artifact.artifact_id,) if artifact else (),
            error=None if succeeded else reason,
            conditional_validator=validator if succeeded else None,
        )
        db.execute("INSERT INTO events VALUES (?, ?)", (run_id, run.model_dump_json()))
        return FetchResult(reason, run, artifact)

    def _retrieve(
        self,
        db: sqlite3.Connection,
        source: RateSource,
        profile: FetchProfile,
        url: httpx.URL,
        run_id: str,
        spacing: int,
    ) -> tuple[RawArtifact | None, RawArtifact | None, ConditionalValidator | None]:
        key = f"cache:{source.source_id}"
        cached = self._get(db, key)
        prior: RawArtifact | None = None
        validator: ConditionalValidator | None = None
        headers: dict[str, str] = {}
        if cached:
            prior_json, etag, last_modified = json.loads(cached)
            try:
                record = RawArtifact.model_validate(prior_json)
                if record.source_url == source.url and record.http_status == 200:
                    prior = record
                    if etag:
                        headers["If-None-Match"] = etag
                    elif last_modified:
                        headers["If-Modified-Since"] = last_modified
                    validator = _validator(etag, last_modified)
            except ValidationError as exc:
                raise _Stopped("cache-invalid") from exc
        origin = f"https://{url.host}"
        for attempt in range(3):
            try:
                response = self._send(
                    db,
                    url,
                    headers,
                    profile.timeout_seconds,
                    limit=MAX_BODY,
                    spacing=spacing,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt == 2:
                    raise _Stopped("transport-failure") from exc
                self._defer(db, origin, httpx.Headers(), minimum=30 * 2**attempt)
                continue
            if _is_challenge(response):
                raise _Stopped("access-challenge")
            code = response.status
            if code == 304:
                echoed = response.headers.get("etag")
                if (
                    prior is None
                    or validator is None
                    or not headers
                    or (echoed and echoed != headers.get("If-None-Match"))
                ):
                    raise _Stopped("unexpected-304")
                try:
                    if self._store.get_observation(prior.fetch_run_id, source.url) != prior:
                        raise _Stopped("cache-invalid")
                    self._store.read(prior)
                    row = db.execute(
                        "SELECT value FROM events WHERE id = ?",
                        (prior.fetch_run_id,),
                    ).fetchone()
                    if not row:
                        raise _Stopped("cache-invalid")
                    original_run = FetchRun.model_validate_json(row[0])
                    if (
                        original_run.run_id != prior.fetch_run_id
                        or original_run.source_id != source.source_id
                        or original_run.status is not RunStatus.SUCCEEDED
                        or prior.artifact_id not in original_run.artifact_ids
                    ):
                        raise _Stopped("cache-invalid")
                except (ArtifactStoreError, OSError, ValidationError) as exc:
                    raise _Stopped("cache-invalid") from exc
                return None, prior, validator
            if code == 200:
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media != _MEDIA[source.evidence_kind] or not response.body:
                    raise _Stopped("unexpected-content")
                record = self._store.put(
                    response.body,
                    media_type=media,
                    source_url=source.url,
                    fetched_at=datetime.fromtimestamp(self._clock(), UTC),
                    http_status=200,
                    fetch_run_id=run_id,
                )
                self._set(
                    db,
                    key,
                    json.dumps(
                        (
                            record.model_dump(mode="json"),
                            response.headers.get("etag"),
                            response.headers.get("last-modified"),
                        )
                    ),
                )
                return (
                    record,
                    None,
                    _validator(response.headers.get("etag"), response.headers.get("last-modified")),
                )
            if code in (408, 429) or 500 <= code < 600:
                delay = self._defer(
                    db, origin, response.headers, minimum=30 * 2**attempt, pressure=code == 429
                )
                if delay > MAX_RETRY_DELAY:
                    raise _Stopped(f"http-{code}-deferred")
                if attempt < 2:
                    continue
            raise _Stopped(f"http-{code}")
        raise AssertionError("retry loop exhausted without result")

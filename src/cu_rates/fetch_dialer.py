"""Gateway-owned pinned dialer: dials only addresses the gateway already validated.

The gateway resolves a host once and validates every answer. This transport then connects
to one of those addresses and never resolves the name itself, while TLS still verifies the
certificate against the original hostname (SNI and hostname check). It is HTTPS-only, port
443-only, never follows redirects, never reads proxy settings, and uses one connection per
request. Nothing here runs at import time or by default; a caller must build a transport.
"""

import contextlib
import ipaddress
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, final

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
HTTPS_PORT = 443

_DENIED_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),  # This network (RFC 791)
    ipaddress.IPv4Network("10.0.0.0/8"),  # Private-use (RFC 1918)
    ipaddress.IPv4Network("100.64.0.0/10"),  # Shared address space / CGNAT (RFC 6598)
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback (RFC 1122)
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local (RFC 3927)
    ipaddress.IPv4Network("172.16.0.0/12"),  # Private-use (RFC 1918)
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments (RFC 6890)
    ipaddress.IPv4Network("192.0.2.0/24"),  # Documentation TEST-NET-1 (RFC 5737)
    ipaddress.IPv4Network("192.88.99.0/24"),  # 6to4 relay anycast (RFC 7526)
    ipaddress.IPv4Network("192.168.0.0/16"),  # Private-use (RFC 1918)
    ipaddress.IPv4Network("198.18.0.0/15"),  # Benchmarking (RFC 2544)
    ipaddress.IPv4Network("198.51.100.0/24"),  # Documentation TEST-NET-2 (RFC 5737)
    ipaddress.IPv4Network("203.0.113.0/24"),  # Documentation TEST-NET-3 (RFC 5737)
    ipaddress.IPv4Network("224.0.0.0/4"),  # Multicast (RFC 5771)
    ipaddress.IPv4Network("240.0.0.0/4"),  # Reserved / Class E (RFC 1112)
    ipaddress.IPv4Network("255.255.255.255/32"),  # Limited broadcast (RFC 8190)
)

_DENIED_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::/128"),  # Unspecified (RFC 4291)
    ipaddress.IPv6Network("::1/128"),  # Loopback (RFC 4291)
    ipaddress.IPv6Network("100::/64"),  # Discard-only prefix (RFC 6666)
    ipaddress.IPv6Network("2001::/23"),  # IETF protocol assignments, including Teredo
    ipaddress.IPv6Network("2001:db8::/32"),  # Documentation (RFC 3849)
    ipaddress.IPv6Network("2002::/16"),  # 6to4 (RFC 3056)
    ipaddress.IPv6Network("3fff::/20"),  # Documentation (RFC 9637)
    ipaddress.IPv6Network("fc00::/7"),  # Unique local / ULA (RFC 4193)
    ipaddress.IPv6Network("fe80::/10"),  # Link-local unicast (RFC 4291)
    ipaddress.IPv6Network("fec0::/10"),  # Site-local (RFC 3879, deprecated)
    ipaddress.IPv6Network("ff00::/8"),  # Multicast (RFC 4291)
)

_IPV6_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")


def globally_routable(address: IPAddress) -> bool:
    """Accept public unicast only; reject transitions that can change the destination."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return False
        if address.ipv4_mapped is not None:
            return globally_routable(address.ipv4_mapped)
    if not address.is_global or address.is_reserved or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in net for net in _DENIED_IPV4_NETWORKS)
    return address in _IPV6_GLOBAL_UNICAST and not any(
        address in net for net in _DENIED_IPV6_NETWORKS
    )


def normalize_address(address: IPAddress) -> IPAddress:
    """Collapse IPv4-mapped IPv6 to IPv4 so one host has one spelling."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


class PinViolation(httpx.TransportError):
    """The connection did not go to, or the request does not match, the validated pin."""


class ResponseTooLarge(httpx.TransportError):
    """The response body exceeded the dialer's byte limit."""


class UnsupportedEncoding(httpx.TransportError):
    """The response declares a content encoding; only identity bodies are accepted."""


# Request extension a caller may set to tighten (never loosen) the total deadline.
TOTAL_TIMEOUT_EXTENSION = "cu_total_timeout"


@dataclass(frozen=True)
class LoopbackOverride:
    """Test-only: permit non-public pinned addresses and one non-443 port.

    A transport carrying this is refused by the fetch worker.
    """

    port: int


def _peer_address(stream: httpcore.NetworkStream) -> IPAddress:
    sock = stream.get_extra_info("socket")
    if sock is None:
        raise PinViolation("peer address unavailable")
    try:
        return normalize_address(ipaddress.ip_address(sock.getpeername()[0]))
    except (OSError, ValueError, IndexError) as exc:
        raise PinViolation("peer address unavailable") from exc


class _Run:
    """One request's single monotonic deadline and everything it can sever.

    The deadline covers registration, every connect attempt, the TLS handshake, and every
    read. Each blocking socket call is bounded by the time remaining; a watchdog also severs
    registered sockets, and a socket registered after the deadline is closed immediately.
    """

    def __init__(self, total: float) -> None:
        self._deadline = time.monotonic() + total
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._tripped = False
        self.pool: httpcore.ConnectionPool | None = None
        self._timer = threading.Timer(total, self._expire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        return self._tripped or self.remaining() <= 0

    def bound(self, timeout: float | None) -> float:
        """Clamp a per-operation timeout to the time left; raise once none is left."""
        left = self.remaining()
        if left <= 0:
            raise TimeoutError("total time limit exceeded")
        return left if timeout is None else min(timeout, left)

    def register(self, sock: socket.socket) -> None:
        with self._lock:
            if self.expired:
                sock.close()
                raise httpcore.ConnectTimeout("total time limit exceeded")
            self._sockets.append(sock)

    def _expire(self) -> None:
        with self._lock:
            self._tripped = True
            sockets = list(self._sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def abort(self) -> None:
        """Stop the watchdog and close every socket and the pool."""
        self._timer.cancel()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()
        if self.pool is not None:
            self.pool.close()


class _PinnedStream(httpcore.NetworkStream):
    def __init__(self, sock: socket.socket, backend: "_PinnedBackend") -> None:
        self._sock, self._backend = sock, backend

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self._sock.settimeout(self._backend.run.bound(timeout))
            return self._sock.recv(max_bytes)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.ReadError(str(exc)) from exc

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            while buffer:
                self._sock.settimeout(self._backend.run.bound(timeout))
                buffer = buffer[self._sock.send(buffer) :]
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    def close(self) -> None:
        self._sock.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        # Certificate and hostname checks must stay on, against the ORIGINAL hostname.
        if (
            server_hostname != self._backend.host
            or ssl_context.verify_mode is not ssl.CERT_REQUIRED
            or not ssl_context.check_hostname
        ):
            self._sock.close()
            raise PinViolation("TLS must verify the original hostname")
        run = self._backend.run
        secured: ssl.SSLSocket | None = None
        try:
            secured = ssl_context.wrap_socket(
                self._sock, server_hostname=server_hostname, do_handshake_on_connect=False
            )
            run.register(secured)
            # One call, bounded by what is left of the single deadline.
            secured.settimeout(run.bound(timeout))
            secured.do_handshake()
        except httpcore.ConnectTimeout:
            raise
        except TimeoutError as exc:
            (secured or self._sock).close()
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            (secured or self._sock).close()
            raise httpcore.ConnectError(str(exc)) from exc
        return self._backend.accept(_PinnedStream(secured, self._backend))

    def get_extra_info(self, info: str) -> Any:
        sock = self._sock
        try:
            if info == "ssl_object" and isinstance(sock, ssl.SSLSocket):
                return sock._sslobj  # type: ignore[attr-defined]
            if info == "client_addr":
                return sock.getsockname()
            if info == "server_addr":
                return sock.getpeername()
            if info == "is_readable":
                return bool(select.select([sock], [], [], 0)[0])
        except (OSError, ValueError):
            return None
        return sock if info == "socket" else None


class _PinnedBackend(httpcore.NetworkBackend):
    """Ignores the hostname it is asked to dial; connects only to pinned addresses.

    Sockets are created for the pin's own address family and connected to the numeric
    address, so no resolver call is made at all.
    """

    def __init__(
        self,
        host: str,
        port: int,
        addresses: tuple[IPAddress, ...],
        max_attempts: int,
        run: _Run,
    ) -> None:
        self.host, self._port, self._addresses = host, port, addresses
        self._max_attempts, self.run = max_attempts, run

    def accept(self, stream: _PinnedStream) -> _PinnedStream:
        """Verify the peer is a validated address."""
        try:
            if _peer_address(stream) not in self._addresses:
                raise PinViolation("connected peer is not a validated address")
        except PinViolation:
            stream.close()
            raise
        return stream

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        del host, local_address, socket_options  # only the pinned addresses are dialed
        if port != self._port:
            raise PinViolation("unexpected destination port")
        failure: Exception | None = None
        for address in self._addresses[: self._max_attempts]:
            family = (
                socket.AF_INET6 if isinstance(address, ipaddress.IPv6Address) else socket.AF_INET
            )
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                self.run.register(sock)
                sock.settimeout(self.run.bound(timeout))
                sock.connect((str(address), port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except TimeoutError as exc:
                sock.close()
                failure = httpcore.ConnectTimeout(str(exc))
                if self.run.expired:
                    break
                continue
            except OSError as exc:
                sock.close()
                failure = httpcore.ConnectError(str(exc))
                continue
            return self.accept(_PinnedStream(sock, self))
        raise failure if failure is not None else PinViolation("no pinned address")


_HTTPCORE_TO_HTTPX: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
)


def _map_error(exc: Exception, run: _Run) -> Exception:
    if run.expired and not isinstance(exc, PinViolation | ResponseTooLarge | UnsupportedEncoding):
        return httpx.ReadTimeout("total time limit exceeded")
    if isinstance(exc, httpx.HTTPError):
        return exc
    for source, target in _HTTPCORE_TO_HTTPX:
        if isinstance(exc, source):
            return target(str(exc))
    return httpx.TransportError(str(exc))


class _GuardedBody(httpx.SyncByteStream):
    def __init__(
        self,
        response: httpcore.Response,
        finish: Callable[[], None],
        run: _Run,
        max_bytes: int,
        encoded: bool,
    ) -> None:
        self._response, self._finish, self._run = response, finish, run
        self._max_bytes, self._encoded = max_bytes, encoded

    def __iter__(self) -> Iterator[bytes]:
        received = 0
        if self._encoded:
            # The limit counts the bytes we hand on, so a decoder could inflate past it.
            raise UnsupportedEncoding("compressed content encodings are refused")
        try:
            for chunk in self._response.iter_stream():
                received += len(chunk)
                if received > self._max_bytes:
                    raise ResponseTooLarge("response exceeded the byte limit")
                if self._run.expired:
                    raise httpx.ReadTimeout("total time limit exceeded")
                yield chunk
            if self._run.expired:
                raise httpx.ReadTimeout("total time limit exceeded")
        except (PinViolation, ResponseTooLarge, httpx.HTTPError):
            raise
        except Exception as exc:
            raise _map_error(exc, self._run) from exc

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._finish()


def _is_encoded(headers: Sequence[tuple[bytes, bytes]]) -> bool:
    return any(
        token.strip() not in (b"", b"identity")
        for name, value in headers
        if name.lower() == b"content-encoding"
        for token in value.lower().split(b",")
    )


@final
class PinnedTransport(httpx.BaseTransport):
    """HTTPX transport that dials only the validated addresses it was built with.

    Building one performs no I/O. ``handle_request`` is the only network entry point.
    """

    def __init__(
        self,
        addresses: Sequence[IPAddress],
        *,
        ssl_context: ssl.SSLContext | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_attempts: int = 2,
        test_override: LoopbackOverride | None = None,
    ) -> None:
        pinned: list[IPAddress] = []
        for address in addresses:
            if not isinstance(address, ipaddress.IPv4Address | ipaddress.IPv6Address):
                raise ValueError("pinned addresses must be parsed IP addresses")
            if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
                raise ValueError("scoped addresses cannot be pinned")
            if test_override is None and not globally_routable(address):
                raise ValueError("pinned address is not globally routable")
            normalized = normalize_address(address)
            if normalized not in pinned:
                pinned.append(normalized)
        if not pinned:
            raise ValueError("at least one validated address is required")
        if min(connect_timeout, read_timeout, total_timeout) <= 0 or max_bytes < 1:
            raise ValueError("timeouts and byte limit must be positive")
        if max_attempts < 1:
            raise ValueError("at least one attempt is required")
        # Never trust the environment (SSL_CERT_FILE, proxies); verification is always on.
        context = ssl_context or httpx.create_ssl_context(verify=True, trust_env=False)
        if context.verify_mode is not ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("TLS verification cannot be disabled")
        self._pinned = tuple(pinned)
        self._ssl = context
        self._timeouts = (connect_timeout, read_timeout, total_timeout)
        self._max_bytes, self._max_attempts = max_bytes, max_attempts
        self._override = test_override
        self._runs: set[_Run] = set()
        self._runs_lock = threading.Lock()

    def _finish(self, run: _Run) -> None:
        with self._runs_lock:
            self._runs.discard(run)
        run.abort()

    def close(self) -> None:
        """Sever every outstanding request, including streaming bodies not yet closed."""
        with self._runs_lock:
            runs = list(self._runs)
        for run in runs:
            self._finish(run)

    @property
    def pinned_addresses(self) -> tuple[IPAddress, ...]:
        return self._pinned

    @property
    def test_override(self) -> LoopbackOverride | None:
        return self._override

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or HTTPS_PORT
        allowed = self._override.port if self._override else HTTPS_PORT
        host = url.host
        if (
            request.method != "GET"
            or url.scheme != "https"
            or port != allowed
            or url.userinfo
            or not host
            or _is_ip_literal(host)
        ):
            raise httpx.UnsupportedProtocol("only HTTPS GET to a hostname on port 443 is allowed")
        connect, read, total = self._timeouts
        requested = request.extensions.get(TOTAL_TIMEOUT_EXTENSION)
        if isinstance(requested, int | float) and not isinstance(requested, bool):
            total = min(total, max(float(requested), 0.001))
        run = _Run(total)
        pool = httpcore.ConnectionPool(
            ssl_context=self._ssl,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PinnedBackend(host, port, self._pinned, self._max_attempts, run),
        )
        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"connection"]
        headers.append((b"Connection", b"close"))
        core_request = httpcore.Request(
            method=b"GET",
            url=httpcore.URL(
                scheme=b"https", host=host.encode("ascii"), port=url.port, target=url.raw_path
            ),
            headers=headers,
            extensions={
                "timeout": {"connect": connect, "read": read, "write": read, "pool": connect}
            },
        )
        run.pool = pool
        with self._runs_lock:
            self._runs.add(run)
        run.start()
        try:
            response = pool.handle_request(core_request)
        except Exception as exc:
            self._finish(run)
            mapped = _map_error(exc, run)
            if mapped is exc:
                raise
            raise mapped from exc
        body = _GuardedBody(
            response,
            lambda: self._finish(run),
            run,
            self._max_bytes,
            _is_encoded(response.headers),
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=body,
            extensions=response.extensions,
        )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True

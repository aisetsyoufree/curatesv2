"""Gateway-owned pinned dialer: dials only addresses the gateway already validated.

The gateway resolves a host once and validates every answer. This transport then connects
to one of those addresses and never resolves the name itself, while TLS still verifies the
certificate against the original hostname (SNI and hostname check). It is HTTPS-only, port
443-only, never follows redirects, never reads proxy settings, and uses one connection per
request. Nothing here runs at import time or by default; a caller must build a transport.
"""

import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator, Sequence
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
    """One request's total-time watchdog and the sockets it can sever."""

    def __init__(self, total: float) -> None:
        self.expired = False
        self._streams: list[httpcore.NetworkStream] = []
        self._timer = threading.Timer(total, self._expire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def track(self, stream: httpcore.NetworkStream) -> None:
        self._streams.append(stream)

    def _expire(self) -> None:
        self.expired = True
        for stream in list(self._streams):
            sock = stream.get_extra_info("socket")
            try:
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self) -> None:
        self._timer.cancel()


class _PinnedStream(httpcore.NetworkStream):
    def __init__(
        self,
        inner: httpcore.NetworkStream,
        backend: "_PinnedBackend",
    ) -> None:
        self._inner, self._backend = inner, backend

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._inner.close()

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
            self._inner.close()
            raise PinViolation("TLS must verify the original hostname")
        secured = self._inner.start_tls(ssl_context, server_hostname, timeout)
        return self._backend.accept(secured)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _PinnedBackend(httpcore.SyncBackend):
    """Ignores the hostname it is asked to dial; connects only to pinned addresses."""

    def __init__(
        self,
        host: str,
        port: int,
        addresses: tuple[IPAddress, ...],
        max_attempts: int,
        run: _Run,
    ) -> None:
        super().__init__()
        self.host, self._port, self._addresses = host, port, addresses
        self._max_attempts, self._run = max_attempts, run

    def accept(self, stream: httpcore.NetworkStream) -> httpcore.NetworkStream:
        """Verify the peer is a validated address, then guard the stream."""
        try:
            if _peer_address(stream) not in self._addresses:
                raise PinViolation("connected peer is not a validated address")
        except PinViolation:
            stream.close()
            raise
        wrapped = _PinnedStream(stream, self)
        self._run.track(wrapped)
        return wrapped

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        del host  # the name is never looked up; only the pinned addresses are dialed
        if port != self._port:
            raise PinViolation("unexpected destination port")
        failure: Exception | None = None
        for address in self._addresses[: self._max_attempts]:
            try:
                stream = super().connect_tcp(
                    str(address), port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
                continue
            return self.accept(stream)
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
    if run.expired and not isinstance(exc, PinViolation | ResponseTooLarge):
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
        pool: httpcore.ConnectionPool,
        run: _Run,
        max_bytes: int,
    ) -> None:
        self._response, self._pool, self._run, self._max_bytes = response, pool, run, max_bytes

    def __iter__(self) -> Iterator[bytes]:
        received = 0
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
            self._run.stop()
            self._pool.close()


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
        run.start()
        try:
            response = pool.handle_request(core_request)
        except Exception as exc:
            run.stop()
            pool.close()
            mapped = _map_error(exc, run)
            if mapped is exc:
                raise
            raise mapped from exc
        body = _GuardedBody(response, pool, run, self._max_bytes)
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

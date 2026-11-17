"""Pinned dialer tests. The only server is a loopback TLS server with a test-only CA."""

import gzip
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpcore
import httpx
import pytest
import trustme

from cu_rates import fetch_dialer
from cu_rates.fetch_dialer import (
    LoopbackOverride,
    PinnedTransport,
    PinViolation,
    ResponseTooLarge,
)

HOST = "rates.example.test"
LOOPBACK = ipaddress.ip_address("127.0.0.1")
Handler = Callable[[ssl.SSLSocket, str], None]


def reply(
    conn: ssl.SSLSocket, status: str = "200 OK", body: bytes = b"ok", extra: str = ""
) -> None:
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n"
    conn.sendall(head.encode() + body)


class TlsServer:
    def __init__(self, ctx: ssl.SSLContext, handler: Handler) -> None:
        self.requests: list[str] = []
        self._ctx, self._handler = ctx, handler
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._one, args=(raw,), daemon=True).start()

    def _one(self, raw: socket.socket) -> None:
        try:
            with self._ctx.wrap_socket(raw, server_side=True) as conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                path = data.split(b" ", 2)[1].decode()
                self.requests.append(path)
                self._handler(conn, path)
        except OSError:
            pass  # includes a client rejecting our certificate

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


class Lab:
    """A test CA, a client trust store that trusts only it, and servers it can certify."""

    def __init__(self) -> None:
        self.ca = trustme.CA()
        self.servers: list[TlsServer] = []

    def client_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        self.ca.configure_trust(ctx)
        return ctx

    def serve(self, handler: Handler, cert_host: str = HOST) -> TlsServer:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ca.issue_cert(cert_host).configure_cert(ctx)
        server = TlsServer(ctx, handler)
        self.servers.append(server)
        return server

    def transport(self, server: TlsServer, **kwargs: Any) -> PinnedTransport:
        return PinnedTransport(
            [LOOPBACK],
            ssl_context=self.client_context(),
            test_override=LoopbackOverride(server.port),
            **kwargs,
        )


@pytest.fixture
def lab() -> Iterator[Lab]:
    lab = Lab()
    yield lab
    for server in lab.servers:
        server.close()


def get(transport: httpx.BaseTransport, url: str) -> httpx.Response:
    response = transport.handle_request(httpx.Request("GET", url))
    try:
        response.read()
    finally:
        response.close()
    return response


def url_for(server: TlsServer, path: str = "/") -> str:
    return f"https://{HOST}:{server.port}{path}"


@pytest.fixture(autouse=True)
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every name lookup; a hostname lookup would be a pin bypass."""
    seen: list[str] = []
    real = socket.getaddrinfo

    def spy(host: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(host))
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return seen


def test_connects_to_pinned_address_and_verifies_original_hostname(
    lab: Lab, lookups: list[str]
) -> None:
    server = lab.serve(lambda conn, path: reply(conn, body=b"hello"))
    response = get(lab.transport(server), url_for(server))
    assert response.status_code == 200 and response.content == b"hello"
    assert server.requests == ["/"]
    assert lookups == []  # no resolver call at all, not even a numeric one


def test_sockets_are_family_specific_and_never_use_create_connection(
    lab: Lab, lookups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    families: list[int] = []
    real = socket.socket

    class Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            families.append(family)
            super().__init__(family, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("create_connection resolves names")

    server = lab.serve(lambda conn, path: reply(conn))
    transport = lab.transport(server)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", Spy)
    get(transport, url_for(server))
    assert socket.AF_INET in families and lookups == []


def test_each_request_reuses_the_pin_not_a_new_lookup(lab: Lab, lookups: list[str]) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    transport = lab.transport(server)
    get(transport, url_for(server))
    get(transport, url_for(server))
    assert lookups == [] and len(server.requests) == 2


def test_certificate_for_another_hostname_is_rejected(lab: Lab) -> None:
    server = lab.serve(lambda conn, path: reply(conn), cert_host="other.example.test")
    with pytest.raises(httpx.ConnectError):
        get(lab.transport(server), url_for(server))
    assert server.requests == []


def test_untrusted_certificate_is_rejected(lab: Lab) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    stranger = ssl.create_default_context()
    trustme.CA().configure_trust(stranger)
    transport = PinnedTransport(
        [LOOPBACK], ssl_context=stranger, test_override=LoopbackOverride(server.port)
    )
    with pytest.raises(httpx.ConnectError):
        get(transport, url_for(server))
    assert server.requests == []


def test_mismatching_peer_address_is_rejected(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    monkeypatch.setattr(
        fetch_dialer, "_peer_address", lambda stream: ipaddress.ip_address("8.8.8.8")
    )
    with pytest.raises(PinViolation):
        get(lab.transport(server), url_for(server))
    assert server.requests == []


def test_pinned_mapped_address_is_normalized() -> None:
    mapped = ipaddress.ip_address("::ffff:8.8.8.8")
    transport = PinnedTransport([mapped])
    assert transport.pinned_addresses == (ipaddress.ip_address("8.8.8.8"),)


def report_peer_as(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make the real peer look like `text` to the real verification, without touching it."""

    class FakePeer:
        def __init__(self, real: socket.socket) -> None:
            self._real = real

        def getpeername(self) -> tuple[str, int, int, int]:
            return (text, 443, 0, 0)

    original = fetch_dialer._PinnedStream.get_extra_info

    def patched(self: Any, info: str) -> Any:
        found = original(self, info)
        return FakePeer(found) if info == "socket" and found is not None else found

    monkeypatch.setattr(fetch_dialer._PinnedStream, "get_extra_info", patched)


def test_peer_reported_in_ipv4_mapped_form_matches_its_pin(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = lab.serve(lambda conn, path: reply(conn, body=b"mapped"))
    report_peer_as(monkeypatch, "::ffff:127.0.0.1")
    assert get(lab.transport(server), url_for(server)).content == b"mapped"


def test_peer_reported_as_a_mismatching_mapped_address_is_rejected(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    report_peer_as(monkeypatch, "::ffff:8.8.8.8")
    with pytest.raises(PinViolation):
        get(lab.transport(server), url_for(server))
    assert server.requests == []


def test_read_timeout_fires(lab: Lab) -> None:
    def slow(conn: ssl.SSLSocket, path: str) -> None:
        time.sleep(1.5)

    server = lab.serve(slow)
    with pytest.raises(httpx.ReadTimeout):
        get(lab.transport(server, read_timeout=0.2), url_for(server))


def test_total_timeout_fires_on_a_slow_drip(lab: Lab) -> None:
    def drip(conn: ssl.SSLSocket, path: str) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        for _ in range(30):
            conn.sendall(b"x")
            time.sleep(0.1)

    server = lab.serve(drip)
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        get(lab.transport(server, read_timeout=2, total_timeout=0.5), url_for(server))
    assert time.monotonic() - started < 2


def test_connect_attempts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    dialed: list[Any] = []

    def refuse(self: Any, address: Any) -> Any:
        dialed.append(address[0])
        raise ConnectionRefusedError("simulated")

    monkeypatch.setattr(socket.socket, "connect", refuse, raising=False)
    transport = PinnedTransport(
        [ipaddress.ip_address(a) for a in ("8.8.8.8", "8.8.4.4", "1.1.1.1")], max_attempts=2
    )
    with pytest.raises(httpx.ConnectError):
        get(transport, f"https://{HOST}/")
    assert dialed == ["8.8.8.8", "8.8.4.4"]


@pytest.fixture
def stalled() -> Iterator[socket.socket]:
    """A loopback listener whose kernel completes TCP but which never answers TLS."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    yield listener
    listener.close()


def stalled_transport(stalled: socket.socket, lab: Lab, **kwargs: Any) -> PinnedTransport:
    return PinnedTransport(
        [LOOPBACK],
        ssl_context=lab.client_context(),
        test_override=LoopbackOverride(stalled.getsockname()[1]),
        **kwargs,
    )


def test_stalled_tls_upgrade_hits_the_connect_timeout(lab: Lab, stalled: socket.socket) -> None:
    transport = stalled_transport(stalled, lab, connect_timeout=0.2, total_timeout=20)
    started = time.monotonic()
    with pytest.raises(httpx.ConnectTimeout):
        get(transport, f"https://{HOST}:{stalled.getsockname()[1]}/")
    assert time.monotonic() - started < 3


def test_total_timeout_bounds_a_stalled_handshake(lab: Lab, stalled: socket.socket) -> None:
    transport = stalled_transport(
        stalled, lab, connect_timeout=20, read_timeout=20, total_timeout=0.1
    )
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        get(transport, f"https://{HOST}:{stalled.getsockname()[1]}/")
    assert time.monotonic() - started < 1


def test_request_deadline_extension_tightens_the_total_timeout(
    lab: Lab, stalled: socket.socket
) -> None:
    transport = stalled_transport(stalled, lab, connect_timeout=20, total_timeout=20)
    request = httpx.Request(
        "GET",
        f"https://{HOST}:{stalled.getsockname()[1]}/",
        extensions={fetch_dialer.TOTAL_TIMEOUT_EXTENSION: 0.1},
    )
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        transport.handle_request(request)
    assert time.monotonic() - started < 1


def test_socket_registered_after_the_deadline_is_closed_immediately() -> None:
    run = fetch_dialer._Run(0.05)
    time.sleep(0.1)
    late = socket.socket()
    with pytest.raises(httpcore.ConnectTimeout):
        run.register(late)
    assert late.fileno() == -1
    run.abort()


def test_closing_the_transport_severs_a_streaming_response(lab: Lab) -> None:
    def drip(conn: ssl.SSLSocket, path: str) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        for _ in range(50):
            conn.sendall(b"x")
            time.sleep(0.1)

    server = lab.serve(drip)
    transport = lab.transport(server, read_timeout=10, total_timeout=20)
    response = transport.handle_request(httpx.Request("GET", url_for(server)))
    assert transport._runs
    transport.close()
    assert not transport._runs
    started = time.monotonic()
    with pytest.raises(httpx.HTTPError):
        for _ in response.stream:  # type: ignore[union-attr]
            pass
    assert time.monotonic() - started < 2


def test_response_size_limit_fires(lab: Lab) -> None:
    server = lab.serve(lambda conn, path: reply(conn, body=b"x" * 50_000))
    with pytest.raises(ResponseTooLarge):
        get(lab.transport(server, max_bytes=1_000), url_for(server))


def test_compressed_response_is_refused_before_any_decoding(lab: Lab) -> None:
    payload = gzip.compress(b"\0" * 1_000_000)  # ~1 KB on the wire, 1 MB decoded
    server = lab.serve(
        lambda conn, path: reply(conn, body=payload, extra="Content-Encoding: gzip\r\n")
    )
    with pytest.raises(fetch_dialer.UnsupportedEncoding):
        get(lab.transport(server, max_bytes=100_000), url_for(server))


def test_response_lying_about_its_encoding_is_not_decoded_and_stays_capped(lab: Lab) -> None:
    payload = gzip.compress(b"\0" * 1_000_000)
    server = lab.serve(lambda conn, path: reply(conn, body=payload))  # no Content-Encoding
    response = get(lab.transport(server, max_bytes=100_000), url_for(server))
    assert response.content == payload  # raw bytes, never inflated
    with pytest.raises(ResponseTooLarge):
        get(lab.transport(server, max_bytes=len(payload) - 1), url_for(server))


def test_redirects_are_not_followed(lab: Lab) -> None:
    server = lab.serve(
        lambda conn, path: reply(
            conn, "302 Found", b"", f"Location: https://{HOST}:{server.port}/elsewhere\r\n"
        )
    )
    response = get(lab.transport(server), url_for(server))
    assert response.status_code == 302
    assert server.requests == ["/"]


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/",
        f"https://{HOST}:8443/",
        f"https://{HOST}:80/",
        "https://8.8.8.8/",
        f"https://user@{HOST}/",
    ],
)
def test_non_https_non_443_and_literal_hosts_are_refused(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a connection was attempted")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", forbidden)
    with pytest.raises(httpx.UnsupportedProtocol):
        PinnedTransport([ipaddress.ip_address("8.8.8.8")]).handle_request(httpx.Request("GET", url))


def test_only_get_is_permitted() -> None:
    with pytest.raises(httpx.UnsupportedProtocol):
        PinnedTransport([ipaddress.ip_address("8.8.8.8")]).handle_request(
            httpx.Request("POST", f"https://{HOST}/")
        )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "fe80::1",
        "64:ff9b::1",
    ],
)
def test_non_public_addresses_cannot_be_pinned(address: str) -> None:
    with pytest.raises(ValueError):
        PinnedTransport([ipaddress.ip_address(address)])


def test_scoped_addresses_cannot_be_pinned_even_with_the_override() -> None:
    scoped = ipaddress.ip_address("fe80::1%eth0")
    with pytest.raises(ValueError):
        PinnedTransport([scoped])
    with pytest.raises(ValueError):
        PinnedTransport([scoped], test_override=LoopbackOverride(1))


def test_strings_and_empty_pins_are_rejected() -> None:
    with pytest.raises(ValueError):
        PinnedTransport(["8.8.8.8"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        PinnedTransport([])


def test_tls_verification_cannot_be_disabled() -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        PinnedTransport([ipaddress.ip_address("8.8.8.8")], ssl_context=ctx)


def test_construction_performs_no_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    transport = PinnedTransport([ipaddress.ip_address("8.8.8.8")])
    assert transport.pinned_addresses == (ipaddress.ip_address("8.8.8.8"),)

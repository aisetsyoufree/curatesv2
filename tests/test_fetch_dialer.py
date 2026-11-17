"""Pinned dialer tests. The only server is a loopback TLS server with a test-only CA."""

import contextlib
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


def test_registration_after_abort_closes_the_socket_and_raises() -> None:
    run = fetch_dialer._Run(60)
    run.abort()
    late = socket.socket()
    with pytest.raises(httpcore.ConnectTimeout):
        run.register(late)
    assert late.fileno() == -1 and not run._sockets


def test_registration_after_the_watchdog_fires_is_refused_even_once_aborted() -> None:
    run = fetch_dialer._Run(60)
    run._expire()
    run.abort()  # cancels the watchdog; the state must already be final
    late = socket.socket()
    with pytest.raises(httpcore.ConnectTimeout):
        run.register(late)
    assert late.fileno() == -1


def register_racing_abort() -> tuple[fetch_dialer._Run, list[socket.socket]]:
    run = fetch_dialer._Run(60)
    socks = [socket.socket() for _ in range(4)]
    go = threading.Barrier(len(socks) + 1)

    def register(sock: socket.socket) -> None:
        go.wait()
        with contextlib.suppress(httpcore.ConnectTimeout):
            run.register(sock)

    threads = [threading.Thread(target=register, args=(s,)) for s in socks]
    for thread in threads:
        thread.start()
    go.wait()
    run.abort()
    for thread in threads:
        thread.join()
    return run, socks


def test_registering_concurrently_with_abort_never_leaves_a_live_socket() -> None:
    for _ in range(200):
        run, socks = register_racing_abort()
        # Whether registration won or lost the race, nothing may outlive the abort.
        assert all(s.fileno() == -1 for s in socks) and not run._sockets


def test_tls_wrap_racing_an_abort_closes_both_sockets_and_no_unrelated_descriptor() -> None:
    run = fetch_dialer._Run(60)
    raw = socket.socket()
    run.register(raw)
    bystander = socket.socket()  # an unrelated descriptor that must survive
    context = ssl.create_default_context()
    aborter = threading.Thread(target=run.abort)

    def wrap(sock: socket.socket) -> socket.socket:
        wrapped = context.wrap_socket(sock, server_hostname=HOST, do_handshake_on_connect=False)
        aborter.start()  # abort lands between wrapping and registration
        time.sleep(0.2)
        return wrapped

    secured = run.adopt_tls(raw, wrap)
    aborter.join()
    assert secured.fileno() == -1 and raw.fileno() == -1 and not run._sockets
    assert bystander.fileno() != -1
    bystander.close()


def test_tls_wrap_after_the_run_is_sealed_never_wraps_and_closes_the_raw_socket() -> None:
    run = fetch_dialer._Run(60)
    raw = socket.socket()
    run.register(raw)
    run.abort()
    wrapped: list[socket.socket] = []

    def wrap(sock: socket.socket) -> socket.socket:
        wrapped.append(sock)
        return sock

    with pytest.raises(httpcore.ConnectTimeout):
        run.adopt_tls(raw, wrap)
    assert not wrapped and raw.fileno() == -1


def test_start_tls_never_closes_an_unrelated_descriptor_when_an_abort_lands_mid_wrap(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production path: abort fires between the descriptor handover and detach of the wrap."""
    server = lab.serve(lambda conn, path: reply(conn))
    transport = lab.transport(server)
    state: dict[str, Any] = {}
    real_detach = socket.socket.detach

    def detach(self: socket.socket) -> int:
        if threading.current_thread() is threading.main_thread() and "bystander" not in state:
            (run,) = transport._runs
            raw_fd = self.fileno()
            aborter = threading.Thread(target=run.abort)
            aborter.start()
            aborter.join(timeout=0.3)  # an unsafe wrap lets the abort finish; a safe one blocks
            filler: list[socket.socket] = []
            bystander = socket.socket()
            if not aborter.is_alive():
                # The raw descriptor was freed under the wrapper: make a bystander reuse it.
                while bystander.fileno() != raw_fd and len(filler) < 4096:
                    filler.append(bystander)
                    bystander = socket.socket()
            for extra in filler:
                extra.close()
            state.update(bystander=bystander, aborter=aborter)
        return real_detach(self)

    monkeypatch.setattr(socket.socket, "detach", detach, raising=False)
    with pytest.raises(httpx.HTTPError):
        transport.handle_request(httpx.Request("GET", url_for(server)))
    state["aborter"].join()
    bystander = state["bystander"]
    try:
        bystander.getsockname()  # EBADF if the wrapper closed the reused descriptor number
    finally:
        bystander.close()


def test_the_watchdog_is_scheduled_from_the_absolute_deadline() -> None:
    run = fetch_dialer._Run(0.3)
    started = time.monotonic()
    time.sleep(0.2)  # slow setup between building the run and starting it
    run.start()
    sock = socket.socket()
    run.register(sock)
    time.sleep(0.4 - (time.monotonic() - started))  # deadline (0.3) plus a 0.1 s bound
    assert sock.fileno() == -1
    run.abort()


def test_an_early_watchdog_wakeup_rechecks_the_deadline_and_rearms() -> None:
    run = fetch_dialer._Run(60)
    run.start()
    sock = socket.socket()
    run.register(sock)
    run._fire()  # woke early: nothing may be severed while time remains
    assert sock.fileno() != -1 and run._state == run._OPEN
    run.abort()
    assert sock.fileno() == -1


def slow_sever(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Make every sever linger after announcing itself, so a second closer can race it."""
    began = threading.Event()
    real = fetch_dialer._sever

    def slow(sock: socket.socket) -> None:
        began.set()
        time.sleep(0.3)
        real(sock)

    monkeypatch.setattr(fetch_dialer, "_sever", slow)
    return began


def test_abort_waits_for_a_watchdog_that_is_mid_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    run = fetch_dialer._Run(60)
    sock = socket.socket()
    run.register(sock)
    began = slow_sever(monkeypatch)
    watchdog = threading.Thread(target=run._expire)
    watchdog.start()
    assert began.wait(timeout=2)
    run.abort()
    assert sock.fileno() == -1 and not run._sockets  # nothing left open when abort returns
    watchdog.join()


def streaming_transport(lab: Lab) -> tuple[PinnedTransport, httpx.Response, list[socket.socket]]:
    def stall(conn: ssl.SSLSocket, path: str) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        time.sleep(3)

    server = lab.serve(stall)
    transport = lab.transport(server, read_timeout=20, total_timeout=30)
    response = transport.handle_request(httpx.Request("GET", url_for(server)))
    (run,) = transport._runs
    socks = list(run._sockets)
    assert socks
    return transport, response, socks


def test_a_second_transport_close_waits_for_the_first_to_finish_closing(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _response, socks = streaming_transport(lab)
    began = slow_sever(monkeypatch)
    first = threading.Thread(target=transport.close)
    first.start()
    assert began.wait(timeout=2)
    transport.close()
    assert all(s.fileno() == -1 for s in socks) and not transport._runs
    first.join()


def test_transport_close_waits_for_a_response_close_that_is_mid_teardown(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, response, socks = streaming_transport(lab)
    began = slow_sever(monkeypatch)
    closer = threading.Thread(target=response.close)
    closer.start()
    assert began.wait(timeout=2)
    transport.close()
    assert all(s.fileno() == -1 for s in socks)
    closer.join()


def test_request_racing_transport_close_is_refused_and_sends_nothing(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    transport = lab.transport(server)
    real_pool = httpcore.ConnectionPool

    def pool_then_close(*args: Any, **kwargs: Any) -> httpcore.ConnectionPool:
        transport.close()  # lands after the request began, before it registers
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(httpcore, "ConnectionPool", pool_then_close)
    with pytest.raises(httpx.TransportError):
        transport.handle_request(httpx.Request("GET", url_for(server)))
    assert not transport._runs and not server.requests


def test_a_closed_transport_refuses_every_later_request(lab: Lab) -> None:
    server = lab.serve(lambda conn, path: reply(conn))
    transport = lab.transport(server)
    transport.close()
    with pytest.raises(httpx.TransportError):
        transport.handle_request(httpx.Request("GET", url_for(server)))
    assert not server.requests


def test_keyboard_interrupt_during_connect_releases_the_socket_and_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[socket.socket] = []
    real_socket = socket.socket

    def tracking(*args: Any, **kwargs: Any) -> socket.socket:
        created.append(real_socket(*args, **kwargs))
        return created[-1]

    def interrupt(self: socket.socket, address: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(socket, "socket", tracking)
    monkeypatch.setattr(real_socket, "connect", interrupt, raising=False)
    transport = PinnedTransport([ipaddress.ip_address("8.8.8.8")], total_timeout=20)
    with pytest.raises(KeyboardInterrupt):
        transport.handle_request(httpx.Request("GET", f"https://{HOST}/"))
    assert created and all(s.fileno() == -1 for s in created)
    assert not transport._runs


def test_keyboard_interrupt_from_the_pool_finishes_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[fetch_dialer._Run] = []
    real_start = fetch_dialer._Run.start

    def start(self: fetch_dialer._Run) -> None:
        runs.append(self)
        real_start(self)

    def interrupt(self: httpcore.ConnectionPool, request: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fetch_dialer._Run, "start", start)
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", interrupt)
    transport = PinnedTransport([ipaddress.ip_address("8.8.8.8")], total_timeout=20)
    with pytest.raises(KeyboardInterrupt):
        transport.handle_request(httpx.Request("GET", f"https://{HOST}/"))
    (run,) = runs
    assert run._state == run._CLOSED and not transport._runs


def test_closing_the_transport_wakes_a_blocked_tls_read(lab: Lab) -> None:
    def stall(conn: ssl.SSLSocket, path: str) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        time.sleep(5)

    server = lab.serve(stall)
    transport = lab.transport(server, read_timeout=20, total_timeout=30)
    response = transport.handle_request(httpx.Request("GET", url_for(server)))
    finished = threading.Event()

    def reader() -> None:
        with contextlib.suppress(httpx.HTTPError):
            for _ in response.stream:  # type: ignore[union-attr]
                pass
        finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)  # let the reader block inside recv
    transport.close()
    assert finished.wait(timeout=1.5)


def terminal_run(transport: PinnedTransport) -> fetch_dialer._Run:
    (run,) = transport._runs
    return run


def test_encoding_rejection_releases_the_socket_without_caller_cleanup(lab: Lab) -> None:
    server = lab.serve(lambda conn, path: reply(conn, extra="Content-Encoding: gzip\r\n"))
    transport = lab.transport(server)
    response = transport.handle_request(httpx.Request("GET", url_for(server)))
    run = terminal_run(transport)
    assert run._sockets
    with pytest.raises(fetch_dialer.UnsupportedEncoding):
        for _ in response.stream:  # type: ignore[union-attr]
            pass
    assert run._state == run._CLOSED and not run._sockets and not transport._runs


def test_deadline_expiry_releases_the_socket_without_caller_cleanup(lab: Lab) -> None:
    def drip(conn: ssl.SSLSocket, path: str) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        for _ in range(30):
            conn.sendall(b"x")
            time.sleep(0.1)

    server = lab.serve(drip)
    transport = lab.transport(server, read_timeout=5, total_timeout=0.5)
    response = transport.handle_request(httpx.Request("GET", url_for(server)))
    run = terminal_run(transport)
    with pytest.raises(httpx.HTTPError):
        for _ in response.stream:  # type: ignore[union-attr]
            pass
    assert run._state == run._CLOSED and not run._sockets and not transport._runs


def test_black_holed_tcp_connect_hits_the_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def black_hole(self: socket.socket, address: Any) -> None:
        # A SYN that is never answered: the kernel gives up only when the socket timeout does.
        time.sleep(min(self.gettimeout() or 30, 30))
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", black_hole, raising=False)
    transport = PinnedTransport(
        [ipaddress.ip_address("8.8.8.8")], connect_timeout=0.2, total_timeout=20, max_attempts=1
    )
    started = time.monotonic()
    with pytest.raises(httpx.ConnectTimeout):
        get(transport, f"https://{HOST}/")
    assert time.monotonic() - started < 3


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

    # Socket level, so any URL-guard regression cannot dial out whatever backend is used.
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
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

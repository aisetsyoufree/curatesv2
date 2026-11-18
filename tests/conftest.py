"""Test configuration and network denial fixtures."""

import ipaddress
import socket
from collections.abc import Generator
from typing import Any

import pytest

_network_allowed: bool = False


def _is_loopback_host(host: str) -> bool:
    cleaned = host.strip().lower().rstrip(".")
    if cleaned == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return bool(
        isinstance(ip, ipaddress.IPv6Address)
        and ip.ipv4_mapped is not None
        and ip.ipv4_mapped.is_loopback
    )


def _is_address_allowed(sock: socket.socket, address: Any) -> bool:
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is not None and getattr(sock, "family", None) == af_unix:
        return True

    if isinstance(address, (str, bytes)) and (
        af_unix is not None and getattr(sock, "family", None) == af_unix
    ):
        return True

    if isinstance(address, tuple) and len(address) >= 1:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("utf-8", errors="replace")
        if isinstance(host, str) and _is_loopback_host(host):
            return True

    return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: opt out of network denial to allow external network connections in tests",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    global _network_allowed
    _network_allowed = item.get_closest_marker("allow_network") is not None


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    global _network_allowed
    _network_allowed = False


@pytest.fixture(autouse=True)
def _network_marker_guard(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    global _network_allowed
    previous = _network_allowed
    if request.node.get_closest_marker("allow_network") is not None:
        _network_allowed = True
    try:
        yield
    finally:
        _network_allowed = previous


@pytest.fixture(scope="session", autouse=True)
def deny_non_loopback_network() -> Generator[None, None, None]:
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, *args: Any, **kwargs: Any) -> None:
        address = args[0] if args else kwargs.get("address")
        if not _network_allowed and not _is_address_allowed(self, address):
            raise AssertionError(
                f"Network connection to non-loopback address {address!r} is denied in tests. "
                "Use @pytest.mark.allow_network to opt out if this test requires network access."
            )
        return orig_connect(self, *args, **kwargs)

    def guarded_connect_ex(self: socket.socket, *args: Any, **kwargs: Any) -> int:
        address = args[0] if args else kwargs.get("address")
        if not _network_allowed and not _is_address_allowed(self, address):
            raise AssertionError(
                f"Network connection to non-loopback address {address!r} is denied in tests. "
                "Use @pytest.mark.allow_network to opt out if this test requires network access."
            )
        return orig_connect_ex(self, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[assignment,method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment,method-assign]
    try:
        yield
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]

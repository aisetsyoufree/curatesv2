"""Tests for test-suite network denial guard."""

import os
import socket
from pathlib import Path

import pytest


def test_non_loopback_connect_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="Network connection to non-loopback address"):
            s.connect(("192.0.2.1", 80))
    finally:
        s.close()


def test_non_loopback_connect_ex_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="Network connection to non-loopback address"):
            s.connect_ex(("192.0.2.1", 80))
    finally:
        s.close()


def test_non_loopback_hostname_connect_is_blocked() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="Network connection to non-loopback address"):
            s.connect(("example.com", 80))
    finally:
        s.close()


def test_loopback_connect_is_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))
    finally:
        client.close()
        server.close()


def test_localhost_connect_is_allowed() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("localhost", port))
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("host", ["127.0.0.2", "127.42.0.1"])
def test_other_loopback_octets_are_permitted_by_guard(host: str) -> None:
    # Closed socket will return EBADF from orig_connect_ex without raising AssertionError.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.close()
    ret = s.connect_ex((host, 80))
    assert ret != 0  # not an AssertionError; passed to OS


def test_loopback_ipv6_connect_is_allowed() -> None:
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("IPv6 socket creation not supported in this environment")

    try:
        server.bind(("::1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        client = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            client.connect(("::1", port))
        finally:
            client.close()
    finally:
        server.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not supported on this platform")
def test_af_unix_connect_is_allowed(tmp_path: Path) -> None:
    sock_path = f"/tmp/cu_test_{os.getpid()}.sock"
    try:
        if os.path.exists(sock_path):
            os.remove(sock_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(sock_path)
        finally:
            client.close()
            server.close()
    finally:
        if os.path.exists(sock_path):
            os.remove(sock_path)


@pytest.mark.allow_network
def test_allow_network_marker_bypasses_denial() -> None:
    # With allow_network marker, non-loopback address is passed through to the OS.
    # On a closed socket, this returns EBADF from the OS without raising AssertionError.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.close()
    ret = s.connect_ex(("192.0.2.1", 80))
    assert ret != 0

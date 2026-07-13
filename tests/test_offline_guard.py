from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from offline_guard import OfflineNetworkError


def test_external_dns_is_blocked() -> None:
    with pytest.raises(OfflineNetworkError):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(OfflineNetworkError):
        socket.gethostbyname("example.com")


def test_external_udp_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        with pytest.raises(OfflineNetworkError):
            client.sendto(b"blocked", ("8.8.8.8", 53))


def test_loopback_socket_is_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1):
            connection, _ = listener.accept()
            connection.close()


def test_network_capable_shell_command_is_blocked() -> None:
    with pytest.raises(OfflineNetworkError):
        subprocess.run(["curl", "https://example.com"], check=False)


def test_python_subprocess_inherits_offline_guard() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.getaddrinfo('example.com', 443)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "OfflineNetworkError" in result.stderr

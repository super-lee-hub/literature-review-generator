from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from offline_guard import OfflineNetworkError, live_api_skip_reason, offline_enabled


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


@pytest.mark.parametrize(
    ("marker_names", "environment", "expected_reason"),
    [
        (set(), {}, None),
        ({"live_api"}, {}, "live API test not explicitly enabled"),
        (
            {"live_api"},
            {"AUTO_GENERATE_RUN_LIVE_API": "1"},
            "live API credential is not configured",
        ),
        (
            {"live_api"},
            {"AUTO_GENERATE_LIVE_API_KEY": "test-key"},
            "live API test not explicitly enabled",
        ),
        (
            {"live_api"},
            {
                "AUTO_GENERATE_RUN_LIVE_API": "1",
                "AUTO_GENERATE_LIVE_API_KEY": "test-key",
            },
            None,
        ),
    ],
)
def test_live_api_gate_requires_marker_opt_in_and_credential(
    marker_names: set[str],
    environment: dict[str, str],
    expected_reason: str | None,
) -> None:
    assert live_api_skip_reason(marker_names, environment) == expected_reason


@pytest.mark.parametrize(
    ("environment", "expected_reason"),
    [
        (
            {
                "AUTO_GENERATE_RUN_LIVE_API": "1",
                "OPENAI_API_KEY": "openai-secret",
            },
            "deepseek live API credential is not configured",
        ),
        (
            {
                "AUTO_GENERATE_RUN_LIVE_API": "1",
                "AUTO_GENERATE_LIVE_API_KEY": "generic-secret",
            },
            "deepseek live API credential is not configured",
        ),
        (
            {
                "AUTO_GENERATE_RUN_LIVE_API": "1",
                "DEEPSEEK_API_KEY": "deepseek-secret",
            },
            None,
        ),
        (
            {"DEEPSEEK_API_KEY": "deepseek-secret"},
            "live API test not explicitly enabled",
        ),
    ],
)
def test_provider_aware_deepseek_live_gate_never_uses_other_provider_keys(
    environment: dict[str, str],
    expected_reason: str | None,
) -> None:
    assert (
        live_api_skip_reason({"live_api"}, environment, provider="deepseek")
        == expected_reason
    )


def test_openai_key_does_not_disable_offline_guard_for_deepseek_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AUTO_GENERATE_LIVE_API_KEY",
        "AUTO_GENERATE_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_API", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    assert offline_enabled() is True

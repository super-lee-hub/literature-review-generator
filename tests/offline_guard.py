"""Strict network isolation used by the pytest process and its Python children."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Collection, Mapping, MutableMapping, Sequence


class OfflineNetworkError(OSError):
    """Raised when a test attempts to access a non-loopback network target."""


_LIVE_API_CREDENTIAL_ENV_NAMES = (
    "AUTO_GENERATE_LIVE_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
)


def live_api_skip_reason(
    marker_names: Collection[str],
    environment: Mapping[str, str],
) -> str | None:
    if "live_api" not in marker_names:
        return None
    if environment.get("AUTO_GENERATE_RUN_LIVE_API") != "1":
        return "live API test not explicitly enabled"
    if not any(environment.get(name) for name in _LIVE_API_CREDENTIAL_ENV_NAMES):
        return "live API credential is not configured"
    return None


_INSTALLED = False
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_SOCKET_SENDTO = socket.socket.sendto
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__
_ORIGINAL_OS_SYSTEM = os.system
_ORIGINAL_OS_POPEN = os.popen
_BLOCKED_COMMAND = re.compile(
    r"(?:^|[\\/\s\"'])(?:curl(?:\.exe)?|wget(?:\.exe)?|powershell(?:\.exe)?|pwsh(?:\.exe)?)(?:$|\s)|"
    r"\b(?:Invoke-WebRequest|Invoke-RestMethod)\b",
    flags=re.IGNORECASE,
)


def offline_enabled() -> bool:
    if (
        os.environ.get("AUTO_GENERATE_RUN_LIVE_API") == "1"
        and any(os.environ.get(name) for name in _LIVE_API_CREDENTIAL_ENV_NAMES)
    ):
        return False
    value = os.environ.get("AUTO_GENERATE_OFFLINE_TESTS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _loopback_host(host: Any) -> bool:
    if host in (None, "", b""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    text = str(host).strip().rstrip(".").lower()
    if text in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _assert_loopback(host: Any) -> None:
    if not _loopback_host(host):
        raise OfflineNetworkError(
            f"external network access is disabled during pytest: host={host!r}"
        )


def _guarded_connect(sock: socket.socket, address: Any) -> Any:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        host = address[0] if isinstance(address, tuple) and address else address
        _assert_loopback(host)
    return _ORIGINAL_SOCKET_CONNECT(sock, address)


def _guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        host = address[0] if isinstance(address, tuple) and address else address
        _assert_loopback(host)
    return _ORIGINAL_SOCKET_CONNECT_EX(sock, address)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
    host = address[0] if isinstance(address, tuple) and address else address
    _assert_loopback(host)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    _assert_loopback(host)
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_gethostbyname(host: Any) -> str:
    _assert_loopback(host)
    return _ORIGINAL_GETHOSTBYNAME(host)


def _guarded_gethostbyname_ex(host: Any) -> Any:
    _assert_loopback(host)
    return _ORIGINAL_GETHOSTBYNAME_EX(host)


def _guarded_sendto(sock: socket.socket, data: Any, *args: Any) -> int:
    if sock.family in {socket.AF_INET, socket.AF_INET6} and args:
        address = args[-1]
        host = address[0] if isinstance(address, tuple) and address else address
        _assert_loopback(host)
    return _ORIGINAL_SOCKET_SENDTO(sock, data, *args)


def _command_text(command: Any) -> str:
    if isinstance(command, (str, bytes)):
        return command.decode(errors="replace") if isinstance(command, bytes) else command
    if isinstance(command, Sequence):
        return " ".join(str(part) for part in command)
    return str(command)


def _assert_offline_command(command: Any) -> None:
    rendered = _command_text(command)
    if _BLOCKED_COMMAND.search(rendered):
        raise OfflineNetworkError(
            f"network-capable shell command is disabled during pytest: {rendered}"
        )


def _child_environment(env: Mapping[str, str] | None) -> MutableMapping[str, str] | None:
    if env is None:
        return None
    merged = dict(env)
    for name in (
        "AUTO_GENERATE_OFFLINE_TESTS",
        "AUTO_GENERATE_FAIL_ON_UNEXPECTED_SKIP",
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        if name in os.environ:
            merged[name] = os.environ[name]
    return merged


def _guarded_popen_init(self: subprocess.Popen[Any], args: Any, *pargs: Any, **kwargs: Any) -> None:
    _assert_offline_command(args)
    if "env" in kwargs:
        kwargs["env"] = _child_environment(kwargs.get("env"))
    _ORIGINAL_POPEN_INIT(self, args, *pargs, **kwargs)


def _guarded_os_system(command: str) -> int:
    _assert_offline_command(command)
    return _ORIGINAL_OS_SYSTEM(command)


def _guarded_os_popen(command: str, mode: str = "r", buffering: int = -1):
    _assert_offline_command(command)
    return _ORIGINAL_OS_POPEN(command, mode, buffering)


def configure_offline_environment() -> None:
    """Configure inherited environment before any test launches a subprocess."""

    os.environ.setdefault("AUTO_GENERATE_OFFLINE_TESTS", "1")
    os.environ.setdefault("AUTO_GENERATE_FAIL_ON_UNEXPECTED_SKIP", "1")
    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:9")
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:9")
    os.environ.setdefault("ALL_PROXY", "http://127.0.0.1:9")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

    tests_dir = Path(__file__).resolve().parent
    sitecustomize_dir = tests_dir / "offline_sitecustomize"
    current = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    for path in (str(tests_dir), str(sitecustomize_dir)):
        if path not in current:
            current.insert(0, path)
    os.environ["PYTHONPATH"] = os.pathsep.join(current)


def install_offline_guard() -> None:
    """Install the guard once when strict-offline testing is enabled."""

    global _INSTALLED
    if _INSTALLED or not offline_enabled():
        return
    configure_offline_environment()
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = _guarded_sendto  # type: ignore[method-assign]
    socket.create_connection = _guarded_create_connection
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex
    subprocess.Popen.__init__ = _guarded_popen_init  # type: ignore[method-assign]
    os.system = _guarded_os_system
    os.popen = _guarded_os_popen
    _INSTALLED = True

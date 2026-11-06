from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import launch_gui


REPO_ROOT = Path(__file__).resolve().parents[1]
START_GUI = REPO_ROOT / "start_gui.bat"
_PYTHON_ENV_ASSIGNMENT = 'set "PYTHON_ENV=%~dp0.venv\\Scripts\\python.exe"'
_PYTHON_EXE_ASSIGNMENT = 'set "PYTHON_EXE=%PYTHON_ENV%"'


def _safe_launcher_source() -> str:
    source = START_GUI.read_text(encoding="utf-8-sig")
    lowered = source.casefold()
    assert "stop-process" not in lowered, "GUI launcher must not kill other processes"
    assert "conda" not in lowered, "GUI launcher must not activate an ambient environment"
    assert "pip install" not in lowered, "GUI launcher must not install packages"
    assert _PYTHON_ENV_ASSIGNMENT.casefold() in lowered
    assert _PYTHON_EXE_ASSIGNMENT.casefold() in lowered
    return source


def _install_fake_python_launcher(tmp_path: Path) -> tuple[Path, Path, Path]:
    if os.name != "nt":
        pytest.skip("start_gui.bat contract tests require Windows cmd.exe")
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir(parents=True)
    python_env = checkout / ".venv" / "Scripts"
    python_env.mkdir(parents=True)
    (python_env / "python.exe").write_bytes(b"test placeholder; never executed")
    fake_python = checkout / "fake-python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        ">>\"%FAKE_PYTHON_LOG%\" echo CALL:%*\r\n"
        "if \"%~1\"==\"-c\" (\r\n"
        "  if \"%FAKE_PYTHON_VERSION%\"==\"3.11\" exit /b 0\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    source = _safe_launcher_source()
    assert source.count(_PYTHON_ENV_ASSIGNMENT) == 1
    assert source.count(_PYTHON_EXE_ASSIGNMENT) == 1
    source = source.replace(
        _PYTHON_EXE_ASSIGNMENT,
        'set "PYTHON_EXE=%~dp0fake-python.cmd"',
        1,
    )
    # A .cmd test double needs CALL to return to its parent batch file; the
    # production interpreter is a .exe and does not need this test-only prefix.
    source = source.replace('"%PYTHON_EXE%"', 'call "%PYTHON_EXE%"')
    batch_path = checkout / "start_gui-test.bat"
    batch_path.write_text(source, encoding="utf-8", newline="")
    return checkout, batch_path, fake_python


def _run_batch(batch_path: Path, args: tuple[str, ...], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(batch_path), *args],
        cwd=batch_path.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_start_gui_is_portable_and_has_no_destructive_or_ambient_setup() -> None:
    source = _safe_launcher_source()
    lowered = source.casefold()
    assert "sys.version_info" in lowered
    assert "--port 8951" in lowered
    assert "--strict-port" in lowered
    assert "--config" in lowered


def test_launcher_uses_repo_python_and_fails_without_ambient_fallback(tmp_path: Path) -> None:
    checkout, batch_path, _fake_python = _install_fake_python_launcher(tmp_path)
    (checkout / ".venv" / "Scripts" / "python.exe").unlink()
    ambient = checkout / "ambient"
    ambient.mkdir()
    ambient_python = ambient / "python.cmd"
    ambient_python.write_text(
        "@echo off\r\n"
        ">>\"%FAKE_PYTHON_LOG%\" echo AMBIENT:%*\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    log_path = checkout / "python-call.log"
    env = dict(os.environ)
    env["FAKE_PYTHON_LOG"] = str(log_path)
    env["FAKE_PYTHON_VERSION"] = "3.11"
    env["PATH"] = str(ambient) + os.pathsep + env.get("PATH", "")

    result = _run_batch(batch_path, (), env)

    assert result.returncode != 0
    assert "python" in (result.stdout + result.stderr).casefold()
    assert not log_path.exists(), "a missing checkout interpreter must not fall through to PATH"


def test_launcher_rejects_unsupported_python_before_gui_entrypoint(tmp_path: Path) -> None:
    checkout, batch_path, _fake_python = _install_fake_python_launcher(tmp_path)
    log_path = checkout / "python-call.log"
    env = dict(os.environ)
    env["FAKE_PYTHON_LOG"] = str(log_path)
    env["FAKE_PYTHON_VERSION"] = "3.10"

    result = _run_batch(batch_path, (), env)

    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert result.returncode != 0
    assert "3.11" in (result.stdout + result.stderr)
    assert calls and all("launch_gui.py" not in call for call in calls)


def test_launcher_forwards_config_and_user_args_but_enforces_stable_strict_port(
    tmp_path: Path,
) -> None:
    checkout, batch_path, _fake_python = _install_fake_python_launcher(tmp_path)
    default_config = checkout / "config.ini"
    user_config = checkout / "user config.ini"
    default_bytes = b"preserve-existing-default-config"
    user_bytes = b"preserve-existing-user-config"
    default_config.write_bytes(default_bytes)
    user_config.write_bytes(user_bytes)
    log_path = checkout / "python-call.log"
    env = dict(os.environ)
    env["FAKE_PYTHON_LOG"] = str(log_path)
    env["FAKE_PYTHON_VERSION"] = "3.11"

    result = _run_batch(
        batch_path,
        (
            "--config",
            str(user_config),
            "--no-show",
            "--reload",
            "--port",
            "9012",
        ),
        env,
    )

    calls = log_path.read_text(encoding="utf-8").splitlines()
    launch_call = next(call for call in calls if "launch_gui.py" in call)
    assert result.returncode == 0
    assert f'--config "{default_config}"' in launch_call
    assert f'--config "{user_config}"' in launch_call
    assert "--no-show" in launch_call and "--reload" in launch_call
    assert launch_call.endswith("--port 8951 --strict-port")
    assert default_config.read_bytes() == default_bytes
    assert user_config.read_bytes() == user_bytes


def test_launch_gui_strict_port_does_not_select_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, int]] = []

    def unavailable(host: str, port: int) -> bool:
        attempts.append((host, port))
        return False

    monkeypatch.setattr(launch_gui, "_can_bind", unavailable)

    with pytest.raises(RuntimeError, match="requested GUI port 8951 is unavailable"):
        launch_gui._pick_available_port(8951, strict=True)
    assert attempts == [("127.0.0.1", 8951)]

    attempts.clear()
    def available(host: str, port: int) -> bool:
        attempts.append((host, port))
        return True

    monkeypatch.setattr(launch_gui, "_can_bind", available)
    assert launch_gui._pick_available_port(8951, strict=True) == 8951
    assert attempts == [("127.0.0.1", 8951)]

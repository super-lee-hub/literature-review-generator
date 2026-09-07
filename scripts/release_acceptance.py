"""Fail-closed release acceptance entrypoint.

The default invocation is offline. A real-provider run requires an explicit
runtime spec, positive provider-call/output-token/retry/wall-clock budgets, a
successful formal ``reviewctl preflight``, and
``AUTO_GENERATE_RUN_LIVE_ACCEPTANCE=1``.

Execution is delegated to the public ``python -m reviewctl`` control plane.
This script never contains a direct provider request or a provider-specific
transport shortcut. Credential values and subprocess output are redacted
before they can enter the JSON result.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping


OFFLINE_GATE = "A"
PREFLIGHT_GATE = "B"
LIVE_GATES = ("C", "D", "E", "F", "G", "H", "I", "J", "Q")
POST_LIVE_GATES = ("R", "S", "T")
ALL_GATES = (OFFLINE_GATE, PREFLIGHT_GATE, *LIVE_GATES, *POST_LIVE_GATES)

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_PROVIDER_CALL_CEILING = 24
DEFAULT_OUTPUT_TOKEN_CEILING = 5_000_000
DEFAULT_RETRY_CEILING = 2

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,}\"]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,}\"]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,}\"]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
)
_CREDENTIAL_MARKERS = (
    "credential",
    "api key",
    "api_key",
    "template",
    "placeholder",
    "占位符",
    "缺少",
    "missing_required_api_keys",
    "401",
    "403",
)


def _redact(value: Any) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]",
            text,
        )
    return text[:4000]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _command(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int | None = None,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    """Run one bounded command and return a secret-safe structured result."""

    command = [executable or sys.executable, *args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "status": "TIMEOUT",
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": _redact(exc.stdout or ""),
            "stderr_tail": _redact(exc.stderr or ""),
        }

    payload: dict[str, Any]
    stdout = (completed.stdout or "").strip()
    try:
        raw_lines = stdout.splitlines()
        parsed = json.loads(raw_lines[-1]) if raw_lines else {}
        payload = parsed if isinstance(parsed, dict) else {"json_result": parsed}
    except (json.JSONDecodeError, TypeError):
        payload = {"stdout_tail": _redact(stdout)}
    payload = _redact_payload(payload)
    payload["command"] = command
    payload["exit_code"] = completed.returncode
    payload["status"] = "PASS" if completed.returncode == 0 else "FAIL"
    if completed.stderr:
        payload["stderr_tail"] = _redact(completed.stderr)
    return payload


def _contains_credential_blocker(payload: Any) -> bool:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
    except (TypeError, ValueError):
        text = str(payload).casefold()
    return any(marker.casefold() in text for marker in _CREDENTIAL_MARKERS)


def _offline_gate(repo_root: Path, *, timeout_seconds: int) -> dict[str, Any]:
    pyright_command = ["-m", "pyright"]
    active_prefix = Path(sys.prefix).resolve()
    if active_prefix.name.casefold() == "venv" and active_prefix.parent != repo_root:
        pyright_command.extend(["--venvpath", str(active_prefix.parent)])
    commands = [
        ["-m", "compileall", "-q", "."],
        [
            "-m",
            "pytest",
            "-q",
            "--strict-markers",
            "-p",
            "no:cacheprovider",
            "-m",
            "not live_api and not playwright and not heavy_ocr and not live_acceptance",
        ],
        pyright_command,
        ["-m", "pip", "check"],
        ["-m", "reviewctl", "doctor", "--config", "config.ini.example"],
        ["diff", "--check"],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        result = _command(
            command,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            executable="git" if command[:1] == ["diff"] else None,
        )
        results.append(result)
        if result.get("status") != "PASS":
            return {"gate": OFFLINE_GATE, "status": "FAIL", "results": results}
    return {"gate": OFFLINE_GATE, "status": "PASS", "results": results}


def _read_spec(spec: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        payload = json.loads(spec.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": f"spec is unreadable: {type(exc).__name__}",
        }
    if not isinstance(payload, dict):
        return None, {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": "spec root must be a JSON object",
        }
    return payload, None


def _spec_config_path(spec: Path, payload: Mapping[str, Any]) -> Path:
    raw_config = str(payload.get("config") or "config.ini")
    config_path = Path(raw_config).expanduser()
    if not config_path.is_absolute():
        config_path = (spec.parent / config_path).resolve()
    return config_path


def _effective_budget(payload: Mapping[str, Any], args: argparse.Namespace) -> dict[str, int]:
    raw = payload.get("acceptance_budget")
    budget = raw if isinstance(raw, Mapping) else {}
    values = {
        "max_provider_calls": int(budget.get("max_provider_calls", args.max_provider_calls)),
        "max_output_tokens": int(budget.get("max_output_tokens", args.max_output_tokens)),
        "timeout_seconds": int(budget.get("timeout_seconds", args.timeout_seconds)),
        "max_retry_attempts": int(budget.get("max_retry_attempts", args.max_retry_attempts)),
    }
    if any(value <= 0 for value in values.values()):
        raise ValueError("all acceptance budget values must be positive")
    if values["max_provider_calls"] > args.max_provider_calls:
        raise ValueError("spec max_provider_calls exceeds the command ceiling")
    if values["max_output_tokens"] > args.max_output_tokens:
        raise ValueError("spec max_output_tokens exceeds the command ceiling")
    if values["timeout_seconds"] > args.timeout_seconds:
        raise ValueError("spec timeout_seconds exceeds the command ceiling")
    if values["max_retry_attempts"] > args.max_retry_attempts:
        raise ValueError("spec max_retry_attempts exceeds the command ceiling")
    return values


def _preflight_gate(
    repo_root: Path,
    *,
    spec: Path | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if spec is None:
        return {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": "--spec is required for exact-path provider preflight",
        }
    payload, error = _read_spec(spec)
    if error is not None or payload is None:
        return error or {"gate": PREFLIGHT_GATE, "status": "BLOCKED_INPUT"}
    config_path = _spec_config_path(spec, payload)
    action = str(payload.get("action") or "run_all")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    stages = metadata.get("requested_stages") if isinstance(metadata, Mapping) else None
    try:
        budget = _effective_budget(payload, args)
    except (TypeError, ValueError) as exc:
        return {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": str(exc),
            "config_path": str(config_path),
        }
    preflight_args = [
        "-m",
        "reviewctl",
        "preflight",
        "--config",
        str(config_path),
        "--action",
        action,
    ]
    if stages:
        preflight_args.extend(["--stages", *[str(item) for item in stages]])
    preflight = _command(preflight_args, cwd=repo_root, timeout_seconds=budget["timeout_seconds"])
    result: dict[str, Any] = {
        "gate": PREFLIGHT_GATE,
        "status": "PASS" if preflight.get("status") == "PASS" and preflight.get("ok") else "BLOCKED_PREFLIGHT",
        "config_path": str(config_path),
        "action": action,
        "requested_stages": list(stages or ()),
        "budget": budget,
        "preflight": preflight,
    }
    if result["status"] != "PASS" and _contains_credential_blocker(preflight):
        result["status"] = "BLOCKED_CREDENTIALS"
    return result


def _live_gate(
    repo_root: Path,
    *,
    gate: str,
    spec: Path | None,
    args: argparse.Namespace,
    preflight: dict[str, Any] | None,
) -> dict[str, Any]:
    if preflight is None or preflight.get("status") != "PASS":
        return {
            "gate": gate,
            "status": "BLOCKED_PREREQUISITE",
            "reason": "Gate B exact-path preflight did not pass",
            "preflight_status": preflight.get("status") if preflight else "NOT_RUN",
        }
    if spec is None:
        return {"gate": gate, "status": "BLOCKED_INPUT", "reason": "--spec is required"}
    if os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") != "1":
        return {
            "gate": gate,
            "status": "NOT_VERIFIED",
            "reason": "set AUTO_GENERATE_RUN_LIVE_ACCEPTANCE=1 for owner-authorized provider calls",
        }

    payload, error = _read_spec(spec)
    if error is not None or payload is None:
        result = dict(error or {"status": "BLOCKED_INPUT"})
        result["gate"] = gate
        return result
    try:
        budget = _effective_budget(payload, args)
    except (TypeError, ValueError) as exc:
        return {"gate": gate, "status": "BLOCKED_INPUT", "reason": str(exc)}

    formal_run = _command(
        ["-m", "reviewctl", "run", "--spec", str(spec)],
        cwd=repo_root,
        timeout_seconds=budget["timeout_seconds"],
    )
    run_status = formal_run.get("job_status")
    completion_status = formal_run.get("completion_status")
    status = (
        "PASS"
        if formal_run.get("status") == "PASS"
        and run_status == "completed"
        and completion_status == "complete"
        else "FAIL"
    )
    if status != "PASS" and _contains_credential_blocker(formal_run):
        status = "BLOCKED_CREDENTIALS"
    return {
        "gate": gate,
        "status": status,
        "budget": budget,
        "formal_control_plane_run": formal_run,
        "evidence_note": "A completed reviewctl job is necessary but not sufficient for specialized gate evidence; inspect Registry closure and stage artifacts before release.",
    }


def _post_live_gate(gate: str, *, repo_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if gate == "S":
        tracked = _command(
            ["ls-files", ".env"],
            cwd=repo_root,
            timeout_seconds=args.timeout_seconds,
            executable="git",
        )
        return {
            "gate": gate,
            "status": "PASS" if tracked.get("status") == "PASS" and not tracked.get("stdout_tail") else "FAIL",
            "tracked_env_check": tracked,
        }
    return {
        "gate": gate,
        "status": "NOT_VERIFIED",
        "reason": "requires the corresponding live/UI/GitHub evidence and is not inferred from offline tests",
    }


def _run_requested_gate(
    gate: str,
    *,
    repo_root: Path,
    spec: Path | None,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    if gate == OFFLINE_GATE:
        return [_offline_gate(repo_root, timeout_seconds=args.timeout_seconds)]
    preflight = _preflight_gate(repo_root, spec=spec, args=args)
    if gate == PREFLIGHT_GATE:
        return [preflight]
    if gate in LIVE_GATES:
        return [preflight, _live_gate(repo_root, gate=gate, spec=spec, args=args, preflight=preflight)]
    if gate in POST_LIVE_GATES:
        return [preflight, _post_live_gate(gate, repo_root=repo_root, args=args)]
    raise ValueError(f"unsupported gate: {gate}")


def _run_all(*, repo_root: Path, spec: Path | None, args: argparse.Namespace) -> list[dict[str, Any]]:
    results = [_offline_gate(repo_root, timeout_seconds=args.timeout_seconds)]
    if results[-1].get("status") != "PASS":
        return results
    preflight = _preflight_gate(repo_root, spec=spec, args=args)
    results.append(preflight)
    if preflight.get("status") != "PASS":
        return results
    for gate in LIVE_GATES:
        result = _live_gate(repo_root, gate=gate, spec=spec, args=args, preflight=preflight)
        results.append(result)
        if result.get("status") != "PASS":
            return results
    results.extend(_post_live_gate(gate, repo_root=repo_root, args=args) for gate in POST_LIVE_GATES)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--gate", choices=(*ALL_GATES, "all"), default=OFFLINE_GATE)
    parser.add_argument("--spec", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-provider-calls", type=int, default=DEFAULT_PROVIDER_CALL_CEILING)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_OUTPUT_TOKEN_CEILING)
    parser.add_argument("--max-retry-attempts", type=int, default=DEFAULT_RETRY_CEILING)
    args = parser.parse_args(argv)
    if any(
        value <= 0
        for value in (
            args.timeout_seconds,
            args.max_provider_calls,
            args.max_output_tokens,
            args.max_retry_attempts,
        )
    ):
        parser.error("all budgets and timeout must be positive")
    repo_root = Path(args.repo_root).expanduser().resolve()
    spec = args.spec.expanduser().resolve() if args.spec is not None else None
    if args.gate == "all":
        results = _run_all(repo_root=repo_root, spec=spec, args=args)
    else:
        results = _run_requested_gate(args.gate, repo_root=repo_root, spec=spec, args=args)
    payload = {"entrypoint": "scripts/release_acceptance.py", "results": results}
    print(json.dumps(_redact_payload(payload), ensure_ascii=False, sort_keys=True))
    return 0 if all(item.get("status") == "PASS" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

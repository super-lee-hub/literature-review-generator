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

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))

from runtime.job_spec import RuntimeJobSpec
from runtime.release_acceptance import (
    GATE_CONTRACTS,
    ReleaseAcceptanceBudget,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
    gate_contract,
    validate_gate_evidence,
)


OFFLINE_GATE = "A"
PREFLIGHT_GATE = "B"
LIVE_GATES = ("C", "D", "E", "F", "G", "H", "I", "J", "K", "Q")
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
            env=({**os.environ, **dict(env)} if env is not None else None),
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
            return {"gate": OFFLINE_GATE, "status": "FAIL", "results": results, "contract": gate_contract(OFFLINE_GATE)}
    return {"gate": OFFLINE_GATE, "status": "PASS", "results": results, "contract": gate_contract(OFFLINE_GATE)}


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
    try:
        RuntimeJobSpec.from_dict(payload).validate()
    except (TypeError, ValueError) as exc:
        return None, {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": f"runtime spec is invalid: {type(exc).__name__}: {exc}",
        }
    return payload, None


def _spec_config_path(spec: Path, payload: Mapping[str, Any]) -> Path:
    raw_config = str(payload.get("config") or "config.ini")
    config_path = Path(raw_config).expanduser()
    if not config_path.is_absolute():
        config_path = (spec.parent / config_path).resolve()
    return config_path


def _command_budget(args: argparse.Namespace) -> ReleaseAcceptanceBudget:
    return ReleaseAcceptanceBudget(
        max_provider_calls_total=int(
            getattr(args, "max_provider_calls_total", getattr(args, "max_provider_calls", DEFAULT_PROVIDER_CALL_CEILING))
        ),
        max_output_tokens_total=int(
            getattr(args, "max_output_tokens_total", getattr(args, "max_output_tokens", DEFAULT_OUTPUT_TOKEN_CEILING))
        ),
        max_retry_attempts_total=int(
            getattr(args, "max_retry_attempts_total", getattr(args, "max_retry_attempts", DEFAULT_RETRY_CEILING))
        ),
        max_wall_seconds=int(getattr(args, "max_wall_seconds", getattr(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))),
    )


def _effective_budget(
    payload: Mapping[str, Any],
    args: argparse.Namespace,
    acceptance_spec: ReleaseAcceptanceSpec | None = None,
) -> dict[str, int]:
    del payload
    command_budget = _command_budget(args)
    budget = acceptance_spec.budget if acceptance_spec is not None else command_budget
    ceilings = command_budget.to_dict()
    for name, value in budget.to_dict().items():
        if value > ceilings[name]:
            raise ValueError(f"acceptance budget {name} exceeds the command ceiling")
    return budget.to_dict()


def _budget_environment(budget: Mapping[str, Any]) -> dict[str, str]:
    return {
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON": json.dumps(
            dict(budget), ensure_ascii=False, sort_keys=True
        )
    }


def _current_sha(repo_root: Path) -> str:
    result = _command(["rev-parse", "HEAD"], cwd=repo_root, timeout_seconds=30, executable="git")
    if result.get("status") != "PASS":
        return ""
    return str(result.get("stdout_tail") or "").strip().splitlines()[-1] if result.get("stdout_tail") else ""


def _load_acceptance_spec(
    path: Path | None,
    *,
    repo_root: Path,
    args: argparse.Namespace,
) -> tuple[ReleaseAcceptanceSpec | None, dict[str, Any] | None]:
    if path is None:
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ReleaseAcceptanceSpec.from_mapping(
            payload,
            origin_dir=path.parent,
            defaults=_command_budget(args),
        ), None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ReleaseAcceptanceSpecError) as exc:
        return None, {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": f"acceptance spec is invalid: {type(exc).__name__}: {exc}",
        }


def _gate_evidence(
    acceptance_spec: ReleaseAcceptanceSpec | None,
    gate: str,
) -> Mapping[str, Any] | None:
    if acceptance_spec is None or not acceptance_spec.evidence_manifest:
        return None
    try:
        payload = json.loads(Path(acceptance_spec.evidence_manifest).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    gates = payload.get("gates")
    if not isinstance(gates, Mapping):
        return None
    value = gates.get(gate)
    return value if isinstance(value, Mapping) else None


def _preflight_gate(
    repo_root: Path,
    *,
    spec: Path | None,
    args: argparse.Namespace,
    acceptance_spec: ReleaseAcceptanceSpec | None = None,
) -> dict[str, Any]:
    if spec is None:
        return {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": "--spec is required for exact-path provider preflight",
            "contract": gate_contract(PREFLIGHT_GATE),
        }
    payload, error = _read_spec(spec)
    if error is not None or payload is None:
        result: dict[str, Any] = dict(
            error or {"gate": PREFLIGHT_GATE, "status": "BLOCKED_INPUT"}
        )
        result.setdefault("contract", gate_contract(PREFLIGHT_GATE))
        return result
    config_path = _spec_config_path(spec, payload)
    action = str(payload.get("action") or "run_all")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    stages = metadata.get("requested_stages") if isinstance(metadata, Mapping) else None
    try:
        budget = _effective_budget(payload, args, acceptance_spec)
    except (TypeError, ValueError) as exc:
        return {
            "gate": PREFLIGHT_GATE,
            "status": "BLOCKED_INPUT",
            "reason": str(exc),
            "config_path": str(config_path),
            "contract": gate_contract(PREFLIGHT_GATE),
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
    preflight = _command(preflight_args, cwd=repo_root, timeout_seconds=budget["max_wall_seconds"])
    result: dict[str, Any] = {
        "gate": PREFLIGHT_GATE,
        "status": "PASS" if preflight.get("status") == "PASS" and preflight.get("ok") else "BLOCKED_PREFLIGHT",
        "kind": "dry_transport_preflight",
        "config_path": str(config_path),
        "action": action,
        "requested_stages": list(stages or ()),
        "budget": budget,
        "preflight": preflight,
        "network_calls": 0,
    }
    if (
        preflight.get("status") == "PASS"
        and os.getenv("AUTO_GENERATE_RUN_LIVE_MICRO_PROBE", "0") == "1"
    ):
        probe_args = [
            "-m",
            "reviewctl",
            "micro-probe",
            "--config",
            str(config_path),
            "--action",
            action,
        ]
        if stages:
            probe_args.extend(["--stages", *[str(item) for item in stages]])
        if acceptance_spec is not None and acceptance_spec.third_party_acknowledged:
            probe_args.append("--third-party-acknowledged")
            for host in acceptance_spec.third_party_hosts:
                probe_args.extend(["--third-party-host", host])
        live_probe = _command(
            probe_args,
            cwd=repo_root,
            timeout_seconds=budget["max_wall_seconds"],
            env=_budget_environment(budget),
        )
        result["live_route_micro_probe"] = live_probe
        result["network_calls"] = int(live_probe.get("network_calls") or 0)
        if live_probe.get("status") != "PASS" or not live_probe.get("ok"):
            result["status"] = "BLOCKED_MICRO_PROBE" if _contains_credential_blocker(live_probe) else "FAIL"
    elif os.getenv("AUTO_GENERATE_RUN_LIVE_MICRO_PROBE", "0") != "1":
        result["live_route_micro_probe"] = {
            "status": "NOT_RUN",
            "reason": "dry transport preflight does not make HTTP calls; set AUTO_GENERATE_RUN_LIVE_MICRO_PROBE=1 for the explicit micro-probe",
            "network_calls": 0,
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
    acceptance_spec: ReleaseAcceptanceSpec | None = None,
) -> dict[str, Any]:
    contract = gate_contract(gate)
    if preflight is None or preflight.get("status") != "PASS":
        return {
            "gate": gate,
            "status": "BLOCKED_PREREQUISITE",
            "reason": "Gate B exact-path preflight did not pass",
            "preflight_status": preflight.get("status") if preflight else "NOT_RUN",
            "contract": contract,
        }
    if spec is None:
        return {"gate": gate, "status": "BLOCKED_INPUT", "reason": "--spec is required"}
    if os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") != "1":
        return {
            "gate": gate,
            "status": "NOT_VERIFIED",
            "reason": "set AUTO_GENERATE_RUN_LIVE_ACCEPTANCE=1 for owner-authorized provider calls",
            "contract": contract,
        }
    try:
        payload, error = _read_spec(spec)
        if error is not None or payload is None:
            result: dict[str, Any] = dict(error or {"status": "BLOCKED_INPUT"})
            result["gate"] = gate
            result["contract"] = contract
            return result
        budget = _effective_budget(payload, args, acceptance_spec)
    except (TypeError, ValueError) as exc:
        return {"gate": gate, "status": "BLOCKED_INPUT", "reason": str(exc), "contract": contract}

    # A generic completed job is intentionally not executed or accepted as
    # evidence for a specialized gate. The owner runs the gate-specific action
    # and supplies its final-SHA-bound manifest through --acceptance-spec.
    evidence = _gate_evidence(acceptance_spec, gate)
    if evidence is None:
        return {
            "gate": gate,
            "status": "NOT_VERIFIED",
            "reason": "gate-specific final-SHA-bound evidence is required; job completion is not sufficient",
            "budget": budget,
            "action_required": contract["actual_action"],
            "contract": contract,
        }
    validation = validate_gate_evidence(
        gate,
        evidence,
        expected_final_sha=_current_sha(repo_root),
    )
    return {
        "gate": gate,
        "status": validation["status"],
        "budget": budget,
        "contract": contract,
        "evidence_validation": validation,
    }


def _post_live_gate(gate: str, *, repo_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    contract = gate_contract(gate)
    if gate == "S":
        tracked = _command(
            ["ls-files", ".env"],
            cwd=repo_root,
            timeout_seconds=int(
                getattr(args, "max_wall_seconds", getattr(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
            ),
            executable="git",
        )
        return {
            "gate": gate,
            "status": "NOT_VERIFIED",
            "reason": "tracked .env absence is only one privacy check; a complete release scan evidence artifact is required",
            "contract": contract,
            "tracked_env_check": tracked,
        }
    return {
        "gate": gate,
        "status": "NOT_VERIFIED",
        "reason": "requires the corresponding live/UI/GitHub evidence and is not inferred from offline tests",
        "contract": contract,
    }


def _run_requested_gate(
    gate: str,
    *,
    repo_root: Path,
    spec: Path | None,
    args: argparse.Namespace,
    acceptance_spec: ReleaseAcceptanceSpec | None = None,
) -> list[dict[str, Any]]:
    if gate == OFFLINE_GATE:
        return [
            _offline_gate(
                repo_root,
                timeout_seconds=int(
                    getattr(args, "max_wall_seconds", getattr(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
                ),
            )
        ]
    preflight = _preflight_gate(
        repo_root,
        spec=spec,
        args=args,
        acceptance_spec=acceptance_spec,
    )
    if gate == PREFLIGHT_GATE:
        return [preflight]
    if gate in LIVE_GATES:
        return [
            preflight,
            _live_gate(
                repo_root,
                gate=gate,
                spec=spec,
                args=args,
                preflight=preflight,
                acceptance_spec=acceptance_spec,
            ),
        ]
    if gate in POST_LIVE_GATES:
        return [preflight, _post_live_gate(gate, repo_root=repo_root, args=args)]
    raise ValueError(f"unsupported gate: {gate}")


def _run_all(*, repo_root: Path, spec: Path | None, args: argparse.Namespace) -> list[dict[str, Any]]:
    acceptance_spec: ReleaseAcceptanceSpec | None = getattr(args, "acceptance_spec", None)
    results = [
        _offline_gate(
            repo_root,
            timeout_seconds=int(
                getattr(args, "max_wall_seconds", getattr(args, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
            ),
        )
    ]
    if results[-1].get("status") != "PASS":
        return results
    preflight = _preflight_gate(
        repo_root,
        spec=spec,
        args=args,
        acceptance_spec=acceptance_spec,
    )
    results.append(preflight)
    for gate in LIVE_GATES:
        results.append(
            _live_gate(
                repo_root,
                gate=gate,
                spec=spec,
                args=args,
                preflight=preflight,
                acceptance_spec=acceptance_spec,
            )
        )
    results.extend(_post_live_gate(gate, repo_root=repo_root, args=args) for gate in POST_LIVE_GATES)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--gate", choices=(*ALL_GATES, "all"), default=OFFLINE_GATE)
    parser.add_argument("--spec", type=Path, default=None)
    parser.add_argument("--acceptance-spec", type=Path, default=None)
    parser.add_argument(
        "--max-wall-seconds",
        "--timeout-seconds",
        dest="max_wall_seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-provider-calls-total",
        "--max-provider-calls",
        dest="max_provider_calls_total",
        type=int,
        default=DEFAULT_PROVIDER_CALL_CEILING,
    )
    parser.add_argument(
        "--max-output-tokens-total",
        "--max-output-tokens",
        dest="max_output_tokens_total",
        type=int,
        default=DEFAULT_OUTPUT_TOKEN_CEILING,
    )
    parser.add_argument(
        "--max-retry-attempts-total",
        "--max-retry-attempts",
        dest="max_retry_attempts_total",
        type=int,
        default=DEFAULT_RETRY_CEILING,
    )
    args = parser.parse_args(argv)
    try:
        command_budget = _command_budget(args)
    except (TypeError, ValueError, ReleaseAcceptanceSpecError) as exc:
        parser.error(str(exc))
    repo_root = Path(args.repo_root).expanduser().resolve()
    spec = args.spec.expanduser().resolve() if args.spec is not None else None
    acceptance_spec, acceptance_error = _load_acceptance_spec(
        args.acceptance_spec.expanduser().resolve() if args.acceptance_spec is not None else None,
        repo_root=repo_root,
        args=args,
    )
    args.acceptance_spec = acceptance_spec
    if acceptance_error is not None:
        results = [acceptance_error]
    elif args.gate == "all":
        results = _run_all(repo_root=repo_root, spec=spec, args=args)
    else:
        results = _run_requested_gate(
            args.gate,
            repo_root=repo_root,
            spec=spec,
            args=args,
            acceptance_spec=acceptance_spec,
        )
    payload = {
        "entrypoint": "scripts/release_acceptance.py",
        "acceptance_budget": command_budget.to_dict(),
        "gate_contracts": GATE_CONTRACTS,
        "results": results,
    }
    print(json.dumps(_redact_payload(payload), ensure_ascii=False, sort_keys=True))
    return 0 if all(item.get("status") == "PASS" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Fresh-process zero-provider control for an all-imported Stage 1 run.

The helper is called after the existing 15-paper positive has produced and
verified all typed manifests.  Its child process deliberately imports no
pytest/conftest surface, clears inherited acceptance/scale environment, and
audits socket use while exercising the public ControlPlane plan/run path.
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from typing import Any, Sequence

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec


_ORDINARY_CHILD = r'''
from __future__ import annotations

import inspect
import importlib
import json
import os
from pathlib import Path
import socket
import sys


def _clear_inherited_context_environment() -> None:
    for name in tuple(os.environ):
        if name.startswith("AUTO_GENERATE_ACCEPTANCE_") or name.startswith("RUN_FIRST_SCALE_"):
            os.environ.pop(name, None)


_clear_inherited_context_environment()

from runtime.control_plane import ReviewControlPlane
from runtime.explicit_stage1_scope import load_explicit_stage1_scope
from runtime.job_spec import load_runtime_job_spec
from runtime.provider_runtime import (
    acceptance_execution_context_from_environment,
    current_acceptance_execution_context,
)
from runtime.runner import AgentRuntimeRunner
from runtime.test_dependencies import current_runtime_test_dependencies
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from services.artifact_registry import ArtifactRegistry
from validation.closure import resolve_current_stage_closure_map


_SOCKET_EVENTS: list[dict[str, object]] = []


def _is_urllib3_ipv6_probe(address: object) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    if str(address[0]) != "::1" or int(address[1]) != 0:
        return False
    return any("urllib3" in str(frame.filename).casefold() for frame in inspect.stack())


def _guard_socket_surface() -> None:
    original_socket = socket.socket
    original_getaddrinfo = socket.getaddrinfo

    class GuardedSocket(original_socket):
        def connect(self, address: object) -> object:
            _SOCKET_EVENTS.append({"operation": "connect", "address": repr(address)})
            raise AssertionError(f"unexpected socket.connect: {address!r}")

        def bind(self, address: object) -> object:
            if _is_urllib3_ipv6_probe(address):
                return super().bind(address)
            _SOCKET_EVENTS.append({"operation": "bind", "address": repr(address)})
            raise AssertionError(f"unexpected socket.bind: {address!r}")

    def guarded_getaddrinfo(*args: object, **kwargs: object) -> object:
        host = args[0] if args else kwargs.get("host")
        port = args[1] if len(args) > 1 else kwargs.get("port")
        if str(host) == "::1" and int(port or 0) == 0 and any(
            "urllib3" in str(frame.filename).casefold() for frame in inspect.stack()
        ):
            return original_getaddrinfo(*args, **kwargs)
        _SOCKET_EVENTS.append({"operation": "getaddrinfo", "host": repr(host), "port": repr(port)})
        raise AssertionError(f"unexpected socket.getaddrinfo: {host!r}:{port!r}")

    socket.socket = GuardedSocket
    socket.getaddrinfo = guarded_getaddrinfo


def _read_budget_state(workspace_path: str) -> dict[str, object]:
    path = Path(workspace_path) / "logs" / "acceptance_budget_state_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("ordinary child budget state is not an object")
    return {
        "budget": payload.get("budget"),
        "calls_used": payload.get("calls_used"),
        "calls_reserved": payload.get("calls_reserved"),
        "output_tokens_used": payload.get("output_tokens_used"),
        "output_tokens_reserved": payload.get("output_tokens_reserved"),
        "retry_attempts_used": payload.get("retry_attempts_used"),
        "retry_attempts_reserved": payload.get("retry_attempts_reserved"),
        "absolute_deadline_epoch": payload.get("absolute_deadline_epoch"),
        "reservations": payload.get("reservations"),
    }


def _report_scope(scope: object) -> dict[str, object]:
    import_plan = scope.import_plan
    routes = [
        route.to_dict() if hasattr(route, "to_dict") else dict(route)
        for route in scope.provider_route_plan.routes
    ]
    return {
        "full_source_count": len(scope.full_bundle.paper_work_items),
        "imported_count": len(import_plan.imports),
        "pending_count": len(import_plan.pending_keys),
        "pending_keys": list(import_plan.pending_keys),
        "remote_preprocess_required": bool(scope.remote_preprocess_required),
        "effective_requested_stages": list(scope.stage_plan.requested_stages),
        "provider_routes": routes,
        "provider_route_count": len(routes),
    }


def main() -> int:
    spec_path = Path(sys.argv[1]).expanduser().resolve()
    source_root = Path(sys.argv[2]).expanduser().resolve()
    report_path = Path(sys.argv[3]).expanduser().resolve()
    dependencies = current_runtime_test_dependencies()
    if dependencies is not None:
        raise AssertionError("ordinary child inherited RuntimeTestDependencies")
    if current_acceptance_execution_context() is not None:
        raise AssertionError("ordinary child inherited an in-process acceptance context")
    if acceptance_execution_context_from_environment() is not None:
        raise AssertionError("ordinary child inherited an acceptance environment context")
    if any(
        name.startswith("RUN_FIRST_SCALE_") or name.startswith("AUTO_GENERATE_ACCEPTANCE_")
        for name in os.environ
    ):
        raise AssertionError("ordinary child retained a scale/acceptance environment variable")

    _guard_socket_surface()
    spec = load_runtime_job_spec(spec_path)
    scope = load_explicit_stage1_scope(spec, allow_template_credentials=False)
    scope_report = _report_scope(scope)
    if scope_report["full_source_count"] != 15 or scope_report["imported_count"] != 15:
        raise AssertionError(f"ordinary child explicit scope is not 15/15: {scope_report}")
    if scope_report["pending_count"] != 0 or scope_report["remote_preprocess_required"]:
        raise AssertionError(f"ordinary child explicit scope still has pending work: {scope_report}")
    if "analyze" in scope_report["effective_requested_stages"]:
        raise AssertionError(f"ordinary child scope did not suppress analyze: {scope_report}")
    if scope_report["provider_route_count"] != 0:
        raise AssertionError(f"ordinary child has reachable provider routes: {scope_report}")

    control = ReviewControlPlane(repo_root=source_root)
    plan = control.plan(spec_path)
    full_plan = plan.get("full_stage_request_plan") or {}
    explicit_inputs = full_plan.get("explicit_stage1_inputs") or {}
    provider_requests = full_plan.get("provider_requests") or []
    if explicit_inputs != {"full_source_count": 15, "imported_count": 15, "pending_count": 0}:
        raise AssertionError(f"ordinary child plan explicit inputs are wrong: {explicit_inputs}")
    if provider_requests:
        raise AssertionError(f"ordinary child plan contains provider requests: {provider_requests}")
    if full_plan.get("stage1_reuse_authority_status") != (
        "verified_explicit_typed_inputs_with_current_pdf_joins"
    ):
        raise AssertionError(f"ordinary child plan did not record explicit suppression: {full_plan}")

    preflight = control.provider_preflight(runtime_spec=spec)
    if preflight.get("ok") is not True:
        raise AssertionError(f"ordinary child provider preflight failed: {preflight}")
    if preflight.get("provider_roles") != []:
        raise AssertionError(f"ordinary child preflight selected provider roles: {preflight}")
    if preflight.get("network_calls") != 0:
        raise AssertionError(f"ordinary child preflight reported network calls: {preflight}")
    preflight_routes = (preflight.get("route_plan") or {}).get("routes") or []
    if any(str(route.get("stage") or "") == "analyze" for route in preflight_routes if isinstance(route, dict)):
        raise AssertionError(f"ordinary child preflight retained Analyze route: {preflight}")
    mineru_admission = preflight.get("mineru_remote_admission") or {}
    if mineru_admission.get("status") != "not_required" or mineru_admission.get("network_probe") is not False:
        raise AssertionError(f"ordinary child unexpectedly admitted MinerU: {preflight}")

    run = control.run(spec_path)
    if (
        run.get("job_status") != "completed"
        or run.get("completion_status") != "complete"
        or run.get("canonical_ready") is not True
        or run.get("success") is not True
    ):
        raise AssertionError(f"ordinary child run was not complete: {run}")
    inspection = control.inspect(workspace=run["workspace_path"])
    receipts = inspection.get("provider_receipts") or {}
    if int(receipts.get("count") or 0) != 0 or receipts.get("entries"):
        raise AssertionError(f"ordinary child observed provider receipts: {receipts}")
    workspace, registry = AgentRuntimeRunner._open_workspace(run["workspace_path"])
    envelope_record = registry.get("provider_run_envelope")
    if envelope_record is None or envelope_record.status != "ready":
        raise AssertionError("ordinary child provider run envelope is missing")
    ArtifactRegistry._verify_ready_artifact(envelope_record)
    raw_envelope = json.loads(Path(envelope_record.path).read_text(encoding="utf-8"))
    while isinstance(raw_envelope, dict) and isinstance(raw_envelope.get("payload"), dict):
        raw_envelope = raw_envelope["payload"]
    envelope = ProviderRunEnvelopeV1.from_dict(raw_envelope)
    computed_scope_hashes = control._provider_run_scope_hashes_for_spec(spec)
    if dict(envelope.scope_hashes) != dict(computed_scope_hashes):
        raise AssertionError(
            "ordinary child provider envelope scope hashes differ from the public recomputation: "
            + json.dumps(
                {"envelope": dict(envelope.scope_hashes), "computed": computed_scope_hashes},
                ensure_ascii=False,
            )
        )
    closure_map = resolve_current_stage_closure_map(registry).to_dict()
    analyze_closure = (closure_map.get("provider_closures_by_stage") or {}).get("analyze") or {}
    if analyze_closure.get("expected_call_ids") or analyze_closure.get("observed_call_ids"):
        raise AssertionError(f"ordinary child analyze closure is nonempty: {analyze_closure}")
    budget = _read_budget_state(run["workspace_path"])
    expected_budget = spec.metadata["provider_budget"]
    if budget["budget"] != expected_budget:
        raise AssertionError(f"ordinary child budget changed: {budget}")
    for field in ("calls_used", "calls_reserved", "output_tokens_used", "output_tokens_reserved", "retry_attempts_used", "retry_attempts_reserved"):
        if budget[field] != 0:
            raise AssertionError(f"ordinary child budget field {field} is nonzero: {budget}")
    if budget["reservations"] != []:
        raise AssertionError(f"ordinary child retained reservations: {budget}")

    module_paths = {
        "explicit_scope": str(Path(importlib.import_module("runtime.explicit_stage1_scope").__file__).resolve()),
        "explicit_import": str(Path(importlib.import_module("services.explicit_stage1_typed_import").__file__).resolve()),
        "control_plane": str(Path(importlib.import_module("runtime.control_plane").__file__).resolve()),
        "runner": str(Path(importlib.import_module("runtime.runner").__file__).resolve()),
    }
    if any(not Path(path).is_relative_to(source_root) for path in module_paths.values()):
        raise AssertionError(f"ordinary child loaded runtime modules outside source root: {module_paths}")

    report = {
        "schema_version": "explicit-typed-import-ordinary-process/v1",
        "runtime_test_dependencies_none": True,
        "acceptance_context_none": True,
        "scale_acceptance_environment_clear": True,
        "scope": scope_report,
        "plan": {
            "read_only": plan.get("read_only"),
            "provider_requests": len(provider_requests),
            "explicit_stage1_inputs": explicit_inputs,
            "stage1_reuse_authority_status": full_plan.get("stage1_reuse_authority_status"),
        },
        "provider_preflight": {
            "ok": preflight.get("ok"),
            "provider_roles": list(preflight.get("provider_roles") or ()),
            "network_calls": preflight.get("network_calls"),
            "mineru_status": mineru_admission.get("status"),
            "analyze_routes": sum(
                1
                for route in preflight_routes
                if isinstance(route, dict) and str(route.get("stage") or "") == "analyze"
            ),
        },
        "run": {
            "job_status": run.get("job_status"),
            "completion_status": run.get("completion_status"),
            "canonical_ready": run.get("canonical_ready"),
            "success": run.get("success"),
            "completed_stages": list(run.get("completed_stages") or ()),
        },
        "provider_receipts": {"count": int(receipts.get("count") or 0)},
        "analyze_closure": {
            "expected_count": len(analyze_closure.get("expected_call_ids") or ()),
            "observed_count": len(analyze_closure.get("observed_call_ids") or ()),
        },
        "provider_envelope": {
            "admission_status": envelope.admission_status,
            "budget_authority": envelope.budget_authority,
            "scope_hashes": dict(envelope.scope_hashes),
        },
        "budget": {
            "calls_used": budget["calls_used"],
            "calls_reserved": budget["calls_reserved"],
            "output_tokens_used": budget["output_tokens_used"],
            "output_tokens_reserved": budget["output_tokens_reserved"],
            "retry_attempts_used": budget["retry_attempts_used"],
            "retry_attempts_reserved": budget["retry_attempts_reserved"],
            "absolute_deadline_epoch": budget["absolute_deadline_epoch"],
        },
        "socket_events": list(_SOCKET_EVENTS),
        "module_paths": module_paths,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        failure = {
            "schema_version": "explicit-typed-import-ordinary-process/v1",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "socket_events": list(_SOCKET_EVENTS),
        }
        try:
            Path(sys.argv[3]).write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        except BaseException:
            pass
        print(json.dumps(failure, ensure_ascii=False, separators=(",", ":")))
        raise
'''


def run_ordinary_fresh_process_all_import_zero(
    *,
    tmp_path: Path,
    source_root: Path,
    pdf_folder: Path,
    manifest_paths: Sequence[str],
    current_config: Path,
    project_name: str,
    job_id: str,
) -> dict[str, Any]:
    """Run the all-imported control in a child without pytest dependencies."""

    if len(manifest_paths) != 15:
        raise AssertionError(f"ordinary all-import control requires 15 manifests: {len(manifest_paths)}")
    ordinary_root = tmp_path / "ordinary-fresh-process"
    ordinary_root.mkdir(parents=True, exist_ok=True)
    ordinary_config = ordinary_root / "ordinary-no-reader.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(current_config, encoding="utf-8")
    parser["Paths"]["output_path"] = str(ordinary_root / "output")
    parser["Primary_Reader_API"]["api_key"] = ""
    parser["Primary_Reader_API"]["api_base"] = "https://unapproved-reader.invalid/v1"
    with ordinary_config.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    budget = {
        "schema_version": "provider-aggregate-budget-contract-v2",
        "max_provider_calls_total": 0,
        "max_output_tokens_total": 1_000_000,
        "max_retry_attempts_total": 0,
        "max_wall_seconds": 900,
    }
    spec = RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        job_id=job_id,
        config=str(ordinary_config),
        action="analyze",
        reuse_stage1=True,
        reuse_summary_files=tuple(manifest_paths),
        queue_file=str(ordinary_root / "queue.json"),
        metadata={
            "stage1_summary_input_mode": "explicit_typed_import",
            "provider_budget": budget,
        },
    )
    spec.validate()
    spec_path = ordinary_root / "runtime-job-spec.json"
    spec_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    child_path = ordinary_root / "ordinary_child.py"
    child_path.write_text(textwrap.dedent(_ORDINARY_CHILD), encoding="utf-8")
    report_path = ordinary_root / "ordinary_child_report.json"
    child_env = dict(os.environ)
    for name in tuple(child_env):
        if name.startswith("AUTO_GENERATE_ACCEPTANCE_") or name.startswith("RUN_FIRST_SCALE_"):
            child_env.pop(name, None)
        elif (
            name.endswith("_API_KEY")
            or name.endswith("_API_TOKEN")
            or name.startswith("LLM_")
            or name.startswith("MINERU_")
        ):
            child_env.pop(name, None)
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(source_root), child_env.get("PYTHONPATH", "")) if item
    )
    completed = subprocess.run(
        [sys.executable, str(child_path), str(spec_path), str(source_root), str(report_path)],
        cwd=str(source_root),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    if completed.returncode != 0:
        raise AssertionError(
            "ordinary fresh-process control failed: "
            + json.dumps(
                {
                    "returncode": completed.returncode,
                    "report": report,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                },
                ensure_ascii=False,
            )
        )
    return report


__all__ = ["run_ordinary_fresh_process_all_import_zero"]

"""Provider-free Agent control plane for the literature-review runtime.

The control plane is deliberately thin.  It resolves an existing workspace,
projects the canonical runner status, and exposes safe next actions without
inventing a second completion contract.  Commands which require an unavailable
validation/repair transaction return an explicit
blocked result instead of mutating canonical artifacts or pretending that the
operation completed.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from typing import Any, Mapping, Sequence, cast

from config_loader import load_config
from config_validator import validate_all_config
from ai_interface import build_provider_transport_preflight, classify_provider_endpoint
from services.credential_provenance import is_template_credential, provenance_payload
from models import APIConfig
from runtime.job_spec import RuntimeJobSpec, load_runtime_job_spec
from runtime.outline_v3_dag import OutlineNodeStore
from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.orchestrator import AgentRuntimeBridge
from runtime.runner import AgentRuntimeRunner, RuntimeExecutionResult, RuntimeRunnerError
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeLedger,
    bind_acceptance_execution_context,
    current_acceptance_execution_context,
)
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.stage_terminal import StageTerminalStore
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.model_capabilities import resolve_model_capability
from services.settings import ApplicationSettings
from services.stage1_output_budget import stage1_output_budget_sequence, provider_output_token_limit
from preprocess.service import DEFAULT_MINERU_ALLOWED_URL_HOSTS, PreprocessManager
from services.job_workspace import JobWorkspace, atomic_write_json
from services.durable_io import interprocess_file_lock
from runtime.cancellation import CancellationRequestStore
from runtime.export_bundle import ExportBundleService, ExportBundleSpecV1, ForensicAttestationService
from outline.adoption_transaction import OutlineAdoptionTransaction
from validation.closure import ValidationClosureService
from validation.repair_transaction import RepairTransactionService


CONTROL_PLANE_VERSION = "reviewctl-v1"
FORBIDDEN_ACTIONS = (
    "edit_registry",
    "edit_stage_health",
    "rerun_completed_candidates",
    "disable_quality_gate",
    "delete_workspace",
)
_API_SECTIONS = (
    "Primary_Reader_API",
    "Backup_Reader_API",
    "Writer_API",
    "Outline_API",
    "Validator_API",
)
_KNOWN_WORKSPACE_CONTAINERS = ("output", "outputs", "workspace", "workspaces")
_REQUIRED_RUNTIME_MODULES = ("requests", "dotenv")
_OPTIONAL_TOKENIZER_MODULES = ("tiktoken", "tokenizers")


class ControlPlaneError(RuntimeError):
    """Raised when a control-plane request cannot be resolved safely."""


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"auto-generate\x00reviewctl\x00" + encoded).hexdigest()


def _record_payload(record: ArtifactRecord) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "artifact_role": record.artifact_role,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "path": record.path,
        "producer": record.producer,
        "job_id": record.job_id,
        "status": record.status,
        "content_hash": record.content_hash,
        "depends_on": [dependency.to_dict() for dependency in record.depends_on],
        "metadata": dict(record.metadata),
        "created_at": record.created_at,
    }


def _persisted_runtime_spec_path(workspace_path: str | Path, registry: ArtifactRegistry) -> Path:
    """Resolve the current runtime spec through Registry identity first."""

    record = registry.get("runtime_job_spec")
    if record is not None and record.status == "ready":
        return Path(record.path)
    return Path(workspace_path) / "artifacts" / "runtime_job_spec_v1.json"


def _json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _load_spec_path(path: str | Path) -> RuntimeJobSpec:
    """Load JSON specs and optional YAML specs without adding a dependency."""

    target = Path(path).expanduser().resolve()
    try:
        return load_runtime_job_spec(target)
    except (json.JSONDecodeError, UnicodeDecodeError) as json_error:
        if target.suffix.lower() not in {".yaml", ".yml"}:
            raise ControlPlaneError(f"spec is not valid JSON: {target}") from json_error
        try:
            yaml = importlib.import_module("yaml")
        except ImportError as exc:
            raise ControlPlaneError(
                "YAML specs require the optional PyYAML package; use JSON or install PyYAML"
            ) from exc
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ControlPlaneError(f"cannot read YAML spec: {target}") from exc
        if not isinstance(raw, Mapping):
            raise ControlPlaneError("spec root must be a JSON/YAML object")
        spec = RuntimeJobSpec.from_dict(raw).resolved_from(target.parent)
        spec.validate()
        return spec
    except (OSError, ValueError, TypeError) as exc:
        raise ControlPlaneError(f"cannot load runtime spec {target}: {exc}") from exc


class ReviewControlPlane:
    """Single machine-readable control surface over existing runtime services."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        workspace_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
        roots = list(workspace_roots or ())
        if not roots:
            roots.extend(
                [
                    Path.cwd(),
                    self.repo_root,
                    self.repo_root / "output",
                ]
            )
        self.workspace_roots = tuple(dict.fromkeys(Path(root).expanduser().resolve() for root in roots))

    @staticmethod
    def _workspace_from_path(path: str | Path) -> str:
        target = Path(path).expanduser().resolve()
        if not target.is_dir() or "__" not in target.name:
            raise ControlPlaneError(
                f"invalid workspace path {target}; expected <project>__<job_id> directory"
            )
        _project, job_id = target.name.rsplit("__", 1)
        if not _project or not job_id:
            raise ControlPlaneError(f"invalid workspace identity: {target.name}")
        return str(target)

    def resolve_workspace(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> str:
        if workspace:
            return self._workspace_from_path(workspace)
        if not job_id or not str(job_id).strip():
            raise ControlPlaneError("a job id or workspace path is required")
        candidate = Path(str(job_id)).expanduser()
        if candidate.exists():
            return self._workspace_from_path(candidate)

        wanted = str(job_id).strip()
        candidates: set[Path] = set()
        for root in self.workspace_roots:
            if not root.exists() or not root.is_dir():
                continue
            search_roots = [root]
            search_roots.extend(root / name for name in _KNOWN_WORKSPACE_CONTAINERS)
            for search_root in search_roots:
                if not search_root.is_dir():
                    continue
                try:
                    children = tuple(search_root.iterdir())
                except OSError:
                    continue
                for child in children:
                    if child.is_dir() and child.name.endswith(f"__{wanted}"):
                        candidates.add(child.resolve())
                for pointer in search_root.glob("*/_latest_job.json"):
                    payload = _json_object(pointer)
                    if payload and str(payload.get("job_id") or "") == wanted:
                        pointer_workspace = payload.get("workspace_path")
                        if pointer_workspace:
                            candidate_path = Path(str(pointer_workspace)).expanduser().resolve()
                            if candidate_path.is_dir():
                                candidates.add(candidate_path)

        if len(candidates) == 1:
            return self._workspace_from_path(next(iter(candidates)))
        if not candidates:
            raise ControlPlaneError(
                f"cannot resolve job {wanted!r}; pass --workspace with the canonical workspace path"
            )
        raise ControlPlaneError(
            f"job id {wanted!r} resolves to multiple workspaces; pass --workspace explicitly"
        )

    @staticmethod
    def _status_payload(result: RuntimeExecutionResult) -> dict[str, Any]:
        payload = asdict(result)
        payload["control_plane_version"] = CONTROL_PLANE_VERSION
        payload["canonical_ready"] = bool(result.canonical_ready)
        payload["success"] = bool(result.success)
        payload["SUMMARY_SCHEMA_READY"] = bool(result.summary_schema_ready)
        payload["VISUAL_QUALIFICATION_READY"] = bool(result.visual_qualification_ready)
        payload["STAGE1_AUTHORITY_READY"] = bool(result.stage1_authority_ready)
        payload["STAGE1_REUSE_ELIGIBLE"] = bool(result.stage1_reuse_eligible)
        return payload

    def status(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        resolved = self.resolve_workspace(job_id=job_id, workspace=workspace)
        try:
            result = AgentRuntimeRunner.status(resolved)
        except (OSError, ValueError, RegistryError, RuntimeRunnerError) as exc:
            raise ControlPlaneError(str(exc)) from exc
        payload = self._status_payload(result)
        payload["workspace_path"] = resolved
        return payload

    def inspect(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        """Return a read-only, hash-checked workspace projection."""

        resolved = self.resolve_workspace(job_id=job_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(resolved)
        status_payload: dict[str, Any]
        try:
            result = AgentRuntimeRunner.status(resolved)
            status_payload = self._status_payload(result)
        except (OSError, ValueError, RegistryError, RuntimeRunnerError) as exc:
            status_payload = {
                "job_id": workspace_obj.job_id,
                "workspace_path": resolved,
                "job_status": "unknown",
                "completion_status": "blocked",
                "canonical_ready": False,
                "requires_attention": True,
                "message": str(exc),
            }

        records = registry.list_records()
        integrity: list[dict[str, Any]] = []
        registry_issues: list[str] = []
        for record in records:
            if record.status != "ready":
                continue
            try:
                ArtifactRegistry._verify_ready_artifact(record)
            except (OSError, RegistryError, TypeError, ValueError) as exc:
                registry_issues.append(f"{record.artifact_id}: {exc}")
                integrity.append(
                    {
                        "artifact_id": record.artifact_id,
                        "status": "untrusted",
                        "path": record.path,
                        "expected_hash": record.content_hash,
                        "error": str(exc),
                    }
                )
            else:
                integrity.append(
                    {
                        "artifact_id": record.artifact_id,
                        "status": "verified",
                        "path": record.path,
                        "content_hash": record.content_hash,
                    }
                )

        terminals: list[dict[str, Any]] = []
        terminal_issues: list[str] = []
        try:
            for terminal, path in StageTerminalStore(workspace_obj, registry).load_records():
                terminals.append({"path": str(path), **terminal.to_dict()})
        except (OSError, ValueError, TypeError) as exc:
            terminal_issues.append(str(exc))

        receipts = self._read_provider_receipts(workspace_obj, records)
        outline_v3, outline_issues = self._read_outline_v3_state(workspace_obj, registry)
        issues = [*registry_issues, *terminal_issues, *outline_issues]
        if not Path(registry.registry_path).is_file():
            issues.append("artifact_registry.json is missing")
        return {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "job_id": workspace_obj.job_id,
            "workspace_path": resolved,
            "status": status_payload,
            "registry_revision": registry.revision,
            "artifacts": [_record_payload(record) for record in records],
            "integrity": integrity,
            "stage_terminals": terminals,
            "provider_receipts": receipts,
            "outline_v3": outline_v3,
            "issues": issues,
            "read_only": True,
            "canonical_evidence_hash": _canonical_hash(
                {
                    "status": status_payload,
                    "registry_revision": registry.revision,
                    "artifacts": [_record_payload(record) for record in records],
                    "integrity": integrity,
                    "stage_terminals": terminals,
                    "provider_receipts": receipts,
                    "outline_v3": outline_v3,
                }
            ),
        }

    @staticmethod
    def _read_outline_v3_state(
        workspace: JobWorkspace,
        registry: ArtifactRegistry,
    ) -> tuple[dict[str, Any], list[str]]:
        """Read the v3 DAG/replay projections without creating Registry locks."""

        issues: list[str] = []
        replay_store = ModelCallReplayStore(workspace)
        try:
            dag = OutlineNodeStore(workspace, registry).load()
        except (OSError, ValueError, TypeError) as exc:
            dag = None
            issues.append(f"outline_v3_node_dag: {exc}")
        try:
            replay_records = replay_store._read_records()
        except (OSError, ValueError, TypeError) as exc:
            replay_records = []
            issues.append(f"outline_v3_replay: {exc}")
        if dag is None:
            return {
                "available": False,
                "failed_node_ids": [],
                "completed_node_ids": [],
                "replay": {
                    "path": str(replay_store.path),
                    "count": len(replay_records),
                },
            }, issues
        return {
            "available": True,
            "dag": dag.to_dict(),
            "content_hash": dag.content_hash,
            "snapshot_sequence": dag.snapshot_sequence,
            "failed_node_ids": dag.failed_node_ids,
            "completed_node_ids": dag.completed_node_ids,
            "replay": {
                "path": str(replay_store.path),
                "count": len(replay_records),
            },
        }, issues

    @staticmethod
    def _read_provider_receipts(
        workspace: JobWorkspace,
        records: Sequence[ArtifactRecord],
    ) -> dict[str, Any]:
        candidates: list[Path] = []
        for record in records:
            if "receipt" in record.artifact_type.lower() or "receipt" in record.artifact_role.lower():
                candidates.append(Path(record.path))
        artifacts_dir = Path(workspace.paths.artifacts_dir)
        if artifacts_dir.is_dir():
            candidates.extend(artifacts_dir.glob("provider_receipts*.jsonl"))
            candidates.extend(artifacts_dir.glob("**/provider_receipts*.jsonl"))
        unique = tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
        entries: list[dict[str, Any]] = []
        malformed: list[str] = []
        for path in unique:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                malformed.append(f"{path}: {exc}")
                continue
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    malformed.append(f"{path}:{line_number}: invalid JSON")
                    continue
                if isinstance(payload, Mapping):
                    entries.append(dict(payload))
                else:
                    malformed.append(f"{path}:{line_number}: receipt must be an object")
        return {
            "paths": [str(path) for path in unique],
            "count": len(entries),
            "entries": entries,
            "malformed": malformed,
            "complete": bool(unique) and not malformed,
        }

    def next_action(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        status = inspection["status"]
        completion_status = str(status.get("completion_status") or "blocked")
        outline_v3 = inspection.get("outline_v3") or {}
        outline_failed = [str(item) for item in outline_v3.get("failed_node_ids") or () if str(item)]
        failed_node = outline_failed[0] if outline_failed else (str(status.get("failed_stage") or "") or None)
        integrity_issues = list(inspection.get("issues") or [])
        provider_receipts = inspection.get("provider_receipts") or {}
        receipt_entries = provider_receipts.get("entries") or []
        error_kind = ""
        for receipt in reversed(receipt_entries):
            if str(receipt.get("status") or "") == "failed":
                error_kind = str(receipt.get("error_kind") or "")
                if error_kind:
                    break

        safe_to_retry = bool(
            failed_node
            and completion_status in {"failed", "blocked"}
            and not integrity_issues
            and (bool(outline_v3.get("available")) or not outline_failed)
        )
        completed = [
            *[str(stage) for stage in status.get("completed_stages") or ()],
            *[str(node_id) for node_id in outline_v3.get("completed_node_ids") or ()],
        ]
        if safe_to_retry:
            recommended = {
                "command": "reviewctl retry-node",
                "arguments": {"job": str(status.get("job_id") or inspection["job_id"]), "node": failed_node},
            }
            retry_scope = [failed_node]
        elif completion_status == "complete":
            recommended = {"command": "none", "arguments": {}}
            retry_scope = []
        elif integrity_issues:
            recommended = {
                "command": "reviewctl repair-plan",
                "arguments": {"job": inspection["job_id"]},
            }
            retry_scope = []
        else:
            recommended = {
                "command": "reviewctl resume",
                "arguments": {"job": inspection["job_id"]},
            }
            retry_scope = []

        result_status = completion_status if completion_status in {"complete", "incomplete", "blocked", "failed"} else "blocked"
        return {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": result_status,
            "job_id": inspection["job_id"],
            "workspace_path": inspection["workspace_path"],
            "failed_node": failed_node,
            "error_kind": error_kind or ("artifact_integrity" if integrity_issues else ""),
            "safe_to_retry": safe_to_retry,
            "retry_scope": retry_scope,
            "preserved_nodes": completed,
            "recommended_action": recommended,
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "issues": integrity_issues,
            "read_only": True,
        }

    def plan(self, spec_path: str | Path) -> dict[str, Any]:
        spec = _load_spec_path(spec_path)
        requested = AgentRuntimeRunner._requested_stages(spec)
        stages = tuple(dict.fromkeys(("source_intake", *requested)))
        payload = {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": "planned",
            "spec_path": str(Path(spec_path).expanduser().resolve()),
            "project_name": spec.project_name,
            "job_id": spec.job_id or None,
            "action": spec.action,
            "stages": list(stages),
            "provider_calls": "not executed",
            "canonical_completion_evaluator": "runtime.completion_evaluator.CanonicalCompletionEvaluator",
            "plan_hash": _canonical_hash(spec.to_dict()),
            "read_only": True,
        }
        return payload

    def _run_spec(
        self,
        spec_path: str | Path,
        *,
        resume: bool = False,
        job_id: str = "",
    ) -> dict[str, Any]:
        spec = _load_spec_path(spec_path)
        if job_id:
            from dataclasses import replace

            spec = replace(spec, job_id=job_id)
        runner = AgentRuntimeRunner(spec)
        result = runner.resume() if resume else runner.run()
        payload = self._status_payload(result)
        if result.job_status != "completed" or result.completion_status != "complete":
            payload["formal_runner_transport_diff"] = self._formal_runner_transport_diff(
                spec,
                result,
            )
        return payload

    def acceptance_run(
        self,
        acceptance_spec_path: str | Path,
    ) -> dict[str, Any]:
        """Execute or resume an owner-authorized acceptance run.

        The acceptance specification is an input contract, not a bag of
        pass/fail claims. Runtime execution is allowed only when the owner
        explicitly enables it; every resulting gate is then verified from
        hashed durable references before it can become PASS.
        """

        from runtime.release_acceptance import (
            AcceptanceRunStateV1,
            AcceptanceScenarioContextV1,
            GateEvidenceProducer,
            GateEvidenceVerifier,
            ReleaseAcceptanceSpec,
            ReleaseAcceptanceSpecError,
            gate_contract,
            scenario_for_gate,
        )

        spec_path = Path(acceptance_spec_path).expanduser().resolve()
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            acceptance_spec = ReleaseAcceptanceSpec.from_mapping(
                raw,
                origin_dir=spec_path.parent,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ControlPlaneError(
                f"acceptance specification is invalid: {type(exc).__name__}: {exc}"
            ) from exc

        current_sha = ""
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_root),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode == 0:
                current_sha = str(completed.stdout or "").strip().splitlines()[-1]
        except (OSError, subprocess.SubprocessError):
            current_sha = ""
        if not current_sha:
            raise ControlPlaneError("acceptance run cannot bind to the current checkout SHA")

        state_path = Path(
            acceptance_spec.state_path
            or (
                Path(acceptance_spec.evidence_manifest).parent
                if acceptance_spec.evidence_manifest
                else self.repo_root / "output" / "_acceptance"
            )
        ).expanduser().resolve()
        if state_path.suffix.casefold() != ".json":
            state_path = state_path / "acceptance_run_state_v1.json"
        evidence_path = Path(
            acceptance_spec.evidence_manifest
            or state_path.with_name("acceptance_evidence_index_v1.json")
        ).expanduser().resolve()
        gates = acceptance_spec.gates or (
            "C",
            "D",
            "E",
            "F",
            "G",
            "H",
            "I",
            "J",
            "K",
            "Q",
        )
        for gate in gates:
            gate_contract(gate)

        state: AcceptanceRunStateV1 | None = None
        with interprocess_file_lock(state_path):
            if state_path.is_file():
                try:
                    state = AcceptanceRunStateV1.from_mapping(
                        json.loads(state_path.read_text(encoding="utf-8"))
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    state = None
            if state is None or state.final_sha != current_sha:
                state = AcceptanceRunStateV1(
                    run_id=f"acceptance-{uuid.uuid4().hex}",
                    final_sha=current_sha,
                    acceptance_spec_path=str(spec_path),
                    runtime_spec_path=acceptance_spec.runtime_spec,
                    status="planned",
                    gates={
                        str(gate): {
                            "gate": str(gate),
                            "status": "NOT_RUN",
                            "contract": gate_contract(gate),
                        }
                        for gate in gates
                    },
                    updated_at=self._utc_now(),
                )
            run_dir = state_path.parent / state.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            state = replace(
                state,
                provider_budget_state_path=(
                    state.provider_budget_state_path
                    or str(run_dir / "provider_budget_state_v1.json")
                ),
                evidence_root=state.evidence_root or str(run_dir / "evidence"),
                process_event_log=(
                    state.process_event_log
                    or str(run_dir / "process_events.jsonl")
                ),
            )
            atomic_write_json(str(state_path), state.to_dict())

        if acceptance_spec.evidence_manifest and evidence_path.is_file():
            try:
                existing_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                existing_evidence = None
            if (
                not isinstance(existing_evidence, Mapping)
                or str(existing_evidence.get("final_sha") or "") != current_sha
                or str(existing_evidence.get("acceptance_run_id") or "") != state.run_id
            ):
                evidence_path = (
                    Path(state.evidence_root).expanduser().resolve()
                    / "acceptance_evidence_index_v1.json"
                )
        if not acceptance_spec.evidence_manifest:
            evidence_path = (
                Path(state.evidence_root).expanduser().resolve()
                / "acceptance_evidence_index_v1.json"
            )
        budget_controller = ProviderBudgetController(
            acceptance_spec.budget.to_provider_budget()
        )
        budget_controller.bind_state_path(state.provider_budget_state_path)
        budget_snapshot = budget_controller.snapshot()
        execution_context = AcceptanceExecutionContextV1(
            acceptance_run_id=state.run_id,
            final_executable_sha=current_sha,
            absolute_deadline_epoch=float(
                budget_snapshot.get("absolute_deadline_epoch") or 0.0
            ),
            provider_budget=acceptance_spec.budget.to_provider_budget(),
            provider_budget_state_path=state.provider_budget_state_path,
            evidence_root=state.evidence_root,
            process_event_log=state.process_event_log,
            scenario_state_path=str(state_path),
            owner_authorized=os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") == "1",
        )

        route_plan: dict[str, Any] | None = None
        runtime_result: dict[str, Any] | None = None
        runtime_spec = (
            Path(acceptance_spec.runtime_spec).expanduser().resolve()
            if acceptance_spec.runtime_spec
            else None
        )
        runtime_job_spec: RuntimeJobSpec | None = None
        if runtime_spec is not None and runtime_spec.is_file():
            try:
                runtime_job_spec = load_runtime_job_spec(runtime_spec)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                runtime_job_spec = None
        if runtime_spec is None:
            runtime_result = {
                "status": "BLOCKED_INPUT",
                "reason": "acceptance spec must provide runtime_spec to execute or resume C-K/Q",
            }
        elif os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") != "1":
            runtime_result = {
                "status": "BLOCKED_AUTHORIZATION",
                "reason": "set AUTO_GENERATE_RUN_LIVE_ACCEPTANCE=1 for owner-authorized acceptance execution",
            }
        else:
            with bind_acceptance_execution_context(execution_context, budget_controller):
                try:
                    runtime_payload = json.loads(runtime_spec.read_text(encoding="utf-8"))
                    runtime_job_spec = RuntimeJobSpec.from_dict(runtime_payload).resolved_from(
                        runtime_spec.parent
                    )
                    config_path = Path(runtime_job_spec.config).expanduser().resolve()
                    route_plan = self.provider_preflight(
                        config_path=config_path,
                        action=runtime_job_spec.action,
                        requested_stages=runtime_job_spec.metadata.get("requested_stages"),
                        free_mode_enabled=bool(
                            runtime_job_spec.free_mode_profile
                            or runtime_job_spec.free_mode_idea
                            or runtime_job_spec.metadata.get("free_mode_input")
                        ),
                    ).get("route_plan")
                    prior_workspace = state.workspace_path if state else ""
                    if prior_workspace and Path(prior_workspace).is_dir():
                        budget_controller.reconcile_orphaned_reservations(
                            receipt_ledgers=self._acceptance_provider_ledger_paths(
                                prior_workspace,
                                job_id=state.job_id if state else "",
                            )
                        )
                        runtime_result = self.resume(
                            workspace=prior_workspace,
                            job_id=state.job_id if state else "",
                        )
                    else:
                        runtime_result = self.run(runtime_spec)
                except ProviderBudgetExceeded as exc:
                    runtime_result = {
                        "status": "BLOCKED_AMBIGUOUS_PROVIDER_CALL",
                        "reason": str(exc),
                    }
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, ControlPlaneError) as exc:
                    runtime_result = {
                        "status": "BLOCKED_INPUT",
                        "reason": f"acceptance runtime input is invalid or blocked: {type(exc).__name__}: {exc}",
                    }

        workspace_path = str(
            (runtime_result or {}).get("workspace_path")
            or (state.workspace_path if state else "")
        )
        job_id = str(
            (runtime_result or {}).get("job_id")
            or (state.job_id if state else "")
            or acceptance_spec.job_id
        )

        refs: list[dict[str, Any]] = []
        if runtime_spec is not None and runtime_spec.is_file():
            refs.append(
                GateEvidenceProducer(
                    final_sha=current_sha,
                    origin_dir=spec_path.parent,
                ).reference(
                    runtime_spec,
                    role="runtime_spec",
                    artifact_type="runtime_job_spec",
                    artifact_version="v1",
                    job_id=job_id,
                )
            )
        if workspace_path and Path(workspace_path).is_dir():
            refs.extend(
                self._acceptance_workspace_references(
                    workspace_path,
                    final_sha=current_sha,
                    job_id=job_id,
                )
            )
        if runtime_job_spec is not None:
            refs.extend(
                self._acceptance_source_references(
                    runtime_job_spec,
                    final_sha=current_sha,
                    job_id=job_id,
                    profile_root=Path(state.evidence_root).expanduser().resolve(),
                )
            )
        scenario_context = AcceptanceScenarioContextV1(
            acceptance_run_id=state.run_id,
            final_executable_sha=current_sha,
            runtime_spec_path=str(runtime_spec or ""),
            workspace_path=workspace_path,
            job_id=job_id,
            evidence_root=state.evidence_root,
            process_event_log=state.process_event_log,
            owner_authorized=execution_context.owner_authorized,
            provider_budget=execution_context.provider_budget.to_dict(),
            provider_budget_state_path=execution_context.provider_budget_state_path,
        )
        scenario_results: dict[str, Any] = {}
        scenario_refs: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for gate in gates:
            scenario_result = scenario_for_gate(str(gate)).execute(
                scenario_context,
                refs,
                runtime_result=runtime_result,
            )
            scenario_results[str(gate)] = scenario_result
            scenario_refs[str(gate)] = scenario_result.evidence_refs
        if any(scenario_refs.values()):
            producer = GateEvidenceProducer(final_sha=current_sha)
            producer.write_manifest(
                evidence_path,
                scenario_refs,
                acceptance_run_id=state.run_id,
                job_id=job_id,
            )

        verifier = GateEvidenceVerifier()
        verified_gates: dict[str, Any] = {}
        evidence_payload: Mapping[str, Any] | None = None
        if evidence_path.is_file():
            try:
                loaded_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                if (
                    isinstance(loaded_evidence, Mapping)
                    and loaded_evidence.get("schema_version")
                    == "release-acceptance-evidence-index-v1"
                    and str(loaded_evidence.get("final_sha") or "") == current_sha
                    and isinstance(loaded_evidence.get("gates"), Mapping)
                ):
                    evidence_payload = loaded_evidence
            except (OSError, UnicodeError, json.JSONDecodeError):
                evidence_payload = None
        for gate in gates:
            scenario_result = scenario_results[str(gate)]
            if scenario_result.status != "READY_FOR_SEMANTIC_VERIFICATION":
                # Do not let a durable manifest from an earlier attempt turn a
                # currently unexecuted or malformed scenario into PASS.  The
                # scenario boundary is authoritative for this acceptance call;
                # the verifier is only reached after that boundary succeeds.
                verified_gates[str(gate)] = {
                    "status": "NOT_VERIFIED",
                    "reason": scenario_result.reason,
                    "contract": gate_contract(str(gate)),
                    "scenario": scenario_result.to_dict(),
                }
                continue
            gate_evidence = (
                evidence_payload.get("gates", {}).get(gate)
                if isinstance(evidence_payload, Mapping)
                and isinstance(evidence_payload.get("gates"), Mapping)
                else None
            )
            verified_gates[str(gate)] = verifier.verify(
                str(gate),
                gate_evidence if isinstance(gate_evidence, Mapping) else None,
                expected_final_sha=current_sha,
                expected_acceptance_run_id=state.run_id,
                origin_dir=evidence_path.parent,
                expected_job_id=job_id,
            )
            verified_gates[str(gate)] = {
                **verified_gates[str(gate)],
                "scenario": scenario_result.to_dict(),
            }
            if verified_gates[str(gate)].get("status") == "PASS":
                continue
            if runtime_result and str(runtime_result.get("status") or "").startswith("BLOCKED"):
                verified_gates[str(gate)] = {
                    **verified_gates[str(gate)],
                    "execution": runtime_result,
                }

        final_status = (
            "complete"
            if verified_gates and all(item.get("status") == "PASS" for item in verified_gates.values())
            else "blocked"
        )
        evidence_revision = int(state.evidence_revision or 0)
        evidence_manifest_hash = str(state.evidence_manifest_hash or "")
        if evidence_path.is_file():
            try:
                evidence_raw = evidence_path.read_bytes()
                evidence_payload_for_state = json.loads(evidence_raw.decode("utf-8"))
                if isinstance(evidence_payload_for_state, Mapping):
                    evidence_revision = int(evidence_payload_for_state.get("revision") or 0)
                    evidence_manifest_hash = hashlib.sha256(evidence_raw).hexdigest()
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                pass
        state = AcceptanceRunStateV1(
            run_id=state.run_id if state else f"acceptance-{uuid.uuid4().hex}",
            final_sha=current_sha,
            acceptance_spec_path=str(spec_path),
            runtime_spec_path=str(runtime_spec or ""),
            status=final_status,
            gates=verified_gates,
            workspace_path=workspace_path,
            job_id=job_id,
            updated_at=self._utc_now(),
            provider_budget_state_path=state.provider_budget_state_path,
            evidence_root=state.evidence_root,
            process_event_log=state.process_event_log,
            evidence_revision=evidence_revision,
            evidence_manifest_hash=evidence_manifest_hash,
            scenario_id="acceptance-run",
        )
        with interprocess_file_lock(state_path):
            atomic_write_json(str(state_path), state.to_dict())
        return {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": final_status,
            "ok": final_status == "complete",
            "run_id": state.run_id,
            "final_sha": current_sha,
            "acceptance_spec_path": str(spec_path),
            "state_path": str(state_path),
            "evidence_manifest": str(evidence_path) if evidence_path.is_file() else "",
            "provider_budget_state_path": state.provider_budget_state_path,
            "acceptance_execution_context": execution_context.to_dict(),
            "runtime_result": runtime_result,
            "route_plan": route_plan,
            "scenarios": {
                gate: result.to_dict()
                for gate, result in scenario_results.items()
            },
            "gates": verified_gates,
            "read_only": False,
        }

    @staticmethod
    def _utc_now() -> str:
        from services.job_workspace import utc_now_iso

        return utc_now_iso()

    def _acceptance_provider_ledger_paths(
        self,
        workspace_path: str | Path,
        *,
        job_id: str = "",
    ) -> tuple[str, ...]:
        """Resolve only job-owned provider ledgers for budget recovery."""

        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.is_dir():
            return ()
        paths: dict[str, Path] = {}
        registry_path = workspace / "artifact_registry.json"
        try:
            registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            registry_payload = {}
        records = (
            registry_payload.get("artifacts", [])
            if isinstance(registry_payload, Mapping)
            else []
        )
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                if str(record.get("artifact_type") or "") != "provider_receipt_ledger":
                    continue
                if job_id and str(record.get("job_id") or "") != job_id:
                    continue
                raw_path = str(record.get("path") or "").strip()
                if not raw_path:
                    continue
                candidate = Path(raw_path).expanduser().resolve()
                try:
                    candidate.relative_to(workspace)
                except ValueError:
                    continue
                if candidate.is_file() and not candidate.is_symlink():
                    paths[str(candidate).casefold()] = candidate
        staging_root = workspace / "artifacts" / ".publication-staging" / "provider-receipts"
        if staging_root.is_dir():
            for candidate in staging_root.rglob("*.jsonl"):
                if candidate.is_file() and not candidate.is_symlink():
                    paths[str(candidate.resolve()).casefold()] = candidate.resolve()
        return tuple(str(paths[key]) for key in sorted(paths))

    def _acceptance_workspace_references(
        self,
        workspace_path: str,
        *,
        final_sha: str,
        job_id: str,
    ) -> list[dict[str, Any]]:
        """Inventory only durable workspace artifacts for acceptance evidence."""

        from runtime.release_acceptance import GateEvidenceProducer

        workspace = Path(workspace_path).expanduser().resolve()
        producer = GateEvidenceProducer(final_sha=final_sha)
        references: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(path: Path, *, role: str, artifact_type: str = "", artifact_version: str = "") -> None:
            resolved = path.expanduser().resolve()
            key = str(resolved).casefold()
            if key in seen or not resolved.is_file() or resolved.is_symlink():
                return
            try:
                ref = producer.reference(
                    resolved,
                    role=role,
                    artifact_type=artifact_type,
                    artifact_version=artifact_version,
                    job_id=job_id,
                )
            except (OSError, ValueError):
                return
            seen.add(key)
            references.append(ref)

        registry_path = workspace / "artifact_registry.json"
        add(registry_path, role="registry")
        registry_payload: Mapping[str, Any] = {}
        try:
            raw_registry = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(raw_registry, Mapping):
                registry_payload = raw_registry
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        for item in registry_payload.get("artifacts", ()) if isinstance(registry_payload, Mapping) else ():
            if not isinstance(item, Mapping):
                continue
            raw_path = str(item.get("path") or "").strip()
            if not raw_path:
                continue
            artifact_type = str(item.get("artifact_type") or "")
            raw_metadata = item.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
            role = {
                "job_outcome": "job_outcome",
                "job_attempt": "attempt",
                "runtime_stage_terminal": (
                    "outline_terminal"
                    if str(metadata.get("stage_name") or "") == "outline"
                    else "stage_terminal"
                ),
                "provider_call_receipt": "provider_receipt_ledger",
                "provider_receipt_ledger": "provider_receipt_ledger",
                "stage1_canonical_summaries": "canonical_stage1",
                "summary_file": "canonical_stage1",
                "paper_artifact": "canonical_stage1",
                "outline_provider_call_plan": "outline_provider_call_plan",
                "review_docx": "review_docx",
                "validation_run_result": "validation_artifact",
                "validation_run_result_repaired": "repair_artifact",
                "validation_report_projection": "defect_artifact",
                "controlled_defect_challenge": "defect_artifact",
                "document_modality_profile": "modality_profile",
                "ocr_diagnostics": "ocr_diagnostics",
                "ocr_artifact": "ocr_artifact",
                "process_interruption_event": "interruption_event",
                "process_resume_event": "resume_event",
                "acceptance_process_event": "process_events",
                "contention_result": "lock_state",
                "citation_manifest_v3": "citation_manifest",
                "citation_manifest": "citation_manifest",
                "validation_receipt_closure": "closure",
                "provider_receipt_closure": "closure",
            }.get(artifact_type, "")
            if role:
                add(
                    Path(raw_path),
                    role=role,
                    artifact_type=artifact_type,
                    artifact_version=str(item.get("artifact_version") or ""),
                )

        return references

    @staticmethod
    def _acceptance_source_references(
        spec: RuntimeJobSpec,
        *,
        final_sha: str,
        job_id: str,
        profile_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        from runtime.release_acceptance import GateEvidenceProducer

        if spec.source.mode != "direct":
            return []
        folder = Path(spec.source.pdf_folder).expanduser().resolve()
        if not folder.is_dir():
            return []
        producer = GateEvidenceProducer(final_sha=final_sha)
        refs: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.pdf")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                source_ref = producer.reference(
                    path,
                    role="source_pdf",
                    artifact_type="source_pdf",
                    job_id=job_id,
                )
                refs.append(source_ref)
                if profile_root is not None:
                    profile_path = ReviewControlPlane._write_modality_profile(
                        path,
                        source_sha256=str(source_ref["sha256"]),
                        profile_root=Path(profile_root).expanduser().resolve(),
                    )
                    refs.append(
                        producer.reference(
                            profile_path,
                            role="modality_profile",
                            artifact_type="document_modality_profile",
                            artifact_version="v1",
                            schema_version="document-modality-profile-v1",
                            job_id=job_id,
                        )
                    )
            except (OSError, ValueError):
                continue
        return refs

    @staticmethod
    def _write_modality_profile(
        source_pdf: Path,
        *,
        source_sha256: str,
        profile_root: Path,
    ) -> Path:
        """Derive document modality from the source PDF without provider input."""

        import fitz  # type: ignore

        page_count = 0
        text_pages = 0
        image_pages = 0
        scanned_pages = 0
        table_count = 0
        figure_count = 0
        with fitz.open(str(source_pdf)) as document:
            page_count = int(document.page_count)
            for index in range(page_count):
                page = document.load_page(index)
                text = str(page.get_text("text") or "").strip()
                image_count = len(page.get_images(full=True))
                if len(text) >= 80:
                    text_pages += 1
                if image_count > 0:
                    image_pages += 1
                if len(text) < 80:
                    scanned_pages += 1
                lowered = text.casefold()
                table_count += len(re.findall(r"\btable\s+[0-9ivx]+\b", lowered))
                figure_count += len(re.findall(r"\b(?:figure|fig\.)\s+[0-9ivx]+\b", lowered))
        if page_count <= 0:
            raise ValueError("source PDF has no pages")
        payload = {
            "artifact_type": "document_modality_profile",
            "artifact_version": "v1",
            "schema_version": "document-modality-profile-v1",
            "source_pdf_sha256": source_sha256,
            "total_page_count": page_count,
            "text_page_ratio": round(text_pages / page_count, 6),
            "image_page_ratio": round(image_pages / page_count, 6),
            "table_count": table_count,
            "figure_count": figure_count,
            "scanned_candidate_page_count": scanned_pages,
            "ocr_used_page_count": 0,
            "selected_visual_count": image_pages,
            "extractor_used": "pymupdf-deterministic-profile",
        }
        target = Path(profile_root).expanduser().resolve() / "modality_profiles" / f"{source_sha256}.json"
        atomic_write_json(str(target), payload)
        return target

    @staticmethod
    def _formal_runner_transport_diff(
        spec: RuntimeJobSpec,
        result: RuntimeExecutionResult,
    ) -> dict[str, Any]:
        """Compare no-network preflight fields with redacted receipt snapshots."""

        try:
            preflight = ReviewControlPlane(repo_root=Path(spec.config).expanduser().resolve().parent).provider_preflight(
                config_path=spec.config,
                action=spec.action,
                requested_stages=spec.metadata.get("requested_stages"),
                free_mode_enabled=bool(
                    spec.free_mode_profile
                    or spec.free_mode_idea
                    or spec.metadata.get("free_mode_input")
                ),
            )
            expected = {
                (
                    str(item.get("provider_family") or ""),
                    str(item.get("model") or ""),
                    str(item.get("endpoint_type") or ""),
                ): item
                for item in (preflight.get("providers") or [])
                if isinstance(item, Mapping)
            }
            workspace = Path(result.workspace_path).expanduser().resolve()
            actual: list[dict[str, Any]] = []
            for path in workspace.glob("artifacts/**/provider_receipts*.jsonl"):
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        raw = json.loads(line)
                        metadata = raw.get("metadata") if isinstance(raw, Mapping) else None
                        snapshot = metadata.get("transport_config") if isinstance(metadata, Mapping) else None
                        if isinstance(snapshot, Mapping):
                            actual.append(dict(snapshot))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            fields = (
                "provider_family",
                "endpoint_type",
                "api_base",
                "model",
                "proxy_mode",
                "trust_env",
                "request_route",
                "request_byte_estimate",
                "timeout_seconds",
                "transport_retries",
            )
            diffs: list[dict[str, Any]] = []
            for snapshot in actual:
                key = (
                    str(snapshot.get("provider_family") or ""),
                    str(snapshot.get("model") or ""),
                    str(snapshot.get("endpoint_type") or ""),
                )
                target = expected.get(key)
                if target is None:
                    diffs.append({"actual": {field: snapshot.get(field) for field in fields}, "expected": None})
                    continue
                field_diff = {
                    field: {"expected": target.get(field), "actual": snapshot.get(field)}
                    for field in fields
                    if target.get(field) != snapshot.get(field)
                }
                if field_diff:
                    diffs.append(field_diff)
            return {
                "status": "diff" if diffs else "match" if actual and preflight.get("ok") else "unavailable",
                "preflight_status": preflight.get("status"),
                "provider_receipt_snapshot_count": len(actual),
                "diffs": diffs,
                "secret_values_included": False,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": type(exc).__name__,
                "secret_values_included": False,
            }

    def run(
        self,
        spec_path: str | Path,
        *,
        job_id: str = "",
    ) -> dict[str, Any]:
        return self._run_spec(spec_path, job_id=job_id)

    def resume(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_workspace(job_id=job_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(resolved)
        spec_path = _persisted_runtime_spec_path(resolved, registry)
        if not spec_path.is_file():
            raise ControlPlaneError(f"persisted runtime spec is missing: {spec_path}")
        outline_resume_plan: dict[str, Any] | None = None
        try:
            node_store = OutlineNodeStore(workspace_obj, registry)
            dag = node_store.load()
            if dag is not None and dag.failed_node_ids:
                _updated, plan = node_store.resume()
                outline_resume_plan = plan.to_dict()
        except (OSError, ValueError, TypeError, RegistryError) as exc:
            raise ControlPlaneError(f"Outline v3 resume planning is blocked: {exc}") from exc
        try:
            cancel_store = CancellationRequestStore(workspace_obj, registry)
            if cancel_store.is_requested():
                cancel_store.clear(cleared_by="reviewctl", reason="resume_requested")
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            raise ControlPlaneError(f"resume cancellation state is invalid: {exc}") from exc
        payload = self._run_spec(
            spec_path,
            resume=True,
            job_id=Path(resolved).name.rsplit("__", 1)[-1],
        )
        payload["outline_v3_resume_plan"] = outline_resume_plan
        return payload

    def retry_node(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        node_id: str,
    ) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        resolved = str(inspection["workspace_path"])
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(resolved)
        outline_state = inspection.get("outline_v3") or {}
        if bool(outline_state.get("available")):
            try:
                updated, plan = OutlineNodeStore(workspace_obj, registry).retry_node(node_id)
            except (OSError, ValueError, TypeError, RegistryError) as exc:
                return {
                    "status": "blocked",
                    "job_id": inspection["job_id"],
                    "node_id": node_id,
                    "safe_to_retry": False,
                    "reason": str(exc),
                    "forbidden_actions": list(FORBIDDEN_ACTIONS),
                    "read_only": True,
                }
            return {
                "status": "planned",
                "job_id": inspection["job_id"],
                "workspace_path": resolved,
                "node_id": node_id,
                "safe_to_retry": True,
                "mutation_performed": True,
                "resume_required": True,
                "resume_plan": plan.to_dict(),
                "preserved_nodes": plan.preserved_node_ids,
                "dag_content_hash": updated.content_hash,
                "forbidden_actions": list(FORBIDDEN_ACTIONS),
                "read_only": False,
            }
        failed_nodes = {
            str(item.get("stage_name") or "")
            for item in inspection.get("stage_terminals") or ()
            if str(item.get("status") or "") in {"failed", "blocked", "cancelled"}
        }
        if node_id not in failed_nodes:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "node_id": node_id,
                "safe_to_retry": False,
                "reason": "node is not a persisted failed terminal; no mutation performed",
                "forbidden_actions": list(FORBIDDEN_ACTIONS),
                "read_only": True,
            }
        return {
            "status": "blocked",
            "job_id": inspection["job_id"],
            "node_id": node_id,
            "safe_to_retry": False,
            "reason": "Outline v3 node replay store is not available for this workspace",
            "preserved_nodes": inspection["status"].get("completed_stages") or [],
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "read_only": True,
        }

    def reconcile(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        resolved = self.resolve_workspace(job_id=job_id, workspace=workspace)
        if dry_run:
            inspection = self.inspect(workspace=resolved)
            return {
                "status": "dry_run",
                "job_id": inspection["job_id"],
                "workspace_path": resolved,
                "would_reconcile": bool(inspection.get("issues")),
                "mutation_performed": False,
                "inspection": inspection,
                "read_only": True,
            }
        try:
            result = AgentRuntimeRunner.reconcile(resolved)
        except (OSError, ValueError, RegistryError, RuntimeRunnerError) as exc:
            raise ControlPlaneError(str(exc)) from exc
        payload = asdict(result)
        payload.update(
            {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "workspace_path": resolved,
                "mutation_performed": bool(
                    payload.get("repaired_artifact_ids")
                    or payload.get("outcome_repaired")
                    or payload.get("pointer_repaired")
                ),
            }
        )
        return payload

    def repair_plan(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        plans = [
            record
            for record in inspection.get("artifacts") or []
            if str(record.get("artifact_type") or "") == "repair_plan"
            and str(record.get("status") or "") == "ready"
        ]
        if plans:
            return {
                "status": "available",
                "job_id": inspection["job_id"],
                "plans": plans,
                "read_only": True,
            }
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        try:
            return RepairTransactionService(workspace_obj, registry).create_report_only_plan()
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "reason": str(exc),
                "mutation_performed": False,
                "read_only": True,
            }

    def repair_apply(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        plan_id: str,
    ) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        plan = next(
            (
                record
                for record in inspection.get("artifacts") or []
                if str(record.get("artifact_id") or "") in {plan_id, f"repair_plan:{plan_id}"}
                and str(record.get("artifact_type") or "") == "repair_plan"
                and str(record.get("status") or "") == "ready"
            ),
            None,
        )
        if plan is None:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "plan_id": plan_id,
                "reason": "repair plan is missing or not a verified ready artifact",
                "mutation_performed": False,
            }

        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        try:
            return RepairTransactionService(workspace_obj, registry).apply_plan(plan_id)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "plan_id": plan_id,
                "reason": str(exc),
                "mutation_performed": False,
            }

    def repair_promote(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        transaction_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Revalidate a quarantined repair, then advance current pointers."""

        inspection = self.inspect(job_id=job_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        source_record = registry.get(transaction_id) or registry.get(f"repair-tx:{transaction_id}")
        if source_record is None:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "transaction_id": transaction_id,
                "reason": "repair transaction is missing from the current Registry",
                "mutation_performed": False,
            }
        source_payload = _json_object(Path(source_record.path))
        if source_payload is None:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "transaction_id": transaction_id,
                "reason": "repair transaction payload is unreadable",
                "mutation_performed": False,
            }
        applied_ids = [str(item) for item in source_payload.get("applied_artifact_ids") or ()]
        derived_draft = None
        derived_manifest = None
        for item in applied_ids:
            record = registry.get(item)
            if record is None:
                continue
            if record.artifact_type == "review_draft_repaired":
                derived_draft = record
            elif record.artifact_type == "citation_manifest_repaired":
                derived_manifest = record
        if derived_draft is None or derived_manifest is None:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "transaction_id": transaction_id,
                "reason": "repair transaction has no quarantined draft/manifest to revalidate",
                "mutation_performed": False,
            }
        spec_path = _persisted_runtime_spec_path(inspection["workspace_path"], registry)
        if not spec_path.is_file():
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "transaction_id": transaction_id,
                "reason": "runtime job spec is required for current-service repair revalidation",
                "mutation_performed": False,
            }
        try:
            spec = load_runtime_job_spec(spec_path)
            bridge = AgentRuntimeBridge(spec)
            session = bridge.bootstrap(
                resume_requested=True,
                claim_latest_pointer=False,
                publish_running_state=False,
            )
            active_registry = session.context.registry
            active_registry.reload()
            active_derived_draft = active_registry.get(derived_draft.artifact_id)
            active_derived_manifest = active_registry.get(derived_manifest.artifact_id)
            if active_derived_draft is None or active_derived_manifest is None:
                raise ControlPlaneError("repair inputs changed before current-service revalidation")
            if (
                active_derived_draft.content_hash != derived_draft.content_hash
                or active_derived_manifest.content_hash != derived_manifest.content_hash
                or file_sha256(active_derived_draft.path) != active_derived_draft.content_hash
                or file_sha256(active_derived_manifest.path) != active_derived_manifest.content_hash
            ):
                raise ControlPlaneError("repair input bytes changed before current-service revalidation")
            versioned_suffix = source_record.content_hash[:16]
            validated_draft_id = f"review_draft:v3:repair:{versioned_suffix}"
            validated_manifest_id = f"citation_manifest:v3:repair:{versioned_suffix}"

            def ensure_validation_candidate(
                *,
                artifact_id: str,
                artifact_type: str,
                artifact_role: str,
                path: str,
                depends_on: Sequence[ArtifactDependencyRefV2] = (),
            ) -> ArtifactRecord:
                existing = active_registry.get(artifact_id)
                if existing is not None:
                    if (
                        existing.artifact_type != artifact_type
                        or existing.artifact_version != "v3"
                        or os.path.abspath(existing.path) != os.path.abspath(path)
                        or existing.content_hash != file_sha256(path)
                    ):
                        raise ControlPlaneError(
                            f"validation candidate identity conflict: {artifact_id}"
                        )
                    return existing
                return active_registry.register_file(
                    artifact_id=artifact_id,
                    artifact_role=artifact_role,
                    artifact_type=artifact_type,
                    artifact_version="v3",
                    path=path,
                    producer="runtime.control_plane.ControlPlane.repair_promote",
                    status="quarantined",
                    depends_on=depends_on,
                    metadata={
                        "repair_validation_candidate": True,
                        "source_artifact_id": (
                            active_derived_draft.artifact_id
                            if artifact_type == "review_draft"
                            else active_derived_manifest.artifact_id
                        ),
                    },
                )

            validated_draft = ensure_validation_candidate(
                artifact_id=validated_draft_id,
                artifact_type="review_draft",
                artifact_role="repair_validation_candidate_review_draft",
                path=active_derived_draft.path,
            )
            validated_manifest = ensure_validation_candidate(
                artifact_id=validated_manifest_id,
                artifact_type="citation_manifest",
                artifact_role="repair_validation_candidate_citation_manifest",
                path=active_derived_manifest.path,
                depends_on=(ArtifactDependencyRefV2.from_record(validated_draft),),
            )
            revalidation_id = f"validation_run_result_repaired:{source_record.content_hash[:16]}"
            revalidation_dir = workspace_obj.artifact_path(
                f"repair_revalidation/{source_record.content_hash[:16]}"
            )
            validation_service = bridge.build_validation_service(
                session,
                attempt_id=f"repair-revalidation:{source_record.content_hash[:16]}:{time.time_ns()}",
            )
            revalidation = validation_service.revalidate_review_artifacts(
                review_draft_record=validated_draft,
                citation_manifest_record=validated_manifest,
                output_dir=revalidation_dir,
                result_artifact_id=revalidation_id,
            )
            session.context.registry.reload()
            revalidation_record = session.context.registry.get(revalidation_id)
            if revalidation_record is None:
                raise ControlPlaneError("current repair revalidation did not register its result")
            result = RepairTransactionService(
                session.context.workspace,
                session.context.registry,
            ).promote_transaction(
                source_record.artifact_id,
                actor=actor,
                reason=reason,
                validation_result=revalidation,
                validation_record=revalidation_record,
                receipt_closure=revalidation.get("provider_receipt_closure"),
            )
            result["revalidation_artifact_id"] = revalidation_record.artifact_id
            result["revalidation_disposition"] = revalidation.get("validation_disposition", "")
            result["revalidation_execution_status"] = revalidation.get("execution_status", "")
            return result
        except (OSError, RegistryError, RuntimeRunnerError, ValueError, TypeError, ControlPlaneError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "transaction_id": transaction_id,
                "reason": str(exc),
                "mutation_performed": False,
            }
    def validation_status(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        try:
            closure = ValidationClosureService(workspace_obj, registry).inspect()
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "reason": str(exc),
                "mutation_performed": False,
                "read_only": True,
            }
        return {
            "status": closure.status,
            "job_id": inspection["job_id"],
            "closure": closure.to_dict(),
            "validation_artifact": closure.validation_artifact,
            "reason": "canonical validation closure inspected without mutation",
            "mutation_performed": False,
            "read_only": True,
        }

    def validate(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        """Execute the current validation stage and persist its receipts/results.

        ``validation-status`` is the read-only projection.  The command named
        ``validate`` must cross the runtime boundary and run the built-in
        current validator; it must not merely inspect a pre-existing report.
        """

        inspection = self.inspect(job_id=job_id, workspace=workspace)
        resolved = Path(str(inspection["workspace_path"])).resolve()
        _workspace_obj, registry = AgentRuntimeRunner._open_workspace(resolved)
        spec_path = _persisted_runtime_spec_path(resolved, registry)
        if not spec_path.is_file():
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "reason": f"persisted runtime spec is missing: {spec_path}",
                "mutation_performed": False,
                "read_only": False,
            }
        try:
            spec = load_runtime_job_spec(spec_path)
            if spec.job_id != inspection["job_id"]:
                raise ControlPlaneError(
                    "persisted runtime spec job_id does not match the resolved workspace"
                )
            bridge = AgentRuntimeBridge(spec)
            attempt_id = f"reviewctl-validation:{spec.job_id}:{time.time_ns()}"
            session = bridge.bootstrap(
                resume_requested=True,
                claim_latest_pointer=False,
                publish_running_state=False,
            )
            stage_result = bridge.run_validation(session, attempt_id=attempt_id)
            session.context.registry.reload()
            closure = ValidationClosureService(
                session.context.workspace,
                session.context.registry,
            ).inspect()
        except (OSError, RegistryError, RuntimeRunnerError, ValueError, TypeError, ControlPlaneError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "reason": str(exc),
                "mutation_performed": False,
                "read_only": False,
            }
        return {
            "status": closure.status,
            "job_id": inspection["job_id"],
            "attempt_id": attempt_id,
            "stage_result": {
                "stage_name": stage_result.stage_name,
                "success": stage_result.success,
                "artifacts": [artifact.to_dict() for artifact in stage_result.artifacts],
                "metadata": dict(stage_result.metadata),
            },
            "closure": closure.to_dict(),
            "validation_artifact": closure.validation_artifact,
            "reason": "current validation execution completed and closure was re-read from Registry",
            "mutation_performed": True,
            "read_only": False,
        }

    def cancel(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        requested_by: str = "reviewctl",
        reason: str = "user_requested",
    ) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        current_status = str((inspection.get("status") or {}).get("job_status") or "")
        if current_status in {"completed", "failed", "cancelled"}:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "reason": f"job is already terminal: {current_status}",
                "mutation_performed": False,
                "read_only": True,
            }
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        try:
            request = CancellationRequestStore(workspace_obj, registry).request(
                requested_by=requested_by,
                reason=reason,
            )
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            raise ControlPlaneError(f"cannot persist cancellation request: {exc}") from exc
        return {
            "status": "requested",
            "job_id": inspection["job_id"],
            "request": request.to_dict(),
            "mutation_performed": True,
            "read_only": False,
        }

    def adopt(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        artifact_id: str,
        actor: str = "",
        reason: str = "",
        expected_hash: str = "",
    ) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        artifact = next(
            (
                record
                for record in inspection.get("artifacts") or []
                if str(record.get("artifact_id") or "") == artifact_id
            ),
            None,
        )
        if artifact is None or str(artifact.get("status") or "") != "ready":
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "artifact_id": artifact_id,
                "reason": "adoption target is not a verified ready Registry artifact",
                "mutation_performed": False,
            }
        if not str(actor or "").strip():
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "artifact_id": artifact_id,
                "reason": "adoption actor is required for the immutable audit record",
                "mutation_performed": False,
            }
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        try:
            result = OutlineAdoptionTransaction(workspace_obj, registry).adopt(
                source_artifact_id=artifact_id,
                actor=actor,
                reason=reason,
                expected_hash=expected_hash,
            )
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            return {
                "status": "blocked",
                "job_id": inspection["job_id"],
                "artifact_id": artifact_id,
                "reason": str(exc),
                "mutation_performed": False,
            }
        payload = result.to_dict()
        payload["artifact_id"] = artifact_id
        payload["forbidden_actions"] = list(FORBIDDEN_ACTIONS)
        return payload

    def export(self, *, batch_id: str | None = None, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        if not job_id and not workspace:
            raise ControlPlaneError("export requires --batch/--job or --workspace")
        inspection = self.inspect(job_id=job_id or batch_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        result = ExportBundleService(workspace_obj, registry).export(
            spec=ExportBundleSpecV1(),
        )
        payload = result.to_dict()
        payload.update(
            {
                "batch_id": batch_id or inspection["job_id"],
                "workspace_path": inspection["workspace_path"],
                "export_scope": "verified_registry_artifacts_and_forensic_provenance",
                "inspection": inspection,
                "read_only": False,
            }
        )
        return payload

    def attest(self, *, job_id: str | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        inspection = self.inspect(job_id=job_id, workspace=workspace)
        workspace_obj, registry = AgentRuntimeRunner._open_workspace(inspection["workspace_path"])
        result = ForensicAttestationService(workspace_obj, registry).attest(
            persist=True,
        )
        payload = result.to_dict()
        payload.update(
            {
                "workspace_path": inspection["workspace_path"],
                "scope": "registry_file_hashes_dependency_graph_validation_closure",
                "read_only": False,
                "next_step": "full closure is required before trusting the inspected workspace"
                if result.status == "untrusted"
                else "no further forensic action is required for the inspected scope",
            }
        )
        return payload

    def doctor(self, *, config_path: str | Path | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, status: str, details: Any) -> None:
            checks.append({"name": name, "status": status, "details": details})

        target_config = Path(config_path or self.repo_root / "config.ini").expanduser().resolve()
        template_config = target_config.name.casefold().endswith(".example")
        normalized_config: Mapping[str, Mapping[str, Any]] = {}
        if not target_config.is_file():
            add("configuration", "fail", {"path": str(target_config), "error": "config.ini is missing"})
        else:
            try:
                normalized_config = load_config(
                    str(target_config),
                    required_provider_sections=(),
                    allow_template_credentials=template_config,
                )
                valid, warnings = validate_all_config(
                    dict(normalized_config),
                    required_provider_sections=(),
                    allow_template_credentials=template_config,
                )
                add(
                    "configuration",
                    "pass" if valid else "fail",
                    {
                        "path": str(target_config),
                        "valid": bool(valid),
                        "warnings": list(warnings),
                        "credential_provenance": provenance_payload(
                            list(getattr(normalized_config, "credential_provenance", ()))
                        ),
                    },
                )
            except Exception as exc:
                add("configuration", "fail", {"path": str(target_config), "error": str(exc)})

        provider_details: list[dict[str, Any]] = []
        missing_keys: list[str] = []
        for section_name in _API_SECTIONS:
            section = dict(normalized_config.get(section_name) or {})
            if not section:
                continue
            capability = (
                resolve_model_capability(cast(APIConfig, section))
                if section.get("model")
                else None
            )
            raw_key = str(section.get("api_key") or "").strip()
            has_key = bool(raw_key) and not is_template_credential(raw_key)
            if not has_key and section_name in {"Primary_Reader_API", "Backup_Reader_API", "Writer_API"}:
                missing_keys.append(section_name)
            provider_details.append(
                {
                    "section": section_name,
                    "api_key_present": has_key,
                    "model_configured": bool(str(section.get("model") or "").strip()),
                    "api_base_configured": bool(str(section.get("api_base") or "").strip()),
                    "endpoint_classification": (
                        classify_provider_endpoint(
                            str(section.get("api_base") or ""),
                            capability.provider_family,
                        )
                        if capability is not None
                        else None
                    ),
                    "capability": (
                        {
                            "provider_family": capability.provider_family,
                            "endpoint_type": capability.endpoint_type,
                            "supports_reasoning": capability.supports_reasoning,
                            "supports_pdf_file_input": capability.supports_pdf_file_input,
                            "max_token_param": capability.max_token_param,
                            "max_output_tokens": capability.max_output_tokens,
                        }
                        if capability is not None
                        else None
                    ),
                }
            )
        add(
            "provider_capability",
            "warn" if missing_keys else "pass",
            {"providers": provider_details, "missing_required_api_keys": missing_keys, "network_probe": False},
        )

        try:
            stage1_settings = ApplicationSettings.from_config(normalized_config)
            primary_reader = dict(normalized_config.get("Primary_Reader_API") or {})
            input_settings = dict(stage1_settings.section("Stage1_Input"))
            stage1_budget_details = {
                "visual_extract_or_scan": list(
                    stage1_output_budget_sequence(
                        "visual_scan",
                        input_settings,
                        provider_config=primary_reader,
                    )
                ),
                "paper_synthesis": list(
                    stage1_output_budget_sequence(
                        "synthesis",
                        input_settings,
                        provider_config=primary_reader,
                    )
                ),
                "provider_max_output_tokens": provider_output_token_limit(primary_reader),
                "selection_mode": str(
                    stage1_settings.section("Stage1_Visual").get("selection_mode")
                    or "selective"
                ),
            }
            add("stage1_budget", "pass", stage1_budget_details)
        except Exception as exc:
            add("stage1_budget", "fail", {"error": str(exc)})

        try:
            route_plan = build_reachable_provider_route_plan(
                normalized_config,
                action="run_all",
                requested_stages=None,
            )
            add(
                "reachable_provider_routes",
                "fail" if route_plan.unresolved_required_routes else "pass",
                {
                    "route_plan": route_plan.to_dict(),
                    "required_provider_sections": list(
                        route_plan.required_provider_sections
                    ),
                    "semantic_roles": list(route_plan.semantic_roles),
                    "physical_route_count": len(route_plan.physical_routes),
                },
            )
        except Exception as exc:
            add("reachable_provider_routes", "fail", {"error": str(exc)})

        try:
            mineru = PreprocessManager(dict(normalized_config))
            invalid_hosts = sorted(
                str(item) for item in getattr(mineru, "mineru_invalid_allowed_url_hosts", set())
            )
            missing_defaults = sorted(
                DEFAULT_MINERU_ALLOWED_URL_HOSTS
                - set(getattr(mineru, "mineru_allowed_url_hosts", set()))
            )
            allowlist_status = "pass" if not invalid_hosts and not missing_defaults else "warn"
            add(
                "mineru_result_allowlist",
                allowlist_status,
                {
                    "default_exact_hosts": sorted(DEFAULT_MINERU_ALLOWED_URL_HOSTS),
                    "effective_exact_hosts": sorted(mineru.mineru_allowed_url_hosts),
                    "invalid_configured_entries": invalid_hosts,
                    "missing_default_hosts": missing_defaults,
                    "wildcards_allowed": False,
                    "network_probe": False,
                },
            )
        except Exception as exc:
            add("mineru_result_allowlist", "fail", {"error": str(exc)})

        add(
            "current_settings",
            "pass" if normalized_config else "warn",
            {
                "typed_sections": sorted(normalized_config),
                "runtime_source": "services.settings.ApplicationSettings",
            },
        )

        add(
            "workspace_permissions",
            "pass" if os.access(self.repo_root, os.R_OK | os.W_OK) else "fail",
            {"repo_root": str(self.repo_root), "readable": os.access(self.repo_root, os.R_OK), "writable": os.access(self.repo_root, os.W_OK)},
        )
        add("dependencies", "pass", self._dependency_check())
        add("tokenizer", "pass" if any(importlib.util.find_spec(name) for name in _OPTIONAL_TOKENIZER_MODULES) else "warn", {"available": [name for name in _OPTIONAL_TOKENIZER_MODULES if importlib.util.find_spec(name)]})
        add("certificate_paths", "pass", self._certificate_check())
        add("stale_locks", "warn" if self._stale_locks(workspace) else "pass", {"locks": self._stale_locks(workspace)})
        add("git", "pass", self._git_check())
        add("project_root_pollution", "pass", {"checked": False, "reason": "no project-specific output was modified by doctor"})

        if workspace:
            try:
                inspection = self.inspect(workspace=workspace)
                add("artifact_integrity", "fail" if inspection["issues"] else "pass", {"issues": inspection["issues"], "job_id": inspection["job_id"]})
                add("running_jobs", "pass", {"job_id": inspection["job_id"], "job_status": inspection["status"].get("job_status")})
            except ControlPlaneError as exc:
                add("artifact_integrity", "fail", {"error": str(exc)})
        else:
            add("artifact_integrity", "skipped", {"reason": "doctor was not given a workspace"})
            add("running_jobs", "skipped", {"reason": "doctor was not given a workspace"})

        failed = [check["name"] for check in checks if check["status"] == "fail"]
        return {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": "fail" if failed else "warn" if any(check["status"] == "warn" for check in checks) else "pass",
            "ok": not failed,
            "checks": checks,
            "provider_network_calls": 0,
            "read_only": True,
        }

    def provider_preflight(
        self,
        *,
        config_path: str | Path | None = None,
        action: str = "analyze",
        requested_stages: Sequence[str] | None = None,
        section: str | None = None,
        free_mode_enabled: bool = False,
    ) -> dict[str, Any]:
        """Exercise the formal config/route/payload construction without HTTP."""

        target_config = Path(config_path or self.repo_root / "config.ini").expanduser().resolve()
        try:
            normalized = load_config(
                str(target_config),
                action=action,
                requested_stages=requested_stages,
                free_mode_enabled=free_mode_enabled,
                allow_template_credentials=False,
            )
            route_plan = build_reachable_provider_route_plan(
                normalized,
                action=action,
                requested_stages=requested_stages,
                free_mode_enabled=free_mode_enabled,
            )
            roles = route_plan.required_provider_sections
            selected_section = section
            if section and section in route_plan.semantic_roles:
                selected_section = route_plan.route_for_role(section).section_name
            if section and selected_section not in roles:
                return {
                    "control_plane_version": CONTROL_PLANE_VERSION,
                    "status": "blocked",
                    "ok": False,
                    "action": action,
                    "requested_stages": list(requested_stages or ()),
                    "free_mode_enabled": bool(free_mode_enabled),
                    "provider_roles": list(roles),
                    "route_plan": route_plan.to_dict(),
                    "error_type": "UnreachableProviderRoute",
                    "error": f"[{section}] is not reachable from the current StagePlan",
                    "network_calls": 0,
                    "read_only": True,
                }
            selected_roles = (selected_section,) if selected_section else roles
            provenance = {
                item.section: item.selected_source
                for item in getattr(normalized, "credential_provenance", ())
            }
            providers: list[dict[str, Any]] = []
            for role in selected_roles:
                provider = dict(normalized.get(role) or {})
                if not provider:
                    raise ValueError(f"required provider section is missing: [{role}]")
                details = build_provider_transport_preflight(
                    provider,
                    credential_source=provenance.get(role, "unknown"),
                )
                details["section"] = role
                providers.append(details)
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": "pass",
                "ok": True,
                "action": action,
                "requested_stages": list(requested_stages or ()),
                "free_mode_enabled": bool(free_mode_enabled),
                "provider_roles": list(selected_roles),
                "route_plan": route_plan.to_dict(),
                "providers": providers,
                "credential_provenance": provenance_payload(
                    list(getattr(normalized, "credential_provenance", ()))
                ),
                "proxy_policy": "formal ai_interface._post_with_proxy_mode",
                "network_calls": 0,
                "read_only": True,
            }
        except Exception as exc:
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": "fail",
                "ok": False,
                "config_path": str(target_config),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "network_calls": 0,
                "read_only": True,
            }

    def provider_micro_probe(
        self,
        *,
        config_path: str | Path | None = None,
        action: str = "analyze",
        requested_stages: Sequence[str] | None = None,
        section: str | None = None,
        third_party_acknowledged: bool = False,
        third_party_hosts: Sequence[str] | None = None,
        free_mode_enabled: bool = False,
    ) -> dict[str, Any]:
        """Make a minimal real request through the production transport path.

        This is intentionally separate from :meth:`provider_preflight`, which
        is a zero-network dry check.  The route, credential provenance, request
        construction, ProviderRuntime admission, and receipt path are still
        the same ones used by a production stage call.
        """

        target_config = Path(config_path or self.repo_root / "config.ini").expanduser().resolve()
        try:
            normalized = load_config(
                str(target_config),
                action=action,
                requested_stages=requested_stages,
                free_mode_enabled=free_mode_enabled,
                allow_template_credentials=False,
            )
            route_plan = build_reachable_provider_route_plan(
                normalized,
                action=action,
                requested_stages=requested_stages,
                free_mode_enabled=free_mode_enabled,
            )
            roles = route_plan.required_provider_sections
            selected_section = section
            if section and section in route_plan.semantic_roles:
                selected_section = route_plan.route_for_role(section).section_name
            selected_roles = (selected_section,) if selected_section else roles
            if section and selected_section not in roles:
                return {
                    "control_plane_version": CONTROL_PLANE_VERSION,
                    "status": "BLOCKED_UNREACHABLE_ROUTE",
                    "ok": False,
                    "provider_roles": list(roles),
                    "route_plan": route_plan.to_dict(),
                    "network_calls": 0,
                    "read_only": False,
                }
            allowed_hosts = {
                str(host).strip().casefold()
                for host in (third_party_hosts or ())
                if str(host).strip()
            }
            provenance = {
                item.section: item.selected_source
                for item in getattr(normalized, "credential_provenance", ())
            }
            providers: list[dict[str, Any]] = []
            for role in selected_roles:
                provider = dict(normalized.get(role) or {})
                if not provider:
                    raise ValueError(f"required provider section is missing: [{role}]")
                details = build_provider_transport_preflight(
                    provider,
                    credential_source=provenance.get(role, "unknown"),
                )
                classification = details.get("endpoint_classification")
                host = str((classification or {}).get("host") or "").casefold()
                if (
                    classification
                    and classification.get("classification") != "official_provider_host"
                    and (
                        not third_party_acknowledged
                        or host not in allowed_hosts
                    )
                ):
                    return {
                        "control_plane_version": CONTROL_PLANE_VERSION,
                        "status": "BLOCKED_TRUST_POLICY",
                        "ok": False,
                        "provider_roles": list(selected_roles),
                        "route_plan": route_plan.to_dict(),
                        "blocked_host": host,
                        "network_calls": 0,
                        "read_only": False,
                    }
                providers.append({"section": role, "details": details, "config": provider})

            output_root = Path(str(normalized.get("Paths", {}).get("output_path") or self.repo_root / "output"))
            from runtime.provider_runtime import provider_budget_controller_from_environment

            acceptance_budget = provider_budget_controller_from_environment()
            if acceptance_budget is not None:
                active_context = current_acceptance_execution_context()
                budget_state_path = (
                    Path(active_context.provider_budget_state_path)
                    if active_context is not None
                    else output_root / "_acceptance" / "provider_budget_state_v1.json"
                )
                acceptance_budget.bind_state_path(
                    str(budget_state_path)
                )
            ledger = ProviderRuntimeLedger(output_root / "_acceptance" / "provider_micro_probe.jsonl")
            from ai_interface import _call_ai_api_detailed

            results: list[dict[str, Any]] = []
            total_calls = 0
            for index, item in enumerate(providers, start=1):
                role = str(item["section"])
                provider = cast(APIConfig, item["config"])
                capability = resolve_model_capability(provider)
                runtime = ProviderRuntime(
                    ledger=ledger,
                    job_id="release-acceptance-micro-probe",
                    attempt_id="micro-probe",
                    stage_name="provider_micro_probe",
                    route=role,
                    node_id=f"micro-probe:{role}",
                    call_id=f"micro-probe:{index}:{role}",
                    endpoint_type=capability.endpoint_type,
                )
                result = _call_ai_api_detailed(
                    "ping",
                    provider,
                    "Return one short token.",
                    max_tokens=1,
                    temperature=0.0,
                    response_format="text",
                    provider_runtime=runtime,
                )
                receipts = runtime.receipts
                total_calls += sum(int(receipt.attempts) for receipt in receipts)
                results.append(
                    {
                        "semantic_roles": [
                            route.semantic_role
                            for route in route_plan.routes
                            if route.section_name == role and route.enabled
                        ],
                        "semantic_role": role,
                        "provider_family": capability.provider_family,
                        "model": str(provider.get("model") or ""),
                        "endpoint_type": capability.endpoint_type,
                        "hostname": str(item["details"].get("endpoint_classification", {}).get("host") or ""),
                        "endpoint_classification": item["details"].get("endpoint_classification", {}),
                        "credential_source": provenance.get(role, "unknown"),
                        "status": result.get("status"),
                        "error_kind": result.get("error_kind"),
                        "receipt_ids": [receipt.receipt_id for receipt in receipts],
                        "attempts": sum(int(receipt.attempts) for receipt in receipts),
                        "usage_status": [receipt.usage_status for receipt in receipts],
                    }
                )
            ok = bool(results) and all(item.get("status") == "success" for item in results)
            usage = ledger.usage_summary()
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": "pass" if ok else "fail",
                "ok": ok,
                "provider_roles": list(selected_roles),
                "free_mode_enabled": bool(free_mode_enabled),
                "route_plan": route_plan.to_dict(),
                "providers": results,
                "network_calls": total_calls,
                "usage": usage,
                "receipt_ledger": str(ledger.path),
                "read_only": False,
            }
        except Exception as exc:
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": "fail",
                "ok": False,
                "config_path": str(target_config),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "network_calls": 0,
                "read_only": False,
            }
    @staticmethod
    def _dependency_check() -> dict[str, Any]:
        missing = [name for name in _REQUIRED_RUNTIME_MODULES if importlib.util.find_spec(name) is None]
        return {"required": list(_REQUIRED_RUNTIME_MODULES), "missing": missing}

    @staticmethod
    def _certificate_check() -> dict[str, Any]:
        paths: list[dict[str, Any]] = []
        for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            value = os.environ.get(variable, "").strip()
            if value:
                target = Path(value).expanduser()
                paths.append({"variable": variable, "configured": True, "exists": target.is_file()})
            else:
                paths.append({"variable": variable, "configured": False, "exists": None})
        return {"paths": paths}

    def _stale_locks(self, workspace: str | Path | None) -> list[dict[str, Any]]:
        roots = [Path(workspace).expanduser().resolve()] if workspace else [self.repo_root]
        found: list[dict[str, Any]] = []
        now = time.time()
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.lock"):
                if any(part in {".git", ".omx", "__pycache__"} for part in path.parts):
                    continue
                if path.name.casefold() in {
                    "requirements-py311-windows.lock",
                    "uv.lock",
                    "poetry.lock",
                    "pipfile.lock",
                }:
                    continue
                try:
                    age = max(0.0, now - path.stat().st_mtime)
                except OSError:
                    continue
                if self._probe_persistent_lock(path):
                    continue
                found.append(
                    {
                        "path": str(path),
                        "age_seconds": int(age),
                        "status": "active_or_contended",
                    }
                )
        return found

    @staticmethod
    def _probe_persistent_lock(path: Path) -> bool:
        """Return whether a persistent lock file can be acquired now.

        Lock files are intentionally retained after release.  The OS lock,
        not mtime or file existence, is the authority for current contention.
        """

        try:
            with path.open("a+b") as handle:
                if handle.seek(0, os.SEEK_END) == 0:
                    return True
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        return False
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                    return True
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return False
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                return True
        except OSError:
            return False

    def _git_check(self) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.repo_root),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "error": str(exc)}
        return {
            "available": completed.returncode == 0,
            "returncode": completed.returncode,
            "dirty": bool(completed.stdout.strip()),
        }


__all__ = [
    "CONTROL_PLANE_VERSION",
    "ControlPlaneError",
    "FORBIDDEN_ACTIONS",
    "ReviewControlPlane",
]

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
import configparser
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence, cast

from config_loader import load_config
from config_validator import validate_all_config
from ai_interface import build_provider_transport_preflight, classify_provider_endpoint
from services.credential_provenance import is_template_credential, provenance_payload
from models import APIConfig
from runtime.job_spec import RuntimeJobSpec, load_runtime_job_spec
from runtime.checkout_identity import CheckoutIdentityError, read_checkout_sha
from runtime.outline_v3_dag import OutlineNodeStore
from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.orchestrator import AgentRuntimeBridge, InternalStageExecutorRegistry
from runtime.runner import AgentRuntimeRunner, RuntimeExecutionResult, RuntimeRunnerError
from runtime.provider_runtime import (
    DEFAULT_PROVIDER_CALL_BUDGET,
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeLedger,
    acceptance_execution_context_from_environment,
    authorized_provider_call_limit,
    acceptance_context_environment,
    bind_acceptance_execution_context,
    current_acceptance_execution_context,
    hash_json,
    is_process_alive,
    process_identity_for_pid,
    provider_budget_controller_from_environment,
)
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    verify_stage1_typed_manifest_authority,
)
from runtime.reconcile import load_summary_source_manifest
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_planning import (
    ProviderStageRequestInventoryV1,
    UnplannedProviderExposureV1,
    build_full_stage_request_plan_v1,
    build_stage_plan,
)
from runtime.trust_admission import (
    ExternalHostAdmissionError,
    acknowledgement_from_values,
    build_external_host_policy,
    build_runtime_external_host_policy,
    validate_external_host_acknowledgement,
)
from runtime.stage_terminal import StageTerminalStore
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.model_capabilities import resolve_model_capability
from services.settings import ApplicationSettings, mineru_remote_requested
from services.stage1_output_budget import stage1_output_budget_sequence, provider_output_token_limit
from preprocess.service import DEFAULT_MINERU_ALLOWED_URL_HOSTS, PreprocessManager
from services.job_workspace import JobWorkspace, atomic_write_json, is_reparse_path
from services.durable_io import atomic_replace_with_retry, interprocess_file_lock
from runtime.cancellation import CancellationRequestStore
from runtime.pause_state import PauseStateStore
from runtime.export_bundle import ExportBundleService, ExportBundleSpecV1, ForensicAttestationService
from outline.adoption_transaction import OutlineAdoptionTransaction
from validation.closure import ValidationClosureService
from validation.repair_transaction import RepairTransactionService
from outline.semantic_chunking import ReuseInventoryItem, build_paper_content_layers, build_semantic_chunk_plan
from outline.v3_evidence import build_outline_evidence_views, build_global_corpus_ledger, build_multi_view_matrix
from outline.v3_relations import build_global_relation_map


CONTROL_PLANE_VERSION = "reviewctl-v1"
PROVIDER_FREE_SHADOW_CALL_LIMITS = (24, 48, 64, 80)
_FULL_STAGE_REQUEST_BUILDER_IDS = {
    ("analyze", "backup_reader"): "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
    ("outline", "relation_adjudication"): "outline.v3_executor.OutlineV3Executor._run_hierarchical_relation_adjudication",
    ("outline", "structure_critique"): "outline.v3_executor.OutlineV3Executor._run_hierarchical_critique:structure_critique",
    ("outline", "coverage_critique"): "outline.v3_executor.OutlineV3Executor._run_hierarchical_critique:coverage_critique",
    ("outline", "evidence_critique"): "outline.v3_executor.OutlineV3Executor._run_hierarchical_critique:evidence_critique",
    ("outline", "arbitration"): "outline.v3_executor.OutlineV3Executor._run_provider_node:arbitration",
}
_FULL_STAGE_BUILDER_EXPOSURE = {
    ("analyze", "backup_reader"): (
        "backup_reader_calls_depend_on_primary_reader_failure_or_correction",
        "primary_reader_failed_or_semantic_correction_required",
    ),
    ("outline", "relation_adjudication"): (
        "relation_builder_requires_materialized_candidate_scope_and_shards",
        "",
    ),
    ("outline", "structure_critique"): ("critique_builder_requires_materialized_candidate_outputs", ""),
    ("outline", "coverage_critique"): ("critique_builder_requires_materialized_candidate_outputs", ""),
    ("outline", "evidence_critique"): ("critique_builder_requires_materialized_candidate_outputs", ""),
    ("outline", "arbitration"): (
        "arbitration_builder_requires_materialized_candidate_and_critique_outputs",
        "eligible_candidate_and_critique_outputs_materialized",
    ),
}
_FULL_STAGE_LOGICAL_CALL_UPPER_BOUNDS = {
    # The canonical Outline path invokes arbitration once after candidate and
    # critique closure; absence of eligible candidates fails before transport.
    ("outline", "arbitration"): 1,
}
FORBIDDEN_ACTIONS = (
    "edit_registry",
    "edit_stage_health",
    "rerun_completed_candidates",
    "disable_quality_gate",
    "delete_workspace",
)


def _provider_free_shadow_capacity_comparisons(
    *,
    topic_call_lower_bound: int,
    logical_call_upper_bound: int | None,
    physical_attempt_upper_bound: int | None,
    actual_runtime_call_limit: int,
    actual_preflight_status: str,
) -> list[dict[str, Any]]:
    """Compare planning bounds without changing provider admission authority."""

    comparisons: list[dict[str, Any]] = []
    for limit in PROVIDER_FREE_SHADOW_CALL_LIMITS:
        if topic_call_lower_bound > limit:
            status = "blocked_known_topic_call_lower_bound"
        elif logical_call_upper_bound is None or physical_attempt_upper_bound is None:
            status = "incomplete_upper_bound"
        elif logical_call_upper_bound > limit or physical_attempt_upper_bound > limit:
            status = "blocked_estimated_upper_bound"
        else:
            status = "within_shadow_capacity"
        comparisons.append(
            {
                "shadow_physical_call_limit": limit,
                "known_topic_call_lower_bound": topic_call_lower_bound,
                "logical_call_upper_bound": logical_call_upper_bound,
                "physical_attempt_upper_bound": physical_attempt_upper_bound,
                "upper_bound_completeness_status": (
                    "materialized_upper_bound"
                    if logical_call_upper_bound is not None
                    and physical_attempt_upper_bound is not None
                    else "incomplete_upper_bound"
                ),
                "status": status,
                "planning_only": True,
                "comparison_scope": "outline_v3_provider_call_plan",
                "actual_runtime_call_limit": actual_runtime_call_limit,
                "actual_preflight_status": actual_preflight_status,
                "provider_admission_authorized": False,
                "provider_posts_emitted": 0,
            }
        )
    return comparisons


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


def _acceptance_lexical_path(value: str | Path) -> Path:
    """Reject existing symlink/reparse ancestors before resolving a write path."""

    target = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    current = Path(target.anchor) if target.anchor else Path.cwd()
    parts = target.parts[1:] if target.anchor else target.parts
    for part in parts:
        current = current / part
        if not os.path.lexists(current):
            # A future child cannot currently be a reparse point. Its parent
            # chain was checked above; creation still happens under the normal
            # locked/contained write boundaries.
            break
        if is_reparse_path(current):
            raise ControlPlaneError(
                f"acceptance path contains a symlink or reparse point: {current}"
            )
    return target


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
        staging_dir = Path(workspace.root_dir) / ".publication-staging" / "provider-receipts"
        if staging_dir.is_dir():
            candidates.extend(staging_dir.glob("**/provider_receipts/*.jsonl"))
        unique = tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
        entries_by_id: dict[str, dict[str, Any]] = {}
        entry_hashes: dict[str, str] = {}
        conflicts: list[str] = []
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
                    item = dict(payload)
                    receipt_id = str(item.get("receipt_id") or item.get("call_id") or "").strip()
                    if not receipt_id:
                        malformed.append(f"{path}:{line_number}: receipt identity is missing")
                        continue
                    item_hash = _canonical_hash(item)
                    previous_hash = entry_hashes.get(receipt_id)
                    if previous_hash is not None and previous_hash != item_hash:
                        conflicts.append(receipt_id)
                        entries_by_id.pop(receipt_id, None)
                        continue
                    entry_hashes[receipt_id] = item_hash
                    entries_by_id[receipt_id] = item
                else:
                    malformed.append(f"{path}:{line_number}: receipt must be an object")
        entries = list(entries_by_id.values())
        return {
            "paths": [str(path) for path in unique],
            "count": len(entries),
            "entries": entries,
            "malformed": malformed,
            "conflicts": sorted(set(conflicts)),
            "complete": bool(unique) and not malformed and not conflicts,
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

    def _full_stage_request_plan_for_spec(
        self, spec: RuntimeJobSpec
    ) -> dict[str, Any]:
        """Expose reachable work before admission without inventing requests."""

        requested = spec.metadata.get("requested_stages")
        free_mode_enabled = bool(
            spec.free_mode_profile
            or spec.free_mode_idea
            or spec.metadata.get("free_mode_input")
        )
        config = load_config(
            spec.config,
            action=spec.action,
            requested_stages=requested,
            free_mode_enabled=free_mode_enabled,
            allow_template_credentials=False,
        )
        settings = ApplicationSettings.from_config(config)
        stage_plan = build_stage_plan(
            action=spec.action,
            requested_stages=requested,
            validation_enabled=settings.review_validation_enabled(),
            validation_required=spec.metadata.get("validation_required"),
            require_clean_validation=spec.metadata.get("require_clean_validation"),
            allow_unvalidated_when_validation_optional=spec.metadata.get(
                "allow_unvalidated_when_validation_optional"
            ),
        )
        stage1_reuse_projection = self._verified_typed_reuse_only_stage1_input(spec)
        route_plan = build_reachable_provider_route_plan(
            config,
            action=spec.action,
            requested_stages=stage_plan.requested_stages,
            free_mode_enabled=free_mode_enabled,
            stage_plan=stage_plan,
        )
        from outline.v3_executor import MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE
        suppressed_provider_routes: list[dict[str, str]] = []
        if stage1_reuse_projection is not None:
            suppressed_provider_routes = [
                {
                    "stage": route.stage,
                    "semantic_role": route.semantic_role,
                    "reason": "verified typed reuse-only source has no Stage 1 paper work items",
                }
                for route in route_plan.routes
                if route.stage == "analyze"
            ]
            route_plan = replace(
                route_plan,
                routes=tuple(
                    route for route in route_plan.routes if route.stage != "analyze"
                ),
                diagnostics=(
                    *route_plan.diagnostics,
                    "Analyze provider routes are unreachable for verified typed reuse-only input",
                ),
            )
        from runtime.provider_context import ProviderContextProfile
        from services.model_selection import get_api_config_for_section

        runtime_retries = max(
            0,
            int(str(config.get("Runtime", {}).get("transport_retries") or "2")),
        )

        def unplanned_exposure(
            *,
            stage_name: str,
            semantic_role: str,
            route: Any,
            reason: str,
            request_builder_id: str = "",
            conditional_on: str = "",
            logical_calls_upper_bound: int | None = None,
            output_tokens_upper_bound: int | None = None,
        ) -> UnplannedProviderExposureV1:
            """Bound each reachable request while keeping its call count unknown."""

            api_config = get_api_config_for_section(config, route.section_name)
            model = str(api_config.get("model") or route.model or "").strip()
            profile: ProviderContextProfile | None = None
            if model:
                capability = resolve_model_capability(api_config)
                output_tokens = max(1, int(api_config.get("max_output_tokens") or 4_096))
                if semantic_role == "evidence_critique":
                    output_tokens = min(output_tokens, 2_048)
                elif semantic_role in {"structure_critique", "coverage_critique"}:
                    output_tokens = min(output_tokens, 2_048)
                if output_tokens_upper_bound is not None:
                    output_tokens = min(output_tokens, max(1, int(output_tokens_upper_bound)))
                profile = ProviderContextProfile.conservative(
                    provider=capability.provider_family,
                    model=model,
                    endpoint_type=capability.endpoint_type,
                    model_context_limit=max(
                        1, int(api_config.get("max_context_tokens") or 128_000),
                    ),
                    max_output_tokens=output_tokens,
                    reasoning_reserve=max(
                        0, int(api_config.get("reasoning_reserve_tokens") or 2_048),
                    ),
                    safety_margin=max(
                        0, int(api_config.get("safety_margin_tokens") or 1_024),
                    ),
                )
            input_tokens_upper_bound = (
                int(profile.input_budget) if profile is not None else None
            )
            if profile is not None and stage_name == "outline":
                configured_source_cap = int(
                    settings.outline_stability.max_source_prompt_tokens or 32_000
                )
                input_tokens_upper_bound = min(
                    int(profile.input_budget),
                    32_000,
                    configured_source_cap,
                )
            retry_text = str(api_config.get("transport_retries") or "").strip()
            retry_bound = max(0, int(retry_text)) if retry_text else runtime_retries
            return UnplannedProviderExposureV1(
                stage_name=stage_name,
                semantic_role=semantic_role,
                reason=reason,
                request_builder_id=request_builder_id,
                route_identity=tuple(route.identity),
                conditional_on=conditional_on,
                logical_calls_upper_bound=logical_calls_upper_bound,
                input_tokens_per_call_upper_bound=input_tokens_upper_bound,
                output_tokens_per_call_upper_bound=(
                    int(profile.max_output_tokens) if profile is not None else None
                ),
                reasoning_tokens_per_call_upper_bound=(
                    int(profile.reasoning_reserve) if profile is not None else None
                ),
                retry_attempts_per_call_upper_bound=retry_bound,
            )

        acceptance = current_acceptance_execution_context()
        aggregate_budget = (
            acceptance.provider_budget
            if acceptance is not None
            else ProviderAggregateBudgetV1(
                max_provider_calls_total=authorized_provider_call_limit()
            )
        )
        inventories: list[ProviderStageRequestInventoryV1] = []
        if "analyze" in stage_plan.requested_stages:
            if stage1_reuse_projection is not None:
                inventories.append(
                    ProviderStageRequestInventoryV1(
                        stage_name="analyze",
                        source_builder=(
                            "runtime.orchestrator.AgentRuntimeBridge._execute_analyze "
                            "verified typed reuse-only path"
                        ),
                    )
                )
            else:
                analyze_exposures: list[UnplannedProviderExposureV1] = []
                for route in route_plan.routes:
                    if (
                        route.stage != "analyze"
                        or not route.enabled
                        or not route.required
                        or not route.resolved
                    ):
                        continue
                    if route.semantic_role == "primary_reader":
                        analyze_exposures.extend((
                            unplanned_exposure(
                                stage_name="analyze",
                                semantic_role="primary_reader",
                                route=route,
                                reason="primary_reader_requests_require_source_pages_or_verified_typed_reuse",
                                request_builder_id="services.stage1_analysis_service.Stage1AnalysisService._call_reader",
                            ),
                            unplanned_exposure(
                                stage_name="analyze",
                                semantic_role="primary_reader",
                                route=route,
                                reason="summary_drift_recheck_requires_current_source_and_failed_authority",
                                request_builder_id="services.stage1_analysis_service.Stage1AnalysisService._call_reader",
                                conditional_on="source_or_summary_drift_requires_recheck",
                            ),
                        ))
                    elif route.semantic_role == "backup_reader":
                        analyze_exposures.append(
                            unplanned_exposure(
                                stage_name="analyze",
                                semantic_role="backup_reader",
                                route=route,
                                reason="backup_reader_calls_depend_on_primary_reader_failure_or_correction",
                                request_builder_id="services.stage1_analysis_service.Stage1AnalysisService._call_reader",
                                conditional_on="primary_reader_failed_or_semantic_correction_required",
                            )
                        )
                if analyze_exposures:
                    inventories.append(
                        ProviderStageRequestInventoryV1(
                            stage_name="analyze",
                            source_builder="Stage1 reader and typed-reuse admission",
                            unknown_exposures=tuple(analyze_exposures),
                        )
                    )
        if "outline" in stage_plan.requested_stages:
            semantic_reducer_call_upper_bound = (
                MAX_SEMANTIC_REDUCER_CALLS_PER_STAGE + 1
            )
            semantic_builder_by_phase = {
                "candidate_generation": "outline.v3_executor.OutlineV3Executor._run_hierarchical_candidate_generation",
                "topic": "outline.v3_executor.OutlineV3Executor._build_topic_provider_request",
                "cross_group": "outline.v3_executor.OutlineV3Executor._run_bounded_semantic_provider_call:cross_group_comparison",
                "global": "outline.v3_executor.OutlineV3Executor._run_bounded_semantic_provider_call:global_synthesis",
                "stability": "outline.v3_executor.OutlineV3Executor._run_stability_variant",
            }
            outline_route = next(
                (
                    route for route in route_plan.routes
                    if route.stage == "outline"
                    and route.semantic_role == "candidate_provider_generation"
                ),
                None,
            )
            outline_exposures: list[UnplannedProviderExposureV1] = []
            if outline_route is not None:
                semantic_output_cap = max(
                    1, int(settings.outline.semantic_output_max_tokens or 4_096)
                )
                if outline_route.enabled and outline_route.required and outline_route.resolved:
                    for phase in (
                        "candidate_generation", "topic", "cross_group", "global",
                        *(
                            ("stability",)
                            if settings.outline_stability.mode != "off" else ()
                        ),
                    ):
                        outline_exposures.append(
                            unplanned_exposure(
                                stage_name="outline",
                                semantic_role="candidate_provider_generation",
                                route=outline_route,
                                reason=f"{phase}_request_requires_frozen_stage1_or_prior_provider_outputs",
                                request_builder_id=semantic_builder_by_phase[phase],
                                conditional_on={
                                    "cross_group": "topic_synthesis_completed",
                                    "global": "cross_group_completed",
                                    "stability": "outline_stability_mode_enabled",
                                }.get(phase, ""),
                                logical_calls_upper_bound=(
                                    semantic_reducer_call_upper_bound
                                    if phase in {"cross_group", "global"}
                                    else None
                                ),
                                output_tokens_upper_bound=(
                                    4_096
                                    if phase in {"candidate_generation", "stability"}
                                    else semantic_output_cap
                                ),
                            )
                        )
                    semantic_repair_enabled = str(
                        config.get("OutlineStability", {}).get("semantic_repair_enabled") or ""
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if semantic_repair_enabled:
                        outline_exposures.append(
                            unplanned_exposure(
                                stage_name="outline",
                                semantic_role="candidate_provider_generation",
                                route=outline_route,
                                reason="primary_candidate_semantic_repair_requires_structural_validation_failure",
                                request_builder_id="outline.v3_executor.OutlineV3Executor._semantic_repair_candidate",
                                conditional_on="candidate_contract_validation_failed",
                                logical_calls_upper_bound=max(0, int(settings.outline.candidate_count)),
                            )
                        )
            role_reasons = {
                role: values
                for (stage, role), values in _FULL_STAGE_BUILDER_EXPOSURE.items()
                if stage == "outline"
            }
            for role, (reason, conditional_on) in role_reasons.items():
                route = next(
                    (
                        item for item in route_plan.routes
                        if item.stage == "outline" and item.semantic_role == role
                    ),
                    None,
                )
                if route is None or not route.enabled or not route.required or not route.resolved:
                    continue
                outline_exposures.append(
                    unplanned_exposure(
                        stage_name="outline",
                        semantic_role=role,
                        route=route,
                        reason=reason,
                        request_builder_id=_FULL_STAGE_REQUEST_BUILDER_IDS.get(
                            ("outline", role), ""
                        ),
                        conditional_on=conditional_on,
                        logical_calls_upper_bound=_FULL_STAGE_LOGICAL_CALL_UPPER_BOUNDS.get(
                            ("outline", role)
                        ),
                    )
                )
            if outline_exposures:
                inventories.append(
                    ProviderStageRequestInventoryV1(
                        stage_name="outline",
                        source_builder="outline.v3_executor semantic and provider request builders",
                        unknown_exposures=tuple(outline_exposures),
                    )
                )
        if "review" in stage_plan.requested_stages:
            writer_route = next(
                (
                    route for route in route_plan.routes
                    if route.stage == "review" and route.semantic_role == "writer"
                ),
                None,
            )
            if writer_route is not None and writer_route.enabled and writer_route.required and writer_route.resolved:
                inventories.append(
                    ProviderStageRequestInventoryV1(
                        stage_name="review",
                        source_builder="services.review_generation_service writer packet builder",
                        unknown_exposures=(
                            unplanned_exposure(
                                stage_name="review", semantic_role="writer",
                                route=writer_route,
                                reason="writer_packets_require_adopted_outline_and_current_catalog",
                            ),
                            unplanned_exposure(
                                stage_name="review", semantic_role="writer",
                                route=writer_route,
                                reason="review_drift_rewrite_requires_approved_repair_scope",
                                conditional_on="review_drift_requires_writer_regeneration",
                            ),
                        ),
                    )
                )
        if "validate" in stage_plan.requested_stages:
            validator_route = next(
                (
                    route for route in route_plan.routes
                    if route.stage == "validate" and route.semantic_role == "validator"
                ),
                None,
            )
            if validator_route is not None and validator_route.enabled and validator_route.required and validator_route.resolved:
                inventories.append(
                    ProviderStageRequestInventoryV1(
                        stage_name="validate",
                        source_builder="validation.llm_adjudicator request builder",
                        unknown_exposures=(
                            unplanned_exposure(
                                stage_name="validate",
                                semantic_role="validator",
                                route=validator_route,
                                reason="primary_validation_requests_require_current_draft_manifest_and_source_binding",
                            ),
                            unplanned_exposure(
                                stage_name="validate",
                                semantic_role="validator",
                                route=validator_route,
                                reason="validation_recheck_request_depends_on_current_draft_manifest_and_approved_repair",
                                conditional_on="finding_and_approved_repair_requires_model_recheck",
                            ),
                        ),
                    )
                )
        projection = build_full_stage_request_plan_v1(
            stage_plan=stage_plan,
            reachable_route_plan=route_plan,
            stage_inventories=inventories,
            aggregate_budget=aggregate_budget,
            local_steps=(
                "source_intake", "outline_adoption", "citation_assembly",
                "docx_render", "current_artifact_set", "verified_export",
            ),
        )
        projection["projection_identity_hash"] = hash_json(
            {
                "action": stage_plan.action,
                "stage_plan": stage_plan.to_dict(),
                "reachable_route_plan": route_plan.to_dict(),
                "provider_requests": projection.get("provider_requests") or [],
                "unknown_exposures": projection.get("unknown_exposures") or [],
                "stage1_reuse_authority": stage1_reuse_projection,
                "suppressed_provider_routes": suppressed_provider_routes,
                "aggregate_budget": aggregate_budget.to_dict(),
            }
        )
        projection["runtime_spec_hash"] = _canonical_hash(spec.to_dict())
        projection["config_sha256"] = file_sha256(spec.config)
        projection["aggregate_budget_source"] = (
            "bound_acceptance_run" if acceptance is not None
            else "application_call_cap_projection_without_bound_acceptance_run"
        )
        projection["unbound_aggregate_limits_are_unknown"] = acceptance is None
        projection["stage1_reuse_authority_status"] = (
            "verified_typed_manifest_summary_only_input"
            if stage1_reuse_projection is not None
            else "not_verified_by_spec_alone"
            if spec.to_job_request().reuse_stage1
            else "not_requested"
        )
        if stage1_reuse_projection is not None:
            projection["stage1_reuse_authority"] = stage1_reuse_projection
            projection["suppressed_provider_routes"] = suppressed_provider_routes
        projection["provider_calls"] = "not executed"
        return projection

    def _verified_typed_reuse_only_stage1_input(
        self, spec: RuntimeJobSpec
    ) -> dict[str, Any] | None:
        """Verify Stage 1 authorities for a summary-only run with no PDF work items."""

        request = spec.to_job_request()
        if (
            str(spec.action) not in {"analyze", "run_all", "retry_failed"}
            or str(spec.source.mode or "").strip().lower() != "direct"
            or not request.reuse_summary_files
            or request.summary_file
            or request.summary_sources
        ):
            return None
        source_bundle = build_source_bundle_for_request(
            request,
            project_name=spec.project_name,
        )
        if source_bundle.paper_work_items:
            return None

        summaries: list[dict[str, Any]] = []
        for path in request.reuse_summary_files:
            summaries.extend(
                InternalStageExecutorRegistry._summary_payloads_from_file(path)
            )
        if not summaries:
            return None

        typed_flags = [
            isinstance(item.get("stage1_reuse"), Mapping)
            and str(item["stage1_reuse"].get("authority_kind") or "").strip()
            == "typed_manifest"
            for item in summaries
        ]
        if not any(typed_flags):
            return None
        if not all(typed_flags):
            raise ControlPlaneError(
                "reuse-only Analyze input mixes typed and non-typed Stage 1 authorities"
            )

        authorities: list[dict[str, str]] = []
        for index, summary in enumerate(summaries):
            reuse = summary["stage1_reuse"]
            raw_binding = reuse.get("binding")
            binding = Stage1ReusableSummaryBindingV1.from_mapping(
                raw_binding if isinstance(raw_binding, Mapping) else None
            )
            authority, reason = verify_stage1_typed_manifest_authority(
                summary,
                binding,
            )
            if authority is None:
                raise ControlPlaneError(
                    f"reuse-only Stage 1 typed authority failed at summary {index}: {reason}"
                )
            authorities.append(
                {
                    "canonical_paper_key": authority.manifest.canonical_paper_key,
                    "manifest_file_hash": authority.manifest_file_hash,
                    "source_summary_artifact_hash": authority.manifest.source_summary_artifact_hash,
                }
            )
        paper_keys = [item["canonical_paper_key"] for item in authorities]
        if len(paper_keys) != len(set(paper_keys)):
            raise ControlPlaneError(
                "reuse-only Stage 1 typed authorities contain duplicate paper identities"
            )
        return {
            "reuse_manifest_file_count": len(request.reuse_summary_files),
            "canonical_summary_count": len(summaries),
            "typed_manifest_authority_count": len(authorities),
            "authority_set_hash": hash_json(
                sorted(authorities, key=lambda item: item["canonical_paper_key"])
            ),
            "paper_work_item_count": 0,
            "provider_calls_upper_bound": 0,
            "current_binding_comparison": "not_run_no_stage1_paper_work_items",
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
        try:
            payload["full_stage_request_plan"] = self._full_stage_request_plan_for_spec(spec)
        except Exception as exc:
            payload["full_stage_request_plan"] = {
                "schema_version": "full-stage-provider-request-plan-v1",
                "status": "blocked_before_request_inventory",
                "reason": str(exc),
                "error_type": type(exc).__name__,
                "provider_calls": "not executed",
                "unknown_exposure": True,
            }
        full_stage = payload["full_stage_request_plan"]
        budget_status = full_stage.get("budget_status")
        payload["provider_admission_status"] = (
            str(budget_status.get("admission") or "unknown")
            if isinstance(budget_status, Mapping)
            else "blocked_before_request_inventory"
        )
        payload["ready_for_provider_admission"] = bool(
            payload["provider_admission_status"] == "within_budget"
            and full_stage.get("aggregate_budget_source") == "bound_acceptance_run"
        )
        return payload

    def chunk_plan(
        self,
        summary_files: Sequence[str | Path],
        *,
        job_id: str = "chunk-plan",
        candidate_count: int | None = None,
        physical_call_limit: int | None = None,
        output_path: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Build navigation and route-bound semantic request plans without provider calls."""

        if not summary_files:
            raise ControlPlaneError("chunk-plan requires at least one --summary-file")
        output_resolved = (
            Path(output_path).expanduser().resolve(strict=False)
            if output_path
            else None
        )
        loaded_config: dict[str, dict[str, str]] | None = None
        configured_settings: ApplicationSettings | None = None
        route_profile = None
        role_router = None
        role_route_summaries: dict[str, dict[str, Any]] = {}
        reachable_outline = None
        reachable_outline_roles: frozenset[str] | None = None
        route_transport_retries: int | None = None
        route_identity_hash = ""
        route_plan_status = "not_planned_config_missing"
        route_plan_diagnostic = ""
        resolved_config_path = (
            Path(config_path).expanduser().resolve()
            if config_path
            else (self.repo_root / "config.ini").resolve()
        )
        if resolved_config_path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(resolved_config_path, encoding="utf-8")
                loaded_config = {
                    section: {key: value for key, value in parser.items(section)}
                    for section in parser.sections()
                }
                configured_settings = ApplicationSettings.from_config(loaded_config)
                outline_models = loaded_config.get("OutlineModels", {})
                route_section = str(outline_models.get("outline_model") or "Outline_API").strip()
                from services.model_capabilities import resolve_model_capability
                from services.model_selection import get_api_config_for_section
                from runtime.provider_context import ProviderContextProfile

                api_config = get_api_config_for_section(loaded_config, route_section)
                model = str(api_config.get("model") or "").strip()
                if not model:
                    route_plan_status = "not_planned_route_missing"
                    route_plan_diagnostic = f"configured semantic route [{route_section}] has no model"
                else:
                    capability = resolve_model_capability(api_config)
                    runtime_retries = loaded_config.get("Runtime", {}).get(
                        "transport_retries"
                    )
                    runtime_transport_retries = max(
                        0, int(str(runtime_retries or "2"))
                    )
                    configured_retries = str(
                        api_config.get("transport_retries") or ""
                    ).strip()
                    route_transport_retries = max(
                        0,
                        int(configured_retries) if configured_retries else runtime_transport_retries,
                    )
                    profile = ProviderContextProfile.conservative(
                        provider=capability.provider_family,
                        model=model,
                        endpoint_type=capability.endpoint_type,
                        model_context_limit=max(1, int(api_config.get("max_context_tokens") or 128_000)),
                        max_output_tokens=max(1, int(api_config.get("max_output_tokens") or 4_096)),
                        reasoning_reserve=max(0, int(api_config.get("reasoning_reserve_tokens") or 2_048)),
                        safety_margin=max(0, int(api_config.get("safety_margin_tokens") or 1_024)),
                    )
                    route_profile = profile
                    from outline.provider_router import OutlineRoleRoute, build_outline_provider_router

                    reachable_outline = build_reachable_provider_route_plan(
                        loaded_config,
                        action="generate_outline",
                        requested_stages=("outline",),
                        free_mode_enabled=False,
                    )
                    reachable_outline_roles = frozenset(reachable_outline.semantic_roles)

                    def reject_shadow_transport(_node_id: str, _request: Mapping[str, Any]) -> Any:
                        raise ControlPlaneError("provider-free chunk-plan attempted transport")

                    def resolve_shadow_route(role: str, section_name: str) -> OutlineRoleRoute | None:
                        role_config = get_api_config_for_section(loaded_config, section_name)
                        role_model = str(role_config.get("model") or "").strip()
                        if not role_model:
                            return None
                        role_capability = resolve_model_capability(role_config)
                        output_tokens = max(1, int(role_config.get("max_output_tokens") or 4_096))
                        if role == "evidence_critique":
                            output_tokens = min(output_tokens, 16_000)
                        role_profile = ProviderContextProfile.conservative(
                            provider=role_capability.provider_family,
                            model=role_model,
                            endpoint_type=role_capability.endpoint_type,
                            model_context_limit=max(1, int(role_config.get("max_context_tokens") or 128_000)),
                            max_output_tokens=output_tokens,
                            reasoning_reserve=max(0, int(role_config.get("reasoning_reserve_tokens") or 2_048)),
                            safety_margin=max(0, int(role_config.get("safety_margin_tokens") or 1_024)),
                        )
                        route_config = dict(role_config)
                        if str(route_config.get("transport_retries") or "").strip() == "":
                            route_config["transport_retries"] = runtime_transport_retries
                        return OutlineRoleRoute(
                            role=role,
                            config_section=section_name,
                            provider_name=role_capability.provider_family,
                            model=role_model,
                            endpoint_type=role_capability.endpoint_type,
                            profile=role_profile,
                            transport=reject_shadow_transport,
                            api_base=str(role_config.get("api_base") or ""),
                            config_identity=route_config,
                        )

                    role_router = build_outline_provider_router(
                        settings=configured_settings,
                        config=loaded_config,
                        enabled_roles=reachable_outline_roles,
                        route_resolver=resolve_shadow_route,
                    )
                    missing_roles = reachable_outline_roles - set(role_router.routes)
                    if missing_roles:
                        route_plan_status = "not_planned_route_missing"
                        route_plan_diagnostic = "missing configured Outline roles: " + ", ".join(sorted(missing_roles))
                    else:
                        role_route_summaries = {
                            role: {
                                "config_section": route.config_section,
                                "model": route.model,
                                "transport_retry_reserve": int(route.config_identity.get("transport_retries") or 0),
                                "profile_limits": {
                                    "model_context_limit": route.profile.model_context_limit,
                                    "input_budget": route.profile.input_budget,
                                    "max_output_tokens": route.profile.max_output_tokens,
                                    "reasoning_reserve": route.profile.reasoning_reserve,
                                    "safety_margin": route.profile.safety_margin,
                                },
                                "route_fingerprint": route.safe_config_fingerprint(),
                            }
                            for role, route in sorted(role_router.routes.items())
                        }
                        route_identity_hash = hash_json({
                            role: route.safe_config_fingerprint()
                            for role, route in sorted(role_router.routes.items())
                        })
                        route_plan_status = "ready"
            except (OSError, UnicodeError, configparser.Error, TypeError, ValueError) as exc:
                if config_path:
                    raise ControlPlaneError(f"cannot load chunk-plan route config: {exc}") from exc
                route_plan_status = "not_planned_config_invalid"
                route_plan_diagnostic = "the repository default config could not resolve an Outline route"
        elif config_path:
            raise ControlPlaneError(f"config file does not exist: {resolved_config_path}")

        effective_candidate_count = int(
            candidate_count
            if candidate_count is not None
            else configured_settings.outline.candidate_count
            if configured_settings is not None
            else 3
        )
        configured_call_limit = (
            configured_settings.outline_stability.max_provider_calls
            if configured_settings is not None
            else DEFAULT_PROVIDER_CALL_BUDGET
        )
        requested_call_limit = (
            int(physical_call_limit)
            if physical_call_limit is not None
            else int(configured_call_limit)
        )
        effective_call_limit = authorized_provider_call_limit(
            min(requested_call_limit, int(configured_call_limit))
        )
        if loaded_config is not None:
            effective_candidate_count = int(
                candidate_count
                if candidate_count is not None
                else loaded_config.get("Outline", {}).get("candidate_count") or 3
            )
            if physical_call_limit is None:
                effective_call_limit = authorized_provider_call_limit(
                    int(
                        loaded_config.get("OutlineStability", {}).get("max_provider_calls")
                        or DEFAULT_PROVIDER_CALL_BUDGET
                    )
                )
        semantic_request_plan: list[dict[str, Any]] = []
        topic_request_plan_identity_hash = ""
        semantic_preflight_status = "NOT_PLANNED"
        semantic_preflight_diagnostic = route_plan_diagnostic
        semantic_request_count = 0
        semantic_reserved_call_count: int | None = None
        semantic_request_input_tokens: int | None = None
        semantic_request_output_tokens: int | None = None
        semantic_request_budget_status = "NOT_PLANNED"
        semantic_route_preflight_summary: dict[str, Any] = {}
        summaries: list[dict[str, Any]] = []
        source_paths: list[str] = []
        typed_manifest_authorities: list[dict[str, str]] = []
        for raw_path in summary_files:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file():
                raise ControlPlaneError(f"summary file does not exist: {path}")
            if output_resolved is not None and path == output_resolved:
                raise ControlPlaneError(
                    "chunk-plan output must differ from every read-only summary input"
                )
            try:
                root_payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(root_payload, Mapping)
                    and root_payload.get("artifact_type") == "summary_source_manifest"
                ):
                    _manifest, materialized_path, _raw_rows = load_summary_source_manifest(path)
                    if output_resolved is not None and materialized_path == output_resolved:
                        raise ControlPlaneError(
                            "chunk-plan output must differ from the manifest materialized summary"
                        )
                    rows = InternalStageExecutorRegistry._summary_payloads_from_file(
                        materialized_path
                    )
                    source_paths.append(str(materialized_path))
                else:
                    rows = InternalStageExecutorRegistry._summary_payloads_from_file(path)
            except ControlPlaneError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, TypeError, ValueError) as exc:
                raise ControlPlaneError(f"cannot load canonical summary source {path}: {exc}") from exc
            for row in rows:
                reuse = row.get("stage1_reuse")
                if not isinstance(reuse, Mapping) or str(reuse.get("authority_kind") or "") != "typed_manifest":
                    continue
                binding_payload = reuse.get("binding")
                if not isinstance(binding_payload, Mapping) or not binding_payload:
                    raise ControlPlaneError(
                        f"typed Stage1 summary at {path} has no verified reuse binding"
                    )
                try:
                    binding = Stage1ReusableSummaryBindingV1.from_mapping(binding_payload)
                    authority, reason = verify_stage1_typed_manifest_authority(row, binding)
                except (OSError, UnicodeError, TypeError, ValueError, RuntimeError) as exc:
                    raise ControlPlaneError(
                        f"typed Stage1 authority verification failed for {path}: {exc}"
                    ) from exc
                if authority is None:
                    raise ControlPlaneError(
                        f"typed Stage1 authority verification failed for {path}: {reason}"
                    )
                typed_manifest_authorities.append(
                    {
                        "manifest_file_hash": authority.manifest_file_hash,
                        "source_summary_artifact_hash": authority.manifest.source_summary_artifact_hash,
                    }
                )
            summaries.extend(dict(item) for item in rows)
            if str(path) not in source_paths:
                source_paths.append(str(path))
        evidence = build_outline_evidence_views(summaries, str(job_id))
        ledger = build_global_corpus_ledger(evidence)
        matrix = build_multi_view_matrix(evidence)
        relation_map = build_global_relation_map(evidence, matrix, ledger)
        content_layers = build_paper_content_layers(summaries, evidence, job_id=str(job_id))
        reuse_inventory = [
            ReuseInventoryItem(
                source_path=path,
                content_hash=content_layers.content_hash,
                source_node="stage1_canonical_summaries",
                content_status="read_only_input",
                evidence_completeness="complete" if not content_layers.blocking_diagnostics else "partial",
                new_node_usage="outline_content_layers -> semantic_chunk_plan",
                disposition="direct_reuse",
                reason="canonical Stage 1 summaries are projected locally; provider calls are not emitted",
            )
            for path in source_paths
        ]
        semantic_plan = build_semantic_chunk_plan(
            content_layers,
            relation_map,
            candidate_count=effective_candidate_count,
            physical_call_limit=effective_call_limit,
            reuse_inventory=reuse_inventory,
        )
        claim_keys: set[tuple[str, str]] = set()
        source_claim_identity_hashes: set[str] = set()
        unbound_source_claim_identity_hashes: set[str] = set()
        paper_level_claim_keys: set[tuple[str, str]] = set()
        study_level_claim_keys: set[tuple[str, str]] = set()
        unresolved_scope_claim_keys: set[tuple[str, str]] = set()
        paper_level_fallback_unit_keys: set[tuple[str, str]] = set()
        explicit_study_unit_keys: set[tuple[str, str]] = set()
        unresolved_study_unit_keys: set[tuple[str, str]] = set()
        multi_study_mapping_unresolved_papers: set[str] = set()
        source_field_ledger_scope_counts: dict[str, int] = {}
        source_field_ledger_top_level_path_counts: dict[str, int] = {}
        evidence_keys: set[tuple[str, str]] = set()
        source_text_occurrences: list[tuple[str, str]] = []

        def add_source_text(paper_id: str, value: Any) -> None:
            if isinstance(value, str) and value.strip():
                normalized = " ".join(value.split())
                source_text_occurrences.append((paper_id, normalized))
            elif isinstance(value, Mapping):
                for child in value.values():
                    add_source_text(paper_id, child)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    add_source_text(paper_id, child)

        for dossier in content_layers.dossiers:
            paper_id = str(dossier.paper_id)
            explicit_study_unit_ids: set[str] = set()
            explicit_study_claim_ids: set[str] = set()
            paper_level_unit_ids: set[str] = set()
            explicit_units: set[str] = set()
            paper_fallback_units: set[str] = set()
            unresolved_units: set[str] = set()
            for unit in dossier.research_units:
                unit_id = str(unit.study_id or "")
                source_study_id = str(unit.source_study_id or "")
                locators = unit.source_locators if isinstance(unit.source_locators, Mapping) else {}
                has_study_locator = bool(locators.get("study"))
                explicit_unit = bool(source_study_id) or bool(
                    has_study_locator
                    and unit_id
                    and not unit_id.endswith(":study:paper_level")
                )
                if explicit_unit:
                    explicit_units.add(unit_id or source_study_id)
                    explicit_study_unit_ids.update(
                        value for value in (unit_id, source_study_id) if value
                    )
                    explicit_study_claim_ids.update(
                        str(claim.claim_id) for claim in unit.claims if str(claim.claim_id)
                    )
                elif unit_id.endswith(":study:paper_level"):
                    paper_fallback_units.add(unit_id)
                    paper_level_unit_ids.add(unit_id)
                else:
                    unresolved_units.add(unit_id or f"unidentified:{paper_id}")
            explicit_study_unit_keys.update((paper_id, unit_id) for unit_id in explicit_units)
            paper_level_fallback_unit_keys.update((paper_id, unit_id) for unit_id in paper_fallback_units)
            unresolved_study_unit_keys.update((paper_id, unit_id) for unit_id in unresolved_units)
            if "multi_study_mapping_unresolved" in set(dossier.diagnostics):
                multi_study_mapping_unresolved_papers.add(paper_id)
            for entry in dossier.source_field_ledger:
                scope = str(entry.scope or "unresolved")
                source_field_ledger_scope_counts[scope] = (
                    source_field_ledger_scope_counts.get(scope, 0) + 1
                )
                top_level_path = str(entry.source_path or "").split(".", 1)[0]
                source_field_ledger_top_level_path_counts[top_level_path] = (
                    source_field_ledger_top_level_path_counts.get(top_level_path, 0) + 1
                )
            for field_name in (
                "overall_context",
                "research_questions",
                "concept_definitions",
                "operationalizations",
                "theoretical_derivation",
                "findings",
                "mechanism_evidence",
                "moderators_boundaries",
                "zero_results",
                "limitations",
            ):
                add_source_text(paper_id, getattr(dossier, field_name, ()))
            for claim in dossier.claims:
                claim_id = str(claim.claim_id)
                key = (paper_id, claim_id)
                claim_keys.add(key)
                source_claim_identity_hashes.add(
                    hash_json({"paper_key": paper_id, "claim_id": claim_id})
                )
                if not claim.evidence_ids:
                    unbound_source_claim_identity_hashes.add(
                        hash_json({"paper_key": paper_id, "claim_id": claim_id})
                    )
                claim_study_id = str(claim.study_id or "")
                if claim_study_id in explicit_study_unit_ids or claim_id in explicit_study_claim_ids:
                    study_level_claim_keys.add(key)
                elif claim_study_id and claim_study_id not in paper_level_unit_ids:
                    unresolved_scope_claim_keys.add(key)
                else:
                    paper_level_claim_keys.add(key)
                add_source_text(paper_id, claim.text)
                evidence_keys.update((paper_id, str(value)) for value in claim.evidence_ids if str(value))
            for unit in dossier.research_units:
                for field_name in (
                    "research_questions",
                    "definitions_and_operationalizations",
                    "theoretical_derivation",
                    "method",
                    "sample_or_context",
                    "findings",
                    "mechanisms",
                    "moderators_or_boundaries",
                    "zero_results",
                    "limitations",
                ):
                    add_source_text(paper_id, getattr(unit, field_name, ()))
                for claim in unit.claims:
                    claim_id = str(claim.claim_id)
                    key = (paper_id, claim_id)
                    claim_keys.add(key)
                    source_claim_identity_hashes.add(
                        hash_json({"paper_key": paper_id, "claim_id": claim_id})
                    )
                    if not claim.evidence_ids:
                        unbound_source_claim_identity_hashes.add(
                            hash_json({"paper_key": paper_id, "claim_id": claim_id})
                        )
                    if claim_id in explicit_study_claim_ids:
                        study_level_claim_keys.add(key)
                    elif unit.study_id in paper_level_unit_ids:
                        paper_level_claim_keys.add(key)
                    else:
                        unresolved_scope_claim_keys.add(key)
                    add_source_text(paper_id, claim.text)
                    evidence_keys.update(
                        (paper_id, str(value))
                        for value in claim.evidence_ids
                        if str(value)
                    )
                evidence_keys.update(
                    (paper_id, str(value)) for value in unit.evidence_ids if str(value)
                )
            for values in dossier.evidence_ids_by_field.values():
                evidence_keys.update((paper_id, str(value)) for value in values if str(value))
            evidence_keys.update(
                (paper_id, str(value))
                for value in dossier.evidence_text_by_id
                if str(value)
            )
            add_source_text(paper_id, list(dossier.evidence_text_by_id.values()))

        study_level_claim_keys.difference_update(unresolved_scope_claim_keys)
        paper_level_claim_keys.difference_update(
            study_level_claim_keys | unresolved_scope_claim_keys
        )

        unique_source_text_by_provenance = {
            (paper_id, text_value.casefold()): text_value
            for paper_id, text_value in source_text_occurrences
        }
        unique_source_texts = list(unique_source_text_by_provenance.values())
        topic_membership_hashes = [
            hash_json(sorted(topic.paper_ids)) for topic in semantic_plan.topics
        ]
        topic_task_hashes = [
            hash_json(
                {
                    "dimensions": sorted(topic.dimensions),
                    "question": " ".join(topic.question.split()).casefold(),
                    "members": sorted(topic.paper_ids),
                }
            )
            for topic in semantic_plan.topics
        ]
        topic_dimension_membership_hashes = [
            hash_json(
                {
                    "dimensions": sorted(topic.dimensions),
                    "members": sorted(topic.paper_ids),
                }
            )
            for topic in semantic_plan.topics
        ]
        source_hashes = sorted(set(content_layers.source_summary_hashes))
        source_field_ledger_total = sum(source_field_ledger_scope_counts.values())
        audit_metadata_paths = {"stage1_reuse", "status", "source_mode", "provider"}
        source_field_ledger_audit_metadata_count = sum(
            source_field_ledger_top_level_path_counts.get(path, 0)
            for path in audit_metadata_paths
        )
        source_field_ledger_bibliographic_count = source_field_ledger_top_level_path_counts.get(
            "paper_info", 0
        )
        source_field_ledger_summary_content_count = source_field_ledger_top_level_path_counts.get(
            "ai_summary", 0
        )
        workload_audit = {
            "artifact_type": "r1_request_workload_audit",
            "artifact_version": "v2",
            "source_scope": (
                "provider_free_typed_stage1_reuse_projection"
                if summaries and len(typed_manifest_authorities) == len(summaries)
                else "provider_free_mixed_stage1_summary_projection"
                if typed_manifest_authorities
                else "provider_free_materialized_summary_projection"
            ),
            "typed_manifest_authority_count": len(typed_manifest_authorities),
            "paper_count": len(content_layers.index_cards),
            "study_unit_count": sum(len(item.research_units) for item in content_layers.dossiers),
            "study_unit_count_kind": "explicit_study_units_plus_paper_level_fallbacks",
            "explicit_study_unit_count": len(explicit_study_unit_keys),
            "paper_level_fallback_unit_count": len(paper_level_fallback_unit_keys),
            "unresolved_study_unit_count": len(unresolved_study_unit_keys),
            "multi_study_mapping_unresolved_dossier_count": len(multi_study_mapping_unresolved_papers),
            "paper_level_source_claim_count": len(paper_level_claim_keys),
            "study_level_source_claim_count": len(study_level_claim_keys),
            "explicit_study_source_claim_count": len(study_level_claim_keys),
            "unresolved_scope_source_claim_count": len(unresolved_scope_claim_keys),
            "source_field_ledger_scope_counts": dict(sorted(source_field_ledger_scope_counts.items())),
            "source_field_ledger_top_level_path_counts": dict(
                sorted(source_field_ledger_top_level_path_counts.items())
            ),
            "source_field_ledger_total": source_field_ledger_total,
            "unresolved_source_field_count": source_field_ledger_scope_counts.get("unresolved", 0),
            "source_field_ledger_audit_metadata_count": source_field_ledger_audit_metadata_count,
            "source_field_ledger_bibliographic_count": source_field_ledger_bibliographic_count,
            "source_field_ledger_summary_content_count": source_field_ledger_summary_content_count,
            "source_field_ledger_other_origin_count": max(
                0,
                source_field_ledger_total
                - source_field_ledger_audit_metadata_count
                - source_field_ledger_bibliographic_count
                - source_field_ledger_summary_content_count,
            ),
            "source_claim_count_unique_by_paper": len(claim_keys),
            "source_claim_identity_set_hash": hash_json(sorted(source_claim_identity_hashes)),
            "unbound_source_claim_count": len(unbound_source_claim_identity_hashes),
            "unbound_source_claim_identity_set_hash": hash_json(
                sorted(unbound_source_claim_identity_hashes)
            ),
            "evidence_id_count_unique_by_paper": len(evidence_keys),
            "evidence_identity_set_hash": hash_json(
                sorted(
                    hash_json({"paper_key": paper_id, "evidence_id": evidence_id})
                    for paper_id, evidence_id in evidence_keys
                )
            ),
            "source_summary_hash_count": len(source_hashes),
            "source_summary_hashes": source_hashes,
            "source_text_occurrence_count_across_dossier_fields": len(source_text_occurrences),
            "source_text_unique_value_count_by_paper": len(unique_source_text_by_provenance),
            "source_text_repeated_occurrence_count": (
                len(source_text_occurrences) - len(unique_source_text_by_provenance)
            ),
            "source_text_total_character_count": sum(
                len(value) for _paper, value in source_text_occurrences
            ),
            "source_text_unique_character_count_by_paper": sum(
                len(value) for value in unique_source_texts
            ),
            "source_text_token_estimate": (
                int(route_profile.estimate_tokens(unique_source_texts))
                if route_profile is not None
                else None
            ),
            "tokenizer_identity": (
                str(route_profile.tokenizer_strategy)
                if route_profile is not None
                else "not_available_without_route_config"
            ),
            "token_estimate_uncertainty": "provider-specific estimate; serialized request plan is authoritative when present",
            "topic_count": len(semantic_plan.topics),
            "unique_topic_membership_set_count": len(set(topic_membership_hashes)),
            "topics_sharing_membership_set_count": (
                len(topic_membership_hashes) - len(set(topic_membership_hashes))
            ),
            "unique_topic_task_count": len(set(topic_task_hashes)),
            "topics_sharing_same_task_count": len(topic_task_hashes) - len(set(topic_task_hashes)),
            "unique_topic_dimension_membership_shape_count": len(
                set(topic_dimension_membership_hashes)
            ),
            "topics_sharing_dimension_membership_shape_count": (
                len(topic_dimension_membership_hashes)
                - len(set(topic_dimension_membership_hashes))
            ),
            "topic_merge_policy": "keep dimension-distinct tasks separate; order same-paper tasks together for wire-unit reuse",
            "topics": [
                {
                    "topic_id_hash": hash_json(topic.topic_id),
                    "question_hash": hash_json(" ".join(topic.question.split())),
                    "membership_hash": hash_json(sorted(topic.paper_ids)),
                    "member_paper_count": len(topic.paper_ids),
                    "source_dimensions": sorted(topic.dimensions),
                    "required_evidence_id_count": len(topic.required_evidence_ids),
                }
                for topic in semantic_plan.topics
            ],
            "content_layers_hash": content_layers.content_hash,
            "typed_manifest_authority_hashes": typed_manifest_authorities,
            "provider_posts_emitted": 0,
            "live_provider_requests_emitted": 0,
            "private_source_text_written": False,
        }
        if (
            route_profile is not None
            and configured_settings is not None
            and route_plan_status == "ready"
            and reachable_outline is not None
        ):
            try:
                from outline.v3_executor import OutlineV3Executor

                outline_settings = loaded_config.get("Outline", {}) if loaded_config else {}
                stability_settings = (
                    loaded_config.get("OutlineStability", {}) if loaded_config else {}
                )
                parsed_stability = configured_settings.outline_stability_settings()
                semantic_repair_enabled = str(
                    stability_settings.get("semantic_repair_enabled") or ""
                ).strip().lower() in {"1", "true", "yes", "on"}
                opaque_alias_enabled = str(
                    stability_settings.get("opaque_alias_enabled") or ""
                ).strip().lower() in {"1", "true", "yes", "on"}
                source_token_limit = int(
                    stability_settings.get("max_source_prompt_tokens") or 0
                )
                technical_target = int(outline_settings.get("technical_shard_target_tokens") or 0)
                with tempfile.TemporaryDirectory(prefix="reviewctl-semantic-plan-") as temp_root:
                    workspace = JobWorkspace.create(
                        temp_root,
                        "chunk-plan",
                        job_id=f"chunk-plan-{hashlib.sha256(str(job_id).encode()).hexdigest()[:12]}",
                    )
                    registry = ArtifactRegistry(
                        workspace.paths.registry_path,
                        workspace.job_id,
                    )
                    executor = OutlineV3Executor(
                        job_id=workspace.job_id,
                        summaries=summaries,
                        workspace=workspace,
                        artifact_registry=registry,
                        provider_profile=route_profile,
                        provider_router=role_router,
                        enabled_semantic_roles=reachable_outline_roles,
                        reachable_provider_route_plan=reachable_outline.to_dict(),
                        candidate_count=effective_candidate_count,
                        stability_mode=parsed_stability.mode,
                        semantic_repair_enabled=semantic_repair_enabled,
                        opaque_alias_enabled=opaque_alias_enabled,
                        max_provider_calls=effective_call_limit,
                        max_estimated_cost=parsed_stability.max_estimated_cost,
                        max_estimated_total_tokens=parsed_stability.max_estimated_total_tokens,
                        pricing_source=parsed_stability.pricing_source or None,
                        pricing_provider=parsed_stability.pricing_provider or None,
                        pricing_model=parsed_stability.pricing_model or None,
                        pricing_version=parsed_stability.pricing_version or None,
                        pricing_effective_date=parsed_stability.pricing_effective_date or None,
                        estimated_cost_per_1k_tokens=parsed_stability.estimated_cost_per_1k_tokens,
                        input_cost_per_1k_tokens=parsed_stability.input_cost_per_1k_tokens,
                        output_cost_per_1k_tokens=parsed_stability.output_cost_per_1k_tokens,
                        reasoning_cost_per_1k_tokens=parsed_stability.reasoning_cost_per_1k_tokens,
                        cache_read_cost_per_1k_tokens=parsed_stability.cache_read_cost_per_1k_tokens,
                        cache_write_cost_per_1k_tokens=parsed_stability.cache_write_cost_per_1k_tokens,
                        max_smoke_overhead_ratio=parsed_stability.max_smoke_overhead_ratio,
                        max_source_prompt_tokens=source_token_limit or None,
                        semantic_output_max_tokens=(
                            configured_settings.outline.semantic_output_max_tokens
                            if configured_settings is not None else 4_096
                        ),
                        semantic_transport_retries=route_transport_retries,
                        technical_shard_target_tokens=technical_target,
                    )
                    if executor.semantic_provider_synthesis_enabled:
                        preflight_error = ""
                        try:
                            executor._preflight_stability_budget()
                        except RuntimeError as exc:
                            # Admission rejection still has a useful, fully
                            # materialized request plan. Preserve that plan for
                            # review while keeping all provider posts at zero.
                            preflight_error = str(exc)
                        semantic_request_plan = [
                            dict(item) for item in executor.semantic_request_plan
                        ]
                        topic_request_plan_identity_hash = (
                            executor._compute_topic_provider_plan_identity_hash(
                                semantic_request_plan
                            )
                        )
                        generation_identity = role_route_summaries.get(
                            "candidate_provider_generation", {}
                        )
                        for row in semantic_request_plan:
                            if str(row.get("node_id") or "").startswith((
                                "topic_synthesis_provider:",
                                "cross_group_comparison_provider:",
                                "global_synthesis_provider:",
                            )) or str(row.get("node_id") or "") in {
                                "cross_group_comparison_provider",
                                "global_synthesis_provider",
                            }:
                                row["role"] = "candidate_provider_generation"
                                row["route_identity"] = {
                                    "config_section": generation_identity.get("config_section", ""),
                                    "model": generation_identity.get("model", ""),
                                }
                        semantic_preflight_status = str(
                            executor.stability_preflight.get("preflight_status")
                            or ("rejected" if preflight_error else "planned")
                        )
                        semantic_route_preflight_summary = {
                            "scope": "executor_preflight_admission_projection",
                            "preflight_status": executor.stability_preflight.get(
                                "preflight_status"
                            ),
                            "rejection_reason": executor.stability_preflight.get(
                                "rejection_reason"
                            ),
                            "max_provider_calls": executor.max_provider_calls,
                            "call_count_scope": "executor-wide provider-free projection using each configured Outline role profile",
                            "role_routes": role_route_summaries,
                            "route_annotations_are_projection_only": True,
                            "relation_preflight_route": {
                                "role": "relation_adjudication",
                                "config_section": role_route_summaries.get("relation_adjudication", {}).get("config_section", ""),
                                "model": role_route_summaries.get("relation_adjudication", {}).get("model", ""),
                            },
                            "stability_mode_in_shadow": executor.stability_mode,
                            "semantic_repair_enabled_in_shadow": executor.semantic_repair_enabled,
                            "opaque_alias_enabled_in_shadow": executor.opaque_alias_enabled,
                            "estimated_provider_calls": executor.stability_preflight.get(
                                "estimated_provider_calls"
                            ),
                            "estimated_provider_physical_attempts_upper_bound": executor.stability_preflight.get(
                                "estimated_provider_physical_attempts_upper_bound"
                            ),
                            "semantic_cross_group_runtime_fragment_count": executor.stability_preflight.get(
                                "semantic_cross_group_runtime_fragment_count"
                            ),
                            "semantic_cross_group_planner_item_count": executor.stability_preflight.get(
                                "semantic_cross_group_planner_item_count"
                            ),
                            "semantic_cross_group_fragment_bounds_status": executor.stability_preflight.get(
                                "semantic_cross_group_fragment_bounds_status"
                            ),
                            "semantic_request_upper_bound_status": executor.stability_preflight.get(
                                "semantic_request_upper_bound_status"
                            ),
                            "hierarchical_relation_shard_calls": executor.stability_preflight.get(
                                "hierarchical_relation_shard_calls"
                            ),
                            "hierarchical_candidate_shard_calls": executor.stability_preflight.get(
                                "hierarchical_candidate_shard_calls"
                            ),
                            "hierarchical_critique_shard_calls": executor.stability_preflight.get(
                                "hierarchical_critique_shard_calls"
                            ),
                            "semantic_synthesis_calls_reserved": executor.stability_preflight.get(
                                "semantic_synthesis_calls_reserved"
                            ),
                            "semantic_conditional_reducer_call_reserve": executor.stability_preflight.get(
                                "semantic_conditional_reducer_call_reserve"
                            ),
                            "semantic_input_tokens_single_attempt_upper_bound": executor.stability_preflight.get(
                                "semantic_input_tokens_single_attempt_upper_bound"
                            ),
                            "semantic_output_tokens_single_attempt_upper_bound": executor.stability_preflight.get(
                                "semantic_output_tokens_single_attempt_upper_bound"
                            ),
                            "semantic_input_tokens_all_attempts_upper_bound": executor.stability_preflight.get(
                                "semantic_input_tokens_all_attempts_upper_bound"
                            ),
                            "semantic_output_tokens_all_attempts_upper_bound": executor.stability_preflight.get(
                                "semantic_output_tokens_all_attempts_upper_bound"
                            ),
                            "semantic_reasoning_tokens_all_attempts_upper_bound": executor.stability_preflight.get(
                                "semantic_reasoning_tokens_all_attempts_upper_bound"
                            ),
                            "semantic_physical_attempts_upper_bound": executor.stability_preflight.get(
                                "semantic_physical_attempts_upper_bound"
                            ),
                            "semantic_transport_retry_reserve": executor.stability_preflight.get(
                                "semantic_transport_retry_reserve"
                            ),
                            "semantic_output_token_limit": executor.stability_preflight.get(
                                "semantic_output_token_limit"
                            ),
                            "estimated_total_tokens": executor.stability_preflight.get(
                                "estimated_total_tokens"
                            ),
                            "estimated_cost": executor.stability_preflight.get("estimated_cost"),
                            "physical_attempt_estimate_status": executor.stability_preflight.get(
                                "physical_attempt_estimate_status"
                            ),
                        }
                        semantic_preflight_diagnostic = preflight_error or str(
                            executor.stability_preflight.get("diagnostic") or ""
                        )
                        semantic_request_count = len(semantic_request_plan)
                        reserved_calls = executor.stability_preflight.get(
                            "semantic_synthesis_calls_reserved"
                        )
                        if reserved_calls is None:
                            semantic_request_budget_status = "BLOCKED_SEMANTIC_RESERVED_CALL_BOUND_UNKNOWN"
                        else:
                            semantic_reserved_call_count = int(reserved_calls)
                            semantic_request_input_tokens = int(
                                executor.stability_preflight.get(
                                    "semantic_input_tokens_single_attempt_upper_bound"
                                ) or 0
                            )
                            semantic_request_output_tokens = int(
                                executor.stability_preflight.get(
                                    "semantic_output_tokens_single_attempt_upper_bound"
                                ) or 0
                            )
                            semantic_request_budget_status = (
                                "SEMANTIC_RESERVED_CALL_BOUND_WITHIN_LIMIT"
                                if semantic_reserved_call_count <= effective_call_limit
                                else "BLOCKED_SEMANTIC_RESERVED_CALL_BOUND_EXCEEDS_LIMIT"
                            )
                        route_plan_status = (
                            "planned_semantic_request_graph"
                            if semantic_request_plan
                            else "blocked_semantic_request_plan"
                        )
                    else:
                        route_plan_status = "not_planned_non_provider_route"
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                semantic_preflight_status = "blocked"
                semantic_preflight_diagnostic = str(exc)
                semantic_request_budget_status = "BLOCKED_SEMANTIC_REQUEST_PLAN"
                route_plan_status = "blocked_semantic_request_plan"
        topic_request_rows = [
            item
            for item in semantic_request_plan
            if str(item.get("node_id") or "").startswith(
                "topic_synthesis_provider:batch:"
            )
        ]
        shadow_upper_bound_materialized = (
            semantic_route_preflight_summary.get("semantic_request_upper_bound_status")
            == "materialized_upper_bound"
        )
        provider_free_shadow_capacity_comparison = _provider_free_shadow_capacity_comparisons(
            topic_call_lower_bound=len(topic_request_rows),
            logical_call_upper_bound=(
                semantic_route_preflight_summary.get("estimated_provider_calls")
                if shadow_upper_bound_materialized else None
            ),
            physical_attempt_upper_bound=(
                semantic_route_preflight_summary.get(
                    "estimated_provider_physical_attempts_upper_bound"
                )
                if shadow_upper_bound_materialized else None
            ),
            actual_runtime_call_limit=effective_call_limit,
            actual_preflight_status=semantic_preflight_status,
        )
        workload_audit["topic_request_plan_identity_hash"] = (
            topic_request_plan_identity_hash
        )
        topic_wire_text_identity_char_counts: dict[str, int] = {}
        topic_wire_text_identity_batch_occurrences = 0
        for item in topic_request_rows:
            records = [
                row
                for row in item.get("source_text_identity_records") or ()
                if isinstance(row, Mapping) and str(row.get("identity_hash") or "")
            ]
            topic_wire_text_identity_batch_occurrences += len(records)
            for row in records:
                identity_hash = str(row.get("identity_hash") or "")
                character_count = max(0, int(row.get("character_count") or 0))
                prior_count = topic_wire_text_identity_char_counts.get(identity_hash)
                if prior_count is not None and prior_count != character_count:
                    raise ControlPlaneError(
                        "topic request plan has inconsistent lengths for one normalized text identity"
                    )
                topic_wire_text_identity_char_counts[identity_hash] = character_count
        topic_wire_text_occurrences = sum(
            int(item.get("source_text_occurrence_count") or 0)
            for item in topic_request_rows
        )
        topic_wire_text_characters = sum(
            int(item.get("source_text_character_count") or 0)
            for item in topic_request_rows
        )
        topic_wire_unique_text_characters = sum(
            topic_wire_text_identity_char_counts.values()
        )
        topic_wire_text_repeated_within_batches = sum(
            int(item.get("repeated_source_text_occurrence_count") or 0)
            for item in topic_request_rows
        )
        topic_wire_text_repeated_across_batches = max(
            0,
            topic_wire_text_identity_batch_occurrences
            - len(topic_wire_text_identity_char_counts),
        )
        safe_semantic_request_plan = []
        for item in semantic_request_plan:
            safe_item = {
                key: value
                for key, value in item.items()
                if key != "source_text_identity_records"
            }
            if "source_text_identity_records" in item:
                safe_item["source_text_identity_unique_value_count"] = len(
                    item.get("source_text_identity_records") or ()
                )
            safe_semantic_request_plan.append(safe_item)
        workload_audit["topic_wire_text"] = {
            "measurement": "casefolded_whitespace_normalized_source_text_identity",
            "request_count": len(topic_request_rows),
            "source_text_occurrence_count": topic_wire_text_occurrences,
            "unique_source_text_identity_count_across_requests": len(
                topic_wire_text_identity_char_counts
            ),
            "repeated_occurrence_count_within_requests": topic_wire_text_repeated_within_batches,
            "repeated_occurrence_count_across_requests": topic_wire_text_repeated_across_batches,
            "repeated_occurrence_count_total": max(
                0,
                topic_wire_text_occurrences - len(topic_wire_text_identity_char_counts),
            ),
            "normalized_character_count_total": topic_wire_text_characters,
            "unique_normalized_character_count": topic_wire_unique_text_characters,
            "repeated_normalized_character_count": max(
                0,
                topic_wire_text_characters - topic_wire_unique_text_characters,
            ),
            "unique_identity_set_hash": hash_json(
                sorted(topic_wire_text_identity_char_counts)
            ),
        }
        topic_wire_claim_hashes = {
            str(value)
            for item in topic_request_rows
            for value in item.get("source_claim_identity_hashes") or ()
            if str(value)
        }
        topic_wire_evidence_hashes = {
            str(value)
            for item in topic_request_rows
            for value in item.get("evidence_identity_hashes") or ()
            if str(value)
        }
        expected_claim_hashes = set(source_claim_identity_hashes)
        expected_evidence_hashes = {
            hash_json({"paper_key": paper_id, "evidence_id": evidence_id})
            for paper_id, evidence_id in evidence_keys
        }
        claim_missing = expected_claim_hashes - topic_wire_claim_hashes
        claim_extra = topic_wire_claim_hashes - expected_claim_hashes
        evidence_missing = expected_evidence_hashes - topic_wire_evidence_hashes
        evidence_extra = topic_wire_evidence_hashes - expected_evidence_hashes
        workload_audit["topic_wire_coverage"] = {
            "status": (
                "NOT_PLANNED"
                if not topic_request_rows
                else "complete"
                if not claim_missing
                and not claim_extra
                and not evidence_missing
                and not evidence_extra
                else "incomplete"
            ),
            "topic_batch_count": len(topic_request_rows),
            "all_planned_unit_sets_equal_materialized_unit_sets": (
                all(
                    bool(item.get("planned_wire_ids_equal_materialized_ids"))
                    for item in topic_request_rows
                )
                if topic_request_rows
                else None
            ),
            "source_claim_identity_count": len(expected_claim_hashes),
            "source_claim_identity_set_hash": hash_json(
                sorted(expected_claim_hashes)
            ),
            "topic_wire_claim_identity_count": len(topic_wire_claim_hashes),
            "topic_wire_claim_identity_set_hash": hash_json(
                sorted(topic_wire_claim_hashes)
            ),
            "missing_source_claim_identity_count": len(claim_missing),
            "missing_source_claim_identity_set_hash": hash_json(
                sorted(claim_missing)
            ),
            "extra_topic_wire_claim_identity_count": len(claim_extra),
            "extra_topic_wire_claim_identity_set_hash": hash_json(
                sorted(claim_extra)
            ),
            "source_evidence_identity_count": len(expected_evidence_hashes),
            "source_evidence_identity_set_hash": hash_json(
                sorted(expected_evidence_hashes)
            ),
            "topic_wire_evidence_identity_count": len(topic_wire_evidence_hashes),
            "topic_wire_evidence_identity_set_hash": hash_json(
                sorted(topic_wire_evidence_hashes)
            ),
            "missing_source_evidence_identity_count": len(evidence_missing),
            "missing_source_evidence_identity_set_hash": hash_json(
                sorted(evidence_missing)
            ),
            "extra_topic_wire_evidence_identity_count": len(evidence_extra),
            "extra_topic_wire_evidence_identity_set_hash": hash_json(
                sorted(evidence_extra)
            ),
        }
        payload: dict[str, Any] = {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": "planned" if semantic_plan.status == "ready" else "blocked",
            "command": "chunk-plan",
            "job_id": str(job_id),
            "source_files": source_paths,
            "source_summary_count": len(summaries),
            "content_layers": {
                "content_hash": content_layers.content_hash,
                "status": content_layers.status,
                "index_card_count": len(content_layers.index_cards),
                "dossier_count": len(content_layers.dossiers),
                "blocking_diagnostics": list(content_layers.blocking_diagnostics),
            },
            "typed_manifest_authority_hashes": typed_manifest_authorities,
            "semantic_chunk_plan": semantic_plan.to_dict(),
            "r1_request_workload_audit": workload_audit,
            "semantic_request_plan": safe_semantic_request_plan,
            "semantic_request_plan_count": semantic_request_count,
            "semantic_request_plan_count_kind": "materialized_rows_excludes_conditional_reducer_reserve",
            "semantic_request_calls_reserved_upper_bound": semantic_reserved_call_count,
            "semantic_topic_batch_count": sum(
                str(item.get("node_id") or "").startswith("topic_synthesis_provider:batch:")
                for item in semantic_request_plan
            ),
            "semantic_cross_materialized_request_count": sum(
                str(item.get("node_id") or "").startswith("cross_group_comparison_provider")
                for item in semantic_request_plan
            ),
            "semantic_cross_request_count_upper_bound": (
                sum(
                    str(item.get("node_id") or "").startswith("cross_group_comparison_provider")
                    for item in semantic_request_plan
                ) + int(semantic_route_preflight_summary.get("semantic_conditional_reducer_call_reserve") or 0)
                if semantic_reserved_call_count is not None else None
            ),
            "semantic_global_request_count_upper_bound": (
                sum(
                    str(item.get("node_id") or "").startswith("global_synthesis_provider")
                    for item in semantic_request_plan
                ) if semantic_reserved_call_count is not None else None
            ),
            "semantic_request_input_token_upper_bound": semantic_request_input_tokens,
            "semantic_request_output_token_reserve": semantic_request_output_tokens,
            "semantic_request_output_tokens_all_attempts_upper_bound": (
                semantic_route_preflight_summary.get("semantic_output_tokens_all_attempts_upper_bound")
            ),
            "semantic_request_reasoning_tokens_all_attempts_upper_bound": (
                semantic_route_preflight_summary.get("semantic_reasoning_tokens_all_attempts_upper_bound")
            ),
            "semantic_request_retry_reserve": (
                semantic_route_preflight_summary.get("semantic_transport_retry_reserve")
            ),
            "semantic_request_physical_attempts_upper_bound": (
                semantic_route_preflight_summary.get("semantic_physical_attempts_upper_bound")
            ),
            "semantic_cross_group_runtime_fragment_count": (
                semantic_route_preflight_summary.get(
                    "semantic_cross_group_runtime_fragment_count"
                )
            ),
            "semantic_cross_group_planner_item_count": (
                semantic_route_preflight_summary.get(
                    "semantic_cross_group_planner_item_count"
                )
            ),
            "semantic_cross_group_fragment_bounds_status": (
                semantic_route_preflight_summary.get(
                    "semantic_cross_group_fragment_bounds_status"
                ) or "incomplete_upper_bound"
            ),
            "semantic_request_upper_bound_status": (
                semantic_route_preflight_summary.get(
                    "semantic_request_upper_bound_status"
                ) or "incomplete_upper_bound"
            ),
            "semantic_request_input_tokens_all_attempts_upper_bound": (
                semantic_route_preflight_summary.get("semantic_input_tokens_all_attempts_upper_bound")
            ),
            "semantic_request_plan_status": route_plan_status,
            "semantic_preflight_status": semantic_preflight_status,
            "semantic_preflight_diagnostic": semantic_preflight_diagnostic,
            "semantic_route_preflight_summary": semantic_route_preflight_summary,
            "semantic_request_budget_status": semantic_request_budget_status,
            "provider_free_shadow_capacity_comparison": provider_free_shadow_capacity_comparison,
            "semantic_request_plan_scope": "outline_v3_topic_cross_global_only",
            "semantic_route_identity_hash": route_identity_hash,
            "topic_request_plan_identity_hash": topic_request_plan_identity_hash,
            "semantic_physical_call_limit": effective_call_limit,
            "end_to_end_provider_budget_status": "NOT_PLANNED",
            "relation_map": {
                "content_hash": relation_map.content_hash,
                "candidate_count": len(relation_map.relations),
                "blocking_diagnostics": list(relation_map.blocking_diagnostics),
            },
            "provider_posts_emitted": 0,
            "provider_call_budget_status": "NOT_PLANNED_END_TO_END",
            "provider_request_plan_status": route_plan_status,
            "provider_request_plan_scope": "outline_v3_topic_cross_global_only",
            "provider_request_plan_note": (
                "Topic requests use the same serializer and batching helper as the executor. "
                "Cross/global reducer inputs use output upper bounds; Writer, Validator, repair, and DOCX budgets are outside this plan."
            ),
            "read_only": True,
        }
        if output_path:
            target = Path(output_path).expanduser().resolve()
            if target in {Path(item).expanduser().resolve() for item in source_paths}:
                raise ControlPlaneError(
                    "chunk-plan output must differ from every resolved summary source"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["output_path"] = str(target)
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
        external_host_admission = self._admit_runtime_spec_external_hosts(spec)
        runner = AgentRuntimeRunner(spec)
        result = runner.resume() if resume else runner.run()
        payload = self._status_payload(result)
        if result.job_status != "completed" or result.completion_status != "complete":
            payload["formal_runner_transport_diff"] = self._formal_runner_transport_diff(
                spec,
                result,
            )
        payload["external_host_admission"] = external_host_admission
        return payload

    def _admit_runtime_spec_external_hosts(
        self,
        spec: RuntimeJobSpec,
    ) -> dict[str, Any]:
        """Fail closed before a direct run/resume constructs any transport."""

        free_mode_enabled = bool(
            spec.free_mode_profile
            or spec.free_mode_idea
            or spec.metadata.get("free_mode_input")
        )
        try:
            from runtime.test_dependencies import current_runtime_test_dependencies
            from services.credential_provenance import is_template_credential

            test_dependencies = current_runtime_test_dependencies()
            normalized = load_config(
                str(spec.config),
                action=spec.action,
                requested_stages=spec.metadata.get("requested_stages"),
                free_mode_enabled=free_mode_enabled,
                allow_template_credentials=bool(
                    test_dependencies and test_dependencies.allow_template_credentials
                ),
            )
            if test_dependencies is not None:
                test_dependencies.validate()
                has_explicit_test_template = any(
                    is_template_credential(section.get("api_key"))
                    for section_name, section in normalized.items()
                    if section_name.endswith("_API") and isinstance(section, Mapping)
                )
                if has_explicit_test_template:
                    # The pytest-only injected adapter owns a zero-external-
                    # transport fixture.  This compatibility projection is
                    # limited to template-based fixtures; a real credential
                    # still follows the normal host-acknowledgement path even
                    # inside pytest.
                    return {
                        "required": False,
                        "acknowledged": False,
                        "offline_test_dependency_injected": True,
                        "read_only": True,
                    }
            route_plan = build_reachable_provider_route_plan(
                normalized,
                action=spec.action,
                requested_stages=spec.metadata.get("requested_stages"),
                free_mode_enabled=free_mode_enabled,
            )
            policy = build_runtime_external_host_policy(
                normalized,
                route_plan,
                requested_stages=spec.metadata.get("requested_stages"),
                outline_pilot=spec.metadata.get("outline_pilot"),
            )
            admission = validate_external_host_acknowledgement(
                policy,
                spec.metadata.get("external_host_acknowledgement"),
            )
        except ExternalHostAdmissionError as exc:
            raise ControlPlaneError(f"external host admission failed: {exc}") from exc
        except (OSError, ValueError, TypeError) as exc:
            # Preserve the public run/resume failure contract for invalid or
            # missing runtime configuration while still performing admission
            # before the runner can construct a transport.
            raise RuntimeRunnerError(f"runtime configuration admission failed: {exc}") from exc
        return {
            **admission,
            "policy": policy.to_dict(),
            "read_only": True,
        }

    def _admit_direct_validator_execution(
        self,
        spec: RuntimeJobSpec,
        *,
        validator_host_acknowledgement: Mapping[str, Any] | None,
        operation: str,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Bind the actual Validator route and shared budget for direct commands."""

        if validator_host_acknowledgement is not None and not isinstance(
            validator_host_acknowledgement, Mapping
        ):
            raise ControlPlaneError("Validator host acknowledgement must be a JSON object")
        acknowledgement = (
            validator_host_acknowledgement
            if validator_host_acknowledgement is not None
            else spec.metadata.get("external_host_acknowledgement")
        )
        from runtime.test_dependencies import current_runtime_test_dependencies
        from services.model_selection import get_validator_api_config

        test_dependencies = current_runtime_test_dependencies()
        normalized_config = load_config(
            str(spec.config),
            action="validate_review",
            requested_stages=["validate"],
            free_mode_enabled=bool(
                spec.free_mode_profile
                or spec.free_mode_idea
                or spec.metadata.get("free_mode_input")
            ),
            allow_template_credentials=bool(test_dependencies),
        )
        validator_config = get_validator_api_config({
            "Validator_API": dict(normalized_config.get("Validator_API") or {})
        })
        validator_can_call = bool(
            str(validator_config.get("api_key") or "").strip()
            and str(validator_config.get("model") or "").strip()
        )
        if not validator_can_call:
            return "", None

        # These commands revalidate existing artifacts. Their only possible
        # disclosure route is Validator; a saved review/ingest stage or a
        # configured remote parser does not authorize or participate in it.
        try:
            route_plan = build_reachable_provider_route_plan(
                normalized_config,
                action="validate_review",
                requested_stages=["validate"],
            )
            if not any(
                route.semantic_role == "validator" and route.enabled and route.resolved
                for route in route_plan.routes
            ):
                raise ControlPlaneError(f"{operation} has no reachable Validator route")
            policy = build_external_host_policy(
                normalized_config,
                route_plan,
                provider_sections={"Validator_API"},
                include_mineru=False,
            )
            validate_external_host_acknowledgement(
                policy,
                acknowledgement,
            )
        except (ExternalHostAdmissionError, ValueError, TypeError) as exc:
            raise ControlPlaneError(f"{operation} host admission failed: {exc}") from exc

        acceptance = (
            current_acceptance_execution_context()
            or acceptance_execution_context_from_environment()
        )
        controller = provider_budget_controller_from_environment()
        if (
            acceptance is None
            or not acceptance.owner_authorized
            or not acceptance.provider_budget_state_started
            or controller is None
            or controller.budget != acceptance.provider_budget
            or acceptance.absolute_deadline_epoch <= time.time()
            or acceptance.provider_budget.max_provider_calls_total <= 0
            or acceptance.provider_budget.max_output_tokens_total <= 0
            or acceptance.provider_budget.max_wall_seconds <= 0
        ):
            raise ControlPlaneError(
                f"{operation} requires a started, bounded aggregate acceptance run"
            )
        current_sha = read_checkout_sha(self.repo_root, require_clean=True)
        if current_sha != acceptance.final_executable_sha:
            raise ControlPlaneError(
                f"{operation} executable SHA differs from the acceptance run"
            )
        controller.bind_state_path(
            acceptance.provider_budget_state_path,
            acceptance_run_id=acceptance.acceptance_run_id,
            state_started=True,
        )
        controller.snapshot()
        return policy.route_fingerprint, acknowledgement

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
            gate_contract,
            scenario_for_gate,
        )

        spec_path = _acceptance_lexical_path(acceptance_spec_path)
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            acceptance_spec = ReleaseAcceptanceSpec.from_mapping(
                raw,
                origin_dir=spec_path.parent,
                allow_missing_live_budget=True,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ControlPlaneError(
                f"acceptance specification is invalid: {type(exc).__name__}: {exc}"
            ) from exc

        if acceptance_spec.plan is not None:
            return self._acceptance_run_plan(
                spec_path,
                acceptance_spec,
            )

        execution_context_owner_authorized = os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") == "1"
        current_sha = self._acceptance_checkout_sha(
            self.repo_root,
            require_clean=execution_context_owner_authorized,
        )
        if acceptance_spec.final_executable_sha and acceptance_spec.final_executable_sha != current_sha:
            raise ControlPlaneError(
                "acceptance specification executable SHA does not match the current checkout"
            )

        state_path = _acceptance_lexical_path(
            acceptance_spec.state_path
            or self.repo_root / "output" / "_acceptance"
        )
        if state_path.suffix.casefold() != ".json":
            state_path = _acceptance_lexical_path(
                state_path / "acceptance_run_state_v1.json"
            )
        evidence_path = _acceptance_lexical_path(
            acceptance_spec.evidence_manifest
            or state_path.with_name("acceptance_evidence_index_v1.json")
        )
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
        if acceptance_spec.plan is None and len(gates) > 1:
            raise ControlPlaneError(
                "multi-gate acceptance requires release-acceptance-plan-v2 with independent child scenarios"
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
            run_dir, evidence_root = self._bind_acceptance_evidence_root(
                state_path,
                state,
            )
            # An inspection is not allowed to decide that a persisted run is
            # unstarted merely because its budget file is missing, unreadable,
            # or has zero completed calls. Reservations and an unexpired wall
            # window are also durable exposure. Preserve the run identity.
            budget_state_was_bound = bool(state.provider_budget_state_path)
            state = replace(
                state,
                provider_budget_state_path=(
                    state.provider_budget_state_path
                    or (str(run_dir / "provider_budget_state_v1.json")
                        if execution_context_owner_authorized else "")
                ),
                evidence_root=str(evidence_root),
                process_event_log=(
                    state.process_event_log
                    or str(run_dir / "process_events.jsonl")
                ),
            )
            atomic_write_json(str(state_path), state.to_dict())

        evidence_path = (
            Path(state.evidence_root).expanduser().resolve()
            / "acceptance_evidence_index_v1.json"
        )
        budget_controller = ProviderBudgetController(
            acceptance_spec.budget.to_provider_budget()
        )
        if execution_context_owner_authorized:
            budget_controller.bind_state_path(
                state.provider_budget_state_path,
                acceptance_run_id=state.run_id,
                state_started=budget_state_was_bound,
            )
        budget_snapshot = budget_controller.snapshot()
        execution_context = AcceptanceExecutionContextV1(
            acceptance_run_id=state.run_id,
            final_executable_sha=current_sha,
            # Authorization is the admission boundary for live wall-clock
            # accounting.  An inspection/blocked call must not spend the
            # live deadline merely by constructing its context.
            absolute_deadline_epoch=(
                float(budget_snapshot.get("absolute_deadline_epoch") or 0.0)
                if execution_context_owner_authorized
                else 0.0
            ),
            provider_budget=acceptance_spec.budget.to_provider_budget(),
            provider_budget_state_path=(
                state.provider_budget_state_path
                or str(Path(state.evidence_root).expanduser().resolve().parent / "provider_budget_state_v1.json")
            ),
            evidence_root=state.evidence_root,
            process_event_log=state.process_event_log,
            scenario_state_path=str(state_path),
            owner_authorized=execution_context_owner_authorized,
            provider_budget_state_started=execution_context_owner_authorized,
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
                    preflight = self.provider_preflight(
                        config_path=config_path,
                        action=runtime_job_spec.action,
                        requested_stages=runtime_job_spec.metadata.get("requested_stages"),
                        outline_pilot=runtime_job_spec.metadata.get("outline_pilot"),
                        free_mode_enabled=bool(
                            runtime_job_spec.free_mode_profile
                            or runtime_job_spec.free_mode_idea
                            or runtime_job_spec.metadata.get("free_mode_input")
                        ),
                    )
                    route_plan = preflight.get("route_plan")
                    if preflight.get("ok") is False:
                        admission = preflight.get("mineru_remote_admission")
                        reason = (
                            str(admission.get("reason") or "")
                            if isinstance(admission, Mapping)
                            else ""
                        ) or str(preflight.get("error_type") or "provider_preflight_failed")
                        raise ControlPlaneError(
                            f"acceptance provider preflight did not admit execution: {reason}"
                        )
                    if not acceptance_spec.budget_explicit:
                        raise ControlPlaneError(
                            "live acceptance requires an explicit budget object"
                        )
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
    def _acceptance_checkout_sha(repo_root: Path, *, require_clean: bool = False) -> str:
        try:
            return read_checkout_sha(repo_root, require_clean=require_clean)
        except CheckoutIdentityError as exc:
            raise ControlPlaneError(str(exc)) from exc

    @staticmethod
    def _bind_acceptance_evidence_root(
        state_path: Path,
        state: Any,
    ) -> tuple[Path, Path]:
        """Create and verify the sole run-owned acceptance evidence root."""

        run_id = str(getattr(state, "run_id", "") or "").strip()
        final_sha = str(getattr(state, "final_sha", "") or "").strip()
        if not run_id or not final_sha:
            raise ControlPlaneError("acceptance evidence root is missing run identity")
        run_dir = _acceptance_lexical_path(state_path.parent / run_id)
        evidence_root = _acceptance_lexical_path(run_dir / "evidence")
        expected_budget_path = _acceptance_lexical_path(
            run_dir / "provider_budget_state_v1.json"
        )
        expected_event_log = _acceptance_lexical_path(run_dir / "process_events.jsonl")
        configured_root = str(getattr(state, "evidence_root", "") or "").strip()
        if configured_root and _acceptance_lexical_path(configured_root).resolve() != evidence_root.resolve():
            raise ControlPlaneError(
                "acceptance evidence root must be the run-owned evidence directory"
            )
        configured_budget = str(
            getattr(state, "provider_budget_state_path", "") or ""
        ).strip()
        if configured_budget and _acceptance_lexical_path(configured_budget).resolve() != expected_budget_path.resolve():
            raise ControlPlaneError(
                "acceptance provider budget state must be in the run-owned directory"
            )
        configured_event_log = str(getattr(state, "process_event_log", "") or "").strip()
        if configured_event_log and _acceptance_lexical_path(configured_event_log).resolve() != expected_event_log.resolve():
            raise ControlPlaneError(
                "acceptance process event log must be in the run-owned directory"
            )
        for candidate in (run_dir, evidence_root):
            current = candidate
            while True:
                if os.path.lexists(current) and is_reparse_path(current):
                    raise ControlPlaneError(
                        "acceptance evidence root contains a symlink or reparse path"
                    )
                parent = current.parent
                if parent == current:
                    break
                current = parent
        run_dir.mkdir(parents=True, exist_ok=True)
        evidence_root.mkdir(parents=True, exist_ok=True)
        if is_reparse_path(run_dir) or is_reparse_path(evidence_root):
            raise ControlPlaneError("acceptance evidence root is a symlink or reparse path")
        marker_path = run_dir / "acceptance_evidence_root_v1.json"
        expected_marker = {
            "schema_version": "acceptance-evidence-root-v1",
            "acceptance_run_id": run_id,
            "final_executable_sha": final_sha,
            "evidence_root": str(evidence_root),
        }
        if marker_path.is_file():
            if is_reparse_path(marker_path):
                raise ControlPlaneError("acceptance evidence root marker is unsafe")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ControlPlaneError("acceptance evidence root marker is unreadable") from exc
            if marker != expected_marker:
                raise ControlPlaneError(
                    "acceptance evidence root marker does not match run identity"
                )
        elif marker_path.exists():
            raise ControlPlaneError("acceptance evidence root marker is not a regular file")
        else:
            atomic_write_json(str(marker_path), expected_marker)
        return run_dir, evidence_root

    @staticmethod
    def _acceptance_file_hash(path: str | Path, *, allow_missing: bool = False) -> str:
        target = _acceptance_lexical_path(path)
        if not target.is_file() or target.is_symlink():
            if allow_missing:
                return hashlib.sha256(b"").hexdigest()
            raise ControlPlaneError(f"acceptance plan file is missing or unsafe: {target}")
        try:
            return hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise ControlPlaneError(f"acceptance plan file is unreadable: {target}") from exc

    @staticmethod
    def _acceptance_workspace_identity(workspace: str | Path, *, job_id: str) -> str:
        target = Path(workspace).expanduser().resolve()
        registry_hash = ""
        registry = target / "artifact_registry.json"
        if registry.is_file() and not registry.is_symlink():
            try:
                registry_hash = hashlib.sha256(registry.read_bytes()).hexdigest()
            except OSError:
                registry_hash = ""
        return hashlib.sha256(
            json.dumps(
                {"workspace": str(target), "job_id": str(job_id), "registry_sha256": registry_hash},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _acceptance_input_identity(
        child: Any,
        *,
        runtime_spec_hash: str,
    ) -> str:
        input_manifest = str(getattr(child, "input_manifest", "") or "").strip()
        if input_manifest:
            return ReviewControlPlane._acceptance_file_hash(input_manifest)
        raw_identity = {
            "runtime_spec_sha256": runtime_spec_hash,
            "scenario_id": str(getattr(child, "scenario_id", "")),
            "gate": str(getattr(child, "gate", "")),
        }
        return hashlib.sha256(
            json.dumps(
                raw_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _acceptance_runtime_child_binding(
        child: Any,
        runtime_spec: RuntimeJobSpec,
    ) -> tuple[Path, str]:
        """Require the plan, RuntimeJobSpec, and runner to share child identity.

        A parent plan's C/D/Q workspace isolation is only meaningful if the
        referenced RuntimeJobSpec names that exact workspace and job.  Keeping
        those bindings at this boundary prevents an apparently independent
        child from silently running in another child's mutable workspace.
        """

        child_workspace_text = str(getattr(child, "workspace", "") or "").strip()
        if not child_workspace_text:
            raise ControlPlaneError("runtime child is missing its declared workspace")
        child_workspace = Path(child_workspace_text).expanduser().resolve()
        runtime_workspace_text = str(runtime_spec.workspace_path or "").strip()
        if not runtime_workspace_text:
            raise ControlPlaneError(
                "runtime child RuntimeJobSpec must declare workspace_path"
            )
        runtime_workspace = Path(runtime_workspace_text).expanduser().resolve()
        if runtime_workspace != child_workspace:
            raise ControlPlaneError(
                "runtime child RuntimeJobSpec workspace_path does not match the plan workspace"
            )

        runtime_job_id = str(runtime_spec.job_id or "").strip()
        if not runtime_job_id:
            raise ControlPlaneError("runtime child RuntimeJobSpec must declare job_id")
        child_job_id = str(getattr(child, "job_id", "") or "").strip()
        if child_job_id and child_job_id != runtime_job_id:
            raise ControlPlaneError(
                "runtime child RuntimeJobSpec job_id does not match the plan job_id"
            )
        return child_workspace, runtime_job_id

    @staticmethod
    def _require_live_runtime_deadline(runtime_spec: RuntimeJobSpec) -> None:
        """Reject live acceptance jobs with the ordinary unlimited deadline."""

        free_mode_enabled = bool(
            runtime_spec.free_mode_profile
            or runtime_spec.free_mode_idea
            or runtime_spec.metadata.get("free_mode_input")
        )
        normalized = load_config(
            runtime_spec.config,
            action=runtime_spec.action,
            requested_stages=runtime_spec.metadata.get("requested_stages"),
            free_mode_enabled=free_mode_enabled,
            allow_template_credentials=False,
        )
        runtime_section = normalized.get("Runtime", {})
        raw_deadline = runtime_section.get("total_job_deadline_seconds", 0)
        try:
            deadline = float(raw_deadline)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneError(
                "live acceptance requires a finite total_job_deadline_seconds"
            ) from exc
        if not math.isfinite(deadline) or deadline <= 0:
            raise ControlPlaneError(
                "live acceptance requires total_job_deadline_seconds greater than zero"
            )

    @staticmethod
    def _acceptance_receipt_ref(
        receipt_path: Path,
        *,
        final_sha: str,
        job_id: str,
    ) -> dict[str, Any]:
        from runtime.release_acceptance import GateEvidenceProducer

        return GateEvidenceProducer(final_sha=final_sha).reference(
            receipt_path,
            role="scenario_execution_receipt",
            artifact_type="scenario_execution_receipt",
            artifact_version="v1",
            schema_version="scenario-execution-receipt-v1",
            job_id=job_id,
        )

    def _write_acceptance_receipt(
        self,
        receipt_path: Path,
        *,
        parent_run_id: str,
        plan_sha256: str,
        child: Any,
        final_sha: str,
        runtime_spec_sha256: str,
        input_identity_sha256: str,
        workspace: str,
        job_id: str,
        attempt_id: str,
        status: str,
        exit_status: int,
        produced_evidence_refs: Iterable[Mapping[str, Any]],
        started_at: str,
        completed_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from runtime.provider_runtime import process_identity_for_pid
        from runtime.release_acceptance import ScenarioExecutionReceiptV1

        identity = process_identity_for_pid(os.getpid())
        payload = ScenarioExecutionReceiptV1(
            parent_acceptance_run_id=parent_run_id,
            scenario_id=str(child.scenario_id),
            gate=str(child.gate),
            final_executable_sha=final_sha,
            plan_sha256=plan_sha256,
            runtime_spec_sha256=runtime_spec_sha256,
            input_identity_sha256=input_identity_sha256,
            workspace_identity_sha256=self._acceptance_workspace_identity(
                workspace or str(receipt_path.parent),
                job_id=job_id,
            ),
            executor_pid=identity.pid,
            executor_process_creation_identity=str(identity.creation_time or "unknown"),
            executor_host_id=identity.host_id,
            started_at=started_at,
            completed_at=completed_at,
            action_type=str(child.execution_mode),
            workspace=workspace or str(receipt_path.parent),
            job_id=job_id,
            attempt_id=attempt_id,
            budget_domain=str(child.budget_domain),
            status=str(status).upper(),
            exit_status=int(exit_status),
            produced_evidence_refs=tuple(dict(item) for item in produced_evidence_refs),
        )
        atomic_write_json(str(receipt_path), payload.to_dict())
        return payload.to_dict(), self._acceptance_receipt_ref(
            receipt_path,
            final_sha=final_sha,
            job_id=job_id,
        )

    def _load_acceptance_child_result(
        self,
        record: Mapping[str, Any],
        *,
        gate: str,
        final_sha: str,
        parent_run_id: str,
        plan_sha256: str,
        runtime_spec_sha256: str,
        input_identity_sha256: str,
    ) -> dict[str, Any] | None:
        from runtime.release_acceptance import (
            GateEvidenceVerifier,
            ScenarioExecutionReceiptV1,
        )

        receipt_path = Path(str(record.get("receipt_path") or "")).expanduser().resolve()
        if not receipt_path.is_file() or receipt_path.is_symlink():
            return None
        try:
            receipt = ScenarioExecutionReceiptV1.from_mapping(
                json.loads(receipt_path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if (
            receipt.parent_acceptance_run_id != parent_run_id
            or receipt.final_executable_sha != final_sha
            or receipt.scenario_id != gate
            or receipt.plan_sha256 != plan_sha256
            or receipt.runtime_spec_sha256 != runtime_spec_sha256
            or receipt.input_identity_sha256 != input_identity_sha256
        ):
            return None
        result: dict[str, Any] = {
            "status": str(record.get("status") or "NOT_VERIFIED"),
            "scenario_id": gate,
            "receipt": receipt.to_dict(),
            "receipt_path": str(receipt_path),
            "evidence_manifest": str(record.get("evidence_manifest") or ""),
        }
        # A blocked or incomplete child is not a reusable terminal.  It may
        # have been produced before owner authorization, input manifests, or
        # a prerequisite became available; the next acceptance invocation must
        # execute that child again instead of freezing the old blocker into
        # the parent state.
        if receipt.status != "PASSED":
            return None
        evidence_path = Path(str(record.get("evidence_manifest") or "")).expanduser().resolve()
        if not evidence_path.is_file() or evidence_path.is_symlink():
            return None
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            gate_evidence = evidence.get("gates", {}).get(gate) if isinstance(evidence, Mapping) else None
            verified = GateEvidenceVerifier().verify(
                gate,
                gate_evidence if isinstance(gate_evidence, Mapping) else None,
                expected_final_sha=final_sha,
                expected_acceptance_run_id=parent_run_id,
                origin_dir=evidence_path.parent,
                expected_job_id=receipt.job_id,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if verified.get("status") != "PASS":
            return None
        result["verified"] = verified
        if receipt.budget_domain == "offline-k":
            result["status"] = "PASS_OFFLINE"
        else:
            result["status"] = "PASS"
        return result

    @staticmethod
    def _acceptance_receipt_is_completed(receipt: Any) -> bool:
        """Count only semantically completed calls in resume snapshots."""

        if str(getattr(receipt, "status", "") or "").casefold() not in {
            "success",
            "completed",
        }:
            return False
        metadata = getattr(receipt, "metadata", {})
        if not isinstance(metadata, Mapping):
            return True
        semantic_status = str(
            metadata.get("semantic_validation_status") or ""
        ).strip().casefold()
        # A transport-success response is not a completed logical call until
        # the Stage 1 semantic boundary explicitly records ``passed``.  An
        # absent status is intentionally not promoted: the provider receipt
        # may have been durable while the summary/manifest publication was
        # still in flight, so resume may legitimately retry it.
        return semantic_status == "passed"

    def _acceptance_unique_provider_ledger_paths(
        self,
        workspace: str | Path,
        *,
        job_id: str,
    ) -> tuple[str, ...]:
        """Return ledger files without counting mirrored receipt copies twice."""

        candidates = self._acceptance_provider_ledger_paths(workspace, job_id=job_id)
        # Prefer Registry-published ledgers over their publication-staging
        # mirrors when both contain the same receipt identities.  A differing
        # payload for one receipt ID is a conflict, not a reason to silently
        # choose one copy.
        ordered = sorted(
            candidates,
            key=lambda value: (
                ".publication-staging" in str(value).casefold(),
                str(value).casefold(),
            ),
        )
        seen: dict[str, str] = {}
        selected: list[str] = []
        for path in ordered:
            try:
                receipts = ProviderRuntimeLedger(path).list_acceptance_receipts(
                    expected_job_id=job_id
                )
            except (OSError, ValueError, RuntimeError):
                continue
            new_receipt = False
            for receipt in receipts:
                receipt_id = str(receipt.receipt_id).strip()
                encoded = json.dumps(
                    receipt.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                previous = seen.get(receipt_id)
                if previous is not None and previous != encoded:
                    raise ControlPlaneError(
                        "acceptance provider ledgers contain conflicting copies "
                        f"of receipt {receipt_id}"
                    )
                if previous is None:
                    seen[receipt_id] = encoded
                    new_receipt = True
            if new_receipt:
                selected.append(str(path))
        return tuple(selected)

    def _acceptance_ledger_snapshot(
        self,
        workspace: str | Path,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        receipt_rows: list[Any] = []
        ledger_paths = self._acceptance_unique_provider_ledger_paths(
            workspace,
            job_id=job_id,
        )
        for path in ledger_paths:
            try:
                receipt_rows.extend(
                    ProviderRuntimeLedger(path).list_acceptance_receipts(
                        expected_job_id=job_id
                    )
                )
            except (OSError, ValueError, RuntimeError):
                continue
        receipt_ids = [str(item.receipt_id) for item in receipt_rows]
        completed_call_ids = sorted(
            {
                str(item.call_id)
                for item in receipt_rows
                if self._acceptance_receipt_is_completed(item)
                and str(item.call_id).strip()
            }
        )
        return {
            "receipt_ids": sorted(receipt_ids),
            "attempt_ids": sorted(
                {
                    str(item.attempt_id)
                    for item in receipt_rows
                    if str(item.attempt_id).strip()
                }
            ),
            "completed_call_ids": completed_call_ids,
            "provider_calls": sum(int(item.attempts) for item in receipt_rows),
            "output_tokens": sum(int(item.output_tokens or 0) for item in receipt_rows),
            "retry_attempts": sum(max(0, int(item.attempts) - 1) for item in receipt_rows),
            "ledger_paths": list(ledger_paths),
        }

    @staticmethod
    def _acceptance_budget_state_snapshot(path: str | Path) -> dict[str, Any]:
        """Read the run-scoped aggregate budget without mutating it."""

        target = Path(path).expanduser().resolve()
        if not target.is_file() or target.is_symlink():
            return {"state_present": False, "path": str(target)}
        raw = target.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ControlPlaneError("acceptance budget state is not a JSON object")
        return {
            "state_present": True,
            "path": str(target),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "schema_version": str(payload.get("schema_version") or ""),
            "budget": dict(payload.get("budget") or {})
            if isinstance(payload.get("budget"), Mapping)
            else {},
            "calls_used": int(payload.get("calls_used") or 0),
            "calls_reserved": int(payload.get("calls_reserved") or 0),
            "output_tokens_used": int(payload.get("output_tokens_used") or 0),
            "output_tokens_reserved": int(payload.get("output_tokens_reserved") or 0),
            "retry_attempts_used": int(payload.get("retry_attempts_used") or 0),
            "retry_attempts_reserved": int(payload.get("retry_attempts_reserved") or 0),
            "absolute_deadline_epoch": float(
                payload.get("absolute_deadline_epoch") or 0.0
            ),
        }

    @staticmethod
    def _acceptance_last_json(output: str) -> dict[str, Any] | None:
        for line in reversed(str(output or "").splitlines()):
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                return dict(value)
        return None

    @staticmethod
    def _acceptance_safe_resume_output(output: str) -> str:
        """Keep only a short, credential-redacted child failure tail."""

        text = str(output or "")
        if not text:
            return ""
        text = re.sub(
            r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
        return text[-2000:]

    @staticmethod
    def _acceptance_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace_with_retry(temporary, str(path), timeout_seconds=5.0)
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass

    def _execute_acceptance_crash_resume(
        self,
        child: Any,
        *,
        context: AcceptanceExecutionContextV1,
        child_dir: Path,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        """Run Gate E with two real reviewctl processes and a parent kill."""

        runtime_spec = load_runtime_job_spec(child.runtime_spec)
        workspace_text = str(child.workspace or runtime_spec.workspace_path or "").strip()
        if not workspace_text:
            raise ControlPlaneError("crash/resume child requires an explicit workspace")
        workspace = Path(workspace_text).expanduser().resolve()
        declared_workspace, declared_job_id = self._acceptance_runtime_child_binding(
            child,
            runtime_spec,
        )
        workspace = declared_workspace
        job_id = declared_job_id
        if not job_id:
            raise ControlPlaneError("crash/resume child requires a stable job_id")
        child_env = acceptance_context_environment(context)
        child_env["AUTO_GENERATE_ACCEPTANCE_RUN_LIVE_ACCEPTANCE"] = "1"
        initial = subprocess.Popen(
            [sys.executable, "-m", "reviewctl", "run", "--spec", child.runtime_spec],
            cwd=str(self.repo_root),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        initial_identity = process_identity_for_pid(initial.pid)
        if initial_identity.creation_time is None:
            if initial.poll() is None:
                initial.terminate()
                initial.wait(timeout=10)
            raise ControlPlaneError(
                "crash/resume child process creation identity is unavailable"
            )
        started_at = self._utc_now()
        initial_output = ""
        boundary_reached = False
        deadline = float(context.absolute_deadline_epoch or 0.0)
        if deadline <= 0:
            deadline = time.time() + 300.0
        try:
            while initial.poll() is None and time.time() < deadline:
                snapshot = self._acceptance_ledger_snapshot(workspace, job_id=job_id)
                registry_ready = False
                registry_path = workspace / "artifact_registry.json"
                if registry_path.is_file() and not registry_path.is_symlink():
                    try:
                        registry = ArtifactRegistry(registry_path, job_id)
                        registry_ready = any(
                            record.status == "ready"
                            for record in registry.list_records()
                        )
                    except (OSError, RegistryError, ValueError):
                        registry_ready = False
                if snapshot["receipt_ids"] and registry_ready:
                    boundary_reached = True
                    break
                time.sleep(0.2)
            # Terminate as soon as the durable receipt/Registry boundary is
            # observed.  Waiting in ``communicate`` first lets a fast child
            # finish gracefully, turning the intended crash/resume probe into
            # a false timing failure.
            if initial.poll() is None:
                initial.terminate()
            try:
                initial_output = initial.communicate(timeout=15.0)[0] or ""
            except subprocess.TimeoutExpired:
                initial.kill()
                initial_output = initial.communicate(timeout=10.0)[0] or ""
        except BaseException:
            if initial.poll() is None:
                initial.kill()
                initial.wait(timeout=10)
            raise
        if not boundary_reached:
            raise ControlPlaneError(
                "crash/resume child did not reach a durable provider receipt and Registry boundary"
            )
        before = self._acceptance_ledger_snapshot(workspace, job_id=job_id)
        budget_before = self._acceptance_budget_state_snapshot(
            context.provider_budget_state_path
        )
        registry_hash = self._acceptance_file_hash(workspace / "artifact_registry.json", allow_missing=True)
        termination_requested_at = self._utc_now()
        if initial.poll() is None:
            initial.terminate()
        initial_exit = initial.wait(timeout=15.0)
        interrupted_at = self._utc_now()
        if initial_exit == 0:
            raise ControlPlaneError(
                "crash/resume terminate scenario received a graceful child exit"
            )
        if is_process_alive(initial_identity):
            raise ControlPlaneError(
                "crash/resume child remained alive after its reported termination"
            )
        attempt_id = str(before["attempt_ids"][-1] if before["attempt_ids"] else f"{job_id}:initial")
        process_events_path = child_dir / "process_events.jsonl"
        interruption_path = child_dir / "interruption_event.json"
        resume_path = child_dir / "resume_event.json"
        parent_identity = process_identity_for_pid(os.getpid())
        event_rows: list[dict[str, Any]] = []

        def event(name: str, **values: Any) -> None:
            row = dict(values)
            # Values such as the budget snapshot carry their own schema
            # version.  The outer JSONL row must remain a typed process event;
            # otherwise the verifier rejects the entire resume trace.
            row.update(
                {
                    "artifact_type": "acceptance_process_event",
                    "artifact_version": "v1",
                    "schema_version": "process-event-v1",
                    "acceptance_run_id": context.acceptance_run_id,
                    "scenario_id": "E",
                    "job_id": job_id,
                    "process_id": "acceptance-parent",
                    "pid": parent_identity.pid,
                    "process_creation_identity": str(parent_identity.creation_time or "unknown"),
                    "event": name,
                    "occurred_at": self._utc_now(),
                }
            )
            event_rows.append(row)

        event(
            "ledger_snapshot",
            snapshot_name="before",
            **before,
        )
        event(
            "interruption_requested",
            child_pid=initial.pid,
            child_process_creation_identity=str(initial_identity.creation_time or "unknown"),
            interruption_method="terminate",
            requested_at=termination_requested_at,
            interrupted_at=interrupted_at,
            exit_code=initial_exit,
        )
        at_interruption = self._acceptance_ledger_snapshot(workspace, job_id=job_id)
        budget_at_interruption = self._acceptance_budget_state_snapshot(
            context.provider_budget_state_path
        )
        event("ledger_snapshot", snapshot_name="at_interruption", **at_interruption)
        event(
            "budget_snapshot",
            snapshot_name="at_interruption",
            **budget_at_interruption,
        )
        interruption = {
            "artifact_type": "process_interruption_event",
            "artifact_version": "v1",
            "schema_version": "process-interruption-event-v1",
            "event_id": f"{context.acceptance_run_id}:E:interruption",
            "acceptance_run_id": context.acceptance_run_id,
            "scenario_id": "E",
            "job_id": job_id,
            "attempt_id": attempt_id,
            "pid": initial.pid,
            "process_creation_identity": str(initial_identity.creation_time or "unknown"),
            "started_at": started_at,
            "interrupted_at": interrupted_at,
            "interruption_method": "terminate",
            "exit_code": initial_exit,
            "last_durable_stage": "provider_receipt",
            "last_durable_receipt_id": str(before["receipt_ids"][-1]),
        }
        atomic_write_json(str(interruption_path), interruption)
        resume_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reviewctl",
                "resume",
                "--workspace",
                str(workspace),
                "--job",
                job_id,
            ],
            cwd=str(self.repo_root),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        resume_identity = process_identity_for_pid(resume_process.pid)
        try:
            resume_output = resume_process.communicate(
                timeout=max(10.0, min(300.0, deadline - time.time()))
            )[0] or ""
        except subprocess.TimeoutExpired:
            resume_process.kill()
            resume_output = resume_process.communicate(timeout=10.0)[0] or ""
        resume_exit = int(resume_process.returncode or 0)
        resumed_at = self._utc_now()
        if resume_exit != 0:
            resume_payload = self._acceptance_last_json(resume_output) or {}
            diagnostic = {
                key: resume_payload.get(key)
                for key in (
                    "status",
                    "error_type",
                    "error",
                    "job_status",
                    "completion_status",
                    "completion_reasons",
                    "failed_stage",
                    "requires_attention",
                )
                if key in resume_payload
            }
            error_value = diagnostic.get("error")
            if isinstance(error_value, str):
                diagnostic["error"] = error_value[-1000:]
            output_tail = self._acceptance_safe_resume_output(resume_output)
            if output_tail:
                diagnostic["resume_output_tail"] = output_tail
            raise ControlPlaneError(
                "fresh resume process exited unsuccessfully: "
                f"{resume_exit}; diagnostic={json.dumps(diagnostic, sort_keys=True)}"
            )
        after = self._acceptance_ledger_snapshot(workspace, job_id=job_id)
        budget_after = self._acceptance_budget_state_snapshot(
            context.provider_budget_state_path
        )
        if resume_identity.pid == initial_identity.pid and resume_identity.creation_time == initial_identity.creation_time:
            raise ControlPlaneError("crash/resume did not start a distinct fresh process")
        new_attempt_ids = sorted(
            set(after["attempt_ids"]) - set(before["attempt_ids"])
        )
        if not new_attempt_ids:
            raise ControlPlaneError(
                "fresh resume produced no distinct durable attempt identity"
            )
        new_attempt_id = new_attempt_ids[-1]
        resume_event = {
            "artifact_type": "process_resume_event",
            "artifact_version": "v1",
            "schema_version": "process-resume-event-v1",
            "event_id": f"{context.acceptance_run_id}:E:resume",
            "acceptance_run_id": context.acceptance_run_id,
            "scenario_id": "E",
            "job_id": job_id,
            "interruption_event_id": interruption["event_id"],
            "interruption_event_sha256": self._acceptance_file_hash(interruption_path),
            "previous_attempt_id": attempt_id,
            "new_attempt_id": new_attempt_id,
            "new_pid": resume_identity.pid,
            "new_process_creation_identity": str(
                resume_identity.creation_time or "unknown"
            ),
            "resumed_at": resumed_at,
        }
        atomic_write_json(str(resume_path), resume_event)
        event("resume_started", new_pid=resume_identity.pid, new_process_creation_identity=str(resume_identity.creation_time or "unknown"))
        event("ledger_snapshot", snapshot_name="after_resume", **after)
        event("budget_snapshot", snapshot_name="before", **budget_before)
        event("budget_snapshot", snapshot_name="after_resume", **budget_after)
        self._acceptance_write_jsonl(process_events_path, event_rows)
        producer = __import__("runtime.release_acceptance", fromlist=["GateEvidenceProducer"]).GateEvidenceProducer(final_sha=context.final_executable_sha)
        refs: list[dict[str, Any]] = [
            producer.reference(
                interruption_path,
                role="interruption_event",
                artifact_type="process_interruption_event",
                artifact_version="v1",
                schema_version="process-interruption-event-v1",
                job_id=job_id,
            ),
            producer.reference(
                resume_path,
                role="resume_event",
                artifact_type="process_resume_event",
                artifact_version="v1",
                schema_version="process-resume-event-v1",
                job_id=job_id,
            ),
            producer.reference(
                process_events_path,
                role="process_events",
                artifact_type="acceptance_process_event",
                artifact_version="v1",
                schema_version="process-event-v1",
                job_id=job_id,
            ),
        ]
        for ledger_path in after["ledger_paths"]:
            refs.append(
                producer.reference(
                    ledger_path,
                    role="provider_receipt_ledger",
                    artifact_type="provider_receipt_ledger",
                    artifact_version="v1",
                    job_id=job_id,
                )
            )
        resume_result = self._acceptance_last_json(resume_output) or {
            "status": "blocked",
            "job_status": "failed" if resume_exit else "unknown",
            "completion_status": "blocked",
            "success": False,
            "job_id": job_id,
            "workspace_path": str(workspace),
        }
        resume_result.setdefault("job_id", job_id)
        resume_result.setdefault("workspace_path", str(workspace))
        resume_result["initial_run_output_available"] = bool(initial_output)
        resume_result["registry_sha256_before_interruption"] = registry_hash
        return resume_result, refs, str(workspace), job_id

    def _execute_acceptance_validator_challenge(
        self,
        child: Any,
        *,
        context: AcceptanceExecutionContextV1,
        child_dir: Path,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        """Run Gate H against a copy through the current validation service.

        The canonical review draft is never edited.  The owner supplies only
        the challenge input (target block and replacement text); detection,
        repair application, and revalidation all execute through the current
        production validation/repair implementations and publish quarantined
        evidence in the job Registry.
        """

        from copy import deepcopy

        from runtime.release_acceptance import (
            AcceptanceValidatorChallengeInputV1,
            ControlledDefectChallengeV1,
            GateEvidenceProducer,
        )
        from services.job_workspace import publish_json_artifact
        from services.artifact_registry import ArtifactDependencyRefV2
        from validation.current_validation import run_current_validation
        from validation.repair_apply import run_repair_apply
        from validation.repair_models import (
            DependencyHashBundle,
            PatchGranularity,
            PatchProposal,
            PatchTargetSignature,
            RepairPlan,
            RepairPolicy,
            RepairRootCause,
            NOT_APPLICABLE,
        )

        input_path = Path(str(child.input_manifest)).expanduser().resolve()
        try:
            input_payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError(
                f"Gate H input manifest is unreadable: {type(exc).__name__}"
            ) from exc
        challenge_input = AcceptanceValidatorChallengeInputV1.from_mapping(
            input_payload,
            origin_dir=input_path.parent,
        )
        job_id = str(challenge_input.job_id).strip()
        if child.job_id and str(child.job_id).strip() != job_id:
            raise ControlPlaneError(
                "Gate H input job_id does not match the child scenario job_id"
            )
        workspace = Path(str(challenge_input.workspace)).expanduser().resolve()
        if child.workspace and Path(str(child.workspace)).expanduser().resolve() != workspace:
            raise ControlPlaneError(
                "Gate H input workspace does not match the child scenario workspace"
            )
        runtime_spec = load_runtime_job_spec(child.runtime_spec)
        if runtime_spec.job_id and runtime_spec.job_id != job_id:
            raise ControlPlaneError(
                "Gate H RuntimeJobSpec job_id does not match the challenge input"
            )
        if runtime_spec.workspace_path:
            declared_workspace = Path(runtime_spec.workspace_path).expanduser().resolve()
            if declared_workspace != workspace:
                raise ControlPlaneError(
                    "Gate H RuntimeJobSpec workspace does not match the challenge input"
                )

        bridge = AgentRuntimeBridge(runtime_spec)
        session = bridge.bootstrap(
            resume_requested=True,
            claim_latest_pointer=False,
            publish_running_state=False,
        )
        registry = session.context.registry
        workspace_obj = session.context.workspace
        if Path(workspace_obj.root_dir).resolve() != workspace:
            raise ControlPlaneError(
                "Gate H resolved workspace is not the RuntimeJobSpec workspace"
            )
        validation_service = bridge.build_validation_service(
            session,
            attempt_id=f"acceptance-H-detection:{job_id}:{time.time_ns()}",
        )
        baseline_record = validation_service.review_draft_record
        manifest_record = validation_service.citation_manifest_record
        if baseline_record is None or manifest_record is None:
            raise ControlPlaneError(
                "Gate H requires current review draft and citation manifest records"
            )
        if baseline_record.artifact_id != challenge_input.baseline_review_artifact_id:
            raise ControlPlaneError(
                "Gate H challenge input names a different baseline review artifact"
            )
        baseline_raw = Path(baseline_record.path).read_bytes()
        baseline_hash = hashlib.sha256(baseline_raw).hexdigest()
        if baseline_hash != baseline_record.content_hash:
            raise ControlPlaneError("Gate H baseline review draft hash is stale")
        if baseline_hash != challenge_input.baseline_review_hash:
            raise ControlPlaneError(
                "Gate H challenge input is bound to a different review draft"
            )
        try:
            baseline_payload = json.loads(baseline_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("Gate H baseline review draft is not valid JSON") from exc
        if not isinstance(baseline_payload, Mapping):
            raise ControlPlaneError("Gate H baseline review draft is not an object")
        content = baseline_payload.get("content")
        sections = content.get("sections") if isinstance(content, Mapping) else None
        if not isinstance(sections, list):
            raise ControlPlaneError("Gate H baseline review draft has no structured sections")
        original_text = ""
        mutation_locator = ""
        mutated_payload = deepcopy(dict(baseline_payload))
        mutated_content = mutated_payload.get("content")
        mutated_sections = (
            mutated_content.get("sections")
            if isinstance(mutated_content, Mapping)
            else None
        )
        if not isinstance(mutated_sections, list):
            raise ControlPlaneError("Gate H baseline review draft sections are invalid")
        for section_index, section in enumerate(sections):
            if not isinstance(section, Mapping):
                continue
            blocks = section.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block_index, block in enumerate(blocks):
                if not isinstance(block, Mapping) or str(block.get("block_id") or "") != challenge_input.block_id:
                    continue
                original_text = str(block.get("text") or "")
                mutation_locator = (
                    f"content.sections[{section_index}].blocks[{block_index}].text"
                )
                mutated_block = mutated_sections[section_index]["blocks"][block_index]
                if not isinstance(mutated_block, dict):
                    raise ControlPlaneError("Gate H target block is not mutable structured data")
                mutated_block["text"] = str(challenge_input.mutated_text)
                break
            if mutation_locator:
                break
        if not mutation_locator:
            raise ControlPlaneError(
                f"Gate H challenge block is missing: {challenge_input.block_id}"
            )
        mutated_text = str(challenge_input.mutated_text)
        if not original_text or original_text == mutated_text:
            raise ControlPlaneError(
                "Gate H challenge must change a non-empty block in a copy"
            )
        challenge_id = challenge_input.challenge_id
        challenge_dir = child_dir / "evidence" / "H" / challenge_id
        challenge_dir.mkdir(parents=True, exist_ok=True)
        mutated_path = Path(
            workspace_obj.artifact_path(
                f"acceptance/H/{challenge_id}/mutated_review_draft.json"
            )
        )
        mutated_record = publish_json_artifact(
            session.context.publication_context,
            registry,
            mutated_path,
            mutated_payload,
            artifact_role="acceptance_validator_challenge_review_draft",
            artifact_type="review_draft",
            artifact_version="v3",
            producer="runtime.control_plane.ReviewControlPlane.GateH",
            artifact_id=f"acceptance-H-mutated:{challenge_id}",
            status="quarantined",
            depends_on=(ArtifactDependencyRefV2.from_record(baseline_record),),
            metadata={
                "acceptance_run_id": context.acceptance_run_id,
                "challenge_id": challenge_id,
                "canonical_replacement": False,
            },
        )
        challenge = ControlledDefectChallengeV1(
            challenge_id=challenge_id,
            baseline_review_artifact_id=baseline_record.artifact_id,
            baseline_review_hash=baseline_hash,
            mutated_review_path=mutated_record.path,
            mutated_review_hash=mutated_record.content_hash,
            mutation_type=challenge_input.mutation_type,
            mutation_locator=mutation_locator,
            original_value_hash=hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
            mutated_value_hash=hashlib.sha256(mutated_text.encode("utf-8")).hexdigest(),
            expected_detection_class=challenge_input.expected_detection_class,
        )
        challenge_path = challenge_dir / "controlled_defect_challenge.json"
        atomic_write_json(
            str(challenge_path),
            {
                "artifact_type": "controlled_defect_challenge",
                "artifact_version": "v1",
                "schema_version": "controlled-defect-challenge-v1",
                **challenge.__dict__,
            },
        )

        def load_mapping(path: str | Path) -> Mapping[str, Any]:
            try:
                value = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ControlPlaneError(f"Gate H artifact is unreadable: {path}") from exc
            if not isinstance(value, Mapping):
                raise ControlPlaneError(f"Gate H artifact is not an object: {path}")
            return value

        def matched_claims(result_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
            raw_claims = result_payload.get("claim_results")
            if not isinstance(raw_claims, list):
                return []
            return [
                claim
                for claim in raw_claims
                if isinstance(claim, Mapping)
                and challenge_input.block_id
                in [str(item) for item in claim.get("block_ids") or []]
                and str(claim.get("verdict") or "").casefold()
                not in {"supported", "clean"}
            ]

        paper_payloads: list[dict[str, Any]] = []
        paper_ids: list[str] = []
        for record in validation_service.paper_artifact_records:
            if record.status != "ready":
                continue
            payload = load_mapping(record.path)
            paper_payloads.append(dict(payload))
            identity = payload.get("paper_identity")
            if isinstance(identity, Mapping):
                key = str(identity.get("canonical_paper_key") or "").strip()
                if key:
                    paper_ids.append(key)
        citation_manifest = load_mapping(manifest_record.path)
        detection_service = replace(
            validation_service,
            review_draft_record=mutated_record,
            attempt_id=f"acceptance-H-detection:{job_id}:{time.time_ns()}",
        )
        detection = run_current_validation(
            detection_service,
            review_draft_override=mutated_payload,
            citation_manifest_override=citation_manifest,
            paper_artifacts_override=paper_payloads,
            review_draft_record_override=mutated_record,
            citation_manifest_record_override=manifest_record,
            output_dir=workspace_obj.artifact_path(
                f"acceptance/H/{challenge_id}/detection"
            ),
            result_artifact_id=f"acceptance-H-validation-detection:{challenge_id}",
        )
        detection_payload = detection.get("validation_run_result_payload")
        if not isinstance(detection_payload, Mapping):
            raise ControlPlaneError("Gate H detection did not return a typed validation result")
        detection_claims = matched_claims(detection_payload)
        if not detection_claims:
            raise ControlPlaneError(
                "Gate H current validation did not detect the challenged block mutation"
            )
        expected_detection_class = challenge_input.expected_detection_class.casefold()
        if not any(
            expected_detection_class
            in {
                str(claim.get("verdict") or "").casefold(),
                *{
                    str(item).casefold()
                    for item in claim.get("root_causes") or ()
                },
            }
            for claim in detection_claims
        ):
            raise ControlPlaneError(
                "Gate H validation did not produce the expected detection class"
            )
        detected_findings = [
            {
                "finding_id": str(claim.get("claim_result_id") or "").strip(),
                "mutation_locator": mutation_locator,
                "verdict": str(claim.get("verdict") or ""),
                "detection_class": challenge_input.expected_detection_class,
                "source_evidence": list(claim.get("evidence_candidates") or ()),
            }
            for claim in detection_claims
            if str(claim.get("claim_result_id") or "").strip()
        ]
        if not detected_findings:
            raise ControlPlaneError("Gate H detection claims have no finding identities")

        dependency_bundle = DependencyHashBundle(
            summary_hash=NOT_APPLICABLE,
            paper_artifact_hash=NOT_APPLICABLE,
            visual_manifest_hash=NOT_APPLICABLE,
            selected_visual_refs_hash=NOT_APPLICABLE,
            review_draft_hash=NOT_APPLICABLE,
            citation_manifest_hash=NOT_APPLICABLE,
            outline_hash=NOT_APPLICABLE,
        )
        proposal = PatchProposal(
            proposal_id=f"acceptance-H-repair:{challenge_id}",
            citation_id=challenge_id,
            root_cause=RepairRootCause.REVIEW_DRIFT,
            granularity=PatchGranularity.BLOCK,
            target=PatchTargetSignature(
                block_id=challenge_input.block_id,
                anchor_text=mutated_text,
                anchor_hash=hashlib.sha256(mutated_text.encode("utf-8")).hexdigest()[:8],
            ),
            original_text=mutated_text,
            proposed_text=original_text,
            confidence=1.0,
            fix_strategy="controlled_defect_restore",
            dependency_bundle=dependency_bundle,
            metadata={
                "paper_ids": list(dict.fromkeys(paper_ids)),
                "challenge_id": challenge_id,
                "mutation_locator": mutation_locator,
            },
        )
        repair_plan = RepairPlan(
            plan_id=f"acceptance-H-plan:{challenge_id}",
            created_at=self._utc_now(),
            created_from_job_id=job_id,
            validation_report_id=str(
                detection_payload.get("validation_run_id") or ""
            ),
            proposals=[proposal],
            policy=RepairPolicy.AUTO_APPLY_SAFE,
        )
        repair_payload = run_repair_apply(
            repair_plan=repair_plan,
            review_draft=deepcopy(dict(mutated_payload)),
            citation_manifest=deepcopy(dict(citation_manifest)),
            paper_artifacts=paper_payloads,
            job_id=job_id,
            visual_manifest={},
            dry_run=False,
            require_auto_safe=False,
        )
        apply_result = repair_payload.get("apply_result")
        patched_payload = repair_payload.get("patched_review_draft")
        if (
            not isinstance(apply_result, Mapping)
            or int(apply_result.get("applied_count") or 0) != 1
            or not isinstance(patched_payload, Mapping)
        ):
            raise ControlPlaneError("Gate H repair executor did not apply exactly one guarded patch")
        patched_content = patched_payload.get("content")
        patched_sections = (
            patched_content.get("sections")
            if isinstance(patched_content, Mapping)
            else None
        )
        if not isinstance(patched_sections, list) or not patched_sections:
            raise ControlPlaneError("Gate H repair output has no structured sections")
        repaired_text = ""
        for section in patched_sections:
            if not isinstance(section, Mapping):
                continue
            for block in section.get("blocks", []) or ():
                if isinstance(block, Mapping) and str(block.get("block_id") or "") == challenge_input.block_id:
                    repaired_text = str(block.get("text") or "")
                    break
            if repaired_text:
                break
        if repaired_text != original_text:
            raise ControlPlaneError("Gate H repair output did not restore the challenged value")
        patched_path = Path(
            workspace_obj.artifact_path(
                f"acceptance/H/{challenge_id}/repaired_review_draft.json"
            )
        )
        patched_record = publish_json_artifact(
            session.context.publication_context,
            registry,
            patched_path,
            dict(patched_payload),
            artifact_role="acceptance_validator_repaired_review_draft",
            artifact_type="review_draft",
            artifact_version="v3",
            producer="runtime.control_plane.ReviewControlPlane.GateH",
            artifact_id=f"acceptance-H-repaired:{challenge_id}",
            status="quarantined",
            depends_on=(ArtifactDependencyRefV2.from_record(mutated_record),),
            metadata={
                "acceptance_run_id": context.acceptance_run_id,
                "challenge_id": challenge_id,
                "canonical_replacement": False,
            },
        )
        repair_artifact_path = challenge_dir / "repair_transaction.json"
        atomic_write_json(
            str(repair_artifact_path),
            {
                "artifact_type": "repair_transaction",
                "artifact_version": "v1",
                "schema_version": "repair-transaction-v1",
                "transaction_id": f"acceptance-H-repair:{challenge_id}",
                "job_id": job_id,
                "challenge_id": challenge_id,
                "status": "applied",
                "before_hash": mutated_record.content_hash,
                "after_hash": patched_record.content_hash,
                "changed_locator": mutation_locator,
                "applied_records": list(repair_payload.get("applied_records") or ()),
            },
        )
        revalidation_service = replace(
            validation_service,
            review_draft_record=patched_record,
            attempt_id=f"acceptance-H-revalidation:{job_id}:{time.time_ns()}",
        )
        revalidation = run_current_validation(
            revalidation_service,
            review_draft_override=dict(patched_payload),
            citation_manifest_override=citation_manifest,
            paper_artifacts_override=paper_payloads,
            review_draft_record_override=patched_record,
            citation_manifest_record_override=manifest_record,
            output_dir=workspace_obj.artifact_path(
                f"acceptance/H/{challenge_id}/revalidation"
            ),
            validation_scope="repair_revalidation",
            result_artifact_id=f"acceptance-H-validation-revalidation:{challenge_id}",
        )
        revalidation_payload = revalidation.get("validation_run_result_payload")
        if not isinstance(revalidation_payload, Mapping):
            raise ControlPlaneError("Gate H revalidation did not return a typed validation result")
        remaining_claims = matched_claims(revalidation_payload)
        execution_status = str(
            revalidation_payload.get("execution_status") or ""
        ).casefold()
        if execution_status not in {"succeeded", "success", "completed"} or remaining_claims:
            raise ControlPlaneError(
                "Gate H revalidation did not prove that the challenged finding was resolved"
            )
        validation_artifact_path = challenge_dir / "validation_challenge_result.json"
        detection_result_path = str(detection.get("validation_run_result_file") or "")
        revalidation_result_path = str(revalidation.get("validation_run_result_file") or "")
        validation_rows = [
            {
                "artifact_type": "validation_completion_projection",
                "artifact_version": "v1",
                "schema_version": "validation-completion-projection-v1",
                "job_id": job_id,
                "challenge_id": challenge_id,
                "phase": "detection",
                "mutation_locator": mutation_locator,
                "detected_findings": detected_findings,
                "validation_run_result_path": detection_result_path,
                "validation_run_result_sha256": self._acceptance_file_hash(
                    detection_result_path
                ),
            },
            {
                "artifact_type": "validation_completion_projection",
                "artifact_version": "v1",
                "schema_version": "validation-completion-projection-v1",
                "job_id": job_id,
                "challenge_id": challenge_id,
                "phase": "revalidation",
                "status": "clean",
                "mutation_locator": mutation_locator,
                "remaining_challenge_findings": [],
                "resolved_finding_ids": [
                    item["finding_id"] for item in detected_findings
                ],
                "validation_run_result_path": revalidation_result_path,
                "validation_run_result_sha256": self._acceptance_file_hash(
                    revalidation_result_path
                ),
            },
        ]
        atomic_write_json(str(validation_artifact_path), validation_rows)
        ledger_paths: list[Path] = []
        for validation_run in (detection_service, revalidation_service):
            ledger = validation_run.provider_receipt_ledger
            receipts = ledger.list_acceptance_receipts(expected_job_id=job_id)
            if receipts:
                candidate = Path(ledger.path).expanduser().resolve()
                if candidate not in ledger_paths:
                    ledger_paths.append(candidate)
        if not ledger_paths:
            raise ControlPlaneError(
                "Gate H requires at least one real non-test Validator provider receipt"
            )
        producer = GateEvidenceProducer(final_sha=context.final_executable_sha)
        refs = [
            producer.reference(
                challenge_path,
                role="defect_artifact",
                artifact_type="controlled_defect_challenge",
                artifact_version="v1",
                schema_version="controlled-defect-challenge-v1",
                job_id=job_id,
                artifact_id=f"acceptance-H-challenge:{challenge_id}",
            ),
            producer.reference(
                repair_artifact_path,
                role="repair_artifact",
                artifact_type="repair_transaction",
                artifact_version="v1",
                schema_version="repair-transaction-v1",
                job_id=job_id,
                artifact_id=f"acceptance-H-repair:{challenge_id}",
            ),
            producer.reference(
                validation_artifact_path,
                role="validation_artifact",
                artifact_type="validation_completion_projection",
                artifact_version="v1",
                schema_version="validation-completion-projection-v1",
                job_id=job_id,
            ),
        ]
        refs.extend(
            producer.reference(
                ledger_path,
                role="provider_receipt_ledger",
                artifact_type="provider_receipt_ledger",
                artifact_version="v1",
                job_id=job_id,
            )
            for ledger_path in ledger_paths
        )
        return (
            {
                "status": "complete",
                "job_status": "completed",
                "completion_status": "complete",
                "success": True,
                "job_id": job_id,
                "workspace_path": str(workspace),
                "validation_detection": detection_payload.get("validation_run_id"),
                "validation_revalidation": revalidation_payload.get("validation_run_id"),
            },
            refs,
            str(workspace),
            job_id,
        )

    def _acceptance_run_plan(
        self,
        spec_path: Path,
        acceptance_spec: Any,
    ) -> dict[str, Any]:
        from runtime.release_acceptance import (
            AcceptanceRunStateV1,
            AcceptanceScenarioContextV1,
            GateEvidenceProducer,
            GateEvidenceVerifier,
            ParentAcceptanceResultV2,
            gate_contract,
            scenario_for_gate,
        )

        plan = acceptance_spec.plan
        if plan is None:
            raise ControlPlaneError("acceptance plan is missing")
        execution_context_owner_authorized = os.getenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "0") == "1"
        current_sha = self._acceptance_checkout_sha(
            self.repo_root,
            require_clean=execution_context_owner_authorized,
        )
        if plan.final_executable_sha and plan.final_executable_sha != current_sha:
            raise ControlPlaneError(
                "acceptance plan executable SHA does not match the current checkout"
            )
        plan_sha = plan.plan_sha256()
        state_path = _acceptance_lexical_path(
            acceptance_spec.state_path
            or spec_path.with_name("acceptance_plan_state_v2.json")
        )
        if state_path.suffix.casefold() != ".json":
            state_path = _acceptance_lexical_path(
                state_path / "acceptance_plan_state_v2.json"
            )
        state: AcceptanceRunStateV1 | None = None
        with interprocess_file_lock(state_path):
            if state_path.is_file():
                try:
                    state = AcceptanceRunStateV1.from_mapping(
                        json.loads(state_path.read_text(encoding="utf-8"))
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    state = None
            compatible = bool(
                state is not None
                and state.final_sha == current_sha
                and state.plan_sha256 == plan_sha
                and state.acceptance_spec_path == str(spec_path)
            )
            if not compatible:
                run_id = plan.parent_run_id
                if state is not None and state.final_sha != current_sha:
                    run_id = f"{plan.parent_run_id}-{current_sha[:12]}"
                state = AcceptanceRunStateV1(
                    run_id=run_id,
                    final_sha=current_sha,
                    acceptance_spec_path=str(spec_path),
                    runtime_spec_path="",
                    status="planned",
                    gates={
                        gate: {
                            "gate": gate,
                            "scenario_id": gate,
                            "status": "NOT_RUN",
                            "contract": gate_contract(gate),
                        }
                        for gate in plan.gates
                    },
                    updated_at=self._utc_now(),
                    plan_sha256=plan_sha,
                )
            if state is None:
                raise ControlPlaneError("acceptance plan state could not be initialized")
            # A blocked/inspection path must not erase a previously bound
            # budget identity. A zero-call snapshot can still have an active
            # reservation or a running wall-clock deadline.
            budget_state_was_bound = bool(state.provider_budget_state_path)
            run_dir, evidence_root = self._bind_acceptance_evidence_root(
                state_path,
                state,
            )
            state = replace(
                state,
                plan_sha256=plan_sha,
                evidence_root=str(evidence_root),
                provider_budget_state_path=(
                    state.provider_budget_state_path
                    or (str(run_dir / "provider_budget_state_v1.json")
                        if execution_context_owner_authorized else "")
                ),
                process_event_log=state.process_event_log or str(run_dir / "process_events.jsonl"),
            )
            atomic_write_json(str(state_path), state.to_dict())

        if state is None:
            raise ControlPlaneError("acceptance plan state could not be initialized")
        budget_controller = ProviderBudgetController(plan.budget.to_provider_budget())
        if execution_context_owner_authorized:
            budget_controller.bind_state_path(
                state.provider_budget_state_path,
                acceptance_run_id=state.run_id,
                state_started=budget_state_was_bound,
            )
        budget_snapshot = budget_controller.snapshot()
        execution_context = AcceptanceExecutionContextV1(
            acceptance_run_id=state.run_id,
            final_executable_sha=current_sha,
            absolute_deadline_epoch=(
                float(budget_snapshot.get("absolute_deadline_epoch") or 0.0)
                if execution_context_owner_authorized
                else 0.0
            ),
            provider_budget=plan.budget.to_provider_budget(),
            provider_budget_state_path=(
                state.provider_budget_state_path
                or str(Path(state.evidence_root).expanduser().resolve().parent / "provider_budget_state_v1.json")
            ),
            evidence_root=state.evidence_root,
            process_event_log=state.process_event_log,
            scenario_state_path=str(state_path),
            owner_authorized=execution_context_owner_authorized,
            provider_budget_state_started=execution_context_owner_authorized,
        )
        child_states = dict(state.child_states)
        child_results: dict[str, dict[str, Any]] = {}
        expected_child_bindings: dict[str, dict[str, Any]] = {}
        for gate, child in plan.scenarios.items():
            runtime_job_spec: RuntimeJobSpec | None = None
            child_preflight: Mapping[str, Any] | None = None
            runtime_admission_error = ""
            runtime_execution_mode = child.execution_mode in {
                "runtime",
                "ocr",
                "crash_resume",
                "validator_challenge",
                "playwright",
            }
            # A prior result is never reusable until the current spec/config
            # route has been admitted again. Fresh non-authorized children keep
            # their useful BLOCKED_OWNER_INPUT state without probing config.
            if runtime_execution_mode and child.runtime_spec and (
                execution_context_owner_authorized or bool(child_states.get(gate))
            ):
                try:
                    runtime_job_spec = load_runtime_job_spec(child.runtime_spec)
                    if (
                        execution_context_owner_authorized
                        and child.budget_domain == "live"
                        and child.execution_mode
                        in {
                            "runtime",
                            "ocr",
                            "crash_resume",
                            "validator_challenge",
                        }
                        and Path(runtime_job_spec.config).expanduser().is_file()
                    ):
                        self._require_live_runtime_deadline(runtime_job_spec)
                    child_preflight = self.provider_preflight(
                        config_path=runtime_job_spec.config,
                        action=runtime_job_spec.action,
                        requested_stages=runtime_job_spec.metadata.get("requested_stages"),
                        outline_pilot=runtime_job_spec.metadata.get("outline_pilot"),
                        free_mode_enabled=bool(
                            runtime_job_spec.free_mode_profile
                            or runtime_job_spec.free_mode_idea
                            or runtime_job_spec.metadata.get("free_mode_input")
                        ),
                    )
                    if child_preflight.get("ok") is False:
                        mineru_admission = child_preflight.get("mineru_remote_admission")
                        reason = (
                            str(mineru_admission.get("reason") or "")
                            if isinstance(mineru_admission, Mapping)
                            else ""
                        ) or str(child_preflight.get("error_type") or "provider_preflight_failed")
                        raise ControlPlaneError(
                            "acceptance child provider preflight did not admit execution: " + reason
                        )
                    spec_admission = self._admit_runtime_spec_external_hosts(runtime_job_spec)
                    plan_acknowledgement = getattr(
                        plan,
                        "external_host_acknowledgement",
                        None,
                    )
                    spec_acknowledgement = runtime_job_spec.metadata.get(
                        "external_host_acknowledgement"
                    )
                    if spec_admission.get("required"):
                        if plan_acknowledgement is None:
                            raise ControlPlaneError(
                                "acceptance plan is missing the required external host acknowledgement"
                            )
                        if not isinstance(spec_acknowledgement, Mapping) or _canonical_hash(
                            dict(spec_acknowledgement)
                        ) != _canonical_hash(plan_acknowledgement.to_dict()):
                            raise ControlPlaneError(
                                "acceptance child RuntimeJobSpec external host acknowledgement "
                                "does not match the parent plan"
                            )
                    # A parent plan may carry one acknowledgement for a
                    # subset of children. Children whose reachable route plan
                    # has no external hosts must ignore that sibling-route
                    # acknowledgement; only a child that actually reaches an
                    # external host must prove the matching child metadata.
                except (ControlPlaneError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
                    runtime_admission_error = (
                        "acceptance child runtime admission failed closed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            child_runtime_hash = self._acceptance_file_hash(
                child.runtime_spec,
                allow_missing=True,
            )
            prior_child_state = child_states.get(gate)
            if (
                not child_runtime_hash
                and isinstance(prior_child_state, Mapping)
                and str(prior_child_state.get("runtime_spec_sha256") or "").strip()
            ):
                child_runtime_hash = str(prior_child_state["runtime_spec_sha256"]).strip().lower()
            input_identity = self._acceptance_input_identity(
                child,
                runtime_spec_hash=child_runtime_hash,
            )
            expected_binding: dict[str, Any] = {
                "plan_sha256": plan_sha,
                "runtime_spec_sha256": child_runtime_hash,
                "input_identity_sha256": input_identity,
                "budget_domain": child.budget_domain,
            }
            if child.job_id:
                expected_binding["job_id"] = child.job_id
            elif child.execution_mode in {"runtime", "ocr", "crash_resume", "validator_challenge"}:
                try:
                    expected_binding["job_id"] = (
                        runtime_job_spec.job_id
                        if runtime_job_spec is not None
                        else load_runtime_job_spec(child.runtime_spec).job_id
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            expected_child_bindings[gate] = expected_binding
            existing = None
            if not runtime_admission_error:
                existing = self._load_acceptance_child_result(
                    child_states.get(gate, {}) if isinstance(child_states.get(gate), Mapping) else {},
                    gate=gate,
                    final_sha=current_sha,
                    parent_run_id=state.run_id,
                    plan_sha256=plan_sha,
                    runtime_spec_sha256=child_runtime_hash,
                    input_identity_sha256=input_identity,
                )
            if existing is not None:
                child_results[gate] = existing
                continue
            child_dir = run_dir / gate
            child_dir.mkdir(parents=True, exist_ok=True)
            evidence_root = child_dir / "evidence"
            evidence_root.mkdir(parents=True, exist_ok=True)
            receipt_path = child_dir / "scenario_execution_receipt.json"
            evidence_path = child_dir / "evidence_index_v1.json"
            child_state_path = child_dir / "scenario_state_v2.json"
            child_job_id = child.job_id or f"{state.run_id}:{gate}"
            child_context = AcceptanceScenarioContextV1(
                acceptance_run_id=state.run_id,
                final_executable_sha=current_sha,
                runtime_spec_path=child.runtime_spec,
                workspace_path=child.workspace,
                job_id=child_job_id,
                evidence_root=str(evidence_root),
                process_event_log=str(child_dir / "process_events.jsonl"),
                owner_authorized=(
                    execution_context.owner_authorized or child.budget_domain == "offline-k"
                ),
                provider_budget=execution_context.provider_budget.to_dict(),
                provider_budget_state_path=execution_context.provider_budget_state_path,
                plan_sha256=plan_sha,
                scenario_execution_receipt_path=str(receipt_path),
                input_identity_sha256=input_identity,
                budget_domain=child.budget_domain,
                input_manifest_path=child.input_manifest,
                absolute_deadline_epoch=execution_context.absolute_deadline_epoch,
            )
            started_at = self._utc_now()
            runtime_result: dict[str, Any] | None = None
            refs: list[dict[str, Any]] = []
            scenario_result: Any | None = None
            scenario = scenario_for_gate(gate)
            blocked_reason = ""
            try:
                prerequisites = [str(item).upper() for item in child.prerequisites]
                unmet = [
                    item
                    for item in prerequisites
                    if str(child_results.get(item, {}).get("status") or "")
                    not in {"PASS", "PASS_OFFLINE"}
                ]
                if runtime_admission_error:
                    blocked_reason = runtime_admission_error
                elif unmet:
                    blocked_reason = "acceptance prerequisites are not passed: " + ", ".join(unmet)
                elif child.execution_mode in {"runtime", "ocr"}:
                    if not execution_context.owner_authorized:
                        blocked_reason = "owner authorization is required for live child execution"
                    elif not child.runtime_spec:
                        blocked_reason = "runtime child is missing an independent RuntimeJobSpec"
                    else:
                        runtime_job_spec = runtime_job_spec or load_runtime_job_spec(child.runtime_spec)
                        declared_workspace, declared_job_id = self._acceptance_runtime_child_binding(
                            child,
                            runtime_job_spec,
                        )
                        child_preflight = child_preflight or self.provider_preflight(
                            config_path=runtime_job_spec.config,
                            action=runtime_job_spec.action,
                            requested_stages=runtime_job_spec.metadata.get("requested_stages"),
                            outline_pilot=runtime_job_spec.metadata.get("outline_pilot"),
                            free_mode_enabled=bool(
                                runtime_job_spec.free_mode_profile
                                or runtime_job_spec.free_mode_idea
                                or runtime_job_spec.metadata.get("free_mode_input")
                            ),
                        )
                        if child_preflight.get("ok") is False:
                            admission = child_preflight.get("mineru_remote_admission")
                            reason = (
                                str(admission.get("reason") or "")
                                if isinstance(admission, Mapping)
                                else ""
                            ) or str(child_preflight.get("error_type") or "provider_preflight_failed")
                            raise ControlPlaneError(
                                "acceptance child provider preflight did not admit execution: "
                                + reason
                            )
                        # A claimed child workspace is durable execution state.
                        # Starting a fresh run against it would either reject
                        # the workspace or duplicate completed work. Resume
                        # only after the RuntimeJobSpec binding above has
                        # verified the exact workspace and job identity.
                        workspace_exists = declared_workspace.is_dir()
                        resume_marker = any(
                            (declared_workspace / marker).is_file()
                            for marker in (
                                "artifact_registry.json",
                                "artifacts/runtime_job_spec_v1.json",
                                "job_outcome_v1.json",
                            )
                        )
                        if workspace_exists and not resume_marker:
                            try:
                                has_unrecognized_state = any(
                                    declared_workspace.iterdir()
                                )
                            except OSError as exc:
                                raise ControlPlaneError(
                                    "cannot inspect existing runtime child workspace"
                                ) from exc
                            prior_child_attempt = bool(child_states.get(gate)) or receipt_path.is_file()
                            if prior_child_attempt:
                                raise ControlPlaneError(
                                    "runtime child workspace exists without a durable "
                                    "resume marker after a prior attempt; refusing to "
                                    "start a second run"
                                )
                            elif not has_unrecognized_state:
                                # A directory claimed before the first durable
                                # artifact is not resumable state. It is safe
                                # to remove this empty, run-owned shell and
                                # let the normal new-run path claim it.
                                declared_workspace.rmdir()
                                workspace_exists = False
                        resume_existing = workspace_exists and resume_marker
                        with bind_acceptance_execution_context(execution_context, budget_controller):
                            runtime_result = (
                                self.resume(
                                    workspace=declared_workspace,
                                    job_id=declared_job_id,
                                )
                                if resume_existing
                                else self.run(child.runtime_spec)
                            )
                        job_value = str(runtime_result.get("job_id") or declared_job_id)
                        if job_value != declared_job_id:
                            raise ControlPlaneError(
                                "runtime child result job_id does not match the RuntimeJobSpec"
                            )
                        child_context = replace(child_context, job_id=job_value)
                        refs.append(
                            GateEvidenceProducer(final_sha=current_sha).reference(
                                child.runtime_spec,
                                role="runtime_spec",
                                artifact_type="runtime_job_spec",
                                artifact_version="v1",
                                job_id=job_value,
                            )
                        )
                        workspace = str(
                            runtime_result.get("workspace_path") or declared_workspace
                        )
                        if Path(workspace).expanduser().resolve() != declared_workspace:
                            raise ControlPlaneError(
                                "runtime child result workspace_path does not match the RuntimeJobSpec"
                            )
                        if gate == "D":
                            # Profile publication updates the child Registry;
                            # inventory it only afterwards so the Registry ref
                            # carries the post-profile content hash.
                            refs.extend(
                                self._acceptance_production_modality_references(
                                    runtime_job_spec,
                                    workspace=workspace,
                                    final_sha=current_sha,
                                    job_id=job_value,
                                    profile_root=evidence_root,
                                    gate=gate,
                                    f1_source_ids=child.f1_source_ids,
                                    runtime_spec_path=child.runtime_spec,
                                )
                            )
                        if workspace and Path(workspace).is_dir():
                            refs.extend(
                                self._acceptance_workspace_references(
                                    workspace,
                                    final_sha=current_sha,
                                    job_id=job_value,
                                )
                            )
                        refs.extend(
                            self._acceptance_source_references(
                                runtime_job_spec,
                                final_sha=current_sha,
                                job_id=job_value,
                                profile_root=evidence_root,
                                gate=gate,
                                f1_source_ids=child.f1_source_ids,
                            )
                        )
                        child_context = replace(
                            child_context,
                            workspace_path=workspace,
                            job_id=job_value,
                        )
                elif child.execution_mode == "crash_resume":
                    if not execution_context.owner_authorized:
                        blocked_reason = "owner authorization is required for crash/resume execution"
                    else:
                        runtime_result, refs, workspace, job_value = self._execute_acceptance_crash_resume(
                            child,
                            context=execution_context,
                            child_dir=child_dir,
                        )
                        child_context = replace(
                            child_context,
                            workspace_path=workspace,
                            job_id=job_value,
                        )
                elif child.execution_mode == "validator_challenge":
                    if not execution_context.owner_authorized:
                        blocked_reason = "owner authorization is required for Validator challenge execution"
                    else:
                        with bind_acceptance_execution_context(
                            execution_context,
                            budget_controller,
                        ):
                            runtime_result, refs, workspace, job_value = (
                                self._execute_acceptance_validator_challenge(
                                    child,
                                    context=execution_context,
                                    child_dir=child_dir,
                                )
                            )
                        child_context = replace(
                            child_context,
                            workspace_path=workspace,
                            job_id=job_value,
                        )
                elif child.execution_mode == "playwright":
                    if not execution_context.owner_authorized and os.getenv("AUTO_GENERATE_RUN_PLAYWRIGHT") != "1":
                        blocked_reason = "explicit Playwright authorization is required"
                    else:
                        # The browser evidence must be tied to the runtime job
                        # named by the GUI input, not to a synthetic parent
                        # child ID.  The typed Gate I parser performs the
                        # remaining schema/localhost checks.
                        try:
                            input_payload = json.loads(
                                Path(child.input_manifest).read_text(encoding="utf-8")
                            )
                        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                            raise ControlPlaneError(
                                f"Gate I input manifest is unreadable: {type(exc).__name__}"
                            ) from exc
                        is_production_v2 = (
                            isinstance(input_payload, Mapping)
                            and input_payload.get("artifact_version") == "v2"
                        )
                        resulting_job_id = str(
                            input_payload.get("resulting_job_id") or ""
                        ).strip() if isinstance(input_payload, Mapping) else ""
                        if not is_production_v2 and not resulting_job_id:
                            raise ControlPlaneError(
                                "Gate I input manifest must bind a resulting job ID"
                            )
                        if not child.runtime_spec and not is_production_v2:
                            raise ControlPlaneError(
                                "Gate I requires an explicit production RuntimeJobSpec"
                            )
                        if is_production_v2:
                            child_context = replace(
                                child_context,
                                runtime_spec_path="",
                            )
                        else:
                            gui_runtime_spec = load_runtime_job_spec(child.runtime_spec)
                            gui_workspace, gui_job_id = self._acceptance_runtime_child_binding(
                                child,
                                gui_runtime_spec,
                            )
                            input_workspace = Path(
                                str(input_payload.get("workspace") or "")
                            ).expanduser().resolve() if isinstance(input_payload, Mapping) else None
                            if input_workspace != gui_workspace or resulting_job_id != gui_job_id:
                                raise ControlPlaneError(
                                    "Gate I input manifest must match the production RuntimeJobSpec workspace and job_id"
                                )
                            child_context = replace(
                                child_context,
                                job_id=resulting_job_id,
                                workspace_path=str(gui_workspace),
                                runtime_spec_path=child.runtime_spec,
                            )
                        scenario_result = scenario.execute(
                            child_context,
                            (),
                            runtime_result=None,
                        )
                        refs = [dict(item) for item in scenario_result.evidence_refs]
                        if is_production_v2 and scenario_result.status == "READY_FOR_SEMANTIC_VERIFICATION":
                            browser_ref = next(
                                (
                                    item
                                    for item in refs
                                    if str(item.get("role") or "") == "browser_evidence"
                                ),
                                None,
                            )
                            runtime_ref = next(
                                (
                                    item
                                    for item in refs
                                    if str(item.get("role") or "") == "runtime_spec"
                                ),
                                None,
                            )
                            if not browser_ref or not runtime_ref:
                                raise ControlPlaneError(
                                    "Gate I production evidence is missing the actual job/spec binding"
                                )
                            browser_payload = _json_object(Path(str(browser_ref.get("path") or "")))
                            runtime_payload = _json_object(Path(str(runtime_ref.get("path") or "")))
                            actual_job_id = str(
                                browser_payload.get("resulting_job_id") if browser_payload else ""
                            ).strip()
                            actual_workspace = str(
                                runtime_payload.get("workspace_path") if runtime_payload else ""
                            ).strip()
                            if not actual_job_id or not actual_workspace:
                                raise ControlPlaneError(
                                    "Gate I production evidence lacks actual job or workspace identity"
                                )
                            # Production Gate I creates its RuntimeJobSpec while
                            # the GUI flow is running.  The parent binding was
                            # initially seeded from the plan (which deliberately
                            # has no child spec for production-v2 input), so it
                            # must be completed from the same durable reference
                            # that the child receipt records.  Otherwise the
                            # child can be independently verified while the
                            # parent rejects its own receipt as cross-boundary.
                            actual_runtime_spec_path = Path(
                                str(runtime_ref.get("path") or "")
                            ).expanduser().resolve()
                            actual_runtime_spec_hash = self._acceptance_file_hash(
                                actual_runtime_spec_path
                            )
                            if not actual_runtime_spec_hash:
                                raise ControlPlaneError(
                                    "Gate I production evidence lacks a readable RuntimeJobSpec"
                                )
                            child_runtime_hash = actual_runtime_spec_hash
                            expected_child_bindings[gate] = {
                                **expected_child_bindings.get(gate, {}),
                                "runtime_spec_sha256": actual_runtime_spec_hash,
                                "job_id": actual_job_id,
                            }
                            child_context = replace(
                                child_context,
                                job_id=actual_job_id,
                                workspace_path=actual_workspace,
                                runtime_spec_path=str(actual_runtime_spec_path),
                            )
                        if scenario_result.status != "READY_FOR_SEMANTIC_VERIFICATION":
                            blocked_reason = scenario_result.reason
                elif child.execution_mode == "offline-k":
                    if gate != "K":
                        blocked_reason = "offline-k budget domain is reserved for Gate K"
                    else:
                        with bind_acceptance_execution_context(execution_context, budget_controller):
                            scenario_result = scenario.execute(
                                child_context,
                                (),
                                runtime_result=None,
                            )
                        refs = [dict(item) for item in scenario_result.evidence_refs]
                else:
                    blocked_reason = (
                        f"scenario executor for mode {child.execution_mode!r} requires its explicit "
                        "action adapter and will not fall back to workspace inventory"
                    )
            except (ControlPlaneError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
                blocked_reason = f"child scenario execution failed closed: {type(exc).__name__}: {exc}"

            if blocked_reason:
                receipt, receipt_ref = self._write_acceptance_receipt(
                    receipt_path,
                    parent_run_id=state.run_id,
                    plan_sha256=plan_sha,
                    child=child,
                    final_sha=current_sha,
                    runtime_spec_sha256=child_runtime_hash,
                    input_identity_sha256=input_identity,
                    workspace=child.workspace or str(child_dir),
                    job_id=child_job_id,
                    attempt_id=f"{child_job_id}:blocked",
                    status="BLOCKED",
                    exit_status=1,
                    produced_evidence_refs=(),
                    started_at=started_at,
                    completed_at=self._utc_now(),
                )
                child_results[gate] = {
                    "status": "BLOCKED",
                    "scenario_id": gate,
                    "reason": blocked_reason,
                    "receipt": receipt,
                    "receipt_path": str(receipt_path),
                    "evidence_manifest": "",
                }
            elif (
                (gate == "K" and child.execution_mode == "offline-k")
                or (gate == "I" and child.execution_mode == "playwright")
            ):
                if scenario_result is None or scenario_result.status != "READY_FOR_SEMANTIC_VERIFICATION":
                    blocked_reason = (
                        scenario_result.reason
                        if scenario_result is not None
                        else "specialized scenario executor did not produce a terminal result"
                    )
                    receipt, receipt_ref = self._write_acceptance_receipt(
                        receipt_path,
                        parent_run_id=state.run_id,
                        plan_sha256=plan_sha,
                        child=child,
                        final_sha=current_sha,
                        runtime_spec_sha256=child_runtime_hash,
                        input_identity_sha256=input_identity,
                        workspace=child_context.workspace_path or str(child_dir),
                        job_id=child_context.job_id,
                        attempt_id=f"{child_context.job_id}:blocked",
                        status="BLOCKED",
                        exit_status=1,
                        produced_evidence_refs=(),
                        started_at=started_at,
                        completed_at=self._utc_now(),
                    )
                    child_results[gate] = {
                        "status": "BLOCKED",
                        "scenario_id": gate,
                        "reason": blocked_reason,
                        "receipt": receipt,
                        "receipt_path": str(receipt_path),
                        "evidence_manifest": "",
                    }
                else:
                    if gate == "K":
                        receipt_refs = [
                            item
                            for item in refs
                            if str(item.get("role") or "") == "scenario_execution_receipt"
                        ]
                        if len(receipt_refs) != 1:
                            raise ControlPlaneError(
                                "Gate K contention evidence must contain exactly one scenario receipt"
                            )
                        receipt_path = Path(
                            str(receipt_refs[0].get("path") or "")
                        ).expanduser().resolve()
                        if not receipt_path.is_file() or receipt_path.is_symlink():
                            raise ControlPlaneError(
                                "Gate K contention scenario receipt is missing or unsafe"
                            )
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    evidence_path = self._write_acceptance_child_manifest(
                        evidence_path,
                        gate=gate,
                        refs=refs,
                        final_sha=current_sha,
                        parent_run_id=state.run_id,
                        job_id=child_context.job_id,
                    )
                    verified = GateEvidenceVerifier().verify(
                        gate,
                        json.loads(evidence_path.read_text(encoding="utf-8"))["gates"][gate],
                        expected_final_sha=current_sha,
                        expected_acceptance_run_id=state.run_id,
                        origin_dir=evidence_path.parent,
                        expected_job_id=child_context.job_id,
                    )
                    child_results[gate] = {
                        "status": (
                            "PASS_OFFLINE"
                            if gate == "K" and verified.get("status") == "PASS"
                            else str(verified.get("status") or "NOT_VERIFIED")
                        ),
                        "scenario_id": gate,
                        "verified": verified,
                        "receipt": receipt,
                        "receipt_path": str(receipt_path),
                        "evidence_manifest": str(evidence_path),
                    }
            else:
                preliminary = scenario.collect(
                    child_context,
                    refs,
                    runtime_result=runtime_result,
                    require_executor_receipt=False,
                )
                if preliminary.status != "READY_FOR_SEMANTIC_VERIFICATION":
                    receipt, receipt_ref = self._write_acceptance_receipt(
                        receipt_path,
                        parent_run_id=state.run_id,
                        plan_sha256=plan_sha,
                        child=child,
                        final_sha=current_sha,
                        runtime_spec_sha256=child_runtime_hash,
                        input_identity_sha256=input_identity,
                        workspace=child_context.workspace_path or str(child_dir),
                        job_id=child_context.job_id,
                        attempt_id=f"{child_context.job_id}:incomplete",
                        status="NOT_VERIFIED",
                        exit_status=1,
                        produced_evidence_refs=preliminary.evidence_refs,
                        started_at=started_at,
                        completed_at=self._utc_now(),
                    )
                    child_results[gate] = {
                        "status": "NOT_VERIFIED",
                        "scenario_id": gate,
                        "reason": preliminary.reason,
                        "receipt": receipt,
                        "receipt_path": str(receipt_path),
                        "evidence_manifest": "",
                    }
                else:
                    receipt, receipt_ref = self._write_acceptance_receipt(
                        receipt_path,
                        parent_run_id=state.run_id,
                        plan_sha256=plan_sha,
                        child=child,
                        final_sha=current_sha,
                        runtime_spec_sha256=child_runtime_hash,
                        input_identity_sha256=input_identity,
                        workspace=child_context.workspace_path or str(child_dir),
                        job_id=child_context.job_id,
                        attempt_id=str(runtime_result.get("attempt_id") or f"{child_context.job_id}:acceptance") if runtime_result else f"{child_context.job_id}:acceptance",
                        status="PASSED",
                        exit_status=0,
                        produced_evidence_refs=preliminary.evidence_refs,
                        started_at=started_at,
                        completed_at=self._utc_now(),
                    )
                    final_scenario = scenario.execute(
                        child_context,
                        [*preliminary.evidence_refs, receipt_ref],
                        runtime_result=runtime_result,
                    )
                    if final_scenario.status != "READY_FOR_SEMANTIC_VERIFICATION":
                        receipt, receipt_ref = self._write_acceptance_receipt(
                            receipt_path,
                            parent_run_id=state.run_id,
                            plan_sha256=plan_sha,
                            child=child,
                            final_sha=current_sha,
                            runtime_spec_sha256=child_runtime_hash,
                            input_identity_sha256=input_identity,
                            workspace=child_context.workspace_path or str(child_dir),
                            job_id=child_context.job_id,
                            attempt_id=f"{child_context.job_id}:not-verified",
                            status="NOT_VERIFIED",
                            exit_status=1,
                            produced_evidence_refs=(
                                ref
                                for ref in final_scenario.evidence_refs
                                if str(ref.get("role") or "")
                                != "scenario_execution_receipt"
                            ),
                            started_at=started_at,
                            completed_at=self._utc_now(),
                        )
                        child_results[gate] = {
                            "status": "NOT_VERIFIED",
                            "scenario_id": gate,
                            "reason": final_scenario.reason,
                            "receipt": receipt,
                            "receipt_path": str(receipt_path),
                            "evidence_manifest": "",
                        }
                    else:
                        final_refs = list(final_scenario.evidence_refs)
                        evidence_path = self._write_acceptance_child_manifest(
                            evidence_path,
                            gate=gate,
                            refs=final_refs,
                            final_sha=current_sha,
                            parent_run_id=state.run_id,
                            job_id=child_context.job_id,
                        )
                        gate_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))["gates"][gate]
                        verified = GateEvidenceVerifier().verify(
                            gate,
                            gate_evidence,
                            expected_final_sha=current_sha,
                            expected_acceptance_run_id=state.run_id,
                            origin_dir=evidence_path.parent,
                            expected_job_id=child_context.job_id,
                        )
                        child_results[gate] = {
                            "status": str(verified.get("status") or "NOT_VERIFIED"),
                            "scenario_id": gate,
                            "verified": verified,
                            "receipt": receipt,
                            "receipt_path": str(receipt_path),
                            "evidence_manifest": str(evidence_path),
                        }
            atomic_write_json(
                str(child_state_path),
                {
                    "schema_version": "release-acceptance-child-state-v2",
                    "parent_acceptance_run_id": state.run_id,
                    "plan_sha256": plan_sha,
                    "final_executable_sha": current_sha,
                    "scenario_id": gate,
                    "gate": gate,
                    "status": child_results[gate]["status"],
                    "job_id": str(
                        child_results[gate].get("receipt", {}).get("job_id")
                        or child_job_id
                    ),
                    "runtime_spec_sha256": str(
                        child_results[gate].get("receipt", {}).get("runtime_spec_sha256")
                        or child_runtime_hash
                    ),
                    "receipt_path": child_results[gate]["receipt_path"],
                    "evidence_manifest": child_results[gate].get("evidence_manifest", ""),
                    "updated_at": self._utc_now(),
                },
            )
            child_states[gate] = {
                "gate": gate,
                "scenario_id": gate,
                "status": child_results[gate]["status"],
                "receipt_path": child_results[gate]["receipt_path"],
                "evidence_manifest": child_results[gate].get("evidence_manifest", ""),
                    "job_id": str(child_results[gate].get("receipt", {}).get("job_id") or child_job_id),
                    "runtime_spec_sha256": str(
                        child_results[gate].get("receipt", {}).get("runtime_spec_sha256")
                        or child_runtime_hash
                    ),
                    "state_path": str(child_state_path),
            }
            state = replace(
                state,
                child_states=child_states,
                gates={
                    **dict(state.gates),
                    gate: {
                        **dict(state.gates.get(gate, {})),
                        **child_results[gate],
                    },
                },
                updated_at=self._utc_now(),
            )
            with interprocess_file_lock(state_path):
                atomic_write_json(str(state_path), state.to_dict())

        parent_result = ParentAcceptanceResultV2.from_child_results(
            parent_acceptance_run_id=state.run_id,
            final_executable_sha=current_sha,
            child_results=child_results,
            required_scenarios=plan.gates,
            expected_child_bindings=expected_child_bindings,
        )
        parent_result_path = run_dir / "parent_acceptance_result_v2.json"
        if parent_result_path.is_file():
            try:
                old = json.loads(parent_result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                old = None
            if isinstance(old, Mapping) and (
                str(old.get("parent_acceptance_run_id") or "") != state.run_id
                or str(old.get("final_executable_sha") or "") != current_sha
            ):
                parent_result_path = run_dir / "parent_acceptance_result_v2.json"
        atomic_write_json(str(parent_result_path), parent_result.to_dict())
        final_status = "complete" if parent_result.status == "READY_TO_MERGE" else "blocked"
        state = replace(
            state,
            status=final_status,
            gates={
                gate: {
                    **dict(state.gates.get(gate, {})),
                    **child_results.get(gate, {}),
                }
                for gate in plan.gates
            },
            parent_result=parent_result.to_dict(),
            updated_at=self._utc_now(),
        )
        with interprocess_file_lock(state_path):
            atomic_write_json(str(state_path), state.to_dict())
        return {
            "control_plane_version": CONTROL_PLANE_VERSION,
            "status": final_status,
            "ok": final_status == "complete",
            "run_id": state.run_id,
            "parent_acceptance_run_id": state.run_id,
            "final_sha": current_sha,
            "plan_sha256": plan_sha,
            "acceptance_spec_path": str(spec_path),
            "state_path": str(state_path),
            "evidence_manifest": str(parent_result_path),
            "provider_budget_state_path": state.provider_budget_state_path,
            "acceptance_execution_context": execution_context.to_dict(),
            "parent_result": parent_result.to_dict(),
            "scenarios": child_results,
            "gates": child_results,
            "read_only": False,
        }

    @staticmethod
    def _write_acceptance_child_manifest(
        path: Path,
        *,
        gate: str,
        refs: Iterable[Mapping[str, Any]],
        final_sha: str,
        parent_run_id: str,
        job_id: str,
    ) -> Path:
        from runtime.release_acceptance import GateEvidenceProducer

        producer = GateEvidenceProducer(final_sha=final_sha)
        return producer.write_manifest(
            path,
            {gate: tuple(refs)},
            acceptance_run_id=parent_run_id,
            scenario_id=gate,
            job_id=job_id,
        )

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
        gate: str = "",
        f1_source_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        from runtime.release_acceptance import GateEvidenceProducer

        if spec.source.mode != "direct":
            return []
        folder = Path(spec.source.pdf_folder).expanduser().resolve()
        if not folder.is_dir():
            return []
        selected_hashes = ReviewControlPlane._acceptance_f1_source_hashes(
            spec,
            gate=gate,
            source_ids=f1_source_ids,
        )
        producer = GateEvidenceProducer(final_sha=final_sha)
        refs: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.pdf")):
            if not path.is_file() or path.is_symlink():
                continue
            if selected_hashes is not None:
                try:
                    if file_sha256(str(path)) not in selected_hashes:
                        continue
                except OSError:
                    continue
            try:
                source_ref = producer.reference(
                    path,
                    role="source_pdf",
                    artifact_type="source_pdf",
                    job_id=job_id,
                )
                refs.append(source_ref)
            except (OSError, ValueError):
                continue
        return refs

    @staticmethod
    def _acceptance_f1_source_hashes(
        spec: RuntimeJobSpec,
        *,
        gate: str,
        source_ids: Iterable[str] | None,
    ) -> set[str] | None:
        """Return the exact manifest hashes selected by an F1 child.

        A direct source folder is intentionally allowed to contain the full
        fifteen-paper corpus so the same immutable staging root can serve C,
        D, and Q. Acceptance evidence must nevertheless expose only the
        child selection; otherwise the verifier mistakes valid extras for a
        source-binding failure.
        """

        raw_binding = spec.metadata.get("f1_corpus_binding")
        if not isinstance(raw_binding, Mapping):
            return None
        manifest_value = str(raw_binding.get("manifest_path") or "").strip()
        raw_bound_ids = raw_binding.get("source_ids")
        if not manifest_value or not isinstance(raw_bound_ids, (list, tuple)):
            raise ControlPlaneError("F1 source binding is incomplete")
        bound_ids = tuple(str(item).strip() for item in raw_bound_ids)
        # Non-F1 gate children (for example the Outline-only Gate F retry)
        # may intentionally omit a separate child selection.  In that case
        # the RuntimeJobSpec's exact manifest binding is the authority.  C/D/Q
        # still receive explicit selections from ReleaseAcceptanceSpec.
        provided_ids = (
            tuple(str(item).strip() for item in source_ids)
            if source_ids is not None
            else ()
        )
        selected_ids = provided_ids or bound_ids
        if (
            not selected_ids
            or any(not item for item in selected_ids)
            or tuple(item.casefold() for item in selected_ids)
            != tuple(item.casefold() for item in bound_ids)
        ):
            raise ControlPlaneError(
                "F1 acceptance source selection does not match the RuntimeJobSpec binding"
            )
        normalized_gate = str(gate or "").strip().upper()
        if not normalized_gate:
            normalized_gate = {1: "C", 3: "D", 15: "Q"}.get(len(selected_ids), "")
        if not normalized_gate:
            raise ControlPlaneError("F1 acceptance source selection has no gate cardinality")
        manifest_path = Path(manifest_value).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = Path(spec.config).expanduser().resolve().parent / manifest_path
        try:
            from runtime.f1_corpus import F1CorpusManifestV1

            manifest = F1CorpusManifestV1.from_file(
                manifest_path,
                verify_source_files=True,
            )
            if normalized_gate in {"C", "D", "Q"}:
                selected = manifest.validate_selection(
                    selected_ids,
                    gate=normalized_gate,
                )
            else:
                # Gate F is an Outline-only runtime child, not one of the
                # C/D/Q corpus gates. Its empty child selection is already
                # resolved to the exact RuntimeJobSpec binding above; verify
                # that full set directly without inventing a new gate count.
                if {
                    item.casefold() for item in selected_ids
                } != {item.source_id.casefold() for item in manifest.sources}:
                    raise ControlPlaneError(
                        "non-C/D/Q F1 child must bind the complete manifest set"
                    )
                selected = tuple(manifest.source_by_id(item) for item in selected_ids)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ControlPlaneError(
                f"F1 acceptance source manifest could not be verified: {type(exc).__name__}"
            ) from exc
        return {source.sha256 for source in selected}

    @staticmethod
    def _acceptance_production_modality_references(
        spec: RuntimeJobSpec,
        *,
        workspace: str | Path,
        final_sha: str,
        job_id: str,
        profile_root: str | Path,
        gate: str = "",
        f1_source_ids: Iterable[str] | None = None,
        runtime_spec_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Derive Gate D profiles from published preprocess/Stage 1 artifacts.

        The source PDF is used only to bind the identity.  All modality counts
        and lineage fields come from the job's paper artifact and its
        production preprocess metadata; the old source-only PyMuPDF heuristic
        is intentionally not admitted here.
        """

        from runtime.release_acceptance import GateEvidenceProducer

        if spec.source.mode != "direct":
            raise ControlPlaneError("production modality profiles require direct PDF sources")
        source_dir = Path(spec.source.pdf_folder).expanduser().resolve()
        if not source_dir.is_dir():
            raise ControlPlaneError("production modality profile source directory is missing")
        selected_hashes = ReviewControlPlane._acceptance_f1_source_hashes(
            spec,
            gate=gate,
            source_ids=f1_source_ids,
        )
        workspace_path = Path(workspace).expanduser().resolve()
        registry_path = workspace_path / "artifact_registry.json"
        if not registry_path.is_file() or registry_path.is_symlink():
            raise ControlPlaneError("production modality profile requires the child Registry")
        registry = ArtifactRegistry(registry_path, job_id)
        paper_records = [
            record
            for record in registry.list_records()
            if record.status == "ready" and record.artifact_type == "paper_artifact"
        ]
        producer = GateEvidenceProducer(final_sha=final_sha)
        refs: list[dict[str, Any]] = []
        for source_pdf in sorted(source_dir.glob("*.pdf")):
            if not source_pdf.is_file() or source_pdf.is_symlink():
                continue
            source_hash = file_sha256(str(source_pdf))
            if selected_hashes is not None and source_hash not in selected_hashes:
                continue
            candidate: ArtifactRecord | None = None
            candidate_payload: Mapping[str, Any] | None = None
            for record in paper_records:
                try:
                    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                paper_info = payload.get("paper_info")
                paper_info = paper_info if isinstance(paper_info, Mapping) else {}
                source_payload = payload.get("source")
                source_payload = source_payload if isinstance(source_payload, Mapping) else {}
                candidate_source_hash = str(
                    paper_info.get("source_pdf_sha256")
                    or payload.get("source_pdf_sha256")
                    or source_payload.get("source_pdf_sha256")
                    or ""
                ).strip()
                if candidate_source_hash == source_hash:
                    candidate = record
                    candidate_payload = payload
                    break
            if candidate is None or candidate_payload is None:
                raise ControlPlaneError(
                    f"production modality profile has no paper artifact for {source_pdf.name}"
                )
            analysis = candidate_payload.get("analysis")
            analysis = analysis if isinstance(analysis, Mapping) else {}
            preprocess = analysis.get("preprocess")
            preprocess = preprocess if isinstance(preprocess, Mapping) else {}
            stage1_inputs = candidate_payload.get("stage1_inputs")
            stage1_inputs = stage1_inputs if isinstance(stage1_inputs, Mapping) else {}
            diagnostics = preprocess.get("page_diagnostics")
            if not isinstance(diagnostics, list) or not diagnostics:
                raise ControlPlaneError(
                    f"production modality profile lacks page diagnostics for {source_pdf.name}"
                )
            manifest_path = Path(str(preprocess.get("manifest_path") or "")).expanduser().resolve()
            stage1_manifest_path = Path(
                str(
                    preprocess.get("stage1_input_manifest_path")
                    or stage1_inputs.get("stage1_input_manifest_path")
                    or ""
                )
            ).expanduser().resolve()
            if (
                not manifest_path.is_file()
                or manifest_path.is_symlink()
                or not stage1_manifest_path.is_file()
                or stage1_manifest_path.is_symlink()
            ):
                raise ControlPlaneError(
                    f"production modality profile lineage files are missing for {source_pdf.name}"
                )
            page_count = len(diagnostics)
            text_page_count = sum(
                1
                for item in diagnostics
                if isinstance(item, Mapping) and int(item.get("text_length") or 0) >= 80
            )
            image_page_count = sum(
                1
                for item in diagnostics
                if isinstance(item, Mapping) and int(item.get("image_count") or 0) > 0
            )
            scanned_pages = sum(
                1
                for item in diagnostics
                if isinstance(item, Mapping) and item.get("scanned_candidate") is True
            )
            ocr_pages = sum(
                1
                for item in diagnostics
                if isinstance(item, Mapping) and item.get("used_ocr") is True
            )
            selected_visual_refs = stage1_inputs.get("selected_visual_refs")
            selected_visual_count = (
                len(selected_visual_refs) if isinstance(selected_visual_refs, list) else 0
            )
            payload = {
                "artifact_type": "document_modality_profile",
                "artifact_version": "v2",
                "schema_version": "document-modality-profile-v2",
                "source_pdf_sha256": source_hash,
                "preprocess_manifest_hash": file_sha256(str(manifest_path)),
                "stage1_input_manifest_hash": file_sha256(str(stage1_manifest_path)),
                "actual_extractor": str(preprocess.get("extractor_used") or "").strip(),
                "page_count": page_count,
                "text_page_count": text_page_count,
                "image_page_count": image_page_count,
                "table_count": int(preprocess.get("table_count") or 0),
                "figure_count": int(preprocess.get("figure_count") or 0),
                "scanned_candidate_pages": scanned_pages,
                "actual_ocr_pages": ocr_pages,
                "actual_selected_visual_count": selected_visual_count,
                "stage1_input_mode": str(
                    stage1_inputs.get("input_mode")
                    or preprocess.get("selected_text_source")
                    or ""
                ).strip(),
                "source_paper_artifact_id": candidate.artifact_id,
            }
            if not payload["actual_extractor"] or not payload["stage1_input_mode"]:
                raise ControlPlaneError(
                    f"production modality profile lacks extractor or Stage 1 input mode for {source_pdf.name}"
                )
            profile_path = (
                Path(profile_root).expanduser().resolve()
                / "modality_profiles"
                / f"{source_hash}.v2.json"
            )
            atomic_write_json(str(profile_path), payload)
            try:
                registry.register_file(
                    artifact_role="document_modality_profile",
                    artifact_type="document_modality_profile",
                    artifact_version="v2",
                    path=profile_path,
                    producer="runtime.control_plane.ReviewControlPlane",
                    artifact_id=f"document-modality-profile:v2:{source_hash}",
                    depends_on=[ArtifactDependencyRefV2.from_record(candidate)],
                    metadata={
                        "source_pdf_sha256": source_hash,
                        "production_lineage": True,
                    },
                )
            except (OSError, RegistryError, TypeError, ValueError) as exc:
                raise ControlPlaneError(
                    f"production modality profile Registry publication failed: {source_pdf.name}"
                ) from exc
            refs.append(
                producer.reference(
                    profile_path,
                    role="modality_profile",
                    artifact_type="document_modality_profile",
                    artifact_version="v2",
                    schema_version="document-modality-profile-v2",
                    job_id=job_id,
                    artifact_id=f"document-modality-profile:v2:{source_hash}",
                )
            )
        d_policy = spec.metadata.get("f1_d_modality_policy")
        if (
            gate == "D"
            and isinstance(d_policy, Mapping)
            and str(d_policy.get("policy_id") or "").strip()
            == "f1-two-in-corpus-plus-auxiliary-ocr-v1"
        ):
            fixture_path = Path(
                str(d_policy.get("auxiliary_fixture_path") or "")
            ).expanduser()
            if not fixture_path.is_absolute() and runtime_spec_path:
                fixture_path = (
                    Path(runtime_spec_path).expanduser().resolve().parent / fixture_path
                )
            fixture_path = fixture_path.resolve()
            if not fixture_path.is_file() or is_reparse_path(fixture_path):
                raise ControlPlaneError(
                    "modified D policy auxiliary OCR fixture is missing or unsafe"
                )
            expected_hash = str(d_policy.get("auxiliary_fixture_sha256") or "").strip().lower()
            actual_hash = file_sha256(str(fixture_path))
            if expected_hash != actual_hash:
                raise ControlPlaneError(
                    "modified D policy auxiliary OCR fixture hash does not match"
                )
            refs.append(
                producer.reference(
                    fixture_path,
                    role="auxiliary_ocr_fixture",
                    artifact_type="auxiliary_ocr_fixture",
                    artifact_version="v1",
                    schema_version="auxiliary-ocr-fixture-v1",
                    job_id=job_id,
                )
            )
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
        # Validate the persisted runtime identity before mutating any resume
        # state. A malformed/stale spec or fingerprint must leave the DAG,
        # cancellation request, and pause marker untouched.
        try:
            from dataclasses import replace

            resume_job_id = Path(resolved).name.rsplit("__", 1)[-1]
            persisted_spec = _load_spec_path(spec_path)
            if resume_job_id:
                persisted_spec = replace(persisted_spec, job_id=resume_job_id)
            normalized_resume_spec = AgentRuntimeRunner(persisted_spec)._normalized_spec(
                resume=True
            )
            AgentRuntimeRunner._validate_persisted_spec(
                workspace_obj,
                normalized_resume_spec.to_dict(),
                registry,
            )
        except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
            raise ControlPlaneError(f"resume identity preflight is invalid: {exc}") from exc
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
        try:
            pause_store = PauseStateStore(workspace_obj, registry)
            pause_state = pause_store.read()
            if pause_state is not None and pause_state.paused:
                pause_store.clear(cleared_by="reviewctl", reason="explicit_resume")
        except (OSError, RegistryError, ValueError, TypeError, RuntimeError) as exc:
            raise ControlPlaneError(f"resume pause state is invalid: {exc}") from exc
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

    def source_correction_plan(
        self,
        *,
        workspace: str | Path,
        proposal_path: str | Path,
        output_root: str | Path,
    ) -> dict[str, Any]:
        """Prepare an immutable source correction for review in a separate job."""
        from services.queue_service import LocalPublicationContext
        from services.summary_correction import (
            SourceSummaryCorrectionError,
            prepare_source_summary_correction_candidate,
        )

        source_workspace, source_registry = AgentRuntimeRunner._open_workspace(workspace)
        proposal_file = Path(proposal_path).expanduser().resolve()
        if not proposal_file.is_file() or proposal_file.stat().st_size > 2 * 1024 * 1024:
            raise ControlPlaneError("source correction proposal is missing or exceeds 2 MiB")
        try:
            proposal = json.loads(proposal_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("source correction proposal is unreadable or invalid JSON") from exc
        if not isinstance(proposal, Mapping):
            raise ControlPlaneError("source correction proposal must be a JSON object")
        authority = proposal.get("source_authority")
        if not isinstance(authority, Mapping) or not str(authority.get("artifact_id") or ""):
            raise ControlPlaneError("source correction proposal lacks a source artifact binding")
        destination_root = Path(output_root).expanduser().resolve()
        original_root = Path(source_workspace.root_dir).resolve()
        if destination_root == original_root or original_root in destination_root.parents:
            raise ControlPlaneError("source correction output must be outside the original workspace")
        destination = JobWorkspace.create(
            str(destination_root), "source_correction", job_id="correction_" + uuid.uuid4().hex
        )
        publication = LocalPublicationContext()
        destination_registry = publication.registry(destination.paths.registry_path, destination.job_id)
        try:
            result = prepare_source_summary_correction_candidate(
                proposal_payload=proposal,
                source_registry=source_registry,
                source_artifact_id=str(authority["artifact_id"]),
                destination_workspace=destination,
                destination_registry=destination_registry,
                publication_context=publication,
            )
        except (SourceSummaryCorrectionError, RegistryError, OSError, ValueError, TypeError) as exc:
            return {
                "status": "blocked",
                "source_workspace": str(original_root),
                "destination_workspace": destination.root_dir,
                "reason": str(exc),
                "provider_posts": 0,
                "canonical_pointer_advanced": False,
                "usable_as_stage1_reuse": False,
            }
        return {
            **result.to_dict(),
            "source_workspace": str(original_root),
            "destination_workspace": destination.root_dir,
            "provider_posts": 0,
        }

    def source_correction_inspect(
        self,
        *,
        source_workspace: str | Path,
        workspace: str | Path,
        candidate_artifact_id: str,
    ) -> dict[str, Any]:
        """Recheck the proposed bytes and expose their exact review identity."""
        from services.summary_correction import (
            SourceSummaryCorrectionError,
            verify_source_summary_correction_candidate,
        )

        original, source_registry = AgentRuntimeRunner._open_workspace(source_workspace)
        destination, destination_registry = AgentRuntimeRunner._open_workspace(workspace)
        registry_hashes_before = (
            file_sha256(source_registry.registry_path),
            file_sha256(destination_registry.registry_path),
        )
        try:
            verification = verify_source_summary_correction_candidate(
                source_registry=source_registry,
                destination_registry=destination_registry,
                candidate_artifact_id=candidate_artifact_id,
            )
            candidate = destination_registry.get(candidate_artifact_id)
            if candidate is None:
                raise SourceSummaryCorrectionError("verified candidate disappeared")
            candidate_bytes = Path(candidate.path).read_bytes()
            if hashlib.sha256(candidate_bytes).hexdigest() != candidate.content_hash:
                raise SourceSummaryCorrectionError("candidate changed after verification")
            payload = json.loads(candidate_bytes)
            registry_hashes_after = (
                file_sha256(source_registry.registry_path),
                file_sha256(destination_registry.registry_path),
            )
            if registry_hashes_after != registry_hashes_before:
                raise SourceSummaryCorrectionError("Registry changed during candidate inspection")
        except (SourceSummaryCorrectionError, RegistryError, OSError, ValueError, TypeError) as exc:
            return {
                "status": "blocked", "reason": str(exc),
                "provider_posts": 0, "read_only": True,
                "usable_as_stage1_reuse": False,
                "canonical_pointer_advanced": False,
            }
        return {
            **verification.to_dict(),
            "status": "ready_for_owner_review",
            "source_workspace": original.root_dir,
            "destination_workspace": destination.root_dir,
            "candidate_artifact_hash": candidate.content_hash,
            "normalized_proposal_hash": payload["normalized_proposal_hash"],
            "source_authority": payload["source_authority"],
            "field_changes": payload["field_changes"],
            "source_conflicts_preserved": payload["source_conflicts_preserved"],
            "summary_count": payload["summary_count_after"],
            "registries_unchanged": True,
            "provider_posts": 0,
            "read_only": True,
        }

    def source_correction_adopt(
        self,
        *,
        source_workspace: str | Path,
        workspace: str | Path,
        candidate_artifact_id: str,
        expected_candidate_hash: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record an explicit operator adoption of the exact reviewed bytes."""
        from services.queue_service import LocalPublicationContext
        from services.summary_correction_adoption import adopt_source_summary_correction_candidate

        original, source_registry = AgentRuntimeRunner._open_workspace(source_workspace)
        destination, destination_registry = AgentRuntimeRunner._open_workspace(workspace)
        try:
            result = adopt_source_summary_correction_candidate(
                source_registry=source_registry,
                destination_registry=destination_registry,
                workspace=destination,
                publication_context=LocalPublicationContext(),
                candidate_artifact_id=candidate_artifact_id,
                expected_candidate_hash=expected_candidate_hash,
                actor=actor,
                reason=reason,
            )
        except (RegistryError, OSError, ValueError, TypeError) as exc:
            return {
                "status": "blocked", "reason": str(exc),
                "source_workspace": original.root_dir,
                "destination_workspace": destination.root_dir,
                "provider_posts": 0,
                "usable_as_stage1_reuse": False,
                "canonical_pointer_advanced": False,
            }
        return {
            **result.to_dict(),
            "source_workspace": original.root_dir,
            "destination_workspace": destination.root_dir,
            "provider_posts": 0,
        }

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
        manual_proposal: Mapping[str, Any] | None = None,
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        if manual_proposal is not None and (
            not isinstance(manual_proposal, Mapping)
            or not str(actor or "").strip()
            or not str(reason or "").strip()
        ):
            return {
                "status": "blocked",
                "plan_id": plan_id,
                "reason": "manual repair requires a typed proposal, actor, and reason",
                "mutation_performed": False,
            }
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
            service = RepairTransactionService(workspace_obj, registry)
            if manual_proposal is not None:
                return service.apply_manual_proposal(
                    plan_id,
                    manual_proposal,
                    actor=str(actor).strip(),
                    reason=str(reason).strip(),
                )
            return service.apply_plan(plan_id)
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
        validator_host_acknowledgement: Mapping[str, Any] | None = None,
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
            admitted_validator_route, admitted_validator_ack = self._admit_direct_validator_execution(
                spec,
                validator_host_acknowledgement=validator_host_acknowledgement,
                operation="external repair revalidation",
            )
            bridge = AgentRuntimeBridge(
                spec,
                direct_validation_route_fingerprint=admitted_validator_route,
                direct_validation_host_acknowledgement=admitted_validator_ack,
            )
            session = bridge.bootstrap(
                resume_requested=True,
                claim_latest_pointer=False,
                publish_running_state=False,
            )
            bridge.verify_direct_validation_route(session.stage_host.config)
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

    def validate(
        self,
        *,
        job_id: str | None = None,
        workspace: str | Path | None = None,
        validator_host_acknowledgement: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            admitted_validator_route, admitted_validator_ack = self._admit_direct_validator_execution(
                spec,
                validator_host_acknowledgement=validator_host_acknowledgement,
                operation="external validation",
            )
            bridge = AgentRuntimeBridge(
                spec,
                direct_validation_route_fingerprint=admitted_validator_route,
                direct_validation_host_acknowledgement=admitted_validator_ack,
            )
            attempt_id = f"reviewctl-validation:{spec.job_id}:{time.time_ns()}"
            session = bridge.bootstrap(
                resume_requested=True,
                claim_latest_pointer=False,
                publish_running_state=False,
            )
            bridge.verify_direct_validation_route(session.stage_host.config)
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

    def pause(
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
            state = PauseStateStore(workspace_obj, registry).request(
                requested_by=requested_by,
                reason=reason,
            )
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            raise ControlPlaneError(f"cannot persist pause state: {exc}") from exc
        return {
            "status": "paused",
            "job_id": inspection["job_id"],
            "pause_state": state.to_dict(),
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
            mineru = PreprocessManager(
                normalized_config,
                preprocess_environment_resolved=bool(
                    getattr(normalized_config, "preprocess_environment_resolved", False)
                ),
            )
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

        if normalized_config:
            mineru_admission = self._mineru_remote_admission(normalized_config)
            add(
                "mineru_remote_admission",
                str(mineru_admission["status"]),
                mineru_admission,
            )
        else:
            add(
                "mineru_remote_admission",
                "skipped",
                {"reason": "configuration is unavailable", "network_probe": False},
            )

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
        dependency_check = self._dependency_check()
        add(
            "dependencies",
            "fail" if dependency_check.get("missing") else "pass",
            dependency_check,
        )
        add("tokenizer", "pass" if any(importlib.util.find_spec(name) for name in _OPTIONAL_TOKENIZER_MODULES) else "warn", {"available": [name for name in _OPTIONAL_TOKENIZER_MODULES if importlib.util.find_spec(name)]})
        certificate_check = self._certificate_check()
        add(
            "certificate_paths",
            "fail" if not certificate_check.get("valid", True) else "pass",
            certificate_check,
        )
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

    @staticmethod
    def _mineru_remote_admission(
        config: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Describe zero-network MinerU readiness for the selected parser path."""

        manager = PreprocessManager(
            config,
            preprocess_environment_resolved=bool(
                getattr(config, "preprocess_environment_resolved", False)
            ),
        )
        remote_requested = mineru_remote_requested(
            manager.parser_mode,
            manager.primary_parser,
        )
        details: dict[str, Any] = {
            "remote_requested": remote_requested,
            "token_present": bool(manager.mineru_api_token),
            "fallback_will_be_used": False,
            "parser_mode": manager.parser_mode,
            "primary_parser": manager.primary_parser,
            "fallback_parser": manager.fallback_parser,
            "network_probe": False,
        }
        if not remote_requested:
            details["status"] = "pass"
            details["reason"] = "remote_parser_not_requested"
            return details
        try:
            manager.preflight_mineru()
        except Exception as exc:
            fallback = bool(manager.allow_local_parse_fallback)
            details.update(
                {
                    "status": "warn" if fallback else "fail",
                    "fallback_will_be_used": fallback,
                    "reason": "remote_parser_admission_failed",
                    "error_type": type(exc).__name__,
                }
            )
            return details
        details["status"] = "pass"
        details["reason"] = "remote_parser_admitted"
        return details

    @staticmethod
    def _unresolved_mineru_remote_admission(
        config_path: str | Path,
    ) -> dict[str, Any] | None:
        """Project parser admission even when full config validation fails."""

        target = Path(config_path).expanduser()
        parser = configparser.ConfigParser()
        try:
            with target.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeError, configparser.Error):
            return None
        section = parser["Preprocess"] if parser.has_section("Preprocess") else {}
        dotenv: dict[str, str] = {}
        dotenv_path = target.with_name(".env")
        try:
            for line in dotenv_path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                name, value = raw.split("=", 1)
                dotenv[name.strip()] = value.strip().strip('"').strip("'")
        except (OSError, UnicodeError):
            pass

        def setting(config_key: str, env_name: str, default: str) -> str:
            return str(
                os.getenv(env_name)
                or dotenv.get(env_name)
                or section.get(config_key)
                or default
            ).strip()

        parser_mode = setting("parser_mode", "MINERU_PARSER_MODE", "local").casefold()
        primary_parser = setting("primary_parser", "MINERU_PRIMARY_PARSER", "local").casefold()
        fallback_parser = setting("fallback_parser", "MINERU_FALLBACK_PARSER", "local").casefold()
        remote_requested = parser_mode in {"remote", "remote_first"} or (
            parser_mode == "hybrid" and primary_parser == "mineru_remote"
        )
        if not remote_requested:
            return {
                "status": "pass",
                "remote_requested": False,
                "token_present": bool(setting("mineru_api_token", "MINERU_API_TOKEN", "")),
                "fallback_will_be_used": False,
                "parser_mode": parser_mode,
                "primary_parser": primary_parser,
                "fallback_parser": fallback_parser,
                "network_probe": False,
                "reason": "remote_parser_not_requested",
            }
        allow_local = setting(
            "allow_local_parse_fallback",
            "ALLOW_LOCAL_PARSE_FALLBACK",
            "true",
        ).casefold() not in {"0", "false", "no", "off"}
        return {
            "status": "warn" if allow_local else "fail",
            "remote_requested": True,
            "token_present": bool(setting("mineru_api_token", "MINERU_API_TOKEN", "")),
            "fallback_will_be_used": bool(allow_local and fallback_parser == "local"),
            "parser_mode": parser_mode,
            "primary_parser": primary_parser,
            "fallback_parser": fallback_parser,
            "network_probe": False,
            "reason": "remote_parser_admission_failed",
            "error_type": "ValueError",
        }

    def provider_preflight(
        self,
        *,
        config_path: str | Path | None = None,
        action: str = "analyze",
        requested_stages: Sequence[str] | None = None,
        outline_pilot: Mapping[str, Any] | None = None,
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
            external_host_policy = build_runtime_external_host_policy(
                normalized,
                route_plan,
                requested_stages=requested_stages,
                outline_pilot=outline_pilot,
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
            mineru_admission = self._mineru_remote_admission(normalized)
            admission_status = str(mineru_admission["status"])
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": admission_status,
                "ok": admission_status != "fail",
                "action": action,
                "requested_stages": list(requested_stages or ()),
                "free_mode_enabled": bool(free_mode_enabled),
                "provider_roles": list(selected_roles),
                "route_plan": route_plan.to_dict(),
                "external_host_policy": external_host_policy.to_dict(),
                "providers": providers,
                "credential_provenance": provenance_payload(
                    list(getattr(normalized, "credential_provenance", ()))
                ),
                "mineru_remote_admission": mineru_admission,
                "proxy_policy": "formal ai_interface._post_with_proxy_mode",
                "network_calls": 0,
                "read_only": True,
            }
        except Exception as exc:
            unresolved_mineru = self._unresolved_mineru_remote_admission(target_config)
            return {
                "control_plane_version": CONTROL_PLANE_VERSION,
                "status": "fail",
                "ok": False,
                "config_path": str(target_config),
                "error_type": type(exc).__name__,
                "error": str(exc),
                **(
                    {"mineru_remote_admission": unresolved_mineru}
                    if unresolved_mineru is not None
                    else {}
                ),
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
            external_host_policy = build_external_host_policy(
                normalized,
                route_plan,
                provider_sections=selected_roles,
                include_mineru=False,
            )
            try:
                external_host_admission = validate_external_host_acknowledgement(
                    external_host_policy,
                    acknowledgement_from_values(
                        external_host_policy,
                        acknowledged=third_party_acknowledged,
                        hosts=tuple(third_party_hosts or ()),
                    ),
                )
            except ExternalHostAdmissionError as exc:
                return {
                    "control_plane_version": CONTROL_PLANE_VERSION,
                    "status": "BLOCKED_TRUST_POLICY",
                    "ok": False,
                    "provider_roles": list(selected_roles),
                    "route_plan": route_plan.to_dict(),
                    "external_host_policy": external_host_policy.to_dict(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "network_calls": 0,
                    "read_only": False,
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
                    str(budget_state_path),
                    acceptance_run_id=(
                        active_context.acceptance_run_id if active_context is not None else ""
                    ),
                    state_started=(
                        active_context.provider_budget_state_started
                        if active_context is not None else None
                    ),
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
                "external_host_admission": external_host_admission,
                "external_host_policy": external_host_policy.to_dict(),
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
        invalid = [
            item["variable"]
            for item in paths
            if item.get("configured") is True and item.get("exists") is not True
        ]
        return {"paths": paths, "valid": not invalid, "invalid_configured": invalid}

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

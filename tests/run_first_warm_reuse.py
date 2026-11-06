"""Test-only construction of a new Stage 1 run from typed reuse authority."""

from __future__ import annotations

import configparser
import copy
import json
import time
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    verify_stage1_typed_manifest_authority,
)
from validation.closure import resolve_current_stage_closure_map


@dataclass(frozen=True)
class WarmReuseExecution:
    """The new run and verified input authorities used to start it."""

    spec: RuntimeJobSpec
    result: Any
    workspace: JobWorkspace
    registry: ArtifactRegistry
    reuse_manifest_paths: tuple[str, ...]
    paper_pdf_sha256: Mapping[str, str]
    plan: Mapping[str, Any]
    plan_seconds: float


def _read_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be a JSON object")
    return payload


def _require_loopback_config(
    config_path: Path,
    *,
    action: Literal["analyze", "run_all"],
) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ValueError("shared fixture config is unreadable") from exc
    if not loaded:
        raise ValueError("shared fixture config is missing")

    required_sections = {"Primary_Reader_API", "Backup_Reader_API"}
    if action == "run_all":
        required_sections.update(
            {"Outline_API", "Writer_API", "Validator_API", "Free_Mode_API"}
        )
    for section_name in required_sections:
        if section_name not in parser:
            raise ValueError(f"shared fixture config is missing {section_name}")
        address = str(parser[section_name].get("api_base") or "").strip()
        parsed = urlsplit(address)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                f"{section_name} must target the active local HTTP fixture"
            )
    return parser


def _record(registry: ArtifactRegistry, artifact_id: str, *, label: str) -> ArtifactRecord:
    record = registry.get(artifact_id)
    if record is None or record.status != "ready":
        raise ValueError(f"{label} is not a ready Registry artifact")
    ArtifactRegistry._verify_ready_artifact(record)
    return record


def _completed_stage1_registry(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> None:
    terminals = [
        record
        for record in registry.list_records()
        if record.artifact_type == "runtime_stage_terminal"
        and record.status == "ready"
    ]
    analyze = []
    for record in terminals:
        payload = _read_json_object(record.path, label="Stage 1 terminal")
        if str(payload.get("stage_name") or "") == "analyze":
            analyze.append(payload)
    if not analyze or str(analyze[-1].get("status") or "") != "succeeded":
        raise ValueError("seed workspace has no successful Stage 1 terminal")

    resolver = AgentRuntimeRunner._external_registry_resolver(workspace)
    closure_map = resolve_current_stage_closure_map(
        registry,
        external_registry_resolver=resolver,
    )
    analyze_closure = closure_map.provider_closures_by_stage.get("analyze")
    if not isinstance(analyze_closure, Mapping) or analyze_closure.get("complete") is not True:
        raise ValueError("seed Stage 1 provider closure is incomplete")


def _verified_reuse_inputs(
    registry: ArtifactRegistry,
    *,
    pdf_folder: Path,
) -> tuple[tuple[str, ...], dict[str, str]]:
    scope_record = _record(registry, "stage1_source_scope", label="Stage 1 source scope")
    scope = _read_json_object(scope_record.path, label="Stage 1 source scope")
    source_rows = scope.get("papers")
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("seed Stage 1 source scope has no paper inventory")

    paper_pdf_sha256: dict[str, str] = {}
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise TypeError("seed Stage 1 source scope contains a malformed paper row")
        paper_key = str(row.get("paper_key") or "").strip()
        source_pdf = Path(str(row.get("source_pdf") or "")).expanduser().resolve()
        expected_sha = str(row.get("source_pdf_sha256") or "").strip().lower()
        if not paper_key or paper_key in paper_pdf_sha256:
            raise ValueError("seed Stage 1 source scope has a missing or duplicate paper key")
        if not source_pdf.is_file() or not source_pdf.is_relative_to(pdf_folder):
            raise ValueError("seed Stage 1 PDF is missing or outside the shared source folder")
        actual_sha = file_sha256(source_pdf)
        if len(expected_sha) != 64:
            raise ValueError("seed Stage 1 source scope has an invalid PDF SHA-256")
        if actual_sha != expected_sha:
            raise ValueError("seed Stage 1 PDF bytes changed since source intake")
        paper_pdf_sha256[paper_key] = actual_sha

    manifest_records = [
        record
        for record in registry.list_records()
        if record.artifact_type == "stage1_reusable_summary_manifest"
        and record.artifact_version == "v1"
        and record.status == "ready"
    ]
    by_key: dict[str, tuple[ArtifactRecord, dict[str, Any]]] = {}
    for record in manifest_records:
        ArtifactRegistry._verify_ready_artifact(record)
        payload = _read_json_object(record.path, label="typed Stage 1 reuse manifest")
        paper_key = str(payload.get("canonical_paper_key") or "").strip()
        if not paper_key or paper_key in by_key:
            raise ValueError("typed Stage 1 manifests have missing or duplicate paper identities")
        if paper_key not in paper_pdf_sha256:
            raise ValueError("typed Stage 1 manifest is outside the seed PDF inventory")
        if str(payload.get("source_pdf_content_sha256") or "").lower() != paper_pdf_sha256[paper_key]:
            raise ValueError("typed Stage 1 manifest does not bind the exact seed PDF bytes")
        by_key[paper_key] = (record, payload)

    if set(by_key) != set(paper_pdf_sha256):
        raise ValueError("seed lacks one ready typed Stage 1 reuse manifest per source PDF")

    reuse_manifest_paths: list[str] = []
    for paper_key in sorted(paper_pdf_sha256):
        manifest_record, manifest = by_key[paper_key]
        source_record = _record(
            registry,
            str(manifest.get("source_summary_artifact_id") or ""),
            label="typed reuse source summary",
        )
        closure_record = _record(
            registry,
            str(manifest.get("provider_receipt_closure_id") or ""),
            label="typed reuse provider closure",
        )
        ledger_record = _record(
            registry,
            str(manifest.get("provider_receipt_ledger_id") or ""),
            label="typed reuse provider ledger",
        )
        for record, expected_type, expected_hash, label in (
            (
                source_record,
                "summary_file",
                manifest.get("source_summary_artifact_hash"),
                "source summary",
            ),
            (
                closure_record,
                "provider_receipt_closure",
                manifest.get("provider_receipt_closure_hash"),
                "provider closure",
            ),
            (
                ledger_record,
                "provider_receipt_ledger",
                manifest.get("provider_receipt_ledger_hash"),
                "provider ledger",
            ),
        ):
            if record.artifact_type != expected_type or record.content_hash != expected_hash:
                raise ValueError(f"typed Stage 1 {label} Registry binding is invalid")

        summary_rows = InternalStageExecutorRegistry._summary_payloads_from_file(
            manifest_record.path
        )
        if len(summary_rows) != 1:
            raise ValueError("each typed Stage 1 reuse manifest must contain exactly one summary")
        reuse = summary_rows[0].get("stage1_reuse")
        binding = (
            Stage1ReusableSummaryBindingV1.from_mapping(reuse.get("binding"))
            if isinstance(reuse, Mapping) and isinstance(reuse.get("binding"), Mapping)
            else None
        )
        if binding is None:
            raise ValueError("typed Stage 1 reuse manifest did not materialize a binding")
        authority, _reason = verify_stage1_typed_manifest_authority(
            summary_rows[0],
            binding,
        )
        if authority is None:
            raise ValueError("typed Stage 1 manifest authority or receipt closure failed verification")
        reuse_manifest_paths.append(str(manifest_record.path))

    return tuple(reuse_manifest_paths), paper_pdf_sha256


def run_first_warm_reuse(
    *,
    seed_workspace_path: str | Path,
    seed_spec: RuntimeJobSpec,
    current_shared_fixture_config: str | Path,
    provider_budget: Mapping[str, Any],
    action: Literal["analyze", "run_all"] = "analyze",
    automatic_discovery: bool = False,
    changed_paper_keys: Collection[str] = (),
    current_pdf_folder: str | Path | None = None,
) -> WarmReuseExecution:
    """Verify seed manifests, then execute the same PDFs in a new Stage 1 job.

    The helper is intentionally local-only: it requires the seed's exact config
    file, the same output-root registry namespace, and loopback API URLs.
    """

    if action not in {"analyze", "run_all"}:
        raise ValueError("warm reuse action must be analyze or run_all")
    if not isinstance(automatic_discovery, bool):
        raise TypeError("automatic_discovery must be an explicit boolean")
    changed_keys = {str(key).strip() for key in changed_paper_keys if str(key).strip()}
    if bool(changed_keys) != (current_pdf_folder is not None):
        raise ValueError("incremental current PDF folder and changed paper keys must be supplied together")
    if current_pdf_folder is not None and not automatic_discovery:
        raise ValueError("incremental current PDF folder requires automatic Stage 1 discovery")
    seed_path = Path(seed_workspace_path).expanduser().resolve()
    seed_workspace, seed_registry = AgentRuntimeRunner._open_workspace(seed_path)
    if Path(seed_workspace.root_dir).resolve() != seed_path:
        raise ValueError("seed workspace path identity is inconsistent")
    if seed_spec.job_id != seed_workspace.job_id:
        raise ValueError("seed RuntimeJobSpec does not identify the seed workspace")
    if seed_spec.source.mode != "direct" or not seed_spec.source.pdf_folder:
        raise ValueError("warm reuse requires the seed's direct PDF source folder")
    if seed_spec.reuse_summary_files or seed_spec.summary_file or seed_spec.summary_sources:
        raise ValueError("warm reuse seed must be a cold Stage 1 source run")

    pdf_folder = Path(seed_spec.source.pdf_folder).expanduser().resolve()
    if not pdf_folder.is_dir():
        raise ValueError("seed source PDF folder is unavailable")
    shared_config = Path(current_shared_fixture_config).expanduser().resolve()
    seed_config = Path(seed_spec.config).expanduser().resolve()
    if shared_config != seed_config:
        raise ValueError("warm reuse must use the same shared fixture config path as the seed")
    config_parser = _require_loopback_config(shared_config, action=action)
    configured_output = str(config_parser.get("Paths", "output_path", fallback="")).strip()
    if not configured_output:
        raise ValueError("shared fixture config has no Paths.output_path")
    if Path(configured_output).expanduser().resolve() != Path(seed_workspace.base_output_dir).resolve():
        raise ValueError("warm reuse config does not preserve the seed Registry output root")

    runtime_record = _record(seed_registry, "runtime_job_spec", label="seed RuntimeJobSpec")
    runtime_binding = runtime_record.metadata.get("config_snapshot_binding")
    if not isinstance(runtime_binding, Mapping):
        raise TypeError("seed RuntimeJobSpec has no config-source hash binding")
    expected_config_sha = str(runtime_binding.get("config_source_sha256") or "").strip().lower()
    if not expected_config_sha or file_sha256(shared_config) != expected_config_sha:
        raise ValueError("shared fixture config bytes changed since the seed run")

    budget = ProviderAggregateBudgetV2.from_mapping(provider_budget)
    if budget.max_wall_seconds <= 0:
        raise ValueError("warm reuse requires a positive finite wall ceiling")
    if budget.max_retry_attempts_total != 0:
        raise ValueError("local warm reuse requires a zero-retry test scope")
    reuse_paths, source_hashes = _verified_reuse_inputs(
        seed_registry,
        pdf_folder=pdf_folder,
    )
    active_pdf_folder = pdf_folder
    if current_pdf_folder is not None:
        active_pdf_folder = Path(current_pdf_folder).expanduser().resolve()
        if not active_pdf_folder.is_dir():
            raise ValueError("current incremental PDF folder is unavailable")
        scope_record = _record(seed_registry, "stage1_source_scope", label="Stage 1 source scope")
        scope = _read_json_object(scope_record.path, label="Stage 1 source scope")
        source_rows = scope.get("papers")
        if not isinstance(source_rows, list) or not source_rows:
            raise ValueError("seed Stage 1 source scope has no paper inventory")
        seed_names = {Path(str(row.get("source_pdf") or "")).name for row in source_rows if isinstance(row, Mapping)}
        current_names = {
            str(path.relative_to(active_pdf_folder))
            for path in active_pdf_folder.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".pdf"
        }
        if current_names != seed_names:
            raise ValueError("current incremental PDF file inventory differs from the seed")
        current_hashes: dict[str, str] = {}
        changed_observed: set[str] = set()
        for row in source_rows:
            if not isinstance(row, Mapping):
                raise TypeError("seed Stage 1 source scope contains a malformed paper row")
            paper_key = str(row.get("paper_key") or "").strip()
            seed_source = Path(str(row.get("source_pdf") or "")).expanduser().resolve()
            current_source = active_pdf_folder / seed_source.name
            if not paper_key or not current_source.is_file():
                raise ValueError("current incremental PDF inventory is incomplete")
            current_hash = file_sha256(current_source)
            current_hashes[paper_key] = current_hash
            if current_hash != source_hashes[paper_key]:
                changed_observed.add(paper_key)
        if changed_observed != changed_keys:
            raise ValueError("current incremental PDF bytes did not match the exact changed paper-key set")
        if set(current_hashes) != set(source_hashes):
            raise ValueError("current incremental PDF identity set differs from seed source scope")
        source_hashes = current_hashes
        reuse_paths = tuple(
            path for path in reuse_paths
            if str(json.loads(Path(path).read_text(encoding="utf-8")).get("canonical_paper_key") or "")
            not in changed_keys
        )
    _completed_stage1_registry(seed_workspace, seed_registry)

    new_job_id = f"{seed_workspace.job_id}-warm-{uuid.uuid4().hex[:10]}"
    metadata = copy.deepcopy(dict(seed_spec.metadata))
    metadata["provider_budget"] = budget.to_dict()
    queue_path = Path(seed_workspace.base_output_dir) / "_queue" / f"{new_job_id}.queue.json"
    warm_spec = RuntimeJobSpec(
        project_name=seed_spec.project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(active_pdf_folder)),
        job_id=new_job_id,
        config=str(shared_config),
        action=action,
        reuse_stage1=True,
        reuse_summary_files=() if automatic_discovery else reuse_paths,
        queue_file=str(queue_path),
        metadata=metadata,
    )
    warm_spec.validate()
    plan_dir = Path(seed_workspace.base_output_dir) / "_warm_reuse_specs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    spec_path = plan_dir / f"{new_job_id}.json"
    spec_path.write_text(
        json.dumps(warm_spec.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    plan_started = time.perf_counter()
    plan = control.plan(spec_path)
    plan_seconds = time.perf_counter() - plan_started
    full_stage_plan = plan.get("full_stage_request_plan")
    boundary = full_stage_plan.get("boundary") if isinstance(full_stage_plan, Mapping) else None
    if plan.get("read_only") is not True or not isinstance(boundary, Mapping) or boundary.get("no_provider_posts") is not True:
        raise ValueError("warm reuse plan was not a read-only zero-POST plan")
    if isinstance(full_stage_plan, Mapping) and full_stage_plan.get("aggregate_budget") != budget.to_dict():
        raise ValueError("warm reuse plan did not retain the exact finite V2 test scope")
    result = AgentRuntimeRunner(warm_spec).run()
    warm_workspace, warm_registry = AgentRuntimeRunner._open_workspace(result.workspace_path)
    return WarmReuseExecution(
        spec=warm_spec,
        result=result,
        workspace=warm_workspace,
        registry=warm_registry,
        reuse_manifest_paths=reuse_paths,
        paper_pdf_sha256=source_hashes,
        plan=plan,
        plan_seconds=plan_seconds,
    )

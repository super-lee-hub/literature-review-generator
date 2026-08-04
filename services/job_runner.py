from __future__ import annotations

import argparse
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, cast

from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_contracts import SourceBundle
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json
from services.source_inventory import (
    SourceInventoryDiagnosticV1,
    SourceInventoryV1,
    build_source_inventory,
)
from services.queue_service import CancelToken
from utils import sanitize_path_component


STAGE1_REUSE_ACTIONS = {"analyze", "run_all"}


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "auto", "default"}:
            return None
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def resolve_stage1_reuse(action: str, raw_value: Any) -> bool:
    normalized_action = str(action or "analyze")
    requested_value = _coerce_optional_bool(raw_value)
    if normalized_action not in STAGE1_REUSE_ACTIONS:
        return bool(requested_value)
    return True if requested_value is None else requested_value


@dataclass(frozen=True)
class JobRunRequest:
    config: str
    project_name: Optional[str]
    pdf_folder: Optional[str]
    action: str
    job_id: Optional[str] = None
    summary_file: Optional[str] = None
    summary_sources: tuple[str, ...] = ()
    reuse_stage1: bool = False
    reuse_summary_files: tuple[str, ...] = ()
    run_all: bool = False
    analyze_only: bool = False
    generate_outline: bool = False
    generate_review: bool = False
    generate_section: Optional[int] = None
    validate_review: bool = False
    retry_failed: bool = False
    retry_review_failed: bool = False
    concept: Optional[str] = None
    free_mode_profile: Optional[str] = None
    free_mode_idea: Optional[str] = None
    progress_tracker: Optional[Any] = None
    publication_context: Optional[Any] = None
    gui: bool = False
    source_mode: str = "direct"
    zotero_report: Optional[str] = None
    library_path: Optional[str] = None
    queue_file: str = "output/_queue/queue.json"
    workspace_path: Optional[str] = None
    requested_stages: tuple[str, ...] | None = None
    validation_required: bool | None = None
    require_clean_validation: bool | None = None
    allow_unvalidated_when_validation_optional: bool | None = None
    derived_summary_source: bool = False


@dataclass(frozen=True)
class JobRunResult:
    success: bool
    exit_code: int
    message: str
    workspace_path: str
    job_id: str
    resume_state: str
    produced_artifacts: list[str]
    log_path: str
    report_paths: list[str]
    failure_summary: Optional[str]
    job_status: str = "failed"
    job_disposition: str = "unvalidated"
    canonical_ready: bool = False
    requires_attention: bool = True
    job_outcome_path: str = ""


@dataclass(frozen=True)
class PreparedSourceInventory:
    inventory: SourceInventoryV1
    source_bundle: SourceBundle | None
    canonical_ready: bool
    degradation_reasons: tuple[str, ...]
    identity_verdicts: tuple[str, ...]


def _normalize_string_list(raw_value: Any) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        return tuple(item.strip() for item in raw_value.splitlines() if item.strip())
    return tuple(str(item).strip() for item in raw_value or () if str(item).strip())


def build_job_request_from_mapping(params: Mapping[str, Any]) -> JobRunRequest:
    action = "analyze"
    if bool(params.get("run_all", False)):
        action = "run_all"
    elif bool(params.get("generate_outline", False)):
        action = "generate_outline"
    elif params.get("generate_section") is not None:
        action = "generate_section"
    elif bool(params.get("retry_failed", False)):
        action = "retry_failed"
    elif bool(params.get("generate_review", False)):
        action = "generate_review"
    elif bool(params.get("retry_review_failed", False)):
        action = "retry_review_failed"
    elif bool(params.get("validate_review", False)):
        action = "validate_review"

    source_mode = "zotero" if params.get("zotero_report") else "direct"
    summary_sources = _normalize_string_list(params.get("summary_sources", ()))
    summary_file = cast(Optional[str], params.get("summary_file"))
    if summary_file and summary_file not in summary_sources:
        summary_sources = (summary_file, *summary_sources)

    return JobRunRequest(
        config=str(params.get("config", "config.ini")),
        project_name=cast(Optional[str], params.get("project_name")),
        pdf_folder=cast(Optional[str], params.get("pdf_folder")),
        action=action,
        job_id=cast(Optional[str], params.get("job_id")),
        summary_file=summary_file,
        summary_sources=summary_sources,
        reuse_stage1=resolve_stage1_reuse(action, params.get("reuse_stage1")),
        reuse_summary_files=_normalize_string_list(params.get("reuse_summary_files", ())),
        run_all=bool(params.get("run_all", False)),
        analyze_only=bool(params.get("analyze_only", False)),
        generate_outline=bool(params.get("generate_outline", False)),
        generate_review=bool(params.get("generate_review", False)),
        generate_section=cast(Optional[int], params.get("generate_section")),
        validate_review=bool(params.get("validate_review", False)),
        retry_failed=bool(params.get("retry_failed", False)),
        retry_review_failed=bool(params.get("retry_review_failed", False)),
        concept=cast(Optional[str], params.get("concept")),
        free_mode_profile=cast(Optional[str], params.get("free_mode_profile")),
        free_mode_idea=cast(Optional[str], params.get("free_mode_idea")),
        progress_tracker=params.get("_progress_tracker"),
        gui=bool(params.get("gui", False)),
        source_mode=source_mode,
        zotero_report=cast(Optional[str], params.get("zotero_report")),
        library_path=cast(Optional[str], params.get("library_path")),
        queue_file=str(params.get("queue_file", "output/_queue/queue.json")),
        workspace_path=cast(Optional[str], params.get("workspace_path")),
        requested_stages=tuple(str(item) for item in params.get("requested_stages", ()) or ()) or None,
        validation_required=params.get("validation_required"),
        require_clean_validation=params.get("require_clean_validation"),
        allow_unvalidated_when_validation_optional=params.get(
            "allow_unvalidated_when_validation_optional"
        ),
        derived_summary_source=bool(params.get("derived_summary_source", False)),
    )


def build_job_request_from_args(args: argparse.Namespace) -> JobRunRequest:
    return build_job_request_from_mapping(vars(args))


def validate_job_request_options(request: Any) -> Optional[str]:
    summary_file = getattr(request, "summary_file", None)
    summary_sources = getattr(request, "summary_sources", ()) or ()
    reuse_stage1 = bool(getattr(request, "reuse_stage1", False))
    reuse_summary_files = getattr(request, "reuse_summary_files", ()) or ()
    action = str(getattr(request, "action", "analyze") or "analyze")
    downstream_actions = {"generate_outline", "generate_review", "generate_section", "validate_review"}
    if summary_file and action not in downstream_actions:
        return "--summary-file can only be used with a downstream review action"
    if summary_sources and action not in downstream_actions and not bool(
        getattr(request, "derived_summary_source", False)
    ):
        return "--summary-source can only be used with a downstream review action"
    if reuse_stage1 and action not in STAGE1_REUSE_ACTIONS:
        return "--reuse-stage1 can only be used with stage1 analysis or --run-all"
    if reuse_summary_files and not reuse_stage1:
        return "--reuse-summary-file requires --reuse-stage1"
    return None


class JobRunner:
    """Current production runner; all execution is owned by ``runtime.runner``."""

    def _resolve_project_name(self, request: JobRunRequest) -> str:
        if request.project_name:
            return sanitize_path_component(request.project_name)
        if request.pdf_folder:
            return sanitize_path_component(Path(request.pdf_folder).expanduser().resolve().name)
        return "literature_review"

    def _resolve_project_name_from_existing_workspaces(
        self,
        *,
        base_output_dir: str,
        requested_project_name: str,
        action: str,
    ) -> str:
        del base_output_dir, action
        return requested_project_name

    @staticmethod
    def _source_snapshot(generator: Any, request: JobRunRequest) -> dict[str, Any]:
        del generator
        return {
            "source_mode": request.source_mode,
            "pdf_folder": str(Path(request.pdf_folder).expanduser().resolve()) if request.pdf_folder else "",
            "zotero_report": str(Path(request.zotero_report).expanduser().resolve()) if request.zotero_report else "",
            "library_path": str(Path(request.library_path).expanduser().resolve()) if request.library_path else "",
            "summary_file": str(Path(request.summary_file).expanduser().resolve()) if request.summary_file else "",
            "summary_sources": [str(Path(item).expanduser().resolve()) for item in request.summary_sources],
            "reuse_summary_files": [
                str(Path(item).expanduser().resolve()) for item in request.reuse_summary_files
            ],
        }

    def _prepare_source_inventory(
        self,
        *,
        generator: Any,
        request: JobRunRequest,
        project_name: str,
    ) -> PreparedSourceInventory:
        del generator
        stage1_required = request.action in STAGE1_REUSE_ACTIONS
        summary_paths = tuple(
            dict.fromkeys(
                str(item)
                for item in (*request.summary_sources, *request.reuse_summary_files)
                if str(item).strip()
            )
        )
        diagnostics: list[SourceInventoryDiagnosticV1] = []
        degradation: list[str] = []
        bundle: SourceBundle | None = None
        if request.source_mode == "direct" and request.pdf_folder and not summary_paths:
            try:
                bundle = build_source_bundle_for_request(request, project_name=project_name)
            except Exception as exc:
                diagnostics.append(
                    SourceInventoryDiagnosticV1(
                        code="source_intake_failed",
                        severity="error",
                        message=str(exc),
                        source_type="direct",
                        path=str(request.pdf_folder),
                    )
                )
                degradation.append(f"source_intake_error:{type(exc).__name__}")
        elif request.source_mode == "zotero" and request.zotero_report and request.library_path:
            try:
                bundle = build_source_bundle_for_request(request, project_name=project_name)
            except Exception as exc:
                diagnostics.append(
                    SourceInventoryDiagnosticV1(
                        code="source_intake_failed",
                        severity="error",
                        message=str(exc),
                        source_type="zotero",
                        path=str(request.zotero_report),
                    )
                )
                degradation.append(f"source_intake_error:{type(exc).__name__}")

        inventory_mode = "summary_only" if not stage1_required and summary_paths else request.source_mode
        inventory = build_source_inventory(
            source_mode=cast(Any, inventory_mode),
            project_name=project_name,
            source_bundle=bundle if inventory_mode != "summary_only" else None,
            pdf_root=request.pdf_folder if inventory_mode == "direct" else None,
            zotero_report=request.zotero_report if inventory_mode == "zotero" else None,
            zotero_root=request.library_path if inventory_mode == "zotero" else None,
            external_summary_paths=summary_paths,
            diagnostics=diagnostics,
        )
        errors = [item for item in inventory.diagnostics if item.severity == "error"]
        ready_pdfs = sum(item.source_type == "pdf" and item.status == "ready" for item in inventory.files)
        ready_summaries = sum(
            item.source_type == "external_summary" and item.status == "ready"
            for item in inventory.files
        )
        identity_verdicts: list[str] = []
        if bundle is not None:
            raw_identity = bundle.source_snapshot.get("identity_results")
            if isinstance(raw_identity, list):
                identity_verdicts = [
                    str(item.get("identity_verdict") or "")
                    for item in raw_identity
                    if isinstance(item, Mapping) and str(item.get("identity_verdict") or "")
                ]
            if bundle.source_snapshot.get("ambiguous_matches"):
                degradation.append("ambiguous_source_identity")
            if bundle.source_snapshot.get("missing_titles"):
                degradation.append("missing_source_identity")
        if stage1_required:
            canonical_ready = bool(ready_pdfs and not errors) and not any(
                item in {"ambiguous", "mismatch"} for item in identity_verdicts
            )
            if not ready_pdfs:
                degradation.append("no_ready_pdf_sources")
        elif summary_paths:
            canonical_ready = bool(not errors and ready_summaries == len(summary_paths))
        else:
            canonical_ready = False
            degradation.append("source_not_available")
        degradation.extend(f"source_inventory:{item.code}" for item in errors)
        return PreparedSourceInventory(
            inventory=inventory,
            source_bundle=bundle,
            canonical_ready=canonical_ready,
            degradation_reasons=tuple(dict.fromkeys(degradation)),
            identity_verdicts=tuple(identity_verdicts),
        )

    @staticmethod
    def _request_snapshot(request: JobRunRequest) -> dict[str, Any]:
        payload = asdict(request)
        payload.pop("progress_tracker", None)
        payload.pop("publication_context", None)
        return payload

    def _build_workspace(
        self,
        *,
        base_output_dir: str,
        project_name: str,
        pointer_payload: dict[str, Any] | None,
        fingerprint_bundle: dict[str, Any],
        request: JobRunRequest | None = None,
    ) -> JobWorkspace:
        requested_job_id = str(getattr(request, "job_id", "") or "")
        if requested_job_id:
            return JobWorkspace.create(base_output_dir, project_name, requested_job_id)
        if pointer_payload:
            pointer_path = str(pointer_payload.get("workspace_path") or "")
            if pointer_path and pointer_payload.get("fingerprint_bundle") == fingerprint_bundle:
                path = Path(pointer_path).expanduser().resolve()
                if path.is_dir():
                    return JobWorkspace.from_workspace_path(
                        str(path), project_name, str(pointer_payload.get("job_id") or "") or None
                    )
        return JobWorkspace.create(base_output_dir, project_name)

    @staticmethod
    def _write_resume_report(workspace: JobWorkspace, report: Any) -> str:
        path = workspace.artifact_path("resume_state_report.json")
        atomic_write_json(path, asdict(report))
        return path

    @staticmethod
    def _result_from_execution(execution: Any) -> JobRunResult:
        workspace_path = str(execution.workspace_path)
        workspace = Path(workspace_path)
        produced: list[str] = []
        log_path = ""
        if workspace.is_dir():
            registry_path = workspace / "artifact_registry.json"
            try:
                registry = ArtifactRegistry(str(registry_path), str(execution.job_id))
                records = [record for record in registry.list_records() if record.status == "ready"]
                produced = [record.path for record in records]
                logs = [record.path for record in records if record.artifact_type == "job_log"]
                log_path = logs[0] if logs else ""
            except Exception:
                produced = []
            if not log_path:
                candidate = workspace / "logs" / "job.log"
                log_path = str(candidate) if candidate.is_file() else ""
        reports = [str(path) for path in (workspace / "reports").glob("*") if path.is_file()] if (workspace / "reports").is_dir() else []
        success = bool(execution.canonical_ready)
        return JobRunResult(
            success=success,
            exit_code=0 if execution.job_status == "completed" else 1,
            message=str(execution.message or execution.completion_status or execution.job_status),
            workspace_path=workspace_path,
            job_id=str(execution.job_id),
            resume_state="resumed" if execution.resumed_from_attempt is not None else "new",
            produced_artifacts=produced,
            log_path=log_path,
            report_paths=reports,
            failure_summary=None if success else str(execution.message or "runtime execution did not reach canonical readiness"),
            job_status=str(execution.job_status),
            job_disposition=str(execution.job_disposition),
            canonical_ready=bool(execution.canonical_ready),
            requires_attention=bool(execution.requires_attention),
            job_outcome_path=str(execution.job_outcome_path),
        )

    def _runtime_spec(self, request: JobRunRequest):
        from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec

        project_name = self._resolve_project_name(request)
        metadata: dict[str, Any] = {
            "requested_stages": list(request.requested_stages) if request.requested_stages is not None else None,
            "validation_required": request.validation_required,
            "require_clean_validation": request.require_clean_validation,
            "allow_unvalidated_when_validation_optional": request.allow_unvalidated_when_validation_optional,
        }
        return RuntimeJobSpec(
            project_name=project_name,
            source=RuntimeSourceSpec(
                mode=cast(Any, request.source_mode),
                pdf_folder=str(Path(request.pdf_folder).expanduser().resolve()) if request.pdf_folder else "",
                zotero_report=str(Path(request.zotero_report).expanduser().resolve()) if request.zotero_report else "",
                library_path=str(Path(request.library_path).expanduser().resolve()) if request.library_path else "",
            ),
            job_id=request.job_id or "",
            config=str(Path(request.config).expanduser().resolve()),
            action=request.action,
            summary_file=str(Path(request.summary_file).expanduser().resolve()) if request.summary_file else "",
            summary_sources=tuple(str(Path(item).expanduser().resolve()) for item in request.summary_sources),
            reuse_stage1=request.reuse_stage1,
            reuse_summary_files=tuple(str(Path(item).expanduser().resolve()) for item in request.reuse_summary_files),
            generate_section=request.generate_section,
            queue_file=str(Path(request.queue_file).expanduser().resolve()),
            workspace_path=(
                str(Path(request.workspace_path).expanduser().resolve())
                if request.workspace_path
                else ""
            ),
            metadata=metadata,
        )

    def run(self, request: JobRunRequest, cancel_token: CancelToken | None = None) -> JobRunResult:
        error = validate_job_request_options(request)
        if error:
            raise ValueError(error)
        from runtime.runner import AgentRuntimeRunner

        token = cancel_token or (
            request.progress_tracker if isinstance(request.progress_tracker, CancelToken) else None
        )
        execution = AgentRuntimeRunner(
            self._runtime_spec(request),
            cancel_token=token,
            publication_context=request.publication_context,
        ).run()
        return self._result_from_execution(execution)

from __future__ import annotations

import argparse
import hashlib
import json
import os
import glob
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, cast

from runtime.lifecycle import BootstrappedRuntimeContext, bootstrap_job_runtime, finalize_job_runtime
from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_contracts import SourceBundle
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json
from services.source_inventory import (
    SourceInventoryDiagnosticV1,
    SourceInventoryV1,
    build_source_inventory,
)
from services.queue_service import CancelToken, JobCancelledError
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
    """Resolve the effective stage-1 reuse policy.

    Historical stage-1 reuse is automatic for stage-1 actions unless the caller
    explicitly disables it. Downstream actions only preserve explicit opt-in so
    validation can reject unsupported reuse flags with the existing error path.
    """
    normalized_action = str(action or "analyze")
    requested_value = _coerce_optional_bool(raw_value)
    if normalized_action not in STAGE1_REUSE_ACTIONS:
        return bool(requested_value)
    if requested_value is None:
        return True
    return requested_value


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
    gui: bool = False
    source_mode: str = "direct"
    zotero_report: Optional[str] = None
    library_path: Optional[str] = None
    queue_file: str = "output/_queue/queue.json"
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
    # 新增产物追踪字段
    produced_artifacts: List[str]
    log_path: str
    report_paths: List[str]
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


def build_job_request_from_mapping(params: Mapping[str, Any]) -> JobRunRequest:
    action = "analyze"
    if bool(params.get("run_all", False)):
        action = "run_all"
    elif bool(params.get("generate_outline", False)):
        action = "generate_outline"
    elif params.get("generate_section", None):
        action = "generate_section"
    elif bool(params.get("retry_failed", False)):
        action = "retry_failed"
    elif bool(params.get("generate_review", False)):
        action = "generate_review"
    elif bool(params.get("retry_review_failed", False)):
        action = "retry_review_failed"
    elif bool(params.get("validate_review", False)):
        action = "validate_review"

    source_mode = "direct"
    if params.get("zotero_report", None):
        source_mode = "zotero"

    def _normalize_string_list(raw_value: Any) -> tuple[str, ...]:
        if isinstance(raw_value, str):
            return tuple(
                item.strip()
                for item in raw_value.splitlines()
                if item.strip()
            )
        return tuple(
            str(item).strip()
            for item in raw_value or ()
            if str(item).strip()
        )

    normalized_summary_sources = _normalize_string_list(params.get("summary_sources", ()))
    summary_file = cast(Optional[str], params.get("summary_file", None))
    summary_source_items = list(normalized_summary_sources)
    if summary_file and summary_file not in summary_source_items:
        summary_source_items.insert(0, summary_file)

    normalized_reuse_files = _normalize_string_list(params.get("reuse_summary_files", ()))

    reuse_stage1 = resolve_stage1_reuse(action, params.get("reuse_stage1", None))

    return JobRunRequest(
        config=str(params.get("config", "config.ini")),
        project_name=cast(Optional[str], params.get("project_name", None)),
        pdf_folder=cast(Optional[str], params.get("pdf_folder", None)),
        action=action,
        job_id=cast(Optional[str], params.get("job_id", None)),
        summary_file=summary_file,
        summary_sources=tuple(summary_source_items),
        reuse_stage1=reuse_stage1,
        reuse_summary_files=normalized_reuse_files,
        run_all=bool(params.get("run_all", False)),
        analyze_only=bool(params.get("analyze_only", False)),
        generate_outline=bool(params.get("generate_outline", False)),
        generate_review=bool(params.get("generate_review", False)),
        generate_section=cast(Optional[int], params.get("generate_section", None)),
        validate_review=bool(params.get("validate_review", False)),
        retry_failed=bool(params.get("retry_failed", False)),
        retry_review_failed=bool(params.get("retry_review_failed", False)),
        concept=cast(Optional[str], params.get("concept", None)),
        free_mode_profile=cast(Optional[str], params.get("free_mode_profile", None)),
        free_mode_idea=cast(Optional[str], params.get("free_mode_idea", None)),
        progress_tracker=params.get("_progress_tracker", None),
        gui=bool(params.get("gui", False)),
        source_mode=source_mode,
        zotero_report=cast(Optional[str], params.get("zotero_report", None)),
        library_path=cast(Optional[str], params.get("library_path", None)),
        queue_file=str(params.get("queue_file", "output/_queue/queue.json")),
    )


def build_job_request_from_args(args: argparse.Namespace) -> JobRunRequest:
    return build_job_request_from_mapping(vars(args))


def validate_job_request_options(request: Any) -> Optional[str]:
    summary_file = getattr(request, "summary_file", None)
    summary_sources = getattr(request, "summary_sources", ()) or ()
    reuse_stage1 = bool(getattr(request, "reuse_stage1", False))
    reuse_summary_files = getattr(request, "reuse_summary_files", ()) or ()
    action = str(getattr(request, "action", "analyze") or "analyze")
    free_mode_profile = str(getattr(request, "free_mode_profile", "") or "").strip()
    free_mode_idea = str(getattr(request, "free_mode_idea", "") or "").strip()

    if summary_file and action not in {"generate_outline", "generate_review", "generate_section", "validate_review"}:
        return "--summary-file can only be used with generate-outline, generate-review, generate-section, or validate-review"
    if (
        summary_sources
        and action not in {"generate_outline", "generate_review", "generate_section", "validate_review"}
        and not bool(getattr(request, "derived_summary_source", False))
    ):
        return "--summary-source can only be used with generate-outline, generate-review, generate-section, or validate-review"
    if reuse_stage1 and action not in STAGE1_REUSE_ACTIONS:
        return "--reuse-stage1 can only be used with stage1 analysis or --run-all"
    if reuse_summary_files and not reuse_stage1:
        return "--reuse-summary-file requires --reuse-stage1"
    if free_mode_profile and free_mode_idea:
        return "--free-mode-profile and --free-mode-idea are mutually exclusive"
    if free_mode_profile and not os.path.isfile(os.path.abspath(free_mode_profile)):
        return f"--free-mode-profile does not exist: {os.path.abspath(free_mode_profile)}"
    return None


class JobRunner:
    @staticmethod
    def _lossy_project_alias(project_name: str) -> str:
        sanitized = sanitize_path_component(project_name)
        alias_chars: list[str] = []
        for char in sanitized:
            if char.isascii() and (char.isalnum() or char in {"_", "-", ".", " "}):
                alias_chars.append(char)
            else:
                alias_chars.append("_")
        alias = "".join(alias_chars).strip(" .")
        return alias or "unknown"

    @staticmethod
    def _looks_like_lossy_project_name(project_name: str) -> bool:
        sanitized = sanitize_path_component(project_name)
        if not sanitized or any(not char.isascii() for char in sanitized):
            return False
        return "_" in sanitized

    def _workspace_support_score(self, workspace_path: str, project_name: str, action: str) -> tuple[int, float]:
        artifacts_dir = os.path.join(workspace_path, "artifacts")
        summary_path = os.path.join(artifacts_dir, f"{project_name}_summaries.json")
        outline_path = os.path.join(artifacts_dir, f"{project_name}_literature_review_outline.md")
        review_draft_v2_path = os.path.join(
            artifacts_dir,
            "review_drafts",
            f"{project_name}_review_draft_v2.json",
        )
        citation_manifest_v3_path = os.path.join(
            artifacts_dir,
            "citation_manifests",
            f"{project_name}_citation_manifest_v3.json",
        )
        citation_manifest_v2_path = os.path.join(
            artifacts_dir,
            "citation_manifests",
            f"{project_name}_citation_manifest_v2.json",
        )

        score = 0
        has_summary = os.path.exists(summary_path)
        has_outline = os.path.exists(outline_path)
        has_review_draft = os.path.exists(review_draft_v2_path)
        has_citation_manifest = os.path.exists(citation_manifest_v3_path) or os.path.exists(citation_manifest_v2_path)

        if has_summary:
            score += 10
        if has_outline:
            score += 20
        if has_review_draft:
            score += 30
        if has_citation_manifest:
            score += 30

        if action == "generate_outline" and has_summary:
            score += 100
        if action in {"generate_review", "generate_section", "retry_review_failed"} and has_outline:
            score += 100
        if action == "validate_review":
            if has_review_draft:
                score += 100
            if has_citation_manifest:
                score += 100

        try:
            mtime = os.path.getmtime(workspace_path)
        except OSError:
            mtime = 0.0
        return score, mtime

    def _resolve_project_name_from_existing_workspaces(
        self,
        *,
        base_output_dir: str,
        requested_project_name: str,
        action: str,
    ) -> str:
        if action not in {"generate_outline", "generate_review", "generate_section", "retry_review_failed", "validate_review"}:
            return requested_project_name

        base_output_dir = os.path.abspath(base_output_dir)
        if not os.path.isdir(base_output_dir):
            return requested_project_name
        if not self._looks_like_lossy_project_name(requested_project_name):
            return requested_project_name

        requested_alias = self._lossy_project_alias(requested_project_name)
        if not requested_alias:
            return requested_project_name

        best_project_name = requested_project_name
        best_score = (-1, -1.0)

        candidate_project_names: set[str] = {requested_project_name}
        for entry in os.listdir(base_output_dir):
            entry_path = os.path.join(base_output_dir, entry)
            if not os.path.isdir(entry_path) or entry.startswith("_"):
                continue
            if "__" in entry:
                candidate_project_names.add(entry.split("__", 1)[0])
            else:
                candidate_project_names.add(entry)

        for candidate_project_name in candidate_project_names:
            if self._lossy_project_alias(candidate_project_name) != requested_alias:
                continue

            workspace_pattern = os.path.join(base_output_dir, f"{candidate_project_name}__*")
            for workspace_path in glob.glob(workspace_pattern):
                if not os.path.isdir(workspace_path):
                    continue
                candidate_score = self._workspace_support_score(workspace_path, candidate_project_name, action)
                if candidate_score > best_score:
                    best_score = candidate_score
                    best_project_name = candidate_project_name

        if best_project_name != requested_project_name and best_score[0] > 0:
            return best_project_name
        return requested_project_name

    def _legacy_args_namespace(self, request: JobRunRequest, project_name: str) -> argparse.Namespace:
        return argparse.Namespace(
            config=request.config,
            project_name=project_name,
            pdf_folder=request.pdf_folder,
            summary_file=request.summary_file,
            summary_sources=list(request.summary_sources),
            reuse_stage1=request.reuse_stage1,
            reuse_summary_files=list(request.reuse_summary_files),
            run_all=request.run_all,
            analyze_only=request.analyze_only,
            generate_outline=request.generate_outline,
            generate_review=request.generate_review,
            generate_section=request.generate_section,
            validate_review=request.validate_review,
            retry_failed=request.retry_failed,
            retry_review_failed=request.retry_review_failed,
            concept=request.concept,
            free_mode_profile=request.free_mode_profile,
            free_mode_idea=request.free_mode_idea,
            gui=request.gui,
        )

    def _resolve_project_name(self, request: JobRunRequest) -> str:
        if request.project_name:
            return sanitize_path_component(request.project_name)
        if request.pdf_folder:
            return sanitize_path_component(os.path.basename(os.path.abspath(request.pdf_folder.rstrip("/\\"))))
        return "literature_review"

    def _pointer_payload(self, pointer_path: str) -> dict[str, Any] | None:
        if not os.path.exists(pointer_path):
            return None
        try:
            with open(pointer_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _source_snapshot(self, generator: Any, request: JobRunRequest) -> dict[str, Any]:
        paths_config = generator.config.get("Paths", {}) if getattr(generator, "config", None) else {}
        return {
            "source_mode": request.source_mode,
            "pdf_folder": os.path.abspath(request.pdf_folder) if request.pdf_folder else "",
            "zotero_report": os.path.abspath(request.zotero_report) if request.zotero_report else os.path.abspath(paths_config.get("zotero_report", "")) if paths_config.get("zotero_report") else "",
            "library_path": os.path.abspath(request.library_path) if request.library_path else os.path.abspath(paths_config.get("library_path", "")) if paths_config.get("library_path") else "",
            "summary_file": os.path.abspath(request.summary_file) if request.summary_file else "",
            "summary_sources": [os.path.abspath(item) for item in request.summary_sources],
            "reuse_summary_files": [os.path.abspath(item) for item in request.reuse_summary_files],
            "project_name": self._resolve_project_name(request),
        }

    def _prepare_source_inventory(
        self,
        *,
        generator: Any,
        request: JobRunRequest,
        project_name: str,
    ) -> PreparedSourceInventory:
        source_snapshot = self._source_snapshot(generator, request)
        action_needs_stage1 = request.action in STAGE1_REUSE_ACTIONS
        resolved_report = str(source_snapshot.get("zotero_report") or "")
        resolved_library = str(source_snapshot.get("library_path") or "")
        resolved_pdf_folder = str(source_snapshot.get("pdf_folder") or "")
        effective_mode = (
            "zotero"
            if request.source_mode == "zotero" or (resolved_report and resolved_library)
            else "direct"
        )
        source_bundle: SourceBundle | None = None
        diagnostics: list[SourceInventoryDiagnosticV1] = []
        degradation_reasons: list[str] = []

        resolved_request = replace(
            request,
            source_mode=effective_mode,
            pdf_folder=resolved_pdf_folder or None,
            zotero_report=resolved_report or None,
            library_path=resolved_library or None,
        )
        can_build_bundle = bool(
            (effective_mode == "direct" and resolved_pdf_folder)
            or (effective_mode == "zotero" and resolved_report and resolved_library)
        )
        if can_build_bundle:
            try:
                source_bundle = build_source_bundle_for_request(
                    resolved_request,
                    project_name=project_name,
                )
            except Exception as exc:
                reason = f"source_intake_error:{type(exc).__name__}:{exc}"
                degradation_reasons.append(reason)
                diagnostics.append(
                    SourceInventoryDiagnosticV1(
                        code="source_intake_failed",
                        severity="error",
                        message=str(exc),
                        source_type=effective_mode,
                        path=resolved_report or resolved_pdf_folder,
                    )
                )

        explicit_summaries = tuple(dict.fromkeys([
            *(str(item) for item in request.summary_sources if str(item)),
            *(str(item) for item in request.reuse_summary_files if str(item)),
        ]))
        inventory_mode = (
            "summary_only"
            if not action_needs_stage1 and explicit_summaries
            else effective_mode
        )
        inventory = build_source_inventory(
            source_mode=cast(Any, inventory_mode),
            project_name=project_name,
            source_bundle=source_bundle if inventory_mode != "summary_only" else None,
            pdf_root=resolved_pdf_folder if effective_mode == "direct" else None,
            zotero_report=resolved_report if effective_mode == "zotero" else None,
            zotero_root=resolved_library if effective_mode == "zotero" else None,
            external_summary_paths=explicit_summaries,
            diagnostics=diagnostics,
        )

        identity_verdicts: list[str] = []
        if source_bundle is not None:
            raw_identity_results = source_bundle.source_snapshot.get("identity_results")
            if isinstance(raw_identity_results, list):
                identity_verdicts = [
                    str(item.get("identity_verdict") or "")
                    for item in raw_identity_results
                    if isinstance(item, Mapping) and str(item.get("identity_verdict") or "")
                ]
            raw_quarantine = source_bundle.source_snapshot.get("quarantined_sources")
            if isinstance(raw_quarantine, list) and raw_quarantine:
                degradation_reasons.extend(
                    f"source_identity_{str(item.get('identity_verdict') or 'ambiguous')}"
                    for item in raw_quarantine
                    if isinstance(item, Mapping)
                )
            if source_bundle.source_snapshot.get("ambiguous_matches"):
                degradation_reasons.append("ambiguous_pdf_match")
            if source_bundle.source_snapshot.get("missing_titles"):
                degradation_reasons.append("missing_pdf_source")

        error_diagnostics = [item for item in inventory.diagnostics if item.severity == "error"]
        ready_pdf_count = sum(
            1
            for item in inventory.files
            if item.source_type == "pdf" and item.status == "ready"
        )
        ready_summary_count = sum(
            1
            for item in inventory.files
            if item.source_type == "external_summary" and item.status == "ready"
        )
        identity_blocked = any(verdict in {"ambiguous", "mismatch"} for verdict in identity_verdicts)
        if action_needs_stage1:
            canonical_ready = (
                not error_diagnostics
                and ready_pdf_count > 0
                and not identity_blocked
                and "ambiguous_pdf_match" not in degradation_reasons
                and "missing_pdf_source" not in degradation_reasons
            )
            if not ready_pdf_count:
                degradation_reasons.append("no_ready_pdf_sources")
        elif explicit_summaries:
            canonical_ready = not error_diagnostics and ready_summary_count == len(explicit_summaries)
        else:
            canonical_ready = False
            degradation_reasons.append("legacy_unverified_source")

        degradation_reasons.extend(
            f"source_inventory:{item.code}"
            for item in error_diagnostics
        )
        return PreparedSourceInventory(
            inventory=inventory,
            source_bundle=source_bundle,
            canonical_ready=canonical_ready,
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
            identity_verdicts=tuple(identity_verdicts),
        )

    def _collect_artifact_tracking_info(self, registry: ArtifactRegistry, workspace: JobWorkspace, failure_summary: Optional[str]) -> tuple[List[str], str, List[str]]:
        """收集产物追踪信息"""
        produced_artifacts = [record.path for record in registry.list_records() if record.status == "ready"]
        registered_log_paths = [
            record.path
            for record in registry.list_records()
            if record.status == "ready" and record.artifact_type == "job_log"
        ]
        workspace_log_path = workspace.log_path("job.log")
        if registered_log_paths:
            log_path = registered_log_paths[0]
        elif os.path.exists(workspace_log_path):
            log_path = workspace_log_path
        else:
            log_path = ""
        report_paths = []
        try:
            report_dir = workspace.paths.reports_dir
            if os.path.exists(report_dir):
                report_paths = [os.path.join(report_dir, f) for f in os.listdir(report_dir) if f.endswith(".txt") or f.endswith(".json")]
        except Exception:
            pass
        return produced_artifacts, log_path, report_paths

    def _request_snapshot(self, request: JobRunRequest) -> dict[str, Any]:
        profile_path = (
            os.path.abspath(request.free_mode_profile)
            if request.free_mode_profile
            else ""
        )
        profile_hash = file_sha256(profile_path) if profile_path and os.path.isfile(profile_path) else ""
        idea_text = str(request.free_mode_idea or "")
        return {
            "action": request.action,
            "project_name": request.project_name or "",
            "pdf_folder": os.path.abspath(request.pdf_folder) if request.pdf_folder else "",
            "summary_file": os.path.abspath(request.summary_file) if request.summary_file else "",
            "summary_sources": [os.path.abspath(item) for item in request.summary_sources],
            "reuse_stage1": bool(request.reuse_stage1),
            "reuse_summary_files": [os.path.abspath(item) for item in request.reuse_summary_files],
            "generate_section": request.generate_section,
            "retry_failed": request.retry_failed,
            "concept": request.concept or "",
            "free_mode_profile": profile_path,
            "free_mode_profile_sha256": profile_hash,
            "free_mode_idea_sha256": (
                hashlib.sha256(idea_text.encode("utf-8")).hexdigest()
                if idea_text
                else ""
            ),
            "gui": bool(request.gui),
            "requested_stages": list(request.requested_stages) if request.requested_stages is not None else None,
            "validation_required": request.validation_required,
            "require_clean_validation": request.require_clean_validation,
            "allow_unvalidated_when_validation_optional": request.allow_unvalidated_when_validation_optional,
            "derived_summary_source": request.derived_summary_source,
        }

    def _build_workspace(
        self,
        *,
        base_output_dir: str,
        project_name: str,
        pointer_payload: dict[str, Any] | None,
        fingerprint_bundle: dict[str, Any],
        request: JobRunRequest | None = None,
    ) -> JobWorkspace:
        explicit_job_id = str(getattr(request, "job_id", "") or "")
        if explicit_job_id:
            return JobWorkspace.create(
                base_output_dir=base_output_dir,
                project_name=project_name,
                job_id=explicit_job_id,
            )

        # 策略1：优先检查是否有完全匹配的工作空间
        if pointer_payload:
            pointer_fingerprint = pointer_payload.get("fingerprint_bundle", {})
            workspace_path = str(pointer_payload.get("workspace_path", "") or "")
            pointer_job_id = pointer_payload.get("job_id")
            if workspace_path and pointer_fingerprint == fingerprint_bundle and os.path.exists(workspace_path):
                return JobWorkspace.from_workspace_path(
                    workspace_path=workspace_path,
                    project_name=project_name,
                    job_id=str(pointer_job_id) if pointer_job_id else None,
                )
        
        action = str(getattr(request, "action", "") or "")
        allow_compat_artifact_recovery = (
            action not in STAGE1_REUSE_ACTIONS
            and not str(getattr(request, "job_id", "") or "")
        )

        # Downstream compatibility recovery remains available until Phase 5
        # replaces it with explicit prior-workspace dependencies. Stage 1 never
        # reuses a workspace whose source inventory fingerprint differs.
        import glob
        workspace_pattern = os.path.join(os.path.abspath(base_output_dir), f"{project_name}__*")
        workspaces = glob.glob(workspace_pattern)
        
        if workspaces and allow_compat_artifact_recovery:
            # 按修改时间排序，找到最新的工作空间
            workspaces.sort(key=os.path.getmtime, reverse=True)
            latest_workspace_path = workspaces[0]
            
            # 检查这个工作空间是否有摘要文件或其他产出物
            has_summaries = os.path.exists(os.path.join(latest_workspace_path, "artifacts", f"{project_name}_summaries.json"))
            has_outline = os.path.exists(os.path.join(latest_workspace_path, "artifacts", f"{project_name}_literature_review_outline.md"))
            
            if has_summaries or has_outline:
                # 如果有摘要或大纲，就重用这个工作空间
                return JobWorkspace.from_workspace_path(
                    workspace_path=latest_workspace_path,
                    project_name=project_name,
                    job_id=None,
                )

        # 如果找不到合适的工作空间，创建新的
        return JobWorkspace.create(
            base_output_dir=base_output_dir,
            project_name=project_name,
            job_id=None,
        )

    def _write_resume_report(self, workspace: JobWorkspace, report: Any) -> str:
        path = workspace.artifact_path("resume_state_report.json")
        atomic_write_json(path, asdict(report))
        return path

    @staticmethod
    def _normalize_handler_result(result: Any) -> bool:
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        return bool(result)

    def _finalize_run_state(
        self,
        *,
        context: BootstrappedRuntimeContext,
        status: str,
        job_disposition: str | None = None,
        canonical_ready: bool | None = None,
        requires_attention: bool | None = None,
        completed_stages: tuple[str, ...] = (),
        failed_stage: str | None = None,
        degradation_reasons: tuple[str, ...] = (),
    ) -> str:
        return finalize_job_runtime(
            context=context,
            write_resume_report=self._write_resume_report,
            status=status,
            job_disposition=cast(Any, job_disposition),
            canonical_ready=canonical_ready,
            requires_attention=requires_attention,
            completed_stages=completed_stages,
            failed_stage=failed_stage,
            degradation_reasons=degradation_reasons,
        )

    def _execute_legacy_action(
        self,
        legacy_main: Any,
        generator: Any,
        legacy_args: argparse.Namespace,
        request: JobRunRequest,
    ) -> tuple[bool, int, str]:
        try:
            if request.run_all:
                result = legacy_main.handle_run_all_mode(generator)
            elif request.generate_outline:
                result = legacy_main.handle_generate_outline_mode(generator, legacy_args)
            elif request.action == "generate_section" and request.generate_section:
                result = legacy_main.handle_generate_section_mode(generator, legacy_args)
            elif request.retry_failed:
                result = legacy_main.handle_retry_failed(legacy_args)
            elif request.generate_review:
                result = legacy_main.handle_generate_review_mode(generator)
            elif request.retry_review_failed:
                result = legacy_main.handle_retry_review_failed_mode(generator)
            elif request.validate_review:
                if generator.load_existing_summaries():
                    import validator as validator_module

                    result = validator_module.run_review_validation(generator)
                else:
                    generator.logger.error("无法加载摘要文件，请先运行阶段一")
                    return False, 1, "unable to load summaries for validation"
            else:
                result = legacy_main.handle_stage_one_mode(generator, legacy_args)
            success = self._normalize_handler_result(result)
            return success, 0 if success else 1, "completed" if success else "handler returned unsuccessful result"
        except JobCancelledError:
            raise
        except SystemExit as exc:
            code = int(exc.code or 0)
            return code == 0, code, f"Exited with code {code}"
        except Exception as exc:
            return False, 1, str(exc)

    @staticmethod
    def _read_job_outcome(path: str) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("job outcome must be a JSON object")
        return payload

    def run(self, request: JobRunRequest, cancel_token: CancelToken | None = None) -> JobRunResult:
        import main as legacy_main

        project_name = self._resolve_project_name(request)
        workspace: JobWorkspace | None = None
        registry: ArtifactRegistry | None = None
        runtime_context: BootstrappedRuntimeContext | None = None
        active_cancel_token = cancel_token or CancelToken()

        request_error = validate_job_request_options(request)
        if request_error:
            return JobRunResult(
                success=False,
                exit_code=1,
                message=request_error,
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
                produced_artifacts=[],
                log_path="",
                report_paths=[],
                failure_summary=request_error,
            )

        try:
            active_cancel_token.check_cancelled()
        except JobCancelledError as exc:
            return JobRunResult(
                success=False,
                exit_code=130,
                message=str(exc),
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
                produced_artifacts=[],
                log_path="",
                report_paths=[],
                failure_summary=None,
            )

        # 使用 request 中的 queue_file 参数
        generator = legacy_main.LiteratureReviewGenerator(request.config, project_name, request.pdf_folder, request.queue_file, request.zotero_report, request.library_path)
        generator.cancel_token = active_cancel_token
        generator.progress_tracker = request.progress_tracker
        generator.free_mode_profile_path = request.free_mode_profile
        generator.free_mode_idea = request.free_mode_idea
        generator.summary_file_override = request.summary_file
        generator.summary_source_overrides = list(request.summary_sources)
        generator.reuse_stage1 = request.reuse_stage1
        generator.reuse_summary_files = list(request.reuse_summary_files)

        if not generator.load_configuration():
            return JobRunResult(
                success=False,
                exit_code=1,
                message="configuration load failed",
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
                produced_artifacts=[],
                log_path="",
                report_paths=[],
                failure_summary="configuration load failed",
            )

        if generator.config is None:
            return JobRunResult(
                success=False,
                exit_code=1,
                message="configuration is unavailable after load",
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
                produced_artifacts=[],
                log_path="",
                report_paths=[],
                failure_summary="configuration is unavailable after load",
            )

        generator_config = cast(MutableMapping[str, Dict[str, str]], generator.config)
        output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")
        resolved_project_name = self._resolve_project_name_from_existing_workspaces(
            base_output_dir=output_base_dir,
            requested_project_name=project_name,
            action=request.action,
        )
        if resolved_project_name != project_name:
            project_name = resolved_project_name
            generator.project_name = resolved_project_name
            generator.logger.warning(
                f"Recovered project name from lossy CLI input: {request.project_name or ''} -> {resolved_project_name}"
            )

        prepared_sources = self._prepare_source_inventory(
            generator=generator,
            request=request,
            project_name=project_name,
        )
        runtime_context = bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name=project_name,
            source_snapshot=self._source_snapshot(generator, request),
            request_snapshot=self._request_snapshot(request),
            build_workspace=self._build_workspace,
            write_resume_report=self._write_resume_report,
            source_inventory=prepared_sources.inventory,
            source_canonical_ready=prepared_sources.canonical_ready,
            source_degradation_reasons=prepared_sources.degradation_reasons,
        )
        workspace = runtime_context.workspace
        registry = runtime_context.registry
        try:
            active_cancel_token.check_cancelled()

            if request.action in STAGE1_REUSE_ACTIONS and not prepared_sources.canonical_ready:
                message = "source identity or inventory requires review; Stage 1 was not executed"
                resume_state = self._finalize_run_state(
                    context=runtime_context,
                    status="completed",
                    job_disposition="needs_review",
                    canonical_ready=False,
                    requires_attention=True,
                    degradation_reasons=prepared_sources.degradation_reasons,
                )
                produced_artifacts, log_path, report_paths = self._collect_artifact_tracking_info(
                    registry,
                    workspace,
                    message,
                )
                outcome = self._read_job_outcome(runtime_context.job_outcome_path)
                return JobRunResult(
                    success=bool(outcome.get("canonical_ready", False)),
                    exit_code=0,
                    message=message,
                    workspace_path=workspace.root_dir,
                    job_id=workspace.job_id,
                    resume_state=resume_state,
                    produced_artifacts=produced_artifacts,
                    log_path=log_path,
                    report_paths=report_paths,
                    failure_summary=message,
                    job_status=str(outcome.get("job_status") or "completed"),
                    job_disposition=str(outcome.get("job_disposition") or "needs_review"),
                    canonical_ready=bool(outcome.get("canonical_ready", False)),
                    requires_attention=bool(outcome.get("requires_attention", True)),
                    job_outcome_path=runtime_context.job_outcome_path,
                )

            if request.concept:
                generator.concept_mode = True
                concept_profile_file = generator._get_concept_profile_file_path()
                if os.path.exists(concept_profile_file):
                    try:
                        with open(concept_profile_file, "r", encoding="utf-8") as handle:
                            generator.concept_profile = json.load(handle)
                        generator.logger.success(f"概念配置文件已加载: {concept_profile_file}")
                    except Exception as exc:
                        generator.logger.error(f"加载概念配置文件失败: {exc}")
                        generator.concept_profile = None
                else:
                    generator.logger.warning(f"未找到概念配置文件: {concept_profile_file}")
                    generator.logger.warning("概念增强分析将无法执行，请先运行概念学习阶段")

            legacy_args = self._legacy_args_namespace(request, project_name)
            success, exit_code, message = self._execute_legacy_action(legacy_main, generator, legacy_args, request)
            resume_state = self._finalize_run_state(
                context=runtime_context,
                status="completed" if success else "failed",
                completed_stages=runtime_context.required_stages if success else (),
                failed_stage=None if success else request.action,
            )
            outcome = self._read_job_outcome(runtime_context.job_outcome_path)
            canonical_ready = bool(outcome.get("canonical_ready", False))
            failure_summary = message if not success else (None if canonical_ready else "canonical output is not ready")
            produced_artifacts, log_path, report_paths = self._collect_artifact_tracking_info(registry, workspace, failure_summary)

            return JobRunResult(
                success=canonical_ready,
                exit_code=exit_code,
                message=message,
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
                produced_artifacts=produced_artifacts,
                log_path=log_path,
                report_paths=report_paths,
                failure_summary=failure_summary,
                job_status=str(outcome.get("job_status") or ("completed" if success else "failed")),
                job_disposition=str(outcome.get("job_disposition") or "unvalidated"),
                canonical_ready=canonical_ready,
                requires_attention=bool(outcome.get("requires_attention", not canonical_ready)),
                job_outcome_path=runtime_context.job_outcome_path,
            )

        except JobCancelledError as exc:
            resume_state = self._finalize_run_state(
                context=runtime_context,
                status="cancelled",
                canonical_ready=False,
                requires_attention=True,
                failed_stage=request.action,
            )
            failure_summary = str(exc)
            produced_artifacts, log_path, report_paths = self._collect_artifact_tracking_info(registry, workspace, failure_summary)
            
            return JobRunResult(
                success=False,
                exit_code=130,
                message=str(exc),
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
                produced_artifacts=produced_artifacts,
                log_path=log_path,
                report_paths=report_paths,
                failure_summary=failure_summary,
                job_status="cancelled",
                job_disposition="unvalidated",
                canonical_ready=False,
                requires_attention=True,
                job_outcome_path=runtime_context.job_outcome_path,
            )
        except Exception as exc:
            resume_state = self._finalize_run_state(
                context=runtime_context,
                status="failed",
                canonical_ready=False,
                requires_attention=True,
                failed_stage=request.action,
            )
            failure_summary = str(exc)
            produced_artifacts, log_path, report_paths = self._collect_artifact_tracking_info(registry, workspace, failure_summary)
            
            return JobRunResult(
                success=False,
                exit_code=1,
                message=str(exc),
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
                produced_artifacts=produced_artifacts,
                log_path=log_path,
                report_paths=report_paths,
                failure_summary=failure_summary,
                job_status="failed",
                job_disposition="unvalidated",
                canonical_ready=False,
                requires_attention=True,
                job_outcome_path=runtime_context.job_outcome_path,
            )

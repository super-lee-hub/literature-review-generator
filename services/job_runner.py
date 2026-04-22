from __future__ import annotations

import argparse
import json
import os
import glob
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, cast

from runtime.lifecycle import BootstrappedRuntimeContext, bootstrap_job_runtime, finalize_job_runtime
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json
from services.progress_state import determine_resume_state
from services.queue_service import CancelToken, JobCancelledError
from utils import sanitize_path_component


@dataclass(frozen=True)
class JobRunRequest:
    config: str
    project_name: Optional[str]
    pdf_folder: Optional[str]
    action: str
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

    return JobRunRequest(
        config=str(params.get("config", "config.ini")),
        project_name=cast(Optional[str], params.get("project_name", None)),
        pdf_folder=cast(Optional[str], params.get("pdf_folder", None)),
        action=action,
        summary_file=summary_file,
        summary_sources=tuple(summary_source_items),
        reuse_stage1=bool(params.get("reuse_stage1", False)),
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

    if summary_file and action not in {"generate_outline", "generate_review", "generate_section", "validate_review"}:
        return "--summary-file can only be used with generate-outline, generate-review, generate-section, or validate-review"
    if summary_sources and action not in {"generate_outline", "generate_review", "generate_section", "validate_review"}:
        return "--summary-source can only be used with generate-outline, generate-review, generate-section, or validate-review"
    if reuse_stage1 and action not in {"analyze", "run_all"}:
        return "--reuse-stage1 can only be used with stage1 analysis or --run-all"
    if reuse_summary_files and not reuse_stage1:
        return "--reuse-summary-file requires --reuse-stage1"
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

    def _collect_artifact_tracking_info(self, registry: ArtifactRegistry, workspace: JobWorkspace, failure_summary: Optional[str]) -> tuple[List[str], str, List[str]]:
        """收集产物追踪信息"""
        produced_artifacts = [record.path for record in registry.list_records() if record.status == "ready"]
        log_path = workspace.artifact_path("job.log")
        report_paths = []
        try:
            report_dir = workspace.paths.reports_dir
            if os.path.exists(report_dir):
                report_paths = [os.path.join(report_dir, f) for f in os.listdir(report_dir) if f.endswith(".txt") or f.endswith(".json")]
        except Exception:
            pass
        return produced_artifacts, log_path, report_paths

    def _request_snapshot(self, request: JobRunRequest) -> dict[str, Any]:
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
            "gui": bool(request.gui),
        }

    def _build_workspace(
        self,
        *,
        base_output_dir: str,
        project_name: str,
        pointer_payload: dict[str, Any] | None,
        fingerprint_bundle: dict[str, Any],
    ) -> JobWorkspace:
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
        
        # 策略2：查找最近的工作空间，即使不完全匹配
        import glob
        workspace_pattern = os.path.join(os.path.abspath(base_output_dir), f"{project_name}__*")
        workspaces = glob.glob(workspace_pattern)
        
        if workspaces:
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
        return JobWorkspace.create(base_output_dir=base_output_dir, project_name=project_name)

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
        workspace: JobWorkspace,
        registry: ArtifactRegistry,
        project_name: str,
        summary_path: str,
        progress_path: str,
        checkpoint_path: str,
        fingerprint_bundle: dict[str, Any],
        status: str,
    ) -> str:
        context = BootstrappedRuntimeContext(
            project_name=project_name,
            output_base_dir=os.path.dirname(os.path.dirname(workspace.root_dir)),
            pointer_path=os.path.join(os.path.dirname(workspace.root_dir), "_latest_job.json"),
            workspace=workspace,
            registry=registry,
            compat_view=None,  # type: ignore[arg-type]
            summary_path=summary_path,
            progress_path=progress_path,
            checkpoint_path=checkpoint_path,
            fingerprint_bundle=fingerprint_bundle,
            resume_report=None,
            resume_report_path="",
        )
        return finalize_job_runtime(
            context=context,
            write_resume_report=self._write_resume_report,
            status=status,
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

    def run(self, request: JobRunRequest, cancel_token: CancelToken | None = None) -> JobRunResult:
        import main as legacy_main

        project_name = self._resolve_project_name(request)
        workspace: JobWorkspace | None = None
        registry: ArtifactRegistry | None = None
        summary_path = ""
        progress_path = ""
        checkpoint_path = ""
        fingerprint_bundle_dict: dict[str, Any] = {}
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

        runtime_context = bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name=project_name,
            source_snapshot=self._source_snapshot(generator, request),
            request_snapshot=self._request_snapshot(request),
            build_workspace=self._build_workspace,
            write_resume_report=self._write_resume_report,
        )
        workspace = runtime_context.workspace
        registry = runtime_context.registry
        summary_path = runtime_context.summary_path
        progress_path = runtime_context.progress_path
        checkpoint_path = runtime_context.checkpoint_path
        fingerprint_bundle_dict = runtime_context.fingerprint_bundle

        try:
            active_cancel_token.check_cancelled()

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
                workspace=workspace,
                registry=registry,
                project_name=project_name,
                summary_path=summary_path,
                progress_path=progress_path,
                checkpoint_path=checkpoint_path,
                fingerprint_bundle=fingerprint_bundle_dict,
                status="completed" if success else "failed",
            )
            
            failure_summary = message if not success else None
            produced_artifacts, log_path, report_paths = self._collect_artifact_tracking_info(registry, workspace, failure_summary)
            
            return JobRunResult(
                success=success,
                exit_code=exit_code,
                message=message,
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
                produced_artifacts=produced_artifacts,
                log_path=log_path,
                report_paths=report_paths,
                failure_summary=failure_summary,
            )

        except JobCancelledError as exc:
            resume_state = self._finalize_run_state(
                workspace=workspace,
                registry=registry,
                project_name=project_name,
                summary_path=summary_path,
                progress_path=progress_path,
                checkpoint_path=checkpoint_path,
                fingerprint_bundle=fingerprint_bundle_dict,
                status="cancelled",
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
            )
        except Exception as exc:
            resume_state = self._finalize_run_state(
                workspace=workspace,
                registry=registry,
                project_name=project_name,
                summary_path=summary_path,
                progress_path=progress_path,
                checkpoint_path=checkpoint_path,
                fingerprint_bundle=fingerprint_bundle_dict,
                status="failed",
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
            )

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, MutableMapping, Optional, cast

from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_fingerprint import FingerprintInputs, build_fingerprint_bundle, sanitize_config_for_fingerprint
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
    run_all: bool = False
    analyze_only: bool = False
    generate_outline: bool = False
    generate_review: bool = False
    generate_section: Optional[int] = None
    validate_review: bool = False
    retry_review_failed: bool = False
    concept: Optional[str] = None
    free_mode_profile: Optional[str] = None
    free_mode_idea: Optional[str] = None
    progress_tracker: Optional[Any] = None
    gui: bool = False
    source_mode: str = "direct"
    zotero_report: Optional[str] = None
    library_path: Optional[str] = None


@dataclass(frozen=True)
class JobRunResult:
    success: bool
    exit_code: int
    message: str
    workspace_path: str
    job_id: str
    resume_state: str


def build_job_request_from_args(args: argparse.Namespace) -> JobRunRequest:
    action = "analyze"
    if getattr(args, "run_all", False):
        action = "run_all"
    elif getattr(args, "generate_outline", False):
        action = "generate_outline"
    elif getattr(args, "generate_section", None):
        action = "generate_section"
    elif getattr(args, "generate_review", False):
        action = "generate_review"
    elif getattr(args, "retry_review_failed", False):
        action = "retry_review_failed"
    elif getattr(args, "validate_review", False):
        action = "validate_review"

    return JobRunRequest(
        config=getattr(args, "config", "config.ini"),
        project_name=getattr(args, "project_name", None),
        pdf_folder=getattr(args, "pdf_folder", None),
        action=action,
        run_all=getattr(args, "run_all", False),
        analyze_only=getattr(args, "analyze_only", False),
        generate_outline=getattr(args, "generate_outline", False),
        generate_review=getattr(args, "generate_review", False),
        generate_section=getattr(args, "generate_section", None),
        validate_review=getattr(args, "validate_review", False),
        retry_review_failed=getattr(args, "retry_review_failed", False),
        concept=getattr(args, "concept", None),
        free_mode_profile=getattr(args, "free_mode_profile", None),
        free_mode_idea=getattr(args, "free_mode_idea", None),
        progress_tracker=getattr(args, "_progress_tracker", None),
        gui=getattr(args, "gui", False),
        source_mode=getattr(args, "source_mode", "direct"),
        zotero_report=getattr(args, "zotero_report", None),
        library_path=getattr(args, "library_path", None),
    )


class JobRunner:
    def _legacy_args_namespace(self, request: JobRunRequest, project_name: str) -> argparse.Namespace:
        return argparse.Namespace(
            config=request.config,
            project_name=request.project_name or project_name,
            pdf_folder=request.pdf_folder,
            run_all=request.run_all,
            analyze_only=request.analyze_only,
            generate_outline=request.generate_outline,
            generate_review=request.generate_review,
            generate_section=request.generate_section,
            validate_review=request.validate_review,
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
            "source_mode": "direct" if request.pdf_folder else "zotero",
            "pdf_folder": os.path.abspath(request.pdf_folder) if request.pdf_folder else "",
            "zotero_report": os.path.abspath(paths_config.get("zotero_report", "")) if paths_config.get("zotero_report") else "",
            "library_path": os.path.abspath(paths_config.get("library_path", "")) if paths_config.get("library_path") else "",
            "project_name": self._resolve_project_name(request),
        }

    def _request_snapshot(self, request: JobRunRequest) -> dict[str, Any]:
        return {
            "action": request.action,
            "project_name": request.project_name or "",
            "pdf_folder": os.path.abspath(request.pdf_folder) if request.pdf_folder else "",
            "generate_section": request.generate_section,
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
        final_resume_report = determine_resume_state(
            project_name=project_name,
            job_id=workspace.job_id,
            summary_file=summary_path,
            progress_snapshot_file=progress_path,
            checkpoint_file=checkpoint_path,
            expected_fingerprint_bundle=fingerprint_bundle,
        )
        final_resume_report_path = self._write_resume_report(workspace, final_resume_report)
        registry.register_file(
            artifact_role="resume",
            artifact_type="resume_state_report",
            artifact_version="v1",
            path=final_resume_report_path,
            producer="services.job_runner",
            artifact_id="resume_state_report",
        )
        workspace.write_latest_pointer(
            resume_state=final_resume_report.state,
            fingerprint_bundle=fingerprint_bundle,
            status=status,
        )
        return final_resume_report.state

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
            elif request.generate_review:
                result = legacy_main.handle_generate_review_mode(generator)
            elif request.retry_review_failed:
                result = legacy_main.handle_retry_review_failed_mode(generator)
            elif request.validate_review:
                if generator.load_existing_summaries():
                    result = legacy_main.validator.run_review_validation(generator)  # type: ignore[attr-defined]
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
            )

        generator = legacy_main.LiteratureReviewGenerator(request.config, project_name, request.pdf_folder)
        generator.cancel_token = active_cancel_token
        generator.progress_tracker = request.progress_tracker
        generator.free_mode_profile_path = request.free_mode_profile
        generator.free_mode_idea = request.free_mode_idea

        if not generator.load_configuration():
            return JobRunResult(
                success=False,
                exit_code=1,
                message="configuration load failed",
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
            )

        if generator.config is None:
            return JobRunResult(
                success=False,
                exit_code=1,
                message="configuration is unavailable after load",
                workspace_path="",
                job_id="",
                resume_state="non_resumable",
            )

        generator_config = cast(MutableMapping[str, Dict[str, str]], generator.config)
        compat_view = CompatConfigView.from_config(generator_config)
        output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")

        fingerprint_bundle = build_fingerprint_bundle(
            FingerprintInputs(
                config_snapshot=sanitize_config_for_fingerprint(generator_config),
                source_snapshot=self._source_snapshot(generator, request),
                request_snapshot=self._request_snapshot(request),
            )
        )
        fingerprint_bundle_dict = fingerprint_bundle.to_dict()

        pointer_path = os.path.join(os.path.abspath(output_base_dir), project_name, "_latest_job.json")
        workspace = self._build_workspace(
            base_output_dir=output_base_dir,
            project_name=project_name,
            pointer_payload=self._pointer_payload(pointer_path),
            fingerprint_bundle=fingerprint_bundle_dict,
        )
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)

        summary_path = workspace.artifact_path(f"{project_name}_summaries.json")
        progress_path = workspace.artifact_path("stage1_progress_snapshot.json")
        checkpoint_path = workspace.checkpoint_path(f"{project_name}_checkpoint.json")
        resume_report = determine_resume_state(
            project_name=project_name,
            job_id=workspace.job_id,
            summary_file=summary_path,
            progress_snapshot_file=progress_path,
            checkpoint_file=checkpoint_path,
            expected_fingerprint_bundle=fingerprint_bundle_dict,
        )

        resume_report_path = self._write_resume_report(workspace, resume_report)
        registry.register_file(
            artifact_role="resume",
            artifact_type="resume_state_report",
            artifact_version="v1",
            path=resume_report_path,
            producer="services.job_runner",
            artifact_id="resume_state_report",
        )

        generator.bind_job_workspace(
            workspace=workspace,
            artifact_registry=registry,
            compat_config=compat_view,
            fingerprint_bundle=fingerprint_bundle_dict,
            resume_state_report=resume_report,
        )

        workspace.write_latest_pointer(
            resume_state=resume_report.state,
            fingerprint_bundle=fingerprint_bundle_dict,
            status="running",
        )

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
            return JobRunResult(
                success=success,
                exit_code=exit_code,
                message=message,
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
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
            return JobRunResult(
                success=False,
                exit_code=130,
                message=str(exc),
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
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
            return JobRunResult(
                success=False,
                exit_code=1,
                message=str(exc),
                workspace_path=workspace.root_dir,
                job_id=workspace.job_id,
                resume_state=resume_state,
            )

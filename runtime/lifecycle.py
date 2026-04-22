from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Dict, Mapping, MutableMapping, cast

from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_fingerprint import FingerprintInputs, build_fingerprint_bundle, sanitize_config_for_fingerprint
from services.job_workspace import JobWorkspace
from services.progress_state import determine_resume_state


ResumeReportWriter = Callable[[JobWorkspace, Any], str]
WorkspaceBuilder = Callable[..., JobWorkspace]


@dataclass(frozen=True)
class BootstrappedRuntimeContext:
    project_name: str
    output_base_dir: str
    pointer_path: str
    workspace: JobWorkspace
    registry: ArtifactRegistry
    compat_view: CompatConfigView
    summary_path: str
    progress_path: str
    checkpoint_path: str
    fingerprint_bundle: Dict[str, Any]
    resume_report: Any
    resume_report_path: str


def bootstrap_job_runtime(
    *,
    request: Any,
    generator: Any,
    project_name: str,
    source_snapshot: Mapping[str, Any],
    request_snapshot: Mapping[str, Any],
    build_workspace: WorkspaceBuilder,
    write_resume_report: ResumeReportWriter,
) -> BootstrappedRuntimeContext:
    generator_config = cast(MutableMapping[str, Dict[str, str]], generator.config)
    compat_view = CompatConfigView.from_config(generator_config)
    output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")

    fingerprint_bundle = build_fingerprint_bundle(
        FingerprintInputs(
            config_snapshot=sanitize_config_for_fingerprint(generator_config),
            source_snapshot=dict(source_snapshot),
            request_snapshot=dict(request_snapshot),
        )
    )
    fingerprint_bundle_dict = fingerprint_bundle.to_dict()

    pointer_path = os.path.join(os.path.abspath(output_base_dir), project_name, "_latest_job.json")
    workspace = build_workspace(
        base_output_dir=output_base_dir,
        project_name=project_name,
        pointer_payload=_load_pointer_payload(pointer_path),
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

    resume_report_path = write_resume_report(workspace, resume_report)
    registry.register_file(
        artifact_role="resume",
        artifact_type="resume_state_report",
        artifact_version="v1",
        path=resume_report_path,
        producer="runtime.lifecycle.bootstrap_job_runtime",
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

    return BootstrappedRuntimeContext(
        project_name=project_name,
        output_base_dir=output_base_dir,
        pointer_path=pointer_path,
        workspace=workspace,
        registry=registry,
        compat_view=compat_view,
        summary_path=summary_path,
        progress_path=progress_path,
        checkpoint_path=checkpoint_path,
        fingerprint_bundle=fingerprint_bundle_dict,
        resume_report=resume_report,
        resume_report_path=resume_report_path,
    )


def finalize_job_runtime(
    *,
    context: BootstrappedRuntimeContext,
    write_resume_report: ResumeReportWriter,
    status: str,
) -> str:
    final_resume_report = determine_resume_state(
        project_name=context.project_name,
        job_id=context.workspace.job_id,
        summary_file=context.summary_path,
        progress_snapshot_file=context.progress_path,
        checkpoint_file=context.checkpoint_path,
        expected_fingerprint_bundle=context.fingerprint_bundle,
    )
    final_resume_report_path = write_resume_report(context.workspace, final_resume_report)
    context.registry.register_file(
        artifact_role="resume",
        artifact_type="resume_state_report",
        artifact_version="v1",
        path=final_resume_report_path,
        producer="runtime.lifecycle.finalize_job_runtime",
        artifact_id="resume_state_report",
    )
    context.workspace.write_latest_pointer(
        resume_state=final_resume_report.state,
        fingerprint_bundle=context.fingerprint_bundle,
        status=status,
    )
    return final_resume_report.state


def _load_pointer_payload(pointer_path: str) -> Dict[str, Any] | None:
    if not os.path.exists(pointer_path):
        return None
    try:
        with open(pointer_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


import json  # noqa: E402  # keep local to avoid import-order churn in patched file

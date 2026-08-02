from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Dict, Mapping, MutableMapping, Sequence, cast

from services.artifact_registry import ArtifactRegistry
from services.job_fingerprint import FingerprintInputs, build_fingerprint_bundle, sanitize_config_for_fingerprint
from services.job_outcome import JobDisposition, JobOutcomeV1, JobStatus
from services.job_workspace import JobWorkspace, atomic_write_json
from services.progress_state import determine_resume_state
from services.settings import ApplicationSettings
from services.source_inventory import SourceInventoryV1


ResumeReportWriter = Callable[[JobWorkspace, Any], str]
WorkspaceBuilder = Callable[..., JobWorkspace]
ResumePreflight = Callable[[JobWorkspace], None]


@dataclass(frozen=True)
class BootstrappedRuntimeContext:
    project_name: str
    output_base_dir: str
    pointer_path: str
    workspace: JobWorkspace
    registry: ArtifactRegistry
    settings: ApplicationSettings
    summary_path: str
    progress_path: str
    checkpoint_path: str
    fingerprint_bundle: Dict[str, Any]
    resume_report: Any
    resume_report_path: str
    source_inventory: Dict[str, Any]
    source_inventory_path: str
    source_canonical_ready: bool
    source_degradation_reasons: tuple[str, ...]
    readiness_policy_snapshot: Dict[str, Any]
    required_stages: tuple[str, ...]
    job_outcome_path: str
    attempt_number: int = 1
    resumed_from_attempt: int | None = None


def _required_stages_for_request(
    request: Any,
    *,
    validation_required: bool,
) -> tuple[str, ...]:
    requested_stages = getattr(request, "requested_stages", None)
    action = str(getattr(request, "action", "analyze") or "analyze")
    if action == "derive_review_batch":
        return ("source_intake", "derive_review_batch")
    if requested_stages is not None:
        stages = tuple(dict.fromkeys((
            "source_intake",
            *(str(item) for item in requested_stages if str(item) != "source_intake"),
        )))
        return tuple(
            stage for stage in stages if validation_required or stage != "validate"
        )
    mapping = {
        "analyze": ("source_intake", "analyze"),
        "derive_review_batch": ("source_intake", "derive_review_batch"),
        "retry_failed": ("source_intake", "analyze"),
        "generate_outline": ("source_intake", "outline"),
        "generate_review": ("source_intake", "outline", "review"),
        "generate_section": ("source_intake", "outline", "review"),
        "retry_review_failed": ("source_intake", "outline", "review"),
        "validate_review": ("source_intake", "validate"),
        "run_all": ("source_intake", "analyze", "outline", "review"),
    }
    stages = mapping.get(action, ("source_intake", action))
    return tuple(stage for stage in stages if validation_required or stage != "validate")


def _readiness_policy_for_request(request: Any) -> dict[str, Any]:
    def configured_bool(field_name: str) -> bool | None:
        value = getattr(request, field_name, None)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} must be a boolean")
        return value

    action = str(getattr(request, "action", "analyze") or "analyze")
    requested_stages = getattr(request, "requested_stages", None)
    default_validation_required = (
        "validate" in requested_stages
        if requested_stages is not None
        else action == "validate_review"
    )
    configured_validation_required = configured_bool("validation_required")
    validation_required = (
        default_validation_required
        if configured_validation_required is None
        else configured_validation_required
    )
    configured_require_clean = configured_bool("require_clean_validation")
    require_clean_validation = (
        validation_required if configured_require_clean is None else configured_require_clean
    )
    configured_allow_unvalidated = configured_bool("allow_unvalidated_when_validation_optional")
    allow_unvalidated = (
        not validation_required
        if configured_allow_unvalidated is None
        else configured_allow_unvalidated
    )
    return {
        "validation_required": validation_required,
        "require_clean_validation": require_clean_validation,
        "source_identity_required": str(getattr(request, "source_mode", "direct")) == "zotero",
        "allow_unvalidated_when_validation_optional": allow_unvalidated,
    }


def _coerce_inventory(
    source_inventory: SourceInventoryV1 | Mapping[str, Any] | None,
) -> SourceInventoryV1 | None:
    if source_inventory is None:
        return None
    if isinstance(source_inventory, SourceInventoryV1):
        return source_inventory
    return SourceInventoryV1.from_dict(source_inventory)


def bootstrap_job_runtime(
    *,
    request: Any,
    generator: Any,
    project_name: str,
    source_snapshot: Mapping[str, Any],
    request_snapshot: Mapping[str, Any],
    build_workspace: WorkspaceBuilder,
    write_resume_report: ResumeReportWriter,
    source_inventory: SourceInventoryV1 | Mapping[str, Any] | None = None,
    source_canonical_ready: bool | None = None,
    source_degradation_reasons: Sequence[str] = (),
    claim_latest_pointer: bool = True,
    resume_requested: bool = False,
    resume_preflight: ResumePreflight | None = None,
    publish_running_state: bool = True,
) -> BootstrappedRuntimeContext:
    generator_config = cast(MutableMapping[str, Dict[str, str]], generator.config)
    settings = ApplicationSettings.from_config(generator_config)
    output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")

    inventory = _coerce_inventory(source_inventory)
    inventory_payload = inventory.to_dict() if inventory is not None else {}
    fingerprint_source_snapshot = (
        {
            "source_inventory_hash": inventory.fingerprint(),
            "source_inventory": inventory.fingerprint_payload(),
        }
        if inventory is not None
        else {
            "compatibility_status": "legacy_unverified",
            "legacy_source_snapshot": dict(source_snapshot),
        }
    )
    fingerprint_bundle = build_fingerprint_bundle(
        FingerprintInputs(
            config_snapshot=sanitize_config_for_fingerprint(generator_config),
            source_snapshot=fingerprint_source_snapshot,
            request_snapshot=dict(request_snapshot),
        )
    )
    fingerprint_bundle_dict = fingerprint_bundle.to_dict()

    requested_job_id = str(getattr(request, "job_id", "") or "")
    expected_workspace = ""
    if requested_job_id:
        expected_workspace = os.path.join(
            os.path.abspath(output_base_dir),
            f"{project_name}__{requested_job_id}",
        )
    if resume_requested:
        if not requested_job_id:
            raise RuntimeError("resume requires an explicit job_id")
        if not os.path.isdir(expected_workspace):
            raise RuntimeError(f"resume workspace does not exist: {expected_workspace}")
    elif expected_workspace:
        os.makedirs(os.path.dirname(expected_workspace), exist_ok=True)
        try:
            os.mkdir(expected_workspace)
        except FileExistsError as exc:
            raise RuntimeError(
                f"new run workspace already exists: {expected_workspace}; use resume instead"
            ) from exc

    pointer_path = os.path.join(os.path.abspath(output_base_dir), project_name, "_latest_job.json")
    workspace = build_workspace(
        base_output_dir=output_base_dir,
        project_name=project_name,
        pointer_payload=_load_pointer_payload(pointer_path),
        fingerprint_bundle=fingerprint_bundle_dict,
        request=request,
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

    if resume_requested:
        persisted_report_path = workspace.artifact_path("resume_state_report.json")
        try:
            with open(persisted_report_path, "r", encoding="utf-8") as handle:
                persisted_report = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"resume state report is unavailable: {persisted_report_path}") from exc
        if not isinstance(persisted_report, Mapping):
            raise RuntimeError("resume state report must be a JSON object")
        if str(persisted_report.get("job_id") or "") != workspace.job_id:
            raise RuntimeError("resume state report belongs to another job")
        if persisted_report.get("fingerprint_bundle") != fingerprint_bundle_dict:
            raise RuntimeError("resume fingerprint does not match the persisted job inputs")
        if resume_preflight is not None:
            resume_preflight(workspace)

    readiness_policy_snapshot = _readiness_policy_for_request(request)
    required_stages = _required_stages_for_request(
        request,
        validation_required=bool(readiness_policy_snapshot["validation_required"]),
    )

    effective_source_ready = bool(source_canonical_ready) if source_canonical_ready is not None else inventory is not None
    source_inventory_path = ""
    if inventory is not None:
        source_inventory_path = workspace.artifact_path("source_inventory_v1.json")
        atomic_write_json(source_inventory_path, inventory_payload)
        registry.register_file(
            artifact_role="source_inventory",
            artifact_type="source_inventory",
            artifact_version="v1",
            path=source_inventory_path,
            producer="runtime.lifecycle.bootstrap_job_runtime",
            artifact_id="source_inventory",
            status="ready" if effective_source_ready else "quarantined",
            metadata={
                "inventory_hash": inventory.fingerprint(),
                "canonical_ready": effective_source_ready,
            },
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
        settings=settings,
        fingerprint_bundle=fingerprint_bundle_dict,
        resume_state_report=resume_report,
    )

    job_outcome_path = workspace.artifact_path("job_outcome_v1.json")
    context = BootstrappedRuntimeContext(
        project_name=project_name,
        output_base_dir=output_base_dir,
        pointer_path=pointer_path,
        workspace=workspace,
        registry=registry,
        settings=settings,
        summary_path=summary_path,
        progress_path=progress_path,
        checkpoint_path=checkpoint_path,
        fingerprint_bundle=fingerprint_bundle_dict,
        resume_report=resume_report,
        resume_report_path=resume_report_path,
        source_inventory=inventory_payload,
        source_inventory_path=source_inventory_path,
        source_canonical_ready=effective_source_ready,
        source_degradation_reasons=tuple(str(item) for item in source_degradation_reasons if str(item)),
        readiness_policy_snapshot=readiness_policy_snapshot,
        required_stages=required_stages,
        job_outcome_path=job_outcome_path,
    )
    if publish_running_state:
        publish_running_job_runtime(
            context,
            claim_latest_pointer=claim_latest_pointer,
        )
    return context


def publish_running_job_runtime(
    context: BootstrappedRuntimeContext,
    *,
    claim_latest_pointer: bool,
) -> JobOutcomeV1:
    running_outcome = JobOutcomeV1.create(
        job_id=context.workspace.job_id,
        attempt_number=context.attempt_number,
        resumed_from_attempt=context.resumed_from_attempt,
        job_status="running",
        job_disposition="unvalidated",
        canonical_ready=False,
        requires_attention=not context.source_canonical_ready,
        readiness_policy_snapshot=context.readiness_policy_snapshot,
        required_stages=context.required_stages,
        completed_stages=("source_intake",) if context.source_canonical_ready else (),
        degradation_reasons=context.source_degradation_reasons,
    )
    atomic_write_json(context.job_outcome_path, running_outcome.to_dict())
    context.registry.register_file(
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=context.job_outcome_path,
        producer="runtime.lifecycle.publish_running_job_runtime",
        artifact_id="job_outcome",
        metadata={
            "job_status": running_outcome.job_status,
            "job_disposition": running_outcome.job_disposition,
            "canonical_ready": running_outcome.canonical_ready,
            "outcome_revision": running_outcome.outcome_revision,
        },
    )
    pointer_writer = (
        context.workspace.write_latest_pointer
        if claim_latest_pointer
        else context.workspace.write_latest_pointer_if_owned
    )
    pointer_writer(
        resume_state=context.resume_report.state,
        fingerprint_bundle=context.fingerprint_bundle,
        status="running",
    )
    return running_outcome


def finalize_job_runtime(
    *,
    context: BootstrappedRuntimeContext,
    write_resume_report: ResumeReportWriter,
    status: str,
    job_disposition: JobDisposition | None = None,
    canonical_ready: bool | None = None,
    requires_attention: bool | None = None,
    completed_stages: Sequence[str] = (),
    failed_stage: str | None = None,
    degradation_reasons: Sequence[str] = (),
    before_latest_pointer: Callable[[JobOutcomeV1], None] | None = None,
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
    if status not in {"pending", "running", "completed", "failed", "cancelled"}:
        raise ValueError(f"unsupported job status: {status}")
    typed_status = cast(JobStatus, status)
    validation_required = bool(context.readiness_policy_snapshot.get("validation_required", False))
    identity_requires_review = any(
        reason.startswith("source_identity_") or reason == "ambiguous_pdf_match"
        for reason in context.source_degradation_reasons
    )
    effective_disposition: JobDisposition = job_disposition or (
        "needs_review" if identity_requires_review else "unvalidated"
    )
    if canonical_ready is None:
        effective_ready = bool(
            typed_status == "completed"
            and context.source_canonical_ready
            and (not validation_required)
            and effective_disposition != "needs_review"
        )
    else:
        effective_ready = bool(canonical_ready)
    effective_attention = (
        bool(requires_attention)
        if requires_attention is not None
        else effective_disposition in {"findings", "needs_review"} or typed_status in {"failed", "cancelled"}
    )
    merged_reasons = tuple(dict.fromkeys([
        *context.source_degradation_reasons,
        *(str(item) for item in degradation_reasons if str(item)),
    ]))
    completed = tuple(dict.fromkeys(str(item) for item in completed_stages if str(item)))
    if effective_ready and not set(context.required_stages).issubset(completed):
        effective_ready = False
        effective_attention = True
    outcome = JobOutcomeV1.create(
        job_id=context.workspace.job_id,
        attempt_number=context.attempt_number,
        resumed_from_attempt=context.resumed_from_attempt,
        job_status=typed_status,
        job_disposition=effective_disposition,
        canonical_ready=effective_ready,
        requires_attention=effective_attention,
        readiness_policy_snapshot=context.readiness_policy_snapshot,
        required_stages=context.required_stages,
        completed_stages=completed,
        failed_stage=failed_stage,
        degradation_reasons=merged_reasons,
        outcome_revision=2,
    )
    atomic_write_json(context.job_outcome_path, outcome.to_dict())
    context.registry.register_file(
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=context.job_outcome_path,
        producer="runtime.lifecycle.finalize_job_runtime",
        artifact_id="job_outcome",
        metadata={
            "job_status": outcome.job_status,
            "job_disposition": outcome.job_disposition,
            "canonical_ready": outcome.canonical_ready,
            "outcome_revision": outcome.outcome_revision,
        },
    )
    if before_latest_pointer is not None:
        before_latest_pointer(outcome)
    context.workspace.write_latest_pointer_if_owned(
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

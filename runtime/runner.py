from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from free_mode.intent_input import (
    build_free_mode_intent_envelope,
    build_free_mode_writer_context,
)
from runtime.attempt_store import (
    ATTEMPT_SNAPSHOT_ROLE,
    AttemptAlreadyRunningError,
    AttemptExecutionLease,
    AttemptStore,
)
from runtime.completion_evaluator import CanonicalCompletionEvaluator, CompletionEvaluationV1
from runtime.job_spec import RuntimeJobSpec
from runtime.lifecycle import finalize_job_runtime
from runtime.lifecycle import publish_running_job_runtime
from runtime.orchestrator import AgentRuntimeBridge, AgentRuntimeSession
from runtime.reconcile import (
    ReconcileValidationError,
    ReconcileResult,
    RuntimeReconciler,
    validate_review_batch_manifest_for_bootstrap,
)
from runtime.stage_contracts import SourceBundle, StageArtifactRef, StageResult
from runtime.source_intake import stage1_paper_work_item_basis
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    CurrentArtifactSetV1,
    RegistryError,
    file_sha256,
)
from services.job_outcome import JobOutcomeV1, load_canonical_job_outcome
from services.job_workspace import JobWorkspace, publish_json_artifact, utc_now_iso
from services.queue_service import JobCancelledError
from services.queue_service import CancelToken
from runtime.provider_runtime import ProviderCancellationRequested, hash_json


class RuntimeRunnerError(RuntimeError):
    pass


class RuntimePathOriginError(RuntimeRunnerError):
    pass


FaultInjector = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class RuntimeExecutionResult:
    job_id: str
    workspace_path: str
    job_status: str
    job_disposition: str
    canonical_ready: bool
    requires_attention: bool
    attempt_number: int
    resumed_from_attempt: int | None
    completed_stages: tuple[str, ...]
    failed_stage: str | None
    job_outcome_path: str
    message: str = ""
    completion_status: str = ""
    completion_reasons: tuple[str, ...] = ()
    completion_evidence_hash: str = ""
    summary_schema_ready: bool = False
    visual_qualification_ready: bool = False
    stage1_authority_ready: bool = False
    stage1_reuse_eligible: bool = False

    @property
    def success(self) -> bool:
        """Return whether the canonical outcome is ready for downstream use."""

        return self.canonical_ready


def _evaluate_runtime_completion(
    outcome: JobOutcomeV1,
    registry: ArtifactRegistry,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> CompletionEvaluationV1:
    """Read and verify the durable evidence used by status projections."""

    registry_verified = True
    ready_job_outcome = False
    validation_record = False
    required_provider_stages = {"analyze", "outline", "review", "validate"}.intersection(
        outcome.required_stages
    )
    provider_receipts_complete = not required_provider_stages
    provider_receipt_closure: Mapping[str, Any] | None = None
    current_stage_closure_map: Mapping[str, Any] | None = None
    try:
        for record in registry.list_records():
            if record.status != "ready":
                continue
            # The workspace log is intentionally append-only operational
            # output: its registered hash is the creation hash, not a frozen
            # canonical artifact hash.  It must not make an otherwise valid
            # completion fail merely because later lifecycle messages were
            # appended.
            if record.artifact_role == "log" or record.artifact_type == "job_log":
                continue
            ArtifactRegistry._verify_ready_artifact(record)
            ready_job_outcome = ready_job_outcome or record.artifact_id == "job_outcome"
            validation_record = validation_record or record.artifact_type == "validation_run_result"
        from validation.closure import resolve_current_stage_closure_map

        current_stage_closure_map = resolve_current_stage_closure_map(registry, external_registry_resolver=external_registry_resolver).to_dict()
        provider_entries = current_stage_closure_map.get("provider_closures_by_stage")
        if required_provider_stages:
            if not isinstance(provider_entries, Mapping):
                provider_receipts_complete = False
            else:
                provider_receipts_complete = all(
                    isinstance(provider_entries.get(stage), Mapping)
                    and bool(provider_entries[stage].get("complete"))
                    for stage in required_provider_stages
                )
        if isinstance(provider_entries, Mapping):
            # Keep the stage-indexed aggregate as the completion source.  The
            # validation closure is one entry in that map, not a replacement
            # for analyze/outline/review evidence.
            provider_receipt_closure = {
                "complete": provider_receipts_complete,
                "stages": {
                    str(stage): dict(value)
                    for stage, value in provider_entries.items()
                    if isinstance(value, Mapping)
                },
            }
        current_receipt_ref = (current_stage_closure_map.get("stages") or {}).get(
            "validation_receipt_closure"
        )
        current_receipt_id = str(
            current_receipt_ref.get("artifact_id") if isinstance(current_receipt_ref, Mapping) else ""
        )
        current_receipt = registry.get(current_receipt_id) if current_receipt_id else None
        if current_receipt is not None:
            closure_envelope = json.loads(Path(current_receipt.path).read_text(encoding="utf-8"))
            candidate = closure_envelope.get("payload") if isinstance(closure_envelope, Mapping) else None
            if isinstance(candidate, Mapping):
                if isinstance(provider_receipt_closure, Mapping):
                    provider_receipt_closure = {
                        **dict(provider_receipt_closure),
                        "validation": dict(candidate),
                    }
                else:
                    provider_receipt_closure = {
                        "complete": provider_receipts_complete,
                        "validation": dict(candidate),
                    }
            else:
                if "validate" in required_provider_stages:
                    provider_receipts_complete = False
        elif "validate" in required_provider_stages:
            provider_receipts_complete = False
    except (OSError, RegistryError, TypeError, ValueError):
        registry_verified = False
        provider_receipts_complete = False

    policy = dict(outcome.readiness_policy_snapshot)
    validation_status = "clean" if outcome.job_disposition == "clean" and validation_record else (
        "findings" if outcome.job_disposition == "findings" else "missing"
    )
    try:
        current_set = registry.resolve_current_artifact_set()
    except (OSError, RegistryError, TypeError, ValueError):
        current_set = None
    if current_set is not None and current_set.validation_status == "not_requested":
        validation_status = "not_requested"
    elif current_set is not None and current_set.validation_status == "findings" and validation_record:
        validation_status = "findings"
    return CanonicalCompletionEvaluator.evaluate(
        {
            "job_id": outcome.job_id,
            "job_status": outcome.job_status,
            "required_stages": outcome.required_stages,
            "completed_stages": outcome.completed_stages,
            "failed_stage": outcome.failed_stage,
            "artifact_registry_verified": registry_verified,
            "canonical_artifacts": {"job_outcome": ready_job_outcome},
            "validation_required": bool(policy.get("validation_required", False)),
            "require_clean_validation": bool(policy.get("require_clean_validation", False)),
            "allow_unvalidated_when_validation_optional": bool(
                policy.get("allow_unvalidated_when_validation_optional", False)
            ),
            "validation_status": validation_status,
            "provider_receipts_complete": provider_receipts_complete,
            "provider_receipt_closure": provider_receipt_closure,
            "current_stage_closure_map": current_stage_closure_map,
            "declared_canonical_ready": outcome.canonical_ready,
            "degradation_reasons": outcome.degradation_reasons,
            "evidence_sources": ("job_outcome_v1", "artifact_registry"),
        }
    )


def _stage1_status_projection(registry: ArtifactRegistry) -> dict[str, bool]:
    """Project Stage 1 evidence states without redefining job completion."""

    from services.stage1_reuse import Stage1VisualEvidenceQualificationV1
    from summary_schema import is_canonical_ai_summary

    records = [record for record in registry.list_records() if record.status == "ready"]

    def latest(artifact_type: str) -> ArtifactRecord | None:
        candidates = [record for record in records if record.artifact_type == artifact_type]
        return max(candidates, key=lambda record: (record.created_at, record.artifact_id), default=None)

    def read_json(record: ArtifactRecord | None) -> Mapping[str, Any] | list[Any] | None:
        if record is None:
            return None
        try:
            payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, (Mapping, list)) else None

    summary_schema_ready = False
    for record in (
        latest("paper_artifact"),
        latest("stage1_reusable_summary_manifest"),
        latest("summary_file"),
    ):
        payload = read_json(record)
        candidates: list[Any] = []
        if isinstance(payload, Mapping):
            candidates.extend(
                (
                    payload.get("summary_payload"),
                    payload.get("ai_summary"),
                    (payload.get("analysis") or {}).get("ai_summary")
                    if isinstance(payload.get("analysis"), Mapping)
                    else None,
                )
            )
        elif isinstance(payload, list):
            candidates.extend(
                item.get("ai_summary")
                for item in payload
                if isinstance(item, Mapping)
            )
        for candidate in candidates:
            if isinstance(candidate, Mapping) and is_canonical_ai_summary(candidate):
                summary_schema_ready = True
                break
        if summary_schema_ready:
            break

    qualification: Mapping[str, Any] | None = None
    for record in (
        latest("stage1_reusable_summary_manifest"),
        latest("paper_artifact"),
    ):
        payload = read_json(record)
        if not isinstance(payload, Mapping):
            continue
        candidate = payload.get("visual_evidence_qualification")
        if not isinstance(candidate, Mapping) and isinstance(payload.get("binding"), Mapping):
            candidate = payload["binding"].get("visual_evidence_qualification")
        if not isinstance(candidate, Mapping) and isinstance(payload.get("stage1_reuse"), Mapping):
            binding = payload["stage1_reuse"].get("binding")
            if isinstance(binding, Mapping):
                candidate = binding.get("visual_evidence_qualification")
        if isinstance(candidate, Mapping) and candidate:
            qualification = candidate
            break

    visual_qualification_ready = False
    if qualification is not None:
        try:
            visual_qualification_ready = (
                Stage1VisualEvidenceQualificationV1.from_current_mapping_strict(
                    qualification
                ).complete_for_reuse()
            )
        except (TypeError, ValueError):
            visual_qualification_ready = False
    else:
        has_visual_artifacts = any(
            record.artifact_type in {
                "page_snapshot",
                "figure_crop",
                "table_crop",
                "formula_crop",
                "stage1_visual_observations",
                "stage1_visual_evidence",
                "stage1_visual_coverage",
            }
            for record in records
        )
        visual_qualification_ready = not has_visual_artifacts

    closure_complete = False
    closure_payload = read_json(latest("provider_receipt_closure"))
    if isinstance(closure_payload, Mapping):
        closure = closure_payload.get("payload")
        closure_complete = isinstance(closure, Mapping) and closure.get("complete") is True

    analyze_terminal_succeeded = False
    for record in records:
        if record.artifact_type != "runtime_stage_terminal":
            continue
        payload = read_json(record)
        if (
            isinstance(payload, Mapping)
            and payload.get("stage_name") == "analyze"
            and payload.get("status") == "succeeded"
        ):
            analyze_terminal_succeeded = True
            break

    stage1_authority_ready = bool(
        summary_schema_ready
        and visual_qualification_ready
        and closure_complete
        and analyze_terminal_succeeded
    )
    return {
        "summary_schema_ready": summary_schema_ready,
        "visual_qualification_ready": visual_qualification_ready,
        "stage1_authority_ready": stage1_authority_ready,
        "stage1_reuse_eligible": stage1_authority_ready,
    }


def _acceptance_budget_state_path(workspace: Any) -> Path:
    """Resolve the parent budget path in-process or from a child environment."""

    from runtime.provider_runtime import (
        acceptance_execution_context_from_environment,
        current_acceptance_execution_context,
    )

    context = (
        current_acceptance_execution_context()
        or acceptance_execution_context_from_environment()
    )
    if context is not None:
        return Path(context.provider_budget_state_path)
    return Path(workspace.log_path("acceptance_budget_state_v1.json"))


class AgentRuntimeRunner:
    """Single AI-native state machine layered on the internal stage registry."""

    _OUTLINE_V3_ARTIFACT_TYPES = frozenset(
        {
            "outline_artifact",
            "relation_adjudication_result",
            "confirmed_global_relation_map",
            "outline_candidate",
            "structure_critique",
            "coverage_critique",
            "evidence_critique",
            "arbitration_decision",
            "selected_outline_candidate",
            "section_evidence_packet_set",
            "final_outline",
            "coverage_audit",
            "stability_audit",
            "provider_receipt_closure",
            "outline_stage_health",
            "adopted_outline",
            "stage1_canonical_summaries",
        }
    )

    @classmethod
    def _schema_validators(cls) -> dict[str, Callable[[ArtifactRecord, Path], None]]:
        from runtime.reconcile import DEFAULT_SCHEMA_VALIDATORS
        from runtime.artifact_validators import make_outline_schema_validators
        from runtime.downstream_continuation import validate_continuation_artifact

        validators = dict(DEFAULT_SCHEMA_VALIDATORS)
        for artifact_type in ("downstream_continuation_grant", "downstream_continuation_pointer"):
            validators[artifact_type] = validate_continuation_artifact
        for artifact_type, current_validator in make_outline_schema_validators().items():
            fallback_validator = validators.get(artifact_type)

            def dispatch(
                record: ArtifactRecord,
                path: Path,
                *,
                current_validator: Callable[[ArtifactRecord, Path], None] = current_validator,
                fallback_validator: Callable[[ArtifactRecord, Path], None] | None = fallback_validator,
            ) -> None:
                if record.artifact_version == "v3":
                    current_validator(record, path)
                elif fallback_validator is not None:
                    fallback_validator(record, path)
                else:
                    raise ValueError(f"no validator is registered for artifact type {record.artifact_type!r}")

            validators[artifact_type] = dispatch
        return validators

    def __init__(
        self,
        job_spec: RuntimeJobSpec,
        *,
        origin_dir: str | Path | None = None,
        fault_injector: FaultInjector | None = None,
        cancel_token: CancelToken | None = None,
        publication_context: Any | None = None,
        queue_resume_intent: Mapping[str, Any] | None = None,
    ) -> None:
        if queue_resume_intent is not None:
            from services.queue_service import QueuePublicationContext

            if not isinstance(publication_context, QueuePublicationContext):
                raise RuntimeRunnerError("queue resume intent requires a lease-bound publication context")
            lease = publication_context.lease
            if job_spec.job_id != lease.job_id or not publication_context.queue_service.lease_is_current(
                lease.job_id, lease_id=lease.lease_id, worker_id=lease.worker_id,
                lease_generation=lease.lease_generation, fence_token=lease.fence_token,
            ):
                raise RuntimeRunnerError("queue resume intent requires the current job lease")
        from runtime.provider_runtime import provider_budget_controller_from_environment

        provider_budget_controller_from_environment()
        if origin_dir is not None:
            resolved = job_spec.resolved_from(origin_dir)
        else:
            resolved = job_spec
            # ``queue_file`` has a historical relative default, while direct
            # RuntimeJobSpec callers commonly provide an absolute config and
            # source path without a spec-file origin.  Canonicalize only that
            # implicit default against the explicit config origin; user-supplied
            # relative paths remain rejected below instead of silently binding
            # to the process CWD.
            default_queue_file = str(
                RuntimeJobSpec.__dataclass_fields__["queue_file"].default
            )
            if (
                resolved.queue_file == default_queue_file
                and Path(resolved.config).expanduser().is_absolute()
            ):
                resolved = replace(
                    resolved,
                    queue_file=str(
                        Path(resolved.config).expanduser().parent / default_queue_file
                    ),
                )
        self._require_explicit_path_origins(resolved)
        resolved.validate()
        self.job_spec = resolved
        self.fault_injector = fault_injector
        self.cancel_token = cancel_token
        self.publication_context = publication_context
        self.queue_resume_intent = dict(queue_resume_intent) if queue_resume_intent is not None else None

    def _apply_queue_resume_intent(self, session: AgentRuntimeSession) -> None:
        intent = self.queue_resume_intent
        if intent is None:
            return
        from services.queue_service import QueueOwnedArtifactRegistry

        if not isinstance(session.context.registry, QueueOwnedArtifactRegistry):
            raise RuntimeRunnerError("queue resume requires the lease-bound Registry")
        expected_fields = {
            "schema_version", "job_id", "workspace_path", "execution_fingerprint",
            "runtime_spec_sha256", "expected_cancel_marker_sha256",
        }
        if (
            set(intent) != expected_fields
            or intent.get("schema_version") != "queue-resume-intent-v1"
            or intent.get("job_id") != session.context.workspace.job_id
            or intent.get("workspace_path") != str(Path(session.context.workspace.root_dir).resolve())
            or intent.get("runtime_spec_sha256") != hash_json(self.job_spec.to_dict())
            or not isinstance(intent.get("execution_fingerprint"), str)
            or len(intent["execution_fingerprint"]) != 64
        ):
            raise RuntimeRunnerError("queue resume intent does not match the persisted runtime identity")
        if self.cancel_token is not None:
            self.cancel_token.check_cancelled()
        from runtime.cancellation import CancellationRequestError, CancellationRequestStore

        store = CancellationRequestStore(session.context.workspace, session.context.registry)
        try:
            store.clear(
                cleared_by="queue_runner", reason="explicit_queue_retry",
                expected_hash=intent["expected_cancel_marker_sha256"],
            )
        except CancellationRequestError as exc:
            if store.is_requested():
                raise JobCancelledError("new cancellation preserved during queue retry") from exc
            raise RuntimeRunnerError(f"queue retry cancellation state is invalid: {exc}") from exc

    @staticmethod
    def _require_explicit_path_origins(spec: RuntimeJobSpec) -> None:
        values = [
            spec.config,
            spec.queue_file,
            spec.free_mode_profile,
            spec.source.pdf_folder,
            spec.source.zotero_report,
            spec.source.library_path,
            spec.summary_file,
            *spec.summary_sources,
            *spec.reuse_summary_files,
        ]
        review_batch_spec = spec.metadata.get("review_batch_spec")
        if isinstance(review_batch_spec, (str, Path)):
            values.append(str(review_batch_spec))
        relative = [value for value in values if value and not Path(value).expanduser().is_absolute()]
        if relative:
            raise RuntimePathOriginError(
                "relative RuntimeJobSpec paths require an explicit origin_dir: " + ", ".join(relative)
            )

    def _fault(self, point: str, **context: Any) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point, context)

    def _normalized_spec(self, *, resume: bool) -> RuntimeJobSpec:
        if resume and not self.job_spec.job_id:
            raise RuntimeRunnerError("resume requires an explicit job_id")
        resolved_job_id = self.job_spec.job_id or JobWorkspace.generate_job_id()
        metadata = dict(self.job_spec.metadata)
        requested_stages = metadata.get("requested_stages")
        from config_loader import load_config
        from services.settings import ApplicationSettings
        from runtime.provider_routes import build_reachable_provider_route_plan
        from runtime.stage_planning import StagePlanError, build_stage_plan
        from runtime.test_dependencies import current_runtime_test_dependencies
        from runtime.trust_admission import (
            ExternalHostAdmissionError,
            build_runtime_external_host_policy,
            validate_external_host_acknowledgement,
        )

        try:
            free_mode_enabled = bool(
                self.job_spec.free_mode_profile
                or self.job_spec.free_mode_idea
                or metadata.get("free_mode_input")
            )
            test_dependencies = current_runtime_test_dependencies()
            if test_dependencies is not None:
                test_dependencies.validate()
            explicit_scope = None
            if metadata.get("stage1_summary_input_mode") == "explicit_typed_import":
                from runtime.explicit_stage1_scope import load_explicit_stage1_scope

                explicit_scope = load_explicit_stage1_scope(
                    self.job_spec,
                    allow_template_credentials=bool(test_dependencies and test_dependencies.allow_template_credentials),
                )
            resolved_config = explicit_scope.config if explicit_scope is not None else load_config(
                self.job_spec.config,
                action=self.job_spec.action,
                requested_stages=requested_stages,
                free_mode_enabled=free_mode_enabled,
                allow_template_credentials=bool(
                    test_dependencies and test_dependencies.allow_template_credentials
                ),
            )
            settings = ApplicationSettings.from_config(resolved_config)
            plan = build_stage_plan(
                action=self.job_spec.action,
                requested_stages=requested_stages,
                validation_enabled=settings.review_validation_enabled(),
                validation_required=metadata.get("validation_required"),
                require_clean_validation=metadata.get("require_clean_validation"),
                allow_unvalidated_when_validation_optional=metadata.get(
                    "allow_unvalidated_when_validation_optional"
                ),
            )
            # Only an explicitly injected, in-process test adapter may omit
            # external-host acknowledgement.  Ordinary environment variables
            # are never an authority boundary for production run/resume.
            if test_dependencies is None:
                reachable = (
                    explicit_scope.provider_route_plan if explicit_scope is not None
                    else build_reachable_provider_route_plan(
                        resolved_config, action=self.job_spec.action,
                        requested_stages=requested_stages, free_mode_enabled=free_mode_enabled, stage_plan=plan,
                    )
                )
                policy = build_runtime_external_host_policy(
                    resolved_config,
                    reachable,
                    requested_stages=(reachable.stage_plan.requested_stages if explicit_scope is not None else requested_stages),
                    outline_pilot=metadata.get("outline_pilot"),
                )
                validate_external_host_acknowledgement(
                    policy,
                    metadata.get("external_host_acknowledgement"),
                )
        except (StagePlanError, ExternalHostAdmissionError, OSError, ValueError) as exc:
            raise RuntimeRunnerError(str(exc)) from exc
        metadata["requested_stages"] = list(plan.requested_stages)
        metadata["validation_required"] = plan.validation_required
        metadata["require_clean_validation"] = plan.require_clean_validation
        metadata["allow_unvalidated_when_validation_optional"] = (
            plan.allow_unvalidated_when_validation_optional
        )
        metadata["stage_plan"] = plan.to_dict()
        if self.job_spec.free_mode_profile or self.job_spec.free_mode_idea:
            if resume:
                frozen_envelope = metadata.get("free_mode_input")
                if not isinstance(frozen_envelope, Mapping) or not frozen_envelope.get("artifact_id"):
                    raise RuntimeRunnerError(
                        "free mode resume requires frozen free_mode_input metadata"
                    )
                envelope = dict(frozen_envelope)
            else:
                envelope = build_free_mode_intent_envelope(
                    profile_path=self.job_spec.free_mode_profile,
                    idea=self.job_spec.free_mode_idea,
                    job_id=resolved_job_id,
                )
                if envelope is None:
                    raise RuntimeRunnerError("free mode input envelope could not be built")
                metadata["free_mode_input"] = envelope
            metadata["review_intent"] = dict(envelope["review_intent"])
            metadata["free_mode_context"] = build_free_mode_writer_context(envelope)
        return replace(
            self.job_spec,
            job_id=resolved_job_id,
            metadata=metadata,
        )

    @staticmethod
    def _review_batch_spec(spec: RuntimeJobSpec) -> Any | None:
        value = spec.metadata.get("review_batch_spec")
        if not value:
            return None
        from services.review_batch import ReviewBatchSpecV1, load_review_batch_spec

        if isinstance(value, Mapping):
            return ReviewBatchSpecV1.from_dict(value)
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise RuntimePathOriginError("review_batch_spec path must be absolute")
        return load_review_batch_spec(path)

    @staticmethod
    def _validate_persisted_spec(
        workspace: JobWorkspace,
        expected_payload: Mapping[str, Any],
        registry: ArtifactRegistry | None = None,
    ) -> None:
        active_registry = registry or ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        record = active_registry.get("runtime_job_spec")
        path = Path(
            record.path
            if record is not None and record.status == "ready"
            else workspace.artifact_path("runtime_job_spec_v1.json")
        )
        try:
            persisted_spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeRunnerError("persisted runtime job spec is unavailable for resume") from exc
        if persisted_spec != dict(expected_payload):
            raise RuntimeRunnerError("resume spec does not match the persisted runtime job spec")

    @staticmethod
    def _requested_stages(spec: RuntimeJobSpec) -> tuple[str, ...]:
        explicit = spec.metadata.get("requested_stages")
        if explicit is not None:
            return tuple(
                dict.fromkeys(str(item) for item in explicit if str(item) != "source_intake")
            )
        mapping = {
            "analyze": ("analyze",),
            "derive_review_batch": ("derive_review_batch",),
            "retry_failed": ("analyze",),
            "generate_outline": ("outline",),
            "generate_review": ("outline", "review"),
            "generate_section": ("outline", "review"),
            "retry_review_failed": ("outline", "review"),
            "validate_review": ("validate",),
            "run_all": ("analyze", "outline", "review"),
        }
        return mapping.get(spec.action, ())

    @staticmethod
    def _external_registry_resolver(
        workspace: JobWorkspace,
        *,
        registry_paths: Sequence[str | Path] = (),
    ) -> Callable[[str], ArtifactRegistry | None]:
        from services.stage1_reuse import verified_typed_reuse_registry_paths
        from runtime.source_revision_binding import source_revision_registry_paths

        cache: dict[str, ArtifactRegistry | None] = {}
        explicit_paths: dict[str, list[Path]] = {}
        local_registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        source_paths = verified_typed_reuse_registry_paths(local_registry)
        discovered_paths = AgentRuntimeRunner._review_batch_registry_paths(local_registry)
        revision_paths = source_revision_registry_paths(local_registry)
        for value in (*registry_paths, *source_paths, *discovered_paths, *revision_paths):
            candidate = Path(value).expanduser().resolve()
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            job_id = str(payload.get("job_id") or "") if isinstance(payload, Mapping) else ""
            if job_id:
                explicit_paths.setdefault(job_id, []).append(candidate)

        def resolve(job_id: str) -> ArtifactRegistry | None:
            if job_id in cache:
                return cache[job_id]
            explicit = tuple(dict.fromkeys(explicit_paths.get(job_id, ())))
            if explicit:
                if len(explicit) != 1:
                    cache[job_id] = None
                    return None
                try:
                    resolved = ArtifactRegistry(explicit[0], job_id)
                except RegistryError:
                    resolved = None
                cache[job_id] = resolved
                return resolved
            matches: list[Path] = []
            root = Path(workspace.base_output_dir)
            if not root.is_dir():
                cache[job_id] = None
                return None
            for candidate in root.iterdir():
                if not candidate.is_dir() or "__" not in candidate.name:
                    continue
                _project_name, candidate_job_id = candidate.name.rsplit("__", 1)
                registry_path = candidate / "artifact_registry.json"
                if candidate_job_id == job_id and registry_path.is_file():
                    try:
                        payload = json.loads(registry_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, Mapping) and str(payload.get("job_id") or "") == job_id:
                        matches.append(registry_path)
            if len(matches) != 1:
                return None
            try:
                resolved = ArtifactRegistry(matches[0], job_id)
            except RegistryError:
                resolved = None
            cache[job_id] = resolved
            return resolved

        return resolve

    @staticmethod
    def _record_for_ref(session: AgentRuntimeSession, artifact_id: str, path: str) -> ArtifactRecord:
        record = session.context.registry.get(artifact_id) if artifact_id else None
        if record is None:
            resolved = Path(path).resolve()
            record = next(
                (
                    item
                    for item in session.context.registry.list_records()
                    if Path(item.path).resolve() == resolved
                ),
                None,
            )
        if record is None or record.status != "ready":
            raise RuntimeRunnerError(f"stage output is not a ready registered artifact: {path}")
        return record

    @classmethod
    def _output_refs(
        cls,
        session: AgentRuntimeSession,
        result: StageResult,
    ) -> tuple[ArtifactDependencyRefV2, ...]:
        refs: list[ArtifactDependencyRefV2] = []
        for artifact in result.artifacts:
            record = cls._record_for_ref(session, artifact.artifact_id, artifact.path)
            if record.artifact_type == "stage1_progress_snapshot":
                continue
            refs.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=record.job_id,
                    artifact_id=record.artifact_id,
                    artifact_type=record.artifact_type,
                    path=record.path,
                    content_hash=record.content_hash,
                )
            )
        return tuple(refs)

    def _persist_terminal(
        self,
        session: AgentRuntimeSession,
        *,
        attempt_id: str,
        stage_name: str,
        result: StageResult,
        started_at: str,
        model_call_count: int,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    ) -> None:
        self._fault("after_registry_write_before_stage_terminal", stage_name=stage_name)
        output_refs = self._output_refs(session, result)
        if result.success:
            reconciler = RuntimeReconciler(
                session.context.workspace,
                session.context.registry,
                schema_validators=self._schema_validators(),
                external_registry_resolver=(
                    external_registry_resolver
                    or self._external_registry_resolver(session.context.workspace)
                ),
            )
            def validate_outputs() -> bool:
                for output_ref in output_refs:
                    reconciler.validate_dependency_ref(output_ref)
                return True

            # Share one operation snapshot and the existing final source/record fences.
            if reconciler._run_completion_query(stage_name, validate_outputs) is not True:
                diagnostics = "; ".join(reconciler.last_completion_diagnostics)
                raise ReconcileValidationError(
                    f"stage outputs are not valid for {stage_name}: {diagnostics}"
                )
        record = TerminalStageRecordV1.create(
            job_id=session.context.workspace.job_id,
            attempt_id=attempt_id,
            stage_name=stage_name,
            status="succeeded" if result.success else "failed",
            producer="runtime.runner.AgentRuntimeRunner",
            output_artifact_refs=output_refs,
            model_call_count=model_call_count,
            started_at=started_at,
            terminal_reason="" if result.success else "stage returned unsuccessful result",
        )
        StageTerminalStore(session.context.workspace, session.context.registry).persist(record)

    def _execute_stage(
        self,
        stage: str,
        *,
        bridge: AgentRuntimeBridge,
        session: AgentRuntimeSession,
        spec: RuntimeJobSpec,
        bundle: SourceBundle,
        results: Mapping[str, StageResult],
        attempt_id: str,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    ) -> tuple[StageResult, int]:
        try:
            return bridge.execute_stage(
                stage,
                session=session,
                spec=spec,
                bundle=bundle,
                results=results,
                attempt_id=attempt_id,
                external_registry_resolver=external_registry_resolver,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeRunnerError(f"built-in stage executor failed for {stage}: {exc}") from exc

    @staticmethod
    def _restore_analyze_host_summaries(
        session: AgentRuntimeSession,
        result: StageResult,
        *,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    ) -> None:
        """Restore the canonical output of a reconciled Analyze stage.

        Request summary sources are inputs to Stage 1. Re-reading them beside
        its materialized output would duplicate papers with different reuse
        provenance, even when their source facts are identical.
        """
        from runtime.orchestrator import InternalStageExecutorRegistry

        outputs = [item for item in result.artifacts if item.artifact_type == "summary_file"]
        if not result.success or result.stage_name != "analyze" or len(outputs) != 1:
            raise RuntimeRunnerError("recovered Analyze requires one canonical summary output")
        output = outputs[0]
        registry = session.context.registry
        record = registry.get(output.artifact_id)
        if (
            record is None
            or record.status != "ready"
            or record.job_id != session.context.workspace.job_id
            or record.artifact_type != output.artifact_type
            or record.artifact_version != output.artifact_version
            or Path(record.path).resolve() != Path(output.path).resolve()
        ):
            raise RuntimeRunnerError("recovered Analyze summary has no matching Registry authority")
        registry.verify_ready_artifact_closure(
            record, external_registry_resolver=external_registry_resolver,
        )
        summaries = InternalStageExecutorRegistry._summary_payloads_from_file(record.path)
        if not summaries:
            raise RuntimeRunnerError("recovered Analyze summary output is empty")
        session.stage_host.summaries = summaries
        session.stage_host.summary_file = record.path

    @staticmethod
    def _publish_optional_current_artifact_set(session: AgentRuntimeSession) -> None:
        """Publish a review set whose validation disposition is explicit.

        When review validation is disabled, the review artifacts still need a
        durable current-set boundary.  Validation is represented by a typed
        ``not_requested`` disposition and an empty, complete receipt closure;
        the set remains ineligible for validation-stage completion until a
        later explicit validation run replaces it.
        """

        from runtime.provider_receipt_closure import ProviderReceiptClosure
        from validation.disposition import ValidationDispositionV1
        from validation.repair_transaction import RepairPromotionTransaction

        context = session.context
        registry = context.registry
        current_set = registry.resolve_current_artifact_set()
        if current_set is not None:
            return
        job_id = context.workspace.job_id
        draft = registry.get("review_draft")
        manifest = registry.get("citation_manifest_v3")
        review_docx = registry.get("review_docx")
        if (
            draft is None
            or manifest is None
            or review_docx is None
            or draft.status != "ready"
            or manifest.status != "ready"
            or review_docx.status != "ready"
        ):
            raise RuntimeRunnerError(
                "cannot publish optional current artifact set without ready review, citation, and DOCX artifacts"
            )

        def ref(record: ArtifactRecord) -> ArtifactDependencyRefV2:
            return ArtifactDependencyRefV2.from_record(record)

        runtime_spec = registry.get("runtime_job_spec")
        if runtime_spec is None or runtime_spec.status != "ready":
            raise RuntimeRunnerError(
                "cannot publish optional validation disposition without a ready runtime_job_spec"
            )
        stage_plan_hash = hash_json(context.readiness_policy_snapshot.get("stage_plan") or {})
        disposition = ValidationDispositionV1.create(
            job_id=job_id,
            stage_plan_hash=stage_plan_hash,
            spec_hash=runtime_spec.content_hash,
            review_draft_artifact_id=draft.artifact_id,
            review_draft_artifact_hash=draft.content_hash,
            citation_manifest_artifact_id=manifest.artifact_id,
            citation_manifest_artifact_hash=manifest.content_hash,
            review_docx_artifact_id=review_docx.artifact_id,
            review_docx_artifact_hash=review_docx.content_hash,
            actor="runtime.runner.AgentRuntimeRunner",
            reason="review completed with validation explicitly not requested by the durable stage plan",
        )
        disposition_id = "validation:not_requested"
        disposition_path = Path(
            context.workspace.artifact_path("validation/not_requested/validation_disposition.json")
        )
        disposition_record = registry.get(disposition_id)
        if disposition_record is None or disposition_record.status != "ready":
            disposition_record = publish_json_artifact(
                context.publication_context,
                registry,
                disposition_path,
                disposition.to_dict(),
                artifact_role="validation_disposition",
                artifact_type="validation_disposition",
                artifact_version="v1",
                producer="runtime.runner.AgentRuntimeRunner",
                artifact_id=disposition_id,
                depends_on=(ref(draft), ref(manifest), ref(review_docx), ref(runtime_spec)),
                metadata={
                    "validation_status": "not_requested",
                    "allow_unvalidated": True,
                    "stage_plan_hash": stage_plan_hash,
                    "spec_hash": runtime_spec.content_hash,
                },
            )
        if disposition_record is None:
            raise RuntimeRunnerError("optional validation disposition registration returned no record")

        empty_closure = ProviderReceiptClosure.evaluate((), ())
        closure_id = "validation:provider_receipt_closure:not_requested"
        closure_path = Path(
            context.workspace.artifact_path("validation/not_requested/provider_receipt_closure.json")
        )
        closure_record = registry.get(closure_id)
        if closure_record is None or closure_record.status != "ready":
            closure_record = publish_json_artifact(
                context.publication_context,
                registry,
                closure_path,
                {
                    "artifact_type": "provider_receipt_closure",
                    "artifact_version": "v1",
                    "job_id": job_id,
                    "payload": empty_closure.to_dict(),
                },
                artifact_role="validation_provider_receipt_closure",
                artifact_type="provider_receipt_closure",
                artifact_version="v1",
                producer="runtime.runner.AgentRuntimeRunner",
                artifact_id=closure_id,
                depends_on=(ref(disposition_record),),
                metadata={
                    "stage_name": "validation",
                    "validation_status": "not_requested",
                },
            )
        if closure_record is None:
            raise RuntimeRunnerError("optional validation closure registration returned no record")

        promotion_id = "runtime-validation:not-requested"
        promotion_path = Path(
            context.workspace.artifact_path(
                "validation/not_requested/repair_promotion_transaction.json"
            )
        )
        promotion_record = registry.get(promotion_id)
        if promotion_record is None or promotion_record.status != "ready":
            promotion = RepairPromotionTransaction(
                transaction_id=promotion_id,
                job_id=job_id,
                source_transaction_id="",
                status="prepared",
                actor="runtime.runner.AgentRuntimeRunner",
                reason="review completed with validation explicitly not requested",
                canonical_version="runtime-validation",
                review_draft_artifact_id=draft.artifact_id,
                citation_manifest_artifact_id=manifest.artifact_id,
                review_docx_artifact_id=review_docx.artifact_id,
                audit_artifact_id="",
                lineage_artifact_id="",
                canonical_input_hashes={
                    draft.artifact_id: draft.content_hash,
                    manifest.artifact_id: manifest.content_hash,
                    review_docx.artifact_id: review_docx.content_hash,
                },
                output_hashes={
                    draft.artifact_id: draft.content_hash,
                    manifest.artifact_id: manifest.content_hash,
                    review_docx.artifact_id: review_docx.content_hash,
                    disposition_record.artifact_id: disposition_record.content_hash,
                    closure_record.artifact_id: closure_record.content_hash,
                },
                created_at=utc_now_iso(),
                validation_run_result_artifact_id="",
                validation_disposition_artifact_id=disposition_record.artifact_id,
            )
            promotion_record = publish_json_artifact(
                context.publication_context,
                registry,
                promotion_path,
                promotion.to_dict(),
                artifact_role="repair_promotion_transaction",
                artifact_type="repair_promotion_transaction",
                artifact_version="v1",
                producer="runtime.runner.AgentRuntimeRunner",
                artifact_id=promotion_id,
                depends_on=(
                    ref(draft),
                    ref(manifest),
                    ref(review_docx),
                    ref(disposition_record),
                    ref(closure_record),
                ),
                metadata={
                    "status": "prepared",
                    "promotion_state": "prepared",
                    "validation_status": "not_requested",
                },
            )
        if promotion_record is None:
            raise RuntimeRunnerError("optional validation promotion registration returned no record")

        current_set = registry.build_current_artifact_set(
            promotion_transaction_id=promotion_id,
            promotion_transaction_hash=promotion_record.content_hash,
            review_draft_artifact_id=draft.artifact_id,
            review_draft_artifact_hash=draft.content_hash,
            citation_manifest_artifact_id=manifest.artifact_id,
            citation_manifest_artifact_hash=manifest.content_hash,
            review_docx_artifact_id=review_docx.artifact_id,
            review_docx_artifact_hash=review_docx.content_hash,
            validation_run_result_artifact_id="",
            validation_run_result_artifact_hash="",
            validation_receipt_closure_artifact_id=closure_record.artifact_id,
            validation_receipt_closure_artifact_hash=closure_record.content_hash,
            validation_status="not_requested",
            actor="runtime.runner.AgentRuntimeRunner",
            reason="review completed with validation explicitly not requested",
            validation_disposition_artifact_id=disposition_record.artifact_id,
            validation_disposition_artifact_hash=disposition_record.content_hash,
        )
        registry.switch_current_artifact_set(
            current_set,
            prepared_promotion_record=promotion_record,
        )

    @staticmethod
    def _recover_promoted_repair_validation(
        session: AgentRuntimeSession,
        *,
        external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
    ) -> StageResult | None:
        """Rehydrate an approved repair's completed validation without a new call."""

        from validation.run_result import (
            ValidationExecutionStatus,
            ValidationRunDisposition,
            ValidationRunResultV1,
        )

        registry = session.context.registry
        current_set = registry.resolve_current_artifact_set()
        if current_set is None:
            return None
        if external_registry_resolver is None:
            external_registry_resolver = AgentRuntimeRunner._external_registry_resolver(
                session.context.workspace,
            )
        # A validation run may have committed a new set over a promoted
        # repair just before the process crashed. Traverse immutable parent
        # sets so that recovery still recognizes the repaired lineage.
        lineage = current_set
        repaired_lineage = False
        seen_set_ids: set[str] = set()
        for _depth in range(32):
            if lineage.set_id in seen_set_ids:
                raise RuntimeRunnerError("current artifact set lineage is cyclic")
            seen_set_ids.add(lineage.set_id)
            if lineage.promotion_transaction_id.startswith("repair-promotion:"):
                repaired_lineage = True
                break
            if not lineage.promotion_transaction_id.startswith("runtime-validation:"):
                break
            if not lineage.previous_set_id:
                break
            parent_record = registry.get(lineage.previous_set_id)
            if parent_record is None or parent_record.status != "ready":
                raise RuntimeRunnerError("repaired validation parent set is missing")
            registry.verify_ready_artifact_closure(
                parent_record, external_registry_resolver=external_registry_resolver,
            )
            parent = CurrentArtifactSetV1.from_dict(
                json.loads(Path(parent_record.path).read_text(encoding="utf-8"))
            )
            if (
                parent.job_id != current_set.job_id
                or parent.set_id != lineage.previous_set_id
                or parent.content_addressed_id() != parent.set_id
                or any(
                    getattr(parent, name) != getattr(current_set, name)
                    for name in (
                        "review_draft_artifact_id",
                        "review_draft_artifact_hash",
                        "citation_manifest_artifact_id",
                        "citation_manifest_artifact_hash",
                        "review_docx_artifact_id",
                        "review_docx_artifact_hash",
                    )
                )
            ):
                raise RuntimeRunnerError(
                    "runtime validation changed the repaired current input lineage"
                )
            lineage = parent
        else:
            raise RuntimeRunnerError(
                "repaired current artifact set lineage exceeds the recovery bound"
            )
        if not repaired_lineage:
            return None
        if current_set.validation_status != "clean":
            raise RuntimeRunnerError(
                "promoted repair has no clean current validation result"
            )
        required = {
            "review_draft": (
                current_set.review_draft_artifact_id,
                current_set.review_draft_artifact_hash,
            ),
            "citation_manifest": (
                current_set.citation_manifest_artifact_id,
                current_set.citation_manifest_artifact_hash,
            ),
            "review_docx": (
                current_set.review_docx_artifact_id,
                current_set.review_docx_artifact_hash,
            ),
            "validation": (
                current_set.validation_run_result_artifact_id,
                current_set.validation_run_result_artifact_hash,
            ),
            "receipt_closure": (
                current_set.validation_receipt_closure_artifact_id,
                current_set.validation_receipt_closure_artifact_hash,
            ),
            "current_set": (current_set.set_id, ""),
            "promotion": (current_set.promotion_transaction_id, ""),
        }
        records: dict[str, ArtifactRecord] = {}
        for role, (artifact_id, expected_hash) in required.items():
            record = registry.get(artifact_id)
            if (
                record is None
                or record.status != "ready"
                or (expected_hash and record.content_hash != expected_hash)
            ):
                raise RuntimeRunnerError(
                    f"promoted repair {role} is missing or changed"
                )
            registry.verify_ready_artifact_closure(
                record, external_registry_resolver=external_registry_resolver,
            )
            records[role] = record
        closure_record = records["receipt_closure"]
        if closure_record.metadata.get("complete") is not True:
            raise RuntimeRunnerError(
                "promoted repair validation receipt closure is incomplete"
            )
        validation = ValidationRunResultV1.from_dict(
            json.loads(Path(records["validation"].path).read_text(encoding="utf-8"))
        )
        if (
            validation.job_id != session.context.workspace.job_id
            or validation.execution_status is not ValidationExecutionStatus.SUCCEEDED
            or validation.validation_disposition is not ValidationRunDisposition.CLEAN
            or not validation.contract_satisfied
            or validation.input_artifacts.review_draft_id
            != records["review_draft"].artifact_id
            or validation.input_artifacts.review_draft_hash
            != records["review_draft"].content_hash
            or validation.input_artifacts.citation_manifest_id
            != records["citation_manifest"].artifact_id
            or validation.input_artifacts.citation_manifest_hash
            != records["citation_manifest"].content_hash
        ):
            raise RuntimeRunnerError(
                "promoted repair validation is not bound to the current draft and manifest"
            )
        closure_payload = json.loads(
            Path(closure_record.path).read_text(encoding="utf-8")
        )
        receipt_counts = closure_payload.get("payload")
        if not isinstance(receipt_counts, Mapping) or receipt_counts.get("complete") is not True:
            raise RuntimeRunnerError(
                "promoted repair receipt closure payload is incomplete"
            )
        record_id = "stage-terminal-repair-" + hash_json(
            {
                "current_set_id": current_set.set_id,
                "validation_hash": records["validation"].content_hash,
            }
        )[:24]
        terminal_store = StageTerminalStore(
            session.context.workspace,
            registry,
            publication_context=session.context.publication_context,
        )
        observed = {
            str(item) for item in receipt_counts.get("observed_call_ids") or ()
            if str(item)
        }
        reused = {
            str(item) for item in receipt_counts.get("verified_reuse_call_ids") or ()
            if str(item)
        }
        model_call_count = len(observed - reused)
        input_refs = tuple(
            ArtifactDependencyRefV2.from_record(records[role])
            for role in ("review_draft", "citation_manifest", "review_docx")
        )
        output_refs = tuple(
            ArtifactDependencyRefV2.from_record(records[role])
            for role in ("validation", "receipt_closure", "current_set")
        )
        recovery_producer = "runtime.runner.AgentRuntimeRunner.recover_promoted_repair"
        existing = registry.get(record_id)
        if existing is None:
            terminal = TerminalStageRecordV1.create(
                job_id=session.context.workspace.job_id,
                attempt_id=validation.attempt_id,
                stage_name="validate",
                status="succeeded",
                producer=recovery_producer,
                input_artifact_refs=input_refs,
                output_artifact_refs=output_refs,
                model_call_count=model_call_count,
                terminal_reason="verified promoted repair revalidation reused without provider call",
                reconstructed_by_reconcile=True,
                record_id=record_id,
            )
            terminal_store.persist(terminal)
        else:
            registry.verify_ready_artifact_closure(
                existing, external_registry_resolver=external_registry_resolver,
            )
            terminal = TerminalStageRecordV1.from_dict(
                json.loads(Path(existing.path).read_text(encoding="utf-8"))
            )
            if (
                existing.artifact_id != record_id
                or existing.artifact_type != "runtime_stage_terminal"
                or existing.artifact_version != "v1"
                or existing.artifact_role != "runtime_stage_terminal"
                or existing.job_id != session.context.workspace.job_id
                or existing.status != "ready"
                or tuple(existing.depends_on) != output_refs
                or Path(existing.path).resolve().parent
                != terminal_store.path_for(terminal).resolve().parent
                or not Path(existing.path).name.startswith(
                    terminal_store.path_for(terminal).stem + "__"
                )
                or terminal.record_id != record_id
                or terminal.job_id != session.context.workspace.job_id
                or terminal.attempt_id != validation.attempt_id
                or terminal.stage_name != "validate"
                or terminal.status != "succeeded"
                or terminal.producer != recovery_producer
                or tuple(terminal.input_artifact_refs) != input_refs
                or tuple(terminal.output_artifact_refs) != output_refs
                or terminal.model_call_count != model_call_count
                or not terminal.reconstructed_by_reconcile
                or existing.metadata.get("stage_name") != "validate"
                or existing.metadata.get("stage_status") != "succeeded"
                or existing.metadata.get("attempt_id") != validation.attempt_id
                or existing.metadata.get("model_call_count") != model_call_count
                or existing.metadata.get("reconstructed_by_reconcile") is not True
            ):
                raise RuntimeRunnerError(
                    "promoted repair recovery terminal has conflicting identity"
                )
        return StageResult(
            stage_name="validate",
            success=True,
            artifacts=[
                StageArtifactRef(
                    artifact_role=record.artifact_role,
                    artifact_type=record.artifact_type,
                    artifact_version=record.artifact_version,
                    path=record.path,
                    artifact_id=record.artifact_id,
                )
                for role in ("validation", "receipt_closure", "current_set")
                for record in (records[role],)
            ],
            metadata={
                "validation_disposition": "clean",
                "validation_run_id": validation.validation_run_id,
                "execution_status": "succeeded",
                "recovered_from_terminal": record_id,
                "model_call_count_this_attempt": 0,
            },
        )

    def run(self) -> RuntimeExecutionResult:
        if self.queue_resume_intent is not None:
            raise RuntimeRunnerError("queue resume intent cannot authorize a new run")
        from runtime.provider_runtime import current_runtime_control_context, runtime_control_lifecycle

        if current_runtime_control_context() is not None:
            return self._execute(resume=False)
        with runtime_control_lifecycle():
            return self._execute(resume=False)

    def resume(self) -> RuntimeExecutionResult:
        from runtime.provider_runtime import current_runtime_control_context, runtime_control_lifecycle

        if current_runtime_control_context() is not None:
            return self._execute(resume=True)
        with runtime_control_lifecycle():
            return self._execute(resume=True)

    def _execute(self, *, resume: bool) -> RuntimeExecutionResult:
        config_source_id = str(Path(self.job_spec.config).expanduser().resolve())
        try:
            config_source_sha256 = file_sha256(config_source_id)
        except OSError as exc:
            raise RuntimeRunnerError("configuration source cannot be read") from exc
        spec = self._normalized_spec(resume=resume)
        if file_sha256(config_source_id) != config_source_sha256:
            raise RuntimeRunnerError("configuration source changed during runtime normalization")
        verified_source_revision_plan = None
        if "source_revision_plan" in spec.metadata:
            from runtime.source_revision_admission import verify_source_revision_plan_v1

            verified_source_revision_plan = verify_source_revision_plan_v1(spec)
        batch_spec = self._review_batch_spec(spec)
        if spec.action == "derive_review_batch" and batch_spec is None:
            raise RuntimeRunnerError("derive_review_batch action requires a review batch spec")
        if (
            spec.action == "derive_review_batch"
            and batch_spec is not None
            and not batch_spec.is_multi_variant
        ):
            raise RuntimeRunnerError(
                "derive_review_batch action requires a multi-variant review batch spec"
            )
        if (
            batch_spec is not None
            and batch_spec.is_multi_variant
            and spec.action != "derive_review_batch"
        ):
            raise RuntimeRunnerError(
                "multi-variant review batches require the derive_review_batch action"
            )
        if batch_spec is not None and not spec.summary_sources:
            spec = replace(
                spec,
                summary_sources=(batch_spec.parent_selection().parent_summary_path,),
            )
        validated_batch_parent_registry_path: str | None = None
        if batch_spec is not None:
            from services.review_batch import validate_review_batch_parent

            _parent_path, validated_parent_registry = validate_review_batch_parent(
                batch_spec.parent_selection()
            )
            validated_batch_parent_registry_path = validated_parent_registry.registry_path
        bridge = AgentRuntimeBridge(
            spec, publication_context=self.publication_context,
            verified_source_revision_plan=verified_source_revision_plan,
        )
        normalized_spec_payload = spec.to_dict()
        resume_preflight = None
        workspace_preflight = None
        execution_lease: AttemptExecutionLease | None = None
        if batch_spec is not None and batch_spec.is_multi_variant:
            from services.review_batch import validate_review_batch_layout

            def validate_batch_workspace(workspace: JobWorkspace) -> None:
                validate_review_batch_layout(batch_spec, workspace=workspace)

            workspace_preflight = validate_batch_workspace
        if resume:
            def validate_resume(workspace: JobWorkspace) -> None:
                nonlocal execution_lease
                candidate = AttemptExecutionLease(workspace)
                candidate.acquire()
                try:
                    self._validate_persisted_spec(
                        workspace,
                        normalized_spec_payload,
                        ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
                    )
                except BaseException:
                    candidate.release()
                    raise
                execution_lease = candidate

            resume_preflight = validate_resume
        try:
            session = bridge.bootstrap(
                cancel_token=self.cancel_token,
                claim_latest_pointer=not resume,
                resume_requested=resume,
                resume_preflight=resume_preflight,
                workspace_preflight=workspace_preflight,
                publish_running_state=False,
            )
            if file_sha256(config_source_id) != config_source_sha256:
                raise RuntimeRunnerError("configuration source changed during runtime bootstrap")
        except BaseException as exc:
            if execution_lease is not None:
                execution_lease.release()
            if isinstance(exc, RuntimeError):
                operation = "resume" if resume else "run"
                raise RuntimeRunnerError(f"{operation} rejected: {exc}") from exc
            raise

        if execution_lease is None:
            execution_lease = AttemptExecutionLease(session.context.workspace)
            try:
                execution_lease.acquire()
            except AttemptAlreadyRunningError as exc:
                raise RuntimeRunnerError(f"run rejected: {exc}") from exc
        try:
            from runtime.provider_runtime import (
                RuntimeControlIdentityV1,
                bind_runtime_control_context,
                bind_provider_run_budget,
                ProviderBudgetController,
                ProviderAggregateBudgetV2,
                current_acceptance_execution_context,
                provider_run_budget_controller_from_environment,
                ProviderRuntimeContractError,
                validate_started_acceptance_execution_context,
            )

            acceptance_context = current_acceptance_execution_context()
            from runtime.downstream_continuation import (
                bind_current_continuation,
                controller_for_continuation,
                load_current_continuation,
            )

            continuation = load_current_continuation(
                session.context.workspace, session.context.registry, spec.to_dict(),
            ) if resume else None
            if continuation is not None and acceptance_context is not None:
                raise RuntimeRunnerError("downstream continuation cannot replace accepted-run authority")
            try:
                acceptance_budget = controller_for_continuation(continuation) if continuation else provider_run_budget_controller_from_environment(
                    runtime_spec_budget=spec.metadata.get("provider_budget"),
                )
            except ProviderRuntimeContractError as exc:
                raise RuntimeRunnerError(f"provider run authority rejected: {exc}") from exc
            ordinary_run_budget = acceptance_context is None
            if acceptance_budget is None:
                from runtime.run_budget_binding import resolve_run_provider_budget

                budget, _authority = resolve_run_provider_budget(spec.metadata, session.stage_host.config)
                acceptance_budget = ProviderBudgetController(budget)
            if acceptance_context is not None:
                from runtime.checkout_identity import read_checkout_sha

                current_executable_sha = read_checkout_sha(
                    Path(__file__).resolve().parents[1],
                    require_clean=True,
                )
                validate_started_acceptance_execution_context(
                    acceptance_context,
                    acceptance_budget,
                    current_executable_sha=current_executable_sha,
                )
            if acceptance_budget is not None and continuation is None:
                acceptance_state_path = _acceptance_budget_state_path(
                    session.context.workspace
                )
                acceptance_budget.bind_state_path(
                    acceptance_state_path,
                    acceptance_run_id=(
                        acceptance_context.acceptance_run_id if acceptance_context
                        else f"run:{session.context.workspace.job_id}" if ordinary_run_budget else ""
                    ),
                    state_started=(resume if ordinary_run_budget else bool(
                        acceptance_context
                        and acceptance_context.provider_budget_state_started
                    )),
                )
            if self.queue_resume_intent is not None:
                if not resume:
                    raise RuntimeRunnerError("queue resume intent cannot authorize a new run")
                # Spec/fingerprint, continuation and the original budget have
                # all been validated. Clear only the captured prior marker;
                # its Registry keeps the current queue publication fence.
                self._apply_queue_resume_intent(session)
            control_identity = RuntimeControlIdentityV1(
                job_id=session.context.workspace.job_id,
                workspace_path=str(session.context.workspace.root_dir),
                acceptance_run_id=(
                    continuation.run_id if continuation is not None
                    else acceptance_context.acceptance_run_id if acceptance_context
                    else f"run:{session.context.workspace.job_id}" if ordinary_run_budget else ""
                ),
                provider_budget_started=ordinary_run_budget or bool(
                    acceptance_context
                    and acceptance_context.provider_budget_state_started
                ),
            )
            from contextlib import nullcontext

            budget_binding = (bind_provider_run_budget(acceptance_budget)
                              if ordinary_run_budget and isinstance(acceptance_budget.budget, ProviderAggregateBudgetV2)
                              else nullcontext())
            continuation_binding = bind_current_continuation(continuation) if continuation else nullcontext()
            with continuation_binding, budget_binding, bind_runtime_control_context(
                control_identity,
                workspace=session.context.workspace,
                registry=session.context.registry,
            ):
                return self._execute_with_lease(
                    session=session,
                    spec=spec,
                    bridge=bridge,
                    batch_spec=batch_spec,
                    validated_batch_parent_registry_path=validated_batch_parent_registry_path,
                    resume=resume,
                    normalized_spec_payload=normalized_spec_payload,
                    config_source_id=config_source_id,
                    config_source_sha256=config_source_sha256,
                )
        finally:
            execution_lease.release()

    def _admit_run_provider_envelope(self, session: AgentRuntimeSession, spec: RuntimeJobSpec) -> None:
        from runtime.provider_runtime import (
            ProviderAggregateBudgetV2,
            build_provider_run_scope_hashes_v1,
            current_acceptance_execution_context,
            provider_run_budget_controller_from_environment,
            provider_run_scope_authorization_from_verified_artifact,
            provider_run_scope_payload_from_plan_binding,
            provider_run_scope_proof_from_verified_artifact,
            provider_run_workspace_identity_sha256,
            validate_provider_run_plan_binding,
            validate_provider_run_scope_authority,
            validate_provider_run_scope_authorization,
            validate_started_acceptance_execution_context,
        )
        from runtime.provider_run_envelope import ProviderRunEnvelopeV1, build_provider_run_envelope_v1
        from runtime.control_plane import ReviewControlPlane
        from runtime.run_budget_binding import resolve_run_provider_budget
        from runtime.provider_routes import build_reachable_provider_route_plan
        from runtime.stage_planning import StagePlan
        from runtime.checkout_identity import read_checkout_sha
        from runtime.downstream_continuation import current_continuation, load_current_continuation

        acceptance = current_acceptance_execution_context()
        continuation = current_continuation()
        controller = provider_run_budget_controller_from_environment(
            runtime_spec_budget=spec.metadata.get("provider_budget"),
        )
        if controller is None:
            raise RuntimeRunnerError("run has no durable finite provider budget")
        if not isinstance(controller.budget, ProviderAggregateBudgetV2):
            raise RuntimeRunnerError(
                "new runtime runs require a strict finite V2 provider budget"
            )
        stage_policy = dict(session.context.readiness_policy_snapshot["stage_plan"])
        stage_plan = StagePlan(
            **{
                **stage_policy,
                "requested_stages": tuple(stage_policy["requested_stages"]),
                "required_stages": tuple(stage_policy["required_stages"]),
            }
        )
        route_plan = build_reachable_provider_route_plan(
            session.stage_host.config,
            action=spec.action,
            requested_stages=stage_plan.requested_stages,
            free_mode_enabled=bool(
                spec.free_mode_profile or spec.free_mode_idea or spec.metadata.get("free_mode_input")
            ),
            stage_plan=stage_plan,
        )
        if spec.metadata.get("stage1_summary_input_mode") == "explicit_typed_import":
            from runtime.explicit_stage1_scope import resolve_explicit_stage1_scope

            route_plan = resolve_explicit_stage1_scope(
                spec, session.stage_host.config, stage_plan,
                external_registry_resolver=self._external_registry_resolver(session.context.workspace),
            ).provider_route_plan
        scope_hashes = build_provider_run_scope_hashes_v1(
            normalized_runtime_spec=spec.to_dict(),
            effective_config_sha256=str(session.context.fingerprint_bundle["config_hash"]),
            source_snapshot_sha256=str(session.context.fingerprint_bundle["source_hash"]),
            review_goal=spec.metadata.get("review_intent") or {},
            route_plan=route_plan,
            stage_work_contract=session.context.readiness_policy_snapshot.get("stage_plan") or {},
        )
        if continuation is not None:
            verified = load_current_continuation(
                session.context.workspace, session.context.registry, spec.to_dict(),
                expected_scope_hashes=scope_hashes,
            )
            if verified is None or verified.artifact_record.content_hash != continuation.artifact_record.content_hash:
                raise RuntimeRunnerError("downstream continuation authority changed during admission")
        workspace_identity_sha256 = provider_run_workspace_identity_sha256(
            session.context.workspace.root_dir,
            job_id=session.context.workspace.job_id,
        )

        accepted_scope_record = None
        accepted_scope_authorization = None
        current_executable_sha = ""
        if acceptance is not None:
            try:
                current_executable_sha = read_checkout_sha(
                    Path(__file__).resolve().parents[1],
                    require_clean=True,
                )
                validate_started_acceptance_execution_context(
                    acceptance,
                    controller,
                    current_executable_sha=current_executable_sha,
                )
                binding = validate_provider_run_plan_binding(
                    controller,
                    job_id=session.context.workspace.job_id,
                    runtime_spec_sha256=scope_hashes["runtime_spec_sha256"],
                    scope_hashes=scope_hashes,
                    workspace_identity_sha256=workspace_identity_sha256,
                    current_executable_sha=current_executable_sha,
                )
            except Exception as exc:
                raise RuntimeRunnerError(
                    f"accepted provider run scope binding rejected: {exc}"
                ) from exc

            runtime_spec_record = session.context.registry.get("runtime_job_spec")
            source_inventory_record = session.context.registry.get("source_inventory")
            if (
                runtime_spec_record is None
                or runtime_spec_record.status != "ready"
                or source_inventory_record is None
                or source_inventory_record.status != "ready"
            ):
                raise RuntimeRunnerError(
                    "accepted runtime scope requires ready RuntimeJobSpec and canonical source inventory artifacts"
                )
            try:
                session.context.registry.verify_ready_artifact_closure(runtime_spec_record)
                session.context.registry.verify_ready_artifact_closure(source_inventory_record)
            except Exception as exc:
                raise RuntimeRunnerError(
                    "accepted runtime scope source/spec closure is invalid"
                ) from exc
            config_binding = (
                runtime_spec_record.metadata.get("config_snapshot_binding")
                if isinstance(runtime_spec_record.metadata, Mapping)
                else None
            )
            if (
                not isinstance(config_binding, Mapping)
                or config_binding.get("effective_config_sha256")
                != scope_hashes["effective_config_sha256"]
                or config_binding.get("normalized_spec_payload_sha256")
                != hash_json(spec.to_dict())
            ):
                raise RuntimeRunnerError(
                    "accepted runtime scope config/spec binding differs from the active run"
                )

            accepted_scope_payload = provider_run_scope_payload_from_plan_binding(
                controller,
                job_id=session.context.workspace.job_id,
                scope_hashes=scope_hashes,
                workspace_identity_sha256=workspace_identity_sha256,
                current_executable_sha=current_executable_sha,
            )
            accepted_scope_payload.update(
                {
                    "runtime_spec_artifact_id": runtime_spec_record.artifact_id,
                    "runtime_spec_artifact_sha256": runtime_spec_record.content_hash,
                    "source_inventory_artifact_id": source_inventory_record.artifact_id,
                    "source_inventory_artifact_sha256": source_inventory_record.content_hash,
                }
            )
            accepted_scope_record = session.context.registry.get(
                "accepted_runtime_run_scope"
            )
            if accepted_scope_record is None:
                accepted_scope_record = publish_json_artifact(
                    session.context.publication_context,
                    session.context.registry,
                    session.context.workspace.artifact_path(
                        "accepted_runtime_run_scope_v1.json"
                    ),
                    accepted_scope_payload,
                    artifact_role="accepted_runtime_run_scope",
                    artifact_type="accepted_runtime_run_scope",
                    artifact_version="v1",
                    producer="runtime.runner.AgentRuntimeRunner",
                    artifact_id="accepted_runtime_run_scope",
                    depends_on=[
                        ArtifactDependencyRefV2.from_record(runtime_spec_record),
                        ArtifactDependencyRefV2.from_record(source_inventory_record),
                    ],
                    metadata={
                        "job_id": session.context.workspace.job_id,
                        "acceptance_run_id": acceptance.acceptance_run_id,
                        "acceptance_plan_sha256": binding.acceptance_plan_sha256,
                    },
                )
            try:
                accepted_scope_record = session.context.registry.verify_ready_artifact_closure(
                    accepted_scope_record
                )
                accepted_scope_payload = json.loads(
                    Path(accepted_scope_record.path).read_text(encoding="utf-8")
                )
                accepted_scope_authorization = provider_run_scope_authorization_from_verified_artifact(
                    accepted_scope_payload,
                    artifact_id=accepted_scope_record.artifact_id,
                    artifact_type=accepted_scope_record.artifact_type,
                    artifact_version=accepted_scope_record.artifact_version,
                    artifact_role=accepted_scope_record.artifact_role,
                    artifact_status=accepted_scope_record.status,
                    artifact_sha256=accepted_scope_record.content_hash,
                )
            except Exception as exc:
                raise RuntimeRunnerError(
                    f"accepted runtime scope Registry artifact is invalid: {exc}"
                ) from exc
            if accepted_scope_payload != {
                **provider_run_scope_payload_from_plan_binding(
                    controller,
                    job_id=session.context.workspace.job_id,
                    scope_hashes=scope_hashes,
                    workspace_identity_sha256=workspace_identity_sha256,
                    current_executable_sha=current_executable_sha,
                ),
                "runtime_spec_artifact_id": runtime_spec_record.artifact_id,
                "runtime_spec_artifact_sha256": runtime_spec_record.content_hash,
                "source_inventory_artifact_id": source_inventory_record.artifact_id,
                "source_inventory_artifact_sha256": source_inventory_record.content_hash,
            }:
                raise RuntimeRunnerError(
                    "accepted runtime scope artifact differs from the current source, goal, route, spec, or plan"
                )

        if acceptance is None and session.context.registry.get(
            "accepted_runtime_run_scope"
        ) is not None:
            raise RuntimeRunnerError(
                "accepted runtime scope artifact cannot be reused without its ControlPlane entry"
            )
        if acceptance is not None:
            try:
                validate_provider_run_scope_authorization(
                    controller,
                    job_id=session.context.workspace.job_id,
                    scope_hashes=scope_hashes,
                    workspace_identity_sha256=workspace_identity_sha256,
                    current_executable_sha=current_executable_sha,
                    authorization=accepted_scope_authorization,
                )
            except Exception as exc:
                raise RuntimeRunnerError(
                    f"accepted runtime source/goal/route scope is not authorized: {exc}"
                ) from exc
        envelope_id = (
            "provider_run_envelope:" + hash_json(continuation.run_id)
            if continuation is not None else "provider_run_envelope"
        )
        existing = session.context.registry.get(envelope_id)
        if existing is not None:
            session.context.registry.verify_ready_artifact_closure(existing)
            try:
                saved_payload = json.loads(Path(existing.path).read_text(encoding="utf-8"))
                for _ in range(3):
                    if not isinstance(saved_payload, Mapping) or not isinstance(
                        saved_payload.get("payload"), Mapping
                    ):
                        break
                    saved_payload = saved_payload["payload"]
                saved = ProviderRunEnvelopeV1.from_dict(saved_payload)
            except Exception as exc:
                raise RuntimeRunnerError(
                    "persisted provider run envelope is invalid"
                ) from exc
            expected_run_id = (
                continuation.run_id if continuation is not None
                else acceptance.acceptance_run_id
                if acceptance is not None
                else f"run:{session.context.workspace.job_id}"
            )
            if (
                saved.job_id != session.context.workspace.job_id
                or saved.run_id != expected_run_id
                or saved.scope_hashes != scope_hashes
                or saved.provider_budget != controller.budget
            ):
                raise RuntimeRunnerError(
                    "run envelope source, goal, route, configuration or budget changed; approval/reconciliation required"
                )
            if continuation is not None and (
                saved.budget_authority != "downstream_continuation"
                or ArtifactDependencyRefV2.from_record(continuation.artifact_record) not in existing.depends_on
            ):
                raise RuntimeRunnerError("downstream envelope lost its exact grant dependency")
            if saved.admission_status == "blocked_budget":
                raise RuntimeRunnerError(
                    "run-start provider budget is insufficient; approve a finite run scope before POST"
                )
            if acceptance is not None:
                try:
                    proof = provider_run_scope_proof_from_verified_artifact(
                        saved.to_dict(),
                        artifact_id=existing.artifact_id,
                        artifact_role=existing.artifact_role,
                        artifact_status=existing.status,
                        artifact_sha256=existing.content_hash,
                    )
                    validate_provider_run_scope_authority(
                        controller,
                        job_id=session.context.workspace.job_id,
                        scope_hashes=scope_hashes,
                        workspace_identity_sha256=workspace_identity_sha256,
                        current_executable_sha=current_executable_sha,
                        authorization=accepted_scope_authorization,
                        proof=proof,
                    )
                except Exception as exc:
                    raise RuntimeRunnerError(
                        f"accepted continuation run-scope proof is invalid: {exc}"
                    ) from exc
            from runtime.arbitration_work_policy import publish_arbitration_surface

            session.stage_host.arbitration_work_policy_record = publish_arbitration_surface(
                registry=session.context.registry, workspace=session.context.workspace,
                publication_context=session.context.publication_context,
                envelope_record=existing, route_plan=route_plan.to_dict(),
                candidate_count=session.context.settings.outline_candidate_count(),
                stage1_work_item_basis=session.context.stage1_work_item_basis,
                existing_run_envelope=True,
            )
            return
        _budget, authority = resolve_run_provider_budget(spec.metadata, session.stage_host.config)
        if acceptance is not None:
            authority = "bound_acceptance_context"
        elif continuation is not None:
            authority = "downstream_continuation"
        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        # Project only the newly authorized work; execution still uses the
        # original immutable spec and verifies all completed upstream stages.
        planning_spec = replace(spec, metadata={
            **spec.metadata, "requested_stages": ["review", "validate"],
        }) if continuation is not None else spec
        projection = control._full_stage_request_plan_for_spec(
            planning_spec,
            aggregate_budget_override=controller.budget,
            stage1_work_item_basis=session.context.stage1_work_item_basis,
            derive_stage1_work_item_basis=False,
        )
        envelope = build_provider_run_envelope_v1(
            job_id=session.context.workspace.job_id,
            run_id=continuation.run_id if continuation is not None else acceptance.acceptance_run_id if acceptance is not None else f"run:{session.context.workspace.job_id}",
            budget_authority=authority, provider_budget=controller.budget,
            scope_hashes=scope_hashes, full_stage_request_plan=projection,
        )
        envelope_dependencies = [
            ArtifactDependencyRefV2.from_record(source)
            for name in ("runtime_job_spec", "source_inventory")
            if (source := session.context.registry.get(name)) is not None
        ]
        if accepted_scope_record is not None:
            envelope_dependencies.append(
                ArtifactDependencyRefV2.from_record(accepted_scope_record)
            )
        if continuation is not None:
            envelope_dependencies.append(ArtifactDependencyRefV2.from_record(continuation.artifact_record))
        record = publish_json_artifact(
            session.context.publication_context, session.context.registry,
            session.context.workspace.artifact_path(
                "downstream_continuations/" + hash_json(continuation.run_id) + "/provider_run_envelope_v1.json"
                if continuation is not None else "provider_run_envelope_v1.json"
            ), envelope.to_dict(),
            artifact_role="provider_run_envelope", artifact_type="provider_run_envelope", artifact_version="v1",
            producer="runtime.runner.AgentRuntimeRunner", artifact_id=envelope_id,
            depends_on=envelope_dependencies,
            metadata={"job_id": session.context.workspace.job_id, "envelope_sha256": envelope.to_dict()["envelope_sha256"]},
        )
        record = session.context.registry.verify_ready_artifact_closure(record)
        if envelope.admission_status == "blocked_budget":
            raise RuntimeRunnerError("run-start provider budget is insufficient; approve a finite run scope before POST")
        if acceptance is not None:
            try:
                proof = provider_run_scope_proof_from_verified_artifact(
                    envelope.to_dict(),
                    artifact_id=record.artifact_id,
                    artifact_role=record.artifact_role,
                    artifact_status=record.status,
                    artifact_sha256=record.content_hash,
                )
                validate_provider_run_scope_authority(
                    controller,
                    job_id=session.context.workspace.job_id,
                    scope_hashes=scope_hashes,
                    workspace_identity_sha256=workspace_identity_sha256,
                    current_executable_sha=current_executable_sha,
                    authorization=accepted_scope_authorization,
                    proof=proof,
                )
            except Exception as exc:
                raise RuntimeRunnerError(
                    f"fresh accepted run-scope envelope proof is invalid: {exc}"
                ) from exc
        from runtime.arbitration_work_policy import publish_arbitration_surface

        session.stage_host.arbitration_work_policy_record = publish_arbitration_surface(
            registry=session.context.registry, workspace=session.context.workspace,
            publication_context=session.context.publication_context,
            envelope_record=record, route_plan=route_plan.to_dict(),
            candidate_count=session.context.settings.outline_candidate_count(),
            stage1_work_item_basis=session.context.stage1_work_item_basis,
        )

    def _execute_with_lease(
        self,
        *,
        session: AgentRuntimeSession,
        spec: RuntimeJobSpec,
        bridge: AgentRuntimeBridge,
        batch_spec: Any | None,
        validated_batch_parent_registry_path: str | None,
        resume: bool,
        normalized_spec_payload: Mapping[str, Any],
        config_source_id: str,
        config_source_sha256: str,
    ) -> RuntimeExecutionResult:
        if file_sha256(config_source_id) != config_source_sha256:
            raise RuntimeRunnerError("configuration source changed before runtime publication")
        normalized_spec_path = session.context.workspace.artifact_path("runtime_job_spec_v1.json")
        attempt_store = AttemptStore(session.context.workspace, session.context.registry)
        started = attempt_store.start(
            job_id=session.context.workspace.job_id,
            producer="runtime.runner.AgentRuntimeRunner",
        )
        running_attempt = started.attempt
        session = replace(
            session,
            context=replace(
                session.context,
                attempt_number=running_attempt.attempt_number,
                resumed_from_attempt=running_attempt.resumed_from_attempt,
            ),
        )
        completed: list[str] = []
        failed_stage: str | None = None
        results: dict[str, StageResult] = {}
        active_stage = "source_intake"
        external_registry_paths = (
            (validated_batch_parent_registry_path,)
            if validated_batch_parent_registry_path is not None
            else ()
        )
        if bridge.source_revision_plan is not None:
            external_registry_paths = (*external_registry_paths,
                                       str(bridge.source_revision_plan["parent"]["registry_path"]))
        external_registry_resolver = self._external_registry_resolver(
            session.context.workspace,
            registry_paths=external_registry_paths,
        )

        publish_running_job_runtime(
            session.context,
            claim_latest_pointer=not resume,
        )
        if not resume:
            publish_json_artifact(
                session.context.publication_context,
                session.context.registry,
                normalized_spec_path,
                normalized_spec_payload,
                artifact_role="runtime_spec",
                artifact_type="runtime_job_spec",
                artifact_version="v1",
                producer="runtime.runner.AgentRuntimeRunner",
                artifact_id="runtime_job_spec",
                metadata={
                    "job_id": session.context.workspace.job_id,
                    "stage_plan_hash": str(
                        (normalized_spec_payload.get("metadata") or {}).get("stage_plan_hash") or ""
                    ),
                    "config_snapshot_binding": {
                        "schema_version": "runtime-config-source/v1",
                        "config_source_id": config_source_id,
                        "config_source_sha256": config_source_sha256,
                        "effective_config_sha256": str(session.context.fingerprint_bundle["config_hash"]),
                        "normalized_spec_payload_sha256": hash_json(normalized_spec_payload),
                    },
                },
            )
        self._fault("after_artifact_write_before_registry", artifact_type="runtime_job_spec")

        try:
            active_stage = "run_admission"
            self._admit_run_provider_envelope(session, spec)
            bridge.bind_source_revision_replay(session, external_registry_resolver)
            active_stage = "source_intake"
            bundle = bridge.build_source_bundle()
            if (
                session.context.stage1_work_item_basis is not None
                and stage1_paper_work_item_basis(bundle)
                != dict(session.context.stage1_work_item_basis)
            ):
                raise RuntimeRunnerError(
                    "Stage 1 source work items changed after run admission"
                )
            f1_binding = spec.metadata.get("f1_corpus_binding")
            if f1_binding is not None:
                from runtime.source_intake import validate_f1_corpus_source_bundle

                if not isinstance(f1_binding, Mapping):
                    raise RuntimeRunnerError("f1_corpus_binding must be a mapping")
                # This happens before source publication, preprocessing, OCR,
                # MinerU, or provider construction. The helper infers C/D/Q
                # from the bound selection cardinality and reopens every PDF.
                bundle = validate_f1_corpus_source_bundle(
                    bundle,
                    binding=f1_binding,
                )
            reconciler = RuntimeReconciler(
                session.context.workspace,
                session.context.registry,
                schema_validators=self._schema_validators(),
                external_registry_resolver=external_registry_resolver,
            )
            source_result = StageResult(
                stage_name="source_intake",
                success=True,
                artifacts=[bridge.persist_source_bundle(session, bundle)],
                metadata={"canonical_ready": session.context.source_canonical_ready},
            )
            results["source_intake"] = source_result
            self._persist_terminal(
                session,
                attempt_id=running_attempt.attempt_id,
                stage_name="source_intake",
                result=source_result,
                started_at=running_attempt.started_at or utc_now_iso(),
                model_call_count=0,
                external_registry_resolver=external_registry_resolver,
            )
            if not session.context.source_canonical_ready:
                attempt_store.finish(running_attempt, "succeeded", reason="source quarantined for review")
                return self._finalize_result(
                    session,
                    status="completed",
                    disposition="needs_review",
                    canonical_ready=False,
                    completed=(),
                    failed_stage=None,
                    message="source identity or inventory requires review; generation was not executed",
                )
            completed.append("source_intake")

            if batch_spec is not None:
                batch_stage = (
                    "derive_review_batch" if batch_spec.is_multi_variant else "analyze"
                )
                active_stage = batch_stage
                recovered_batch = (
                    reconciler.load_completed_stage_result(batch_stage) if resume else None
                )
                if recovered_batch is not None:
                    results[batch_stage] = recovered_batch
                    completed.append(batch_stage)
                    if batch_stage == "analyze":
                        self._restore_analyze_host_summaries(
                            session, recovered_batch,
                            external_registry_resolver=external_registry_resolver,
                        )
                else:
                    derived = bridge.derive_review_batch(
                        session,
                        batch_spec,
                        derivation_id=running_attempt.attempt_id,
                    )
                    results[batch_stage] = derived
                    self._persist_terminal(
                        session,
                        attempt_id=running_attempt.attempt_id,
                        stage_name=batch_stage,
                        result=derived,
                        started_at=utc_now_iso(),
                        model_call_count=0,
                        external_registry_resolver=external_registry_resolver,
                    )
                    if not derived.success:
                        failed_stage = batch_stage
                        raise RuntimeRunnerError("review batch derivation has failed variants")
                    completed.append(batch_stage)

            for stage in self._requested_stages(spec):
                active_stage = stage
                if stage == "review" and spec.action == "run_all":
                    from outline.adoption_transaction import current_adoption_record

                    adoption = current_adoption_record(session.context.registry)
                    if adoption is None or adoption.status != "ready":
                        reason = "outline is ready_for_adoption; explicit adoption is required before review"
                        attempt_store.finish(running_attempt, "succeeded", reason=reason)
                        return self._finalize_result(
                            session,
                            status="completed",
                            disposition="needs_review",
                            canonical_ready=False,
                            completed=tuple(completed),
                            failed_stage=None,
                            requires_attention=True,
                            message=reason,
                        )
                if stage in results:
                    continue
                if resume:
                    if stage == "validate":
                        promoted_validation = self._recover_promoted_repair_validation(
                            session, external_registry_resolver=external_registry_resolver,
                        )
                        if promoted_validation is not None:
                            results[stage] = promoted_validation
                            completed.append(stage)
                            continue
                    recovered_result = reconciler.load_completed_stage_result(stage)
                    if recovered_result is None and stage == "analyze":
                        from runtime.stage1_publication_recovery import (
                            Stage1PublicationRecoveryBlocked,
                            try_recover_registered_stage1_publication,
                        )
                        from runtime.provider_runtime import (
                            provider_budget_controller_from_environment,
                        )

                        source_bundle_record = session.context.registry.get("source_bundle")
                        runtime_spec_record = session.context.registry.get("runtime_job_spec")
                        try:
                            recovery_proposal = try_recover_registered_stage1_publication(
                                session.context.workspace,
                                session.context.registry,
                                expected_source_bundle_hash=(
                                    source_bundle_record.content_hash
                                    if source_bundle_record is not None
                                    and source_bundle_record.status == "ready"
                                    else ""
                                ),
                                expected_runtime_spec_hash=(
                                    runtime_spec_record.content_hash
                                    if runtime_spec_record is not None
                                    and runtime_spec_record.status == "ready"
                                    else ""
                                ),
                                provider_budget_controller=(
                                    provider_budget_controller_from_environment()
                                ),
                                external_registry_resolver=external_registry_resolver,
                                schema_validators=self._schema_validators(),
                            )
                        except Stage1PublicationRecoveryBlocked as exc:
                            raise RuntimeRunnerError(str(exc)) from exc
                        if recovery_proposal is not None:
                            recovery_result = reconciler.reconcile(
                                stage_recoveries=(recovery_proposal,),
                            )
                            if stage not in recovery_result.completed_stages:
                                raise RuntimeRunnerError(
                                    "Stage 1 recovery did not register a completed Analyze terminal"
                                )
                            recovered_result = reconciler.load_completed_stage_result(stage)
                            if recovered_result is None:
                                raise RuntimeRunnerError(
                                    "Stage 1 recovery terminal could not be rehydrated"
                                )
                    if recovered_result is not None:
                        if stage == "analyze":
                            self._restore_analyze_host_summaries(
                                session, recovered_result,
                                external_registry_resolver=external_registry_resolver,
                            )
                        results[stage] = recovered_result
                        completed.append(stage)
                        continue
                started_at = utc_now_iso()
                result, model_calls = self._execute_stage(
                    stage,
                    bridge=bridge,
                    session=session,
                    spec=spec,
                    bundle=bundle,
                    results=results,
                    attempt_id=running_attempt.attempt_id,
                    external_registry_resolver=external_registry_resolver,
                )
                if stage == "analyze" and result.success:
                    external_registry_resolver = self._external_registry_resolver(
                        session.context.workspace, registry_paths=external_registry_paths,
                    )
                    reconciler.external_registry_resolver = external_registry_resolver
                self._persist_terminal(
                    session,
                    attempt_id=running_attempt.attempt_id,
                    stage_name=(
                        "outline_topic_pilot"
                        if stage == "outline" and result.metadata.get("pilot_checkpoint_complete")
                        else stage
                    ),
                    result=result,
                    started_at=started_at,
                    model_call_count=model_calls,
                    external_registry_resolver=external_registry_resolver,
                )
                if stage == "outline" and result.metadata.get("pilot_checkpoint_complete"):
                    if (
                        not isinstance(spec.metadata.get("outline_pilot"), Mapping)
                        or not result.success
                        or result.stage_name != "outline_topic_pilot"
                        or result.metadata.get("outline_v3_status") != "topic_pilot_complete"
                    ):
                        raise RuntimeRunnerError("topic pilot stage result has an invalid terminal shape")
                    checkpoint_id = str(result.metadata.get("pilot_checkpoint_artifact_id") or "")
                    checkpoint = session.context.registry.get(checkpoint_id)
                    if (
                        checkpoint is None
                        or checkpoint.status != "ready"
                        or checkpoint.content_hash != result.metadata.get("pilot_checkpoint_hash")
                        or checkpoint_id == "outline-v3:final_outline"
                    ):
                        raise RuntimeRunnerError("topic pilot checkpoint is not ready or has drifted")
                    reason = (
                        "selected topic pilot checkpoint complete; Outline remains incomplete "
                        "and requires separate approval before continuation"
                    )
                    attempt_store.finish(running_attempt, "succeeded", reason=reason)
                    return self._finalize_result(
                        session,
                        status="completed",
                        disposition="needs_review",
                        canonical_ready=False,
                        completed=(*completed, "outline_topic_pilot"),
                        failed_stage=None,
                        requires_attention=True,
                        message=reason,
                    )
                if not result.success:
                    validation_required = bool(
                        session.context.readiness_policy_snapshot.get("validation_required", False)
                    )
                    if stage != "validate" or validation_required:
                        raise RuntimeRunnerError(f"stage failed: {stage}")
                    results[stage] = result
                    continue
                results[stage] = result
                completed.append(stage)
                if stage == "review" and "validate" not in self._requested_stages(spec):
                    self._publish_optional_current_artifact_set(session)

            disposition = "unvalidated"
            policy = session.context.readiness_policy_snapshot
            validation_required = bool(policy.get("validation_required", False))
            require_clean_validation = bool(policy.get("require_clean_validation", validation_required))
            allow_optional_unvalidated = bool(
                policy.get("allow_unvalidated_when_validation_optional", not validation_required)
            )
            validation = results.get("validate")
            if validation is not None and validation.success:
                disposition = str(validation.metadata.get("validation_disposition") or "unvalidated")
            elif validation is not None:
                disposition = "unvalidated"
            canonical_ready = bool(
                session.context.source_canonical_ready
                and set(session.context.required_stages).issubset(completed)
                and (
                    disposition == "clean"
                    or (disposition == "findings" and not require_clean_validation)
                    or (
                        disposition == "unvalidated"
                        and not validation_required
                        and allow_optional_unvalidated
                    )
                )
            )
            requires_attention = bool(
                disposition in {"findings", "needs_review"} or not canonical_ready
            )
            attempt_store.finish(running_attempt, "succeeded")
            return self._finalize_result(
                session,
                status="completed",
                disposition=disposition,
                canonical_ready=canonical_ready,
                completed=tuple(completed),
                failed_stage=None,
                requires_attention=requires_attention,
                message="completed",
            )
        except SystemExit as exc:
            try:
                history = attempt_store.load_history()
            except Exception as inspection_error:
                raise exc from inspection_error
            if not history or history[-1] != running_attempt:
                raise
            failed_stage = failed_stage or active_stage
            try:
                attempt_store.finish(
                    running_attempt,
                    "cancelled",
                    reason=str(exc) or type(exc).__name__,
                )
                self._finalize_result(
                    session,
                    status="cancelled",
                    disposition="unvalidated",
                    canonical_ready=False,
                    completed=tuple(completed),
                    failed_stage=failed_stage,
                    requires_attention=True,
                    message=str(exc) or type(exc).__name__,
                )
            except Exception as persistence_error:
                raise exc from persistence_error
            raise
        except (KeyboardInterrupt, JobCancelledError, ProviderCancellationRequested) as exc:
            try:
                history = attempt_store.load_history()
            except Exception as inspection_error:
                raise exc from inspection_error
            if not history or history[-1] != running_attempt:
                raise
            failed_stage = failed_stage or active_stage
            try:
                attempt_store.finish(
                    running_attempt,
                    "cancelled",
                    reason=str(exc) or type(exc).__name__,
                )
            except Exception as finish_error:
                raise exc from finish_error
            return self._finalize_result(
                session,
                status="cancelled",
                disposition="unvalidated",
                canonical_ready=False,
                completed=tuple(completed),
                failed_stage=failed_stage,
                requires_attention=True,
                message=str(exc) or type(exc).__name__,
            )
        except Exception as exc:
            try:
                history = attempt_store.load_history()
            except Exception as inspection_error:
                raise exc from inspection_error
            if not history or history[-1] != running_attempt:
                raise
            failed_stage = failed_stage or active_stage
            try:
                attempt_store.finish(running_attempt, "failed", reason=str(exc))
            except Exception as finish_error:
                raise exc from finish_error
            return self._finalize_result(
                session,
                status="failed",
                disposition="unvalidated",
                canonical_ready=False,
                completed=tuple(completed),
                failed_stage=failed_stage,
                requires_attention=True,
                message=str(exc),
            )

    def _finalize_result(
        self,
        session: AgentRuntimeSession,
        *,
        status: str,
        disposition: str,
        canonical_ready: bool,
        completed: tuple[str, ...],
        failed_stage: str | None,
        message: str,
        requires_attention: bool = True,
    ) -> RuntimeExecutionResult:
        self._fault("after_stage_terminal_before_job_outcome", status=status)
        finalize_job_runtime(
            context=session.context,
            write_resume_report=session.runner._write_resume_report,
            status=status,
            job_disposition=disposition,  # type: ignore[arg-type]
            canonical_ready=canonical_ready,
            requires_attention=requires_attention,
            completed_stages=completed,
            failed_stage=failed_stage,
            before_latest_pointer=lambda _outcome: self._fault(
                "after_report_write_before_pointer",
                status=status,
            ),
        )
        try:
            outcome, outcome_record = load_canonical_job_outcome(session.context.registry)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeRunnerError(
                f"canonical job outcome is invalid after finalization: {exc}"
            ) from exc
        evaluation = _evaluate_runtime_completion(
            outcome, session.context.registry,
            external_registry_resolver=self._external_registry_resolver(
                session.context.workspace, registry_paths=self._review_batch_registry_paths(session.context.registry),
            ),
        )
        stage1_projection = _stage1_status_projection(session.context.registry)
        return RuntimeExecutionResult(
            job_id=outcome.job_id,
            workspace_path=session.context.workspace.root_dir,
            job_status=outcome.job_status,
            job_disposition=outcome.job_disposition,
            canonical_ready=outcome.canonical_ready and evaluation.canonical_ready,
            requires_attention=outcome.requires_attention or evaluation.requires_attention,
            attempt_number=outcome.attempt_number,
            resumed_from_attempt=outcome.resumed_from_attempt,
            completed_stages=outcome.completed_stages,
            failed_stage=outcome.failed_stage,
            job_outcome_path=outcome_record.path,
            message=message,
            completion_status=evaluation.status,
            completion_reasons=evaluation.reasons,
            completion_evidence_hash=evaluation.evidence_hash,
            **stage1_projection,
        )

    @staticmethod
    def _workspace_from_path(workspace_path: str | Path) -> JobWorkspace:
        path = Path(workspace_path).expanduser().resolve()
        if not path.is_dir() or "__" not in path.name:
            raise RuntimeRunnerError(f"invalid job workspace path: {path}")
        project_name, job_id = path.name.rsplit("__", 1)
        if not project_name or not job_id:
            raise RuntimeRunnerError(f"workspace name must be <project>__<job_id>: {path.name}")
        return JobWorkspace(str(path.parent), project_name, job_id)

    @classmethod
    def _open_workspace(cls, workspace_path: str | Path) -> tuple[JobWorkspace, ArtifactRegistry]:
        workspace = cls._workspace_from_path(workspace_path)
        job_id = workspace.job_id
        return workspace, ArtifactRegistry(workspace.paths.registry_path, job_id)

    @staticmethod
    def _review_batch_registry_paths(registry: ArtifactRegistry) -> tuple[str, ...]:
        """Bootstrap external Registry paths from hash-verified batch manifests."""

        paths: list[str] = []
        for record in registry.list_records():
            if (
                record.job_id != registry.job_id
                or record.artifact_role != "review_batch_manifest"
                or record.artifact_type != "review_batch_manifest"
                or record.artifact_version != "v1"
                or record.status != "ready"
                or not record.content_hash
            ):
                continue
            manifest_path = Path(record.path)
            try:
                if not manifest_path.is_file() or file_sha256(manifest_path) != record.content_hash:
                    continue
                payload = validate_review_batch_manifest_for_bootstrap(
                    record,
                    manifest_path,
                    registry,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ReconcileValidationError):
                continue
            if (
                not isinstance(payload, Mapping)
                or str(payload.get("coordinator_job_id") or "") != registry.job_id
            ):
                continue
            parent = payload.get("parent")
            if isinstance(parent, Mapping) and str(parent.get("registry_path") or "").strip():
                paths.append(str(parent["registry_path"]))
            variants = payload.get("variants")
            if isinstance(variants, list):
                paths.extend(
                    str(variant["child_registry_path"])
                    for variant in variants
                    if isinstance(variant, Mapping)
                    and str(variant.get("status") or "") == "completed"
                    and str(variant.get("child_registry_path") or "").strip()
                )
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _verified_current_attempt_message(
        workspace: JobWorkspace,
        registry: ArtifactRegistry,
        outcome: JobOutcomeV1,
    ) -> str:
        """Read a failure reason only from the verified current attempt head."""

        if outcome.job_status != "failed":
            return ""
        try:
            store = AttemptStore(workspace, registry)
            history = store.load_history()
            for sequence, snapshot in enumerate(history, start=1):
                artifact_id = store._artifact_id(snapshot, sequence)
                record = registry.get(artifact_id)
                if (
                    record is None
                    or record.artifact_id != artifact_id
                    or record.artifact_role != ATTEMPT_SNAPSHOT_ROLE
                    or record.artifact_type != "job_attempt"
                    or record.artifact_version != "v1"
                    or record.job_id != outcome.job_id
                    or record.status != "ready"
                    or not isinstance(record.metadata, Mapping)
                ):
                    return ""
                expected_metadata = {
                    "attempt_id": snapshot.attempt_id,
                    "attempt_number": snapshot.attempt_number,
                    "attempt_status": snapshot.status,
                    "snapshot_sequence": sequence,
                }
                if any(
                    record.metadata.get(name) != value
                    for name, value in expected_metadata.items()
                ):
                    return ""
                expected_path = store._snapshot_path(sequence).resolve(strict=True)
                if Path(record.path).expanduser().resolve(strict=True) != expected_path:
                    return ""
                ArtifactRegistry._verify_ready_artifact(record)
                payload = json.loads(expected_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, Mapping)
                    or type(payload.get("snapshot_sequence")) is not int
                    or payload.get("snapshot_sequence") != sequence
                    or payload != {**snapshot.to_dict(), "snapshot_sequence": sequence}
                ):
                    return ""
        except (OSError, RuntimeError, TypeError, ValueError):
            # Status remains available from the canonical JobOutcome, but an
            # unverified or corrupt attempt chain cannot supply its message.
            return ""

        if not history:
            return ""
        current = history[-1]
        if (
            current.job_id != outcome.job_id
            or current.attempt_number != outcome.attempt_number
            or current.status != "failed"
            or outcome.job_status != "failed"
        ):
            return ""
        return current.terminal_reason.strip()

    @classmethod
    def status(cls, workspace_path: str | Path) -> RuntimeExecutionResult:
        """Read the canonical job head without mutating workspace state."""

        workspace, _registry = cls._open_workspace(workspace_path)
        try:
            outcome, outcome_record = load_canonical_job_outcome(_registry)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeRunnerError(f"canonical job outcome is invalid: {exc}") from exc
        outcome_path = Path(outcome_record.path)
        if outcome.job_id != workspace.job_id:
            raise RuntimeRunnerError("job outcome belongs to another workspace")
        evaluation = _evaluate_runtime_completion(
            outcome, _registry,
            external_registry_resolver=cls._external_registry_resolver(workspace, registry_paths=cls._review_batch_registry_paths(_registry)),
        )
        stage1_projection = _stage1_status_projection(_registry)
        message = cls._verified_current_attempt_message(workspace, _registry, outcome)
        return RuntimeExecutionResult(
            job_id=outcome.job_id,
            workspace_path=workspace.root_dir,
            job_status=outcome.job_status,
            job_disposition=outcome.job_disposition,
            canonical_ready=outcome.canonical_ready and evaluation.canonical_ready,
            requires_attention=outcome.requires_attention or evaluation.requires_attention,
            attempt_number=outcome.attempt_number,
            resumed_from_attempt=outcome.resumed_from_attempt,
            completed_stages=outcome.completed_stages,
            failed_stage=outcome.failed_stage,
            job_outcome_path=str(outcome_path),
            message=message,
            completion_status=evaluation.status,
            completion_reasons=evaluation.reasons,
            completion_evidence_hash=evaluation.evidence_hash,
            **stage1_projection,
        )

    @classmethod
    def reconcile(cls, workspace_path: str | Path) -> ReconcileResult:
        """Repair only durable projections; this surface has no provider input."""

        workspace, registry = cls._open_workspace(workspace_path)
        external_registry_paths = cls._review_batch_registry_paths(registry)
        reconciler = RuntimeReconciler(
            workspace,
            registry,
            schema_validators=cls._schema_validators(),
            external_registry_resolver=cls._external_registry_resolver(
                workspace,
                registry_paths=external_registry_paths,
            ),
        )
        AttemptStore(workspace, registry).register_orphaned_snapshots()
        return reconciler.reconcile()

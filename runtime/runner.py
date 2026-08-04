from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime.attempt_store import (
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
from runtime.stage_contracts import SourceBundle, StageResult
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_outcome import JobOutcomeV1
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.queue_service import JobCancelledError
from services.queue_service import CancelToken


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

    @property
    def success(self) -> bool:
        """Return whether the canonical outcome is ready for downstream use."""

        return self.canonical_ready


def _evaluate_runtime_completion(
    outcome: JobOutcomeV1,
    registry: ArtifactRegistry,
) -> CompletionEvaluationV1:
    """Read and verify the durable evidence used by status projections."""

    registry_verified = True
    ready_job_outcome = False
    validation_record = False
    required_provider_stages = {"outline", "review"}.intersection(outcome.required_stages)
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

        current_stage_closure_map = resolve_current_stage_closure_map(registry).to_dict()
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
                provider_receipt_closure = dict(candidate)
                provider_receipts_complete = bool(candidate.get("complete"))
            else:
                provider_receipts_complete = False
        elif required_provider_stages:
            provider_receipts_complete = False
    except (OSError, RegistryError, TypeError, ValueError):
        registry_verified = False
        provider_receipts_complete = False

    policy = dict(outcome.readiness_policy_snapshot)
    validation_status = "clean" if outcome.job_disposition == "clean" and validation_record else (
        "findings" if outcome.job_disposition == "findings" else "missing"
    )
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
            "validation_status": validation_status,
            "provider_receipts_complete": provider_receipts_complete,
            "provider_receipt_closure": provider_receipt_closure,
            "current_stage_closure_map": current_stage_closure_map,
            "declared_canonical_ready": outcome.canonical_ready,
            "degradation_reasons": outcome.degradation_reasons,
            "evidence_sources": ("job_outcome_v1", "artifact_registry"),
        }
    )


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

        validators = dict(DEFAULT_SCHEMA_VALIDATORS)
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
    ) -> None:
        resolved = job_spec.resolved_from(origin_dir) if origin_dir is not None else job_spec
        self._require_explicit_path_origins(resolved)
        resolved.validate()
        self.job_spec = resolved
        self.fault_injector = fault_injector
        self.cancel_token = cancel_token

    @staticmethod
    def _require_explicit_path_origins(spec: RuntimeJobSpec) -> None:
        values = [
            spec.config,
            spec.queue_file,
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
        metadata = dict(self.job_spec.metadata)
        requested_stages = metadata.get("requested_stages")
        if requested_stages is not None:
            metadata["requested_stages"] = list(
                dict.fromkeys(str(item) for item in requested_stages)
            )
        return replace(
            self.job_spec,
            job_id=self.job_spec.job_id or JobWorkspace.generate_job_id(),
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
    ) -> None:
        path = Path(workspace.artifact_path("runtime_job_spec_v1.json"))
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
        cache: dict[str, ArtifactRegistry | None] = {}
        explicit_paths: dict[str, list[Path]] = {}
        for value in registry_paths:
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
                cache[job_id] = None
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
            for output_ref in output_refs:
                reconciler.validate_dependency_ref(output_ref)
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

    def run(self) -> RuntimeExecutionResult:
        return self._execute(resume=False)

    def resume(self) -> RuntimeExecutionResult:
        return self._execute(resume=True)

    def _execute(self, *, resume: bool) -> RuntimeExecutionResult:
        spec = self._normalized_spec(resume=resume)
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
        bridge = AgentRuntimeBridge(spec)
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
                    self._validate_persisted_spec(workspace, normalized_spec_payload)
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
            return self._execute_with_lease(
                session=session,
                spec=spec,
                bridge=bridge,
                batch_spec=batch_spec,
                validated_batch_parent_registry_path=validated_batch_parent_registry_path,
                resume=resume,
                normalized_spec_payload=normalized_spec_payload,
            )
        finally:
            execution_lease.release()

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
    ) -> RuntimeExecutionResult:
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
        external_registry_resolver = self._external_registry_resolver(
            session.context.workspace,
            registry_paths=external_registry_paths,
        )

        publish_running_job_runtime(
            session.context,
            claim_latest_pointer=not resume,
        )
        if not resume:
            atomic_write_json(normalized_spec_path, normalized_spec_payload)
        self._fault("after_artifact_write_before_registry", artifact_type="runtime_job_spec")
        session.context.registry.register_file(
            artifact_role="runtime_spec",
            artifact_type="runtime_job_spec",
            artifact_version="v1",
            path=normalized_spec_path,
            producer="runtime.runner.AgentRuntimeRunner",
            artifact_id="runtime_job_spec",
        )

        try:
            bundle = bridge.build_source_bundle()
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
                        summary_artifact = next(
                            (
                                artifact
                                for artifact in recovered_batch.artifacts
                                if artifact.artifact_type == "summary_file"
                            ),
                            None,
                        )
                        if summary_artifact is not None:
                            session.stage_host.summary_file = summary_artifact.path
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
                if stage in results:
                    continue
                if resume:
                    recovered_result = reconciler.load_completed_stage_result(stage)
                    if recovered_result is not None:
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
                self._persist_terminal(
                    session,
                    attempt_id=running_attempt.attempt_id,
                    stage_name=stage,
                    result=result,
                    started_at=started_at,
                    model_call_count=model_calls,
                    external_registry_resolver=external_registry_resolver,
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
        except (KeyboardInterrupt, JobCancelledError) as exc:
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
        payload = json.loads(Path(session.context.job_outcome_path).read_text(encoding="utf-8"))
        outcome = JobOutcomeV1.from_dict(payload)
        evaluation = _evaluate_runtime_completion(outcome, session.context.registry)
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
            job_outcome_path=session.context.job_outcome_path,
            message=message,
            completion_status=evaluation.status,
            completion_reasons=evaluation.reasons,
            completion_evidence_hash=evaluation.evidence_hash,
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

    @classmethod
    def status(cls, workspace_path: str | Path) -> RuntimeExecutionResult:
        """Read the canonical job head without mutating workspace state."""

        workspace, _registry = cls._open_workspace(workspace_path)
        outcome_path = Path(workspace.artifact_path("job_outcome_v1.json"))
        if not outcome_path.is_file():
            raise RuntimeRunnerError(f"job outcome is missing: {outcome_path}")
        else:
            payload = json.loads(outcome_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise RuntimeRunnerError("job outcome must be a JSON object")
            outcome = JobOutcomeV1.from_dict(payload)
        if outcome.job_id != workspace.job_id:
            raise RuntimeRunnerError("job outcome belongs to another workspace")
        evaluation = _evaluate_runtime_completion(outcome, _registry)
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
            message="",
            completion_status=evaluation.status,
            completion_reasons=evaluation.reasons,
            completion_evidence_hash=evaluation.evidence_hash,
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

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

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
    LegacyMigrationResult,
    ReconcileValidationError,
    ReconcileResult,
    RuntimeReconciler,
    project_legacy_workspace_outcome,
    validate_canonical_ai_summary,
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


class RuntimeRunnerError(RuntimeError):
    pass


class RuntimePathOriginError(RuntimeRunnerError):
    pass


class RuntimeStageHandler(Protocol):
    def __call__(self, stage_name: str, request: "RuntimeStageRequest") -> Any: ...


FaultInjector = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class RuntimeStageRequest:
    stage_name: str
    job_spec: RuntimeJobSpec
    source_bundle: SourceBundle
    workspace_path: str
    prior_results: Mapping[str, StageResult]


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
    compatibility_status: str = "native"
    message: str = ""
    completion_status: str = ""
    completion_reasons: tuple[str, ...] = ()
    completion_evidence_hash: str = ""

    @property
    def success(self) -> bool:
        """Legacy readiness projection; Queue must use job_status."""

        return self.canonical_ready


def _evaluate_runtime_completion(
    outcome: JobOutcomeV1,
    registry: ArtifactRegistry,
) -> CompletionEvaluationV1:
    """Read and verify the durable evidence used by status projections."""

    registry_verified = True
    ready_job_outcome = False
    validation_record = False
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
    except (OSError, RegistryError, TypeError, ValueError):
        registry_verified = False

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
            "provider_receipts_complete": True,
            "compatibility_status": outcome.compatibility_status,
            "declared_canonical_ready": outcome.canonical_ready,
            "degradation_reasons": outcome.degradation_reasons,
            "evidence_sources": ("job_outcome_v1", "artifact_registry"),
        }
    )


class AgentRuntimeRunner:
    """Single AI-native state machine layered on the existing bridge.

    Generation remains a host/subagent responsibility through ``stage_handler``;
    all lifecycle, persistence, validation, recovery, and reconciliation remain
    local and deterministic.
    """

    def __init__(
        self,
        job_spec: RuntimeJobSpec,
        *,
        legacy_main: Any,
        stage_handler: RuntimeStageHandler | None = None,
        validator_module: Any | None = None,
        origin_dir: str | Path | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        resolved = job_spec.resolved_from(origin_dir) if origin_dir is not None else job_spec
        self._require_explicit_path_origins(resolved)
        resolved.validate()
        self.job_spec = resolved
        self.legacy_main = legacy_main
        self.stage_handler = stage_handler
        self.validator_module = validator_module
        self.fault_injector = fault_injector

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

    def _call_handler(
        self,
        stage_name: str,
        *,
        spec: RuntimeJobSpec,
        bundle: SourceBundle,
        session: AgentRuntimeSession,
        results: Mapping[str, StageResult],
    ) -> Any:
        if self.stage_handler is None:
            raise RuntimeRunnerError(f"required generation stage has no host handler: {stage_name}")
        return self.stage_handler(
            stage_name,
            RuntimeStageRequest(
                stage_name=stage_name,
                job_spec=spec,
                source_bundle=bundle,
                workspace_path=session.context.workspace.root_dir,
                prior_results=dict(results),
            ),
        )

    @staticmethod
    def _validate_stage1_summaries(
        summaries: Any,
        source_bundle: SourceBundle,
    ) -> list[Mapping[str, Any]]:
        if not isinstance(summaries, (list, tuple)):
            raise RuntimeRunnerError("stage1 handler summaries must be a JSON array")
        if not source_bundle.paper_work_items:
            raise RuntimeRunnerError("stage1 requires at least one source work item")
        if len(summaries) != len(source_bundle.paper_work_items):
            raise RuntimeRunnerError(
                "stage1 summary count does not match the source work-item count"
            )

        expected = {
            item.canonical_paper_key: item for item in source_bundle.paper_work_items
        }
        if len(expected) != len(source_bundle.paper_work_items):
            raise RuntimeRunnerError("source bundle contains duplicate canonical paper keys")

        seen: set[str] = set()
        normalized: list[Mapping[str, Any]] = []
        for index, summary in enumerate(summaries):
            if not isinstance(summary, Mapping):
                raise RuntimeRunnerError(f"stage1 summary[{index}] must be a JSON object")
            if str(summary.get("status") or "").strip().lower() != "success":
                raise RuntimeRunnerError(
                    f"stage1 summary[{index}] is not a successful canonical result"
                )
            paper_info = summary.get("paper_info")
            if not isinstance(paper_info, Mapping):
                raise RuntimeRunnerError(f"stage1 summary[{index}] has no paper_info object")
            paper_key = str(paper_info.get("canonical_paper_key") or "").strip()
            source_paper_id = str(paper_info.get("source_paper_id") or "").strip()
            if not paper_key or paper_key not in expected:
                raise RuntimeRunnerError(
                    f"stage1 summary[{index}] canonical paper identity is not in the source bundle"
                )
            if paper_key in seen:
                raise RuntimeRunnerError(f"stage1 summary identity is duplicated: {paper_key}")
            expected_item = expected[paper_key]
            if not source_paper_id or source_paper_id != expected_item.source_paper_id:
                raise RuntimeRunnerError(
                    f"stage1 summary[{index}] source_paper_id does not match the source bundle"
                )
            try:
                validate_canonical_ai_summary(
                    summary.get("ai_summary"),
                    label=f"stage1 summary[{index}] ai_summary",
                )
            except ReconcileValidationError as exc:
                raise RuntimeRunnerError(str(exc)) from exc
            seen.add(paper_key)
            normalized.append(summary)

        if seen != set(expected):
            raise RuntimeRunnerError("stage1 summaries do not cover every source work item")
        return normalized

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
        if stage == "analyze":
            response = self._call_handler("stage1_analyze", spec=spec, bundle=bundle, session=session, results=results)
            if not isinstance(response, Mapping):
                raise RuntimeRunnerError("stage1 handler must return a mapping with summaries")
            summaries = self._validate_stage1_summaries(response.get("summaries"), bundle)
            return (
                bridge.persist_stage1_results(
                    session,
                    summaries,
                    source_items=list(response.get("source_items") or []),
                    rejected_candidates=list(response.get("rejected_candidates") or []),
                    subagent_run_id=str(response.get("subagent_run_id") or "") or None,
                ),
                int(response.get("model_call_count") or len(bundle.paper_work_items)),
            )
        if stage == "outline":
            response = self._call_handler("stage2_outline", spec=spec, bundle=bundle, session=session, results=results)
            payload = response if isinstance(response, Mapping) else {"outline_text": response}
            if bool(payload.get("use_generator_outline_v2", False)):
                if not session.generator.create_literature_review_outline():
                    raise RuntimeRunnerError("generator Outline v2 pipeline failed")
                if bool(payload.get("adopt_outline_v2", False)) and not session.generator.adopt_outline_v2(
                    adopted_by=str(payload.get("adopted_by") or "runtime-handler"),
                    reason=str(payload.get("adoption_reason") or "explicit runtime Outline v2 adoption"),
                ):
                    raise RuntimeRunnerError("generator Outline v2 adoption failed")
                record = session.context.registry.get("adopted_final_outline")
                if record is None or record.status != "ready":
                    raise RuntimeRunnerError("Outline v2 did not produce a ready adopted outline")
                return (
                    StageResult(
                        stage_name="stage2_outline",
                        success=True,
                        artifacts=[bridge._artifact_ref_from_record(record)],
                        metadata={
                            "outline_mode": "v2",
                            "stage_health_artifact_id": "outline_stage_health",
                        },
                    ),
                    int(payload.get("model_call_count") or 0),
                )
            outline_text = str(payload.get("outline_text") or "")
            if not outline_text.strip():
                raise RuntimeRunnerError("stage2 handler returned an empty outline")
            return (
                bridge.persist_outline(
                    session,
                    outline_text,
                    subagent_run_id=str(payload.get("subagent_run_id") or "") or None,
                ),
                int(payload.get("model_call_count") or 1),
            )
        if stage == "review":
            response = self._call_handler("stage3_review", spec=spec, bundle=bundle, session=session, results=results)
            if not isinstance(response, Mapping):
                raise RuntimeRunnerError("stage3 handler must return a mapping")
            outline_result = results.get("outline")
            outline_file = str(response.get("outline_file") or "")
            if not outline_file and outline_result and outline_result.artifacts:
                outline_file = outline_result.artifacts[0].path
            if not outline_file:
                adopted = session.context.registry.get("adopted_final_outline")
                legacy = session.context.registry.get("literature_review_outline")
                record = adopted or legacy
                if record is not None and record.status == "ready":
                    outline_file = record.path
            if not outline_file:
                raise RuntimeRunnerError("review requires an explicit registered outline dependency")
            self._record_for_ref(session, "", outline_file)
            sections = response.get("review_sections")
            if not isinstance(sections, list):
                raise RuntimeRunnerError("stage3 handler must return review_sections")
            return (
                bridge.persist_review_chain(
                    session,
                    outline_file=outline_file,
                    review_sections=sections,
                    references=list(response.get("references") or []),
                    rebuild_docx=bool(response.get("rebuild_docx", True)),
                    subagent_run_id=str(response.get("subagent_run_id") or "") or None,
                ),
                int(response.get("model_call_count") or 1),
            )
        if stage == "validate":
            result = bridge.run_validation(
                session,
                attempt_id=attempt_id,
                validator_module=self.validator_module,
                external_registry_resolver=external_registry_resolver,
            )
            return result, 0
        raise RuntimeRunnerError(f"unsupported runtime stage: {stage}")

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
                self.legacy_main,
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
                            session.generator.summary_file = summary_artifact.path
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
            compatibility_status=outcome.compatibility_status,
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
            outcome = project_legacy_workspace_outcome(workspace)
            if outcome is None:
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
            compatibility_status=outcome.compatibility_status,
            message=(
                "legacy_unverified workspace projection"
                if outcome.compatibility_status == "legacy_unverified"
                else ""
            ),
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
            external_registry_resolver=cls._external_registry_resolver(
                workspace,
                registry_paths=external_registry_paths,
            ),
        )
        legacy_result = reconciler.legacy_read_only_result()
        if legacy_result is not None:
            return legacy_result
        AttemptStore(workspace, registry).register_orphaned_snapshots()
        return reconciler.reconcile()

    @classmethod
    def migrate_legacy(
        cls,
        workspace_path: str | Path,
        actor: str,
        reason: str,
    ) -> LegacyMigrationResult:
        """Explicitly materialize a fail-closed, audited legacy workspace head."""

        if not actor.strip() or not reason.strip():
            raise RuntimeRunnerError("legacy migration requires actor and reason")
        workspace = cls._workspace_from_path(workspace_path)
        execution_lease = AttemptExecutionLease(workspace)
        try:
            execution_lease.acquire()
        except AttemptAlreadyRunningError as exc:
            raise RuntimeRunnerError(f"legacy migration rejected: {exc}") from exc
        try:
            try:
                registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
                return RuntimeReconciler(
                    workspace,
                    registry,
                    external_registry_resolver=cls._external_registry_resolver(workspace),
                ).migrate_legacy(actor=actor, reason=reason)
            except (ReconcileValidationError, RegistryError, TypeError, ValueError) as exc:
                raise RuntimeRunnerError(f"legacy migration rejected: {exc}") from exc
        finally:
            execution_lease.release()

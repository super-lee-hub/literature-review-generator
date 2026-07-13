from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from runtime.attempt_store import AttemptStore
from runtime.job_spec import RuntimeJobSpec
from runtime.lifecycle import finalize_job_runtime
from runtime.orchestrator import AgentRuntimeBridge, AgentRuntimeSession
from runtime.reconcile import ReconcileResult, RuntimeReconciler
from runtime.stage_contracts import SourceBundle, StageResult
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord
from services.job_outcome import JobOutcomeV1
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.queue_service import CancelToken, JobCancelledError


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
    completed_stages: tuple[str, ...]
    failed_stage: str | None
    job_outcome_path: str
    message: str = ""

    @property
    def success(self) -> bool:
        """Legacy readiness projection; Queue must use job_status."""

        return self.canonical_ready


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
        if self.job_spec.job_id:
            return self.job_spec
        return replace(self.job_spec, job_id=JobWorkspace.generate_job_id())

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
    def _requested_stages(spec: RuntimeJobSpec) -> tuple[str, ...]:
        explicit = spec.metadata.get("requested_stages")
        if explicit is not None:
            return tuple(str(item) for item in explicit if str(item) != "source_intake")
        mapping = {
            "analyze": ("analyze",),
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
    ) -> None:
        self._fault("after_registry_write_before_stage_terminal", stage_name=stage_name)
        record = TerminalStageRecordV1.create(
            job_id=session.context.workspace.job_id,
            attempt_id=attempt_id,
            stage_name=stage_name,
            status="succeeded" if result.success else "failed",
            producer="runtime.runner.AgentRuntimeRunner",
            output_artifact_refs=self._output_refs(session, result),
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

    def _execute_stage(
        self,
        stage: str,
        *,
        bridge: AgentRuntimeBridge,
        session: AgentRuntimeSession,
        spec: RuntimeJobSpec,
        bundle: SourceBundle,
        results: Mapping[str, StageResult],
    ) -> tuple[StageResult, int]:
        if stage == "analyze":
            response = self._call_handler("stage1_analyze", spec=spec, bundle=bundle, session=session, results=results)
            if not isinstance(response, Mapping) or not isinstance(response.get("summaries"), Sequence):
                raise RuntimeRunnerError("stage1 handler must return a mapping with summaries")
            return (
                bridge.persist_stage1_results(
                    session,
                    response["summaries"],
                    source_items=list(response.get("source_items") or []),
                    rejected_candidates=list(response.get("rejected_candidates") or []),
                    subagent_run_id=str(response.get("subagent_run_id") or "") or None,
                ),
                int(response.get("model_call_count") or len(bundle.paper_work_items)),
            )
        if stage == "outline":
            response = self._call_handler("stage2_outline", spec=spec, bundle=bundle, session=session, results=results)
            payload = response if isinstance(response, Mapping) else {"outline_text": response}
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
            result = bridge.run_validation(session, validator_module=self.validator_module)
            return result, 0
        raise RuntimeRunnerError(f"unsupported runtime stage: {stage}")

    def run(self) -> RuntimeExecutionResult:
        return self._execute(resume=False)

    def resume(self) -> RuntimeExecutionResult:
        return self._execute(resume=True)

    def _execute(self, *, resume: bool) -> RuntimeExecutionResult:
        spec = self._normalized_spec(resume=resume)
        batch_spec = self._review_batch_spec(spec)
        if batch_spec is not None and not spec.summary_sources:
            spec = replace(spec, summary_sources=(batch_spec.selection.parent_summary_path,))
        bridge = AgentRuntimeBridge(spec)
        session = bridge.bootstrap(self.legacy_main)
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
        normalized_spec_path = session.context.workspace.artifact_path("runtime_job_spec_v1.json")
        atomic_write_json(normalized_spec_path, spec.to_dict())
        self._fault("after_artifact_write_before_registry", artifact_type="runtime_job_spec")
        session.context.registry.register_file(
            artifact_role="runtime_spec",
            artifact_type="runtime_job_spec",
            artifact_version="v1",
            path=normalized_spec_path,
            producer="runtime.runner.AgentRuntimeRunner",
            artifact_id="runtime_job_spec",
        )
        completed: list[str] = []
        failed_stage: str | None = None
        results: dict[str, StageResult] = {}
        bundle = bridge.build_source_bundle()
        reconciler = RuntimeReconciler(session.context.workspace, session.context.registry)

        try:
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
                derived = bridge.derive_review_batch(session, batch_spec)
                results["analyze"] = derived
                self._persist_terminal(
                    session,
                    attempt_id=running_attempt.attempt_id,
                    stage_name="analyze",
                    result=derived,
                    started_at=utc_now_iso(),
                    model_call_count=0,
                )
                completed.append("analyze")

            for stage in self._requested_stages(spec):
                if stage == "analyze" and "analyze" in results:
                    continue
                if resume and reconciler.stage_is_complete(stage):
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
                )
                self._persist_terminal(
                    session,
                    attempt_id=running_attempt.attempt_id,
                    stage_name=stage,
                    result=result,
                    started_at=started_at,
                    model_call_count=model_calls,
                )
                if not result.success:
                    raise RuntimeRunnerError(f"stage failed: {stage}")
                results[stage] = result
                completed.append(stage)

            disposition = "unvalidated"
            canonical_ready = True
            requires_attention = False
            validation = results.get("validate")
            if validation is not None:
                disposition = str(validation.metadata.get("validation_disposition") or "unvalidated")
                canonical_ready = disposition == "clean"
                requires_attention = disposition in {"findings", "needs_review", "unvalidated"}
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
        except (KeyboardInterrupt, JobCancelledError) as exc:
            failed_stage = failed_stage or next(
                (stage for stage in self._requested_stages(spec) if stage not in completed),
                None,
            )
            attempt_store.finish(running_attempt, "cancelled", reason=str(exc) or type(exc).__name__)
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
            failed_stage = failed_stage or next(
                (stage for stage in self._requested_stages(spec) if stage not in completed),
                None,
            )
            attempt_store.finish(running_attempt, "failed", reason=str(exc))
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
        return RuntimeExecutionResult(
            job_id=outcome.job_id,
            workspace_path=session.context.workspace.root_dir,
            job_status=outcome.job_status,
            job_disposition=outcome.job_disposition,
            canonical_ready=outcome.canonical_ready,
            requires_attention=outcome.requires_attention,
            attempt_number=outcome.attempt_number,
            completed_stages=outcome.completed_stages,
            failed_stage=outcome.failed_stage,
            job_outcome_path=session.context.job_outcome_path,
            message=message,
        )

    @staticmethod
    def _open_workspace(workspace_path: str | Path) -> tuple[JobWorkspace, Any]:
        path = Path(workspace_path).expanduser().resolve()
        if not path.is_dir() or "__" not in path.name:
            raise RuntimeRunnerError(f"invalid job workspace path: {path}")
        project_name, job_id = path.name.rsplit("__", 1)
        if not project_name or not job_id:
            raise RuntimeRunnerError(f"workspace name must be <project>__<job_id>: {path.name}")
        workspace = JobWorkspace(str(path.parent), project_name, job_id)
        from services.artifact_registry import ArtifactRegistry

        return workspace, ArtifactRegistry(workspace.paths.registry_path, job_id)

    @classmethod
    def status(cls, workspace_path: str | Path) -> RuntimeExecutionResult:
        """Read the canonical job head without mutating workspace state."""

        workspace, _registry = cls._open_workspace(workspace_path)
        outcome_path = Path(workspace.artifact_path("job_outcome_v1.json"))
        if not outcome_path.is_file():
            raise RuntimeRunnerError(f"job outcome is missing: {outcome_path}")
        payload = json.loads(outcome_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise RuntimeRunnerError("job outcome must be a JSON object")
        outcome = JobOutcomeV1.from_dict(payload)
        return RuntimeExecutionResult(
            job_id=outcome.job_id,
            workspace_path=workspace.root_dir,
            job_status=outcome.job_status,
            job_disposition=outcome.job_disposition,
            canonical_ready=outcome.canonical_ready,
            requires_attention=outcome.requires_attention,
            attempt_number=outcome.attempt_number,
            completed_stages=outcome.completed_stages,
            failed_stage=outcome.failed_stage,
            job_outcome_path=str(outcome_path),
        )

    @classmethod
    def reconcile(cls, workspace_path: str | Path) -> ReconcileResult:
        """Repair only durable projections; this surface has no provider input."""

        workspace, registry = cls._open_workspace(workspace_path)
        AttemptStore(workspace, registry).register_orphaned_snapshots()
        return RuntimeReconciler(workspace, registry).reconcile()

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence, cast

from config_loader import load_config
from runtime.architecture_gates import ArchitectureGateScope, collect_scannable_paths, scan_paths_for_forbidden_patterns
from runtime.lifecycle import BootstrappedRuntimeContext, bootstrap_job_runtime, finalize_job_runtime
from runtime.job_spec import RuntimeJobSpec
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json
from runtime.reconcile import ReconcileValidationError, validate_canonical_ai_summary
from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_contracts import SourceBundle, StageArtifactRef, StageResult
from runtime.subagent_policy import ExecutionMode, build_runtime_stage_trace_entry, stage_policy_for
from validation.execution_service import ValidationExecutionService
from validation.repair_transaction import RepairPromotionTransaction, current_artifact_record
from outline.v3_executor import OutlineV3Executor
from outline.adoption_transaction import current_adoption_record
from services.model_capabilities import resolve_model_capability
from services.model_selection import get_outline_api_config
from services.settings import ApplicationSettings
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    file_sha256,
)
from services.job_runner import JobRunRequest, JobRunner, validate_job_request_options
from services.job_workspace import (
    JobWorkspace,
    atomic_write_json,
    publish_bytes_artifact,
    publish_json_artifact,
    utc_now_iso,
)
from services.progress_state import Stage1ProgressSnapshot
from services.queue_service import CancelToken
from services.stage1_analysis_service import Stage1AnalysisService


class _RuntimeStageHost:
    """Small runtime-owned host for stage services.

    The control plane owns the durable runtime contract, so stages receive
    this narrow host with current typed settings and workspace bindings.
    """

    PAPER_ARTIFACT_ROLE = "paper_summary"
    PAPER_ARTIFACT_TYPE = "paper_artifact"
    PAPER_ARTIFACT_VERSION = "v1"
    REVIEW_DRAFT_ARTIFACT_ROLE = "review_draft"
    REVIEW_DRAFT_ARTIFACT_TYPE = "review_draft"
    REVIEW_DRAFT_ARTIFACT_VERSION = "v3"
    REVIEW_DRAFT_ARTIFACT_ID = "review_draft"
    CITATION_MANIFEST_ARTIFACT_ROLE = "citation_manifest"
    CITATION_MANIFEST_ARTIFACT_TYPE = "citation_manifest"
    CITATION_MANIFEST_ARTIFACT_VERSION = "v3"
    CITATION_MANIFEST_ARTIFACT_ID = "citation_manifest_v3"

    def __init__(
        self,
        request: JobRunRequest,
        project_name: str,
        cancel_token: CancelToken,
    ) -> None:
        self.logger = logging.getLogger("auto_generate.runtime")
        self.config = load_config(request.config)
        if request.source_mode == "direct":
            # A direct job owns its source mode; configured Zotero defaults
            # must not silently override it during source-inventory planning.
            paths = dict(self.config.get("Paths", {}))
            paths["zotero_report"] = ""
            paths["library_path"] = ""
            self.config["Paths"] = paths
        self.settings = ApplicationSettings.from_config(self.config)
        self.project_name = project_name
        self.pdf_folder = request.pdf_folder
        self.queue_file = request.queue_file
        self.zotero_report = request.zotero_report
        self.library_path = request.library_path
        self.cancel_token = cancel_token
        self.progress_tracker = request.progress_tracker
        self.free_mode_profile_path = request.free_mode_profile
        self.free_mode_idea = request.free_mode_idea
        self.summary_file_override = request.summary_file
        self.summary_source_overrides = list(request.summary_sources)
        self.audit_actor = ""
        self.audit_reason = ""
        self.audit_scope: dict[str, Any] = {}
        self.reuse_stage1 = bool(request.reuse_stage1)
        self.reuse_summary_files = list(request.reuse_summary_files)
        self.summaries: list[dict[str, Any]] = []
        self.summary_file = ""
        self.artifact_registry: Any = None
        self.job_workspace: JobWorkspace | None = None
        self.workspace: JobWorkspace | None = None
        self._checkpoint_processed_papers: set[str] = set()
        self._checkpoint_failed_papers: set[str] = set()

    def check_cancelled(self) -> None:
        self.cancel_token.check_cancelled()

    def bind_job_workspace(
        self,
        *,
        workspace: JobWorkspace,
        artifact_registry: Any,
        settings: ApplicationSettings,
        fingerprint_bundle: Mapping[str, Any] | None = None,
        resume_state_report: Any | None = None,
    ) -> None:
        self.job_workspace = workspace
        self.workspace = workspace
        self.artifact_registry = artifact_registry
        self.settings = settings
        current_summary = artifact_registry.get("summary_file")
        self.summary_file = (
            current_summary.path
            if current_summary is not None
            and current_summary.status == "ready"
            and Path(current_summary.path).is_file()
            else workspace.artifact_path(f"{self.project_name}_summaries.json")
        )
        current_progress = artifact_registry.get("stage1_progress_snapshot")
        self.progress_path = (
            current_progress.path
            if current_progress is not None
            and current_progress.status == "ready"
            and Path(current_progress.path).is_file()
            else workspace.artifact_path("stage1_progress_snapshot.json")
        )
        self.checkpoint_path = workspace.checkpoint_path(f"{self.project_name}_checkpoint.json")
        self.fingerprint_bundle = dict(fingerprint_bundle or {})
        self.resume_state_report = resume_state_report

    def _require_workspace(self) -> tuple[JobWorkspace, Any]:
        if self.job_workspace is None or self.artifact_registry is None:
            raise RuntimeError("runtime stage host is not bound to a job workspace")
        return self.job_workspace, self.artifact_registry

    @staticmethod
    def _publication_context(registry: Any) -> Any:
        from services.queue_service import LocalPublicationContext

        return getattr(registry, "publication_context", None) or LocalPublicationContext()

    @staticmethod
    def get_paper_key(paper: Mapping[str, Any]) -> str:
        return str(
            paper.get("canonical_paper_key")
            or paper.get("source_paper_id")
            or paper.get("title")
            or "unknown-paper"
        ).strip()

    def _paper_artifact_id(self, paper: Mapping[str, Any]) -> str:
        import hashlib

        digest = hashlib.sha256(self.get_paper_key(paper).encode("utf-8")).hexdigest()[:24]
        return f"paper:{digest}"

    def _paper_artifact_path(self, paper: Mapping[str, Any]) -> str:
        workspace, _registry = self._require_workspace()
        file_id = self._paper_artifact_id(paper).replace(":", "_")
        return workspace.artifact_path(f"paper_artifacts/{file_id}.json")

    def _persist_paper_artifact(self, result: Mapping[str, Any]) -> bool:
        _workspace, registry = self._require_workspace()
        paper = result.get("paper_info")
        if not isinstance(paper, Mapping):
            return False
        from services.paper_artifact import build_paper_artifact_v1

        path = self._paper_artifact_path(paper)
        paper_key = self.get_paper_key(paper)
        artifact = build_paper_artifact_v1(
            job_id=registry.job_id,
            paper=paper,
            result=result,
            paper_key=paper_key,
        )
        dependency_paths = {
            str(paper.get("source_pdf") or "").strip(),
        }
        preprocess = result.get("preprocess")
        if isinstance(preprocess, Mapping):
            dependency_paths.update(
                str(value).strip()
                for key, value in preprocess.items()
                if str(key).endswith("_path") and str(value).strip()
            )
        stage1_input = result.get("stage1_input")
        if isinstance(stage1_input, Mapping):
            dependency_paths.update(
                str(stage1_input.get(key) or "").strip()
                for key in ("visual_manifest_path", "visual_bundle_path")
            )
            dependency_paths.update(
                str(item.get("image_path") or "").strip()
                for item in (stage1_input.get("selected_visual_refs") or [])
                if isinstance(item, Mapping)
            )
        dependencies: list[ArtifactDependencyRefV2] = []
        for record in registry.list_records():
            if record.status != "ready" or not record.path:
                continue
            if str(Path(record.path).resolve()) not in {
                str(Path(value).expanduser().resolve())
                for value in dependency_paths
                if value
            }:
                continue
            dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=record.job_id,
                    artifact_id=record.artifact_id,
                    artifact_type=record.artifact_type,
                    path=record.path,
                    content_hash=record.content_hash,
                )
            )
        provider_record = registry.get("stage1_provider_receipts")
        if provider_record is not None and provider_record.status == "ready":
            dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=provider_record.job_id,
                    artifact_id=provider_record.artifact_id,
                    artifact_type=provider_record.artifact_type,
                    path=provider_record.path,
                    content_hash=provider_record.content_hash,
                )
            )
        publish_json_artifact(
            self._publication_context(registry),
            registry,
            path,
            artifact.to_dict(),
            artifact_role=self.PAPER_ARTIFACT_ROLE,
            artifact_type=self.PAPER_ARTIFACT_TYPE,
            artifact_version=self.PAPER_ARTIFACT_VERSION,
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id=self._paper_artifact_id(paper),
            depends_on=dependencies,
        )
        return True

    def save_summaries(self, *, depends_on: Sequence[Any] = ()) -> bool:
        _workspace, registry = self._require_workspace()
        summary_payload = list(self.summaries)
        summary_set_hash = hash_json(summary_payload)
        versioned_path = _workspace.artifact_path(
            f"stage1/inputs/stage1_summaries_{summary_set_hash[:24]}.json"
        )
        if Path(versioned_path).is_file():
            try:
                existing_payload = json.loads(Path(versioned_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"current Stage 1 summary artifact is unreadable: {versioned_path}") from exc
            if hash_json(existing_payload) != summary_set_hash:
                raise RuntimeError(f"content-addressed Stage 1 summary path has drifted: {versioned_path}")
        versioned_id = f"summary_file:{summary_set_hash}"
        versioned_record = publish_json_artifact(
            self._publication_context(registry),
            registry,
            versioned_path,
            summary_payload,
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id=versioned_id,
            depends_on=list(depends_on),
            metadata={
                "immutable": True,
                "summary_set_hash": summary_set_hash,
                "versioned_artifact_id": versioned_id,
            },
        )
        self.summary_file = versioned_record.path
        publish_json_artifact(
            self._publication_context(registry),
            registry,
            versioned_path,
            summary_payload,
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id="summary_file",
            depends_on=list(depends_on),
            metadata={
                "pointer_role": "current",
                "current_version_artifact_id": versioned_record.artifact_id,
                "summary_set_hash": summary_set_hash,
            },
        )
        progress = Stage1ProgressSnapshot(
            artifact_type="stage1_progress_snapshot",
            artifact_version="v1",
            created_from_job_id=registry.job_id,
            created_at=utc_now_iso(),
            project_name=self.project_name,
            job_id=registry.job_id,
            summary_file=self.summary_file,
            summary_count=len(self.summaries),
            processed_papers=sorted(self._checkpoint_processed_papers),
            failed_papers=sorted(self._checkpoint_failed_papers),
            fingerprint_bundle=dict(getattr(self, "fingerprint_bundle", {}) or {}),
            checkpoint_file=self.checkpoint_path,
        )
        progress_record = publish_json_artifact(
            self._publication_context(registry),
            registry,
            self.progress_path,
            asdict(progress),
            artifact_role="progress",
            artifact_type="stage1_progress_snapshot",
            artifact_version="v1",
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id="stage1_progress_snapshot",
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=registry.job_id,
                    artifact_id="summary_file",
                    artifact_type="summary_file",
                    path=self.summary_file,
                    content_hash=registry.get("summary_file").content_hash if registry.get("summary_file") else "",
                )
            ],
        )
        self.progress_path = progress_record.path
        return True

    def _get_summary_source_manifest_path(self) -> str:
        workspace, _registry = self._require_workspace()
        return workspace.artifact_path("summary_source_manifest.json")

    def _review_draft_path(self) -> str:
        workspace, _registry = self._require_workspace()
        return workspace.artifact_path("review_draft.json")

    def _citation_manifest_path(self) -> str:
        workspace, _registry = self._require_workspace()
        return workspace.artifact_path("citation_manifest_v3.json")

    def _citation_ref_catalog_path(self) -> str:
        workspace, _registry = self._require_workspace()
        return workspace.artifact_path("citation_ref_catalog.json")

    def _load_citation_ref_catalog(self) -> dict[str, Any]:
        path = Path(self._citation_ref_catalog_path())
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _get_review_word_file_path(self) -> str:
        workspace, _registry = self._require_workspace()
        return workspace.artifact_path(f"{self.project_name}_literature_review.docx")

    def _stage2_validation_enabled(self) -> bool:
        return self.settings.review_validation_enabled()

    def _persist_review_draft(
        self,
        *,
        outline_file: str,
        review_sections: Sequence[Mapping[str, Any]],
        references: Sequence[str],
        word_file: str,
        generation_mode: str,
        citation_ref_catalog: Mapping[str, Any] | None = None,
        citation_ref_catalog_path: str = "",
        citation_ref_catalog_hash: str = "",
    ) -> bool:
        from services.review_draft import build_review_draft

        _workspace, registry = self._require_workspace()
        draft = build_review_draft(
            job_id=registry.job_id,
            project_name=self.project_name,
            draft_id="review_draft",
            outline_artifact_id="outline-v3:final_outline",
            outline_source_path=outline_file,
            summary_file=self.summary_file,
            review_word_path=word_file,
            sections=review_sections,
            references=references,
            generation_mode=generation_mode,
            paper_summaries=list(self.summaries),
            citation_ref_catalog=citation_ref_catalog,
            citation_ref_catalog_path=citation_ref_catalog_path,
            citation_ref_catalog_hash=citation_ref_catalog_hash,
        )
        path = self._review_draft_path()
        dependencies: list[ArtifactDependencyRefV2] = []
        catalog_record = registry.get("citation_ref_catalog")
        if catalog_record is not None and catalog_record.status == "ready":
            dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=catalog_record.job_id,
                    artifact_id=catalog_record.artifact_id,
                    artifact_type=catalog_record.artifact_type,
                    path=catalog_record.path,
                    content_hash=catalog_record.content_hash,
                )
            )
        publish_json_artifact(
            self._publication_context(registry),
            registry,
            path,
            draft.to_dict(),
            artifact_role=self.REVIEW_DRAFT_ARTIFACT_ROLE,
            artifact_type=self.REVIEW_DRAFT_ARTIFACT_TYPE,
            artifact_version=self.REVIEW_DRAFT_ARTIFACT_VERSION,
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id="review_draft",
            depends_on=dependencies,
        )
        return True

    def _persist_citation_manifest(
        self,
        *,
        review_draft_path: str,
        review_word_path: str,
        citation_ref_catalog: Mapping[str, Any] | None = None,
        citation_ref_catalog_path: str = "",
        citation_ref_catalog_hash: str = "",
    ) -> bool:
        from services.citation_manifest import build_citation_manifest_from_review_draft

        _workspace, registry = self._require_workspace()
        review_draft = json.loads(Path(review_draft_path).read_text(encoding="utf-8"))
        catalog = dict(citation_ref_catalog or self._load_citation_ref_catalog())
        catalog_path = citation_ref_catalog_path or self._citation_ref_catalog_path()
        catalog_hash = citation_ref_catalog_hash or str(catalog.get("catalog_hash") or "")
        manifest = build_citation_manifest_from_review_draft(
            job_id=registry.job_id,
            project_name=self.project_name,
            manifest_id="citation_manifest",
            review_draft_path=review_draft_path,
            review_word_path=review_word_path,
            review_draft=review_draft,
            paper_summaries=list(self.summaries),
            citation_ref_catalog=catalog or None,
            citation_ref_catalog_path=catalog_path,
            citation_ref_catalog_hash=catalog_hash,
        )
        path = self._citation_manifest_path()
        draft_record = registry.get("review_draft")
        if draft_record is None or draft_record.status != "ready":
            raise RuntimeError("review draft must be registered before citation manifest publication")
        publish_json_artifact(
            self._publication_context(registry),
            registry,
            path,
            manifest.to_dict(),
            artifact_role=self.CITATION_MANIFEST_ARTIFACT_ROLE,
            artifact_type=self.CITATION_MANIFEST_ARTIFACT_TYPE,
            artifact_version=self.CITATION_MANIFEST_ARTIFACT_VERSION,
            producer="runtime.orchestrator._RuntimeStageHost",
            artifact_id=self.CITATION_MANIFEST_ARTIFACT_ID,
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=registry.job_id,
                    artifact_id="review_draft",
                    artifact_type="review_draft",
                    path=draft_record.path,
                    content_hash=draft_record.content_hash,
                ),
                *(
                    [
                        ArtifactDependencyRefV2(
                            dependency_kind="local_job",
                            job_id=catalog_record.job_id,
                            artifact_id=catalog_record.artifact_id,
                            artifact_type=catalog_record.artifact_type,
                            path=catalog_record.path,
                            content_hash=catalog_record.content_hash,
                        )
                    ]
                    if (catalog_record := registry.get("citation_ref_catalog")) is not None
                    and catalog_record.status == "ready"
                    else []
                ),
            ],
        )
        return True

    def _load_citation_manifest(self) -> dict[str, Any]:
        path = Path(self._citation_manifest_path())
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, Mapping) else {}


@dataclass(frozen=True)
class AgentRuntimeSession:
    runner: JobRunner
    request: JobRunRequest
    stage_host: _RuntimeStageHost
    context: BootstrappedRuntimeContext


class InternalStageExecutorRegistry:
    """Registry of the stage executors owned by the current runtime.

    Stage dispatch is intentionally closed over this registry.  Callers may
    choose a job action, but they cannot inject an arbitrary Python callback
    into the production control plane.
    """

    def __init__(self, bridge: "AgentRuntimeBridge") -> None:
        self.bridge = bridge

    @staticmethod
    def _summary_payloads_from_file(path: str | Path) -> list[dict[str, Any]]:
        target = Path(path).expanduser().resolve()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load canonical summary source: {target}") from exc
        if isinstance(payload, list):
            raw_summaries = payload
        elif isinstance(payload, Mapping) and isinstance(payload.get("summaries"), list):
            raw_summaries = payload["summaries"]
        else:
            raise RuntimeError(f"canonical summary source must contain a JSON array: {target}")
        summaries = [dict(item) for item in raw_summaries if isinstance(item, Mapping)]
        if len(summaries) != len(raw_summaries):
            raise RuntimeError(f"canonical summary source contains a non-object item: {target}")
        return summaries

    def _load_summary_payloads(
        self,
        session: AgentRuntimeSession,
        results: Mapping[str, StageResult],
    ) -> list[dict[str, Any]]:
        if session.stage_host.summaries:
            return [dict(item) for item in session.stage_host.summaries]

        paths: list[str] = []
        for value in (
            session.request.summary_file,
            *session.request.summary_sources,
            *session.request.reuse_summary_files,
        ):
            if value is None:
                continue
            text = str(value).strip()
            if text:
                paths.append(text)
        for stage_result in results.values():
            for artifact in stage_result.artifacts:
                if artifact.artifact_type == "summary_file" and artifact.path:
                    paths.append(artifact.path)
        summaries: list[dict[str, Any]] = []
        for path in dict.fromkeys(paths):
            summaries.extend(self._summary_payloads_from_file(path))
        return summaries

    @staticmethod
    def _validate_summary_identity(
        summaries: Sequence[Mapping[str, Any]],
        source_bundle: SourceBundle,
    ) -> list[Mapping[str, Any]]:
        if not source_bundle.paper_work_items:
            return list(summaries)
        if len(summaries) != len(source_bundle.paper_work_items):
            raise RuntimeError("canonical summary count does not match the source work-item count")
        expected = {item.canonical_paper_key: item for item in source_bundle.paper_work_items}
        seen: set[str] = set()
        normalized: list[Mapping[str, Any]] = []
        for index, summary in enumerate(summaries):
            if str(summary.get("status") or "").strip().lower() != "success":
                raise RuntimeError(f"canonical summary[{index}] is not a successful result")
            paper_info = summary.get("paper_info")
            if not isinstance(paper_info, Mapping):
                raise RuntimeError(f"canonical summary[{index}] has no paper_info object")
            paper_key = str(paper_info.get("canonical_paper_key") or "").strip()
            source_paper_id = str(paper_info.get("source_paper_id") or "").strip()
            if paper_key not in expected or paper_key in seen:
                raise RuntimeError(f"canonical summary[{index}] has an unknown or duplicate identity")
            if source_paper_id != expected[paper_key].source_paper_id:
                raise RuntimeError(f"canonical summary[{index}] source_paper_id does not match the source bundle")
            try:
                validate_canonical_ai_summary(
                    summary.get("ai_summary"),
                    label=f"canonical summary[{index}] ai_summary",
                )
            except ReconcileValidationError as exc:
                raise RuntimeError(str(exc)) from exc
            seen.add(paper_key)
            normalized.append(summary)
        if seen != set(expected):
            raise RuntimeError("canonical summaries do not cover every source work item")
        return normalized

    def _execute_analyze(
        self,
        *,
        session: AgentRuntimeSession,
        bundle: SourceBundle,
        results: Mapping[str, StageResult],
        attempt_id: str,
    ) -> tuple[StageResult, int]:
        summaries = self._load_summary_payloads(session, results)
        if bundle.paper_work_items:
            generation_service = Stage1AnalysisService(
                job_id=session.context.workspace.job_id,
                attempt_id=attempt_id,
                workspace=session.context.workspace,
                artifact_registry=session.context.registry,
                config=session.stage_host.config,
                settings=session.context.settings,
                cancellation_checker=session.stage_host.check_cancelled,
                publication_context=session.context.publication_context,
                logger=session.stage_host.logger,
            )
            generation = generation_service.run(bundle, existing_summaries=summaries)
            normalized = self._validate_summary_identity(generation.summaries, bundle)
            return (
                self.bridge.persist_stage1_results(
                    session,
                    normalized,
                    source_kind=(
                        "provider_generated"
                        if generation.generated_count
                        else "runtime_summary_source"
                    ),
                    source_items=[dict(item) for item in generation.source_items],
                    provider_receipt_ids=list(generation.receipt_ids),
                    generated_count=generation.generated_count,
                    reused_count=generation.reused_count,
                    stage1_generation=generation_service,
                ),
                len(generation.receipt_ids),
            )
        if not summaries:
            raise RuntimeError("Stage 1 requires a source work item or a canonical summary source")
        normalized = self._validate_summary_identity(summaries, bundle)
        generation_service = Stage1AnalysisService(
            job_id=session.context.workspace.job_id,
            attempt_id=attempt_id,
            workspace=session.context.workspace,
            artifact_registry=session.context.registry,
            config=session.stage_host.config,
            settings=session.context.settings,
            cancellation_checker=session.stage_host.check_cancelled,
            publication_context=session.context.publication_context,
            logger=session.stage_host.logger,
        )
        generation_service.prepare_empty_provider_receipt_closure(bundle)
        return (
            self.bridge.persist_stage1_results(
                session,
                normalized,
                source_kind="runtime_summary_source",
                stage1_generation=generation_service,
            ),
            0,
        )

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _outline_provider(
        self,
        session: AgentRuntimeSession,
        profile: ProviderContextProfile,
        api_config: Mapping[str, Any],
    ) -> Callable[[str, Mapping[str, Any]], Any]:
        from ai_interface import _call_ai_api_detailed_uninstrumented

        def call(node_id: str, request: Mapping[str, Any]) -> Any:
            return _call_ai_api_detailed_uninstrumented(
                json.dumps({"node_id": node_id, "request": dict(request)}, ensure_ascii=False, sort_keys=True),
                cast(Any, dict(api_config)),
                "You are the built-in Outline v3 stage executor. Return only valid JSON.",
                max_tokens=profile.max_output_tokens,
                temperature=0.0,
                response_format="json",
                logger=session.stage_host.logger,
                retry_attempts=session.stage_host.settings.runtime.transport_retries,
                max_retries_per_call=session.stage_host.settings.runtime.node_retry_limit,
            )

        return call

    def _execute_outline(
        self,
        *,
        session: AgentRuntimeSession,
        results: Mapping[str, StageResult],
    ) -> tuple[StageResult, int]:
        summaries = self._load_summary_payloads(session, results)
        if not summaries:
            raise RuntimeError("Outline v3 requires a canonical Stage 1 summary source")

        settings = session.context.settings
        route_name = settings.outline_model()
        api_config = get_outline_api_config(dict(session.stage_host.config))
        model = str(api_config.get("model") or route_name or "outline-v3")
        capability = resolve_model_capability(api_config)
        model_context_limit = self._positive_int(api_config.get("max_context_tokens"), 128_000)
        max_output_tokens = self._positive_int(
            api_config.get("max_output_tokens"),
            4_096,
        )
        profile = ProviderContextProfile.conservative(
            provider=capability.provider_family,
            model=model,
            endpoint_type=capability.endpoint_type,
            model_context_limit=model_context_limit,
            max_output_tokens=max_output_tokens,
        )
        provider = self._outline_provider(session, profile, api_config)
        stability = settings.outline_stability_settings()
        executor = OutlineV3Executor(
            job_id=session.context.workspace.job_id,
            summaries=summaries,
            workspace=session.context.workspace,
            artifact_registry=session.context.registry,
            provider=provider,
            provider_profile=profile,
            candidate_count=settings.outline_candidate_count(),
            quality_gate=settings.outline_quality_gate(),
            review_intent=(
                self.bridge.job_spec.metadata.get("review_intent")
                if isinstance(self.bridge.job_spec.metadata.get("review_intent"), Mapping)
                else None
            ),
            cancellation_checker=session.stage_host.check_cancelled,
            publication_context=self.bridge.publication_context,
            stability_mode=stability.mode,
            max_provider_calls=stability.max_provider_calls,
            max_estimated_cost=stability.max_estimated_cost,
            max_estimated_total_tokens=stability.max_estimated_total_tokens,
            pricing_source=stability.pricing_source,
            estimated_cost_per_1k_tokens=stability.estimated_cost_per_1k_tokens,
            input_cost_per_1k_tokens=stability.input_cost_per_1k_tokens,
            output_cost_per_1k_tokens=stability.output_cost_per_1k_tokens,
            reasoning_cost_per_1k_tokens=stability.reasoning_cost_per_1k_tokens,
            cache_read_cost_per_1k_tokens=stability.cache_read_cost_per_1k_tokens,
            cache_write_cost_per_1k_tokens=stability.cache_write_cost_per_1k_tokens,
            pricing_provider=stability.pricing_provider,
            pricing_model=stability.pricing_model,
            pricing_version=stability.pricing_version,
            pricing_effective_date=stability.pricing_effective_date,
            max_smoke_overhead_ratio=stability.max_smoke_overhead_ratio,
            max_source_prompt_tokens=stability.max_source_prompt_tokens or None,
        )
        execution = executor.run()
        if not execution.ok:
            detail = "; ".join(execution.diagnostics) or "Outline v3 execution is blocked"
            raise RuntimeError(detail)

        final_record = session.context.registry.get("outline-v3:final_outline")
        if final_record is None or final_record.status != "ready":
            raise RuntimeError("Outline v3 completed without a ready final outline")
        try:
            final_envelope = json.loads(Path(final_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Outline v3 final outline cannot be loaded") from exc
        final_payload = final_envelope.get("payload") if isinstance(final_envelope, Mapping) else None
        if not isinstance(final_payload, Mapping):
            raise RuntimeError("Outline v3 final outline payload is missing")
        artifact_refs: list[StageArtifactRef] = []
        for node_id in ("final_outline", "coverage_audit", "stability_audit", "provider_receipt_closure", "stage_health"):
            record = session.context.registry.get(f"outline-v3:{node_id}")
            if record is not None and record.status == "ready":
                artifact_refs.append(self.bridge._artifact_ref_from_record(record))
        if not artifact_refs:
            raise RuntimeError("Outline v3 completed without registered canonical outputs")
        return (
            StageResult(
                stage_name="stage2_outline",
                success=True,
                artifacts=artifact_refs,
                metadata={
                    "outline_mode": "v3",
                    "outline_v3_status": execution.status,
                    "node_ids": list(execution.node_ids),
                    "receipt_ids": list(execution.receipt_ids),
                    "artifact_paths": dict(execution.artifacts),
                    "stage_health_artifact_id": "outline-v3:stage_health",
                },
            ),
            len(execution.receipt_ids),
        )

    def _execute_review(
        self,
        *,
        session: AgentRuntimeSession,
        results: Mapping[str, StageResult],
        attempt_id: str,
    ) -> tuple[StageResult, int]:
        summaries = self._load_summary_payloads(session, results)
        if not summaries:
            raise RuntimeError("stage3 requires canonical Stage 1 summaries")
        final_record = session.context.registry.get("outline-v3:final_outline")
        if final_record is None or final_record.status != "ready":
            raise RuntimeError("stage3 requires a ready Outline v3 final outline")
        # Writer v3 may consume only the explicit adoption artifact.  The
        # setting remains part of the configuration surface for compatibility,
        # but a current Outline v3 review can never implicitly promote a
        # ready-for-adoption outline.
        adoption_record = current_adoption_record(session.context.registry)
        if adoption_record is None or adoption_record.status != "ready":
            raise RuntimeError("stage3 requires an explicitly adopted Outline v3 final outline")
        try:
            adoption_envelope = json.loads(Path(adoption_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stage3 adoption transaction cannot be loaded") from exc
        adoption_payload = adoption_envelope.get("payload") if isinstance(adoption_envelope, Mapping) else None
        if not isinstance(adoption_payload, Mapping):
            raise RuntimeError("stage3 adoption transaction payload is missing")
        gate_ids = {
            "coverage_audit_hash": "outline-v3:coverage_audit",
            "stability_audit_hash": "outline-v3:stability_audit",
            "provider_receipt_closure_hash": "outline-v3:provider_receipt_closure",
            "stage_health_hash": "outline-v3:stage_health",
        }
        if str(adoption_payload.get("final_outline_hash") or "") != final_record.content_hash:
            raise RuntimeError("stage3 adoption transaction is bound to a different final outline")
        for field_name, artifact_id in gate_ids.items():
            record = session.context.registry.get(artifact_id)
            if record is None or record.status != "ready" or str(adoption_payload.get(field_name) or "") != record.content_hash:
                raise RuntimeError(f"stage3 adoption transaction does not bind current {artifact_id}")
        closure_record = session.context.registry.get("outline-v3:provider_receipt_closure")
        if closure_record is None or closure_record.status != "ready":
            raise RuntimeError("stage3 requires a ready provider receipt closure")
        try:
            closure_envelope = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stage3 provider receipt closure cannot be loaded") from exc
        closure_payload = closure_envelope.get("payload") if isinstance(closure_envelope, Mapping) else None
        if not isinstance(closure_payload, Mapping) or not bool(closure_payload.get("complete")):
            raise RuntimeError("stage3 requires a complete provider receipt closure")
        try:
            envelope = json.loads(Path(final_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stage3 final outline cannot be loaded") from exc
        payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
        if not isinstance(payload, Mapping):
            raise RuntimeError("stage3 final outline payload is missing")
        packet_record = session.context.registry.get("outline-v3:section_evidence_packets")
        if packet_record is None or packet_record.status != "ready":
            raise RuntimeError("stage3 requires a ready section evidence packet set")
        try:
            packet_envelope = json.loads(Path(packet_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stage3 section evidence packets cannot be loaded") from exc
        packet_payload = packet_envelope.get("payload") if isinstance(packet_envelope, Mapping) else None
        packets = packet_payload.get("packets") if isinstance(packet_payload, Mapping) else None
        if not isinstance(packets, list):
            raise RuntimeError("stage3 section evidence packet payload is missing")
        from services.review_generation_service import ReviewGenerationService

        session.stage_host.summaries = list(summaries)
        generation = ReviewGenerationService(
            job_id=session.context.workspace.job_id,
            attempt_id=attempt_id,
            workspace=session.context.workspace,
            artifact_registry=session.context.registry,
            settings=session.context.settings,
            summaries=summaries,
            cancellation_checker=session.stage_host.check_cancelled,
            logger=session.stage_host.logger,
            publication_context=session.context.publication_context,
        ).run(
            outline_payload=payload,
            evidence_packets=[dict(item) for item in packets if isinstance(item, Mapping)],
        )
        review_stage = self.bridge.persist_review_chain(
                session,
                outline_file=final_record.path,
                review_sections=[dict(item) for item in generation.sections],
                references=[],
                generation_mode="outline_v3",
                citation_ref_catalog=generation.citation_ref_catalog,
                citation_ref_catalog_path=generation.citation_ref_catalog_path,
                citation_ref_catalog_hash=generation.citation_ref_catalog.get("catalog_hash", ""),
            )
        closure_record = session.context.registry.get("review:provider_receipt_closure")
        if closure_record is not None and closure_record.status == "ready":
            review_stage = replace(
                review_stage,
                artifacts=[
                    *review_stage.artifacts,
                    self.bridge._artifact_ref_from_record(closure_record),
                ],
            )
        return review_stage, len(generation.receipt_ids)

    def execute(
        self,
        stage: str,
        *,
        session: AgentRuntimeSession,
        spec: RuntimeJobSpec,
        bundle: SourceBundle,
        results: Mapping[str, StageResult],
        attempt_id: str,
        external_registry_resolver: Callable[[str], Any | None] | None = None,
    ) -> tuple[StageResult, int]:
        del spec, external_registry_resolver
        executors: dict[str, Callable[[], tuple[StageResult, int]]] = {
            "analyze": lambda: self._execute_analyze(
                session=session,
                bundle=bundle,
                results=results,
                attempt_id=attempt_id,
            ),
            "outline": lambda: self._execute_outline(session=session, results=results),
            "review": lambda: self._execute_review(
                session=session,
                results=results,
                attempt_id=attempt_id,
            ),
            "validate": lambda: (
                self.bridge.run_validation(session, attempt_id=attempt_id),
                0,
            ),
        }
        executor = executors.get(stage)
        if executor is None:
            raise RuntimeError(f"unsupported runtime stage: {stage}")
        return executor()


class AgentRuntimeBridge:
    """Thin additive bridge used by the repo-local skill entrypoint."""

    def __init__(self, job_spec: RuntimeJobSpec, publication_context: Any | None = None) -> None:
        job_spec.validate()
        self.job_spec = job_spec
        self.publication_context = publication_context

    def build_job_request(self) -> JobRunRequest:
        request = self.job_spec.to_job_request()
        if request.summary_file and request.summary_file not in request.summary_sources:
            request = replace(
                request,
                summary_sources=(request.summary_file, *request.summary_sources),
            )
        if (
            request.action not in {"analyze", "run_all", "retry_failed"}
            and request.summary_sources
        ):
            # Downstream stages consume a verified summary source.  Do not
            # make their inventory readiness depend on re-running PDF intake.
            request = replace(request, pdf_folder=None)
        error = validate_job_request_options(request)
        if error:
            raise ValueError(error)
        return request

    def build_source_bundle(self) -> SourceBundle:
        request = self.build_job_request()
        summary_sources = tuple(
            str(item)
            for item in (
                request.summary_file,
                *request.summary_sources,
                *request.reuse_summary_files,
            )
            if str(item).strip()
        )
        if request.action not in {"analyze", "run_all", "retry_failed"} and summary_sources:
            # Downstream and derived-review actions consume an already verified
            # canonical summary source.  Their source-intake artifact must not
            # reinterpret a deliberately cleared PDF path as the current
            # working directory and ingest unrelated repository PDFs.
            return SourceBundle(
                source_mode=request.source_mode,
                project_name=self.job_spec.project_name,
                paper_work_items=[],
                source_snapshot={
                    "canonical_ready": True,
                    "summary_only": True,
                    "summary_sources": list(summary_sources),
                },
            )
        try:
            bundle = build_source_bundle_for_request(request, project_name=self.job_spec.project_name)
        except Exception:
            # Summary-driven downstream actions can proceed from an explicit
            # canonical summary source when PDF intake is not part of the job.
            if request.action in {"analyze", "run_all", "retry_failed"} or not any(summary_sources):
                raise
            return SourceBundle(
                source_mode=request.source_mode,
                project_name=self.job_spec.project_name,
                paper_work_items=[],
                source_snapshot={
                    "canonical_ready": True,
                    "summary_only": True,
                    "summary_sources": [str(item) for item in summary_sources if str(item)],
                },
            )
        if bundle.source_snapshot.get("canonical_ready") is False:
            return SourceBundle(
                source_mode=bundle.source_mode,
                project_name=bundle.project_name,
                paper_work_items=[],
                source_snapshot=dict(bundle.source_snapshot),
            )
        return bundle

    def stage_executor_registry(self) -> InternalStageExecutorRegistry:
        return InternalStageExecutorRegistry(self)

    def execute_stage(
        self,
        stage: str,
        *,
        session: AgentRuntimeSession,
        spec: RuntimeJobSpec,
        bundle: SourceBundle,
        results: Mapping[str, StageResult],
        attempt_id: str,
        external_registry_resolver: Callable[[str], Any | None] | None = None,
    ) -> tuple[StageResult, int]:
        return self.stage_executor_registry().execute(
            stage,
            session=session,
            spec=spec,
            bundle=bundle,
            results=results,
            attempt_id=attempt_id,
            external_registry_resolver=external_registry_resolver,
        )

    def stage_policies(self) -> Dict[str, Dict[str, Any]]:
        return {
            stage_name: stage_policy_for(stage_name).to_dict()
            for stage_name in (
                "source_intake",
                "stage1_analyze",
                "stage1_derive",
                "stage2_outline",
                "stage3_review",
                "stage4_validate",
            )
        }

    def initial_stage_trace(self) -> list[dict[str, Any]]:
        return [
            build_runtime_stage_trace_entry(
                stage_name="source_intake",
                step_name="normalize_request_and_sources",
                producer="runtime.orchestrator.AgentRuntimeBridge",
            ),
            build_runtime_stage_trace_entry(
                stage_name="stage4_validate",
                step_name="validation_entry_is_local_only",
                producer="runtime.orchestrator.AgentRuntimeBridge",
            ),
        ]

    def bootstrap(
        self,
        *,
        cancel_token: CancelToken | None = None,
        claim_latest_pointer: bool = True,
        resume_requested: bool = False,
        resume_preflight: Callable[[JobWorkspace], None] | None = None,
        workspace_preflight: Callable[[JobWorkspace], None] | None = None,
        publish_running_state: bool = True,
    ) -> AgentRuntimeSession:
        request = self.build_job_request()
        runner = JobRunner()
        project_name = runner._resolve_project_name(request)
        active_cancel_token = cancel_token or CancelToken()
        active_cancel_token.check_cancelled()

        stage_host = _RuntimeStageHost(request, project_name, active_cancel_token)
        stage_host.audit_actor = str(self.job_spec.metadata.get("audit_actor") or "")
        stage_host.audit_reason = str(self.job_spec.metadata.get("audit_reason") or "")
        stage_host.audit_scope = dict(self.job_spec.metadata.get("audit_scope") or {})

        generator_config = dict(stage_host.config)
        output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")
        if self.job_spec.workspace_path:
            planned_workspace = Path(self.job_spec.workspace_path).expanduser().resolve()
            output_base_dir = str(planned_workspace.parent)
        resolved_project_name = runner._resolve_project_name_from_existing_workspaces(
            base_output_dir=output_base_dir,
            requested_project_name=project_name,
            action=request.action,
        )
        if resolved_project_name != project_name:
            project_name = resolved_project_name
            stage_host.project_name = resolved_project_name

        if workspace_preflight is not None:
            planned_workspace = JobWorkspace(
                output_base_dir,
                project_name,
                str(request.job_id or self.job_spec.job_id),
            )
            workspace_preflight(planned_workspace)

        prepared_sources = runner._prepare_source_inventory(
            generator=stage_host,
            request=request,
            project_name=project_name,
        )
        context = bootstrap_job_runtime(
            request=request,
            generator=stage_host,
            project_name=project_name,
            source_snapshot=runner._source_snapshot(stage_host, request),
            request_snapshot=runner._request_snapshot(request),
            build_workspace=runner._build_workspace,
            write_resume_report=runner._write_resume_report,
            source_inventory=prepared_sources.inventory,
            source_canonical_ready=prepared_sources.canonical_ready,
            source_degradation_reasons=prepared_sources.degradation_reasons,
            claim_latest_pointer=claim_latest_pointer,
            resume_requested=resume_requested,
            resume_preflight=resume_preflight,
            publish_running_state=publish_running_state,
            publication_context=self.publication_context,
        )
        return AgentRuntimeSession(
            runner=runner,
            request=request,
            stage_host=stage_host,
            context=context,
        )

    def persist_source_bundle(self, session: AgentRuntimeSession, source_bundle: SourceBundle) -> StageArtifactRef:
        source_dependencies: list[ArtifactDependencyRefV2] = []
        for item in source_bundle.paper_work_items:
            source_path = Path(item.source_pdf)
            if not source_path.is_file():
                continue
            source_record = session.context.registry.register_file(
                artifact_role="source_pdf",
                artifact_type="source_pdf",
                artifact_version="v1",
                path=source_path,
                producer="runtime.orchestrator.AgentRuntimeBridge.persist_source_bundle",
                artifact_id=f"source_pdf:{source_path.name}",
            )
            source_dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=source_record.job_id,
                    artifact_id=source_record.artifact_id,
                    artifact_type=source_record.artifact_type,
                    path=source_record.path,
                    content_hash=source_record.content_hash,
                )
            )
        path = session.context.workspace.artifact_path("source_bundle.json")
        record = publish_json_artifact(
            session.context.publication_context,
            session.context.registry,
            path,
            source_bundle.to_dict(),
            artifact_role="source_bundle",
            artifact_type="source_bundle",
            artifact_version="v1",
            producer="runtime.orchestrator.AgentRuntimeBridge.persist_source_bundle",
            artifact_id="source_bundle",
            depends_on=source_dependencies,
        )
        return self._artifact_ref_from_record(record)

    def write_stage_trace(
        self,
        session: AgentRuntimeSession,
        entries: list[dict[str, Any]] | None = None,
        *,
        artifact_name: str = "runtime_stage_trace.json",
    ) -> StageArtifactRef:
        trace_entries = list(entries or self.initial_stage_trace())
        path = session.context.workspace.artifact_path(artifact_name)
        record = publish_json_artifact(
            session.context.publication_context,
            session.context.registry,
            path,
            {"entries": trace_entries},
            artifact_role="runtime_stage_trace",
            artifact_type="runtime_stage_trace",
            artifact_version="v1",
            producer="runtime.orchestrator.AgentRuntimeBridge.write_stage_trace",
            artifact_id="runtime_stage_trace",
        )
        return self._artifact_ref_from_record(record)

    def build_validation_service(
        self,
        session: AgentRuntimeSession,
        *,
        attempt_id: str = "",
        external_registry_resolver: Any | None = None,
    ) -> ValidationExecutionService:
        registry = session.context.registry
        paper_records = tuple(
            record
            for record in registry.list_records()
            if record.status == "ready" and record.artifact_type == "paper_artifact"
        )
        visual_records = tuple(
            record
            for record in registry.list_records()
            if record.status == "ready"
            and record.artifact_type in {"visual_manifest", "visual_bundle", "visual_ref", "visual_artifact"}
        )
        return ValidationExecutionService(
            job_id=session.context.workspace.job_id,
            attempt_id=str(attempt_id or "validation"),
            workspace=session.context.workspace,
            artifact_registry=registry,
            settings=session.stage_host.settings,
            summaries=list(session.stage_host.summaries),
            review_draft_record=current_artifact_record(registry, "review_draft"),
            citation_manifest_record=current_artifact_record(registry, "citation_manifest"),
            paper_artifact_records=paper_records,
            visual_artifact_records=visual_records,
            provider_factory=None,
            cancellation_checker=session.stage_host.check_cancelled,
            logger=session.stage_host.logger,
            runtime_config=session.stage_host.config,
            validation_external_registry_resolver=external_registry_resolver,
            publication_context=session.context.publication_context,
        )

    def persist_stage1_results(
        self,
        session: AgentRuntimeSession,
        summaries: Iterable[Mapping[str, Any]],
        *,
        source_kind: str = "runtime_stage1",
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.persist_stage1_results",
        source_items: list[dict[str, Any]] | None = None,
        rejected_candidates: list[dict[str, Any]] | None = None,
        write_excel_report: bool = False,
        subagent_run_id: str | None = None,
        provider_receipt_ids: list[str] | None = None,
        generated_count: int = 0,
        reused_count: int = 0,
        stage1_generation: Any | None = None,
    ) -> StageResult:
        host = session.stage_host
        if write_excel_report:
            raise RuntimeError("excel report generation is not part of the built-in runtime stage registry")
        normalized_summaries = [dict(summary) for summary in summaries]
        source_bundle_record = session.context.registry.get("source_bundle")
        if source_bundle_record is None:
            self.persist_source_bundle(session, self.build_source_bundle())
            source_bundle_record = session.context.registry.get("source_bundle")
        if source_bundle_record is None or source_bundle_record.status != "ready":
            raise RuntimeError("stage1 source bundle is not registered and ready")
        source_dependencies = [
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=source_bundle_record.job_id,
                artifact_id=source_bundle_record.artifact_id,
                artifact_type=source_bundle_record.artifact_type,
                path=source_bundle_record.path,
                content_hash=source_bundle_record.content_hash,
            )
        ]
        provider_record = session.context.registry.get("stage1_provider_receipts")
        if provider_record is not None and provider_record.status == "ready":
            source_dependencies.append(
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=provider_record.job_id,
                    artifact_id=provider_record.artifact_id,
                    artifact_type=provider_record.artifact_type,
                    path=provider_record.path,
                    content_hash=provider_record.content_hash,
                )
            )
        host.summaries = normalized_summaries
        host._checkpoint_processed_papers = set()
        host._checkpoint_failed_papers = set()

        for summary in normalized_summaries:
            paper = summary.get("paper_info")
            if not isinstance(paper, Mapping):
                continue
            paper_key = host.get_paper_key(paper)
            if summary.get("status") == "success":
                host._checkpoint_processed_papers.add(paper_key)
            else:
                host._checkpoint_failed_papers.add(paper_key)

        if not host.save_summaries(depends_on=source_dependencies):
            raise RuntimeError("stage1 summary persistence failed")
        # Resume and completion reports must follow the Registry-selected
        # immutable paths produced by the publication boundary.
        object.__setattr__(session.context, "summary_path", host.summary_file)
        object.__setattr__(session.context, "progress_path", host.progress_path)

        manifest_path = host._get_summary_source_manifest_path()
        summary_source_record = publish_json_artifact(
            session.context.publication_context,
            session.context.registry,
            manifest_path,
            {
                "artifact_type": "summary_source_manifest",
                "artifact_version": "v2",
                "created_at": utc_now_iso(),
                "project_name": host.project_name,
                "source_kind": source_kind,
                "source_path": source_bundle_record.path,
                "source_items": list(source_items or []),
                "rejected_candidates": list(rejected_candidates or []),
                "materialized_summary_file": host.summary_file,
                "summary_count": len(normalized_summaries),
            },
            artifact_role="summary_source",
            artifact_type="summary_source_manifest",
            artifact_version="v2",
            producer=producer,
            artifact_id="summary_source_manifest",
            depends_on=source_dependencies,
        )
        manifest_path = summary_source_record.path

        artifact_refs = [
            self._artifact_ref_for_path(
                session,
                host.summary_file,
                artifact_role="summary",
                artifact_type="summary_file",
                artifact_version="v1",
            ),
            self._artifact_ref_for_path(
                session,
                host.progress_path,
                artifact_role="progress",
                artifact_type="stage1_progress_snapshot",
                artifact_version="v1",
            ),
        ]

        if manifest_path:
            artifact_refs.append(
                self._artifact_ref_for_path(
                    session,
                    manifest_path,
                    artifact_role="summary_source",
                    artifact_type="summary_source_manifest",
                    artifact_version="v2",
                )
            )

        for summary in normalized_summaries:
            if not host._persist_paper_artifact(summary):
                raise RuntimeError("paper artifact persistence failed")
            paper = summary.get("paper_info")
            if summary.get("status") != "success" or not isinstance(paper, Mapping):
                continue
            artifact_refs.append(
                self._artifact_ref_for_path(
                    session,
                    host._paper_artifact_path(paper),
                    artifact_role=host.PAPER_ARTIFACT_ROLE,
                    artifact_type=host.PAPER_ARTIFACT_TYPE,
                    artifact_version=host.PAPER_ARTIFACT_VERSION,
                    artifact_id=host._paper_artifact_id(paper),
                )
            )

        stage1_closure_record = None
        expected_graph_record = None
        if stage1_generation is not None:
            stage1_closure_record = stage1_generation.finalize_provider_receipt_closure()
            expected_graph_record = session.context.registry.get(
                "stage1:provider_expected_call_graph"
            )
            if expected_graph_record is not None:
                artifact_refs.append(self._artifact_ref_from_record(expected_graph_record))
            artifact_refs.append(self._artifact_ref_from_record(stage1_closure_record))

        if provider_record is not None and provider_record.status == "ready":
            artifact_refs.append(self._artifact_ref_from_record(provider_record))

        self._append_stage_trace_entries(
            session,
            self._build_generation_trace_entries(
                stage_name="stage1_analyze",
                producer=producer,
                subagent_run_id=subagent_run_id,
                subagent_step_name="subagent_generation_complete",
                local_step_name="persist_stage1_results",
                subagent_metadata={"summary_count": len(normalized_summaries)},
                local_metadata={
                    "processed_count": len(host._checkpoint_processed_papers),
                    "failed_count": len(host._checkpoint_failed_papers),
                },
            ),
        )

        return StageResult(
            stage_name="stage1_analyze",
            success=True,
            artifacts=artifact_refs,
            metadata={
                "summary_count": len(normalized_summaries),
                "processed_count": len(host._checkpoint_processed_papers),
                "failed_count": len(host._checkpoint_failed_papers),
                "generated_count": int(generated_count),
                "reused_count": int(reused_count),
                "provider_receipt_ids": list(provider_receipt_ids or []),
                "provider_expected_call_graph": (
                    expected_graph_record.artifact_id if expected_graph_record is not None else ""
                ),
                "provider_receipt_closure": (
                    stage1_closure_record.artifact_id if stage1_closure_record is not None else ""
                ),
                "expected_provider_transport_count": int(
                    getattr(stage1_generation, "expected_provider_transport_count", 0)
                    if stage1_generation is not None
                    else 0
                ),
                "actual_provider_transport_count": int(
                    getattr(stage1_generation, "actual_provider_transport_count", 0)
                    if stage1_generation is not None
                    else len(provider_receipt_ids or [])
                ),
                "provider_receipt_count": len(provider_receipt_ids or []),
                "reuse_evidence_count": int(
                    len(getattr(stage1_generation, "reuse_evidence_ids", ()) or ())
                    if stage1_generation is not None
                    else 0
                ),
                "reuse_evidence_ids": list(
                    getattr(stage1_generation, "reuse_evidence_ids", ()) or ()
                    if stage1_generation is not None
                    else ()
                ),
            },
        )

    def persist_review_chain(
        self,
        session: AgentRuntimeSession,
        *,
        outline_file: str,
        review_sections: list[dict[str, Any]],
        references: list[str] | None = None,
        word_file: str | None = None,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.persist_review_chain",
        generation_mode: str = "full_review",
        rebuild_docx: bool = True,
        subagent_run_id: str | None = None,
        citation_ref_catalog: Mapping[str, Any] | None = None,
        citation_ref_catalog_path: str = "",
        citation_ref_catalog_hash: str = "",
    ) -> StageResult:
        host = session.stage_host
        review_word_path = word_file or host._get_review_word_file_path()
        if not host._persist_review_draft(
            outline_file=outline_file,
            review_sections=review_sections,
            references=list(references or ()),
            word_file=review_word_path,
            generation_mode=generation_mode,
            citation_ref_catalog=citation_ref_catalog,
            citation_ref_catalog_path=citation_ref_catalog_path,
            citation_ref_catalog_hash=citation_ref_catalog_hash,
        ):
            raise RuntimeError("current review draft persistence failed")
        draft_record = session.context.registry.get("review_draft")
        if draft_record is None or draft_record.status != "ready":
            raise RuntimeError("current review draft record is unavailable after publication")
        draft_path = draft_record.path
        if not host._persist_citation_manifest(
            review_draft_path=draft_path,
            review_word_path=review_word_path,
            citation_ref_catalog=citation_ref_catalog,
            citation_ref_catalog_path=citation_ref_catalog_path,
            citation_ref_catalog_hash=citation_ref_catalog_hash,
        ):
            raise RuntimeError("current citation manifest persistence failed")
        manifest_record = session.context.registry.get(host.CITATION_MANIFEST_ARTIFACT_ID)
        if manifest_record is None or manifest_record.status != "ready":
            raise RuntimeError("current citation manifest record is unavailable after publication")
        try:
            manifest_payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("current citation manifest is unreadable after publication") from exc
        manifest = dict(manifest_payload) if isinstance(manifest_payload, Mapping) else {}
        if not manifest:
            raise RuntimeError("current citation manifest is unavailable")
        docx_source_path = Path(review_word_path)
        if rebuild_docx:
            from docx_writer import rebuild_review_docx_from_structured_artifacts

            draft = json.loads(Path(draft_record.path).read_text(encoding="utf-8"))
            docx_source_path = Path(
                session.context.workspace.artifact_path(
                    ".publication-staging/review-docx/rebuilt_review.docx"
                )
            )
            rebuild_review_docx_from_structured_artifacts(
                host,
                draft,
                manifest,
                str(docx_source_path),
            )
        registry = session.context.registry
        catalog_record = registry.get("citation_ref_catalog")
        if not docx_source_path.is_file():
            raise RuntimeError(f"review DOCX bytes are unavailable before publication: {docx_source_path}")
        docx_record = publish_bytes_artifact(
            session.context.publication_context,
            registry,
            review_word_path,
            docx_source_path.read_bytes(),
            artifact_role="review_docx",
            artifact_type="review_docx",
            artifact_version="v1",
            producer=producer,
            artifact_id="review_docx",
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=registry.job_id,
                    artifact_id=draft_record.artifact_id,
                    artifact_type=draft_record.artifact_type,
                    path=draft_record.path,
                    content_hash=draft_record.content_hash,
                ),
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id=registry.job_id,
                    artifact_id=manifest_record.artifact_id,
                    artifact_type=manifest_record.artifact_type,
                    path=manifest_record.path,
                    content_hash=manifest_record.content_hash,
                ),
            ],
        )
        self._append_stage_trace_entries(
            session,
            self._build_generation_trace_entries(
                stage_name="stage3_review",
                producer=producer,
                subagent_run_id=subagent_run_id,
                subagent_step_name="subagent_review_complete",
                local_step_name="persist_current_review",
                subagent_metadata={"section_count": len(review_sections)},
                local_metadata={"word_file": review_word_path},
            ),
        )
        return StageResult(
            stage_name="stage3_review",
            success=True,
            artifacts=[
                self._artifact_ref_from_record(draft_record),
                self._artifact_ref_from_record(manifest_record),
                self._artifact_ref_from_record(docx_record),
                *([self._artifact_ref_from_record(catalog_record)] if catalog_record is not None else []),
            ],
            metadata={
                "section_count": len(review_sections),
                "reference_count": len(references or ()),
                "word_file": review_word_path,
                "citation_ref_catalog_hash": citation_ref_catalog_hash
                or str((citation_ref_catalog or {}).get("catalog_hash") or ""),
            },
        )

    def run_validation(
        self,
        session: AgentRuntimeSession,
        *,
        attempt_id: str = "",
        external_registry_resolver: Any | None = None,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.run_validation",
    ) -> StageResult:
        validation_service = self.build_validation_service(
            session,
            attempt_id=attempt_id,
            external_registry_resolver=external_registry_resolver,
        )
        try:
            result = validation_service.run_review_validation()
        finally:
            validation_provider_receipts = validation_service.finalize_provider_receipts()

        validation_closure = validation_provider_receipts.get("closure")
        validation_expected_call_ids = tuple(
            str(item)
            for item in validation_provider_receipts.get("expected_call_ids", ())
            if str(item).strip()
        )
        validation_closure_complete = bool(
            not validation_expected_call_ids
            or getattr(validation_closure, "complete", False)
        )

        from validation.run_result import ValidationExecutionStatus, ValidationRunResultV1

        validation_run_result_file = str(result.get("validation_run_result_file") or "")
        report_file = str(result.get("report_file") or "")
        manual_report_file = str(result.get("manual_report_file") or "")
        completion_report_file = str(result.get("completion_report_file") or "")
        run_obj = result.get("validation_run_result")
        if isinstance(run_obj, ValidationRunResultV1):
            candidate_result = run_obj
        elif isinstance(run_obj, Mapping):
            candidate_result = ValidationRunResultV1.from_dict(run_obj)
        else:
            candidate_result = ValidationRunResultV1.create(
                job_id=session.context.workspace.job_id,
                attempt_id=str(attempt_id or ""),
                execution_status=ValidationExecutionStatus.FAILED,
                report_id="validation-run-missing",
                failure_reason="validation stage did not produce current ValidationRunResultV1",
                review_has_citations=False,
                evidence_complete=False,
            )

        validation_run_result = candidate_result
        canonical_digest = ""
        durability_failure = ""
        if validation_run_result_file:
            canonical_path = Path(validation_run_result_file).expanduser().resolve()
            if not canonical_path.is_file():
                durability_failure = "canonical_validation_result_missing"
            else:
                try:
                    validation_run_result = ValidationRunResultV1.from_dict(
                        json.loads(canonical_path.read_text(encoding="utf-8"))
                    )
                    canonical_digest = file_sha256(canonical_path)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    durability_failure = "canonical_validation_result_invalid"
        elif candidate_result.execution_status is ValidationExecutionStatus.SUCCEEDED:
            durability_failure = "canonical_validation_result_missing"

        if not durability_failure and validation_run_result_file:
            if validation_run_result.job_id != session.context.workspace.job_id:
                durability_failure = "canonical_validation_job_mismatch"
            elif validation_run_result.attempt_id != str(attempt_id or ""):
                durability_failure = "canonical_validation_attempt_mismatch"

        if durability_failure:
            self._append_stage_trace_entries(
                session,
                [
                    build_runtime_stage_trace_entry(
                        stage_name="stage4_validate",
                        step_name="run_review_validation",
                        producer=producer,
                        execution_mode=ExecutionMode.LOCAL,
                        metadata={
                            "validation_run_result_file": validation_run_result_file,
                            "execution_status": "failed",
                            "validation_disposition": "unvalidated",
                            "failure_reason": durability_failure,
                        },
                    )
                ],
            )
            return StageResult(
                stage_name="stage4_validate",
                success=False,
                artifacts=[],
                metadata={
                    "manual_review_count": 0,
                    "validation_run_id": "",
                    "execution_status": "failed",
                    "validation_disposition": "unvalidated",
                    "claim_verdict_counts": {},
                    "contradicted_count": 0,
                    "failure_reason": durability_failure,
                },
            )

        from validation.input_dependencies import (
            ValidationInputDependencyError,
            resolve_validation_input_dependencies,
        )

        dependency_error = ""
        try:
            depends_on = resolve_validation_input_dependencies(
                session.context.registry,
                validation_run_result.input_artifacts,
                external_registry_resolver=external_registry_resolver,
            )
        except ValidationInputDependencyError as exc:
            depends_on = []
            dependency_error = str(exc)

        artifact_refs: list[StageArtifactRef] = []
        for receipt_key in ("ledger", "closure_record"):
            receipt_record = validation_provider_receipts.get(receipt_key)
            if receipt_record is not None:
                artifact_refs.append(self._artifact_ref_from_record(receipt_record))
        validation_contract_complete = bool(
            validation_run_result.contract_satisfied
            and not dependency_error
        )
        validation_success = bool(
            validation_run_result.execution_status is ValidationExecutionStatus.SUCCEEDED
            and validation_contract_complete
            and validation_closure_complete
        )
        contract_failure = ""
        if validation_run_result.execution_status is ValidationExecutionStatus.SUCCEEDED:
            if dependency_error:
                contract_failure = "validation_input_dependencies_unverified"
            elif not validation_contract_complete:
                contract_failure = "validation_contract_incomplete"
            elif not validation_closure_complete:
                contract_failure = "validation_provider_receipt_closure_incomplete"
        published_execution_status = validation_run_result.execution_status.value
        published_validation_disposition = validation_run_result.validation_disposition.value
        if contract_failure:
            published_execution_status = ValidationExecutionStatus.FAILED.value
            published_validation_disposition = "unvalidated"

        canonical_record: ArtifactRecord | None = None
        if validation_run_result_file:
            existing_canonical = session.context.registry.get(
                validation_run_result.validation_run_id
            )
            if existing_canonical is not None and Path(existing_canonical.path).is_file():
                canonical_record = existing_canonical
                if canonical_record.status == "ready" and not validation_success:
                    canonical_record = session.context.registry.update_record(
                        canonical_record.artifact_id,
                        status="quarantined",
                        metadata_updates={"dependency_error": dependency_error},
                    )
            else:
                canonical_record = publish_bytes_artifact(
                    session.context.publication_context,
                    session.context.registry,
                    validation_run_result_file,
                    Path(validation_run_result_file).read_bytes(),
                    artifact_role="validation",
                    artifact_type="validation_run_result",
                    artifact_version="v1",
                    producer=producer,
                    depends_on=depends_on,
                    artifact_id=validation_run_result.validation_run_id,
                    status="ready" if validation_success else "quarantined",
                    external_registry_resolver=external_registry_resolver,
                    metadata={
                        "execution_status": validation_run_result.execution_status.value,
                        "validation_disposition": validation_run_result.validation_disposition.value,
                        "claim_verdict_counts": dict(validation_run_result.claim_verdict_counts),
                        "dependency_error": dependency_error,
                    },
                )
            if canonical_record is None:
                raise RuntimeError("validation result publication returned no Registry record")
            validation_run_result_file = canonical_record.path
            canonical_digest = canonical_record.content_hash
            if canonical_record.content_hash != canonical_digest:
                session.context.registry.update_record(
                    canonical_record.artifact_id,
                    status="invalid",
                    metadata_updates={"invalid_reason": "canonical_validation_hash_changed"},
                )
                return StageResult(
                    stage_name="stage4_validate",
                    success=False,
                    artifacts=[],
                    metadata={
                        "manual_review_count": 0,
                        "validation_run_id": validation_run_result.validation_run_id,
                        "execution_status": "failed",
                        "validation_disposition": "unvalidated",
                        "claim_verdict_counts": {},
                        "contradicted_count": 0,
                        "failure_reason": "canonical_validation_hash_changed",
                    },
                )
            if validation_success:
                artifact_refs.append(self._artifact_ref_from_record(canonical_record))

        projection_dependencies = [
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=session.context.workspace.job_id,
                artifact_id=canonical_record.artifact_id,
                artifact_type=canonical_record.artifact_type,
                path=canonical_record.path,
                content_hash=canonical_record.content_hash,
            )
        ] if canonical_record is not None and validation_success else []

        projections = (
            (report_file, "validation_report_projection", "validation-report"),
            (manual_report_file, "manual_review_projection", "manual-review"),
            (completion_report_file, "validation_completion_projection", "validation-completion"),
            (str(result.get("claim_alignment_audit_json") or ""), "claim_alignment_audit_projection", "claim-alignment"),
        )

        for path, artifact_type, artifact_prefix in projections:
            if not path or canonical_record is None or not validation_success:
                continue
            projection_id = f"{artifact_prefix}:{Path(path).name}"
            record = session.context.registry.get(projection_id)
            if record is None or not Path(record.path).is_file():
                record = publish_bytes_artifact(
                    session.context.publication_context,
                    session.context.registry,
                    path,
                    Path(path).read_bytes(),
                    artifact_role="validation_projection",
                    artifact_type=artifact_type,
                    artifact_version="v1",
                    producer=producer,
                    depends_on=projection_dependencies,
                    artifact_id=projection_id,
                )
            artifact_refs.append(self._artifact_ref_from_record(record))

        if validation_success:
            review_draft_record = session.context.registry.get("review_draft")
            citation_manifest_record = session.context.registry.get("citation_manifest_v3")
            review_docx_record = session.context.registry.get("review_docx")
            validation_closure_record = validation_provider_receipts.get("closure_record")
            if (
                review_draft_record is None
                or review_draft_record.status != "ready"
                or citation_manifest_record is None
                or citation_manifest_record.status != "ready"
                or review_docx_record is None
                or review_docx_record.status != "ready"
                or canonical_record is None
                or canonical_record.status != "ready"
                or validation_closure_record is None
                or validation_closure_record.status != "ready"
            ):
                raise RuntimeError(
                    "current artifact set requires ready review, citation, document, "
                    "validation, and validation receipt closure artifacts"
                )
            previous_set = session.context.registry.resolve_current_artifact_set()
            promotion_id = f"runtime-validation:{validation_run_result.validation_run_id}"
            promotion = RepairPromotionTransaction(
                transaction_id=promotion_id,
                job_id=session.context.workspace.job_id,
                source_transaction_id="",
                status="prepared",
                actor=producer,
                reason="validation completed and established the verified current artifact set",
                canonical_version="runtime-validation",
                review_draft_artifact_id=review_draft_record.artifact_id,
                citation_manifest_artifact_id=citation_manifest_record.artifact_id,
                review_docx_artifact_id=review_docx_record.artifact_id,
                audit_artifact_id="",
                lineage_artifact_id="",
                canonical_input_hashes={
                    review_draft_record.artifact_id: review_draft_record.content_hash,
                    citation_manifest_record.artifact_id: citation_manifest_record.content_hash,
                    review_docx_record.artifact_id: review_docx_record.content_hash,
                },
                output_hashes={
                    review_draft_record.artifact_id: review_draft_record.content_hash,
                    citation_manifest_record.artifact_id: citation_manifest_record.content_hash,
                    review_docx_record.artifact_id: review_docx_record.content_hash,
                    canonical_record.artifact_id: canonical_record.content_hash,
                    validation_closure_record.artifact_id: validation_closure_record.content_hash,
                },
                created_at=utc_now_iso(),
                validation_run_result_artifact_id=canonical_record.artifact_id,
            )
            promotion_path = Path(
                session.context.workspace.artifact_path(
                    f"runtime_validation/{promotion_id.replace(':', '-')}/repair_promotion_transaction.json"
                )
            )
            promotion_record = publish_json_artifact(
                session.context.publication_context,
                session.context.registry,
                promotion_path,
                promotion.to_dict(),
                artifact_role="repair_promotion_transaction",
                artifact_type=promotion.artifact_type,
                artifact_version=promotion.artifact_version,
                producer="runtime.orchestrator._RuntimeStageHost.validate",
                artifact_id=promotion_id,
                depends_on=[
                    ArtifactDependencyRefV2.from_record(review_draft_record),
                    ArtifactDependencyRefV2.from_record(citation_manifest_record),
                    ArtifactDependencyRefV2.from_record(review_docx_record),
                    ArtifactDependencyRefV2.from_record(canonical_record),
                    ArtifactDependencyRefV2.from_record(validation_closure_record),
                ],
                metadata={
                    "status": promotion.status,
                    "promotion_state": "prepared",
                    "commit_boundary": "current_artifact_set_pointer_cas",
                    "canonical_replacement": True,
                },
            )
            current_set = session.context.registry.build_current_artifact_set(
                promotion_transaction_id=promotion_id,
                promotion_transaction_hash=promotion_record.content_hash,
                review_draft_artifact_id=review_draft_record.artifact_id,
                review_draft_artifact_hash=review_draft_record.content_hash,
                citation_manifest_artifact_id=citation_manifest_record.artifact_id,
                citation_manifest_artifact_hash=citation_manifest_record.content_hash,
                review_docx_artifact_id=review_docx_record.artifact_id,
                review_docx_artifact_hash=review_docx_record.content_hash,
                validation_run_result_artifact_id=canonical_record.artifact_id,
                validation_run_result_artifact_hash=canonical_record.content_hash,
                validation_receipt_closure_artifact_id=validation_closure_record.artifact_id,
                validation_receipt_closure_artifact_hash=validation_closure_record.content_hash,
                actor=producer,
                reason="validation completed and established the verified current artifact set",
                previous_set_id=previous_set.set_id if previous_set is not None else "",
            )
            current_set_pointer = session.context.registry.switch_current_artifact_set(
                current_set,
                prepared_promotion_record=promotion_record,
            )
            current_set_record = session.context.registry.get(current_set.set_id)
            if current_set_record is None or current_set_record.status != "ready":
                raise RuntimeError("current artifact set was not registered as ready")
            artifact_refs.extend(
                [
                    self._artifact_ref_from_record(current_set_record),
                    self._artifact_ref_from_record(current_set_pointer),
                ]
            )

        self._append_stage_trace_entries(
            session,
            [
                build_runtime_stage_trace_entry(
                    stage_name="stage4_validate",
                    step_name="run_review_validation",
                    producer=producer,
                    execution_mode=ExecutionMode.LOCAL,
                    metadata={
                        "validation_run_result_file": validation_run_result_file,
                        "report_file": report_file,
                        "manual_report_file": manual_report_file,
                        "execution_status": published_execution_status,
                        "validation_disposition": published_validation_disposition,
                        "declared_execution_status": validation_run_result.execution_status.value,
                        "declared_validation_disposition": (
                            validation_run_result.validation_disposition.value
                        ),
                        "failure_reason": contract_failure,
                        "provider_receipt_closure_complete": validation_closure_complete,
                        "provider_receipt_expected_call_count": len(validation_expected_call_ids),
                    },
                )
            ],
        )

        return StageResult(
            stage_name="stage4_validate",
            success=validation_success,
            artifacts=artifact_refs,
            metadata={
                "manual_review_count": validation_run_result.claim_verdict_counts.get("needs_review", 0),
                "validation_run_id": validation_run_result.validation_run_id,
                "execution_status": published_execution_status,
                "validation_disposition": published_validation_disposition,
                "declared_execution_status": validation_run_result.execution_status.value,
                "declared_validation_disposition": (
                    validation_run_result.validation_disposition.value
                ),
                "claim_verdict_counts": dict(validation_run_result.claim_verdict_counts),
                "contradicted_count": validation_run_result.contradicted_count,
                "canonical_artifact_id": canonical_record.artifact_id if canonical_record else "",
                "canonical_content_hash": canonical_record.content_hash if canonical_record else "",
                "failure_reason": contract_failure,
                "provider_receipt_closure_complete": validation_closure_complete,
                "provider_receipt_expected_call_count": len(validation_expected_call_ids),
                "provider_receipt_closure_hash": (
                    str(getattr(validation_closure, "closure_hash", "") or "")
                ),
            },
        )

    def derive_review_batch(
        self,
        session: AgentRuntimeSession,
        batch_spec: Any,
        *,
        derivation_id: str = "",
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.derive_review_batch",
    ) -> StageResult:
        """Materialize a verified parent Stage 1 subset without invoking Stage 1."""

        from services.review_batch import (
            ReviewBatchDerivationResultV1,
            ReviewBatchSpecV1,
            ReviewVariantDerivationResultV1,
            derive_review_batch,
        )

        if not isinstance(batch_spec, ReviewBatchSpecV1):
            raise TypeError("batch_spec must be ReviewBatchSpecV1")
        result = derive_review_batch(
            batch_spec,
            workspace=session.context.workspace,
            registry=session.context.registry,
            derivation_id=derivation_id,
            producer=producer,
        )
        if isinstance(result, ReviewBatchDerivationResultV1):
            metadata = {
                "batch_id": result.batch_id,
                "derivation_id": result.derivation_id,
                "parent_job_id": result.parent_job_id,
                "parent_artifact_id": result.parent_artifact_id,
                "parent_summary_hash": result.parent_summary_hash,
                "variant_count": len(batch_spec.variant_specs()),
                "completed_variant_count": len(result.variant_results),
                "failed_variant_count": len(result.failed_variants),
                "failed_variants": dict(result.failed_variants),
                "review_batch_manifest_path": result.manifest_path,
                "review_batch_projection_path": result.projection_path,
                "stage1_model_calls": 0,
            }
            self._append_stage_trace_entries(
                session,
                [
                    build_runtime_stage_trace_entry(
                        stage_name="stage1_derive",
                        step_name="coordinate_review_batch_variants",
                        producer=producer,
                        execution_mode=ExecutionMode.LOCAL,
                        metadata=metadata,
                    )
                ],
            )
            return StageResult(
                stage_name="derive_review_batch",
                success=result.success,
                artifacts=[self._artifact_ref_from_record(result.manifest_artifact)],
                metadata=metadata,
            )

        if not isinstance(result, ReviewVariantDerivationResultV1):
            raise TypeError("derive_review_batch returned an unsupported result")
        session.stage_host.summary_file = result.summary_path
        self._append_stage_trace_entries(
            session,
            [
                build_runtime_stage_trace_entry(
                    stage_name="stage1_derive",
                    step_name="materialize_verified_parent_subset",
                    producer=producer,
                    execution_mode=ExecutionMode.LOCAL,
                    metadata={
                        "parent_job_id": result.parent_job_id,
                        "parent_artifact_id": result.parent_artifact_id,
                        "parent_summary_hash": result.parent_summary_hash,
                        "selection_hash": result.selection_hash,
                        "selected_count": result.selected_count,
                        "stage1_model_calls": 0,
                    },
                )
            ],
        )
        return StageResult(
            stage_name="stage1_derive",
            success=True,
            artifacts=[
                self._artifact_ref_from_record(result.selection_artifact),
                self._artifact_ref_from_record(result.summary_artifact),
            ],
            metadata={
                "parent_job_id": result.parent_job_id,
                "parent_artifact_id": result.parent_artifact_id,
                "parent_summary_hash": result.parent_summary_hash,
                "selection_hash": result.selection_hash,
                "selected_count": result.selected_count,
                "stage1_model_calls": 0,
            },
        )

    def finalize(self, session: AgentRuntimeSession, *, status: str = "completed") -> str:
        return finalize_job_runtime(
            context=session.context,
            write_resume_report=session.runner._write_resume_report,
            status=status,
        )

    def architecture_gate_report(self, repo_root: str | Path = ".") -> Dict[str, Any]:
        root = Path(repo_root).resolve()
        scope = ArchitectureGateScope()
        scannable_paths = collect_scannable_paths(root, scope=scope)
        findings = scan_paths_for_forbidden_patterns(scannable_paths)

        def _relative_to_root(value: str | Path) -> str:
            path = Path(value).resolve()
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                return str(path)

        return {
            "scope": {
                "include": list(scope.canonical_prefixes()),
                "exclude": list(scope.excluded_prefixes()),
            },
            "scanned_paths": [path.relative_to(root).as_posix() for path in scannable_paths],
            "findings": [{"path": _relative_to_root(path), "pattern": pattern} for path, pattern in findings],
        }

    @staticmethod
    def _artifact_ref_from_record(record: ArtifactRecord) -> StageArtifactRef:
        return StageArtifactRef(
            artifact_role=record.artifact_role,
            artifact_type=record.artifact_type,
            artifact_version=record.artifact_version,
            path=record.path,
            artifact_id=record.artifact_id,
        )

    def _artifact_ref_for_path(
        self,
        session: AgentRuntimeSession,
        path: str,
        *,
        artifact_role: str,
        artifact_type: str,
        artifact_version: str,
        artifact_id: str = "",
    ) -> StageArtifactRef:
        abs_path = str(Path(path).resolve())
        if artifact_id:
            identity_record = session.context.registry.get(artifact_id)
            if identity_record is not None and identity_record.status == "ready":
                return self._artifact_ref_from_record(identity_record)
        for record in session.context.registry.list_records():
            if record.path == abs_path:
                return self._artifact_ref_from_record(record)
        return StageArtifactRef(
            artifact_role=artifact_role,
            artifact_type=artifact_type,
            artifact_version=artifact_version,
            path=abs_path,
            artifact_id=artifact_id,
        )

    def _append_stage_trace_entries(
        self,
        session: AgentRuntimeSession,
        entries: list[dict[str, Any]],
        *,
        artifact_name: str = "runtime_stage_trace.json",
    ) -> StageArtifactRef:
        artifact_path = session.context.workspace.artifact_path(artifact_name)
        if Path(artifact_path).exists():
            with open(artifact_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            existing_entries = [
                dict(item)
                for item in (payload.get("entries") or [])
                if isinstance(item, Mapping)
            ]
        else:
            existing_entries = list(self.initial_stage_trace())
        existing_entries.extend(entries)
        return self.write_stage_trace(session, existing_entries, artifact_name=artifact_name)

    @staticmethod
    def _build_generation_trace_entries(
        *,
        stage_name: str,
        producer: str,
        subagent_run_id: str | None,
        subagent_step_name: str,
        local_step_name: str,
        subagent_metadata: dict[str, Any] | None = None,
        local_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if subagent_run_id:
            entries.append(
                build_runtime_stage_trace_entry(
                    stage_name=stage_name,
                    step_name=subagent_step_name,
                    producer=producer,
                    subagent_run_id=subagent_run_id,
                    execution_mode=ExecutionMode.SUBAGENT,
                    metadata=subagent_metadata,
                )
            )
        local_entry = build_runtime_stage_trace_entry(
            stage_name=stage_name,
            step_name=local_step_name,
            producer=producer,
            execution_mode=ExecutionMode.LOCAL,
            metadata=local_metadata,
        )
        entries.append(local_entry)
        return entries

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping

from runtime.architecture_gates import ArchitectureGateScope, collect_scannable_paths, scan_paths_for_forbidden_patterns
from runtime.lifecycle import BootstrappedRuntimeContext, bootstrap_job_runtime, finalize_job_runtime
from runtime.job_spec import RuntimeJobSpec
from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_contracts import SourceBundle, StageArtifactRef, StageResult
from runtime.subagent_policy import ExecutionMode, build_runtime_stage_trace_entry, stage_policy_for
from runtime.validation_adapter import RuntimeValidationAdapter
from services.artifact_registry import (
    ArtifactDependencyRef,
    ArtifactDependencyRefV2,
    ArtifactRecord,
    file_sha256,
)
from services.job_runner import JobRunRequest, JobRunner, validate_job_request_options
from services.job_workspace import JobWorkspace, atomic_write_json
from services.queue_service import CancelToken


@dataclass(frozen=True)
class AgentRuntimeSession:
    runner: JobRunner
    request: JobRunRequest
    generator: Any
    context: BootstrappedRuntimeContext


class AgentRuntimeBridge:
    """Thin additive bridge used by the repo-local skill entrypoint."""

    def __init__(self, job_spec: RuntimeJobSpec) -> None:
        job_spec.validate()
        self.job_spec = job_spec

    def build_job_request(self) -> JobRunRequest:
        request = self.job_spec.to_job_request()
        error = validate_job_request_options(request)
        if error:
            raise ValueError(error)
        return request

    def build_source_bundle(self) -> SourceBundle:
        request = self.build_job_request()
        bundle = build_source_bundle_for_request(request, project_name=self.job_spec.project_name)
        if bundle.source_snapshot.get("canonical_ready") is False:
            return SourceBundle(
                source_mode=bundle.source_mode,
                project_name=bundle.project_name,
                paper_work_items=[],
                source_snapshot=dict(bundle.source_snapshot),
            )
        return bundle

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
        legacy_main: Any,
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

        generator = legacy_main.LiteratureReviewGenerator(
            request.config,
            project_name,
            request.pdf_folder,
            request.queue_file,
            request.zotero_report,
            request.library_path,
        )
        generator.cancel_token = active_cancel_token
        generator.progress_tracker = request.progress_tracker
        generator.free_mode_profile_path = request.free_mode_profile
        generator.free_mode_idea = request.free_mode_idea
        generator.summary_file_override = request.summary_file
        generator.summary_source_overrides = list(request.summary_sources)
        generator.audit_actor = str(self.job_spec.metadata.get("audit_actor") or "")
        generator.audit_reason = str(self.job_spec.metadata.get("audit_reason") or "")
        generator.audit_scope = dict(self.job_spec.metadata.get("audit_scope") or {})
        generator.reuse_stage1 = request.reuse_stage1
        generator.reuse_summary_files = list(request.reuse_summary_files)

        if not generator.load_configuration():
            raise RuntimeError("configuration load failed")
        if generator.config is None:
            raise RuntimeError("configuration is unavailable after load")

        generator_config = dict(generator.config)
        output_base_dir = generator_config.get("Paths", {}).get("output_path", "./output")
        resolved_project_name = runner._resolve_project_name_from_existing_workspaces(
            base_output_dir=output_base_dir,
            requested_project_name=project_name,
            action=request.action,
        )
        if resolved_project_name != project_name:
            project_name = resolved_project_name
            generator.project_name = resolved_project_name

        if workspace_preflight is not None:
            planned_workspace = JobWorkspace(
                output_base_dir,
                project_name,
                str(request.job_id or self.job_spec.job_id),
            )
            workspace_preflight(planned_workspace)

        prepared_sources = runner._prepare_source_inventory(
            generator=generator,
            request=request,
            project_name=project_name,
        )
        context = bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name=project_name,
            source_snapshot=runner._source_snapshot(generator, request),
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
        )
        return AgentRuntimeSession(
            runner=runner,
            request=request,
            generator=generator,
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
        atomic_write_json(path, source_bundle.to_dict())
        record = session.context.registry.register_file(
            artifact_role="source_bundle",
            artifact_type="source_bundle",
            artifact_version="v1",
            path=path,
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
        atomic_write_json(path, {"entries": trace_entries})
        record = session.context.registry.register_file(
            artifact_role="runtime_stage_trace",
            artifact_type="runtime_stage_trace",
            artifact_version="v1",
            path=path,
            producer="runtime.orchestrator.AgentRuntimeBridge.write_stage_trace",
            artifact_id="runtime_stage_trace",
        )
        return self._artifact_ref_from_record(record)

    def build_validation_adapter(
        self,
        session: AgentRuntimeSession,
        *,
        attempt_id: str = "",
        external_registry_resolver: Any | None = None,
    ) -> RuntimeValidationAdapter:
        return RuntimeValidationAdapter(
            session.generator,
            validation_attempt_id=attempt_id,
            validation_external_registry_resolver=external_registry_resolver,
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
    ) -> StageResult:
        generator = session.generator
        normalized_summaries = [dict(summary) for summary in summaries]
        source_bundle_record = session.context.registry.get("source_bundle")
        if source_bundle_record is None:
            self.persist_source_bundle(session, self.build_source_bundle())
            source_bundle_record = session.context.registry.get("source_bundle")
        if source_bundle_record is None or source_bundle_record.status != "ready":
            raise RuntimeError("stage1 source bundle is not registered and ready")
        source_dependencies = [
            ArtifactDependencyRef(
                dependency_kind="local_job",
                job_id=source_bundle_record.job_id,
                artifact_id=source_bundle_record.artifact_id,
                artifact_type=source_bundle_record.artifact_type,
                path=source_bundle_record.path,
                content_hash=source_bundle_record.content_hash,
            )
        ]
        generator.summaries = normalized_summaries
        generator._checkpoint_processed_papers = set()
        generator._checkpoint_failed_papers = set()

        for summary in normalized_summaries:
            paper = summary.get("paper_info")
            if not isinstance(paper, Mapping):
                continue
            paper_key = str(generator._paper_artifact_key(paper))
            if summary.get("status") == "success":
                generator._checkpoint_processed_papers.add(paper_key)
            else:
                generator._checkpoint_failed_papers.add(paper_key)

        if not generator.save_summaries(depends_on=source_dependencies):
            raise RuntimeError("stage1 summary persistence failed")

        manifest_path = ""
        if source_items is not None or rejected_candidates is not None:
            if not generator._materialize_effective_summaries(
                normalized_summaries,
                source_kind=source_kind,
                producer=producer,
                source_items=source_items,
                rejected_candidates=rejected_candidates,
                source_dependencies_override=source_dependencies,
            ):
                raise RuntimeError("summary source manifest persistence failed")
            manifest_path = generator._get_summary_source_manifest_path()

        artifact_refs = [
            self._artifact_ref_for_path(
                session,
                generator.summary_file,
                artifact_role="summary",
                artifact_type="summary_file",
                artifact_version="v1",
            ),
            self._artifact_ref_for_path(
                session,
                session.context.progress_path,
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
            if not generator._persist_paper_artifact(summary):
                raise RuntimeError("paper artifact persistence failed")
            paper = summary.get("paper_info")
            if summary.get("status") != "success" or not isinstance(paper, Mapping):
                continue
            artifact_refs.append(
                self._artifact_ref_for_path(
                    session,
                    generator._paper_artifact_path(paper),
                    artifact_role=generator.PAPER_ARTIFACT_ROLE,
                    artifact_type=generator.PAPER_ARTIFACT_TYPE,
                    artifact_version=generator.PAPER_ARTIFACT_VERSION,
                    artifact_id=generator._paper_artifact_id(paper),
                )
            )

        if write_excel_report:
            if not generator.generate_excel_report():
                raise RuntimeError("stage1 excel report generation failed")
            excel_path = generator._get_report_file_path("_analyzed_papers.xlsx")
            generator._register_workspace_artifact(
                artifact_role="report",
                artifact_type="excel_report",
                artifact_version="v1",
                path=excel_path,
                producer=producer,
                depends_on=[ArtifactDependencyRef(artifact_type="summary_file", path=generator.summary_file)],
            )
            artifact_refs.append(
                self._artifact_ref_for_path(
                    session,
                    excel_path,
                    artifact_role="report",
                    artifact_type="excel_report",
                    artifact_version="v1",
                )
            )

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
                    "processed_count": len(generator._checkpoint_processed_papers),
                    "failed_count": len(generator._checkpoint_failed_papers),
                },
            ),
        )

        return StageResult(
            stage_name="stage1_analyze",
            success=True,
            artifacts=artifact_refs,
            metadata={
                "summary_count": len(normalized_summaries),
                "processed_count": len(generator._checkpoint_processed_papers),
                "failed_count": len(generator._checkpoint_failed_papers),
            },
        )

    def persist_outline(
        self,
        session: AgentRuntimeSession,
        outline_text: str,
        *,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.persist_outline",
        subagent_run_id: str | None = None,
    ) -> StageResult:
        outline_path = session.generator._write_outline_artifact(outline_text, producer=producer)
        self._append_stage_trace_entries(
            session,
            self._build_generation_trace_entries(
                stage_name="stage2_outline",
                producer=producer,
                subagent_run_id=subagent_run_id,
                subagent_step_name="subagent_outline_complete",
                local_step_name="persist_outline_artifact",
                subagent_metadata={"outline_length": len(outline_text)},
                local_metadata={"outline_path": outline_path},
            ),
        )
        return StageResult(
            stage_name="stage2_outline",
            success=True,
            artifacts=[
                self._artifact_ref_for_path(
                    session,
                    outline_path,
                    artifact_role=session.generator.OUTLINE_ARTIFACT_ROLE,
                    artifact_type=session.generator.OUTLINE_ARTIFACT_TYPE,
                    artifact_version=session.generator.OUTLINE_ARTIFACT_VERSION,
                    artifact_id=session.generator.OUTLINE_ARTIFACT_ID,
                )
            ],
            metadata={"outline_path": outline_path},
        )

    def persist_outline_v2_replay(
        self,
        session: AgentRuntimeSession,
        *,
        response_manifest_path: str,
        adopted_by: str,
        adoption_reason: str,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.persist_outline_v2_replay",
    ) -> StageResult:
        """Run Outline v2 from hash-bound subagent responses and adopt it locally."""

        from dataclasses import replace

        from outline.pipeline import V2Pipeline
        from runtime.outline_v2_replay import (
            OutlineV2ReplayCaller,
            OutlineV2ReplayError,
        )

        generator = session.generator
        if not generator._outline_v2_enabled():
            raise RuntimeError("Outline v2 replay requires Outline Intelligence v2")

        replay = OutlineV2ReplayCaller(response_manifest_path)
        replay.verify_artifacts()
        replay_dependencies: list[ArtifactDependencyRef] = []
        for binding in replay.artifact_bindings:
            if file_sha256(binding.path) != binding.content_hash:
                raise OutlineV2ReplayError(
                    "Outline v2 replay evidence is missing or stale: "
                    f"{binding.artifact_kind} {binding.call_index}"
                )
            record = session.context.registry.register_file(
                artifact_role=f"outline_v2_subagent_{binding.artifact_kind}",
                artifact_type=f"outline_v2_subagent_{binding.artifact_kind}",
                artifact_version="v1",
                path=binding.path,
                producer=producer,
                artifact_id=(
                    f"outline_v2_subagent_{binding.artifact_kind}:"
                    f"{binding.call_index:03d}"
                ),
                metadata={"call_index": binding.call_index},
            )
            replay_dependencies.append(
                ArtifactDependencyRef(
                    artifact_type=record.artifact_type,
                    path=record.path,
                    content_hash=record.content_hash,
                    dependency_kind="local_job",
                    job_id=record.job_id,
                    artifact_id=record.artifact_id,
                )
            )

        replay_manifest = session.context.registry.register_file(
            artifact_role="outline_v2_subagent_response_manifest",
            artifact_type="outline_v2_subagent_response_manifest",
            artifact_version="v1",
            path=replay.manifest_path,
            producer=producer,
            artifact_id="outline_v2_subagent_response_manifest",
            depends_on=replay_dependencies,
            metadata={
                "expected_call_count": replay.expected_call_count,
                "subagent_run_ids": list(replay.subagent_run_ids),
            },
        )
        replay_manifest_dependency = ArtifactDependencyRef(
            artifact_type=replay_manifest.artifact_type,
            path=replay_manifest.path,
            content_hash=replay_manifest.content_hash,
            dependency_kind="local_job",
            job_id=replay_manifest.job_id,
            artifact_id=replay_manifest.artifact_id,
        )

        compat = generator._ensure_compat_config()
        config_errors = compat.validate_outline_v2_config()
        if config_errors:
            raise RuntimeError(
                "Outline v2 replay configuration is invalid: "
                + "; ".join(str(error) for error in config_errors)
            )
        pipeline = V2Pipeline(
            job_id=session.context.workspace.job_id,
            summaries=[dict(summary) for summary in generator.summaries],
            config_view=compat,
            artifact_registry=session.context.registry,
            workspace=session.context.workspace,
            output_dir=generator.output_dir or "",
            project_name=generator.project_name or "review",
            model_caller=replay,
            model_dependencies=[replay_manifest_dependency],
            logger=generator.logger,
        )
        result = pipeline.run(
            candidate_count=compat.outline_candidate_count(),
            test_dev_mode=False,
            generator_model=compat.outline_model(),
            structure_critic=compat.structure_critic_model(),
            coverage_critic=compat.coverage_critic_model(),
            arbitrator_model=compat.arbitrator_model(),
            paper_artifacts=generator._load_paper_artifacts_for_outline_v2(),
        )
        replay.assert_consumed()
        if not result.ok:
            raise RuntimeError(
                "Outline v2 replay failed: " + "; ".join(str(error) for error in result.errors)
            )
        if result.stage_health is None:
            raise RuntimeError("Outline v2 replay produced no stage health evidence")
        result.stage_health = replace(
            result.stage_health,
            execution_mode="subagent_replay",
            stages=tuple(
                replace(
                    entry,
                    fallback_provenance=(
                        "codex_native_subagent"
                        if entry.fallback_provenance == "provider"
                        else entry.fallback_provenance
                    ),
                )
                for entry in result.stage_health.stages
            ),
        )

        pipeline.persist_artifacts(result)
        adopted, adopted_path, adoption_message = pipeline.adopt(
            result,
            adopted_by=adopted_by,
        )
        if adopted is None or not adopted_path:
            raise RuntimeError(
                "Outline v2 replay explicit adoption failed: " + adoption_message
            )
        adopted_record = session.context.registry.get("adopted_final_outline")
        if adopted_record is None or adopted_record.status != "ready":
            raise RuntimeError("Outline v2 replay did not register an adopted outline")

        manifest_run_id = (
            "outline-v2-replay:" + replay_manifest.content_hash[:16]
        )
        self._append_stage_trace_entries(
            session,
            self._build_generation_trace_entries(
                stage_name="stage2_outline",
                producer=producer,
                subagent_run_id=manifest_run_id,
                subagent_step_name="subagent_outline_v2_responses_complete",
                local_step_name="persist_and_adopt_outline_v2_replay",
                subagent_metadata={
                    "model_call_count": replay.expected_call_count,
                    "subagent_run_ids": list(replay.subagent_run_ids),
                },
                local_metadata={
                    "adopted_outline_path": adopted_path,
                    "adopted_by": adopted_by,
                    "adoption_reason": adoption_reason,
                    "response_manifest_hash": replay_manifest.content_hash,
                },
            ),
        )
        return StageResult(
            stage_name="stage2_outline",
            success=True,
            artifacts=[
                self._artifact_ref_from_record(adopted_record),
                self._artifact_ref_from_record(replay_manifest),
            ],
            metadata={
                "outline_mode": "v2_subagent_replay",
                "stage_health_artifact_id": "outline_stage_health",
                "model_call_count": replay.expected_call_count,
                "response_manifest_path": replay_manifest.path,
                "response_manifest_hash": replay_manifest.content_hash,
                "adoption_message": adoption_message,
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
    ) -> StageResult:
        from docx_writer import generate_apa_references_from_manifest, rebuild_review_docx_from_structured_artifacts

        generator = session.generator
        review_word_path = word_file or generator._get_review_word_file_path()
        initial_references = list(references or [])
        if not generator._persist_citation_ref_catalog():
            raise RuntimeError("citation ref catalog persistence failed")
        if not generator._persist_review_draft_v2(
            outline_file=outline_file,
            review_sections=review_sections,
            references=initial_references,
            word_file=review_word_path,
            generation_mode=generation_mode,
        ):
            raise RuntimeError("review_draft_v2 persistence failed")

        review_draft_path = generator._review_draft_v2_path()
        if not generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=review_word_path,
        ):
            raise RuntimeError("citation manifest persistence failed")

        citation_manifest = generator._load_citation_manifest()
        if not citation_manifest:
            raise RuntimeError("canonical citation manifest is unavailable")

        canonical_references = list(references or [])
        if not canonical_references:
            canonical_references = generate_apa_references_from_manifest(
                citation_manifest,
                generator,
                allow_compat_fallback=False,
            )

        if not generator._persist_review_draft(
            outline_file=outline_file,
            review_sections=review_sections,
            references=canonical_references,
            word_file=review_word_path,
            generation_mode=generation_mode,
        ):
            raise RuntimeError("review_draft_v1 persistence failed")

        if not generator._persist_review_draft_v2(
            outline_file=outline_file,
            review_sections=review_sections,
            references=canonical_references,
            word_file=review_word_path,
            generation_mode=generation_mode,
        ):
            raise RuntimeError("review_draft_v2 canonical reference update failed")

        # The manifest depends on the final review_draft_v2 bytes.  Rebuild it
        # after canonical references are projected so its dependency hash does
        # not point at the provisional draft.
        if not generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=review_word_path,
        ):
            raise RuntimeError("citation manifest final dependency refresh failed")
        citation_manifest = generator._load_citation_manifest()
        if not citation_manifest:
            raise RuntimeError("final canonical citation manifest is unavailable")

        with open(review_draft_path, "r", encoding="utf-8") as handle:
            review_draft = json.load(handle)

        if rebuild_docx:
            rebuild_review_docx_from_structured_artifacts(
                generator,
                review_draft,
                citation_manifest,
                review_word_path,
                allow_compat_fallback=False,
            )

        generator._register_workspace_artifact(
            artifact_role="review_docx",
            artifact_type="review_docx",
            artifact_version="v1",
            path=review_word_path,
            producer=producer,
            depends_on=[
                ArtifactDependencyRef(
                    artifact_type=generator.REVIEW_DRAFT_V2_ARTIFACT_TYPE,
                    path=review_draft_path,
                ),
                ArtifactDependencyRef(
                    artifact_type=generator.CITATION_MANIFEST_ARTIFACT_TYPE,
                    path=generator._citation_manifest_path(),
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
                local_step_name="persist_review_chain",
                subagent_metadata={"section_count": len(review_sections)},
                local_metadata={"word_file": review_word_path},
            ),
        )

        return StageResult(
            stage_name="stage3_review",
            success=True,
            artifacts=[
                self._artifact_ref_for_path(
                    session,
                    generator._review_draft_path(),
                    artifact_role=generator.REVIEW_DRAFT_ARTIFACT_ROLE,
                    artifact_type=generator.REVIEW_DRAFT_ARTIFACT_TYPE,
                    artifact_version=generator.REVIEW_DRAFT_ARTIFACT_VERSION,
                    artifact_id=generator.REVIEW_DRAFT_ARTIFACT_ID,
                ),
                self._artifact_ref_for_path(
                    session,
                    review_draft_path,
                    artifact_role=generator.REVIEW_DRAFT_V2_ARTIFACT_ROLE,
                    artifact_type=generator.REVIEW_DRAFT_V2_ARTIFACT_TYPE,
                    artifact_version=generator.REVIEW_DRAFT_V2_ARTIFACT_VERSION,
                    artifact_id=generator.REVIEW_DRAFT_V2_ARTIFACT_ID,
                ),
                self._artifact_ref_for_path(
                    session,
                    generator._citation_manifest_path(),
                    artifact_role=generator.CITATION_MANIFEST_ARTIFACT_ROLE,
                    artifact_type=generator.CITATION_MANIFEST_ARTIFACT_TYPE,
                    artifact_version=generator.CITATION_MANIFEST_ARTIFACT_VERSION,
                    artifact_id=generator.CITATION_MANIFEST_ARTIFACT_ID,
                ),
                self._artifact_ref_for_path(
                    session,
                    review_word_path,
                    artifact_role="review_docx",
                    artifact_type="review_docx",
                    artifact_version="v1",
                ),
            ],
            metadata={
                "section_count": len(review_sections),
                "reference_count": len(canonical_references),
                "word_file": review_word_path,
            },
        )

    def run_validation(
        self,
        session: AgentRuntimeSession,
        *,
        attempt_id: str = "",
        validator_module: Any | None = None,
        external_registry_resolver: Any | None = None,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.run_validation",
    ) -> StageResult:
        if validator_module is None:
            import validator as validator_module  # type: ignore

        adapter = self.build_validation_adapter(
            session,
            attempt_id=attempt_id,
            external_registry_resolver=external_registry_resolver,
        )
        result = dict(validator_module.run_review_validation(adapter) or {})

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
            legacy_report = result.get("report")
            legacy_payload: Mapping[str, Any] = legacy_report if isinstance(legacy_report, Mapping) else {}
            candidate_result = ValidationRunResultV1.from_legacy_report(
                legacy_payload,
                job_id=session.context.workspace.job_id,
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
        validation_contract_complete = bool(
            validation_run_result.contract_satisfied
            and validation_run_result.compatibility_status == "verified"
            and not dependency_error
        )
        validation_success = bool(
            validation_run_result.execution_status is ValidationExecutionStatus.SUCCEEDED
            and validation_contract_complete
        )
        contract_failure = ""
        if validation_run_result.execution_status is ValidationExecutionStatus.SUCCEEDED:
            if dependency_error:
                contract_failure = "validation_input_dependencies_unverified"
            elif not validation_contract_complete:
                contract_failure = "validation_contract_incomplete"
        published_execution_status = validation_run_result.execution_status.value
        published_validation_disposition = validation_run_result.validation_disposition.value
        if contract_failure:
            published_execution_status = ValidationExecutionStatus.FAILED.value
            published_validation_disposition = "unvalidated"

        canonical_record: ArtifactRecord | None = None
        if validation_run_result_file:
            canonical_record = session.context.registry.register_file(
                artifact_role="validation",
                artifact_type="validation_run_result",
                artifact_version="v1",
                path=validation_run_result_file,
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
            record = session.context.registry.register_file(
                artifact_role="validation_projection",
                artifact_type=artifact_type,
                artifact_version="v1",
                path=path,
                producer=producer,
                depends_on=projection_dependencies,
                artifact_id=f"{artifact_prefix}:{Path(path).name}",
            )
            artifact_refs.append(self._artifact_ref_from_record(record))

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
        session.generator.summary_file = result.summary_path
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

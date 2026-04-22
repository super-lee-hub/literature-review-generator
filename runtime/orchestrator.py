from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from runtime.architecture_gates import ArchitectureGateScope, collect_scannable_paths, scan_paths_for_forbidden_patterns
from runtime.lifecycle import BootstrappedRuntimeContext, bootstrap_job_runtime, finalize_job_runtime
from runtime.job_spec import RuntimeJobSpec
from runtime.source_intake import build_source_bundle_for_request
from runtime.stage_contracts import SourceBundle, StageArtifactRef, StageResult
from runtime.subagent_policy import ExecutionMode, build_runtime_stage_trace_entry, stage_policy_for
from runtime.validation_adapter import RuntimeValidationAdapter
from services.artifact_registry import ArtifactDependencyRef, ArtifactRecord
from services.job_runner import JobRunRequest, JobRunner, validate_job_request_options
from services.job_workspace import atomic_write_json
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
        return build_source_bundle_for_request(request, project_name=self.job_spec.project_name)

    def stage_policies(self) -> Dict[str, Dict[str, Any]]:
        return {
            stage_name: stage_policy_for(stage_name).to_dict()
            for stage_name in ("source_intake", "stage1_analyze", "stage2_outline", "stage3_review", "stage4_validate")
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

    def bootstrap(self, legacy_main: Any, *, cancel_token: CancelToken | None = None) -> AgentRuntimeSession:
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

        context = bootstrap_job_runtime(
            request=request,
            generator=generator,
            project_name=project_name,
            source_snapshot=runner._source_snapshot(generator, request),
            request_snapshot=runner._request_snapshot(request),
            build_workspace=runner._build_workspace,
            write_resume_report=runner._write_resume_report,
        )
        return AgentRuntimeSession(
            runner=runner,
            request=request,
            generator=generator,
            context=context,
        )

    def persist_source_bundle(self, session: AgentRuntimeSession, source_bundle: SourceBundle) -> StageArtifactRef:
        path = session.context.workspace.artifact_path("source_bundle.json")
        atomic_write_json(path, source_bundle.to_dict())
        record = session.context.registry.register_file(
            artifact_role="source_bundle",
            artifact_type="source_bundle",
            artifact_version="v1",
            path=path,
            producer="runtime.orchestrator.AgentRuntimeBridge.persist_source_bundle",
            artifact_id="source_bundle",
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

    def build_validation_adapter(self, session: AgentRuntimeSession) -> RuntimeValidationAdapter:
        return RuntimeValidationAdapter(session.generator)

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

        if not generator.save_summaries():
            raise RuntimeError("stage1 summary persistence failed")

        manifest_path = ""
        if source_items is not None or rejected_candidates is not None:
            if not generator._materialize_effective_summaries(
                normalized_summaries,
                source_kind=source_kind,
                producer=producer,
                source_items=source_items,
                rejected_candidates=rejected_candidates,
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
                    artifact_version="v1",
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
        validator_module: Any | None = None,
        producer: str = "runtime.orchestrator.AgentRuntimeBridge.run_validation",
    ) -> StageResult:
        if validator_module is None:
            import validator as validator_module  # type: ignore

        adapter = self.build_validation_adapter(session)
        result = dict(validator_module.run_review_validation(adapter) or {})

        report_file = str(result.get("report_file") or "")
        manual_report_file = str(result.get("manual_report_file") or "")
        report_obj = result.get("report")
        report_id = str(getattr(report_obj, "report_id", "") or "")
        report_version = str(getattr(report_obj, "artifact_version", "") or "v1")

        artifact_refs: list[StageArtifactRef] = []
        depends_on = [
            ArtifactDependencyRef(
                artifact_type=session.generator.REVIEW_DRAFT_V2_ARTIFACT_TYPE,
                path=session.generator._review_draft_v2_path(),
            ),
            ArtifactDependencyRef(
                artifact_type=session.generator.CITATION_MANIFEST_ARTIFACT_TYPE,
                path=session.generator._citation_manifest_path(),
            ),
        ]

        if report_file:
            record = session.context.registry.register_file(
                artifact_role="validation",
                artifact_type="validation_report",
                artifact_version=report_version,
                path=report_file,
                producer=producer,
                depends_on=depends_on,
                artifact_id=report_id or None,
            )
            artifact_refs.append(self._artifact_ref_from_record(record))

        if manual_report_file:
            record = session.context.registry.register_file(
                artifact_role="validation",
                artifact_type="manual_review_report",
                artifact_version="v1",
                path=manual_report_file,
                producer=producer,
                depends_on=depends_on,
                artifact_id=f"manual_review_report:{Path(manual_report_file).name}",
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
                    metadata={"report_file": report_file, "manual_report_file": manual_report_file},
                )
            ],
        )

        return StageResult(
            stage_name="stage4_validate",
            success=bool(result.get("success", False)),
            artifacts=artifact_refs,
            metadata={
                "manual_review_count": len(result.get("manual_review_items", []) or []),
                "report_id": report_id,
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

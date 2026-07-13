"""V2 pipeline orchestrator for Outline Intelligence v2.

Orchestrates the complete v2 artifact chain:
literature_map -> synthesis_flow -> candidates -> critiques ->
arbitration -> final_outline -> coverage_audit -> [adoption]
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.artifact_registry import ArtifactDependencyRef

from outline.v2_models import (
    AdoptedFinalOutline,
    ArbitrationReport,
    CoverageAudit,
    FinalOutline,
    LiteratureMap,
    OutlineCandidates,
    OutlineCritiquesV2,
    SynthesisFlow,
    compute_content_hash,
)
from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import (
    CandidateGenerationError,
    deterministic_candidate_generation_report,
    generate_candidates_deterministic,
    generate_candidates_production_with_report,
    validate_candidate_count,
)
from outline.critique_v2 import (
    build_critiques_v2,
    run_critique_production,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.arbitration_v2 import (
    arbitrate_deterministic,
    arbitrate_production,
    build_final_outline,
    complete_final_outline_coverage,
)
from outline.coverage_audit import run_coverage_audit
from outline.adoption import adopt_final_outline, write_adopted_outline
from outline.prompt_budget import PromptBudgetV1
from outline.stage_health import (
    OutlineStageHealthV1,
    StageHealthCollector,
    StageHealthEntryV1,
    make_test_double_entry,
)
from outline.v2_config import OutlineQualityGateConfig, OutlineV2Config


ModelCaller = Callable[[str, str, Dict[str, Any]], Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class V2PipelineResult:
    """Result of running the v2 pipeline."""

    def __init__(self):
        self.literature_map: Optional[LiteratureMap] = None
        self.synthesis_flow: Optional[SynthesisFlow] = None
        self.candidates: Optional[OutlineCandidates] = None
        self.candidate_generation_report: Optional[Dict[str, Any]] = None
        self.critiques: Optional[OutlineCritiquesV2] = None
        self.arbitration_report: Optional[ArbitrationReport] = None
        self.final_outline: Optional[FinalOutline] = None
        self.coverage_audit: Optional[CoverageAudit] = None
        self.stage_health: Optional[OutlineStageHealthV1] = None
        self.adopted_outline: Optional[AdoptedFinalOutline] = None
        self.errors: List[str] = []
        self.warnings: List[str] = []

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


class V2Pipeline:
    """Orchestrates the complete v2 outline generation pipeline."""

    def __init__(
        self,
        job_id: str,
        summaries: Sequence[Dict[str, Any]],
        config_view: Any = None,
        artifact_registry: Any = None,
        workspace: Any = None,
        output_dir: str = "",
        project_name: str = "",
        model_caller: ModelCaller | None = None,
        logger: Any = None,
    ):
        self.job_id = job_id
        self.summaries = summaries
        self.config_view = config_view
        self.registry = artifact_registry
        self.workspace = workspace
        self.output_dir = output_dir
        self.project_name = project_name
        self.model_caller = model_caller
        self.logger = logger

    def run(
        self,
        candidate_count: int = 3,
        test_dev_mode: bool = False,
        generator_model: str = "Outline_API",
        structure_critic: str = "Writer_API",
        coverage_critic: str = "Primary_Reader_API",
        arbitrator_model: str = "Outline_API",
        paper_artifacts: Sequence[Dict[str, Any]] | None = None,
        model_caller: ModelCaller | None = None,
    ) -> V2PipelineResult:
        result = V2PipelineResult()

        # Validate candidate count
        count_errors = validate_candidate_count(candidate_count, test_dev_mode)
        if count_errors:
            result.errors.extend(count_errors)
            return result

        # Phase 1: Literature map
        lit_map = build_literature_map(self.summaries, self.job_id, paper_artifacts)
        result.literature_map = lit_map
        if lit_map.blocking_diagnostics:
            result.warnings.extend(
                f"LitMap: {d.get('message', str(d))}" for d in lit_map.blocking_diagnostics
            )

        # Phase 2: Synthesis flow
        synth_flow = build_synthesis_flow(lit_map, self.job_id)
        result.synthesis_flow = synth_flow

        # Phase 3: Multi-candidate generation
        active_model_caller = model_caller or self.model_caller
        raw_config = getattr(self.config_view, "raw_config", None)
        quality_gate = OutlineQualityGateConfig.from_config(raw_config or {})
        outline_config = OutlineV2Config.from_config(
            raw_config or {}, is_test_fixture_mode=test_dev_mode
        )
        route_defaults = {
            "Outline_API": ("outline_max_tokens", 16000),
            "Writer_API": ("writer_max_tokens", 32000),
            "Primary_Reader_API": ("primary_max_tokens", 5000),
        }

        def route_budget(route: str) -> PromptBudgetV1:
            config = raw_config or {}
            api_section = dict(config.get(route, {}))
            api_parameters = dict(config.get("API_Parameters", {}))
            max_token_key, default_output = route_defaults.get(
                route, ("outline_max_tokens", outline_config.max_output_tokens)
            )
            try:
                context_limit = int(
                    api_section.get("max_context_tokens") or outline_config.model_context_limit
                )
                max_output = int(
                    api_section.get("max_tokens")
                    or api_parameters.get(max_token_key)
                    or default_output
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid Outline prompt budget for {route}: {exc}") from exc
            return PromptBudgetV1(
                model_context_limit=context_limit,
                max_output_tokens=max_output,
            )

        candidate_prompt_budget = route_budget(generator_model)
        health_collector = StageHealthCollector(active_model_caller)

        def guarded_model_caller(route: str, prompt: str, metadata: Dict[str, Any]) -> Any:
            prompt_budget = route_budget(route)
            prompt_budget.assert_fits(prompt, stage=str(metadata.get("stage") or "outline_v2"))
            enriched = dict(metadata)
            enriched["prompt_budget"] = prompt_budget.metadata(prompt)
            return health_collector.call(route, prompt, enriched)

        health_entries: List[StageHealthEntryV1] = []
        try:
            if test_dev_mode:
                candidates = generate_candidates_deterministic(
                    lit_map, synth_flow, candidate_count, generator_model, self.job_id
                )
                result.candidate_generation_report = deterministic_candidate_generation_report(
                    candidates, candidate_count, generator_model, lit_map, synth_flow, quality_gate
                )
            else:
                candidates, report = generate_candidates_production_with_report(
                    lit_map,
                    synth_flow,
                    candidate_count,
                    generator_model,
                    guarded_model_caller,
                    quality_gate,
                    source_summaries=self.summaries,
                    prompt_budget=candidate_prompt_budget,
                )
                result.candidate_generation_report = report
        except CandidateGenerationError as exc:
            result.candidate_generation_report = exc.report
            result.errors.append(str(exc))
            self.persist_candidate_generation_report(result)
            return result
        result.candidates = candidates
        if test_dev_mode:
            health_entries.append(
                make_test_double_entry("outline_candidates", generator_model, lit_map.to_dict(), candidates.to_dict())
            )
        else:
            for supporting_stage in ("outline_stream_synthesis", "outline_synthesis_merge"):
                if health_collector.has_calls(supporting_stage):
                    health_entries.append(
                        health_collector.entry(supporting_stage, generator_model, schema_valid=True)
                    )
            candidate_fallback = next(
                (
                    candidate.provenance
                    for candidate in candidates.candidates
                    if candidate.provenance in {"deterministic_fallback", "deterministic_topup"}
                ),
                "provider",
            )
            health_entries.append(
                health_collector.entry(
                    "outline_candidates",
                    generator_model,
                    schema_valid=True,
                    fallback_provenance=candidate_fallback,
                    degraded_reason=(
                        "production candidate generation used deterministic fallback"
                        if candidate_fallback != "provider"
                        else ""
                    ),
                )
            )

        # Phase 4: Role-specific critique
        if test_dev_mode:
            structure_run = run_structure_critique_deterministic(candidates, structure_critic)
            coverage_run = run_coverage_critique_deterministic(candidates, coverage_critic)
        else:
            structure_run = run_critique_production(
                candidates, structure_critic, "structure", guarded_model_caller
            )
            coverage_run = run_critique_production(
                candidates, coverage_critic, "coverage", guarded_model_caller
            )
        critiques_v2 = build_critiques_v2(
            structure_run, coverage_run,
            [c.candidate_id for c in candidates.candidates],
        )
        result.critiques = critiques_v2
        if test_dev_mode:
            health_entries.extend(
                [
                    make_test_double_entry("structure_critique", structure_critic, candidates.to_dict(), structure_run.to_dict()),
                    make_test_double_entry("coverage_critique", coverage_critic, candidates.to_dict(), coverage_run.to_dict()),
                ]
            )
        else:
            health_entries.extend(
                [
                    health_collector.entry("structure_critique", structure_critic, schema_valid=True),
                    health_collector.entry("coverage_critique", coverage_critic, schema_valid=True),
                ]
            )

        # Phase 5: Arbitration
        if test_dev_mode:
            arbitration_report = arbitrate_deterministic(candidates, critiques_v2, arbitrator_model)
        else:
            arbitration_report = arbitrate_production(
                candidates, critiques_v2, arbitrator_model, guarded_model_caller
            )
        result.arbitration_report = arbitration_report
        if test_dev_mode:
            health_entries.append(
                make_test_double_entry(
                    "outline_arbitration", arbitrator_model, critiques_v2.to_dict(), arbitration_report.to_dict()
                )
            )
        else:
            arbitration_payload = arbitration_report.to_dict()
            fallback_reason = str(
                (arbitration_payload.get("final_decision") or {}).get("fallback_reason") or ""
            )
            fallback = (
                "deterministic_fallback"
                if fallback_reason or "fallback" in arbitration_report.merged_strategy.lower()
                else "provider"
            )
            health_entries.append(
                health_collector.entry(
                    "outline_arbitration",
                    arbitrator_model,
                    schema_valid=True,
                    fallback_provenance=fallback,
                    degraded_reason=fallback_reason,
                )
            )

        # Phase 6: Final outline
        lit_map_hash = compute_content_hash(lit_map.to_dict())
        synth_flow_hash = compute_content_hash(synth_flow.to_dict())
        final_outline = build_final_outline(
            candidates, arbitration_report, lit_map_hash, synth_flow_hash, self.job_id
        )
        final_outline = complete_final_outline_coverage(
            final_outline,
            lit_map,
            synth_flow,
            min_canonical_coverage=quality_gate.min_canonical_coverage,
        )
        result.final_outline = final_outline

        # Phase 7: Coverage audit
        audit = run_coverage_audit(final_outline, lit_map, synth_flow, quality_gate)
        result.coverage_audit = audit

        result.stage_health = OutlineStageHealthV1(
            job_id=self.job_id,
            execution_mode="test_dev" if test_dev_mode else "production",
            stages=tuple(health_entries),
            source_final_outline_hash=compute_content_hash(final_outline.to_dict()),
            source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
        )

        return result

    def persist_candidate_generation_report(self, result: V2PipelineResult) -> Dict[str, str]:
        """Best-effort write/register for the optional candidate diagnostics sidecar."""
        if not result.candidate_generation_report:
            return {}
        artifacts_dir = self._artifacts_dir()
        report_path = os.path.join(
            artifacts_dir,
            f"{self.project_name}_outline_candidate_generation_report.json",
        )
        try:
            self._write_json(report_path, result.candidate_generation_report)
            dependency_refs: List[ArtifactDependencyRef] = []
            lit_path = os.path.join(artifacts_dir, f"{self.project_name}_literature_map.json")
            synth_path = os.path.join(artifacts_dir, f"{self.project_name}_synthesis_flow.json")
            for artifact_type, dep_path in (
                ("literature_map", lit_path),
                ("synthesis_flow", synth_path),
            ):
                if os.path.exists(dep_path):
                    dependency_refs.append(self._dependency_ref(artifact_type, dep_path))
            self._register(
                "candidate_generation_report",
                "candidate_generation_report",
                "v1",
                report_path,
                "v2_pipeline",
                depends_on=dependency_refs,
            )
            return {"candidate_generation_report": report_path}
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to persist candidate generation report: {exc}")
            return {}

    def persist_artifacts(self, result: V2PipelineResult) -> Dict[str, str]:
        """Write all v2 artifacts to disk and register them.

        Returns dict mapping artifact type to file path.
        """
        paths: Dict[str, str] = {}
        artifacts_dir = self._artifacts_dir()

        if not result.literature_map or not result.synthesis_flow or not result.candidates:
            raise ValueError("Cannot persist incomplete v2 pipeline result")
        if not result.critiques or not result.arbitration_report or not result.final_outline:
            raise ValueError("Cannot persist incomplete v2 pipeline result")
        if not result.coverage_audit:
            raise ValueError("Cannot persist incomplete v2 pipeline result")
        if not result.stage_health:
            raise ValueError("Cannot persist v2 pipeline result without stage health")

        dependency_records: Dict[str, ArtifactDependencyRef] = {}

        # Literature map
        lit_map_path = os.path.join(artifacts_dir, f"{self.project_name}_literature_map.json")
        self._write_json(lit_map_path, result.literature_map.to_dict())
        paths["literature_map"] = lit_map_path
        dependency_records["literature_map"] = self._register(
            "literature_map", "literature_map", "v1", lit_map_path, "v2_pipeline"
        )

        # Synthesis flow
        synth_path = os.path.join(artifacts_dir, f"{self.project_name}_synthesis_flow.json")
        self._write_json(synth_path, result.synthesis_flow.to_dict())
        paths["synthesis_flow"] = synth_path
        dependency_records["synthesis_flow"] = self._register(
            "synthesis_flow", "synthesis_flow", "v1", synth_path, "v2_pipeline",
            depends_on=[dependency_records["literature_map"]],
        )

        # Candidates
        cand_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_candidates.json")
        self._write_json(cand_path, result.candidates.to_dict())
        paths["outline_candidates"] = cand_path
        dependency_records["outline_candidates"] = self._register(
            "outline_candidates", "outline_candidates", "v1", cand_path, "v2_pipeline",
            depends_on=[
                dependency_records["literature_map"],
                dependency_records["synthesis_flow"],
            ],
        )

        # Candidate generation diagnostics sidecar (not an arbitration/adoption/runtime input)
        report_paths = self.persist_candidate_generation_report(result)
        paths.update(report_paths)

        # Critiques
        crit_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_critiques.json")
        self._write_json(crit_path, result.critiques.to_dict())
        paths["outline_critiques"] = crit_path
        dependency_records["outline_critiques"] = self._register(
            "outline_critiques", "outline_critiques", "v1", crit_path, "v2_pipeline",
            depends_on=[dependency_records["outline_candidates"]],
        )

        # Arbitration report
        arb_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_arbitration_report.json")
        self._write_json(arb_path, result.arbitration_report.to_dict())
        paths["outline_arbitration_report"] = arb_path
        dependency_records["outline_arbitration_report"] = self._register(
            "outline_arbitration_report",
            "outline_arbitration_report",
            "v1",
            arb_path,
            "v2_pipeline",
            depends_on=[
                dependency_records["outline_candidates"],
                dependency_records["outline_critiques"],
            ],
        )

        # Final outline
        final_path = os.path.join(artifacts_dir, f"{self.project_name}_final_outline.json")
        self._write_json(final_path, result.final_outline.to_dict())
        paths["final_outline"] = final_path
        dependency_records["final_outline"] = self._register(
            "final_outline", "final_outline", "v2", final_path, "v2_pipeline",
            depends_on=[
                dependency_records["outline_candidates"],
                dependency_records["outline_arbitration_report"],
            ],
        )

        # Coverage audit
        audit_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_coverage_audit.json")
        self._write_json(audit_path, result.coverage_audit.to_dict())
        paths["outline_coverage_audit"] = audit_path
        dependency_records["outline_coverage_audit"] = self._register(
            "outline_coverage_audit", "outline_coverage_audit", "v1", audit_path, "v2_pipeline",
            depends_on=[dependency_records["final_outline"]],
        )

        # Independent health sidecar. Existing Outline artifacts remain at their
        # original schema versions; adoption consumes this registered dependency.
        health_path = os.path.join(
            artifacts_dir, f"{self.project_name}_outline_stage_health_v1.json"
        )
        self._write_json(health_path, result.stage_health.to_dict())
        paths["outline_stage_health"] = health_path
        dependency_records["outline_stage_health"] = self._register(
            "outline_stage_health",
            "outline_stage_health",
            "v1",
            health_path,
            "v2_pipeline",
            depends_on=[
                dependency_records["literature_map"],
                dependency_records["synthesis_flow"],
                dependency_records["outline_candidates"],
                dependency_records["outline_critiques"],
                dependency_records["outline_arbitration_report"],
                dependency_records["final_outline"],
                dependency_records["outline_coverage_audit"],
            ],
        )

        return paths

    def adopt(self, result: V2PipelineResult, adopted_by: str) -> Tuple[Optional[AdoptedFinalOutline], str, str]:
        """Attempt to adopt the final outline."""
        if not self.registry:
            return None, "", "Adoption requires an Artifact Registry for immutable audit"
        if not result.final_outline or not result.coverage_audit or not result.stage_health:
            return None, "", "Missing final outline, coverage audit, or stage health"

        adopted, msg = adopt_final_outline(
            result.final_outline, result.coverage_audit,
            self.job_id, adopted_by, result.stage_health,
        )

        if adopted is None:
            return None, "", msg

        artifacts_dir = self._artifacts_dir()
        dependencies = []
        final_path = os.path.join(artifacts_dir, f"{self.project_name}_final_outline.json")
        audit_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_coverage_audit.json")
        health_path = os.path.join(artifacts_dir, f"{self.project_name}_outline_stage_health_v1.json")
        for artifact_id, artifact_type, dep_path in (
            ("final_outline", "final_outline", final_path),
            ("outline_coverage_audit", "outline_coverage_audit", audit_path),
            ("outline_stage_health", "outline_stage_health", health_path),
        ):
            record = self.registry.get(artifact_id)
            if (
                record is None
                or record.status != "ready"
                or not os.path.isfile(dep_path)
                or os.path.abspath(record.path) != os.path.abspath(dep_path)
                or record.content_hash != self._dependency_ref(artifact_type, dep_path).content_hash
            ):
                return None, "", f"Missing or stale registered adoption dependency: {artifact_id}"
            dependencies.append(
                ArtifactDependencyRef(
                    artifact_type=artifact_type,
                    path=record.path,
                    content_hash=record.content_hash,
                    dependency_kind="local_job",
                    job_id=record.job_id,
                    artifact_id=record.artifact_id,
                )
            )
        adopted_path = os.path.join(artifacts_dir, f"{self.project_name}_adopted_final_outline.json")
        write_adopted_outline(adopted, adopted_path)
        adopted_ref = self._register(
            "adopted_final_outline", "adopted_final_outline", "v1", adopted_path, "v2_adoption",
            depends_on=dependencies,
        )

        from services.audit_record import AuditArtifactRefV1, AuditRecordV1
        from services.job_workspace import atomic_write_json

        input_refs = [
            AuditArtifactRefV1(
                artifact_id=dependency.artifact_id,
                artifact_type=dependency.artifact_type,
                job_id=dependency.job_id or self.job_id,
                content_hash=dependency.content_hash,
            )
            for dependency in dependencies
        ]
        adoption_audit = AuditRecordV1.create(
            audit_type="outline_manual_adoption",
            job_id=self.job_id,
            attempt_id=f"outline-adoption:{self.job_id}",
            producer="outline.pipeline.V2Pipeline.adopt",
            actor=adopted_by,
            reason="explicit Outline v2 pipeline adoption",
            scope={
                "operation": "explicit_adoption",
                "execution_mode": result.stage_health.execution_mode,
            },
            target_artifacts=input_refs,
            input_artifact_refs=input_refs,
            output_artifact_refs=[
                AuditArtifactRefV1(
                    artifact_id="adopted_final_outline",
                    artifact_type="adopted_final_outline",
                    job_id=self.job_id,
                    content_hash=adopted_ref.content_hash,
                )
            ],
            input_hashes={
                "final_outline": dependencies[0].content_hash,
                "coverage_audit": dependencies[1].content_hash,
                "stage_health": dependencies[2].content_hash,
            },
            policy_snapshot={
                "require_stage_health": True,
                "production_fallback_adoptable": False,
            },
            disposition="adopted",
        )
        audit_record_path = os.path.join(
            artifacts_dir, f"outline_manual_adoption_{adoption_audit.audit_id}.json"
        )
        atomic_write_json(audit_record_path, adoption_audit.to_dict())
        self._register(
            adoption_audit.audit_id,
            "audit_record",
            "v1",
            audit_record_path,
            "v2_adoption",
            depends_on=[*dependencies, adopted_ref],
        )

        return adopted, adopted_path, msg

    def _artifacts_dir(self) -> str:
        if self.workspace:
            return self.workspace.paths.artifacts_dir
        return os.path.join(self.output_dir, "artifacts")

    @staticmethod
    def _write_json(path: str, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _register(
        self,
        artifact_id: str,
        artifact_type: str,
        artifact_version: str,
        path: str,
        producer: str,
        depends_on: List[ArtifactDependencyRef] | None = None,
    ) -> ArtifactDependencyRef:
        dependency_ref = self._dependency_ref(artifact_type, path)
        if not self.registry:
            return dependency_ref
        try:
            record = self.registry.register_file(
                artifact_id=artifact_id,
                artifact_role=artifact_type,
                artifact_type=artifact_type,
                artifact_version=artifact_version,
                path=path,
                producer=producer,
                status="ready",
                depends_on=depends_on or [],
            )
            return ArtifactDependencyRef(
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
                dependency_kind="local_job",
                job_id=record.job_id,
                artifact_id=record.artifact_id,
            )
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"Failed to register v2 artifact {artifact_id}: {exc}")
            raise

    @staticmethod
    def _dependency_ref(artifact_type: str, path: str) -> ArtifactDependencyRef:
        try:
            from services.artifact_registry import file_sha256
            content_hash = file_sha256(path) if path and os.path.exists(path) else ""
        except Exception:
            content_hash = ""
        return ArtifactDependencyRef(
            artifact_type=artifact_type,
            path=os.path.abspath(path) if path else "",
            content_hash=content_hash,
        )

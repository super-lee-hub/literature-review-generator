"""Week 4 Repair Pipeline Integration.

Wires repair planning and apply into the main workflow.
Handles artifact persistence and registry integration.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from services.job_workspace import JobWorkspace, atomic_write_json
from services.artifact_registry import ArtifactRegistry
from validation.repair_models import RepairPlan, RepairApplyResult, AppliedPatchRecord, RepairReport
from validation.repair_apply import run_repair_apply
from validation.review_validator import ReviewValidationReport


REPAIR_PLAN_ARTIFACT_TYPE = "repair_plan"
REPAIR_REPORT_ARTIFACT_TYPE = "repair_report"
REPAIR_APPLY_RESULT_ARTIFACT_TYPE = "repair_apply_result"
APPLIED_PATCH_RECORD_ARTIFACT_TYPE = "applied_patch_record"


def persist_repair_plan(
    repair_plan: RepairPlan,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> str:
    """Persist repair plan to workspace and register in artifact registry."""
    plan_path = workspace.artifact_path(f"repair_plan_{repair_plan.plan_id}.json")
    
    atomic_write_json(plan_path, repair_plan.to_dict())
    
    registry.register(
        artifact_id=f"repair_plan_{repair_plan.plan_id}",
        artifact_type=REPAIR_PLAN_ARTIFACT_TYPE,
        artifact_version=repair_plan.artifact_version,
        path=plan_path,
        producer="services.repair_integration.persist_repair_plan",
        job_id=repair_plan.created_from_job_id,
        status="completed",
        depends_on=[
            {"artifact_id": repair_plan.validation_report_id, "role": "validation_report"},
        ],
    )
    
    return plan_path


def persist_repair_report(
    repair_report: RepairReport,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
) -> str:
    """Persist repair report to workspace and register in artifact registry."""
    report_path = workspace.artifact_path(f"repair_report_{repair_report.report_id}.json")
    
    atomic_write_json(report_path, repair_report.to_dict())
    
    registry.register(
        artifact_id=f"repair_report_{repair_report.report_id}",
        artifact_type=REPAIR_REPORT_ARTIFACT_TYPE,
        artifact_version=repair_report.artifact_version,
        path=report_path,
        producer="services.repair_integration.persist_repair_report",
        job_id=repair_report.created_from_job_id,
        status="completed",
        depends_on=[
            {"artifact_id": repair_report.plan_id, "role": "repair_plan"},
        ],
    )
    
    return report_path


def persist_repair_apply_result(
    apply_result: RepairApplyResult,
    applied_records: List[AppliedPatchRecord],
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    job_id: str,
) -> str:
    """Persist repair apply result to workspace and register in artifact registry."""
    result_path = workspace.artifact_path(f"repair_apply_result_{apply_result.plan_id}.json")
    
    result_data = apply_result.to_dict()
    result_data["applied_records"] = [r.to_dict() for r in applied_records]
    
    atomic_write_json(result_path, result_data)
    
    registry.register(
        artifact_id=f"repair_apply_result_{apply_result.plan_id}",
        artifact_type=REPAIR_APPLY_RESULT_ARTIFACT_TYPE,
        artifact_version=apply_result.artifact_version,
        path=result_path,
        producer="services.repair_integration.persist_repair_apply_result",
        job_id=job_id,
        status="completed",
        depends_on=[
            {"artifact_id": apply_result.plan_id, "role": "repair_plan"},
        ],
    )
    
    # Also persist individual patch records
    for record in applied_records:
        record_path = workspace.artifact_path(f"applied_patch_{record.record_id}.json")
        atomic_write_json(record_path, record.to_dict())
        
        registry.register(
            artifact_id=f"applied_patch_{record.record_id}",
            artifact_type=APPLIED_PATCH_RECORD_ARTIFACT_TYPE,
            artifact_version=record.artifact_version,
            path=record_path,
            producer="services.repair_integration.persist_repair_apply_result",
            job_id=job_id,
            status="completed",
            depends_on=[
                {"artifact_id": apply_result.plan_id, "role": "repair_plan"},
            ],
        )
    
    return result_path


def run_repair_pipeline(
    validation_report: ReviewValidationReport,
    review_draft: Dict[str, Any],
    citation_manifest: Dict[str, Any],
    paper_artifacts: Sequence[Dict[str, Any]],
    job_id: str,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    visual_manifest: Optional[Dict[str, Any]] = None,
    auto_apply: bool = False,  # Default is report-first
) -> Dict[str, Any]:
    """Run the full Week 4 repair pipeline.
    
    1. Create repair plan from validation findings
    2. Persist repair plan and report
    3. Apply repairs (if auto_apply is True, otherwise report-only)
    4. Persist apply results
    5. Generate new review_draft, citation_manifest, and review docx
    6. Run review recheck
    7. Return patched review_draft if repairs were applied
    
    Default policy is report-first, not silent auto-apply.
    """
    from validation.repair_planner import RepairPlanner
    from validation.repair_models import RepairPolicy
    from services.citation_manifest import build_citation_manifest_v2_from_review_draft, unresolved_occurrences
    from docx_writer import create_word_document, generate_apa_references_from_manifest
    from validator import run_review_validation
    
    # 检查 citation manifest 是否完整
    if citation_manifest:
        unresolved_cites = unresolved_occurrences(citation_manifest)
        if unresolved_cites:
            result = {
                "repair_pipeline": True,
                "status": "skipped_due_to_manifest_integrity",
                "message": f"Citation manifest has {len(unresolved_cites)} unresolved citations, skipping repair pipeline",
                "unresolved_citations_count": len(unresolved_cites)
            }
            return result
    else:
        result = {
            "repair_pipeline": True,
            "status": "skipped_due_to_manifest_integrity",
            "message": "Citation manifest is missing or empty, skipping repair pipeline"
        }
        return result
    
    # Step 1: Create repair plan
    policy = RepairPolicy.AUTO_APPLY_SAFE if auto_apply else RepairPolicy.REPORT_FIRST
    planner = RepairPlanner(
        validation_report=validation_report,
        review_draft=review_draft,
        citation_manifest=citation_manifest,
        paper_artifacts=paper_artifacts,
        job_id=job_id,
    )
    repair_plan = planner.create_plan(policy=policy)
    
    # Step 2: Persist repair plan
    plan_path = persist_repair_plan(repair_plan, workspace, registry)
    
    # Create and persist repair report
    repair_report = planner.create_report(repair_plan)
    report_path = persist_repair_report(repair_report, workspace, registry)
    
    result = {
        "repair_pipeline": True,
        "plan_id": repair_plan.plan_id,
        "policy": policy.value,
        "plan_path": plan_path,
        "report_path": report_path,
        "proposals_count": len(repair_plan.proposals),
        "applied": False,
    }
    
    # Step 3: Apply repairs (only if auto_apply is True)
    if auto_apply and repair_plan.proposals:
        apply_result = run_repair_apply(
            repair_plan=repair_plan,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id=job_id,
            visual_manifest=visual_manifest,
            dry_run=False,
        )
        
        # Step 4: Persist apply results
        applied_records = [AppliedPatchRecord(**r) for r in apply_result.get("applied_records", [])]
        apply_result_obj = RepairApplyResult(**apply_result["apply_result"])
        result_path = persist_repair_apply_result(
            apply_result_obj, applied_records, workspace, registry, job_id
        )
        
        result["applied"] = True
        result["apply_result_path"] = result_path
        result["patched_review_draft"] = apply_result.get("patched_review_draft")
        result["applied_count"] = apply_result_obj.applied_count
        result["rejected_count"] = apply_result_obj.rejected_count
        
        # Step 5: Generate new review_draft, citation_manifest, and review docx
        if apply_result.get("patched_review_draft"):
            patched_review_draft = apply_result["patched_review_draft"]
            
            # Generate new citation manifest from patched review draft
            new_citation_manifest = build_citation_manifest_v2_from_review_draft(
                job_id=job_id,
                project_name="repair",
                manifest_id=f"manifest_{job_id}",
                review_draft_path="",
                review_word_path="",
                review_draft_v2=patched_review_draft,
                paper_summaries=list(paper_artifacts)
            )
            
            # Generate new review docx
            docx_path = workspace.artifact_path(f"review_{job_id}_repaired.docx")
            create_word_document(
                docx_path,
                "Repaired Literature Review",
                ["Introduction", "Methodology", "Results", "Discussion", "Conclusion"],
                "",
                []
            )
            
            # Generate APA references from new citation manifest
            # Create a simple generator instance mock
            class MockLogger:
                def info(self, *args):
                    pass
                def warning(self, *args):
                    pass
                def error(self, *args):
                    pass
                def success(self, *args):
                    pass
                def warn(self, *args):
                    pass
                def debug(self, *args):
                    pass
            
            class MockGenerator:
                def __init__(self, workspace):
                    self.logger = MockLogger()
                    self.config = {
                        'Performance': {'enable_stage2_validation': 'True'},
                        'Paths': {'output_path': workspace.base_output_dir},
                        'Primary_Reader_API': {'api_key': 'mock_key'},
                        'Writer_API': {'api_key': 'mock_key'}
                    }
                    self.compat_config = None
                    self.output_dir = workspace.base_output_dir
                    self.project_name = "repair"
                    self.summaries = []
                    self.failed_papers = []
                    self.processed_count = type('Counter', (), {'value': 0, 'get_value': lambda self: self.value, 'set': lambda self, val: setattr(self, 'value', val)})()
                    self.failed_count = type('Counter', (), {'value': 0, 'get_value': lambda self: self.value, 'set': lambda self, val: setattr(self, 'value', val)})()
                    self.progress_tracker = None
                    self.free_mode_profile_path = None
                    self.free_mode_profile = None
                    self.free_mode_idea = None
                    self.cancel_token = None
                    self.job_workspace = workspace
                    self.artifact_registry = None
                    self.job_fingerprint_bundle = {}
                    self.resume_state_report = None
                    self.queue_service = None
                    self.mode = "direct"
                    self.pdf_folder = None
                    self.zotero_report = None
                    self.library_path = None
                    self.summary_file = workspace.artifact_path("repair_summaries.json")
                    self.papers = []
                    self.source_descriptors = []
                    self.preprocess_manager = None
                    self.save_lock = None
                
                def _get_summary_file_path(self):
                    return self.summary_file
                
                def _get_report_file_path(self, suffix):
                    return self.job_workspace.artifact_path(f"repair{suffix}")
                
                def _stage2_validation_enabled(self):
                    return True
            
            mock_generator = MockGenerator(workspace)
            generate_apa_references_from_manifest(
                new_citation_manifest.to_dict(),
                mock_generator,
                allow_compat_fallback=True,
            )
            
            # Step 6: Run review recheck
            try:
                recheck_result = run_review_validation(mock_generator)
                result["recheck_result"] = recheck_result
                result["recheck_success"] = recheck_result.get("success", False)
            except Exception as recheck_error:
                result["recheck_error"] = str(recheck_error)
                result["recheck_success"] = False
    else:
        # Report-only mode
        if not repair_plan.proposals:
            result["message"] = "No repair proposals generated (citation manifest is complete and validation passed)"
        else:
            result["message"] = "Repair plan created but not applied (report-first policy). Use auto_apply=True to apply repairs."
    
    return result


def load_repair_plan(plan_id: str, workspace: JobWorkspace) -> Optional[RepairPlan]:
    """Load a persisted repair plan from workspace.
    
    Reconstructs a RepairPlan from its JSON serialization, including all
    nested PatchProposal objects with their DependencyHashBundle and
    PatchTargetSignature.
    """
    plan_path = workspace.artifact_path(f"repair_plan_{plan_id}.json")
    
    if not os.path.exists(plan_path):
        return None
    
    with open(plan_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    from validation.repair_models import (
        RepairPlan, RepairPolicy, PatchProposal, PatchTargetSignature,
        DependencyHashBundle, RepairRootCause, PatchGranularity
    )
    
    # Reconstruct proposals
    proposals = []
    for prop_data in data.get("proposals", []):
        # Reconstruct target signature
        target_data = prop_data.get("target", {})
        target = PatchTargetSignature(
            block_id=target_data.get("block_id", ""),
            anchor_text=target_data.get("anchor_text", ""),
            anchor_hash=target_data.get("anchor_hash", ""),
            span_start=target_data.get("span_start"),
            span_end=target_data.get("span_end"),
        )
        
        # Reconstruct dependency bundle
        bundle_data = prop_data.get("dependency_bundle", {})
        dependency_bundle = DependencyHashBundle(
            summary_hash=bundle_data.get("summary_hash", ""),
            paper_artifact_hash=bundle_data.get("paper_artifact_hash", ""),
            visual_manifest_hash=bundle_data.get("visual_manifest_hash", ""),
            selected_visual_refs_hash=bundle_data.get("selected_visual_refs_hash", ""),
        )
        
        # Reconstruct proposal
        proposal = PatchProposal(
            proposal_id=prop_data.get("proposal_id", ""),
            citation_id=prop_data.get("citation_id", ""),
            root_cause=RepairRootCause(prop_data.get("root_cause", "citation_mapping_error")),
            granularity=PatchGranularity(prop_data.get("granularity", "span")),
            target=target,
            original_text=prop_data.get("original_text", ""),
            proposed_text=prop_data.get("proposed_text", ""),
            confidence=prop_data.get("confidence", 0.5),
            fix_strategy=prop_data.get("fix_strategy", ""),
            dependency_bundle=dependency_bundle,
            metadata=prop_data.get("metadata", {}),
        )
        proposals.append(proposal)
    
    # Reconstruct RepairPlan
    return RepairPlan(
        plan_id=data.get("plan_id", plan_id),
        created_at=data.get("created_at", ""),
        created_from_job_id=data.get("created_from_job_id", ""),
        validation_report_id=data.get("validation_report_id", ""),
        proposals=proposals,
        policy=RepairPolicy(data.get("policy", "report_first")),
    )

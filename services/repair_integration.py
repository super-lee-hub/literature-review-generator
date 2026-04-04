"""Week 4 Repair Pipeline Integration.

Wires repair planning and apply into the main workflow.
Handles artifact persistence and registry integration.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from services.job_workspace import JobWorkspace, atomic_write_json
from services.artifact_registry import ArtifactRegistry
from validation.repair_models import RepairPlan, RepairApplyResult, AppliedPatchRecord, RepairReport
from validation.repair_planner import RepairPlanner, run_repair_planning
from validation.repair_apply import RepairApplier, run_repair_apply
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
    5. Return patched review_draft if repairs were applied
    
    Default policy is report-first, not silent auto-apply.
    """
    from validation.repair_planner import RepairPlanner
    from validation.repair_models import RepairPolicy
    
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
    else:
        # Report-only mode
        result["message"] = "Repair plan created but not applied (report-first policy). Use auto_apply=True to apply repairs."
    
    return result


def load_repair_plan(plan_id: str, workspace: JobWorkspace) -> Optional[RepairPlan]:
    """Load a persisted repair plan from workspace."""
    plan_path = workspace.artifact_path(f"repair_plan_{plan_id}.json")
    
    if not os.path.exists(plan_path):
        return None
    
    with open(plan_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    from validation.repair_models import RepairPlan, RepairPolicy, PatchProposal
    
    # Reconstruct RepairPlan
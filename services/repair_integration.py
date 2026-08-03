"""Week 4 Repair Pipeline Integration.

Wires repair planning and apply into the main workflow.
Handles artifact persistence and registry integration.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from services.job_workspace import JobWorkspace, atomic_write_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.repair_policy import (
    ValidationRepairPolicy,
    auto_safe_apply_enabled,
    is_auto_safe_proposal,
    parse_repair_policy,
    repair_policy_to_week4_policy,
    requires_manual_confirmation,
    unsafe_auto_rewrite_enabled,
)
from validation.repair_models import RepairPlan, RepairApplyResult, AppliedPatchRecord, RepairReport
from validation.repair_apply import run_repair_apply
from validation.review_validator import ReviewValidationReport


REPAIR_PLAN_ARTIFACT_TYPE = "repair_plan"
REPAIR_REPORT_ARTIFACT_TYPE = "repair_report"
REPAIR_APPLY_RESULT_ARTIFACT_TYPE = "repair_apply_result"
APPLIED_PATCH_RECORD_ARTIFACT_TYPE = "applied_patch_record"
CITATION_MANIFEST_ARTIFACT_TYPE = "citation_manifest"


def _registered_dependency(
    registry: ArtifactRegistry,
    artifact_id: str,
    *,
    require_ready: bool = True,
) -> ArtifactDependencyRefV2:
    record = registry.get(str(artifact_id))
    if record is None or (require_ready and record.status != "ready"):
        expected = "ready current" if require_ready else "registered current"
        raise ValueError(f"repair dependency is not a {expected} artifact: {artifact_id}")
    return ArtifactDependencyRefV2.from_record(record)


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
        status="ready",
        depends_on=[_registered_dependency(registry, repair_plan.validation_report_id)],
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
        status="quarantined",
        depends_on=[_registered_dependency(registry, f"repair_plan_{repair_report.plan_id}")],
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
        status="quarantined",
        depends_on=[_registered_dependency(registry, f"repair_plan_{apply_result.plan_id}")],
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
            status="quarantined",
            depends_on=[_registered_dependency(registry, f"repair_plan_{apply_result.plan_id}")],
        )
    
    return result_path


def persist_patched_citation_manifest(
    citation_manifest: Dict[str, Any],
    apply_result: RepairApplyResult,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    job_id: str,
) -> str:
    """Persist the manifest mutated by auto-safe structural repair."""
    artifact_version = str(citation_manifest.get("artifact_version") or "v3")
    manifest_path = workspace.artifact_path(f"citation_manifests/repaired_citation_manifest_{job_id}.json")

    atomic_write_json(manifest_path, citation_manifest)

    registry.register(
        artifact_id=f"repaired_citation_manifest_{job_id}",
        artifact_type=CITATION_MANIFEST_ARTIFACT_TYPE,
        artifact_version=artifact_version,
        path=manifest_path,
        producer="services.repair_integration.persist_patched_citation_manifest",
        job_id=job_id,
        status="quarantined",
        depends_on=[
            _registered_dependency(
                registry,
                f"repair_apply_result_{apply_result.plan_id}",
                require_ready=False,
            )
        ],
        artifact_role="citation_manifest",
    )

    return manifest_path


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
    repair_policy: str | ValidationRepairPolicy | None = None,
    validation_service: Any | None = None,
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
    from dataclasses import replace
    from validation.repair_planner import RepairPlanner
    from services.citation_manifest import unresolved_occurrences
    
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
    external_policy = (
        parse_repair_policy(repair_policy)
        if repair_policy is not None
        else (ValidationRepairPolicy.AUTO_SAFE if auto_apply else ValidationRepairPolicy.REPORT_ONLY)
    )
    policy = repair_policy_to_week4_policy(external_policy)
    planner = RepairPlanner(
        validation_report=validation_report,
        review_draft=review_draft,
        citation_manifest=citation_manifest,
        paper_artifacts=paper_artifacts,
        job_id=job_id,
    )
    repair_plan = planner.create_plan(policy=policy)
    safe_proposals = [proposal for proposal in repair_plan.proposals if is_auto_safe_proposal(proposal)]
    
    # Step 2: Persist repair plan
    plan_path = persist_repair_plan(repair_plan, workspace, registry)
    
    # Create and persist repair report
    repair_report = planner.create_report(repair_plan)
    repair_report.summary.update({
        "external_repair_policy": external_policy.value,
        "requires_manual_confirmation": requires_manual_confirmation(external_policy),
        "eligible_for_manual_apply": requires_manual_confirmation(external_policy),
        "unsafe_auto_rewrite_enabled": unsafe_auto_rewrite_enabled(external_policy),
        "auto_safe_eligible_count": len(safe_proposals),
    })
    report_path = persist_repair_report(repair_report, workspace, registry)
    
    result = {
        "repair_pipeline": True,
        "plan_id": repair_plan.plan_id,
        "policy": policy.value,
        "repair_policy": external_policy.value,
        "requires_manual_confirmation": requires_manual_confirmation(external_policy),
        "eligible_for_manual_apply": requires_manual_confirmation(external_policy),
        "unsafe_auto_rewrite_enabled": unsafe_auto_rewrite_enabled(external_policy),
        "plan_path": plan_path,
        "report_path": report_path,
        "proposals_count": len(repair_plan.proposals),
        "auto_safe_eligible_count": len(safe_proposals),
        "applied": False,
    }
    
    # Step 3: Apply repairs (only if explicitly allowed by policy)
    if auto_safe_apply_enabled(external_policy) and safe_proposals:
        apply_plan = replace(repair_plan, proposals=safe_proposals)
        apply_result = run_repair_apply(
            repair_plan=apply_plan,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id=job_id,
            visual_manifest=visual_manifest,
            dry_run=False,
            require_auto_safe=True,
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
        result["patched_citation_manifest"] = apply_result.get("patched_citation_manifest")
        result["applied_count"] = apply_result_obj.applied_count
        result["rejected_count"] = apply_result_obj.rejected_count
        
        # Step 5: Persist repaired manifest and regenerate downstream artifacts.
        if apply_result.get("patched_review_draft"):
            patched_citation_manifest = apply_result.get("patched_citation_manifest") or citation_manifest
            manifest_path = persist_patched_citation_manifest(
                patched_citation_manifest,
                apply_result_obj,
                workspace,
                registry,
                job_id,
            )
            result["patched_citation_manifest_path"] = manifest_path
            
            # A repair result is never promoted on the strength of a placeholder
            # DOCX or a legacy validator adapter.  Register both repaired inputs
            # as quarantined artifacts, rebuild the DOCX through the current
            # service, and run the current validator against those exact files.
            if validation_service is None:
                result.update(
                    {
                        "recheck_status": "blocked",
                        "recheck_success": False,
                        "recheck_error": "current validation service is required for repair revalidation",
                    }
                )
            else:
                docx_path = workspace.artifact_path(f"review_{job_id}_repaired.docx")
                try:
                    patched_draft = dict(apply_result.get("patched_review_draft") or review_draft)
                    patched_draft_path = workspace.artifact_path(
                        f"review_drafts/repaired_review_draft_{job_id}.json"
                    )
                    atomic_write_json(patched_draft_path, patched_draft)
                    draft_record = registry.register(
                        artifact_id=f"repaired_review_draft_{job_id}",
                        artifact_type="review_draft_repaired",
                        artifact_version="v1",
                        path=patched_draft_path,
                        producer="services.repair_integration.run_repair_pipeline",
                        job_id=job_id,
                        status="quarantined",
                        depends_on=[
                            _registered_dependency(
                                registry,
                                f"repair_apply_result_{apply_result_obj.plan_id}",
                                require_ready=False,
                            )
                        ],
                        artifact_role="review_draft_repaired",
                    )
                    validation_service.rebuild_review_docx(
                        patched_draft,
                        dict(patched_citation_manifest),
                        docx_path,
                    )
                    manifest_record = registry.get(f"repaired_citation_manifest_{job_id}")
                    if manifest_record is None:
                        raise ValueError("repaired citation manifest was not registered")
                    docx_record = registry.register(
                        artifact_id=f"repaired_review_docx_{job_id}",
                        artifact_type="review_docx_repaired",
                        artifact_version="v1",
                        path=docx_path,
                        producer="services.repair_integration.run_repair_pipeline",
                        job_id=job_id,
                        status="quarantined",
                        depends_on=[
                            ArtifactDependencyRefV2.from_record(draft_record),
                            ArtifactDependencyRefV2.from_record(manifest_record),
                        ],
                        artifact_role="review_docx_repaired",
                    )
                    revalidation = validation_service.revalidate_review_artifacts(
                        review_draft_record=draft_record,
                        citation_manifest_record=manifest_record,
                        output_dir=workspace.artifact_path(
                            f"repair_revalidation/{apply_result_obj.plan_id}"
                        ),
                        result_artifact_id=f"validation_run_result_repaired:{apply_result_obj.plan_id}",
                    )
                    revalidation_model = revalidation.get("validation_run_result")
                    recheck_success = bool(
                        getattr(revalidation_model, "contract_satisfied", False)
                        and str(revalidation.get("validation_disposition") or "") == "clean"
                        and bool(
                            (revalidation.get("provider_receipt_closure") or {}).get("complete")
                        )
                    )
                    result.update(
                        {
                            "recheck_status": "passed" if recheck_success else "failed",
                            "recheck_success": recheck_success,
                            "recheck_error": "" if recheck_success else "current-service semantic revalidation did not produce a clean closed result",
                            "repaired_docx_path": docx_path,
                            "repaired_artifact_ids": [
                                draft_record.artifact_id,
                                manifest_record.artifact_id,
                                docx_record.artifact_id,
                                str(revalidation.get("provider_receipt_closure_record_id") or ""),
                            ],
                            "revalidation_artifact_id": str(
                                revalidation.get("provider_receipt_closure_record_id") or ""
                            ),
                        }
                    )
                except (OSError, TypeError, ValueError, RuntimeError) as recheck_error:
                    result.update(
                        {
                            "recheck_status": "blocked",
                            "recheck_success": False,
                            "recheck_error": str(recheck_error),
                        }
                    )
    else:
        # Report-only mode
        if not repair_plan.proposals:
            result["message"] = "No repair proposals generated (citation manifest is complete and validation passed)"
        elif auto_safe_apply_enabled(external_policy):
            result["message"] = "Repair plan created but no proposals were eligible for auto_safe structural apply."
        else:
            result["message"] = f"Repair plan created but not applied ({external_policy.value} policy)."
    
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

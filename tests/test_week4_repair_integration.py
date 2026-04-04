"""Tests for Week 4 Repair Integration.

Tests repair integration with ArtifactRegistry, persistence, and workflow wiring.
"""

import json
import os
import tempfile
import pytest
from datetime import datetime

from services.job_workspace import JobWorkspace
from services.artifact_registry import ArtifactRegistry
from services.repair_integration import (
    persist_repair_plan,
    persist_repair_report,
    persist_repair_apply_result,
    load_repair_plan,
    run_repair_pipeline,
    REPAIR_PLAN_ARTIFACT_TYPE,
    REPAIR_REPORT_ARTIFACT_TYPE,
    REPAIR_APPLY_RESULT_ARTIFACT_TYPE,
    APPLIED_PATCH_RECORD_ARTIFACT_TYPE,
)
from validation.repair_models import (
    AppliedPatchRecord,
    DependencyHashBundle,
    PatchGranularity,
    PatchProposal,
    PatchTargetSignature,
    RepairApplyResult,
    RepairPlan,
    RepairPolicy,
    RepairReport,
    RepairRootCause,
)
from validation.review_validator import (
    CitationValidationResult,
    ReviewValidationReport,
    RootCause,
    ValidationConclusion,
)


class TestArtifactRegistryRegister:
    """Test ArtifactRegistry.register() method used by repair_integration."""
    
    def test_register_method_exists(self):
        """Test that register() method exists and is callable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = os.path.join(tmpdir, "registry.json")
            registry = ArtifactRegistry(registry_path, "job-001")
            
            # Create a dummy file to register
            test_file = os.path.join(tmpdir, "test.json")
            with open(test_file, "w") as f:
                json.dump({"test": "data"}, f)
            
            # Should not raise AttributeError
            record = registry.register(
                artifact_id="test-artifact-001",
                artifact_type="test_type",
                artifact_version="v1",
                path=test_file,
                producer="test",
                job_id="job-001",
                status="completed",
                depends_on=[],
            )
            
            assert record is not None
            assert record.artifact_id == "test-artifact-001"
            assert record.artifact_type == "test_type"
    
    def test_register_with_dependencies(self):
        """Test register() with dependency tracking."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry_path = os.path.join(tmpdir, "registry.json")
            registry = ArtifactRegistry(registry_path, "job-001")
            
            # Create dependency artifact first
            dep_file = os.path.join(tmpdir, "dep.json")
            with open(dep_file, "w") as f:
                json.dump({"dep": "data"}, f)
            
            dep_record = registry.register(
                artifact_id="dep-001",
                artifact_type="dependency",
                artifact_version="v1",
                path=dep_file,
                producer="test",
                job_id="job-001",
            )
            
            # Create main artifact with dependency
            main_file = os.path.join(tmpdir, "main.json")
            with open(main_file, "w") as f:
                json.dump({"main": "data"}, f)
            
            main_record = registry.register(
                artifact_id="main-001",
                artifact_type="main",
                artifact_version="v1",
                path=main_file,
                producer="test",
                job_id="job-001",
                depends_on=[{"artifact_id": "dep-001", "role": "dependency"}],
            )
            
            assert len(main_record.depends_on) == 1
            assert main_record.depends_on[0].artifact_type == "dependency"


class TestPersistRepairPlan:
    """Test persist_repair_plan function."""
    
    def test_persist_and_register_repair_plan(self):
        """Test that repair plan is persisted and registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            plan = RepairPlan(
                plan_id="plan-001",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                validation_report_id="val-001",
                proposals=[],
                policy=RepairPolicy.REPORT_FIRST,
            )
            
            plan_path = persist_repair_plan(plan, workspace, registry)
            
            # Check file exists
            assert os.path.exists(plan_path)
            
            # Check registry has the artifact
            record = registry.get("repair_plan_plan-001")
            assert record is not None
            assert record.artifact_type == REPAIR_PLAN_ARTIFACT_TYPE
            assert record.artifact_version == "v1"
    
    def test_persist_repair_plan_with_proposals(self):
        """Test persisting repair plan with patch proposals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            bundle = DependencyHashBundle(
                summary_hash="abc123",
                paper_artifact_hash="def456",
                visual_manifest_hash="ghi789",
                selected_visual_refs_hash="jkl012",
            )
            target = PatchTargetSignature(
                block_id="s1_b1",
                anchor_text="Test text...",
                anchor_hash="a1b2c3d4",
            )
            proposal = PatchProposal(
                proposal_id="prop-001",
                citation_id="cite-001",
                root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
                granularity=PatchGranularity.SPAN,
                target=target,
                original_text="Original",
                proposed_text="Proposed",
                confidence=0.8,
                fix_strategy="manifest_fix",
                dependency_bundle=bundle,
            )
            
            plan = RepairPlan(
                plan_id="plan-002",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                validation_report_id="val-001",
                proposals=[proposal],
                policy=RepairPolicy.REPORT_FIRST,
            )
            
            plan_path = persist_repair_plan(plan, workspace, registry)
            
            # Load and verify
            with open(plan_path, "r") as f:
                data = json.load(f)
            
            assert data["plan_id"] == "plan-002"
            assert len(data["proposals"]) == 1
            assert data["proposals"][0]["proposal_id"] == "prop-001"


class TestPersistRepairReport:
    """Test persist_repair_report function."""
    
    def test_persist_repair_report(self):
        """Test that repair report is persisted and registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            report = RepairReport(
                report_id="report-001",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                plan_id="plan-001",
                apply_result_id=None,
                summary={"total_proposals": 5},
                proposals_detail=[],
            )
            
            report_path = persist_repair_report(report, workspace, registry)
            
            # Check file exists
            assert os.path.exists(report_path)
            
            # Check registry has the artifact
            record = registry.get("repair_report_report-001")
            assert record is not None
            assert record.artifact_type == REPAIR_REPORT_ARTIFACT_TYPE


class TestPersistRepairApplyResult:
    """Test persist_repair_apply_result function."""
    
    def test_persist_apply_result_with_records(self):
        """Test that apply result and patch records are persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            apply_result = RepairApplyResult(
                success=True,
                plan_id="plan-001",
                applied_count=2,
                rejected_count=1,
                applied_proposals=["prop-001", "prop-002"],
                rejected_proposals=[{"proposal_id": "prop-003", "reason": "guard_failed"}],
            )
            
            applied_records = [
                AppliedPatchRecord(
                    record_id="rec-001",
                    proposal_id="prop-001",
                    plan_id="plan-001",
                    applied_at=datetime.now().isoformat(),
                    applied_in_job_id="job-001",
                    original_text="Original text",
                    applied_text="Patched text",
                    target_block_id="s1_b1",
                    anchor_hash_before="hash1",
                    anchor_hash_after="hash2",
                ),
            ]
            
            result_path = persist_repair_apply_result(
                apply_result, applied_records, workspace, registry, "job-001"
            )
            
            # Check file exists
            assert os.path.exists(result_path)
            
            # Check registry has the artifacts
            result_record = registry.get("repair_apply_result_plan-001")
            assert result_record is not None
            assert result_record.artifact_type == REPAIR_APPLY_RESULT_ARTIFACT_TYPE
            
            patch_record = registry.get("applied_patch_rec-001")
            assert patch_record is not None
            assert patch_record.artifact_type == APPLIED_PATCH_RECORD_ARTIFACT_TYPE


class TestLoadRepairPlan:
    """Test load_repair_plan function."""
    
    def test_load_nonexistent_plan_returns_none(self):
        """Test loading a non-existent plan returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            
            result = load_repair_plan("nonexistent", workspace)
            assert result is None
    
    def test_load_repair_plan_round_trip(self):
        """Test that a persisted plan can be loaded back correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            # Create and persist a plan
            bundle = DependencyHashBundle(
                summary_hash="abc123",
                paper_artifact_hash="def456",
                visual_manifest_hash="ghi789",
                selected_visual_refs_hash="jkl012",
            )
            target = PatchTargetSignature(
                block_id="s1_b1",
                anchor_text="Test text...",
                anchor_hash="a1b2c3d4",
                span_start=10,
                span_end=50,
            )
            proposal = PatchProposal(
                proposal_id="prop-001",
                citation_id="cite-001",
                root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
                granularity=PatchGranularity.SPAN,
                target=target,
                original_text="Original text content",
                proposed_text="Proposed text content",
                confidence=0.85,
                fix_strategy="manifest_fix_rerender",
                dependency_bundle=bundle,
                metadata={"paper_id": "paper-001"},
            )
            
            original_plan = RepairPlan(
                plan_id="plan-roundtrip",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                validation_report_id="val-001",
                proposals=[proposal],
                policy=RepairPolicy.REPORT_FIRST,
            )
            
            persist_repair_plan(original_plan, workspace, registry)
            
            # Load the plan back
            loaded_plan = load_repair_plan("plan-roundtrip", workspace)
            
            assert loaded_plan is not None
            assert loaded_plan.plan_id == "plan-roundtrip"
            assert loaded_plan.policy == RepairPolicy.REPORT_FIRST
            assert len(loaded_plan.proposals) == 1
            
            loaded_proposal = loaded_plan.proposals[0]
            assert loaded_proposal.proposal_id == "prop-001"
            assert loaded_proposal.root_cause == RepairRootCause.CITATION_MAPPING_ERROR
            assert loaded_proposal.granularity == PatchGranularity.SPAN
            assert loaded_proposal.target.block_id == "s1_b1"
            assert loaded_proposal.target.span_start == 10
            assert loaded_proposal.target.span_end == 50
            assert loaded_proposal.dependency_bundle.summary_hash == "abc123"
            assert loaded_proposal.metadata.get("paper_id") == "paper-001"


class TestRepairPipelineIntegration:
    """Test run_repair_pipeline integration."""
    
    def test_repair_pipeline_report_first_policy(self):
        """Test that default policy is report-first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            # Create validation report with one non-supported citation
            citation_result = CitationValidationResult(
                citation_id="cite-001",
                paper_id="paper-001",
                conclusion=ValidationConclusion.WRONG_SOURCE,
                root_causes=[RootCause.CITATION_MAPPING_ERROR],
                evidence_candidates=[],
                details={"block_id": "s1_b1"},
            )
            validation_report = ReviewValidationReport(
                report_id="val-001",
                created_at=datetime.now().isoformat(),
                total_citations=1,
                supported_count=0,
                partial_support_count=0,
                unsupported_count=0,
                wrong_source_count=1,
                needs_review_count=0,
                citation_results=[citation_result],
            )
            
            review_draft = {
                "artifact_type": "review_draft",
                "artifact_version": "v1",
                "content": {
                    "sections": [
                        {
                            "section_number": 1,
                            "section_title": "Test",
                            "blocks": [
                                {
                                    "block_id": "s1_b1",
                                    "block_kind": "paragraph",
                                    "block_order": 1,
                                    "text": "Test paragraph with citation.",
                                    "anchor_text": "Test paragraph...",
                                    "anchor_hash": "abc123",
                                }
                            ],
                        }
                    ]
                },
            }
            citation_manifest = {"citations": [{"citation_id": "cite-001", "paper_id": "paper-001", "text": "citation"}]}
            paper_artifact = {
                "paper_identity": {"canonical_paper_key": "paper-001"},
                "analysis": {"ai_summary": {}},
                "stage1_inputs": {"selected_visual_refs": []},
            }
            
            result = run_repair_pipeline(
                validation_report=validation_report,
                review_draft=review_draft,
                citation_manifest=citation_manifest,
                paper_artifacts=[paper_artifact],
                job_id="job-001",
                workspace=workspace,
                registry=registry,
                auto_apply=False,  # Default: report-first
            )
            
            assert result["repair_pipeline"] is True
            assert result["policy"] == "report_first"
            assert result["applied"] is False
            assert result["proposals_count"] == 1
            assert "plan_path" in result
            assert "report_path" in result
            assert "message" in result  # Should have report-only message
    
    def test_repair_pipeline_auto_apply(self):
        """Test repair pipeline with auto_apply=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            citation_result = CitationValidationResult(
                citation_id="cite-001",
                paper_id="paper-001",
                conclusion=ValidationConclusion.WRONG_SOURCE,
                root_causes=[RootCause.CITATION_MAPPING_ERROR],
                evidence_candidates=[],
                details={"block_id": "s1_b1"},
            )
            validation_report = ReviewValidationReport(
                report_id="val-001",
                created_at=datetime.now().isoformat(),
                total_citations=1,
                supported_count=0,
                partial_support_count=0,
                unsupported_count=0,
                wrong_source_count=1,
                needs_review_count=0,
                citation_results=[citation_result],
            )
            
            review_draft = {
                "artifact_type": "review_draft",
                "artifact_version": "v1",
                "content": {
                    "sections": [
                        {
                            "section_number": 1,
                            "section_title": "Test",
                            "blocks": [
                                {
                                    "block_id": "s1_b1",
                                    "block_kind": "paragraph",
                                    "block_order": 1,
                                    "text": "Test paragraph with citation.",
                                }
                            ],
                        }
                    ]
                },
            }
            citation_manifest = {"citations": [{"citation_id": "cite-001", "paper_id": "paper-001", "text": "citation"}]}
            paper_artifact = {
                "paper_identity": {"canonical_paper_key": "paper-001"},
                "analysis": {"ai_summary": {}},
                "stage1_inputs": {"selected_visual_refs": []},
            }
            
            result = run_repair_pipeline(
                validation_report=validation_report,
                review_draft=review_draft,
                citation_manifest=citation_manifest,
                paper_artifacts=[paper_artifact],
                job_id="job-001",
                workspace=workspace,
                registry=registry,
                auto_apply=True,  # Auto-apply mode
            )
            
            assert result["repair_pipeline"] is True
            assert result["policy"] == "auto_apply_safe"
            assert result["applied"] is True
            assert "apply_result_path" in result
            assert "patched_review_draft" in result


class TestRepairArtifactsDurability:
    """Test that repair artifacts are durably persisted."""
    
    def test_all_artifact_types_registered(self):
        """Test that all repair artifact types are properly registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = JobWorkspace(tmpdir, "test_project", "job-001")
            registry = ArtifactRegistry(workspace.paths.registry_path, "job-001")
            
            # Create a full repair pipeline result
            plan = RepairPlan(
                plan_id="durability-test",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                validation_report_id="val-001",
                proposals=[],
                policy=RepairPolicy.REPORT_FIRST,
            )
            
            plan_path = persist_repair_plan(plan, workspace, registry)
            
            report = RepairReport(
                report_id="durability-test",
                created_at=datetime.now().isoformat(),
                created_from_job_id="job-001",
                plan_id="durability-test",
                apply_result_id=None,
                summary={"total": 0},
                proposals_detail=[],
            )
            
            report_path = persist_repair_report(report, workspace, registry)
            
            apply_result = RepairApplyResult(
                success=True,
                plan_id="durability-test",
                applied_count=0,
                rejected_count=0,
                applied_proposals=[],
                rejected_proposals=[],
            )
            
            apply_path = persist_repair_apply_result(
                apply_result, [], workspace, registry, "job-001"
            )
            
            # Verify all files exist
            assert os.path.exists(plan_path)
            assert os.path.exists(report_path)
            assert os.path.exists(apply_path)
            
            # Verify all are in registry
            assert registry.get("repair_plan_durability-test") is not None
            assert registry.get("repair_report_durability-test") is not None
            assert registry.get("repair_apply_result_durability-test") is not None
            
            # Verify registry is saved to disk
            assert os.path.exists(registry.registry_path)
            
            # Load registry fresh and verify
            registry2 = ArtifactRegistry(registry.registry_path, "job-001")
            assert registry2.get("repair_plan_durability-test") is not None
            assert registry2.get("repair_report_durability-test") is not None
            assert registry2.get("repair_apply_result_durability-test") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

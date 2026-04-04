"""Tests for Week 4 Repair Pipeline.

Tests repair proposal creation, guard enforcement, and apply behavior.
"""

import pytest
from datetime import datetime

from validation.repair_models import (
    AppliedPatchRecord,
    ApplyGuardResult,
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
from validation.repair_planner import (
    RepairPlanner,
    _compute_hash,
    _build_dependency_bundle,
    _map_validation_root_cause,
    run_repair_planning,
)
from validation.repair_apply import (
    RepairApplier,
    check_apply_guards,
    _compute_anchor_hash,
    run_repair_apply,
)
from validation.review_validator import (
    CitationValidationResult,
    ReviewValidationReport,
    RootCause,
    ValidationConclusion,
)


class TestDependencyHashBundle:
    """Test DependencyHashBundle structure and behavior."""
    
    def test_bundle_creation(self):
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        assert bundle.summary_hash == "abc123"
        assert bundle.paper_artifact_hash == "def456"
        assert bundle.visual_manifest_hash == "ghi789"
        assert bundle.selected_visual_refs_hash == "jkl012"
    
    def test_bundle_to_dict(self):
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        d = bundle.to_dict()
        assert d["summary_hash"] == "abc123"
        assert d["paper_artifact_hash"] == "def456"
    
    def test_bundle_from_dict(self):
        data = {
            "summary_hash": "abc123",
            "paper_artifact_hash": "def456",
            "visual_manifest_hash": "ghi789",
            "selected_visual_refs_hash": "jkl012",
        }
        bundle = DependencyHashBundle.from_dict(data)
        assert bundle.summary_hash == "abc123"


class TestPatchTargetSignature:
    """Test PatchTargetSignature structure."""
    
    def test_signature_creation(self):
        sig = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="This is the anchor...",
            anchor_hash="a1b2c3d4",
        )
        assert sig.block_id == "s1_b1"
        assert sig.anchor_text == "This is the anchor..."
        assert sig.anchor_hash == "a1b2c3d4"
        assert sig.span_start is None
        assert sig.span_end is None
    
    def test_signature_with_span(self):
        sig = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="This is the anchor...",
            anchor_hash="a1b2c3d4",
            span_start=10,
            span_end=50,
        )
        assert sig.span_start == 10
        assert sig.span_end == 50


class TestPatchProposal:
    """Test PatchProposal structure."""
    
    def test_proposal_creation(self):
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Original text...",
            anchor_hash="a1b2c3d4",
        )
        proposal = PatchProposal(
            proposal_id="prop-001",
            citation_id="cite-001",
            root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original text",
            proposed_text="Proposed text",
            confidence=0.8,
            fix_strategy="manifest_fix_rerender",
            dependency_bundle=bundle,
        )
        assert proposal.proposal_id == "prop-001"
        assert proposal.root_cause == RepairRootCause.CITATION_MAPPING_ERROR
        assert proposal.granularity == PatchGranularity.SPAN
    
    def test_proposal_to_dict(self):
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Original text...",
            anchor_hash="a1b2c3d4",
        )
        proposal = PatchProposal(
            proposal_id="prop-001",
            citation_id="cite-001",
            root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original text",
            proposed_text="Proposed text",
            confidence=0.8,
            fix_strategy="manifest_fix_rerender",
            dependency_bundle=bundle,
        )
        d = proposal.to_dict()
        assert d["proposal_id"] == "prop-001"
        assert d["root_cause"] == "citation_mapping_error"
        assert d["granularity"] == "span"


class TestRepairPlan:
    """Test RepairPlan structure and behavior."""
    
    def test_plan_creation(self):
        plan = RepairPlan(
            plan_id="plan-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            validation_report_id="val-001",
            proposals=[],
            policy=RepairPolicy.REPORT_FIRST,
        )
        assert plan.plan_id == "plan-001"
        assert plan.policy == RepairPolicy.REPORT_FIRST
        assert plan.artifact_type == "repair_plan"
    
    def test_mapping_first_priority(self):
        """Test that citation_mapping_error proposals are prioritized."""
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
            anchor_hash="a1b2c3d4",
        )
        
        # Create proposals with different root causes
        prop_visual = PatchProposal(
            proposal_id="prop-visual",
            citation_id="cite-001",
            root_cause=RepairRootCause.VISUAL_UNDERSTANDING_GAP,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original",
            proposed_text="Proposed",
            confidence=0.9,
            fix_strategy="summary_recheck",
            dependency_bundle=bundle,
        )
        
        prop_mapping = PatchProposal(
            proposal_id="prop-mapping",
            citation_id="cite-002",
            root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original",
            proposed_text="Proposed",
            confidence=0.7,
            fix_strategy="manifest_fix",
            dependency_bundle=bundle,
        )
        
        plan = RepairPlan(
            plan_id="plan-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            validation_report_id="val-001",
            proposals=[prop_visual, prop_mapping],  # Visual first in list
            policy=RepairPolicy.REPORT_FIRST,
        )
        
        mapping_first = plan.get_mapping_first_proposals()
        assert len(mapping_first) == 1
        assert mapping_first[0].proposal_id == "prop-mapping"


class TestApplyGuardResult:
    """Test ApplyGuardResult structure."""
    
    def test_guard_result_creation(self):
        result = ApplyGuardResult(
            can_apply=True,
            version_guard_passed=True,
            anchor_hash_guard_passed=True,
            dependency_guard_passed=True,
            block_reasons=[],
        )
        assert result.can_apply is True
        assert result.version_guard_passed is True
    
    def test_guard_result_blocked(self):
        result = ApplyGuardResult(
            can_apply=False,
            version_guard_passed=True,
            anchor_hash_guard_passed=False,
            dependency_guard_passed=True,
            block_reasons=["Anchor hash mismatch"],
        )
        assert result.can_apply is False
        assert len(result.block_reasons) == 1


class TestRepairPlanner:
    """Test RepairPlanner functionality."""
    
    def test_create_plan_empty(self):
        """Test creating a plan with no validation findings."""
        report = ReviewValidationReport(
            report_id="val-001",
            created_at=datetime.now().isoformat(),
            total_citations=0,
            supported_count=0,
            partial_support_count=0,
            unsupported_count=0,
            wrong_source_count=0,
            needs_review_count=0,
            citation_results=[],
        )
        review_draft = {
            "artifact_type": "review_draft",
            "content": {"sections": []},
        }
        citation_manifest = {"citations": []}
        paper_artifacts = []
        
        planner = RepairPlanner(
            validation_report=report,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id="job-001",
        )
        
        plan = planner.create_plan()
        assert len(plan.proposals) == 0
    
    def test_mapping_first_priority_in_plan(self):
        """Test that mapping errors are prioritized in plan creation."""
        citation_result = CitationValidationResult(
            citation_id="cite-001",
            paper_id="paper-001",
            conclusion=ValidationConclusion.WRONG_SOURCE,
            root_causes=[RootCause.CITATION_MAPPING_ERROR],
            evidence_candidates=[],
            details={},
            claim_text="Test claim",
            claim_context="Test context",
            evidence_excerpt_list=[],
            reasoning_summary="Test reasoning",
            repair_hint="Test repair hint",
        )
        report = ReviewValidationReport(
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
        citation_manifest = {"citations": []}
        paper_artifact = {
            "paper_identity": {"canonical_paper_key": "paper-001"},
            "analysis": {"ai_summary": {}},
            "stage1_inputs": {"selected_visual_refs": []},
        }
        
        planner = RepairPlanner(
            validation_report=report,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=[paper_artifact],
            job_id="job-001",
        )
        
        plan = planner.create_plan()
        assert len(plan.proposals) == 1
        assert plan.proposals[0].root_cause == RepairRootCause.CITATION_MAPPING_ERROR


class TestApplyGuards:
    """Test guard enforcement in repair apply."""
    
    def test_version_guard_failure(self):
        """Test that version guard blocks apply on invalid artifact type."""
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
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
        
        # Invalid review_draft (wrong artifact_type)
        review_draft = {"artifact_type": "wrong_type"}
        paper_artifacts = []
        
        result = check_apply_guards(proposal, review_draft, paper_artifacts)
        assert result.can_apply is False
        assert result.version_guard_passed is False
    
    def test_anchor_hash_guard_failure(self):
        """Test that anchor hash guard blocks apply on mismatch."""
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
            anchor_hash="WRONG_HASH",  # Wrong hash
        )
        proposal = PatchProposal(
            proposal_id="prop-001",
            citation_id="cite-001",
            root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original text content",
            proposed_text="Proposed",
            confidence=0.8,
            fix_strategy="manifest_fix",
            dependency_bundle=bundle,
        )
        
        review_draft = {
            "artifact_type": "review_draft",
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Test",
                        "blocks": [
                            {
                                "block_id": "s1_b1",
                                "text": "Different text content",  # Different from what proposal expects
                            }
                        ],
                    }
                ]
            },
        }
        paper_artifacts = []
        
        result = check_apply_guards(proposal, review_draft, paper_artifacts)
        assert result.can_apply is False
        assert result.anchor_hash_guard_passed is False
    
    def test_dependency_guard_failure(self):
        """Test that dependency guard blocks apply on stale dependencies."""
        bundle = DependencyHashBundle(
            summary_hash="OLD_HASH",  # Old hash
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
            anchor_hash=_compute_anchor_hash("Test text"),
        )
        proposal = PatchProposal(
            proposal_id="prop-001",
            citation_id="cite-001",
            root_cause=RepairRootCause.CITATION_MAPPING_ERROR,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Test text",
            proposed_text="Proposed",
            confidence=0.8,
            fix_strategy="manifest_fix",
            dependency_bundle=bundle,
            metadata={"paper_id": "paper-001"},
        )
        
        review_draft = {
            "artifact_type": "review_draft",
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Test",
                        "blocks": [
                            {
                                "block_id": "s1_b1",
                                "text": "Test text",
                            }
                        ],
                    }
                ]
            },
        }
        # Paper artifact with different summary hash
        paper_artifact = {
            "paper_identity": {"canonical_paper_key": "paper-001"},
            "analysis": {"ai_summary": {"new": "data"}},  # Different from bundle
            "stage1_inputs": {"selected_visual_refs": []},
        }
        
        result = check_apply_guards(proposal, review_draft, [paper_artifact])
        assert result.can_apply is False
        assert result.dependency_guard_passed is False


class TestBlockSpanOnlyBehavior:
    """Test that patches are block/span only (no whole-section rewrite)."""
    
    def test_granularity_is_block_or_span(self):
        """Test that only BLOCK and SPAN granularities exist."""
        assert PatchGranularity.BLOCK.value == "block"
        assert PatchGranularity.SPAN.value == "span"
        
        # Should not have section or chapter granularity
        granularities = [g.value for g in PatchGranularity]
        assert "section" not in granularities
        assert "chapter" not in granularities


class TestVisualUnderstandingGapRouting:
    """Test visual_understanding_gap routing behavior."""
    
    def test_visual_gap_fix_strategy(self):
        """Test that visual gaps use summary_recheck_visual_bundle strategy."""
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
            anchor_hash="a1b2c3d4",
        )
        proposal = PatchProposal(
            proposal_id="prop-001",
            citation_id="cite-001",
            root_cause=RepairRootCause.VISUAL_UNDERSTANDING_GAP,
            granularity=PatchGranularity.SPAN,
            target=target,
            original_text="Original",
            proposed_text="Proposed",
            confidence=0.7,
            fix_strategy="summary_recheck_visual_bundle",
            dependency_bundle=bundle,
        )
        assert proposal.root_cause == RepairRootCause.VISUAL_UNDERSTANDING_GAP
        assert proposal.fix_strategy == "summary_recheck_visual_bundle"


class TestReportFirstPolicy:
    """Test that default policy is report-first, not auto-apply."""
    
    def test_default_policy_is_report_first(self):
        """Test that default policy is REPORT_FIRST."""
        report = ReviewValidationReport(
            report_id="val-001",
            created_at=datetime.now().isoformat(),
            total_citations=0,
            supported_count=0,
            partial_support_count=0,
            unsupported_count=0,
            wrong_source_count=0,
            needs_review_count=0,
            citation_results=[],
        )
        review_draft = {"artifact_type": "review_draft", "content": {"sections": []}}
        citation_manifest = {"citations": []}
        paper_artifacts = []
        
        planner = RepairPlanner(
            validation_report=report,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id="job-001",
        )
        
        plan = planner.create_plan()
        assert plan.policy == RepairPolicy.REPORT_FIRST


class TestRunRepairPlanning:
    """Test run_repair_planning entry point."""
    
    def test_entry_point_returns_expected_structure(self):
        """Test that entry point returns expected structure."""
        report = ReviewValidationReport(
            report_id="val-001",
            created_at=datetime.now().isoformat(),
            total_citations=0,
            supported_count=0,
            partial_support_count=0,
            unsupported_count=0,
            wrong_source_count=0,
            needs_review_count=0,
            citation_results=[],
        )
        review_draft = {"artifact_type": "review_draft", "content": {"sections": []}}
        citation_manifest = {"citations": []}
        paper_artifacts = []
        
        result = run_repair_planning(
            validation_report=report,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id="job-001",
        )
        
        assert result["week4_repair_planning"] is True
        assert "plan" in result
        assert "report" in result


class TestRunRepairApply:
    """Test run_repair_apply entry point."""
    
    def test_dry_run_checks_guards(self):
        """Test that dry run checks guards without applying."""
        bundle = DependencyHashBundle(
            summary_hash="abc123",
            paper_artifact_hash="def456",
            visual_manifest_hash="ghi789",
            selected_visual_refs_hash="jkl012",
        )
        target = PatchTargetSignature(
            block_id="s1_b1",
            anchor_text="Text...",
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
            plan_id="plan-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            validation_report_id="val-001",
            proposals=[proposal],
            policy=RepairPolicy.REPORT_FIRST,
        )
        review_draft = {
            "artifact_type": "review_draft",
            "content": {
                "sections": [
                    {
                        "section_number": 1,
                        "section_title": "Test",
                        "blocks": [
                            {
                                "block_id": "s1_b1",
                                "text": "Original",
                            }
                        ],
                    }
                ]
            },
        }
        citation_manifest = {"citations": []}
        paper_artifacts = []
        
        result = run_repair_apply(
            repair_plan=plan,
            review_draft=review_draft,
            citation_manifest=citation_manifest,
            paper_artifacts=paper_artifacts,
            job_id="job-001",
            dry_run=True,
        )
        
        assert result["week4_repair_apply"] is True
        assert result["dry_run"] is True
        assert "proposal_checks" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

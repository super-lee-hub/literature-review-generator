"""Tests for Week 5 Outline JSON-first + Critique + Arbitration.

Tests outline JSON representation, critique taxonomy, and arbitration behavior.
"""

import pytest
from datetime import datetime

from outline.models import (
    ArbitrationDecision,
    CritiqueArbitration,
    CritiqueCategory,
    OutlineArbitrationResult,
    OutlineCritique,
    OutlineDocument,
    OutlineSection,
    ReviewStatus,
    ReviewedOutlineDocument,
)
from outline.generator import (
    OutlineGenerator,
    create_outline_from_markdown,
    create_outline_from_sections,
    run_outline_generation,
)
from outline.arbitration import (
    OutlineArbitrator,
    adopt_outline,
    apply_accepted_critiques,
    arbitrate_critique,
    create_critique,
    run_arbitration,
    run_outline_adopt,
    run_outline_arbitration,
    run_outline_critique,
    run_peer_critique,
)


class TestOutlineSection:
    """Test OutlineSection structure."""
    
    def test_section_creation(self):
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce the topic",
            supporting_summary_refs=["ref-001", "ref-002"],
            children=[],
        )
        assert section.section_id == "sec-001"
        assert section.title == "Introduction"
        assert section.purpose == "Introduce the topic"
        assert len(section.supporting_summary_refs) == 2
    
    def test_section_with_children(self):
        child = OutlineSection(
            section_id="sec-001-1",
            title="Background",
            purpose="Provide background",
            supporting_summary_refs=["ref-003"],
            children=[],
        )
        parent = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce the topic",
            supporting_summary_refs=["ref-001"],
            children=[child],
        )
        assert len(parent.children) == 1
        assert parent.children[0].title == "Background"
    
    def test_section_to_dict(self):
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        d = section.to_dict()
        assert d["section_id"] == "sec-001"
        assert d["title"] == "Introduction"
        assert d["purpose"] == "Introduce"
    
    def test_section_from_dict(self):
        data = {
            "section_id": "sec-001",
            "title": "Introduction",
            "purpose": "Introduce",
            "supporting_summary_refs": ["ref-001"],
            "children": [],
        }
        section = OutlineSection.from_dict(data)
        assert section.section_id == "sec-001"
        assert section.title == "Introduction"


class TestCritiqueCategory:
    """Test critique taxonomy is fixed."""
    
    def test_all_categories_exist(self):
        """Test that all required critique categories exist."""
        assert CritiqueCategory.MISSING_THEME.value == "missing_theme"
        assert CritiqueCategory.WEAK_SUPPORT_FROM_SUMMARIES.value == "weak_support_from_summaries"
        assert CritiqueCategory.REDUNDANT_SECTION.value == "redundant_section"
        assert CritiqueCategory.ORDERING_ISSUE.value == "ordering_issue"
        assert CritiqueCategory.OVERCLAIM.value == "overclaim"
        assert CritiqueCategory.SCOPE_MISMATCH.value == "scope_mismatch"
    
    def test_no_extra_categories(self):
        """Test that only the specified categories exist."""
        expected = {
            "missing_theme",
            "weak_support_from_summaries",
            "redundant_section",
            "ordering_issue",
            "overclaim",
            "scope_mismatch",
        }
        actual = {c.value for c in CritiqueCategory}
        assert actual == expected


class TestOutlineCritique:
    """Test OutlineCritique structure."""
    
    def test_critique_creation(self):
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at=datetime.now().isoformat(),
            created_by="model-001",
            target_section_id="sec-001",
            category=CritiqueCategory.MISSING_THEME,
            description="This theme is missing",
            severity="high",
            suggested_fix="Add section about X",
        )
        assert critique.critique_id == "crit-001"
        assert critique.category == CritiqueCategory.MISSING_THEME
        assert critique.severity == "high"
    
    def test_critique_whole_outline(self):
        """Test critique targeting whole outline (no section_id)."""
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at=datetime.now().isoformat(),
            created_by="model-001",
            target_section_id=None,  # Whole outline
            category=CritiqueCategory.SCOPE_MISMATCH,
            description="Outline scope is too broad",
            severity="medium",
            suggested_fix=None,
        )
        assert critique.target_section_id is None
    
    def test_critique_to_dict(self):
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at="2024-01-01T00:00:00",
            created_by="model-001",
            target_section_id="sec-001",
            category=CritiqueCategory.MISSING_THEME,
            description="Missing theme",
            severity="high",
            suggested_fix=None,
        )
        d = critique.to_dict()
        assert d["critique_id"] == "crit-001"
        assert d["category"] == "missing_theme"
        assert d["severity"] == "high"
    
    def test_critique_from_dict(self):
        data = {
            "critique_id": "crit-001",
            "created_at": "2024-01-01T00:00:00",
            "created_by": "model-001",
            "target_section_id": "sec-001",
            "category": "missing_theme",
            "description": "Missing theme",
            "severity": "high",
        }
        critique = OutlineCritique.from_dict(data)
        assert critique.critique_id == "crit-001"
        assert critique.category == CritiqueCategory.MISSING_THEME


class TestOutlineDocument:
    """Test OutlineDocument structure."""
    
    def test_document_creation(self):
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=["hash-001"],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        assert doc.outline_id == "outline-001"
        assert doc.review_status == ReviewStatus.DRAFT
        assert len(doc.sections) == 1
    
    def test_document_required_artifact_fields(self):
        """Test that document has all required artifact fields."""
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[],
            critiques=[],
            arbitration_result_id=None,
        )
        assert doc.artifact_type == "outline_document"
        assert doc.artifact_version == "v1"
        assert doc.created_from_job_id == "job-001"
        assert doc.created_at is not None
    
    def test_document_with_critiques(self):
        """Test adding critiques to document."""
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[],
            critiques=[],
            arbitration_result_id=None,
        )
        
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at=datetime.now().isoformat(),
            created_by="model-001",
            target_section_id=None,
            category=CritiqueCategory.MISSING_THEME,
            description="Missing theme",
            severity="high",
            suggested_fix=None,
        )
        
        doc_with_critique = doc.with_critiques([critique])
        assert len(doc_with_critique.critiques) == 1
        assert doc_with_critique.review_status == ReviewStatus.CRITIQUED
    
    def test_document_get_section_by_id(self):
        """Test finding section by ID."""
        child = OutlineSection(
            section_id="sec-001-1",
            title="Background",
            purpose="Background info",
            supporting_summary_refs=[],
            children=[],
        )
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=[],
            children=[child],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        
        found = doc.get_section_by_id("sec-001-1")
        assert found is not None
        assert found.title == "Background"
        
        not_found = doc.get_section_by_id("nonexistent")
        assert not_found is None
    
    def test_document_to_markdown(self):
        """Test markdown projection."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce the topic",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=["hash-001"],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        
        markdown = doc.to_markdown()
        assert "# Literature Review Outline" in markdown
        assert "Introduction" in markdown
        assert "Purpose:" in markdown


class TestOutlineGenerator:
    """Test outline generation from markdown and sections."""
    
    def test_from_markdown_basic(self):
        """Test parsing markdown into outline."""
        markdown = """# Outline

## 1. Introduction

Some notes here.

## 2. Methods

More notes.
"""
        summaries = [{"title": "Paper 1"}]
        
        doc = create_outline_from_markdown(
            markdown_text=markdown,
            job_id="job-001",
            generator_model="gpt-4",
            summaries=summaries,
        )
        
        assert doc.artifact_type == "outline_document"
        assert len(doc.sections) == 2
        assert doc.sections[0].title == "Introduction"
        assert doc.sections[1].title == "Methods"
    
    def test_from_sections(self):
        """Test creating outline from structured sections."""
        sections_data = [
            {
                "section_id": "sec-001",
                "title": "Introduction",
                "purpose": "Introduce",
                "supporting_summary_refs": ["ref-001"],
                "children": [],
            },
            {
                "section_id": "sec-002",
                "title": "Conclusion",
                "purpose": "Conclude",
                "supporting_summary_refs": ["ref-002"],
                "children": [],
            },
        ]
        summaries = [{"title": "Paper 1"}]
        
        doc = create_outline_from_sections(
            sections_data=sections_data,
            job_id="job-001",
            generator_model="gpt-4",
            summaries=summaries,
        )
        
        assert len(doc.sections) == 2
        assert doc.sections[0].title == "Introduction"
        assert doc.sections[1].title == "Conclusion"


class TestPeerCritique:
    """Test peer critique functionality."""
    
    def test_critique_sections_without_refs(self):
        """Test that sections without summary refs are critiqued."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=[],  # Empty
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        summaries = [{"title": "Paper 1"}]
        
        critiques = run_peer_critique(
            outline=doc,
            critic_model="critic-model",
            summaries=summaries,
        )
        
        # Should have at least one critique for missing refs
        assert len(critiques) >= 1
        assert any(c.category == CritiqueCategory.WEAK_SUPPORT_FROM_SUMMARIES for c in critiques)
    
    def test_critique_ordering_issues(self):
        """Test that ordering issues are detected."""
        sections = [
            OutlineSection(
                section_id="sec-001",
                title="Methods",
                purpose="Methods",
                supporting_summary_refs=["ref-001"],
                children=[],
            ),
            OutlineSection(
                section_id="sec-002",
                title="Introduction",  # Introduction after Methods
                purpose="Introduce",
                supporting_summary_refs=["ref-002"],
                children=[],
            ),
        ]
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=sections,
            critiques=[],
            arbitration_result_id=None,
        )
        summaries = []
        
        critiques = run_peer_critique(
            outline=doc,
            critic_model="critic-model",
            summaries=summaries,
        )
        
        # Should detect ordering issue
        assert any(c.category == CritiqueCategory.ORDERING_ISSUE for c in critiques)


class TestArbitration:
    """Test arbitration functionality."""
    
    def test_arbitrate_critique_accept(self):
        """Test accepting a critique."""
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at=datetime.now().isoformat(),
            created_by="model-001",
            target_section_id="sec-001",
            category=CritiqueCategory.MISSING_THEME,
            description="Missing theme",
            severity="high",
            suggested_fix=None,
        )
        
        arb = arbitrate_critique(
            critique=critique,
            decision=ArbitrationDecision.ACCEPT,
            reason="Valid critique",
            arbitrated_by="arbitrator-001",
        )
        
        assert arb.critique_id == "crit-001"
        assert arb.decision == ArbitrationDecision.ACCEPT
    
    def test_run_arbitration(self):
        """Test running arbitration on multiple critiques."""
        critiques = [
            OutlineCritique(
                critique_id="crit-001",
                created_at=datetime.now().isoformat(),
                created_by="model-001",
                target_section_id="sec-001",
                category=CritiqueCategory.MISSING_THEME,
                description="Missing theme",
                severity="high",
                suggested_fix=None,
            ),
            OutlineCritique(
                critique_id="crit-002",
                created_at=datetime.now().isoformat(),
                created_by="model-001",
                target_section_id="sec-002",
                category=CritiqueCategory.REDUNDANT_SECTION,
                description="Redundant",
                severity="medium",
                suggested_fix=None,
            ),
        ]
        
        arbitrations = [
            CritiqueArbitration(
                critique_id="crit-001",
                decision=ArbitrationDecision.ACCEPT,
                reason="Valid",
                arbitrated_at=datetime.now().isoformat(),
                arbitrated_by="arb-001",
            ),
            CritiqueArbitration(
                critique_id="crit-002",
                decision=ArbitrationDecision.REJECT,
                reason="Not valid",
                arbitrated_at=datetime.now().isoformat(),
                arbitrated_by="arb-001",
            ),
        ]
        
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.CRITIQUED,
            sections=[],
            critiques=critiques,
            arbitration_result_id=None,
        )
        
        result = run_arbitration(
            outline=doc,
            critiques=critiques,
            arbitrations=arbitrations,
            job_id="job-001",
            arbitrated_by="arb-001",
        )
        
        assert result.outline_id == "outline-001"
        assert "crit-001" in result.accepted_critiques
        assert "crit-002" in result.rejected_critiques


class TestOutlineAdoption:
    """Test outline adoption behavior."""
    
    def test_adoption_creates_reviewed_outline(self):
        """Test that adoption creates a reviewed outline document."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=["hash-001"],
            generator_model="gpt-4",
            review_status=ReviewStatus.ARBITRATED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        
        arb_result = OutlineArbitrationResult(
            result_id="arb-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            outline_id="outline-001",
            arbitrations=[],
            accepted_critiques=[],
            rejected_critiques=[],
            deferred_critiques=[],
            modified_sections=[],
        )
        
        reviewed = adopt_outline(
            outline=doc,
            arbitration_result=arb_result,
            job_id="job-001",
            adopted_by="user-001",
        )
        
        assert reviewed.artifact_type == "reviewed_outline_document"
        assert reviewed.original_outline_id == "outline-001"
        assert reviewed.adopted_by == "user-001"
        assert reviewed.outline.review_status == ReviewStatus.ADOPTED
    
    def test_no_adoption_without_arbitration(self):
        """Test that outline cannot be adopted without arbitration."""
        arbitrator = OutlineArbitrator(
            outline=OutlineDocument(
                artifact_type="outline_document",
                artifact_version="v1",
                created_from_job_id="job-001",
                created_at=datetime.now().isoformat(),
                outline_id="outline-001",
                outline_version="v1",
                source_summary_hashes=[],
                generator_model="gpt-4",
                review_status=ReviewStatus.DRAFT,
                sections=[],
                critiques=[],
                arbitration_result_id=None,
            ),
            job_id="job-001",
        )
        
        # No arbitrations added
        result = arbitrator.adopt("user-001")
        assert result is None


class TestEntryPoints:
    """Test Week 5 entry points."""
    
    def test_run_outline_generation(self):
        """Test outline generation entry point."""
        markdown = "## 1. Introduction\n\nNotes"
        summaries = [{"title": "Paper 1"}]
        
        result = run_outline_generation(
            markdown_text=markdown,
            sections_data=None,
            job_id="job-001",
            generator_model="gpt-4",
            summaries=summaries,
        )
        
        assert result["week5_outline_generation"] is True
        assert "outline" in result
    
    def test_run_outline_critique(self):
        """Test outline critique entry point."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=[],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        summaries = []
        
        result = run_outline_critique(
            outline=doc,
            critic_model="critic-model",
            coverage_critic_model="coverage-critic-model",
            summaries=summaries,
            job_id="job-001",
        )
        
        assert result["week5_outline_critique"] is True
        assert "critiques" in result
        assert "outline" in result
    
    def test_run_outline_arbitration(self):
        """Test outline arbitration entry point."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=[],
            children=[],
        )
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at=datetime.now().isoformat(),
            created_by="model-001",
            target_section_id="sec-001",
            category=CritiqueCategory.MISSING_THEME,
            description="Missing",
            severity="high",
            suggested_fix=None,
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.CRITIQUED,
            sections=[section],
            critiques=[critique],
            arbitration_result_id=None,
        )
        
        arb = CritiqueArbitration(
            critique_id="crit-001",
            decision=ArbitrationDecision.ACCEPT,
            reason="Valid",
            arbitrated_at=datetime.now().isoformat(),
            arbitrated_by="arb-001",
        )
        
        result = run_outline_arbitration(
            outline=doc,
            arbitrations=[arb],
            job_id="job-001",
            arbitrated_by="arb-001",
        )
        
        assert result["week5_outline_arbitration"] is True
        assert "arbitration_result" in result
    
    def test_run_outline_adopt(self):
        """Test outline adopt entry point."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce",
            supporting_summary_refs=[],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ARBITRATED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        
        arb_result = OutlineArbitrationResult(
            result_id="arb-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            outline_id="outline-001",
            arbitrations=[],
            accepted_critiques=[],
            rejected_critiques=[],
            deferred_critiques=[],
            modified_sections=[],
        )
        
        result = run_outline_adopt(
            outline=doc,
            arbitration_result=arb_result,
            job_id="job-001",
            adopted_by="user-001",
        )
        
        assert result["week5_outline_adopt"] is True
        assert "reviewed_outline" in result


class TestNoSilentOverwrite:
    """Test that there is no silent overwrite of outlines."""
    
    def test_outline_with_adopted_status_is_immutable(self):
        """Test that adopted outline status is preserved."""
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[],
            critiques=[],
            arbitration_result_id=None,
        )
        
        adopted = doc.with_adopted_status()
        assert adopted.review_status == ReviewStatus.ADOPTED
        
        # Original should be unchanged
        assert doc.review_status == ReviewStatus.DRAFT


class TestApplyAcceptedCritiques:
    """Test apply_accepted_critiques functionality."""
    
    def test_ordering_issue_moves_intro_to_front(self):
        """Test that introduction ordering issue moves section to front."""
        from outline.arbitration import apply_accepted_critiques
        
        # Create outline with introduction at the end
        section_methods = OutlineSection(
            section_id="sec-001",
            title="Methods",
            purpose="Describe methods",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        section_intro = OutlineSection(
            section_id="sec-002",
            title="Introduction",
            purpose="Introduce topic",
            supporting_summary_refs=["ref-002"],
            children=[],
        )
        doc = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at=datetime.now().isoformat(),
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=["ref-001", "ref-002"],
            generator_model="gpt-4",
            review_status=ReviewStatus.CRITIQUED,
            sections=[section_methods, section_intro],  # Intro at end
            critiques=[],
            arbitration_result_id=None,
        )
        
        # Create critique and arbitration
        critique = create_critique(
            target_section_id="sec-002",
            category=CritiqueCategory.ORDERING_ISSUE,
            description="Introduction should come first",
            created_by="critic-model",
            severity="medium",
            suggested_fix="Move to front",
        )
        arbitration = CritiqueArbitration(
            critique_id=critique.critique_id,
            decision=ArbitrationDecision.ACCEPT,
            reason="Valid ordering issue",
            arbitrated_at=datetime.now().isoformat(),
            arbitrated_by="arbitrator-001",
        )
        arb_result = OutlineArbitrationResult(
            result_id="arb-001",
            created_at=datetime.now().isoformat(),
            created_from_job_id="job-001",
            outline_id="outline-001",
            arbitrations=[arbitration],
            accepted_critiques=[critique.critique_id],
            rejected_critiques=[],
            deferred_critiques=[],
            modified_sections=["sec-002"],
        )
        
        # Add critique to outline
        doc_with_critique = doc.with_critiques([critique])
        
        # Apply accepted critique
        modified = apply_accepted_critiques(doc_with_critique, arb_result)
        
        # Verify introduction is now first
        assert len(modified.sections) == 2
        assert modified.sections[0].title == "Introduction"
        assert modified.sections[1].title == "Methods"
        assert modified.review_status == ReviewStatus.ARBITRATED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

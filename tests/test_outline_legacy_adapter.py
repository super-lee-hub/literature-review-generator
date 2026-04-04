"""Tests for outline legacy adapter.

Tests legacy adapter behavior, no silent overwrite before adopt,
adopted reviewed outline becomes preferred input, and fallback to legacy markdown.
"""

import json
import os
from pathlib import Path

import pytest

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
from outline.legacy_adapter import (
    OutlineLegacyAdapter,
    get_outline_markdown_for_downstream,
    is_reviewed_outline_adopted,
    outline_document_to_markdown,
    reviewed_outline_to_markdown,
)


class TestLegacyAdapterBasics:
    """Test basic legacy adapter functionality."""

    def test_reviewed_outline_to_markdown(self):
        """Test converting reviewed outline to markdown."""
        section = OutlineSection(
            section_id="sec-001",
            title="Introduction",
            purpose="Introduce the topic",
            supporting_summary_refs=["ref-001"],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=["hash-001"],
            generator_model="gpt-4",
            review_status=ReviewStatus.ADOPTED,
            sections=[section],
            critiques=[],
            arbitration_result_id="arb-001",
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        markdown = reviewed_outline_to_markdown(reviewed)

        assert "# Literature Review Outline" in markdown
        assert "Introduction" in markdown
        assert "Purpose:" in markdown

    def test_outline_document_to_markdown(self):
        """Test converting outline document to markdown."""
        section = OutlineSection(
            section_id="sec-001",
            title="Methods",
            purpose="Describe methods",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )

        markdown = outline_document_to_markdown(outline)

        assert "# Literature Review Outline" in markdown
        assert "Methods" in markdown

    def test_is_reviewed_outline_adopted_true(self):
        """Test checking if reviewed outline is adopted (True case)."""
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ADOPTED,
            sections=[],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        assert is_reviewed_outline_adopted(reviewed) is True

    def test_is_reviewed_outline_adopted_false(self):
        """Test checking if reviewed outline is adopted (False case)."""
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,  # Not adopted
            sections=[],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        assert is_reviewed_outline_adopted(reviewed) is False


class TestGetOutlineMarkdownForDownstream:
    """Test get_outline_markdown_for_downstream priority logic."""

    def test_prefers_adopted_reviewed_outline(self):
        """Test that adopted reviewed outline is preferred over fallback."""
        section = OutlineSection(
            section_id="sec-001",
            title="Adopted Section",
            purpose="This is adopted",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ADOPTED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        fallback = "# Fallback Outline\n\n## 1. Fallback Section"

        result = get_outline_markdown_for_downstream(reviewed, fallback)

        assert "Adopted Section" in result
        assert "Fallback Section" not in result

    def test_fallback_when_no_reviewed_outline(self):
        """Test fallback is used when no reviewed outline exists."""
        fallback = "# Fallback Outline\n\n## 1. Fallback Section"

        result = get_outline_markdown_for_downstream(None, fallback)

        assert result == fallback
        assert "Fallback Section" in result

    def test_fallback_when_reviewed_outline_not_adopted(self):
        """Test fallback is used when reviewed outline exists but is not adopted."""
        section = OutlineSection(
            section_id="sec-001",
            title="Non-Adopted Section",
            purpose="This is not adopted",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ARBITRATED,  # Not adopted
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        fallback = "# Fallback Outline\n\n## 1. Fallback Section"

        result = get_outline_markdown_for_downstream(reviewed, fallback)

        # Should use fallback because outline is not adopted
        assert result == fallback
        assert "Non-Adopted Section" not in result


class TestOutlineLegacyAdapterClass:
    """Test OutlineLegacyAdapter class."""

    def test_adapter_with_adopted_outline(self):
        """Test adapter returns adopted outline markdown."""
        section = OutlineSection(
            section_id="sec-001",
            title="Adapter Section",
            purpose="Test adapter",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ADOPTED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        adapter = OutlineLegacyAdapter(
            reviewed_outline=reviewed,
            fallback_markdown="# Fallback",
        )

        assert adapter.has_adopted_outline() is True
        markdown = adapter.get_markdown()
        assert "Adapter Section" in markdown
        assert "Fallback" not in markdown

    def test_adapter_without_adopted_outline(self):
        """Test adapter returns fallback when no adopted outline."""
        adapter = OutlineLegacyAdapter(
            reviewed_outline=None,
            fallback_markdown="# Fallback Outline",
        )

        assert adapter.has_adopted_outline() is False
        markdown = adapter.get_markdown()
        assert markdown == "# Fallback Outline"

    def test_adapter_from_workspace_with_file(self, tmp_path: Path):
        """Test loading adapter from workspace with reviewed outline file."""
        # Create reviewed outline file
        section = OutlineSection(
            section_id="sec-001",
            title="Workspace Section",
            purpose="From workspace",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ADOPTED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        reviewed_outline_path = tmp_path / "test_project_reviewed_outline.json"
        reviewed_outline_path.write_text(
            json.dumps(reviewed.to_dict()),
            encoding="utf-8",
        )

        adapter = OutlineLegacyAdapter.from_workspace(
            workspace_path=str(tmp_path),
            project_name="test_project",
            legacy_markdown="# Fallback",
        )

        assert adapter.has_adopted_outline() is True
        markdown = adapter.get_markdown()
        assert "Workspace Section" in markdown

    def test_adapter_from_workspace_without_file(self, tmp_path: Path):
        """Test loading adapter from workspace without reviewed outline file."""
        adapter = OutlineLegacyAdapter.from_workspace(
            workspace_path=str(tmp_path),
            project_name="nonexistent_project",
            legacy_markdown="# Fallback Markdown",
        )

        assert adapter.has_adopted_outline() is False
        markdown = adapter.get_markdown()
        assert markdown == "# Fallback Markdown"


class TestNoSilentOverwrite:
    """Test that there is no silent overwrite before adopt."""

    def test_outline_not_adopted_without_explicit_call(self):
        """Test that outline status doesn't change without explicit adopt."""
        section = OutlineSection(
            section_id="sec-001",
            title="Test Section",
            purpose="Test",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )

        # Status should remain DRAFT without explicit adopt
        assert outline.review_status == ReviewStatus.DRAFT

        # with_critiques should change status to CRITIQUED
        critique = OutlineCritique(
            critique_id="crit-001",
            created_at="2024-01-01T00:00:00",
            created_by="model-001",
            target_section_id=None,
            category=CritiqueCategory.MISSING_THEME,
            description="Missing theme",
            severity="high",
            suggested_fix=None,
        )
        with_critiques = outline.with_critiques([critique])
        assert with_critiques.review_status == ReviewStatus.CRITIQUED

        # Original should still be DRAFT
        assert outline.review_status == ReviewStatus.DRAFT

    def test_adopted_outline_is_immutable(self):
        """Test that adopted outline cannot be silently modified."""
        section = OutlineSection(
            section_id="sec-001",
            title="Test Section",
            purpose="Test",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.ARBITRATED,
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )

        # Adopt creates new object
        from outline.arbitration import adopt_outline

        arb_result = OutlineArbitrationResult(
            result_id="arb-001",
            created_at="2024-01-01T00:00:00",
            created_from_job_id="job-001",
            outline_id="outline-001",
            arbitrations=[],
            accepted_critiques=[],
            rejected_critiques=[],
            deferred_critiques=[],
            modified_sections=[],
        )

        reviewed = adopt_outline(
            outline=outline,
            arbitration_result=arb_result,
            job_id="job-001",
            adopted_by="user-001",
        )

        # Reviewed outline should be ADOPTED
        assert reviewed.outline.review_status == ReviewStatus.ADOPTED

        # Original outline should still be ARBITRATED
        assert outline.review_status == ReviewStatus.ARBITRATED


class TestFallbackToLegacyMarkdown:
    """Test fallback to legacy markdown when adopted outline is absent."""

    def test_fallback_when_adopted_outline_missing(self):
        """Test that legacy markdown is used when adopted outline is missing."""
        fallback = "# Legacy Outline\n\n## 1. Legacy Section\n\nLegacy content"

        adapter = OutlineLegacyAdapter(
            reviewed_outline=None,
            fallback_markdown=fallback,
        )

        markdown = adapter.get_markdown()

        assert "Legacy Outline" in markdown
        assert "Legacy Section" in markdown

    def test_fallback_when_adopted_outline_not_adopted(self):
        """Test fallback when outline exists but is not in ADOPTED status."""
        section = OutlineSection(
            section_id="sec-001",
            title="Draft Section",
            purpose="Still in draft",
            supporting_summary_refs=[],
            children=[],
        )
        outline = OutlineDocument(
            artifact_type="outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            outline_id="outline-001",
            outline_version="v1",
            source_summary_hashes=[],
            generator_model="gpt-4",
            review_status=ReviewStatus.DRAFT,  # Not adopted
            sections=[section],
            critiques=[],
            arbitration_result_id=None,
        )
        reviewed = ReviewedOutlineDocument(
            artifact_type="reviewed_outline_document",
            artifact_version="v1",
            created_from_job_id="job-001",
            created_at="2024-01-01T00:00:00",
            original_outline_id="outline-001",
            reviewed_outline_id="reviewed-001",
            outline=outline,
            adopted_at="2024-01-01T00:00:00",
            adopted_by="user-001",
        )

        fallback = "# Legacy Outline\n\n## 1. Legacy Section"

        adapter = OutlineLegacyAdapter(
            reviewed_outline=reviewed,
            fallback_markdown=fallback,
        )

        # Should use fallback because outline is not adopted
        markdown = adapter.get_markdown()
        assert markdown == fallback
        assert "Draft Section" not in markdown


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

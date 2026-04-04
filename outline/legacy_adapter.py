"""Legacy adapter for Week 5.

Converts ReviewedOutlineDocument back to markdown for downstream consumption.
Provides thin compatibility layer between JSON-first outlines and legacy markdown-based workflow.
"""

from __future__ import annotations

from typing import Optional

from outline.models import OutlineDocument, ReviewedOutlineDocument, ReviewStatus


def reviewed_outline_to_markdown(reviewed_outline: ReviewedOutlineDocument) -> str:
    """Convert a reviewed outline document to markdown.

    This is the reverse of parsing - takes the canonical JSON representation
    and projects it to markdown for downstream consumption by legacy workflow.

    Args:
        reviewed_outline: The reviewed outline document to convert

    Returns:
        Markdown string representation of the outline
    """
    return reviewed_outline.outline.to_markdown()


def outline_document_to_markdown(outline: OutlineDocument) -> str:
    """Convert an outline document to markdown.

    Args:
        outline: The outline document to convert

    Returns:
        Markdown string representation of the outline
    """
    return outline.to_markdown()


def is_reviewed_outline_adopted(reviewed_outline: ReviewedOutlineDocument) -> bool:
    """Check if a reviewed outline has been explicitly adopted.

    Args:
        reviewed_outline: The reviewed outline document to check

    Returns:
        True if the outline has been adopted, False otherwise
    """
    return reviewed_outline.outline.review_status == ReviewStatus.ADOPTED


def get_outline_markdown_for_downstream(
    reviewed_outline: Optional[ReviewedOutlineDocument],
    fallback_markdown: str,
) -> str:
    """Get markdown for downstream consumption with proper fallback.

    Priority:
    1. If reviewed_outline exists and is adopted, use it
    2. Otherwise, use fallback markdown

    Args:
        reviewed_outline: Optional reviewed outline document
        fallback_markdown: Fallback markdown if no adopted outline

    Returns:
        Markdown string for downstream consumption
    """
    if reviewed_outline is not None and is_reviewed_outline_adopted(reviewed_outline):
        return reviewed_outline_to_markdown(reviewed_outline)
    return fallback_markdown


class OutlineLegacyAdapter:
    """Adapter for converting between JSON-first outlines and legacy markdown.

    Provides a thin compatibility layer that allows the new JSON-first
    outline system to work with existing markdown-based workflows.
    """

    def __init__(
        self,
        reviewed_outline: Optional[ReviewedOutlineDocument] = None,
        fallback_markdown: str = "",
    ):
        self.reviewed_outline = reviewed_outline
        self.fallback_markdown = fallback_markdown

    def get_markdown(self) -> str:
        """Get markdown representation, preferring adopted reviewed outline."""
        return get_outline_markdown_for_downstream(
            self.reviewed_outline,
            self.fallback_markdown,
        )

    def has_adopted_outline(self) -> bool:
        """Check if an adopted reviewed outline is available."""
        return self.reviewed_outline is not None and is_reviewed_outline_adopted(
            self.reviewed_outline
        )

    @classmethod
    def from_workspace(
        cls,
        workspace_path: str,
        project_name: str,
        legacy_markdown: str = "",
    ) -> "OutlineLegacyAdapter":
        """Create adapter from workspace, loading reviewed outline if available.

        Args:
            workspace_path: Path to the workspace
            project_name: Name of the project
            legacy_markdown: Fallback markdown content

        Returns:
            Configured OutlineLegacyAdapter
        """
        import json
        import os

        reviewed_outline_path = os.path.join(
            workspace_path,
            f"{project_name}_reviewed_outline.json",
        )

        reviewed_outline: Optional[ReviewedOutlineDocument] = None
        if os.path.exists(reviewed_outline_path):
            try:
                with open(reviewed_outline_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                reviewed_outline = ReviewedOutlineDocument.from_dict(data)
            except Exception:
                # If loading fails, we'll use fallback
                pass

        return cls(
            reviewed_outline=reviewed_outline,
            fallback_markdown=legacy_markdown,
        )

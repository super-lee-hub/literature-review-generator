"""Outline generator for Week 5.

Converts markdown outlines or generates new ones into JSON-first OutlineDocument format.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from outline.models import (
    OutlineDocument,
    OutlineSection,
    ReviewStatus,
)


def _compute_hash(data: Any) -> str:
    """Compute a stable hash for data."""
    content = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _extract_summary_refs_from_text(text: str) -> List[str]:
    """Extract summary references from text.
    
    Looks for patterns like [ref:paper_id] or citations.
    """
    refs = []
    # Pattern for [ref:xxx] format
    ref_pattern = r'\[ref:([^\]]+)\]'
    refs.extend(re.findall(ref_pattern, text))
    return refs


def _parse_markdown_outline(
    markdown_text: str,
    job_id: str,
    generator_model: str,
    source_summary_hashes: List[str],
) -> OutlineDocument:
    """Parse markdown outline into OutlineDocument.
    
    This is the migration path from legacy markdown to JSON-first.
    """
    lines = markdown_text.split('\n')
    
    sections: List[OutlineSection] = []
    current_section: Optional[OutlineSection] = None
    section_stack: List[OutlineSection] = []
    
    section_pattern = re.compile(r'^(#{2,4})\s+(?:\d+\.\s*)?(.+)$')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        match = section_pattern.match(line)
        if match:
            hashes = match.group(1)
            title = match.group(2).strip()
            level = len(hashes)
            
            section_id = f"sec_{len(sections)}_{_compute_hash(title)[:8]}"
            
            # Extract purpose from following lines if available
            purpose = ""
            
            section = OutlineSection(
                section_id=section_id,
                title=title,
                purpose=purpose,
                supporting_summary_refs=[],
                children=[],
            )
            
            if level == 2:
                # Top-level section
                sections.append(section)
                section_stack = [section]
            elif level == 3 and section_stack:
                # Child section
                parent = section_stack[0] if len(section_stack) >= 1 else section_stack[-1]
                # Create new section with updated children
                new_children = list(parent.children) + [section]
                updated_parent = OutlineSection(
                    section_id=parent.section_id,
                    title=parent.title,
                    purpose=parent.purpose,
                    supporting_summary_refs=parent.supporting_summary_refs,
                    children=new_children,
                )
                # Replace in sections list
                if parent in sections:
                    idx = sections.index(parent)
                    sections[idx] = updated_parent
                section_stack = [updated_parent, section]
            elif level == 4 and len(section_stack) >= 2:
                # Grandchild section
                grandparent = section_stack[0]
                parent = section_stack[1]
                new_children = list(parent.children) + [section]
                updated_parent = OutlineSection(
                    section_id=parent.section_id,
                    title=parent.title,
                    purpose=parent.purpose,
                    supporting_summary_refs=parent.supporting_summary_refs,
                    children=new_children,
                )
                # Update grandparent's children
                new_grandchildren = [
                    updated_parent if c.section_id == parent.section_id else c
                    for c in grandparent.children
                ]
                if parent not in grandparent.children:
                    new_grandchildren.append(updated_parent)
                updated_grandparent = OutlineSection(
                    section_id=grandparent.section_id,
                    title=grandparent.title,
                    purpose=grandparent.purpose,
                    supporting_summary_refs=grandparent.supporting_summary_refs,
                    children=new_grandchildren,
                )
                # Replace in sections list
                if grandparent in sections:
                    idx = sections.index(grandparent)
                    sections[idx] = updated_grandparent
    
    return OutlineDocument(
        artifact_type="outline_document",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=datetime.now().isoformat(),
        outline_id=str(uuid.uuid4()),
        outline_version="v1",
        source_summary_hashes=source_summary_hashes,
        generator_model=generator_model,
        review_status=ReviewStatus.DRAFT,
        sections=sections,
        critiques=[],
        arbitration_result_id=None,
        metadata={
            "parsed_from_markdown": True,
            "original_markdown_length": len(markdown_text),
        },
    )


def create_outline_from_markdown(
    markdown_text: str,
    job_id: str,
    generator_model: str,
    summaries: Sequence[Dict[str, Any]],
) -> OutlineDocument:
    """Create an OutlineDocument from markdown text.
    
    This is the migration path from legacy markdown outlines.
    """
    # Compute hashes for source summaries
    source_summary_hashes = [_compute_hash(s) for s in summaries]
    
    return _parse_markdown_outline(
        markdown_text=markdown_text,
        job_id=job_id,
        generator_model=generator_model,
        source_summary_hashes=source_summary_hashes,
    )


def create_outline_from_sections(
    sections_data: List[Dict[str, Any]],
    job_id: str,
    generator_model: str,
    summaries: Sequence[Dict[str, Any]],
) -> OutlineDocument:
    """Create an OutlineDocument from structured section data.
    
    This is for new outline generation (not from markdown).
    """
    source_summary_hashes = [_compute_hash(s) for s in summaries]
    
    def build_section(data: Dict[str, Any]) -> OutlineSection:
        children_data = data.get("children", [])
        children = [build_section(c) for c in children_data]
        
        return OutlineSection(
            section_id=data.get("section_id", f"sec_{_compute_hash(data.get('title', ''))[:8]}"),
            title=data.get("title", ""),
            purpose=data.get("purpose", ""),
            supporting_summary_refs=data.get("supporting_summary_refs", []),
            children=children,
        )
    
    sections = [build_section(s) for s in sections_data]
    
    return OutlineDocument(
        artifact_type="outline_document",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=datetime.now().isoformat(),
        outline_id=str(uuid.uuid4()),
        outline_version="v1",
        source_summary_hashes=source_summary_hashes,
        generator_model=generator_model,
        review_status=ReviewStatus.DRAFT,
        sections=sections,
        critiques=[],
        arbitration_result_id=None,
        metadata={
            "created_from_sections": True,
            "section_count": len(sections),
        },
    )


class OutlineGenerator:
    """Generator for creating and managing JSON-first outlines."""
    
    def __init__(
        self,
        job_id: str,
        generator_model: str,
        summaries: Sequence[Dict[str, Any]],
    ):
        self.job_id = job_id
        self.generator_model = generator_model
        self.summaries = summaries
    
    def from_markdown(self, markdown_text: str) -> OutlineDocument:
        """Create outline from markdown."""
        return create_outline_from_markdown(
            markdown_text=markdown_text,
            job_id=self.job_id,
            generator_model=self.generator_model,
            summaries=self.summaries,
        )
    
    def from_sections(self, sections_data: List[Dict[str, Any]]) -> OutlineDocument:
        """Create outline from structured sections."""
        return create_outline_from_sections(
            sections_data=sections_data,
            job_id=self.job_id,
            generator_model=self.generator_model,
            summaries=self.summaries,
        )


def run_outline_generation(
    markdown_text: Optional[str],
    sections_data: Optional[List[Dict[str, Any]]],
    job_id: str,
    generator_model: str,
    summaries: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Week 5 entry point for outline generation.
    
    Creates JSON-first outline from markdown or structured sections.
    """
    generator = OutlineGenerator(
        job_id=job_id,
        generator_model=generator_model,
        summaries=summaries,
    )
    
    if sections_data:
        outline = generator.from_sections(sections_data)
    elif markdown_text:
        outline = generator.from_markdown(markdown_text)
    else:
        raise ValueError("Either markdown_text or sections_data must be provided")
    
    return {
        "week5_outline_generation": True,
        "outline": outline.to_dict(),
    }

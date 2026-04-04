from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from services.job_workspace import utc_now_iso


@dataclass(frozen=True)
class ReviewDraftV1:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    draft_identity: Dict[str, Any]
    generation_context: Dict[str, Any]
    content: Dict[str, Any]
    projections: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructuredCitation:
    local_ref_id: str
    paper_id: Optional[str] = None
    paper_key: Optional[str] = None
    raw_text: str = ""
    mode: str = "parenthetical"
    locator: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewBlock:
    block_id: str
    block_kind: str
    block_order: int
    text: str
    anchor_text: str = ""
    anchor_hash: str = ""
    citations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewSection:
    section_number: int
    section_title: str
    blocks: List[ReviewBlock]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_number": self.section_number,
            "section_title": self.section_title,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class ReviewDraftV2:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    draft_identity: Dict[str, Any]
    generation_context: Dict[str, Any]
    content: Dict[str, Any]
    projections: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "draft_identity": self.draft_identity,
            "generation_context": self.generation_context,
            "content": {
                "sections": [
                    section.to_dict() for section in self.content.get("sections", [])
                ],
                "references": self.content.get("references", []),
            },
            "projections": self.projections,
        }


def build_review_draft_v1(
    *,
    job_id: str,
    project_name: str,
    draft_id: str,
    outline_artifact_id: str,
    outline_source_path: str,
    summary_file: str,
    review_word_path: str,
    sections: Sequence[Mapping[str, Any]],
    references: Sequence[str],
    generation_mode: str,
) -> ReviewDraftV1:
    normalized_sections = [
        {
            "section_number": int(section.get("section_number") or 0),
            "section_title": str(section.get("section_title") or "").strip(),
            "content": str(section.get("content") or "").strip(),
        }
        for section in sections
    ]
    normalized_references = [str(reference).strip() for reference in references if str(reference).strip()]

    return ReviewDraftV1(
        artifact_type="review_draft",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        draft_identity={
            "draft_id": draft_id,
            "project_name": project_name,
            "scope": "full_review",
        },
        generation_context={
            "generation_mode": generation_mode,
            "outline_artifact_id": outline_artifact_id,
            "outline_source_path": outline_source_path,
            "summary_file": summary_file,
            "section_count": len(normalized_sections),
        },
        content={
            "sections": normalized_sections,
            "references": normalized_references,
        },
        projections={
            "docx_path": review_word_path,
        },
    )


def _match_citation_to_paper(
    citation_token: str,
    paper_key_to_info: Dict[str, Dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """Match a citation token to a paper using heuristics.
    
    Returns:
        Tuple of (paper_id, paper_key)
    """
    citation_lower = citation_token.lower()
    for paper_key, paper_data in paper_key_to_info.items():
        # Check if author names from paper appear in citation
        authors = paper_data.get('authors', [])
        year = paper_data.get('year', '')
        title = paper_data.get('title', '')
        
        # Check for author + year match
        author_matches = any(author.lower() in citation_lower for author in authors)
        year_match = str(year) in citation_lower if year else False
        
        if author_matches and year_match:
            return paper_data.get('paper_id', paper_key), paper_key
        
        # Fallback: check for title keywords
        if title and title.lower() in citation_lower:
            return paper_data.get('paper_id', paper_key), paper_key
    
    return None, None


def _extract_citations_from_text(
    text: str, 
    block_id: str, 
    paper_key_to_info: Optional[Dict[str, Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """Extract structured citations from text content using APA-style pattern matching."""
    citations: List[Dict[str, Any]] = []
    paper_key_to_info = paper_key_to_info or {}
    
    # APA-style citation patterns: (Author, Year) or (Author et al., Year) or Author (Year)
    parenthetical_pattern = r'\([^)]+,\s*\d{4}(?:\s*[;,]\s*[^)]+)*\)'
    narrative_pattern = r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*\(\s*\d{4}\s*\)'
    
    # Find all parenthetical citations
    parenthetical_matches = re.finditer(parenthetical_pattern, text)
    for idx, match in enumerate(parenthetical_matches, start=1):
        local_ref_id = f"{block_id}_cite_p{idx}"
        raw_text = match.group(0)
        
        # Try to match to paper info if available
        paper_id = None
        paper_key = None
        if paper_key_to_info:
            paper_id, paper_key = _match_citation_to_paper(raw_text, paper_key_to_info)
        
        citation = StructuredCitation(
            local_ref_id=local_ref_id,
            paper_key=paper_key,
            paper_id=paper_id,
            raw_text=raw_text,
            mode="parenthetical",
            locator=None
        )
        citations.append(citation.to_dict())
    
    # Find all narrative citations
    narrative_matches = re.finditer(narrative_pattern, text)
    for idx, match in enumerate(narrative_matches, start=len(citations) + 1):
        local_ref_id = f"{block_id}_cite_n{idx}"
        raw_text = match.group(0)
        
        # Try to match to paper info if available
        paper_id = None
        paper_key = None
        if paper_key_to_info:
            paper_id, paper_key = _match_citation_to_paper(raw_text, paper_key_to_info)
        
        citation = StructuredCitation(
            local_ref_id=local_ref_id,
            paper_key=paper_key,
            paper_id=paper_id,
            raw_text=raw_text,
            mode="narrative",
            locator=None
        )
        citations.append(citation.to_dict())
    
    return citations


def _parse_section_into_blocks(
    section_number: int, 
    section_title: str, 
    content: str,
    paper_key_to_info: Optional[Dict[str, Dict[str, Any]]] = None
) -> List[ReviewBlock]:
    """Parse section content into blocks (paragraphs as minimal blocks)."""
    blocks: List[ReviewBlock] = []
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]

    for order, para in enumerate(paragraphs, start=1):
        block_id = f"s{section_number}_b{order}"
        anchor_text = para[:80] if len(para) <= 80 else para[:80] + "..."
        # 生成 anchor_hash 使用 SHA256 的前 8 个字符
        anchor_hash = hashlib.sha256(para.encode('utf-8')).hexdigest()[:8]
        
        # Extract structured citations from paragraph text
        citations = _extract_citations_from_text(para, block_id, paper_key_to_info)
        
        blocks.append(ReviewBlock(
            block_id=block_id,
            block_kind="paragraph",
            block_order=order,
            text=para,
            anchor_text=anchor_text,
            anchor_hash=anchor_hash,
            citations=citations,
        ))

    return blocks


def build_review_draft_v2(
    *,
    job_id: str,
    project_name: str,
    draft_id: str,
    outline_artifact_id: str,
    outline_source_path: str,
    summary_file: str,
    review_word_path: str,
    sections: Sequence[Mapping[str, Any]],
    references: Sequence[str],
    generation_mode: str,
    paper_summaries: Optional[List[Dict[str, Any]]] = None,
) -> ReviewDraftV2:
    # Build paper key to info mapping from summaries if provided
    paper_key_to_info: Dict[str, Dict[str, Any]] = {}
    if paper_summaries:
        for summary in paper_summaries:
            paper_info = summary.get('paper_info', {})
            title = paper_info.get('title', '')
            if title:
                paper_key = title.lower()
                paper_key_to_info[paper_key] = {
                    'paper_id': paper_key,
                    'paper_key': paper_key,
                    'title': title,
                    'authors': paper_info.get('authors', []),
                    'year': paper_info.get('year', ''),
                }
    
    normalized_sections: List[ReviewSection] = []
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        content = str(section.get("content") or "").strip()
        blocks = _parse_section_into_blocks(section_number, section_title, content, paper_key_to_info)
        normalized_sections.append(ReviewSection(
            section_number=section_number,
            section_title=section_title,
            blocks=blocks,
        ))

    normalized_references = [str(reference).strip() for reference in references if str(reference).strip()]

    return ReviewDraftV2(
        artifact_type="review_draft",
        artifact_version="v2",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        draft_identity={
            "draft_id": draft_id,
            "project_name": project_name,
            "scope": "full_review",
        },
        generation_context={
            "generation_mode": generation_mode,
            "outline_artifact_id": outline_artifact_id,
            "outline_source_path": outline_source_path,
            "summary_file": summary_file,
            "section_count": len(normalized_sections),
        },
        content={
            "sections": normalized_sections,
            "references": normalized_references,
        },
        projections={
            "docx_path": review_word_path,
        },
    )

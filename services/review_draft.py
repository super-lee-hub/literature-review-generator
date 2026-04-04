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
    citation_token: str
    paper_id: Optional[str] = None
    paper_key: Optional[str] = None
    raw_text: str = ""
    mode: str = "parenthetical"
    locator: Optional[str] = None
    block_id: str = ""
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source_type: str = "legacy_regex"

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
    """Extract structured citations from text content using APA-style pattern matching and [[cite:]] syntax."""
    citations: List[Dict[str, Any]] = []
    paper_key_to_info = paper_key_to_info or {}
    
    # 优先解析新的 [[cite:]] 语法
    cite_pattern = r'\[\[cite:([^|\]]+)(?:\|([^\]]+))*\]\]'
    cite_matches = re.finditer(cite_pattern, text)
    
    for idx, match in enumerate(cite_matches, start=1):
        local_ref_id = f"{block_id}_cite_t{idx}"
        raw_text = match.group(0)
        paper_key = match.group(1).strip()
        
        # 解析可选参数
        params = {}
        if match.group(2):
            param_str = match.group(2)
            for param in param_str.split('|'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    params[key.strip()] = value.strip()
        
        # 获取参数值
        mode = params.get('mode', 'parenthetical')
        locator = params.get('locator')
        prefix = params.get('prefix', '')
        suffix = params.get('suffix', '')
        suppress_author = params.get('suppress_author', 'false') == 'true'
        
        # 尝试匹配到paper info
        paper_id = None
        if paper_key_to_info:
            # 查找匹配的paper_key
            for key, info in paper_key_to_info.items():
                if info.get('paper_key') == paper_key or info.get('paper_id') == paper_key:
                    paper_id = info.get('paper_id', paper_key)
                    break
        
        citation = StructuredCitation(
            local_ref_id=local_ref_id,
            citation_token=raw_text,
            paper_key=paper_key,
            paper_id=paper_id,
            raw_text=raw_text,
            mode=mode,
            locator=locator,
            block_id=block_id,
            source_type="legacy_regex"
        )
        citations.append(citation.to_dict())
    
    # 兼容旧的APA-style citation patterns（作为fallback）
    # 简化的括号引用模式，确保能匹配 (Author A, 2023) 这种格式
    parenthetical_pattern = r'\([^)]+,\s*\d{4}[^)]*\)'
    narrative_pattern = r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*\(\s*\d{4}\s*\)'
    
    # Find all parenthetical citations
    parenthetical_matches = list(re.finditer(parenthetical_pattern, text))
    for idx, match in enumerate(parenthetical_matches, start=len(citations) + 1):
        local_ref_id = f"{block_id}_cite_p{idx}"
        raw_text = match.group(0)
        
        # Try to match to paper info if available
        paper_id = None
        paper_key = None
        if paper_key_to_info:
            paper_id, paper_key = _match_citation_to_paper(raw_text, paper_key_to_info)
        
        citation = StructuredCitation(
            local_ref_id=local_ref_id,
            citation_token=raw_text,
            paper_key=paper_key,
            paper_id=paper_id,
            raw_text=raw_text,
            mode="parenthetical",
            locator=None,
            block_id=block_id,
            source_type="legacy_regex"
        )
        citations.append(citation.to_dict())
    
    # Find all narrative citations
    narrative_matches = list(re.finditer(narrative_pattern, text))
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
            citation_token=raw_text,
            paper_key=paper_key,
            paper_id=paper_id,
            raw_text=raw_text,
            mode="narrative",
            locator=None,
            block_id=block_id,
            source_type="legacy_regex"
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


def _normalize_block_citations(citations: List[Mapping[str, Any]], block_id: str) -> List[Dict[str, Any]]:
    """Normalize block citations to canonical structure."""
    normalized: List[Dict[str, Any]] = []
    for idx, citation in enumerate(citations, start=1):
        # Extract fields with fallbacks
        local_ref_id = citation.get('local_ref_id', f"{block_id}_cite_{idx}")
        citation_token = citation.get('citation_token', citation.get('raw_text', citation.get('text', '')))
        paper_id = citation.get('paper_id')
        paper_key = citation.get('paper_key', paper_id)
        raw_text = citation.get('raw_text', citation_token)
        mode = citation.get('mode', 'parenthetical')
        locator = citation.get('locator')
        span_start = citation.get('span_start')
        span_end = citation.get('span_end')
        source_type = citation.get('source_type', 'structured_block')
        
        # Create normalized citation
        normalized.append({
            'local_ref_id': local_ref_id,
            'citation_token': citation_token,
            'paper_id': paper_id,
            'paper_key': paper_key,
            'raw_text': raw_text,
            'mode': mode,
            'locator': locator,
            'block_id': block_id,
            'span_start': span_start,
            'span_end': span_end,
            'source_type': source_type,
        })
    return normalized

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
        from main import get_paper_key
        for summary in paper_summaries:
            paper_info = summary.get('paper_info', {})
            # 使用get_paper_key函数生成一致的paper_key
            paper_key = get_paper_key(paper_info)
            paper_key_to_info[paper_key] = {
                'paper_id': paper_key,
                'paper_key': paper_key,
                'title': paper_info.get('title', ''),
                'authors': paper_info.get('authors', []),
                'year': paper_info.get('year', ''),
            }
    
    normalized_sections: List[ReviewSection] = []
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        content = str(section.get("content") or "").strip()
        
        # Check if section already has blocks with citations (new input mode)
        existing_blocks = section.get("blocks", [])
        if existing_blocks:
            # Use existing blocks and normalize citations
            blocks: List[ReviewBlock] = []
            for block_idx, block_data in enumerate(existing_blocks, start=1):
                block_id = block_data.get('block_id', f"s{section_number}_b{block_idx}")
                block_kind = block_data.get('block_kind', 'paragraph')
                block_order = block_data.get('block_order', block_idx)
                text = str(block_data.get('text', '')).strip()
                anchor_text = block_data.get('anchor_text', text[:80] if len(text) <= 80 else text[:80] + "...")
                anchor_hash = block_data.get('anchor_hash', hashlib.sha256(text.encode('utf-8')).hexdigest()[:8])
                
                # Normalize citations if they exist
                citations = block_data.get('citations', [])
                if citations:
                    # 如果有结构化 citations，直接标准化
                    normalized_citations = _normalize_block_citations(citations, block_id)
                else:
                    # 如果没有结构化 citations，尝试从文本中提取
                    normalized_citations = _extract_citations_from_text(text, block_id, paper_key_to_info)
                
                blocks.append(ReviewBlock(
                    block_id=block_id,
                    block_kind=block_kind,
                    block_order=block_order,
                    text=text,
                    anchor_text=anchor_text,
                    anchor_hash=anchor_hash,
                    citations=normalized_citations,
                ))
        else:
            # Old input mode: parse from content
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

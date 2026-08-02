from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from services.citation_ref_catalog import extract_ref_ids_from_token, resolve_ref_id
from services.job_workspace import utc_now_iso
from services.sentence_segmenter import build_sentence_span_map


@dataclass(frozen=True)
class StructuredCitation:
    local_ref_id: str
    citation_token: str
    ref_id: Optional[str] = None
    paper_id: Optional[str] = None
    canonical_paper_key: Optional[str] = None
    paper_key: Optional[str] = None
    raw_text: str = ""
    mode: str = "parenthetical"
    locator: Optional[str] = None
    block_id: str = ""
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source_type: str = "structured_ref"
    warning: Optional[str] = None

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
    block_source: str = "model_generated"
    span_map: Dict[str, Any] = field(default_factory=dict)

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
class ReviewDraft:
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


def _extract_citations_from_text(
    text: str,
    block_id: str,
    *,
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []

    for token_match in re.finditer(r"\[\[cite(?:_ref)?:[^\]]+\]\]", text):
        token = token_match.group(0)
        if not extract_ref_ids_from_token(token):
            raise ValueError(
                f"Review block {block_id} contains a non-structured citation token: {token}"
            )

    ref_pattern = r"\[\[cite_ref:([^\]]+)\]\]"

    structured_ref_count = 0
    for match in re.finditer(ref_pattern, text):
        raw_text = match.group(0)
        ref_ids = extract_ref_ids_from_token(raw_text)
        if not ref_ids:
            raise ValueError(
                f"Invalid structured citation token in review block {block_id}: {raw_text}"
            )
        for ref_id in ref_ids:
            structured_ref_count += 1
            entry = resolve_ref_id(citation_ref_catalog, ref_id)
            warning = None if entry else f"unresolved citation ref id: {ref_id}"
            paper_id = str(entry.get("paper_id") or "").strip() if entry else None
            canonical_key = str(entry.get("canonical_paper_key") or paper_id or "").strip() if entry else None

            citations.append(StructuredCitation(
                local_ref_id=f"{block_id}_cite_r{structured_ref_count}",
                citation_token=raw_text,
                ref_id=ref_id,
                paper_id=paper_id,
                canonical_paper_key=canonical_key,
                paper_key=canonical_key,
                raw_text=raw_text,
                block_id=block_id,
                span_start=match.start(),
                span_end=match.end(),
                source_type="structured_ref" if entry else "unresolved_ref",
                warning=warning,
            ).to_dict())

    return citations


def _build_block_span_map(text: str) -> Dict[str, Any]:
    return build_sentence_span_map(text)


def _build_anchor_text(text: str) -> str:
    return text[:80] if len(text) <= 80 else text[:80] + "..."


def _build_anchor_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _validate_citation_tokens(text: str, block_id: str) -> None:
    for token_match in re.finditer(r"\[\[cite(?:_ref)?:[^\]]+\]\]", text):
        token = token_match.group(0)
        if not extract_ref_ids_from_token(token):
            raise ValueError(
                f"Review block {block_id} contains a non-structured citation token: {token}"
            )


def _parse_section_into_blocks(
    section_number: int,
    section_title: str,
    content: str,
    *,
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
) -> List[ReviewBlock]:
    blocks: List[ReviewBlock] = []
    paragraphs = [paragraph.strip() for paragraph in content.split("\n\n") if paragraph.strip()]

    for order, paragraph in enumerate(paragraphs, start=1):
        block_id = f"s{section_number}_b{order}"
        anchor_text = _build_anchor_text(paragraph)
        anchor_hash = _build_anchor_hash(paragraph)
        citations = _extract_citations_from_text(
            paragraph,
            block_id,
            citation_ref_catalog=citation_ref_catalog,
        )
        blocks.append(
            ReviewBlock(
                block_id=block_id,
                block_kind="paragraph",
                block_order=order,
                text=paragraph,
                anchor_text=anchor_text,
                anchor_hash=anchor_hash,
                citations=citations,
                block_source="model_generated",
                span_map=_build_block_span_map(paragraph),
            )
        )

    return blocks


def _normalize_block_citations(
    citations: List[Mapping[str, Any]],
    block_id: str,
    *,
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, citation in enumerate(citations, start=1):
        if not isinstance(citation, Mapping):
            raise ValueError(f"Review block {block_id} citation must be an object")

        base_local_ref_id = str(citation.get("local_ref_id") or f"{block_id}_cite_{idx}")
        citation_token = str(
            citation.get("citation_token") or citation.get("raw_text") or ""
        ).strip()
        token_ref_ids = extract_ref_ids_from_token(citation_token)
        explicit_ref_id = str(citation.get("ref_id") or "").strip()
        if citation_token and not token_ref_ids:
            raise ValueError(
                f"Review block {block_id} contains a non-structured citation token: {citation_token}"
            )
        ref_ids = [explicit_ref_id] if explicit_ref_id else token_ref_ids
        if not ref_ids:
            raise ValueError(
                f"Review block {block_id} contains a citation without a structured ref_id"
            )
        if explicit_ref_id and token_ref_ids and explicit_ref_id not in token_ref_ids:
            raise ValueError(
                f"Review block {block_id} citation ref_id does not match its token: {explicit_ref_id}"
            )
        if not citation_token:
            citation_token = f"[[cite_ref:{', '.join(ref_ids)}]]"

        for ref_index, ref_id in enumerate(ref_ids, start=1):
            entry = resolve_ref_id(citation_ref_catalog, ref_id)
            paper_id = str(entry.get("paper_id") or "").strip() if entry else None
            canonical_paper_key = (
                str(entry.get("canonical_paper_key") or paper_id or "").strip()
                if entry
                else None
            )
            local_ref_id = (
                base_local_ref_id
                if len(ref_ids) == 1
                else f"{base_local_ref_id}_{ref_index}"
            )
            normalized.append(
                {
                    "local_ref_id": local_ref_id,
                    "citation_token": citation_token,
                    "ref_id": ref_id,
                    "paper_id": paper_id,
                    "canonical_paper_key": canonical_paper_key,
                    "paper_key": canonical_paper_key,
                    "raw_text": str(citation.get("raw_text") or citation_token),
                    "mode": citation.get("mode", "parenthetical"),
                    "locator": citation.get("locator"),
                    "block_id": block_id,
                    "span_start": citation.get("span_start"),
                    "span_end": citation.get("span_end"),
                    "source_type": "structured_ref" if entry else "unresolved_ref",
                    "warning": None if entry else f"unresolved citation ref id: {ref_id}",
                }
            )
    return normalized


def build_review_draft(
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
    citation_ref_catalog: Optional[Mapping[str, Any]] = None,
    citation_ref_catalog_path: str = "",
    citation_ref_catalog_hash: str = "",
) -> ReviewDraft:
    normalized_sections: List[ReviewSection] = []
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        content = str(section.get("content") or "").strip()

        existing_blocks = section.get("blocks", [])
        if existing_blocks:
            blocks: List[ReviewBlock] = []
            for block_idx, block_data in enumerate(existing_blocks, start=1):
                block_id = block_data.get("block_id", f"s{section_number}_b{block_idx}")
                block_kind = block_data.get("block_kind", "paragraph")
                block_order = block_data.get("block_order", block_idx)
                text = str(block_data.get("text", "")).strip()
                anchor_text = block_data.get("anchor_text", _build_anchor_text(text))
                anchor_hash = block_data.get("anchor_hash", _build_anchor_hash(text))
                citations = block_data.get("citations", [])
                _validate_citation_tokens(text, block_id)
                normalized_citations = (
                    _normalize_block_citations(
                        citations,
                        block_id,
                        citation_ref_catalog=citation_ref_catalog,
                    )
                    if citations
                    else _extract_citations_from_text(
                        text,
                        block_id,
                        citation_ref_catalog=citation_ref_catalog,
                    )
                )
                blocks.append(
                    ReviewBlock(
                        block_id=block_id,
                        block_kind=block_kind,
                        block_order=block_order,
                        text=text,
                        anchor_text=anchor_text,
                        anchor_hash=anchor_hash,
                        citations=normalized_citations,
                        block_source=block_data.get("block_source", "model_generated"),
                        span_map=block_data.get("span_map") or _build_block_span_map(text),
                    )
                )
        else:
            blocks = _parse_section_into_blocks(
                section_number,
                section_title,
                content,
                citation_ref_catalog=citation_ref_catalog,
            )

        normalized_sections.append(
            ReviewSection(
                section_number=section_number,
                section_title=section_title,
                blocks=blocks,
            )
        )

    normalized_references = [str(reference).strip() for reference in references if str(reference).strip()]

    return ReviewDraft(
        artifact_type="review_draft",
        artifact_version="v3",
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
            "citation_ref_catalog_path": citation_ref_catalog_path,
            "citation_ref_catalog_hash": citation_ref_catalog_hash,
        },
        content={
            "sections": normalized_sections,
            "references": normalized_references,
        },
        projections={
            "docx_path": review_word_path,
        },
    )

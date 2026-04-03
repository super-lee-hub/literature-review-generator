from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Sequence

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
class ReviewBlock:
    block_id: str
    block_kind: str
    block_order: int
    text: str
    anchor_text: str = ""
    anchor_hash: str = ""

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


def _parse_section_into_blocks(section_number: int, section_title: str, content: str) -> List[ReviewBlock]:
    """Parse section content into blocks (paragraphs as minimal blocks)."""
    blocks: List[ReviewBlock] = []
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]

    for order, para in enumerate(paragraphs, start=1):
        block_id = f"s{section_number}_b{order}"
        anchor_text = para[:80] if len(para) <= 80 else para[:80] + "..."
        blocks.append(ReviewBlock(
            block_id=block_id,
            block_kind="paragraph",
            block_order=order,
            text=para,
            anchor_text=anchor_text,
            anchor_hash="",
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
) -> ReviewDraftV2:
    normalized_sections: List[ReviewSection] = []
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        content = str(section.get("content") or "").strip()
        blocks = _parse_section_into_blocks(section_number, section_title, content)
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

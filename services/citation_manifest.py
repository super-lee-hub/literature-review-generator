from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Sequence

from services.job_workspace import utc_now_iso


@dataclass(frozen=True)
class CitationManifestV1:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    citations: Sequence[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_citation_manifest_v1(
    *, 
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    citations: Sequence[Dict[str, Any]],
) -> CitationManifestV1:
    normalized_citations = [
        {
            "citation_id": str(citation.get("citation_id") or ""),
            "paper_id": str(citation.get("paper_id") or ""),
            "text": str(citation.get("text") or "").strip(),
            "context": str(citation.get("context") or "").strip(),
            "section_number": int(citation.get("section_number") or 0),
            "section_title": str(citation.get("section_title") or "").strip(),
        }
        for citation in citations
    ]

    return CitationManifestV1(
        artifact_type="citation_manifest",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        citations=normalized_citations,
    )

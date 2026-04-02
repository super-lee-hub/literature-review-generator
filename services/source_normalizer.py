from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, cast

from models import PaperInfo


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _paper_key_from_metadata(paper: Mapping[str, Any]) -> str:
    doi = _safe_text(paper.get("doi"))
    if doi:
        return doi.lower()
    title = _safe_text(paper.get("title")).lower()
    authors = [_safe_text(item).lower() for item in (paper.get("authors") or []) if _safe_text(item)]
    author_key = "_".join(authors[:3]) if authors else "unknown_author"
    return f"{title}_{author_key}" if title else author_key


def fingerprint_pdf_file(pdf_path: str | None) -> str:
    if not pdf_path:
        return ""
    path = os.path.abspath(pdf_path)
    if not os.path.exists(path) or not os.path.isfile(path):
        return hashlib.sha256(path.encode("utf-8")).hexdigest()

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourcePaperDescriptor:
    source_mode: str
    source_paper_id: str
    canonical_paper_key: str
    paper_key_aliases: List[str]
    source_pdf: str
    source_pdf_fingerprint: str
    metadata_confidence: str
    metadata_source_priority_snapshot: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_source_papers(source_mode: str, papers: Iterable[Mapping[str, Any]]) -> List[SourcePaperDescriptor]:
    normalized: List[SourcePaperDescriptor] = []
    for index, paper in enumerate(papers):
        pdf_path = _safe_text(paper.get("pdf_path"))
        canonical_key = _paper_key_from_metadata(paper)
        aliases = [canonical_key]
        title = _safe_text(paper.get("title"))
        doi = _safe_text(paper.get("doi"))
        if title:
            aliases.append(title.lower())
        if doi:
            aliases.append(doi.lower())

        if source_mode == "zotero":
            source_paper_id = doi or title or f"zotero-{index + 1}"
            metadata_confidence = "high" if doi or title else "medium"
            priority = ["zotero_metadata", "attachment_match", "filename"]
        else:
            source_paper_id = pdf_path or f"pdf-{index + 1}"
            metadata_confidence = "medium" if title else "low"
            priority = ["pdf_filename", "pdf_embedded_metadata", "ai_backfill"]

        normalized.append(
            SourcePaperDescriptor(
                source_mode=source_mode,
                source_paper_id=source_paper_id,
                canonical_paper_key=canonical_key,
                paper_key_aliases=list(dict.fromkeys(alias for alias in aliases if alias)),
                source_pdf=pdf_path,
                source_pdf_fingerprint=fingerprint_pdf_file(pdf_path),
                metadata_confidence=metadata_confidence,
                metadata_source_priority_snapshot=priority,
            )
        )
    return normalized


def project_descriptors_to_legacy_papers(
    papers: Iterable[Mapping[str, Any]],
    descriptors: Iterable[SourcePaperDescriptor],
) -> List[PaperInfo]:
    projected: List[PaperInfo] = []
    for paper, descriptor in zip(papers, descriptors):
        enriched = dict(paper)
        enriched["source_mode"] = descriptor.source_mode
        enriched["source_paper_id"] = descriptor.source_paper_id
        enriched["canonical_paper_key"] = descriptor.canonical_paper_key
        enriched["paper_key_aliases"] = list(descriptor.paper_key_aliases)
        enriched["source_pdf"] = descriptor.source_pdf
        enriched["source_pdf_fingerprint"] = descriptor.source_pdf_fingerprint
        enriched["metadata_confidence"] = descriptor.metadata_confidence
        enriched["metadata_source_priority_snapshot"] = list(descriptor.metadata_source_priority_snapshot)
        enriched["source_descriptor"] = descriptor.to_dict()
        projected.append(cast(PaperInfo, enriched))
    return projected

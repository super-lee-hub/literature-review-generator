from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping

from services.paper_identity import build_paper_key, normalize_doi

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class SourceIdentityError(ValueError):
    """Raised when source identities cannot safely form independent work items."""


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _paper_key_from_metadata(paper: Mapping[str, Any]) -> str:
    return build_paper_key(paper)


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


def _identity_fields(
    item: SourcePaperDescriptor | Mapping[str, Any],
) -> tuple[str, str, str]:
    if isinstance(item, SourcePaperDescriptor):
        return (
            _safe_text(item.canonical_paper_key),
            _safe_text(item.source_pdf),
            _safe_text(item.source_pdf_fingerprint).casefold(),
        )
    return (
        _safe_text(item.get("canonical_paper_key")),
        _safe_text(item.get("source_pdf")),
        _safe_text(item.get("source_pdf_fingerprint")).casefold(),
    )


def validate_source_paper_uniqueness(
    papers: Iterable[SourcePaperDescriptor | Mapping[str, Any]],
) -> None:
    """Reject identities that would collapse Stage 1 work or provider call IDs.

    Canonical paper keys are used directly in Stage 1 artifact and provider-call
    identifiers.  PDF content SHA-256 is the physical-source identity, so two
    copies with different filenames must not become independent work items.
    """

    key_positions: dict[str, list[int]] = {}
    key_values: dict[str, str] = {}
    pdf_positions: dict[str, list[int]] = {}
    for index, item in enumerate(papers):
        canonical_key, source_pdf, source_pdf_fingerprint = _identity_fields(item)
        if canonical_key:
            normalized_key = canonical_key.casefold()
            key_positions.setdefault(normalized_key, []).append(index)
            key_values.setdefault(normalized_key, canonical_key)
        if source_pdf_fingerprint and not _SHA256_HEX.fullmatch(source_pdf_fingerprint):
            raise SourceIdentityError(
                f"source_identity_pdf_sha256_invalid:index={index}"
            )
        if source_pdf and source_pdf_fingerprint:
            pdf_positions.setdefault(source_pdf_fingerprint, []).append(index)

    duplicate_keys = [
        (key_values[key], positions)
        for key, positions in key_positions.items()
        if len(positions) > 1
    ]
    if duplicate_keys:
        canonical_key, positions = min(
            duplicate_keys,
            key=lambda item: (item[0].casefold(), item[1]),
        )
        raise SourceIdentityError(
            "source_identity_duplicate_canonical_paper_key:"
            f"{canonical_key}:indexes={','.join(str(index) for index in positions)}"
        )

    duplicate_pdfs = [
        (fingerprint, positions)
        for fingerprint, positions in pdf_positions.items()
        if len(positions) > 1
    ]
    if duplicate_pdfs:
        fingerprint, positions = min(duplicate_pdfs, key=lambda item: (item[0], item[1]))
        raise SourceIdentityError(
            "source_identity_duplicate_pdf_sha256:"
            f"{fingerprint}:indexes={','.join(str(index) for index in positions)}"
        )


def normalize_source_papers(source_mode: str, papers: Iterable[Mapping[str, Any]]) -> List[SourcePaperDescriptor]:
    normalized: List[SourcePaperDescriptor] = []
    for index, paper in enumerate(papers):
        pdf_path = _safe_text(paper.get("pdf_path"))
        canonical_key = _paper_key_from_metadata(paper)
        aliases = [canonical_key]
        title = _safe_text(paper.get("title"))
        doi = normalize_doi(paper.get("doi"))
        if title:
            aliases.append(title.lower())
        if doi:
            aliases.append(doi)

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
    validate_source_paper_uniqueness(normalized)
    return normalized


__all__ = [
    "SourceIdentityError",
    "SourcePaperDescriptor",
    "fingerprint_pdf_file",
    "normalize_source_papers",
    "validate_source_paper_uniqueness",
]

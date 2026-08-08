"""Deterministic semantic closure checks for quarantined repair outputs.

Repair application is allowed to create derived artifacts, but those artifacts
are not promotable until this service proves that the repaired text still has
the same section/block/citation meaning surface and contains no unresolved
repair markers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticRevalidationResult:
    passed: bool
    status: str
    diagnostics: tuple[str, ...]
    section_count: int
    block_count: int
    occurrence_count: int
    mapped_occurrence_count: int
    evidence_hash: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["diagnostics"] = list(self.diagnostics)
        return payload


def run_semantic_revalidation(
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    paper_artifacts: Sequence[Mapping[str, Any]],
    *,
    citation_ref_catalog: Mapping[str, Any] | None = None,
) -> SemanticRevalidationResult:
    diagnostics: set[str] = set()
    content = review_draft.get("content")
    sections = content.get("sections") if isinstance(content, Mapping) else None
    block_ids: set[str] = set()
    block_count = 0
    if not isinstance(sections, list) or not sections:
        diagnostics.add("sections_missing")
        sections = []
    for section_index, section in enumerate(sections, start=1):
        if not isinstance(section, Mapping):
            diagnostics.add(f"section_not_object:{section_index}")
            continue
        if not str(section.get("title") or section.get("heading") or "").strip():
            diagnostics.add(f"section_heading_missing:{section_index}")
        blocks = section.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            diagnostics.add(f"section_blocks_missing:{section_index}")
            continue
        for block_index, block in enumerate(blocks, start=1):
            if not isinstance(block, Mapping):
                diagnostics.add(f"block_not_object:{section_index}:{block_index}")
                continue
            block_id = str(block.get("block_id") or "").strip()
            text = str(block.get("text") or "")
            if not block_id:
                diagnostics.add(f"block_id_missing:{section_index}:{block_index}")
            elif block_id in block_ids:
                diagnostics.add(f"duplicate_block_id:{block_id}")
            else:
                block_ids.add(block_id)
            if not text.strip():
                diagnostics.add(f"block_text_empty:{block_id or f'{section_index}:{block_index}'}")
            if "CITATION_MAPPING_ERROR" in text or "needs manual review" in text:
                diagnostics.add(f"unresolved_repair_marker:{block_id or section_index}")
            block_count += 1

    known_papers: set[str] = set()
    for artifact in paper_artifacts:
        identity = artifact.get("paper_identity")
        if isinstance(identity, Mapping):
            for key in ("canonical_paper_key", "source_paper_id"):
                value = str(identity.get(key) or "").strip()
                if value:
                    known_papers.add(value)
    for entry in (citation_ref_catalog or {}).get("entries", []):
        if isinstance(entry, Mapping):
            for key in ("paper_id", "canonical_paper_key"):
                value = str(entry.get(key) or "").strip()
                if value:
                    known_papers.add(value)
    for field_name in ("paper_entries", "bibliography"):
        for entry in citation_manifest.get(field_name, []) or []:
            if isinstance(entry, Mapping):
                for key in ("paper_id", "paper_key"):
                    value = str(entry.get(key) or "").strip()
                    if value:
                        known_papers.add(value)

    active_refs = {
        str(entry.get("ref_id") or "").strip()
        for entry in (citation_ref_catalog or {}).get("entries", [])
        if isinstance(entry, Mapping)
        and entry.get("status") == "active"
        and str(entry.get("ref_id") or "").strip()
    }
    occurrences = citation_manifest.get("occurrences")
    if not isinstance(occurrences, list):
        diagnostics.add("citation_occurrences_missing")
        occurrences = []
    mapped = 0
    for index, occurrence in enumerate(occurrences, start=1):
        if not isinstance(occurrence, Mapping):
            diagnostics.add(f"citation_occurrence_not_object:{index}")
            continue
        occurrence_id = str(occurrence.get("occurrence_id") or index)
        block_id = str(occurrence.get("block_id") or "").strip()
        ref_id = str(occurrence.get("ref_id") or "").strip()
        paper_id = str(occurrence.get("paper_id") or occurrence.get("paper_key") or "").strip()
        if not block_id or block_id not in block_ids:
            diagnostics.add(f"citation_block_unresolved:{occurrence_id}")
        if not ref_id or (active_refs and ref_id not in active_refs):
            diagnostics.add(f"citation_ref_unresolved:{occurrence_id}")
        if not paper_id or paper_id.lower() == "unknown" or (known_papers and paper_id not in known_papers):
            diagnostics.add(f"citation_paper_unresolved:{occurrence_id}")
        if block_id in block_ids and ref_id and paper_id and paper_id.lower() != "unknown":
            mapped += 1

    ordered = tuple(sorted(diagnostics))
    evidence_hash = _hash(
        {
            "sections": len(sections),
            "blocks": sorted(block_ids),
            "occurrences": occurrences,
            "diagnostics": ordered,
        }
    )
    return SemanticRevalidationResult(
        passed=not ordered,
        status="passed" if not ordered else "blocked",
        diagnostics=ordered,
        section_count=len(sections),
        block_count=block_count,
        occurrence_count=len(occurrences),
        mapped_occurrence_count=mapped,
        evidence_hash=evidence_hash,
    )


__all__ = ["SemanticRevalidationResult", "run_semantic_revalidation"]

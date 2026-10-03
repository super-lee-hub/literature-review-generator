from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

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
    writer_task_id: str = ""
    writer_output_unit_id: str = ""
    writer_task_basis_hash: str = ""
    table_layout_schema_version: str = ""
    table_id: str = ""
    headers: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    allowed_ref_ids: List[str] = field(default_factory=list)
    required_source_context: Dict[str, Any] = field(default_factory=dict)
    source_validation_status: str = ""

    def __post_init__(self) -> None:
        if self.table_layout_schema_version:
            if (
                self.table_layout_schema_version != "writer_table_layout/v1"
                or self.block_kind != "table" or not self.table_id
                or self.text or self.citations or self.writer_task_id or self.writer_output_unit_id
                or re.fullmatch(r"[0-9a-f]{64}", self.writer_task_basis_hash) is None
            ):
                raise ValueError("Native Writer table requires a layout basis and cell-level factual bindings")
            list(iter_review_text_blocks({"blocks": [{
                "block_id": self.block_id, "block_kind": self.block_kind,
                "table_layout_schema_version": self.table_layout_schema_version,
                "table_id": self.table_id, "headers": self.headers, "rows": self.rows,
                "writer_task_basis_hash": self.writer_task_basis_hash,
            }]}))
            return
        binding = (self.writer_task_id, self.writer_output_unit_id, self.writer_task_basis_hash)
        if any(binding) and (
            not all(isinstance(value, str) and value.strip() for value in binding)
            or re.fullmatch(r"[0-9a-f]{64}", self.writer_task_basis_hash) is None
        ):
            raise ValueError("Writer block requires a complete task/unit/basis binding")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if not self.writer_task_id and not self.table_layout_schema_version:
            for name in ("writer_task_id", "writer_output_unit_id", "writer_task_basis_hash"):
                payload.pop(name)
        if self.table_layout_schema_version:
            payload.pop("writer_task_id")
            payload.pop("writer_output_unit_id")
        else:
            for name in ("table_layout_schema_version", "table_id", "headers", "rows"):
                payload.pop(name)
        for name in ("allowed_ref_ids", "required_source_context", "source_validation_status"):
            if not payload[name]:
                payload.pop(name)
        return payload


def iter_review_text_blocks(section: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield paragraphs and factual table cells with their own text offsets and IDs."""
    seen_ids: set[str] = set()
    for block in section.get("blocks", []):
        if not isinstance(block, Mapping):
            raise ValueError("Review section block must be an object")
        if not block.get("table_layout_schema_version"):
            yield block
            continue
        if (
            block.get("table_layout_schema_version") != "writer_table_layout/v1"
            or block.get("block_kind") != "table" or not block.get("table_id")
            or block.get("text") or block.get("citations")
        ):
            raise ValueError("Native Writer table has an invalid container")
        headers, rows = block.get("headers"), block.get("rows")
        if (
            not isinstance(headers, list) or not 1 <= len(headers) <= 16
            or any(not isinstance(header, str) or not header for header in headers)
            or not isinstance(rows, list) or not 1 <= len(rows) <= 512
            or (len(rows) + 1) * len(headers) > 4096
        ):
            raise ValueError("Native Writer table has invalid row or column cardinality")
        row_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("row_id"), str) or not row["row_id"] or row["row_id"] in row_ids:
                raise ValueError("Native Writer table has a missing or duplicate row identity")
            row_ids.add(row["row_id"])
            cells = row.get("cells")
            if not isinstance(cells, list) or len(cells) != len(headers):
                raise ValueError("Native Writer table row has missing or extra cells")
            for cell in cells:
                if not isinstance(cell, Mapping):
                    raise ValueError("Native Writer table cell must be an object")
                block_id = cell.get("block_id")
                if not isinstance(block_id, str) or not block_id or block_id in seen_ids:
                    raise ValueError("Native Writer table has missing or duplicate cell block IDs")
                seen_ids.add(block_id)
                if not isinstance(cell.get("text"), str) or not cell["text"].strip():
                    raise ValueError("Native Writer table cell is empty")
                if cell.get("cell_kind") == "static_label":
                    if cell.get("citations") or "[[cite" in cell["text"] or any(
                        cell.get(name) for name in ("writer_task_id", "writer_output_unit_id", "writer_task_basis_hash")
                    ):
                        raise ValueError("Static Writer table labels cannot contain factual bindings or citations")
                    continue
                if cell.get("cell_kind") != "factual_output_unit" or any(
                    not isinstance(cell.get(name), str) or not cell[name]
                    for name in ("writer_task_id", "writer_output_unit_id", "writer_task_basis_hash")
                ) or cell["writer_task_basis_hash"] != block.get("writer_task_basis_hash"):
                    raise ValueError("Native Writer table factual cell has an incomplete task binding")
                yield {
                    **cell, "block_kind": "paragraph", "block_order": block.get("block_order", 0),
                    "table_id": block["table_id"], "row_id": row["row_id"],
                }


def validate_review_section_writer_scope(section: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Recheck factual units and the locally projected layout before adoption."""
    text_blocks = list(iter_review_text_blocks(section))
    scope = section.get("writer_task_scope")
    has_native_table = any(block.get("table_layout_schema_version") for block in section.get("blocks", []))
    if not scope:
        if has_native_table:
            raise ValueError("Native Writer tables require the bound section task scope")
        return text_blocks
    from services.writer_task_scope import validate_writer_task_output_v1

    validated = validate_writer_task_output_v1(scope, {
        "blocks": [{name: block.get(name) for name in (
            "writer_task_id", "writer_output_unit_id", "writer_task_basis_hash", "text"
        )} for block in text_blocks],
        "task_dispositions": section.get("writer_task_dispositions"),
    })
    if (
        validated.get("scope_status") != "ready"
        or validated.get("usable_for_provider_admission") is not True
        or validated.get("source_authority_status") != "canonical_claim_and_evidence_inventory_verified"
    ):
        raise ValueError("Review section cannot adopt unresolved Writer tasks")
    for block in text_blocks:
        if "citations" in block:
            citations = block["citations"]
            if not isinstance(citations, list):
                raise ValueError("Writer unit citations must be an array")
            _normalize_block_citations(citations, str(block.get("block_id") or ""), text=str(block.get("text") or ""))
    if scope.get("schema_version") == "writer_task_scope/v2":
        from services.writer_table_layout import project_writer_table_layouts_v1

        expected = project_writer_table_layouts_v1(scope, validated)["blocks"]

        def matches(actual, wanted):
            if isinstance(wanted, dict):
                return isinstance(actual, Mapping) and all(key in actual and matches(actual[key], value) for key, value in wanted.items())
            if isinstance(wanted, list):
                return isinstance(actual, list) and len(actual) == len(wanted) and all(matches(left, right) for left, right in zip(actual, wanted))
            return actual == wanted

        if not matches(section.get("blocks", []), expected):
            raise ValueError("Native Writer table projection differs from its bound layout or source units")
    elif has_native_table:
        raise ValueError("Native Writer table requires the table-bound scope version")
    return text_blocks


def find_review_text_block(review_draft: Mapping[str, Any], block_id: str) -> Optional[Dict[str, Any]]:
    """Resolve an actual mutable paragraph or factual cell, never a static label."""
    found = find_review_text_block_location(review_draft, block_id)
    return found[1] if found is not None else None


def find_review_text_block_location(
    review_draft: Mapping[str, Any], block_id: str,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Return a factual repair target and its exact structured text locator."""
    matches = []
    for section_index, section in enumerate(review_draft.get("content", {}).get("sections", [])):
        # Validate native container shape before looking through its mutable rows.
        list(iter_review_text_blocks(section))
        for block_index, block in enumerate(section.get("blocks", [])):
            path = f"content.sections[{section_index}].blocks[{block_index}]"
            if block.get("table_layout_schema_version"):
                candidates = [
                    (f"{path}.rows[{row_index}].cells[{cell_index}].text", cell)
                    for row_index, row in enumerate(block["rows"])
                    for cell_index, cell in enumerate(row["cells"])
                    if cell["cell_kind"] == "factual_output_unit"
                ]
            else:
                candidates = [(f"{path}.text", block)]
            matches.extend(candidate for candidate in candidates if candidate[1].get("block_id") == block_id)
    if len(matches) > 1:
        raise ValueError(f"Review text block identity is duplicated: {block_id}")
    if not matches:
        return None
    if not isinstance(matches[0][1], dict):
        raise ValueError("Review text repair target must be a mutable object")
    return matches[0]


@dataclass(frozen=True)
class ReviewSection:
    section_number: int
    section_title: str
    blocks: List[ReviewBlock]
    writer_task_scope: Dict[str, Any] = field(default_factory=dict)
    writer_task_dispositions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "section_number": self.section_number,
            "section_title": self.section_title,
            "blocks": [block.to_dict() for block in self.blocks],
        }
        if self.writer_task_scope:
            payload["writer_task_scope"] = self.writer_task_scope
            payload["writer_task_dispositions"] = self.writer_task_dispositions
        return payload


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
    text: Optional[str] = None,
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
    if text is not None:
        expected = _extract_citations_from_text(text, block_id, citation_ref_catalog=citation_ref_catalog)
        remaining = list(expected)
        for citation in normalized:
            start, end = citation.get("span_start"), citation.get("span_end")
            if (start is None) != (end is None) or any(
                value is not None and (isinstance(value, bool) or not isinstance(value, int))
                for value in (start, end)
            ):
                raise ValueError(f"Writer unit {block_id} has invalid citation spans")
            match_index = next((index for index, actual in enumerate(remaining) if (
                actual["citation_token"] == citation["citation_token"]
                and actual["ref_id"] == citation["ref_id"]
                and (start is None or (actual["span_start"] == start and actual["span_end"] == end))
            )), None)
            if match_index is None:
                raise ValueError(f"Writer unit {block_id} citation metadata is detached from its text token/ref/span")
            actual = remaining.pop(match_index)
            citation["span_start"], citation["span_end"] = actual["span_start"], actual["span_end"]
        if remaining:
            raise ValueError(f"Writer unit {block_id} citation metadata omits a text token/ref occurrence")
    return normalized


def _normalize_native_table_rows(
    block_data: Mapping[str, Any],
    *,
    citation_ref_catalog: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    rows = []
    for row in block_data["rows"]:
        cells = []
        for cell in row["cells"]:
            normalized = dict(cell)
            if cell["cell_kind"] == "factual_output_unit":
                text, block_id = cell["text"], cell["block_id"]
                _validate_citation_tokens(text, block_id)
                normalized["citations"] = (
                    _normalize_block_citations(cell["citations"], block_id, citation_ref_catalog=citation_ref_catalog, text=text)
                    if cell.get("citations") else _extract_citations_from_text(text, block_id, citation_ref_catalog=citation_ref_catalog)
                )
                normalized["anchor_text"] = _build_anchor_text(text)
                normalized["anchor_hash"] = _build_anchor_hash(text)
                normalized["span_map"] = _build_block_span_map(text)
            cells.append(normalized)
        rows.append({"row_id": row["row_id"], "cells": cells})
    return rows


def build_review_draft(
    *,
    job_id: str,
    project_name: str,
    draft_id: str,
    title: str = "",
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
    outline_artifact_hash: str = "",
    adoption_artifact_id: str = "",
    adoption_artifact_hash: str = "",
    writer_section_artifacts: Sequence[Mapping[str, str]] = (),
) -> ReviewDraft:
    normalized_sections: List[ReviewSection] = []
    for section in sections:
        section_number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        content = str(section.get("content") or "").strip()

        existing_blocks = section.get("blocks", [])
        if any(block.get("table_layout_schema_version") for block in existing_blocks):
            validate_review_section_writer_scope(section)
        if existing_blocks:
            blocks: List[ReviewBlock] = []
            for block_idx, block_data in enumerate(existing_blocks, start=1):
                block_id = block_data.get("block_id", f"s{section_number}_b{block_idx}")
                block_kind = block_data.get("block_kind", "paragraph")
                block_order = block_data.get("block_order", block_idx)
                text = str(block_data.get("text", "")).strip()
                if block_data.get("table_layout_schema_version"):
                    blocks.append(ReviewBlock(
                        block_id=block_id, block_kind=block_kind, block_order=block_order, text="",
                        block_source=block_data.get("block_source", "writer_v3_local_table_projection"),
                        writer_task_basis_hash=block_data.get("writer_task_basis_hash", ""),
                        table_layout_schema_version=block_data["table_layout_schema_version"],
                        table_id=block_data["table_id"], headers=list(block_data["headers"]),
                        rows=_normalize_native_table_rows(block_data, citation_ref_catalog=citation_ref_catalog),
                    ))
                    continue
                anchor_text = block_data.get("anchor_text", _build_anchor_text(text))
                anchor_hash = block_data.get("anchor_hash", _build_anchor_hash(text))
                citations = block_data.get("citations", [])
                _validate_citation_tokens(text, block_id)
                normalized_citations = (
                    _normalize_block_citations(
                        citations,
                        block_id,
                        citation_ref_catalog=citation_ref_catalog,
                        text=text if block_data.get("writer_task_id") else None,
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
                        writer_task_id=block_data.get("writer_task_id", ""),
                        writer_output_unit_id=block_data.get("writer_output_unit_id", ""),
                        writer_task_basis_hash=block_data.get("writer_task_basis_hash", ""),
                        allowed_ref_ids=list(block_data.get("allowed_ref_ids") or []),
                        required_source_context=dict(block_data.get("required_source_context") or {}),
                        source_validation_status=block_data.get("source_validation_status", ""),
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
                writer_task_scope=dict(section.get("writer_task_scope") or {}),
                writer_task_dispositions=[dict(item) for item in section.get("writer_task_dispositions") or []],
            )
        )

    normalized_references = [str(reference).strip() for reference in references if str(reference).strip()]

    generation_context = {
        "generation_mode": generation_mode,
        "outline_artifact_id": outline_artifact_id,
        "outline_source_path": outline_source_path,
        "summary_file": summary_file,
        "section_count": len(normalized_sections),
        "citation_ref_catalog_path": citation_ref_catalog_path,
        "citation_ref_catalog_hash": citation_ref_catalog_hash,
    }
    if generation_mode == "outline_v3":
        generation_context.update({
            "outline_artifact_hash": outline_artifact_hash,
            "adoption_artifact_id": adoption_artifact_id,
            "adoption_artifact_hash": adoption_artifact_hash,
            "writer_section_artifacts": [
                {
                    "artifact_id": str(item.get("artifact_id") or ""),
                    "content_hash": str(item.get("content_hash") or ""),
                }
                for item in writer_section_artifacts
            ],
        })

    draft_identity = {
        "draft_id": draft_id,
        "project_name": project_name,
        "scope": "full_review",
    }
    normalized_title = str(title or "").strip()
    if normalized_title:
        draft_identity["title"] = normalized_title

    return ReviewDraft(
        artifact_type="review_draft",
        artifact_version="v3",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        draft_identity=draft_identity,
        generation_context=generation_context,
        content={
            "sections": normalized_sections,
            "references": normalized_references,
        },
        projections={
            "docx_path": review_word_path,
        },
    )

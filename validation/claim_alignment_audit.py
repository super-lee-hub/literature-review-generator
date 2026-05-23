from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Sequence


def _unique_non_empty(values: Sequence[Any]) -> List[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _paper_titles(report: Any) -> Dict[str, str]:
    titles: Dict[str, str] = {}
    for result in getattr(report, "citation_results", []) or []:
        hints = (getattr(result, "details", {}) or {}).get("paper_identity_hints", {}) or {}
        for paper_id, hint in hints.items():
            title = str((hint or {}).get("title") or "").strip()
            if title:
                titles[str(paper_id)] = title
    return titles


def _block_text_for_unit(result: Any, claim_unit: Dict[str, Any]) -> str:
    details = getattr(result, "details", {}) or {}
    bundle = details.get("bundle", {}) or {}
    block_context = str(getattr(result, "block_context", "") or details.get("block_context") or "").strip()
    if block_context:
        return block_context
    block_id = str(claim_unit.get("block_id") or "").strip()
    for block in bundle.get("blocks", []) or []:
        if str(block.get("block_id") or "") == block_id:
            return str(block.get("text") or "").strip()
    return str(getattr(result, "claim_text", "") or "").strip()


def _evidence_excerpt_rows(item: Dict[str, Any], paper_titles: Dict[str, str]) -> List[Dict[str, Any]]:
    excerpts = item.get("evidence_excerpts") or []
    if not excerpts:
        return []
    rows: List[Dict[str, Any]] = []
    for excerpt in excerpts[:5]:
        if not isinstance(excerpt, dict):
            continue
        paper_id = str(excerpt.get("paper_id") or "").strip()
        rows.append(
            {
                "paper_id": paper_id,
                "paper_title": paper_titles.get(paper_id, ""),
                "resolver_tier": excerpt.get("resolver_tier", ""),
                "match_reason": excerpt.get("match_reason", ""),
                "confidence": excerpt.get("confidence", 0.0),
                "text_excerpt": excerpt.get("text_excerpt") or excerpt.get("caption_excerpt") or "",
                "page_span": excerpt.get("page_span") or [],
                "chunk_ids": excerpt.get("chunk_ids") or [],
                "visual_refs": excerpt.get("visual_refs") or [],
            }
        )
    return rows


def _shorten(value: Any, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _row_for_claim_unit(
    *,
    result: Any,
    claim_unit: Dict[str, Any],
    item: Dict[str, Any],
    paper_titles: Dict[str, str],
) -> Dict[str, Any]:
    checked_paper_ids = _unique_non_empty(item.get("checked_paper_ids", []))
    expected_paper_ids = _unique_non_empty(item.get("expected_supporting_paper_ids", []))
    contributing_paper_ids = _unique_non_empty(item.get("contributing_paper_ids", []))
    pooled_paper_ids = _unique_non_empty(item.get("pooled_paper_ids", []))
    paper_ids_for_titles = _unique_non_empty(checked_paper_ids + expected_paper_ids + contributing_paper_ids + pooled_paper_ids)
    return {
        "citation_set_key": item.get("citation_set_key") or getattr(result, "citation_set_key", ""),
        "claim_unit_id": item.get("claim_unit_id", ""),
        "claim_text": item.get("claim_text") or claim_unit.get("claim_text") or "",
        "block_text": _block_text_for_unit(result, claim_unit),
        "alignment_status": item.get("alignment_status", ""),
        "alignment_confidence": item.get("alignment_confidence", 0.0),
        "paper_resolution_source": item.get("paper_resolution_source", ""),
        "checked_paper_ids": checked_paper_ids,
        "expected_supporting_paper_ids": expected_paper_ids,
        "unsupported_expected_paper_ids": _unique_non_empty(item.get("unsupported_expected_paper_ids", [])),
        "contributing_paper_ids": contributing_paper_ids,
        "pooled_paper_ids": pooled_paper_ids,
        "conclusion": getattr(getattr(result, "conclusion", None), "value", getattr(result, "conclusion", "")),
        "evidence_status": item.get("evidence_status") or getattr(result, "evidence_status", ""),
        "disposition": item.get("disposition") or getattr(result, "disposition", ""),
        "reason": item.get("reason") or (getattr(result, "details", {}) or {}).get("reason", ""),
        "paper_titles": {paper_id: paper_titles.get(paper_id, "") for paper_id in paper_ids_for_titles},
        "evidence_excerpts": _evidence_excerpt_rows(item, paper_titles),
        "span_start": item.get("span_start"),
        "span_end": item.get("span_end"),
    }


def _claim_unit_lookup(result: Any) -> Dict[str, Dict[str, Any]]:
    units = getattr(result, "claim_units", []) or (getattr(result, "details", {}) or {}).get("claim_units", []) or []
    return {
        str(unit.get("claim_unit_id") or ""): dict(unit)
        for unit in units
        if isinstance(unit, dict) and str(unit.get("claim_unit_id") or "").strip()
    }


def _is_multi_paper_row(row: Dict[str, Any]) -> bool:
    paper_ids = set(row.get("checked_paper_ids") or [])
    paper_ids.update(row.get("expected_supporting_paper_ids") or [])
    paper_ids.update(row.get("pooled_paper_ids") or [])
    return len(paper_ids) > 1 or "+" in str(row.get("citation_set_key") or "")


def build_claim_alignment_audit(report: Any) -> Dict[str, Any]:
    paper_titles = _paper_titles(report)
    wrong_source_rows: List[Dict[str, Any]] = []
    ambiguous_rows: List[Dict[str, Any]] = []
    gap_rows: List[Dict[str, Any]] = []
    supported_rows: List[Dict[str, Any]] = []

    for result in getattr(report, "citation_results", []) or []:
        unit_lookup = _claim_unit_lookup(result)
        for item in (getattr(result, "details", {}) or {}).get("claim_unit_results", []) or []:
            if not isinstance(item, dict):
                continue
            unit = unit_lookup.get(str(item.get("claim_unit_id") or ""), {})
            row = _row_for_claim_unit(result=result, claim_unit=unit, item=item, paper_titles=paper_titles)
            conclusion = str(row.get("conclusion") or "")
            reason = str(row.get("reason") or "")
            evidence_status = str(row.get("evidence_status") or "")
            if conclusion == "WRONG_SOURCE" or evidence_status == "wrong_source":
                wrong_source_rows.append(row)
            if reason == "ambiguous_claim_paper_alignment":
                ambiguous_rows.append(row)
            if evidence_status in {"evidence_gap", "needs_review"}:
                gap_rows.append(row)
            if conclusion == "SUPPORTED" or evidence_status == "clean_supported":
                supported_rows.append(row)

    gap_rows.sort(key=lambda row: (not _is_multi_paper_row(row), row.get("citation_set_key", "")))
    supported_rows.sort(key=lambda row: (not _is_multi_paper_row(row), row.get("citation_set_key", "")))

    return {
        "generated_at": datetime.now().isoformat(),
        "report_id": getattr(report, "report_id", ""),
        "summary": {
            "wrong_source_rows": len(wrong_source_rows),
            "ambiguous_claim_paper_alignment_rows": len(ambiguous_rows),
            "sampled_evidence_gap_or_needs_review_rows": min(len(gap_rows), 20),
            "sampled_supported_rows": min(len(supported_rows), 10),
        },
        "wrong_source": wrong_source_rows,
        "ambiguous_claim_paper_alignment": ambiguous_rows,
        "evidence_gap_or_needs_review_sample": gap_rows[:20],
        "supported_sample": supported_rows[:10],
    }


def _write_markdown(path: str, audit: Dict[str, Any]) -> None:
    lines = [
        "# Claim Alignment Audit",
        "",
        f"generated_at: {audit.get('generated_at', '')}",
        f"report_id: {audit.get('report_id', '')}",
        "",
    ]
    for section_key, title in (
        ("wrong_source", "WRONG_SOURCE"),
        ("ambiguous_claim_paper_alignment", "Ambiguous Claim-Paper Alignment"),
        ("evidence_gap_or_needs_review_sample", "Evidence Gap / Needs Review Sample"),
        ("supported_sample", "Supported Sample"),
    ):
        rows = audit.get(section_key, []) or []
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("_None._")
            lines.append("")
            continue
        for index, row in enumerate(rows, start=1):
            lines.append(f"### {index}. {row.get('citation_set_key', '')}")
            lines.append(f"- conclusion/reason: {row.get('conclusion', '')} / {row.get('reason', '')}")
            lines.append(
                "- alignment: "
                f"{row.get('alignment_status', '')} "
                f"({row.get('alignment_confidence', 0.0)}) via {row.get('paper_resolution_source', '')}"
            )
            lines.append(f"- checked: {', '.join(row.get('checked_paper_ids') or []) or '(none)'}")
            lines.append(f"- expected: {', '.join(row.get('expected_supporting_paper_ids') or []) or '(none)'}")
            unsupported = row.get("unsupported_expected_paper_ids") or []
            if unsupported:
                lines.append(f"- unsupported_expected: {', '.join(unsupported)}")
            lines.append(f"- contributing: {', '.join(row.get('contributing_paper_ids') or []) or '(none)'}")
            pooled = row.get("pooled_paper_ids") or []
            if pooled:
                lines.append(f"- pooled: {', '.join(pooled)}")
            paper_titles = row.get("paper_titles") or {}
            if paper_titles:
                title_bits = [
                    f"{paper_id}={_shorten(title, 140)}"
                    for paper_id, title in paper_titles.items()
                    if title
                ]
                lines.append(f"- paper_titles: {'; '.join(title_bits) or '(none)'}")
            lines.append(f"- claim: {_shorten(row.get('claim_text', ''), 700)}")
            lines.append(f"- block: {_shorten(row.get('block_text', ''), 700)}")
            evidence_rows = row.get("evidence_excerpts") or []
            if evidence_rows:
                lines.append("- evidence:")
                for evidence_index, evidence in enumerate(evidence_rows, start=1):
                    visual_refs = evidence.get("visual_refs") or []
                    visual_ref_ids = [
                        str(ref.get("visual_id") or ref.get("artifact_id") or ref.get("image_path") or "").strip()
                        for ref in visual_refs
                        if isinstance(ref, dict)
                    ]
                    lines.append(
                        f"  {evidence_index}. [{evidence.get('paper_id', '')}] "
                        f"{evidence.get('paper_title', '')} | {evidence.get('resolver_tier', '')} | "
                        f"pages={evidence.get('page_span', [])} chunks={evidence.get('chunk_ids', [])} "
                        f"visual_refs={list(filter(None, visual_ref_ids))}"
                    )
                    lines.append(f"     {_shorten(evidence.get('text_excerpt', ''), 700)}")
            else:
                lines.append("- evidence: (none)")
            lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def write_claim_alignment_audit(report: Any, reports_dir: str) -> Dict[str, str]:
    os.makedirs(reports_dir, exist_ok=True)
    audit = build_claim_alignment_audit(report)
    json_path = os.path.join(reports_dir, "validation_claim_alignment_audit.json")
    markdown_path = os.path.join(reports_dir, "validation_claim_alignment_audit.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(audit, handle, ensure_ascii=False, indent=2)
    _write_markdown(markdown_path, audit)
    return {"claim_alignment_audit_json": json_path, "claim_alignment_audit_md": markdown_path}

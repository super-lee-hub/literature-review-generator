from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from services.stage1_text_quality import FAIL, PASS, WARN, score_text_quality


@dataclass(frozen=True)
class Stage1InputSelection:
    selected_text: str
    selected_source: str
    quality_level: str
    fallback_reason: str
    stage1_quality_reasons: List[str]
    candidate_reports: List[Dict[str, Any]]
    manifest_payload: Dict[str, Any]
    quality_report_payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def select_stage1_input(
    *,
    markdown_text: str,
    plain_text: str,
    page_index: Optional[List[Dict[str, Any]]] = None,
    expected_language: Optional[str] = None,
    title: Optional[str] = None,
    allow_reprocess: bool = True,
) -> Stage1InputSelection:
    """Choose the final text that is allowed to enter stage-one model analysis."""

    markdown = str(markdown_text or "")
    plain = str(plain_text or "")
    markdown_from_plain = _fallback_markdown_from_text(plain)
    candidates = [
        ("normalized_markdown", markdown, plain or None),
        ("plain_text", plain, None),
        ("markdown_from_plain_text", markdown_from_plain, plain or None),
    ]

    reports: List[Dict[str, Any]] = []
    for source, text, reference in candidates:
        result = score_text_quality(
            text,
            reference_text=reference,
            expected_language=expected_language,
            title=title,
        )
        reports.append(
            {
                "source": source,
                "decision": result.decision,
                "reasons": result.reasons,
                "metrics": result.metrics,
                "text_length": len(text),
            }
        )

    normalized = reports[0]
    if normalized["decision"] in {PASS, WARN} and markdown.strip():
        return _build_selection(
            selected_text=markdown,
            selected_source="normalized_markdown",
            quality_level=str(normalized["decision"]),
            fallback_reason="",
            reasons=list(normalized.get("reasons") or []),
            reports=reports,
            page_index=page_index,
        )

    plain_report = reports[1]
    if plain_report["decision"] in {PASS, WARN} and plain.strip():
        return _build_selection(
            selected_text=plain,
            selected_source="plain_text",
            quality_level="FALLBACK",
            fallback_reason="normalized_markdown_failed",
            reasons=list(normalized.get("reasons") or []),
            reports=reports,
            page_index=page_index,
        )

    markdown_from_plain_report = reports[2]
    if markdown_from_plain_report["decision"] in {PASS, WARN} and markdown_from_plain.strip():
        return _build_selection(
            selected_text=markdown_from_plain,
            selected_source="markdown_from_plain_text",
            quality_level="FALLBACK",
            fallback_reason="normalized_and_plain_text_failed",
            reasons=list(markdown_from_plain_report.get("reasons") or []),
            reports=reports,
            page_index=page_index,
        )

    quality_level = "REPROCESS" if allow_reprocess else "BLOCK"
    all_reasons = sorted({reason for report in reports for reason in (report.get("reasons") or [])})
    return _build_selection(
        selected_text="",
        selected_source="",
        quality_level=quality_level,
        fallback_reason="all_stage1_text_candidates_failed",
        reasons=all_reasons,
        reports=reports,
        page_index=page_index,
    )


def _build_selection(
    *,
    selected_text: str,
    selected_source: str,
    quality_level: str,
    fallback_reason: str,
    reasons: List[str],
    reports: List[Dict[str, Any]],
    page_index: Optional[List[Dict[str, Any]]],
) -> Stage1InputSelection:
    manifest_payload = {
        "artifact_type": "stage1_input_manifest",
        "artifact_version": "v1",
        "selected_text_source": selected_source,
        "stage1_quality_level": quality_level,
        "fallback_reason": fallback_reason,
        "stage1_quality_reasons": reasons,
        "selected_text_length": len(selected_text),
        "candidate_count": len(reports),
        "page_count": len(page_index or []),
    }
    quality_report_payload = {
        "artifact_type": "stage1_text_quality_report",
        "artifact_version": "v1",
        "selected_text_source": selected_source,
        "stage1_quality_level": quality_level,
        "fallback_reason": fallback_reason,
        "stage1_quality_reasons": reasons,
        "candidate_reports": reports,
    }
    return Stage1InputSelection(
        selected_text=selected_text,
        selected_source=selected_source,
        quality_level=quality_level,
        fallback_reason=fallback_reason,
        stage1_quality_reasons=reasons,
        candidate_reports=reports,
        manifest_payload=manifest_payload,
        quality_report_payload=quality_report_payload,
    )


def _fallback_markdown_from_text(plain_text: str) -> str:
    text = str(plain_text or "")
    if not text.strip():
        return ""
    sections = [block.strip() for block in text.split("--- Page ") if block.strip()]
    if not sections:
        return text
    parts: List[str] = []
    for section in sections:
        lines = section.splitlines()
        page_marker = lines[0].strip(" -") if lines else ""
        content = "\n".join(lines[1:]).strip()
        if page_marker and content:
            parts.append(f"## Page {page_marker}\n\n{content}")
        elif content:
            parts.append(content)
    return "\n\n".join(parts).strip() or text

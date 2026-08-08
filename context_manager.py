"""Deterministic summary formatting and quality checks.

Context assembly is performed by typed stage executors.  This module contains
only bounded, lossless helpers used by validation and reporting; it never
silently drops source text to fit a provider budget.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple, Union

from models import ProcessingResult
from summary_schema import get_core_analysis, get_paper_metadata


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return int(chinese + latin / 4)


def _placeholder(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or text.casefold() in {"unknown", "n/a", "na", "none", "null", "..."}


def _authors(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if not _placeholder(item)]


def _metadata(primary: Any, fallback: Any) -> Any:
    if isinstance(primary, list):
        return _authors(primary) or _authors(fallback)
    if not _placeholder(primary):
        return str(primary).strip()
    return "" if _placeholder(fallback) else str(fallback).strip()


def convert_json_to_markdown(
    summaries_data: Union[List[Dict[str, Any]], List[ProcessingResult]],
) -> str:
    if not summaries_data:
        return "# Literature review evidence\n\n(No source summaries.)"
    lines = ["# Literature review evidence", ""]
    for index, summary in enumerate(summaries_data, start=1):
        payload = summary if isinstance(summary, dict) else summary.to_dict()
        paper_info = payload.get("paper_info") or {}
        core = get_core_analysis(payload)
        title = str(paper_info.get("title") or "Untitled source")
        year = str(paper_info.get("year") or "")
        authors = _authors(paper_info.get("authors"))
        lines.extend([f"## Source {index}: {title}", "", f"Authors: {', '.join(authors)} ({year})", ""])
        for label, key in (("Summary", "summary"), ("Findings", "findings"), ("Method", "methodology")):
            value = str(core.get(key) or "").strip()
            if value and value != "...":
                lines.extend([f"**{label}:** {value}", ""])
        points = [str(item).strip() for item in core.get("key_points", []) or () if str(item).strip()]
        if points:
            lines.append("**Key points:**")
            lines.extend(f"{point_index}. {point}" for point_index, point in enumerate(points, start=1))
            lines.append("")
        lines.extend(["---", ""])
    return "\n".join(lines)


def validate_summary_quality(summary_data: Union[Dict[str, Any], ProcessingResult]) -> Tuple[bool, str]:
    if not summary_data:
        return False, "summary is empty"
    payload = summary_data if isinstance(summary_data, dict) else summary_data.to_dict()
    paper_info = payload.get("paper_info") or {}
    metadata = get_paper_metadata(payload)
    core = get_core_analysis(payload)
    source_mode = str(payload.get("source_mode") or paper_info.get("source_mode") or "").casefold()
    relaxed_metadata = source_mode == "direct"
    issues: list[str] = []
    for label, key in (("summary", "summary"), ("findings", "findings")):
        value = str(core.get(key) or "").strip()
        if not value or value == "...":
            issues.append(f"{label} is empty")
        elif len(value) < 50:
            issues.append(f"{label} is too short")
    points = [str(item).strip() for item in core.get("key_points", []) or () if str(item).strip() and str(item).strip() != "..."]
    if not points:
        issues.append("key_points are empty")
    authors = _metadata(paper_info.get("authors"), metadata.get("authors"))
    year = _metadata(paper_info.get("year"), metadata.get("year"))
    journal = _metadata(paper_info.get("journal"), metadata.get("journal"))
    if not authors and not relaxed_metadata:
        issues.append("authors are missing")
    if _placeholder(year) and not relaxed_metadata:
        issues.append("year is missing")
    if _placeholder(journal) and not relaxed_metadata:
        issues.append("journal is missing")
    return (False, "; ".join(issues)) if issues else (True, "quality check passed")


def batch_quality_check(
    summaries_data: Union[List[Dict[str, Any]], List[ProcessingResult]],
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "total_papers": len(summaries_data),
        "qualified_papers": 0,
        "failed_papers": [],
        "quality_issues": {},
    }
    for index, summary in enumerate(summaries_data):
        if isinstance(summary, dict) and summary.get("status") == "failed":
            report["failed_papers"].append({"index": index, "reason": "source analysis failed"})
            continue
        qualified, reason = validate_summary_quality(summary)
        if qualified:
            report["qualified_papers"] += 1
            continue
        report["failed_papers"].append({"index": index, "reason": reason})
        issue_type = reason.split(";", 1)[0]
        issues = report["quality_issues"]
        issues[issue_type] = int(issues.get(issue_type, 0)) + 1
    return report

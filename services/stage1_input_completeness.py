from __future__ import annotations

import math
import re
from typing import Any, Dict, Mapping, Optional


BLOCKING_COMPLETENESS_REASONS = {
    "incomplete_by_page_count",
    "shorter_than_alternative",
}
WARNING_COMPLETENESS_REASONS = {
    "thin_multpage_input",
}

_LATE_STRUCTURE_RE = re.compile(
    r"\b(references?|bibliography|discussion|conclusions?|limitations?|appendix|received|revised)\b",
    re.IGNORECASE,
)


def build_completeness_metrics(
    *,
    text: str = "",
    page_count: int = 0,
    candidate_lengths: Optional[Mapping[str, int]] = None,
    selected_text_length: Optional[int] = None,
    chunk_count: Optional[int] = None,
) -> Dict[str, Any]:
    candidate = str(text or "")
    length = int(selected_text_length) if selected_text_length is not None else len(candidate)
    safe_page_count = max(int(page_count or 0), 0)
    lengths = {
        str(key): max(int(value or 0), 0)
        for key, value in dict(candidate_lengths or {}).items()
    }
    best_candidate_length = max(lengths.values(), default=length)
    chars_per_page = length / safe_page_count if safe_page_count else None
    estimated_chunk_count = int(chunk_count) if chunk_count is not None else _estimate_chunk_count(candidate, length)
    has_late_structure_signal = bool(_LATE_STRUCTURE_RE.search(candidate)) if candidate else None

    blocking_reasons = []
    warning_reasons = []
    min_length_for_pages = None
    if safe_page_count >= 5:
        min_length_for_pages = min(6000, safe_page_count * 750)
        if length < min_length_for_pages:
            blocking_reasons.append("incomplete_by_page_count")

        thin_length_threshold = min(9000, safe_page_count * 1200)
        if length < thin_length_threshold or estimated_chunk_count <= 1:
            warning_reasons.append("thin_multpage_input")
        elif has_late_structure_signal is False and length < 12000:
            warning_reasons.append("thin_multpage_input")

    alternative_ratio = None
    if best_candidate_length > 0:
        alternative_ratio = length / best_candidate_length
    if best_candidate_length >= 5000 and alternative_ratio is not None and alternative_ratio < 0.60:
        blocking_reasons.append("shorter_than_alternative")

    blocking_reasons = sorted(set(blocking_reasons))
    warning_reasons = sorted(set(warning_reasons))
    return {
        "selected_text_length": length,
        "page_count": safe_page_count,
        "chars_per_page": chars_per_page,
        "min_length_for_pages": min_length_for_pages,
        "candidate_lengths": lengths,
        "best_candidate_length": best_candidate_length,
        "alternative_length_ratio": alternative_ratio,
        "estimated_chunk_count": estimated_chunk_count,
        "has_late_structure_signal": has_late_structure_signal,
        "blocking_reasons": blocking_reasons,
        "warning_reasons": warning_reasons,
        "reasons": sorted(set(blocking_reasons + warning_reasons)),
    }


def has_blocking_stage1_reason(reasons: Any) -> bool:
    return any(str(reason) in BLOCKING_COMPLETENESS_REASONS for reason in reasons or [])


def is_blocked_stage1_quality(quality_level: Any, reasons: Any) -> bool:
    level = str(quality_level or "").strip().upper()
    return level in {"REPROCESS", "BLOCK"} or has_blocking_stage1_reason(reasons)


def _estimate_chunk_count(text: str, length: int) -> int:
    if not text.strip():
        return 0
    for marker in ("--- Page ", "## Page "):
        if marker in text:
            sections = [block.strip() for block in text.split(marker) if block.strip()]
            if sections:
                return len(sections)
    return max(1, math.ceil(length / 8000))

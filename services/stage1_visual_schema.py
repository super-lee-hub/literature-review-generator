"""Single-source schema constants shared by Stage 1 validation and prompts."""

from __future__ import annotations

import json


VISUAL_EVIDENCE_KINDS = (
    "quantitative_values",
    "significance_markers",
    "ocr_conflict",
    "relationships",
    "axes_or_headers",
    "visible_text",
    "layout_observations",
    "manual_review",
)
VISUAL_EVIDENCE_KINDS_SET = frozenset(VISUAL_EVIDENCE_KINDS)


def visual_evidence_kinds_json() -> str:
    """Render the canonical evidence-kind enum for the active visual prompt."""

    return json.dumps(list(VISUAL_EVIDENCE_KINDS), ensure_ascii=False, indent=2)


__all__ = [
    "VISUAL_EVIDENCE_KINDS",
    "VISUAL_EVIDENCE_KINDS_SET",
    "visual_evidence_kinds_json",
]

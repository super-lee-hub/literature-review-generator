"""Deterministic planning and validation for Stage 1 visual scan batches."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Iterable, Mapping, Sequence

from services.prompt_registry import PromptRegistry


@dataclass(frozen=True)
class VisualScanBatch:
    batch_index: int
    visual_refs: tuple[dict[str, Any], ...]

    @property
    def visual_ids(self) -> tuple[str, ...]:
        return tuple(str(item.get("visual_id") or "") for item in self.visual_refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "visual_ids": list(self.visual_ids),
            "page_nos": [int(item.get("page_no") or 0) for item in self.visual_refs],
            "visual_refs": [dict(item) for item in self.visual_refs],
        }


def plan_visual_scan_batches(
    visual_refs: Iterable[Mapping[str, Any]],
    *,
    batch_size: int = 10,
) -> tuple[VisualScanBatch, ...]:
    """Partition every page snapshot into stable, page-ordered batches."""

    size = max(1, int(batch_size))
    normalized = [dict(item) for item in visual_refs if isinstance(item, Mapping)]
    normalized.sort(key=lambda item: (int(item.get("page_no") or 0), str(item.get("visual_id") or "")))
    return tuple(
        VisualScanBatch(index, tuple(normalized[start:start + size]))
        for index, start in enumerate(range(0, len(normalized), size))
    )


def visual_label(visual: Mapping[str, Any]) -> str:
    return (
        f"visual_id={str(visual.get('visual_id') or '')}; "
        f"page_no={int(visual.get('page_no') or 0)}; "
        f"bbox={visual.get('bbox') or []}; "
        f"artifact_type={str(visual.get('artifact_type') or '')}; "
        f"caption_excerpt={str(visual.get('caption_excerpt') or '')[:360]}; "
        f"nearby_text_excerpt={str(visual.get('nearby_text_excerpt') or '')[:500]}"
    )


def build_visual_scan_user_content(batch: VisualScanBatch) -> list[dict[str, Any]]:
    """Put a text label immediately before each image content item."""

    content: list[dict[str, Any]] = []
    for visual in batch.visual_refs:
        image_path = str(visual.get("image_path") or "").strip()
        if not image_path or not os.path.isfile(image_path):
            continue
        content.append({
            "type": "text",
            "text": "[VISUAL SCAN OBJECT]\n" + visual_label(visual),
        })
        content.append({
            "type": "local_image_path",
            "path": image_path,
            "visual_id": str(visual.get("visual_id") or ""),
            "page_no": int(visual.get("page_no") or 0),
            "bbox": list(visual.get("bbox") or []),
            "artifact_type": str(visual.get("artifact_type") or ""),
        })
    return content


def validate_visual_observations(
    payload: Mapping[str, Any],
    *,
    allowed_visual_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate observation identity without inferring visual content."""

    allowed = {str(item) for item in allowed_visual_ids if str(item)}
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("visual observation payload must contain an observations array")
    normalized: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ValueError("visual observation entries must be objects")
        visual_id = str(observation.get("visual_id") or "")
        if visual_id not in allowed:
            raise ValueError(f"visual observation references unknown visual_id: {visual_id}")
        normalized.append(dict(observation))
    return {
        "artifact_type": "stage1_visual_observations",
        "artifact_version": "v1",
        "observations": normalized,
    }


def build_visual_scan_prompt(batch: VisualScanBatch, *, ocr_by_visual_id: Mapping[str, str] | None = None) -> tuple[str, str]:
    registry = PromptRegistry()
    labels = []
    ocr = dict(ocr_by_visual_id or {})
    for visual in batch.visual_refs:
        visual_id = str(visual.get("visual_id") or "")
        labels.append({
            "visual_id": visual_id,
            "page_no": int(visual.get("page_no") or 0),
            "bbox": list(visual.get("bbox") or []),
            "artifact_type": str(visual.get("artifact_type") or ""),
            "caption_excerpt": str(visual.get("caption_excerpt") or ""),
            "nearby_text_excerpt": str(visual.get("nearby_text_excerpt") or ""),
            "ocr_excerpt": str(ocr.get(visual_id) or ""),
        })
    user_text = (
        "Visual scan batch metadata:\n"
        + json.dumps({"batch_index": batch.batch_index, "objects": labels}, ensure_ascii=False, indent=2)
        + "\nReturn only the stage1_visual_observations JSON required by the system prompt."
    )
    return user_text, registry.read("stage1.visual_scan.system.v1")


__all__ = [
    "VisualScanBatch",
    "build_visual_scan_prompt",
    "build_visual_scan_user_content",
    "plan_visual_scan_batches",
    "validate_visual_observations",
    "visual_label",
]

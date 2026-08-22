"""Deterministic planning, transport accounting, and validation for Stage 1 visuals.

The visual scan boundary is deliberately strict: a successful scan is only
valid when the provider returned exactly one schema-valid observation for every
visual that was actually sent. Planned, missing, oversized, and omitted
visuals remain explicit evidence instead of being counted as covered.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from services.prompt_registry import PromptRegistry


VISUAL_INPUT_IDENTITY_VERSION = "stage1_visual_input_identity/v1"
DEFAULT_MAX_REQUEST_IMAGE_BYTES = 36_000_000
DEFAULT_MAX_SINGLE_IMAGE_BYTES = 24_000_000
_BASE64_NUMERATOR = 4
_BASE64_DENOMINATOR = 3


def estimate_encoded_image_bytes(raw_bytes: int, *, metadata_bytes: int = 240) -> int:
    """Estimate JSON/base64 request bytes for one local image."""

    raw = max(0, int(raw_bytes or 0))
    return int(math.ceil(raw * _BASE64_NUMERATOR / _BASE64_DENOMINATOR)) + max(0, int(metadata_bytes))


def normalize_visual_byte_budgets(
    *,
    max_request_image_bytes: Any = None,
    max_single_image_bytes: Any = None,
) -> tuple[int, int]:
    """Resolve the one raw/encoded image budget used by planning and transport."""

    try:
        request_limit = int(max_request_image_bytes)
    except (TypeError, ValueError):
        request_limit = DEFAULT_MAX_REQUEST_IMAGE_BYTES
    try:
        single_limit = int(max_single_image_bytes)
    except (TypeError, ValueError):
        single_limit = DEFAULT_MAX_SINGLE_IMAGE_BYTES
    return max(1, request_limit), max(1, single_limit)


def _image_bytes(visual: Mapping[str, Any]) -> int:
    try:
        declared = int(visual.get("image_bytes") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0:
        return declared
    path = str(visual.get("image_path") or "").strip()
    try:
        return max(0, int(os.path.getsize(path))) if path else 0
    except OSError:
        return 0


def _file_sha256(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


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
    max_request_image_bytes: int = DEFAULT_MAX_REQUEST_IMAGE_BYTES,
    max_single_image_bytes: int = DEFAULT_MAX_SINGLE_IMAGE_BYTES,
) -> tuple[VisualScanBatch, ...]:
    """Partition every page snapshot by order, count, and encoded byte budget."""

    size = max(1, int(batch_size))
    request_limit, single_limit = normalize_visual_byte_budgets(
        max_request_image_bytes=max_request_image_bytes,
        max_single_image_bytes=max_single_image_bytes,
    )
    normalized = [dict(item) for item in visual_refs if isinstance(item, Mapping)]
    normalized.sort(key=lambda item: (int(item.get("page_no") or 0), str(item.get("visual_id") or "")))
    batches: list[VisualScanBatch] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for visual in normalized:
        estimated = estimate_encoded_image_bytes(_image_bytes(visual))
        if current and (len(current) >= size or current_bytes + estimated > request_limit):
            batches.append(VisualScanBatch(len(batches), tuple(current)))
            current = []
            current_bytes = 0
        # Keep an oversized item in its own batch. Execution records it as
        # unsent rather than allowing it to count as covered.
        current.append(visual)
        current_bytes += estimated
        if _image_bytes(visual) > single_limit and len(current) > 1:
            last = current.pop()
            current_bytes -= estimated
            batches.append(VisualScanBatch(len(batches), tuple(current)))
            current = [last]
            current_bytes = estimated
    if current:
        batches.append(VisualScanBatch(len(batches), tuple(current)))
    return tuple(batches)


def visual_label(visual: Mapping[str, Any]) -> str:
    return (
        f"visual_id={str(visual.get('visual_id') or '')}; "
        f"page_no={int(visual.get('page_no') or 0)}; "
        f"bbox={visual.get('bbox') or []}; "
        f"artifact_type={str(visual.get('artifact_type') or '')}; "
        f"caption_excerpt={str(visual.get('caption_excerpt') or '')[:360]}; "
        f"nearby_text_excerpt={str(visual.get('nearby_text_excerpt') or '')[:500]}"
    )


def build_visual_scan_user_content(
    batch: VisualScanBatch,
    *,
    return_report: bool = False,
    max_single_image_bytes: int = DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    max_request_image_bytes: int = DEFAULT_MAX_REQUEST_IMAGE_BYTES,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    """Put a label immediately before each sendable image.

    The legacy list return remains available. The report is the authoritative
    transport plan for receipt identity and coverage accounting.
    """

    content: list[dict[str, Any]] = []
    sent_refs: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    encoded_bytes = 0
    request_limit, single_limit = normalize_visual_byte_budgets(
        max_request_image_bytes=max_request_image_bytes,
        max_single_image_bytes=max_single_image_bytes,
    )
    for visual in batch.visual_refs:
        visual_id = str(visual.get("visual_id") or "")
        path = str(visual.get("image_path") or "").strip()
        raw_bytes = _image_bytes(visual)
        reason = ""
        if not visual_id:
            reason = "visual_id_missing"
        elif not path or not os.path.isfile(path):
            reason = "image_missing"
        elif raw_bytes <= 0:
            reason = "image_empty"
        elif raw_bytes > single_limit:
            reason = "image_exceeds_single_byte_budget"
        else:
            estimated = estimate_encoded_image_bytes(raw_bytes)
            if encoded_bytes + estimated > request_limit:
                reason = "image_exceeds_request_byte_budget"
        if reason:
            omissions.append({"visual_id": visual_id, "page_no": int(visual.get("page_no") or 0), "reason": reason})
            continue
        estimated = estimate_encoded_image_bytes(raw_bytes)
        encoded_bytes += estimated
        sent_refs.append(dict(visual))
        content.append({"type": "text", "text": "[VISUAL SCAN OBJECT]\n" + visual_label(visual)})
        content.append({
            "type": "local_image_path",
            "path": path,
            "visual_id": visual_id,
            "page_no": int(visual.get("page_no") or 0),
            "bbox": list(visual.get("bbox") or []),
            "artifact_type": str(visual.get("artifact_type") or ""),
            "image_sha256": str(visual.get("image_sha256") or _file_sha256(path)),
            "image_bytes": raw_bytes,
        })
    report = {
        "identity_version": VISUAL_INPUT_IDENTITY_VERSION,
        "planned_visual_ids": list(batch.visual_ids),
        "sent_visual_ids": [str(item.get("visual_id") or "") for item in sent_refs],
        "sent_visual_refs": sent_refs,
        "omissions": omissions,
        "estimated_encoded_bytes": encoded_bytes,
        "max_single_image_bytes": single_limit,
        "max_request_image_bytes": request_limit,
    }
    return (content, report) if return_report else content


_OBSERVATION_FIELDS = {
    "visual_id", "page_no", "bbox", "artifact_type", "visible_text", "title_or_caption",
    "axes_or_headers", "legend_or_notes", "quantitative_values", "relationships",
    "layout_observations", "ocr_conflicts", "confidence", "needs_manual_review",
}
_ARTIFACT_TYPES = {"page_snapshot", "figure_crop", "table_crop", "formula_crop"}
_CONFIDENCE = {"high", "medium", "low"}


def validate_visual_observations(
    payload: Mapping[str, Any],
    *,
    allowed_visual_ids: Sequence[str],
    expected_visual_refs: Sequence[Mapping[str, Any]] | None = None,
    sent_visual_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate exact sent coverage, identity, and the v1 observation schema."""

    if not isinstance(payload, Mapping):
        raise ValueError("visual observation payload must be an object")
    if payload.get("artifact_type") != "stage1_visual_observations":
        raise ValueError("visual observation artifact_type is invalid")
    if payload.get("artifact_version") != "v1":
        raise ValueError("visual observation artifact_version is invalid")
    allowed = {str(item) for item in allowed_visual_ids if str(item)}
    expected_ids = {str(item) for item in (sent_visual_ids or allowed_visual_ids) if str(item)}
    if not expected_ids:
        raise ValueError("visual scan has no sent visual IDs")
    if not expected_ids.issubset(allowed):
        raise ValueError("sent visual IDs must be a subset of allowed visual IDs")
    refs_by_id = {
        str(item.get("visual_id") or ""): dict(item)
        for item in (expected_visual_refs or ())
        if isinstance(item, Mapping) and str(item.get("visual_id") or "")
    }
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("visual observation payload must contain an observations array")
    if len(observations) != len(expected_ids):
        raise ValueError(f"visual observation coverage mismatch: expected {len(expected_ids)}, got {len(observations)}")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ValueError("visual observation entries must be objects")
        unknown_fields = set(observation) - _OBSERVATION_FIELDS
        if unknown_fields:
            raise ValueError(f"visual observation has unexpected fields: {sorted(map(str, unknown_fields))}")
        visual_id = str(observation.get("visual_id") or "")
        if visual_id not in expected_ids:
            raise ValueError(f"visual observation references unknown or unsent visual_id: {visual_id}")
        if visual_id in seen:
            raise ValueError(f"duplicate visual observation: {visual_id}")
        seen.add(visual_id)
        ref = refs_by_id.get(visual_id)
        try:
            page_no = int(observation.get("page_no") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"visual observation page_no is invalid: {visual_id}") from exc
        if ref is not None and page_no != int(ref.get("page_no") or 0):
            raise ValueError(f"visual observation page mismatch: {visual_id}")
        artifact_type = str(observation.get("artifact_type") or "")
        if artifact_type not in _ARTIFACT_TYPES:
            raise ValueError(f"visual observation artifact_type is invalid: {visual_id}")
        if ref is not None and artifact_type != str(ref.get("artifact_type") or ""):
            raise ValueError(f"visual observation artifact type mismatch: {visual_id}")
        bbox = observation.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"visual observation bbox is invalid: {visual_id}")
        try:
            bbox_values = [float(value) for value in bbox]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"visual observation bbox is not numeric: {visual_id}") from exc
        if any(value < 0 for value in bbox_values):
            raise ValueError(f"visual observation bbox is negative: {visual_id}")
        for field_name in (
            "visible_text", "axes_or_headers", "legend_or_notes", "quantitative_values",
            "relationships", "layout_observations", "ocr_conflicts",
        ):
            if not isinstance(observation.get(field_name), list):
                raise ValueError(f"visual observation {field_name} must be an array: {visual_id}")
        title = observation.get("title_or_caption")
        if title is not None and not isinstance(title, str):
            raise ValueError(f"visual observation title_or_caption must be string or null: {visual_id}")
        confidence = str(observation.get("confidence") or "")
        if confidence not in _CONFIDENCE:
            raise ValueError(f"visual observation confidence is invalid: {visual_id}")
        if not isinstance(observation.get("needs_manual_review"), bool):
            raise ValueError(f"visual observation needs_manual_review must be boolean: {visual_id}")
        normalized.append({**dict(observation), "page_no": page_no, "bbox": bbox_values})
    missing = expected_ids - seen
    if missing:
        raise ValueError(f"missing visual observations: {sorted(missing)}")
    order = {visual_id: index for index, visual_id in enumerate(allowed_visual_ids)}
    normalized.sort(key=lambda item: (order.get(str(item.get("visual_id") or ""), 10**9), str(item.get("visual_id") or "")))
    return {"artifact_type": "stage1_visual_observations", "artifact_version": "v1", "observations": normalized}


def select_final_visual_refs_after_scan(
    visual_refs: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    *,
    max_refs: int = 8,
) -> list[dict[str, Any]]:
    """Select raw crops/pages after scan evidence exists, deterministically."""

    limit = max(0, int(max_refs))
    if limit == 0:
        return []
    by_id = {
        str(item.get("visual_id") or ""): dict(item)
        for item in observations
        if isinstance(item, Mapping) and str(item.get("visual_id") or "")
    }
    candidates: list[tuple[float, int, str, dict[str, Any]]] = []
    for ref in visual_refs:
        if not isinstance(ref, Mapping):
            continue
        visual_id = str(ref.get("visual_id") or "")
        observation = by_id.get(visual_id)
        if observation is None:
            continue
        score = float(ref.get("selection_score") or 0.0)
        score += 2.0 * len(observation.get("quantitative_values") or [])
        score += 1.5 * len(observation.get("relationships") or [])
        score += 1.0 * len(observation.get("axes_or_headers") or [])
        score += 0.5 * len(observation.get("visible_text") or [])
        score += 2.0 if observation.get("needs_manual_review") else 0.0
        score += {"high": 1.5, "medium": 1.0, "low": 0.25}.get(str(observation.get("confidence") or ""), 0.0)
        score += 0.5 if str(ref.get("artifact_type") or "") != "page_snapshot" else 0.0
        candidates.append((-score, int(ref.get("page_no") or 0), visual_id, dict(ref)))
    candidates.sort(key=lambda item: item[:3])
    selected: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for _neg_score, _page_no, _visual_id, ref in candidates:
        group = str(ref.get("dedupe_group_id") or "")
        if group and group in seen_groups:
            continue
        selected.append(ref)
        if group:
            seen_groups.add(group)
        if len(selected) >= limit:
            break
    return selected


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
        + "\nReturn exactly one observation for every image actually sent; use low confidence and manual review when unclear."
    )
    return user_text, registry.read("stage1.visual_scan.system.v1")


__all__ = [
    "DEFAULT_MAX_REQUEST_IMAGE_BYTES", "DEFAULT_MAX_SINGLE_IMAGE_BYTES", "VISUAL_INPUT_IDENTITY_VERSION",
    "VisualScanBatch", "build_visual_scan_prompt", "build_visual_scan_user_content",
    "estimate_encoded_image_bytes", "normalize_visual_byte_budgets", "plan_visual_scan_batches", "select_final_visual_refs_after_scan",
    "validate_visual_observations", "visual_label",
]

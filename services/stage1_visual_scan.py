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
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from services.prompt_registry import PromptRegistry


VISUAL_INPUT_IDENTITY_VERSION = "stage1_visual_input_identity/v1"
VISUAL_OBSERVATIONS_ARTIFACT_TYPE = "stage1_visual_observations"
VISUAL_OBSERVATIONS_VERSION = "v2"
VISUAL_SCAN_PROMPT_ID = "stage1.visual_scan.system.v2"
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
    child_candidates: tuple[dict[str, Any], ...] = ()

    @property
    def visual_ids(self) -> tuple[str, ...]:
        return tuple(str(item.get("visual_id") or "") for item in self.visual_refs)

    @property
    def child_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.get("visual_id") or "")
            for item in self.child_candidates
            if str(item.get("visual_id") or "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "visual_ids": list(self.visual_ids),
            "page_nos": [int(item.get("page_no") or 0) for item in self.visual_refs],
            "visual_refs": [dict(item) for item in self.visual_refs],
            "child_candidates": [
                _candidate_metadata(item) for item in self.child_candidates
            ],
        }


def plan_visual_scan_batches(
    visual_refs: Iterable[Mapping[str, Any]],
    *,
    candidate_refs: Iterable[Mapping[str, Any]] = (),
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
    candidates = [
        dict(item)
        for item in candidate_refs
        if isinstance(item, Mapping)
        and str(item.get("artifact_type") or "") != "page_snapshot"
        and str(item.get("visual_id") or "")
    ]
    candidates.sort(
        key=lambda item: (
            int(item.get("page_no") or 0),
            str(item.get("artifact_type") or ""),
            str(item.get("visual_id") or ""),
        )
    )

    def _batch(refs: Sequence[Mapping[str, Any]], index: int) -> VisualScanBatch:
        page_numbers = {int(item.get("page_no") or 0) for item in refs}
        return VisualScanBatch(
            batch_index=index,
            visual_refs=tuple(dict(item) for item in refs),
            child_candidates=tuple(
                dict(item)
                for item in candidates
                if int(item.get("page_no") or 0) in page_numbers
            ),
        )

    batches: list[VisualScanBatch] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for visual in normalized:
        estimated = estimate_encoded_image_bytes(_image_bytes(visual))
        if current and (len(current) >= size or current_bytes + estimated > request_limit):
            batches.append(_batch(current, len(batches)))
            current = []
            current_bytes = 0
        # Keep an oversized item in its own batch. Execution records it as
        # unsent rather than allowing it to count as covered.
        current.append(visual)
        current_bytes += estimated
        if _image_bytes(visual) > single_limit and len(current) > 1:
            last = current.pop()
            current_bytes -= estimated
            batches.append(_batch(current, len(batches)))
            current = [last]
            current_bytes = estimated
    if current:
        batches.append(_batch(current, len(batches)))
    return tuple(batches)


def _candidate_metadata(visual: Mapping[str, Any]) -> dict[str, Any]:
    """Return only non-image metadata that can identify a child candidate."""

    return {
        "candidate_visual_id": str(visual.get("visual_id") or ""),
        "page_no": int(visual.get("page_no") or 0),
        "artifact_type": str(visual.get("artifact_type") or ""),
        "bbox": list(visual.get("bbox") or []),
        "caption_excerpt": str(visual.get("caption_excerpt") or "")[:360],
        "nearby_text_excerpt": str(visual.get("nearby_text_excerpt") or "")[:500],
    }


def _page_candidate_metadata(
    page_ref: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    page_no = int(page_ref.get("page_no") or 0)
    return {
        "page_visual_id": str(page_ref.get("visual_id") or ""),
        "page_no": page_no,
        "child_candidates": [
            _candidate_metadata(item)
            for item in candidates
            if int(item.get("page_no") or 0) == page_no
        ],
    }


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
        if str(visual.get("artifact_type") or "") == "page_snapshot":
            page_metadata = _page_candidate_metadata(visual, batch.child_candidates)
            if page_metadata["child_candidates"]:
                content.append(
                    {
                        "type": "text",
                        "text": "[PAGE CHILD CANDIDATE METADATA]\n"
                        + json.dumps(
                            page_metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )
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
        "child_candidate_ids": list(batch.child_candidate_ids),
        "child_candidate_metadata": [
            _candidate_metadata(item) for item in batch.child_candidates
        ],
    }
    return (content, report) if return_report else content


_OBSERVATION_FIELDS = {
    "visual_id", "page_no", "bbox", "artifact_type", "visible_text", "title_or_caption",
    "axes_or_headers", "legend_or_notes", "quantitative_values", "relationships",
    "layout_observations", "ocr_conflicts", "confidence", "needs_manual_review",
}
_V2_OBSERVATION_FIELDS = _OBSERVATION_FIELDS | {
    "candidate_attribution_status",
    "raw_reinspection_candidates",
}
_RAW_REINSPECTION_FIELDS = {
    "candidate_visual_id",
    "evidence_kinds",
    "reason",
    "confidence",
    "requires_raw_reinspection",
}
_ATTRIBUTION_STATUSES = {"resolved", "ambiguous", "no_matching_candidate"}
_EVIDENCE_KINDS = {
    "quantitative_values",
    "significance_markers",
    "ocr_conflict",
    "relationships",
    "axes_or_headers",
    "visible_text",
    "layout_observations",
    "manual_review",
}
_ARTIFACT_TYPES = {"page_snapshot", "figure_crop", "table_crop", "formula_crop"}
_CONFIDENCE = {"high", "medium", "low"}


def _validate_visual_observations_v1(
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


def _validate_visual_observations_v2(
    payload: Mapping[str, Any],
    *,
    allowed_visual_ids: Sequence[str],
    expected_visual_refs: Sequence[Mapping[str, Any]] | None = None,
    sent_visual_ids: Sequence[str] | None = None,
    candidate_refs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate v2 page observations and model-to-object attribution."""

    if payload.get("artifact_version") != VISUAL_OBSERVATIONS_VERSION:
        raise ValueError("visual observation artifact_version is invalid")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        raise ValueError("visual observation payload must contain an observations array")
    for observation in raw_observations:
        if not isinstance(observation, Mapping):
            raise ValueError("visual observation entries must be objects")
        unknown_fields = set(observation) - _V2_OBSERVATION_FIELDS
        if unknown_fields:
            raise ValueError(
                f"visual observation has unexpected fields: {sorted(map(str, unknown_fields))}"
            )

    # Reuse the complete v1 field/type/coverage validator for the unchanged
    # page-level observation fields, then add the v2 attribution contract.
    base_payload = {
        "artifact_type": VISUAL_OBSERVATIONS_ARTIFACT_TYPE,
        "artifact_version": "v1",
        "observations": [
            {key: value for key, value in observation.items() if key in _OBSERVATION_FIELDS}
            for observation in raw_observations
        ],
    }
    base = _validate_visual_observations_v1(
        base_payload,
        allowed_visual_ids=allowed_visual_ids,
        expected_visual_refs=expected_visual_refs,
        sent_visual_ids=sent_visual_ids,
    )
    expected_children = [
        dict(item)
        for item in (candidate_refs or ())
        if isinstance(item, Mapping) and str(item.get("visual_id") or "")
    ]
    children_by_id: dict[str, dict[str, Any]] = {}
    child_order: dict[str, int] = {}
    for index, candidate in enumerate(expected_children):
        candidate_id = str(candidate.get("visual_id") or "")
        if candidate_id in children_by_id:
            raise ValueError(f"duplicate child candidate in page manifest: {candidate_id}")
        children_by_id[candidate_id] = candidate
        child_order[candidate_id] = index

    normalized_observations: list[dict[str, Any]] = []
    for observation, base_observation in zip(raw_observations, base["observations"]):
        visual_id = str(base_observation.get("visual_id") or "")
        page_no = int(base_observation.get("page_no") or 0)
        status = str(observation.get("candidate_attribution_status") or "").strip().casefold()
        if status not in _ATTRIBUTION_STATUSES:
            raise ValueError(f"visual observation candidate_attribution_status is invalid: {visual_id}")
        raw_candidates = observation.get("raw_reinspection_candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError(f"visual observation raw_reinspection_candidates must be an array: {visual_id}")
        normalized_candidates: list[dict[str, Any]] = []
        seen_candidates: set[str] = set()
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, Mapping):
                raise ValueError(f"visual observation raw reinspection candidate must be an object: {visual_id}")
            unknown_candidate_fields = set(raw_candidate) - _RAW_REINSPECTION_FIELDS
            if unknown_candidate_fields:
                raise ValueError(
                    f"visual observation candidate has unexpected fields: {sorted(map(str, unknown_candidate_fields))}"
                )
            candidate_id = str(raw_candidate.get("candidate_visual_id") or "").strip()
            if not candidate_id or candidate_id in seen_candidates:
                raise ValueError(f"duplicate or empty raw reinspection candidate: {candidate_id or visual_id}")
            candidate = children_by_id.get(candidate_id)
            if candidate is None:
                raise ValueError(f"unknown raw reinspection candidate: {candidate_id}")
            candidate_page = int(candidate.get("page_no") or 0)
            if candidate_page != page_no:
                raise ValueError(
                    f"raw reinspection candidate page mismatch: {candidate_id} != page {page_no}"
                )
            candidate_type = str(candidate.get("artifact_type") or "")
            if candidate_type not in _ARTIFACT_TYPES or candidate_type == "page_snapshot":
                raise ValueError(f"raw reinspection candidate artifact type is invalid: {candidate_id}")
            evidence_kinds = raw_candidate.get("evidence_kinds")
            if not isinstance(evidence_kinds, list) or not evidence_kinds:
                raise ValueError(f"raw reinspection candidate evidence_kinds is invalid: {candidate_id}")
            normalized_kinds = sorted(
                {str(kind).strip() for kind in evidence_kinds if str(kind).strip()}
            )
            if not normalized_kinds or any(kind not in _EVIDENCE_KINDS for kind in normalized_kinds):
                raise ValueError(f"raw reinspection candidate evidence_kinds is invalid: {candidate_id}")
            reason = str(raw_candidate.get("reason") or "").strip()
            if not reason:
                raise ValueError(f"raw reinspection candidate reason is missing: {candidate_id}")
            confidence = str(raw_candidate.get("confidence") or "").strip().casefold()
            if confidence not in _CONFIDENCE:
                raise ValueError(f"raw reinspection candidate confidence is invalid: {candidate_id}")
            requires_raw = raw_candidate.get("requires_raw_reinspection")
            if not isinstance(requires_raw, bool):
                raise ValueError(f"raw reinspection candidate requires_raw_reinspection is invalid: {candidate_id}")
            seen_candidates.add(candidate_id)
            normalized_candidates.append(
                {
                    "candidate_visual_id": candidate_id,
                    "evidence_kinds": normalized_kinds,
                    "reason": reason,
                    "confidence": confidence,
                    "requires_raw_reinspection": requires_raw,
                }
            )
        normalized_candidates.sort(
            key=lambda item: child_order.get(str(item.get("candidate_visual_id") or ""), 10**9)
        )
        if status == "no_matching_candidate" and normalized_candidates:
            raise ValueError(f"no_matching_candidate observation contains candidates: {visual_id}")
        if status == "resolved" and len(normalized_candidates) != 1:
            raise ValueError(f"resolved observation must identify exactly one candidate: {visual_id}")
        if status == "ambiguous" and len(normalized_candidates) < 2:
            raise ValueError(f"ambiguous observation must identify at least two candidates: {visual_id}")
        normalized_observations.append(
            {
                **base_observation,
                "candidate_attribution_status": status,
                "raw_reinspection_candidates": normalized_candidates,
            }
        )
    return {
        "artifact_type": VISUAL_OBSERVATIONS_ARTIFACT_TYPE,
        "artifact_version": VISUAL_OBSERVATIONS_VERSION,
        "observations": normalized_observations,
    }


def validate_visual_observations(
    payload: Mapping[str, Any],
    *,
    allowed_visual_ids: Sequence[str],
    expected_visual_refs: Sequence[Mapping[str, Any]] | None = None,
    sent_visual_ids: Sequence[str] | None = None,
    candidate_refs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate current v2 observations while retaining an explicit v1 reader."""

    if not isinstance(payload, Mapping):
        raise ValueError("visual observation payload must be an object")
    if payload.get("artifact_type") != VISUAL_OBSERVATIONS_ARTIFACT_TYPE:
        raise ValueError("visual observation artifact_type is invalid")
    version = str(payload.get("artifact_version") or "")
    if version == "v1":
        return _validate_visual_observations_v1(
            payload,
            allowed_visual_ids=allowed_visual_ids,
            expected_visual_refs=expected_visual_refs,
            sent_visual_ids=sent_visual_ids,
        )
    if version == VISUAL_OBSERVATIONS_VERSION:
        return _validate_visual_observations_v2(
            payload,
            allowed_visual_ids=allowed_visual_ids,
            expected_visual_refs=expected_visual_refs,
            sent_visual_ids=sent_visual_ids,
            candidate_refs=candidate_refs,
        )
    raise ValueError("visual observation artifact_version is invalid")


def select_final_visual_refs_after_scan(
    visual_refs: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    *,
    max_refs: int = 8,
) -> list[dict[str, Any]]:
    """Select raw crops/pages after scan evidence exists, deterministically.

    The first scan pass is intentionally page-only.  Crops therefore do not
    have a direct observation of their own; a crop is eligible when it belongs
    to a page with a validated observation and that observation contains
    evidence appropriate for the crop type.  This keeps the scan budget page
    based while allowing the final synthesis to receive a higher-resolution
    table, figure, or formula crop.
    """

    limit = max(0, int(max_refs))
    if limit == 0:
        return []
    by_id = {
        str(item.get("visual_id") or ""): dict(item)
        for item in observations
        if isinstance(item, Mapping) and str(item.get("visual_id") or "")
    }
    page_refs_by_page = {
        int(ref.get("page_no") or 0): dict(ref)
        for ref in visual_refs
        if isinstance(ref, Mapping)
        and str(ref.get("artifact_type") or "") == "page_snapshot"
        and int(ref.get("page_no") or 0) > 0
    }
    page_observations_by_page: dict[int, dict[str, Any]] = {}
    for observation in by_id.values():
        page_no = int(observation.get("page_no") or 0)
        if page_no <= 0:
            continue
        current = page_observations_by_page.get(page_no)
        # Prefer the observation whose id is also the page visual id.  This
        # makes the page -> crop provenance deterministic when a producer
        # returns an additional observation for the same page.
        page_ref = page_refs_by_page.get(page_no)
        if current is None or (
            page_ref is not None
            and str(observation.get("visual_id") or "")
            == str(page_ref.get("visual_id") or "")
        ):
            page_observations_by_page[page_no] = observation

    def _candidate_attributions(observation: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        raw = observation.get("raw_reinspection_candidates") if observation is not None else None
        if not isinstance(raw, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, Mapping) and str(item.get("candidate_visual_id") or ""):
                result[str(item.get("candidate_visual_id"))] = dict(item)
        return result

    page_attributions_by_page = {
        page_no: _candidate_attributions(observation)
        for page_no, observation in page_observations_by_page.items()
    }
    ambiguous_pages = {
        page_no
        for page_no, observation in page_observations_by_page.items()
        if str(observation.get("candidate_attribution_status") or "").strip().casefold() == "ambiguous"
        and len(page_attributions_by_page.get(page_no, {})) >= 2
    }

    def _items(observation: Mapping[str, Any], field: str) -> list[str]:
        value = observation.get(field)
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _joined_observation_text(observation: Mapping[str, Any]) -> str:
        fields = (
            "visible_text", "axes_or_headers", "legend_or_notes",
            "quantitative_values", "relationships", "layout_observations",
            "ocr_conflicts", "title_or_caption",
        )
        return " ".join(
            [str(observation.get("title_or_caption") or "")] + [
                text
                for field in fields
                if field != "title_or_caption"
                for text in _items(observation, field)
            ]
        ).casefold()

    def _bbox(value: Any) -> tuple[float, float, float, float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            x0, y0, x1, y1 = (float(item) for item in value)
        except (TypeError, ValueError):
            return None
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _overlap_ratio(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
        first = _bbox(left.get("bbox"))
        second = _bbox(right.get("bbox"))
        if first is None or second is None:
            return 0.0
        ix0, iy0 = max(first[0], second[0]), max(first[1], second[1])
        ix1, iy1 = min(first[2], second[2]), min(first[3], second[3])
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        if intersection <= 0:
            return 0.0
        first_area = (first[2] - first[0]) * (first[3] - first[1])
        second_area = (second[2] - second[0]) * (second[3] - second[1])
        return intersection / max(min(first_area, second_area), 1e-9)

    def _score(
        ref: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
        attribution: Mapping[str, Any] | None = None,
    ) -> tuple[float, dict[str, float], str]:
        artifact_type = str(ref.get("artifact_type") or "page_snapshot")
        components: dict[str, float] = {
            # The pre-scan heuristic is only a tie-breaker.  A crop with
            # substantive page evidence must beat a decorative crop whose
            # heuristic happened to be large.
            "selection_prior": min(max(float(ref.get("selection_score") or 0.0), 0.0), 4.0) * 0.25,
            "quantitative_values": 0.0,
            "relationships": 0.0,
            "axes_or_headers": 0.0,
            "visible_text": 0.0,
            "layout_observations": 0.0,
            "ocr_conflicts": 0.0,
            "manual_review": 0.0,
            "confidence": 0.0,
            "artifact_type_match": 0.0,
            "crop_detail": 0.0,
            "caption_linkage": 0.0,
            "explicit_attribution": 0.0,
            "requires_raw_reinspection": 0.0,
            "attribution_confidence": 0.0,
            "attribution_evidence": 0.0,
        }
        reasons: list[str] = []
        if attribution is not None:
            components["explicit_attribution"] = 100.0
            confidence = str(attribution.get("confidence") or "").casefold()
            components["attribution_confidence"] = {
                "high": 8.0,
                "medium": 4.0,
                "low": 1.0,
            }.get(confidence, 0.0)
            if bool(attribution.get("requires_raw_reinspection")):
                components["requires_raw_reinspection"] = 12.0
            evidence_weights = {
                "quantitative_values": 5.0,
                "significance_markers": 5.0,
                "ocr_conflict": 4.0,
                "relationships": 5.0,
                "axes_or_headers": 3.0,
                "visible_text": 1.0,
                "layout_observations": 3.0,
                "manual_review": 2.0,
            }
            components["attribution_evidence"] = sum(
                evidence_weights.get(str(kind), 0.0)
                for kind in (attribution.get("evidence_kinds") or [])
            )
            reasons.append(
                "explicit model attribution: "
                + str(attribution.get("candidate_visual_id") or "")
            )
            if str(attribution.get("confidence") or ""):
                reasons.append(f"attribution confidence={attribution.get('confidence')}")
            if attribution.get("requires_raw_reinspection"):
                reasons.append("model requires raw reinspection")
        if observation is None:
            reasons.append("page snapshot fallback: no validated page observation")
            return components["selection_prior"], components, "; ".join(reasons)

        quantitative = _items(observation, "quantitative_values")
        relationships = _items(observation, "relationships")
        axes = _items(observation, "axes_or_headers")
        visible = _items(observation, "visible_text")
        layout = _items(observation, "layout_observations")
        conflicts = _items(observation, "ocr_conflicts")
        text = _joined_observation_text(observation)
        components["quantitative_values"] = 3.0 * len(quantitative)
        components["relationships"] = 3.0 * len(relationships)
        components["axes_or_headers"] = 1.5 * len(axes)
        components["visible_text"] = 0.25 * len(visible)
        components["layout_observations"] = 1.5 * len(layout)
        components["ocr_conflicts"] = 1.25 * len(conflicts)
        if observation.get("needs_manual_review"):
            components["manual_review"] = 2.5
            reasons.append("manual review requested")
        components["confidence"] = {
            "high": 1.5,
            "medium": 0.75,
            "low": 0.25,
        }.get(str(observation.get("confidence") or ""), 0.0)

        if artifact_type == "table_crop":
            if quantitative or axes or "table" in text or "表" in text:
                components["artifact_type_match"] = 4.0
                reasons.append("table crop matches quantitative/header evidence")
        elif artifact_type == "figure_crop":
            mechanism_terms = (
                "relationship", "mechanism", "framework", "process", "workflow",
                "diagram", "node", "arrow", "causal", "pathway", "关系", "机制",
                "框架", "流程", "箭头",
            )
            if relationships or any(term in text for term in mechanism_terms):
                components["artifact_type_match"] = 4.5
                reasons.append("figure crop matches relationship/mechanism evidence")
            elif quantitative or axes:
                components["artifact_type_match"] = 1.0
        elif artifact_type == "formula_crop":
            formula_terms = (
                "equation", "formula", "regression", "coefficient", "beta", "β",
                "∑", "∫", "公式", "方程", "回归",
            )
            if layout or any(term in text for term in formula_terms):
                components["artifact_type_match"] = 5.0
                reasons.append("formula crop matches equation/layout evidence")
        else:
            components["artifact_type_match"] = 0.5

        if artifact_type != "page_snapshot":
            components["crop_detail"] = 2.0
            reasons.append("higher-resolution child crop available")
        caption = " ".join(
            str(ref.get(key) or "") for key in ("caption_excerpt", "nearby_text_excerpt")
        ).casefold()
        if caption and any(token in text for token in re.findall(r"[a-z0-9_\u4e00-\u9fff]+", caption)):
            components["caption_linkage"] = 1.0
            reasons.append("crop caption/nearby text linked to page observation")
        if quantitative:
            reasons.append(f"{len(quantitative)} quantitative value(s) observed")
        if relationships:
            reasons.append(f"{len(relationships)} relationship(s) observed")
        if conflicts:
            reasons.append(f"{len(conflicts)} OCR conflict(s) retained")
        if not reasons:
            reasons.append("page observation retained as bounded visual evidence")
        return sum(components.values()), components, "; ".join(reasons)

    candidates: list[tuple[float, int, str, dict[str, Any]]] = []
    for ref in visual_refs:
        if not isinstance(ref, Mapping):
            continue
        visual_id = str(ref.get("visual_id") or "")
        page_no = int(ref.get("page_no") or 0)
        artifact_type = str(ref.get("artifact_type") or "page_snapshot")
        direct_observation = by_id.get(visual_id)
        page_ref = page_refs_by_page.get(page_no)
        page_observation = page_observations_by_page.get(page_no)
        attribution = page_attributions_by_page.get(page_no, {}).get(visual_id)
        is_v2_page_observation = bool(
            page_observation is not None
            and "candidate_attribution_status" in page_observation
        )
        if artifact_type != "page_snapshot" and is_v2_page_observation:
            # Current v2 is fail-closed: a child without an explicit model
            # association does not inherit page-level quantitative or
            # relationship evidence merely because it shares a page.
            observation = page_observation if attribution is not None else None
        else:
            observation = direct_observation or page_observation
        # A child crop is scored from its source page observation.  A crop
        # without that source is not silently promoted; the page snapshot is
        # the only safe fallback for that page.
        if artifact_type != "page_snapshot" and direct_observation is None and page_observation is None:
            continue
        score, components, reason = _score(ref, observation, attribution)
        if artifact_type != "page_snapshot":
            evidence_strength = sum(
                components[name]
                for name in (
                    "quantitative_values", "relationships", "axes_or_headers",
                    "layout_observations", "ocr_conflicts", "artifact_type_match",
                )
            )
            if evidence_strength <= 0.0:
                continue
        selected_ref = dict(ref)
        source_page_visual_id = str(
            (page_ref or {}).get("visual_id") or ref.get("visual_id") or ""
        )
        source_observation_visual_id = str(
            (observation or {}).get("visual_id") or ""
        )
        selected_ref.update(
            {
                "post_scan_score": round(score, 4),
                "score_components": {key: round(value, 4) for key, value in components.items()},
                "selection_reason": reason,
                "source_page_visual_id": source_page_visual_id,
                "source_observation_visual_id": source_observation_visual_id,
                "object_attribution_status": (
                    str((page_observation or {}).get("candidate_attribution_status") or "legacy_same_page")
                    if artifact_type == "page_snapshot"
                    else (
                        str((page_observation or {}).get("candidate_attribution_status") or "")
                        if attribution is not None
                        else "not_attributed"
                    )
                ),
                "object_attribution_confidence": str(
                    (attribution or {}).get("confidence")
                    or (page_observation or {}).get("confidence")
                    or "low"
                ),
                "object_attribution_reason": str(
                    (attribution or {}).get("reason")
                    or (
                        "page-level safe fallback"
                        if artifact_type == "page_snapshot"
                        else "no explicit page-to-object association"
                    )
                ),
                "requires_raw_reinspection": bool(
                    (attribution or {}).get("requires_raw_reinspection")
                ),
                "attribution_ambiguous": bool(
                    page_no in ambiguous_pages
                    and (
                        artifact_type == "page_snapshot"
                        or attribution is not None
                    )
                ),
            }
        )
        candidates.append((-score, page_no, visual_id, selected_ref))
    candidates.sort(key=lambda item: item[:3])
    selected: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    ambiguous_fallback_pages: set[int] = set()
    ambiguous_reserved_pages: set[int] = set()
    for _neg_score, _page_no, _visual_id, ref in candidates:
        page_no = int(ref.get("page_no") or 0)
        artifact_type = str(ref.get("artifact_type") or "page_snapshot")
        if page_no in ambiguous_pages:
            candidate_ids = set(page_attributions_by_page.get(page_no, {}))
            if artifact_type != "page_snapshot":
                if page_no not in ambiguous_reserved_pages and page_no not in ambiguous_fallback_pages:
                    remaining = limit - len(selected)
                    if len(candidate_ids) > remaining:
                        ambiguous_fallback_pages.add(page_no)
                    else:
                        ambiguous_reserved_pages.add(page_no)
                if page_no in ambiguous_fallback_pages:
                    continue
            elif page_no in ambiguous_reserved_pages:
                # The explicitly attributed child set is the higher-detail
                # representation when the full ambiguous set fits the budget.
                continue
            elif page_no in ambiguous_fallback_pages:
                ref["selection_reason"] = (
                    str(ref.get("selection_reason") or "").rstrip("; ")
                    + "; ambiguous attribution; page snapshot safe fallback"
                )
                ref["object_attribution_reason"] = (
                    "ambiguous child attribution did not fit the raw-image budget; "
                    "retained page snapshot"
                )
        group = str(ref.get("dedupe_group_id") or "")
        if group and group in seen_groups and page_no not in ambiguous_reserved_pages:
            continue
        if artifact_type != "page_snapshot":
            if any(
                str(item.get("artifact_type") or "") != "page_snapshot"
                and int(item.get("page_no") or 0) == page_no
                and _overlap_ratio(item, ref) >= 0.72
                for item in selected
            ):
                continue
        elif any(
            str(item.get("artifact_type") or "") != "page_snapshot"
            and int(item.get("page_no") or 0) == page_no
            and float(item.get("post_scan_score") or 0.0) >= float(ref.get("post_scan_score") or 0.0) + 2.0
            for item in selected
        ):
            # A substantive crop is a better representation of the same page
            # than a low-resolution full-page duplicate.
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
        label = {
            "visual_id": visual_id,
            "page_no": int(visual.get("page_no") or 0),
            "bbox": list(visual.get("bbox") or []),
            "artifact_type": str(visual.get("artifact_type") or ""),
            "caption_excerpt": str(visual.get("caption_excerpt") or ""),
            "nearby_text_excerpt": str(visual.get("nearby_text_excerpt") or ""),
            "ocr_excerpt": str(ocr.get(visual_id) or ""),
        }
        if str(visual.get("artifact_type") or "") == "page_snapshot":
            label["child_candidates"] = _page_candidate_metadata(
                visual,
                batch.child_candidates,
            )["child_candidates"]
        labels.append(label)
    user_text = (
        "Visual scan batch metadata:\n"
        + json.dumps(
            {
                "batch_index": batch.batch_index,
                "objects": labels,
                "child_candidate_metadata": [
                    _candidate_metadata(item) for item in batch.child_candidates
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\nReturn exactly one observation for every image actually sent; use low confidence and manual review when unclear."
    )
    return user_text, registry.read(VISUAL_SCAN_PROMPT_ID)


__all__ = [
    "DEFAULT_MAX_REQUEST_IMAGE_BYTES", "DEFAULT_MAX_SINGLE_IMAGE_BYTES", "VISUAL_INPUT_IDENTITY_VERSION",
    "VISUAL_OBSERVATIONS_ARTIFACT_TYPE", "VISUAL_OBSERVATIONS_VERSION", "VISUAL_SCAN_PROMPT_ID",
    "VisualScanBatch", "build_visual_scan_prompt", "build_visual_scan_user_content",
    "estimate_encoded_image_bytes", "normalize_visual_byte_budgets", "plan_visual_scan_batches", "select_final_visual_refs_after_scan",
    "validate_visual_observations", "visual_label",
]

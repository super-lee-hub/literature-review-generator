from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF releases.
    import fitz  # type: ignore

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.config_values import parse_bounded_float, parse_strict_bool
from services.job_workspace import publish_bytes_artifact, publish_json_artifact, utc_now_iso
from services.queue_service import LocalPublicationContext


_VISUAL_KEYWORDS = (
    "figure",
    "fig.",
    "fig ",
    "table",
    "framework",
    "model",
    "process",
    "mechanism",
    "architecture",
    "workflow",
    "diagram",
    "schema",
    "illustrates",
    "shown in",
    "图",
    "表",
    "框架",
    "机制",
    "模型",
    "流程",
    "架构",
)

_STAGE1_VISUAL_SELECTION_VERSION = "stage1_visual_selection/v1"
_SELECTIVE_SELECTION_MODE = "selective"
_ADAPTIVE_PAGE_SCAN_MODE = "adaptive_page_scan"
_SELECTION_MODES = frozenset({_SELECTIVE_SELECTION_MODE, _ADAPTIVE_PAGE_SCAN_MODE})
_PAGE_LAYOUT_CUES = (
    "research framework",
    "conceptual framework",
    "theoretical framework",
    "conceptual model",
    "research model",
    "process model",
    "mechanism",
    "workflow",
    "diagram",
    "architecture",
    "研究框架",
    "概念框架",
    "理论框架",
    "概念模型",
    "机制",
    "流程",
    "架构",
    "图示",
)
_FIGURE_CAPTION_RE = re.compile(
    r"(?:^|\s)(?:fig(?:ure)?\.?|图)\s*[a-z]?\d+\b",
    flags=re.IGNORECASE,
)
_TABLE_CAPTION_RE = re.compile(
    r"(?:^|\s)(?:table|tab\.?|表)\s*[a-z]?\d+\b",
    flags=re.IGNORECASE,
)

_DEFAULT_SELECTION_POLICY: Dict[str, Any] = {
    "policy_name": "stage1_visual_selection_v1",
    "selection_contract_version": _STAGE1_VISUAL_SELECTION_VERSION,
    "selection_mode": _SELECTIVE_SELECTION_MODE,
    "text_is_primary": True,
    "supported_artifact_types": ["page_snapshot", "figure_crop", "table_crop", "formula_crop"],
    "deferred_artifact_types": [],
    "budgets": {
        "page_snapshot_soft_max": 4,
        "figure_crop_soft_max": 6,
        "table_crop_soft_max": 6,
        "formula_crop_soft_max": 4,
        "selected_visual_soft_total": 10,
        "selected_visual_hard_total": 16,
        # Compatibility aliases remain in the policy snapshot for older
        # readers, but the *_soft_max and selected_visual_* keys are current.
        "page_snapshot_max": 4,
        "figure_crop_max": 6,
        "table_crop_max": 6,
        "formula_crop_max": 4,
        "total_visuals_max": 10,
        "figure_crop_max_per_page": 2,
        "table_crop_max_per_page": 2,
        "formula_crop_max_per_page": 2,
    },
    "rendering": {
        "page_long_edge_px": 2200,
        "crop_long_edge_px": 2400,
        "page_max_pixels": 16000000,
        "crop_max_pixels": 16000000,
        "page_format": "jpeg",
        "page_jpeg_quality": 92,
        "crop_format": "png",
        "crop_padding_ratio": 0.04,
        "max_visual_artifact_bytes": 24000000,
    },
    "rendering_safety": {
        "max_rendered_pixels": 16000000,
        "max_rendered_dimension_px": 8000,
        "max_visual_artifact_bytes": 24000000,
    },
    "selection_signals": [
        "structured_page_metadata",
        "figure_table_formula_captions",
        "framework_and_mechanism_keywords",
        "nearby_caption_and_context_cues",
        "ocr_and_text_layer_quality",
        "deterministic_crop_candidate_sorting",
        "per_page_diversity_limits",
        "decorative_small_block_filtering",
        "adaptive_scan_heavy_escalation",
    ],
}


def _truncate(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _normalize_bbox(value: Any) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return []
    bbox: List[float] = []
    for item in value:
        try:
            bbox.append(round(float(item), 2))
        except (TypeError, ValueError):
            return []
    x0, y0, x1, y1 = bbox
    if x1 <= x0 or y1 <= y0:
        return []
    return bbox


def _block_text(block: Mapping[str, Any]) -> str:
    collected: List[str] = []
    for line in block.get("lines", []) if isinstance(block.get("lines"), list) else []:
        if not isinstance(line, Mapping):
            continue
        for span in line.get("spans", []) if isinstance(line.get("spans"), list) else []:
            if not isinstance(span, Mapping):
                continue
            text = str(span.get("text") or "").strip()
            if text:
                collected.append(text)
    return " ".join(collected).strip()


def _count_keyword_hits(text: str) -> int:
    lowered = text.casefold()
    hits = 0
    for keyword in _VISUAL_KEYWORDS:
        hits += lowered.count(keyword.casefold())
    return hits


def _count_cues(text: str, cues: Iterable[str]) -> int:
    lowered = str(text or "").casefold()
    return sum(lowered.count(str(cue).casefold()) for cue in cues)


def _budget_value(
    budgets: Mapping[str, Any],
    current_key: str,
    legacy_key: str,
    default: int,
) -> int:
    raw = budgets.get(current_key, budgets.get(legacy_key, default))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return max(0, int(default))


def _load_json(path: str) -> Any:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _paper_hash(paper_key: str) -> str:
    return hashlib.sha256(paper_key.encode("utf-8")).hexdigest()[:16]


def _normalize_image_format(value: Any, default: str, *, strict: bool = False) -> str:
    normalized = str(value or default).strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpeg"
    if normalized == "png":
        return "png"
    if strict:
        raise ValueError(f"unsupported visual image format: {value}")
    return default


@dataclass(frozen=True)
class VisualArtifactRecord:
    visual_id: str
    artifact_id: str
    paper_key: str
    source_pdf: str
    page_no: int
    bbox: List[float]
    artifact_type: str
    source_type: str
    image_path: str
    caption_excerpt: str
    nearby_text_excerpt: str
    selection_reason: str
    selection_score: float
    dedupe_group_id: str
    width: int = 0
    height: int = 0
    render_scale: float = 0.0
    estimated_dpi: int = 0
    image_format: str = ""
    image_bytes: int = 0
    image_sha256: str = ""
    selection_required: bool = True
    selection_status: str = "required"

    def to_ref(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Stage1VisualBundle:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    paper_key: str
    source_pdf: str
    bundle_path: str
    visual_manifest_path: str
    selected_visual_refs: List[Dict[str, Any]]
    selection_policy_snapshot: Dict[str, Any]
    bundle_metadata: Dict[str, Any]
    all_visual_refs: List[Dict[str, Any]] = None  # type: ignore[assignment]
    coverage_report: Dict[str, Any] = None  # type: ignore[assignment]
    selection_report: Dict[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["all_visual_refs"] = list(self.all_visual_refs or self.selected_visual_refs)
        payload["coverage_report"] = dict(self.coverage_report or {})
        payload["selection_report"] = dict(self.selection_report or {})
        return payload


class Stage1VisualArtifactBuilder:
    """Create a small, durable, traceable visual bundle for stage-one analysis."""

    def __init__(self, *, logger: Any = None):
        self.logger = logger

    def build_bundle(
        self,
        *,
        job_id: str,
        paper_key: str,
        paper_info: Mapping[str, Any],
        source_pdf: str,
        output_dir: str,
        artifact_registry: ArtifactRegistry,
        preprocess_metadata: Mapping[str, Any] | None = None,
        visual_settings: Mapping[str, Any] | None = None,
    ) -> Optional[Stage1VisualBundle]:
        if not source_pdf or not os.path.exists(source_pdf):
            return None

        policy = json.loads(json.dumps(_DEFAULT_SELECTION_POLICY))
        configured_visual = dict(visual_settings or {})
        rendering = policy["rendering"]
        render_all = parse_strict_bool(
            configured_visual.get("render_all_nonblank_pages"),
            field="Stage1_Visual.render_all_nonblank_pages",
            default=False,
        )
        configured_mode = str(
            configured_visual.get("selection_mode") or _SELECTIVE_SELECTION_MODE
        ).strip().casefold()
        if configured_mode not in _SELECTION_MODES:
            raise ValueError(
                "Stage1_Visual.selection_mode must be selective or adaptive_page_scan"
            )
        escalation_reasons: list[str] = []
        if render_all:
            # Keep the old switch readable as an explicit exceptional request.
            # It no longer defines the shipped/default behavior.
            configured_mode = _ADAPTIVE_PAGE_SCAN_MODE
            escalation_reasons.append("explicit_all_page_request")
        for key, target in (
            ("page_long_edge_px", "page_long_edge_px"),
            ("crop_long_edge_px", "crop_long_edge_px"),
            ("page_max_pixels", "page_max_pixels"),
            ("crop_max_pixels", "crop_max_pixels"),
            ("page_format", "page_format"),
            ("page_jpeg_quality", "page_jpeg_quality"),
            ("crop_format", "crop_format"),
            ("crop_padding_ratio", "crop_padding_ratio"),
            ("max_visual_artifact_bytes", "max_visual_artifact_bytes"),
        ):
            if key in configured_visual and str(configured_visual[key]).strip() != "":
                raw_value = configured_visual[key]
                if key in {"page_format", "crop_format"}:
                    rendering[target] = _normalize_image_format(raw_value, rendering[target], strict=True)
                else:
                    if key == "crop_padding_ratio":
                        rendering[target] = parse_bounded_float(
                            raw_value,
                            field="Stage1_Visual.crop_padding_ratio",
                            minimum=0.0,
                            maximum=0.25,
                        )
                    else:
                        try:
                            rendering[target] = (
                                float(raw_value) if "." in str(raw_value) else int(raw_value)
                            )
                        except (TypeError, ValueError) as exc:
                            raise ValueError(f"invalid Stage1_Visual.{key}: {raw_value}") from exc
        rendering["page_format"] = _normalize_image_format(rendering.get("page_format"), "jpeg", strict=True)
        rendering["crop_format"] = _normalize_image_format(rendering.get("crop_format"), "png", strict=True)
        policy["rendering_safety"]["max_rendered_pixels"] = int(
            rendering.get("page_max_pixels") or policy["rendering_safety"]["max_rendered_pixels"]
        )
        policy["rendering_safety"]["max_visual_artifact_bytes"] = int(
            rendering.get("max_visual_artifact_bytes")
            or policy["rendering_safety"]["max_visual_artifact_bytes"]
        )
        for key in (
            "page_snapshot_soft_max",
            "figure_crop_soft_max",
            "table_crop_soft_max",
            "formula_crop_soft_max",
            "selected_visual_soft_total",
            "selected_visual_hard_total",
        ):
            if key not in configured_visual or str(configured_visual[key]).strip() == "":
                continue
            try:
                parsed_budget = int(configured_visual[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid Stage1_Visual.{key}: {configured_visual[key]}") from exc
            if parsed_budget < 0:
                raise ValueError(f"invalid Stage1_Visual.{key}: must be non-negative")
            policy["budgets"][key] = parsed_budget
        preprocess_metadata = dict(preprocess_metadata or {})
        bundle_dir = os.path.abspath(output_dir)
        os.makedirs(bundle_dir, exist_ok=True)
        bundle_path = os.path.join(bundle_dir, "visual_bundle.json")
        manifest_path = os.path.join(bundle_dir, "visual_manifest.json")
        publication_context = (
            getattr(artifact_registry, "publication_context", None)
            or LocalPublicationContext()
        )

        page_index = self._load_page_index(preprocess_metadata)
        page_blocks = self._load_page_blocks(preprocess_metadata)
        if not page_index or not page_blocks:
            fallback_page_index, fallback_page_blocks = self._extract_pdf_page_data(source_pdf)
            if not page_index:
                page_index = fallback_page_index
            if not page_blocks:
                page_blocks = fallback_page_blocks

        pdf_hash = file_sha256(source_pdf)
        depends_on = self._build_base_dependencies(
            source_pdf,
            pdf_hash,
            preprocess_metadata,
            artifact_registry,
        )
        if configured_mode == _SELECTIVE_SELECTION_MODE:
            escalation_reasons.extend(
                self._derive_escalation_reasons(page_index, preprocess_metadata)
            )
            if escalation_reasons:
                configured_mode = _ADAPTIVE_PAGE_SCAN_MODE
        policy["selection_mode"] = configured_mode
        policy["render_all_nonblank_pages"] = bool(
            configured_mode == _ADAPTIVE_PAGE_SCAN_MODE and render_all
        )
        policy["escalation"] = {
            "enabled": configured_mode == _ADAPTIVE_PAGE_SCAN_MODE,
            "reasons": list(dict.fromkeys(escalation_reasons)),
            "source": (
                "explicit_or_preprocess_quality"
                if escalation_reasons
                else "not_triggered"
            ),
        }
        artifact_hash = hashlib.sha256(
            (
                f"{paper_key}|{_STAGE1_VISUAL_SELECTION_VERSION}|"
                f"{json.dumps(policy, ensure_ascii=False, sort_keys=True)}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        if configured_mode == _ADAPTIVE_PAGE_SCAN_MODE:
            page_candidates = self._select_all_nonblank_page_candidates(
                page_index,
                policy,
                reasons=escalation_reasons,
            )
        else:
            page_candidates = self._select_page_candidates(page_index, policy)
        figure_candidates = self._select_figure_candidates(page_blocks, page_index, policy)
        layout_candidates = self._select_layout_candidates(page_blocks, page_index, policy)
        if not parse_strict_bool(
            configured_visual.get("table_crop_enabled"),
            field="Stage1_Visual.table_crop_enabled",
            default=True,
        ):
            layout_candidates = [item for item in layout_candidates if item.get("artifact_type") != "table_crop"]
        if not parse_strict_bool(
            configured_visual.get("formula_crop_enabled"),
            field="Stage1_Visual.formula_crop_enabled",
            default=True,
        ):
            layout_candidates = [item for item in layout_candidates if item.get("artifact_type") != "formula_crop"]

        selected_pages, selected_crops, selection_report = self._select_visual_candidates(
            page_candidates=page_candidates,
            figure_candidates=figure_candidates,
            layout_candidates=layout_candidates,
            policy=policy,
            adaptive_page_scan=configured_mode == _ADAPTIVE_PAGE_SCAN_MODE,
        )

        render_dir = tempfile.mkdtemp(prefix=".visual-render-", dir=bundle_dir)
        try:
            selected_visuals, materialization_failures = self._materialize_visuals(
                source_pdf=source_pdf,
                page_candidates=selected_pages,
                figure_candidates=selected_crops,
                layout_candidates=[],
                policy=policy,
                bundle_dir=render_dir,
                paper_key=paper_key,
                artifact_hash=artifact_hash,
            )
            failed_visual_ids = [
                str(item.get("visual_id") or "")
                for item in materialization_failures
                if str(item.get("visual_id") or "")
            ]
            failed_required_visual_ids = [
                str(item.get("visual_id") or "")
                for item in materialization_failures
                if str(item.get("visual_id") or "") and bool(item.get("selection_required", True))
            ]

            created_at = utc_now_iso()
            budget_decisions = {
                "candidate_counts": {
                    "page_snapshot": len(page_candidates),
                    "figure_crop": len(figure_candidates),
                    "table_crop": sum(1 for item in layout_candidates if item.get("artifact_type") == "table_crop"),
                    "formula_crop": sum(1 for item in layout_candidates if item.get("artifact_type") == "formula_crop"),
                },
                "selected_counts": {
                    "page_snapshot": sum(1 for item in selected_visuals if item.artifact_type == "page_snapshot"),
                    "figure_crop": sum(1 for item in selected_visuals if item.artifact_type == "figure_crop"),
                    "table_crop": sum(1 for item in selected_visuals if item.artifact_type == "table_crop"),
                    "formula_crop": sum(1 for item in selected_visuals if item.artifact_type == "formula_crop"),
                    "total": len(selected_visuals),
                },
                "deferred_artifact_types": [],
                "selection_mode": configured_mode,
                "selection_contract_version": _STAGE1_VISUAL_SELECTION_VERSION,
                "selection_report": dict(selection_report),
            }

            published_visuals: list[VisualArtifactRecord] = []
            visual_records: list[Any] = []
            for visual in selected_visuals:
                image_target = os.path.join(bundle_dir, os.path.basename(visual.image_path))
                image_record = publish_bytes_artifact(
                    publication_context,
                    artifact_registry,
                    image_target,
                    Path(visual.image_path).read_bytes(),
                    artifact_role=visual.artifact_type,
                    artifact_type=visual.artifact_type,
                    artifact_version="v1",
                    producer="preprocess.visual_artifacts.Stage1VisualArtifactBuilder",
                    depends_on=depends_on,
                    artifact_id=visual.artifact_id,
                )
                visual_records.append(image_record)
                published_visuals.append(replace(visual, image_path=image_record.path))

            selection_report["selected_visual_ids"] = [
                str(item.visual_id) for item in published_visuals if str(item.visual_id)
            ]
            selection_report["selected_visual_count"] = len(published_visuals)

            manifest_payload = {
                "artifact_type": "visual_manifest",
                "artifact_version": "v1",
                "created_from_job_id": job_id,
                "created_at": created_at,
                "paper_key": paper_key,
                "paper_title": str(paper_info.get("title") or ""),
                "source_pdf": source_pdf,
                "bundle_dir": bundle_dir,
                "selection_policy": policy,
                "budget_decisions": budget_decisions,
                "selection_report": selection_report,
                # Explicit current-authority proof for the selective contract.
                # Registry fallback may only promote a manifest that carries
                # this self-declaration; legacy all-page manifests without it
                # remain readable for audit but never become the authority.
                "selection_authority": {
                    "current": True,
                    "role": "authority",
                    "job_id": job_id,
                    "selection_mode": configured_mode,
                    "selection_contract_version": _STAGE1_VISUAL_SELECTION_VERSION,
                    "policy_name": str(policy.get("policy_name") or ""),
                },
                "visuals": [item.to_ref() for item in published_visuals],
            }
            nonblank_pages = [
                int(item.get("page_no") or item.get("page_number") or 0)
                for item in page_index
                if int(item.get("page_no") or item.get("page_number") or 0) > 0
                and not bool(item.get("is_blank"))
            ]
            rendered_page_numbers = {
                int(item.page_no)
                for item in published_visuals
                if item.artifact_type == "page_snapshot"
            }
            page_status = []
            for page_no in range(1, len(page_index) + 1):
                if page_no not in nonblank_pages:
                    page_status.append({"page_no": page_no, "status": "skipped_blank", "skipped_reason": "blank_page"})
                elif page_no in rendered_page_numbers:
                    page_status.append({"page_no": page_no, "status": "rendered", "skipped_reason": ""})
                elif configured_mode == _ADAPTIVE_PAGE_SCAN_MODE:
                    page_status.append({"page_no": page_no, "status": "render_failed", "skipped_reason": "render_failed_or_safety_limit"})
                else:
                    page_status.append({"page_no": page_no, "status": "not_selected", "skipped_reason": "selective_gate_not_required"})
            selected_ids = [
                str(item.visual_id) for item in published_visuals if str(item.visual_id)
            ]
            required_ids = [
                str(item.visual_id)
                for item in published_visuals
                if str(item.visual_id)
                and (
                    configured_mode == _ADAPTIVE_PAGE_SCAN_MODE
                    and item.artifact_type == "page_snapshot"
                    or configured_mode != _ADAPTIVE_PAGE_SCAN_MODE
                    and bool(item.selection_required)
                )
            ]
            optional_ids = [
                str(item.visual_id)
                for item in published_visuals
                if str(item.visual_id)
                and str(item.visual_id) not in set(required_ids)
            ]
            is_adaptive = configured_mode == _ADAPTIVE_PAGE_SCAN_MODE
            initial_evidence_status = (
                "incomplete"
                if required_ids or failed_required_visual_ids
                else "not_required"
            )
            unresolved_unit_ids = list(
                dict.fromkeys([*required_ids, *failed_required_visual_ids])
            )
            all_required_ids = list(
                dict.fromkeys([*required_ids, *failed_required_visual_ids])
            )
            materialization_omissions = [
                {
                    "visual_id": str(item.get("visual_id") or ""),
                    "page_no": int(item.get("page_no") or 0),
                    "reason": str(item.get("reason") or "render_failed"),
                    "scope": "selected_visual_extraction",
                    "authority_blocking": True,
                    "artifact_type": str(item.get("artifact_type") or ""),
                }
                for item in materialization_failures
                if str(item.get("visual_id") or "")
            ]
            failed_only_optional = bool(failed_visual_ids) and not failed_required_visual_ids
            coverage_report = {
                "artifact_type": "stage1_visual_coverage",
                "artifact_version": "v2",
                "job_id": job_id,
                "paper_key": paper_key,
                "total_pdf_pages": len(page_index),
                "nonblank_pages": len(nonblank_pages),
                "rendered_pages": len(rendered_page_numbers),
                "visually_scanned_pages": 0,
                "skipped_pages": sum(1 for item in page_status if item["status"] == "skipped_blank"),
                "failed_pages": sum(1 for item in page_status if is_adaptive and item["status"] == "render_failed"),
                "page_status": page_status,
                "selected_crops": [item.to_ref() for item in published_visuals if item.artifact_type != "page_snapshot"],
                "scan_batches": [],
                "coverage_status": "complete" if not is_adaptive or not nonblank_pages else "partial",
                "scan_coverage_status": "planned" if is_adaptive and nonblank_pages else "not_required",
                "final_synthesis_modality": "text_only",
                "final_raw_visual_recheck_status": "not_required",
                "evidence_coverage_status": initial_evidence_status,
                "raw_reinspection_units": [],
                "required_raw_reinspection_unit_count": 0,
                "closed_raw_reinspection_unit_count": 0,
                "unresolved_raw_reinspection_unit_ids": [],
                "omissions": [
                    {
                        "visual_id": f"page-{int(item['page_no']):03d}",
                        "page_no": int(item["page_no"]),
                        "reason": str(item.get("skipped_reason") or "render_failed"),
                        "scope": "page_coverage",
                        "authority_blocking": True,
                    }
                    for item in page_status
                    if is_adaptive and item["status"] == "render_failed"
                ]
                + materialization_omissions,
                "selection_mode": configured_mode,
                "selection_contract_version": _STAGE1_VISUAL_SELECTION_VERSION,
                "visual_selection_status": (
                    "incomplete"
                    if failed_required_visual_ids
                    else ("degraded" if failed_only_optional else ("complete" if selected_ids else "not_required"))
                ),
                "required_visual_unit_count": len(all_required_ids),
                "required_visual_unit_ids": all_required_ids,
                "optional_visual_unit_ids": optional_ids,
                "selected_visual_unit_ids": selected_ids,
                "inspected_visual_unit_ids": [],
                "unresolved_visual_unit_ids": unresolved_unit_ids,
                "materialization_failed_unit_ids": failed_visual_ids,
                "escalation_reasons": list(dict.fromkeys(escalation_reasons)),
                "visual_extraction_strategy": (
                    "adaptive_page_scan" if is_adaptive else "direct_synthesis_visuals"
                ),
            }
            manifest_payload["coverage_report"] = coverage_report
            manifest_dependencies = [*depends_on, *(
                ArtifactDependencyRefV2.from_record(record)
                for record in visual_records
            )]
            manifest_record = publish_json_artifact(
                publication_context,
                artifact_registry,
                manifest_path,
                manifest_payload,
                artifact_role="visual_manifest",
                artifact_type="visual_manifest",
                artifact_version="v1",
                producer="preprocess.visual_artifacts.Stage1VisualArtifactBuilder",
                depends_on=manifest_dependencies,
                artifact_id=f"visual_manifest:{artifact_hash}",
            )

            coverage_record = publish_json_artifact(
                publication_context,
                artifact_registry,
                os.path.join(bundle_dir, "visual_coverage.json"),
                coverage_report,
                artifact_role="stage1_visual_coverage",
                artifact_type="stage1_visual_coverage",
                artifact_version="v2",
                producer="preprocess.visual_artifacts.Stage1VisualArtifactBuilder",
                depends_on=manifest_dependencies,
                artifact_id=f"stage1_visual_coverage:{artifact_hash}",
            )
            coverage_report = {
                **coverage_report,
                "coverage_artifact_path": coverage_record.path,
                "coverage_artifact_hash": coverage_record.content_hash,
            }

            bundle = Stage1VisualBundle(
                artifact_type="stage1_visual_bundle",
                artifact_version="v1",
                created_from_job_id=job_id,
                created_at=created_at,
                paper_key=paper_key,
                source_pdf=source_pdf,
                bundle_path=bundle_path,
                visual_manifest_path=manifest_record.path,
                selected_visual_refs=[item.to_ref() for item in published_visuals],
                selection_policy_snapshot=policy,
                bundle_metadata=budget_decisions,
                all_visual_refs=[item.to_ref() for item in published_visuals],
                coverage_report=coverage_report,
                selection_report=selection_report,
            )
            bundle_dependencies = [
                ArtifactDependencyRefV2.from_record(manifest_record),
                ArtifactDependencyRefV2.from_record(coverage_record),
                *(
                ArtifactDependencyRefV2.from_record(record)
                for record in visual_records
                ),
            ]
            bundle_record = publish_json_artifact(
                publication_context,
                artifact_registry,
                bundle_path,
                bundle.to_dict(),
                artifact_role="visual_bundle",
                artifact_type="stage1_visual_bundle",
                artifact_version="v1",
                producer="preprocess.visual_artifacts.Stage1VisualArtifactBuilder",
                depends_on=bundle_dependencies,
                artifact_id=f"stage1_visual_bundle:{artifact_hash}",
            )
            return replace(
                bundle,
                bundle_path=bundle_record.path,
                visual_manifest_path=manifest_record.path,
            )
        finally:
            shutil.rmtree(render_dir, ignore_errors=True)

    def _build_base_dependencies(
        self,
        source_pdf: str,
        pdf_hash: str,
        preprocess_metadata: Mapping[str, Any],
        artifact_registry: ArtifactRegistry,
    ) -> List[ArtifactDependencyRefV2]:
        source_dependency = self._registered_input_dependency(
            artifact_registry,
            artifact_type="source_pdf",
            path=source_pdf,
        )
        if source_dependency.content_hash != pdf_hash:
            raise ValueError("source PDF hash changed before visual artifact registration")
        depends_on = [source_dependency]
        for artifact_type, key in (
            ("preprocess_manifest", "manifest_path"),
            ("preprocess_page_index", "page_index_path"),
            ("preprocess_structured_json", "structured_json_path"),
        ):
            path = str(preprocess_metadata.get(key) or "").strip()
            if path and os.path.exists(path):
                depends_on.append(
                    self._registered_input_dependency(
                        artifact_registry,
                        artifact_type=artifact_type,
                        path=path,
                    )
                )
        return depends_on

    @staticmethod
    def _registered_input_dependency(
        artifact_registry: ArtifactRegistry,
        *,
        artifact_type: str,
        path: str,
    ) -> ArtifactDependencyRefV2:
        resolved_path = os.path.abspath(path)
        normalized_path = os.path.normcase(resolved_path)
        candidates = [
            item
            for item in artifact_registry.list_records()
            if os.path.normcase(os.path.abspath(item.path)) == normalized_path
            and item.artifact_type == artifact_type
        ]
        if len(candidates) > 1:
            raise ValueError(
                f"ambiguous Registry identity for visual input {artifact_type}: {resolved_path}"
            )
        record = candidates[0] if candidates else None
        if record is not None and record.status != "ready":
            raise ValueError(
                f"visual input dependency is not ready: {record.artifact_id} ({record.status})"
            )
        if record is None:
            path_hash = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:16]
            record = artifact_registry.register_file(
                artifact_role="visual_input",
                artifact_type=artifact_type,
                artifact_version="v1",
                path=resolved_path,
                producer="preprocess.visual_artifacts.Stage1VisualArtifactBuilder",
                artifact_id=f"visual-input:{artifact_type}:{path_hash}",
            )
        return ArtifactDependencyRefV2(
            artifact_type=record.artifact_type,
            path=record.path,
            content_hash=record.content_hash,
            dependency_kind="local_job",
            job_id=record.job_id,
            artifact_id=record.artifact_id,
        )

    def _load_page_index(self, preprocess_metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
        page_index_path = str(preprocess_metadata.get("page_index_path") or "").strip()
        payload = _load_json(page_index_path)
        return payload if isinstance(payload, list) else []

    def _load_page_blocks(self, preprocess_metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
        structured_json_path = str(preprocess_metadata.get("structured_json_path") or "").strip()
        payload = _load_json(structured_json_path)
        if isinstance(payload, Mapping):
            pages = payload.get("pages")
            if isinstance(pages, list):
                return [dict(item) for item in pages if isinstance(item, Mapping)]
        return []

    def _extract_pdf_page_data(self, source_pdf: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        page_index: List[Dict[str, Any]] = []
        page_blocks: List[Dict[str, Any]] = []
        doc = fitz.open(source_pdf)
        try:
            for page_number in range(doc.page_count):
                page = doc.load_page(page_number)
                text = str(page.get_text("text") or "")
                blocks = page.get_text("dict")
                page_index.append(
                    {
                        "page_number": page_number + 1,
                        "text": text,
                        "text_length": len(text.strip()),
                        "image_count": len(page.get_images(full=True)),
                        "block_count": len(blocks.get("blocks", [])) if isinstance(blocks, Mapping) else 0,
                        "scanned_candidate": False,
                        "used_ocr": False,
                        "low_quality": len(text.strip()) < 80,
                    }
                )
                page_blocks.append(
                    {
                        "page_number": page_number + 1,
                        "text": text,
                        "image_count": len(page.get_images(full=True)),
                        "blocks": blocks,
                    }
                )
        finally:
            doc.close()
        return page_index, page_blocks

    def _derive_escalation_reasons(
        self,
        page_index: Sequence[Mapping[str, Any]],
        preprocess_metadata: Mapping[str, Any],
    ) -> List[str]:
        """Return deterministic reasons for exceptional page-level inspection.

        This gate is intentionally based only on persisted preprocessing facts.
        It never calls a provider to decide whether a provider call is needed.
        """

        pages = [item for item in page_index if isinstance(item, Mapping)]
        if not pages:
            return []
        reasons: List[str] = []
        total = len(pages)
        scanned_pages = sum(1 for item in pages if bool(item.get("scanned_candidate")))
        low_quality_pages = sum(1 for item in pages if bool(item.get("low_quality")))
        low_quality_text_only_pages = sum(
            1
            for item in pages
            if bool(item.get("low_quality"))
            and int(item.get("image_count") or 0) <= 0
            and not bool(item.get("scanned_candidate"))
        )
        ocr_pages = sum(1 for item in pages if bool(item.get("used_ocr")))
        reliable_pages = sum(
            1
            for item in pages
            if (
                int(item.get("text_length") or len(str(item.get("text") or "").strip())) >= 80
                or int(item.get("image_count") or 0) > 0
            )
            and not bool(item.get("scanned_candidate"))
        )
        if bool(preprocess_metadata.get("scanned_like")) and scanned_pages / total >= 0.25:
            reasons.append("scanned_page_ratio")
        if low_quality_text_only_pages / total >= 0.50:
            reasons.append("low_text_coverage")
        if ocr_pages / total >= 0.50:
            reasons.append("ocr_coverage_low_confidence")
        if reliable_pages / total < 0.60:
            reasons.append("low_text_layer_coverage")
        return list(dict.fromkeys(reasons))

    def _select_all_nonblank_page_candidates(
        self,
        page_index: Iterable[Mapping[str, Any]],
        policy: Mapping[str, Any],
        *,
        reasons: Sequence[str],
    ) -> List[Dict[str, Any]]:
        """Build the explicit exceptional page-scan plan.

        The exceptional path deliberately bypasses the selective soft caps;
        its reason is persisted and every page remains an auditable required
        visual unit.
        """

        result: List[Dict[str, Any]] = []
        reason_text = ", ".join(str(item) for item in reasons if str(item))
        for item in page_index:
            page_no = int(item.get("page_number", 0) or 0)
            if page_no <= 0:
                continue
            text = str(item.get("text") or "")
            result.append(
                {
                    "page_no": page_no,
                    "bbox": [],
                    "score": 100.0,
                    "selection_reason": (
                        "adaptive_page_scan:" + reason_text
                        if reason_text
                        else "adaptive_page_scan"
                    ),
                    "caption_excerpt": "",
                    "nearby_text_excerpt": _truncate(text),
                    "dedupe_group_id": f"page:{page_no}",
                    "selection_required": True,
                    "page_snapshot_preferred": True,
                }
            )
        return result

    def _select_page_candidates(
        self,
        page_index: Iterable[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for item in page_index:
            page_no = int(item.get("page_number", 0) or 0)
            if page_no <= 0:
                continue
            text = str(item.get("text") or "")
            image_count = int(item.get("image_count", 0) or 0)
            cue_hits = _count_keyword_hits(text)
            layout_hits = _count_cues(text, _PAGE_LAYOUT_CUES)
            figure_caption_hits = len(_FIGURE_CAPTION_RE.findall(text))
            score = float(min(image_count, 4) * 1.5 + layout_hits * 3.0 + figure_caption_hits * 2.0)
            is_low_quality = bool(item.get("low_quality"))
            is_scanned = bool(item.get("scanned_candidate"))
            uses_ocr = bool(item.get("used_ocr"))
            page_level_required = bool(
                (is_low_quality and image_count == 0)
                or is_scanned
                or uses_ocr
                or (layout_hits >= 2 and figure_caption_hits == 0 and image_count == 0)
            )
            if image_count <= 0 and not page_level_required and figure_caption_hits <= 0:
                continue
            if score <= 0:
                continue
            reason_parts: List[str] = []
            if image_count > 0:
                reason_parts.append(f"image_count:{image_count}")
            if layout_hits > 0:
                reason_parts.append(f"layout_cues:{layout_hits}")
            if figure_caption_hits > 0:
                reason_parts.append(f"figure_captions:{figure_caption_hits}")
            if is_low_quality:
                reason_parts.append("low_quality")
            if is_scanned:
                reason_parts.append("scanned_candidate")
            if uses_ocr:
                reason_parts.append("used_ocr")

            candidates.append(
                {
                    "page_no": page_no,
                    "bbox": [],
                    "score": round(score, 2),
                    "selection_reason": ", ".join(reason_parts) or "layout_sensitive_page",
                    "caption_excerpt": "",
                    "nearby_text_excerpt": _truncate(text),
                    "dedupe_group_id": f"page:{page_no}",
                    "selection_required": page_level_required,
                    "page_snapshot_preferred": page_level_required,
                }
            )

        candidates.sort(
            key=lambda item: (
                -int(bool(item.get("selection_required"))),
                -float(item.get("score") or 0.0),
                int(item["page_no"]),
            )
        )
        limit = _budget_value(
            policy.get("budgets", {}) if isinstance(policy, Mapping) else {},
            "page_snapshot_soft_max",
            "page_snapshot_max",
            4,
        )
        required = [item for item in candidates if bool(item.get("selection_required"))]
        optional = [item for item in candidates if not bool(item.get("selection_required"))]
        return required + optional[: max(0, limit - len(required))]

    def _select_figure_candidates(
        self,
        page_blocks: Iterable[Mapping[str, Any]],
        page_index: Iterable[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        page_text_by_no = {
            int(item.get("page_number", 0) or 0): str(item.get("text") or "")
            for item in page_index
            if int(item.get("page_number", 0) or 0) > 0
        }
        candidates: List[Dict[str, Any]] = []
        max_per_page = int(
            policy.get("budgets", {}).get("figure_crop_max_per_page", 2) or 2
        )

        for page_entry in page_blocks:
            page_no = int(page_entry.get("page_number", 0) or 0)
            if page_no <= 0:
                continue
            block_payload = page_entry.get("blocks")
            if not isinstance(block_payload, Mapping):
                continue
            blocks = block_payload.get("blocks")
            if not isinstance(blocks, list):
                continue

            page_width = float(block_payload.get("width", 0.0) or 0.0)
            page_height = float(block_payload.get("height", 0.0) or 0.0)
            page_area = page_width * page_height if page_width > 0 and page_height > 0 else 1.0

            text_blocks = []
            image_candidates = []
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                block_type = block.get("type")
                bbox = _normalize_bbox(block.get("bbox"))
                if not bbox:
                    continue
                if block_type == 0:
                    text = _block_text(block)
                    if text:
                        text_blocks.append({"bbox": bbox, "text": text})
                    continue
                if block_type != 1:
                    continue

                x0, y0, x1, y1 = bbox
                width = x1 - x0
                height = y1 - y0
                area_ratio = (width * height) / page_area if page_area else 0.0
                if width < 70 or height < 70 or area_ratio < 0.015:
                    continue
                if (y0 < page_height * 0.08 or y1 > page_height * 0.92) and height < page_height * 0.12:
                    continue

                caption_excerpt, nearby_text_excerpt = self._extract_nearby_text(
                    image_bbox=bbox,
                    text_blocks=text_blocks,
                    page_text=page_text_by_no.get(page_no, ""),
                )
                cue_hits = _count_keyword_hits(f"{caption_excerpt}\n{nearby_text_excerpt}\n{page_text_by_no.get(page_no, '')}")
                score = round(min(area_ratio * 30, 5.0) + cue_hits * 1.5, 2)
                if score <= 0:
                    score = round(area_ratio * 20, 2)
                selection_reason_parts = [f"large_image_block:{round(area_ratio, 3)}"]
                if cue_hits > 0:
                    selection_reason_parts.append(f"caption_or_context_cues:{cue_hits}")
                caption_is_explicit = bool(_FIGURE_CAPTION_RE.search(caption_excerpt))

                dedupe_seed = caption_excerpt or nearby_text_excerpt or f"{page_no}:{bbox}"
                dedupe_group_id = self._dedupe_group_id(page_no, dedupe_seed, bbox)
                image_candidates.append(
                    {
                        "page_no": page_no,
                        "bbox": bbox,
                        "score": score,
                        "selection_reason": ", ".join(selection_reason_parts),
                        "caption_excerpt": _truncate(caption_excerpt),
                        "nearby_text_excerpt": _truncate(nearby_text_excerpt),
                        "dedupe_group_id": dedupe_group_id,
                        "artifact_type": "figure_crop",
                        "selection_required": bool(caption_is_explicit or cue_hits >= 2),
                    }
                )

            image_candidates.sort(
                key=lambda item: (
                    -float(item["score"]),
                    int(item["page_no"]),
                    float(item["bbox"][1]) if item.get("bbox") else 0.0,
                )
            )
            required_images = [
                item for item in image_candidates if bool(item.get("selection_required"))
            ]
            optional_images = [
                item for item in image_candidates if not bool(item.get("selection_required"))
            ]
            candidates.extend(required_images + optional_images[: max(0, max_per_page - len(required_images))])

        candidates.sort(
            key=lambda item: (
                -float(item["score"]),
                int(item["page_no"]),
                float(item["bbox"][1]) if item.get("bbox") else 0.0,
            )
        )

        deduped: List[Dict[str, Any]] = []
        seen_groups = set()
        total_limit = _budget_value(
            policy.get("budgets", {}) if isinstance(policy, Mapping) else {},
            "figure_crop_soft_max",
            "figure_crop_max",
            6,
        )
        for item in candidates:
            group_id = str(item.get("dedupe_group_id") or "")
            if group_id in seen_groups:
                continue
            seen_groups.add(group_id)
            deduped.append(item)
        required = [item for item in deduped if bool(item.get("selection_required"))]
        optional = [item for item in deduped if not bool(item.get("selection_required"))]
        return required + optional[: max(0, total_limit - len(required))]

    def _select_layout_candidates(
        self,
        page_blocks: Iterable[Mapping[str, Any]],
        page_index: Iterable[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Find deterministic table/formula regions from text layout metadata."""

        page_text_by_no = {
            int(item.get("page_number", 0) or 0): str(item.get("text") or "")
            for item in page_index
            if int(item.get("page_number", 0) or 0) > 0
        }
        candidates: List[Dict[str, Any]] = []
        budgets = policy.get("budgets", {}) if isinstance(policy, Mapping) else {}
        per_page = {
            "table_crop": max(1, int(budgets.get("table_crop_max_per_page", 2) or 2)),
            "formula_crop": max(1, int(budgets.get("formula_crop_max_per_page", 2) or 2)),
        }
        for page_entry in page_blocks:
            page_no = int(page_entry.get("page_number", 0) or 0)
            block_payload = page_entry.get("blocks")
            if page_no <= 0 or not isinstance(block_payload, Mapping):
                continue
            blocks = block_payload.get("blocks")
            if not isinstance(blocks, list):
                continue
            text_blocks = []
            for block in blocks:
                if not isinstance(block, Mapping) or block.get("type") != 0:
                    continue
                bbox = _normalize_bbox(block.get("bbox"))
                text = _block_text(block)
                if bbox and text:
                    text_blocks.append({"bbox": bbox, "text": text})
            page_height = float(block_payload.get("height", 0.0) or 0.0)
            for artifact_type, pattern in (
                ("table_crop", r"\b(?:table|tab\.|表)\s*[a-z]?\d+\b"),
                ("formula_crop", r"(?:formula|equation|公式|方程|∑|∫|β\s*=)"),
            ):
                hits = [item for item in text_blocks if re.search(pattern, item["text"], flags=re.IGNORECASE)]
                for index, hit in enumerate(hits[: per_page[artifact_type]], start=1):
                    x0, y0, x1, y1 = hit["bbox"]
                    nearby = [item for item in text_blocks if abs(float(item["bbox"][1]) - y0) <= 260]
                    if nearby:
                        x0 = min(float(item["bbox"][0]) for item in nearby)
                        y0 = min(float(item["bbox"][1]) for item in nearby)
                        x1 = max(float(item["bbox"][2]) for item in nearby)
                        y1 = max(float(item["bbox"][3]) for item in nearby)
                    y0 = max(0.0, y0 - 18.0)
                    y1 = min(page_height or y1, y1 + 18.0)
                    bbox = _normalize_bbox([x0, y0, x1, y1])
                    if not bbox:
                        continue
                    seed = f"{artifact_type}:{page_no}:{hit['text']}"
                    hit_text = str(hit["text"])
                    nearby_text = " ".join(item["text"] for item in nearby[:5])
                    normalized_context = f"{hit_text} {nearby_text}".casefold()
                    if artifact_type == "table_crop":
                        semantic_cues = _count_cues(
                            normalized_context,
                            (
                                "regression", "results", "descriptive", "sample", "variable",
                                "comparison", "hypothesis", "effect", "mean", "结果", "样本", "变量",
                            ),
                        )
                        required = bool(
                            semantic_cues > 0
                            or _TABLE_CAPTION_RE.search(hit_text)
                        )
                    else:
                        semantic_cues = _count_cues(
                            normalized_context,
                            ("formula", "equation", "公式", "方程", "∑", "∫"),
                        )
                        required = semantic_cues > 0
                    candidates.append({
                        "page_no": page_no,
                        "bbox": bbox,
                        "score": round(2.0 + len(nearby) * 0.1 + semantic_cues * 1.5, 2),
                        "selection_reason": f"{artifact_type}_caption_or_layout_candidate",
                        "caption_excerpt": _truncate(hit_text),
                        "nearby_text_excerpt": _truncate(nearby_text),
                        "dedupe_group_id": self._dedupe_group_id(page_no, seed, bbox),
                        "artifact_type": artifact_type,
                        "selection_required": required,
                    })
        candidates.sort(
            key=lambda item: (
                -int(bool(item.get("selection_required"))),
                -float(item.get("score") or 0.0),
                int(item["page_no"]),
                float(item["bbox"][1]),
                str(item["artifact_type"]),
            )
        )
        limits = {
            "table_crop": _budget_value(budgets, "table_crop_soft_max", "table_crop_max", 6),
            "formula_crop": _budget_value(budgets, "formula_crop_soft_max", "formula_crop_max", 4),
        }
        result: List[Dict[str, Any]] = []
        counts = {key: 0 for key in limits}
        for item in candidates:
            artifact_type = str(item.get("artifact_type") or "")
            if artifact_type not in limits or counts[artifact_type] >= limits[artifact_type]:
                continue
            result.append(item)
            counts[artifact_type] += 1
        return result

    def _extract_nearby_text(
        self,
        *,
        image_bbox: List[float],
        text_blocks: List[Dict[str, Any]],
        page_text: str,
    ) -> Tuple[str, str]:
        x0, y0, x1, y1 = image_bbox
        nearby: List[Tuple[float, str]] = []
        captions: List[Tuple[float, str]] = []
        for text_block in text_blocks:
            bbox = text_block.get("bbox") or []
            if not bbox:
                continue
            tx0, ty0, tx1, ty1 = bbox
            vertical_distance = min(abs(ty1 - y0), abs(ty0 - y1), abs(ty0 - y0), abs(ty1 - y1))
            horizontal_overlap = min(x1, tx1) - max(x0, tx0)
            if horizontal_overlap < -40:
                continue
            text = str(text_block.get("text") or "").strip()
            if not text:
                continue
            nearby.append((vertical_distance, text))
            if ty0 >= y1 - 20 and ty0 <= y1 + 140:
                captions.append((vertical_distance, text))

        nearby.sort(key=lambda item: (item[0], item[1]))
        captions.sort(key=lambda item: (item[0], item[1]))

        caption_excerpt = ""
        for _distance, text in captions:
            if _count_keyword_hits(text) > 0:
                caption_excerpt = text
                break
        if not caption_excerpt and captions:
            caption_excerpt = captions[0][1]

        nearby_text_excerpt = " ".join(text for _distance, text in nearby[:3]).strip()
        if not nearby_text_excerpt:
            nearby_text_excerpt = _truncate(page_text)
        return caption_excerpt, nearby_text_excerpt

    @staticmethod
    def _bbox_overlap_ratio(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
        first = _normalize_bbox(left.get("bbox"))
        second = _normalize_bbox(right.get("bbox"))
        if not first or not second:
            return 0.0
        ix0, iy0 = max(first[0], second[0]), max(first[1], second[1])
        ix1, iy1 = min(first[2], second[2]), min(first[3], second[3])
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        if intersection <= 0:
            return 0.0
        left_area = (first[2] - first[0]) * (first[3] - first[1])
        right_area = (second[2] - second[0]) * (second[3] - second[1])
        return intersection / max(min(left_area, right_area), 1e-9)

    def _select_visual_candidates(
        self,
        *,
        page_candidates: Sequence[Mapping[str, Any]],
        figure_candidates: Sequence[Mapping[str, Any]],
        layout_candidates: Sequence[Mapping[str, Any]],
        policy: Mapping[str, Any],
        adaptive_page_scan: bool,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        """Apply deterministic type/total budgets without dropping required units."""

        budgets = policy.get("budgets", {}) if isinstance(policy, Mapping) else {}
        page_items = [
            {"artifact_type": "page_snapshot", **dict(item)}
            for item in page_candidates
            if isinstance(item, Mapping)
        ]
        crop_items = [
            dict(item)
            for item in [*figure_candidates, *layout_candidates]
            if isinstance(item, Mapping)
        ]

        if adaptive_page_scan:
            # The first exception pass is page-only, but retain bounded child
            # crops in the durable bundle so a validated page observation may
            # authorize a higher-detail final reinspection.  The input builder
            # sends only page snapshots during this pass.
            selected_pages = page_items
            selected_crops = [
                dict(item)
                for item in crop_items
                if isinstance(item, Mapping)
            ]
            selected_crops.sort(
                key=lambda item: (
                    -int(bool(item.get("selection_required"))),
                    -float(item.get("score") or 0.0),
                    int(item.get("page_no") or 0),
                    str(item.get("artifact_type") or ""),
                )
            )
            selected_crops = [
                *[
                    item
                    for item in selected_crops
                    if str(item.get("artifact_type") or "") == "figure_crop"
                ][: _budget_value(budgets, "figure_crop_soft_max", "figure_crop_max", 6)],
                *[
                    item
                    for item in selected_crops
                    if str(item.get("artifact_type") or "") == "table_crop"
                ][: _budget_value(budgets, "table_crop_soft_max", "table_crop_max", 6)],
                *[
                    item
                    for item in selected_crops
                    if str(item.get("artifact_type") or "") == "formula_crop"
                ][: _budget_value(budgets, "formula_crop_soft_max", "formula_crop_max", 4)],
            ]
        else:
            page_by_no: Dict[int, Dict[str, Any]] = {
                int(item.get("page_no") or 0): item
                for item in page_items
                if int(item.get("page_no") or 0) > 0
            }
            crop_items.sort(
                key=lambda item: (
                    -int(bool(item.get("selection_required"))),
                    -float(item.get("score") or 0.0),
                    int(item.get("page_no") or 0),
                    str(item.get("artifact_type") or ""),
                    list(item.get("bbox") or []),
                )
            )
            # A page snapshot and a crop for the same page are alternatives in
            # the normal path.  Keep the page only when the deterministic gate
            # says page-level layout, scan quality, or attribution is itself
            # the evidence unit.
            crop_pages = {
                int(item.get("page_no") or 0)
                for item in crop_items
                if int(item.get("page_no") or 0) > 0
            }
            selected_pages = [
                item
                for page_no, item in page_by_no.items()
                if page_no not in crop_pages or bool(item.get("page_snapshot_preferred"))
            ]
            preferred_pages = {
                int(item.get("page_no") or 0)
                for item in selected_pages
                if bool(item.get("page_snapshot_preferred"))
            }
            selected_crops = [
                item
                for item in crop_items
                if int(item.get("page_no") or 0) not in preferred_pages
            ]

            # Remove overlapping candidates of different crop types.  A table
            # and formula label produced from the same text block is one
            # layout-sensitive unit, not two reasons to spend image budget.
            deduped_crops: List[Dict[str, Any]] = []
            for item in selected_crops:
                duplicate = any(
                    int(existing.get("page_no") or 0) == int(item.get("page_no") or 0)
                    and self._bbox_overlap_ratio(existing, item) >= 0.72
                    and str(existing.get("artifact_type") or "")
                    != str(item.get("artifact_type") or "")
                    for existing in deduped_crops
                )
                if not duplicate:
                    deduped_crops.append(item)
            selected_crops = deduped_crops

        def _pick(items: Sequence[Mapping[str, Any]], artifact_type: str, cap: int) -> List[Dict[str, Any]]:
            typed = [
                dict(item)
                for item in items
                if str(item.get("artifact_type") or artifact_type) == artifact_type
            ]
            typed.sort(
                key=lambda item: (
                    -int(bool(item.get("selection_required"))),
                    -float(item.get("score") or 0.0),
                    int(item.get("page_no") or 0),
                    float((item.get("bbox") or [0.0, 0.0])[1] or 0.0),
                )
            )
            required = [item for item in typed if bool(item.get("selection_required"))]
            optional = [item for item in typed if not bool(item.get("selection_required"))]
            return required + optional[: max(0, cap - len(required))]

        if adaptive_page_scan:
            selected_pages = list(selected_pages)
        else:
            selected_pages = _pick(
                selected_pages,
                "page_snapshot",
                _budget_value(budgets, "page_snapshot_soft_max", "page_snapshot_max", 4),
            )
            selected_crops = [
                *_pick(
                    selected_crops,
                    "figure_crop",
                    _budget_value(budgets, "figure_crop_soft_max", "figure_crop_max", 6),
                ),
                *_pick(
                    selected_crops,
                    "table_crop",
                    _budget_value(budgets, "table_crop_soft_max", "table_crop_max", 6),
                ),
                *_pick(
                    selected_crops,
                    "formula_crop",
                    _budget_value(budgets, "formula_crop_soft_max", "formula_crop_max", 4),
                ),
            ]

        selected = [*selected_pages, *selected_crops]
        selected.sort(
            key=lambda item: (
                -int(bool(item.get("selection_required"))),
                -float(item.get("score") or 0.0),
                int(item.get("page_no") or 0),
                str(item.get("artifact_type") or ""),
            )
        )
        required = [item for item in selected if bool(item.get("selection_required"))]
        soft_total = _budget_value(budgets, "selected_visual_soft_total", "total_visuals_max", 10)
        hard_total = _budget_value(budgets, "selected_visual_hard_total", "total_visuals_max", 16)
        optional = [item for item in selected if not bool(item.get("selection_required"))]
        if not adaptive_page_scan and len(required) <= hard_total:
            selected = required + optional[:max(0, min(soft_total, hard_total) - len(required))]
        elif not adaptive_page_scan:
            selected = required

        selected_pages = [
            item for item in selected if str(item.get("artifact_type") or "") == "page_snapshot"
        ]
        selected_crops = [
            item for item in selected if str(item.get("artifact_type") or "") != "page_snapshot"
        ]
        report = {
            "selection_contract_version": _STAGE1_VISUAL_SELECTION_VERSION,
            "selection_mode": str(policy.get("selection_mode") or _SELECTIVE_SELECTION_MODE),
            "adaptive_page_scan": bool(adaptive_page_scan),
            "candidate_counts": {
                "page_snapshot": len(page_candidates),
                "figure_crop": len(figure_candidates),
                "table_crop": sum(
                    1 for item in layout_candidates if str(item.get("artifact_type") or "") == "table_crop"
                ),
                "formula_crop": sum(
                    1 for item in layout_candidates if str(item.get("artifact_type") or "") == "formula_crop"
                ),
                "total": len(page_candidates) + len(figure_candidates) + len(layout_candidates),
            },
            "selected_counts": {
                "page_snapshot": len(selected_pages),
                "figure_crop": sum(1 for item in selected_crops if str(item.get("artifact_type") or "") == "figure_crop"),
                "table_crop": sum(1 for item in selected_crops if str(item.get("artifact_type") or "") == "table_crop"),
                "formula_crop": sum(1 for item in selected_crops if str(item.get("artifact_type") or "") == "formula_crop"),
                "total": len(selected),
            },
            "required_candidate_count": sum(1 for item in selected if bool(item.get("selection_required"))),
            "optional_candidate_count": sum(1 for item in selected if not bool(item.get("selection_required"))),
            "soft_total": soft_total,
            "hard_total": hard_total,
            "hard_total_exceeded_by_required": bool(len(required) > hard_total),
            "selected_visual_ids": [],
            "selection_status": "complete" if selected else "not_required",
        }
        return selected_pages, selected_crops, report

    def _dedupe_group_id(self, page_no: int, seed_text: str, bbox: List[float]) -> str:
        normalized_seed = re.sub(r"\W+", " ", seed_text.casefold()).strip()
        if normalized_seed:
            digest_source = f"{page_no}:{normalized_seed}"
        else:
            digest_source = f"{page_no}:{bbox}"
        return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]

    def _render_pixmap_if_safe(
        self,
        *,
        page: Any,
        matrix: Any,
        image_path: str,
        policy: Mapping[str, Any],
        clip: Optional[Any] = None,
        image_format: str = "png",
        jpeg_quality: int = 92,
        max_rendered_pixels: Optional[int] = None,
    ) -> bool:
        safety = policy.get("rendering_safety", {}) if isinstance(policy, Mapping) else {}
        max_pixels = int(
            max_rendered_pixels
            or safety.get("max_rendered_pixels", 16_000_000)
            or 16_000_000
        )
        max_dimension = int(safety.get("max_rendered_dimension_px", 8_000) or 8_000)
        max_bytes = int(safety.get("max_visual_artifact_bytes", 20 * 1024 * 1024) or (20 * 1024 * 1024))

        rect = fitz.Rect(clip) if clip is not None else fitz.Rect(page.rect)
        page_rect = fitz.Rect(page.rect)
        rect.x0 = max(rect.x0, page_rect.x0)
        rect.y0 = max(rect.y0, page_rect.y0)
        rect.x1 = min(rect.x1, page_rect.x1)
        rect.y1 = min(rect.y1, page_rect.y1)
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            return False

        rendered_width = int(math.ceil(float(rect.width) * abs(float(matrix.a))))
        rendered_height = int(math.ceil(float(rect.height) * abs(float(matrix.d))))
        rendered_pixels = rendered_width * rendered_height
        if (
            rendered_width <= 0
            or rendered_height <= 0
            or rendered_width > max_dimension
            or rendered_height > max_dimension
            or rendered_pixels > max_pixels
        ):
            if self.logger:
                self.logger.warning(
                    "Skipping visual artifact render that exceeds safety bounds: "
                    f"{rendered_width}x{rendered_height} pixels"
                )
            return False

        pixmap = page.get_pixmap(matrix=matrix, clip=rect if clip is not None else None, alpha=False)
        normalized_format = _normalize_image_format(image_format, "png")
        if normalized_format == "jpeg":
            try:
                encoded = pixmap.tobytes(output="jpeg", jpg_quality=max(1, min(int(jpeg_quality), 100)))
                with open(image_path, "wb") as handle:
                    handle.write(encoded)
            except (AttributeError, TypeError, ValueError, OSError):
                # Older PyMuPDF releases do not expose jpg_quality on tobytes;
                # retain the configured format and let the safety check below
                # decide whether the fallback output is acceptable.
                pixmap.save(image_path)
        else:
            pixmap.save(image_path)
        try:
            if os.path.getsize(image_path) > max_bytes:
                os.remove(image_path)
                if self.logger:
                    self.logger.warning(f"Skipping oversized visual artifact image: {image_path}")
                return False
        except OSError:
            return False
        return True

    @staticmethod
    def _image_metadata(image_path: str, *, render_scale: float) -> Dict[str, Any]:
        width = 0
        height = 0
        try:
            pixmap = fitz.Pixmap(image_path)
            width = int(pixmap.width)
            height = int(pixmap.height)
        except Exception:
            pass
        try:
            image_bytes = int(os.path.getsize(image_path))
        except OSError:
            image_bytes = 0
        digest = ""
        try:
            digest = file_sha256(image_path)
        except (OSError, TypeError, ValueError):
            pass
        return {
            "width": width,
            "height": height,
            "render_scale": round(float(render_scale), 4),
            "estimated_dpi": max(1, int(round(float(render_scale) * 72.0))),
            "image_format": Path(image_path).suffix.lstrip(".").lower(),
            "image_bytes": image_bytes,
            "image_sha256": digest,
        }

    @staticmethod
    def _is_blank_page(page: Any) -> bool:
        """Skip truly blank pages while retaining scanned/image/drawn pages."""

        try:
            if str(page.get_text("text") or "").strip():
                return False
            if page.get_images(full=True) or page.get_drawings():
                return False
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.18, 0.18), alpha=False)
            samples = bytes(pixmap.samples)
            if not samples:
                return True
            channels = max(int(pixmap.n), 1)
            nonwhite = 0
            sample_count = len(samples) // channels
            for offset in range(0, len(samples), channels):
                if any(int(channel) < 245 for channel in samples[offset: offset + min(channels, 3)]):
                    nonwhite += 1
            return nonwhite <= max(2, int(sample_count * 0.002))
        except Exception:
            return False

    def _materialize_visuals(
        self,
        *,
        source_pdf: str,
        page_candidates: List[Dict[str, Any]],
        figure_candidates: List[Dict[str, Any]],
        layout_candidates: List[Dict[str, Any]],
        policy: Mapping[str, Any],
        bundle_dir: str,
        paper_key: str,
        artifact_hash: str,
    ) -> tuple[List[VisualArtifactRecord], List[Dict[str, Any]]]:
        """Render selected visuals and report failures as durable facts.

        Returns ``(published_visuals, materialization_failures)``.  A failed
        required candidate must not silently vanish from the selective
        coverage contract; callers fold the failure records into
        unresolved/omission sets so Stage 1 cannot report complete coverage
        for evidence it never received.
        """
        crop_candidates = [*figure_candidates, *layout_candidates]
        selected_pages = list(page_candidates)
        selected_crops = crop_candidates
        materialization_failures: List[Dict[str, Any]] = []

        def _record_failure(
            *,
            visual_id: str,
            artifact_type: str,
            page_no: int,
            bbox: list[float],
            candidate: Mapping[str, Any],
            reason: str,
        ) -> None:
            materialization_failures.append(
                {
                    "visual_id": visual_id,
                    "artifact_type": artifact_type,
                    "page_no": int(page_no),
                    "bbox": [round(float(value), 2) for value in (bbox or [])],
                    "selection_required": bool(candidate.get("selection_required", True)),
                    "selection_score": round(float(candidate.get("score", 0.0) or 0.0), 2),
                    "caption_excerpt": str(candidate.get("caption_excerpt") or ""),
                    "nearby_text_excerpt": str(candidate.get("nearby_text_excerpt") or ""),
                    "selection_reason": str(candidate.get("selection_reason") or "layout_crop"),
                    "dedupe_group_id": str(candidate.get("dedupe_group_id") or ""),
                    "reason": reason,
                }
            )

        doc = fitz.open(source_pdf)
        try:
            visuals: List[VisualArtifactRecord] = []

            rendering = policy.get("rendering", {}) if isinstance(policy, Mapping) else {}
            page_target = max(1, int(rendering.get("page_long_edge_px", 2200) or 2200))
            crop_target = max(1, int(rendering.get("crop_long_edge_px", 2400) or 2400))
            padding_ratio = parse_bounded_float(
                rendering.get("crop_padding_ratio"),
                field="Stage1_Visual.crop_padding_ratio",
                minimum=0.0,
                maximum=0.25,
                default=0.04,
            )
            page_format = _normalize_image_format(rendering.get("page_format"), "jpeg")
            crop_format = _normalize_image_format(rendering.get("crop_format"), "png")
            page_max_pixels = max(
                1,
                int(rendering.get("page_max_pixels") or 16_000_000),
            )
            crop_max_pixels = max(
                1,
                int(rendering.get("crop_max_pixels") or 16_000_000),
            )
            page_extension = "jpg" if page_format == "jpeg" else "png"
            crop_extension = "jpg" if crop_format == "jpeg" else "png"
            try:
                page_jpeg_quality = max(1, min(int(rendering.get("page_jpeg_quality", 92) or 92), 100))
            except (TypeError, ValueError):
                page_jpeg_quality = 92

            for page_candidate in selected_pages:
                page_no = int(page_candidate["page_no"])
                page = doc.load_page(page_no - 1)
                if self._is_blank_page(page):
                    page_candidate["is_blank"] = True
                    continue
                page_candidate["is_blank"] = False
                bbox = [0.0, 0.0, round(float(page.rect.width), 2), round(float(page.rect.height), 2)]
                page_long_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
                render_scale = min(max(page_target / page_long_edge, 0.5), 6.0)
                image_path = os.path.join(bundle_dir, f"page_snapshot_p{page_no:03d}.{page_extension}")
                if not self._render_pixmap_if_safe(
                    page=page,
                    matrix=fitz.Matrix(render_scale, render_scale),
                    image_path=image_path,
                    policy=policy,
                    image_format=page_format,
                    jpeg_quality=page_jpeg_quality,
                    max_rendered_pixels=page_max_pixels,
                ):
                    # Adaptive page failures are captured by the caller through
                    # page_status/omissions; only crop failures need a durable
                    # record here (see coverage materialization_failed_unit_ids).
                    continue
                artifact_id = f"page_snapshot:{artifact_hash}:p{page_no:03d}"
                image_metadata = self._image_metadata(image_path, render_scale=render_scale)
                visuals.append(
                    VisualArtifactRecord(
                        visual_id=f"page-{page_no:03d}",
                        artifact_id=artifact_id,
                        paper_key=paper_key,
                        source_pdf=source_pdf,
                        page_no=page_no,
                        bbox=bbox,
                        artifact_type="page_snapshot",
                        source_type="page",
                        image_path=os.path.abspath(image_path),
                        caption_excerpt=str(page_candidate.get("caption_excerpt") or ""),
                        nearby_text_excerpt=str(page_candidate.get("nearby_text_excerpt") or ""),
                        selection_reason=str(page_candidate.get("selection_reason") or "image_rich_page"),
                        selection_score=round(float(page_candidate.get("score", 0.0) or 0.0), 2),
                        dedupe_group_id=str(page_candidate.get("dedupe_group_id") or f"page:{page_no}"),
                        selection_required=bool(page_candidate.get("selection_required", True)),
                        selection_status=(
                            "required"
                            if bool(page_candidate.get("selection_required", True))
                            else "optional"
                        ),
                        **image_metadata,
                    )
                )

            for index, crop_candidate in enumerate(selected_crops, start=1):
                page_no = int(crop_candidate["page_no"])
                bbox = _normalize_bbox(crop_candidate.get("bbox"))
                if not bbox:
                    continue
                page = doc.load_page(page_no - 1)
                x0, y0, x1, y1 = bbox
                pad_x = (x1 - x0) * padding_ratio
                pad_y = (y1 - y0) * padding_ratio
                clip = fitz.Rect(
                    max(0.0, x0 - pad_x),
                    max(0.0, y0 - pad_y),
                    min(float(page.rect.width), x1 + pad_x),
                    min(float(page.rect.height), y1 + pad_y),
                )
                clip_long_edge = max(float(clip.width), float(clip.height), 1.0)
                render_scale = min(max(crop_target / clip_long_edge, 0.75), 8.0)
                artifact_type = str(crop_candidate.get("artifact_type") or "figure_crop")
                image_path = os.path.join(bundle_dir, f"{artifact_type}_p{page_no:03d}_{index:02d}.{crop_extension}")
                if not self._render_pixmap_if_safe(
                    page=page,
                    matrix=fitz.Matrix(render_scale, render_scale),
                    clip=clip,
                    image_path=image_path,
                    policy=policy,
                    image_format=crop_format,
                    jpeg_quality=page_jpeg_quality,
                    max_rendered_pixels=crop_max_pixels,
                ):
                    _record_failure(
                        visual_id=f"{artifact_type.removesuffix('_crop')}-{page_no:03d}-{index:02d}",
                        artifact_type=artifact_type,
                        page_no=page_no,
                        bbox=list(bbox),
                        candidate=crop_candidate,
                        reason="render_failed_or_safety_limit",
                    )
                    continue
                artifact_id = f"{artifact_type}:{artifact_hash}:p{page_no:03d}:c{index:02d}"
                image_metadata = self._image_metadata(image_path, render_scale=render_scale)
                visuals.append(
                    VisualArtifactRecord(
                        visual_id=f"{artifact_type.removesuffix('_crop')}-{page_no:03d}-{index:02d}",
                        artifact_id=artifact_id,
                        paper_key=paper_key,
                        source_pdf=source_pdf,
                        page_no=page_no,
                        bbox=[round(float(clip.x0), 2), round(float(clip.y0), 2), round(float(clip.x1), 2), round(float(clip.y1), 2)],
                        artifact_type=artifact_type,
                        source_type="layout_crop",
                        image_path=os.path.abspath(image_path),
                        caption_excerpt=str(crop_candidate.get("caption_excerpt") or ""),
                        nearby_text_excerpt=str(crop_candidate.get("nearby_text_excerpt") or ""),
                        selection_reason=str(crop_candidate.get("selection_reason") or "layout_crop"),
                        selection_score=round(float(crop_candidate.get("score", 0.0) or 0.0), 2),
                        dedupe_group_id=str(crop_candidate.get("dedupe_group_id") or ""),
                        selection_required=bool(crop_candidate.get("selection_required", True)),
                        selection_status=(
                            "required"
                            if bool(crop_candidate.get("selection_required", True))
                            else "optional"
                        ),
                        **image_metadata,
                    )
                )
            return visuals, materialization_failures
        finally:
            doc.close()

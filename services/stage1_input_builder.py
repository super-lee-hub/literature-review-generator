from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, cast

from models import APIConfig

from services.config_values import parse_bounded_float, parse_enum, parse_strict_bool
from services.model_capabilities import resolve_model_capability
from services.multimodal_capability import detect_multimodal_capability
from services.stage1_visual_scan import (
    DEFAULT_MAX_REQUEST_IMAGE_BYTES,
    DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    estimate_encoded_image_bytes,
    normalize_visual_byte_budgets,
    plan_visual_scan_batches,
    summarize_raw_reinspection_groups,
)
from services.visual_artifact_resolver import normalize_visual_artifact


def _truncate(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


@dataclass(frozen=True)
class Stage1BuiltInput:
    input_mode: str
    prompt_text: str
    user_message_content: Optional[List[Dict[str, Any]]]
    selected_visual_refs: List[Dict[str, Any]]
    visual_manifest_path: str
    visual_bundle_path: str
    visual_selection_policy_snapshot: Dict[str, Any]
    multimodal_capability: Dict[str, Any]
    fallback_reason: str
    pdf_file_input_supported: bool
    pdf_attachment_status: str
    original_pdf_attached: bool
    pdf_attachment_reason: str
    pdf_attachment_size_mb: float
    formal_input_path: str
    text_only_evidence_used: str
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    all_visual_refs: List[Dict[str, Any]] = None  # type: ignore[assignment]
    visual_coverage: Dict[str, Any] = None  # type: ignore[assignment]
    visual_scan_batches: List[List[Dict[str, Any]]] = None  # type: ignore[assignment]
    visual_scan_candidate_refs: List[List[Dict[str, Any]]] = None  # type: ignore[assignment]

    def to_metadata_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["selected_visual_refs"] = [dict(item) for item in self.selected_visual_refs]
        payload["all_visual_refs"] = [dict(item) for item in (self.all_visual_refs or [])]
        payload["visual_selection_policy_snapshot"] = dict(self.visual_selection_policy_snapshot)
        payload["multimodal_capability"] = dict(self.multimodal_capability)
        payload["visual_coverage"] = dict(self.visual_coverage or {})
        payload["visual_scan_batches"] = [
            [dict(item) for item in batch]
            for batch in (self.visual_scan_batches or [])
        ]
        payload["visual_scan_candidate_refs"] = [
            [dict(item) for item in batch]
            for batch in (self.visual_scan_candidate_refs or [])
        ]
        return payload


class Stage1InputBuilder:
    """Construct stage-one model inputs with text-first, budgeted visual support."""

    def __init__(self, logger: Any = None):
        self.logger = logger

    def build(
        self,
        *,
        prompt_template: str,
        paper_text: str,
        reader_api_config: Mapping[str, Any] | None,
        visual_bundle: Mapping[str, Any] | None = None,
        pdf_path: str = "",
        stage1_input_settings: Mapping[str, Any] | None = None,
        preprocess_metadata: Mapping[str, Any] | None = None,
        prompt_identity: Mapping[str, Any] | None = None,
        prompt_values: Mapping[str, Any] | None = None,
        post_scan_visual_refs: Sequence[Mapping[str, Any]] | None = None,
        visual_observations: Sequence[Mapping[str, Any]] | None = None,
        visual_coverage: Mapping[str, Any] | None = None,
    ) -> Stage1BuiltInput:
        visual_bundle_dict = dict(visual_bundle or {})
        stage1_settings = dict(stage1_input_settings or {})
        preprocess_metadata_dict = dict(preprocess_metadata or {})
        all_visual_refs = [
            normalize_visual_artifact(dict(item))
            for item in (
                visual_bundle_dict.get("all_visual_refs")
                or visual_bundle_dict.get("selected_visual_refs")
                or []
            )
            if isinstance(item, Mapping)
        ]
        selected_visual_refs = list(all_visual_refs)
        visual_manifest_path = str(visual_bundle_dict.get("visual_manifest_path") or "")
        visual_bundle_path = str(visual_bundle_dict.get("bundle_path") or "")
        selection_policy_snapshot = dict(visual_bundle_dict.get("selection_policy_snapshot") or {})
        visual_coverage = dict(
            visual_coverage
            or visual_bundle_dict.get("coverage_report")
            or visual_bundle_dict.get("visual_coverage")
            or {}
        )
        post_scan_refs = [
            normalize_visual_artifact(dict(item))
            for item in (post_scan_visual_refs or [])
            if isinstance(item, Mapping)
        ]
        prompt_identity_dict = dict(prompt_identity or {})
        prompt_values_dict = dict(prompt_values or {})
        prompt_values_dict.setdefault(
            "VISUAL_COVERAGE_JSON",
            json.dumps(
                visual_coverage,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        prompt_values_dict.setdefault(
            "VISUAL_OBSERVATIONS_JSON",
            json.dumps(
                [dict(item) for item in (visual_observations or []) if isinstance(item, Mapping)],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if "SUMMARY_SCHEMA_CONTRACT" in prompt_template and "SUMMARY_SCHEMA_CONTRACT" not in prompt_values_dict:
            from summary_schema import build_summary_schema_contract

            prompt_values_dict["SUMMARY_SCHEMA_CONTRACT"] = build_summary_schema_contract()

        send_extracted_text = parse_strict_bool(
            stage1_settings.get("send_extracted_text"),
            field="Stage1_Input.send_extracted_text",
            default=True,
        )
        send_selected_visuals = parse_strict_bool(
            stage1_settings.get("send_selected_visuals"),
            field="Stage1_Input.send_selected_visuals",
            default=True,
        )
        mode = parse_enum(
            stage1_settings.get("mode"),
            field="Stage1_Input.mode",
            allowed=("vision_first",),
            default="vision_first",
        )
        image_transport = parse_enum(
            stage1_settings.get("image_transport"),
            field="Stage1_Input.image_transport",
            allowed=("base64",),
            default="base64",
        )
        send_original_pdf = parse_enum(
            stage1_settings.get("send_original_pdf"),
            field="Stage1_Input.send_original_pdf",
            allowed=("never", "auto", "always"),
            default="never",
        )
        max_pdf_file_mb = parse_bounded_float(
            stage1_settings.get("max_pdf_file_mb"),
            field="Stage1_Input.max_pdf_file_mb",
            minimum=1.0,
            maximum=1_000_000.0,
            default=50.0,
        )
        force_pdf_file_input_for_provider = parse_strict_bool(
            stage1_settings.get("force_pdf_file_input_for_provider"),
            field="Stage1_Input.force_pdf_file_input_for_provider",
            default=False,
        )

        single_call_max_pages = max(1, int(stage1_settings.get("single_call_max_pages", 12) or 12))
        # Keep the implicit default small enough for the configured 5k-token
        # reader route to return one structured observation per page.  A
        # caller-provided Stage1_Input value still has authority.
        visual_scan_batch_size = max(1, int(stage1_settings.get("visual_scan_batch_size", 1) or 1))
        final_image_refs_max = max(0, int(stage1_settings.get("final_image_refs_max", 8) or 8))
        max_request_image_bytes, max_single_image_bytes = normalize_visual_byte_budgets(
            max_request_image_bytes=stage1_settings.get(
                "max_request_image_bytes", DEFAULT_MAX_REQUEST_IMAGE_BYTES
            ),
            max_single_image_bytes=stage1_settings.get(
                "max_single_image_bytes", DEFAULT_MAX_SINGLE_IMAGE_BYTES
            ),
        )
        visual_coverage["max_request_image_bytes"] = max_request_image_bytes
        visual_coverage["max_single_image_bytes"] = max_single_image_bytes
        page_refs = sorted(
            [item for item in all_visual_refs if str(item.get("artifact_type") or "") == "page_snapshot"],
            key=lambda item: int(item.get("page_no") or 0),
        )
        crop_refs = sorted(
            [item for item in all_visual_refs if str(item.get("artifact_type") or "") != "page_snapshot"],
            key=lambda item: (-float(item.get("selection_score") or 0.0), int(item.get("page_no") or 0)),
        )
        nonblank_page_count = int(visual_coverage.get("nonblank_pages") or len(page_refs))
        visual_coverage["required_nonblank_page_count"] = nonblank_page_count
        visual_coverage["required_page_ids"] = [
            str(item.get("visual_id") or "")
            for item in page_refs
            if str(item.get("visual_id") or "")
        ]
        page_total_bytes = sum(
            estimate_encoded_image_bytes(int(item.get("image_bytes") or 0))
            for item in page_refs
        )
        page_sizes_safe = all(
            int(item.get("image_bytes") or 0) <= max_single_image_bytes
            for item in page_refs
        )
        short_path = (
            nonblank_page_count <= single_call_max_pages
            and page_total_bytes <= max_request_image_bytes
            and page_sizes_safe
        )
        if short_path:
            selected_visual_refs = self._fit_visual_budget(
                [*page_refs, *crop_refs[:final_image_refs_max]],
                max_bytes=max_request_image_bytes,
                max_single_bytes=max_single_image_bytes,
                required=page_refs,
            )
        else:
            selected_visual_refs = self._fit_visual_budget(
                post_scan_refs or [],
                max_bytes=max_request_image_bytes,
                max_single_bytes=max_single_image_bytes,
                required=[],
            )
        visual_coverage.update(
            summarize_raw_reinspection_groups(
                selected_visual_refs,
                planned_units=visual_coverage.get("raw_reinspection_units"),
            )
        )
        planned_batches = plan_visual_scan_batches(
            page_refs,
            candidate_refs=all_visual_refs,
            batch_size=visual_scan_batch_size,
            max_request_image_bytes=max_request_image_bytes,
            max_single_image_bytes=max_single_image_bytes,
        ) if not short_path else ()
        visual_scan_batches = [list(batch.visual_refs) for batch in planned_batches]
        visual_scan_candidate_refs = [
            [dict(item) for item in batch.child_candidates]
            for batch in planned_batches
        ]
        planned_scan_batches = [
            {
                "batch_index": index,
                "visual_ids": [str(item.get("visual_id") or "") for item in batch],
                "page_nos": [int(item.get("page_no") or 0) for item in batch],
                "child_candidate_ids": [
                    str(item.get("visual_id") or "")
                    for item in visual_scan_candidate_refs[index]
                    if str(item.get("visual_id") or "")
                ],
            }
            for index, batch in enumerate(visual_scan_batches)
        ]
        # Keep the immutable plan separate from execution results.  A long
        # paper is built once before scanning and once after scanning; the
        # second build must not overwrite the durable batch outcomes.
        visual_coverage["planned_scan_batches"] = planned_scan_batches
        visual_coverage.setdefault("scan_batches", [])
        visual_coverage.setdefault("planned_visual_ids", [str(item.get("visual_id") or "") for item in page_refs])
        visual_coverage.setdefault("coverage_status", "planned" if page_refs else "complete")
        visual_coverage.setdefault(
            "scan_coverage_status",
            "planned" if page_refs and not short_path else "not_required",
        )

        if not send_selected_visuals:
            selected_visual_refs = []
            visual_scan_batches = []
            visual_scan_candidate_refs = []
            visual_coverage["scan_batches"] = []

        transport_visual_refs = [
            item
            for item in selected_visual_refs
            if str(item.get("visual_id") or "").strip()
        ]

        visual_appendix = self._build_visual_appendix(selected_visual_refs)
        rich_evidence_appendix = self._build_rich_mineru_evidence_appendix(
            preprocess_metadata=preprocess_metadata_dict,
            selected_visual_refs=selected_visual_refs,
        )
        paper_body = paper_text
        if not send_extracted_text:
            paper_body = ""
        if rich_evidence_appendix:
            paper_body = (
                f"{paper_body}\n\n"
                "[RICH MINERU EVIDENCE V1]\n"
                "Use this compact trace data only to recover layout-sensitive facts already extracted by preprocessing.\n\n"
                f"{rich_evidence_appendix}"
            ).strip()
        if visual_appendix:
            paper_body = (
                f"{paper_body}\n\n"
                "[VISUAL EVIDENCE BUNDLE]\n"
                "Treat the paper text as the primary evidence. Use the visual evidence only as a bounded supplement "
                "for figures, framework diagrams, process diagrams, tables, and other layout-sensitive content.\n\n"
                f"{visual_appendix}"
            ).strip()

        prompt_text = prompt_template
        for key, value in prompt_values_dict.items():
            prompt_text = prompt_text.replace("{{" + str(key) + "}}", str(value))
        prompt_text = prompt_text.replace("{{PAPER_FULL_TEXT}}", paper_body)
        capability = detect_multimodal_capability(reader_api_config)
        model_capability = resolve_model_capability(
            cast(APIConfig, dict(reader_api_config or {}))
        )
        provider_forced_pdf = force_pdf_file_input_for_provider and model_capability.endpoint_type == "responses"
        pdf_file_input_supported = bool(model_capability.supports_pdf_file_input or provider_forced_pdf)
        pdf_attachment_status = "not_requested"
        pdf_attachment_reason = "send_original_pdf=never"
        original_pdf_attached = False
        pdf_attachment_size_mb = 0.0
        pdf_item: Optional[Dict[str, Any]] = None
        if pdf_path:
            try:
                pdf_attachment_size_mb = round(float(os.path.getsize(pdf_path)) / (1024 * 1024), 3)
            except OSError:
                pdf_attachment_size_mb = 0.0
        if send_original_pdf in {"auto", "always"}:
            if not pdf_path:
                pdf_attachment_status = "missing_pdf"
                pdf_attachment_reason = "pdf_path_missing"
            elif not pdf_file_input_supported:
                pdf_attachment_status = "not_supported"
                pdf_attachment_reason = "provider_does_not_support_pdf_file_input"
            elif pdf_attachment_size_mb > max_pdf_file_mb:
                pdf_attachment_status = "too_large"
                pdf_attachment_reason = f"pdf_size_exceeds_{max_pdf_file_mb:g}_mb"
            else:
                pdf_attachment_status = "attached"
                pdf_attachment_reason = "provider_pdf_file_input_supported"
                original_pdf_attached = True
                pdf_item = {"type": "local_pdf_path", "path": pdf_path}
        formal_input_path = "pdf_plus_rich_evidence" if original_pdf_attached else "text_only_rich_evidence"
        text_only_evidence_used = "rich_mineru_evidence_v1" if rich_evidence_appendix else "stage1_input_text"

        user_message_content: Optional[List[Dict[str, Any]]]
        if not selected_visual_refs or not transport_visual_refs:
            user_message_content = None
            if pdf_item:
                user_message_content = [{"type": "text", "text": prompt_text}, pdf_item]
            return Stage1BuiltInput(
                input_mode="pdf_plus_text" if pdf_item else "text_only",
                prompt_text=prompt_text,
                user_message_content=user_message_content,
                selected_visual_refs=[],
                all_visual_refs=all_visual_refs,
                visual_manifest_path=visual_manifest_path,
                visual_bundle_path=visual_bundle_path,
                visual_selection_policy_snapshot=selection_policy_snapshot,
                multimodal_capability=capability.to_dict(),
                fallback_reason=("no_selected_visuals" if not selected_visual_refs else "visual_image_unavailable"),
                pdf_file_input_supported=pdf_file_input_supported,
                pdf_attachment_status=pdf_attachment_status,
                original_pdf_attached=original_pdf_attached,
                pdf_attachment_reason=pdf_attachment_reason,
                pdf_attachment_size_mb=pdf_attachment_size_mb,
                formal_input_path=formal_input_path,
                text_only_evidence_used=text_only_evidence_used,
                prompt_id=str(prompt_identity_dict.get("prompt_id") or ""),
                prompt_version=str(prompt_identity_dict.get("prompt_version") or prompt_identity_dict.get("version") or ""),
                prompt_sha256=str(prompt_identity_dict.get("prompt_sha256") or prompt_identity_dict.get("sha256") or ""),
                visual_coverage=visual_coverage,
                visual_scan_batches=visual_scan_batches,
                visual_scan_candidate_refs=visual_scan_candidate_refs,
            )

        if not capability.supports_image_input:
            user_message_content = None
            if pdf_item:
                user_message_content = [{"type": "text", "text": prompt_text}, pdf_item]
            return Stage1BuiltInput(
                input_mode="pdf_plus_text" if pdf_item else "text_only",
                prompt_text=prompt_text,
                user_message_content=user_message_content,
                selected_visual_refs=selected_visual_refs,
                all_visual_refs=all_visual_refs,
                visual_manifest_path=visual_manifest_path,
                visual_bundle_path=visual_bundle_path,
                visual_selection_policy_snapshot=selection_policy_snapshot,
                multimodal_capability=capability.to_dict(),
                fallback_reason=capability.reason,
                pdf_file_input_supported=pdf_file_input_supported,
                pdf_attachment_status=pdf_attachment_status,
                original_pdf_attached=original_pdf_attached,
                pdf_attachment_reason=pdf_attachment_reason,
                pdf_attachment_size_mb=pdf_attachment_size_mb,
                formal_input_path=formal_input_path,
                text_only_evidence_used=text_only_evidence_used,
                prompt_id=str(prompt_identity_dict.get("prompt_id") or ""),
                prompt_version=str(prompt_identity_dict.get("prompt_version") or prompt_identity_dict.get("version") or ""),
                prompt_sha256=str(prompt_identity_dict.get("prompt_sha256") or prompt_identity_dict.get("sha256") or ""),
                visual_coverage=visual_coverage,
                visual_scan_batches=visual_scan_batches,
                visual_scan_candidate_refs=visual_scan_candidate_refs,
            )

        user_message_content = [{"type": "text", "text": prompt_text}]
        if pdf_item:
            user_message_content.append(pdf_item)
        for visual in transport_visual_refs:
            image_path = str(visual.get("image_path") or "").strip()
            raw_reinspection_group_id = str(
                visual.get("raw_reinspection_group_id") or ""
            )
            label = self._visual_label(visual)
            user_message_content.append({"type": "text", "text": label})
            user_message_content.append(
                {
                    "type": "local_image_path",
                    "path": image_path,
                    "visual_id": str(visual.get("visual_id") or ""),
                    "artifact_type": str(visual.get("artifact_type") or ""),
                    "page_no": int(visual.get("page_no") or 0),
                    "bbox": list(visual.get("bbox") or []),
                    "image_bytes": int(visual.get("image_bytes") or 0),
                    "image_sha256": str(visual.get("image_sha256") or ""),
                    "raw_reinspection_group_id": raw_reinspection_group_id,
                    "raw_reinspection_resolution": str(
                        visual.get("raw_reinspection_resolution") or ""
                    ),
                    "ambiguous_candidate_ids": [
                        str(item)
                        for item in (visual.get("ambiguous_candidate_ids") or [])
                        if str(item)
                    ],
                    "raw_reinspection_selected_ids": [
                        str(item)
                        for item in (visual.get("raw_reinspection_selected_ids") or [])
                        if str(item)
                    ],
                    "raw_reinspection_fallback_reason": str(
                        visual.get("raw_reinspection_fallback_reason") or ""
                    ),
                    "raw_reinspection_atomic": bool(
                        visual.get("raw_reinspection_atomic")
                    ),
                    "raw_reinspection_fallback_ref": dict(
                        visual.get("raw_reinspection_fallback_ref") or {}
                    ),
                    "transport_omission_scope": (
                        "raw_reinspection" if raw_reinspection_group_id else "final_transport"
                    ),
                    "transport_omission_authority_blocking": (
                        False if raw_reinspection_group_id else True
                    ),
                }
            )

        return Stage1BuiltInput(
            input_mode="pdf_plus_multimodal" if pdf_item else "multimodal",
            prompt_text=prompt_text,
            user_message_content=user_message_content,
            selected_visual_refs=selected_visual_refs,
            all_visual_refs=all_visual_refs,
            visual_manifest_path=visual_manifest_path,
            visual_bundle_path=visual_bundle_path,
            visual_selection_policy_snapshot=selection_policy_snapshot,
            multimodal_capability=capability.to_dict(),
            fallback_reason="",
            pdf_file_input_supported=pdf_file_input_supported,
            pdf_attachment_status=pdf_attachment_status,
            original_pdf_attached=original_pdf_attached,
            pdf_attachment_reason=pdf_attachment_reason,
            pdf_attachment_size_mb=pdf_attachment_size_mb,
            formal_input_path=formal_input_path,
            text_only_evidence_used=text_only_evidence_used,
            prompt_id=str(prompt_identity_dict.get("prompt_id") or ""),
            prompt_version=str(prompt_identity_dict.get("prompt_version") or prompt_identity_dict.get("version") or ""),
            prompt_sha256=str(prompt_identity_dict.get("prompt_sha256") or prompt_identity_dict.get("sha256") or ""),
            visual_coverage=visual_coverage,
            visual_scan_batches=visual_scan_batches,
            visual_scan_candidate_refs=visual_scan_candidate_refs,
        )

    @staticmethod
    def _fit_visual_budget(
        refs: List[Dict[str, Any]],
        *,
        max_bytes: int,
        max_single_bytes: int,
        required: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        required_ids = {str(item.get("visual_id") or "") for item in required}
        selected: List[Dict[str, Any]] = []
        total = 0
        processed_group_keys: set[tuple[str, str]] = set()

        def _size(item: Mapping[str, Any]) -> int:
            image_path = str(item.get("image_path") or "")
            try:
                if image_path and os.path.isfile(image_path):
                    return int(os.path.getsize(image_path))
                return int(item.get("image_bytes") or 0)
            except OSError:
                return 0

        def _encoded(item: Mapping[str, Any]) -> int:
            return estimate_encoded_image_bytes(_size(item))

        for item in refs:
            group_id = str(item.get("raw_reinspection_group_id") or "").strip()
            resolution = str(item.get("raw_reinspection_resolution") or "").strip()
            group_key = (group_id, resolution) if group_id and resolution else None
            if group_key is not None:
                if group_key in processed_group_keys:
                    continue
                processed_group_keys.add(group_key)
                group_items = [
                    candidate
                    for candidate in refs
                    if (
                        str(candidate.get("raw_reinspection_group_id") or "").strip(),
                        str(candidate.get("raw_reinspection_resolution") or "").strip(),
                    ) == group_key
                ]
                if resolution == "all_children":
                    group_unsafe = any(
                        not str(candidate.get("image_path") or "").strip()
                        or not os.path.isfile(str(candidate.get("image_path") or "").strip())
                        or _size(candidate) <= 0
                        or _size(candidate) > max_single_bytes
                        for candidate in group_items
                    )
                    group_cost = sum(_encoded(candidate) for candidate in group_items)
                    if group_unsafe or total + group_cost > max_bytes:
                        fallback = next(
                            (
                                candidate.get("raw_reinspection_fallback_ref")
                                for candidate in group_items
                                if isinstance(candidate.get("raw_reinspection_fallback_ref"), Mapping)
                                and candidate.get("raw_reinspection_fallback_ref")
                            ),
                            None,
                        )
                        if isinstance(fallback, Mapping):
                            fallback_item = dict(fallback)
                            fallback_path = str(fallback_item.get("image_path") or "").strip()
                            fallback_size = _size(fallback_item)
                            fallback_cost = _encoded(fallback_item)
                            if (
                                str(fallback_item.get("visual_id") or "").strip()
                                and fallback_path
                                and os.path.isfile(fallback_path)
                                and 0 < fallback_size <= max_single_bytes
                                and total + fallback_cost <= max_bytes
                            ):
                                fallback_item.update(
                                    {
                                        "raw_reinspection_group_id": group_id,
                                        "raw_reinspection_resolution": "page_snapshot_fallback",
                                        "raw_reinspection_selected_ids": [
                                            str(fallback_item.get("visual_id") or "")
                                        ],
                                        "raw_reinspection_fallback_reason": str(
                                            group_items[0].get("raw_reinspection_fallback_reason")
                                            or "transport_preflight_group_not_admitted"
                                        ),
                                        "raw_reinspection_atomic": True,
                                        "ambiguous_candidate_ids": [
                                            str(item)
                                            for item in (
                                                group_items[0].get("ambiguous_candidate_ids") or []
                                            )
                                            if str(item)
                                        ],
                                    }
                                )
                                selected.append(fallback_item)
                                total += fallback_cost
                        continue
                    selected.extend(dict(candidate) for candidate in group_items)
                    total += group_cost
                    continue

                # A page-snapshot fallback is itself one atomic resolution;
                # never reintroduce any of the child members here.
                if len(group_items) != 1:
                    continue
                item = group_items[0]
            visual_id = str(item.get("visual_id") or "")
            size = _size(item)
            if size > max_single_bytes and visual_id not in required_ids:
                continue
            encoded_size = estimate_encoded_image_bytes(size)
            if visual_id in required_ids or total + encoded_size <= max_bytes:
                selected.append(item)
                total += encoded_size
        return selected

    @staticmethod
    def _visual_label(visual: Mapping[str, Any]) -> str:
        page_no = int(visual.get("page_no") or 0)
        bbox = visual.get("bbox") or []
        artifact_type = str(visual.get("artifact_type") or "visual")
        visual_id = str(visual.get("visual_id") or "")
        caption = _truncate(visual.get("caption_excerpt") or "", 360)
        nearby = _truncate(visual.get("nearby_text_excerpt") or "", 500)
        return (
            f"[VISUAL OBJECT] visual_id={visual_id}; page_no={page_no}; bbox={bbox}; "
            f"artifact_type={artifact_type}\n"
            f"caption_excerpt={caption or '<none>'}\n"
            f"nearby_text_excerpt={nearby or '<none>'}\n"
            "The following image is evidence for this label only."
        )

    def _build_visual_appendix(self, selected_visual_refs: List[Dict[str, Any]]) -> str:
        if not selected_visual_refs:
            return ""

        lines: List[str] = [
            "The following visual objects were preselected under a conservative budget and are traceable to page numbers and bounding boxes.",
        ]
        for index, visual in enumerate(selected_visual_refs, start=1):
            label = str(visual.get("artifact_type") or "visual")
            page_no = int(visual.get("page_no") or 0)
            selection_reason = _truncate(visual.get("selection_reason") or "", limit=120)
            caption_excerpt = _truncate(visual.get("caption_excerpt") or "", limit=160)
            nearby_text_excerpt = _truncate(visual.get("nearby_text_excerpt") or "", limit=160)
            bbox = visual.get("bbox") or []
            lines.append(
                f"{index}. [{label}] page {page_no}, bbox={bbox}, reason={selection_reason or 'traceable visual selection'}"
            )
            if caption_excerpt:
                lines.append(f"   caption: {caption_excerpt}")
            if nearby_text_excerpt:
                lines.append(f"   nearby_text: {nearby_text_excerpt}")
        return "\n".join(lines)

    def _build_rich_mineru_evidence_appendix(
        self,
        *,
        preprocess_metadata: Mapping[str, Any],
        selected_visual_refs: List[Dict[str, Any]],
    ) -> str:
        lines: List[str] = []
        stage1_input_path = str(preprocess_metadata.get("stage1_input_path") or "").strip()
        markdown_path = str(preprocess_metadata.get("markdown_path") or "").strip()
        selected_source = str(preprocess_metadata.get("selected_text_source") or "").strip()
        if stage1_input_path:
            lines.append(f"primary_text_source: {selected_source or 'stage1_input'} ({stage1_input_path})")
        normalized_excerpt = self._read_optional_distinct_excerpt(markdown_path, stage1_input_path)
        if normalized_excerpt:
            lines.append("normalized_md_distinct_excerpt:")
            lines.append(normalized_excerpt)
        compact_metadata = self._compact_stage1_page_chunk_metadata(preprocess_metadata)
        if compact_metadata:
            lines.append("compact_page_chunk_metadata:")
            lines.extend(compact_metadata)
        if selected_visual_refs:
            lines.append("selected_visual_metadata:")
            for index, visual in enumerate(selected_visual_refs, start=1):
                lines.append(
                    f"{index}. page={int(visual.get('page_no') or 0)} bbox={visual.get('bbox') or []} "
                    f"reason={_truncate(visual.get('selection_reason') or '', 120)}"
                )
                caption = _truncate(visual.get("caption_excerpt") or "", 160)
                nearby = _truncate(visual.get("nearby_text_excerpt") or "", 160)
                if caption:
                    lines.append(f"   caption_excerpt={caption}")
                if nearby:
                    lines.append(f"   nearby_text_excerpt={nearby}")
        return "\n".join(lines)

    def _read_optional_distinct_excerpt(self, markdown_path: str, stage1_input_path: str) -> str:
        if not markdown_path or markdown_path == stage1_input_path:
            return ""
        try:
            with open(markdown_path, "r", encoding="utf-8") as handle:
                markdown = handle.read()
        except Exception:
            return ""
        if not markdown.strip():
            return ""
        stage1_sample = ""
        if stage1_input_path:
            try:
                with open(stage1_input_path, "r", encoding="utf-8") as handle:
                    stage1_sample = handle.read(4000)
            except Exception:
                stage1_sample = ""
        normalized_md = " ".join(markdown.split())
        normalized_stage1 = " ".join(stage1_sample.split())
        if normalized_stage1 and normalized_md[:2000] == normalized_stage1[:2000]:
            return ""
        return _truncate(markdown, 1200)

    def _compact_stage1_page_chunk_metadata(self, preprocess_metadata: Mapping[str, Any]) -> List[str]:
        page_index_path = str(preprocess_metadata.get("page_index_path") or "").strip()
        if not page_index_path:
            return []
        try:
            import json

            with open(page_index_path, "r", encoding="utf-8") as handle:
                pages = json.load(handle)
        except Exception:
            return []
        if not isinstance(pages, list):
            return []
        lines: List[str] = []
        for page in pages[:12]:
            if not isinstance(page, Mapping):
                continue
            page_no = int(page.get("page_number") or page.get("page_no") or 0)
            text_length = int(page.get("text_length") or len(str(page.get("text") or "")))
            block_count = int(page.get("block_count") or len(page.get("blocks") or []))
            image_count = int(page.get("image_count") or len(page.get("images") or []))
            lines.append(
                f"- page={page_no} text_length={text_length} blocks={block_count} images={image_count}"
            )
        if len(pages) > 12:
            lines.append(f"- additional_pages={len(pages) - 12}")
        return lines

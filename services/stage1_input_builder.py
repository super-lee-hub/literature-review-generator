from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any, Dict, List, Mapping, Optional

from services.model_capabilities import resolve_model_capability
from services.multimodal_capability import detect_multimodal_capability
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

    def to_metadata_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["selected_visual_refs"] = [dict(item) for item in self.selected_visual_refs]
        payload["visual_selection_policy_snapshot"] = dict(self.visual_selection_policy_snapshot)
        payload["multimodal_capability"] = dict(self.multimodal_capability)
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
    ) -> Stage1BuiltInput:
        visual_bundle_dict = dict(visual_bundle or {})
        stage1_settings = dict(stage1_input_settings or {})
        preprocess_metadata_dict = dict(preprocess_metadata or {})
        selected_visual_refs = [
            normalize_visual_artifact(dict(item))
            for item in (visual_bundle_dict.get("selected_visual_refs") or [])
            if isinstance(item, Mapping)
        ]
        visual_manifest_path = str(visual_bundle_dict.get("visual_manifest_path") or "")
        visual_bundle_path = str(visual_bundle_dict.get("bundle_path") or "")
        selection_policy_snapshot = dict(visual_bundle_dict.get("selection_policy_snapshot") or {})

        send_extracted_text = _as_bool(stage1_settings.get("send_extracted_text"), default=True)
        send_selected_visuals = _as_bool(stage1_settings.get("send_selected_visuals"), default=True)
        send_original_pdf = str(stage1_settings.get("send_original_pdf") or "never").strip().lower()
        if send_original_pdf not in {"never", "auto", "always"}:
            send_original_pdf = "never"
        max_pdf_file_mb = _as_float(stage1_settings.get("max_pdf_file_mb"), default=50.0)
        force_pdf_file_input_for_provider = _as_bool(
            stage1_settings.get("force_pdf_file_input_for_provider"),
            default=False,
        )

        if not send_selected_visuals:
            selected_visual_refs = []

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

        prompt_text = prompt_template.replace("{{PAPER_FULL_TEXT}}", paper_body)
        capability = detect_multimodal_capability(reader_api_config)
        model_capability = resolve_model_capability(dict(reader_api_config or {}))
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

        if not selected_visual_refs:
            user_message_content = None
            if pdf_item:
                user_message_content = [{"type": "text", "text": prompt_text}, pdf_item]
            return Stage1BuiltInput(
                input_mode="pdf_plus_text" if pdf_item else "text_only",
                prompt_text=prompt_text,
                user_message_content=user_message_content,
                selected_visual_refs=[],
                visual_manifest_path=visual_manifest_path,
                visual_bundle_path=visual_bundle_path,
                visual_selection_policy_snapshot=selection_policy_snapshot,
                multimodal_capability=capability.to_dict(),
                fallback_reason="no_selected_visuals",
                pdf_file_input_supported=pdf_file_input_supported,
                pdf_attachment_status=pdf_attachment_status,
                original_pdf_attached=original_pdf_attached,
                pdf_attachment_reason=pdf_attachment_reason,
                pdf_attachment_size_mb=pdf_attachment_size_mb,
                formal_input_path=formal_input_path,
                text_only_evidence_used=text_only_evidence_used,
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
            )

        user_message_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        if pdf_item:
            user_message_content.append(pdf_item)
        for visual in selected_visual_refs:
            image_path = str(visual.get("image_path") or "").strip()
            if not image_path:
                continue
            user_message_content.append(
                {
                    "type": "local_image_path",
                    "path": image_path,
                    "visual_id": str(visual.get("visual_id") or ""),
                    "artifact_type": str(visual.get("artifact_type") or ""),
                    "page_no": int(visual.get("page_no") or 0),
                }
            )

        return Stage1BuiltInput(
            input_mode="pdf_plus_multimodal" if pdf_item else "multimodal",
            prompt_text=prompt_text,
            user_message_content=user_message_content,
            selected_visual_refs=selected_visual_refs,
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


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "on", "enabled", "enable"}


def _as_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default

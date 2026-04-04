from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional

from services.multimodal_capability import MultimodalCapability, detect_multimodal_capability
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
    ) -> Stage1BuiltInput:
        visual_bundle_dict = dict(visual_bundle or {})
        selected_visual_refs = [
            normalize_visual_artifact(dict(item))
            for item in (visual_bundle_dict.get("selected_visual_refs") or [])
            if isinstance(item, Mapping)
        ]
        visual_manifest_path = str(visual_bundle_dict.get("visual_manifest_path") or "")
        visual_bundle_path = str(visual_bundle_dict.get("bundle_path") or "")
        selection_policy_snapshot = dict(visual_bundle_dict.get("selection_policy_snapshot") or {})

        visual_appendix = self._build_visual_appendix(selected_visual_refs)
        paper_body = paper_text
        if visual_appendix:
            paper_body = (
                f"{paper_text}\n\n"
                "[VISUAL EVIDENCE BUNDLE]\n"
                "Treat the paper text as the primary evidence. Use the visual evidence only as a bounded supplement "
                "for figures, framework diagrams, process diagrams, tables, and other layout-sensitive content.\n\n"
                f"{visual_appendix}"
            )

        prompt_text = prompt_template.replace("{{PAPER_FULL_TEXT}}", paper_body)
        capability = detect_multimodal_capability(reader_api_config)

        if not selected_visual_refs:
            return Stage1BuiltInput(
                input_mode="text_only",
                prompt_text=prompt_text,
                user_message_content=None,
                selected_visual_refs=[],
                visual_manifest_path=visual_manifest_path,
                visual_bundle_path=visual_bundle_path,
                visual_selection_policy_snapshot=selection_policy_snapshot,
                multimodal_capability=capability.to_dict(),
                fallback_reason="no_selected_visuals",
            )

        if not capability.supports_image_input:
            return Stage1BuiltInput(
                input_mode="text_only",
                prompt_text=prompt_text,
                user_message_content=None,
                selected_visual_refs=selected_visual_refs,
                visual_manifest_path=visual_manifest_path,
                visual_bundle_path=visual_bundle_path,
                visual_selection_policy_snapshot=selection_policy_snapshot,
                multimodal_capability=capability.to_dict(),
                fallback_reason=capability.reason,
            )

        user_message_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
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
            input_mode="multimodal",
            prompt_text=prompt_text,
            user_message_content=user_message_content,
            selected_visual_refs=selected_visual_refs,
            visual_manifest_path=visual_manifest_path,
            visual_bundle_path=visual_bundle_path,
            visual_selection_policy_snapshot=selection_policy_snapshot,
            multimodal_capability=capability.to_dict(),
            fallback_reason="",
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

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from services.artifact_registry import ArtifactRegistry


def normalize_visual_artifact(visual: Dict[str, Any]) -> Dict[str, Any]:
    """归一化视觉证据 artifact，统一字段命名
    
    解决以下不一致问题：
    - caption_excerpt vs caption
    - image_path vs path
    - page_no vs page_range
    
    Args:
        visual: 视觉证据 artifact
        
    Returns:
        归一化后的视觉证据 artifact
    """
    normalized = {}
    
    # 基本字段
    normalized["visual_id"] = visual.get("visual_id") or visual.get("id") or ""
    normalized["artifact_id"] = visual.get("artifact_id") or ""
    normalized["paper_key"] = visual.get("paper_key") or ""
    normalized["source_pdf"] = visual.get("source_pdf") or ""
    
    # 页面信息
    normalized["page_no"] = int(visual.get("page_no") or visual.get("page_number") or -1)
    page_range = visual.get("page_range")
    if page_range:
        normalized["page_range"] = page_range
    else:
        normalized["page_range"] = [normalized["page_no"]] if normalized["page_no"] >= 0 else []
    
    # 位置信息
    normalized["bbox"] = visual.get("bbox") or []
    
    # 类型信息
    normalized["artifact_type"] = visual.get("artifact_type") or ""
    normalized["source_type"] = visual.get("source_type") or ""
    
    # 路径信息
    normalized["image_path"] = visual.get("image_path") or visual.get("path") or ""
    
    # 文本信息
    normalized["caption_excerpt"] = visual.get("caption_excerpt") or visual.get("caption") or ""
    normalized["nearby_text_excerpt"] = visual.get("nearby_text_excerpt") or visual.get("nearby_text") or ""
    
    # 选择信息
    normalized["selection_reason"] = visual.get("selection_reason") or ""
    normalized["selection_score"] = float(visual.get("selection_score") or 0.0)
    normalized["post_scan_score"] = float(visual.get("post_scan_score") or 0.0)
    normalized["score_components"] = dict(visual.get("score_components") or {}) if isinstance(visual.get("score_components"), dict) else {}
    normalized["source_page_visual_id"] = visual.get("source_page_visual_id") or ""
    normalized["source_observation_visual_id"] = visual.get("source_observation_visual_id") or ""
    normalized["dedupe_group_id"] = visual.get("dedupe_group_id") or ""
    normalized["raw_reinspection_group_id"] = visual.get("raw_reinspection_group_id") or ""
    normalized["ambiguous_candidate_ids"] = [
        str(item)
        for item in (visual.get("ambiguous_candidate_ids") or [])
        if str(item)
    ]
    normalized["raw_reinspection_resolution"] = visual.get("raw_reinspection_resolution") or ""
    normalized["raw_reinspection_selected_ids"] = [
        str(item)
        for item in (visual.get("raw_reinspection_selected_ids") or [])
        if str(item)
    ]
    normalized["raw_reinspection_fallback_reason"] = (
        visual.get("raw_reinspection_fallback_reason") or ""
    )
    normalized["raw_reinspection_upgrade_reason"] = (
        visual.get("raw_reinspection_upgrade_reason") or ""
    )
    normalized["raw_reinspection_fallback_ref"] = dict(
        visual.get("raw_reinspection_fallback_ref") or {}
    ) if isinstance(visual.get("raw_reinspection_fallback_ref"), dict) else {}
    normalized["raw_reinspection_atomic"] = bool(
        visual.get("raw_reinspection_atomic")
    )
    for field_name in (
        "width", "height", "render_scale", "estimated_dpi", "image_format",
        "image_bytes", "image_sha256",
    ):
        normalized[field_name] = visual.get(field_name) or (0 if field_name not in {"image_format", "image_sha256"} else "")
    
    return normalized


class VisualArtifactResolver:
    def __init__(self, artifact_registry: ArtifactRegistry, logger: Any = None):
        self.artifact_registry = artifact_registry
        self.logger = logger

    def _load_json_file(self, path: str) -> Optional[Dict[str, Any]]:
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            if self.logger:
                self.logger.warning(f"Error loading JSON file {path}: {e}")
            return None
        return payload if isinstance(payload, dict) else None

    def _load_paper_artifact(self, paper_artifact_path: str) -> Optional[Dict[str, Any]]:
        return self._load_json_file(paper_artifact_path)

    def _paper_key_candidates(self, paper_artifact: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        paper_identity = paper_artifact.get("paper_identity")
        if isinstance(paper_identity, dict):
            for key in ("canonical_paper_key", "source_paper_id"):
                value = str(paper_identity.get(key) or "").strip()
                if value and value not in candidates:
                    candidates.append(value)
            aliases = paper_identity.get("paper_key_aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    value = str(alias or "").strip()
                    if value and value not in candidates:
                        candidates.append(value)

        stage1_inputs = paper_artifact.get("stage1_inputs")
        if isinstance(stage1_inputs, dict):
            for item in stage1_inputs.get("selected_visual_refs", []) or []:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("paper_key") or "").strip()
                if value and value not in candidates:
                    candidates.append(value)
        return candidates

    def resolve_visual_manifest_path(self, paper_artifact_path: str) -> str:
        paper_artifact = self._load_paper_artifact(paper_artifact_path)
        if not paper_artifact:
            return ""

        stage1_inputs = paper_artifact.get("stage1_inputs")
        manifest_path = ""
        if isinstance(stage1_inputs, dict):
            manifest_path = str(stage1_inputs.get("visual_artifact_manifest_path") or "").strip()
        if manifest_path and os.path.isfile(manifest_path):
            return os.path.abspath(manifest_path)

        paper_keys = set(self._paper_key_candidates(paper_artifact))
        for record in self.artifact_registry.list_records():
            if record.artifact_type != "visual_manifest" or record.status != "ready":
                continue
            manifest = self._load_json_file(record.path)
            if not manifest:
                continue
            manifest_paper_key = str(manifest.get("paper_key") or "").strip()
            if paper_keys and manifest_paper_key not in paper_keys:
                continue
            return os.path.abspath(record.path)
        return ""

    def resolve_visual_manifest(self, paper_artifact_path: str) -> Optional[Dict[str, Any]]:
        """Resolve visual manifest from paper artifact and registry.
        
        Args:
            paper_artifact_path: Path to paper artifact JSON file
            
        Returns:
            Visual manifest dict if found, None otherwise
        """
        manifest_path = self.resolve_visual_manifest_path(paper_artifact_path)
        return self._load_json_file(manifest_path)

    def resolve_selected_visual_refs(self, paper_artifact_path: str) -> List[Dict[str, Any]]:
        """Resolve selected visual refs from paper artifact and registry.
        
        Args:
            paper_artifact_path: Path to paper artifact JSON file
            
        Returns:
            List of normalized selected visual refs
        """
        paper_artifact = self._load_paper_artifact(paper_artifact_path)
        if not paper_artifact:
            return []

        stage1_inputs = paper_artifact.get("stage1_inputs")
        if isinstance(stage1_inputs, dict):
            selected_refs = [
                normalize_visual_artifact(dict(item))
                for item in (stage1_inputs.get("selected_visual_refs") or [])
                if isinstance(item, dict)
            ]
            if selected_refs:
                return selected_refs

        manifest = self.resolve_visual_manifest(paper_artifact_path)
        if not manifest:
            return []
        return [normalize_visual_artifact(dict(item)) for item in (manifest.get("visuals") or []) if isinstance(item, dict)]

    def resolve_visual_artifact_by_id(self, visual_id: str) -> Optional[Dict[str, Any]]:
        """Resolve visual artifact by its ID.
        
        Args:
            visual_id: Visual artifact ID
            
        Returns:
            Normalized visual artifact dict if found, None otherwise
        """
        for record in self.artifact_registry.list_records():
            if record.artifact_type != "visual_manifest" or record.status != "ready":
                continue
            manifest = self._load_json_file(record.path)
            if not manifest:
                continue
            for visual in manifest.get("visuals", []):
                if not isinstance(visual, dict):
                    continue
                if visual.get("id") == visual_id or visual.get("visual_id") == visual_id:
                    return normalize_visual_artifact(visual)
        return None

    def get_visual_artifacts_for_paper(self, paper_key: str) -> List[Dict[str, Any]]:
        """Get all visual artifacts for a specific paper.
        
        Args:
            paper_key: Paper key
            
        Returns:
            List of normalized visual artifacts for the paper
        """
        visual_artifacts: List[Dict[str, Any]] = []
        for record in self.artifact_registry.list_records():
            if record.artifact_type != "visual_manifest" or record.status != "ready":
                continue
            manifest = self._load_json_file(record.path)
            if not manifest:
                continue
            if str(manifest.get("paper_key") or "") != paper_key:
                continue
            visual_artifacts.extend(
                normalize_visual_artifact(dict(item))
                for item in (manifest.get("visuals") or [])
                if isinstance(item, dict)
            )
        return visual_artifacts

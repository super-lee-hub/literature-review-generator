from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from services.artifact_registry import ArtifactRegistry


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
            List of selected visual refs
        """
        paper_artifact = self._load_paper_artifact(paper_artifact_path)
        if not paper_artifact:
            return []

        stage1_inputs = paper_artifact.get("stage1_inputs")
        if isinstance(stage1_inputs, dict):
            selected_refs = [
                dict(item)
                for item in (stage1_inputs.get("selected_visual_refs") or [])
                if isinstance(item, dict)
            ]
            if selected_refs:
                return selected_refs

        manifest = self.resolve_visual_manifest(paper_artifact_path)
        if not manifest:
            return []
        return [dict(item) for item in (manifest.get("visuals") or []) if isinstance(item, dict)]

    def resolve_visual_artifact_by_id(self, visual_id: str) -> Optional[Dict[str, Any]]:
        """Resolve visual artifact by its ID.
        
        Args:
            visual_id: Visual artifact ID
            
        Returns:
            Visual artifact dict if found, None otherwise
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
                    return visual
        return None

    def get_visual_artifacts_for_paper(self, paper_key: str) -> List[Dict[str, Any]]:
        """Get all visual artifacts for a specific paper.
        
        Args:
            paper_key: Paper key
            
        Returns:
            List of visual artifacts for the paper
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
                dict(item)
                for item in (manifest.get("visuals") or [])
                if isinstance(item, dict)
            )
        return visual_artifacts

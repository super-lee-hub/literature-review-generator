from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple, Any

from services.artifact_registry import ArtifactRegistry, ArtifactRecord


class VisualArtifactResolver:
    def __init__(self, artifact_registry: ArtifactRegistry, logger: Any = None):
        self.artifact_registry = artifact_registry
        self.logger = logger

    def resolve_visual_manifest(self, paper_artifact_path: str) -> Optional[Dict[str, Any]]:
        """Resolve visual manifest from paper artifact and registry.
        
        Args:
            paper_artifact_path: Path to paper artifact JSON file
            
        Returns:
            Visual manifest dict if found, None otherwise
        """
        try:
            if not os.path.exists(paper_artifact_path):
                return None
            
            with open(paper_artifact_path, 'r', encoding='utf-8') as f:
                paper_artifact = json.load(f)
            
            # Try to get manifest path from paper artifact
            manifest_path = paper_artifact.get('stage1_inputs', {}).get('visual_artifact_manifest_path')
            if manifest_path and os.path.exists(manifest_path):
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            # Fall back to registry lookup
            registry_records = self.artifact_registry.list_records()
            for record in registry_records:
                if record.artifact_type == 'visual_manifest' and record.status == 'ready':
                    if os.path.exists(record.path):
                        with open(record.path, 'r', encoding='utf-8') as f:
                            return json.load(f)
            
        except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
            if self.logger:
                self.logger.warning(f"Error resolving visual manifest: {e}")
        
        return None

    def resolve_selected_visual_refs(self, paper_artifact_path: str) -> List[Dict[str, Any]]:
        """Resolve selected visual refs from paper artifact and registry.
        
        Args:
            paper_artifact_path: Path to paper artifact JSON file
            
        Returns:
            List of selected visual refs
        """
        try:
            if not os.path.exists(paper_artifact_path):
                return []
            
            with open(paper_artifact_path, 'r', encoding='utf-8') as f:
                paper_artifact = json.load(f)
            
            # Try to get selected visual refs from paper artifact
            selected_refs = paper_artifact.get('stage1_inputs', {}).get('selected_visual_refs', [])
            if selected_refs:
                return selected_refs
            
            # Fall back to visual manifest
            manifest = self.resolve_visual_manifest(paper_artifact_path)
            if manifest:
                return manifest.get('visuals', [])
            
        except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
            if self.logger:
                self.logger.warning(f"Error resolving selected visual refs: {e}")
        
        return []

    def resolve_visual_artifact_by_id(self, visual_id: str) -> Optional[Dict[str, Any]]:
        """Resolve visual artifact by its ID.
        
        Args:
            visual_id: Visual artifact ID
            
        Returns:
            Visual artifact dict if found, None otherwise
        """
        try:
            # Look through all visual manifests
            registry_records = self.artifact_registry.list_records()
            for record in registry_records:
                if record.artifact_type == 'visual_manifest' and record.status == 'ready':
                    if os.path.exists(record.path):
                        with open(record.path, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)
                        
                        # Look for visual artifact with matching ID
                        for visual in manifest.get('visuals', []):
                            if visual.get('id') == visual_id or visual.get('visual_id') == visual_id:
                                return visual
            
        except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
            if self.logger:
                self.logger.warning(f"Error resolving visual artifact by ID: {e}")
        
        return None

    def get_visual_artifacts_for_paper(self, paper_key: str) -> List[Dict[str, Any]]:
        """Get all visual artifacts for a specific paper.
        
        Args:
            paper_key: Paper key
            
        Returns:
            List of visual artifacts for the paper
        """
        try:
            visual_artifacts = []
            
            # Look through all visual manifests
            registry_records = self.artifact_registry.list_records()
            for record in registry_records:
                if record.artifact_type == 'visual_manifest' and record.status == 'ready':
                    if os.path.exists(record.path):
                        with open(record.path, 'r', encoding='utf-8') as f:
                            manifest = json.load(f)
                        
                        # Check if this manifest is for the specified paper
                        if manifest.get('paper_key') == paper_key:
                            visual_artifacts.extend(manifest.get('visuals', []))
            
            return visual_artifacts
            
        except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
            if self.logger:
                self.logger.warning(f"Error getting visual artifacts for paper: {e}")
        
        return []

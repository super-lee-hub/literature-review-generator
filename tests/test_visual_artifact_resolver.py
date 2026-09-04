import json
import os
from pathlib import Path

import pytest

from services.artifact_registry import ArtifactRegistry
from services.visual_artifact_resolver import VisualArtifactResolver


def test_resolve_visual_manifest_from_paper_artifact(tmp_path):
    """Test resolving visual manifest from paper artifact."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create visual manifest
    manifest_path = tmp_path / "visual_manifest.json"
    manifest = {
        "artifact_type": "visual_manifest",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_key": "test_paper",
        "paper_title": "Test Paper",
        "source_pdf": "test.pdf",
        "bundle_dir": str(tmp_path),
        "selection_policy": {},
        "budget_decisions": {},
        "visuals": []
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    
    # Register manifest
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test"
    )
    
    # Create paper artifact
    paper_artifact_path = tmp_path / "paper_artifact.json"
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_identity": {
            "source_paper_id": "test_paper",
            "canonical_paper_key": "test_paper",
            "paper_key_aliases": ["test_paper"]
        },
        "source": {},
        "paper_info": {},
        "analysis": {},
        "stage1_inputs": {
            "visual_artifact_manifest_path": str(manifest_path),
            "selected_visual_refs": []
        }
    }
    paper_artifact_path.write_text(json.dumps(paper_artifact), encoding="utf-8")
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    resolved_manifest = resolver.resolve_visual_manifest(str(paper_artifact_path))
    
    assert resolved_manifest is not None
    assert resolved_manifest["paper_key"] == "test_paper"


def _current_authority_manifest(tmp_path, *, paper_key="test_paper", job_id="job-123",
                                 mode="selective", contract="stage1_visual_selection/v1",
                                 created_from_job_id=None):
    """Build a manifest dict carrying the current selection-contract authority proof."""
    return {
        "artifact_type": "visual_manifest",
        "artifact_version": "v1",
        "created_from_job_id": created_from_job_id or job_id,
        "created_at": "2024-01-01T00:00:00Z",
        "paper_key": paper_key,
        "paper_title": "Test Paper",
        "source_pdf": "test.pdf",
        "bundle_dir": str(tmp_path),
        "selection_policy": {
            "policy_name": "stage1_visual_selection_v1",
            "selection_mode": mode,
            "selection_contract_version": contract,
        },
        "budget_decisions": {},
        "selection_authority": {
            "current": True,
            "role": "authority",
            "job_id": created_from_job_id or job_id,
            "selection_mode": mode,
            "selection_contract_version": contract,
        },
        "visuals": []
    }


def _paper_artifact_with_identity(tmp_path, *, job_id="job-123", stage1_inputs=None):
    """Build a minimal paper artifact for resolver tests."""
    paper_artifact_path = tmp_path / "paper_artifact.json"
    payload = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": job_id,
        "created_at": "2024-01-01T00:00:00Z",
        "paper_identity": {
            "source_paper_id": "test_paper",
            "canonical_paper_key": "test_paper",
            "paper_key_aliases": ["test_paper"],
        },
        "source": {},
        "paper_info": {},
        "analysis": {},
        "stage1_inputs": dict(stage1_inputs or {}),
    }
    paper_artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    return paper_artifact_path


def test_resolve_visual_manifest_from_registry(tmp_path):
    """Test resolving a current-authority visual manifest from the registry."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create a current-authority visual manifest
    manifest_path = tmp_path / "visual_manifest.json"
    manifest = _current_authority_manifest(tmp_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    
    # Register manifest
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test"
    )
    
    # Create paper artifact without visual manifest path
    paper_artifact_path = tmp_path / "paper_artifact.json"
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_identity": {
            "source_paper_id": "test_paper",
            "canonical_paper_key": "test_paper",
            "paper_key_aliases": ["test_paper"]
        },
        "source": {},
        "paper_info": {},
        "analysis": {},
        "stage1_inputs": {}
    }
    paper_artifact_path.write_text(json.dumps(paper_artifact), encoding="utf-8")
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    resolved_manifest = resolver.resolve_visual_manifest(str(paper_artifact_path))
    
    assert resolved_manifest is not None
    assert resolved_manifest["paper_key"] == "test_paper"


def test_resolve_visual_manifest_from_registry_ignores_other_papers(tmp_path):
    """Test resolver skips unrelated manifests and picks the matching current manifest."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    wrong_manifest_path = tmp_path / "wrong_visual_manifest.json"
    wrong_manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "visual_manifest",
                "artifact_version": "v1",
                "created_from_job_id": "job-123",
                "created_at": "2024-01-01T00:00:00Z",
                "paper_key": "other_paper",
                "paper_title": "Other Paper",
                "source_pdf": "other.pdf",
                "bundle_dir": str(tmp_path / "other"),
                "selection_policy": {
                    "selection_mode": "selective",
                    "selection_contract_version": "stage1_visual_selection/v1",
                },
                "budget_decisions": {},
                "selection_authority": {
                    "current": True,
                    "role": "authority",
                    "job_id": "job-123",
                },
                "visuals": [],
            }
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(wrong_manifest_path),
        producer="test",
        artifact_id="visual_manifest:wrong",
    )

    right_manifest_path = tmp_path / "right_visual_manifest.json"
    right_manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "visual_manifest",
                "artifact_version": "v1",
                "created_from_job_id": "job-123",
                "created_at": "2024-01-01T00:00:00Z",
                "paper_key": "test_paper",
                "paper_title": "Test Paper",
                "source_pdf": "test.pdf",
                "bundle_dir": str(tmp_path / "right"),
                "selection_policy": {
                    "selection_mode": "selective",
                    "selection_contract_version": "stage1_visual_selection/v1",
                },
                "budget_decisions": {},
                "selection_authority": {
                    "current": True,
                    "role": "authority",
                    "job_id": "job-123",
                },
                "visuals": [],
            }
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(right_manifest_path),
        producer="test",
        artifact_id="visual_manifest:right",
    )

    paper_artifact_path = _paper_artifact_with_identity(tmp_path)

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == str(right_manifest_path)
    resolved_manifest = resolver.resolve_visual_manifest(str(paper_artifact_path))

    assert resolved_manifest is not None
    assert resolved_manifest["paper_key"] == "test_paper"


def test_resolve_selected_visual_refs_from_paper_artifact(tmp_path):
    """Test resolving selected visual refs from paper artifact."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create paper artifact with selected visual refs
    paper_artifact_path = tmp_path / "paper_artifact.json"
    selected_refs = [
        {
            "visual_id": "fig1",
            "artifact_id": "figure_crop:test:1",
            "paper_key": "test_paper",
            "source_pdf": "test.pdf",
            "page_no": 1,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "figure_crop",
            "source_type": "figure",
            "image_path": str(tmp_path / "fig1.png"),
            "caption_excerpt": "Figure 1",
            "nearby_text_excerpt": "Test",
            "selection_reason": "keyword_match",
            "selection_score": 0.9,
            "dedupe_group_id": "fig1"
        }
    ]
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_identity": {
            "source_paper_id": "test_paper",
            "canonical_paper_key": "test_paper",
            "paper_key_aliases": ["test_paper"]
        },
        "source": {},
        "paper_info": {},
        "analysis": {},
        "stage1_inputs": {
            "selected_visual_refs": selected_refs
        }
    }
    paper_artifact_path.write_text(json.dumps(paper_artifact), encoding="utf-8")
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    resolved_refs = resolver.resolve_selected_visual_refs(str(paper_artifact_path))
    
    assert len(resolved_refs) == 1
    assert resolved_refs[0]["visual_id"] == "fig1"


def test_resolve_selected_visual_refs_from_manifest(tmp_path):
    """Test resolving selected visual refs from manifest when paper artifact doesn't have them."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create visual manifest
    manifest_path = tmp_path / "visual_manifest.json"
    visuals = [
        {
            "visual_id": "fig1",
            "artifact_id": "figure_crop:test:1",
            "paper_key": "test_paper",
            "source_pdf": "test.pdf",
            "page_no": 1,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "figure_crop",
            "source_type": "figure",
            "image_path": str(tmp_path / "fig1.png"),
            "caption_excerpt": "Figure 1",
            "nearby_text_excerpt": "Test",
            "selection_reason": "keyword_match",
            "selection_score": 0.9,
            "dedupe_group_id": "fig1"
        }
    ]
    manifest = {
        "artifact_type": "visual_manifest",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_key": "test_paper",
        "paper_title": "Test Paper",
        "source_pdf": "test.pdf",
        "bundle_dir": str(tmp_path),
        "selection_policy": {},
        "budget_decisions": {},
        "visuals": visuals
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    
    # Register manifest
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test"
    )
    
    # Create paper artifact without selected visual refs
    paper_artifact_path = tmp_path / "paper_artifact.json"
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_identity": {
            "source_paper_id": "test_paper",
            "canonical_paper_key": "test_paper",
            "paper_key_aliases": ["test_paper"]
        },
        "source": {},
        "paper_info": {},
        "analysis": {},
        "stage1_inputs": {
            "visual_artifact_manifest_path": str(manifest_path)
        }
    }
    paper_artifact_path.write_text(json.dumps(paper_artifact), encoding="utf-8")
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    resolved_refs = resolver.resolve_selected_visual_refs(str(paper_artifact_path))
    
    assert len(resolved_refs) == 1
    assert resolved_refs[0]["visual_id"] == "fig1"


def test_resolve_visual_artifact_by_id(tmp_path):
    """Test resolving visual artifact by ID."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create visual manifest
    manifest_path = tmp_path / "visual_manifest.json"
    visuals = [
        {
            "visual_id": "fig1",
            "artifact_id": "figure_crop:test:1",
            "paper_key": "test_paper",
            "source_pdf": "test.pdf",
            "page_no": 1,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "figure_crop",
            "source_type": "figure",
            "image_path": str(tmp_path / "fig1.png"),
            "caption_excerpt": "Figure 1",
            "nearby_text_excerpt": "Test",
            "selection_reason": "keyword_match",
            "selection_score": 0.9,
            "dedupe_group_id": "fig1"
        }
    ]
    manifest = {
        "artifact_type": "visual_manifest",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_key": "test_paper",
        "paper_title": "Test Paper",
        "source_pdf": "test.pdf",
        "bundle_dir": str(tmp_path),
        "selection_policy": {},
        "budget_decisions": {},
        "visuals": visuals
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    
    # Register manifest
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test"
    )
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    resolved_artifact = resolver.resolve_visual_artifact_by_id("fig1")
    
    assert resolved_artifact is not None
    assert resolved_artifact["visual_id"] == "fig1"


def test_get_visual_artifacts_for_paper(tmp_path):
    """Test getting visual artifacts for a specific paper."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Create visual manifest
    manifest_path = tmp_path / "visual_manifest.json"
    visuals = [
        {
            "visual_id": "fig1",
            "artifact_id": "figure_crop:test:1",
            "paper_key": "test_paper",
            "source_pdf": "test.pdf",
            "page_no": 1,
            "bbox": [0, 0, 100, 100],
            "artifact_type": "figure_crop",
            "source_type": "figure",
            "image_path": str(tmp_path / "fig1.png"),
            "caption_excerpt": "Figure 1",
            "nearby_text_excerpt": "Test",
            "selection_reason": "keyword_match",
            "selection_score": 0.9,
            "dedupe_group_id": "fig1"
        }
    ]
    manifest = {
        "artifact_type": "visual_manifest",
        "artifact_version": "v1",
        "created_from_job_id": "job-123",
        "created_at": "2024-01-01T00:00:00Z",
        "paper_key": "test_paper",
        "paper_title": "Test Paper",
        "source_pdf": "test.pdf",
        "bundle_dir": str(tmp_path),
        "selection_policy": {},
        "budget_decisions": {},
        "visuals": visuals
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    
    # Register manifest
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test"
    )
    
    # Test resolver
    resolver = VisualArtifactResolver(registry)
    artifacts = resolver.get_visual_artifacts_for_paper("test_paper")
    
    assert len(artifacts) == 1
    assert artifacts[0]["paper_key"] == "test_paper"


def test_missing_artifact_cases(tmp_path):
    """Test handling of missing artifacts."""
    # Create registry
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")
    
    # Test with non-existent paper artifact
    resolver = VisualArtifactResolver(registry)
    non_existent_path = str(tmp_path / "non_existent.json")
    
    manifest = resolver.resolve_visual_manifest(non_existent_path)
    assert manifest is None
    
    refs = resolver.resolve_selected_visual_refs(non_existent_path)
    assert len(refs) == 0
    
    # Test with non-existent visual ID
    artifact = resolver.resolve_visual_artifact_by_id("non_existent")
    assert artifact is None
    
    # Test with non-existent paper key
    artifacts = resolver.get_visual_artifacts_for_paper("non_existent")
    assert len(artifacts) == 0


def test_resolve_visual_manifest_path_ignores_directory_value(tmp_path):
    """Test resolver ignores stale directory-valued manifest paths and falls back to registry."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    manifest_path = tmp_path / "visual_manifest.json"
    manifest_path.write_text(
        json.dumps(
            _current_authority_manifest(tmp_path),
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(manifest_path),
        producer="test",
    )

    stale_dir = tmp_path / "bundle"
    stale_dir.mkdir()
    paper_artifact_path = tmp_path / "paper_artifact.json"
    paper_artifact_path.write_text(
        json.dumps(
            {
                "artifact_type": "paper_artifact",
                "artifact_version": "v1",
                "created_from_job_id": "job-123",
                "created_at": "2024-01-01T00:00:00Z",
                "paper_identity": {
                    "source_paper_id": "test_paper",
                    "canonical_paper_key": "test_paper",
                    "paper_key_aliases": ["test_paper"],
                },
                "source": {},
                "paper_info": {},
                "analysis": {},
                "stage1_inputs": {
                    "visual_artifact_manifest_path": str(stale_dir),
                },
            }
        ),
        encoding="utf-8",
    )

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == str(manifest_path)


def test_registry_fallback_rejects_legacy_manifest_without_authority_proof(tmp_path):
    """A legacy all-page-like manifest must never become the selective authority."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    legacy_manifest_path = tmp_path / "legacy_visual_manifest.json"
    legacy_manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "visual_manifest",
                "artifact_version": "v1",
                "created_from_job_id": "job-123",
                "created_at": "2024-01-01T00:00:00Z",
                "paper_key": "test_paper",
                "paper_title": "Test Paper",
                "source_pdf": "test.pdf",
                "bundle_dir": str(tmp_path),
                "selection_policy": {"policy_name": "stage1_visual_bundle_budgeted_v1"},
                "budget_decisions": {},
                "visuals": [
                    {
                        "visual_id": "page-001",
                        "artifact_type": "page_snapshot",
                        "page_no": 1,
                        "image_path": str(tmp_path / "page-001.png"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(legacy_manifest_path),
        producer="test",
    )

    paper_artifact_path = _paper_artifact_with_identity(tmp_path)

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == ""
    assert resolver.resolve_visual_manifest(str(paper_artifact_path)) is None
    assert resolver.resolve_selected_visual_refs(str(paper_artifact_path)) == []


def test_registry_fallback_rejects_manifest_from_another_job(tmp_path):
    """Registry fallback stays within the requesting job identity."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    foreign_manifest_path = tmp_path / "foreign_visual_manifest.json"
    foreign_manifest_path.write_text(
        json.dumps(_current_authority_manifest(tmp_path, created_from_job_id="job-999")),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(foreign_manifest_path),
        producer="test",
    )

    paper_artifact_path = _paper_artifact_with_identity(tmp_path, job_id="job-123")

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == ""
    assert resolver.resolve_visual_manifest(str(paper_artifact_path)) is None


def test_registry_fallback_rejects_selection_contract_mismatch(tmp_path):
    """Fallback must honor the selection contract requested by the paper artifact."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    adaptive_manifest_path = tmp_path / "adaptive_visual_manifest.json"
    adaptive_manifest_path.write_text(
        json.dumps(_current_authority_manifest(tmp_path, mode="adaptive_page_scan")),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(adaptive_manifest_path),
        producer="test",
    )

    # The paper artifact requests the selective contract.
    paper_artifact_path = _paper_artifact_with_identity(
        tmp_path,
        stage1_inputs={
            "visual_coverage": {
                "selection_mode": "selective",
                "selection_contract_version": "stage1_visual_selection/v1",
            }
        },
    )

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == ""
    assert resolver.resolve_visual_manifest(str(paper_artifact_path)) is None


def test_registry_fallback_accepts_current_authority_manifest_matching_contract(tmp_path):
    """A current-authority manifest matching the requested contract is resolvable."""
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(str(registry_path), "job-123")

    current_manifest_path = tmp_path / "current_visual_manifest.json"
    current_manifest_path.write_text(
        json.dumps(_current_authority_manifest(tmp_path, mode="selective")),
        encoding="utf-8",
    )
    registry.register_file(
        artifact_role="visual_manifest",
        artifact_type="visual_manifest",
        artifact_version="v1",
        path=str(current_manifest_path),
        producer="test",
    )

    paper_artifact_path = _paper_artifact_with_identity(
        tmp_path,
        stage1_inputs={
            "visual_coverage": {
                "selection_mode": "selective",
                "selection_contract_version": "stage1_visual_selection/v1",
            }
        },
    )

    resolver = VisualArtifactResolver(registry)
    assert resolver.resolve_visual_manifest_path(str(paper_artifact_path)) == str(current_manifest_path)

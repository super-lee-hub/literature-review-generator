from __future__ import annotations

import json
from pathlib import Path

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json
from validation.closure import ValidationClosureService
from validation.run_result import ValidationInputArtifactsV1, ValidationRunResultV1


def _write_json(path: Path, payload: dict) -> Path:
    atomic_write_json(str(path), payload)
    return path


def _bundle(tmp_path: Path, *, render_policy: bool = True):
    workspace = JobWorkspace.create(str(tmp_path), "demo", "job-closure")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    draft_path = _write_json(
        Path(workspace.artifact_path("review_drafts/review.json")),
        {
            "artifact_type": "review_draft",
            "artifact_version": "v3",
            "content": {
                "sections": [
                    {
                        "section_id": "s1",
                        "blocks": [
                            {
                                "block_id": "b1",
                                "block_kind": "paragraph",
                                "text": "A claim.",
                                "citation_refs": [],
                            }
                        ],
                    }
                ]
            },
        },
    )
    draft = registry.register_file(
        artifact_id="review_draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=draft_path,
        producer="tests",
    )
    manifest_payload = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "occurrences": [],
        "bibliography": [],
    }
    if render_policy:
        manifest_payload["render_policy"] = {
            "citation_style": "APA7",
            "citation_locale": "en-US",
            "citation_render_mode": "structured_refs",
            "style_engine_version": "test",
            "bibliography_sort_policy": "manifest_order",
            "narrative_parenthetical_policy": "preserve_source_refs",
        }
    manifest_path = _write_json(
        Path(workspace.artifact_path("citation_manifests/manifest.json")),
        manifest_payload,
    )
    manifest = registry.register_file(
        artifact_id="citation_manifest:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2.from_record(draft).to_dict()
        ],
    )
    validation = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        execution_status="succeeded",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=draft.artifact_id,
            review_draft_hash=draft.content_hash,
            citation_manifest_id=manifest.artifact_id,
            citation_manifest_hash=manifest.content_hash,
        ),
        expected_claim_count=0,
        review_has_citations=False,
        evidence_complete=True,
    )
    validation_path = _write_json(
        Path(workspace.artifact_path("validation/validation.json")),
        validation.to_dict(),
    )
    registry.register_file(
        artifact_id=validation.validation_run_id,
        artifact_role="validation_run_result",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests",
        depends_on=[
            ArtifactDependencyRefV2.from_record(draft).to_dict(),
            ArtifactDependencyRefV2.from_record(manifest).to_dict(),
        ],
    )
    return workspace, registry


def test_validation_closure_is_clean_only_with_registered_hash_bound_inputs(tmp_path: Path) -> None:
    workspace, registry = _bundle(tmp_path)
    result = ValidationClosureService(workspace, registry).inspect()
    assert result.status == "clean"
    assert result.semantic["contract_satisfied"] is True
    assert result.citation_counts["mapped_occurrences"] == 0


def test_validation_closure_blocks_missing_render_policy(tmp_path: Path) -> None:
    workspace, registry = _bundle(tmp_path, render_policy=False)
    result = ValidationClosureService(workspace, registry).inspect()
    assert result.status == "blocked"
    assert "citation_render_policy_snapshot_missing" in result.blocking_issues


def test_validation_closure_detects_draft_tampering_without_promoting_clean(tmp_path: Path) -> None:
    workspace, registry = _bundle(tmp_path)
    draft = registry.get("review_draft")
    assert draft is not None
    Path(draft.path).write_text(json.dumps({"tampered": True}), encoding="utf-8")
    result = ValidationClosureService(workspace, registry).inspect()
    assert result.status == "blocked"
    assert any("review_draft_hash_untrusted" in item for item in result.blocking_issues)

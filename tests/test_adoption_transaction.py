from __future__ import annotations

import json
from pathlib import Path

from outline.adoption_transaction import OutlineAdoptionTransaction
from outline.stage_health import OutlineStageHealthV1, make_test_double_entry
from outline.v2_models import CoverageAudit, FinalOutline, compute_content_hash
from runtime.control_plane import ReviewControlPlane
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json


def _workspace_with_adoption_inputs(tmp_path: Path):
    workspace = JobWorkspace.create(str(tmp_path), "adoption", "job-adoption")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    final = FinalOutline(created_from_job_id=workspace.job_id, outline_id="final-1")
    final_path = Path(workspace.artifact_path("final_outline.json"))
    atomic_write_json(str(final_path), final.to_dict())
    final_record = registry.register_file(
        artifact_id="final_outline",
        artifact_role="final_outline",
        artifact_type="final_outline",
        artifact_version="v2",
        path=final_path,
        producer="test",
    )

    final_hash = compute_content_hash(final.to_dict())
    coverage = CoverageAudit(
        passed=True,
        source_final_outline_id="final_outline",
        source_final_outline_hash=final_hash,
    )
    coverage_path = Path(workspace.artifact_path("coverage_audit.json"))
    atomic_write_json(str(coverage_path), coverage.to_dict())
    coverage_record = registry.register_file(
        artifact_id="outline_coverage_audit",
        artifact_role="outline_coverage_audit",
        artifact_type="outline_coverage_audit",
        artifact_version="v1",
        path=coverage_path,
        producer="test",
    )

    health = OutlineStageHealthV1(
        job_id=workspace.job_id,
        execution_mode="test_dev",
        stages=(make_test_double_entry("outline", "test", {}, {}),),
        source_final_outline_hash=final_hash,
        source_coverage_audit_hash=compute_content_hash(coverage.to_dict()),
    )
    health_path = Path(workspace.artifact_path("stage_health.json"))
    atomic_write_json(str(health_path), health.to_dict())
    health_record = registry.register_file(
        artifact_id="outline_stage_health",
        artifact_role="outline_stage_health",
        artifact_type="outline_stage_health",
        artifact_version="v1",
        path=health_path,
        producer="test",
    )
    assert final_record.status == coverage_record.status == health_record.status == "ready"
    return workspace, registry


def test_adoption_transaction_is_explicit_and_idempotent(tmp_path: Path) -> None:
    workspace, registry = _workspace_with_adoption_inputs(tmp_path)
    service = OutlineAdoptionTransaction(workspace, registry)

    first = service.adopt(source_artifact_id="final_outline", adopted_by="tester")
    assert first.status == "succeeded"
    assert first.mutation_performed is True
    adopted = registry.get("adopted_final_outline")
    assert adopted is not None and adopted.status == "ready"
    audit = registry.get(first.audit_artifact_id)
    assert audit is not None and audit.status == "ready"
    assert json.loads(Path(adopted.path).read_text(encoding="utf-8"))["adopted_by"] == "tester"

    second = service.adopt(source_artifact_id="final_outline", adopted_by="tester")
    assert second.status == "already_adopted"
    assert second.mutation_performed is False


def test_reviewctl_adopt_uses_registry_backed_transaction(tmp_path: Path) -> None:
    workspace, _registry = _workspace_with_adoption_inputs(tmp_path)
    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])
    result = control.adopt(workspace=workspace.root_dir, artifact_id="final_outline", adopted_by="reviewer")
    assert result["status"] == "succeeded"
    assert result["adopted_artifact_id"] == "adopted_final_outline"
    assert result["forbidden_actions"]

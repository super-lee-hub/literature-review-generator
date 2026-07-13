from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, RegistryCorruption
from services.audit_record import AuditRecordV1
from services.dependency_lifecycle import (
    ArtifactDependencyBlocked,
    find_external_dependents,
    guard_artifact_delete,
    materialize_external_dependency,
)
from services.job_outcome import JobOutcomeV1
from services.job_workspace import JobWorkspace, atomic_write_json


def _registered_parent_and_child(tmp_path: Path):
    output = tmp_path / "output"
    parent_workspace = JobWorkspace(str(output), "parent", "parent-job")
    child_workspace = JobWorkspace(str(output), "child", "child-job")
    parent_registry = ArtifactRegistry(parent_workspace.paths.registry_path, parent_workspace.job_id)
    child_registry = ArtifactRegistry(child_workspace.paths.registry_path, child_workspace.job_id)
    parent_path = Path(parent_workspace.artifact_path("parent_summaries.json"))
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text("[]", encoding="utf-8")
    parent = parent_registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="test",
        artifact_id="parent-summary",
    )
    external = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id=parent.job_id,
        artifact_id=parent.artifact_id,
        artifact_type=parent.artifact_type,
        path=parent.path,
        content_hash=parent.content_hash,
    )
    child_path = Path(child_workspace.artifact_path("child_summaries.json"))
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_text("[]", encoding="utf-8")
    child = child_registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=child_path,
        producer="test",
        artifact_id="child-summary",
        depends_on=[external],
    )
    return output, parent_workspace, child_workspace, parent_registry, child_registry, parent, child, external


def test_live_external_dependency_blocks_parent_delete(tmp_path: Path) -> None:
    output, parent_workspace, _child_workspace, _pr, _cr, parent, _child, external = _registered_parent_and_child(tmp_path)

    with pytest.raises(ArtifactDependencyBlocked, match="child-job:child-summary"):
        guard_artifact_delete(
            output_root=output,
            target=external,
            parent_registry_path=parent_workspace.paths.registry_path,
        )

    assert Path(parent.path).is_file()


def test_force_break_writes_child_audit_and_invalidates_artifact_and_outcome(tmp_path: Path) -> None:
    output, parent_workspace, child_workspace, _pr, child_registry, _parent, _child, external = _registered_parent_and_child(tmp_path)
    outcome = JobOutcomeV1.create(
        job_id=child_workspace.job_id,
        attempt_number=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot={"require_clean_validation": True},
        required_stages=("analyze",),
        completed_stages=("analyze",),
    )
    outcome_path = Path(child_workspace.artifact_path("job_outcome_v1.json"))
    atomic_write_json(str(outcome_path), outcome.to_dict())
    child_registry.register_file(
        artifact_role="job_outcome",
        artifact_type="job_outcome",
        artifact_version="v1",
        path=outcome_path,
        producer="test",
        artifact_id="job_outcome",
    )

    audit_paths = guard_artifact_delete(
        output_root=output,
        target=external,
        parent_registry_path=parent_workspace.paths.registry_path,
        force=True,
        actor="maintainer@example.test",
        reason="parent artifact must be retired",
    )

    assert len(audit_paths) == 1
    audit = AuditRecordV1.from_dict(json.loads(Path(audit_paths[0]).read_text(encoding="utf-8")))
    assert audit.audit_type == "dependency_force_delete"
    assert audit.job_id == "child-job"
    child_registry.reload()
    child = child_registry.get("child-summary")
    assert child is not None
    assert child.status == "invalid"
    assert child.metadata["requires_attention"] is True
    updated_outcome = JobOutcomeV1.from_dict(json.loads(outcome_path.read_text(encoding="utf-8")))
    assert updated_outcome.job_disposition == "needs_review"
    assert updated_outcome.canonical_ready is False
    assert updated_outcome.requires_attention is True
    assert updated_outcome.outcome_revision == 2
    assert find_external_dependents(output, external) == ()


def test_materialized_local_replacement_releases_parent_dependency(tmp_path: Path) -> None:
    output, _pw, child_workspace, _pr, child_registry, _parent, _child, external = _registered_parent_and_child(tmp_path)

    updated = materialize_external_dependency(
        registry=child_registry,
        dependent_artifact_id="child-summary",
        external=external,
        local_copy_path=Path(child_workspace.artifact_path("materialized_parent.json")),
    )

    assert all(item.dependency_kind == "local_job" for item in updated.depends_on)
    assert find_external_dependents(output, external) == ()
    assert Path(child_workspace.artifact_path("materialized_parent.json")).is_file()


def test_corrupt_candidate_registry_fails_dependency_scan_closed(tmp_path: Path) -> None:
    output, _pw, _cw, _pr, _cr, _parent, _child, external = _registered_parent_and_child(tmp_path)
    corrupt = output / "corrupt__job" / "artifact_registry.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json", encoding="utf-8")

    with pytest.raises(RegistryCorruption, match="cannot inspect dependent registry"):
        find_external_dependents(output, external)


def test_invalid_child_no_longer_blocks_parent_delete(tmp_path: Path) -> None:
    output, parent_workspace, _cw, _pr, child_registry, _parent, _child, external = _registered_parent_and_child(tmp_path)
    child_registry.update_record("child-summary", status="invalid")

    assert guard_artifact_delete(
        output_root=output,
        target=external,
        parent_registry_path=parent_workspace.paths.registry_path,
    ) == ()

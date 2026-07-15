from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    RegistryCorruption,
    UnverifiedDependency,
)
from services.audit_record import AuditRecordV1
from services.dependency_lifecycle import (
    ArtifactDependencyBlocked,
    DependencyLifecycleError,
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
        external_registry_resolver=(
            lambda job_id: parent_registry if job_id == parent_workspace.job_id else None
        ),
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


def test_materialization_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    (
        _output,
        _parent_workspace,
        child_workspace,
        _parent_registry,
        child_registry,
        _parent,
        child,
        external,
    ) = _registered_parent_and_child(tmp_path)
    destination = Path(child_workspace.artifact_path("materialized_parent.json"))
    destination.write_text("keep-existing-content", encoding="utf-8")

    with pytest.raises(DependencyLifecycleError, match="destination already exists"):
        materialize_external_dependency(
            registry=child_registry,
            dependent_artifact_id=child.artifact_id,
            external=external,
            local_copy_path=destination,
        )

    assert destination.read_text(encoding="utf-8") == "keep-existing-content"
    assert child_registry.get(child.artifact_id) == child
    assert not any(
        record.artifact_role == "materialized_dependency"
        for record in child_registry.list_records()
    )


def test_materialize_one_of_multiple_external_dependencies_uses_resolver(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    parent_registries: dict[str, ArtifactRegistry] = {}
    external_dependencies = []
    for index in (1, 2):
        workspace = JobWorkspace.create(
            str(output),
            f"parent-{index}",
            job_id=f"parent-job-{index}",
        )
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        path = Path(workspace.artifact_path(f"parent-{index}.json"))
        path.write_text(json.dumps({"parent": index}), encoding="utf-8")
        record = registry.register_file(
            artifact_id=f"parent-{index}",
            artifact_role="summary",
            artifact_type="summary_file",
            artifact_version="v1",
            path=path,
            producer="tests",
        )
        parent_registries[workspace.job_id] = registry
        external_dependencies.append(
            ArtifactDependencyRefV2(
                dependency_kind="external_job",
                job_id=record.job_id,
                artifact_id=record.artifact_id,
                artifact_type=record.artifact_type,
                path=record.path,
                content_hash=record.content_hash,
            )
        )

    child_workspace = JobWorkspace.create(str(output), "child", job_id="child-job")
    child_registry = ArtifactRegistry(
        child_workspace.paths.registry_path,
        child_workspace.job_id,
    )
    child_path = Path(child_workspace.artifact_path("child.json"))
    child_path.write_text("{}", encoding="utf-8")
    child_registry.register_file(
        artifact_id="child",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        depends_on=external_dependencies,
        external_registry_resolver=lambda job_id: parent_registries.get(job_id),
    )
    local_path = Path(child_workspace.artifact_path("materialized-parent-1.json"))

    with pytest.raises(UnverifiedDependency, match="without a resolver"):
        materialize_external_dependency(
            registry=child_registry,
            dependent_artifact_id="child",
            external=external_dependencies[0],
            local_copy_path=local_path,
        )

    assert not local_path.exists()
    assert not any(
        record.artifact_role == "materialized_dependency"
        for record in child_registry.list_records()
    )

    updated = materialize_external_dependency(
        registry=child_registry,
        dependent_artifact_id="child",
        external=external_dependencies[0],
        local_copy_path=local_path,
        external_registry_resolver=lambda job_id: parent_registries.get(job_id),
    )

    assert len(updated.depends_on) == 2
    assert updated.depends_on[0].dependency_kind == "local_job"
    assert updated.depends_on[1] == external_dependencies[1]
    local_record = child_registry.get(updated.depends_on[0].artifact_id)
    assert local_record is not None
    assert local_record.status == "ready"


def test_materialization_edge_update_failure_invalidates_local_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _output,
        _parent_workspace,
        child_workspace,
        _parent_registry,
        child_registry,
        _parent,
        child,
        external,
    ) = _registered_parent_and_child(tmp_path)
    local_path = Path(child_workspace.artifact_path("failed-materialization.json"))
    original_update = child_registry.update_record

    def fail_dependent_update(artifact_id: str, **kwargs):
        if artifact_id == child.artifact_id:
            raise RuntimeError("injected edge update failure")
        return original_update(artifact_id, **kwargs)

    monkeypatch.setattr(child_registry, "update_record", fail_dependent_update)

    with pytest.raises(RuntimeError, match="edge update failure"):
        materialize_external_dependency(
            registry=child_registry,
            dependent_artifact_id=child.artifact_id,
            external=external,
            local_copy_path=local_path,
        )

    assert not local_path.exists()
    local_record = child_registry.get(
        f"materialized:{external.job_id}:{external.artifact_id}"
    )
    assert local_record is not None
    assert local_record.status == "invalid"
    unchanged_child = child_registry.get(child.artifact_id)
    assert unchanged_child is not None
    assert unchanged_child.depends_on == child.depends_on


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

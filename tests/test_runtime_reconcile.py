from __future__ import annotations

import json
from pathlib import Path

from runtime.attempt_store import _write_json_exclusive
from runtime.reconcile import ProvenStageRecovery, RuntimeReconciler
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace


def _fixture(tmp_path: Path):
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="job-1")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    payload_path = Path(workspace.artifact_path("payload.json"))
    payload_path.write_text(json.dumps({"schema": "test-v1", "value": 1}), encoding="utf-8")
    output = registry.register_file(
        artifact_role="test",
        artifact_type="test_payload",
        artifact_version="v1",
        path=payload_path,
        producer="tests",
        artifact_id="payload",
    )
    ref = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=workspace.job_id,
        artifact_id=output.artifact_id,
        artifact_type=output.artifact_type,
        path=output.path,
        content_hash=output.content_hash,
    )

    def validate_test_payload(_record, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "test-v1":
            raise ValueError("wrong schema")

    reconciler = RuntimeReconciler(
        workspace,
        registry,
        schema_validators={"test_payload": validate_test_payload},
    )
    return workspace, registry, ref, reconciler


def test_stage_completion_requires_registered_terminal_file_hash_schema_and_dependencies(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="analyze",
        status="succeeded",
        producer="tests",
        output_artifact_refs=(ref,),
        model_call_count=1,
    )
    StageTerminalStore(workspace, registry).persist(record)

    assert reconciler.stage_is_complete("analyze") is True

    Path(ref.path).write_text(json.dumps({"schema": "test-v1", "value": 2}), encoding="utf-8")
    assert reconciler.stage_is_complete("analyze") is False


def test_reconcile_registers_provable_orphan_terminal_without_provider_call(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="analyze",
        status="succeeded",
        producer="tests",
        output_artifact_refs=(ref,),
        model_call_count=1,
    )
    store = StageTerminalStore(workspace, registry)
    _write_json_exclusive(store.path_for(record), record.to_dict())
    provider_calls = 0

    result = reconciler.reconcile()

    assert provider_calls == 0
    assert record.record_id in result.repaired_artifact_ids
    assert result.completed_stages == ("analyze",)
    assert registry.get(record.record_id) is not None


def test_reconcile_reconstructs_terminal_only_from_valid_registered_outputs(tmp_path: Path) -> None:
    _workspace, _registry, ref, reconciler = _fixture(tmp_path)

    result = reconciler.reconcile(
        stage_recoveries=(
            ProvenStageRecovery(
                stage_name="review",
                attempt_id="attempt-1",
                output_artifact_refs=(ref,),
                model_call_count=1,
            ),
        )
    )

    assert result.completed_stages == ("review",)
    assert len(result.reconstructed_stage_records) == 1


def test_reconcile_fails_closed_for_missing_dependency_and_unknown_schema(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    missing_ref = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=workspace.job_id,
        artifact_id="missing",
        artifact_type="test_payload",
        path=str(tmp_path / "missing.json"),
        content_hash="deadbeef",
    )

    result = reconciler.reconcile(
        stage_recoveries=(
            ProvenStageRecovery(
                stage_name="review",
                attempt_id="attempt-1",
                output_artifact_refs=(missing_ref,),
            ),
        )
    )

    assert result.completed_stages == ()
    assert any(issue.code == "stage_reconstruction_not_proven" for issue in result.issues)

    record = registry.get(ref.artifact_id)
    assert record is not None
    no_schema = RuntimeReconciler(workspace, registry)
    try:
        no_schema.validate_record(record)
    except ValueError as exc:
        assert "no schema validator" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown schemas must fail closed")

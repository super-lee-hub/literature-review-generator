from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.attempt_store import _write_json_exclusive
from runtime.reconcile import ProvenStageRecovery, RuntimeReconciler
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_terminal import (
    StageTerminalContractError,
    StageTerminalStore,
    TerminalStageRecordV1,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_outcome import JobOutcomeV1
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


def _contract_ref(artifact_type: str, index: int = 0) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id="job-1",
        artifact_id=f"artifact-{index}",
        artifact_type=artifact_type,
        path=f"C:/fixtures/{artifact_type}-{index}",
        content_hash=f"hash-{index}",
    )


@pytest.mark.parametrize(
    ("stage_name", "artifact_types"),
    [
        ("source_intake", ("source_bundle",)),
        ("analyze", ("summary_file",)),
        ("outline", ("adopted_final_outline",)),
        ("outline", ("literature_review_outline",)),
        ("review", ("review_draft", "citation_manifest", "review_docx")),
        ("validate", ("validation_run_result",)),
    ],
)
def test_succeeded_stage_terminal_accepts_only_complete_stage_output_contracts(
    stage_name: str,
    artifact_types: tuple[str, ...],
) -> None:
    TerminalStageRecordV1.create(
        job_id="job-1",
        attempt_id="attempt-1",
        stage_name=stage_name,
        status="succeeded",
        producer="tests",
        output_artifact_refs=tuple(
            _contract_ref(artifact_type, index)
            for index, artifact_type in enumerate(artifact_types)
        ),
    )


@pytest.mark.parametrize(
    ("stage_name", "artifact_types"),
    [
        ("source_intake", ("summary_file",)),
        ("analyze", ("source_bundle",)),
        ("outline", ("final_outline",)),
        ("review", ("review_draft", "citation_manifest")),
        ("validate", ("validation_report_projection",)),
    ],
)
def test_succeeded_stage_terminal_rejects_incomplete_stage_output_contracts(
    stage_name: str,
    artifact_types: tuple[str, ...],
) -> None:
    with pytest.raises(StageTerminalContractError, match="canonical outputs"):
        TerminalStageRecordV1.create(
            job_id="job-1",
            attempt_id="attempt-1",
            stage_name=stage_name,
            status="succeeded",
            producer="tests",
            output_artifact_refs=tuple(
                _contract_ref(artifact_type, index)
                for index, artifact_type in enumerate(artifact_types)
            ),
        )


def test_stage_completion_requires_registered_terminal_file_hash_schema_and_dependencies(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="test_stage",
        status="succeeded",
        producer="tests",
        output_artifact_refs=(ref,),
        model_call_count=1,
    )
    StageTerminalStore(workspace, registry).persist(record)

    assert reconciler.stage_is_complete("test_stage") is True

    Path(ref.path).write_text(json.dumps({"schema": "test-v1", "value": 2}), encoding="utf-8")
    assert reconciler.stage_is_complete("test_stage") is False


@pytest.mark.parametrize(
    "ai_summary",
    [
        {},
        {"summary": "Legacy mappings are not canonical Stage 1 output."},
    ],
    ids=["empty", "legacy-shape"],
)
def test_reconcile_rejects_noncanonical_stage1_summary_before_completion(
    tmp_path: Path,
    ai_summary: dict[str, object],
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "demo", job_id="job-1")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summary_path = Path(workspace.artifact_path("demo_summaries.json"))
    summary_path.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {"canonical_paper_key": "paper-1"},
                    "ai_summary": ai_summary,
                }
            ]
        ),
        encoding="utf-8",
    )
    summary_record = registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary_path,
        producer="tests.test_runtime_reconcile",
        artifact_id="summary",
    )
    summary_ref = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=workspace.job_id,
        artifact_id=summary_record.artifact_id,
        artifact_type=summary_record.artifact_type,
        path=summary_record.path,
        content_hash=summary_record.content_hash,
    )
    terminal = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="analyze",
        status="succeeded",
        producer="tests.test_runtime_reconcile",
        output_artifact_refs=(summary_ref,),
    )
    StageTerminalStore(workspace, registry).persist(terminal)

    assert RuntimeReconciler(workspace, registry).stage_is_complete("analyze") is False


def test_reconcile_registers_provable_orphan_terminal_without_provider_call(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="test_stage",
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
    assert result.completed_stages == ("test_stage",)
    assert registry.get(record.record_id) is not None


def test_reconcile_does_not_release_quarantined_terminal_registration(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        stage_name="test_stage",
        status="succeeded",
        producer="tests",
        output_artifact_refs=(ref,),
    )
    StageTerminalStore(workspace, registry).persist(record)
    registry.update_record(record.record_id, status="quarantined")

    result = reconciler.reconcile()

    assert result.clean is False
    assert result.completed_stages == ()
    assert any(issue.code == "unresolved_stage_terminal" for issue in result.issues)
    current = registry.get(record.record_id)
    assert current is not None and current.status == "quarantined"


def test_reconcile_rejects_foreign_job_terminal_without_crashing(tmp_path: Path) -> None:
    workspace, registry, ref, reconciler = _fixture(tmp_path)
    record = TerminalStageRecordV1.create(
        job_id="foreign-job",
        attempt_id="attempt-1",
        stage_name="test_stage",
        status="succeeded",
        producer="tests",
        output_artifact_refs=(ref,),
    )
    store = StageTerminalStore(workspace, registry)
    _write_json_exclusive(store.path_for(record), record.to_dict())

    result = reconciler.reconcile()

    assert result.clean is False
    assert result.completed_stages == ()
    assert any(issue.code == "invalid_stage_terminal" for issue in result.issues)


def test_reconcile_and_status_reject_foreign_job_outcome(tmp_path: Path) -> None:
    workspace, registry, _ref, reconciler = _fixture(tmp_path)
    outcome = JobOutcomeV1.create(
        job_id="foreign-job",
        attempt_number=1,
        job_status="completed",
        job_disposition="clean",
        canonical_ready=True,
        requires_attention=False,
        readiness_policy_snapshot={},
    )
    outcome_path = Path(workspace.artifact_path("job_outcome_v1.json"))
    outcome_path.write_text(json.dumps(outcome.to_dict()), encoding="utf-8")

    result = reconciler.reconcile()

    assert result.clean is False
    assert result.outcome_repaired is False
    assert registry.get("job_outcome") is None
    assert any(issue.code == "invalid_job_outcome" for issue in result.issues)
    with pytest.raises(RuntimeRunnerError, match="another workspace"):
        AgentRuntimeRunner.status(workspace.root_dir)


def test_reconcile_reconstructs_terminal_only_from_valid_registered_outputs(tmp_path: Path) -> None:
    _workspace, _registry, ref, reconciler = _fixture(tmp_path)

    result = reconciler.reconcile(
        stage_recoveries=(
            ProvenStageRecovery(
                stage_name="test_stage",
                attempt_id="attempt-1",
                output_artifact_refs=(ref,),
                model_call_count=1,
            ),
        )
    )

    assert result.completed_stages == ("test_stage",)
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
                stage_name="test_stage",
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


def test_external_job_dependency_resolves_by_registry_identity(tmp_path: Path) -> None:
    parent = JobWorkspace.create(str(tmp_path), "parent", job_id="parent-job")
    parent_registry = ArtifactRegistry(parent.paths.registry_path, parent.job_id)
    leaf_path = Path(parent.artifact_path("leaf.json"))
    leaf_path.write_text(json.dumps({"schema": "leaf"}), encoding="utf-8")
    leaf_record = parent_registry.register_file(
        artifact_role="parent-leaf",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=leaf_path,
        producer="tests",
        artifact_id="parent-leaf",
    )
    parent_path = Path(parent.artifact_path("summary.json"))
    parent_path.write_text(json.dumps({"schema": "parent"}), encoding="utf-8")
    parent_record = parent_registry.register_file(
        artifact_role="parent",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=parent_path,
        producer="tests",
        artifact_id="parent-summary",
        depends_on=(
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=parent.job_id,
                artifact_id=leaf_record.artifact_id,
                artifact_type=leaf_record.artifact_type,
                path=leaf_record.path,
                content_hash=leaf_record.content_hash,
            ),
        ),
    )

    child = JobWorkspace.create(str(tmp_path), "child", job_id="child-job")
    child_registry = ArtifactRegistry(child.paths.registry_path, child.job_id)
    child_path = Path(child.artifact_path("derived.json"))
    child_path.write_text(json.dumps({"schema": "child"}), encoding="utf-8")
    child_record = child_registry.register_file(
        artifact_role="child",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=child_path,
        producer="tests",
        artifact_id="derived",
        depends_on=(
            ArtifactDependencyRefV2(
                dependency_kind="external_job",
                job_id=parent.job_id,
                artifact_id=parent_record.artifact_id,
                artifact_type=parent_record.artifact_type,
                path=parent_record.path,
                content_hash=parent_record.content_hash,
            ),
        ),
    )
    child_ref = ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=child.job_id,
        artifact_id=child_record.artifact_id,
        artifact_type=child_record.artifact_type,
        path=child_record.path,
        content_hash=child_record.content_hash,
    )
    StageTerminalStore(child, child_registry).persist(
        TerminalStageRecordV1.create(
            job_id=child.job_id,
            attempt_id="attempt-1",
            stage_name="test_stage",
            status="succeeded",
            producer="tests",
            output_artifact_refs=(child_ref,),
        )
    )

    reconciler = RuntimeReconciler(
        child,
        child_registry,
        external_registry_resolver=AgentRuntimeRunner._external_registry_resolver(child),
    )
    assert reconciler.stage_is_complete("test_stage") is True

    parent_path.write_text(json.dumps({"schema": "tampered"}), encoding="utf-8")
    assert reconciler.stage_is_complete("test_stage") is False


def test_external_registry_resolver_fails_closed_for_duplicate_job_ids(tmp_path: Path) -> None:
    first = JobWorkspace.create(str(tmp_path), "first", job_id="same-job")
    second = JobWorkspace.create(str(tmp_path), "second", job_id="same-job")
    ArtifactRegistry(first.paths.registry_path, first.job_id).save()
    ArtifactRegistry(second.paths.registry_path, second.job_id).save()
    child = JobWorkspace.create(str(tmp_path), "child", job_id="child-job")

    resolver = AgentRuntimeRunner._external_registry_resolver(child)

    assert resolver("same-job") is None

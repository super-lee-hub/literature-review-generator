"""The intentional replay hands off its exact DAG without weakening CAS."""

import json
from pathlib import Path

import pytest

from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from runtime.outline_v3_dag import OutlineNodeStore
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from tests.test_outline_v3_semantic_execution import _executor


def test_parent_publishes_current_health_after_verified_replay_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_snapshots = []
    original_run = OutlineV3Executor.run

    def capture_replay(executor: OutlineV3Executor):
        result = original_run(executor)
        if executor._skip_exact_replay_verification:
            assert result.ok, result.diagnostics
            replay_snapshots.append(executor._dag)
        return result

    monkeypatch.setattr(OutlineV3Executor, "run", capture_replay)
    executor = _executor(tmp_path, stability_mode="smoke")
    result = executor.run()

    assert result.ok, result.diagnostics
    assert len(replay_snapshots) == 1
    durable = executor._node_store.load()
    assert durable is not None
    assert durable.content_hash == executor._dag.content_hash
    assert durable.snapshot_sequence > replay_snapshots[0].snapshot_sequence

    health = executor.registry.get("outline-v3:stage_health")
    stability = executor.registry.get("outline-v3:stability_audit")
    assert health is not None and stability is not None
    assert ArtifactDependencyRefV2.from_record(stability) in health.depends_on
    executor.registry.verify_ready_artifact_closure(health)
    health_payload = json.loads(Path(health.path).read_text(encoding="utf-8"))["payload"]
    stability_payload = json.loads(Path(stability.path).read_text(encoding="utf-8"))["payload"]
    assert health_payload["status"] == "healthy"
    assert health_payload["stability_audit_hash"] == stability.content_hash
    assert stability_payload["exact_replay_verification"]["status"] == "verified"
    assert stability_payload["exact_replay_verification"]["transport_call_count"] == 0


def test_verified_replay_rejects_a_later_durable_writer_without_adopting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    baseline = executor.run()
    assert baseline.ok, baseline.diagnostics
    parent_dag = executor._dag
    concurrent_snapshots = []
    original_run = OutlineV3Executor.run

    def append_after_replay(replay: OutlineV3Executor):
        result = original_run(replay)
        if replay._skip_exact_replay_verification:
            assert result.ok, result.diagnostics
            verified = replay._dag
            node = verified.get("stage_health")
            other_registry = ArtifactRegistry(replay.registry.registry_path, replay.job_id)
            other_store = OutlineNodeStore(replay.workspace, other_registry)
            concurrent = other_store.record_node(
                "stage_health", expected_current_dag=verified,
                status="succeeded", input_hash=node.input_hash,
                output_hash=node.output_hash,
                output_artifact_ids=node.output_artifact_ids,
                execution_binding=node.execution_binding,
                diagnostics=("independent writer after replay completion",),
            )
            assert concurrent.snapshot_sequence > verified.snapshot_sequence
            assert replay._dag.content_hash == verified.content_hash
            concurrent_snapshots.append(concurrent)
        return result

    monkeypatch.setattr(OutlineV3Executor, "run", append_after_replay)
    with pytest.raises(
        OutlineV3ExecutionError,
        match="durable DAG changed after the verified second-executor replay",
    ):
        executor._verify_exact_replay_with_second_executor()

    assert len(concurrent_snapshots) == 1
    assert executor._dag.content_hash == parent_dag.content_hash
    durable = executor._node_store.load()
    assert durable is not None
    assert durable.content_hash == concurrent_snapshots[0].content_hash
    assert durable.snapshot_sequence == concurrent_snapshots[0].snapshot_sequence

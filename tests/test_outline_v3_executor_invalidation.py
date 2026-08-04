"""Executor-level binding, replay, and invalidation regression tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from outline.v3_executor import OutlineV3Executor
from runtime.outline_v3_dag import OutlineNodeStore
from services.artifact_registry import ArtifactRegistry
from tests.test_outline_v3_semantic_execution import _executor, _summary


def _counted_fixture_executor(tmp_path: Path, calls: list[str]) -> OutlineV3Executor:
    holder: dict[str, OutlineV3Executor] = {}

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        return holder["executor"]._fixture_response(node_id, request)

    executor = _executor(tmp_path, provider=provider)
    holder["executor"] = executor
    return executor


def test_executor_second_run_reuses_only_exact_binding_and_receipt_closure(tmp_path: Path) -> None:
    calls: list[str] = []
    first_executor = _counted_fixture_executor(tmp_path, calls)
    first = first_executor.run()
    first_call_count = len(calls)

    second_executor = _counted_fixture_executor(tmp_path, calls)
    second = second_executor.run()

    assert first.ok is True
    assert second.ok is True
    assert len(calls) == first_call_count

    dag = OutlineNodeStore(
        second_executor.workspace,
        second_executor.registry,
    ).load()
    assert dag is not None
    succeeded = [node for node in dag.nodes if node.status == "succeeded"]
    assert succeeded
    for node in succeeded:
        assert node.execution_binding["node_id"] == node.node_id
        assert node.execution_binding["node_version"] == "v3"
        assert node.execution_binding["schema_version"] == "outline-v3"
        assert "dependency_hashes" in node.execution_binding
        assert "current_summary_hashes" in node.execution_binding
        assert "quality_gate_hash" in node.execution_binding


def test_executor_missing_receipt_invalidates_replay_before_reuse(tmp_path: Path) -> None:
    calls: list[str] = []
    first_executor = _counted_fixture_executor(tmp_path, calls)
    first = first_executor.run()
    assert first.ok is True
    first_call_count = len(calls)

    ledger_path = Path(first_executor._receipt_ledger.path)
    receipts = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert receipts
    ledger_path.write_text("\n".join(receipts[1:]) + "\n", encoding="utf-8")

    second_executor = _counted_fixture_executor(tmp_path, calls)
    second = second_executor.run()

    assert second.ok is True
    assert len(calls) > first_call_count


def test_executor_summary_change_invalidates_downstream_and_preserves_no_stale_reuse(tmp_path: Path) -> None:
    calls: list[str] = []
    first_executor = _counted_fixture_executor(tmp_path, calls)
    first = first_executor.run()
    first_call_count = len(calls)
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()
    assert first_dag is not None

    changed_summaries = [
        _summary("paper-a", "Study A", "The treatment had no measurable effect."),
        _summary("paper-b", "Study B", "The treatment improved the outcome under a different context."),
    ]
    second_executor = OutlineV3Executor(
        job_id=first_executor.job_id,
        summaries=changed_summaries,
        workspace=first_executor.workspace,
        artifact_registry=first_executor.registry,
        provider=lambda node_id, request: (
            calls.append(node_id) or second_executor._fixture_response(node_id, request)
        ),
        candidate_count=2,
    )
    second = second_executor.run()
    second_dag = OutlineNodeStore(second_executor.workspace, second_executor.registry).load()

    assert first.ok is True
    assert second.ok is True
    assert len(calls) > first_call_count
    assert second_dag is not None
    assert (
        first_dag.get("outline_evidence_views").execution_binding["current_summary_hashes"]
        != second_dag.get("outline_evidence_views").execution_binding["current_summary_hashes"]
    )
    assert second_dag.get("outline_evidence_views").status == "succeeded"
    assert second_dag.get("final_outline").status == "succeeded"


def test_executor_candidate_count_change_expands_dag_and_keeps_shared_upstream(tmp_path: Path) -> None:
    calls: list[str] = []
    first_executor = _counted_fixture_executor(tmp_path, calls)
    first = first_executor.run()
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()
    assert first_dag is not None

    workspace = first_executor.workspace
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    holder: dict[str, OutlineV3Executor] = {}

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        return holder["executor"]._fixture_response(node_id, request)

    second_executor = OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=first_executor.summaries,
        workspace=workspace,
        artifact_registry=registry,
        provider=provider,
        candidate_count=3,
    )
    holder["executor"] = second_executor
    second = second_executor.run()
    second_dag = OutlineNodeStore(workspace, registry).load()

    assert first.ok is True
    assert second.ok is True
    assert second_dag is not None
    assert second_dag.get("candidate_3").status == "succeeded"
    assert second_dag.get("candidate_3_provider_generation").status == "succeeded"
    assert (
        first_dag.get("outline_evidence_views").execution_binding
        == second_dag.get("outline_evidence_views").execution_binding
    )


def test_executor_review_intent_change_invalidates_replay_binding(tmp_path: Path) -> None:
    calls: list[str] = []
    first_executor = _counted_fixture_executor(tmp_path, calls)
    first = first_executor.run()
    first_call_count = len(calls)
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()
    assert first_dag is not None

    holder: dict[str, OutlineV3Executor] = {}

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        return holder["executor"]._fixture_response(node_id, request)

    second_executor = OutlineV3Executor(
        job_id=first_executor.job_id,
        summaries=first_executor.summaries,
        workspace=first_executor.workspace,
        artifact_registry=first_executor.registry,
        provider=provider,
        candidate_count=2,
        review_intent={"review_question": "Compare boundary conditions across studies."},
    )
    holder["executor"] = second_executor
    second = second_executor.run()
    second_dag = OutlineNodeStore(second_executor.workspace, second_executor.registry).load()

    assert first.ok is True
    assert second.ok is True
    assert len(calls) > first_call_count
    assert first_dag.get("outline_evidence_views").execution_binding == second_dag.get("outline_evidence_views").execution_binding
    assert first_dag.get("review_intent").execution_binding["review_intent_hash"] != second_dag.get("review_intent").execution_binding["review_intent_hash"]
    assert second_dag.get("final_outline").status == "succeeded"

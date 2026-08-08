"""Executor-level binding, replay, and invalidation regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

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

    published_ledger = first_executor.registry.get("outline_v3_provider_receipts")
    assert published_ledger is not None
    ledger_path = Path(published_ledger.path)
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


def test_executor_provider_failure_persists_failed_node_and_resumes_only_descendants(
    tmp_path: Path,
) -> None:
    first_executor = _executor(tmp_path, stability_mode="off")
    injected = {"done": False}

    def fault_injector(node_id: str, _payload: Mapping[str, Any]) -> None:
        if node_id == "candidate_2_provider_generation" and not injected["done"]:
            injected["done"] = True
            raise RuntimeError("injected provider failure")

    first_executor.fault_injector = fault_injector
    first = first_executor.run()
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()

    assert first.ok is False
    assert first.status == "blocked"
    assert any("injected provider failure" in item for item in first.diagnostics)
    assert first_dag is not None
    assert first_dag.get("candidate_1_provider_generation").status == "succeeded"
    assert first_dag.get("candidate_2_provider_generation").status == "failed"

    calls: list[str] = []
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
        stability_mode="off",
    )
    holder["executor"] = second_executor
    second = second_executor.run()

    assert second.ok is True
    assert second.status == "ready_for_adoption"
    assert calls == [
        "candidate_2_provider_generation",
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
        "arbitration",
    ]
    assert not second.dag.failed_node_ids


def test_executor_coverage_critic_failure_preserves_prior_nodes_and_resumes_exact_descendants(
    tmp_path: Path,
) -> None:
    holder: dict[str, OutlineV3Executor] = {}
    first_calls: list[str] = []

    def first_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        first_calls.append(node_id)
        return holder["executor"]._fixture_response(node_id, request)

    first_executor = _executor(tmp_path, provider=first_provider, stability_mode="off")
    holder["executor"] = first_executor
    injected = {"done": False}

    def fail_after_coverage_transport(node_id: str, payload: Mapping[str, Any]) -> None:
        if (
            node_id == "coverage_critique"
            and payload.get("phase") == "provider_success"
            and not injected["done"]
        ):
            injected["done"] = True
            raise RuntimeError("injected coverage critic failure after provider success")

    first_executor.fault_injector = fail_after_coverage_transport
    first = first_executor.run()
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()

    assert first.ok is False
    assert first_calls == [
        "relation_adjudication",
        "candidate_1_provider_generation",
        "candidate_2_provider_generation",
        "structure_critique",
        "coverage_critique",
    ]
    assert first_dag is not None
    assert first_dag.get("relation_adjudication").status == "succeeded"
    assert first_dag.get("candidate_1_provider_generation").status == "succeeded"
    assert first_dag.get("candidate_2_provider_generation").status == "succeeded"
    assert first_dag.get("structure_critique").status == "succeeded"
    assert first_dag.get("coverage_critique").status == "failed"

    preserved_nodes = {
        node_id: first_dag.get(node_id)
        for node_id in (
            "candidate_1_provider_generation",
            "candidate_2_provider_generation",
            "structure_critique",
        )
    }
    preserved_records = {
        node_id: first_executor.registry.get(f"outline-v3:{node_id}")
        for node_id in preserved_nodes
    }
    assert all(record is not None for record in preserved_records.values())
    preserved_hashes = {
        node_id: record.content_hash
        for node_id, record in preserved_records.items()
        if record is not None
    }
    preserved_receipt_ids = {
        node_id: tuple(node.receipt_ids)
        for node_id, node in preserved_nodes.items()
    }

    resumed_calls: list[str] = []
    second_holder: dict[str, OutlineV3Executor] = {}

    def resumed_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        resumed_calls.append(node_id)
        return second_holder["executor"]._fixture_response(node_id, request)

    second_registry = ArtifactRegistry(
        first_executor.workspace.paths.registry_path,
        first_executor.job_id,
    )
    second_executor = OutlineV3Executor(
        job_id=first_executor.job_id,
        summaries=first_executor.summaries,
        workspace=first_executor.workspace,
        artifact_registry=second_registry,
        provider=resumed_provider,
        candidate_count=2,
        stability_mode="off",
        logical_attempt_identity=first_executor.logical_attempt_identity,
        pricing_source="tests:explicit-rates-v1",
        pricing_provider=first_executor.profile.provider,
        pricing_model=first_executor.profile.model,
        pricing_version="v1",
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )
    second_holder["executor"] = second_executor
    second = second_executor.run()
    second_dag = OutlineNodeStore(second_executor.workspace, second_executor.registry).load()

    assert second.ok is True
    assert resumed_calls == [
        "coverage_critique",
        "evidence_critique",
        "arbitration",
    ]
    assert second_dag is not None
    assert not second_dag.failed_node_ids
    for node_id, expected_hash in preserved_hashes.items():
        record = second_registry.get(f"outline-v3:{node_id}")
        assert record is not None
        assert record.content_hash == expected_hash
        assert tuple(second_dag.get(node_id).receipt_ids) == preserved_receipt_ids[node_id]
    closure = second_registry.get("outline-v3:provider_receipt_closure")
    assert closure is not None
    closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))["payload"]
    assert closure_payload["complete"] is True


def test_executor_candidate_two_binding_change_reruns_only_candidate_two_and_dependents(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    holder: dict[str, OutlineV3Executor] = {}

    def first_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(node_id)
        return holder["executor"]._fixture_response(node_id, request)

    first_executor = _executor(tmp_path, provider=first_provider, stability_mode="off")
    holder["executor"] = first_executor
    first = first_executor.run()
    assert first.ok is True
    first_dag = OutlineNodeStore(first_executor.workspace, first_executor.registry).load()
    assert first_dag is not None
    first_candidate_one = first_executor.registry.get("outline-v3:candidate_1_provider_generation")
    assert first_candidate_one is not None
    first_candidate_one_hash = first_candidate_one.content_hash

    second_calls: list[str] = []
    second_holder: dict[str, OutlineV3Executor] = {}

    def changed_provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        second_calls.append(node_id)
        response = second_holder["executor"]._fixture_response(node_id, request)
        if node_id == "candidate_2_provider_generation":
            content = dict(response["content"])
            content["claims"] = ["Candidate two changed because its execution binding changed."]
            response = {**response, "content": content}
        return response

    second_registry = ArtifactRegistry(
        first_executor.workspace.paths.registry_path,
        first_executor.job_id,
    )
    second_executor = OutlineV3Executor(
        job_id=first_executor.job_id,
        summaries=first_executor.summaries,
        workspace=first_executor.workspace,
        artifact_registry=second_registry,
        provider=changed_provider,
        candidate_count=2,
        stability_mode="off",
        logical_attempt_identity=first_executor.logical_attempt_identity,
        pricing_source="tests:explicit-rates-v1",
        pricing_provider=first_executor.profile.provider,
        pricing_model=first_executor.profile.model,
        pricing_version="v1",
        input_cost_per_1k_tokens=0.0,
        output_cost_per_1k_tokens=0.001,
        reasoning_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.0,
        cache_write_cost_per_1k_tokens=0.0,
    )
    second_holder["executor"] = second_executor
    original_binding = second_executor._provider_binding

    def changed_candidate_two_binding(
        node_id: str,
        request: Mapping[str, Any],
        *,
        expect_json: bool,
        input_artifact_hashes: Sequence[str],
    ) -> dict[str, Any]:
        binding = original_binding(
            node_id,
            request,
            expect_json=expect_json,
            input_artifact_hashes=input_artifact_hashes,
        )
        if node_id == "candidate_2_provider_generation":
            binding["prompt_template_hash"] = "candidate-two-binding-v2"
        return binding

    second_executor._provider_binding = changed_candidate_two_binding  # type: ignore[method-assign]
    second = second_executor.run()

    assert second.ok is True
    assert second_calls == [
        "candidate_2_provider_generation",
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
        "arbitration",
    ]
    second_candidate_one = second_registry.get("outline-v3:candidate_1_provider_generation")
    assert second_candidate_one is not None
    assert second_candidate_one.content_hash == first_candidate_one_hash

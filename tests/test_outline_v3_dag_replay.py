"""Durability, retry-scope, and exact replay tests for Outline v3."""

from dataclasses import replace

from outline.v3_models import compute_v3_hash
from runtime.outline_v3_dag import (
    OutlineNodeStore,
    create_outline_v3_node_dag,
    plan_outline_v3_resume,
)
from runtime.outline_v3_replay import ModelCallReplayKey, ModelCallReplayStore
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


def _workspace(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "review", "job-v3")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return workspace, registry


def test_node_dag_contains_required_nodes_and_preserves_completed_candidates_on_critique_retry(tmp_path):
    workspace, registry = _workspace(tmp_path)
    store = OutlineNodeStore(workspace, registry)
    dag = store.ensure(workspace.job_id, candidate_count=3)
    node_ids = {node.node_id for node in dag.nodes}

    assert {
        "outline_evidence_views",
        "global_corpus_ledger",
        "multi_view_matrix",
        "relation_candidates",
        "global_relation_map",
        "organizing_axes",
        "candidate_1",
        "candidate_2",
        "candidate_3",
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
        "arbitration",
        "selected_candidate",
        "section_evidence_packets",
        "final_outline",
        "coverage_audit",
        "stability_audit",
        "stage_health",
        "provider_receipt_closure",
    }.issubset(node_ids)
    assert "candidate_1_provider_generation" in node_ids

    succeeded_candidates = {
        node.node_id
        for node in dag.nodes
        if node.node_id in {"candidate_1", "candidate_2", "candidate_3", "candidate_1_provider_generation", "candidate_2_provider_generation", "candidate_3_provider_generation"}
    }
    nodes = [
        replace(
            node,
            status="succeeded" if node.node_id in succeeded_candidates else ("failed" if node.node_id == "structure_critique" else node.status),
            output_hash=compute_v3_hash(node.node_id) if node.node_id in succeeded_candidates else "",
        )
        for node in dag.nodes
    ]
    failed_dag = replace(dag, nodes=nodes)
    plan = plan_outline_v3_resume(failed_dag, "structure_critique")

    assert "structure_critique" in plan.rerun_node_ids
    assert "arbitration" in plan.rerun_node_ids
    assert "candidate_1" not in plan.rerun_node_ids
    assert "candidate_1_provider_generation" in plan.preserved_node_ids

    store.save(failed_dag)
    retried, retry_plan = store.retry_node("structure_critique")
    assert retry_plan.rerun_node_ids == plan.rerun_node_ids
    assert retried.get("candidate_1").status == "succeeded"
    assert retried.get("structure_critique").status == "pending"
    assert retried.get("provider_receipt_closure").status == "pending"
    assert len(store.load().nodes) == len(dag.nodes)  # type: ignore[union-attr]
    assert len(registry.list_records()) >= 2


def test_receipt_closure_failure_reruns_only_receipt_closure_downstream(tmp_path):
    dag = create_outline_v3_node_dag("job-adoption", candidate_count=3)
    nodes = [
        replace(
            node,
            status="failed" if node.node_id == "provider_receipt_closure" else "succeeded",
            output_hash="hash",
        )
        for node in dag.nodes
    ]
    plan = plan_outline_v3_resume(replace(dag, nodes=nodes), "provider_receipt_closure")

    assert plan.rerun_node_ids == ["provider_receipt_closure", "stage_health"]
    assert "final_outline" in plan.preserved_node_ids
    assert "selected_candidate" in plan.preserved_node_ids


def test_model_replay_is_reusable_only_for_exact_binding(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "review", "job-replay")
    store = ModelCallReplayStore(workspace)
    key = ModelCallReplayKey(
        node_id="candidate_1_provider_generation",
        node_version="v3",
        schema_version="outline_v3",
        model_route="responses",
        model_name="model-a",
        provider="provider-a",
        prompt_template_hash="template-hash",
        prompt_payload_hash="payload-hash",
        input_artifact_hashes=["ledger-hash", "matrix-hash"],
        config_hash="config-hash",
    )
    record = store.append(key, output_hash="output-hash", receipt_ids=["receipt-1"])
    exact = store.lookup(key)

    assert record.replay_id == f"replay:{key.key_hash}"
    assert exact.reusable is True
    assert exact.record is not None
    assert exact.record.output_hash == "output-hash"

    stale_model = store.lookup(replace(key, model_name="model-b"))
    assert stale_model.status == "stale"
    assert "model_changed" in stale_model.stale_reasons

    stale_input = store.lookup(replace(key, input_artifact_hashes=["different-input"]))
    assert stale_input.status == "stale"
    assert "input_artifacts_changed" in stale_input.stale_reasons

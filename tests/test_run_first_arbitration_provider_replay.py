"""JIT binding, Registry persistence, and exact replay for dynamic arbitration calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from runtime.outline_v3_replay import ModelCallReplayStore
from runtime.provider_runtime import hash_text
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_run_first_arbitration_hierarchy import _plan, _response_for


def test_arbitration_leaf_and_final_use_role_route_registry_closure_and_zero_call_replay(tmp_path: Path):
    plan = _plan()
    assert len(plan["leaves"]) == 1
    leaf = plan["leaves"][0]
    leaf_node = f"arbitration:leaf:{leaf['leaf_id']}"
    final_node = f"arbitration:final:{plan['plan_hash'][:24]}"
    provider_calls: list[str] = []
    provider_inputs: list[dict[str, Any]] = []

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        provider_inputs.append(dict(request))
        if node_id == leaf_node:
            return {"status": "success", "content": _response_for(plan, leaf)}
        assert node_id == final_node
        return {
            "status": "success",
            "content": {
                "selected_candidate_id": plan["candidate_ids"][0],
                "selection_reasons": [
                    "The complete source-bound section ledger preserves each candidate's evidence and limitations."
                ],
                "accepted_recommendations": [],
                "rejected_recommendations": [],
                "unresolved_risks": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_provider_calls=2,
        candidate_count=5,
    )
    persisted_dynamic: list[tuple[str, str, dict[str, Any]]] = []
    original_persist = executor._persist_semantic_provider_output

    def observe_persist(node_id, payload, **kwargs):
        if ":leaf:" in node_id or ":final:" in node_id:
            assert kwargs.get("update_parent_node") is False
            persisted_dynamic.append((node_id, str(payload.get("leaf_id") or payload.get("selected_candidate_id") or ""), dict(kwargs)))
        return original_persist(node_id, payload, **kwargs)

    executor._persist_semantic_provider_output = observe_persist
    dependencies = {"candidate_generations": "candidate-parent-hash", "critics": "critique-parent-hash"}
    source = {
        "candidate_ids": plan["candidate_ids"],
        "candidate_hashes": plan["candidate_hashes"],
        "candidate_contents": {
            candidate_id: {"candidate_id": candidate_id, "organizing_logic": "source evidence"}
            for candidate_id in plan["candidate_ids"]
        },
        "critiques": {"structure_critique": {"passed": True, "issues": []}},
        "critique_disposition": {"eligible_candidate_ids": plan["candidate_ids"], "issues": []},
        "review_intent": {"review_question": "Compare complete source sections."},
        "selection_rule": "coverage_then_evidence_then_structure",
        "section_coordination_contract": {
            "required_if_selected_candidate_sharded": [plan["candidate_ids"][0]],
            "candidate_section_hashes": {
                candidate_id: {
                    row["section_id"]: row["section_hash"]
                    for row in plan["candidate_section_index"][candidate_id]
                }
                for candidate_id in plan["candidate_ids"]
            },
            "output_fields": "Keep every selected source section and hash bound.",
        },
    }
    prepared = {"plan": plan, "source_request": source, "node_id": "arbitration", "output_limit": 4096}

    first = executor._run_arbitration_hierarchy(prepared, dependencies)
    assert first["selected_candidate_id"] == plan["candidate_ids"][0]
    assert provider_calls == [leaf_node, final_node]
    assert executor._provider_call_count == 2
    assert len(persisted_dynamic) == 2

    expected_nodes = {leaf_node, final_node}
    receipts_by_call = {row.call_id: row for row in executor._receipt_ledger.list_receipts()}
    for node_id in expected_nodes:
        expected = executor._expected_provider_calls[f"outline:{node_id}"]
        assert expected.node_id == node_id
        assert expected.call_id == f"outline:{node_id}"
        assert expected.provider_response_hash
        assert expected.artifact_content_hash
        assert expected.registry_file_hash
        receipt = receipts_by_call[expected.call_id]
        assert expected.node_id == receipt.node_id
        assert expected.prompt_id == receipt.prompt_id
        assert expected.prompt_version == receipt.prompt_version
        assert expected.prompt_sha256 == receipt.prompt_sha256
        assert expected.prompt_hash == receipt.prompt_hash
        assert expected.input_hash == receipt.input_hash
        assert expected.config_hash == receipt.config_hash
        assert expected.schema_hash == receipt.schema_hash
        assert expected.provider == receipt.provider
        assert expected.model == receipt.model
        assert expected.endpoint == receipt.endpoint
        assert expected.endpoint_type == receipt.endpoint_type
        artifact_id = f"outline-v3:semantic-provider:{hash_text(node_id)[:24]}"
        record = executor.registry.get(artifact_id)
        assert record is not None and record.status == "ready"
        assert record.artifact_role == "outline_v3_semantic_provider_output"
        assert record.artifact_type == "outline_artifact" and record.artifact_version == "v3"
        assert record.metadata["node_id"] == node_id
        executor.registry.verify_ready_artifact_closure(record)
        envelope = json.loads(Path(record.path).read_text(encoding="utf-8"))
        assert envelope["artifact_type"] == "outline_artifact"
        expected_output_dependencies = {
            f"input_{index}": value for index, value in enumerate(dependencies.values())
        }
        expected_output_dependencies[f"input_{len(dependencies)}"] = plan["plan_hash"]
        expected_output_dependencies[f"input_{len(dependencies) + 1}"] = (
            leaf["request_hash"] if node_id == leaf_node
            else first["hierarchical_evaluation"]["ledger_hash"]
        )
        assert envelope["dependency_hashes"] == expected_output_dependencies
        with pytest.raises(KeyError):
            executor._dag.get(node_id)

    audits = [row for row in executor._request_payload_audit if row.get("node_id") in expected_nodes]
    assert len(audits) == 2
    assert all(row["role"] == "arbitration" for row in audits)
    assert {row["node_id"] for row in audits} == expected_nodes
    assert all(row["estimated_input_tokens"] <= 32_000 for row in audits)
    assert provider_inputs[0]["_prompt_authority"]["node_id"] == leaf_node
    assert provider_inputs[1]["_prompt_authority"]["node_id"] == final_node
    assert all(executor._node_route(node, "arbitration").role == "arbitration" for node in expected_nodes)

    replay_store = ModelCallReplayStore(executor.workspace)
    records_before = replay_store._read_records()
    assert {row.audit_node_id for row in records_before} == expected_nodes
    assert all(row.key.node_id == row.audit_node_id for row in records_before)
    for record in records_before:
        assert record.output_artifact_ids == [
            f"outline-v3:semantic-provider:{hash_text(record.audit_node_id)[:24]}"
        ]
        assert record.receipt_ids

    for node_id in expected_nodes:
        executor.artifact_records.pop(node_id)
        executor.artifact_paths.pop(node_id)
    second = executor._run_arbitration_hierarchy(prepared, dependencies)
    assert second == first
    assert provider_calls == [leaf_node, final_node], "the exact second execution must reuse both saved provider calls"
    assert executor._provider_call_count == 2
    assert len(replay_store._read_records()) == 2
    for node_id in expected_nodes:
        restored = executor.artifact_records[node_id]
        refs = executor._dependency_refs((node_id,))
        assert len(refs) == 1 and refs[0].artifact_id == restored.artifact_id
        assert refs[0].content_hash == restored.content_hash and refs[0].path == restored.path
    assert all(row["lookup_status"] == "hit" and not row["provider_invoked"] for row in executor._replay_evidence[-2:])
    assert len([row for row in executor._request_payload_audit if row.get("node_id") in expected_nodes]) == 4

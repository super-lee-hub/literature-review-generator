from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from outline.semantic_final_fold_response import topic_fragment_inputs
from outline.semantic_global_view import expand_global_retention_request
from outline.semantic_request_wire import WIRE_KEY, expand_semantic_request_wire
from outline.v3_artifacts import OutlineArtifact
from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_runtime import ProviderCallReceiptV1, hash_json
from services.artifact_registry import RegistryError
from services.job_workspace import publish_json_artifact
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_run_first_semantic_reducer_closure import _source_comparison


NODE = "cross_group_comparison_provider"
QUESTION = "Compare B/C while retaining every reported sample boundary."


def _case(tmp_path: Path, *, invalid_leaf: str = "") -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    def no_provider(_node: str, _request: Any) -> None:
        pytest.fail("deterministic final retention must not invoke a provider")

    executor = _executor(tmp_path, provider=no_provider, stability_mode="off")
    source, source_claims, evidence, field_values = _source_comparison()
    topic_id = source["topic_id"]
    topics = [{
        "topic_id": topic_id,
        "question": QUESTION,
        "bridge_paper_ids": [],
        "fragments": [{
            "fragment_id": f"fragment:{index}",
            "paper_ids": source["paper_ids"],
            "provider_results": [{
                "batch_id": f"batch:{index}",
                "result_id": f"result:{index}",
                "provider_output": {
                    "topic": {"supporting_evidence_ids": sorted(evidence)},
                    "claims": deepcopy(source["claims"]),
                },
                "interpretation_context": deepcopy(source["interpretation_context"]),
            }],
        } for index in range(2)],
    }]
    for node, payload, dependencies in (
        ("semantic_chunk_plan", {"cross_group_questions": [QUESTION]}, ()),
        ("topic_synthesis", {"topics": topics}, ("semantic_chunk_plan",)),
    ):
        artifact = executor._artifact(OutlineArtifact, payload)
        record = publish_json_artifact(
            executor.publication_context, executor.registry, executor._node_path(node),
            artifact.to_dict(), artifact_role="outline_v3_node_output",
            artifact_type=artifact.artifact_type, artifact_version="v3",
            producer="outline.v3_executor.OutlineV3Executor", artifact_id=f"outline-v3:{node}",
            depends_on=executor._dependency_refs(dependencies),
            metadata={"job_id": executor.job_id, "node_id": node, "content_hash": artifact.content_hash},
        )
        executor.artifact_records[node] = record
        executor.artifact_paths[node] = record.path
    bases = topic_fragment_inputs(topics)
    items = []
    for index, base in enumerate(bases):
        reducer_id = f"{NODE}:reduce:1:{index}"
        bridge_id = f"synthesis:cross_group_comparison:leaf:{index}"
        disposition = (
            {"topic_id": topic_id, "status": "integrated", "synthesis_claim_ids": [bridge_id]}
            if index == 0 else
            {"topic_id": topic_id, "status": "deferred", "reason": "The second fragment has no complete comparison; retain its zero-effect uncertainty."}
        )
        result = {
            "comparisons": [],
            "bridge_claims": [{
                "claim_id": bridge_id,
                "topic_ids": [topic_id],
                "paper_keys": source["paper_ids"],
                "text": "The B/C findings retain these exact boundaries: " + "; ".join(field_values.values()),
                "source_claim_ids": sorted(source_claims),
                "evidence_ids": sorted(evidence),
                "source_field_ids": sorted(field_values),
            }] if index == 0 else [],
            "topic_dispositions": [disposition],
            "unresolved_questions": ["Does the unobserved follow-up change the result?"] if index else [],
        }
        if invalid_leaf and index == 0:
            bridge = result["bridge_claims"][0]
            if invalid_leaf == "foreign_paper":
                bridge["paper_keys"] = ["paper:foreign"]
            elif invalid_leaf == "missing_qualifier":
                bridge["source_field_ids"] = []
            else:
                bridge["evidence_ids"] = ["evidence:foreign"]
        manifest = {
            "schema_version": "outline-semantic-reducer-input/v1",
            "node_id": reducer_id,
            "reduction_level": 1,
            "topic_ids": [topic_id],
            "fragment_ids": [base["fragment_id"]],
            "result_ids": base["result_ids"],
            "relation_ids": [],
            "paper_ids": source["paper_ids"],
            "topic_members_by_id": {topic_id: source["paper_ids"]},
            "fragment_members_by_id": {base["fragment_id"]: source["paper_ids"]},
            "relation_members_by_id": {},
            "source_field_ids": sorted(field_values),
            "source_item_hashes": [hash_json(base)],
            "source_input_hash": hash_json([base]),
            "child_input_manifest_hashes": [],
        }
        coverage = executor._persist_semantic_reducer_input_manifest(
            reducer_id, manifest, dependency_hashes={"topic_synthesis": "source"},
        )
        result_record = executor._persist_semantic_provider_output(
            reducer_id, result, dependency_hashes={"topic_synthesis": "source"},
        )
        group = {"status": disposition["status"], "reason": disposition.get("reason", ""), "topic_ids": [topic_id], "synthesis_claim_ids_by_topic": {}}
        if index == 0:
            group["synthesis_claim_ids_by_topic"] = {topic_id: [bridge_id]}
        membership = {key: manifest[key] for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")}
        items.append({
            "reducer_id": reducer_id,
            "topic_ids": [topic_id],
            "paper_ids": source["paper_ids"],
            "source_claim_ids": sorted(source_claims),
            "evidence_ids": sorted(evidence),
            "interpretation_context": deepcopy(source["interpretation_context"]),
            "topic_disposition_groups": [group],
            "semantic_result": {key: value for key, value in result.items() if key != "topic_dispositions"},
            "coverage_ref": {
                "schema_version": "outline-semantic-reducer-coverage-ref/v1",
                "artifact_id": coverage.artifact_id,
                "registry_file_hash": coverage.content_hash,
                "artifact_content_hash": coverage.metadata["content_hash"],
                "node_id": reducer_id,
                "manifest_hash": hash_json(manifest),
                "membership_hash": hash_json(membership),
                "topic_count": 1, "fragment_count": 1, "result_count": 1,
                "relation_count": 0, "paper_count": 2,
            },
            "semantic_result_ref": {
                "schema_version": "outline-semantic-reducer-result-ref/v1",
                "artifact_id": result_record.artifact_id,
                "registry_file_hash": result_record.content_hash,
                "artifact_content_hash": result_record.metadata["content_hash"],
                "provider_result_hash": hash_json(result),
            },
        })
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "hierarchy": {"level": "bounded_semantic_final_fold", "stage": "cross_group_comparison", "reduction_level": 2},
        "questions": [QUESTION],
        "topic_synthesis": items,
        "relation_candidates": [],
        "output_contract": {},
    }
    return executor, items, bases, request


def _prepare(executor: Any, items: Any, bases: Any, request: dict[str, Any]) -> dict[str, Any]:
    catalog = executor._prepare_final_fold_catalog(NODE, items, bases, {}, request["questions"])
    request["final_fold_retention_contract"] = {"catalog_hash": catalog["catalog_hash"]}
    return catalog


def test_local_final_fold_preserves_sources_and_fragment_states_without_provider_work(tmp_path: Path) -> None:
    executor, items, bases, request = _case(tmp_path)
    catalog = _prepare(executor, items, bases, request)
    receipts_before = executor._receipt_ledger.list_receipts()
    pending_before = dict(executor._pending_replays)
    expected_before = dict(executor._expected_provider_calls)
    assert not executor._replay_store.path.exists()

    result = executor._compile_final_fold_retention(NODE, request, {})

    assert executor._receipt_ledger.list_receipts() == receipts_before == ()
    assert executor._pending_replays == pending_before == {}
    assert executor._expected_provider_calls == expected_before
    assert result["bridge_claims"] == items[0]["semantic_result"]["bridge_claims"]
    assert result["unresolved_questions"] == items[1]["semantic_result"]["unresolved_questions"]
    assert result["topic_dispositions"][0]["integration_scope"] == "partial_source_fragments"
    occurrences = result["fragment_aggregation"]["occurrences"]
    assert [entry["row"]["status"] for entry in occurrences] == ["integrated", "deferred"]
    assert occurrences[1]["row"]["reason"] == items[1]["topic_disposition_groups"][0]["reason"]
    assert result["fragment_aggregation"]["original_questions"] == [QUESTION]
    assert result["fragment_aggregation"]["question_disposition"] == "not_adjudicated_by_local_aggregation"
    state = executor._final_fold_results[NODE]
    record = executor.registry.get(state["artifact_id"])
    assert record is not None
    assert record.artifact_role == "outline_v3_semantic_local_aggregate"
    assert record.metadata["provider_invoked"] is False
    assert record.metadata["execution_kind"] == "validated_leaf_aggregation"
    executor.registry.verify_ready_artifact_closure(record)
    catalog_record = executor.registry.get(state["catalog_artifact_id"])
    assert catalog_record is not None
    assert {(dep.artifact_id, dep.content_hash) for dep in catalog_record.depends_on} == {
        (source.artifact_id, source.content_hash) for source in executor._final_fold_catalogs[catalog["catalog_hash"]][1]
    }
    assert catalog_record.artifact_id in {dep.artifact_id for dep in record.depends_on}
    request_record = executor.registry.get(state["request_artifact_id"])
    assert request_record is not None
    assert request_record.artifact_role == "outline_v3_semantic_local_aggregate_request"
    assert request_record.artifact_id in {dep.artifact_id for dep in record.depends_on}
    assert [dep.artifact_id for dep in request_record.depends_on] == [catalog_record.artifact_id]
    assert executor._persist_semantic_provider_output(NODE, result, dependency_hashes={}) == record
    assert executor._receipt_ledger.list_receipts() == ()
    assert not executor._replay_store.path.exists()


@pytest.mark.parametrize("mutation", ["base_fragment", "questions", "result_file", "coverage_file"])
def test_final_fold_preparation_rejects_changed_owned_source(tmp_path: Path, mutation: str) -> None:
    executor, items, bases, request = _case(tmp_path)
    if mutation == "base_fragment":
        bases[0]["fragment_id"] = "foreign-fragment"
    elif mutation == "questions":
        request["questions"] = ["A different scientific question"]
    else:
        ref = items[0]["semantic_result_ref" if mutation == "result_file" else "coverage_ref"]
        record = executor.registry.get(ref["artifact_id"])
        Path(record.path).write_text("{}", encoding="utf-8")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        _prepare(executor, items, bases, request)
    assert executor._receipt_ledger.list_receipts() == ()
    assert NODE not in executor._final_fold_results


@pytest.mark.parametrize("invalid_leaf", ["foreign_paper", "missing_qualifier", "foreign_evidence"])
def test_full_semantic_validator_rejects_an_invalid_sealed_leaf(tmp_path: Path, invalid_leaf: str) -> None:
    executor, items, bases, request = _case(tmp_path, invalid_leaf=invalid_leaf)
    # The catalog codec preserves sealed rows, but cannot decide their scientific
    # source ownership. A genuinely registered bad leaf must still reach and fail
    # the production semantic validator when the final aggregate is compiled.
    _prepare(executor, items, bases, request)
    with pytest.raises(OutlineV3ExecutionError, match="paper|source|membership|qualifier|evidence"):
        executor._compile_final_fold_retention(NODE, request, {})
    assert NODE not in executor._final_fold_results


@pytest.mark.parametrize("existing", ["pending", "receipt"])
@pytest.mark.parametrize("phase", ["compile", "fresh_restore"])
def test_local_final_fold_cannot_hide_current_epoch_provider_work(tmp_path: Path, existing: str, phase: str) -> None:
    executor, items, bases, request = _case(tmp_path)
    _prepare(executor, items, bases, request)
    parent = None
    if phase == "fresh_restore":
        result = executor._compile_final_fold_retention(NODE, request, {})
        parent = {
            "execution_mode": "source_closed_retention", "provider_output": result,
            "semantic_retention": {**executor._final_fold_results[NODE], "provider_invoked": False},
        }
        executor = _executor(tmp_path, stability_mode="off")
    if existing == "pending":
        executor._pending_replays[NODE] = ("pending-key", "a" * 64, "receipt-pending")
    else:
        executor._receipt_ledger.append(ProviderCallReceiptV1(
            artifact_type="provider_call_receipt", artifact_version="v2", receipt_id="receipt-existing", sequence=1,
            job_id=executor.job_id, attempt_id="attempt-final-fold", stage_name="outline_v3", route="Outline_API",
            provider="local-fixture", model="fixture-model", endpoint="http://127.0.0.1:1",
            prompt_hash="a" * 64, input_hash="b" * 64, config_hash="c" * 64, schema_hash="d" * 64,
            status="success", error_kind=None, http_status=200, provider_code=None, attempts=1,
            retry_after_seconds=None, input_tokens=1, output_tokens=1, total_tokens=2, response_hash="e" * 64,
            started_at="2026-10-24T00:00:00Z", finished_at="2026-10-24T00:00:01Z", budget={},
            node_id=NODE, call_id=executor._provider_call_id(NODE), closure_epoch_id=executor.closure_epoch_id,
            endpoint_type="chat_completions", logical_attempt_identity="attempt-final-fold",
        ))
    with pytest.raises(OutlineV3ExecutionError, match="cannot hide"):
        if parent is None:
            executor._compile_final_fold_retention(NODE, request, {})
        else:
            executor._restore_local_final_fold(NODE, parent)
    assert NODE not in executor._final_fold_results
    assert len(executor._receipt_ledger.list_receipts()) == (1 if existing == "receipt" else 0)


def test_fresh_executor_restores_exact_local_aggregate_without_provider_replay(tmp_path: Path) -> None:
    executor, items, bases, request = _case(tmp_path)
    _prepare(executor, items, bases, request)
    result = executor._compile_final_fold_retention(NODE, request, {})
    state = deepcopy(executor._final_fold_results[NODE])
    parent = {
        "execution_mode": "source_closed_retention",
        "provider_output": result,
        "semantic_retention": {**state, "provider_invoked": False},
    }
    fresh = _executor(tmp_path, stability_mode="off")
    assert fresh._final_fold_results == {}
    assert fresh.closure_epoch_id == executor.closure_epoch_id

    fresh._restore_local_final_fold(NODE, parent)

    assert fresh._final_fold_results[NODE] == parent["semantic_retention"]
    record = fresh._persist_semantic_provider_output(NODE, result, dependency_hashes={})
    assert record.artifact_id == state["artifact_id"]
    assert fresh._receipt_ledger.list_receipts() == ()
    assert fresh._pending_replays == {}
    assert fresh._provider_call_id(NODE) not in fresh._expected_provider_calls
    assert not fresh._replay_store.path.exists()


@pytest.mark.parametrize("mutation", ["request_bytes", "catalog_bytes", "aggregate_bytes", "source_bytes", "parent_result", "epoch"])
def test_fresh_executor_rejects_tampered_local_retention_authority(tmp_path: Path, mutation: str) -> None:
    executor, items, bases, request = _case(tmp_path)
    _prepare(executor, items, bases, request)
    result = executor._compile_final_fold_retention(NODE, request, {})
    state = deepcopy(executor._final_fold_results[NODE])
    parent = {"execution_mode": "source_closed_retention", "provider_output": deepcopy(result), "semantic_retention": {**state, "provider_invoked": False}}
    fresh = _executor(tmp_path, stability_mode="off")
    if mutation == "parent_result":
        parent["provider_output"]["bridge_claims"][0]["source_field_ids"] = []
    elif mutation == "epoch":
        parent["semantic_retention"]["closure_epoch_id"] = "foreign-epoch"
    else:
        artifact_id = {
            "request_bytes": state["request_artifact_id"],
            "catalog_bytes": state["catalog_artifact_id"],
            "aggregate_bytes": state["artifact_id"],
            "source_bytes": items[0]["semantic_result_ref"]["artifact_id"],
        }[mutation]
        record = executor.registry.get(artifact_id)
        Path(record.path).write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        fresh._restore_local_final_fold(NODE, parent)
    assert NODE not in fresh._final_fold_results
    assert fresh._receipt_ledger.list_receipts() == ()


def _global_case(tmp_path: Path) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    executor, items, bases, cross_request = _case(tmp_path)
    _prepare(executor, items, bases, cross_request)
    cross = executor._compile_final_fold_retention(NODE, cross_request, {})
    ledger = executor._build_semantic_coverage_ledger(bases, [], cross)
    executor._payloads["cross_group_comparison"] = {"coverage_ledger": ledger, "provider_output": cross}
    request = {
        "task": "substantive_global_synthesis", "node_id": "global_synthesis",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": bases, "cross_group_comparison": cross,
        "cross_coverage_ledger_ref": {"content_hash": ledger["content_hash"], "topic_count": len(ledger["topic_ids"])},
        "relation_candidates": [], "questions": [QUESTION], "output_contract": {},
    }
    claim = deepcopy(cross["bridge_claims"][0])
    claim["claim_id"] = "synthesis:global_synthesis:qualified"
    output = {"synthesis_claims": [claim], "organizing_principles": ["Organize by source-reported conditions; retain the deferred fragment."], "unresolved_questions": cross["unresolved_questions"]}
    return executor, request, cross, output


def test_global_provider_uses_lossless_view_and_keeps_physical_receipt_replay_hash(tmp_path: Path) -> None:
    executor, request, cross, output = _global_case(tmp_path)
    physical_requests = []

    def provider(node: str, wire: Any) -> dict[str, Any]:
        assert node == "global_synthesis_provider"
        physical_requests.append(deepcopy(wire))
        assert wire[WIRE_KEY]["schema_version"] == "semantic-source-tables-and-strings/v2"
        assert wire[WIRE_KEY]["source_keys"] == ["topic_synthesis", "cross_group_comparison"]
        view = expand_semantic_request_wire(wire)
        projected = view["cross_group_comparison"]["fragment_aggregation"]
        assert projected["schema_version"] == "outline-semantic-final-fold-scientific-view/v1"
        assert "occurrences" not in projected
        assert [row["disposition"]["status"] for row in projected["source_dispositions"]] == ["integrated", "deferred"]
        restored = expand_global_retention_request(view, source=cross)
        assert restored["cross_group_comparison"] == request["cross_group_comparison"]
        assert restored["topic_synthesis"] == request["topic_synthesis"]
        return {"status": "success", "content": deepcopy(output)}

    executor.provider = provider
    result = executor._run_bounded_semantic_provider_call(
        "global_synthesis_provider", request, {"cross_group_comparison": hash_json(cross)},
    )
    assert result == output
    assert len(physical_requests) == 1
    receipts = executor._receipt_ledger.list_receipts()
    assert len(receipts) == 1
    assert receipts[0].response_hash == hash_json(output)
    assert executor._pending_replays["global_synthesis_provider"][1] == hash_json(output)
    record = executor._persist_semantic_provider_output(
        "global_synthesis_provider", result, dependency_hashes={"cross_group_comparison": hash_json(cross)},
    )
    physical = json.loads(Path(record.path).read_text(encoding="utf-8"))
    assert physical["payload"] == output
    replays = executor._replay_store._read_records()
    assert len(replays) == 1
    assert replays[0].normalized_output_hash == hash_json(output)
    assert replays[0].output_hash == hash_json(output)
    assert replays[0].output_artifact_ids == [record.artifact_id]
    assert replays[0].receipt_ids == [receipts[0].receipt_id]
    assert executor._pending_replays == {}
    assert next(row for row in executor._request_payload_audit if row["node_id"] == "global_synthesis_provider")["semantic_validation_status"] == "accepted"


@pytest.mark.parametrize("mutation", ["aggregate_bytes", "aggregate_reference", "expanded_cross"])
def test_global_provider_rejects_stale_retained_authority_before_callback(tmp_path: Path, mutation: str) -> None:
    executor, request, cross, _output = _global_case(tmp_path)
    state = executor._final_fold_results[NODE]
    if mutation == "aggregate_bytes":
        record = executor.registry.get(state["artifact_id"])
        Path(record.path).write_text("{}", encoding="utf-8")
    elif mutation == "aggregate_reference":
        state["registry_file_hash"] = "0" * 64
    else:
        request["cross_group_comparison"] = deepcopy(cross)
        request["cross_group_comparison"]["bridge_claims"][0]["source_field_ids"] = []
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._run_bounded_semantic_provider_call("global_synthesis_provider", request, {})
    assert executor._receipt_ledger.list_receipts() == ()
    assert not executor._replay_store.path.exists()


def test_global_reply_cannot_omit_qualifier_from_full_original_request(tmp_path: Path) -> None:
    executor, request, cross, output = _global_case(tmp_path)
    output["synthesis_claims"][0]["source_field_ids"] = []
    observed = []

    def provider(_node: str, wire: Any) -> dict[str, Any]:
        observed.append(wire)
        view = expand_semantic_request_wire(wire)
        assert expand_global_retention_request(view, source=cross)["cross_group_comparison"] == cross
        return {"status": "success", "content": output}

    executor.provider = provider
    with pytest.raises(OutlineV3ExecutionError, match="qualifier|source_field"):
        executor._run_bounded_semantic_provider_call("global_synthesis_provider", request, {"cross_group_comparison": hash_json(cross)})
    assert len(observed) == 1
    assert executor._receipt_ledger.list_receipts()[0].response_hash == hash_json(output)
    assert not executor._replay_store.path.exists()
    audit = next(row for row in executor._request_payload_audit if row["node_id"] == "global_synthesis_provider")
    assert audit["semantic_validation_status"] == "rejected"


def test_global_source_drift_after_transport_keeps_receipt_but_rejects_semantic_replay(tmp_path: Path) -> None:
    executor, request, cross, output = _global_case(tmp_path)
    aggregate = executor.registry.get(executor._final_fold_results[NODE]["artifact_id"])
    calls = []

    def provider(_node: str, wire: Any) -> dict[str, Any]:
        calls.append(wire)
        assert expand_global_retention_request(expand_semantic_request_wire(wire), source=cross)["cross_group_comparison"] == cross
        # The request was legitimately admitted; simulate bytes changing while
        # its physical response is in flight. Preserve the response receipt and
        # prevent this stale source from acquiring reusable semantic output.
        Path(aggregate.path).write_text("{}", encoding="utf-8")
        return {"status": "success", "content": output}

    executor.provider = provider
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._run_bounded_semantic_provider_call("global_synthesis_provider", request, {"cross_group_comparison": hash_json(cross)})
    assert len(calls) == 1
    receipts = executor._receipt_ledger.list_receipts()
    assert len(receipts) == 1 and receipts[0].response_hash == hash_json(output)
    assert not executor._replay_store.path.exists()


@pytest.mark.parametrize("reference_key", ["coverage_ref", "semantic_result_ref"])
def test_restore_requires_direct_leaf_dependency_even_when_catalog_bytes_are_unchanged(tmp_path: Path, reference_key: str) -> None:
    executor, items, bases, request = _case(tmp_path)
    _prepare(executor, items, bases, request)
    result = executor._compile_final_fold_retention(NODE, request, {})
    state = deepcopy(executor._final_fold_results[NODE])
    parent = {"execution_mode": "source_closed_retention", "provider_output": result, "semantic_retention": {**state, "provider_invoked": False}}
    catalog = executor.registry.get(state["catalog_artifact_id"])
    before_bytes = Path(catalog.path).read_bytes()
    removed_id = items[0][reference_key]["artifact_id"]
    assert removed_id in {dep.artifact_id for dep in catalog.depends_on}
    executor.registry.update_record(
        catalog.artifact_id,
        depends_on=[dep for dep in catalog.depends_on if dep.artifact_id != removed_id],
    )
    assert Path(catalog.path).read_bytes() == before_bytes
    fresh = _executor(tmp_path, stability_mode="off")
    with pytest.raises(OutlineV3ExecutionError, match="typed source dependency"):
        fresh._restore_local_final_fold(NODE, parent)
    assert NODE not in fresh._final_fold_results
    assert fresh._receipt_ledger.list_receipts() == ()
    assert not fresh._replay_store.path.exists()

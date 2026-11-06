from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from outline.semantic_identifier_aliases import (
    IDENTIFIER_FIELDS,
    WIRE_KEY as ALIAS_KEY,
    SemanticIdentifierAliasError,
    decode_identifier_alias_output,
    expand_identifier_alias_request,
)
from outline.semantic_request_wire import WIRE_KEY, expand_semantic_request_wire, pack_semantic_request_wire
from outline.v3_artifacts import OutlineArtifact
from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, RegistryError
from services.job_workspace import publish_json_artifact
from tests import test_semantic_final_fold_runtime as fold_fixtures
from tests.test_outline_v3_semantic_execution import _executor


NODE = "global_synthesis_provider"


def _encode_output(value, forward, *, structural=False):
    if isinstance(value, dict):
        return {key: _encode_output(item, forward, structural=key in IDENTIFIER_FIELDS) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode_output(item, forward, structural=structural) for item in value]
    return forward.get(value, value) if structural and isinstance(value, str) else deepcopy(value)


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fault: str = "") -> dict[str, Any]:
    original_source = fold_fixtures._source_comparison

    def long_source():
        # Only the controlled source fixture is replaced; all Registry, source,
        # request, provider-runtime and semantic-validation gates remain real.
        source, claims, evidence, fields = original_source()
        mapping = {value: f"{value}:opaque:{index}:" + "x" * 3_500 for index, value in enumerate(sorted({*claims, *evidence, *source["paper_ids"]}))}

        def rewrite(value):
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return mapping.get(value, value) if isinstance(value, str) else value

        return rewrite(source), {mapping[value] for value in claims}, {mapping[value] for value in evidence}, fields

    monkeypatch.setattr(fold_fixtures, "_source_comparison", long_source)
    executor, request, cross, canonical_output = fold_fixtures._global_case(tmp_path)
    # Preserve the original conservative 32k alias-regression case even when
    # the default route advertises a larger input cap.
    executor.max_source_prompt_tokens = 32_000
    cross_parent = {
        "execution_mode": "source_closed_retention", "provider_output": cross,
        "coverage_ledger": executor._payloads["cross_group_comparison"]["coverage_ledger"],
        "semantic_retention": {**executor._final_fold_results[fold_fixtures.NODE], "provider_invoked": False},
    }
    aggregate_record = executor.registry.get(cross_parent["semantic_retention"]["artifact_id"])
    parent_artifact = executor._artifact(OutlineArtifact, cross_parent)
    parent_record = publish_json_artifact(
        executor.publication_context, executor.registry, executor._node_path("identifier_test_cross_parent"),
        parent_artifact.to_dict(), artifact_role="outline_v3_node_output",
        artifact_type="outline_artifact", artifact_version="v3",
        producer="outline.v3_executor.OutlineV3Executor", artifact_id="test:identifier-cross-parent",
        depends_on=[ArtifactDependencyRefV2.from_record(aggregate_record)],
        metadata={"job_id": executor.job_id, "node_id": "cross_group_comparison", "content_hash": parent_artifact.content_hash},
    )
    observed = []
    case = {"executor": executor, "request": request, "cross": cross, "canonical_output": canonical_output, "parent_record": parent_record, "observed": observed}

    def provider(node, wire):
        assert node == NODE
        assert wire[WIRE_KEY]["schema_version"] == "semantic-source-tables-and-strings/v2"
        aliased = expand_semantic_request_wire(wire)
        assert ALIAS_KEY in aliased
        catalog_hash = aliased[ALIAS_KEY]["catalog_hash"]
        catalog, canonical_request = executor._semantic_identifier_plans[catalog_hash]
        profile = executor._node_route("candidate_1_provider_generation").profile
        unaliased_wire = pack_semantic_request_wire(canonical_request, source_keys=("topic_synthesis", "cross_group_comparison"))
        input_cap = executor._effective_input_cap(profile)
        assert profile.estimate_tokens(unaliased_wire) > input_cap
        assert profile.estimate_tokens(wire) < input_cap
        assert hash_json(expand_identifier_alias_request(aliased, catalog)) == hash_json(canonical_request)
        assert "forward" not in aliased[ALIAS_KEY] and "reverse" not in aliased[ALIAS_KEY]
        raw = _encode_output(canonical_output, catalog["forward"])
        assert raw["synthesis_claims"][0]["claim_id"] == "synthesis:global_synthesis:qualified"
        assert raw["synthesis_claims"][0]["source_claim_ids"] != canonical_output["synthesis_claims"][0]["source_claim_ids"]
        if fault == "unknown_alias":
            raw["synthesis_claims"][0]["source_claim_ids"] = [catalog["prefix"] + "999999"]
        elif fault == "missing_qualifier":
            raw["synthesis_claims"][0]["source_field_ids"] = []
        observed.append({"wire": deepcopy(wire), "raw": deepcopy(raw), "catalog": catalog, "unaliased_tokens": profile.estimate_tokens(unaliased_wire), "aliased_tokens": profile.estimate_tokens(wire)})
        return {"status": "success", "content": raw}

    executor.provider = provider
    return case


def _run(case):
    return case["executor"]._run_bounded_semantic_provider_call(NODE, case["request"], {"cross_group_comparison": hash_json(case["cross"])})


def _fresh(case, tmp_path):
    fresh = _executor(tmp_path, stability_mode="off")
    parent_record = fresh.registry.get(case["parent_record"].artifact_id)
    fresh.registry.verify_ready_artifact_closure(parent_record)
    parent = json.loads(Path(parent_record.path).read_text(encoding="utf-8"))["payload"]
    fresh._restore_local_final_fold(fold_fixtures.NODE, parent)
    fresh._payloads["cross_group_comparison"] = parent
    return fresh


def test_actual_alias_branch_preserves_raw_receipt_replay_and_typed_expanded_output(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    result = _run(case)
    executor = case["executor"]
    assert result == case["canonical_output"]
    assert len(case["observed"]) == 1
    raw = case["observed"][0]["raw"]
    assert hash_json(raw) != hash_json(result)
    assert decode_identifier_alias_output(raw, case["observed"][0]["catalog"]) == result
    receipt, = executor._receipt_ledger.list_receipts()
    assert receipt.response_hash == hash_json(raw)
    assert receipt.input_hash == hash_json(case["observed"][0]["wire"])
    state = executor._semantic_identifier_outputs[NODE]
    assert state["physical_response_hash"] == hash_json(raw)
    assert state["expanded_response_hash"] == hash_json(result)
    physical = executor.registry.get(state["physical_artifact_id"])
    expanded = executor.registry.get(state["expanded_artifact_id"])
    plan = executor.registry.get(state["map_artifact_id"])
    assert json.loads(Path(physical.path).read_text(encoding="utf-8"))["payload"] == raw
    assert json.loads(Path(expanded.path).read_text(encoding="utf-8"))["payload"] == result
    assert physical.artifact_role == "outline_v3_semantic_provider_output"
    assert expanded.artifact_role == "outline_v3_semantic_identifier_expanded_output"
    assert plan.artifact_role == "outline_v3_semantic_identifier_alias_map"
    assert {dep.artifact_id for dep in expanded.depends_on} == {physical.artifact_id, plan.artifact_id}
    assert plan.artifact_id in {dep.artifact_id for dep in physical.depends_on}
    executor.registry.verify_ready_artifact_closure(expanded)
    replay, = executor._replay_store._read_records()
    assert replay.normalized_output_hash == replay.output_hash == hash_json(raw)
    assert replay.output_artifact_ids == [physical.artifact_id]
    assert replay.receipt_ids == [receipt.receipt_id]
    assert executor._persist_semantic_provider_output(NODE, result, dependency_hashes={}) == physical
    assert len(executor._receipt_ledger.list_receipts()) == 1
    fresh = _fresh(case, tmp_path)
    fresh._restore_semantic_identifier_output(NODE, result, state)
    assert fresh._semantic_identifier_outputs[NODE] == state
    assert fresh._persist_semantic_provider_output(NODE, result, dependency_hashes={}) == physical
    assert len(fresh._receipt_ledger.list_receipts()) == 1
    assert len(fresh._replay_store._read_records()) == 1


@pytest.mark.parametrize("fault", ["unknown_alias", "missing_qualifier"])
def test_invalid_alias_reply_retains_physical_receipt_without_semantic_replay(tmp_path, monkeypatch, fault):
    case = _case(tmp_path, monkeypatch, fault=fault)
    with pytest.raises((OutlineV3ExecutionError, SemanticIdentifierAliasError)):
        _run(case)
    executor = case["executor"]
    assert len(case["observed"]) == 1
    receipt, = executor._receipt_ledger.list_receipts()
    assert receipt.response_hash == hash_json(case["observed"][0]["raw"])
    assert NODE not in executor._semantic_identifier_outputs
    assert not executor._replay_store.path.exists()


@pytest.mark.parametrize("mutation", ["map_bytes", "expanded_bytes", "raw_bytes", "expanded_dependency", "raw_dependency", "map_source_dependency", "request_hash", "expanded_parent"])
def test_fresh_alias_restore_rejects_map_response_dependency_and_request_tamper(tmp_path, monkeypatch, mutation):
    case = _case(tmp_path, monkeypatch)
    result = _run(case)
    executor = case["executor"]
    state = deepcopy(executor._semantic_identifier_outputs[NODE])
    fresh = _fresh(case, tmp_path)
    if mutation in {"map_bytes", "expanded_bytes", "raw_bytes"}:
        key = {"map_bytes": "map", "expanded_bytes": "expanded", "raw_bytes": "physical"}[mutation]
        record = executor.registry.get(state[f"{key}_artifact_id"])
        Path(record.path).write_text("{}", encoding="utf-8")
    elif mutation in {"expanded_dependency", "raw_dependency", "map_source_dependency"}:
        key = {"expanded_dependency": "expanded", "raw_dependency": "physical", "map_source_dependency": "map"}[mutation]
        record = executor.registry.get(state[f"{key}_artifact_id"])
        original_bytes = Path(record.path).read_bytes()
        executor.registry.update_record(record.artifact_id, depends_on=[])
        assert Path(record.path).read_bytes() == original_bytes
    elif mutation == "request_hash":
        state["source_request_hash"] = "0" * 64
    else:
        result = deepcopy(result)
        result["synthesis_claims"][0]["source_field_ids"] = []
    with pytest.raises((OutlineV3ExecutionError, RegistryError, SemanticIdentifierAliasError)):
        fresh._restore_semantic_identifier_output(NODE, result, state)
    assert NODE not in fresh._semantic_identifier_outputs
    assert len(fresh._receipt_ledger.list_receipts()) == 1

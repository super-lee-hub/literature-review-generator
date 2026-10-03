"""The production candidate validator consumes the source-bound output scope."""
from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from tests.test_candidate_output_scope import _valid_payload, source_scope
from tests.test_outline_v3_semantic_execution import _executor


def test_executor_rejects_claim_expansion_outside_finite_scope(tmp_path, source_scope):
    _inventory, paper, _primary, _route, scope = source_scope
    executor = _executor(tmp_path)
    executor._candidate_output_scope = scope
    payload = _valid_payload(scope)
    executor._validate_candidate_payload(
        "candidate_1", payload, allowed_paper_keys=[paper.paper_key],
        allowed_relation_ids=["relation:confirmed:1"],
    )
    payload["sections"][0]["claims"].append("A second unsupported assertion.")
    with pytest.raises(OutlineV3ExecutionError, match="finite output contract"):
        executor._validate_candidate_payload(
            "candidate_1", payload, allowed_paper_keys=[paper.paper_key],
            allowed_relation_ids=["relation:confirmed:1"],
        )


@pytest.mark.parametrize("overflow", ["sections", "support"])
def test_executor_rejects_output_record_proliferation(tmp_path, source_scope, overflow):
    _inventory, paper, _primary, _route, scope = source_scope
    executor = _executor(tmp_path)
    executor._candidate_output_scope = scope
    payload = _valid_payload(scope)
    if overflow == "sections":
        payload["sections"] = [
            {**copy.deepcopy(payload["sections"][0]), "section_id": f"section:{index}"}
            for index in range(scope.max_sections + 1)
        ]
    else:
        payload["sections"][0]["claim_support"] *= scope.max_support_rows + 1
    with pytest.raises(OutlineV3ExecutionError, match="finite output contract"):
        executor._validate_candidate_payload(
            "candidate_1", payload, allowed_paper_keys=[paper.paper_key],
            allowed_relation_ids=["relation:confirmed:1"],
        )


def test_compact_scope_keeps_local_claim_groups_distinguishable(tmp_path, source_scope):
    _inventory, paper, _primary, _route, scope = source_scope
    group = scope.claim_groups[0]
    slot = scope.claim_slots[0]
    second_group = replace(group, claim_group_id="group:second", fragment_id="fragment:second",
                           provider_result_id="result:second", support_slot_ids=("slot:second",))
    second_slot = replace(slot, claim_group_id=second_group.claim_group_id, claim_slot_id="slot:second",
                          fragment_id=second_group.fragment_id, provider_result_id=second_group.provider_result_id)
    executor = _executor(tmp_path)
    executor._candidate_output_scope = replace(scope, claim_groups=(group, second_group),
                                               claim_slots=(slot, second_slot))
    wire = executor._candidate_output_scope_wire([paper.paper_key])
    assert len({item["claim_group_index"] for item in wire["claim_slots"]}) == 2
    assert len({item["synthesis_claim_id"] for item in wire["claim_slots"]}) == 1
    assert all(item["source_claim_ids"] and item["evidence_ids"] for item in wire["claim_slots"])


def test_candidate_shard_uses_one_frozen_alias_projection(tmp_path, source_scope, monkeypatch):
    from outline.evidence_alias import build_alias_map

    _inventory, paper, _primary, _route, scope = source_scope
    executor = _executor(tmp_path)
    executor._candidate_output_scope = scope
    executor.opaque_alias_enabled = True
    executor._alias_map = build_alias_map([paper.paper_key])
    monkeypatch.setattr(executor, "_build_relation_shard_plan", lambda *args, **kwargs: {
        "shards": [{"shard_id": "shard_1", "paper_keys": [paper.paper_key],
                    "evidence_chunks": [{"paper_key": paper.paper_key}]}],
    })
    requests = executor._candidate_shard_requests(
        generation_node_id="candidate_1_provider_generation", provider_request={"candidate_id": "candidate_1"},
        evidence_views=[], relation_candidates=[],
    )
    wire = requests[0][-1]
    assert wire["paper_keys"] == ["P001"]
    assert wire["evidence"][0]["paper_key"] == "P001"
    assert wire["candidate_output_scope"]["claim_slots"][0]["paper_key"] == "P001"


def test_shard_alias_projection_does_not_realias_a_token_shaped_canonical_key(tmp_path, source_scope, monkeypatch):
    from outline.evidence_alias import build_alias_map

    _inventory, paper, _primary, _route, scope = source_scope
    executor = _executor(tmp_path)
    executor._candidate_output_scope = scope
    executor.opaque_alias_enabled = True
    executor._alias_map = build_alias_map([paper.paper_key, "P001"])
    monkeypatch.setattr(executor, "_build_relation_shard_plan", lambda *args, **kwargs: {
        "shards": [{"shard_id": "shard_1", "paper_keys": [paper.paper_key],
                    "evidence_chunks": [{"paper_key": paper.paper_key}]}],
    })
    wire = executor._candidate_shard_requests(
        generation_node_id="candidate_1_provider_generation",
        provider_request={"candidate_id": "candidate_1", "paper_keys": ["P001", "P002"]},
        evidence_views=[], relation_candidates=[],
    )[0][-1]
    assert wire["paper_keys"] == ["P001"]
    assert wire["candidate_output_scope"]["claim_slots"][0]["paper_key"] == "P001"

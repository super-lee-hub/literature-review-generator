from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from outline.candidate_arbitration_ledger_packing import expand_arbitration_final_ledger
from outline.candidate_arbitration_final_projection import expand_arbitration_final_projection
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from runtime.provider_context import ProviderContextProfile
from services.prompt_registry import PromptRegistry
from tests.test_run_first_arbitration_hierarchy import _plan, _response_for


def _runtime_case():
    plan = _plan()
    executor = object.__new__(OutlineV3Executor)
    profile = ProviderContextProfile.conservative(
        provider="local", model="local", endpoint_type="chat_completions",
    )
    executor.max_source_prompt_tokens = 32_000
    executor.prompt_registry = PromptRegistry()
    executor._outline_prompt_identity = executor.prompt_registry.identity("outline.node.system.v3")
    executor._outline_policy_identity = executor.prompt_registry.identity("outline.node.policies.v3")
    executor._provider_call_id = lambda node_id: node_id
    executor._node_route = lambda *_args, **_kwargs: SimpleNamespace(profile=profile)
    calls = []
    persisted = []

    def persist(node_id, payload, **_kwargs):
        persisted.append((node_id, copy.deepcopy(payload)))
        return SimpleNamespace(artifact_id=node_id, content_hash="a" * 64)

    executor._persist_semantic_provider_output = persist

    def provider(node_id, request, **kwargs):
        calls.append((node_id, copy.deepcopy(request), kwargs))
        for leaf in plan["leaves"]:
            if node_id.endswith(leaf["leaf_id"]):
                return _response_for(plan, leaf)
        return {"selected_candidate_id": plan["candidate_ids"][0],
                "selection_reasons": ["Complete section evaluations retain the stated source boundaries."]}

    executor._provider_call = provider
    source = {
        "candidate_ids": plan["candidate_ids"],
        "candidate_hashes": plan["candidate_hashes"],
        "candidate_contents": {candidate: {"organizing_logic": "source evidence"} for candidate in plan["candidate_ids"]},
        "critiques": {"structure_critique": {"passed": True}},
        "critique_disposition": {"eligible_candidate_ids": plan["candidate_ids"], "issues": []},
        "review_intent": {"review_question": "Compare complete source sections."},
        "section_coordination_contract": {
            "candidate_section_hashes": {
                candidate: {row["section_id"]: row["section_hash"] for row in plan["candidate_section_index"][candidate]}
                for candidate in plan["candidate_ids"]
            },
        },
    }
    prepared = {"plan": plan, "source_request": source, "node_id": "arbitration", "output_limit": 4096}
    return executor, prepared, calls, persisted


def test_final_arbitration_receives_every_semantic_leaf_assessment_and_coordination_hash():
    executor, prepared, calls, persisted = _runtime_case()
    decision = executor._run_arbitration_hierarchy(prepared, {"critique": "critic-hash"})
    final = expand_arbitration_final_projection(calls[-1][1])
    ledger = expand_arbitration_final_ledger(final["arbitration_evaluation_ledger"])
    assert ledger["assessments"]
    assert all(row["coverage_assessment"] and row["evidence_assessment"] and row["structure_assessment"]
               for row in ledger["assessments"])
    assert final["candidate_hashes"] == prepared["plan"]["candidate_hashes"]
    assert final["section_coordination_contract"] == prepared["source_request"]["section_coordination_contract"]
    assert ledger["coverage"]["expected_candidate_section_count"] == ledger["coverage"]["observed_candidate_section_count"]
    assert len(calls) == len(prepared["plan"]["leaves"]) + 1
    assert calls[-1][0].startswith("arbitration:final:")
    assert calls[-1][2]["transport_node_id"] == "arbitration"
    assert calls[-1][2]["output_tokens"] <= 2048
    assert len(persisted) == 2
    assert persisted[-1][1]["ledger_hash"] == decision["hierarchical_evaluation"]["ledger_hash"]


def test_invalid_leaf_stops_before_final_decision_and_preserves_evaluation_plan():
    executor, prepared, calls, persisted = _runtime_case()
    executor._provider_call = lambda node_id, request, **kwargs: calls.append(node_id) or {
        "leaf_id": prepared["plan"]["leaves"][0]["leaf_id"],
        "unit_ids": [], "candidate_assessments": [],
    }
    with pytest.raises(OutlineV3ExecutionError, match="leaf coverage invalid"):
        executor._run_arbitration_hierarchy(prepared, {"critique": "critic-hash"})
    assert len(calls) == 1
    assert ":leaf:" in calls[0]
    assert len(persisted) == 1


def test_over_cap_final_arbitration_preserves_every_critic_row_in_one_admissible_decision():
    from outline.critique_coverage_wire import expand_critique_coverage_certificates
    from outline.v3_models import compute_v3_hash
    from runtime.provider_runtime import hash_json

    executor, prepared, calls, persisted = _runtime_case()
    critiques = {}
    for critic in ("structure_critique", "coverage_critique", "evidence_critique"):
        rows = {}
        for index in range(120):
            candidate = prepared["plan"]["candidate_ids"][index % 5]
            rows[f"{candidate}:shard:{index + 1}"] = {
                "candidate_id": candidate,
                "shard_index": index + 1,
                "parent_candidate_hash": prepared["plan"]["candidate_hashes"][candidate],
                "reviewed_section_ids": [],
                "passed": True,
                "blocking_diagnostics": [],
                "issues": [],
                "recommendations": [],
                "score": 0.97,
                "coverage_metrics": {},
                "evidence_metrics": {},
                "structure_metrics": {},
                "feedback": f"{critic} distinct note {index}: retain null, reverse and qualified findings.",
                "qualifier": {"negative_result": False, "observed_effect": 0, "study_id": ""},
            }
        critiques[critic] = {"passed": True, "candidate_shard_results": rows}
    prepared["source_request"]["critiques"] = critiques
    plan = prepared["plan"]
    plan["critique_hashes"] = {key: hash_json(value) for key, value in critiques.items()}
    plan["plan_hash"] = compute_v3_hash({key: value for key, value in plan.items() if key not in {"plan_hash", "max_calls"}})

    executor._run_arbitration_hierarchy(prepared, {"critique": "critic-hash"})

    wire = calls[-1][1]
    profile = executor._node_route("arbitration").profile
    restored = expand_critique_coverage_certificates(wire)
    assert profile.estimate_request(restored)["estimated_input_tokens"] > 32_000
    assert profile.estimate_request(wire)["estimated_input_tokens"] <= 32_000
    assert hash_json(restored["critiques"]) == hash_json(critiques)
    canonical = expand_arbitration_final_projection(restored)
    ledger = expand_arbitration_final_ledger(canonical["arbitration_evaluation_ledger"])
    assert ledger["critique_hashes"] == plan["critique_hashes"]
    assert ledger["assessments"] == persisted[-1][1]["assessments"]
    assert len(calls) == len(plan["leaves"]) + 1


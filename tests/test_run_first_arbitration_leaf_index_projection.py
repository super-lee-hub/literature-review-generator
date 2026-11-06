from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from outline.candidate_arbitration_hierarchy import (
    ArbitrationHierarchyError,
    build_arbitration_final_ledger,
    project_arbitration_leaf_section_index,
)
from outline.candidate_arbitration_ledger_packing import (
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from outline.candidate_arbitration_packing import (
    expand_arbitration_candidate_support,
    pack_arbitration_candidate_support,
)
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import compute_v3_hash
from runtime.provider_context import ProviderContextProfile
from services.prompt_registry import PromptRegistry
from tests.test_run_first_arbitration_hierarchy import (
    _CANDIDATES,
    _inputs,
    _plan,
    _response_for,
)


def _expected_index_rows(plan, leaf, candidate_id):
    selected = leaf["expected_candidate_rows"][candidate_id]["section_hashes"]
    return [
        row for row in plan["candidate_section_index"][candidate_id]
        if selected.get(row["section_id"]) == row["section_hash"]
    ]


def test_four_group_projection_is_lossless_and_does_not_change_final_ledger():
    plan = _plan(input_cap=3_500)
    assert len(plan["leaves"]) > 1
    original_input_hash = plan["input_hash"]
    original_index = copy.deepcopy(plan["candidate_section_index"])
    original_index_hash = compute_v3_hash(original_index)
    source_contents = _inputs()[2]
    source_sections = {
        candidate_id: {row["section_id"]: row for row in content["sections"]}
        for candidate_id, content in source_contents.items()
    }
    responses = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    ledger_before = build_arbitration_final_ledger(plan, responses)

    for leaf in plan["leaves"]:
        body = leaf["request"]["provider_request"]
        untouched = copy.deepcopy(body)
        projected = project_arbitration_leaf_section_index(body)
        assert body == untouched
        assert projected["hierarchy"]["plan_input_hash"] == original_input_hash
        for candidate_id in plan["candidate_ids"]:
            assert projected["candidate_section_index"][candidate_id] == _expected_index_rows(
                plan, leaf, candidate_id
            )
            assert projected["candidate_sections_by_id"][candidate_id] == body["candidate_sections_by_id"][candidate_id]
            for section in body["candidate_sections_by_id"][candidate_id]:
                assert compute_v3_hash(section) == compute_v3_hash(
                    source_sections[candidate_id][section["section_id"]]
                )

    ledger_after = build_arbitration_final_ledger(plan, responses)
    assert ledger_after == ledger_before
    assert ledger_after["candidate_section_index"] == original_index
    assert plan["input_hash"] == original_input_hash
    assert compute_v3_hash(plan["candidate_section_index"]) == original_index_hash
    assert ledger_after["coverage"]["expected_candidate_section_count"] == 9
    assert ledger_after["coverage"]["observed_candidate_section_count"] == 9
    assert expand_arbitration_final_ledger(pack_arbitration_final_ledger(ledger_after)) == ledger_after


def test_projection_rejects_stale_foreign_and_duplicate_bindings():
    plan = _plan(input_cap=3_500)
    body = copy.deepcopy(plan["leaves"][0]["request"]["provider_request"])
    candidate_id = plan["candidate_ids"][0]

    stale = copy.deepcopy(body)
    stale["candidate_section_bindings"][candidate_id][0]["section_hash"] = "stale"
    with pytest.raises(ArbitrationHierarchyError, match="stale hash"):
        project_arbitration_leaf_section_index(stale)

    foreign = copy.deepcopy(body)
    foreign["candidate_section_bindings"][candidate_id][0]["section_id"] = "foreign-section"
    with pytest.raises(ArbitrationHierarchyError, match="unknown section"):
        project_arbitration_leaf_section_index(foreign)

    duplicate = copy.deepcopy(body)
    duplicate["candidate_section_bindings"][candidate_id].append(
        copy.deepcopy(duplicate["candidate_section_bindings"][candidate_id][0])
    )
    with pytest.raises(ArbitrationHierarchyError, match="repeat section"):
        project_arbitration_leaf_section_index(duplicate)


def _scaled_contents(group_count: int):
    contents = {}
    for candidate_id in _CANDIDATES:
        sections = []
        for group_index in range(group_count):
            is_ab_pair = group_index == 0
            task_id = (
                "relation_comparison:ab_pair_000"
                if is_ab_pair
                else f"topic_synthesis:context:group_{group_index:03d}"
            )
            papers = (
                ["paper:ab-left", "paper:ab-right"]
                if is_ab_pair
                else [f"paper:group_{group_index:03d}"]
            )
            sides = ("left", "right") if is_ab_pair else ("only",)
            claims = [f"Source-bounded claim for group {group_index:03d}, {side}." for side in sides]
            relation_ids = [f"relation:ab_pair_{group_index:03d}"] if is_ab_pair else []
            support = []
            for side, paper_key, claim in zip(sides, papers, claims, strict=True):
                support.append({
                    "claim": claim,
                    "claim_id": f"claim:{group_index:03d}:{side}",
                    "claim_slot_id": f"slot:{group_index:03d}:{side}",
                    "task_id": task_id,
                    "synthesis_claim_id": f"synthesis:{group_index:03d}:{side}",
                    "paper_key": paper_key,
                    "study_id": "",
                    "source_claim_ids": [f"source-claim:{group_index:03d}:{side}"],
                    "evidence_ids": [f"evidence:{group_index:03d}:{side}"],
                    "source_field_ids": [f"field:{group_index:03d}:{side}"],
                    "relation_ids": relation_ids,
                })
            sections.append({
                "section_id": f"{candidate_id}_group_{group_index:03d}",
                "title": f"Complete section for group {group_index:03d}.",
                "goal": (
                    "Compare the recorded source evidence, preserve the qualification and unit of analysis, "
                    "and distinguish the assigned task from adjacent tasks. Assess the claim only within its "
                    "declared paper identities, retain the complete support closure, and state which bounded "
                    "contrast this section makes available for the review. "
                ) * 3,
                "task_ids": [task_id],
                "paper_keys": papers,
                "relation_ids": relation_ids,
                "claims": claims,
                "claim_support": support,
            })
        contents[candidate_id] = {
            "candidate_id": candidate_id,
            "organizing_logic": f"Keep the complete source sections for {candidate_id}.",
            "sections": sections,
        }
    return contents


def _executor_for_profile(profile, input_cap):
    executor = object.__new__(OutlineV3Executor)
    executor.max_source_prompt_tokens = input_cap
    executor.max_provider_calls = 1_000
    executor._provider_call_count = 0
    executor.prompt_registry = PromptRegistry()
    executor._outline_prompt_identity = executor.prompt_registry.identity("outline.node.system.v3")
    executor._outline_policy_identity = executor.prompt_registry.identity("outline.node.policies.v3")
    executor._node_route = lambda *_args, **_kwargs: SimpleNamespace(profile=profile)
    return executor


# Synthetic fixtures exercise the production request builder and profile; they are not corpus acceptance.
@pytest.mark.parametrize("group_count", [4, 64, 100])
def test_synthetic_group_plans_fit_configured_profile_and_preserve_full_closure(group_count):
    candidate_ids = list(_CANDIDATES)
    contents = _scaled_contents(group_count)
    candidate_hashes = {
        candidate_id: compute_v3_hash(contents[candidate_id])
        for candidate_id in candidate_ids
    }
    disposition = {
        "schema_version": "outline_critique_disposition/v1",
        "global_blocker": False,
        "blocked_candidate_ids": [],
        "eligible_candidate_ids": candidate_ids,
        "issues": [],
    }
    source_request = pack_arbitration_candidate_support({
        "candidate_ids": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "candidate_contents": contents,
        "critiques": {"structure_critique": {"status": "passed", "issues": []}},
        "critique_disposition": disposition,
        "review_intent": {"review_question": "Compare complete source-bounded sections."},
        "selection_rule": "Select among all candidates after complete section evaluation.",
    })
    profile = ProviderContextProfile.conservative(
        provider="local",
        model="local",
        endpoint_type="chat_completions",
        model_context_limit=200_000,
        max_output_tokens=16_000,
        reasoning_reserve=4_096,
        safety_margin=2_048,
    )
    # The four-group fixture has an 8,488-token indivisible source unit;
    # 12k permits that unit while still requiring bounded leaf packing.
    input_cap = 12_000 if group_count == 4 else 32_000
    executor = _executor_for_profile(profile, input_cap)
    prepared = executor._prepare_arbitration_hierarchy(source_request)
    assert prepared is not None
    plan = prepared["plan"]
    full_index = plan["candidate_section_index"]
    full_index_hash = compute_v3_hash(full_index)
    assert sum(len(rows) for rows in full_index.values()) == len(candidate_ids) * group_count
    assert plan["leaves"]

    source_by_id = {
        candidate_id: {
            section["section_id"]: section
            for section in contents[candidate_id]["sections"]
        }
        for candidate_id in candidate_ids
    }
    seen_sections = set()
    ab_sections = []
    for leaf in plan["leaves"]:
        request = leaf["request"]
        assert request["hierarchy"]["plan_input_hash"] == plan["input_hash"]
        assert profile.estimate_request(request)["estimated_input_tokens"] <= input_cap
        expanded = expand_arbitration_candidate_support(request)
        for candidate_id in candidate_ids:
            expected_ids = set(leaf["expected_candidate_rows"][candidate_id]["reviewed_section_ids"])
            expected_rows = [
                row for row in full_index[candidate_id]
                if row["section_id"] in expected_ids
            ]
            assert request["candidate_section_index"][candidate_id] == expected_rows
            assert {row["section_id"] for row in expected_rows} == expected_ids
            sections = expanded["candidate_contents"][candidate_id]["sections"]
            assert {row["section_id"] for row in sections} == expected_ids
            for section in sections:
                key = (candidate_id, section["section_id"])
                assert key not in seen_sections
                seen_sections.add(key)
                assert compute_v3_hash(section) == compute_v3_hash(source_by_id[candidate_id][section["section_id"]])
                if section["section_id"].endswith("_group_000"):
                    assert len(section["paper_keys"]) == 2
                    assert len(section["claim_support"]) == 2
                    ab_sections.append(key)

    assert len(seen_sections) == len(candidate_ids) * group_count
    assert len(ab_sections) == len(candidate_ids)
    assert compute_v3_hash(plan["candidate_section_index"]) == full_index_hash
    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    ledger = build_arbitration_final_ledger(plan, results)
    assert ledger["candidate_section_index"] == full_index
    assert ledger["coverage"]["expected_candidate_section_count"] == len(candidate_ids) * group_count
    assert ledger["coverage"]["observed_candidate_section_count"] == len(candidate_ids) * group_count
    assert expand_arbitration_final_ledger(pack_arbitration_final_ledger(ledger)) == ledger

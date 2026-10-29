"""Assertion closure and dual-capacity regressions for physical assignment."""

import json
import math
from dataclasses import replace

import pytest

from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1, CandidateOutputScopeV1, CandidateOutputSupportSlotV1,
)
from outline.candidate_request_packing import (
    CandidatePackingError, estimate_candidate_output, pack_candidate_groups,
    project_semantic_context, expand_candidate_slot_output,
)
from runtime.provider_context import ProviderContextProfile
from tests.test_run_first_scale_integration import _compact_candidate


def _input():
    groups, slots, fields, dependencies = [], [], [], []
    for group_id, papers in (("AB", ("A", "B")), ("BC", ("B", "C")), ("CD", ("C", "D"))):
        task = f"comparison:{group_id}"
        local_slots = []
        for paper in papers:
            primary, qualifier = f"{paper}:finding", f"{paper}:qualifier"
            slot = CandidateOutputSupportSlotV1(
                claim_slot_id=f"slot:{group_id}:{paper}", claim_group_id=group_id,
                task_id=task, topic_id="topic", fragment_id="fragment", provider_result_id="result",
                synthesis_claim_id=f"claim:{group_id}", paper_key=paper, primary_claim_id=primary,
                study_id="", source_claim_ids=(primary, qualifier), evidence_ids=(f"{paper}:evidence",),
                source_field_ids=(primary, qualifier), relation_ids=(f"relation:{group_id}",),
            )
            slots.append(slot)
            local_slots.append(slot.claim_slot_id)
        groups.append(CandidateOutputClaimGroupV1(
            claim_group_id=group_id, task_id=task, topic_id="topic", fragment_id="fragment",
            provider_result_id="result", synthesis_claim_id=f"claim:{group_id}", claim_hash="a" * 64,
            paper_keys=papers, support_slot_ids=tuple(local_slots), relation_ids=(f"relation:{group_id}",),
        ))
    for paper in "ABCD":
        fields.extend([
            {"source_field_id": f"{paper}:finding", "paper_key": paper,
             "source_value": "Zero effect was observed." if paper == "B" else "The effect was negative."},
            {"source_field_id": f"{paper}:qualifier", "paper_key": paper,
             "source_value": "Only in this measured sample; no study number was established."},
        ])
        dependencies.append({"dependency_id": f"dep:{paper}", "paper_key": paper,
                             "primary_claim_id": f"{paper}:finding",
                             "required_source_field_ids": [f"{paper}:finding", f"{paper}:qualifier"]})
    scope = CandidateOutputScopeV1(
        registry_job_id="job", source_inventory_artifact_id="inventory",
        source_inventory_artifact_hash="a" * 64, source_inventory_content_hash="b" * 64,
        task_ids=tuple(group.task_id for group in groups), selected_relation_ids=(),
        claim_groups=tuple(groups), claim_slots=tuple(slots),
    )
    context = {"interpretation_source_tables": {"source_fields": fields, "dependencies": dependencies},
               "topic_routes": [], "global_synthesis": {"provider_output": {"claims": []}}}
    return scope, context


def _plan(scope, context, output_cap=1400):
    profile = ProviderContextProfile.conservative(provider="local", model="test", endpoint_type="chat_completions")

    def build(group_ids):
        selected = scope.for_groups(group_ids)
        return {"candidate_id": "candidate_1", "paper_keys": list(selected.paper_keys),
                "candidate_output_scope": selected.to_dict(),
                "shared_semantic_context": project_semantic_context(context, selected)}

    def measure(request, selected):
        return {"input_tokens": profile.estimate_tokens(request),
                "output_tokens": estimate_candidate_output(selected, request, profile.estimate_tokens)}

    return pack_candidate_groups(scope, build, measure, input_cap=12000, output_cap=output_cap)


def test_comparison_chain_is_packed_by_assertion_and_every_side_is_assigned():
    scope, context = _input()
    bins = _plan(scope, context)
    assert sorted(group for groups, _metrics in bins for group in groups) == ["AB", "BC", "CD"]
    assert len(bins) > 1  # Output capacity requires batching even though input fits.
    for groups, metrics in bins:
        local = scope.for_groups(groups)
        assert all(set(group.paper_keys).issubset(local.paper_keys) for group in local.claim_groups)
        assert metrics["output_tokens"] <= 1400
    assert scope.for_papers(["A"]).claim_groups == ()


def test_projection_preserves_zero_reverse_and_complete_qualifier_text():
    scope, context = _input()
    projected = project_semantic_context(context, scope.for_groups(["AB"]))
    fields = projected["interpretation_source_tables"]["source_fields"]
    assert {row["source_field_id"] for row in fields} == {"A:finding", "A:qualifier", "B:finding", "B:qualifier"}
    assert any(row["source_value"] == "Zero effect was observed." for row in fields)
    assert any(row["source_value"] == "The effect was negative." for row in fields)
    assert all("no study number" in row["source_value"] for row in fields if row["source_field_id"].endswith("qualifier"))
    context["interpretation_source_tables"]["source_fields"].pop(1)
    with pytest.raises(CandidatePackingError, match="source text"):
        project_semantic_context(context, scope.for_groups(["AB"]))


def test_request_assignment_is_invariant_to_input_order():
    scope, context = _input()
    reversed_scope = replace(scope, claim_groups=tuple(reversed(scope.claim_groups)),
                             claim_slots=tuple(reversed(scope.claim_slots)))
    assert [groups for groups, _ in _plan(scope, context)] == [groups for groups, _ in _plan(reversed_scope, context)]


def test_an_indivisible_oversized_comparison_reports_its_exact_group():
    scope, context = _input()
    with pytest.raises(CandidatePackingError, match=r"candidate group AB.*output=.*\/1"):
        _plan(scope, context, output_cap=1)


def _compact_payload():
    return {"candidate_id": "candidate_1", "candidate_wire_version": "candidate-slot-output/v1",
            "sections": [{"section_id": "comparison", "title": "A/B comparison",
                          "goal": "Compare the findings within their sample boundary.",
                          "relation_ids": ["relation:AB"],
                          "claims": [{"text": "A was negative and B had a zero effect, only in the measured samples.",
                                      "claim_group_indices": [0]}]}]}


def test_compact_output_restores_both_actual_source_closures_without_inventing_studies():
    scope, _context = _input()
    scope = scope.for_groups(["AB"])
    expanded = expand_candidate_slot_output(_compact_payload(), scope)
    section = expanded["sections"][0]
    assert section["paper_keys"] == ["A", "B"]
    assert section["task_ids"] == ["comparison:AB"]
    assert len(section["claim_support"]) == 2
    assert "zero effect" in section["claims"][0]
    for row in section["claim_support"]:
        slot = next(slot for slot in scope.claim_slots if slot.claim_slot_id == row["claim_slot_id"])
        assert row["source_claim_ids"] == list(slot.source_claim_ids)
        assert row["source_field_ids"] == list(slot.source_field_ids)
        assert row["evidence_ids"] == list(slot.evidence_ids)
        assert "study_id" not in row
    scope.validate(expanded)


@pytest.mark.parametrize("indices", [[99], [-1], [True], [0, 0], []])
def test_compact_output_rejects_unknown_repeated_or_missing_group_binding(indices):
    scope, _context = _input()
    payload = _compact_payload()
    payload["sections"][0]["claims"][0]["claim_group_indices"] = indices
    with pytest.raises(CandidatePackingError):
        expand_candidate_slot_output(payload, scope.for_groups(["AB"]))


def test_output_estimate_keeps_long_source_values_on_input_only():
    scope, context = _input()
    local = scope.for_groups(["AB"])
    profile = ProviderContextProfile.conservative(provider="local", model="test", endpoint_type="chat_completions")
    request = {"candidate_id": "candidate_1", "output_contract": {
        "preferred_candidate_wire_version": "candidate-slot-output/v1",
        "claim_prose_reserve_tokens": 256, "section_prose_reserve_tokens": 64,
    }, "shared_semantic_context": project_semantic_context(context, local)}
    before = estimate_candidate_output(local, request, profile.estimate_tokens)
    input_before = profile.estimate_tokens(request)
    request["shared_semantic_context"]["interpretation_source_tables"]["source_fields"][0]["source_value"] *= 500
    assert estimate_candidate_output(local, request, profile.estimate_tokens) == before
    assert profile.estimate_tokens(request) > input_before


def test_compact_local_candidate_fits_dual_caps_and_retains_required_ab_qualifiers():
    base_scope, _context = _input()
    scope = base_scope.for_groups(["AB"])
    comparison_contract = {
        "task_id": "comparison:AB",
        "relation_id": "relation:AB",
        "comparison_question": "Compare the source-reported findings within their sample boundaries.",
        "relation_id_is_not_a_conclusion": True,
        "output_semantics": "model_authored_comparison_using_both_source_sides_and_their_qualifiers",
    }
    group = replace(
        scope.claim_groups[0],
        claim_seed=True,
        claim_kind="model_authored_relation_comparison_slot",
        task_contract=tuple(sorted(comparison_contract.items())),
    )
    scope = replace(scope, claim_groups=(group,))

    source_claims = []
    claim_text_by_id = {
        "A:finding": (
            "The effect was negative. Study A (n=24) and Study B (n=31) are "
            "separately labeled; no common pooled effect is reported."
        ),
        "A:qualifier": (
            "Study A (n=24) and Study B (n=31) are separately labeled; "
            "no common pooled effect is reported."
        ),
        "B:finding": "Zero effect was observed. Only in this measured sample; no study number was established.",
        "B:qualifier": "Only in this measured sample; no study number was established.",
    }
    for claim_id, text in claim_text_by_id.items():
        source_claims.append({"claim_id": claim_id, "text": text})
    source_fields = [
        {"source_field_id": "A:finding", "paper_key": "A", "canonical_field": "findings",
         "source_value": "The effect was negative."},
        {"source_field_id": "A:qualifier", "paper_key": "A", "canonical_field": "sample_or_context",
         "source_value": "Study A (n=24) and Study B (n=31) are separately labeled; no common pooled effect is reported."},
        {"source_field_id": "B:finding", "paper_key": "B", "canonical_field": "findings",
         "source_value": "Zero effect was observed."},
        {"source_field_id": "B:qualifier", "paper_key": "B", "canonical_field": "limitations",
         "source_value": "Only in this measured sample; no study number was established."},
    ]
    scope_wire = {
        "claim_group_ids": [group.claim_group_id],
        "required_claim_group_indices": [0],
        "claim_slots": [
            {"claim_group_index": 0, **slot.to_dict()}
            for slot in scope.claim_slots
        ],
        "organization_tasks": [{
            "claim_group_index": 0,
            "claim_group_id": group.claim_group_id,
            "task_id": group.task_id,
            "synthesis_claim_id": group.synthesis_claim_id,
            "claim_kind": group.claim_kind,
            "claim_seed": True,
            "paper_keys": list(group.paper_keys),
            "support_slot_ids": list(group.support_slot_ids),
            "task_contract": dict(group.task_contract),
        }],
    }
    request = {
        "candidate_id": "candidate_1",
        "organizing_logic": "reported findings",
        "paper_keys": list(scope.paper_keys),
        "evidence": [
            {"paper_key": paper, "title": f"Paper {paper}"}
            for paper in scope.paper_keys
        ],
        "candidate_output_scope": scope_wire,
        "hierarchy": {
            "shard_id": "candidate_group_ab",
            "claim_group_ids": [group.claim_group_id],
            "group_assignment": {group.claim_group_id: "candidate_group_ab"},
        },
        "output_contract": {
            "preferred_candidate_wire_version": "candidate-slot-output/v1",
            "claim_prose_reserve_tokens": 256,
            "section_prose_reserve_tokens": 64,
        },
        "shared_semantic_context": {
            "interpretation_source_tables": {
                "source_claims": source_claims,
                "source_fields": source_fields,
                "dependencies": [],
            },
        },
    }
    profile = ProviderContextProfile.conservative(
        provider="local", model="rfs-outline-local", endpoint_type="chat_completions",
    )
    estimated_output = estimate_candidate_output(scope, request, profile.estimate_tokens)
    input_tokens = int(profile.estimate_request(request)["estimated_input_tokens"])
    requested_output = min(4096, max(1024, estimated_output))

    compact = _compact_candidate(
        "candidate_1_provider_generation:local:candidate_group_ab", request,
    )
    serialized = json.dumps(compact, ensure_ascii=False).encode("utf-8")
    fixture_output_tokens = math.ceil(len(serialized) / 4)

    assert input_tokens <= 32_000
    assert estimated_output <= 4_096
    assert fixture_output_tokens <= requested_output
    claim_text = compact["sections"][0]["claims"][0]["text"]
    assert "The effect was negative." in claim_text
    assert "Zero effect was observed." in claim_text
    assert "Study A (n=24) and Study B (n=31) are separately labeled" in claim_text
    assert "no common pooled effect is reported" in claim_text
    assert "Only in this measured sample; no study number was established." in claim_text

    expanded = expand_candidate_slot_output(compact, scope)
    support = expanded["sections"][0]["claim_support"]
    assert {row["paper_key"] for row in support} == {"A", "B"}
    assert all(
        row["source_field_ids"] == list(next(
            slot for slot in scope.claim_slots if slot.claim_slot_id == row["claim_slot_id"]
        ).source_field_ids)
        for row in support
    )
    scope.validate(expanded)

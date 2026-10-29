"""Source-closed evidence-critique packing regressions."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from runtime.provider_context import ProviderContextProfile
from services.prompt_registry import PromptRegistry


def _section(section_id: str, papers: tuple[str, str], relation_id: str) -> dict[str, Any]:
    claim = f"{papers[0]} and {papers[1]} retain their recorded findings and boundaries."
    slots = []
    for paper in papers:
        slots.append({
            "claim": claim,
            "claim_slot_id": f"slot:{section_id}:{paper}",
            "task_id": f"comparison:{section_id}",
            "paper_key": paper,
            "primary_claim_id": f"{paper}:primary",
            "source_claim_ids": [f"{paper}:primary", *[f"{paper}:qualifier:{i}" for i in range(5)]],
            "evidence_ids": [f"{paper}:evidence:{i}" for i in range(6)],
            "source_field_ids": [f"{paper}:field:{i}" for i in range(8)],
            "relation_ids": [relation_id],
            "study_id": "",
        })
    return {
        "section_id": section_id,
        "title": f"Comparison {section_id}",
        "goal": "Compare the reported findings within their source boundaries.",
        "paper_keys": list(papers),
        "relation_ids": [relation_id],
        "claims": [claim],
        "claim_support": slots,
    }


def _oversized_evidence_critique_request() -> dict[str, Any]:
    sections = [
        _section("required-ab", ("paper-a", "paper-b"), "relation-ab"),
        _section("comparison-cd", ("paper-c", "paper-d"), "relation-cd"),
        _section("comparison-ef", ("paper-e", "paper-f"), "relation-ef"),
    ]
    candidate = {
        "candidate_id": "candidate_1",
        "organizing_logic": "source-grounded comparison",
        "sections": sections,
        "planned_claims": [claim for section in sections for claim in section["claims"]],
    }
    # These unconsumed relation rows are valid for the corpus but irrelevant to
    # this candidate's five authored comparison sides. Paper overlap alone
    # would keep every row in the old shard builder.
    relation_evidence = [
        {
            "relation_id": f"unused-relation-{index:02d}",
            "relation_type": "qualifies",
            "paper_keys": ["paper-a", "paper-b", "paper-c", "paper-d", "paper-e", "paper-f"],
            "evidence_fields": {
                "source_context": f"unused relation source context {index}: " + ("source-bound text " * 300),
                "limitation": f"unused qualifier {index}: " + ("recorded limit " * 90),
            },
        }
        for index in range(23)
    ]
    relation_evidence.extend({
        "relation_id": relation,
        "relation_type": "reported_magnitude_contrast",
        "paper_keys": list(papers),
        "evidence_fields": {"source_context": "Selected relation context for both source sides."},
    } for relation, papers in (
        ("relation-ab", ["paper-a", "paper-b"]),
        ("relation-cd", ["paper-c", "paper-d"]),
        ("relation-ef", ["paper-e", "paper-f"]),
    ))
    return {
        "node_id": "evidence_critique",
        "candidate_contents": {"candidate_1": candidate},
        "candidate_hashes": {"candidate_1": "parent-candidate-hash"},
        "paper_key_aliases": {"P001": "paper-a", "P002": "paper-b"},
        "paper_reuse_policy": "Repeated use is allowed when every claim has distinct evidence function.",
        "candidate_claims": {"candidate_1": list(candidate["planned_claims"])},
        "section_evidence": {"candidate_1": sections},
        "paper_keys": [f"paper-{letter}" for letter in "abcdefghij"],
        "source_summary_hashes": [f"summary-hash-{index:02d}" for index in range(10)],
        "relation_evidence": relation_evidence,
        "contradictions": [],
        "boundaries": [],
        "gaps": [],
    }


def _critique_executor(calls: list[tuple[str, dict[str, Any]]]) -> tuple[OutlineV3Executor, ProviderContextProfile]:
    executor = object.__new__(OutlineV3Executor)
    profile = ProviderContextProfile.conservative(
        provider="local", model="rfs-outline-local", endpoint_type="chat_completions",
    )
    registry = PromptRegistry()
    executor.prompt_registry = registry
    executor._outline_prompt_identity = registry.identity("outline.node.system.v3")
    executor._outline_policy_identity = registry.identity("outline.node.policies.v3")
    executor.max_source_prompt_tokens = 32_000
    executor.technical_shard_target_tokens = 0
    executor.max_provider_calls = 100
    executor._provider_call_count = 0
    executor._node_route = lambda *_args, **_kwargs: SimpleNamespace(profile=profile)
    executor._provider_call_id = lambda node_id: node_id

    def provider_call(node_id: str, request: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((node_id, dict(request)))
        return {"passed": True, "blocking_diagnostics": [], "recommendations": [], "score": 1.0}

    executor._provider_call = provider_call
    return executor, profile


def test_evidence_critique_local_requests_fit_cap_and_keep_required_ab_section_closed():
    request = _oversized_evidence_critique_request()
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, profile = _critique_executor(calls)

    result = executor._run_hierarchical_critique(
        node_id="evidence_critique", request=request, dependency_hashes={"candidate": "input-hash"},
    )

    assert calls
    assert all(
        profile.estimate_request(payload).get("estimated_input_tokens", 0) <= 32_000
        for _node_id, payload in calls
    )
    reviewed_section_ids = {
        section_id
        for row in result["candidate_shard_results"].values()
        for section_id in row["reviewed_section_ids"]
    }
    assert reviewed_section_ids == {"required-ab", "comparison-cd", "comparison-ef"}
    assert len(reviewed_section_ids) == sum(
        len(row["reviewed_section_ids"])
        for row in result["candidate_shard_results"].values()
    )
    assert all(
        row["parent_candidate_hash"] == "parent-candidate-hash"
        for row in result["candidate_shard_results"].values()
    )

    seen_relations = set()
    saw_complete_ab = False
    for _node_id, payload in calls:
        candidate = payload["candidate_contents"]["candidate_1"]
        candidate_sections = {section["section_id"]: section for section in candidate["sections"]}
        original_sections = {
            section["section_id"]: section
            for section in request["section_evidence"]["candidate_1"]
        }
        section_evidence = payload["section_evidence"]["candidate_1"]
        assert {row["section_id"] for row in section_evidence} == set(candidate_sections)
        for section in section_evidence:
            assert section.get("claim_array_ref") == {
                "candidate_id": "candidate_1",
                "section_id": section["section_id"],
                "field": "candidate_contents.sections.claims",
            }
            target = candidate_sections[section["section_id"]]
            expanded_support = []
            for support in section["claim_support"]:
                index = support.get("claim_index")
                assert type(index) is int and 0 <= index < len(target["claims"])
                assert support.get("section_id", section["section_id"]) == section["section_id"]
                expanded = {
                    key: value for key, value in support.items()
                    if key not in {"claim_index", "section_id"}
                }
                expanded["claim"] = target["claims"][index]
                expanded_support.append(expanded)
            assert expanded_support == original_sections[section["section_id"]]["claim_support"]
        relations = {
            str(row.get("relation_id") or "") for row in payload.get("relation_evidence") or ()
            if isinstance(row, Mapping)
        }
        seen_relations.update(relations)
        if "required-ab" in candidate_sections:
            ab = candidate_sections["required-ab"]
            ab_evidence = next(row for row in section_evidence if row["section_id"] == "required-ab")
            assert ab["paper_keys"] == ["paper-a", "paper-b"]
            assert ab["relation_ids"] == ["relation-ab"]
            assert len(ab_evidence["claim_support"]) == 2
            support_papers = {row["paper_key"] for row in ab_evidence["claim_support"]}
            assert support_papers == {"paper-a", "paper-b"}
            assert all(len(row["source_claim_ids"]) == 6 for row in ab_evidence["claim_support"])
            assert all(len(row["evidence_ids"]) == 6 for row in ab_evidence["claim_support"])
            assert all(len(row["source_field_ids"]) == 8 for row in ab_evidence["claim_support"])
            saw_complete_ab = True
    assert saw_complete_ab
    assert seen_relations == {"relation-ab", "relation-cd", "relation-ef"}


def test_hierarchical_critique_retains_typed_issues_from_every_shard():
    request = _oversized_evidence_critique_request()
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _profile = _critique_executor(calls)
    emitted: list[dict[str, Any]] = []

    def review(node_id: str, payload: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        issue = {
            "issue_id": f"issue:{node_id}", "scope": "section",
            "target_ids": list(payload["hierarchy"]["section_ids"]),
            "severity": "blocking", "evidence_refs": ["relation-ab"],
            "resolution_status": "unresolved", "parent_candidate_hash": "parent-candidate-hash",
            "message": "Keep this recorded unresolved source issue.",
        }
        emitted.append(issue)
        return {"passed": False, "blocking_diagnostics": [], "issues": [issue],
                "recommendations": [], "score": 0.0}

    executor._provider_call = review
    result = executor._run_hierarchical_critique(
        node_id="evidence_critique", request=request, dependency_hashes={"candidate": "input-hash"},
    )

    assert emitted and result["issues"] == emitted
    assert result["passed"] is False


def test_critique_section_claim_index_rejects_dangling_and_foreign_support():
    section = _section("required-ab", ("paper-a", "paper-b"), "relation-ab")
    candidate = {"candidate_id": "candidate_1", "sections": [
        {key: value for key, value in section.items() if key != "claim_support"}
    ]}

    dangling = [dict(section)]
    dangling[0]["claim_support"] = [dict(section["claim_support"][0], claim="a foreign claim")]
    with pytest.raises(OutlineV3ExecutionError, match="support claim"):
        OutlineV3Executor._compact_critique_section_evidence(
            "candidate_1", candidate, dangling,
        )

    foreign = [dict(section)]
    foreign[0]["claim_support"] = [dict(section["claim_support"][0], section_id="comparison-cd")]
    with pytest.raises(OutlineV3ExecutionError, match="foreign section"):
        OutlineV3Executor._compact_critique_section_evidence(
            "candidate_1", candidate, foreign,
        )


def test_coverage_critique_keeps_unselected_relation_context():
    request = _oversized_evidence_critique_request()
    request["relation_evidence"] = [
        {
            "relation_id": "relation-ab",
            "relation_type": "reported_magnitude_contrast",
            "paper_keys": ["paper-a", "paper-b"],
            "evidence_fields": {"source_context": "The selected AB relation."},
        },
        {
            "relation_id": "unselected-overlap",
            "relation_type": "qualifies",
            # It overlaps this candidate's paper set, but no candidate section
            # consumed it. Coverage critique still gets the corpus contrast.
            "paper_keys": ["paper-a", "paper-z"],
            "evidence_fields": {"source_context": "A broader corpus boundary."},
        },
    ]
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, profile = _critique_executor(calls)

    executor._run_hierarchical_critique(
        node_id="coverage_critique", request=request,
        dependency_hashes={"candidate": "input-hash"},
    )

    assert calls
    assert all(
        profile.estimate_request(payload).get("estimated_input_tokens", 0) <= 32_000
        for _node_id, payload in calls
    )
    assert any(
        relation.get("relation_id") == "unselected-overlap"
        for _node_id, payload in calls
        for relation in payload.get("relation_evidence") or ()
    )


def _structure_request(section_count: int, words_per_claim: int) -> dict[str, Any]:
    sections = [{
        "section_id": f"source-{index}",
        "title": f"Source boundary {index}",
        "goal": "Preserve the complete finding and its source conditions.",
        "paper_keys": [f"paper-{index}"],
        "paper_roles": {f"paper-{index}": "Discuss its distinct source limitation."},
        "relation_ids": [],
        "claims": [f"Finding {index}: " + ("bounded source result " * words_per_claim)],
    } for index in range(section_count)]
    return {
        "node_id": "structure_critique",
        "candidate_contents": {"candidate_1": {
            "candidate_id": "candidate_1", "organizing_logic": "source boundaries",
            "sections": sections,
        }},
        "candidate_hashes": {"candidate_1": "original-candidate-hash"},
        "review_intent": {"review_question": "retain source context " * 1400},
        "checks": ["section_progression", "goal_claim_alignment"],
    }


def test_structure_critique_packs_complete_attached_requests_before_post():
    request = _structure_request(9, 390)
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, profile = _critique_executor(calls)
    compact = executor._compact_candidate_for_critique(
        "candidate_1", request["candidate_contents"]["candidate_1"],
    )
    heuristic = executor._split_critique_candidate(compact, target_tokens=28_000)
    assert len(heuristic) == 1
    attached = executor._attach_prompt_authority("structure_critique:local:candidate_1:shard:1", {
        **request, "candidate_contents": {"candidate_1": heuristic[0]},
    })
    assert profile.estimate_request(attached)["estimated_input_tokens"] > 32_000

    result = executor._run_hierarchical_critique(
        node_id="structure_critique", request=request,
        dependency_hashes={"candidate": "original-candidate-hash"},
    )
    assert len(calls) > 1
    returned = []
    for _node_id, payload in calls:
        assert profile.estimate_request(payload)["estimated_input_tokens"] <= 32_000
        assert payload["output_contract"]["must_include"] == [
            "node_id", "passed", "issues", "blocking_diagnostics",
        ]
        assert executor._attach_critique_output_contract(payload, "structure_critique") == payload
        returned.extend(payload["candidate_contents"]["candidate_1"]["sections"])
        assert payload["candidate_hashes"] == {"candidate_1": "original-candidate-hash"}
    assert returned == compact["sections"]
    reviewed = [sid for row in result["candidate_shard_results"].values()
                for sid in row["reviewed_section_ids"]]
    assert sorted(reviewed) == sorted(section["section_id"] for section in compact["sections"])


def test_indivisible_structure_section_over_cap_blocks_before_any_post():
    calls: list[tuple[str, dict[str, Any]]] = []
    executor, _profile = _critique_executor(calls)
    with pytest.raises(OutlineV3ExecutionError, match="complete critique request"):
        executor._run_hierarchical_critique(
            node_id="structure_critique", request=_structure_request(1, 12_000),
            dependency_hashes={"candidate": "original-candidate-hash"},
        )
    assert calls == []

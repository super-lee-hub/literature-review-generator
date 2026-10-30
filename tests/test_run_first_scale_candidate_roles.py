"""Non-comparison paper roles describe typed source function and task dimension."""

from __future__ import annotations

from typing import Any

from tests.test_run_first_scale_integration import _compact_candidate


def _one_source_task(
    *,
    shard: str,
    task_id: str,
    dimension: str,
    family: str,
    text: str,
    claim_suffix: str = "shared",
):
    paper = "10.5555/role-fixture"
    group_id = f"group:{shard}"
    primary_id = f"claim:{paper}:{family}:{claim_suffix}"
    slot = {
        "claim_group_index": 0,
        "claim_group_id": group_id,
        "claim_slot_id": f"slot:{shard}:{paper}",
        "task_id": task_id,
        "paper_key": paper,
        "primary_claim_id": primary_id,
        "study_id": "",
        "source_claim_ids": [primary_id],
        "evidence_ids": [f"evidence:{shard}"],
        "source_field_ids": [],
        "relation_ids": [],
    }
    return _compact_candidate(
        "candidate_1_provider_generation:local:candidate_group_" + shard,
        {
            "candidate_id": "candidate_1",
            "paper_keys": [paper],
            "evidence": [{"paper_key": paper, "title": "Role-fixture paper"}],
            "hierarchy": {
                "shard_id": shard,
                "claim_group_ids": [group_id],
                "group_assignment": {group_id: shard},
            },
            "candidate_output_scope": {
                "claim_group_ids": [group_id],
                "required_claim_group_indices": [0],
                "claim_slots": [slot],
                "organization_tasks": [],
            },
            "shared_semantic_context": {
                "interpretation_source_tables": {
                    "source_claims": [{
                        "claim_id": primary_id,
                        "claim_type": "author_interpretation",
                        "text": text,
                    }],
                    "source_fields": [],
                },
                "topic_routes": [{
                    "topic_id": f"topic:{dimension}:{shard}",
                    "logical_node_id": task_id,
                    "dimensions": [dimension],
                }],
            },
        },
    )


def _paper_role(candidate: dict[str, Any]) -> str:
    return candidate["sections"][0]["paper_roles"]["10.5555/role-fixture"]


def test_same_typed_limitation_in_context_and_theory_keeps_both_functions():
    # The exact same source passage can be retained in two task sections when
    # it serves two distinct discussion functions. The role states those uses,
    # not a fabricated difference in source facts.
    exact_passage = (
        "The authors call for actual consumer choices and note that other platforms may differ."
    )
    context = _one_source_task(
        shard="context",
        task_id="topic_synthesis:context:source-role",
        dimension="context",
        family="moderators_boundaries",
        text=exact_passage,
    )
    theory = _one_source_task(
        shard="theory",
        task_id="topic_synthesis:theory:source-role",
        dimension="theory",
        family="limitations",
        text=exact_passage,
    )

    context_role = _paper_role(context)
    theory_role = _paper_role(theory)
    assert context_role != theory_role
    assert "context-focused" in context_role
    assert "theory-focused" in theory_role
    assert "source-reported" in context_role and "source-reported" in theory_role
    assert len(context["sections"][0]["claims"]) == 1
    assert len(theory["sections"][0]["claims"]) == 1
    assert "actual consumer choices" in context["sections"][0]["claims"][0]["text"]
    assert "actual consumer choices" in theory["sections"][0]["claims"][0]["text"]
    assert context["sections"][0]["claims"][0]["claim_group_indices"] == [0]
    assert theory["sections"][0]["claims"][0]["claim_group_indices"] == [0]


def test_identical_same_dimension_source_function_keeps_same_role_and_fails_strict_reuse_oracle():
    context_one = _one_source_task(
        shard="context-one",
        task_id="topic_synthesis:context:one",
        dimension="context",
        family="limitations",
        text="This source reports a bounded limitation in the observed context.",
        claim_suffix="source_left",
    )
    context_two = _one_source_task(
        shard="context-two",
        task_id="topic_synthesis:context:two",
        dimension="context",
        family="limitations",
        text="This source reports a separate boundary for the observed context.",
        claim_suffix="source_right",
    )
    roles = [_paper_role(context_one), _paper_role(context_two)]

    # The production quality gate requires a distinct paper role for every
    # repeated section assignment; identical source functions remain a block.
    assert roles[0] == roles[1]
    assert len({role.casefold() for role in roles}) < len(roles)


def test_relation_comparison_counterpart_roles_are_unchanged():
    papers = ("paper:A", "paper:B")
    group_id = "group:comparison"
    task_id = "cross_group_comparison"
    slots = [
        {
            "claim_group_index": 0,
            "claim_group_id": group_id,
            "claim_slot_id": f"slot:{paper}",
            "task_id": task_id,
            "paper_key": paper,
            "primary_claim_id": f"claim:{paper}:findings:source",
            "source_claim_ids": [f"claim:{paper}:findings:source"],
            "evidence_ids": [f"evidence:{paper}"],
            "source_field_ids": [],
            "relation_ids": ["relation:AB"],
        }
        for paper in papers
    ]
    result = _compact_candidate(
        "candidate_1_provider_generation:local:comparison",
        {
            "candidate_id": "candidate_1",
            "paper_keys": list(papers),
            "evidence": [{"paper_key": paper, "title": f"Source {paper}"} for paper in papers],
            "relation_ids": ["relation:AB"],
            "hierarchy": {
                "shard_id": "comparison",
                "claim_group_ids": [group_id],
                "group_assignment": {group_id: "comparison"},
            },
            "candidate_output_scope": {
                "claim_group_ids": [group_id],
                "required_claim_group_indices": [0],
                "claim_slots": slots,
                "organization_tasks": [{
                    "claim_group_id": group_id,
                    "paper_keys": list(papers),
                    "claim_seed": True,
                    "claim_kind": "model_authored_relation_comparison_slot",
                    "task_contract": {"relation_id_is_not_a_conclusion": True},
                }],
            },
            "shared_semantic_context": {
                "interpretation_source_tables": {
                    "source_claims": [{
                        "claim_id": f"claim:{paper}:findings:source",
                        "claim_type": "empirical_finding",
                        "text": f"Source {paper} records a bounded finding.",
                    } for paper in papers],
                    "source_fields": [],
                },
                "topic_routes": [],
            },
        },
    )
    roles = result["sections"][0]["paper_roles"]
    assert roles["paper:A"] == "Compare its source-reported finding with Source paper:B within their recorded source boundaries."
    assert roles["paper:B"] == "Compare its source-reported finding with Source paper:A within their recorded source boundaries."

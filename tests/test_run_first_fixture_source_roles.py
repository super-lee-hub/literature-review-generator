"""The model edge explains actual source reuse rather than inventing shard roles."""

from tests.test_run_first_scale_integration import _compact_candidate


def _request(counterpart):
    papers = ("A", counterpart)
    group = "comparison"
    return {
        "candidate_id": "candidate_1",
        "paper_keys": list(papers),
        "evidence": [{"paper_key": paper, "title": f"Source {paper}"} for paper in papers],
        "hierarchy": {"shard_id": "part", "claim_group_ids": [group]},
        "candidate_output_scope": {
            "claim_group_ids": [group],
            "required_claim_group_indices": [0],
            "claim_slots": [{
                "claim_group_index": 0, "claim_slot_id": f"slot:{paper}",
                "paper_key": paper, "primary_claim_id": f"finding:{paper}",
                "source_claim_ids": [f"finding:{paper}"], "source_field_ids": [],
                "relation_ids": [],
            } for paper in papers],
            "organization_tasks": [{
                "claim_group_id": group, "paper_keys": list(papers),
                "claim_seed": True, "claim_kind": "model_authored_relation_comparison_slot",
                "task_contract": {"relation_id_is_not_a_conclusion": True},
            }],
        },
        "shared_semantic_context": {"interpretation_source_tables": {
            "source_claims": [{
                "claim_id": f"finding:{paper}",
                "text": "A negative finding was recorded only in the observed sample.",
            } for paper in papers], "source_fields": [],
        }},
    }


def test_reused_source_role_describes_the_actual_comparison_counterpart():
    ab = _compact_candidate("candidate_1:local:ab", _request("B"))
    ac = _compact_candidate("candidate_1:local:ac", _request("C"))
    ab_roles = ab["sections"][0]["paper_roles"]
    ac_roles = ac["sections"][0]["paper_roles"]
    assert set(ab_roles) == {"A", "B"}
    assert set(ac_roles) == {"A", "C"}
    assert "Source B" in ab_roles["A"]
    assert "Source C" in ac_roles["A"]
    assert ab_roles["A"] != ac_roles["A"]
    assert "observed sample" in ab["sections"][0]["claims"][0]["text"]
    assert "part" not in ab_roles["A"]

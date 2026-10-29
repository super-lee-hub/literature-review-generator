"""The small runtime fixture consumes required groups without dropping sides."""

from copy import deepcopy
from dataclasses import replace

import pytest

from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
)
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from tests.test_current_runtime_full_e2e import _candidate_source_bound_response


def _request():
    fields, source_claims, dependencies, routes, groups, slots = [], [], [], [], [], []
    source_text = {
        "paper-a": ("The treatment increased the outcome.", "Only in the measured sample."),
        "paper-b": ("No effect was observed.", "The estimate excludes other contexts."),
        "paper-c": ("The treatment decreased the outcome.", "Only during the observed follow-up."),
    }
    for paper, (finding, boundary) in source_text.items():
        primary, qualifier = f"claim:{paper}:finding", f"claim:{paper}:boundary"
        source_claims.extend([
            {"claim_id": primary, "paper_key": paper, "text": finding,
             "claim_type": "empirical_finding", "evidence_ids": [f"evidence:{paper}:finding"]},
            {"claim_id": qualifier, "paper_key": paper, "text": boundary,
             "claim_type": "author_interpretation", "evidence_ids": [f"evidence:{paper}:boundary"]},
        ])
        fields.extend([
            {"source_field_id": f"field:{paper}:finding", "paper_key": paper, "source_value": finding},
            {"source_field_id": f"field:{paper}:boundary", "paper_key": paper, "source_value": boundary},
        ])
        dependencies.append({
            "dependency_id": f"dependency:{paper}", "paper_key": paper, "primary_claim_id": primary,
            "primary_evidence_ids": [f"evidence:{paper}:finding"],
            "required_source_claim_ids": [qualifier], "required_evidence_ids": [f"evidence:{paper}:boundary"],
            "required_source_field_ids": [f"field:{paper}:boundary"],
        })
        routes.append({"fragments": [{"provider_results": [{"provider_output": {"claims": [{
            "claim_id": f"synthesis:topic:{paper}", "paper_key": paper,
            "primary_claim_id": primary, "text": finding + " " + boundary,
            "source_claim_ids": [primary, qualifier],
            "evidence_ids": [f"evidence:{paper}:finding", f"evidence:{paper}:boundary"],
            "primary_evidence_ids": [f"evidence:{paper}:finding"],
            "source_field_ids": [f"field:{paper}:finding", f"field:{paper}:boundary"],
        }]}}]}]})

    for group_id, task, papers in (
        ("topic-a", "topic:paper-a", ("paper-a",)),
        ("topic-b", "topic:paper-b", ("paper-b",)),
        ("topic-c", "topic:paper-c", ("paper-c",)),
        ("bridge-bc", "cross_group_comparison", ("paper-b", "paper-c")),
        ("comparison-ab", "relation_comparison:relation-ab", ("paper-a", "paper-b")),
    ):
        relation = () if group_id.startswith("topic-") else (f"relation-{group_id[-2:]}",)
        comparison = group_id == "comparison-ab"
        synthesis_id = f"synthesis:topic:{papers[0]}" if len(papers) == 1 else f"synthesis:{task}:{group_id}"
        group_slots = []
        for paper in papers:
            slot_id = f"slot:{group_id}:{paper}"
            group_slots.append(slot_id)
            slots.append(CandidateOutputSupportSlotV1(
                claim_slot_id=slot_id, claim_group_id=group_id, task_id=task,
                topic_id="", fragment_id="", provider_result_id="result", synthesis_claim_id=synthesis_id,
                paper_key=paper, primary_claim_id=f"claim:{paper}:finding", study_id="",
                source_claim_ids=tuple(sorted((f"claim:{paper}:finding", f"claim:{paper}:boundary"))),
                evidence_ids=tuple(sorted((f"evidence:{paper}:finding", f"evidence:{paper}:boundary"))),
                source_field_ids=tuple(sorted((f"field:{paper}:finding", f"field:{paper}:boundary"))),
                relation_ids=relation,
            ))
        groups.append(CandidateOutputClaimGroupV1(
            claim_group_id=group_id, task_id=task, topic_id="", fragment_id="", provider_result_id="result",
            synthesis_claim_id=synthesis_id, claim_hash="a" * 64, paper_keys=papers,
            support_slot_ids=tuple(group_slots), relation_ids=relation,
            claim_kind="model_authored_relation_comparison_slot" if comparison else "provider_authored_source_closed_claim",
            claim_seed=comparison,
            task_contract=(("comparison_question", "Compare both recorded findings and boundaries."),
                           ("relation_type", "qualifies")) if comparison else (),
        ))
    scope = CandidateOutputScopeV1(
        registry_job_id="small-fixture", source_inventory_artifact_id="inventory",
        source_inventory_artifact_hash="a" * 64, source_inventory_content_hash="b" * 64,
        task_ids=tuple(sorted(group.task_id for group in groups)),
        selected_relation_ids=("relation-ab", "relation-bc"), claim_groups=tuple(groups), claim_slots=tuple(slots),
    )
    executor = OutlineV3Executor.__new__(OutlineV3Executor)
    executor._candidate_output_scope = scope
    executor._candidate_corpus_witness_scope_hash = scope.content_hash
    executor._candidate_corpus_witness_group_ids = {"topic-a", "bridge-bc"}
    executor.review_intent_input = {"required_comparisons": [{
        "paper_keys": ["paper-a", "paper-b"], "relation_type": "qualifies",
        "question": "Compare both recorded findings and boundaries.",
    }]}
    tables = {"source_fields": fields, "source_claims": source_claims, "dependencies": dependencies}
    executor._candidate_interpretation_tables = tables
    request = {
        "candidate_id": "candidate_1", "organizing_logic": "source evidence",
        "evidence_projection": {"projection": "registry_complete_evidence_ref_v1"},
        "evidence": [{"paper_key": paper, "title": paper} for paper in source_text],
        "shared_semantic_context": {"interpretation_source_tables": tables, "topic_routes": routes},
        "output_contract": {"planned_claims_require_source_support": True},
        "candidate_output_scope": executor._candidate_output_scope_wire(list(source_text)),
    }
    return executor, scope, request, source_text


def _validate(executor, payload):
    executor._validate_candidate_payload(
        "candidate_1", payload,
        allowed_paper_keys=["paper-a", "paper-b", "paper-c"],
        allowed_relation_ids=["relation-ab", "relation-bc"],
    )


def test_small_fixture_completes_bridge_and_explicit_comparison_with_every_member():
    executor, scope, request, source_text = _request()
    trace = []
    result = _candidate_source_bound_response("candidate_1_provider_generation", request, trace)
    payload = result["content"]
    _validate(executor, payload)
    assert len(payload["sections"]) == 5  # Three paper claims plus two complete group assertions.
    assert len({row["claim_slot_id"] for section in payload["sections"] for row in section["claim_support"]}) == 7
    for group_id, papers in (("bridge-bc", {"paper-b", "paper-c"}), ("comparison-ab", {"paper-a", "paper-b"})):
        group = next(group for group in scope.claim_groups if group.claim_group_id == group_id)
        section = next(section for section in payload["sections"] if section["task_ids"] == [group.task_id])
        assert set(section["paper_keys"]) == papers
        assert {row["claim_slot_id"] for row in section["claim_support"]} == set(group.support_slot_ids)
        assert section["relation_ids"] == list(group.relation_ids)
        for row in section["claim_support"]:
            paper = row["paper_key"]
            assert all(text.rstrip(".") in row["claim"] for text in source_text[paper])
            assert row.get("study_id", "") == ""
            assert row["qualifier_source_claim_ids"] == [f"claim:{paper}:boundary"]
            assert row["qualifier_evidence_ids"] == [f"evidence:{paper}:boundary"]
            assert row["qualifier_source_field_ids"] == [f"field:{paper}:boundary"]
    assert len(trace[0]["sections"]) == 7


@pytest.mark.parametrize("mutation", ["missing_group", "missing_member", "lost_qualifier", "foreign_owner"])
def test_small_fixture_required_group_parser_stays_fail_closed(mutation):
    executor, _scope, request, _source_text = _request()
    payload = deepcopy(_candidate_source_bound_response("candidate_1_provider_generation", request, None)["content"])
    section = next(section for section in payload["sections"] if section["task_ids"] == ["cross_group_comparison"])
    if mutation == "missing_group":
        payload["sections"].remove(section)
    elif mutation == "missing_member":
        missing = section["claim_support"].pop()
        section["paper_keys"].remove(missing["paper_key"])
    elif mutation == "lost_qualifier":
        section["claim_support"][0]["source_claim_ids"].pop(0)
    else:
        section["claim_support"][0]["paper_key"] = "paper-a"
    with pytest.raises(OutlineV3ExecutionError, match="finite output contract"):
        _validate(executor, payload)


def test_small_fixture_rejects_foreign_owned_field_before_emitting_required_support():
    _executor, _scope, request, _source_text = _request()
    request["shared_semantic_context"]["interpretation_source_tables"]["source_fields"][0]["paper_key"] = "paper-c"
    with pytest.raises(AssertionError, match="not owned by candidate paper"):
        _candidate_source_bound_response("candidate_1_provider_generation", request, None)


def test_small_fixture_accepts_exact_fieldless_operationalization_in_required_groups():
    executor, scope, request, _source_text = _request()
    # This is the current96 P001 operationalization's captured canonical
    # closure: a real paper-level source assertion with no study or qualifier.
    primary = "claim:papera_unknown_author:operationalizations:f1b2c6c4a8c79c86"
    evidence = "evidence:papera_unknown_author:operationalizations:9c612e151b27eb77"
    text = "A reproducible controlled sample"
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    tables["source_claims"] = [row for row in tables["source_claims"] if row["paper_key"] != "paper-a"]
    tables["source_claims"].append({
        "claim_id": primary, "paper_key": "paper-a", "text": text,
        "claim_type": "author_interpretation", "evidence_ids": [evidence],
        "source_locator": "stage1:ai_summary.core_analysis.operationalizations",
    })
    tables["source_fields"] = [row for row in tables["source_fields"] if row["paper_key"] != "paper-a"]
    tables["dependencies"] = [row for row in tables["dependencies"] if row["paper_key"] != "paper-a"]
    claim = request["shared_semantic_context"]["topic_routes"][0]["fragments"][0]["provider_results"][0]["provider_output"]["claims"][0]
    claim.update({
        "primary_claim_id": primary, "text": text, "source_claim_ids": [primary],
        "evidence_ids": [evidence], "primary_evidence_ids": [evidence], "source_field_ids": [],
    })
    scope = replace(scope, claim_slots=tuple(
        replace(slot, primary_claim_id=primary, source_claim_ids=(primary,), evidence_ids=(evidence,), source_field_ids=())
        if slot.paper_key == "paper-a" else slot for slot in scope.claim_slots
    ))
    executor._candidate_output_scope = scope
    executor._candidate_corpus_witness_scope_hash = scope.content_hash
    request["candidate_output_scope"] = executor._candidate_output_scope_wire(["paper-a", "paper-b", "paper-c"])
    trace = []
    payload = _candidate_source_bound_response("candidate_1_provider_generation", request, trace)["content"]
    _validate(executor, payload)
    a_rows = [row for section in payload["sections"] for row in section["claim_support"] if row["paper_key"] == "paper-a"]
    assert len(a_rows) == 2  # Its paper claim and the unchanged required A/B group.
    for row in a_rows:
        assert row["source_claim_ids"] == [primary]
        assert row["evidence_ids"] == [evidence]
        assert row["source_field_ids"] == []
        assert row["qualifier_source_claim_ids"] == []
        assert row["qualifier_evidence_ids"] == []
        assert row["qualifier_source_field_ids"] == []
        assert row["dependency_ids"] == []
        assert row.get("study_id", "") == ""
        assert text in row["claim"]
    b_trace = next(row for row in trace[0]["sections"] if row["paper_key"] == "paper-b")
    assert b_trace["qualifier_source_field_ids"] == ["field:paper-b:boundary"]


def test_small_fixture_repeated_sources_have_distinct_source_task_roles():
    _executor, _scope, request, _source_text = _request()
    payload = _candidate_source_bound_response("candidate_1_provider_generation", request, None)["content"]
    roles_by_paper = {paper: [] for paper in ("paper-a", "paper-b", "paper-c")}
    for section in payload["sections"]:
        for paper in section["paper_keys"]:
            role = str((section.get("paper_roles") or {}).get(paper) or "").strip()
            assert role, (paper, section["section_id"])
            assert "claim-slot" not in role and "candidate_" not in role
            roles_by_paper[paper].append(role.casefold())
        if len(section["paper_keys"]) > 1:
            for paper in section["paper_keys"]:
                counterpart = next(key for key in section["paper_keys"] if key != paper)
                assert counterpart in section["paper_roles"][paper]
    assert all(len(values) == len(set(values)) for values in roles_by_paper.values())

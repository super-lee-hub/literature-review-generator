"""Whole-section, all-candidate arbitration hierarchy contract tests."""

from __future__ import annotations

import json
import math

import pytest

from outline.candidate_arbitration_hierarchy import (
    ArbitrationHierarchyError,
    build_arbitration_final_ledger,
    plan_arbitration_hierarchy,
    validate_arbitration_leaf,
)
from outline.v3_models import compute_v3_hash

_CANDIDATES = tuple(f"candidate_{index}" for index in range(1, 6))


def test_shared_broad_topic_does_not_fuse_distinct_source_sections_into_one_atomic_unit():
    contents = {}
    for candidate in _CANDIDATES:
        sections = []
        for family in ('a', 'b'):
            section = _section(candidate, ('shared_topic',))
            section['section_id'] = f'{candidate}_source_family_{family}'
            section['paper_keys'] = [f'source-family-{family}']
            for row in section['claim_support']:
                row.update(paper_key=f'source-family-{family}', claim_slot_id=f'slot:family:{family}',
                           source_claim_ids=[f'claim-source:family:{family}'],
                           evidence_ids=[f'evidence:family:{family}'], source_field_ids=[f'field:family:{family}'])
            sections.append(section)
        contents[candidate] = {'candidate_id': candidate, 'sections': sections}
    # This control has no critic issues, so only the complete source alignment
    # determines units; all five alternatives remain in every assessment matrix.
    _ids, _hashes, _old, disposition = _inputs(omit_task_t2_from_candidate_5=False)
    disposition['issues'] = []
    plan = _plan(contents=contents, disposition=disposition)
    assert len(plan['units']) == 2
    for unit in plan['units']:
        assert all(len(rows) == 1 for rows in unit['sections_by_candidate'].values())
    observed = {(candidate, row['section_id']) for unit in plan['units']
                for candidate, rows in unit['sections_by_candidate'].items() for row in rows}
    expected = {(candidate, section['section_id']) for candidate, content in contents.items()
                for section in content['sections']}
    assert observed == expected
    assert sum(len(rows) for rows in plan['candidate_section_index'].values()) == 10
    for leaf in plan['leaves']:
        validate_arbitration_leaf(plan, leaf['leaf_id'], _response_for(plan, leaf))


def _section(candidate_id: str, task_ids: tuple[str, ...]) -> dict:
    section_id = f"{candidate_id}_{'_'.join(task_ids)}"
    paper_key = f"paper:{candidate_id}"
    claims = [f"{candidate_id} source-bounded claim for {task_id}." for task_id in task_ids]
    support = [
        {
            "claim": claim,
            "claim_id": f"claim:{candidate_id}:{task_id}",
            "claim_slot_id": f"slot:{candidate_id}:{task_id}",
            "task_id": task_id,
            "synthesis_claim_id": f"synthesis:{candidate_id}:{task_id}",
            "paper_key": paper_key,
            "study_id": "",
            "source_claim_ids": [f"claim-source:{candidate_id}:{task_id}"],
            "evidence_ids": [f"evidence:{candidate_id}:{task_id}"],
            "source_field_ids": [f"field:{candidate_id}:{task_id}"],
            "relation_ids": [f"relation:{task_id}"],
        }
        for task_id, claim in zip(task_ids, claims, strict=True)
    ]
    return {
        "section_id": section_id,
        "title": f"Title {candidate_id} {task_ids}",
        "goal": f"Compare {' and '.join(task_ids)} within recorded source limits.",
        "task_ids": list(task_ids),
        "paper_keys": [paper_key],
        "relation_ids": [f"relation:{task_id}" for task_id in task_ids],
        "claims": claims,
        "claim_support": support,
    }


def _inputs(*, omit_task_t2_from_candidate_5: bool = True):
    ids = list(_CANDIDATES)
    hashes = {candidate_id: compute_v3_hash({"candidate": candidate_id}) for candidate_id in ids}
    contents = {}
    for candidate_id in ids:
        sections = [_section(candidate_id, ("task_ab",))]
        if not (omit_task_t2_from_candidate_5 and candidate_id == "candidate_5"):
            sections.append(_section(candidate_id, ("task_bc",)))
        contents[candidate_id] = {
            "candidate_id": candidate_id,
            "organizing_logic": f"Organize {candidate_id} around source tasks.",
            "sections": sections,
            "planned_claims": [claim for row in sections for claim in row["claims"]],
        }
    issue = {
        "issue_id": "critic-1-section-issue",
        "scope": "section",
        "target_ids": ["candidate_1_task_ab"],
        "severity": "non_blocking",
        "evidence_refs": ["evidence:candidate_1"],
        "resolution_status": "unresolved",
        "parent_candidate_hash": hashes["candidate_1"],
        "candidate_id": "candidate_1",
        "source_critic": "structure_critique",
        "source": "typed",
        "message": "Section order could be clearer.",
    }
    claim_issue = {
        **issue,
        "issue_id": "critic-1-claim-issue",
        "scope": "claim",
        "target_ids": ["claim:candidate_1:task_ab"],
        "message": "The claim needs a clearer source boundary.",
    }
    disposition = {
        "schema_version": "outline_critique_disposition/v1",
        "global_blocker": False,
        "blocked_candidate_ids": [],
        "eligible_candidate_ids": ids,
        "issues": [issue, claim_issue],
    }
    return ids, hashes, contents, disposition


def _request_builder(body):
    # Stand-in for the actual callback: include both authority and the leaf
    # schema before the planner measures serialized bytes.
    return {
        "system_prompt": "Compare only the bound, source-closed candidate sections.",
        "output_contract": "Return all five candidate assessments and exact section hashes.",
        "provider_request": body,
    }


def _measure(request):
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "estimated_input_tokens": math.ceil(len(raw) / 4),
        "requested_output_tokens": 512,
        "within_budget": True,
    }


def _plan(*, ids=None, input_cap=100_000, max_calls=24, contents=None, disposition=None):
    base_ids, hashes, base_contents, base_disposition = _inputs()
    if ids is not None:
        base_ids = ids
    if contents is not None:
        base_contents = contents
    if disposition is not None:
        base_disposition = disposition
    return plan_arbitration_hierarchy(
        base_ids,
        hashes,
        base_contents,
        base_disposition,
        critique_hashes={"structure_critique": "critic-hash-structure"},
        provenance_hashes={"candidate_output_scope": "scope-hash", "review_intent": "intent-hash"},
        request_builder=_request_builder,
        estimate_input_tokens=_measure,
        input_cap=input_cap,
        max_calls=max_calls,
        output_cap=1024,
    )


def _response_for(plan, leaf):
    assessments = []
    for candidate_id in plan["candidate_ids"]:
        expected = leaf["expected_candidate_rows"][candidate_id]
        present = expected["presence"] == "present"
        assessments.append({
            "candidate_id": candidate_id,
            "candidate_hash": expected["candidate_hash"],
            "presence": expected["presence"],
            "reviewed_section_ids": list(expected["reviewed_section_ids"]),
            "section_hashes": dict(expected["section_hashes"]),
            "coverage_assessment": (
                "The assigned section covers the source task." if present
                else "No section for this task is assigned to this candidate in this leaf."
            ),
            "evidence_assessment": (
                "The source closure supports the stated bounded assertion." if present
                else "No source section is assigned for evidence assessment in this leaf."
            ),
            "structure_assessment": (
                "The section has a distinct position in the candidate structure." if present
                else "No section is assigned for structural assessment in this leaf."
            ),
            "advantages": ["Preserves the recorded source boundary."] if present else [],
            "limitations": ["Does not support inference beyond the cited source."] if present else [],
            "evidence_refs": list(expected["allowed_evidence_refs"][:1]) if present else [],
            "critic_issue_ids": list(expected["critic_issue_ids"]) if present else [],
        })
    return {
        "leaf_id": leaf["leaf_id"],
        "unit_ids": list(leaf["unit_ids"]),
        "candidate_assessments": assessments,
    }


def test_single_candidate_hierarchy_keeps_every_source_section_and_critic_target():
    _ids, hashes, contents, disposition = _inputs()
    candidate_id = "candidate_1"
    disposition = {**disposition, "eligible_candidate_ids": [candidate_id]}
    plan = plan_arbitration_hierarchy(
        [candidate_id],
        {candidate_id: hashes[candidate_id]},
        {candidate_id: contents[candidate_id]},
        disposition,
        critique_hashes={"structure_critique": "critic-hash-structure"},
        provenance_hashes={"candidate_output_scope": "scope-hash"},
        request_builder=lambda body: {
            "output_contract": "Assess only the sole candidate_id in this request.",
            "provider_request": body,
        },
        estimate_input_tokens=_measure,
        input_cap=100_000,
        output_cap=1024,
        max_calls=24,
    )
    assert plan["candidate_ids"] == [candidate_id]
    assert sum(len(rows) for rows in plan["candidate_section_index"].values()) == 2
    results = {}
    for leaf in plan["leaves"]:
        assert leaf["request"]["provider_request"]["candidate_ids"] == [candidate_id]
        results[leaf["leaf_id"]] = _response_for(plan, leaf)
        validate_arbitration_leaf(plan, leaf["leaf_id"], results[leaf["leaf_id"]])
    ledger = build_arbitration_final_ledger(plan, results)
    assert ledger["candidate_ids"] == [candidate_id]
    assert ledger["coverage"]["expected_candidate_section_count"] == 2
    assert ledger["coverage"]["observed_candidate_section_count"] == 2


def test_plan_keeps_all_five_candidates_exact_section_closures_and_explicit_absence():
    plan = _plan()
    assert len(plan["candidate_ids"]) == 5
    assert len(plan["candidate_section_index"]) == 5
    assert sum(len(rows) for rows in plan["candidate_section_index"].values()) == 9
    assert plan["call_count"] == len(plan["leaves"]) + 1
    task_bc = next(unit for unit in plan["units"] if unit["task_ids"] == ["task_bc"])
    assert task_bc["sections_by_candidate"]["candidate_5"] == []

    for leaf in plan["leaves"]:
        body = leaf["request"]["provider_request"]
        assert body["candidate_ids"] == plan["candidate_ids"]
        assert body["candidate_hashes"] == plan["candidate_hashes"]
        assert set(body["candidate_sections_by_id"]) == set(plan["candidate_ids"])
        for candidate_id, sections in body["candidate_sections_by_id"].items():
            expected = leaf["expected_candidate_rows"][candidate_id]
            assert [section["section_id"] for section in sections] == expected["reviewed_section_ids"]
            for section in sections:
                assert section["claim_support"]
                assert section["claim_support"][0]["source_claim_ids"]
                assert section["claim_support"][0]["evidence_ids"]
                assert section["claim_support"][0]["source_field_ids"]


def test_plan_is_permutation_invariant_and_does_not_transitively_merge_task_sets():
    plan = _plan()
    ids, _hashes, contents, disposition = _inputs()
    reversed_disposition = dict(disposition)
    reversed_disposition["eligible_candidate_ids"] = list(reversed(ids))
    permuted = _plan(ids=list(reversed(ids)), contents=dict(reversed(list(contents.items()))), disposition=reversed_disposition)
    assert plan["plan_hash"] == permuted["plan_hash"]

    # Exact task-set signatures keep a mixed AB+BC section separate from AB
    # and BC; overlapping task IDs do not form a transitive mega-unit.
    contents["candidate_1"]["sections"].append(_section("candidate_1", ("task_ab", "task_bc")))
    contents["candidate_1"]["planned_claims"].extend(contents["candidate_1"]["sections"][-1]["claims"])
    mixed = _plan(contents=contents)
    assert {tuple(unit["task_ids"]) for unit in mixed["units"]} == {
        ("task_ab",), ("task_bc",), ("task_ab", "task_bc")
    }


def test_leaf_validation_and_final_ledger_cover_every_candidate_section_once():
    plan = _plan()
    results = {
        leaf["leaf_id"]: _response_for(plan, leaf)
        for leaf in plan["leaves"]
    }
    validated = {
        leaf_id: validate_arbitration_leaf(plan, leaf_id, response)
        for leaf_id, response in results.items()
    }
    ledger = build_arbitration_final_ledger(plan, results)
    assert ledger["coverage"]["expected_candidate_section_count"] == 9
    assert ledger["coverage"]["observed_candidate_section_count"] == 9
    assert len(ledger["candidate_ids"]) == 5
    assert ledger["candidate_hashes"] == plan["candidate_hashes"]
    assert ledger["critique_hashes"] == plan["critique_hashes"]
    assert set(validated) == {leaf["leaf_id"] for leaf in plan["leaves"]}


def test_leaf_rejects_missing_duplicate_foreign_and_stale_candidate_or_section_bindings():
    plan = _plan()
    leaf = plan["leaves"][0]
    response = _response_for(plan, leaf)

    missing = dict(response)
    missing["candidate_assessments"] = response["candidate_assessments"][:-1]
    with pytest.raises(ArbitrationHierarchyError, match="every candidate exactly once"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], missing)

    duplicate = dict(response)
    duplicate["candidate_assessments"] = [*response["candidate_assessments"], response["candidate_assessments"][0]]
    with pytest.raises(ArbitrationHierarchyError, match="duplicate candidate assessment"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], duplicate)

    foreign = json.loads(json.dumps(response))
    foreign["candidate_assessments"][0]["reviewed_section_ids"] = ["foreign-section"]
    with pytest.raises(ArbitrationHierarchyError, match="missing or foreign section coverage"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], foreign)

    stale = json.loads(json.dumps(response))
    stale["candidate_assessments"][0]["candidate_hash"] = "stale-hash"
    with pytest.raises(ArbitrationHierarchyError, match="stale candidate hash"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], stale)


def test_leaf_rejects_foreign_evidence_and_missing_typed_critic_issue_binding():
    plan = _plan()
    leaf = next(
        item for item in plan["leaves"]
        if "critic-1-section-issue" in item["expected_candidate_rows"]["candidate_1"]["critic_issue_ids"]
    )
    response = _response_for(plan, leaf)
    target = next(row for row in response["candidate_assessments"] if row["candidate_id"] == "candidate_1")
    target["evidence_refs"] = ["evidence:foreign"]
    with pytest.raises(ArbitrationHierarchyError, match="outside its source closure"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], response)

    response = _response_for(plan, leaf)
    target = next(row for row in response["candidate_assessments"] if row["candidate_id"] == "candidate_1")
    target["critic_issue_ids"] = []
    with pytest.raises(ArbitrationHierarchyError, match="typed critic issues"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], response)

    response = _response_for(plan, leaf)
    target = next(row for row in response["candidate_assessments"] if row["candidate_id"] == "candidate_1")
    target["evidence_refs"] = ["slot:candidate_1:task_ab"]
    with pytest.raises(ArbitrationHierarchyError, match="outside its source closure"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], response)


def test_planner_rejects_typed_issue_with_foreign_section_target():
    _ids, _hashes, _contents, disposition = _inputs()
    disposition = json.loads(json.dumps(disposition))
    disposition["issues"][0]["target_ids"] = ["foreign-section"]
    with pytest.raises(ArbitrationHierarchyError, match="targets unknown candidate content"):
        _plan(disposition=disposition)


@pytest.mark.parametrize("targets", [[], ["candidate_1", "candidate_2"]])
def test_planner_rejects_candidate_issue_without_exact_parent_target(targets):
    _ids, _hashes, _contents, disposition = _inputs()
    disposition = json.loads(json.dumps(disposition))
    issue = disposition["issues"][0]
    issue["scope"] = "candidate"
    issue["target_ids"] = targets
    with pytest.raises(ArbitrationHierarchyError, match="must target exactly its candidate_id"):
        _plan(disposition=disposition)


def test_claim_issue_is_bound_only_to_its_exact_section_leaf():
    plan = _plan()
    issue_leaf_ids = {
        leaf["leaf_id"]
        for leaf in plan["leaves"]
        if "critic-1-claim-issue" in leaf["expected_candidate_rows"]["candidate_1"]["critic_issue_ids"]
    }
    section_leaf_ids = {
        leaf["leaf_id"]
        for leaf in plan["leaves"]
        if "candidate_1_task_ab" in leaf["expected_candidate_rows"]["candidate_1"]["reviewed_section_ids"]
    }
    assert issue_leaf_ids == section_leaf_ids


def test_planner_stops_before_dispatch_for_oversized_single_section_or_call_envelope():
    with pytest.raises(ArbitrationHierarchyError, match="indivisible section leaf.*exceeds cap"):
        _plan(input_cap=1)
    with pytest.raises(ArbitrationHierarchyError, match="exceeding max_calls"):
        _plan(max_calls=1)


def test_plan_replay_identity_does_not_depend_on_remaining_call_admission_snapshot():
    more_remaining = _plan(max_calls=100)
    fewer_remaining = _plan(max_calls=90)
    assert more_remaining["plan_hash"] == fewer_remaining["plan_hash"]
    assert [row["request_hash"] for row in more_remaining["leaves"]] == [
        row["request_hash"] for row in fewer_remaining["leaves"]
    ]
    assert more_remaining["call_count"] == fewer_remaining["call_count"]


def test_final_ledger_rejects_missing_or_duplicate_leaf_results():
    plan = _plan()
    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    with pytest.raises(ArbitrationHierarchyError, match="every planned leaf exactly once"):
        build_arbitration_final_ledger(plan, {})
    if len(results) > 1:
        first = next(iter(results))
        reduced = dict(results)
        reduced.pop(first)
        with pytest.raises(ArbitrationHierarchyError, match="every planned leaf exactly once"):
            build_arbitration_final_ledger(plan, reduced)

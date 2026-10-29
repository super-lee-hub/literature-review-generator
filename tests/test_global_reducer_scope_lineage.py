"""Retained reducer ancestors resolve source lineage without adding output slots."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from outline.candidate_fragment_bank import (
    CandidateFragmentBankError,
    resolve_candidate_fragment_claims,
)
from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    build_candidate_output_scope_v1,
)
from tests.test_run_first_candidate_fragment_bank import _sealed_scope
from tests.test_run_first_candidate_fragment_source_labels import _fixture_case


@pytest.fixture
def reducer_case(tmp_path) -> dict[str, Any]:
    source = _fixture_case(tmp_path)
    slots = source["scope"].claim_slots
    canonical_ids = sorted({value for slot in slots for value in slot.source_claim_ids})
    evidence_ids = sorted({value for slot in slots for value in slot.evidence_ids})
    field_ids = sorted({
        value for dependency in source["dependency_by_paper"].values()
        for value in dependency.required_source_field_ids
    })
    papers = sorted(source["paper_keys"])
    leaf = {
        "claim_id": "synthesis:cross_group_comparison:retained-leaf",
        "paper_keys": papers,
        "text": "The two findings differ within their recorded conditions.",
        "source_claim_ids": canonical_ids,
        "evidence_ids": evidence_ids,
        "source_field_ids": field_ids,
    }
    lower = {
        "claim_id": "synthesis:global_synthesis:reduce-1-1",
        "paper_keys": papers,
        "text": "The comparison retains both observed source boundaries.",
        "source_claim_ids": [leaf["claim_id"]],
    }
    upper = {
        "claim_id": "synthesis:global_synthesis:reduce-2-1",
        "paper_keys": papers,
        "text": "The retained lower comparison supports this reduction.",
        "source_claim_ids": [lower["claim_id"]],
    }
    final = {
        "claim_id": "synthesis:global_synthesis:final",
        "paper_keys": papers,
        "text": "Both findings and their conditions support the final comparison.",
        "source_claim_ids": [upper["claim_id"]],
    }
    return {
        **source,
        "leaf": leaf,
        "lower": lower,
        "upper": upper,
        "final": final,
        "lineage": [leaf, lower, upper],
    }


def _build(case: dict[str, Any], *, lineage: Any = None, final: Any = None):
    return build_candidate_output_scope_v1(
        case["inventory"],
        [],
        bridge_claims=[{
            "task_id": "global_synthesis",
            "result_id": "global-result:final",
            "provider_output": {"synthesis_claims": [final or case["final"]]},
        }],
        lineage_claims=case["lineage"] if lineage is None else lineage,
    )


def _resolve(case: dict[str, Any], *, lineage: Any):
    return resolve_candidate_fragment_claims(
        _build(case), case["inventory"], [], [{
            "task_id": "global_synthesis",
            "result_id": "global-result:final",
            "provider_output": {"synthesis_claims": [case["final"]]},
        }],
        lineage_claims=lineage,
    )


def _payload(scope) -> dict[str, Any]:
    text = "Both findings remain conditional on their recorded boundaries."
    return {
        "sections": [{
            "section_id": "section:comparison",
            "task_ids": ["global_synthesis"],
            "title": "Conditional comparison",
            "goal": "Preserve both source results and their qualifiers.",
            "paper_keys": sorted({slot.paper_key for slot in scope.claim_slots}),
            "relation_ids": [],
            "claims": [text],
            "claim_support": [{
                "claim_slot_id": slot.claim_slot_id,
                "task_id": slot.task_id,
                "claim": text,
                "paper_key": slot.paper_key,
                "primary_claim_id": slot.primary_claim_id,
                "study_id": slot.study_id,
                "source_claim_ids": list(slot.source_claim_ids),
                "evidence_ids": list(slot.evidence_ids),
                "source_field_ids": list(slot.source_field_ids),
            } for slot in scope.claim_slots],
        }],
    }


def test_canonical_inventory_alone_cannot_replace_missing_reducer_ancestry(reducer_case):
    case = reducer_case
    inventory_ids = {
        claim.claim_id for paper in case["inventory"].papers for claim in paper.claims
    }
    assert set(case["leaf"]["source_claim_ids"]).issubset(inventory_ids)
    with pytest.raises(CandidateOutputScopeError, match="absent from both task lineage and verified inventory"):
        _build(case, lineage=[])


def test_existing_public_fragment_resolver_keeps_two_argument_graph_call_compatible(tmp_path):
    inventory, routes, bridges, _bundles, scope = _sealed_scope(tmp_path)
    original_scope = deepcopy(scope.to_dict())
    original_routes = deepcopy(routes)
    original_bridges = deepcopy(bridges)

    resolved = resolve_candidate_fragment_claims(scope, inventory, routes, bridges)

    expected_groups = {group.claim_group_id for group in scope.claim_groups if not group.claim_seed}
    assert set(resolved) == expected_groups
    assert {row["claim_id"] for row in resolved.values()} == {
        group.synthesis_claim_id for group in scope.claim_groups if not group.claim_seed
    }
    assert scope.to_dict() == original_scope
    assert routes == original_routes
    assert bridges == original_bridges


def test_public_fragment_resolver_uses_retained_lineage_without_extra_output_groups(reducer_case):
    case = reducer_case
    before = deepcopy(case["lineage"])
    scope = _build(case)
    resolved = _resolve(case, lineage=case["lineage"])
    extra = {**case["leaf"], "claim_id": "synthesis:global_synthesis:unused-resolver-ancestor"}

    assert _resolve(case, lineage=[*case["lineage"], extra]) == resolved
    assert set(resolved) == {scope.claim_groups[0].claim_group_id}
    canonical, = resolved.values()
    assert canonical["claim_id"] == case["final"]["claim_id"]
    for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
        assert canonical[field] == case["leaf"][field]
    assert len(scope.claim_groups) == 1
    assert len(scope.claim_slots) == 2
    assert case["lineage"] == before


@pytest.mark.parametrize("fault", ["missing", "conflict", "cycle", "foreign_source", "foreign_paper"])
def test_public_fragment_resolver_rejects_unclosed_or_conflicting_retained_lineage(reducer_case, fault):
    case = reducer_case
    lineage = deepcopy(case["lineage"])
    if fault == "missing":
        lineage.pop(1)
        message = "absent from both task lineage and verified inventory"
    elif fault == "conflict":
        lineage.append({**case["upper"], "source_claim_ids": [case["leaf"]["claim_id"]]})
        message = "conflicting task outputs"
    elif fault == "cycle":
        lineage[1]["source_claim_ids"] = [case["upper"]["claim_id"]]
        message = "contains a cycle"
    elif fault == "foreign_source":
        lineage[0]["source_claim_ids"].append("claim:foreign-original")
        message = "absent from both task lineage and verified inventory"
    else:
        lineage[0]["paper_keys"] = [case["paper_keys"][0]]
        message = "outside its synthesis claim paper scope"

    with pytest.raises(CandidateFragmentBankError, match=message):
        _resolve(case, lineage=lineage)


@pytest.mark.parametrize("field", ["source_claim_ids", "evidence_ids", "source_field_ids"])
def test_public_fragment_resolver_preserves_qualifier_rejection(reducer_case, field):
    case = reducer_case
    dependency = case["dependency_by_paper"][case["paper_keys"][0]]
    required_ids = set(getattr(dependency, f"required_{field}"))
    assert required_ids
    lineage = deepcopy(case["lineage"])
    lineage[0][field] = [value for value in lineage[0][field] if value not in required_ids]

    with pytest.raises(CandidateFragmentBankError, match="canonical payload hash differs from scope"):
        _resolve(case, lineage=lineage)


@pytest.mark.parametrize("lineage", [None, "not-an-array", {}, ["not-a-claim"]])
def test_public_fragment_resolver_requires_lineage_claim_array(reducer_case, lineage):
    with pytest.raises(CandidateFragmentBankError, match="lineage|synthesis claim"):
        _resolve(reducer_case, lineage=lineage)


def test_retained_multilevel_reducer_lineage_resolves_to_exact_original_output(reducer_case):
    case = reducer_case
    lineage_before = deepcopy(case["lineage"])
    scope = _build(case)
    flattened = {
        **case["final"],
        "source_claim_ids": case["leaf"]["source_claim_ids"],
        "evidence_ids": case["leaf"]["evidence_ids"],
        "source_field_ids": case["leaf"]["source_field_ids"],
    }
    flat_scope = _build(case, lineage=[], final=flattened)

    assert scope.to_dict() == flat_scope.to_dict()
    assert case["lineage"] == lineage_before
    assert scope.task_ids == ("global_synthesis",)
    assert len(scope.claim_groups) == 1
    assert scope.claim_groups[0].synthesis_claim_id == case["final"]["claim_id"]
    assert len(scope.claim_slots) == 2
    assert {slot.paper_key for slot in scope.claim_slots} == set(case["paper_keys"])
    scope.validate(_payload(scope))


def test_lineage_only_claims_never_become_visible_groups_or_required_slots(reducer_case):
    case = reducer_case
    scope = _build(case)
    extra = {**case["leaf"], "claim_id": "synthesis:global_synthesis:unused-ancestor"}
    with_extra = _build(case, lineage=[*case["lineage"], extra])

    assert with_extra.to_dict() == scope.to_dict()
    assert with_extra.max_claims == 1
    assert with_extra.max_support_rows == 2
    assert with_extra.for_papers([case["paper_keys"][0]]).claim_slots == ()


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_reducer_ancestor_still_rejects(reducer_case, missing_index):
    with pytest.raises(CandidateOutputScopeError, match="absent from both task lineage and verified inventory"):
        _build(reducer_case, lineage=[
            row for index, row in enumerate(reducer_case["lineage"]) if index != missing_index
        ])


def test_conflicting_reducer_identity_cannot_replace_retained_lineage(reducer_case):
    case = reducer_case
    conflict = {**case["upper"], "source_claim_ids": [case["leaf"]["claim_id"]]}
    with pytest.raises(CandidateOutputScopeError, match="conflicting task outputs"):
        _build(case, lineage=[*case["lineage"], conflict])


def test_lineage_conflict_with_visible_final_claim_also_rejects(reducer_case):
    case = reducer_case
    conflict = {**case["final"], "source_claim_ids": case["leaf"]["source_claim_ids"]}
    with pytest.raises(CandidateOutputScopeError, match="conflicting task outputs"):
        _build(case, lineage=[*case["lineage"], conflict])


def test_reducer_lineage_cycle_cannot_be_hidden_by_available_canonical_ids(reducer_case):
    case = reducer_case
    cyclic_lower = {**case["lower"], "source_claim_ids": [case["upper"]["claim_id"]]}
    with pytest.raises(CandidateOutputScopeError, match="contains a cycle"):
        _build(case, lineage=[case["leaf"], cyclic_lower, case["upper"]])


def test_lineage_original_source_must_exist_in_verified_inventory(reducer_case):
    case = reducer_case
    foreign_leaf = {
        **case["leaf"],
        "source_claim_ids": [*case["leaf"]["source_claim_ids"], "claim:foreign-original-source"],
    }
    with pytest.raises(CandidateOutputScopeError, match="absent from both task lineage and verified inventory"):
        _build(case, lineage=[foreign_leaf, case["lower"], case["upper"]])


def test_lineage_canonical_source_cannot_cross_its_declared_paper_scope(reducer_case):
    case = reducer_case
    wrong_owner = {**case["leaf"], "paper_keys": [case["paper_keys"][0]]}
    with pytest.raises(CandidateOutputScopeError, match="outside its synthesis claim paper scope"):
        _build(case, lineage=[wrong_owner, case["lower"], case["upper"]])


@pytest.mark.parametrize("field", ["source_claim_ids", "evidence_ids", "source_field_ids"])
def test_reducer_ancestor_cannot_omit_qualifier_closure(reducer_case, field):
    case = reducer_case
    dependency = case["dependency_by_paper"][case["paper_keys"][0]]
    required_ids = set(getattr(dependency, f"required_{field}"))
    assert required_ids
    partial_leaf = {**case["leaf"], field: [
        value for value in case["leaf"][field] if value not in required_ids
    ]}
    with pytest.raises(CandidateOutputScopeError, match=field):
        _build(case, lineage=[partial_leaf, case["lower"], case["upper"]])


def test_reducer_output_cannot_drop_one_side_of_the_comparison(reducer_case):
    scope = _build(reducer_case)
    payload = _payload(scope)
    section = payload["sections"][0]
    section["claim_support"] = section["claim_support"][:1]
    section["paper_keys"] = [section["claim_support"][0]["paper_key"]]

    with pytest.raises(CandidateOutputScopeError, match="missing one or more paper support closures"):
        scope.validate(payload)


@pytest.mark.parametrize("lineage", ["not-an-array", {}, ["not-a-claim"], [{"claim_id": "foreign:claim"}]])
def test_lineage_input_requires_synthesis_claim_mappings(reducer_case, lineage):
    with pytest.raises(CandidateOutputScopeError, match="lineage_claims|synthesis claim"):
        _build(reducer_case, lineage=lineage)

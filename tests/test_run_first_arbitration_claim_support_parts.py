from __future__ import annotations

import copy
from functools import lru_cache
import json
import math

import pytest

from outline.candidate_arbitration_hierarchy import (
    ArbitrationHierarchyError,
    build_arbitration_final_ledger,
    plan_arbitration_hierarchy,
    validate_arbitration_leaf,
)
from outline.candidate_arbitration_ledger_packing import (
    ArbitrationLedgerPackingError,
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from outline.v3_models import compute_v3_hash

_CANDIDATES = [f"candidate_{index}" for index in range(1, 6)]
_SECTION_IDS = {candidate: f"section:{candidate}:1" for candidate in _CANDIDATES}


def _current104_shape(*, claim_count: int = 52):
    contents = {}
    for candidate_id in _CANDIDATES:
        claims = []
        support = []
        for slot in range(claim_count):
            group = slot // 2
            claim_id = f"claim-target:{slot:03d}"
            claim = (
                f"{candidate_id} distinct source-bounded assertion {slot:03d}: "
                f"observed context and boundary " + ("specific-note " * 18)
            )
            claims.append(claim)
            support.append({
                "claim_slot_id": f"claim-slot:{slot:03d}",
                "task_id": "comparison:fairness-context",
                "claim": claim,
                "claim_id": claim_id,
                "paper_key": f"paper:{slot % 7}",
                "primary_claim_id": f"primary-claim:{group:03d}",
                "source_claim_ids": [f"source-claim:{slot:03d}", "qualifier:shared"],
                "evidence_ids": [f"evidence:{slot:03d}", "evidence:shared"],
                "source_field_ids": [f"source-field:{slot:03d}", "source-field:shared"],
                "qualifier_source_claim_ids": [f"qualifier-claim:{slot:03d}", "qualifier:shared"],
                "qualifier_evidence_ids": [f"qualifier-evidence:{slot:03d}", "evidence:shared"],
                "qualifier_source_field_ids": [f"qualifier-field:{slot:03d}", "source-field:shared"],
                "study_id": f"study:{slot % 7}",
                "comparison_role": "A" if slot % 2 == 0 else "B",
            })
        paper_keys = sorted({row["paper_key"] for row in support})
        contents[candidate_id] = {
            "candidate_id": candidate_id,
            "organizing_logic": f"Compare distinct source evidence for {candidate_id}.",
            "sections": [{
                "section_id": _SECTION_IDS[candidate_id],
                "title": "Fairness context comparison",
                "goal": "Compare each aligned primary claim while retaining source qualifiers.",
                "task_ids": ["comparison:fairness-context"],
                "paper_keys": paper_keys,
                "relation_ids": [],
                "claims": claims,
                "claim_support": support,
            }],
        }
    return contents


def _request_builder(body):
    return {
        "system_prompt": "Review only the bound source claims and their complete support rows.",
        "output_contract": "Assess all five alternatives; echo exact part receipts when present.",
        "request": body,
    }


def _measure(request):
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "estimated_input_tokens": math.ceil(len(raw) / 4),
        "requested_output_tokens": 512,
        "within_budget": True,
    }


@lru_cache(maxsize=None)
def _plan(
    *, input_cap: int = 16_000, max_calls: int = 64, claim_count: int = 52,
    multi_support: bool = False, missing_b: bool = False,
):
    contents = _current104_shape(claim_count=claim_count)
    if multi_support:
        for candidate_id in _CANDIDATES:
            section = contents[candidate_id]["sections"][0]
            for support_row in section["claim_support"]:
                support_row.pop("comparison_role", None)
            section["claim_support"][0]["primary_claim_id"] = "primary-claim:slot-000-a"
            section["claim_support"][1]["primary_claim_id"] = "primary-claim:slot-001-b"
            extra_support = copy.deepcopy(section["claim_support"][0])
            extra_support["claim_slot_id"] = "claim-slot:000-extra"
            extra_support["primary_claim_id"] = "primary-claim:slot-000-qualifier"
            section["claim_support"].insert(1, extra_support)
    if missing_b:
        for candidate_id in _CANDIDATES:
            contents[candidate_id]["sections"][0]["claim_support"][1]["comparison_role"] = "A"
    candidate_hashes = {
        candidate_id: compute_v3_hash({"candidate": candidate_id}) for candidate_id in _CANDIDATES
    }
    disposition = {
        "eligible_candidate_ids": list(_CANDIDATES),
        "global_blocker": False,
        "issues": [
            {
                "issue_id": "critic:section:unresolved",
                "scope": "section",
                "candidate_id": "candidate_1",
                "parent_candidate_hash": candidate_hashes["candidate_1"],
                "target_ids": [_SECTION_IDS["candidate_1"]],
                "evidence_refs": ["evidence:shared"],
                "severity": "critical",
                "resolution_status": "unresolved",
                "message": "Retain this section-level unresolved issue in every related part.",
            },
            {
                "issue_id": "critic:claim:unresolved",
                "scope": "claim",
                "candidate_id": "candidate_2",
                "parent_candidate_hash": candidate_hashes["candidate_2"],
                "target_ids": ["claim-target:010"],
                "evidence_refs": ["evidence:010"],
                "severity": "critical",
                "resolution_status": "unresolved",
                "message": "Preserve the qualifier boundary for the targeted claim.",
            },
            {
                "issue_id": "critic:global:unresolved",
                "scope": "global",
                "target_ids": [],
                "evidence_refs": [],
                "severity": "critical",
                "resolution_status": "unresolved",
                "message": "Keep this global unresolved issue available to the final arbiter.",
            },
        ],
    }
    plan = plan_arbitration_hierarchy(
        _CANDIDATES,
        candidate_hashes,
        contents,
        disposition,
        critique_hashes={"structure_critique": "unobserved-critic-hash"},
        provenance_hashes={"candidate_output_scope": "scope-hash", "review_intent": "intent-hash"},
        request_builder=_request_builder,
        estimate_input_tokens=_measure,
        input_cap=input_cap,
        max_calls=max_calls,
        output_cap=1024,
    )
    return plan, contents


def _response_for(plan, leaf):
    rows = []
    for candidate_id in plan["candidate_ids"]:
        expected = leaf["expected_candidate_rows"][candidate_id]
        present = expected["presence"] == "present"
        row = {
            "candidate_id": candidate_id,
            "candidate_hash": expected["candidate_hash"],
            "presence": expected["presence"],
            "reviewed_section_ids": list(expected["reviewed_section_ids"]),
            "section_hashes": dict(expected["section_hashes"]),
            "coverage_assessment": "The source-bounded part covers the assigned comparison group." if present else "No section assigned.",
            "evidence_assessment": "The complete support rows retain all cited qualifiers." if present else "No source section assigned.",
            "structure_assessment": "The original parent section identity is preserved." if present else "No source section assigned.",
            "advantages": ["A/B comparison rows remain together by primary claim."] if present else [],
            "limitations": ["This assessment is limited to the assigned part."] if present else [],
            "evidence_refs": list(expected["allowed_evidence_refs"][:1]) if present else [],
            "critic_issue_ids": list(expected["critic_issue_ids"]) if present else [],
        }
        if "claim_support_part_receipts" in expected:
            row["claim_support_part_receipts"] = copy.deepcopy(expected["claim_support_part_receipts"])
        rows.append(row)
    return {
        "leaf_id": leaf["leaf_id"],
        "unit_ids": list(leaf["unit_ids"]),
        "candidate_assessments": rows,
    }


def test_current104_fiftytwo_slot_sections_split_only_after_exact_whole_section_failure():
    plan, contents = _plan()
    assert len(plan["candidate_ids"]) == 5
    assert all(len(contents[candidate]["sections"][0]["claims"]) == 52 for candidate in _CANDIDATES)
    assert all(len(contents[candidate]["sections"][0]["claim_support"]) == 52 for candidate in _CANDIDATES)
    assert plan.get("claim_support_partition", {}).get("schema_version") == "outline_candidate_claim_support_partition/v1"
    assert len(plan["claim_support_partition"]["parent_sections"]) == 5
    assert all(len(row["slots"]) == 52 for row in plan["claim_support_partition"]["parent_sections"])
    assert len(plan["leaves"]) > 1
    assert all(leaf["estimated_input_tokens"] <= 16_000 for leaf in plan["leaves"])
    assert plan["call_count"] == len(plan["leaves"]) + 1

    # Each two-row A/B support group has one task/primary-claim scope. Shared
    # qualifier/source IDs recur across groups but do not fuse those groups.
    parent = plan["claim_support_partition"]["parent_sections"][0]
    group_slots: dict[str, set[str]] = {}
    for slot in parent["slots"]:
        group_slots.setdefault(slot["comparison_group_id"], set()).add(slot["claim_slot_id"])
    assert len(group_slots) == 26
    assert all(len(slot_ids) == 2 for slot_ids in group_slots.values())
    assert sum("qualifier:shared" in row["source_claim_ids"] for row in contents["candidate_1"]["sections"][0]["claim_support"]) == 52

    expected_pairs = {
        (candidate, _SECTION_IDS[candidate], slot)
        for candidate in _CANDIDATES
        for slot in (f"claim-slot:{index:03d}" for index in range(52))
    }
    observed_pairs = set()
    for leaf in plan["leaves"]:
        body = leaf["request"]["request"]
        assert body["candidate_ids"] == _CANDIDATES
        assert body["hierarchy"]["level"] == "claim_support_part_comparison_leaf"
        assert body["claim_support_part_bindings"]["parts"]
        for candidate_id, sections in body["candidate_sections_by_id"].items():
            assert len(sections) == 1
            section = sections[0]
            assert section["section_id"] == _SECTION_IDS[candidate_id]
            for support in section["claim_support"]:
                slot_id = support["claim_slot_id"]
                observed_pairs.add((candidate_id, section["section_id"], slot_id))
                assert support == next(
                    row for row in contents[candidate_id]["sections"][0]["claim_support"]
                    if row["claim_slot_id"] == slot_id
                )
        for candidate_id in _CANDIDATES:
            expected = leaf["expected_candidate_rows"][candidate_id]
            part_receipts = expected["claim_support_part_receipts"]
            assert all(set(row) == {"part_id", "part_hash", "parent_section_bindings"} for row in part_receipts)
            assert all(len(row["part_hash"]) == 64 for row in part_receipts)
            assert all(
                binding == {"section_id": _SECTION_IDS[candidate_id], "section_hash": next(
                    row["section_hash"] for row in plan["candidate_section_index"][candidate_id]
                    if row["section_id"] == _SECTION_IDS[candidate_id]
                )}
                for receipt in part_receipts for binding in receipt["parent_section_bindings"]
            )
    assert observed_pairs == expected_pairs

    section_issue_leaf_ids = {
        leaf["leaf_id"] for leaf in plan["leaves"]
        if "critic:section:unresolved" in leaf["expected_candidate_rows"]["candidate_1"]["critic_issue_ids"]
    }
    assert section_issue_leaf_ids == {
        leaf["leaf_id"] for leaf in plan["leaves"]
        if "section:candidate_1:1" in leaf["expected_candidate_rows"]["candidate_1"]["reviewed_section_ids"]
    }
    claim_issue_leaf_ids = {
        leaf["leaf_id"] for leaf in plan["leaves"]
        if "critic:claim:unresolved" in leaf["expected_candidate_rows"]["candidate_2"]["critic_issue_ids"]
    }
    assert claim_issue_leaf_ids
    assert all(
        any(row["claim_id"] == "claim-target:010" for section in leaf["request"]["request"]["candidate_sections_by_id"]["candidate_2"]
            for row in section["claim_support"])
        for leaf in plan["leaves"] if leaf["leaf_id"] in claim_issue_leaf_ids
    )
    assert all(
        leaf["request"]["request"]["global_critique_issues"][0]["issue_id"] == "critic:global:unresolved"
        for leaf in plan["leaves"]
    )

    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    for leaf_id, response in results.items():
        validate_arbitration_leaf(plan, leaf_id, response)
    ledger = build_arbitration_final_ledger(plan, results)
    assert ledger["candidate_hashes"] == plan["candidate_hashes"]
    assert [row["section_id"] for row in ledger["candidate_section_index"]["candidate_1"]] == [
        _SECTION_IDS["candidate_1"]
    ]
    assert len(ledger["assessments"]) == len(plan["leaves"]) * 5
    assert ledger["coverage"]["expected_candidate_section_count"] == 5
    assert ledger["coverage"]["observed_candidate_section_count"] == 5
    issue_coverage = {row["issue_id"]: row for row in ledger["claim_support_partition"]["typed_issue_coverage"]}
    assert set(issue_coverage) == {
        "critic:section:unresolved", "critic:claim:unresolved", "critic:global:unresolved"
    }
    assert {row["claim_slot_ids"][0] for row in issue_coverage["critic:claim:unresolved"]["claim_slot_targets"]} == {
        "claim-slot:010"
    }
    packed = pack_arbitration_final_ledger(ledger)
    packed_partition = packed["claim_support_partition"]
    assert packed_partition["schema_version"] == "outline_candidate_claim_support_partition_wire/v1"
    assert len(json.dumps(packed_partition, ensure_ascii=False)) < len(
        json.dumps(ledger["claim_support_partition"], ensure_ascii=False)
    ) / 2
    assert expand_arbitration_final_ledger(packed) == ledger

    damaged_wire = copy.deepcopy(packed)
    wire_parent = damaged_wire["claim_support_partition"]["parent_sections"][0]
    wire_parent[7][1][1] = 99
    damaged_wire["wire_body_hash"] = compute_v3_hash({
        key: value for key, value in damaged_wire.items() if key != "wire_body_hash"
    })
    with pytest.raises(ArbitrationLedgerPackingError, match="canonical digest|occurrence index"):
        expand_arbitration_final_ledger(damaged_wire)


def test_multi_support_rows_share_one_authored_claim_occurrence_and_remain_complete():
    plan, _contents = _plan(multi_support=True)
    parent = next(
        row for row in plan["claim_support_partition"]["parent_sections"]
        if row["candidate_id"] == "candidate_1"
    )
    assert parent["claim_count"] == 52
    assert parent["support_row_count"] == 53
    by_slot = {row["claim_slot_id"]: row for row in parent["slots"]}
    assert by_slot["claim-slot:000"]["claim_occurrence_index"] == 0
    assert by_slot["claim-slot:000-extra"]["claim_occurrence_index"] == 0
    assert by_slot["claim-slot:000"]["support_row_index"] != by_slot["claim-slot:000-extra"]["support_row_index"]
    assert by_slot["claim-slot:000"]["comparison_group_id"] == by_slot["claim-slot:000-extra"]["comparison_group_id"]
    assert by_slot["claim-slot:000"]["comparison_scope_id"] != by_slot["claim-slot:000-extra"]["comparison_scope_id"]
    assert by_slot["claim-slot:000"]["comparison_group_id"] != by_slot["claim-slot:001"]["comparison_group_id"]
    assert all("comparison_role" not in row for row in _contents["candidate_1"]["sections"][0]["claim_support"])
    slot_zero_part_leaves = [
        leaf for leaf in plan["leaves"]
        if any(
            "claim-slot:000-extra" in binding["claim_slot_ids"]
            for part in leaf["request"]["request"]["claim_support_part_bindings"]["parts"]
            for binding in part["parent_bindings_by_candidate"]["candidate_1"]
        )
    ]
    assert slot_zero_part_leaves
    assert all(
        {"claim-slot:000", "claim-slot:000-extra"}.issubset(
            {row["claim_slot_id"] for section in leaf["request"]["request"]["candidate_sections_by_id"]["candidate_1"]
             for row in section["claim_support"]}
        )
        for leaf in slot_zero_part_leaves
    )
    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    ledger = build_arbitration_final_ledger(plan, results)
    assert expand_arbitration_final_ledger(pack_arbitration_final_ledger(ledger)) == ledger


def test_explicit_ab_comparison_part_rejects_a_missing_b_side():
    with pytest.raises(ArbitrationHierarchyError, match="explicit A/B comparison group is incomplete"):
        _plan(input_cap=1, claim_count=52, missing_b=True)


def test_one_authored_claim_with_different_primary_supports_is_indivisible_over_cap():
    with pytest.raises(ArbitrationHierarchyError, match="indivisible section leaf.*exceeds cap"):
        _plan(input_cap=1, multi_support=True)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "foreign_part", "stale_hash", "foreign_parent", "duplicate_part"],
)
def test_part_response_rejects_missing_foreign_stale_or_duplicate_receipts(mutation):
    plan, _contents = _plan()
    leaf = plan["leaves"][0]
    response = _response_for(plan, leaf)
    row = response["candidate_assessments"][0]
    if mutation == "missing":
        row.pop("claim_support_part_receipts")
    elif mutation == "foreign_part":
        row["claim_support_part_receipts"][0]["part_id"] = "claim-support-part:foreign"
    elif mutation == "stale_hash":
        row["claim_support_part_receipts"][0]["part_hash"] = "f" * 64
    elif mutation == "foreign_parent":
        row["claim_support_part_receipts"][0]["parent_section_bindings"][0]["section_id"] = "foreign-section"
    else:
        row["claim_support_part_receipts"].append(copy.deepcopy(row["claim_support_part_receipts"][0]))
    with pytest.raises(ArbitrationHierarchyError, match="part receipts"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], response)


def test_claim_support_ledger_rejects_tampered_partition_even_after_rehash():
    plan, _contents = _plan()
    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    ledger = build_arbitration_final_ledger(plan, results)
    damaged = copy.deepcopy(ledger)
    binding = damaged["claim_support_partition"]["unit_assignments"][0]["bindings_by_candidate"][
        "candidate_1"
    ][0]
    first_slot = binding["claim_slot_ids"][0]
    binding["support_row_hashes"][first_slot] = "stale-support-hash"
    damaged["ledger_hash"] = compute_v3_hash({key: value for key, value in damaged.items() if key != "ledger_hash"})
    with pytest.raises(ArbitrationLedgerPackingError, match="slot/occurrence binding is stale"):
        pack_arbitration_final_ledger(damaged)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("out_of_range", "occurrence index is invalid"),
        ("alias", "authored claim-occurrence coverage is not exact"),
    ],
)
def test_hierarchy_rejects_rehashed_wrong_or_aliased_claim_occurrence_indices(mutation, message):
    plan = copy.deepcopy(_plan()[0])
    target = next(
        row for row in plan["claim_support_partition"]["parent_sections"]
        if row["candidate_id"] == "candidate_1"
    )
    target["slots"][1]["claim_occurrence_index"] = (
        99 if mutation == "out_of_range" else target["slots"][0]["claim_occurrence_index"]
    )
    plan["plan_hash"] = compute_v3_hash({
        key: value for key, value in plan.items() if key not in {"plan_hash", "max_calls"}
    })
    with pytest.raises(ArbitrationHierarchyError, match=message):
        validate_arbitration_leaf(plan, plan["leaves"][0]["leaf_id"], _response_for(plan, plan["leaves"][0]))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("out_of_range", "occurrence index is invalid"),
        ("alias", "authored claim-occurrence coverage is not exact"),
    ],
)
def test_canonical_ledger_rejects_rehashed_wrong_or_aliased_claim_occurrence_indices(mutation, message):
    plan, _contents = _plan()
    results = {leaf["leaf_id"]: _response_for(plan, leaf) for leaf in plan["leaves"]}
    damaged = copy.deepcopy(build_arbitration_final_ledger(plan, results))
    target = next(
        row for row in damaged["claim_support_partition"]["parent_sections"]
        if row["candidate_id"] == "candidate_1"
    )
    target["slots"][1]["claim_occurrence_index"] = (
        99 if mutation == "out_of_range" else target["slots"][0]["claim_occurrence_index"]
    )
    damaged["ledger_hash"] = compute_v3_hash({
        key: value for key, value in damaged.items() if key != "ledger_hash"
    })
    with pytest.raises(ArbitrationLedgerPackingError, match=message):
        pack_arbitration_final_ledger(damaged)


def test_unaligned_or_atomic_oversized_parent_fails_closed_before_any_leaf_can_dispatch():
    with pytest.raises(ArbitrationHierarchyError, match="indivisible section leaf.*exceeds cap"):
        _plan(input_cap=1, max_calls=64, claim_count=52)

    contents = _current104_shape(claim_count=4)
    for candidate_id in _CANDIDATES:
        for support in contents[candidate_id]["sections"][0]["claim_support"]:
            support.pop("comparison_role", None)
    for candidate_id in _CANDIDATES[1:]:
        contents[candidate_id]["sections"][0]["claim_support"][1]["primary_claim_id"] = "misaligned-primary"
    hashes = {candidate: compute_v3_hash({"candidate": candidate}) for candidate in _CANDIDATES}
    disposition = {"eligible_candidate_ids": list(_CANDIDATES), "global_blocker": False, "issues": []}
    with pytest.raises(ArbitrationHierarchyError, match="indivisible section leaf.*exceeds cap"):
        plan_arbitration_hierarchy(
            _CANDIDATES,
            hashes,
            contents,
            disposition,
            critique_hashes={"structure_critique": "critic-hash"},
            provenance_hashes={"candidate_output_scope": "scope-hash"},
            request_builder=_request_builder,
            estimate_input_tokens=_measure,
            input_cap=1,
            max_calls=64,
            output_cap=1024,
        )


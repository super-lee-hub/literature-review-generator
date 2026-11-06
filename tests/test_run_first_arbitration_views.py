"""Source joins and literal-wire integrity for v2 overflow arbitration."""

from __future__ import annotations

import copy

import pytest

from outline.candidate_arbitration_views import (
    ArbitrationViewError,
    build_arbitration_review_group_map,
    expand_arbitration_critical_view_wire,
    pack_arbitration_critical_view_wire,
    validate_arbitration_view_set,
)
from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
)
from outline.v3_models import compute_v3_hash
from tests.test_run_first_arbitration_aggregation import _build_ready


def _relation_scope(*, duplicate_relation_groups=0):
    task_id = "relation_comparison:relation-7"
    question = "Does the source evidence differ by context?"
    contract = {
        "schema_version": "relation-comparison-task/v1",
        "task_id": task_id,
        "relation_id": "relation-7",
        "relation_type": "context_difference",
        "comparison_question": question,
        "output_semantics": "model_authored_comparison_using_both_source_sides_and_their_qualifiers",
        "relation_id_is_not_a_conclusion": True,
    }
    groups = []
    slots = []
    count = 1 + duplicate_relation_groups
    for index in range(count):
        group_id = f"relation-group:{index + 1}"
        assertion_id = f"synthesis:relation-comparison:{index + 1}"
        slot_id = f"relation-slot:{index + 1}"
        groups.append(CandidateOutputClaimGroupV1(
            claim_group_id=group_id,
            task_id=task_id,
            topic_id="",
            fragment_id="",
            provider_result_id="",
            synthesis_claim_id=assertion_id,
            claim_hash="a" * 64,
            paper_keys=("paper:left", "paper:right"),
            support_slot_ids=(slot_id,),
            relation_ids=("relation-7",),
            claim_kind="model_authored_relation_comparison_slot",
            claim_seed=True,
            task_contract=tuple(sorted(contract.items())),
        ))
        slots.append(CandidateOutputSupportSlotV1(
            claim_slot_id=slot_id,
            claim_group_id=group_id,
            task_id=task_id,
            topic_id="",
            fragment_id="",
            provider_result_id="",
            synthesis_claim_id=assertion_id,
            paper_key="paper:left" if index % 2 == 0 else "paper:right",
            primary_claim_id=f"claim:{index + 1}",
            study_id="",
            source_claim_ids=(f"claim:{index + 1}",),
            evidence_ids=(f"evidence:{index + 1}",),
            source_field_ids=(),
            relation_ids=("relation-7",),
        ))
    return CandidateOutputScopeV1(
        registry_job_id="job",
        source_inventory_artifact_id="inventory",
        source_inventory_artifact_hash="b" * 64,
        source_inventory_content_hash="c" * 64,
        task_ids=(task_id,),
        selected_relation_ids=("relation-7",),
        claim_groups=tuple(groups),
        claim_slots=tuple(slots),
    ), {"required_comparisons": [{
        "paper_keys": ["paper:left", "paper:right"],
        "question": question,
        "relation_type": "context_difference",
    }]}


def test_review_group_map_has_exact_w_equal_to_task_plus_comparison_plus_issue():
    scope, intent = _relation_scope()
    issues = [{
        "issue_id": "issue:critical:1",
        "scope": "section",
        "target_ids": ["section:1"],
        "severity": "blocking",
        "evidence_refs": ["evidence:1"],
        "resolution_status": "resolved",
        "candidate_id": "candidate_1",
        "parent_candidate_hash": "d" * 64,
        "source_critic": "structure_critic",
        "source": "typed",
        "message": "Keep the source comparison visible.",
    }]

    group_map = build_arbitration_review_group_map(scope, intent, issues)

    assert group_map["schema_version"] == "arbitration_review_group_map/v1"
    assert group_map["task_count"] == 1
    assert group_map["comparison_count"] == 1
    assert group_map["typed_issue_count"] == 1
    assert group_map["group_count"] == 3
    assert group_map["group_count"] == (
        group_map["task_count"] + group_map["comparison_count"] + group_map["typed_issue_count"]
    )
    comparison_id = group_map["comparison_relation_bindings"]
    assert len(comparison_id) == 1
    group = next(row for row in group_map["groups"] if row["group_kind"] == "required_comparison")
    assert group["source_scope_ids"] == ["relation-group:1"]
    assert group["required_comparison_ids"] == [group["group_id"]]
    task_group = next(row for row in group_map["groups"] if row["group_kind"] == "task_topic")
    assert task_group["source_scope_ids"] == ["relation-group:1"]
    assert task_group["used_assertion_ids"] == ["synthesis:relation-comparison:1"]


def test_required_comparison_join_fails_when_relation_group_is_missing_or_ambiguous():
    scope, intent = _relation_scope(duplicate_relation_groups=1)
    with pytest.raises(ArbitrationViewError, match="2 matching source relation groups"):
        build_arbitration_review_group_map(scope, intent, [])

    unmatched, unmatched_intent = _relation_scope()
    unmatched_intent["required_comparisons"][0]["question"] = "A different comparison question"
    with pytest.raises(ArbitrationViewError, match="0 matching source relation groups"):
        build_arbitration_review_group_map(unmatched, unmatched_intent, [])


def test_verified_literal_views_roundtrip_on_wire_with_shared_catalog_and_unknown_direction(tmp_path):
    inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=5, slot_count=1,
    )
    view_set = validate_arbitration_view_set(
        plan["critical_view_set"],
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
    )
    wire = pack_arbitration_critical_view_wire(
        view_set, expected_view_set_hash=view_set["view_set_hash"],
    )
    expanded = expand_arbitration_critical_view_wire(
        wire, expected_view_set_hash=view_set["view_set_hash"],
    )

    assert wire["schema_version"] == "arbitration_critical_view_wire/v1"
    assert wire["source_view_set_hash"] == view_set["view_set_hash"]
    assert len(wire["views"]) == len(view_set["critical_views"]) == 5
    assert len(wire["source_literal_bundle_catalog"]) < len(wire["views"])
    assert expanded == view_set["critical_views"]
    assert all(
        row["source_bindings"]["source_relation_direction"] == {"status": "unknown", "value": None}
        for row in expanded
    )
    assert all(row["source_bindings"]["source_inventory_artifact_hash"] == inventory.artifact_hash for row in expanded)
    assert all(row["source_bindings"]["study_status"] in {"known", "unknown"} for row in expanded)
    assert '"critical_for":' not in str(expanded)

    tampered = copy.deepcopy(wire)
    catalog = tampered["source_literal_bundle_catalog"]
    for row in catalog:
        claims = row["literal_critical_detail"].get("source_claims") or []
        if claims:
            claims[0]["text"] += " altered"
            row["bundle_hash"] = compute_hash_for_test({
                "source_bindings": row["source_bindings"],
                "literal_qualifier_values": row["literal_qualifier_values"],
                "literal_critical_detail": row["literal_critical_detail"],
            })
            break
    tampered["source_literal_bundle_catalog_hash"] = compute_hash_for_test(catalog)
    tampered.pop("wire_hash")
    tampered["wire_hash"] = compute_hash_for_test(tampered)
    with pytest.raises(ArbitrationViewError, match="literal binding differs"):
        expand_arbitration_critical_view_wire(
            tampered, expected_view_set_hash=view_set["view_set_hash"],
        )


def test_self_rehashed_same_length_source_literal_is_rejected_against_ready_view_hash(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=1, slot_count=1,
    )
    view_set = copy.deepcopy(plan["critical_view_set"])
    ready_hash = view_set["view_set_hash"]
    view = view_set["critical_views"][0]
    claim = view["literal_critical_detail"]["source_claims"][0]
    claim["text"] = "X" + claim["text"][1:]
    view.pop("decision_view_hash")
    view["decision_view_hash"] = compute_v3_hash(view)
    view_set.pop("view_set_hash")
    view_set["view_set_hash"] = compute_v3_hash(view_set)

    with pytest.raises(ArbitrationViewError, match="READY|pinned|expected"):
        validate_arbitration_view_set(view_set, expected_view_set_hash=ready_hash)


def test_source_disagreement_status_cannot_be_rehashed_into_empirical_judgment(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=1, slot_count=1,
    )
    view_set = copy.deepcopy(plan["critical_view_set"])
    view = view_set["critical_views"][0]
    view["source_bindings"]["source_disagreement_status"] = {
        "status": "known",
        "value": "disagrees",
    }
    view.pop("decision_view_hash")
    view["decision_view_hash"] = compute_v3_hash(view)
    view_set.pop("view_set_hash")
    view_set["view_set_hash"] = compute_v3_hash(view_set)

    with pytest.raises(ArbitrationViewError, match="disagreement status must remain unknown"):
        validate_arbitration_view_set(
            view_set,
            expected_view_set_hash=view_set["view_set_hash"],
        )


def test_self_recomputed_view_and_viewset_hash_cannot_replace_pinned_source_literals(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=1, slot_count=1,
    )
    trusted_view_set = copy.deepcopy(plan["critical_view_set"])
    pinned_hash = trusted_view_set["view_set_hash"]
    tampered = copy.deepcopy(trusted_view_set)
    row = tampered["critical_views"][0]
    claim = row["literal_critical_detail"]["source_claims"][0]
    claim["text"] = "X" + claim["text"][1:]
    row.pop("decision_view_hash")
    row["decision_view_hash"] = compute_v3_hash(row)
    tampered.pop("view_set_hash")
    tampered["view_set_hash"] = compute_v3_hash(tampered)

    with pytest.raises(ArbitrationViewError, match="pinned|READY|expected"):
        validate_arbitration_view_set(tampered, expected_view_set_hash=pinned_hash)


def compute_hash_for_test(value):
    from outline.v3_models import compute_v3_hash

    return compute_v3_hash(value)

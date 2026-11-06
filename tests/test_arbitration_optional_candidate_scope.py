"""Regression coverage for candidate-specific use of optional source slots."""

from __future__ import annotations

import copy
import json

import pytest

from outline.candidate_arbitration_aggregation import (
    ArbitrationAggregationError,
    _selector_detail_literal,
    build_overflow_assessment_plan,
)
from outline.candidate_arbitration_views import (
    ArbitrationViewError,
    _source_fact_payload,
    validate_arbitration_view_set,
)
from outline.v3_models import compute_v3_hash
from tests.test_run_first_arbitration_aggregation import (
    _caps,
    _input_estimator,
    _legacy_plan_and_ledger,
    _output_estimator,
    _prepared_legacy_source_request,
    _route_builder,
)
from tests.test_run_first_candidate_fragment_bank import _sealed_scope


def _review_intent(scope, required_group):
    contract = dict(required_group.task_contract)
    return {
        "review_question": "Compare the source-reported findings within their recorded boundaries.",
        "scope": "Retain source qualifiers and any disagreement or null result.",
        "must_cover": list(scope.task_ids),
        "required_comparisons": [{
            "paper_keys": list(required_group.paper_keys),
            "question": contract["comparison_question"],
            "relation_type": contract["relation_type"],
        }],
    }


def _candidate_content(scope, candidate_id: str, group_ids: set[str]) -> dict:
    sections = []
    for group in scope.claim_groups:
        if group.claim_group_id not in group_ids:
            continue
        slots = sorted(
            (slot for slot in scope.claim_slots if slot.claim_group_id == group.claim_group_id),
            key=lambda slot: slot.claim_slot_id,
        )
        assert {slot.claim_slot_id for slot in slots} == set(group.support_slot_ids)
        claim = f"{candidate_id} preserves the complete source closure for {group.claim_group_id}."
        support_rows = []
        for slot in slots:
            support_rows.append({
                "claim_id": f"candidate-claim:{candidate_id}:{slot.claim_slot_id}",
                "claim_group_id": slot.claim_group_id,
                "synthesis_claim_id": slot.synthesis_claim_id,
                "claim_slot_id": slot.claim_slot_id,
                "task_id": slot.task_id,
                "claim": claim,
                "paper_key": slot.paper_key,
                "primary_claim_id": slot.primary_claim_id,
                "study_id": slot.study_id,
                "source_claim_ids": list(slot.source_claim_ids),
                "evidence_ids": list(slot.evidence_ids),
                "source_field_ids": list(slot.source_field_ids),
                "relation_ids": list(slot.relation_ids),
            })
        sections.append({
            "section_id": f"{candidate_id}:section:{group.claim_group_id}",
            "title": f"Source group {group.claim_group_id}",
            "goal": "Keep each included claim attached to its complete source support.",
            "task_ids": [group.task_id],
            "paper_keys": sorted(group.paper_keys),
            "relation_ids": list(group.relation_ids),
            "claims": [claim],
            "claim_support": support_rows,
        })
    return {
        "candidate_id": candidate_id,
        "organizing_logic": f"Source-bounded organization for {candidate_id}.",
        "sections": sections,
        "planned_claims": [claim for section in sections for claim in section["claims"]],
    }


def _build_ready_plan(inventory, scope, contents, intent):
    candidate_ids = sorted(contents)
    _candidate_hashes, disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
        candidate_ids, contents, scope, intent, [],
    )
    legacy_source_request = _prepared_legacy_source_request(
        candidate_ids,
        legacy_plan["candidate_hashes"],
        contents,
        disposition,
        intent,
    )
    canonical_contents = copy.deepcopy(legacy_source_request["candidate_contents"])
    ledger_snapshot = (
        compute_v3_hash(ledger),
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ledger["ledger_hash"],
    )
    scope_hash = scope.content_hash
    inventory_hash = inventory.content_hash
    plan = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=ledger,
        legacy_source_request=legacy_source_request,
        candidate_contents=canonical_contents,
        candidate_scope=scope,
        source_inventory=inventory,
        review_intent=intent,
        critique_disposition=disposition,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )
    assert scope.content_hash == scope_hash
    assert inventory.content_hash == inventory_hash
    assert (
        compute_v3_hash(ledger),
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ledger["ledger_hash"],
    ) == ledger_snapshot
    return plan, canonical_contents, legacy_plan, ledger


def _optional_fixture(tmp_path):
    inventory, _routes, _bridges, _bundles, source_scope = _sealed_scope(tmp_path)
    required_group = next(
        group for group in source_scope.claim_groups
        if group.claim_kind == "model_authored_relation_comparison_slot"
        and dict(group.task_contract).get("relation_id") == "relation:required-ab"
    )
    optional_groups = [
        group for group in source_scope.claim_groups
        if not group.claim_seed and group.claim_group_id != required_group.claim_group_id
    ]
    scope = source_scope.for_groups([
        required_group.claim_group_id,
        *(group.claim_group_id for group in optional_groups),
    ])
    required_group = next(
        group for group in scope.claim_groups if group.claim_group_id == required_group.claim_group_id
    )
    optional_groups = [group for group in scope.claim_groups if not group.claim_seed]
    assert len(optional_groups) >= 2
    candidate_group_ids = {
        "candidate_1": {required_group.claim_group_id, optional_groups[0].claim_group_id},
        "candidate_2": {required_group.claim_group_id, optional_groups[1].claim_group_id},
    }
    contents = {
        candidate_id: _candidate_content(scope, candidate_id, group_ids)
        for candidate_id, group_ids in candidate_group_ids.items()
    }
    intent = _review_intent(scope, required_group)
    return inventory, scope, required_group, optional_groups, candidate_group_ids, contents, intent


def test_candidates_partition_only_used_optional_groups_and_keep_unused_detail(tmp_path):
    inventory, scope, required_group, optional_groups, selected, contents, intent = _optional_fixture(tmp_path)
    plan, canonical_contents, _legacy_plan, _ledger = _build_ready_plan(
        inventory, scope, contents, intent,
    )

    group_map = plan["review_group_map"]
    assert group_map["group_count"] == group_map["task_count"] + group_map["comparison_count"] + group_map["typed_issue_count"]
    assert group_map["group_count"] == (
        len(scope.task_ids) + len(intent["required_comparisons"]) + len(group_map["issue_rows"])
    )
    assert group_map["claim_group_slot_bindings"] == {
        group.claim_group_id: sorted(group.support_slot_ids)
        for group in sorted(scope.claim_groups, key=lambda item: item.claim_group_id)
    }
    relation_task = next(group.task_id for group in scope.claim_groups if group.claim_group_id == required_group.claim_group_id)
    relation_summary = next(
        row for row in group_map["groups"]
        if row["group_kind"] == "task_topic" and row["task_id"] == relation_task
    )
    assert relation_summary["source_scope_ids"] == sorted(
        group.claim_group_id for group in scope.claim_groups if group.task_id == relation_task
    )
    assert relation_summary["used_assertion_ids"] == [required_group.synthesis_claim_id]
    for optional_task_id in sorted({group.task_id for group in optional_groups}):
        row = next(
            row for row in group_map["groups"]
            if row["group_kind"] == "task_topic" and row["task_id"] == optional_task_id
        )
        expected_by_candidate = {
            candidate_id: sorted({
                group.synthesis_claim_id
                for group in optional_groups
                if group.task_id == optional_task_id and group.claim_group_id in group_ids
            })
            for candidate_id, group_ids in selected.items()
        }
        assert row["used_assertion_ids_by_candidate"] == expected_by_candidate
        assert row["used_assertion_ids"] == sorted({
            assertion_id
            for assertion_ids in expected_by_candidate.values()
            for assertion_id in assertion_ids
        })

    used_index = {
        candidate_id: {
            row["claim_slot_id"]: row["usage_status"]
            for row in plan["critical_view_set"]["source_slot_scope_index"]
            if row["candidate_id"] == candidate_id
        }
        for candidate_id in selected
    }
    assert all(len(rows) == len(scope.claim_slots) for rows in used_index.values())
    for candidate_id, group_ids in selected.items():
        expected_used = {
            slot.claim_slot_id for slot in scope.claim_slots if slot.claim_group_id in group_ids
        }
        actual_used = {
            slot_id for slot_id, status in used_index[candidate_id].items() if status == "used"
        }
        assert actual_used == expected_used
        assert {
            slot_id for slot_id, status in used_index[candidate_id].items()
            if status == "not_used_optional"
        } == {slot.claim_slot_id for slot in scope.claim_slots} - expected_used
        unit_slots = {
            slot_id
            for unit in plan["assessment_units"]
            for slot_id in unit["candidate_rows"][candidate_id]["claim_slot_ids"]
        }
        assert unit_slots == expected_used
        assert candidate_group_ids_for_contents(canonical_contents[candidate_id]) == expected_used

    unused_group = optional_groups[1]
    unused_slot = next(slot for slot in scope.claim_slots if slot.claim_group_id == unused_group.claim_group_id)
    unused_scope_row = next(
        row for row in plan["critical_view_set"]["source_slot_scope_index"]
        if row["candidate_id"] == "candidate_1" and row["claim_slot_id"] == unused_slot.claim_slot_id
    )
    assert unused_scope_row["usage_status"] == "not_used_optional"
    assert "candidate_section_id" not in unused_scope_row and "section_id" not in unused_scope_row
    detail_row = next(
        row for row in plan["critical_view_set"]["noncritical_fact_index"]
        if row["candidate_id"] == "candidate_1" and unused_slot.claim_slot_id in row["claim_slot_ids"]
    )
    payload = _selector_detail_literal(plan, detail_row, inventory)
    paper = inventory.paper_by_key()[detail_row["paper_key"]]
    source_rows = {
        "source_claim": paper.claims,
        "evidence": paper.evidence,
        "source_field": paper.source_fields,
    }[detail_row["fact_kind"]]
    identity_field = {
        "source_claim": "claim_id",
        "evidence": "evidence_id",
        "source_field": "source_field_id",
    }[detail_row["fact_kind"]]
    source_row = next(row for row in source_rows if getattr(row, identity_field) == detail_row["fact_id"])
    assert payload == _source_fact_payload(detail_row["fact_kind"], source_row)
    assert detail_row["source_fact_hash"] == compute_v3_hash(payload)


def candidate_group_ids_for_contents(content):
    return {
        str(row["claim_slot_id"])
        for section in content["sections"]
        for row in section["claim_support"]
    }


def test_required_comparison_missing_side_and_wrong_source_still_fail_closed(tmp_path):
    inventory, scope, required_group, optional_groups, _selected, contents, intent = _optional_fixture(tmp_path)

    missing_group = copy.deepcopy(contents)
    missing_group["candidate_1"] = _candidate_content(
        scope, "candidate_1", {optional_groups[0].claim_group_id},
    )
    with pytest.raises(ArbitrationAggregationError, match="required comparison.*not fully used"):
        _build_ready_plan(inventory, scope, missing_group, intent)

    missing_side = copy.deepcopy(contents)
    relation_section = next(
        section for section in missing_side["candidate_1"]["sections"]
        if any(row["claim_group_id"] == required_group.claim_group_id for row in section["claim_support"])
    )
    relation_section["claim_support"].pop()
    relation_section["paper_keys"] = sorted({row["paper_key"] for row in relation_section["claim_support"]})
    with pytest.raises(ArbitrationAggregationError):
        _build_ready_plan(inventory, scope, missing_side, intent)

    wrong_source = copy.deepcopy(contents)
    optional_support = next(
        row for section in wrong_source["candidate_1"]["sections"]
        for row in section["claim_support"]
        if row["claim_group_id"] == optional_groups[0].claim_group_id
    )
    optional_support["source_claim_ids"] = sorted(
        set(optional_support["source_claim_ids"]) | {"claim:foreign"}
    )
    with pytest.raises(ArbitrationAggregationError):
        _build_ready_plan(inventory, scope, wrong_source, intent)


def test_qualifier_tampering_and_missing_critical_literal_are_rejected(tmp_path):
    inventory, scope, _required_group, _optional_groups, _selected, contents, intent = _optional_fixture(tmp_path)
    plan, _canonical_contents, _legacy_plan, _ledger = _build_ready_plan(
        inventory, scope, contents, intent,
    )
    ready_view_set = plan["critical_view_set"]
    pinned_hash = ready_view_set["view_set_hash"]

    qualifier_tamper = copy.deepcopy(ready_view_set)
    qualifier_view = next(
        row for row in qualifier_tamper["critical_views"]
        if row["literal_qualifier_values"]
    )
    qualifier_view["literal_qualifier_values"][0]["literal_value"] += " altered"
    with pytest.raises(ArbitrationViewError):
        validate_arbitration_view_set(qualifier_tamper, expected_view_set_hash=pinned_hash)

    missing_literal = copy.deepcopy(ready_view_set)
    critical_view = next(
        row for row in missing_literal["critical_views"]
        if row["literal_critical_detail"].get("source_claims")
    )
    critical_view["literal_critical_detail"]["source_claims"].pop()
    with pytest.raises(ArbitrationViewError):
        validate_arbitration_view_set(missing_literal, expected_view_set_hash=pinned_hash)

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
)
from outline.candidate_request_packing import (
    CandidatePackingError,
    estimate_candidate_output,
    select_required_candidate_groups,
)
from runtime.provider_runtime import hash_json


def _scope_with_groups(
    definitions: list[dict[str, Any]],
) -> tuple[CandidateOutputScopeV1, dict[str, str]]:
    groups = []
    slots = []
    source_values: dict[str, str] = {}
    for definition in definitions:
        group_id = definition["group_id"]
        papers = tuple(sorted(definition["papers"]))
        relation_ids = tuple(definition.get("relation_ids", ()))
        claim_seed = bool(definition.get("claim_seed", False))
        fields_per_slot = int(definition.get("fields_per_slot", 2))
        evidence_per_slot = int(definition.get("evidence_per_slot", 2))
        slot_ids = []
        for index, paper_key in enumerate(papers):
            slot_id = f"slot:{group_id}:{index}"
            slot_ids.append(slot_id)
            source_claim_ids = (
                f"claim:{paper_key}:primary",
                f"claim:{paper_key}:qualifier",
            )
            evidence_ids = tuple(
                f"evidence:{paper_key}:{value}"
                for value in range(evidence_per_slot)
            )
            source_field_ids = tuple(
                f"field:{group_id}:{paper_key}:{value}"
                for value in range(fields_per_slot)
            )
            for field_id in source_field_ids:
                source_values[field_id] = str(
                    definition.get("field_values", {}).get(
                        paper_key,
                        f"Source boundary {paper_key} field {field_id.rsplit(':', 1)[-1]}.",
                    )
                )
            slots.append(CandidateOutputSupportSlotV1(
                claim_slot_id=slot_id,
                claim_group_id=group_id,
                task_id=str(definition["task_id"]),
                topic_id="",
                fragment_id="",
                provider_result_id="",
                synthesis_claim_id=f"synthesis:{group_id}",
                paper_key=paper_key,
                primary_claim_id=source_claim_ids[0],
                study_id="study-a" if paper_key == "paper-a" else "",
                source_claim_ids=source_claim_ids,
                evidence_ids=evidence_ids,
                source_field_ids=source_field_ids,
                relation_ids=relation_ids,
            ))
        groups.append(CandidateOutputClaimGroupV1(
            claim_group_id=group_id,
            task_id=str(definition["task_id"]),
            topic_id="",
            fragment_id="",
            provider_result_id="",
            synthesis_claim_id=f"synthesis:{group_id}",
            claim_hash=str(definition.get("claim_hash", f"hash:{definition['task_id']}")),
            paper_keys=papers,
            support_slot_ids=tuple(slot_ids),
            relation_ids=relation_ids,
            claim_kind=str(definition.get("claim_kind", "source_closed_claim")),
            claim_seed=claim_seed,
            task_contract=tuple(sorted(dict(definition.get("task_contract", {})).items())),
        ))
    scope = CandidateOutputScopeV1(
        registry_job_id="job-witness-test",
        source_inventory_artifact_id="source-inventory",
        source_inventory_artifact_hash="a" * 64,
        source_inventory_content_hash="b" * 64,
        task_ids=tuple(sorted({group.task_id for group in groups})),
        selected_relation_ids=tuple(sorted({value for group in groups for value in group.relation_ids})),
        claim_groups=tuple(groups),
        claim_slots=tuple(slots),
    )
    return scope, source_values


def _group_id_for_task(scope: CandidateOutputScopeV1, task_id: str) -> str:
    return next(group.claim_group_id for group in scope.claim_groups if group.task_id == task_id)


def test_required_corpus_witness_selects_one_complete_relation_group_and_keeps_core():
    scope, source_values = _scope_with_groups([
        {
            "group_id": "core-ab",
            "task_id": "required-comparison:ab",
            "papers": ["paper-a", "paper-b"],
            "relation_ids": ["relation-ab"],
            "claim_seed": True,
            "task_contract": {"comparison_question": "Compare A and B."},
        },
        {"group_id": "core-c", "task_id": "topic:paper-c", "papers": ["paper-c"]},
        {
            "group_id": "z-p6-boundary",
            "task_id": "required-comparison:paper-c-p6-boundary",
            "papers": ["paper-c", "paper-6"],
            "relation_ids": ["relation-p6-boundary"],
            "claim_seed": True,
            "task_contract": {"relation_type": "qualifies", "comparison_question": "Keep the P6 boundary."},
            "fields_per_slot": 3,
            "evidence_per_slot": 3,
            "claim_hash": "complete-content-hash",
        },
        {
            "group_id": "a-p6-optional",
            "task_id": "optional:paper-c-p6",
            "papers": ["paper-c", "paper-6"],
            "relation_ids": ["relation-p6-optional"],
            "claim_seed": True,
            "fields_per_slot": 1,
            "evidence_per_slot": 1,
            "claim_hash": "sparse-content-hash",
        },
        {"group_id": "optional-unrelated", "task_id": "optional:x", "papers": ["paper-x"]},
    ])
    core_id = _group_id_for_task(scope, "topic:paper-c")
    comparison_id = _group_id_for_task(scope, "required-comparison:ab")
    complete_id = _group_id_for_task(scope, "required-comparison:paper-c-p6-boundary")
    sparse_id = _group_id_for_task(scope, "optional:paper-c-p6")
    unrelated_id = _group_id_for_task(scope, "optional:x")
    measures: list[str] = []

    def measure(group_id: str) -> dict[str, Any]:
        measures.append(group_id)
        # The complete witness has a larger serialized request; semantic
        # source closure still outranks a smaller incomplete-looking group.
        return {"input_tokens": 400 if group_id == complete_id else 100,
                "output_tokens": 100, "within_context": True}

    selected, witnesses = select_required_candidate_groups(
        scope,
        required_paper_keys=["paper-a", "paper-b", "paper-c", "paper-6"],
        core_group_ids=[core_id],
        required_comparison_group_ids=[comparison_id],
        source_field_values=source_values,
        measure_group=measure,
        input_cap=1_000,
        output_cap=500,
    )
    assert set(selected) == {core_id, comparison_id, complete_id}
    assert sparse_id not in selected and unrelated_id not in selected
    assert set(measures) == {complete_id, sparse_id}
    assert all(row["reason"] in {"retained_core_group", "required_comparison", "required_corpus_source_closed_witness"}
               for row in witnesses)
    witness = next(row for row in witnesses if row["paper_key"] == "paper-6")
    assert witness == {
        "paper_key": "paper-6",
        "claim_group_id": complete_id,
        "reason": "required_corpus_source_closed_witness",
    }
    selected_scope = scope.for_groups(selected)
    assert {slot.paper_key for slot in selected_scope.claim_slots} == {
        "paper-a", "paper-b", "paper-c", "paper-6",
    }
    assert all(slot.primary_claim_id in slot.source_claim_ids for slot in selected_scope.claim_slots)
    assert all(slot.evidence_ids for slot in selected_scope.claim_slots)
    assert all(slot.source_field_ids for slot in selected_scope.claim_slots)


def _rename_group_and_field_ids(
    scope: CandidateOutputScopeV1,
    source_values: dict[str, str],
    renames: dict[str, str],
) -> tuple[CandidateOutputScopeV1, dict[str, str]]:
    field_renames = {
        field_id: f"renamed:{index}:{field_id.rsplit(':', 1)[-1]}"
        for index, field_id in enumerate(sorted(source_values))
    }
    groups = tuple(
        replace(
            group,
            claim_group_id=renames.get(group.claim_group_id, group.claim_group_id),
            support_slot_ids=tuple(
                f"renamed-slot:{renames.get(group.claim_group_id, group.claim_group_id)}:{index}"
                for index, _slot_id in enumerate(group.support_slot_ids)
            ),
        )
        for group in scope.claim_groups
    )
    group_renames = {
        group.claim_group_id: renames.get(group.claim_group_id, group.claim_group_id)
        for group in scope.claim_groups
    }
    slots_by_group_index: dict[str, int] = {}
    slots = []
    for slot in scope.claim_slots:
        renamed_group = group_renames[slot.claim_group_id]
        index = slots_by_group_index.get(slot.claim_group_id, 0)
        slots_by_group_index[slot.claim_group_id] = index + 1
        slots.append(replace(
            slot,
            claim_group_id=renamed_group,
            claim_slot_id=f"renamed-slot:{renamed_group}:{index}",
            source_field_ids=tuple(field_renames[value] for value in slot.source_field_ids),
        ))
    renamed_scope = replace(scope, claim_groups=tuple(reversed(groups)), claim_slots=tuple(reversed(slots)))
    renamed_values = {field_renames[key]: value for key, value in source_values.items()}
    return renamed_scope, renamed_values


def test_witness_choice_is_independent_of_group_order_and_ids():
    definitions = [
        {"group_id": "core", "task_id": "core", "papers": ["paper-core"]},
        {
            "group_id": "z-witness",
            "task_id": "relation:best-content",
            "papers": ["paper-core", "paper-6"],
            "claim_seed": True,
            "relation_ids": ["relation-z"],
            "claim_hash": "best-content",
            "fields_per_slot": 3,
            "evidence_per_slot": 3,
            "field_values": {"paper-6": "precise source boundary, sample, and duration."},
        },
        {
            "group_id": "a-witness",
            "task_id": "relation:sparse-content",
            "papers": ["paper-core", "paper-6"],
            "claim_seed": True,
            "relation_ids": ["relation-a"],
            "claim_hash": "sparse-content",
            "fields_per_slot": 1,
            "evidence_per_slot": 1,
            "field_values": {"paper-6": "Brief source note."},
        },
    ]
    scope, values = _scope_with_groups(definitions)
    core_id = _group_id_for_task(scope, "core")
    measures: dict[str, int] = {}

    def measure(group_id: str) -> dict[str, Any]:
        measures[group_id] = measures.get(group_id, 0) + 1
        return {"input_tokens": 100, "output_tokens": 50, "within_context": True}

    _selected, witnesses = select_required_candidate_groups(
        scope, required_paper_keys=["paper-core", "paper-6"],
        core_group_ids=[core_id], required_comparison_group_ids=[],
        source_field_values=values, measure_group=measure, input_cap=500, output_cap=200,
    )
    winner = next(row for row in witnesses if row["paper_key"] == "paper-6")
    assert next(group for group in scope.claim_groups if group.claim_group_id == winner["claim_group_id"]).task_id == "relation:best-content"
    assert set(measures.values()) == {1}

    renamed, renamed_values = _rename_group_and_field_ids(
        scope,
        values,
        {"z-witness": "a-renamed", "a-witness": "z-renamed", "core": "core-renamed"},
    )
    renamed_core = _group_id_for_task(renamed, "core")
    renamed_measured: list[str] = []

    def measure_renamed(group_id: str) -> dict[str, Any]:
        renamed_measured.append(group_id)
        return {"input_tokens": 100, "output_tokens": 50, "within_context": True}

    renamed_selected, renamed_witnesses = select_required_candidate_groups(
        renamed, required_paper_keys=["paper-core", "paper-6"],
        core_group_ids=[renamed_core], required_comparison_group_ids=[],
        source_field_values=renamed_values, measure_group=measure_renamed,
        input_cap=500, output_cap=200,
    )
    renamed_witness = next(row for row in renamed_witnesses if row["paper_key"] == "paper-6")
    assert next(group for group in renamed.claim_groups if group.claim_group_id == renamed_witness["claim_group_id"]).task_id == "relation:best-content"
    assert set(renamed_selected) == {"core-renamed", "a-renamed"}
    assert "z-renamed" not in renamed_selected
    assert set(renamed_measured) == {"a-renamed", "z-renamed"}


def test_source_content_breaks_ties_before_adversarial_id_renaming():
    scope, values = _scope_with_groups([
        {"group_id": "core", "task_id": "core", "papers": ["paper-core"]},
        {
            "group_id": "alpha-source",
            "task_id": "relation:alpha-source",
            "papers": ["paper-core", "paper-6"],
            "relation_ids": ["relation-alpha-source"],
            "claim_seed": True,
            "task_contract": {
                "relation_type": "qualifies",
                "comparison_question": "Compare paper-core and paper-6 within their source boundaries.",
                "output_semantics": "bounded source comparison",
                "relation_id": "relation-alpha-source",
            },
            "claim_hash": hash_json({"structural_id": "alpha-source"}),
            "fields_per_slot": 2,
            "evidence_per_slot": 2,
            "field_values": {"paper-6": "The source reports a 30-day follow-up and a bounded sample."},
        },
        {
            "group_id": "omega-source",
            "task_id": "relation:omega-source",
            "papers": ["paper-core", "paper-6"],
            "relation_ids": ["relation-omega-source"],
            "claim_seed": True,
            "task_contract": {
                "relation_type": "qualifies",
                "comparison_question": "Compare paper-core and paper-6 within their source boundaries.",
                "output_semantics": "bounded source comparison",
                "relation_id": "relation-omega-source",
            },
            "claim_hash": hash_json({"structural_id": "omega-source"}),
            "fields_per_slot": 2,
            "evidence_per_slot": 2,
            "field_values": {"paper-6": "The source records a distinct 90-day sample boundary."},
        },
    ])
    core_id = _group_id_for_task(scope, "core")

    def select(current_scope, current_values):
        selected, witnesses = select_required_candidate_groups(
            current_scope,
            required_paper_keys=["paper-core", "paper-6"],
            core_group_ids=[core_id if current_scope is scope else "core-renamed"],
            required_comparison_group_ids=[],
            source_field_values=current_values,
            measure_group=lambda _group_id: {
                "input_tokens": 100, "output_tokens": 50, "within_context": True,
            },
            input_cap=500,
            output_cap=200,
        )
        witness = next(row for row in witnesses if row["paper_key"] == "paper-6")
        group = next(
            item for item in current_scope.claim_groups
            if item.claim_group_id == witness["claim_group_id"]
        )
        slot = next(
            item for item in current_scope.claim_slots
            if item.claim_group_id == group.claim_group_id and item.paper_key == "paper-6"
        )
        source_text = " | ".join(current_values[field_id] for field_id in slot.source_field_ids)
        return selected, source_text

    selected, selected_source_content = select(scope, values)
    selected_relation = next(
        group for group in scope.claim_groups if group.claim_group_id in selected and group.claim_seed
    )
    loser_relation = next(
        group for group in scope.claim_groups if group.claim_seed and group.claim_group_id != selected_relation.claim_group_id
    )
    renamed_ids = {
        selected_relation.claim_group_id: "z-content-winner",
        loser_relation.claim_group_id: "a-content-loser",
        core_id: "core-renamed",
    }
    relation_renames = {
        selected_relation.relation_ids[0]: "relation-z-content-winner",
        loser_relation.relation_ids[0]: "relation-a-content-loser",
    }
    field_renames = {
        field_id: f"renamed-field:{index}"
        for index, field_id in enumerate(sorted(values))
    }
    group_rows = []
    for group in scope.claim_groups:
        new_id = renamed_ids.get(group.claim_group_id, group.claim_group_id)
        relation_ids = tuple(relation_renames.get(value, value) for value in group.relation_ids)
        contract = dict(group.task_contract)
        if "relation_id" in contract:
            contract["relation_id"] = relation_renames.get(str(contract["relation_id"]), str(contract["relation_id"]))
        task_id = f"relation:{new_id}" if group.claim_seed else "core-renamed"
        group_rows.append(replace(
            group,
            claim_group_id=new_id,
            task_id=task_id,
            claim_hash=hash_json({"structural_id": new_id}),
            support_slot_ids=tuple(
                f"renamed-slot:{new_id}:{index}"
                for index, _value in enumerate(group.support_slot_ids)
            ),
            relation_ids=relation_ids,
            task_contract=tuple(sorted(contract.items())),
        ))
    group_id_map = {
        group.claim_group_id: renamed_ids.get(group.claim_group_id, group.claim_group_id)
        for group in scope.claim_groups
    }
    slot_indexes: dict[str, int] = {}
    slot_rows = []
    for slot in reversed(scope.claim_slots):
        old_group_id = slot.claim_group_id
        new_group_id = group_id_map[old_group_id]
        index = slot_indexes.get(old_group_id, 0)
        slot_indexes[old_group_id] = index + 1
        slot_rows.append(replace(
            slot,
            claim_group_id=new_group_id,
            claim_slot_id=f"renamed-slot:{new_group_id}:{index}",
            source_field_ids=tuple(field_renames[value] for value in slot.source_field_ids),
            relation_ids=tuple(relation_renames.get(value, value) for value in slot.relation_ids),
        ))
    renamed_scope = replace(
        scope,
        task_ids=tuple(sorted({group.task_id for group in group_rows})),
        selected_relation_ids=tuple(sorted(relation_renames.values())),
        claim_groups=tuple(reversed(group_rows)),
        claim_slots=tuple(slot_rows),
    )
    renamed_values = {field_renames[field_id]: value for field_id, value in values.items()}
    renamed_selected, renamed_source_content = select(renamed_scope, renamed_values)

    assert selected_source_content == renamed_source_content
    assert "z-content-winner" in renamed_selected
    assert "a-content-loser" not in renamed_selected


@pytest.mark.parametrize(
    ("required", "source_values", "metrics", "message"),
    [
        (["unknown-paper"], None, None, "has no source-closed group in the verified scope"),
        (["paper-6"], {}, None, "has no complete source-closed witness group"),
        (["paper-6"], None, {"input_tokens": 1000, "output_tokens": 1, "within_context": True}, "measured request caps"),
    ],
)
def test_unknown_or_unwitnessable_required_paper_is_actionable(
    required, source_values, metrics, message,
):
    scope, actual_values = _scope_with_groups([{
        "group_id": "only-witness",
        "task_id": "relation:paper-6",
        "papers": ["paper-6", "paper-c"],
        "claim_seed": True,
        "relation_ids": ["relation-p6"],
    }])
    measured: list[str] = []

    def measure(group_id: str) -> dict[str, Any]:
        measured.append(group_id)
        return metrics or {"input_tokens": 1, "output_tokens": 1, "within_context": True}

    with pytest.raises(CandidatePackingError, match=message):
        select_required_candidate_groups(
            scope, required_paper_keys=required, core_group_ids=[],
            required_comparison_group_ids=[],
            source_field_values=actual_values if source_values is None else source_values,
            measure_group=measure, input_cap=100, output_cap=100,
        )
    if required == ["unknown-paper"]:
        assert not measured


@pytest.mark.parametrize("compact", [False, True])
def test_candidate_output_estimate_includes_per_paper_role_shape(compact: bool):
    scope, _values = _scope_with_groups([{
        "group_id": "role-shape",
        "task_id": "topic:role-shape",
        "papers": ["paper-a", "paper-b"],
    }])
    observed: list[dict[str, Any]] = []
    contract = {
        "claim_prose_reserve_tokens": 7,
        "section_prose_reserve_tokens": 3,
    }
    if compact:
        contract["preferred_candidate_wire_version"] = "candidate-slot-output/v1"
    estimate = estimate_candidate_output(
        scope,
        {"candidate_id": "candidate_1", "output_contract": contract},
        lambda value: (observed.append(value) or 19),
    )
    expected_prose_reserve = 7 + 3 if compact else 7 * (1 + len(scope.claim_slots)) + 3
    assert estimate == 19 + expected_prose_reserve
    section = observed[0]["sections"][0]
    assert set(section["paper_roles"]) == {"paper-a", "paper-b"}
    assert all(isinstance(role, str) and role for role in section["paper_roles"].values())

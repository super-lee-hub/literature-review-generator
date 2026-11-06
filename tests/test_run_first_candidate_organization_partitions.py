from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from outline.candidate_organization_partitions import (
    CandidateOrganizationPartitionError,
    account_candidate_organization_partition_maps_v1,
    plan_candidate_organization_partitions_v1,
    project_candidate_organization_partition_v1,
)
from runtime.provider_runtime import hash_json
from tests.test_run_first_candidate_fragment_bank import (
    _build_bank,
    _comparison_row,
    _seed_group,
    _sealed_scope,
)


@pytest.fixture
def candidate_context(tmp_path):
    fixture = _sealed_scope(tmp_path)
    scope = fixture[-1]
    required_group = _seed_group(scope, "relation:required-ab")
    rows = [_comparison_row(required_group, "required")]
    _canonical, bank = _build_bank(
        fixture,
        comparison_rows=rows,
        required_group_ids=[required_group],
    )
    canonical_by_paper = {
        paper_key: next(
            entry["claim_group"]["claim_group_id"]
            for entry in bank["groups"]
            if entry["entry_kind"] == "canonical_claim"
            and entry["canonical_claim"].get("claim_type") == "empirical_finding"
            and paper_key in entry["claim_group"]["paper_keys"]
        )
        for paper_key in scope.paper_keys
    }
    return fixture, scope, bank, required_group, canonical_by_paper


def _plan(candidate_context, *, max_groups: int = 2, fits=None):
    _fixture, scope, bank, required_group, witnesses = candidate_context
    fit = fits or (lambda indices: len(indices) <= max_groups)
    return plan_candidate_organization_partitions_v1(
        bank,
        scope,
        required_group_ids=[required_group],
        required_paper_keys=list(witnesses),
        required_paper_witnesses=witnesses,
        fits_partition=fit,
    )


def test_singleton_capacity_retains_an_explicit_all_deferred_optional_leaf(tmp_path):
    fixture = _sealed_scope(tmp_path)
    full_scope = fixture[-1]
    required = _seed_group(full_scope, 'relation:required-ab')
    optional = _seed_group(full_scope, 'relation:optional-ab')
    scope = full_scope.for_groups([required, optional])
    _canonical, bank = _build_bank((*fixture[:-1], scope),
                                  comparison_rows=[_comparison_row(required, 'required')],
                                  required_group_ids=[required])
    plan = plan_candidate_organization_partitions_v1(
        bank, scope, required_group_ids=[required], required_paper_keys=list(scope.paper_keys),
        required_paper_witnesses={paper: required for paper in scope.paper_keys},
        fits_partition=lambda indices: len(indices) <= 1,
    )
    assert len(plan.partitions) == 2
    bindings = {'candidate_id': 'candidate-a', 'axis_hash': 'a' * 64, 'plan_hash': 'b' * 64,
                'bank_hash': plan.bank_hash, 'metadata_catalog_hash': 'c' * 64}
    maps = []
    for part in plan.partitions:
        if required in part.global_group_ids:
            maps.append(_leaf_map(bank, part, bindings))
        else:
            maps.append({**bindings, 'required_group_indices': [], 'required_paper_indices': [],
                         'sections': [], 'deferred': [{'reason_code': 'optional_unresolved', 'fragment_indices': [0]}]})
    accounting = account_candidate_organization_partition_maps_v1(bank, plan, maps, expected_bindings=bindings)
    optional_index = next(row['index'] for row in bank['groups'] if row['claim_group']['claim_group_id'] == optional)
    assert accounting.deferred_global_indices == (optional_index,)
    assert len(accounting.used_global_indices) == 1
    owner = next(i for i, part in enumerate(plan.partitions) if required in part.global_group_ids)
    malformed = deepcopy(maps)
    malformed[owner]['sections'] = []
    with pytest.raises(CandidateOrganizationPartitionError):
        account_candidate_organization_partition_maps_v1(bank, plan, malformed, expected_bindings=bindings)


def _leaf_map(bank, partition, bindings: dict[str, str]) -> dict[str, Any]:
    local_to_global = partition.local_to_global_group_indices
    used_local = [
        local
        for local, global_index in enumerate(local_to_global)
        if bank["groups"][global_index]["materialization_state"] == "materialized"
    ]
    deferred_local = [
        local
        for local, global_index in enumerate(local_to_global)
        if bank["groups"][global_index]["materialization_state"] == "unresolved"
    ]
    assert used_local, (
        "each provider organization leaf needs a materialized section anchor"
    )
    expected_papers = {
        str(slot["paper_key"])
        for local in used_local
        for slot in bank["groups"][local_to_global[local]]["support_slots"]
    }
    roles = []
    for paper_key in sorted(expected_papers):
        paper_index = partition.paper_keys.index(paper_key)
        role_local = next(
            local
            for local in used_local
            if paper_key
            in {
                str(slot["paper_key"])
                for slot in bank["groups"][local_to_global[local]]["support_slots"]
            }
        )
        roles.append([paper_index, role_local])
    required_group_local = [
        local
        for local, global_index in enumerate(local_to_global)
        if bank["groups"][global_index]["claim_group"]["claim_group_id"]
        in set(partition.required_group_ids)
    ]
    required_paper_local = [
        index
        for index, paper_key in enumerate(partition.paper_keys)
        if paper_key in {paper for paper, _group in partition.required_paper_witnesses}
    ]
    result = {
        **bindings,
        "required_group_indices": required_group_local,
        "required_paper_indices": required_paper_local,
        "sections": [
            {
                "section_index": 0,
                "metadata_index": 0,
                "paper_roles": roles,
                "fragment_indices": used_local,
            }
        ],
        "deferred": [],
    }
    if deferred_local:
        result["deferred"] = [
            {
                "reason_code": "optional_unresolved",
                "fragment_indices": deferred_local,
            }
        ]
    return result


def test_projection_keeps_the_single_bank_hash_and_exact_global_scope(
    candidate_context,
):
    _fixture, scope, bank, required_group, witnesses = candidate_context
    before = deepcopy(bank)
    required_index = next(
        row["index"]
        for row in bank["groups"]
        if row["claim_group"]["claim_group_id"] == required_group
    )
    witness_group = next(iter(witnesses.values()))
    witness_index = next(
        row["index"]
        for row in bank["groups"]
        if row["claim_group"]["claim_group_id"] == witness_group
    )
    projection = project_candidate_organization_partition_v1(
        bank,
        scope,
        [required_index, witness_index],
        partition_index=3,
        required_group_ids=[required_group, witness_group],
        required_paper_witnesses={next(iter(witnesses)): witness_group},
    )

    assert projection.bank_hash == bank["content_hash"]
    assert projection.partition_index == 3
    assert projection.global_group_indices == tuple(
        sorted([required_index, witness_index])
    )
    assert projection.local_to_global_group_indices == projection.global_group_indices
    assert projection.global_to_local_group_indices == tuple(
        (global_index, local_index)
        for local_index, global_index in enumerate(projection.global_group_indices)
    )
    assert {group.claim_group_id for group in projection.scope_subset.claim_groups} == {
        required_group,
        witness_group,
    }
    expected_slots = {
        slot["claim_slot_id"]
        for index in projection.global_group_indices
        for slot in bank["groups"][index]["support_slots"]
    }
    assert {
        slot.claim_slot_id for slot in projection.scope_subset.claim_slots
    } == expected_slots
    assert projection.bank_hash == before["content_hash"]
    assert bank == before
    assert "groups" not in projection.__dataclass_fields__


def test_planner_is_deterministic_whole_group_and_checks_every_axis_fit(
    candidate_context,
):
    _fixture, scope, bank, required_group, witnesses = candidate_context
    calls: list[tuple[int, ...]] = []
    # This callback stands in for the root's production serializer loop: both
    # candidate-axis estimates must fit the same unchanged cap for each probe.
    axis_costs = ({"fixed": 8, "per_group": 5}, {"fixed": 11, "per_group": 7})

    def exact_cross_axis_fit(indices: tuple[int, ...]) -> bool:
        calls.append(indices)
        return all(
            axis["fixed"] + axis["per_group"] * len(indices) <= 25
            for axis in axis_costs
        )

    plan = _plan(candidate_context, fits=exact_cross_axis_fit)
    second = _plan(candidate_context, fits=exact_cross_axis_fit)
    expected_group_ids = {
        row["claim_group"]["claim_group_id"] for row in bank["groups"]
    }
    assigned_group_ids = [group_id for group_id, _part in plan.group_assignment]

    assert plan.group_assignment == second.group_assignment
    assert tuple(part.global_group_indices for part in plan.partitions) == tuple(
        part.global_group_indices for part in second.partitions
    )
    assert set(assigned_group_ids) == expected_group_ids
    assert (
        len(assigned_group_ids) == len(set(assigned_group_ids)) == len(bank["groups"])
    )
    assert all(
        exact_cross_axis_fit(part.global_group_indices) for part in plan.partitions
    )
    assert all(
        any(
            bank["groups"][index]["materialization_state"] == "materialized"
            for index in part.global_group_indices
        )
        for part in plan.partitions
    )
    assert plan.required_group_ids == tuple(
        sorted({required_group, *witnesses.values()})
    )
    assert all(
        group_id in part.required_group_ids
        for group_id in plan.required_group_ids
        for part in plan.partitions
        if any(
            bank["groups"][index]["claim_group"]["claim_group_id"] == group_id
            for index in part.global_group_indices
        )
    )
    assert all(part.global_group_indices in calls for part in plan.partitions)
    # Exponential/binary prefix probes plus exact final checks stay well below
    # trying every prefix for every group.
    assert len(calls) < 5 * len(bank["groups"]) + 8


def test_planner_rejects_missing_foreign_and_unresolved_required_witnesses(
    candidate_context,
):
    _fixture, scope, bank, required_group, witnesses = candidate_context
    with pytest.raises(
        CandidateOrganizationPartitionError, match="missing required paper witness"
    ):
        plan_candidate_organization_partitions_v1(
            bank,
            scope,
            required_group_ids=[required_group],
            required_paper_keys=list(witnesses),
            required_paper_witnesses={},
            fits_partition=lambda _indices: True,
        )

    paper_key, canonical_group = next(iter(witnesses.items()))
    foreign_group = next(
        group.claim_group_id
        for group in scope.claim_groups
        if not group.claim_seed and paper_key not in group.paper_keys
    )
    with pytest.raises(
        CandidateOrganizationPartitionError, match="not owned by witness group"
    ):
        plan_candidate_organization_partitions_v1(
            bank,
            scope,
            required_group_ids=[required_group],
            required_paper_keys=[paper_key],
            required_paper_witnesses={paper_key: foreign_group},
            fits_partition=lambda _indices: True,
        )

    unresolved_group = next(
        row["claim_group"]["claim_group_id"]
        for row in bank["groups"]
        if row["materialization_state"] == "unresolved"
    )
    with pytest.raises(
        CandidateOrganizationPartitionError, match="unresolved before organization"
    ):
        plan_candidate_organization_partitions_v1(
            bank,
            scope,
            required_group_ids=[required_group, unresolved_group],
            required_paper_keys=[paper_key],
            required_paper_witnesses={paper_key: canonical_group},
            fits_partition=lambda _indices: True,
        )


def test_nonfitting_singleton_fails_before_a_leaf_can_be_emitted(candidate_context):
    fixture, scope, _bank, required_group, witnesses = candidate_context
    # Materialize both seeds so the first semantic item is a valid section anchor.
    optional_group = _seed_group(scope, "relation:optional-ab")
    _canonical, bank = _build_bank(
        fixture,
        comparison_rows=[
            _comparison_row(required_group, "required"),
            _comparison_row(optional_group, "optional"),
        ],
        required_group_ids=[required_group],
    )
    calls: list[tuple[int, ...]] = []

    def never_fits(indices: tuple[int, ...]) -> bool:
        calls.append(indices)
        return False

    with pytest.raises(CandidateOrganizationPartitionError, match="capacity"):
        plan_candidate_organization_partitions_v1(
            bank,
            scope,
            required_group_ids=[required_group],
            required_paper_keys=list(witnesses),
            required_paper_witnesses=witnesses,
            fits_partition=never_fits,
        )
    assert calls and len(calls[0]) == 1


def test_leaf_map_accounting_translates_local_indices_and_preserves_optional_deferral(
    candidate_context,
):
    _fixture, _scope, bank, required_group, _witnesses = candidate_context
    plan = _plan(candidate_context, max_groups=3)
    bindings = {
        "candidate_id": "candidate-a",
        "axis_hash": "a" * 64,
        "plan_hash": "b" * 64,
        "bank_hash": plan.bank_hash,
        "metadata_catalog_hash": "c" * 64,
    }
    leaf_maps = [_leaf_map(bank, part, bindings) for part in plan.partitions]
    before = deepcopy(bank)
    accounting = account_candidate_organization_partition_maps_v1(
        bank,
        plan,
        leaf_maps,
        expected_bindings=bindings,
    )

    expected_unresolved = {
        row["index"]
        for row in bank["groups"]
        if row["materialization_state"] == "unresolved"
    }
    assert accounting.bank_hash == plan.bank_hash
    assert set(accounting.used_global_indices) | set(
        accounting.deferred_global_indices
    ) == set(range(len(bank["groups"])))
    assert set(accounting.used_global_indices).isdisjoint(
        accounting.deferred_global_indices
    )
    assert set(accounting.deferred_global_indices) == expected_unresolved
    assert all(
        reason == "optional_unresolved"
        for _index, reason in accounting.deferred_reason_codes
    )
    assert all(
        section.global_fragment_indices
        and all(
            index in accounting.used_global_indices
            for index in section.global_fragment_indices
        )
        for section in accounting.sections
    )
    assert all(
        paper_key in bank["groups"][global_index]["claim_group"]["paper_keys"]
        for section in accounting.sections
        for paper_key, global_index in section.paper_roles
    )
    assert bank == before


def test_leaf_accounting_rejects_incomplete_partitions_missing_witness_and_bad_bindings(
    candidate_context,
):
    _fixture, _scope, bank, required_group, _witnesses = candidate_context
    plan = _plan(candidate_context, max_groups=3)
    bindings = {
        "candidate_id": "candidate-a",
        "axis_hash": "a" * 64,
        "plan_hash": "b" * 64,
        "bank_hash": plan.bank_hash,
        "metadata_catalog_hash": "c" * 64,
    }

    incomplete = [_leaf_map(bank, part, bindings) for part in plan.partitions]
    unresolved_leaf = next(
        index for index, item in enumerate(incomplete) if item["deferred"]
    )
    incomplete[unresolved_leaf]["deferred"] = []
    with pytest.raises(
        CandidateOrganizationPartitionError, match="partition its groups"
    ):
        account_candidate_organization_partition_maps_v1(
            bank,
            plan,
            incomplete,
            expected_bindings=bindings,
        )

    wrong_binding = [_leaf_map(bank, part, bindings) for part in plan.partitions]
    wrong_binding[-1]["axis_hash"] = "d" * 64
    with pytest.raises(CandidateOrganizationPartitionError, match="axis_hash"):
        account_candidate_organization_partition_maps_v1(
            bank,
            plan,
            wrong_binding,
            expected_bindings=bindings,
        )

    duplicate_partition = replace(
        plan,
        partitions=(*plan.partitions, plan.partitions[0]),
    )
    with pytest.raises(
        CandidateOrganizationPartitionError, match="each global group once"
    ):
        account_candidate_organization_partition_maps_v1(
            bank,
            duplicate_partition,
            [*wrong_binding, wrong_binding[0]],
            expected_bindings=bindings,
        )

    boolean_required_index = [
        _leaf_map(bank, part, bindings) for part in plan.partitions
    ]
    required_owner = next(
        index
        for index, part in enumerate(plan.partitions)
        if required_group in part.required_group_ids
    )
    required_indices = boolean_required_index[required_owner]["required_group_indices"]
    replacement = next(
        (index for index, value in enumerate(required_indices) if value > 0), None
    )
    assert replacement is not None
    required_indices[replacement] = True  # bool must not pass as the integer 1.
    with pytest.raises(
        CandidateOrganizationPartitionError, match="required_group_indices"
    ):
        account_candidate_organization_partition_maps_v1(
            bank,
            plan,
            boolean_required_index,
            expected_bindings=bindings,
        )

    deferred_required = [_leaf_map(bank, part, bindings) for part in plan.partitions]
    required_group_id = required_group
    required_owner = next(
        index
        for index, part in enumerate(plan.partitions)
        if required_group_id in part.required_group_ids
    )
    owner = plan.partitions[required_owner]
    local_index = next(
        local
        for local, global_index in enumerate(owner.local_to_global_group_indices)
        if bank["groups"][global_index]["claim_group"]["claim_group_id"]
        == required_group_id
    )
    section = deferred_required[required_owner]["sections"][0]
    if local_index in section["fragment_indices"]:
        section["fragment_indices"] = [
            index for index in section["fragment_indices"] if index != local_index
        ]
    selected_local = section["fragment_indices"]
    selected_global = [
        owner.local_to_global_group_indices[index] for index in selected_local
    ]
    expected_papers = {
        str(slot["paper_key"])
        for index in selected_global
        for slot in bank["groups"][index]["support_slots"]
    }
    section["paper_roles"] = [
        [
            owner.paper_keys.index(paper_key),
            next(
                local
                for local in selected_local
                if paper_key
                in {
                    str(slot["paper_key"])
                    for slot in bank["groups"][
                        owner.local_to_global_group_indices[local]
                    ]["support_slots"]
                }
            ),
        ]
        for paper_key in sorted(expected_papers)
    ]
    deferred_required[required_owner]["deferred"].append(
        {
            "reason_code": "not_selected_for_axis",
            "fragment_indices": [local_index],
        }
    )
    with pytest.raises(
        CandidateOrganizationPartitionError, match="required group .* was not selected"
    ):
        account_candidate_organization_partition_maps_v1(
            bank,
            plan,
            deferred_required,
            expected_bindings=bindings,
        )


def test_accounting_rejects_duplicate_role_text_across_leaves(candidate_context):
    fixture, scope, _bank, required_group, witnesses = candidate_context
    optional_group = _seed_group(scope, "relation:optional-ab")
    optional_row = _comparison_row(optional_group, "optional")
    required_row = _comparison_row(required_group, "required")
    optional_row["text"] = required_row["text"]
    _canonical, bank = _build_bank(
        fixture,
        comparison_rows=[required_row, optional_row],
        required_group_ids=[required_group],
    )
    plan = plan_candidate_organization_partitions_v1(
        bank,
        scope,
        required_group_ids=[required_group],
        required_paper_keys=list(witnesses),
        required_paper_witnesses=witnesses,
        fits_partition=lambda indices: len(indices) <= 1,
    )
    bindings = {
        "candidate_id": "candidate-collision",
        "axis_hash": "a" * 64,
        "plan_hash": "b" * 64,
        "bank_hash": plan.bank_hash,
        "metadata_catalog_hash": "c" * 64,
    }
    leaf_maps = [_leaf_map(bank, part, bindings) for part in plan.partitions]
    with pytest.raises(CandidateOrganizationPartitionError, match="role collision"):
        account_candidate_organization_partition_maps_v1(
            bank,
            plan,
            leaf_maps,
            expected_bindings=bindings,
        )


def test_projection_rejects_bool_duplicate_and_foreign_indices(candidate_context):
    _fixture, scope, bank, _required_group, _witnesses = candidate_context
    with pytest.raises(
        CandidateOrganizationPartitionError, match="invalid global group index"
    ):
        project_candidate_organization_partition_v1(bank, scope, [True])
    with pytest.raises(
        CandidateOrganizationPartitionError, match="repeats a global group index"
    ):
        project_candidate_organization_partition_v1(bank, scope, [0, 0])
    with pytest.raises(
        CandidateOrganizationPartitionError, match="invalid global group index"
    ):
        project_candidate_organization_partition_v1(bank, scope, [len(bank["groups"])])


def test_planning_does_not_mutate_bank_or_invent_any_claim_text(candidate_context):
    _fixture, _scope, bank, _required_group, _witnesses = candidate_context
    before = deepcopy(bank)
    before_hash = hash_json(bank)
    _plan(candidate_context)
    assert bank == before
    assert hash_json(bank) == before_hash

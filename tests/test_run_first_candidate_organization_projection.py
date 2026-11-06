from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from outline.candidate_fragment_bank import (
    CandidateFragmentBankError,
    build_candidate_organization_request_v1,
    compile_candidate_organization_section_v1,
    compile_candidate_organization_sections_v1,
    expand_candidate_organization_map_v1,
    validate_candidate_organization_map_for_request_v1,
)
from outline.candidate_organization_partitions import (
    CandidateOrganizationSectionChoiceV1,
)
from runtime.provider_runtime import hash_json
from tests.test_run_first_candidate_fragment_bank import (
    _build_bank,
    _complete_map,
    _comparison_row,
    _organization_context,
    _sealed_scope,
    _seed_group,
)


def _full_fixture(tmp_path):
    fixture = _sealed_scope(tmp_path)
    scope = fixture[-1]
    required = _seed_group(scope, "relation:required-ab")
    _canonical, bank = _build_bank(
        fixture,
        comparison_rows=[_comparison_row(required, "required")],
        required_group_ids=[required],
    )
    request, axis, plan, catalog = _organization_context(
        bank,
        scope,
        candidate_id="candidate-golden",
        required_group_ids=[required],
    )
    return fixture, scope, bank, required, request, axis, plan, catalog


@pytest.fixture
def candidate_context(tmp_path):
    return _full_fixture(tmp_path)


def _partition_descriptor(
    scope, bank, global_indices, *, partition_index=0, partition_count=2
):
    indices = sorted(global_indices)
    group_ids = [
        bank["groups"][index]["claim_group"]["claim_group_id"] for index in indices
    ]
    local_scope = scope.for_groups(group_ids, allowed_paper_keys=scope.paper_keys)
    return {
        "schema_version": "candidate-organization-partition/v1",
        "partition_index": partition_index,
        "partition_count": partition_count,
        "full_bank_hash": bank["content_hash"],
        "full_bank_group_count": len(bank["groups"]),
        "full_scope_hash": scope.content_hash,
        "local_scope_hash": local_scope.content_hash,
        "partition_plan_hash": hash_json({"global_group_indices_by_leaf": indices}),
        "local_to_global_group_indices": indices,
    }


def _required_leaf_request(candidate_context):
    _fixture, scope, bank, required, _full_request, axis, plan, catalog = (
        candidate_context
    )
    required_index = next(
        entry["index"]
        for entry in bank["groups"]
        if entry["claim_group"]["claim_group_id"] == required
    )
    empirical_witness = next(
        entry
        for entry in bank["groups"]
        if entry["entry_kind"] == "canonical_claim"
        and entry["canonical_claim"].get("claim_type") == "empirical_finding"
        and "paper-a" in entry["claim_group"]["paper_keys"]
    )
    witness_index = int(empirical_witness["index"])
    assert witness_index != required_index
    indices = sorted([required_index, witness_index])
    partition = _partition_descriptor(scope, bank, indices)
    request = build_candidate_organization_request_v1(
        bank,
        scope,
        axis=axis,
        plan=plan,
        required_group_ids=[required],
        required_paper_keys=["paper-a"],
        metadata_catalog=catalog,
        partition=partition,
    )
    return partition, request, indices


def _empty_deferred_map(request, reason: str) -> dict[str, object]:
    return {
        "schema_version": "candidate-organization-map/v1",
        "candidate_id": request["candidate_id"],
        "axis_hash": request["axis_hash"],
        "plan_hash": request["plan_hash"],
        "bank_hash": request["bank_hash"],
        "map_schema_hash": request["map_schema_hash"],
        "metadata_catalog_hash": request["metadata_catalog_hash"],
        "metadata_catalog_contract_hash": request["metadata_catalog_contract_hash"],
        "required_group_indices": request["required_group_indices"],
        "required_paper_indices": request["required_paper_indices"],
        "organization_contract_hash": request["organization_contract_hash"],
        "sections": [],
        "deferred": [
            {
                "reason_code": reason,
                "fragment_indices": list(range(request["bank_group_count"])),
            }
        ],
    }


def test_unpartitioned_request_matches_preprojection_golden(tmp_path):
    _fixture, _scope, _bank, _required, request, _axis, _plan, _catalog = _full_fixture(
        tmp_path
    )
    encoded = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert (
        hash_json(request)
        == "9b06807ebbd446b19a010b99dc481648d1b2830d4300d980f71c36199a28f31e"
    )
    assert (
        sha256(encoded).hexdigest()
        == "413d360d1171c5ad2a303705779b1fc7d7c4403a3d9b7e1c88d84f55e864e9af"
    )
    assert len(encoded) == 3462
    assert (
        request["map_schema_hash"]
        == "fcebff1b2e92310ad8b7269918c9d024a9bed6942f64d337121e640345eb8ef0"
    )
    assert (
        request["metadata_catalog_contract_hash"]
        == "c99703a3c2e803c6076107ed412c0ca010302e79395fe321af837aa1d759d8e2"
    )
    assert (
        request["organization_contract_hash"]
        == "fad6569d8a8f270414d655c06bf89a421c10aa6eb5e8df75f9f211cb02fee72f"
    )


def test_partition_request_projects_local_tables_without_changing_bank_or_provenance(
    candidate_context,
):
    _fixture, _scope, bank, required, _full, axis, plan, catalog = candidate_context
    bank_before = deepcopy(bank)
    partition, request, global_indices = _required_leaf_request(candidate_context)

    assert request["bank_hash"] == bank["content_hash"]
    assert (
        request["bank_group_count"]
        == len(global_indices)
        == len(request["fragment_cards"])
    )
    assert request["partition"] == partition
    assert request["partition"]["full_bank_group_count"] == len(bank["groups"])
    assert [card[0] for card in request["fragment_cards"]] == [0, 1]
    assert request["partition"]["local_to_global_group_indices"] == global_indices
    assert request["required_group_indices"] == [
        global_indices.index(
            next(
                entry["index"]
                for entry in bank["groups"]
                if entry["claim_group"]["claim_group_id"] == required
            )
        )
    ]
    assert request["required_paper_indices"] == [request["paper_keys"].index("paper-a")]
    assert (
        request["required_comparison_sections"][0]["group_index"]
        == request["required_group_indices"][0]
    )
    assert request["required_comparison_sections"][0]["paper_indices"] == [
        request["paper_keys"].index("paper-a"),
        request["paper_keys"].index("paper-b"),
    ]
    assert all(
        request["text_pool"][card[1]] == bank["groups"][global_indices[local]]["text"]
        for local, card in enumerate(request["fragment_cards"])
    )
    assert "support_slots" not in request
    assert "source_claim_ids" not in request
    assert "evidence_ids" not in request
    assert "source_field_ids" not in request
    assert "claim_slot_id" not in request
    assert bank == bank_before


def test_partition_map_uses_local_indices_and_compiles_exact_global_closure(
    candidate_context,
):
    fixture, scope, bank, required, _full, axis, plan, catalog = candidate_context
    partition, request, global_indices = _required_leaf_request(candidate_context)
    raw_map = _complete_map(request)
    validate_candidate_organization_map_for_request_v1(raw_map, request)

    expanded = expand_candidate_organization_map_v1(
        raw_map,
        bank,
        scope,
        [required],
        ["paper-a"],
        axis=axis,
        plan=plan,
        metadata_catalog=catalog,
        partition=partition,
    )
    section_choice = CandidateOrganizationSectionChoiceV1(
        partition_index=0,
        section_index=raw_map["sections"][0]["section_index"],
        metadata_index=raw_map["sections"][0]["metadata_index"],
        global_fragment_indices=tuple(
            global_indices[local]
            for local in raw_map["sections"][0]["fragment_indices"]
        ),
        paper_roles=tuple(
            (request["paper_keys"][paper_index], global_indices[role_local])
            for paper_index, role_local in raw_map["sections"][0]["paper_roles"]
        ),
    )
    merged = compile_candidate_organization_sections_v1(
        bank,
        scope,
        candidate_id=plan.candidate_id,
        section_choices=[section_choice],
        metadata_catalog=catalog,
        required_group_ids=[required],
        required_paper_keys=["paper-a"],
    )
    direct = compile_candidate_organization_section_v1(
        bank,
        candidate_id=plan.candidate_id,
        section_index=0,
        metadata_index=section_choice.metadata_index,
        global_fragment_indices=section_choice.global_fragment_indices,
        paper_roles=section_choice.paper_roles,
        metadata_catalog=catalog,
    )
    assert expanded == merged
    assert expanded["sections"][0] == direct
    support_rows = expanded["sections"][0]["claim_support"]
    expected_slots = {
        slot["claim_slot_id"]
        for index in global_indices
        for slot in bank["groups"][index]["support_slots"]
    }
    assert {row["claim_slot_id"] for row in support_rows} == expected_slots
    assert all(row["study_id"] == "" for row in support_rows)


@pytest.mark.parametrize(
    "mutation", ["duplicate", "boolean", "foreign", "wrong_bank", "wrong_scope"]
)
def test_partition_descriptor_rejects_stale_or_foreign_projection(
    candidate_context, mutation
):
    fixture, scope, bank, _required, _full, axis, plan, catalog = candidate_context
    partition, _request, global_indices = _required_leaf_request(candidate_context)
    changed = deepcopy(partition)
    if mutation == "duplicate":
        changed["local_to_global_group_indices"] = [
            global_indices[0],
            global_indices[0],
        ]
    elif mutation == "boolean":
        changed["local_to_global_group_indices"][0] = True
    elif mutation == "foreign":
        changed["local_to_global_group_indices"][0] = len(bank["groups"])
    elif mutation == "wrong_bank":
        changed["full_bank_hash"] = "0" * 64
    else:
        changed["full_scope_hash"] = "0" * 64
    with pytest.raises(CandidateFragmentBankError):
        build_candidate_organization_request_v1(
            bank,
            scope,
            axis=axis,
            plan=plan,
            required_group_ids=[],
            required_paper_keys=[],
            metadata_catalog=catalog,
            partition=changed,
        )


@pytest.mark.parametrize("materialized", [False, True])
def test_optional_only_leaf_can_return_empty_sections_only_when_fully_deferred(
    tmp_path, materialized
):
    fixture = _sealed_scope(tmp_path)
    scope = fixture[-1]
    required = _seed_group(scope, "relation:required-ab")
    optional = _seed_group(scope, "relation:optional-ab")
    rows = [_comparison_row(required, "required")]
    if materialized:
        rows.append(_comparison_row(optional, "optional"))
    _canonical, bank = _build_bank(
        fixture,
        comparison_rows=rows,
        required_group_ids=[required],
    )
    _full, _axis, _plan, _catalog = _organization_context(
        bank, scope, candidate_id="candidate-optional-leaf", required_group_ids=[]
    )
    axis = _axis
    plan = _plan
    catalog = _catalog
    optional_index = next(
        entry["index"]
        for entry in bank["groups"]
        if entry["claim_group"]["claim_group_id"] == optional
    )
    partition = _partition_descriptor(
        scope, bank, [optional_index], partition_index=1, partition_count=2
    )
    request = build_candidate_organization_request_v1(
        bank,
        scope,
        axis=axis,
        plan=plan,
        required_group_ids=[],
        required_paper_keys=[],
        metadata_catalog=catalog,
        partition=partition,
    )
    reason = "not_selected_for_axis" if materialized else "optional_unresolved"
    raw_map = _empty_deferred_map(request, reason)
    validate_candidate_organization_map_for_request_v1(raw_map, request)
    expanded = expand_candidate_organization_map_v1(
        raw_map,
        bank,
        scope,
        [],
        [],
        axis=axis,
        plan=plan,
        metadata_catalog=catalog,
        partition=partition,
    )
    assert expanded == {"candidate_id": "candidate-optional-leaf", "sections": []}


def test_full_bank_map_still_rejects_empty_sections(candidate_context):
    _fixture, _scope, bank, _required, full_request, _axis, _plan, _catalog = (
        candidate_context
    )
    empty_full_map = _empty_deferred_map(full_request, "not_selected_for_axis")
    # The unresolved optional comparison uses its one permitted reason code.
    unresolved = [
        card[0] for card in full_request["fragment_cards"] if card[3] == "unresolved"
    ]
    empty_full_map["deferred"] = []
    materialized = [
        card[0] for card in full_request["fragment_cards"] if card[3] == "materialized"
    ]
    if materialized:
        empty_full_map["deferred"].append(
            {
                "reason_code": "not_selected_for_axis",
                "fragment_indices": materialized,
            }
        )
    if unresolved:
        empty_full_map["deferred"].append(
            {
                "reason_code": "optional_unresolved",
                "fragment_indices": unresolved,
            }
        )
    with pytest.raises(CandidateFragmentBankError, match="requires nonempty sections"):
        validate_candidate_organization_map_for_request_v1(empty_full_map, full_request)


def test_partition_descriptor_tampering_invalidates_exact_map_binding(
    candidate_context,
):
    _fixture, _scope, _bank, _required, _full, _axis, _plan, _catalog = (
        candidate_context
    )
    _partition, request, _global_indices = _required_leaf_request(candidate_context)
    raw_map = _complete_map(request)
    tampered = deepcopy(request)
    tampered["partition"]["partition_plan_hash"] = "f" * 64
    with pytest.raises(
        CandidateFragmentBankError, match="organization request contract hash"
    ):
        validate_candidate_organization_map_for_request_v1(raw_map, tampered)

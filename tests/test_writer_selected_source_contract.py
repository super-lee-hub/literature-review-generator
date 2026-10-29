from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from services.citation_ref_catalog import build_document_ref_catalog
from services.writer_table_layout import bind_writer_table_layouts_v1
from services.writer_task_scope import (
    WRITER_SELECTED_SOURCE_CONTRACT,
    WriterTaskScopeError,
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
    verify_writer_selected_source_scope_v2,
)
from services.writer_wire_projection import (
    WriterWireProjectionError,
    expand_writer_source_bundles_for_test_v1,
    project_writer_scope_for_provider_v1,
)
from tests.test_writer_source_inventory import (
    PLANNED_CLAIM, PRIMARY_TEXT, SOURCE_SUMMARY, _registry_with_inventory,
)

V2 = WRITER_SELECTED_SOURCE_CONTRACT


def _case(tmp_path, *, changed=False):
    summaries = []
    for index in range(10):
        row = deepcopy(SOURCE_SUMMARY)
        row["paper_info"]["canonical_paper_key"] = f"paper-{index}"
        row["paper_info"]["title"] = f"Controlled treatment study {index}"
        summaries.append(row)
    if changed:
        qualifier = "The effect applies only to adults aged fifty or older."
        summaries[9]["ai_summary"]["core_analysis"]["limitations"] = [qualifier]
        summaries[9]["ai_summary"]["specialized_details"]["empirical"]["studies"][0]["limitations"] = [qualifier]
    registry, inventory, *_ = _registry_with_inventory(tmp_path, stage1_summaries=summaries, evidence_summaries=summaries)
    assert inventory.is_verified
    catalog = build_document_ref_catalog(summaries, project_name="revision", job_id=registry.job_id)
    return inventory, catalog, summaries


def _packet(inventory, summaries, indices):
    papers = inventory.paper_by_key()
    support, evidence_items, hashes, view_hashes = [], [], [], []
    for index in indices:
        paper = papers[f"paper-{index}"]
        unit = paper.units[0]
        primary = next(claim for claim in paper.claims if claim.claim_id in unit.claim_ids
                       and claim.claim_type == "empirical_finding" and claim.text == PRIMARY_TEXT)
        dependencies = [dep for dep in paper.interpretation_dependencies if dep.primary_claim_id == primary.claim_id]
        claims = sorted({primary.claim_id, *(key for dep in dependencies for key in dep.required_source_claim_ids)})
        evidence_ids = sorted({*primary.evidence_ids, *(key for dep in dependencies for key in dep.required_evidence_ids)})
        support.append({"claim": PLANNED_CLAIM, "paper_key": paper.paper_key, "study_id": unit.study_id,
                        "primary_claim_id": primary.claim_id, "source_claim_ids": claims,
                        "evidence_ids": evidence_ids, "source_field_ids": list(unit.source_field_ids)})
        evidence_items.append({"paper_key": paper.paper_key, "summary_hash": paper.source_summary_hash,
                               "view_hash": paper.evidence_view_hash,
                               "fields": {"findings": [PRIMARY_TEXT], "limitations": summaries[index]["ai_summary"]["core_analysis"]["limitations"]},
                               "source_fields": {}, "interpretation_context": []})
        hashes.append(paper.source_summary_hash)
        view_hashes.append(paper.evidence_view_hash)
    keys = [f"paper-{index}" for index in indices]
    return {"section_id": "incremental-U45" if indices == (4, 5) else "incremental-A9",
            "planned_claims": [PLANNED_CLAIM], "claim_support": support,
            "paper_keys": keys, "paper_roles": {key: "source_support" for key in keys},
            "must_use_paper_keys": keys, "section_goal": "Retain the complete reported source conditions.",
            "relation_ids": [], "relation_evidence": [], "source_summary_hashes": hashes,
            "evidence_view_hashes": view_hashes, "evidence_items": evidence_items,
            "retrieval_provenance": {"selection": "section_targeted", "paper_keys": keys, "view_hashes": view_hashes}}


def _scope(case, indices=(4, 5), version=V2):
    inventory, catalog, summaries = case
    return build_writer_task_scope_v1(_packet(inventory, summaries, indices), catalog,
        source_inventory=inventory, provider_semantic_contract_version=version)


def _wire(scope):
    return project_writer_scope_for_provider_v1(scope, provider_semantic_contract_version=V2)


def _output(scope):
    blocks, dispositions = [], []
    for task in scope["tasks"]:
        dispositions.append({"writer_task_id": task["writer_task_id"], "writer_task_basis_hash": scope["writer_task_basis_hash"],
                             "disposition": "covered"})
        for unit in task["output_units"]:
            if unit["required"]:
                blocks.append({"writer_task_id": task["writer_task_id"], "writer_output_unit_id": unit["writer_output_unit_id"],
                    "writer_task_basis_hash": scope["writer_task_basis_hash"],
                    "text": "The reported treatment effect retains its stated condition [[cite_ref:" + ",".join(unit["allowed_ref_ids"]) + "]]."})
    return {"blocks": blocks, "task_dispositions": dispositions}


def test_actual_complete_selected_wire_and_unit_ids_stay_equal_for_U45_and_change_for_A9(tmp_path):
    parent = _case(tmp_path / "parent")
    child = _case(tmp_path / "child", changed=True)
    parent_u, child_u = _scope(parent), _scope(child)
    assert parent_u["source_inventory_binding"] != child_u["source_inventory_binding"]
    assert hash_json(parent_u) != hash_json(child_u)  # Local authority remains global.
    assert parent_u["selected_source_binding"] == child_u["selected_source_binding"]
    assert parent_u["writer_task_basis_hash"] == child_u["writer_task_basis_hash"]
    assert _wire(parent_u) == _wire(child_u)  # Every actual scope wire key.
    assert json.dumps(_wire(parent_u), ensure_ascii=False, sort_keys=True) == json.dumps(_wire(child_u), ensure_ascii=False, sort_keys=True)
    assert hash_json(_wire(parent_u)) == hash_json(_wire(child_u))
    assert "source_inventory_binding" not in _wire(parent_u)
    assert parent_u["tasks"][0]["output_units"] == child_u["tasks"][0]["output_units"]
    parent_a, child_a = _scope(parent, (9,)), _scope(child, (9,))
    assert _wire(parent_a) != _wire(child_a)
    assert parent_a["writer_task_basis_hash"] != child_a["writer_task_basis_hash"]
    assert [u["writer_output_unit_id"] for u in parent_a["tasks"][0]["output_units"]] != [u["writer_output_unit_id"] for u in child_a["tasks"][0]["output_units"]]
    with pytest.raises(WriterTaskScopeError):
        validate_writer_task_output_v1(child_a, _output(parent_a), provider_semantic_contract_version=V2)


def test_selected_wire_roundtrips_full_crosspaper_source_and_validates_original_response_echo(tmp_path):
    parent = _scope(_case(tmp_path / "parent"))
    child = _scope(_case(tmp_path / "child", changed=True))
    restored = expand_writer_source_bundles_for_test_v1(_wire(parent), provider_semantic_contract_version=V2)
    assert restored == {task["writer_task_id"]: task["canonical_source_bundle"] for task in parent["tasks"]}
    assert {row["paper_key"] for bundle in restored.values() for row in bundle} == {"paper-4", "paper-5"}
    result = validate_writer_task_output_v1(child, _output(parent), provider_semantic_contract_version=V2)
    assert result["scope_status"] == "ready"
    assert result["source_inventory_binding"] == child["source_inventory_binding"]
    stale = _output(parent)
    stale["blocks"][0]["writer_task_basis_hash"] = "f" * 64
    with pytest.raises(WriterTaskScopeError, match="basis"):
        validate_writer_task_output_v1(child, stale, provider_semantic_contract_version=V2)


def test_context_only_paper_is_in_complete_selected_wire_scope_and_cannot_false_hit(tmp_path):
    results = []
    for changed in (False, True):
        inventory, catalog, summaries = _case(tmp_path / str(changed), changed=changed)
        packet = _packet(inventory, summaries, (4, 5, 9))
        packet["section_id"] = "context-U45-and9"
        packet["claim_support"] = packet["claim_support"][:2]
        scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory, provider_semantic_contract_version=V2)
        assert scope["tasks"][0]["paper_keys"] == ["paper-4", "paper-5"]
        assert scope["selected_source_binding"]["paper_keys"] == ["paper-4", "paper-5", "paper-9"]
        assert len(scope["selected_catalog_entries"]) == 3
        wire = _wire(scope)
        assert expand_writer_source_bundles_for_test_v1(wire, provider_semantic_contract_version=V2)
        results.append(wire)
    assert results[0] != results[1]


def test_full_packet_context_is_preserved_in_selected_basis(tmp_path):
    inventory, catalog, summaries = _case(tmp_path)
    packet = _packet(inventory, summaries, (4, 5))
    first = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory, provider_semantic_contract_version=V2)
    packet["explicit_extra_context"] = {"comparison_requirement": "Keep the null and reverse findings", "locator": "page:7/span:2"}
    second = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory, provider_semantic_contract_version=V2)
    assert second["source_bundle"]["explicit_extra_context"] == packet["explicit_extra_context"]
    assert first["writer_task_basis_hash"] != second["writer_task_basis_hash"]
    assert _wire(first) != _wire(second)


@pytest.mark.parametrize("mutation", ["foreign_claim", "missing_field", "missing_qualifier", "unsealed"])
def test_build_rejects_foreign_or_incomplete_canonical_membership(tmp_path, mutation):
    inventory, catalog, summaries = _case(tmp_path)
    packet = _packet(inventory, summaries, (4, 5))
    row = packet["claim_support"][0]
    if mutation == "foreign_claim": row["source_claim_ids"][0] = "foreign:source-claim"
    elif mutation == "missing_field": row["source_field_ids"] = []
    elif mutation == "missing_qualifier": row["source_claim_ids"] = [row["primary_claim_id"]]
    else: inventory = replace(inventory, _seal=None)
    with pytest.raises(WriterTaskScopeError, match="complete verified"):
        build_writer_task_scope_v1(packet, catalog, source_inventory=inventory, provider_semantic_contract_version=V2)


@pytest.mark.parametrize("mutation", ["missing_source_field", "locator", "qualifier", "catalog", "basis", "global_missing",
                                     "unknown_schema", "missing_marker", "task_order", "units_limit", "byte_limit"])
def test_scope_consumer_rejects_mutated_selected_sources_or_missing_current_authority(tmp_path, mutation):
    scope = _scope(_case(tmp_path))
    source = scope["tasks"][0]["canonical_source_bundle"][0]
    if mutation == "missing_source_field": source.pop("source_fields")
    elif mutation == "locator": source["source_fields"][0]["source_path"] += ".different"
    elif mutation == "qualifier": source["interpretation_dependencies"][0]["required_source_claim_ids"] = []
    elif mutation == "catalog": scope["selected_catalog_entries"][0]["canonical_paper_key"] = "paper-9"
    elif mutation == "basis": scope["writer_task_basis_hash"] = "f" * 64
    elif mutation == "global_missing": scope.pop("source_inventory_binding")
    elif mutation == "unknown_schema": scope["schema_version"] = "writer_task_scope/v3"
    elif mutation == "missing_marker": scope.pop("provider_semantic_contract_version")
    elif mutation == "task_order": scope["required_task_ids"] = ["foreign-task"]
    elif mutation == "units_limit": scope["max_output_units"] += 1
    else: scope["max_serialized_output_bytes_upper_bound"] += 1
    with pytest.raises(WriterWireProjectionError): _wire(scope)


@pytest.mark.parametrize("operation", ["project", "output", "expand"])
def test_explicit_contract_dispatch_cannot_silently_downgrade_selected_scope(tmp_path, operation):
    scope = _scope(_case(tmp_path))
    with pytest.raises((WriterTaskScopeError, WriterWireProjectionError), match="explicit"):
        if operation == "project": project_writer_scope_for_provider_v1(scope)
        elif operation == "output": validate_writer_task_output_v1(scope, _output(scope))
        else: expand_writer_source_bundles_for_test_v1(_wire(scope))
    with pytest.raises((WriterTaskScopeError, WriterWireProjectionError)):
        project_writer_scope_for_provider_v1(scope, provider_semantic_contract_version="unknown/v3")


def test_legacy_scope_does_not_gain_selected_contract_from_caller_argument(tmp_path):
    scope = _scope(_case(tmp_path), version=None)
    with pytest.raises(WriterWireProjectionError, match="marker"):
        _wire(scope)
    with pytest.raises(WriterTaskScopeError, match="marker"):
        validate_writer_task_output_v1(scope, _output(scope), provider_semantic_contract_version=V2)


def test_existing_table_binder_uses_selected_base_before_layout_and_response_validation(tmp_path):
    parent, child = _scope(_case(tmp_path / "parent")), _scope(_case(tmp_path / "child", changed=True))
    task = parent["tasks"][0]
    optional = next(unit for unit in task["output_units"] if not unit["required"])
    layout = {"schema_version": "writer_table_layout/v1", "table_id": "qualifier-table", "headers": ["Finding"],
              "rows": [{"row_id": "qualifier", "cells": [{"writer_task_id": task["writer_task_id"],
                         "writer_output_unit_id": optional["writer_output_unit_id"]}]}]}
    bound_parent = bind_writer_table_layouts_v1(parent, [layout], allowed_static_labels=["Finding"])
    bound_child = bind_writer_table_layouts_v1(child, [layout], allowed_static_labels=["Finding"])
    assert bound_parent["schema_version"] == "writer_task_scope/v2"  # Existing table schema.
    assert bound_parent["base_writer_task_basis_hash"] == parent["writer_task_basis_hash"]
    assert _wire(bound_parent) == _wire(bound_child)
    assert validate_writer_task_output_v1(bound_child, _output(bound_parent), provider_semantic_contract_version=V2)["scope_status"] == "ready"
    verify_writer_selected_source_scope_v2(bound_child, provider_semantic_contract_version=V2)
    broken = deepcopy(bound_child)
    broken["table_layouts"][0]["rows"][0]["cells"][0]["writer_output_unit_id"] = "foreign-unit"
    with pytest.raises(WriterWireProjectionError): _wire(broken)


def test_legacy_default_scope_and_wire_keep_prechange_golden_hashes(tmp_path):
    scope = _scope(_case(tmp_path), version=None)
    assert "provider_semantic_contract_version" not in scope
    assert "selected_source_binding" not in scope
    assert "selected_catalog_entries" not in scope
    assert "selected_context_sources" not in scope
    wire = project_writer_scope_for_provider_v1(scope)
    # Recorded from the unmodified HEAD producer using this real sealed fixture.
    assert compute_v3_hash(scope) == "1b0a9e3089b6cf3ed8d56712d803bdb0aa63c35f64cc60600dc7afbfae388e69"
    assert compute_v3_hash(wire) == "66eef6d521b99042751511c38bf136c1a4a96cdf321cce21570118302446e82f"

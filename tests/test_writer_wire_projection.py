from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from services import writer_wire_projection
from services.writer_task_scope import build_writer_task_scope_v1
from services.writer_wire_projection import (
    WriterWireProjectionError,
    expand_writer_source_bundles_for_test_v1,
    project_writer_scope_for_provider_v1,
)
from tests.test_writer_source_inventory import (
    PLANNED_CLAIM,
    _catalog,
    _registry_with_inventory,
)


def _scope_with_shared_sources(tmp_path: Path) -> dict:
    _, inventory, packet, _, _ = _registry_with_inventory(tmp_path)
    packet = deepcopy(packet)
    second_claim = "The outcome also improves under the reported study condition."
    packet["planned_claims"].append(second_claim)
    second_support = deepcopy(packet["claim_support"][0])
    second_support["claim"] = second_claim
    packet["claim_support"].append(second_support)
    scope = build_writer_task_scope_v1(packet, _catalog(), source_inventory=inventory)
    assert scope["scope_status"] == "ready"
    assert scope["usable_for_provider_admission"] is True
    assert len(scope["tasks"]) == 2
    assert all(task["planned_claim"] in {PLANNED_CLAIM, second_claim} for task in scope["tasks"])
    return scope


def test_module_import_is_from_the_checkout_under_test() -> None:
    expected = Path(__file__).resolve().parents[1] / "services" / "writer_wire_projection.py"
    assert Path(writer_wire_projection.__file__).resolve() == expected.resolve()


def test_projection_deduplicates_sources_and_roundtrips_full_task_closure(tmp_path: Path) -> None:
    scope = _scope_with_shared_sources(tmp_path)
    # Exercise JSON's easily lost falsey values through the content-addressed store.
    for task in scope["tasks"]:
        task["canonical_source_bundle"][0]["source_fields"][0]["wire_probe"] = {
            "zero": 0,
            "false": False,
        }

    projected = project_writer_scope_for_provider_v1(scope)
    assert projected["schema_version"] == "writer_task_scope_wire/v1"
    assert projected["required_task_ids"] == scope["required_task_ids"]
    assert projected["task_count"] == scope["task_count"]
    assert projected["source_inventory_binding"] == scope["source_inventory_binding"]
    assert projected["output_contract"] == scope["output_contract"]
    assert projected["source_identity_validation"] == scope["source_identity_validation"]
    assert "source_bundle" not in projected
    assert "active_ref_mapping" not in projected

    tasks_by_id = {task["writer_task_id"]: task for task in scope["tasks"]}
    projected_by_id = {task["writer_task_id"]: task for task in projected["tasks"]}
    assert set(projected_by_id) == set(tasks_by_id)
    for task_id, source_task in tasks_by_id.items():
        task = projected_by_id[task_id]
        assert task["planned_claim"] == source_task["planned_claim"]
        assert task["reason_codes"] == source_task["reason_codes"]
        assert task["source_claim_ids"] == source_task["source_claim_ids"]
        assert task["evidence_ids"] == source_task["evidence_ids"]
        assert task["source_field_ids"] == source_task["source_field_ids"]
        assert task["qualifier_source_claim_ids"] == source_task["qualifier_source_claim_ids"]
        assert task["qualifier_evidence_ids"] == source_task["qualifier_evidence_ids"]
        assert task["qualifier_source_field_ids"] == source_task["qualifier_source_field_ids"]
        assert task["allowed_ref_ids"] == source_task["allowed_ref_ids"]
        assert task["output_units"] == source_task["output_units"]
        for duplicated in (
            "support_rows",
            "packet_evidence_unverified",
            "canonical_source_bundle",
            "source_evidence",
        ):
            assert duplicated not in task

    total_refs = sum(
        len(refs)
        for task in projected["tasks"]
        for row in task["canonical_source_bundle_refs"]
        for key, refs in row.items()
        if key.endswith("_refs") and isinstance(refs, list)
    )
    assert len(projected["evidence_store"]) < total_refs
    expanded = expand_writer_source_bundles_for_test_v1(projected)
    assert expanded == {
        task["writer_task_id"]: task["canonical_source_bundle"]
        for task in scope["tasks"]
    }
    assert any(
        row.get("wire_probe") == {"zero": 0, "false": False}
        for bundle in expanded.values()
        for row in bundle[0]["source_fields"]
    )


def test_projection_reduces_exact_production_json_wire_bytes(tmp_path: Path) -> None:
    scope = _scope_with_shared_sources(tmp_path)
    projected = project_writer_scope_for_provider_v1(scope)
    # ReviewGenerationService._prompt uses this same JSON mode for prompt_text.
    def production_dumps(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")

    full_scope_bytes = len(production_dumps(scope))
    projected_scope_bytes = len(production_dumps(projected))
    assert projected_scope_bytes < full_scope_bytes
    assert full_scope_bytes - projected_scope_bytes >= 1000


def test_invalid_content_address_or_task_reference_is_rejected(tmp_path: Path) -> None:
    projected = project_writer_scope_for_provider_v1(_scope_with_shared_sources(tmp_path))
    bad_ref = deepcopy(projected)
    first_row = bad_ref["tasks"][0]["canonical_source_bundle_refs"][0]
    collection = next(key for key in first_row if key.endswith("_refs") and first_row[key])
    first_row[collection][0] = "source:missing"
    with pytest.raises(WriterWireProjectionError, match="invalid .* ref"):
        expand_writer_source_bundles_for_test_v1(bad_ref)

    tampered_entity = deepcopy(projected)
    store_key = next(iter(tampered_entity["evidence_store"]))
    tampered_entity["evidence_store"][store_key]["value"]["tampered"] = True
    with pytest.raises(WriterWireProjectionError, match="key is invalid"):
        expand_writer_source_bundles_for_test_v1(tampered_entity)


def test_unverified_or_incomplete_scope_never_projects(tmp_path: Path) -> None:
    scope = _scope_with_shared_sources(tmp_path)
    unverified = deepcopy(scope)
    unverified["usable_for_provider_admission"] = False
    with pytest.raises(WriterWireProjectionError, match="not verified and ready"):
        project_writer_scope_for_provider_v1(unverified)

    missing_task = deepcopy(scope)
    missing_task["tasks"].pop()
    with pytest.raises(WriterWireProjectionError, match="identities are missing"):
        project_writer_scope_for_provider_v1(missing_task)

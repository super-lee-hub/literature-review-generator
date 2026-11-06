from __future__ import annotations

from copy import deepcopy

import pytest

from outline.v3_models import compute_v3_hash
from services.writer_task_scope import build_writer_task_scope_v1, validate_writer_task_output_v1
from services.writer_wire_projection import WriterWireProjectionError, project_writer_scope_for_provider_v1
from tests.test_current_runtime_full_e2e import _source_bound_writer_response
from tests.test_writer_source_inventory import _catalog, _registry_with_inventory


def _source_scope(tmp_path, *, fieldless=False):
    _, inventory, packet, _, _ = _registry_with_inventory(tmp_path)
    catalog = _catalog()
    title = "A controlled study of treatment effects"
    catalog["entries"][0]["title"] = title
    if fieldless:
        paper = inventory.papers[0]
        claim = next(row for row in paper.claims if row.claim_type == "author_interpretation"
                     and row.owner_study_ids == ("",) and ":limitations:" in row.claim_id)
        text = f"{title} reports {claim.text}"
        packet["planned_claims"] = [text]
        packet["claim_support"] = [{
            "claim": text, "paper_key": paper.paper_key, "primary_claim_id": claim.claim_id,
            "source_claim_ids": [claim.claim_id], "evidence_ids": list(claim.evidence_ids),
            "source_field_ids": [],
        }]
    else:
        text = f"{title} reports {packet['planned_claims'][0]}"
        packet["planned_claims"] = [text]
        packet["claim_support"][0]["claim"] = text
    scope = build_writer_task_scope_v1(packet, catalog, source_inventory=inventory)
    assert scope["usable_for_provider_admission"] is True
    return scope, {"writer_task_scope": project_writer_scope_for_provider_v1(scope),
                   "citation_ref_catalog": catalog["entries"]}


def test_canonical_fieldless_paper_level_claim_reaches_writer_and_output_validation(tmp_path):
    scope, envelope = _source_scope(tmp_path, fieldless=True)
    task = scope["tasks"][0]
    assert task["source_field_ids"] == task["qualifier_source_field_ids"] == []
    trace = []
    response = _source_bound_writer_response(envelope, trace)
    assert response is not None
    assert validate_writer_task_output_v1(scope, response["content"])["scope_status"] == "ready"
    assert trace[0]["tasks"][0]["required_qualifier_ids"] == {
        "source_claim_ids": [], "evidence_ids": [], "source_field_ids": [],
    }


@pytest.mark.parametrize("field", ["source_claim_ids", "evidence_ids", "source_field_ids"])
def test_fixture_rejects_missing_qualifier_partition_with_intact_source_store(tmp_path, field):
    _, envelope = _source_scope(tmp_path)
    wire = envelope["writer_task_scope"]
    before = deepcopy(wire["evidence_store"])
    assert wire["tasks"][0][f"qualifier_{field}"]
    wire["tasks"][0][f"qualifier_{field}"] = []
    with pytest.raises(AssertionError, match="omitted canonical qualifier"):
        _source_bound_writer_response(envelope, [])
    assert wire["evidence_store"] == before


def test_fixture_rejects_unresolved_dependency_even_with_valid_content_address(tmp_path):
    _, envelope = _source_scope(tmp_path)
    wire = envelope["writer_task_scope"]
    row = wire["tasks"][0]["canonical_source_bundle_refs"][0]
    ref = row["interpretation_dependencies_refs"][0]
    entity = wire["evidence_store"].pop(ref)
    entity["value"]["scope"] = "unresolved"
    key = "source:" + compute_v3_hash(entity)
    wire["evidence_store"][key] = entity
    row["interpretation_dependencies_refs"][0] = key
    with pytest.raises(AssertionError, match="unresolved interpretation dependency"):
        _source_bound_writer_response(envelope, [])


def test_fixture_rejects_tampered_content_address(tmp_path):
    _, envelope = _source_scope(tmp_path)
    wire = envelope["writer_task_scope"]
    row = wire["tasks"][0]["canonical_source_bundle_refs"][0]
    ref = row["interpretation_dependencies_refs"][0]
    wire["evidence_store"][ref]["value"]["required_source_field_ids"] = []
    with pytest.raises(WriterWireProjectionError, match="key is invalid"):
        _source_bound_writer_response(envelope, [])

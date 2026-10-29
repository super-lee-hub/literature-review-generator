from __future__ import annotations

from copy import deepcopy

import pytest

from outline.semantic_final_fold_response import compact_final_fold_response, expand_final_fold_response
from outline.semantic_global_view import expand_global_retention_request, project_global_retention_request
from outline.semantic_request_wire import (
    WIRE_KEY,
    SemanticRequestWireError,
    expand_semantic_request_wire,
    pack_semantic_request_wire,
)
from tests.test_semantic_final_fold_response import _build_v2, _v2_case


def _case():
    items, full, ownership = _v2_case()
    catalog = _build_v2(items, full, ownership)
    cross = expand_final_fold_response(compact_final_fold_response(catalog), catalog)
    repeated = "仅适用于来源明确报告的样本；zero effect remains zero and uncertainty remains false. " * 3
    request = {
        "task": "substantive_global_synthesis",
        "topic_synthesis": [{"topic_id": "topic-0", "claims": [{"text": repeated}], "zero": 0, "uncertain": False, "empty": ""}],
        "cross_group_comparison": cross,
        "question": repeated,
    }
    request["cross_group_comparison"]["bridge_claims"][0]["text"] = repeated
    reference = {
        "dependency_kind": "local_job", "job_id": "local-job", "artifact_id": "retained-aggregate",
        "artifact_type": "outline_artifact", "path": "D:/owned/aggregate.json", "content_hash": "a" * 64,
    }
    return request, reference


def test_global_scientific_view_and_multisource_wire_restore_exact_source_science():
    request, reference = _case()
    original = deepcopy(request)
    view = project_global_retention_request(request, source_reference=reference)
    assert request == original
    sidecar = view["cross_group_comparison"]["fragment_aggregation"]
    assert not {"occurrences", "base_item_hashes", "manifest_hashes", "intermediate_item_hashes"}.intersection(sidecar)
    assert sidecar["source_reference"] == reference
    assert sidecar["source_dispositions"] == [{
        "disposition": entry["row"], "assigned_paper_ids": entry["assigned_paper_ids"],
        "assigned_fragment_count": len(entry["assigned_source_item_hashes"]),
    } for entry in request["cross_group_comparison"]["fragment_aggregation"]["occurrences"]]
    assert sidecar["question_disposition"] == "not_adjudicated_by_local_aggregation"
    for key in ("bridge_claims", "comparisons", "unresolved_questions"):
        assert view["cross_group_comparison"][key] == request["cross_group_comparison"][key]
    wire = pack_semantic_request_wire(view, source_keys=("topic_synthesis", "cross_group_comparison"))
    assert wire[WIRE_KEY]["schema_version"] == "semantic-source-tables-and-strings/v2"
    assert wire[WIRE_KEY]["source_keys"] == ["topic_synthesis", "cross_group_comparison"]
    decoded = expand_semantic_request_wire(wire)
    assert decoded == view
    assert decoded["topic_synthesis"][0]["zero"] == 0
    assert decoded["topic_synthesis"][0]["uncertain"] is False
    assert decoded["topic_synthesis"][0]["empty"] == ""
    assert expand_global_retention_request(decoded, source=request["cross_group_comparison"]) == request


@pytest.mark.parametrize("mutation", ["claim_prose", "claim_support", "fragment_state", "assigned_papers", "fragment_count", "logical_count", "ownership_hash", "source_changed", "boolean_as_integer", "integer_as_float"])
def test_global_scientific_view_rejects_changed_prose_support_and_fragment_scope(mutation):
    request, reference = _case()
    view = project_global_retention_request(request, source_reference=reference)
    source = deepcopy(request["cross_group_comparison"])
    cross = view["cross_group_comparison"]
    sidecar = cross["fragment_aggregation"]
    if mutation == "claim_prose":
        cross["bridge_claims"][0]["text"] = "Invented unqualified conclusion."
    elif mutation == "claim_support":
        cross["bridge_claims"][0]["source_claim_ids"] = []
    elif mutation == "fragment_state":
        sidecar["source_dispositions"][1]["disposition"]["status"] = "integrated"
    elif mutation == "assigned_papers":
        sidecar["source_dispositions"][0]["assigned_paper_ids"] = ["foreign-paper"]
    elif mutation == "fragment_count":
        sidecar["source_dispositions"][0]["assigned_fragment_count"] += 1
    elif mutation == "logical_count":
        cross["topic_dispositions"][0]["retained_fragment_counts"]["integrated_source_item_hashes"] += 1
    elif mutation == "ownership_hash":
        sidecar["technical_ownership_hash"] = "0" * 64
    elif mutation == "boolean_as_integer":
        assert sidecar["source_dispositions"][0]["disposition"]["uncertain"] is False
        sidecar["source_dispositions"][0]["disposition"]["uncertain"] = 0
    elif mutation == "integer_as_float":
        value = sidecar["source_dispositions"][0]["assigned_fragment_count"]
        assert type(value) is int
        sidecar["source_dispositions"][0]["assigned_fragment_count"] = float(value)
    else:
        source["unresolved_questions"].append("Changed original source")
    with pytest.raises(ValueError, match="changed"):
        expand_global_retention_request(view, source=source)


@pytest.mark.parametrize("mutation", ["source_keys_missing_cross", "source_keys_duplicate", "source_keys_foreign", "source_hash", "table_row", "string_reference"])
def test_multisource_wire_rejects_corrupt_source_fields_tables_and_identity(mutation):
    request, reference = _case()
    view = project_global_retention_request(request, source_reference=reference)
    wire = pack_semantic_request_wire(view, source_keys=("topic_synthesis", "cross_group_comparison"))
    metadata = wire[WIRE_KEY]
    if mutation == "source_keys_missing_cross":
        metadata["source_keys"] = ["topic_synthesis"]
    elif mutation == "source_keys_duplicate":
        metadata["source_keys"] = ["cross_group_comparison", "cross_group_comparison"]
    elif mutation == "source_keys_foreign":
        metadata["source_keys"] = ["question"]
    elif mutation == "source_hash":
        metadata["expanded_request_hash"] = "0" * 64
    elif mutation == "table_row":
        wire["cross_group_comparison"]["bridge_claims"]["semantic_row_table_v1"]["rows"][0][1].pop()
    else:
        wire["topic_synthesis"][0]["empty"] = metadata["prefix"] + "999999"
    with pytest.raises(SemanticRequestWireError):
        expand_semantic_request_wire(wire)


@pytest.mark.parametrize("mutation", ["foreign_dependency_kind", "wrong_type", "missing_hash", "missing_inventory", "already_projected"])
def test_global_view_requires_well_formed_local_authority_and_original_inventory(mutation):
    request, reference = _case()
    if mutation == "foreign_dependency_kind":
        reference["dependency_kind"] = "external_job"
    elif mutation == "wrong_type":
        reference["artifact_type"] = "raw_summary"
    elif mutation == "missing_hash":
        reference["content_hash"] = ""
    elif mutation == "missing_inventory":
        del request["cross_group_comparison"]["topic_dispositions"][0]["integrated_source_item_hashes"]
    else:
        request = project_global_retention_request(request, source_reference=reference)
    with pytest.raises(ValueError):
        project_global_retention_request(request, source_reference=reference)

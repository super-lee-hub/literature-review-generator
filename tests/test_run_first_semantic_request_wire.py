from __future__ import annotations

from copy import deepcopy

import pytest

from outline.semantic_request_wire import (
    WIRE_KEY,
    SemanticRequestWireError,
    expand_semantic_request_wire,
    pack_semantic_request_wire,
)
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json


def _request() -> dict:
    primary = "claim:paper-a:primary:" + "a" * 64
    qualifier = "claim:paper-a:qualifier:" + "b" * 64
    field_id = "source-field:" + "c" * 64
    evidence = "evidence:paper-a:primary:" + "d" * 64
    limit = "This result applies only to the recorded sample and 30-day follow-up."
    items = []
    for index in range(12):
        items.append({
            "topic_ids": [f"topic-{index}"],
            "semantic_result": {"bridge_claims": [{
                "claim_id": f"synthesis:cross_group_comparison:{index}",
                "paper_keys": ["paper-a", "paper-b"],
                "text": "A reports zero while B reports a reverse effect under their source boundaries.",
                "source_claim_ids": [primary, qualifier],
                "evidence_ids": [evidence],
                "source_field_ids": [field_id],
            }]},
            "interpretation_context": {
                "fields": [{
                    "source_field_id": field_id, "source_path": "methods.sample",
                    "source_value": limit, "scope": "paper", "study_id": "",
                    "paper_key": "paper-a", "interpretation_required": True,
                    "source_summary_hash": "e" * 64,
                    "extra": {"zero": 0, "false": False, "none": None, "empty": []},
                    **({"optional": ""} if index % 2 else {}),
                }],
                "dependencies": [{
                    "primary_claim_id": primary, "primary_evidence_ids": [evidence],
                    "required_source_claim_ids": [qualifier], "required_evidence_ids": [evidence],
                    "required_source_field_ids": [field_id], "scope": "paper", "owner_study_id": "",
                }],
            },
        })
    return {"task": "substantive_cross_group_comparison", "topic_synthesis": items,
            "questions": ["Retain both source sides and their conditions."],
            "output_contract": {"bridge_claims": "original canonical IDs"}}


def test_tables_and_strings_roundtrip_source_closure_prose_and_falsey_values() -> None:
    request = _request()
    before = deepcopy(request)
    wire = pack_semantic_request_wire(request)
    assert request == before
    assert expand_semantic_request_wire(wire) == request
    assert wire["questions"] == request["questions"]
    assert wire["output_contract"] == request["output_contract"]
    assert wire[WIRE_KEY]["expanded_request_hash"] == hash_json(before)
    profile = ProviderContextProfile.conservative(
        provider="generic", model="fixture", endpoint_type="chat_completions",
    )
    assert profile.estimate_tokens(wire) < profile.estimate_tokens(request)
    restored = expand_semantic_request_wire(wire)["topic_synthesis"]
    assert "optional" not in restored[0]["interpretation_context"]["fields"][0]
    assert restored[1]["interpretation_context"]["fields"][0]["optional"] == ""
    assert restored[0]["interpretation_context"]["fields"][0]["extra"] == {
        "zero": 0, "false": False, "none": None, "empty": [],
    }


def test_marker_collision_uses_a_distinct_exact_string_namespace() -> None:
    request = _request()
    request["topic_synthesis"][0]["literal"] = "$S123 is a literal source string"
    wire = pack_semantic_request_wire(request)
    assert wire[WIRE_KEY]["prefix"] != "$S"
    assert expand_semantic_request_wire(wire) == request


@pytest.mark.parametrize("fault", ["text", "index", "bool_index", "width", "duplicate_column", "unknown_marker", "unused_string"])
def test_corrupt_text_row_identity_and_string_references_reject(fault: str) -> None:
    wire = pack_semantic_request_wire(_request())
    table = wire["topic_synthesis"][0]["interpretation_context"]["fields"]["semantic_row_table_v1"]
    if fault == "text":
        wire[WIRE_KEY]["strings"][0] += " altered"
    elif fault == "index":
        table["rows"][0][0] = 999
    elif fault == "bool_index":
        table["rows"][0][0] = False
    elif fault == "width":
        table["rows"][0][1].pop()
    elif fault == "duplicate_column":
        table["columns"][0].append(table["columns"][0][0])
    elif fault == "unknown_marker":
        wire["topic_synthesis"][0]["literal"] = wire[WIRE_KEY]["prefix"] + "999999"
    else:
        wire[WIRE_KEY]["strings"].append("z" * 100)
    with pytest.raises(SemanticRequestWireError):
        expand_semantic_request_wire(wire)


def test_empty_inputs_remain_literal_and_encoded_inputs_cannot_be_repacked() -> None:
    request = {"topic_synthesis": [], "zero": 0}
    assert pack_semantic_request_wire(request) == request
    wire = pack_semantic_request_wire(_request())
    with pytest.raises(SemanticRequestWireError, match="already"):
        pack_semantic_request_wire(wire)

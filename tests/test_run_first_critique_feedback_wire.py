"""Opt-in feedback tables preserve complete literal critique authority."""
import hashlib
import json
from copy import deepcopy

import pytest

from outline.candidate_arbitration_ledger_packing import (
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from outline.critique_coverage_wire import (
    FEEDBACK_SCHEMA,
    WIRE_KEY,
    CritiqueCoverageWireError,
    expand_critique_coverage_certificates,
    pack_critique_coverage_certificates,
)
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from tests.test_run_first_critique_coverage_wire import _request


def _ordered(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bind(request):
    ledger = expand_arbitration_final_ledger(request["arbitration_evaluation_ledger"])
    ledger["critique_hashes"] = {key: hash_json(value) for key, value in request["critiques"].items()}
    ledger["ledger_hash"] = compute_v3_hash({key: value for key, value in ledger.items() if key != "ledger_hash"})
    request["arbitration_evaluation_ledger"] = pack_arbitration_final_ledger(ledger)
    return request


def _rich_request():
    request = _request()
    rows = request["critiques"]["evidence_critique"]["candidate_shard_results"]
    first = next(iter(rows.values()))
    first.update(uncertain=False, zero=0, empty="", unknown={"z": 0, "a": False, "nested": [[], {}, None, ""]})
    duplicate = deepcopy(first)
    duplicate.pop("claim_support_coverage", None)
    rows["literal-key:repeat:1"] = duplicate
    rows["literal-key:repeat:2"] = deepcopy(duplicate)
    distinct = deepcopy(duplicate)
    distinct["feedback"] = "A distinct scientific limitation; preserve this exact wording."
    rows["literal-key:distinct"] = distinct
    return _bind(request)


def _rehash(request):
    wire = request[WIRE_KEY]
    wire["feedback_encoded_hash"] = hash_json(request["critiques"])
    wire["wire_hash"] = hash_json({key: value for key, value in wire.items() if key != "wire_hash"})


def _table(request):
    return request["critiques"]["evidence_critique"]["candidate_shard_results"]["$cfr"]


def test_default_v1_bytes_match_preserved_am646_golden():
    packed = pack_critique_coverage_certificates(_request())
    assert hashlib.sha256(_ordered(packed).encode()).hexdigest() == "51bd2e796362aae0f7cc2decf48f31216d679aa4f434c10003852d7bdd2de09c"
    assert packed[WIRE_KEY]["schema_version"] == "outline_critique_coverage_wire/v1"


def test_feedback_catalog_preserves_distinct_notes_repeated_rows_types_and_key_order():
    source = _rich_request()
    before = _ordered(source)
    packed = pack_critique_coverage_certificates(source, compact_feedback=True)
    assert _ordered(source) == before
    assert packed[WIRE_KEY]["schema_version"] == FEEDBACK_SCHEMA
    assert "keys" in _table(packed)
    assert "keys_mode" not in _table(packed)
    restored = expand_critique_coverage_certificates(packed)
    assert _ordered(restored) == before
    rows = restored["critiques"]["evidence_critique"]["candidate_shard_results"]
    assert _ordered(rows["literal-key:repeat:1"]) == _ordered(rows["literal-key:repeat:2"])
    assert rows["literal-key:repeat:1"]["feedback"] != rows["literal-key:distinct"]["feedback"]
    assert rows["literal-key:repeat:1"]["uncertain"] is False
    assert type(rows["literal-key:repeat:1"]["zero"]) is int
    assert pack_critique_coverage_certificates(packed, compact_feedback=True) == packed


def test_exact_derived_record_keys_and_array_occurrences_roundtrip():
    request = _request()
    derived = pack_critique_coverage_certificates(request, compact_feedback=True)
    assert _table(derived)["keys_mode"] == "candidate_id:shard:shard_index"
    assert "keys" not in _table(derived)
    assert _ordered(expand_critique_coverage_certificates(derived)) == _ordered(request)
    row = deepcopy(next(iter(request["critiques"]["evidence_critique"]["candidate_shard_results"].values())))
    row.pop("claim_support_coverage", None)
    request["critiques"]["evidence_critique"]["candidate_shard_results"] = [row, deepcopy(row)]
    _bind(request)
    packed = pack_critique_coverage_certificates(request, compact_feedback=True)
    assert _table(packed)["kind"] == "array"
    assert _ordered(expand_critique_coverage_certificates(packed)) == _ordered(request)


def test_empty_feedback_and_unknown_nonrow_fields_stay_literal():
    request = {"critiques": {"future": {"candidate_shard_results": {}, "new_field": [False, 0, {}, []]}}, "other": "unchanged"}
    assert _ordered(pack_critique_coverage_certificates(request, compact_feedback=True)) == _ordered(request)


def test_feedback_wire_survives_transport_json_object_key_sorting():
    source = _rich_request()
    packed = pack_critique_coverage_certificates(source, compact_feedback=True)
    transported = json.loads(json.dumps(packed, ensure_ascii=False, sort_keys=True))
    restored = expand_critique_coverage_certificates(transported)
    assert hash_json(restored) == hash_json(source)
    assert json.dumps(restored, sort_keys=True) == json.dumps(source, sort_keys=True)


def test_sorted_wire_reaches_the_real_arbitration_response_fixture():
    from tests.test_current_runtime_full_e2e import _outline_provider_response

    source = _rich_request()
    source["hierarchy"] = {"level": "all_candidate_final_arbitration"}
    packed = pack_critique_coverage_certificates(source, compact_feedback=True)
    transported = json.loads(json.dumps(packed, ensure_ascii=False, sort_keys=True))
    response = _outline_provider_response("arbitration", transported)
    assert response["status"] == "success"
    assert response["content"]["selected_candidate_id"] == source["candidate_ids"][0]


def test_sorted_feedback_wire_completes_one_real_loopback_http_arbitration():
    import urllib.request

    from tests.test_run_first_scale_integration import _start_scale_server

    source = _rich_request()
    source["hierarchy"] = {"level": "all_candidate_final_arbitration"}
    packed = pack_critique_coverage_certificates(source, compact_feedback=True)
    body = json.dumps({
        "model": "rfs-outline-local", "max_tokens": 2048,
        "messages": [{"role": "user", "content": json.dumps({
            "node_id": "arbitration:final:feedback-wire-smoke", "request": packed,
        }, ensure_ascii=False, sort_keys=True)}],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    server, thread, api_base = _start_scale_server([])
    try:
        request = urllib.request.Request(
            f"{api_base}/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            result = json.loads(json.loads(response.read())["choices"][0]["message"]["content"])
        assert result["selected_candidate_id"] == source["candidate_ids"][0]
        assert len(server.calls) == 1
        assert server.calls[0]["status"] == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("mutation", [
    "layout_bool", "layout_unknown", "cell_bool", "cell_float", "cell_unknown", "width",
    "unused_layout", "unused_value", "duplicate_value", "field_missing", "unknown_table_field",
    "record_key", "source_prose", "false_to_zero", "source_hash", "ledger_hash", "wire_hash",
    "catalog_row_marker", "nested_wire_v2", "source_field_missing",
])
def test_feedback_tampering_never_becomes_valid_critique_authority(mutation):
    packed = pack_critique_coverage_certificates(_rich_request(), compact_feedback=True)
    wire, table = packed[WIRE_KEY], _table(packed)
    if mutation == "layout_bool":
        table["rows"][0][0] = False
    elif mutation == "layout_unknown":
        table["rows"][0][0] = len(wire["layouts"])
    elif mutation == "cell_bool":
        table["rows"][0][1][0] = False
    elif mutation == "cell_float":
        table["rows"][0][1][0] = float(table["rows"][0][1][0])
    elif mutation == "cell_unknown":
        table["rows"][0][1][0] = len(wire["values"])
    elif mutation == "width":
        table["rows"][0][1].pop()
    elif mutation == "unused_layout":
        wire["layouts"].append(["new-unused-field"])
    elif mutation == "unused_value":
        wire["values"].append({"new-unused-source": "unchanged scientific source remains required"})
    elif mutation == "duplicate_value":
        wire["values"].append(deepcopy(wire["values"][0]))
    elif mutation == "field_missing":
        wire.pop("feedback_source_hash")
    elif mutation == "unknown_table_field":
        table["skip_feedback"] = True
    elif mutation == "record_key":
        table["keys"][0] = "foreign-key"
    elif mutation == "source_prose":
        index = next(i for i, value in enumerate(wire["values"]) if isinstance(value, str) and value.startswith("A distinct"))
        wire["values"][index] = "Changed scientific boundary."
    elif mutation == "false_to_zero":
        index = next(i for i, value in enumerate(wire["values"]) if value is False)
        wire["values"][index] = 0
    elif mutation == "source_hash":
        wire["feedback_source_hash"] = "f" * 64
    elif mutation == "ledger_hash":
        packed["arbitration_evaluation_ledger"]["critique_hashes"]["evidence_critique"] = "f" * 64
    elif mutation == "catalog_row_marker":
        wire["values"][0] = {"$cfr": {"kind": "array", "rows": [[0, [0]]]}}
    elif mutation == "nested_wire_v2":
        wire["coverage_wire"] = {"schema_version": FEEDBACK_SCHEMA}
    elif mutation == "source_field_missing":
        layout_index = table["rows"][0][0]
        column = wire["layouts"][layout_index].index("feedback")
        wire["layouts"][layout_index].pop(column)
        for row in table["rows"]:
            if row[0] == layout_index:
                row[1].pop(column)
    if mutation != "wire_hash":
        _rehash(packed)
    else:
        wire["wire_hash"] = "f" * 64
    with pytest.raises((CritiqueCoverageWireError, ValueError)):
        expand_critique_coverage_certificates(packed)


def test_source_marker_collision_is_rejected_without_mutation():
    request = _rich_request()
    request["critiques"]["evidence_critique"]["new_unknown_field"] = {"$cfr": []}
    before = _ordered(request)
    with pytest.raises(CritiqueCoverageWireError, match="collides"):
        pack_critique_coverage_certificates(request, compact_feedback=True)
    assert _ordered(request) == before


def test_full_critique_hashes_cannot_be_detached_from_the_canonical_ledger():
    packed = pack_critique_coverage_certificates(_rich_request(), compact_feedback=True)
    packed[WIRE_KEY]["original_critique_hashes"]["evidence_critique"] = "f" * 64
    _rehash(packed)
    with pytest.raises(CritiqueCoverageWireError, match="canonical arbitration ledger"):
        expand_critique_coverage_certificates(packed)

"""Lossless identity/string wire projection for R4 assessment and reduction."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from outline.arbitration_assessment_wire import (
    _REDUCER_GROUP_BINDING_COLUMNS,
    _REDUCER_GROUP_BINDING_FIELDS,
    _REDUCER_GROUP_BINDINGS_MARKER,
    DECISION_VIEW_REFERENCE_SCHEMA,
    ROW_TABLE_MARKER,
    WIRE_KEY,
    WIRE_SCHEMA,
    ArbitrationAssessmentWireError,
    _compact_candidate_decision_view_rows,
    _compact_reducer_group_bindings,
    _expand_candidate_decision_view_rows,
    _expand_reducer_group_bindings,
    _expand_response_global_layout_tables,
    _expand_row_tables,
    _pack_row_tables,
    _transform_typed,
    expand_assessment_wire_request,
    expand_assessment_wire_response,
    project_assessment_wire,
    project_assessment_wire_response,
)
from outline.arbitration_response_row_defaults import (
    COMPACT_VARIABLE_COLUMNS,
    RESPONSE_ROW_TABLE_SCHEMA_V1,
    RESPONSE_ROW_TABLE_SCHEMA_V2,
)
from outline.candidate_arbitration_aggregation import (
    ASSESSMENT_INPUT_SCHEMA,
    ASSESSMENT_RESPONSE_SCHEMA,
    REDUCTION_INPUT_SCHEMA,
    ArbitrationAggregationError,
    build_arbitration_reducer_request,
    build_overflow_assessment_plan,
    validate_arbitration_assessment_response,
    validate_arbitration_reduction_response,
)
from outline.semantic_request_wire import expand_semantic_request_wire
from outline.v3_models import compute_v3_hash
from tests.test_run_first_arbitration_aggregation import (
    _build_ready,
    _caps,
    _critical_issues,
    _input_estimator,
    _legacy_plan_and_ledger,
    _output_estimator,
    _prepared_legacy_source_request,
    _refresh_group_contributions,
    _typed_leaf_response,
    _typed_reduction_response,
)

_NODE = "arbitration-assessment:leaf-0001"
_CANDIDATE = "candidate-abcdefghijklmnopqrstuvwxyz"
_CANDIDATE_HASH = "a" * 64
_UNIT = "section-unit:abcdefghijklmnopqrstuvwxyz"
_UNIT_HASH = "b" * 64
_UNIT2 = "section-unit:bcdefghijklmnopqrstuvwxyza"
_UNIT_HASH2 = "9" * 64
_GROUP = "topic-group:abcdefghijklmnopqrstuvwxyz"
_GROUP2 = "topic-group:bcdefghijklmnopqrstuvwxyza"
_VIEW = "decision-view:abcdefghijklmnopqrstuvwxyz"
_VIEW_HASH = "c" * 64
_VIEW2 = "decision-view:bcdefghijklmnopqrstuvwxyza"
_VIEW_HASH2 = "8" * 64
_SOURCE_CLAIM = "claim:source-abcdefghijklmnopqrstuvwxyz"
_SOURCE_EVIDENCE = "evidence:source-abcdefghijklmnopqrstuvwxyz"
_SOURCE_FIELD = "source-field:source-abcdefghijklmnopqrstuvwxyz"
_PAPER = "paper:source-abcdefghijklmnopqrstuvwxyz"
_LITERAL = (
    "The reported estimate applies only to the observed controlled sample and "
    "the 30-day follow-up; it does not establish a longer-term effect."
)
_UNIQUE_LITERAL = "A unique exact source note that appears once in this request."


def _sample_request() -> dict:
    view_one = {
        "candidate_id": _CANDIDATE,
        "decision_view_id": _VIEW,
        "decision_view_hash": _VIEW_HASH,
        "literal_view_ref_ids": [_SOURCE_CLAIM, _SOURCE_EVIDENCE, _SOURCE_FIELD],
        "source_bindings": {
            "paper_key": _PAPER,
            "source_claim_ids": [_SOURCE_CLAIM],
            "evidence_ids": [_SOURCE_EVIDENCE],
            "source_field_ids": [_SOURCE_FIELD],
        },
        "literal_critical_detail": {
            "source_claims": [{"claim_id": _SOURCE_CLAIM, "text": _LITERAL}],
            "evidence": [{"evidence_id": _SOURCE_EVIDENCE, "text": _LITERAL}],
            "source_fields": [{"source_field_id": _SOURCE_FIELD, "source_value": _LITERAL}],
        },
        "literal_qualifier_values": [{"literal_value": _UNIQUE_LITERAL}],
    }
    view_two = {
        **copy.deepcopy(view_one),
        "decision_view_id": _VIEW2,
        "decision_view_hash": _VIEW_HASH2,
        "literal_qualifier_values": [{"literal_value": "A distinct second source condition."}],
    }
    return {
        "schema_version": ASSESSMENT_INPUT_SCHEMA,
        "node_id": _NODE,
        "candidate_ids": [_CANDIDATE],
        "candidate_hashes": {_CANDIDATE: _CANDIDATE_HASH},
        "unit_ids": [_UNIT, _UNIT2],
        "review_group_map": [{
            "group_id": _GROUP,
            "group_kind": "task_topic",
            "source_scope_ids": [_GROUP],
            "used_assertion_ids": ["synthesis:claim:abcdefghijklmnopqrstuvwxyz"],
        }, {
            "group_id": _GROUP2,
            "group_kind": "task_topic",
            "source_scope_ids": [_GROUP2],
            "used_assertion_ids": ["synthesis:claim:bcdefghijklmnopqrstuvwxyza"],
        }, {
            "group_id": "issue:candidate",
            "group_kind": "typed_issue",
            "issue_id": "issue:candidate",
            "issue_scope": "candidate",
            "source_scope_ids": [_CANDIDATE],
        }, {
            "group_id": "issue:claim",
            "group_kind": "typed_issue",
            "issue_id": "issue:claim",
            "issue_scope": "claim",
            "source_scope_ids": ["claim:source-abcdefghijklmnopqrstuvwxyz"],
        }],
        "candidate_units": [{
            "candidate_id": _CANDIDATE,
            "candidate_hash": _CANDIDATE_HASH,
            "unit_rows": [{
                "unit_id": _UNIT,
                "unit_hash": _UNIT_HASH,
                "sections": [{
                    "section_id": "section:abcdefghijklmnopqrstuvwxyz",
                    "section_hash": "d" * 64,
                    "claim_slots": [{"claim_slot_id": "claim-slot:abcdefghijklmnopqrstuvwxyz", "claim": _LITERAL}],
                }],
                "decision_views": [copy.deepcopy(view_one)],
            }, {
                "unit_id": _UNIT2,
                "unit_hash": _UNIT_HASH2,
                "sections": [{
                    "section_id": "section:bcdefghijklmnopqrstuvwxyza",
                    "section_hash": "7" * 64,
                    "claim_slots": [{"claim_slot_id": "claim-slot:bcdefghijklmnopqrstuvwxyza", "claim": _LITERAL}],
                }],
                "decision_views": [copy.deepcopy(view_two)],
            }],
        }],
        "critical_view_wire": {
            "views": [copy.deepcopy(view_one), copy.deepcopy(view_two)],
        },
        "unknown_extension": {
            "text": _LITERAL,
            "opaque_hash": "f" * 64,
            "empty": "",
            "zero": 0,
            "false": False,
            "none": None,
        },
        "omitted_extension_is_absent": True,
    }


def _sample_response_shape() -> dict:
    return {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": _NODE,
        "candidate_ids": [_CANDIDATE],
        "candidate_hashes": {_CANDIDATE: _CANDIDATE_HASH},
        "unit_ids": [_UNIT, _UNIT2],
        "assessments": [{
            "candidate_id": _CANDIDATE,
            "candidate_hash": _CANDIDATE_HASH,
            "unit_id": _UNIT,
            "unit_hash": _UNIT_HASH,
            "presence": "present",
            "section_bindings": [{"section_id": "section:abcdefghijklmnopqrstuvwxyz", "section_hash": "d" * 64}],
            "assessed_direction": "supports",
            "assessed_disagreement": "consistent",
            "reason": "reason text",
            "evidence_refs": ["evidence:abcdefghijklmnopqrstuvwxyz"],
            "counterevidence_refs": [],
            "view_assessments": [{
                "decision_view_id": _VIEW,
                "decision_view_hash": _VIEW_HASH,
                "assessed_direction": "supports",
                "assessed_disagreement": "consistent",
                "reason": "view reason",
                "evidence_refs": [_SOURCE_CLAIM, _SOURCE_EVIDENCE, _SOURCE_FIELD],
                "counterevidence_refs": [],
            }],
        }, {
            "candidate_id": _CANDIDATE,
            "candidate_hash": _CANDIDATE_HASH,
            "unit_id": _UNIT2,
            "unit_hash": _UNIT_HASH2,
            "presence": "present",
            "section_bindings": [{"section_id": "section:bcdefghijklmnopqrstuvwxyza", "section_hash": "7" * 64}],
            "assessed_direction": "supports",
            "assessed_disagreement": "consistent",
            "reason": "second reason",
            "evidence_refs": ["evidence:bcdefghijklmnopqrstuvwxyza"],
            "counterevidence_refs": [],
            "view_assessments": [{
                "decision_view_id": _VIEW2,
                "decision_view_hash": _VIEW_HASH2,
                "assessed_direction": "supports",
                "assessed_disagreement": "consistent",
                "reason": "second view reason",
                "evidence_refs": ["evidence:bcdefghijklmnopqrstuvwxyza"],
                "counterevidence_refs": [],
            }],
        }],
        "group_contributions": [{
            "candidate_id": _CANDIDATE,
            "candidate_hash": _CANDIDATE_HASH,
            "group_id": _GROUP,
            "group_hash": "e" * 64,
            "unit_ids": [_UNIT],
            "decision_view_ids": [_VIEW],
            "assessed_direction": "supports",
            "assessed_disagreement": "consistent",
            "summary": "summary text",
            "evidence_refs": [_SOURCE_EVIDENCE],
            "counterevidence_refs": [],
        }, {
            "candidate_id": _CANDIDATE,
            "candidate_hash": _CANDIDATE_HASH,
            "group_id": _GROUP2,
            "group_hash": "f" * 64,
            "unit_ids": [_UNIT2],
            "decision_view_ids": [_VIEW2],
            "assessed_direction": "supports",
            "assessed_disagreement": "consistent",
            "summary": "second summary",
                "evidence_refs": [_SOURCE_EVIDENCE],
            "counterevidence_refs": [],
        }],
    }


def _expand_wire_table_aliases(wire_request: dict) -> dict:
    wrapper = expand_semantic_request_wire({
        "topic_synthesis": wire_request["topic_synthesis"],
        "semantic_source_wire": wire_request["semantic_source_wire"],
    })
    return _expand_row_tables(
        wrapper["topic_synthesis"][0]["assessment_request"],
        reducer_input=wire_request.get("schema_version") == REDUCTION_INPUT_SCHEMA,
    )


def test_reducer_scoped_projection_preserves_saved_leaf_wire_hash_and_catalog() -> None:
    request = _sample_request()
    shape = _sample_response_shape()
    wire, _wire_shape, catalog = project_assessment_wire(request, shape)

    # Frozen before reducer-only group-map declarations were introduced.
    assert catalog["wire_request_body_hash"] == "68c7d3b1fd987f4036b62138cda1da69ca2e5bff0b82b5d4f35c96e77a697507"
    assert catalog["catalog_hash"] == "ccfe936e035c4cba40cac5f0b728d3b64a415b69a9cde956f13214e7b2c25567"
    assert expand_assessment_wire_request(wire, catalog) == request


def test_response_decode_preserves_ordered_view_assessments_and_contribution_views() -> None:
    request = _sample_request()
    shape = _sample_response_shape()
    first_assessment = shape["assessments"][0]
    second_view = copy.deepcopy(shape["assessments"][1]["view_assessments"][0])
    first_view = copy.deepcopy(first_assessment["view_assessments"][0])
    first_assessment["view_assessments"] = [second_view, first_view]
    shape["group_contributions"][0]["decision_view_ids"] = [_VIEW2, _VIEW]

    wire_request, _wire_shape, catalog = project_assessment_wire(request, shape)
    wire_response = project_assessment_wire_response(shape, catalog)
    decoded = expand_assessment_wire_response(wire_response, catalog, wire_request)

    assert [
        row["decision_view_id"] for row in decoded["assessments"][0]["view_assessments"]
    ] == [_VIEW2, _VIEW]
    assert decoded["group_contributions"][0]["decision_view_ids"] == [_VIEW2, _VIEW]


def test_assessment_wire_roundtrips_typed_aliases_and_exact_literals() -> None:
    canonical_request = _sample_request()
    canonical_shape = _sample_response_shape()
    request_before = copy.deepcopy(canonical_request)
    shape_before = copy.deepcopy(canonical_shape)

    wire, wire_shape, catalog = project_assessment_wire(canonical_request, canonical_shape)

    assert wire["node_id"] == _NODE
    assert wire["schema_version"] == ASSESSMENT_INPUT_SCHEMA
    assert wire["topic_synthesis"][0]["assessment_request"]["candidate_ids"][0] != _CANDIDATE
    assert "forward" not in wire and "alias_entries" not in wire
    assert wire[WIRE_KEY]["catalog_hash"] == catalog["catalog_hash"]
    assert catalog["response_row_table_schemas"]["schema_version"] == RESPONSE_ROW_TABLE_SCHEMA_V1
    assert "row_defaults" not in catalog["response_row_table_schemas"]
    assert wire_shape["node_id"] == _NODE
    assert compute_v3_hash(wire_shape) == catalog["wire_response_shape_hash"]
    assert catalog["canonical_response_shape_hash"] == compute_v3_hash(canonical_shape)
    wire_echo_rows = _expand_row_tables(wire["response_echo_bindings"], "response_echo_bindings")
    assert wire_echo_rows == catalog["response_echo_bindings"]
    expanded_wire_shape = _expand_response_global_layout_tables(
        wire_shape, wire["response_row_table_schemas"],
    )
    assert wire_echo_rows[0]["group_hash"] == expanded_wire_shape["group_contributions"][0]["group_hash"]
    response_schema = wire["response_row_table_schemas"]
    assessment_table_schema = next(
        row for row in response_schema["tables"]
        if row["path_template"] == ["assessments"]
    )
    layout = response_schema["layouts"][assessment_table_schema["layout_ids"][0]]
    assessment_wire_table = wire_shape["assessments"]["arbitration_row_table_v1"]
    assert layout == response_schema["layouts"][assessment_wire_table["rows"][0][0]]
    assert assessment_table_schema["layout_ids"]
    response_marker = assessment_wire_table
    assert response_marker["layout_ids"] == assessment_table_schema["layout_ids"]
    assert all(row[0] in assessment_table_schema["layout_ids"] for row in response_marker["rows"])
    assert set(response_marker) == {"layout_ids", "rows"}
    assert catalog["literal_string_table"].count(_LITERAL) == 1
    assert _UNIQUE_LITERAL not in catalog["literal_string_table"]
    assert sum(json_value == _LITERAL for json_value in _all_json_strings(wire)) == 1
    encoded_request = wire["topic_synthesis"][0]["assessment_request"]
    assert encoded_request["unknown_extension"]["opaque_hash"] == "f" * 64
    assert _UNIQUE_LITERAL not in _all_json_strings(wire["semantic_source_wire"])
    assert _expand_wire_table_aliases(wire)["critical_view_wire"]["views"][0]["literal_qualifier_values"][0]["literal_value"] == _UNIQUE_LITERAL
    wire_assessment = _expand_wire_table_aliases(wire)
    wire_candidate = wire_assessment["candidate_units"][0]
    wire_unit_ids = [row["decision_view_ids"] for row in wire_candidate["unit_rows"]]
    wire_global_view_ids = [row["decision_view_id"] for row in wire_assessment["critical_view_wire"]["views"]]
    assert wire_unit_ids == [[wire_global_view_ids[0]], [wire_global_view_ids[1]]]
    assert all("decision_views" not in row for row in wire_candidate["unit_rows"])
    assert len(wire_assessment["critical_view_wire"]["views"]) == 2

    wire_with_authority = copy.deepcopy(wire)
    wire_with_authority["_prompt_authority"] = {"node_id": _NODE, "opaque": "transport-owned"}
    assert expand_assessment_wire_request(wire_with_authority, catalog) == canonical_request
    assert canonical_request == request_before
    assert canonical_shape == shape_before
    wire_response = project_assessment_wire_response(canonical_shape, catalog)
    assert wire_response["candidate_ids"][0] != _CANDIDATE
    assert wire_response["node_id"] == _NODE
    assert expand_assessment_wire_response(wire_response, catalog, wire) == canonical_shape

    reordered_shape = copy.deepcopy(canonical_shape)
    reordered_shape["assessments"].reverse()
    reordered_shape["group_contributions"].reverse()
    reordered_wire_response = project_assessment_wire_response(reordered_shape, catalog)
    assert expand_assessment_wire_response(reordered_wire_response, catalog, wire) == reordered_shape

    uncompressed_response = _expand_response_global_layout_tables(
        wire_response, wire["response_row_table_schemas"],
    )
    with pytest.raises(ArbitrationAssessmentWireError, match="must use the declared row-table"):
        expand_assessment_wire_response(uncompressed_response, catalog, wire)

    foreign_layout_response = copy.deepcopy(wire_response)
    foreign_layout_response["assessments"]["arbitration_row_table_v1"]["layout_ids"] = [999]
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_response(foreign_layout_response, catalog, wire)

    request_body = _expand_wire_table_aliases(wire)
    request_rows = request_body["review_group_map"]
    candidate_issue = next(row for row in request_rows if row.get("issue_scope") == "candidate")
    claim_issue = next(row for row in request_rows if row.get("issue_scope") == "claim")
    candidate_scope_alias = next(
        row["alias"] for row in catalog["alias_entries"]
        if row["domain"] == "request" and row["kind"] == "candidate_id"
        and row["canonical_value"] == _CANDIDATE
    )
    claim_scope_alias = next(
        row["alias"] for row in catalog["alias_entries"]
        if row["domain"] == "request" and row["kind"] == "claim_id"
        and row["canonical_value"] == "claim:source-abcdefghijklmnopqrstuvwxyz"
    )
    assert candidate_issue["source_scope_ids"] == [candidate_scope_alias]
    assert claim_issue["source_scope_ids"] == [claim_scope_alias]
    assert any(
        row["domain"] == "request" and row["kind"] == "candidate_id"
        and row["path"][-2:] == ["source_scope_ids", 0]
        and row["canonical_value"] == _CANDIDATE
        for row in catalog["alias_entries"]
    )
    wrong_scope_role = copy.deepcopy(request_body)
    wrong_candidate_issue = next(row for row in wrong_scope_role["review_group_map"] if row.get("issue_scope") == "candidate")
    wrong_candidate_issue["source_scope_ids"][0] = claim_scope_alias
    with pytest.raises(ArbitrationAssessmentWireError):
        _transform_typed(
            wrong_scope_role,
            domain="request",
            entries=catalog["alias_entries"],
            encode=False,
            prefix=catalog["alias_prefix"],
        )


def test_decision_view_reference_projection_is_exact_and_fails_closed() -> None:
    canonical = _sample_request()
    body = {key: copy.deepcopy(value) for key, value in canonical.items() if key != "node_id"}
    compacted, reference_rows = _compact_candidate_decision_view_rows(body)
    assert reference_rows[0]["decision_view_ids"] == [_VIEW]
    assert reference_rows[1]["decision_view_ids"] == [_VIEW2]
    first_unit = compacted["candidate_units"][0]["unit_rows"][0]
    assert first_unit["decision_view_ids"] == [_VIEW]
    assert "decision_views" not in first_unit
    restored = _expand_candidate_decision_view_rows(
        compacted,
        reference_schema=DECISION_VIEW_REFERENCE_SCHEMA,
        reference_hash=compute_v3_hash(reference_rows),
    )
    assert restored == body

    missing_view = copy.deepcopy(compacted)
    missing_view["candidate_units"][0]["unit_rows"][0]["decision_view_ids"] = ["missing-view"]
    with pytest.raises(ArbitrationAssessmentWireError, match="missing or foreign"):
        _expand_candidate_decision_view_rows(
            missing_view,
            reference_schema=DECISION_VIEW_REFERENCE_SCHEMA,
            reference_hash=compute_v3_hash(reference_rows),
        )

    foreign_owner = copy.deepcopy(compacted)
    foreign_owner["candidate_units"][0]["candidate_id"] = "candidate-foreign"
    with pytest.raises(ArbitrationAssessmentWireError, match="missing or foreign"):
        _expand_candidate_decision_view_rows(
            foreign_owner,
            reference_schema=DECISION_VIEW_REFERENCE_SCHEMA,
            reference_hash=compute_v3_hash(reference_rows),
        )

    partial_duplicate = copy.deepcopy(body)
    partial_duplicate["candidate_units"][0]["unit_rows"][0]["decision_views"][0].pop("source_bindings")
    with pytest.raises(ArbitrationAssessmentWireError, match="not exactly present"):
        _compact_candidate_decision_view_rows(partial_duplicate)


def test_lexically_sorted_opaque_group_refs_expand_to_canonical_set_order_and_keep_semantic_gates(tmp_path) -> None:
    plan, _caps_value = _wire_ready_plan(tmp_path, slot_count=6)
    leaf = plan["assessment_leaves"][0]
    canonical_response = _typed_leaf_response(plan, leaf)
    expected_by_key = {
        (row["candidate_id"], row["unit_id"]): row
        for row in leaf["expected_assessments"]
    }
    for assessment in canonical_response["assessments"]:
        expected = expected_by_key[(assessment["candidate_id"], assessment["unit_id"])]
        assessment["evidence_refs"] = list(expected["evidence_ref_ids"])
        for view_response, expected_view in zip(
            assessment["view_assessments"], expected["decision_views"], strict=True,
        ):
            view_response["evidence_refs"] = list(expected_view["literal_view_ref_ids"])
    canonical_response["group_contributions"] = _refresh_group_contributions(
        plan, leaf, canonical_response,
    )

    canonical_refs = canonical_response["group_contributions"][0]["evidence_refs"]
    assert len(canonical_refs) >= 10
    catalog = leaf["wire_catalog"]
    alias_by_value = {
        row["canonical_value"]: row["alias"]
        for row in catalog["alias_entries"]
        if row["domain"] == "response" and row["kind"] == "source_ref_id"
    }
    canonical_alias_order = [alias_by_value[value] for value in canonical_refs]
    assert len(canonical_alias_order) >= 10
    assert sorted(canonical_alias_order) != canonical_alias_order

    wire_response = project_assessment_wire_response(canonical_response, catalog)
    group_table = wire_response["group_contributions"]["arbitration_row_table_v1"]
    schema_layouts = catalog["response_row_table_schemas"]["layouts"]
    for layout_id, cells in group_table["rows"]:
        columns = schema_layouts[layout_id]
        for field in ("evidence_refs", "counterevidence_refs"):
            refs = cells[columns.index(field)]
            refs.sort()

    opaque_expanded = _expand_response_global_layout_tables(
        wire_response, catalog["response_row_table_schemas"],
    )
    opaque_refs = opaque_expanded["group_contributions"][0]["evidence_refs"]
    assert opaque_refs == sorted(canonical_alias_order)
    assert opaque_refs != canonical_alias_order

    decoded = expand_assessment_wire_response(wire_response, catalog, leaf["request"])
    assert decoded["group_contributions"][0]["evidence_refs"] == canonical_refs
    validated = validate_arbitration_assessment_response(
        plan, leaf["node_id"], wire_response, estimate_output_tokens=_output_estimator,
    )
    assert validated["group_contributions"][0]["evidence_refs"] == canonical_refs

    def mutate_first_group_refs(response: dict, mutation) -> dict:
        altered = copy.deepcopy(response)
        table = altered["group_contributions"]["arbitration_row_table_v1"]
        first_layout, first_cells = table["rows"][0]
        columns = schema_layouts[first_layout]
        refs_index = columns.index("evidence_refs")
        mutation(first_cells[refs_index])
        first_cells[refs_index].sort()
        return altered

    missing_ref = mutate_first_group_refs(wire_response, lambda refs: refs.pop())
    with pytest.raises(ArbitrationAggregationError, match="omits or adds typed evidence references"):
        validate_arbitration_assessment_response(
            plan, leaf["node_id"], missing_ref, estimate_output_tokens=_output_estimator,
        )

    duplicate_ref = mutate_first_group_refs(wire_response, lambda refs: refs.append(refs[0]))
    with pytest.raises(ArbitrationAggregationError, match="duplicate"):
        validate_arbitration_assessment_response(
            plan, leaf["node_id"], duplicate_ref, estimate_output_tokens=_output_estimator,
        )

    extra_value = canonical_refs[-1]
    partial_response = copy.deepcopy(canonical_response)
    for assessment in partial_response["assessments"]:
        for field in ("evidence_refs", "counterevidence_refs"):
            assessment[field] = [reference for reference in assessment[field] if reference != extra_value]
        for view in assessment["view_assessments"]:
            for field in ("evidence_refs", "counterevidence_refs"):
                view[field] = [reference for reference in view[field] if reference != extra_value]
    partial_response["group_contributions"] = _refresh_group_contributions(
        plan, leaf, partial_response,
    )
    partial_wire = project_assessment_wire_response(partial_response, catalog)
    extra_alias = alias_by_value[extra_value]
    extra_ref = mutate_first_group_refs(partial_wire, lambda refs: refs.append(extra_alias))
    with pytest.raises(ArbitrationAggregationError, match="omits or adds typed evidence references"):
        validate_arbitration_assessment_response(
            plan, leaf["node_id"], extra_ref, estimate_output_tokens=_output_estimator,
        )


def test_same_canonical_value_in_two_typed_roles_is_shared_but_other_kind_is_rejected() -> None:
    canonical = {
        "schema_version": ASSESSMENT_INPUT_SCHEMA,
        "node_id": _NODE,
        "candidate_ids": ["shared-role-value"],
        "unit_ids": ["shared-role-value"],
    }
    shape = {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": _NODE,
        "candidate_ids": ["shared-role-value"],
        "unit_ids": ["shared-role-value"],
    }
    wire, _wire_shape, catalog = project_assessment_wire(canonical, shape)
    request_row = wire["topic_synthesis"][0]["assessment_request"]
    assert request_row["candidate_ids"][0] == request_row["unit_ids"][0]
    assert expand_assessment_wire_request(wire, catalog) == canonical

    distinct = {
        **canonical,
        "candidate_ids": [_CANDIDATE],
        "unit_ids": [_UNIT],
    }
    distinct_shape = {
        **shape,
        "candidate_ids": [_CANDIDATE],
        "unit_ids": [_UNIT],
    }
    distinct_wire, _distinct_shape, distinct_catalog = project_assessment_wire(distinct, distinct_shape)
    bad = copy.deepcopy(distinct_wire)
    request_body = bad["topic_synthesis"][0]["assessment_request"]
    request_body["candidate_ids"][0] = request_body["unit_ids"][0]
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(bad, distinct_catalog)


def _all_json_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _all_json_strings(key)
            yield from _all_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_json_strings(item)
    elif isinstance(value, str):
        yield value


def test_assessment_wire_rejects_foreign_aliases_and_tampered_tables() -> None:
    request = _sample_request()
    shape = _sample_response_shape()
    wire, _wire_shape, catalog = project_assessment_wire(request, shape)

    foreign_request = copy.deepcopy(wire)
    foreign_request["topic_synthesis"][0]["assessment_request"]["candidate_ids"][0] = "$I999"
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(foreign_request, catalog)

    unknown_alias_in_prose = copy.deepcopy(wire)
    unknown_alias_in_prose["topic_synthesis"][0]["assessment_request"]["unknown_extension"]["text"] = "$I999"
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(unknown_alias_in_prose, catalog)

    tampered_table = copy.deepcopy(wire)
    tampered_table["semantic_source_wire"]["strings"][0] += " altered"
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(tampered_table, catalog)

    tampered_echo = copy.deepcopy(wire)
    echo_rows = _expand_row_tables(tampered_echo["response_echo_bindings"], "response_echo_bindings")
    echo_rows[0]["group_hash"] = "$I999"
    tampered_echo["response_echo_bindings"] = _pack_row_tables(echo_rows, "response_echo_bindings")
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(tampered_echo, catalog)

    wrong_kind_response = copy.deepcopy(project_assessment_wire_response(shape, catalog))
    response_table = wrong_kind_response["assessments"]["arbitration_row_table_v1"]
    wrong_kind_row = response_table["rows"][0]
    wrong_kind_columns = wire["response_row_table_schemas"]["layouts"][wrong_kind_row[0]]
    wrong_kind_row[1][wrong_kind_columns.index("candidate_id")] = wrong_kind_row[1][
        wrong_kind_columns.index("unit_id")
    ]
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_response(wrong_kind_response, catalog, wire)

    wrong_scope_role = copy.deepcopy(wire)
    request_body = _expand_wire_table_aliases(wrong_scope_role)
    request_rows = request_body["review_group_map"]
    candidate_issue = next(row for row in request_rows if row.get("issue_scope") == "candidate")
    claim_alias = next(
        row["alias"] for row in catalog["alias_entries"]
        if row["domain"] == "request" and row["kind"] == "claim_id"
        and row["canonical_value"] == "claim:source-abcdefghijklmnopqrstuvwxyz"
    )
    candidate_issue["source_scope_ids"][0] = claim_alias
    with pytest.raises(ArbitrationAssessmentWireError):
        _transform_typed(
            request_body,
            domain="request",
            entries=catalog["alias_entries"],
            encode=False,
            prefix=catalog["alias_prefix"],
        )

    tampered_catalog = copy.deepcopy(catalog)
    tampered_catalog["alias_entries"][0]["canonical_value"] = "foreign-id"
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(wire, tampered_catalog)


def _wire_ready_plan(
    tmp_path,
    *,
    slot_count=3,
    input_cap=32_000,
    include_candidate_issues=False,
):
    inventory, scope, contents, intent, disposition, legacy_plan, ledger, _baseline = _build_ready(
        tmp_path, candidate_count=2, slot_count=slot_count,
    )
    candidate_ids = list(legacy_plan["candidate_ids"])
    if include_candidate_issues:
        candidate_hashes = {
            candidate_id: compute_v3_hash(contents[candidate_id])
            for candidate_id in candidate_ids
        }
        issues = _critical_issues(candidate_ids, candidate_hashes)
        _hashes, disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
            candidate_ids, contents, scope, intent, issues,
        )
    source_request = _prepared_legacy_source_request(
        candidate_ids, legacy_plan["candidate_hashes"], contents, disposition, intent,
    )
    canonical_contents = source_request["candidate_contents"]

    def prompt_builder(_kind, body):
        return {
            **copy.deepcopy(dict(body)),
            "_prompt_authority": {"node_id": body["node_id"], "prompt": "unit"},
        }

    caps = replace(_caps(input_cap=input_cap), assessment_wire_enabled=True)
    plan = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=ledger,
        legacy_source_request=source_request,
        candidate_contents=canonical_contents,
        candidate_scope=scope,
        source_inventory=inventory,
        review_intent=intent,
        critique_disposition=disposition,
        request_builder=prompt_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    return plan, caps


def test_wire_assessment_and_reducer_validate_after_canonical_expansion(tmp_path) -> None:
    plan, caps = _wire_ready_plan(tmp_path)
    assert plan["identity"]["assessment_wire_schema"] == WIRE_SCHEMA
    leaf_results = {}
    for leaf in plan["assessment_leaves"]:
        assert leaf["wire_catalog"]["canonical_request_hash"] == leaf["canonical_request_hash"]
        assert leaf["request_hash"] == compute_v3_hash(leaf["request"])
        assert leaf["request"]["node_id"] == leaf["node_id"]
        assert leaf["request"]["_prompt_authority"]["node_id"] == leaf["node_id"]
        assert "alias_entries" not in leaf["request"]
        assert expand_assessment_wire_request(leaf["request"], leaf["wire_catalog"]) == leaf["canonical_request"]
        canonical_response = _typed_leaf_response(plan, leaf)
        wire_response = project_assessment_wire_response(canonical_response, leaf["wire_catalog"])
        normalized = validate_arbitration_assessment_response(
            plan, leaf["node_id"], wire_response, estimate_output_tokens=_output_estimator,
        )
        assert normalized["candidate_ids"] == plan["candidate_ids"]
        assert normalized["result_hash"] == compute_v3_hash({key: value for key, value in normalized.items() if key != "result_hash"})
        leaf_results[leaf["node_id"]] = normalized

    reducer_results = dict(leaf_results)
    for reducer in plan["reducer_tree"]["nodes"]:
        children = {child_id: reducer_results[child_id] for child_id in reducer["child_node_ids"]}
        reducer_request = build_arbitration_reducer_request(
            plan,
            reducer["tree_node_id"],
            children,
            request_builder=lambda _kind, body: {
                **copy.deepcopy(dict(body)),
                "_prompt_authority": {"node_id": body["node_id"], "prompt": "unit"},
            },
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=caps,
        )
        assert reducer_request["request_hash"] == compute_v3_hash(reducer_request["request"])
        assert expand_assessment_wire_request(
            reducer_request["request"], reducer_request["wire_catalog"],
        ) == reducer_request["canonical_request"]
        canonical_reduction = _typed_reduction_response(plan, reducer, reducer_request)
        wire_reduction = project_assessment_wire_response(canonical_reduction, reducer_request["wire_catalog"])
        normalized = validate_arbitration_reduction_response(
            plan, reducer_request, wire_reduction, children, estimate_output_tokens=_output_estimator,
        )
        assert normalized["unit_ids"] == reducer["unit_ids"]
        reducer_results[reducer["tree_node_id"]] = normalized


def test_reducer_wire_keeps_summary_columns_unaliased_and_values_typed(tmp_path) -> None:
    plan, caps = _wire_ready_plan(
        tmp_path,
        slot_count=16,
        input_cap=24_000,
        include_candidate_issues=True,
    )
    assert len(plan["assessment_leaves"]) >= 2
    first_reducer = plan["reducer_tree"]["nodes"][0]
    assert len(first_reducer["child_node_ids"]) >= 2

    leaf_by_id = {row["node_id"]: row for row in plan["assessment_leaves"]}
    child_results = {}
    for child_id in first_reducer["child_node_ids"]:
        leaf = leaf_by_id[child_id]
        canonical_response = _typed_leaf_response(plan, leaf)
        wire_response = project_assessment_wire_response(canonical_response, leaf["wire_catalog"])
        child_results[child_id] = validate_arbitration_assessment_response(
            plan, child_id, wire_response, estimate_output_tokens=_output_estimator,
        )

    reducer_request = build_arbitration_reducer_request(
        plan,
        first_reducer["tree_node_id"],
        child_results,
        request_builder=lambda _kind, body: {
            **copy.deepcopy(dict(body)),
            "_prompt_authority": {"node_id": body["node_id"], "prompt": "unit"},
        },
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    request = reducer_request["request"]
    catalog = reducer_request["wire_catalog"]
    response_schema = catalog["response_row_table_schemas"]
    assert response_schema["schema_version"] == RESPONSE_ROW_TABLE_SCHEMA_V2
    compact_layout_id = len(response_schema["layouts"]) - 1
    assert response_schema["layouts"][compact_layout_id] == list(COMPACT_VARIABLE_COLUMNS)
    group_table_schema = next(
        row for row in response_schema["tables"]
        if row["path_template"] == ["group_summaries"]
    )
    assert compact_layout_id in group_table_schema["layout_ids"]
    assert expand_assessment_wire_request(request, catalog) == reducer_request["canonical_request"]
    wire_group_rows = request["topic_synthesis"][0]["assessment_request"]["group_map"]["groups"]
    assert set(wire_group_rows) == {ROW_TABLE_MARKER}
    wire_group_map = request["topic_synthesis"][0]["assessment_request"]["group_map"]
    binding_table = wire_group_map[_REDUCER_GROUP_BINDINGS_MARKER]
    assert binding_table["schema_version"] == "arbitration_claim_group_bindings/v1"
    assert binding_table["columns"] == _REDUCER_GROUP_BINDING_COLUMNS
    binding_count = len(binding_table["claim_group_ids"])
    assert binding_count >= 2
    assert all(
        len(binding_table[column]) == binding_count
        for column in _REDUCER_GROUP_BINDING_COLUMNS
    )
    assert not any(field in wire_group_map for field in _REDUCER_GROUP_BINDING_FIELDS)
    wire_echo_rows = _expand_row_tables(request["response_echo_bindings"], "response_echo_bindings")
    assert all(set(row) == {"group_id", "group_hash"} for row in wire_echo_rows)
    assert len(wire_echo_rows) == len({row["group_id"] for row in wire_echo_rows})
    assert len(wire_echo_rows) == len({row["group_id"] for row in reducer_request["expected_group_summaries"]})
    response_aliases = {
        (row["canonical_value"], row["kind"]): row["alias"]
        for row in catalog["alias_entries"]
        if row["domain"] == "response"
    }
    echo_hash_by_group = {row["group_id"]: row["group_hash"] for row in wire_echo_rows}
    for summary in reducer_request["expected_group_summaries"]:
        group_alias = response_aliases[(summary["group_id"], "group_id")]
        hash_alias = response_aliases[(summary["group_hash"], "group_hash")]
        assert echo_hash_by_group[group_alias] == hash_alias
    expected_by_key = {
        (row["candidate_id"], row["group_id"]): row
        for row in reducer_request["expected_group_summaries"]
    }
    shape = reducer_request["canonical_response_shape"]
    assert shape["child_node_ids"] == reducer_request["child_node_ids"]
    assert shape["child_result_hashes"] == reducer_request["child_result_hashes"]
    for row in shape["group_summaries"]:
        expected = expected_by_key[(row["candidate_id"], row["group_id"])]
        for field in (
            "assessed_direction_values", "assessed_disagreement_values", "child_lineage",
            "evidence_refs", "counterevidence_refs",
        ):
            assert row[field] == expected[field]
        assert row["assessed_direction"] == expected["assessed_direction"]
        assert row["assessed_disagreement"] == expected["assessed_disagreement"]
    assert expand_assessment_wire_response(
        reducer_request["response_shape"], catalog, request,
    ) == shape
    compact_shape_table = reducer_request["response_shape"]["group_summaries"][ROW_TABLE_MARKER]
    assert any(row[0] == compact_layout_id for row in compact_shape_table["rows"])

    raw_aliased_body = _expand_wire_table_aliases(request)
    aliased_body = _expand_reducer_group_bindings(raw_aliased_body, reducer_input=True)
    canonical_group_map = reducer_request["canonical_request"]["group_map"]
    restored_aliased_body = _transform_typed(
        aliased_body,
        domain="request",
        entries=catalog["alias_entries"],
        encode=False,
        prefix=catalog["alias_prefix"],
        reducer_input=True,
    )
    for field in _REDUCER_GROUP_BINDING_FIELDS:
        assert restored_aliased_body["group_map"][field] == canonical_group_map[field]
    alias_prefix = request[WIRE_KEY]["alias_prefix"]
    group_map = aliased_body["group_map"]
    task_groups = [row for row in group_map["groups"] if row.get("group_kind") == "task_topic"]
    assert task_groups
    alias_kinds = {
        (row["alias"], row["kind"])
        for row in catalog["alias_entries"]
        if row["domain"] == "request"
    }
    for group in group_map["groups"]:
        assert all(not key.startswith(alias_prefix) for key in group)
        for assertion_id in group.get("used_assertion_ids") or []:
            assert (assertion_id, "claim_id") in alias_kinds
        used_by_candidate = group.get("used_assertion_ids_by_candidate") or {}
        for candidate_id, assertion_ids in used_by_candidate.items():
            assert (candidate_id, "candidate_id") in alias_kinds
            assert all((assertion_id, "claim_id") in alias_kinds for assertion_id in assertion_ids)
    assert any(
        assertion_id
        for group in task_groups
        for assertion_id in group.get("used_assertion_ids") or []
    )
    slot_bindings = group_map["claim_group_slot_bindings"]
    assert slot_bindings
    assert all((group_id, "claim_group_id") in alias_kinds for group_id in slot_bindings)
    assert all(
        (slot_id, "claim_slot_id") in alias_kinds
        for slot_ids in slot_bindings.values()
        for slot_id in slot_ids
    )

    summaries = aliased_body["candidate_group_summaries"]
    assert isinstance(summaries, list) and len(summaries) >= 2
    first_summary = summaries[0]
    assert {"candidate_id", "candidate_hash", "group_id", "group_hash"}.issubset(first_summary)
    assert all(not key.startswith(alias_prefix) for key in first_summary)
    for field in ("candidate_id", "candidate_hash", "group_id", "group_hash"):
        assert first_summary[field].startswith(alias_prefix)

    wrong_type_alias = copy.deepcopy(aliased_body)
    wrong_type_alias["candidate_group_summaries"][0]["candidate_hash"] = (
        wrong_type_alias["candidate_group_summaries"][0]["group_id"]
    )
    with pytest.raises(ArbitrationAssessmentWireError):
        _transform_typed(
            wrong_type_alias,
            domain="request",
            entries=catalog["alias_entries"],
            encode=False,
            prefix=catalog["alias_prefix"],
        )

    reducer_alias_errors = []
    assertion_group = next(group for group in task_groups if group.get("used_assertion_ids"))
    wrong_assertion = copy.deepcopy(aliased_body)
    wrong_assertion["group_map"]["groups"][
        next(index for index, row in enumerate(wrong_assertion["group_map"]["groups"])
             if row["group_id"] == assertion_group["group_id"])
    ]["used_assertion_ids"][0] = assertion_group["group_id"]
    reducer_alias_errors.append(wrong_assertion)

    candidate_group = next(group for group in task_groups if group.get("used_assertion_ids_by_candidate"))
    wrong_candidate_key = copy.deepcopy(aliased_body)
    candidate_row = next(
        row for row in wrong_candidate_key["group_map"]["groups"]
        if row["group_id"] == candidate_group["group_id"]
    )
    old_candidate_id = next(iter(candidate_row["used_assertion_ids_by_candidate"]))
    candidate_assertions = candidate_row["used_assertion_ids_by_candidate"].pop(old_candidate_id)
    candidate_row["used_assertion_ids_by_candidate"][candidate_group["group_id"]] = candidate_assertions
    reducer_alias_errors.append(wrong_candidate_key)

    wrong_slot_value = copy.deepcopy(aliased_body)
    first_group_id = next(
        group_id for group_id, slot_ids in wrong_slot_value["group_map"]["claim_group_slot_bindings"].items()
        if slot_ids
    )
    wrong_slot_value["group_map"]["claim_group_slot_bindings"][first_group_id][0] = first_group_id
    reducer_alias_errors.append(wrong_slot_value)

    for wrong_body in reducer_alias_errors:
        with pytest.raises(ArbitrationAssessmentWireError):
            _transform_typed(
                wrong_body,
                domain="request",
                entries=catalog["alias_entries"],
                encode=False,
                prefix=catalog["alias_prefix"],
                reducer_input=True,
            )

    malformed_group_binding = copy.deepcopy(raw_aliased_body)
    malformed_group_binding["group_map"][_REDUCER_GROUP_BINDINGS_MARKER]["schema_version"] = "unknown/v9"
    with pytest.raises(ArbitrationAssessmentWireError, match="unknown schema or columns"):
        _expand_reducer_group_bindings(malformed_group_binding, reducer_input=True)

    changed_columns = copy.deepcopy(raw_aliased_body)
    changed_columns["group_map"][_REDUCER_GROUP_BINDINGS_MARKER]["columns"][0] = "candidate_ids"
    with pytest.raises(ArbitrationAssessmentWireError, match="unknown schema or columns"):
        _expand_reducer_group_bindings(changed_columns, reducer_input=True)

    duplicate_group = copy.deepcopy(raw_aliased_body)
    duplicate_table = duplicate_group["group_map"][_REDUCER_GROUP_BINDINGS_MARKER]
    duplicate_table["claim_group_ids"][1] = duplicate_table["claim_group_ids"][0]
    with pytest.raises(ArbitrationAssessmentWireError, match="malformed or misaligned"):
        _expand_reducer_group_bindings(duplicate_group, reducer_input=True)

    length_drift = copy.deepcopy(raw_aliased_body)
    length_drift["group_map"][_REDUCER_GROUP_BINDINGS_MARKER]["task_ids"].pop()
    with pytest.raises(ArbitrationAssessmentWireError, match="malformed or misaligned"):
        _expand_reducer_group_bindings(length_drift, reducer_input=True)

    shadow_map = copy.deepcopy(raw_aliased_body)
    shadow_map["group_map"]["claim_group_slot_bindings"] = {}
    with pytest.raises(ArbitrationAssessmentWireError, match="coexists with shadow"):
        _expand_reducer_group_bindings(shadow_map, reducer_input=True)

    wrong_kind_group_binding = copy.deepcopy(raw_aliased_body)
    wrong_kind_table = wrong_kind_group_binding["group_map"][_REDUCER_GROUP_BINDINGS_MARKER]
    wrong_kind_table["task_ids"][0] = next(
        row["alias"] for row in catalog["alias_entries"]
        if row["domain"] == "request" and row["kind"] == "claim_id"
    )
    wrong_kind_expanded = _expand_reducer_group_bindings(wrong_kind_group_binding, reducer_input=True)
    with pytest.raises(ArbitrationAssessmentWireError):
        _transform_typed(
            wrong_kind_expanded,
            domain="request",
            entries=catalog["alias_entries"],
            encode=False,
            prefix=catalog["alias_prefix"],
            reducer_input=True,
        )

    mismatched_maps = {
        "group_map": {
            "claim_group_task_bindings": {"g1": "t1", "g2": "t2"},
            "claim_group_assertion_bindings": {"g1": "a1"},
            "claim_group_slot_bindings": {"g1": ["s1"], "g2": ["s2"]},
        },
    }
    assert _compact_reducer_group_bindings(mismatched_maps, reducer_input=True) == mismatched_maps

    canonical_response = _typed_reduction_response(plan, first_reducer, reducer_request)
    wire_response = project_assessment_wire_response(canonical_response, catalog)
    compact_response_table = wire_response["group_summaries"][ROW_TABLE_MARKER]
    assert any(row[0] == compact_layout_id for row in compact_response_table["rows"])

    expected_summaries = {
        (row["candidate_id"], row["group_id"]): row
        for row in reducer_request["expected_group_summaries"]
    }
    model_sorted_alias_response = copy.deepcopy(wire_response)
    alias_list_to_reorder = None
    for layout_id, cells in model_sorted_alias_response["group_summaries"][ROW_TABLE_MARKER]["rows"]:
        columns = response_schema["layouts"][layout_id]
        if "decision_view_ids" not in columns:
            continue
        view_index = columns.index("decision_view_ids")
        aliases = cells[view_index]
        if (
            isinstance(aliases, list) and len(aliases) > 1
            and (alias_list_to_reorder is None or len(aliases) > len(alias_list_to_reorder[0]))
        ):
            alias_list_to_reorder = (aliases, view_index)
    assert alias_list_to_reorder is not None
    alias_list, _ = alias_list_to_reorder
    assert list(reversed(alias_list)) != alias_list
    for layout_id, cells in model_sorted_alias_response["group_summaries"][ROW_TABLE_MARKER]["rows"]:
        columns = response_schema["layouts"][layout_id]
        if "decision_view_ids" not in columns:
            continue
        view_index = columns.index("decision_view_ids")
        aliases = cells[view_index]
        if aliases is alias_list:
            cells[view_index] = list(reversed(aliases))
    decoded_model_order = expand_assessment_wire_response(
        model_sorted_alias_response, catalog, request,
    )
    for summary in decoded_model_order["group_summaries"]:
        expected = expected_summaries[(summary["candidate_id"], summary["group_id"])]
        assert summary["decision_view_ids"] == expected["decision_view_ids"]

    measured_output = _output_estimator(wire_response)
    assert measured_output <= reducer_request["requested_output_tokens"] <= caps.route_max_output
    validated_reducer = validate_arbitration_reduction_response(
        plan, reducer_request, wire_response, child_results,
        estimate_output_tokens=_output_estimator,
    )
    assert validated_reducer["unit_ids"] == first_reducer["unit_ids"]

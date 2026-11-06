"""Edge-only regression tests for the current overflow model fixture."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from outline.arbitration_assessment_wire import (
    WIRE_KEY,
    ArbitrationAssessmentWireError,
    _expand_row_tables,
    _pack_row_tables,
    expand_assessment_wire_request,
    expand_assessment_wire_response,
    project_assessment_wire,
    project_assessment_wire_response,
)
from outline.candidate_arbitration_aggregation import (
    ArbitrationAggregationError,
    build_arbitration_reducer_request,
    build_global_selection_detail_requests,
    build_global_selection_followup_request,
    build_global_selection_request,
    validate_arbitration_assessment_response,
    validate_arbitration_reduction_response,
    validate_global_selection_detail_responses,
    validate_global_selection_response,
)
from outline.candidate_arbitration_navigation import (
    DETAIL_REQUEST_SCHEMA,
    SelectedNavigationError,
    build_global_navigation_request,
    build_layout_reduction_requests,
    build_merge_detail_reduction_requests,
    build_merge_group_adjudication_requests,
    build_proposed_merge_detail_requests,
    build_source_detail_index,
    compile_section_coordination,
    layout_leaf_nodes_from_fragments,
    validate_global_navigation_response,
    validate_layout_fragment,
    validate_layout_reduction_response,
    validate_merge_detail_reduction_response,
    validate_merge_detail_responses,
    validate_merge_group_adjudication_responses,
)
from outline.semantic_request_wire import (
    expand_semantic_request_wire,
    pack_semantic_request_wire,
)
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from tests.arbitration_overflow_model_fixture import (
    build_arbitration_overflow_response,
)
from tests.test_arbitration_assessment_wire import _wire_ready_plan
from tests.test_run_first_arbitration_aggregation import (
    _build_ready,
    _input_estimator,
    _output_estimator,
    _route_builder,
)
from tests.test_run_first_arbitration_aggregation import (
    _caps as aggregation_caps,
)
from tests.test_run_first_arbitration_aggregation import (
    _input_estimator as aggregation_input,
)
from tests.test_run_first_arbitration_aggregation import (
    _output_estimator as aggregation_output,
)
from tests.test_run_first_arbitration_navigation import (
    _callbacks as navigation_callbacks,
)
from tests.test_run_first_arbitration_navigation import (
    _contexts as navigation_contexts,
)
from tests.test_run_first_arbitration_navigation import (
    _many_claims_section,
)
from tests.test_run_first_arbitration_navigation import (
    _plan as navigation_plan,
)
from tests.test_run_first_arbitration_navigation import (
    _section as navigation_section,
)


def _finish_navigation_tree(plan, state, contexts):
    authority, route, output = navigation_callbacks(contexts)
    fragments = []
    for request in plan["layout_extraction_requests"]:
        response = build_arbitration_overflow_response(
            "arbitration", request["request"], state=state,
        )
        if not fragments:
            forged = copy.deepcopy(response)
            forged["text_hash"] = "f" * 64
            with pytest.raises(SelectedNavigationError):
                validate_layout_fragment(
                    plan, request, forged, output_estimator=output, context_reserve=contexts,
                )
        fragments.append(validate_layout_fragment(
            plan, request, response, output_estimator=output, context_reserve=contexts,
        ))
    ready = {row["node_id"]: row for row in layout_leaf_nodes_from_fragments(plan, fragments)}
    for level in plan["layout_reduction_tree"]["levels"]:
        inputs = [ready[node_id] for node_id in level["input_node_ids"]]
        requests = build_layout_reduction_requests(
            plan,
            int(level["level"]),
            inputs,
            prompt_authority=authority,
            route_estimator=route,
            output_estimator=output,
            context_reserve=contexts,
            input_cap=plan["input_cap"],
        )
        reduced = {}
        input_by_id = {row["node_id"]: row for row in inputs}
        for request in requests["requests"]:
            response = build_arbitration_overflow_response(
                "arbitration", request["request"], state=state,
            )
            children = [input_by_id[node_id] for node_id in request["body"]["child_node_ids"]]
            if not reduced:
                forged = copy.deepcopy(response)
                forged["candidate_hash"] = "e" * 64
                with pytest.raises(SelectedNavigationError):
                    validate_layout_reduction_response(
                        plan, request, forged, input_nodes=children,
                        output_estimator=output, context_reserve=contexts,
                    )
            reduced[request["request_id"]] = validate_layout_reduction_response(
                plan, request, response, input_nodes=children,
                output_estimator=output, context_reserve=contexts,
            )
        ready = reduced | {
            carry_id: ready[carry_id] for carry_id in requests["carry_node_ids"]
        }
    root = ready[plan["layout_reduction_tree"]["root_node_id"]]
    request = build_global_navigation_request(
        plan, root, prompt_authority=authority, route_estimator=route,
        output_estimator=output, context_reserve=contexts, input_cap=plan["input_cap"],
    )
    response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
    forged_navigation = copy.deepcopy(response)
    forged_navigation["section_order"] = []
    with pytest.raises(SelectedNavigationError):
        validate_global_navigation_response(
            plan, request, forged_navigation, output_estimator=output, context_reserve=contexts,
        )
    proposal = validate_global_navigation_response(
        plan, request, response, output_estimator=output, context_reserve=contexts,
    )
    return authority, route, output, root, proposal


def _finish_proposed_merge(plan, proposal, state, contexts, *, detail_input_cap):
    authority, route, output = navigation_callbacks(contexts)
    reduction_input_cap = min(int(plan["input_cap"]), max(detail_input_cap * 2, 12_000))
    detail_index = build_source_detail_index(plan)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index,
        prompt_authority=authority,
        route_estimator=route,
        output_estimator=output,
        context_reserve=contexts,
        input_cap=detail_input_cap,
        emax=plan["emax"],
    )
    slice_responses = []
    for request in detail_plan["requests"]:
        response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
        slice_responses.append({"request_id": request["request_id"], "response": response})
    forged_slices = copy.deepcopy(slice_responses)
    if forged_slices:
        forged_slices[0]["response"]["detail_item_hashes"] = {}
        with pytest.raises(SelectedNavigationError):
            validate_merge_detail_responses(
                plan, proposal, detail_plan, forged_slices, detail_index=detail_index,
                output_estimator=output, context_reserve=contexts,
            )
    slice_results = validate_merge_detail_responses(
        plan, proposal, detail_plan, slice_responses,
        detail_index=detail_index, output_estimator=output, context_reserve=contexts,
    )
    reduction_roots = {}
    for group_id, tree in detail_plan["adjudication_trees"].items():
        ready = {
            row["node_id"]: row
            for row in slice_results["groups"][group_id]["slice_nodes"]
        }
        for level in tree["levels"]:
            inputs = [ready[node_id] for node_id in level["input_node_ids"]]
            requests = build_merge_detail_reduction_requests(
                plan, proposal, detail_plan, slice_results, group_id, int(level["level"]), inputs,
                detail_index=detail_index, prompt_authority=authority, route_estimator=route,
                output_estimator=output, context_reserve=contexts, input_cap=reduction_input_cap,
            )
            input_by_id = {row["node_id"]: row for row in inputs}
            reduced = {}
            for request in requests["requests"]:
                response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
                children = [input_by_id[node_id] for node_id in request["child_node_ids"]]
                if not reduced:
                    forged = copy.deepcopy(response)
                    forged["critical_literal_bindings_hash"] = "f" * 64
                    with pytest.raises(SelectedNavigationError):
                        validate_merge_detail_reduction_response(
                            plan, proposal, detail_plan, slice_results, request, forged, children,
                            output_estimator=output, context_reserve=contexts,
                        )
                reduced[request["request_id"]] = validate_merge_detail_reduction_response(
                    plan, proposal, detail_plan, slice_results, request, response, children,
                    output_estimator=output, context_reserve=contexts,
                )
            ready = reduced | {
                carry_id: ready[carry_id] for carry_id in requests["carry_node_ids"]
            }
        reduction_roots[group_id] = ready[tree["root_node_id"]]

    root, _ = _finish_navigation_tree_again(plan, state, contexts)
    final_plan = build_merge_group_adjudication_requests(
        plan, proposal, detail_plan, slice_results, reduction_roots, root, detail_index,
        prompt_authority=authority, route_estimator=route, output_estimator=output,
        context_reserve=contexts, input_cap=plan["input_cap"],
    )
    final_responses = []
    for request in final_plan["requests"]:
        response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
        final_responses.append({"request_id": request["request_id"], "response": response})
    forged_final = copy.deepcopy(final_responses)
    if forged_final:
        forged_final[0]["response"]["detail_root_hash"] = "f" * 64
        with pytest.raises(SelectedNavigationError):
            validate_merge_group_adjudication_responses(
                plan, proposal, detail_plan, final_plan, forged_final,
                output_estimator=output, context_reserve=contexts,
            )
    results = validate_merge_group_adjudication_responses(
        plan, proposal, detail_plan, final_plan, final_responses,
        output_estimator=output, context_reserve=contexts,
    )
    return detail_plan, slice_results, reduction_roots, final_plan, results


def _finish_navigation_tree_again(plan, state, contexts):
    """Rebuild a READY layout tree for the finalizer from only wire responses."""
    authority, route, output = navigation_callbacks(contexts)
    fragments = []
    for request in plan["layout_extraction_requests"]:
        response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
        fragments.append(validate_layout_fragment(
            plan, request, response, output_estimator=output, context_reserve=contexts,
        ))
    ready = {row["node_id"]: row for row in layout_leaf_nodes_from_fragments(plan, fragments)}
    for level in plan["layout_reduction_tree"]["levels"]:
        inputs = [ready[node_id] for node_id in level["input_node_ids"]]
        requests = build_layout_reduction_requests(
            plan, int(level["level"]), inputs,
            prompt_authority=authority, route_estimator=route, output_estimator=output,
            context_reserve=contexts, input_cap=plan["input_cap"],
        )
        input_by_id = {row["node_id"]: row for row in inputs}
        reduced = {}
        for request in requests["requests"]:
            response = build_arbitration_overflow_response("arbitration", request["request"], state=state)
            children = [input_by_id[node_id] for node_id in request["body"]["child_node_ids"]]
            reduced[request["request_id"]] = validate_layout_reduction_response(
                plan, request, response, input_nodes=children,
                output_estimator=output, context_reserve=contexts,
            )
        ready = reduced | {carry_id: ready[carry_id] for carry_id in requests["carry_node_ids"]}
    return ready[plan["layout_reduction_tree"]["root_node_id"]], fragments


def test_unrecognized_or_legacy_v1_request_is_left_to_the_existing_fixture():
    state = {"navigation_mode": "coherent_merge"}
    assert build_arbitration_overflow_response(
        "arbitration", {"schema_version": "outline_candidate_arbitration_leaf/v1"}, state=state,
    ) is None
    assert build_arbitration_overflow_response("arbitration", {"schema_version": "other/v1"}, state=state) is None
    assert "wire_response_history" not in state


def test_same_server_state_replays_a_stable_wire_response_without_receipts():
    state = {}
    trace = []
    first_request = {
        "schema_version": "arbitration_selection_input/v3",
        "node_id": "selector-stable-1",
        "candidate_ids": ["candidate_a", "candidate_b"],
        "candidate_hashes": {"candidate_a": "a" * 64, "candidate_b": "b" * 64},
        "noncritical_fact_index": [],
    }
    first = build_arbitration_overflow_response("arbitration", first_request, trace=trace, state=state)
    changed_request = copy.deepcopy(first_request)
    changed_request["candidate_ids"].append("candidate_c")
    changed_request["candidate_hashes"]["candidate_c"] = "c" * 64
    repeated = build_arbitration_overflow_response("arbitration", changed_request, trace=trace, state=state)
    assert repeated == first
    assert "receipt_id" not in repeated
    assert len(trace) == 2
    assert trace[0]["response_hash"] == trace[1]["response_hash"]


def test_wire_assessment_and_reducer_fixture_reads_only_the_wire(tmp_path):
    plan, caps = _wire_ready_plan(tmp_path)
    leaf_results = {}

    for leaf in plan["assessment_leaves"]:
        request = leaf["request"]
        assert WIRE_KEY in request
        assert "wire_catalog" not in request and "alias_entries" not in request
        response = build_arbitration_overflow_response("arbitration", request)
        assert response is not None
        assert response["node_id"] == request["node_id"]
        assert isinstance(response.get("assessments"), dict)
        assert set(response["assessments"]) == {"arbitration_row_table_v1"}

        # The catalog is used only on the test side to inspect the fixture's
        # physical response; it is never passed to the model fixture.
        expanded = expand_assessment_wire_response(response, leaf["wire_catalog"], request)
        assert expanded["candidate_ids"] == leaf["candidate_ids"]
        assert expanded["unit_ids"] == leaf["unit_ids"]
        leaf_results[leaf["node_id"]] = validate_arbitration_assessment_response(
            plan, leaf["node_id"], response, estimate_output_tokens=_output_estimator,
        )

    ready_by_id = dict(leaf_results)
    for reducer in plan["reducer_tree"]["nodes"]:
        children = {child_id: ready_by_id[child_id] for child_id in reducer["child_node_ids"]}
        reducer_request = build_arbitration_reducer_request(
            plan,
            reducer["tree_node_id"],
            children,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=caps,
        )
        request = reducer_request["request"]
        response = build_arbitration_overflow_response("arbitration", request)
        assert response is not None
        expanded = expand_assessment_wire_response(response, reducer_request["wire_catalog"], request)
        assert expanded["child_node_ids"] == reducer_request["child_node_ids"]
        ready_by_id[reducer["tree_node_id"]] = validate_arbitration_reduction_response(
            plan,
            reducer_request,
            response,
            children,
            estimate_output_tokens=_output_estimator,
        )


def _rewrite_wire_assessment_body(request, mutate):
    rewritten = copy.deepcopy(request)
    expanded = expand_semantic_request_wire({
        "topic_synthesis": rewritten["topic_synthesis"],
        "semantic_source_wire": rewritten["semantic_source_wire"],
    })
    body = _expand_row_tables(expanded["topic_synthesis"][0]["assessment_request"])
    mutate(body)
    semantic = pack_semantic_request_wire({
        "topic_synthesis": [{"assessment_request": _pack_row_tables(body)}],
    })
    rewritten["topic_synthesis"] = semantic["topic_synthesis"]
    rewritten["semantic_source_wire"] = semantic["semantic_source_wire"]
    return rewritten


def test_wire_view_references_drive_ordered_model_assessments(tmp_path):
    plan, _caps = _wire_ready_plan(tmp_path)
    leaf = plan["assessment_leaves"][0]
    request = leaf["request"]
    body = expand_assessment_wire_request(request, leaf["wire_catalog"])
    expected_by_unit = {
        (candidate["candidate_id"], unit["unit_id"]): [
            view["decision_view_id"] for view in unit["decision_views"]
        ]
        for candidate in body["candidate_units"]
        for unit in candidate["unit_rows"]
    }

    response = build_arbitration_overflow_response("arbitration", request)
    assert response is not None
    expanded_response = expand_assessment_wire_response(response, leaf["wire_catalog"], request)
    actual_by_unit = {
        (row["candidate_id"], row["unit_id"]): [
            view["decision_view_id"] for view in row["view_assessments"]
        ]
        for row in expanded_response["assessments"]
    }
    assert actual_by_unit == expected_by_unit
    validate_arbitration_assessment_response(
        plan, leaf["node_id"], response, estimate_output_tokens=_output_estimator,
    )


def _group_evidence_reference_order(plan, leaf, expanded_response):
    assessment_by_key = {
        (row["candidate_id"], row["unit_id"]): row
        for row in expanded_response["assessments"]
    }
    contributions = {
        (row["candidate_id"], row["group_id"]): row
        for row in expanded_response["group_contributions"]
    }
    units_by_id = {str(row["unit_id"]): row for row in plan["assessment_units"]}
    comparisons = {}
    for expected in leaf["expected_group_contributions"]:
        key = (expected["candidate_id"], expected["group_id"])
        expected_refs: set[str] = set()
        for unit_id in expected["unit_ids"]:
            assessment = assessment_by_key[(key[0], unit_id)]
            expected_refs.update(assessment["evidence_refs"])
            group_view_ids = set(
                units_by_id[unit_id]["group_views_by_candidate"][key[0]].get(key[1], []),
            )
            for view in assessment["view_assessments"]:
                if view["decision_view_id"] in group_view_ids:
                    expected_refs.update(view["evidence_refs"])
        expected_order = sorted(expected_refs)
        observed_order = list(contributions[key]["evidence_refs"])
        comparisons[key] = (expected_order, observed_order)
    return comparisons


def _wire_group_evidence_reference_order(response, catalog):
    schema = catalog["response_row_table_schemas"]
    packed = response["group_contributions"]
    if isinstance(packed, list):
        rows = packed
    else:
        table = packed["arbitration_row_table_v1"]
        rows = [
            dict(zip(schema["layouts"][layout_id], cells, strict=True))
            for layout_id, cells in table["rows"]
        ]
    response_aliases = {
        (row["alias"], row["kind"]): row["canonical_value"]
        for row in catalog["alias_entries"]
        if row["domain"] == "response"
    }
    source_reference_aliases = {
        row["alias"]: row["canonical_value"]
        for row in catalog["source_reference_kinds"]
    }
    return {
        (
            response_aliases[(row["candidate_id"], "candidate_id")],
            response_aliases[(row["group_id"], "group_id")],
        ): [source_reference_aliases[alias] for alias in row["evidence_refs"]]
        for row in rows
    }


def test_saved_real_r4_response_normalizes_order_and_rejects_ref_corruption():
    case_root_value = os.environ.get("PR25_R4_OVERFLOW_TRACE_CASE")
    if not case_root_value:
        directory = Path(__file__).parent / "fixtures" / "arbitration_saved_first_response"
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        compressed = (directory / manifest["file"]).read_bytes()
        assert hashlib.sha256(compressed).hexdigest() == manifest["sha256"]
        raw = gzip.decompress(compressed)
        assert len(raw) == manifest["json_bytes"]
        assert hashlib.sha256(raw).hexdigest() == manifest["json_sha256"]
        saved = json.loads(raw)
        request_artifact, plan, response_snapshot = (
            saved["request_artifact"], saved["plan"], saved["response_snapshot"],
        )
    else:
        case_root = Path(case_root_value)
        registry_paths = list(case_root.glob("output/**/artifact_registry.json"))
        assert len(registry_paths) == 1
        registry = json.loads(registry_paths[0].read_text(encoding="utf-8"))
        artifacts = registry["artifacts"]
        request_entry = next(
            row for row in artifacts
            if row.get("artifact_role") == "provider_request_plan"
            and str(row.get("artifact_id") or "").startswith("arbitration:request:arbitration-assessment:")
        )
        plan_entry = next(
            row for row in artifacts if row.get("artifact_role") == "arbitration_overflow_work_plan"
        )
        response_entry = next(
            row for row in artifacts
            if row.get("artifact_role") == "provider_raw_response"
            and str(row.get("artifact_id") or "").startswith("arbitration:raw:arbitration-assessment:")
        )
        request_artifact = json.loads(Path(request_entry["path"]).read_text(encoding="utf-8"))["payload"]
        plan = json.loads(Path(plan_entry["path"]).read_text(encoding="utf-8"))["payload"]
        response_snapshot = json.loads(Path(response_entry["path"]).read_text(encoding="utf-8"))["payload"]
    request = request_artifact["request"]
    provider_response = response_snapshot["raw_response"]
    leaf = next(row for row in plan["assessment_leaves"] if row["node_id"] == request["node_id"])

    assert request_artifact["request_hash"] == leaf["request_hash"]
    assert response_snapshot["request_id"] == leaf["node_id"]
    assert hash_json(provider_response) == response_snapshot["response_hash"]

    expanded_provider = expand_assessment_wire_response(
        provider_response, leaf["wire_catalog"], request,
    )
    validate_arbitration_assessment_response(
        plan, leaf["node_id"], provider_response, estimate_output_tokens=_output_estimator,
    )

    expected_vs_captured = _group_evidence_reference_order(plan, leaf, expanded_provider)
    captured_wire_order = _wire_group_evidence_reference_order(provider_response, leaf["wire_catalog"])
    assert len(expected_vs_captured) == 10
    for key, (expected, observed) in expected_vs_captured.items():
        captured = captured_wire_order[key]
        assert set(expected) == set(captured) == set(observed)
        assert expected == observed
        assert captured != expected

    fixture_response = build_arbitration_overflow_response("arbitration", request)
    assert fixture_response is not None
    expanded_fixture = expand_assessment_wire_response(
        fixture_response, leaf["wire_catalog"], request,
    )
    fixture_order = _group_evidence_reference_order(plan, leaf, expanded_fixture)
    assert all(expected == observed for expected, observed in fixture_order.values())
    validate_arbitration_assessment_response(
        plan, leaf["node_id"], fixture_response, estimate_output_tokens=_output_estimator,
    )

    for reference_error in ("missing", "extra", "duplicate"):
        negative = copy.deepcopy(expanded_provider)
        refs = negative["group_contributions"][0]["evidence_refs"]
        if reference_error == "missing":
            refs.pop()
        elif reference_error == "extra":
            refs.append("source-ref:outside-the-wire")
        else:
            refs.append(refs[0])
        negative_wire = project_assessment_wire_response(negative, leaf["wire_catalog"])
        with pytest.raises(ArbitrationAggregationError):
            validate_arbitration_assessment_response(
                plan, leaf["node_id"], negative_wire, estimate_output_tokens=_output_estimator,
            )


def test_reducer_model_uses_declared_absence_layout_and_cannot_hide_active_group(tmp_path):
    plan, caps = _wire_ready_plan(
        tmp_path, slot_count=16, input_cap=24_000, include_candidate_issues=True,
    )
    assert len(plan["assessment_leaves"]) >= 2
    node = plan["reducer_tree"]["nodes"][0]
    leaves = {row["node_id"]: row for row in plan["assessment_leaves"]}
    children = {}
    for child_id in node["child_node_ids"]:
        response = build_arbitration_overflow_response("arbitration", leaves[child_id]["request"])
        children[child_id] = validate_arbitration_assessment_response(
            plan, child_id, response, estimate_output_tokens=_output_estimator,
        )
    request_record = build_arbitration_reducer_request(
        plan, node["tree_node_id"], children,
        request_builder=lambda _kind, body: copy.deepcopy(dict(body)),
        estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
    )
    request = request_record["request"]
    schema = request["response_row_table_schemas"]
    assert schema["schema_version"] == "arbitration_assessment_response_row_tables/v2"
    compact_ids = {int(layout_id) for layout_id in schema["row_defaults"]}
    response = build_arbitration_overflow_response("arbitration", request)
    table = response["group_summaries"]["arbitration_row_table_v1"]
    assert any(row[0] in compact_ids for row in table["rows"])
    validate_arbitration_reduction_response(
        plan, request_record, response, children, estimate_output_tokens=_output_estimator,
    )

    for field in ("child_node_ids", "child_result_hashes"):
        changed = copy.deepcopy(request_record["canonical_request"])
        changed[field].reverse()
        changed_wire, changed_shape, changed_catalog = project_assessment_wire(
            changed, request_record["canonical_response_shape"],
        )
        changed_record = {**request_record, "request": changed_wire,
            "request_hash": compute_v3_hash(changed_wire), "canonical_request": changed,
            "canonical_request_hash": changed_catalog["canonical_request_hash"],
            "wire_request_body_hash": changed_catalog["wire_request_body_hash"],
            "wire_catalog": changed_catalog, "response_shape": changed_shape,
            "wire_response_shape_hash": changed_catalog["wire_response_shape_hash"]}
        with pytest.raises(ArbitrationAggregationError, match="ordered child identity"):
            validate_arbitration_reduction_response(
                plan, changed_record, response, children, estimate_output_tokens=_output_estimator,
            )

    forged = copy.deepcopy(response)
    forged_table = forged["group_summaries"]["arbitration_row_table_v1"]
    for index, (layout_id, cells) in enumerate(forged_table["rows"]):
        if layout_id in compact_ids:
            continue
        row = dict(zip(schema["layouts"][layout_id], cells, strict=True))
        if row.get("presence") != "present":
            continue
        compact_id = min(compact_ids)
        forged_table["rows"][index] = [compact_id, [row[column] for column in schema["layouts"][compact_id]]]
        break
    else:
        pytest.fail("fixture has no active group to test false absence")
    forged_table["layout_ids"] = sorted({row[0] for row in forged_table["rows"]})
    with pytest.raises(ArbitrationAggregationError):
        validate_arbitration_reduction_response(
            plan, request_record, forged, children, estimate_output_tokens=_output_estimator,
        )


@pytest.mark.parametrize("reference_error", ["missing", "cross_candidate"])
def test_wire_view_reference_errors_are_rejected_by_fixture_and_canonical_validator(
    tmp_path, reference_error,
):
    plan, _caps = _wire_ready_plan(tmp_path)
    leaf = plan["assessment_leaves"][0]
    request = leaf["request"]

    def mutate(body):
        candidates = body["candidate_units"]
        assert len(candidates) >= 2
        first_unit = candidates[0]["unit_rows"][0]
        second_unit = candidates[1]["unit_rows"][0]
        assert first_unit["decision_view_ids"] and second_unit["decision_view_ids"]
        if reference_error == "missing":
            first_unit["decision_view_ids"].pop(0)
        else:
            first_unit["decision_view_ids"][0] = second_unit["decision_view_ids"][0]

    malformed_request = _rewrite_wire_assessment_body(request, mutate)
    with pytest.raises(ArbitrationAssessmentWireError):
        expand_assessment_wire_request(malformed_request, leaf["wire_catalog"])
    with pytest.raises(ValueError, match="could not read the supplied wire"):
        build_arbitration_overflow_response("arbitration", malformed_request)


def test_wire_model_fixture_rejects_a_missing_required_source_literal(tmp_path):
    plan, _caps = _wire_ready_plan(tmp_path)
    leaf = plan["assessment_leaves"][0]
    request = copy.deepcopy(leaf["request"])
    expanded_wrapper = expand_semantic_request_wire({
        "topic_synthesis": request["topic_synthesis"],
        "semantic_source_wire": request["semantic_source_wire"],
    })
    assessment_request = _expand_row_tables(
        expanded_wrapper["topic_synthesis"][0]["assessment_request"],
    )
    critical_wire = assessment_request["critical_view_wire"]
    first_view = critical_wire["views"][0]
    source_bundle = critical_wire["source_literal_bundle_catalog"][first_view["source_literal_bundle_ref"]]
    source_claims = source_bundle["literal_critical_detail"]["source_claims"]
    assert source_claims
    source_claims.pop(0)
    packed_semantic = pack_semantic_request_wire({
        "topic_synthesis": [{"assessment_request": _pack_row_tables(assessment_request)}],
    })
    request["topic_synthesis"] = packed_semantic["topic_synthesis"]
    request["semantic_source_wire"] = packed_semantic["semantic_source_wire"]

    with pytest.raises(ValueError, match="could not read the supplied wire"):
        build_arbitration_overflow_response("arbitration", request)


def test_visible_assessment_reducer_selector_and_single_u10_followup_validate(tmp_path):
    inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, slot_count=2, caps=aggregation_caps(input_cap=14_500), claim_length=1_000,
    )
    state = {"selector_mode": "detail_once"}
    leaf_results = {}
    view_ids = []
    for leaf in plan["assessment_leaves"]:
        for candidate in leaf["request"]["candidate_units"]:
            for unit in candidate["unit_rows"]:
                for view in unit["decision_views"]:
                    if view.get("decision_view_id"):
                        view_ids.append(view["decision_view_id"])
    if view_ids:
        state["view_overrides"] = {
            view_ids[0]: {"assessed_direction": "reverse", "assessed_disagreement": "disagrees"},
        }
    selector_caps = aggregation_caps(input_cap=20_000)

    for leaf in plan["assessment_leaves"]:
        response = build_arbitration_overflow_response("arbitration", leaf["request"], state=state)
        if not leaf_results:
            forged = copy.deepcopy(response)
            forged["candidate_hashes"][plan["candidate_ids"][0]] = "f" * 64
            with pytest.raises(ArbitrationAggregationError):
                validate_arbitration_assessment_response(
                    plan, leaf["node_id"], forged, estimate_output_tokens=aggregation_output,
                )
        leaf_results[leaf["node_id"]] = validate_arbitration_assessment_response(
            plan, leaf["node_id"], response, estimate_output_tokens=aggregation_output,
        )

    ready_by_id = dict(leaf_results)
    reducer_responses = []
    for reducer in plan["reducer_tree"]["nodes"]:
        children = {child_id: ready_by_id[child_id] for child_id in reducer["child_node_ids"]}
        reducer_request = build_arbitration_reducer_request(
            plan, reducer["tree_node_id"], children,
            request_builder=_route_builder, estimate_input_tokens=aggregation_input,
                estimate_output_tokens=aggregation_output, caps=aggregation_caps(input_cap=14_500),
        )
        response = build_arbitration_overflow_response("arbitration", reducer_request["request"], state=state)
        reducer_responses.append(response)
        if not ready_by_id or reducer is plan["reducer_tree"]["nodes"][0]:
            forged = copy.deepcopy(response)
            forged["child_result_hashes"][0] = "f" * 64
            with pytest.raises(ArbitrationAggregationError):
                validate_arbitration_reduction_response(
                    plan, reducer_request, forged, children, estimate_output_tokens=aggregation_output,
                )
        ready_by_id[reducer["tree_node_id"]] = validate_arbitration_reduction_response(
            plan, reducer_request, response, children, estimate_output_tokens=aggregation_output,
        )
    root = ready_by_id[plan["reducer_root_node_id"]]
    root_groups = root.get("group_summaries") or root.get("group_contributions") or []
    assert any(
        "reverse" in (row.get("assessed_direction_values") or [])
        or row.get("assessed_direction") == "reverse"
        for row in root_groups
    )

    selector = build_global_selection_request(
        plan, root, leaf_results,
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
            request_builder=_route_builder, estimate_input_tokens=aggregation_input,
            estimate_output_tokens=aggregation_output, caps=selector_caps,
    )
    first_selection = build_arbitration_overflow_response("arbitration", selector["request"], state=state)
    forged_selection = copy.deepcopy(first_selection)
    forged_selection["candidate_hashes"][plan["candidate_ids"][0]] = "e" * 64
    with pytest.raises(ArbitrationAggregationError):
        validate_global_selection_response(
            plan, selector, forged_selection, estimate_output_tokens=aggregation_output,
        )
    validated_selection = validate_global_selection_response(
        plan, selector, first_selection, estimate_output_tokens=aggregation_output,
    )
    assert validated_selection["outcome"] == "detail_required"

    detail_work = build_global_selection_detail_requests(
        plan, selector, validated_selection, inventory,
        request_builder=_route_builder, estimate_input_tokens=aggregation_input,
        estimate_output_tokens=aggregation_output, caps=selector_caps,
    )
    detail_responses = []
    for request in detail_work["requests"]:
        detail_responses.append({
            "request_id": request["request_id"],
            "response": build_arbitration_overflow_response("arbitration", request["request"], state=state),
        })
    forged_detail = copy.deepcopy(detail_responses)
    forged_detail[0]["response"]["source_fact_hash"] = "d" * 64
    with pytest.raises(ArbitrationAggregationError):
        validate_global_selection_detail_responses(
            plan, selector, validated_selection, detail_work, forged_detail,
            source_inventory=inventory, actual_requests=detail_work["requests"],
            estimate_input_tokens=aggregation_input, estimate_output_tokens=aggregation_output,
            caps=selector_caps,
        )
    details = validate_global_selection_detail_responses(
        plan, selector, validated_selection, detail_work, detail_responses,
        source_inventory=inventory, actual_requests=detail_work["requests"],
        estimate_input_tokens=aggregation_input, estimate_output_tokens=aggregation_output,
        caps=selector_caps,
    )
    final_request = build_global_selection_followup_request(
        plan, selector, validated_selection, detail_work, details, inventory,
        request_builder=_route_builder, estimate_input_tokens=aggregation_input,
        estimate_output_tokens=aggregation_output, caps=selector_caps,
    )
    final_response = build_arbitration_overflow_response("arbitration", final_request["request"], state=state)
    final_selection = validate_global_selection_response(
        plan, final_request, final_response, estimate_output_tokens=aggregation_output,
    )
    assert final_selection["outcome"] == "selected"
    assert final_selection["selected_candidate_id"] in plan["candidate_ids"]
    assert state["selector_detail_started"] is True
    assert state["selector_detail_finished"] is True


def test_selected_navigation_and_optional_merge_detail_are_wire_derived(tmp_path):
    contexts = navigation_contexts(input_limit=24_000)
    sections = [
        _many_claims_section("s1", "shared-task", 5),
        _many_claims_section("s2", "shared-task", 5),
        navigation_section("s3", "other-task"),
    ]
    plan = navigation_plan(sections, contexts=contexts, input_cap=24_000, fnav_max=500, emax=500)
    _default_authority, _default_route, _default_output, _default_root, default_proposal = _finish_navigation_tree(
        plan, {}, contexts,
    )
    assert default_proposal["merge_groups"] == []
    state = {"navigation_mode": "coherent_merge"}
    _authority, _route, _output, root, proposal = _finish_navigation_tree(plan, state, contexts)
    assert len(proposal["merge_groups"]) == 1
    assert proposal["merge_groups"][0]["source_section_ids"] == ["s1", "s2"]
    assert root["candidate_id"] == plan["candidate_id"]

    detail_plan, _slice_results, reduction_roots, final_plan, final_results = _finish_proposed_merge(
        plan, proposal, state, contexts, detail_input_cap=6_000,
    )
    assert detail_plan["counts"]["Qdetail_slice"] > 0
    assert detail_plan["counts"]["Qdetail_final"] == 1
    assert detail_plan["counts"]["Qdetail_reduce"] > 0
    assert set(reduction_roots) == {proposal["merge_groups"][0]["new_section_id"]}
    assert len(final_plan["requests"]) == 1
    coordination = compile_section_coordination(plan, proposal, final_results)
    assert len(coordination["merge_groups"]) == 1
    assert coordination["merge_groups"][0]["source_section_ids"] == ["s1", "s2"]


def test_explicit_adverse_literals_cannot_be_optimistically_overridden():
    request = {
        "schema_version": DETAIL_REQUEST_SCHEMA,
        "stage": "source_detail_slice_assessment",
        "candidate_id": "c1", "candidate_hash": "1" * 64,
        "proposal_hash": "2" * 64, "group_id": "g1", "slice_id": "slice1",
        "source_section_ids": ["s1", "s2"],
        "detail_item_ids": ["fact1"], "detail_item_hashes": {"fact1": "3" * 64},
        "source_critical_literal_binding_refs_hash": "4" * 64,
        "intent_detail_item_ids": [], "intent_detail_item_hashes": {},
        "exact_detail_items": [{"detail_id": "fact1", "value": "A reverse effect and minority result."}],
        "exact_intent_items": [],
        "max_summary_tokens": 4,
    }
    response = build_arbitration_overflow_response(
        "arbitration", request,
        state={"navigation_mode": "coherent_merge", "detail_dispositions": {"fact1": "compatible"}},
    )
    assert response["assessments"][0]["disposition"] == "uncertain"

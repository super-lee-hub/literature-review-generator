from __future__ import annotations

import copy
import hashlib
import json
from itertools import pairwise

import pytest

from outline.candidate_arbitration_navigation import (
    DETAIL_FINAL_REQUEST_SCHEMA,
    DETAIL_KIND,
    DETAIL_REDUCE_REQUEST_SCHEMA,
    DETAIL_REDUCE_RESPONSE_SCHEMA,
    DETAIL_REQUEST_SCHEMA,
    DETAIL_RESPONSE_SCHEMA,
    DETAIL_SLICE_RESPONSE_SCHEMA,
    EXTRACT_REQUEST_SCHEMA,
    EXTRACT_RESPONSE_SCHEMA,
    NAV_REQUEST_SCHEMA,
    NAV_RESPONSE_SCHEMA,
    REDUCE_REQUEST_SCHEMA,
    REDUCE_RESPONSE_SCHEMA,
    SelectedNavigationError,
    build_global_navigation_request,
    build_layout_reduction_requests,
    build_merge_detail_reduction_requests,
    build_merge_group_adjudication_requests,
    build_proposed_merge_detail_requests,
    build_source_detail_index,
    compile_section_coordination,
    compile_selected_candidate_navigation_plan,
    layout_leaf_nodes_from_fragments,
    measure_navigation_output_reserve,
    merge_detail_reduction_response_shape,
    selected_navigation_call_counts,
    validate_all_layout_fragments,
    validate_global_navigation_response,
    validate_layout_fragment,
    validate_layout_reduction_response,
    validate_merge_detail_reduction_response,
    validate_merge_detail_responses,
    validate_merge_group_adjudication_responses,
    validate_source_detail_index,
)
from outline.v3_models import compute_v3_hash
from runtime.provider_context import ProviderContextProfile

KINDS = (
    "arbitration_layout_extract",
    "arbitration_layout_reduce",
    "arbitration_section_navigation",
    DETAIL_KIND,
)


def _contexts(*, max_output: int = 6000, context_limit: int = 80_000, input_limit: int = 24_000):
    return {
        kind: {
            "verified_context_limit": context_limit,
            "max_output_tokens": max_output,
            "reasoning_reserve": 120,
            "safety_margin": 80,
            "route_identity": f"loopback:{kind}:model-v1",
            "effective_input_cap": input_limit,
        }
        for kind in KINDS
    }


def _callbacks(contexts=None, *, input_estimator_fn=None, output_estimator_fn=None):
    contexts = contexts or _contexts()

    def authority(kind, node_id, logical_body):
        return {
            "prompt_id": f"test.{kind}",
            "prompt_sha256": compute_v3_hash({"kind": kind, "body_schema": logical_body.get("schema_version")}),
            "node_id": node_id,
            "role": "selected-only navigation test authority",
        }

    def input_estimator(kind, complete_logical_request):
        if input_estimator_fn is not None:
            return input_estimator_fn(kind, complete_logical_request)
        return {
            "estimated_input_tokens": max(
                1, (len(json.dumps(complete_logical_request, ensure_ascii=False, sort_keys=True).encode("utf-8")) + 3) // 4
            ),
            "route_identity": contexts[kind]["route_identity"],
        }

    def output_estimator(kind, response_shape):
        if output_estimator_fn is not None:
            return output_estimator_fn(kind, response_shape)
        return max(
            1, (len(json.dumps(response_shape, ensure_ascii=False, sort_keys=True).encode("utf-8")) + 3) // 4
        )

    return authority, input_estimator, output_estimator


def _section(section_id: str, task_id: str, *, suffix: str = "") -> dict:
    claim = f"Claim from {section_id}{suffix}"
    paper_key = f"paper-{section_id}"
    return {
        "section_id": section_id,
        "title": f"标题 {section_id} {suffix}".strip(),
        "goal": f"Goal for {section_id} {suffix}".strip(),
        "rationale": f"Rationale {section_id} {suffix}".strip(),
        "task_ids": [task_id],
        "section_hash_aliases": [f"alias-{section_id}"],
        "paper_keys": [paper_key],
        "relation_ids": [f"relation-{section_id}"],
        "claims": [claim],
        "claim_support": [{
            "claim": claim,
            "claim_id": f"claim-id-{section_id}",
            "paper_key": paper_key,
            "source_claim_ids": [f"source-claim-{section_id}"],
            "evidence_ids": [f"evidence-{section_id}"],
            "source_field_ids": [f"source-field-{section_id}"],
            "task_id": task_id,
            "qualifiers": [{"qualifier_id": f"qualifier-{section_id}", "value": "only in this sample"}],
            "locators": [{"locator_id": f"locator-{section_id}", "page": 7}],
        }],
        "paper_roles": {paper_key: {"role": "primary", "provenance": section_id}},
    }


def _many_claims_section(section_id: str, task_id: str, count: int) -> dict:
    section = _section(section_id, task_id)
    paper_key = section["paper_keys"][0]
    section["claims"] = [f"Claim {index} from {section_id}" for index in range(count)]
    section["claim_support"] = [{
        "claim": claim,
        "claim_id": f"{section_id}-claim-{index}",
        "paper_key": paper_key,
        "source_claim_ids": [f"{section_id}-source-{index}"],
        "evidence_ids": [f"{section_id}-evidence-{index}"],
        "source_field_ids": [f"{section_id}-field-{index}"],
        "task_id": task_id,
        "qualifiers": [{"qualifier_id": f"{section_id}-qualifier-{index}", "value": f"q{index}"}],
        "locators": [{"locator_id": f"{section_id}-locator-{index}", "page": index + 1}],
    } for index, claim in enumerate(section["claims"])]
    return section


def _plan(sections=None, *, candidate_id="candidate-selected", intent_fields=None, contexts=None, input_cap=24_000,
          fnav_max=500, emax=500, tnav_max=None, tfrag_max=None, tproposal_max=None,
          input_estimator_fn=None, output_estimator_fn=None):
    sections = sections or [_section("s1", "t1"), _section("s2", "t2")]
    authority, route, output = _callbacks(
        contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    return compile_selected_candidate_navigation_plan(
        candidate_id,
        compute_v3_hash({"selected_candidate": candidate_id, "source": "frozen"}),
        sections,
        intent_fields if intent_fields is not None else {
            "title": "Review title", "goal": "Review goal", "rationale": "Review rationale"
        },
        prompt_authority=authority,
        route_estimator=route,
        output_estimator=output,
        context_reserve=contexts or _contexts(),
        input_cap=input_cap,
        fnav_max=fnav_max,
        tnav_max=tnav_max,
        tfrag_max=tfrag_max,
        tproposal_max=tproposal_max,
        emax=emax,
    )


def _validated_fragments(plan, contexts=None, *, input_estimator_fn=None, output_estimator_fn=None):
    _, _, output = _callbacks(
        contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    fragments = []
    for request in plan["layout_extraction_requests"]:
        span = request["body"]["span"]
        response = {
            "schema_version": EXTRACT_RESPONSE_SCHEMA,
            "candidate_id": plan["candidate_id"],
            "candidate_hash": plan["candidate_hash"],
            "span_id": span["span_id"],
            "source_hash": span["source_hash"],
            "utf8_start": span["utf8_start"],
            "utf8_end": span["utf8_end"],
            "text_hash": span["text_hash"],
            "expression": f"layout {span['source_field']} {span['span_id'][-5:]}",
        }
        fragments.append(validate_layout_fragment(
            plan, request, response, output_estimator=output, context_reserve=contexts or _contexts()
        ))
    return fragments


def _finish_tree(
    plan, contexts=None, *, input_estimator_fn=None, output_estimator_fn=None,
    request_capture=None,
):
    authority, route, output = _callbacks(
        contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    fragments = _validated_fragments(
        plan, contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    ready_by_id = {node["node_id"]: node for node in layout_leaf_nodes_from_fragments(plan, fragments)}
    for level in plan["layout_reduction_tree"]["levels"]:
        inputs = [ready_by_id[node_id] for node_id in level["input_node_ids"]]
        level_requests = build_layout_reduction_requests(
            plan, level["level"], inputs,
            prompt_authority=authority, route_estimator=route, output_estimator=output,
            context_reserve=contexts or _contexts(),
            input_cap=plan["input_cap"],
        )
        if request_capture is not None:
            request_capture.extend(copy.deepcopy(level_requests["requests"]))
        reduced_by_id = {}
        for request in level_requests["requests"]:
            body = request["body"]
            children = {node["node_id"]: node for node in inputs}
            child_rows: dict[str, list[dict]] = {}
            intent_rows: dict[str, list[dict]] = {}
            for child_id in body["child_node_ids"]:
                child = children[child_id]
                for row in child["layout_by_section"]:
                    child_rows.setdefault(row["section_id"], []).append(row)
                for row in child["intent_layout"]:
                    intent_rows.setdefault(row["source_field"], []).append(row)
            layout = []
            for scope in plan["section_scope"]:
                rows = child_rows.get(scope["section_id"], [])
                if rows:
                    layout.append({
                        "section_id": scope["section_id"],
                        "section_hash": scope["section_hash"],
                        "position": scope["position"],
                        "task_ids": list(scope["task_ids"]),
                        "hash_aliases": list(scope["hash_aliases"]),
                        "span_ids": [span for row in rows for span in row["span_ids"]],
                        "source_fields": [field for row in rows for field in row["source_fields"]],
                        "expression": " ".join(row["expression"] for row in rows),
                    })
            intent = []
            for field, rows in intent_rows.items():
                intent.append({
                    "source_field": field,
                    "source_hash": rows[0]["source_hash"],
                    "span_ids": [span for row in rows for span in row["span_ids"]],
                    "expression": " ".join(row["expression"] for row in rows),
                })
            response = {
                "schema_version": REDUCE_RESPONSE_SCHEMA,
                "candidate_id": plan["candidate_id"],
                "candidate_hash": plan["candidate_hash"],
                "node_id": request["request_id"],
                "child_node_ids": list(body["child_node_ids"]),
                "child_hashes": list(body["child_hashes"]),
                "span_ids": list(body["expected_span_ids"]),
                "layout_by_section": layout,
                "intent_layout": intent,
            }
            reduced_by_id[request["request_id"]] = validate_layout_reduction_response(
                plan, request, response, input_nodes=[children[item] for item in body["child_node_ids"]],
                output_estimator=output, context_reserve=contexts or _contexts(),
            )
        next_by_id = dict(reduced_by_id)
        for carry_id in level_requests["carry_node_ids"]:
            next_by_id[carry_id] = ready_by_id[carry_id]
        ready_by_id = next_by_id
    root = ready_by_id[plan["layout_reduction_tree"]["root_node_id"]]
    return root, fragments


def _navigation_request_and_proposal(
    plan, *, sections_order=None, merge_groups=None, contexts=None,
    input_estimator_fn=None, output_estimator_fn=None,
):
    contexts = contexts or _contexts()
    authority, route, output = _callbacks(
        contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    root, fragments = _finish_tree(
        plan, contexts,
        input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    request = build_global_navigation_request(
        plan, root, prompt_authority=authority, route_estimator=route,
        output_estimator=output,
        context_reserve=contexts, input_cap=plan["input_cap"],
    )
    response = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "root_node_id": request["body"]["root_node_id"],
        "root_hash": request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"],
        "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": sections_order or [row["section_id"] for row in reversed(plan["section_scope"])],
        "merge_groups": merge_groups or [],
    }
    proposal = validate_global_navigation_response(
        plan, request, response, output_estimator=output, context_reserve=contexts
    )
    return request, proposal, root, fragments


def test_long_bilingual_intent_is_split_into_exact_utf8_spans_and_all_sections_reach_root():
    original = [
        _section("s1", "t1", suffix="说明" * 500),
        _section("s2", "t2", suffix="evidence-" * 280),
        _section("s3", "t3", suffix="结论" * 450),
    ]
    before = copy.deepcopy(original)
    intent = {
        "title": "研究标题：" + "消费者跨情境公平感 " * 500,
        "rationale": "Rationale / 理由 — " * 500,
        "goal": "比较多任务边界。" * 400,
    }
    intent_before = copy.deepcopy(intent)
    plan = _plan(original, intent_fields=intent, input_cap=1600, fnav_max=4000)
    assert original == before
    assert intent == intent_before
    assert plan["counts"]["Fnav"] == len(plan["intent_span_manifest"])
    assert plan["counts"]["Jnav"] <= plan["counts"]["Fnav"]
    assert plan["counts"]["Anav"] <= max(0, plan["counts"]["Fnav"] - 1)
    assert plan["counts"]["Qglobal"] == 1
    assert plan["bounds"]["Tnav"] > 0 and plan["bounds"]["Tfrag"] > 0 and plan["bounds"]["Tproposal"] > 0
    grouped = {}
    for span in plan["layout_extraction_requests"]:
        info = span["body"]["span"]
        assert info["utf8_end"] - info["utf8_start"] == len(info["text"].encode("utf-8"))
        info["text"].encode("utf-8").decode("utf-8")
        grouped.setdefault((info["scope_id"], info["source_field"]), []).append(info)
        assert span["estimate"]["estimated_input_tokens"] <= span["estimate"]["effective_input_cap"]
    for (scope_id, field_name), rows in grouped.items():
        rows.sort(key=lambda row: row["utf8_start"])
        assert rows[0]["utf8_start"] == 0
        assert all(a["utf8_end"] == b["utf8_start"] for a, b in pairwise(rows))
        if scope_id == "intent":
            key = field_name.removeprefix("intent.")
            expected = intent[key] if isinstance(intent[key], str) else json.dumps(intent[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            section = next(row for row in original if row["section_id"] == scope_id)
            field = field_name.removeprefix("section.")
            expected_value = section.get(field, [])
            expected = expected_value if isinstance(expected_value, str) else json.dumps(expected_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert "".join(row["text"] for row in rows) == expected
    root, fragments = _finish_tree(plan)
    assert {row["section_id"] for row in root["layout_by_section"]} == {"s1", "s2", "s3"}
    request = build_global_navigation_request(
        plan, root, prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1],
        output_estimator=_callbacks()[2],
        context_reserve=_contexts(), input_cap=plan["input_cap"],
    )
    assert request["body"]["candidate_id"] == "candidate-selected"
    assert [row["section_id"] for row in request["body"]["section_scope"]] == ["s1", "s2", "s3"]
    assert all(row["section_hash"] == plan["source_section_hashes"][row["section_id"]] for row in request["body"]["section_scope"])
    assert fragments


def test_odd_carry_tree_uses_two_disjoint_children_and_exact_union():
    plan = _plan(
        [_section(f"s{i}", f"t{i}") for i in range(1, 4)],
        intent_fields={"title": "long intent " * 20},
    )
    levels = plan["layout_reduction_tree"]["levels"]
    assert levels
    assert any(level["odd_carry_node_id"] for level in levels)
    root, _ = _finish_tree(plan)
    assert root["span_ids"] == [row["span_id"] for row in plan["intent_span_manifest"]]
    assert len(root["span_ids"]) == len(set(root["span_ids"]))
    assert {row["section_id"] for row in root["layout_by_section"]} == {"s1", "s2", "s3"}
    assert selected_navigation_call_counts(plan) == {
        "Jnav": plan["counts"]["Jnav"], "Anav": plan["counts"]["Anav"], "Qglobal": 1,
        "Qnav": plan["counts"]["Jnav"] + plan["counts"]["Anav"] + 1,
        "Qmerge_detail": 0, "Qtotal": plan["counts"]["Jnav"] + plan["counts"]["Anav"] + 1,
    }


def test_single_section_candidate_needs_no_navigation_or_provider_calls():
    plan = _plan([_section("only", "t1")], intent_fields={"title": "single section"})
    assert plan["requires_coordination"] is False
    assert plan["counts"] == {"Fnav": 0, "Jnav": 0, "Anav": 0, "Qglobal": 0, "Qmerge_detail": 0}
    assert plan["layout_extraction_requests"] == []
    assert plan["layout_reduction_tree"]["root_node_id"] is None
    assert selected_navigation_call_counts(plan)["Qtotal"] == 0


def test_multi_section_navigation_requires_complete_exact_original_review_intent_fields():
    with pytest.raises(SelectedNavigationError, match="complete exact original ReviewIntent"):
        _plan(intent_fields={})


def test_c12_selection_compiles_only_one_winner_navigation_scope():
    plan = _plan(
        [_section("left", "task-left"), _section("right", "task-right")],
        candidate_id="configured-c12-winner-09",
    )
    for request in plan["layout_extraction_requests"]:
        assert request["body"]["candidate_id"] == "configured-c12-winner-09"
        assert "candidate_ids" not in request["body"]
        assert "candidate_contents" not in request["body"]
    request, _proposal, _root, _fragments = _navigation_request_and_proposal(plan)
    assert request["body"]["candidate_id"] == "configured-c12-winner-09"
    assert "candidate_ids" not in request["body"]
    assert len(request["body"]["section_scope"]) == 2


def test_positive_derived_text_bounds_near_output_cap_and_context_fails_closed():
    tight_output = _contexts(max_output=900, input_limit=3000, context_limit=20_000)
    plan = _plan(contexts=tight_output, input_cap=3000, fnav_max=100)
    assert 0 < plan["bounds"]["Tfrag"]
    assert 0 < plan["bounds"]["Tproposal"]
    assert 0 < plan["bounds"]["Tnav"]
    too_small_context = _contexts(max_output=500, input_limit=4000, context_limit=100)
    with pytest.raises(SelectedNavigationError, match="context"):
        _plan(contexts=too_small_context, input_cap=4000)


def test_extraction_omission_duplicate_foreign_hash_and_response_fields_fail_closed():
    plan = _plan()
    fragments = _validated_fragments(plan)
    with pytest.raises(SelectedNavigationError, match="coverage differs"):
        validate_all_layout_fragments(plan, fragments[:-1])
    with pytest.raises(SelectedNavigationError, match="duplicate"):
        validate_all_layout_fragments(plan, [*fragments, fragments[0]])
    request = plan["layout_extraction_requests"][0]
    span = request["body"]["span"]
    bad = {
        "schema_version": EXTRACT_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "span_id": span["span_id"], "source_hash": "f" * 64,
        "utf8_start": span["utf8_start"], "utf8_end": span["utf8_end"],
        "text_hash": span["text_hash"], "expression": "layout",
    }
    with pytest.raises(SelectedNavigationError, match="source_hash"):
        validate_layout_fragment(plan, request, bad, output_estimator=_callbacks()[2], context_reserve=_contexts())
    bad["source_hash"] = span["source_hash"]
    bad["extra"] = "must fail closed"
    with pytest.raises(SelectedNavigationError, match="unknown or missing fields"):
        validate_layout_fragment(plan, request, bad, output_estimator=_callbacks()[2], context_reserve=_contexts())


def test_global_order_and_hash_map_reject_omissions_duplicates_and_foreign_hashes():
    plan = _plan()
    request, _proposal, root, _ = _navigation_request_and_proposal(plan)
    base = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "root_node_id": request["body"]["root_node_id"], "root_hash": request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"], "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": ["s2", "s1"], "merge_groups": [],
    }
    with pytest.raises(SelectedNavigationError, match="incomplete"):
        validate_global_navigation_response(plan, request, {**base, "section_order": ["s1"]}, output_estimator=_callbacks()[2], context_reserve=_contexts())
    group = {
        "source_section_ids": ["s1", "s2"],
        "source_section_hashes": {"s1": plan["source_section_hashes"]["s1"], "s2": "e" * 64},
        "title": "Merged", "goal": "Shared goal", "integration_reason": "Overlapping purpose",
    }
    with pytest.raises(SelectedNavigationError, match="foreign source-section hashes"):
        validate_global_navigation_response(plan, request, {**base, "merge_groups": [group]}, output_estimator=_callbacks()[2], context_reserve=_contexts())
    group["source_section_hashes"]["s2"] = plan["source_section_hashes"]["s2"]
    with pytest.raises(SelectedNavigationError, match="duplicate"):
        validate_global_navigation_response(plan, request, {**base, "section_order": ["s1", "s1"], "merge_groups": [group]}, output_estimator=_callbacks()[2], context_reserve=_contexts())
    assert root["candidate_id"] == plan["candidate_id"]


def _cross_boundary_proposal(plan):
    request, _proposal, root, fragments = _navigation_request_and_proposal(plan)
    source_ids = ["s1", "s3"]
    source_hashes = {section_id: plan["source_section_hashes"][section_id] for section_id in source_ids}
    raw = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "root_node_id": request["body"]["root_node_id"], "root_hash": request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"], "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": ["s1", "s3", "s2"],
        "merge_groups": [{
            "source_section_ids": source_ids,
            "source_section_hashes": source_hashes,
            "title": "Shared structure", "goal": "Integrate two cross-boundary sections",
            "integration_reason": "The selected sections share an organizing purpose.",
        }],
    }
    proposal = validate_global_navigation_response(
        plan, request, raw, output_estimator=_callbacks()[2], context_reserve=_contexts()
    )
    return request, proposal, root, fragments


def _slice_responses_for(plan, proposal, detail_plan, disposition="compatible"):
    slice_responses = []
    for request in detail_plan["requests"]:
        body = request["body"]
        slice_responses.append({
            "request_id": request["request_id"],
            "response": {
                "schema_version": DETAIL_SLICE_RESPONSE_SCHEMA,
                "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
                "proposal_hash": proposal["proposal_hash"], "group_id": request["group_id"],
                "slice_id": body["slice_id"],
                "detail_item_ids": list(body["detail_item_ids"]),
                "detail_item_hashes": copy.deepcopy(body["detail_item_hashes"]),
                "critical_literal_bindings_hash": body["source_critical_literal_binding_refs_hash"],
                "intent_context_hash": compute_v3_hash({
                    key: body[key] for key in ("intent_detail_item_ids", "intent_detail_item_hashes")
                }),
                "assessments": [
                    {"detail_id": detail_id, "value_hash": body["detail_item_hashes"][detail_id], "disposition": disposition}
                    for detail_id in body["detail_item_ids"]
                ],
                "summary": "Slice preserves its exact selected-source support and qualifiers.",
            },
        })
    return slice_responses


def _complete_group_adjudication(
    plan, proposal, detail_index, *, decision="accept", input_cap=None, slice_input_cap=None,
    contexts=None, input_estimator_fn=None, output_estimator_fn=None, request_capture=None,
):
    contexts = contexts or _contexts()
    authority, route, output = _callbacks(
        contexts, input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
    )
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index, prompt_authority=authority, route_estimator=route,
        output_estimator=output, context_reserve=contexts,
        input_cap=slice_input_cap or input_cap or plan["input_cap"],
        emax=plan["emax"],
    )
    if request_capture is not None:
        request_capture.extend(copy.deepcopy(detail_plan["requests"]))
    slice_responses = _slice_responses_for(plan, proposal, detail_plan)
    slice_results = validate_merge_detail_responses(
        plan, proposal, detail_plan, slice_responses, detail_index=detail_index,
        output_estimator=output, context_reserve=contexts,
    )
    reduction_roots = {}
    slices_by_group = slice_results["groups"]
    for group_id, tree in detail_plan["adjudication_trees"].items():
        ready_by_id = {row["node_id"]: row for row in slices_by_group[group_id]["slice_nodes"]}
        for level_plan in tree["levels"]:
            level = level_plan["level"]
            ready_nodes = [ready_by_id[node_id] for node_id in level_plan["input_node_ids"]]
            level_plan_requests = build_merge_detail_reduction_requests(
                plan, proposal, detail_plan, slice_results, group_id, level, ready_nodes,
                detail_index=detail_index, prompt_authority=authority, route_estimator=route,
                output_estimator=output, context_reserve=contexts, input_cap=input_cap or plan["input_cap"],
            )
            if request_capture is not None:
                request_capture.extend(copy.deepcopy(level_plan_requests["requests"]))
            reduced_by_id = {}
            children = {row["node_id"]: row for row in ready_nodes}
            for request in level_plan_requests["requests"]:
                body = request["body"]
                child_rows = [children[node_id] for node_id in body["child_node_ids"]]
                dispositions = {row["disposition"] for row in child_rows}
                disposition = "conflict" if "conflict" in dispositions else (
                    "uncertain" if "uncertain" in dispositions else "compatible"
                )
                response = {
                    "schema_version": DETAIL_REDUCE_RESPONSE_SCHEMA,
                    "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
                    "proposal_hash": proposal["proposal_hash"], "group_id": group_id,
                    "node_id": request["request_id"], "level": level,
                    "child_node_ids": list(body["child_node_ids"]),
                    "child_hashes": list(body["child_hashes"]),
                    "detail_item_ids": list(body["detail_item_ids"]),
                    "detail_item_hashes": copy.deepcopy(body["detail_item_hashes"]),
                    "critical_literal_bindings_hash": body["critical_literal_bindings_hash"],
                    "disposition": disposition,
                    "summary": "Combined slice summaries preserve exact item and qualifier bindings.",
                }
                node = validate_merge_detail_reduction_response(
                    plan, proposal, detail_plan, slice_results, request, response, child_rows,
                    output_estimator=output, context_reserve=contexts,
                )
                reduced_by_id[request["request_id"]] = node
            next_by_id = dict(reduced_by_id)
            for carry_id in level_plan_requests["carry_node_ids"]:
                next_by_id[carry_id] = ready_by_id[carry_id]
            ready_by_id = next_by_id
        reduction_roots[group_id] = ready_by_id[tree["root_node_id"]]
    root, _fragments = _finish_tree(
        plan, contexts,
        input_estimator_fn=input_estimator_fn, output_estimator_fn=output_estimator_fn,
        request_capture=request_capture,
    )
    final_plan = build_merge_group_adjudication_requests(
        plan, proposal, detail_plan, slice_results, reduction_roots, root, detail_index,
        prompt_authority=authority, route_estimator=route, output_estimator=output,
        context_reserve=contexts, input_cap=input_cap or plan["input_cap"],
    )
    if request_capture is not None:
        request_capture.extend(copy.deepcopy(final_plan["requests"]))
    final_responses = []
    for request in final_plan["requests"]:
        body = request["body"]
        root_row = body["detail_root"]
        final_responses.append({
            "request_id": request["request_id"],
            "response": {
                "schema_version": DETAIL_RESPONSE_SCHEMA,
                "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
                "proposal_hash": proposal["proposal_hash"], "group_id": request["group_id"],
                "detail_root_hash": root_row["node_hash"],
                "detail_item_ids": list(root_row["detail_item_ids"]),
                "detail_item_hashes": copy.deepcopy(root_row["detail_item_hashes"]),
                "critical_literal_bindings_hash": root_row["critical_literal_bindings_hash"],
                "decision": decision,
                "source_contributions": {sid: f"Contribution for {sid}" for sid in body["source_section_ids"]},
            },
        })
    final_results = validate_merge_group_adjudication_responses(
        plan, proposal, detail_plan, final_plan, final_responses,
        output_estimator=output, context_reserve=contexts,
    )
    return detail_plan, slice_results, reduction_roots, final_plan, final_results


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_cross_boundary_merge_retrieves_exact_full_detail_and_compiles_accept_or_expansion(decision):
    sections = [_section("s1", "task-A"), _section("s2", "task-B"), _section("s3", "task-C")]
    source_before = copy.deepcopy(sections)
    plan = _plan(sections, intent_fields={"title": "Global review title", "goal": "Global review goal"})
    _request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    detail_plan, _slice_results, _reduction_roots, _final_plan, detail_results = _complete_group_adjudication(
        plan, proposal, detail_index, decision=decision
    )
    assert detail_plan["counts"]["Qmerge_detail"] == (
        detail_plan["counts"]["Qdetail_slice"] + detail_plan["counts"]["Qdetail_reduce"]
        + detail_plan["counts"]["Qdetail_final"]
    )
    assert 0 < detail_plan["counts"]["Qmerge_detail"] <= plan["emax"]
    expected_ids = []
    for request in detail_plan["requests"]:
        body = request["body"]
        assert body["source_section_ids"] == ["s1", "s3"]
        assert all(row["scope_id"] in {"s1", "s3"} for row in body["exact_detail_items"])
        assert {row["scope_id"] for row in body["exact_detail_items"]} >= {"s1", "s3"}
        assert {row["scope_id"] for row in body["exact_intent_items"]} == {"intent"}
        expected_ids.extend(body["detail_item_ids"])
    assert len(expected_ids) == len(set(expected_ids))
    assert "s2" not in {row["scope_id"] for req in detail_plan["requests"] for row in req["body"]["exact_detail_items"]}
    counts = selected_navigation_call_counts(plan, detail_plan)
    assert counts["Qmerge_detail"] == detail_plan["counts"]["Qmerge_detail"]
    coordination = compile_section_coordination(plan, proposal, detail_results)
    assert sections == source_before
    if decision == "accept":
        assert coordination["section_order"] == [proposal["merge_groups"][0]["new_section_id"], "s2"]
        assert len(coordination["merge_groups"]) == 1
        assert coordination["merge_groups"][0]["canonical_intent"]["source_intents"]["s1"]["title"] == sections[0]["title"]
        assert coordination["merge_groups"][0]["source_section_hashes"] == {
            "s1": compute_v3_hash(sections[0]), "s3": compute_v3_hash(sections[2]),
        }
        from outline.v3_executor import OutlineV3Executor
        merged, audit = OutlineV3Executor._apply_section_coordination(
            plan["candidate_id"], sections, coordination, parent_hash=plan["candidate_hash"]
        )
        assert [row["section_id"] for row in merged] == [proposal["merge_groups"][0]["new_section_id"], "s2"]
        merged_row = merged[0]
        expected_support = sections[0]["claim_support"] + sections[2]["claim_support"]
        assert merged_row["claim_support"] == expected_support
        assert merged_row["claim_support"][0]["qualifiers"] == sections[0]["claim_support"][0]["qualifiers"]
        assert merged_row["claim_support"][1]["locators"] == sections[2]["claim_support"][0]["locators"]
        assert audit["status"] == "applied"
    else:
        assert coordination["section_order"] == ["s1", "s3", "s2"]
        assert coordination["merge_groups"] == []


def test_source_detail_index_tamper_omission_duplicates_and_foreign_candidate_are_rejected():
    plan = _plan()
    index = build_source_detail_index(plan)
    with pytest.raises(SelectedNavigationError, match="index hash changed"):
        validate_source_detail_index(plan, {**index, "items": index["items"][:-1]})
    duplicate = copy.deepcopy(index)
    duplicate["items"].append(copy.deepcopy(duplicate["items"][0]))
    duplicate["index_hash"] = compute_v3_hash({key: value for key, value in duplicate.items() if key != "index_hash"})
    with pytest.raises(SelectedNavigationError, match="duplicate"):
        validate_source_detail_index(plan, duplicate)
    foreign = copy.deepcopy(index)
    foreign["candidate_hash"] = "f" * 64
    foreign["index_hash"] = compute_v3_hash({key: value for key, value in foreign.items() if key != "index_hash"})
    with pytest.raises(SelectedNavigationError, match="foreign"):
        validate_source_detail_index(plan, foreign)


def test_multiple_global_merge_proposals_include_exact_intent_in_every_detail_call():
    plan = _plan([_section(f"s{i}", f"task-{i}") for i in range(1, 5)], emax=1000)
    request, _proposal, _root, _fragments = _navigation_request_and_proposal(plan)
    pairs = [["s1", "s4"], ["s2", "s3"]]
    groups = []
    for pair in pairs:
        hashes = {section_id: plan["source_section_hashes"][section_id] for section_id in pair}
        groups.append({
            "source_section_ids": pair,
            "source_section_hashes": hashes,
            "title": "Group title", "goal": "Group goal", "integration_reason": "Group reason",
        })
    raw = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "root_node_id": request["body"]["root_node_id"], "root_hash": request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"], "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": ["s1", "s4", "s2", "s3"], "merge_groups": groups,
    }
    proposal = validate_global_navigation_response(
        plan, request, raw, output_estimator=_callbacks()[2], context_reserve=_contexts()
    )
    index = build_source_detail_index(plan)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, index, prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1],
        output_estimator=_callbacks()[2], context_reserve=_contexts(),
        input_cap=plan["input_cap"], emax=plan["emax"],
    )
    intent_ids = {row["detail_id"] for row in index["items"] if row["scope_id"] == "intent"}
    observed_intent_ids = [
        detail_id
        for request_row in detail_plan["requests"]
        for detail_id in request_row["body"]["intent_detail_item_ids"]
        if detail_id in intent_ids
    ]
    assert set(observed_intent_ids) == intent_ids
    assert len(observed_intent_ids) == len(intent_ids) * len(detail_plan["requests"])
    assert detail_plan["counts"]["Qmerge_detail"] <= plan["emax"]
    assert selected_navigation_call_counts(plan, detail_plan)["Qmerge_detail"] == detail_plan["counts"]["Qmerge_detail"]


def test_merge_detail_response_and_request_tamper_omission_duplicates_and_foreign_hashes_fail_closed():
    plan = _plan([_section("s1", "task-A"), _section("s2", "task-B"), _section("s3", "task-C")])
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index,
        prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1], output_estimator=_callbacks()[2],
        context_reserve=_contexts(),
        input_cap=plan["input_cap"], emax=plan["emax"],
    )
    assert len(detail_plan["requests"]) == 1
    request = detail_plan["requests"][0]
    body = request["body"]
    valid_slices = _slice_responses_for(plan, proposal, detail_plan)
    validator = {
        "detail_index": detail_index, "output_estimator": _callbacks()[2],
        "context_reserve": _contexts(),
    }
    with pytest.raises(SelectedNavigationError, match="missing, duplicate, or foreign nodes"):
        validate_merge_detail_responses(plan, proposal, detail_plan, [], **validator)
    with pytest.raises(SelectedNavigationError, match="missing, duplicate, or foreign nodes"):
        validate_merge_detail_responses(plan, proposal, detail_plan, [valid_slices[0], valid_slices[0]], **validator)
    bad_hash = copy.deepcopy(valid_slices[0])
    bad_hash["response"]["detail_item_hashes"][body["detail_item_ids"][0]] = "f" * 64
    with pytest.raises(SelectedNavigationError, match="changed detail_item_hashes"):
        validate_merge_detail_responses(plan, proposal, detail_plan, [bad_hash], **validator)
    omitted_item_request = copy.deepcopy(detail_plan["requests"])
    omitted_item_request[0]["body"]["exact_detail_items"].pop()
    with pytest.raises(SelectedNavigationError, match="measured request|body differs"):
        validate_merge_detail_responses(
            plan, proposal, detail_plan, valid_slices, actual_requests=omitted_item_request, **validator
        )


def test_partial_detail_slices_cannot_accept_a_group_they_did_not_all_see():
    def many_rows(section_id, task_id, count):
        return _many_claims_section(section_id, task_id, count)

    detail_contexts = _contexts(input_limit=32_000)
    plan = _plan(
        [many_rows("s1", "task-A", 16), _section("s2", "task-B"), many_rows("s3", "task-C", 16)],
        contexts=detail_contexts, input_cap=32_000, emax=2000,
    )
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index, prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1],
        output_estimator=_callbacks(detail_contexts)[2], context_reserve=detail_contexts,
        input_cap=2500, emax=plan["emax"],
    )
    assert len(detail_plan["requests"]) > 1
    partial_accepts = []
    for request in detail_plan["requests"]:
        body = request["body"]
        partial_accepts.append({
            "request_id": request["request_id"],
            "response": {
                "schema_version": DETAIL_RESPONSE_SCHEMA,
                "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
                "proposal_hash": proposal["proposal_hash"], "group_id": request["group_id"],
                "detail_item_ids": list(body["detail_item_ids"]),
                "detail_item_hashes": copy.deepcopy(body["detail_item_hashes"]),
                "decision": "accept",
                # This is precisely the forbidden claim: one slice certifies
                # a whole-group decision and contributions it did not inspect.
                "source_contributions": {"s1": "partial", "s3": "partial"},
            },
        })
    with pytest.raises(SelectedNavigationError, match="slice assessment"):
        validate_merge_detail_responses(
            plan, proposal, detail_plan, partial_accepts, detail_index=detail_index,
            output_estimator=_callbacks()[2], context_reserve=_contexts(),
        )
    complete_plan, _slices, _roots, _final_plan, complete_results = _complete_group_adjudication(
        plan, proposal, detail_index, decision="accept", slice_input_cap=2500,
        input_cap=plan["input_cap"], contexts=detail_contexts,
    )
    assert complete_plan["counts"]["Qdetail_slice"] > 1
    assert complete_plan["counts"]["Qdetail_reduce"] > 0
    assert complete_plan["counts"]["Qdetail_final"] == 1
    assert complete_plan["counts"]["Qmerge_detail"] <= plan["emax"]
    coordination = compile_section_coordination(plan, proposal, complete_results)
    assert len(coordination["merge_groups"]) == 1
    from outline.v3_executor import OutlineV3Executor
    merged, _audit = OutlineV3Executor._apply_section_coordination(
        plan["candidate_id"], sections=[plan["source_sections"]["s1"], plan["source_sections"]["s2"], plan["source_sections"]["s3"]],
        coordination=coordination, parent_hash=plan["candidate_hash"],
    )
    merged_group = merged[0]
    assert len(merged_group["claim_support"]) == 32


def test_detail_reducer_rejects_self_rehashed_node_that_erases_sealed_slice_conflict():
    contexts = _contexts(input_limit=32_000)
    plan = _plan([
        _many_claims_section("s1", "task-A", 16),
        _section("s2", "task-B"),
        _many_claims_section("s3", "task-C", 16),
    ], contexts=contexts, input_cap=32_000, emax=2000)
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    authority, route, output = _callbacks(contexts)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index, prompt_authority=authority, route_estimator=route,
        output_estimator=output, context_reserve=contexts, input_cap=2500, emax=plan["emax"],
    )
    slice_results = validate_merge_detail_responses(
        plan, proposal, detail_plan, _slice_responses_for(plan, proposal, detail_plan, "conflict"),
        detail_index=detail_index, output_estimator=output, context_reserve=contexts,
    )
    group_id, tree = next(iter(detail_plan["adjudication_trees"].items()))
    assert len(tree["levels"]) > 1
    level_zero = tree["levels"][0]
    sealed_nodes = {row["node_id"]: row for row in slice_results["groups"][group_id]["slice_nodes"]}
    first = sealed_nodes[level_zero["input_node_ids"][0]]
    assert first["disposition"] == "conflict"
    forged = copy.deepcopy(first)
    forged["assessments"] = {detail_id: "compatible" for detail_id in forged["detail_item_ids"]}
    forged["disposition"] = "compatible"
    forged["node_hash"] = compute_v3_hash({key: value for key, value in forged.items() if key != "node_hash"})
    ready_nodes = [forged if node_id == first["node_id"] else sealed_nodes[node_id]
                   for node_id in level_zero["input_node_ids"]]
    with pytest.raises(SelectedNavigationError, match="exact validated slice|validator result"):
        build_merge_detail_reduction_requests(
            plan, proposal, detail_plan, slice_results, group_id, 0, ready_nodes,
            detail_index=detail_index, prompt_authority=authority, route_estimator=route,
            output_estimator=output, context_reserve=contexts, input_cap=plan["input_cap"],
        )


def test_detail_reducer_rejects_self_rehashed_node_that_erases_prior_validator_conflict():
    contexts = _contexts(input_limit=32_000)
    plan = _plan([
        _many_claims_section("s1", "task-A", 16),
        _section("s2", "task-B"),
        _many_claims_section("s3", "task-C", 16),
    ], contexts=contexts, input_cap=32_000, emax=2000)
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    authority, route, output = _callbacks(contexts)
    detail_plan = build_proposed_merge_detail_requests(
        plan, proposal, detail_index, prompt_authority=authority, route_estimator=route,
        output_estimator=output, context_reserve=contexts, input_cap=2500, emax=plan["emax"],
    )
    slice_results = validate_merge_detail_responses(
        plan, proposal, detail_plan, _slice_responses_for(plan, proposal, detail_plan, "conflict"),
        detail_index=detail_index, output_estimator=output, context_reserve=contexts,
    )
    group_id, tree = next(iter(detail_plan["adjudication_trees"].items()))
    assert len(tree["levels"]) > 1
    level_zero = tree["levels"][0]
    slice_nodes = {row["node_id"]: row for row in slice_results["groups"][group_id]["slice_nodes"]}
    level_zero_plan = build_merge_detail_reduction_requests(
        plan, proposal, detail_plan, slice_results, group_id, 0,
        [slice_nodes[node_id] for node_id in level_zero["input_node_ids"]],
        detail_index=detail_index, prompt_authority=authority, route_estimator=route,
        output_estimator=output, context_reserve=contexts, input_cap=plan["input_cap"],
    )
    reduced_by_id = {}
    for request in level_zero_plan["requests"]:
        child_rows = [slice_nodes[node_id] for node_id in request["child_node_ids"]]
        body = request["body"]
        reduction_response = {
            "schema_version": DETAIL_REDUCE_RESPONSE_SCHEMA,
            "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
            "proposal_hash": proposal["proposal_hash"], "group_id": group_id,
            "node_id": request["request_id"], "level": 0,
            "child_node_ids": list(body["child_node_ids"]),
            "child_hashes": list(body["child_hashes"]),
            "detail_item_ids": list(body["detail_item_ids"]),
            "detail_item_hashes": copy.deepcopy(body["detail_item_hashes"]),
            "critical_literal_bindings_hash": body["critical_literal_bindings_hash"],
            "disposition": "conflict", "summary": "The exact source group contains conflicts.",
        }
        reduced_by_id[request["request_id"]] = validate_merge_detail_reduction_response(
            plan, proposal, detail_plan, slice_results, request, reduction_response, child_rows,
            output_estimator=output, context_reserve=contexts,
        )
    level_one = tree["levels"][1]
    ready_by_id = dict(reduced_by_id)
    for node_id in level_zero_plan["carry_node_ids"]:
        ready_by_id[node_id] = slice_nodes[node_id]
    first_reducer_id = next(node_id for node_id in level_one["input_node_ids"] if node_id in reduced_by_id)
    forged = reduced_by_id[first_reducer_id].to_dict()
    forged["assessments"] = {detail_id: "compatible" for detail_id in forged["detail_item_ids"]}
    forged["disposition"] = "compatible"
    forged["node_hash"] = compute_v3_hash({key: value for key, value in forged.items() if key != "node_hash"})
    ready_by_id[first_reducer_id] = forged
    with pytest.raises(SelectedNavigationError, match="prior sealed validator"):
        build_merge_detail_reduction_requests(
            plan, proposal, detail_plan, slice_results, group_id, 1,
            [ready_by_id[node_id] for node_id in level_one["input_node_ids"]],
            detail_index=detail_index, prompt_authority=authority, route_estimator=route,
            output_estimator=output, context_reserve=contexts, input_cap=plan["input_cap"],
        )


def test_final_group_request_reserves_complete_response_schema_before_post():
    contexts = _contexts()
    contexts[DETAIL_KIND]["max_output_tokens"] = 4000
    sections = [_section("s1", "task-A"), _section("s2", "task-B"), _section("s3", "task-C")]
    plan = _plan(sections, contexts=contexts)
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    _detail_plan, _slices, _roots, final_plan, _results = _complete_group_adjudication(
        plan, proposal, detail_index, contexts=contexts,
    )
    request = final_plan["requests"][0]
    reserve = request["measurement"]["output_reserve"]
    assert reserve["response_schema"] == DETAIL_RESPONSE_SCHEMA
    assert reserve["response_shape_hash"]
    assert reserve["estimated_output_tokens"] <= reserve["max_output_tokens"] == 4000
    assert request["body"]["max_contribution_tokens"] < plan["bounds"]["Tproposal"]
    assert request["body"]["max_contribution_tokens"] == reserve["max_contribution_tokens"]
    body = request["body"]
    root = body["detail_root"]
    response_shape = {
        "schema_version": DETAIL_RESPONSE_SCHEMA,
        "candidate_id": body["candidate_id"], "candidate_hash": body["candidate_hash"],
        "proposal_hash": body["proposal_hash"], "group_id": body["group_id"],
        "detail_root_hash": root["node_hash"],
        "detail_item_ids": list(root["detail_item_ids"]),
        "detail_item_hashes": copy.deepcopy(root["detail_item_hashes"]),
        "critical_literal_bindings_hash": root["critical_literal_bindings_hash"],
        "decision": "accept",
        "source_contributions": {
            section_id: " ".join(["x"] * body["max_contribution_tokens"])
            for section_id in body["source_section_ids"]
        },
    }
    assert _callbacks(contexts)[2](DETAIL_KIND, response_shape) == reserve["estimated_output_tokens"]


def test_source_mutation_after_plan_is_rejected_even_if_detail_index_is_rebuilt():
    plan = _plan()
    plan["source_sections"]["s1"]["claims"][0] = "mutated after plan"
    with pytest.raises(SelectedNavigationError, match="source section.*changed|plan.*changed"):
        build_source_detail_index(plan)


def test_validated_rejection_cannot_be_mutated_into_acceptance():
    sections = [_section("s1", "task-A"), _section("s2", "task-B"), _section("s3", "task-C")]
    plan = _plan(sections)
    _nav_request, proposal, _root, _fragments = _cross_boundary_proposal(plan)
    detail_index = build_source_detail_index(plan)
    _detail_plan, _slices, _roots, _final_plan, validated = _complete_group_adjudication(
        plan, proposal, detail_index, decision="reject"
    )
    mutated_copy = validated["groups"]
    mutated_copy[proposal["merge_groups"][0]["new_section_id"]]["decision"] = "accept"
    coordination = compile_section_coordination(plan, proposal, validated)
    assert coordination["merge_groups"] == []
    assert coordination["section_order"] == ["s1", "s3", "s2"]


def test_prompt_attached_navigation_request_tamper_is_detected_even_when_shadow_body_is_unchanged():
    plan = _plan()
    root, _ = _finish_tree(plan)
    request = build_global_navigation_request(
        plan, root, prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1],
        output_estimator=_callbacks()[2],
        context_reserve=_contexts(), input_cap=plan["input_cap"],
    )
    response = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "root_node_id": request["body"]["root_node_id"], "root_hash": request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"], "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": ["s2", "s1"], "merge_groups": [],
    }
    tampered = copy.deepcopy(request)
    tampered["request"]["section_scope"][0]["layout_expression"] = "foreign actual request content"
    with pytest.raises(SelectedNavigationError, match="request hash|body differs"):
        validate_global_navigation_response(
            plan, tampered, response, output_estimator=_callbacks()[2], context_reserve=_contexts()
        )


def test_empty_review_intent_fields_remain_explicit_and_hash_bound_at_global_root():
    plan = _plan(intent_fields={"title": "", "goal": "Review goal"})
    root, _ = _finish_tree(plan)
    request = build_global_navigation_request(
        plan, root, prompt_authority=_callbacks()[0], route_estimator=_callbacks()[1],
        output_estimator=_callbacks()[2],
        context_reserve=_contexts(), input_cap=plan["input_cap"],
    )
    empty_row = next(row for row in request["body"]["intent_layout"] if row["source_field"] == "title")
    assert empty_row["source_hash"] == plan["intent_field_hashes"]["title"]
    assert empty_row["span_ids"] == []
    assert empty_row["expression"] == "(empty source field)"


def test_actual_provider_context_profile_measures_prompt_attached_requests_and_output_shapes():
    profile = ProviderContextProfile(
        provider="local-test", model="context-profile-v1", endpoint_type="chat",
        model_context_limit=60_000, verified_context_limit=60_000, input_budget=20_000,
        max_output_tokens=6_000, reasoning_reserve=0, safety_margin=100,
    )
    contexts = {
        kind: {
            "verified_context_limit": profile.verified_context_limit,
            "max_output_tokens": profile.max_output_tokens,
            "reasoning_reserve": profile.reasoning_reserve,
            "safety_margin": profile.safety_margin,
            "effective_input_cap": profile.input_budget,
            "route_identity": f"{profile.provider}:{profile.model}:{profile.endpoint_type}",
        }
        for kind in KINDS
    }
    def route_estimator(kind, complete_request):
        return profile.estimate_request(complete_request)

    def output_estimator(kind, response_shape):
        return profile.estimate_tokens(response_shape)

    sections = [_section("s1", "t1"), _section("s2", "t2"), _section("s3", "t3")]
    plan = _plan(
        sections, candidate_id="selected-with-provider-context",
        intent_fields={"review_intent": {"title": "Exact title", "goal": "Exact goal", "rationale": "Exact reason"}},
        contexts=contexts, input_cap=profile.input_budget, fnav_max=500, emax=500,
        input_estimator_fn=route_estimator, output_estimator_fn=output_estimator,
    )
    assert plan["bounds"]["Tfrag"] > 0
    assert plan["bounds"]["Tnav"] > 0
    assert plan["bounds"]["Tproposal"] > 0
    assert all(row["measurement"]["estimated_input_tokens"] <= profile.input_budget
               for row in plan["layout_extraction_requests"])
    assert all(row["measurement"]["request_hash"] == row["request_hash"]
               for row in plan["layout_extraction_requests"])

    layout_requests = []
    layout_root, _fragments = _finish_tree(
        plan, contexts, input_estimator_fn=route_estimator,
        output_estimator_fn=output_estimator, request_capture=layout_requests,
    )
    nav_request = build_global_navigation_request(
        plan, layout_root, prompt_authority=_callbacks(contexts)[0],
        route_estimator=route_estimator, output_estimator=output_estimator,
        context_reserve=contexts, input_cap=plan["input_cap"],
    )
    source_ids = ["s1", "s3"]
    nav_response = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "root_node_id": nav_request["body"]["root_node_id"],
        "root_hash": nav_request["body"]["root_hash"],
        "plan_hash": plan["semantic_hash"], "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": ["s1", "s2", "s3"],
        "merge_groups": [{
            "source_section_ids": source_ids,
            "source_section_hashes": {sid: plan["source_section_hashes"][sid] for sid in source_ids},
            "title": "Shared organization", "goal": "Integrate exact selected topics",
            "integration_reason": "The selected source sections share a review purpose.",
        }],
    }
    proposal = validate_global_navigation_response(
        plan, nav_request, nav_response,
        output_estimator=output_estimator, context_reserve=contexts,
    )
    detail_index = build_source_detail_index(plan)
    detail_requests = []
    detail_plan, _slices, _roots, _final, _results = _complete_group_adjudication(
        plan, proposal, detail_index, contexts=contexts,
        input_cap=plan["input_cap"], slice_input_cap=6_000,
        input_estimator_fn=route_estimator, output_estimator_fn=output_estimator,
        request_capture=detail_requests,
    )
    requests = [
        *plan["layout_extraction_requests"], *layout_requests, nav_request, *detail_requests,
    ]
    request_kinds = {
        EXTRACT_REQUEST_SCHEMA: "arbitration_layout_extract",
        REDUCE_REQUEST_SCHEMA: "arbitration_layout_reduce",
        NAV_REQUEST_SCHEMA: "arbitration_section_navigation",
        DETAIL_REQUEST_SCHEMA: DETAIL_KIND,
        DETAIL_REDUCE_REQUEST_SCHEMA: DETAIL_KIND,
        DETAIL_FINAL_REQUEST_SCHEMA: DETAIL_KIND,
    }
    observed_schemas = set()
    for request in requests:
        kind = request_kinds[request["body"]["schema_version"]]
        observed_schemas.add(request["body"]["schema_version"])
        measured_shapes = []

        def measured_output(measured_kind, shape, *, expected_kind=kind, shapes=measured_shapes):
            assert measured_kind == expected_kind
            shapes.append(copy.deepcopy(dict(shape)))
            return profile.estimate_tokens(shape)

        reserve = measure_navigation_output_reserve(
            plan, kind, request, output_estimator=measured_output, context_reserve=contexts,
        )
        assert len(measured_shapes) == 1
        assert reserve == request["measurement"]["output_reserve"]
        assert reserve["response_shape_hash"] == hashlib.sha256(
            json.dumps(
                measured_shapes[0], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        assert reserve["requested_output_tokens"] == reserve["estimated_output_tokens"]
        assert reserve["estimated_output_tokens"] <= reserve["max_output_tokens"] == profile.max_output_tokens
        assert reserve["route_identity"] == contexts[kind]["route_identity"]
    assert observed_schemas == set(request_kinds)

    reducer_request = next(
        request for request in detail_requests
        if request["body"]["schema_version"] == DETAIL_REDUCE_REQUEST_SCHEMA
    )
    reducer_shape = merge_detail_reduction_response_shape(reducer_request)
    assert reducer_shape["detail_item_ids"] == reducer_request["body"]["detail_item_ids"]
    assert reducer_shape["child_node_ids"] == reducer_request["body"]["child_node_ids"]
    assert reducer_shape["child_hashes"] == reducer_request["body"]["child_hashes"]
    assert len(reducer_shape["summary"].split()) == reducer_request["body"]["max_summary_tokens"]

    slice_responses = _slice_responses_for(plan, proposal, detail_plan)
    reserve_tamper = copy.deepcopy(detail_plan["requests"])
    reserve_tamper[0]["measurement"]["output_reserve"]["requested_output_tokens"] += 1
    with pytest.raises(SelectedNavigationError, match="output reserve"):
        validate_merge_detail_responses(
            plan, proposal, detail_plan, slice_responses, detail_index=detail_index,
            output_estimator=output_estimator, context_reserve=contexts,
            actual_requests=reserve_tamper,
        )
    actual_request_tamper = copy.deepcopy(detail_plan["requests"])
    actual_request_tamper[0]["request"]["detail_item_ids"] = []
    with pytest.raises(SelectedNavigationError, match="actual request body differs"):
        validate_merge_detail_responses(
            plan, proposal, detail_plan, slice_responses, detail_index=detail_index,
            output_estimator=output_estimator, context_reserve=contexts,
            actual_requests=actual_request_tamper,
        )

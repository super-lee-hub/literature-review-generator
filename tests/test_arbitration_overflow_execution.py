"""Unit-level ordering tests for the injected overflow execution driver.

These fixtures exercise pure builders and callbacks only. They are not local
HTTP, Runner, scale, provider, or scientific acceptance evidence.
"""

from __future__ import annotations

import copy
import json
import math

import pytest

from outline.arbitration_overflow_execution import (
    execute_arbitration_overflow_pipeline,
)
from outline.candidate_arbitration_aggregation import (
    SELECTION_DETAIL_KIND,
    SELECTION_FINAL_KIND,
    SELECTION_RESPONSE_SCHEMA,
)
from outline.candidate_arbitration_navigation import (
    DETAIL_KIND,
    DETAIL_REDUCE_RESPONSE_SCHEMA,
    DETAIL_RESPONSE_SCHEMA,
    DETAIL_SLICE_RESPONSE_SCHEMA,
    EXTRACT_KIND,
    EXTRACT_RESPONSE_SCHEMA,
    NAV_KIND,
    NAV_RESPONSE_SCHEMA,
    REDUCE_KIND,
    REDUCE_RESPONSE_SCHEMA,
)
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import compute_v3_hash
from tests.test_run_first_arbitration_aggregation import (
    _candidate_contents,
    _caps,
    _input_estimator,
    _legacy_plan_and_ledger,
    _output_estimator,
    _prepared_legacy_source_request,
    _route_builder,
    _source_scope,
    _typed_leaf_response,
    _typed_reduction_response,
)


def _token_count(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return max(1, math.ceil(len(raw) / 3))


def _navigation_contexts(input_cap=24_000):
    return {
        kind: {
            "verified_context_limit": 80_000,
            "max_output_tokens": 6_000,
            "reasoning_reserve": 120,
            "safety_margin": 300,
            "effective_input_cap": input_cap,
            "route_identity": "unit-local-profile-v1",
        }
        for kind in (EXTRACT_KIND, REDUCE_KIND, NAV_KIND, DETAIL_KIND)
    }


def _navigation_callbacks():
    def authority(kind, node_id, logical_body):
        return {
            "prompt_id": f"unit.{kind}",
            "prompt_version": "unit-v1",
            "prompt_sha256": compute_v3_hash({"kind": kind, "schema": logical_body.get("schema_version")}),
            "policy_prompt_id": "unit.policy",
            "policy_prompt_version": "unit-v1",
            "policy_prompt_sha256": compute_v3_hash("unit-policy"),
            "node_id": node_id,
            "system_prompt": "Use only the exact source and scope supplied in the request.",
            "node_policies": {"unit-test": True},
        }

    def route(kind, request):
        return {"estimated_input_tokens": _token_count(request), "route_identity": "unit-local-profile-v1"}

    def output(kind, shape):
        return _token_count(shape)

    return authority, route, output


def _ready_inputs(tmp_path, *, slot_count=3, claim_length=0):
    candidate_ids = ["candidate_1", "candidate_2"]
    inventory, _paper, scope = _source_scope(tmp_path, slot_count=slot_count)
    contents = _candidate_contents(scope, candidate_ids, claim_length=claim_length)
    intent = {
        "review_question": "How do the source-backed assertions compare?",
        "scope": "Preserve the tested source boundaries.",
        "must_cover": [scope.task_ids[0]],
        "required_comparisons": [],
    }
    _hashes, disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
        candidate_ids, contents, scope, intent, [],
    )
    source_request = _prepared_legacy_source_request(
        candidate_ids, legacy_plan["candidate_hashes"], contents, disposition, intent,
    )
    return {
        "prepared_hierarchy": {
            "plan": legacy_plan,
            "source_request": source_request,
            "node_id": "arbitration",
        },
        "legacy_ledger": ledger,
        "candidate_scope": scope,
        "source_inventory": inventory,
        "review_intent": intent,
        "critique_disposition": disposition,
    }


def test_driver_runs_overflow_detail_and_selected_navigation_in_staged_order(tmp_path):
    ready = _ready_inputs(tmp_path, slot_count=2, claim_length=1_000)
    nav_authority, nav_route, nav_output = _navigation_callbacks()
    nav_contexts = _navigation_contexts()
    nav_contexts[DETAIL_KIND]["effective_input_cap"] = 6_000
    navigation_input_caps = {kind: 24_000 for kind in nav_contexts}
    navigation_input_caps[DETAIL_KIND] = 6_000
    caps = _caps(input_cap=14_500)
    root_dependencies = [
        {"artifact_id": "ready:candidates", "content_hash": "a" * 64, "status": "ready"},
        {"artifact_id": "ready:inventory", "content_hash": "b" * 64, "status": "ready"},
        {"artifact_id": "ready:scope", "content_hash": "c" * 64, "status": "ready"},
    ]
    events = []
    admissions = {}
    persisted = {}
    sent_records = []
    sent_response_shapes = []

    def admit_work(*, work_plan, kind, max_count, request_records):
        request_ids = [str(row.get("request_id") or row.get("node_id") or "") for row in request_records]
        assert max_count >= len(request_records)
        assert all(request_ids)
        for row in request_records:
            request_id = str(row.get("request_id") or row.get("node_id") or "")
            admissions[request_id] = {"kind": kind, "work_plan": work_plan, "request_record": copy.deepcopy(dict(row))}
        events.append(("admit", kind, tuple(request_ids)))
        return {
            "artifact_id": f"admission:{kind}:{len(events)}",
            "content_hash": compute_v3_hash({"kind": kind, "request_ids": request_ids, "max_count": max_count}),
            "status": "ready",
            "kind": kind,
            "request_ids": request_ids,
            "count": len(request_ids),
            "max_count": max_count,
        }

    def persist(*, kind, artifact_id, payload, dependencies):
        content_hash = compute_v3_hash(payload)
        prior = persisted.get(artifact_id)
        if prior is not None and prior["content_hash"] != content_hash:
            raise AssertionError(f"non-idempotent persist for {artifact_id}")
        persisted[artifact_id] = {
            "kind": kind,
            "payload": copy.deepcopy(dict(payload)),
            "content_hash": content_hash,
            "dependencies": copy.deepcopy(list(dependencies)),
        }
        events.append(("persist", kind, artifact_id))
        return {"artifact_id": artifact_id, "content_hash": content_hash, "status": "ready"}

    def response_for(kind, request_record, context):
        work_plan = context["work_plan"]
        if kind == "arbitration_leaf_assessment_v2":
            leaf = next(row for row in work_plan["assessment_leaves"] if row["node_id"] == request_record["request_id"])
            return _typed_leaf_response(work_plan, leaf)
        if kind == "arbitration_reduce_new":
            reducer = next(
                row for row in work_plan["reducer_tree"]["nodes"]
                if row["tree_node_id"] == request_record["tree_node_id"]
            )
            return _typed_reduction_response(work_plan, reducer, request_record)
        if kind == "arbitration_decision":
            return {
                "schema_version": SELECTION_RESPONSE_SCHEMA,
                "node_id": request_record["node_id"],
                "candidate_ids": list(request_record["candidate_ids"]),
                "candidate_hashes": dict(request_record["candidate_hashes"]),
                "outcome": "detail_required",
                "selected_candidate_id": None,
                "detail_required_view_ids": [request_record["noncritical_fact_ids"][0]],
                "rationale": "More.",
            }
        if kind == SELECTION_DETAIL_KIND:
            body = request_record["body"]
            return {
                "schema_version": "arbitration_selection_detail_response/v1",
                "node_id": request_record["request_id"],
                "prior_selector_node_id": body["prior_selector_node_id"],
                "prior_selector_request_hash": body["prior_selector_request_hash"],
                "prior_selection_result_hash": body["prior_selection_result_hash"],
                "candidate_ids": list(body["candidate_ids"]),
                "candidate_hashes": dict(body["candidate_hashes"]),
                "detail_view_id": body["detail_view"]["decision_view_id"],
                "detail_view_hash": body["detail_view"]["view_hash"],
                "source_fact_hash": body["detail_view"]["source_fact_hash"],
                "assessment_status": "resolved",
                "assessed_direction": "mixed",
                "assessed_disagreement": "uncertain",
                "cited_detail_view_ids": [body["detail_view"]["decision_view_id"]],
                "reason": "OK.",
            }
        if kind == "arbitration_decision_final":
            return {
                "schema_version": SELECTION_RESPONSE_SCHEMA,
                "node_id": request_record["node_id"],
                "candidate_ids": list(request_record["candidate_ids"]),
                "candidate_hashes": dict(request_record["candidate_hashes"]),
                "outcome": "selected",
                "selected_candidate_id": request_record["candidate_ids"][-1],
                "detail_required_view_ids": [],
                "rationale": "Done.",
            }
        if kind == EXTRACT_KIND:
            span = request_record["body"]["span"]
            return {
                "schema_version": EXTRACT_RESPONSE_SCHEMA,
                "candidate_id": span["candidate_id"] if "candidate_id" in span else work_plan["candidate_id"],
                "candidate_hash": work_plan["candidate_hash"],
                "span_id": span["span_id"],
                "source_hash": span["source_hash"],
                "utf8_start": span["utf8_start"],
                "utf8_end": span["utf8_end"],
                "text_hash": span["text_hash"],
                "expression": f"layout {span['source_field']} {span['span_id'][-5:]}",
            }
        if kind == REDUCE_KIND:
            body = request_record["body"]
            sections_by_id = {}
            intent_by_field = {}
            for child in body["child_nodes"]:
                for row in child["layout_by_section"]:
                    sections_by_id.setdefault(row["section_id"], []).append(row)
                for row in child["intent_layout"]:
                    intent_by_field.setdefault(row["source_field"], []).append(row)
            layout_rows = []
            for scope_row in work_plan["section_scope"]:
                rows = sections_by_id.get(scope_row["section_id"], [])
                if rows:
                    layout_rows.append({
                        "section_id": scope_row["section_id"],
                        "section_hash": scope_row["section_hash"],
                        "position": scope_row["position"],
                        "task_ids": list(scope_row["task_ids"]),
                        "hash_aliases": list(scope_row["hash_aliases"]),
                        "span_ids": [span_id for row in rows for span_id in row["span_ids"]],
                        "source_fields": [field for row in rows for field in row["source_fields"]],
                        "expression": " ".join(row["expression"] for row in rows),
                    })
            intent_rows = [
                {
                    "source_field": key,
                    "source_hash": rows[0]["source_hash"],
                    "span_ids": [span_id for row in rows for span_id in row["span_ids"]],
                    "expression": " ".join(row["expression"] for row in rows),
                }
                for key, rows in sorted(intent_by_field.items())
            ]
            return {
                "schema_version": REDUCE_RESPONSE_SCHEMA,
                "candidate_id": work_plan["candidate_id"],
                "candidate_hash": work_plan["candidate_hash"],
                "node_id": request_record["request_id"],
                "child_node_ids": list(body["child_node_ids"]),
                "child_hashes": list(body["child_hashes"]),
                "span_ids": list(body["expected_span_ids"]),
                "layout_by_section": layout_rows,
                "intent_layout": intent_rows,
            }
        if kind == NAV_KIND:
            body = request_record["body"]
            scopes = body["section_scope"]
            source_ids = [row["section_id"] for row in scopes]
            merge_ids = source_ids[:2]
            merge_hashes = {row["section_id"]: row["section_hash"] for row in scopes[:2]}
            return {
                "schema_version": NAV_RESPONSE_SCHEMA,
                "candidate_id": body["candidate_id"],
                "candidate_hash": body["candidate_hash"],
                "root_node_id": body["root_node_id"],
                "root_hash": body["root_hash"],
                "plan_hash": body["plan_hash"],
                "span_manifest_hash": body["span_manifest_hash"],
                "section_order": [row["section_id"] for row in reversed(body["section_scope"])],
                "merge_groups": [{
                    "source_section_ids": merge_ids,
                    "source_section_hashes": merge_hashes,
                    "title": "Merged title",
                    "goal": "Merged goal",
                    "integration_reason": "Shared source structure.",
                }],
            }
        if kind in {DETAIL_KIND, "arbitration_detail_finalize/v2"}:
            body = request_record["body"]
            response_schema = body["response_schema"]
            if response_schema == DETAIL_SLICE_RESPONSE_SCHEMA:
                item_ids = list(body["detail_item_ids"])
                item_hashes = dict(body["detail_item_hashes"])
                return {
                    "schema_version": DETAIL_SLICE_RESPONSE_SCHEMA,
                    "candidate_id": body["candidate_id"],
                    "candidate_hash": body["candidate_hash"],
                    "proposal_hash": body["proposal_hash"],
                    "group_id": body["group_id"],
                    "slice_id": body["slice_id"],
                    "detail_item_ids": item_ids,
                    "detail_item_hashes": item_hashes,
                    "critical_literal_bindings_hash": body["source_critical_literal_binding_refs_hash"],
                    "intent_context_hash": compute_v3_hash({
                        key: body[key] for key in ("intent_detail_item_ids", "intent_detail_item_hashes")
                    }),
                    "assessments": [
                        {"detail_id": item_id, "value_hash": item_hashes[item_id], "disposition": "compatible"}
                        for item_id in item_ids
                    ],
                    "summary": "x",
                }
            if response_schema == DETAIL_REDUCE_RESPONSE_SCHEMA:
                return {
                    "schema_version": DETAIL_REDUCE_RESPONSE_SCHEMA,
                    "candidate_id": body["candidate_id"],
                    "candidate_hash": body["candidate_hash"],
                    "proposal_hash": body["proposal_hash"],
                    "group_id": body["group_id"],
                    "node_id": body["node_id"],
                    "level": request_record["level"],
                    "child_node_ids": list(body["child_node_ids"]),
                    "child_hashes": list(body["child_hashes"]),
                    "detail_item_ids": list(body["detail_item_ids"]),
                    "detail_item_hashes": dict(body["detail_item_hashes"]),
                    "critical_literal_bindings_hash": body["critical_literal_bindings_hash"],
                    "disposition": "compatible",
                    "summary": "x",
                }
            if response_schema == DETAIL_RESPONSE_SCHEMA:
                detail_root = body["detail_root"]
                return {
                    "schema_version": DETAIL_RESPONSE_SCHEMA,
                    "candidate_id": body["candidate_id"],
                    "candidate_hash": body["candidate_hash"],
                    "proposal_hash": body["proposal_hash"],
                    "group_id": body["group_id"],
                    "detail_root_hash": detail_root["node_hash"],
                    "detail_item_ids": list(detail_root["detail_item_ids"]),
                    "detail_item_hashes": dict(detail_root["detail_item_hashes"]),
                    "critical_literal_bindings_hash": detail_root["critical_literal_bindings_hash"],
                    "decision": "accept",
                    "source_contributions": {section_id: "OK." for section_id in body["source_section_ids"]},
                }
            raise AssertionError(f"unexpected merge detail response schema {response_schema}")
        raise AssertionError(f"unexpected unit test request kind {kind}")

    def send(*, request_record, root_dependencies, admission):
        request_id = request_record["request_id"]
        context = admissions[request_id]
        assert context["kind"] == request_record["kind"]
        assert request_record["transport_node_id"] == "arbitration"
        requested_output_tokens = request_record["requested_output_tokens"]
        assert context["request_record"]["requested_output_tokens"] == requested_output_tokens
        assert context["request_record"]["response_shape_hash"] == request_record["response_shape_hash"]
        assert len(request_record["response_shape_hash"]) == 64
        if request_record["kind"] in {
            "arbitration_leaf_assessment_v2",
            "arbitration_reduce_new",
            "arbitration_decision",
            SELECTION_DETAIL_KIND,
            SELECTION_FINAL_KIND,
        }:
            assert 1 <= requested_output_tokens <= caps.route_max_output
        else:
            route_kind = DETAIL_KIND if request_record["kind"] == "arbitration_detail_finalize/v2" else request_record["kind"]
            assert 1 <= requested_output_tokens <= nav_contexts[route_kind]["max_output_tokens"]
        sent_records.append(copy.deepcopy(dict(request_record)))
        events.append(("send", request_record["kind"], request_id))
        raw = response_for(request_record["kind"], request_record, context)
        sent_response_shapes.append((request_record["kind"], copy.deepcopy(dict(raw)), requested_output_tokens))
        return {
            "request_id": request_id,
            "transport_node_id": "arbitration",
            "actual_request": request_record["request"],
            "request_hash": request_record["request_hash"],
            "raw_response": raw,
            "receipt_id": f"unit-original-receipt:{request_id}",
            "response_hash": compute_v3_hash(raw),
            "replay_status": "unit-test-exact-replay",
        }

    def apply(candidate_id, sections, coordination, *, parent_hash):
        return OutlineV3Executor._apply_section_coordination(
            candidate_id, sections, coordination, parent_hash=parent_hash,
        )

    nav_contexts = _navigation_contexts()
    nav_authority, nav_route, nav_output = _navigation_callbacks()
    result = execute_arbitration_overflow_pipeline(
        **ready,
        arbitration_caps=caps,
        navigation_context_reserve=nav_contexts,
        navigation_input_caps=navigation_input_caps,
        fnav_max=100,
        emax=200,
        root_dependencies=root_dependencies,
        arbitration_request_builder=_route_builder,
        arbitration_input_estimator=_input_estimator,
        arbitration_output_estimator=_output_estimator,
        navigation_prompt_authority=nav_authority,
        navigation_route_estimator=nav_route,
        navigation_output_estimator=nav_output,
        admit_work=admit_work,
        send=send,
        persist=persist,
        apply_section_coordination=apply,
    )

    assert result["selected_candidate_id"] == max(["candidate_1", "candidate_2"])
    assert result["selection_result"]["outcome"] == "selected"
    assert len(result["navigation_proposal"]["merge_groups"]) == 1
    assert len(result["coordination"]["merge_groups"]) == 1
    assert [section["section_id"] for section in result["coordinated_sections"]] == result["coordination"]["section_order"]
    assert result["counts"]["Qselector_detail"] == 1
    assert result["counts"]["Qfinal_selector"] == 1
    assert result["detail_work_plan"]["counts"]["Qdetail_slice"] > 0
    assert result["detail_work_plan"]["counts"]["Qdetail_reduce"] > 0
    assert result["detail_work_plan"]["counts"]["Qdetail_final"] == 1
    raw_snapshots = {
        row["payload"]["request_id"]: row
        for row in persisted.values()
        if row["kind"] == "provider_raw_response"
    }
    validated_results = [
        row for row in persisted.values() if row["kind"] == "provider_validated_result"
    ]
    assert set(raw_snapshots) == {row["request_id"] for row in sent_records}
    wrapped_kinds = {
        "arbitration_leaf_assessment_v2",
        "arbitration_reduce_new",
        "arbitration_decision",
        EXTRACT_KIND,
        REDUCE_KIND,
        NAV_KIND,
        SELECTION_FINAL_KIND,
    }
    assert len(validated_results) == sum(record["kind"] in wrapped_kinds for record in sent_records)
    persisted_kinds = {row["kind"] for row in persisted.values()}
    assert {
        "selector_detail_results",
        "selected_candidate_merge_detail_slice_results",
        "selected_candidate_merge_detail_reduction_node",
        "selected_candidate_merge_group_adjudications",
    }.issubset(persisted_kinds)
    assert all(
        snapshot["payload"]["raw_response"]
        and snapshot["payload"]["receipt_id"].startswith("unit-original-receipt:")
        and snapshot["payload"]["requested_output_tokens"] > 0
        and len(snapshot["payload"]["response_shape_hash"]) == 64
        and any(ref["artifact_id"].startswith("admission:") for ref in snapshot["dependencies"])
        for snapshot in raw_snapshots.values()
    )
    sent_ids = {request_id for event, _kind, request_id in events if event == "send"}
    admitted_ids = {request_id for _kind, request_ids in [event[1:] for event in events if event[0] == "admit"] for request_id in request_ids}
    assert sent_ids.issubset(admitted_ids)
    assert all("requested_output_tokens" in record for record in sent_records)
    nav_sent = [record for record in sent_records if record["kind"] in {EXTRACT_KIND, REDUCE_KIND, NAV_KIND}]
    assert nav_sent and all(record["body"]["candidate_id"] == result["selected_candidate_id"] for record in nav_sent)
    selected_detail_sent = [
        record for record in sent_records
        if record["kind"] in {DETAIL_KIND, "arbitration_detail_finalize/v2"}
    ]
    assert selected_detail_sent and all(
        record["body"]["candidate_id"] == result["selected_candidate_id"]
        for record in selected_detail_sent
    )
    assert all(
        (_output_estimator(response) if kind in {
            "arbitration_leaf_assessment_v2", "arbitration_reduce_new", "arbitration_decision",
            SELECTION_DETAIL_KIND, SELECTION_FINAL_KIND,
        } else nav_output(kind if kind != "arbitration_detail_finalize/v2" else DETAIL_KIND, response))
        <= reserve
        for kind, response, reserve in sent_response_shapes
    )


def test_driver_requires_all_production_boundary_callbacks(tmp_path):
    ready = _ready_inputs(tmp_path)
    nav_authority, nav_route, nav_output = _navigation_callbacks()
    with pytest.raises(TypeError):
        execute_arbitration_overflow_pipeline(
            **ready,
            arbitration_caps=_caps(input_cap=8_000),
            navigation_context_reserve=_navigation_contexts(),
            navigation_input_caps={kind: 24_000 for kind in _navigation_contexts()},
            fnav_max=100,
            emax=200,
            root_dependencies=[],
            arbitration_request_builder=_route_builder,
            arbitration_input_estimator=_input_estimator,
            arbitration_output_estimator=_output_estimator,
            navigation_prompt_authority=nav_authority,
            navigation_route_estimator=nav_route,
            navigation_output_estimator=nav_output,
            persist=lambda **kwargs: {},
            apply_section_coordination=lambda *_args, **_kwargs: ([], {}),
        )

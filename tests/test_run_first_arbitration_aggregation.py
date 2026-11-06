"""Focused source-bound overflow arbitration compiler regressions."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from outline.candidate_arbitration_aggregation import (
    ASSESSMENT_RESPONSE_SCHEMA,
    REDUCTION_INPUT_SCHEMA,
    REDUCTION_RESPONSE_SCHEMA,
    SELECTION_DETAIL_RESPONSE_SCHEMA,
    SELECTION_RESPONSE_SCHEMA,
    ArbitrationAggregationError,
    ArbitrationSerializationCaps,
    ArbitrationSerializationError,
    _build_reducer_tree,
    build_arbitration_reducer_request,
    build_global_selection_detail_requests,
    build_global_selection_followup_request,
    build_global_selection_request,
    build_overflow_assessment_plan,
    validate_arbitration_assessment_response,
    validate_arbitration_reduction_response,
    validate_global_selection_detail_responses,
    validate_global_selection_response,
)
from outline.candidate_arbitration_hierarchy import (
    build_arbitration_final_ledger,
    plan_arbitration_hierarchy,
    validate_arbitration_leaf,
)
from outline.candidate_arbitration_packing import (
    expand_arbitration_candidate_support,
    pack_arbitration_candidate_support,
)
from outline.candidate_arbitration_views import (
    ArbitrationViewError,
    build_arbitration_review_group_map,
    compile_arbitration_critical_views,
    expand_arbitration_critical_view_wire,
    validate_arbitration_view_set,
)
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import compute_v3_hash
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json
from services.prompt_registry import PromptRegistry
from tests.test_candidate_output_scope import source_scope as _source_scope_fixture


def _route_builder(kind, payload):
    return {
        "request_kind": kind,
        **copy.deepcopy(dict(payload)),
        "_prompt_authority": {
            "prompt_id": "unit.outline.v3",
            "prompt_version": "test-v2",
            "prompt_sha256": "a" * 64,
            "policy_prompt_id": "unit.policy.v3",
            "policy_prompt_version": "test-v2",
            "policy_prompt_sha256": "b" * 64,
            "node_id": str(payload.get("node_id") or kind),
            "system_prompt": "Assess only the supplied source-bound scope.",
            "node_policies": {"mode": "all-candidates"},
        },
    }


def _tokens(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return max(1, math.ceil(len(raw) / 3))


def _input_estimator(value):
    return {"estimated_input_tokens": _tokens(value)}


def _output_estimator(value):
    return _tokens(value)


def _caps(*, input_cap=32_000, output_cap=20_000, detail_ids=4):
    return ArbitrationSerializationCaps(
        effective_input_cap=input_cap,
        verified_context_limit=100_000,
        route_max_output=output_cap,
        reasoning_reserve=0,
        safety_margin=0,
        route_identity={
            "provider": "loopback",
            "model": "serializer-fixture",
            "endpoint_type": "responses",
            "prompt_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
        },
        assessment_text_tokens=4,
        group_summary_text_tokens=5,
        selection_text_tokens=6,
        max_detail_required_ids=detail_ids,
    )


def _source_scope(tmp_path, slot_count=5):
    inventory, paper, _primary, route, _original_scope = _source_scope_fixture.__wrapped__(tmp_path)
    route = copy.deepcopy(route)
    fragment = route["fragments"][0]
    original = fragment["provider_results"][0]
    base_claim = original["provider_output"]["claims"][0]
    fragment["provider_results"] = []
    for index in range(slot_count):
        claim = copy.deepcopy(base_claim)
        claim["claim_id"] = f"synthesis:topic_synthesis:scope-claim-{index + 1}"
        claim["text"] = f"The source bounded claim {index + 1} keeps its tested context."
        fragment["provider_results"].append({
            "result_id": f"topic-result:scope:{index + 1}",
            "provider_output": {"claims": [claim]},
        })
    from outline.candidate_output_scope import build_candidate_output_scope_v1

    scope = build_candidate_output_scope_v1(
        inventory,
        [route],
        selected_relation_ids=["relation:confirmed:1", "relation:confirmed:unused"],
    )
    assert len(scope.claim_slots) == slot_count
    return inventory, paper, scope


def _candidate_contents(scope, candidate_ids, *, claim_length=0):
    contents = {}
    for candidate_id in candidate_ids:
        sections = []
        for index, slot in enumerate(scope.claim_slots):
            claim = f"{candidate_id} preserves source-bounded assertion {index + 1}."
            if claim_length:
                claim = claim + (" x" * claim_length)
            support = {
                "claim_id": f"candidate-claim:{candidate_id}:{index + 1}",
                "synthesis_claim_id": slot.synthesis_claim_id,
                "claim_slot_id": slot.claim_slot_id,
                "task_id": slot.task_id,
                "claim": claim,
                "paper_key": slot.paper_key,
                "primary_claim_id": slot.primary_claim_id,
                "source_claim_ids": list(slot.source_claim_ids),
                "evidence_ids": list(slot.evidence_ids),
                "source_field_ids": list(slot.source_field_ids),
                "relation_ids": list(slot.relation_ids),
            }
            if slot.study_id:
                support["study_id"] = slot.study_id
            sections.append({
                "section_id": f"{candidate_id}:section:{index + 1}",
                "title": f"{candidate_id} source result {index + 1}",
                "goal": f"Preserve the complete source limit for result {index + 1}.",
                "task_ids": [slot.task_id],
                "paper_keys": [slot.paper_key],
                "relation_ids": list(slot.relation_ids),
                "claims": [claim],
                "claim_support": [support],
            })
        contents[candidate_id] = {
            "candidate_id": candidate_id,
            "organizing_logic": f"Organize source-bound assertions for {candidate_id}.",
            "sections": sections,
            "planned_claims": [claim for section in sections for claim in section["claims"]],
        }
    return contents


def _critical_issues(candidate_ids, candidate_hashes):
    return [
        {
            "issue_id": f"blocking-candidate:{candidate_id}",
            "scope": "candidate",
            "target_ids": [candidate_id],
            "severity": "blocking",
            "evidence_refs": [],
            "resolution_status": "resolved",
            "parent_candidate_hash": candidate_hashes[candidate_id],
            "candidate_id": candidate_id,
            "source_critic": "structure_critic",
            "source": "typed_fixture",
            "message": "Preserve this candidate's complete source boundary.",
        }
        for candidate_id in candidate_ids
    ]


def _prepared_legacy_source_request(candidate_ids, candidate_hashes, contents, disposition, intent):
    """Mirror the source mapping returned by the production support expander."""

    critic_payload = {"passed": True, "issues": copy.deepcopy(disposition.get("issues") or [])}
    request = {
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": dict(candidate_hashes),
        "candidate_contents": copy.deepcopy(dict(contents)),
        "critique_disposition": copy.deepcopy(dict(disposition)),
        "critiques": {"structure_critic": critic_payload},
        "coverage_metrics": {"structure_critic": {}},
        "evidence_metrics": {"structure_critic": {}},
        "structure_metrics": {"structure_critic": {}},
        "blocking_diagnostics": [],
        "review_intent": copy.deepcopy(dict(intent)),
        "selection_rule": "coverage_then_evidence_then_structure",
    }
    return expand_arbitration_candidate_support(pack_arbitration_candidate_support(request))


def _legacy_plan_and_ledger(
    candidate_ids,
    candidate_contents,
    candidate_scope,
    intent,
    issues,
    *,
    legacy_input_cap=250_000,
    provenance_hashes=None,
    legacy_source_request=None,
):
    candidate_hashes = {candidate_id: compute_v3_hash(candidate_contents[candidate_id]) for candidate_id in candidate_ids}
    disposition = {
        "schema_version": "outline_critique_disposition/v1",
        "global_blocker": False,
        "blocked_candidate_ids": [],
        "eligible_candidate_ids": list(candidate_ids),
        "issues": copy.deepcopy(issues),
    }
    prepared_source = legacy_source_request or _prepared_legacy_source_request(
        candidate_ids, candidate_hashes, candidate_contents, disposition, intent,
    )
    prepared_provenance = {
        "request": hash_json(prepared_source),
        "intent": hash_json(prepared_source.get("review_intent") or {}),
    }
    legacy_plan = plan_arbitration_hierarchy(
        candidate_ids,
        candidate_hashes,
        candidate_contents,
        disposition,
        critique_hashes={key: hash_json(value) for key, value in prepared_source["critiques"].items()},
        provenance_hashes=provenance_hashes or prepared_provenance,
        request_builder=lambda body: {"provider_request": copy.deepcopy(dict(body))},
        estimate_input_tokens=lambda request: {
            "estimated_input_tokens": _tokens(request),
            "requested_output_tokens": 512,
            "within_budget": True,
        },
        input_cap=legacy_input_cap,
        max_calls=512,
        output_cap=50_000,
    )
    leaf_results = {}
    for leaf in legacy_plan["leaves"]:
        assessments = []
        for candidate_id in candidate_ids:
            expected = leaf["expected_candidate_rows"][candidate_id]
            present = expected["presence"] == "present"
            assessments.append({
                "candidate_id": candidate_id,
                "candidate_hash": expected["candidate_hash"],
                "presence": expected["presence"],
                "reviewed_section_ids": list(expected["reviewed_section_ids"]),
                "section_hashes": dict(expected["section_hashes"]),
                "coverage_assessment": "Legacy v1 prose retained only as immutable history." if present else "No section assigned.",
                "evidence_assessment": "Legacy v1 prose is not a typed overflow judgment." if present else "No section assigned.",
                "structure_assessment": "Legacy v1 structure assessment remains unconverted." if present else "No section assigned.",
                "advantages": ["Historical v1 only."] if present else [],
                "limitations": ["Historical v1 only."] if present else [],
                "evidence_refs": list(expected["allowed_evidence_refs"][:1]) if present else [],
                "critic_issue_ids": list(expected["critic_issue_ids"]) if present else [],
            })
        response = {"leaf_id": leaf["leaf_id"], "unit_ids": list(leaf["unit_ids"]), "candidate_assessments": assessments}
        leaf_results[leaf["leaf_id"]] = validate_arbitration_leaf(legacy_plan, leaf["leaf_id"], response)
    ledger = build_arbitration_final_ledger(legacy_plan, leaf_results)
    return candidate_hashes, disposition, legacy_plan, ledger


def _legacy_ledger_from_plan(legacy_plan):
    candidate_ids = list(legacy_plan["candidate_ids"])
    leaf_results = {}
    for leaf in legacy_plan["leaves"]:
        assessments = []
        for candidate_id in candidate_ids:
            expected = leaf["expected_candidate_rows"][candidate_id]
            present = expected["presence"] == "present"
            assessments.append({
                "candidate_id": candidate_id,
                "candidate_hash": expected["candidate_hash"],
                "presence": expected["presence"],
                "reviewed_section_ids": list(expected["reviewed_section_ids"]),
                "section_hashes": dict(expected["section_hashes"]),
                "coverage_assessment": "Legacy v1 prose retained only as immutable history." if present else "No section was assigned.",
                "evidence_assessment": "Legacy v1 prose is not a typed overflow judgment." if present else "No section was assigned.",
                "structure_assessment": "Legacy v1 structure assessment remains unconverted." if present else "No section was assigned.",
                "advantages": ["Historical v1 only."] if present else [],
                "limitations": ["Historical v1 only."] if present else [],
                "evidence_refs": list(expected["allowed_evidence_refs"][:1]) if present else [],
                "critic_issue_ids": list(expected["critic_issue_ids"]) if present else [],
            })
        response = {"leaf_id": leaf["leaf_id"], "unit_ids": list(leaf["unit_ids"]), "candidate_assessments": assessments}
        leaf_results[leaf["leaf_id"]] = validate_arbitration_leaf(legacy_plan, leaf["leaf_id"], response)
    return build_arbitration_final_ledger(legacy_plan, leaf_results)


def _production_hierarchy(source_request, *, input_cap=None):
    """Exercise the same hierarchy preparation and request provenance as v3_executor."""

    profile = ProviderContextProfile.conservative(
        provider="local",
        model="local",
        endpoint_type="chat_completions",
        model_context_limit=200_000,
        max_output_tokens=20_000,
        reasoning_reserve=0,
        safety_margin=0,
    )
    executor = object.__new__(OutlineV3Executor)
    executor.max_source_prompt_tokens = 32_000
    executor.max_provider_calls = 512
    executor._provider_call_count = 0
    executor.prompt_registry = PromptRegistry()
    executor._outline_prompt_identity = executor.prompt_registry.identity("outline.node.system.v3")
    executor._outline_policy_identity = executor.prompt_registry.identity("outline.node.policies.v3")
    executor._node_route = lambda *_args, **_kwargs: SimpleNamespace(profile=profile)
    packed_request = pack_arbitration_candidate_support(source_request)
    full_request = executor._attach_arbitration_output_contract(
        executor._attach_prompt_authority("arbitration", packed_request),
    )
    if input_cap is None:
        full_tokens = int(profile.estimate_request(full_request)["estimated_input_tokens"])
        input_cap = min(32_000, max(1, full_tokens - max(1, full_tokens // 10)))
    executor.max_source_prompt_tokens = input_cap
    return executor._prepare_arbitration_hierarchy(packed_request)


def _build_ready(tmp_path, candidate_count=2, *, slot_count=5, caps=None, claim_length=0):
    candidate_ids = sorted(f"candidate_{index + 1}" for index in range(candidate_count))
    inventory, _paper, scope = _source_scope(tmp_path, slot_count)
    contents = _candidate_contents(scope, candidate_ids, claim_length=claim_length)
    intent = {
        "review_question": "How do the source-backed assertions compare?",
        "scope": "Preserve the tested source boundaries.",
        "must_cover": [scope.task_ids[0]],
        "required_comparisons": [],
    }
    issues = []
    _hashes, disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
        candidate_ids, contents, scope, intent, issues,
    )
    legacy_source_request = _prepared_legacy_source_request(
        candidate_ids, legacy_plan["candidate_hashes"], contents, disposition, intent,
    )
    canonical_contents = copy.deepcopy(legacy_source_request["candidate_contents"])
    original_hashes = {
        "legacy_plan": compute_v3_hash(legacy_plan),
        "legacy_ledger": compute_v3_hash(ledger),
        "legacy_ledger_bytes": json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        "candidate_contents": compute_v3_hash(contents),
        "scope": scope.content_hash,
        "source_inventory": inventory.content_hash,
    }
    plan = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=ledger,
        legacy_source_request=legacy_source_request,
        candidate_contents=canonical_contents,
        candidate_scope=scope,
        source_inventory=inventory,
        review_intent=intent,
        critique_disposition=disposition,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps or _caps(),
    )
    assert compute_v3_hash(legacy_plan) == original_hashes["legacy_plan"]
    assert compute_v3_hash(ledger) == original_hashes["legacy_ledger"]
    assert json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") == original_hashes["legacy_ledger_bytes"]
    assert compute_v3_hash(contents) == original_hashes["candidate_contents"]
    assert scope.content_hash == original_hashes["scope"]
    assert inventory.content_hash == original_hashes["source_inventory"]
    return inventory, scope, canonical_contents, intent, disposition, legacy_plan, ledger, plan


def _typed_leaf_response(plan, node):
    assessments = []
    expected_rows = {
        (row["candidate_id"], row["unit_id"]): {
            key: value for key, value in row.items() if key not in {"candidate_id", "unit_id"}
        }
        for row in node["expected_assessments"]
    }
    response = {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": node["node_id"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": list(node["unit_ids"]),
        "assessments": assessments,
    }
    for candidate_id in plan["candidate_ids"]:
        for unit_id in node["unit_ids"]:
            expected = expected_rows[(candidate_id, unit_id)]
            view_rows = []
            for view in expected["decision_views"]:
                refs = view["literal_view_ref_ids"]
                view_rows.append({
                    "decision_view_id": view["decision_view_id"],
                    "decision_view_hash": view["decision_view_hash"],
                    "assessed_direction": "supports",
                    "assessed_disagreement": "consistent",
                    "reason": "Bounded.",
                    "evidence_refs": list(refs[:1]),
                    "counterevidence_refs": [],
                })
            present = expected["presence"] == "present"
            assessments.append({
                "candidate_id": candidate_id,
                "candidate_hash": plan["candidate_hashes"][candidate_id],
                "unit_id": unit_id,
                "unit_hash": expected["unit_hash"],
                "presence": expected["presence"],
                "section_bindings": copy.deepcopy(expected["section_bindings"]),
                "assessed_direction": "supports" if present else "not_comparable",
                "assessed_disagreement": "consistent" if present else "not_applicable",
                "reason": "Bounded." if present else "Absent.",
                "evidence_refs": list(expected["evidence_ref_ids"][:1]) if present else [],
                "counterevidence_refs": [],
                "view_assessments": view_rows,
            })
    response["assessments"] = assessments
    response["group_contributions"] = _refresh_group_contributions(plan, node, response)
    return response


def _refresh_group_contributions(plan, node, response):
    assessment_map = {
        (row["candidate_id"], row["unit_id"]): row
        for row in response["assessments"]
    }
    units_by_id = {row["unit_id"]: row for row in plan["assessment_units"]}
    contributions = []
    for expected in node["expected_group_contributions"]:
        candidate_id = expected["candidate_id"]
        group_id = expected["group_id"]
        directions = set()
        disagreements = set()
        evidence_refs = set()
        counterevidence_refs = set()
        for unit_id in expected["unit_ids"]:
            assessment = assessment_map[(candidate_id, unit_id)]
            unit = units_by_id[unit_id]
            group_view_ids = set(unit["group_views_by_candidate"][candidate_id].get(group_id, []))
            directions.add(assessment["assessed_direction"])
            disagreements.add(assessment["assessed_disagreement"])
            evidence_refs.update(assessment["evidence_refs"])
            counterevidence_refs.update(assessment["counterevidence_refs"])
            for view in assessment["view_assessments"]:
                if view["decision_view_id"] in group_view_ids:
                    directions.add(view["assessed_direction"])
                    disagreements.add(view["assessed_disagreement"])
                    evidence_refs.update(view["evidence_refs"])
                    counterevidence_refs.update(view["counterevidence_refs"])
        direction = next(iter(directions)) if len(directions) == 1 else "mixed"
        disagreement = next(iter(disagreements)) if len(disagreements) == 1 else "uncertain"
        contributions.append({
            "candidate_id": candidate_id,
            "candidate_hash": plan["candidate_hashes"][candidate_id],
            "group_id": group_id,
            "group_hash": expected["group_hash"],
            "unit_ids": list(expected["unit_ids"]),
            "decision_view_ids": list(expected["decision_view_ids"]),
            "assessed_direction": direction,
            "assessed_disagreement": disagreement,
            "summary": "Group.",
            "evidence_refs": sorted(evidence_refs),
            "counterevidence_refs": sorted(counterevidence_refs),
        })
    return contributions


def _ready_leaf_results(plan):
    return {
        node["node_id"]: validate_arbitration_assessment_response(
            plan,
            node["node_id"],
            _typed_leaf_response(plan, node),
            estimate_output_tokens=_output_estimator,
        )
        for node in plan["assessment_leaves"]
    }


def _typed_reduction_response(plan, reducer, request):
    rows = []
    for expected in request["expected_group_summaries"]:
        present = bool(expected["unit_ids"])
        rows.append({
            "candidate_id": expected["candidate_id"],
            "candidate_hash": plan["candidate_hashes"][expected["candidate_id"]],
            "group_id": expected["group_id"],
            "group_hash": expected["group_hash"],
            "presence": "present" if present else "not_applicable",
            "unit_ids": list(expected["unit_ids"]),
            "decision_view_ids": list(expected["decision_view_ids"]),
            "assessed_direction": expected["assessed_direction"],
            "assessed_disagreement": expected["assessed_disagreement"],
            "assessed_direction_values": list(expected["assessed_direction_values"]),
            "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
            "child_lineage": copy.deepcopy(expected["child_lineage"]),
            "summary": "Group." if present else "",
            "evidence_refs": list(expected["evidence_refs"]),
            "counterevidence_refs": list(expected["counterevidence_refs"]),
        })
    return {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "node_id": request["node_id"],
        "tree_node_id": reducer["tree_node_id"],
        "child_node_ids": list(request["child_node_ids"]),
        "child_result_hashes": list(request["child_result_hashes"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": list(reducer["unit_ids"]),
        "group_summaries": rows,
    }


def _ready_reduction_root(plan, leaf_results):
    result_by_tree_id = dict(leaf_results)
    if not plan["reducer_tree"]["nodes"]:
        return result_by_tree_id[plan["reducer_root_node_id"]]
    for node in plan["reducer_tree"]["nodes"]:
        children = {child_id: result_by_tree_id[child_id] for child_id in node["child_node_ids"]}
        request = build_arbitration_reducer_request(
            plan,
            node["tree_node_id"],
            children,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(),
        )
        rows = []
        for expected in request["expected_group_summaries"]:
            present = bool(expected["unit_ids"])
            rows.append({
                "candidate_id": expected["candidate_id"],
                "candidate_hash": plan["candidate_hashes"][expected["candidate_id"]],
                "group_id": expected["group_id"],
                "group_hash": expected["group_hash"],
                "presence": "present" if present else "not_applicable",
                "unit_ids": list(expected["unit_ids"]),
                "decision_view_ids": list(expected["decision_view_ids"]),
                "assessed_direction": expected["assessed_direction"],
                "assessed_disagreement": expected["assessed_disagreement"],
                "assessed_direction_values": list(expected["assessed_direction_values"]),
                "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
                "child_lineage": copy.deepcopy(expected["child_lineage"]),
                "summary": "Group." if present else "",
                "evidence_refs": list(expected["evidence_refs"]),
                "counterevidence_refs": list(expected["counterevidence_refs"]),
            })
        response = {
            "schema_version": REDUCTION_RESPONSE_SCHEMA,
            "node_id": request["node_id"],
            "tree_node_id": node["tree_node_id"],
            "child_node_ids": list(request["child_node_ids"]),
            "child_result_hashes": list(request["child_result_hashes"]),
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "unit_ids": list(node["unit_ids"]),
            "group_summaries": rows,
        }
        result_by_tree_id[node["tree_node_id"]] = validate_arbitration_reduction_response(
            plan, request, response, children, estimate_output_tokens=_output_estimator,
        )
    return result_by_tree_id[plan["reducer_root_node_id"]]


@pytest.mark.parametrize("candidate_count", [1, 2, 5, 12])
def test_complete_source_bound_overflow_plan_has_exact_candidate_scope_and_measured_requests(tmp_path, candidate_count):
    inventory, _scope, contents, _intent, _disposition, legacy_plan, ledger, plan = _build_ready(
        tmp_path, candidate_count=candidate_count, slot_count=1,
        caps=_caps(output_cap=30_000),
    )

    assert plan["candidate_ids"] == sorted(f"candidate_{index + 1}" for index in range(candidate_count))
    assert plan["counts"]["candidate_count"] == candidate_count
    assert plan["counts"]["review_group_count"] == 1
    assert plan["counts"]["assessment_unit_count"] == len(legacy_plan["units"])
    assert plan["critical_view_set"]["critical_views"]
    assert all(row["source_bindings"]["source_inventory_artifact_hash"] == inventory.artifact_hash
               for row in plan["critical_view_set"]["critical_views"])
    assert all(row["source_bindings"]["source_relation_direction"] == {"status": "unknown", "value": None}
               for row in plan["critical_view_set"]["critical_views"])
    assert '"critical_for":' not in json.dumps(plan["critical_view_set"])
    assert plan["counts"]["new_assessment_leaf_count"] <= plan["counts"]["assessment_unit_count"]
    assert plan["counts"]["new_reducer_count"] <= max(0, plan["counts"]["new_assessment_leaf_count"] - 1)
    assert plan["counts"]["reduction_depth"] == (
        0 if plan["counts"]["new_assessment_leaf_count"] <= 1
        else math.ceil(math.log2(plan["counts"]["new_assessment_leaf_count"]))
    )
    for node in plan["assessment_leaves"]:
        request = node["request"]
        assert request["_prompt_authority"]["prompt_sha256"] == "a" * 64
        assert request["candidate_ids"] == plan["candidate_ids"]
        assert request["candidate_hashes"] == plan["candidate_hashes"]
        assert request["decision_scope"]["schema_version"] == "arbitration_decision_scope/v2"
        assert request["critical_view_wire"]["source_literal_bundle_catalog"]
        assert expand_arbitration_critical_view_wire(
            request["critical_view_wire"],
            expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
        )
        assert all(
            set(section).isdisjoint({"title", "goal", "rationale", "layout_expression"})
            for candidate in request["candidate_units"]
            for unit in candidate["unit_rows"]
            for section in unit["sections"]
        )
        assert node["estimated_input_tokens"] == _input_estimator(request)["estimated_input_tokens"]
        assert _output_estimator(node["response_shape"]) == node["requested_output_tokens"]
        assert node["estimated_input_tokens"] <= 32_000
        assert node["requested_output_tokens"] == request["output_contract"]["max_output_tokens"]
        assert node["requested_output_tokens"] <= plan["serialization_caps"]["route_max_output"]
        assert (
            node["estimated_input_tokens"] + node["requested_output_tokens"]
            + plan["serialization_caps"]["reasoning_reserve"]
            + plan["serialization_caps"]["safety_margin"]
            <= plan["serialization_caps"]["verified_context_limit"]
        )
    assert compute_v3_hash(legacy_plan) == plan["identity"]["legacy_plan_hash"] or plan["identity"]["legacy_plan_hash"] == legacy_plan["plan_hash"]
    assert compute_v3_hash(ledger) != plan["identity"]["legacy_ledger_hash"]  # canonical self-hash is the bound legacy domain
    assert set(contents) == set(plan["candidate_ids"])


@pytest.mark.parametrize("leaf_count,expected_depth,expected_odd_levels", [(1, 0, []), (4, 2, []), (5, 3, [1, 2])])
def test_binary_reducer_tree_uses_adjacent_pairs_and_odd_carry(leaf_count, expected_depth, expected_odd_levels):
    units = [{"unit_id": f"unit:{index}", "ordinal": index} for index in range(leaf_count)]
    leaves = [{"node_id": f"leaf:{index}", "unit_ids": [f"unit:{index}"]} for index in range(leaf_count)]
    tree = _build_reducer_tree(
        leaves=leaves,
        units=units,
        candidate_ids=["candidate_1"],
        candidate_hashes={"candidate_1": "a" * 64},
        group_map={"group_map_hash": "b" * 64},
        plan_input_hash="c" * 64,
        caps=_caps(),
    )
    assert tree["depth"] == expected_depth
    assert tree["odd_carry_levels"] == expected_odd_levels
    assert len(tree["nodes"]) == max(0, leaf_count - 1)
    if leaf_count:
        assert set(next(row for row in tree["nodes"] if row["tree_node_id"] == tree["root_node_id"])["unit_ids"] if tree["nodes"] else leaves[0]["unit_ids"]) == {
            row["unit_id"] for row in units
        }


def test_leaf_and_reducer_responses_reject_omissions_duplicates_and_foreign_views(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=3, caps=_caps(input_cap=8_000),
    )
    leaves = _ready_leaf_results(plan)
    assert plan["reducer_tree"]["nodes"]
    first = plan["assessment_leaves"][0]
    valid = _typed_leaf_response(plan, first)

    omitted = copy.deepcopy(valid)
    omitted["assessments"].pop()
    with pytest.raises(ArbitrationAggregationError, match="omitted or added candidate/unit"):
        validate_arbitration_assessment_response(
            plan, first["node_id"], omitted, estimate_output_tokens=_output_estimator,
        )

    duplicate = copy.deepcopy(valid)
    duplicate["assessments"].append(copy.deepcopy(duplicate["assessments"][0]))
    with pytest.raises(ArbitrationAggregationError, match="duplicates"):
        validate_arbitration_assessment_response(
            plan, first["node_id"], duplicate, estimate_output_tokens=_output_estimator,
        )

    unknown = copy.deepcopy(valid)
    unknown["layout_expression"] = "forbidden during all-candidate assessment"
    with pytest.raises(ArbitrationAggregationError, match="unknown fields"):
        validate_arbitration_assessment_response(
            plan, first["node_id"], unknown, estimate_output_tokens=_output_estimator,
        )

    foreign = copy.deepcopy(valid)
    first_view = foreign["assessments"][0]["view_assessments"]
    if first_view:
        first_view[0]["decision_view_id"] = "decision-view:foreign"
        with pytest.raises(ArbitrationAggregationError, match="foreign or stale"):
            validate_arbitration_assessment_response(
                plan, first["node_id"], foreign, estimate_output_tokens=_output_estimator,
            )
        foreign_evidence = copy.deepcopy(valid)
        foreign_evidence["assessments"][0]["view_assessments"][0]["evidence_refs"] = ["evidence:foreign"]
        with pytest.raises(ArbitrationAggregationError, match="literal|wire"):
            validate_arbitration_assessment_response(
                plan, first["node_id"], foreign_evidence, estimate_output_tokens=_output_estimator,
            )

    if plan["reducer_tree"]["nodes"]:
        node = plan["reducer_tree"]["nodes"][0]
        children = {child_id: leaves[child_id] for child_id in node["child_node_ids"]}
        request = build_arbitration_reducer_request(
            plan, node["tree_node_id"], children,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(),
        )
        response = _typed_reduction_response(plan, node, request)
        validate_arbitration_reduction_response(
            plan, request, response, children, estimate_output_tokens=_output_estimator,
        )
        missing_group = copy.deepcopy(response)
        missing_group["group_summaries"].pop()
        with pytest.raises(ArbitrationAggregationError, match="omitted or added"):
            validate_arbitration_reduction_response(
                plan, request, missing_group, children, estimate_output_tokens=_output_estimator,
            )


def test_reducer_validator_rebinds_request_hashes_and_expected_groups_to_ready_children(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=3, caps=_caps(input_cap=8_000),
    )
    leaves = _ready_leaf_results(plan)
    reducer = plan["reducer_tree"]["nodes"][0]
    children = {child_id: leaves[child_id] for child_id in reducer["child_node_ids"]}
    request = build_arbitration_reducer_request(
        plan, reducer["tree_node_id"], children,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )

    def response_for(source_request):
        rows = []
        for expected in source_request["expected_group_summaries"]:
            present = bool(expected["unit_ids"])
            rows.append({
                "candidate_id": expected["candidate_id"],
                "candidate_hash": plan["candidate_hashes"][expected["candidate_id"]],
                "group_id": expected["group_id"],
                "group_hash": expected["group_hash"],
                "presence": "present" if present else "not_applicable",
                "unit_ids": list(expected["unit_ids"]),
                "decision_view_ids": list(expected["decision_view_ids"]),
                "assessed_direction": expected["assessed_direction"],
                "assessed_disagreement": expected["assessed_disagreement"],
                "assessed_direction_values": list(expected["assessed_direction_values"]),
                "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
                "child_lineage": copy.deepcopy(expected["child_lineage"]),
                "summary": "Group." if present else "",
                "evidence_refs": list(expected["evidence_refs"]),
                "counterevidence_refs": list(expected["counterevidence_refs"]),
            })
        return {
            "schema_version": REDUCTION_RESPONSE_SCHEMA,
            "node_id": source_request["node_id"],
            "tree_node_id": source_request["tree_node_id"],
            "child_node_ids": list(source_request["child_node_ids"]),
            "child_result_hashes": list(source_request["child_result_hashes"]),
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "unit_ids": list(reducer["unit_ids"]),
            "group_summaries": rows,
        }

    valid_response = response_for(request)
    forged_hash_request = copy.deepcopy(request)
    forged_hashes = ["0" * 64 for _ in forged_hash_request["child_node_ids"]]
    forged_hash_request["child_result_hashes"] = forged_hashes
    forged_hash_request["node_id"] = "arbitration-reducer:" + compute_v3_hash({
        "schema_version": REDUCTION_INPUT_SCHEMA,
        "plan_input_hash": plan["plan_input_hash"],
        "reducer_tree_node_id": reducer["tree_node_id"],
        "ordered_child_node_ids": list(reducer["child_node_ids"]),
        "ordered_child_result_hashes": forged_hashes,
        "unit_ids": list(reducer["unit_ids"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "review_group_map_hash": plan["review_group_map"]["group_map_hash"],
        "route_identity": plan["identity"]["route_identity"],
    })[:24]
    forged_hash_request["request"]["node_id"] = forged_hash_request["node_id"]
    forged_hash_request["request"]["_prompt_authority"]["node_id"] = forged_hash_request["node_id"]
    forged_hash_request["request_hash"] = compute_v3_hash(forged_hash_request["request"])
    forged_response = response_for(forged_hash_request)

    with pytest.raises(ArbitrationAggregationError, match="child result hashes.*READY|request identity"):
        validate_arbitration_reduction_response(
            plan, forged_hash_request, forged_response, children,
            estimate_output_tokens=_output_estimator,
        )

    altered_expectations = copy.deepcopy(request)
    summary = next(row for row in altered_expectations["expected_group_summaries"] if row["child_lineage"])
    summary["child_lineage"][0]["child_result_hash"] = "0" * 64
    with pytest.raises(ArbitrationAggregationError, match="expected group summaries.*READY children"):
        validate_arbitration_reduction_response(
            plan, altered_expectations, valid_response, children,
            estimate_output_tokens=_output_estimator,
        )


def test_three_leaf_reducer_pipeline_validates_odd_carry_and_reaches_root(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=3, caps=_caps(input_cap=8_000),
    )
    assert len(plan["assessment_leaves"]) == 3
    tree = plan["reducer_tree"]
    assert tree["depth"] == 2
    assert tree["odd_carry_levels"] == [1]
    leaves = _ready_leaf_results(plan)

    root = _ready_reduction_root(plan, leaves)

    assert root["tree_node_id"] == tree["root_node_id"]
    assert set(root["unit_ids"]) == {row["unit_id"] for row in plan["assessment_units"]}
    assert all(
        row["child_lineage"] and row["typed_lineage"]
        for row in root["group_summaries"] if row["unit_ids"]
    )

    first_reducer = tree["nodes"][0]
    first_children = {child_id: leaves[child_id] for child_id in first_reducer["child_node_ids"]}
    first_request = build_arbitration_reducer_request(
        plan, first_reducer["tree_node_id"], first_children,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )
    first_result = validate_arbitration_reduction_response(
        plan, first_request,
        _typed_reduction_response(plan, first_reducer, first_request),
        first_children,
        estimate_output_tokens=_output_estimator,
    )
    forged_child = copy.deepcopy(first_result)
    group_row = next(row for row in forged_child["group_summaries"] if row["child_lineage"])
    group_row["child_lineage"][0]["child_result_hash"] = "0" * 64
    forged_child["result_hash"] = compute_v3_hash({
        key: value for key, value in forged_child.items() if key != "result_hash"
    })
    root_reducer = next(row for row in tree["nodes"] if row["tree_node_id"] == tree["root_node_id"])
    root_children = {
        child_id: (forged_child if child_id == first_reducer["tree_node_id"] else leaves[child_id])
        for child_id in root_reducer["child_node_ids"]
    }
    with pytest.raises(ArbitrationAggregationError, match="child lineage has a stale result hash"):
        build_arbitration_reducer_request(
            plan, root_reducer["tree_node_id"], root_children,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(),
        )


def test_reducer_cannot_erase_null_reverse_or_minority_lineage_from_ready_children(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=3, caps=_caps(input_cap=8_000),
    )
    assert len(plan["assessment_leaves"]) == 3
    task_group_id = next(
        row["group_id"] for row in plan["review_group_map"]["groups"]
        if row["group_kind"] == "task_topic"
    )
    leaf_results = {}
    direct_values = [
        ("null", "uncertain"),
        ("reverse", "disagrees"),
        ("mixed", "uncertain"),
    ]
    for index, leaf in enumerate(plan["assessment_leaves"]):
        response = _typed_leaf_response(plan, leaf)
        if index < len(direct_values):
            direction, disagreement = direct_values[index]
            unit_assessment = next(
                row for row in response["assessments"]
                if row["candidate_id"] == "candidate_1"
            )
            unit_assessment["assessed_direction"] = direction
            unit_assessment["assessed_disagreement"] = disagreement
            if index < 2:
                unit_assessment["counterevidence_refs"] = [unit_assessment["evidence_refs"][0]]
                for view in unit_assessment["view_assessments"]:
                    literal_refs = view["evidence_refs"]
                    if literal_refs:
                        view["counterevidence_refs"] = [literal_refs[0]]
                        view["assessed_direction"] = direction
                        view["assessed_disagreement"] = disagreement
            else:
                for view in unit_assessment["view_assessments"]:
                    view["assessed_direction"] = direction
                    view["assessed_disagreement"] = disagreement
            response["group_contributions"] = _refresh_group_contributions(plan, leaf, response)
        leaf_results[leaf["node_id"]] = validate_arbitration_assessment_response(
            plan, leaf["node_id"], response, estimate_output_tokens=_output_estimator,
        )

    reducer = plan["reducer_tree"]["nodes"][0]
    children = {child_id: leaf_results[child_id] for child_id in reducer["child_node_ids"]}
    request = build_arbitration_reducer_request(
        plan,
        reducer["tree_node_id"],
        children,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )
    rows = []
    for expected in request["expected_group_summaries"]:
        present = bool(expected["unit_ids"])
        direction = "supports" if present else "not_comparable"
        disagreement = "consistent" if present else "not_applicable"
        if expected["candidate_id"] == "candidate_1" and expected["group_id"] == task_group_id:
            # This superficially plausible aggregate erases null/reverse and
            # minority evidence that are present in the exact READY children.
            direction = "supports"
            disagreement = "consistent"
        rows.append({
            "candidate_id": expected["candidate_id"],
            "candidate_hash": plan["candidate_hashes"][expected["candidate_id"]],
            "group_id": expected["group_id"],
            "group_hash": expected["group_hash"],
            "presence": "present" if present else "not_applicable",
            "unit_ids": list(expected["unit_ids"]),
            "decision_view_ids": list(expected["decision_view_ids"]),
            "assessed_direction": expected["assessed_direction"],
            "assessed_disagreement": expected["assessed_disagreement"],
            "assessed_direction_values": list(expected["assessed_direction_values"]),
            "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
            "child_lineage": copy.deepcopy(expected["child_lineage"]),
            "summary": "Group." if present else "",
            "evidence_refs": list(expected["evidence_refs"]),
            "counterevidence_refs": list(expected["counterevidence_refs"]),
        })
        if expected["candidate_id"] == "candidate_1" and expected["group_id"] == task_group_id:
            # This superficially plausible aggregate erases null/reverse and
            # minority evidence that are present in the exact READY children.
            rows[-1]["assessed_direction"] = "supports"
            rows[-1]["assessed_disagreement"] = "consistent"
            rows[-1]["assessed_direction_values"] = ["supports"]
            rows[-1]["assessed_disagreement_values"] = ["consistent"]
            rows[-1]["counterevidence_refs"] = []
            rows[-1]["child_lineage"] = []
    response = {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "node_id": request["node_id"],
        "tree_node_id": reducer["tree_node_id"],
        "child_node_ids": list(request["child_node_ids"]),
        "child_result_hashes": list(request["child_result_hashes"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": list(reducer["unit_ids"]),
        "group_summaries": rows,
    }
    with pytest.raises(ArbitrationAggregationError, match="direction|disagreement|counterevidence|lineage"):
        validate_arbitration_reduction_response(
            plan, request, response, children, estimate_output_tokens=_output_estimator,
        )


def test_global_selection_uses_all_critical_views_and_detail_required_cannot_choose(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=1, slot_count=1,
    )
    leaves = _ready_leaf_results(plan)
    root = _ready_reduction_root(plan, leaves)
    selector = build_global_selection_request(
        plan,
        root,
        leaves,
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(detail_ids=3),
    )
    assert selector["request"]["_prompt_authority"]["node_id"] == selector["node_id"]
    assert selector["request"]["candidate_ids"] == ["candidate_1"]
    assert selector["request"]["critical_assessments"]
    assert selector["estimated_input_tokens"] == _input_estimator(selector["request"])["estimated_input_tokens"]
    selected = {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": selector["node_id"],
        "candidate_ids": ["candidate_1"],
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": "selected",
        "selected_candidate_id": "candidate_1",
        "detail_required_view_ids": [],
        "rationale": "Sole candidate.",
    }
    assert validate_global_selection_response(
        plan, selector, selected, estimate_output_tokens=_output_estimator,
    )["selected_candidate_id"] == "candidate_1"
    tampered = {**selected, "selected_candidate_id": "foreign-candidate"}
    with pytest.raises(ArbitrationAggregationError, match="outside the exact eligible"):
        validate_global_selection_response(plan, selector, tampered, estimate_output_tokens=_output_estimator)


def test_global_detail_required_names_only_noncritical_fact_ids_and_publishes_no_winner(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=1,
    )
    leaves = _ready_leaf_results(plan)
    root = _ready_reduction_root(plan, leaves)
    selector = build_global_selection_request(
        plan,
        root,
        leaves,
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(detail_ids=2),
    )
    assert selector["noncritical_fact_ids"]
    detail_id = selector["noncritical_fact_ids"][0]
    response = {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": selector["node_id"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": "detail_required",
        "selected_candidate_id": None,
        "detail_required_view_ids": [detail_id],
        "rationale": "Need detail.",
    }
    validated = validate_global_selection_response(
        plan, selector, response, estimate_output_tokens=_output_estimator,
    )
    assert validated["outcome"] == "detail_required"
    assert validated["selected_candidate_id"] is None

    foreign = {**response, "detail_required_view_ids": ["decision-view:foreign"]}
    with pytest.raises(ArbitrationAggregationError, match="foreign or critical"):
        validate_global_selection_response(plan, selector, foreign, estimate_output_tokens=_output_estimator)


def _selector_detail_setup(tmp_path):
    inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=1,
    )
    leaves = _ready_leaf_results(plan)
    root = _ready_reduction_root(plan, leaves)
    selector = build_global_selection_request(
        plan,
        root,
        leaves,
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(detail_ids=2),
    )
    response = {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": selector["node_id"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": "detail_required",
        "selected_candidate_id": None,
        "detail_required_view_ids": [selector["noncritical_fact_ids"][0]],
        "rationale": "One exact noncritical fact is needed.",
    }
    selection = validate_global_selection_response(
        plan, selector, response, estimate_output_tokens=_output_estimator,
    )
    return inventory, plan, selector, selection


def _selector_detail_responses(detail_plan, plan):
    return [
        {
            "request_id": request["request_id"],
            "response": {
                "schema_version": SELECTION_DETAIL_RESPONSE_SCHEMA,
                "node_id": request["request_id"],
                "prior_selector_node_id": detail_plan["prior_selector_node_id"],
                "prior_selector_request_hash": detail_plan["prior_selector_request_hash"],
                "prior_selection_result_hash": detail_plan["prior_selection_result_hash"],
                "candidate_ids": list(plan["candidate_ids"]),
                "candidate_hashes": dict(plan["candidate_hashes"]),
                "detail_view_id": request["detail_view_id"],
                "detail_view_hash": request["detail_view_hash"],
                "source_fact_hash": request["source_fact_hash"],
                "assessment_status": "resolved",
                "assessed_direction": "mixed",
                "assessed_disagreement": "uncertain",
                "cited_detail_view_ids": [request["detail_view_id"]],
                "reason": "x",
            },
        }
        for request in detail_plan["requests"]
    ]


def test_detail_required_uses_verified_literal_detail_and_one_final_selector(tmp_path):
    inventory, plan, selector, selection = _selector_detail_setup(tmp_path)
    caps = _caps(detail_ids=2)
    detail_plan = build_global_selection_detail_requests(
        plan, selector, selection, inventory,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    assert detail_plan["counts"] == {"Qselector_detail": 1, "Qfinal_selector": 1, "Qtotal": 2}
    detail_request = detail_plan["requests"][0]
    assert detail_request["body"]["source_literal_fact"]
    assert detail_request["body"]["candidate_ids"] == plan["candidate_ids"]
    assert detail_request["body"]["followup_ordinal"] == 1
    assert detail_request["body"]["max_followups"] == 1
    assert detail_request["estimated_input_tokens"] <= caps.effective_input_cap
    assert detail_request["requested_output_tokens"] <= caps.route_max_output
    assert detail_request["request"]["_prompt_authority"]["node_id"] == detail_request["request_id"]
    assert detail_request["measurement"]["output_reserve"]["response_schema"] == SELECTION_DETAIL_RESPONSE_SCHEMA
    assert detail_request["measurement"]["context_total_tokens"] <= caps.verified_context_limit

    detail_results = validate_global_selection_detail_responses(
        plan, selector, selection, detail_plan, _selector_detail_responses(detail_plan, plan),
        source_inventory=inventory,
        actual_requests=detail_plan["requests"],
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    assert selection["selector_request_node_id"] == selector["node_id"]
    assert selection["selector_request_hash"] == selector["request_hash"]
    assert selection["selector_request_full_hash"] == compute_v3_hash(selector["request"])
    resigned_details = detail_results.to_dict()
    resigned_details["detail_assessments"][0]["assessed_direction"] = "contradicts"
    resigned_details["result_hash"] = compute_v3_hash({
        key: value for key, value in resigned_details.items() if key != "result_hash"
    })
    with pytest.raises(ArbitrationAggregationError, match="immutable compiler/validator-issued"):
        build_global_selection_followup_request(
            plan, selector, selection, detail_plan, resigned_details, inventory,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=caps,
        )
    final_selector = build_global_selection_followup_request(
        plan, selector, selection, detail_plan, detail_results, inventory,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    assert final_selector["max_detail_required_ids"] == 0
    assert final_selector["noncritical_fact_ids"] == []
    assert final_selector["counts"] == {"Qfinal_selector": 1, "Qselector_detail": 1}
    assert final_selector["request"]["selection_policy"]["allowed_outcomes"] == ["selected"]
    assert final_selector["request"]["candidate_ids"] == plan["candidate_ids"]
    assert final_selector["request"]["detail_assessments"][0]["source_literal_fact"] == detail_request["body"]["source_literal_fact"]
    assert final_selector["estimated_input_tokens"] == _input_estimator(final_selector["request"])["estimated_input_tokens"]
    assert final_selector["request"]["_prompt_authority"]["node_id"] == final_selector["node_id"]
    assert final_selector["requested_output_tokens"] <= caps.route_max_output
    assert (
        final_selector["estimated_input_tokens"] + final_selector["requested_output_tokens"]
        + caps.reasoning_reserve + caps.safety_margin <= caps.verified_context_limit
    )

    final_response = {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": final_selector["node_id"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": "selected",
        "selected_candidate_id": plan["candidate_ids"][-1],
        "detail_required_view_ids": [],
        "rationale": "x",
    }
    final_result = validate_global_selection_response(
        plan, final_selector, final_response, estimate_output_tokens=_output_estimator,
    )
    assert final_result["outcome"] == "selected"
    assert final_result["selected_candidate_id"] == plan["candidate_ids"][-1]

    repeated_detail = {
        **final_response,
        "outcome": "detail_required",
        "selected_candidate_id": None,
        "detail_required_view_ids": [selection["detail_required_view_ids"][0]],
    }
    with pytest.raises(ArbitrationAggregationError, match="finite maximum|at least one"):
        validate_global_selection_response(
            plan, final_selector, repeated_detail, estimate_output_tokens=_output_estimator,
        )


def test_selector_detail_rejects_missing_literal_wrong_scope_echo_and_unresolved(tmp_path):
    inventory, plan, selector, selection = _selector_detail_setup(tmp_path)
    caps = _caps(detail_ids=2)
    detail_plan = build_global_selection_detail_requests(
        plan, selector, selection, inventory,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=caps,
    )
    good_responses = _selector_detail_responses(detail_plan, plan)

    resigned_work_plan = detail_plan.to_dict()
    resigned_work_request = resigned_work_plan["requests"][0]
    resigned_work_request["body"].pop("source_literal_fact")
    resigned_work_request["request"].pop("source_literal_fact")
    resigned_work_request["request_hash"] = compute_v3_hash(resigned_work_request["request"])
    resigned_work_request["measurement"]["request_hash"] = resigned_work_request["request_hash"]
    resigned_work_plan["work_plan_hash"] = compute_v3_hash({
        key: value for key, value in resigned_work_plan.items() if key != "work_plan_hash"
    })
    with pytest.raises(ArbitrationAggregationError, match="immutable compiler/validator-issued"):
        validate_global_selection_detail_responses(
            plan, selector, selection, resigned_work_plan, good_responses,
            source_inventory=inventory, actual_requests=resigned_work_plan["requests"],
            estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
        )

    resigned_selector = selector.to_dict()
    resigned_selector["request"]["candidate_group_summaries"][0]["summary"] += " changed"
    resigned_selector["request_hash"] = compute_v3_hash(resigned_selector["request"])
    with pytest.raises(ArbitrationAggregationError, match="immutable compiler/validator-issued"):
        build_global_selection_detail_requests(
            plan, resigned_selector, selection, inventory,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=caps,
        )

    resigned_selection = selection.to_dict()
    resigned_selection["rationale"] = "Re-signed but not validator-issued."
    resigned_selection["result_hash"] = compute_v3_hash({
        key: value for key, value in resigned_selection.items() if key != "result_hash"
    })
    with pytest.raises(ArbitrationAggregationError, match="immutable compiler/validator-issued"):
        build_global_selection_detail_requests(
            plan, selector, resigned_selection, inventory,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=caps,
        )

    missing_literal_request = copy.deepcopy(detail_plan["requests"])
    row = missing_literal_request[0]
    row["body"].pop("source_literal_fact")
    row["request"].pop("source_literal_fact")
    row["request_hash"] = compute_v3_hash(row["request"])
    row["measurement"]["request_hash"] = row["request_hash"]
    with pytest.raises(ArbitrationAggregationError, match="actual selector detail request differs"):
        validate_global_selection_detail_responses(
            plan, selector, selection, detail_plan, good_responses,
            source_inventory=inventory, actual_requests=missing_literal_request,
            estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
        )

    wrong_scope_request = copy.deepcopy(detail_plan["requests"])
    row = wrong_scope_request[0]
    row["body"]["detail_view"]["candidate_id"] = plan["candidate_ids"][-1]
    row["request"]["detail_view"]["candidate_id"] = plan["candidate_ids"][-1]
    row["request_hash"] = compute_v3_hash(row["request"])
    row["measurement"]["request_hash"] = row["request_hash"]
    with pytest.raises(ArbitrationAggregationError, match="actual selector detail request differs"):
        validate_global_selection_detail_responses(
            plan, selector, selection, detail_plan, good_responses,
            source_inventory=inventory, actual_requests=wrong_scope_request,
            estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
        )

    bad_echoes = copy.deepcopy(good_responses)
    bad_echoes[0]["response"]["detail_view_hash"] = "0" * 64
    with pytest.raises(ArbitrationAggregationError, match="changed detail_view_hash"):
        validate_global_selection_detail_responses(
            plan, selector, selection, detail_plan, bad_echoes,
            source_inventory=inventory, actual_requests=detail_plan["requests"],
            estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
        )

    unresolved = copy.deepcopy(good_responses)
    unresolved[0]["response"]["assessment_status"] = "unresolved"
    with pytest.raises(ArbitrationAggregationError, match="unresolved selector detail"):
        validate_global_selection_detail_responses(
            plan, selector, selection, detail_plan, unresolved,
            source_inventory=inventory, actual_requests=detail_plan["requests"],
            estimate_input_tokens=_input_estimator, estimate_output_tokens=_output_estimator, caps=caps,
        )


def test_selection_detail_followup_uses_prompt_registry_and_actual_provider_profile(tmp_path):
    profile = ProviderContextProfile.conservative(
        provider="local-selector-detail",
        model="profile-v1",
        endpoint_type="chat_completions",
        model_context_limit=100_000,
        max_output_tokens=12_000,
        reasoning_reserve=512,
        safety_margin=256,
    )
    route_identity = {
        "provider": profile.provider,
        "model": profile.model,
        "endpoint_type": profile.endpoint_type,
    }
    caps = ArbitrationSerializationCaps(
        effective_input_cap=min(32_000, int(profile.input_budget)),
        verified_context_limit=int(profile.verified_context_limit),
        route_max_output=int(profile.max_output_tokens),
        reasoning_reserve=int(profile.reasoning_reserve),
        safety_margin=int(profile.safety_margin),
        route_identity=route_identity,
        assessment_text_tokens=4,
        group_summary_text_tokens=5,
        selection_text_tokens=64,
        max_detail_required_ids=2,
    )
    inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=1, caps=caps,
    )
    executor = object.__new__(OutlineV3Executor)
    executor.prompt_registry = PromptRegistry()
    executor._outline_prompt_identity = executor.prompt_registry.identity("outline.node.system.v3")
    executor._outline_policy_identity = executor.prompt_registry.identity("outline.node.policies.v3")

    def production_builder(kind, payload):
        return executor._attach_prompt_authority(str(payload.get("node_id") or kind), payload)

    def route_estimator(request):
        return profile.estimate_request(request)

    def output_estimator(response_shape):
        return profile.estimate_tokens(response_shape)

    leaves = _ready_leaf_results(plan)
    root = _ready_reduction_root(plan, leaves)
    selector = build_global_selection_request(
        plan, root, leaves,
        expected_view_set_hash=plan["critical_view_set"]["view_set_hash"],
        request_builder=production_builder,
        estimate_input_tokens=route_estimator,
        estimate_output_tokens=output_estimator,
        caps=caps,
    )
    view_id = selector["noncritical_fact_ids"][0]
    selection = validate_global_selection_response(
        plan, selector,
        {
            "schema_version": SELECTION_RESPONSE_SCHEMA,
            "node_id": selector["node_id"],
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "outcome": "detail_required",
            "selected_candidate_id": None,
            "detail_required_view_ids": [view_id],
            "rationale": "x",
        },
        estimate_output_tokens=output_estimator,
    )
    detail_plan = build_global_selection_detail_requests(
        plan, selector, selection, inventory,
        request_builder=production_builder,
        estimate_input_tokens=route_estimator,
        estimate_output_tokens=output_estimator,
        caps=caps,
    )
    detail_record = detail_plan["requests"][0]
    assert detail_record["request"]["_prompt_authority"]["node_id"] == detail_record["request_id"]
    assert detail_record["measurement"]["route_identity"] == route_identity
    assert detail_record["measurement"]["estimated_input_tokens"] == profile.estimate_request(
        detail_record["request"]
    )["estimated_input_tokens"]
    detail_results = validate_global_selection_detail_responses(
        plan, selector, selection, detail_plan, _selector_detail_responses(detail_plan, plan),
        source_inventory=inventory, actual_requests=detail_plan["requests"],
        estimate_input_tokens=route_estimator, estimate_output_tokens=output_estimator, caps=caps,
    )
    final_selector = build_global_selection_followup_request(
        plan, selector, selection, detail_plan, detail_results, inventory,
        request_builder=production_builder,
        estimate_input_tokens=route_estimator,
        estimate_output_tokens=output_estimator,
        caps=caps,
    )
    assert final_selector["request"]["_prompt_authority"]["node_id"] == final_selector["node_id"]
    assert final_selector["request"]["selection_policy"]["allowed_outcomes"] == ["selected"]
    assert final_selector["max_detail_required_ids"] == 0
    assert final_selector["measurement"]["route_identity"] == route_identity
    assert final_selector["measurement"]["output_reserve"]["response_shape_hash"] == compute_v3_hash(
        final_selector["response_shape"]
    )
    assert final_selector["measurement"]["output_reserve"]["estimated_output_tokens"] == output_estimator(
        final_selector["response_shape"]
    )


def test_critical_view_cannot_cite_source_evidence_id_absent_from_literal_wire_rows(tmp_path):
    _inventory, _scope, _contents, _intent, _disposition, _legacy_plan, _ledger, plan = _build_ready(
        tmp_path, candidate_count=1, slot_count=1,
    )
    leaf = plan["assessment_leaves"][0]
    response = _typed_leaf_response(plan, leaf)
    view = plan["critical_view_set"]["critical_views"][0]
    literal_evidence_ids = {
        row["evidence_id"]
        for row in view["literal_critical_detail"]["evidence"]
    }
    bound_evidence_ids = set(view["source_bindings"]["evidence_ids"])
    absent_literal_ids = sorted(bound_evidence_ids - literal_evidence_ids)
    assert absent_literal_ids, "fixture must contain closure evidence not literal on the critical view wire"
    target = next(
        row for row in response["assessments"][0]["view_assessments"]
        if row["decision_view_id"] == view["decision_view_id"]
    )
    target["evidence_refs"] = [absent_literal_ids[0]]

    with pytest.raises(ArbitrationAggregationError, match="literal|wire"):
        validate_arbitration_assessment_response(
            plan, leaf["node_id"], response, estimate_output_tokens=_output_estimator,
        )


def test_support_claim_group_is_derived_from_authoritative_slot_not_provider_echo(tmp_path):
    candidate_ids = ["candidate_1", "candidate_2"]
    inventory, _paper, scope = _source_scope(tmp_path, slot_count=3)
    contents = _candidate_contents(scope, candidate_ids)
    for content in contents.values():
        for section in content["sections"]:
            for support in section["claim_support"]:
                support["claim_group_id"] = "claim-group:provider-forgery"
    intent = {"review_question": "Compare source-bound assertions.", "required_comparisons": []}
    _hashes, disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
        candidate_ids, contents, scope, intent, [],
    )
    legacy_source_request = _prepared_legacy_source_request(
        candidate_ids, legacy_plan["candidate_hashes"], contents, disposition, intent,
    )

    with pytest.raises(ArbitrationAggregationError, match="claim_group_id differs"):
        build_overflow_assessment_plan(
            legacy_plan=legacy_plan,
            legacy_ledger=ledger,
            legacy_source_request=legacy_source_request,
            candidate_contents=legacy_source_request["candidate_contents"],
            candidate_scope=scope,
            source_inventory=inventory,
            review_intent=intent,
            critique_disposition=disposition,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(),
        )


def test_production_style_legacy_provenance_is_verified_without_retagging_v1(tmp_path):
    candidate_ids = ["candidate_1", "candidate_2"]
    inventory, _paper, scope = _source_scope(tmp_path, slot_count=2)
    contents = _candidate_contents(scope, candidate_ids)
    intent = {
        "review_question": "Compare the recorded findings.",
        "scope": "Use the verified source context.",
        "required_comparisons": [],
    }
    candidate_hashes = {candidate_id: compute_v3_hash(contents[candidate_id]) for candidate_id in candidate_ids}
    disposition = {
        "schema_version": "outline_critique_disposition/v1",
        "global_blocker": False,
        "blocked_candidate_ids": [],
        "eligible_candidate_ids": list(candidate_ids),
        "issues": [],
    }
    legacy_source_request = _prepared_legacy_source_request(
        candidate_ids, candidate_hashes, contents, disposition, intent,
    )
    provenance_hashes = {
        "request": hash_json(legacy_source_request),
        "intent": hash_json(legacy_source_request.get("review_intent") or {}),
    }
    _hashes, ready_disposition, legacy_plan, ledger = _legacy_plan_and_ledger(
        candidate_ids,
        contents,
        scope,
        intent,
        [],
        provenance_hashes=provenance_hashes,
        legacy_source_request=legacy_source_request,
    )
    before_plan = copy.deepcopy(legacy_plan)
    before_ledger_bytes = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    derived = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=ledger,
        legacy_source_request=legacy_source_request,
        candidate_contents=legacy_source_request["candidate_contents"],
        candidate_scope=scope,
        source_inventory=inventory,
        review_intent=intent,
        critique_disposition=ready_disposition,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )

    assert legacy_plan == before_plan
    assert json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") == before_ledger_bytes
    assert legacy_plan["provenance_hashes"] == provenance_hashes
    assert ledger["provenance_hashes"] == provenance_hashes
    assert derived["identity"]["legacy_request_hash"] == provenance_hashes["request"]
    assert derived["identity"]["legacy_intent_hash"] == provenance_hashes["intent"]
    assert derived["identity"]["candidate_output_scope_hash"] == scope.content_hash


def test_actual_executor_prepared_request_and_intent_provenance_are_accepted(tmp_path):
    candidate_ids = ["candidate_1", "candidate_2"]
    inventory, _paper, scope = _source_scope(tmp_path, slot_count=2)
    contents = _candidate_contents(scope, candidate_ids, claim_length=1_000)
    intent = {
        "review_question": "Compare the recorded findings.",
        "scope": "Use the verified source context.",
        "required_comparisons": [],
    }
    candidate_hashes = {
        candidate_id: compute_v3_hash(contents[candidate_id])
        for candidate_id in candidate_ids
    }
    disposition = {
        "schema_version": "outline_critique_disposition/v1",
        "global_blocker": False,
        "blocked_candidate_ids": [],
        "eligible_candidate_ids": list(candidate_ids),
        "issues": [],
    }
    request = _prepared_legacy_source_request(
        candidate_ids, candidate_hashes, contents, disposition, intent,
    )

    prepared = _production_hierarchy(request)

    assert prepared is not None
    legacy_plan = prepared["plan"]
    legacy_source_request = prepared["source_request"]
    assert legacy_plan["provenance_hashes"] == {
        "request": hash_json(legacy_source_request),
        "intent": hash_json(legacy_source_request.get("review_intent") or {}),
    }
    ledger = _legacy_ledger_from_plan(legacy_plan)
    derived = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=ledger,
        legacy_source_request=legacy_source_request,
        candidate_contents=legacy_source_request["candidate_contents"],
        candidate_scope=scope,
        source_inventory=inventory,
        review_intent=intent,
        critique_disposition=disposition,
        request_builder=_route_builder,
        estimate_input_tokens=_input_estimator,
        estimate_output_tokens=_output_estimator,
        caps=_caps(),
    )
    assert derived["identity"]["legacy_request_hash"] == legacy_plan["provenance_hashes"]["request"]
    assert derived["identity"]["legacy_intent_hash"] == legacy_plan["provenance_hashes"]["intent"]


def test_source_scope_and_literal_view_tampering_fail_closed(tmp_path):
    inventory, scope, contents, intent, disposition, _legacy_plan, ledger, _plan = _build_ready(
        tmp_path, candidate_count=2, slot_count=2,
    )
    hashes = dict(_legacy_plan["candidate_hashes"])
    group_map = build_arbitration_review_group_map(scope, intent, ledger["critique_issues"])
    view_set = compile_arbitration_critical_views(
        candidate_ids=list(contents),
        candidate_hashes=hashes,
        candidate_contents=contents,
        legacy_ledger=ledger,
        candidate_scope=scope,
        source_inventory=inventory,
        review_group_map=group_map,
    )
    validate_arbitration_view_set(view_set, expected_view_set_hash=view_set["view_set_hash"])

    tampered_view = copy.deepcopy(view_set)
    tampered_view["critical_views"][0]["literal_critical_detail"]["source_claims"][0]["text"] += " altered"
    with pytest.raises(ArbitrationViewError, match="hash"):
        validate_arbitration_view_set(
            tampered_view, expected_view_set_hash=view_set["view_set_hash"],
        )

    tampered_candidate = copy.deepcopy(contents)
    first = tampered_candidate["candidate_1"]["sections"][0]["claim_support"][0]
    first["source_claim_ids"] = ["foreign-source-claim"]
    with pytest.raises(ArbitrationAggregationError, match="prepared legacy source candidate contents"):
        build_overflow_assessment_plan(
            legacy_plan=_legacy_plan,
            legacy_ledger=ledger,
            legacy_source_request=_prepared_legacy_source_request(
                list(contents),
                _legacy_plan["candidate_hashes"],
                contents,
                disposition,
                intent,
            ),
            candidate_contents=tampered_candidate,
            candidate_scope=scope,
            source_inventory=inventory,
            review_intent=intent,
            critique_disposition=disposition,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(),
        )


def test_indivisible_unit_and_output_overflow_block_without_truncation(tmp_path):
    huge = _build_ready(
        tmp_path, candidate_count=2, slot_count=1,
        caps=_caps(input_cap=32_000), claim_length=18_000,
    )
    inventory, scope, contents, intent, disposition, legacy_plan, ledger, _prior = huge
    before = copy.deepcopy(contents)
    with pytest.raises(ArbitrationSerializationError, match="indivisible assessment unit"):
        build_overflow_assessment_plan(
            legacy_plan=legacy_plan,
            legacy_ledger=ledger,
            legacy_source_request=_prepared_legacy_source_request(
                list(contents),
                legacy_plan["candidate_hashes"],
                contents,
                disposition,
                intent,
            ),
            candidate_contents=contents,
            candidate_scope=scope,
            source_inventory=inventory,
            review_intent=intent,
            critique_disposition=disposition,
            request_builder=_route_builder,
            estimate_input_tokens=_input_estimator,
            estimate_output_tokens=_output_estimator,
            caps=_caps(input_cap=2_000),
        )
    assert contents == before

    with pytest.raises(ArbitrationSerializationError, match="output skeleton"):
        _build_ready(
            tmp_path / "second",
            candidate_count=2,
            slot_count=1,
            caps=_caps(output_cap=1),
        )

    with pytest.raises(ArbitrationSerializationError, match="total context"):
        _build_ready(
            tmp_path / "third",
            candidate_count=2,
            slot_count=1,
            caps=replace(_caps(), verified_context_limit=1),
        )


def test_deterministic_ids_exclude_volatile_remaining_budget_snapshots(tmp_path):
    first = _build_ready(tmp_path / "one", candidate_count=2, slot_count=2, caps=_caps())[-1]
    second = _build_ready(tmp_path / "two", candidate_count=2, slot_count=2, caps=_caps())[-1]
    assert first["plan_input_hash"] == second["plan_input_hash"]
    assert [row["node_id"] for row in first["assessment_leaves"]] == [
        row["node_id"] for row in second["assessment_leaves"]
    ]
    assert "remaining_budget" not in json.dumps(first["identity"])

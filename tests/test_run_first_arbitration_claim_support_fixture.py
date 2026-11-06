from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from outline.candidate_arbitration_hierarchy import (
    ArbitrationHierarchyError,
    _ordered_claim_support_groups,
    build_arbitration_final_ledger,
    validate_arbitration_leaf,
)
from outline.candidate_arbitration_ledger_packing import (
    _derive_claim_support_comparison_groups,
    expand_arbitration_final_ledger,
    pack_arbitration_final_ledger,
)
from tests.test_current_runtime_full_e2e import _outline_provider_response
from tests.test_run_first_arbitration_claim_support_parts import _plan
from outline.v3_models import compute_v3_hash
from tests.test_run_first_critique_packing import _critique_executor


def _provider_leaf_request(leaf):
    request = deepcopy(leaf["request"]["request"])
    sections = request.pop("candidate_sections_by_id")
    headers = request.pop("candidate_headers")
    request["candidate_contents"] = {
        candidate: {**headers[candidate], "sections": sections[candidate]}
        for candidate in request["candidate_ids"]
    }
    return request


def test_local_fixture_echoes_complete_part_bindings_and_final_certificate_round_trips():
    plan, _contents = _plan()
    responses = {}
    for leaf in plan["leaves"]:
        request = _provider_leaf_request(leaf)
        original = deepcopy(request)
        response = _outline_provider_response(
            f"arbitration:leaf:{leaf['leaf_id']}", request,
        )["content"]
        validated = validate_arbitration_leaf(plan, leaf["leaf_id"], response)
        assert validated["claim_support_part_receipts_by_candidate"]
        assert request == original
        responses[leaf["leaf_id"]] = response

    ledger = build_arbitration_final_ledger(plan, responses)
    packed = pack_arbitration_final_ledger(ledger)
    assert expand_arbitration_final_ledger(packed) == ledger
    result = _outline_provider_response("arbitration", {
        "candidate_ids": plan["candidate_ids"],
        "candidate_hashes": plan["candidate_hashes"],
        "hierarchy": {"level": "all_candidate_final_arbitration"},
        "arbitration_evaluation_ledger": packed,
        "section_coordination_contract": {},
    })["content"]
    assert result["selected_candidate_id"] in plan["candidate_ids"]
    assert ledger["claim_support_partition"]["typed_issue_coverage"]


def test_runtime_revalidates_raw_part_echoes_before_final_ledger_assembly():
    plan, contents = _plan()
    calls = []
    executor, _profile = _critique_executor(calls)
    leaf_by_node = {f"arbitration:leaf:{leaf['leaf_id']}": leaf for leaf in plan["leaves"]}

    def provider(node_id, request, **_kwargs):
        calls.append((node_id, request))
        if node_id in leaf_by_node:
            assert request == leaf_by_node[node_id]["request"]
            request = _provider_leaf_request(leaf_by_node[node_id])
        return _outline_provider_response(node_id, request)["content"]

    executor._provider_call = provider
    executor._persist_semantic_provider_output = lambda *_args, **_kwargs: SimpleNamespace(
        artifact_id="local-test-plan-or-ledger", content_hash="a" * 64,
    )
    result = executor._run_arbitration_hierarchy({
        "plan": plan, "node_id": "arbitration",
        "source_request": {
            "candidate_ids": plan["candidate_ids"],
            "candidate_hashes": plan["candidate_hashes"],
            "candidate_contents": contents,
            "section_coordination_contract": {},
        },
    }, {"candidate_generations": "source-generation-hash"})
    assert result["selected_candidate_id"] in plan["candidate_ids"]
    assert len(calls) == len(plan["leaves"]) + 1
    assert calls[-1][1]["arbitration_evaluation_ledger"]["ledger_packing"]
    assert calls[-1][1]["arbitration_final_projection"]


def test_fixture_echo_cannot_make_a_foreign_part_hash_pass_runtime_validation():
    plan, _contents = _plan()
    leaf = plan["leaves"][0]
    request = _provider_leaf_request(leaf)
    request["claim_support_part_bindings"]["parts"][0]["part_hash"] = "0" * 64
    response = _outline_provider_response(
        f"arbitration:leaf:{leaf['leaf_id']}", request,
    )["content"]

    with pytest.raises(ArbitrationHierarchyError, match="part"):
        validate_arbitration_leaf(plan, leaf["leaf_id"], response)


@pytest.mark.parametrize("group_rows", [_ordered_claim_support_groups, _derive_claim_support_comparison_groups])
def test_explicit_comparison_sides_close_within_their_source_bound_task(group_rows):
    rows = [
        {
            "claim_slot_id": f"slot-{side}", "claim_occurrence_index": 0,
            "support_row_index": index, "task_id": "relation_comparison:AB",
            "primary_claim_id": f"source-primary-{side}", "comparison_role": side,
            "comparison_scope_id": compute_v3_hash({"task_id": "relation_comparison:AB", "primary_claim_id": f"source-primary-{side}"}),
        }
        for index, side in enumerate(("A", "B"))
    ]
    assert len(group_rows(deepcopy(rows))) == 1
    separate_tasks = deepcopy(rows)
    separate_tasks[1]["task_id"] = "relation_comparison:CD"
    separate_tasks[1]["comparison_scope_id"] = compute_v3_hash({
        "task_id": separate_tasks[1]["task_id"],
        "primary_claim_id": separate_tasks[1]["primary_claim_id"],
    })
    with pytest.raises(ValueError, match="task.*incomplete"):
        group_rows(separate_tasks)

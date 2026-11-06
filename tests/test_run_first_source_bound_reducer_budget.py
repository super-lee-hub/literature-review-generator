"""Stage-local reducer allowance follows a materialized bounded request tree."""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from pathlib import Path
from typing import Any

import pytest

from outline.v3_executor import OutlineV3ExecutionError
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_run_first_semantic_reducer_closure import (
    _bounded_noise_topics,
    _bridge_claims,
    _context_fields,
    _source_comparison,
    _topic_ids,
)


_CROSS_STAGE = "cross_group_comparison_provider"
_GLOBAL_STAGE = "global_synthesis_provider"
_INPUT_LIMIT = 32_000
_OUTPUT_LIMIT = 4_096


def _materialized_reducer_plan(
    stage: str,
    *,
    item_count: int,
    item_tokens_upper_bound: int = 9_500,
    input_tokens_upper_bound: int = _INPUT_LIMIT,
    wrapper_tokens_upper_bound: int = 1_200,
    reducer_output_tokens_upper_bound: int = _OUTPUT_LIMIT,
) -> list[dict[str, Any]]:
    """Make finite plan rows from source/output caps for this component seam."""

    count = item_count
    item_tokens = item_tokens_upper_bound
    level = 1
    rows: list[dict[str, Any]] = []
    while wrapper_tokens_upper_bound + count * item_tokens > input_tokens_upper_bound:
        available = input_tokens_upper_bound - wrapper_tokens_upper_bound
        fan_in = max(1, available // item_tokens)
        next_count = ceil(count / fan_in)
        for index in range(next_count):
            group_size = min(fan_in, count - index * fan_in)
            rows.append(
                {
                    "node_id": f"{stage}:reduce:{level}:{index + 1}",
                    "estimated_input_tokens": (
                        wrapper_tokens_upper_bound + group_size * item_tokens
                    ),
                    "estimated_output_tokens": reducer_output_tokens_upper_bound,
                    "input_estimate_kind": "bounded_reducer_upper_bound",
                    "request_payload_status": "output_dependent",
                }
            )
        if next_count >= count and reducer_output_tokens_upper_bound >= item_tokens:
            raise AssertionError("test plan did not contract under its declared input cap")
        count = next_count
        item_tokens = reducer_output_tokens_upper_bound
        level += 1
    return rows


def _large_cross_request(count: int = 80) -> tuple[dict[str, Any], set[str], set[str], dict[str, str]]:
    comparison, claim_ids, evidence_ids, field_values = _source_comparison()
    topics = _bounded_noise_topics(count)
    topics[0] = comparison
    # Each noise fragment is near the finite per-fragment input projection,
    # while remaining indivisible and below the unchanged 32k request cap.
    for topic in topics[1:]:
        topic["bounded_context"] = (
            "The reported outcome remains within its documented sample window. " * 430
        )
    return (
        {
            "task": "substantive_cross_group_comparison",
            "node_id": "cross_group_comparison",
            "semantic_contract_version": "semantic-evidence-graph-v2",
            "shared_synthesis_contract_version": "v1",
            "topic_synthesis": topics,
            "relation_candidates": [],
            "questions": [
                "Compare the B/C findings while preserving their exact source boundaries."
            ],
            "output_contract": {
                "comparisons": "array of evidence-bound comparisons",
                "bridge_claims": "source-closed claims",
                "topic_dispositions": "one exact row per topic",
                "coverage_ledger": "runtime persists exact membership",
                "unresolved_questions": "array",
            },
        },
        claim_ids,
        evidence_ids,
        field_values,
    )


def _source_closed_provider(
    expected_claim_ids: set[str],
    expected_evidence_ids: set[str],
    expected_field_values: Mapping[str, str],
    calls: list[tuple[str, dict[str, Any]]],
):
    def provider(node_id: str, raw_request: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(raw_request)
        calls.append((node_id, request))
        items = request.get("topic_synthesis") or ()
        requested_topics = sorted(_topic_ids(items))
        fields = _context_fields(items)
        fields_by_id = {
            str(field.get("source_field_id") or ""): field
            for field in fields
            if str(field.get("source_field_id") or "")
        }
        prior_bridges = _bridge_claims(items)
        bridge_claims: list[dict[str, Any]] = []
        if "topic:b-c" in set(requested_topics):
            field_ids = sorted(set(expected_field_values).intersection(fields_by_id))
            if set(field_ids) == set(expected_field_values):
                source_ids = sorted(expected_claim_ids)
                source_evidence = sorted(expected_evidence_ids)
                source_field_ids = field_ids
            elif prior_bridges:
                prior = next(
                    (
                        claim
                        for claim in prior_bridges
                        if "topic:b-c" in claim.get("topic_ids", ())
                    ),
                    prior_bridges[0],
                )
                source_ids = [str(prior["claim_id"])]
                source_evidence = sorted(expected_evidence_ids)
                source_field_ids = sorted(expected_field_values)
            else:
                raise AssertionError("B/C source closure was lost before the final fold")
            exact_values = [
                str(fields_by_id[field_id]["source_value"])
                for field_id in sorted(set(expected_field_values).intersection(fields_by_id))
            ]
            bridge_claims.append(
                {
                    "claim_id": f"synthesis:cross_group_comparison:{len(calls):03d}",
                    "topic_ids": ["topic:b-c"],
                    "fragment_id": "fragment:b-c",
                    "paper_keys": ["paper:b", "paper:c"],
                    "text": "The B/C comparison retains these exact boundaries: "
                    + "; ".join(exact_values or expected_field_values.values()),
                    "source_claim_ids": source_ids,
                    "evidence_ids": source_evidence,
                    "source_field_ids": source_field_ids,
                }
            )
        bridge_ids = {str(claim["claim_id"]) for claim in bridge_claims}
        dispositions = [
            (
                {
                    "topic_id": topic_id,
                    "status": "integrated",
                    "synthesis_claim_ids": sorted(bridge_ids),
                }
                if topic_id == "topic:b-c" and bridge_claims
                else {
                    "topic_id": topic_id,
                    "status": "unresolved",
                    "reason": "No source-complete comparison claim was returned for this topic.",
                }
            )
            for topic_id in requested_topics
        ]
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": bridge_claims,
                "topic_dispositions": dispositions,
                "unresolved_questions": [],
            },
        }

    return provider


def test_materialized_tree_allows_more_than_24_reducers_with_coverage_and_qualifiers(
    tmp_path: Path,
) -> None:
    request, source_claim_ids, evidence_ids, field_values = _large_cross_request()
    calls: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(
        tmp_path,
        provider=_source_closed_provider(
            source_claim_ids, evidence_ids, field_values, calls
        ),
        stability_mode="off",
        max_provider_calls=100,
        max_source_prompt_tokens=_INPUT_LIMIT,
    )
    plan_rows = _materialized_reducer_plan(
        _CROSS_STAGE,
        item_count=len(request["topic_synthesis"]),
    )
    assert len(plan_rows) > 24
    executor.semantic_request_plan = plan_rows

    result = executor._run_bounded_semantic_provider_call(
        _CROSS_STAGE, request, {"topic_synthesis": "source-input-hash"}
    )

    reducer_calls = [node_id for node_id, _ in calls if ":reduce:" in node_id]
    assert len(reducer_calls) > 24
    assert len(reducer_calls) <= len(plan_rows)
    reducer_levels = {
        int(request["hierarchy"]["reduction_level"])
        for node_id, request in calls
        if ":reduce:" in node_id
    }
    assert max(reducer_levels) <= 4
    profile = executor._node_route("candidate_1_provider_generation").profile
    assert all(
        profile.estimate_tokens(request) <= _INPUT_LIMIT
        for node_id, request in calls
        if ":reduce:" in node_id
    )
    assert all(
        int(row.get("requested_output_tokens") or 0) <= profile.max_output_tokens
        for row in executor._request_payload_audit
        if ":reduce:" in str(row.get("node_id") or "")
    )

    final_call = next(
        request for node_id, request in reversed(calls) if ":reduce:" not in node_id
    )
    assert final_call["hierarchy"]["level"] == "bounded_semantic_final_fold"
    assert final_call["questions"] == request["questions"]
    assert 0 < len(final_call["topic_synthesis"]) < len(request["topic_synthesis"])
    assert {row["topic_id"] for row in result["topic_dispositions"]} == _topic_ids(
        request["topic_synthesis"]
    )
    assert any(
        row["topic_id"] == "topic:b-c" and row["status"] == "integrated"
        for row in result["topic_dispositions"]
    )
    final_items = final_call["topic_synthesis"]
    topic_members: dict[str, set[str]] = {}
    source_field_ids: set[str] = set()
    for item in final_items:
        manifest = executor._load_semantic_reducer_coverage_ref(item)
        assert manifest is not None
        for topic_id, members in manifest["topic_members_by_id"].items():
            topic_members.setdefault(topic_id, set()).update(members)
        source_field_ids.update(manifest["source_field_ids"])
    assert set(topic_members) == _topic_ids(request["topic_synthesis"])
    assert set(field_values).issubset(source_field_ids)
    bridge = next(
        claim
        for item in final_items
        if "topic:b-c" in _topic_ids(item)
        for claim in _bridge_claims(item.get("semantic_result"))
        if "topic:b-c" in claim.get("topic_ids", ())
    )
    assert set(bridge["source_claim_ids"]) == source_claim_ids
    assert set(bridge["evidence_ids"]) == evidence_ids
    assert set(bridge["source_field_ids"]) == set(field_values)
    assert all(value in bridge["text"] for value in field_values.values())


def test_unrelated_stage_plan_cannot_raise_cross_group_reducer_allowance(
    tmp_path: Path,
) -> None:
    request, source_claim_ids, evidence_ids, field_values = _large_cross_request()
    calls: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(
        tmp_path,
        provider=_source_closed_provider(
            source_claim_ids, evidence_ids, field_values, calls
        ),
        stability_mode="off",
        max_provider_calls=100,
        max_source_prompt_tokens=_INPUT_LIMIT,
    )
    executor.semantic_request_plan = _materialized_reducer_plan(
        _GLOBAL_STAGE,
        item_count=len(request["topic_synthesis"]),
    )

    with pytest.raises(
        OutlineV3ExecutionError,
        match="cross_group_comparison reducer cannot contract within 24 calls",
    ):
        executor._run_bounded_semantic_provider_call(
            _CROSS_STAGE, request, {"topic_synthesis": "source-input-hash"}
        )

    assert calls == []


def test_materialized_reducer_allowance_does_not_override_global_call_budget(
    tmp_path: Path,
) -> None:
    request, source_claim_ids, evidence_ids, field_values = _large_cross_request(4)
    calls: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(
        tmp_path,
        provider=_source_closed_provider(
            source_claim_ids, evidence_ids, field_values, calls
        ),
        stability_mode="off",
        max_provider_calls=1,
        max_source_prompt_tokens=_INPUT_LIMIT,
    )
    executor.semantic_request_plan = _materialized_reducer_plan(
        _CROSS_STAGE, item_count=4
    )

    with pytest.raises(
        OutlineV3ExecutionError,
        match="reducer level exceeds the remaining provider call budget",
    ):
        executor._run_bounded_semantic_provider_call(
            _CROSS_STAGE, request, {"topic_synthesis": "source-input-hash"}
        )

    assert calls == []


def test_planned_allowance_does_not_bypass_singleton_noncontraction_guard(
    tmp_path: Path,
) -> None:
    source_items: list[dict[str, Any]] = []
    for index in range(2):
        topic_id = f"topic-{index}"
        paper_key = f"paper-{index}"
        field_id = f"field-{index}"
        claim = {
            "claim_id": f"synthesis:cross_group_comparison:prior-{index}",
            "topic_ids": [topic_id],
            "paper_key": paper_key,
            "text": f"The source-bound finding for {paper_key} remains qualified.",
            "evidence_ids": [f"evidence:{index}"],
            "source_field_ids": [field_id],
        }
        source_items.append(
            {
                "topic_ids": [topic_id],
                "topic_members_by_id": {topic_id: [paper_key]},
                "fragment_ids": [f"fragment-{index}"],
                "result_ids": [f"result-{index}"],
                "paper_ids": [paper_key],
                "semantic_result": {
                    "comparisons": [],
                    "bridge_claims": [claim],
                    "unresolved_questions": [],
                },
                "topic_disposition_groups": [
                    {
                        "status": "integrated",
                        "topic_ids": [topic_id],
                        "synthesis_claim_ids_by_topic": {topic_id: [claim["claim_id"]]},
                    }
                ],
                "interpretation_context": {
                    "fields": [
                        {
                            "source_field_id": field_id,
                            "paper_key": paper_key,
                            "owner_study_id": "",
                            "scope": "paper",
                            "study_id": "",
                            "canonical_field": "context",
                            "source_path": "source condition",
                            "disposition": "context",
                            "interpretation_required": True,
                            "source_value": "source value retained in exact input closure. " * 200,
                            "source_summary_hash": f"summary-hash-{index}",
                        },
                        {
                            "source_field_id": f"unused-field-{index}",
                            "paper_key": paper_key,
                            "owner_study_id": "",
                            "scope": "paper",
                            "study_id": "",
                            "canonical_field": "context",
                            "source_path": "unused source context",
                            "disposition": "context",
                            "interpretation_required": False,
                            "source_value": "unused source context retained in input. " * 40,
                            "source_summary_hash": f"unused-summary-{index}",
                        },
                    ],
                    "dependencies": [],
                },
            }
        )

    calls: list[str] = []

    def unchanged_provider(node_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        calls.append(node_id)
        topic_ids = sorted(
            {
                str(topic_id)
                for item in request.get("topic_synthesis") or ()
                if isinstance(item, Mapping)
                for topic_id in item.get("topic_ids") or ()
                if str(topic_id)
            }
        )
        claims = {
            str(claim["claim_id"]): dict(claim)
            for item in request.get("topic_synthesis") or ()
            if isinstance(item, Mapping)
            for claim in (
                (item.get("semantic_result") or {}).get("bridge_claims") or ()
                if isinstance(item.get("semantic_result"), Mapping)
                else ()
            )
            if isinstance(claim, Mapping) and str(claim.get("claim_id") or "")
        }
        claims_by_topic: dict[str, list[str]] = {}
        for claim in claims.values():
            for topic_id in claim.get("topic_ids") or ():
                claims_by_topic.setdefault(str(topic_id), []).append(str(claim["claim_id"]))
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": [claims[key] for key in sorted(claims)],
                "topic_dispositions": [
                    {
                        "topic_id": topic_id,
                        "status": "integrated",
                        "synthesis_claim_ids": sorted(set(claims_by_topic.get(topic_id, []))),
                    }
                    for topic_id in topic_ids
                ],
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=unchanged_provider,
        stability_mode="off",
        max_provider_calls=100,
        max_source_prompt_tokens=6_000,
    )
    executor.semantic_request_plan = _materialized_reducer_plan(
        _CROSS_STAGE,
        item_count=2,
        item_tokens_upper_bound=4_000,
        input_tokens_upper_bound=6_000,
        wrapper_tokens_upper_bound=1_000,
        reducer_output_tokens_upper_bound=1_500,
    )
    # The context carried by each unchanged singleton remains too large for a
    # final fold, so the bounded tree includes the second level as well.
    executor.semantic_request_plan.extend(
        {
            "node_id": f"{_CROSS_STAGE}:reduce:2:{index}",
            "estimated_input_tokens": 5_500,
            "estimated_output_tokens": 1_500,
            "input_estimate_kind": "bounded_reducer_upper_bound",
            "request_payload_status": "output_dependent",
        }
        for index in (1, 2)
    )
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": "v1",
        "topic_synthesis": source_items,
        "relation_candidates": [],
        "questions": ["Preserve the supported source claims and conditions."],
        "output_contract": {},
    }

    with pytest.raises(
        OutlineV3ExecutionError,
        match="singleton reducers repeated the exact",
    ):
        executor._run_bounded_semantic_provider_call(
            _CROSS_STAGE, request, {"topic_synthesis": "source-input"}
        )

    assert len(calls) == 4
    assert not any(":reduce:3:" in node_id for node_id in calls)

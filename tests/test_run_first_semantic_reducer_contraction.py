from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from outline.v3_executor import (
    INTERPRETATION_CONTRACT_VERSION,
    SHARED_SYNTHESIS_CONTRACT_VERSION,
    OutlineV3ExecutionError,
    _decode_semantic_dependency_templates_v1,
    _encode_semantic_dependency_templates_v1,
    _project_semantic_dependency_templates_request_v1,
    _source_derived_semantic_output_reserve_v1,
)
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderBudgetV1,
    ProviderRuntime,
)


def _dependency(
    primary: str, evidence: str, *, paper: str = "paper-a"
) -> dict[str, Any]:
    return {
        "primary_claim_id": primary,
        "primary_evidence_ids": [evidence],
        "paper_key": paper,
        "owner_study_id": "",
        "scope": "paper",
        "study_id": "",
        "reason": "boundary",
        "required_source_claim_ids": [f"{paper}:limitation", f"{paper}:condition"],
        "required_evidence_ids": [
            f"{paper}:limitation-evidence",
            f"{paper}:condition-evidence",
        ],
        "required_source_field_ids": [f"{paper}:condition-field"],
        "required_for_synthesis_output": True,
        "dependency_hash": f"hash:{paper}",
    }


def test_dependency_template_codec_roundtrips_rows_and_keeps_primary_memberships() -> (
    None
):
    rows = [_dependency(f"primary-{index}", f"evidence-{index}") for index in range(8)]

    encoded = _encode_semantic_dependency_templates_v1(rows)
    decoded = _decode_semantic_dependency_templates_v1(encoded)

    assert decoded == rows
    assert len(encoded["templates"]) == 1
    assert len(encoded["templates"][0]["members"]) == 8
    assert [member["ordinal"] for member in encoded["templates"][0]["members"]] == list(
        range(8)
    )
    assert len(json.dumps(encoded, separators=(",", ":"))) < len(
        json.dumps(rows, separators=(",", ":"))
    )


def test_dependency_template_codec_separates_foreign_ownership_and_closure() -> None:
    base = _dependency("primary-a", "evidence-a")
    rows = [
        base,
        _dependency("primary-b", "evidence-b"),
        {**_dependency("primary-foreign-paper", "evidence-c"), "paper_key": "paper-b"},
        {
            **_dependency("primary-foreign-owner", "evidence-d"),
            "owner_study_id": "study-b",
        },
        {
            **_dependency("primary-foreign-scope", "evidence-e"),
            "scope": "explicit_study",
        },
        {**_dependency("primary-foreign-study", "evidence-f"), "study_id": "study-b"},
        {
            **_dependency("primary-foreign-field", "evidence-g"),
            "required_source_field_ids": ["foreign-field"],
        },
        {
            **_dependency("primary-foreign-claim", "evidence-h"),
            "required_source_claim_ids": ["foreign-claim"],
        },
        {
            **_dependency("primary-foreign-evidence", "evidence-i"),
            "required_evidence_ids": ["foreign-evidence"],
        },
        {
            **_dependency("primary-different-reason", "evidence-j"),
            "reason": "different-boundary",
        },
        {
            **_dependency("primary-different-hash", "evidence-k"),
            "dependency_hash": "other-hash",
        },
    ]

    encoded = _encode_semantic_dependency_templates_v1(rows)

    assert _decode_semantic_dependency_templates_v1(encoded) == rows
    assert len(encoded["templates"]) == 10
    assert sorted(len(template["members"]) for template in encoded["templates"]) == [
        1
    ] * 9 + [2]


def test_dependency_template_codec_rejects_corrupt_hash_and_duplicate_order() -> None:
    encoded = _encode_semantic_dependency_templates_v1(
        [_dependency("primary-a", "evidence-a"), _dependency("primary-b", "evidence-b")]
    )
    corrupted_hash = json.loads(json.dumps(encoded))
    corrupted_hash["templates"][0]["template_hash"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        _decode_semantic_dependency_templates_v1(corrupted_hash)

    duplicate_order = json.loads(json.dumps(encoded))
    duplicate_order["templates"][0]["members"][1]["ordinal"] = 0
    with pytest.raises(ValueError, match="order"):
        _decode_semantic_dependency_templates_v1(duplicate_order)


def test_provider_request_projection_is_lossless_and_leaves_canonical_input_unchanged() -> (
    None
):
    dependencies = [
        _dependency(f"primary-{index}", f"evidence-{index}") for index in range(8)
    ]
    fields = [
        {
            "source_field_id": "field-condition",
            "paper_key": "paper-a",
            "owner_study_id": "",
            "scope": "paper",
            "study_id": "",
            "source_path": "source condition",
            "source_value": "exact source condition remains inline",
            "disposition": "exact",
        }
    ]
    canonical = {
        "task": "substantive_cross_group_comparison",
        "topic_synthesis": [
            {
                "topic_ids": ["topic-a"],
                "paper_ids": ["paper-a"],
                "interpretation_context": {
                    "fields": fields,
                    "dependencies": dependencies,
                },
            }
        ],
    }
    original = deepcopy(canonical)

    wire = _project_semantic_dependency_templates_request_v1(canonical)
    wire_context = wire["topic_synthesis"][0]["interpretation_context"]
    encoded = wire_context["dependency_templates"]

    assert canonical == original
    assert "dependencies" not in wire_context
    assert wire_context["fields"] == fields
    assert _decode_semantic_dependency_templates_v1(encoded) == dependencies
    assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":"))) < len(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    )


def test_bounded_semantic_call_compacts_only_wire_and_validates_expanded_closure(
    tmp_path: Path,
) -> None:
    from test_outline_v3_semantic_execution import _executor

    dependencies = [
        _dependency("primary-a", "primary-evidence-a"),
        _dependency("primary-b", "primary-evidence-b"),
    ]
    field_id = "paper-a:condition-field"
    source_field = {
        "source_field_id": field_id,
        "paper_key": "paper-a",
        "owner_study_id": "",
        "scope": "paper",
        "study_id": "",
        "source_path": "methods.sample",
        "source_value": "The result applies only to the recorded sample.",
        "disposition": "exact",
        "canonical_field": "sample boundary",
        "interpretation_required": True,
        "source_summary_hash": "a" * 64,
        "derived_value": "",
    }
    qualifier_claims = ["paper-a:limitation", "paper-a:condition"]
    qualifier_evidence = ["paper-a:limitation-evidence", "paper-a:condition-evidence"]
    primary_ids = ["primary-a", "primary-b"]
    primary_evidence = ["primary-evidence-a", "primary-evidence-b"]
    prior_claims = [
        {
            "claim_id": f"synthesis:topic_synthesis:prior-{index}",
            "topic_ids": ["topic:one"],
            "paper_key": "paper-a",
            "text": f"Source finding {index} remains bounded by its sample.",
            "source_claim_ids": [primary_id, *qualifier_claims],
            "evidence_ids": [primary_evidence_id, *qualifier_evidence],
            "source_field_ids": [field_id],
        }
        for index, (primary_id, primary_evidence_id) in enumerate(
            zip(primary_ids, primary_evidence, strict=True), start=1
        )
    ]
    item = {
        "topic_ids": ["topic:one"],
        "topic_members_by_id": {"topic:one": ["paper-a"]},
        "fragment_ids": ["fragment:one"],
        "result_ids": ["result:one"],
        "paper_ids": ["paper-a"],
        "semantic_result": {"bridge_claims": prior_claims},
        "interpretation_context": {
            "fields": [source_field],
            "dependencies": dependencies,
        },
    }
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
        "topic_synthesis": [item],
        "relation_candidates": [],
        "questions": ["Retain the supported finding and its exact qualifier."],
        "output_contract": {
            "comparisons": "array",
            "bridge_claims": "source-closed claims",
            "topic_dispositions": "one exact row per topic",
            "coverage_ledger": "runtime persists exact membership",
            "unresolved_questions": "array",
        },
    }
    original = deepcopy(request)
    captured: list[dict[str, Any]] = []
    omit_field = False
    output_claim_id = "synthesis:cross_group_comparison:fixture"

    def provider(_node_id: str, wire_request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal omit_field
        wire = deepcopy(dict(wire_request))
        captured.append(wire)
        context = wire["topic_synthesis"][0]["interpretation_context"]
        assert "dependencies" not in context
        assert context["fields"] == [source_field]
        assert context["dependency_template_contract"]
        assert _decode_semantic_dependency_templates_v1(
            context["dependency_templates"]
        ) == dependencies
        bridge_claim = {
            "claim_id": output_claim_id,
            "topic_ids": ["topic:one"],
            "paper_key": "paper-a",
            "text": "Two supported findings retain the same sample boundary.",
            "source_claim_ids": [*primary_ids, *qualifier_claims],
            "evidence_ids": [*primary_evidence, *qualifier_evidence],
            "source_field_ids": [] if omit_field else [field_id],
        }
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": [bridge_claim],
                "topic_dispositions": [{
                    "topic_id": "topic:one",
                    "status": "integrated",
                    "synthesis_claim_ids": [output_claim_id],
                }],
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path / "positive",
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    node_id = "cross_group_comparison_provider:reduce:1:1"
    result = executor._run_bounded_semantic_provider_call(
        node_id, request, {"topic_synthesis": "expanded-source-context"}
    )

    assert request == original
    assert len(captured) == 1
    encoded_context = captured[0]["topic_synthesis"][0]["interpretation_context"]
    encoded = encoded_context["dependency_templates"]
    assert len(encoded["templates"]) == 1
    assert len(encoded["templates"][0]["members"]) == 2
    assert _decode_semantic_dependency_templates_v1(encoded) == dependencies
    bridge = result["bridge_claims"][0]
    assert bridge["source_claim_ids"] == [*primary_ids, *qualifier_claims]
    assert bridge["evidence_ids"] == [*primary_evidence, *qualifier_evidence]
    assert bridge["source_field_ids"] == [field_id]

    audit = next(row for row in executor._request_payload_audit if row.get("node_id") == node_id)
    route_profile = executor._node_route("candidate_1_provider_generation").profile
    assert audit["estimated_input_tokens"] <= audit["effective_input_cap"] <= 8_000
    assert audit["requested_output_tokens"] <= audit["output_cap"]
    assert audit["output_cap"] == route_profile.max_output_tokens
    assert (
        audit["estimated_input_tokens"]
        + audit["requested_output_tokens"]
        + route_profile.reasoning_reserve
        + route_profile.safety_margin
        <= route_profile.verified_context_limit
    )

    persisted = executor._persist_semantic_provider_output(
        node_id, result, dependency_hashes={"topic_synthesis": "expanded-source-context"}
    )
    artifact = json.loads(Path(persisted.path).read_text(encoding="utf-8"))
    persisted_json = json.dumps(artifact, ensure_ascii=False)
    assert "dependency_templates" not in persisted_json
    assert all(value in persisted_json for value in [*primary_ids, *qualifier_claims, field_id])

    omit_field = True
    rejected_executor = _executor(
        tmp_path / "negative",
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=8_000,
    )
    with pytest.raises(OutlineV3ExecutionError, match="omits required qualifiers"):
        rejected_executor._run_bounded_semantic_provider_call(
            node_id, request, {"topic_synthesis": "expanded-source-context"}
        )
    assert "dependencies" in request["topic_synthesis"][0]["interpretation_context"]
    assert "dependency_templates" not in request["topic_synthesis"][0]["interpretation_context"]


def test_source_derived_final_fold_reserve_stays_within_route_and_context_caps() -> (
    None
):
    profile = ProviderContextProfile.conservative(
        provider="generic",
        model="rfs-outline-local",
        endpoint_type="chat_completions",
        model_context_limit=200_000,
        max_output_tokens=16_000,
        reasoning_reserve=4_096,
        safety_margin=2_048,
    )
    envelope = {
        "comparisons": [],
        "bridge_claims": [
            {
                "claim_id": f"bridge-{index}",
                "text": "source-closed supported bridge statement " * 30,
                "topic_ids": [f"topic-{index}"],
                "paper_key": f"paper-{index}",
                "source_claim_ids": [f"claim-{index}-a", f"claim-{index}-b"],
                "evidence_ids": [f"evidence-{index}-a", f"evidence-{index}-b"],
                "source_field_ids": [f"field-{index}"],
            }
            for index in range(11)
        ],
        "topic_dispositions": [
            {"topic_id": f"topic-{index}", "status": "integrated"}
            for index in range(19)
        ],
        "unresolved_questions": [],
    }

    reserve = _source_derived_semantic_output_reserve_v1(envelope, profile, 4_096)
    source_estimate = max(
        profile.estimate_tokens(envelope),
        (
            len(
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            + 3
        )
        // 4,
    )

    assert reserve == ((source_estimate + 1023) // 1024) * 1024
    assert reserve <= profile.max_output_tokens
    assert (
        26_220 + reserve + profile.reasoning_reserve + profile.safety_margin
        < profile.verified_context_limit
    )


def test_source_derived_final_fold_reserve_rejects_route_max_before_transport() -> None:
    profile = ProviderContextProfile.conservative(
        provider="generic",
        model="rfs-outline-local",
        endpoint_type="chat_completions",
        model_context_limit=20_000,
        max_output_tokens=4_096,
        reasoning_reserve=1_024,
        safety_margin=512,
    )
    envelope = {
        "comparisons": [],
        "bridge_claims": [
            {
                "claim_id": f"bridge-{index}",
                "topic_ids": [f"topic-{index}"],
                "paper_key": f"paper-{index}",
                "text": "supported",
                "source_claim_ids": [f"claim-{index}"],
                "evidence_ids": [f"evidence-{index}"],
                "source_field_ids": [f"field-{index}"],
            }
            for index in range(2_000)
        ],
        "topic_dispositions": [],
        "unresolved_questions": [],
    }

    with pytest.raises(OutlineV3ExecutionError, match="response schema floor requires"):
        _source_derived_semantic_output_reserve_v1(envelope, profile, 4_096)


def test_conservative_prose_estimate_clamps_to_existing_route_cap_when_floor_fits() -> (
    None
):
    profile = ProviderContextProfile.conservative(
        provider="generic",
        model="outline-test",
        endpoint_type="internal",
        model_context_limit=20_000,
        max_output_tokens=4_096,
        reasoning_reserve=1_024,
        safety_margin=512,
    )
    envelope = {
        "comparisons": [],
        "bridge_claims": [
            {
                "claim_id": "bridge-one",
                "topic_ids": ["topic-one"],
                "paper_key": "paper-one",
                "text": "A long, source-bound statement that can be condensed. " * 300,
                "source_claim_ids": ["claim-one"],
                "evidence_ids": ["evidence-one"],
                "source_field_ids": ["field-one"],
            }
        ],
        "topic_dispositions": [{"topic_id": "topic-one", "status": "integrated"}],
        "unresolved_questions": [],
    }

    assert profile.estimate_tokens(envelope) > profile.max_output_tokens
    assert _source_derived_semantic_output_reserve_v1(envelope, profile, 4_096) == 4_096


def test_source_derived_reserve_still_uses_shared_aggregate_output_budget() -> None:
    profile = ProviderContextProfile.conservative(
        provider="generic",
        model="rfs-outline-local",
        endpoint_type="chat_completions",
        model_context_limit=200_000,
        max_output_tokens=16_000,
        reasoning_reserve=4_096,
        safety_margin=2_048,
    )
    envelope = {
        "comparisons": [],
        "bridge_claims": [
            {"claim_id": f"bridge-{index}", "text": "supported claim text " * 90}
            for index in range(11)
        ],
        "topic_dispositions": [
            {"topic_id": f"topic-{index}", "status": "integrated"}
            for index in range(19)
        ],
        "unresolved_questions": [],
    }
    requested = _source_derived_semantic_output_reserve_v1(envelope, profile, 4_096)
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=1,
            max_output_tokens_total=4_096,
            max_retry_attempts_total=0,
            max_wall_seconds=60,
        )
    )
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1),
        aggregate_budget=controller,
        test_only=True,
    )

    assert requested == 8_192
    with pytest.raises(
        ProviderBudgetExceeded,
        match="aggregate provider output-token budget exhausted",
    ):
        runtime.admit(requested_output_tokens=requested)
    snapshot = controller.snapshot()
    assert snapshot["output_tokens_reserved"] == 0
    assert snapshot["calls_reserved"] == 0


def test_source_complete_multilevel_fixture_response_fits_4096_actual_output_cap() -> (
    None
):
    from test_run_first_semantic_reducer_closure import (
        _bounded_noise_topics,
        _source_comparison,
    )

    comparison, source_claim_ids, evidence_ids, field_values = _source_comparison()
    topics = _bounded_noise_topics()
    topics[0] = comparison
    claim = {
        "claim_id": "synthesis:cross_group_comparison:source-complete-final",
        "topic_ids": ["topic:b-c"],
        "fragment_id": "fragment:b-c",
        "paper_keys": ["paper:b", "paper:c"],
        "text": "The B/C comparison retains these exact reported boundaries: "
        + "; ".join(field_values.values()),
        "source_claim_ids": sorted(source_claim_ids),
        "evidence_ids": sorted(evidence_ids),
        "source_field_ids": sorted(field_values),
    }
    response = {
        "comparisons": [],
        "bridge_claims": [claim],
        "topic_dispositions": [
            (
                {
                    "topic_id": str(topic["topic_id"]),
                    "status": "integrated",
                    "synthesis_claim_ids": [claim["claim_id"]],
                }
                if str(topic["topic_id"]) == "topic:b-c"
                else {
                    "topic_id": str(topic["topic_id"]),
                    "status": "unresolved",
                    "reason": "No source-complete comparison claim was returned for this topic.",
                }
            )
            for topic in topics
        ],
        "unresolved_questions": [],
    }
    serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )

    assert set(claim["source_claim_ids"]) == source_claim_ids
    assert set(claim["evidence_ids"]) == evidence_ids
    assert set(claim["source_field_ids"]) == set(field_values)
    assert all(value in claim["text"] for value in field_values.values())
    assert len(response["topic_dispositions"]) == 64
    assert (len(serialized) + 3) // 4 <= 4_096
    conservative_profile = ProviderContextProfile.conservative(
        provider="configured",
        model="outline-v3",
        endpoint_type="internal",
        model_context_limit=128_000,
        max_output_tokens=4_096,
    )
    assert (
        conservative_profile.estimate_tokens(response)
        > conservative_profile.max_output_tokens
    )


def test_unchanged_singleton_reducers_stop_before_a_repeated_provider_call(
    tmp_path: Path,
) -> None:
    from test_outline_v3_semantic_execution import _executor

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
            "evidence_ids": [f"evidence-{index}"],
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
                            "source_value": "source value retained in exact input closure. "
                            * 200,
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
                            "source_value": "unused source context retained in input. "
                            * 40,
                            "source_summary_hash": f"unused-summary-{index}",
                        },
                    ],
                    "dependencies": [],
                },
            }
        )
    calls: list[str] = []

    def provider(node_id: str, request: dict[str, Any]) -> dict[str, Any]:
        calls.append(node_id)
        topic_ids = sorted(
            {
                str(topic_id)
                for item in request.get("topic_synthesis") or ()
                if isinstance(item, dict)
                for topic_id in item.get("topic_ids") or ()
                if str(topic_id)
            }
        )
        claims_by_id: dict[str, dict[str, Any]] = {}
        for item in request.get("topic_synthesis") or ():
            if not isinstance(item, dict):
                continue
            semantic = item.get("semantic_result")
            if isinstance(semantic, dict):
                for claim in semantic.get("bridge_claims") or ():
                    if isinstance(claim, dict):
                        claims_by_id[str(claim.get("claim_id") or "")] = dict(claim)
        claims = [claims_by_id[key] for key in sorted(claims_by_id) if key]
        claim_ids_by_topic: dict[str, list[str]] = {}
        for claim in claims:
            for topic_id in claim.get("topic_ids") or ():
                claim_ids_by_topic.setdefault(str(topic_id), []).append(
                    str(claim["claim_id"])
                )
        return {
            "status": "success",
            "content": {
                "comparisons": [],
                "bridge_claims": claims,
                "topic_dispositions": [
                    {
                        "topic_id": topic_id,
                        "status": "integrated",
                        "synthesis_claim_ids": sorted(
                            set(claim_ids_by_topic.get(topic_id, []))
                        ),
                    }
                    for topic_id in topic_ids
                ],
                "unresolved_questions": [],
            },
        }

    executor = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
        max_source_prompt_tokens=6_000,
    )
    request = {
        "task": "substantive_cross_group_comparison",
        "node_id": "cross_group_comparison",
        "semantic_contract_version": "semantic-evidence-graph-v2",
        "shared_synthesis_contract_version": SHARED_SYNTHESIS_CONTRACT_VERSION,
        "interpretation_contract_version": INTERPRETATION_CONTRACT_VERSION,
        "topic_synthesis": source_items,
        "relation_candidates": [],
        "questions": ["Preserve the supported source claims and conditions."],
        "output_contract": {
            "comparisons": "array",
            "bridge_claims": "source-closed claims",
            "topic_dispositions": "one exact row per topic",
            "coverage_ledger": "runtime persists exact membership",
            "unresolved_questions": "array",
        },
    }

    with pytest.raises(
        OutlineV3ExecutionError, match="singleton reducers repeated the exact"
    ):
        executor._run_bounded_semantic_provider_call(
            "cross_group_comparison_provider",
            request,
            {"topic_synthesis": "source-input"},
        )

    assert len(calls) == 4
    assert {node for node in calls if ":reduce:2:" in node} == {
        "cross_group_comparison_provider:reduce:2:1",
        "cross_group_comparison_provider:reduce:2:2",
    }
    assert not any(":reduce:3:" in node for node in calls)

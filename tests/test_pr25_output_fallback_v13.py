from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.semantic_chunking import TopicRoute, build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3ExecutionError
from outline.v3_models import TopicSynthesis
from tests.test_outline_v3_semantic_execution import _executor


def _topic_components(
    executor: Any,
) -> tuple[Any, Any, list[TopicSynthesis], dict[str, TopicRoute]]:
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    content_layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    topics: list[TopicSynthesis] = []
    routes: dict[str, TopicRoute] = {}
    for paper_id in ("paper-a", "paper-b"):
        topic_id = f"topic:{paper_id}"
        topic = TopicSynthesis(
            topic_id=topic_id,
            fragment_id=f"fragment:{paper_id}",
            paper_ids=[paper_id],
            supporting_evidence_ids=list(
                content_layers.dossier_by_paper[paper_id].evidence_ids
            ),
        )
        topics.append(topic)
        routes[topic_id] = TopicRoute(
            topic_id=topic_id,
            question=f"Assess the reported finding for {paper_id}.",
            paper_ids=[paper_id],
            dimensions=["finding"],
        )
    return evidence, content_layers, topics, routes


def _topic_request(executor: Any) -> dict[str, Any]:
    evidence, content_layers, topics, routes = _topic_components(executor)
    return executor._build_topic_provider_request(
        topics,
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=content_layers,
        batch_index=1,
    )


def _unresolved_response(
    request: Mapping[str, Any],
    reason: str = "Output limit.",
) -> dict[str, Any]:
    requested_topics = [
        item for item in request.get("topics") or () if isinstance(item, Mapping)
    ]
    fragment_ids = [
        str(item.get("fragment_id") or item.get("topic_id") or "")
        for item in requested_topics
    ]
    return {
        "topics": [
            {
                "topic_id": str(item.get("topic_id") or ""),
                "fragment_id": str(item.get("fragment_id") or item.get("topic_id") or ""),
                "status": "unresolved",
                "conclusions": [],
                "unresolved_questions": [reason],
                "supporting_evidence_ids": [],
            }
            for item in requested_topics
        ],
        "processed_fragment_ids": fragment_ids,
        "claims": [],
        "unresolved_questions": [],
    }


def test_v4_unresolved_fallback_covers_fragments_and_rejects_unsupported_facts(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)
    # Keep this case on the historical v4 contract. The reason-size field is
    # provider-visible and belongs to v5, so it is removed from this legacy case.
    request["output_contract"] = deepcopy(request["output_contract"])
    request["output_contract"]["semantic_result_contract_version"] = (
        "bounded-topic-synthesis/v4"
    )
    request["output_contract"].pop("max_unresolved_reason_utf8_bytes", None)
    request["output_contract"]["overflow_policy"] = (
        "If material conclusions and exceptions cannot fit, mark the affected "
        "fragment unresolved with an explicit reason; never truncate a supported "
        "claim or silently omit a requested fragment"
    )
    contract = request["output_contract"]
    assert contract["semantic_result_contract_version"] == "bounded-topic-synthesis/v4"
    assert "explicit reason" in contract["overflow_policy"]

    response = _unresolved_response(request)
    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1",
        request,
        response,
    )
    requested_fragment_ids = [
        str(item["fragment_id"]) for item in request["topics"]
    ]
    assert response["processed_fragment_ids"] == requested_fragment_ids
    assert [item["fragment_id"] for item in response["topics"]] == (
        requested_fragment_ids
    )
    assert response["claims"] == []
    assert all(item["conclusions"] == [] for item in response["topics"])
    assert all(
        item["unresolved_questions"] == ["Output limit."]
        for item in response["topics"]
    )

    missing_processed = deepcopy(response)
    missing_processed["processed_fragment_ids"].pop()
    with pytest.raises(OutlineV3ExecutionError, match="every requested topic fragment"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, missing_processed
        )

    extra_topic = deepcopy(response)
    extra_topic["topics"].append(deepcopy(extra_topic["topics"][0]))
    with pytest.raises(OutlineV3ExecutionError, match="topic output fragment coverage"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, extra_topic
        )

    unsupported_fact = deepcopy(response)
    unsupported_fact["topics"][0]["conclusions"] = [
        "The treatment improves outcomes."
    ]
    with pytest.raises(OutlineV3ExecutionError, match="factual without evidence"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, unsupported_fact
        )


def test_v5_fallback_reason_has_a_declared_and_enforced_utf8_byte_limit(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)
    output_contract = request["output_contract"]
    assert output_contract["semantic_result_contract_version"] == (
        "bounded-topic-synthesis/v5"
    )

    # v4 requires an explicit fallback reason but currently specifies no finite
    # bound. The numeric limit remains a product-contract choice; this test
    # accepts any positive limit that permits the minimal reason used above.
    reason_limit = output_contract.get("max_unresolved_reason_utf8_bytes")
    assert type(reason_limit) is int
    assert reason_limit >= len("Output limit.".encode("utf-8"))

    response = _unresolved_response(request)
    swapped_topics = deepcopy(response)
    swapped_topics["topics"][0]["topic_id"], swapped_topics["topics"][1]["topic_id"] = (
        swapped_topics["topics"][1]["topic_id"],
        swapped_topics["topics"][0]["topic_id"],
    )
    with pytest.raises(OutlineV3ExecutionError, match="fragment/topic pairing"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, swapped_topics
        )

    for value in ("partial", "UNRESOLVED"):
        unsupported_status = deepcopy(response)
        unsupported_status["topics"][0]["status"] = value
        unsupported_status["topics"][0]["conclusions"] = ["A partial finding."]
        with pytest.raises(OutlineV3ExecutionError, match="unsupported status"):
            executor._validate_semantic_provider_output(
                "topic_synthesis_provider:batch:1", request, unsupported_status
            )

    partial_fallback = deepcopy(response)
    partial_fallback["topics"][0]["conclusions"] = ["A partial finding."]
    with pytest.raises(OutlineV3ExecutionError, match="partial factual claims"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, partial_fallback
        )

    partial_claim = deepcopy(response)
    partial_claim["claims"] = [{
        "claim_id": "synthesis:topic_synthesis:partial",
        "fragment_id": partial_claim["topics"][0]["fragment_id"],
        "paper_key": "paper-a",
        "evidence_ids": ["evidence:partial"],
    }]
    with pytest.raises(OutlineV3ExecutionError, match="partial factual claims"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, partial_claim
        )

    too_long = _unresolved_response(
        request,
        reason="x" * (reason_limit + 1),
    )
    with pytest.raises(OutlineV3ExecutionError, match="max_unresolved_reason_utf8_bytes"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, too_long
        )


def test_topic_packer_rejects_unfit_mandatory_unresolved_envelope(
    tmp_path: Path,
) -> None:
    executor = _executor(
        tmp_path,
        stability_mode="off",
        max_provider_calls=1_000,
    )
    evidence, content_layers, topics, routes = _topic_components(executor)
    request = executor._build_topic_provider_request(
        topics,
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=content_layers,
        batch_index=1,
    )
    reason_limit = request["output_contract"]["max_unresolved_reason_utf8_bytes"]
    assert reason_limit >= len("Output limit.".encode("utf-8"))
    fallback = {
        "topics": [
            {
                "topic_id": str(item["topic_id"]),
                "fragment_id": str(item["fragment_id"]),
                "status": "unresolved",
                "unresolved_questions": ["Output limit."],
            }
            for item in request["topics"]
        ],
        "processed_fragment_ids": [str(item["fragment_id"]) for item in request["topics"]],
        "claims": [],
        "unresolved_questions": [],
    }
    compact_fallback_bytes = len(
        json.dumps(
            fallback,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    fallback_output_token_upper_bound = compact_fallback_bytes + 1

    # One complete, explicitly permitted overflow result fits this allowance.
    # This does not claim substantive synthesis fits the same allowance.
    executor.semantic_output_max_tokens = fallback_output_token_upper_bound
    _expanded, batches, request_plan = executor._plan_topic_provider_batches(
        topics,
        topic_routes=routes,
        evidence_model=evidence,
        content_layers_model=content_layers,
        profile=executor.profile,
    )
    assert len(batches) == len(request_plan) == 1
    assert request_plan[0]["fragment_ids"] == [
        item["fragment_id"] for item in request["topics"]
    ]

    # One output token cannot contain the required JSON object and both
    # requested fragment identities, even when no factual claims are emitted.
    executor.semantic_output_max_tokens = 1
    with pytest.raises(
        OutlineV3ExecutionError,
        match="minimum_complete_response_exceeds_output_cap",
    ):
        executor._plan_topic_provider_batches(
            topics,
            topic_routes=routes,
            evidence_model=evidence,
            content_layers_model=content_layers,
            profile=executor.profile,
        )

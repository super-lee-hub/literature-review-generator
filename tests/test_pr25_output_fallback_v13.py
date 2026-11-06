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


def test_rejected_topic_keeps_raw_response_reference_in_persisted_audit(
    tmp_path: Path,
) -> None:
    import hashlib
    from dataclasses import replace

    from runtime.outline_v3_dag import OutlineNodeRecord
    from runtime.provider_runtime import hash_json

    raw_path = tmp_path / "response.bin"
    raw_bytes = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
    raw_path.write_bytes(raw_bytes)
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    rejected_content: dict[str, Any] = {}

    def provider(_node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        rejected_content.update(_unresolved_response(request))
        rejected_content["claims"] = [{
            "claim_id": "synthesis:topic_synthesis:unsupported",
            "fragment_id": request["topics"][0]["fragment_id"],
            "text": "An unsupported factual conclusion.",
            "claim_type": "finding",
            "paper_key": "paper-a",
            "evidence_ids": [],
        }]
        return {
            "status": "success",
            "finish_reason": "stop",
            "content": rejected_content,
            "raw_response_path": str(raw_path),
            "raw_response_sha256": raw_hash,
            "response_bytes": len(raw_bytes),
        }

    executor = _executor(tmp_path / "job", provider=provider, stability_mode="off")
    executor._dag = replace(
        executor._dag,
        nodes=[
            *executor._dag.nodes,
            OutlineNodeRecord(node_id="topic_synthesis_provider:batch:1"),
        ],
    )
    request = _topic_request(executor)
    with pytest.raises(OutlineV3ExecutionError, match="partial factual claims"):
        executor._run_semantic_provider_call(
            "topic_synthesis_provider:batch:1", request, {}
        )

    executor._persist_audit_evidence()
    audit_path = Path(executor.artifact_paths["request_payload_audit"])
    audit_rows = [json.loads(row) for row in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(audit_rows) == 1
    assert audit_rows[0]["status"] == "success"
    assert audit_rows[0]["semantic_validation_status"] == "rejected"
    assert "partial factual claims" in audit_rows[0]["semantic_validation_error"]
    assert audit_rows[0]["raw_response_refs"] == [{
        "path": str(raw_path),
        "sha256": raw_hash,
        "bytes": len(raw_bytes),
        "normalized_response_hash": hash_json(rejected_content),
    }]
    assert raw_path.read_bytes() == raw_bytes


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

    # The v5 limit bounds the explicit unresolved fallback reason.
    reason_limit = output_contract.get("max_unresolved_reason_utf8_bytes")
    assert type(reason_limit) is int
    assert reason_limit == 128

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

    at_limit = _unresolved_response(request, reason="x" * 128)
    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1", request, at_limit
    )

    too_long = _unresolved_response(
        request,
        reason="x" * 129,
    )
    with pytest.raises(OutlineV3ExecutionError, match="max_unresolved_reason_utf8_bytes"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, too_long
        )

    multibyte_at_limit = _unresolved_response(
        request,
        reason="汉" * 42 + "ab",
    )
    assert len(
        multibyte_at_limit["topics"][0]["unresolved_questions"][0].encode("utf-8")
    ) == 128
    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1", request, multibyte_at_limit
    )

    multibyte_over_limit = _unresolved_response(
        request,
        reason="汉" * 43,
    )
    assert len(
        multibyte_over_limit["topics"][0]["unresolved_questions"][0].encode("utf-8")
    ) == 129
    with pytest.raises(OutlineV3ExecutionError, match="max_unresolved_reason_utf8_bytes"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, multibyte_over_limit
        )


@pytest.mark.parametrize("status", ["integrated", "completed", "processed"])
def test_v5_non_fallback_topics_preserve_long_unresolved_questions(
    tmp_path: Path,
    status: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)
    questions = ["a" * 142, "b" * 119, "c" * 183]
    response = _unresolved_response(request)
    response["topics"][0]["status"] = status
    response["topics"][0]["unresolved_questions"] = questions
    response["unresolved_questions"] = questions

    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1", request, response
    )

    assert [len(value.encode("utf-8")) for value in response["topics"][0]["unresolved_questions"]] == [
        142, 119, 183
    ]
    assert response["topics"][0]["unresolved_questions"] == questions
    assert response["unresolved_questions"] == questions


@pytest.mark.parametrize(
    "status", ["integrated", "completed", "processed", "unresolved"]
)
def test_v5_topic_unresolved_questions_keep_array_string_and_nonempty_validation(
    tmp_path: Path,
    status: str,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)

    invalid_type = _unresolved_response(request)
    invalid_type["topics"][0]["status"] = status
    invalid_type["topics"][0]["unresolved_questions"] = ["open item", 7]
    with pytest.raises(OutlineV3ExecutionError, match="entries must be strings"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, invalid_type
        )

    invalid_array = _unresolved_response(request)
    invalid_array["topics"][0]["status"] = status
    invalid_array["topics"][0]["unresolved_questions"] = "open item"
    with pytest.raises(OutlineV3ExecutionError, match="must be an array"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, invalid_array
        )

    blank_item = _unresolved_response(request)
    blank_item["topics"][0]["status"] = status
    blank_item["topics"][0]["unresolved_questions"] = [""]
    with pytest.raises(OutlineV3ExecutionError, match="entries must be non-empty"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, blank_item
        )

    if status == "unresolved":
        empty_array = _unresolved_response(request)
        empty_array["topics"][0]["unresolved_questions"] = []
        with pytest.raises(OutlineV3ExecutionError, match="no explicit reason"):
            executor._validate_semantic_provider_output(
                "topic_synthesis_provider:batch:1", request, empty_array
            )


@pytest.mark.parametrize("questions", [[" "], [7], [{"question": "Open item"}]])
def test_v5_root_unresolved_questions_require_nonempty_strings(
    tmp_path: Path,
    questions: list[Any],
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)
    response = _unresolved_response(request)
    response["unresolved_questions"] = questions
    with pytest.raises(OutlineV3ExecutionError, match="non-empty strings"):
        executor._validate_semantic_provider_output(
            "topic_synthesis_provider:batch:1", request, response
        )


def test_topic_fragment_projection_preserves_batch_questions_without_reassigning_scope(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    request = _topic_request(executor)
    response = _unresolved_response(request)
    questions = ["a" * 142, "An unresolved question shared by the request batch."]
    response["unresolved_questions"] = questions
    executor._validate_semantic_provider_output(
        "topic_synthesis_provider:batch:1", request, response
    )
    fragments = [topic["fragment_id"] for topic in request["topics"]]
    for fragment_id in fragments:
        projected = executor._topic_output_for_fragment(response, fragment_id)
        assert projected["batch_unresolved_questions"] == questions
        assert projected["unresolved_questions"] == []
        assert projected["topic"]["unresolved_questions"] == ["Output limit."]

    legacy = deepcopy(response)
    legacy["unresolved_questions"] = [
        {"fragment_id": fragment_id, "question": "A fragment-specific legacy question."}
        for fragment_id in fragments
    ]
    for fragment_id in fragments:
        projected = executor._topic_output_for_fragment(legacy, fragment_id)
        assert projected["batch_unresolved_questions"] == []
        assert projected["unresolved_questions"] == [
            item for item in legacy["unresolved_questions"]
            if item["fragment_id"] == fragment_id
        ]


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

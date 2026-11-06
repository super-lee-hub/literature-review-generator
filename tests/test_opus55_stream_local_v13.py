from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai_interface
from config_loader import load_config
from outline.provider_router import OutlineRoleRoute
from outline.semantic_chunking import TopicRoute, build_paper_content_layers
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_executor import OutlineV3Executor
from outline.v3_models import TopicSynthesis
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderRuntime,
    ProviderRuntimeLedger,
)
from tests.test_outline_v3_semantic_execution import _executor
from tests.test_provider_local_http_integration import _LocalProvider


_MODEL = "claude-opus-5-5"
_SYSTEM_PROMPT = "SYSTEM_PROMPT_SENTINEL: preserve this instruction"
_USER_PROMPT = "TOPIC_PROMPT_SENTINEL: assess the reported finding"
_MAX_TOKENS = 1536


def test_stream_usage_knob_survives_typed_config_loading(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    lines = source.read_text(encoding="utf-8").splitlines()
    section = lines.index("[Outline_API]")
    lines[section + 1:section + 1] = [
        "provider_stream = true",
        "provider_stream_include_usage = true",
    ]
    path = tmp_path / "stream-config.ini"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config(
        str(path),
        action="generate_outline",
        requested_stages=["outline"],
        allow_template_credentials=True,
    )
    assert config["Outline_API"]["provider_stream"] == "true"
    assert config["Outline_API"]["provider_stream_include_usage"] == "true"


def _opus55_config(base_url: str) -> dict[str, str]:
    return {
        "api_key": "local-test-secret",
        "model": _MODEL,
        "api_base": base_url,
        "provider_family": "claude_chat_reasoning",
        "endpoint_type": "chat_completions",
        "proxy_mode": "direct",
        "provider_stream": "true",
        "provider_stream_include_usage": "true",
        "reasoning_effort": "xhigh",
        "transport_retries": "0",
        "connect_timeout_seconds": "2",
        "read_timeout_seconds": "3",
        "total_timeout_seconds": "4",
    }


def _strict_zero_retry_runtime(
    tmp_path,
) -> tuple[ProviderRuntime, ProviderBudgetController, ProviderRuntimeLedger]:
    aggregate = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=1,
            max_output_tokens_total=4096,
            max_retry_attempts_total=0,
            max_wall_seconds=30,
        )
    )
    ledger = ProviderRuntimeLedger(tmp_path / "opus55_provider_receipts.jsonl")
    runtime = ProviderRuntime(
        aggregate_budget=aggregate,
        ledger=ledger,
        job_id="opus55-local-http-job",
        attempt_id="attempt-1",
        stage_name="outline_v3",
        route="local-opus55-chat-completions",
        node_id="topic_synthesis_provider:batch:1",
        call_id="opus55-local-call",
        endpoint_type="chat_completions",
    )
    return runtime, aggregate, ledger


def test_opus55_stream_sends_chat_payload_and_accepts_spaced_sse_terminal_event(
    tmp_path,
) -> None:
    sse = (
        b'event: message\r\n'
        b'data: {"choices":[{"delta":{"content":"{\\"topic\\":\\""}}]}\r\n\r\n'
        b'data: {"choices":[{"delta":{"content":"Opus 5.5\\"}"}}]}\r\n\r\n'
        b'data: {"choices": [ {"delta": {}, "finish_reason": "stop"} ]}\r\n\r\n'
        b'data: {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}\r\n\r\n'
        b'data: [DONE]\r\n\r\n'
    )
    with _LocalProvider(
        [("raw", sse)],
        response_headers={"Content-Type": "text/event-stream"},
    ) as provider:
        runtime, aggregate, ledger = _strict_zero_retry_runtime(tmp_path)
        result = ai_interface._call_ai_api_detailed(
            _USER_PROMPT,
            _opus55_config(provider.base_url),
            _SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            response_format="json",
            retry_attempts=0,
            provider_runtime=runtime,
            provider_route="candidate_provider_generation",
        )

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request["path"] == "/v1/chat/completions"
    payload = request["payload"]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["model"] == _MODEL
    assert payload["max_tokens"] == _MAX_TOKENS
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["messages"] == [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_PROMPT},
    ]
    assert result["status"] == "success", result
    assert result["content"] == {"topic": "Opus 5.5"}
    assert result["response_protocol"] == "sse"
    assert result["response_complete"] is True
    assert result["attempts"] == 1
    assert result["usage_status"] == "reported"
    receipt = ledger.list_receipts()[0]
    assert receipt.status == "success"
    assert receipt.usage_status == "reported"
    assert receipt.input_tokens == 100
    assert receipt.output_tokens == 20
    snapshot = aggregate.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["retry_attempts_used"] == 0


def test_opus55_stream_without_terminal_event_fails_once_under_strict_v2(
    tmp_path,
) -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"{\\"topic\\":\\"partial\\"}"}}]}\n\n'
    )
    with _LocalProvider(
        [("raw", sse)],
        response_headers={"Content-Type": "text/event-stream"},
    ) as provider:
        runtime, aggregate, ledger = _strict_zero_retry_runtime(tmp_path)
        result = ai_interface._call_ai_api_detailed(
            _USER_PROMPT,
            _opus55_config(provider.base_url),
            _SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            response_format="json",
            retry_attempts=0,
            provider_runtime=runtime,
            provider_route="candidate_provider_generation",
        )

    assert len(provider.requests) == 1
    assert provider.requests[0]["payload"]["stream"] is True
    assert result["status"] == "failed"
    assert result["error_kind"] == "invalid_response"
    assert "terminal completion event" in result["message"]
    assert result["attempts"] == 1
    receipt = ledger.list_receipts()[0]
    assert receipt.status == "failed"
    assert receipt.attempts == 1
    snapshot = aggregate.snapshot()
    assert snapshot["calls_used"] == 1
    assert snapshot["retry_attempts_used"] == 0


def test_opus55_stream_complete_message_keeps_later_usage_chunk(tmp_path) -> None:
    sse = (
        b'data: {"choices":[{"message":{"content":"{\\"topic\\":\\"complete\\"}"},"finish_reason":"stop"}],"usage":{}}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}\n\n'
        b'data: [DONE]\n\n'
    )
    with _LocalProvider(
        [("raw", sse)],
        response_headers={"Content-Type": "text/event-stream"},
    ) as provider:
        runtime, _aggregate, ledger = _strict_zero_retry_runtime(tmp_path)
        result = ai_interface._call_ai_api_detailed(
            _USER_PROMPT,
            _opus55_config(provider.base_url),
            _SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            response_format="json",
            retry_attempts=0,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["content"] == {"topic": "complete"}
    assert result["usage_status"] == "reported"
    receipt = ledger.list_receipts()[0]
    assert receipt.input_tokens == 9
    assert receipt.output_tokens == 4


def test_topic_request_identity_stays_semantic_when_stream_route_fingerprint_changes(
    tmp_path,
    monkeypatch,
) -> None:
    executor = _executor(tmp_path, stability_mode="off")
    evidence = build_outline_evidence_views(executor.summaries, executor.job_id)
    content_layers = build_paper_content_layers(
        executor.summaries,
        evidence,
        job_id=executor.job_id,
    )
    topic = TopicSynthesis(
        topic_id="topic:paper-a",
        fragment_id="fragment:paper-a",
        paper_ids=["paper-a"],
        supporting_evidence_ids=list(
            content_layers.dossier_by_paper["paper-a"].evidence_ids
        ),
    )
    topic_route = TopicRoute(
        topic_id=topic.topic_id,
        question="Assess the reported finding for paper-a.",
        paper_ids=list(topic.paper_ids),
        dimensions=["finding"],
    )
    profile = ProviderContextProfile.conservative(
        provider="claude_chat_reasoning",
        model=_MODEL,
        endpoint_type="chat_completions",
        model_context_limit=200_000,
        max_output_tokens=16_384,
        reasoning_reserve=4_096,
    )
    buffered_route = OutlineRoleRoute(
        role="candidate_provider_generation",
        config_section="Outline_API",
        provider_name="claude_chat_reasoning",
        model=_MODEL,
        endpoint_type="chat_completions",
        profile=profile,
        api_base="http://127.0.0.1:12345/v1",
        config_identity={"transport_retries": "0"},
    )
    streamed_route = replace(
        buffered_route,
        config_identity={
            **dict(buffered_route.config_identity),
            "provider_stream": "true",
            "provider_stream_include_usage": "true",
        },
    )
    assert buffered_route.safe_config_fingerprint() != streamed_route.safe_config_fingerprint()

    monkeypatch.setattr(executor, "_role_route", lambda _node_id: buffered_route)
    _expanded, _batches, buffered_rows = executor._plan_topic_provider_batches(
        [topic],
        topic_routes={topic.topic_id: topic_route},
        evidence_model=evidence,
        content_layers_model=content_layers,
        profile=profile,
    )
    monkeypatch.setattr(executor, "_role_route", lambda _node_id: streamed_route)
    _expanded, _batches, streamed_rows = executor._plan_topic_provider_batches(
        [topic],
        topic_routes={topic.topic_id: topic_route},
        evidence_model=evidence,
        content_layers_model=content_layers,
        profile=profile,
    )

    assert buffered_rows[0]["request_hash"] == streamed_rows[0]["request_hash"]
    assert OutlineV3Executor._compute_topic_provider_plan_identity_hash(
        buffered_rows
    ) == OutlineV3Executor._compute_topic_provider_plan_identity_hash(streamed_rows)

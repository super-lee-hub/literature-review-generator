from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

import ai_interface
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderRuntime,
    ProviderRuntimeLedger,
)


class _LocalProvider:
    """Small real HTTP server used to exercise the production transport path."""

    def __init__(
        self,
        responses: list[tuple[int, Any] | tuple[str, Any]],
        *,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.responses = responses
        self.response_headers = response_headers or {}
        self.requests: list[dict[str, Any]] = []
        self._next_response = 0

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                payload = json.loads(raw_body.decode("utf-8"))
                owner.requests.append({"path": self.path, "payload": payload})
                response = owner.responses[min(owner._next_response, len(owner.responses) - 1)]
                owner._next_response += 1
                status_or_action, body = response
                if status_or_action == "close":
                    self.connection.close()
                    return
                if status_or_action == "raw":
                    status = 200
                    encoded = bytes(body)
                else:
                    status = int(status_or_action)
                    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in owner.response_headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "_LocalProvider":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _runtime(tmp_path, *, max_calls: int, max_output_tokens: int) -> tuple[ProviderRuntime, ProviderBudgetController, ProviderRuntimeLedger]:
    aggregate = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=max_calls,
            max_output_tokens_total=max_output_tokens,
        )
    )
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        aggregate_budget=aggregate,
        ledger=ledger,
        job_id="local-http-job",
        attempt_id="attempt-1",
        stage_name="analyze",
        route="local-http",
        node_id="local-paper",
        call_id="local-call",
        endpoint_type="chat_completions",
    )
    return runtime, aggregate, ledger


def _config(base_url: str) -> dict[str, str]:
    return {
        "api_key": "local-test-secret",
        "model": "local-test-model",
        "api_base": base_url,
        "provider_family": "generic",
        "endpoint_type": "chat_completions",
        "proxy_mode": "direct",
        "total_timeout_seconds": "3",
        "transport_retries": "1",
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "error_kind"),
    [(400, "fatal_config_or_auth"), (429, "retryable_http"), (503, "retryable_http")],
)
def test_real_local_http_statuses_are_recorded_once(tmp_path, status: int, error_kind: str) -> None:
    with _LocalProvider([(status, {"error": {"message": "local test response"}})]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        result = ai_interface._call_ai_api_detailed(
            "status test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert provider.requests[0]["path"] == "/v1/chat/completions"
    assert result["status"] == "failed"
    assert result["error_kind"] == error_kind
    assert result["attempts"] == 1
    receipt = ledger.list_receipts()[0]
    assert receipt.http_status == status
    assert receipt.attempts == 1
    assert aggregate.snapshot()["calls_used"] == 1


@pytest.mark.integration
def test_real_local_http_malformed_json_is_failed_and_usage_is_unreported(tmp_path) -> None:
    body = {"choices": [{"message": {"content": "not a JSON object"}}]}
    with _LocalProvider([(200, body)]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        result = ai_interface._call_ai_api_detailed(
            "malformed test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["error_kind"] == "invalid_response"
    receipt = ledger.list_receipts()[0]
    assert receipt.attempts == 1
    assert receipt.usage_status == "unreported"
    assert aggregate.snapshot()["calls_used"] == 1


@pytest.mark.integration
def test_real_local_http_sse_is_reassembled_and_honors_response_size_limit(tmp_path) -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"true}"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    with _LocalProvider([("raw", sse)]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        raw_dir = tmp_path / "raw-responses"
        config = {
            **_config(provider.base_url),
            "provider_stream": "true",
            "max_response_bytes": "4096",
            "raw_response_dir": str(raw_dir),
        }
        result = ai_interface._call_ai_api_detailed(
            "sse test",
            config,
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert provider.requests[0]["payload"]["stream"] is True
    assert result["status"] == "success"
    assert result["content"] == {"ok": True}
    assert result["response_protocol"] == "sse"
    assert result["response_complete"] is True
    assert result["raw_response_sha256"]
    assert result["response_bytes"] == len(sse)
    raw_path = raw_dir / f"response-{result['raw_response_sha256']}.bin"
    assert raw_path.read_bytes() == sse
    assert result["raw_response_path"] == str(raw_path.resolve())
    assert ledger.list_receipts()[0].metadata["response_protocol"] == "sse"
    assert aggregate.snapshot()["calls_used"] == 1


@pytest.mark.integration
def test_real_local_http_sse_without_terminal_event_is_not_success(tmp_path) -> None:
    sse = b'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}\n\n'
    with _LocalProvider([("raw", sse)]) as provider:
        runtime, _aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        result = ai_interface._call_ai_api_detailed(
            "partial sse test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["error_kind"] == "invalid_response"
    assert ledger.list_receipts()[0].status == "failed"


@pytest.mark.integration
def test_real_local_http_malformed_response_body_uses_the_configured_retry_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ai_interface.time, "sleep", lambda _seconds: None)

    with _LocalProvider([("raw", b"{not-json"), ("raw", b"{not-json")]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=2, max_output_tokens=16)
        result = ai_interface._call_ai_api_detailed(
            "malformed body retry test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=2,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 2
    assert result["status"] == "failed"
    assert result["error_kind"] == "invalid_response"
    assert result["attempts"] == 2
    receipt = ledger.list_receipts()[0]
    assert receipt.attempts == 2
    assert receipt.usage_status == "unreported"
    assert aggregate.snapshot()["calls_used"] == 2
    assert aggregate.snapshot()["retry_attempts_used"] == 1


@pytest.mark.integration
def test_real_local_http_success_without_usage_is_conservative_and_durable(tmp_path) -> None:
    body = {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "model": "local-test-model",
    }
    with _LocalProvider([(200, body)], response_headers={"X-Aihubmix-Request-Id": "req-local-1"}) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        result = ai_interface._call_ai_api_detailed(
            "missing usage test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["provider_request_id"] == "req-local-1"
    receipt = ledger.list_receipts()[0]
    assert receipt.usage_status == "unreported"
    assert receipt.output_tokens is None
    assert aggregate.snapshot()["output_tokens_used"] == 8
    assert receipt.metadata["provider_request_id"] == "req-local-1"


@pytest.mark.integration
def test_real_local_http_disconnect_is_failed_without_false_success(tmp_path) -> None:
    with _LocalProvider([("close", None)]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=1, max_output_tokens=8)
        result = ai_interface._call_ai_api_detailed(
            "disconnect test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=1,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["error_kind"] == "transient_network"
    receipt = ledger.list_receipts()[0]
    assert receipt.status == "failed"
    assert receipt.attempts == 1
    assert aggregate.snapshot()["calls_used"] == 1


@pytest.mark.integration
def test_real_local_http_retry_matches_request_count_receipt_and_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ai_interface.time, "sleep", lambda _seconds: None)
    first = (503, {"error": {"message": "temporary"}})
    second = (
        200,
        {
            "model": "local-test-model",
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        },
    )
    with _LocalProvider([first, second]) as provider:
        runtime, aggregate, ledger = _runtime(tmp_path, max_calls=2, max_output_tokens=16)
        result = ai_interface._call_ai_api_detailed(
            "retry test",
            _config(provider.base_url),
            "system",
            max_tokens=8,
            retry_attempts=2,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 2
    assert result["status"] == "success"
    receipt = ledger.list_receipts()[0]
    assert receipt.attempts == 2
    assert receipt.output_tokens == 3
    snapshot = aggregate.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["retry_attempts_used"] == 1
    assert snapshot["output_tokens_used"] == 3

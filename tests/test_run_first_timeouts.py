from __future__ import annotations

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Self

import pytest

import ai_interface
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderRuntime,
    ProviderRuntimeLedger,
)


class _LocalBodyServer:
    """A loopback HTTP server with explicit header/body timing controls."""

    def __init__(
        self,
        chunks: list[tuple[float, bytes]],
        *,
        status: int | list[int] = 200,
        content_type: str = "application/json",
    ) -> None:
        self.chunks = chunks
        self.statuses = status if isinstance(status, list) else [status]
        self.content_type = content_type
        self.requests: list[bytes] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                request_length = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(self.rfile.read(request_length))
                status = owner.statuses[min(len(owner.requests) - 1, len(owner.statuses) - 1)]
                self.send_response(status)
                self.send_header("Content-Type", owner.content_type)
                self.send_header(
                    "Content-Length",
                    str(sum(len(chunk) for _, chunk in owner.chunks)),
                )
                self.end_headers()
                try:
                    for delay_seconds, chunk in owner.chunks:
                        if delay_seconds:
                            time.sleep(delay_seconds)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    # A timeout test intentionally closes the client socket
                    # while this handler is waiting to send the delayed body.
                    return

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _valid_response_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-local-timeout",
            "model": "local-timeout-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"ok": true}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _config(base_url: str, raw_response_dir: Path) -> dict[str, str]:
    return {
        "api_key": "local-timeout-test-key",
        "model": "local-timeout-model",
        "api_base": base_url,
        "provider_family": "generic",
        "endpoint_type": "chat_completions",
        "proxy_mode": "direct",
        "connect_timeout_seconds": "1",
        "read_timeout_seconds": "1",
        "total_timeout_seconds": "5",
        "first_token_timeout_seconds": "1",
        "first_body_timeout_seconds": "1",
        "transport_retries": "3",
        "raw_response_dir": str(raw_response_dir),
    }


def _call(
    config: dict[str, str],
    *,
    retries: int = 3,
    retention_callback: Any = None,
    attempt_started_callback: Any = None,
) -> dict[str, Any]:
    return ai_interface._call_ai_api_detailed_uninstrumented(
        "local timeout test",
        config,
        "local system prompt",
        max_tokens=8,
        retry_attempts=retries,
        primary_raw_response_retention_callback=retention_callback,
        primary_provider_attempt_started_callback=attempt_started_callback,
    )


def _provider_runtime(tmp_path: Path) -> tuple[ProviderRuntime, ProviderRuntimeLedger]:
    aggregate = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=4,
            max_output_tokens_total=32,
            max_retry_attempts_total=3,
        )
    )
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        aggregate_budget=aggregate,
        ledger=ledger,
        job_id="first-body-timeout-job",
        attempt_id="attempt-1",
        stage_name="analyze",
        route="local-http",
        node_id="local-paper",
        call_id="first-body-timeout-call",
        endpoint_type="chat_completions",
    )
    return runtime, ledger


@pytest.mark.integration
def test_first_body_delay_within_limit_succeeds(tmp_path: Path) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(0.1, body)]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        result = _call(config)

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["response_complete"] is True
    assert result["attempts"] == 1
    assert result["first_token_timeout_enforced"] is False
    assert result["first_token_timeout_status"] == "unsupported_semantic_content_detection"
    assert result["first_body_timeout_enforced"] is True
    assert result["first_body_timeout_source"] == "first_body_timeout_seconds"


@pytest.mark.integration
def test_first_body_timeout_after_200_is_unknown_and_never_retried(tmp_path: Path) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(1.4, body)]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["first_token_timeout_seconds"] = "1"
        config["first_body_timeout_seconds"] = "1"
        config["read_timeout_seconds"] = "4"
        result = _call(config, retries=3)

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["http_status"] == 200
    assert result["error_kind"] == "outcome_unknown"
    assert result["timeout_phase"] == "first_body", result
    assert result["first_token_timeout_enforced"] is False
    assert result["first_body_timeout_enforced"] is True
    assert result["response_complete"] is False
    assert result["response_bytes"] == 0
    assert result["attempts"] == 1
    spool_path = Path(result["raw_response_spool_path"])
    assert spool_path.is_file()
    assert spool_path.read_bytes() == b""


@pytest.mark.integration
def test_later_chunk_inside_stream_idle_limit_succeeds(tmp_path: Path) -> None:
    body = _valid_response_body()
    padded_body = body + (b" " * (8192 - len(body) + 200))
    with _LocalBodyServer([(0.05, padded_body[:8192]), (0.2, padded_body[8192:])]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["first_token_timeout_seconds"] = "1"
        config["read_timeout_seconds"] = "1"
        result = _call(config)

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["response_complete"] is True
    assert result["first_token_timeout_enforced"] is False
    assert result["attempts"] == 1


@pytest.mark.integration
def test_slow_later_chunk_is_unknown_and_partial_spool_is_retained(tmp_path: Path) -> None:
    body = _valid_response_body()
    padded_body = body + (b" " * (8192 - len(body) + 200))
    with _LocalBodyServer([(0.05, padded_body[:8192]), (1.4, padded_body[8192:])]) as provider:
        raw_dir = tmp_path / "raw"
        config = _config(provider.base_url, raw_dir)
        config["first_token_timeout_seconds"] = "3"
        config["read_timeout_seconds"] = "1"
        result = _call(config, retries=3)

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["http_status"] == 200
    assert result["error_kind"] == "outcome_unknown"
    assert result["timeout_phase"] == "stream_idle"
    assert result["first_token_timeout_enforced"] is False
    assert result["response_complete"] is False
    assert result["attempts"] == 1
    spool_path = Path(result["raw_response_spool_path"])
    assert spool_path.is_file()
    partial = spool_path.read_bytes()
    assert partial
    assert partial == padded_body[: len(partial)]
    assert len(partial) == result["response_bytes"]


@pytest.mark.integration
def test_total_deadline_remains_distinct_from_stream_idle_timeout(tmp_path: Path) -> None:
    body = _valid_response_body()
    padded_body = body + (b" " * (8192 - len(body) + 200))
    with _LocalBodyServer([(0.05, padded_body[:8192]), (1.4, padded_body[8192:])]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["first_token_timeout_seconds"] = "3"
        config["read_timeout_seconds"] = "4"
        config["total_timeout_seconds"] = "1"
        result = _call(config, retries=3)

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["error_kind"] == "outcome_unknown"
    assert result["timeout_phase"] == "total_deadline"
    assert result["attempts"] == 1


@pytest.mark.integration
def test_sse_heartbeat_before_content_does_not_claim_first_token_enforcement(
    tmp_path: Path,
) -> None:
    content_event = json.dumps(
        {"choices": [{"delta": {"content": '{"ok": true}'}}]},
        separators=(",", ":"),
    ).encode("utf-8")
    finish_event = json.dumps(
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        separators=(",", ":"),
    ).encode("utf-8")
    sse_events = (
        b": keepalive\n\n"
        + b"data: "
        + content_event
        + b"\n\n"
        + b"data: "
        + finish_event
        + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    # The body starts promptly with an SSE heartbeat, while the first semantic
    # content delta arrives after first_token_timeout_seconds. The transport
    # explicitly reports semantic token timing as unsupported.
    with _LocalBodyServer(
        [(0.05, b": keepalive\n\n"), (1.4, sse_events[len(b": keepalive\n\n") :])],
        content_type="text/event-stream",
    ) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["first_token_timeout_seconds"] = "1"
        config["first_body_timeout_seconds"] = "2"
        config["read_timeout_seconds"] = "3"
        config["total_timeout_seconds"] = "5"
        result = _call(config)

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["response_complete"] is True
    assert result["first_token_timeout_enforced"] is False
    assert result["first_token_timeout_status"] == "unsupported_semantic_content_detection"
    assert result["first_body_timeout_enforced"] is True


@pytest.mark.integration
def test_timeout_semantics_propagate_to_provider_receipt_without_credentials(tmp_path: Path) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(0, body)]) as provider:
        runtime, ledger = _provider_runtime(tmp_path)
        result = ai_interface._call_ai_api_detailed(
            "local timeout metadata test",
            _config(provider.base_url, tmp_path / "raw"),
            "local system prompt",
            max_tokens=8,
            retry_attempts=0,
            provider_runtime=runtime,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "success"
    assert result["first_token_timeout_enforced"] is False
    assert result["first_token_timeout_status"] == "unsupported_semantic_content_detection"
    assert result["first_body_timeout_enforced"] is True
    receipt = ledger.list_receipts()[0]
    assert receipt.metadata["transport_config"]["semantic_first_content_timeout_enforced"] is False
    assert receipt.metadata["transport_config"]["semantic_first_content_timeout_status"] == (
        "unsupported_buffered_response_parser"
    )
    assert receipt.metadata["transport_config"]["first_body_timeout_enforced"] is True
    assert receipt.metadata["transport_config"]["first_body_timeout_source"] == (
        "first_body_timeout_seconds"
    )
    assert receipt.metadata["semantic_first_content_timeout_enforced"] is False
    assert "local-timeout-test-key" not in json.dumps(receipt.to_dict())


@pytest.mark.integration
def test_zero_transport_retries_still_sends_one_initial_request(tmp_path: Path) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(0, body)], status=503) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["transport_retries"] = "0"
        result = _call(config, retries=0)

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["error_kind"] == "retryable_http"
    assert result["http_status"] == 503
    assert result["attempts"] == 1


@pytest.mark.integration
def test_http_error_body_first_chunk_obeys_first_body_deadline(
    tmp_path: Path,
) -> None:
    error_body = json.dumps({
        "error": {"message": "delayed local error body"},
        "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
    }).encode("utf-8")
    retained: list[tuple[Any, ...]] = []
    with _LocalBodyServer([(1.5, error_body)], status=500) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["first_body_timeout_seconds"] = "1"
        config["read_timeout_seconds"] = "3"
        config["total_timeout_seconds"] = "4"
        result = _call(
            config,
            retries=0,
            retention_callback=lambda *args: retained.append(args),
        )

    assert len(provider.requests) == 1, "a partial HTTP error body must not cause another POST"
    assert result["http_status"] == 500
    assert result["timeout_phase"] == "first_body"
    assert result["response_body_complete"] is False
    assert result.get("usage_status", "unknown") == "unknown"
    assert not {"input_tokens", "output_tokens", "total_tokens"}.intersection(result)
    assert len(retained) == 1
    assert retained[0][3] == 500
    assert retained[0][6] is False


@pytest.mark.integration
def test_primary_retention_requires_a_writable_spool_root_before_post(
    tmp_path: Path,
) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(0, body)]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config.pop("raw_response_dir")
        result = _call(config, retention_callback=lambda *_args: None)

    assert provider.requests == []
    assert result["status"] == "failed"
    assert result["error_kind"] == "fatal_config_or_auth"


@pytest.mark.integration
def test_primary_retention_callback_receives_fsynced_hash_bound_spool_before_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _valid_response_body()
    callback_events: list[tuple[Any, ...]] = []
    original_decoder = ai_interface._decode_sse_response

    def retain(*args: Any) -> None:
        callback_events.append(args)
        path, sha256, byte_count, status, content_type, protocol, complete, attempt = args
        spool = Path(path)
        assert spool.is_file()
        assert spool.read_bytes() == body
        assert hashlib.sha256(spool.read_bytes()).hexdigest() == sha256
        assert byte_count == len(body)
        assert status == 200
        assert content_type == "application/json"
        assert protocol == "json"
        assert complete is True
        assert attempt == 1

    def assert_retained_before_decode(*args: Any, **kwargs: Any) -> Any:
        assert len(callback_events) == 1
        return original_decoder(*args, **kwargs)

    monkeypatch.setattr(ai_interface, "_decode_sse_response", assert_retained_before_decode)
    with _LocalBodyServer([(0, body)]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        result = _call(config, retention_callback=retain)

    assert len(provider.requests) == 1
    assert len(callback_events) == 1
    assert result["status"] == "success"
    assert result["primary_raw_response_retention_callback_called"] is True
    assert result["response_body_complete"] is True


@pytest.mark.integration
def test_primary_retention_records_complete_http_error_as_metadata_only(
    tmp_path: Path,
) -> None:
    body = json.dumps({"error": {"message": "local service unavailable"}}).encode("utf-8")
    callback_events: list[tuple[Any, ...]] = []
    durable_copy = tmp_path / "callback-captured-503.spool"

    def retain_error(*args: Any) -> None:
        callback_events.append(args)
        source = Path(str(args[0]))
        assert source.is_file()
        durable_copy.write_bytes(source.read_bytes())

    with _LocalBodyServer([(0, body)], status=503) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        result = _call(
            config,
            retries=0,
            retention_callback=retain_error,
        )

    assert len(provider.requests) == 1
    assert len(callback_events) == 1
    path, sha256, byte_count, status, content_type, protocol, complete, attempt = callback_events[0]
    assert Path(path) == Path(path).resolve()
    assert durable_copy.is_file()
    assert durable_copy.read_bytes() == body
    assert sha256 == hashlib.sha256(body).hexdigest()
    assert byte_count == len(body)
    assert status == 503
    assert content_type == "application/json"
    assert protocol == "json"
    assert complete is True
    assert attempt == 1
    assert result["status"] == "failed"
    assert result["http_status"] == 503
    assert result["primary_raw_response_retention_callback_called"] is True
    assert result["response_body_complete"] is True


@pytest.mark.integration
def test_primary_callbacks_bind_each_physical_attempt_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _valid_response_body()
    started_attempts: list[int] = []
    retained_attempts: list[int] = []
    monkeypatch.setattr(ai_interface.time, "sleep", lambda _seconds: None)
    with _LocalBodyServer([(0, body)], status=[503, 200]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        result = _call(
            config,
            retries=2,
            retention_callback=lambda *args: retained_attempts.append(args[7]),
            attempt_started_callback=started_attempts.append,
        )

    assert len(provider.requests) == 2
    assert started_attempts == [1, 2]
    assert retained_attempts == [1, 2]
    assert result["status"] == "success"
    assert result["attempts"] == 2


@pytest.mark.integration
def test_attempt_start_callback_failure_prevents_the_next_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _valid_response_body()
    started_attempts: list[int] = []
    retained_attempts: list[int] = []
    monkeypatch.setattr(ai_interface.time, "sleep", lambda _seconds: None)
    with _LocalBodyServer([(0, body)], status=[503, 200]) as provider:
        config = _config(provider.base_url, tmp_path / "raw")

        def fail_second_start(attempt: int) -> None:
            started_attempts.append(attempt)
            if attempt == 2:
                raise OSError("durable start callback detail")

        result = _call(
            config,
            retries=2,
            retention_callback=lambda *args: retained_attempts.append(args[7]),
            attempt_started_callback=fail_second_start,
        )

    assert len(provider.requests) == 1
    assert started_attempts == [1, 2]
    assert retained_attempts == [1]
    assert result["status"] == "failed"
    assert result["error_kind"] == "outcome_unknown"
    assert result["primary_provider_attempt_start_callback_failed"] is True
    assert result["provider_attempt_number"] == 2
    assert result["provider_http_attempt_started"] is False
    assert result["attempts"] == 1
    assert "durable start callback detail" not in json.dumps(result)


@pytest.mark.integration
def test_primary_retention_failure_after_200_is_unknown_and_never_retried(
    tmp_path: Path,
) -> None:
    body = _valid_response_body()
    with _LocalBodyServer([(0, body)]) as provider:
        runtime, ledger = _provider_runtime(tmp_path)
        config = _config(provider.base_url, tmp_path / "raw")

        def fail_retention(*_args: Any) -> None:
            raise OSError("secret callback detail must not escape")

        result = ai_interface._call_ai_api_detailed(
            "local retention failure test",
            config,
            "local system prompt",
            max_tokens=8,
            retry_attempts=3,
            provider_runtime=runtime,
            primary_raw_response_retention_callback=fail_retention,
        )

    assert len(provider.requests) == 1
    assert result["status"] == "failed"
    assert result["http_status"] == 200
    assert result["error_kind"] == "outcome_unknown"
    assert result["primary_raw_response_retention_callback_failed"] is True
    assert result["response_body_complete"] is True
    assert result["attempts"] == 1
    assert "secret callback detail" not in json.dumps(result)
    spool_path = Path(result["raw_response_spool_path"])
    assert spool_path.is_file()
    assert spool_path.read_bytes() == body
    assert ledger.list_receipts()


@pytest.mark.integration
def test_primary_retention_callback_marks_partial_200_incomplete_and_keeps_spool(
    tmp_path: Path,
) -> None:
    body = _valid_response_body()
    padded_body = body + (b" " * (8192 - len(body) + 200))
    first_chunk = padded_body[:8192]
    callback_events: list[tuple[Any, ...]] = []
    with _LocalBodyServer(
            [(0.05, first_chunk), (1.4, padded_body[8192:])]
    ) as provider:
        config = _config(provider.base_url, tmp_path / "raw")
        config["read_timeout_seconds"] = "1"
        result = _call(
            config,
            retries=3,
            retention_callback=lambda *args: callback_events.append(args),
        )

    assert len(provider.requests) == 1
    assert len(callback_events) == 1
    path, sha256, byte_count, status, content_type, protocol, complete, attempt = callback_events[0]
    partial_spool = Path(path)
    assert partial_spool.is_file()
    assert status == 200
    assert content_type == "application/json"
    assert protocol == "json"
    assert complete is False
    assert attempt == 1
    assert byte_count == len(first_chunk)
    assert sha256 == hashlib.sha256(first_chunk).hexdigest()
    assert partial_spool.read_bytes() == first_chunk
    assert result["error_kind"] == "outcome_unknown"
    assert result["response_body_complete"] is False
    assert result["attempts"] == 1


def test_reparse_provider_response_spool_is_local_and_verifies_hash_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _valid_response_body()
    path = tmp_path / "retained-response.spool"
    path.write_bytes(body)

    def no_http(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("offline spool reparse must not make an HTTP request")

    monkeypatch.setattr(ai_interface, "_post_with_proxy_mode", no_http)
    parsed = ai_interface.reparse_provider_response_spool(
        path,
        hashlib.sha256(body).hexdigest(),
        len(body),
        "application/json",
        "json",
        "chat_completions",
        "local-timeout-model",
        "json",
        200,
        True,
    )
    assert parsed["status"] == "success"
    assert parsed["content"] == {"ok": True}
    assert parsed["raw_response_sha256"] == hashlib.sha256(body).hexdigest()
    assert parsed["response_bytes"] == len(body)
    assert parsed["response_body_complete"] is True
    assert parsed["total_tokens"] == 3
    assert parsed["usage_status"] == "reported"

    with pytest.raises(ValueError, match="byte count"):
        ai_interface.reparse_provider_response_spool(
            path,
            hashlib.sha256(body).hexdigest(),
            len(body) + 1,
            "application/json",
            "json",
            "chat_completions",
            "local-timeout-model",
            "json",
            200,
            True,
        )


def test_reparse_provider_sse_spool_uses_terminal_parser(tmp_path: Path) -> None:
    events = [
        {"choices": [{"delta": {"content": '{"ok":'}}]},
        {"choices": [{"delta": {"content": " true}"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
    ]
    body = b"".join(
        b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for event in events
    ) + b"data: [DONE]\n\n"
    path = tmp_path / "retained-stream.spool"
    path.write_bytes(body)

    parsed = ai_interface.reparse_provider_response_spool(
        path,
        hashlib.sha256(body).hexdigest(),
        len(body),
        "text/event-stream",
        "sse",
        "chat_completions",
        "local-timeout-model",
        "json",
        200,
        True,
    )

    assert parsed["status"] == "success"
    assert parsed["content"] == {"ok": True}
    assert parsed["response_protocol"] == "sse"
    assert parsed["response_complete"] is True
    assert parsed["usage_status"] == "reported"
    with pytest.raises(ValueError, match="incomplete"):
        ai_interface.reparse_provider_response_spool(
            path,
            hashlib.sha256(body).hexdigest(),
            len(body),
            "application/json",
            "json",
            "chat_completions",
            "local-timeout-model",
            "json",
            200,
            False,
        )
    with pytest.raises(ValueError, match="HTTP 200"):
        ai_interface.reparse_provider_response_spool(
            path,
            hashlib.sha256(body).hexdigest(),
            len(body),
            "application/json",
            "json",
            "chat_completions",
            "local-timeout-model",
            "json",
            503,
            True,
        )

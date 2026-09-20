from __future__ import annotations

import json
import time
from pathlib import Path

import ai_interface


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = raw_body or json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = headers or {"content-type": "application/json"}

    def json(self) -> object:
        return self._payload


def _api_config() -> dict[str, object]:
    return {
        "api_base": "https://aihubmix.com/v1",
        "api_key": "sk-test-recovery",
        "model": "claude-opus-5",
        "proxy_mode": "environment",
        "aihubmix_recovery_enabled": "true",
        "operation_id": "operation-recovery-1",
        "attempt_id": "attempt-recovery-1",
    }


def test_aihubmix_recovery_requires_one_recent_matching_task(monkeypatch) -> None:
    now = time.time()
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url.endswith("/ai/v1/tasks/task_recovery_1"):
            return _Response(
                200,
                {
                    "id": "task_recovery_1",
                    "model": "claude-opus-5",
                    "created_at": now,
                    "output": [{"type": "response", "truncated": False}],
                },
            )
        return _Response(
            200,
            {
                "choices": [
                    {
                        "message": {"content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(ai_interface.requests, "get", fake_get)
    config = _api_config()
    config["aihubmix_recovery_proof"] = {
        "task_id": "task_recovery_1",
        "request_fingerprint": "test-fingerprint",
        "operator": "test-operator",
        "operation_id": "operation-recovery-1",
        "attempt_id": "attempt-recovery-1",
    }
    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=config,
        model="claude-opus-5",
        request_started_epoch=now - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
        request_fingerprint="test-fingerprint",
    )

    assert result is not None
    assert result["status"] == "success"
    assert result["content"] == {"ok": True}
    assert result["recovered_from_aihubmix_task"] is True
    assert result["aihubmix_recovery_task_id"] == "task_recovery_1"
    assert calls == [
        "https://aihubmix.com/ai/v1/tasks/task_recovery_1",
        "https://aihubmix.com/ai/v1/tasks/task_recovery_1/content",
    ]


def test_aihubmix_recovery_refuses_unique_task_without_identity_proof(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_interface.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unbound recovery must not query tasks")),
    )

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=_api_config(),
        model="claude-opus-5",
        request_started_epoch=time.time() - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
    )

    assert result is None


def test_aihubmix_recovery_refuses_proof_bound_to_another_operation(monkeypatch) -> None:
    config = _api_config()
    config["aihubmix_recovery_proof"] = {
        "task_id": "task_recovery_1",
        "request_fingerprint": "test-fingerprint",
        "operator": "test-operator",
        "operation_id": "operation-other",
        "attempt_id": "attempt-recovery-1",
    }
    monkeypatch.setattr(
        ai_interface.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mismatched proof must not query tasks")),
    )

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=config,
        model="claude-opus-5",
        request_started_epoch=time.time() - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
        request_fingerprint="test-fingerprint",
    )

    assert result is None


def test_aihubmix_recovery_polls_delayed_task(monkeypatch) -> None:
    now = time.time()
    detail_calls = 0

    def fake_get(url: str, **kwargs):
        nonlocal detail_calls
        if url.endswith("/ai/v1/tasks/task_recovery_1"):
            detail_calls += 1
            output = [] if detail_calls == 1 else [{"type": "response", "truncated": False}]
            return _Response(
                200,
                {
                    "id": "task_recovery_1",
                    "model": "claude-opus-5",
                    "created_at": now,
                    "output": output,
                },
            )
        return _Response(
            200,
            {
                "choices": [
                    {
                        "message": {"content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(ai_interface.requests, "get", fake_get)
    monkeypatch.setattr(ai_interface.time, "sleep", lambda _seconds: None)
    config = _api_config()
    config["recovery_max_polls"] = 2
    config["recovery_poll_interval_seconds"] = 1
    config["aihubmix_recovery_proof"] = {
        "task_id": "task_recovery_1",
        "request_fingerprint": "test-fingerprint",
        "operator": "test-operator",
        "operation_id": "operation-recovery-1",
        "attempt_id": "attempt-recovery-1",
    }

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=config,
        model="claude-opus-5",
        request_started_epoch=now - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
        request_fingerprint="test-fingerprint",
    )

    assert result is not None
    assert result["content"] == {"ok": True}
    assert detail_calls == 2


def test_aihubmix_recovery_decodes_sse_content_and_persists_transport_facts(
    monkeypatch,
    tmp_path,
) -> None:
    now = time.time()
    raw_sse = (
        b'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def fake_get(url: str, **kwargs):
        if url.endswith("/ai/v1/tasks/task_recovery_1"):
            return _Response(
                200,
                {
                    "id": "task_recovery_1",
                    "model": "claude-opus-5",
                    "created_at": now,
                    "output": [{"type": "response", "truncated": False}],
                },
            )
        return _Response(
            200,
            {},
            raw_body=raw_sse,
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(ai_interface.requests, "get", fake_get)
    config = _api_config()
    config["raw_response_dir"] = str(tmp_path / "recovery-raw")
    config["aihubmix_recovery_proof"] = {
        "task_id": "task_recovery_1",
        "request_fingerprint": "test-fingerprint",
        "operator": "test-operator",
        "operation_id": "operation-recovery-1",
        "attempt_id": "attempt-recovery-1",
    }

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=config,
        model="claude-opus-5",
        request_started_epoch=now - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
        request_fingerprint="test-fingerprint",
    )

    assert result is not None
    assert result["status"] == "success"
    assert result["content"] == {"ok": True}
    assert result["response_protocol"] == "sse"
    assert result["response_complete"] is True
    assert Path(result["raw_response_path"]).read_bytes() == raw_sse


def test_aihubmix_recovery_is_opt_in(monkeypatch) -> None:
    config = _api_config()
    config.pop("aihubmix_recovery_enabled")
    monkeypatch.delenv("AUTO_GENERATE_AIHUBMIX_RECOVERY", raising=False)
    monkeypatch.setattr(
        ai_interface.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GET must not run")),
    )

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=config,
        model="claude-opus-5",
        request_started_epoch=time.time() - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
    )

    assert result is None

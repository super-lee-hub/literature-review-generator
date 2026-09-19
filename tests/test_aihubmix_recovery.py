from __future__ import annotations

import json
import time

import ai_interface


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self) -> object:
        return self._payload


def _api_config() -> dict[str, str]:
    return {
        "api_base": "https://aihubmix.com/v1",
        "api_key": "sk-test-recovery",
        "model": "claude-opus-5",
        "proxy_mode": "environment",
        "aihubmix_recovery_enabled": "true",
    }


def test_aihubmix_recovery_requires_one_recent_matching_task(monkeypatch) -> None:
    now = time.time()
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if url.endswith("/ai/v1/tasks"):
            return _Response(
                200,
                {
                    "data": [
                        {
                            "id": "task_recovery_1",
                            "model": "claude-opus-5",
                            "created_at": now,
                            "output": [{"type": "response", "truncated": False}],
                        }
                    ]
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
    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=_api_config(),
        model="claude-opus-5",
        request_started_epoch=now - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
    )

    assert result is not None
    assert result["status"] == "success"
    assert result["content"] == {"ok": True}
    assert result["recovered_from_aihubmix_task"] is True
    assert result["aihubmix_recovery_task_id"] == "task_recovery_1"
    assert calls == [
        "https://aihubmix.com/ai/v1/tasks",
        "https://aihubmix.com/ai/v1/tasks/task_recovery_1/content",
    ]


def test_aihubmix_recovery_refuses_ambiguous_tasks(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_interface.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            200,
            {
                "data": [
                    {"id": "task_a", "model": "claude-opus-5", "created_at": time.time()},
                    {"id": "task_b", "model": "claude-opus-5", "created_at": time.time()},
                ]
            },
        ),
    )

    result = ai_interface._aihubmix_recover_disconnected_call(
        api_config=_api_config(),
        model="claude-opus-5",
        request_started_epoch=time.time() - 1,
        response_parser=ai_interface.parse_chat_completions_response,
        response_format="json",
    )

    assert result is None


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

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import tests.live.test_deepseek_vision_smoke as smoke


def _clear_deepseek_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "AUTO_GENERATE_DEEPSEEK_API_KEY",
        "AUTO_GENERATE_LIVE_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "foreign_env",
    [
        {"OPENAI_API_KEY": "openai-secret"},
        {"AUTO_GENERATE_LIVE_API_KEY": "generic-secret"},
    ],
)
def test_deepseek_smoke_skips_without_deepseek_key_and_never_transports_foreign_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_env: dict[str, str],
) -> None:
    _clear_deepseek_environment(monkeypatch)
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_API", "1")
    for name, value in foreign_env.items():
        monkeypatch.setenv(name, value)
    calls: list[dict[str, Any]] = []

    def forbidden_transport(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("DeepSeek transport must not run without a DeepSeek key")

    monkeypatch.setattr(smoke.ai_interface, "_call_ai_api_detailed", forbidden_transport)
    with pytest.raises(pytest.skip.Exception, match="NOT_RUN_NO_DEEPSEEK_KEY"):
        smoke.test_deepseek_vision_live_smoke(tmp_path)
    assert calls == []


def test_deepseek_smoke_allows_only_deepseek_specific_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_deepseek_environment(monkeypatch)
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_API", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    image_path = tmp_path / "synthetic.png"
    image_path.write_bytes(b"synthetic-image")
    monkeypatch.setattr(smoke, "_build_smoke_pdf_and_page_image", lambda _tmp: image_path)
    captured: dict[str, Any] = {}

    def fake_transport(
        prompt_text: str,
        api_config: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["api_config"] = dict(api_config)
        captured["kwargs"] = dict(kwargs)
        return {
            "status": "success",
            "http_status": 200,
            "transport_metadata": {"images_actually_sent_count": 1},
            "provider_response_model": smoke._MODEL,
            "provider_usage_present": True,
            "usage_status": "reported",
            "provider_receipt": {
                "status": "success",
                "metadata": {"images_actually_sent_count": 1},
            },
        }

    monkeypatch.setattr(smoke.ai_interface, "_call_ai_api_detailed", fake_transport)
    smoke.test_deepseek_vision_live_smoke(tmp_path)

    assert captured["api_config"]["api_key"] == "deepseek-secret"
    assert captured["api_config"]["provider_family"] == "deepseek"
    output = capsys.readouterr().out
    assert "openai-secret" not in output
    assert "deepseek-secret" not in output


def test_deepseek_smoke_requires_explicit_live_opt_in_even_with_deepseek_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_deepseek_environment(monkeypatch)
    monkeypatch.delenv("AUTO_GENERATE_RUN_LIVE_API", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    calls: list[object] = []

    def forbidden_transport(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        raise AssertionError("live transport must require explicit opt-in")

    monkeypatch.setattr(smoke.ai_interface, "_call_ai_api_detailed", forbidden_transport)
    with pytest.raises(pytest.skip.Exception, match="NOT_RUN_LIVE_API_NOT_ENABLED"):
        smoke.test_deepseek_vision_live_smoke(tmp_path)
    assert calls == []

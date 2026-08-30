from pathlib import Path

import pytest

from free_mode.profile_manager import get_profile_path
from free_mode.service import generate_free_mode_profile, plan_free_mode_chat_turn


def test_plan_free_mode_chat_turn_uses_dedicated_api_and_normalizes_profile(monkeypatch) -> None:
    captured = {}

    def fake_call_ai_api(prompt, api_config, system_prompt, max_tokens=4000, temperature=0.3, response_format="json", logger=None, **_kwargs):
        captured["prompt"] = prompt
        captured["api_config"] = api_config
        captured["system_prompt"] = system_prompt
        captured["max_tokens"] = max_tokens
        captured["temperature"] = temperature
        captured["response_format"] = response_format
        return {
            "assistant_message": "先把 A 到 B 的主线钉住。",
            "ready_to_apply": True,
            "missing_information": ["是否限定具体研究场景"],
            "profile": {
                "research_goal": "解释 A 如何推导到 B",
                "focus_points": ["变量链路"],
                "generated_prompt": "请围绕 A 到 B 的推导组织综述。",
            },
        }

    monkeypatch.setattr("free_mode.service._call_ai_api", fake_call_ai_api)

    result = plan_free_mode_chat_turn(
        messages=[
            {"role": "user", "content": "我想写 A 到 B 的推导。"},
            {"role": "assistant", "content": "你更想强调变量链路还是理论解释？"},
            {"role": "user", "content": "先强调变量链路。"},
        ],
        config={
            "Free_Mode_API": {
                "api_key": "free-mode-key",
                "model": "planner-model",
                "api_base": "https://api.example.com/v1",
                "max_output_tokens": "2222",
                "temperature": "0.15",
            },
        },
    )

    assert result is not None
    assert result["ready_to_apply"] is True
    assert result["missing_information"] == ["是否限定具体研究场景"]
    assert result["profile"]["research_goal"] == "解释 A 如何推导到 B"
    assert result["profile"]["generated_prompt"] == "请围绕 A 到 B 的推导组织综述。"
    assert captured["api_config"]["model"] == "planner-model"
    assert captured["response_format"] == "json"
    assert captured["max_tokens"] == 2222
    assert captured["temperature"] == 0.15


def test_generate_free_mode_profile_requires_its_own_api(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    called = {"value": False}

    def fake_call_ai_api(prompt, api_config, system_prompt, max_tokens=4000, temperature=0.3, response_format="json", logger=None, **_kwargs):
        called["value"] = True
        captured["prompt"] = prompt
        captured["api_config"] = api_config
        return {
            "research_goal": "比较 A 与 B 的理论连接",
            "concept_relationship": "从 A 到 B 的推导",
            "focus_points": ["理论解释", "research gap"],
            "generated_prompt": "请围绕 A 到 B 的推导主线写综述。",
            "conversation_notes": ["强调理论解释", "补足 research gap"],
        }

    monkeypatch.setattr("free_mode.service._call_ai_api", fake_call_ai_api)

    profile = generate_free_mode_profile(
        user_idea="",
        config={
            "Outline_API": {
                "api_key": "outline-key",
                "model": "outline-model",
                "api_base": "https://outline.example.com/v1",
            },
        },
        output_dir=str(tmp_path),
        project_name="demo",
        conversation_messages=[
            {"role": "user", "content": "我想写 A 到 B 的推导。"},
            {"role": "assistant", "content": "你想强调理论解释还是变量链路？"},
            {"role": "user", "content": "更强调理论解释。"},
        ],
    )

    assert profile is None
    assert called["value"] is False
    assert not Path(get_profile_path(str(tmp_path), "demo")).exists()


def test_generate_free_mode_profile_uses_only_free_mode_api(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_call_ai_api(prompt, api_config, system_prompt, max_tokens=4000, temperature=0.3, response_format="json", logger=None, **_kwargs):
        captured["prompt"] = prompt
        captured["api_config"] = api_config
        return {
            "research_goal": "比较 A 与 B 的理论连接",
            "concept_relationship": "从 A 到 B 的推导",
            "focus_points": ["理论解释", "research gap"],
            "generated_prompt": "请围绕 A 到 B 的推导主线写综述。",
            "conversation_notes": ["强调理论解释", "补足 research gap"],
        }

    monkeypatch.setattr("free_mode.service._call_ai_api", fake_call_ai_api)

    profile = generate_free_mode_profile(
        user_idea="",
        config={
            "Outline_API": {
                "api_key": "outline-key",
                "model": "outline-model",
                "api_base": "https://outline.example.com/v1",
            },
            "Free_Mode_API": {
                "api_key": "free-key",
                "model": "free-model",
                "api_base": "https://free.example.com/v1",
            },
        },
        output_dir=str(tmp_path),
        project_name="demo",
        conversation_messages=[
            {"role": "user", "content": "我想写 A 到 B 的推导。"},
            {"role": "assistant", "content": "你想强调理论解释还是变量链路？"},
            {"role": "user", "content": "更强调理论解释。"},
        ],
    )

    assert profile is not None
    assert profile["research_goal"] == "比较 A 与 B 的理论连接"
    assert captured["api_config"]["model"] == "free-model"
    assert captured["api_config"]["api_base"] == "https://free.example.com/v1"
    assert "对话记录" in captured["prompt"]
    assert Path(get_profile_path(str(tmp_path), "demo")).exists()


def test_generate_free_mode_profile_does_not_complete_an_incomplete_free_route(
    tmp_path: Path, monkeypatch
) -> None:
    called = {"value": False}

    def fake_call_ai_api(*_args, **_kwargs):
        called["value"] = True
        raise AssertionError("incomplete Free_Mode_API must fail before transport")

    monkeypatch.setattr("free_mode.service._call_ai_api", fake_call_ai_api)

    profile = generate_free_mode_profile(
        user_idea="只提供了一个想法",
        config={
            "Outline_API": {
                "api_key": "outline-key",
                "model": "outline-model",
                "api_base": "https://outline.example.com/v1",
            },
            "Free_Mode_API": {
                "api_key": "",
                "model": "",
                "api_base": "",
            },
        },
        output_dir=str(tmp_path),
        project_name="incomplete",
    )

    assert profile is None
    assert called["value"] is False


@pytest.mark.parametrize(
    "free_route",
    [
        {"api_key": "free-key", "model": "free-model", "api_base": ""},
        {"api_key": "free-key", "model": "", "api_base": "https://free.example.com/v1"},
    ],
    ids=("missing-api-base", "missing-model"),
)
def test_generate_free_mode_profile_rejects_partial_dedicated_route(
    tmp_path: Path, monkeypatch, free_route
) -> None:
    called = {"value": False}

    def fake_call_ai_api(*_args, **_kwargs):
        called["value"] = True
        raise AssertionError("partial Free_Mode_API must fail before transport")

    monkeypatch.setattr("free_mode.service._call_ai_api", fake_call_ai_api)

    profile = generate_free_mode_profile(
        user_idea="不完整的独立路由",
        config={"Free_Mode_API": free_route},
        output_dir=str(tmp_path),
        project_name="partial",
    )

    assert profile is None
    assert called["value"] is False
    assert not Path(get_profile_path(str(tmp_path), "partial")).exists()

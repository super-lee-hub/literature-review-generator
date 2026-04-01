"""Free-mode chat planning and profile synthesis services."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ai_interface import _call_ai_api
from free_mode.profile_manager import normalize_profile, save_profile
from services.model_selection import get_free_mode_api_config


FREE_MODE_PROFILE_SYSTEM_PROMPT = """你是一个学术文献综述规划助手。
你的任务不是直接写综述，而是把用户的自然语言想法转化为一个可执行的综述 prompt profile。
你必须返回 JSON，并且严格包含以下字段：
research_goal, concept_relationship, focus_points, exclusions, theory_or_variable_focus,
outline_preferences, writing_constraints, generated_prompt, conversation_notes。
如果用户没有给出某项信息，允许返回空字符串或空数组，不要硬猜。"""


FREE_MODE_CHAT_SYSTEM_PROMPT = """你是一个学术文献综述自由模式规划助手。
你的职责是和用户多轮澄清写作意图，然后逐步沉淀成适合后续综述生成流程的 prompt profile。
你不能直接开始写综述正文；你要做的是帮助用户把想法变清楚、变可执行。

每次都必须返回 JSON，且严格包含以下字段：
assistant_message, ready_to_apply, missing_information, profile。

字段要求：
1. assistant_message: 给用户的自然语言回复。先简短总结已明确的信息，再提出 1-2 个最关键的问题；如果已经足够清楚，就说明可以应用到本次任务。
2. ready_to_apply: 布尔值。只有当研究目标、概念关系或主线、以及生成 prompt 已经基本清楚时才返回 true。
3. missing_information: 字符串数组。列出仍然缺失但最影响后续综述执行的信息；如果已经足够清楚，可返回空数组。
4. profile: 一个 JSON 对象，并且严格包含以下字段：
research_goal, concept_relationship, focus_points, exclusions, theory_or_variable_focus,
outline_preferences, writing_constraints, generated_prompt, conversation_notes。

profile 规则：
- generated_prompt 必须是给后续综述流程使用的优化 prompt，而不是综述正文。
- conversation_notes 应该是对已确认关键信息的简短项目符号式摘要。
- 如果用户明确说想写 A 到 B 的推导、比较、机制、边界条件、变量链路或 research gap，要尽量结构化到 profile 里。
- 如果信息还不够，不要强行补全，只保留已经明确的部分。"""


def _get_free_mode_parameters(config: Dict[str, Any], logger: Any = None) -> tuple[int, float]:
    api_params = (config or {}).get("API_Parameters", {}) or {}
    try:
        max_tokens = int(api_params.get("free_mode_max_tokens", api_params.get("outline_max_tokens", 6000)))
        temperature = float(api_params.get("free_mode_temperature", api_params.get("outline_temperature", 0.4)))
        return max_tokens, temperature
    except (TypeError, ValueError) as exc:
        if logger:
            logger.warning(f"读取自由模式 API 参数失败，使用默认值: {exc}")
        return 6000, 0.4


def _serialize_conversation(messages: Optional[List[Dict[str, str]]]) -> str:
    lines: List[str] = []
    for message in messages or []:
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines).strip()


def _normalize_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_planner_response(response: Dict[str, Any]) -> Dict[str, Any]:
    raw_profile = response.get("profile") if isinstance(response.get("profile"), dict) else response
    profile = normalize_profile(raw_profile if isinstance(raw_profile, dict) else {})
    missing_information = _normalize_string_list(response.get("missing_information", []))
    assistant_message = str(response.get("assistant_message") or "").strip()
    ready_to_apply = bool(response.get("ready_to_apply"))

    if not assistant_message:
        if ready_to_apply:
            assistant_message = "当前信息已经足够清楚，可以应用到本次任务。"
        elif missing_information:
            assistant_message = "我先把你的想法整理了一版。为了让后续综述更贴合目标，还想确认这几个点："
        else:
            assistant_message = "我先根据当前对话整理了一版自由模式规划。你可以继续补充，也可以直接应用。"

    return {
        "assistant_message": assistant_message,
        "ready_to_apply": ready_to_apply,
        "missing_information": missing_information,
        "profile": profile,
    }


def plan_free_mode_chat_turn(
    messages: List[Dict[str, str]],
    config: Dict[str, Any],
    logger: Any = None,
) -> Optional[Dict[str, Any]]:
    """Plan one conversational turn and update the free-mode profile draft."""

    free_mode_api = get_free_mode_api_config(config)
    if not free_mode_api.get("api_key") or not free_mode_api.get("model"):
        if logger:
            logger.error("Free_Mode_API 未配置，且无法回退到可用的 Outline_API。")
        return None

    transcript = _serialize_conversation(messages)
    if not transcript:
        return None

    max_tokens, temperature = _get_free_mode_parameters(config, logger=logger)
    prompt = (
        "以下是当前自由模式规划对话，请基于完整上下文继续规划。\n\n"
        f"{transcript}\n\n"
        "请输出本轮应答、缺失信息，以及更新后的 profile 草案。"
    )
    response = _call_ai_api(
        prompt=prompt,
        api_config=free_mode_api,
        system_prompt=FREE_MODE_CHAT_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
    )
    if not isinstance(response, dict):
        return None
    return _normalize_planner_response(response)


def generate_free_mode_profile(
    user_idea: str,
    config: Dict[str, Any],
    output_dir: str,
    project_name: str,
    logger: Any = None,
    conversation_notes: Optional[List[str]] = None,
    conversation_messages: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Generate and persist a free-mode profile from user intent or a chat transcript."""

    free_mode_api = get_free_mode_api_config(config)
    if not free_mode_api.get("api_key") or not free_mode_api.get("model"):
        if logger:
            logger.error("Free_Mode_API 未配置，无法生成自由模式 profile。")
        return None

    max_tokens, temperature = _get_free_mode_parameters(config, logger=logger)
    note_lines = _normalize_string_list(conversation_notes or [])
    note_block = "\n".join(f"- {note}" for note in note_lines)
    transcript = _serialize_conversation(conversation_messages)

    if transcript:
        prompt = (
            "请根据以下自由模式对话记录，生成一个适用于本项目学术综述流程的最终 prompt profile。\n\n"
            f"对话记录:\n{transcript}\n\n"
            f"补充备注:\n{note_block if note_block else '(无)'}\n"
        )
    else:
        prompt = (
            "请根据以下用户输入，生成一个适用于本项目学术综述流程的 prompt profile。\n\n"
            f"用户原始想法:\n{user_idea.strip()}\n\n"
            f"补充对话记录:\n{note_block if note_block else '(无)'}\n"
        )

    response = _call_ai_api(
        prompt=prompt,
        api_config=free_mode_api,
        system_prompt=FREE_MODE_PROFILE_SYSTEM_PROMPT,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
    )
    if not isinstance(response, dict):
        return None

    raw_profile = response.get("profile") if isinstance(response.get("profile"), dict) else response
    profile = normalize_profile(raw_profile if isinstance(raw_profile, dict) else {})
    if note_lines and not profile.get("conversation_notes"):
        profile["conversation_notes"] = note_lines
    save_profile(profile, output_dir=output_dir, project_name=project_name)
    return profile


def apply_free_mode_profile(profile: Dict[str, Any], output_dir: str, project_name: str) -> str:
    """Persist the current free-mode profile draft and return its path."""

    return save_profile(normalize_profile(profile), output_dir=output_dir, project_name=project_name)


def profile_to_json(profile: Dict[str, Any]) -> str:
    return json.dumps(normalize_profile(profile), ensure_ascii=False, indent=2)

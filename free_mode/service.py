"""Free-mode chat planning and profile synthesis services."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_interface import _call_ai_api
from free_mode.profile_manager import normalize_profile, save_profile
from models import APIConfig
from runtime.provider_runtime import ProviderBudgetV1, ProviderBudgetExceeded, ProviderRuntime, ProviderRuntimeLedger
from services.model_selection import get_free_mode_api_config, has_complete_api_route
from services.prompt_registry import PromptRegistry


def _prompt_registry() -> PromptRegistry:
    return PromptRegistry()


def _get_free_mode_parameters(config: Dict[str, Any], logger: Any = None) -> tuple[int, float]:
    api_params = (config or {}).get("Free_Mode_API", {}) or {}
    try:
        max_tokens = int(api_params.get("max_output_tokens", 6000))
        temperature = float(api_params.get("temperature", 0.4))
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


def _new_free_mode_provider_runtime(
    *,
    api_config: APIConfig,
    output_dir: Optional[str],
    project_name: str,
    stage_name: str,
    prompt: str,
    config: Dict[str, Any],
    provider_runtime: Optional[ProviderRuntime],
    prompt_id: str = "",
) -> Optional[ProviderRuntime]:
    if provider_runtime is not None:
        return provider_runtime
    resolved_output_dir = str(output_dir or "").strip()
    if not resolved_output_dir:
        return None
    safe_project = str(project_name or "free_mode").strip() or "free_mode"
    ledger_path = Path(resolved_output_dir).expanduser().resolve() / f"{safe_project}_free_mode_provider_receipts.jsonl"
    try:
        retry_limit = max(0, int(str((config or {}).get("Runtime", {}).get("node_retry_limit", 2)).strip()))
    except (TypeError, ValueError):
        retry_limit = 2
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]
    prompt_identity = _prompt_registry().identity(prompt_id) if prompt_id else None
    return ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=retry_limit),
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id=f"free-mode:{safe_project}",
        attempt_id=f"free-mode:{stage_name}",
        stage_name=f"free_mode_{stage_name}",
        route="Free_Mode_API",
        node_id=prompt_hash,
        call_id=f"free-mode:{stage_name}:{prompt_hash}",
        endpoint_type=str(api_config.get("endpoint_type") or "chat_completions"),
        schema_hash=hashlib.sha256(b"free-mode-provider-request-v1").hexdigest(),
        prompt_id=prompt_identity.prompt_id if prompt_identity else "",
        prompt_version=prompt_identity.version if prompt_identity else "",
        prompt_sha256=prompt_identity.sha256 if prompt_identity else "",
    )


def _complete_injected_free_mode_runtime(
    provider_runtime: Optional[ProviderRuntime],
    *,
    prompt: str,
    api_config: APIConfig,
    response: Any,
) -> None:
    if provider_runtime is None or provider_runtime.receipts:
        return
    result = {
        "status": "success" if isinstance(response, dict) else "failed",
        "content": response if isinstance(response, dict) else None,
        "finish_reason": "stop" if isinstance(response, dict) else "",
        "usage_status": "reported",
        "error_kind": None if isinstance(response, dict) else "invalid_response",
    }
    try:
        admission = provider_runtime.admit(estimated_tokens=max(1, len(prompt) // 4))
        provider_runtime.complete(
            admission=admission,
            prompt=prompt,
            input_payload={"prompt": prompt},
            api_config=api_config,
            result=result,
            metadata={"execution_mode": "injected_free_mode"},
        )
    except ProviderBudgetExceeded:
        provider_runtime.blocked_receipt(
            prompt=prompt,
            input_payload={"prompt": prompt},
            api_config=api_config,
            message="free-mode provider did not produce a receipt before its budget closed",
        )


def _call_free_mode_api(
    *,
    prompt: str,
    api_config: APIConfig,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    logger: Any,
    provider_runtime: Optional[ProviderRuntime],
    prompt_id: str = "",
) -> Any:
    call_kwargs: Dict[str, Any] = {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": "json",
        "logger": logger,
    }
    if provider_runtime is not None:
        if prompt_id:
            identity = _prompt_registry().identity(prompt_id)
            provider_runtime.prompt_id = identity.prompt_id
            provider_runtime.prompt_version = identity.version
            provider_runtime.prompt_sha256 = identity.sha256
        call_kwargs["provider_runtime"] = provider_runtime
    response = _call_ai_api(
        prompt=prompt,
        api_config=api_config,
        system_prompt=system_prompt,
        **call_kwargs,
    )
    _complete_injected_free_mode_runtime(
        provider_runtime,
        prompt=prompt,
        api_config=api_config,
        response=response,
    )
    return response


def plan_free_mode_chat_turn(
    messages: List[Dict[str, str]],
    config: Dict[str, Any],
    logger: Any = None,
    output_dir: Optional[str] = None,
    project_name: str = "free_mode",
    provider_runtime: Optional[ProviderRuntime] = None,
) -> Optional[Dict[str, Any]]:
    """Plan one conversational turn and update the free-mode profile draft."""

    free_mode_section = (config or {}).get("Free_Mode_API")
    if not has_complete_api_route(free_mode_section):
        if logger:
            logger.error(
                "Free_Mode_API 未完整配置。自由模式不再从 Outline_API "
                "隐式继承 provider route。"
            )
        return None
    free_mode_api = get_free_mode_api_config(config)

    transcript = _serialize_conversation(messages)
    if not transcript:
        return None

    max_tokens, temperature = _get_free_mode_parameters(config, logger=logger)
    prompt = (
        "以下是当前自由模式规划对话，请基于完整上下文继续规划。\n\n"
        f"{transcript}\n\n"
        "请输出本轮应答、缺失信息，以及更新后的 profile 草案。"
    )
    runtime = _new_free_mode_provider_runtime(
        api_config=free_mode_api,
        output_dir=output_dir,
        project_name=project_name,
        stage_name="chat",
        prompt=prompt,
        config=config,
        provider_runtime=provider_runtime,
        prompt_id="free_mode.chat.system.v1",
    )
    response = _call_free_mode_api(
        prompt=prompt,
        api_config=free_mode_api,
        system_prompt=_prompt_registry().read("free_mode.chat.system.v1"),
        max_tokens=max_tokens,
        temperature=temperature,
        logger=logger,
        provider_runtime=runtime,
        prompt_id="free_mode.chat.system.v1",
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
    provider_runtime: Optional[ProviderRuntime] = None,
) -> Optional[Dict[str, Any]]:
    """Generate and persist a free-mode profile from user intent or a chat transcript."""

    free_mode_section = (config or {}).get("Free_Mode_API")
    if not has_complete_api_route(free_mode_section):
        if logger:
            logger.error(
                "Free_Mode_API 未完整配置。自由模式不再从 Outline_API "
                "隐式继承 provider route。"
            )
        return None
    free_mode_api = get_free_mode_api_config(config)

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

    runtime = _new_free_mode_provider_runtime(
        api_config=free_mode_api,
        output_dir=output_dir,
        project_name=project_name,
        stage_name="profile",
        prompt=prompt,
        config=config,
        provider_runtime=provider_runtime,
        prompt_id="free_mode.profile.system.v1",
    )
    response = _call_free_mode_api(
        prompt=prompt,
        api_config=free_mode_api,
        system_prompt=_prompt_registry().read("free_mode.profile.system.v1"),
        max_tokens=max_tokens,
        temperature=temperature,
        logger=logger,
        provider_runtime=runtime,
        prompt_id="free_mode.profile.system.v1",
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

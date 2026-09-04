#!/usr/bin/env python3
"""Strict validation for the current configuration schema."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

import requests  # type: ignore

from services.model_capabilities import (
    DEFAULT_ANTHROPIC_VERSION,
    resolve_anthropic_effort,
    resolve_anthropic_messages_url,
    resolve_model_capability,
)
from services.proxy_policy import should_bypass_environment_proxy
from services.repair_policy import parse_repair_policy
from services.config_values import (
    StrictConfigValueError,
    normalize_stage1_config_sections,
)
from services.settings import ApplicationSettings, validate_config_keys


class ConfigValidationError(Exception):
    """Raised when a configuration value violates a current contract."""


_ANTHROPIC_EFFORT_VALUES = frozenset({
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "auto_highest",
})


def _normalize_config_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_api_transport_combo(section_name: str, section: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate provider/endpoint/reasoning fields that shape request payloads.

    Returns ``(errors, warnings)``. An impossible transport combination is an
    error, not a warning: the request cannot be built at all, so reporting it as
    an advisory would let ``doctor`` look green while the section is unusable.
    Reasoning fields that merely degrade -- an effort level that gets clamped, a
    budget token that no longer applies -- stay warnings, because the transport
    still produces a valid request.
    """

    messages: List[str] = []
    errors: List[str] = []
    provider_family = _normalize_config_text(section.get("provider_family")).replace("-", "_").lower()
    endpoint_type = _normalize_config_text(section.get("endpoint_type")).replace("-", "_").lower()
    api_base = _normalize_config_text(section.get("api_base")).lower()
    reasoning_effort = _normalize_config_text(section.get("reasoning_effort"))
    reasoning_display = _normalize_config_text(section.get("reasoning_display"))
    thinking = _normalize_config_text(section.get("thinking"))

    known_families = {
        "openai_responses",
        "claude_chat_reasoning",
        "aihubmix_openai",
        "aihubmix_claude",
        "anthropic",
        "deepseek",
        "generic",
    }
    if provider_family and provider_family not in known_families:
        errors.append(f"[{section_name}] provider_family={provider_family!r} is not supported")
    if endpoint_type and endpoint_type not in {"chat_completions", "responses", "response", "anthropic"}:
        errors.append(f"[{section_name}] endpoint_type={endpoint_type!r} is not supported")
    if provider_family in {"aihubmix_openai", "openai_responses"} and endpoint_type not in {"", "responses", "response"}:
        errors.append(f"[{section_name}] the selected provider family requires endpoint_type=responses")
    if provider_family in {"claude_chat_reasoning", "aihubmix_claude", "deepseek"} and endpoint_type not in {"", "chat_completions"}:
        errors.append(f"[{section_name}] the selected provider family requires endpoint_type=chat_completions")
    # Anthropic Messages is a different wire contract from both OpenAI
    # protocols, so the pairing is checked in both directions. A half-configured
    # section would otherwise be silently routed down the wrong transport.
    if provider_family == "anthropic" and endpoint_type not in {"", "anthropic"}:
        errors.append(f"[{section_name}] provider_family=anthropic requires endpoint_type=anthropic")
    if endpoint_type == "anthropic" and provider_family not in {"", "anthropic"}:
        errors.append(f"[{section_name}] endpoint_type=anthropic requires provider_family=anthropic")
    if provider_family == "deepseek" and api_base and "deepseek" not in api_base:
        messages.append(f"[{section_name}] provider_family=deepseek should use a DeepSeek api_base")
    if provider_family.startswith("aihubmix") and api_base and "aihubmix" not in api_base:
        messages.append(f"[{section_name}] provider_family={provider_family} should use an AIHubMix api_base")

    capability = resolve_model_capability(section)  # type: ignore[arg-type]
    if (reasoning_effort or reasoning_display or thinking) and not capability.supports_reasoning:
        messages.append(f"[{section_name}] reasoning fields are set but the selected model does not support reasoning")
    if reasoning_display and capability.reasoning_param_style != "chat_reasoning":
        messages.append(f"[{section_name}] reasoning_display is only valid for chat reasoning providers")
    # Anthropic exposes thinking through the same key, but the mode differs by
    # model generation; DeepSeek is no longer the only valid style.
    if thinking and capability.reasoning_param_style not in {"deepseek_thinking", "anthropic_thinking"}:
        messages.append(
            f"[{section_name}] thinking is only valid for DeepSeek reasoning or the "
            "Anthropic Messages transport"
        )
    if _normalize_config_text(section.get("thinking_budget_tokens")) and capability.anthropic_thinking_mode != "manual":
        messages.append(
            f"[{section_name}] thinking_budget_tokens applies only to manual extended "
            "thinking (Claude 4.5 and earlier); on adaptive models depth is controlled "
            "by reasoning_effort instead"
        )
    # An effort level the model does not accept is a rejected request, so it is
    # reported here rather than surfacing as a runtime failure far from the cause.
    model_id = _normalize_config_text(section.get("model"))
    if endpoint_type == "anthropic" and reasoning_effort and reasoning_effort.casefold() not in _ANTHROPIC_EFFORT_VALUES:
        errors.append(
            f"[{section_name}] reasoning_effort={reasoning_effort!r} is invalid; "
            "Anthropic effort must be low/medium/high/xhigh/max/auto_highest"
        )
    elif reasoning_effort and capability.anthropic_effort_levels:
        resolved = resolve_anthropic_effort(reasoning_effort, model_id)
        if resolved and resolved != reasoning_effort.lower():
            messages.append(
                f"[{section_name}] reasoning_effort={reasoning_effort!r} is not supported by "
                f"this model and will be reduced to {resolved!r}"
            )
    return errors, messages


def validate_file_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not path:
        return (True, "") if allow_empty else (False, "文件路径不能为空")
    if ".." in path:
        return False, "路径不能包含'..'"
    return (True, "") if os.path.isfile(path) else (False, "文件不存在")


def validate_directory_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not path:
        return (True, "") if allow_empty else (False, "目录路径不能为空")
    if ".." in path:
        return False, "路径不能包含'..'"
    return (True, "") if os.path.isdir(path) else (False, "目录不存在")


def validate_output_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not path:
        return (True, "") if allow_empty else (False, "输出路径不能为空")
    if ".." in path:
        return False, "路径不能包含'..'"
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".permission_test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("test")
        os.remove(probe)
        return True, ""
    except OSError as exc:
        return False, f"无法创建或写入目录: {exc}"


def validate_api_key(api_key: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not api_key:
        return (True, "") if allow_empty else (False, "API Key不能为空")
    value = api_key.strip()
    if value in {"loaded_from_.env_file", "YOUR_PRIMARY_READER_API_KEY_HERE", "YOUR_BACKUP_READER_API_KEY_HERE", "YOUR_WRITER_API_KEY_HERE", "YOUR_VALIDATOR_API_KEY_HERE"}:
        return True, ""
    if len(value) < 8:
        return False, "API Key长度似乎过短，请确认是否正确"
    return True, ""


def validate_url(url: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not url:
        return (True, "") if allow_empty else (False, "URL不能为空")
    value = url.strip().rstrip("/")
    if not (value.startswith("http://") or value.startswith("https://")):
        return False, "URL应该以'http://'或'https://'开头"
    pattern = re.compile(r"^https?://(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}\.?|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/?|[/?]\S+)$", re.IGNORECASE)
    return (True, "") if pattern.match(value) else (False, "URL格式不正确")


def validate_numeric_range(value: str, min_val: int, max_val: int, allow_empty: bool = False) -> Tuple[bool, str]:
    if value is None or str(value).strip() == "":
        return (True, "") if allow_empty else (False, "值不能为空")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False, "请输入一个有效的数字"
    if number < min_val or number > max_val:
        return False, f"值应在{min_val}-{max_val}之间"
    return True, ""


def validate_positive_number(value: str, allow_empty: bool = False) -> Tuple[bool, str]:
    valid, error = validate_numeric_range(value, 1, 1000000, allow_empty=allow_empty)
    return valid, error if valid else ("值应大于0" if "范围" in error else error)


def validate_model_name(model: str, allow_empty: bool = False) -> Tuple[bool, str]:
    if not model or not model.strip():
        return (True, "") if allow_empty else (False, "模型名称不能为空")
    return (True, "") if re.match(r"^[a-zA-Z0-9._/:-]+$", model.strip()) else (False, "模型名称包含无效字符")


def validate_config_section(config_dict: Dict[str, Any], section_name: str, required_keys: List[str]) -> Tuple[bool, str]:
    if section_name not in config_dict:
        return False, f"缺少配置段: [{section_name}]"
    section = config_dict[section_name]
    for key in required_keys:
        if key not in section or not str(section[key]).strip():
            return False, f"配置项[{section_name}]{key}不能为空"
    return True, ""


def validate_all_config(config_dict: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate current settings and return ``(valid, messages)``."""

    schema_errors = validate_config_keys(config_dict)
    if schema_errors:
        return False, schema_errors
    try:
        normalized_stage1 = normalize_stage1_config_sections(config_dict)
    except StrictConfigValueError as exc:
        return False, [str(exc)]
    if "Paths" not in config_dict:
        return False, ["缺少配置段: [Paths]"]
    if not str(config_dict["Paths"].get("output_path", "")).strip():
        return False, ["配置项[Paths]output_path不能为空"]

    messages: List[str] = []
    for section_name in ("Primary_Reader_API", "Backup_Reader_API", "Writer_API"):
        valid, error = validate_config_section(config_dict, section_name, ["api_key", "model", "api_base"])
        if not valid:
            return False, [error]
        valid, error = validate_api_key(str(config_dict[section_name]["api_key"]), allow_empty=True)
        if not valid:
            messages.append(f"[{section_name}] {error}")
        combo_errors, combo_warnings = _validate_api_transport_combo(section_name, config_dict[section_name])
        if combo_errors:
            return False, combo_errors
        messages.extend(combo_warnings)
        valid, error = validate_url(str(config_dict[section_name]["api_base"]), allow_empty=True)
        if not valid:
            messages.append(f"[{section_name}] {error}")

    for section_name in ("Outline_API", "Free_Mode_API", "Validator_API"):
        if section_name not in config_dict:
            continue
        section = config_dict[section_name]
        if not any(str(value).strip() for value in section.values()):
            continue
        valid, error = validate_config_section(config_dict, section_name, ["api_key", "model", "api_base"])
        if not valid:
            return False, [error]
        combo_errors, combo_warnings = _validate_api_transport_combo(section_name, section)
        if combo_errors:
            return False, combo_errors
        messages.extend(combo_warnings)
        valid, error = validate_url(str(section["api_base"]), allow_empty=True)
        if not valid:
            messages.append(f"[{section_name}] {error}")

    runtime = config_dict.get("Runtime", {})
    for key, minimum, maximum in (("max_workers", 1, 64), ("transport_retries", 0, 10), ("node_retry_limit", 0, 1000), ("total_job_deadline_seconds", 0, 1000000)):
        if key in runtime:
            valid, error = validate_numeric_range(str(runtime[key]), minimum, maximum)
            if not valid:
                return False, [f"[Runtime] {key} {error}"]

    stage1_input = normalized_stage1.get("Stage1_Input", {})
    if isinstance(stage1_input, dict):
        mode = _normalize_config_text(stage1_input.get("mode"))
        if mode and mode.casefold() not in {"text_first", "vision_first", "text_only"}:
            return False, ["[Stage1_Input] mode must be text_first"]
        image_transport = _normalize_config_text(stage1_input.get("image_transport"))
        if image_transport and image_transport.casefold() != "base64":
            return False, ["[Stage1_Input] image_transport must be base64"]
        for key, minimum, maximum in (
            ("max_pdf_file_mb", 1, 1000000),
            ("single_call_max_pages", 1, 1000000),
            ("visual_scan_batch_size", 1, 1000000),
            ("stage1_visual_scan_max_output_tokens", 1, 1000000),
            ("stage1_synthesis_max_output_tokens", 1, 1000000),
            ("stage1_length_retry_max_attempts", 0, 10),
            ("stage1_length_retry_ceiling_tokens", 1, 1000000),
            ("stage1_request_timeout_seconds", 30, 3600),
            ("stage1_semantic_retry_max_attempts", 0, 3),
            ("final_image_refs_max", 0, 1000000),
            ("max_request_image_bytes", 1, 2000000000),
            ("max_single_image_bytes", 1, 2000000000),
        ):
            if key in stage1_input:
                valid, error = validate_numeric_range(str(stage1_input[key]), minimum, maximum)
                if not valid:
                    return False, [f"[Stage1_Input] {key} {error}"]
        visual_budget = stage1_input.get("stage1_visual_scan_max_output_tokens")
        synthesis_budget = stage1_input.get("stage1_synthesis_max_output_tokens")
        retry_ceiling = stage1_input.get("stage1_length_retry_ceiling_tokens")
        if retry_ceiling is not None:
            try:
                ceiling_value = int(str(retry_ceiling).strip())
            except (TypeError, ValueError):
                ceiling_value = 0
            for name, value in (
                ("stage1_visual_scan_max_output_tokens", visual_budget),
                ("stage1_synthesis_max_output_tokens", synthesis_budget),
            ):
                if value is None:
                    continue
                try:
                    budget_value = int(str(value).strip())
                except (TypeError, ValueError):
                    continue
                if budget_value > ceiling_value:
                    return False, [
                        f"[Stage1_Input] {name} cannot exceed stage1_length_retry_ceiling_tokens"
                    ]

    stage1_visual = normalized_stage1.get("Stage1_Visual", {})
    if isinstance(stage1_visual, dict):
        selection_mode = _normalize_config_text(stage1_visual.get("selection_mode"))
        if selection_mode and selection_mode.casefold() not in {"selective", "adaptive_page_scan"}:
            return False, [
                "[Stage1_Visual] selection_mode must be selective or adaptive_page_scan"
            ]
        render_all = _normalize_config_text(stage1_visual.get("render_all_nonblank_pages"))
        for key in ("page_format", "crop_format"):
            if key in stage1_visual:
                image_format = _normalize_config_text(stage1_visual[key]).casefold()
                if image_format not in {"jpg", "jpeg", "png"}:
                    return False, [f"[Stage1_Visual] {key} must be jpeg/jpg/png"]
        if "page_jpeg_quality" in stage1_visual:
            valid, error = validate_numeric_range(str(stage1_visual["page_jpeg_quality"]), 1, 100)
            if not valid:
                return False, [f"[Stage1_Visual] page_jpeg_quality {error}"]
        for key in (
            "page_long_edge_px", "crop_long_edge_px", "page_max_pixels",
            "crop_max_pixels", "max_visual_artifact_bytes",
        ):
            if key in stage1_visual:
                valid, error = validate_numeric_range(str(stage1_visual[key]), 1, 2000000000)
                if not valid:
                    return False, [f"[Stage1_Visual] {key} {error}"]
        for key in (
            "page_snapshot_soft_max", "figure_crop_soft_max", "table_crop_soft_max",
            "formula_crop_soft_max", "selected_visual_soft_total", "selected_visual_hard_total",
        ):
            if key in stage1_visual:
                valid, error = validate_numeric_range(str(stage1_visual[key]), 0, 1000000)
                if not valid:
                    return False, [f"[Stage1_Visual] {key} {error}"]
        try:
            soft_total = int(str(stage1_visual.get("selected_visual_soft_total", "10")).strip())
            hard_total = int(str(stage1_visual.get("selected_visual_hard_total", "16")).strip())
        except (TypeError, ValueError):
            soft_total = hard_total = 0
        if soft_total > hard_total:
            return False, [
                "[Stage1_Visual] selected_visual_soft_total cannot exceed selected_visual_hard_total"
            ]

    try:
        settings = ApplicationSettings.from_config(config_dict)
        parse_repair_policy(config_dict.get("Validation", {}).get("repair_policy"))
    except (TypeError, ValueError) as exc:
        return False, [str(exc)]
    outline_errors = settings.validate_outline_config()
    if outline_errors:
        return False, outline_errors
    # A critique that shares the generator's identity is legal but must never be
    # invisible, so it is surfaced as a warning rather than silently accepted.
    messages.extend(settings.outline_routing_diagnostics())
    preprocess = config_dict.get("Preprocess", {})
    if str(preprocess.get("ocr_mode", "auto")).lower() not in {"auto", "off", "always"}:
        return False, ["[Preprocess] ocr_mode 应为 auto/off/always 之一"]
    return True, messages


def test_api_connection(
    api_key: str,
    api_base: str,
    model: str,
    proxy_mode: str = "environment",
    *,
    provider_family: str = "",
    endpoint_type: str = "",
    anthropic_path: str = "",
    anthropic_version: str = "",
) -> Tuple[bool, str]:
    """Probe the configured wire protocol without exposing credentials.

    OpenAI-compatible providers expose a model-list endpoint. Native Anthropic
    Messages providers are probed with a one-token request instead: the native
    endpoint is the meaningful connectivity check, and it must use
    ``x-api-key`` plus ``anthropic-version`` rather than an OpenAI Bearer
    header.
    """

    base = api_base.rstrip("/")
    normalized_endpoint = _normalize_config_text(endpoint_type).replace("-", "_").casefold()
    normalized_family = _normalize_config_text(provider_family).replace("-", "_").casefold()

    if normalized_endpoint == "anthropic" or normalized_family == "anthropic":
        # Same resolver the runtime uses. A probe that builds its own URL can
        # pass while the real request 400s on a duplicated /v1, which is the
        # most misleading failure this validator could produce.
        url = resolve_anthropic_messages_url(base, _normalize_config_text(anthropic_path))
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _normalize_config_text(anthropic_version) or DEFAULT_ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
        try:
            if should_bypass_environment_proxy({"proxy_mode": proxy_mode}):
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.post(url, headers=headers, json=payload, timeout=10)
            else:
                response = requests.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                return True, f"Anthropic API 连通成功，模型'{model}'可用"
            return False, f"Anthropic API请求失败：HTTP {response.status_code}"
        except requests.exceptions.Timeout:
            return False, "连接超时：API服务器响应时间过长"
        except requests.exceptions.RequestException:
            # Do not echo the exception: a malformed endpoint may contain
            # userinfo/query material, and request libraries include the URL in
            # their error text. Credentials must never reach UI/log output.
            return False, "请求异常：无法连接 Anthropic API 服务器"

    base = re.sub(r"/chat/completions/?$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"/v1/chat/completions/?$", "/v1", base, flags=re.IGNORECASE)
    base = re.sub(r"/models/?$", "", base, flags=re.IGNORECASE)
    if not re.search(r"/v\d+$", base, flags=re.IGNORECASE):
        base = f"{base}/v1"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if should_bypass_environment_proxy({"proxy_mode": proxy_mode}):
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(f"{base}/models", headers=headers, timeout=10)
        else:
            response = requests.get(f"{base}/models", headers=headers, timeout=10)
        if response.status_code != 200:
            return False, f"API请求失败：HTTP {response.status_code}"
        try:
            model_ids = [str(item.get("id", "")) for item in response.json().get("data", [])]
        except (AttributeError, json.JSONDecodeError, TypeError):
            return False, "API响应格式异常：无法解析模型列表"
        if model in model_ids:
            return True, f"API连通成功，模型'{model}'可用"
        for model_id in model_ids:
            if model.lower() in model_id.lower() or model_id.lower() in model.lower():
                return True, f"API连通成功，找到匹配模型'{model_id}'"
        return False, f"模型不可用：指定模型'{model}'不存在或无权访问"
    except requests.exceptions.Timeout:
        return False, "连接超时：API服务器响应时间过长"
    except requests.exceptions.RequestException:
        # Keep provider errors safe even when an endpoint was entered with
        # credential-shaped URL material.
        return False, "请求异常：无法连接 API 服务器"


def validate_zotero_library_path(library_path: str) -> Tuple[bool, str]:
    if not library_path:
        return False, "Zotero库路径不能为空"
    if ".." in library_path:
        return False, "路径不能包含'..'"
    if not os.path.isdir(library_path):
        return False, "目录不存在"
    parent_db = os.path.join(os.path.dirname(library_path), "zotero.sqlite")
    current_db = os.path.join(library_path, "zotero.sqlite")
    if os.path.exists(parent_db):
        return True, "有效的Zotero存储库路径"
    if os.path.exists(current_db):
        return True, "有效的Zotero主目录路径"
    return False, f"警告：在路径'{library_path}'及其上级目录中均未找到'zotero.sqlite'文件"

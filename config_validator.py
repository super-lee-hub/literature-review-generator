#!/usr/bin/env python3
"""Strict validation for the current configuration schema."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

import requests  # type: ignore

from services.model_capabilities import resolve_model_capability
from services.proxy_policy import should_bypass_environment_proxy
from services.repair_policy import parse_repair_policy
from services.settings import ApplicationSettings, validate_config_keys


class ConfigValidationError(Exception):
    """Raised when a configuration value violates a current contract."""


def _normalize_config_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_api_transport_combo(section_name: str, section: Dict[str, Any]) -> List[str]:
    """Validate provider/endpoint/reasoning fields that shape request payloads."""

    messages: List[str] = []
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
        "deepseek",
        "generic",
    }
    if provider_family and provider_family not in known_families:
        messages.append(f"[{section_name}] provider_family={provider_family!r} is not supported")
    if endpoint_type and endpoint_type not in {"chat_completions", "responses", "response"}:
        messages.append(f"[{section_name}] endpoint_type={endpoint_type!r} is not supported")
    if provider_family in {"aihubmix_openai", "openai_responses"} and endpoint_type not in {"", "responses", "response"}:
        messages.append(f"[{section_name}] the selected provider family requires endpoint_type=responses")
    if provider_family in {"claude_chat_reasoning", "aihubmix_claude", "deepseek"} and endpoint_type not in {"", "chat_completions"}:
        messages.append(f"[{section_name}] the selected provider family requires endpoint_type=chat_completions")
    if provider_family == "deepseek" and api_base and "deepseek" not in api_base:
        messages.append(f"[{section_name}] provider_family=deepseek should use a DeepSeek api_base")
    if provider_family.startswith("aihubmix") and api_base and "aihubmix" not in api_base:
        messages.append(f"[{section_name}] provider_family={provider_family} should use an AIHubMix api_base")

    capability = resolve_model_capability(section)  # type: ignore[arg-type]
    if (reasoning_effort or reasoning_display or thinking) and not capability.supports_reasoning:
        messages.append(f"[{section_name}] reasoning fields are set but the selected model does not support reasoning")
    if reasoning_display and capability.reasoning_param_style != "chat_reasoning":
        messages.append(f"[{section_name}] reasoning_display is only valid for chat reasoning providers")
    if thinking and capability.reasoning_param_style != "deepseek_thinking":
        messages.append(f"[{section_name}] thinking is only valid for DeepSeek reasoning")
    return messages


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
        messages.extend(_validate_api_transport_combo(section_name, config_dict[section_name]))
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
        messages.extend(_validate_api_transport_combo(section_name, section))
        valid, error = validate_url(str(section["api_base"]), allow_empty=True)
        if not valid:
            messages.append(f"[{section_name}] {error}")

    runtime = config_dict.get("Runtime", {})
    for key, minimum, maximum in (("max_workers", 1, 64), ("transport_retries", 0, 10), ("node_retry_limit", 0, 1000), ("total_job_deadline_seconds", 0, 1000000)):
        if key in runtime:
            valid, error = validate_numeric_range(str(runtime[key]), minimum, maximum)
            if not valid:
                return False, [f"[Runtime] {key} {error}"]

    try:
        settings = ApplicationSettings.from_config(config_dict)
        parse_repair_policy(config_dict.get("Validation", {}).get("repair_policy"))
    except (TypeError, ValueError) as exc:
        return False, [str(exc)]
    outline_errors = settings.validate_outline_config()
    if outline_errors:
        return False, outline_errors
    preprocess = config_dict.get("Preprocess", {})
    if str(preprocess.get("ocr_mode", "auto")).lower() not in {"auto", "off", "always"}:
        return False, ["[Preprocess] ocr_mode 应为 auto/off/always 之一"]
    return True, messages


def test_api_connection(api_key: str, api_base: str, model: str, proxy_mode: str = "environment") -> Tuple[bool, str]:
    """Probe a provider model list without exposing credentials in errors."""

    base = api_base.rstrip("/")
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
    except requests.exceptions.RequestException as exc:
        return False, f"请求异常：{exc}"


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

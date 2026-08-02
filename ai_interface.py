import base64
import copy
import json
import mimetypes
import os
import time
import re
import requests  # type: ignore
from typing import Union, Dict, Optional, Any, List, Tuple, Callable, Set

from models import APIConfig
from config_loader import load_config
from services.model_capabilities import (
    ModelCapability,
    apply_reasoning_policy,
    remove_payload_path,
    resolve_model_capability,
)
from services.proxy_policy import should_bypass_environment_proxy
from summary_schema import (
    default_ai_summary,
    get_ai_summary,
    normalize_ai_summary,
    project_legacy_ai_summary,
)

_DEFAULT_TIMEOUT_SECONDS = 600
_DEFAULT_API_RETRY_ATTEMPTS = 3
_MAX_LOCAL_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
_NON_RETRIABLE_HTTP_STATUSES = {400, 401, 402, 403, 404, 422}
_QUOTA_ERROR_MARKERS = (
    "insufficient_user_quota",
    "insufficient quota",
    "quota exceeded",
    "balance is insufficient",
    "insufficient balance",
    "recharge",
    "credit balance",
    "billing quota",
    "余额不足",
    "额度不足",
    "余额/额度",
    "充值",
    "欠费",
)
_TRANSIENT_NETWORK_MARKERS = (
    "timeout",
    "timed out",
    "proxy",
    "disconnect",
    "connection reset",
    "connection aborted",
    "ssl eof",
    "eof occurred",
    "remote end closed connection",
    "temporarily unavailable",
)
_PAYLOAD_PARAMETER_ERROR_MARKERS = (
    "unsupported parameter",
    "not support",
    "not supported",
    "deprecated",
    "unknown parameter",
    "unrecognized parameter",
    "not allowed",
)


def _api_result(
    *,
    status: str,
    content: Any = None,
    error_kind: Optional[str] = None,
    http_status: Optional[int] = None,
    provider_code: Optional[str] = None,
    message: str = "",
    engine_type: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "error_kind": error_kind,
        "http_status": http_status,
        "provider_code": provider_code,
        "message": message,
        "content": content,
        "engine_type": engine_type,
        "finish_reason": finish_reason,
    }


def _extract_provider_error(response: Any) -> Dict[str, Any]:
    status_code = getattr(response, "status_code", None)
    provider_code = None
    message = ""
    raw_text = ""

    if response is None:
        return {
            "http_status": None,
            "provider_code": None,
            "message": "",
            "raw_text": "",
        }

    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        error_payload = payload.get("error", payload)
        if isinstance(error_payload, dict):
            provider_code = (
                error_payload.get("code")
                or error_payload.get("type")
                or error_payload.get("error_code")
            )
            message = str(error_payload.get("message") or error_payload.get("error") or "")
        elif error_payload is not None:
            message = str(error_payload)
        raw_text = str(payload)
    else:
        raw_text = str(getattr(response, "text", "") or "")
        message = raw_text

    return {
        "http_status": status_code,
        "provider_code": str(provider_code or ""),
        "message": message,
        "raw_text": raw_text,
    }


def _looks_like_quota_error(*parts: Any) -> bool:
    text = " ".join(str(part or "") for part in parts).casefold()
    return any(marker.casefold() in text for marker in _QUOTA_ERROR_MARKERS)


def _classify_http_error(response: Any) -> Tuple[str, Optional[int], Optional[str], str]:
    details = _extract_provider_error(response)
    status_code = details.get("http_status")
    provider_code = str(details.get("provider_code") or "")
    message = str(details.get("message") or details.get("raw_text") or "")

    if _looks_like_quota_error(provider_code, message, details.get("raw_text")):
        return "quota_exhausted", status_code, provider_code or None, message
    if status_code == 429 or (isinstance(status_code, int) and 500 <= status_code <= 599):
        return "retryable_http", status_code, provider_code or None, message
    if status_code in {400, 401, 402, 403, 404, 422}:
        return "fatal_config_or_auth", status_code, provider_code or None, message
    return "retryable_http", status_code, provider_code or None, message


def _classify_exception(exc: BaseException) -> Tuple[str, str]:
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ProxyError,
            requests.exceptions.SSLError,
        ),
    ):
        return "transient_network", str(exc)

    message = str(exc)
    message_folded = message.casefold()
    if any(marker in message_folded for marker in _TRANSIENT_NETWORK_MARKERS):
        return "transient_network", message
    return "invalid_response", message


def _is_payload_parameter_error(parameter_name: str, *parts: Any) -> bool:
    text = " ".join(str(part or "") for part in parts).casefold()
    return parameter_name.casefold() in text and any(marker in text for marker in _PAYLOAD_PARAMETER_ERROR_MARKERS)


def _is_payload_value_error(parameter_name: str, parameter_value: str, *parts: Any) -> bool:
    text = " ".join(str(part or "") for part in parts).casefold()
    return (
        parameter_name.casefold() in text
        and parameter_value.casefold() in text
        and any(marker in text for marker in _PAYLOAD_PARAMETER_ERROR_MARKERS)
    )


def _normalize_thinking_payload(value: Any) -> Optional[Dict[str, str]]:
    if isinstance(value, dict):
        thinking_type = str(value.get("type") or "").strip().lower()
        if thinking_type in {"enabled", "disabled"}:
            return {"type": thinking_type}
        return None

    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered.startswith("{"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        return _normalize_thinking_payload(parsed)
    if lowered in {"enabled", "enable", "on", "true", "yes", "1"}:
        return {"type": "enabled"}
    if lowered in {"disabled", "disable", "off", "false", "no", "0"}:
        return {"type": "disabled"}
    return None


def _apply_optional_api_payload_params(payload: Dict[str, Any], api_config: APIConfig, logger: Any = None) -> None:
    reasoning_effort = str(api_config.get("reasoning_effort") or "").strip()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    thinking_payload = _normalize_thinking_payload(api_config.get("thinking"))
    if thinking_payload:
        payload["thinking"] = thinking_payload
    elif api_config.get("thinking") not in (None, "") and logger:
        logger.warning("Ignoring invalid API thinking config; expected enabled/disabled or {'type': ...}.")


def _config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "enabled", "enable"}


def _configured_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _extract_response_output_text(response_data: Dict[str, Any]) -> str:
    output_text = response_data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    chunks: List[str] = []
    output_items = response_data.get("output")
    if isinstance(output_items, list):
        for output_item in output_items:
            if not isinstance(output_item, dict):
                continue
            content_items = output_item.get("content")
            if not isinstance(content_items, list):
                continue
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                    continue
                output_text_part = content_item.get("output_text")
                if isinstance(output_text_part, str):
                    chunks.append(output_text_part)
    return "".join(chunks)


def _responses_finish_reason(response_data: Dict[str, Any]) -> str:
    status = str(response_data.get("status") or "").strip()
    if status == "completed":
        return "stop"
    if status:
        return status
    return "stop"


def _convert_chat_content_to_responses_content(content: Any) -> Any:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content or "")}]

    converted: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "text":
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
            continue
        if item_type == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            else:
                url = str(image_url or "").strip()
            if url:
                converted.append({"type": "input_image", "image_url": url})
            continue
        if item_type == "input_text":
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
            continue
        if item_type == "input_image":
            image_url = str(item.get("image_url") or item.get("url") or "").strip()
            if image_url:
                converted.append({"type": "input_image", "image_url": image_url})
            continue
        if item_type == "input_file":
            file_item: Dict[str, Any] = {"type": "input_file"}
            for key in ("file_id", "file_url", "file_data", "filename"):
                if item.get(key):
                    file_item[key] = str(item.get(key))
            if len(file_item) > 1:
                converted.append(file_item)
    return converted or [{"type": "input_text", "text": ""}]


def build_chat_completions_payload(
    prompt: str,
    api_config: APIConfig,
    system_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    response_format: str,
    user_content: Any = None,
    logger: Any = None,
    capability: Optional[ModelCapability] = None,
) -> Dict[str, Any]:
    capability = capability or resolve_model_capability(api_config)
    normalized_user_content = _normalize_user_message_content(prompt, user_content, logger=logger)
    token_limit = _configured_positive_int(api_config.get("max_completion_tokens"), max_tokens)
    token_limit = _configured_positive_int(api_config.get("max_tokens"), token_limit)
    payload: Dict[str, Any] = {
        "model": api_config.get("model") or "",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": normalized_user_content},
        ],
        "temperature": temperature,
        "max_tokens": token_limit,
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}
    apply_reasoning_policy(payload, api_config, capability, logger=logger)
    return payload


def build_responses_payload(
    prompt: str,
    api_config: APIConfig,
    system_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    response_format: str,
    user_content: Any = None,
    logger: Any = None,
    capability: Optional[ModelCapability] = None,
) -> Dict[str, Any]:
    capability = capability or resolve_model_capability(api_config)
    normalized_user_content = _normalize_user_message_content(prompt, user_content, logger=logger)
    max_output_tokens = _configured_positive_int(api_config.get("max_output_tokens"), max_tokens)
    payload: Dict[str, Any] = {
        "model": api_config.get("model") or "",
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": _convert_chat_content_to_responses_content(normalized_user_content),
            },
        ],
        "max_output_tokens": max_output_tokens,
    }
    if not _config_bool(api_config.get("omit_temperature_when_reasoning")):
        payload["temperature"] = temperature
    if response_format == "json":
        payload["text"] = {"format": {"type": "json_object"}}
    apply_reasoning_policy(payload, api_config, capability, logger=logger)
    return payload


def parse_chat_completions_response(response_data: Dict[str, Any]) -> Tuple[str, str]:
    content = response_data["choices"][0]["message"]["content"]
    finish_reason = response_data["choices"][0].get("finish_reason", "stop")
    return content, finish_reason


def parse_responses_response(response_data: Dict[str, Any]) -> Tuple[str, str]:
    return _extract_response_output_text(response_data), _responses_finish_reason(response_data)


def _format_success_result(content: Any, response_format: str, response: Any, finish_reason: str, logger: Any = None) -> Dict[str, Any]:
    if response_format == "json":
        parsed_content = _smart_json_parser(str(content or ""))
        if parsed_content is not None:
            return _api_result(
                status="success",
                content=parsed_content,
                http_status=getattr(response, "status_code", None),
                finish_reason=finish_reason,
            )

        corrected_content = _auto_correct_json(str(content or ""))
        if corrected_content is not None:
            if corrected_content.get("error") == "无法修复JSON格式":
                message = "200 response but JSON auto-correction could not recover a valid object"
                if logger:
                    logger.error(message)
                    logger.debug(f"AI返回内容: {str(content)[:500]}...")
                return _api_result(
                    status="failed",
                    error_kind="invalid_response",
                    http_status=getattr(response, "status_code", None),
                    message=message,
                    finish_reason=finish_reason,
                )
            if logger:
                logger.info("通过自动纠错成功修复JSON")
            return _api_result(
                status="success",
                content=corrected_content,
                http_status=getattr(response, "status_code", None),
                finish_reason=finish_reason,
            )

        message = "200 response but JSON content is empty or malformed"
        if logger:
            logger.error(message)
            logger.debug(f"AI返回内容: {str(content)[:500]}...")
        return _api_result(
            status="failed",
            error_kind="invalid_response",
            http_status=getattr(response, "status_code", None),
            message=message,
        )

    return _api_result(
        status="success",
        content=content,
        http_status=getattr(response, "status_code", None),
        finish_reason=finish_reason,
    )


def _post_with_proxy_mode(api_url: str, *, api_config: APIConfig, **kwargs: Any) -> Any:
    if should_bypass_environment_proxy(api_config):
        with requests.Session() as session:
            session.trust_env = False
            return session.post(api_url, **kwargs)
    return requests.post(api_url, **kwargs)


def _default_core_variables() -> Dict[str, List[str]]:
    return project_legacy_ai_summary(default_ai_summary())["type_specific_details"]["core_variables"]


def _default_type_specific_details() -> Dict[str, Any]:
    return project_legacy_ai_summary(default_ai_summary())["type_specific_details"]


def _normalize_type_specific_details(payload: Any) -> Dict[str, Any]:
    canonical = normalize_ai_summary({"type_specific_details": payload})
    return project_legacy_ai_summary(canonical)["type_specific_details"]


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _response_error_details(response: Any, limit: int = 500) -> str:
    if response is None:
        return "HTTP错误 ?"

    details = f"HTTP错误 {getattr(response, 'status_code', '?')}"
    try:
        error_response = response.json()
        details += f"，响应: {str(error_response)[:limit]}"
    except Exception:
        response_text = getattr(response, "text", "") or "无响应内容"
        details += f"，响应文本: {response_text[:limit]}"
    return details


def _is_non_retriable_http_response(response: Any) -> bool:
    status_code = getattr(response, "status_code", None)
    return status_code in _NON_RETRIABLE_HTTP_STATUSES


def _load_api_runtime_settings() -> Tuple[int, int]:
    timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    retry_attempts = _DEFAULT_API_RETRY_ATTEMPTS

    try:
        config = load_config()
    except Exception:
        return timeout_seconds, retry_attempts

    api_parameters = config.get("API_Parameters", {}) or {}
    performance = config.get("Performance", {}) or {}
    timeout_seconds = _coerce_positive_int(
        api_parameters.get("timeout_seconds", timeout_seconds),
        timeout_seconds,
    )
    retry_attempts = _coerce_positive_int(
        performance.get("api_retry_attempts", retry_attempts),
        retry_attempts,
    )
    return timeout_seconds, retry_attempts


def _encode_local_image_as_data_url(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        image_size = os.path.getsize(path)
    except OSError:
        return None
    if image_size <= 0 or image_size > _MAX_LOCAL_IMAGE_INPUT_BYTES:
        return None

    try:
        with open(path, "rb") as handle:
            image_bytes = handle.read(_MAX_LOCAL_IMAGE_INPUT_BYTES + 1)
    except OSError:
        return None

    if not image_bytes or len(image_bytes) > _MAX_LOCAL_IMAGE_INPUT_BYTES:
        return None

    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _encode_local_pdf_as_data_url(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            pdf_bytes = handle.read()
    except Exception:
        return None
    if not pdf_bytes:
        return None
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    return f"data:application/pdf;base64,{encoded}"


def _normalize_user_message_content(prompt: str, user_content: Any, logger: Any = None) -> Any:
    if not isinstance(user_content, list):
        return prompt

    normalized: List[Dict[str, Any]] = []
    for item in user_content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "text":
            text = str(item.get("text") or "")
            if text:
                normalized.append({"type": "text", "text": text})
            continue
        if item_type == "local_image_path":
            path = str(item.get("path") or "").strip()
            data_url = _encode_local_image_as_data_url(path)
            if not data_url:
                if logger:
                    logger.warning(f"Skipping missing or unreadable local image input: {path}")
                continue
            normalized.append({"type": "image_url", "image_url": {"url": data_url}})
            continue
        if item_type == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and image_url.get("url"):
                normalized.append({"type": "image_url", "image_url": {"url": str(image_url.get("url"))}})
            elif isinstance(image_url, str) and image_url.strip():
                normalized.append({"type": "image_url", "image_url": {"url": image_url.strip()}})
            continue
        if item_type == "local_pdf_path":
            path = str(item.get("path") or "").strip()
            data_url = _encode_local_pdf_as_data_url(path)
            if not data_url:
                if logger:
                    logger.warning(f"Skipping missing or unreadable local PDF input: {path}")
                continue
            normalized.append(
                {
                    "type": "input_file",
                    "filename": os.path.basename(path) or "document.pdf",
                    "file_data": data_url,
                }
            )
            continue
        if item_type == "input_file":
            file_item: Dict[str, Any] = {"type": "input_file"}
            for key in ("file_id", "file_url", "file_data", "filename"):
                if item.get(key):
                    file_item[key] = str(item.get(key))
            if len(file_item) > 1:
                normalized.append(file_item)
            continue

    if not normalized:
        return prompt

    has_text = any(item.get("type") == "text" for item in normalized)
    if not has_text and prompt:
        normalized.insert(0, {"type": "text", "text": prompt})
    return normalized


def _call_ai_api_detailed(
    prompt: str,
    api_config: APIConfig,
    system_prompt: str,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    response_format: str = "json",
    logger: Any = None,
    user_content: Any = None,
    retry_attempts: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Call a configured AI API transport and retain failure details."""
    try:
        api_key = api_config.get('api_key') or ''
        model_name = api_config.get('model') or ''
        api_base = api_config.get('api_base', 'https://api.openai.com/v1') or 'https://api.openai.com/v1'
        capability = resolve_model_capability(api_config)

        if not api_key or not model_name:
            message = "API config is missing api_key or model"
            if logger:
                logger.error(message)
            return _api_result(status="failed", error_kind="fatal_config_or_auth", message=message)

        configured_timeout_seconds, configured_retries = _load_api_runtime_settings()
        request_timeout_seconds = (
            _coerce_positive_int(timeout_seconds, configured_timeout_seconds)
            if timeout_seconds is not None
            else configured_timeout_seconds
        )
        max_retries = (
            _coerce_positive_int(retry_attempts, configured_retries)
            if retry_attempts is not None
            else configured_retries
        )
        endpoint_suffix = "responses" if capability.endpoint_type == "responses" else "chat/completions"
        api_url = f"{api_base.rstrip('/')}/{endpoint_suffix}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if capability.endpoint_type == "responses":
            payload = build_responses_payload(
                prompt,
                api_config,
                system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                user_content=user_content,
                logger=logger,
                capability=capability,
            )
            response_parser = parse_responses_response
        else:
            payload = build_chat_completions_payload(
                prompt,
                api_config,
                system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                user_content=user_content,
                logger=logger,
                capability=capability,
            )
            response_parser = parse_chat_completions_response

        response = None
        last_failure = _api_result(status="failed", error_kind="invalid_response", message="API call did not run")
        attempt = 0
        removed_compat_params: Set[Any] = set()
        while attempt < max_retries:
            attempt += 1
            try:
                final_payload = copy.deepcopy(payload)
                if 'aihubmix.com' in api_base.lower() and logger:
                    logger.info(f"调用AIHubMix API，模型: {final_payload['model']}")

                response = _post_with_proxy_mode(
                    api_url,
                    api_config=api_config,
                    headers=headers,
                    json=final_payload,
                    timeout=request_timeout_seconds,
                )
                response.raise_for_status()

                try:
                    response_data = response.json()
                    content, finish_reason = response_parser(response_data)
                except Exception as exc:
                    message = f"Malformed API response: {exc}"
                    if logger:
                        logger.error(message)
                    return _api_result(
                        status="failed",
                        error_kind="invalid_response",
                        http_status=getattr(response, "status_code", None),
                        message=message,
                    )

                formatted = _format_success_result(content, response_format, response, finish_reason, logger=logger)
                if (
                    formatted.get("status") == "failed"
                    and formatted.get("error_kind") == "invalid_response"
                    and response_format == "json"
                    and attempt < max_retries
                ):
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(
                            f"API returned malformed JSON; retrying structured request in {wait_time:.1f}s..."
                        )
                    time.sleep(wait_time)
                    last_failure = formatted
                    continue

                return formatted

            except requests.exceptions.HTTPError:
                error_kind, http_status, provider_code, message = _classify_http_error(response)
                last_failure = _api_result(
                    status="failed",
                    error_kind=error_kind,
                    http_status=http_status,
                    provider_code=provider_code,
                    message=message or _response_error_details(response, limit=500),
                )
                if (
                    capability.reasoning_param_style == "chat_reasoning"
                    and ("reasoning", "display") not in removed_compat_params
                    and _is_payload_parameter_error("display", provider_code, message, last_failure["message"])
                    and remove_payload_path(payload, ("reasoning", "display"))
                ):
                    removed_compat_params.add(("reasoning", "display"))
                    attempt -= 1
                    if logger:
                        logger.warning("API rejected reasoning.display, retrying once without it.")
                    continue

                if (
                    capability.reasoning_param_style == "deepseek_thinking"
                    and "reasoning_effort:max_to_high" not in removed_compat_params
                    and str(payload.get("reasoning_effort") or "").strip().casefold() == "max"
                    and (
                        _is_payload_parameter_error("reasoning_effort", provider_code, message, last_failure["message"])
                        or _is_payload_value_error("reasoning_effort", "max", provider_code, message, last_failure["message"])
                    )
                ):
                    removed_compat_params.add("reasoning_effort:max_to_high")
                    payload["reasoning_effort"] = "high"
                    attempt -= 1
                    if logger:
                        logger.warning("API rejected reasoning_effort=max, retrying once with high.")
                    continue

                compat_payload_keys = [
                    "temperature",
                    "top_p",
                    "response_format",
                    "reasoning_effort",
                    "thinking",
                    "reasoning",
                ]
                if capability.endpoint_type == "responses":
                    compat_payload_keys = ["temperature", "top_p", "text", "reasoning"]

                for payload_key in compat_payload_keys:
                    if (
                        payload_key in payload
                        and payload_key not in removed_compat_params
                        and _is_payload_parameter_error(payload_key, provider_code, message, last_failure["message"])
                    ):
                        removed_compat_params.add(payload_key)
                        payload.pop(payload_key, None)
                        attempt -= 1
                        if logger:
                            logger.warning(
                                f"API rejected payload parameter '{payload_key}', retrying once without it."
                            )
                        break
                else:
                    payload_key = ""

                if payload_key:
                    continue

                if error_kind in {"quota_exhausted", "fatal_config_or_auth"}:
                    if logger:
                        logger.error(f"API调用不可重试失败 ({error_kind}): {last_failure['message']}")
                    return last_failure

                if attempt < max_retries:
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(f"{_response_error_details(response, limit=200)}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue

                if logger:
                    logger.error(f"API调用最终失败: {last_failure['message']}")
                return last_failure

            except Exception as exc:
                response_status = getattr(response, "status_code", None)
                if isinstance(response_status, int) and response_status >= 400:
                    error_kind, http_status, provider_code, message = _classify_http_error(response)
                else:
                    error_kind, message = _classify_exception(exc)
                    http_status = response_status
                    provider_code = None

                last_failure = _api_result(
                    status="failed",
                    error_kind=error_kind,
                    http_status=http_status,
                    provider_code=provider_code,
                    message=message,
                )
                if error_kind in {"quota_exhausted", "fatal_config_or_auth", "invalid_response"}:
                    if logger:
                        logger.error(f"API调用不可重试失败 ({error_kind}): {message}")
                    return last_failure

                if attempt < max_retries:
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(f"API调用失败 ({error_kind}): {message}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue

                if logger:
                    logger.error(f"API调用最终失败 ({error_kind}): {message}")
                return last_failure

        return last_failure

    except Exception as exc:
        if logger:
            logger.error(f"调用API失败: {exc}")
        error_kind, message = _classify_exception(exc)
        return _api_result(status="failed", error_kind=error_kind, message=message)


def _call_ai_api(prompt: str, api_config: APIConfig, system_prompt: str, max_tokens: int = 4000,
                 temperature: float = 0.3, response_format: str = "json", logger: Any = None,
                 user_content: Any = None, retry_attempts: Optional[int] = None,
                 timeout_seconds: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Backward-compatible wrapper: existing callers receive parsed content only.
    Use _call_ai_api_detailed when transport failure kind is needed.
    """
    result = _call_ai_api_detailed(
        prompt,
        api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        logger=logger,
        user_content=user_content,
        retry_attempts=retry_attempts,
        timeout_seconds=timeout_seconds,
    )
    if result.get("status") == "success":
        return result.get("content")
    return None


def _call_ai_api_text_detailed(
    prompt: str,
    api_config: APIConfig,
    system_prompt: str,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    logger: Any = None,
    user_content: Any = None,
    retry_attempts: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Call the chat-completions API for text content and return full metadata.

    Returns a dict with content, finish_reason, and raw response metadata.
    Does NOT change _call_ai_api legacy return shape.
    """
    result = _call_ai_api_detailed(
        prompt,
        api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="text",
        logger=logger,
        user_content=user_content,
        retry_attempts=retry_attempts,
        timeout_seconds=timeout_seconds,
    )
    if result.get("status") == "success":
        return {
            "content": result.get("content", ""),
            "finish_reason": result.get("finish_reason", "stop"),
            "http_status": result.get("http_status"),
        }
    return {
        "content": None,
        "finish_reason": None,
        "http_status": result.get("http_status"),
        "error_kind": result.get("error_kind"),
        "message": result.get("message", ""),
    }


def _smart_json_parser(content: str) -> Optional[Dict[str, Any]]:
    """
    智能JSON解析器，尝试多种方式解析JSON
    简化逻辑，提高可靠性和性能

    Args:
        content: AI返回的原始内容

    Returns:
        解析后的字典，失败返回None
    """
    if not content:
        return None

    # 清理内容，移除可能影响解析的前后空白
    content_stripped: str = content.strip()
    if not content_stripped:
        return None

    # 变量类型注解
    strategy_results: Optional[Dict[str, Any]] = None

    # 解析策略按优先级排序
    def parse_strategy_1() -> Optional[Dict[str, Any]]:
        return json.loads(content) if content else None
    
    def parse_strategy_2() -> Optional[Dict[str, Any]]:
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL | re.IGNORECASE)
        return json.loads(match.group(1)) if match and match.group(1) else None
    
    def parse_strategy_3() -> Optional[Dict[str, Any]]:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        return json.loads(match.group(0)) if match and match.group(0) else None
    
    def parse_strategy_4() -> Optional[Dict[str, Any]]:
        start = content.find('{')
        end = content.rfind('}')
        return json.loads(content[start:end+1]) if start != -1 and end != -1 and end >= start else None
    
    parse_strategies: List[Callable[[], Optional[Dict[str, Any]]]] = [
        parse_strategy_1,
        parse_strategy_2,
        parse_strategy_3,
        parse_strategy_4
    ]

    for strategy in parse_strategies:
        try:
            strategy_outcome = strategy()
            if strategy_outcome is not None:
                # 解析成功，不需要打印，因为这是正常流程
                return strategy_outcome
        except (AttributeError, json.JSONDecodeError, ValueError):
            # AttributeError: regex没有匹配到内容
            # JSONDecodeError: JSON格式错误
            # ValueError: 其他解析错误
            continue
        except Exception:
            # 意外错误，也不需要打印，避免日志噪音
            continue

    return strategy_results


def _auto_correct_json(content: str) -> Optional[Dict[str, Any]]:
    """
    自动纠错JSON，尝试修复常见的JSON格式错误
    
    Args:
        content: AI返回的原始内容
        
    Returns:
        修复后的字典，失败返回None
    """
    # 添加content None安全检查
    if not content:
        return None
    
    try:
        # 提取可能的JSON字符串
        json_str: Optional[str] = _extract_json_string(content)
        if not json_str:
            return None
        
        # 常见JSON错误修复
        corrected_json: str = _fix_common_json_errors(json_str)
        
        # 尝试解析修复后的JSON
        try:
            return json.loads(corrected_json)
        except json.JSONDecodeError:
            # 修复失败，不需要打印，因为还有后续处理
            # 尝试更激进的修复
            aggressively_fixed: str = _aggressive_json_fix(corrected_json)
            try:
                return json.loads(aggressively_fixed)
            except json.JSONDecodeError:
                return None

    except Exception:
        # 纠错过程出错，不需要打印，避免日志噪音
        return None


def _extract_json_string(content: str) -> Optional[str]:
    """
    从内容中提取JSON字符串
    
    Args:
        content: AI返回的原始内容
        
    Returns:
        提取的JSON字符串
    """
    # 添加content None安全检查
    if not content:
        return None
    
    # 尝试多种方法提取JSON字符串
    
    # 方法1：查找JSON代码块
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        return json_match.group(1)
    
    # 方法2：查找JSON对象
    json_obj_match = re.search(r'(\{.*\})', content, re.DOTALL)
    if json_obj_match:
        return json_obj_match.group(1)
    
    # 方法3：查找第一个{和最后一个}之间的内容
    first_brace = content.find('{')
    last_brace = content.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return content[first_brace:last_brace+1]
    
    # 如果都失败了，返回原内容
    return content if content else ""


def _fix_common_json_errors(json_str: str) -> str:
    """
    修复常见的JSON格式错误
    
    Args:
        json_str: 原始JSON字符串
        
    Returns:
        修复后的JSON字符串
    """
    # 修复1：移除注释
    json_str = re.sub(r'//.*', '', json_str)  # 移除单行注释
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)  # 移除多行注释
    
    # 修复2：移除尾随逗号
    json_str = re.sub(r',\s*}', '}', json_str)  # 对象中的尾随逗号
    json_str = re.sub(r',\s*]', ']', json_str)  # 数组中的尾随逗号
    
    # 修复3：修复单引号为双引号
    # 这个修复比较复杂，需要确保不替换内容中的单引号
    # 简单处理：只替换键名和字符串值的单引号
    json_str = re.sub(r"(\w+)\s*:\s*'([^']*)'", r'"\1": "\2"', json_str)
    
    # 修复4：修复未引用的键名
    json_str = re.sub(r'(\w+)\s*:', r'"\1":', json_str)
    
    # 修复5：修复换行符问题
    json_str = re.sub(r'[\n\r]+', ' ', json_str)  # 将换行符替换为空格
    json_str = re.sub(r'\s+', ' ', json_str)  # 合并多个空格
    
    # 变量类型注解
    corrected_json: str = json_str.strip()
    return corrected_json


def _aggressive_json_fix(json_str: str) -> str:
    """
    更激进的JSON修复方法 - 修复版
    
    修复说明：
    - 原正则表达式 [^\"\\'\\{\\}\\[\\],]+ 无法匹配包含逗号、引号、大括号的值
    - 学术论文摘要必然包含这些字符，导致大规模失败
    - 新实现采用更智能的解析策略，能正确处理嵌套结构和特殊字符
    
    Args:
        json_str: 原始JSON字符串
        
    Returns:
        修复后的JSON字符串
    """
    try:
        # 如果看起来像是一个对象，尝试修复基本结构
        if json_str.strip().startswith('{'):
            # 方法1：尝试查找所有键值对（改进的正则表达式）
            # 使用更健壮的匹配策略，能处理包含特殊字符的值
            pairs: List[Tuple[str, str]] = []
            
            # 策略A：尝试匹配常见的键值对模式
            # 匹配：键: 值（值可以是字符串、数字、布尔值）
            simple_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*("(?:\\.|[^"\\])*"|\d+|true|false|null)', json_str)
            if simple_pairs:
                pairs.extend(simple_pairs)
            
            # 策略B：如果策略A失败，尝试更宽松的匹配
            if not pairs:
                # 匹配：键: "值"（值可以包含转义引号）
                quoted_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*"((?:\\.|[^"\\])*)"', json_str)
                if quoted_pairs:
                    pairs.extend(quoted_pairs)
            
            # 策略C：如果以上都失败，尝试提取所有可能的键值对（最宽松）
            if not pairs:
                # 匹配：键: 值（值到下一个键或结束）
                loose_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*([^,\}]+)', json_str)
                if loose_pairs:
                    pairs.extend(loose_pairs)
            
            if pairs:
                # 重建JSON对象
                fixed_pairs: List[str] = []
                for key, value in pairs:
                    key = key.strip()
                    value = value.strip()
                    
                    # 确保键被引号包围
                    if not (key.startswith('"') and key.endswith('"')):
                        key = f'"{key}"'
                    
                    # 如果值看起来像字符串（包含字母、中文、特殊字符），确保它被引号包围
                    if (not (value.startswith('"') and value.endswith('"')) and 
                        not value in ('true', 'false', 'null') and 
                        not re.match(r'^\d+(\.\d+)?$', value)):
                        # 转义值中的引号
                        value = value.replace('"', '\\"')
                        value = f'"{value}"'
                    
                    fixed_pairs.append(f'{key}: {value}')
                
                result_json = '{' + ', '.join(fixed_pairs) + '}'
                # 验证生成的JSON是否有效
                try:
                    json.loads(result_json)
                    return result_json
                except json.JSONDecodeError:
                    # 如果无效，继续尝试其他方法
                    pass
        
        # 如果看起来像是一个数组，尝试修复基本结构
        elif json_str.strip().startswith('['):
            # 尝试匹配数组元素（支持字符串和简单值）
            elements: List[str] = []
            
            # 策略A：匹配引号包围的字符串
            quoted_elements = re.findall(r'"((?:\\.|[^"\\])*)"', json_str)
            if quoted_elements:
                elements.extend(quoted_elements)
            
            # 策略B：如果策略A失败，尝试匹配非引号值
            if not elements:
                simple_elements = re.findall(r'\[\s*([^,\]]+)\s*\]', json_str)
                if simple_elements:
                    elements.extend(simple_elements)
            
            if elements:
                # 重建JSON数组
                fixed_elements: List[str] = []
                for elem in elements:
                    elem = elem.strip()
                    if elem:
                        # 如果元素包含字母或特殊字符，用引号包围
                        if re.search(r'[a-zA-Z\u4e00-\u9fa5]', elem):
                            elem = elem.replace('"', '\\"')
                            fixed_elements.append(f'"{elem}"')
                        else:
                            fixed_elements.append(elem)
                
                result_json = '[' + ', '.join(fixed_elements) + ']'
                # 验证生成的JSON是否有效
                try:
                    json.loads(result_json)
                    return result_json
                except json.JSONDecodeError:
                    # 如果无效，继续尝试其他方法
                    pass
        
    except Exception:
        # 激进修复出错，不需要打印
        pass

    # 如果所有修复都失败，返回一个最小的有效JSON
    # 变量类型注解
    result: str = '{"error": "无法修复JSON格式", "original_content": ' + json.dumps(json_str[:200]) + '}'
    return result



def get_concept_profile(prompt: str, api_config: APIConfig, logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    调用AI生成概念配置文件

    Args:
        prompt: 概念学习提示词
        api_config: API配置字典
        logger: 日志记录器实例
        config: 配置字典（可选）

    Returns:
        概念配置字典，失败返回None
    """
    # 读取API参数配置
    try:
        if config:
            api_params = config.get('API_Parameters', {}) or {}  # type: ignore
            max_tokens = int(api_params.get('concept_max_tokens', 4000))  # type: ignore
            temperature = float(api_params.get('concept_temperature', 0.3))  # type: ignore
        else:
            max_tokens = 4000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取概念分析API参数配置失败，使用默认值: {e}")
        max_tokens = 4000
        temperature = 0.3

    # 使用统一的API调用函数
    system_prompt = "你是一位学术研究专家，专门研究概念的历史发展和理论演化。请基于提供的种子论文，深入分析并创建一个关于指定概念的全面学习笔记。"
    return _call_ai_api(prompt, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json", logger=logger)


def get_concept_analysis(prompt: str, api_config: APIConfig, logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    调用AI进行概念分析

    Args:
        prompt: 概念分析提示词
        api_config: API配置字典
        logger: 日志记录器实例
        config: 配置字典（可选）

    Returns:
        概念分析字典，失败返回None
    """
    # 读取API参数配置
    try:
        if config:
            api_params = config.get('API_Parameters', {}) or {}  # type: ignore
            max_tokens = int(api_params.get('concept_max_tokens', 4000))  # type: ignore
            temperature = float(api_params.get('concept_temperature', 0.3))  # type: ignore
        else:
            max_tokens = 4000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取概念分析API参数配置失败，使用默认值: {e}")
        max_tokens = 4000
        temperature = 0.3

    # 使用统一的API调用函数
    system_prompt = "你是一位专门研究概念的学术分析专家。请基于提供的概念学习笔记，对当前论文进行深度分析，评估其在该概念发展历程中的地位和贡献。"
    return _call_ai_api(prompt, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json", logger=logger)


class ContextLengthExceededError(Exception):
    """上下文长度超限错误，用于智能切换到备用引擎"""
    pass


# 引擎映射表，统一引擎名称和日志术语
engine_map = {
    'primary': {
        'name': '主阅读引擎',
        'short_name': '主引擎'
    },
    'backup': {
        'name': '备用阅读引擎',
        'short_name': '备用引擎'
    }
}

def get_summary_from_ai_detailed(
    prompt_text: str,
    primary_api_config: APIConfig,
    backup_api_config: APIConfig,
    engine_type: str = 'primary',
    logger: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None,
    user_content: Any = None,
    retry_attempts: Optional[int] = None,
) -> Dict[str, Any]:
    """Stage-1 reader call that preserves API failure classification."""
    if ('dummy' in (primary_api_config.get('api_key') or '') or
        'dummy' in (backup_api_config.get('api_key') or '')):
        return _api_result(
            status="success",
            engine_type=engine_type,
            content=normalize_ai_summary(
                {
                    'routing': {
                        'paper_type': None,
                        'paper_subtype_raw': None,
                        'paper_subtype_normalized': None,
                        'classification_status': 'uncertain',
                        'route_confidence': 'low',
                        'classification_rationale': None,
                        'secondary_candidates': [],
                    },
                    'core_analysis': {
                        'summary': 'This is a dummy summary.',
                        'key_points': ['Dummy key point 1', 'Dummy key point 2'],
                        'methodology': 'Dummy methodology.',
                        'findings': 'Dummy findings.',
                        'conclusions': 'Dummy conclusions.',
                        'relevance': 'Dummy relevance.',
                        'limitations': 'Dummy limitations.',
                        'theoretical_framework': None,
                        'research_gap': None,
                        'future_research_directions': [],
                    },
                    'specialized_details': {
                        'empirical': None,
                        'review': None,
                        'conceptual': None,
                    },
                }
            ),
        )

    if not prompt_text or not prompt_text.strip():
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message="Prompt text is empty",
            engine_type=engine_type,
        )
    if engine_type not in engine_map:
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message=f"Unknown reader engine type: {engine_type}",
            engine_type=engine_type,
        )

    api_config = primary_api_config if engine_type == 'primary' else backup_api_config
    engine_name = engine_map[engine_type]['name']
    api_key = api_config.get('api_key') or ''
    model_name = api_config.get('model') or ''
    api_base = api_config.get('api_base') or 'https://api.openai.com/v1'

    if not api_key.strip():
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message=f"{engine_name} api_key is empty",
            engine_type=engine_type,
        )
    if not model_name.strip():
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message=f"{engine_name} model is empty",
            engine_type=engine_type,
        )
    if len(prompt_text) > 10000000:
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message=f"Prompt text is too long: {len(prompt_text)} characters",
            engine_type=engine_type,
        )

    runtime_config = config
    if runtime_config is None:
        try:
            runtime_config = load_config('config.ini')
        except Exception:
            runtime_config = None

    try:
        if runtime_config:
            api_params = runtime_config.get('API_Parameters', {}) or {}  # type: ignore[union-attr]
            if engine_type == 'primary':
                max_tokens = int(api_params.get('primary_max_tokens', 3000))
                temperature = float(api_params.get('primary_temperature', 0.3))
            else:
                max_tokens = int(api_params.get('backup_max_tokens', 8192))
                temperature = float(api_params.get('backup_temperature', 0.3))
        else:
            max_tokens = 3000
            temperature = 0.3
    except (ValueError, TypeError) as exc:
        if logger:
            logger.warning(f"Failed to read API parameters, using defaults: {exc}")
        max_tokens = 3000
        temperature = 0.3

    try:
        with open('prompts/prompt_system_analyze.txt', 'r', encoding='utf-8') as handle:
            system_prompt = handle.read()
    except Exception as exc:
        if logger:
            logger.warning(f"Unable to load system prompt, using default: {exc}")
        system_prompt = (
            "You are an academic literature analysis expert. Analyze the paper text "
            "and return a structured JSON summary."
        )

    request_api_config: APIConfig = {
        **api_config,
        'api_key': api_key,
        'model': model_name,
        'api_base': api_base,
    }

    detailed = _call_ai_api_detailed(
        prompt_text,
        request_api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
        user_content=user_content,
        retry_attempts=retry_attempts,
    )
    detailed["engine_type"] = engine_type

    if detailed.get("status") != "success":
        return detailed

    ai_response = detailed.get("content")
    if isinstance(ai_response, dict):
        detailed["content"] = normalize_ai_summary(ai_response)
        return detailed
    if ai_response:
        if logger:
            logger.warning("AI returned non-dict content, trying manual extraction")
        detailed["content"] = _extract_summary_manually(ai_response)
        return detailed
    return _api_result(
        status="failed",
        error_kind="invalid_response",
        message="AI response was empty",
        engine_type=engine_type,
    )


def get_summary_from_ai_with_fallback(prompt_text: str, primary_api_config: APIConfig, backup_api_config: APIConfig,
                                      logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None,
                                      user_content: Any = None, return_detailed: bool = False,
                                      disable_engine_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                                      is_engine_disabled_callback: Optional[Callable[[str], bool]] = None,
                                      skip_engines: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Stage-1 reader scheduler. Transient failures alternate engines:
    primary#1 -> backup#1 -> primary#2 -> backup#2. Quota/balance failures
    disable that engine for the current caller-managed retry round.
    """
    _, attempt_budget = _load_api_runtime_settings()
    attempt_budget = max(1, attempt_budget)
    skip_engine_set = set(skip_engines or set())
    engine_order = ["primary", "backup"]
    remaining = {engine: attempt_budget for engine in engine_order}
    last_result: Optional[Dict[str, Any]] = None

    def configured(engine: str) -> bool:
        cfg = primary_api_config if engine == "primary" else backup_api_config
        return bool(str(cfg.get("api_key") or "").strip() and str(cfg.get("model") or "").strip())

    while True:
        made_attempt = False
        for engine in engine_order:
            if engine in skip_engine_set:
                continue
            if remaining.get(engine, 0) <= 0:
                continue
            if not configured(engine):
                remaining[engine] = 0
                continue
            if is_engine_disabled_callback and is_engine_disabled_callback(engine):
                remaining[engine] = 0
                continue

            made_attempt = True
            remaining[engine] -= 1
            if logger:
                logger.info(f"Stage 1 reader attempt: {engine}#{attempt_budget - remaining[engine]}")

            result = get_summary_from_ai_detailed(
                prompt_text,
                primary_api_config,
                backup_api_config,
                engine_type=engine,
                logger=logger,
                config=config,
                user_content=user_content,
                retry_attempts=1,
            )
            last_result = result
            if result.get("status") == "success":
                return result if return_detailed else result.get("content")

            error_kind = str(result.get("error_kind") or "")
            if error_kind == "quota_exhausted":
                remaining[engine] = 0
                if disable_engine_callback:
                    disable_engine_callback(engine, result)
                elif logger:
                    label = "主引擎" if engine == "primary" else "备用引擎"
                    logger.warning(f"{label}余额/额度不足，本轮自动跳过。")
                continue
            if error_kind in {"fatal_config_or_auth", "invalid_response"}:
                remaining[engine] = 0
                continue

        if not made_attempt:
            break

    if last_result is None:
        last_result = _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message="No configured reader engine was available",
        )
    return last_result if return_detailed else None


def get_summary_from_ai(prompt_text: str, primary_api_config: APIConfig, backup_api_config: APIConfig,
                       engine_type: str = 'primary', logger: Optional[Any] = None,
                       config: Optional[Dict[str, Any]] = None, user_content: Any = None) -> Optional[Dict[str, Any]]:
    """
    调用AI API并返回结构化摘要（带重试机制和429错误处理）

    Args:
        prompt_text: 完整的提示词文本
        primary_api_config: 主引擎API配置字典
        backup_api_config: 备用引擎API配置字典
        engine_type: 引擎类型 ('primary' 或 'backup')
        logger: 日志记录器实例（可选）

    Returns:
        Optional[Dict[str, Any]]: 结构化摘要，如果调用失败则返回None

    Raises:
        ValueError: 当输入参数无效时
        requests.RequestException: 当API调用失败时
    """
    if ('dummy' in (primary_api_config.get('api_key') or '') or 
        'dummy' in (backup_api_config.get('api_key') or '')):
        return normalize_ai_summary(
            {
                'routing': {
                    'paper_type': None,
                    'paper_subtype_raw': None,
                    'paper_subtype_normalized': None,
                    'classification_status': 'uncertain',
                    'route_confidence': 'low',
                    'classification_rationale': None,
                    'secondary_candidates': [],
                },
                'core_analysis': {
                    'summary': 'This is a dummy summary.',
                    'key_points': ['Dummy key point 1', 'Dummy key point 2'],
                    'methodology': 'Dummy methodology.',
                    'findings': 'Dummy findings.',
                    'conclusions': 'Dummy conclusions.',
                    'relevance': 'Dummy relevance.',
                    'limitations': 'Dummy limitations.',
                    'theoretical_framework': None,
                    'research_gap': None,
                    'future_research_directions': [],
                },
                'specialized_details': {
                    'empirical': None,
                    'review': None,
                    'conceptual': None,
                },
            }
        )

    # 增强的输入验证
    if not prompt_text or not prompt_text.strip():
        raise ValueError("提示词文本不能为空")

    if not primary_api_config:
        raise ValueError("主引擎API配置必须是有效的字典")

    if not backup_api_config:
        raise ValueError("备用引擎API配置必须是有效的字典")

    # 检查prompt_text长度，防止内存溢出
    if len(prompt_text) > 10000000:  # 10MB限制
        raise ValueError(f"提示词文本过长({len(prompt_text)}字符)，超过10MB限制")

    # 根据引擎类型选择配置
    if engine_type in engine_map:
        api_config = primary_api_config if engine_type == 'primary' else backup_api_config
        engine_name = engine_map[engine_type]['name']
    else:
        raise ValueError(f"未知的引擎类型: {engine_type}")

    api_key = api_config.get('api_key')
    api_base = api_config.get('api_base')
    model_name = api_config.get('model')

    if not api_key or not api_key.strip():
        raise ValueError(f"{engine_name}的API密钥不能为空")

    if not model_name or not model_name.strip():
        raise ValueError(f"{engine_name}的模型名称不能为空")

    try:
        config = load_config('config.ini')
    except Exception:
        config = None

    # 如果未提供api_base，则使用默认值
    if api_base is None:
        api_base = 'https://api.openai.com/v1'

    # 读取API参数配置
    try:
        if config:
            if engine_type == 'primary':
                max_tokens = int(config.get('API_Parameters', {}).get('primary_max_tokens', 3000))
                temperature = float(config.get('API_Parameters', {}).get('primary_temperature', 0.3))
            else:  # backup
                max_tokens = int(config.get('API_Parameters', {}).get('backup_max_tokens', 8192))
                temperature = float(config.get('API_Parameters', {}).get('backup_temperature', 0.3))
        else:
            # 默认值（向后兼容）
            max_tokens = 3000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取API参数配置失败，使用默认值: {e}")
        max_tokens = 3000
        temperature = 0.3

    # 从外部文件读取系统提示词
    try:
        with open('prompts/prompt_system_analyze.txt', 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except Exception as e:
        # 如果读取失败，使用默认提示词
        if logger:
            logger.warning(f"无法加载系统提示词文件，使用默认提示词: {e}")
        system_prompt = """你是一个学术文献分析专家。请对提供的学术文本进行深度分析，并返回一个结构化摘要。请严格按照JSON格式返回结果，包含title、authors、year、journal、summary、key_points、methodology、findings、conclusions、relevance、limitations等字段。"""

    # 使用统一的API调用函数
    ai_response = _call_ai_api(
        prompt_text,
        api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
        user_content=user_content,
    )

    if not ai_response:
        return None
    if isinstance(ai_response, dict):  # type: ignore
        structured_summary = normalize_ai_summary(ai_response)
        return structured_summary
    if logger:
        logger.warning("AI返回非字典格式，尝试手动解析")
    return _extract_summary_manually(ai_response)

    # 验证必需字段（两段式结构）
    if isinstance(ai_response, dict):  # type: ignore
        structured_summary: Dict[str, Any] = ai_response

        if 'common_core' not in structured_summary:
            # 兼容旧格式，自动转换
            if logger:
                logger.debug("检测到旧格式摘要，自动转换为两段式结构")
            structured_summary = {
                'common_core': structured_summary,
                'type_specific_details': _default_type_specific_details()
            }

        # 确保common_core是字典类型
        if not isinstance(structured_summary.get('common_core'), dict):
            if logger:
                logger.error(f"common_core类型错误: {type(structured_summary.get('common_core'))}")
            # 修复：返回None表示处理失败，而不是继续返回空结构
            # 这样可以正确触发main.py中的失败处理逻辑
            return None

        # 验证common_core中的必需字段
        required_fields = ['summary', 'key_points', 'methodology', 'findings', 'conclusions', 'relevance', 'limitations']
        for field in required_fields:
            if field not in structured_summary['common_core']:
                structured_summary['common_core'][field] = '' if field != 'key_points' else []

        metadata_defaults = {
            'title': '',
            'authors': [],
            'year': '',
            'journal': '',
            'doi': '',
        }
        for field, default_value in metadata_defaults.items():
            if field not in structured_summary['common_core']:
                structured_summary['common_core'][field] = default_value

        # 确保key_points是列表
        if not isinstance(structured_summary['common_core']['key_points'], list):
            structured_summary['common_core']['key_points'] = [str(structured_summary['common_core']['key_points'])]

        # 确保type_specific_details存在
        if 'type_specific_details' not in structured_summary:
            structured_summary['type_specific_details'] = _default_type_specific_details()
        else:
            structured_summary['type_specific_details'] = _normalize_type_specific_details(structured_summary['type_specific_details'])

        return structured_summary
    else:
        # 如果返回的是字符串，尝试手动提取信息
        if logger:
            logger.warning("AI返回非字典格式，尝试手动解析")
        return _extract_summary_manually(ai_response)


def _extract_summary_manually(ai_response: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    """
    当JSON解析失败时，使用正则表达式从AI响应中提取摘要信息

    Args:
        ai_response: AI的原始响应文本

    Returns:
        手动提取的结构化摘要
    """
    # 导入正则表达式模块（如果尚未导入）
    import re

    # 初始化两段式结果字典
    result: Dict[str, Any] = {
        'common_core': {
            'summary': '',
            'key_points': [],
            'methodology': '',
            'findings': '',
            'conclusions': '',
            'relevance': '',
            'limitations': ''
        },
        'type_specific_details': _default_type_specific_details()
    }

    # 尝试使用正则表达式提取JSON格式的部分
    # 查找可能的JSON结构，即使周围有其他文本
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    
        # 确保ai_response是字符串类型
    if isinstance(ai_response, dict):
        # 如果是字典，尝试转换为JSON字符串
        try:
            ai_response_str = json.dumps(ai_response)
        except (TypeError, ValueError):
            ai_response_str = str(ai_response)
    else:
        ai_response_str = str(ai_response)
    
    json_matches: List[str] = re.findall(json_pattern, ai_response_str, re.DOTALL)
    
    for match in json_matches:
        try:
            # 尝试解析找到的JSON片段
            json_data: Any = json.loads(match)
            
            # 如果解析成功，提取有用信息
            if isinstance(json_data, dict):  # json.loads可能返回任何JSON类型，需要检查是否为字典
                # 提取common_core部分
                if 'common_core' in json_data:
                    for key in result['common_core']:
                        if key in json_data['common_core']:
                            result['common_core'][key] = json_data['common_core'][key]
                else:
                    # 如果没有common_core，直接从顶层提取
                    for key in result['common_core']:
                        if key in json_data:
                            result['common_core'][key] = json_data[key]
                
                # 如果成功提取到有用信息，直接返回
                if any(result['common_core'].values()):
                    return result
        except (json.JSONDecodeError, AttributeError):
            # 如果解析失败，继续尝试下一个匹配
            continue

    # 如果JSON提取失败，使用正则表达式直接从文本中提取内容
    # 定义各种键的正则表达式模式
    patterns = {
        'summary': [
            r'"summary"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'摘要[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'摘要[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'summary[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'summary[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'key_points': [
            r'"key_points"\s*:\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]',
            r'要点[：:]\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]',
            r'key_points[：:]\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]'
        ],
        'methodology': [
            r'"methodology"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'方法[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'方法[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'methodology[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'methodology[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'findings': [
            r'"findings"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'发现[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'发现[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'findings[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'findings[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'conclusions': [
            r'"conclusions"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'结论[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'结论[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'conclusions[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'conclusions[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'relevance': [
            r'"relevance"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'相关性[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'相关性[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'relevance[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'relevance[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'limitations': [
            r'"limitations"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'限制[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'限制[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'limitations[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'limitations[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ]
    }

    # 对每个字段尝试所有模式
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            matches: List[str] = re.findall(pattern, ai_response_str, re.IGNORECASE | re.DOTALL)
            if matches:
                if field == 'key_points':
                    # 对于key_points，需要进一步解析列表项
                    list_content: str = matches[0]
                    # 尝试提取列表项
                    item_pattern = r'"([^"]*(?:\\.[^"]*)*)"'
                    items: List[str] = re.findall(item_pattern, list_content)
                    if not items:
                        # 如果没有找到带引号的项，尝试不带引号的项
                        item_pattern = r'([^,\[\]]+(?:\([^)]*\))?[^,\[\]]*)'
                        items = re.findall(item_pattern, list_content)
                    
                    # 清理并过滤空项
                    items = [item.strip().strip('"\'') for item in items if item.strip()]
                    if items:
                        result['common_core'][field] = items
                        break
                else:
                    # 对于其他字段，直接使用第一个匹配
                    content_str: str = matches[0].strip()
                    # 清理内容
                    content_str = re.sub(r'\s+', ' ', content_str)  # 合并多个空白字符
                    content_str = content_str.strip('"\'' )  # 移除引号
                    if content_str:
                        result['common_core'][field] = content_str
                        break


    # 如果没有提取到任何内容，返回一个基本结构
    if not any(result['common_core'].values()):
        result['common_core']['summary'] = ai_response_str[:500]  # 取前500字符作为摘要
        result['common_core']['key_points'] = ['解析失败，请查看原始响应']

    return result





if __name__ == "__main__":
    # 测试函数
    # 注意：模块级别的测试代码，应该使用logging而不是print
    import logging

    # 创建测试用logger
    test_logger = logging.getLogger('ai_interface_test')
    test_logger.setLevel(logging.INFO)

    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # 创建格式器
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s',
                                datefmt='%H:%M:%S')
    console_handler.setFormatter(formatter)

    # 添加处理器到记录器
    test_logger.addHandler(console_handler)

    test_logger.info("AI接口测试")
    test_logger.info("=" * 50)

    test_logger.info("\n注意：要进行完整测试，请提供有效的API配置")
    test_logger.info("使用方法：")
    test_logger.info("  python ai_interface.py")

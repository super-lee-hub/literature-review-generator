import base64
import copy
import hashlib
import json
import mimetypes
import os
import time
import re
import requests  # type: ignore
from typing import Union, Dict, Optional, Any, List, Tuple, Callable, Set, Mapping

from models import APIConfig
from config_loader import load_config
from services.model_capabilities import (
    DEFAULT_ANTHROPIC_MESSAGES_PATH,
    DEFAULT_ANTHROPIC_VERSION,
    ModelCapability,
    anthropic_temperature_allowed,
    anthropic_thinking_will_be_active,
    apply_reasoning_policy,
    remove_payload_path,
    resolve_anthropic_messages_url,
    resolve_model_capability,
)
from services.proxy_policy import should_bypass_environment_proxy
from services.prompt_registry import default_prompt_registry
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderBudgetExceeded,
    ProviderRuntime,
    canonical_provider_request_payload,
)
from summary_schema import (
    default_ai_summary,
    get_ai_summary,
    normalize_ai_summary,
)
from services.multimodal_capability import detect_multimodal_capability
from services.stage1_visual_scan import (
    DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    estimate_encoded_image_bytes,
    normalize_visual_byte_budgets,
)

_DEFAULT_TIMEOUT_SECONDS = 600
_DEFAULT_API_RETRY_ATTEMPTS = 3
_MAX_LOCAL_IMAGE_INPUT_BYTES = DEFAULT_MAX_SINGLE_IMAGE_BYTES
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


def _load_stage1_system_prompt(logger: Any = None) -> str:
    """Load the canonical Stage 1 system prompt through prompt authority."""

    # Prompt authority failures are production blockers. A generic fallback
    # would make the receipt look valid while changing the actual prompt.
    return default_prompt_registry().read("stage1.analysis.system.v3")


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
                detail = str(image_url.get("detail") or item.get("detail") or "original").strip()
            else:
                url = str(image_url or "").strip()
                detail = str(item.get("detail") or "original").strip()
            if url:
                converted_item: Dict[str, Any] = {"type": "input_image", "image_url": url}
                if detail:
                    converted_item["detail"] = detail
                converted.append(converted_item)
            continue
        if item_type == "input_text":
            converted.append({"type": "input_text", "text": str(item.get("text") or "")})
            continue
        if item_type == "input_image":
            image_url = str(item.get("image_url") or item.get("url") or "").strip()
            if image_url:
                converted_item = {"type": "input_image", "image_url": image_url}
                if item.get("detail"):
                    converted_item["detail"] = str(item.get("detail"))
                converted.append(converted_item)
            continue
        if item_type == "input_file":
            file_item: Dict[str, Any] = {"type": "input_file"}
            for key in ("file_id", "file_url", "file_data", "filename"):
                if item.get(key):
                    file_item[key] = str(item.get(key))
            if len(file_item) > 1:
                converted.append(file_item)
    return converted or [{"type": "input_text", "text": ""}]


ANTHROPIC_JSON_INSTRUCTION = (
    "Return only a single valid JSON object and nothing else. "
    "Do not wrap it in markdown code fences and do not add commentary."
)

# Re-exported from ``services.model_capabilities`` so there is exactly one
# definition of the Anthropic wire defaults. The runtime, the connection probe
# and the setup wizard all resolve the URL through the same helper below.
# ``DEFAULT_ANTHROPIC_VERSION`` is imported directly and is not re-declared.
DEFAULT_ANTHROPIC_PATH = DEFAULT_ANTHROPIC_MESSAGES_PATH

# Anthropic's own guidance for xhigh/max effort is to start at 64k tokens.
ANTHROPIC_HIGH_EFFORT_TOKEN_FLOOR = 65_536


def _split_data_url(url: str) -> Tuple[str, str]:
    """Split a ``data:`` URL into ``(media_type, base64_payload)``."""

    if not url.startswith("data:"):
        return "", ""
    header, _, payload = url.partition(",")
    media_type = header[len("data:") :].split(";", 1)[0].strip()
    return (media_type or "image/png"), payload.strip()


def _convert_chat_content_to_anthropic_content(content: Any) -> Any:
    """Convert normalized OpenAI-style content parts into Anthropic blocks.

    Anthropic splits text and images into typed blocks and represents inline
    images as base64 sources rather than ``image_url`` wrappers.  Parts with no
    Anthropic equivalent are dropped here; the caller's visual omission report
    is what surfaces that to the operator, so nothing disappears silently.
    """

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    converted: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"text", "input_text"}:
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                converted.append({"type": "text", "text": text})
            continue
        if item_type == "image_url":
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            elif isinstance(image_url, str):
                url = image_url.strip()
            if not url:
                continue
            if url.startswith("data:"):
                media_type, data = _split_data_url(url)
                if data:
                    converted.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": data},
                        }
                    )
            else:
                converted.append({"type": "image", "source": {"type": "url", "url": url}})
    return converted or None


def build_anthropic_messages_payload(
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
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a native Anthropic Messages request body.

    Three wire differences matter and are handled here rather than being fudged
    into the OpenAI shape: the system prompt is a top-level ``system`` field, the
    token limit is ``max_tokens``, and there is no response_format parameter, so
    a JSON request is expressed as a prompt instruction instead.
    """

    capability = capability or resolve_model_capability(api_config)
    normalized_user_content = _normalize_user_message_content(
        prompt,
        user_content,
        logger=logger,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )

    token_limit = _configured_positive_int(api_config.get("max_tokens"), max_tokens)
    token_limit = _configured_positive_int(api_config.get("max_output_tokens"), token_limit)
    # Only manual extended thinking has a budget, and it must sit below
    # max_tokens because thinking tokens count against that ceiling. Adaptive
    # thinking has no budget at all, so this does not apply there.
    if capability.anthropic_thinking_mode == "manual":
        thinking_budget = _configured_positive_int(api_config.get("thinking_budget_tokens"), 0)
        if thinking_budget:
            token_limit = max(token_limit, thinking_budget + 1)

    system_text = str(system_prompt or "").strip()
    if response_format == "json":
        system_text = f"{system_text}\n\n{ANTHROPIC_JSON_INSTRUCTION}" if system_text else ANTHROPIC_JSON_INSTRUCTION
        if logger:
            logger.info(
                "Anthropic Messages has no native JSON response mode; a JSON-only "
                "instruction was appended to the system prompt instead."
            )

    payload: Dict[str, Any] = {
        "model": api_config.get("model") or "",
        "max_tokens": token_limit,
        "system": system_text,
        "messages": [
            {
                "role": "user",
                "content": _convert_chat_content_to_anthropic_content(normalized_user_content),
            }
        ],
    }
    # Anthropic's sampling contract is generation-specific, not a global
    # "thinking on/off" switch. Two rules decide this, both from the current
    # API usage primer:
    #   * temperature must be 1 or unset whenever thinking is enabled;
    #   * on Claude 4.7+ and Mythos Preview it is deprecated outright, so
    #     thinking=disabled does not restore it.
    # An unrecognised model gets the conservative answer (withhold) rather than
    # an optimistic one, because there is no generation evidence to relax on.
    thinking_active = anthropic_thinking_will_be_active(api_config, capability)
    if (
        not _config_bool(api_config.get("omit_temperature_when_reasoning"))
        and anthropic_temperature_allowed(
            str(api_config.get("model") or ""), thinking_active=thinking_active
        )
    ):
        payload["temperature"] = temperature
    # top_p / top_k are not part of this transport's emitted contract at all;
    # stripping them keeps a stray value from reaching a strict gateway.
    payload.pop("top_p", None)
    payload.pop("top_k", None)
    apply_reasoning_policy(payload, api_config, capability, logger=logger)

    # Anthropic needs a large max_tokens at the top effort levels: thinking,
    # tool calls and subagent work all draw on the same ceiling. The configured
    # limit is respected rather than silently raised -- this is a cost decision,
    # not something the transport should decide for the caller -- but it is
    # reported, because an undersized limit surfaces as truncation far from here.
    effort = str((payload.get("output_config") or {}).get("effort") or "").casefold()
    if effort in {"xhigh", "max"} and token_limit < ANTHROPIC_HIGH_EFFORT_TOKEN_FLOOR:
        if logger:
            logger.warning(
                "Anthropic %s effort needs a large max_tokens (64k is a reasonable "
                "starting point); configured limit is %d, so the response may be "
                "truncated.",
                effort,
                token_limit,
            )
    return payload


def parse_anthropic_messages_response(response_data: Dict[str, Any]) -> Tuple[str, str]:
    """Extract text and a normalized finish reason from a Messages response.

    Anthropic returns content as a block list that can interleave thinking and
    text blocks; only text blocks are answer content.
    """

    blocks = response_data.get("content")
    texts: List[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").strip().lower() != "text":
                continue
            text = block.get("text")
            if text:
                texts.append(str(text))
    content = "\n".join(texts).strip()

    # Not every Anthropic stop reason is terminal. Collapsing a truncated or
    # refused response onto "stop" lets it be adopted as a finished artifact,
    # which is the worst possible outcome for a citation-bearing outline, so
    # each non-terminal outcome keeps its own value and is treated as incomplete
    # downstream -- even when the partial content happens to be valid JSON.
    stop_reason = str(response_data.get("stop_reason") or "").strip().lower()
    finish_reason = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        # The context window was exhausted: this is a prefix, not an answer.
        "model_context_window_exceeded": "length",
        "pause_turn": "incomplete_continuation",
        "refusal": "refusal",
        # Outline has no tool loop, so a tool request cannot be completed here.
        "tool_use": "anthropic_tool_use",
    }.get(stop_reason, stop_reason or "stop")
    return content, finish_reason


def anthropic_request_target(
    api_base: str,
    api_config: APIConfig,
    api_key: str,
) -> Tuple[str, Dict[str, str]]:
    """Resolve the Anthropic Messages URL and headers.

    Anthropic does not take a Bearer token: it authenticates with ``x-api-key``
    and pins an ``anthropic-version`` header. Sending the OpenAI-style header
    fails at the gateway, so this contract is kept in one place and unit-tested
    rather than being rebuilt inline at the call site.

    The path is configurable because gateways disagree on whether the configured
    base URL already contains the version segment.

    The join itself is delegated to :func:`resolve_anthropic_messages_url`, which
    is also what the "test connection" probe uses. Duplicating the rule here is
    what previously allowed a probe to succeed against a URL the runtime would
    never request.
    """

    url = resolve_anthropic_messages_url(api_base, str(api_config.get("anthropic_path") or ""))
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": str(
            api_config.get("anthropic_version") or DEFAULT_ANTHROPIC_VERSION
        ).strip(),
    }
    return url, headers


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
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    capability = capability or resolve_model_capability(api_config)
    normalized_user_content = _normalize_user_message_content(
        prompt,
        user_content,
        logger=logger,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
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
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    capability = capability or resolve_model_capability(api_config)
    normalized_user_content = _normalize_user_message_content(
        prompt,
        user_content,
        logger=logger,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
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
    if response_format == "json" and finish_reason in {
        "length",
        "incomplete_continuation",
        "refusal",
        "anthropic_tool_use",
    }:
        message = f"JSON provider response terminated with non-terminal finish reason: {finish_reason}"
        if logger:
            logger.warning(message)
        return _api_result(
            status="failed",
            error_kind="invalid_response",
            http_status=getattr(response, "status_code", None),
            message=message,
            finish_reason=finish_reason,
        )
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
    specialized = default_ai_summary()["specialized_details"]
    empirical = specialized.get("empirical") or {}
    return dict(empirical.get("core_variables") or {})


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


def _load_api_runtime_settings(api_config: Optional[Mapping[str, Any]] = None) -> Tuple[int, int]:
    timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    retry_attempts = _DEFAULT_API_RETRY_ATTEMPTS

    if api_config:
        timeout_seconds = _coerce_positive_int(
            api_config.get("read_timeout_seconds", api_config.get("total_timeout_seconds", timeout_seconds)),
            timeout_seconds,
        )
        retry_attempts = _coerce_positive_int(
            api_config.get("transport_retries", retry_attempts),
            retry_attempts,
        )
        return timeout_seconds, retry_attempts

    try:
        config = load_config()
    except Exception:
        return timeout_seconds, retry_attempts

    runtime = config.get("Runtime", {}) or {}
    retry_attempts = _coerce_positive_int(
        runtime.get("transport_retries", retry_attempts),
        retry_attempts,
    )
    return timeout_seconds, retry_attempts


def _encode_local_image_as_data_url(
    path: str,
    *,
    max_image_bytes: int = _MAX_LOCAL_IMAGE_INPUT_BYTES,
) -> Optional[str]:
    if not path:
        return None
    try:
        image_size = os.path.getsize(path)
    except OSError:
        return None
    max_bytes = max(1, int(max_image_bytes or _MAX_LOCAL_IMAGE_INPUT_BYTES))
    if image_size <= 0 or image_size > max_bytes:
        return None

    try:
        with open(path, "rb") as handle:
            image_bytes = handle.read(max_bytes + 1)
    except OSError:
        return None

    if not image_bytes or len(image_bytes) > max_bytes:
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


def _snapshot_local_image(
    path: str,
    *,
    max_single_image_bytes: int,
) -> Tuple[Optional[str], int, str, str]:
    """Read one image exactly once for the final wire preflight.

    The returned data URL is the frozen wire fact.  The transport must not
    re-open the path after this function succeeds; a later delete or resize
    therefore cannot turn an admitted atomic group into a partial request.
    """

    normalized = str(path or "").strip()
    if not normalized or not os.path.isfile(normalized):
        return None, 0, "", "missing_or_unreadable_local_image"
    try:
        with open(normalized, "rb") as handle:
            image_bytes = handle.read(max_single_image_bytes + 1)
    except OSError:
        return None, 0, "", "missing_or_unreadable_local_image"
    if not image_bytes:
        return None, 0, "", "image_empty"
    if len(image_bytes) > max_single_image_bytes:
        return None, len(image_bytes), "", "image_exceeds_single_byte_budget"
    digest = hashlib.sha256(image_bytes).hexdigest()
    mime_type = mimetypes.guess_type(normalized)[0] or "image/png"
    return (
        f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}",
        len(image_bytes),
        digest,
        "",
    )


def _transport_visual_label(item: Mapping[str, Any]) -> str:
    visual_id = str(item.get("visual_id") or "")
    page_no = int(item.get("page_no") or 0)
    bbox = item.get("bbox") or []
    artifact_type = str(item.get("artifact_type") or "visual")
    caption = " ".join(
        " ".join(str(item.get(key) or "").split())
        for key in ("caption_excerpt", "title_or_caption")
    )[:360]
    nearby = " ".join(str(item.get("nearby_text_excerpt") or "").split())[:500]
    return (
        f"[VISUAL OBJECT] visual_id={visual_id}; page_no={page_no}; bbox={bbox}; "
        f"artifact_type={artifact_type}\n"
        f"caption_excerpt={caption or '<none>'}\n"
        f"nearby_text_excerpt={nearby or '<none>'}\n"
        "The following image is evidence for this label only."
    )


def _typed_transport_omission(
    item: Mapping[str, Any] | None,
    *,
    reason: str,
    visual_id: str = "",
    page_no: int = 0,
    default_scope: str = "final_transport",
    raw_reinspection_group_id: str = "",
    scope: str = "",
    authority_blocking: bool | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Attach an explicit authority scope without allowing semantic drift."""

    source = dict(item or {})
    group_id = str(
        raw_reinspection_group_id or source.get("raw_reinspection_group_id") or ""
    ).strip()
    resolved_scope = (
        "raw_reinspection"
        if group_id
        else str(scope or source.get("transport_omission_scope") or default_scope).strip()
    )
    resolved_authority_blocking = authority_blocking
    if not isinstance(resolved_authority_blocking, bool):
        resolved_authority_blocking = source.get("transport_omission_authority_blocking")
    if not isinstance(resolved_authority_blocking, bool):
        resolved_authority_blocking = resolved_scope != "raw_reinspection"
    omission: Dict[str, Any] = {
        "visual_id": str(visual_id or source.get("visual_id") or ""),
        "page_no": int(page_no or source.get("page_no") or 0),
        "reason": str(reason or "transport_omission"),
        "scope": resolved_scope,
        "authority_blocking": resolved_authority_blocking,
    }
    if group_id:
        omission["raw_reinspection_group_id"] = group_id
    protected = {
        "visual_id",
        "page_no",
        "reason",
        "scope",
        "authority_blocking",
        "raw_reinspection_group_id",
    }
    omission.update(
        {
            key: value
            for key, value in extra.items()
            if value is not None and key not in protected
        }
    )
    return omission


def _drop_visual_label(output: List[Dict[str, Any]], visual_id: str) -> None:
    marker = f"visual_id={visual_id}"
    for index in range(len(output) - 1, -1, -1):
        item = output[index]
        if str(item.get("type") or "").strip().lower() not in {"text", "input_text"}:
            break
        if marker in str(item.get("text") or ""):
            output.pop(index)
            break


def _frozen_local_image_item(
    item: Mapping[str, Any],
    *,
    data_url: str,
    image_bytes: int,
    image_sha256: str,
) -> Dict[str, Any]:
    frozen = dict(item)
    frozen["type"] = "local_image_path"
    frozen["frozen_image_data_url"] = data_url
    frozen["frozen_image_bytes"] = image_bytes
    frozen["frozen_image_sha256"] = image_sha256
    frozen["image_bytes"] = image_bytes
    frozen["image_sha256"] = image_sha256
    frozen["transport_frozen"] = True
    return frozen


def freeze_local_visual_transport_content(
    user_content: Any,
    *,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Freeze local visual membership before a provider POST.

    Atomic ``all_children`` groups are admitted as one transaction.  If the
    actual filesystem snapshot no longer satisfies the group contract, the
    whole group is replaced by its declared page fallback, or omitted as one
    explicit ``not_represented`` unit.  No child is ever sent alone.
    """

    max_request, max_single = normalize_visual_byte_budgets(
        max_request_image_bytes=max_request_image_bytes,
        max_single_image_bytes=max_single_image_bytes,
    )
    if not isinstance(user_content, list):
        return user_content, {
            "planned_visual_ids": [],
            "sent_visual_ids": [],
            "omissions": [],
            "raw_reinspection_groups": [],
            "estimated_encoded_image_bytes": 0,
        }

    raw_items = [dict(item) for item in user_content if isinstance(item, Mapping)]
    image_indices = [
        index
        for index, item in enumerate(raw_items)
        if str(item.get("type") or "").strip().lower() == "local_image_path"
    ]
    groups: Dict[Tuple[str, str], List[int]] = {}
    for index in image_indices:
        item = raw_items[index]
        group_id = str(item.get("raw_reinspection_group_id") or "").strip()
        resolution = str(item.get("raw_reinspection_resolution") or "").strip()
        if bool(item.get("raw_reinspection_atomic")) and group_id and resolution == "all_children":
            groups.setdefault((group_id, resolution), []).append(index)

    replacements: Dict[int, Optional[Dict[str, Any]]] = {}
    processed: set[int] = set()
    omissions: List[Dict[str, Any]] = []
    group_reports: List[Dict[str, Any]] = []
    encoded_bytes = 0
    sent_visual_ids: List[str] = []

    def admit_one(item: Mapping[str, Any], *, remaining: int) -> Tuple[Optional[Dict[str, Any]], str]:
        frozen_url = str(item.get("frozen_image_data_url") or "").strip()
        if frozen_url:
            try:
                frozen_bytes = int(item.get("frozen_image_bytes") or item.get("image_bytes") or 0)
            except (TypeError, ValueError):
                frozen_bytes = 0
            frozen_hash = str(item.get("frozen_image_sha256") or item.get("image_sha256") or "")
            estimated = estimate_encoded_image_bytes(frozen_bytes)
            if frozen_bytes <= 0:
                return None, "image_empty"
            if frozen_bytes > max_single:
                return None, "image_exceeds_single_byte_budget"
            if estimated > remaining:
                return None, "image_exceeds_request_byte_budget"
            return _frozen_local_image_item(
                item,
                data_url=frozen_url,
                image_bytes=frozen_bytes,
                image_sha256=frozen_hash,
            ), ""
        data_url, image_size, image_hash, reason = _snapshot_local_image(
            str(item.get("path") or ""),
            max_single_image_bytes=max_single,
        )
        if reason:
            return None, reason
        estimated = estimate_encoded_image_bytes(image_size)
        if estimated > remaining:
            return None, "image_exceeds_request_byte_budget"
        return _frozen_local_image_item(
            item,
            data_url=str(data_url),
            image_bytes=image_size,
            image_sha256=image_hash,
        ), ""

    for index in image_indices:
        if index in processed:
            continue
        item = raw_items[index]
        group_id = str(item.get("raw_reinspection_group_id") or "").strip()
        resolution = str(item.get("raw_reinspection_resolution") or "").strip()
        group_key = (group_id, resolution)
        member_indices = groups.get(group_key) if group_key in groups else None
        if member_indices:
            processed.update(member_indices)
            candidate_ids = [
                str(raw_items[member].get("visual_id") or "")
                for member in member_indices
                if str(raw_items[member].get("visual_id") or "")
            ]
            snapshots: List[Tuple[int, Dict[str, Any]]] = []
            group_reason = ""
            for member in member_indices:
                frozen, reason = admit_one(raw_items[member], remaining=max_request - encoded_bytes)
                if reason:
                    group_reason = reason
                    break
                if frozen is not None:
                    snapshots.append((member, frozen))
            group_cost = sum(
                estimate_encoded_image_bytes(int(frozen.get("image_bytes") or 0))
                for _member, frozen in snapshots
            )
            if not group_reason and len(snapshots) != len(member_indices):
                group_reason = "atomic_group_member_not_admitted"
            if not group_reason and encoded_bytes + group_cost > max_request:
                group_reason = "image_exceeds_request_byte_budget"
            if not group_reason:
                for member, frozen in snapshots:
                    frozen["transport_planned_visual_ids"] = list(candidate_ids)
                    replacements[member] = frozen
                encoded_bytes += group_cost
                sent_visual_ids.extend(candidate_ids)
                group_reports.append(
                    {
                        "group_id": group_id,
                        "page_no": int(item.get("page_no") or 0),
                        "ambiguous_candidate_ids": candidate_ids,
                        "resolution": "all_children",
                        "selected_ids": candidate_ids,
                        "actual_sent_ids": candidate_ids,
                        "transport_status": "complete",
                        "fallback_reason": "",
                    }
                )
                continue

            fallback = item.get("raw_reinspection_fallback_ref")
            fallback_item = dict(fallback) if isinstance(fallback, Mapping) else {}
            fallback_id = str(fallback_item.get("visual_id") or "").strip()
            fallback_frozen: Optional[Dict[str, Any]] = None
            fallback_reason = group_reason or "atomic_group_not_admitted"
            if fallback_id:
                # Selector/manifest refs use ``image_path`` while the local
                # transport item uses ``path``.  Normalize the declared page
                # fallback before the atomic admission attempt; otherwise a
                # valid fallback would be misclassified as missing and the
                # whole unit would be unnecessarily dropped.
                fallback_item["path"] = str(
                    fallback_item.get("path")
                    or fallback_item.get("image_path")
                    or ""
                )
                fallback_frozen, fallback_error = admit_one(
                    {
                        **fallback_item,
                        "raw_reinspection_group_id": group_id,
                        "raw_reinspection_resolution": "page_snapshot_fallback",
                        "raw_reinspection_selected_ids": [fallback_id],
                        "ambiguous_candidate_ids": candidate_ids,
                        "raw_reinspection_atomic": True,
                    },
                    remaining=max_request - encoded_bytes,
                )
                if fallback_error:
                    fallback_frozen = None
                    fallback_reason = f"{fallback_reason};fallback:{fallback_error}"
            if fallback_frozen is not None:
                fallback_frozen["transport_planned_visual_ids"] = list(candidate_ids)
                replacements[member_indices[0]] = fallback_frozen
                for member in member_indices[1:]:
                    replacements[member] = None
                encoded_bytes += estimate_encoded_image_bytes(
                    int(fallback_frozen.get("image_bytes") or 0)
                )
                sent_visual_ids.append(fallback_id)
                group_reports.append(
                    {
                        "group_id": group_id,
                        "page_no": int(item.get("page_no") or 0),
                        "ambiguous_candidate_ids": candidate_ids,
                        "resolution": "page_snapshot_fallback",
                        "selected_ids": [fallback_id],
                        "actual_sent_ids": [fallback_id],
                        "transport_status": "complete",
                        "fallback_reason": fallback_reason,
                    }
                )
            else:
                for member in member_indices:
                    replacements[member] = None
                    omissions.append(
                        _typed_transport_omission(
                            raw_items[member],
                            reason="raw_reinspection_group_not_represented",
                            raw_reinspection_group_id=group_id,
                            raw_reinspection_resolution="not_represented",
                            raw_reinspection_fallback_reason=fallback_reason,
                            raw_reinspection_planned_ids=candidate_ids,
                        )
                    )
                group_reports.append(
                    {
                        "group_id": group_id,
                        "page_no": int(item.get("page_no") or 0),
                        "ambiguous_candidate_ids": candidate_ids,
                        "resolution": "not_represented",
                        "selected_ids": [],
                        "actual_sent_ids": [],
                        "transport_status": "not_sent",
                        "fallback_reason": fallback_reason,
                    }
                )
            continue

        processed.add(index)
        frozen, reason = admit_one(item, remaining=max_request - encoded_bytes)
        if frozen is None:
            replacements[index] = None
            omissions.append(
                _typed_transport_omission(
                    item,
                    reason=reason or "local_image_not_admitted",
                )
            )
            continue
        visual_id = str(item.get("visual_id") or "")
        replacements[index] = frozen
        frozen["transport_planned_visual_ids"] = [visual_id] if visual_id else []
        encoded_bytes += estimate_encoded_image_bytes(int(frozen.get("image_bytes") or 0))
        if visual_id:
            sent_visual_ids.append(visual_id)

    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if str(item.get("type") or "").strip().lower() != "local_image_path":
            normalized.append(item)
            continue
        replacement = replacements.get(index)
        if replacement is None:
            _drop_visual_label(normalized, str(item.get("visual_id") or ""))
            continue
        if replacement is not item and replacement.get("visual_id") != item.get("visual_id"):
            _drop_visual_label(normalized, str(item.get("visual_id") or ""))
            normalized.append({"type": "text", "text": _transport_visual_label(replacement)})
        normalized.append(replacement)
    planned_ids = [
        str(raw_items[index].get("visual_id") or "")
        for index in image_indices
        if str(raw_items[index].get("visual_id") or "")
    ]
    return normalized, {
        "planned_visual_ids": planned_ids,
        "sent_visual_ids": sent_visual_ids,
        "omissions": omissions,
        "raw_reinspection_groups": group_reports,
        "estimated_encoded_image_bytes": encoded_bytes,
        "max_single_image_bytes": max_single,
        "max_request_image_bytes": max_request,
        "images_planned_count": len(planned_ids),
        "images_actually_sent_count": len(sent_visual_ids),
    }


def _normalize_user_message_content_with_report(
    prompt: str,
    user_content: Any,
    logger: Any = None,
    *,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Tuple[Any, Dict[str, Any]]:
    max_request, max_single = normalize_visual_byte_budgets(
        max_request_image_bytes=max_request_image_bytes,
        max_single_image_bytes=max_single_image_bytes,
    )
    if not isinstance(user_content, list):
        return prompt, {"sent_visual_ids": [], "omissions": [], "images_actually_sent_count": 0}

    normalized: List[Dict[str, Any]] = []
    sent_visual_ids: List[str] = []
    planned_visual_ids: List[str] = []
    omissions: List[Dict[str, Any]] = []
    raw_group_reports: Dict[str, Dict[str, Any]] = {}
    encoded_bytes = 0

    def note_raw_group(item: Mapping[str, Any], *, actual_visual_id: str = "") -> None:
        group_id = str(item.get("raw_reinspection_group_id") or "").strip()
        if not group_id:
            return
        candidate_ids = [
            str(value)
            for value in (
                item.get("ambiguous_candidate_ids")
                or item.get("transport_planned_visual_ids")
                or []
            )
            if str(value)
        ]
        selected_ids = [
            str(value)
            for value in (item.get("raw_reinspection_selected_ids") or [])
            if str(value)
        ]
        report = raw_group_reports.setdefault(
            group_id,
            {
                "group_id": group_id,
                "page_no": int(item.get("page_no") or 0),
                "ambiguous_candidate_ids": candidate_ids,
                "resolution": str(item.get("raw_reinspection_resolution") or ""),
                "selected_ids": selected_ids,
                "actual_sent_ids": [],
                "fallback_reason": str(
                    item.get("raw_reinspection_fallback_reason") or ""
                ),
            },
        )
        for value in candidate_ids:
            if value not in report["ambiguous_candidate_ids"]:
                report["ambiguous_candidate_ids"].append(value)
        for value in selected_ids:
            if value not in report["selected_ids"]:
                report["selected_ids"].append(value)
        if actual_visual_id and actual_visual_id not in report["actual_sent_ids"]:
            report["actual_sent_ids"].append(actual_visual_id)
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
            visual_id = str(item.get("visual_id") or "")
            page_no = int(item.get("page_no") or 0)
            planned_ids_for_item = [
                str(value)
                for value in (
                    item.get("transport_planned_visual_ids")
                    or item.get("ambiguous_candidate_ids")
                    or ([visual_id] if visual_id else [])
                )
                if str(value)
            ]
            for value in planned_ids_for_item:
                if value not in planned_visual_ids:
                    planned_visual_ids.append(value)
            note_raw_group(item)
            frozen_url = str(item.get("frozen_image_data_url") or "").strip()
            try:
                image_size = int(item.get("frozen_image_bytes") or 0)
            except (TypeError, ValueError):
                image_size = 0
            if frozen_url and image_size > 0:
                data_url = frozen_url
            else:
                try:
                    image_size = int(os.path.getsize(path))
                except OSError:
                    image_size = 0
                if not path or not os.path.isfile(path) or image_size <= 0:
                    omissions.append(
                        _typed_transport_omission(
                            item,
                            reason="missing_or_unreadable_local_image",
                            visual_id=visual_id,
                            page_no=page_no,
                        )
                    )
                    if logger:
                        logger.warning(f"Skipping missing or unreadable local image input: {path}")
                    continue
                if image_size > max_single:
                    omissions.append(
                        _typed_transport_omission(
                            item,
                            reason="image_exceeds_single_byte_budget",
                            visual_id=visual_id,
                            page_no=page_no,
                        )
                    )
                    continue
                data_url = _encode_local_image_as_data_url(path, max_image_bytes=max_single)
            estimated = estimate_encoded_image_bytes(image_size)
            if encoded_bytes + estimated > max_request:
                omissions.append(
                    _typed_transport_omission(
                        item,
                        reason="image_exceeds_request_byte_budget",
                        visual_id=visual_id,
                        page_no=page_no,
                    )
                )
                continue
            if not data_url:
                omissions.append(
                    _typed_transport_omission(
                        item,
                        reason="missing_or_oversized_local_image",
                        visual_id=visual_id,
                        page_no=page_no,
                    )
                )
                if logger:
                    logger.warning(f"Skipping missing or unreadable local image input: {path}")
                continue
            normalized.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": str(item.get("detail") or "original")},
                }
            )
            encoded_bytes += estimated
            note_raw_group(item, actual_visual_id=visual_id)
            if visual_id:
                sent_visual_ids.append(visual_id)
            continue
        if item_type == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and image_url.get("url"):
                normalized.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": str(image_url.get("url")),
                            "detail": str(image_url.get("detail") or item.get("detail") or "original"),
                        },
                    }
                )
            elif isinstance(image_url, str) and image_url.strip():
                normalized.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url.strip(),
                            "detail": str(item.get("detail") or "original"),
                        },
                    }
                )
            continue
        if item_type == "local_pdf_path":
            path = str(item.get("path") or "").strip()
            data_url = _encode_local_pdf_as_data_url(path)
            if not data_url:
                omissions.append(
                    _typed_transport_omission(
                        item,
                        reason="missing_or_unreadable_local_pdf",
                    )
                )
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
        return prompt, {
            "planned_visual_ids": planned_visual_ids,
            "sent_visual_ids": sent_visual_ids,
            "omissions": omissions,
            "raw_reinspection_groups": list(raw_group_reports.values()),
            "images_planned_count": len(planned_visual_ids),
            "images_actually_sent_count": len(sent_visual_ids),
            "estimated_encoded_image_bytes": encoded_bytes,
        }

    has_text = any(item.get("type") == "text" for item in normalized)
    if not has_text and prompt:
        normalized.insert(0, {"type": "text", "text": prompt})
    return normalized, {
        "planned_visual_ids": planned_visual_ids,
        "sent_visual_ids": sent_visual_ids,
        "omissions": omissions,
        "raw_reinspection_groups": list(raw_group_reports.values()),
        "images_planned_count": len(planned_visual_ids),
        "images_actually_sent_count": len(sent_visual_ids),
        "estimated_encoded_image_bytes": encoded_bytes,
    }


def _normalize_user_message_content(
    prompt: str,
    user_content: Any,
    logger: Any = None,
    *,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Any:
    normalized, _report = _normalize_user_message_content_with_report(
        prompt,
        user_content,
        logger=logger,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    return normalized


def _text_only_user_content(user_content: Any) -> Any:
    """Remove image/file parts when a fallback route is text-only."""

    if not isinstance(user_content, list):
        return user_content
    text_items = [
        dict(item)
        for item in user_content
        if isinstance(item, dict)
        and str(item.get("type") or "").strip().lower() in {"text", "input_text"}
        and str(item.get("text") or "").strip()
    ]
    return text_items or None


def _admit_local_images_to_budget(
    user_content: Any,
    *,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Freeze image membership once, preserving atomic raw-reinspection units."""

    frozen, report = freeze_local_visual_transport_content(
        user_content,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    return frozen, [
        dict(item)
        for item in (report.get("omissions") or [])
        if isinstance(item, Mapping)
    ]


def _call_ai_api_detailed_uninstrumented(
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
    max_retries_per_call: int = 0,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Call a configured AI API transport and retain failure details."""
    attempts_used = 0
    removed_compat_params: Set[Any] = set()

    def mutation_label(value: Any) -> str:
        if isinstance(value, tuple) and tuple(str(item) for item in value) == ("reasoning", "display"):
            return "removed_reasoning_display"
        text = str(value or "").strip()
        if text == "reasoning_effort:max_to_high":
            return "reasoning_effort:max->high"
        if text.startswith("removed_"):
            return text
        return f"removed_{text}" if text else ""

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(result)
        enriched["attempts"] = max(1, attempts_used)
        enriched["fallback_or_payload_mutations"] = sorted(
            label
            for label in (mutation_label(item) for item in removed_compat_params)
            if label
        )
        return enriched

    try:
        api_key = api_config.get('api_key') or ''
        model_name = api_config.get('model') or ''
        api_base = api_config.get('api_base', 'https://api.openai.com/v1') or 'https://api.openai.com/v1'
        capability = resolve_model_capability(api_config)

        if not api_key or not model_name:
            message = "API config is missing api_key or model"
            if logger:
                logger.error(message)
            return finish(_api_result(status="failed", error_kind="fatal_config_or_auth", message=message))

        configured_timeout_seconds, configured_retries = _load_api_runtime_settings(api_config)
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
        if max_retries_per_call:
            max_retries = min(max_retries, max(1, int(max_retries_per_call)) + 1)
        if capability.endpoint_type == "anthropic":
            api_url, headers = anthropic_request_target(api_base, api_config, api_key)
        else:
            endpoint_suffix = (
                "responses" if capability.endpoint_type == "responses" else "chat/completions"
            )
            api_url = f"{api_base.rstrip('/')}/{endpoint_suffix}"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        if capability.endpoint_type == "anthropic":
            payload = build_anthropic_messages_payload(
                prompt,
                api_config,
                system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                user_content=user_content,
                logger=logger,
                capability=capability,
                max_single_image_bytes=max_single_image_bytes,
                max_request_image_bytes=max_request_image_bytes,
            )
            response_parser = parse_anthropic_messages_response
        elif capability.endpoint_type == "responses":
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
                max_single_image_bytes=max_single_image_bytes,
                max_request_image_bytes=max_request_image_bytes,
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
                max_single_image_bytes=max_single_image_bytes,
                max_request_image_bytes=max_request_image_bytes,
            )
            response_parser = parse_chat_completions_response

        response = None
        last_failure = _api_result(status="failed", error_kind="invalid_response", message="API call did not run")
        attempt = 0
        strict_retry_budget = bool(max_retries_per_call)

        def can_start_attempt() -> bool:
            if strict_retry_budget:
                return attempts_used < max_retries
            return attempt < max_retries

        while can_start_attempt():
            attempt += 1
            attempts_used += 1
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
                    return finish(_api_result(
                        status="failed",
                        error_kind="invalid_response",
                        http_status=getattr(response, "status_code", None),
                        message=message,
                    ))

                formatted = _format_success_result(content, response_format, response, finish_reason, logger=logger)
                if isinstance(response_data, dict):
                    provider_model = str(response_data.get("model") or "").strip()
                    usage = response_data.get("usage")
                    formatted["provider_response_model"] = provider_model
                    formatted["provider_usage_present"] = isinstance(usage, dict)
                    if isinstance(usage, dict):
                        usage_keys = {
                            "input_tokens": ("prompt_tokens", "input_tokens"),
                            "output_tokens": ("completion_tokens", "output_tokens"),
                            "total_tokens": ("total_tokens",),
                        }
                        for result_key, candidates in usage_keys.items():
                            for usage_key in candidates:
                                raw_value = usage.get(usage_key)
                                if isinstance(raw_value, bool) or not isinstance(
                                    raw_value, (int, float, str)
                                ):
                                    continue
                                try:
                                    parsed_value = int(raw_value)
                                except (TypeError, ValueError, OverflowError):
                                    continue
                                if parsed_value >= 0:
                                    formatted[result_key] = parsed_value
                                    break
                        formatted["usage_status"] = "reported"
                if (
                    formatted.get("status") == "failed"
                    and formatted.get("error_kind") == "invalid_response"
                    and response_format == "json"
                    and can_start_attempt()
                ):
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(
                            f"API returned malformed JSON; retrying structured request in {wait_time:.1f}s..."
                        )
                    time.sleep(wait_time)
                    last_failure = formatted
                    continue

                return finish(formatted)

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
                    if not strict_retry_budget:
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
                    if not strict_retry_budget:
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
                        if not strict_retry_budget:
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
                    return finish(last_failure)

                if can_start_attempt():
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(f"{_response_error_details(response, limit=200)}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue

                if logger:
                    logger.error(f"API调用最终失败: {last_failure['message']}")
                return finish(last_failure)

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
                    return finish(last_failure)

                if can_start_attempt():
                    wait_time = 2 * (2 ** (attempt - 1))
                    if logger:
                        logger.warning(f"API调用失败 ({error_kind}): {message}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue

                if logger:
                    logger.error(f"API调用最终失败 ({error_kind}): {message}")
                return finish(last_failure)

        return finish(last_failure)

    except Exception as exc:
        if logger:
            logger.error(f"调用API失败: {exc}")
        error_kind, message = _classify_exception(exc)
        return finish(_api_result(status="failed", error_kind=error_kind, message=message))


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
    provider_runtime: Optional[ProviderRuntime] = None,
    provider_route: Optional[str] = None,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Call the transport only after bound-runtime and complete-budget admission."""

    if provider_runtime is None:
        return _api_result(
            status="failed",
            error_kind="fatal_config_or_auth",
            message="a bound ProviderRuntime is required for production model calls",
        )

    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    admitted_user_content, admission_omissions = _admit_local_images_to_budget(
        user_content,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    request_payload = canonical_provider_request_payload(
        prompt=prompt,
        system_prompt=system_prompt,
        user_content=admitted_user_content,
        response_format=response_format,
        max_output_tokens=int(max_tokens),
        temperature=temperature,
    )
    capability = resolve_model_capability(api_config)
    profile = ProviderContextProfile.conservative(
        provider=str(api_config.get("provider_family") or capability.provider_family),
        model=str(api_config.get("model") or ""),
        endpoint_type=str(api_config.get("endpoint_type") or capability.endpoint_type),
        model_context_limit=_positive_int(api_config.get("max_context_tokens"), 128_000),
        max_output_tokens=max(1, int(max_tokens)),
        reasoning_reserve=max(0, _positive_int(api_config.get("reasoning_reserve_tokens"), 0)),
        safety_margin=max(0, _positive_int(api_config.get("safety_margin_tokens"), 256)),
    )
    budget = profile.estimate_request(request_payload)
    if not budget["within_budget"]:
        receipt = provider_runtime.blocked_receipt(
            prompt=prompt,
            input_payload=request_payload,
            api_config=api_config,
            message=(
                "provider input exceeds verified context budget: "
                f"{budget['estimated_input_tokens']} > {budget['input_budget']}"
            ),
            route=provider_route,
        )
        blocked = _api_result(
            status="failed",
            error_kind="budget_exhausted",
            message="provider input exceeds verified context budget",
        )
        blocked["provider_receipt"] = receipt.to_dict()
        blocked["transport_metadata"] = {
            "successful_input_mode": "text_only",
            "images_actually_sent_count": 0,
        }
        return blocked

    estimated_tokens = int(budget["estimated_input_tokens"])
    try:
        admission = provider_runtime.admit(estimated_tokens=estimated_tokens)
    except ProviderBudgetExceeded as exc:
        receipt = provider_runtime.blocked_receipt(
            prompt=prompt,
            input_payload=request_payload,
            api_config=api_config,
            message=str(exc),
            route=provider_route,
        )
        blocked = _api_result(
            status="failed",
            error_kind="budget_exhausted",
            message=str(exc),
        )
        blocked["provider_receipt"] = receipt.to_dict()
        blocked["transport_metadata"] = {
            "successful_input_mode": "text_only",
            "images_actually_sent_count": 0,
        }
        return blocked

    result = _call_ai_api_detailed_uninstrumented(
        prompt,
        api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        logger=logger,
        user_content=admitted_user_content,
        retry_attempts=retry_attempts,
        timeout_seconds=timeout_seconds,
        max_retries_per_call=provider_runtime.budget.max_retries_per_call,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    _normalized_content, transport_report = _normalize_user_message_content_with_report(
        prompt,
        admitted_user_content,
        logger=logger,
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    transport_report["omissions"] = [
        *admission_omissions,
        *list(transport_report.get("omissions") or []),
    ]
    planned_ids = [
        str(value)
        for value in (transport_report.get("planned_visual_ids") or [])
        if str(value)
    ]
    group_reports = {
        str(item.get("group_id") or ""): dict(item)
        for item in (transport_report.get("raw_reinspection_groups") or [])
        if isinstance(item, Mapping) and str(item.get("group_id") or "")
    }
    for omission in admission_omissions:
        if not isinstance(omission, Mapping):
            continue
        visual_id = str(omission.get("visual_id") or "")
        if visual_id and visual_id not in planned_ids:
            planned_ids.append(visual_id)
        group_id = str(omission.get("raw_reinspection_group_id") or "").strip()
        if not group_id or group_id in group_reports:
            continue
        candidate_ids = [
            str(value)
            for value in (omission.get("raw_reinspection_planned_ids") or [])
            if str(value)
        ]
        group_reports[group_id] = {
            "group_id": group_id,
            "page_no": int(omission.get("page_no") or 0),
            "ambiguous_candidate_ids": candidate_ids,
            "resolution": str(
                omission.get("raw_reinspection_resolution") or "not_represented"
            ),
            "selected_ids": [],
            "actual_sent_ids": [],
            "transport_status": "not_sent",
            "fallback_reason": str(
                omission.get("raw_reinspection_fallback_reason") or ""
            ),
        }
    transport_report["planned_visual_ids"] = planned_ids
    transport_report["images_planned_count"] = len(planned_ids)
    transport_report["raw_reinspection_groups"] = list(group_reports.values())
    result = {
        **dict(result),
        "transport_metadata": {
            **transport_report,
            "successful_input_mode": "multimodal" if transport_report.get("images_actually_sent_count", 0) else "text_only",
        },
    }
    receipt = provider_runtime.complete(
        admission=admission,
        prompt=prompt,
        input_payload=request_payload,
        api_config=api_config,
        result=result,
        metadata={
            "request_budget": budget,
            "requested_output_tokens": int(max_tokens),
            **dict(result.get("transport_metadata") or {}),
        },
        route=provider_route,
    )
    enriched = dict(result)
    enriched["provider_receipt"] = receipt.to_dict()
    return enriched


def _call_ai_api(prompt: str, api_config: APIConfig, system_prompt: str, max_tokens: int = 4000,
                 temperature: float = 0.3, response_format: str = "json", logger: Any = None,
                 user_content: Any = None, retry_attempts: Optional[int] = None,
                 timeout_seconds: Optional[int] = None,
                 provider_runtime: Optional[ProviderRuntime] = None) -> Optional[Dict[str, Any]]:
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
        provider_runtime=provider_runtime,
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
    provider_runtime: Optional[ProviderRuntime] = None,
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
        provider_runtime=provider_runtime,
    )
    if result.get("status") == "success":
        return {
            "content": result.get("content", ""),
            "finish_reason": result.get("finish_reason", "stop"),
            "http_status": result.get("http_status"),
            "provider_receipt": result.get("provider_receipt"),
        }
    return {
        "content": None,
        "finish_reason": None,
        "http_status": result.get("http_status"),
        "error_kind": result.get("error_kind"),
        "message": result.get("message", ""),
        "provider_receipt": result.get("provider_receipt"),
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
    raise RuntimeError("Concept Mode is not yet available in the current PR14 runtime")


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
    raise RuntimeError("Concept Mode is not yet available in the current PR14 runtime")


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
    timeout_seconds: Optional[int] = None,
    provider_runtime: Optional[ProviderRuntime] = None,
    system_prompt: Optional[str] = None,
    normalize_summary: bool = True,
    max_single_image_bytes: Optional[int] = None,
    max_request_image_bytes: Optional[int] = None,
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
        max_tokens = int(api_config.get('max_output_tokens', 3000 if engine_type == 'primary' else 8192))
        temperature = float(api_config.get('temperature', 0.3))
    except (ValueError, TypeError) as exc:
        if logger:
            logger.warning(f"Failed to read API parameters, using defaults: {exc}")
        max_tokens = 3000
        temperature = 0.3

    system_prompt = system_prompt or _load_stage1_system_prompt(logger)

    request_api_config: APIConfig = {
        **api_config,
        'api_key': api_key,
        'model': model_name,
        'api_base': api_base,
    }
    effective_user_content = user_content
    if engine_type != "primary" or not detect_multimodal_capability(request_api_config).supports_image_input:
        effective_user_content = _text_only_user_content(user_content)

    detailed = _call_ai_api_detailed(
        prompt_text,
        request_api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
        user_content=effective_user_content,
        retry_attempts=retry_attempts,
        timeout_seconds=timeout_seconds,
        provider_runtime=provider_runtime,
        provider_route="Backup_Reader_API" if engine_type == "backup" else "Primary_Reader_API",
        max_single_image_bytes=max_single_image_bytes,
        max_request_image_bytes=max_request_image_bytes,
    )
    detailed["engine_type"] = engine_type

    if detailed.get("status") != "success":
        return detailed

    ai_response = detailed.get("content")
    if isinstance(ai_response, dict):
        detailed["content"] = normalize_ai_summary(ai_response) if normalize_summary else dict(ai_response)
        return detailed
    if ai_response:
        if logger:
            logger.warning("AI returned non-dict content; current summary JSON is required")
        return _api_result(
            status="failed",
            error_kind="invalid_response",
            message="AI response must be a summary_v2_lite canonical JSON object",
            engine_type=engine_type,
        )
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
                                      skip_engines: Optional[Set[str]] = None,
                                      provider_runtime: Optional[ProviderRuntime] = None,
                                      system_prompt: Optional[str] = None,
                                      normalize_summary: bool = True,
                                      max_single_image_bytes: Optional[int] = None,
                                      max_request_image_bytes: Optional[int] = None) -> Optional[Dict[str, Any]]:
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
    primary_failure_reason = ""

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
                provider_runtime=provider_runtime,
                system_prompt=system_prompt,
                normalize_summary=normalize_summary,
                max_single_image_bytes=max_single_image_bytes,
                max_request_image_bytes=max_request_image_bytes,
            )
            last_result = result
            if result.get("status") == "success":
                if engine != "primary":
                    result = {
                        **dict(result),
                        "fallback_reason": primary_failure_reason or "primary_reader_failed_before_backup",
                    }
                return result if return_detailed else result.get("content")

            error_kind = str(result.get("error_kind") or "")
            if engine == "primary":
                primary_failure_reason = str(
                    result.get("message") or result.get("error_kind") or "primary_reader_failed"
                )[:240]
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
                       config: Optional[Dict[str, Any]] = None, user_content: Any = None,
                       system_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
                max_tokens = int(primary_api_config.get('max_output_tokens', 3000))
                temperature = float(primary_api_config.get('temperature', 0.3))
            else:  # backup
                max_tokens = int(backup_api_config.get('max_output_tokens', 8192))
                temperature = float(backup_api_config.get('temperature', 0.3))
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
    system_prompt = system_prompt or _load_stage1_system_prompt(logger)

    # 使用统一的API调用函数
    effective_user_content = user_content
    if not detect_multimodal_capability(api_config).supports_image_input:
        effective_user_content = _text_only_user_content(user_content)
    ai_response = _call_ai_api(
        prompt_text,
        api_config,
        system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format="json",
        logger=logger,
        user_content=effective_user_content,
    )

    if not ai_response:
        return None
    if isinstance(ai_response, dict):  # type: ignore
        structured_summary = normalize_ai_summary(ai_response)
        return structured_summary
    if logger:
        logger.warning("AI返回非字典格式，尝试手动解析")
    if logger:
        logger.warning("AI returned non-dict content; canonical summary JSON is required")
    return None

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

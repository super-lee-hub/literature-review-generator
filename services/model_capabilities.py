"""Model/provider capability resolution for API transport selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Literal, Optional, Set

from models import APIConfig

EndpointType = Literal["chat_completions", "responses"]
ProviderFamily = Literal["aihubmix_openai", "aihubmix_claude", "deepseek", "generic"]
ReasoningParamStyle = Literal["responses_reasoning", "chat_reasoning", "deepseek_thinking", "none"]


@dataclass(frozen=True)
class ModelCapability:
    endpoint_type: EndpointType = "chat_completions"
    provider_family: ProviderFamily = "generic"
    supports_reasoning: bool = False
    supports_pdf_file_input: bool = False
    reasoning_param_style: ReasoningParamStyle = "none"
    highest_reasoning_effort: str = ""
    max_token_param: str = "max_tokens"
    supports_text_verbosity: bool = False
    disallowed_when_reasoning: Set[str] = field(default_factory=set)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _lower(value) in {"1", "true", "yes", "y", "on", "enabled", "enable"}


def _normalize_endpoint(value: Any) -> EndpointType:
    endpoint = _lower(value).replace("-", "_")
    if endpoint in {"responses", "response"}:
        return "responses"
    return "chat_completions"


def _infer_provider_family(api_config: APIConfig) -> ProviderFamily:
    configured = _lower(api_config.get("provider_family")).replace("-", "_")
    if configured in {"aihubmix_openai", "aihubmix_claude", "deepseek", "generic"}:
        return configured  # type: ignore[return-value]

    api_base = _lower(api_config.get("api_base"))
    model = _lower(api_config.get("model"))
    if "api.deepseek.com" in api_base or model.startswith("deepseek-"):
        return "deepseek"
    if "aihubmix.com" in api_base:
        if "claude" in model or "opus" in model:
            return "aihubmix_claude"
        if model.startswith("gpt-"):
            return "aihubmix_openai"
    return "generic"


def resolve_model_capability(api_config: APIConfig) -> ModelCapability:
    """Resolve transport and reasoning behavior from explicit config plus safe inference."""

    provider_family = _infer_provider_family(api_config)
    endpoint_type = _normalize_endpoint(api_config.get("endpoint_type"))
    model = _lower(api_config.get("model"))
    explicit_pdf_input = _truthy(api_config.get("supports_pdf_file_input")) or _truthy(api_config.get("pdf_file_input"))
    official_openai_host = "api.openai.com" in _lower(api_config.get("api_base"))

    if provider_family == "aihubmix_openai" and endpoint_type == "responses":
        return ModelCapability(
            endpoint_type="responses",
            provider_family=provider_family,
            supports_reasoning=True,
            supports_pdf_file_input=explicit_pdf_input,
            reasoning_param_style="responses_reasoning",
            highest_reasoning_effort="high",
            max_token_param="max_output_tokens",
            supports_text_verbosity=True,
            disallowed_when_reasoning={"temperature", "top_p"},
        )

    if provider_family == "aihubmix_claude":
        return ModelCapability(
            endpoint_type="chat_completions",
            provider_family=provider_family,
            supports_reasoning=True,
            supports_pdf_file_input=explicit_pdf_input,
            reasoning_param_style="chat_reasoning",
            highest_reasoning_effort="xhigh",
            max_token_param="max_tokens",
            disallowed_when_reasoning={"temperature", "top_p"},
        )

    if provider_family == "deepseek":
        return ModelCapability(
            endpoint_type="chat_completions",
            provider_family=provider_family,
            supports_reasoning=True,
            supports_pdf_file_input=False,
            reasoning_param_style="deepseek_thinking",
            highest_reasoning_effort="max",
            max_token_param="max_tokens",
            disallowed_when_reasoning={"temperature", "top_p", "presence_penalty", "frequency_penalty"},
        )

    return ModelCapability(
        endpoint_type=endpoint_type,
        provider_family=provider_family,
        supports_pdf_file_input=bool(explicit_pdf_input and endpoint_type == "responses" and official_openai_host),
    )


def _configured_reasoning_effort(api_config: APIConfig, capability: ModelCapability) -> str:
    configured = _text(api_config.get("reasoning_effort"))
    if _truthy(api_config.get("force_highest_reasoning")) or _lower(configured) == "auto_highest":
        return capability.highest_reasoning_effort
    return configured or capability.highest_reasoning_effort


def _normalize_thinking_payload(value: Any) -> Optional[Dict[str, str]]:
    if isinstance(value, dict):
        thinking_type = _lower(value.get("type"))
        if thinking_type in {"enabled", "disabled"}:
            return {"type": thinking_type}
        return None

    text = _text(value)
    lowered = text.casefold()
    if not lowered:
        return None
    if lowered in {"enabled", "enable", "on", "true", "yes", "1"}:
        return {"type": "enabled"}
    if lowered in {"disabled", "disable", "off", "false", "no", "0"}:
        return {"type": "disabled"}
    return None


def is_reasoning_active(payload: Dict[str, Any], capability: ModelCapability) -> bool:
    if capability.reasoning_param_style == "responses_reasoning":
        return bool(payload.get("reasoning"))
    if capability.reasoning_param_style == "chat_reasoning":
        return bool(payload.get("reasoning"))
    if capability.reasoning_param_style == "deepseek_thinking":
        return bool(payload.get("thinking") or payload.get("reasoning_effort"))
    return False


def apply_reasoning_policy(
    payload: Dict[str, Any],
    api_config: APIConfig,
    capability: ModelCapability,
    *,
    logger: Any = None,
) -> None:
    """Apply provider-specific reasoning params without guessing for generic providers."""

    if not capability.supports_reasoning:
        return

    effort = _configured_reasoning_effort(api_config, capability)
    if capability.reasoning_param_style == "responses_reasoning":
        if effort:
            payload["reasoning"] = {"effort": effort}
        verbosity = _text(api_config.get("text_verbosity"))
        if verbosity and capability.supports_text_verbosity:
            text_payload = payload.get("text")
            if not isinstance(text_payload, dict):
                text_payload = {}
                payload["text"] = text_payload
            text_payload["verbosity"] = verbosity
    elif capability.reasoning_param_style == "chat_reasoning":
        if effort:
            reasoning: Dict[str, Any] = {"effort": effort}
            display = _text(api_config.get("reasoning_display"))
            if display:
                reasoning["display"] = display
            payload["reasoning"] = reasoning
    elif capability.reasoning_param_style == "deepseek_thinking":
        thinking_payload = _normalize_thinking_payload(api_config.get("thinking")) or {"type": "enabled"}
        payload["thinking"] = thinking_payload
        if effort:
            payload["reasoning_effort"] = effort

    if is_reasoning_active(payload, capability) and (
        _truthy(api_config.get("omit_temperature_when_reasoning"))
        or bool(capability.disallowed_when_reasoning)
    ):
        for key in capability.disallowed_when_reasoning:
            payload.pop(key, None)


def remove_payload_path(payload: Dict[str, Any], path: Iterable[str]) -> bool:
    keys = list(path)
    if not keys:
        return False
    target: Any = payload
    for key in keys[:-1]:
        if not isinstance(target, dict) or key not in target:
            return False
        target = target[key]
    if isinstance(target, dict) and keys[-1] in target:
        target.pop(keys[-1], None)
        return True
    return False

"""Model/provider capability resolution for API transport selection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Literal, Optional, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

from models import APIConfig

DEFAULT_ANTHROPIC_MESSAGES_PATH = "v1/messages"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

EndpointType = Literal["chat_completions", "responses", "anthropic"]
ProviderFamily = Literal[
    "openai_responses",
    "claude_chat_reasoning",
    "aihubmix_openai",
    "aihubmix_claude",
    "anthropic",
    "deepseek",
    "generic",
]
ReasoningParamStyle = Literal[
    "responses_reasoning",
    "chat_reasoning",
    "deepseek_thinking",
    "anthropic_thinking",
    "none",
]


@dataclass(frozen=True)
class ModelCapability:
    endpoint_type: EndpointType = "chat_completions"
    provider_family: ProviderFamily = "generic"
    supports_reasoning: bool = False
    supports_pdf_file_input: bool = False
    reasoning_param_style: ReasoningParamStyle = "none"
    highest_reasoning_effort: str = ""
    max_token_param: str = "max_tokens"
    max_output_tokens: int | None = None
    supports_text_verbosity: bool = False
    disallowed_when_reasoning: Set[str] = field(default_factory=set)
    # Anthropic only. How this model wants its thinking configured:
    # "manual" (enabled + budget_tokens), "adaptive" (adaptive + output_config.effort),
    # or "none" (send no thinking block at all).
    anthropic_thinking_mode: str = "none"
    anthropic_effort_levels: Tuple[str, ...] = ()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _lower(value) in {"1", "true", "yes", "y", "on", "enabled", "enable"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


# ---------------------------------------------------------------------------
# Anthropic thinking modes.
#
# Anthropic changed the thinking contract across model generations, and the
# current generation rejects the legacy form outright:
#
#   * Claude 4.5 and earlier that support thinking -- manual extended thinking
#     only (``{"type": "enabled", "budget_tokens": N}``); ``adaptive`` is a 400.
#   * Claude 4.6 -- both modes; manual is deprecated but still succeeds.
#   * Claude 4.7 and later (Opus 4.7/4.8, Opus 5, Sonnet 5, Fable 5, Mythos 5)
#     -- adaptive only; ``{"type": "enabled"}`` is a 400.
#
# This derives the *thinking mode* from the model id. It deliberately does not
# derive the transport protocol: whether an endpoint speaks Anthropic Messages
# at all stays an explicit ``endpoint_type`` setting, because the same model id
# may be served by an Anthropic endpoint or by an OpenAI-compatible gateway.
# ---------------------------------------------------------------------------

ANTHROPIC_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

ANTHROPIC_MANUAL_ONLY_MODELS = frozenset(
    {"opus-4-5", "sonnet-4-5", "haiku-4-5", "opus-4-1", "opus-4", "sonnet-4", "haiku-4"}
)
ANTHROPIC_ADAPTIVE_ONLY_MODELS = frozenset(
    {"opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5", "mythos-5"}
)
ANTHROPIC_BOTH_MODES = frozenset({"opus-4-6", "sonnet-4-6", "mythos-preview"})

# Effort support is narrower than the thinking mode: Opus 4.5 tops out at high,
# and the 4.6 models support max but not xhigh.
ANTHROPIC_EFFORT_LEVELS: dict[str, tuple[str, ...]] = {
    "opus-4-5": ("low", "medium", "high"),
    "opus-4-6": ("low", "medium", "high", "max"),
    "sonnet-4-6": ("low", "medium", "high", "max"),
    "mythos-preview": ("low", "medium", "high", "max"),
}


def anthropic_model_key(model: str) -> str:
    """Reduce an Anthropic model id to its canonical ``family-version`` key.

    ``claude-opus-4-6-20260206`` -> ``opus-4-6``; ``claude-opus-5[1m]`` -> ``opus-5``.
    """

    text = _lower(model).split("[", 1)[0].strip()
    if text.startswith("claude-"):
        text = text[len("claude-"):]
    text = re.sub(r"-\d{6,8}$", "", text)
    return text


def anthropic_thinking_mode(model: str) -> str:
    """Return ``manual``, ``adaptive`` or ``none`` for one model id.

    ``none`` means "do not send a thinking block at all". That is the safe answer
    for a model this mapping does not recognise, which includes non-Claude models
    served through an Anthropic-shaped gateway: sending ``adaptive`` to a bridge
    that has never heard of it would fail the request.
    """

    lowered = _lower(model)
    if lowered and not lowered.startswith("claude-"):
        return "none"
    key = anthropic_model_key(model)
    if key in ANTHROPIC_ADAPTIVE_ONLY_MODELS:
        return "adaptive"
    if key in ANTHROPIC_MANUAL_ONLY_MODELS:
        return "manual"
    if key in ANTHROPIC_BOTH_MODES:
        # Both work here; adaptive is the documented recommendation.
        return "adaptive"
    # An unrecognised *Claude* model is assumed to be current-generation rather
    # than legacy: the current generation rejects the legacy form outright, so
    # guessing adaptive fails far less badly than guessing manual.
    return "adaptive" if key.startswith(("opus-", "sonnet-", "haiku-", "fable-", "mythos-")) else "none"


def anthropic_effort_levels(model: str) -> tuple[str, ...]:
    """Effort levels one model accepts. Empty when effort is not applicable."""

    if anthropic_thinking_mode(model) == "none":
        return ()
    return ANTHROPIC_EFFORT_LEVELS.get(anthropic_model_key(model), ANTHROPIC_EFFORT_LADDER)


def resolve_anthropic_effort(requested: Any, model: str) -> str:
    """Clamp a configured effort onto the levels the model actually accepts.

    Anthropic rejects an unknown effort value outright, so an unsupported level
    is stepped down the ladder rather than forwarded.
    """

    allowed = anthropic_effort_levels(model)
    if not allowed:
        return ""
    value = _text(requested).casefold()
    if not value:
        return ""
    if value == "auto_highest":
        return allowed[-1]
    if value in allowed:
        return value
    try:
        wanted = ANTHROPIC_EFFORT_LADDER.index(value)
    except ValueError:
        return allowed[-1] if _truthy(requested) else ""
    supported = [level for level in allowed if ANTHROPIC_EFFORT_LADDER.index(level) <= wanted]
    return supported[-1] if supported else allowed[0]


# ---------------------------------------------------------------------------
# Canonical Anthropic Messages URL resolution.
#
# The production transport and the "test connection" probe used to build this
# URL in two different places with two different rules. The probe stripped a
# duplicated ``/v1``; the runtime did not. That divergence is worse than a plain
# bug, because it produces the single most confusing failure mode available:
#
#     setup "Test connection" -> PASS
#     the real Outline run     -> 400 / unreachable
#
# Everything that needs the URL therefore goes through one resolver.
# ---------------------------------------------------------------------------


def _path_segments(value: str) -> list[str]:
    return [segment for segment in str(value or "").split("/") if segment]


def resolve_anthropic_messages_url(api_base: str, anthropic_path: str = "") -> str:
    """Join an API base and an Anthropic Messages path exactly once.

    Guarantees, all covered by tests:

    * ``/v1`` is never duplicated, whether the base carries it or the path does;
    * ``/messages`` is never duplicated when the base already points at it;
    * a custom explicit path is preserved rather than rewritten;
    * the resolved URL keeps the caller's scheme, host, port and userinfo, so
      the host that was configured is the host that is actually requested.

    The join is an overlap splice rather than a strip-and-append: the longest
    suffix of the base path that is also a prefix of the configured path is
    collapsed. Stripping unconditionally would break a gateway whose base ends
    in a *different* segment that merely looks like a version, and would also
    mangle an operator-supplied custom path.
    """

    base_text = str(api_base or "").strip()
    path_segments = _path_segments(anthropic_path) or _path_segments(DEFAULT_ANTHROPIC_MESSAGES_PATH)
    if not path_segments:
        return base_text

    parsed = urlsplit(base_text)
    base_segments = _path_segments(parsed.path) if (parsed.scheme and parsed.netloc) else _path_segments(base_text)

    overlap = 0
    for size in range(min(len(base_segments), len(path_segments)), 0, -1):
        if base_segments[len(base_segments) - size:] == path_segments[:size]:
            overlap = size
            break

    merged = base_segments[: len(base_segments) - overlap] + path_segments

    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(merged), parsed.query, ""))
    # No scheme/host to preserve: the validator rejects such a base anyway, so
    # the join stays textual and simply must not duplicate segments.
    joined = "/".join(merged)
    return f"/{joined}" if base_text.startswith("/") else joined


# ---------------------------------------------------------------------------
# Anthropic sampling parameters.
#
# Quoted from the current Anthropic API usage primer:
#
#   "Temperature must be set to 1 (or left unset) whenever thinking is enabled,
#    on all models."
#
#   "On Claude 4.7 and later models and Claude Mythos Preview, ``temperature``
#    is deprecated and only its default value is accepted, even when thinking is
#    off."
#
# The second sentence is the one that matters here: turning thinking *off* does
# not restore a custom temperature on the current generation. A test that
# asserted "thinking disabled -> temperature kept" was therefore locking in a
# request the provider is documented to reject.
# ---------------------------------------------------------------------------

# Mythos Preview is named explicitly by the deprecation note even though its
# version stamp does not place it in the 4.7+ series.
ANTHROPIC_TEMPERATURE_DEPRECATED_KEYS = frozenset({"mythos-preview"})
_ANTHROPIC_VERSION_KEY_RE = re.compile(r"^(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d+))?$")


def anthropic_temperature_deprecated(model: str) -> bool:
    """Whether this Claude generation accepts only the default temperature."""

    if anthropic_thinking_mode(model) == "none":
        return False
    key = anthropic_model_key(model)
    if key in ANTHROPIC_TEMPERATURE_DEPRECATED_KEYS:
        return True
    match = _ANTHROPIC_VERSION_KEY_RE.match(key)
    if match is None:
        return False
    return (int(match.group("major")), int(match.group("minor") or 0)) >= (4, 7)


def anthropic_thinking_will_be_active(api_config: APIConfig, capability: ModelCapability) -> bool:
    """Whether the request this config produces will carry an active thinking block."""

    if capability.anthropic_thinking_mode == "none":
        return False
    return _text(api_config.get("thinking")).casefold() != "disabled"


def anthropic_temperature_allowed(model: str, *, thinking_active: bool) -> bool:
    """Whether a caller-supplied ``temperature`` may be sent to this model.

    Three cases, in the order the current contract decides them:

    * thinking active  -> temperature must be 1 or unset, on every model;
    * current generation (4.7+, Mythos Preview) -> deprecated regardless of
      thinking, so thinking=disabled does not bring it back;
    * an unrecognised model on an Anthropic-shaped endpoint -> withheld. There
      is no generation evidence to relax on, and this repository resolves that
      kind of ambiguity fail-closed rather than optimistically.
    """

    if anthropic_thinking_mode(model) == "none":
        return False
    if thinking_active:
        return False
    return not anthropic_temperature_deprecated(model)


def _normalize_endpoint(value: Any) -> EndpointType:
    endpoint = _lower(value).replace("-", "_")
    if endpoint in {"anthropic", "anthropic_messages", "messages"}:
        return "anthropic"
    if endpoint in {"responses", "response"}:
        return "responses"
    return "chat_completions"


def _infer_provider_family(api_config: APIConfig) -> ProviderFamily:
    configured = _lower(api_config.get("provider_family")).replace("-", "_")
    if configured in {
        "openai_responses",
        "claude_chat_reasoning",
        "aihubmix_openai",
        "aihubmix_claude",
        "anthropic",
        "deepseek",
        "generic",
    }:
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
    # An explicitly configured Anthropic transport is authoritative.  Model-name
    # inference is deliberately NOT used here: a Claude model id alone cannot
    # tell an Anthropic Messages endpoint from an OpenAI-compatible gateway that
    # proxies Claude, and guessing would silently pick the wrong wire format.
    if _normalize_endpoint(api_config.get("endpoint_type")) == "anthropic":
        return "anthropic"
    return "generic"


def resolve_model_capability(api_config: APIConfig) -> ModelCapability:
    """Resolve transport and reasoning behavior from explicit config plus safe inference."""

    provider_family = _infer_provider_family(api_config)
    endpoint_type = _normalize_endpoint(api_config.get("endpoint_type"))
    model = _lower(api_config.get("model"))
    explicit_pdf_input = _truthy(api_config.get("supports_pdf_file_input")) or _truthy(api_config.get("pdf_file_input"))
    official_openai_host = "api.openai.com" in _lower(api_config.get("api_base"))

    if provider_family in {"openai_responses", "aihubmix_openai"} and endpoint_type == "responses":
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

    if provider_family in {"claude_chat_reasoning", "aihubmix_claude"}:
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

    if provider_family == "anthropic" or endpoint_type == "anthropic":
        # Native Anthropic Messages API.  Its wire contract differs from both
        # OpenAI protocols: the system prompt is a top-level field rather than a
        # message, the token limit is max_tokens, extended thinking uses a
        # budget_tokens sub-field, and temperature is rejected while thinking is
        # active.
        thinking_mode = anthropic_thinking_mode(model)
        effort_levels = anthropic_effort_levels(model)
        return ModelCapability(
            endpoint_type="anthropic",
            provider_family="anthropic",
            supports_reasoning=thinking_mode != "none",
            supports_pdf_file_input=False,
            reasoning_param_style="anthropic_thinking",
            # The highest level this model actually accepts. Sending "max" to a
            # model that tops out at "high" would be rejected.
            highest_reasoning_effort=effort_levels[-1] if effort_levels else "",
            max_token_param="max_tokens",
            disallowed_when_reasoning={"temperature", "top_p"},
            anthropic_thinking_mode=thinking_mode,
            anthropic_effort_levels=effort_levels,
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
            max_output_tokens=384_000,
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
    if capability.reasoning_param_style == "anthropic_thinking":
        # Both "enabled" (legacy manual) and "adaptive" (current) mean thinking
        # is on, so temperature must be withheld. Key presence alone would strip
        # temperature from configurations that legitimately allow it.
        thinking = payload.get("thinking")
        if not isinstance(thinking, dict):
            return False
        return str(thinking.get("type") or "").casefold() in {"enabled", "adaptive"}
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
    elif capability.reasoning_param_style == "anthropic_thinking":
        mode = capability.anthropic_thinking_mode
        if mode == "none":
            return

        model_id = str(api_config.get("model") or "")
        disabled = _text(api_config.get("thinking")).casefold() == "disabled"

        if mode == "manual":
            # Legacy manual extended thinking. Correct for Claude 4.5 and
            # earlier; sending it to 4.7+ yields a 400, which is why the mode is
            # derived from the model rather than assumed.
            thinking_payload: Dict[str, Any] = {"type": "disabled"} if disabled else {"type": "enabled"}
            if not disabled:
                # A bare {"type": "enabled"} is legal; Anthropic picks a default
                # budget, so an absent budget is a valid configuration rather
                # than a value to be invented here.
                budget = _positive_int(api_config.get("thinking_budget_tokens"), 0)
                if budget > 0:
                    thinking_payload["budget_tokens"] = budget
            payload["thinking"] = thinking_payload
            return

        # Adaptive thinking: there is no budget_tokens. Depth is effort-driven.
        if disabled:
            # Opus 5 rejects {"type": "disabled"} at xhigh/max, so effort is
            # withheld here rather than producing a guaranteed 400.
            payload["thinking"] = {"type": "disabled"}
            return
        payload["thinking"] = {"type": "adaptive"}
        # The generic fallback is the model's top effort level, which for Opus 5
        # is "max" -- but Anthropic documents the unset default as "high", and
        # max demands a very large max_tokens. Forcing max on every call would be
        # a silent cost and truncation regression, so an unconfigured effort
        # follows the documented default and only force_highest_reasoning (or an
        # explicit auto_highest) climbs the ladder.
        configured_effort = _text(api_config.get("reasoning_effort"))
        force_highest = _truthy(api_config.get("force_highest_reasoning")) or _lower(
            configured_effort
        ) == "auto_highest"
        raw_effort = capability.highest_reasoning_effort if force_highest else (configured_effort or "high")
        resolved = resolve_anthropic_effort(raw_effort, model_id)
        if resolved:
            payload["output_config"] = {"effort": resolved}

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

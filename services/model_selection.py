"""Helpers for resolving model routing with new outline support."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, cast

from models import APIConfig


_API_KEY_PLACEHOLDER_RE = re.compile(r"^(?:your_.+_api_key_here|loaded_from_\.env_file)$", re.IGNORECASE)
_API_CONFIG_OPTIONAL_FIELDS = (
    "endpoint_type",
    "provider_family",
    "thinking",
    "reasoning_effort",
    "reasoning_display",
    "text_verbosity",
    "max_output_tokens",
    "max_context_tokens",
    "temperature",
    "connect_timeout_seconds",
    "read_timeout_seconds",
    "total_timeout_seconds",
    "first_token_timeout_seconds",
    "transport_retries",
    "reasoning_reserve_tokens",
    "safety_margin_tokens",
    "supports_pdf_file_input",
    "pdf_file_input",
    "force_highest_reasoning",
    "omit_temperature_when_reasoning",
    # Native Anthropic Messages transport. Without these in the allow-list the
    # values are dropped between config and runtime, so a documented setting
    # would be accepted by the schema and then have no effect at all.
    "anthropic_path",
    "anthropic_version",
    # Manual extended thinking only (Claude 4.5 and earlier; deprecated but
    # still accepted on 4.6). Adaptive models ignore it, because effort controls
    # depth there.
    "thinking_budget_tokens",
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _has_meaningful_api_key(value: Any) -> bool:
    api_key = _normalize_text(value)
    return bool(api_key) and not _API_KEY_PLACEHOLDER_RE.fullmatch(api_key)


def has_complete_api_route(section: Mapping[str, Any] | None) -> bool:
    """Return whether a standalone API section has an explicit safe route.

    ``_section_to_api_config`` retains legacy normalization for callers that
    intentionally resolve an incomplete/legacy section.  Independent runtime
    entrypoints must check the raw section first, otherwise a missing
    ``api_base`` would silently become the unrelated OpenAI default.  The
    endpoint type remains optional here because the capability resolver has a
    documented model/provider inference path; when it is supplied, the global
    configuration validator owns its compatibility checks.
    """

    section = section or {}
    return (
        _has_meaningful_api_key(section.get("api_key"))
        and bool(_normalize_text(section.get("model")))
        and bool(_normalize_text(section.get("api_base")))
    )


def _section_has_effective_route(section: Dict[str, Any] | None) -> bool:
    """Backward-compatible private alias for the raw route completeness check."""

    return has_complete_api_route(section)


def _section_to_api_config(section: Dict[str, Any] | None) -> APIConfig:
    section = section or {}
    api_key = _normalize_text(section.get("api_key"))
    if not _has_meaningful_api_key(api_key):
        api_key = ""
    api_config: Dict[str, Any] = {
        "api_key": api_key,
        "model": _normalize_text(section.get("model")),
        "api_base": _normalize_text(section.get("api_base")) or "https://api.openai.com/v1",
        "proxy_mode": _normalize_text(section.get("proxy_mode")) or "environment",
    }
    for field in _API_CONFIG_OPTIONAL_FIELDS:
        if field in section and _normalize_text(section.get(field)):
            api_config[field] = section.get(field)
    return cast(APIConfig, api_config)


def get_reader_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Primary_Reader_API"))


def get_backup_reader_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Backup_Reader_API"))


def get_writer_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Writer_API"))


def get_outline_api_config(config: Dict[str, Any] | None) -> APIConfig:
    """Resolve ``[Outline_API]`` on its own, with no Writer fallback.

    Role-aware routing treats an API section as the route authority: the
    section's ``api_base``, ``endpoint_type`` and ``provider_family`` are one
    indivisible wire contract. The old fallback copied just the model and
    address from ``[Writer_API]`` and left the transport behind, which is how a
    Writer gateway ended up being addressed with the Anthropic Messages
    protocol. An incomplete section is now an incomplete section.
    """

    return _section_to_api_config((config or {}).get("Outline_API"))


def get_free_mode_api_config(config: Dict[str, Any] | None) -> APIConfig:
    """Resolve ``[Free_Mode_API]`` on its own, with no Outline fallback."""

    return _section_to_api_config((config or {}).get("Free_Mode_API"))


def get_validator_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Validator_API"))


def get_api_config_for_section(config: Dict[str, Any] | None, section_name: str) -> APIConfig:
    """Resolve any API section by name.

    Used by Outline role routing so each ``[OutlineModels]`` role can select its
    own section without duplicating the normalization rules above.
    """

    return _section_to_api_config((config or {}).get(str(section_name or "").strip()))

"""Helpers for resolving model routing with new outline support."""

from __future__ import annotations

import re
from typing import Any, Dict, cast

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
)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _has_meaningful_api_key(value: Any) -> bool:
    api_key = _normalize_text(value)
    return bool(api_key) and not _API_KEY_PLACEHOLDER_RE.fullmatch(api_key)


def _section_has_effective_route(section: Dict[str, Any] | None) -> bool:
    section = section or {}
    return _has_meaningful_api_key(section.get("api_key")) and bool(_normalize_text(section.get("model")))


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
    outline_section = (config or {}).get("Outline_API")
    if _section_has_effective_route(outline_section):
        return _section_to_api_config(outline_section)
    return get_writer_api_config(config)


def get_free_mode_api_config(config: Dict[str, Any] | None) -> APIConfig:
    free_mode_section = (config or {}).get("Free_Mode_API")
    if _section_has_effective_route(free_mode_section):
        return _section_to_api_config(free_mode_section)
    return get_outline_api_config(config)


def get_validator_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Validator_API"))


def get_api_config_for_section(config: Dict[str, Any] | None, section_name: str) -> APIConfig:
    """Resolve any API section by name.

    Used by Outline role routing so each ``[OutlineModels]`` role can select its
    own section without duplicating the normalization rules above.
    """

    return _section_to_api_config((config or {}).get(str(section_name or "").strip()))

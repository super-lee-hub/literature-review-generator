"""Helpers for resolving model routing with new outline support."""

from __future__ import annotations

from typing import Any, Dict

from models import APIConfig


def _section_to_api_config(section: Dict[str, Any] | None) -> APIConfig:
    section = section or {}
    return {
        "api_key": section.get("api_key") or "",
        "model": section.get("model") or "",
        "api_base": section.get("api_base") or "https://api.openai.com/v1",
    }


def get_reader_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Primary_Reader_API"))


def get_backup_reader_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Backup_Reader_API"))


def get_writer_api_config(config: Dict[str, Any] | None) -> APIConfig:
    return _section_to_api_config((config or {}).get("Writer_API"))


def get_outline_api_config(config: Dict[str, Any] | None) -> APIConfig:
    outline_config = _section_to_api_config((config or {}).get("Outline_API"))
    if outline_config.get("model"):
        return outline_config
    return get_writer_api_config(config)


def get_free_mode_api_config(config: Dict[str, Any] | None) -> APIConfig:
    free_mode_config = _section_to_api_config((config or {}).get("Free_Mode_API"))
    if free_mode_config.get("api_key") and free_mode_config.get("model"):
        return free_mode_config
    return get_outline_api_config(config)

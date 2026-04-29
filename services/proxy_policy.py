"""HTTP proxy policy helpers for API calls."""

from __future__ import annotations

from typing import Any, Mapping


PROXY_MODE_ENVIRONMENT = "environment"
PROXY_MODE_DIRECT = "direct"

_DIRECT_PROXY_ALIASES = {
    "direct",
    "none",
    "off",
    "disabled",
    "disable",
    "bypass",
    "no_proxy",
    "no-proxy",
    "false",
    "0",
}


def normalize_proxy_mode(value: Any) -> str:
    """Normalize proxy mode values from config/GUI into a stable enum."""

    raw = str(value or "").strip().lower()
    if raw in _DIRECT_PROXY_ALIASES:
        return PROXY_MODE_DIRECT
    return PROXY_MODE_ENVIRONMENT


def should_bypass_environment_proxy(api_config: Mapping[str, Any] | None) -> bool:
    """Return True when this API call should ignore HTTP(S)_PROXY env vars."""

    if not api_config:
        return False
    return normalize_proxy_mode(api_config.get("proxy_mode")) == PROXY_MODE_DIRECT

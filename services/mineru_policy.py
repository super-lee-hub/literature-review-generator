"""Shared, strict MinerU destination and URL policy helpers.

The trust-admission projection and the actual preprocessing transport must
derive the same effective artifact-host set.  Keeping the small policy in a
dependency-light module avoids importing the transport implementation from
the admission layer and prevents either side from silently adding defaults.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any
from urllib.parse import urlsplit


DEFAULT_MINERU_ALLOWED_URL_HOSTS = frozenset(
    {
        "mineru.oss-cn-shanghai.aliyuncs.com",
        "cdn-mineru.openxlab.org.cn",
    }
)

_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_exact_mineru_host(value: Any) -> str:
    """Return one exact DNS host or ``""`` for an unsafe value.

    Configuration is intentionally host-only.  URL-shaped values are rejected
    here; acknowledgement parsing has a separate URL compatibility branch and
    reduces an approved URL to this same host-only representation.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return ""
    if not raw or "/" in raw or "@" in raw or ":" in raw or any(
        ord(char) < 32 for char in raw
    ):
        return ""
    host = raw.casefold()
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return ""
    labels = host.split(".")
    if not labels or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return ""
    return host


def _host_values(value: Any) -> Iterable[Any]:
    if isinstance(value, str):
        return value.split(",")
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    if value is None:
        return ()
    return (value,)


def effective_mineru_allowed_url_hosts(
    value: Any,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(effective_hosts, invalid_configured_values)`` exactly once.

    The shipped defaults are part of the effective policy even when the
    configured value is empty or contains custom hosts.  Callers must consume
    this result as-is; they must not append defaults later in the transport.
    """

    valid: set[str] = set()
    invalid: set[str] = set()
    for raw in _host_values(value):
        candidate = str(raw or "").strip().casefold()
        if not candidate:
            continue
        normalized = normalize_exact_mineru_host(candidate)
        if normalized:
            valid.add(normalized)
        else:
            invalid.add(candidate)
    return frozenset(DEFAULT_MINERU_ALLOWED_URL_HOSTS | valid), frozenset(invalid)


def parse_mineru_http_url(value: Any, *, require_https: bool = False):
    """Parse and validate a MinerU HTTP(S) URL before transport construction."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        hostname = str(parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("MinerU URL is malformed") from exc
    if scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("MinerU URL must be an absolute HTTP(S) URL")
    if require_https and scheme != "https":
        raise ValueError("MinerU URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("MinerU URL must not contain userinfo")
    if parsed.fragment:
        raise ValueError("MinerU URL must not contain a fragment")
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("MinerU URL port is outside 1..65535")
    return parsed


__all__ = [
    "DEFAULT_MINERU_ALLOWED_URL_HOSTS",
    "effective_mineru_allowed_url_hosts",
    "normalize_exact_mineru_host",
    "parse_mineru_http_url",
]

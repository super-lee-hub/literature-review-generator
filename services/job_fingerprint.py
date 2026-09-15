from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sanitize_config_for_fingerprint(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a recursive, secret-free configuration projection."""

    secret_exact = {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "credential",
        "userinfo",
    }
    non_secret_token_keys = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "max_tokens",
        "max_output_tokens",
        "max_context_tokens",
        "max_total_tokens",
        "reasoning_tokens",
    }

    def is_secret_key(key: Any) -> bool:
        normalized = str(key or "").casefold().replace("-", "_")
        if normalized in non_secret_token_keys:
            return False
        if normalized in secret_exact:
            return True
        return any(
            normalized.endswith("_" + marker)
            or normalized.startswith(marker + "_")
            for marker in secret_exact
        )

    def clean(value: Any, *, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {
                str(item_key): clean(item_value, key=str(item_key))
                for item_key, item_value in value.items()
                if not is_secret_key(item_key)
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [clean(item, key=key) for item in value]
        return value

    return clean(config if isinstance(config, Mapping) else {})


@dataclass(frozen=True)
class FingerprintInputs:
    config_snapshot: dict[str, Any]
    source_snapshot: dict[str, Any]
    request_snapshot: dict[str, Any]


@dataclass(frozen=True)
class FingerprintBundle:
    config_hash: str
    source_hash: str
    request_hash: str
    combined_hash: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def build_fingerprint_bundle(inputs: FingerprintInputs) -> FingerprintBundle:
    config_hash = _sha256_text(_stable_json(inputs.config_snapshot))
    source_hash = _sha256_text(_stable_json(inputs.source_snapshot))
    request_hash = _sha256_text(_stable_json(inputs.request_snapshot))
    combined_hash = _sha256_text(_stable_json({
        "config_hash": config_hash,
        "source_hash": source_hash,
        "request_hash": request_hash,
    }))
    return FingerprintBundle(
        config_hash=config_hash,
        source_hash=source_hash,
        request_hash=request_hash,
        combined_hash=combined_hash,
    )


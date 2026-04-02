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
    sanitized: dict[str, Any] = {}
    for section_name, section_value in (config or {}).items():
        if not isinstance(section_value, Mapping):
            continue
        sanitized[section_name] = {}
        for key, value in section_value.items():
            if str(key).lower() == "api_key":
                continue
            sanitized[section_name][str(key)] = value
    return sanitized


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


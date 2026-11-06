"""Verified runtime-spec and configuration bindings for Outline execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runtime.job_spec import RuntimeJobSpec
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, file_sha256


_CONFIG_BINDING_SCHEMA = "runtime-config-source/v1"
_CONFIG_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "config_source_id",
        "config_source_sha256",
        "effective_config_sha256",
        "normalized_spec_payload_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class RuntimeSpecBindingError(ValueError):
    """A persisted runtime spec or its accepted configuration binding is invalid."""


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeSpecBindingError(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class RuntimeSpecBindingV1:
    """Hashes read from and verified against the ready runtime-spec artifact."""

    normalized_spec_artifact_id: str
    normalized_spec_artifact_sha256: str
    normalized_spec_payload_sha256: str
    config_source_id: str
    config_source_sha256: str
    effective_config_sha256: str

    def __post_init__(self) -> None:
        if self.normalized_spec_artifact_id != "runtime_job_spec":
            raise RuntimeSpecBindingError("runtime spec artifact identity is invalid")
        if not self.config_source_id:
            raise RuntimeSpecBindingError("config source identity is missing")
        _require_sha256(self.normalized_spec_artifact_sha256, "runtime spec artifact hash")
        _require_sha256(self.normalized_spec_payload_sha256, "runtime spec payload hash")
        _require_sha256(self.config_source_sha256, "config source hash")
        _require_sha256(self.effective_config_sha256, "effective config hash")

    def assert_current_config(self) -> None:
        """Fail if the raw config bytes changed after this binding was read."""

        try:
            current_sha256 = file_sha256(self.config_source_id)
        except OSError as exc:
            raise RuntimeSpecBindingError("bound config source is unavailable") from exc
        if current_sha256 != self.config_source_sha256:
            raise RuntimeSpecBindingError("bound config source changed after acceptance")


def _artifact_payload(record: ArtifactRecord) -> Mapping[str, Any]:
    try:
        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeSpecBindingError("ready runtime_job_spec payload is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeSpecBindingError("ready runtime_job_spec payload must be an object")
    return payload


def read_runtime_spec_binding_v1(
    registry: ArtifactRegistry,
    *,
    expected_effective_config_sha256: str,
) -> RuntimeSpecBindingV1:
    """Read a complete, ready binding; never manufacture missing authority."""

    expected_effective_config_sha256 = _require_sha256(
        expected_effective_config_sha256,
        "expected effective config hash",
    )
    try:
        registry.reload()
        record = registry.get("runtime_job_spec")
    except Exception as exc:
        raise RuntimeSpecBindingError("runtime spec Registry state is unavailable") from exc
    if record is None:
        raise RuntimeSpecBindingError("required runtime_job_spec binding is missing")
    if record.artifact_id != "runtime_job_spec":
        raise RuntimeSpecBindingError("runtime spec artifact identity is invalid")
    if record.status != "ready":
        raise RuntimeSpecBindingError("runtime_job_spec artifact is not ready")
    if record.artifact_type != "runtime_job_spec" or record.artifact_version != "v1":
        raise RuntimeSpecBindingError("runtime_job_spec artifact type or version is invalid")
    if not registry.job_id or record.job_id != registry.job_id:
        raise RuntimeSpecBindingError("runtime_job_spec belongs to another job")

    try:
        record = registry.verify_ready_artifact_closure(record)
    except Exception as exc:
        raise RuntimeSpecBindingError("runtime_job_spec ready closure is invalid") from exc
    if (
        record.status != "ready"
        or record.artifact_id != "runtime_job_spec"
        or record.job_id != registry.job_id
        or record.artifact_type != "runtime_job_spec"
        or record.artifact_version != "v1"
    ):
        raise RuntimeSpecBindingError("verified runtime_job_spec identity changed")
    artifact_sha256 = _require_sha256(record.content_hash, "runtime spec artifact hash")

    payload = _artifact_payload(record)
    try:
        spec = RuntimeJobSpec.from_dict(payload)
        spec.validate()
        normalized_payload = spec.to_dict()
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeSpecBindingError("persisted RuntimeJobSpec is invalid") from exc
    if dict(payload) != normalized_payload:
        raise RuntimeSpecBindingError("persisted RuntimeJobSpec is not normalized")
    if not spec.job_id or spec.job_id != registry.job_id or spec.job_id != record.job_id:
        raise RuntimeSpecBindingError("normalized RuntimeJobSpec belongs to another job")

    binding = record.metadata.get("config_snapshot_binding")
    if not isinstance(binding, Mapping):
        raise RuntimeSpecBindingError("runtime_job_spec config snapshot binding is missing")
    if binding.get("schema_version") != _CONFIG_BINDING_SCHEMA:
        raise RuntimeSpecBindingError("runtime_job_spec config snapshot binding schema is invalid")
    if set(binding) != _CONFIG_BINDING_FIELDS:
        raise RuntimeSpecBindingError("runtime_job_spec config snapshot binding fields are incomplete or unknown")

    try:
        config_path = Path(spec.config).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeSpecBindingError("normalized RuntimeJobSpec config path is invalid") from exc
    config_source_id = binding.get("config_source_id")
    if not isinstance(config_source_id, str) or config_source_id != str(config_path):
        raise RuntimeSpecBindingError("config source identity does not match RuntimeJobSpec.config")
    if not config_path.is_file():
        raise RuntimeSpecBindingError("bound config source is unavailable")

    config_sha256 = _require_sha256(
        binding.get("config_source_sha256"),
        "config source hash",
    )
    try:
        current_config_sha256 = file_sha256(config_path)
    except OSError as exc:
        raise RuntimeSpecBindingError("bound config source is unavailable") from exc
    if current_config_sha256 != config_sha256:
        raise RuntimeSpecBindingError("bound config source hash does not match current bytes")

    effective_config_sha256 = _require_sha256(
        binding.get("effective_config_sha256"),
        "effective config hash",
    )
    if effective_config_sha256 != expected_effective_config_sha256:
        raise RuntimeSpecBindingError("effective config hash does not match the accepted runtime")

    from runtime.provider_runtime import hash_json

    payload_sha256 = _require_sha256(
        binding.get("normalized_spec_payload_sha256"),
        "normalized spec payload hash",
    )
    actual_payload_sha256 = hash_json(normalized_payload)
    if payload_sha256 != actual_payload_sha256:
        raise RuntimeSpecBindingError("normalized spec payload hash does not match persisted payload")

    return RuntimeSpecBindingV1(
        normalized_spec_artifact_id=record.artifact_id,
        normalized_spec_artifact_sha256=artifact_sha256,
        normalized_spec_payload_sha256=actual_payload_sha256,
        config_source_id=str(config_path),
        config_source_sha256=config_sha256,
        effective_config_sha256=effective_config_sha256,
    )

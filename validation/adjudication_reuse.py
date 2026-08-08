"""Typed validation adjudication reuse authority.

Raw checkpoint files remain coordination-only.  A canonical verdict may be
reused only after a Registry-backed reuse record verifies against the current
packet, route, provider output, source receipt, and provider closure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from runtime.provider_runtime import (
    ProviderCallReceiptV1,
    _redact_mapping,
    hash_json,
    hash_text,
)
from services.artifact_registry import ArtifactDependencyRefV2, file_sha256
from validation.adjudication_checkpoint import ADJUDICATION_PROMPT_VERSION
from validation.llm_adjudicator import (
    AdjudicationPacket,
    _build_prompts,
)
from validation.run_result import VALIDATION_RUN_SCHEMA_VERSION


ADJUDICATION_REUSE_ARTIFACT_TYPE = "validation_adjudication_reuse_record"
ADJUDICATION_REUSE_ARTIFACT_VERSION = "v1"
ADJUDICATION_REUSE_VERSION = "validation_adjudication_reuse_record/v1"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def adjudication_packet_hash(packet: AdjudicationPacket | Mapping[str, Any]) -> str:
    payload = dict(packet) if isinstance(packet, Mapping) else packet.__dict__
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def adjudication_node_id(packet: AdjudicationPacket) -> str:
    citation_key = str(packet.citation_set_key or "validation").strip()
    return f"{packet.stage}:{citation_key}"


def adjudication_call_id(packet: AdjudicationPacket) -> str:
    packet_hash = adjudication_packet_hash(packet)[:24]
    return f"validation:{packet.stage}:{packet_hash}"


def adjudication_schema_hash(packet: AdjudicationPacket) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "stage_name": "stage4_validate",
                "route": "Validator_API",
                "node_id": adjudication_node_id(packet),
                "response_format": "json",
            }
        ).encode("utf-8")
    ).hexdigest()


def _request_payload(packet: AdjudicationPacket, api_config: Mapping[str, Any]) -> dict[str, Any]:
    prompt, system_prompt = _build_prompts(packet)
    try:
        base_max_tokens = int(api_config.get("max_output_tokens", 4096))
        base_temperature = float(api_config.get("temperature", 0.2))
    except (TypeError, ValueError):
        base_max_tokens = 4096
        base_temperature = 0.2
    if packet.stage == "stronger":
        max_tokens = max(base_max_tokens, 6144)
        temperature = min(base_temperature, 0.15)
    else:
        max_tokens = base_max_tokens
        temperature = base_temperature
    return {
        "system": system_prompt,
        "user": prompt,
        "user_content": None,
        "response_format": "json",
        "max_output_tokens": int(max_tokens),
        "temperature": temperature,
    }


def build_reuse_key(
    *,
    packet: AdjudicationPacket,
    api_config: Mapping[str, Any],
    input_dependency_hashes: Mapping[str, str],
) -> str:
    identity = {
        "packet_hash": adjudication_packet_hash(packet),
        "stage": packet.stage,
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "validation_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
        "redacted_provider_config_hash": hash_json(_redact_mapping(dict(api_config))),
        "current_input_dependency_hashes": {
            str(key): str(value)
            for key, value in sorted(input_dependency_hashes.items(), key=lambda item: str(item[0]))
        },
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def build_reuse_record_payload(
    *,
    packet: AdjudicationPacket,
    api_config: Mapping[str, Any],
    service: Any,
    output_record: Any,
    receipt: ProviderCallReceiptV1,
    reuse_key: str,
    input_dependency_hashes: Mapping[str, str],
) -> dict[str, Any]:
    prompt, _system_prompt = _build_prompts(packet)
    request_payload = _request_payload(packet, api_config)
    return {
        "artifact_type": ADJUDICATION_REUSE_ARTIFACT_TYPE,
        "artifact_version": ADJUDICATION_REUSE_ARTIFACT_VERSION,
        "reuse_version": ADJUDICATION_REUSE_VERSION,
        "reuse_key": reuse_key,
        "job_id": service.job_id,
        "attempt_id": service.attempt_id,
        "citation_set_key": str(packet.citation_set_key or ""),
        "stage": packet.stage,
        "node_id": adjudication_node_id(packet),
        "call_id": adjudication_call_id(packet),
        "canonical_adjudication_packet_hash": adjudication_packet_hash(packet),
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "validation_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
        "provider": str(api_config.get("provider_family") or "configured"),
        "model": str(api_config.get("model") or ""),
        "endpoint_type": str(api_config.get("endpoint_type") or "chat_completions"),
        "redacted_provider_config_hash": hash_json(_redact_mapping(dict(api_config))),
        "prompt_hash": hash_text(prompt),
        "input_hash": hash_json(request_payload),
        "schema_hash": adjudication_schema_hash(packet),
        "provider_output_artifact_id": output_record.artifact_id,
        "provider_output_artifact_hash": output_record.content_hash,
        "normalized_result_hash": hash_json(output_record_payload(output_record)),
        "source_receipt_id": receipt.receipt_id,
        "source_receipt_hash": hash_json(receipt.to_dict()),
        "source_receipt_ledger_artifact_id": "",
        "source_receipt_ledger_artifact_hash": "",
        "source_provider_closure_epoch_id": service.closure_epoch_id,
        "source_provider_closure_artifact_id": "",
        "source_provider_closure_artifact_hash": "",
        "current_input_dependency_hashes": {
            str(key): str(value)
            for key, value in sorted(input_dependency_hashes.items(), key=lambda item: str(item[0]))
        },
    }


def output_record_payload(output_record: Any) -> Mapping[str, Any]:
    try:
        raw = json.loads(Path(output_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"provider output artifact is unreadable: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("provider output artifact must be a JSON object")
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else raw


def _receipts_from_ledger(registry: Any) -> list[ProviderCallReceiptV1]:
    receipts: list[ProviderCallReceiptV1] = []
    for record in registry.list_records():
        if record.artifact_type != "provider_receipt_ledger" or record.status != "ready":
            continue
        try:
            for line in Path(record.path).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                if isinstance(raw, Mapping):
                    receipts.append(ProviderCallReceiptV1.from_dict(raw))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return receipts


def _source_receipt_verified(
    service: Any,
    record_payload: Mapping[str, Any],
) -> bool:
    receipt_id = str(record_payload.get("source_receipt_id") or "")
    expected_hash = str(record_payload.get("source_receipt_hash") or "")
    if not receipt_id or not expected_hash:
        return False
    candidates: list[ProviderCallReceiptV1] = []
    try:
        candidates.extend(service.provider_receipt_ledger.list_receipts())
    except (OSError, TypeError, ValueError):
        pass
    candidates.extend(_receipts_from_ledger(service.artifact_registry))
    for receipt in candidates:
        if receipt.receipt_id == receipt_id and hash_json(receipt.to_dict()) == expected_hash:
            return True
    return False


def _source_closure_verified(
    registry: Any,
    record_payload: Mapping[str, Any],
    *,
    current_epoch: str,
) -> bool:
    source_epoch = str(record_payload.get("source_provider_closure_epoch_id") or "")
    closure_id = str(record_payload.get("source_provider_closure_artifact_id") or "")
    closure_hash = str(record_payload.get("source_provider_closure_artifact_hash") or "")
    if not closure_id or not closure_hash:
        return bool(source_epoch and source_epoch == current_epoch)
    record = registry.get(closure_id)
    if record is None or record.status != "ready" or record.content_hash != closure_hash:
        return False
    try:
        raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, Mapping):
        return False
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return (
        str(payload.get("closure_epoch_id") or "") == source_epoch
        and bool(payload.get("complete"))
    )


def verify_reuse_record(
    registry: Any,
    reuse_record: Any,
    *,
    packet: AdjudicationPacket,
    api_config: Mapping[str, Any],
    input_dependency_hashes: Mapping[str, str],
    current_epoch: str,
    service: Any,
) -> tuple[Mapping[str, Any] | None, str]:
    """Verify a Registry-backed reuse record before trusting its verdict."""

    try:
        raw = json.loads(Path(reuse_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"adjudication_reuse_unreadable:{exc}"
    if not isinstance(raw, Mapping):
        return None, "adjudication_reuse_payload_invalid"
    if reuse_record.content_hash != file_sha256(reuse_record.path):
        return None, "adjudication_reuse_registry_hash_mismatch"
    prompt, _system_prompt = _build_prompts(packet)
    expected_fields = {
        "artifact_type": ADJUDICATION_REUSE_ARTIFACT_TYPE,
        "artifact_version": ADJUDICATION_REUSE_ARTIFACT_VERSION,
        "reuse_version": ADJUDICATION_REUSE_VERSION,
        "job_id": service.job_id,
        "citation_set_key": str(packet.citation_set_key or ""),
        "stage": packet.stage,
        "node_id": adjudication_node_id(packet),
        "call_id": adjudication_call_id(packet),
        "canonical_adjudication_packet_hash": adjudication_packet_hash(packet),
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "validation_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
        "provider": str(api_config.get("provider_family") or "configured"),
        "model": str(api_config.get("model") or ""),
        "endpoint_type": str(api_config.get("endpoint_type") or "chat_completions"),
        "redacted_provider_config_hash": hash_json(_redact_mapping(dict(api_config))),
        "prompt_hash": hash_text(prompt),
        "input_hash": hash_json(_request_payload(packet, api_config)),
        "schema_hash": adjudication_schema_hash(packet),
    }
    for field, expected in expected_fields.items():
        if str(raw.get(field) or "") != str(expected or ""):
            return None, f"adjudication_reuse_{field}_mismatch"
    if str(raw.get("job_id") or "") != str(service.job_id or ""):
        return None, "adjudication_reuse_job_mismatch"
    if str(raw.get("stage") or "") not in {"primary", "stronger"}:
        return None, "adjudication_reuse_stage_invalid"
    current_deps = {
        str(key): str(value)
        for key, value in (raw.get("current_input_dependency_hashes") or {}).items()
    }
    if current_deps != {
        str(key): str(value)
        for key, value in sorted(input_dependency_hashes.items(), key=lambda item: str(item[0]))
    }:
        return None, "adjudication_reuse_input_dependencies_mismatch"

    output_record = registry.get(str(raw.get("provider_output_artifact_id") or ""))
    if output_record is None or output_record.status != "ready":
        return None, "adjudication_reuse_provider_output_missing"
    if output_record.content_hash != str(raw.get("provider_output_artifact_hash") or ""):
        return None, "adjudication_reuse_provider_output_hash_mismatch"
    try:
        output_payload = output_record_payload(output_record)
    except ValueError as exc:
        return None, f"adjudication_reuse_provider_output_invalid:{exc}"
    if hash_json(output_payload) != str(raw.get("normalized_result_hash") or ""):
        return None, "adjudication_reuse_normalized_result_mismatch"
    if not _source_receipt_verified(service, raw):
        return None, "adjudication_reuse_source_receipt_missing"
    if not _source_closure_verified(
        registry,
        raw,
        current_epoch=current_epoch,
    ):
        return None, "adjudication_reuse_source_closure_missing"
    return output_payload, ""


def reuse_record_artifact_id(reuse_key: str, *, closure_bound: bool = False) -> str:
    suffix = ":closure-bound" if closure_bound else ""
    return f"validation_adjudication_reuse:{reuse_key}{suffix}"


def dependency_ref(record: Any) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2.from_record(record)


__all__ = [
    "ADJUDICATION_REUSE_ARTIFACT_TYPE",
    "ADJUDICATION_REUSE_ARTIFACT_VERSION",
    "ADJUDICATION_REUSE_VERSION",
    "adjudication_call_id",
    "adjudication_node_id",
    "adjudication_packet_hash",
    "adjudication_schema_hash",
    "build_reuse_key",
    "build_reuse_record_payload",
    "dependency_ref",
    "output_record_payload",
    "reuse_record_artifact_id",
    "verify_reuse_record",
]

"""Typed validation adjudication reuse authority.

Raw checkpoint files remain coordination-only.  A canonical verdict may be
reused only after a Registry-backed reuse record verifies against the current
packet, route, provider output, source receipt, and provider closure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.provider_runtime import (
    ProviderCallReceiptV1,
    _redact_mapping,
    hash_json,
    hash_text,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from validation.adjudication_checkpoint import ADJUDICATION_PROMPT_VERSION
from validation.llm_adjudicator import (
    AdjudicationPacket,
    _build_prompts,
)
from validation.run_result import VALIDATION_RUN_SCHEMA_VERSION


ADJUDICATION_REUSE_ARTIFACT_TYPE = "validation_adjudication_reuse_record"
ADJUDICATION_REUSE_ARTIFACT_VERSION = "v1"
ADJUDICATION_REUSE_VERSION = "validation_adjudication_reuse_record/v1"
ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL = "provisional"
ADJUDICATION_REUSE_AUTHORITY_DURABLE = "durable"
PROVIDER_RECEIPT_LEDGER_ARTIFACT_TYPE = "provider_receipt_ledger"
PROVIDER_RECEIPT_LEDGER_ARTIFACT_VERSION = "v1"
PROVIDER_RECEIPT_CLOSURE_ARTIFACT_TYPE = "provider_receipt_closure"
PROVIDER_RECEIPT_CLOSURE_ARTIFACT_VERSION = "v1"


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
        "authority_state": ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL,
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


def _json_mapping(path: str | Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _paths_match(left: str | Path, right: str | Path) -> bool:
    try:
        return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()
    except (OSError, TypeError, ValueError):
        return False


def _record_is_intact(
    record: Any,
    *,
    artifact_type: str,
    artifact_version: str,
    expected_hash: str = "",
    expected_job_id: str = "",
) -> bool:
    if record is None or record.status != "ready":
        return False
    if record.artifact_type != artifact_type or record.artifact_version != artifact_version:
        return False
    if expected_job_id and record.job_id != expected_job_id:
        return False
    if expected_hash and record.content_hash != expected_hash:
        return False
    try:
        ArtifactRegistry._verify_ready_artifact(record)
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    return True


def _dependency_matches_record(dependency: Any, record: Any) -> bool:
    return bool(
        dependency is not None
        and str(getattr(dependency, "dependency_kind", "") or "") == "local_job"
        and str(getattr(dependency, "job_id", "") or "") == str(record.job_id or "")
        and str(getattr(dependency, "artifact_id", "") or "") == str(record.artifact_id or "")
        and str(getattr(dependency, "artifact_type", "") or "") == str(record.artifact_type or "")
        and str(getattr(dependency, "content_hash", "") or "") == str(record.content_hash or "")
        and _paths_match(str(getattr(dependency, "path", "") or ""), record.path)
    )


def _has_exact_dependency(parent: Any, child: Any) -> bool:
    return any(_dependency_matches_record(item, child) for item in (parent.depends_on or ()))


def _ledger_receipts(record: Any) -> list[ProviderCallReceiptV1] | None:
    receipts: list[ProviderCallReceiptV1] = []
    try:
        for line in Path(record.path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                return None
            receipts.append(ProviderCallReceiptV1.from_dict(raw))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError):
        return None
    return receipts


def _receipt_matches_payload(
    receipt: ProviderCallReceiptV1,
    payload: Mapping[str, Any],
) -> bool:
    expected = {
        "receipt_id": str(payload.get("source_receipt_id") or ""),
        "job_id": str(payload.get("job_id") or ""),
        "attempt_id": str(payload.get("attempt_id") or ""),
        "stage_name": "stage4_validate",
        "node_id": str(payload.get("node_id") or ""),
        "call_id": str(payload.get("call_id") or ""),
        "closure_epoch_id": str(payload.get("source_provider_closure_epoch_id") or ""),
        "logical_attempt_identity": str(payload.get("attempt_id") or ""),
        "prompt_hash": str(payload.get("prompt_hash") or ""),
        "input_hash": str(payload.get("input_hash") or ""),
        "config_hash": str(payload.get("redacted_provider_config_hash") or ""),
        "schema_hash": str(payload.get("schema_hash") or ""),
        "response_hash": str(payload.get("normalized_result_hash") or ""),
    }
    if not all(expected.values()) or receipt.status != "success":
        return False
    return all(str(getattr(receipt, field) or "") == value for field, value in expected.items())


def _find_matching_receipt(
    receipts: Sequence[ProviderCallReceiptV1],
    payload: Mapping[str, Any],
) -> ProviderCallReceiptV1 | None:
    receipt_id = str(payload.get("source_receipt_id") or "")
    receipt_hash = str(payload.get("source_receipt_hash") or "")
    if not receipt_id or not receipt_hash:
        return None
    for receipt in receipts:
        if (
            receipt.receipt_id == receipt_id
            and hash_json(receipt.to_dict()) == receipt_hash
            and _receipt_matches_payload(receipt, payload)
        ):
            return receipt
    return None


def _output_record_for_payload(
    registry: Any,
    payload: Mapping[str, Any],
) -> tuple[Any | None, Mapping[str, Any] | None, str]:
    output_id = str(payload.get("provider_output_artifact_id") or "")
    output_hash = str(payload.get("provider_output_artifact_hash") or "")
    output_record = registry.get(output_id) if output_id else None
    if output_record is None or not _record_is_intact(
        output_record,
        artifact_type="validation_provider_output",
        artifact_version="v1",
        expected_hash=output_hash,
        expected_job_id=str(payload.get("job_id") or ""),
    ):
        return None, None, "provider_output_missing_or_untrusted"
    output_envelope = _json_mapping(output_record.path)
    if output_envelope is None:
        return None, None, "provider_output_payload_invalid"
    output_payload = output_envelope.get("payload")
    if not isinstance(output_payload, Mapping):
        return None, None, "provider_output_payload_invalid"
    if (
        str(output_envelope.get("job_id") or "") != str(payload.get("job_id") or "")
        or str(output_envelope.get("attempt_id") or "") != str(payload.get("attempt_id") or "")
        or str(output_envelope.get("stage_name") or "") != "stage4_validate"
        or str(output_envelope.get("call_id") or "") != str(payload.get("call_id") or "")
        or hash_json(output_payload) != str(payload.get("normalized_result_hash") or "")
    ):
        return None, None, "provider_output_binding_mismatch"
    return output_record, output_payload, ""


def _closure_payload(record: Any) -> Mapping[str, Any] | None:
    raw = _json_mapping(record.path)
    if raw is None:
        return None
    value = raw.get("payload")
    return value if isinstance(value, Mapping) else None


def durable_reuse_authority_issues(
    registry: Any,
    reuse_record: Any,
    *,
    expected_call: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return fail-closed issues for a closure-bound reuse authority graph."""

    issues: list[str] = []
    if not _record_is_intact(
        reuse_record,
        artifact_type=ADJUDICATION_REUSE_ARTIFACT_TYPE,
        artifact_version=ADJUDICATION_REUSE_ARTIFACT_VERSION,
    ):
        return ["reuse_record_untrusted"]
    payload = _json_mapping(reuse_record.path)
    if payload is None:
        return ["reuse_record_unreadable"]
    if str(reuse_record.job_id or "") != str(payload.get("job_id") or ""):
        issues.append("reuse_record_job_mismatch")
    if str(payload.get("authority_state") or "") != ADJUDICATION_REUSE_AUTHORITY_DURABLE:
        issues.append("reuse_record_authority_not_durable")
    reuse_key = str(payload.get("reuse_key") or "")
    if not reuse_key or reuse_record.artifact_id != reuse_record_artifact_id(reuse_key, closure_bound=True):
        issues.append("reuse_record_artifact_id_mismatch")

    raw_record = registry.get(reuse_record_artifact_id(reuse_key)) if reuse_key else None
    if raw_record is None or not _record_is_intact(
        raw_record,
        artifact_type=ADJUDICATION_REUSE_ARTIFACT_TYPE,
        artifact_version=ADJUDICATION_REUSE_ARTIFACT_VERSION,
        expected_job_id=str(payload.get("job_id") or ""),
    ):
        issues.append("source_reuse_record_missing_or_untrusted")
        raw_payload = None
    else:
        raw_payload = _json_mapping(raw_record.path)
        if raw_payload is None:
            issues.append("source_reuse_record_unreadable")
        if not _has_exact_dependency(reuse_record, raw_record):
            issues.append("reuse_record_source_dependency_mismatch")
    if raw_payload is None:
        raw_payload = {}
    if str(raw_payload.get("authority_state") or "") != ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL:
        issues.append("source_reuse_record_authority_invalid")
    identity_fields = (
        "reuse_key",
        "job_id",
        "attempt_id",
        "citation_set_key",
        "stage",
        "node_id",
        "call_id",
        "canonical_adjudication_packet_hash",
        "prompt_version",
        "validation_schema_version",
        "provider",
        "model",
        "endpoint_type",
        "redacted_provider_config_hash",
        "prompt_hash",
        "input_hash",
        "schema_hash",
        "provider_output_artifact_id",
        "provider_output_artifact_hash",
        "normalized_result_hash",
        "source_receipt_id",
        "source_receipt_hash",
        "source_provider_closure_epoch_id",
        "current_input_dependency_hashes",
    )
    for field in identity_fields:
        if payload.get(field) != raw_payload.get(field):
            issues.append(f"source_reuse_record_{field}_mismatch")

    output_record, _output_payload, output_error = _output_record_for_payload(registry, raw_payload)
    if output_error:
        issues.append(output_error)
    if output_record is not None and raw_record is not None and not _has_exact_dependency(raw_record, output_record):
        issues.append("source_reuse_record_output_dependency_mismatch")

    ledger_id = str(payload.get("source_receipt_ledger_artifact_id") or "")
    ledger_hash = str(payload.get("source_receipt_ledger_artifact_hash") or "")
    ledger_record = registry.get(ledger_id) if ledger_id else None
    if not ledger_id or not ledger_hash or not _record_is_intact(
        ledger_record,
        artifact_type=PROVIDER_RECEIPT_LEDGER_ARTIFACT_TYPE,
        artifact_version=PROVIDER_RECEIPT_LEDGER_ARTIFACT_VERSION,
        expected_hash=ledger_hash,
        expected_job_id=str(raw_payload.get("job_id") or ""),
    ):
        issues.append("source_receipt_ledger_missing_or_untrusted")
        ledger_receipts: list[ProviderCallReceiptV1] = []
    else:
        ledger_receipts = _ledger_receipts(ledger_record) or []
        if not ledger_receipts:
            issues.append("source_receipt_ledger_unreadable")
        if raw_record is not None and not _has_exact_dependency(reuse_record, ledger_record):
            issues.append("reuse_record_ledger_dependency_mismatch")
    receipt = _find_matching_receipt(ledger_receipts, raw_payload)
    if receipt is None:
        issues.append("source_receipt_binding_mismatch")

    closure_id = str(payload.get("source_provider_closure_artifact_id") or "")
    closure_hash = str(payload.get("source_provider_closure_artifact_hash") or "")
    closure_record = registry.get(closure_id) if closure_id else None
    if not closure_id or not closure_hash or not _record_is_intact(
        closure_record,
        artifact_type=PROVIDER_RECEIPT_CLOSURE_ARTIFACT_TYPE,
        artifact_version=PROVIDER_RECEIPT_CLOSURE_ARTIFACT_VERSION,
        expected_hash=closure_hash,
        expected_job_id=str(raw_payload.get("job_id") or ""),
    ):
        issues.append("source_provider_closure_missing_or_untrusted")
        source_closure = None
    else:
        source_closure = _closure_payload(closure_record)
        if source_closure is None:
            issues.append("source_provider_closure_unreadable")
        if not _has_exact_dependency(reuse_record, closure_record):
            issues.append("reuse_record_closure_dependency_mismatch")
        if ledger_record is not None and not _has_exact_dependency(closure_record, ledger_record):
            issues.append("source_provider_closure_ledger_dependency_mismatch")
        if raw_record is not None and not _has_exact_dependency(closure_record, raw_record):
            issues.append("source_provider_closure_reuse_dependency_mismatch")
        if output_record is not None and not _has_exact_dependency(closure_record, output_record):
            issues.append("source_provider_closure_output_dependency_mismatch")
    if source_closure is not None:
        if (
            source_closure.get("complete") is not True
            or str(source_closure.get("job_id") or "") != str(raw_payload.get("job_id") or "")
            or str(source_closure.get("stage_name") or "") != "stage4_validate"
            or str(source_closure.get("attempt_id") or "") != str(raw_payload.get("attempt_id") or "")
            or str(source_closure.get("logical_attempt_identity") or "")
            != str(raw_payload.get("attempt_id") or "")
            or str(source_closure.get("closure_epoch_id") or "")
            != str(raw_payload.get("source_provider_closure_epoch_id") or "")
        ):
            issues.append("source_provider_closure_identity_mismatch")
        expected_calls = source_closure.get("expected_calls")
        expected_calls = expected_calls if isinstance(expected_calls, list) else []
        source_call = next(
            (item for item in expected_calls if isinstance(item, Mapping) and str(item.get("call_id") or "") == str(raw_payload.get("call_id") or "")),
            None,
        )
        if source_call is None:
            issues.append("source_provider_closure_call_missing")
        else:
            if str(source_call.get("expected_call_graph_hash") or "") != str(
                source_closure.get("expected_call_graph_hash") or ""
            ):
                issues.append("source_provider_closure_call_graph_mismatch")
            for field, expected in (
                ("job_id", raw_payload.get("job_id")),
                ("attempt_id", raw_payload.get("attempt_id")),
                ("stage_name", "stage4_validate"),
                ("node_id", raw_payload.get("node_id")),
                ("closure_epoch_id", raw_payload.get("source_provider_closure_epoch_id")),
                ("logical_attempt_identity", raw_payload.get("attempt_id")),
                ("prompt_hash", raw_payload.get("prompt_hash")),
                ("input_hash", raw_payload.get("input_hash")),
                ("config_hash", raw_payload.get("redacted_provider_config_hash")),
                ("schema_hash", raw_payload.get("schema_hash")),
                ("artifact_path", output_record.path if output_record is not None else ""),
                ("registry_file_hash", output_record.content_hash if output_record is not None else ""),
                ("artifact_payload_hash", raw_payload.get("normalized_result_hash")),
                ("output_hash", raw_payload.get("normalized_result_hash")),
                ("provider_response_hash", raw_payload.get("normalized_result_hash")),
                ("normalized_output_hash", raw_payload.get("normalized_result_hash")),
                ("artifact_content_hash", raw_payload.get("normalized_result_hash")),
                ("registered_artifact_hash", raw_payload.get("normalized_result_hash")),
                ("node_output_hash", raw_payload.get("normalized_result_hash")),
            ):
                if str(source_call.get(field) or "") != str(expected or ""):
                    issues.append(f"source_provider_closure_call_{field}_mismatch")
            if bool(source_call.get("verified_reuse")):
                issues.append("source_provider_closure_call_marked_reuse")
        observed = {str(item) for item in source_closure.get("observed_call_ids") or () if str(item)}
        verified = {str(item) for item in source_closure.get("verified_reuse_call_ids") or () if str(item)}
        if str(raw_payload.get("call_id") or "") not in observed or str(raw_payload.get("call_id") or "") in verified:
            issues.append("source_provider_closure_observation_mismatch")

    if expected_call is not None:
        if str(expected_call.get("call_id") or "") != str(payload.get("call_id") or ""):
            issues.append("reuse_call_id_mismatch")
        for field in ("node_id", "prompt_hash", "input_hash", "schema_hash"):
            if str(expected_call.get(field) or "") != str(payload.get(field) or ""):
                issues.append(f"reuse_call_{field}_mismatch")
    return list(dict.fromkeys(issues))


def _provisional_reuse_authority_issues(
    service: Any,
    reuse_record: Any,
    payload: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    if str(payload.get("authority_state") or "") != ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL:
        issues.append("reuse_record_authority_not_provisional")
    if str(payload.get("job_id") or "") != str(service.job_id or ""):
        issues.append("reuse_record_job_mismatch")
    if str(payload.get("attempt_id") or "") != str(service.attempt_id or ""):
        issues.append("reuse_record_attempt_mismatch")
    if str(payload.get("source_provider_closure_epoch_id") or "") != str(service.closure_epoch_id or ""):
        issues.append("reuse_record_epoch_mismatch")
    for field in (
        "source_receipt_ledger_artifact_id",
        "source_receipt_ledger_artifact_hash",
        "source_provider_closure_artifact_id",
        "source_provider_closure_artifact_hash",
    ):
        if str(payload.get(field) or ""):
            issues.append(f"reuse_record_{field}_unexpected")
    try:
        receipts = service.provider_receipt_ledger.list_receipts()
    except (OSError, TypeError, ValueError, RuntimeError):
        receipts = ()
    if _find_matching_receipt(receipts, payload) is None:
        issues.append("source_receipt_not_observed_by_current_service")
    output_record, _output_payload, output_error = _output_record_for_payload(
        service.artifact_registry,
        payload,
    )
    if output_error:
        issues.append(output_error)
    if output_record is not None and not _has_exact_dependency(reuse_record, output_record):
        issues.append("reuse_record_output_dependency_mismatch")
    return list(dict.fromkeys(issues))


def verify_reuse_record(
    registry: Any,
    reuse_record: Any,
    *,
    packet: AdjudicationPacket,
    api_config: Mapping[str, Any],
    input_dependency_hashes: Mapping[str, str],
    current_epoch: str,
    service: Any,
    authority: str = ADJUDICATION_REUSE_AUTHORITY_DURABLE,
) -> tuple[Mapping[str, Any] | None, str]:
    """Verify a Registry-backed reuse record before trusting its verdict."""

    if authority not in {
        ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL,
        ADJUDICATION_REUSE_AUTHORITY_DURABLE,
    }:
        return None, f"adjudication_reuse_authority_invalid:{authority}"
    if not _record_is_intact(
        reuse_record,
        artifact_type=ADJUDICATION_REUSE_ARTIFACT_TYPE,
        artifact_version=ADJUDICATION_REUSE_ARTIFACT_VERSION,
        expected_job_id=str(service.job_id or ""),
    ):
        return None, "adjudication_reuse_registry_hash_mismatch"
    raw = _json_mapping(reuse_record.path)
    if raw is None:
        return None, "adjudication_reuse_payload_invalid"
    prompt, _system_prompt = _build_prompts(packet)
    expected_reuse_key = build_reuse_key(
        packet=packet,
        api_config=api_config,
        input_dependency_hashes=input_dependency_hashes,
    )
    expected_fields = {
        "artifact_type": ADJUDICATION_REUSE_ARTIFACT_TYPE,
        "artifact_version": ADJUDICATION_REUSE_ARTIFACT_VERSION,
        "reuse_version": ADJUDICATION_REUSE_VERSION,
        "reuse_key": expected_reuse_key,
        "authority_state": authority,
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

    output_record, output_payload, output_error = _output_record_for_payload(registry, raw)
    if output_error or output_payload is None:
        return None, f"adjudication_reuse_{output_error or 'provider_output_invalid'}"
    if authority == ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL:
        if reuse_record.artifact_id != reuse_record_artifact_id(expected_reuse_key):
            return None, "adjudication_reuse_provisional_artifact_id_mismatch"
        authority_issues = _provisional_reuse_authority_issues(service, reuse_record, raw)
    else:
        if reuse_record.artifact_id != reuse_record_artifact_id(expected_reuse_key, closure_bound=True):
            return None, "adjudication_reuse_durable_artifact_id_mismatch"
        authority_issues = durable_reuse_authority_issues(registry, reuse_record)
    if (
        authority == ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL
        and output_record is not None
        and not _has_exact_dependency(reuse_record, output_record)
    ):
        authority_issues.append("reuse_record_output_dependency_mismatch")
    if authority_issues:
        return None, f"adjudication_reuse_{authority_issues[0]}"
    return output_payload, ""


def reuse_record_artifact_id(reuse_key: str, *, closure_bound: bool = False) -> str:
    suffix = ":closure-bound" if closure_bound else ""
    return f"validation_adjudication_reuse:{reuse_key}{suffix}"


def dependency_ref(record: Any) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2.from_record(record)


__all__ = [
    "ADJUDICATION_REUSE_ARTIFACT_TYPE",
    "ADJUDICATION_REUSE_ARTIFACT_VERSION",
    "ADJUDICATION_REUSE_AUTHORITY_DURABLE",
    "ADJUDICATION_REUSE_AUTHORITY_PROVISIONAL",
    "ADJUDICATION_REUSE_VERSION",
    "adjudication_call_id",
    "adjudication_node_id",
    "adjudication_packet_hash",
    "adjudication_schema_hash",
    "build_reuse_key",
    "build_reuse_record_payload",
    "dependency_ref",
    "durable_reuse_authority_issues",
    "output_record_payload",
    "reuse_record_artifact_id",
    "verify_reuse_record",
]

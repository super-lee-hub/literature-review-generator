"""Read-only recovery of retained semantic provider responses.

This module validates an existing Registry-owned request audit, receipt, and
raw response spool. It never publishes an artifact, appends a receipt, or
performs transport.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from ai_interface import (
    reparse_provider_response_spool,  # pyright: ignore[reportAttributeAccessIssue]
)
from runtime.provider_completion import ProviderCompletionEvaluator
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json


@dataclass(frozen=True)
class VerifiedSemanticResponse:
    payload: Mapping[str, Any]
    normalized_hash: str
    receipt_id: str
    raw_ref: Mapping[str, Any]
    audit_record_id: str
    audit_record_hash: str


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _registry_fence(registry: Any) -> tuple[int, str] | None:
    capture = getattr(registry, "capture_record_snapshot", None)
    if not callable(capture):
        return None
    revision, records = cast(Callable[[], tuple[int, Sequence[Any]]], capture)()
    records = list(cast(Sequence[Any], records))
    record_map = {
        str(record.artifact_id): (
            asdict(record) if hasattr(record, "__dataclass_fields__") else dict(vars(record))
        )
        for record in sorted(records, key=lambda value: str(value.artifact_id))
    }
    return int(revision), hash_json(record_map)


def _payload_from_envelope(path: Path) -> Mapping[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    payload = raw.get("payload") if isinstance(raw, Mapping) else None
    if not isinstance(payload, Mapping):
        raise TypeError("audit artifact payload is not an object")
    return payload


def _audit_rows(registry: Any, job_id: str, resolver: Callable[[str], Any] | None) -> list[tuple[Any, Mapping[str, Any]]]:
    rows: list[tuple[Any, Mapping[str, Any]]] = []
    registry_owner = str(getattr(registry, "job_id", "") or "")
    if registry_owner and registry_owner != job_id:
        return rows
    for record in registry.list_records():
        metadata = getattr(record, "metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        record_job = str(getattr(record, "job_id", "") or "")
        metadata_job = str(metadata.get("job_id") or "")
        if record_job and metadata_job and record_job != metadata_job:
            continue
        record_owner = record_job or metadata_job or registry_owner
        if (
            str(getattr(record, "artifact_type", "")) != "outline_request_payload_audit"
            or str(getattr(record, "artifact_version", "")) != "v1"
            or str(getattr(record, "status", "")) != "ready"
            or record_owner != job_id
        ):
            continue
        registry.verify_ready_artifact_closure(
            record,
            external_registry_resolver=resolver,
        )
        path = Path(str(record.path)).expanduser().resolve(strict=True)
        raw_hash, _size = _sha256(path)
        if raw_hash != str(record.content_hash):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, Mapping) and str(row.get("job_id") or "") == job_id:
                rows.append((record, row))
    return rows


def _route_matches(route: Mapping[str, Any], expected: Mapping[str, Any], receipt: Any) -> bool:
    for key, value in expected.items():
        if value in (None, ""):
            continue
        if str(route.get(key) or "") != str(value):
            return False
    route_provider = str(route.get("provider") or "")
    route_model = str(route.get("model") or "")
    route_endpoint = str(route.get("endpoint_type") or "")
    return (
        (not route_provider or route_provider == str(getattr(receipt, "provider", "")))
        and (not route_model or route_model == str(getattr(receipt, "model", "")))
        and (not route_endpoint or route_endpoint == str(getattr(receipt, "endpoint_type", "")))
    )


def find_verified_topic_response(
    registry: Any,
    receipt_ledger: ProviderRuntimeLedger,
    job_id: str,
    node_id: str,
    replay_key_hash: str,
    payload_hash: str,
    expected_prompt_hash: str,
    schema_hash: str,
    config_hash: str,
    route_mapping: Mapping[str, Any],
    route_fingerprint: str,
    input_artifact_hashes: Sequence[str],
    workspace_root: str | Path,
    external_registry_resolver: Callable[[str], Any] | None = None,
    receipt_source_records: Mapping[str, Any] | None = None,
    verified_receipts: Mapping[str, Any] | None = None,
) -> VerifiedSemanticResponse | None:
    """Return one fully bound retained topic response, or ``None``.

    All checks are read-only and fail closed on ambiguity, stale identity,
    foreign ownership, tampered bytes, failed receipts, or incomplete HTTP
    responses.
    """

    try:
        initial_registry_fence = _registry_fence(registry)
        if initial_registry_fence is None:
            return None
        rows = _audit_rows(registry, job_id, external_registry_resolver)
        matches = [
            (record, row)
            for record, row in rows
            if str(row.get("node_id") or "") == node_id
            and str(row.get("replay_key_hash") or "") == replay_key_hash
            and str(row.get("payload_hash") or "") == payload_hash
            and str(row.get("schema_hash") or "") == schema_hash
            and str(row.get("config_hash") or "") == config_hash
            and str(row.get("route_fingerprint") or "") == route_fingerprint
            and sorted(str(value) for value in row.get("input_artifact_hashes") or ())
            == sorted(str(value) for value in input_artifact_hashes)
        ]
        if len(matches) != 1:
            return None
        audit_record, audit = matches[0]
        if (
            audit.get("provider_invoked") is not True
            or str(audit.get("status") or "") != "success"
            or str(audit.get("semantic_validation_status") or "") != "accepted"
        ):
            return None
        route_value = audit.get("route")
        route: Mapping[str, Any] = route_value if isinstance(route_value, Mapping) else {}
        refs = audit.get("raw_response_refs")
        if not isinstance(refs, list) or len(refs) != 1:
            return None
        raw_ref = refs[0] if isinstance(refs[0], Mapping) else None
        if raw_ref is None:
            return None
        raw_ref = dict(raw_ref)
        receipt_id = str((audit.get("receipt_ids") or [""])[-1] or "")
        if not receipt_id:
            return None
        if verified_receipts is not None:
            candidate = verified_receipts.get(receipt_id)
            receipts = [candidate] if candidate is not None else []
        else:
            receipts = [
                receipt for receipt in receipt_ledger.list_receipts()
                if str(getattr(receipt, "receipt_id", "")) == receipt_id
            ]
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        source_path: Path | None = None
        if receipt_source_records is not None:
            source_record = receipt_source_records.get(receipt_id)
            if source_record is None:
                return None
            current_source = registry.get(str(getattr(source_record, "artifact_id", "")))
            source_metadata = getattr(source_record, "metadata", {})
            source_metadata = source_metadata if isinstance(source_metadata, Mapping) else {}
            source_job = str(getattr(source_record, "job_id", "") or source_metadata.get("job_id") or "")
            if (
                current_source is None
                or str(getattr(current_source, "artifact_type", "")) != "provider_receipt_ledger"
                or str(getattr(current_source, "artifact_version", "")) != "v1"
                or str(getattr(current_source, "status", "")) != "ready"
                or source_job != job_id
                or str(getattr(current_source, "job_id", "")) != job_id
                or str(getattr(current_source, "path", "")) != str(getattr(source_record, "path", ""))
                or str(getattr(current_source, "content_hash", "")) != str(getattr(source_record, "content_hash", ""))
            ):
                return None
            registry.verify_ready_artifact_closure(
                current_source,
                external_registry_resolver=external_registry_resolver,
            )
            source_path = Path(str(current_source.path)).expanduser().resolve(strict=True)
            source_hash, _source_bytes = _sha256(source_path)
            if source_hash != str(current_source.content_hash):
                return None
            source_receipts = [
                candidate for candidate in ProviderRuntimeLedger(source_path).list_receipts()
                if str(getattr(candidate, "receipt_id", "")) == receipt_id
            ]
            if len(source_receipts) != 1:
                return None
            source_payload = source_receipts[0].to_dict()
            receipt_to_dict = getattr(receipt, "to_dict", None)
            receipt_payload = receipt_to_dict() if callable(receipt_to_dict) else vars(receipt)
            if hash_json(source_payload) != hash_json(receipt_payload):
                return None
        else:
            return None
        if (
            str(getattr(receipt, "job_id", "")) != job_id
            or str(getattr(receipt, "stage_name", "")) != "outline_v3"
            or str(getattr(receipt, "node_id", ""))
            not in {node_id, "topic_synthesis" if node_id.startswith("topic_synthesis_provider:") else node_id}
            or str(getattr(receipt, "call_id", "")) != str(audit.get("attempt_id") or "")
            or str(getattr(receipt, "closure_epoch_id", "")) != str(audit.get("closure_epoch_id") or "")
            or str(getattr(receipt, "input_hash", "")) != str(audit.get("payload_hash") or "")
            or str(getattr(receipt, "logical_attempt_identity", "")) != str(audit.get("operation_id") or "")
            or str(audit.get("physical_attempt_id") or "") != receipt_id
            or str(getattr(receipt, "status", "")) != "success"
            or getattr(receipt, "http_status", None) != 200
            or str(getattr(receipt, "prompt_hash", "")) != expected_prompt_hash
            or str(getattr(receipt, "schema_hash", "")) != schema_hash
            or str(getattr(receipt, "config_hash", "")) != config_hash
            or str(getattr(receipt, "response_hash", "")) != str(raw_ref.get("normalized_response_hash") or "")
            or not str(getattr(receipt, "finished_at", ""))
            or str(getattr(receipt, "finish_reason", "")) == "length"
            or str(getattr(receipt, "incomplete_reason", ""))
            or getattr(receipt, "test_only", True) is not False
            or str(getattr(receipt, "endpoint", "")) != str(route.get("api_base_host") or "")
            or str((getattr(receipt, "metadata", {}) or {}).get("route_fingerprint") or "")
            != str(audit.get("route_fingerprint") or "")
            or not _route_matches(route, route_mapping, receipt)
        ):
            return None
        raw_path = Path(str(raw_ref.get("path") or "")).expanduser().resolve(strict=True)
        root = Path(workspace_root).expanduser().resolve(strict=True)
        try:
            raw_path.relative_to(root)
        except ValueError:
            return None
        actual_hash, actual_bytes = _sha256(raw_path)
        expected_raw_hash = str(raw_ref.get("sha256") or "").lower()
        expected_raw_bytes = raw_ref.get("bytes")
        if actual_hash != expected_raw_hash or actual_bytes != expected_raw_bytes:
            return None
        metadata = getattr(receipt, "metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        protocol = str(metadata.get("response_protocol") or "json")
        content_type = str(metadata.get("content_type") or metadata.get("response_content_type") or (
            "text/event-stream" if protocol == "sse" else "application/json"
        ))
        endpoint_type = str(getattr(receipt, "endpoint_type", "") or route.get("endpoint_type") or "")
        parsed = reparse_provider_response_spool(
            raw_path,
            actual_hash,
            actual_bytes,
            content_type,
            protocol,
            endpoint_type,
            str(getattr(receipt, "model", "") or route.get("model") or ""),
            "json",
            200,
            True,
        )
        completion = ProviderCompletionEvaluator.evaluate(parsed, minimum_output=2, expect_json=True)
        if completion.status != "complete" or not isinstance(completion.content, Mapping):
            return None
        normalized_hash = hash_json(completion.content)
        if normalized_hash != str(getattr(receipt, "response_hash", "")):
            return None
        audit_path = Path(str(audit_record.path)).expanduser().resolve(strict=True)
        audit_hash, _audit_bytes = _sha256(audit_path)
        if audit_hash != str(audit_record.content_hash):
            return None
        if source_path is None:
            return None
        final_source_hash, _final_source_bytes = _sha256(source_path)
        if final_source_hash != str(current_source.content_hash):
            return None
        final_raw_hash, final_raw_bytes = _sha256(raw_path)
        if final_raw_hash != actual_hash or final_raw_bytes != actual_bytes:
            return None
        final_registry_fence = _registry_fence(registry)
        if final_registry_fence != initial_registry_fence:
            return None
        return VerifiedSemanticResponse(
            payload=dict(completion.content),
            normalized_hash=normalized_hash,
            receipt_id=receipt_id,
            raw_ref=dict(raw_ref),
            audit_record_id=str(audit_record.artifact_id),
            audit_record_hash=str(audit_record.content_hash),
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


__all__ = ["VerifiedSemanticResponse", "find_verified_topic_response"]

"""Immutable original response values for the successor reuse contract.

A snapshot is intermediate evidence, not a receipt or reuse authorization.
The caller must verify the original ledger/closure and current source scope.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from runtime.provider_runtime import (
    ProviderCallReceiptV1,
    ProviderRuntimeContractError,
    hash_json,
)
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, file_sha256
from services.durable_io import interprocess_file_lock
from services.job_workspace import JobWorkspace, atomic_write_json, publish_json_artifact

SNAPSHOT_ARTIFACT_TYPE = "provider_response_snapshot"
SNAPSHOT_ARTIFACT_VERSION = "v1"
SNAPSHOT_PRODUCER = "runtime.provider_response_snapshot.ProviderResponseSnapshotStore"
_FIELDS = frozenset({
    "artifact_type", "artifact_version", "status", "job_id", "stage_name",
    "call_id", "node_id", "receipt_id", "receipt", "receipt_hash",
    "response_hash", "content",
})


class ProviderResponseSnapshotError(ProviderRuntimeContractError):
    """Blocking capture/reconciliation error; never retry the provider for it."""


class ProviderResponseSnapshotMissing(ProviderResponseSnapshotError):
    """Known receipt has no registered capture; this is not a cache miss."""


def _json_value(value: Any) -> Any:
    """Copy JSON values without the provider hash's non-JSON repr fallback."""
    pending = [(value, False)]
    active: set[int] = set()
    while pending:
        item, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ProviderResponseSnapshotError("response contains a non-finite number")
            continue
        if type(item) not in (dict, list):
            raise ProviderResponseSnapshotError("response must contain only JSON value types")
        if id(item) in active:
            raise ProviderResponseSnapshotError("response contains a circular JSON value")
        active.add(id(item))
        pending.append((item, True))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ProviderResponseSnapshotError("response object keys must be strings")
            pending.extend((child, False) for child in item.values())
        else:
            pending.extend((child, False) for child in item)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProviderResponseSnapshotError("response cannot be retained as exact JSON") from exc


def _receipt_row(receipt: ProviderCallReceiptV1) -> dict[str, Any]:
    if not isinstance(receipt, ProviderCallReceiptV1):
        raise ProviderResponseSnapshotError("a typed original receipt is required")
    try:
        receipt.validate_acceptance_authority()
        if not isinstance(receipt.logical_attempt_identity, str) or not receipt.logical_attempt_identity.strip():
            raise ProviderResponseSnapshotError("receipt lacks logical attempt identity")
        if (receipt.status != "success" or receipt.error_kind is not None
                or receipt.incomplete_reason or receipt.timeout_kind
                or receipt.finish_reason.strip().casefold() in {"length", "max_tokens", "max_output_tokens", "incomplete"}
                or (receipt.http_status is not None and not 200 <= receipt.http_status < 300)):
            raise ProviderResponseSnapshotError("receipt has no complete known successful response")
        digest = receipt.response_hash
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProviderResponseSnapshotError("receipt lacks a complete response hash")
        # The DTO intentionally has tuple fields. Its existing JSON encoding
        # represents them as arrays; do not change the DTO or its hash domain.
        return json.loads(json.dumps(receipt.to_dict(), ensure_ascii=False, allow_nan=False))
    except ProviderResponseSnapshotError:
        raise
    except (ProviderRuntimeContractError, TypeError, ValueError, RecursionError) as exc:
        raise ProviderResponseSnapshotError("receipt identity is not capturable") from exc


def validate_provider_response_snapshot(payload: Mapping[str, Any]) -> None:
    """Validate structure and hash domains, without granting receipt authority."""
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        raise ProviderResponseSnapshotError("response snapshot fields are incomplete or unknown")
    if (payload["artifact_type"] != SNAPSHOT_ARTIFACT_TYPE
            or payload["artifact_version"] != SNAPSHOT_ARTIFACT_VERSION
            or payload["status"] != "ready"):
        raise ProviderResponseSnapshotError("response snapshot type/version/status is invalid")
    row = payload["receipt"]
    if not isinstance(row, dict):
        raise ProviderResponseSnapshotError("response snapshot receipt must be an object")
    try:
        receipt = ProviderCallReceiptV1.from_dict(row)
    except (ProviderRuntimeContractError, TypeError, ValueError, OverflowError) as exc:
        raise ProviderResponseSnapshotError("response snapshot receipt schema is invalid") from exc
    canonical_row = _receipt_row(receipt)
    if hash_json(row) != hash_json(canonical_row) or payload["receipt_hash"] != hash_json(row):
        raise ProviderResponseSnapshotError("response snapshot receipt row hash or shape differs")
    for name in ("job_id", "stage_name", "call_id", "node_id", "receipt_id", "response_hash"):
        if payload[name] != getattr(receipt, name):
            raise ProviderResponseSnapshotError(f"response snapshot {name} differs from its receipt")
    content = _json_value(payload["content"])
    if hash_json(content) != receipt.response_hash:
        raise ProviderResponseSnapshotError("response snapshot content differs from response_hash")


def _encoded(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def _read_payload(path: Path) -> tuple[dict[str, Any], bytes]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProviderResponseSnapshotError("response snapshot contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        payload = json.loads(raw, object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ProviderResponseSnapshotError("response snapshot bytes are missing or malformed") from exc
    validate_provider_response_snapshot(payload)
    return payload, raw


class ProviderResponseSnapshotStore:
    """Retain a response privately before fenced, immutable publication.

    ``read_registered`` never repairs or writes parent data. ``require`` is
    for the same job's existing-receipt path and can finish local publication
    from retained bytes. Neither method can invoke a provider.
    """

    def __init__(self, workspace: JobWorkspace, registry: ArtifactRegistry, publication_context: Any) -> None:
        if (workspace.job_id != registry.job_id
                or Path(workspace.paths.registry_path).resolve() != Path(registry.registry_path).resolve()):
            raise ProviderResponseSnapshotError("snapshot store workspace/Registry ownership differs")
        self.workspace = workspace
        self.registry = registry
        self.publication_context = publication_context

    def _identity(self, receipt: ProviderCallReceiptV1) -> tuple[str, dict[str, Any]]:
        row = _receipt_row(receipt)
        if receipt.job_id != self.registry.job_id:
            raise ProviderResponseSnapshotError("snapshot receipt belongs to a foreign job")
        key = hash_json({"job_id": receipt.job_id, "receipt_id": receipt.receipt_id})
        return key, row

    @staticmethod
    def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {name: payload[name] for name in (
            "stage_name", "call_id", "node_id", "receipt_id", "receipt_hash", "response_hash",
        )}

    def _staged(self, key: str, receipt: ProviderCallReceiptV1) -> dict[str, Any] | None:
        directory = Path(self.workspace.log_path(f"provider_response_snapshots/{key}"))
        if not directory.exists():
            return None
        entries = []
        for path in directory.iterdir():
            if len(entries) == 1:
                raise ProviderResponseSnapshotError("response snapshot staging is ambiguous")
            entries.append(path)
        if not entries:
            return None
        path = Path(self.workspace.log_path(f"provider_response_snapshots/{key}/{entries[0].name}"))
        if path.name != f"{receipt.response_hash}.json" or not path.is_file():
            raise ProviderResponseSnapshotError("response snapshot staging identity differs")
        payload, _raw = _read_payload(path)
        if payload["receipt_hash"] != hash_json(receipt.to_dict()):
            raise ProviderResponseSnapshotError("retained response belongs to a different receipt row")
        return payload

    def _read_registered_payload(
        self, receipt: ProviderCallReceiptV1,
    ) -> tuple[ArtifactRecord, dict[str, Any]]:
        """Read one durable binding and the value from the same verified bytes.

        The final Registry and file fences make the first byte buffer the
        linearized value returned to a caller. A second file hash catches a
        concurrent persistent replacement, while parsing never reopens the
        path and therefore cannot return content different from the bytes
        whose hash was checked against the Registry.
        """
        key, row = self._identity(receipt)
        artifact_id = f"provider-response-snapshot:{key}"
        try:
            _revision, records = self.registry._read_registry_unlocked()
            record = records.get(artifact_id)
            if record is None:
                raise ProviderResponseSnapshotMissing("known receipt has no registered response snapshot")
            expected_path = self.workspace.artifact_path(f"provider_response_snapshots/{Path(record.path).name}")
            if (record.job_id != receipt.job_id or record.artifact_type != SNAPSHOT_ARTIFACT_TYPE
                    or record.artifact_version != SNAPSHOT_ARTIFACT_VERSION
                    or record.artifact_role != SNAPSHOT_ARTIFACT_TYPE or record.status != "ready"
                    or record.producer != SNAPSHOT_PRODUCER or record.depends_on
                    or os.path.normcase(record.path) != os.path.normcase(expected_path)):
                raise ProviderResponseSnapshotError("registered response snapshot binding is invalid")
            payload, raw = _read_payload(Path(record.path))
            if (hashlib.sha256(raw).hexdigest() != record.content_hash
                    or payload["receipt_hash"] != hash_json(row)):
                raise ProviderResponseSnapshotError("registered response snapshot hash differs")
            metadata = dict(record.metadata)
            fence = metadata.pop("publication_fence", None)
            if metadata != self._metadata(payload) or (fence is not None and not isinstance(fence, Mapping)):
                raise ProviderResponseSnapshotError("registered response snapshot metadata differs")
            _current_revision, current = self.registry._read_registry_unlocked()
            if current.get(artifact_id) != record or file_sha256(record.path) != record.content_hash:
                raise ProviderResponseSnapshotError("registered response snapshot changed during read")
            return record, payload
        except ProviderResponseSnapshotError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProviderResponseSnapshotError("response snapshot Registry read failed") from exc

    def read_registered(self, receipt: ProviderCallReceiptV1) -> ArtifactRecord:
        """Read one durable binding without writes, locks, or parent repair.

        This checks only the immutable capsule. The caller still owes the
        original ledger/closure and complete dependency-union verification.
        """
        record, _payload = self._read_registered_payload(receipt)
        return record

    def read_registered_content(self, receipt: ProviderCallReceiptV1) -> Any:
        """Return a detached raw response from the exact registered bytes.

        This verifies only capsule ownership, Registry/file hashes, and the
        receipt/content hash domains. It does not make the receipt authoritative;
        callers must separately verify its original ledger, stage closure, and
        request/source binding before using the returned value.
        """
        _record, payload = self._read_registered_payload(receipt)
        return _json_value(payload["content"])

    def _publish(self, key: str, payload: Mapping[str, Any], receipt: ProviderCallReceiptV1) -> ArtifactRecord:
        try:
            publish_json_artifact(
                self.publication_context, self.registry,
                self.workspace.artifact_path(f"provider_response_snapshots/{key}.json"), payload,
                artifact_id=f"provider-response-snapshot:{key}",
                artifact_role=SNAPSHOT_ARTIFACT_TYPE, artifact_type=SNAPSHOT_ARTIFACT_TYPE,
                artifact_version=SNAPSHOT_ARTIFACT_VERSION, status="ready",
                producer=SNAPSHOT_PRODUCER, depends_on=[], metadata=self._metadata(payload),
            )
        except Exception as exc:
            # Private bytes remain intact even if the queue fence/registration
            # fails. Do not reinterpret this error as a transport retry.
            raise ProviderResponseSnapshotError("response retained; fenced snapshot publication failed") from exc
        return self.read_registered(receipt)

    def capture(self, receipt: ProviderCallReceiptV1, content: Any) -> ArtifactRecord:
        try:
            return self._capture(receipt, content)
        except ProviderResponseSnapshotError:
            raise
        except Exception as exc:
            raise ProviderResponseSnapshotError("response snapshot capture failed; reconciliation required") from exc

    def _capture(self, receipt: ProviderCallReceiptV1, content: Any) -> ArtifactRecord:
        key, row = self._identity(receipt)
        payload = {
            "artifact_type": SNAPSHOT_ARTIFACT_TYPE, "artifact_version": SNAPSHOT_ARTIFACT_VERSION,
            "status": "ready", "job_id": receipt.job_id, "stage_name": receipt.stage_name,
            "call_id": receipt.call_id, "node_id": receipt.node_id, "receipt_id": receipt.receipt_id,
            "receipt": row, "receipt_hash": hash_json(row),
            "response_hash": receipt.response_hash, "content": _json_value(content),
        }
        validate_provider_response_snapshot(payload)
        # Canonical insertion order makes both private and published JSON bytes
        # deterministic without modifying the original content value or DTO.
        payload = json.loads(_encoded(payload))
        with interprocess_file_lock(self.workspace.log_path(f"provider_response_snapshots/{key}")):
            try:
                return self.read_registered(receipt)
            except ProviderResponseSnapshotMissing:
                pass
            retained = self._staged(key, receipt)
            if retained is not None and _encoded(retained) != _encoded(payload):
                raise ProviderResponseSnapshotError("a different response is already retained")
            if retained is None:
                path = self.workspace.log_path(f"provider_response_snapshots/{key}/{receipt.response_hash}.json")
                atomic_write_json(path, payload)
            return self._publish(key, payload, receipt)

    def require(self, receipt: ProviderCallReceiptV1) -> ArtifactRecord:
        try:
            return self._require(receipt)
        except ProviderResponseSnapshotError:
            raise
        except Exception as exc:
            raise ProviderResponseSnapshotError("response snapshot recovery failed; reconciliation required") from exc

    def _require(self, receipt: ProviderCallReceiptV1) -> ArtifactRecord:
        key, _row = self._identity(receipt)
        with interprocess_file_lock(self.workspace.log_path(f"provider_response_snapshots/{key}")):
            try:
                return self.read_registered(receipt)
            except ProviderResponseSnapshotMissing:
                pass
            payload = self._staged(key, receipt)
            if payload is None:
                raise ProviderResponseSnapshotMissing("known response content is not retained; reconciliation required")
            return self._publish(key, payload, receipt)

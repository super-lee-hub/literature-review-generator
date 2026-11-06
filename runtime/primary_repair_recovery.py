"""Durable identity and response journal for one PRIMARY Outline repair scope.

This module contains no provider transport code.  The caller binds one scope
before transport and journals the returned response before publishing
Registry artifacts or replay records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.durable_io import atomic_replace_with_retry
from services.job_workspace import atomic_write_json, utc_now_iso


class PrimaryRepairRecoveryError(ValueError):
    """A PRIMARY repair scope or its durable response cannot be trusted."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PrimaryRepairRecoveryError("repair recovery payload is not canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as exc:
        raise PrimaryRepairRecoveryError("raw provider spool cannot be read") from exc
    return digest.hexdigest(), byte_count


@dataclass(frozen=True)
class PrimaryRepairScopeV1:
    """Stable PRIMARY candidate scope plus the exact materialization binding.

    ``scope_id`` deliberately excludes mutable request/route fingerprints.  A
    changed request therefore collides with the existing scope and requires
    reconciliation instead of creating another provider-call opportunity.
    """

    job_id: str
    logical_attempt_identity: str
    candidate_id: str
    request_hash: str
    route_identity: str
    route_config_fingerprint_sha256: str
    source_authority_sha256: str
    plan_contract_sha256: str
    schema_hash: str
    scope: str = "PRIMARY"

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "logical_attempt_identity",
            "candidate_id",
            "request_hash",
            "route_identity",
            "route_config_fingerprint_sha256",
            "source_authority_sha256",
            "plan_contract_sha256",
            "schema_hash",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise PrimaryRepairRecoveryError(f"PRIMARY repair scope is missing {field_name}")
            object.__setattr__(self, field_name, value)
        if self.scope != "PRIMARY":
            raise PrimaryRepairRecoveryError("repair reservation scope must be PRIMARY")
        if re.fullmatch(r"candidate_[1-9]\d*", self.candidate_id) is None:
            raise PrimaryRepairRecoveryError("PRIMARY repair candidate ID is invalid")
        for field_name in (
            "route_config_fingerprint_sha256",
            "source_authority_sha256",
            "plan_contract_sha256",
            "schema_hash",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, field_name)) is None:
                raise PrimaryRepairRecoveryError(f"PRIMARY repair {field_name} is invalid")

    @property
    def scope_id(self) -> str:
        identity = {
            "schema_version": "outline-primary-repair-scope-identity/v1",
            "scope": self.scope,
            "job_id": self.job_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "candidate_id": self.candidate_id,
        }
        return f"primary-repair:{_sha256(identity)}"

    @property
    def fingerprint_sha256(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "outline-primary-repair-scope/v1",
            "scope": self.scope,
            "job_id": self.job_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "candidate_id": self.candidate_id,
            "request_hash": self.request_hash,
            "route_identity": self.route_identity,
            "route_config_fingerprint_sha256": self.route_config_fingerprint_sha256,
            "source_authority_sha256": self.source_authority_sha256,
            "plan_contract_sha256": self.plan_contract_sha256,
            "schema_hash": self.schema_hash,
            "scope_id": self.scope_id,
        }
        if include_fingerprint:
            payload["scope_fingerprint_sha256"] = self.fingerprint_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PrimaryRepairScopeV1:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != "outline-primary-repair-scope/v1":
            raise PrimaryRepairRecoveryError("PRIMARY repair scope schema is invalid")
        scope = cls(
            job_id=str(payload.get("job_id") or ""),
            logical_attempt_identity=str(payload.get("logical_attempt_identity") or ""),
            candidate_id=str(payload.get("candidate_id") or ""),
            request_hash=str(payload.get("request_hash") or ""),
            route_identity=str(payload.get("route_identity") or ""),
            route_config_fingerprint_sha256=str(
                payload.get("route_config_fingerprint_sha256") or ""
            ),
            source_authority_sha256=str(payload.get("source_authority_sha256") or ""),
            plan_contract_sha256=str(payload.get("plan_contract_sha256") or ""),
            schema_hash=str(payload.get("schema_hash") or ""),
            scope=str(payload.get("scope") or ""),
        )
        supplied_id = str(payload.get("scope_id") or "")
        supplied_fingerprint = str(payload.get("scope_fingerprint_sha256") or "")
        if supplied_id and supplied_id != scope.scope_id:
            raise PrimaryRepairRecoveryError("PRIMARY repair scope ID does not match its identity")
        if supplied_fingerprint and supplied_fingerprint != scope.fingerprint_sha256:
            raise PrimaryRepairRecoveryError("PRIMARY repair scope fingerprint does not match its binding")
        return scope


@dataclass(frozen=True)
class PrimaryRepairResponseRefV1:
    path: str
    scope_id: str
    scope_fingerprint_sha256: str
    request_hash: str
    reservation_id: str
    response_sha256: str
    record_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "path",
            "scope_id",
            "scope_fingerprint_sha256",
            "request_hash",
            "reservation_id",
            "response_sha256",
            "record_sha256",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise PrimaryRepairRecoveryError(f"repair response reference is missing {field_name}")
        for field_name in (
            "scope_fingerprint_sha256",
            "response_sha256",
            "record_sha256",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", getattr(self, field_name)) is None:
                raise PrimaryRepairRecoveryError(f"repair response reference {field_name} is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": "outline-primary-repair-response-ref/v1",
            "path": self.path,
            "scope_id": self.scope_id,
            "scope_fingerprint_sha256": self.scope_fingerprint_sha256,
            "request_hash": self.request_hash,
            "reservation_id": self.reservation_id,
            "response_sha256": self.response_sha256,
            "record_sha256": self.record_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PrimaryRepairResponseRefV1:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != "outline-primary-repair-response-ref/v1":
            raise PrimaryRepairRecoveryError("repair response reference schema is invalid")
        return cls(
            path=str(payload.get("path") or ""),
            scope_id=str(payload.get("scope_id") or ""),
            scope_fingerprint_sha256=str(payload.get("scope_fingerprint_sha256") or ""),
            request_hash=str(payload.get("request_hash") or ""),
            reservation_id=str(payload.get("reservation_id") or ""),
            response_sha256=str(payload.get("response_sha256") or ""),
            record_sha256=str(payload.get("record_sha256") or ""),
        )


@dataclass(frozen=True)
class PrimaryRepairRawSpoolRefV1:
    path: str
    sha256: str
    byte_count: int
    http_status: int | None
    content_type: str
    response_protocol: str
    complete: bool
    attempt_number: int = 1
    completion_proof: str = "legacy_unknown"

    def __post_init__(self) -> None:
        if not str(self.path or "").strip():
            raise PrimaryRepairRecoveryError("raw response spool path is missing")
        if re.fullmatch(r"[0-9a-f]{64}", str(self.sha256 or "")) is None:
            raise PrimaryRepairRecoveryError("raw response spool SHA-256 is invalid")
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int) or self.byte_count < 0:
            raise PrimaryRepairRecoveryError("raw response spool byte count must be non-negative")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or self.http_status < 100
        ):
            raise PrimaryRepairRecoveryError("raw response HTTP status is invalid")
        if self.response_protocol not in {"json", "sse"}:
            raise PrimaryRepairRecoveryError("raw response protocol is unsupported")
        if not isinstance(self.complete, bool):
            raise PrimaryRepairRecoveryError("raw response completeness flag must be boolean")
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise PrimaryRepairRecoveryError("raw response attempt number must be positive")
        if self.completion_proof not in {
            "legacy_unknown",
            "reader_eof",
            "content_length_exact",
            "semantic_complete_only",
            "partial_read",
        }:
            raise PrimaryRepairRecoveryError("raw response completion proof is invalid")
        if self.complete and self.completion_proof == "partial_read":
            raise PrimaryRepairRecoveryError(
                "complete raw response cannot have a partial-read proof"
            )
        if self.complete and self.completion_proof == "semantic_complete_only":
            raise PrimaryRepairRecoveryError(
                "semantic-only completion cannot claim HTTP body EOF"
            )
        if (
            not self.complete
            and self.completion_proof == "semantic_complete_only"
            and self.http_status != 200
        ):
            raise PrimaryRepairRecoveryError(
                "semantic-only response recovery requires HTTP 200 headers"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "response_protocol": self.response_protocol,
            "complete": self.complete,
            "attempt_number": self.attempt_number,
            "completion_proof": self.completion_proof,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PrimaryRepairRawSpoolRefV1:
        if not isinstance(payload, Mapping):
            raise PrimaryRepairRecoveryError("raw response spool reference must be an object")
        raw_status = payload.get("http_status")
        return cls(
            path=str(payload.get("path") or ""),
            sha256=str(payload.get("sha256") or ""),
            byte_count=int(payload.get("byte_count") or 0),
            http_status=None if raw_status is None else int(raw_status),
            content_type=str(payload.get("content_type") or ""),
            response_protocol=str(payload.get("response_protocol") or ""),
            complete=payload.get("complete") is True,
            attempt_number=int(payload.get("attempt_number") or 1),
            completion_proof=str(
                payload.get("completion_proof") or "legacy_unknown"
            ),
        )


@dataclass(frozen=True)
class PrimaryRepairResponseEntryV1:
    reference: PrimaryRepairResponseRefV1
    entry_kind: str
    response: Mapping[str, Any]
    receipt_binding: Mapping[str, Any]
    created_at: str
    raw_spool: PrimaryRepairRawSpoolRefV1 | None = None
    raw_attempts: tuple[PrimaryRepairRawSpoolRefV1, ...] = ()


class PrimaryRepairResponseJournal:
    """Atomic, exact-scope storage for a returned PRIMARY repair response."""

    SCHEMA_VERSION = "outline-primary-repair-response/v1"
    ATTEMPT_SCHEMA_VERSION = "outline-primary-repair-transport-attempt/v1"
    _RESPONSE_FIELDS = frozenset(
        {
            "status",
            "content",
            "error_kind",
            "finish_reason",
            "incomplete_reason",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "usage_status",
            "attempts",
            "endpoint_type",
            "raw_response_path",
            "raw_response_sha256",
            "response_bytes",
            "http_status",
            "response_complete",
            "response_body_complete",
            "response_protocol",
            "transport_completion_proof",
            "provider_response_model",
            "provider_usage_present",
            "provider_code",
            "retry_after_seconds",
            "fallback_or_payload_mutations",
            "first_token_at",
            "first_token_latency_ms",
            "total_latency_ms",
            "timeout_kind",
            "message",
        }
    )

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.raw_spool_root = self.root / "raw_spools"

    def _path_for_scope(self, scope: PrimaryRepairScopeV1) -> Path:
        return self.root / f"{_sha256(scope.scope_id)}.json"

    def _attempt_root(self) -> Path:
        return self.root / "transport_attempts"

    def attempt_spool_path(
        self,
        scope: PrimaryRepairScopeV1,
        attempt_number: int,
    ) -> Path:
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise PrimaryRepairRecoveryError("PRIMARY attempt number must be positive")
        return self.raw_spool_root / (
            f"primary-attempt-{_sha256(scope.scope_id)[:24]}-{attempt_number}.spool"
        )

    def _attempt_metadata_path(
        self,
        scope: PrimaryRepairScopeV1,
        attempt_number: int,
    ) -> Path:
        return self._attempt_root() / (
            f"{_sha256(scope.scope_id)}-{attempt_number}.json"
        )

    def persist_attempt_intent(
        self,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        attempt_number: int,
        *,
        receipt_binding: Mapping[str, Any],
    ) -> str:
        """Persist the exact spool path and receipt binding before each POST."""

        reservation_id = str(reservation_id or "").strip()
        if not reservation_id:
            raise PrimaryRepairRecoveryError("PRIMARY attempt intent has no reservation")
        if not isinstance(receipt_binding, Mapping) or not receipt_binding:
            raise PrimaryRepairRecoveryError("PRIMARY attempt intent has no receipt binding")
        expected = self.attempt_spool_path(scope, attempt_number).resolve()
        receipt_binding = dict(receipt_binding)
        _canonical_json(receipt_binding)
        self.raw_spool_root.mkdir(parents=True, exist_ok=True)
        self._attempt_root().mkdir(parents=True, exist_ok=True)
        target = self._attempt_metadata_path(scope, attempt_number)
        body: dict[str, Any] = {
            "schema_version": self.ATTEMPT_SCHEMA_VERSION,
            "phase": "post_intent",
            "created_at": utc_now_iso(),
            "scope": scope.to_dict(),
            "reservation_id": reservation_id,
            "request_hash": scope.request_hash,
            "attempt_number": attempt_number,
            "spool_path": str(expected),
            "receipt_binding": receipt_binding,
            "http_status": None,
            "content_type": "",
            "content_length": None,
            "content_encoding": "",
            "transfer_encoding": "",
        }
        if target.exists():
            existing = self._read_attempt_intent(
                target,
                scope=scope,
                reservation_id=reservation_id,
                attempt_number=attempt_number,
            )
            if (
                existing.get("spool_path") != str(expected)
                or dict(existing.get("receipt_binding") or {}) != receipt_binding
            ):
                raise PrimaryRepairRecoveryError(
                    "PRIMARY attempt intent conflicts with an existing attempt binding"
                )
            return str(expected)
        body["record_sha256"] = _sha256(body)
        atomic_write_json(str(target), body)
        return str(expected)

    def bind_attempt_response_headers(
        self,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        attempt_number: int,
        *,
        http_status: int | None,
        content_type: str,
        content_length: int | None,
        content_encoding: str,
        transfer_encoding: str,
    ) -> None:
        """Durably bind response headers before reading any response bytes."""

        target = self._attempt_metadata_path(scope, attempt_number)
        record = self._read_attempt_intent(
            target,
            scope=scope,
            reservation_id=reservation_id,
            attempt_number=attempt_number,
        )
        if http_status is not None and (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or http_status < 100
        ):
            raise PrimaryRepairRecoveryError("PRIMARY response HTTP status is invalid")
        if content_length is not None and (
            isinstance(content_length, bool)
            or not isinstance(content_length, int)
            or content_length < 0
        ):
            raise PrimaryRepairRecoveryError("PRIMARY response Content-Length is invalid")
        normalized = {
            "http_status": http_status,
            "content_type": str(content_type or ""),
            "content_length": content_length,
            "content_encoding": str(content_encoding or "").strip().casefold(),
            "transfer_encoding": str(transfer_encoding or "").strip().casefold(),
        }
        prior_phase = str(record.get("phase") or "")
        prior = {key: record.get(key) for key in normalized}
        if prior_phase == "response_headers_bound":
            if prior != normalized:
                raise PrimaryRepairRecoveryError(
                    "PRIMARY response headers conflict with their durable binding"
                )
            return
        if prior_phase != "post_intent":
            raise PrimaryRepairRecoveryError(
                "PRIMARY response headers arrived outside a started attempt"
            )
        record.update(normalized)
        record["phase"] = "response_headers_bound"
        record["headers_bound_at"] = utc_now_iso()
        record.pop("record_sha256", None)
        record["record_sha256"] = _sha256(record)
        atomic_write_json(str(target), record)

    def _read_attempt_intent(
        self,
        path: Path,
        *,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        attempt_number: int,
    ) -> dict[str, Any]:
        target = path.expanduser().resolve()
        if target.parent != self._attempt_root().resolve():
            raise PrimaryRepairRecoveryError("PRIMARY attempt metadata escaped its root")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PrimaryRepairRecoveryError("PRIMARY attempt metadata is unreadable") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != self.ATTEMPT_SCHEMA_VERSION
        ):
            raise PrimaryRepairRecoveryError("PRIMARY attempt metadata schema is invalid")
        body = dict(payload)
        record_hash = str(body.pop("record_sha256", "") or "")
        try:
            stored_scope = PrimaryRepairScopeV1.from_dict(body.get("scope") or {})
        except PrimaryRepairRecoveryError as exc:
            raise PrimaryRepairRecoveryError("PRIMARY attempt scope is invalid") from exc
        if (
            stored_scope.fingerprint_sha256 != scope.fingerprint_sha256
            or str(body.get("reservation_id") or "") != str(reservation_id)
            or str(body.get("request_hash") or "") != scope.request_hash
            or body.get("attempt_number") != attempt_number
            or body.get("spool_path")
            != str(self.attempt_spool_path(scope, attempt_number).resolve())
            or not body.get("created_at")
            or record_hash != _sha256(body)
        ):
            raise PrimaryRepairRecoveryError(
                "PRIMARY attempt metadata identity or hash is invalid"
            )
        receipt_binding = body.get("receipt_binding")
        if not isinstance(receipt_binding, Mapping) or not receipt_binding:
            raise PrimaryRepairRecoveryError(
                "PRIMARY attempt metadata receipt binding is invalid"
            )
        _canonical_json(receipt_binding)
        return body

    def recover_orphaned_raw_response(
        self,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        attempt_number: int,
    ) -> PrimaryRepairResponseRefV1 | None:
        """Adopt a deterministic fsynced spool when its normal callback was interrupted.

        Recovery requires a durable HTTP 200 header record and an exact,
        unencoded Content-Length match. Local JSON/SSE parsing must also prove
        semantic completion before the body can become a recoverable response.
        """

        target = self._attempt_metadata_path(scope, attempt_number)
        if not target.is_file():
            return None
        record = self._read_attempt_intent(
            target,
            scope=scope,
            reservation_id=reservation_id,
            attempt_number=attempt_number,
        )
        if str(record.get("phase") or "") != "response_headers_bound":
            return None
        if record.get("http_status") != 200:
            return None
        spool_path = Path(str(record.get("spool_path") or "")).expanduser().resolve()
        if spool_path.parent != self.raw_spool_root.resolve() or not spool_path.is_file():
            return None
        try:
            digest, byte_count = _hash_file(spool_path)
            if byte_count > 64 * 1024 * 1024:
                return None
            content_length = record.get("content_length")
            exact_length_header = (
                content_length is not None
                and record.get("content_encoding") in {"", "identity"}
                and record.get("transfer_encoding") in {"", "identity"}
            )
            if exact_length_header and content_length is not None and byte_count != int(content_length):
                # A complete JSON prefix does not override an explicit HTTP
                # length mismatch; the provider body is known to be partial.
                return None
            length_proven = (
                exact_length_header and content_length is not None and byte_count == int(content_length)
            )
            raw_body = spool_path.read_bytes()
            if len(raw_body) != byte_count:
                return None
            from ai_interface import (
                _response_protocol_from_body,
                reparse_provider_response_spool,
            )

            content_type = str(record.get("content_type") or "")
            protocol = _response_protocol_from_body(raw_body, content_type)
            receipt_binding = dict(record["receipt_binding"])
            parsed = reparse_provider_response_spool(
                str(spool_path),
                digest,
                byte_count,
                content_type,
                protocol,
                str(receipt_binding.get("endpoint_type") or ""),
                str(receipt_binding.get("model") or ""),
                str(receipt_binding.get("response_format") or ""),
                200,
                True,
            )
            if (
                str(parsed.get("status") or "") != "success"
                or parsed.get("response_complete") is not True
            ):
                return None
            completion_proof = (
                "content_length_exact" if length_proven else "semantic_complete_only"
            )
            return self.persist_raw_response(
                scope,
                reservation_id,
                spool_path=spool_path,
                sha256=digest,
                byte_count=byte_count,
                http_status=200,
                content_type=content_type,
                response_protocol=protocol,
                complete=length_proven,
                receipt_binding=receipt_binding,
                attempt_number=attempt_number,
                completion_proof=completion_proof,
            )
        except (OSError, ValueError, TypeError, PrimaryRepairRecoveryError):
            return None

    def _copy_raw_spool_to_content_addressed_path(
        self,
        source: Path,
        target: Path,
        *,
        expected_sha256: str,
        expected_bytes: int,
    ) -> None:
        if source.resolve() == target.resolve():
            return
        if target.exists():
            existing_hash, existing_bytes = _hash_file(target)
            if existing_hash != expected_sha256 or existing_bytes != expected_bytes:
                raise PrimaryRepairRecoveryError(
                    "durable raw response spool conflicts with its hash"
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".primary-repair-raw-",
            suffix=".tmp",
            dir=str(self.raw_spool_root),
        )
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            copied = 0
            with source.open("rb") as input_handle, os.fdopen(
                descriptor, "wb"
            ) as output_handle:
                while True:
                    chunk = input_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    output_handle.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if digest.hexdigest() != expected_sha256 or copied != expected_bytes:
                raise PrimaryRepairRecoveryError(
                    "raw response changed while copying its spool"
                )
            atomic_replace_with_retry(temporary, target, timeout_seconds=5.0)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def persist_raw_response(
        self,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        *,
        spool_path: str | Path,
        sha256: str,
        byte_count: int,
        http_status: int | None,
        content_type: str,
        response_protocol: str,
        complete: bool,
        receipt_binding: Mapping[str, Any],
        attempt_number: int = 1,
        completion_proof: str | None = None,
    ) -> PrimaryRepairResponseRefV1:
        """Copy an fsynced wire spool into the job journal before adapter return."""

        reservation_id = str(reservation_id or "").strip()
        if not reservation_id:
            raise PrimaryRepairRecoveryError("raw response has no aggregate reservation ID")
        if not isinstance(receipt_binding, Mapping) or not receipt_binding:
            raise PrimaryRepairRecoveryError("raw response has no receipt binding")
        receipt_binding = dict(receipt_binding)
        _canonical_json(receipt_binding)
        source = Path(spool_path).expanduser().resolve()
        if not source.is_file():
            raise PrimaryRepairRecoveryError("fsynced raw response spool is missing")
        expected_hash = str(sha256 or "")
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise PrimaryRepairRecoveryError("raw response SHA-256 is invalid")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise PrimaryRepairRecoveryError("raw response byte count is invalid")
        if not isinstance(complete, bool):
            raise PrimaryRepairRecoveryError("raw response completeness flag must be boolean")
        if completion_proof is None:
            completion_proof = "reader_eof" if complete else "partial_read"
        if completion_proof not in {
            "reader_eof",
            "content_length_exact",
            "semantic_complete_only",
            "partial_read",
            "legacy_unknown",
        }:
            raise PrimaryRepairRecoveryError("raw response completion proof is invalid")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise PrimaryRepairRecoveryError("raw response attempt number must be positive")
        if http_status is not None and (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or http_status < 100
        ):
            raise PrimaryRepairRecoveryError("raw response HTTP status is invalid")
        response_protocol = str(response_protocol or "").casefold()
        if response_protocol not in {"json", "sse"}:
            raise PrimaryRepairRecoveryError("raw response protocol is unsupported")
        actual_hash, actual_bytes = _hash_file(source)
        if actual_hash != expected_hash or actual_bytes != byte_count:
            raise PrimaryRepairRecoveryError("raw response spool hash or byte count changed")

        self.raw_spool_root.mkdir(parents=True, exist_ok=True)
        raw_root = self.raw_spool_root.resolve()
        attempt_metadata_path = self._attempt_metadata_path(scope, attempt_number)
        if attempt_metadata_path.is_file():
            intent = self._read_attempt_intent(
                attempt_metadata_path,
                scope=scope,
                reservation_id=reservation_id,
                attempt_number=attempt_number,
            )
            if (
                str(intent.get("phase") or "") != "response_headers_bound"
                or str(source) != str(intent.get("spool_path") or "")
                or intent.get("http_status") != http_status
                or str(intent.get("content_type") or "") != str(content_type or "")
            ):
                raise PrimaryRepairRecoveryError(
                    "raw response differs from its bound PRIMARY attempt headers"
                )
            if (
                complete
                and intent.get("content_length") is not None
                and intent.get("content_encoding") in {"", "identity"}
                and intent.get("transfer_encoding") in {"", "identity"}
                and byte_count != int(intent["content_length"])
            ):
                raise PrimaryRepairRecoveryError(
                    "complete PRIMARY raw response differs from its Content-Length"
                )
        durable_spool = raw_root / f"response-{expected_hash}.bin"
        source_is_journal_owned = source.parent == raw_root
        # If the adapter wrote its attempt spool in the journal root, first
        # atomically commit the exact scope/reservation/attempt metadata while
        # that source is still present. A crash during the content-addressed
        # copy can then recover from this verified original spool. Other
        # callers use a temporary source outside this root, so copy first.
        if not source_is_journal_owned:
            self._copy_raw_spool_to_content_addressed_path(
                source,
                durable_spool,
                expected_sha256=expected_hash,
                expected_bytes=byte_count,
            )
        journal_spool_path = source if source_is_journal_owned else durable_spool
        raw_spool = PrimaryRepairRawSpoolRefV1(
            path=str(journal_spool_path.resolve()),
            sha256=expected_hash,
            byte_count=byte_count,
            http_status=http_status,
            content_type=str(content_type or ""),
            response_protocol=response_protocol,
            complete=complete,
            attempt_number=attempt_number,
            completion_proof=str(completion_proof),
        )
        entry_kind = (
            "raw_response"
            if complete and http_status == 200
            else "semantic_raw_response"
            if (
                not complete
                and http_status == 200
                and completion_proof == "semantic_complete_only"
            )
            else "ambiguous_raw_response"
        )
        target = self._path_for_scope(scope)
        prior_attempts: tuple[PrimaryRepairRawSpoolRefV1, ...] = ()
        created_at = utc_now_iso()
        if target.exists():
            existing = self._read_entry(target, scope=scope)
            if (
                existing.reference.reservation_id != reservation_id
                or dict(existing.receipt_binding) != dict(receipt_binding)
                or existing.raw_spool is None
            ):
                raise PrimaryRepairRecoveryError(
                    "PRIMARY repair journal already contains a different response binding"
                )
            prior_attempts = existing.raw_attempts or (existing.raw_spool,)
            previous = prior_attempts[-1]
            if attempt_number == previous.attempt_number:
                same_raw_attempt = (
                    raw_spool.sha256 == previous.sha256
                    and raw_spool.byte_count == previous.byte_count
                    and raw_spool.http_status == previous.http_status
                    and raw_spool.content_type == previous.content_type
                    and raw_spool.response_protocol == previous.response_protocol
                    and raw_spool.complete == previous.complete
                    and raw_spool.attempt_number == previous.attempt_number
                    and raw_spool.completion_proof == previous.completion_proof
                )
                if same_raw_attempt:
                    return existing.reference
                raise PrimaryRepairRecoveryError(
                    "PRIMARY repair physical attempt already retained a different response"
                )
            if existing.entry_kind == "normalized_response":
                raise PrimaryRepairRecoveryError(
                    "cannot append a provider attempt after the response was normalized"
                )
            if attempt_number != previous.attempt_number + 1:
                raise PrimaryRepairRecoveryError(
                    "PRIMARY raw response attempt history has a missing or out-of-order attempt"
                )
            if not previous.complete:
                raise PrimaryRepairRecoveryError(
                    "cannot retry after an incomplete PRIMARY provider response"
                )
            created_at = existing.created_at
        elif attempt_number != 1:
            raise PrimaryRepairRecoveryError(
                "PRIMARY raw response history must start with physical attempt 1"
            )
        raw_attempts = prior_attempts + (raw_spool,)
        body: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "entry_kind": entry_kind,
            "created_at": created_at,
            "scope": scope.to_dict(),
            "reservation_id": reservation_id,
            "request_hash": scope.request_hash,
            "response": {},
            "response_sha256": expected_hash,
            "receipt_binding": dict(receipt_binding),
            "raw_spool": raw_spool.to_dict(),
            "raw_attempts": [item.to_dict() for item in raw_attempts],
        }
        body["record_sha256"] = _sha256(body)
        atomic_write_json(str(target), body)
        if source_is_journal_owned:
            self._copy_raw_spool_to_content_addressed_path(
                source,
                durable_spool,
                expected_sha256=expected_hash,
                expected_bytes=byte_count,
            )
            if source.resolve() != durable_spool.resolve():
                durable_raw_spool = PrimaryRepairRawSpoolRefV1(
                    path=str(durable_spool.resolve()),
                    sha256=raw_spool.sha256,
                    byte_count=raw_spool.byte_count,
                    http_status=raw_spool.http_status,
                    content_type=raw_spool.content_type,
                    response_protocol=raw_spool.response_protocol,
                    complete=raw_spool.complete,
                    attempt_number=raw_spool.attempt_number,
                    completion_proof=raw_spool.completion_proof,
                )
                body["raw_spool"] = durable_raw_spool.to_dict()
                body["raw_attempts"][-1] = durable_raw_spool.to_dict()
                body.pop("record_sha256", None)
                body["record_sha256"] = _sha256(body)
                atomic_write_json(str(target), body)
        return self._read_entry(target, scope=scope).reference

    @staticmethod
    def _normalized_response(response: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise PrimaryRepairRecoveryError("repair response must be an object")
        normalized = {
            str(key): value
            for key, value in response.items()
            if str(key) in PrimaryRepairResponseJournal._RESPONSE_FIELDS
        }
        if "status" not in normalized or "content" not in normalized:
            raise PrimaryRepairRecoveryError("repair response lacks status or content")
        if not isinstance(normalized["status"], str):
            raise PrimaryRepairRecoveryError("repair response status must be a string")
        normalized.setdefault("raw_response_path", "")
        normalized.setdefault("raw_response_sha256", "")
        normalized.setdefault("response_bytes", 0)
        try:
            return json.loads(_canonical_json(normalized))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PrimaryRepairRecoveryError(
                "repair response content is not canonical JSON"
            ) from exc

    def persist_response(
        self,
        scope: PrimaryRepairScopeV1,
        reservation_id: str,
        response: Mapping[str, Any],
        *,
        receipt_binding: Mapping[str, Any] | None = None,
    ) -> PrimaryRepairResponseRefV1:
        reservation_id = str(reservation_id or "").strip()
        if not reservation_id:
            raise PrimaryRepairRecoveryError("repair response has no aggregate reservation ID")
        normalized = self._normalized_response(response)
        normalized_receipt_binding = dict(receipt_binding or {})
        _canonical_json(normalized_receipt_binding)
        response_hash = _sha256(normalized)
        body: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "entry_kind": "normalized_response",
            "created_at": utc_now_iso(),
            "scope": scope.to_dict(),
            "reservation_id": reservation_id,
            "request_hash": scope.request_hash,
            "response": normalized,
            "response_sha256": response_hash,
            "receipt_binding": normalized_receipt_binding,
        }
        body["record_sha256"] = _sha256(body)
        target = self._path_for_scope(scope)
        if target.exists():
            existing_entry = self._read_entry(target, scope=scope)
            if (
                existing_entry.reference.reservation_id != reservation_id
                or dict(existing_entry.receipt_binding) != normalized_receipt_binding
            ):
                raise PrimaryRepairRecoveryError(
                    "PRIMARY repair response journal already contains a different response"
                )
            if existing_entry.entry_kind == "normalized_response":
                if dict(existing_entry.response) != normalized:
                    raise PrimaryRepairRecoveryError(
                        "PRIMARY repair response journal already contains a different response"
                    )
                return existing_entry.reference
            if existing_entry.raw_spool is None:
                raise PrimaryRepairRecoveryError("raw PRIMARY response entry lost its spool reference")
            last_raw_attempt = existing_entry.raw_spool.attempt_number
            try:
                normalized_attempts = int(normalized.get("attempts") or last_raw_attempt)
            except (TypeError, ValueError) as exc:
                raise PrimaryRepairRecoveryError(
                    "normalized PRIMARY response attempt count is invalid"
                ) from exc
            if normalized_attempts != last_raw_attempt:
                raise PrimaryRepairRecoveryError(
                    "normalized PRIMARY response attempt count differs from its raw history"
                )
            normalized["attempts"] = normalized_attempts
            if (
                not existing_entry.raw_spool.complete
                or existing_entry.raw_spool.http_status != 200
            ) and not (
                existing_entry.entry_kind == "semantic_raw_response"
                and existing_entry.raw_spool.completion_proof
                == "semantic_complete_only"
            ) and str(normalized.get("status") or "") == "success":
                raise PrimaryRepairRecoveryError(
                    "incomplete raw response cannot be promoted to a successful response"
                )
            body["created_at"] = existing_entry.created_at
            body["raw_spool"] = existing_entry.raw_spool.to_dict()
            if existing_entry.raw_attempts:
                body["raw_attempts"] = [
                    item.to_dict() for item in existing_entry.raw_attempts
                ]
            body["record_sha256"] = _sha256(
                {key: value for key, value in body.items() if key != "record_sha256"}
            )
        atomic_write_json(str(target), body)
        return self._read_entry(target, scope=scope).reference

    def _read_document(
        self,
        path: Path,
        *,
        scope: PrimaryRepairScopeV1,
    ) -> tuple[PrimaryRepairResponseRefV1, dict[str, Any]]:
        entry = self._read_entry(path, scope=scope)
        return entry.reference, dict(entry.response)

    def _read_entry(
        self,
        path: Path,
        *,
        scope: PrimaryRepairScopeV1,
    ) -> PrimaryRepairResponseEntryV1:
        target = path.expanduser().resolve()
        root = self.root.resolve()
        if target.parent != root:
            raise PrimaryRepairRecoveryError("repair response journal path escaped its root")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PrimaryRepairRecoveryError("repair response journal is unreadable") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise PrimaryRepairRecoveryError("repair response journal schema is invalid")
        stored_scope = PrimaryRepairScopeV1.from_dict(payload.get("scope") or {})
        entry_kind = str(payload.get("entry_kind") or "normalized_response")
        raw_spool_payload = payload.get("raw_spool")
        raw_spool: PrimaryRepairRawSpoolRefV1 | None = None
        raw_attempts: tuple[PrimaryRepairRawSpoolRefV1, ...] = ()
        if raw_spool_payload is not None:
            raw_spool = PrimaryRepairRawSpoolRefV1.from_dict(raw_spool_payload)
            if entry_kind not in {
                "raw_response",
                "semantic_raw_response",
                "ambiguous_raw_response",
                "normalized_response",
            }:
                raise PrimaryRepairRecoveryError("raw response journal entry kind is invalid")
            attempts_payload = payload.get("raw_attempts")
            if attempts_payload is None:
                raw_attempts = (raw_spool,)
            else:
                if not isinstance(attempts_payload, list) or not attempts_payload:
                    raise PrimaryRepairRecoveryError("raw response attempt history is invalid")
                try:
                    raw_attempts = tuple(
                        PrimaryRepairRawSpoolRefV1.from_dict(item)
                        for item in attempts_payload
                    )
                except (TypeError, PrimaryRepairRecoveryError) as exc:
                    raise PrimaryRepairRecoveryError(
                        "raw response attempt history is invalid"
                    ) from exc
            if raw_attempts[-1] != raw_spool:
                raise PrimaryRepairRecoveryError(
                    "raw response latest spool differs from its attempt history"
                )
            if tuple(item.attempt_number for item in raw_attempts) != tuple(
                range(1, len(raw_attempts) + 1)
            ):
                raise PrimaryRepairRecoveryError(
                    "raw response attempt history is not contiguous from attempt 1"
                )
            for attempt in raw_attempts:
                raw_spool_path = Path(attempt.path).expanduser().resolve()
                if raw_spool_path.parent != self.raw_spool_root.resolve():
                    raise PrimaryRepairRecoveryError(
                        "raw response spool escaped its journal root"
                    )
                actual_hash, actual_bytes = _hash_file(raw_spool_path)
                if actual_hash != attempt.sha256 or actual_bytes != attempt.byte_count:
                    raise PrimaryRepairRecoveryError(
                        "raw response spool bytes do not match the retained attempt binding"
                    )
        elif entry_kind != "normalized_response":
            raise PrimaryRepairRecoveryError("raw response journal entry lacks its spool reference")
        response = (
            {}
            if entry_kind
            in {
                "raw_response",
                "semantic_raw_response",
                "ambiguous_raw_response",
            }
            else self._normalized_response(payload.get("response") or {})
        )
        raw_receipt_binding = payload.get("receipt_binding") or {}
        if not isinstance(raw_receipt_binding, Mapping):
            raise PrimaryRepairRecoveryError("repair response receipt binding is invalid")
        receipt_binding = dict(raw_receipt_binding)
        _canonical_json(receipt_binding)
        created_at = str(payload.get("created_at") or "")
        reservation_id = str(payload.get("reservation_id") or "")
        request_hash = str(payload.get("request_hash") or "")
        response_hash = str(payload.get("response_sha256") or "")
        record_hash = str(payload.get("record_sha256") or "")
        body = dict(payload)
        body.pop("record_sha256", None)
        expected_response_hash = (
            raw_spool.sha256
            if entry_kind
            in {"raw_response", "semantic_raw_response", "ambiguous_raw_response"}
            and raw_spool is not None
            else _sha256(response)
        )
        if (
            stored_scope.scope_id != scope.scope_id
            or stored_scope.fingerprint_sha256 != scope.fingerprint_sha256
            or request_hash != scope.request_hash
            or not reservation_id
            or not created_at
            or response_hash != expected_response_hash
            or record_hash != _sha256(body)
        ):
            raise PrimaryRepairRecoveryError("repair response journal identity or hash is invalid")
        if entry_kind == "raw_response" and (
            raw_spool is None or not raw_spool.complete or raw_spool.http_status != 200
        ):
            raise PrimaryRepairRecoveryError("raw success journal entry is incomplete or not HTTP 200")
        if entry_kind == "semantic_raw_response" and (
            raw_spool is None
            or raw_spool.complete
            or raw_spool.http_status != 200
            or raw_spool.completion_proof != "semantic_complete_only"
        ):
            raise PrimaryRepairRecoveryError(
                "semantic raw response does not preserve its incomplete HTTP-body status"
            )
        if entry_kind == "ambiguous_raw_response" and (
            raw_spool is None or (raw_spool.complete and raw_spool.http_status == 200)
        ):
            raise PrimaryRepairRecoveryError("ambiguous raw journal entry incorrectly claims success")
        ref = PrimaryRepairResponseRefV1(
            path=str(target),
            scope_id=stored_scope.scope_id,
            scope_fingerprint_sha256=stored_scope.fingerprint_sha256,
            request_hash=request_hash,
            reservation_id=reservation_id,
            response_sha256=response_hash,
            record_sha256=record_hash,
        )
        return PrimaryRepairResponseEntryV1(
            reference=ref,
            entry_kind=entry_kind,
            response=response,
            receipt_binding=receipt_binding,
            created_at=created_at,
            raw_spool=raw_spool,
            raw_attempts=raw_attempts,
        )

    def load(
        self,
        reference: PrimaryRepairResponseRefV1 | Mapping[str, Any],
        *,
        scope: PrimaryRepairScopeV1,
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        ref = (
            reference
            if isinstance(reference, PrimaryRepairResponseRefV1)
            else PrimaryRepairResponseRefV1.from_dict(reference)
        )
        if ref.path != str(self._path_for_scope(scope).resolve()):
            raise PrimaryRepairRecoveryError("repair response reference path does not match its scope")
        stored_ref, response = self._read_document(Path(ref.path), scope=scope)
        if stored_ref != ref:
            raise PrimaryRepairRecoveryError("repair response reference does not match its journal")
        if reservation_id is not None and ref.reservation_id != str(reservation_id):
            raise PrimaryRepairRecoveryError("repair response reservation ID does not match")
        return response

    def load_for_scope(
        self,
        scope: PrimaryRepairScopeV1,
        *,
        reservation_id: str | None = None,
    ) -> tuple[PrimaryRepairResponseRefV1, dict[str, Any]] | None:
        target = self._path_for_scope(scope)
        if not target.exists():
            return None
        ref, response = self._read_document(target, scope=scope)
        if reservation_id is not None and ref.reservation_id != str(reservation_id):
            raise PrimaryRepairRecoveryError("repair response reservation ID does not match")
        return ref, response

    def load_entry_for_scope(
        self,
        scope: PrimaryRepairScopeV1,
        *,
        reservation_id: str | None = None,
    ) -> PrimaryRepairResponseEntryV1 | None:
        target = self._path_for_scope(scope)
        if not target.exists():
            return None
        entry = self._read_entry(target, scope=scope)
        if reservation_id is not None and entry.reference.reservation_id != str(reservation_id):
            raise PrimaryRepairRecoveryError("repair response reservation ID does not match")
        return entry


__all__ = [
    "PrimaryRepairRawSpoolRefV1",
    "PrimaryRepairRecoveryError",
    "PrimaryRepairResponseEntryV1",
    "PrimaryRepairResponseJournal",
    "PrimaryRepairResponseRefV1",
    "PrimaryRepairScopeV1",
]

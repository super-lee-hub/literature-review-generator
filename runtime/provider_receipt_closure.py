"""Typed expected-call-graph validation for provider receipts.

Completion is allowed to consume this result, rather than treating a
non-empty JSONL ledger as proof that the current execution completed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from runtime.provider_runtime import ProviderCallReceiptV1, compute_closure_epoch_id, hash_json
from services.artifact_registry import file_sha256


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpectedProviderCall:
    """The immutable identity and output contract for one provider node."""

    call_id: str
    job_id: str
    attempt_id: str
    stage_name: str
    node_id: str
    closure_epoch_id: str = ""
    logical_attempt_identity: str = ""
    expected_call_graph_hash: str = ""
    prompt_hash: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    input_hash: str = ""
    config_hash: str = ""
    schema_hash: str = ""
    output_hash: str = ""
    provider_response_hash: str = ""
    normalized_output_hash: str = ""
    artifact_payload_hash: str = ""
    artifact_content_hash: str = ""
    registry_file_hash: str = ""
    artifact_path: str = ""
    registered_artifact_hash: str = ""
    replay_output_hash: str = ""
    node_output_hash: str = ""
    max_attempts: int = 0
    usage_required: bool = False
    verified_reuse: bool = False
    reuse_evidence_artifact_id: str = ""
    reuse_evidence_artifact_hash: str = ""
    reuse_evidence_record_hash: str = ""
    # Optional provider identity. Older stage callers may omit these fields;
    # current routed Outline calls populate them so closure validation proves
    # the receipt came from the route that was expected, not only from a
    # matching prompt/config hash.
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    endpoint_type: str = ""
    # A transport node may have a declared primary and backup request identity.
    # Each variant is still exact; this is not a wildcard for mismatched calls.
    request_variants: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExpectedProviderCall":
        return cls(
            call_id=str(payload.get("call_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            stage_name=str(payload.get("stage_name") or ""),
            node_id=str(payload.get("node_id") or ""),
            closure_epoch_id=str(payload.get("closure_epoch_id") or ""),
            logical_attempt_identity=str(payload.get("logical_attempt_identity") or ""),
            expected_call_graph_hash=str(payload.get("expected_call_graph_hash") or ""),
            prompt_hash=str(payload.get("prompt_hash") or ""),
            prompt_id=str(payload.get("prompt_id") or ""),
            prompt_version=str(payload.get("prompt_version") or ""),
            prompt_sha256=str(payload.get("prompt_sha256") or ""),
            input_hash=str(payload.get("input_hash") or ""),
            config_hash=str(payload.get("config_hash") or ""),
            schema_hash=str(payload.get("schema_hash") or ""),
            output_hash=str(payload.get("output_hash") or ""),
            provider_response_hash=str(payload.get("provider_response_hash") or ""),
            normalized_output_hash=str(payload.get("normalized_output_hash") or ""),
            artifact_payload_hash=str(payload.get("artifact_payload_hash") or ""),
            artifact_content_hash=str(payload.get("artifact_content_hash") or ""),
            registry_file_hash=str(payload.get("registry_file_hash") or ""),
            artifact_path=str(payload.get("artifact_path") or ""),
            registered_artifact_hash=str(payload.get("registered_artifact_hash") or ""),
            replay_output_hash=str(payload.get("replay_output_hash") or ""),
            node_output_hash=str(payload.get("node_output_hash") or ""),
            max_attempts=max(0, int(payload.get("max_attempts") or 0)),
            usage_required=bool(payload.get("usage_required", False)),
            verified_reuse=bool(payload.get("verified_reuse", False)),
            reuse_evidence_artifact_id=str(payload.get("reuse_evidence_artifact_id") or ""),
            reuse_evidence_artifact_hash=str(payload.get("reuse_evidence_artifact_hash") or ""),
            reuse_evidence_record_hash=str(payload.get("reuse_evidence_record_hash") or ""),
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            endpoint=str(payload.get("endpoint") or ""),
            endpoint_type=str(payload.get("endpoint_type") or ""),
            request_variants=tuple(
                dict(item)
                for item in (payload.get("request_variants") or ())
                if isinstance(item, Mapping)
            ),
        )

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (self.call_id, self.job_id, self.attempt_id, self.stage_name, self.node_id)):
            raise ValueError("expected provider calls require call, job, attempt, stage, and node identities")
        if self.max_attempts < 0:
            raise ValueError("max_attempts cannot be negative")
        object.__setattr__(self, "request_variants", tuple(dict(item) for item in self.request_variants))


@dataclass(frozen=True)
class ReceiptClosureResult:
    closure_epoch_id: str = ""
    expected_call_ids: tuple[str, ...] = ()
    observed_call_ids: tuple[str, ...] = ()
    missing_call_ids: tuple[str, ...] = ()
    stale_call_ids: tuple[str, ...] = ()
    failed_call_ids: tuple[str, ...] = ()
    incomplete_call_ids: tuple[str, ...] = ()
    hash_mismatches: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unexpected_receipts: tuple[str, ...] = ()
    out_of_scope_receipts: tuple[str, ...] = ()
    out_of_epoch_receipts: tuple[str, ...] = ()
    historical_receipts: tuple[str, ...] = ()
    retry_exceeded_call_ids: tuple[str, ...] = ()
    usage_incomplete_call_ids: tuple[str, ...] = ()
    verified_reuse_call_ids: tuple[str, ...] = ()
    complete: bool = False
    closure_hash: str = ""

    def __post_init__(self) -> None:
        for name in (
            "expected_call_ids",
            "observed_call_ids",
            "missing_call_ids",
            "stale_call_ids",
            "failed_call_ids",
            "incomplete_call_ids",
            "unexpected_receipts",
            "out_of_scope_receipts",
            "out_of_epoch_receipts",
            "historical_receipts",
            "retry_exceeded_call_ids",
            "usage_incomplete_call_ids",
            "verified_reuse_call_ids",
        ):
            object.__setattr__(self, name, tuple(dict.fromkeys(str(item) for item in getattr(self, name) if str(item))))
        object.__setattr__(self, "hash_mismatches", {
            str(key): tuple(str(item) for item in values)
            for key, values in self.hash_mismatches.items()
        })

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "expected_call_ids",
            "observed_call_ids",
            "missing_call_ids",
            "stale_call_ids",
            "failed_call_ids",
            "incomplete_call_ids",
            "unexpected_receipts",
            "out_of_scope_receipts",
            "out_of_epoch_receipts",
            "historical_receipts",
            "retry_exceeded_call_ids",
            "usage_incomplete_call_ids",
            "verified_reuse_call_ids",
        ):
            payload[key] = list(getattr(self, key))
        payload["hash_mismatches"] = {key: list(value) for key, value in self.hash_mismatches.items()}
        return payload


class ProviderReceiptClosure:
    """Verify receipt identity, status, hashes, output, and retry ceilings."""

    @classmethod
    def evaluate(
        cls,
        expected: Iterable[ExpectedProviderCall | Mapping[str, Any]],
        observed: Iterable[ProviderCallReceiptV1 | Mapping[str, Any]],
        *,
        out_of_scope: Iterable[ProviderCallReceiptV1 | Mapping[str, Any]] = (),
    ) -> ReceiptClosureResult:
        expected_items = [item if isinstance(item, ExpectedProviderCall) else ExpectedProviderCall.from_mapping(item) for item in expected]
        receipts = [item if isinstance(item, ProviderCallReceiptV1) else ProviderCallReceiptV1.from_dict(item) for item in observed]
        out_of_scope_receipts = [
            item if isinstance(item, ProviderCallReceiptV1) else ProviderCallReceiptV1.from_dict(item)
            for item in out_of_scope
        ]
        expected_by_id = {item.call_id: item for item in expected_items}
        expected_epochs = {str(item.closure_epoch_id) for item in expected_items if str(item.closure_epoch_id)}
        if len(expected_epochs) == 1:
            closure_epoch_id = next(iter(expected_epochs))
        elif expected_epochs:
            closure_epoch_id = _hash(sorted(expected_epochs))
        else:
            # Legacy callers did not provide an epoch.  Their explicit
            # ``observed`` collection remains the current collection, while
            # ``out_of_scope`` is retained as forensic history.
            closure_epoch_id = ""

        if expected_epochs:
            current_receipts = [
                receipt for receipt in receipts if str(receipt.closure_epoch_id or "") in expected_epochs
            ]
            out_of_epoch_receipts = tuple(
                sorted(
                    {
                        str(receipt.receipt_id or receipt.call_id)
                        for receipt in receipts
                        if str(receipt.closure_epoch_id or "") not in expected_epochs
                        and str(receipt.receipt_id or receipt.call_id)
                    }
                )
            )
        else:
            current_receipts = list(receipts)
            out_of_epoch_receipts = ()
        historical_receipts = tuple(
            sorted(
                {
                    str(receipt.receipt_id or receipt.call_id)
                    for receipt in receipts
                    if str(receipt.receipt_id or receipt.call_id) and receipt not in current_receipts
                }
                | {
                    str(receipt.receipt_id or receipt.call_id)
                    for receipt in out_of_scope_receipts
                    if str(receipt.receipt_id or receipt.call_id)
                }
            )
        )
        receipts_by_id: dict[str, list[ProviderCallReceiptV1]] = {}
        for receipt in current_receipts:
            receipts_by_id.setdefault(receipt.call_id, []).append(receipt)

        expected_ids = tuple(sorted(expected_by_id))
        observed_ids = tuple(sorted(receipts_by_id))
        verified_reuse_ids = tuple(
            sorted(
                call_id
                for call_id, contract in expected_by_id.items()
                if contract.verified_reuse
            )
        )
        missing: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        incomplete: list[str] = []
        retry_exceeded: list[str] = []
        usage_incomplete: list[str] = []
        mismatches: dict[str, tuple[str, ...]] = {}

        for call_id, contract in expected_by_id.items():
            if contract.verified_reuse:
                if (
                    not contract.reuse_evidence_artifact_id
                    or not contract.reuse_evidence_artifact_hash
                    or not contract.reuse_evidence_record_hash
                    or not contract.artifact_path
                    or not contract.registry_file_hash
                    or not contract.artifact_payload_hash
                    or not contract.normalized_output_hash
                    or not contract.provider_response_hash
                ):
                    stale.append(call_id)
                    mismatches[call_id] = ("reuse_evidence_incomplete",)
                continue
            candidates = receipts_by_id.get(call_id, [])
            if not candidates:
                missing.append(call_id)
                continue
            current = max(candidates, key=lambda item: (item.attempts, item.sequence, item.finished_at))
            variant_matches = [
                variant
                for variant in contract.request_variants
                if all(
                    not str(field).strip()
                    or str(getattr(current, str(field), "") or "") == str(value or "")
                    for field, value in variant.items()
                )
            ]
            identity_mismatches = {
                field: (str(getattr(current, field) or ""), str(getattr(contract, field) or ""))
                for field in (
                    "job_id",
                    "attempt_id",
                    "stage_name",
                    "node_id",
                    "prompt_hash",
                    "prompt_id",
                    "prompt_version",
                    "prompt_sha256",
                    "input_hash",
                    "config_hash",
                    "schema_hash",
                    "logical_attempt_identity",
                )
                if getattr(contract, field)
                and str(getattr(current, field) or "") != str(getattr(contract, field) or "")
                and not (variant_matches and field in {"input_hash", "config_hash"})
            }
            for field, actual_field in (
                ("provider", "provider"),
                ("model", "model"),
                ("endpoint", "endpoint"),
                ("endpoint_type", "endpoint_type"),
            ):
                expected_value = str(getattr(contract, field) or "")
                actual_value = str(getattr(current, actual_field) or "")
                if expected_value and expected_value != actual_value:
                    identity_mismatches[field] = (actual_value, expected_value)
            # A variant must explicitly bind both input and config identity;
            # partial variants are never allowed to excuse a mismatch.
            if variant_matches and not any(
                str(variant.get("input_hash") or "")
                and str(variant.get("config_hash") or "")
                for variant in variant_matches
            ):
                variant_matches = []
                identity_mismatches = {
                    field: (str(getattr(current, field) or ""), str(getattr(contract, field) or ""))
                    for field in (
                        "job_id", "attempt_id", "stage_name", "node_id", "prompt_hash", "prompt_id",
                        "prompt_version", "prompt_sha256", "input_hash", "config_hash", "schema_hash",
                        "logical_attempt_identity",
                    )
                    if getattr(contract, field) and str(getattr(current, field) or "") != str(getattr(contract, field) or "")
                }
            if identity_mismatches:
                stale.append(call_id)
                mismatches[call_id] = tuple(sorted(identity_mismatches))
            if current.status != "success":
                failed.append(call_id)
            if current.status == "success" and (current.incomplete_reason or current.finish_reason == "length"):
                incomplete.append(call_id)
            if current.status == "success":
                mismatch_fields = set(mismatches.get(call_id, ()))
                response_hash = str(current.response_hash or "")
                expected_response_hash = str(contract.provider_response_hash or contract.output_hash or "")
                normalized_hash = str(contract.normalized_output_hash or "")
                payload_hash = str(contract.artifact_payload_hash or "")
                content_hash = str(contract.artifact_content_hash or contract.registered_artifact_hash or "")
                registry_hash = str(contract.registry_file_hash or "")
                node_hash = str(contract.node_output_hash or "")
                replay_hash = str(contract.replay_output_hash or "")
                if not response_hash:
                    mismatch_fields.add("response_hash_missing")
                if expected_response_hash and response_hash != expected_response_hash:
                    mismatch_fields.add("provider_response_hash")
                if not normalized_hash:
                    mismatch_fields.add("normalized_output_hash_missing")
                elif response_hash != normalized_hash:
                    mismatch_fields.add("normalized_output_hash")
                if not payload_hash:
                    mismatch_fields.add("artifact_payload_hash_missing")
                # The provider response and the persisted artifact payload are
                # intentionally different hash domains.  A writer may add
                # citation spans, receipt IDs, or other durable metadata while
                # normalizing a successful response; the artifact envelope
                # check below validates the payload hash independently.
                if not content_hash:
                    mismatch_fields.add("artifact_content_hash_missing")
                if not registry_hash:
                    mismatch_fields.add("registry_file_hash_missing")
                elif current.metadata.get("registry_file_hash") and str(current.metadata.get("registry_file_hash")) != registry_hash:
                    mismatch_fields.add("registry_file_hash")
                registry_path = str(contract.artifact_path or current.metadata.get("registry_file_path") or "")
                if registry_hash and registry_path:
                    try:
                        if file_sha256(registry_path) != registry_hash:
                            mismatch_fields.add("registered_file_hash")
                    except (OSError, TypeError, ValueError):
                        mismatch_fields.add("registered_file_unreadable")
                if content_hash and registry_path:
                    try:
                        envelope = json.loads(Path(registry_path).read_text(encoding="utf-8"))
                        if isinstance(envelope, Mapping):
                            # Some artifact writers keep the registry hash
                            # outside the JSON envelope.  When an embedded
                            # hash exists it must agree; its absence is not a
                            # second, circular content-hash requirement.
                            embedded_content_hash = str(envelope.get("content_hash") or "")
                            if embedded_content_hash and embedded_content_hash != content_hash:
                                mismatch_fields.add("artifact_content_hash")
                            payload = envelope.get("payload")
                            if payload is None:
                                payload = envelope.get("section")
                            if payload is None:
                                payload = envelope.get("analysis")
                            if (
                                payload is None
                                and envelope.get("artifact_type") == "stage1_visual_observations"
                            ):
                                payload = {
                                    "artifact_type": envelope.get("artifact_type"),
                                    "artifact_version": envelope.get("artifact_version"),
                                    "observations": envelope.get("observations") or [],
                                }
                            if payload_hash and hash_json(payload) != payload_hash:
                                mismatch_fields.add("artifact_payload_hash")
                    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                        mismatch_fields.add("artifact_unreadable")
                if not normalized_hash:
                    mismatch_fields.add("normalized_output_hash_missing")
                if content_hash and node_hash and content_hash != node_hash:
                    mismatch_fields.add("node_artifact_hash")
                elif content_hash and not node_hash:
                    mismatch_fields.add("node_output_hash_missing")
                elif node_hash and not content_hash:
                    mismatch_fields.add("artifact_content_hash_missing")
                if replay_hash and normalized_hash and replay_hash != normalized_hash:
                    mismatch_fields.add("replay_output_hash")
                if replay_hash and current.metadata.get("replay_output_hash") and str(current.metadata.get("replay_output_hash")) != replay_hash:
                    mismatch_fields.add("replay_output_hash")
                if mismatch_fields:
                    mismatches[call_id] = tuple(sorted(mismatch_fields))
                else:
                    mismatches.pop(call_id, None)
            if contract.max_attempts and current.attempts > contract.max_attempts:
                retry_exceeded.append(call_id)
            if contract.usage_required and current.usage_status not in {"reported", "provider_not_supported"}:
                usage_incomplete.append(call_id)

        unexpected = tuple(sorted(set(observed_ids) - set(expected_ids)))
        out_of_scope_ids = tuple(
            sorted({str(item.call_id) for item in out_of_scope_receipts if str(item.call_id)})
        )
        complete = not any(
            (
                missing,
                stale,
                failed,
                incomplete,
                mismatches,
                unexpected,
                retry_exceeded,
                usage_incomplete,
            )
        )
        payload = {
            "closure_epoch_id": closure_epoch_id,
            "expected_call_ids": expected_ids,
            "observed_call_ids": observed_ids,
            "missing_call_ids": tuple(sorted(missing)),
            "stale_call_ids": tuple(sorted(stale)),
            "failed_call_ids": tuple(sorted(failed)),
            "incomplete_call_ids": tuple(sorted(incomplete)),
            "hash_mismatches": mismatches,
            "unexpected_receipts": unexpected,
            "out_of_scope_receipts": out_of_scope_ids,
            "out_of_epoch_receipts": out_of_epoch_receipts,
            "historical_receipts": historical_receipts,
            "retry_exceeded_call_ids": tuple(sorted(retry_exceeded)),
            "usage_incomplete_call_ids": tuple(sorted(usage_incomplete)),
            "verified_reuse_call_ids": verified_reuse_ids,
            "complete": complete,
        }
        return ReceiptClosureResult(**payload, closure_hash=_hash(payload))


__all__ = [
    "ExpectedProviderCall",
    "ProviderReceiptClosure",
    "ReceiptClosureResult",
    "compute_closure_epoch_id",
]

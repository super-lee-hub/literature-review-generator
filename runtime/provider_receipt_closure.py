"""Typed expected-call-graph validation for provider receipts.

Completion is allowed to consume this result, rather than treating a
non-empty JSONL ledger as proof that the current execution completed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping

from runtime.provider_runtime import ProviderCallReceiptV1


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
    prompt_hash: str = ""
    input_hash: str = ""
    config_hash: str = ""
    schema_hash: str = ""
    output_hash: str = ""
    max_attempts: int = 0
    usage_required: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ExpectedProviderCall":
        return cls(
            call_id=str(payload.get("call_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            stage_name=str(payload.get("stage_name") or ""),
            node_id=str(payload.get("node_id") or ""),
            prompt_hash=str(payload.get("prompt_hash") or ""),
            input_hash=str(payload.get("input_hash") or ""),
            config_hash=str(payload.get("config_hash") or ""),
            schema_hash=str(payload.get("schema_hash") or ""),
            output_hash=str(payload.get("output_hash") or ""),
            max_attempts=max(0, int(payload.get("max_attempts") or 0)),
            usage_required=bool(payload.get("usage_required", False)),
        )

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (self.call_id, self.job_id, self.attempt_id, self.stage_name, self.node_id)):
            raise ValueError("expected provider calls require call, job, attempt, stage, and node identities")
        if self.max_attempts < 0:
            raise ValueError("max_attempts cannot be negative")


@dataclass(frozen=True)
class ReceiptClosureResult:
    expected_call_ids: tuple[str, ...] = ()
    observed_call_ids: tuple[str, ...] = ()
    missing_call_ids: tuple[str, ...] = ()
    stale_call_ids: tuple[str, ...] = ()
    failed_call_ids: tuple[str, ...] = ()
    incomplete_call_ids: tuple[str, ...] = ()
    hash_mismatches: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unexpected_receipts: tuple[str, ...] = ()
    retry_exceeded_call_ids: tuple[str, ...] = ()
    usage_incomplete_call_ids: tuple[str, ...] = ()
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
            "retry_exceeded_call_ids",
            "usage_incomplete_call_ids",
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
            "retry_exceeded_call_ids",
            "usage_incomplete_call_ids",
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
    ) -> ReceiptClosureResult:
        expected_items = [item if isinstance(item, ExpectedProviderCall) else ExpectedProviderCall.from_mapping(item) for item in expected]
        receipts = [item if isinstance(item, ProviderCallReceiptV1) else ProviderCallReceiptV1.from_dict(item) for item in observed]
        expected_by_id = {item.call_id: item for item in expected_items}
        receipts_by_id: dict[str, list[ProviderCallReceiptV1]] = {}
        for receipt in receipts:
            receipts_by_id.setdefault(receipt.call_id, []).append(receipt)

        expected_ids = tuple(sorted(expected_by_id))
        observed_ids = tuple(sorted(receipts_by_id))
        missing: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        incomplete: list[str] = []
        retry_exceeded: list[str] = []
        usage_incomplete: list[str] = []
        mismatches: dict[str, tuple[str, ...]] = {}

        for call_id, contract in expected_by_id.items():
            candidates = receipts_by_id.get(call_id, [])
            if not candidates:
                missing.append(call_id)
                continue
            current = max(candidates, key=lambda item: (item.attempts, item.sequence, item.finished_at))
            identity_mismatches = {
                field: (str(getattr(current, field) or ""), str(getattr(contract, field) or ""))
                for field in ("job_id", "attempt_id", "stage_name", "node_id", "prompt_hash", "input_hash", "config_hash", "schema_hash")
                if getattr(contract, field) and str(getattr(current, field) or "") != str(getattr(contract, field) or "")
            }
            if identity_mismatches:
                stale.append(call_id)
                mismatches[call_id] = tuple(sorted(identity_mismatches))
            if current.status != "success":
                failed.append(call_id)
            if current.status == "success" and (current.incomplete_reason or current.finish_reason == "length"):
                incomplete.append(call_id)
            if contract.output_hash and current.response_hash != contract.output_hash:
                mismatches.setdefault(call_id, tuple())
                mismatches[call_id] = tuple(sorted(set((*mismatches[call_id], "response_hash"))))
            if contract.max_attempts and current.attempts > contract.max_attempts:
                retry_exceeded.append(call_id)
            if contract.usage_required and current.usage_status not in {"reported", "provider_not_supported"}:
                usage_incomplete.append(call_id)

        unexpected = tuple(sorted(set(observed_ids) - set(expected_ids)))
        complete = not any((missing, stale, failed, incomplete, mismatches, unexpected, retry_exceeded, usage_incomplete))
        payload = {
            "expected_call_ids": expected_ids,
            "observed_call_ids": observed_ids,
            "missing_call_ids": tuple(sorted(missing)),
            "stale_call_ids": tuple(sorted(stale)),
            "failed_call_ids": tuple(sorted(failed)),
            "incomplete_call_ids": tuple(sorted(incomplete)),
            "hash_mismatches": mismatches,
            "unexpected_receipts": unexpected,
            "retry_exceeded_call_ids": tuple(sorted(retry_exceeded)),
            "usage_incomplete_call_ids": tuple(sorted(usage_incomplete)),
            "complete": complete,
        }
        return ReceiptClosureResult(**payload, closure_hash=_hash(payload))


__all__ = ["ExpectedProviderCall", "ProviderReceiptClosure", "ReceiptClosureResult"]

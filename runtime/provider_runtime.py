from __future__ import annotations

"""Provider-call budgets, redacted receipts, and fail-closed error taxonomy.

This module is deliberately transport-neutral.  The HTTP adapter performs the
request, while this runtime owns the retry ceiling and records the durable
facts needed to audit that decision.  Secrets and raw prompts never belong in
a receipt.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Literal, Mapping
import uuid

from services.job_workspace import utc_now_iso


PROVIDER_RECEIPT_ARTIFACT_TYPE = "provider_call_receipt"
PROVIDER_RECEIPT_ARTIFACT_VERSION = "v2"
PROVIDER_RECEIPT_LEDGER_VERSION = "provider-receipt-ledger-v1"

ProviderErrorKind = Literal[
    "quota_exhausted",
    "retryable_http",
    "fatal_config_or_auth",
    "transient_network",
    "invalid_response",
    "budget_exhausted",
    "cancelled",
]
ProviderCallStatus = Literal["success", "failed", "blocked"]

_ERROR_KINDS = frozenset(
    {
        "quota_exhausted",
        "retryable_http",
        "fatal_config_or_auth",
        "transient_network",
        "invalid_response",
        "budget_exhausted",
        "cancelled",
    }
)
_CALL_STATUSES = frozenset({"success", "failed", "blocked"})
_SECRET_KEY_MARKERS = frozenset(
    {"api_key", "apikey", "authorization", "password", "secret", "token", "credential"}
)
_REDACTION_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"),
)
_LEDGER_LOCK_GUARD = threading.Lock()
_LEDGER_LOCKS: dict[str, threading.RLock] = {}


class ProviderRuntimeContractError(ValueError):
    """Raised when a provider budget or receipt violates its contract."""


class ProviderBudgetExceeded(RuntimeError):
    """Raised only by explicit callers that request strict admission."""


class ProviderReceiptConflict(RuntimeError):
    """Raised when an append-only receipt ID is reused with different content."""


def _ledger_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LEDGER_LOCK_GUARD:
        lock = _LEDGER_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LEDGER_LOCKS[key] = lock
        return lock


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProviderRuntimeContractError(f"value is not canonical JSON: {exc}") from exc


def stable_provider_hash(domain: str, value: Any) -> str:
    if not str(domain).strip():
        raise ProviderRuntimeContractError("hash domain is required")
    return hashlib.sha256(f"auto-generate\x00{domain}\x00{_canonical_json(value)}".encode("utf-8")).hexdigest()


def hash_text(value: str) -> str:
    return stable_provider_hash("text", str(value))


def hash_json(value: Any) -> str:
    try:
        return stable_provider_hash("json", value)
    except ProviderRuntimeContractError:
        return stable_provider_hash("repr", repr(value))


def _redact_text(value: Any) -> str:
    text = str(value or "")
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:2000]


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        folded = key.casefold().replace("-", "_")
        if any(marker in folded for marker in _SECRET_KEY_MARKERS):
            result[key] = "[REDACTED_SECRET]"
        elif isinstance(raw_value, Mapping):
            result[key] = _redact_mapping(raw_value)
        elif isinstance(raw_value, (list, tuple)):
            result[key] = [
                _redact_mapping(item) if isinstance(item, Mapping) else _redact_text(item)
                for item in raw_value
            ]
        else:
            result[key] = raw_value
    return result


@dataclass(frozen=True)
class ProviderBudgetV1:
    """Per-runtime admission limits; zero means unlimited for that dimension."""

    max_calls: int = 0
    max_total_tokens: int = 0
    max_elapsed_seconds: float = 0.0
    max_retries_per_call: int = 0

    def __post_init__(self) -> None:
        for name in ("max_calls", "max_total_tokens", "max_retries_per_call"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise ProviderRuntimeContractError(f"{name} must be a non-negative integer")
            object.__setattr__(self, name, int(value))
        if float(self.max_elapsed_seconds) < 0:
            raise ProviderRuntimeContractError("max_elapsed_seconds must be non-negative")
        object.__setattr__(self, "max_elapsed_seconds", float(self.max_elapsed_seconds))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProviderBudgetV1":
        source = value or {}

        def integer(name: str) -> int:
            raw = source.get(name, 0)
            try:
                return max(0, int(str(raw).strip()))
            except (TypeError, ValueError):
                return 0

        def real(name: str) -> float:
            raw = source.get(name, 0.0)
            try:
                return max(0.0, float(str(raw).strip()))
            except (TypeError, ValueError):
                return 0.0

        return cls(
            max_calls=integer("max_calls"),
            max_total_tokens=integer("max_total_tokens"),
            max_elapsed_seconds=real("max_elapsed_seconds"),
            max_retries_per_call=integer("max_retries_per_call"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderCallAdmissionV1:
    sequence: int
    estimated_tokens: int
    admitted_at: str
    remaining_calls: int | None
    remaining_tokens: int | None


@dataclass(frozen=True)
class ProviderCallReceiptV1:
    artifact_type: str
    artifact_version: str
    receipt_id: str
    sequence: int
    job_id: str
    attempt_id: str
    stage_name: str
    route: str
    provider: str
    model: str
    endpoint: str
    prompt_hash: str
    input_hash: str
    config_hash: str
    schema_hash: str
    status: ProviderCallStatus
    error_kind: str | None
    http_status: int | None
    provider_code: str | None
    attempts: int
    retry_after_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    response_hash: str | None
    started_at: str
    finished_at: str
    budget: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    node_id: str = ""
    call_id: str = ""
    endpoint_type: str = ""
    estimated_input_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str = ""
    incomplete_reason: str = ""
    fallback_or_payload_mutations: tuple[str, ...] = ()
    first_token_at: str = ""
    first_token_latency_ms: float | None = None
    total_latency_ms: float | None = None
    timeout_kind: str = ""
    usage_status: str = "unreported"

    def __post_init__(self) -> None:
        if self.artifact_type != PROVIDER_RECEIPT_ARTIFACT_TYPE:
            raise ProviderRuntimeContractError(f"unsupported receipt artifact_type: {self.artifact_type}")
        if self.artifact_version != PROVIDER_RECEIPT_ARTIFACT_VERSION:
            raise ProviderRuntimeContractError(f"unsupported receipt artifact_version: {self.artifact_version}")
        if not self.receipt_id.strip() or self.sequence < 1:
            raise ProviderRuntimeContractError("receipt_id and positive sequence are required")
        if self.status not in _CALL_STATUSES:
            raise ProviderRuntimeContractError(f"unsupported provider call status: {self.status}")
        if self.error_kind is not None and self.error_kind not in _ERROR_KINDS:
            raise ProviderRuntimeContractError(f"unsupported provider error kind: {self.error_kind}")
        if self.status == "success" and self.error_kind is not None:
            raise ProviderRuntimeContractError("successful provider calls cannot carry an error kind")
        if self.status != "success" and self.error_kind is None:
            raise ProviderRuntimeContractError("failed or blocked calls require an error kind")
        if self.attempts < 1:
            raise ProviderRuntimeContractError("provider attempts must be positive")
        for name in ("prompt_hash", "input_hash", "config_hash", "schema_hash"):
            value = str(getattr(self, name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ProviderRuntimeContractError(f"{name} must be a lowercase SHA-256 hash")
        if self.response_hash is not None and len(self.response_hash) != 64:
            raise ProviderRuntimeContractError("response_hash must be a SHA-256 hash when present")
        if self.http_status is not None and self.http_status < 100:
            raise ProviderRuntimeContractError("http_status is invalid")
        if self.input_tokens is not None and self.input_tokens < 0:
            raise ProviderRuntimeContractError("input_tokens cannot be negative")
        if self.output_tokens is not None and self.output_tokens < 0:
            raise ProviderRuntimeContractError("output_tokens cannot be negative")
        if self.total_tokens is not None and self.total_tokens < 0:
            raise ProviderRuntimeContractError("total_tokens cannot be negative")
        if not self.started_at or not self.finished_at:
            raise ProviderRuntimeContractError("receipt timestamps are required")
        for name in (
            "estimated_input_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if value is not None and int(value) < 0:
                raise ProviderRuntimeContractError(f"{name} cannot be negative")
        object.__setattr__(self, "budget", dict(self.budget))
        object.__setattr__(self, "metadata", _redact_mapping(dict(self.metadata)))
        object.__setattr__(self, "fallback_or_payload_mutations", tuple(str(item) for item in self.fallback_or_payload_mutations))

    @classmethod
    def from_result(
        cls,
        *,
        admission: ProviderCallAdmissionV1,
        job_id: str,
        attempt_id: str,
        stage_name: str,
        route: str,
        provider: str,
        model: str,
        endpoint: str,
        prompt_hash: str,
        input_hash: str,
        config_hash: str,
        schema_hash: str,
        result: Mapping[str, Any],
        budget: ProviderBudgetV1,
        started_at: str,
        finished_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        node_id: str = "",
        call_id: str = "",
    ) -> "ProviderCallReceiptV1":
        status = "success" if result.get("status") == "success" else "failed"
        candidate_error_kind = str(result.get("error_kind") or "invalid_response")
        error_kind = None if status == "success" else (
            candidate_error_kind if candidate_error_kind in _ERROR_KINDS else "invalid_response"
        )
        response = result.get("content")
        response_hash = hash_json(response) if status == "success" and response is not None else None
        retry_after = result.get("retry_after_seconds")
        try:
            retry_after_value = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after_value = None
        return cls(
            artifact_type=PROVIDER_RECEIPT_ARTIFACT_TYPE,
            artifact_version=PROVIDER_RECEIPT_ARTIFACT_VERSION,
            receipt_id=f"provider-receipt-{uuid.uuid4().hex}",
            sequence=admission.sequence,
            job_id=str(job_id or "unbound"),
            attempt_id=str(attempt_id or "unbound"),
            stage_name=str(stage_name or "unbound"),
            route=str(route or ""),
            provider=str(provider or ""),
            model=str(model or ""),
            endpoint=str(endpoint or ""),
            prompt_hash=prompt_hash,
            input_hash=input_hash,
            config_hash=config_hash,
            schema_hash=schema_hash,
            status=status,  # type: ignore[arg-type]
            error_kind=error_kind,
            http_status=_optional_http_status(result.get("http_status")),
            provider_code=str(result.get("provider_code") or "") or None,
            attempts=max(1, int(result.get("attempts") or 1)),
            retry_after_seconds=retry_after_value,
            input_tokens=_optional_nonnegative_int(result.get("input_tokens")),
            output_tokens=_optional_nonnegative_int(result.get("output_tokens")),
            total_tokens=_optional_nonnegative_int(result.get("total_tokens")),
            response_hash=response_hash,
            started_at=started_at,
            finished_at=finished_at or utc_now_iso(),
            budget=budget.to_dict(),
            metadata=metadata or {},
            node_id=node_id,
            call_id=call_id or f"call-{admission.sequence}",
            endpoint_type=str(result.get("endpoint_type") or ""),
            estimated_input_tokens=admission.estimated_tokens,
            cached_input_tokens=_optional_nonnegative_int(result.get("cached_input_tokens")),
            reasoning_tokens=_optional_nonnegative_int(result.get("reasoning_tokens")),
            finish_reason=str(result.get("finish_reason") or ""),
            incomplete_reason=str(result.get("incomplete_reason") or ""),
            fallback_or_payload_mutations=tuple(str(item) for item in result.get("fallback_or_payload_mutations") or ()),
            first_token_at=str(result.get("first_token_at") or ""),
            first_token_latency_ms=_optional_float(result.get("first_token_latency_ms")),
            total_latency_ms=_optional_float(result.get("total_latency_ms")),
            timeout_kind=str(result.get("timeout_kind") or ""),
            usage_status=str(result.get("usage_status") or ("reported" if result.get("input_tokens") is not None or result.get("output_tokens") is not None else "unreported")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["budget"] = dict(self.budget)
        payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProviderCallReceiptV1":
        raw_budget = payload.get("budget")
        budget = raw_budget if isinstance(raw_budget, Mapping) else {}
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        return cls(
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            receipt_id=str(payload.get("receipt_id") or ""),
            sequence=int(payload.get("sequence") or 0),
            job_id=str(payload.get("job_id") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            stage_name=str(payload.get("stage_name") or ""),
            route=str(payload.get("route") or ""),
            provider=str(payload.get("provider") or ""),
            model=str(payload.get("model") or ""),
            endpoint=str(payload.get("endpoint") or ""),
            prompt_hash=str(payload.get("prompt_hash") or ""),
            input_hash=str(payload.get("input_hash") or ""),
            config_hash=str(payload.get("config_hash") or ""),
            schema_hash=str(payload.get("schema_hash") or ""),
            status=str(payload.get("status") or "") if payload.get("status") else "failed",  # type: ignore[arg-type]
            error_kind=str(payload.get("error_kind") or "") or None,
            http_status=int(payload["http_status"]) if payload.get("http_status") is not None else None,
            provider_code=str(payload.get("provider_code") or "") or None,
            attempts=int(payload.get("attempts") or 0),
            retry_after_seconds=(
                float(payload["retry_after_seconds"])
                if payload.get("retry_after_seconds") is not None
                else None
            ),
            input_tokens=_optional_nonnegative_int(payload.get("input_tokens")),
            output_tokens=_optional_nonnegative_int(payload.get("output_tokens")),
            total_tokens=_optional_nonnegative_int(payload.get("total_tokens")),
            response_hash=str(payload.get("response_hash") or "") or None,
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            budget=budget,
            metadata=metadata,
            node_id=str(payload.get("node_id") or ""),
            call_id=str(payload.get("call_id") or ""),
            endpoint_type=str(payload.get("endpoint_type") or ""),
            estimated_input_tokens=_optional_nonnegative_int(payload.get("estimated_input_tokens")),
            cached_input_tokens=_optional_nonnegative_int(payload.get("cached_input_tokens")),
            reasoning_tokens=_optional_nonnegative_int(payload.get("reasoning_tokens")),
            finish_reason=str(payload.get("finish_reason") or ""),
            incomplete_reason=str(payload.get("incomplete_reason") or ""),
            fallback_or_payload_mutations=tuple(str(item) for item in payload.get("fallback_or_payload_mutations") or ()),
            first_token_at=str(payload.get("first_token_at") or ""),
            first_token_latency_ms=_optional_float(payload.get("first_token_latency_ms")),
            total_latency_ms=_optional_float(payload.get("total_latency_ms")),
            timeout_kind=str(payload.get("timeout_kind") or ""),
            usage_status=str(payload.get("usage_status") or "unreported"),
        )


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_http_status(value: Any) -> int | None:
    """Normalize transport metadata without trusting mock or foreign values."""

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 100 else None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class ProviderRuntimeLedger:
    """Append-only JSONL receipt store with duplicate-ID conflict detection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = _ledger_lock(self.path)

    def _read_unlocked(self) -> list[ProviderCallReceiptV1]:
        if not self.path.exists():
            return []
        receipts: list[ProviderCallReceiptV1] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderRuntimeContractError(
                    f"provider receipt ledger line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ProviderRuntimeContractError(f"provider receipt ledger line {line_number} is not an object")
            receipts.append(ProviderCallReceiptV1.from_dict(payload))
        return receipts

    def append(self, receipt: ProviderCallReceiptV1) -> ProviderCallReceiptV1:
        payload = receipt.to_dict()
        encoded = _canonical_json(payload)
        with self._lock:
            existing = self._read_unlocked()
            for candidate in existing:
                if candidate.receipt_id != receipt.receipt_id:
                    continue
                if _canonical_json(candidate.to_dict()) != encoded:
                    raise ProviderReceiptConflict(f"receipt ID reused with different content: {receipt.receipt_id}")
                return candidate
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
                handle.flush()
        return receipt

    def list_receipts(self) -> tuple[ProviderCallReceiptV1, ...]:
        with self._lock:
            return tuple(self._read_unlocked())


class ProviderRuntime:
    """Admission controller and receipt producer for one job/attempt/stage."""

    def __init__(
        self,
        *,
        budget: ProviderBudgetV1 | None = None,
        ledger: ProviderRuntimeLedger | None = None,
        job_id: str = "",
        attempt_id: str = "",
        stage_name: str = "",
        route: str = "",
        schema_hash: str | None = None,
        node_id: str = "",
    ) -> None:
        self.budget = budget or ProviderBudgetV1()
        self.ledger = ledger
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.stage_name = stage_name
        self.route = route
        self.node_id = node_id
        self.schema_hash = schema_hash or hash_text("provider-runtime-default-schema-v1")
        self.started_monotonic = time.monotonic()
        self.started_at = utc_now_iso()
        self._lock = threading.RLock()
        self._calls = 0
        self._reserved_tokens = 0
        self._receipts: list[ProviderCallReceiptV1] = []

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def reserved_tokens(self) -> int:
        return self._reserved_tokens

    @property
    def receipts(self) -> tuple[ProviderCallReceiptV1, ...]:
        return tuple(self._receipts)

    def max_attempts_for_call(self, requested_attempts: int) -> int:
        """Return the transport loop limit imposed by this runtime.

        The caller-facing limit is a total-attempt limit.  The formal runtime
        budget is expressed as retries, so one initial attempt is added when
        the retry dimension is bounded.  A zero budget means the caller's
        requested limit remains in force.
        """

        requested = max(1, int(requested_attempts))
        if not self.budget.max_retries_per_call:
            return requested
        return min(requested, self.budget.max_retries_per_call + 1)

    def admit(self, *, estimated_tokens: int = 0) -> ProviderCallAdmissionV1:
        estimated = max(0, int(estimated_tokens))
        with self._lock:
            elapsed = time.monotonic() - self.started_monotonic
            if self.budget.max_elapsed_seconds and elapsed >= self.budget.max_elapsed_seconds:
                raise ProviderBudgetExceeded("provider runtime elapsed-time budget exhausted")
            if self.budget.max_calls and self._calls >= self.budget.max_calls:
                raise ProviderBudgetExceeded("provider runtime call budget exhausted")
            if self.budget.max_total_tokens and self._reserved_tokens + estimated > self.budget.max_total_tokens:
                raise ProviderBudgetExceeded("provider runtime token budget exhausted")
            self._calls += 1
            self._reserved_tokens += estimated
            return ProviderCallAdmissionV1(
                sequence=self._calls,
                estimated_tokens=estimated,
                admitted_at=utc_now_iso(),
                remaining_calls=(self.budget.max_calls - self._calls) if self.budget.max_calls else None,
                remaining_tokens=(self.budget.max_total_tokens - self._reserved_tokens)
                if self.budget.max_total_tokens
                else None,
            )

    def complete(
        self,
        *,
        admission: ProviderCallAdmissionV1,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        result: Mapping[str, Any],
        schema_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderCallReceiptV1:
        provider = str(api_config.get("provider_family") or api_config.get("provider") or "generic")
        model = str(api_config.get("model") or "")
        endpoint = str(api_config.get("api_base") or "")
        config_hash = hash_json(_redact_mapping(api_config))
        receipt = ProviderCallReceiptV1.from_result(
            admission=admission,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            stage_name=self.stage_name,
            route=self.route or provider,
            provider=provider,
            model=model,
            endpoint=endpoint,
            prompt_hash=hash_text(prompt),
            input_hash=hash_json(input_payload),
            config_hash=config_hash,
            schema_hash=schema_hash or self.schema_hash,
            result=result,
            budget=self.budget,
            started_at=admission.admitted_at,
            metadata=metadata,
            node_id=self.node_id,
            call_id=str((metadata or {}).get("call_id") or ""),
        )
        with self._lock:
            if self.ledger is not None:
                self.ledger.append(receipt)
            self._receipts.append(receipt)
        return receipt

    def blocked_receipt(
        self,
        *,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        error_kind: ProviderErrorKind = "budget_exhausted",
        message: str = "provider runtime admission rejected",
        schema_hash: str | None = None,
    ) -> ProviderCallReceiptV1:
        with self._lock:
            self._calls += 1
            admission = ProviderCallAdmissionV1(
                sequence=self._calls,
                estimated_tokens=0,
                admitted_at=utc_now_iso(),
                remaining_calls=(self.budget.max_calls - self._calls) if self.budget.max_calls else None,
                remaining_tokens=self.budget.max_total_tokens - self._reserved_tokens
                if self.budget.max_total_tokens
                else None,
            )
        receipt = self.complete(
            admission=admission,
            prompt=prompt,
            input_payload=input_payload,
            api_config=api_config,
            result={"status": "failed", "error_kind": error_kind, "message": _redact_text(message)},
            schema_hash=schema_hash,
        )
        return receipt


__all__ = [
    "ProviderBudgetExceeded",
    "ProviderBudgetV1",
    "ProviderCallAdmissionV1",
    "ProviderCallReceiptV1",
    "ProviderErrorKind",
    "ProviderReceiptConflict",
    "ProviderRuntime",
    "ProviderRuntimeContractError",
    "ProviderRuntimeLedger",
    "hash_json",
    "hash_text",
    "stable_provider_hash",
]

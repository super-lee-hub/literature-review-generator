from __future__ import annotations

"""Provider-call budgets, redacted receipts, and fail-closed error taxonomy.

This module is deliberately transport-neutral.  The HTTP adapter performs the
request, while this runtime owns the retry ceiling and records the durable
facts needed to audit that decision.  Secrets and raw prompts never belong in
a receipt.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Iterable, Literal, Mapping, cast
import uuid

from services.durable_io import atomic_replace_with_retry, interprocess_file_lock
from services.job_workspace import atomic_write_json, utc_now_iso


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
_NON_SECRET_TOKEN_KEYS = frozenset(
    {
        "max_output_tokens",
        "max_context_tokens",
        "max_total_tokens",
        "estimated_input_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "requested_output_tokens",
        "stage1_output_budget_tokens",
        "stage1_requested_output_budgets",
        "initial_output_tokens",
        "ceiling_output_tokens",
        "reasoning_reserve_tokens",
        "reasoning_reserve",
        "safety_margin_tokens",
        "safety_margin",
        "max_retries_per_call",
    }
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


ProcessLiveness = Literal["alive", "dead", "unknown"]


@dataclass(frozen=True)
class ProcessIdentityV1:
    """Durable identity for a process owner used by recovery probes."""

    pid: int
    creation_time: float | None = None
    host_id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or int(self.pid) < 0:
            raise ProviderRuntimeContractError("process identity pid must be non-negative")
        object.__setattr__(self, "pid", int(self.pid))
        if self.creation_time is not None:
            if (
                isinstance(self.creation_time, bool)
                or not math.isfinite(float(self.creation_time))
                or float(self.creation_time) < 0
            ):
                raise ProviderRuntimeContractError(
                    "process identity creation_time must be a finite non-negative number"
                )
            object.__setattr__(self, "creation_time", float(self.creation_time))
        object.__setattr__(self, "host_id", str(self.host_id or "").strip().casefold())


def _local_host_id() -> str:
    try:
        return socket.gethostname().strip().casefold()
    except OSError:
        return ""


def _windows_filetime_seconds(filetime: Any) -> float:
    ticks = (int(filetime.dwHighDateTime) << 32) | int(filetime.dwLowDateTime)
    return ticks / 10_000_000.0


def _windows_process_creation_time(pid: int) -> float | None:
    if pid <= 0:
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return _windows_filetime_seconds(creation)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _posix_process_creation_time(pid: int) -> float | None:
    stat_path = Path(f"/proc/{pid}/stat")
    boot_path = Path("/proc/stat")
    try:
        raw_stat = stat_path.read_text(encoding="utf-8")
        closing_comm = raw_stat.rfind(")")
        if closing_comm < 0:
            return None
        fields = raw_stat[closing_comm + 2 :].split()
        start_ticks = int(fields[19])
        boot_epoch = None
        for line in boot_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot_epoch = float(line.split()[1])
                break
        if boot_epoch is None:
            return None
        sysconf = getattr(os, "sysconf", None)
        if not callable(sysconf):
            return None
        clock_ticks = int(cast(Any, sysconf)("SC_CLK_TCK"))
        if clock_ticks <= 0:
            return None
        return boot_epoch + (start_ticks / clock_ticks)
    except (OSError, IndexError, TypeError, ValueError):
        return None


def process_identity_for_pid(pid: int) -> ProcessIdentityV1:
    """Capture a PID plus the platform process-creation identity."""

    normalized_pid = int(pid)
    if normalized_pid <= 0:
        return ProcessIdentityV1(pid=0, host_id=_local_host_id())
    creation_time = (
        _windows_process_creation_time(normalized_pid)
        if os.name == "nt"
        else _posix_process_creation_time(normalized_pid)
    )
    return ProcessIdentityV1(
        pid=normalized_pid,
        creation_time=creation_time,
        host_id=_local_host_id(),
    )


def _windows_process_liveness(identity: ProcessIdentityV1) -> ProcessLiveness:
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, identity.pid)
        if not handle:
            error = ctypes.get_last_error()
            return "dead" if error in {6, 87} else "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown"
            if int(exit_code.value) != 259:  # STILL_ACTIVE
                return "dead"
            if identity.creation_time is None:
                return "alive"
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return "unknown"
            actual_creation = _windows_filetime_seconds(creation)
            return (
                "alive"
                if abs(actual_creation - float(identity.creation_time)) <= 0.01
                else "dead"
            )
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"


def process_liveness(identity: ProcessIdentityV1) -> ProcessLiveness:
    """Probe a process without sending it a signal.

    ``unknown`` is deliberately conservative: callers deciding whether to
    release a durable reservation must treat it as still owned.
    """

    if identity.pid <= 0:
        return "dead"
    if identity.host_id and identity.host_id != _local_host_id():
        return "unknown"
    if os.name == "nt":
        return _windows_process_liveness(identity)
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    if identity.creation_time is not None:
        actual_creation = _posix_process_creation_time(identity.pid)
        if actual_creation is None:
            return "unknown"
        if abs(actual_creation - float(identity.creation_time)) > 0.01:
            return "dead"
    return "alive"


def is_process_alive(identity: ProcessIdentityV1) -> bool:
    """Return a conservative boolean process-liveness result.

    Access-denied and cross-host probes return ``True`` so recovery cannot
    release a reservation without proof that its owner is gone.
    """

    return process_liveness(identity) != "dead"


@dataclass(frozen=True)
class ProviderAggregateBudgetV1:
    """One hard budget shared by every provider route in a process.

    The limits are reservation limits, not post-hoc report fields.  A call
    reserves its possible transport attempts and requested output allowance
    before the transport starts, so concurrent stage runtimes cannot overshoot
    an acceptance budget.
    """

    max_provider_calls_total: int = 0
    max_output_tokens_total: int = 0
    max_retry_attempts_total: int = 0
    max_wall_seconds: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls_total",
            "max_output_tokens_total",
            "max_retry_attempts_total",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise ProviderRuntimeContractError(f"{name} must be a non-negative integer")
            object.__setattr__(self, name, int(value))
        if (
            isinstance(self.max_wall_seconds, bool)
            or not math.isfinite(float(self.max_wall_seconds))
            or float(self.max_wall_seconds) < 0
        ):
            raise ProviderRuntimeContractError("max_wall_seconds must be non-negative")
        object.__setattr__(self, "max_wall_seconds", float(self.max_wall_seconds))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ProviderAggregateBudgetV1":
        source = value or {}
        aliases = {
            "max_provider_calls_total": ("max_provider_calls_total", "max_provider_calls"),
            "max_output_tokens_total": ("max_output_tokens_total", "max_output_tokens"),
            "max_retry_attempts_total": ("max_retry_attempts_total", "max_retry_attempts"),
            "max_wall_seconds": ("max_wall_seconds", "timeout_seconds"),
        }

        def raw_for(name: str) -> Any:
            for key in aliases[name]:
                if key in source:
                    return source[key]
            return 0

        def integer(name: str) -> int:
            raw = raw_for(name)
            if isinstance(raw, bool):
                raise ProviderRuntimeContractError(f"{name} must be an integer")
            try:
                parsed = int(str(raw).strip())
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(f"{name} must be an integer") from exc
            if parsed < 0:
                raise ProviderRuntimeContractError(f"{name} must be non-negative")
            return parsed

        raw_wall = raw_for("max_wall_seconds")
        try:
            wall = float(str(raw_wall).strip())
        except (TypeError, ValueError) as exc:
            raise ProviderRuntimeContractError("max_wall_seconds must be a number") from exc
        return cls(
            max_provider_calls_total=integer("max_provider_calls_total"),
            max_output_tokens_total=integer("max_output_tokens_total"),
            max_retry_attempts_total=integer("max_retry_attempts_total"),
            max_wall_seconds=wall,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AcceptanceExecutionContextV1:
    """Run-scoped acceptance authority propagated to provider runtimes."""

    acceptance_run_id: str
    final_executable_sha: str
    absolute_deadline_epoch: float
    provider_budget: ProviderAggregateBudgetV1
    provider_budget_state_path: str
    evidence_root: str
    process_event_log: str
    scenario_state_path: str
    owner_authorized: bool

    def __post_init__(self) -> None:
        if not str(self.acceptance_run_id).strip():
            raise ProviderRuntimeContractError("acceptance execution context run ID is required")
        if not str(self.final_executable_sha).strip():
            raise ProviderRuntimeContractError("acceptance execution context final SHA is required")
        if (
            isinstance(self.absolute_deadline_epoch, bool)
            or not math.isfinite(float(self.absolute_deadline_epoch))
            or float(self.absolute_deadline_epoch) < 0
        ):
            raise ProviderRuntimeContractError(
                "acceptance execution context deadline must be a finite non-negative number"
            )
        for name in (
            "provider_budget_state_path",
            "evidence_root",
            "process_event_log",
            "scenario_state_path",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ProviderRuntimeContractError(
                    f"acceptance execution context {name} is required"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance_run_id": self.acceptance_run_id,
            "final_executable_sha": self.final_executable_sha,
            "absolute_deadline_epoch": self.absolute_deadline_epoch,
            "provider_budget": self.provider_budget.to_dict(),
            "provider_budget_state_path": self.provider_budget_state_path,
            "evidence_root": self.evidence_root,
            "process_event_log": self.process_event_log,
            "scenario_state_path": self.scenario_state_path,
            "owner_authorized": self.owner_authorized,
        }


@dataclass(frozen=True)
class ProviderAggregateReservationV1:
    reservation_id: str
    provider_calls: int
    output_tokens: int
    retry_attempts: int
    admitted_at: str
    owner_id: str = ""
    owner_pid: int = 0
    transport_started: bool = False
    context: Mapping[str, Any] = field(default_factory=dict)
    owner_process_creation_time: float | None = None
    owner_host_id: str = ""


_ACTIVE_ACCEPTANCE_CONTEXT: ContextVar[AcceptanceExecutionContextV1 | None] = ContextVar(
    "active_acceptance_execution_context",
    default=None,
)
_ACTIVE_ACCEPTANCE_CONTROLLER: ContextVar["ProviderBudgetController | None"] = ContextVar(
    "active_acceptance_budget_controller",
    default=None,
)


def current_acceptance_execution_context() -> AcceptanceExecutionContextV1 | None:
    return _ACTIVE_ACCEPTANCE_CONTEXT.get()


@contextmanager
def bind_acceptance_execution_context(
    context: AcceptanceExecutionContextV1,
    controller: "ProviderBudgetController",
):
    """Bind typed acceptance authority and a child-process environment bridge."""

    if controller.budget != context.provider_budget:
        raise ProviderRuntimeContractError(
            "acceptance execution context budget does not match its controller"
        )
    context_token = _ACTIVE_ACCEPTANCE_CONTEXT.set(context)
    controller_token = _ACTIVE_ACCEPTANCE_CONTROLLER.set(controller)
    environment_keys = (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
    )
    previous = {key: os.environ.get(key) for key in environment_keys}
    os.environ["AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON"] = json.dumps(
        context.provider_budget.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )
    os.environ["AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH"] = (
        context.provider_budget_state_path
    )
    os.environ["AUTO_GENERATE_ACCEPTANCE_RUN_ID"] = context.acceptance_run_id
    os.environ["AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON"] = json.dumps(
        context.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        yield context
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _ACTIVE_ACCEPTANCE_CONTROLLER.reset(controller_token)
        _ACTIVE_ACCEPTANCE_CONTEXT.reset(context_token)


class ProviderBudgetController:
    """Thread-safe aggregate reservation and durable-receipt accounting."""

    def __init__(
        self,
        budget: ProviderAggregateBudgetV1,
        *,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.budget = budget
        self._monotonic = monotonic
        self._started_monotonic = float(monotonic())
        self._first_started_epoch = float(time.time())
        self._absolute_deadline_epoch = (
            self._first_started_epoch + float(budget.max_wall_seconds)
            if budget.max_wall_seconds
            else 0.0
        )
        self._owner_id = uuid.uuid4().hex
        self._process_identity = process_identity_for_pid(os.getpid())
        self._lock = threading.RLock()
        self._reservations: dict[str, ProviderAggregateReservationV1] = {}
        self._calls_used = 0
        self._output_tokens_used = 0
        self._retry_attempts_used = 0
        self._calls_reserved = 0
        self._output_tokens_reserved = 0
        self._retry_attempts_reserved = 0
        self._state_path: Path | None = None
        self._ambiguous_reservation_ids: set[str] = set()

    def bind_state_path(self, path: str | Path) -> None:
        """Bind aggregate usage to a job-owned durable state file."""

        target = Path(path).expanduser().resolve()
        with self._lock:
            if self._state_path is not None and self._state_path != target:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget cannot be rebound to a different state path"
                )
            self._state_path = target
            with interprocess_file_lock(target):
                if not target.is_file():
                    self._persist_state_unlocked()
                    return
                self._load_state_unlocked()
                self._reconcile_dead_reservations_unlocked()
                self._persist_state_unlocked()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        return is_process_alive(process_identity_for_pid(pid))

    def _load_state_unlocked(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderRuntimeContractError(
                "provider aggregate budget state is unreadable"
            ) from exc
        state_schema = payload.get("schema_version") if isinstance(payload, Mapping) else None
        if not isinstance(payload, Mapping) or state_schema not in {
            "provider-aggregate-budget-v1",
            "provider-aggregate-budget-v2",
            "provider-aggregate-budget-v3",
        }:
            raise ProviderRuntimeContractError("provider aggregate budget state schema is invalid")
        if (
            state_schema == "provider-aggregate-budget-v1"
            and self.budget.max_wall_seconds
            and payload.get("absolute_deadline_epoch") is None
        ):
            raise ProviderRuntimeContractError(
                "legacy provider aggregate budget state has no durable wall-clock deadline"
            )
        if payload.get("budget") != self.budget.to_dict():
            raise ProviderRuntimeContractError("provider aggregate budget state limits changed")
        for field_name, attribute in (
            ("calls_used", "_calls_used"),
            ("output_tokens_used", "_output_tokens_used"),
            ("retry_attempts_used", "_retry_attempts_used"),
        ):
            raw = payload.get(field_name, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ProviderRuntimeContractError(
                    f"provider aggregate budget state field is invalid: {field_name}"
                )
            setattr(self, attribute, raw)
        raw_started = payload.get("first_started_epoch")
        raw_deadline = payload.get("absolute_deadline_epoch")
        if raw_started is not None:
            try:
                self._first_started_epoch = float(raw_started)
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget first_started_epoch is invalid"
                ) from exc
        if raw_deadline is not None:
            try:
                self._absolute_deadline_epoch = float(raw_deadline)
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget absolute_deadline_epoch is invalid"
                ) from exc
        reservations: dict[str, ProviderAggregateReservationV1] = {}
        raw_reservations = payload.get("reservations")
        if isinstance(raw_reservations, list):
            for raw in raw_reservations:
                if not isinstance(raw, Mapping):
                    raise ProviderRuntimeContractError("provider aggregate reservation is invalid")
                reservation_id = str(raw.get("reservation_id") or "").strip()
                if not reservation_id:
                    raise ProviderRuntimeContractError("provider aggregate reservation ID is missing")
                raw_creation_time = raw.get("owner_process_creation_time")
                try:
                    reservation = ProviderAggregateReservationV1(
                        reservation_id=reservation_id,
                        provider_calls=int(raw.get("provider_calls") or 0),
                        output_tokens=int(raw.get("output_tokens") or 0),
                        retry_attempts=int(raw.get("retry_attempts") or 0),
                        admitted_at=str(raw.get("admitted_at") or ""),
                        owner_id=str(raw.get("owner_id") or ""),
                        owner_pid=int(raw.get("owner_pid") or 0),
                        owner_process_creation_time=(
                            None
                            if raw_creation_time in (None, "")
                            else float(str(raw_creation_time))
                        ),
                        owner_host_id=str(raw.get("owner_host_id") or ""),
                        transport_started=bool(raw.get("transport_started", False)),
                        context=dict(raw.get("context") or {}),
                    )
                except (TypeError, ValueError) as exc:
                    raise ProviderRuntimeContractError(
                        "provider aggregate reservation fields are invalid"
                    ) from exc
                if (
                    reservation.provider_calls < 0
                    or reservation.output_tokens < 0
                    or reservation.retry_attempts < 0
                    or not reservation.admitted_at
                ):
                    raise ProviderRuntimeContractError("provider aggregate reservation values are invalid")
                reservations[reservation.reservation_id] = reservation
        else:
            reserved = {
                name: payload.get(name, 0)
                for name in (
                    "calls_reserved",
                    "output_tokens_reserved",
                    "retry_attempts_reserved",
                )
            }
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in reserved.values()
            ):
                raise ProviderRuntimeContractError("provider aggregate budget reserved state is invalid")
            if any(reserved.values()):
                raise ProviderBudgetExceeded(
                    "provider aggregate budget has legacy unreconciled reservations"
                )
        self._reservations = reservations
        self._ambiguous_reservation_ids = set()
        self._recompute_reserved_unlocked()
        stored_reserved = (
            payload.get("calls_reserved", self._calls_reserved),
            payload.get("output_tokens_reserved", self._output_tokens_reserved),
            payload.get("retry_attempts_reserved", self._retry_attempts_reserved),
        )
        if stored_reserved != (
            self._calls_reserved,
            self._output_tokens_reserved,
            self._retry_attempts_reserved,
        ):
            raise ProviderRuntimeContractError(
                "provider aggregate budget reserved totals do not match reservation records"
            )
        for limit, used, reserved, label in (
            (
                self.budget.max_provider_calls_total,
                self._calls_used,
                self._calls_reserved,
                "provider calls",
            ),
            (
                self.budget.max_output_tokens_total,
                self._output_tokens_used,
                self._output_tokens_reserved,
                "provider output tokens",
            ),
            (
                self.budget.max_retry_attempts_total,
                self._retry_attempts_used,
                self._retry_attempts_reserved,
                "provider retries",
            ),
        ):
            if limit and used + reserved > limit:
                raise ProviderRuntimeContractError(
                    f"provider aggregate budget state exceeds its {label} limit"
                )

    def _recompute_reserved_unlocked(self) -> None:
        self._calls_reserved = sum(item.provider_calls for item in self._reservations.values())
        self._output_tokens_reserved = sum(item.output_tokens for item in self._reservations.values())
        self._retry_attempts_reserved = sum(item.retry_attempts for item in self._reservations.values())

    @staticmethod
    def _reservation_process_identity(
        reservation: ProviderAggregateReservationV1,
    ) -> ProcessIdentityV1:
        return ProcessIdentityV1(
            pid=reservation.owner_pid,
            creation_time=reservation.owner_process_creation_time,
            host_id=reservation.owner_host_id,
        )

    @classmethod
    def _reservation_owner_liveness(
        cls,
        reservation: ProviderAggregateReservationV1,
    ) -> ProcessLiveness:
        return process_liveness(cls._reservation_process_identity(reservation))

    def _reconcile_dead_reservations_unlocked(self) -> None:
        """Release only reservations proven to have died before transport."""

        stale: list[str] = []
        for reservation_id, reservation in self._reservations.items():
            if self._reservation_owner_liveness(reservation) != "dead":
                continue
            if reservation.transport_started:
                self._ambiguous_reservation_ids.add(reservation_id)
                continue
            stale.append(reservation_id)
        for reservation_id in stale:
            self._reservations.pop(reservation_id, None)
        self._recompute_reserved_unlocked()

    def _persist_state_unlocked(self) -> None:
        if self._state_path is None:
            return
        atomic_write_json(
            str(self._state_path),
            {
                "schema_version": "provider-aggregate-budget-v3",
                "budget": self.budget.to_dict(),
                "calls_used": self._calls_used,
                "output_tokens_used": self._output_tokens_used,
                "retry_attempts_used": self._retry_attempts_used,
                "calls_reserved": self._calls_reserved,
                "output_tokens_reserved": self._output_tokens_reserved,
                "retry_attempts_reserved": self._retry_attempts_reserved,
                "first_started_epoch": self._first_started_epoch,
                "absolute_deadline_epoch": self._absolute_deadline_epoch,
                "owner_id": self._owner_id,
                "reservations": [
                    {
                        "reservation_id": reservation.reservation_id,
                        "provider_calls": reservation.provider_calls,
                        "output_tokens": reservation.output_tokens,
                        "retry_attempts": reservation.retry_attempts,
                        "admitted_at": reservation.admitted_at,
                        "owner_id": reservation.owner_id,
                        "owner_pid": reservation.owner_pid,
                        "owner_process_creation_time": reservation.owner_process_creation_time,
                        "owner_host_id": reservation.owner_host_id,
                        "transport_started": reservation.transport_started,
                        "context": dict(reservation.context),
                    }
                    for reservation in self._reservations.values()
                ],
            },
        )

    def _check_wall(self) -> None:
        if self.budget.max_wall_seconds and self._absolute_deadline_epoch:
            if float(time.time()) >= self._absolute_deadline_epoch:
                raise ProviderBudgetExceeded("aggregate provider wall-clock budget exhausted")
        elif self.budget.max_wall_seconds and (
            float(self._monotonic()) - self._started_monotonic
        ) >= self.budget.max_wall_seconds:
            raise ProviderBudgetExceeded("aggregate provider wall-clock budget exhausted")

    def admit(
        self,
        *,
        requested_output_tokens: int = 0,
        requested_retry_attempts: int = 0,
        context: Mapping[str, Any] | None = None,
    ) -> ProviderAggregateReservationV1:
        output_tokens = max(0, int(requested_output_tokens))
        retry_attempts = max(0, int(requested_retry_attempts))
        provider_calls = 1 + retry_attempts
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._admit_unlocked(
                    provider_calls=provider_calls,
                    output_tokens=output_tokens,
                    retry_attempts=retry_attempts,
                    context=context,
                )
            with lock:
                self._load_state_unlocked()
                self._reconcile_dead_reservations_unlocked()
                return self._admit_unlocked(
                    provider_calls=provider_calls,
                    output_tokens=output_tokens,
                    retry_attempts=retry_attempts,
                    context=context,
                )

    def _admit_unlocked(
        self,
        *,
        provider_calls: int,
        output_tokens: int,
        retry_attempts: int,
        context: Mapping[str, Any] | None,
    ) -> ProviderAggregateReservationV1:
        self._check_wall()
        if self._ambiguous_reservation_ids:
            raise ProviderBudgetExceeded(
                "provider aggregate budget has ambiguous reservations requiring durable receipt reconciliation"
            )
        if (
                self.budget.max_provider_calls_total
                and self._calls_used + self._calls_reserved + provider_calls
                > self.budget.max_provider_calls_total
        ):
            raise ProviderBudgetExceeded("aggregate provider call budget exhausted")
        if (
                self.budget.max_output_tokens_total
                and self._output_tokens_used + self._output_tokens_reserved + output_tokens
                > self.budget.max_output_tokens_total
        ):
            raise ProviderBudgetExceeded("aggregate provider output-token budget exhausted")
        if (
                self.budget.max_retry_attempts_total
                and self._retry_attempts_used + self._retry_attempts_reserved + retry_attempts
                > self.budget.max_retry_attempts_total
        ):
            raise ProviderBudgetExceeded("aggregate provider retry budget exhausted")
        reservation = ProviderAggregateReservationV1(
                reservation_id=f"{os.getpid()}-{uuid.uuid4().hex}",
                provider_calls=provider_calls,
                output_tokens=output_tokens,
                retry_attempts=retry_attempts,
                admitted_at=utc_now_iso(),
                owner_id=self._owner_id,
                owner_pid=self._process_identity.pid,
                owner_process_creation_time=self._process_identity.creation_time,
                owner_host_id=self._process_identity.host_id,
                context=dict(context or {}),
        )
        self._reservations[reservation.reservation_id] = reservation
        self._calls_reserved += provider_calls
        self._output_tokens_reserved += output_tokens
        self._retry_attempts_reserved += retry_attempts
        try:
            self._persist_state_unlocked()
        except BaseException:
            del self._reservations[reservation.reservation_id]
            self._calls_reserved -= provider_calls
            self._output_tokens_reserved -= output_tokens
            self._retry_attempts_reserved -= retry_attempts
            raise
        return reservation

    def mark_transport_started(self, reservation: ProviderAggregateReservationV1) -> None:
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                self._mark_transport_started_unlocked(reservation)
                return
            with lock:
                self._load_state_unlocked()
                self._mark_transport_started_unlocked(reservation)

    def _mark_transport_started_unlocked(self, reservation: ProviderAggregateReservationV1) -> None:
        current = self._reservations.get(reservation.reservation_id)
        if current is None:
            raise ProviderRuntimeContractError(
                f"unknown provider reservation: {reservation.reservation_id}"
            )
        self._reservations[reservation.reservation_id] = replace(
            current,
            transport_started=True,
        )
        self._persist_state_unlocked()

    def reconcile_orphaned_reservations(
        self,
        *,
        receipt_ledgers: Iterable[str | Path] = (),
    ) -> dict[str, Any]:
        """Reconcile dead reservations from durable provider receipts.

        A reservation with no transport-start marker is released after its
        owner process is gone. A marked reservation is released only when a
        receipt carrying that reservation ID is found; otherwise the state
        remains blocked and the caller receives an explicit ambiguity error.
        """

        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._reconcile_orphaned_unlocked(receipt_ledgers)
            with lock:
                self._load_state_unlocked()
                return self._reconcile_orphaned_unlocked(receipt_ledgers)

    def _reconcile_orphaned_unlocked(
        self,
        receipt_ledgers: Iterable[str | Path],
    ) -> dict[str, Any]:
        receipt_by_reservation: dict[str, Mapping[str, Any]] = {}
        for ledger_path in receipt_ledgers:
            try:
                receipts = ProviderRuntimeLedger(ledger_path).list_receipts()
            except (OSError, ValueError, ProviderRuntimeContractError):
                continue
            for receipt in receipts:
                metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
                reservation_id = str(metadata.get("aggregate_reservation_id") or "")
                if reservation_id:
                    receipt_by_reservation[reservation_id] = receipt.to_dict()
        released: list[str] = []
        recovered: list[str] = []
        ambiguous: list[str] = []
        for reservation_id, reservation in list(self._reservations.items()):
            if self._reservation_owner_liveness(reservation) != "dead":
                continue
            receipt = receipt_by_reservation.get(reservation_id)
            if receipt is not None:
                try:
                    attempts = max(1, int(receipt.get("attempts") or 1))
                except (TypeError, ValueError):
                    attempts = reservation.provider_calls
                output = receipt.get("output_tokens")
                try:
                    actual_output = (
                        reservation.output_tokens
                        if output in (None, "")
                        else int(output)
                    )
                except (TypeError, ValueError):
                    actual_output = reservation.output_tokens
                if attempts > reservation.provider_calls or actual_output > reservation.output_tokens:
                    raise ProviderRuntimeContractError(
                        "durable receipt exceeds its orphaned aggregate reservation"
                    )
                self._reservations.pop(reservation_id, None)
                self._calls_used += attempts
                self._output_tokens_used += actual_output
                self._retry_attempts_used += max(0, attempts - 1)
                self._ambiguous_reservation_ids.discard(reservation_id)
                recovered.append(reservation_id)
            elif reservation.transport_started:
                self._ambiguous_reservation_ids.add(reservation_id)
                ambiguous.append(reservation_id)
            else:
                self._reservations.pop(reservation_id, None)
                self._ambiguous_reservation_ids.discard(reservation_id)
                released.append(reservation_id)
        self._recompute_reserved_unlocked()
        self._persist_state_unlocked()
        if ambiguous:
            raise ProviderBudgetExceeded(
                "provider aggregate budget has ambiguous orphaned reservations: "
                + ", ".join(sorted(ambiguous))
            )
        return {"released": released, "recovered": recovered, "ambiguous": ambiguous}

    def complete(
        self,
        reservation: ProviderAggregateReservationV1,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._complete_unlocked(
                    reservation=reservation,
                    result=result,
                )
            with lock:
                self._load_state_unlocked()
                return self._complete_unlocked(
                    reservation=reservation,
                    result=result,
                )

    def _complete_unlocked(
        self,
        *,
        reservation: ProviderAggregateReservationV1,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self._reservations.get(reservation.reservation_id)
        if current is None:
                raise ProviderRuntimeContractError(
                    f"unknown or already completed provider reservation: {reservation.reservation_id}"
                )
        try:
            attempts = max(1, int(result.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = current.provider_calls
        if attempts > current.provider_calls:
                raise ProviderRuntimeContractError(
                    "provider transport attempts exceeded the pre-admitted aggregate reservation"
                )
        actual_retries = max(0, attempts - 1)
        raw_output = result.get("output_tokens")
        try:
            actual_output = int(raw_output) if raw_output is not None else current.output_tokens
        except (TypeError, ValueError):
            actual_output = current.output_tokens
        if actual_output < 0 or actual_output > current.output_tokens:
                raise ProviderRuntimeContractError(
                    "provider output tokens exceeded the pre-admitted aggregate reservation"
                )
        self._reservations.pop(reservation.reservation_id, None)
        self._calls_reserved -= current.provider_calls
        self._output_tokens_reserved -= current.output_tokens
        self._retry_attempts_reserved -= current.retry_attempts
        self._calls_used += attempts
        self._output_tokens_used += actual_output
        self._retry_attempts_used += actual_retries
        self._persist_state_unlocked()
        return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "calls_used": self._calls_used,
            "output_tokens_used": self._output_tokens_used,
            "retry_attempts_used": self._retry_attempts_used,
            "calls_reserved": self._calls_reserved,
            "output_tokens_reserved": self._output_tokens_reserved,
            "retry_attempts_reserved": self._retry_attempts_reserved,
            "elapsed_seconds": max(
                0.0,
                (
                    float(time.time()) - self._first_started_epoch
                    if self._absolute_deadline_epoch
                    else float(self._monotonic()) - self._started_monotonic
                ),
            ),
            "absolute_deadline_epoch": self._absolute_deadline_epoch,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._state_path is not None:
                with interprocess_file_lock(self._state_path):
                    self._load_state_unlocked()
            return self._snapshot_unlocked()


_ENV_BUDGET_LOCK = threading.Lock()
_ENV_BUDGET_RAW = ""
_ENV_BUDGET_CONTROLLER: ProviderBudgetController | None = None


def provider_budget_controller_from_environment() -> ProviderBudgetController | None:
    """Return one shared controller for the current acceptance subprocess."""

    global _ENV_BUDGET_RAW, _ENV_BUDGET_CONTROLLER
    active_controller = _ACTIVE_ACCEPTANCE_CONTROLLER.get()
    if active_controller is not None:
        return active_controller
    raw = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "")).strip()
    if not raw:
        with _ENV_BUDGET_LOCK:
            _ENV_BUDGET_RAW = ""
            _ENV_BUDGET_CONTROLLER = None
        return None
    state_path = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", "")).strip()
    cache_key = raw + "\x00" + state_path
    with _ENV_BUDGET_LOCK:
        if cache_key == _ENV_BUDGET_RAW and _ENV_BUDGET_CONTROLLER is not None:
            return _ENV_BUDGET_CONTROLLER
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderRuntimeContractError(
                "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderRuntimeContractError(
                "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON must be a JSON object"
            )
        controller = ProviderBudgetController(
            ProviderAggregateBudgetV1.from_mapping(payload)
        )
        if state_path:
            controller.bind_state_path(state_path)
        _ENV_BUDGET_RAW = cache_key
        _ENV_BUDGET_CONTROLLER = controller
        return controller


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


def _canonical_file_identity(path: str) -> dict[str, Any]:
    """Return path-independent, non-secret identity for a local input file."""

    normalized = str(path or "").strip()
    if not normalized:
        return {"exists": False, "bytes": 0, "sha256": ""}
    try:
        size = int(os.path.getsize(normalized))
    except OSError:
        return {"exists": False, "bytes": 0, "sha256": ""}
    digest = hashlib.sha256()
    try:
        with open(normalized, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return {"exists": False, "bytes": 0, "sha256": ""}
    return {"exists": True, "bytes": size, "sha256": digest.hexdigest()}


def _canonical_request_content(prompt: str, user_content: Any) -> Any:
    """Normalize logical content without persisting raw prompts or base64."""

    if not isinstance(user_content, (list, tuple)):
        return [{"type": "text", "text": str(prompt or "")}] if str(prompt or "") else []
    normalized: list[dict[str, Any]] = []
    has_text = False
    for raw in user_content:
        if not isinstance(raw, Mapping):
            continue
        item_type = str(raw.get("type") or "").strip().lower()
        if item_type in {"text", "input_text"}:
            value = str(raw.get("text") or "")
            if value:
                normalized.append({"type": "text", "text": value})
                has_text = True
            continue
        if item_type == "local_image_path":
            frozen_bytes = 0
            try:
                frozen_bytes = int(raw.get("frozen_image_bytes") or 0)
            except (TypeError, ValueError):
                frozen_bytes = 0
            frozen_hash = str(raw.get("frozen_image_sha256") or "").strip()
            if bool(raw.get("transport_frozen")) and frozen_bytes > 0 and frozen_hash:
                # The final preflight snapshot, not the mutable path, is the
                # request identity used by expected calls and receipts.
                identity = {
                    "exists": True,
                    "bytes": frozen_bytes,
                    "sha256": frozen_hash,
                }
            else:
                path = str(raw.get("path") or "").strip()
                identity = _canonical_file_identity(path)
            if not identity["exists"] or identity["bytes"] <= 0:
                continue
            normalized.append({
                "type": "image",
                "visual_id": str(raw.get("visual_id") or ""),
                "page_no": int(raw.get("page_no") or 0),
                "bbox": list(raw.get("bbox") or []),
                "artifact_type": str(raw.get("artifact_type") or ""),
                "detail": str(raw.get("detail") or "original"),
                "raw_reinspection_group_id": str(raw.get("raw_reinspection_group_id") or ""),
                "raw_reinspection_resolution": str(raw.get("raw_reinspection_resolution") or ""),
                "raw_reinspection_atomic": bool(raw.get("raw_reinspection_atomic")),
                "ambiguous_candidate_ids": [
                    str(item)
                    for item in (raw.get("ambiguous_candidate_ids") or [])
                    if str(item)
                ],
                "raw_reinspection_selected_ids": [
                    str(item)
                    for item in (raw.get("raw_reinspection_selected_ids") or [])
                    if str(item)
                ],
                "raw_reinspection_fallback_reason": str(
                    raw.get("raw_reinspection_fallback_reason") or ""
                ),
                **identity,
            })
            continue
        if item_type == "image_url":
            image_url = raw.get("image_url")
            if isinstance(image_url, Mapping):
                url = str(image_url.get("url") or "").strip()
                detail = str(image_url.get("detail") or raw.get("detail") or "original")
            else:
                url = str(image_url or "").strip()
                detail = str(raw.get("detail") or "original")
            if url:
                normalized.append({"type": "image_url", "url_sha256": hash_text(url), "detail": detail})
            continue
        if item_type == "local_pdf_path":
            identity = _canonical_file_identity(str(raw.get("path") or "").strip())
            if identity["exists"]:
                normalized.append({"type": "file", "filename": "document.pdf", **identity})
            continue
        if item_type in {"input_file", "file"}:
            file_identity = {
                str(key): str(raw.get(key) or "")
                for key in ("file_id", "file_url", "filename")
                if raw.get(key)
            }
            if raw.get("file_data"):
                file_identity["file_data_sha256"] = hash_text(str(raw.get("file_data")))
            if file_identity:
                normalized.append({"type": "file", **file_identity})
    if not has_text and prompt:
        normalized.insert(0, {"type": "text", "text": str(prompt)})
    return normalized


def canonical_provider_request_payload(
    *,
    prompt: str,
    system_prompt: str,
    user_content: Any,
    response_format: str,
    max_output_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Build the one request identity shared by expected and actual calls."""

    return {
        "identity_version": "provider_request_identity/v1",
        "system": str(system_prompt or ""),
        "user": str(prompt or ""),
        "user_content": _canonical_request_content(prompt, user_content),
        "response_format": str(response_format or ""),
        "max_output_tokens": int(max_output_tokens),
        "temperature": float(temperature),
    }


def provider_request_input_hash(**kwargs: Any) -> str:
    return hash_json(canonical_provider_request_payload(**kwargs))


def compute_closure_epoch_id(
    *,
    job_id: str,
    stage_name: str,
    logical_attempt_identity: str,
    expected_call_graph_hash: str,
    current_input_artifact_hashes: Mapping[str, str] | list[str] | tuple[str, ...] = (),
    provider_config_hash: str,
    schema_version: str,
) -> str:
    """Return the content-addressed identity of one provider closure epoch.

    Receipt ledgers are append-only, so an attempt must be identified by the
    immutable inputs which define its expected call graph.  The returned
    value deliberately contains no timestamps or random values: an exact
    replay of the same logical attempt resolves to the same epoch, while a
    retry with a new logical attempt identity gets a different epoch.
    """

    if isinstance(current_input_artifact_hashes, Mapping):
        input_hashes: Any = {
            str(key): str(value)
            for key, value in sorted(current_input_artifact_hashes.items(), key=lambda item: str(item[0]))
        }
    else:
        input_hashes = sorted(str(value) for value in current_input_artifact_hashes)
    payload = {
        "job_id": str(job_id),
        "stage_name": str(stage_name),
        "logical_attempt_identity": str(logical_attempt_identity),
        "expected_call_graph_hash": str(expected_call_graph_hash),
        "current_input_artifact_hashes": input_hashes,
        "provider_config_hash": str(provider_config_hash),
        "schema_version": str(schema_version),
    }
    return hashlib.sha256(
        f"auto-generate\x00provider-closure-epoch-v1\x00{_canonical_json(payload)}".encode("utf-8")
    ).hexdigest()


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
        if folded in _NON_SECRET_TOKEN_KEYS:
            if isinstance(raw_value, Mapping):
                result[key] = _redact_mapping(raw_value)
            elif isinstance(raw_value, (list, tuple)):
                result[key] = [
                    _redact_mapping(item) if isinstance(item, Mapping) else item
                    for item in raw_value
                ]
            else:
                result[key] = raw_value
        elif any(marker in folded for marker in _SECRET_KEY_MARKERS):
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
    estimated_output_tokens: int = 0
    reserved_call_attempts: int = 1
    reserved_retry_attempts: int = 0
    aggregate_reservation_id: str | None = None


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
    closure_epoch_id: str = ""
    logical_attempt_identity: str = ""
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
    test_only: bool = False
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""

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
        if not self.test_only:
            for name in (
                "job_id",
                "attempt_id",
                "stage_name",
                "node_id",
                "call_id",
                "provider",
                "model",
                "endpoint_type",
            ):
                if not str(getattr(self, name) or "").strip():
                    raise ProviderRuntimeContractError(f"bound provider receipt requires {name}")
        if self.attempts < 1:
            raise ProviderRuntimeContractError("provider attempts must be positive")
        for name in ("prompt_hash", "input_hash", "config_hash", "schema_hash"):
            value = str(getattr(self, name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ProviderRuntimeContractError(f"{name} must be a lowercase SHA-256 hash")
        if self.prompt_sha256 and (
            len(self.prompt_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.prompt_sha256)
        ):
            raise ProviderRuntimeContractError("prompt_sha256 must be a lowercase SHA-256 hash when present")
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

    def validate_acceptance_authority(
        self,
        *,
        expected_job_id: str = "",
        expected_attempt_id: str = "",
        expected_stage_name: str = "",
    ) -> None:
        """Require the complete identity needed for a live acceptance claim."""

        if self.test_only is not False:
            raise ProviderRuntimeContractError("test-only provider receipt cannot satisfy live acceptance")
        for name in (
            "receipt_id",
            "job_id",
            "attempt_id",
            "stage_name",
            "route",
            "provider",
            "model",
            "endpoint",
            "endpoint_type",
            "node_id",
            "call_id",
            "closure_epoch_id",
            "started_at",
            "finished_at",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ProviderRuntimeContractError(
                    f"acceptance provider receipt requires {name}"
                )
        for expected, actual, label in (
            (expected_job_id, self.job_id, "job_id"),
            (expected_attempt_id, self.attempt_id, "attempt_id"),
            (expected_stage_name, self.stage_name, "stage_name"),
        ):
            if expected and actual != expected:
                raise ProviderRuntimeContractError(
                    f"acceptance provider receipt {label} does not match its evidence owner"
                )
        if not isinstance(self.metadata, Mapping):
            raise ProviderRuntimeContractError("acceptance provider receipt metadata is invalid")
        transport_config = self.metadata.get("transport_config")
        if not isinstance(transport_config, Mapping):
            raise ProviderRuntimeContractError(
                "acceptance provider receipt lacks transport identity"
            )
        for name in ("provider_family", "endpoint_type", "api_base", "model"):
            if not str(transport_config.get(name) or "").strip():
                raise ProviderRuntimeContractError(
                    f"acceptance provider receipt transport identity requires {name}"
                )

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
        closure_epoch_id: str = "",
        logical_attempt_identity: str = "",
        endpoint_type: str = "",
        test_only: bool = False,
        prompt_id: str = "",
        prompt_version: str = "",
        prompt_sha256: str = "",
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
            closure_epoch_id=str(closure_epoch_id or ""),
            logical_attempt_identity=str(logical_attempt_identity or ""),
            endpoint_type=str(result.get("endpoint_type") or endpoint_type),
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
            test_only=test_only,
            prompt_id=str(prompt_id or ""),
            prompt_version=str(prompt_version or ""),
            prompt_sha256=str(prompt_sha256 or ""),
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
            closure_epoch_id=str(payload.get("closure_epoch_id") or ""),
            logical_attempt_identity=str(payload.get("logical_attempt_identity") or ""),
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
            test_only=bool(payload.get("test_only", False)),
            prompt_id=str(payload.get("prompt_id") or ""),
            prompt_version=str(payload.get("prompt_version") or ""),
            prompt_sha256=str(payload.get("prompt_sha256") or ""),
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

    @classmethod
    def for_epoch(cls, root: str | Path, *, stage_name: str, closure_epoch_id: str) -> "ProviderRuntimeLedger":
        """Open the immutable stage/epoch ledger location.

        Keeping the epoch in the path prevents a retry from silently mixing
        receipts with a previous attempt.  Legacy callers may continue to
        pass an explicit JSONL path to the normal constructor.
        """

        safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage_name or "stage"))
        safe_epoch = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(closure_epoch_id or "unknown"))
        root_path = Path(root).expanduser().resolve()
        if root_path.suffix.casefold() == ".jsonl":
            root_path = root_path.parent
        return cls(root_path / "provider_receipts" / safe_stage / f"{safe_epoch}.jsonl")

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
            with interprocess_file_lock(self.path):
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
                    os.fsync(handle.fileno())
        return receipt

    def list_receipts(self) -> tuple[ProviderCallReceiptV1, ...]:
        with self._lock:
            with interprocess_file_lock(self.path):
                return tuple(self._read_unlocked())

    def list_acceptance_receipts(
        self,
        *,
        expected_job_id: str = "",
        expected_attempt_id: str = "",
        expected_stage_name: str = "",
    ) -> tuple[ProviderCallReceiptV1, ...]:
        """Load receipts through the strict live-acceptance authority parser."""

        receipts = self.list_receipts()
        for receipt in receipts:
            receipt.validate_acceptance_authority(
                expected_job_id=expected_job_id,
                expected_attempt_id=expected_attempt_id,
                expected_stage_name=expected_stage_name,
            )
        return receipts

    def usage_summary(self) -> dict[str, Any]:
        """Recompute physical call usage from durable receipts."""

        receipts = self.list_receipts()

        def attempted(receipt: ProviderCallReceiptV1) -> bool:
            if receipt.status == "success":
                return True
            metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
            return bool(metadata.get("transport_config"))

        used = [receipt for receipt in receipts if attempted(receipt)]

        def breakdown(items: list[ProviderCallReceiptV1], key: str) -> dict[str, Any]:
            grouped: dict[str, dict[str, Any]] = {}
            for receipt in items:
                group = str(getattr(receipt, key) or "unknown")
                entry = grouped.setdefault(
                    group,
                    {"calls": 0, "output_tokens_reported": 0, "unreported_output_receipts": 0, "retries": 0},
                )
                entry["calls"] += int(receipt.attempts)
                entry["retries"] += max(0, int(receipt.attempts) - 1)
                if receipt.output_tokens is None:
                    entry["unreported_output_receipts"] += 1
                else:
                    entry["output_tokens_reported"] += int(receipt.output_tokens)
            return grouped

        output_reported = sum(
            int(receipt.output_tokens)
            for receipt in used
            if receipt.output_tokens is not None
        )
        return {
            "receipt_count": len(receipts),
            "physical_calls_used": sum(int(receipt.attempts) for receipt in used),
            "output_tokens_reported": output_reported,
            "unreported_output_receipts": sum(
                1 for receipt in used if receipt.output_tokens is None
            ),
            "retry_attempts_used": sum(
                max(0, int(receipt.attempts) - 1) for receipt in used
            ),
            "by_stage": breakdown(used, "stage_name"),
            "by_provider": breakdown(used, "provider"),
        }

    def retag_epoch(self, previous: str, current: str) -> int:
        """Rebind receipts written under ``previous`` to ``current``.

        One logical Stage 1 attempt finalizes its expected call graph after
        visual scans have already produced receipts (the synthesis request
        identity is only known post-scan).  When the closure epoch moves to
        that final graph, the receipts already appended for the same attempt
        must follow the attempt, otherwise the epoch filter silently drops
        them.  The ledger is a staging file owned by one attempt at this
        point, so the rewrite is lossless and each migrated receipt records
        the rebind in its metadata.
        """
        if not str(previous) or not str(current) or str(previous) == str(current):
            return 0
        with self._lock:
            with interprocess_file_lock(self.path):
                receipts = self._read_unlocked()
                changed = []
                for receipt in receipts:
                    if str(receipt.closure_epoch_id or "") != str(previous):
                        changed.append(receipt)
                        continue
                    metadata = dict(receipt.metadata)
                    metadata["closure_epoch_retagged_from"] = str(previous)
                    changed.append(
                        replace(
                            receipt,
                            closure_epoch_id=str(current),
                            metadata=metadata,
                        )
                    )
                migrated_count = sum(
                    1
                    for receipt in changed
                    if str(receipt.closure_epoch_id or "") == str(current)
                    and str(receipt.metadata.get("closure_epoch_retagged_from") or "") == str(previous)
                )
                if not migrated_count:
                    return 0
                encoded_lines = [
                    _canonical_json(receipt.to_dict()) + "\n" for receipt in changed
                ]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = self.path.with_suffix(self.path.suffix + f".retag-{uuid.uuid4().hex}.tmp")
                try:
                    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                        handle.writelines(encoded_lines)
                        handle.flush()
                        os.fsync(handle.fileno())
                    atomic_replace_with_retry(temp_path, self.path, timeout_seconds=5.0)
                finally:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                return migrated_count


class ProviderRuntime:
    """Admission controller and receipt producer for one job/attempt/stage."""

    def __init__(
        self,
        *,
        budget: ProviderBudgetV1 | None = None,
        aggregate_budget: ProviderBudgetController | None = None,
        ledger: ProviderRuntimeLedger | None = None,
        job_id: str = "",
        attempt_id: str = "",
        stage_name: str = "",
        route: str = "",
        schema_hash: str | None = None,
        node_id: str = "",
        call_id: str = "",
        closure_epoch_id: str = "",
        logical_attempt_identity: str = "",
        endpoint_type: str = "",
        test_only: bool = False,
        prompt_id: str = "",
        prompt_version: str = "",
        prompt_sha256: str = "",
    ) -> None:
        if not test_only:
            missing = [
                name
                for name, value in {
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "stage_name": stage_name,
                    "route": route,
                    "node_id": node_id,
                    "call_id": call_id,
                    "ledger": ledger,
                }.items()
                if not str(value or "").strip()
            ]
            if missing:
                raise ProviderRuntimeContractError(
                    "bound ProviderRuntime requires: " + ", ".join(missing)
                )
        self.budget = budget or ProviderBudgetV1()
        self.aggregate_budget = (
            aggregate_budget
            or _ACTIVE_ACCEPTANCE_CONTROLLER.get()
            or provider_budget_controller_from_environment()
        )
        self.ledger = ledger
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.stage_name = stage_name
        self.route = route
        self.node_id = node_id
        self.call_id = call_id
        self.logical_attempt_identity = str(logical_attempt_identity or attempt_id)
        self.endpoint_type = endpoint_type
        self.prompt_id = str(prompt_id or "")
        self.prompt_version = str(prompt_version or "")
        self.prompt_sha256 = str(prompt_sha256 or "")
        self.test_only = bool(test_only)
        self.schema_hash = schema_hash or hash_text("provider-runtime-default-schema-v1")
        self.closure_epoch_id = str(closure_epoch_id or "")
        if not self.closure_epoch_id and not self.test_only:
            self.closure_epoch_id = compute_closure_epoch_id(
                job_id=self.job_id,
                stage_name=self.stage_name,
                logical_attempt_identity=self.logical_attempt_identity,
                expected_call_graph_hash=hash_json({"node_id": self.node_id, "call_id": self.call_id}),
                current_input_artifact_hashes=(),
                provider_config_hash=hash_json({"route": self.route}),
                schema_version=self.schema_hash,
            )
        self.started_monotonic = time.monotonic()
        self.started_at = utc_now_iso()
        self._lock = threading.RLock()
        self._calls = 0
        self._reserved_tokens = 0
        self._aggregate_reservations: dict[int, ProviderAggregateReservationV1] = {}
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

    def admit(
        self,
        *,
        estimated_tokens: int = 0,
        requested_output_tokens: int = 0,
        requested_retry_attempts: int = 0,
    ) -> ProviderCallAdmissionV1:
        estimated = max(0, int(estimated_tokens))
        output = max(0, int(requested_output_tokens))
        retries = max(0, int(requested_retry_attempts))
        with self._lock:
            elapsed = time.monotonic() - self.started_monotonic
            if self.budget.max_elapsed_seconds and elapsed >= self.budget.max_elapsed_seconds:
                raise ProviderBudgetExceeded("provider runtime elapsed-time budget exhausted")
            if self.budget.max_calls and self._calls >= self.budget.max_calls:
                raise ProviderBudgetExceeded("provider runtime call budget exhausted")
            if self.budget.max_total_tokens and self._reserved_tokens + estimated + output > self.budget.max_total_tokens:
                raise ProviderBudgetExceeded("provider runtime token budget exhausted")
            aggregate_reservation = None
            if self.aggregate_budget is not None:
                aggregate_reservation = self.aggregate_budget.admit(
                    requested_output_tokens=output,
                    requested_retry_attempts=retries,
                    context={
                        "job_id": self.job_id,
                        "attempt_id": self.attempt_id,
                        "stage_name": self.stage_name,
                        "node_id": self.node_id,
                        "call_id": self.call_id,
                        "ledger_path": str(self.ledger.path) if self.ledger is not None else "",
                    },
                )
            self._calls += 1
            self._reserved_tokens += estimated + output
            if aggregate_reservation is not None:
                self._aggregate_reservations[self._calls] = aggregate_reservation
            return ProviderCallAdmissionV1(
                sequence=self._calls,
                estimated_tokens=estimated,
                admitted_at=utc_now_iso(),
                remaining_calls=(self.budget.max_calls - self._calls) if self.budget.max_calls else None,
                remaining_tokens=(self.budget.max_total_tokens - self._reserved_tokens)
                if self.budget.max_total_tokens
                else None,
                estimated_output_tokens=output,
                reserved_call_attempts=(aggregate_reservation.provider_calls if aggregate_reservation else 1 + retries),
                reserved_retry_attempts=(aggregate_reservation.retry_attempts if aggregate_reservation else retries),
                aggregate_reservation_id=(aggregate_reservation.reservation_id if aggregate_reservation else None),
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
        route: str | None = None,
    ) -> ProviderCallReceiptV1:
        provider = str(api_config.get("provider_family") or api_config.get("provider") or "generic")
        model = str(api_config.get("model") or "")
        endpoint = str(api_config.get("api_base") or "")
        config_hash = hash_json(_redact_mapping(api_config))
        aggregate_reservation = None
        aggregate_snapshot: dict[str, Any] | None = None
        with self._lock:
            aggregate_reservation = self._aggregate_reservations.pop(admission.sequence, None)
        if self.aggregate_budget is not None and aggregate_reservation is not None:
            aggregate_snapshot = self.aggregate_budget.complete(aggregate_reservation, result)
        receipt_metadata = dict(metadata or {})
        if aggregate_snapshot is not None:
            receipt_metadata["aggregate_budget"] = aggregate_snapshot
        if aggregate_reservation is not None:
            receipt_metadata["aggregate_reservation_id"] = aggregate_reservation.reservation_id
        receipt = ProviderCallReceiptV1.from_result(
            admission=admission,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            stage_name=self.stage_name,
            route=str(route or self.route or provider),
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
            metadata=receipt_metadata,
            node_id=self.node_id,
            call_id=str((metadata or {}).get("call_id") or self.call_id or f"call-{admission.sequence}"),
            closure_epoch_id=self.closure_epoch_id,
            logical_attempt_identity=self.logical_attempt_identity,
            endpoint_type=str(result.get("endpoint_type") or self.endpoint_type),
            test_only=self.test_only,
            prompt_id=self.prompt_id,
            prompt_version=self.prompt_version,
            prompt_sha256=self.prompt_sha256,
        )
        with self._lock:
            self._reserved_tokens = max(
                0,
                self._reserved_tokens
                - admission.estimated_tokens
                - admission.estimated_output_tokens,
            )
            if self.ledger is not None:
                self.ledger.append(receipt)
            self._receipts.append(receipt)
        return receipt

    def mark_transport_started(self, admission: ProviderCallAdmissionV1) -> None:
        """Durably mark the boundary immediately before a transport call."""

        if self.aggregate_budget is None or admission.aggregate_reservation_id is None:
            return
        with self._lock:
            reservation = self._aggregate_reservations.get(admission.sequence)
        if reservation is None:
            raise ProviderRuntimeContractError(
                f"aggregate reservation is missing for admission sequence {admission.sequence}"
            )
        self.aggregate_budget.mark_transport_started(reservation)

    def blocked_receipt(
        self,
        *,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        error_kind: ProviderErrorKind = "budget_exhausted",
        message: str = "provider runtime admission rejected",
        schema_hash: str | None = None,
        route: str | None = None,
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
                estimated_output_tokens=0,
                reserved_call_attempts=0,
                reserved_retry_attempts=0,
            )
        receipt = self.complete(
            admission=admission,
            prompt=prompt,
            input_payload=input_payload,
            api_config=api_config,
            result={"status": "failed", "error_kind": error_kind, "message": _redact_text(message)},
            schema_hash=schema_hash,
            route=route,
        )
        return receipt


__all__ = [
    "AcceptanceExecutionContextV1",
    "ProviderBudgetExceeded",
    "ProviderAggregateBudgetV1",
    "ProviderAggregateReservationV1",
    "ProviderBudgetController",
    "ProviderBudgetV1",
    "ProviderCallAdmissionV1",
    "ProviderCallReceiptV1",
    "ProviderErrorKind",
    "ProviderReceiptConflict",
    "ProviderRuntime",
    "ProviderRuntimeContractError",
    "ProviderRuntimeLedger",
    "ProcessIdentityV1",
    "ProcessLiveness",
    "canonical_provider_request_payload",
    "bind_acceptance_execution_context",
    "compute_closure_epoch_id",
    "current_acceptance_execution_context",
    "hash_json",
    "hash_text",
    "is_process_alive",
    "process_identity_for_pid",
    "process_liveness",
    "provider_budget_controller_from_environment",
    "provider_request_input_hash",
    "stable_provider_hash",
]

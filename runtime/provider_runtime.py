from __future__ import annotations

"""Provider-call budgets, redacted receipts, and fail-closed error taxonomy.

This module is deliberately transport-neutral.  The HTTP adapter performs the
request, while this runtime owns the retry ceiling and records the durable
facts needed to audit that decision.  Secrets and raw prompts never belong in
a receipt.
"""

import ctypes
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Literal, Mapping, Protocol, cast

from runtime.primary_repair_recovery import (
    PrimaryRepairRecoveryError,
    PrimaryRepairResponseEntryV1,
    PrimaryRepairResponseJournal,
    PrimaryRepairResponseRefV1,
    PrimaryRepairScopeV1,
)
from services.durable_io import atomic_replace_with_retry, interprocess_file_lock
from services.job_workspace import atomic_write_json, utc_now_iso

PROVIDER_RECEIPT_ARTIFACT_TYPE = "provider_call_receipt"
PROVIDER_RECEIPT_ARTIFACT_VERSION = "v2"
PROVIDER_RECEIPT_LEDGER_VERSION = "provider-receipt-ledger-v1"
# Compatibility/default run budget only. Explicit per-run budgets are not
# truncated to this value; the aggregate budget controller enforces the run's
# actual approved envelope.
DEFAULT_PROVIDER_CALL_BUDGET = 24


def authorized_provider_call_limit(configured_limit: int | None = None) -> int:
    """Normalize a per-run call budget without applying a product-wide ceiling."""

    if configured_limit is None:
        return DEFAULT_PROVIDER_CALL_BUDGET
    if isinstance(configured_limit, bool):
        raise ValueError("provider call budget must be an integer, not a boolean")
    requested = int(configured_limit)
    return max(0, requested)

ProviderErrorKind = Literal[
    "quota_exhausted",
    "retryable_http",
    "fatal_config_or_auth",
    "transient_network",
    "outcome_unknown",
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
        "outcome_unknown",
        "invalid_response",
        "budget_exhausted",
        "cancelled",
    }
)
_CALL_STATUSES = frozenset({"success", "failed", "blocked"})

_ACTIVE_PAUSE_STATE_PATH: ContextVar[str] = ContextVar(
    "auto_generate_pause_state_path",
    default="",
)


def bind_pause_state_path(path: str | Path) -> Token[str]:
    """Bind the current runtime context to its durable pause marker."""

    return _ACTIVE_PAUSE_STATE_PATH.set(str(Path(path).expanduser().resolve()))


def current_pause_state_path() -> str:
    return str(_ACTIVE_PAUSE_STATE_PATH.get() or "").strip()
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


class ProviderCancellationRequested(ProviderBudgetExceeded):
    """A verified cancellation blocked admission before a new transport."""


class ProviderReceiptConflict(RuntimeError):
    """Raised when an append-only receipt ID is reused with different content."""


@dataclass(frozen=True)
class RuntimeControlIdentityV1:
    """Typed identity for one job execution and its optional queue lease."""

    job_id: str
    workspace_path: str
    acceptance_run_id: str = ""
    lease_id: str = ""
    lease_generation: int = 0
    provider_budget_started: bool = False

    def __post_init__(self) -> None:
        job_id = str(self.job_id or "").strip()
        workspace_path = str(self.workspace_path or "").strip()
        acceptance_run_id = str(self.acceptance_run_id or "").strip()
        lease_id = str(self.lease_id or "").strip()
        if not job_id or not workspace_path:
            raise ProviderRuntimeContractError(
                "runtime control identity requires a job ID and workspace path"
            )
        if isinstance(self.lease_generation, bool) or not isinstance(self.lease_generation, int):
            raise ProviderRuntimeContractError("runtime control lease generation must be an integer")
        if self.lease_generation < 0 or bool(lease_id) != bool(self.lease_generation):
            raise ProviderRuntimeContractError("runtime control lease identity is incomplete")
        if not isinstance(self.provider_budget_started, bool):
            raise ProviderRuntimeContractError("runtime control budget-started flag must be boolean")
        if self.provider_budget_started and not acceptance_run_id:
            raise ProviderRuntimeContractError(
                "runtime control started provider budget requires an acceptance run ID"
            )
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "workspace_path", str(Path(workspace_path).expanduser().resolve()))
        object.__setattr__(self, "acceptance_run_id", acceptance_run_id)
        object.__setattr__(self, "lease_id", lease_id)


@dataclass(frozen=True)
class RuntimeControlContextV1:
    identity: RuntimeControlIdentityV1
    pause_state_store: Any
    cancellation_store: Any
    lease_validator: Callable[[], bool] | None = None
    cancel_token: Any | None = None


_ACTIVE_RUNTIME_CONTROL_CONTEXT: ContextVar[RuntimeControlContextV1 | None] = ContextVar(
    "auto_generate_runtime_control_context",
    default=None,
)


def current_runtime_control_context() -> RuntimeControlContextV1 | None:
    return _ACTIVE_RUNTIME_CONTROL_CONTEXT.get()


@contextmanager
def runtime_control_lifecycle():
    """Clear legacy job context at a run boundary and prevent same-thread leakage."""

    previous_context = _ACTIVE_RUNTIME_CONTROL_CONTEXT.get()
    context_token = _ACTIVE_RUNTIME_CONTROL_CONTEXT.set(None)
    pause_token = _ACTIVE_PAUSE_STATE_PATH.set("")
    try:
        yield
    finally:
        if previous_context is None:
            _ACTIVE_RUNTIME_CONTROL_CONTEXT.reset(context_token)
            _ACTIVE_PAUSE_STATE_PATH.set("")
        else:
            _ACTIVE_RUNTIME_CONTROL_CONTEXT.reset(context_token)
            _ACTIVE_PAUSE_STATE_PATH.reset(pause_token)


@contextmanager
def bind_runtime_control_context(
    identity: RuntimeControlIdentityV1,
    *,
    workspace: Any,
    registry: Any,
    lease_validator: Callable[[], bool] | None = None,
    cancel_token: Any | None = None,
):
    """Bind hash-verified pause/cancel authorities to one scoped job run."""

    from runtime.cancellation import CancellationRequestStore
    from runtime.pause_state import PauseStateStore

    previous_context = _ACTIVE_RUNTIME_CONTROL_CONTEXT.get()
    if previous_context is not None:
        previous_identity = previous_context.identity
        same_owner = (
            previous_identity.job_id == identity.job_id
            and previous_identity.workspace_path == identity.workspace_path
        )
        if same_owner:
            if (
                identity.lease_id
                and previous_identity.lease_id
                and (
                    identity.lease_id != previous_identity.lease_id
                    or identity.lease_generation != previous_identity.lease_generation
                )
            ):
                raise ProviderRuntimeContractError(
                    "nested runtime control context changed the owning queue lease"
                )
            if not identity.lease_id and previous_identity.lease_id:
                identity = replace(
                    identity,
                    lease_id=previous_identity.lease_id,
                    lease_generation=previous_identity.lease_generation,
                )
            if lease_validator is None:
                lease_validator = previous_context.lease_validator
            if cancel_token is None:
                cancel_token = previous_context.cancel_token
            if not identity.acceptance_run_id and previous_identity.acceptance_run_id:
                identity = replace(
                    identity,
                    acceptance_run_id=previous_identity.acceptance_run_id,
                    provider_budget_started=(
                        identity.provider_budget_started
                        or previous_identity.provider_budget_started
                    ),
                )
            elif (
                identity.acceptance_run_id
                and identity.acceptance_run_id == previous_identity.acceptance_run_id
                and previous_identity.provider_budget_started
                and not identity.provider_budget_started
            ):
                identity = replace(identity, provider_budget_started=True)
            elif (
                identity.acceptance_run_id
                and previous_identity.acceptance_run_id
                and identity.acceptance_run_id != previous_identity.acceptance_run_id
            ):
                raise ProviderRuntimeContractError(
                    "nested runtime control context changed the acceptance run identity"
                )
    workspace_job_id = str(getattr(workspace, "job_id", "") or "")
    workspace_root = str(Path(getattr(getattr(workspace, "paths", None), "root_dir", "")).resolve())
    if workspace_job_id != identity.job_id or workspace_root != identity.workspace_path:
        raise ProviderRuntimeContractError(
            "runtime control identity does not match its workspace"
        )
    if str(getattr(registry, "job_id", "") or "") != identity.job_id:
        raise ProviderRuntimeContractError(
            "runtime control identity does not match its Registry"
        )
    pause_store = PauseStateStore(workspace, registry)
    cancellation_store = CancellationRequestStore(workspace, registry)
    context = RuntimeControlContextV1(
        identity=identity,
        pause_state_store=pause_store,
        cancellation_store=cancellation_store,
        lease_validator=lease_validator,
        cancel_token=cancel_token,
    )
    previous_context = _ACTIVE_RUNTIME_CONTROL_CONTEXT.get()
    context_token = _ACTIVE_RUNTIME_CONTROL_CONTEXT.set(context)
    pause_path_token = _ACTIVE_PAUSE_STATE_PATH.set(str(pause_store.path.resolve()))
    try:
        yield context
    finally:
        if previous_context is None:
            _ACTIVE_PAUSE_STATE_PATH.set("")
        else:
            _ACTIVE_PAUSE_STATE_PATH.reset(pause_path_token)
        _ACTIVE_RUNTIME_CONTROL_CONTEXT.reset(context_token)


def _registered_pause_store_for_path(marker_path: str) -> Any | None:
    """Resolve a legacy pause path back to its Registry when it has workspace shape."""

    path = Path(marker_path).expanduser().resolve()
    if path.parent.name != "pause_state" or path.suffix.lower() != ".json":
        return None
    workspace_root = path.parent.parent.parent
    job_id = path.stem
    marker = f"__{job_id}"
    if not job_id or not workspace_root.name.endswith(marker):
        return None
    project_name = workspace_root.name[: -len(marker)]
    if not project_name:
        return None
    try:
        from runtime.pause_state import PauseStateStore
        from services.artifact_registry import ArtifactRegistry
        from services.job_workspace import JobWorkspace

        workspace = JobWorkspace(str(workspace_root.parent), project_name, job_id)
        if str(Path(workspace.paths.root_dir).resolve()) != str(workspace_root):
            raise ProviderRuntimeContractError("pause marker workspace path is inconsistent")
        registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
        return PauseStateStore(workspace, registry)
    except ProviderRuntimeContractError:
        raise
    except Exception as exc:
        raise ProviderRuntimeContractError(
            f"CONTROL_STATE_INVALID: pause workspace cannot be verified: {type(exc).__name__}"
        ) from exc


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
class ProviderAggregateBudgetV2:
    """Strict finite run budget: zero is a real zero allowance.

    V1 state files keep their historical zero-as-unbounded interpretation.
    New approved runs use this nested budget schema and must provide every
    limit explicitly, including an explicit retry allowance of zero.
    """

    SCHEMA_VERSION: ClassVar[str] = "provider-aggregate-budget-contract-v2"

    max_provider_calls_total: int
    max_output_tokens_total: int
    max_retry_attempts_total: int
    max_wall_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls_total",
            "max_output_tokens_total",
            "max_retry_attempts_total",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderRuntimeContractError(f"{name} must be a non-negative integer")
        if (
            isinstance(self.max_wall_seconds, bool)
            or not math.isfinite(float(self.max_wall_seconds))
            or float(self.max_wall_seconds) < 0
        ):
            raise ProviderRuntimeContractError("max_wall_seconds must be non-negative")
        object.__setattr__(self, "max_wall_seconds", float(self.max_wall_seconds))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderAggregateBudgetV2":
        if not isinstance(value, Mapping) or value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ProviderRuntimeContractError(
                "strict provider aggregate budget schema is missing or unsupported"
            )
        required = (
            "max_provider_calls_total",
            "max_output_tokens_total",
            "max_retry_attempts_total",
            "max_wall_seconds",
        )
        allowed = {"schema_version", *required}
        unknown = sorted(str(key) for key in value if str(key) not in allowed)
        if unknown:
            raise ProviderRuntimeContractError(
                "strict provider aggregate budget contains unknown fields: "
                + ", ".join(unknown)
            )
        missing = [name for name in required if name not in value]
        if missing:
            raise ProviderRuntimeContractError(
                "strict provider aggregate budget is missing limits: " + ", ".join(missing)
            )

        integers: dict[str, int] = {}
        for name in required[:3]:
            raw = value[name]
            if isinstance(raw, bool):
                raise ProviderRuntimeContractError(f"{name} must be an integer")
            try:
                parsed = int(str(raw).strip())
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(f"{name} must be an integer") from exc
            if parsed < 0:
                raise ProviderRuntimeContractError(f"{name} must be non-negative")
            integers[name] = parsed
        try:
            wall = float(str(value["max_wall_seconds"]).strip())
        except (TypeError, ValueError) as exc:
            raise ProviderRuntimeContractError("max_wall_seconds must be numeric") from exc
        return cls(**integers, max_wall_seconds=wall)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "max_provider_calls_total": self.max_provider_calls_total,
            "max_output_tokens_total": self.max_output_tokens_total,
            "max_retry_attempts_total": self.max_retry_attempts_total,
            "max_wall_seconds": self.max_wall_seconds,
        }


ProviderAggregateBudget = ProviderAggregateBudgetV1 | ProviderAggregateBudgetV2


def provider_aggregate_budget_from_mapping(value: Mapping[str, Any]) -> ProviderAggregateBudget:
    """Decode an explicitly versioned strict budget or a legacy V1 budget."""

    schema_version = value.get("schema_version") if isinstance(value, Mapping) else None
    if schema_version == ProviderAggregateBudgetV2.SCHEMA_VERSION:
        return ProviderAggregateBudgetV2.from_mapping(value)
    if schema_version is not None:
        raise ProviderRuntimeContractError("provider aggregate budget schema is unsupported")
    return ProviderAggregateBudgetV1.from_mapping(value)


@dataclass(frozen=True)
class AcceptanceExecutionContextV1:
    """Run-scoped acceptance authority propagated to provider runtimes."""

    acceptance_run_id: str
    final_executable_sha: str
    absolute_deadline_epoch: float
    provider_budget: ProviderAggregateBudget
    provider_budget_state_path: str
    evidence_root: str
    process_event_log: str
    scenario_state_path: str
    owner_authorized: bool
    provider_budget_state_started: bool = False

    def __post_init__(self) -> None:
        if not str(self.acceptance_run_id).strip():
            raise ProviderRuntimeContractError("acceptance execution context run ID is required")
        normalized_sha = str(self.final_executable_sha).strip().lower()
        if len(normalized_sha) not in {40, 64} or any(
            char not in "0123456789abcdef" for char in normalized_sha
        ):
            raise ProviderRuntimeContractError("acceptance execution context final SHA is required")
        object.__setattr__(self, "final_executable_sha", normalized_sha)
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
        if not isinstance(self.owner_authorized, bool):
            raise ProviderRuntimeContractError(
                "acceptance execution context owner_authorized must be a boolean"
            )
        if not isinstance(self.provider_budget_state_started, bool):
            raise ProviderRuntimeContractError(
                "acceptance execution context provider_budget_state_started must be a boolean"
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
            "provider_budget_state_started": self.provider_budget_state_started,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AcceptanceExecutionContextV1":
        if not isinstance(payload, Mapping):
            raise ProviderRuntimeContractError(
                "acceptance execution context must be a JSON object"
            )
        allowed = {
            "acceptance_run_id",
            "final_executable_sha",
            "absolute_deadline_epoch",
            "provider_budget",
            "provider_budget_state_path",
            "evidence_root",
            "process_event_log",
            "scenario_state_path",
            "owner_authorized",
            "provider_budget_state_started",
        }
        unknown = sorted(str(key) for key in payload if str(key) not in allowed)
        if unknown:
            raise ProviderRuntimeContractError(
                "acceptance execution context contains unknown fields: "
                + ", ".join(unknown)
            )
        raw_budget = payload.get("provider_budget")
        if not isinstance(raw_budget, Mapping):
            raise ProviderRuntimeContractError(
                "acceptance execution context provider_budget must be an object"
            )
        owner_authorized = payload.get("owner_authorized")
        if not isinstance(owner_authorized, bool):
            raise ProviderRuntimeContractError(
                "acceptance execution context owner_authorized must be a boolean"
            )
        provider_budget_state_started = payload.get("provider_budget_state_started", False)
        if not isinstance(provider_budget_state_started, bool):
            raise ProviderRuntimeContractError(
                "acceptance execution context provider_budget_state_started must be a boolean"
            )
        try:
            deadline = float(str(payload.get("absolute_deadline_epoch") or ""))
        except (TypeError, ValueError) as exc:
            raise ProviderRuntimeContractError(
                "acceptance execution context deadline must be numeric"
            ) from exc
        return cls(
            acceptance_run_id=str(payload.get("acceptance_run_id") or ""),
            final_executable_sha=str(payload.get("final_executable_sha") or ""),
            absolute_deadline_epoch=deadline,
            provider_budget=provider_aggregate_budget_from_mapping(raw_budget),
            provider_budget_state_path=str(
                payload.get("provider_budget_state_path") or ""
            ),
            evidence_root=str(payload.get("evidence_root") or ""),
            process_event_log=str(payload.get("process_event_log") or ""),
            scenario_state_path=str(payload.get("scenario_state_path") or ""),
            owner_authorized=owner_authorized,
            provider_budget_state_started=provider_budget_state_started,
        )


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
_ACTIVE_RUN_PROVIDER_CONTROLLER: ContextVar["ProviderBudgetController | None"] = ContextVar(
    "active_run_provider_budget_controller",
    default=None,
)
_ACTIVE_PROVIDER_RUN_SCOPE_PROOF: ContextVar["ProviderRunScopeProofV1 | None"] = ContextVar(
    "active_provider_run_scope_proof",
    default=None,
)
_ACTIVE_PROVIDER_RUN_SCOPE_AUTHORIZATION: ContextVar[
    "ProviderRunScopeAuthorizationV1 | None"
] = ContextVar(
    "active_provider_run_scope_authorization",
    default=None,
)
_ACTIVE_PROVIDER_RUN_PLAN_BINDING: ContextVar[
    "ProviderRunPlanBindingV1 | None"
] = ContextVar(
    "active_provider_run_plan_binding",
    default=None,
)


_PROVIDER_RUN_SCOPE_HASH_FIELDS = frozenset(
    {
        "runtime_spec_sha256",
        "effective_config_sha256",
        "source_snapshot_sha256",
        "review_goal_sha256",
        "route_plan_sha256",
        "stage_work_contract_sha256",
    }
)
_PROVIDER_RUN_SCOPE_PROOF_TOKEN = object()
_PROVIDER_RUN_SCOPE_AUTHORIZATION_TOKEN = object()
_PROVIDER_RUN_PLAN_BINDING_TOKEN = object()


@dataclass(frozen=True)
class ProviderRunScopeProofV1:
    """An in-process proof binding an accepted continuation to one run scope.

    Instances are made only after the caller verifies the ready Registry
    ``provider_run_envelope`` artifact. The proof is deliberately not decoded
    from environment variables: inherited serialized context alone cannot
    authorize a new RuntimeJobSpec.
    """

    job_id: str
    run_id: str
    provider_budget_sha256: str
    scope_hashes: Mapping[str, str]
    artifact_id: str
    artifact_sha256: str
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _PROVIDER_RUN_SCOPE_PROOF_TOKEN:
            raise ProviderRuntimeContractError(
                "provider run scope proof must come from a verified Registry envelope"
            )
        if not str(self.job_id or "").strip() or not str(self.run_id or "").strip():
            raise ProviderRuntimeContractError(
                "provider run scope proof requires job_id and run_id"
            )
        if self.artifact_id != "provider_run_envelope":
            raise ProviderRuntimeContractError(
                "provider run scope proof must reference the run envelope artifact"
            )
        for name, value in (
            ("provider_budget_sha256", self.provider_budget_sha256),
            ("artifact_sha256", self.artifact_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None:
                raise ProviderRuntimeContractError(
                    f"provider run scope proof {name} is invalid"
                )
        if not isinstance(self.scope_hashes, Mapping):
            raise ProviderRuntimeContractError(
                "provider run scope proof hashes must be a mapping"
            )
        if set(self.scope_hashes) != _PROVIDER_RUN_SCOPE_HASH_FIELDS:
            raise ProviderRuntimeContractError(
                "provider run scope proof hashes do not match the run envelope contract"
            )
        normalized: dict[str, str] = {}
        for name in sorted(_PROVIDER_RUN_SCOPE_HASH_FIELDS):
            value = str(self.scope_hashes.get(name) or "")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ProviderRuntimeContractError(
                    f"provider run scope proof hash is invalid: {name}"
                )
            normalized[name] = value
        object.__setattr__(self, "scope_hashes", normalized)


@dataclass(frozen=True)
class SharedRetryProjectionV1:
    """Potential physical attempts under per-call ceilings and a shared pool."""

    logical_calls: int | None
    configured_retry_slots_upper_bound: int | None
    aggregate_retry_pool_remaining: int | None
    retry_attempts_upper_bound: int | None
    physical_attempts_upper_bound: int | None
    retry_bound_source: str


@dataclass(frozen=True)
class ProviderRunScopeAuthorizationV1:
    """ControlPlane-verified exact scope approved for one accepted run.

    The artifact payload is created from a verified acceptance plan by the
    ControlPlane. It is distinct from the Runner-produced budget envelope: it
    authorizes the first envelope to be minted for this scope.
    """

    job_id: str
    run_id: str
    provider_budget_sha256: str
    scope_hashes: Mapping[str, str]
    artifact_id: str
    artifact_sha256: str
    acceptance_plan_sha256: str
    acceptance_spec_file_sha256: str
    scenario_id: str
    runtime_spec_file_sha256: str
    input_identity_sha256: str
    workspace_identity_sha256: str
    final_executable_sha: str
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _PROVIDER_RUN_SCOPE_AUTHORIZATION_TOKEN:
            raise ProviderRuntimeContractError(
                "provider run scope authorization must come from a verified accepted-plan artifact"
            )
        if not str(self.job_id or "").strip() or not str(self.run_id or "").strip():
            raise ProviderRuntimeContractError(
                "provider run scope authorization requires job_id and run_id"
            )
        for label, value in (
            ("artifact_id", self.artifact_id),
            ("scenario_id", self.scenario_id),
        ):
            if not str(value or "").strip():
                raise ProviderRuntimeContractError(
                    f"provider run scope authorization {label} is required"
                )
        for name, value in (
            ("provider_budget_sha256", self.provider_budget_sha256),
            ("artifact_sha256", self.artifact_sha256),
            ("acceptance_plan_sha256", self.acceptance_plan_sha256),
            ("acceptance_spec_file_sha256", self.acceptance_spec_file_sha256),
            ("runtime_spec_file_sha256", self.runtime_spec_file_sha256),
            ("input_identity_sha256", self.input_identity_sha256),
            ("workspace_identity_sha256", self.workspace_identity_sha256),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None:
                raise ProviderRuntimeContractError(
                    f"provider run scope authorization {name} is invalid"
                )
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.final_executable_sha) is None:
            raise ProviderRuntimeContractError(
                "provider run scope authorization final executable SHA is invalid"
            )
        if not isinstance(self.scope_hashes, Mapping):
            raise ProviderRuntimeContractError(
                "provider run scope authorization hashes must be a mapping"
            )
        if set(self.scope_hashes) != _PROVIDER_RUN_SCOPE_HASH_FIELDS:
            raise ProviderRuntimeContractError(
                "provider run scope authorization hashes do not match the run envelope contract"
            )
        normalized: dict[str, str] = {}
        for name in sorted(_PROVIDER_RUN_SCOPE_HASH_FIELDS):
            value = str(self.scope_hashes.get(name) or "")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ProviderRuntimeContractError(
                    f"provider run scope authorization hash is invalid: {name}"
                )
            normalized[name] = value
        object.__setattr__(self, "scope_hashes", normalized)


@dataclass(frozen=True)
class ProviderRunPlanBindingV1:
    """Active ControlPlane binding from one verified acceptance child plan."""

    acceptance_run_id: str
    acceptance_plan_sha256: str
    acceptance_spec_file_sha256: str
    scenario_id: str
    job_id: str
    runtime_spec_sha256: str
    runtime_spec_file_sha256: str
    scope_hashes: Mapping[str, str]
    input_identity_sha256: str
    workspace_identity_sha256: str
    provider_budget_sha256: str
    final_executable_sha: str
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _PROVIDER_RUN_PLAN_BINDING_TOKEN:
            raise ProviderRuntimeContractError(
                "provider run plan binding must come from a verified active ControlPlane plan"
            )
        if not str(self.acceptance_run_id or "").strip():
            raise ProviderRuntimeContractError("accepted run plan binding run ID is required")
        if not str(self.scenario_id or "").strip() or not str(self.job_id or "").strip():
            raise ProviderRuntimeContractError(
                "accepted run plan binding scenario and job IDs are required"
            )
        for name in (
            "acceptance_plan_sha256",
            "acceptance_spec_file_sha256",
            "runtime_spec_sha256",
            "runtime_spec_file_sha256",
            "input_identity_sha256",
            "workspace_identity_sha256",
            "provider_budget_sha256",
        ):
            if re.fullmatch(r"[0-9a-f]{64}", str(getattr(self, name) or "")) is None:
                raise ProviderRuntimeContractError(
                    f"accepted run plan binding {name} is invalid"
                )
        if not isinstance(self.scope_hashes, Mapping) or set(self.scope_hashes) != _PROVIDER_RUN_SCOPE_HASH_FIELDS:
            raise ProviderRuntimeContractError(
                "accepted run plan binding does not contain the six-hash scope contract"
            )
        normalized_scope: dict[str, str] = {}
        for name in sorted(_PROVIDER_RUN_SCOPE_HASH_FIELDS):
            value = str(self.scope_hashes.get(name) or "")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ProviderRuntimeContractError(
                    f"accepted run plan binding scope hash is invalid: {name}"
                )
            normalized_scope[name] = value
        if normalized_scope["runtime_spec_sha256"] != self.runtime_spec_sha256:
            raise ProviderRuntimeContractError(
                "accepted run plan binding normalized RuntimeJobSpec hash differs from scope"
            )
        object.__setattr__(self, "scope_hashes", normalized_scope)
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.final_executable_sha) is None:
            raise ProviderRuntimeContractError(
                "accepted run plan binding final executable SHA is invalid"
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
    controller.bind_state_path(
        context.provider_budget_state_path,
        acceptance_run_id=context.acceptance_run_id,
        state_started=context.provider_budget_state_started,
    )
    context_token = _ACTIVE_ACCEPTANCE_CONTEXT.set(context)
    controller_token = _ACTIVE_ACCEPTANCE_CONTROLLER.set(controller)
    try:
        yield context
    finally:
        _ACTIVE_ACCEPTANCE_CONTROLLER.reset(controller_token)
        _ACTIVE_ACCEPTANCE_CONTEXT.reset(context_token)


@contextmanager
def bind_provider_run_budget(controller: ProviderBudgetController):
    """Bind a durable finite ordinary-run controller across shared stage paths.

    This is a budget binding only. It does not create acceptance authority or
    change the controller's persisted budget/run identity.
    """

    if not isinstance(controller, ProviderBudgetController):
        raise ProviderRuntimeContractError(
            "ordinary run provider budget binding requires a ProviderBudgetController"
        )
    if not isinstance(controller.budget, ProviderAggregateBudgetV2):
        raise ProviderRuntimeContractError(
            "ordinary run provider budget binding requires strict finite V2 limits"
        )
    if controller.state_path is None or not controller.state_started:
        raise ProviderRuntimeContractError(
            "ordinary run provider budget must be durably bound before stage execution"
        )
    token = _ACTIVE_RUN_PROVIDER_CONTROLLER.set(controller)
    try:
        yield controller
    finally:
        _ACTIVE_RUN_PROVIDER_CONTROLLER.reset(token)


def acceptance_context_environment(
    context: AcceptanceExecutionContextV1,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child-process environment without mutating the parent process."""

    environment = dict(base_environment if base_environment is not None else os.environ)
    environment["AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON"] = json.dumps(
        context.provider_budget.to_dict(), sort_keys=True, separators=(",", ":")
    )
    environment["AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH"] = (
        context.provider_budget_state_path
    )
    environment["AUTO_GENERATE_ACCEPTANCE_RUN_ID"] = context.acceptance_run_id
    environment["AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON"] = json.dumps(
        context.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return environment


def acceptance_execution_context_from_environment() -> AcceptanceExecutionContextV1 | None:
    raw = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON", "")).strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderRuntimeContractError(
            "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON is not valid JSON"
        ) from exc
    return AcceptanceExecutionContextV1.from_mapping(payload)


class ProviderBudgetController:
    """Thread-safe aggregate reservation and durable-receipt accounting."""

    def __init__(
        self,
        budget: ProviderAggregateBudget,
        *,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.budget = budget
        self._monotonic = monotonic
        self._started_monotonic = float(monotonic())
        self._first_started_epoch = float(time.time())
        self._absolute_deadline_epoch = (
            self._first_started_epoch + float(budget.max_wall_seconds)
            if budget.max_wall_seconds or isinstance(budget, ProviderAggregateBudgetV2)
            else 0.0
        )
        self._owner_id = uuid.uuid4().hex
        self._process_identity = process_identity_for_pid(os.getpid())
        self._lock = threading.RLock()
        self._reservations: dict[str, ProviderAggregateReservationV1] = {}
        self._primary_repair_scopes: dict[str, dict[str, Any]] = {}
        self._calls_used = 0
        self._output_tokens_used = 0
        self._retry_attempts_used = 0
        self._calls_reserved = 0
        self._output_tokens_reserved = 0
        self._retry_attempts_reserved = 0
        self._state_path: Path | None = None
        self._identity_path: Path | None = None
        self._acceptance_run_id = ""
        self._loaded_run_id = ""
        self._state_started = False
        self._ambiguous_reservation_ids: set[str] = set()
        self._verified_downstream_continuation: Any | None = None

    def bind_state_path(
        self,
        path: str | Path,
        *,
        acceptance_run_id: str = "",
        state_started: bool | None = None,
    ) -> None:
        """Bind aggregate usage to a job-owned durable state file."""

        target = Path(path).expanduser().resolve()
        run_id = str(acceptance_run_id or "").strip()
        if state_started is not None and not isinstance(state_started, bool):
            raise ProviderRuntimeContractError("provider budget state-started flag must be boolean")
        with self._lock:
            if self._state_path is not None and self._state_path != target:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget cannot be rebound to a different state path"
                )
            self._state_path = target
            self._identity_path = target.with_name(target.name + ".identity.json")
            previous_run_id = self._acceptance_run_id
            if self._acceptance_run_id and run_id and self._acceptance_run_id != run_id:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget acceptance run identity changed"
                )
            if run_id:
                self._acceptance_run_id = run_id
            if state_started is True:
                self._state_started = True
            with interprocess_file_lock(target):
                if not target.is_file():
                    identity = self._read_state_identity_unlocked()
                    if self._state_started or identity is not None:
                        raise ProviderRuntimeContractError(
                            "provider aggregate budget state is missing for a started run"
                        )
                    self._persist_state_unlocked()
                    self._persist_state_identity_unlocked()
                    self._state_started = True
                    return
                identity = self._read_state_identity_unlocked(validate_expected_run=False)
                stored_run_id = str(identity.get("acceptance_run_id") or "") if identity else ""
                if run_id and stored_run_id != run_id:
                    may_adopt = (
                        state_started is not True
                        and not previous_run_id
                        and not stored_run_id
                        and self._state_payload_is_pristine_unlocked()
                    )
                    if not may_adopt:
                        raise ProviderRuntimeContractError(
                            "provider aggregate budget state belongs to a different acceptance run"
                        )
                    self._adopt_pristine_state_run_id_unlocked(run_id)
                    identity = self._read_state_identity_unlocked()
                if state_started is True and identity is None:
                    raise ProviderRuntimeContractError(
                        "started provider aggregate budget has no durable run identity"
                    )
                self._load_state_unlocked()
                if run_id and self._loaded_run_id != run_id:
                    raise ProviderRuntimeContractError(
                        "provider aggregate budget state run ID does not match the active run"
                    )
                if identity is None:
                    self._persist_state_identity_unlocked()
                self._state_started = True
                self._reconcile_dead_reservations_unlocked()
                receipt_ledgers = tuple(
                    str(reservation.context.get("ledger_path") or "").strip()
                    for reservation in self._reservations.values()
                    if isinstance(reservation.context, Mapping)
                    and str(reservation.context.get("ledger_path") or "").strip()
                )
                if receipt_ledgers:
                    self._reconcile_orphaned_unlocked(receipt_ledgers)
                self._persist_state_unlocked()

    def _read_state_identity_unlocked(
        self,
        *,
        validate_expected_run: bool = True,
    ) -> Mapping[str, Any] | None:
        if self._identity_path is None or not self._identity_path.is_file():
            return None
        try:
            payload = json.loads(self._identity_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderRuntimeContractError(
                "provider aggregate budget identity is unreadable"
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != "provider-aggregate-budget-identity-v1"
            or str(payload.get("state_path") or "") != str(self._state_path)
            or not str(payload.get("created_at") or "").strip()
        ):
            raise ProviderRuntimeContractError("provider aggregate budget identity is invalid")
        stored_run_id = str(payload.get("acceptance_run_id") or "")
        if validate_expected_run and self._acceptance_run_id and stored_run_id != self._acceptance_run_id:
            raise ProviderRuntimeContractError(
                "provider aggregate budget identity belongs to a different acceptance run"
            )
        if validate_expected_run and not self._acceptance_run_id and stored_run_id:
            self._acceptance_run_id = stored_run_id
        return payload

    def _state_payload_is_pristine_unlocked(self) -> bool:
        if self._state_path is None or not self._state_path.is_file():
            return False
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if (
            not isinstance(payload, Mapping)
            or payload.get("budget") != self.budget.to_dict()
            or str(payload.get("acceptance_run_id") or "")
        ):
            return False
        if any(
            payload.get(name, 0) != 0
            for name in (
                "calls_used",
                "output_tokens_used",
                "retry_attempts_used",
                "calls_reserved",
                "output_tokens_reserved",
                "retry_attempts_reserved",
            )
        ):
            return False
        reservations = payload.get("reservations", [])
        return reservations in (None, [])

    def _adopt_pristine_state_run_id_unlocked(self, run_id: str) -> None:
        if self._state_path is None or not self._state_payload_is_pristine_unlocked():
            raise ProviderRuntimeContractError(
                "provider aggregate budget run identity cannot change after use"
            )
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        payload["schema_version"] = self._state_schema_version
        payload["acceptance_run_id"] = run_id
        atomic_write_json(str(self._state_path), payload)
        self._acceptance_run_id = run_id
        self._persist_state_identity_unlocked()

    def _persist_state_identity_unlocked(self) -> None:
        if self._identity_path is None or self._state_path is None:
            return
        atomic_write_json(
            str(self._identity_path),
            {
                "schema_version": "provider-aggregate-budget-identity-v1",
                "state_path": str(self._state_path),
                "acceptance_run_id": self._acceptance_run_id,
                "created_at": utc_now_iso(),
            },
        )

    def assert_state_integrity(
        self,
        *,
        acceptance_run_id: str,
        state_started: bool,
    ) -> None:
        """Verify the expected run-owned aggregate state still exists and matches."""

        if not state_started:
            return
        run_id = str(acceptance_run_id or "").strip()
        with self._lock:
            if self._state_path is None or self._identity_path is None:
                raise ProviderRuntimeContractError(
                    "started provider aggregate budget has no bound state path"
                )
            if not self._state_started:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget state has not been initialized"
                )
            if self._acceptance_run_id != run_id:
                raise ProviderRuntimeContractError(
                    "provider aggregate budget does not match the active acceptance run"
                )
            with interprocess_file_lock(self._state_path):
                identity = self._read_state_identity_unlocked()
                if identity is None:
                    raise ProviderRuntimeContractError(
                        "started provider aggregate budget has no durable run identity"
                    )
                self._load_state_unlocked()
                if self._loaded_run_id != run_id:
                    raise ProviderRuntimeContractError(
                        "provider aggregate budget state run ID does not match the active run"
                    )

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    @property
    def durable_deadline_epoch(self) -> float | None:
        """Read the existing durable deadline without starting a new clock."""

        with self._lock:
            deadline = float(self._absolute_deadline_epoch or 0.0)
            return deadline if deadline > 0 else None

    @property
    def acceptance_run_id(self) -> str:
        return self._acceptance_run_id

    @property
    def state_started(self) -> bool:
        return self._state_started

    @property
    def _state_schema_version(self) -> str:
        return (
            "provider-aggregate-budget-v5"
            if isinstance(self.budget, ProviderAggregateBudgetV2)
            else "provider-aggregate-budget-v4"
        )

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        return is_process_alive(process_identity_for_pid(pid))

    def _load_state_unlocked(self) -> None:
        if self._state_path is None:
            return
        if not self._state_path.is_file():
            if self._state_started or (
                self._identity_path is not None and self._identity_path.exists()
            ):
                raise ProviderRuntimeContractError(
                    "provider aggregate budget state is missing for a started run"
                )
            return
        identity = self._read_state_identity_unlocked()
        if self._state_started and identity is None:
            raise ProviderRuntimeContractError(
                "started provider aggregate budget has no durable run identity"
            )
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
            "provider-aggregate-budget-v4",
            "provider-aggregate-budget-v5",
            "provider-aggregate-budget-v6",
        }:
            raise ProviderRuntimeContractError("provider aggregate budget state schema is invalid")
        strict_budget = isinstance(self.budget, ProviderAggregateBudgetV2)
        strict_state_schema = state_schema == "provider-aggregate-budget-v5" or (
            state_schema == "provider-aggregate-budget-v6"
            and isinstance(payload.get("budget"), Mapping)
            and payload.get("budget", {}).get("schema_version")
            == ProviderAggregateBudgetV2.SCHEMA_VERSION
        )
        if strict_budget != strict_state_schema:
            raise ProviderRuntimeContractError(
                "provider aggregate budget state semantics do not match the active run"
            )
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
        self._loaded_run_id = str(payload.get("acceptance_run_id") or "")
        if self._acceptance_run_id and self._loaded_run_id != self._acceptance_run_id:
            raise ProviderRuntimeContractError(
                "provider aggregate budget state run ID does not match the active run"
            )
        if identity is not None and str(identity.get("acceptance_run_id") or "") != self._loaded_run_id:
            raise ProviderRuntimeContractError(
                "provider aggregate budget state and identity run IDs differ"
            )
        if not self._acceptance_run_id and self._loaded_run_id:
            self._acceptance_run_id = self._loaded_run_id
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
        if (
            raw_started is None
            or raw_deadline is None
            or isinstance(raw_started, bool)
            or isinstance(raw_deadline, bool)
        ):
            raise ProviderRuntimeContractError(
                "provider aggregate budget start/deadline state is missing or invalid"
            )
        try:
            first_started_epoch = float(raw_started)
            absolute_deadline_epoch = float(raw_deadline)
        except (TypeError, ValueError) as exc:
            raise ProviderRuntimeContractError(
                "provider aggregate budget start/deadline state is invalid"
            ) from exc
        if (
            not math.isfinite(first_started_epoch)
            or first_started_epoch <= 0
            or not math.isfinite(absolute_deadline_epoch)
            or absolute_deadline_epoch < 0
        ):
            raise ProviderRuntimeContractError(
                "provider aggregate budget start/deadline state must be finite and non-negative"
            )
        if self.budget.max_wall_seconds or strict_budget:
            expected_deadline = first_started_epoch + self.budget.max_wall_seconds
            if absolute_deadline_epoch <= 0 or not math.isclose(
                absolute_deadline_epoch,
                expected_deadline,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                raise ProviderRuntimeContractError(
                    "provider aggregate budget absolute deadline is missing or inconsistent"
                )
        elif absolute_deadline_epoch != 0:
            raise ProviderRuntimeContractError(
                "provider aggregate budget deadline is inconsistent with its wall-clock limit"
            )
        self._first_started_epoch = first_started_epoch
        self._absolute_deadline_epoch = absolute_deadline_epoch
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
        raw_primary_scopes = payload.get("primary_repair_scopes", {})
        if not isinstance(raw_primary_scopes, Mapping):
            raise ProviderRuntimeContractError(
                "provider aggregate primary repair scope state is invalid"
            )
        primary_scopes: dict[str, dict[str, Any]] = {}
        for raw_scope_id, raw_scope_record in raw_primary_scopes.items():
            if not isinstance(raw_scope_record, Mapping):
                raise ProviderRuntimeContractError(
                    "provider aggregate primary repair scope record is invalid"
                )
            try:
                scope = PrimaryRepairScopeV1.from_dict(raw_scope_record.get("scope") or {})
            except PrimaryRepairRecoveryError as exc:
                raise ProviderRuntimeContractError(
                    "provider aggregate primary repair scope identity is invalid"
                ) from exc
            scope_id = str(raw_scope_id or "")
            status = str(raw_scope_record.get("status") or "")
            reservation_id = str(raw_scope_record.get("reservation_id") or "")
            if scope_id != scope.scope_id or status not in {
                "reserved",
                "transport_started",
                "raw_retained",
                "raw_semantically_complete",
                "ambiguous_raw_response",
                "response_retained",
                "completed",
                "ambiguous",
            }:
                raise ProviderRuntimeContractError(
                    "provider aggregate primary repair scope status or identity is invalid"
                )
            response_ref_payload = raw_scope_record.get("response_ref")
            response_ref: dict[str, Any] | None = None
            if response_ref_payload is not None:
                try:
                    response_ref = PrimaryRepairResponseRefV1.from_dict(response_ref_payload).to_dict()
                except (PrimaryRepairRecoveryError, TypeError) as exc:
                    raise ProviderRuntimeContractError(
                        "provider aggregate primary repair response reference is invalid"
                    ) from exc
                if (
                    response_ref["scope_id"] != scope.scope_id
                    or response_ref["scope_fingerprint_sha256"] != scope.fingerprint_sha256
                    or response_ref["request_hash"] != scope.request_hash
                    or response_ref["reservation_id"] != reservation_id
                ):
                    raise ProviderRuntimeContractError(
                        "provider aggregate primary repair response reference binding is invalid"
                    )
            default_started = 1 if status in {
                "transport_started",
                "raw_retained",
                "raw_semantically_complete",
                "ambiguous_raw_response",
                "response_retained",
                "completed",
                "ambiguous",
            } else 0
            physical_attempts_started = raw_scope_record.get(
                "physical_attempts_started", default_started
            )
            raw_attempt_number = raw_scope_record.get(
                "raw_attempt_number",
                1 if status in {
                    "raw_retained",
                    "raw_semantically_complete",
                    "ambiguous_raw_response",
                    "response_retained",
                    "completed",
                    "ambiguous",
                } else 0,
            )
            if (
                isinstance(physical_attempts_started, bool)
                or not isinstance(physical_attempts_started, int)
                or physical_attempts_started < 0
                or isinstance(raw_attempt_number, bool)
                or not isinstance(raw_attempt_number, int)
                or raw_attempt_number < 0
                or raw_attempt_number > physical_attempts_started
            ):
                raise ProviderRuntimeContractError(
                    "provider aggregate primary repair physical attempt state is invalid"
                )
            raw_http_status = raw_scope_record.get("raw_http_status")
            if raw_http_status is not None and (
                isinstance(raw_http_status, bool)
                or not isinstance(raw_http_status, int)
                or raw_http_status < 100
            ):
                raise ProviderRuntimeContractError(
                    "provider aggregate PRIMARY raw response HTTP status is invalid"
                )
            raw_body_complete = raw_scope_record.get("raw_body_complete", False)
            if not isinstance(raw_body_complete, bool):
                raise ProviderRuntimeContractError(
                    "provider aggregate PRIMARY raw response completeness flag is invalid"
                )
            if status in {
                "reserved",
                "transport_started",
                "raw_retained",
                "raw_semantically_complete",
                "ambiguous_raw_response",
                "response_retained",
            }:
                reservation = reservations.get(reservation_id)
                if reservation is None:
                    raise ProviderRuntimeContractError(
                        "active primary repair scope has no aggregate reservation"
                    )
                reservation_scope_payload = reservation.context.get("primary_repair_scope")
                try:
                    reservation_scope = PrimaryRepairScopeV1.from_dict(
                        reservation_scope_payload or {}
                    )
                except PrimaryRepairRecoveryError as exc:
                    raise ProviderRuntimeContractError(
                        "primary repair reservation scope binding is invalid"
                    ) from exc
                if (
                    reservation_scope.scope_id != scope.scope_id
                    or reservation_scope.fingerprint_sha256 != scope.fingerprint_sha256
                ):
                    raise ProviderRuntimeContractError(
                        "primary repair reservation differs from its scope record"
                    )
                if status in {
                    "raw_retained",
                    "raw_semantically_complete",
                    "ambiguous_raw_response",
                    "response_retained",
                } and response_ref is None:
                    raise ProviderRuntimeContractError(
                        "retained primary repair response has no journal reference"
                    )
            elif status == "completed" and not response_ref:
                raise ProviderRuntimeContractError(
                    "completed primary repair scope has no durable response reference"
                )
            primary_scopes[scope_id] = {
                "scope": scope.to_dict(),
                "reservation_id": reservation_id,
                "status": status,
                "response_ref": response_ref,
                "physical_attempts_started": physical_attempts_started,
                "raw_attempt_number": raw_attempt_number,
                "raw_http_status": raw_http_status,
                "raw_body_complete": raw_body_complete,
            }
        if state_schema == "provider-aggregate-budget-v6":
            for reservation in reservations.values():
                raw_scope = reservation.context.get("primary_repair_scope")
                if raw_scope is None:
                    continue
                try:
                    scope = PrimaryRepairScopeV1.from_dict(raw_scope)
                except PrimaryRepairRecoveryError as exc:
                    raise ProviderRuntimeContractError(
                        "primary repair reservation scope is invalid"
                    ) from exc
                if scope.scope_id not in primary_scopes:
                    raise ProviderRuntimeContractError(
                        "primary repair reservation is missing its atomic scope record"
                    )
        self._primary_repair_scopes = primary_scopes
        self._ambiguous_reservation_ids = set()
        self._ambiguous_reservation_ids.update(
            str(record.get("reservation_id") or "")
            for record in primary_scopes.values()
            if record.get("status")
            in {
                "raw_semantically_complete",
                "ambiguous_raw_response",
                "ambiguous",
            }
            and str(record.get("reservation_id") or "")
        )
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
            if (strict_budget or limit) and used + reserved > limit:
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

    def _scope_record_for_reservation_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
    ) -> tuple[PrimaryRepairScopeV1, dict[str, Any]] | None:
        raw_scope = reservation.context.get("primary_repair_scope")
        if raw_scope is None:
            return None
        try:
            scope = PrimaryRepairScopeV1.from_dict(raw_scope)
        except PrimaryRepairRecoveryError as exc:
            raise ProviderRuntimeContractError(
                "primary repair reservation scope is invalid"
            ) from exc
        record = self._primary_repair_scopes.get(scope.scope_id)
        if (
            record is None
            or str(record.get("reservation_id") or "") != reservation.reservation_id
        ):
            raise ProviderRuntimeContractError(
                "primary repair scope reservation record is missing or stale"
            )
        try:
            stored_scope = PrimaryRepairScopeV1.from_dict(record.get("scope") or {})
        except PrimaryRepairRecoveryError as exc:
            raise ProviderRuntimeContractError(
                "primary repair scope reservation record is invalid"
            ) from exc
        if stored_scope.fingerprint_sha256 != scope.fingerprint_sha256:
            raise ProviderRuntimeContractError(
                "primary repair scope reservation fingerprint changed"
            )
        return scope, record

    def _drop_scope_for_reservation_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
    ) -> None:
        matched = self._scope_record_for_reservation_unlocked(reservation)
        if matched is not None:
            scope, _record = matched
            self._primary_repair_scopes.pop(scope.scope_id, None)

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
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is not None:
                self._drop_scope_for_reservation_unlocked(reservation)
        self._recompute_reserved_unlocked()

    def _persist_state_unlocked(self) -> None:
        if self._state_path is None:
            return
        atomic_write_json(
            str(self._state_path),
            {
                "schema_version": self._state_schema_version,
                "budget": self.budget.to_dict(),
                "acceptance_run_id": self._acceptance_run_id,
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
                "primary_repair_scopes": {
                    scope_id: dict(scope_record)
                    for scope_id, scope_record in sorted(self._primary_repair_scopes.items())
                },
            },
        )

    def _check_wall(self) -> None:
        strict_budget = isinstance(self.budget, ProviderAggregateBudgetV2)
        if (self.budget.max_wall_seconds or strict_budget) and self._absolute_deadline_epoch:
            if float(time.time()) >= self._absolute_deadline_epoch:
                raise ProviderBudgetExceeded("aggregate provider wall-clock budget exhausted")
        elif (self.budget.max_wall_seconds or strict_budget) and (
            float(self._monotonic()) - self._started_monotonic
        ) >= self.budget.max_wall_seconds:
            raise ProviderBudgetExceeded("aggregate provider wall-clock budget exhausted")

    @staticmethod
    def _coerce_primary_repair_scope(
        value: PrimaryRepairScopeV1 | Mapping[str, Any] | None,
    ) -> PrimaryRepairScopeV1 | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, PrimaryRepairScopeV1) else PrimaryRepairScopeV1.from_dict(value)
        except (PrimaryRepairRecoveryError, TypeError) as exc:
            raise ProviderRuntimeContractError("PRIMARY repair scope is invalid") from exc

    def admit(
        self,
        *,
        requested_output_tokens: int = 0,
        requested_retry_attempts: int = 0,
        context: Mapping[str, Any] | None = None,
        primary_repair_scope: PrimaryRepairScopeV1 | Mapping[str, Any] | None = None,
    ) -> ProviderAggregateReservationV1:
        output_tokens = max(0, int(requested_output_tokens))
        retry_attempts = max(0, int(requested_retry_attempts))
        provider_calls = 1 + retry_attempts
        scope = self._coerce_primary_repair_scope(primary_repair_scope)
        if scope is not None and self._state_path is None:
            raise ProviderRuntimeContractError(
                "PRIMARY repair admission requires a bound durable aggregate budget state"
            )
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._admit_unlocked(
                    provider_calls=provider_calls,
                    output_tokens=output_tokens,
                    retry_attempts=retry_attempts,
                    context=context,
                    primary_repair_scope=scope,
                )
            with lock:
                self._load_state_unlocked()
                self._reconcile_dead_reservations_unlocked()
                return self._admit_unlocked(
                    provider_calls=provider_calls,
                    output_tokens=output_tokens,
                    retry_attempts=retry_attempts,
                    context=context,
                    primary_repair_scope=scope,
                )

    def _admit_unlocked(
        self,
        *,
        provider_calls: int,
        output_tokens: int,
        retry_attempts: int,
        context: Mapping[str, Any] | None,
        primary_repair_scope: PrimaryRepairScopeV1 | None = None,
    ) -> ProviderAggregateReservationV1:
        continuation = getattr(self, "_verified_downstream_continuation", None)
        if continuation is not None:
            from runtime.downstream_continuation import validate_continuation_controller

            validate_continuation_controller(continuation, self)
            if (
                not isinstance(context, Mapping)
                or context.get("job_id") != continuation.job_id
                or context.get("stage_name") not in continuation.allow_provider_stages
            ):
                raise ProviderRuntimeContractError(
                    "downstream continuation admits only this job's Writer and Validator stages"
                )
        self._check_wall()
        if self._ambiguous_reservation_ids:
            raise ProviderBudgetExceeded(
                "provider aggregate budget has ambiguous reservations requiring durable receipt reconciliation"
            )
        if primary_repair_scope is not None:
            existing_scope = self._primary_repair_scopes.get(primary_repair_scope.scope_id)
            if existing_scope is not None:
                try:
                    prior_scope = PrimaryRepairScopeV1.from_dict(existing_scope.get("scope") or {})
                except PrimaryRepairRecoveryError as exc:
                    raise ProviderRuntimeContractError(
                        "existing PRIMARY repair scope state is invalid"
                    ) from exc
                if prior_scope.fingerprint_sha256 != primary_repair_scope.fingerprint_sha256:
                    raise ProviderRuntimeContractError(
                        "PRIMARY repair scope materialization changed; reconcile the prior attempt"
                    )
                raise ProviderBudgetExceeded(
                    "PRIMARY repair scope is already reserved or attempted; recover its response before another POST"
                )
        strict_budget = isinstance(self.budget, ProviderAggregateBudgetV2)
        if (
                (strict_budget or self.budget.max_provider_calls_total)
                and self._calls_used + self._calls_reserved + provider_calls
                > self.budget.max_provider_calls_total
        ):
            raise ProviderBudgetExceeded("aggregate provider call budget exhausted")
        if (
                (strict_budget or self.budget.max_output_tokens_total)
                and self._output_tokens_used + self._output_tokens_reserved + output_tokens
                > self.budget.max_output_tokens_total
        ):
            raise ProviderBudgetExceeded("aggregate provider output-token budget exhausted")
        if (
                (strict_budget or self.budget.max_retry_attempts_total)
                and self._retry_attempts_used + self._retry_attempts_reserved + retry_attempts
                > self.budget.max_retry_attempts_total
        ):
            raise ProviderBudgetExceeded("aggregate provider retry budget exhausted")
        reservation_context = dict(context or {})
        if primary_repair_scope is not None:
            reservation_context["primary_repair_scope"] = primary_repair_scope.to_dict()
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
                context=reservation_context,
        )
        self._reservations[reservation.reservation_id] = reservation
        if primary_repair_scope is not None:
            self._primary_repair_scopes[primary_repair_scope.scope_id] = {
                "scope": primary_repair_scope.to_dict(),
                "reservation_id": reservation.reservation_id,
                "status": "reserved",
                "response_ref": None,
                "physical_attempts_started": 0,
                "raw_attempt_number": 0,
                "raw_http_status": None,
                "raw_body_complete": False,
            }
        self._calls_reserved += provider_calls
        self._output_tokens_reserved += output_tokens
        self._retry_attempts_reserved += retry_attempts
        try:
            self._persist_state_unlocked()
        except BaseException:
            del self._reservations[reservation.reservation_id]
            if primary_repair_scope is not None:
                self._primary_repair_scopes.pop(primary_repair_scope.scope_id, None)
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
        matched = self._scope_record_for_reservation_unlocked(current)
        if matched is not None:
            _scope, scope_record = matched
            if scope_record.get("status") == "transport_started":
                return
            if scope_record.get("status") != "reserved":
                raise ProviderRuntimeContractError(
                    "PRIMARY repair scope is not in its reserved state before transport"
                )
            scope_record["status"] = "transport_started"
            scope_record["physical_attempts_started"] = 1
            scope_record["raw_attempt_number"] = 0
            scope_record["raw_http_status"] = None
            scope_record["raw_body_complete"] = False
        self._persist_state_unlocked()

    def mark_primary_repair_attempt_started(
        self,
        reservation: ProviderAggregateReservationV1,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
        attempt_number: int,
    ) -> int:
        """Durably bind each physical PRIMARY POST before its transport hand-off."""

        normalized_scope = self._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise ProviderRuntimeContractError("PRIMARY physical attempt number must be positive")
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._mark_primary_repair_attempt_started_unlocked(
                    reservation, normalized_scope, attempt_number
                )
            with lock:
                self._load_state_unlocked()
                return self._mark_primary_repair_attempt_started_unlocked(
                    reservation, normalized_scope, attempt_number
                )

    def _mark_primary_repair_attempt_started_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
        scope: PrimaryRepairScopeV1,
        attempt_number: int,
    ) -> int:
        current = self._reservations.get(reservation.reservation_id)
        if current is None:
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempt has no aggregate reservation"
            )
        if attempt_number > current.provider_calls:
            raise ProviderBudgetExceeded(
                "PRIMARY physical attempt exceeds its pre-admitted call allowance"
            )
        matched = self._scope_record_for_reservation_unlocked(current)
        if matched is None or matched[0].fingerprint_sha256 != scope.fingerprint_sha256:
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempt does not match its atomic scope reservation"
            )
        _stored_scope, record = matched
        started = int(record.get("physical_attempts_started") or 0)
        if started == 0:
            if (
                attempt_number != 1
                or record.get("status") != "reserved"
                or current.transport_started
            ):
                raise ProviderRuntimeContractError(
                    "PRIMARY first POST boundary differs from its reserved scope"
                )
            # Check the durable global wall cap at the actual pre-POST
            # boundary, after request materialization and immediately before
            # the adapter hands the request to the network.
            self._check_wall()
            self._reservations[current.reservation_id] = replace(
                current,
                transport_started=True,
            )
            record["status"] = "transport_started"
            record["physical_attempts_started"] = 1
            record["raw_attempt_number"] = 0
            record["raw_http_status"] = None
            record["raw_body_complete"] = False
            self._persist_state_unlocked()
            return 1
        if not current.transport_started:
            raise ProviderRuntimeContractError(
                "PRIMARY retry has no started aggregate reservation"
            )
        if (
            attempt_number == 1
            and started == 1
            and int(record.get("raw_attempt_number") or 0) == 0
            and record.get("status") == "transport_started"
        ):
            return started  # Attempt 1 was pre-marked by mark_transport_started.
        if attempt_number == started:
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempt was already marked before this POST"
            )
        if attempt_number != started + 1:
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempts must start once, in order"
            )
        raw_attempt = int(record.get("raw_attempt_number") or 0)
        if raw_attempt != started:
            raise ProviderRuntimeContractError(
                "PRIMARY retry is blocked because the preceding provider outcome is unknown"
            )
        if started:
            if not bool(record.get("raw_body_complete")):
                raise ProviderRuntimeContractError(
                    "PRIMARY retry is blocked after an incomplete provider response"
                )
            if record.get("raw_http_status") == 200:
                raise ProviderRuntimeContractError(
                    "PRIMARY retry is blocked after a complete HTTP 200 response"
                )
        if record.get("status") not in {
            "transport_started",
            "raw_retained",
            "ambiguous_raw_response",
        }:
            raise ProviderRuntimeContractError(
                "PRIMARY scope cannot begin another physical attempt from its current state"
            )
        record["physical_attempts_started"] = attempt_number
        record["status"] = "transport_started"
        self._ambiguous_reservation_ids.discard(current.reservation_id)
        self._persist_state_unlocked()
        return attempt_number

    def retain_primary_repair_raw_response(
        self,
        reservation: ProviderAggregateReservationV1,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
        response_ref: PrimaryRepairResponseRefV1 | Mapping[str, Any],
        *,
        journal: PrimaryRepairResponseJournal,
    ) -> str:
        """Anchor a durable raw spool before the HTTP adapter returns."""

        normalized_scope = self._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        try:
            normalized_ref = (
                response_ref
                if isinstance(response_ref, PrimaryRepairResponseRefV1)
                else PrimaryRepairResponseRefV1.from_dict(response_ref)
            )
            entry = journal.load_entry_for_scope(
                normalized_scope,
                reservation_id=reservation.reservation_id,
            )
        except (PrimaryRepairRecoveryError, TypeError) as exc:
            raise ProviderRuntimeContractError(
                "raw PRIMARY response reference could not be verified"
            ) from exc
        if entry is None or entry.entry_kind not in {
            "raw_response",
            "semantic_raw_response",
            "ambiguous_raw_response",
        } or entry.reference != normalized_ref or entry.raw_spool is None:
            raise ProviderRuntimeContractError(
                "raw PRIMARY response reference does not identify a retained spool"
            )
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._retain_primary_repair_raw_response_unlocked(
                    reservation,
                    normalized_scope,
                    entry,
                )
            with lock:
                self._load_state_unlocked()
                return self._retain_primary_repair_raw_response_unlocked(
                    reservation,
                    normalized_scope,
                    entry,
                )

    def _retain_primary_repair_raw_response_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
        scope: PrimaryRepairScopeV1,
        entry: PrimaryRepairResponseEntryV1,
    ) -> str:
        current = self._reservations.get(reservation.reservation_id)
        if current is None or not current.transport_started:
            raise ProviderRuntimeContractError(
                "raw PRIMARY response has no started aggregate reservation"
            )
        matched = self._scope_record_for_reservation_unlocked(current)
        if matched is None or matched[0].fingerprint_sha256 != scope.fingerprint_sha256:
            raise ProviderRuntimeContractError(
                "raw PRIMARY response does not match its atomic scope reservation"
            )
        _scope, scope_record = matched
        if (
            entry.raw_spool is None
            or entry.raw_spool.attempt_number
            != int(scope_record.get("physical_attempts_started") or 0)
        ):
            raise ProviderRuntimeContractError(
                "raw PRIMARY response does not match the latest started physical attempt"
            )
        if scope_record.get("status") in {
            "raw_retained",
            "raw_semantically_complete",
            "ambiguous_raw_response",
        }:
            if scope_record.get("response_ref") != entry.reference.to_dict():
                raise ProviderRuntimeContractError(
                    "PRIMARY response scope already retains a different raw spool"
                )
            return str(scope_record.get("status"))
        if scope_record.get("status") != "transport_started":
            if scope_record.get("status") in {"response_retained", "completed"}:
                return str(scope_record.get("status"))
            raise ProviderRuntimeContractError(
                "PRIMARY response scope is not transport-started"
            )
        scope_record["status"] = (
            "raw_retained"
            if entry.entry_kind == "raw_response"
            else "raw_semantically_complete"
            if entry.entry_kind == "semantic_raw_response"
            else "ambiguous_raw_response"
        )
        scope_record["response_ref"] = entry.reference.to_dict()
        scope_record["raw_attempt_number"] = entry.raw_spool.attempt_number
        scope_record["raw_http_status"] = entry.raw_spool.http_status
        scope_record["raw_body_complete"] = entry.raw_spool.complete
        if entry.entry_kind in {
            "semantic_raw_response",
            "ambiguous_raw_response",
        }:
            self._ambiguous_reservation_ids.add(current.reservation_id)
        self._persist_state_unlocked()
        return entry.entry_kind

    def release_unstarted(
        self,
        reservation: ProviderAggregateReservationV1,
    ) -> bool:
        """Release an admission only while its transport-start marker is clear."""

        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._release_unstarted_unlocked(reservation)
            with lock:
                self._load_state_unlocked()
                return self._release_unstarted_unlocked(reservation)

    def _release_unstarted_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
    ) -> bool:
        current = self._reservations.get(reservation.reservation_id)
        if current is None:
            return False
        if current.transport_started:
            raise ProviderRuntimeContractError(
                "provider reservation cannot be released after transport started"
            )
        self._drop_scope_for_reservation_unlocked(current)
        self._reservations.pop(current.reservation_id, None)
        self._recompute_reserved_unlocked()
        self._persist_state_unlocked()
        return True

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
                matched_scope = self._scope_record_for_reservation_unlocked(reservation)
                if matched_scope is not None:
                    scope, scope_record = matched_scope
                    metadata = receipt.get("metadata")
                    raw_ref = (
                        metadata.get("primary_repair_response_ref")
                        if isinstance(metadata, Mapping)
                        else None
                    )
                    try:
                        response_ref = (
                            None
                            if raw_ref is None
                            else PrimaryRepairResponseRefV1.from_dict(raw_ref)
                        )
                    except (PrimaryRepairRecoveryError, TypeError):
                        response_ref = None
                    if response_ref is not None and (
                        response_ref.scope_id == scope.scope_id
                        and response_ref.scope_fingerprint_sha256 == scope.fingerprint_sha256
                        and response_ref.request_hash == scope.request_hash
                        and response_ref.reservation_id == reservation_id
                    ):
                        scope_record["status"] = "completed"
                        scope_record["response_ref"] = response_ref.to_dict()
                    else:
                        scope_record["status"] = "ambiguous"
                        scope_record["response_ref"] = None
                self._reservations.pop(reservation_id, None)
                self._calls_used += attempts
                self._output_tokens_used += actual_output
                self._retry_attempts_used += max(0, attempts - 1)
                self._ambiguous_reservation_ids.discard(reservation_id)
                recovered.append(reservation_id)
            elif reservation.transport_started:
                journal_entry = self._journaled_primary_repair_response_unlocked(
                    reservation
                )
                if journal_entry is not None:
                    matched_scope = self._scope_record_for_reservation_unlocked(reservation)
                    if matched_scope is None:
                        raise ProviderRuntimeContractError(
                            "journaled PRIMARY response has no atomic scope reservation"
                        )
                    scope, scope_record = matched_scope
                    reference = journal_entry.reference
                    receipt_binding = journal_entry.receipt_binding
                    if (
                        receipt_binding.get("input_hash") != scope.request_hash
                        or receipt_binding.get("job_id") != scope.job_id
                        or receipt_binding.get("logical_attempt_identity")
                        != scope.logical_attempt_identity
                        or receipt_binding.get("call_id")
                        != str(reservation.context.get("call_id") or "")
                    ):
                        raise ProviderRuntimeContractError(
                            "journaled PRIMARY response receipt binding differs from its reservation"
                        )
                    physical_attempts = int(
                        scope_record.get("physical_attempts_started") or 1
                    )
                    raw_spool = journal_entry.raw_spool
                    if raw_spool is not None:
                        scope_record["raw_attempt_number"] = raw_spool.attempt_number
                        scope_record["raw_http_status"] = raw_spool.http_status
                        scope_record["raw_body_complete"] = raw_spool.complete
                    if (
                        raw_spool is not None
                        and raw_spool.attempt_number != physical_attempts
                    ):
                        # A later POST boundary without a retained body makes
                        # this response stale. Never recover an earlier retry
                        # response while a later physical attempt is unknown.
                        scope_record["status"] = "ambiguous"
                        scope_record["response_ref"] = reference.to_dict()
                        ambiguous.append(reservation_id)
                    elif journal_entry.entry_kind == "raw_response":
                        scope_record["status"] = "raw_retained"
                    elif journal_entry.entry_kind == "semantic_raw_response":
                        scope_record["status"] = "raw_semantically_complete"
                    elif journal_entry.entry_kind == "ambiguous_raw_response":
                        scope_record["status"] = "ambiguous_raw_response"
                        ambiguous.append(reservation_id)
                    elif journal_entry.entry_kind == "normalized_response":
                        scope_record["status"] = "response_retained"
                    else:
                        raise ProviderRuntimeContractError(
                            "journaled PRIMARY response has an unsupported entry kind"
                        )
                    scope_record["response_ref"] = reference.to_dict()
                    self._ambiguous_reservation_ids.add(reservation_id)
                else:
                    self._ambiguous_reservation_ids.add(reservation_id)
                    ambiguous.append(reservation_id)
            else:
                self._drop_scope_for_reservation_unlocked(reservation)
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

    def _journaled_primary_repair_response_unlocked(
        self,
        reservation: ProviderAggregateReservationV1,
    ) -> PrimaryRepairResponseEntryV1 | None:
        scope_payload = reservation.context.get("primary_repair_scope")
        journal_root = str(
            reservation.context.get("primary_repair_response_journal_root") or ""
        ).strip()
        if not isinstance(scope_payload, Mapping) or not journal_root:
            return None
        try:
            scope = PrimaryRepairScopeV1.from_dict(scope_payload)
            journal = PrimaryRepairResponseJournal(journal_root)
            entry = journal.load_entry_for_scope(
                scope,
                reservation_id=reservation.reservation_id,
            )
            if entry is None:
                scope_record = self._primary_repair_scopes.get(scope.scope_id)
                attempt_number = int(
                    (scope_record or {}).get("physical_attempts_started") or 0
                )
                if attempt_number > 0:
                    journal.recover_orphaned_raw_response(
                        scope,
                        reservation.reservation_id,
                        attempt_number,
                    )
                    entry = journal.load_entry_for_scope(
                        scope,
                        reservation_id=reservation.reservation_id,
                    )
        except (PrimaryRepairRecoveryError, OSError, ValueError, TypeError):
            return None
        if entry is None or not entry.receipt_binding:
            return None
        return entry

    def lookup_primary_repair_scopes(self) -> list[dict[str, Any]]:
        """Return durable PRIMARY scope state for local response reconciliation."""

        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return [
                    dict(record)
                    for _scope_id, record in sorted(self._primary_repair_scopes.items())
                ]
            with lock:
                self._load_state_unlocked()
                return [
                    dict(record)
                    for _scope_id, record in sorted(self._primary_repair_scopes.items())
                ]

    def lookup_primary_repair_scope(
        self,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
    ) -> dict[str, Any] | None:
        normalized = self._coerce_primary_repair_scope(scope)
        assert normalized is not None
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                record = self._primary_repair_scopes.get(normalized.scope_id)
            else:
                with lock:
                    self._load_state_unlocked()
                    record = self._primary_repair_scopes.get(normalized.scope_id)
            if record is None:
                return None
            try:
                stored_scope = PrimaryRepairScopeV1.from_dict(record.get("scope") or {})
            except PrimaryRepairRecoveryError as exc:
                raise ProviderRuntimeContractError("stored PRIMARY repair scope is invalid") from exc
            if stored_scope.fingerprint_sha256 != normalized.fingerprint_sha256:
                raise ProviderRuntimeContractError(
                    "stored PRIMARY repair scope has a different materialized binding"
                )
            result = {
                "scope": stored_scope.to_dict(),
                "reservation_id": str(record.get("reservation_id") or ""),
                "status": str(record.get("status") or ""),
                "response_ref": (
                    dict(record["response_ref"])
                    if isinstance(record.get("response_ref"), Mapping)
                    else None
                ),
                "physical_attempts_started": int(
                    record.get("physical_attempts_started") or 0
                ),
                "raw_attempt_number": int(record.get("raw_attempt_number") or 0),
                "raw_http_status": record.get("raw_http_status"),
                "raw_body_complete": bool(record.get("raw_body_complete")),
            }
            reservation = self._reservations.get(result["reservation_id"])
            if reservation is not None:
                result["response_journal_root"] = str(
                    reservation.context.get("primary_repair_response_journal_root") or ""
                )
            return result

    def recover_primary_repair_response(
        self,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
        response_ref: PrimaryRepairResponseRefV1 | Mapping[str, Any],
        response: Mapping[str, Any],
        *,
        journal: PrimaryRepairResponseJournal,
    ) -> dict[str, Any]:
        """Account a journaled response after its transport owner has died.

        This path converts an already-reserved, already-started call into used
        budget.  It never admits or sends a replacement request.
        """

        normalized_scope = self._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        try:
            normalized_ref = (
                response_ref
                if isinstance(response_ref, PrimaryRepairResponseRefV1)
                else PrimaryRepairResponseRefV1.from_dict(response_ref)
            )
            journal_response = journal.load(
                normalized_ref,
                scope=normalized_scope,
                reservation_id=normalized_ref.reservation_id,
            )
        except (PrimaryRepairRecoveryError, TypeError) as exc:
            raise ProviderRuntimeContractError(
                "journaled PRIMARY repair response could not be verified"
            ) from exc
        if journal_response != dict(response):
            raise ProviderRuntimeContractError(
                "recovered PRIMARY repair response differs from its durable journal"
            )
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._recover_primary_repair_response_unlocked(
                    normalized_scope,
                    normalized_ref,
                    journal_response,
                )
            with lock:
                self._load_state_unlocked()
                return self._recover_primary_repair_response_unlocked(
                    normalized_scope,
                    normalized_ref,
                    journal_response,
                )

    def _recover_primary_repair_response_unlocked(
        self,
        scope: PrimaryRepairScopeV1,
        response_ref: PrimaryRepairResponseRefV1,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self._primary_repair_scopes.get(scope.scope_id)
        if record is None:
            raise ProviderRuntimeContractError("PRIMARY repair scope reservation is missing")
        stored_scope = PrimaryRepairScopeV1.from_dict(record.get("scope") or {})
        if stored_scope.fingerprint_sha256 != scope.fingerprint_sha256:
            raise ProviderRuntimeContractError("PRIMARY repair scope changed during recovery")
        if record.get("status") == "completed":
            if record.get("response_ref") != response_ref.to_dict():
                raise ProviderRuntimeContractError(
                    "PRIMARY repair scope is complete with a different response"
                )
            return self.snapshot()
        reservation = self._reservations.get(response_ref.reservation_id)
        if reservation is None:
            raise ProviderRuntimeContractError(
                "journaled PRIMARY repair response has no pending aggregate reservation"
            )
        if self._reservation_owner_liveness(reservation) != "dead":
            raise ProviderRuntimeContractError(
                "PRIMARY repair response recovery requires its transport owner to be stopped"
            )
        if not reservation.transport_started:
            raise ProviderRuntimeContractError(
                "journaled PRIMARY repair response has no transport-started reservation"
            )
        matched = self._scope_record_for_reservation_unlocked(reservation)
        if matched is None or matched[0].fingerprint_sha256 != scope.fingerprint_sha256:
            raise ProviderRuntimeContractError(
                "journaled PRIMARY repair response does not match its pending scope"
            )
        if (
            response_ref.scope_id != scope.scope_id
            or response_ref.scope_fingerprint_sha256 != scope.fingerprint_sha256
            or response_ref.request_hash != scope.request_hash
            or response_ref.reservation_id != reservation.reservation_id
        ):
            raise ProviderRuntimeContractError(
                "journaled PRIMARY repair response reference is outside the pending scope"
            )
        snapshot = self._complete_unlocked(
            reservation=reservation,
            result=response,
            primary_repair_response_ref=response_ref,
        )
        self._ambiguous_reservation_ids.discard(reservation.reservation_id)
        return snapshot

    def complete(
        self,
        reservation: ProviderAggregateReservationV1,
        result: Mapping[str, Any],
        *,
        primary_repair_response_ref: PrimaryRepairResponseRefV1 | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            lock = interprocess_file_lock(self._state_path) if self._state_path is not None else None
            if lock is None:
                return self._complete_unlocked(
                    reservation=reservation,
                    result=result,
                    primary_repair_response_ref=primary_repair_response_ref,
                )
            with lock:
                self._load_state_unlocked()
                return self._complete_unlocked(
                    reservation=reservation,
                    result=result,
                    primary_repair_response_ref=primary_repair_response_ref,
                )

    def _complete_unlocked(
        self,
        *,
        reservation: ProviderAggregateReservationV1,
        result: Mapping[str, Any],
        primary_repair_response_ref: PrimaryRepairResponseRefV1 | Mapping[str, Any] | None = None,
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
        matched_scope = self._scope_record_for_reservation_unlocked(current)
        if matched_scope is not None:
            scope, scope_record = matched_scope
            if not current.transport_started:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair scope cannot complete before transport started"
                )
            if primary_repair_response_ref is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair completion requires a durable response journal reference"
                )
            try:
                response_ref = (
                    primary_repair_response_ref
                    if isinstance(primary_repair_response_ref, PrimaryRepairResponseRefV1)
                    else PrimaryRepairResponseRefV1.from_dict(primary_repair_response_ref)
                )
            except (PrimaryRepairRecoveryError, TypeError) as exc:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair response journal reference is invalid"
                ) from exc
            if (
                response_ref.scope_id != scope.scope_id
                or response_ref.scope_fingerprint_sha256 != scope.fingerprint_sha256
                or response_ref.request_hash != scope.request_hash
                or response_ref.reservation_id != current.reservation_id
            ):
                raise ProviderRuntimeContractError(
                    "PRIMARY repair response reference does not match its reservation"
                )
            if scope_record.get("status") == "ambiguous_raw_response":
                if str(result.get("status") or "") == "success":
                    raise ProviderRuntimeContractError(
                        "incomplete raw PRIMARY response cannot be settled as success"
                    )
                scope_record["status"] = "ambiguous"
            else:
                scope_record["status"] = "completed"
            scope_record["response_ref"] = response_ref.to_dict()
        self._reservations.pop(reservation.reservation_id, None)
        self._calls_reserved -= current.provider_calls
        self._output_tokens_reserved -= current.output_tokens
        self._retry_attempts_reserved -= current.retry_attempts
        self._calls_used += attempts
        self._output_tokens_used += actual_output
        self._retry_attempts_used += actual_retries
        self._ambiguous_reservation_ids.discard(reservation.reservation_id)
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

    def assert_response_reuse_allowed(self, *, job_id: str, stage_name: str, call_id: str) -> None:
        """Keep a parent cache hit from hiding this child's unknown outcome.

        A started, unsettled physical attempt remains charged/reserved until
        the existing reconciliation workflow resolves it. Independent calls
        may still reuse their own verified responses.
        """
        identity = {"job_id": job_id, "stage_name": stage_name, "call_id": call_id}
        if any(not str(value).strip() for value in identity.values()):
            raise ProviderRuntimeContractError("response reuse requires an exact child call identity")

        def verify() -> None:
            for reservation in self._reservations.values():
                if reservation.transport_started and all(
                    reservation.context.get(key) == value for key, value in identity.items()
                ):
                    raise ProviderRuntimeContractError(
                        "response reuse requires reconciliation of the child's started provider attempt"
                    )

        with self._lock:
            if self._state_path is None:
                verify()
            else:
                with interprocess_file_lock(self._state_path):
                    self._load_state_unlocked()
                    verify()

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
    """Resolve accepted, bound ordinary-run, then legacy environment budgets."""

    global _ENV_BUDGET_RAW, _ENV_BUDGET_CONTROLLER
    active_controller = _ACTIVE_ACCEPTANCE_CONTROLLER.get()
    if active_controller is not None:
        return active_controller
    raw = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "")).strip()
    state_path = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", "")).strip()
    serialized_context_raw = str(
        os.getenv("AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON", "")
    ).strip()
    serialized_context = acceptance_execution_context_from_environment()
    if serialized_context is not None:
        if str(os.getenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", "")).strip() != serialized_context.acceptance_run_id:
            raise ProviderRuntimeContractError(
                "acceptance execution context run ID does not match its environment bridge"
            )
        if state_path != serialized_context.provider_budget_state_path:
            raise ProviderRuntimeContractError(
                "acceptance execution context budget state does not match its environment bridge"
            )
    else:
        # A public run binds its already-persisted controller around all stages.
        # It takes precedence over any unbound compatibility environment
        # budget; only explicit acceptance authority outranks it.
        active_run_controller = _ACTIVE_RUN_PROVIDER_CONTROLLER.get()
        if active_run_controller is not None:
            return active_run_controller
        if not raw:
            with _ENV_BUDGET_LOCK:
                _ENV_BUDGET_RAW = ""
                _ENV_BUDGET_CONTROLLER = None
            return None
    cache_key = raw + "\x00" + state_path + "\x00" + serialized_context_raw
    with _ENV_BUDGET_LOCK:
        if cache_key == _ENV_BUDGET_RAW and _ENV_BUDGET_CONTROLLER is not None:
            return _ENV_BUDGET_CONTROLLER
        if raw:
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
            budget = provider_aggregate_budget_from_mapping(payload)
        elif serialized_context is not None:
            budget = serialized_context.provider_budget
        else:
            return None
        controller = ProviderBudgetController(budget)
        if serialized_context is not None and controller.budget != serialized_context.provider_budget:
            raise ProviderRuntimeContractError(
                "acceptance execution context budget does not match its environment bridge"
            )
        if serialized_context is not None:
            controller.bind_state_path(
                serialized_context.provider_budget_state_path,
                acceptance_run_id=serialized_context.acceptance_run_id,
                state_started=serialized_context.provider_budget_state_started,
            )
        elif state_path:
            controller.bind_state_path(
                state_path,
                acceptance_run_id=str(
                    os.getenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", "")
                ).strip(),
                state_started=None,
            )
        _ENV_BUDGET_RAW = cache_key
        _ENV_BUDGET_CONTROLLER = controller
        return controller


def provider_run_budget_controller_from_environment(
    *,
    runtime_spec_budget: ProviderAggregateBudgetV2 | Mapping[str, Any] | None = None,
) -> ProviderBudgetController | None:
    """Resolve a controller for a new public RuntimeJobSpec run.

    Legacy V1 environment budgets remain readable through
    ``provider_budget_controller_from_environment`` for historical acceptance
    and recovery tools. They are not authority for a newly invoked runtime job.
    In particular, this function inspects an unbound V1 payload before the
    legacy factory can bind or initialize its state file.

    Serialized acceptance contexts are inherited data, not an in-process
    accepted entry. The controlling process must revalidate and bind one with
    ``bind_acceptance_execution_context`` before a V2 accepted continuation
    can proceed. A verified run-scope proof is checked separately after the
    Runner has reopened its Registry envelope.
    """

    explicit_budget = (
        ProviderAggregateBudgetV2.from_mapping(runtime_spec_budget)
        if isinstance(runtime_spec_budget, Mapping)
        else runtime_spec_budget
    )
    if explicit_budget is not None and not isinstance(
        explicit_budget, ProviderAggregateBudgetV2
    ):
        raise ProviderRuntimeContractError(
            "runtime-spec provider budget must be strict finite V2"
        )

    active_acceptance_context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    active_acceptance_controller = _ACTIVE_ACCEPTANCE_CONTROLLER.get()
    active_run_controller = _ACTIVE_RUN_PROVIDER_CONTROLLER.get()
    raw_context = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON", "")).strip()
    raw_budget = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON", "")).strip()
    raw_state_path = str(
        os.getenv("AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH", "")
    ).strip()
    raw_run_id = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", "")).strip()

    if active_acceptance_context is not None or active_acceptance_controller is not None:
        if active_acceptance_context is None or active_acceptance_controller is None:
            raise ProviderRuntimeContractError(
                "accepted runtime entry requires both the typed context and controller"
            )
        if active_acceptance_controller.budget != active_acceptance_context.provider_budget:
            raise ProviderRuntimeContractError(
                "accepted runtime entry context and controller budgets differ"
            )
        if (
            explicit_budget is not None
            and explicit_budget != active_acceptance_controller.budget
        ):
            raise ProviderRuntimeContractError(
                "runtime-spec budget conflicts with the active accepted-run budget"
            )
        if raw_context:
            inherited = acceptance_execution_context_from_environment()
            if inherited != active_acceptance_context:
                raise ProviderRuntimeContractError(
                    "inherited acceptance context conflicts with the active typed entry"
                )
            if (
                raw_state_path
                != active_acceptance_context.provider_budget_state_path
                or raw_run_id != active_acceptance_context.acceptance_run_id
            ):
                raise ProviderRuntimeContractError(
                    "inherited acceptance state identity conflicts with the active typed entry"
                )
            if raw_budget:
                try:
                    raw_budget_payload = json.loads(raw_budget)
                except json.JSONDecodeError as exc:
                    raise ProviderRuntimeContractError(
                        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON is not valid JSON"
                    ) from exc
                if (
                    not isinstance(raw_budget_payload, Mapping)
                    or provider_aggregate_budget_from_mapping(raw_budget_payload)
                    != active_acceptance_context.provider_budget
                ):
                    raise ProviderRuntimeContractError(
                        "inherited acceptance budget conflicts with the active typed entry"
                    )
        if (
            not active_acceptance_context.owner_authorized
            or not active_acceptance_context.provider_budget_state_started
            or active_acceptance_context.absolute_deadline_epoch <= time.time()
            or not active_acceptance_controller.state_started
            or active_acceptance_controller.state_path
            != Path(active_acceptance_context.provider_budget_state_path)
            .expanduser()
            .resolve()
        ):
            raise ProviderRuntimeContractError(
                "accepted runtime entry is not owner-authorized, started, and current"
            )
        active_acceptance_controller.assert_state_integrity(
            acceptance_run_id=active_acceptance_context.acceptance_run_id,
            state_started=True,
        )
        if not isinstance(active_acceptance_controller.budget, ProviderAggregateBudgetV2):
            raise ProviderRuntimeContractError(
                "legacy V1 acceptance budget cannot authorize a new runtime job; "
                "its durable ledger is preserved"
            )
        return active_acceptance_controller

    # Do this before consulting the compatibility factory: its historical
    # behavior may initialize a missing V1 state file, which is inappropriate
    # when rejecting a new public run.
    if raw_context:
        raise ProviderRuntimeContractError(
            "serialized acceptance context is inherited and lacks an in-process "
            "accepted-run scope proof"
        )
    if raw_budget:
        try:
            payload = json.loads(raw_budget)
        except json.JSONDecodeError as exc:
            raise ProviderRuntimeContractError(
                "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderRuntimeContractError(
                "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON must be a JSON object"
            )
        budget = provider_aggregate_budget_from_mapping(payload)
        if not isinstance(budget, ProviderAggregateBudgetV2):
            raise ProviderRuntimeContractError(
                "legacy V1 environment budget cannot authorize a new runtime job; "
                "its durable ledger is preserved"
            )
        raise ProviderRuntimeContractError(
            "acceptance budget environment variables are not ordinary run authority; "
            "bind the typed acceptance entry or provide metadata.provider_budget"
        )
    elif raw_state_path or raw_run_id:
        raise ProviderRuntimeContractError(
            "unpaired acceptance budget state identity cannot authorize a new runtime job"
        )

    if active_run_controller is not None:
        if not isinstance(active_run_controller.budget, ProviderAggregateBudgetV2):
            raise ProviderRuntimeContractError(
                "ordinary runtime run binding must use strict finite budget V2"
            )
        if active_run_controller.state_path is None or not active_run_controller.state_started:
            raise ProviderRuntimeContractError(
                "ordinary runtime run budget must be durably bound"
            )
        if (
            explicit_budget is not None
            and explicit_budget != active_run_controller.budget
        ):
            from runtime.downstream_continuation import validate_current_continuation_controller

            if not validate_current_continuation_controller(
                active_run_controller, runtime_spec_budget=explicit_budget,
            ):
                raise ProviderRuntimeContractError(
                    "runtime-spec budget conflicts with the active ordinary run budget"
                )
        return active_run_controller
    return None


def validate_started_acceptance_execution_context(
    context: AcceptanceExecutionContextV1,
    controller: ProviderBudgetController,
    *,
    current_executable_sha: str,
    now_epoch: float | None = None,
) -> None:
    """Validate acceptance authority before binding it to a runtime run.

    This is the budget/context portion of ControlPlane's started-acceptance
    check. Callers remain responsible for their operation-specific host policy
    and for verifying any Registry scope proof.
    """

    if not isinstance(context, AcceptanceExecutionContextV1):
        raise ProviderRuntimeContractError("accepted runtime context is not typed")
    if not isinstance(controller, ProviderBudgetController):
        raise ProviderRuntimeContractError("accepted runtime controller is not typed")
    if not context.owner_authorized or not context.provider_budget_state_started:
        raise ProviderRuntimeContractError(
            "acceptance context is not owner-authorized and started"
        )
    current_time = time.time() if now_epoch is None else float(now_epoch)
    if (
        isinstance(current_time, bool)
        or not math.isfinite(current_time)
        or current_time < 0
        or context.absolute_deadline_epoch <= current_time
    ):
        raise ProviderRuntimeContractError("acceptance context deadline has expired")
    expected_sha = str(current_executable_sha or "").strip().lower()
    if expected_sha != context.final_executable_sha:
        raise ProviderRuntimeContractError(
            "acceptance context executable SHA differs from the current runtime"
        )
    if controller.budget != context.provider_budget:
        raise ProviderRuntimeContractError(
            "acceptance context budget differs from its controller"
        )
    if (
        not controller.state_started
        or controller.state_path
        != Path(context.provider_budget_state_path).expanduser().resolve()
    ):
        raise ProviderRuntimeContractError(
            "acceptance context does not match a started durable budget controller"
        )
    if (
        controller.budget.max_provider_calls_total <= 0
        or controller.budget.max_output_tokens_total <= 0
        or controller.budget.max_wall_seconds <= 0
    ):
        raise ProviderRuntimeContractError(
            "acceptance context provider budget is not finitely bounded"
        )
    controller.assert_state_integrity(
        acceptance_run_id=context.acceptance_run_id,
        state_started=True,
    )


def provider_run_plan_binding_from_verified_acceptance_plan(
    context: AcceptanceExecutionContextV1,
    controller: ProviderBudgetController,
    *,
    current_executable_sha: str,
    acceptance_plan_sha256: str,
    acceptance_spec_file_sha256: str,
    scenario_id: str,
    job_id: str,
    runtime_spec_sha256: str,
    runtime_spec_file_sha256: str,
    scope_hashes: Mapping[str, str],
    input_identity_sha256: str,
    workspace_identity_sha256: str,
    now_epoch: float | None = None,
) -> ProviderRunPlanBindingV1:
    """Bind one child after ControlPlane has verified its acceptance plan.

    The caller must parse and hash the current acceptance plan, confirm the
    scenario's exact RuntimeJobSpec/job/workspace/input binding, and verify
    the acceptance-run state before calling this factory. The factory checks
    that its result is coupled to a current, started, finite V2 controller.
    """

    validate_started_acceptance_execution_context(
        context,
        controller,
        current_executable_sha=current_executable_sha,
        now_epoch=now_epoch,
    )
    if not isinstance(controller.budget, ProviderAggregateBudgetV2):
        raise ProviderRuntimeContractError(
            "legacy V1 acceptance budgets cannot bind a new accepted runtime scope"
        )
    return ProviderRunPlanBindingV1(
        acceptance_run_id=context.acceptance_run_id,
        acceptance_plan_sha256=acceptance_plan_sha256,
        acceptance_spec_file_sha256=acceptance_spec_file_sha256,
        scenario_id=scenario_id,
        job_id=job_id,
        runtime_spec_sha256=runtime_spec_sha256,
        runtime_spec_file_sha256=runtime_spec_file_sha256,
        scope_hashes=scope_hashes,
        input_identity_sha256=input_identity_sha256,
        workspace_identity_sha256=workspace_identity_sha256,
        provider_budget_sha256=hash_json(controller.budget.to_dict()),
        final_executable_sha=context.final_executable_sha,
        _verification_token=_PROVIDER_RUN_PLAN_BINDING_TOKEN,
    )


@contextmanager
def bind_provider_run_plan_binding(binding: ProviderRunPlanBindingV1):
    """Bind one ControlPlane-verified child plan for its runtime execution."""

    if not isinstance(binding, ProviderRunPlanBindingV1):
        raise ProviderRuntimeContractError(
            "provider run plan binding must be a typed verified binding"
        )
    context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    if context is None or binding.acceptance_run_id != context.acceptance_run_id:
        raise ProviderRuntimeContractError(
            "provider run plan binding does not match the active accepted entry"
        )
    if (
        binding._verification_token is not _PROVIDER_RUN_PLAN_BINDING_TOKEN
        or binding.final_executable_sha != context.final_executable_sha
    ):
        raise ProviderRuntimeContractError(
            "provider run plan binding verification or executable identity is invalid"
        )
    token = _ACTIVE_PROVIDER_RUN_PLAN_BINDING.set(binding)
    try:
        yield binding
    finally:
        _ACTIVE_PROVIDER_RUN_PLAN_BINDING.reset(token)


def validate_provider_run_plan_binding(
    controller: ProviderBudgetController,
    *,
    job_id: str,
    runtime_spec_sha256: str,
    scope_hashes: Mapping[str, str],
    workspace_identity_sha256: str = "",
    current_executable_sha: str,
    now_epoch: float | None = None,
) -> ProviderRunPlanBindingV1:
    """Require the active verified plan to match this normalized runtime spec."""

    context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    if context is None or _ACTIVE_ACCEPTANCE_CONTROLLER.get() is not controller:
        raise ProviderRuntimeContractError(
            "accepted runtime scope lacks an active in-process plan owner"
        )
    validate_started_acceptance_execution_context(
        context,
        controller,
        current_executable_sha=current_executable_sha,
        now_epoch=now_epoch,
    )
    binding = _ACTIVE_PROVIDER_RUN_PLAN_BINDING.get()
    if not isinstance(binding, ProviderRunPlanBindingV1):
        raise ProviderRuntimeContractError(
            "accepted runtime scope lacks a ControlPlane-verified child plan binding"
        )
    if (
        binding._verification_token is not _PROVIDER_RUN_PLAN_BINDING_TOKEN
        or binding.acceptance_run_id != context.acceptance_run_id
        or binding.final_executable_sha != context.final_executable_sha
        or binding.provider_budget_sha256 != hash_json(controller.budget.to_dict())
        or binding.job_id != str(job_id or "")
        or binding.runtime_spec_sha256 != str(runtime_spec_sha256 or "")
        or dict(binding.scope_hashes) != dict(scope_hashes)
        or (
            workspace_identity_sha256
            and binding.workspace_identity_sha256 != str(workspace_identity_sha256)
        )
    ):
        raise ProviderRuntimeContractError(
            "accepted child plan does not match this job, budget, executable, or normalized RuntimeJobSpec"
        )
    return binding


def provider_run_scope_payload_from_plan_binding(
    controller: ProviderBudgetController,
    *,
    job_id: str,
    scope_hashes: Mapping[str, str],
    workspace_identity_sha256: str,
    current_executable_sha: str,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Materialize the exact actual scope under a verified accepted child plan."""

    binding = validate_provider_run_plan_binding(
        controller,
        job_id=job_id,
        runtime_spec_sha256=str(scope_hashes.get("runtime_spec_sha256") or ""),
        scope_hashes=scope_hashes,
        workspace_identity_sha256=workspace_identity_sha256,
        current_executable_sha=current_executable_sha,
        now_epoch=now_epoch,
    )
    if not isinstance(controller.budget, ProviderAggregateBudgetV2):
        raise ProviderRuntimeContractError(
            "accepted run scope artifact requires strict finite budget V2"
        )
    if set(scope_hashes) != _PROVIDER_RUN_SCOPE_HASH_FIELDS:
        raise ProviderRuntimeContractError(
            "accepted run scope does not contain the complete six-hash contract"
        )
    normalized_scope: dict[str, str] = {}
    for name in sorted(_PROVIDER_RUN_SCOPE_HASH_FIELDS):
        value = str(scope_hashes.get(name) or "")
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ProviderRuntimeContractError(
                f"accepted run scope hash is invalid: {name}"
            )
        normalized_scope[name] = value
    return {
        "schema_version": "accepted-provider-run-scope/v1",
        "job_id": binding.job_id,
        "run_id": binding.acceptance_run_id,
        "provider_budget": controller.budget.to_dict(),
        "scope_hashes": normalized_scope,
        "acceptance_plan_sha256": binding.acceptance_plan_sha256,
        "acceptance_spec_file_sha256": binding.acceptance_spec_file_sha256,
        "scenario_id": binding.scenario_id,
        "runtime_spec_sha256": binding.runtime_spec_sha256,
        "runtime_spec_file_sha256": binding.runtime_spec_file_sha256,
        "input_identity_sha256": binding.input_identity_sha256,
        "workspace_identity_sha256": binding.workspace_identity_sha256,
        "final_executable_sha": binding.final_executable_sha,
    }


def provider_run_scope_authorization_from_verified_artifact(
    payload: Mapping[str, Any],
    *,
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    artifact_role: str,
    artifact_status: str,
    artifact_sha256: str,
) -> ProviderRunScopeAuthorizationV1:
    """Decode a ready accepted-plan scope artifact after Registry closure verification."""

    if (
        artifact_type != "accepted_runtime_run_scope"
        or artifact_version != "v1"
        or artifact_role != "accepted_runtime_run_scope"
        or artifact_status != "ready"
        or not str(artifact_id or "").strip()
    ):
        raise ProviderRuntimeContractError(
            "accepted run scope must come from a ready accepted_runtime_run_scope/v1 artifact"
        )
    if not isinstance(payload, Mapping) or payload.get("schema_version") != "accepted-provider-run-scope/v1":
        raise ProviderRuntimeContractError(
            "accepted runtime scope payload schema is invalid"
        )
    raw_budget = payload.get("provider_budget")
    if not isinstance(raw_budget, Mapping):
        raise ProviderRuntimeContractError(
            "accepted runtime scope payload lacks its finite provider budget"
        )
    budget = ProviderAggregateBudgetV2.from_mapping(raw_budget)
    scope_hashes = payload.get("scope_hashes")
    if not isinstance(scope_hashes, Mapping):
        raise ProviderRuntimeContractError(
            "accepted runtime scope payload hashes are invalid"
        )
    runtime_spec_sha256 = str(scope_hashes.get("runtime_spec_sha256") or "")
    if runtime_spec_sha256 != str(payload.get("runtime_spec_sha256") or ""):
        raise ProviderRuntimeContractError(
            "accepted runtime scope payload RuntimeJobSpec hash is inconsistent"
        )
    return ProviderRunScopeAuthorizationV1(
        job_id=str(payload.get("job_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        provider_budget_sha256=hash_json(budget.to_dict()),
        scope_hashes={str(key): str(value) for key, value in scope_hashes.items()},
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        acceptance_plan_sha256=str(payload.get("acceptance_plan_sha256") or ""),
        acceptance_spec_file_sha256=str(
            payload.get("acceptance_spec_file_sha256") or ""
        ),
        scenario_id=str(payload.get("scenario_id") or ""),
        runtime_spec_file_sha256=str(payload.get("runtime_spec_file_sha256") or ""),
        input_identity_sha256=str(payload.get("input_identity_sha256") or ""),
        workspace_identity_sha256=str(payload.get("workspace_identity_sha256") or ""),
        final_executable_sha=str(payload.get("final_executable_sha") or ""),
        _verification_token=_PROVIDER_RUN_SCOPE_AUTHORIZATION_TOKEN,
    )


@contextmanager
def bind_provider_run_scope_authorization(
    authorization: ProviderRunScopeAuthorizationV1,
):
    """Bind a ControlPlane-verified exact scope for an accepted run entry."""

    if not isinstance(authorization, ProviderRunScopeAuthorizationV1):
        raise ProviderRuntimeContractError(
            "accepted provider run scope authorization must be a typed artifact binding"
        )
    context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    if context is None:
        raise ProviderRuntimeContractError(
            "accepted provider run scope authorization requires an active typed acceptance entry"
        )
    if (
        authorization._verification_token is not _PROVIDER_RUN_SCOPE_AUTHORIZATION_TOKEN
        or authorization.run_id != context.acceptance_run_id
    ):
        raise ProviderRuntimeContractError(
            "accepted provider run scope authorization does not match the active accepted entry"
        )
    token = _ACTIVE_PROVIDER_RUN_SCOPE_AUTHORIZATION.set(authorization)
    try:
        yield authorization
    finally:
        _ACTIVE_PROVIDER_RUN_SCOPE_AUTHORIZATION.reset(token)


def validate_provider_run_scope_authorization(
    controller: ProviderBudgetController,
    *,
    job_id: str,
    scope_hashes: Mapping[str, str],
    workspace_identity_sha256: str,
    current_executable_sha: str,
    authorization: ProviderRunScopeAuthorizationV1 | None = None,
    now_epoch: float | None = None,
) -> None:
    """Check that a trusted ControlPlane entry authorized this exact scope."""

    context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    active_controller = _ACTIVE_ACCEPTANCE_CONTROLLER.get()
    bound_authorization = authorization or _ACTIVE_PROVIDER_RUN_SCOPE_AUTHORIZATION.get()
    if context is None or active_controller is not controller:
        raise ProviderRuntimeContractError(
            "accepted runtime scope lacks an active in-process acceptance entry"
        )
    validate_started_acceptance_execution_context(
        context,
        controller,
        current_executable_sha=current_executable_sha,
        now_epoch=now_epoch,
    )
    if not isinstance(controller.budget, ProviderAggregateBudgetV2):
        raise ProviderRuntimeContractError(
            "legacy V1 acceptance budget cannot authorize a new runtime scope"
        )
    plan_binding = validate_provider_run_plan_binding(
        controller,
        job_id=job_id,
        runtime_spec_sha256=str(scope_hashes.get("runtime_spec_sha256") or ""),
        scope_hashes=scope_hashes,
        workspace_identity_sha256=workspace_identity_sha256,
        current_executable_sha=current_executable_sha,
        now_epoch=now_epoch,
    )
    if not isinstance(bound_authorization, ProviderRunScopeAuthorizationV1):
        raise ProviderRuntimeContractError(
            "accepted runtime entry lacks a verified accepted-plan scope artifact"
        )
    if (
        bound_authorization._verification_token is not _PROVIDER_RUN_SCOPE_AUTHORIZATION_TOKEN
        or bound_authorization.run_id != context.acceptance_run_id
        or bound_authorization.job_id != str(job_id or "")
        or bound_authorization.provider_budget_sha256 != hash_json(controller.budget.to_dict())
        or dict(bound_authorization.scope_hashes) != dict(scope_hashes)
        or bound_authorization.acceptance_plan_sha256 != plan_binding.acceptance_plan_sha256
        or bound_authorization.acceptance_spec_file_sha256
        != plan_binding.acceptance_spec_file_sha256
        or bound_authorization.scenario_id != plan_binding.scenario_id
        or bound_authorization.runtime_spec_file_sha256
        != plan_binding.runtime_spec_file_sha256
        or bound_authorization.input_identity_sha256 != plan_binding.input_identity_sha256
        or bound_authorization.workspace_identity_sha256
        != plan_binding.workspace_identity_sha256
        or bound_authorization.final_executable_sha != plan_binding.final_executable_sha
    ):
        raise ProviderRuntimeContractError(
            "accepted-plan scope authorization does not match this job, budget, or source/goal/route scope"
        )


def provider_run_scope_proof_from_verified_artifact(
    envelope_payload: Mapping[str, Any],
    *,
    artifact_id: str,
    artifact_role: str,
    artifact_status: str,
    artifact_sha256: str,
) -> ProviderRunScopeProofV1:
    """Create an accepted-run proof from a Registry-verified ready envelope.

    The caller must first run ``ArtifactRegistry.verify_ready_artifact_closure``
    on the artifact. This function revalidates the envelope payload itself and
    records the Registry artifact identity/hash; it never accepts a serialized
    environment flag as proof.
    """

    if artifact_id != "provider_run_envelope" or artifact_role != "provider_run_envelope":
        raise ProviderRuntimeContractError(
            "accepted runtime scope must come from the provider run envelope artifact"
        )
    if artifact_status != "ready":
        raise ProviderRuntimeContractError(
            "accepted runtime scope proof artifact is not ready"
        )
    from runtime.provider_run_envelope import ProviderRunEnvelopeV1

    try:
        envelope = ProviderRunEnvelopeV1.from_dict(envelope_payload)
    except (TypeError, ValueError) as exc:
        raise ProviderRuntimeContractError(
            f"accepted runtime scope envelope is invalid: {exc}"
        ) from exc
    if envelope.admission_status != "ready_with_jit":
        raise ProviderRuntimeContractError(
            "accepted runtime scope envelope is not admitted"
        )
    return ProviderRunScopeProofV1(
        job_id=envelope.job_id,
        run_id=envelope.run_id,
        provider_budget_sha256=hash_json(envelope.provider_budget.to_dict()),
        scope_hashes=dict(envelope.scope_hashes),
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        _verification_token=_PROVIDER_RUN_SCOPE_PROOF_TOKEN,
    )


@contextmanager
def bind_provider_run_scope_proof(proof: ProviderRunScopeProofV1):
    """Bind a Registry-verified scope proof for one accepted continuation."""

    if not isinstance(proof, ProviderRunScopeProofV1):
        raise ProviderRuntimeContractError(
            "accepted provider run scope proof must be a typed verified proof"
        )
    active_context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    if active_context is None:
        raise ProviderRuntimeContractError(
            "accepted provider run scope proof requires an active validated acceptance entry"
        )
    if (
        proof._verification_token is not _PROVIDER_RUN_SCOPE_PROOF_TOKEN
        or proof.run_id != active_context.acceptance_run_id
    ):
        raise ProviderRuntimeContractError(
            "accepted provider run scope proof is not bound to the active accepted entry"
        )
    token = _ACTIVE_PROVIDER_RUN_SCOPE_PROOF.set(proof)
    try:
        yield proof
    finally:
        _ACTIVE_PROVIDER_RUN_SCOPE_PROOF.reset(token)


def validate_provider_run_scope_authority(
    controller: ProviderBudgetController,
    *,
    job_id: str,
    scope_hashes: Mapping[str, str],
    workspace_identity_sha256: str,
    current_executable_sha: str,
    authorization: ProviderRunScopeAuthorizationV1 | None = None,
    proof: ProviderRunScopeProofV1 | None = None,
    now_epoch: float | None = None,
) -> None:
    """Require a same-job, same-budget accepted continuation proof."""

    validate_provider_run_scope_authorization(
        controller,
        job_id=job_id,
        scope_hashes=scope_hashes,
        workspace_identity_sha256=workspace_identity_sha256,
        current_executable_sha=current_executable_sha,
        authorization=authorization,
        now_epoch=now_epoch,
    )
    bound_proof = proof or _ACTIVE_PROVIDER_RUN_SCOPE_PROOF.get()
    if not isinstance(bound_proof, ProviderRunScopeProofV1):
        raise ProviderRuntimeContractError(
            "accepted runtime continuation lacks a verified run-scope envelope"
        )
    accepted_context = _ACTIVE_ACCEPTANCE_CONTEXT.get()
    if accepted_context is None:
        raise ProviderRuntimeContractError(
            "accepted runtime continuation lost its active acceptance context"
        )
    if (
        bound_proof._verification_token is not _PROVIDER_RUN_SCOPE_PROOF_TOKEN
        or bound_proof.run_id != accepted_context.acceptance_run_id
        or bound_proof.job_id != str(job_id or "")
        or bound_proof.provider_budget_sha256
        != hash_json(controller.budget.to_dict())
        or dict(bound_proof.scope_hashes) != dict(scope_hashes)
    ):
        raise ProviderRuntimeContractError(
            "accepted run-scope proof does not match this job, budget, or source/goal/route scope"
        )


def project_shared_retry_attempts_v1(
    logical_calls: int | None,
    per_call_retry_ceilings: Iterable[int | None],
    *,
    aggregate_controller: ProviderBudgetController | None = None,
) -> SharedRetryProjectionV1:
    """Project physical attempts using per-call ceilings and a shared pool.

    This is a diagnostic upper bound only; aggregate admission remains atomic
    in ``ProviderBudgetController.admit``. V2 zero is a real exhausted shared
    retry pool. V1 zero retains its historical unbounded-pool meaning and thus
    falls back to the per-call ceilings when those are known.
    """

    if logical_calls is not None and (
        isinstance(logical_calls, bool)
        or not isinstance(logical_calls, int)
        or logical_calls < 0
    ):
        raise ProviderRuntimeContractError(
            "logical_calls must be a non-negative integer or None"
        )
    ceilings: list[int | None] = []
    for value in per_call_retry_ceilings:
        if value is None:
            ceilings.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProviderRuntimeContractError(
                "per-call retry ceilings must be non-negative integers or None"
            )
        ceilings.append(value)
    if logical_calls is not None and len(ceilings) != logical_calls:
        raise ProviderRuntimeContractError(
            "per-call retry ceilings must contain one row per logical call"
        )

    configured_slots = (
        None if any(value is None for value in ceilings)
        else sum(int(value or 0) for value in ceilings)
    )
    return project_shared_retry_slots_v1(
        logical_calls,
        configured_retry_slots_upper_bound=configured_slots,
        aggregate_controller=aggregate_controller,
    )


def project_shared_retry_slots_v1(
    logical_calls: int | None,
    *,
    configured_retry_slots_upper_bound: int | None,
    aggregate_controller: ProviderBudgetController | None = None,
) -> SharedRetryProjectionV1:
    """Apply a run retry pool to an already-summed per-call ceiling.

    ``configured_retry_slots_upper_bound`` is the sum of the request-specific
    retry ceilings; it remains separate from aggregate pool availability. This
    form is useful to callers whose call plan already stores those ceilings as
    per-call physical-attempt bounds.
    """

    if logical_calls is not None and (
        isinstance(logical_calls, bool)
        or not isinstance(logical_calls, int)
        or logical_calls < 0
    ):
        raise ProviderRuntimeContractError(
            "logical_calls must be a non-negative integer or None"
        )
    if configured_retry_slots_upper_bound is not None and (
        isinstance(configured_retry_slots_upper_bound, bool)
        or not isinstance(configured_retry_slots_upper_bound, int)
        or configured_retry_slots_upper_bound < 0
    ):
        raise ProviderRuntimeContractError(
            "configured retry slot upper bound must be a non-negative integer or None"
        )

    shared_remaining: int | None = None
    if aggregate_controller is not None:
        budget = aggregate_controller.budget
        if (
            isinstance(budget, ProviderAggregateBudgetV2)
            or budget.max_retry_attempts_total > 0
        ):
            snapshot = aggregate_controller.snapshot()
            shared_remaining = max(
                0,
                int(budget.max_retry_attempts_total)
                - int(snapshot["retry_attempts_used"])
                - int(snapshot["retry_attempts_reserved"]),
            )

    configured_slots = configured_retry_slots_upper_bound
    if shared_remaining is None:
        retry_upper = configured_slots
        retry_source = "per_call" if configured_slots is not None else "unknown"
    elif configured_slots is None:
        retry_upper = shared_remaining
        retry_source = "shared_pool"
    else:
        retry_upper = min(configured_slots, shared_remaining)
        retry_source = (
            "per_call_and_shared_pool"
            if configured_slots <= shared_remaining
            else "shared_pool"
        )
    physical_upper = (
        None
        if logical_calls is None or retry_upper is None
        else logical_calls + retry_upper
    )
    return SharedRetryProjectionV1(
        logical_calls=logical_calls,
        configured_retry_slots_upper_bound=configured_slots,
        aggregate_retry_pool_remaining=shared_remaining,
        retry_attempts_upper_bound=retry_upper,
        physical_attempts_upper_bound=physical_upper,
        retry_bound_source=retry_source,
    )


def build_provider_run_scope_hashes_v1(
    *,
    normalized_runtime_spec: Mapping[str, Any],
    effective_config_sha256: str,
    source_snapshot_sha256: str,
    review_goal: Mapping[str, Any],
    route_plan: Any,
    stage_work_contract: Mapping[str, Any],
) -> dict[str, str]:
    """Build the canonical six run-scope hashes from the actual run inputs."""

    route_payload = route_plan.to_dict() if callable(getattr(route_plan, "to_dict", None)) else route_plan
    if not isinstance(normalized_runtime_spec, Mapping):
        raise ProviderRuntimeContractError("normalized RuntimeJobSpec payload must be a mapping")
    if not isinstance(review_goal, Mapping) or not isinstance(stage_work_contract, Mapping):
        raise ProviderRuntimeContractError("run goal and stage work contract must be mappings")
    if not isinstance(route_payload, Mapping):
        raise ProviderRuntimeContractError("reachable provider route plan must be a mapping")
    for name, value in (
        ("effective_config_sha256", effective_config_sha256),
        ("source_snapshot_sha256", source_snapshot_sha256),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None:
            raise ProviderRuntimeContractError(f"{name} must be a lowercase SHA-256 digest")
    return {
        "runtime_spec_sha256": hash_json(dict(normalized_runtime_spec)),
        "effective_config_sha256": str(effective_config_sha256),
        "source_snapshot_sha256": str(source_snapshot_sha256),
        "review_goal_sha256": hash_json(dict(review_goal)),
        "route_plan_sha256": hash_json(dict(route_payload)),
        "stage_work_contract_sha256": hash_json(dict(stage_work_contract)),
    }


def provider_run_workspace_identity_sha256(
    workspace_path: str | Path,
    *,
    job_id: str,
) -> str:
    """Return a stable workspace/job binding that survives Registry growth."""

    target = Path(workspace_path).expanduser().resolve()
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        raise ProviderRuntimeContractError("provider run workspace identity requires job_id")
    return hash_json({"workspace": str(target), "job_id": normalized_job_id})


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
        if folded == "requested_output_tokens_per_attempt" and type(raw_value) is int and raw_value >= 0:
            # This is an accounting count, never a credential string. Keep
            # legacy redacted values and nonnumeric values redacted below.
            result[key] = raw_value
        elif folded in _NON_SECRET_TOKEN_KEYS:
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
        if value is None:
            source: Mapping[str, Any] = {}
        elif not isinstance(value, Mapping):
            raise ProviderRuntimeContractError("provider budget must be a mapping")
        else:
            source = value

        def integer(name: str) -> int:
            raw = source.get(name, 0)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return 0
            if isinstance(raw, bool):
                raise ProviderRuntimeContractError(f"{name} must be an integer")
            try:
                parsed = int(str(raw).strip())
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(f"{name} must be an integer") from exc
            if parsed < 0:
                raise ProviderRuntimeContractError(f"{name} must be non-negative")
            return parsed

        def real(name: str) -> float:
            raw = source.get(name, 0.0)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return 0.0
            if isinstance(raw, bool):
                raise ProviderRuntimeContractError(f"{name} must be a number")
            try:
                parsed = float(str(raw).strip())
            except (TypeError, ValueError) as exc:
                raise ProviderRuntimeContractError(f"{name} must be a number") from exc
            if not math.isfinite(parsed) or parsed < 0:
                raise ProviderRuntimeContractError(f"{name} must be a finite non-negative number")
            return parsed

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
        if not isinstance(payload, Mapping):
            raise ProviderRuntimeContractError("provider receipt must be a JSON object")
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(str(key) for key in payload if str(key) not in allowed)
        if unknown:
            raise ProviderRuntimeContractError(
                "provider receipt contains unknown fields: " + ", ".join(unknown)
            )
        required = {
            "artifact_type",
            "artifact_version",
            "receipt_id",
            "sequence",
            "job_id",
            "attempt_id",
            "stage_name",
            "route",
            "provider",
            "model",
            "endpoint",
            "prompt_hash",
            "input_hash",
            "config_hash",
            "schema_hash",
            "status",
            "attempts",
            "started_at",
            "finished_at",
            "budget",
            "metadata",
            "test_only",
        }
        missing = sorted(name for name in required if name not in payload)
        if missing:
            raise ProviderRuntimeContractError(
                "provider receipt is missing: " + ", ".join(missing)
            )
        for name in ("sequence", "attempts"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProviderRuntimeContractError(
                    f"provider receipt {name} must be an integer"
                )
        if not isinstance(payload.get("test_only"), bool):
            raise ProviderRuntimeContractError(
                "provider receipt test_only must be a boolean"
            )
        if not isinstance(payload.get("budget"), Mapping) or not isinstance(
            payload.get("metadata"), Mapping
        ):
            raise ProviderRuntimeContractError(
                "provider receipt budget and metadata must be objects"
            )
        raw_status = payload.get("status")
        if not isinstance(raw_status, str):
            raise ProviderRuntimeContractError("provider receipt status must be a string")
        raw_budget = payload.get("budget")
        budget = raw_budget if isinstance(raw_budget, Mapping) else {}
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        return cls(
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            receipt_id=str(payload.get("receipt_id") or ""),
            sequence=payload["sequence"],
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
            status=raw_status,  # type: ignore[arg-type]
            error_kind=str(payload.get("error_kind") or "") or None,
            http_status=int(payload["http_status"]) if payload.get("http_status") is not None else None,
            provider_code=str(payload.get("provider_code") or "") or None,
            attempts=payload["attempts"],
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
            test_only=payload["test_only"],
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


class ProviderResponseSnapshotSink(Protocol):
    """Optional successor storage boundary, separate from receipt authority."""

    def capture(self, receipt: ProviderCallReceiptV1, content: Any) -> Any: ...

    def require(self, receipt: ProviderCallReceiptV1) -> Any: ...


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
        primary_repair_response_journal: PrimaryRepairResponseJournal | None = None,
        response_snapshot_sink: ProviderResponseSnapshotSink | None = None,
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
        self._consumed_tokens = 0
        self._unknown_exposure_tokens = 0
        self._aggregate_reservations: dict[int, ProviderAggregateReservationV1] = {}
        self.primary_repair_response_journal = primary_repair_response_journal
        self.response_snapshot_sink = response_snapshot_sink
        self._primary_repair_scopes: dict[int, PrimaryRepairScopeV1] = {}
        self._primary_repair_receipt_bindings: dict[int, dict[str, Any]] = {}
        self._receipts: list[ProviderCallReceiptV1] = []
        self._completion_lock = threading.RLock()
        self._completed_receipts: dict[int, ProviderCallReceiptV1] = {}
        self._accounted_sequences: set[int] = set()
        self._restore_local_budget_from_ledger()

    @staticmethod
    def _snapshot_eligible(receipt: ProviderCallReceiptV1) -> bool:
        return (receipt.status == "success" and receipt.error_kind is None
                and not receipt.incomplete_reason and not receipt.timeout_kind
                and receipt.finish_reason.strip().casefold() not in {
                    "length", "max_tokens", "max_output_tokens", "incomplete",
                }
                and (receipt.http_status is None or 200 <= receipt.http_status < 300))

    def _require_response_snapshot(self, receipt: ProviderCallReceiptV1) -> None:
        if self.response_snapshot_sink is None or not self._snapshot_eligible(receipt):
            return
        try:
            self.response_snapshot_sink.require(receipt)
        except Exception as exc:
            raise ProviderRuntimeContractError(
                f"provider response snapshot is unavailable for {receipt.receipt_id}; "
                "local reconciliation is required before reuse"
            ) from exc

    def _capture_response_snapshot(self, receipt: ProviderCallReceiptV1, content: Any) -> None:
        if self.response_snapshot_sink is None or not self._snapshot_eligible(receipt):
            return
        try:
            self.response_snapshot_sink.capture(receipt, content)
        except Exception as exc:
            raise ProviderRuntimeContractError(
                f"provider response snapshot capture is incomplete for {receipt.receipt_id}; "
                "the original receipt and consumption are retained"
            ) from exc

    def _snapshot_receipt_metadata(
        self, metadata: Mapping[str, Any] | None, api_config: Mapping[str, Any],
        endpoint_type: str,
    ) -> dict[str, Any]:
        result = dict(metadata or {})
        if self.response_snapshot_sink is not None and "transport_config" not in result:
            # Some production stages own an uninstrumented transport and append
            # the authoritative receipt here. Capture must see its admitted
            # route identity before that receipt is made durable. Keep old
            # receipts and richer instrumented transport metadata unchanged.
            result["transport_config"] = {
                "provider_family": str(api_config.get("provider_family") or api_config.get("provider") or "generic"),
                "endpoint_type": endpoint_type,
                "api_base": str(api_config.get("api_base") or ""),
                "model": str(api_config.get("model") or ""),
            }
        return result

    @staticmethod
    def _reported_total_tokens(receipt: ProviderCallReceiptV1) -> int | None:
        if receipt.total_tokens is not None:
            return int(receipt.total_tokens)
        if receipt.input_tokens is not None and receipt.output_tokens is not None:
            return int(receipt.input_tokens) + int(receipt.output_tokens)
        return None

    def _restore_local_budget_from_ledger(self) -> None:
        """Rebuild local cumulative usage so a restart cannot reset the cap."""

        if self.ledger is None:
            return
        for receipt in self.ledger.list_receipts():
            if not self.test_only:
                identity_matches = (
                    (not self.job_id or receipt.job_id == self.job_id)
                    and (not self.attempt_id or receipt.attempt_id == self.attempt_id)
                    and (not self.stage_name or receipt.stage_name == self.stage_name)
                    and (not self.call_id or receipt.call_id == self.call_id)
                )
                if not identity_matches:
                    continue
            self._receipts.append(receipt)
            if receipt.status == "blocked":
                continue
            metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
            if str(metadata.get("accounting_status") or "committed") != "pending":
                self._accounted_sequences.add(int(receipt.sequence))
            total = self._reported_total_tokens(receipt)
            if total is not None:
                self._consumed_tokens += max(0, total)
                continue
            try:
                reserved = int(metadata.get("local_budget_reservation_tokens") or 0)
            except (TypeError, ValueError):
                reserved = 0
            self._unknown_exposure_tokens += max(0, reserved)

    def _existing_receipt_for_admission(
        self,
        admission: ProviderCallAdmissionV1,
        *,
        prompt: str,
        input_payload: Any,
    ) -> ProviderCallReceiptV1 | None:
        def belongs_to_runtime(receipt: ProviderCallReceiptV1) -> bool:
            if self.test_only:
                return True
            return (
                (not self.job_id or receipt.job_id == self.job_id)
                and (not self.attempt_id or receipt.attempt_id == self.attempt_id)
                and (not self.stage_name or receipt.stage_name == self.stage_name)
                and (not self.call_id or receipt.call_id == self.call_id)
            )

        def is_pending_accounting(receipt: ProviderCallReceiptV1) -> bool:
            metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
            return str(metadata.get("accounting_status") or "committed") == "pending"

        candidates = [
            receipt
            for receipt in self._receipts
            if receipt.sequence == admission.sequence
            and belongs_to_runtime(receipt)
            and (is_pending_accounting(receipt) or self.response_snapshot_sink is not None)
        ]
        if not candidates and self.ledger is not None:
            candidates = [
                receipt
                for receipt in self.ledger.list_receipts()
                if receipt.sequence == admission.sequence
                and belongs_to_runtime(receipt)
                and (is_pending_accounting(receipt) or self.response_snapshot_sink is not None)
            ]
        if not candidates:
            return None
        if self.response_snapshot_sink is not None and len({row.receipt_id for row in candidates}) != 1:
            raise ProviderRuntimeContractError(
                f"response snapshot completion has ambiguous receipt authority for sequence {admission.sequence}"
            )
        receipt = candidates[0]
        if receipt.prompt_hash != hash_text(prompt) or receipt.input_hash != hash_json(input_payload):
            raise ProviderReceiptConflict(
                f"admission sequence {admission.sequence} was reused with different input"
            )
        return receipt

    def _assert_pause_state_before_admission(self) -> None:
        context = current_runtime_control_context()
        identity = context.identity if context is not None else None
        if identity is not None and self.job_id and self.job_id != identity.job_id:
            raise ProviderRuntimeContractError(
                "runtime control identity does not match ProviderRuntime job ID"
            )
        pause_store = context.pause_state_store if context is not None else None
        marker_path = current_pause_state_path()
        if context is not None and marker_path and str(Path(marker_path).resolve()) != str(
            Path(context.pause_state_store.path).resolve()
        ):
            raise ProviderRuntimeContractError(
                "runtime pause path does not match the bound job control context"
            )
        if pause_store is None and marker_path:
            pause_store = _registered_pause_store_for_path(marker_path)
            if pause_store is None:
                raise ProviderRuntimeContractError(
                    "CONTROL_STATE_INVALID: pause marker is not bound to a verifiable workspace"
                )
        if pause_store is not None:
            try:
                pause_state = pause_store.read()
            except Exception as exc:
                raise ProviderRuntimeContractError(
                    f"CONTROL_STATE_INVALID: pause authority cannot be verified: {exc}"
                ) from exc
            if pause_state is not None and pause_state.paused:
                raise ProviderBudgetExceeded("PAUSED_BY_USER: provider admission is blocked")

        if context is not None:
            if context.lease_validator is not None:
                try:
                    lease_current = bool(context.lease_validator())
                except Exception as exc:
                    raise ProviderRuntimeContractError(
                        f"QUEUE_LEASE_INVALID: active lease cannot be verified: {type(exc).__name__}"
                    ) from exc
                if not lease_current:
                    raise ProviderBudgetExceeded(
                        "QUEUE_LEASE_LOST: provider admission is blocked for a stale queue lease"
                    )
            if context.cancel_token is not None:
                try:
                    cancelled = bool(context.cancel_token.is_cancelled())
                except Exception as exc:
                    raise ProviderRuntimeContractError(
                        f"CANCEL_STATE_INVALID: cancellation token cannot be read: {type(exc).__name__}"
                    ) from exc
                if cancelled:
                    raise ProviderCancellationRequested(
                        "CANCEL_REQUESTED: provider admission is blocked by the queue cancellation token"
                    )
            try:
                cancellation = context.cancellation_store.read()
            except Exception as exc:
                raise ProviderRuntimeContractError(
                    f"CONTROL_STATE_INVALID: cancellation authority cannot be verified: {exc}"
                ) from exc
            if cancellation is not None and cancellation.active:
                raise ProviderCancellationRequested(
                    "CANCEL_REQUESTED: provider admission is blocked by the durable cancellation request"
                )

        acceptance_context = current_acceptance_execution_context()
        environment_run_id = str(os.getenv("AUTO_GENERATE_ACCEPTANCE_RUN_ID", "")).strip()
        if acceptance_context is not None and identity is not None:
            if identity.acceptance_run_id != acceptance_context.acceptance_run_id:
                raise ProviderRuntimeContractError(
                    "runtime control identity does not match the active acceptance run"
                )
        if (
            acceptance_context is not None
            and environment_run_id
            and environment_run_id != acceptance_context.acceptance_run_id
        ):
            raise ProviderRuntimeContractError(
                "acceptance run environment ID does not match its typed context"
            )
        if (
            identity is not None
            and identity.acceptance_run_id
            and environment_run_id
            and identity.acceptance_run_id != environment_run_id
        ):
            raise ProviderRuntimeContractError(
                "runtime control identity does not match the acceptance run environment"
            )
        expected_run_id = (
            acceptance_context.acceptance_run_id
            if acceptance_context is not None
            else identity.acceptance_run_id
            if identity is not None and identity.acceptance_run_id
            else environment_run_id
        )
        require_budget_state = bool(
            self.aggregate_budget is not None
            and self.aggregate_budget.state_path is not None
        ) or bool(
            (identity is not None and (identity.acceptance_run_id or identity.provider_budget_started))
            or (
                acceptance_context is not None
                and (
                    acceptance_context.owner_authorized
                    or acceptance_context.provider_budget_state_started
                )
            )
        )
        if require_budget_state:
            if self.aggregate_budget is None:
                raise ProviderRuntimeContractError(
                    "started acceptance run has no aggregate provider budget controller"
                )
            if acceptance_context is not None and self.aggregate_budget.state_path is not None:
                if self.aggregate_budget.state_path != Path(
                    acceptance_context.provider_budget_state_path
                ).expanduser().resolve():
                    raise ProviderRuntimeContractError(
                        "provider aggregate budget state path does not match the active acceptance run"
                    )
            self.aggregate_budget.assert_state_integrity(
                acceptance_run_id=expected_run_id,
                state_started=True,
            )

    def _account_existing_receipt(self, receipt: ProviderCallReceiptV1, admission: ProviderCallAdmissionV1) -> None:
        if admission.sequence in self._accounted_sequences:
            return
        metadata = receipt.metadata if isinstance(receipt.metadata, Mapping) else {}
        try:
            reservation_tokens = int(metadata.get("local_budget_reservation_tokens") or 0)
        except (TypeError, ValueError):
            reservation_tokens = admission.estimated_tokens + admission.estimated_output_tokens
        total = self._reported_total_tokens(receipt)
        self._reserved_tokens = max(0, self._reserved_tokens - reservation_tokens)
        if total is None:
            self._unknown_exposure_tokens += max(0, reservation_tokens)
        else:
            self._consumed_tokens += max(0, total)
        self._accounted_sequences.add(admission.sequence)

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def aggregate_wall_deadline_epoch(self) -> float | None:
        if self.aggregate_budget is None:
            return None
        return self.aggregate_budget.durable_deadline_epoch

    @property
    def reserved_tokens(self) -> int:
        return self._reserved_tokens

    @property
    def consumed_tokens(self) -> int:
        return self._consumed_tokens

    @property
    def unknown_exposure_tokens(self) -> int:
        return self._unknown_exposure_tokens

    @property
    def receipts(self) -> tuple[ProviderCallReceiptV1, ...]:
        return tuple(self._receipts)

    def max_attempts_for_call(self, requested_attempts: int) -> int:
        """Return a transport-loop limit under local and shared remaining budgets.

        The initial attempt is retained even when no shared call slot remains;
        the aggregate controller's atomic admission then rejects that attempt.
        Snapshot-derived shared limits only reduce optional retries. The
        controller rechecks all reservations atomically in admit().
        """

        requested = max(1, int(requested_attempts))
        if self.budget.max_retries_per_call:
            requested = min(requested, self.budget.max_retries_per_call + 1)

        controller = self.aggregate_budget
        if controller is None:
            return requested

        snapshot = controller.snapshot()
        aggregate = controller.budget
        strict_budget = isinstance(aggregate, ProviderAggregateBudgetV2)

        if strict_budget or aggregate.max_retry_attempts_total:
            remaining_retries = max(
                0,
                int(aggregate.max_retry_attempts_total)
                - int(snapshot["retry_attempts_used"])
                - int(snapshot["retry_attempts_reserved"]),
            )
            requested = min(requested, 1 + remaining_retries)

        if strict_budget or aggregate.max_provider_calls_total:
            remaining_calls = max(
                0,
                int(aggregate.max_provider_calls_total)
                - int(snapshot["calls_used"])
                - int(snapshot["calls_reserved"]),
            )
            requested = min(requested, max(1, remaining_calls))

        return max(1, requested)

    def admit(
        self,
        *,
        estimated_tokens: int = 0,
        requested_output_tokens: int = 0,
        requested_retry_attempts: int = 0,
        primary_repair_scope: PrimaryRepairScopeV1 | Mapping[str, Any] | None = None,
        request_hash: str = "",
    ) -> ProviderCallAdmissionV1:
        self._assert_pause_state_before_admission()
        estimated = max(0, int(estimated_tokens))
        output = max(0, int(requested_output_tokens))
        retries = max(0, int(requested_retry_attempts))
        scope = ProviderBudgetController._coerce_primary_repair_scope(primary_repair_scope)
        if scope is not None:
            if self.aggregate_budget is None or self.aggregate_budget.state_path is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair admission requires the durable run aggregate budget"
                )
            if self.primary_repair_response_journal is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair admission requires a durable response journal"
                )
            if scope.job_id != self.job_id or scope.logical_attempt_identity != self.logical_attempt_identity:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair scope does not match the bound runtime attempt"
                )
            if not str(request_hash or "") or str(request_hash) != scope.request_hash:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair request hash does not match its materialized scope"
                )
        with self._lock:
            elapsed = time.monotonic() - self.started_monotonic
            if self.budget.max_elapsed_seconds and elapsed >= self.budget.max_elapsed_seconds:
                raise ProviderBudgetExceeded("provider runtime elapsed-time budget exhausted")
            if self.budget.max_calls and self._calls >= self.budget.max_calls:
                raise ProviderBudgetExceeded("provider runtime call budget exhausted")
            if (
                self.budget.max_total_tokens
                and self._consumed_tokens
                + self._unknown_exposure_tokens
                + self._reserved_tokens
                + estimated
                + output
                > self.budget.max_total_tokens
            ):
                raise ProviderBudgetExceeded("provider runtime token budget exhausted")
            aggregate_reservation = None
            if self.aggregate_budget is not None:
                reservation_context = {
                    "job_id": self.job_id,
                    "attempt_id": self.attempt_id,
                    "stage_name": self.stage_name,
                    "node_id": self.node_id,
                    "call_id": self.call_id,
                    "ledger_path": str(self.ledger.path) if self.ledger is not None else "",
                }
                if scope is not None:
                    assert self.primary_repair_response_journal is not None
                    reservation_context["primary_repair_response_journal_root"] = str(
                        self.primary_repair_response_journal.root
                    )
                aggregate_reservation = self.aggregate_budget.admit(
                    requested_output_tokens=output,
                    requested_retry_attempts=retries,
                    context=reservation_context,
                    primary_repair_scope=scope,
                )
            self._calls += 1
            self._reserved_tokens += estimated + output
            if aggregate_reservation is not None:
                self._aggregate_reservations[self._calls] = aggregate_reservation
            if scope is not None:
                self._primary_repair_scopes[self._calls] = scope
            return ProviderCallAdmissionV1(
                sequence=self._calls,
                estimated_tokens=estimated,
                admitted_at=utc_now_iso(),
                remaining_calls=(self.budget.max_calls - self._calls) if self.budget.max_calls else None,
                remaining_tokens=(
                    self.budget.max_total_tokens
                    - self._consumed_tokens
                    - self._unknown_exposure_tokens
                    - self._reserved_tokens
                )
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
        """Complete an admission exactly once, including concurrent duplicates."""

        with self._completion_lock:
            cached = self._completed_receipts.get(admission.sequence)
            if cached is not None:
                self._require_response_snapshot(cached)
                return cached
            receipt = self._complete_once(
                admission=admission,
                prompt=prompt,
                input_payload=input_payload,
                api_config=api_config,
                result=result,
                schema_hash=schema_hash,
                metadata=metadata,
                route=route,
            )
            self._completed_receipts[admission.sequence] = receipt
            return receipt

    def _primary_repair_receipt_binding(
        self,
        *,
        admission: ProviderCallAdmissionV1,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        schema_hash: str | None,
        metadata: Mapping[str, Any] | None,
        route: str | None,
        result: Mapping[str, Any],
        response_format: str = "",
    ) -> dict[str, Any]:
        provider = str(api_config.get("provider_family") or api_config.get("provider") or "generic")
        return {
            "schema_version": "provider-primary-repair-receipt-binding/v1",
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "stage_name": self.stage_name,
            "route": str(route or self.route or provider),
            "provider": provider,
            "model": str(api_config.get("model") or ""),
            "endpoint": str(api_config.get("api_base") or ""),
            "prompt_hash": hash_text(prompt),
            "input_hash": hash_json(input_payload),
            "config_hash": hash_json(_redact_mapping(api_config)),
            "schema_hash": schema_hash or self.schema_hash,
            "node_id": self.node_id,
            "call_id": str((metadata or {}).get("call_id") or self.call_id),
            "closure_epoch_id": self.closure_epoch_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "endpoint_type": str(result.get("endpoint_type") or self.endpoint_type),
            "response_format": str(response_format or "text"),
            "admission": asdict(admission),
            "budget": self.budget.to_dict(),
            "receipt_metadata": _redact_mapping(self._snapshot_receipt_metadata(
                metadata, api_config, str(result.get("endpoint_type") or self.endpoint_type),
            )),
            "test_only": self.test_only,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
        }

    def primary_repair_raw_response_callback(
        self,
        admission: ProviderCallAdmissionV1,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
        *,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
        response_format: str,
        metadata: Mapping[str, Any] | None = None,
        route: str | None = None,
    ) -> Callable[[str, str, int, int | None, str, str, bool, int], None]:
        """Prepare the fsynced-spool callback for a PRIMARY call already admitted."""

        normalized_scope = ProviderBudgetController._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        response_journal = self.primary_repair_response_journal
        aggregate_budget = self.aggregate_budget
        if (
            aggregate_budget is None
            or response_journal is None
            or admission.aggregate_reservation_id is None
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY raw response retention requires its durable reservation and journal"
            )
        if hash_json(input_payload) != normalized_scope.request_hash:
            raise ProviderRuntimeContractError(
                "PRIMARY raw response request differs from its reserved scope"
            )
        with self._lock:
            registered_scope = self._primary_repair_scopes.get(admission.sequence)
            reservation = self._aggregate_reservations.get(admission.sequence)
        if (
            registered_scope is None
            or registered_scope.fingerprint_sha256 != normalized_scope.fingerprint_sha256
            or reservation is None
            or reservation.reservation_id != admission.aggregate_reservation_id
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY raw response callback does not match the live admission"
            )
        receipt_binding = self._primary_repair_receipt_binding(
            admission=admission,
            prompt=prompt,
            input_payload=input_payload,
            api_config=api_config,
            schema_hash=self.schema_hash,
            metadata=metadata,
            route=route,
            result={"endpoint_type": self.endpoint_type},
            response_format=response_format,
        )
        if receipt_binding["schema_hash"] != normalized_scope.schema_hash:
            raise ProviderRuntimeContractError(
                "PRIMARY raw response schema differs from its reserved scope"
            )
        self._primary_repair_receipt_bindings[admission.sequence] = receipt_binding

        def persist_primary_raw_response(
            path: str,
            sha256: str,
            byte_count: int,
            http_status: int | None,
            content_type: str,
            response_protocol: str,
            complete: bool,
            attempt_number: int,
        ) -> PrimaryRepairResponseRefV1:
            return response_journal.persist_raw_response(
                normalized_scope,
                reservation.reservation_id,
                spool_path=path,
                sha256=sha256,
                byte_count=byte_count,
                http_status=http_status,
                content_type=content_type,
                response_protocol=response_protocol,
                complete=complete,
                receipt_binding=receipt_binding,
                attempt_number=attempt_number,
            )

        def finalize_primary_raw_response(
            path: str,
            sha256: str,
            byte_count: int,
            http_status: int | None,
            content_type: str,
            response_protocol: str,
            complete: bool,
            attempt_number: int,
        ) -> None:
            # This fsynced per-scope record is committed before the adapter's
            # later retention callback updates aggregate scope state. On kill
            # in between, reservation reconciliation can recover locally.
            persist_primary_raw_response(
                path,
                sha256,
                byte_count,
                http_status,
                content_type,
                response_protocol,
                complete,
                attempt_number,
            )

        def retain_primary_raw_response(
            path: str,
            sha256: str,
            byte_count: int,
            http_status: int | None,
            content_type: str,
            response_protocol: str,
            complete: bool,
            attempt_number: int,
        ) -> None:
            response_ref = persist_primary_raw_response(
                path,
                sha256,
                byte_count,
                http_status,
                content_type,
                response_protocol,
                complete,
                attempt_number,
            )
            aggregate_budget.retain_primary_repair_raw_response(
                reservation,
                normalized_scope,
                response_ref,
                journal=response_journal,
            )

        setattr(
            retain_primary_raw_response,
            "finalize_transport_spool",
            finalize_primary_raw_response,
        )
        return retain_primary_raw_response

    def _reparse_primary_repair_raw_response(
        self,
        entry: PrimaryRepairResponseEntryV1,
    ) -> dict[str, Any]:
        """Locally parse one complete, retained PRIMARY HTTP 200 response."""

        raw_spool = entry.raw_spool
        semantic_only = (
            entry.entry_kind == "semantic_raw_response"
            and raw_spool is not None
            and not raw_spool.complete
            and raw_spool.http_status == 200
            and raw_spool.completion_proof == "semantic_complete_only"
        )
        if (
            entry.entry_kind not in {"raw_response", "semantic_raw_response"}
            or raw_spool is None
            or (not raw_spool.complete and not semantic_only)
            or raw_spool.http_status != 200
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY raw response has no verified successful content"
            )
        receipt_binding = entry.receipt_binding
        if receipt_binding.get("schema_version") != "provider-primary-repair-receipt-binding/v1":
            raise ProviderRuntimeContractError(
                "PRIMARY raw response has no supported receipt binding"
            )
        try:
            from ai_interface import reparse_provider_response_spool

            response = reparse_provider_response_spool(
                raw_spool.path,
                raw_spool.sha256,
                raw_spool.byte_count,
                raw_spool.content_type,
                raw_spool.response_protocol,
                str(receipt_binding.get("endpoint_type") or ""),
                str(receipt_binding.get("model") or ""),
                str(receipt_binding.get("response_format") or ""),
                int(raw_spool.http_status),
                True,
            )
        except Exception as exc:
            raise ProviderRuntimeContractError(
                "retained PRIMARY raw response failed local protocol reparse"
            ) from exc
        response["endpoint_type"] = str(
            receipt_binding.get("endpoint_type") or self.endpoint_type
        )
        response["attempts"] = raw_spool.attempt_number
        if semantic_only:
            # Parsed content is usable, but HTTP EOF and provider usage remain
            # unknown. Preserve that distinction and charge the full output
            # reservation.
            response["response_body_complete"] = False
            response["transport_completion_proof"] = "semantic_complete_only"
        if semantic_only or raw_spool.attempt_number > 1:
            # Earlier non-200 attempts may have incurred unreported usage.
            # Keep the full pre-reserved output ceiling charged.
            for token_field in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            ):
                response.pop(token_field, None)
            response["usage_status"] = "unknown"
        return response

    def _complete_once(
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
        result = dict(result)
        provider = str(api_config.get("provider_family") or api_config.get("provider") or "generic")
        model = str(api_config.get("model") or "")
        endpoint = str(api_config.get("api_base") or "")
        config_hash = hash_json(_redact_mapping(api_config))
        existing = self._existing_receipt_for_admission(
            admission,
            prompt=prompt,
            input_payload=input_payload,
        )
        with self._lock:
            primary_scope = self._primary_repair_scopes.get(admission.sequence)
            aggregate_reservation = self._aggregate_reservations.get(admission.sequence)
        prepost_failure = (
            result.get("provider_http_attempt_started") is False
            and int(result.get("provider_http_attempts_started") or 0) == 0
        )
        if (
            prepost_failure
            and primary_scope is not None
            and aggregate_reservation is not None
            and self.aggregate_budget is not None
            and self.release_unstarted(admission)
        ):
            raise ProviderBudgetExceeded(
                "PRIMARY provider attempt was blocked before its first POST; "
                "the unstarted run-budget reservation was released"
            )
        if (
            primary_scope is not None
            and result.get("primary_raw_response_retention_callback_failed") is True
        ):
            if self.primary_repair_response_journal is None or self.aggregate_budget is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY response retention failed without a recoverable journal"
                )
            entry = self.primary_repair_response_journal.load_entry_for_scope(
                primary_scope,
                reservation_id=(
                    aggregate_reservation.reservation_id
                    if aggregate_reservation is not None
                    else None
                ),
            )
            with self._lock:
                expected_binding = self._primary_repair_receipt_bindings.get(
                    admission.sequence
                )
            state = self.aggregate_budget.lookup_primary_repair_scope(primary_scope)
            if (
                entry is not None
                and expected_binding is not None
                and dict(entry.receipt_binding) == expected_binding
                and entry.raw_spool is not None
                and state is not None
                and entry.raw_spool.attempt_number
                == int(state.get("physical_attempts_started") or 0)
            ):
                result = self._reparse_primary_repair_raw_response(entry)
        if primary_scope is not None:
            try:
                physical_attempts = max(1, int(result.get("attempts") or 1))
            except (TypeError, ValueError):
                physical_attempts = admission.reserved_call_attempts
            if physical_attempts > 1:
                # A successful final response does not report usage consumed
                # by earlier retry responses. Charge the reserved output
                # ceiling instead of crediting only the last response's usage.
                for token_field in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_input_tokens",
                    "reasoning_tokens",
                ):
                    result.pop(token_field, None)
                result["usage_status"] = "unknown"
        if existing is not None:
            if self.aggregate_budget is not None and aggregate_reservation is not None:
                existing_metadata = (
                    existing.metadata if isinstance(existing.metadata, Mapping) else {}
                )
                existing_response_ref = existing_metadata.get("primary_repair_response_ref")
                if primary_scope is not None and existing_response_ref is None:
                    if self.primary_repair_response_journal is None:
                        raise ProviderRuntimeContractError(
                            "PRIMARY repair receipt has no response journal binding"
                        )
                    recovered = self.primary_repair_response_journal.load_for_scope(
                        primary_scope,
                        reservation_id=aggregate_reservation.reservation_id,
                    )
                    if recovered is None:
                        raise ProviderRuntimeContractError(
                            "PRIMARY repair receipt has no recoverable response"
                        )
                    existing_response_ref = recovered[0].to_dict()
                if existing_response_ref is None:
                    self.aggregate_budget.complete(aggregate_reservation, result)
                else:
                    self.aggregate_budget.complete(
                        aggregate_reservation,
                        result,
                        primary_repair_response_ref=existing_response_ref,
                    )
                with self._lock:
                    self._aggregate_reservations.pop(admission.sequence, None)
                    self._primary_repair_scopes.pop(admission.sequence, None)
                    self._account_existing_receipt(existing, admission)
            self._require_response_snapshot(existing)
            return existing
        aggregate_reservation = None
        aggregate_snapshot: dict[str, Any] | None = None
        aggregate_error: BaseException | None = None
        response_ref: PrimaryRepairResponseRefV1 | None = None
        reservation_tokens = admission.estimated_tokens + admission.estimated_output_tokens
        result_total_tokens = _optional_nonnegative_int(result.get("total_tokens"))
        if result_total_tokens is None:
            input_tokens = _optional_nonnegative_int(result.get("input_tokens"))
            output_tokens = _optional_nonnegative_int(result.get("output_tokens"))
            if input_tokens is not None and output_tokens is not None:
                result_total_tokens = input_tokens + output_tokens
        with self._lock:
            aggregate_reservation = self._aggregate_reservations.get(admission.sequence)
            primary_scope = self._primary_repair_scopes.get(admission.sequence)
        if primary_scope is not None:
            if aggregate_reservation is None or self.aggregate_budget is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair response has no durable aggregate reservation"
                )
            if self.primary_repair_response_journal is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair response journal disappeared after admission"
                )
            with self._lock:
                receipt_binding = self._primary_repair_receipt_bindings.get(
                    admission.sequence
                )
            if receipt_binding is None:
                response_format = (
                    "json"
                    if isinstance(result.get("content"), Mapping)
                    else "text"
                )
                receipt_binding = self._primary_repair_receipt_binding(
                    admission=admission,
                    prompt=prompt,
                    input_payload=input_payload,
                    api_config=api_config,
                    schema_hash=schema_hash,
                    metadata=metadata,
                    route=route,
                    result=result,
                    response_format=response_format,
                )
            else:
                binding_check = self._primary_repair_receipt_binding(
                    admission=admission,
                    prompt=prompt,
                    input_payload=input_payload,
                    api_config=api_config,
                    schema_hash=schema_hash,
                    metadata=metadata,
                    route=route,
                    result=result,
                    response_format=str(receipt_binding.get("response_format") or ""),
                )
                if binding_check != receipt_binding:
                    raise ProviderRuntimeContractError(
                        "PRIMARY raw response binding changed before normalization"
                    )
            if receipt_binding["input_hash"] != primary_scope.request_hash:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair receipt input differs from its reserved request hash"
                )
            response_ref = self.primary_repair_response_journal.persist_response(
                primary_scope,
                aggregate_reservation.reservation_id,
                result,
                receipt_binding=receipt_binding,
            )
        if self.aggregate_budget is not None and aggregate_reservation is not None:
            try:
                if response_ref is None:
                    aggregate_snapshot = self.aggregate_budget.complete(
                        aggregate_reservation,
                        result,
                    )
                else:
                    aggregate_snapshot = self.aggregate_budget.complete(
                        aggregate_reservation,
                        result,
                        primary_repair_response_ref=response_ref,
                    )
            except BaseException as exc:
                # The provider response must still become durable.  Keep the
                # reservation live so a later resume can retry accounting
                # without issuing another provider request.
                aggregate_error = exc
            else:
                with self._lock:
                    self._aggregate_reservations.pop(admission.sequence, None)
        receipt_metadata = self._snapshot_receipt_metadata(
            metadata, api_config, str(result.get("endpoint_type") or self.endpoint_type),
        )
        receipt_metadata["local_budget_reservation_tokens"] = reservation_tokens
        receipt_metadata["local_budget_usage_status"] = (
            "reported" if result_total_tokens is not None else "unknown"
        )
        receipt_metadata["accounting_status"] = "pending" if aggregate_error else "committed"
        if aggregate_error is not None:
            receipt_metadata["accounting_error"] = _redact_text(aggregate_error)
        if (
            result_total_tokens is not None
            and self.budget.max_total_tokens
            and result_total_tokens > self.budget.max_total_tokens
        ):
            receipt_metadata["local_budget_accounting_conflict"] = True
        if aggregate_snapshot is not None:
            receipt_metadata["aggregate_budget"] = aggregate_snapshot
        if aggregate_reservation is not None:
            receipt_metadata["aggregate_reservation_id"] = aggregate_reservation.reservation_id
        if primary_scope is not None:
            receipt_metadata["primary_repair_scope_id"] = primary_scope.scope_id
            receipt_metadata["primary_repair_scope_fingerprint_sha256"] = (
                primary_scope.fingerprint_sha256
            )
            if response_ref is not None:
                receipt_metadata["primary_repair_response_ref"] = response_ref.to_dict()
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
            if aggregate_error is None:
                self._reserved_tokens = max(
                    0,
                    self._reserved_tokens
                    - admission.estimated_tokens
                    - admission.estimated_output_tokens,
                )
                if result_total_tokens is None:
                    self._unknown_exposure_tokens += reservation_tokens
                else:
                    self._consumed_tokens += result_total_tokens
            if self.ledger is not None:
                self.ledger.append(receipt)
            self._receipts.append(receipt)
            if aggregate_error is None:
                self._accounted_sequences.add(admission.sequence)
                self._primary_repair_scopes.pop(admission.sequence, None)
        # Capture after the receipt is durably appended and local accounting is
        # recorded, but outside the runtime accounting lock. A failed capture
        # never turns that known call into a new admission or refunds it.
        try:
            self._capture_response_snapshot(receipt, result.get("content"))
        except ProviderRuntimeContractError as exc:
            if aggregate_error is not None:
                raise aggregate_error from exc
            raise
        if aggregate_error is not None:
            raise aggregate_error
        return receipt

    def mark_transport_started(self, admission: ProviderCallAdmissionV1) -> None:
        """Durably mark the boundary immediately before a transport call."""

        try:
            self._assert_pause_state_before_admission()
        except ProviderCancellationRequested:
            self.release_unstarted(admission)
            raise
        if self.aggregate_budget is None or admission.aggregate_reservation_id is None:
            return
        with self._lock:
            reservation = self._aggregate_reservations.get(admission.sequence)
            primary_scope = self._primary_repair_scopes.get(admission.sequence)
        if reservation is None:
            raise ProviderRuntimeContractError(
                f"aggregate reservation is missing for admission sequence {admission.sequence}"
            )
        if primary_scope is not None:
            # PRIMARY's physical POST marker is recorded by the adapter's
            # attempt-start callback after local request preparation. The
            # executor's earlier call marks the transport phase only.
            return
        self.aggregate_budget.mark_transport_started(reservation)

    def primary_provider_attempt_started_callback(
        self,
        admission: ProviderCallAdmissionV1,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
    ) -> Callable[[int], str]:
        """Bind each pre-POST PRIMARY physical attempt to its reservation."""

        normalized_scope = ProviderBudgetController._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        response_journal = self.primary_repair_response_journal
        aggregate_budget = self.aggregate_budget
        if (
            aggregate_budget is None
            or response_journal is None
            or admission.aggregate_reservation_id is None
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempt tracking requires its aggregate reservation"
            )
        with self._lock:
            registered_scope = self._primary_repair_scopes.get(admission.sequence)
            reservation = self._aggregate_reservations.get(admission.sequence)
        if (
            registered_scope is None
            or registered_scope.fingerprint_sha256 != normalized_scope.fingerprint_sha256
            or reservation is None
            or reservation.reservation_id != admission.aggregate_reservation_id
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY physical attempt callback does not match the live admission"
            )

        def mark_attempt_started(attempt_number: int) -> str:
            with self._lock:
                receipt_binding = self._primary_repair_receipt_bindings.get(
                    admission.sequence
                )
            if receipt_binding is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY physical attempt has no prebound receipt identity"
                )
            spool_path = response_journal.persist_attempt_intent(
                normalized_scope,
                reservation.reservation_id,
                attempt_number,
                receipt_binding=receipt_binding,
            )
            aggregate_budget.mark_primary_repair_attempt_started(
                reservation,
                normalized_scope,
                attempt_number,
            )
            return spool_path

        def bind_response_headers(
            attempt_number: int,
            http_status: int | None,
            content_type: str,
            content_length: int | None,
            content_encoding: str,
            transfer_encoding: str,
        ) -> None:
            response_journal.bind_attempt_response_headers(
                normalized_scope,
                reservation.reservation_id,
                attempt_number,
                http_status=http_status,
                content_type=content_type,
                content_length=content_length,
                content_encoding=content_encoding,
                transfer_encoding=transfer_encoding,
            )

        setattr(mark_attempt_started, "response_headers", bind_response_headers)
        return mark_attempt_started

    def release_unstarted(self, admission: ProviderCallAdmissionV1) -> bool:
        """Release a reservation when transport is proven not to have started."""

        if self.aggregate_budget is None or admission.aggregate_reservation_id is None:
            return False
        with self._lock:
            reservation = self._aggregate_reservations.get(admission.sequence)
        if reservation is None:
            return False
        released = self.aggregate_budget.release_unstarted(reservation)
        if released:
            with self._lock:
                self._aggregate_reservations.pop(admission.sequence, None)
                self._primary_repair_scopes.pop(admission.sequence, None)
                self._reserved_tokens = max(
                    0,
                    self._reserved_tokens
                    - admission.estimated_tokens
                    - admission.estimated_output_tokens,
                )
        return released

    def recover_primary_repair_response(
        self,
        scope: PrimaryRepairScopeV1 | Mapping[str, Any],
        *,
        prompt: str,
        input_payload: Any,
        api_config: Mapping[str, Any],
    ) -> tuple[PrimaryRepairResponseRefV1, dict[str, Any]] | None:
        """Recover a journaled PRIMARY response and append its durable receipt.

        A missing journal leaves the scope untouched so the caller can report
        the original ambiguity. A retained response never becomes a new
        provider admission. The receipt is reconstructed from the receipt
        binding written beside the response before aggregate accounting.
        """

        normalized_scope = ProviderBudgetController._coerce_primary_repair_scope(scope)
        assert normalized_scope is not None
        if self.aggregate_budget is None or self.primary_repair_response_journal is None:
            raise ProviderRuntimeContractError(
                "PRIMARY repair recovery requires its aggregate budget and response journal"
            )
        state = self.aggregate_budget.lookup_primary_repair_scope(normalized_scope)
        if state is None:
            return None
        reservation_id = str(state.get("reservation_id") or "")
        stored_journal_root = str(state.get("response_journal_root") or "").strip()
        if not stored_journal_root and isinstance(state.get("response_ref"), Mapping):
            stored_journal_root = str(
                Path(str(state["response_ref"].get("path") or "")).expanduser().resolve().parent
            )
        if (
            stored_journal_root
            and Path(stored_journal_root).expanduser().resolve()
            != self.primary_repair_response_journal.root.resolve()
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY repair response journal differs from its durable reservation binding"
            )
        entry = self.primary_repair_response_journal.load_entry_for_scope(
            normalized_scope,
            reservation_id=reservation_id,
        )
        if entry is None:
            return None
        response_ref = entry.reference
        response = dict(entry.response)
        receipt_binding = dict(entry.receipt_binding)
        if receipt_binding.get("schema_version") != "provider-primary-repair-receipt-binding/v1":
            raise ProviderRuntimeContractError(
                "PRIMARY repair response journal has no supported receipt binding"
            )
        expected_binding = {
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "stage_name": self.stage_name,
            "route": self.route,
            "node_id": self.node_id,
            "call_id": self.call_id,
            "closure_epoch_id": self.closure_epoch_id,
            "logical_attempt_identity": self.logical_attempt_identity,
            "schema_hash": self.schema_hash,
            "endpoint_type": self.endpoint_type,
            "test_only": self.test_only,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
        }
        if any(receipt_binding.get(key) != value for key, value in expected_binding.items()):
            raise ProviderRuntimeContractError(
                "PRIMARY repair receipt binding differs from the current runtime identity"
            )
        input_hash = hash_json(input_payload)
        prompt_hash = hash_text(prompt)
        config_hash = hash_json(_redact_mapping(api_config))
        if (
            input_hash != normalized_scope.request_hash
            or receipt_binding.get("input_hash") != input_hash
            or receipt_binding.get("prompt_hash") != prompt_hash
            or receipt_binding.get("config_hash") != config_hash
            or receipt_binding.get("schema_hash") != normalized_scope.schema_hash
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY repair response receipt binding does not match the current request"
            )
        if entry.entry_kind == "ambiguous_raw_response":
            raise ProviderRuntimeContractError(
                "incomplete or non-200 PRIMARY raw response remains outcome-unknown"
            )
        if entry.entry_kind in {"raw_response", "semantic_raw_response"}:
            raw_spool = entry.raw_spool
            if raw_spool is None:
                raise ProviderRuntimeContractError(
                    "PRIMARY raw response journal entry lost its spool"
                )
            physical_attempts_started = int(
                state.get("physical_attempts_started") or raw_spool.attempt_number
            )
            expected_scope_status = (
                "raw_semantically_complete"
                if entry.entry_kind == "semantic_raw_response"
                else "raw_retained"
            )
            if (
                str(state.get("status") or "") != expected_scope_status
                or raw_spool.attempt_number != physical_attempts_started
            ):
                raise ProviderRuntimeContractError(
                    "PRIMARY raw response is stale relative to the latest physical attempt"
                )
            response = self._reparse_primary_repair_raw_response(entry)
            response_ref = self.primary_repair_response_journal.persist_response(
                normalized_scope,
                reservation_id,
                response,
                receipt_binding=receipt_binding,
            )
            response = self.primary_repair_response_journal.load(
                response_ref,
                scope=normalized_scope,
                reservation_id=reservation_id,
            )
        elif entry.entry_kind != "normalized_response":
            raise ProviderRuntimeContractError(
                "PRIMARY repair response journal entry kind cannot be recovered"
            )
        try:
            admission_payload = receipt_binding.get("admission")
            if not isinstance(admission_payload, Mapping):
                raise TypeError("admission is missing")
            remaining_calls_value = admission_payload.get("remaining_calls")
            remaining_tokens_value = admission_payload.get("remaining_tokens")
            admission = ProviderCallAdmissionV1(
                sequence=int(admission_payload.get("sequence") or 0),
                estimated_tokens=int(admission_payload.get("estimated_tokens") or 0),
                admitted_at=str(admission_payload.get("admitted_at") or ""),
                remaining_calls=(
                    None
                    if remaining_calls_value is None
                    else int(remaining_calls_value)
                ),
                remaining_tokens=(
                    None
                    if remaining_tokens_value is None
                    else int(remaining_tokens_value)
                ),
                estimated_output_tokens=int(
                    admission_payload.get("estimated_output_tokens") or 0
                ),
                reserved_call_attempts=int(
                    admission_payload.get("reserved_call_attempts") or 0
                ),
                reserved_retry_attempts=int(
                    admission_payload.get("reserved_retry_attempts") or 0
                ),
                aggregate_reservation_id=str(
                    admission_payload.get("aggregate_reservation_id") or ""
                ),
            )
            receipt_budget = ProviderBudgetV1.from_mapping(receipt_binding.get("budget"))
        except (TypeError, ValueError, ProviderRuntimeContractError) as exc:
            raise ProviderRuntimeContractError(
                "PRIMARY repair receipt admission binding is invalid"
            ) from exc
        if (
            admission.sequence < 1
            or not admission.admitted_at
            or admission.aggregate_reservation_id != response_ref.reservation_id
            or receipt_binding.get("budget") != self.budget.to_dict()
            or receipt_binding.get("provider")
            != str(api_config.get("provider_family") or api_config.get("provider") or "generic")
            or receipt_binding.get("model") != str(api_config.get("model") or "")
            or receipt_binding.get("endpoint") != str(api_config.get("api_base") or "")
        ):
            raise ProviderRuntimeContractError(
                "PRIMARY repair receipt admission identity is inconsistent"
            )
        status = str(state.get("status") or "")
        if status in {
            "transport_started",
            "raw_retained",
            "raw_semantically_complete",
            "response_retained",
        }:
            if status == "response_retained" and state.get("response_ref") != response_ref.to_dict():
                raise ProviderRuntimeContractError(
                    "response-retained PRIMARY scope has a different journal reference"
                )
            self.aggregate_budget.recover_primary_repair_response(
                normalized_scope,
                response_ref,
                response,
                journal=self.primary_repair_response_journal,
            )
        elif status == "completed":
            if state.get("response_ref") != response_ref.to_dict():
                raise ProviderRuntimeContractError(
                    "completed PRIMARY repair scope has a different response reference"
                )
        elif status == "reserved":
            raise ProviderRuntimeContractError(
                "PRIMARY repair response exists before its transport-started reservation"
            )
        else:
            raise ProviderRuntimeContractError(
                "PRIMARY repair response scope is ambiguous and requires operator reconciliation"
            )
        if self.ledger is None:
            raise ProviderRuntimeContractError(
                "PRIMARY repair recovery requires the bound provider receipt ledger"
            )
        existing_by_call = [
            item
            for item in self.ledger.list_receipts()
            if item.job_id == self.job_id
            and item.call_id == self.call_id
            and item.logical_attempt_identity == self.logical_attempt_identity
        ]
        matching_receipts = []
        for item in existing_by_call:
            item_metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
            if (
                item_metadata.get("primary_repair_scope_id") == normalized_scope.scope_id
                and item_metadata.get("primary_repair_scope_fingerprint_sha256")
                == normalized_scope.fingerprint_sha256
                and item_metadata.get("primary_repair_response_ref") == response_ref.to_dict()
                and item.prompt_hash == prompt_hash
                and item.input_hash == input_hash
                and item.config_hash == config_hash
            ):
                matching_receipts.append(item)
        if len(matching_receipts) > 1:
            raise ProviderRuntimeContractError(
                "PRIMARY repair response has duplicate durable receipts"
            )
        if existing_by_call and not matching_receipts:
            raise ProviderRuntimeContractError(
                "PRIMARY repair call already has a receipt with a different response binding"
            )
        if matching_receipts:
            receipt = matching_receipts[0]
            expected_response_hash = (
                hash_json(response.get("content"))
                if receipt.status == "success" and response.get("content") is not None
                else None
            )
            if receipt.response_hash != expected_response_hash:
                raise ProviderRuntimeContractError(
                    "PRIMARY repair receipt response differs from its journal"
                )
        else:
            base_metadata = receipt_binding.get("receipt_metadata")
            if not isinstance(base_metadata, Mapping):
                raise ProviderRuntimeContractError(
                    "PRIMARY repair receipt metadata binding is invalid"
                )
            receipt_metadata = dict(base_metadata)
            result_total_tokens = _optional_nonnegative_int(response.get("total_tokens"))
            if result_total_tokens is None:
                input_tokens = _optional_nonnegative_int(response.get("input_tokens"))
                output_tokens = _optional_nonnegative_int(response.get("output_tokens"))
                if input_tokens is not None and output_tokens is not None:
                    result_total_tokens = input_tokens + output_tokens
            receipt_metadata.update(
                {
                    "accounting_status": "committed",
                    "accounting_recovered_from_response_journal": True,
                    "local_budget_reservation_tokens": (
                        admission.estimated_tokens + admission.estimated_output_tokens
                    ),
                    "local_budget_usage_status": (
                        "reported" if result_total_tokens is not None else "unknown"
                    ),
                    "aggregate_reservation_id": response_ref.reservation_id,
                    "primary_repair_scope_id": normalized_scope.scope_id,
                    "primary_repair_scope_fingerprint_sha256": (
                        normalized_scope.fingerprint_sha256
                    ),
                    "primary_repair_response_ref": response_ref.to_dict(),
                }
            )
            if (
                entry.raw_spool is not None
                and entry.raw_spool.completion_proof == "semantic_complete_only"
            ):
                receipt_metadata.update(
                    {
                        "provider_http_body_complete": False,
                        "provider_content_semantically_complete": True,
                        "provider_http_completion_proof": "semantic_complete_only",
                        "local_budget_usage_status": "unknown",
                    }
                )
            receipt = ProviderCallReceiptV1.from_result(
                admission=admission,
                job_id=str(receipt_binding.get("job_id") or ""),
                attempt_id=str(receipt_binding.get("attempt_id") or ""),
                stage_name=str(receipt_binding.get("stage_name") or ""),
                route=str(receipt_binding.get("route") or ""),
                provider=str(receipt_binding.get("provider") or ""),
                model=str(receipt_binding.get("model") or ""),
                endpoint=str(receipt_binding.get("endpoint") or ""),
                prompt_hash=str(receipt_binding.get("prompt_hash") or ""),
                input_hash=str(receipt_binding.get("input_hash") or ""),
                config_hash=str(receipt_binding.get("config_hash") or ""),
                schema_hash=str(receipt_binding.get("schema_hash") or ""),
                result=response,
                budget=receipt_budget,
                started_at=admission.admitted_at,
                finished_at=entry.created_at,
                metadata=receipt_metadata,
                node_id=str(receipt_binding.get("node_id") or ""),
                call_id=str(receipt_binding.get("call_id") or ""),
                closure_epoch_id=str(receipt_binding.get("closure_epoch_id") or ""),
                logical_attempt_identity=str(
                    receipt_binding.get("logical_attempt_identity") or ""
                ),
                endpoint_type=str(receipt_binding.get("endpoint_type") or ""),
                test_only=bool(receipt_binding.get("test_only")),
                prompt_id=str(receipt_binding.get("prompt_id") or ""),
                prompt_version=str(receipt_binding.get("prompt_version") or ""),
                prompt_sha256=str(receipt_binding.get("prompt_sha256") or ""),
            )
            stable_receipt_id = "provider-receipt-" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        normalized_scope.scope_id,
                        response_ref.reservation_id,
                        response_ref.record_sha256,
                    )
                ),
            ).hex
            receipt = replace(receipt, receipt_id=stable_receipt_id)
            receipt = self.ledger.append(receipt)
        with self._lock:
            if all(item.receipt_id != receipt.receipt_id for item in self._receipts):
                self._receipts.append(receipt)
            self._calls = max(self._calls, admission.sequence)
            self._account_existing_receipt(receipt, admission)
        # This content comes from the verified PRIMARY response journal, after
        # stable receipt-ID assignment, not from an expanded output artifact.
        self._capture_response_snapshot(receipt, response.get("content"))
        return response_ref, response

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
                remaining_tokens=(
                    self.budget.max_total_tokens
                    - self._consumed_tokens
                    - self._unknown_exposure_tokens
                    - self._reserved_tokens
                )
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
    "acceptance_context_environment",
    "acceptance_execution_context_from_environment",
    "build_provider_run_scope_hashes_v1",
    "ProviderBudgetExceeded",
    "ProviderCancellationRequested",
    "ProviderAggregateBudgetV1",
    "ProviderAggregateBudgetV2",
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
    "ProviderRunPlanBindingV1",
    "ProviderRunScopeAuthorizationV1",
    "ProviderRunScopeProofV1",
    "SharedRetryProjectionV1",
    "ProcessIdentityV1",
    "ProcessLiveness",
    "canonical_provider_request_payload",
    "bind_acceptance_execution_context",
    "bind_provider_run_scope_authorization",
    "bind_provider_run_scope_proof",
    "bind_provider_run_budget",
    "bind_provider_run_plan_binding",
    "compute_closure_epoch_id",
    "current_acceptance_execution_context",
    "hash_json",
    "hash_text",
    "is_process_alive",
    "process_identity_for_pid",
    "process_liveness",
    "provider_budget_controller_from_environment",
    "provider_aggregate_budget_from_mapping",
    "provider_run_budget_controller_from_environment",
    "provider_run_plan_binding_from_verified_acceptance_plan",
    "provider_run_scope_payload_from_plan_binding",
    "provider_run_scope_authorization_from_verified_artifact",
    "provider_run_scope_proof_from_verified_artifact",
    "provider_run_workspace_identity_sha256",
    "project_shared_retry_attempts_v1",
    "project_shared_retry_slots_v1",
    "provider_request_input_hash",
    "stable_provider_hash",
    "validate_provider_run_scope_authority",
    "validate_provider_run_scope_authorization",
    "validate_provider_run_plan_binding",
    "validate_started_acceptance_execution_context",
]

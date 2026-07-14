"""Job-scoped provider circuit state for preprocessing backends."""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class CircuitSnapshot:
    provider: str
    open: bool
    reason: str = ""
    status_code: int | None = None


class ProviderCircuitOpen(RuntimeError):
    def __init__(self, message: str, *, snapshot: CircuitSnapshot):
        super().__init__(message)
        self.snapshot = snapshot


class ProviderCircuitBreaker:
    def __init__(self, provider: str):
        self.provider = provider
        self._lock = threading.RLock()
        self._snapshot = CircuitSnapshot(provider=provider, open=False)

    @property
    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            return self._snapshot

    def open(self, *, reason: str, status_code: int | None = None) -> CircuitSnapshot:
        with self._lock:
            if not self._snapshot.open:
                self._snapshot = CircuitSnapshot(self.provider, True, str(reason), status_code)
            return self._snapshot

    def ensure_closed(self) -> None:
        snapshot = self.snapshot
        if snapshot.open:
            raise ProviderCircuitOpen(
                f"{snapshot.provider} circuit is open: {snapshot.reason}",
                snapshot=snapshot,
            )

"""Structured progress tracking shared by CLI-triggered and GUI-triggered runs."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict


class ProgressTracker:
    """Thread-safe progress state container."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {
            "task_type": "",
            "stage": "",
            "status": "idle",
            "current": 0,
            "total": 0,
            "message": "",
            "item_label": "",
            "retry_round": 0,
            "retry_total_rounds": 0,
            "indeterminate": False,
            "success_count": 0,
            "failure_count": 0,
            "remaining_count": 0,
            "elapsed_seconds": 0.0,
            "started_at": 0.0,
            "updated_at": 0.0,
        }

    def reset(self, *, task_type: str, stage: str, message: str = "", indeterminate: bool = False) -> None:
        now = time.time()
        with self._lock:
            self._snapshot = {
                "task_type": task_type,
                "stage": stage,
                "status": "running",
                "current": 0,
                "total": 0,
                "message": message,
                "item_label": "",
                "retry_round": 0,
                "retry_total_rounds": 0,
                "indeterminate": indeterminate,
                "success_count": 0,
                "failure_count": 0,
                "remaining_count": 0,
                "elapsed_seconds": 0.0,
                "started_at": now,
                "updated_at": now,
            }

    def emit(self, **kwargs: Any) -> None:
        now = time.time()
        with self._lock:
            if not self._snapshot.get("started_at"):
                self._snapshot["started_at"] = now
            self._snapshot.update(kwargs)
            self._snapshot["updated_at"] = now
            self._snapshot["elapsed_seconds"] = max(0.0, now - float(self._snapshot.get("started_at") or now))
            total = int(self._snapshot.get("total") or 0)
            current = int(self._snapshot.get("current") or 0)
            if total > 0 and "remaining_count" not in kwargs:
                self._snapshot["remaining_count"] = max(total - current, 0)

    def finish(self, *, success: bool, message: str = "") -> None:
        total = int(self._snapshot.get("total") or 0)
        current = int(self._snapshot.get("current") or 0)
        final_current = total if success and total > 0 else current
        final_remaining = 0 if success and total > 0 else max(total - final_current, 0)
        self.emit(
            status="completed" if success else "failed",
            message=message or self._snapshot.get("message", ""),
            indeterminate=False,
            current=final_current,
            remaining_count=final_remaining,
        )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if snapshot.get("status") == "running":
            started_at = float(snapshot.get("started_at") or 0.0)
            if started_at > 0:
                snapshot["elapsed_seconds"] = max(0.0, time.time() - started_at)
        return snapshot

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Iterable, Sequence

from services.artifact_registry import ArtifactRecord, ArtifactRegistry
from services.job_outcome import (
    ATTEMPT_ARTIFACT_TYPE,
    ATTEMPT_ARTIFACT_VERSION,
    AttemptStatus,
    AttemptV1,
    append_attempt_snapshot,
    interrupt_stale_running_and_start_next,
)


ATTEMPT_SNAPSHOT_ROLE = "job_attempt_snapshot"
ATTEMPT_SNAPSHOT_DIR = "job_attempts"


class AttemptStoreCorruption(ValueError):
    """Raised when durable attempt snapshots are missing, reordered, or invalid."""


@dataclass(frozen=True)
class StartedAttempt:
    attempt: AttemptV1
    recovered_attempt: AttemptV1 | None = None


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


class AttemptStore:
    """Persist immutable attempt-state snapshots and validate their transition chain."""

    def __init__(self, workspace: object, registry: ArtifactRegistry) -> None:
        self.workspace = workspace
        self.registry = registry
        self.directory = Path(str(getattr(workspace, "artifact_path")(ATTEMPT_SNAPSHOT_DIR)))

    @staticmethod
    def _artifact_id(snapshot: AttemptV1, sequence: int) -> str:
        return f"job-attempt:{snapshot.attempt_id}:{sequence:06d}:{snapshot.status}"

    def _snapshot_path(self, sequence: int) -> Path:
        return self.directory / f"snapshot-{sequence:06d}.json"

    def load_history(self) -> tuple[AttemptV1, ...]:
        if not self.directory.exists():
            return ()
        paths = sorted(self.directory.glob("snapshot-*.json"))
        history: tuple[AttemptV1, ...] = ()
        for expected_sequence, path in enumerate(paths, start=1):
            if path.name != f"snapshot-{expected_sequence:06d}.json":
                raise AttemptStoreCorruption(
                    f"attempt snapshot sequence has a gap at {expected_sequence}: {path.name}"
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise AttemptStoreCorruption(f"cannot read attempt snapshot {path}: {exc}") from exc
            if not isinstance(payload, dict):
                raise AttemptStoreCorruption(f"attempt snapshot must be an object: {path}")
            if int(payload.get("snapshot_sequence") or 0) != expected_sequence:
                raise AttemptStoreCorruption(f"attempt snapshot sequence mismatch: {path}")
            try:
                snapshot = AttemptV1.from_dict(payload)
                history = append_attempt_snapshot(history, snapshot)
            except (TypeError, ValueError) as exc:
                raise AttemptStoreCorruption(f"invalid attempt snapshot {path}: {exc}") from exc
        return history

    def append(self, snapshot: AttemptV1) -> ArtifactRecord:
        history = self.load_history()
        validated = append_attempt_snapshot(history, snapshot)
        sequence = len(validated)
        path = self._snapshot_path(sequence)
        payload = snapshot.to_dict()
        payload["snapshot_sequence"] = sequence
        _write_json_exclusive(path, payload)
        return self.registry.register_file(
            artifact_role=ATTEMPT_SNAPSHOT_ROLE,
            artifact_type=ATTEMPT_ARTIFACT_TYPE,
            artifact_version=ATTEMPT_ARTIFACT_VERSION,
            path=path,
            producer="runtime.attempt_store.AttemptStore",
            artifact_id=self._artifact_id(snapshot, sequence),
            metadata={
                "attempt_id": snapshot.attempt_id,
                "attempt_number": snapshot.attempt_number,
                "attempt_status": snapshot.status,
                "snapshot_sequence": sequence,
            },
        )

    def start(self, *, job_id: str, producer: str) -> StartedAttempt:
        history = self.load_history()
        recovered: AttemptV1 | None = None
        if not history:
            pending = AttemptV1.new_pending(job_id=job_id, attempt_number=1, producer=producer)
            self.append(pending)
        else:
            last = history[-1]
            if last.job_id != job_id:
                raise AttemptStoreCorruption(
                    f"attempt history belongs to {last.job_id!r}, not {job_id!r}"
                )
            if last.status == "running":
                recovered, pending = interrupt_stale_running_and_start_next(last, producer=producer)
                self.append(recovered)
                self.append(pending)
            elif last.status == "pending":
                pending = last
            elif last.is_terminal:
                pending = AttemptV1.new_pending(
                    job_id=job_id,
                    attempt_number=last.attempt_number + 1,
                    producer=producer,
                    resumed_from_attempt=last.attempt_number,
                )
                self.append(pending)
            else:  # pragma: no cover - AttemptV1 currently makes this unreachable.
                raise AttemptStoreCorruption(f"unsupported attempt state: {last.status}")
        running = pending.transition("running")
        self.append(running)
        return StartedAttempt(attempt=running, recovered_attempt=recovered)

    def finish(
        self,
        running_attempt: AttemptV1,
        status: AttemptStatus,
        *,
        reason: str = "",
    ) -> AttemptV1:
        if status not in {"succeeded", "failed", "cancelled", "blocked", "interrupted"}:
            raise ValueError(f"attempt finish requires a terminal status, got {status!r}")
        history = self.load_history()
        if not history or history[-1] != running_attempt:
            raise AttemptStoreCorruption("only the durable running attempt head may be finished")
        terminal = running_attempt.transition(status, reason=reason)
        self.append(terminal)
        return terminal

    def register_orphaned_snapshots(self) -> tuple[ArtifactRecord, ...]:
        """Register valid snapshot files left behind by a crash before Registry commit."""

        history = self.load_history()
        repaired: list[ArtifactRecord] = []
        for sequence, snapshot in enumerate(history, start=1):
            artifact_id = self._artifact_id(snapshot, sequence)
            if self.registry.get(artifact_id) is not None:
                continue
            repaired.append(
                self.registry.register_file(
                    artifact_role=ATTEMPT_SNAPSHOT_ROLE,
                    artifact_type=ATTEMPT_ARTIFACT_TYPE,
                    artifact_version=ATTEMPT_ARTIFACT_VERSION,
                    path=self._snapshot_path(sequence),
                    producer="runtime.attempt_store.AttemptStore.register_orphaned_snapshots",
                    artifact_id=artifact_id,
                    metadata={
                        "attempt_id": snapshot.attempt_id,
                        "attempt_number": snapshot.attempt_number,
                        "attempt_status": snapshot.status,
                        "snapshot_sequence": sequence,
                    },
                )
            )
        return tuple(repaired)


def terminal_attempts(history: Sequence[AttemptV1]) -> tuple[AttemptV1, ...]:
    return tuple(snapshot for snapshot in history if snapshot.is_terminal)


def attempt_ids(history: Iterable[AttemptV1]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(snapshot.attempt_id for snapshot in history))

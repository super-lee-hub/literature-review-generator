"""Immutable checkpoints around paid Validation adjudication calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from services.job_workspace import atomic_write_json, utc_now_iso
from validation.edge_checkpoint import canonical_hash
from validation.run_result import VALIDATION_RUN_SCHEMA_VERSION


ADJUDICATION_CHECKPOINT_VERSION = "v1"
ADJUDICATION_PROMPT_VERSION = "validation_adjudication_prompt_v1"


def sanitized_route_hash(api_config: Mapping[str, Any]) -> str:
    secret_fragments = ("key", "token", "secret", "password", "authorization")
    sanitized = {
        str(key): value
        for key, value in api_config.items()
        if not any(fragment in str(key).lower() for fragment in secret_fragments)
    }
    return canonical_hash(sanitized)


class AdjudicationCheckpointStore:
    def __init__(self, root_dir: str | os.PathLike[str]):
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def key_for(self, *, packet: Mapping[str, Any], stage: str, route_hash: str) -> str:
        return canonical_hash(
            {
                "artifact_version": ADJUDICATION_CHECKPOINT_VERSION,
                "packet": packet,
                "stage": stage,
                "route_hash": route_hash,
                "prompt_version": ADJUDICATION_PROMPT_VERSION,
                "adjudication_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
            }
        )

    def load(self, key: str) -> dict[str, Any] | None:
        path = self.root_dir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            payload.get("artifact_type") != "validation_adjudication_checkpoint"
            or payload.get("artifact_version") != ADJUDICATION_CHECKPOINT_VERSION
            or payload.get("checkpoint_key") != key
        ):
            return None
        result = payload.get("result")
        return dict(result) if isinstance(result, Mapping) else None

    def save(self, key: str, result: Mapping[str, Any]) -> tuple[str, bool]:
        path = self.root_dir / f"{key}.json"
        with self._lock:
            existing = self.load(key)
            if existing is not None:
                if canonical_hash(existing) != canonical_hash(result):
                    raise ValueError("immutable adjudication checkpoint collision")
                return str(path), False
            atomic_write_json(
                str(path),
                {
                    "artifact_type": "validation_adjudication_checkpoint",
                    "artifact_version": ADJUDICATION_CHECKPOINT_VERSION,
                    "checkpoint_key": key,
                    "result": dict(result),
                    "created_at": utc_now_iso(),
                },
            )
        return str(path), True

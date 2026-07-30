"""Durable Validation checkpoints at the claim-unit by paper edge."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from services.job_workspace import atomic_write_json, utc_now_iso
from validation.run_result import VALIDATION_RUN_SCHEMA_VERSION


VALIDATION_EDGE_CHECKPOINT_VERSION = "v1"
DEFAULT_RETRIEVAL_CONFIG_VERSION = "bilingual_retrieval_v2"
DEFAULT_PROMPT_VERSION = "validation_edge_prompt_v1"
DEFAULT_ADJUDICATION_SCHEMA_VERSION = VALIDATION_RUN_SCHEMA_VERSION


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_or_value_hash(path: str, fallback: Any = None) -> str:
    if path and os.path.isfile(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    return canonical_hash(fallback)


@dataclass(frozen=True)
class ValidationEdgeKeyV1:
    claim_unit_hash: str
    canonical_paper_key: str
    evidence_hashes: tuple[str, ...]
    segmenter_version: str
    retrieval_config_hash: str
    model_route: str
    prompt_version: str = DEFAULT_PROMPT_VERSION
    adjudication_schema_version: str = DEFAULT_ADJUDICATION_SCHEMA_VERSION
    version: str = VALIDATION_EDGE_CHECKPOINT_VERSION

    @property
    def checkpoint_key(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "claim_unit_hash": self.claim_unit_hash,
            "canonical_paper_key": self.canonical_paper_key,
            "evidence_hashes": list(self.evidence_hashes),
            "segmenter_version": self.segmenter_version,
            "retrieval_config_hash": self.retrieval_config_hash,
            "model_route": self.model_route,
            "prompt_version": self.prompt_version,
            "adjudication_schema_version": self.adjudication_schema_version,
        }


class ValidationEdgeCheckpointStore:
    """One atomic file per edge avoids lost updates under parallel validation."""

    def __init__(self, root_dir: str | os.PathLike[str]):
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path_for(self, key: ValidationEdgeKeyV1) -> Path:
        return self.root_dir / f"{key.checkpoint_key}.json"

    def load(self, key: ValidationEdgeKeyV1) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("artifact_type") != "validation_edge_checkpoint":
            return None
        if payload.get("artifact_version") != VALIDATION_EDGE_CHECKPOINT_VERSION:
            return None
        if payload.get("checkpoint_key") != key.checkpoint_key:
            return None
        if payload.get("key") != key.to_dict():
            return None
        result = payload.get("result")
        return dict(result) if isinstance(result, Mapping) else None

    def save(self, key: ValidationEdgeKeyV1, result: Mapping[str, Any]) -> tuple[str, bool]:
        path = self.path_for(key)
        payload = {
            "artifact_type": "validation_edge_checkpoint",
            "artifact_version": VALIDATION_EDGE_CHECKPOINT_VERSION,
            "checkpoint_key": key.checkpoint_key,
            "key": key.to_dict(),
            "result": dict(result),
            "created_at": utc_now_iso(),
        }
        with self._lock:
            existing = self.load(key)
            if existing is not None:
                if canonical_hash(existing) != canonical_hash(result):
                    raise ValueError("immutable validation edge checkpoint collision")
                return str(path), False
            atomic_write_json(str(path), payload)
        return str(path), True

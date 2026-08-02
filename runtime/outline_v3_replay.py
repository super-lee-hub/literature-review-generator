"""Exact-match model-call replay store for Outline Intelligence v3."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from runtime.attempt_store import _write_json_exclusive
from services.job_workspace import utc_now_iso

from outline.v3_models import compute_v3_hash


MODEL_REPLAY_ARTIFACT_TYPE = "outline_v3_model_call_replay"
MODEL_REPLAY_ARTIFACT_VERSION = "v1"
MODEL_REPLAY_DIR = "outline_v3"
MODEL_REPLAY_FILENAME = "model_call_replay.jsonl"


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _stable_unique(values: Iterable[Any]) -> List[str]:
    result: Dict[str, str] = {}
    for value in values:
        text = _safe_text(value)
        if text:
            result.setdefault(text.casefold(), text)
    return [result[key] for key in sorted(result)]


def _stable_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): value[key] for key in sorted(value, key=lambda item: str(item))}


@dataclass(frozen=True)
class ModelCallReplayKey:
    """All bindings that must match before a model response can be reused."""

    node_id: str
    node_version: str
    schema_version: str
    model_route: str
    model_name: str
    provider: str
    prompt_template_hash: str
    prompt_payload_hash: str
    input_artifact_hashes: List[str] = field(default_factory=list)
    config_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_version": self.node_version,
            "schema_version": self.schema_version,
            "model_route": self.model_route,
            "model_name": self.model_name,
            "provider": self.provider,
            "prompt_template_hash": self.prompt_template_hash,
            "prompt_payload_hash": self.prompt_payload_hash,
            "input_artifact_hashes": _stable_unique(self.input_artifact_hashes),
            "config_hash": self.config_hash,
        }

    @property
    def key_hash(self) -> str:
        return compute_v3_hash(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelCallReplayKey":
        return cls(
            node_id=str(data.get("node_id") or ""),
            node_version=str(data.get("node_version") or ""),
            schema_version=str(data.get("schema_version") or ""),
            model_route=str(data.get("model_route") or ""),
            model_name=str(data.get("model_name") or ""),
            provider=str(data.get("provider") or ""),
            prompt_template_hash=str(data.get("prompt_template_hash") or ""),
            prompt_payload_hash=str(data.get("prompt_payload_hash") or ""),
            input_artifact_hashes=_stable_unique(data.get("input_artifact_hashes") or []),
            config_hash=str(data.get("config_hash") or ""),
        )


@dataclass(frozen=True)
class ModelCallReplayRecord:
    replay_id: str
    key: ModelCallReplayKey
    status: str = "succeeded"
    output_hash: str = ""
    output_artifact_ids: List[str] = field(default_factory=list)
    receipt_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": MODEL_REPLAY_ARTIFACT_TYPE,
            "artifact_version": MODEL_REPLAY_ARTIFACT_VERSION,
            "replay_id": self.replay_id,
            "key": self.key.to_dict(),
            "status": self.status,
            "output_hash": self.output_hash,
            "output_artifact_ids": _stable_unique(self.output_artifact_ids),
            "receipt_ids": _stable_unique(self.receipt_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelCallReplayRecord":
        key_payload = data.get("key")
        return cls(
            replay_id=str(data.get("replay_id") or ""),
            key=ModelCallReplayKey.from_dict(key_payload if isinstance(key_payload, Mapping) else {}),
            status=str(data.get("status") or "succeeded"),
            output_hash=str(data.get("output_hash") or ""),
            output_artifact_ids=_stable_unique(data.get("output_artifact_ids") or []),
            receipt_ids=_stable_unique(data.get("receipt_ids") or []),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


@dataclass(frozen=True)
class ReplayLookup:
    status: str
    record: Optional[ModelCallReplayRecord] = None
    stale_reasons: List[str] = field(default_factory=list)

    @property
    def reusable(self) -> bool:
        return self.status == "reusable" and self.record is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "record": self.record.to_dict() if self.record is not None else None,
            "stale_reasons": _stable_unique(self.stale_reasons),
            "reusable": self.reusable,
        }


def _key_mismatch_reasons(expected: ModelCallReplayKey, actual: ModelCallReplayKey) -> List[str]:
    comparisons = (
        ("node_version", "node_version_changed"),
        ("schema_version", "schema_version_changed"),
        ("model_route", "route_changed"),
        ("model_name", "model_changed"),
        ("provider", "provider_changed"),
        ("prompt_template_hash", "prompt_template_changed"),
        ("prompt_payload_hash", "prompt_payload_changed"),
        ("input_artifact_hashes", "input_artifacts_changed"),
        ("config_hash", "config_changed"),
    )
    return [reason for field, reason in comparisons if getattr(expected, field) != getattr(actual, field)]


class ModelCallReplayStore:
    """Append-only JSONL replay records with stale-key diagnostics."""

    def __init__(self, workspace: Any) -> None:
        if hasattr(workspace, "artifact_path"):
            self.path = Path(str(workspace.artifact_path(f"{MODEL_REPLAY_DIR}/{MODEL_REPLAY_FILENAME}")))
        else:
            self.path = Path(str(workspace)) / "artifacts" / MODEL_REPLAY_DIR / MODEL_REPLAY_FILENAME

    def _read_records(self) -> List[ModelCallReplayRecord]:
        if not self.path.exists():
            return []
        records: List[ModelCallReplayRecord] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid model replay JSONL at line {line_number}: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"model replay line {line_number} must be an object")
            record = ModelCallReplayRecord.from_dict(payload)
            if record.replay_id != f"replay:{record.key.key_hash}":
                raise ValueError(f"model replay id mismatch at line {line_number}")
            records.append(record)
        return records

    def lookup(self, key: ModelCallReplayKey) -> ReplayLookup:
        records = self._read_records()
        exact = [record for record in records if record.replay_id == f"replay:{key.key_hash}" and record.key == key]
        if exact:
            record = exact[-1]
            if record.status == "succeeded" and record.output_hash:
                return ReplayLookup(status="reusable", record=record)
            return ReplayLookup(status="stale", record=record, stale_reasons=["replay_record_not_successful_or_output_missing"])

        node_records = [record for record in records if record.key.node_id == key.node_id]
        if not node_records:
            return ReplayLookup(status="missing")
        reasons: List[str] = []
        for record in node_records:
            reasons.extend(_key_mismatch_reasons(key, record.key))
        return ReplayLookup(status="stale", stale_reasons=_stable_unique(reasons or ["replay_key_mismatch"]))

    def append(
        self,
        key: ModelCallReplayKey,
        *,
        output_hash: str,
        output_artifact_ids: Sequence[str] = (),
        receipt_ids: Sequence[str] = (),
    ) -> ModelCallReplayRecord:
        if not output_hash:
            raise ValueError("a replay record requires output_hash")
        record = ModelCallReplayRecord(
            replay_id=f"replay:{key.key_hash}",
            key=key,
            output_hash=output_hash,
            output_artifact_ids=_stable_unique(output_artifact_ids),
            receipt_ids=_stable_unique(receipt_ids),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
        return record

    def put(self, *args: Any, **kwargs: Any) -> ModelCallReplayRecord:
        return self.append(*args, **kwargs)


ModelCallReplayKeyV1 = ModelCallReplayKey
ModelCallReplayRecordV1 = ModelCallReplayRecord
ReplayLookupV1 = ReplayLookup


__all__ = [
    "MODEL_REPLAY_ARTIFACT_TYPE",
    "MODEL_REPLAY_ARTIFACT_VERSION",
    "ModelCallReplayKey",
    "ModelCallReplayRecord",
    "ReplayLookup",
    "ModelCallReplayStore",
    "ModelCallReplayKeyV1",
    "ModelCallReplayRecordV1",
    "ReplayLookupV1",
]

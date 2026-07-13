"""Versioned health sidecar for Outline Intelligence v2.

The sidecar deliberately lives outside the existing Outline v2 artifacts so
their schemas remain stable while adoption can fail closed on provider health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence


OUTLINE_STAGE_HEALTH_TYPE = "outline_stage_health"
OUTLINE_STAGE_HEALTH_VERSION = "v1"
_FALLBACK_PROVENANCE = {"deterministic_fallback", "deterministic_topup"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def content_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StageHealthEntryV1:
    stage_name: str
    provider_route: str
    execution_status: str
    schema_valid: bool
    attempts: int
    input_hashes: tuple[str, ...]
    output_hashes: tuple[str, ...]
    fallback_provenance: str = "provider"
    degraded_reason: str = ""
    prompt_budget: Mapping[str, Any] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return bool(
            self.execution_status != "succeeded"
            or not self.schema_valid
            or self.degraded_reason
            or self.fallback_provenance in _FALLBACK_PROVENANCE
        )

    @property
    def adoption_eligible(self) -> bool:
        return not self.degraded

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "provider_route": self.provider_route,
            "execution_status": self.execution_status,
            "schema_valid": self.schema_valid,
            "attempts": self.attempts,
            "input_hashes": list(self.input_hashes),
            "output_hashes": list(self.output_hashes),
            "fallback_provenance": self.fallback_provenance,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "adoption_eligible": self.adoption_eligible,
            "prompt_budget": dict(self.prompt_budget),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StageHealthEntryV1":
        return cls(
            stage_name=str(value.get("stage_name") or ""),
            provider_route=str(value.get("provider_route") or ""),
            execution_status=str(value.get("execution_status") or ""),
            schema_valid=bool(value.get("schema_valid")),
            attempts=int(value.get("attempts") or 0),
            input_hashes=tuple(str(item) for item in value.get("input_hashes") or ()),
            output_hashes=tuple(str(item) for item in value.get("output_hashes") or ()),
            fallback_provenance=str(value.get("fallback_provenance") or "provider"),
            degraded_reason=str(value.get("degraded_reason") or ""),
            prompt_budget=dict(value.get("prompt_budget") or {}),
        )


@dataclass(frozen=True)
class OutlineStageHealthV1:
    job_id: str
    execution_mode: str
    stages: tuple[StageHealthEntryV1, ...]
    source_final_outline_hash: str
    source_coverage_audit_hash: str
    created_at: str = field(default_factory=_utc_now_iso)
    artifact_type: str = OUTLINE_STAGE_HEALTH_TYPE
    artifact_version: str = OUTLINE_STAGE_HEALTH_VERSION

    @property
    def degradation_reasons(self) -> tuple[str, ...]:
        return tuple(
            entry.degraded_reason or f"{entry.stage_name}:{entry.fallback_provenance}"
            for entry in self.stages
            if entry.degraded
        )

    @property
    def adoptable(self) -> bool:
        if not self.stages:
            return False
        if self.execution_mode not in {"production", "test_dev"}:
            return False
        return all(entry.adoption_eligible for entry in self.stages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "job_id": self.job_id,
            "execution_mode": self.execution_mode,
            "stages": [entry.to_dict() for entry in self.stages],
            "adoptable": self.adoptable,
            "degradation_reasons": list(self.degradation_reasons),
            "source_final_outline_hash": self.source_final_outline_hash,
            "source_coverage_audit_hash": self.source_coverage_audit_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutlineStageHealthV1":
        if str(value.get("artifact_type") or "") != OUTLINE_STAGE_HEALTH_TYPE:
            raise ValueError("not an outline_stage_health artifact")
        if str(value.get("artifact_version") or "") != OUTLINE_STAGE_HEALTH_VERSION:
            raise ValueError("unsupported outline_stage_health version")
        result = cls(
            job_id=str(value.get("job_id") or ""),
            execution_mode=str(value.get("execution_mode") or ""),
            stages=tuple(StageHealthEntryV1.from_dict(item) for item in value.get("stages") or ()),
            source_final_outline_hash=str(value.get("source_final_outline_hash") or ""),
            source_coverage_audit_hash=str(value.get("source_coverage_audit_hash") or ""),
            created_at=str(value.get("created_at") or ""),
        )
        if bool(value.get("adoptable")) != result.adoptable:
            raise ValueError("outline_stage_health adoptable projection is inconsistent")
        return result


class StageHealthCollector:
    """Collect logical provider calls without changing provider interfaces."""

    def __init__(self, model_caller: Callable[[str, str, Dict[str, Any]], Any] | None):
        self._model_caller = model_caller
        self._calls: list[dict[str, Any]] = []

    def call(self, route: str, prompt: str, metadata: Dict[str, Any]) -> Any:
        stage = str(metadata.get("stage") or "outline_v2")
        call = {
            "stage": stage,
            "route": route,
            "input_hash": content_hash(prompt),
            "output_hash": "",
            "status": "failed",
            "reason": "",
            "prompt_budget": dict(metadata.get("prompt_budget") or {}),
        }
        self._calls.append(call)
        if self._model_caller is None:
            call["reason"] = "model_caller_missing"
            raise RuntimeError(f"Outline stage {stage} requires a model caller")
        try:
            result = self._model_caller(route, prompt, metadata)
            call["output_hash"] = content_hash(result)
            call["status"] = "succeeded"
            return result
        except BaseException as exc:
            call["reason"] = f"{type(exc).__name__}:{exc}"
            raise

    def entry(
        self,
        stage_name: str,
        route: str,
        *,
        schema_valid: bool,
        fallback_provenance: str = "provider",
        degraded_reason: str = "",
    ) -> StageHealthEntryV1:
        calls = [item for item in self._calls if item["stage"] == stage_name]
        status = "succeeded" if calls and all(item["status"] == "succeeded" for item in calls) else "failed"
        reasons = [str(item["reason"]) for item in calls if item["reason"]]
        budget = calls[-1]["prompt_budget"] if calls else {}
        return StageHealthEntryV1(
            stage_name=stage_name,
            provider_route=route,
            execution_status=status,
            schema_valid=schema_valid,
            attempts=len(calls),
            input_hashes=tuple(str(item["input_hash"]) for item in calls),
            output_hashes=tuple(str(item["output_hash"]) for item in calls if item["output_hash"]),
            fallback_provenance=fallback_provenance,
            degraded_reason=degraded_reason or ";".join(reasons),
            prompt_budget=budget,
        )

    def has_calls(self, stage_name: str) -> bool:
        return any(item["stage"] == stage_name for item in self._calls)


def make_test_double_entry(
    stage_name: str, route: str, input_value: Any, output_value: Any
) -> StageHealthEntryV1:
    return StageHealthEntryV1(
        stage_name=stage_name,
        provider_route=route,
        execution_status="succeeded",
        schema_valid=True,
        attempts=0,
        input_hashes=(content_hash(input_value),),
        output_hashes=(content_hash(output_value),),
        fallback_provenance="test_double",
    )

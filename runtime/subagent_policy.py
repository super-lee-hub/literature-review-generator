from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class ExecutionMode(str, Enum):
    SUBAGENT = "subagent"
    LOCAL = "local"


@dataclass(frozen=True)
class StageExecutionPolicy:
    stage_name: str
    execution_mode: ExecutionMode
    max_concurrency: int
    total_attempt_budget: int
    quality_retry_budget: int = 0
    legacy_api_path_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["execution_mode"] = self.execution_mode.value
        return payload


DEFAULT_STAGE_POLICIES: dict[str, StageExecutionPolicy] = {
    "source_intake": StageExecutionPolicy(
        stage_name="source_intake",
        execution_mode=ExecutionMode.LOCAL,
        max_concurrency=1,
        total_attempt_budget=1,
    ),
    "stage1_analyze": StageExecutionPolicy(
        stage_name="stage1_analyze",
        execution_mode=ExecutionMode.SUBAGENT,
        max_concurrency=4,
        total_attempt_budget=2,
        quality_retry_budget=1,
    ),
    "stage1_derive": StageExecutionPolicy(
        stage_name="stage1_derive",
        execution_mode=ExecutionMode.LOCAL,
        max_concurrency=1,
        total_attempt_budget=1,
        legacy_api_path_allowed=False,
    ),
    "stage2_outline": StageExecutionPolicy(
        stage_name="stage2_outline",
        execution_mode=ExecutionMode.SUBAGENT,
        max_concurrency=1,
        total_attempt_budget=2,
    ),
    "stage3_review": StageExecutionPolicy(
        stage_name="stage3_review",
        execution_mode=ExecutionMode.SUBAGENT,
        max_concurrency=3,
        total_attempt_budget=2,
    ),
    "stage4_validate": StageExecutionPolicy(
        stage_name="stage4_validate",
        execution_mode=ExecutionMode.LOCAL,
        max_concurrency=1,
        total_attempt_budget=1,
    ),
}


def stage_policy_for(stage_name: str) -> StageExecutionPolicy:
    try:
        return DEFAULT_STAGE_POLICIES[stage_name]
    except KeyError as exc:  # pragma: no cover - defensive guard
        raise ValueError(f"unsupported stage_name: {stage_name}") from exc


def classify_stage_failure(message: str | None) -> str:
    text = str(message or "").lower()
    if not text:
        return "unknown"
    if any(token in text for token in ("path", "missing", "not found", "invalid input")):
        return "input"
    if any(token in text for token in ("timeout", "network", "transport", "429", "rate limit")):
        return "transport"
    if any(token in text for token in ("json", "schema", "parse", "serialization")):
        return "schema"
    if any(token in text for token in ("quality", "unsupported", "weak support", "empty summary")):
        return "quality"
    return "unknown"


def build_runtime_stage_trace_entry(
    *,
    stage_name: str,
    step_name: str,
    producer: str,
    subagent_run_id: str | None = None,
    legacy_api_path_used: bool = False,
    metadata: Dict[str, Any] | None = None,
    execution_mode: ExecutionMode | str | None = None,
) -> Dict[str, Any]:
    policy = stage_policy_for(stage_name)
    resolved_execution_mode = execution_mode.value if isinstance(execution_mode, ExecutionMode) else execution_mode
    return {
        "stage_name": stage_name,
        "step_name": step_name,
        "execution_mode": resolved_execution_mode or policy.execution_mode.value,
        "producer": producer,
        "subagent_run_id": subagent_run_id,
        "legacy_api_path_used": legacy_api_path_used,
        "metadata": dict(metadata or {}),
    }

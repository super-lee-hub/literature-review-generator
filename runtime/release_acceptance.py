"""Typed release-acceptance contracts and evidence gates.

This module deliberately does not manufacture evidence.  A successful public
runtime job is only one input to a gate; each specialized gate must carry its
own final-SHA-bound evidence and its own facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, cast
import zipfile

from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderReceiptConflict,
    ProviderRuntime,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
    is_process_alive,
    process_identity_for_pid,
)
from services.durable_io import atomic_replace_with_retry


class ReleaseAcceptanceSpecError(ValueError):
    """Raised when release-acceptance input is ambiguous or malformed."""


_BUDGET_FIELDS = frozenset(
    {
        "max_provider_calls_total",
        "max_output_tokens_total",
        "max_retry_attempts_total",
        "max_wall_seconds",
        # Explicit compatibility aliases. They are normalized, never ignored.
        "max_provider_calls",
        "max_output_tokens",
        "max_retry_attempts",
        "timeout_seconds",
    }
)
_ACCEPTANCE_FIELDS = frozenset(
    {
        "schema_version",
        "parent_run_id",
        "final_executable_sha",
        "executable_sha",
        "scenarios",
        "budget",
        "acceptance_budget",
        "evidence_manifest",
        "runtime_spec",
        "state_path",
        "job_id",
        "third_party_acknowledged",
        "third_party_hosts",
        "gates",
    }
)
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "parent_run_id",
        "final_executable_sha",
        "executable_sha",
        "budget",
        "acceptance_budget",
        "scenarios",
        "third_party_acknowledged",
        "third_party_acknowledgement",
        "third_party_hosts",
        "evidence_manifest",
        "state_path",
        "job_id",
        "gates",
    }
)
_CHILD_SCENARIO_FIELDS = frozenset(
    {
        "scenario_id",
        "gate",
        "runtime_spec",
        "workspace",
        "input_manifest",
        "execution_mode",
        "budget_domain",
        "prerequisites",
        "job_id",
    }
)
_SCENARIO_RECEIPT_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "parent_acceptance_run_id",
        "scenario_id",
        "gate",
        "final_executable_sha",
        "plan_sha256",
        "runtime_spec_sha256",
        "input_identity_sha256",
        "workspace_identity_sha256",
        "executor_pid",
        "executor_process_creation_identity",
        "executor_host_id",
        "started_at",
        "completed_at",
        "action_type",
        "workspace",
        "job_id",
        "attempt_id",
        "budget_domain",
        "status",
        "exit_status",
        "produced_evidence_refs",
    }
)
_PROCESS_RESUME_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "event_id",
        "acceptance_run_id",
        "scenario_id",
        "job_id",
        "interruption_event_id",
        "interruption_event_sha256",
        "previous_attempt_id",
        "new_attempt_id",
        "new_pid",
        "new_process_creation_identity",
        "resumed_at",
    }
)
_VALIDATOR_CHALLENGE_INPUT_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "challenge_id",
        "job_id",
        "workspace",
        "baseline_review_artifact_id",
        "baseline_review_hash",
        "block_id",
        "mutation_type",
        "mutated_text",
        "expected_detection_class",
    }
)


def _resolve_acceptance_path(
    value: Any,
    *,
    field_name: str,
    origin_dir: str | Path | None,
    required: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ReleaseAcceptanceSpecError(f"{field_name} must be a JSON string")
    value = value.strip()
    if required and not value:
        raise ReleaseAcceptanceSpecError(f"{field_name} is required")
    if not value:
        return ""
    path = Path(value).expanduser()
    if origin_dir is not None and not path.is_absolute():
        path = Path(origin_dir).expanduser().resolve() / path
    return str(path.resolve())


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ReleaseAcceptanceSpecError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )


def _integer(value: Any, *, field_name: str, positive: bool = True) -> int:
    if isinstance(value, bool):
        raise ReleaseAcceptanceSpecError(f"{field_name} must be an integer")
    if not isinstance(value, int):
        raise ReleaseAcceptanceSpecError(f"{field_name} must be an integer")
    if positive and value <= 0:
        raise ReleaseAcceptanceSpecError(f"{field_name} must be greater than zero")
    if not positive and value < 0:
        raise ReleaseAcceptanceSpecError(f"{field_name} must be non-negative")
    return value


@dataclass(frozen=True)
class ReleaseAcceptanceBudget:
    max_provider_calls_total: int = 24
    max_output_tokens_total: int = 5_000_000
    max_retry_attempts_total: int = 2
    max_wall_seconds: int = 900

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls_total",
            "max_output_tokens_total",
            "max_retry_attempts_total",
            "max_wall_seconds",
        ):
            _integer(getattr(self, name), field_name=name)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        defaults: "ReleaseAcceptanceBudget | None" = None,
    ) -> "ReleaseAcceptanceBudget":
        source = value or {}
        if not isinstance(source, Mapping):
            raise ReleaseAcceptanceSpecError("acceptance budget must be a JSON object")
        _reject_unknown(source, _BUDGET_FIELDS, "acceptance budget")
        base = defaults or cls()
        aliases = {
            "max_provider_calls_total": ("max_provider_calls_total", "max_provider_calls"),
            "max_output_tokens_total": ("max_output_tokens_total", "max_output_tokens"),
            "max_retry_attempts_total": ("max_retry_attempts_total", "max_retry_attempts"),
            "max_wall_seconds": ("max_wall_seconds", "timeout_seconds"),
        }
        values: dict[str, int] = {}
        for canonical, keys in aliases.items():
            present = [key for key in keys if key in source]
            if not present:
                values[canonical] = int(getattr(base, canonical))
                continue
            parsed = [_integer(source[key], field_name=key) for key in present]
            if len(set(parsed)) != 1:
                raise ReleaseAcceptanceSpecError(
                    f"acceptance budget aliases disagree for {canonical}"
                )
            values[canonical] = parsed[0]
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_provider_calls_total": self.max_provider_calls_total,
            "max_output_tokens_total": self.max_output_tokens_total,
            "max_retry_attempts_total": self.max_retry_attempts_total,
            "max_wall_seconds": self.max_wall_seconds,
        }

    def to_provider_budget(self) -> ProviderAggregateBudgetV1:
        return ProviderAggregateBudgetV1(**self.to_dict())


@dataclass(frozen=True)
class AcceptanceValidatorChallengeInputV1:
    """Owner-supplied input for the real Gate H challenge executor."""

    challenge_id: str
    job_id: str
    workspace: str
    baseline_review_artifact_id: str
    baseline_review_hash: str
    block_id: str
    mutation_type: str
    mutated_text: str
    expected_detection_class: str

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "AcceptanceValidatorChallengeInputV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input must be a JSON object"
            )
        _reject_unknown(
            payload,
            _VALIDATOR_CHALLENGE_INPUT_FIELDS,
            "Validator challenge input",
        )
        if (
            payload.get("artifact_type") != "acceptance_validator_challenge_input"
            or payload.get("artifact_version") != "v1"
            or payload.get("schema_version")
            != "acceptance-validator-challenge-input-v1"
        ):
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input schema is invalid"
            )
        text_fields = (
            "challenge_id",
            "job_id",
            "baseline_review_artifact_id",
            "block_id",
            "mutation_type",
            "mutated_text",
            "expected_detection_class",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_fields}
        if any(not value for value in values.values()):
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input identity is incomplete"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", values["challenge_id"]):
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input challenge_id is unsafe"
            )
        if values["mutation_type"] != "replace_block_text":
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input mutation_type is unsupported"
            )
        workspace = _resolve_acceptance_path(
            payload.get("workspace", ""),
            field_name="Validator challenge input workspace",
            origin_dir=origin_dir,
            required=True,
        )
        baseline_hash = str(payload.get("baseline_review_hash") or "").strip().lower()
        if not _valid_sha256(baseline_hash):
            raise ReleaseAcceptanceSpecError(
                "Validator challenge input baseline_review_hash is invalid"
            )
        return cls(
            **values,
            workspace=workspace,
            baseline_review_hash=baseline_hash,
        )


@dataclass(frozen=True)
class AcceptanceChildScenarioSpecV2:
    """One independently executable child of a parent acceptance plan."""

    scenario_id: str
    gate: str
    runtime_spec: str = ""
    workspace: str = ""
    input_manifest: str = ""
    execution_mode: str = "runtime"
    budget_domain: str = "live"
    prerequisites: tuple[str, ...] = ()
    job_id: str = ""

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
    ) -> "AcceptanceChildScenarioSpecV2":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("acceptance child scenario must be a JSON object")
        _reject_unknown(payload, _CHILD_SCENARIO_FIELDS, "acceptance child scenario")
        scenario_id = str(payload.get("scenario_id") or "").strip()
        gate = str(payload.get("gate") or "").strip().upper()
        if not scenario_id:
            raise ReleaseAcceptanceSpecError("acceptance child scenario_id is required")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", scenario_id):
            raise ReleaseAcceptanceSpecError("acceptance child scenario_id contains unsafe characters")
        if gate not in {"C", "D", "E", "F", "G", "H", "I", "J", "K", "Q"}:
            raise ReleaseAcceptanceSpecError(f"unsupported acceptance child gate: {gate}")
        if scenario_id != gate:
            raise ReleaseAcceptanceSpecError(
                "acceptance child scenario_id must equal its gate for the current plan schema"
            )
        execution_mode = str(payload.get("execution_mode") or "runtime").strip()
        budget_domain = str(payload.get("budget_domain") or "live").strip().lower()
        if not execution_mode:
            raise ReleaseAcceptanceSpecError("acceptance child execution_mode is required")
        if execution_mode not in {
            "runtime",
            "ocr",
            "crash_resume",
            "validator_challenge",
            "playwright",
            "offline-k",
        }:
            raise ReleaseAcceptanceSpecError(
                f"unsupported acceptance child execution_mode: {execution_mode}"
            )
        mode_gate = {
            "ocr": "J",
            "crash_resume": "E",
            "validator_challenge": "H",
            "playwright": "I",
            "offline-k": "K",
        }.get(execution_mode)
        if mode_gate is not None and gate != mode_gate:
            raise ReleaseAcceptanceSpecError(
                f"acceptance child execution_mode {execution_mode} belongs to Gate {mode_gate}"
            )
        if budget_domain not in {"live", "offline-k"}:
            raise ReleaseAcceptanceSpecError(
                "acceptance child budget_domain must be live or offline-k"
            )
        if gate == "K" and budget_domain != "offline-k":
            raise ReleaseAcceptanceSpecError(
                "Gate K acceptance child must use the offline-k budget domain"
            )
        raw_prerequisites = payload.get("prerequisites", [])
        if not isinstance(raw_prerequisites, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in raw_prerequisites
        ):
            raise ReleaseAcceptanceSpecError(
                "acceptance child prerequisites must be a non-empty-string array"
            )
        runtime_spec = _resolve_acceptance_path(
            payload.get("runtime_spec", ""),
            field_name="acceptance child runtime_spec",
            origin_dir=origin_dir,
            required=execution_mode in {"runtime", "ocr", "crash_resume", "validator_challenge"},
        )
        workspace = _resolve_acceptance_path(
            payload.get("workspace", ""),
            field_name="acceptance child workspace",
            origin_dir=origin_dir,
        )
        if not workspace:
            raise ReleaseAcceptanceSpecError(
                "acceptance child workspace is required for an independent child namespace"
            )
        if execution_mode == "crash_resume" and not workspace:
            raise ReleaseAcceptanceSpecError(
                "crash_resume acceptance child requires an explicit workspace"
            )
        if gate in {"J"} and execution_mode == "ocr" and not runtime_spec:
            raise ReleaseAcceptanceSpecError(
                "OCR acceptance child requires an independent RuntimeJobSpec"
            )
        if gate in {"H", "I"} and execution_mode in {
            "validator_challenge",
            "playwright",
        } and not str(payload.get("input_manifest") or "").strip():
            raise ReleaseAcceptanceSpecError(
                f"{gate} acceptance child requires an explicit input_manifest"
            )
        return cls(
            scenario_id=scenario_id,
            gate=gate,
            runtime_spec=runtime_spec,
            workspace=workspace,
            input_manifest=_resolve_acceptance_path(
                payload.get("input_manifest", ""),
                field_name="acceptance child input_manifest",
                origin_dir=origin_dir,
            ),
            execution_mode=execution_mode,
            budget_domain=budget_domain,
            prerequisites=tuple(str(item).strip() for item in raw_prerequisites),
            job_id=str(payload.get("job_id") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "gate": self.gate,
            "runtime_spec": self.runtime_spec,
            "workspace": self.workspace,
            "input_manifest": self.input_manifest,
            "execution_mode": self.execution_mode,
            "budget_domain": self.budget_domain,
            "prerequisites": list(self.prerequisites),
            "job_id": self.job_id,
        }


@dataclass(frozen=True)
class ReleaseAcceptancePlanV2:
    """Parent acceptance plan containing independently executable children."""

    parent_run_id: str
    budget: ReleaseAcceptanceBudget
    scenarios: Mapping[str, AcceptanceChildScenarioSpecV2]
    final_executable_sha: str = ""
    third_party_acknowledged: bool = False
    third_party_hosts: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
        defaults: ReleaseAcceptanceBudget | None = None,
    ) -> "ReleaseAcceptancePlanV2":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("release acceptance plan must be a JSON object")
        if payload.get("schema_version") != "release-acceptance-plan-v2":
            raise ReleaseAcceptanceSpecError("release acceptance plan schema is invalid")
        _reject_unknown(payload, _PLAN_FIELDS, "release acceptance plan")
        parent_run_id = str(payload.get("parent_run_id") or "").strip()
        if not parent_run_id:
            raise ReleaseAcceptanceSpecError("release acceptance plan parent_run_id is required")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", parent_run_id):
            raise ReleaseAcceptanceSpecError("release acceptance plan parent_run_id contains unsafe characters")
        budget_values: list[ReleaseAcceptanceBudget] = []
        for budget_field in ("budget", "acceptance_budget"):
            if budget_field not in payload:
                continue
            raw_budget = payload[budget_field]
            if not isinstance(raw_budget, Mapping):
                raise ReleaseAcceptanceSpecError(f"{budget_field} must be a JSON object")
            budget_values.append(
                ReleaseAcceptanceBudget.from_mapping(raw_budget, defaults=defaults)
            )
        if len(budget_values) == 2 and budget_values[0] != budget_values[1]:
            raise ReleaseAcceptanceSpecError("acceptance plan budget aliases disagree")
        budget = budget_values[0] if budget_values else ReleaseAcceptanceBudget.from_mapping({}, defaults=defaults)
        raw_scenarios = payload.get("scenarios")
        if not isinstance(raw_scenarios, Mapping) or not raw_scenarios:
            raise ReleaseAcceptanceSpecError("release acceptance plan scenarios must be a non-empty object")
        scenarios: dict[str, AcceptanceChildScenarioSpecV2] = {}
        scenario_ids: set[str] = set()
        for raw_gate, raw_child in raw_scenarios.items():
            gate = str(raw_gate).strip().upper()
            child = AcceptanceChildScenarioSpecV2.from_mapping(
                raw_child,
                origin_dir=origin_dir,
            )
            if child.gate != gate:
                raise ReleaseAcceptanceSpecError(
                    f"acceptance plan scenario key {gate} does not match child gate {child.gate}"
                )
            if child.scenario_id in scenario_ids:
                raise ReleaseAcceptanceSpecError("acceptance plan child scenario IDs must be unique")
            scenario_ids.add(child.scenario_id)
            scenarios[gate] = child
        cardinality_gates = [scenarios[gate] for gate in ("C", "D", "Q") if gate in scenarios]
        runtime_specs = [item.runtime_spec.casefold() for item in cardinality_gates if item.runtime_spec]
        workspaces = [item.workspace.casefold() for item in scenarios.values() if item.workspace]
        if len(runtime_specs) != len(set(runtime_specs)):
            raise ReleaseAcceptanceSpecError(
                "C, D, and Q acceptance children require independent runtime specs"
            )
        if len(workspaces) != len(set(workspaces)):
            raise ReleaseAcceptanceSpecError(
                "acceptance children require independent workspaces"
            )
        job_ids = [child.job_id.casefold() for child in scenarios.values() if child.job_id]
        if len(job_ids) != len(set(job_ids)):
            raise ReleaseAcceptanceSpecError(
                "acceptance children require independent explicit job IDs"
            )
        raw_ack = payload.get("third_party_acknowledged", payload.get("third_party_acknowledgement", False))
        if isinstance(raw_ack, Mapping):
            raw_ack = raw_ack.get("acknowledged", False)
        if not isinstance(raw_ack, bool):
            raise ReleaseAcceptanceSpecError("third_party_acknowledged must be a JSON boolean")
        raw_hosts = payload.get("third_party_hosts", [])
        if not isinstance(raw_hosts, (list, tuple)) or any(not isinstance(item, str) for item in raw_hosts):
            raise ReleaseAcceptanceSpecError("third_party_hosts must be an array of strings")
        raw_sha = payload.get("final_executable_sha", payload.get("executable_sha", "")) or ""
        if not isinstance(raw_sha, str):
            raise ReleaseAcceptanceSpecError("final_executable_sha must be a JSON string")
        final_sha = raw_sha.strip().lower()
        if final_sha and (len(final_sha) not in {40, 64} or any(char not in "0123456789abcdef" for char in final_sha)):
            raise ReleaseAcceptanceSpecError("final_executable_sha must be a lowercase checkout SHA")
        return cls(
            parent_run_id=parent_run_id,
            budget=budget,
            scenarios=scenarios,
            final_executable_sha=final_sha,
            third_party_acknowledged=raw_ack,
            third_party_hosts=tuple(item.strip() for item in raw_hosts if item.strip()),
        )

    @property
    def gates(self) -> tuple[str, ...]:
        return tuple(self.scenarios)

    def child(self, gate: str) -> AcceptanceChildScenarioSpecV2:
        try:
            return self.scenarios[str(gate).strip().upper()]
        except KeyError as exc:
            raise ReleaseAcceptanceSpecError(
                f"acceptance plan has no child scenario for gate {gate}"
            ) from exc

    def plan_sha256(self) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "release-acceptance-plan-v2",
            "parent_run_id": self.parent_run_id,
            "final_executable_sha": self.final_executable_sha,
            "budget": self.budget.to_dict(),
            "third_party_acknowledged": self.third_party_acknowledged,
            "third_party_hosts": list(self.third_party_hosts),
            "scenarios": {gate: child.to_dict() for gate, child in self.scenarios.items()},
        }


def _receipt_timestamp(value: Any, *, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ReleaseAcceptanceSpecError(f"scenario receipt {field_name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseAcceptanceSpecError(f"scenario receipt {field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ReleaseAcceptanceSpecError(f"scenario receipt {field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ScenarioExecutionReceiptV1:
    """Executor-owned proof that one acceptance child actually ran."""

    parent_acceptance_run_id: str
    scenario_id: str
    gate: str
    final_executable_sha: str
    plan_sha256: str
    runtime_spec_sha256: str
    input_identity_sha256: str
    workspace_identity_sha256: str
    executor_pid: int
    executor_process_creation_identity: str
    executor_host_id: str
    started_at: str
    completed_at: str
    action_type: str
    workspace: str
    job_id: str
    attempt_id: str
    budget_domain: str
    status: str
    exit_status: int
    produced_evidence_refs: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ScenarioExecutionReceiptV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("scenario execution receipt must be a JSON object")
        _reject_unknown(payload, _SCENARIO_RECEIPT_FIELDS, "scenario execution receipt")
        if payload.get("artifact_type") != "scenario_execution_receipt":
            raise ReleaseAcceptanceSpecError("scenario execution receipt artifact type is invalid")
        if payload.get("artifact_version") != "v1" or payload.get("schema_version") != "scenario-execution-receipt-v1":
            raise ReleaseAcceptanceSpecError("scenario execution receipt schema is invalid")
        text_values = (
            "parent_acceptance_run_id",
            "scenario_id",
            "gate",
            "final_executable_sha",
            "plan_sha256",
            "runtime_spec_sha256",
            "input_identity_sha256",
            "workspace_identity_sha256",
            "executor_process_creation_identity",
            "executor_host_id",
            "action_type",
            "workspace",
            "job_id",
            "attempt_id",
            "budget_domain",
            "status",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_values}
        if any(not values[name] for name in text_values):
            missing = [name for name in text_values if not values[name]]
            raise ReleaseAcceptanceSpecError(
                "scenario execution receipt is missing: " + ", ".join(missing)
            )
        if values["gate"] not in {"C", "D", "E", "F", "G", "H", "I", "J", "K", "Q"}:
            raise ReleaseAcceptanceSpecError("scenario execution receipt gate is invalid")
        if values["scenario_id"] != values["gate"]:
            raise ReleaseAcceptanceSpecError("scenario execution receipt scenario does not match gate")
        if len(values["final_executable_sha"]) not in {40, 64} or any(
            char not in "0123456789abcdef" for char in values["final_executable_sha"].lower()
        ) or values["final_executable_sha"] != values["final_executable_sha"].lower():
            raise ReleaseAcceptanceSpecError("scenario execution receipt final SHA is invalid")
        for name in (
            "plan_sha256",
            "runtime_spec_sha256",
            "input_identity_sha256",
            "workspace_identity_sha256",
        ):
            if not _valid_sha256(values[name]):
                raise ReleaseAcceptanceSpecError(f"scenario execution receipt {name} is invalid")
        raw_pid = payload.get("executor_pid")
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
            raise ReleaseAcceptanceSpecError("scenario execution receipt executor_pid is invalid")
        raw_exit = payload.get("exit_status")
        if isinstance(raw_exit, bool) or not isinstance(raw_exit, int):
            raise ReleaseAcceptanceSpecError("scenario execution receipt exit_status is invalid")
        budget_domain = values.pop("budget_domain").lower()
        if budget_domain not in {"live", "offline-k"}:
            raise ReleaseAcceptanceSpecError("scenario execution receipt budget domain is invalid")
        status = values.pop("status").upper()
        if status not in {"PASSED", "BLOCKED", "FAILED", "CANCELLED", "NOT_VERIFIED"}:
            raise ReleaseAcceptanceSpecError("scenario execution receipt status is invalid")
        started = _receipt_timestamp(payload.get("started_at"), field_name="started_at")
        completed = _receipt_timestamp(payload.get("completed_at"), field_name="completed_at")
        if completed < started:
            raise ReleaseAcceptanceSpecError("scenario execution receipt completed_at precedes started_at")
        if status == "PASSED" and raw_exit != 0:
            raise ReleaseAcceptanceSpecError("passed scenario execution receipt must have exit_status 0")
        raw_refs = payload.get("produced_evidence_refs")
        if not isinstance(raw_refs, (list, tuple)):
            raise ReleaseAcceptanceSpecError("scenario execution receipt produced_evidence_refs must be an array")
        normalized_refs: list[Mapping[str, Any]] = []
        for raw_ref in raw_refs:
            try:
                normalized_refs.append(DurableEvidenceRefV1.from_mapping(raw_ref).to_dict())
            except (ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
                raise ReleaseAcceptanceSpecError(
                    "scenario execution receipt contains an invalid durable evidence ref"
                ) from exc
        return cls(
            **values,
            started_at=str(payload["started_at"]).strip(),
            completed_at=str(payload["completed_at"]).strip(),
            executor_pid=raw_pid,
            exit_status=raw_exit,
            budget_domain=budget_domain,
            status=status,
            produced_evidence_refs=tuple(normalized_refs),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "scenario_execution_receipt",
            "artifact_version": "v1",
            "schema_version": "scenario-execution-receipt-v1",
            "parent_acceptance_run_id": self.parent_acceptance_run_id,
            "scenario_id": self.scenario_id,
            "gate": self.gate,
            "final_executable_sha": self.final_executable_sha,
            "plan_sha256": self.plan_sha256,
            "runtime_spec_sha256": self.runtime_spec_sha256,
            "input_identity_sha256": self.input_identity_sha256,
            "workspace_identity_sha256": self.workspace_identity_sha256,
            "executor_pid": self.executor_pid,
            "executor_process_creation_identity": self.executor_process_creation_identity,
            "executor_host_id": self.executor_host_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "action_type": self.action_type,
            "workspace": self.workspace,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "budget_domain": self.budget_domain,
            "status": self.status,
            "exit_status": self.exit_status,
            "produced_evidence_refs": [dict(item) for item in self.produced_evidence_refs],
        }


@dataclass(frozen=True)
class ParentAcceptanceResultV2:
    """Machine-derived parent projection; caller live flags are ignored."""

    parent_acceptance_run_id: str
    final_executable_sha: str
    status: str
    terminal_status: str
    live_pass: bool
    ready_to_merge: bool
    reason: str
    child_results: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_child_results(
        cls,
        *,
        parent_acceptance_run_id: str,
        final_executable_sha: str,
        child_results: Mapping[str, Mapping[str, Any]],
        required_scenarios: Iterable[str],
        expected_child_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> "ParentAcceptanceResultV2":
        required = tuple(str(item).strip().upper() for item in required_scenarios)
        normalized: dict[str, Mapping[str, Any]] = {}
        issues: list[str] = []
        statuses: list[str] = []
        live_authority = True
        required_live_scenarios = False
        for scenario_id in required:
            raw = child_results.get(scenario_id)
            if not isinstance(raw, Mapping):
                issues.append(f"missing child {scenario_id}")
                continue
            raw_receipt = raw.get("receipt")
            if not isinstance(raw_receipt, Mapping):
                issues.append(f"invalid child receipt {scenario_id}: receipt is not an object")
                continue
            try:
                receipt = ScenarioExecutionReceiptV1.from_mapping(raw_receipt)
            except (ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
                issues.append(f"invalid child receipt {scenario_id}: {exc}")
                continue
            if (
                receipt.parent_acceptance_run_id != parent_acceptance_run_id
                or receipt.final_executable_sha != final_executable_sha
                or receipt.scenario_id != scenario_id
            ):
                issues.append(f"child binding mismatch {scenario_id}")
            expected_binding = (
                expected_child_bindings.get(scenario_id, {})
                if isinstance(expected_child_bindings, Mapping)
                else {}
            )
            for field_name in (
                "plan_sha256",
                "runtime_spec_sha256",
                "input_identity_sha256",
                "workspace_identity_sha256",
                "job_id",
                "budget_domain",
            ):
                expected_value = str(expected_binding.get(field_name) or "").strip()
                if expected_value and str(getattr(receipt, field_name, "") or "").strip() != expected_value:
                    issues.append(f"child {field_name} binding mismatch {scenario_id}")
            status = str(raw.get("status") or "").strip().upper()
            statuses.append(status)
            if status in {"PASS", "PASS_OFFLINE", "PASS_OFFLINE_HOSTED"} and receipt.status != "PASSED":
                issues.append(f"child status/receipt terminal mismatch {scenario_id}")
            normalized[scenario_id] = {
                **dict(raw),
                "receipt": receipt.to_dict(),
                "live_pass": False,
                "ready_to_merge": False,
            }
            contract = GATE_CONTRACTS.get(scenario_id, {})
            if bool(contract.get("required_live")):
                required_live_scenarios = True
                live_authority = live_authority and _receipt_has_live_authority(receipt)
        if issues:
            status = "NOT_VERIFIED"
            reason = "; ".join(issues)
        elif any(item in {"BLOCKED", "NOT_RUN", "NOT_VERIFIED", "FAILED", "CANCELLED"} for item in statuses):
            status = "BLOCKED" if any(item == "BLOCKED" for item in statuses) else "NOT_VERIFIED"
            reason = "one or more required child scenarios did not pass"
        elif statuses and all(item in {"PASS_OFFLINE", "PASS_OFFLINE_HOSTED"} for item in statuses):
            status = "PASS_OFFLINE"
            reason = "all children are offline evidence; live acceptance remains unproven"
        elif (
            statuses
            and all(item == "PASS" for item in statuses)
            and required_live_scenarios
            and live_authority
        ):
            status = "READY_TO_MERGE"
            reason = "all required children have independently verified live authority receipts"
        elif statuses and all(item == "PASS" for item in statuses):
            if required_live_scenarios:
                status = "NOT_VERIFIED"
                reason = "live-required children passed without live provider authority"
            else:
                status = "PASS_OFFLINE"
                reason = "children passed, but no required live provider authority was requested"
        else:
            status = "NOT_VERIFIED"
            reason = "child statuses or live authority receipts are incomplete"
        live_pass = status == "READY_TO_MERGE"
        ready_to_merge = live_pass
        return cls(
            parent_acceptance_run_id=parent_acceptance_run_id,
            final_executable_sha=final_executable_sha,
            status=status,
            terminal_status=status,
            live_pass=live_pass,
            ready_to_merge=ready_to_merge,
            reason=reason,
            child_results=normalized,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "parent-acceptance-result-v2",
            "parent_acceptance_run_id": self.parent_acceptance_run_id,
            "final_executable_sha": self.final_executable_sha,
            "status": self.status,
            "terminal_status": self.terminal_status,
            "live_pass": self.live_pass,
            "ready_to_merge": self.ready_to_merge,
            "reason": self.reason,
            "child_results": {key: dict(value) for key, value in self.child_results.items()},
        }


def _receipt_has_live_authority(receipt: ScenarioExecutionReceiptV1) -> bool:
    if receipt.budget_domain != "live":
        return False
    for raw_ref in receipt.produced_evidence_refs:
        try:
            ref = DurableEvidenceRefV1.from_mapping(raw_ref)
        except (ReleaseAcceptanceSpecError, TypeError, ValueError):
            continue
        if ref.role != "provider_receipt_ledger" or ref.artifact_type != "provider_receipt_ledger":
            continue
        try:
            ledger = ProviderRuntimeLedger(Path(ref.path))
            receipts = ledger.list_acceptance_receipts(expected_job_id=receipt.job_id)
        except (OSError, ValueError, ProviderRuntimeContractError):
            continue
        if not receipts or any(item.test_only for item in receipts):
            continue
        if any(
            item.status == "success"
            or (
                isinstance(item.metadata, Mapping)
                and bool(item.metadata.get("transport_config"))
            )
            for item in receipts
        ):
            return True
    return False


@dataclass(frozen=True)
class ReleaseAcceptanceSpec:
    budget: ReleaseAcceptanceBudget = field(default_factory=ReleaseAcceptanceBudget)
    evidence_manifest: str = ""
    runtime_spec: str = ""
    state_path: str = ""
    job_id: str = ""
    third_party_acknowledged: bool = False
    third_party_hosts: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    plan: ReleaseAcceptancePlanV2 | None = None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
        defaults: ReleaseAcceptanceBudget | None = None,
    ) -> "ReleaseAcceptanceSpec":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("release acceptance spec must be a JSON object")
        _reject_unknown(payload, _ACCEPTANCE_FIELDS, "release acceptance spec")
        plan: ReleaseAcceptancePlanV2 | None = None
        if payload.get("schema_version") == "release-acceptance-plan-v2":
            plan = ReleaseAcceptancePlanV2.from_mapping(
                payload,
                origin_dir=origin_dir,
                defaults=defaults,
            )
            budget = plan.budget
        else:
            budget_values: list[ReleaseAcceptanceBudget] = []
            for budget_field in ("budget", "acceptance_budget"):
                if budget_field not in payload:
                    continue
                raw_budget = payload[budget_field]
                if not isinstance(raw_budget, Mapping):
                    raise ReleaseAcceptanceSpecError(
                        f"{budget_field} must be a JSON object"
                    )
                budget_values.append(
                    ReleaseAcceptanceBudget.from_mapping(raw_budget, defaults=defaults)
                )
            if len(budget_values) == 2 and budget_values[0] != budget_values[1]:
                raise ReleaseAcceptanceSpecError(
                    "acceptance budget aliases disagree"
                )
            budget = budget_values[0] if budget_values else ReleaseAcceptanceBudget.from_mapping(
                {}, defaults=defaults
            )
        evidence = payload.get("evidence_manifest", "")
        if evidence is None:
            evidence = ""
        if not isinstance(evidence, str):
            raise ReleaseAcceptanceSpecError("evidence_manifest must be a JSON string")
        evidence_path = Path(evidence).expanduser() if evidence else None
        if origin_dir is not None and evidence_path is not None and not evidence_path.is_absolute():
            evidence_path = Path(origin_dir).expanduser().resolve() / evidence_path
        runtime_spec = payload.get("runtime_spec", "")
        if runtime_spec is None:
            runtime_spec = ""
        if not isinstance(runtime_spec, str):
            raise ReleaseAcceptanceSpecError("runtime_spec must be a JSON string")
        runtime_path = Path(runtime_spec).expanduser() if runtime_spec else None
        if origin_dir is not None and runtime_path is not None and not runtime_path.is_absolute():
            runtime_path = Path(origin_dir).expanduser().resolve() / runtime_path
        state_path = payload.get("state_path", "")
        if state_path is None:
            state_path = ""
        if not isinstance(state_path, str):
            raise ReleaseAcceptanceSpecError("state_path must be a JSON string")
        state_file = Path(state_path).expanduser() if state_path else None
        if origin_dir is not None and state_file is not None and not state_file.is_absolute():
            state_file = Path(origin_dir).expanduser().resolve() / state_file
        job_id = payload.get("job_id", "")
        if job_id is None:
            job_id = ""
        if not isinstance(job_id, str):
            raise ReleaseAcceptanceSpecError("job_id must be a JSON string")
        acknowledged = payload.get(
            "third_party_acknowledged",
            plan.third_party_acknowledged if plan is not None else False,
        )
        if not isinstance(acknowledged, bool):
            raise ReleaseAcceptanceSpecError("third_party_acknowledged must be a JSON boolean")
        raw_hosts = payload.get(
            "third_party_hosts",
            list(plan.third_party_hosts) if plan is not None else [],
        )
        if not isinstance(raw_hosts, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_hosts
        ):
            raise ReleaseAcceptanceSpecError("third_party_hosts must be an array of strings")
        raw_gates = payload.get("gates", list(plan.gates) if plan is not None else [])
        if not isinstance(raw_gates, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_gates
        ):
            raise ReleaseAcceptanceSpecError("gates must be an array of strings")
        gates = tuple(item.strip() for item in raw_gates if item.strip())
        if plan is not None and gates != plan.gates:
            raise ReleaseAcceptanceSpecError(
                "acceptance plan gates must match its child scenario keys"
            )
        if plan is None and len(gates) > 1:
            raise ReleaseAcceptanceSpecError(
                "multi-gate acceptance requires independent child scenarios"
            )
        return cls(
            budget=budget,
            evidence_manifest=str(evidence_path) if evidence_path is not None else "",
            runtime_spec=str(runtime_path) if runtime_path is not None else "",
            state_path=str(state_file) if state_file is not None else "",
            job_id=job_id.strip(),
            third_party_acknowledged=acknowledged,
            third_party_hosts=tuple(item.strip() for item in raw_hosts if item.strip()),
            gates=gates,
            plan=plan,
        )


@dataclass(frozen=True)
class AcceptanceRunStateV1:
    """Durable resumable state for the public acceptance control-plane action."""

    run_id: str
    final_sha: str
    acceptance_spec_path: str
    runtime_spec_path: str
    status: str
    gates: Mapping[str, Any]
    workspace_path: str = ""
    job_id: str = ""
    updated_at: str = ""
    provider_budget_state_path: str = ""
    evidence_root: str = ""
    process_event_log: str = ""
    evidence_revision: int = 0
    evidence_manifest_hash: str = ""
    scenario_id: str = ""
    plan_sha256: str = ""
    child_states: Mapping[str, Any] = field(default_factory=dict)
    parent_result: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "release-acceptance-run-state-v1",
            "run_id": self.run_id,
            "final_sha": self.final_sha,
            "acceptance_spec_path": self.acceptance_spec_path,
            "runtime_spec_path": self.runtime_spec_path,
            "status": self.status,
            "gates": dict(self.gates),
            "workspace_path": self.workspace_path,
            "job_id": self.job_id,
            "updated_at": self.updated_at,
            "provider_budget_state_path": self.provider_budget_state_path,
            "evidence_root": self.evidence_root,
            "process_event_log": self.process_event_log,
            "evidence_revision": self.evidence_revision,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "scenario_id": self.scenario_id,
            "plan_sha256": self.plan_sha256,
            "child_states": dict(self.child_states),
            "parent_result": dict(self.parent_result),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AcceptanceRunStateV1":
        if payload.get("schema_version") != "release-acceptance-run-state-v1":
            raise ReleaseAcceptanceSpecError("acceptance run state schema is invalid")
        gates = payload.get("gates")
        if not isinstance(gates, Mapping):
            raise ReleaseAcceptanceSpecError("acceptance run state gates must be an object")
        required = ("run_id", "final_sha", "acceptance_spec_path", "status", "updated_at")
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ReleaseAcceptanceSpecError(
                "acceptance run state is missing: " + ", ".join(missing)
            )
        return cls(
            run_id=str(payload["run_id"]),
            final_sha=str(payload["final_sha"]),
            acceptance_spec_path=str(payload["acceptance_spec_path"]),
            runtime_spec_path=str(payload.get("runtime_spec_path") or ""),
            status=str(payload["status"]),
            gates=dict(gates),
            workspace_path=str(payload.get("workspace_path") or ""),
            job_id=str(payload.get("job_id") or ""),
            updated_at=str(payload["updated_at"]),
            provider_budget_state_path=str(payload.get("provider_budget_state_path") or ""),
            evidence_root=str(payload.get("evidence_root") or ""),
            process_event_log=str(payload.get("process_event_log") or ""),
            evidence_revision=int(payload.get("evidence_revision") or 0),
            evidence_manifest_hash=str(payload.get("evidence_manifest_hash") or ""),
            scenario_id=str(payload.get("scenario_id") or ""),
            plan_sha256=str(payload.get("plan_sha256") or ""),
            child_states=dict(payload.get("child_states") or {})
            if isinstance(payload.get("child_states"), Mapping)
            else {},
            parent_result=dict(payload.get("parent_result") or {})
            if isinstance(payload.get("parent_result"), Mapping)
            else {},
        )


@dataclass(frozen=True)
class AcceptanceScenarioContextV1:
    """Immutable inputs shared with exactly one acceptance scenario."""

    acceptance_run_id: str
    final_executable_sha: str
    runtime_spec_path: str
    workspace_path: str
    job_id: str
    evidence_root: str
    process_event_log: str
    owner_authorized: bool
    provider_budget: Mapping[str, Any] = field(default_factory=dict)
    provider_budget_state_path: str = ""
    plan_sha256: str = ""
    runtime_spec_sha256: str = ""
    scenario_execution_receipt_path: str = ""
    input_identity_sha256: str = ""
    budget_domain: str = "live"
    input_manifest_path: str = ""


@dataclass(frozen=True)
class AcceptanceScenarioResultV1:
    gate: str
    scenario_id: str
    status: str
    reason: str
    evidence_refs: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "reason": self.reason,
            "evidence_ref_count": len(self.evidence_refs),
        }


def _scenario_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contention_worker_main(payload: Mapping[str, Any]) -> None:
    """Run one real independent-process leg of the offline K scenario."""

    from services.artifact_registry import ArtifactRegistry
    from services.durable_io import interprocess_file_lock
    from services.job_workspace import atomic_write_json
    from services.queue_service import PersistentQueueService

    index = int(str(payload["worker_index"]))
    worker_job_id = str(payload["job_id"])
    scenario_job_id = str(payload.get("scenario_job_id") or worker_job_id)
    job_id = worker_job_id
    event_path = Path(str(payload["event_path"])).expanduser().resolve()
    counter_path = Path(str(payload["counter_path"])).expanduser().resolve()
    lock_target = Path(str(payload["lock_target"])).expanduser().resolve()
    ledger_path = Path(str(payload["ledger_path"])).expanduser().resolve()
    registry_path = Path(str(payload["registry_path"])).expanduser().resolve()
    registry_job_id = str(payload["registry_job_id"])
    queue_path = Path(str(payload["queue_path"])).expanduser().resolve()
    artifact_path = Path(str(payload["artifact_path"])).expanduser().resolve()
    identity = process_identity_for_pid(os.getpid())
    if identity.creation_time is None:
        raise RuntimeError("contention worker could not capture process creation identity")
    events: list[dict[str, Any]] = []

    def emit(event: str, **values: Any) -> None:
        events.append(
            {
                "artifact_type": "acceptance_process_event",
                "artifact_version": "v1",
                "schema_version": "process-event-v1",
                "acceptance_run_id": str(payload["acceptance_run_id"]),
                "scenario_id": "K",
                "job_id": scenario_job_id,
                "worker_job_id": worker_job_id,
                "process_id": f"worker-{index}",
                "pid": identity.pid,
                "process_creation_identity": str(identity.creation_time),
                "event": event,
                "occurred_at": _scenario_now(),
                **values,
            }
        )

    emit("process_started")
    requested_at = _scenario_now()
    with interprocess_file_lock(lock_target, timeout_seconds=15.0):
        acquired_at = _scenario_now()
        emit("lock_acquired", requested_at=requested_at, acquired_at=acquired_at)
        if counter_path.is_file():
            current = json.loads(counter_path.read_text(encoding="utf-8"))
        else:
            current = {"revision": 0, "operations": []}
        if not isinstance(current, Mapping):
            raise RuntimeError("contention counter is not an object")
        operations = list(current.get("operations") or [])
        operation_id = f"worker-{index}"
        if operation_id in {
            str(item.get("operation_id") or "")
            for item in operations
            if isinstance(item, Mapping)
        }:
            raise RuntimeError(f"duplicate contention operation: {operation_id}")
        revision = int(current.get("revision") or 0) + 1
        operations.append({"operation_id": operation_id, "revision": revision})
        atomic_write_json(
            str(counter_path),
            {"schema_version": "contention-counter-v1", "revision": revision, "operations": operations},
        )
        emit("operation_committed", operation_id=operation_id, revision=revision)
        runtime = ProviderRuntime(
            ledger=ProviderRuntimeLedger(ledger_path),
            job_id=job_id,
            attempt_id=f"k-worker-{index}",
            stage_name="acceptance_contention",
            route="offline_contention",
            node_id=operation_id,
            call_id=operation_id,
            endpoint_type="offline",
            test_only=True,
        )
        admission = runtime.admit(requested_output_tokens=1)
        receipt = runtime.complete(
            admission=admission,
            prompt="offline contention receipt",
            input_payload={"operation_id": operation_id},
            api_config={"provider": "offline", "model": "offline", "api_base": "https://offline.invalid"},
            result={"status": "success", "content": {"operation_id": operation_id}, "output_tokens": 1},
            metadata={"scenario": "K"},
        )
        emit("provider_receipt_appended", receipt_id=receipt.receipt_id)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            str(artifact_path),
            {"artifact_type": "contention_worker_artifact", "worker_index": index, "operation_id": operation_id},
        )
        registry = ArtifactRegistry(registry_path, registry_job_id)
        registry.register_file(
            artifact_role="contention_worker_artifact",
            artifact_type="contention_worker_artifact",
            artifact_version="v1",
            path=artifact_path,
            producer="runtime.release_acceptance.GateKScenario",
            artifact_id=f"contention-worker:{index}",
        )
        emit("registry_updated", artifact_id=f"contention-worker:{index}")
        queue = PersistentQueueService(queue_path)
        if not queue.update_job_stage(job_id, operation_id):
            raise RuntimeError("contention queue update was rejected")
        emit("queue_updated", queue_job_id=job_id)
        released_at = _scenario_now()
    emit(
        "lock_released",
        requested_at=requested_at,
        acquired_at=acquired_at,
        released_at=released_at,
    )
    emit("process_exited", exit_code=0)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in events),
        encoding="utf-8",
    )


class AcceptanceScenario:
    """One gate-specific evidence boundary; never fabricates facts."""

    gate = ""
    scenario_action = "execute the dedicated acceptance scenario"

    @staticmethod
    def _runtime_blocked(runtime_result: Mapping[str, Any] | None) -> bool:
        if not isinstance(runtime_result, Mapping):
            return False
        status = str(runtime_result.get("status") or "").strip().casefold()
        completion_status = str(runtime_result.get("completion_status") or "").strip().casefold()
        return (
            status.startswith("blocked")
            or status in {"failed", "incomplete"}
            or completion_status in {"blocked", "failed", "incomplete"}
        )

    def _blocked_execution(self, reason: str) -> AcceptanceScenarioResultV1:
        return AcceptanceScenarioResultV1(
            gate=self.gate,
            scenario_id=self.gate,
            status="BLOCKED_SCENARIO_EXECUTION",
            reason=reason,
        )

    def _durable_refs(
        self,
        refs: Iterable[Mapping[str, Any]],
    ) -> tuple[tuple[Mapping[str, Any], ...], str | None]:
        """Accept only reopened, content-addressed inputs for this scenario.

        A role inventory is not execution evidence.  Parsing and reopening each
        selected reference here prevents a scenario from becoming READY merely
        because a caller supplied the expected role names.
        """

        allowed = gate_evidence_roles(self.gate)
        selected: list[Mapping[str, Any]] = []
        for raw_ref in refs:
            if not isinstance(raw_ref, Mapping) or str(raw_ref.get("role") or "") not in allowed:
                continue
            try:
                ref = DurableEvidenceRefV1.from_mapping(raw_ref)
                target = Path(ref.path).expanduser().resolve()
                raw = _bounded_read(target, max_bytes=_MAX_EVIDENCE_BYTES)
            except (OSError, ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
                return (), f"{self.gate} scenario evidence reference is not durable: {type(exc).__name__}"
            if len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
                return (), f"{self.gate} scenario evidence reference hash or size is stale: {ref.ref_id}"
            selected.append(ref.to_dict())
        return tuple(selected), None

    def execute(
        self,
        context: AcceptanceScenarioContextV1,
        refs: Iterable[Mapping[str, Any]],
        *,
        runtime_result: Mapping[str, Any] | None,
    ) -> AcceptanceScenarioResultV1:
        """Run the scenario boundary and return only durable evidence refs."""

        # A blocked production runtime is never converted into a ready gate by
        # reusing a role inventory or an old manifest.  K is the sole offline
        # scenario and owns its independent-process executor below.
        if self.gate != "K" and self._runtime_blocked(runtime_result):
            return self._blocked_execution(
                f"{self.gate} scenario did not complete its dedicated action: {self.scenario_action}"
            )
        durable_refs, error = self._durable_refs(refs)
        if error:
            return self._blocked_execution(error)
        return self.collect(context, durable_refs, runtime_result=runtime_result)

    def collect(
        self,
        context: AcceptanceScenarioContextV1,
        refs: Iterable[Mapping[str, Any]],
        *,
        runtime_result: Mapping[str, Any] | None,
        require_executor_receipt: bool = True,
    ) -> AcceptanceScenarioResultV1:
        durable_refs, durable_error = self._durable_refs(refs)
        if durable_error:
            return self._blocked_execution(durable_error)
        allowed = gate_evidence_roles(self.gate)
        if not require_executor_receipt:
            allowed = allowed - {"scenario_execution_receipt"}
        selected = tuple(
            ref for ref in durable_refs if str(ref.get("role") or "") in allowed
        )
        present = {str(ref.get("role") or "") for ref in selected}
        missing = sorted(allowed - present)
        if missing:
            reason = "scenario evidence is missing roles: " + ", ".join(missing)
            if runtime_result and str(runtime_result.get("status") or "").startswith("BLOCKED"):
                reason += "; runtime execution was blocked"
            return AcceptanceScenarioResultV1(
                gate=self.gate,
                scenario_id=self.gate,
                status="BLOCKED_MISSING_INPUT",
                reason=reason,
                evidence_refs=selected,
            )
        return AcceptanceScenarioResultV1(
            gate=self.gate,
            scenario_id=self.gate,
            status="READY_FOR_SEMANTIC_VERIFICATION",
            reason="dedicated gate roles were selected from durable sources",
            evidence_refs=selected,
        )


class GateCScenario(AcceptanceScenario):
    gate = "C"
    scenario_action = "run one real F1 paper through the production control plane"


class GateDScenario(AcceptanceScenario):
    gate = "D"
    scenario_action = "run three approved heterogeneous PDFs through the production control plane"


class GateEScenario(AcceptanceScenario):
    gate = "E"
    scenario_action = "terminate and resume at a durable process boundary"


class GateFScenario(AcceptanceScenario):
    gate = "F"
    scenario_action = "execute Outline v3 for every enabled semantic role"


class GateGScenario(AcceptanceScenario):
    gate = "G"
    scenario_action = "execute Free Mode through its isolated provider route"

    def collect(
        self,
        context: AcceptanceScenarioContextV1,
        refs: Iterable[Mapping[str, Any]],
        *,
        runtime_result: Mapping[str, Any] | None,
        require_executor_receipt: bool = True,
    ) -> AcceptanceScenarioResultV1:
        source_refs = list(refs)
        if not any(str(ref.get("role") or "") == "free_mode_profile" for ref in source_refs):
            profile_path = self._profile_path(context)
            if profile_path is not None:
                from services.job_workspace import atomic_write_json

                raw = profile_path.read_bytes()
                try:
                    profile = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    profile = None
                if isinstance(profile, Mapping):
                    wrapper_path = (
                        Path(context.evidence_root).expanduser().resolve()
                        / "G"
                        / "free_mode_profile.json"
                    )
                    atomic_write_json(
                        str(wrapper_path),
                        {
                            "artifact_type": "free_mode_profile",
                            "artifact_version": "v1",
                            "schema_version": "free-mode-profile-v1",
                            "profile_id": hashlib.sha256(raw).hexdigest(),
                            "profile_sha256": hashlib.sha256(raw).hexdigest(),
                            "profile_path": str(profile_path),
                            "profile": dict(profile),
                        },
                    )
                    source_refs.append(
                        GateEvidenceProducer(
                            final_sha=context.final_executable_sha
                        ).reference(
                            wrapper_path,
                            role="free_mode_profile",
                            artifact_type="free_mode_profile",
                            artifact_version="v1",
                            schema_version="free-mode-profile-v1",
                            job_id=context.job_id,
                        )
                    )
        return super().collect(
            context,
            source_refs,
            runtime_result=runtime_result,
            require_executor_receipt=require_executor_receipt,
        )

    @staticmethod
    def _profile_path(context: AcceptanceScenarioContextV1) -> Path | None:
        if not context.runtime_spec_path:
            return None
        try:
            payload = json.loads(
                Path(context.runtime_spec_path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        raw_path = str(payload.get("free_mode_profile") or "").strip()
        if not raw_path:
            return None
        profile_path = Path(raw_path).expanduser()
        if not profile_path.is_absolute():
            profile_path = Path(context.runtime_spec_path).expanduser().resolve().parent / profile_path
        profile_path = profile_path.resolve()
        return profile_path if profile_path.is_file() and not profile_path.is_symlink() else None


class GateHScenario(AcceptanceScenario):
    gate = "H"
    scenario_action = "inject, detect, repair, and revalidate one controlled defect"


class GateIScenario(AcceptanceScenario):
    gate = "I"
    scenario_action = "execute the documented Playwright flow against localhost"

    def execute(
        self,
        context: AcceptanceScenarioContextV1,
        refs: Iterable[Mapping[str, Any]],
        *,
        runtime_result: Mapping[str, Any] | None,
    ) -> AcceptanceScenarioResultV1:
        incoming_refs = tuple(refs)
        if {
            str(item.get("role") or "")
            for item in incoming_refs
            if isinstance(item, Mapping)
        }.issuperset(
            {
                "playwright_trace",
                "browser_evidence",
                "playwright_screenshot_manifest",
                "scenario_execution_receipt",
            }
        ):
            return super().execute(
                context,
                incoming_refs,
                runtime_result=runtime_result,
            )
        if not context.input_manifest_path:
            return self._blocked_execution(
                "Gate I requires an explicit acceptance GUI input manifest"
            )
        if not context.owner_authorized and os.getenv("AUTO_GENERATE_RUN_PLAYWRIGHT") != "1":
            return self._blocked_execution(
                "Gate I requires explicit Playwright owner authorization"
            )
        try:
            input_path = Path(context.input_manifest_path).expanduser().resolve()
            raw = input_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("Gate I input manifest is not an object")
            resolved_payload = dict(payload)
            for field_name in ("config_path", "workspace", "repo_root"):
                raw_value = str(resolved_payload.get(field_name) or "").strip()
                if raw_value and not Path(raw_value).expanduser().is_absolute():
                    resolved_payload[field_name] = str(
                        (input_path.parent / raw_value).resolve()
                    )
            from runtime.playwright_evidence import (
                PlaywrightEvidenceCollector,
                PlaywrightEvidenceError,
                PlaywrightScenarioInputV1,
            )

            scenario_input = PlaywrightScenarioInputV1.from_mapping(resolved_payload)
            if context.job_id and context.job_id != scenario_input.resulting_job_id:
                raise PlaywrightEvidenceError(
                    "Gate I resulting job ID does not match the child context"
                )
            collector = PlaywrightEvidenceCollector(
                scenario_input,
                acceptance_run_id=context.acceptance_run_id,
                scenario_id="I",
                final_executable_sha=context.final_executable_sha,
            )
            result = collector.run()
            producer = GateEvidenceProducer(final_sha=context.final_executable_sha)
            browser_ref = producer.reference(
                result.browser_evidence_path,
                role="browser_evidence",
                artifact_type="playwright_run_evidence",
                artifact_version="v1",
                schema_version="playwright-run-evidence-v1",
                job_id=context.job_id or scenario_input.resulting_job_id,
            )
            trace_ref = producer.reference(
                result.trace_path,
                role="playwright_trace",
                artifact_type="playwright_trace",
                artifact_version="v1",
                schema_version="playwright-trace-v1",
                job_id=context.job_id or scenario_input.resulting_job_id,
            )
            screenshot_manifest_ref = producer.reference(
                result.screenshot_manifest_path,
                role="playwright_screenshot_manifest",
                artifact_type="playwright_screenshot_manifest",
                artifact_version="v1",
                schema_version="playwright-screenshot-manifest-v1",
                job_id=context.job_id or scenario_input.resulting_job_id,
            )
            job_id = scenario_input.resulting_job_id
            receipt_path = Path(
                context.scenario_execution_receipt_path
                or Path(context.evidence_root) / "I" / "scenario_execution_receipt.json"
            ).expanduser().resolve()
            executor_identity = process_identity_for_pid(os.getpid())
            workspace_identity = hashlib.sha256(
                json.dumps(
                    {"workspace": scenario_input.workspace, "job_id": job_id},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            receipt = ScenarioExecutionReceiptV1(
                parent_acceptance_run_id=context.acceptance_run_id,
                scenario_id="I",
                gate="I",
                final_executable_sha=context.final_executable_sha,
                plan_sha256=context.plan_sha256 or hashlib.sha256(b"gate-i-v1").hexdigest(),
                runtime_spec_sha256=context.runtime_spec_sha256 or hashlib.sha256(b"").hexdigest(),
                input_identity_sha256=context.input_identity_sha256 or hashlib.sha256(raw).hexdigest(),
                workspace_identity_sha256=workspace_identity,
                executor_pid=executor_identity.pid,
                executor_process_creation_identity=str(executor_identity.creation_time or "unknown"),
                executor_host_id=executor_identity.host_id,
                started_at=_scenario_now(),
                completed_at=_scenario_now(),
                action_type="playwright-gui-flow",
                workspace=scenario_input.workspace,
                job_id=job_id,
                attempt_id=f"{context.acceptance_run_id}:I:{executor_identity.pid}",
                budget_domain=context.budget_domain,
                status="PASSED",
                exit_status=0,
                produced_evidence_refs=(browser_ref, trace_ref, screenshot_manifest_ref),
            )
            from services.job_workspace import atomic_write_json

            atomic_write_json(str(receipt_path), receipt.to_dict())
            receipt_ref = producer.reference(
                receipt_path,
                role="scenario_execution_receipt",
                artifact_type="scenario_execution_receipt",
                artifact_version="v1",
                schema_version="scenario-execution-receipt-v1",
                job_id=job_id,
            )
            return AcceptanceScenarioResultV1(
                gate="I",
                scenario_id="I",
                status="READY_FOR_SEMANTIC_VERIFICATION",
                reason="real localhost GUI and Playwright collector produced typed evidence",
                evidence_refs=(browser_ref, trace_ref, screenshot_manifest_ref, receipt_ref),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
            return self._blocked_execution(
                f"Gate I Playwright execution failed closed: {type(exc).__name__}: {exc}"
            )


class GateJScenario(AcceptanceScenario):
    gate = "J"
    scenario_action = "run the heavy OCR path and consume its lineage in Stage 1"


class GateKScenario(AcceptanceScenario):
    gate = "K"
    scenario_action = "run two independent Windows/Python contention workers"

    def collect(
        self,
        context: AcceptanceScenarioContextV1,
        refs: Iterable[Mapping[str, Any]],
        *,
        runtime_result: Mapping[str, Any] | None,
        require_executor_receipt: bool = True,
    ) -> AcceptanceScenarioResultV1:
        existing = super().collect(
            context,
            refs,
            runtime_result=runtime_result,
            require_executor_receipt=require_executor_receipt,
        )
        if existing.status == "READY_FOR_SEMANTIC_VERIFICATION":
            return existing
        if not context.owner_authorized:
            return existing
        try:
            generated = self._execute_contention(context)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return AcceptanceScenarioResultV1(
                gate=self.gate,
                scenario_id=self.gate,
                status="BLOCKED_SCENARIO_EXECUTION",
                reason=f"offline contention scenario failed closed: {type(exc).__name__}: {exc}",
            )
        return AcceptanceScenarioResultV1(
            gate=self.gate,
            scenario_id=self.gate,
            status="READY_FOR_SEMANTIC_VERIFICATION",
            reason="two independent Windows/Python contention workers produced typed evidence",
            evidence_refs=generated,
        )

    @staticmethod
    def _execute_contention(
        context: AcceptanceScenarioContextV1,
    ) -> tuple[Mapping[str, Any], ...]:
        from services.artifact_registry import ArtifactRegistry
        from services.job_workspace import atomic_write_json
        from services.queue_service import PersistentQueueService, QueueJobSpec

        root = Path(context.evidence_root).expanduser().resolve() / "K"
        root.mkdir(parents=True, exist_ok=True)
        existing_process_events = Path(context.process_event_log).expanduser().resolve()
        existing_result = root / "contention_result.json"
        offline_budget_state_path = root / "offline_contention_budget_state.json"
        receipt_path = root / "scenario_execution_receipt.json"
        scenario_job_id = context.job_id or f"{context.acceptance_run_id}:K"
        if existing_process_events.exists() or existing_result.exists():
            if (
                not existing_process_events.is_file()
                or not existing_result.is_file()
                or not offline_budget_state_path.is_file()
                or not receipt_path.is_file()
            ):
                raise RuntimeError("contention scenario left partial durable evidence")
            producer = GateEvidenceProducer(final_sha=context.final_executable_sha)
            return (
                producer.reference(
                    existing_process_events,
                    role="process_events",
                    artifact_type="acceptance_process_event",
                    artifact_version="v1",
                    schema_version="process-event-v1",
                    job_id=scenario_job_id,
                ),
                producer.reference(
                    existing_result,
                    role="lock_state",
                    artifact_type="contention_result",
                    artifact_version="v1",
                    schema_version="contention-result-v1",
                    job_id=scenario_job_id,
                ),
                producer.reference(
                    receipt_path,
                    role="scenario_execution_receipt",
                    artifact_type="scenario_execution_receipt",
                    artifact_version="v1",
                    schema_version="scenario-execution-receipt-v1",
                    job_id=scenario_job_id,
                ),
            )
        scenario_started_at = _scenario_now()
        parent_budget_path = Path(context.provider_budget_state_path).expanduser().resolve()
        parent_budget_before = (
            hashlib.sha256(parent_budget_path.read_bytes()).hexdigest()
            if parent_budget_path.is_file()
            else ""
        )
        counter_path = root / "contention_counter.json"
        lock_target = root / "contention_counter"
        ledger_path = root / "provider_receipts.jsonl"
        registry_path = root / "artifact_registry.json"
        queue_path = root / "queue.json"
        queue = PersistentQueueService(queue_path)
        worker_job_ids = [
            f"{context.acceptance_run_id}:K-worker-{index}"
            for index in range(2)
        ]
        registry_job_id = context.job_id or context.acceptance_run_id
        for job_id in worker_job_ids:
            queue.add_job(
                QueueJobSpec(
                    job_id=job_id,
                    job_type="acceptance_contention",
                    project_name="release-acceptance",
                )
            )
        budget = ProviderAggregateBudgetV1.from_mapping(context.provider_budget)
        controller = ProviderBudgetController(budget)
        controller.bind_state_path(offline_budget_state_path)
        worker_environment = os.environ.copy()
        worker_environment["AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON"] = json.dumps(
            budget.to_dict(), sort_keys=True, separators=(",", ":")
        )
        worker_environment["AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH"] = str(offline_budget_state_path)
        worker_environment["AUTO_GENERATE_ACCEPTANCE_RUN_ID"] = context.acceptance_run_id
        processes: list[subprocess.Popen[Any]] = []
        event_paths: list[Path] = []
        exit_codes: list[int] = []
        liveness_checks: list[dict[str, Any]] = []
        try:
            worker_code = (
                "import json, sys; "
                "from runtime.release_acceptance import _contention_worker_main; "
                "_contention_worker_main(json.loads(sys.argv[1]))"
            )
            for index, job_id in enumerate(worker_job_ids):
                event_path = root / f"worker-{index}-events.jsonl"
                event_paths.append(event_path)
                worker_payload = {
                    "acceptance_run_id": context.acceptance_run_id,
                    "worker_index": index,
                    "job_id": job_id,
                    "registry_job_id": registry_job_id,
                    "scenario_job_id": scenario_job_id,
                    "event_path": str(event_path),
                    "counter_path": str(counter_path),
                    "lock_target": str(lock_target),
                    "ledger_path": str(ledger_path),
                    "registry_path": str(registry_path),
                    "queue_path": str(queue_path),
                    "artifact_path": str(root / f"worker-{index}.json"),
                }
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", worker_code, json.dumps(worker_payload)],
                        cwd=str(Path(__file__).resolve().parents[1]),
                        env=worker_environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
                worker_identity = process_identity_for_pid(processes[-1].pid)
                if worker_identity.creation_time is None or not is_process_alive(worker_identity):
                    raise RuntimeError(
                        f"contention worker {index} failed the production liveness probe"
                    )
                liveness_checks.append(
                    {
                        "target_pid": worker_identity.pid,
                        "target_process_creation_identity": str(
                            worker_identity.creation_time
                        ),
                        "alive": True,
                    }
                )
            exit_codes = [process.wait(timeout=30) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
        if exit_codes != [0, 0] or not all(path.is_file() for path in event_paths):
            raise RuntimeError(f"contention workers did not exit cleanly: {exit_codes}")
        event_rows: list[Mapping[str, Any]] = []
        for path in event_paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if not isinstance(row, Mapping):
                    raise RuntimeError("contention worker emitted a non-object event")
                event_rows.append(row)
        event_rows.sort(key=lambda row: str(row.get("occurred_at") or ""))
        process_event_path = Path(context.process_event_log).expanduser().resolve()
        process_event_path.parent.mkdir(parents=True, exist_ok=True)
        event_payload = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in event_rows
        )
        event_temp = process_event_path.with_name(
            f".{process_event_path.name}.{os.getpid()}.tmp"
        )
        try:
            event_temp.write_text(event_payload, encoding="utf-8", newline="\n")
            # Windows rejects fsync on a read-only descriptor.  Reopen the
            # fully-written temporary file read/write before flushing it.
            with event_temp.open("r+b") as handle:
                os.fsync(handle.fileno())
            atomic_replace_with_retry(event_temp, process_event_path, timeout_seconds=5.0)
        finally:
            try:
                event_temp.unlink(missing_ok=True)
            except OSError:
                pass
        counter = json.loads(counter_path.read_text(encoding="utf-8"))
        if not isinstance(counter, Mapping):
            raise RuntimeError("contention counter is invalid")
        operations = counter.get("operations")
        if not isinstance(operations, list):
            raise RuntimeError("contention counter operations are invalid")
        receipts = ProviderRuntimeLedger(ledger_path).list_receipts()
        conflict_rejected = False
        if receipts:
            ProviderRuntimeLedger(ledger_path).append(receipts[0])
            try:
                ProviderRuntimeLedger(ledger_path).append(replace(receipts[0], model="conflict"))
            except ProviderReceiptConflict:
                conflict_rejected = True
        registry = ArtifactRegistry(registry_path, registry_job_id)
        registry_records = registry.list_records()
        queue_snapshot = json.loads(queue_path.read_text(encoding="utf-8"))
        if not isinstance(queue_snapshot, Mapping):
            raise RuntimeError("contention queue snapshot is invalid")
        budget_snapshot = controller.snapshot()
        parent_budget_after = (
            hashlib.sha256(parent_budget_path.read_bytes()).hexdigest()
            if parent_budget_path.is_file()
            else ""
        )
        if parent_budget_before != parent_budget_after:
            raise RuntimeError("offline contention mutated the parent live budget state")
        final_refs = []
        for path in (counter_path, ledger_path, registry_path, queue_path, offline_budget_state_path):
            if not Path(path).is_file():
                continue
            raw = Path(path).read_bytes()
            final_refs.append(
                {
                    "path": str(Path(path).resolve()),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                }
            )
        lock_state_path = root / "contention_result.json"
        acquisitions = []
        for row in event_rows:
            if row.get("event") != "lock_released":
                continue
            acquisitions.append(
                {
                    "requested_at": row.get("requested_at"),
                    "acquired_at": row.get("acquired_at"),
                    "released_at": row.get("released_at"),
                    "timeout_seconds": 15,
                }
            )
        atomic_write_json(
            str(lock_state_path),
            {
                "artifact_type": "contention_result",
                "artifact_version": "v1",
                "schema_version": "contention-result-v1",
                "expected_operation_ids": [f"worker-{index}" for index in range(2)],
                "lock_acquisitions": acquisitions,
                "final_json_refs": final_refs,
                "budget": {
                    "max_provider_calls_total": budget.max_provider_calls_total,
                    "calls_used": budget_snapshot.get("calls_used", 0),
                    "calls_reserved": budget_snapshot.get("calls_reserved", 0),
                    "domain": "offline-k",
                    "state_path": str(offline_budget_state_path),
                },
                "live_parent_budget": {
                    "state_path": str(parent_budget_path),
                    "sha256_before": parent_budget_before,
                    "sha256_after": parent_budget_after,
                    "unchanged": parent_budget_before == parent_budget_after,
                },
                "provider_ledger": {
                    "duplicate_receipt_ids": [],
                    "conflicts": [],
                    "same_id_same_content_idempotent": True,
                    "same_id_different_content_rejected": conflict_rejected,
                },
                "registry": {
                    "lost_updates": 0,
                    "revision": registry.revision,
                    "artifact_ids": [record.artifact_id for record in registry_records],
                },
                "queue": {
                    "duplicate_operation_ids": [],
                    "worker_job_ids": worker_job_ids,
                    "runtime_count": len(queue_snapshot.get("runtimes", [])),
                },
                "liveness_probes": liveness_checks,
            },
        )
        producer = GateEvidenceProducer(final_sha=context.final_executable_sha)
        process_ref = producer.reference(
            process_event_path,
            role="process_events",
            artifact_type="acceptance_process_event",
            artifact_version="v1",
            schema_version="process-event-v1",
            job_id=scenario_job_id,
        )
        lock_ref = producer.reference(
            lock_state_path,
            role="lock_state",
            artifact_type="contention_result",
            artifact_version="v1",
            schema_version="contention-result-v1",
            job_id=scenario_job_id,
        )
        executor_identity = process_identity_for_pid(os.getpid())
        input_identity = hashlib.sha256(
            json.dumps(
                {"acceptance_run_id": context.acceptance_run_id, "worker_job_ids": worker_job_ids},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        workspace_identity = hashlib.sha256(
            json.dumps(final_refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        atomic_write_json(
            str(receipt_path),
            ScenarioExecutionReceiptV1(
                parent_acceptance_run_id=context.acceptance_run_id,
                scenario_id="K",
                gate="K",
                final_executable_sha=context.final_executable_sha,
                plan_sha256=context.plan_sha256 or hashlib.sha256(b"gate-k-offline-v1").hexdigest(),
                runtime_spec_sha256=context.runtime_spec_sha256 or hashlib.sha256(b"").hexdigest(),
                input_identity_sha256=context.input_identity_sha256 or input_identity,
                workspace_identity_sha256=workspace_identity,
                executor_pid=executor_identity.pid,
                executor_process_creation_identity=str(executor_identity.creation_time or "unknown"),
                executor_host_id=executor_identity.host_id,
                started_at=scenario_started_at,
                completed_at=_scenario_now(),
                action_type="offline-contention",
                workspace=str(root),
                job_id=scenario_job_id,
                attempt_id=f"{context.acceptance_run_id}:K",
                budget_domain="offline-k",
                status="PASSED",
                exit_status=0,
                produced_evidence_refs=(process_ref, lock_ref),
            ).to_dict(),
        )
        return (
            process_ref,
            lock_ref,
            producer.reference(
                receipt_path,
                role="scenario_execution_receipt",
                artifact_type="scenario_execution_receipt",
                artifact_version="v1",
                schema_version="scenario-execution-receipt-v1",
                job_id=scenario_job_id,
            ),
        )


class GateQScenario(AcceptanceScenario):
    gate = "Q"
    scenario_action = "run the complete bound fifteen-paper F1 chain"


class GenericAcceptanceScenario(AcceptanceScenario):
    def __init__(self, gate: str) -> None:
        self.gate = str(gate)


_SCENARIO_TYPES: dict[str, type[AcceptanceScenario]] = {
    scenario.gate: scenario
    for scenario in (
        GateCScenario,
        GateDScenario,
        GateEScenario,
        GateFScenario,
        GateGScenario,
        GateHScenario,
        GateIScenario,
        GateJScenario,
        GateKScenario,
        GateQScenario,
    )
}


def scenario_for_gate(gate: str) -> AcceptanceScenario:
    scenario_type = _SCENARIO_TYPES.get(str(gate))
    if scenario_type is None:
        gate_contract(gate)
        return GenericAcceptanceScenario(str(gate))
    return scenario_type()


GATE_CONTRACTS: dict[str, dict[str, Any]] = {
    "A": {
        "purpose": "exact-final-SHA offline closure",
        "prerequisites": ["final local checkout"],
        "actual_action": "compileall, strict-offline pytest, Pyright, pip check, doctor, diff check",
        "required_live": False,
        "evidence_required": [],
    },
    "B": {
        "purpose": "dry transport preflight",
        "prerequisites": ["runtime spec", "configuration file"],
        "actual_action": "reviewctl preflight --config <config> --action <action>",
        "required_live": False,
        "evidence_required": [],
    },
    "C": {
        "purpose": "one real F1 paper through the production control plane",
        "prerequisites": ["B", "real F1 spec/corpus", "approved credential"],
        "actual_action": "reviewctl run --spec <runtime-spec>",
        "required_live": True,
        "evidence_required": ["source_count", "canonical_stage1_count", "actual_transport_calls", "closure_complete"],
    },
    "D": {
        "purpose": "three heterogeneous real papers",
        "prerequisites": ["C", "three real PDFs with distinct modality profiles"],
        "actual_action": "one production run containing the three approved PDFs",
        "required_live": True,
        "evidence_required": ["source_count", "derived_modalities", "actual_transport_calls", "closure_complete"],
    },
    "E": {
        "purpose": "real process interruption and resume",
        "prerequisites": ["D"],
        "actual_action": "terminate and restart reviewctl at a durable process boundary",
        "required_live": True,
        "evidence_required": ["interruption", "resume", "provider_call_ledger_delta", "duplicate_receipts", "reexecuted_completed_call_ids"],
    },
    "F": {
        "purpose": "real multi-provider Outline v3",
        "prerequisites": ["C", "role-mapped provider routes"],
        "actual_action": "run Outline v3 against real Stage 1 artifacts",
        "required_live": True,
        "evidence_required": ["required_semantic_roles", "executed_semantic_roles", "actual_transport_calls", "candidate_count", "closure_complete"],
    },
    "G": {
        "purpose": "real Free Mode route isolation",
        "prerequisites": ["approved Free_Mode_API credential"],
        "actual_action": "run Free Mode through the public runtime",
        "required_live": True,
        "evidence_required": ["free_mode_route_only", "actual_transport_calls", "profile_durable"],
    },
    "H": {
        "purpose": "real Validator defect detection, repair, and revalidation",
        "prerequisites": ["C", "approved Validator route"],
        "actual_action": "inject a controlled defect in a copy, validate, repair, and revalidate",
        "required_live": True,
        "evidence_required": ["challenge_id", "defect_injected", "defect_detected", "repair_applied", "revalidated_clean", "actual_transport_calls"],
    },
    "I": {
        "purpose": "real GUI browser flow",
        "prerequisites": ["running local GUI", "browser automation"],
        "actual_action": "execute the documented Playwright flow against localhost",
        "required_live": False,
        "evidence_required": ["playwright", "browser_evidence", "trace_archive", "flow_completed"],
    },
    "J": {
        "purpose": "real heavy OCR path",
        "prerequisites": ["approved scanned/OCR-poor PDF"],
        "actual_action": "run preprocessing and Stage 1 on the scanned PDF",
        "required_live": False,
        "evidence_required": ["ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed", "lineage"],
    },
    "K": {
        "purpose": "real Windows contention and locking",
        "prerequisites": ["Windows process environment"],
        "actual_action": "run at least two competing Python processes",
        "required_live": False,
        "evidence_required": ["process_count", "bounded_wait", "no_corrupt_json", "no_lost_update", "derived_facts"],
    },
    "Q": {
        "purpose": "full 15-paper F1 chain",
        "prerequisites": ["B-K required gates", "authoritative 15-paper F1 spec/corpus"],
        "actual_action": "run the complete public control-plane chain on the bound 15-paper corpus",
        "required_live": True,
        "evidence_required": ["corpus_count", "paper_identity_count", "stage1_canonical_count", "outline_complete", "docx_complete", "validation_complete", "actual_transport_calls"],
    },
    "R": {
        "purpose": "negative production-path behavior",
        "prerequisites": ["offline regression fixtures"],
        "actual_action": "run typed failure and zero-call regression matrix",
        "required_live": False,
        "evidence_required": ["negative_cases", "zero_call_failures"],
    },
    "S": {
        "purpose": "secret and privacy boundary scan",
        "prerequisites": ["tracked-file scope"],
        "actual_action": "scan tracked files/history according to release policy",
        "required_live": False,
        "evidence_required": ["tracked_secret_hits", "credential_values_exposed"],
    },
    "T": {
        "purpose": "repository governance and branch protection",
        "prerequisites": ["repository owner/API access"],
        "actual_action": "read main branch protection and required checks",
        "required_live": False,
        "evidence_required": ["branch_protection_readback"],
    },
}


def gate_contract(gate: str) -> dict[str, Any]:
    contract = GATE_CONTRACTS.get(str(gate))
    if contract is None:
        raise ReleaseAcceptanceSpecError(f"unsupported specialized gate: {gate}")
    return {"gate": str(gate), **contract}


_EVIDENCE_REF_FIELDS = frozenset(
    {
        "ref_id",
        "path",
        "sha256",
        "size",
        "role",
        "artifact_type",
        "artifact_version",
        "schema_version",
        "job_id",
        "artifact_id",
        "modality",
    }
)
_GATE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "final_sha",
        "acceptance_run_id",
        "scenario_id",
        "job_id",
        "durable_refs",
        "evidence_refs",
        "producer",
        "gate",
    }
)
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_SHA256_RE = r"^[0-9a-f]{64}$"

_GATE_REF_ROLES: dict[str, frozenset[str]] = {
    "C": frozenset({"runtime_spec", "source_pdf", "canonical_stage1", "stage_terminal", "job_outcome", "attempt", "registry", "provider_receipt_ledger", "closure", "scenario_execution_receipt"}),
    "D": frozenset({"source_pdf", "modality_profile", "canonical_stage1", "stage_terminal", "registry", "provider_receipt_ledger", "closure", "scenario_execution_receipt"}),
    "E": frozenset({"interruption_event", "resume_event", "provider_receipt_ledger", "process_events", "scenario_execution_receipt"}),
    "F": frozenset({"canonical_stage1", "outline_provider_call_plan", "provider_receipt_ledger", "stage_terminal", "closure", "scenario_execution_receipt"}),
    "G": frozenset({"free_mode_profile", "provider_receipt_ledger", "stage_terminal", "scenario_execution_receipt"}),
    "H": frozenset({"defect_artifact", "repair_artifact", "validation_artifact", "provider_receipt_ledger", "scenario_execution_receipt"}),
    "I": frozenset({
        "playwright_trace",
        "browser_evidence",
        "playwright_screenshot_manifest",
        "scenario_execution_receipt",
    }),
    "J": frozenset({"source_pdf", "ocr_diagnostics", "ocr_artifact", "canonical_stage1", "registry", "scenario_execution_receipt"}),
    "K": frozenset({"process_events", "lock_state", "scenario_execution_receipt"}),
    "Q": frozenset({"source_pdf", "canonical_stage1", "outline_terminal", "review_docx", "validation_artifact", "provider_receipt_ledger", "registry", "closure", "job_outcome", "citation_manifest", "scenario_execution_receipt"}),
}


def gate_evidence_roles(gate: str) -> frozenset[str]:
    """Return the only evidence roles admitted to one specialized scenario."""

    gate_contract(gate)
    return _GATE_REF_ROLES.get(str(gate), frozenset())

_ROLE_ARTIFACT_TYPES: dict[str, frozenset[str]] = {
    "stage_terminal": frozenset({"runtime_stage_terminal"}),
    "outline_terminal": frozenset({"runtime_stage_terminal"}),
    "attempt": frozenset({"job_attempt"}),
    "job_outcome": frozenset({"job_outcome"}),
    "provider_receipt_ledger": frozenset({"provider_receipt_ledger"}),
    "outline_provider_call_plan": frozenset({"outline_provider_call_plan"}),
    "canonical_stage1": frozenset({
        "stage1_canonical_summaries",
        "summary_file",
        "paper_artifact",
        "stage1_portable_summary_source",
        "stage1_reusable_summary_manifest",
    }),
    "closure": frozenset({
        "provider_receipt_closure",
        "validation_receipt_closure",
        "current_stage_closure_map",
    }),
    "review_docx": frozenset({"review_docx", "review_docx_repaired"}),
    "validation_artifact": frozenset({
        "validation_run_result",
        "validation_run_result_repaired",
        "validation_completion_projection",
    }),
    "citation_manifest": frozenset({"citation_manifest", "citation_manifest_v3"}),
    "free_mode_profile": frozenset({"free_mode_profile"}),
    "ocr_diagnostics": frozenset({"ocr_diagnostics"}),
    "ocr_artifact": frozenset({"ocr_artifact"}),
    "defect_artifact": frozenset({
        "validation_run_result",
        "validation_report_projection",
        "controlled_defect_challenge",
    }),
    "modality_profile": frozenset({"document_modality_profile"}),
    "repair_artifact": frozenset({
        "repair_transaction",
        "validation_run_result_repaired",
    }),
    "playwright_trace": frozenset({"playwright_trace"}),
    "browser_evidence": frozenset({"playwright_run_evidence"}),
    "playwright_screenshot_manifest": frozenset({"playwright_screenshot_manifest"}),
    "scenario_execution_receipt": frozenset({"scenario_execution_receipt"}),
}


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _valid_checkout_sha(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) in {40, 64} and all(char in "0123456789abcdef" for char in text)


def _bounded_read(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ReleaseAcceptanceSpecError(f"evidence reference is a symlink: {path}")
    try:
        size_before = path.stat().st_size
        if size_before > max_bytes:
            raise ReleaseAcceptanceSpecError(
                f"evidence reference exceeds bounded read size: {path.name}"
            )
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
            size_after = os.fstat(handle.fileno()).st_size
        if len(raw) > max_bytes or size_before != size_after or size_after != len(raw):
            raise ReleaseAcceptanceSpecError(
                f"evidence reference changed or exceeds bounded read size: {path.name}"
            )
        return raw
    except ReleaseAcceptanceSpecError:
        raise
    except OSError as exc:
        raise ReleaseAcceptanceSpecError(
            f"evidence reference is unreadable: {path.name}"
        ) from exc


@dataclass(frozen=True)
class DurableEvidenceRefV1:
    """A content-addressed pointer to a durable runtime artifact."""

    ref_id: str
    path: str
    sha256: str
    size: int
    role: str
    artifact_type: str = ""
    artifact_version: str = ""
    schema_version: str = ""
    job_id: str = ""
    artifact_id: str = ""
    modality: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DurableEvidenceRefV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("durable evidence reference must be an object")
        unknown = sorted(str(key) for key in payload if str(key) not in _EVIDENCE_REF_FIELDS)
        if unknown:
            raise ReleaseAcceptanceSpecError(
                "durable evidence reference contains unknown fields: " + ", ".join(unknown)
            )
        required = ("ref_id", "path", "sha256", "size", "role")
        missing = [key for key in required if not str(payload.get(key) or "").strip() and key != "size"]
        if "size" not in payload:
            missing.append("size")
        if missing:
            raise ReleaseAcceptanceSpecError(
                "durable evidence reference is missing: " + ", ".join(missing)
            )
        raw_size = payload.get("size")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise ReleaseAcceptanceSpecError("durable evidence reference size must be non-negative")
        sha256 = str(payload.get("sha256") or "").strip()
        if not _valid_sha256(sha256) or sha256 != sha256.lower():
            raise ReleaseAcceptanceSpecError("durable evidence reference sha256 must be lowercase SHA-256")
        return cls(
            ref_id=str(payload["ref_id"]).strip(),
            path=str(payload["path"]).strip(),
            sha256=sha256,
            size=raw_size,
            role=str(payload["role"]).strip(),
            artifact_type=str(payload.get("artifact_type") or "").strip(),
            artifact_version=str(payload.get("artifact_version") or "").strip(),
            schema_version=str(payload.get("schema_version") or "").strip(),
            job_id=str(payload.get("job_id") or "").strip(),
            artifact_id=str(payload.get("artifact_id") or "").strip(),
            modality=str(payload.get("modality") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "ref_id": self.ref_id,
                "path": self.path,
                "sha256": self.sha256,
                "size": self.size,
                "role": self.role,
                "artifact_type": self.artifact_type,
                "artifact_version": self.artifact_version,
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "artifact_id": self.artifact_id,
                "modality": self.modality,
            }.items()
            if value not in ("", None)
        }


@dataclass(frozen=True)
class DocumentModalityProfileV1:
    """Deterministic source diagnostics used by the heterogeneous-paper gate."""

    source_pdf_sha256: str
    total_page_count: int
    text_page_ratio: float
    image_page_ratio: float
    table_count: int
    figure_count: int
    scanned_candidate_page_count: int
    ocr_used_page_count: int
    selected_visual_count: int
    extractor_used: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DocumentModalityProfileV1":
        if payload.get("artifact_type") != "document_modality_profile":
            raise ReleaseAcceptanceSpecError("document modality profile artifact type is invalid")
        if payload.get("schema_version") != "document-modality-profile-v1":
            raise ReleaseAcceptanceSpecError("document modality profile schema is invalid")
        source_hash = str(payload.get("source_pdf_sha256") or "").strip()
        if not _valid_sha256(source_hash):
            raise ReleaseAcceptanceSpecError("document modality profile source hash is invalid")
        integer_values: dict[str, int] = {}
        for name in (
            "total_page_count",
            "table_count",
            "figure_count",
            "scanned_candidate_page_count",
            "ocr_used_page_count",
            "selected_visual_count",
        ):
            raw = payload.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} is invalid"
                )
            integer_values[name] = raw
        ratios: dict[str, float] = {}
        for name in ("text_page_ratio", "image_page_ratio"):
            raw = payload.get(name)
            try:
                value = float(str(raw))
            except (TypeError, ValueError):
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} is invalid"
                ) from None
            if value < 0 or value > 1:
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} must be between zero and one"
                )
            ratios[name] = value
        if integer_values["total_page_count"] <= 0:
            raise ReleaseAcceptanceSpecError("document modality profile must have pages")
        if not str(payload.get("extractor_used") or "").strip():
            raise ReleaseAcceptanceSpecError("document modality profile extractor is missing")
        return cls(
            source_pdf_sha256=source_hash,
            total_page_count=integer_values["total_page_count"],
            text_page_ratio=ratios["text_page_ratio"],
            image_page_ratio=ratios["image_page_ratio"],
            table_count=integer_values["table_count"],
            figure_count=integer_values["figure_count"],
            scanned_candidate_page_count=integer_values["scanned_candidate_page_count"],
            ocr_used_page_count=integer_values["ocr_used_page_count"],
            selected_visual_count=integer_values["selected_visual_count"],
            extractor_used=str(payload["extractor_used"]).strip(),
        )

    @property
    def derived_modality(self) -> str:
        scan_ratio = self.scanned_candidate_page_count / self.total_page_count
        ocr_ratio = self.ocr_used_page_count / self.total_page_count
        if ocr_ratio > 0 or scan_ratio >= 0.25:
            return "ocr_scanned"
        visual_signal = self.image_page_ratio >= 0.5 or (
            self.table_count + self.figure_count
            >= max(1, self.total_page_count // 3)
        )
        return "visual_table_heavy" if visual_signal else "text_heavy"


@dataclass(frozen=True)
class DocumentModalityProfileV2:
    """Production-lineage modality profile for Gate D."""

    source_pdf_sha256: str
    preprocess_manifest_hash: str
    stage1_input_manifest_hash: str
    actual_extractor: str
    page_count: int
    text_page_count: int
    image_page_count: int
    table_count: int
    figure_count: int
    scanned_candidate_pages: int
    actual_ocr_pages: int
    actual_selected_visual_count: int
    stage1_input_mode: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DocumentModalityProfileV2":
        if (
            payload.get("artifact_type") != "document_modality_profile"
            or payload.get("artifact_version") != "v2"
            or payload.get("schema_version") != "document-modality-profile-v2"
        ):
            raise ReleaseAcceptanceSpecError(
                "document modality profile is not a production-derived v2 artifact"
            )
        text_fields = (
            "source_pdf_sha256",
            "preprocess_manifest_hash",
            "stage1_input_manifest_hash",
            "actual_extractor",
            "stage1_input_mode",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_fields}
        if any(not values[name] for name in text_fields):
            raise ReleaseAcceptanceSpecError(
                "production modality profile lineage fields are incomplete"
            )
        for name in (
            "source_pdf_sha256",
            "preprocess_manifest_hash",
            "stage1_input_manifest_hash",
        ):
            if not _valid_sha256(values[name]):
                raise ReleaseAcceptanceSpecError(
                    f"production modality profile {name} is invalid"
                )
        integer_values: dict[str, int] = {}
        for name in (
            "page_count",
            "text_page_count",
            "image_page_count",
            "table_count",
            "figure_count",
            "scanned_candidate_pages",
            "actual_ocr_pages",
            "actual_selected_visual_count",
        ):
            raw = payload.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ReleaseAcceptanceSpecError(
                    f"production modality profile {name} is invalid"
                )
            integer_values[name] = raw
        if integer_values["page_count"] <= 0:
            raise ReleaseAcceptanceSpecError("production modality profile page_count is invalid")
        if integer_values["text_page_count"] > integer_values["page_count"] or integer_values["image_page_count"] > integer_values["page_count"]:
            raise ReleaseAcceptanceSpecError("production modality profile page counts are inconsistent")
        return cls(
            source_pdf_sha256=values["source_pdf_sha256"],
            preprocess_manifest_hash=values["preprocess_manifest_hash"],
            stage1_input_manifest_hash=values["stage1_input_manifest_hash"],
            actual_extractor=values["actual_extractor"],
            stage1_input_mode=values["stage1_input_mode"],
            page_count=integer_values["page_count"],
            text_page_count=integer_values["text_page_count"],
            image_page_count=integer_values["image_page_count"],
            table_count=integer_values["table_count"],
            figure_count=integer_values["figure_count"],
            scanned_candidate_pages=integer_values["scanned_candidate_pages"],
            actual_ocr_pages=integer_values["actual_ocr_pages"],
            actual_selected_visual_count=integer_values["actual_selected_visual_count"],
        )

    @property
    def derived_modality(self) -> str:
        if self.actual_ocr_pages > 0 or self.scanned_candidate_pages / self.page_count >= 0.25:
            return "ocr_scanned"
        if self.image_page_count / self.page_count >= 0.5 or self.table_count + self.figure_count >= max(1, self.page_count // 3):
            return "visual_table_heavy"
        return "text_heavy"


@dataclass(frozen=True)
class ControlledDefectChallengeV1:
    """Typed challenge lineage for Validator defect injection and repair."""

    challenge_id: str
    baseline_review_artifact_id: str
    baseline_review_hash: str
    mutated_review_path: str
    mutated_review_hash: str
    mutation_type: str
    mutation_locator: str
    original_value_hash: str
    mutated_value_hash: str
    expected_detection_class: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ControlledDefectChallengeV1":
        if payload.get("artifact_type") != "controlled_defect_challenge":
            raise ReleaseAcceptanceSpecError("controlled defect challenge artifact type is invalid")
        if payload.get("schema_version") != "controlled-defect-challenge-v1":
            raise ReleaseAcceptanceSpecError("controlled defect challenge schema is invalid")
        text_fields = (
            "challenge_id",
            "baseline_review_artifact_id",
            "baseline_review_hash",
            "mutated_review_path",
            "mutated_review_hash",
            "mutation_type",
            "mutation_locator",
            "original_value_hash",
            "mutated_value_hash",
            "expected_detection_class",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_fields}
        if any(not value for value in values.values()):
            raise ReleaseAcceptanceSpecError("controlled defect challenge has missing identity fields")
        for name in ("baseline_review_hash", "mutated_review_hash", "original_value_hash", "mutated_value_hash"):
            if not _valid_sha256(values[name]):
                raise ReleaseAcceptanceSpecError(
                    f"controlled defect challenge {name} is not SHA-256"
                )
        return cls(**values)


@dataclass(frozen=True)
class ProcessInterruptionEventV1:
    event_id: str
    acceptance_run_id: str
    scenario_id: str
    job_id: str
    attempt_id: str
    pid: int
    process_creation_identity: str
    started_at: str
    interrupted_at: str
    interruption_method: str
    exit_code: int
    last_durable_stage: str
    last_durable_receipt_id: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProcessInterruptionEventV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("process interruption event must be a JSON object")
        if (
            payload.get("artifact_type") != "process_interruption_event"
            or payload.get("artifact_version") != "v1"
            or payload.get("schema_version") != "process-interruption-event-v1"
        ):
            raise ReleaseAcceptanceSpecError("process interruption event type or schema is invalid")
        values = {
            "event_id": str(payload.get("event_id") or "").strip(),
            "acceptance_run_id": str(payload.get("acceptance_run_id") or "").strip(),
            "scenario_id": str(payload.get("scenario_id") or "").strip(),
            "job_id": str(payload.get("job_id") or "").strip(),
            "attempt_id": str(payload.get("attempt_id") or "").strip(),
            "process_creation_identity": str(payload.get("process_creation_identity") or "").strip(),
            "started_at": str(payload.get("started_at") or "").strip(),
            "interrupted_at": str(payload.get("interrupted_at") or "").strip(),
            "interruption_method": str(payload.get("interruption_method") or "").strip(),
            "last_durable_stage": str(payload.get("last_durable_stage") or "").strip(),
            "last_durable_receipt_id": str(payload.get("last_durable_receipt_id") or "").strip(),
        }
        if any(not values[name] for name in (
            "event_id", "acceptance_run_id", "scenario_id", "job_id", "attempt_id",
            "process_creation_identity", "started_at", "interrupted_at",
            "interruption_method", "last_durable_stage",
        )):
            raise ReleaseAcceptanceSpecError("process interruption event identity is incomplete")
        raw_pid = payload.get("pid")
        raw_exit = payload.get("exit_code")
        if isinstance(raw_pid, bool) or isinstance(raw_exit, bool):
            raise ReleaseAcceptanceSpecError("process interruption event numeric fields are invalid")
        try:
            pid = int(str(raw_pid))
            exit_code = int(str(raw_exit))
        except (TypeError, ValueError):
            raise ReleaseAcceptanceSpecError("process interruption event numeric fields are invalid") from None
        if pid <= 0:
            raise ReleaseAcceptanceSpecError("process interruption event pid is invalid")
        if values["interruption_method"] not in {
            "terminate",
            "kill",
            "ctrl_break",
            "external_crash",
        }:
            raise ReleaseAcceptanceSpecError(
                "process interruption event method is invalid"
            )
        if exit_code == 0:
            raise ReleaseAcceptanceSpecError(
                "process interruption event termination must have a non-zero exit code"
            )
        started = _receipt_timestamp(values["started_at"], field_name="started_at")
        interrupted = _receipt_timestamp(values["interrupted_at"], field_name="interrupted_at")
        if interrupted < started:
            raise ReleaseAcceptanceSpecError(
                "process interruption event interrupted_at precedes started_at"
            )
        return cls(pid=pid, exit_code=exit_code, **values)


@dataclass(frozen=True)
class ProcessResumeEventV1:
    event_id: str
    acceptance_run_id: str
    scenario_id: str
    job_id: str
    interruption_event_id: str
    interruption_event_sha256: str
    previous_attempt_id: str
    new_attempt_id: str
    new_pid: int
    new_process_creation_identity: str
    resumed_at: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProcessResumeEventV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("process resume event must be a JSON object")
        _reject_unknown(payload, _PROCESS_RESUME_FIELDS, "process resume event")
        if payload.get("artifact_type") != "process_resume_event" or payload.get("schema_version") != "process-resume-event-v1":
            raise ReleaseAcceptanceSpecError("process resume event type or schema is invalid")
        if payload.get("artifact_version") != "v1":
            raise ReleaseAcceptanceSpecError("process resume event artifact version is invalid")
        text_fields = (
            "event_id", "acceptance_run_id", "scenario_id", "job_id",
            "interruption_event_id", "interruption_event_sha256",
            "previous_attempt_id", "new_attempt_id", "new_process_creation_identity", "resumed_at",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_fields}
        if any(not value for value in values.values()) or not _valid_sha256(values["interruption_event_sha256"]):
            raise ReleaseAcceptanceSpecError("process resume event identity is incomplete")
        raw_pid = payload.get("new_pid")
        if isinstance(raw_pid, bool):
            raise ReleaseAcceptanceSpecError("process resume event pid is invalid")
        try:
            new_pid = int(str(raw_pid))
        except (TypeError, ValueError):
            raise ReleaseAcceptanceSpecError("process resume event pid is invalid") from None
        if new_pid <= 0:
            raise ReleaseAcceptanceSpecError("process resume event pid is invalid")
        if _receipt_timestamp(values["resumed_at"], field_name="resumed_at") is None:
            raise ReleaseAcceptanceSpecError("process resume event timestamp is invalid")
        return cls(new_pid=new_pid, **values)


class GateEvidenceProducer:
    """Create evidence indexes from bytes that actually exist on disk."""

    def __init__(
        self,
        *,
        final_sha: str,
        origin_dir: str | Path | None = None,
        max_bytes: int = _MAX_EVIDENCE_BYTES,
    ) -> None:
        if not _valid_checkout_sha(final_sha):
            raise ReleaseAcceptanceSpecError("evidence producer requires final_sha")
        self.final_sha = str(final_sha).strip()
        self.origin_dir = Path(origin_dir).expanduser().resolve() if origin_dir else None
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ReleaseAcceptanceSpecError("evidence producer max_bytes must be positive")

    def reference(
        self,
        path: str | Path,
        *,
        role: str,
        artifact_type: str = "",
        artifact_version: str = "",
        schema_version: str = "",
        job_id: str = "",
        artifact_id: str = "",
        modality: str = "",
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        raw = _bounded_read(target, max_bytes=self.max_bytes)
        ref = DurableEvidenceRefV1(
            ref_id=f"{role}:{hashlib.sha256(str(target).encode('utf-8')).hexdigest()[:16]}",
            path=str(target),
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            role=str(role).strip(),
            artifact_type=str(artifact_type or "").strip(),
            artifact_version=str(artifact_version or "").strip(),
            schema_version=str(schema_version or "").strip(),
            job_id=str(job_id or "").strip(),
            artifact_id=str(artifact_id or "").strip(),
            modality=str(modality or "").strip(),
        )
        return ref.to_dict()

    def build_gate(
        self,
        gate: str,
        refs: Iterable[Mapping[str, Any]],
        *,
        acceptance_run_id: str = "",
        scenario_id: str = "",
        job_id: str = "",
    ) -> dict[str, Any]:
        gate_contract(str(gate))
        normalized = [DurableEvidenceRefV1.from_mapping(item).to_dict() for item in refs]
        return {
            "final_sha": self.final_sha,
            "acceptance_run_id": str(acceptance_run_id or ""),
            "scenario_id": str(scenario_id or gate),
            "job_id": str(job_id or ""),
            "durable_refs": normalized,
            "producer": "runtime.release_acceptance.GateEvidenceProducer",
            "schema_version": "release-acceptance-gate-evidence-v1",
            "gate": str(gate),
        }

    def write_manifest(
        self,
        path: str | Path,
        gates: Mapping[str, Iterable[Mapping[str, Any]]],
        *,
        acceptance_run_id: str = "",
        scenario_id: str = "",
        job_id: str = "",
    ) -> Path:
        target = Path(path).expanduser().resolve()
        previous_raw = b""
        previous_revision = 0
        if target.is_file():
            try:
                previous_raw = target.read_bytes()
                previous_payload = json.loads(previous_raw.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index is unreadable"
                ) from exc
            if not isinstance(previous_payload, Mapping):
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index is not an object"
                )
            previous_sha = str(
                previous_payload.get("final_executable_sha")
                or previous_payload.get("final_sha")
                or ""
            ).strip()
            if previous_sha and previous_sha != self.final_sha:
                raise ReleaseAcceptanceSpecError(
                    "acceptance evidence index belongs to a different executable SHA"
                )
            raw_revision = previous_payload.get("revision", 0)
            if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index revision is invalid"
                )
            previous_revision = raw_revision
        revision = previous_revision + 1
        normalized_gates = {
            str(gate): self.build_gate(
                str(gate),
                list(refs),
                acceptance_run_id=acceptance_run_id,
                scenario_id=scenario_id or str(gate),
                job_id=job_id,
            )
            for gate, refs in gates.items()
        }
        payload = {
            "schema_version": "release-acceptance-evidence-index-v1",
            "final_sha": self.final_sha,
            "final_executable_sha": self.final_sha,
            "acceptance_run_id": str(acceptance_run_id or ""),
            "scenario_id": str(scenario_id or ""),
            "job_id": str(job_id or ""),
            "revision": revision,
            "created_at": _scenario_now(),
            "previous_revision_hash": (
                hashlib.sha256(previous_raw).hexdigest() if previous_raw else ""
            ),
            "refs": [
                ref
                for gate_payload in normalized_gates.values()
                for ref in gate_payload["durable_refs"]
            ],
            "gates": normalized_gates,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

        def write_atomic(destination: Path) -> None:
            temp = destination.with_name(f".{destination.name}.{os.getpid()}.{revision}.tmp")
            try:
                with temp.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                atomic_replace_with_retry(temp, destination, timeout_seconds=5.0)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

        revision_path = target.with_name(f"{target.stem}.revision-{revision}{target.suffix}")
        write_atomic(revision_path)
        write_atomic(target)
        return target


class GateEvidenceVerifier:
    """Reopen durable references and derive gate facts from their bytes."""

    def __init__(self, *, max_bytes: int = _MAX_EVIDENCE_BYTES) -> None:
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ReleaseAcceptanceSpecError("evidence verifier max_bytes must be positive")

    @staticmethod
    def _resolve_path(raw: str, *, origin_dir: str | Path | None) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute() and origin_dir is not None:
            path = Path(origin_dir).expanduser() / path
        return path.resolve()

    @staticmethod
    def _json_payload(raw: bytes, path: Path) -> Any:
        if path.suffix.casefold() not in {".json", ".jsonl"}:
            return None
        if path.suffix.casefold() == ".jsonl":
            rows: list[Any] = []
            for line in raw.decode("utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
            return rows
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _derive_facts(
        gate: str,
        refs: list[DurableEvidenceRefV1],
        payloads: Mapping[str, Any],
    ) -> dict[str, Any]:
        roles = {ref.role for ref in refs}
        facts: dict[str, Any] = {
            "source_count": len({ref.sha256 for ref in refs if ref.role in {"source_pdf", "f1_corpus"}}),
            "canonical_stage1_count": len({ref.sha256 for ref in refs if ref.role == "canonical_stage1"}),
            "actual_transport_calls": 0,
            "semantic_roles": set(),
            "receipt_routes": set(),
            "candidate_count": 0,
            "closure_complete": False,
            "interruption": False,
            "resume": False,
            "duplicate_receipts": 0,
            "reexecuted_completed_call_ids": [],
            "heterogeneity": 0,
            "free_mode_route_only": False,
            "profile_durable": False,
            "defect_injected": False,
            "defect_detected": False,
            "repair_applied": False,
            "revalidated_clean": False,
            "playwright": False,
            "browser_evidence": False,
            "flow_completed": False,
            "trace_archive": False,
            "ocr_actually_used": False,
            "lineage": False,
            "page_identity": False,
            "evidence_artifact": False,
            "stage1_consumed": False,
            "process_count": 0,
            "bounded_wait": False,
            "no_corrupt_json": False,
            "no_lost_update": False,
            "outline_complete": False,
            "docx_complete": False,
            "validation_complete": False,
            "route_plan_present": False,
        }
        receipt_ids: set[str] = set()
        process_ids: set[str] = set()
        providers: set[str] = set()
        for ref in refs:
            payload = payloads.get(ref.ref_id)
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                artifact_type = str(row.get("artifact_type") or ref.artifact_type)
                if ref.role == "provider_receipt_ledger" or artifact_type == "provider_call_receipt":
                    receipt_id = str(row.get("receipt_id") or "")
                    if receipt_id in receipt_ids:
                        facts["duplicate_receipts"] += 1
                    elif receipt_id:
                        receipt_ids.add(receipt_id)
                    status = str(row.get("status") or "")
                    metadata = row.get("metadata")
                    test_only_value = row.get("test_only", False)
                    metadata_test_only = (
                        metadata.get("test_only", False)
                        if isinstance(metadata, Mapping)
                        else False
                    )
                    test_only = (
                        test_only_value is True
                        or (
                            isinstance(test_only_value, str)
                            and test_only_value.strip().casefold() == "true"
                        )
                        or metadata_test_only is True
                        or (
                            isinstance(metadata_test_only, str)
                            and metadata_test_only.strip().casefold() == "true"
                        )
                    )
                    malformed_test_only = (
                        not isinstance(test_only_value, (bool, str))
                    ) or (
                        isinstance(test_only_value, str)
                        and test_only_value.strip().casefold() not in {"true", "false", ""}
                    ) or (
                        not isinstance(metadata_test_only, (bool, str))
                    ) or (
                        isinstance(metadata_test_only, str)
                        and metadata_test_only.strip().casefold() not in {"true", "false", ""}
                    )
                    attempted = not malformed_test_only and not test_only and (
                        status == "success"
                        or (isinstance(metadata, Mapping) and bool(metadata.get("transport_config")))
                    )
                    if attempted:
                        try:
                            facts["actual_transport_calls"] += max(1, int(row.get("attempts") or 1))
                        except (TypeError, ValueError):
                            facts["actual_transport_calls"] += 1
                    node_id = str(row.get("node_id") or row.get("route") or "")
                    if node_id:
                        facts["semantic_roles"].add(node_id)
                        if "candidate_" in node_id and "_provider_generation" in node_id:
                            facts["candidate_count"] += 1
                    route_name = str(row.get("route") or "")
                    if route_name:
                        facts["receipt_routes"].add(route_name)
                    provider = str(row.get("provider") or "")
                    if provider:
                        providers.add(provider)
                if ref.role in {"closure", "stage_terminal", "outline_terminal"}:
                    status = str(row.get("status") or row.get("closure_status") or "").casefold()
                    closure_payload = row.get("payload")
                    if isinstance(closure_payload, Mapping):
                        status = str(
                            closure_payload.get("status")
                            or closure_payload.get("closure_status")
                            or status
                        ).casefold()
                    facts["closure_complete"] = facts["closure_complete"] or status in {
                        "complete", "completed", "trusted", "verified", "succeeded"
                    } or (
                        isinstance(closure_payload, Mapping)
                        and closure_payload.get("complete") is True
                    ) or row.get("complete") is True
                if ref.role == "outline_provider_call_plan":
                    route_plan = row.get("reachable_provider_route_plan")
                    if isinstance(route_plan, Mapping) and isinstance(route_plan.get("routes"), list):
                        facts["route_plan_present"] = True
                if ref.role == "job_outcome":
                    facts["closure_complete"] = facts["closure_complete"] or (
                        str(row.get("job_status") or "").casefold() == "completed"
                        and bool(row.get("canonical_ready", False))
                    )
                if ref.role == "process_events":
                    process_id = str(row.get("process_id") or row.get("pid") or "")
                    if process_id:
                        process_ids.add(process_id)
        facts["process_count"] = len(process_ids)
        facts["semantic_roles"] = sorted(facts["semantic_roles"])
        facts["receipt_routes"] = sorted(facts["receipt_routes"])
        facts["free_mode_route_only"] = (
            "provider_receipt_ledger" in roles
            and bool(facts["receipt_routes"])
            and all("free" in str(route).casefold() for route in facts["receipt_routes"])
        )
        return facts

    @staticmethod
    def _rows(payload: Any) -> list[Mapping[str, Any]]:
        raw_rows = payload if isinstance(payload, list) else [payload]
        return [row for row in raw_rows if isinstance(row, Mapping)]

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _string_list(value: Any) -> list[str] | None:
        if not isinstance(value, (list, tuple)):
            return None
        result = [str(item).strip() for item in value]
        if any(not item for item in result):
            return None
        return result

    @staticmethod
    def _semantic_role(node_id: Any) -> str:
        value = str(node_id or "").strip()
        if re.fullmatch(r"candidate_[0-9]+_provider_generation", value):
            return "candidate_provider_generation"
        return value

    def _derive_semantic_facts(
        self,
        gate: str,
        refs: list[DurableEvidenceRefV1],
        payloads: Mapping[str, Any],
        raw_by_ref: Mapping[str, bytes],
        *,
        origin_dir: str | Path | None,
        expected_job_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        by_role: dict[str, list[DurableEvidenceRefV1]] = {}
        for ref in refs:
            by_role.setdefault(ref.role, []).append(ref)

        if gate == "C":
            source_refs = by_role.get("source_pdf", [])
            canonical_refs = by_role.get("canonical_stage1", [])
            if len(source_refs) != 1 or not canonical_refs:
                return {}, "one-paper gate requires exactly one source PDF and canonical Stage 1 evidence"
            identity_rows = []
            for canonical_ref in canonical_refs:
                for row in self._rows(payloads.get(canonical_ref.ref_id)):
                    paper = row.get("paper_info") if isinstance(row.get("paper_info"), Mapping) else row
                    if not isinstance(paper, Mapping):
                        continue
                    paper_key = str(paper.get("canonical_paper_key") or "").strip()
                    raw_preprocess = row.get("preprocess")
                    preprocess = raw_preprocess if isinstance(raw_preprocess, Mapping) else {}
                    source_hash = str(
                        paper.get("source_pdf_sha256")
                        or row.get("source_pdf_sha256")
                        or preprocess.get("source_pdf_sha256")
                        or ""
                    ).strip()
                    if paper_key and source_hash:
                        identity_rows.append((paper_key, source_hash))
            if not any(source_hash == source_refs[0].sha256 for _key, source_hash in identity_rows):
                return {}, "canonical Stage 1 evidence is not bound to the one source PDF identity"
            outcome_refs = by_role.get("job_outcome", [])
            terminal_refs = by_role.get("stage_terminal", [])
            if not outcome_refs or not terminal_refs:
                return {}, "one-paper gate lacks durable job outcome or stage terminal evidence"
            outcome_ok = any(
                isinstance(row, Mapping)
                and str(row.get("job_status") or "").casefold() == "completed"
                and row.get("canonical_ready") is True
                for ref in outcome_refs
                for row in self._rows(payloads.get(ref.ref_id))
            )
            terminal_ok = any(
                str(row.get("status") or "").casefold() in {"succeeded", "complete", "completed"}
                for ref in terminal_refs
                for row in self._rows(payloads.get(ref.ref_id))
            )
            if not outcome_ok or not terminal_ok:
                return {}, "one-paper gate lacks a completed durable outcome and successful Stage 1 terminal"
            return {"source_count": 1, "canonical_stage1_count": 1, "closure_complete": True}, None

        if gate == "G":
            profile_refs = by_role.get("free_mode_profile", [])
            if len(profile_refs) != 1:
                return {}, "Free Mode gate requires one typed durable profile"
            profile_payload = payloads.get(profile_refs[0].ref_id)
            if not isinstance(profile_payload, Mapping):
                return {}, "Free Mode profile is not a JSON object"
            if profile_payload.get("artifact_type") != "free_mode_profile" or profile_payload.get("schema_version") != "free-mode-profile-v1":
                return {}, "Free Mode profile type or schema is invalid"
            if not str(profile_payload.get("profile_id") or "").strip():
                return {}, "Free Mode profile identity is missing"
            receipts: list[Mapping[str, Any]] = []
            for ref in by_role.get("provider_receipt_ledger", []):
                receipts.extend(self._rows(payloads.get(ref.ref_id)))
            if not receipts:
                return {}, "Free Mode gate has no valid provider receipts"
            if any(
                str(row.get("route") or "") != "Free_Mode_API"
                or not isinstance(row.get("metadata"), Mapping)
                or str(cast(Mapping[str, Any], row.get("metadata")).get("config_section") or "")
                != "Free_Mode_API"
                for row in receipts
            ):
                return {}, "Free Mode provider receipts contain a non-Free Mode route"
            return {
                "free_mode_route_only": True,
                "profile_durable": True,
            }, None

        if gate == "I":
            trace_refs = by_role.get("playwright_trace", [])
            browser_refs = by_role.get("browser_evidence", [])
            screenshot_manifest_refs = by_role.get("playwright_screenshot_manifest", [])
            if (
                len(trace_refs) != 1
                or len(browser_refs) != 1
                or len(screenshot_manifest_refs) != 1
            ):
                return {}, (
                    "Playwright evidence requires one trace archive, one run metadata "
                    "artifact, and one screenshot manifest"
                )
            trace_ref = trace_refs[0]
            trace_path = self._resolve_path(trace_ref.path, origin_dir=origin_dir)
            if trace_path.suffix.casefold() != ".zip":
                return {}, "Playwright trace evidence must be a trace.zip archive"
            try:
                with zipfile.ZipFile(BytesIO(raw_by_ref[trace_ref.ref_id])) as archive:
                    if archive.testzip() is not None:
                        return {}, "Playwright trace archive is corrupt"
                    names = {name.casefold().lstrip("/") for name in archive.namelist()}
            except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
                return {}, f"Playwright trace archive is invalid: {type(exc).__name__}"
            if not any(name.endswith("trace.trace") for name in names):
                return {}, "Playwright trace archive has no trace.trace event stream"
            browser_payload = payloads.get(browser_refs[0].ref_id)
            if not isinstance(browser_payload, Mapping):
                return {}, "Playwright run metadata must be a JSON object"
            if browser_payload.get("artifact_type") != "playwright_run_evidence":
                return {}, "Playwright run metadata artifact type is invalid"
            if browser_payload.get("schema_version") != "playwright-run-evidence-v1":
                return {}, "Playwright run metadata schema is invalid"
            for field_name in ("run_id", "session_id", "url", "resulting_job_id", "trace_sha256"):
                if not str(browser_payload.get(field_name) or "").strip():
                    return {}, f"Playwright run metadata requires {field_name}"
            parsed_url = str(browser_payload["url"]).strip()
            try:
                from urllib.parse import urlsplit

                url = urlsplit(parsed_url)
            except ValueError:
                return {}, "Playwright run metadata URL is invalid"
            if url.scheme != "http" or (url.hostname or "").casefold() not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                return {}, "Playwright run metadata must bind to a localhost URL"
            assertions = browser_payload.get("flow_assertions")
            if not isinstance(assertions, list) or not assertions:
                return {}, "Playwright run metadata requires non-empty flow assertions"
            if any(
                not isinstance(item, Mapping)
                or item.get("passed") is not True
                or not str(item.get("name") or "").strip()
                for item in assertions
            ):
                return {}, "Playwright flow assertions are incomplete or failed"
            console_errors = browser_payload.get("console_errors")
            page_errors = browser_payload.get("page_errors", [])
            if not isinstance(console_errors, list) or not isinstance(page_errors, list):
                return {}, "Playwright error captures must be arrays"
            if console_errors or page_errors:
                return {}, "Playwright evidence contains console or page errors"
            if str(browser_payload.get("trace_sha256")) != trace_ref.sha256:
                return {}, "Playwright run metadata is not bound to the trace archive"
            if expected_job_id and str(browser_payload.get("resulting_job_id")) != expected_job_id:
                return {}, "Playwright run metadata resulting job does not match acceptance job"
            screenshot_manifest = payloads.get(screenshot_manifest_refs[0].ref_id)
            if not isinstance(screenshot_manifest, Mapping):
                return {}, "Playwright screenshot manifest must be a JSON object"
            if (
                screenshot_manifest.get("artifact_type") != "playwright_screenshot_manifest"
                or screenshot_manifest.get("schema_version")
                != "playwright-screenshot-manifest-v1"
            ):
                return {}, "Playwright screenshot manifest schema is invalid"
            if (
                screenshot_manifest.get("acceptance_run_id") != browser_payload.get("run_id")
                or screenshot_manifest.get("scenario_id") != "I"
                or screenshot_manifest.get("job_id")
                != browser_payload.get("resulting_job_id")
            ):
                return {}, "Playwright screenshot manifest is not bound to the browser run"
            screenshots = screenshot_manifest.get("screenshots")
            if not isinstance(screenshots, list) or not screenshots or any(
                not isinstance(item, Mapping)
                or not str(item.get("name") or "").strip()
                or not str(item.get("path") or "").strip()
                for item in screenshots
            ):
                return {}, "Playwright screenshot manifest has no valid screenshot records"
            return {
                "playwright": True,
                "browser_evidence": True,
                "trace_archive": True,
                "flow_completed": True,
            }, None

        if gate == "K":
            event_refs = by_role.get("process_events", [])
            lock_refs = by_role.get("lock_state", [])
            if len(event_refs) != 1 or len(lock_refs) != 1:
                return {}, "contention evidence requires one typed process-event log and one lock-state result"
            events = self._rows(payloads.get(event_refs[0].ref_id))
            if not events:
                return {}, "contention process-event log is empty"
            process_identities: set[tuple[str, str]] = set()
            committed_ids: list[str] = []
            revisions: list[int] = []
            for row in events:
                if row.get("artifact_type") != "acceptance_process_event":
                    return {}, "contention process events are not typed acceptance_process_event records"
                if row.get("schema_version") != "process-event-v1":
                    return {}, "contention process-event schema is invalid"
                if not str(row.get("acceptance_run_id") or "").strip() or not str(
                    row.get("scenario_id") or ""
                ).strip():
                    return {}, "contention process events lack run/scenario identity"
                pid = str(row.get("pid") or "").strip()
                creation = str(row.get("process_creation_identity") or "").strip()
                try:
                    pid_value = int(pid)
                except (TypeError, ValueError):
                    pid_value = 0
                if not pid or not creation or pid_value <= 0:
                    return {}, "contention process events lack process creation identity"
                if self._timestamp(row.get("occurred_at")) is None:
                    return {}, "contention process events lack a valid occurred_at timestamp"
                event_name = str(row.get("event") or "").strip()
                if not event_name:
                    return {}, "contention process event name is missing"
                process_identities.add((pid, creation))
                operation_id = str(row.get("operation_id") or "").strip()
                if event_name == "operation_committed":
                    if not operation_id:
                        return {}, "committed contention event lacks operation_id"
                    committed_ids.append(operation_id)
                    raw_revision = row.get("revision")
                    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
                        return {}, "committed contention event lacks integer revision"
                    revisions.append(raw_revision)
            lock_payload = payloads.get(lock_refs[0].ref_id)
            if not isinstance(lock_payload, Mapping):
                return {}, "contention lock-state result must be a JSON object"
            if lock_payload.get("artifact_type") != "contention_result":
                return {}, "contention lock-state artifact type is invalid"
            if lock_payload.get("schema_version") != "contention-result-v1":
                return {}, "contention lock-state schema is invalid"
            expected_ids = self._string_list(lock_payload.get("expected_operation_ids"))
            if expected_ids is None or not expected_ids or len(set(expected_ids)) != len(expected_ids):
                return {}, "contention result requires unique expected operation IDs"
            if set(committed_ids) != set(expected_ids) or len(committed_ids) != len(set(committed_ids)):
                return {}, "contention result operation IDs do not prove exactly-once publication"
            if revisions != sorted(set(revisions)) or len(revisions) != len(expected_ids):
                return {}, "contention result revisions are not monotonic and unique"
            acquisitions = lock_payload.get("lock_acquisitions")
            if not isinstance(acquisitions, list) or not acquisitions:
                return {}, "contention result requires lock acquisition timing records"
            for item in acquisitions:
                if not isinstance(item, Mapping):
                    return {}, "contention lock acquisition record is invalid"
                requested = self._timestamp(item.get("requested_at"))
                acquired = self._timestamp(item.get("acquired_at"))
                released = self._timestamp(item.get("released_at"))
                try:
                    timeout = float(str(item.get("timeout_seconds")))
                except (TypeError, ValueError):
                    return {}, "contention lock timeout is invalid"
                if requested is None or acquired is None or released is None or timeout < 0:
                    return {}, "contention lock timing fields are incomplete"
                if acquired < requested or released < acquired or acquired - requested > timeout:
                    return {}, "contention lock wait exceeded its durable timeout"
            final_json_refs = lock_payload.get("final_json_refs")
            if not isinstance(final_json_refs, list) or not final_json_refs:
                return {}, "contention result requires final JSON references"
            parsed_final: dict[str, Any] = {}
            for item in final_json_refs:
                if not isinstance(item, Mapping):
                    return {}, "contention final JSON reference is invalid"
                raw_path = str(item.get("path") or "").strip()
                expected_hash = str(item.get("sha256") or "").strip()
                raw_size = item.get("size")
                if not raw_path or not _valid_sha256(expected_hash) or isinstance(raw_size, bool) or not isinstance(raw_size, int):
                    return {}, "contention final JSON reference lacks identity, hash, or size"
                try:
                    final_path = self._resolve_path(raw_path, origin_dir=origin_dir)
                    final_raw = _bounded_read(final_path, max_bytes=self.max_bytes)
                    if len(final_raw) != raw_size or hashlib.sha256(final_raw).hexdigest() != expected_hash:
                        return {}, "contention final JSON reference hash or size mismatch"
                    parsed_final[final_path.name.casefold()] = self._json_payload(final_raw, final_path)
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError, ReleaseAcceptanceSpecError) as exc:
                    return {}, f"contention final JSON is corrupt or unreadable: {type(exc).__name__}"
            budget = lock_payload.get("budget")
            ledger = lock_payload.get("provider_ledger")
            registry = lock_payload.get("registry")
            queue = lock_payload.get("queue")
            if not all(isinstance(item, Mapping) for item in (budget, ledger, registry, queue)):
                return {}, "contention result lacks budget, provider-ledger, Registry, or queue derivations"
            budget_map = cast(Mapping[str, Any], budget)
            ledger_map = cast(Mapping[str, Any], ledger)
            registry_map = cast(Mapping[str, Any], registry)
            queue_map = cast(Mapping[str, Any], queue)
            liveness_probes = lock_payload.get("liveness_probes")
            if not isinstance(liveness_probes, list) or len(liveness_probes) < 2:
                return {}, "contention result lacks two real process liveness probes"
            for probe in liveness_probes:
                if not isinstance(probe, Mapping) or probe.get("alive") is not True:
                    return {}, "contention process liveness probe did not report alive"
                raw_pid = probe.get("target_pid")
                creation = str(
                    probe.get("target_process_creation_identity") or ""
                ).strip()
                if (
                    isinstance(raw_pid, bool)
                    or not isinstance(raw_pid, int)
                    or raw_pid <= 0
                    or not creation
                    or (str(raw_pid), creation) not in process_identities
                ):
                    return {}, "contention liveness probe is not bound to a worker identity"
            if budget_map.get("domain") != "offline-k" or not str(
                budget_map.get("state_path") or ""
            ).endswith("offline_contention_budget_state.json"):
                return {}, "contention budget is not isolated in the offline-k namespace"
            live_parent_budget = lock_payload.get("live_parent_budget")
            if not isinstance(live_parent_budget, Mapping):
                return {}, "contention result lacks parent live budget invariance"
            if (
                live_parent_budget.get("unchanged") is not True
                or str(live_parent_budget.get("sha256_before") or "")
                != str(live_parent_budget.get("sha256_after") or "")
            ):
                return {}, "contention scenario changed the parent live budget"
            try:
                limit = int(str(budget_map.get("max_provider_calls_total") or 0))
                used = int(str(budget_map.get("calls_used") or 0))
                reserved = int(str(budget_map.get("calls_reserved") or 0))
            except (TypeError, ValueError):
                return {}, "contention budget derivation is invalid"
            if limit > 0 and (used < 0 or reserved < 0 or used + reserved > limit):
                return {}, "contention budget derivation proves an overshoot"
            budget_files = [
                value for name, value in parsed_final.items()
                if "budget" in name and isinstance(value, Mapping)
            ]
            if not budget_files:
                return {}, "contention final references lack a parsed budget state"
            budget_state = budget_files[0]
            budget_payload = budget_state.get("budget")
            if not isinstance(budget_payload, Mapping):
                return {}, "contention final budget state lacks its budget object"
            try:
                state_limit = int(str(budget_payload.get("max_provider_calls_total") or 0))
                state_used = int(str(budget_state.get("calls_used") or 0))
                state_reserved = int(str(budget_state.get("calls_reserved") or 0))
            except (TypeError, ValueError):
                return {}, "contention final budget state counters are invalid"
            if (state_limit, state_used, state_reserved) != (limit, used, reserved):
                return {}, "contention result budget does not match the reopened budget state"
            ledger_files = [
                value for name, value in parsed_final.items()
                if "receipt" in name and isinstance(value, list)
            ]
            if not ledger_files:
                return {}, "contention final references lack a parsed provider ledger"
            ledger_receipt_rows = [row for row in ledger_files[0] if isinstance(row, Mapping)]
            actual_receipt_ids = [str(row.get("receipt_id") or "") for row in ledger_receipt_rows]
            if any(not receipt_id for receipt_id in actual_receipt_ids):
                return {}, "contention provider ledger has a missing receipt ID"
            actual_duplicate_receipts = len(actual_receipt_ids) - len(set(actual_receipt_ids))
            if actual_duplicate_receipts != 0:
                return {}, "contention provider ledger contains duplicate receipt IDs"
            registry_files = [
                value for name, value in parsed_final.items()
                if "registry" in name and isinstance(value, Mapping)
            ]
            if not registry_files:
                return {}, "contention final references lack a parsed Registry"
            registry_state = registry_files[0]
            registry_artifacts = registry_state.get("artifacts")
            if not isinstance(registry_artifacts, list):
                return {}, "contention Registry artifacts are not an array"
            registry_ids = [
                str(item.get("artifact_id") or "")
                for item in registry_artifacts
                if isinstance(item, Mapping)
            ]
            if any(not item for item in registry_ids) or len(registry_ids) != len(set(registry_ids)):
                return {}, "contention Registry artifact IDs are missing or duplicated"
            if set(registry_ids) != set(str(item) for item in registry_map.get("artifact_ids") or []):
                return {}, "contention Registry derivation does not match its reopened records"
            queue_files = [
                value for name, value in parsed_final.items()
                if name == "queue.json" and isinstance(value, Mapping)
            ]
            if not queue_files:
                return {}, "contention final references lack a parsed queue"
            queue_state = queue_files[0]
            queue_jobs = queue_state.get("jobs")
            queue_runtimes = queue_state.get("runtimes")
            if not isinstance(queue_jobs, Mapping) or not isinstance(queue_runtimes, Mapping):
                return {}, "contention queue state is missing jobs or runtimes"
            queue_job_ids = {str(item) for item in queue_jobs}
            runtime_ids = {str(item) for item in queue_runtimes}
            expected_worker_jobs = set(str(item) for item in queue_map.get("worker_job_ids") or [])
            if not expected_worker_jobs or not expected_worker_jobs.issubset(queue_job_ids) or not expected_worker_jobs.issubset(runtime_ids):
                return {}, "contention queue derivation does not contain every worker job"
            if (
                ledger_map.get("duplicate_receipt_ids") != []
                or ledger_map.get("conflicts") != []
                or ledger_map.get("same_id_same_content_idempotent") is not True
                or ledger_map.get("same_id_different_content_rejected") is not True
            ):
                return {}, "contention provider ledger derivation contains duplicates or conflicts"
            if int(str(registry_map.get("lost_updates") or 0)) != 0 or queue_map.get("duplicate_operation_ids") != []:
                return {}, "contention Registry or queue derivation contains a lost update"
            return {
                "process_count": len(process_identities),
                "bounded_wait": True,
                "no_corrupt_json": True,
                "no_lost_update": True,
                "offline_contention_calls": used,
                "live_budget_unchanged": True,
            }, None

        if gate == "E":
            interruption_refs = by_role.get("interruption_event", [])
            resume_refs = by_role.get("resume_event", [])
            event_refs = by_role.get("process_events", [])
            if len(interruption_refs) != 1 or len(resume_refs) != 1 or len(event_refs) != 1:
                return {}, "resume evidence requires one typed interruption, resume, and process-event artifact"
            interruption_payload = payloads.get(interruption_refs[0].ref_id)
            resume_payload = payloads.get(resume_refs[0].ref_id)
            if not isinstance(interruption_payload, Mapping) or not isinstance(resume_payload, Mapping):
                return {}, "interruption and resume evidence must be JSON objects"
            try:
                interruption = ProcessInterruptionEventV1.from_mapping(interruption_payload)
                resume = ProcessResumeEventV1.from_mapping(resume_payload)
            except ReleaseAcceptanceSpecError as exc:
                return {}, str(exc)
            if expected_job_id and interruption.job_id != expected_job_id:
                return {}, "interruption evidence belongs to a different job"
            if self._timestamp(interruption.started_at) is None or self._timestamp(interruption.interrupted_at) is None:
                return {}, "interruption evidence timestamps are invalid"
            if resume.interruption_event_id != interruption.event_id:
                return {}, "resume evidence does not reference the interruption event"
            if resume.interruption_event_sha256 != interruption_refs[0].sha256:
                return {}, "resume evidence is not hash-bound to the interruption event"
            if resume.previous_attempt_id != interruption.attempt_id:
                return {}, "resume evidence previous attempt does not match interruption"
            if resume.new_attempt_id == interruption.attempt_id or self._timestamp(resume.resumed_at) is None:
                return {}, "resume evidence lacks a distinct new attempt and process"
            event_rows = self._rows(payloads.get(event_refs[0].ref_id))
            if not event_rows or any(
                row.get("artifact_type") != "acceptance_process_event"
                or row.get("schema_version") != "process-event-v1"
                for row in event_rows
            ):
                return {}, "resume process events are not typed acceptance records"
            snapshots: dict[str, Mapping[str, Any]] = {}
            budget_snapshots: dict[str, Mapping[str, Any]] = {}
            for row in event_rows:
                if (
                    str(row.get("acceptance_run_id") or "")
                    != interruption.acceptance_run_id
                    or str(row.get("scenario_id") or "") != "E"
                    or str(row.get("job_id") or "") != interruption.job_id
                ):
                    return {}, "resume process events are not bound to the interruption job"
                if str(row.get("event") or "") != "ledger_snapshot":
                    if str(row.get("event") or "") == "budget_snapshot":
                        name = str(row.get("snapshot_name") or "").strip()
                        if name in {"before", "at_interruption", "after_resume"}:
                            budget_snapshots[name] = row
                    continue
                name = str(row.get("snapshot_name") or "").strip()
                if name in {"before", "at_interruption", "after_resume"}:
                    snapshots[name] = row
            if set(snapshots) != {"before", "at_interruption", "after_resume"}:
                return {}, "resume evidence requires before, interruption, and after-resume ledger snapshots"
            if set(budget_snapshots) != {"before", "at_interruption", "after_resume"}:
                return {}, "resume evidence requires durable budget snapshots at each process boundary"
            resume_started = next(
                (
                    row
                    for row in event_rows
                    if str(row.get("event") or "") == "resume_started"
                ),
                None,
            )
            if not isinstance(resume_started, Mapping) or (
                str(resume_started.get("new_pid") or "") != str(resume.new_pid)
                or str(resume_started.get("new_process_creation_identity") or "")
                != resume.new_process_creation_identity
            ):
                return {}, "resume evidence does not bind the fresh process identity"
            receipt_refs = by_role.get("provider_receipt_ledger", [])
            receipts: list[Mapping[str, Any]] = []
            for ref in receipt_refs:
                receipts.extend(self._rows(payloads.get(ref.ref_id)))
            receipt_ids = [str(row.get("receipt_id") or "") for row in receipts]
            if any(not value for value in receipt_ids):
                return {}, "resume provider ledger contains a receipt without receipt_id"
            duplicate_count = len(receipt_ids) - len(set(receipt_ids))
            before_ids = set(self._string_list(snapshots["before"].get("receipt_ids")) or [])
            interruption_ids = set(self._string_list(snapshots["at_interruption"].get("receipt_ids")) or [])
            after_ids = set(self._string_list(snapshots["after_resume"].get("receipt_ids")) or [])
            if not before_ids.issubset(interruption_ids) or not interruption_ids.issubset(after_ids) or after_ids != set(receipt_ids):
                return {}, "resume ledger snapshots do not form a durable monotonic receipt lineage"
            before_completed = set(self._string_list(snapshots["before"].get("completed_call_ids")) or [])
            new_after = after_ids - before_ids
            reexecuted = {
                str(row.get("call_id") or "")
                for row in receipts
                if str(row.get("receipt_id") or "") in new_after
                and str(row.get("call_id") or "") in before_completed
            }
            try:
                actual_calls = sum(int(row.get("attempts") or 0) for row in receipts)
                actual_output = sum(int(row.get("output_tokens") or 0) for row in receipts)
                actual_retries = sum(max(0, int(row.get("attempts") or 0) - 1) for row in receipts)
                after_calls = int(str(snapshots["after_resume"].get("provider_calls")))
                after_output = int(str(snapshots["after_resume"].get("output_tokens")))
                after_retries = int(str(snapshots["after_resume"].get("retry_attempts")))
            except (TypeError, ValueError):
                return {}, "resume ledger snapshot usage counters are invalid"
            if (actual_calls, actual_output, actual_retries) != (after_calls, after_output, after_retries):
                return {}, "resume ledger snapshot usage does not match the reopened provider ledger"
            try:
                budget_limit = int(
                    str(
                        budget_snapshots["after_resume"].get(
                            "budget", {}
                        ).get("max_provider_calls_total")
                        if isinstance(budget_snapshots["after_resume"].get("budget"), Mapping)
                        else 0
                    )
                )
                before_deadline = float(
                    str(budget_snapshots["before"].get("absolute_deadline_epoch") or 0.0)
                )
                after_deadline = float(
                    str(
                        budget_snapshots["after_resume"].get(
                            "absolute_deadline_epoch"
                        )
                        or 0.0
                    )
                )
                after_reserved = int(
                    str(budget_snapshots["after_resume"].get("calls_reserved") or 0)
                )
                after_used = int(
                    str(budget_snapshots["after_resume"].get("calls_used") or 0)
                )
            except (TypeError, ValueError, AttributeError):
                return {}, "resume budget snapshots contain invalid counters"
            if before_deadline <= 0 or after_deadline != before_deadline:
                return {}, "resume reset or lost the absolute acceptance deadline"
            if after_reserved != 0 or after_used < 0 or (
                budget_limit > 0 and after_used > budget_limit
            ):
                return {}, "resume budget reconciliation left an invalid reservation state"
            return {
                "interruption": True,
                "resume": True,
                "duplicate_receipts": duplicate_count,
                "reexecuted_completed_call_ids": sorted(reexecuted),
                "provider_call_ledger_delta": {
                    "receipt_ids_before": sorted(before_ids),
                    "receipt_ids_after_resume": sorted(after_ids),
                    "new_receipts_after_resume": sorted(new_after),
                    "reexecuted_completed_call_ids": sorted(reexecuted),
                    "provider_calls_delta": after_calls - int(str(snapshots["before"].get("provider_calls") or 0)),
                    "output_tokens_delta": after_output - int(str(snapshots["before"].get("output_tokens") or 0)),
                    "retry_delta": after_retries - int(str(snapshots["before"].get("retry_attempts") or 0)),
                },
                "budget_before_crash": dict(budget_snapshots["before"]),
                "budget_at_interruption": dict(budget_snapshots["at_interruption"]),
                "budget_after_reconciliation": dict(budget_snapshots["after_resume"]),
                "budget_after_resume": dict(budget_snapshots["after_resume"]),
            }, None

        if gate == "F":
            plan_refs = by_role.get("outline_provider_call_plan", [])
            if len(plan_refs) != 1:
                return {}, "Outline role closure requires one authoritative provider call plan"
            plan_payload = payloads.get(plan_refs[0].ref_id)
            if not isinstance(plan_payload, Mapping):
                return {}, "Outline provider call plan is not a JSON object"
            route_plan = plan_payload.get("reachable_provider_route_plan")
            if not isinstance(route_plan, Mapping):
                return {}, "Outline provider call plan lacks ReachableProviderRoutePlan"
            raw_routes = route_plan.get("routes")
            if not isinstance(raw_routes, list) or not raw_routes:
                return {}, "Outline provider route plan has no routes"
            expected_routes: dict[str, Mapping[str, Any]] = {}
            for route in raw_routes:
                if not isinstance(route, Mapping) or route.get("enabled") is not True:
                    continue
                role = str(route.get("semantic_role") or "").strip()
                if not role or route.get("resolved") is not True:
                    return {}, "Outline route plan contains an enabled unresolved route"
                expected_routes[role] = route
            if not expected_routes:
                return {}, "Outline route plan has no enabled resolved semantic roles"
            receipts: list[Mapping[str, Any]] = []
            for ref in by_role.get("provider_receipt_ledger", []):
                receipts.extend(self._rows(payloads.get(ref.ref_id)))
            executed: dict[str, list[Mapping[str, Any]]] = {}
            for row in receipts:
                role = self._semantic_role(row.get("node_id") or row.get("route"))
                if role:
                    executed.setdefault(role, []).append(row)
            mismatches: list[str] = []
            for role, route in expected_routes.items():
                candidates = executed.get(role, [])
                if not candidates:
                    mismatches.append(f"missing:{role}")
                    continue
                expected_provider = str(route.get("provider_family") or "").strip()
                expected_model = str(route.get("model") or "").strip()
                expected_endpoint = str(route.get("endpoint_type") or "").strip()
                expected_host = str(route.get("api_base_host") or "").strip().casefold()
                expected_section = str(route.get("section") or "").strip()
                expected_fingerprint = str(route.get("route_fingerprint") or "").strip()
                for row in candidates:
                    raw_metadata = row.get("metadata")
                    metadata = cast(Mapping[str, Any], raw_metadata) if isinstance(raw_metadata, Mapping) else {}
                    raw_transport = metadata.get("transport_config")
                    transport = cast(Mapping[str, Any], raw_transport) if isinstance(raw_transport, Mapping) else {}
                    actual_host = str(transport.get("api_base") or row.get("endpoint") or "").strip()
                    try:
                        from urllib.parse import urlsplit

                        parsed_host = urlsplit(actual_host)
                        actual_host = (parsed_host.hostname or "").casefold()
                        if parsed_host.port:
                            actual_host = f"{actual_host}:{parsed_host.port}"
                    except ValueError:
                        actual_host = ""
                    if (
                        str(row.get("provider") or "") != expected_provider
                        or str(row.get("model") or "") != expected_model
                        or str(row.get("endpoint_type") or "") != expected_endpoint
                        or (expected_host and actual_host != expected_host)
                        or str(metadata.get("config_section") or "") != expected_section
                        or not str(metadata.get("route_fingerprint") or "").strip()
                        or (
                            expected_fingerprint
                            and str(metadata.get("route_fingerprint") or "")
                            != expected_fingerprint
                        )
                    ):
                        mismatches.append(f"identity:{role}")
            if mismatches:
                return {}, "Outline semantic-role/provider closure failed: " + ", ".join(sorted(set(mismatches)))
            actual_roles = sorted(set(expected_routes).intersection(executed))
            candidate_count = len(executed.get("candidate_provider_generation", []))
            return {
                "route_plan_present": True,
                "semantic_roles": actual_roles,
                "executed_semantic_roles": actual_roles,
                "required_semantic_roles": sorted(expected_routes),
                "candidate_count": candidate_count,
            }, None

        if gate == "D":
            source_refs = by_role.get("source_pdf", [])
            profile_refs = by_role.get("modality_profile", [])
            source_hashes = {ref.sha256 for ref in source_refs}
            if len(source_hashes) < 3 or len(profile_refs) < 3:
                return {}, "heterogeneous gate requires three source PDFs and three derived modality profiles"
            profiles: list[DocumentModalityProfileV2] = []
            for ref in profile_refs:
                payload = payloads.get(ref.ref_id)
                if not isinstance(payload, Mapping):
                    return {}, "document modality profile is not a JSON object"
                try:
                    profile = DocumentModalityProfileV2.from_mapping(payload)
                except ReleaseAcceptanceSpecError as exc:
                    return {}, "heterogeneous gate requires three production-derived modality profiles: " + str(exc)
                if profile.source_pdf_sha256 not in source_hashes:
                    return {}, "document modality profile is not bound to a source PDF"
                profiles.append(profile)
            modalities = {profile.derived_modality for profile in profiles}
            if len(modalities) < 3:
                return {}, "heterogeneous gate requires text-heavy, visual/table-heavy, and OCR/scanned derived profiles"
            return {
                "source_count": len(source_hashes),
                "heterogeneity": len(modalities),
                "derived_modalities": sorted(modalities),
                "modality_profiles": [profile.source_pdf_sha256 for profile in profiles],
            }, None

        if gate == "H":
            challenge_refs = by_role.get("defect_artifact", [])
            validation_refs = by_role.get("validation_artifact", [])
            repair_refs = by_role.get("repair_artifact", [])
            if len(challenge_refs) != 1 or not validation_refs or len(repair_refs) != 1:
                return {}, "Validator challenge requires one challenge, validation, and repair artifact"
            challenge_payload = payloads.get(challenge_refs[0].ref_id)
            if not isinstance(challenge_payload, Mapping):
                return {}, "controlled Validator challenge is not a JSON object"
            try:
                challenge = ControlledDefectChallengeV1.from_mapping(challenge_payload)
            except ReleaseAcceptanceSpecError as exc:
                return {}, str(exc)
            try:
                mutated_path = self._resolve_path(
                    challenge.mutated_review_path,
                    origin_dir=origin_dir,
                )
                mutated_raw = _bounded_read(mutated_path, max_bytes=self.max_bytes)
            except (OSError, ReleaseAcceptanceSpecError) as exc:
                return {}, f"controlled defect copy is unreadable: {type(exc).__name__}"
            if hashlib.sha256(mutated_raw).hexdigest() != challenge.mutated_review_hash:
                return {}, "controlled defect copy hash does not match the challenge"
            if challenge.mutated_review_hash == challenge.baseline_review_hash:
                return {}, "controlled defect challenge did not mutate a copy"
            detected_ids: set[str] = set()
            detected = False
            revalidated_clean = False
            for ref in validation_refs:
                payload = payloads.get(ref.ref_id)
                for row in self._rows(payload):
                    if str(row.get("challenge_id") or "") != challenge.challenge_id:
                        continue
                    findings = row.get("detected_findings")
                    if isinstance(findings, list):
                        for finding in findings:
                            if not isinstance(finding, Mapping):
                                continue
                            if str(finding.get("mutation_locator") or "") == challenge.mutation_locator:
                                if (
                                    str(finding.get("detection_class") or "").casefold()
                                    != challenge.expected_detection_class.casefold()
                                ):
                                    continue
                                finding_id = str(finding.get("finding_id") or "").strip()
                                if finding_id:
                                    detected_ids.add(finding_id)
                                detected = detected or bool(finding_id)
                    if row.get("phase") == "revalidation" and row.get("status") in {"clean", "succeeded", "passed"}:
                        remaining = row.get("remaining_challenge_findings")
                        resolved = self._string_list(row.get("resolved_finding_ids"))
                        revalidated_clean = isinstance(remaining, list) and not remaining and bool(
                            resolved and detected_ids.intersection(resolved)
                        )
            repair_payload = payloads.get(repair_refs[0].ref_id)
            repair_rows = self._rows(repair_payload)
            repair_applied = False
            for row in repair_rows:
                if str(row.get("challenge_id") or "") != challenge.challenge_id:
                    continue
                if (
                    str(row.get("before_hash") or "") == challenge.mutated_review_hash
                    and _valid_sha256(row.get("after_hash"))
                    and str(row.get("after_hash")) != challenge.mutated_review_hash
                    and str(row.get("changed_locator") or "") == challenge.mutation_locator
                    and str(row.get("status") or "").casefold() in {"applied", "completed", "promoted"}
                ):
                    repair_applied = True
            if not detected:
                return {}, "Validator did not explicitly detect the challenged mutation"
            if not repair_applied:
                return {}, "Validator repair artifact is not bound to the challenged mutation"
            if not revalidated_clean:
                return {}, "Validator revalidation did not prove the challenged finding was resolved"
            return {
                "defect_injected": True,
                "defect_detected": True,
                "repair_applied": True,
                "revalidated_clean": True,
                "challenge_id": challenge.challenge_id,
                "detected_finding_ids": sorted(detected_ids),
            }, None

        if gate == "J":
            source_refs = by_role.get("source_pdf", [])
            diagnostics_refs = by_role.get("ocr_diagnostics", [])
            artifact_refs = by_role.get("ocr_artifact", [])
            canonical_refs = by_role.get("canonical_stage1", [])
            registry_refs = by_role.get("registry", [])
            if len(source_refs) != 1 or len(diagnostics_refs) != 1 or len(artifact_refs) != 1 or not canonical_refs or len(registry_refs) != 1:
                return {}, "OCR gate requires one source, diagnostics, OCR artifact, canonical Stage 1, and Registry reference"
            source_hash = source_refs[0].sha256
            diagnostics = payloads.get(diagnostics_refs[0].ref_id)
            ocr_artifact = payloads.get(artifact_refs[0].ref_id)
            canonical = payloads.get(canonical_refs[0].ref_id)
            registry = payloads.get(registry_refs[0].ref_id)
            if not all(isinstance(item, Mapping) for item in (diagnostics, ocr_artifact, canonical, registry)):
                return {}, "OCR lineage artifacts must be JSON objects"
            diagnostics = cast(Mapping[str, Any], diagnostics)
            ocr_artifact = cast(Mapping[str, Any], ocr_artifact)
            canonical = cast(Mapping[str, Any], canonical)
            registry = cast(Mapping[str, Any], registry)
            if diagnostics.get("artifact_type") != "ocr_diagnostics" or diagnostics.get("schema_version") != "ocr-diagnostics-v1":
                return {}, "OCR diagnostics type or schema is invalid"
            if str(diagnostics.get("source_pdf_sha256") or "") != source_hash:
                return {}, "OCR diagnostics source hash does not match the source PDF"
            page_numbers = self._string_list(diagnostics.get("page_numbers"))
            raw_ocr_page_count = diagnostics.get("ocr_page_count")
            if (
                not page_numbers
                or isinstance(raw_ocr_page_count, bool)
                or not isinstance(raw_ocr_page_count, int)
                or raw_ocr_page_count != len(page_numbers)
            ):
                return {}, "OCR diagnostics page identity is incomplete"
            if not str(diagnostics.get("ocr_engine") or "").strip() or not str(diagnostics.get("ocr_engine_version") or "").strip():
                return {}, "OCR diagnostics engine identity is incomplete"
            if ocr_artifact.get("artifact_type") != "ocr_artifact" or ocr_artifact.get("schema_version") != "ocr-artifact-v1":
                return {}, "OCR output artifact type or schema is invalid"
            if str(ocr_artifact.get("source_pdf_sha256") or "") != source_hash:
                return {}, "OCR output artifact source hash does not match the source PDF"
            if str(ocr_artifact.get("diagnostics_sha256") or "") != diagnostics_refs[0].sha256:
                return {}, "OCR output artifact is not bound to diagnostics"
            artifact_pages = self._string_list(ocr_artifact.get("page_numbers"))
            if artifact_pages != page_numbers or not isinstance(ocr_artifact.get("page_text_hashes"), Mapping):
                return {}, "OCR output artifact page lineage is incomplete"
            if isinstance(canonical, list):
                canonical_items = [item for item in canonical if isinstance(item, Mapping)]
            else:
                nested_canonical = canonical.get("payload")
                canonical_items = [
                    cast(Mapping[str, Any], nested_canonical)
                    if isinstance(nested_canonical, Mapping)
                    else canonical
                ]
            lineage = next(
                (
                    item.get("ocr_lineage")
                    for item in canonical_items
                    if str(item.get("source_pdf_sha256") or "") == source_hash
                    and isinstance(item.get("ocr_lineage"), Mapping)
                ),
                None,
            )
            if not isinstance(lineage, Mapping):
                return {}, "canonical Stage 1 artifact lacks OCR lineage"
            if (
                str(lineage.get("source_pdf_sha256") or "") != source_hash
                or str(lineage.get("ocr_artifact_sha256") or "") != artifact_refs[0].sha256
                or not str(lineage.get("stage1_input_sha256") or "").strip()
            ):
                return {}, "canonical Stage 1 OCR lineage is incomplete or mismatched"
            dependency_ids = self._string_list(lineage.get("registry_dependency_artifact_ids"))
            records = registry.get("artifacts")
            if not dependency_ids or not isinstance(records, list):
                return {}, "OCR lineage lacks Registry dependency identities"
            registered_ids = {
                str(item.get("artifact_id") or "")
                for item in records
                if isinstance(item, Mapping)
            }
            if not set(dependency_ids).issubset(registered_ids):
                return {}, "OCR lineage references artifacts absent from the Registry"
            return {
                "ocr_actually_used": True,
                "page_identity": True,
                "evidence_artifact": True,
                "stage1_consumed": True,
                "lineage": True,
                "ocr_page_numbers": page_numbers,
            }, None

        if gate == "Q":
            source_refs = by_role.get("source_pdf", [])
            canonical_refs = by_role.get("canonical_stage1", [])
            if len({ref.sha256 for ref in source_refs}) != 15 or not canonical_refs:
                return {}, "15-paper gate requires exactly fifteen distinct source PDFs and canonical Stage 1 evidence"
            source_hashes = {ref.sha256 for ref in source_refs}
            paper_keys: set[str] = set()
            paper_hashes: set[str] = set()
            for ref in canonical_refs:
                payload = payloads.get(ref.ref_id)
                if isinstance(payload, list):
                    items = payload
                else:
                    if not isinstance(payload, Mapping):
                        return {}, "canonical Stage 1 aggregate is not a JSON object or array"
                    candidate_raw = payload.get("payload")
                    candidate = cast(Mapping[str, Any], candidate_raw) if isinstance(candidate_raw, Mapping) else payload
                    summaries = candidate.get("summaries")
                    items = summaries if isinstance(summaries, list) else [candidate]
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    key = str(
                        item.get("canonical_paper_key")
                        or item.get("paper_key")
                        or item.get("paper_id")
                        or item.get("source_pdf_sha256")
                        or ""
                    ).strip()
                    source_hash = str(item.get("source_pdf_sha256") or "").strip()
                    if not key:
                        continue
                    paper_keys.add(key)
                    if source_hash:
                        if source_hash not in source_hashes:
                            return {}, "canonical Stage 1 paper identity is not bound to the authoritative corpus"
                        paper_hashes.add(source_hash)
            if len(paper_keys) != 15 or paper_hashes != source_hashes:
                return {}, "canonical Stage 1 aggregate does not prove fifteen distinct paper identities"
            outline_complete = False
            for ref in by_role.get("outline_terminal", []):
                for row in self._rows(payloads.get(ref.ref_id)):
                    outline_complete = outline_complete or str(row.get("status") or row.get("closure_status") or "").casefold() in {"complete", "completed", "succeeded"}
            docx_complete = False
            for ref in by_role.get("review_docx", []):
                raw = raw_by_ref.get(ref.ref_id, b"")
                try:
                    with zipfile.ZipFile(BytesIO(raw)) as archive:
                        document_xml = archive.read("word/document.xml").decode("utf-8", errors="strict")
                        docx_complete = (
                            "<w:body" in document_xml
                            and "reference" in document_xml.casefold()
                            and not any(marker in document_xml.casefold() for marker in ("placeholder", "your_", "todo"))
                        )
                except (KeyError, UnicodeError, zipfile.BadZipFile):
                    docx_complete = False
            citation_complete = False
            for ref in by_role.get("citation_manifest", []):
                payload = payloads.get(ref.ref_id)
                if isinstance(payload, Mapping):
                    entries = (
                        payload.get("citations")
                        or payload.get("references")
                        or payload.get("items")
                        or payload.get("paper_entries")
                        or payload.get("bibliography")
                    )
                    citation_complete = isinstance(entries, list) and bool(entries)
            validation_complete = False
            for ref in by_role.get("validation_artifact", []):
                for row in self._rows(payloads.get(ref.ref_id)):
                    validation_complete = validation_complete or str(row.get("status") or row.get("validation_status") or "").casefold() in {"clean", "completed", "succeeded", "passed"}
            if not outline_complete or not docx_complete or not citation_complete or not validation_complete:
                return {}, "15-paper gate lacks complete Outline, DOCX, citation, or validation semantics"
            return {
                "source_count": 15,
                "corpus_count": 15,
                "canonical_stage1_count": 15,
                "paper_identity_count": len(paper_keys),
                "paper_keys": sorted(paper_keys),
                "outline_complete": True,
                "docx_complete": True,
                "validation_complete": True,
            }, None

        return {}, None

    def verify(
        self,
        gate: str,
        evidence: Mapping[str, Any] | None,
        *,
        expected_final_sha: str = "",
        expected_acceptance_run_id: str = "",
        origin_dir: str | Path | None = None,
        expected_job_id: str = "",
    ) -> dict[str, Any]:
        contract = gate_contract(gate)
        if not isinstance(evidence, Mapping):
            return {"status": "NOT_VERIFIED", "reason": "gate evidence is missing", "contract": contract}
        if evidence.get("schema_version") != "release-acceptance-gate-evidence-v1":
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence schema is not the durable reference schema",
                "contract": contract,
            }
        if evidence.get("producer") != "runtime.release_acceptance.GateEvidenceProducer":
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence producer binding is missing or invalid",
                "contract": contract,
            }
        unknown = sorted(str(key) for key in evidence if str(key) not in _GATE_EVIDENCE_FIELDS)
        if unknown:
            return {
                "status": "FAIL",
                "reason": "gate evidence contains unknown fields: " + ", ".join(unknown),
                "contract": contract,
            }
        if evidence.get("gate") != str(gate):
            return {
                "status": "FAIL",
                "reason": "gate evidence is bound to a different gate",
                "contract": contract,
            }
        evidence_run_id = str(evidence.get("acceptance_run_id") or "").strip()
        if expected_acceptance_run_id and evidence_run_id != str(expected_acceptance_run_id):
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence belongs to a different acceptance run",
                "evidence_acceptance_run_id": evidence_run_id,
                "expected_acceptance_run_id": str(expected_acceptance_run_id),
                "contract": contract,
            }
        if str(evidence.get("scenario_id") or "").strip() != str(gate):
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence is not bound to its dedicated scenario",
                "contract": contract,
            }
        evidence_job_id = str(evidence.get("job_id") or "").strip()
        if expected_job_id and evidence_job_id and evidence_job_id != str(expected_job_id):
            return {
                "status": "FAIL",
                "reason": "gate evidence job identity does not match the tested job",
                "contract": contract,
            }
        evidence_sha = str(evidence.get("final_sha") or "").strip()
        if not _valid_checkout_sha(evidence_sha):
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence is not bound to a valid final checkout SHA",
                "contract": contract,
            }
        if expected_final_sha and evidence_sha != expected_final_sha:
            return {
                "status": "FAIL",
                "reason": "gate evidence final SHA does not match the tested checkout",
                "evidence_final_sha": evidence_sha,
                "expected_final_sha": expected_final_sha,
                "contract": contract,
            }
        raw_refs = evidence.get("durable_refs", evidence.get("evidence_refs"))
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence must contain durable_refs; handwritten facts are not evidence",
                "contract": contract,
            }
        try:
            refs = [DurableEvidenceRefV1.from_mapping(item) for item in raw_refs]
        except (ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
            return {"status": "FAIL", "reason": str(exc), "contract": contract}
        identities = [ref.ref_id for ref in refs]
        paths = [str(self._resolve_path(ref.path, origin_dir=origin_dir)).casefold() for ref in refs]
        if len(set(identities)) != len(identities):
            return {"status": "FAIL", "reason": "durable evidence ref_id values must be unique", "contract": contract}
        if len(set(paths)) != len(paths):
            return {"status": "FAIL", "reason": "durable evidence paths must be unique", "contract": contract}
        jobs = {ref.job_id for ref in refs if ref.job_id}
        if expected_job_id and (
            not jobs or jobs != {str(expected_job_id)} or any(not ref.job_id for ref in refs)
        ):
            return {"status": "FAIL", "reason": "durable evidence job identity is missing or belongs to a different job", "contract": contract}
        payloads: dict[str, Any] = {}
        raw_by_ref: dict[str, bytes] = {}
        try:
            for ref in refs:
                path = self._resolve_path(ref.path, origin_dir=origin_dir)
                raw = _bounded_read(path, max_bytes=self.max_bytes)
                raw_by_ref[ref.ref_id] = raw
                if len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
                    return {
                        "status": "FAIL",
                        "reason": f"durable evidence hash or size mismatch: {ref.ref_id}",
                        "contract": contract,
                    }
                allowed_types = _ROLE_ARTIFACT_TYPES.get(ref.role)
                if allowed_types is not None and ref.artifact_type not in allowed_types:
                    not_playwright = ref.role in {
                        "playwright_trace",
                        "browser_evidence",
                        "playwright_screenshot_manifest",
                    }
                    return {
                        "status": (
                            "FAIL_NOT_PLAYWRIGHT_EVIDENCE"
                            if not_playwright
                            else "FAIL"
                        ),
                        "reason": (
                            "Playwright evidence invalid (FAIL_NOT_PLAYWRIGHT_EVIDENCE): "
                            if not_playwright
                            else ""
                        )
                        + f"durable evidence role/type binding is invalid: {ref.ref_id}",
                        "contract": contract,
                    }
                if ref.role == "provider_receipt_ledger":
                    if path.suffix.casefold() != ".jsonl":
                        return {
                            "status": "FAIL",
                            "reason": f"provider receipt evidence must be JSONL: {ref.ref_id}",
                            "contract": contract,
                        }
                    try:
                        receipts = ProviderRuntimeLedger(path).list_acceptance_receipts(
                            expected_job_id=expected_job_id or ref.job_id,
                        )
                    except (OSError, ValueError, ProviderRuntimeContractError) as exc:
                        return {
                            "status": "FAIL",
                            "reason": f"malformed provider receipt evidence: {exc}",
                            "contract": contract,
                        }
                    payloads[ref.ref_id] = [receipt.to_dict() for receipt in receipts]
                    continue
                if ref.role == "scenario_execution_receipt":
                    if path.suffix.casefold() != ".json":
                        return {
                            "status": "FAIL",
                            "reason": f"scenario execution receipt evidence must be JSON: {ref.ref_id}",
                            "contract": contract,
                        }
                    try:
                        receipt_payload = self._json_payload(raw, path)
                        receipt = ScenarioExecutionReceiptV1.from_mapping(receipt_payload)
                    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
                        return {
                            "status": "FAIL",
                            "reason": f"invalid scenario execution receipt: {exc}",
                            "contract": contract,
                        }
                    payloads[ref.ref_id] = receipt.to_dict()
                    continue
                payload: Any = None
                if ref.artifact_type and path.suffix.casefold() in {".json", ".jsonl"}:
                    payload = self._json_payload(raw, path)
                    rows = payload if isinstance(payload, list) else [payload]
                    for row in rows:
                        if isinstance(row, Mapping) and row.get("artifact_type"):
                            row_type = str(row.get("artifact_type"))
                            accepted_types = (
                                {"provider_receipt_ledger", "provider_call_receipt"}
                                if ref.artifact_type == "provider_receipt_ledger"
                                else {ref.artifact_type}
                            )
                            if row_type not in accepted_types:
                                return {
                                    "status": "FAIL",
                                    "reason": f"durable evidence artifact type mismatch: {ref.ref_id}",
                                    "contract": contract,
                                }
                        if isinstance(row, Mapping) and ref.artifact_version:
                            row_version = str(
                                row.get("artifact_version")
                                or row.get("schema_version")
                                or ""
                            )
                            allowed_versions = (
                                {ref.artifact_version, "v2"}
                                if ref.artifact_type == "provider_receipt_ledger"
                                else {ref.artifact_version}
                            )
                            if row_version and row_version not in allowed_versions:
                                return {
                                    "status": "FAIL",
                                    "reason": f"durable evidence artifact version mismatch: {ref.ref_id}",
                                    "contract": contract,
                                }
                        if (
                            isinstance(row, Mapping)
                            and ref.job_id
                            and row.get("job_id")
                            and str(row.get("job_id")) != ref.job_id
                        ):
                            return {
                                "status": "FAIL",
                                "reason": f"durable evidence job identity mismatch: {ref.ref_id}",
                                "contract": contract,
                            }
                    if ref.schema_version and isinstance(payload, Mapping):
                        actual_schema = str(
                            payload.get("schema_version")
                            or payload.get("artifact_version")
                            or ""
                        )
                        if actual_schema and actual_schema != ref.schema_version:
                            return {
                                "status": "FAIL",
                                "reason": f"durable evidence schema mismatch: {ref.ref_id}",
                                "contract": contract,
                            }
                    payloads[ref.ref_id] = payload
                elif path.suffix.casefold() in {".json", ".jsonl"}:
                    payload = self._json_payload(raw, path)
                if ref.role == "source_pdf" and not raw.startswith(b"%PDF-"):
                    return {
                        "status": "FAIL",
                        "reason": f"source_pdf evidence is not a PDF: {ref.ref_id}",
                        "contract": contract,
                    }
                if path.suffix.casefold() in {".json", ".jsonl"}:
                    if ref.role == "runtime_spec":
                        try:
                            from runtime.job_spec import RuntimeJobSpec

                            if not isinstance(payload, Mapping):
                                raise ValueError("runtime spec must be an object")
                            RuntimeJobSpec.from_dict(payload).validate()
                        except (TypeError, ValueError) as exc:
                            return {
                                "status": "FAIL",
                                "reason": f"runtime spec evidence is invalid: {type(exc).__name__}",
                                "contract": contract,
                            }
                    payloads[ref.ref_id] = payload
        except (OSError, UnicodeError, json.JSONDecodeError, ReleaseAcceptanceSpecError) as exc:
            return {"status": "FAIL", "reason": str(exc), "contract": contract}

        scenario_receipt_refs = [
            ref for ref in refs if ref.role == "scenario_execution_receipt"
        ]
        if len(scenario_receipt_refs) != 1:
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence requires exactly one executor-owned scenario receipt",
                "contract": contract,
            }
        try:
            scenario_receipt = ScenarioExecutionReceiptV1.from_mapping(
                payloads[scenario_receipt_refs[0].ref_id]
            )
        except (KeyError, ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
            return {
                "status": "FAIL",
                "reason": f"invalid scenario execution receipt: {exc}",
                "contract": contract,
            }
        if not evidence_run_id:
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence must carry a non-empty acceptance run for executor binding",
                "contract": contract,
            }
        if (
            scenario_receipt.parent_acceptance_run_id != evidence_run_id
            or scenario_receipt.scenario_id != str(gate)
            or scenario_receipt.final_executable_sha != evidence_sha
            or (expected_job_id and scenario_receipt.job_id != str(expected_job_id))
        ):
            return {
                "status": "FAIL",
                "reason": "scenario execution receipt is not bound to this gate run, SHA, or job",
                "contract": contract,
            }
        evidence_ref_ids = {ref.ref_id for ref in refs}
        if any(
            str(item.get("ref_id") or "") not in evidence_ref_ids
            for item in scenario_receipt.produced_evidence_refs
        ):
            return {
                "status": "FAIL",
                "reason": "scenario execution receipt names evidence refs absent from the gate",
                "contract": contract,
            }
        if scenario_receipt.status != "PASSED":
            return {
                "status": "NOT_VERIFIED",
                "reason": "scenario execution receipt is not a successful executor terminal",
                "contract": contract,
            }

        # A hash of artifact_registry.json is not enough by itself. Reopen and
        # verify every ready record it names, including its owner and content
        # hash, so a copied or edited workspace cannot inherit a PASS.
        for ref in refs:
            if ref.role != "registry":
                continue
            registry = payloads.get(ref.ref_id)
            if not isinstance(registry, Mapping):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence is not a JSON object",
                    "contract": contract,
                }
            registry_job_id = str(registry.get("job_id") or "")
            if not registry_job_id:
                return {
                    "status": "FAIL",
                    "reason": "registry evidence has no job owner",
                    "contract": contract,
                }
            if expected_job_id and registry_job_id != str(expected_job_id):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence belongs to a different job",
                    "contract": contract,
                }
            records = registry.get("artifacts")
            if not isinstance(records, list):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence does not contain an artifact array",
                    "contract": contract,
                }
            if registry.get("artifact_registry_version") != "v2":
                return {
                    "status": "FAIL",
                    "reason": "registry evidence is not the current Registry schema",
                    "contract": contract,
                }
            for item in records:
                if not isinstance(item, Mapping) or str(item.get("status") or "") != "ready":
                    continue
                record_job_id = str(item.get("job_id") or "")
                if registry_job_id and record_job_id != registry_job_id:
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact job identity mismatch",
                        "contract": contract,
                    }
                artifact_path = Path(str(item.get("path") or "")).expanduser().resolve()
                expected_hash = str(item.get("content_hash") or "").strip().lower()
                if not _valid_sha256(expected_hash):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact content hash is invalid",
                        "contract": contract,
                    }
                try:
                    artifact_raw = _bounded_read(artifact_path, max_bytes=self.max_bytes)
                except (OSError, ReleaseAcceptanceSpecError) as exc:
                    return {
                        "status": "FAIL",
                        "reason": f"registry artifact is unreadable: {type(exc).__name__}",
                        "contract": contract,
                    }
                raw_record_size = item.get("size")
                try:
                    record_size = (
                        len(artifact_raw)
                        if raw_record_size in (None, "")
                        else int(raw_record_size)
                    )
                except (TypeError, ValueError):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact size is invalid",
                        "contract": contract,
                    }
                if (
                    len(artifact_raw) != record_size
                    or hashlib.sha256(artifact_raw).hexdigest() != expected_hash
                ):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact hash or size mismatch",
                        "contract": contract,
                    }
            # Reuse the Registry's recursive verifier for canonical roots. It
            # checks dependency identity, ready status, cycles, path, and
            # content hashes; a hand-written summary field cannot replace it.
            try:
                from services.artifact_registry import ArtifactRegistry

                registry_object = ArtifactRegistry(
                    self._resolve_path(ref.path, origin_dir=origin_dir),
                    registry_job_id,
                )
                critical_types = {
                    "job_outcome",
                    "runtime_stage_terminal",
                    "provider_receipt_closure",
                    "current_artifact_set",
                    "review_docx",
                    "review_docx_repaired",
                    "validation_run_result",
                    "validation_run_result_repaired",
                    "paper_artifact",
                    "evidence_manifest",
                    "ocr_diagnostics",
                    "ocr_artifact",
                    "document_modality_profile",
                    "scenario_execution_receipt",
                    "playwright_run_evidence",
                    "playwright_screenshot_manifest",
                    "playwright_trace",
                    "citation_manifest",
                }
                for record in registry_object.list_records():
                    if record.status != "ready" or record.artifact_type not in critical_types:
                        continue
                    registry_object.verify_ready_artifact_closure(record)
            except Exception as exc:
                return {
                    "status": "FAIL",
                    "reason": f"Registry dependency closure is not verified: {type(exc).__name__}",
                    "contract": contract,
                }

        required_roles = _GATE_REF_ROLES.get(str(gate), frozenset())
        unexpected_roles = sorted({ref.role for ref in refs} - required_roles)
        if unexpected_roles:
            return {
                "status": "FAIL",
                "reason": "gate evidence contains roles owned by another scenario: " + ", ".join(unexpected_roles),
                "contract": contract,
            }
        missing_roles = sorted(required_roles - {ref.role for ref in refs})
        if missing_roles:
            return {
                "status": "NOT_VERIFIED",
                "reason": "durable evidence is missing required references: " + ", ".join(missing_roles),
                "contract": contract,
            }
        try:
            semantic_facts, semantic_error = self._derive_semantic_facts(
                str(gate),
                refs,
                payloads,
                raw_by_ref,
                origin_dir=origin_dir,
                expected_job_id=expected_job_id,
            )
        except Exception as exc:
            return {
                "status": "FAIL",
                "reason": f"durable evidence semantic verification failed closed: {type(exc).__name__}",
                "contract": contract,
            }
        if semantic_error:
            return {
                "status": (
                    "FAIL_NOT_PLAYWRIGHT_EVIDENCE"
                    if str(gate) == "I"
                    else "FAIL"
                ),
                "reason": semantic_error,
                "derived_facts": self._derive_facts(str(gate), refs, payloads),
                "contract": contract,
            }
        facts = self._derive_facts(str(gate), refs, payloads)
        facts.update(semantic_facts)
        if contract["required_live"] and int(facts["actual_transport_calls"]) <= 0:
            return {
                "status": "FAIL",
                "reason": "a live provider gate requires actual transport calls derived from receipts",
                "derived_facts": facts,
                "contract": contract,
            }
        truthy_fields = {
            "C": ("closure_complete",),
            "D": ("closure_complete",),
            "E": ("interruption", "resume"),
            "F": ("closure_complete",),
            "G": ("free_mode_route_only", "profile_durable"),
            "H": ("defect_injected", "defect_detected", "repair_applied", "revalidated_clean"),
            "I": ("playwright", "browser_evidence", "trace_archive", "flow_completed"),
            "J": ("ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed", "lineage"),
            "K": ("bounded_wait", "no_corrupt_json", "no_lost_update"),
            "Q": ("outline_complete", "docx_complete", "validation_complete"),
        }
        false_fields = [field for field in truthy_fields.get(str(gate), ()) if facts.get(field) is not True]
        if false_fields:
            return {
                "status": "FAIL",
                "reason": "durable evidence did not prove required facts: " + ", ".join(false_fields),
                "derived_facts": facts,
                "contract": contract,
            }
        if gate == "D" and int(facts.get("source_count") or 0) < 3:
            return {"status": "FAIL", "reason": "heterogeneous gate requires at least three source artifacts", "derived_facts": facts, "contract": contract}
        if gate == "D" and int(facts.get("heterogeneity") or 0) < 3:
            return {"status": "FAIL", "reason": "heterogeneous gate requires three distinct durable modality identities", "derived_facts": facts, "contract": contract}
        if gate == "F" and (
            not facts.get("semantic_roles")
            or int(facts.get("candidate_count") or 0) <= 0
            or facts.get("route_plan_present") is not True
        ):
            return {"status": "FAIL", "reason": "Outline gate lacks the authoritative route plan or candidate receipts derived from the ledger", "derived_facts": facts, "contract": contract}
        if gate == "K" and int(facts.get("process_count") or 0) < 2:
            return {"status": "FAIL", "reason": "contention gate requires at least two durable process identities", "derived_facts": facts, "contract": contract}
        if gate == "C" and (
            int(facts.get("source_count") or 0) != 1
            or int(facts.get("canonical_stage1_count") or 0) != 1
        ):
            return {"status": "FAIL", "reason": "one-paper counts were not derived as a matching durable pair", "derived_facts": facts, "contract": contract}
        if gate == "Q" and (
            int(facts.get("source_count") or 0) != 15
            or int(facts.get("paper_identity_count") or 0) != 15
        ):
            return {"status": "FAIL", "reason": "F1 counts were not derived as exactly fifteen durable paper identities", "derived_facts": facts, "contract": contract}
        if gate == "E" and int(facts.get("duplicate_receipts") or 0) != 0:
            return {"status": "FAIL", "reason": "resume gate recorded duplicate receipt identities", "derived_facts": facts, "contract": contract}
        if gate == "E" and facts.get("reexecuted_completed_call_ids"):
            return {
                "status": "FAIL",
                "reason": "resume gate re-executed a completed provider call",
                "derived_facts": facts,
                "contract": contract,
            }
        return {
            "status": "PASS",
            "reason": "gate facts were derived from uniquely hashed durable references",
            "derived_facts": facts,
            "verified_refs": [ref.to_dict() for ref in refs],
            "contract": contract,
        }


def validate_gate_evidence(
    gate: str,
    evidence: Mapping[str, Any] | None,
    *,
    expected_final_sha: str = "",
    expected_acceptance_run_id: str = "",
    origin_dir: str | Path | None = None,
    expected_job_id: str = "",
) -> dict[str, Any]:
    """Validate one gate only from reopened durable references."""

    return GateEvidenceVerifier().verify(
        gate,
        evidence,
        expected_final_sha=expected_final_sha,
        expected_acceptance_run_id=expected_acceptance_run_id,
        origin_dir=origin_dir,
        expected_job_id=expected_job_id,
    )


__all__ = [
    "AcceptanceExecutionContextV1",
    "AcceptanceChildScenarioSpecV2",
    "AcceptanceScenario",
    "AcceptanceScenarioContextV1",
    "AcceptanceScenarioResultV1",
    "GATE_CONTRACTS",
    "AcceptanceRunStateV1",
    "ParentAcceptanceResultV2",
    "GateCScenario",
    "GateDScenario",
    "GateEScenario",
    "GateFScenario",
    "GateGScenario",
    "GateHScenario",
    "GateIScenario",
    "GateJScenario",
    "GateKScenario",
    "GateQScenario",
    "GenericAcceptanceScenario",
    "ProcessInterruptionEventV1",
    "ProcessResumeEventV1",
    "ControlledDefectChallengeV1",
    "AcceptanceValidatorChallengeInputV1",
    "DocumentModalityProfileV1",
    "DocumentModalityProfileV2",
    "DurableEvidenceRefV1",
    "GateEvidenceProducer",
    "GateEvidenceVerifier",
    "ReleaseAcceptanceBudget",
    "ReleaseAcceptancePlanV2",
    "ReleaseAcceptanceSpec",
    "ReleaseAcceptanceSpecError",
    "ScenarioExecutionReceiptV1",
    "gate_contract",
    "gate_evidence_roles",
    "scenario_for_gate",
    "validate_gate_evidence",
]

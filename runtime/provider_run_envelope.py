"""Finite run-start budget envelope with just-in-time downstream admission.

The stage request plan is a preflight projection. It is evidence for the
run-start decision, while each request still reserves its exact materialized
identity and resource demand immediately before transport.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from runtime.provider_runtime import ProviderAggregateBudgetV2


class ProviderRunEnvelopeError(ValueError):
    """A run does not have a valid finite provider budget envelope."""


_REQUIRED_SCOPE_HASHES = (
    "runtime_spec_sha256",
    "effective_config_sha256",
    "source_snapshot_sha256",
    "review_goal_sha256",
    "route_plan_sha256",
    "stage_work_contract_sha256",
)
_BUDGET_AUTHORITIES = frozenset(
    {
        "bound_acceptance_context",
        "explicit_runtime_spec",
        "documented_default",
        "downstream_continuation",
    }
)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderRunEnvelopeError("provider run envelope is not canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _known_nonnegative_number(value: Any, *, label: str, integer: bool) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderRunEnvelopeError(f"{label} must be numeric, not boolean")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderRunEnvelopeError(f"{label} must be numeric") from exc
    if parsed < 0 or (not integer and not math.isfinite(float(parsed))):
        raise ProviderRunEnvelopeError(f"{label} must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class ProviderRunEnvelopeV1:
    """A strict finite run budget bound to source, goal, config and work plan."""

    job_id: str
    run_id: str
    budget_authority: str
    provider_budget: ProviderAggregateBudgetV2
    scope_hashes: Mapping[str, str]
    projection_identity_hash: str
    admission_status: str
    known_lower_bounds: Mapping[str, int | float | None]
    conditional_work_upper_bounds: Mapping[str, int | float | None]
    jit_admission_required: bool = True

    SCHEMA_VERSION = "provider-run-envelope/v1"

    def __post_init__(self) -> None:
        if not str(self.job_id or "").strip() or not str(self.run_id or "").strip():
            raise ProviderRunEnvelopeError("provider run envelope requires job_id and run_id")
        if self.budget_authority not in _BUDGET_AUTHORITIES:
            raise ProviderRunEnvelopeError("provider run budget authority is unsupported")
        if not isinstance(self.provider_budget, ProviderAggregateBudgetV2):
            raise ProviderRunEnvelopeError("provider run envelope requires strict budget V2")
        if not isinstance(self.scope_hashes, Mapping):
            raise ProviderRunEnvelopeError("provider run envelope scope hashes must be an object")
        if self.admission_status not in {"ready_with_jit", "blocked_budget"}:
            raise ProviderRunEnvelopeError("provider run envelope admission status is invalid")
        if self.jit_admission_required is not True:
            raise ProviderRunEnvelopeError("downstream just-in-time admission is required")
        for name in _REQUIRED_SCOPE_HASHES:
            value = str(self.scope_hashes.get(name) or "")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ProviderRunEnvelopeError(f"scope hash is missing or invalid: {name}")
        if re.fullmatch(r"[0-9a-f]{64}", self.projection_identity_hash) is None:
            raise ProviderRunEnvelopeError("projection_identity_hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "budget_authority": self.budget_authority,
            "provider_budget": self.provider_budget.to_dict(),
            "scope_hashes": dict(sorted(self.scope_hashes.items())),
            "projection_identity_hash": self.projection_identity_hash,
            "admission_status": self.admission_status,
            "known_lower_bounds": dict(self.known_lower_bounds),
            "conditional_work_upper_bounds": dict(self.conditional_work_upper_bounds),
            "jit_admission_required": self.jit_admission_required,
            "future_request_materialization": "at_ready_stage_before_first_post",
            "projection_is_provider_authority": False,
        }
        payload["envelope_sha256"] = _sha256(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProviderRunEnvelopeV1:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ProviderRunEnvelopeError("provider run envelope schema is invalid")
        raw_budget = payload.get("provider_budget")
        if not isinstance(raw_budget, Mapping):
            raise ProviderRunEnvelopeError("provider run envelope budget is missing")
        scope_hashes = payload.get("scope_hashes")
        lower = payload.get("known_lower_bounds")
        conditional = payload.get("conditional_work_upper_bounds")
        if not isinstance(scope_hashes, Mapping) or not isinstance(lower, Mapping) or not isinstance(conditional, Mapping):
            raise ProviderRunEnvelopeError("provider run envelope bindings are invalid")
        supplied_hash = str(payload.get("envelope_sha256") or "")
        body = dict(payload)
        body.pop("envelope_sha256", None)
        if supplied_hash != _sha256(body):
            raise ProviderRunEnvelopeError("provider run envelope hash does not match its payload")
        envelope = cls(
            job_id=str(payload.get("job_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            budget_authority=str(payload.get("budget_authority") or ""),
            provider_budget=ProviderAggregateBudgetV2.from_mapping(raw_budget),
            scope_hashes={str(key): str(value) for key, value in scope_hashes.items()},
            projection_identity_hash=str(payload.get("projection_identity_hash") or ""),
            admission_status=str(payload.get("admission_status") or ""),
            known_lower_bounds=dict(lower),
            conditional_work_upper_bounds=dict(conditional),
            jit_admission_required=payload.get("jit_admission_required") is True,
        )
        if envelope.to_dict() != dict(payload):
            raise ProviderRunEnvelopeError("provider run envelope fields are not canonical")
        return envelope


def build_provider_run_envelope_v1(
    *,
    job_id: str,
    run_id: str,
    budget_authority: str,
    provider_budget: ProviderAggregateBudgetV2,
    scope_hashes: Mapping[str, str],
    full_stage_request_plan: Mapping[str, Any],
) -> ProviderRunEnvelopeV1:
    """Bind a finite strict run budget while leaving future requests JIT.

    Exact request hashes and output bodies for downstream stages are not
    required at run start. The projection's known required lower bounds must
    fit; actual requests reserve their route, serializer size, output cap and
    retry demand through ``ProviderRuntime.admit`` immediately before POST.
    """

    if not isinstance(full_stage_request_plan, Mapping):
        raise ProviderRunEnvelopeError("full-stage request plan must be an object")
    if full_stage_request_plan.get("schema_version") != "full-stage-provider-request-plan-v1":
        raise ProviderRunEnvelopeError("full-stage request plan schema is unsupported")
    if full_stage_request_plan.get("aggregate_budget") != provider_budget.to_dict():
        raise ProviderRunEnvelopeError("projection budget differs from the approved run budget")
    projection_hash = str(full_stage_request_plan.get("projection_identity_hash") or "")
    totals = full_stage_request_plan.get("totals")
    statuses = full_stage_request_plan.get("budget_status")
    if not isinstance(totals, Mapping) or not isinstance(statuses, Mapping):
        raise ProviderRunEnvelopeError("full-stage projection lacks totals or budget status")

    lower = {
        "provider_calls": _known_nonnegative_number(
            totals.get("physical_attempts_known_lower_bound"),
            label="physical_attempts_known_lower_bound",
            integer=True,
        ),
        "output_tokens": _known_nonnegative_number(
            totals.get("aggregate_output_tokens_known_lower_bound"),
            label="aggregate_output_tokens_known_lower_bound",
            integer=True,
        ),
        "retry_attempts": _known_nonnegative_number(
            totals.get("retry_attempts_required_reserve"),
            label="retry_attempts_required_reserve",
            integer=True,
        ),
    }
    wall_upper = _known_nonnegative_number(
        totals.get("wall_seconds_upper_bound"),
        label="wall_seconds_upper_bound",
        integer=False,
    )
    limits_exceeded = (
        (lower["provider_calls"] is not None and lower["provider_calls"] > provider_budget.max_provider_calls_total)
        or (lower["output_tokens"] is not None and lower["output_tokens"] > provider_budget.max_output_tokens_total)
        or (lower["retry_attempts"] is not None and lower["retry_attempts"] > provider_budget.max_retry_attempts_total)
    )
    # The projection's wall total sums per-request upper bounds across the run.
    # The strict controller enforces the approved global wall deadline at each
    # actual admission; the sum is diagnostic and is not a required lower bound.
    # Likewise, conditional/future upper bounds are handled by JIT reservations.
    status = "blocked_budget" if limits_exceeded else "ready_with_jit"
    conditional = {
        "logical_calls_upper_bound": _known_nonnegative_number(
            totals.get("logical_calls_upper_bound"),
            label="logical_calls_upper_bound",
            integer=True,
        ),
        "physical_attempts_upper_bound": _known_nonnegative_number(
            totals.get("physical_attempts_upper_bound"),
            label="physical_attempts_upper_bound",
            integer=True,
        ),
        "output_tokens_all_attempts": _known_nonnegative_number(
            totals.get("estimated_output_tokens_all_attempts"),
            label="estimated_output_tokens_all_attempts",
            integer=True,
        ),
        "wall_seconds_upper_bound": wall_upper,
        "unknown_exposure_count": _known_nonnegative_number(
            totals.get("unknown_exposure_count", 0),
            label="unknown_exposure_count",
            integer=True,
        ),
    }
    return ProviderRunEnvelopeV1(
        job_id=str(job_id),
        run_id=str(run_id),
        budget_authority=str(budget_authority),
        provider_budget=provider_budget,
        scope_hashes={str(key): str(value) for key, value in scope_hashes.items()},
        projection_identity_hash=projection_hash,
        admission_status=status,
        known_lower_bounds=lower,
        conditional_work_upper_bounds=conditional,
    )


__all__ = [
    "ProviderRunEnvelopeError",
    "ProviderRunEnvelopeV1",
    "build_provider_run_envelope_v1",
]

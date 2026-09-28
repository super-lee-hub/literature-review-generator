"""One typed stage policy shared by the runner, lifecycle, and closure readers."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from runtime.provider_context import ProviderContextProfile, ProviderRequestEstimateV1


class StagePlanError(ValueError):
    """Raised when a requested stage policy cannot be executed safely."""


_DEFAULTS: dict[str, tuple[str, ...]] = {
    "analyze": ("analyze",),
    "derive_review_batch": ("derive_review_batch",),
    "retry_failed": ("analyze",),
    "generate_outline": ("outline",),
    "generate_review": ("outline", "review"),
    "generate_section": ("outline", "review"),
    "retry_review_failed": ("outline", "review"),
    "validate_review": ("validate",),
    "run_all": ("analyze", "outline", "review"),
}

_CURRENT_SET_ACTIONS = frozenset(
    {
        "derive_review_batch",
        "generate_outline",
        "generate_review",
        "generate_section",
        "retry_review_failed",
        "run_all",
        "validate_review",
    }
)


@dataclass(frozen=True)
class StagePlan:
    version: str
    action: str
    requested_stages: tuple[str, ...]
    required_stages: tuple[str, ...]
    validation_enabled: bool
    validation_required: bool
    require_clean_validation: bool
    allow_unvalidated_when_validation_optional: bool
    current_artifact_set_required: bool
    validation_status: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requested_stages"] = list(self.requested_stages)
        payload["required_stages"] = list(self.required_stages)
        return payload


def _normalize(raw: Iterable[Any] | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(
        dict.fromkeys(
            str(item).strip()
            for item in raw
            if str(item).strip() and str(item).strip() != "source_intake"
        )
    )


def build_stage_plan(
    *,
    action: str,
    requested_stages: Iterable[Any] | None,
    validation_enabled: bool,
    validation_required: bool | None = None,
    require_clean_validation: bool | None = None,
    allow_unvalidated_when_validation_optional: bool | None = None,
) -> StagePlan:
    normalized_action = str(action or "analyze")
    explicit = _normalize(requested_stages)
    default_stages = _DEFAULTS.get(normalized_action, ())
    if explicit is None:
        stages = tuple(default_stages)
        if normalized_action == "run_all" and validation_enabled:
            stages = (*stages, "validate")
    else:
        stages = explicit

    configured_required = validation_required
    if configured_required is None:
        configured_required = "validate" in stages or normalized_action == "validate_review"
    required = bool(configured_required)
    if normalized_action == "validate_review" and not validation_enabled and not required:
        stages = tuple(stage for stage in stages if stage != "validate")
    if "validate" in stages and not validation_enabled:
        if required:
            raise StagePlanError(
                "validation is required by the durable stage plan but Validation.review_enabled is false"
            )
        stages = tuple(stage for stage in stages if stage != "validate")

    # A required validation stage must remain in the plan; silently dropping it
    # would turn a provider-free run into a false completion.
    if required and "validate" not in stages:
        raise StagePlanError("validation_required=true but the stage plan has no validate stage")

    clean = required if require_clean_validation is None else bool(require_clean_validation)
    allow = (not required) if allow_unvalidated_when_validation_optional is None else bool(
        allow_unvalidated_when_validation_optional
    )
    required_stages = ("source_intake", *stages)
    return StagePlan(
        version="stage-plan-v1",
        action=normalized_action,
        requested_stages=tuple(stages),
        required_stages=tuple(dict.fromkeys(required_stages)),
        validation_enabled=bool(validation_enabled),
        validation_required=required,
        require_clean_validation=clean,
        allow_unvalidated_when_validation_optional=allow,
        current_artifact_set_required=(
            "validate" in stages or normalized_action in _CURRENT_SET_ACTIONS
        ),
        validation_status="required" if "validate" in stages else "not_requested",
    )


def stage_plan_from_metadata(metadata: Mapping[str, Any]) -> StagePlan | None:
    raw = metadata.get("stage_plan")
    if not isinstance(raw, Mapping):
        return None
    try:
        requested = tuple(str(item) for item in raw.get("requested_stages") or ())
        required = tuple(str(item) for item in raw.get("required_stages") or ())
        return StagePlan(
            version=str(raw.get("version") or "stage-plan-v1"),
            action=str(raw.get("action") or ""),
            requested_stages=requested,
            required_stages=required,
            validation_enabled=bool(raw.get("validation_enabled")),
            validation_required=bool(raw.get("validation_required")),
            require_clean_validation=bool(raw.get("require_clean_validation")),
            allow_unvalidated_when_validation_optional=bool(
                raw.get("allow_unvalidated_when_validation_optional")
            ),
            current_artifact_set_required=bool(raw.get("current_artifact_set_required")),
            validation_status=str(raw.get("validation_status") or "not_requested"),
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class VerifiedProviderReuseAuthorityV1:
    """Identity evidence already accepted by a stage's existing reuse gate."""

    request_hash: str
    route_identity: tuple[str, ...]
    receipt_hash: str
    output_hash: str
    authority_hash: str

    def __post_init__(self) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.request_hash,
                self.receipt_hash,
                self.output_hash,
                self.authority_hash,
            )
        ):
            raise StagePlanError("verified provider reuse requires request, receipt, output, and authority hashes")
        if len(self.route_identity) < 3 or any(not str(item).strip() for item in self.route_identity[:3]):
            raise StagePlanError("verified provider reuse requires a complete route identity")


@dataclass(frozen=True)
class ProviderRequestPlanRowV1:
    """One call projected from an actual stage request builder."""

    stage_name: str
    semantic_role: str
    request_key_hash: str
    source_builder: str
    route_identity: tuple[str, ...]
    request_estimate: ProviderRequestEstimateV1
    retry_attempts: int | None
    conditional_on: str = ""
    verified_reuse_authority_hash: str = ""
    estimated_cost: float | None = None
    price_status: str = "unknown"
    pricing_source: str = ""
    wall_seconds_upper_bound: float | None = None

    def __post_init__(self) -> None:
        if not str(self.stage_name).strip() or not str(self.semantic_role).strip():
            raise StagePlanError("provider request stage and semantic role are required")
        if not str(self.request_key_hash).strip() or not str(self.source_builder).strip():
            raise StagePlanError("provider request key hash and source builder are required")
        if len(self.route_identity) < 3 or any(not str(item).strip() for item in self.route_identity[:3]):
            raise StagePlanError("provider request route identity is incomplete")
        if self.retry_attempts is not None and (
            isinstance(self.retry_attempts, bool)
            or not isinstance(self.retry_attempts, int)
            or self.retry_attempts < 0
        ):
            raise StagePlanError("provider request retry reserve must be non-negative or unknown")
        if self.estimated_cost is not None and (
            not math.isfinite(float(self.estimated_cost)) or float(self.estimated_cost) < 0
        ):
            raise StagePlanError("provider request cost estimate must be finite and non-negative")
        if self.wall_seconds_upper_bound is not None and (
            not math.isfinite(float(self.wall_seconds_upper_bound))
            or float(self.wall_seconds_upper_bound) < 0
        ):
            raise StagePlanError("provider request wall-time upper bound must be finite and non-negative")
        if self.verified_reuse_authority_hash:
            if self.retry_attempts not in (0, None):
                raise StagePlanError("verified reuse cannot reserve provider retries")
            if self.estimated_cost not in (0, 0.0, None):
                raise StagePlanError("verified reuse cannot reserve new provider cost")

    @property
    def verified_reuse(self) -> bool:
        return bool(self.verified_reuse_authority_hash)

    @property
    def physical_attempt_upper_bound(self) -> int | None:
        if self.verified_reuse:
            return 0
        if self.retry_attempts is None:
            return None
        return 1 + int(self.retry_attempts)

    def to_dict(self) -> dict[str, Any]:
        estimate = self.request_estimate.to_dict()
        return {
            "stage_name": self.stage_name,
            "semantic_role": self.semantic_role,
            "request_key_hash": self.request_key_hash,
            "source_builder": self.source_builder,
            "route_identity": list(self.route_identity),
            "request_hash": estimate["request_hash"],
            "estimated_input_tokens": estimate["estimated_input_tokens"],
            "requested_output_tokens": estimate["requested_output_tokens"],
            "reasoning_reserve_tokens": estimate["reasoning_reserve_tokens"],
            "safety_margin_tokens": estimate["safety_margin_tokens"],
            "estimated_total_tokens": estimate["estimated_total_tokens"],
            "input_budget": estimate["input_budget"],
            "within_input_budget": estimate["within_input_budget"],
            "within_context_budget": estimate["within_context_budget"],
            "tokenizer_strategy": estimate["tokenizer_strategy"],
            "retry_attempts": self.retry_attempts,
            "physical_attempt_upper_bound": self.physical_attempt_upper_bound,
            "conditional_on": self.conditional_on or None,
            "verified_reuse": self.verified_reuse,
            "verified_reuse_authority_hash": self.verified_reuse_authority_hash or None,
            "estimated_cost": self.estimated_cost,
            "price_status": self.price_status,
            "pricing_source": self.pricing_source or None,
            "wall_seconds_upper_bound": self.wall_seconds_upper_bound,
        }


@dataclass(frozen=True)
class UnplannedProviderExposureV1:
    """A reachable provider branch with no complete serialized request yet."""

    stage_name: str
    semantic_role: str
    reason: str
    route_identity: tuple[str, ...] = ()
    conditional_on: str = ""
    logical_calls_upper_bound: int | None = None
    input_tokens_per_call_upper_bound: int | None = None
    output_tokens_per_call_upper_bound: int | None = None
    reasoning_tokens_per_call_upper_bound: int | None = None
    retry_attempts_per_call_upper_bound: int | None = None
    wall_seconds_per_call_upper_bound: float | None = None

    def __post_init__(self) -> None:
        if not str(self.stage_name).strip() or not str(self.semantic_role).strip():
            raise StagePlanError("unplanned provider exposure stage and semantic role are required")
        if not str(self.reason).strip():
            raise StagePlanError("unplanned provider exposure requires a reason")
        for name in (
            "logical_calls_upper_bound",
            "input_tokens_per_call_upper_bound",
            "output_tokens_per_call_upper_bound",
            "reasoning_tokens_per_call_upper_bound",
            "retry_attempts_per_call_upper_bound",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) < 0):
                raise StagePlanError(f"{name} must be non-negative or unknown")
        if self.wall_seconds_per_call_upper_bound is not None and (
            not math.isfinite(float(self.wall_seconds_per_call_upper_bound))
            or float(self.wall_seconds_per_call_upper_bound) < 0
        ):
            raise StagePlanError("unplanned provider exposure wall-time bound must be finite and non-negative")

    @property
    def physical_attempt_upper_bound(self) -> int | None:
        if self.logical_calls_upper_bound is None or self.retry_attempts_per_call_upper_bound is None:
            return None
        return int(self.logical_calls_upper_bound) * (1 + int(self.retry_attempts_per_call_upper_bound))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "semantic_role": self.semantic_role,
            "reason": self.reason,
            "route_identity": list(self.route_identity),
            "conditional_on": self.conditional_on or None,
            "logical_calls_upper_bound": self.logical_calls_upper_bound,
            "input_tokens_per_call_upper_bound": self.input_tokens_per_call_upper_bound,
            "output_tokens_per_call_upper_bound": self.output_tokens_per_call_upper_bound,
            "reasoning_tokens_per_call_upper_bound": self.reasoning_tokens_per_call_upper_bound,
            "retry_attempts_per_call_upper_bound": self.retry_attempts_per_call_upper_bound,
            "physical_attempt_upper_bound": self.physical_attempt_upper_bound,
            "wall_seconds_per_call_upper_bound": self.wall_seconds_per_call_upper_bound,
        }


@dataclass(frozen=True)
class ProviderStageRequestInventoryV1:
    """Request-builder output or explicit unknown exposure for one stage."""

    stage_name: str
    source_builder: str
    requests: tuple[ProviderRequestPlanRowV1, ...] = ()
    unknown_exposures: tuple[UnplannedProviderExposureV1, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.stage_name).strip() or not str(self.source_builder).strip():
            raise StagePlanError("stage request inventory requires a stage and source builder")
        if any(item.stage_name != self.stage_name for item in self.requests):
            raise StagePlanError("stage request inventory contains a request from another stage")
        if any(item.stage_name != self.stage_name for item in self.unknown_exposures):
            raise StagePlanError("stage request inventory contains an exposure from another stage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "source_builder": self.source_builder,
            "requests": [item.to_dict() for item in self.requests],
            "unknown_exposures": [item.to_dict() for item in self.unknown_exposures],
        }


def _route_field(route: Any, name: str, default: Any = None) -> Any:
    if isinstance(route, Mapping):
        return route.get(name, default)
    return getattr(route, name, default)


def _route_identity(route: Any) -> tuple[str, ...]:
    if isinstance(route, Mapping):
        raw = route.get("physical_identity") or route.get("binding_identity")
    else:
        raw = getattr(route, "binding_identity", None) or getattr(route, "identity", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise StagePlanError("provider request needs a resolved route identity")
    identity = tuple(str(item).strip() for item in raw)
    if len(identity) < 3 or any(not item for item in identity[:3]):
        raise StagePlanError("provider request route identity is incomplete")
    return identity


def build_provider_request_plan_row_v1(
    *,
    stage_name: str,
    request_id: str,
    source_builder: str,
    route: Any,
    request_payload: Mapping[str, Any],
    profile: ProviderContextProfile,
    retry_attempts: int | None,
    requested_output_tokens: int | None = None,
    reasoning_reserve_tokens: int | None = None,
    conditional_on: str = "",
    verified_reuse: VerifiedProviderReuseAuthorityV1 | None = None,
    pricing_per_1k_tokens: Mapping[str, float] | None = None,
    pricing_source: str = "",
    wall_seconds_upper_bound: float | None = None,
) -> ProviderRequestPlanRowV1:
    """Measure a stage builder's actual provider-visible request body.

    `request_payload` must be the serialized schema sent to the provider,
    including visible interpretation dependencies, system/task instructions,
    and structural wrappers. The projection stores only hashes and estimates.
    """

    estimate = profile.estimate_request_v1(
        request_payload,
        requested_output_tokens=requested_output_tokens,
        reasoning_reserve_tokens=reasoning_reserve_tokens,
    )
    if not str(request_id).strip():
        raise StagePlanError("provider request ID is required for projection identity")
    identity = _route_identity(route)
    route_provider = str(
        _route_field(route, "provider_family", "")
        or _route_field(route, "provider_name", "")
        or _route_field(route, "provider", "")
    )
    route_model = str(_route_field(route, "model", "") or "")
    route_endpoint = str(_route_field(route, "endpoint_type", "") or "")
    if (route_provider, route_model, route_endpoint) != (
        estimate.provider,
        estimate.model,
        estimate.endpoint_type,
    ):
        raise StagePlanError("provider request profile does not match the reachable route identity")
    if retry_attempts is not None and (
        isinstance(retry_attempts, bool)
        or not isinstance(retry_attempts, int)
        or retry_attempts < 0
    ):
        raise StagePlanError("provider retry reserve must be non-negative or unknown")

    reuse_hash = ""
    if verified_reuse is not None:
        if verified_reuse.request_hash != estimate.request_hash:
            raise StagePlanError("verified reuse request hash does not match the serialized request")
        if tuple(verified_reuse.route_identity) != identity:
            raise StagePlanError("verified reuse route identity does not match the reachable route")
        reuse_hash = verified_reuse.authority_hash
        retry_attempts = 0

    estimated_cost: float | None = 0.0 if verified_reuse is not None else None
    price_status = "verified_reuse_no_provider_cost" if verified_reuse is not None else "unknown"
    if verified_reuse is None and pricing_per_1k_tokens is not None:
        rate_names = (
            "input_cost_per_1k_tokens",
            "output_cost_per_1k_tokens",
            "reasoning_cost_per_1k_tokens",
        )
        rates: dict[str, float] = {}
        for name in rate_names:
            raw = pricing_per_1k_tokens.get(name)
            if raw is None or isinstance(raw, bool):
                rates = {}
                break
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise StagePlanError(f"{name} must be finite and non-negative")
            rates[name] = value
        if len(rates) == len(rate_names) and pricing_source.strip():
            estimated_cost = (
                estimate.estimated_input_tokens * rates["input_cost_per_1k_tokens"]
                + estimate.requested_output_tokens * rates["output_cost_per_1k_tokens"]
                + estimate.reasoning_reserve_tokens * rates["reasoning_cost_per_1k_tokens"]
            ) / 1000.0
            price_status = "estimate"
        else:
            price_status = "unknown_incomplete_route_pricing"

    from runtime.provider_runtime import hash_json

    return ProviderRequestPlanRowV1(
        stage_name=str(stage_name).strip(),
        semantic_role=str(_route_field(route, "semantic_role", "") or _route_field(route, "role", "") or "unknown"),
        request_key_hash=hash_json({"stage": str(stage_name).strip(), "request_id": str(request_id)}),
        source_builder=str(source_builder).strip(),
        route_identity=identity,
        request_estimate=estimate,
        retry_attempts=retry_attempts,
        conditional_on=str(conditional_on or "").strip(),
        verified_reuse_authority_hash=reuse_hash,
        estimated_cost=estimated_cost,
        price_status=price_status,
        pricing_source=str(pricing_source or "").strip(),
        wall_seconds_upper_bound=wall_seconds_upper_bound,
    )


def _plan_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    payload = to_dict() if callable(to_dict) else None
    if not isinstance(payload, Mapping):
        raise StagePlanError("stage and route plans must be typed plans or mappings")
    return payload


def build_full_stage_request_plan_v1(
    *,
    stage_plan: StagePlan | Mapping[str, Any],
    reachable_route_plan: Any,
    stage_inventories: Iterable[ProviderStageRequestInventoryV1],
    aggregate_budget: Any,
    local_steps: Iterable[str] = (),
) -> dict[str, Any]:
    """Project the reachable provider work against the existing run budget.

    This is a report projection only. It never mutates or reserves the budget.
    Exact rows must come from stage request builders; a reachable role without
    a complete builder row is represented as unknown exposure. Local steps are
    recorded separately and have zero provider calls by definition.
    """

    from runtime.provider_runtime import (
        AUTHORIZED_PROVIDER_CALL_LIMIT,
        ProviderAggregateBudgetV1,
        authorized_provider_call_limit,
        hash_json,
    )

    if not isinstance(aggregate_budget, ProviderAggregateBudgetV1):
        raise StagePlanError("full-stage projection requires ProviderAggregateBudgetV1")
    stage_payload = _plan_payload(stage_plan)
    route_payload = _plan_payload(reachable_route_plan)
    requested = tuple(str(item) for item in stage_payload.get("requested_stages") or ())
    required = tuple(str(item) for item in stage_payload.get("required_stages") or requested)
    requested_set = set(requested)
    routes = [
        dict(item)
        for item in route_payload.get("routes") or ()
        if isinstance(item, Mapping)
    ]
    route_by_role = {
        (str(item.get("stage") or ""), str(item.get("semantic_role") or "")): item
        for item in routes
    }
    inventories = tuple(stage_inventories)
    inventory_by_stage = {item.stage_name: item for item in inventories}
    if len(inventory_by_stage) != len(inventories):
        raise StagePlanError("full-stage request plan has duplicate stage inventories")

    rows: list[ProviderRequestPlanRowV1] = []
    exposures: list[UnplannedProviderExposureV1] = []
    for inventory in inventories:
        if inventory.stage_name not in requested_set and not any(
            item.conditional_on for item in inventory.unknown_exposures
        ) and not any(item.conditional_on for item in inventory.requests):
            raise StagePlanError("request inventory is outside the durable requested stage plan")
        rows.extend(inventory.requests)
        exposures.extend(inventory.unknown_exposures)

    represented_roles: set[tuple[str, str]] = set()
    for item in rows:
        route = route_by_role.get((item.stage_name, item.semantic_role))
        if route is None:
            raise StagePlanError("provider request row has no matching reachable route")
        if not bool(route.get("enabled", True)) or not bool(route.get("resolved", False)):
            raise StagePlanError("provider request row uses a disabled or unresolved route")
        route_identity = tuple(str(value) for value in route.get("physical_identity") or ())
        if route_identity and route_identity != item.route_identity:
            raise StagePlanError("provider request row route identity differs from the route plan")
        represented_roles.add((item.stage_name, item.semantic_role))

    # Every enabled required route in a requested stage must have either an
    # exact request inventory or an explicit unknown-exposure record.
    represented_roles.update((item.stage_name, item.semantic_role) for item in exposures)
    for route in routes:
        stage_name = str(route.get("stage") or "")
        role = str(route.get("semantic_role") or "")
        if (
            stage_name in requested_set
            and bool(route.get("enabled", True))
            and bool(route.get("required", True))
            and (stage_name, role) not in represented_roles
        ):
            exposures.append(
                UnplannedProviderExposureV1(
                    stage_name=stage_name,
                    semantic_role=role,
                    reason="reachable_route_has_no_request_builder_inventory",
                    route_identity=tuple(str(value) for value in route.get("physical_identity") or ()),
                )
            )
    for route in route_payload.get("unresolved_required_routes") or ():
        if not isinstance(route, Mapping):
            continue
        stage_name = str(route.get("stage") or "")
        role = str(route.get("semantic_role") or "")
        if stage_name in requested_set and (stage_name, role) not in represented_roles:
            exposures.append(
                UnplannedProviderExposureV1(
                    stage_name=stage_name,
                    semantic_role=role,
                    reason="reachable_required_route_is_unresolved",
                )
            )

    if len({item.request_key_hash for item in rows}) != len(rows):
        raise StagePlanError("full-stage request plan contains duplicate request identities")

    fresh_rows = [item for item in rows if not item.verified_reuse]
    conditional_rows = [item for item in fresh_rows if item.conditional_on]
    definite_rows = [item for item in fresh_rows if not item.conditional_on]
    known_logical = len(fresh_rows)
    active_exposures = [item for item in exposures if item.logical_calls_upper_bound != 0]
    unknown_count = any(item.logical_calls_upper_bound is None for item in active_exposures)
    exposure_logical_upper = sum(
        int(item.logical_calls_upper_bound or 0)
        for item in active_exposures
        if item.logical_calls_upper_bound is not None
    )
    logical_upper = None if unknown_count else known_logical + exposure_logical_upper

    row_attempts = [item.physical_attempt_upper_bound for item in fresh_rows]
    exposure_attempts = [item.physical_attempt_upper_bound for item in active_exposures]
    physical_unknown = any(item is None for item in (*row_attempts, *exposure_attempts))
    physical_upper = (
        None
        if physical_unknown
        else sum(int(item or 0) for item in row_attempts)
        + sum(int(item or 0) for item in exposure_attempts)
    )
    physical_lower = len(definite_rows)
    output_reserved_lower = sum(
        int(item.request_estimate.requested_output_tokens) for item in definite_rows
    )
    retries_unknown = any(item.retry_attempts is None for item in fresh_rows) or any(
        item.logical_calls_upper_bound is None or item.retry_attempts_per_call_upper_bound is None
        for item in active_exposures
    )
    retry_upper = (
        None
        if retries_unknown
        else sum(int(item.retry_attempts or 0) for item in fresh_rows)
        + sum(
            int(item.logical_calls_upper_bound or 0)
            * int(item.retry_attempts_per_call_upper_bound or 0)
            for item in active_exposures
        )
    )

    def total_or_unknown(values: Sequence[int | None], exposure_values: Sequence[int | None]) -> int | None:
        if any(value is None for value in values) or any(value is None for value in exposure_values):
            return None
        return sum(int(value or 0) for value in values) + sum(
            int(value or 0) for value in exposure_values
        )

    input_once = total_or_unknown(
        [item.request_estimate.estimated_input_tokens for item in fresh_rows],
        [
            0
            if item.logical_calls_upper_bound == 0
            else None
            if item.logical_calls_upper_bound is None or item.input_tokens_per_call_upper_bound is None
            else int(item.logical_calls_upper_bound) * int(item.input_tokens_per_call_upper_bound)
            for item in active_exposures
        ],
    )
    input_all_attempts = (
        None
        if physical_upper is None
        or any(item.input_tokens_per_call_upper_bound is None for item in active_exposures)
        else sum(
            int(item.request_estimate.estimated_input_tokens) * int(item.physical_attempt_upper_bound or 0)
            for item in fresh_rows
        )
        + sum(
            int(item.logical_calls_upper_bound or 0)
            * int(item.input_tokens_per_call_upper_bound or 0)
            * (1 + int(item.retry_attempts_per_call_upper_bound or 0))
            for item in active_exposures
        )
    )
    output_reserved = total_or_unknown(
        [item.request_estimate.requested_output_tokens for item in fresh_rows],
        [
            0
            if item.logical_calls_upper_bound == 0
            else None
            if item.logical_calls_upper_bound is None or item.output_tokens_per_call_upper_bound is None
            else int(item.logical_calls_upper_bound) * int(item.output_tokens_per_call_upper_bound)
            for item in active_exposures
        ],
    )
    output_all_attempts = (
        None
        if physical_upper is None
        or any(item.output_tokens_per_call_upper_bound is None for item in active_exposures)
        else sum(
            int(item.request_estimate.requested_output_tokens) * int(item.physical_attempt_upper_bound or 0)
            for item in fresh_rows
        )
        + sum(
            int(item.logical_calls_upper_bound or 0)
            * int(item.output_tokens_per_call_upper_bound or 0)
            * (1 + int(item.retry_attempts_per_call_upper_bound or 0))
            for item in active_exposures
        )
    )
    reasoning_all_attempts = (
        None
        if physical_upper is None
        or any(item.reasoning_tokens_per_call_upper_bound is None for item in active_exposures)
        else sum(
            int(item.request_estimate.reasoning_reserve_tokens) * int(item.physical_attempt_upper_bound or 0)
            for item in fresh_rows
        )
        + sum(
            int(item.logical_calls_upper_bound or 0)
            * int(item.reasoning_tokens_per_call_upper_bound or 0)
            * (1 + int(item.retry_attempts_per_call_upper_bound or 0))
            for item in active_exposures
        )
    )
    wall_unknown = any(item.wall_seconds_upper_bound is None for item in fresh_rows) or any(
        item.wall_seconds_per_call_upper_bound is None
        for item in active_exposures
    )
    wall_upper = (
        None
        if wall_unknown
        else sum(float(item.wall_seconds_upper_bound or 0.0) for item in fresh_rows)
        + sum(
            int(item.logical_calls_upper_bound or 0)
            * float(item.wall_seconds_per_call_upper_bound or 0.0)
            for item in active_exposures
        )
    )

    authorized_limit = authorized_provider_call_limit(
        aggregate_budget.max_provider_calls_total or None
    )
    call_status = (
        "exceeded"
        if physical_lower > authorized_limit
        else "exceeded"
        if physical_upper is not None and physical_upper > authorized_limit
        else "unknown"
        if physical_upper is None
        else "within_limit"
    )
    retry_status = (
        "unbounded"
        if not aggregate_budget.max_retry_attempts_total
        else "unknown"
        if retry_upper is None
        else "exceeded"
        if retry_upper > aggregate_budget.max_retry_attempts_total
        else "within_limit"
    )
    output_status = (
        "unbounded"
        if not aggregate_budget.max_output_tokens_total
        else "exceeded"
        if output_reserved_lower > aggregate_budget.max_output_tokens_total
        else "exceeded"
        if output_reserved is not None
        and output_reserved > aggregate_budget.max_output_tokens_total
        else "unknown"
        if output_all_attempts is None
        else "exceeded"
        if output_all_attempts > aggregate_budget.max_output_tokens_total
        else "within_limit"
    )
    wall_status = (
        "unbounded"
        if not aggregate_budget.max_wall_seconds
        else "unknown"
        if wall_upper is None
        else "exceeded"
        if wall_upper > aggregate_budget.max_wall_seconds
        else "within_limit"
    )
    request_context_status = (
        "exceeded"
        if any(not item.request_estimate.within_context_budget for item in fresh_rows)
        else "unknown"
        if active_exposures
        else "within_limit"
    )
    budget_exceeded = any(
        status == "exceeded"
        for status in (call_status, retry_status, output_status, wall_status, request_context_status)
    )
    unknown_exposure = bool(active_exposures) or any(
        status == "unknown"
        for status in (call_status, retry_status, output_status, wall_status, request_context_status)
    )
    if budget_exceeded:
        admission_status = "blocked_budget"
    elif unknown_exposure:
        admission_status = "incomplete_unknown_exposure"
    elif any(item.conditional_on for item in fresh_rows):
        admission_status = "conditional_within_budget"
    else:
        admission_status = "within_budget"

    priced_rows = [item for item in fresh_rows if item.estimated_cost is not None]
    cost_unknown = (
        len(priced_rows) != len(fresh_rows)
        or bool(active_exposures)
        or any(item.physical_attempt_upper_bound is None for item in priced_rows)
    )
    if cost_unknown:
        total_cost = None
        cost_status = "unknown_no_complete_route_pricing"
    else:
        total_cost = sum(
            float(item.estimated_cost or 0.0) * int(item.physical_attempt_upper_bound or 0)
            for item in priced_rows
        )
        cost_status = "estimate" if priced_rows else "zero_provider_posts"

    output = {
        "schema_version": "full-stage-provider-request-plan-v1",
        "action": str(stage_payload.get("action") or ""),
        "requested_stages": list(requested),
        "required_stages": list(required),
        "stage_plan_version": str(stage_payload.get("version") or ""),
        "reachable_route_plan_hash": hash_json(route_payload),
        "stage_inventories": [item.to_dict() for item in inventories],
        "provider_requests": [item.to_dict() for item in rows],
        "unknown_exposures": [item.to_dict() for item in exposures],
        "local_steps": list(dict.fromkeys(str(item) for item in local_steps if str(item).strip())),
        "local_steps_provider_calls": 0,
        "aggregate_budget": aggregate_budget.to_dict(),
        "limits": {
            "authorized_provider_call_limit": AUTHORIZED_PROVIDER_CALL_LIMIT,
            "effective_provider_call_limit": authorized_limit,
        },
        "totals": {
            "logical_calls_known": known_logical,
            "logical_calls_unconditional": len(definite_rows),
            "logical_calls_conditional": len(conditional_rows),
            "logical_calls_upper_bound": logical_upper,
            "physical_attempts_known_lower_bound": physical_lower,
            "physical_attempts_upper_bound": physical_upper,
            "retry_attempts_upper_bound": retry_upper,
            "estimated_input_tokens_one_attempt": input_once,
            "estimated_input_tokens_all_attempts": input_all_attempts,
            "aggregate_output_tokens_reserved": output_reserved,
            "aggregate_output_tokens_known_lower_bound": output_reserved_lower,
            "estimated_output_tokens_all_attempts": output_all_attempts,
            "estimated_reasoning_tokens_all_attempts": reasoning_all_attempts,
            "verified_reuse_calls": sum(item.verified_reuse for item in rows),
            "unknown_exposure_count": len(exposures),
            "wall_seconds_upper_bound": wall_upper,
            "wall_budget_seconds": aggregate_budget.max_wall_seconds or None,
            "price_status": cost_status,
            "estimated_cost": total_cost,
        },
        "budget_status": {
            "provider_calls": call_status,
            "provider_retries": retry_status,
            "requested_output_tokens": output_status,
            "per_request_context": request_context_status,
            "wall_time": wall_status,
            "admission": admission_status,
        },
        "projection_identity_hash": hash_json(
            {
                "action": stage_payload.get("action"),
                "stage_plan": stage_payload,
                "reachable_route_plan": route_payload,
                "provider_requests": [item.to_dict() for item in rows],
                "unknown_exposures": [item.to_dict() for item in exposures],
                "aggregate_budget": aggregate_budget.to_dict(),
            }
        ),
        "boundary": {
            "projection_only": True,
            "no_provider_posts": True,
            "input_estimates_are_not_provider_tokenizer_measurements": True,
            "aggregate_output_reserve_is_once_per_logical_call": True,
            "output_admission_uses_all_attempts_upper_bound": True,
            "all_attempt_token_exposure_multiplies_retries": True,
            "unknown_or_conditional_stage_work_is_not_inferred_from_other_stage_counts": True,
        },
    }
    return output


__all__ = [
    "ProviderRequestPlanRowV1",
    "ProviderStageRequestInventoryV1",
    "StagePlan",
    "StagePlanError",
    "UnplannedProviderExposureV1",
    "VerifiedProviderReuseAuthorityV1",
    "build_full_stage_request_plan_v1",
    "build_provider_request_plan_row_v1",
    "build_stage_plan",
    "stage_plan_from_metadata",
]

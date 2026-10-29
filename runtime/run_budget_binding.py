"""Resolve finite ordinary-run resources without granting acceptance authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    authorized_provider_call_limit,
)


def resolve_run_provider_budget(
    metadata: Mapping[str, Any], config: Mapping[str, Mapping[str, Any]],
) -> tuple[ProviderAggregateBudgetV2, str]:
    explicit = metadata.get("provider_budget")
    if explicit is not None:
        if not isinstance(explicit, Mapping):
            raise ValueError("metadata.provider_budget must be a strict V2 budget object")
        return ProviderAggregateBudgetV2.from_mapping(explicit), "explicit_runtime_spec"
    calls = authorized_provider_call_limit((config.get("OutlineStability") or {}).get("max_provider_calls"))
    runtime = config.get("Runtime") or {}
    retry_pool = max(0, int(runtime.get("max_retry_attempts_total", runtime.get("transport_retries", 2))))
    route_outputs, route_deadlines = [], []
    for name, values in config.items():
        if not name.endswith("_API") or not values.get("model"):
            continue
        route_outputs.append(max(1, int(values.get("max_output_tokens") or 32000)))
        route_deadlines.append(max(1, int(values.get("total_timeout_seconds") or 600)))

    # Stage 1's per-request output ceiling is resolved independently from the
    # API route's max_output_tokens field. Include the effective initial and
    # same-provider length-retry ceilings for every configured reader route;
    # this resolver does not receive the action's reachable route plan, so its
    # default is deliberately conservative across all configured routes.
    from services.stage1_output_budget import stage1_output_budget_sequence

    stage1_settings = config.get("Stage1_Input") or {}
    for route_name in ("Primary_Reader_API", "Backup_Reader_API"):
        reader_route = config.get(route_name) or {}
        if not reader_route.get("model"):
            continue
        for stage in ("visual_scan", "synthesis"):
            route_outputs.extend(
                stage1_output_budget_sequence(
                    stage,
                    stage1_settings,
                    provider_config=reader_route,
                )
            )

    # A global POST cap also bounds total output/time exposure under these
    # configured route and stage ceilings. These are limits, not estimated
    # usage or fees.
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=calls * max(route_outputs, default=32000),
        max_retry_attempts_total=min(retry_pool, max(0, calls - 1)),
        max_wall_seconds=calls * max(route_deadlines, default=600),
    ), "documented_default"

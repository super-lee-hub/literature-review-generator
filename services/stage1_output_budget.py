"""Stage-specific output budgets and bounded length-retry planning."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_VISUAL_SCAN_MAX_OUTPUT_TOKENS = 16_000
DEFAULT_SYNTHESIS_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_LENGTH_RETRY_MAX_ATTEMPTS = 2
DEFAULT_LENGTH_RETRY_CEILING_TOKENS = 65_536
DEFAULT_STAGE1_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_SEMANTIC_RETRY_MAX_ATTEMPTS = 1


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed >= 0 else int(default)


def stage1_output_budget_sequence(
    stage: str,
    settings: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    """Return the initial budget plus a finite same-provider escalation path.

    The stage-specific value is the first request budget.  A ``length`` result
    may advance through at most ``stage1_length_retry_max_attempts`` larger
    budgets, capped by the configured ceiling.  No transport or provider
    change is implied by this plan.
    """

    values = dict(settings or {})
    normalized_stage = str(stage or "").strip().casefold()
    if normalized_stage == "visual_scan":
        base_key = "stage1_visual_scan_max_output_tokens"
        default_base = DEFAULT_VISUAL_SCAN_MAX_OUTPUT_TOKENS
    elif normalized_stage == "synthesis":
        base_key = "stage1_synthesis_max_output_tokens"
        default_base = DEFAULT_SYNTHESIS_MAX_OUTPUT_TOKENS
    else:
        raise ValueError(f"unsupported Stage 1 output-budget stage: {stage!r}")

    base = _positive_int(values.get(base_key), default_base)
    ceiling = _positive_int(
        values.get(
            "stage1_length_retry_ceiling_tokens",
            DEFAULT_LENGTH_RETRY_CEILING_TOKENS,
        ),
        DEFAULT_LENGTH_RETRY_CEILING_TOKENS,
    )
    if ceiling < base:
        raise ValueError(
            f"{base_key} cannot exceed stage1_length_retry_ceiling_tokens"
        )
    retry_count = _nonnegative_int(
        values.get(
            "stage1_length_retry_max_attempts",
            DEFAULT_LENGTH_RETRY_MAX_ATTEMPTS,
        ),
        DEFAULT_LENGTH_RETRY_MAX_ATTEMPTS,
    )
    budgets = [base]
    current = base
    for retry_index in range(retry_count):
        if current >= ceiling:
            break
        current = ceiling if retry_index == retry_count - 1 else min(ceiling, current * 2)
        budgets.append(current)
    return tuple(budgets)


def stage1_output_budget_snapshot(
    stage: str,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a non-secret provenance projection for one budget plan."""

    budgets = stage1_output_budget_sequence(stage, settings)
    return {
        "stage": str(stage),
        "requested_output_budgets": list(budgets),
        "initial_output_tokens": budgets[0],
        "length_retry_count": max(0, len(budgets) - 1),
        "ceiling_output_tokens": budgets[-1],
    }


def stage1_request_timeout_seconds(settings: Mapping[str, Any] | None = None) -> int:
    """Return the independent Stage 1 long-request timeout."""

    return _positive_int(
        dict(settings or {}).get("stage1_request_timeout_seconds"),
        DEFAULT_STAGE1_REQUEST_TIMEOUT_SECONDS,
    )


def stage1_semantic_retry_max_attempts(settings: Mapping[str, Any] | None = None) -> int:
    """Return the finite same-primary retry count for schema-invalid visuals."""

    return min(
        3,
        _nonnegative_int(
            dict(settings or {}).get("stage1_semantic_retry_max_attempts"),
            DEFAULT_SEMANTIC_RETRY_MAX_ATTEMPTS,
        ),
    )


__all__ = [
    "DEFAULT_LENGTH_RETRY_CEILING_TOKENS",
    "DEFAULT_LENGTH_RETRY_MAX_ATTEMPTS",
    "DEFAULT_STAGE1_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_SEMANTIC_RETRY_MAX_ATTEMPTS",
    "DEFAULT_SYNTHESIS_MAX_OUTPUT_TOKENS",
    "DEFAULT_VISUAL_SCAN_MAX_OUTPUT_TOKENS",
    "stage1_request_timeout_seconds",
    "stage1_semantic_retry_max_attempts",
    "stage1_output_budget_sequence",
    "stage1_output_budget_snapshot",
]

"""Compatibility wrapper for the explicit current validation service.

This module intentionally has no import path to the historical ``validator``
module.  Callers must provide the current execution service, which owns the
durable inputs, provider ledger, and report registration.
"""

from __future__ import annotations

from typing import Any, Mapping


def run_current_review_validation(adapter: Any) -> dict[str, Any]:
    """Run the explicit current service without reviving a generator adapter."""

    runner = getattr(adapter, "run_review_validation", None)
    if not callable(runner):
        raise TypeError("current validation requires ValidationExecutionService")
    result = runner()
    return dict(result) if isinstance(result, Mapping) else {}


__all__ = ["run_current_review_validation"]

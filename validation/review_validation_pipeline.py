"""Current validation-stage execution seam.

The durable runtime owns the explicit service contract.  The historical
validator implementation is loaded only behind this narrow compatibility
seam so callers cannot pass a generator-shaped object through the production
orchestration layer.
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping


def run_current_review_validation(adapter: Any) -> dict[str, Any]:
    """Run the existing claim engine through the current service adapter."""

    module = importlib.import_module("validator")
    runner = getattr(module, "run_review_validation", None)
    if not callable(runner):
        raise RuntimeError("current review validation runner is unavailable")
    result = runner(adapter)
    return dict(result) if isinstance(result, Mapping) else {}


__all__ = ["run_current_review_validation"]

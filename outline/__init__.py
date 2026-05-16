"""Outline module — Week 5 + Outline Intelligence v2 Complete Vertical Slice.

JSON-first outline representation with critique and arbitration.
V2 adds full literature map -> synthesis flow -> multi-candidate ->
critique -> arbitration -> audit -> adoption pipeline.
"""

from outline.models import (
    ArbitrationDecision,
    CritiqueArbitration,
    CritiqueCategory,
    OutlineArbitrationResult,
    OutlineCritique,
    OutlineDocument,
    OutlineSection,
    ReviewStatus,
    ReviewedOutlineDocument,
)
from outline.generator import (
    OutlineGenerator,
    create_outline_from_markdown,
    create_outline_from_sections,
    run_outline_generation,
)
from outline.arbitration import (
    OutlineArbitrator,
    adopt_outline,
    apply_accepted_critiques,
    arbitrate_critique,
    create_critique,
    run_arbitration,
    run_outline_adopt,
    run_outline_arbitration,
    run_outline_critique,
    run_peer_critique,
)
from outline.legacy_adapter import (
    OutlineLegacyAdapter,
    get_outline_markdown_for_downstream,
    is_reviewed_outline_adopted,
    outline_document_to_markdown,
    reviewed_outline_to_markdown,
)

from outline.runtime_resolver import OutlineRuntimeResolver, ResolveResult


def __getattr__(name):
    if name in {"V2Pipeline", "V2PipelineResult"}:
        from outline.pipeline import V2Pipeline, V2PipelineResult

        return {"V2Pipeline": V2Pipeline, "V2PipelineResult": V2PipelineResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # Models
    "ArbitrationDecision",
    "CritiqueArbitration",
    "CritiqueCategory",
    "OutlineArbitrationResult",
    "OutlineCritique",
    "OutlineDocument",
    "OutlineSection",
    "ReviewStatus",
    "ReviewedOutlineDocument",
    # Generator
    "OutlineGenerator",
    "create_outline_from_markdown",
    "create_outline_from_sections",
    "run_outline_generation",
    # Arbitration
    "OutlineArbitrator",
    "adopt_outline",
    "apply_accepted_critiques",
    "arbitrate_critique",
    "create_critique",
    "run_arbitration",
    "run_outline_adopt",
    "run_outline_arbitration",
    "run_outline_critique",
    "run_peer_critique",
    # Legacy Adapter
    "OutlineLegacyAdapter",
    "get_outline_markdown_for_downstream",
    "is_reviewed_outline_adopted",
    "outline_document_to_markdown",
    "reviewed_outline_to_markdown",
    # V2
    "OutlineRuntimeResolver",
    "ResolveResult",
    "V2Pipeline",
    "V2PipelineResult",
]

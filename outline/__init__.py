"""Outline module for Week 5.

JSON-first outline representation with critique and arbitration.
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
]

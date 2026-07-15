"""Explicit adoption service for Outline Intelligence v2.

Writes adopted_final_outline.json only after passing non-stale audit.
Does NOT write/substitute reviewed_outline.json.
Does NOT overwrite legacy Markdown.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from outline.v2_models import (
    AdoptedFinalOutline,
    CoverageAudit,
    FinalOutline,
    compute_content_hash,
)
from outline.stage_health import OutlineStageHealthV1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def verify_adoption_prerequisites(
    final_outline: FinalOutline,
    audit: CoverageAudit,
    stage_health: OutlineStageHealthV1 | None = None,
) -> Tuple[bool, str]:
    """Verify prerequisites for adoption.

    Returns (ok, error_message).
    """
    if stage_health is None:
        return False, "Outline stage health sidecar is required; adoption is blocked"

    if stage_health.job_id != final_outline.created_from_job_id:
        return False, "Outline stage health belongs to a different job; adoption is blocked"

    if not stage_health.adoptable:
        reasons = "; ".join(stage_health.degradation_reasons) or "stage health is not adoptable"
        return False, f"Outline provider chain is degraded; adoption is blocked: {reasons}"

    current_final_hash = compute_content_hash(final_outline.to_dict())
    current_audit_hash = compute_content_hash(audit.to_dict())
    if stage_health.source_final_outline_hash != current_final_hash:
        return False, "Stale stage health: final outline hash does not match"
    if stage_health.source_coverage_audit_hash != current_audit_hash:
        return False, "Stale stage health: coverage audit hash does not match"

    if not audit.passed:
        return False, "Coverage audit did not pass; adoption is blocked"

    if final_outline.review_status == "blocked":
        return False, "Final outline review_status is blocked; adoption is blocked"

    if final_outline.blocking_critique_ids:
        return False, "Final outline has unresolved blocking critiques; adoption is blocked"

    if audit.source_final_outline_hash != current_final_hash:
        return False, (
            f"Stale audit: audit hash ({audit.source_final_outline_hash[:16]}...) "
            f"does not match current final outline hash ({current_final_hash[:16]}...)"
        )

    return True, ""


def adopt_final_outline(
    final_outline: FinalOutline,
    audit: CoverageAudit,
    job_id: str,
    adopted_by: str,
    stage_health: OutlineStageHealthV1 | None = None,
) -> Tuple[Optional[AdoptedFinalOutline], str]:
    """Adopt a final outline after passing audit.

    Returns (adopted_outline_or_none, status_message).
    Does NOT overwrite legacy Markdown or reviewed_outline.json.
    """
    ok, err = verify_adoption_prerequisites(final_outline, audit, stage_health)
    if not ok:
        return None, err

    source_final_outline_hash = compute_content_hash(final_outline.to_dict())
    adopted_outline = replace(final_outline, adoption_status="adopted")

    adopted = AdoptedFinalOutline(
        created_from_job_id=job_id,
        source_final_outline_id=f"final_outline:{source_final_outline_hash[:12]}",
        source_final_outline_hash=source_final_outline_hash,
        source_coverage_audit_id=f"coverage_audit:{compute_content_hash(audit.to_dict())[:12]}",
        source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
        adopted_at=_utc_now_iso(),
        adopted_by=adopted_by,
        outline=adopted_outline,
    )

    return adopted, "Adoption successful"


def write_adopted_outline(
    adopted: AdoptedFinalOutline,
    path: str,
) -> str:
    """Write adopted final outline to disk. Returns the written path."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(adopted.to_dict(), f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)

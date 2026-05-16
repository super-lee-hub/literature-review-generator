"""Explicit adoption service for Outline Intelligence v2.

Writes adopted_final_outline.json only after passing non-stale audit.
Does NOT write/substitute reviewed_outline.json.
Does NOT overwrite legacy Markdown.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from outline.v2_models import (
    AdoptedFinalOutline,
    CoverageAudit,
    FinalOutline,
    compute_content_hash,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def verify_adoption_prerequisites(
    final_outline: FinalOutline,
    audit: CoverageAudit,
) -> Tuple[bool, str]:
    """Verify prerequisites for adoption.

    Returns (ok, error_message).
    """
    if not audit.passed:
        return False, "Coverage audit did not pass; adoption is blocked"

    current_final_hash = compute_content_hash(final_outline.to_dict())
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
) -> Tuple[Optional[AdoptedFinalOutline], str]:
    """Adopt a final outline after passing audit.

    Returns (adopted_outline_or_none, status_message).
    Does NOT overwrite legacy Markdown or reviewed_outline.json.
    """
    ok, err = verify_adoption_prerequisites(final_outline, audit)
    if not ok:
        return None, err

    adopted = AdoptedFinalOutline(
        created_from_job_id=job_id,
        source_final_outline_id=f"final_outline:{compute_content_hash(final_outline.to_dict())[:12]}",
        source_final_outline_hash=compute_content_hash(final_outline.to_dict()),
        source_coverage_audit_id=f"coverage_audit:{compute_content_hash(audit.to_dict())[:12]}",
        source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
        adopted_at=_utc_now_iso(),
        adopted_by=adopted_by,
        outline=final_outline,
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

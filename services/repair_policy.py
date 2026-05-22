"""External validation repair policy contract.

This module keeps user-facing repair policy values separate from the
Week 4 repair model enum, which only distinguishes report-first from
safe auto-apply.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ValidationRepairPolicy(str, Enum):
    REPORT_ONLY = "report_only"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_SAFE = "auto_safe"
    FULL_AUTO_EXPERIMENTAL = "full_auto_experimental"


DEFAULT_REPAIR_POLICY = ValidationRepairPolicy.REPORT_ONLY
REPAIR_POLICY_VALUES = {policy.value for policy in ValidationRepairPolicy}


def parse_repair_policy(value: Any) -> ValidationRepairPolicy:
    """Parse a user-facing repair policy.

    Missing or blank values are safe by default. Explicit unknown values
    are rejected because this setting controls whether generated text can
    be rewritten automatically.
    """

    if value is None:
        return DEFAULT_REPAIR_POLICY
    normalized = str(value).strip().lower()
    if not normalized:
        return DEFAULT_REPAIR_POLICY
    try:
        return ValidationRepairPolicy(normalized)
    except ValueError as exc:
        allowed = ", ".join(sorted(REPAIR_POLICY_VALUES))
        raise ValueError(f"Invalid [Validation].repair_policy={value!r}; expected one of: {allowed}") from exc


def repair_policy_to_week4_policy(policy: ValidationRepairPolicy):
    """Map external policy values to the existing Week 4 RepairPolicy enum."""

    from validation.repair_models import RepairPolicy

    if policy == ValidationRepairPolicy.AUTO_SAFE:
        return RepairPolicy.AUTO_APPLY_SAFE
    return RepairPolicy.REPORT_FIRST


def requires_manual_confirmation(policy: ValidationRepairPolicy) -> bool:
    return policy == ValidationRepairPolicy.MANUAL_CONFIRM


def unsafe_auto_rewrite_enabled(policy: ValidationRepairPolicy) -> bool:
    return policy == ValidationRepairPolicy.FULL_AUTO_EXPERIMENTAL


def auto_safe_apply_enabled(policy: ValidationRepairPolicy) -> bool:
    return policy == ValidationRepairPolicy.AUTO_SAFE


def is_auto_safe_proposal(proposal: Any) -> bool:
    """Return whether a proposal is eligible for non-text structural apply.

    The current Week 4 text patcher can mutate review text for citation
    mapping proposals, so auto-safe requires an explicit structural-only
    proposal shape before apply is allowed.
    """

    from validation.repair_models import RepairRootCause

    metadata = getattr(proposal, "metadata", {}) or {}
    if metadata.get("low_confidence") or metadata.get("evidence_status") in {
        "evidence_gap",
        "needs_review",
        "low_confidence",
    }:
        return False
    if getattr(proposal, "root_cause", None) != RepairRootCause.CITATION_MAPPING_ERROR:
        return False
    if str(getattr(proposal, "fix_strategy", "")).strip() not in {
        "manifest_fix",
        "manifest_fix_rerender",
        "bibliography_rerender",
    }:
        return False
    if metadata.get("structural_only") is True:
        return True
    return str(getattr(proposal, "original_text", "")) == str(getattr(proposal, "proposed_text", ""))

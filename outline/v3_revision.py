"""Pure post-arbitration candidate revision operations for Outline v3."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from runtime.provider_runtime import hash_json, hash_text


def apply_selected_revision(
    *,
    candidate_id: str,
    sections: Sequence[Mapping[str, Any]],
    recommendations: Sequence[Any] | None,
    view_by_key: Mapping[str, Any],
    parent_candidate_hash: str,
) -> dict[str, Any]:
    """Apply accepted title, goal, and claim revisions without side effects.

    This preserves the primary Outline executor's target inference and
    operation names. The returned ``accepted_recommendations`` list contains
    only non-empty mapping/string recommendations, copied from the input.
    Every unresolved target or unsupported operation is returned in
    ``unresolved_revisions`` instead of being silently ignored.
    """

    revised_sections = [deepcopy(dict(section)) for section in sections if isinstance(section, Mapping)]
    accepted_recommendations = [
        deepcopy(item)
        for item in (recommendations or ())
        if (isinstance(item, Mapping) and (item.get("issue_id") or item.get("recommendation") or item.get("text")))
        or (not isinstance(item, Mapping) and str(item).strip())
    ]
    revision_records: list[dict[str, Any]] = []
    unresolved_revisions: list[dict[str, Any]] = []
    known_section_ids = [str(section.get("section_id") or "") for section in revised_sections]
    section_ids_seen: set[str] = set()
    duplicate_section_ids: set[str] = set()
    for section_id in known_section_ids:
        if section_id and section_id in section_ids_seen:
            duplicate_section_ids.add(section_id)
        section_ids_seen.add(section_id)

    for raw_recommendation in accepted_recommendations:
        if isinstance(raw_recommendation, Mapping):
            recommendation = str(
                raw_recommendation.get("recommendation")
                or raw_recommendation.get("text")
                or raw_recommendation.get("reason")
                or ""
            ).strip()
            issue_id = str(raw_recommendation.get("issue_id") or "").strip() or f"issue:{hash_text(recommendation)[:16]}"
            target_values = (
                raw_recommendation.get("target_section_ids")
                or raw_recommendation.get("target_section_id")
                or raw_recommendation.get("section_id")
            )
            if isinstance(target_values, str):
                target_values = [target_values]
            target_ids = {str(value) for value in target_values or () if str(value)}
            operation = str(raw_recommendation.get("operation") or "").strip().lower()
            replacement = str(
                raw_recommendation.get("replacement")
                or raw_recommendation.get("new_value")
                or raw_recommendation.get("new_title")
                or raw_recommendation.get("new_goal")
                or ""
            )
        else:
            recommendation = str(raw_recommendation).strip()
            issue_id = f"issue:{hash_text(recommendation)[:16]}"
            target_ids = set()
            operation = ""
            replacement = ""

        lowered = recommendation.casefold()
        if not target_ids:
            target_ids = {
                section_id for section_id in known_section_ids
                if section_id and section_id.casefold() in lowered
            }
        if not target_ids:
            # Legacy S12-style references are accepted only when they exactly
            # identify a real section identity; a bare S12 is never positionally
            # rebound to another section.
            target_ids = {
                section_id for section_id in known_section_ids
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(section_id)}(?![A-Za-z0-9_])",
                    recommendation,
                    flags=re.IGNORECASE,
                )
            }
        if not operation:
            if "title" in lowered:
                operation = "replace_title"
            elif "goal" in lowered or "purpose" in lowered:
                operation = "replace_goal"
            elif any(marker in lowered for marker in ("remove claim", "delete claim", "drop claim")):
                operation = "remove_claim"
            elif any(marker in lowered for marker in ("aggregate", "共同指向", "共同说明", "概括性", "gap claim")):
                operation = "replace_aggregate_claim_with_per_paper_boundaries"

        targets = [
            section for section in revised_sections
            if str(section.get("section_id") or "") in target_ids
        ]
        operation_records: list[dict[str, Any]] = []
        target_results: list[dict[str, Any]] = []
        for section_id in sorted(target_ids):
            section = next(
                (item for item in targets if str(item.get("section_id") or "") == section_id),
                None,
            )
            if section_id in duplicate_section_ids:
                target_results.append({
                    "section_id": section_id,
                    "operation": operation or "unknown",
                    "status": "unresolved",
                    "reason": "target_section_id_ambiguous",
                })
                continue
            if section is None:
                target_results.append({
                    "section_id": section_id,
                    "operation": operation or "unknown",
                    "status": "unresolved",
                    "reason": "target_section_not_found",
                })
                continue

            before_hash = hash_json(section)
            claims = [str(item) for item in section.get("claims") or () if str(item).strip()]
            target_status = "unresolved"
            reason = "operation_not_applied"
            if operation == "replace_title":
                if not replacement:
                    reason = "replacement_missing"
                elif str(section.get("title") or "") == replacement:
                    target_status, reason = "already_satisfied", "title_already_matches"
                else:
                    section["title"] = replacement
                    target_status, reason = "changed", "title_replaced"
            elif operation == "replace_goal":
                if not replacement:
                    reason = "replacement_missing"
                elif str(section.get("goal") or "") == replacement:
                    target_status, reason = "already_satisfied", "goal_already_matches"
                else:
                    section["goal"] = replacement
                    target_status, reason = "changed", "goal_replaced"
            elif operation == "replace_aggregate_claim_with_per_paper_boundaries":
                aggregate_claims = [
                    claim for claim in claims
                    if any(marker in claim.casefold() for marker in ("共同指向", "共同说明", "aggregate", "概括性", "gap claim"))
                ]
                per_paper_claims: list[str] = []
                for paper_key in section.get("paper_keys") or ():
                    view = view_by_key.get(str(paper_key))
                    if view is None:
                        continue
                    evidence = [*list(view.limitations), *list(view.research_gaps), *list(view.future_directions)]
                    if evidence:
                        per_paper_claims.append(f"{paper_key} 的作者自陈边界：" + "；".join(evidence))
                if not aggregate_claims:
                    try:
                        support_changed = _reconcile_claim_support(section, claims)
                    except (TypeError, ValueError):
                        reason = "claim_support_malformed"
                    else:
                        if support_changed:
                            target_status, reason = "changed", "orphan_claim_support_removed"
                        else:
                            target_status, reason = "already_satisfied", "no_aggregate_claim_remains"
                elif per_paper_claims:
                    revised_claims = [claim for claim in claims if claim not in aggregate_claims] + per_paper_claims
                    if _replace_claims_and_reconcile_support(section, revised_claims):
                        target_status, reason = "changed", "aggregate_claim_replaced_with_per_paper_boundaries"
                    else:
                        reason = "claim_support_malformed"
                else:
                    reason = "supporting_boundary_evidence_missing"
            elif operation == "remove_claim":
                if not replacement:
                    reason = "claim_replacement_missing"
                elif replacement not in claims:
                    try:
                        support_changed = _reconcile_claim_support(section, claims)
                    except (TypeError, ValueError):
                        reason = "claim_support_malformed"
                    else:
                        if support_changed:
                            target_status, reason = "changed", "orphan_claim_support_removed"
                        else:
                            target_status, reason = "already_satisfied", "claim_already_absent"
                else:
                    kept = [claim for claim in claims if claim != replacement]
                    if not kept:
                        reason = "cannot_delete_last_supported_claim"
                    elif _replace_claims_and_reconcile_support(section, kept):
                        target_status, reason = "changed", "claim_removed"
                    else:
                        reason = "claim_support_malformed"
            else:
                reason = "unsupported_or_missing_operation"

            after_hash = hash_json(section)
            record: dict[str, Any] = {
                "issue_id": issue_id,
                "candidate_id": candidate_id,
                "recommendation": recommendation,
                "section_id": section_id,
                "operation": operation or "unknown",
                "status": target_status,
                "reason": reason,
                "before_hash": before_hash,
                "after_hash": after_hash,
            }
            if target_status == "changed":
                section["revision_lineage"] = {
                    "issue_id": issue_id,
                    "candidate_id": candidate_id,
                    "parent_candidate_hash": parent_candidate_hash,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                }
                record["after_hash"] = hash_json(section)
                record["targeted_verification"] = "candidate_structure_and_evidence_recheck_pending"
            operation_records.append(record)
            target_results.append({
                "section_id": section_id,
                "status": target_status,
                "reason": reason,
            })

        required_resolved = bool(target_results) and all(
            item.get("status") in {"changed", "already_satisfied"}
            for item in target_results
        )
        if required_resolved:
            revision_records.extend(operation_records)
        else:
            unresolved_revisions.append({
                "issue_id": issue_id,
                "candidate_id": candidate_id,
                "recommendation": recommendation,
                "target_section_ids": sorted(target_ids),
                "operation": operation or "unknown",
                "status": "needs_manual_review",
                "target_results": target_results,
            })

    return {
        "revised_sections": revised_sections,
        "accepted_recommendations": accepted_recommendations,
        "revision_records": revision_records,
        "unresolved_revisions": unresolved_revisions,
    }


def _reconcile_claim_support(section: dict[str, Any], claims: Sequence[str]) -> bool:
    """Remove support rows whose exact claim no longer appears in the section."""

    if "claim_support" not in section:
        return False
    raw_support = section.get("claim_support")
    if not isinstance(raw_support, Sequence) or isinstance(raw_support, (str, bytes)):
        raise TypeError("claim_support must be a sequence of support rows")
    if any(not isinstance(row, Mapping) for row in raw_support):
        raise TypeError("claim_support rows must be objects")
    claim_set = set(claims)
    kept = [
        row for row in raw_support
        if str(row.get("claim") or "").strip() in claim_set
    ]
    if len(kept) == len(raw_support):
        return False
    section["claim_support"] = [dict(row) for row in kept]
    return True


def _replace_claims_and_reconcile_support(
    section: dict[str, Any],
    claims: Sequence[str],
) -> bool:
    """Set claims and support atomically; return false for malformed support."""

    prior_claims = deepcopy(section.get("claims")) if "claims" in section else None
    claims_present = "claims" in section
    prior_support = deepcopy(section.get("claim_support")) if "claim_support" in section else None
    support_present = "claim_support" in section
    section["claims"] = list(claims)
    try:
        _reconcile_claim_support(section, claims)
    except (TypeError, ValueError):
        if claims_present:
            section["claims"] = prior_claims
        else:
            section.pop("claims", None)
        if support_present:
            section["claim_support"] = prior_support
        else:
            section.pop("claim_support", None)
        return False
    return True


__all__ = ["apply_selected_revision"]

"""Source-bound review-group and critical decision views for overflow arbitration.

This module compiles provider-visible facts only from the READY legacy
arbitration inputs, the caller's verified Writer source inventory, and the
source-derived CandidateOutputScopeV1. It never infers empirical direction or
per-field criticality from prose.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from outline.candidate_output_scope import CandidateOutputScopeV1
from outline.v3_models import compute_v3_hash
from services.writer_source_inventory import VerifiedWriterSourceInventoryV1

GROUP_MAP_SCHEMA = "arbitration_review_group_map/v1"
CRITICAL_VIEW_SCHEMA = "arbitration_critical_view_set/v1"
DECISION_VIEW_SCHEMA = "arbitration_decision_view/v1"
CRITICAL_VIEW_WIRE_SCHEMA = "arbitration_critical_view_wire/v1"


class ArbitrationViewError(ValueError):
    """A review group or source-bound decision view is incomplete or foreign."""


def _used_assertions_by_candidate(
    candidate_scope: CandidateOutputScopeV1,
    candidate_contents: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(candidate_contents, Mapping) or not candidate_contents:
        raise ArbitrationViewError("candidate contents are required to derive used assertions")
    candidate_ids = _ordered_unique_ids(list(candidate_contents), "candidate_contents keys")
    if not candidate_ids:
        raise ArbitrationViewError("candidate contents are required to derive used assertions")
    groups_by_id = {group.claim_group_id: group for group in candidate_scope.claim_groups}
    slots_by_group: dict[str, set[str]] = {group_id: set() for group_id in groups_by_id}
    for slot in candidate_scope.claim_slots:
        if slot.claim_group_id not in groups_by_id:
            raise ArbitrationViewError(f"claim slot {slot.claim_slot_id} has a foreign claim group")
        slots_by_group[slot.claim_group_id].add(slot.claim_slot_id)

    result: dict[str, dict[str, list[str]]] = {}
    for candidate_id in candidate_ids:
        content = candidate_contents[candidate_id]
        if not isinstance(content, Mapping):
            raise ArbitrationViewError(f"candidate {candidate_id} content must be an object")
        if content.get("candidate_id") not in (None, candidate_id):
            raise ArbitrationViewError(f"candidate {candidate_id} content has a foreign candidate ID")
        try:
            candidate_scope.validate(content)
        except (TypeError, ValueError, KeyError) as exc:
            raise ArbitrationViewError(
                f"candidate {candidate_id} is not source-closed under CandidateOutputScopeV1"
            ) from exc
        used_slot_ids = {
            str(support.get("claim_slot_id") or "")
            for section in content.get("sections") or ()
            for support in section.get("claim_support") or ()
        }
        used_by_task: dict[str, set[str]] = {task_id: set() for task_id in candidate_scope.task_ids}
        for group_id, group in groups_by_id.items():
            group_slot_ids = slots_by_group[group_id]
            observed = group_slot_ids & used_slot_ids
            if observed and observed != group_slot_ids:
                raise ArbitrationViewError(
                    f"candidate {candidate_id} uses an incomplete support closure for {group_id}"
                )
            if observed:
                used_by_task[group.task_id].add(group.synthesis_claim_id)
        result[candidate_id] = {
            task_id: sorted(assertion_ids)
            for task_id, assertion_ids in sorted(used_by_task.items())
        }
    return result


def build_arbitration_review_group_map(
    candidate_scope: CandidateOutputScopeV1,
    review_intent: Any,
    critique_issues: Sequence[Mapping[str, Any]],
    *,
    candidate_contents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile exactly W=T+M+I explicit review groups.

    Required comparison IDs are deterministic hashes of the normalized
    semantic triple (paper keys, question, relation type), because ReviewIntent
    currently carries no comparison ID. Each comparison must uniquely match a
    source-derived relation group in CandidateOutputScopeV1.
    """

    if type(candidate_scope) is not CandidateOutputScopeV1:
        raise ArbitrationViewError("candidate scope must be CandidateOutputScopeV1")
    intent = review_intent.to_dict() if callable(getattr(review_intent, "to_dict", None)) else review_intent
    if not isinstance(intent, Mapping):
        raise ArbitrationViewError("review intent must be an object")
    raw_comparisons = intent.get("required_comparisons") or []
    if not isinstance(raw_comparisons, list):
        raise ArbitrationViewError("required comparisons must be an array")
    issues = _ordered_issue_rows(critique_issues)
    candidate_used_assertions = (
        _used_assertions_by_candidate(candidate_scope, candidate_contents)
        if candidate_contents is not None else None
    )

    groups: list[dict[str, Any]] = []
    group_by_id: dict[str, dict[str, Any]] = {}
    claim_group_ids = {group.claim_group_id for group in candidate_scope.claim_groups}
    if len(claim_group_ids) != len(candidate_scope.claim_groups):
        raise ArbitrationViewError("candidate scope repeats a source claim group ID")
    claim_slot_ids = {slot.claim_slot_id for slot in candidate_scope.claim_slots}
    if len(claim_slot_ids) != len(candidate_scope.claim_slots):
        raise ArbitrationViewError("candidate scope repeats a source claim slot ID")
    group_to_slots: dict[str, list[str]] = {group_id: [] for group_id in claim_group_ids}
    groups_by_task: dict[str, list[str]] = {task_id: [] for task_id in candidate_scope.task_ids}
    assertions_by_task: dict[str, list[str]] = {task_id: [] for task_id in candidate_scope.task_ids}

    for group in candidate_scope.claim_groups:
        if group.task_id not in groups_by_task:
            raise ArbitrationViewError(
                f"claim group {group.claim_group_id} has no declared task/topic scope"
            )
        groups_by_task[group.task_id].append(group.claim_group_id)
        if candidate_used_assertions is None:
            # Preserve the original all-available behavior for read-only
            # callers without candidate contents. Production compilation
            # supplies actual contents and records their exact used union.
            assertions_by_task[group.task_id].append(group.synthesis_claim_id)
        else:
            assertions_by_task[group.task_id].extend(
                assertion_id
                for candidate_rows in candidate_used_assertions.values()
                for assertion_id in candidate_rows[group.task_id]
            )
    for slot in candidate_scope.claim_slots:
        if slot.claim_group_id not in group_to_slots or slot.task_id not in groups_by_task:
            raise ArbitrationViewError(f"claim slot {slot.claim_slot_id} has a foreign group/task binding")
        if slot.claim_group_id not in groups_by_task[slot.task_id]:
            raise ArbitrationViewError(f"claim slot {slot.claim_slot_id} disagrees with its group task")
        group_to_slots[slot.claim_group_id].append(slot.claim_slot_id)
    if set(group_to_slots) != claim_group_ids or set(claim_slot_ids) != {
        slot_id for rows in group_to_slots.values() for slot_id in rows
    }:
        raise ArbitrationViewError("claim group/support scope is not exact")

    for task_id in candidate_scope.task_ids:
        row = {
            "group_id": f"task:{task_id}",
            "group_kind": "task_topic",
            "task_id": task_id,
            "source_scope_ids": sorted(groups_by_task[task_id]),
            "required_comparison_ids": [],
            "typed_issue_ids": [],
            "used_assertion_ids": sorted(set(assertions_by_task[task_id])),
        }
        if candidate_used_assertions is not None:
            row["used_assertion_ids_by_candidate"] = {
                candidate_id: list(candidate_rows[task_id])
                for candidate_id, candidate_rows in sorted(candidate_used_assertions.items())
            }
        _append_group(groups, group_by_id, row)

    relation_groups = [
        group for group in candidate_scope.claim_groups
        if group.claim_kind == "model_authored_relation_comparison_slot"
    ]
    comparison_ids: list[str] = []
    comparison_matches: dict[str, str] = {}
    for index, raw in enumerate(raw_comparisons):
        if not isinstance(raw, Mapping):
            raise ArbitrationViewError(f"required comparison {index} must be an object")
        paper_keys = _string_ids(raw.get("paper_keys"), f"required_comparisons[{index}].paper_keys", required=True)
        if len(paper_keys) < 2:
            raise ArbitrationViewError(f"required comparison {index} needs at least two distinct paper keys")
        question = _nonempty_string(raw.get("question"), f"required_comparisons[{index}].question").strip()
        relation_type = str(raw.get("relation_type") or "").strip()
        normalized = {
            "paper_keys": sorted(paper_keys),
            "question": question,
            "relation_type": relation_type,
        }
        comparison_id = "comparison:" + compute_v3_hash(normalized)[:24]
        if comparison_id in comparison_ids:
            raise ArbitrationViewError("required comparisons repeat a semantic identity")
        matches = [
            group for group in relation_groups
            if set(group.paper_keys) == set(normalized["paper_keys"])
            and _relation_contract_matches(
                dict(group.task_contract) if not isinstance(group.task_contract, Mapping) else group.task_contract,
                normalized,
            )
        ]
        if len(matches) != 1:
            raise ArbitrationViewError(
                f"required comparison {comparison_id} has {len(matches)} matching source relation groups; expected one"
            )
        matched_group_id = matches[0].claim_group_id
        comparison_ids.append(comparison_id)
        comparison_matches[comparison_id] = matched_group_id
        row = {
            "group_id": comparison_id,
            "group_kind": "required_comparison",
            "source_scope_ids": [matched_group_id],
            "required_comparison_ids": [comparison_id],
            "typed_issue_ids": [],
            "used_assertion_ids": [matches[0].synthesis_claim_id],
            "comparison": normalized,
            "source_relation_group_id": matched_group_id,
        }
        _append_group(groups, group_by_id, row)

    issue_targets: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_id = str(issue["issue_id"])
        row = {
            "group_id": f"issue:{issue_id}",
            "group_kind": "typed_issue",
            "source_scope_ids": list(issue["target_ids"]),
            "required_comparison_ids": [],
            "typed_issue_ids": [issue_id],
            "used_assertion_ids": [],
            "issue_severity": str(issue["severity"]),
            "issue_scope": str(issue["scope"]),
            "candidate_id": str(issue.get("candidate_id") or ""),
        }
        _append_group(groups, group_by_id, row)
        issue_targets[issue_id] = copy.deepcopy(dict(issue))

    expected_groups = len(candidate_scope.task_ids) + len(raw_comparisons) + len(issues)
    if len(groups) != expected_groups:
        raise ArbitrationViewError("review-group map violates W=T+M+I")
    body = {
        "schema_version": GROUP_MAP_SCHEMA,
        "task_count": len(candidate_scope.task_ids),
        "comparison_count": len(raw_comparisons),
        "typed_issue_count": len(issues),
        "group_count": len(groups),
        "groups": groups,
        "claim_group_task_bindings": {
            key: next(group.task_id for group in candidate_scope.claim_groups if group.claim_group_id == key)
            for key in sorted(claim_group_ids)
        },
        "claim_group_assertion_bindings": {
            group.claim_group_id: group.synthesis_claim_id
            for group in sorted(candidate_scope.claim_groups, key=lambda item: item.claim_group_id)
        },
        "claim_group_slot_bindings": {key: sorted(value) for key, value in sorted(group_to_slots.items())},
        "comparison_relation_bindings": dict(sorted(comparison_matches.items())),
        "issue_rows": issue_targets,
    }
    body["group_map_hash"] = compute_v3_hash(body)
    return body


def compile_arbitration_critical_views(
    *,
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    candidate_contents: Mapping[str, Mapping[str, Any]],
    legacy_ledger: Mapping[str, Any],
    candidate_scope: CandidateOutputScopeV1,
    source_inventory: VerifiedWriterSourceInventoryV1,
    review_group_map: Mapping[str, Any],
) -> dict[str, Any]:
    """Join critical source facts to exact candidate/support/source identities.

    Ordinary task assertions are covered by the review-group map, not copied
    into the literal-critical set. Required comparison supports, explicit
    blocking issue targets, and actual interpretation-dependency qualifiers
    are literal-critical. The current verified inventory has no typed source
    direction or per-field ``critical_for`` field; those remain explicitly
    unknown/absent.
    """

    if type(candidate_scope) is not CandidateOutputScopeV1:
        raise ArbitrationViewError("candidate scope must be CandidateOutputScopeV1")
    if type(source_inventory) is not VerifiedWriterSourceInventoryV1 or not source_inventory.is_verified:
        raise ArbitrationViewError("critical views require the actual verified Writer source inventory")
    ids = _ordered_unique_ids(candidate_ids, "candidate_ids")
    if not ids:
        raise ArbitrationViewError("critical views require at least one eligible candidate")
    hashes = _exact_hash_map(candidate_hashes, ids, "candidate_hashes")
    if set(candidate_contents) != set(ids):
        raise ArbitrationViewError("candidate contents must exactly match eligible candidate IDs")
    _verify_source_scope_binding(candidate_scope, source_inventory)
    canonical_ledger = _validated_ledger(legacy_ledger)
    if canonical_ledger.get("candidate_ids") != list(ids):
        raise ArbitrationViewError("READY legacy ledger candidate scope differs from eligible candidates")
    if canonical_ledger.get("candidate_hashes") != hashes:
        raise ArbitrationViewError("READY legacy ledger candidate hashes differ from eligible candidate hashes")
    if canonical_ledger.get("candidate_output_scope_hash") not in (None, candidate_scope.content_hash):
        raise ArbitrationViewError("READY legacy ledger candidate-output scope binding is stale")
    provenance = canonical_ledger.get("provenance_hashes")
    if isinstance(provenance, Mapping) and provenance.get("candidate_output_scope") not in (
        None, candidate_scope.content_hash,
    ):
        raise ArbitrationViewError("READY legacy hierarchy provenance has a stale candidate-output scope hash")
    if not isinstance(review_group_map, Mapping) or review_group_map.get("schema_version") != GROUP_MAP_SCHEMA:
        raise ArbitrationViewError("review group map schema is invalid")
    expected_group_hash = compute_v3_hash({key: value for key, value in review_group_map.items() if key != "group_map_hash"})
    if review_group_map.get("group_map_hash") != expected_group_hash:
        raise ArbitrationViewError("review group map hash is invalid")
    _validate_group_map_source_scope(
        review_group_map, candidate_scope, canonical_ledger, candidate_contents,
    )

    section_index = canonical_ledger.get("candidate_section_index")
    if not isinstance(section_index, Mapping) or set(section_index) != set(ids):
        raise ArbitrationViewError("READY legacy ledger section index does not match candidate scope")
    slot_rows = {slot.claim_slot_id: slot for slot in candidate_scope.claim_slots}
    claim_groups = {group.claim_group_id: group for group in candidate_scope.claim_groups}
    source_indexes = _source_indexes(source_inventory)
    issue_rows = review_group_map.get("issue_rows")
    comparison_bindings = review_group_map.get("comparison_relation_bindings")
    claim_group_slot_bindings = review_group_map.get("claim_group_slot_bindings")
    if not isinstance(issue_rows, Mapping) or not isinstance(comparison_bindings, Mapping):
        raise ArbitrationViewError("review group map is missing typed issue/comparison joins")
    if not isinstance(claim_group_slot_bindings, Mapping):
        raise ArbitrationViewError("review group map is missing claim-slot bindings")

    # First validate all candidate/source rows and bind every source slot to one
    # exact section index row; no response prose participates in this join.
    slot_bindings: dict[str, dict[str, dict[str, Any]]] = {candidate_id: {} for candidate_id in ids}
    section_by_candidate: dict[str, dict[str, Mapping[str, Any]]] = {}
    index_by_candidate: dict[str, dict[str, Mapping[str, Any]]] = {}
    for candidate_id in ids:
        content = candidate_contents[candidate_id]
        candidate_scope.validate(content)
        index_rows = section_index[candidate_id]
        if not isinstance(index_rows, list):
            raise ArbitrationViewError(f"legacy section index for {candidate_id} is malformed")
        index_by_candidate[candidate_id] = _unique_rows(index_rows, "section_id", f"{candidate_id} section index")
        raw_sections = content.get("sections")
        if not isinstance(raw_sections, list):
            raise ArbitrationViewError(f"READY candidate sections for {candidate_id} must be an array")
        section_by_candidate[candidate_id] = _unique_rows(raw_sections, "section_id", f"{candidate_id} sections")
        if set(index_by_candidate[candidate_id]) != set(section_by_candidate[candidate_id]):
            raise ArbitrationViewError(f"legacy ledger sections differ from READY {candidate_id} source sections")
        for section_id, section in section_by_candidate[candidate_id].items():
            index_row = index_by_candidate[candidate_id][section_id]
            section_hash = compute_v3_hash(dict(section))
            if index_row.get("section_hash") != section_hash:
                raise ArbitrationViewError(f"legacy section hash differs for {candidate_id}.{section_id}")
        for section in raw_sections:
            for support in section.get("claim_support") or ():
                slot_id = str(support.get("claim_slot_id") or "")
                slot = slot_rows.get(slot_id)
                if slot is None or slot_id in slot_bindings[candidate_id]:
                    raise ArbitrationViewError(f"{candidate_id} has a foreign or duplicate source claim slot")
                _validate_support_binding(support, slot, f"{candidate_id}.{slot_id}")
                claim_group = claim_groups.get(slot.claim_group_id)
                if claim_group is None:
                    raise ArbitrationViewError(f"{slot_id} refers to an unknown source claim group")
                slot_bindings[candidate_id][slot_id] = {
                    "support": copy.deepcopy(dict(support)),
                    "section_id": str(section["section_id"]),
                    "section_hash": str(index_by_candidate[candidate_id][str(section["section_id"])]["section_hash"]),
                    "claim_group_id": slot.claim_group_id,
                    "task_id": slot.task_id,
                }
        if not slot_bindings[candidate_id]:
            raise ArbitrationViewError(f"{candidate_id} has no source-closed claim slots")

    for comparison_id, claim_group_id in comparison_bindings.items():
        required_slot_ids = set(claim_group_slot_bindings.get(claim_group_id, ()))
        if not required_slot_ids:
            raise ArbitrationViewError(f"required comparison {comparison_id} has no source support slots")
        for candidate_id in ids:
            missing_slot_ids = sorted(required_slot_ids - set(slot_bindings[candidate_id]))
            if missing_slot_ids:
                raise ArbitrationViewError(
                    f"required comparison {comparison_id} is not fully used by {candidate_id}; "
                    f"missing source support slot {missing_slot_ids[0]}"
                )

    # Determine exact critical source-ID subsets per claim slot. A blocking
    # critique target is literal-critical only after it resolves to a known
    # candidate section/claim/slot/source ID. Non-blocking issues still get a
    # W issue-summary group but do not cause ordinary support to be echoed.
    critical_by_candidate: dict[str, dict[str, dict[str, Any]]] = {
        candidate_id: {
            slot_id: {"reasons": set(), "group_ids": set(), "claim_ids": set(), "evidence_ids": set(), "field_ids": set()}
            for slot_id in slot_rows
        }
        for candidate_id in ids
    }
    for comparison_id, claim_group_id in comparison_bindings.items():
        for slot_id in claim_group_slot_bindings.get(claim_group_id, ()):
            if slot_id not in slot_rows:
                raise ArbitrationViewError(f"comparison {comparison_id} references a foreign claim slot")
            for candidate_id in ids:
                _mark_critical_slot(
                    critical_by_candidate[candidate_id], slot_id, reason=f"required_comparison:{comparison_id}",
                    group_id=comparison_id, slot=slot_rows[slot_id], include_full_closure=True,
                )

    blocking_issue_groups = {
        str(row["issue_id"]): row
        for row in issue_rows.values()
        if isinstance(row, Mapping) and str(row.get("severity") or "") == "blocking"
    }
    for issue_id, issue in blocking_issue_groups.items():
        issue_group_id = f"issue:{issue_id}"
        issue_candidate = str(issue.get("candidate_id") or "")
        if issue_candidate and issue_candidate not in hashes:
            raise ArbitrationViewError(f"blocking issue {issue_id} has a foreign candidate binding")
        if issue_candidate and issue.get("parent_candidate_hash") != hashes[issue_candidate]:
            raise ArbitrationViewError(f"blocking issue {issue_id} has a stale candidate hash binding")
        targets = {str(value) for value in issue.get("target_ids") or ()}
        resolved: set[tuple[str, str]] = set()
        for candidate_id in ids:
            if issue_candidate and candidate_id != issue_candidate:
                continue
            for slot_id, binding in slot_bindings[candidate_id].items():
                slot = slot_rows[slot_id]
                claim_group = claim_groups[slot.claim_group_id]
                support = binding["support"]
                candidate_source_ids = {
                    slot_id, slot.claim_group_id, slot.synthesis_claim_id, slot.primary_claim_id,
                    *slot.source_claim_ids, *slot.evidence_ids, *slot.source_field_ids,
                }
                section_id = str(binding["section_id"])
                section = section_by_candidate[candidate_id][section_id]
                claim_ids = _section_claim_ids(section)
                matched = (
                    issue.get("scope") == "global"
                    or issue.get("scope") == "candidate"
                    or (issue.get("scope") == "section" and section_id in targets)
                    or (issue.get("scope") == "claim" and bool(targets & (candidate_source_ids | claim_ids)))
                    or bool(targets & candidate_source_ids)
                )
                if matched:
                    _mark_critical_slot(
                        critical_by_candidate[candidate_id], slot_id, reason=f"blocking_issue:{issue_id}",
                        group_id=issue_group_id, slot=slot, include_full_closure=True,
                    )
                    resolved.add((candidate_id, slot_id))
        # A section/claim issue must resolve to a source slot. Candidate/global
        # issues can legitimately target a section with no source rows only if
        # the verified scope itself is empty, which CandidateOutputScope
        # forbids for production candidates.
        if str(issue.get("scope") or "") in {"section", "claim"} and not resolved:
            raise ArbitrationViewError(f"blocking issue {issue_id} has no source-bound claim/support target")

    # Required interpretation dependencies make their actual qualifier
    # claims/evidence/fields literal-critical, even when the surrounding task
    # group is otherwise an ordinary topic summary.
    for candidate_id in ids:
        for slot_id, binding in slot_bindings[candidate_id].items():
            slot = slot_rows[slot_id]
            dependencies = source_indexes[slot.paper_key]["dependencies"].get(slot.primary_claim_id, ())
            for dependency in dependencies:
                if dependency.scope == "unresolved":
                    raise ArbitrationViewError(f"{slot.primary_claim_id} has unresolved qualifier source scope")
                qualifier_claim_ids = set(dependency.required_source_claim_ids)
                qualifier_evidence_ids = set(dependency.required_evidence_ids)
                qualifier_field_ids = set(dependency.required_source_field_ids)
                if not qualifier_claim_ids and not qualifier_evidence_ids and not qualifier_field_ids:
                    continue
                _mark_critical_slot(
                    critical_by_candidate[candidate_id], slot_id,
                    reason=f"required_qualifier:{slot.primary_claim_id}",
                    group_id=f"task:{slot.task_id}", slot=slot, include_full_closure=False,
                    claim_ids=qualifier_claim_ids,
                    evidence_ids=qualifier_evidence_ids,
                    field_ids=qualifier_field_ids,
                )

    views: list[dict[str, Any]] = []
    noncritical_by_id: dict[str, dict[str, Any]] = {}
    for candidate_id in ids:
        for slot_id in sorted(slot_rows):
            slot = slot_rows[slot_id]
            binding = slot_bindings[candidate_id].get(slot_id)
            critical = critical_by_candidate[candidate_id][slot_id]
            group_ids = set(critical["group_ids"])
            if critical["reasons"]:
                if binding is None:
                    raise ArbitrationViewError(
                        f"unused optional source slot {candidate_id}.{slot_id} cannot be marked literal-critical"
                    )
                # The view is candidate-specific and bound to the exact READY
                # section, slot, inventory and source literal rows.
                source_rows = _selected_source_rows(
                    source_indexes[slot.paper_key],
                    slot,
                    critical,
                )
                source_bindings = {
                    "source_inventory_artifact_id": source_inventory.artifact_id,
                    "source_inventory_artifact_hash": source_inventory.artifact_hash,
                    "source_inventory_content_hash": source_inventory.content_hash,
                    "paper_key": slot.paper_key,
                    "study_id": _verified_study_id(slot, source_rows),
                    "study_status": "known" if _verified_study_id(slot, source_rows) else "unknown",
                    "claim_slot_id": slot_id,
                    "claim_group_id": slot.claim_group_id,
                    "primary_claim_id": slot.primary_claim_id,
                    "source_claim_ids": sorted(row["claim_id"] for row in source_rows["claims"]),
                    # IDs bind the source closure even when a qualifier-only
                    # view does not need to repeat the ordinary evidence text.
                    "evidence_ids": list(slot.evidence_ids),
                    "source_field_ids": sorted(row["source_field_id"] for row in source_rows["fields"]),
                    "source_locator_bindings": [
                        {"source_claim_id": row["claim_id"], "source_locator": row["source_locator"]}
                        for row in source_rows["claims"] if row["source_locator"]
                    ],
                    "source_relation_direction": {"status": "unknown", "value": None},
                    "source_disagreement_status": {"status": "unknown", "value": None},
                }
                critical_reasons = sorted(critical["reasons"])
                literal_qualifier_values = [
                    {
                        "source_claim_id": row["claim_id"],
                        "literal_value": row["text"],
                    }
                    for row in source_rows["claims"]
                    if any(reason.startswith("required_qualifier:") for reason in critical_reasons)
                ]
                literal_qualifier_values.extend(
                    {
                        "source_field_id": row["source_field_id"],
                        "literal_value": row["source_value"],
                    }
                    for row in source_rows["fields"]
                    if any(reason.startswith("required_qualifier:") for reason in critical_reasons)
                )
                literal_details = {
                    "source_claims": source_rows["claims"],
                    "evidence": source_rows["evidence"],
                    "source_fields": source_rows["fields"],
                }
                view_core = {
                    "schema_version": DECISION_VIEW_SCHEMA,
                    "candidate_id": candidate_id,
                    "candidate_hash": hashes[candidate_id],
                    "candidate_section_id": binding["section_id"],
                    "candidate_section_hash": binding["section_hash"],
                    "claim_slot_id": slot_id,
                    "claim_group_id": slot.claim_group_id,
                    "group_ids": sorted(group_ids),
                    "comparison_group_ids": sorted(group_id for group_id in group_ids if group_id.startswith("comparison:")),
                    "finding_id": slot.primary_claim_id,
                    "criticality_reasons": critical_reasons,
                    "source_bindings": source_bindings,
                    "literal_qualifier_values": literal_qualifier_values,
                    "literal_critical_detail": literal_details,
                }
                view_id = "decision-view:" + compute_v3_hash(view_core)[:24]
                row = {**view_core, "decision_view_id": view_id}
                row["decision_view_hash"] = compute_v3_hash(row)
                views.append(row)
            # Index every source fact that is not physically literal-critical,
            # including noncritical siblings inside a slot that also has a
            # qualifier/comparison view. Detail-required can therefore name
            # exact facts without deferring any critical literal.
            critical_claim_ids = set(critical["claim_ids"])
            critical_evidence_ids = set(critical["evidence_ids"])
            critical_field_ids = set(critical["field_ids"])
            paper_index = source_indexes[slot.paper_key]
            for fact_kind, fact_ids, critical_ids, source_map in (
                ("source_claim", slot.source_claim_ids, critical_claim_ids, paper_index["claims"]),
                ("evidence", slot.evidence_ids, critical_evidence_ids, paper_index["evidence"]),
                ("source_field", slot.source_field_ids, critical_field_ids, paper_index["fields"]),
            ):
                for fact_id in fact_ids:
                    if fact_id in critical_ids:
                        continue
                    source_row = source_map.get(fact_id)
                    if source_row is None:
                        raise ArbitrationViewError(f"noncritical source fact {fact_id} is absent from verified inventory")
                    fact_payload = _source_fact_payload(fact_kind, source_row)
                    fact_hash = compute_v3_hash(fact_payload)
                    identity = {
                        "candidate_id": candidate_id,
                        "candidate_hash": hashes[candidate_id],
                        "fact_kind": fact_kind,
                        "fact_id": fact_id,
                        "paper_key": slot.paper_key,
                        "source_fact_hash": fact_hash,
                        "source_inventory_artifact_id": source_inventory.artifact_id,
                        "source_inventory_artifact_hash": source_inventory.artifact_hash,
                    }
                    view_id = "decision-view:" + compute_v3_hash(identity)[:24]
                    row = noncritical_by_id.get(view_id)
                    if row is None:
                        row = {
                            "decision_view_id": view_id,
                            **identity,
                            "claim_slot_ids": [],
                            "claim_group_ids": [],
                            "view_hash": compute_v3_hash(identity),
                            "source_binding_hash": compute_v3_hash(fact_payload),
                        }
                        noncritical_by_id[view_id] = row
                    row["claim_slot_ids"] = sorted(set(row["claim_slot_ids"]) | {slot_id})
                    row["claim_group_ids"] = sorted(set(row["claim_group_ids"]) | {slot.claim_group_id})

    views.sort(key=lambda row: (ids.index(row["candidate_id"]), row["claim_slot_id"], row["decision_view_id"]))
    noncritical_index = sorted(
        noncritical_by_id.values(),
        key=lambda row: (ids.index(row["candidate_id"]), row["fact_kind"], row["fact_id"], row["decision_view_id"]),
    )
    source_slot_scope_index = [
        {
            "candidate_id": candidate_id,
            "candidate_hash": hashes[candidate_id],
            "claim_slot_id": slot.claim_slot_id,
            "claim_group_id": slot.claim_group_id,
            "paper_key": slot.paper_key,
            "primary_claim_id": slot.primary_claim_id,
            "source_claim_ids": list(slot.source_claim_ids),
            "evidence_ids": list(slot.evidence_ids),
            "source_field_ids": list(slot.source_field_ids),
            "usage_status": (
                "used" if slot.claim_slot_id in slot_bindings[candidate_id] else "not_used_optional"
            ),
        }
        for candidate_id in ids
        for slot in sorted(candidate_scope.claim_slots, key=lambda item: item.claim_slot_id)
    ]
    body = {
        "schema_version": CRITICAL_VIEW_SCHEMA,
        "candidate_ids": list(ids),
        "candidate_hashes": hashes,
        "candidate_output_scope_hash": candidate_scope.content_hash,
        "source_inventory_binding": {
            "artifact_id": source_inventory.artifact_id,
            "artifact_hash": source_inventory.artifact_hash,
            "content_hash": source_inventory.content_hash,
        },
        "review_group_map_hash": review_group_map["group_map_hash"],
        "critical_views": views,
        "noncritical_fact_index": noncritical_index,
        "source_slot_scope_index": source_slot_scope_index,
        "critical_view_count": len(views),
        "noncritical_fact_count": len(noncritical_index),
        "direction_semantics": "source direction absent from verified inventory is unknown; assessed direction is model-authored in a new response",
        "critical_for_semantics": "per-field critical_for is absent from the current verified inventory and was not inferred",
    }
    body["view_set_hash"] = compute_v3_hash(body)
    return body


def validate_arbitration_view_set(
    view_set: Mapping[str, Any],
    *,
    expected_view_set_hash: str,
) -> dict[str, Any]:
    """Recheck schema and semantic hash before using compiled views on wire."""

    if not isinstance(view_set, Mapping) or view_set.get("schema_version") != CRITICAL_VIEW_SCHEMA:
        raise ArbitrationViewError("critical view set schema is invalid")
    if not _is_sha256(expected_view_set_hash):
        raise ArbitrationViewError("expected READY critical view-set hash must be a lowercase SHA-256")
    if view_set.get("view_set_hash") != expected_view_set_hash:
        raise ArbitrationViewError("critical view set differs from the pinned READY view-set hash")
    body = {key: copy.deepcopy(value) for key, value in view_set.items() if key != "view_set_hash"}
    if view_set.get("view_set_hash") != compute_v3_hash(body):
        raise ArbitrationViewError("critical view set hash is invalid")
    ids = _ordered_unique_ids(view_set.get("candidate_ids"), "critical view candidate_ids")
    hashes = _exact_hash_map(view_set.get("candidate_hashes"), ids, "critical view candidate_hashes")
    seen_views: set[str] = set()
    for row in view_set.get("critical_views") or ():
        if not isinstance(row, Mapping):
            raise ArbitrationViewError("critical decision view row is invalid")
        view_id = _nonempty_string(row.get("decision_view_id"), "decision_view_id")
        if view_id in seen_views:
            raise ArbitrationViewError("critical view set repeats a decision_view_id")
        seen_views.add(view_id)
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id not in hashes or row.get("candidate_hash") != hashes[candidate_id]:
            raise ArbitrationViewError("critical decision view has a foreign candidate/hash binding")
        if row.get("schema_version") != DECISION_VIEW_SCHEMA:
            raise ArbitrationViewError("critical decision view schema is invalid")
        view_core = {key: copy.deepcopy(value) for key, value in row.items() if key != "decision_view_hash"}
        if row.get("decision_view_hash") != compute_v3_hash(view_core):
            raise ArbitrationViewError(f"critical decision view {view_id} hash is invalid")
        source_bindings = row.get("source_bindings")
        if not isinstance(source_bindings, Mapping):
            raise ArbitrationViewError("critical view source bindings are malformed")
        if source_bindings.get("source_relation_direction") != {"status": "unknown", "value": None}:
            raise ArbitrationViewError("source direction must remain explicitly unknown until provided by source authority")
        if source_bindings.get("source_disagreement_status") != {"status": "unknown", "value": None}:
            raise ArbitrationViewError("source disagreement status must remain unknown unless source authority provides it")
    noncritical_rows = view_set.get("noncritical_fact_index")
    if not isinstance(noncritical_rows, list):
        raise ArbitrationViewError("noncritical fact index must be an array")
    seen_noncritical: set[str] = set()
    allowed_kinds = {"source_claim", "evidence", "source_field"}
    for index, row in enumerate(noncritical_rows):
        if not isinstance(row, Mapping):
            raise ArbitrationViewError(f"noncritical fact row {index} is malformed")
        view_id = _nonempty_string(row.get("decision_view_id"), f"noncritical_fact_index[{index}].decision_view_id")
        if view_id in seen_noncritical or view_id in seen_views:
            raise ArbitrationViewError("decision-view identity is duplicated across critical/noncritical sets")
        seen_noncritical.add(view_id)
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id not in hashes or row.get("candidate_hash") != hashes[candidate_id]:
            raise ArbitrationViewError("noncritical fact row has a foreign candidate/hash binding")
        if row.get("fact_kind") not in allowed_kinds:
            raise ArbitrationViewError("noncritical fact row has an unknown source kind")
        _nonempty_string(row.get("fact_id"), f"noncritical_fact_index[{index}].fact_id")
        _nonempty_string(row.get("paper_key"), f"noncritical_fact_index[{index}].paper_key")
        fact_identity = {
            key: row[key] for key in (
                "candidate_id", "candidate_hash", "fact_kind", "fact_id", "paper_key",
                "source_fact_hash", "source_inventory_artifact_id", "source_inventory_artifact_hash",
            )
        }
        if view_id != "decision-view:" + compute_v3_hash(fact_identity)[:24]:
            raise ArbitrationViewError("noncritical fact ID is not bound to its canonical source identity")
        if row.get("view_hash") != compute_v3_hash(fact_identity):
            raise ArbitrationViewError("noncritical fact hash is invalid")
        if not _is_sha256(row.get("source_binding_hash")):
            raise ArbitrationViewError("noncritical fact source binding hash is invalid")
        if not _is_sha256(row.get("source_fact_hash")):
            raise ArbitrationViewError("noncritical source fact hash is invalid")
    if view_set.get("critical_view_count") != len(seen_views):
        raise ArbitrationViewError("critical view count differs from exact rows")
    if view_set.get("noncritical_fact_count") != len(seen_noncritical):
        raise ArbitrationViewError("noncritical fact count differs from exact rows")
    _validate_fact_partition(view_set, hashes)
    return copy.deepcopy(dict(view_set))


def _validate_fact_partition(view_set: Mapping[str, Any], candidate_hashes: Mapping[str, str]) -> None:
    scope_rows = view_set.get("source_slot_scope_index")
    if not isinstance(scope_rows, list):
        raise ArbitrationViewError("critical view set lacks its source-slot closure index")
    scope_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(scope_rows):
        if not isinstance(row, Mapping):
            raise ArbitrationViewError(f"source slot scope row {index} is malformed")
        candidate_id = str(row.get("candidate_id") or "")
        slot_id = _nonempty_string(row.get("claim_slot_id"), f"source_slot_scope_index[{index}].claim_slot_id")
        key = (candidate_id, slot_id)
        if key in scope_by_pair or candidate_id not in candidate_hashes:
            raise ArbitrationViewError("source slot scope index repeats or references a foreign candidate")
        if row.get("candidate_hash") != candidate_hashes[candidate_id]:
            raise ArbitrationViewError("source slot scope row has a stale candidate hash")
        if row.get("usage_status") not in {"used", "not_used_optional"}:
            raise ArbitrationViewError("source slot scope row has an unsupported candidate usage status")
        for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
            _string_ids(row.get(field), f"source_slot_scope_index[{index}].{field}")
        scope_by_pair[key] = row
    slot_ids = {slot_id for _candidate_id, slot_id in scope_by_pair}
    expected_pairs = {(candidate_id, slot_id) for candidate_id in candidate_hashes for slot_id in slot_ids}
    if set(scope_by_pair) != expected_pairs or not expected_pairs:
        raise ArbitrationViewError("source slot scope index is not an exact candidate-by-slot table")
    usage_by_group: dict[tuple[str, str], set[str]] = {}
    for (candidate_id, _slot_id), row in scope_by_pair.items():
        usage_by_group.setdefault(
            (candidate_id, str(row.get("claim_group_id") or "")), set(),
        ).add(str(row["usage_status"]))
    if any(len(statuses) != 1 for statuses in usage_by_group.values()):
        raise ArbitrationViewError("candidate usage status splits a source claim group's support closure")
    critical_by_pair = {
        (str(row.get("candidate_id") or ""), str(row.get("claim_slot_id") or "")): row
        for row in view_set.get("critical_views") or ()
    }
    if len(critical_by_pair) != len(view_set.get("critical_views") or ()):
        raise ArbitrationViewError("critical view set repeats a candidate/source-slot pair")
    noncritical_fact_ids_by_candidate_kind_slot: dict[tuple[str, str, str], set[str]] = {}
    for row in view_set.get("noncritical_fact_index") or ():
        claim_slot_ids = _string_ids(row.get("claim_slot_ids"), "noncritical fact claim_slot_ids", required=True)
        if any((str(row.get("candidate_id") or ""), slot_id) not in scope_by_pair for slot_id in claim_slot_ids):
            raise ArbitrationViewError("noncritical fact index references a foreign candidate/source slot")
        _string_ids(row.get("claim_group_ids"), "noncritical fact claim_group_ids", required=True)
        candidate_id = row.get("candidate_id")
        fact_kind = row.get("fact_kind")
        if not isinstance(candidate_id, str) or not isinstance(fact_kind, str):
            continue
        fact_id = str(row["fact_id"])
        for slot_id in claim_slot_ids:
            noncritical_fact_ids_by_candidate_kind_slot.setdefault(
                (candidate_id, fact_kind, slot_id), set(),
            ).add(fact_id)
    for key, scope_row in scope_by_pair.items():
        candidate_id, slot_id = key
        view = critical_by_pair.get(key)
        critical_facts = {"source_claim": set(), "evidence": set(), "source_field": set()}
        if view is not None:
            details = view.get("literal_critical_detail")
            if not isinstance(details, Mapping):
                raise ArbitrationViewError("critical view literal detail is malformed")
            critical_facts["source_claim"] = {
                str(row.get("claim_id") or "") for row in details.get("source_claims") or ()
                if isinstance(row, Mapping)
            }
            critical_facts["evidence"] = {
                str(row.get("evidence_id") or "") for row in details.get("evidence") or ()
                if isinstance(row, Mapping)
            }
            critical_facts["source_field"] = {
                str(row.get("source_field_id") or "") for row in details.get("source_fields") or ()
                if isinstance(row, Mapping)
            }
            source_bindings = view.get("source_bindings")
            if not isinstance(source_bindings, Mapping):
                raise ArbitrationViewError("critical view source bindings are malformed")
            if (
                view.get("candidate_hash") != candidate_hashes[candidate_id]
                or view.get("claim_slot_id") != slot_id
                or view.get("claim_group_id") != scope_row.get("claim_group_id")
                or view.get("finding_id") != scope_row.get("primary_claim_id")
                or source_bindings.get("claim_slot_id") != slot_id
                or source_bindings.get("claim_group_id") != scope_row.get("claim_group_id")
                or source_bindings.get("paper_key") != scope_row.get("paper_key")
                or source_bindings.get("primary_claim_id") != scope_row.get("primary_claim_id")
                or source_bindings.get("evidence_ids") != list(scope_row.get("evidence_ids") or [])
                or source_bindings.get("source_claim_ids") != sorted(critical_facts["source_claim"])
                or source_bindings.get("source_field_ids") != sorted(critical_facts["source_field"])
            ):
                raise ArbitrationViewError(f"critical view {candidate_id}.{slot_id} differs from verified slot/source bindings")
        for kind, field in (
            ("source_claim", "source_claim_ids"),
            ("evidence", "evidence_ids"),
            ("source_field", "source_field_ids"),
        ):
            expected = {str(value) for value in scope_row.get(field) or ()}
            critical_ids = critical_facts[kind]
            noncritical_ids = noncritical_fact_ids_by_candidate_kind_slot.get(
                (candidate_id, kind, slot_id), set(),
            )
            if critical_ids.intersection(noncritical_ids) or critical_ids | noncritical_ids != expected:
                raise ArbitrationViewError(f"{candidate_id}.{slot_id} {field} facts are not a disjoint exhaustive partition")


def pack_arbitration_critical_view_wire(
    view_set: Mapping[str, Any],
    view_ids: Sequence[str] | None = None,
    *,
    expected_view_set_hash: str,
) -> dict[str, Any]:
    """Losslessly deduplicate repeated source literals in the same wire body.

    The catalog is part of the provider-visible request, never a local-only
    lookup. Each candidate-specific view keeps its own candidate/source hashes
    and an exact reference to the one literal payload included on that wire.
    """

    validated = validate_arbitration_view_set(
        view_set, expected_view_set_hash=expected_view_set_hash,
    )
    all_views = list(validated.get("critical_views") or ())
    by_id = {str(row["decision_view_id"]): row for row in all_views}
    selected_ids = (
        [str(row["decision_view_id"]) for row in all_views]
        if view_ids is None
        else list(_string_ids(view_ids, "critical wire view_ids"))
    )
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(by_id):
        raise ArbitrationViewError("critical view wire scope has duplicate or foreign decision_view IDs")
    selected = [by_id[view_id] for view_id in selected_ids]
    literal_by_hash: dict[str, dict[str, Any]] = {}
    literal_hash_by_view: dict[str, str] = {}
    for row in selected:
        literal_payload = {
            "source_bindings": copy.deepcopy(row.get("source_bindings") or {}),
            "literal_qualifier_values": copy.deepcopy(row.get("literal_qualifier_values") or []),
            "literal_critical_detail": copy.deepcopy(row.get("literal_critical_detail") or {}),
        }
        literal_hash = compute_v3_hash(literal_payload)
        literal_by_hash.setdefault(literal_hash, literal_payload)
        literal_hash_by_view[str(row["decision_view_id"])] = literal_hash
    catalog_hashes = sorted(literal_by_hash)
    catalog = [
        {"bundle_hash": literal_hash, **literal_by_hash[literal_hash]}
        for literal_hash in catalog_hashes
    ]
    catalog_refs = {literal_hash: index for index, literal_hash in enumerate(catalog_hashes)}
    wire_views = []
    for row in selected:
        projected = copy.deepcopy(dict(row))
        projected.pop("source_bindings", None)
        projected.pop("literal_qualifier_values", None)
        projected.pop("literal_critical_detail", None)
        projected["source_literal_bundle_ref"] = catalog_refs[literal_hash_by_view[str(row["decision_view_id"])]]
        wire_views.append(projected)
    body = {
        "schema_version": CRITICAL_VIEW_WIRE_SCHEMA,
        "source_view_set_hash": validated["view_set_hash"],
        "decision_view_ids": selected_ids,
        "source_literal_bundle_catalog": catalog,
        "source_literal_bundle_catalog_hash": compute_v3_hash(catalog),
        "views": wire_views,
        "resolution_contract": (
            "Each source_literal_bundle_ref indexes the exact source_bindings, literal_qualifier_values, and "
            "literal_critical_detail physically present in this request; no local-only source lookup is required."
        ),
    }
    body["wire_hash"] = compute_v3_hash(body)
    return body


def expand_arbitration_critical_view_wire(
    wire: Mapping[str, Any],
    *,
    expected_view_set_hash: str,
) -> list[dict[str, Any]]:
    """Expand a critical wire bundle and verify catalog and per-view hashes."""

    if not isinstance(wire, Mapping) or wire.get("schema_version") != CRITICAL_VIEW_WIRE_SCHEMA:
        raise ArbitrationViewError("critical view wire schema is invalid")
    if not _is_sha256(expected_view_set_hash) or wire.get("source_view_set_hash") != expected_view_set_hash:
        raise ArbitrationViewError("critical view wire differs from the pinned READY view-set hash")
    body = {key: copy.deepcopy(value) for key, value in wire.items() if key != "wire_hash"}
    if wire.get("wire_hash") != compute_v3_hash(body):
        raise ArbitrationViewError("critical view wire hash is invalid")
    catalog = wire.get("source_literal_bundle_catalog")
    if not isinstance(catalog, list) or wire.get("source_literal_bundle_catalog_hash") != compute_v3_hash(catalog):
        raise ArbitrationViewError("critical view source/literal bundle catalog hash is invalid")
    for row in catalog:
        if not isinstance(row, Mapping) or set(row) != {
            "bundle_hash", "source_bindings", "literal_qualifier_values", "literal_critical_detail"
        }:
            raise ArbitrationViewError("critical view source/literal bundle catalog row has an invalid shape")
        literal_payload = {
            "source_bindings": row["source_bindings"],
            "literal_qualifier_values": row["literal_qualifier_values"],
            "literal_critical_detail": row["literal_critical_detail"],
        }
        if row.get("bundle_hash") != compute_v3_hash(literal_payload):
            raise ArbitrationViewError("critical view source/literal bundle row hash is invalid")
    raw_views = wire.get("views")
    view_ids = wire.get("decision_view_ids")
    if not isinstance(raw_views, list) or not isinstance(view_ids, list) or len(raw_views) != len(view_ids):
        raise ArbitrationViewError("critical view wire rows and ID list differ")
    expanded = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_views):
        if not isinstance(raw, Mapping):
            raise ArbitrationViewError(f"critical view wire row {index} is malformed")
        row = copy.deepcopy(dict(raw))
        view_id = _nonempty_string(row.get("decision_view_id"), f"critical view wire row {index} decision_view_id")
        if view_id in seen or view_ids[index] != view_id:
            raise ArbitrationViewError("critical view wire repeats or misorders decision_view IDs")
        seen.add(view_id)
        reference = row.pop("source_literal_bundle_ref", None)
        if type(reference) is not int or reference < 0 or reference >= len(catalog):
            raise ArbitrationViewError(f"critical view {view_id} has an invalid literal catalog reference")
        literal = catalog[reference]
        row["source_bindings"] = copy.deepcopy(literal["source_bindings"])
        row["literal_qualifier_values"] = copy.deepcopy(literal["literal_qualifier_values"])
        row["literal_critical_detail"] = copy.deepcopy(literal["literal_critical_detail"])
        view_hash = row.get("decision_view_hash")
        view_core = {key: copy.deepcopy(value) for key, value in row.items() if key != "decision_view_hash"}
        if view_hash != compute_v3_hash(view_core):
            raise ArbitrationViewError(f"critical view {view_id} literal binding differs from its view hash")
        expanded.append(row)
    return expanded


def _ordered_issue_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ArbitrationViewError("critique issues must be an array")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ArbitrationViewError(f"critique issue {index} must be an object")
        issue = copy.deepcopy(dict(raw))
        issue_id = _nonempty_string(issue.get("issue_id"), f"critique issue {index}.issue_id")
        if issue_id in seen:
            raise ArbitrationViewError(f"critique issues repeat issue_id {issue_id}")
        seen.add(issue_id)
        if issue.get("scope") not in {"global", "candidate", "section", "claim"}:
            raise ArbitrationViewError(f"critique issue {issue_id} has an unsupported scope")
        if issue.get("severity") not in {"blocking", "non_blocking"}:
            raise ArbitrationViewError(f"critique issue {issue_id} has an unsupported severity")
        targets = _string_ids(issue.get("target_ids"), f"{issue_id}.target_ids")
        if issue.get("scope") in {"section", "claim"} and not targets:
            raise ArbitrationViewError(f"critique issue {issue_id} has no typed targets")
        issue["target_ids"] = list(targets)
        result.append(issue)
    return sorted(result, key=lambda row: str(row["issue_id"]))


def _append_group(groups: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    group_id = _nonempty_string(row.get("group_id"), "review group id")
    if group_id in by_id:
        raise ArbitrationViewError(f"review group map repeats group_id {group_id}")
    row["source_scope_ids"] = _ordered_unique_ids(row.get("source_scope_ids", []), f"{group_id}.source_scope_ids")
    row["required_comparison_ids"] = _ordered_unique_ids(row.get("required_comparison_ids", []), f"{group_id}.required_comparison_ids")
    row["typed_issue_ids"] = _ordered_unique_ids(row.get("typed_issue_ids", []), f"{group_id}.typed_issue_ids")
    row["used_assertion_ids"] = _ordered_unique_ids(row.get("used_assertion_ids", []), f"{group_id}.used_assertion_ids")
    by_id[group_id] = row
    groups.append(row)


def _relation_contract_matches(contract: Mapping[str, Any], comparison: Mapping[str, Any]) -> bool:
    return (
        str(contract.get("comparison_question") or "").strip() == comparison["question"]
        and str(contract.get("relation_type") or "").strip() == comparison["relation_type"]
    )


def _validate_group_map_source_scope(
    group_map: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
    ledger: Mapping[str, Any],
    candidate_contents: Mapping[str, Mapping[str, Any]],
) -> None:
    groups = group_map.get("groups")
    if not isinstance(groups, list) or any(not isinstance(row, Mapping) for row in groups):
        raise ArbitrationViewError("review group map groups must be an array of objects")
    group_rows: dict[str, Mapping[str, Any]] = {}
    for row in groups:
        group_id = _nonempty_string(row.get("group_id"), "review group id")
        if group_id in group_rows:
            raise ArbitrationViewError("review group map repeats a group ID")
        group_rows[group_id] = row
    claim_group_task = {
        row.claim_group_id: row.task_id for row in scope.claim_groups
    }
    claim_group_slots: dict[str, list[str]] = {group_id: [] for group_id in claim_group_task}
    for group in scope.claim_groups:
        if group.task_id not in scope.task_ids:
            raise ArbitrationViewError(f"claim group {group.claim_group_id} has an undeclared task")
    for slot in scope.claim_slots:
        if slot.claim_group_id not in claim_group_slots or claim_group_task[slot.claim_group_id] != slot.task_id:
            raise ArbitrationViewError(f"claim slot {slot.claim_slot_id} has a foreign task/group binding")
        claim_group_slots[slot.claim_group_id].append(slot.claim_slot_id)
    if group_map.get("claim_group_task_bindings") != dict(sorted(claim_group_task.items())):
        raise ArbitrationViewError("review group task assignment differs from CandidateOutputScopeV1")
    comparisons = group_map.get("comparison_relation_bindings")
    if not isinstance(comparisons, Mapping):
        raise ArbitrationViewError("review group map comparison bindings are malformed")
    expected_assertions = {
        group.claim_group_id: group.synthesis_claim_id
        for group in sorted(scope.claim_groups, key=lambda item: item.claim_group_id)
    }
    if group_map.get("claim_group_assertion_bindings") != expected_assertions:
        raise ArbitrationViewError("review group assertion assignments differ from CandidateOutputScopeV1")
    expected_slot_map = {key: sorted(value) for key, value in sorted(claim_group_slots.items())}
    if group_map.get("claim_group_slot_bindings") != expected_slot_map:
        raise ArbitrationViewError("review group slot assignments differ from CandidateOutputScopeV1")
    actual_used_assertions = _used_assertions_by_candidate(scope, candidate_contents)
    expected_task_group_ids = {f"task:{task_id}" for task_id in scope.task_ids}
    for task_id in scope.task_ids:
        row = group_rows.get(f"task:{task_id}")
        if row is None or row.get("group_kind") != "task_topic" or row.get("task_id") != task_id:
            raise ArbitrationViewError(f"review group map omits or alters task {task_id}")
        expected_groups = sorted(group_id for group_id, owner in claim_group_task.items() if owner == task_id)
        task_assertion_ids = sorted({
            assertion_id
            for candidate_rows in actual_used_assertions.values()
            for assertion_id in candidate_rows[task_id]
        })
        if row.get("source_scope_ids") != expected_groups or row.get("used_assertion_ids") != task_assertion_ids:
            raise ArbitrationViewError(f"task group {task_id} has missing/foreign source claim groups or assertions")
        expected_by_candidate = {
            candidate_id: list(candidate_rows[task_id])
            for candidate_id, candidate_rows in sorted(actual_used_assertions.items())
        }
        observed_by_candidate = row.get("used_assertion_ids_by_candidate")
        if observed_by_candidate is None:
            all_task_assertions = sorted({
                group.synthesis_claim_id for group in scope.claim_groups if group.task_id == task_id
            })
            if any(assertion_ids != all_task_assertions for assertion_ids in expected_by_candidate.values()):
                raise ArbitrationViewError(
                    f"task group {task_id} omits its candidate-specific used assertion index"
                )
        elif observed_by_candidate != expected_by_candidate:
            raise ArbitrationViewError(
                f"task group {task_id} candidate-specific used assertions differ from actual candidate support"
            )
        if row.get("required_comparison_ids") != [] or row.get("typed_issue_ids") != []:
            raise ArbitrationViewError(f"task group {task_id} has foreign comparison/issue IDs")

    relation_groups = {
        row.claim_group_id: row for row in scope.claim_groups
        if row.claim_kind == "model_authored_relation_comparison_slot"
    }
    for comparison_id, relation_group_id in comparisons.items():
        if relation_group_id not in relation_groups:
            raise ArbitrationViewError("comparison map references a foreign relation group")
        row = group_rows.get(str(comparison_id))
        relation_group = relation_groups[str(relation_group_id)]
        comparison = row.get("comparison") if isinstance(row, Mapping) else None
        if not isinstance(row, Mapping) or not isinstance(comparison, Mapping):
            raise ArbitrationViewError("comparison group is missing its exact semantic triple")
        if comparison_id != "comparison:" + compute_v3_hash(dict(comparison))[:24]:
            raise ArbitrationViewError("comparison ID is not bound to its canonical semantic triple")
        contract = dict(relation_group.task_contract)
        if (
            set(comparison.get("paper_keys") or ()) != set(relation_group.paper_keys)
            or not _relation_contract_matches(contract, comparison)
            or row.get("group_kind") != "required_comparison"
            or row.get("source_scope_ids") != [relation_group.claim_group_id]
            or row.get("source_relation_group_id") != relation_group.claim_group_id
            or row.get("required_comparison_ids") != [comparison_id]
            or row.get("used_assertion_ids") != [relation_group.synthesis_claim_id]
            or row.get("typed_issue_ids") != []
        ):
            raise ArbitrationViewError("required comparison does not match its unique source relation group")

    raw_issues = ledger.get("critique_issues")
    if not isinstance(raw_issues, list):
        raise ArbitrationViewError("legacy ledger critique issues are malformed")
    issue_by_id = {str(row.get("issue_id") or ""): row for row in raw_issues if isinstance(row, Mapping)}
    mapped_issues = group_map.get("issue_rows")
    if not isinstance(mapped_issues, Mapping) or dict(mapped_issues) != issue_by_id:
        raise ArbitrationViewError("review group issue rows differ from READY legacy critique issues")
    for issue_id, issue in issue_by_id.items():
        row = group_rows.get(f"issue:{issue_id}")
        if (
            not isinstance(row, Mapping)
            or row.get("group_kind") != "typed_issue"
            or row.get("source_scope_ids") != list(issue.get("target_ids") or [])
            or row.get("typed_issue_ids") != [issue_id]
            or row.get("required_comparison_ids") != []
            or row.get("used_assertion_ids") != []
            or row.get("issue_severity") != issue.get("severity")
            or row.get("issue_scope") != issue.get("scope")
            or row.get("candidate_id") != str(issue.get("candidate_id") or "")
        ):
            raise ArbitrationViewError(f"typed issue group {issue_id} differs from its READY critique issue")
    expected_group_ids = expected_task_group_ids | set(comparisons) | {f"issue:{issue_id}" for issue_id in issue_by_id}
    if set(group_rows) != expected_group_ids:
        raise ArbitrationViewError("review group map has missing or foreign task/comparison/issue groups")
    expected_count = len(scope.task_ids) + len(comparisons) + len(issue_by_id)
    if (
        group_map.get("task_count") != len(scope.task_ids)
        or group_map.get("comparison_count") != len(comparisons)
        or group_map.get("typed_issue_count") != len(issue_by_id)
        or group_map.get("group_count") != expected_count
        or len(groups) != expected_count
    ):
        raise ArbitrationViewError("review group map violates exact W=T+M+I cardinality")


def _verify_source_scope_binding(scope: CandidateOutputScopeV1, inventory: VerifiedWriterSourceInventoryV1) -> None:
    if (
        scope.registry_job_id != inventory.registry_job_id
        or scope.source_inventory_artifact_id != inventory.artifact_id
        or scope.source_inventory_artifact_hash != inventory.artifact_hash
        or scope.source_inventory_content_hash != inventory.content_hash
    ):
        raise ArbitrationViewError("candidate scope is not bound to the supplied verified source inventory")


def _validated_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(ledger, Mapping):
        raise ArbitrationViewError("READY legacy ledger must be an object")
    from outline.candidate_arbitration_ledger_packing import (
        expand_arbitration_final_ledger,
        pack_arbitration_final_ledger,
    )

    if ledger.get("ledger_packing") is not None:
        try:
            return expand_arbitration_final_ledger(ledger)
        except (TypeError, ValueError, KeyError) as exc:
            raise ArbitrationViewError("READY legacy packed ledger is invalid") from exc
    try:
        pack_arbitration_final_ledger(ledger)
    except (TypeError, ValueError, KeyError) as exc:
        raise ArbitrationViewError("READY legacy canonical ledger is invalid") from exc
    return copy.deepcopy(dict(ledger))


def _source_indexes(inventory: VerifiedWriterSourceInventoryV1) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for paper in inventory.papers:
        if paper.paper_key in result:
            raise ArbitrationViewError(f"verified source inventory repeats paper {paper.paper_key}")
        claims = _unique_objects(paper.claims, "claim_id", f"{paper.paper_key} source claims")
        evidence = _unique_objects(paper.evidence, "evidence_id", f"{paper.paper_key} evidence")
        fields = _unique_objects(paper.source_fields, "source_field_id", f"{paper.paper_key} source fields")
        dependencies: dict[str, list[Any]] = {}
        for dependency in paper.interpretation_dependencies:
            dependencies.setdefault(str(dependency.primary_claim_id), []).append(dependency)
        result[paper.paper_key] = {
            "paper": paper,
            "claims": claims,
            "evidence": evidence,
            "fields": fields,
            "dependencies": dependencies,
        }
    return result


def _unique_objects(rows: Sequence[Any], field: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, row in enumerate(rows):
        key = _nonempty_string(getattr(row, field, None), f"{label}[{index}].{field}")
        if key in result:
            raise ArbitrationViewError(f"{label} repeats {field} {key}")
        result[key] = row
    return result


def _unique_rows(rows: Any, field: str, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ArbitrationViewError(f"{label} must be an array of objects")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        key = _nonempty_string(row.get(field), f"{label}[{index}].{field}")
        if key in result:
            raise ArbitrationViewError(f"{label} repeats {field} {key}")
        result[key] = row
    return result


def _validate_support_binding(support: Mapping[str, Any], slot: Any, label: str) -> None:
    expected = {
        "task_id": slot.task_id,
        "paper_key": slot.paper_key,
        "primary_claim_id": slot.primary_claim_id,
        "source_claim_ids": list(slot.source_claim_ids),
        "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids),
    }
    for field, value in expected.items():
        actual = support.get(field)
        if actual != value:
            raise ArbitrationViewError(f"{label} {field} differs from its verified candidate-output scope")
    if str(support.get("study_id") or "") != slot.study_id:
        raise ArbitrationViewError(f"{label} study_id differs from its verified candidate-output scope")
    if support.get("claim_group_id") is not None and support.get("claim_group_id") != slot.claim_group_id:
        raise ArbitrationViewError(f"{label} claim_group_id differs from CandidateOutputScopeV1")
    if support.get("synthesis_claim_id") is not None and support.get("synthesis_claim_id") != slot.synthesis_claim_id:
        raise ArbitrationViewError(f"{label} synthesis_claim_id differs from CandidateOutputScopeV1")


def _section_claim_ids(section: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"claim_id", "synthesis_claim_id", "primary_claim_id"} and isinstance(item, str):
                    ids.add(item)
                else:
                    visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item)

    visit(section)
    return ids


def _mark_critical_slot(
    by_slot: dict[str, dict[str, Any]],
    slot_id: str,
    *,
    reason: str,
    group_id: str,
    slot: Any,
    include_full_closure: bool,
    claim_ids: set[str] | None = None,
    evidence_ids: set[str] | None = None,
    field_ids: set[str] | None = None,
) -> None:
    if slot_id not in by_slot:
        raise ArbitrationViewError(f"critical scope references foreign claim slot {slot_id}")
    row = by_slot[slot_id]
    row["reasons"].add(reason)
    row["group_ids"].add(group_id)
    if include_full_closure:
        row["claim_ids"].update(slot.source_claim_ids)
        row["evidence_ids"].update(slot.evidence_ids)
        row["field_ids"].update(slot.source_field_ids)
    row["claim_ids"].update(claim_ids or set())
    row["evidence_ids"].update(evidence_ids or set())
    row["field_ids"].update(field_ids or set())


def _selected_source_rows(indexes: Mapping[str, Any], slot: Any, critical: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    claim_ids = set(critical["claim_ids"])
    evidence_ids = set(critical["evidence_ids"])
    field_ids = set(critical["field_ids"])
    # Critical comparison/issue closure already includes every source fact;
    # qualifier-only views include only exact dependency IDs.
    if not (claim_ids or evidence_ids or field_ids):
        raise ArbitrationViewError(f"critical slot {slot.claim_slot_id} has no source literal bindings")
    claims = []
    for claim_id in sorted(claim_ids):
        row = indexes["claims"].get(claim_id)
        if row is None:
            raise ArbitrationViewError(f"source claim {claim_id} is absent from verified paper {slot.paper_key}")
        owners = tuple(sorted(str(value) for value in row.owner_study_ids if str(value)))
        if len(owners) > 1:
            raise ArbitrationViewError(f"source claim {claim_id} has ambiguous study ownership")
        claims.append({
            "claim_id": row.claim_id,
            "claim_type": row.claim_type,
            "text": row.text,
            "owner_study_ids": list(owners),
            "evidence_ids": list(row.evidence_ids),
            "source_locator": row.source_locator,
            "source_summary_hash": row.source_summary_hash,
        })
    evidence = []
    for evidence_id in sorted(evidence_ids):
        row = indexes["evidence"].get(evidence_id)
        if row is None:
            raise ArbitrationViewError(f"source evidence {evidence_id} is absent from verified paper {slot.paper_key}")
        owners = tuple(sorted(str(value) for value in row.owner_study_ids if str(value)))
        if len(owners) > 1:
            raise ArbitrationViewError(f"source evidence {evidence_id} has ambiguous study ownership")
        evidence.append({
            "evidence_id": row.evidence_id,
            "text": row.text,
            "text_status": "known" if isinstance(row.text, str) else "unknown",
            "owner_study_ids": list(owners),
        })
    fields = []
    for field_id in sorted(field_ids):
        row = indexes["fields"].get(field_id)
        if row is None:
            raise ArbitrationViewError(f"source field {field_id} is absent from verified paper {slot.paper_key}")
        owners = tuple(sorted(str(value) for value in row.owner_study_ids if str(value)))
        if len(owners) > 1:
            raise ArbitrationViewError(f"source field {field_id} has ambiguous study ownership")
        fields.append({
            "source_field_id": row.source_field_id,
            "source_path": row.source_path,
            "source_value": row.source_value,
            "disposition": row.disposition,
            "canonical_field": row.canonical_field,
            "scope": row.scope,
            "study_id": row.source_study_id or (owners[0] if owners else ""),
            "interpretation_required": row.interpretation_required,
            "source_summary_hash": row.source_summary_hash,
            "derived_value": row.derived_value,
            "owner_study_ids": list(owners),
        })
    expected_claims = set(slot.source_claim_ids)
    expected_evidence = set(slot.evidence_ids)
    expected_fields = set(slot.source_field_ids)
    if not claim_ids.issubset(expected_claims):
        raise ArbitrationViewError(f"critical view for {slot.claim_slot_id} has foreign source claim IDs")
    if not evidence_ids.issubset(expected_evidence):
        raise ArbitrationViewError(f"critical view for {slot.claim_slot_id} has foreign evidence IDs")
    if not field_ids.issubset(expected_fields):
        raise ArbitrationViewError(f"critical view for {slot.claim_slot_id} has foreign source field IDs")
    return {"claims": claims, "evidence": evidence, "fields": fields}


def _source_fact_payload(kind: str, row: Any) -> dict[str, Any]:
    """Canonical exact row hash for a source fact retained in detail lookup."""

    if kind == "source_claim":
        return {
            "source_kind": kind,
            "claim_id": row.claim_id,
            "paper_key": row.paper_key,
            "owner_study_ids": list(row.owner_study_ids),
            "claim_type": row.claim_type,
            "text": row.text,
            "evidence_ids": list(row.evidence_ids),
            "source_locator": row.source_locator,
            "source_summary_hash": row.source_summary_hash,
        }
    if kind == "evidence":
        return {
            "source_kind": kind,
            "evidence_id": row.evidence_id,
            "paper_key": row.paper_key,
            "owner_study_ids": list(row.owner_study_ids),
            "text": row.text,
        }
    if kind == "source_field":
        return {
            "source_kind": kind,
            "source_field_id": row.source_field_id,
            "paper_key": row.paper_key,
            "owner_study_ids": list(row.owner_study_ids),
            "source_path": row.source_path,
            "source_value": row.source_value,
            "disposition": row.disposition,
            "canonical_field": row.canonical_field,
            "scope": row.scope,
            "source_study_id": row.source_study_id,
            "interpretation_required": row.interpretation_required,
            "source_summary_hash": row.source_summary_hash,
            "derived_value": row.derived_value,
        }
    raise ArbitrationViewError(f"unsupported source fact kind {kind}")


def _verified_study_id(slot: Any, source_rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    observed = {str(slot.study_id or "")} - {""}
    for row in source_rows["claims"]:
        observed.update(str(value) for value in row["owner_study_ids"] if str(value))
    for row in source_rows["evidence"]:
        observed.update(str(value) for value in row["owner_study_ids"] if str(value))
    for row in source_rows["fields"]:
        if row.get("study_id"):
            observed.add(str(row["study_id"]))
        observed.update(str(value) for value in row["owner_study_ids"] if str(value))
    if len(observed) > 1:
        raise ArbitrationViewError(f"claim slot {slot.claim_slot_id} has conflicting source study IDs")
    return next(iter(observed), "")


def _string_ids(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ArbitrationViewError(f"{label} must be an array of IDs")
    rows = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ArbitrationViewError(f"{label} contains an empty or non-string ID")
    if len(rows) != len(set(rows)):
        raise ArbitrationViewError(f"{label} contains duplicate IDs")
    if required and not rows:
        raise ArbitrationViewError(f"{label} is required")
    return rows


def _ordered_unique_ids(value: Any, label: str) -> list[str]:
    return list(_string_ids(value, label))


def _exact_hash_map(value: Any, ids: Sequence[str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise ArbitrationViewError(f"{label} must exactly match eligible candidate IDs")
    result: dict[str, str] = {}
    for candidate_id in ids:
        digest = value[candidate_id]
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ArbitrationViewError(f"{label}[{candidate_id}] must be a lowercase SHA-256")
        result[candidate_id] = digest
    return result


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationViewError(f"{label} must be a non-empty string")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "CRITICAL_VIEW_SCHEMA",
    "CRITICAL_VIEW_WIRE_SCHEMA",
    "DECISION_VIEW_SCHEMA",
    "GROUP_MAP_SCHEMA",
    "ArbitrationViewError",
    "build_arbitration_review_group_map",
    "compile_arbitration_critical_views",
    "expand_arbitration_critical_view_wire",
    "pack_arbitration_critical_view_wire",
    "validate_arbitration_view_set",
]

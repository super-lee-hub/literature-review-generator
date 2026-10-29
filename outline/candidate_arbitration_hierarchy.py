"""Bounded, lossless section-level arbitration planning and coverage checks.

The module has no provider, Registry, or executor side effects.  It prepares
complete candidate-section units, measures the fully built request supplied by
the caller, and validates the exact coverage returned by each leaf.  The
caller remains responsible for durable ProviderCall/Registry execution.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from outline.v3_models import compute_v3_hash

_PLAN_SCHEMA = "outline_candidate_arbitration_hierarchy/v1"
_LEAF_SCHEMA = "outline_candidate_arbitration_leaf/v1"
_LEDGER_SCHEMA = "outline_candidate_arbitration_ledger/v1"
_CLAIM_SUPPORT_PARTITION_SCHEMA = "outline_candidate_claim_support_partition/v1"


class ArbitrationHierarchyError(ValueError):
    """An arbitration plan or leaf result is malformed or incomplete."""


def plan_arbitration_hierarchy(
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    candidate_contents: Mapping[str, Any],
    critique_disposition: Mapping[str, Any],
    *,
    critique_hashes: Mapping[str, str],
    provenance_hashes: Mapping[str, str],
    request_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    estimate_input_tokens: Callable[[Mapping[str, Any]], Any],
    input_cap: int,
    max_calls: int,
    reserved_calls: int = 1,
    output_cap: int | None = None,
) -> dict[str, Any]:
    """Pack exact whole-section comparison units under the effective request cap.

    Sections are aligned only when their exact sorted task-id sets match.  This
    deliberately avoids transitive grouping: a section assigned to tasks A/B
    does not pull in separate A and B sections.  Candidate sections without a
    task identity receive candidate-local units and are never falsely aligned.

    ``request_builder`` must attach the actual prompt authority and leaf output
    contract before returning the request.  ``estimate_input_tokens`` must use
    the production route/profile estimator on that final request.  The planner
    greedily packs whole units only; one oversized unit blocks before dispatch.
    """

    ids = _ordered_unique_ids(candidate_ids, "candidate_ids")
    ids = tuple(sorted(ids))
    if len(ids) < 1:
        raise ArbitrationHierarchyError("arbitration hierarchy requires at least one eligible candidate")
    hashes = _exact_hash_map(candidate_hashes, ids, "candidate_hashes")
    contents = _exact_mapping(candidate_contents, ids, "candidate_contents")
    critic_hash_map = _nonempty_hash_map(critique_hashes, "critique_hashes")
    provenance_hash_map = _nonempty_hash_map(provenance_hashes, "provenance_hashes")
    if type(input_cap) is not int or input_cap < 1:
        raise ArbitrationHierarchyError("input_cap must be a positive integer")
    if type(max_calls) is not int or max_calls < 1:
        raise ArbitrationHierarchyError("max_calls must be a positive integer")
    if type(reserved_calls) is not int or reserved_calls < 1:
        raise ArbitrationHierarchyError("reserved_calls must reserve at least the final arbitration call")
    if output_cap is not None and (type(output_cap) is not int or output_cap < 1):
        raise ArbitrationHierarchyError("output_cap must be a positive integer when supplied")

    issues = _validated_issues(critique_disposition, ids, hashes)
    if critique_disposition.get("global_blocker"):
        raise ArbitrationHierarchyError("cannot plan arbitration while critique disposition has a global blocker")
    eligible = _ordered_unique_ids(
        critique_disposition.get("eligible_candidate_ids"), "critique eligible_candidate_ids"
    )
    if set(eligible) != set(ids):
        raise ArbitrationHierarchyError("candidate IDs must exactly match critique-eligible candidate IDs")

    sections_by_candidate: dict[str, list[dict[str, Any]]] = {}
    section_index: dict[str, list[dict[str, Any]]] = {}
    unit_sections: dict[str, dict[str, list[dict[str, Any]]]] = {}
    unit_task_ids: dict[str, list[str]] = {}
    for candidate_id in ids:
        raw_content = contents[candidate_id]
        if not isinstance(raw_content, Mapping):
            raise ArbitrationHierarchyError(f"candidate {candidate_id} content must be an object")
        raw_sections = raw_content.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ArbitrationHierarchyError(f"candidate {candidate_id} must contain a non-empty sections array")
        seen_section_ids: set[str] = set()
        candidate_sections: list[dict[str, Any]] = []
        candidate_index: list[dict[str, Any]] = []
        for position, raw_section in enumerate(raw_sections):
            if not isinstance(raw_section, Mapping):
                raise ArbitrationHierarchyError(f"candidate {candidate_id} section {position} must be an object")
            section = copy.deepcopy(dict(raw_section))
            section_id = _nonempty_str(section.get("section_id"), f"{candidate_id} section_id")
            if section_id in seen_section_ids:
                raise ArbitrationHierarchyError(f"candidate {candidate_id} has duplicate section_id {section_id}")
            seen_section_ids.add(section_id)
            claims = section.get("claims")
            if not isinstance(claims, list) or any(not isinstance(claim, str) for claim in claims):
                raise ArbitrationHierarchyError(f"candidate {candidate_id} section {section_id} claims must be strings")
            _validate_section_source_closure(section, candidate_id, section_id, claims)
            tasks = _section_task_ids(section, candidate_id, section_id)
            section_hash = compute_v3_hash(section)
            task_signature = list(tasks)
            unit_key = (
                "task-set:" + compute_v3_hash(task_signature)
                if task_signature
                else "candidate-section:" + compute_v3_hash({
                    "candidate_id": candidate_id,
                    "section_id": section_id,
                    "section_hash": section_hash,
                })
            )
            descriptor = {
                "candidate_id": candidate_id,
                "section_id": section_id,
                "position": position,
                "section_hash": section_hash,
                "task_ids": task_signature,
                "paper_keys": _string_list(section.get("paper_keys"), f"{candidate_id}.{section_id}.paper_keys"),
                "title": str(section.get("title") or ""),
                "goal": str(section.get("goal") or ""),
                "relation_ids": _string_list(section.get("relation_ids"), f"{candidate_id}.{section_id}.relation_ids"),
                "claim_count": len(claims),
                "support_row_count": len(section.get("claim_support") or []),
                "source_closure_hash": compute_v3_hash(section.get("claim_support") or []),
            }
            candidate_sections.append({
                "section_id": section_id,
                "section_hash": section_hash,
                "task_ids": task_signature,
                "unit_key": unit_key,
                "section": section,
                "position": position,
            })
            candidate_index.append(descriptor)
            unit_sections.setdefault(unit_key, {candidate: [] for candidate in ids})
            unit_sections[unit_key][candidate_id].append({
                "section_id": section_id,
                "section_hash": section_hash,
                "task_ids": task_signature,
                "section": section,
                "position": position,
            })
            unit_task_ids[unit_key] = task_signature
        sections_by_candidate[candidate_id] = candidate_sections
        section_index[candidate_id] = candidate_index

    # Broad topic tasks can own several scientifically distinct sections in one
    # alternative. Such a task label is not an atomic comparison dependency.
    # Refine only these ambiguous alignments by the complete source footprint;
    # keep every original section, claim and support row intact.
    ambiguous = {key for key, members in unit_sections.items()
                 if any(len(rows) > 1 for rows in members.values())}
    if ambiguous:
        refined_sections: dict[str, dict[str, list[dict[str, Any]]]] = {}
        refined_tasks: dict[str, list[str]] = {}
        for candidate_id in ids:
            for item in sections_by_candidate[candidate_id]:
                parent_key = item["unit_key"]
                key = (parent_key + ":source-scope:" + _section_source_footprint(item["section"])
                       if parent_key in ambiguous else parent_key)
                item["unit_key"] = key
                refined_sections.setdefault(key, {candidate: [] for candidate in ids})[candidate_id].append(item)
                refined_tasks[key] = list(item["task_ids"])
        unit_sections, unit_task_ids = refined_sections, refined_tasks

    _validate_issue_targets(issues, section_index, sections_by_candidate)

    units: list[dict[str, Any]] = []
    for unit_key in sorted(unit_sections):
        by_candidate = unit_sections[unit_key]
        unit_id = "section-unit:" + compute_v3_hash({
            "unit_key": unit_key,
            "sections": {
                candidate_id: [
                    {"section_id": row["section_id"], "section_hash": row["section_hash"]}
                    for row in sorted(by_candidate[candidate_id], key=lambda item: (item["position"], item["section_id"]))
                ]
                for candidate_id in ids
            },
        })[:24]
        units.append({
            "unit_id": unit_id,
            "task_ids": list(unit_task_ids[unit_key]),
            "unit_key": unit_key,
            "sections_by_candidate": {
                candidate_id: [
                    {
                        "section_id": row["section_id"],
                        "section_hash": row["section_hash"],
                        "task_ids": list(row["task_ids"]),
                    }
                    for row in sorted(by_candidate[candidate_id], key=lambda item: (item["position"], item["section_id"]))
                ]
                for candidate_id in ids
            },
        })
    if len({str(unit["unit_id"]) for unit in units}) != len(units):
        raise ArbitrationHierarchyError("section unit identity collision detected")

    input_identity = {
        "schema_version": _PLAN_SCHEMA,
        "candidate_ids": list(ids),
        "candidate_hashes": hashes,
        "candidate_projection_hashes": {
            candidate_id: compute_v3_hash(contents[candidate_id]) for candidate_id in ids
        },
        "critique_hashes": critic_hash_map,
        "provenance_hashes": provenance_hash_map,
        "critique_issue_hash": compute_v3_hash(issues),
        "section_index_hash": compute_v3_hash(section_index),
        "units": units,
    }
    input_hash = compute_v3_hash(input_identity)
    section_index_rows = copy.deepcopy(section_index)
    critique_issue_rows = copy.deepcopy(issues)
    full_sections_by_id = {
        candidate_id: {str(item["section_id"]): item for item in rows}
        for candidate_id, rows in sections_by_candidate.items()
    }

    def make_leaf_body(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        unit_ids = [str(row["unit_id"]) for row in rows]
        selected_sections: dict[str, list[dict[str, Any]]] = {candidate: [] for candidate in ids}
        expected_rows: dict[str, dict[str, Any]] = {}
        issues_by_candidate: dict[str, list[dict[str, Any]]] = {candidate: [] for candidate in ids}
        global_issues = [issue for issue in critique_issue_rows if issue["scope"] == "global"]
        selected_ids_by_candidate: dict[str, set[str]] = {candidate: set() for candidate in ids}
        evidence_by_candidate: dict[str, set[str]] = {candidate: set() for candidate in ids}
        section_hashes_by_candidate: dict[str, dict[str, str]] = {candidate: {} for candidate in ids}
        selected_slots_by_candidate: dict[str, dict[str, set[str]]] = {candidate: {} for candidate in ids}
        part_receipts_by_candidate: dict[str, list[dict[str, Any]]] = {candidate: [] for candidate in ids}

        selected_items: dict[str, dict[str, dict[str, Any]]] = {candidate: {} for candidate in ids}
        for unit in rows:
            by_candidate = unit_sections.get(str(unit["unit_key"]))
            if not isinstance(by_candidate, Mapping):
                raise ArbitrationHierarchyError("arbitration unit has no bound source sections")
            for candidate_id in ids:
                for item in by_candidate[candidate_id]:
                    section_id = str(item["section_id"])
                    current = selected_items[candidate_id].get(section_id)
                    slot_ids = item.get("claim_slot_ids")
                    if current is None:
                        selected_items[candidate_id][section_id] = {
                            "item": item,
                            "full": slot_ids is None,
                            "claim_slot_ids": set(slot_ids or ()),
                        }
                    elif current["full"] or slot_ids is None:
                        raise ArbitrationHierarchyError(
                            f"whole-section and claim-part units overlap for {candidate_id}.{section_id}"
                        )
                    else:
                        current["claim_slot_ids"].update(slot_ids)
                if unit.get("claim_support_part") is True:
                    bindings = unit["claim_support_bindings_by_section"][candidate_id]
                    part_receipts_by_candidate[candidate_id].append({
                        "part_id": str(unit["unit_id"]),
                        "part_hash": str(unit["claim_support_part_hash"]),
                        "parent_section_bindings": [
                            {"section_id": str(binding["section_id"]),
                             "section_hash": str(binding["section_hash"])}
                            for binding in bindings
                        ],
                    })

        for candidate_id in ids:
            for section_id, selection in selected_items[candidate_id].items():
                original = full_sections_by_id[candidate_id][section_id]
                slot_ids = None if selection["full"] else selection["claim_slot_ids"]
                selected_section = (
                    copy.deepcopy(original["section"])
                    if slot_ids is None
                    else _project_section_to_claim_slots(original["section"], slot_ids, candidate_id, section_id)
                )
                selected_sections[candidate_id].append(selected_section)
                selected_ids_by_candidate[candidate_id].add(section_id)
                section_hashes_by_candidate[candidate_id][section_id] = original["section_hash"]
                evidence_by_candidate[candidate_id].update(_section_evidence_refs(selected_section))
                if slot_ids is not None:
                    selected_slots_by_candidate[candidate_id][section_id] = set(slot_ids)

        for issue in critique_issue_rows:
            scope = str(issue["scope"])
            candidate_id = str(issue.get("candidate_id") or "")
            if scope == "global":
                continue
            if candidate_id not in issues_by_candidate:
                continue
            section_ids = selected_ids_by_candidate[candidate_id]
            attached = (
                scope == "candidate"
                or scope not in {"section", "claim"}
                or _issue_targets_selected_sections(issue, section_ids, selected_sections[candidate_id], scope)
            )
            if attached:
                issues_by_candidate[candidate_id].append(copy.deepcopy(issue))
                evidence_by_candidate[candidate_id].update(_string_list(issue.get("evidence_refs"), "issue evidence_refs"))

        for candidate_id in ids:
            selected_sections[candidate_id].sort(
                key=lambda section: (
                    next((item["position"] for item in sections_by_candidate[candidate_id]
                          if item["section_id"] == section.get("section_id")), 0),
                    str(section.get("section_id") or ""),
                )
            )
            expected_rows[candidate_id] = {
                "candidate_id": candidate_id,
                "candidate_hash": hashes[candidate_id],
                "presence": "present" if selected_sections[candidate_id] else "absent",
                "reviewed_section_ids": sorted(selected_ids_by_candidate[candidate_id]),
                "section_hashes": dict(sorted(section_hashes_by_candidate[candidate_id].items())),
                "critic_issue_ids": (
                    sorted(str(item["issue_id"]) for item in issues_by_candidate[candidate_id])
                    if selected_sections[candidate_id]
                    else []
                ),
                "allowed_evidence_refs": sorted(evidence_by_candidate[candidate_id]),
            }
            if part_receipts_by_candidate[candidate_id]:
                expected_rows[candidate_id]["claim_support_part_receipts"] = copy.deepcopy(
                    part_receipts_by_candidate[candidate_id]
                )

        has_claim_parts = any(selected_slots_by_candidate.values())

        body = {
            "schema_version": _LEAF_SCHEMA,
            "hierarchy": {
                "level": "claim_support_part_comparison_leaf" if has_claim_parts else "whole_section_comparison_leaf",
                "plan_input_hash": input_hash,
                "unit_ids": unit_ids,
            },
            "candidate_ids": list(ids),
            "candidate_hashes": copy.deepcopy(hashes),
            "candidate_headers": {
                candidate_id: _candidate_header(contents[candidate_id], sections_by_candidate[candidate_id])
                for candidate_id in ids
            },
            "candidate_section_index": copy.deepcopy(section_index_rows),
            "candidate_sections_by_id": selected_sections,
            "candidate_section_bindings": {
                candidate_id: [
                    {"section_id": section_id, "section_hash": section_hash}
                    for section_id, section_hash in sorted(section_hashes_by_candidate[candidate_id].items())
                ]
                for candidate_id in ids
            },
            "comparison_units": [copy.deepcopy(dict(row)) for row in rows],
            "critique_hashes": copy.deepcopy(critic_hash_map),
            "provenance_hashes": copy.deepcopy(provenance_hash_map),
            "global_critique_issues": copy.deepcopy(global_issues),
            "candidate_critique_issues": {
                candidate_id: issues_by_candidate[candidate_id] for candidate_id in ids
            },
            "_expected_candidate_rows": expected_rows,
        }
        if has_claim_parts:
            body["claim_support_part_bindings"] = {
                "schema_version": _CLAIM_SUPPORT_PARTITION_SCHEMA,
                "parts": [
                    {
                        "part_id": str(unit["unit_id"]),
                        "part_hash": str(unit["claim_support_part_hash"]),
                        "parent_bindings_by_candidate": copy.deepcopy(
                            dict(unit["claim_support_bindings_by_section"])
                        ),
                    }
                    for unit in rows if unit.get("claim_support_part") is True
                ],
            }
        return body

    packed_leaves: list[dict[str, Any]] = []
    current_units: list[dict[str, Any]] = []
    partition_parent_rows: dict[str, list[dict[str, Any]]] = {}

    def build_measured_leaf(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        body = make_leaf_body(rows)
        leaf_id = "arbitration-leaf:" + compute_v3_hash({
            "plan_input_hash": input_hash,
            "unit_ids": [str(row["unit_id"]) for row in rows],
        })[:24]
        body["hierarchy"]["leaf_id"] = leaf_id
        expected_rows = body.pop("_expected_candidate_rows")
        request = request_builder(copy.deepcopy(body))
        if not isinstance(request, Mapping):
            raise ArbitrationHierarchyError("request_builder must return a mapping")
        request_copy = copy.deepcopy(dict(request))
        metrics = _measure(estimate_input_tokens(request_copy), leaf_id)
        if metrics["input_tokens"] > input_cap:
            raise ArbitrationHierarchyError(
                f"indivisible section leaf {leaf_id} request input={metrics['input_tokens']} exceeds cap={input_cap}"
            )
        if not metrics["within_budget"]:
            raise ArbitrationHierarchyError(f"section leaf {leaf_id} is outside the effective provider profile budget")
        if output_cap is not None and metrics["output_tokens"] is not None and metrics["output_tokens"] > output_cap:
            raise ArbitrationHierarchyError(
                f"section leaf {leaf_id} requested output={metrics['output_tokens']} exceeds cap={output_cap}"
            )
        return {
            "leaf_id": leaf_id,
            "unit_ids": [str(row["unit_id"]) for row in rows],
            "request": request_copy,
            "request_hash": compute_v3_hash(request_copy),
            "estimated_input_tokens": metrics["input_tokens"],
            "requested_output_tokens": metrics["output_tokens"],
            "expected_candidate_rows": expected_rows,
        }

    # Probe every atomic comparison unit with the exact production request
    # builder before greedy packing. Only a whole-section unit that fails this
    # measurement may enter the claim-support path; fitting sections retain
    # the original wire and assessment contract.
    pending_units = list(units)
    while True:
        prior_unit_count = len(pending_units)
        partition = _claim_support_partition_for_units(pending_units, partition_parent_rows)
        input_identity["units"] = pending_units
        if partition is None:
            input_identity.pop("claim_support_partition", None)
        else:
            input_identity["claim_support_partition"] = partition
        input_hash = compute_v3_hash(input_identity)

        expanded_units: list[dict[str, Any]] = []
        split_any = False
        for unit in pending_units:
            try:
                build_measured_leaf([unit])
            except ArbitrationHierarchyError as exc:
                if not _request_size_failure(exc):
                    raise
                child_units = _split_oversized_claim_unit(
                    unit,
                    unit_sections=unit_sections,
                    candidate_ids=ids,
                    build_measured_leaf=build_measured_leaf,
                    partition_parent_rows=partition_parent_rows,
                    original_error=exc,
                )
                expanded_units.extend(child_units)
                split_any = True
            else:
                expanded_units.append(unit)
        pending_units = expanded_units
        if not split_any:
            units = pending_units
            break
        if len(pending_units) <= prior_unit_count:
            raise ArbitrationHierarchyError("claim-support subdivision made no progress")

    plan_claim_support_partition = _claim_support_partition_for_units(units, partition_parent_rows)
    input_identity["units"] = units
    if plan_claim_support_partition is None:
        input_identity.pop("claim_support_partition", None)
    else:
        input_identity["claim_support_partition"] = plan_claim_support_partition
    input_hash = compute_v3_hash(input_identity)

    # Re-run the exact singleton measurement after the semantic input hash has
    # stabilized. This catches tokenization differences in the hash-bound wire
    # fields before the planner admits any leaf.
    for unit in units:
        build_measured_leaf([unit])

    for unit in units:
        trial = [*current_units, unit]
        try:
            build_measured_leaf(trial)
        except ArbitrationHierarchyError as exc:
            if not current_units:
                raise
            packed_leaves.append(build_measured_leaf(current_units))
            current_units = [unit]
            try:
                build_measured_leaf(current_units)
            except ArbitrationHierarchyError as singleton_exc:
                raise singleton_exc from exc
        else:
            current_units = trial
    if current_units:
        packed_leaves.append(build_measured_leaf(current_units))

    if len(packed_leaves) + reserved_calls > max_calls:
        raise ArbitrationHierarchyError(
            f"arbitration hierarchy requires {len(packed_leaves)} leaf calls plus "
            f"{reserved_calls} reserved calls, exceeding max_calls={max_calls}"
        )
    if len({str(leaf["leaf_id"]) for leaf in packed_leaves}) != len(packed_leaves):
        raise ArbitrationHierarchyError("arbitration leaf identity collision detected")

    plan: dict[str, Any] = {
        "schema_version": _PLAN_SCHEMA,
        "input_hash": input_hash,
        "candidate_ids": list(ids),
        "candidate_hashes": hashes,
        "candidate_projection_hashes": input_identity["candidate_projection_hashes"],
        "critique_hashes": critic_hash_map,
        "provenance_hashes": provenance_hash_map,
        "critique_issues": critique_issue_rows,
        "candidate_section_index": section_index_rows,
        "units": units,
        "leaves": packed_leaves,
        "reserved_calls": reserved_calls,
        "call_count": len(packed_leaves) + reserved_calls,
        "max_calls": max_calls,
    }
    if plan_claim_support_partition is not None:
        plan["claim_support_partition"] = plan_claim_support_partition
    # The available call count is a volatile admission snapshot. It must gate
    # whether this plan can run, but it is not part of semantic replay
    # identity; a resumed run with more remaining budget must reuse the same
    # leaf requests and receipts.
    plan["plan_hash"] = compute_v3_hash(_semantic_plan_hash_body(plan))
    return plan


def validate_arbitration_leaf(
    plan: Mapping[str, Any], leaf_id: str, response: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one leaf's five-candidate assessment and exact section coverage."""

    _validate_plan_integrity(plan)
    leaf = _leaf_by_id(plan, leaf_id)
    if not isinstance(response, Mapping):
        raise ArbitrationHierarchyError("arbitration leaf response must be an object")
    if response.get("leaf_id") != leaf_id:
        raise ArbitrationHierarchyError("arbitration leaf response has a foreign leaf_id")
    expected_unit_ids = list(leaf.get("unit_ids") or [])
    if _ordered_unique_ids(response.get("unit_ids"), "leaf unit_ids") != tuple(expected_unit_ids):
        raise ArbitrationHierarchyError("arbitration leaf response unit_ids differ from its assigned units")
    raw_rows = response.get("candidate_assessments")
    if not isinstance(raw_rows, list):
        raise ArbitrationHierarchyError("arbitration leaf candidate_assessments must be an array")
    candidate_ids = list(plan.get("candidate_ids") or [])
    by_candidate: dict[str, Mapping[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationHierarchyError("arbitration leaf assessment row must be an object")
        candidate_id = _nonempty_str(row.get("candidate_id"), "leaf assessment candidate_id")
        if candidate_id not in candidate_ids or candidate_id in by_candidate:
            raise ArbitrationHierarchyError("arbitration leaf has an unknown or duplicate candidate assessment")
        by_candidate[candidate_id] = row
    if set(by_candidate) != set(candidate_ids):
        raise ArbitrationHierarchyError("arbitration leaf must assess every candidate exactly once")

    expected_rows = leaf.get("expected_candidate_rows")
    if not isinstance(expected_rows, Mapping) or set(expected_rows) != set(candidate_ids):
        raise ArbitrationHierarchyError("arbitration leaf plan has malformed candidate expectations")
    normalized_rows: list[dict[str, Any]] = []
    claim_support_part_receipts_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for candidate_id in candidate_ids:
        row = by_candidate[candidate_id]
        expected = expected_rows[candidate_id]
        if not isinstance(expected, Mapping):
            raise ArbitrationHierarchyError("arbitration leaf plan candidate expectation is invalid")
        if row.get("candidate_hash") != expected.get("candidate_hash"):
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} has a stale candidate hash")
        expected_sections = list(expected.get("reviewed_section_ids") or [])
        observed_sections = _ordered_unique_ids(row.get("reviewed_section_ids"), f"{candidate_id}.reviewed_section_ids")
        if set(observed_sections) != set(expected_sections):
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} has missing or foreign section coverage")
        expected_hashes = dict(expected.get("section_hashes") or {})
        observed_hashes = row.get("section_hashes")
        if not isinstance(observed_hashes, Mapping) or dict(observed_hashes) != expected_hashes:
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} has stale or foreign section hashes")
        expected_presence = str(expected.get("presence") or "")
        if row.get("presence") != expected_presence:
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} presence does not match its assigned sections")
        assessments = {}
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment"):
            assessments[field] = _nonempty_str(row.get(field), f"{candidate_id}.{field}")
        advantages = _string_list(row.get("advantages"), f"{candidate_id}.advantages")
        limitations = _string_list(row.get("limitations"), f"{candidate_id}.limitations")
        evidence_refs = _ordered_unique_ids(row.get("evidence_refs"), f"{candidate_id}.evidence_refs")
        allowed_evidence = set(expected.get("allowed_evidence_refs") or [])
        if not set(evidence_refs).issubset(allowed_evidence):
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} cites evidence outside its source closure")
        if expected_presence == "present" and allowed_evidence and not evidence_refs:
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} omitted source evidence references")
        expected_issue_ids = list(expected.get("critic_issue_ids") or [])
        issue_ids = _ordered_unique_ids(row.get("critic_issue_ids"), f"{candidate_id}.critic_issue_ids")
        if set(issue_ids) != set(expected_issue_ids):
            raise ArbitrationHierarchyError(f"leaf assessment {candidate_id} omitted or added typed critic issues")
        expected_parts = expected.get("claim_support_part_receipts")
        if expected_parts is not None:
            observed_parts = row.get("claim_support_part_receipts")
            if not isinstance(observed_parts, list) or observed_parts != expected_parts:
                raise ArbitrationHierarchyError(
                    f"leaf assessment {candidate_id} has missing, foreign, or stale claim-support part receipts"
                )
            seen_part_ids: set[str] = set()
            for part_receipt in observed_parts:
                if not isinstance(part_receipt, Mapping) or set(part_receipt) != {
                    "part_id", "part_hash", "parent_section_bindings"
                }:
                    raise ArbitrationHierarchyError("leaf claim-support part receipt shape is invalid")
                part_id = _nonempty_str(part_receipt.get("part_id"), "leaf part receipt part_id")
                if part_id in seen_part_ids:
                    raise ArbitrationHierarchyError("leaf repeats a claim-support part receipt")
                seen_part_ids.add(part_id)
                _nonempty_str(part_receipt.get("part_hash"), "leaf part receipt part_hash")
                bindings = part_receipt.get("parent_section_bindings")
                if not isinstance(bindings, list) or not bindings:
                    raise ArbitrationHierarchyError("leaf part receipt omits original parent bindings")
                for binding in bindings:
                    if not isinstance(binding, Mapping) or set(binding) != {"section_id", "section_hash"}:
                        raise ArbitrationHierarchyError("leaf part receipt parent binding is malformed")
                    _nonempty_str(binding.get("section_id"), "leaf part parent section_id")
                    _nonempty_str(binding.get("section_hash"), "leaf part parent section_hash")
            claim_support_part_receipts_by_candidate[candidate_id] = copy.deepcopy(observed_parts)
        if expected_presence == "absent" and (advantages or limitations or evidence_refs or issue_ids):
            raise ArbitrationHierarchyError(f"absent candidate {candidate_id} must not invent section evidence")
        normalized_rows.append({
            "candidate_id": candidate_id,
            "candidate_hash": str(expected["candidate_hash"]),
            "presence": expected_presence,
            "reviewed_section_ids": expected_sections,
            "section_hashes": expected_hashes,
            **assessments,
            "advantages": list(advantages),
            "limitations": list(limitations),
            "evidence_refs": list(evidence_refs),
            "critic_issue_ids": expected_issue_ids,
        })
    result = {
        "leaf_id": leaf_id,
        "unit_ids": expected_unit_ids,
        "candidate_assessments": normalized_rows,
    }
    if claim_support_part_receipts_by_candidate:
        result["claim_support_part_receipts_by_candidate"] = claim_support_part_receipts_by_candidate
    result["leaf_result_hash"] = compute_v3_hash(result)
    return result


def build_arbitration_final_ledger(
    plan: Mapping[str, Any], leaf_results: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Build an all-candidate, all-section ledger after validating every leaf."""

    _validate_plan_integrity(plan)
    leaves = plan.get("leaves")
    if not isinstance(leaves, list):
        raise ArbitrationHierarchyError("arbitration plan leaves must be an array")
    expected_leaf_ids = [str(leaf.get("leaf_id") or "") for leaf in leaves if isinstance(leaf, Mapping)]
    if len(expected_leaf_ids) != len(leaves) or len(expected_leaf_ids) != len(set(expected_leaf_ids)):
        raise ArbitrationHierarchyError("arbitration plan has duplicate or malformed leaves")
    if not isinstance(leaf_results, Mapping) or set(leaf_results) != set(expected_leaf_ids):
        raise ArbitrationHierarchyError("final arbitration ledger requires every planned leaf exactly once")
    candidate_ids = list(plan.get("candidate_ids") or [])
    candidate_hashes = dict(plan.get("candidate_hashes") or {})
    plan_partition = plan.get("claim_support_partition")
    partitioned_pairs = _partitioned_candidate_section_pairs(plan_partition)
    seen_sections: set[tuple[str, str]] = set()
    assessment_rows: list[dict[str, Any]] = []
    leaf_result_receipts: list[dict[str, Any]] = []
    leaf_claim_slot_coverage: list[dict[str, Any]] = []
    leaf_part_receipts: list[dict[str, Any]] = []
    for leaf in sorted(leaves, key=lambda row: str(row.get("leaf_id") or "")):
        leaf_id = str(leaf.get("leaf_id") or "")
        result = validate_arbitration_leaf(plan, leaf_id, leaf_results[leaf_id])
        leaf_result_receipts.append({
            "leaf_id": result["leaf_id"],
            "unit_ids": list(result["unit_ids"]),
            "leaf_result_hash": result["leaf_result_hash"],
        })
        observed_part_receipts = result.get("claim_support_part_receipts_by_candidate")
        if observed_part_receipts is not None:
            expected_leaf_slots = _expected_leaf_claim_slot_coverage(plan, leaf)
            expected_parts_by_candidate = {
                candidate_id: list((leaf.get("expected_candidate_rows") or {}).get(candidate_id, {}).get(
                    "claim_support_part_receipts", []
                ))
                for candidate_id in candidate_ids
                if (leaf.get("expected_candidate_rows") or {}).get(candidate_id, {}).get(
                    "claim_support_part_receipts"
                ) is not None
            }
            if observed_part_receipts != expected_parts_by_candidate:
                raise ArbitrationHierarchyError("leaf claim-support part receipts differ from assigned parts")
            for candidate_id, part_receipts in sorted(observed_part_receipts.items()):
                leaf_part_receipts.append({
                    "leaf_id": leaf_id,
                    "candidate_id": candidate_id,
                    "part_receipts": copy.deepcopy(part_receipts),
                })
            for candidate_id, sections in sorted(expected_leaf_slots.items()):
                for section_id, slot_ids in sorted(sections.items()):
                    leaf_claim_slot_coverage.append({
                        "leaf_id": leaf_id,
                        "candidate_id": candidate_id,
                        "section_id": section_id,
                        "claim_slot_ids": list(slot_ids),
                    })
        for row in result["candidate_assessments"]:
            candidate_id = str(row["candidate_id"])
            if candidate_id not in candidate_ids or candidate_hashes.get(candidate_id) != row["candidate_hash"]:
                raise ArbitrationHierarchyError("final arbitration ledger candidate hash binding is invalid")
            for section_id in row["reviewed_section_ids"]:
                identity = (candidate_id, section_id)
                if identity in seen_sections and identity not in partitioned_pairs:
                    raise ArbitrationHierarchyError("final arbitration ledger contains duplicate section coverage")
                seen_sections.add(identity)
            assessment_rows.append({"leaf_id": leaf_id, **copy.deepcopy(row)})

    expected_sections = {
        (candidate_id, str(section["section_id"]))
        for candidate_id, sections in (plan.get("candidate_section_index") or {}).items()
        for section in sections
        if isinstance(section, Mapping) and str(section.get("section_id") or "")
    }
    if seen_sections != expected_sections:
        missing = sorted(expected_sections - seen_sections)
        foreign = sorted(seen_sections - expected_sections)
        raise ArbitrationHierarchyError(
            f"final arbitration ledger section coverage mismatch: missing={len(missing)} foreign={len(foreign)}"
        )
    if plan_partition is not None:
        _validate_final_claim_support_coverage(
            plan,
            leaf_claim_slot_coverage,
            assessment_rows,
        )
    if set(candidate_ids) != set(candidate_hashes):
        raise ArbitrationHierarchyError("final arbitration ledger candidate hashes are incomplete")
    ledger: dict[str, Any] = {
        "schema_version": _LEDGER_SCHEMA,
        "plan_hash": str(plan.get("plan_hash") or ""),
        "input_hash": str(plan.get("input_hash") or ""),
        "candidate_ids": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "critique_hashes": copy.deepcopy(dict(plan.get("critique_hashes") or {})),
        "provenance_hashes": copy.deepcopy(dict(plan.get("provenance_hashes") or {})),
        "candidate_section_index": copy.deepcopy(dict(plan.get("candidate_section_index") or {})),
        "critique_issues": copy.deepcopy(list(plan.get("critique_issues") or [])),
        "leaf_result_receipts": leaf_result_receipts,
        "assessments": sorted(
            assessment_rows,
            key=lambda row: (str(row["candidate_id"]), str(row["leaf_id"])),
        ),
        "coverage": {
            "expected_candidate_section_count": len(expected_sections),
            "observed_candidate_section_count": len(seen_sections),
            "candidate_section_pairs_hash": compute_v3_hash(sorted(expected_sections)),
            "leaf_result_hashes": {
                row["leaf_id"]: row["leaf_result_hash"] for row in leaf_result_receipts
            },
        },
    }
    if plan_partition is not None:
        typed_issue_coverage = _typed_issue_coverage_rows(plan)
        partition = copy.deepcopy(dict(plan_partition))
        partition["leaf_coverage"] = leaf_claim_slot_coverage
        partition["leaf_part_receipts"] = leaf_part_receipts
        partition["typed_issue_coverage"] = typed_issue_coverage
        partition["coverage_hash"] = compute_v3_hash({
            "leaf_coverage": leaf_claim_slot_coverage,
            "leaf_part_receipts": leaf_part_receipts,
            "typed_issue_coverage": typed_issue_coverage,
        })
        ledger["claim_support_partition"] = partition
    ledger["ledger_hash"] = compute_v3_hash(ledger)
    return ledger


def _request_size_failure(error: ArbitrationHierarchyError) -> bool:
    message = str(error)
    return "exceeds cap=" in message or "outside the effective provider profile budget" in message


def _split_oversized_claim_unit(
    unit: Mapping[str, Any],
    *,
    unit_sections: dict[str, dict[str, list[dict[str, Any]]]],
    candidate_ids: Sequence[str],
    build_measured_leaf: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]],
    partition_parent_rows: dict[str, list[dict[str, Any]]],
    original_error: ArbitrationHierarchyError,
) -> list[dict[str, Any]]:
    """Split one over-cap section only at complete task/primary-claim groups."""

    unit_key = str(unit.get("unit_key") or "")
    by_candidate = unit_sections.get(unit_key)
    if not isinstance(by_candidate, Mapping):
        raise original_error
    if any(len(by_candidate.get(candidate_id) or []) != 1 for candidate_id in candidate_ids):
        raise original_error

    parent_unit_id = str(unit.get("claim_support_parent_unit_id") or unit.get("unit_id") or "")
    records_by_candidate: dict[str, list[dict[str, Any]]] = {}
    sections_by_candidate: dict[str, Mapping[str, Any]] = {}
    for candidate_id in candidate_ids:
        item = by_candidate[candidate_id][0]
        full_section = item.get("parent_section", item.get("section"))
        if not isinstance(full_section, Mapping):
            raise original_error
        section_id = str(item.get("section_id") or "")
        section_records = _claim_support_slot_records(full_section, candidate_id, section_id)
        if section_records is None:
            raise original_error
        allowed_slots = item.get("claim_slot_ids")
        allowed = set(allowed_slots) if allowed_slots is not None else None
        if allowed is not None and (not allowed or not allowed.issubset({row["claim_slot_id"] for row in section_records})):
            raise original_error
        records_by_candidate[candidate_id] = [
            row for row in section_records if allowed is None or row["claim_slot_id"] in allowed
        ]
        sections_by_candidate[candidate_id] = full_section

    grouped_by_candidate = {
        candidate_id: _ordered_claim_support_groups(records_by_candidate[candidate_id])
        for candidate_id in candidate_ids
    }
    reference_groups = grouped_by_candidate[candidate_ids[0]]
    if len(reference_groups) < 2:
        raise original_error
    for candidate_id in candidate_ids[1:]:
        observed = grouped_by_candidate[candidate_id]
        if [row["comparison_group_id"] for row in observed] != [
            row["comparison_group_id"] for row in reference_groups
        ] or [row["claim_slot_ids"] for row in observed] != [
            row["claim_slot_ids"] for row in reference_groups
        ] or [row["comparison_roles"] for row in observed] != [
            row["comparison_roles"] for row in reference_groups
        ]:
            raise original_error

    if parent_unit_id not in partition_parent_rows:
        parent_rows: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            item = by_candidate[candidate_id][0]
            section = sections_by_candidate[candidate_id]
            parent_rows.append({
                "comparison_parent_unit_id": parent_unit_id,
                "candidate_id": candidate_id,
                "section_id": str(item["section_id"]),
                "section_hash": str(item["section_hash"]),
                "source_closure_hash": compute_v3_hash(section.get("claim_support") or []),
                "claim_count": len(section.get("claims") or []),
                "support_row_count": len(section.get("claim_support") or []),
                "slots": [
                    {
                        "claim_slot_id": row["claim_slot_id"],
                        "claim_occurrence_index": row["claim_occurrence_index"],
                        "support_row_index": row["support_row_index"],
                        "claim_hash": row["claim_hash"],
                        "support_row_hash": row["support_row_hash"],
                        "comparison_scope_id": row["comparison_scope_id"],
                        "comparison_group_id": row["comparison_group_id"],
                        "task_id": row["task_id"],
                        "primary_claim_id": row["primary_claim_id"],
                        "comparison_role": row["comparison_role"],
                        "claim_target_ids": list(row["claim_target_ids"]),
                    }
                    for row in records_by_candidate[candidate_id]
                ],
            })
        partition_parent_rows[parent_unit_id] = parent_rows

    def make_part(groups: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        group_ids = [str(group["comparison_group_id"]) for group in groups]
        slot_ids = [slot_id for group in groups for slot_id in group["claim_slot_ids"]]
        bindings_by_candidate: dict[str, list[dict[str, Any]]] = {}
        raw_part_items: dict[str, list[dict[str, Any]]] = {}
        selected_tasks: set[str] = set()
        for group in groups:
            selected_tasks.update(str(task_id) for task_id in group["task_ids"])
        for candidate_id in candidate_ids:
            source_item = by_candidate[candidate_id][0]
            section = sections_by_candidate[candidate_id]
            records = {row["claim_slot_id"]: row for row in records_by_candidate[candidate_id]}
            if set(slot_ids) - set(records):
                raise ArbitrationHierarchyError("comparison part references a claim slot absent from an alternative")
            candidate_slots = [
                row["claim_slot_id"] for row in records_by_candidate[candidate_id]
                if row["claim_slot_id"] in set(slot_ids)
            ]
            projected = _project_section_to_claim_slots(
                section, set(candidate_slots), candidate_id, str(source_item["section_id"])
            )
            slot_rows = [records[slot_id] for slot_id in candidate_slots]
            claim_hashes = {row["claim_slot_id"]: row["claim_hash"] for row in slot_rows}
            support_hashes = {row["claim_slot_id"]: row["support_row_hash"] for row in slot_rows}
            group_hashes = {row["claim_slot_id"]: row["comparison_group_id"] for row in slot_rows}
            occurrence_indices = {
                row["claim_slot_id"]: row["claim_occurrence_index"] for row in slot_rows
            }
            support_row_indices = {
                row["claim_slot_id"]: row["support_row_index"] for row in slot_rows
            }
            binding = {
                "section_id": str(source_item["section_id"]),
                "section_hash": str(source_item["section_hash"]),
                "claim_slot_ids": candidate_slots,
                "claim_hashes": claim_hashes,
                "support_row_hashes": support_hashes,
                "comparison_group_ids": group_hashes,
                "claim_occurrence_indices": occurrence_indices,
                "support_row_indices": support_row_indices,
            }
            bindings_by_candidate[candidate_id] = [binding]
            raw_part_items[candidate_id] = [{
                "section_id": str(source_item["section_id"]),
                "section_hash": str(source_item["section_hash"]),
                "task_ids": list(source_item["task_ids"]),
                "section": projected,
                "parent_section": section,
                "position": source_item["position"],
                "claim_slot_ids": candidate_slots,
                "claim_hashes": claim_hashes,
                "support_row_hashes": support_hashes,
                "comparison_group_ids": group_hashes,
            }]
        part_identity = {
            "comparison_parent_unit_id": parent_unit_id,
            "groups": group_ids,
            "bindings_by_candidate": bindings_by_candidate,
        }
        part_hash = compute_v3_hash(part_identity)
        part_key = f"{unit_key}:claim-support-part:{part_hash[:24]}"
        part_id = f"claim-support-part:{part_hash[:24]}"
        unit_sections[part_key] = raw_part_items
        descriptor = {
            "unit_id": part_id,
            "unit_key": part_key,
            "task_ids": sorted(selected_tasks),
            "sections_by_candidate": {
                candidate_id: [
                    {
                        "section_id": binding["section_id"],
                        "section_hash": binding["section_hash"],
                        "task_ids": list(by_candidate[candidate_id][0]["task_ids"]),
                        "claim_slot_ids": list(binding["claim_slot_ids"]),
                    }
                    for binding in bindings_by_candidate[candidate_id]
                ]
                for candidate_id in candidate_ids
            },
            "claim_support_part": True,
            "claim_support_part_hash": part_hash,
            "claim_support_parent_unit_id": parent_unit_id,
            "claim_support_bindings_by_section": bindings_by_candidate,
            "claim_support_slot_ids_by_section": {
                candidate_id: {
                    binding["section_id"]: list(binding["claim_slot_ids"])
                    for binding in bindings_by_candidate[candidate_id]
                }
                for candidate_id in candidate_ids
            },
        }
        return descriptor

    result: list[dict[str, Any]] = []
    active_groups: list[Mapping[str, Any]] = []
    for group in reference_groups:
        trial_groups = [*active_groups, group]
        trial_unit = make_part(trial_groups)
        try:
            build_measured_leaf([trial_unit])
        except ArbitrationHierarchyError as exc:
            if not _request_size_failure(exc):
                raise
            if not active_groups:
                leaf_id = "arbitration-leaf:" + compute_v3_hash({
                    "plan_input_hash": "0" * 64,
                    "unit_ids": [str(trial_unit["unit_id"])],
                })[:24]
                raise ArbitrationHierarchyError(
                    f"indivisible section leaf {leaf_id} contains an atomic claim-support comparison group "
                    f"that exceeds cap; {exc}"
                ) from original_error
            result.append(make_part(active_groups))
            active_groups = [group]
            singleton = make_part(active_groups)
            try:
                build_measured_leaf([singleton])
            except ArbitrationHierarchyError as singleton_error:
                if _request_size_failure(singleton_error):
                    raise ArbitrationHierarchyError(
                        f"indivisible section leaf {singleton['unit_id']} contains an atomic claim-support "
                        f"comparison group that exceeds cap; {singleton_error}"
                    ) from original_error
                raise
        else:
            active_groups = trial_groups
    if active_groups:
        result.append(make_part(active_groups))
    if len(result) < 2:
        raise original_error
    return result


def _claim_support_slot_records(
    section: Mapping[str, Any], candidate_id: str, section_id: str
) -> list[dict[str, Any]] | None:
    claims = section.get("claims")
    support = section.get("claim_support")
    if (
        not isinstance(claims, list)
        or not claims
        or any(not isinstance(claim, str) for claim in claims)
        or not isinstance(support, list)
        or not support
    ):
        return None
    positions_by_claim: dict[str, list[int]] = {}
    for occurrence_index, claim in enumerate(claims):
        positions_by_claim.setdefault(claim, []).append(occurrence_index)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for support_row_index, raw_row in enumerate(support):
        if not isinstance(raw_row, Mapping):
            return None
        claim = raw_row.get("claim")
        if not isinstance(claim, str) or claim not in positions_by_claim:
            return None
        declared_occurrence = raw_row.get("claim_occurrence_index")
        if declared_occurrence is None:
            matching_occurrences = positions_by_claim[claim]
            if len(matching_occurrences) != 1:
                return None
            claim_occurrence_index = matching_occurrences[0]
        else:
            if (
                type(declared_occurrence) is not int
                or declared_occurrence < 0
                or declared_occurrence >= len(claims)
                or claims[declared_occurrence] != claim
            ):
                return None
            claim_occurrence_index = declared_occurrence
        slot_id = raw_row.get("claim_slot_id")
        task_id = raw_row.get("task_id")
        primary_claim_id = raw_row.get("primary_claim_id")
        if (
            not isinstance(slot_id, str)
            or not slot_id.strip()
            or not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(primary_claim_id, str)
            or not primary_claim_id.strip()
        ):
            return None
        if slot_id in seen:
            return None
        seen.add(slot_id)
        comparison_role = raw_row.get("comparison_role")
        if comparison_role is not None and comparison_role not in {"A", "B"}:
            return None
        comparison_scope_id = compute_v3_hash({
            "task_id": task_id,
            "primary_claim_id": primary_claim_id,
        })
        result.append({
            "claim_slot_id": str(slot_id),
            "claim_occurrence_index": claim_occurrence_index,
            "support_row_index": support_row_index,
            "claim_hash": compute_v3_hash(claim),
            "support_row_hash": compute_v3_hash(raw_row),
            "comparison_scope_id": comparison_scope_id,
            "comparison_group_id": comparison_scope_id,
            "comparison_role": comparison_role,
            "task_id": str(task_id),
            "primary_claim_id": str(primary_claim_id),
            "claim_target_ids": sorted(_section_claim_ids({"claim_support": [raw_row]})),
        })
    return result


def _ordered_claim_support_groups(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    explicit_roles_by_task: dict[str, set[str]] = {}
    for row in records:
        role = row.get("comparison_role")
        if isinstance(role, str) and role in {"A", "B"}:
            explicit_roles_by_task.setdefault(str(row["task_id"]), set()).add(role)
    if any(roles != {"A", "B"} for roles in explicit_roles_by_task.values()):
        raise ArbitrationHierarchyError("explicit A/B comparison task is incomplete")

    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    # All support rows for one authored claim occurrence travel together,
    # even when they identify different primary source claims. Comparison
    # scopes then join distinct authored occurrences only when both explicit
    # task_id and primary_claim_id match. Shared evidence/qualifier/source IDs
    # never participate in the grouping relation.
    by_occurrence: dict[int, list[int]] = {}
    by_primary_scope: dict[str, list[int]] = {}
    for index, row in enumerate(records):
        by_occurrence.setdefault(int(row["claim_occurrence_index"]), []).append(index)
        by_primary_scope.setdefault(str(row["comparison_scope_id"]), []).append(index)
    for rows in (*by_occurrence.values(), *by_primary_scope.values()):
        for index in rows[1:]:
            union(rows[0], index)

    components: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(records):
        components.setdefault(find(index), []).append(row)
    result: list[dict[str, Any]] = []
    for component in components.values():
        slot_ids = [str(row["claim_slot_id"]) for row in component]
        occurrence_indices = sorted({int(row["claim_occurrence_index"]) for row in component})
        task_ids = sorted({str(row["task_id"]) for row in component})
        primary_claim_ids = sorted({str(row["primary_claim_id"]) for row in component})
        roles = [row.get("comparison_role") for row in component]
        if any(role is not None for role in roles) and (
            any(role is None for role in roles) or set(roles) != {"A", "B"}
        ):
            raise ArbitrationHierarchyError(
                "explicit A/B comparison group is incomplete within its authored-claim/task/primary scope"
            )
        group_id = compute_v3_hash({
            "claim_slot_ids": slot_ids,
            "claim_occurrence_indices": occurrence_indices,
            "task_ids": task_ids,
            "primary_claim_ids": primary_claim_ids,
            "comparison_scope_ids": sorted({str(row["comparison_scope_id"]) for row in component}),
            "comparison_roles": roles,
        })
        for row in component:
            row["comparison_group_id"] = group_id
        result.append({
            "comparison_group_id": group_id,
            "comparison_scope_ids": sorted({str(row["comparison_scope_id"]) for row in component}),
            "claim_occurrence_indices": occurrence_indices,
            "task_ids": task_ids,
            "primary_claim_ids": primary_claim_ids,
            "claim_slot_ids": slot_ids,
            "comparison_roles": roles,
        })
    return result


def _project_section_to_claim_slots(
    section: Mapping[str, Any], slot_ids: set[str], candidate_id: str, section_id: str
) -> dict[str, Any]:
    records = _claim_support_slot_records(section, candidate_id, section_id)
    if records is None:
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} cannot prove exact claim/support row alignment")
    selected = [row for row in records if row["claim_slot_id"] in slot_ids]
    if {str(row["claim_slot_id"]) for row in selected} != slot_ids:
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} claim-support part has foreign slots")
    support_by_slot = {
        str(row["claim_slot_id"]): raw_row
        for row, raw_row in zip(records, section.get("claim_support") or [], strict=True)
    }
    claims = list(section.get("claims") or [])
    result = copy.deepcopy(dict(section))
    selected_records = [row for row in records if row["claim_slot_id"] in slot_ids]
    selected_occurrences = sorted({int(row["claim_occurrence_index"]) for row in selected_records})
    result["claims"] = [claims[index] for index in selected_occurrences]
    # Each complete support row, including qualifier/source identities, stays
    # intact. Shared IDs may recur in separate rows and are never merged.
    result["claim_support"] = [
        copy.deepcopy(dict(support_by_slot[row["claim_slot_id"]])) for row in selected_records
    ]
    paper_keys = sorted({str(row["paper_key"]) for row in result["claim_support"]})
    result["paper_keys"] = paper_keys
    if isinstance(result.get("paper_roles"), Mapping):
        result["paper_roles"] = {
            key: copy.deepcopy(value) for key, value in result["paper_roles"].items()
            if key in paper_keys
        }
    result["task_ids"] = sorted({str(row["task_id"]) for row in result["claim_support"] if row.get("task_id")})
    relation_ids = sorted({
        str(relation_id)
        for row in result["claim_support"]
        for relation_id in row.get("relation_ids", [])
        if isinstance(relation_id, str) and relation_id.strip()
    })
    if "relation_ids" in result:
        result["relation_ids"] = relation_ids
    return result


def _claim_support_partition_for_units(
    units: Sequence[Mapping[str, Any]], parent_rows: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any] | None:
    if not parent_rows:
        return None
    assignments: list[dict[str, Any]] = []
    for unit in units:
        if unit.get("claim_support_part") is not True:
            continue
        assignments.append({
            "unit_id": str(unit["unit_id"]),
            "part_hash": str(unit["claim_support_part_hash"]),
            "comparison_parent_unit_id": str(unit["claim_support_parent_unit_id"]),
            "bindings_by_candidate": copy.deepcopy(dict(unit["claim_support_bindings_by_section"])),
        })
    return {
        "schema_version": _CLAIM_SUPPORT_PARTITION_SCHEMA,
        "parent_sections": [
            copy.deepcopy(dict(row))
            for parent_id in sorted(parent_rows)
            for row in sorted(parent_rows[parent_id], key=lambda item: str(item["candidate_id"]))
        ],
        "unit_assignments": sorted(assignments, key=lambda row: str(row["unit_id"])),
    }


def _validate_plan_claim_support_partition(plan: Mapping[str, Any]) -> None:
    partition = plan.get("claim_support_partition")
    units = plan.get("units")
    if not isinstance(units, list):
        raise ArbitrationHierarchyError("arbitration plan units are invalid")
    part_units = [row for row in units if isinstance(row, Mapping) and row.get("claim_support_part") is True]
    if partition is None:
        if part_units:
            raise ArbitrationHierarchyError("claim-support part units require an explicit partition manifest")
        return
    if not isinstance(partition, Mapping) or partition.get("schema_version") != _CLAIM_SUPPORT_PARTITION_SCHEMA:
        raise ArbitrationHierarchyError("claim-support partition schema is invalid")
    candidate_ids = list(plan.get("candidate_ids") or [])
    index = plan.get("candidate_section_index")
    parent_rows = partition.get("parent_sections")
    assignment_rows = partition.get("unit_assignments")
    if (
        not isinstance(index, Mapping)
        or not isinstance(parent_rows, list)
        or not parent_rows
        or not isinstance(assignment_rows, list)
        or not assignment_rows
    ):
        raise ArbitrationHierarchyError("claim-support partition manifest is incomplete")

    parents: dict[tuple[str, str], Mapping[str, Any]] = {}
    parents_by_comparison_unit: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row_index, parent in enumerate(parent_rows):
        if not isinstance(parent, Mapping):
            raise ArbitrationHierarchyError(f"claim-support parent row {row_index} is invalid")
        candidate_id = _nonempty_str(parent.get("candidate_id"), "claim-support parent candidate_id")
        section_id = _nonempty_str(parent.get("section_id"), "claim-support parent section_id")
        comparison_parent_id = _nonempty_str(
            parent.get("comparison_parent_unit_id"), "claim-support comparison parent unit_id"
        )
        pair = (candidate_id, section_id)
        if candidate_id not in candidate_ids or pair in parents:
            raise ArbitrationHierarchyError("claim-support partition has a foreign or duplicate parent section")
        candidate_index_rows = index.get(candidate_id)
        matching_index = [
            row for row in candidate_index_rows or ()
            if isinstance(row, Mapping) and row.get("section_id") == section_id
        ]
        if len(matching_index) != 1:
            raise ArbitrationHierarchyError("claim-support partition parent is absent from the section index")
        index_row = matching_index[0]
        if (
            parent.get("section_hash") != index_row.get("section_hash")
            or parent.get("source_closure_hash") != index_row.get("source_closure_hash")
            or parent.get("claim_count") != index_row.get("claim_count")
            or parent.get("support_row_count") != index_row.get("support_row_count")
        ):
            raise ArbitrationHierarchyError("claim-support partition parent hash is stale")
        claim_count = index_row.get("claim_count")
        support_row_count = index_row.get("support_row_count")
        if (
            type(claim_count) is not int
            or claim_count < 1
            or type(support_row_count) is not int
            or support_row_count < 1
        ):
            raise ArbitrationHierarchyError("claim-support parent occurrence/support counts are invalid")
        slots = parent.get("slots")
        if not isinstance(slots, list) or len(slots) < 2 or len(slots) != support_row_count:
            raise ArbitrationHierarchyError("claim-support parent must contain multiple exact slot records")
        seen_slots: set[str] = set()
        seen_support_rows: set[int] = set()
        seen_occurrences: set[int] = set()
        roles_by_group: dict[str, list[str | None]] = {}
        normalized_group_rows: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(slots):
            if not isinstance(slot, Mapping):
                raise ArbitrationHierarchyError("claim-support slot record is invalid")
            slot_id = _nonempty_str(slot.get("claim_slot_id"), "claim-support claim_slot_id")
            if slot_id in seen_slots:
                raise ArbitrationHierarchyError("claim-support parent repeats a claim slot")
            seen_slots.add(slot_id)
            occurrence_index = slot.get("claim_occurrence_index")
            if type(occurrence_index) is not int or not 0 <= occurrence_index < claim_count:
                raise ArbitrationHierarchyError("claim-support occurrence index is invalid")
            seen_occurrences.add(occurrence_index)
            support_row_index = slot.get("support_row_index")
            if (
                type(support_row_index) is not int
                or support_row_index != slot_index
                or not 0 <= support_row_index < support_row_count
                or support_row_index in seen_support_rows
            ):
                raise ArbitrationHierarchyError("claim-support support-row index is invalid or aliased")
            seen_support_rows.add(support_row_index)
            task_id = _nonempty_str(slot.get("task_id"), "claim-support task_id")
            primary_claim_id = _nonempty_str(slot.get("primary_claim_id"), "claim-support primary_claim_id")
            expected_scope_id = compute_v3_hash({"task_id": task_id, "primary_claim_id": primary_claim_id})
            if slot.get("comparison_scope_id") != expected_scope_id:
                raise ArbitrationHierarchyError("claim-support task/primary scope binding is stale")
            for field in ("claim_hash", "support_row_hash", "comparison_group_id"):
                _nonempty_str(slot.get(field), f"claim-support {field}")
            role = slot.get("comparison_role")
            if role is not None and role not in {"A", "B"}:
                raise ArbitrationHierarchyError("claim-support comparison role is unsupported")
            roles_by_group.setdefault(str(slot["comparison_group_id"]), []).append(role)
            _string_list(slot.get("claim_target_ids"), "claim-support claim_target_ids")
            normalized_group_rows.append({
                "claim_slot_id": slot_id,
                "claim_occurrence_index": occurrence_index,
                "support_row_index": support_row_index,
                "task_id": task_id,
                "primary_claim_id": primary_claim_id,
                "comparison_scope_id": expected_scope_id,
                "comparison_group_id": str(slot["comparison_group_id"]),
                "comparison_role": role,
            })
        if seen_support_rows != set(range(support_row_count)):
            raise ArbitrationHierarchyError("claim-support support-row coverage is not exact")
        if seen_occurrences != set(range(claim_count)):
            raise ArbitrationHierarchyError("claim-support authored claim-occurrence coverage is not exact")
        for roles in roles_by_group.values():
            if any(role is not None for role in roles) and (
                any(role is None for role in roles) or set(roles) != {"A", "B"}
            ):
                raise ArbitrationHierarchyError("claim-support explicit A/B comparison group is incomplete")
        derived_groups = _ordered_claim_support_groups(normalized_group_rows)
        derived_group_by_slot = {
            slot_id: group["comparison_group_id"]
            for group in derived_groups
            for slot_id in group["claim_slot_ids"]
        }
        if any(
            row["comparison_group_id"] != derived_group_by_slot[row["claim_slot_id"]]
            for row in normalized_group_rows
        ):
            raise ArbitrationHierarchyError("claim-support comparison groups do not bind authored occurrences")
        parents[pair] = parent
        group = parents_by_comparison_unit.setdefault(comparison_parent_id, {})
        if candidate_id in group:
            raise ArbitrationHierarchyError("claim-support comparison parent repeats a candidate")
        group[candidate_id] = parent

    for comparison_parent_id, by_candidate in parents_by_comparison_unit.items():
        if set(by_candidate) != set(candidate_ids):
            raise ArbitrationHierarchyError(
                f"claim-support comparison parent {comparison_parent_id} does not include every candidate"
            )
        reference = by_candidate[candidate_ids[0]]["slots"]
        reference_signature = [
            (row["claim_slot_id"], row["comparison_group_id"], row.get("comparison_role"))
            for row in reference
        ]
        for candidate_id in candidate_ids[1:]:
            observed_signature = [
                (row["claim_slot_id"], row["comparison_group_id"], row.get("comparison_role"))
                for row in by_candidate[candidate_id]["slots"]
            ]
            if observed_signature != reference_signature:
                raise ArbitrationHierarchyError("claim-support slots are not aligned across candidate alternatives")

    part_units_by_id = {str(row["unit_id"]): row for row in part_units}
    if len(part_units_by_id) != len(part_units):
        raise ArbitrationHierarchyError("claim-support part unit IDs are not unique")
    assignments_by_id: dict[str, Mapping[str, Any]] = {}
    for assignment in assignment_rows:
        if not isinstance(assignment, Mapping):
            raise ArbitrationHierarchyError("claim-support unit assignment is invalid")
        unit_id = _nonempty_str(assignment.get("unit_id"), "claim-support assignment unit_id")
        if unit_id in assignments_by_id:
            raise ArbitrationHierarchyError("claim-support unit assignment is duplicated")
        assignments_by_id[unit_id] = assignment
    if set(assignments_by_id) != set(part_units_by_id):
        raise ArbitrationHierarchyError("claim-support assignments differ from planned part units")

    assigned_slots: dict[tuple[str, str], list[str]] = {pair: [] for pair in parents}
    for unit_id, unit in part_units_by_id.items():
        assignment = assignments_by_id[unit_id]
        parent_id = _nonempty_str(unit.get("claim_support_parent_unit_id"), "claim-support part parent unit_id")
        part_hash = _nonempty_str(unit.get("claim_support_part_hash"), "claim-support part hash")
        if assignment.get("part_hash") != part_hash or unit_id != f"claim-support-part:{part_hash[:24]}":
            raise ArbitrationHierarchyError("claim-support part identity hash is stale")
        if assignment.get("comparison_parent_unit_id") != parent_id:
            raise ArbitrationHierarchyError("claim-support unit assignment has a foreign comparison parent")
        bindings = unit.get("claim_support_bindings_by_section")
        if not isinstance(bindings, Mapping) or set(bindings) != set(candidate_ids):
            raise ArbitrationHierarchyError("claim-support unit alternative bindings are incomplete")
        expected_slot_ids_by_section: dict[str, dict[str, list[str]]] = {}
        for candidate_id in candidate_ids:
            candidate_bindings = bindings[candidate_id]
            if not isinstance(candidate_bindings, list) or len(candidate_bindings) != 1:
                raise ArbitrationHierarchyError(
                    "claim-support unit must bind one original parent section per candidate"
                )
            binding = candidate_bindings[0]
            if not isinstance(binding, Mapping) or not isinstance(binding.get("claim_slot_ids"), list):
                raise ArbitrationHierarchyError("claim-support unit candidate binding is malformed")
            expected_slot_ids_by_section[candidate_id] = {
                str(binding.get("section_id") or ""): list(binding["claim_slot_ids"])
            }
        if unit.get("claim_support_slot_ids_by_section") != expected_slot_ids_by_section:
            raise ArbitrationHierarchyError("claim-support slot projection differs from its bound part rows")
        if assignment.get("bindings_by_candidate") != bindings:
            raise ArbitrationHierarchyError("claim-support unit assignment differs from its planned bindings")
        signatures_by_candidate: list[list[tuple[str, str, Any, Any]]] = []
        for candidate_id in candidate_ids:
            candidate_bindings = bindings[candidate_id]
            if not isinstance(candidate_bindings, list) or len(candidate_bindings) != 1:
                raise ArbitrationHierarchyError("claim-support unit must bind one original parent section per candidate")
            binding = candidate_bindings[0]
            if not isinstance(binding, Mapping):
                raise ArbitrationHierarchyError("claim-support candidate binding is invalid")
            section_id = _nonempty_str(binding.get("section_id"), "claim-support binding section_id")
            pair = (candidate_id, section_id)
            parent = parents.get(pair)
            if parent is None or parent.get("comparison_parent_unit_id") != parent_id:
                raise ArbitrationHierarchyError("claim-support unit binds a foreign parent section")
            if binding.get("section_hash") != parent.get("section_hash"):
                raise ArbitrationHierarchyError("claim-support unit parent section hash is stale")
            slots = list(_ordered_unique_ids(binding.get("claim_slot_ids"), "claim-support unit claim_slot_ids"))
            claim_hashes = binding.get("claim_hashes")
            support_hashes = binding.get("support_row_hashes")
            group_ids = binding.get("comparison_group_ids")
            occurrence_indices = binding.get("claim_occurrence_indices")
            support_row_indices = binding.get("support_row_indices")
            if (
                not isinstance(claim_hashes, Mapping)
                or set(claim_hashes) != set(slots)
                or not isinstance(support_hashes, Mapping)
                or set(support_hashes) != set(slots)
                or not isinstance(group_ids, Mapping)
                or set(group_ids) != set(slots)
                or not isinstance(occurrence_indices, Mapping)
                or set(occurrence_indices) != set(slots)
                or not isinstance(support_row_indices, Mapping)
                or set(support_row_indices) != set(slots)
            ):
                raise ArbitrationHierarchyError("claim-support unit slot hashes are incomplete")
            parent_slots = {str(row["claim_slot_id"]): row for row in parent["slots"]}
            if not set(slots).issubset(parent_slots):
                raise ArbitrationHierarchyError("claim-support unit contains a foreign slot")
            for slot_id in slots:
                slot = parent_slots[slot_id]
                if (
                    claim_hashes[slot_id] != slot["claim_hash"]
                    or support_hashes[slot_id] != slot["support_row_hash"]
                    or group_ids[slot_id] != slot["comparison_group_id"]
                    or occurrence_indices[slot_id] != slot["claim_occurrence_index"]
                    or support_row_indices[slot_id] != slot["support_row_index"]
                ):
                    raise ArbitrationHierarchyError("claim-support unit slot/occurrence binding is stale")
            group_members: dict[str, set[str]] = {}
            for slot in parent["slots"]:
                group_members.setdefault(str(slot["comparison_group_id"]), set()).add(str(slot["claim_slot_id"]))
            selected = set(slots)
            if any(selected.intersection(members) and not members.issubset(selected)
                   for members in group_members.values()):
                raise ArbitrationHierarchyError("claim-support unit splits an atomic task/primary-claim group")
            signatures_by_candidate.append([
                (slot_id, str(group_ids[slot_id]), occurrence_indices[slot_id], support_row_indices[slot_id])
                for slot_id in slots
            ])
            assigned_slots[pair].extend(slots)
        if any(signature != signatures_by_candidate[0] for signature in signatures_by_candidate[1:]):
            raise ArbitrationHierarchyError("claim-support part alternatives do not share exact slot coverage")
        first_binding = bindings[candidate_ids[0]][0]
        ordered_group_ids = list(dict.fromkeys(
            str(first_binding["comparison_group_ids"][slot_id])
            for slot_id in first_binding["claim_slot_ids"]
        ))
        expected_part_hash = compute_v3_hash({
            "comparison_parent_unit_id": parent_id,
            "groups": ordered_group_ids,
            "bindings_by_candidate": bindings,
        })
        if expected_part_hash != part_hash:
            raise ArbitrationHierarchyError("claim-support part hash does not bind its exact parent/slot coverage")

    for pair, parent in parents.items():
        expected_slots = [str(row["claim_slot_id"]) for row in parent["slots"]]
        observed = assigned_slots[pair]
        if len(observed) != len(set(observed)) or set(observed) != set(expected_slots):
            raise ArbitrationHierarchyError("claim-support unit assignments are not disjoint and exhaustive")


def _partitioned_candidate_section_pairs(partition: Any) -> set[tuple[str, str]]:
    if not isinstance(partition, Mapping):
        return set()
    return {
        (str(row.get("candidate_id") or ""), str(row.get("section_id") or ""))
        for row in partition.get("parent_sections") or []
        if isinstance(row, Mapping)
    }


def _expected_leaf_claim_slot_coverage(
    plan: Mapping[str, Any], leaf: Mapping[str, Any]
) -> dict[str, dict[str, list[str]]]:
    units = plan.get("units")
    if not isinstance(units, list):
        raise ArbitrationHierarchyError("claim-slot leaf plan units are invalid")
    by_id = {str(row.get("unit_id") or ""): row for row in units if isinstance(row, Mapping)}
    coverage: dict[str, dict[str, list[str]]] = {}
    for unit_id in leaf.get("unit_ids") or []:
        unit = by_id.get(str(unit_id))
        if unit is None:
            raise ArbitrationHierarchyError("claim-slot leaf references an unknown unit")
        bindings = unit.get("claim_support_slot_ids_by_section")
        if not isinstance(bindings, Mapping):
            continue
        for candidate_id, by_section in bindings.items():
            if not isinstance(by_section, Mapping):
                raise ArbitrationHierarchyError("claim-slot unit bindings are malformed")
            for section_id, raw_slots in by_section.items():
                slot_ids = list(_ordered_unique_ids(raw_slots, "unit claim_slot_ids"))
                existing = coverage.setdefault(str(candidate_id), {}).setdefault(str(section_id), [])
                if set(existing).intersection(slot_ids):
                    raise ArbitrationHierarchyError("leaf units duplicate claim-slot coverage")
                existing.extend(slot_ids)
    return {
        candidate_id: {section_id: slots for section_id, slots in sorted(by_section.items())}
        for candidate_id, by_section in sorted(coverage.items())
    }


def _validate_final_claim_support_coverage(
    plan: Mapping[str, Any],
    leaf_coverage: Sequence[Mapping[str, Any]],
    assessment_rows: Sequence[Mapping[str, Any]],
) -> None:
    partition = plan.get("claim_support_partition")
    if not isinstance(partition, Mapping):
        raise ArbitrationHierarchyError("claim-support result has no plan partition")
    parents = partition.get("parent_sections")
    if not isinstance(parents, list):
        raise ArbitrationHierarchyError("claim-support parent manifests are malformed")
    expected_by_parent: dict[tuple[str, str], list[str]] = {}
    for parent in parents:
        candidate_id = str(parent.get("candidate_id") or "")
        section_id = str(parent.get("section_id") or "")
        pair = (candidate_id, section_id)
        slots = parent.get("slots")
        if pair in expected_by_parent or not isinstance(slots, list):
            raise ArbitrationHierarchyError("claim-support parent manifest duplicates or omits a section")
        expected_by_parent[pair] = [str(row["claim_slot_id"]) for row in slots]

    expected_leaf_rows = []
    for leaf in plan.get("leaves") or []:
        coverage = _expected_leaf_claim_slot_coverage(plan, leaf)
        for candidate_id, sections in coverage.items():
            for section_id, slot_ids in sections.items():
                expected_leaf_rows.append({
                    "leaf_id": str(leaf["leaf_id"]),
                    "candidate_id": candidate_id,
                    "section_id": section_id,
                    "claim_slot_ids": list(slot_ids),
                })
    if list(leaf_coverage) != expected_leaf_rows:
        if sorted(leaf_coverage, key=_claim_coverage_sort_key) != sorted(expected_leaf_rows, key=_claim_coverage_sort_key):
            raise ArbitrationHierarchyError("final claim-support leaf coverage is not exact against the plan")

    observed_by_parent: dict[tuple[str, str], list[str]] = {}
    observed_leaf_pairs: set[tuple[str, str, str]] = set()
    for row in leaf_coverage:
        candidate_id = str(row.get("candidate_id") or "")
        section_id = str(row.get("section_id") or "")
        leaf_id = str(row.get("leaf_id") or "")
        pair = (candidate_id, section_id)
        if pair not in expected_by_parent:
            raise ArbitrationHierarchyError("final claim-support coverage names a foreign parent section")
        identity = (leaf_id, candidate_id, section_id)
        if identity in observed_leaf_pairs:
            raise ArbitrationHierarchyError("final claim-support coverage duplicates a leaf parent section")
        observed_leaf_pairs.add(identity)
        slot_ids = list(_ordered_unique_ids(row.get("claim_slot_ids"), "leaf claim_slot_ids"))
        if not set(slot_ids).issubset(set(expected_by_parent[pair])):
            raise ArbitrationHierarchyError("final claim-support coverage names a foreign claim slot")
        observed_by_parent.setdefault(pair, []).extend(slot_ids)
    for pair, expected_slots in expected_by_parent.items():
        observed = observed_by_parent.get(pair, [])
        if len(observed) != len(set(observed)) or set(observed) != set(expected_slots):
            raise ArbitrationHierarchyError("final claim-support coverage is not disjoint and exhaustive")

    assessments_by_leaf_candidate: dict[tuple[str, str], Mapping[str, Any]] = {
        (str(row.get("leaf_id") or ""), str(row.get("candidate_id") or "")): row
        for row in assessment_rows
    }
    for leaf_id, candidate_id, section_id in observed_leaf_pairs:
        assessment = assessments_by_leaf_candidate.get((leaf_id, candidate_id))
        if assessment is None or section_id not in set(assessment.get("reviewed_section_ids") or []):
            raise ArbitrationHierarchyError("claim-support part has no parent-bound candidate assessment")
    for row in assessment_rows:
        candidate_id = str(row.get("candidate_id") or "")
        leaf_id = str(row.get("leaf_id") or "")
        for section_id in row.get("reviewed_section_ids") or []:
            if (candidate_id, str(section_id)) in expected_by_parent and (
                leaf_id, candidate_id, str(section_id)
            ) not in observed_leaf_pairs:
                raise ArbitrationHierarchyError("partitioned parent assessment omits its slot coverage receipt")


def _claim_coverage_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("leaf_id") or ""), str(row.get("candidate_id") or ""), str(row.get("section_id") or ""))


def _typed_issue_coverage_rows(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues = plan.get("critique_issues") or []
    leaves = sorted(plan.get("leaves") or [], key=lambda row: str(row.get("leaf_id") or ""))
    partition = plan.get("claim_support_partition") or {}
    parent_slots = {
        (str(parent.get("candidate_id") or ""), str(parent.get("section_id") or "")): parent.get("slots") or []
        for parent in partition.get("parent_sections") or []
        if isinstance(parent, Mapping)
    }
    result: list[dict[str, Any]] = []
    for issue in issues:
        issue_id = str(issue["issue_id"])
        scope = str(issue["scope"])
        candidate_id = str(issue.get("candidate_id") or "")
        targets = list(issue.get("target_ids") or [])
        leaf_ids: list[str] = []
        slot_refs: list[dict[str, Any]] = []
        for leaf in leaves:
            leaf_id = str(leaf["leaf_id"])
            if scope == "global":
                leaf_ids.append(leaf_id)
                continue
            expected_rows = leaf.get("expected_candidate_rows") or {}
            expected = expected_rows.get(candidate_id)
            if not isinstance(expected, Mapping) or issue_id not in set(expected.get("critic_issue_ids") or []):
                continue
            leaf_ids.append(leaf_id)
            slots_by_section = _expected_leaf_claim_slot_coverage(plan, leaf).get(candidate_id, {})
            for section_id, assigned_slots in slots_by_section.items():
                if scope == "section" and section_id not in targets:
                    continue
                if scope == "claim":
                    parent = parent_slots.get((candidate_id, str(section_id)), [])
                    target_slots = {
                        str(row["claim_slot_id"])
                        for row in parent
                        if set(targets).intersection(str(value) for value in row.get("claim_target_ids") or [])
                    }
                    target_slots.intersection_update(assigned_slots)
                    if not target_slots:
                        continue
                    assigned_slots = [slot_id for slot_id in assigned_slots if slot_id in target_slots]
                slot_refs.append({
                    "leaf_id": leaf_id,
                    "candidate_id": candidate_id,
                    "section_id": str(section_id),
                    "claim_slot_ids": list(assigned_slots),
                })
        result.append({
            "issue_id": issue_id,
            "scope": scope,
            "candidate_id": candidate_id or None,
            "target_ids": targets,
            "leaf_ids": leaf_ids,
            "claim_slot_targets": slot_refs,
        })
    return result


def _ordered_unique_ids(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ArbitrationHierarchyError(f"{label} must be an array of IDs")
    rows = list(value)
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ArbitrationHierarchyError(f"{label} contains an empty or non-string ID")
    if len(rows) != len(set(rows)):
        raise ArbitrationHierarchyError(f"{label} contains duplicate IDs")
    return tuple(rows)


def _exact_hash_map(value: Mapping[str, str], ids: Sequence[str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise ArbitrationHierarchyError(f"{label} must exactly match candidate IDs")
    result: dict[str, str] = {}
    for candidate_id in ids:
        item = value.get(candidate_id)
        if not isinstance(item, str) or not item.strip():
            raise ArbitrationHierarchyError(f"{label}[{candidate_id}] must be non-empty")
        result[candidate_id] = item
    return result


def _exact_mapping(value: Mapping[str, Any], ids: Sequence[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise ArbitrationHierarchyError(f"{label} must exactly match candidate IDs")
    return {candidate_id: copy.deepcopy(value[candidate_id]) for candidate_id in ids}


def _nonempty_hash_map(value: Mapping[str, str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ArbitrationHierarchyError(f"{label} must be a non-empty mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip():
            raise ArbitrationHierarchyError(f"{label} must contain non-empty string identities and hashes")
        result[key] = item
    return dict(sorted(result.items()))


def _nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationHierarchyError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ArbitrationHierarchyError(f"{label} must be an array of strings")
    rows = list(value)
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ArbitrationHierarchyError(f"{label} contains an empty or non-string value")
    if len(rows) != len(set(rows)):
        raise ArbitrationHierarchyError(f"{label} contains duplicate values")
    return [str(item) for item in rows]


def _section_task_ids(section: Mapping[str, Any], candidate_id: str, section_id: str) -> list[str]:
    declared = _string_list(section.get("task_ids"), f"{candidate_id}.{section_id}.task_ids")
    support = section.get("claim_support")
    if support is None:
        support_rows: list[Any] = []
    elif not isinstance(support, list):
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id}.claim_support must be an array")
    else:
        support_rows = support
    observed: set[str] = set()
    for index, row in enumerate(support_rows):
        if not isinstance(row, Mapping):
            raise ArbitrationHierarchyError(f"{candidate_id}.{section_id}.claim_support[{index}] must be an object")
        task_id = row.get("task_id")
        if task_id is not None:
            observed.add(_nonempty_str(task_id, f"{candidate_id}.{section_id}.claim_support[{index}].task_id"))
        for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
            if field in row:
                _string_list(row.get(field), f"{candidate_id}.{section_id}.claim_support[{index}].{field}")
    if declared and observed and set(declared) != observed:
        raise ArbitrationHierarchyError(
            f"{candidate_id}.{section_id} task_ids differ from complete section support tasks"
        )
    return sorted(set(declared) if declared else observed)


def _validate_section_source_closure(
    section: Mapping[str, Any], candidate_id: str, section_id: str, claims: Sequence[str]
) -> None:
    paper_keys = set(_string_list(section.get("paper_keys"), f"{candidate_id}.{section_id}.paper_keys"))
    if not paper_keys:
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} must name source papers")
    support = section.get("claim_support")
    if not isinstance(support, list) or not support:
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} has no source support rows")
    covered_claims: set[str] = set()
    supported_papers: set[str] = set()
    for index, row in enumerate(support):
        label = f"{candidate_id}.{section_id}.claim_support[{index}]"
        if not isinstance(row, Mapping):
            raise ArbitrationHierarchyError(f"{label} must be an object")
        claim = _nonempty_str(row.get("claim"), f"{label}.claim")
        if claim not in claims:
            raise ArbitrationHierarchyError(f"{label} is not bound to a section claim")
        covered_claims.add(claim)
        paper_key = _nonempty_str(row.get("paper_key"), f"{label}.paper_key")
        supported_papers.add(paper_key)
        for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
            if field not in row:
                raise ArbitrationHierarchyError(f"{label}.{field} is required for source closure")
            values = _string_list(row.get(field), f"{label}.{field}")
            if field in {"source_claim_ids", "evidence_ids"} and not values:
                raise ArbitrationHierarchyError(f"{label}.{field} must retain at least one source identity")
        for field in ("claim_slot_id", "task_id", "synthesis_claim_id", "primary_claim_id"):
            if field in row and row[field] is not None:
                _nonempty_str(row[field], f"{label}.{field}")
        if "study_id" in row and row["study_id"] is not None and not isinstance(row["study_id"], str):
            raise ArbitrationHierarchyError(f"{label}.study_id must be a string")
    if covered_claims != set(claims):
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} has a claim without a support row")
    if supported_papers != paper_keys:
        raise ArbitrationHierarchyError(f"{candidate_id}.{section_id} paper keys differ from source support")


def _section_evidence_refs(section: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    for row in section.get("claim_support") or []:
        if not isinstance(row, Mapping):
            continue
        for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                refs.add(value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                refs.update(item for item in value if isinstance(item, str) and item.strip())
    return refs


def _section_claim_ids(section: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "claim_id":
                    if isinstance(item, str) and item.strip():
                        result.add(item)
                    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                        result.update(row for row in item if isinstance(row, str) and row.strip())
                if isinstance(item, (Mapping, list, tuple)):
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(section)
    return result


def _validated_issues(
    disposition: Mapping[str, Any], ids: Sequence[str], hashes: Mapping[str, str]
) -> list[dict[str, Any]]:
    raw_issues = disposition.get("issues")
    if not isinstance(raw_issues, list):
        raise ArbitrationHierarchyError("critique disposition issues must be an array")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_issues):
        if not isinstance(raw, Mapping):
            raise ArbitrationHierarchyError(f"critique disposition issue {index} must be an object")
        row = copy.deepcopy(dict(raw))
        issue_id = _nonempty_str(row.get("issue_id"), f"critique issue {index}.issue_id")
        if issue_id in seen:
            raise ArbitrationHierarchyError(f"duplicate critique issue_id {issue_id}")
        seen.add(issue_id)
        scope = _nonempty_str(row.get("scope"), f"critique issue {issue_id}.scope")
        if scope not in {"global", "candidate", "section", "claim"}:
            raise ArbitrationHierarchyError(f"critique issue {issue_id} has an unsupported scope")
        targets = _string_list(row.get("target_ids"), f"critique issue {issue_id}.target_ids")
        candidate_id = str(row.get("candidate_id") or "")
        parent_hash = str(row.get("parent_candidate_hash") or "")
        if scope == "global":
            if candidate_id or parent_hash or targets:
                raise ArbitrationHierarchyError(f"global issue {issue_id} cannot bind candidate targets")
        else:
            if candidate_id not in ids or parent_hash != hashes[candidate_id]:
                raise ArbitrationHierarchyError(f"critique issue {issue_id} has a foreign candidate/hash binding")
            if scope == "candidate" and targets != [candidate_id]:
                raise ArbitrationHierarchyError(
                    f"candidate critique issue {issue_id} must target exactly its candidate_id"
                )
            if scope in {"section", "claim"} and not targets:
                raise ArbitrationHierarchyError(f"scoped critique issue {issue_id} has no targets")
        evidence_refs = _string_list(row.get("evidence_refs"), f"critique issue {issue_id}.evidence_refs")
        row["target_ids"] = targets
        row["evidence_refs"] = evidence_refs
        result.append(row)
    return sorted(result, key=lambda row: str(row["issue_id"]))


def _validate_issue_targets(
    issues: Sequence[Mapping[str, Any]],
    section_index: Mapping[str, Sequence[Mapping[str, Any]]],
    sections_by_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    section_ids_by_candidate = {
        candidate_id: {str(row.get("section_id") or "") for row in rows}
        for candidate_id, rows in section_index.items()
    }
    claim_ids_by_candidate: dict[str, set[str]] = {}
    for candidate_id, rows in sections_by_candidate.items():
        claim_ids: set[str] = set()
        for row in rows:
            claim_ids.update(_section_claim_ids(row["section"]))
        claim_ids_by_candidate[candidate_id] = claim_ids
    for issue in issues:
        scope = str(issue.get("scope") or "")
        if scope not in {"section", "claim"}:
            continue
        candidate_id = str(issue.get("candidate_id") or "")
        targets = {str(item) for item in issue.get("target_ids") or []}
        known = (
            section_ids_by_candidate.get(candidate_id, set())
            if scope == "section"
            else claim_ids_by_candidate.get(candidate_id, set())
        )
        if not targets or not targets.issubset(known):
            raise ArbitrationHierarchyError(
                f"typed {scope} critique issue {issue['issue_id']} targets unknown candidate content"
            )


def _issue_targets_selected_sections(
    issue: Mapping[str, Any], section_ids: set[str], sections: Sequence[Mapping[str, Any]], scope: str
) -> bool:
    targets = {str(item) for item in issue.get("target_ids") or []}
    if scope == "section":
        return bool(targets & section_ids)
    known_claim_ids = set().union(*(_section_claim_ids(section) for section in sections)) if sections else set()
    return bool(targets & known_claim_ids)


def _section_source_footprint(section: Mapping[str, Any]) -> str:
    """Alignment identity only; full submitted source rows stay in the leaf."""
    fields = (
        "claim_slot_id", "task_id", "paper_key", "primary_claim_id", "study_id",
        "source_claim_ids", "evidence_ids", "source_field_ids", "relation_ids",
        "primary_evidence_ids", "qualifier_source_claim_ids", "qualifier_evidence_ids",
        "qualifier_source_field_ids",
    )
    rows = [{key: row[key] for key in fields if key in row}
            for row in section.get("claim_support") or ()]
    return compute_v3_hash({"paper_keys": sorted(section.get("paper_keys") or ()),
                            "source_support": sorted(rows, key=compute_v3_hash)})


def _candidate_header(content: Mapping[str, Any], all_sections: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    header = copy.deepcopy(dict(content))
    header.pop("sections", None)
    flattened_claims = [claim for section in all_sections for claim in section.get("section", {}).get("claims", [])]
    for field in ("claims", "planned_claims"):
        if field in header and header[field] == flattened_claims:
            header.pop(field, None)
    derived_assignments = [
        {"section_id": str(section.get("section_id") or ""), "paper_keys": list(section.get("paper_keys") or [])}
        for section in all_sections
    ]
    if header.get("paper_assignments") == derived_assignments:
        header.pop("paper_assignments", None)
    shard_plan = header.get("shard_plan")
    if isinstance(shard_plan, Mapping):
        assignment = shard_plan.get("group_assignment")
        deferred = shard_plan.get("deferred_optional_groups")
        if isinstance(assignment, Mapping) and isinstance(deferred, (Mapping, list)):
            receipt = {
                "plan_hash": compute_v3_hash(shard_plan),
                "shard_count": shard_plan.get("shard_count"),
                "group_assignment_count": len(assignment),
                "group_assignment_hash": compute_v3_hash(assignment),
                "deferred_optional_group_count": len(deferred),
                "deferred_optional_groups_hash": compute_v3_hash(deferred),
            }
            for field in ("assignment_coverage_hash", "status"):
                if field in shard_plan:
                    receipt[field] = copy.deepcopy(shard_plan[field])
            header.pop("shard_plan", None)
            header["shard_plan_receipt"] = receipt
    return header


def _measure(value: Any, leaf_id: str) -> dict[str, Any]:
    if type(value) is int:
        return {"input_tokens": value, "output_tokens": None, "within_budget": True}
    if not isinstance(value, Mapping):
        raise ArbitrationHierarchyError(f"request estimator for {leaf_id} must return an integer or mapping")
    raw_input = value.get("estimated_input_tokens", value.get("input_tokens"))
    if type(raw_input) is not int or raw_input < 0:
        raise ArbitrationHierarchyError(f"request estimator for {leaf_id} omitted a valid input token count")
    raw_output = value.get("requested_output_tokens", value.get("output_tokens"))
    if raw_output is not None and (type(raw_output) is not int or raw_output < 0):
        raise ArbitrationHierarchyError(f"request estimator for {leaf_id} returned an invalid output token count")
    return {
        "input_tokens": raw_input,
        "output_tokens": raw_output,
        "within_budget": bool(value.get("within_budget", True)),
    }


def _leaf_by_id(plan: Mapping[str, Any], leaf_id: str) -> Mapping[str, Any]:
    leaves = plan.get("leaves")
    if not isinstance(leaves, list):
        raise ArbitrationHierarchyError("arbitration hierarchy plan leaves are invalid")
    matches = [row for row in leaves if isinstance(row, Mapping) and row.get("leaf_id") == leaf_id]
    if len(matches) != 1:
        raise ArbitrationHierarchyError(f"unknown or duplicate arbitration leaf_id {leaf_id}")
    return matches[0]


def _validate_plan_integrity(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != _PLAN_SCHEMA:
        raise ArbitrationHierarchyError("unsupported arbitration hierarchy plan schema")
    stored_hash = plan.get("plan_hash")
    body = _semantic_plan_hash_body(plan)
    if not isinstance(stored_hash, str) or stored_hash != compute_v3_hash(body):
        raise ArbitrationHierarchyError("arbitration hierarchy plan hash is invalid")
    _validate_plan_claim_support_partition(plan)


def _semantic_plan_hash_body(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"plan_hash", "max_calls"}
    }


def project_arbitration_leaf_section_index(request: Mapping[str, Any]) -> dict[str, Any]:
    """Project a provider request index to the exact sections assigned to its leaf.

    The plan keeps the complete index and binds it through plan_input_hash.
    This lossless wire view removes only unassigned metadata rows; the full
    source sections, support closures, expected rows, and final ledger remain
    in the local plan.
    """

    if not isinstance(request, Mapping):
        raise ArbitrationHierarchyError("arbitration leaf request must be an object")
    candidate_ids = _ordered_unique_ids(request.get("candidate_ids"), "candidate_ids")
    raw_index = request.get("candidate_section_index")
    raw_bindings = request.get("candidate_section_bindings")
    if not isinstance(raw_index, Mapping) or set(raw_index) != set(candidate_ids):
        raise ArbitrationHierarchyError("candidate section index must exactly match candidate IDs")
    if not isinstance(raw_bindings, Mapping) or set(raw_bindings) != set(candidate_ids):
        raise ArbitrationHierarchyError("candidate section bindings must exactly match candidate IDs")

    projected: dict[str, list[dict[str, Any]]] = {}
    for candidate_id in candidate_ids:
        rows = raw_index[candidate_id]
        bindings = raw_bindings[candidate_id]
        if not isinstance(rows, list) or not isinstance(bindings, list):
            raise ArbitrationHierarchyError(f"{candidate_id} index and bindings must be arrays")

        by_section_id: dict[str, Mapping[str, Any]] = {}
        for row_index, raw_row in enumerate(rows):
            if not isinstance(raw_row, Mapping):
                raise ArbitrationHierarchyError(f"{candidate_id} index row {row_index} must be an object")
            row = dict(raw_row)
            if row.get("candidate_id") != candidate_id:
                raise ArbitrationHierarchyError(f"{candidate_id} index row has a foreign owner")
            section_id = _nonempty_str(row.get("section_id"), f"{candidate_id} index section_id")
            _nonempty_str(row.get("section_hash"), f"{candidate_id}.{section_id} index section_hash")
            if section_id in by_section_id:
                raise ArbitrationHierarchyError(f"{candidate_id} index repeats section {section_id}")
            by_section_id[section_id] = raw_row

        selected_hashes: dict[str, str] = {}
        for binding_index, raw_binding in enumerate(bindings):
            if not isinstance(raw_binding, Mapping) or set(raw_binding) != {"section_id", "section_hash"}:
                raise ArbitrationHierarchyError(
                    f"{candidate_id} binding {binding_index} must contain only section_id and section_hash"
                )
            section_id = _nonempty_str(raw_binding.get("section_id"), f"{candidate_id} binding section_id")
            section_hash = _nonempty_str(raw_binding.get("section_hash"), f"{candidate_id}.{section_id} binding hash")
            if section_id in selected_hashes:
                raise ArbitrationHierarchyError(f"{candidate_id} bindings repeat section {section_id}")
            index_row = by_section_id.get(section_id)
            if index_row is None:
                raise ArbitrationHierarchyError(f"{candidate_id} binding names unknown section {section_id}")
            if index_row.get("section_hash") != section_hash:
                raise ArbitrationHierarchyError(f"{candidate_id} binding has a stale hash for section {section_id}")
            selected_hashes[section_id] = section_hash

        projected[candidate_id] = [
            copy.deepcopy(dict(row))
            for row in rows
            if str(row["section_id"]) in selected_hashes
        ]
        if len(projected[candidate_id]) != len(selected_hashes):
            raise ArbitrationHierarchyError(f"{candidate_id} index projection is incomplete")

    result = copy.deepcopy(dict(request))
    result["candidate_section_index"] = projected
    return result


__all__ = [
    "ArbitrationHierarchyError",
    "build_arbitration_final_ledger",
    "plan_arbitration_hierarchy",
    "project_arbitration_leaf_section_index",
    "validate_arbitration_leaf",
]

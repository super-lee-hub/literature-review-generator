"""Pack complete candidate assertion closures against both wire capacities.

The heuristic is deterministic first-fit decreasing, not an optimal solver.
It changes physical assignment only; every input group receives an assignment.
The caller supplies the production builder and serializer-aware estimator.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from outline.candidate_output_scope import CandidateOutputScopeV1
from runtime.provider_runtime import hash_json


class CandidatePackingError(ValueError):
    pass


def select_required_candidate_groups(
    scope: CandidateOutputScopeV1,
    *,
    required_paper_keys: Sequence[str],
    core_group_ids: Sequence[str],
    required_comparison_group_ids: Sequence[str],
    source_field_values: Mapping[str, str],
    measure_group: Callable[[str], Mapping[str, Any]],
    input_cap: int,
    output_cap: int,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    """Retain the core candidate scope and add minimal corpus witnesses.

    A witness is an already existing complete claim group, never a new claim
    or an inference from a relation id.  The supplied measurement callback
    must be deterministic and provider-free; it is called only for groups
    that can cover a still-uncovered required paper.

    The first result is the complete mandatory group-id set.  The second is
    one auditable witness row per required paper with ``paper_key``,
    ``claim_group_id``, and ``reason``.
    """

    if type(input_cap) is not int or input_cap <= 0:
        raise CandidatePackingError("required-corpus input cap must be a positive integer")
    if type(output_cap) is not int or output_cap <= 0:
        raise CandidatePackingError("required-corpus output cap must be a positive integer")

    def normalize_ids(values: Sequence[str], label: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise CandidatePackingError(f"{label} must be a sequence of IDs")
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise CandidatePackingError(f"{label} contains an empty ID")
        return tuple(sorted(set(normalized)))

    required_papers = normalize_ids(required_paper_keys, "required corpus paper keys")
    core_ids = normalize_ids(core_group_ids, "core candidate group IDs")
    comparison_ids = normalize_ids(required_comparison_group_ids, "required comparison group IDs")
    groups_by_id = {group.claim_group_id: group for group in scope.claim_groups}
    mandatory = set(core_ids) | set(comparison_ids)
    unknown_group_ids = sorted(mandatory - set(groups_by_id))
    if unknown_group_ids:
        raise CandidatePackingError(
            f"mandatory candidate group is absent from the verified scope: {unknown_group_ids[0]}"
        )

    slots_by_group: dict[str, list[Any]] = {}
    for slot in scope.claim_slots:
        slots_by_group.setdefault(slot.claim_group_id, []).append(slot)
    papers_by_group: dict[str, set[str]] = {}
    for group_id in mandatory:
        local = scope.for_groups([group_id])
        group = groups_by_id[group_id]
        supported_papers = {slot.paper_key for slot in local.claim_slots}
        if supported_papers != set(group.paper_keys):
            raise CandidatePackingError(
                f"mandatory candidate group {group_id} has incomplete paper-support closure"
            )
        papers_by_group[group_id] = supported_papers

    known_papers = set(scope.paper_keys)
    unknown_papers = sorted(set(required_papers) - known_papers)
    if unknown_papers:
        raise CandidatePackingError(
            f"required corpus paper {unknown_papers[0]!r} has no source-closed group in the verified scope"
        )

    required_set = set(required_papers)
    covered = set().union(*(papers_by_group.values() or [set()])) & required_set
    witness_group_for_paper: dict[str, str] = {}
    witness_reason_for_paper: dict[str, str] = {}

    def content_fingerprint(group: Any, slots: Sequence[Any]) -> str:
        support_rows = []
        for slot in slots:
            values = []
            for field_id in slot.source_field_ids:
                if field_id not in source_field_values:
                    raise CandidatePackingError(
                        f"candidate group {group.claim_group_id} has no verified source text for field {field_id}"
                    )
                value = source_field_values[field_id]
                if not isinstance(value, str):
                    raise CandidatePackingError(
                        f"candidate group {group.claim_group_id} has non-text verified source value for field {field_id}"
                    )
                values.append(" ".join(value.split()))
            support_rows.append({
                "has_study_id": bool(slot.study_id),
                "source_claim_count": len(slot.source_claim_ids),
                "evidence_count": len(slot.evidence_ids),
                "source_field_values": sorted(values),
            })
        support_rows.sort(key=lambda row: (
            row["has_study_id"], row["source_field_values"],
            row["source_claim_count"], row["evidence_count"],
        ))
        task_contract = dict(group.task_contract)
        semantic_task: dict[str, Any] = {}
        for key in ("relation_type", "comparison_question", "output_semantics"):
            value = task_contract.get(key)
            if isinstance(value, str):
                normalized = " ".join(value.split())
                # Contract prose can mention a source key for readability.
                # Replace those structural tokens so renaming keys cannot
                # change witness priority ahead of the final stable-ID tie.
                for paper_key in sorted(group.paper_keys, key=lambda item: (-len(item), item)):
                    normalized = normalized.replace(paper_key, "<source>")
                semantic_task[key] = normalized
        return hash_json({
            "claim_kind": group.claim_kind,
            "claim_seed": bool(group.claim_seed),
            "task": semantic_task,
            "support": support_rows,
        })

    def semantic_completeness(group_id: str, slots: Sequence[Any]) -> int:
        del group_id
        return sum(
            4 * bool(slot.primary_claim_id)
            + 3 * bool(slot.source_claim_ids)
            + 3 * bool(slot.evidence_ids)
            + min(8, len(slot.source_field_ids))
            + 4 * bool(slot.study_id)
            for slot in slots
        )

    # Already-mandatory groups are sufficient witnesses where possible. The
    # reason remains explicit about whether the authority came from a core
    # task or an accepted required comparison.
    for paper_key in sorted(covered):
        witness_candidates = [
            group_id for group_id in mandatory if paper_key in papers_by_group[group_id]
        ]
        selected_group = min(
            witness_candidates,
            key=lambda group_id: (
                0 if group_id in comparison_ids else 1,
                -semantic_completeness(group_id, slots_by_group.get(group_id, ())),
                content_fingerprint(groups_by_id[group_id], slots_by_group.get(group_id, ())),
                group_id,
            ),
        )
        witness_group_for_paper[paper_key] = selected_group
        witness_reason_for_paper[paper_key] = (
            "required_comparison" if selected_group in comparison_ids else "retained_core_group"
        )

    uncovered = required_set - covered
    measured: dict[str, tuple[int, int]] = {}
    measurement_errors: dict[str, str] = {}
    while uncovered:
        witness_group_candidates: list[tuple[tuple[Any, ...], str, set[str]]] = []
        for group in scope.claim_groups:
            group_id = group.claim_group_id
            if group_id in mandatory:
                continue
            local_slots = slots_by_group.get(group_id, [])
            supported_papers = {slot.paper_key for slot in local_slots}
            new_papers = supported_papers & uncovered
            if not new_papers:
                continue
            try:
                local_scope = scope.for_groups([group_id])
            except (ValueError, TypeError) as exc:
                measurement_errors[group_id] = f"incomplete source-support closure ({type(exc).__name__})"
                continue
            if {slot.paper_key for slot in local_scope.claim_slots} != set(group.paper_keys):
                measurement_errors[group_id] = "group paper keys differ from its complete support slots"
                continue
            if group_id in measurement_errors:
                continue
            try:
                content_fingerprint(group, local_scope.claim_slots)
            except CandidatePackingError as exc:
                measurement_errors[group_id] = str(exc)
                continue
            if group_id not in measured:
                metrics = dict(measure_group(group_id))
                input_tokens = metrics.get("input_tokens")
                output_tokens = metrics.get("output_tokens")
                within_context = metrics.get("within_context", True)
                if (type(input_tokens) is not int or input_tokens < 0
                        or type(output_tokens) is not int or output_tokens < 0
                        or type(within_context) is not bool):
                    raise CandidatePackingError(
                        f"measurement for required witness group {group_id} is incomplete"
                    )
                if (input_tokens > input_cap or output_tokens > output_cap or not within_context):
                    measurement_errors[group_id] = (
                        f"measured capacity input={input_tokens}/{input_cap}, "
                        f"output={output_tokens}/{output_cap}, within_context={within_context}"
                    )
                    continue
                measured[group_id] = (input_tokens, output_tokens)
            input_tokens, output_tokens = measured[group_id]
            collateral = len(supported_papers - required_set)
            completeness = semantic_completeness(group_id, local_scope.claim_slots)
            witness_group_candidates.append((
                (
                    -len(new_papers),
                    collateral,
                    -completeness,
                    input_tokens + output_tokens,
                    content_fingerprint(group, local_scope.claim_slots),
                    group_id,
                ),
                group_id,
                new_papers,
            ))
        if not witness_group_candidates:
            paper_key = min(uncovered)
            detail = "; ".join(
                f"{group_id}: {reason}"
                for group_id, reason in sorted(measurement_errors.items())
            )
            suffix = f" Candidate groups: {detail}." if detail else ""
            raise CandidatePackingError(
                f"required corpus paper {paper_key!r} has no complete source-closed witness group "
                f"that fits the measured request caps.{suffix}"
            )
        _rank, group_id, newly_covered = min(
            witness_group_candidates, key=lambda item: item[0],
        )
        mandatory.add(group_id)
        local_scope = scope.for_groups([group_id])
        papers_by_group[group_id] = {slot.paper_key for slot in local_scope.claim_slots}
        for paper_key in sorted(newly_covered):
            witness_group_for_paper[paper_key] = group_id
            witness_reason_for_paper[paper_key] = "required_corpus_source_closed_witness"
        uncovered -= newly_covered

    witnesses = tuple(
        {
            "paper_key": paper_key,
            "claim_group_id": witness_group_for_paper[paper_key],
            "reason": witness_reason_for_paper[paper_key],
        }
        for paper_key in sorted(required_set)
    )
    return tuple(sorted(mandatory)), witnesses


def expand_candidate_slot_output(payload: Mapping[str, Any], scope: CandidateOutputScopeV1) -> dict[str, Any]:
    """Restore reversible support records from explicitly selected group indices.

    A model still writes every assertion. The compiler restores only identities
    and source closure already sealed in the scope; it creates no source facts,
    source text, or study identity. Legacy full-support output remains accepted.
    """
    if "candidate_wire_version" not in payload:
        return dict(payload)
    if payload["candidate_wire_version"] != "candidate-slot-output/v1":
        raise CandidatePackingError("unknown compact candidate output schema")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise CandidatePackingError("compact candidate requires nonempty sections")
    used_groups: set[int] = set()
    sections = []
    slots_by_group: dict[str, list[Any]] = {}
    for slot in scope.claim_slots:
        slots_by_group.setdefault(slot.claim_group_id, []).append(slot)
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            raise CandidatePackingError("compact candidate section is not an object")
        if "claim_support" in raw:
            raise CandidatePackingError("compact candidate cannot mix untrusted full support rows")
        rows = raw.get("claims")
        if not isinstance(rows, list) or not rows:
            raise CandidatePackingError("compact candidate section needs authored claims")
        claims, support, tasks, papers = [], [], set(), set()
        for claim in rows:
            if not isinstance(claim, Mapping) or not str(claim.get("text") or "").strip():
                raise CandidatePackingError("compact candidate claim has no authored text")
            indices = claim.get("claim_group_indices")
            if not isinstance(indices, list) or not indices:
                raise CandidatePackingError("compact candidate claim has no complete support groups")
            text = str(claim["text"]).strip()
            if text not in claims:
                claims.append(text)
            for index in indices:
                if type(index) is not int or index < 0 or index >= len(scope.claim_groups):
                    raise CandidatePackingError("compact candidate references an unknown support group")
                if index in used_groups:
                    raise CandidatePackingError("compact candidate repeats a support group")
                used_groups.add(index)
                group = scope.claim_groups[index]
                tasks.add(group.task_id)
                for slot in slots_by_group.get(group.claim_group_id, ()):
                    papers.add(slot.paper_key)
                    support.append({
                        "claim": text, "claim_slot_id": slot.claim_slot_id, "task_id": slot.task_id,
                        "paper_key": slot.paper_key, "primary_claim_id": slot.primary_claim_id,
                        "source_claim_ids": list(slot.source_claim_ids), "evidence_ids": list(slot.evidence_ids),
                        "source_field_ids": list(slot.source_field_ids),
                        **({"study_id": slot.study_id} if slot.study_id else {}),
                    })
        for field, expected in (("paper_keys", papers), ("task_ids", tasks)):
            if field in raw and set(raw[field]) != expected:
                raise CandidatePackingError(f"compact candidate {field} differs from selected source groups")
        sections.append({**dict(raw), "claims": claims, "claim_support": support,
                         "paper_keys": sorted(papers), "task_ids": sorted(tasks)})
    result = {key: value for key, value in payload.items() if key not in {"candidate_wire_version", "claims"}}
    result["sections"] = sections
    result["claims"] = [claim for section in sections for claim in section["claims"]]
    # Parser authority is shared with the full-support wire, not relaxed.
    scope.validate(result)
    return result


def project_semantic_context(
    context: Mapping[str, Any], scope: CandidateOutputScopeV1,
    *, context_hash: str | None = None,
) -> dict[str, Any]:
    """Retain exact task claims and their qualifier text, with shared refs.

    All local source material remains unchanged. This projection is only for
    organization requests after source-closed shared understanding exists.
    """
    fields = {field_id for slot in scope.claim_slots for field_id in slot.source_field_ids}
    primaries = {slot.primary_claim_id for slot in scope.claim_slots}
    papers = set(scope.paper_keys)
    claim_ids = {group.synthesis_claim_id for group in scope.claim_groups}
    source_claim_ids = {value for slot in scope.claim_slots for value in slot.source_claim_ids}
    tables = context.get("interpretation_source_tables")
    if not isinstance(tables, Mapping):
        raise CandidatePackingError("candidate task is missing interpretation source tables")
    source_fields = [dict(row) for row in tables.get("source_fields") or ()
                     if isinstance(row, Mapping) and row.get("source_field_id") in fields]
    if {row["source_field_id"] for row in source_fields} != fields:
        raise CandidatePackingError("candidate group lost required qualifier/source text")
    dependencies = [
        dict(row) for row in tables.get("dependencies") or ()
        if isinstance(row, Mapping) and row.get("paper_key") in papers
        and row.get("primary_claim_id") in primaries
    ]
    if any(not set(row.get("required_source_field_ids") or ()).issubset(fields)
           for row in dependencies):
        raise CandidatePackingError("candidate group lost an interpretation dependency closure")

    def result_projection(result: Mapping[str, Any]) -> dict[str, Any]:
        output = result.get("provider_output")
        if not isinstance(output, Mapping):
            return {}
        claims = [dict(row) for row in output.get("claims") or ()
                  if isinstance(row, Mapping) and (row.get("claim_id") in claim_ids or (
                      row.get("paper_key") in papers and row.get("primary_claim_id") in primaries
                      and set(row.get("source_claim_ids") or ()).issubset(source_claim_ids)
                      and set(row.get("source_field_ids") or ()).issubset(fields)
                  ))]
        if not claims:
            return {}
        retained = {key: value for key, value in result.items()
                    if key in {"result_id", "node_id", "task_id", "logical_node_id", "fragment_id"}}
        retained["provider_output"] = {"claims": claims}
        interpretation = result.get("interpretation_context")
        if isinstance(interpretation, Mapping):
            retained["interpretation_context"] = {
                "source_field_ids": [value for value in interpretation.get("source_field_ids") or () if value in fields],
                "dependency_ids": [row["dependency_id"] for row in dependencies if row.get("dependency_id")],
            }
        return retained

    topics: list[dict[str, Any]] = []
    for topic in context.get("topic_routes") or ():
        if not isinstance(topic, Mapping):
            continue
        fragments: list[dict[str, Any]] = []
        for fragment in topic.get("fragments") or ():
            if not isinstance(fragment, Mapping):
                continue
            results = [projected for result in fragment.get("provider_results") or ()
                       if isinstance(result, Mapping) and (projected := result_projection(result))]
            if results:
                fragments.append({
                    "fragment_id": fragment.get("fragment_id"),
                    "paper_ids": [value for value in fragment.get("paper_ids") or () if value in papers],
                    "provider_results": results,
                })
        if fragments:
            topics.append({
                "topic_id": topic.get("topic_id"),
                "logical_node_id": topic.get("logical_node_id"),
                "dimensions": list(topic.get("dimensions") or ()),
                "fragments": fragments,
            })
    projected_context: dict[str, Any] = {
        "interpretation_source_tables": {
            "source_fields": source_fields, "dependencies": dependencies,
            "source_claims": [dict(row) for row in tables.get("source_claims") or ()
                              if isinstance(row, Mapping) and row.get("claim_id") in source_claim_ids
                              and row.get("paper_key") in papers],
        },
        "interpretation_source_contract": context.get("interpretation_source_contract", ""),
        "planned_evidence_policy": context.get("planned_evidence_policy", ""),
        "topic_routes": topics,
    }
    # The complete shared understanding stays in Registry. A scoped physical
    # request depends only on the task projections actually sent on its wire.
    del context_hash
    for key in ("global_synthesis", "cross_group_comparison"):
        result = context.get(key)
        if isinstance(result, Mapping):
            projection = result_projection(result)
            projected_context[key] = {**projection, "shared_result_hash": hash_json(projection)}
    projected_context["organization_tasks"] = [
        {"task_id": group.task_id, "claim_group_id": group.claim_group_id,
         "synthesis_claim_id": group.synthesis_claim_id, "paper_keys": list(group.paper_keys),
         "relation_ids": list(group.relation_ids),
         "claim_kind": group.claim_kind, "claim_seed": group.claim_seed,
         "task_contract": dict(group.task_contract),
         "instruction": "Organize or compare these complete source supports; a relation ID is not a scientific conclusion."}
        for group in scope.claim_groups
    ]
    projected_context["shared_context_hash"] = hash_json(projected_context)
    return json.loads(json.dumps(projected_context, ensure_ascii=False, allow_nan=False))


def estimate_candidate_output(scope: CandidateOutputScopeV1, request: Mapping[str, Any],
                              estimate_tokens: Callable[[Mapping[str, Any]], int]) -> int:
    """Estimate the complete required JSON shape, including every support row.

    This is an estimate, not model-reported usage or a guarantee on prose length.
    Cardinalities and support IDs come from the same scope validated by parser.
    """
    contract = request.get("output_contract") or {}
    prose_reserve = contract.get("claim_prose_reserve_tokens", 256)
    section_reserve = contract.get("section_prose_reserve_tokens", 64)
    if any(type(value) is not int or value <= 0 for value in (prose_reserve, section_reserve)):
        raise CandidatePackingError("candidate prose estimate reserves must be positive integers")
    compact = contract.get("preferred_candidate_wire_version") == "candidate-slot-output/v1"
    sections = []
    prose_tokens = 0
    for index, group in enumerate(scope.claim_groups):
        slots = [slot for slot in scope.claim_slots if slot.claim_group_id == group.claim_group_id]
        # Exact ID/record shape plus declared authored-prose reserve. Source
        # values are necessary INPUT; output never needs to copy them verbatim.
        claim = "Authored source-supported assertion."
        prose_tokens += prose_reserve * (1 if compact else 1 + len(slots)) + section_reserve
        if compact:
            sections.append({
                "section_id": f"section_{index + 1}", "title": "Source-supported synthesis",
                "goal": "Organize the finding with its recorded limitations.",
                "rationale": "Preserve both primary and qualifier support.",
                "relation_ids": list(group.relation_ids),
                "paper_roles": {paper_key: "Source-specific evidentiary contribution."
                                for paper_key in group.paper_keys},
                "claims": [{"text": claim, "claim_group_indices": [index]}],
            })
            continue
        sections.append({
            "section_id": f"section_{index + 1}", "title": "Source-supported synthesis",
            "goal": "Organize findings with their recorded distinctions and limitations.",
            "rationale": "Use the complete primary evidence and qualifiers from the assigned task.",
            "paper_keys": list(group.paper_keys), "relation_ids": list(group.relation_ids),
            "paper_roles": {paper_key: "Source-specific evidentiary contribution."
                            for paper_key in group.paper_keys},
            "task_ids": [group.task_id], "claims": [claim],
            "claim_support": [
                {"claim": claim, "claim_slot_id": slot.claim_slot_id, "task_id": slot.task_id,
                 "paper_key": slot.paper_key, "primary_claim_id": slot.primary_claim_id,
                 "source_claim_ids": list(slot.source_claim_ids), "evidence_ids": list(slot.evidence_ids),
                 "source_field_ids": list(slot.source_field_ids),
                 **({"study_id": slot.study_id} if slot.study_id else {})}
                for slot in slots
            ],
        })
    output = {"candidate_id": request.get("candidate_id"), "sections": sections}
    if compact:
        output["candidate_wire_version"] = "candidate-slot-output/v1"
    return max(1, estimate_tokens(output)) + prose_tokens


def pack_candidate_groups(
    scope: CandidateOutputScopeV1,
    build_request: Callable[[Sequence[str]], Mapping[str, Any]],
    measure: Callable[[Mapping[str, Any], CandidateOutputScopeV1], Mapping[str, Any]],
    *, input_cap: int, output_cap: int,
) -> list[tuple[list[str], dict[str, Any]]]:
    """Assign every indivisible assertion to a measured executable request."""
    tasks: list[tuple[Any, dict[str, Any], str]] = []
    for group in scope.claim_groups:
        local_scope = scope.for_groups([group.claim_group_id])
        request = build_request([group.claim_group_id])
        metrics = dict(measure(request, local_scope))
        if (int(metrics["input_tokens"]) > input_cap or int(metrics["output_tokens"]) > output_cap
                or not bool(metrics.get("within_context", True))):
            raise CandidatePackingError(
                f"candidate group {group.claim_group_id} cannot fit intact: "
                f"input={metrics['input_tokens']}/{input_cap}, output={metrics['output_tokens']}/{output_cap}; "
                "retain its scientific scope and retrieve a bounded source-closed comparison"
            )
        context = request.get("shared_semantic_context") or {}
        tables = context.get("interpretation_source_tables") or {}
        semantic_key = hash_json(sorted(str(row.get("source_value", ""))
                                        for row in tables.get("source_fields") or () if isinstance(row, Mapping)))
        tasks.append((group, metrics, semantic_key))
    tasks.sort(key=lambda row: (-int(row[1]["input_tokens"]), -int(row[1]["output_tokens"]),
                               row[2], row[0].claim_group_id))
    bins: list[tuple[list[str], dict[str, Any]]] = []
    for group, metrics, _semantic_key in tasks:
        for index, (group_ids, _old_metrics) in enumerate(bins):
            proposed = [*group_ids, group.claim_group_id]
            merged_scope = scope.for_groups(proposed)
            merged_metrics = dict(measure(build_request(proposed), merged_scope))
            if (int(merged_metrics["input_tokens"]) <= input_cap
                    and int(merged_metrics["output_tokens"]) <= output_cap
                    and bool(merged_metrics.get("within_context", True))):
                bins[index] = (proposed, merged_metrics)
                break
        else:
            bins.append(([group.claim_group_id], metrics))
    assigned = [group_id for group_ids, _metrics in bins for group_id in group_ids]
    expected = {group.claim_group_id for group in scope.claim_groups}
    if len(assigned) != len(expected) or set(assigned) != expected:
        raise CandidatePackingError("candidate group assignment lost or duplicated a logical assertion")
    return bins

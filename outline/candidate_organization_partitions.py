"""Pure whole-group partition planning for candidate organization requests.

The shared candidate fragment bank remains the only bank artifact. This module
projects its global group indices into bounded organization leaves without
creating a subset bank or changing source provenance. Callers must still
validate each raw map against its exact request and run the existing full-scope
candidate validators before publication.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from outline.candidate_fragment_bank import (
    CandidateFragmentBankError,
    validate_candidate_fragment_bank_v1,
)
from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    CandidateOutputScopeV1,
)
from runtime.provider_runtime import hash_json


class CandidateOrganizationPartitionError(ValueError):
    """Raised when exact candidate organization partitions are incomplete."""


@dataclass(frozen=True, slots=True)
class CandidateOrganizationPartitionV1:
    """One source-closed whole-group projection over the shared bank."""

    partition_index: int
    bank_hash: str
    global_group_indices: tuple[int, ...]
    global_group_ids: tuple[str, ...]
    global_to_local_group_indices: tuple[tuple[int, int], ...]
    paper_keys: tuple[str, ...]
    required_group_ids: tuple[str, ...]
    required_paper_witnesses: tuple[tuple[str, str], ...]
    scope_subset: CandidateOutputScopeV1

    @property
    def local_to_global_group_indices(self) -> tuple[int, ...]:
        """Map each local request-card index to its original bank index."""

        return self.global_group_indices


@dataclass(frozen=True, slots=True)
class CandidateOrganizationPartitionPlanV1:
    """Deterministic assignment of every full-bank group to one leaf."""

    bank_hash: str
    bank_group_count: int
    full_scope: CandidateOutputScopeV1
    partitions: tuple[CandidateOrganizationPartitionV1, ...]
    required_group_ids: tuple[str, ...]
    required_paper_keys: tuple[str, ...]
    required_paper_witnesses: tuple[tuple[str, str], ...]
    group_assignment: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class CandidateOrganizationSectionChoiceV1:
    """Source-indexed section/role choices retained from one validated leaf."""

    partition_index: int
    section_index: int
    metadata_index: int
    global_fragment_indices: tuple[int, ...]
    # (paper key, global bank group index chosen as the source-owned role)
    paper_roles: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class CandidateOrganizationPartitionAccountingV1:
    """Global used/deferred accounting and lossless leaf section choices.

    This is deterministic bookkeeping, not a provider response and not a raw
    map artifact. The original provider maps and receipts remain leaf-scoped.
    """

    bank_hash: str
    expected_bindings: tuple[tuple[str, str], ...]
    used_global_indices: tuple[int, ...]
    deferred_global_indices: tuple[int, ...]
    deferred_reason_codes: tuple[tuple[int, str], ...]
    sections: tuple[CandidateOrganizationSectionChoiceV1, ...]


def _normal_ids(value: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CandidateOrganizationPartitionError(f"{label} must be a sequence of IDs")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CandidateOrganizationPartitionError(
            f"{label} contains an empty or non-string ID"
        )
    result = tuple(sorted(set(value)))
    if len(result) != len(value):
        raise CandidateOrganizationPartitionError(f"{label} contains duplicate IDs")
    return result


def _checked_bank_groups(
    bank: Mapping[str, Any], scope: CandidateOutputScopeV1
) -> tuple[list[Mapping[str, Any]], str]:
    try:
        validate_candidate_fragment_bank_v1(bank, scope)
    except CandidateFragmentBankError as exc:
        raise CandidateOrganizationPartitionError(
            f"shared fragment bank is invalid: {exc}"
        ) from exc
    groups = bank.get("groups")
    bank_hash = bank.get("content_hash")
    if (
        not isinstance(groups, Sequence)
        or isinstance(groups, (str, bytes))
        or not groups
    ):
        raise CandidateOrganizationPartitionError("shared fragment bank has no groups")
    if not isinstance(bank_hash, str) or len(bank_hash) != 64:
        raise CandidateOrganizationPartitionError(
            "shared fragment bank has no content hash"
        )
    checked: list[Mapping[str, Any]] = []
    for index, raw in enumerate(groups):
        if (
            not isinstance(raw, Mapping)
            or type(raw.get("index")) is not int
            or raw["index"] != index
        ):
            raise CandidateOrganizationPartitionError(
                "shared fragment bank indices are not canonical"
            )
        group = raw.get("claim_group")
        if not isinstance(group, Mapping) or not isinstance(
            group.get("claim_group_id"), str
        ):
            raise CandidateOrganizationPartitionError(
                f"shared fragment bank group {index} has no identity"
            )
        checked.append(raw)
    return checked, bank_hash


def _group_id(entry: Mapping[str, Any]) -> str:
    group = entry.get("claim_group")
    if not isinstance(group, Mapping):
        raise CandidateOrganizationPartitionError("fragment entry has no claim group")
    value = group.get("claim_group_id")
    if not isinstance(value, str) or not value.strip():
        raise CandidateOrganizationPartitionError(
            "fragment entry has an invalid group identity"
        )
    return value


def _selected_indices(value: Sequence[int], total: int) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise CandidateOrganizationPartitionError(
            "a partition requires at least one global group index"
        )
    if any(type(index) is not int or index < 0 or index >= total for index in value):
        raise CandidateOrganizationPartitionError(
            "partition contains an invalid global group index"
        )
    if len(set(value)) != len(value):
        raise CandidateOrganizationPartitionError(
            "partition repeats a global group index"
        )
    return tuple(sorted(value))


def _scope_projection(
    bank_groups: Sequence[Mapping[str, Any]],
    bank_hash: str,
    full_scope: CandidateOutputScopeV1,
    global_indices: Sequence[int],
    *,
    partition_index: int,
    required_group_ids: Sequence[str],
    required_paper_witnesses: Mapping[str, str],
) -> CandidateOrganizationPartitionV1:
    selected_indices = tuple(sorted(global_indices))
    group_ids = tuple(_group_id(bank_groups[index]) for index in selected_indices)
    index_by_group_id = {
        _group_id(bank_groups[index]): index for index in selected_indices
    }
    selected_group_ids = set(group_ids)
    required_ids = _normal_ids(required_group_ids, "partition required_group_ids")
    missing_required = sorted(set(required_ids) - selected_group_ids)
    if missing_required:
        raise CandidateOrganizationPartitionError(
            f"partition omits required group {missing_required[0]}"
        )
    for group_id in required_ids:
        global_index = index_by_group_id.get(group_id)
        if global_index is None:
            raise CandidateOrganizationPartitionError(
                f"required group {group_id} is absent from bank"
            )
        entry = bank_groups[global_index]
        if entry.get("materialization_state") != "materialized":
            raise CandidateOrganizationPartitionError(
                f"required group {group_id} is unresolved before organization"
            )

    selected_papers = {
        str(slot["paper_key"])
        for index in selected_indices
        for slot in bank_groups[index]["support_slots"]
    }
    local_witnesses: list[tuple[str, str]] = []
    for paper_key, group_id in sorted(required_paper_witnesses.items()):
        if group_id not in selected_group_ids:
            raise CandidateOrganizationPartitionError(
                f"partition omits required paper witness group {group_id} for {paper_key}"
            )
        entry = bank_groups[index_by_group_id[group_id]]
        if entry.get("materialization_state") != "materialized":
            raise CandidateOrganizationPartitionError(
                f"required paper {paper_key} witness group {group_id} is unresolved"
            )
        supported = {str(slot["paper_key"]) for slot in entry["support_slots"]}
        if paper_key not in supported or paper_key not in selected_papers:
            raise CandidateOrganizationPartitionError(
                f"required paper {paper_key} is not owned by witness group {group_id}"
            )
        local_witnesses.append((paper_key, group_id))

    try:
        subset_scope = full_scope.for_groups(
            group_ids,
            allowed_paper_keys=full_scope.paper_keys,
        )
    except CandidateOutputScopeError as exc:
        raise CandidateOrganizationPartitionError(
            f"whole-group scope projection failed: {exc}"
        ) from exc
    expected_slots = {
        str(slot["claim_slot_id"])
        for index in selected_indices
        for slot in bank_groups[index]["support_slots"]
    }
    projected_slot_ids = {slot.claim_slot_id for slot in subset_scope.claim_slots}
    if expected_slots != projected_slot_ids:
        raise CandidateOrganizationPartitionError(
            "partition projection lost complete support slots"
        )
    projected_group_ids = {group.claim_group_id for group in subset_scope.claim_groups}
    if projected_group_ids != selected_group_ids:
        raise CandidateOrganizationPartitionError(
            "partition projection changed its whole-group set"
        )

    paper_keys = tuple(sorted(selected_papers))
    return CandidateOrganizationPartitionV1(
        partition_index=partition_index,
        bank_hash=bank_hash,
        global_group_indices=selected_indices,
        global_group_ids=group_ids,
        global_to_local_group_indices=tuple(
            (global_index, local_index)
            for local_index, global_index in enumerate(selected_indices)
        ),
        paper_keys=paper_keys,
        required_group_ids=required_ids,
        required_paper_witnesses=tuple(local_witnesses),
        scope_subset=subset_scope,
    )


def project_candidate_organization_partition_v1(
    bank: Mapping[str, Any],
    full_scope: CandidateOutputScopeV1,
    global_group_indices: Sequence[int],
    *,
    partition_index: int = 0,
    required_group_ids: Sequence[str] = (),
    required_paper_witnesses: Mapping[str, str] | None = None,
) -> CandidateOrganizationPartitionV1:
    """Project selected complete groups while preserving the full bank identity."""

    bank_groups, bank_hash = _checked_bank_groups(bank, full_scope)
    selected = _selected_indices(global_group_indices, len(bank_groups))
    if type(partition_index) is not int or partition_index < 0:
        raise CandidateOrganizationPartitionError(
            "partition_index must be a nonnegative integer"
        )
    witnesses = {} if required_paper_witnesses is None else required_paper_witnesses
    if not isinstance(witnesses, Mapping):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses must be a mapping"
        )
    if any(not isinstance(paper, str) or not paper.strip() for paper in witnesses):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses has an invalid paper key"
        )
    if any(
        not isinstance(group, str) or not group.strip() for group in witnesses.values()
    ):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses has an invalid group identity"
        )
    group_ids = {_group_id(bank_groups[index]) for index in selected}
    witness_group_ids = set(witnesses.values())
    if not witness_group_ids.issubset(group_ids):
        missing = sorted(witness_group_ids - group_ids)[0]
        raise CandidateOrganizationPartitionError(
            f"partition omits required paper witness group {missing}"
        )
    return _scope_projection(
        bank_groups,
        bank_hash,
        full_scope,
        selected,
        partition_index=partition_index,
        required_group_ids=required_group_ids,
        required_paper_witnesses=witnesses,
    )


def _semantic_order_key(
    entry: Mapping[str, Any], mandatory_group_ids: set[str]
) -> tuple[Any, ...]:
    group = entry["claim_group"]
    group_id = str(group["claim_group_id"])
    state = str(entry["materialization_state"])
    content_hash = next(
        (
            str(entry.get(key))
            for key in (
                "generated_text_hash",
                "canonical_payload_hash",
                "seed_identity_hash",
            )
            if isinstance(entry.get(key), str) and entry.get(key)
        ),
        str(group.get("claim_hash") or ""),
    )
    semantic_hash = hash_json(
        {
            "entry_kind": str(entry.get("entry_kind") or ""),
            "claim_hash": str(group.get("claim_hash") or ""),
            "content_hash": content_hash,
            "materialization_state": state,
        }
    )
    return (
        0 if group_id in mandatory_group_ids else 1,
        0 if state == "materialized" else 1,
        semantic_hash,
        group_id,
        int(entry["index"]),
    )


def _fits_exactly(
    fits_partition: Callable[[tuple[int, ...]], bool], indices: Sequence[int]
) -> bool:
    result = fits_partition(tuple(sorted(indices)))
    if type(result) is not bool:
        raise CandidateOrganizationPartitionError("exact-fit callback must return bool")
    return result


def _largest_fitting_prefix(
    ordered_indices: Sequence[int],
    fits_partition: Callable[[tuple[int, ...]], bool],
    bank_groups: Sequence[Mapping[str, Any]],
) -> int:
    """Find a deterministic fitting prefix in O(log n) exact-fit probes."""

    remaining = len(ordered_indices)
    if not remaining:
        return 0
    # A partition with no obligations may explicitly defer every assigned
    # optional group. It does not need a borrowed materialized section anchor.
    minimum = 1
    first_valid_prefix = tuple(sorted(ordered_indices[:minimum]))
    if not _fits_exactly(fits_partition, first_valid_prefix):
        group_id = _group_id(bank_groups[ordered_indices[0]])
        failed_unit = f"single group {group_id}"
        raise CandidateOrganizationPartitionError(
            f"{failed_unit} exceeds an exact organization request capacity"
        )

    fitting_lengths = [minimum]
    low = minimum
    probe = min(remaining, max(minimum + 1, minimum * 2))
    upper: int | None = None
    if minimum == remaining:
        upper = remaining
    while probe <= remaining and probe > low:
        candidate = tuple(sorted(ordered_indices[:probe]))
        if _fits_exactly(fits_partition, candidate):
            fitting_lengths.append(probe)
            low = probe
            if probe == remaining:
                upper = remaining
                break
            probe = min(remaining, probe * 2)
            if probe == low:
                break
        else:
            upper = probe
            break

    if upper is not None and upper > low + 1:
        left, right = low + 1, upper - 1
        while left <= right:
            middle = (left + right) // 2
            candidate = tuple(sorted(ordered_indices[:middle]))
            if _fits_exactly(fits_partition, candidate):
                fitting_lengths.append(middle)
                left = middle + 1
            else:
                right = middle - 1

    best = max(fitting_lengths)
    exact_leaf = tuple(sorted(ordered_indices[:best]))
    if not _fits_exactly(fits_partition, exact_leaf):
        raise CandidateOrganizationPartitionError(
            "exact-fit callback changed its result while rechecking an accepted partition"
        )
    return best


def plan_candidate_organization_partitions_v1(
    bank: Mapping[str, Any],
    full_scope: CandidateOutputScopeV1,
    *,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    required_paper_witnesses: Mapping[str, str],
    fits_partition: Callable[[tuple[int, ...]], bool],
) -> CandidateOrganizationPartitionPlanV1:
    """Assign each whole group once using the exact cross-axis fit callback.

    The callback must construct and measure every actual candidate-axis request
    for the supplied global bank indices. This helper stays provider-free and
    source-neutral; its deterministic prefix packing is a heuristic, not an
    optimal bin-packing guarantee.
    """

    bank_groups, bank_hash = _checked_bank_groups(bank, full_scope)
    normalized_required_groups = _normal_ids(required_group_ids, "required_group_ids")
    normalized_required_papers = _normal_ids(required_paper_keys, "required_paper_keys")
    if not isinstance(required_paper_witnesses, Mapping):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses must be a mapping"
        )
    if any(
        not isinstance(paper, str) or not paper.strip()
        for paper in required_paper_witnesses
    ):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses has an invalid paper key"
        )
    if any(
        not isinstance(group, str) or not group.strip()
        for group in required_paper_witnesses.values()
    ):
        raise CandidateOrganizationPartitionError(
            "required_paper_witnesses has an invalid group identity"
        )
    normalized_witnesses = dict(required_paper_witnesses)
    if set(normalized_witnesses) != set(normalized_required_papers):
        missing = sorted(set(normalized_required_papers) - set(normalized_witnesses))
        extra = sorted(set(normalized_witnesses) - set(normalized_required_papers))
        problem = (
            f"missing required paper witness for {missing[0]}"
            if missing
            else f"unexpected witness for {extra[0]}"
        )
        raise CandidateOrganizationPartitionError(problem)
    if not callable(fits_partition):
        raise CandidateOrganizationPartitionError("fits_partition must be callable")

    group_index_by_id = {_group_id(entry): int(entry["index"]) for entry in bank_groups}
    unknown_groups = sorted(set(normalized_required_groups) - set(group_index_by_id))
    if unknown_groups:
        raise CandidateOrganizationPartitionError(
            f"required group {unknown_groups[0]} is absent from bank"
        )
    unknown_witness_groups = sorted(
        set(normalized_witnesses.values()) - set(group_index_by_id)
    )
    if unknown_witness_groups:
        raise CandidateOrganizationPartitionError(
            f"required paper witness group {unknown_witness_groups[0]} is absent from bank"
        )

    paper_keys_by_group: dict[str, set[str]] = {}
    for group_id, global_index in group_index_by_id.items():
        entry = bank_groups[global_index]
        if entry.get("materialization_state") not in {"materialized", "unresolved"}:
            raise CandidateOrganizationPartitionError(
                f"group {group_id} has invalid materialization state"
            )
        paper_keys_by_group[group_id] = {
            str(slot["paper_key"]) for slot in entry["support_slots"]
        }
    mandatory_group_ids = set(normalized_required_groups) | set(
        normalized_witnesses.values()
    )
    normalized_required_groups = tuple(sorted(mandatory_group_ids))
    for group_id in sorted(mandatory_group_ids):
        entry = bank_groups[group_index_by_id[group_id]]
        if entry.get("materialization_state") != "materialized":
            raise CandidateOrganizationPartitionError(
                f"required group {group_id} is unresolved before organization"
            )
    full_scope_papers = set(full_scope.paper_keys)
    for paper_key, group_id in sorted(normalized_witnesses.items()):
        if paper_key not in paper_keys_by_group[group_id]:
            raise CandidateOrganizationPartitionError(
                f"required paper {paper_key} is not owned by witness group {group_id}"
            )
        if paper_key not in full_scope_papers:
            raise CandidateOrganizationPartitionError(
                f"required paper {paper_key} is outside full scope"
            )

    materialized_groups = sorted(
        (
            entry
            for entry in bank_groups
            if entry["materialization_state"] == "materialized"
        ),
        key=lambda item: _semantic_order_key(item, mandatory_group_ids),
    )
    unresolved_groups = sorted(
        (
            entry
            for entry in bank_groups
            if entry["materialization_state"] == "unresolved"
        ),
        key=lambda item: _semantic_order_key(item, mandatory_group_ids),
    )
    if not materialized_groups:
        raise CandidateOrganizationPartitionError(
            "organization partitions require at least one materialized group to anchor a section"
        )
    # Interleave optional unresolved cards with materialized groups for compact
    # requests, allowing an explicit all-deferred optional leaf at tight caps.
    unresolved_buckets: list[list[Mapping[str, Any]]] = [
        [] for _ in materialized_groups
    ]
    for index, entry in enumerate(unresolved_groups):
        unresolved_buckets[index % len(unresolved_buckets)].append(entry)
    ordered_rows: list[Mapping[str, Any]] = []
    for index, entry in enumerate(materialized_groups):
        ordered_rows.extend(unresolved_buckets[index])
        ordered_rows.append(entry)
    ordered_indices = tuple(int(entry["index"]) for entry in ordered_rows)
    partitions_raw: list[tuple[int, ...]] = []
    cursor = 0
    while cursor < len(ordered_indices):
        count = _largest_fitting_prefix(
            ordered_indices[cursor:], fits_partition, bank_groups
        )
        if count < 1:
            raise CandidateOrganizationPartitionError(
                "partition planner failed to make progress"
            )
        partitions_raw.append(tuple(sorted(ordered_indices[cursor : cursor + count])))
        cursor += count

    observed = [index for partition in partitions_raw for index in partition]
    if len(observed) != len(bank_groups) or set(observed) != set(
        range(len(bank_groups))
    ):
        raise CandidateOrganizationPartitionError(
            "planner did not assign every bank group exactly once"
        )
    if len(set(observed)) != len(observed):
        raise CandidateOrganizationPartitionError(
            "planner assigned a bank group to multiple partitions"
        )

    partitions: list[CandidateOrganizationPartitionV1] = []
    group_assignment: list[tuple[str, int]] = []
    for partition_index, global_indices in enumerate(partitions_raw):
        local_group_ids = {_group_id(bank_groups[index]) for index in global_indices}
        local_required = tuple(
            sorted(set(normalized_required_groups) & local_group_ids)
        )
        local_witnesses = {
            paper: group_id
            for paper, group_id in normalized_witnesses.items()
            if group_id in local_group_ids
        }
        projection = _scope_projection(
            bank_groups,
            bank_hash,
            full_scope,
            global_indices,
            partition_index=partition_index,
            required_group_ids=local_required,
            required_paper_witnesses=local_witnesses,
        )
        partitions.append(projection)
        group_assignment.extend(
            (group_id, partition_index) for group_id in projection.global_group_ids
        )

    try:
        validate_candidate_fragment_bank_v1(bank, full_scope)
    except CandidateFragmentBankError as exc:
        raise CandidateOrganizationPartitionError(
            f"shared bank changed during planning: {exc}"
        ) from exc
    return CandidateOrganizationPartitionPlanV1(
        bank_hash=bank_hash,
        bank_group_count=len(bank_groups),
        full_scope=full_scope,
        partitions=tuple(partitions),
        required_group_ids=normalized_required_groups,
        required_paper_keys=normalized_required_papers,
        required_paper_witnesses=tuple(sorted(normalized_witnesses.items())),
        group_assignment=tuple(sorted(group_assignment)),
    )


def account_candidate_organization_partition_maps_v1(
    bank: Mapping[str, Any],
    plan: CandidateOrganizationPartitionPlanV1,
    leaf_maps: Sequence[Mapping[str, Any]],
    *,
    expected_bindings: Mapping[str, str],
) -> CandidateOrganizationPartitionAccountingV1:
    """Translate validated leaf choices to global indices and check accounting.

    Each raw map must first pass `validate_candidate_organization_map_for_request_v1`
    against its exact stored request. This helper preserves leaf section/role
    choices and checks the cross-leaf invariants that a single map cannot see.
    It does not construct a replacement provider map or grant source authority.
    """

    bank_groups, bank_hash = _checked_bank_groups(bank, plan.full_scope)
    if bank_hash != plan.bank_hash or len(bank_groups) != plan.bank_group_count:
        raise CandidateOrganizationPartitionError(
            "partition plan is bound to another shared bank"
        )
    group_ids = [_group_id(entry) for entry in bank_groups]
    planned_indices = [
        index
        for part in plan.partitions
        for index in part.local_to_global_group_indices
    ]
    if (
        any(
            type(index) is not int or not 0 <= index < len(bank_groups)
            for index in planned_indices
        )
        or len(planned_indices) != len(bank_groups)
        or set(planned_indices) != set(range(len(bank_groups)))
    ):
        raise CandidateOrganizationPartitionError(
            "partition plan does not assign each global group once"
        )
    expected_assignment: list[tuple[str, int]] = []
    required_group_union: set[str] = set()
    witness_union: dict[str, str] = {}
    for part_index, part in enumerate(plan.partitions):
        indices = part.local_to_global_group_indices
        part_group_ids = {group_ids[index] for index in indices}
        if (
            type(part.partition_index) is not int
            or part.partition_index != part_index
            or part.bank_hash != bank_hash
            or indices != tuple(sorted(indices))
            or part.global_to_local_group_indices
            != tuple(
                (global_index, local) for local, global_index in enumerate(indices)
            )
            or part.global_group_ids != tuple(group_ids[index] for index in indices)
            or not set(part.required_group_ids).issubset(part_group_ids)
            or any(
                group_id not in part_group_ids
                for _paper, group_id in part.required_paper_witnesses
            )
        ):
            raise CandidateOrganizationPartitionError(
                "partition plan has inconsistent global/local mappings"
            )
        required_group_union.update(part.required_group_ids)
        expected_assignment.extend((group_ids[index], part_index) for index in indices)
        for paper_key, group_id in part.required_paper_witnesses:
            if paper_key in witness_union:
                raise CandidateOrganizationPartitionError(
                    "partition plan repeats a required-paper witness"
                )
            witness_union[paper_key] = group_id
    if (
        required_group_union != set(plan.required_group_ids)
        or len(plan.required_group_ids) != len(required_group_union)
        or witness_union != dict(plan.required_paper_witnesses)
        or len(plan.required_paper_witnesses) != len(witness_union)
        or set(plan.required_paper_keys) != set(witness_union)
        or len(plan.required_paper_keys) != len(witness_union)
        or plan.group_assignment != tuple(sorted(expected_assignment))
    ):
        raise CandidateOrganizationPartitionError(
            "partition plan obligations differ from its global assignment"
        )
    if not isinstance(leaf_maps, Sequence) or isinstance(leaf_maps, (str, bytes)):
        raise CandidateOrganizationPartitionError("leaf_maps must be an array")
    if len(leaf_maps) != len(plan.partitions) or not leaf_maps:
        raise CandidateOrganizationPartitionError(
            "leaf map count differs from the partition plan"
        )

    binding_names = (
        "candidate_id",
        "axis_hash",
        "plan_hash",
        "bank_hash",
        "metadata_catalog_hash",
    )
    if not isinstance(expected_bindings, Mapping) or any(
        name not in expected_bindings for name in binding_names
    ):
        raise CandidateOrganizationPartitionError(
            "expected_bindings omits candidate/axis/plan/bank/catalog identity"
        )
    bindings = {name: expected_bindings[name] for name in binding_names}
    if any(
        not isinstance(value, str) or not value.strip() for value in bindings.values()
    ):
        raise CandidateOrganizationPartitionError(
            "expected_bindings contains an empty or non-text identity"
        )
    if bindings["bank_hash"] != bank_hash:
        raise CandidateOrganizationPartitionError(
            "expected map bank_hash differs from the shared partition plan"
        )

    def local_indices(value: Any, limit: int, label: str) -> tuple[int, ...]:
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or not value
        ):
            raise CandidateOrganizationPartitionError(
                f"{label} must be a nonempty index array"
            )
        if any(
            type(index) is not int or index < 0 or index >= limit for index in value
        ):
            raise CandidateOrganizationPartitionError(
                f"{label} contains an invalid index"
            )
        if len(set(value)) != len(value):
            raise CandidateOrganizationPartitionError(
                f"{label} contains duplicate indices"
            )
        return tuple(value)

    used_global: set[int] = set()
    deferred_global: set[int] = set()
    deferred_reasons: dict[int, str] = {}
    sections: list[CandidateOrganizationSectionChoiceV1] = []
    role_texts: dict[str, set[str]] = {}
    role_counts: dict[str, int] = {}
    group_index_by_id = {group_id: index for index, group_id in enumerate(group_ids)}

    for part, raw_map in zip(plan.partitions, leaf_maps, strict=True):
        if not isinstance(raw_map, Mapping):
            raise CandidateOrganizationPartitionError(
                "leaf organization map must be an object"
            )
        for key, value in bindings.items():
            if raw_map.get(key) != value:
                raise CandidateOrganizationPartitionError(
                    f"leaf map {key} differs from its exact binding"
                )
        local_to_global = part.local_to_global_group_indices
        local_count = len(local_to_global)
        required_group_local = [
            local
            for local, global_index in enumerate(local_to_global)
            if group_ids[global_index] in part.required_group_ids
        ]
        witness_papers = {paper for paper, _group in part.required_paper_witnesses}
        required_paper_local = [
            index
            for index, paper in enumerate(part.paper_keys)
            if paper in witness_papers
        ]
        for name, expected in (
            ("required_group_indices", required_group_local),
            ("required_paper_indices", required_paper_local),
        ):
            values = raw_map.get(name)
            if (
                not isinstance(values, Sequence)
                or isinstance(values, (str, bytes))
                or any(type(value) is not int for value in values)
                or list(values) != expected
            ):
                raise CandidateOrganizationPartitionError(
                    f"leaf map {name} differs from partition obligations"
                )

        local_used: set[int] = set()
        raw_sections = raw_map.get("sections")
        if (
            not isinstance(raw_sections, Sequence)
            or isinstance(raw_sections, (str, bytes))
            or (not raw_sections and (part.required_group_ids or part.required_paper_witnesses))
        ):
            raise CandidateOrganizationPartitionError("leaf map has no sections")
        seen_section_ids: set[int] = set()
        for section in raw_sections:
            if not isinstance(section, Mapping):
                raise CandidateOrganizationPartitionError(
                    "leaf section must be an object"
                )
            section_id, metadata_id = (
                section.get("section_index"),
                section.get("metadata_index"),
            )
            if (
                type(section_id) is not int
                or section_id < 0
                or section_id in seen_section_ids
                or type(metadata_id) is not int
                or metadata_id < 0
            ):
                raise CandidateOrganizationPartitionError(
                    "leaf section or metadata index is invalid"
                )
            seen_section_ids.add(section_id)
            selected = local_indices(
                section.get("fragment_indices"), local_count, "section fragment_indices"
            )
            if local_used.intersection(selected):
                raise CandidateOrganizationPartitionError(
                    "leaf map repeats a selected fragment"
                )
            if any(
                bank_groups[local_to_global[index]]["materialization_state"]
                != "materialized"
                for index in selected
            ):
                raise CandidateOrganizationPartitionError(
                    "leaf map selected an unresolved group"
                )
            local_used.update(selected)
            global_selected = tuple(
                sorted(local_to_global[index] for index in selected)
            )
            roles = section.get("paper_roles")
            if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
                raise CandidateOrganizationPartitionError(
                    "leaf paper_roles must be indexed pairs"
                )
            translated_roles: list[tuple[str, int]] = []
            seen_role_papers: set[str] = set()
            for role in roles:
                if (
                    not isinstance(role, Sequence)
                    or isinstance(role, (str, bytes))
                    or len(role) != 2
                ):
                    raise CandidateOrganizationPartitionError(
                        "leaf role must be a positional pair"
                    )
                paper_index, role_local = role
                if (
                    type(paper_index) is not int
                    or not 0 <= paper_index < len(part.paper_keys)
                    or type(role_local) is not int
                    or role_local not in selected
                ):
                    raise CandidateOrganizationPartitionError(
                        "leaf role has an invalid paper or fragment index"
                    )
                paper_key = part.paper_keys[paper_index]
                global_role = local_to_global[role_local]
                role_entry = bank_groups[global_role]
                if paper_key not in {
                    str(slot["paper_key"]) for slot in role_entry["support_slots"]
                }:
                    raise CandidateOrganizationPartitionError(
                        "leaf role fragment is not owned by its paper"
                    )
                text = role_entry.get("text")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or paper_key in seen_role_papers
                ):
                    raise CandidateOrganizationPartitionError(
                        "leaf role is empty or repeats a paper"
                    )
                seen_role_papers.add(paper_key)
                translated_roles.append((paper_key, global_role))
                role_texts.setdefault(paper_key, set()).add(text.casefold())
                role_counts[paper_key] = role_counts.get(paper_key, 0) + 1
            sections.append(
                CandidateOrganizationSectionChoiceV1(
                    part.partition_index,
                    section_id,
                    metadata_id,
                    global_selected,
                    tuple(sorted(translated_roles)),
                )
            )

        local_deferred: set[int] = set()
        raw_deferred = raw_map.get("deferred")
        if not isinstance(raw_deferred, Sequence) or isinstance(
            raw_deferred, (str, bytes)
        ):
            raise CandidateOrganizationPartitionError(
                "leaf map deferred must be an array"
            )
        for row in raw_deferred:
            if not isinstance(row, Mapping) or not isinstance(
                row.get("reason_code"), str
            ):
                raise CandidateOrganizationPartitionError(
                    "leaf deferred row has no reason code"
                )
            reason = str(row["reason_code"])
            for local in local_indices(
                row.get("fragment_indices"), local_count, "deferred fragment_indices"
            ):
                if local in local_used or local in local_deferred:
                    raise CandidateOrganizationPartitionError(
                        "leaf used/deferred indices overlap"
                    )
                entry = bank_groups[local_to_global[local]]
                if (entry["materialization_state"] == "unresolved") != (
                    reason == "optional_unresolved"
                ):
                    raise CandidateOrganizationPartitionError(
                        "leaf deferral reason differs from materialization state"
                    )
                local_deferred.add(local)
                global_index = local_to_global[local]
                deferred_global.add(global_index)
                deferred_reasons[global_index] = reason
        if local_used | local_deferred != set(range(local_count)):
            raise CandidateOrganizationPartitionError(
                "leaf used and deferred indices do not partition its groups"
            )
        used_global.update(local_to_global[local] for local in local_used)

    all_global = set(range(plan.bank_group_count))
    if used_global & deferred_global or used_global | deferred_global != all_global:
        raise CandidateOrganizationPartitionError(
            "global used/deferred indices do not partition the full bank"
        )
    used_ids = {group_ids[index] for index in used_global}
    missing = sorted(set(plan.required_group_ids) - used_ids)
    if missing:
        raise CandidateOrganizationPartitionError(
            f"required group {missing[0]} was not selected"
        )
    for paper, group_id in plan.required_paper_witnesses:
        index = group_index_by_id[group_id]
        if index not in used_global or paper not in {
            str(slot["paper_key"]) for slot in bank_groups[index]["support_slots"]
        }:
            raise CandidateOrganizationPartitionError(
                f"required paper {paper} witness was not selected"
            )
    collision = next(
        (
            paper
            for paper, count in role_counts.items()
            if count > 1 and len(role_texts.get(paper, set())) < count
        ),
        None,
    )
    if collision:
        raise CandidateOrganizationPartitionError(
            f"duplicate source-owned paper role collision: {collision}"
        )

    sections.sort(
        key=lambda item: (
            item.metadata_index,
            item.global_fragment_indices,
            item.partition_index,
            item.section_index,
        )
    )
    return CandidateOrganizationPartitionAccountingV1(
        bank_hash,
        tuple(sorted(bindings.items())),
        tuple(sorted(used_global)),
        tuple(sorted(deferred_global)),
        tuple(sorted(deferred_reasons.items())),
        tuple(sections),
    )


__all__ = [
    "CandidateOrganizationPartitionAccountingV1",
    "CandidateOrganizationPartitionError",
    "CandidateOrganizationPartitionPlanV1",
    "CandidateOrganizationPartitionV1",
    "CandidateOrganizationSectionChoiceV1",
    "account_candidate_organization_partition_maps_v1",
    "plan_candidate_organization_partitions_v1",
    "project_candidate_organization_partition_v1",
]

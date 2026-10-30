"""Exact, source-closed packing of critique claim/support atoms."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from outline.critique_paper_role_projection import (
    PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
    expand_critique_paper_roles_v1,
    project_critique_paper_roles_v1,
)
from runtime.provider_runtime import hash_json

_CLAIM_ARRAY_REF_FIELD = "claim_array_ref"
_SUPPORT_FIELD = "claim_support"
CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1 = (
    "A candidate section may be a partial review view of its unchanged parent "
    "section. Resolve local claim indices only within that exact section's "
    "claims array. atom_coverage_index maps each local claim occurrence and "
    "support row to its exact parent index; all support rows for a selected "
    "claim and all claims in a listed comparison group are present together. "
    "The parent section_id remains exact; local paper_keys and relation_ids "
    "are restricted to selected source support and bound to the full parent "
    "arrays by the coverage hashes. Absence from this shard does not mean "
    "absence from the parent."
)


class CritiqueClaimSupportPackingError(ValueError):
    """Raised when source-closed critique atoms cannot be packed safely."""


@dataclass(frozen=True, slots=True)
class CritiqueComparisonClaimGroupV1:
    """Claim occurrences that must stay together as a cross-paper comparison."""

    group_id: str
    claim_indices: tuple[int, ...]
    required_paper_keys: tuple[str, ...]
    relation_ids: tuple[str, ...] = ()


MeasureRequest = Callable[[Mapping[str, Any]], int]


def pack_critique_claim_support_shards_v1(
    candidate_id: str,
    parent_candidate_hash: str,
    candidate_section: Mapping[str, Any],
    section_evidence: Mapping[str, Any],
    *,
    input_cap_tokens: int,
    measure_request: MeasureRequest,
    comparison_groups: Sequence[CritiqueComparisonClaimGroupV1] | None = None,
    must_preserve_parent_section: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Pack whole claim occurrences with every exact support row attached.

    ``candidate_section`` is one complete compact section. ``section_evidence``
    is its complete compact support projection with parent-local claim indices.
    Callers supply the actual serialized-request estimator; this helper never
    estimates by characters or changes claims/support to meet a cap.

    Explicit comparison groups keep all named claim occurrences and all their
    paper support in one atom. Each output retains the complete parent section
    identity and an index coverage map so duplicate claim text cannot collapse
    distinct occurrences during merge.
    """

    if not isinstance(candidate_id, str) or not candidate_id:
        _fail("candidate_id is required")
    if not isinstance(parent_candidate_hash, str) or not parent_candidate_hash:
        _fail("parent candidate hash is required")
    if type(input_cap_tokens) is not int or input_cap_tokens <= 0:
        _fail("input cap must be a positive integer")
    if type(must_preserve_parent_section) is not bool:
        _fail("must_preserve_parent_section must be a boolean")
    if not callable(measure_request):
        _fail("exact request measurement callback is required")

    candidate_parent = dict(candidate_section)
    evidence_parent = dict(section_evidence)
    section_id = candidate_parent.get("section_id")
    if not isinstance(section_id, str) or not section_id:
        _fail("parent section_id is required")
    if evidence_parent.get("candidate_id") != candidate_id:
        _fail("support projection has a foreign candidate_id")
    if evidence_parent.get("section_id") != section_id:
        _fail("candidate and support section IDs differ")

    raw_claims = candidate_parent.get("claims")
    raw_support_rows = evidence_parent.get(_SUPPORT_FIELD)
    if (
        not isinstance(raw_claims, Sequence)
        or isinstance(raw_claims, (str, bytes))
        or not raw_claims
        or any(not isinstance(claim, str) or not claim for claim in raw_claims)
    ):
        _fail("parent claims must be a nonempty array of exact text")
    if not isinstance(raw_support_rows, Sequence) or isinstance(raw_support_rows, (str, bytes)):
        _fail("compact section evidence must contain a claim_support array")
    claims = cast(Sequence[str], raw_claims)
    support_rows = cast(Sequence[Mapping[str, Any]], raw_support_rows)
    if not support_rows:
        _fail("every parent claim needs complete source support")

    parent_claim_count = len(claims)
    parent_support_count = len(support_rows)
    expected_claim_ref = {
        "candidate_id": candidate_id,
        "section_id": section_id,
        "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
    }
    if evidence_parent.get(_CLAIM_ARRAY_REF_FIELD) != expected_claim_ref:
        _fail("support projection claim_array_ref differs from its parent section")

    candidate_parent = _normalize_role_projection(candidate_id, candidate_parent)
    candidate_roles = dict(candidate_parent.get("paper_roles") or {})
    evidence_roles = evidence_parent.get("paper_roles")
    if evidence_roles is None:
        evidence_roles = {}
    if not isinstance(evidence_roles, Mapping):
        _fail("support projection paper_roles must be an object")
    evidence_parent["paper_roles"] = dict(evidence_roles)
    evidence_parent = _normalize_evidence_roles(
        candidate_id,
        candidate_parent,
        evidence_parent,
    )
    if dict(evidence_parent.get("paper_roles") or {}) != candidate_roles:
        _fail("candidate and support projection paper_roles differ")
    if evidence_parent.get("paper_role_claim_array_ref") != candidate_parent.get(
        "paper_role_claim_array_ref"
    ):
        _fail("candidate and support paper-role claim references differ")

    parent_papers = _string_ids(candidate_parent.get("paper_keys"), "parent paper_keys")
    parent_paper_set = set(parent_papers)
    if not set(candidate_roles).issubset(parent_paper_set):
        _fail("parent paper_roles contains a paper outside parent paper_keys")
    evidence_papers = _string_ids(evidence_parent.get("paper_keys"), "support paper_keys")
    if parent_papers != evidence_papers:
        _fail("candidate and support paper_keys differ")
    parent_relations = _string_ids(
        candidate_parent.get("relation_ids"), "parent relation_ids", allow_empty=True
    )
    evidence_relations = _string_ids(
        evidence_parent.get("relation_ids"), "support relation_ids", allow_empty=True
    )
    if parent_relations != evidence_relations:
        _fail("candidate and support relation_ids differ")

    support_by_claim: dict[int, list[int]] = {
        index: [] for index in range(parent_claim_count)
    }
    seen_slot_ids: set[str] = set()
    for support_index, row in enumerate(support_rows):
        if not isinstance(row, Mapping):
            _fail("claim_support contains a non-object row")
        raw_claim_index = row.get("claim_index")
        if type(raw_claim_index) is not int or not 0 <= raw_claim_index < parent_claim_count:
            _fail("claim_support contains an unmatched parent claim index")
        claim_index = cast(int, raw_claim_index)
        support_section_id = row.get("section_id")
        if support_section_id is not None and support_section_id != section_id:
            _fail("claim_support contains a foreign section row")
        support_candidate_id = row.get("candidate_id")
        if support_candidate_id is not None and support_candidate_id != candidate_id:
            _fail("claim_support contains a foreign candidate row")
        slot_id = row.get("claim_slot_id")
        if not isinstance(slot_id, str) or not slot_id or slot_id in seen_slot_ids:
            _fail("claim_support contains a missing or duplicate claim_slot_id")
        seen_slot_ids.add(slot_id)
        paper_key = row.get("paper_key")
        if not isinstance(paper_key, str) or paper_key not in parent_paper_set:
            _fail("claim_support contains a paper outside its parent section")
        row_relations = _string_ids(
            row.get("relation_ids") or (),
            "claim_support relation_ids",
            allow_empty=True,
        )
        if not set(row_relations).issubset(set(parent_relations)):
            _fail("claim_support contains a relation outside its parent section")
        support_by_claim[claim_index].append(support_index)
    if any(not indices for indices in support_by_claim.values()):
        _fail("every parent claim needs at least one complete support row")
    supported_papers = {str(row.get("paper_key") or "") for row in support_rows}
    parent_role_texts = _expanded_roles(candidate_id, candidate_parent)
    if not set(parent_role_texts).issubset(supported_papers):
        _fail("a paper role cannot be assigned to any supported claim atom")

    atoms, comparison_group_ids = _build_atoms(
        parent_claim_count,
        support_by_claim,
        support_rows,
        set(parent_papers),
        set(parent_relations),
        comparison_groups,
    )
    parent_section_hash = hash_json(
        {
            "candidate_section": candidate_parent,
            "section_evidence": evidence_parent,
        }
    )

    if must_preserve_parent_section and not comparison_groups:
        projection = _build_projection(
            candidate_id=candidate_id,
            parent_candidate_hash=parent_candidate_hash,
            parent_section_hash=parent_section_hash,
            candidate_section=candidate_parent,
            section_evidence=evidence_parent,
            atoms=[tuple(range(parent_claim_count))],
            support_by_claim=support_by_claim,
            support_rows=support_rows,
            comparison_group_ids=comparison_group_ids,
            parent_paper_keys=parent_papers,
            parent_relation_ids=parent_relations,
            parent_claim_count=parent_claim_count,
            parent_support_count=parent_support_count,
            parent_role_texts=parent_role_texts,
            shard_index=1,
        )
        tokens = _measure(measure_request, projection)
        if tokens > input_cap_tokens:
            _fail(
                "comparison-marked section has no proven comparison atom mapping "
                "and cannot be split"
            )
        projection["estimated_input_tokens"] = tokens
        return (projection,)

    packed_atoms: list[list[tuple[int, ...]]] = []
    packed_payloads: list[dict[str, Any]] = []
    current_atoms: list[tuple[int, ...]] = []
    for atom in atoms:
        trial_atoms = [*current_atoms, atom]
        trial_projection = _build_projection(
            candidate_id=candidate_id,
            parent_candidate_hash=parent_candidate_hash,
            parent_section_hash=parent_section_hash,
            candidate_section=candidate_parent,
            section_evidence=evidence_parent,
            atoms=trial_atoms,
            support_by_claim=support_by_claim,
            support_rows=support_rows,
            comparison_group_ids=comparison_group_ids,
            parent_paper_keys=parent_papers,
            parent_relation_ids=parent_relations,
            parent_claim_count=parent_claim_count,
            parent_support_count=parent_support_count,
            parent_role_texts=parent_role_texts,
        )
        tokens = _measure(measure_request, trial_projection)
        if tokens <= input_cap_tokens:
            current_atoms = trial_atoms
            continue

        if current_atoms:
            packed_atoms.append(current_atoms)
            current_atoms = []
        atom_projection = _build_projection(
            candidate_id=candidate_id,
            parent_candidate_hash=parent_candidate_hash,
            parent_section_hash=parent_section_hash,
            candidate_section=candidate_parent,
            section_evidence=evidence_parent,
            atoms=[atom],
            support_by_claim=support_by_claim,
            support_rows=support_rows,
            comparison_group_ids=comparison_group_ids,
            parent_paper_keys=parent_papers,
            parent_relation_ids=parent_relations,
            parent_claim_count=parent_claim_count,
            parent_support_count=parent_support_count,
            parent_role_texts=parent_role_texts,
        )
        atom_tokens = _measure(measure_request, atom_projection)
        if atom_tokens > input_cap_tokens:
            _fail(
                "one complete claim/support atom exceeds the exact request cap"
            )
        current_atoms = [atom]
    if current_atoms:
        packed_atoms.append(current_atoms)

    for shard_index, shard_atoms in enumerate(packed_atoms, start=1):
        projection = _build_projection(
            candidate_id=candidate_id,
            parent_candidate_hash=parent_candidate_hash,
            parent_section_hash=parent_section_hash,
            candidate_section=candidate_parent,
            section_evidence=evidence_parent,
            atoms=shard_atoms,
            support_by_claim=support_by_claim,
            support_rows=support_rows,
            comparison_group_ids=comparison_group_ids,
            parent_paper_keys=parent_papers,
            parent_relation_ids=parent_relations,
            parent_claim_count=parent_claim_count,
            parent_support_count=parent_support_count,
            parent_role_texts=parent_role_texts,
            shard_index=shard_index,
        )
        tokens = _measure(measure_request, projection)
        if tokens > input_cap_tokens:
            _fail("final exact serialized claim/support shard exceeds its cap")
        projection["estimated_input_tokens"] = tokens
        packed_payloads.append(projection)

    _validate_full_coverage(
        packed_payloads,
        parent_claim_count=parent_claim_count,
        parent_support_count=parent_support_count,
        parent_role_texts=parent_role_texts,
        candidate_id=candidate_id,
        comparison_groups=comparison_groups,
    )
    return tuple(packed_payloads)


def _build_atoms(
    claim_count: int,
    support_by_claim: Mapping[int, Sequence[int]],
    support_rows: Sequence[Any],
    parent_papers: set[str],
    parent_relations: set[str],
    comparison_groups: Sequence[CritiqueComparisonClaimGroupV1] | None,
) -> tuple[list[tuple[int, ...]], dict[str, dict[str, Any]]]:
    group_by_claim: dict[int, str] = {}
    group_claims: dict[str, dict[str, Any]] = {}
    for group in comparison_groups or ():
        if not isinstance(group, CritiqueComparisonClaimGroupV1):
            _fail("comparison groups must use the typed V1 contract")
        if not isinstance(group.group_id, str) or not group.group_id:
            _fail("comparison group_id is required")
        if group.group_id in group_claims:
            _fail("comparison group_id is duplicated")
        claim_indices = _integer_indices(
            group.claim_indices,
            upper_bound=claim_count,
            label="comparison group claim_indices",
        )
        if not claim_indices:
            _fail("comparison groups must contain at least one claim index")
        required_papers = _string_ids(
            group.required_paper_keys,
            "comparison group required_paper_keys",
        )
        if not required_papers:
            _fail("comparison groups must require at least one paper")
        if not set(required_papers).issubset(parent_papers):
            _fail("comparison group papers are absent from its parent section")
        relation_ids = _string_ids(
            group.relation_ids,
            "comparison group relation_ids",
            allow_empty=True,
        )
        if not set(relation_ids).issubset(parent_relations):
            _fail("comparison group relations are absent from its parent section")
        support_papers = {
            str(support_rows[support_index].get("paper_key") or "")
            for claim_index in claim_indices
            for support_index in support_by_claim[claim_index]
        }
        if not set(required_papers).issubset(support_papers):
            _fail("comparison claim group lacks complete required paper support")
        for claim_index in claim_indices:
            if claim_index in group_by_claim:
                _fail("comparison claim groups overlap")
            group_by_claim[claim_index] = group.group_id
        group_claims[group.group_id] = {
            "claim_indices": claim_indices,
            "required_paper_keys": required_papers,
            "relation_ids": relation_ids,
        }

    atoms: list[tuple[int, ...]] = []
    emitted_groups: set[str] = set()
    for claim_index in range(claim_count):
        group_id = group_by_claim.get(claim_index)
        if group_id is None:
            atoms.append((claim_index,))
        elif group_id not in emitted_groups:
            atoms.append(tuple(group_claims[group_id]["claim_indices"]))
            emitted_groups.add(group_id)
    atoms.sort(key=lambda values: min(values))
    return atoms, group_claims


def _build_projection(
    *,
    candidate_id: str,
    parent_candidate_hash: str,
    parent_section_hash: str,
    candidate_section: Mapping[str, Any],
    section_evidence: Mapping[str, Any],
    atoms: Sequence[Sequence[int]],
    support_by_claim: Mapping[int, Sequence[int]],
    support_rows: Sequence[Any],
    comparison_group_ids: Mapping[str, Mapping[str, Any]],
    parent_paper_keys: Sequence[str],
    parent_relation_ids: Sequence[str],
    parent_claim_count: int,
    parent_support_count: int,
    parent_role_texts: Mapping[str, str],
    shard_index: int = 0,
) -> dict[str, Any]:
    claim_indices = sorted({index for atom in atoms for index in atom})
    local_by_parent = {parent: local for local, parent in enumerate(claim_indices)}
    support_indices = [
        index
        for index, row in enumerate(support_rows)
        if row.get("claim_index") in local_by_parent
    ]
    local_section = dict(candidate_section)
    local_section["claims"] = [candidate_section["claims"][index] for index in claim_indices]
    if "claim_count" in local_section:
        local_section["claim_count"] = len(claim_indices)
    local_evidence = dict(section_evidence)
    local_support: list[dict[str, Any]] = []
    for support_index in support_indices:
        row = dict(support_rows[support_index])
        row["claim_index"] = local_by_parent[row["claim_index"]]
        local_support.append(row)
    local_evidence[_SUPPORT_FIELD] = local_support

    selected_groups = {
        group_id: dict(contract)
        for group_id, contract in comparison_group_ids.items()
        if set(contract["claim_indices"]).issubset(local_by_parent)
    }
    selected_support_papers = {
        str(support_rows[index].get("paper_key") or "") for index in support_indices
    }
    selected_roles: dict[str, str | int] = {}
    local_claim_index_by_text: dict[str, int] = {}
    for local_index, claim_text in enumerate(local_section["claims"]):
        local_claim_index_by_text.setdefault(claim_text, local_index)
    for paper_key, role_text in parent_role_texts.items():
        if paper_key not in selected_support_papers:
            continue
        local_role_index = local_claim_index_by_text.get(role_text)
        selected_roles[paper_key] = (
            local_role_index if local_role_index is not None else role_text
        )
    local_section["paper_roles"] = selected_roles
    local_evidence["paper_roles"] = dict(selected_roles)
    if any(type(value) is int for value in selected_roles.values()):
        role_ref = {
            "candidate_id": candidate_id,
            "section_id": str(candidate_section["section_id"]),
            "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
        }
        local_section["paper_role_claim_array_ref"] = role_ref
        local_evidence["paper_role_claim_array_ref"] = dict(role_ref)
    else:
        local_section.pop("paper_role_claim_array_ref", None)
        local_evidence.pop("paper_role_claim_array_ref", None)
    local_paper_set = selected_support_papers | set(selected_roles)
    covers_full_section = set(claim_indices) == set(range(parent_claim_count))
    local_section["paper_keys"] = (
        list(parent_paper_keys)
        if covers_full_section
        else [paper_key for paper_key in parent_paper_keys if paper_key in local_paper_set]
    )
    support_relation_ids = {
        str(relation_id)
        for index in support_indices
        for relation_id in (support_rows[index].get("relation_ids") or ())
        if str(relation_id)
    }
    for group_id, contract in selected_groups.items():
        support_relation_ids.update(str(item) for item in contract["relation_ids"])
        if group_id in parent_relation_ids:
            support_relation_ids.add(group_id)
    local_relations = (
        list(parent_relation_ids)
        if covers_full_section
        else [
            relation_id
            for relation_id in parent_relation_ids
            if relation_id in support_relation_ids
        ]
    )
    local_section["relation_ids"] = local_relations
    local_evidence["paper_keys"] = list(local_section["paper_keys"])
    local_evidence["relation_ids"] = list(local_relations)
    coverage_body = {
        "schema_version": "critique-claim-support-atom-coverage/v1",
        "candidate_id": candidate_id,
        "section_id": str(candidate_section["section_id"]),
        "shard_index": shard_index,
        "parent_candidate_hash": parent_candidate_hash,
        "parent_section_hash": parent_section_hash,
        "parent_claim_count": parent_claim_count,
        "parent_support_count": parent_support_count,
        "parent_paper_key_count": len(parent_paper_keys),
        "parent_paper_keys_hash": hash_json(list(parent_paper_keys)),
        "parent_relation_ids_hash": hash_json(list(parent_relation_ids)),
        "local_to_parent_claim_indices": claim_indices,
        "local_to_parent_support_indices": support_indices,
        "atomic_claim_groups": [list(atom) for atom in atoms],
        "comparison_groups": {
            key: {
                "claim_indices": list(value["claim_indices"]),
                "required_paper_keys": list(value["required_paper_keys"]),
                "relation_ids": list(value["relation_ids"]),
            }
            for key, value in sorted(selected_groups.items())
        },
    }
    coverage = {**coverage_body, "content_hash": hash_json(coverage_body)}
    return {
        "candidate_id": candidate_id,
        "section_id": str(candidate_section["section_id"]),
        "parent_candidate_hash": parent_candidate_hash,
        "parent_section_hash": parent_section_hash,
        "candidate_section": local_section,
        "section_evidence": local_evidence,
        "atom_coverage_index": coverage,
        "claim_support_atom_contract": CRITIQUE_CLAIM_SUPPORT_ATOM_CONTRACT_V1,
    }


def _normalize_role_projection(
    candidate_id: str,
    section: dict[str, Any],
) -> dict[str, Any]:
    roles = section.get("paper_roles")
    if not isinstance(roles, Mapping) or not roles:
        return section
    if section.get("paper_role_claim_array_ref") is None:
        return project_critique_paper_roles_v1(candidate_id, section)
    try:
        expand_critique_paper_roles_v1(candidate_id, section)
    except ValueError as exc:
        _fail(f"parent paper-role reference is invalid: {exc}")
    return section


def _normalize_evidence_roles(
    candidate_id: str,
    candidate_section: Mapping[str, Any],
    evidence_section: dict[str, Any],
) -> dict[str, Any]:
    candidate_roles = dict(candidate_section.get("paper_roles") or {})
    candidate_ref = candidate_section.get("paper_role_claim_array_ref")
    evidence_roles = evidence_section.get("paper_roles")
    if evidence_roles is None:
        evidence_roles = {}
    if not isinstance(evidence_roles, Mapping):
        _fail("support projection paper_roles must be an object")
    evidence_ref = evidence_section.get("paper_role_claim_array_ref")
    if evidence_ref is not None:
        if dict(evidence_ref) != dict(candidate_ref or {}):
            _fail("support paper-role reference differs from the candidate section")
        local_for_decode = dict(evidence_section)
        local_for_decode["claims"] = list(candidate_section.get("claims") or ())
        try:
            decoded = expand_critique_paper_roles_v1(candidate_id, local_for_decode)
        except ValueError as exc:
            _fail(f"support paper-role reference is invalid: {exc}")
        candidate_decoded = expand_critique_paper_roles_v1(candidate_id, candidate_section)
        if decoded.get("paper_roles") != candidate_decoded.get("paper_roles"):
            _fail("support paper-role text differs from the candidate section")
    elif dict(evidence_roles) != _expanded_roles(candidate_id, candidate_section):
        _fail("unreferenced support paper-role text differs from candidate roles")
    evidence_section["paper_roles"] = candidate_roles
    if candidate_ref is not None:
        evidence_section["paper_role_claim_array_ref"] = dict(candidate_ref)
    return evidence_section


def _expanded_roles(candidate_id: str, section: Mapping[str, Any]) -> dict[str, Any]:
    roles = section.get("paper_roles")
    if not isinstance(roles, Mapping):
        return {}
    if section.get("paper_role_claim_array_ref") is None:
        return dict(roles)
    try:
        return dict(expand_critique_paper_roles_v1(candidate_id, section)["paper_roles"])
    except ValueError as exc:
        _fail(f"parent paper-role reference is invalid: {exc}")


def _validate_full_coverage(
    shards: Sequence[Mapping[str, Any]],
    *,
    parent_claim_count: int,
    parent_support_count: int,
    parent_role_texts: Mapping[str, str],
    candidate_id: str,
    comparison_groups: Sequence[CritiqueComparisonClaimGroupV1] | None,
) -> None:
    claim_indices = [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_claim_indices"]
    ]
    support_indices = [
        index
        for shard in shards
        for index in shard["atom_coverage_index"]["local_to_parent_support_indices"]
    ]
    if sorted(claim_indices) != list(range(parent_claim_count)):
        _fail("packed shards do not cover every parent claim occurrence exactly once")
    if sorted(support_indices) != list(range(parent_support_count)):
        _fail("packed shards do not cover every parent support row exactly once")
    observed_roles: dict[str, set[str]] = {}
    for shard in shards:
        roles = _expanded_roles(candidate_id, shard["candidate_section"])
        for paper_key, role_text in roles.items():
            observed_roles.setdefault(paper_key, set()).add(role_text)
    if set(observed_roles) != set(parent_role_texts):
        _fail("packed shards do not cover every parent paper role")
    if any(
        values != {parent_role_texts[paper_key]}
        for paper_key, values in observed_roles.items()
    ):
        _fail("packed shards changed a parent paper role")
    for group in comparison_groups or ():
        holders = [
            shard
            for shard in shards
            if group.group_id in shard["atom_coverage_index"]["comparison_groups"]
        ]
        if len(holders) != 1:
            _fail("a complete comparison group was split across critique shards")


def _measure(callback: MeasureRequest, projection: Mapping[str, Any]) -> int:
    value = callback(projection)
    if type(value) is not int or value < 0:
        _fail("exact request measurement must return a nonnegative integer")
    return value


def _integer_indices(value: Any, *, upper_bound: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail(f"{label} must be an array")
    indices = tuple(value)
    if any(type(index) is not int or not 0 <= index < upper_bound for index in indices):
        _fail(f"{label} contains an invalid index")
    if len(set(indices)) != len(indices):
        _fail(f"{label} contains duplicate indices")
    return tuple(sorted(indices))


def _string_ids(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail(f"{label} must be an array")
    items = tuple(value)
    if any(not isinstance(item, str) or not item for item in items):
        _fail(f"{label} contains an invalid ID")
    if len(set(items)) != len(items):
        _fail(f"{label} contains duplicate IDs")
    if not allow_empty and not items:
        _fail(f"{label} must be nonempty")
    return items


def _fail(message: str) -> NoReturn:
    raise CritiqueClaimSupportPackingError(message)

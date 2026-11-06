"""Lossless aliases for duplicated final-arbitration ledger metadata."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from outline.candidate_arbitration_ledger_packing import expand_arbitration_final_ledger
from outline.v3_models import compute_v3_hash

_PROJECTION_SCHEMA = "outline_candidate_arbitration_final_projection/v1"
_PROJECTION_KEY = "arbitration_final_projection"
_CANDIDATE_HASH_PATH = "arbitration_evaluation_ledger.candidate_hash_values"
_ISSUES_PATH = "arbitration_evaluation_ledger.critique_issues"
_SECTION_INDEX_PATH = "arbitration_evaluation_ledger.candidate_section_index"
_ALIAS_ORDER = (
    "candidate_hashes_ref",
    "critique_disposition.issues_ref",
    "section_coordination_contract.candidate_section_hashes_ref",
)
_RESOLUTION_CONTRACT = (
    "Every *_ref field in arbitration_final_projection is an exact alias, not a summary. "
    "Resolve candidate_hashes_ref from the listed ledger_field, aligned in candidate_ids order. "
    "Resolve issues_ref to the complete encoded critique_issues array in the packed ledger and "
    "decode it with ledger_packing. Resolve candidate_section_hashes_ref from the referenced "
    "candidate_section_index rows, mapping candidate_id and section_id to the exact section_hash. "
    "Use every resolved value in the decision."
)


class ArbitrationFinalProjectionError(ValueError):
    """A projected final arbitration request is stale, foreign, or malformed."""


def project_arbitration_final_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Replace only byte-identical final-request duplicates with hash-bound references.

    A valid packed ledger must be bound to the request hierarchy before any
    field is projected. If an individual equality cannot be proved, that field
    remains literal. Invalid packed-ledger hashes fail through the canonical
    ledger expander.
    """

    if not isinstance(request, Mapping):
        raise TypeError("final arbitration request must be a mapping")
    projected = copy.deepcopy(dict(request))

    if _PROJECTION_KEY in projected:
        expand_arbitration_final_projection(projected)
        return projected

    packed = projected.get("arbitration_evaluation_ledger")
    if not isinstance(packed, Mapping) or not isinstance(packed.get("ledger_packing"), Mapping):
        return projected

    canonical = expand_arbitration_final_ledger(packed)
    if not _hierarchy_binds_ledger(projected, canonical):
        return projected

    aliases: list[str] = []

    candidate_ids = projected.get("candidate_ids")
    candidate_hashes = projected.get("candidate_hashes")
    if (
        isinstance(candidate_ids, list)
        and candidate_ids == canonical.get("candidate_ids")
        and isinstance(candidate_hashes, Mapping)
        and dict(candidate_hashes) == canonical.get("candidate_hashes")
    ):
        wire_hashes = packed.get("candidate_hash_values")
        expected_hashes = [
            canonical["candidate_hashes"][candidate_id]
            for candidate_id in canonical["candidate_ids"]
        ]
        if wire_hashes == expected_hashes:
            projected.pop("candidate_hashes")
            projected["candidate_hashes_ref"] = {
                "ledger_field": _CANDIDATE_HASH_PATH,
                "aligned_by": "candidate_ids order",
                "expected_map_sha256": compute_v3_hash(canonical["candidate_hashes"]),
            }
            aliases.append(_ALIAS_ORDER[0])

    disposition = projected.get("critique_disposition")
    if (
        isinstance(disposition, Mapping)
        and isinstance(disposition.get("issues"), list)
        and disposition["issues"] == canonical.get("critique_issues")
    ):
        projected_disposition = copy.deepcopy(dict(disposition))
        issues = projected_disposition.pop("issues")
        projected_disposition["issues_ref"] = {
            "ledger_field": _ISSUES_PATH,
            "expected_count": len(issues),
            "expected_sha256": compute_v3_hash(issues),
        }
        projected["critique_disposition"] = projected_disposition
        aliases.append(_ALIAS_ORDER[1])

    section_hashes = _candidate_section_hashes(canonical)
    contract = projected.get("section_coordination_contract")
    if (
        section_hashes is not None
        and isinstance(contract, Mapping)
        and contract.get("candidate_section_hashes") == section_hashes
    ):
        projected_contract = copy.deepcopy(dict(contract))
        projected_contract.pop("candidate_section_hashes")
        projected_contract["candidate_section_hashes_ref"] = {
            "ledger_field": _SECTION_INDEX_PATH,
            "candidate_key": "candidate_id",
            "section_id_field": "section_id",
            "hash_field": "section_hash",
            "expected_map_sha256": compute_v3_hash(section_hashes),
            "expected_candidate_section_pairs_sha256": canonical["coverage"]["candidate_section_pairs_hash"],
        }
        projected["section_coordination_contract"] = projected_contract
        aliases.append(_ALIAS_ORDER[2])

    if not aliases:
        return projected

    projected[_PROJECTION_KEY] = {
        "schema_version": _PROJECTION_SCHEMA,
        "resolution_contract": _RESOLUTION_CONTRACT,
        "aliases": aliases,
        "ledger_hash": canonical["ledger_hash"],
    }
    return projected


def expand_arbitration_final_projection(request: Mapping[str, Any]) -> dict[str, Any]:
    """Restore projected aliases to their literal values and verify every digest."""

    if not isinstance(request, Mapping):
        raise TypeError("final arbitration request must be a mapping")
    expanded = copy.deepcopy(dict(request))
    projection = expanded.get(_PROJECTION_KEY)
    if projection is None:
        return expanded
    if not isinstance(projection, Mapping) or projection.get("schema_version") != _PROJECTION_SCHEMA:
        raise ArbitrationFinalProjectionError("unsupported final arbitration projection schema")
    if projection.get("resolution_contract") != _RESOLUTION_CONTRACT:
        raise ArbitrationFinalProjectionError("final arbitration projection contract is missing or stale")
    aliases = projection.get("aliases")
    if not isinstance(aliases, list) or len(aliases) != len(set(aliases)) or any(
        alias not in _ALIAS_ORDER for alias in aliases
    ):
        raise ArbitrationFinalProjectionError("final arbitration projection aliases are invalid")

    packed = expanded.get("arbitration_evaluation_ledger")
    if not isinstance(packed, Mapping):
        raise ArbitrationFinalProjectionError("projected request has no packed evaluation ledger")
    canonical = expand_arbitration_final_ledger(packed)
    if (
        projection.get("ledger_hash") != canonical.get("ledger_hash")
        or not _hierarchy_binds_ledger(expanded, canonical)
    ):
        raise ArbitrationFinalProjectionError("projection is not bound to the canonical hierarchy ledger")

    if _ALIAS_ORDER[0] in aliases:
        reference = expanded.pop("candidate_hashes_ref", None)
        if not isinstance(reference, Mapping) or reference != {
            "ledger_field": _CANDIDATE_HASH_PATH,
            "aligned_by": "candidate_ids order",
            "expected_map_sha256": compute_v3_hash(canonical["candidate_hashes"]),
        }:
            raise ArbitrationFinalProjectionError("candidate hash reference is foreign or stale")
        if expanded.get("candidate_ids") != canonical["candidate_ids"]:
            raise ArbitrationFinalProjectionError("candidate hash reference order differs from the ledger")
        if packed.get("candidate_hash_values") != [
            canonical["candidate_hashes"][candidate_id] for candidate_id in canonical["candidate_ids"]
        ]:
            raise ArbitrationFinalProjectionError("candidate hash values do not match the canonical ledger")
        if "candidate_hashes" in expanded:
            raise ArbitrationFinalProjectionError("candidate hash reference conflicts with a literal map")
        expanded["candidate_hashes"] = copy.deepcopy(canonical["candidate_hashes"])
    elif "candidate_hashes_ref" in expanded:
        raise ArbitrationFinalProjectionError("unlisted candidate hash reference")

    if _ALIAS_ORDER[1] in aliases:
        disposition = expanded.get("critique_disposition")
        if not isinstance(disposition, Mapping):
            raise ArbitrationFinalProjectionError("projected issue disposition is missing")
        disposition_copy = copy.deepcopy(dict(disposition))
        reference = disposition_copy.pop("issues_ref", None)
        issues = canonical.get("critique_issues")
        if not isinstance(issues, list):
            raise ArbitrationFinalProjectionError("canonical critique issues must be an array")
        if not isinstance(reference, Mapping) or reference != {
            "ledger_field": _ISSUES_PATH,
            "expected_count": len(issues),
            "expected_sha256": compute_v3_hash(issues),
        }:
            raise ArbitrationFinalProjectionError("issue reference is foreign or stale")
        if "issues" in disposition_copy:
            raise ArbitrationFinalProjectionError("issue reference conflicts with literal issues")
        disposition_copy["issues"] = copy.deepcopy(issues)
        expanded["critique_disposition"] = disposition_copy
    elif isinstance(expanded.get("critique_disposition"), Mapping) and "issues_ref" in expanded["critique_disposition"]:
        raise ArbitrationFinalProjectionError("unlisted issue reference")

    if _ALIAS_ORDER[2] in aliases:
        contract = expanded.get("section_coordination_contract")
        if not isinstance(contract, Mapping):
            raise ArbitrationFinalProjectionError("section coordination contract is missing")
        contract_copy = copy.deepcopy(dict(contract))
        reference = contract_copy.pop("candidate_section_hashes_ref", None)
        expected_hashes = _candidate_section_hashes(canonical)
        if expected_hashes is None or not isinstance(reference, Mapping) or reference != {
            "ledger_field": _SECTION_INDEX_PATH,
            "candidate_key": "candidate_id",
            "section_id_field": "section_id",
            "hash_field": "section_hash",
            "expected_map_sha256": compute_v3_hash(expected_hashes),
            "expected_candidate_section_pairs_sha256": canonical["coverage"]["candidate_section_pairs_hash"],
        }:
            raise ArbitrationFinalProjectionError("section hash reference is foreign or stale")
        if "candidate_section_hashes" in contract_copy:
            raise ArbitrationFinalProjectionError("section hash reference conflicts with a literal map")
        contract_copy["candidate_section_hashes"] = copy.deepcopy(expected_hashes)
        expanded["section_coordination_contract"] = contract_copy
    elif (
        isinstance(expanded.get("section_coordination_contract"), Mapping)
        and "candidate_section_hashes_ref" in expanded["section_coordination_contract"]
    ):
        raise ArbitrationFinalProjectionError("unlisted section hash reference")

    expanded.pop(_PROJECTION_KEY)
    return expanded


def _hierarchy_binds_ledger(request: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    hierarchy = request.get("hierarchy")
    if not isinstance(hierarchy, Mapping):
        return False
    if hierarchy.get("ledger_hash") != canonical.get("ledger_hash"):
        return False
    coverage = hierarchy.get("coverage")
    return isinstance(coverage, Mapping) and dict(coverage) == canonical.get("coverage")


def _candidate_section_hashes(canonical: Mapping[str, Any]) -> dict[str, dict[str, str]] | None:
    section_index = canonical.get("candidate_section_index")
    candidate_ids = canonical.get("candidate_ids")
    coverage = canonical.get("coverage")
    if (
        not isinstance(section_index, Mapping)
        or not isinstance(candidate_ids, list)
        or set(section_index) != set(candidate_ids)
        or not isinstance(coverage, Mapping)
    ):
        return None
    expected: dict[str, dict[str, str]] = {}
    pairs: list[tuple[str, str]] = []
    for candidate_id in candidate_ids:
        rows = section_index.get(candidate_id)
        if not isinstance(rows, list):
            return None
        local: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping) or row.get("candidate_id") != candidate_id:
                return None
            section_id = row.get("section_id")
            section_hash = row.get("section_hash")
            if not isinstance(section_id, str) or not section_id or not isinstance(section_hash, str) or not section_hash:
                return None
            if section_id in local:
                return None
            local[section_id] = section_hash
            pairs.append((candidate_id, section_id))
        expected[candidate_id] = local
    pair_digest = compute_v3_hash(sorted(pairs))
    if (
        type(coverage.get("expected_candidate_section_count")) is not int
        or type(coverage.get("observed_candidate_section_count")) is not int
        or coverage.get("expected_candidate_section_count") != len(pairs)
        or coverage.get("observed_candidate_section_count") != len(pairs)
        or coverage.get("candidate_section_pairs_hash") != pair_digest
    ):
        return None
    return expected


__all__ = [
    "ArbitrationFinalProjectionError",
    "expand_arbitration_final_projection",
    "project_arbitration_final_request",
]

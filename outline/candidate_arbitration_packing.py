"""Lossless wire compaction for arbitration candidate source support.

Candidate generations frequently share the exact same source-closure rows
while proposing different prose.  The arbitration provider needs every claim
and every support identity, but does not need each source row repeated once
per candidate.  This module keeps the authored candidate payloads intact and
uses reversible indexes for repeated support rows and exact authored strings.
Different candidate wording and different source closures remain distinct.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

_PACKING_SCHEMA = "outline_candidate_support_catalog/v1"
_SOURCE_ID_ARRAY_FIELDS = ("source_claim_ids", "evidence_ids", "source_field_ids")
_PACKING_CONTRACT = {
    "schema_version": _PACKING_SCHEMA,
    "claim_support_refs": "Each pair is [section_claim_index, candidate_support_catalog_index], in original row order.",
    "source_id_refs": "Each *_ref field indexes the exact ordered array in candidate_source_id_catalog.",
    "text_refs": "Section claim_text_refs retain ordered claims; paper_role_text_refs retain each source role. Both index the full exact strings in candidate_text_catalog. No wording is summarized.",
    "candidate_claims": "Top-level claims/planned_claims are omitted only when identical to ordered section claims.",
    "paper_assignments": "Omitted only when identical to section_id/paper_keys derived from sections.",
    "shard_plan_receipt": "Hashes and counts bind the local full plan; critique results carry evaluated coverage.",
}


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("arbitration payload values must be canonical JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _compact_shard_plan(value: Any) -> dict[str, Any] | None:
    """Keep an auditable compact receipt for the exact local assignment plan."""

    if not isinstance(value, Mapping):
        return None
    assignment = value.get("group_assignment")
    deferred = value.get("deferred_optional_groups")
    if not isinstance(assignment, Mapping) or not isinstance(deferred, (Mapping, list)):
        return None
    receipt: dict[str, Any] = {
        "plan_hash": _digest(value),
        "shard_count": value.get("shard_count"),
        "group_assignment_count": len(assignment),
        "group_assignment_hash": _digest(assignment),
        "deferred_optional_group_count": len(deferred),
        "deferred_optional_groups_hash": _digest(deferred),
    }
    if "assignment_coverage_hash" in value:
        receipt["assignment_coverage_hash"] = copy.deepcopy(value["assignment_coverage_hash"])
    if "status" in value:
        receipt["status"] = copy.deepcopy(value["status"])
    return receipt


def pack_arbitration_candidate_support(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a provider-ready arbitration request with a shared support catalog.

    Candidate section support rows are cataloged by full canonical content
    after removing only their repeated authored ``claim`` text. Rows with the
    same claim-slot ID but different source closure remain distinct. Duplicated
    top-level ``claims`` and ``planned_claims`` are omitted only when they
    exactly equal the ordered flattening of section claims. A duplicated
    ``paper_assignments`` list is omitted only when it exactly matches section
    IDs and paper keys. The full shard plan is replaced by a hash/count
    receipt; its authoritative payload remains in the local candidate
    artifact, and critique results remain in this request.
    """

    if not isinstance(request, Mapping):
        raise TypeError("arbitration request must be a mapping")
    if request.get("candidate_support_packing") is not None:
        raise ValueError("arbitration candidate support is already packed")

    candidate_ids = request.get("candidate_ids")
    candidate_contents = request.get("candidate_contents")
    candidate_hashes = request.get("candidate_hashes")
    if not isinstance(candidate_ids, list) or any(not isinstance(item, str) or not item for item in candidate_ids):
        raise ValueError("arbitration candidate IDs must be a list of non-empty strings")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("arbitration candidate IDs must be unique")
    if not isinstance(candidate_contents, Mapping) or set(candidate_contents) != set(candidate_ids):
        raise ValueError("arbitration candidate contents must exactly match candidate IDs")
    if not isinstance(candidate_hashes, Mapping) or set(candidate_hashes) != set(candidate_ids):
        raise ValueError("arbitration candidate hashes must exactly match candidate IDs")

    catalog_by_bytes: dict[bytes, dict[str, Any]] = {}
    source_id_arrays_by_bytes: dict[bytes, list[Any]] = {}
    refs_by_candidate: dict[str, list[tuple[dict[str, Any], bool, list[tuple[int, bytes]]]]] = {}
    text_values: set[str] = set()

    for candidate_id in candidate_ids:
        candidate = candidate_contents[candidate_id]
        if not isinstance(candidate, Mapping):
            raise TypeError(f"candidate {candidate_id} must be a mapping")
        source_sections = candidate.get("sections")
        if not isinstance(source_sections, list):
            raise TypeError(f"candidate {candidate_id} sections must be a list")

        candidate_section_refs: list[tuple[dict[str, Any], bool, list[tuple[int, bytes]]]] = []
        for section_index, raw_section in enumerate(source_sections):
            if not isinstance(raw_section, Mapping):
                raise TypeError(f"candidate {candidate_id} section {section_index} must be a mapping")
            section = copy.deepcopy(dict(raw_section))
            claims = section.get("claims")
            if not isinstance(claims, list) or any(not isinstance(claim, str) for claim in claims):
                raise ValueError(f"candidate {candidate_id} section claims must be a list of strings")
            text_values.update(claims)
            roles = section.get("paper_roles")
            if isinstance(roles, Mapping) and all(isinstance(value, str) for value in roles.values()):
                text_values.update(roles.values())
            if "claim_support" not in section:
                candidate_section_refs.append((section, False, []))
                continue
            support_rows = section.pop("claim_support")
            if not isinstance(support_rows, list):
                raise TypeError(f"candidate {candidate_id} section claim_support must be a list")

            claim_indexes: dict[str, int] = {}
            for claim_index, claim in enumerate(claims):
                claim_indexes.setdefault(claim, claim_index)

            refs: list[tuple[int, bytes]] = []
            for row_index, raw_row in enumerate(support_rows):
                if not isinstance(raw_row, Mapping):
                    raise TypeError(
                        f"candidate {candidate_id} section support row {row_index} must be a mapping"
                    )
                claim = raw_row.get("claim")
                if not isinstance(claim, str) or claim not in claim_indexes:
                    raise ValueError(
                        f"candidate {candidate_id} support row is not bound to a section claim"
                    )
                support = {key: copy.deepcopy(value) for key, value in raw_row.items() if key != "claim"}
                for field in _SOURCE_ID_ARRAY_FIELDS:
                    source_ids = support.get(field)
                    if isinstance(source_ids, list):
                        source_ids_bytes = _canonical_json_bytes(source_ids)
                        source_id_arrays_by_bytes.setdefault(
                            source_ids_bytes,
                            json.loads(source_ids_bytes.decode("utf-8")),
                        )
                canonical = _canonical_json_bytes(support)
                catalog_by_bytes.setdefault(canonical, json.loads(canonical.decode("utf-8")))
                refs.append((claim_indexes[claim], canonical))
            candidate_section_refs.append((section, True, refs))

        refs_by_candidate[candidate_id] = candidate_section_refs

    catalog_bytes = sorted(catalog_by_bytes)
    catalog_indexes = {canonical: index for index, canonical in enumerate(catalog_bytes)}
    text_catalog = sorted(text_values)
    text_indexes = {value: index for index, value in enumerate(text_catalog)}
    source_id_array_bytes = sorted(source_id_arrays_by_bytes)
    source_id_array_indexes = {
        canonical: index for index, canonical in enumerate(source_id_array_bytes)
    }

    compact_catalog: list[dict[str, Any]] = []
    for canonical in catalog_bytes:
        row = copy.deepcopy(catalog_by_bytes[canonical])
        for field in _SOURCE_ID_ARRAY_FIELDS:
            source_ids = row.pop(field, None)
            if isinstance(source_ids, list):
                source_ids_bytes = _canonical_json_bytes(source_ids)
                row[f"{field}_ref"] = source_id_array_indexes[source_ids_bytes]
            elif field in catalog_by_bytes[canonical]:
                row[field] = copy.deepcopy(catalog_by_bytes[canonical][field])
        compact_catalog.append({key: row[key] for key in sorted(row)})

    packed = copy.deepcopy(dict(request))
    projected_contents: dict[str, Any] = {}
    for candidate_id in candidate_ids:
        original = candidate_contents[candidate_id]
        projected = copy.deepcopy(dict(original))
        sections: list[dict[str, Any]] = []
        per_candidate_sections = refs_by_candidate[candidate_id]
        if len(projected["sections"]) != len(per_candidate_sections):
            raise ValueError(f"candidate {candidate_id} section projection is inconsistent")
        for prepared_section, had_support, raw_refs in per_candidate_sections:
            if had_support:
                prepared_section["claim_support_refs"] = [
                    [claim_index, catalog_indexes[canonical]]
                    for claim_index, canonical in raw_refs
                ]
            prepared_section["claim_text_refs"] = [
                text_indexes[claim] for claim in prepared_section.pop("claims")
            ]
            prepared_roles = prepared_section.get("paper_roles")
            if isinstance(prepared_roles, Mapping) and all(isinstance(value, str) for value in prepared_roles.values()):
                prepared_section["paper_role_text_refs"] = {
                    paper: text_indexes[role] for paper, role in prepared_roles.items()
                }
                prepared_section.pop("paper_roles")
            sections.append(prepared_section)
        projected["sections"] = sections
        ordered_section_claims = [
            claim
            for section in original.get("sections", [])
            for claim in section.get("claims", [])
        ]
        for claim_field in ("claims", "planned_claims"):
            if claim_field in original and original[claim_field] == ordered_section_claims:
                projected.pop(claim_field, None)
        derived_paper_assignments = [
            {
                "section_id": str(section.get("section_id") or ""),
                "paper_keys": list(section.get("paper_keys") or []),
            }
            for section in original.get("sections", [])
            if isinstance(section, Mapping)
        ]
        if (
            "paper_assignments" in original
            and original["paper_assignments"] == derived_paper_assignments
        ):
            projected.pop("paper_assignments", None)
        shard_plan = original.get("shard_plan")
        compact_receipt = _compact_shard_plan(shard_plan)
        if compact_receipt is not None:
            projected.pop("shard_plan", None)
            projected["shard_plan_receipt"] = compact_receipt
        projected_contents[candidate_id] = projected

    packed["candidate_contents"] = projected_contents
    packed["candidate_support_catalog"] = compact_catalog
    packed["candidate_source_id_catalog"] = [
        copy.deepcopy(source_id_arrays_by_bytes[key]) for key in source_id_array_bytes
    ]
    packed["candidate_text_catalog"] = text_catalog
    packed["candidate_text_catalog_sha256"] = _digest(text_catalog)
    packed["candidate_support_packing"] = copy.deepcopy(_PACKING_CONTRACT)
    return packed


def expand_arbitration_candidate_support(request: Mapping[str, Any]) -> dict[str, Any]:
    """Restore exact support rows in a packed arbitration request.

    The function restores the source-support arrays losslessly. It leaves
    section-derived duplicate claim and paper-assignment summaries compact and
    does not recreate the local-only full shard plan; canonical candidates
    remain available by their unchanged candidate hashes.
    """

    if not isinstance(request, Mapping):
        raise TypeError("arbitration request must be a mapping")
    schema = request.get("candidate_support_packing")
    if not isinstance(schema, Mapping) or schema.get("schema_version") != _PACKING_SCHEMA:
        if schema is None:
            return copy.deepcopy(dict(request))
        raise ValueError("unsupported arbitration candidate support packing schema")

    candidate_ids = request.get("candidate_ids")
    candidate_contents = request.get("candidate_contents")
    catalog = request.get("candidate_support_catalog")
    source_id_catalog = request.get("candidate_source_id_catalog")
    text_catalog = request.get("candidate_text_catalog")
    if not isinstance(candidate_ids, list) or len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("packed arbitration candidate IDs must be a unique list")
    if not isinstance(candidate_contents, Mapping) or set(candidate_contents) != set(candidate_ids):
        raise ValueError("packed arbitration candidate contents must exactly match candidate IDs")
    if not isinstance(catalog, list) or any(not isinstance(row, Mapping) or "claim" in row for row in catalog):
        raise ValueError("packed arbitration support catalog is invalid")
    if not isinstance(source_id_catalog, list) or any(not isinstance(ids, list) for ids in source_id_catalog):
        raise ValueError("packed arbitration source ID catalog is invalid")
    if text_catalog is not None and (
        not isinstance(text_catalog, list)
        or any(not isinstance(value, str) for value in text_catalog)
        or request.get("candidate_text_catalog_sha256") != _digest(text_catalog)
    ):
        raise ValueError("packed arbitration text catalog identity is invalid")

    def restore_text(index: Any) -> str:
        if not isinstance(text_catalog, list) or type(index) is not int or index < 0 or index >= len(text_catalog):
            raise ValueError("packed arbitration has an invalid text reference")
        return text_catalog[index]

    expanded = copy.deepcopy(dict(request))
    expanded_contents: dict[str, Any] = {}
    for candidate_id in candidate_ids:
        candidate = candidate_contents[candidate_id]
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("sections"), list):
            raise TypeError(f"packed candidate {candidate_id} is invalid")
        expanded_candidate = copy.deepcopy(dict(candidate))
        expanded_sections = expanded_candidate["sections"]
        for section_index, section in enumerate(expanded_sections):
            if not isinstance(section, dict):
                continue
            if "claim_text_refs" in section:
                if "claims" in section or not isinstance(section["claim_text_refs"], list):
                    raise ValueError("packed section has conflicting claim text references")
                section["claims"] = [restore_text(index) for index in section.pop("claim_text_refs")]
            if "paper_role_text_refs" in section:
                if "paper_roles" in section or not isinstance(section["paper_role_text_refs"], Mapping):
                    raise ValueError("packed section has conflicting source role text references")
                section["paper_roles"] = {
                    paper: restore_text(index) for paper, index in section.pop("paper_role_text_refs").items()
                }
            if "claim_support_refs" not in section:
                continue
            if "claim_support" in section:
                raise ValueError("packed section cannot contain both support rows and support references")
            claims = section.get("claims")
            refs = section.pop("claim_support_refs")
            if not isinstance(claims, list) or any(not isinstance(claim, str) for claim in claims):
                raise ValueError(f"packed candidate {candidate_id} section claims are invalid")
            if not isinstance(refs, list):
                raise TypeError(f"packed candidate {candidate_id} section support references are invalid")

            support_rows: list[dict[str, Any]] = []
            for ref in refs:
                if (
                    not isinstance(ref, list)
                    or len(ref) != 2
                    or type(ref[0]) is not int
                    or type(ref[1]) is not int
                    or ref[0] < 0
                    or ref[0] >= len(claims)
                    or ref[1] < 0
                    or ref[1] >= len(catalog)
                ):
                    raise ValueError(
                        f"packed candidate {candidate_id} section {section_index} has an invalid support reference"
                    )
                row = copy.deepcopy(dict(catalog[ref[1]]))
                for field in _SOURCE_ID_ARRAY_FIELDS:
                    ref_key = f"{field}_ref"
                    if ref_key not in row:
                        continue
                    source_index = row.pop(ref_key)
                    if type(source_index) is not int or source_index < 0 or source_index >= len(source_id_catalog):
                        raise ValueError(
                            f"packed candidate {candidate_id} has an invalid source ID array reference"
                        )
                    row[field] = copy.deepcopy(source_id_catalog[source_index])
                row["claim"] = claims[ref[0]]
                support_rows.append(row)
            section["claim_support"] = support_rows
        expanded_contents[candidate_id] = expanded_candidate

    expanded["candidate_contents"] = expanded_contents
    expanded.pop("candidate_support_catalog", None)
    expanded.pop("candidate_source_id_catalog", None)
    expanded.pop("candidate_support_packing", None)
    expanded.pop("candidate_text_catalog", None)
    expanded.pop("candidate_text_catalog_sha256", None)
    return expanded


__all__ = ["expand_arbitration_candidate_support", "pack_arbitration_candidate_support"]

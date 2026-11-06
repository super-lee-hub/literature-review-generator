"""Reversible explicit catalogs for arbitration's final evaluation ledger.

The wire form retains all assessment meaning and source/issue identities. It
replaces repeated prose and string arrays with indexed, digest-bound catalogs;
candidate, leaf, and section references are checked against the canonical
ledger index before expansion.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from outline.v3_models import compute_v3_hash

_LEDGER_SCHEMA = "outline_candidate_arbitration_ledger/v1"
_PACKING_SCHEMA = "outline_candidate_arbitration_ledger_wire/v2"
_CLOSURE_REFS_PACKING_SCHEMA = "outline_candidate_arbitration_ledger_wire/v3"
_CLOSURE_HASH_ENCODING = {
    "schema_version": "outline_candidate_section_source_closure_refs/v1",
    "catalog": "ledger_text_catalog",
    "field": "source_closure_hash",
    "column": "source_closure_hash_text_ref",
    "resolution_contract": "Each source_closure_hash_text_ref indexes its exact lowercase SHA-256 in ledger_text_catalog; restore every literal source_closure_hash before canonical ledger validation.",
}
_CLAIM_PARTITION_WIRE_SCHEMA = "outline_candidate_claim_support_partition_wire/v1"
_CLAIM_PARTITION_SCHEMA = "outline_candidate_claim_support_partition/v1"
_PARTITION_PARENT_COLUMNS = (
    "comparison_parent_unit_id_ref", "candidate_id_ref", "section_id_ref", "section_hash_ref",
    "source_closure_hash_ref", "claim_count", "support_row_count", "slots",
)
_PARTITION_SLOT_COLUMNS = (
    "claim_slot_id_ref", "claim_occurrence_index", "support_row_index", "claim_hash_ref",
    "support_row_hash_ref", "comparison_scope_id_ref", "comparison_group_id_ref", "task_id_ref",
    "primary_claim_id_ref", "comparison_role_ref_or_minus_one", "claim_target_ids_array_ref",
)
_PARTITION_UNIT_COLUMNS = (
    "unit_id_ref", "part_hash_ref", "comparison_parent_group_ref", "parent_slot_positions",
)
_PARTITION_LEAF_COVERAGE_COLUMNS = (
    "leaf_id_ref", "candidate_id_ref", "parent_section_ref", "parent_slot_positions",
)
_PARTITION_ISSUE_COLUMNS = (
    "issue_id_ref", "scope_ref", "candidate_id_ref_or_minus_one", "target_ids_array_ref",
    "leaf_id_refs", "claim_slot_target_rows",
)
_PARTITION_ISSUE_SLOT_TARGET_COLUMNS = (
    "leaf_id_ref", "candidate_id_ref", "parent_section_ref", "parent_slot_positions",
)
_PARTITION_RECEIPT_PROJECTION = (
    "Derive exact part receipts from leaf_result_receipt_rows.unit_ids and unit_assignments; "
    "each assignment binds one parent section and the same positional slot set for every candidate."
)
_SECTION_INDEX_ROW_COLUMNS = (
    "section_id", "section_hash", "source_closure_hash", "title_text_ref", "goal_text_ref",
    "task_ids_array_ref", "paper_keys_array_ref", "relation_ids_array_ref", "claim_count",
    "support_row_count", "position",
)
_CLOSURE_REFS_SECTION_INDEX_ROW_COLUMNS = tuple(
    "source_closure_hash_text_ref" if field == "source_closure_hash" else field
    for field in _SECTION_INDEX_ROW_COLUMNS
)
_ASSESSMENT_ROW_COLUMNS = (
    "candidate_ref", "leaf_ref", "presence", "reviewed_section_refs", "coverage_assessment_text_ref",
    "evidence_assessment_text_ref", "structure_assessment_text_ref", "advantages_text_refs",
    "limitations_text_refs", "evidence_refs_array_ref", "critic_issue_ids_array_ref",
)
_LEAF_RECEIPT_ROW_COLUMNS = ("leaf_id", "unit_ids", "leaf_result_hash")
_TEXT_FIELDS = (
    "title",
    "goal",
    "coverage_assessment",
    "evidence_assessment",
    "structure_assessment",
    "message",
)
_ARRAY_FIELDS = ("task_ids", "paper_keys", "relation_ids", "evidence_refs", "critic_issue_ids", "target_ids")


class ArbitrationLedgerPackingError(ValueError):
    """The canonical ledger or its packed wire representation is invalid."""


def pack_arbitration_final_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Pack repeated ledger text/IDs as readable, hash-checked exact catalogs."""

    if not isinstance(ledger, Mapping):
        raise TypeError("arbitration final ledger must be a mapping")
    if ledger.get("ledger_packing") is not None:
        raise ArbitrationLedgerPackingError("arbitration final ledger is already packed")
    _validate_canonical_ledger(ledger)

    candidate_ids = list(ledger["candidate_ids"])
    candidate_index = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    leaves = list(ledger["leaf_result_receipts"])
    leaf_ids = [str(row["leaf_id"]) for row in leaves]
    leaf_index = {leaf_id: index for index, leaf_id in enumerate(leaf_ids)}
    section_index = ledger["candidate_section_index"]
    section_positions: dict[str, dict[str, int]] = {
        candidate_id: {
            str(row["section_id"]): index
            for index, row in enumerate(section_index[candidate_id])
        }
        for candidate_id in candidate_ids
    }

    text_values: set[str] = set()
    array_values: dict[str, list[str]] = {}

    def collect_text(value: Any, label: str) -> None:
        if not isinstance(value, str):
            raise ArbitrationLedgerPackingError(f"{label} must be a string")
        text_values.add(value)

    def collect_array(value: Any, label: str) -> None:
        rows = _string_array(value, label)
        key = _canonical(rows)
        array_values.setdefault(key, rows)

    for candidate_id in candidate_ids:
        for row in section_index[candidate_id]:
            for field in ("title", "goal"):
                if field in row:
                    collect_text(row[field], f"{candidate_id}.{field}")
            for field in ("task_ids", "paper_keys", "relation_ids"):
                if field in row:
                    collect_array(row[field], f"{candidate_id}.{field}")
    for row_index, row in enumerate(ledger["assessments"]):
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment"):
            collect_text(row.get(field), f"assessment[{row_index}].{field}")
        for field in ("advantages", "limitations"):
            values = _string_array(row.get(field), f"assessment[{row_index}].{field}")
            for value in values:
                collect_text(value, f"assessment[{row_index}].{field} item")
        for field in ("evidence_refs", "critic_issue_ids"):
            collect_array(row.get(field), f"assessment[{row_index}].{field}")
    for issue_index, issue in enumerate(ledger["critique_issues"]):
        if "message" in issue:
            collect_text(issue["message"], f"critique_issues[{issue_index}].message")
        for field in ("target_ids", "evidence_refs"):
            if field in issue:
                collect_array(issue[field], f"critique_issues[{issue_index}].{field}")

    text_catalog = sorted(text_values)
    text_refs = {value: index for index, value in enumerate(text_catalog)}
    array_catalog = [array_values[key] for key in sorted(array_values)]
    array_refs = {key: index for index, key in enumerate(sorted(array_values))}

    packed = copy.deepcopy(dict(ledger))
    packed_index: dict[str, list[list[Any]]] = {}
    for candidate_id in candidate_ids:
        projected_rows: list[list[Any]] = []
        for raw_row in section_index[candidate_id]:
            row = copy.deepcopy(dict(raw_row))
            if row.pop("candidate_id", None) != candidate_id:
                raise ArbitrationLedgerPackingError(
                    f"section index {candidate_id} has a foreign or missing candidate_id"
                )
            for field in ("title", "goal"):
                if field in row:
                    value = row.pop(field)
                    row[f"{field}_text_ref"] = text_refs[value]
            for field in ("task_ids", "paper_keys", "relation_ids"):
                if field in row:
                    row[f"{field}_array_ref"] = array_refs[_canonical(row.pop(field))]
            if set(row) != set(_SECTION_INDEX_ROW_COLUMNS):
                raise ArbitrationLedgerPackingError(
                    f"section index {candidate_id} has fields outside the explicit row codec"
                )
            projected_rows.append([row[field] for field in _SECTION_INDEX_ROW_COLUMNS])
        packed_index[candidate_id] = projected_rows
    packed.pop("candidate_section_index", None)
    packed["candidate_section_index"] = packed_index

    packed_assessments: list[list[Any]] = []
    for row_index, raw_row in enumerate(ledger["assessments"]):
        row = copy.deepcopy(dict(raw_row))
        candidate_id = str(row.pop("candidate_id"))
        leaf_id = str(row.pop("leaf_id"))
        candidate_hash = str(row.pop("candidate_hash"))
        if candidate_id not in candidate_index or ledger["candidate_hashes"].get(candidate_id) != candidate_hash:
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] candidate hash binding is invalid")
        if leaf_id not in leaf_index:
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] leaf reference is unknown")
        raw_section_ids = _string_array(row.pop("reviewed_section_ids"), f"assessment[{row_index}].reviewed_section_ids")
        raw_section_hashes = row.pop("section_hashes")
        if not isinstance(raw_section_hashes, Mapping) or set(raw_section_hashes) != set(raw_section_ids):
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] section hash closure is incomplete")
        section_refs: list[int] = []
        for section_id in raw_section_ids:
            position = section_positions[candidate_id].get(section_id)
            if position is None:
                raise ArbitrationLedgerPackingError(f"assessment[{row_index}] has a foreign section reference")
            expected_hash = str(section_index[candidate_id][position].get("section_hash") or "")
            if raw_section_hashes.get(section_id) != expected_hash:
                raise ArbitrationLedgerPackingError(f"assessment[{row_index}] section hash is stale")
            section_refs.append(position)
        row["candidate_ref"] = candidate_index[candidate_id]
        row["leaf_ref"] = leaf_index[leaf_id]
        row["reviewed_section_refs"] = section_refs
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment"):
            row[f"{field}_text_ref"] = text_refs[row.pop(field)]
        for field in ("advantages", "limitations"):
            row[f"{field}_text_refs"] = [text_refs[value] for value in _string_array(row.pop(field), field)]
        for field in ("evidence_refs", "critic_issue_ids"):
            row[f"{field}_array_ref"] = array_refs[_canonical(_string_array(row.pop(field), field))]
        if set(row) != set(_ASSESSMENT_ROW_COLUMNS):
            raise ArbitrationLedgerPackingError(
                f"assessment[{row_index}] has fields outside the explicit row codec"
            )
        packed_assessments.append([row[field] for field in _ASSESSMENT_ROW_COLUMNS])
    packed["assessments"] = packed_assessments

    packed_issues: list[dict[str, Any]] = []
    for issue in ledger["critique_issues"]:
        row = copy.deepcopy(dict(issue))
        if "message" in row:
            row["message_text_ref"] = text_refs[row.pop("message")]
        for field in ("target_ids", "evidence_refs"):
            if field in row:
                row[f"{field}_array_ref"] = array_refs[_canonical(_string_array(row.pop(field), field))]
        packed_issues.append(row)
    packed["critique_issues"] = packed_issues
    packed["candidate_hash_values"] = [str(ledger["candidate_hashes"][candidate_id]) for candidate_id in candidate_ids]
    packed.pop("candidate_hashes", None)
    receipt_rows: list[list[Any]] = []
    for row_index, receipt in enumerate(ledger["leaf_result_receipts"]):
        row = copy.deepcopy(dict(receipt))
        if set(row) != set(_LEAF_RECEIPT_ROW_COLUMNS):
            raise ArbitrationLedgerPackingError(
                f"leaf receipt[{row_index}] has fields outside the explicit row codec"
            )
        receipt_rows.append([row[field] for field in _LEAF_RECEIPT_ROW_COLUMNS])
    packed["leaf_result_receipt_rows"] = receipt_rows
    packed.pop("leaf_result_receipts", None)
    packed_coverage = copy.deepcopy(dict(packed["coverage"]))
    packed_coverage.pop("leaf_result_hashes", None)
    packed["coverage"] = packed_coverage
    packed["ledger_text_catalog"] = text_catalog
    packed["ledger_string_array_catalog"] = array_catalog
    packed["ledger_packing"] = {
        "schema_version": _PACKING_SCHEMA,
        "text_catalog_sha256": compute_v3_hash(text_catalog),
        "string_array_catalog_sha256": compute_v3_hash(array_catalog),
        "candidate_refs": "candidate_ref indexes candidate_ids; candidate_hash_values aligns with candidate_ids.",
        "leaf_refs": "leaf_ref indexes leaf_result_receipt_rows in original order.",
        "section_refs": "reviewed_section_refs indexes candidate_section_index[candidate_id] in original order; section_hashes are restored from those exact rows.",
        "text_refs": "*_text_ref indexes the exact UTF-8 strings in ledger_text_catalog; wording is never summarized.",
        "array_refs": "*_array_ref indexes the exact ordered string array in ledger_string_array_catalog.",
        "section_index_row_columns": list(_SECTION_INDEX_ROW_COLUMNS),
        "assessment_row_columns": list(_ASSESSMENT_ROW_COLUMNS),
        "leaf_receipt_row_columns": list(_LEAF_RECEIPT_ROW_COLUMNS),
        "leaf_result_hashes_are_derived_from_receipts": True,
    }
    if "claim_support_partition" in ledger:
        packed["claim_support_partition"] = _pack_claim_support_partition(
            ledger["claim_support_partition"], candidate_ids
        )
    packed["wire_body_hash"] = compute_v3_hash(packed)
    return _pack_repeated_section_closure_hashes(packed)


def _pack_repeated_section_closure_hashes(packed: dict[str, Any]) -> dict[str, Any]:
    """Use the existing exact text catalog only when closure refs save bytes."""

    hashes = [row[2] for rows in packed["candidate_section_index"].values() for row in rows]
    if (any(not _is_sha256(value) for value in hashes)
            or len(set(hashes)) == len(hashes)):
        return packed
    projected = copy.deepcopy(packed)
    catalog = projected["ledger_text_catalog"]
    catalog.extend(sorted(set(hashes) - set(catalog)))
    refs = {value: index for index, value in enumerate(catalog)}
    for rows in projected["candidate_section_index"].values():
        for row in rows:
            row[2] = refs[row[2]]
    packing = projected["ledger_packing"]
    packing["schema_version"] = _CLOSURE_REFS_PACKING_SCHEMA
    packing["section_source_closure_hash_encoding"] = dict(_CLOSURE_HASH_ENCODING)
    packing["section_index_row_columns"] = list(_CLOSURE_REFS_SECTION_INDEX_ROW_COLUMNS)
    packing["text_catalog_sha256"] = compute_v3_hash(catalog)
    projected.pop("wire_body_hash")
    projected["wire_body_hash"] = compute_v3_hash(projected)
    return projected if len(_canonical(projected)) < len(_canonical(packed)) else packed


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def expand_arbitration_final_ledger(packed: Mapping[str, Any]) -> dict[str, Any]:
    """Expand an indexed ledger and verify it matches its canonical ledger hash."""

    if not isinstance(packed, Mapping):
        raise TypeError("packed arbitration final ledger must be a mapping")
    packing = packed.get("ledger_packing")
    if packing is None:
        return copy.deepcopy(dict(packed))
    if not isinstance(packing, Mapping) or packing.get("schema_version") not in {
        _PACKING_SCHEMA, _CLOSURE_REFS_PACKING_SCHEMA,
    }:
        raise ArbitrationLedgerPackingError("unsupported arbitration ledger packing schema")
    closure_refs = packing["schema_version"] == _CLOSURE_REFS_PACKING_SCHEMA
    if closure_refs:
        if packing.get("section_source_closure_hash_encoding") != _CLOSURE_HASH_ENCODING:
            raise ArbitrationLedgerPackingError("unsupported section source-closure hash encoding")
        section_columns = _CLOSURE_REFS_SECTION_INDEX_ROW_COLUMNS
    else:
        if "section_source_closure_hash_encoding" in packing:
            raise ArbitrationLedgerPackingError("legacy ledger cannot declare section source-closure hash encoding")
        section_columns = _SECTION_INDEX_ROW_COLUMNS
    wire_hash = packed.get("wire_body_hash")
    wire_body = {key: value for key, value in packed.items() if key != "wire_body_hash"}
    if not isinstance(wire_hash, str) or wire_hash != compute_v3_hash(wire_body):
        raise ArbitrationLedgerPackingError("packed arbitration ledger wire hash is invalid")
    text_catalog = packed.get("ledger_text_catalog")
    array_catalog = packed.get("ledger_string_array_catalog")
    if not isinstance(text_catalog, list) or any(not isinstance(value, str) for value in text_catalog):
        raise ArbitrationLedgerPackingError("packed arbitration ledger text catalog is invalid")
    if not isinstance(array_catalog, list):
        raise ArbitrationLedgerPackingError("packed arbitration ledger string-array catalog is invalid")
    for index, row in enumerate(array_catalog):
        _string_array(row, f"ledger_string_array_catalog[{index}]")
    if packing.get("text_catalog_sha256") != compute_v3_hash(text_catalog):
        raise ArbitrationLedgerPackingError("packed arbitration ledger text catalog digest is invalid")
    if packing.get("string_array_catalog_sha256") != compute_v3_hash(array_catalog):
        raise ArbitrationLedgerPackingError("packed arbitration ledger string-array catalog digest is invalid")

    for field, expected in (
        ("section_index_row_columns", list(section_columns)),
        ("assessment_row_columns", list(_ASSESSMENT_ROW_COLUMNS)),
        ("leaf_receipt_row_columns", list(_LEAF_RECEIPT_ROW_COLUMNS)),
    ):
        if packing.get(field) != expected:
            raise ArbitrationLedgerPackingError(f"packed arbitration ledger {field} contract is invalid")
    if packing.get("leaf_result_hashes_are_derived_from_receipts") is not True:
        raise ArbitrationLedgerPackingError("packed arbitration ledger receipt coverage contract is invalid")

    expanded = copy.deepcopy(dict(packed))
    candidate_ids = _unique_nonempty_strings(expanded.get("candidate_ids"), "candidate_ids")
    raw_candidate_hashes = expanded.get("candidate_hash_values")
    raw_leaf_receipts = expanded.get("leaf_result_receipt_rows")
    index = expanded.get("candidate_section_index")
    if (
        not isinstance(raw_candidate_hashes, list)
        or len(raw_candidate_hashes) != len(candidate_ids)
        or any(not isinstance(value, str) or not value for value in raw_candidate_hashes)
    ):
        raise ArbitrationLedgerPackingError("packed arbitration ledger candidate hashes are incomplete")
    candidate_hashes = dict(zip(candidate_ids, raw_candidate_hashes, strict=True))
    if not isinstance(raw_leaf_receipts, list) or not isinstance(index, Mapping) or set(index) != set(candidate_ids):
        raise ArbitrationLedgerPackingError("packed arbitration ledger indices are incomplete")
    leaf_receipts: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_leaf_receipts):
        if not isinstance(raw_row, list) or len(raw_row) != len(_LEAF_RECEIPT_ROW_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed leaf receipt[{row_index}] has an invalid positional row")
        leaf_receipts.append(dict(zip(_LEAF_RECEIPT_ROW_COLUMNS, raw_row, strict=True)))
    leaf_ids = [str(row.get("leaf_id") or "") for row in leaf_receipts]
    if len(set(leaf_ids)) != len(leaf_ids) or any(not item for item in leaf_ids):
        raise ArbitrationLedgerPackingError("packed arbitration ledger leaf receipt identities are invalid")

    def text_at(reference: Any, label: str) -> str:
        return _catalog_at(text_catalog, reference, label)

    def array_at(reference: Any, label: str) -> list[str]:
        value = _catalog_at(array_catalog, reference, label)
        return _string_array(value, label)

    expanded_index: dict[str, list[dict[str, Any]]] = {}
    for candidate_id in candidate_ids:
        rows = index[candidate_id]
        if not isinstance(rows, list):
            raise ArbitrationLedgerPackingError(f"packed section index for {candidate_id} is invalid")
        decoded: list[dict[str, Any]] = []
        seen_indexed_sections: set[str] = set()
        for row_index, raw_row in enumerate(rows):
            if not isinstance(raw_row, list) or len(raw_row) != len(section_columns):
                raise ArbitrationLedgerPackingError(f"packed section index {candidate_id}[{row_index}] has an invalid positional row")
            row: dict[str, Any] = dict(zip(section_columns, raw_row, strict=True))
            row["candidate_id"] = candidate_id
            if closure_refs:
                source_hash = text_at(row.pop("source_closure_hash_text_ref"), f"section {candidate_id}.source_closure_hash")
                if not _is_sha256(source_hash):
                    raise ArbitrationLedgerPackingError("section source-closure hash reference is not a SHA-256")
                row["source_closure_hash"] = source_hash
            for field in ("title", "goal"):
                ref_field = f"{field}_text_ref"
                if ref_field in row:
                    row[field] = text_at(row.pop(ref_field), f"section {candidate_id}.{field}")
            for field in ("task_ids", "paper_keys", "relation_ids"):
                ref_field = f"{field}_array_ref"
                if ref_field in row:
                    row[field] = array_at(row.pop(ref_field), f"section {candidate_id}.{field}")
            section_id = _nonempty_string(row.get("section_id"), f"section {candidate_id}.section_id")
            if section_id in seen_indexed_sections:
                raise ArbitrationLedgerPackingError(f"packed section index for {candidate_id} repeats a section")
            seen_indexed_sections.add(section_id)
            _nonempty_string(row.get("section_hash"), f"section {candidate_id}.section_hash")
            decoded.append(row)
        expanded_index[candidate_id] = decoded
    expanded["candidate_section_index"] = expanded_index

    expanded["candidate_hashes"] = candidate_hashes
    expanded["leaf_result_receipts"] = leaf_receipts
    raw_assessments = expanded.get("assessments")
    if not isinstance(raw_assessments, list):
        raise ArbitrationLedgerPackingError("packed arbitration assessments must be an array")
    decoded_assessments: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_assessments):
        if not isinstance(raw_row, list) or len(raw_row) != len(_ASSESSMENT_ROW_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed assessment[{row_index}] has an invalid positional row")
        row: dict[str, Any] = dict(zip(_ASSESSMENT_ROW_COLUMNS, raw_row, strict=True))
        candidate_ref = row.pop("candidate_ref", None)
        leaf_ref = row.pop("leaf_ref", None)
        candidate_id = _catalog_at(candidate_ids, candidate_ref, f"assessment[{row_index}].candidate_ref")
        leaf_id = _catalog_at(leaf_ids, leaf_ref, f"assessment[{row_index}].leaf_ref")
        if not isinstance(candidate_id, str) or not isinstance(leaf_id, str):
            raise ArbitrationLedgerPackingError(f"packed assessment[{row_index}] has invalid identity refs")
        section_refs = row.pop("reviewed_section_refs", None)
        if not isinstance(section_refs, list):
            raise ArbitrationLedgerPackingError(f"packed assessment[{row_index}] section refs are invalid")
        section_ids: list[str] = []
        section_hashes: dict[str, str] = {}
        seen_positions: set[int] = set()
        candidate_sections = expanded_index[candidate_id]
        for section_ref in section_refs:
            section_position = _index(section_ref, len(candidate_sections), f"assessment[{row_index}] section ref")
            if section_position in seen_positions:
                raise ArbitrationLedgerPackingError(f"packed assessment[{row_index}] repeats a section ref")
            seen_positions.add(section_position)
            section = candidate_sections[section_position]
            section_id = str(section["section_id"])
            section_ids.append(section_id)
            section_hashes[section_id] = str(section["section_hash"])
        row["candidate_id"] = candidate_id
        row["candidate_hash"] = str(candidate_hashes[candidate_id])
        row["leaf_id"] = leaf_id
        row["reviewed_section_ids"] = section_ids
        row["section_hashes"] = section_hashes
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment"):
            row[field] = text_at(row.pop(f"{field}_text_ref", None), f"assessment[{row_index}].{field}")
        for field in ("advantages", "limitations"):
            refs = row.pop(f"{field}_text_refs", None)
            if not isinstance(refs, list):
                raise ArbitrationLedgerPackingError(f"packed assessment[{row_index}].{field} refs are invalid")
            row[field] = [text_at(ref, f"assessment[{row_index}].{field}") for ref in refs]
        for field in ("evidence_refs", "critic_issue_ids"):
            row[field] = array_at(row.pop(f"{field}_array_ref", None), f"assessment[{row_index}].{field}")
        decoded_assessments.append(row)
    expanded["assessments"] = decoded_assessments
    coverage = expanded.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ArbitrationLedgerPackingError("packed ledger coverage object is invalid")
    expanded_coverage = copy.deepcopy(dict(coverage))
    expanded_coverage["leaf_result_hashes"] = {
        str(row["leaf_id"]): str(row["leaf_result_hash"])
        for row in leaf_receipts
    }
    expanded["coverage"] = expanded_coverage

    raw_issues = expanded.get("critique_issues")
    if not isinstance(raw_issues, list):
        raise ArbitrationLedgerPackingError("packed critique issues must be an array")
    decoded_issues: list[dict[str, Any]] = []
    for issue_index, raw_issue in enumerate(raw_issues):
        if not isinstance(raw_issue, Mapping):
            raise ArbitrationLedgerPackingError(f"packed critique issue[{issue_index}] must be an object")
        issue = copy.deepcopy(dict(raw_issue))
        if "message_text_ref" in issue:
            issue["message"] = text_at(issue.pop("message_text_ref"), f"critique issue[{issue_index}].message")
        for field in ("target_ids", "evidence_refs"):
            ref_field = f"{field}_array_ref"
            if ref_field in issue:
                issue[field] = array_at(issue.pop(ref_field), f"critique issue[{issue_index}].{field}")
        decoded_issues.append(issue)
    expanded["critique_issues"] = decoded_issues

    raw_partition = expanded.get("claim_support_partition")
    if isinstance(raw_partition, Mapping) and raw_partition.get("schema_version") == _CLAIM_PARTITION_WIRE_SCHEMA:
        expanded["claim_support_partition"] = _expand_claim_support_partition(
            raw_partition, candidate_ids, leaf_receipts
        )

    expanded.pop("ledger_text_catalog", None)
    expanded.pop("ledger_string_array_catalog", None)
    expanded.pop("ledger_packing", None)
    expanded.pop("wire_body_hash", None)
    expanded.pop("candidate_hash_values", None)
    expanded.pop("leaf_result_receipt_rows", None)
    _validate_canonical_ledger(expanded)
    return expanded


def _pack_claim_support_partition(
    partition: Mapping[str, Any], candidate_ids: Sequence[str]
) -> dict[str, Any]:
    """Pack the exact partition as candidate-aligned positional closure rows."""

    if not isinstance(partition, Mapping) or partition.get("schema_version") != _CLAIM_PARTITION_SCHEMA:
        raise ArbitrationLedgerPackingError("canonical claim-support partition schema is invalid")
    canonical_hash = compute_v3_hash(partition)
    strings: set[str] = set()
    arrays: set[tuple[str, ...]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(partition)
    strings.add(canonical_hash)
    strings.add(str(partition.get("coverage_hash") or ""))
    for parent in partition.get("parent_sections") or []:
        for slot in parent.get("slots") or []:
            arrays.add(tuple(_string_array(slot.get("claim_target_ids"), "claim-support target ids")))
    for issue in partition.get("typed_issue_coverage") or []:
        arrays.add(tuple(_string_array(issue.get("target_ids"), "typed issue target ids")))
    string_catalog = sorted(strings)
    string_ref = {value: index for index, value in enumerate(string_catalog)}
    array_catalog = [list(value) for value in sorted(arrays, key=lambda value: _canonical(list(value)))]
    array_ref = {_canonical(value): index for index, value in enumerate(array_catalog)}

    parent_rows = list(partition.get("parent_sections") or [])
    parent_index = {
        (str(row["candidate_id"]), str(row["section_id"])): index
        for index, row in enumerate(parent_rows)
    }
    parent_group_ids: list[str] = []
    parent_groups: dict[str, dict[str, int]] = {}
    for parent_row_index, row in enumerate(parent_rows):
        parent_id = str(row["comparison_parent_unit_id"])
        if parent_id not in parent_groups:
            parent_group_ids.append(parent_id)
            parent_groups[parent_id] = {}
        candidate_id = str(row["candidate_id"])
        if candidate_id in parent_groups[parent_id]:
            raise ArbitrationLedgerPackingError("claim-support parent group repeats a candidate")
        parent_groups[parent_id][candidate_id] = parent_row_index
    group_ref = {value: index for index, value in enumerate(parent_group_ids)}
    comparison_parent_groups = [
        [
            string_ref[parent_id],
            [parent_groups[parent_id].get(candidate_id, -1) for candidate_id in candidate_ids],
        ]
        for parent_id in parent_group_ids
    ]

    packed_parents: list[list[Any]] = []
    for row in parent_rows:
        slots: list[list[Any]] = []
        for slot in row["slots"]:
            role = slot.get("comparison_role")
            slots.append([
                string_ref[str(slot["claim_slot_id"])],
                int(slot["claim_occurrence_index"]),
                int(slot["support_row_index"]),
                string_ref[str(slot["claim_hash"])],
                string_ref[str(slot["support_row_hash"])],
                string_ref[str(slot["comparison_scope_id"])],
                string_ref[str(slot["comparison_group_id"])],
                string_ref[str(slot["task_id"])],
                string_ref[str(slot["primary_claim_id"])],
                -1 if role is None else string_ref[str(role)],
                array_ref[_canonical(_string_array(slot.get("claim_target_ids"), "claim-support target ids"))],
            ])
        packed_parents.append([
            group_ref[str(row["comparison_parent_unit_id"])],
            string_ref[str(row["candidate_id"])],
            string_ref[str(row["section_id"])],
            string_ref[str(row["section_hash"])],
            string_ref[str(row["source_closure_hash"])],
            int(row["claim_count"]),
            int(row["support_row_count"]),
            slots,
        ])

    unit_rows: list[list[Any]] = []
    for assignment in partition.get("unit_assignments") or []:
        parent_id = str(assignment["comparison_parent_unit_id"])
        candidate_bindings = assignment["bindings_by_candidate"]
        base_binding = candidate_bindings[candidate_ids[0]][0]
        base_parent_index = parent_index[(candidate_ids[0], str(base_binding["section_id"]))]
        base_parent = parent_rows[base_parent_index]
        base_slot_positions = {
            str(slot["claim_slot_id"]): index for index, slot in enumerate(base_parent["slots"])
        }
        selected_slots = [str(slot_id) for slot_id in base_binding["claim_slot_ids"]]
        slot_positions = [base_slot_positions[slot_id] for slot_id in selected_slots]
        for candidate_id in candidate_ids:
            binding = candidate_bindings[candidate_id][0]
            candidate_parent_index = parent_index[(candidate_id, str(binding["section_id"]))]
            candidate_parent = parent_rows[candidate_parent_index]
            candidate_positions = {
                str(slot["claim_slot_id"]): index for index, slot in enumerate(candidate_parent["slots"])
            }
            if [candidate_positions.get(slot_id) for slot_id in selected_slots] != slot_positions:
                raise ArbitrationLedgerPackingError("claim-support positional slots differ across alternatives")
        unit_rows.append([
            string_ref[str(assignment["unit_id"])],
            string_ref[str(assignment["part_hash"])],
            group_ref[parent_id],
            slot_positions,
        ])

    packed_leaf_coverage: list[list[Any]] = []
    for row in partition.get("leaf_coverage") or []:
        candidate_id = str(row["candidate_id"])
        section_id = str(row["section_id"])
        parent_row_index = parent_index[(candidate_id, section_id)]
        slot_positions = {
            str(slot["claim_slot_id"]): index
            for index, slot in enumerate(parent_rows[parent_row_index]["slots"])
        }
        packed_leaf_coverage.append([
            string_ref[str(row["leaf_id"])],
            string_ref[candidate_id],
            parent_row_index,
            [slot_positions[str(slot_id)] for slot_id in row["claim_slot_ids"]],
        ])

    packed_issues: list[list[Any]] = []
    for issue in partition.get("typed_issue_coverage") or []:
        candidate_id = issue.get("candidate_id")
        target_ids = _string_array(issue.get("target_ids"), "typed issue target ids")
        slot_targets = []
        for ref in issue.get("claim_slot_targets") or []:
            candidate_ref = str(ref["candidate_id"])
            parent_row_index = parent_index[(candidate_ref, str(ref["section_id"]))]
            slot_positions = {
                str(slot["claim_slot_id"]): index
                for index, slot in enumerate(parent_rows[parent_row_index]["slots"])
            }
            slot_targets.append([
                string_ref[str(ref["leaf_id"])],
                string_ref[candidate_ref],
                parent_row_index,
                [slot_positions[str(slot_id)] for slot_id in ref["claim_slot_ids"]],
            ])
        packed_issues.append([
            string_ref[str(issue["issue_id"])],
            string_ref[str(issue["scope"])],
            -1 if candidate_id is None else string_ref[str(candidate_id)],
            array_ref[_canonical(target_ids)],
            [string_ref[str(leaf_id)] for leaf_id in issue["leaf_ids"]],
            slot_targets,
        ])

    return {
        "schema_version": _CLAIM_PARTITION_WIRE_SCHEMA,
        "canonical_partition_hash_ref": string_ref[canonical_hash],
        "coverage_hash_ref": string_ref[str(partition["coverage_hash"])],
        "string_catalog": string_catalog,
        "string_array_catalog": [[string_ref[value] for value in row] for row in array_catalog],
        "comparison_parent_group_columns": ["comparison_parent_unit_id_ref", "parent_section_row_refs_by_candidate"],
        "parent_section_columns": list(_PARTITION_PARENT_COLUMNS),
        "parent_slot_columns": list(_PARTITION_SLOT_COLUMNS),
        "unit_assignment_columns": list(_PARTITION_UNIT_COLUMNS),
        "leaf_coverage_columns": list(_PARTITION_LEAF_COVERAGE_COLUMNS),
        "issue_coverage_columns": list(_PARTITION_ISSUE_COLUMNS),
        "issue_slot_target_columns": list(_PARTITION_ISSUE_SLOT_TARGET_COLUMNS),
        "comparison_parent_groups": comparison_parent_groups,
        "parent_sections": packed_parents,
        "unit_assignments": unit_rows,
        "leaf_coverage": packed_leaf_coverage,
        "typed_issue_coverage": packed_issues,
        "leaf_part_receipts_projection": _PARTITION_RECEIPT_PROJECTION,
    }


def _expand_claim_support_partition(
    wire: Mapping[str, Any],
    candidate_ids: Sequence[str],
    leaf_result_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Restore and verify the exact canonical claim-support partition."""

    expected_fields = {
        "schema_version", "canonical_partition_hash_ref", "coverage_hash_ref", "string_catalog",
        "string_array_catalog", "comparison_parent_group_columns", "parent_section_columns",
        "parent_slot_columns", "unit_assignment_columns", "leaf_coverage_columns",
        "issue_coverage_columns", "issue_slot_target_columns", "comparison_parent_groups",
        "parent_sections", "unit_assignments", "leaf_coverage", "typed_issue_coverage",
        "leaf_part_receipts_projection",
    }
    if set(wire) != expected_fields or wire.get("schema_version") != _CLAIM_PARTITION_WIRE_SCHEMA:
        raise ArbitrationLedgerPackingError("packed claim-support partition schema is invalid")
    contracts = (
        ("comparison_parent_group_columns", ["comparison_parent_unit_id_ref", "parent_section_row_refs_by_candidate"]),
        ("parent_section_columns", list(_PARTITION_PARENT_COLUMNS)),
        ("parent_slot_columns", list(_PARTITION_SLOT_COLUMNS)),
        ("unit_assignment_columns", list(_PARTITION_UNIT_COLUMNS)),
        ("leaf_coverage_columns", list(_PARTITION_LEAF_COVERAGE_COLUMNS)),
        ("issue_coverage_columns", list(_PARTITION_ISSUE_COLUMNS)),
        ("issue_slot_target_columns", list(_PARTITION_ISSUE_SLOT_TARGET_COLUMNS)),
    )
    for field, expected in contracts:
        if wire.get(field) != expected:
            raise ArbitrationLedgerPackingError(f"packed claim-support {field} contract is invalid")
    if wire.get("leaf_part_receipts_projection") != _PARTITION_RECEIPT_PROJECTION:
        raise ArbitrationLedgerPackingError("packed claim-support receipt projection contract is missing")
    strings = wire.get("string_catalog")
    arrays = wire.get("string_array_catalog")
    if not isinstance(strings, list) or any(not isinstance(value, str) for value in strings):
        raise ArbitrationLedgerPackingError("packed claim-support string catalog is invalid")
    if strings != sorted(set(strings)):
        raise ArbitrationLedgerPackingError("packed claim-support string catalog is not canonical")
    if not isinstance(arrays, list):
        raise ArbitrationLedgerPackingError("packed claim-support array catalog is invalid")
    for array_index, array in enumerate(arrays):
        if not isinstance(array, list):
            raise ArbitrationLedgerPackingError(f"packed claim-support target array {array_index} is invalid")
        for value in array:
            _index(value, len(strings), f"claim-support target array {array_index} reference")
    array_keys = [tuple(strings[index] for index in row) for row in arrays]
    if len(array_keys) != len(set(array_keys)):
        raise ArbitrationLedgerPackingError("packed claim-support target array catalog contains duplicates")

    def text_at(reference: Any, label: str) -> str:
        return _catalog_at(strings, reference, label)

    def array_at(reference: Any, label: str) -> list[str]:
        array_ref = _index(reference, len(arrays), label)
        return [text_at(value, label) for value in arrays[array_ref]]

    raw_groups = wire.get("comparison_parent_groups")
    raw_parents = wire.get("parent_sections")
    raw_units = wire.get("unit_assignments")
    raw_leaf_coverage = wire.get("leaf_coverage")
    raw_issues = wire.get("typed_issue_coverage")
    if (
        not isinstance(raw_groups, list)
        or not isinstance(raw_parents, list)
        or not isinstance(raw_units, list)
        or not isinstance(raw_leaf_coverage, list)
        or not isinstance(raw_issues, list)
    ):
        raise ArbitrationLedgerPackingError("packed claim-support partition tables are invalid")

    parent_groups: list[tuple[str, list[int]]] = []
    group_by_parent_id: dict[str, int] = {}
    row_group_index: dict[int, int] = {}
    for group_index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, list) or len(raw_group) != 2 or not isinstance(raw_group[1], list):
            raise ArbitrationLedgerPackingError("packed claim-support parent group row is invalid")
        parent_id = text_at(raw_group[0], f"parent group[{group_index}].id")
        if parent_id in group_by_parent_id:
            raise ArbitrationLedgerPackingError("packed claim-support parent group is duplicated")
        row_refs = [
            _index(value, len(raw_parents), f"parent group[{group_index}] row reference")
            for value in raw_group[1]
        ]
        if len(row_refs) != len(candidate_ids) or len(set(row_refs)) != len(row_refs):
            raise ArbitrationLedgerPackingError("packed claim-support parent group omits or repeats a candidate")
        group_by_parent_id[parent_id] = group_index
        parent_groups.append((parent_id, row_refs))
        for row_ref in row_refs:
            if row_ref in row_group_index:
                raise ArbitrationLedgerPackingError("packed claim-support parent row belongs to multiple groups")
            row_group_index[row_ref] = group_index
    if set(row_group_index) != set(range(len(raw_parents))):
        raise ArbitrationLedgerPackingError("packed claim-support parent groups do not cover every parent row")

    parent_sections: list[dict[str, Any]] = []
    for row_index, raw_parent in enumerate(raw_parents):
        if not isinstance(raw_parent, list) or len(raw_parent) != len(_PARTITION_PARENT_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed claim-support parent row {row_index} has invalid width")
        group_index = _index(raw_parent[0], len(parent_groups), f"parent row[{row_index}] group ref")
        comparison_parent_id = parent_groups[group_index][0]
        candidate_id = text_at(raw_parent[1], f"parent row[{row_index}] candidate ref")
        if candidate_id not in candidate_ids:
            raise ArbitrationLedgerPackingError("packed claim-support parent has a foreign candidate")
        section_id = text_at(raw_parent[2], f"parent row[{row_index}] section ref")
        section_hash = text_at(raw_parent[3], f"parent row[{row_index}] section hash ref")
        source_hash = text_at(raw_parent[4], f"parent row[{row_index}] source hash ref")
        claim_count = raw_parent[5]
        support_count = raw_parent[6]
        if type(claim_count) is not int or type(support_count) is not int:
            raise ArbitrationLedgerPackingError("packed claim-support parent counts are invalid")
        raw_slots = raw_parent[7]
        if not isinstance(raw_slots, list):
            raise ArbitrationLedgerPackingError("packed claim-support slot table is invalid")
        slots: list[dict[str, Any]] = []
        for slot_index, raw_slot in enumerate(raw_slots):
            if not isinstance(raw_slot, list) or len(raw_slot) != len(_PARTITION_SLOT_COLUMNS):
                raise ArbitrationLedgerPackingError("packed claim-support slot row has invalid width")
            role = None if raw_slot[9] == -1 else text_at(raw_slot[9], "claim-support comparison role ref")
            slots.append({
                "claim_slot_id": text_at(raw_slot[0], "claim-support slot id ref"),
                "claim_occurrence_index": raw_slot[1],
                "support_row_index": raw_slot[2],
                "claim_hash": text_at(raw_slot[3], "claim-support claim hash ref"),
                "support_row_hash": text_at(raw_slot[4], "claim-support support hash ref"),
                "comparison_scope_id": text_at(raw_slot[5], "claim-support scope ref"),
                "comparison_group_id": text_at(raw_slot[6], "claim-support comparison group ref"),
                "task_id": text_at(raw_slot[7], "claim-support task id ref"),
                "primary_claim_id": text_at(raw_slot[8], "claim-support primary claim ref"),
                "comparison_role": role,
                "claim_target_ids": array_at(raw_slot[10], "claim-support target array ref"),
            })
        parent_sections.append({
            "comparison_parent_unit_id": comparison_parent_id,
            "candidate_id": candidate_id,
            "section_id": section_id,
            "section_hash": section_hash,
            "source_closure_hash": source_hash,
            "claim_count": claim_count,
            "support_row_count": support_count,
            "slots": slots,
        })

    for group_index, (parent_id, row_refs) in enumerate(parent_groups):
        rows = [parent_sections[row_ref] for row_ref in row_refs]
        if [row["candidate_id"] for row in rows] != list(candidate_ids):
            raise ArbitrationLedgerPackingError("packed claim-support parent group candidate order is invalid")
        if any(row["comparison_parent_unit_id"] != parent_id for row in rows):
            raise ArbitrationLedgerPackingError("packed claim-support parent group identity is stale")

    unit_assignments: list[dict[str, Any]] = []
    unit_by_id: dict[str, dict[str, Any]] = {}
    for unit_index, raw_unit in enumerate(raw_units):
        if not isinstance(raw_unit, list) or len(raw_unit) != len(_PARTITION_UNIT_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed claim-support unit[{unit_index}] has invalid width")
        unit_id = text_at(raw_unit[0], f"claim-support unit[{unit_index}] id ref")
        part_hash = text_at(raw_unit[1], f"claim-support unit[{unit_index}] hash ref")
        group_index = _index(raw_unit[2], len(parent_groups), f"claim-support unit[{unit_index}] parent ref")
        parent_id, parent_row_refs = parent_groups[group_index]
        if not isinstance(raw_unit[3], list):
            raise ArbitrationLedgerPackingError("packed claim-support unit slot positions are invalid")
        slot_positions = [
            _index(value, len(parent_sections[parent_row_refs[0]]["slots"]), "claim-support unit slot position")
            for value in raw_unit[3]
        ]
        if len(slot_positions) != len(set(slot_positions)):
            raise ArbitrationLedgerPackingError("packed claim-support unit repeats a slot position")
        bindings_by_candidate: dict[str, list[dict[str, Any]]] = {}
        expected_slot_ids: list[str] | None = None
        for candidate_id, parent_row_ref in zip(candidate_ids, parent_row_refs, strict=True):
            parent = parent_sections[parent_row_ref]
            if parent["candidate_id"] != candidate_id:
                raise ArbitrationLedgerPackingError("packed claim-support parent row is misaligned by candidate")
            if any(position >= len(parent["slots"]) for position in slot_positions):
                raise ArbitrationLedgerPackingError("packed claim-support slot position is outside its parent")
            slots = [parent["slots"][position] for position in slot_positions]
            slot_ids = [slot["claim_slot_id"] for slot in slots]
            if expected_slot_ids is None:
                expected_slot_ids = slot_ids
            elif slot_ids != expected_slot_ids:
                raise ArbitrationLedgerPackingError("packed claim-support slot positions differ across candidates")
            bindings_by_candidate[candidate_id] = [{
                "section_id": parent["section_id"],
                "section_hash": parent["section_hash"],
                "claim_slot_ids": slot_ids,
                "claim_hashes": {slot["claim_slot_id"]: slot["claim_hash"] for slot in slots},
                "support_row_hashes": {slot["claim_slot_id"]: slot["support_row_hash"] for slot in slots},
                "comparison_group_ids": {slot["claim_slot_id"]: slot["comparison_group_id"] for slot in slots},
                "claim_occurrence_indices": {
                    slot["claim_slot_id"]: slot["claim_occurrence_index"] for slot in slots
                },
                "support_row_indices": {slot["claim_slot_id"]: slot["support_row_index"] for slot in slots},
            }]
        assignment = {
            "unit_id": unit_id,
            "part_hash": part_hash,
            "comparison_parent_unit_id": parent_id,
            "bindings_by_candidate": bindings_by_candidate,
        }
        if unit_id in unit_by_id:
            raise ArbitrationLedgerPackingError("packed claim-support unit ID is duplicated")
        unit_by_id[unit_id] = assignment
        unit_assignments.append(assignment)

    leaf_coverage: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_leaf_coverage):
        if not isinstance(raw_row, list) or len(raw_row) != len(_PARTITION_LEAF_COVERAGE_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed claim-support leaf coverage[{row_index}] has invalid width")
        leaf_id = text_at(raw_row[0], "claim-support coverage leaf ref")
        candidate_id = text_at(raw_row[1], "claim-support coverage candidate ref")
        parent_row_ref = _index(raw_row[2], len(parent_sections), "claim-support coverage parent ref")
        parent = parent_sections[parent_row_ref]
        if parent["candidate_id"] != candidate_id:
            raise ArbitrationLedgerPackingError("packed claim-support leaf coverage candidate is foreign")
        if not isinstance(raw_row[3], list):
            raise ArbitrationLedgerPackingError("packed claim-support leaf coverage positions are invalid")
        positions = [
            _index(value, len(parent["slots"]), "claim-support leaf coverage slot position")
            for value in raw_row[3]
        ]
        if len(positions) != len(set(positions)):
            raise ArbitrationLedgerPackingError("packed claim-support leaf coverage repeats a slot")
        leaf_coverage.append({
            "leaf_id": leaf_id,
            "candidate_id": candidate_id,
            "section_id": parent["section_id"],
            "claim_slot_ids": [parent["slots"][position]["claim_slot_id"] for position in positions],
        })

    typed_issue_coverage: list[dict[str, Any]] = []
    for issue_index, raw_issue in enumerate(raw_issues):
        if not isinstance(raw_issue, list) or len(raw_issue) != len(_PARTITION_ISSUE_COLUMNS):
            raise ArbitrationLedgerPackingError(f"packed typed issue coverage[{issue_index}] has invalid width")
        candidate_ref = raw_issue[2]
        candidate_id = None if candidate_ref == -1 else text_at(candidate_ref, "typed issue candidate ref")
        target_ids = array_at(raw_issue[3], "typed issue target ids ref")
        leaf_ids = [text_at(value, "typed issue leaf id ref") for value in raw_issue[4]]
        slot_targets: list[dict[str, Any]] = []
        if not isinstance(raw_issue[5], list):
            raise ArbitrationLedgerPackingError("typed issue slot target rows are invalid")
        for target_index, raw_target in enumerate(raw_issue[5]):
            if not isinstance(raw_target, list) or len(raw_target) != len(_PARTITION_ISSUE_SLOT_TARGET_COLUMNS):
                raise ArbitrationLedgerPackingError("typed issue slot target row has invalid width")
            leaf_id = text_at(raw_target[0], "typed issue target leaf ref")
            target_candidate = text_at(raw_target[1], "typed issue target candidate ref")
            parent_row_ref = _index(raw_target[2], len(parent_sections), "typed issue target parent ref")
            parent = parent_sections[parent_row_ref]
            if parent["candidate_id"] != target_candidate or not isinstance(raw_target[3], list):
                raise ArbitrationLedgerPackingError("typed issue target parent/candidate binding is invalid")
            positions = [
                _index(value, len(parent["slots"]), "typed issue target slot position")
                for value in raw_target[3]
            ]
            if len(positions) != len(set(positions)):
                raise ArbitrationLedgerPackingError("typed issue target repeats a slot")
            slot_targets.append({
                "leaf_id": leaf_id,
                "candidate_id": target_candidate,
                "section_id": parent["section_id"],
                "claim_slot_ids": [parent["slots"][position]["claim_slot_id"] for position in positions],
            })
        typed_issue_coverage.append({
            "issue_id": text_at(raw_issue[0], "typed issue id ref"),
            "scope": text_at(raw_issue[1], "typed issue scope ref"),
            "candidate_id": candidate_id,
            "target_ids": target_ids,
            "leaf_ids": leaf_ids,
            "claim_slot_targets": slot_targets,
        })

    leaf_part_receipts: list[dict[str, Any]] = []
    for receipt in leaf_result_receipts:
        leaf_id = str(receipt.get("leaf_id") or "")
        expected_by_candidate: dict[str, list[dict[str, Any]]] = {candidate_id: [] for candidate_id in candidate_ids}
        for unit_id in _unique_nonempty_strings(receipt.get("unit_ids"), f"{leaf_id}.unit_ids"):
            assignment = unit_by_id.get(unit_id)
            if assignment is None:
                continue
            for candidate_id in candidate_ids:
                binding = assignment["bindings_by_candidate"][candidate_id][0]
                expected_by_candidate[candidate_id].append({
                    "part_id": unit_id,
                    "part_hash": assignment["part_hash"],
                    "parent_section_bindings": [{
                        "section_id": binding["section_id"],
                        "section_hash": binding["section_hash"],
                    }],
                })
        if any(expected_by_candidate.values()):
            for candidate_id in candidate_ids:
                leaf_part_receipts.append({
                    "leaf_id": leaf_id,
                    "candidate_id": candidate_id,
                    "part_receipts": expected_by_candidate[candidate_id],
                })

    result = {
        "schema_version": _CLAIM_PARTITION_SCHEMA,
        "parent_sections": parent_sections,
        "unit_assignments": unit_assignments,
        "leaf_coverage": leaf_coverage,
        "leaf_part_receipts": leaf_part_receipts,
        "typed_issue_coverage": typed_issue_coverage,
        "coverage_hash": text_at(wire.get("coverage_hash_ref"), "claim-support coverage hash ref"),
    }
    canonical_hash = text_at(wire.get("canonical_partition_hash_ref"), "claim-support canonical hash ref")
    if compute_v3_hash(result) != canonical_hash:
        raise ArbitrationLedgerPackingError("expanded claim-support partition hash does not match its canonical digest")
    return result


def _validate_canonical_ledger(ledger: Mapping[str, Any]) -> None:
    if ledger.get("schema_version") != _LEDGER_SCHEMA:
        raise ArbitrationLedgerPackingError("unsupported canonical arbitration ledger schema")
    stored_hash = ledger.get("ledger_hash")
    body = {key: value for key, value in ledger.items() if key != "ledger_hash"}
    if not isinstance(stored_hash, str) or stored_hash != compute_v3_hash(body):
        raise ArbitrationLedgerPackingError("canonical arbitration ledger hash is invalid")
    candidate_ids = _unique_nonempty_strings(ledger.get("candidate_ids"), "candidate_ids")
    candidate_hashes = ledger.get("candidate_hashes")
    section_index = ledger.get("candidate_section_index")
    receipts = ledger.get("leaf_result_receipts")
    assessments = ledger.get("assessments")
    if not isinstance(candidate_hashes, Mapping) or set(candidate_hashes) != set(candidate_ids):
        raise ArbitrationLedgerPackingError("canonical candidate hashes must exactly match candidate IDs")
    if not isinstance(section_index, Mapping) or set(section_index) != set(candidate_ids):
        raise ArbitrationLedgerPackingError("canonical candidate section index must exactly match candidate IDs")
    if not isinstance(receipts, list) or not isinstance(assessments, list):
        raise ArbitrationLedgerPackingError("canonical ledger receipts and assessments must be arrays")
    section_hashes_by_candidate: dict[str, dict[str, str]] = {}
    for candidate_id in candidate_ids:
        if not isinstance(candidate_hashes[candidate_id], str) or not candidate_hashes[candidate_id]:
            raise ArbitrationLedgerPackingError(f"candidate hash for {candidate_id} is empty")
        rows = section_index[candidate_id]
        if not isinstance(rows, list):
            raise ArbitrationLedgerPackingError(f"candidate section index for {candidate_id} is invalid")
        local: dict[str, str] = {}
        for row_index, section in enumerate(rows):
            if not isinstance(section, Mapping):
                raise ArbitrationLedgerPackingError(f"section index {candidate_id}[{row_index}] is invalid")
            section_id = _nonempty_string(section.get("section_id"), f"section {candidate_id}.section_id")
            section_hash = _nonempty_string(section.get("section_hash"), f"section {candidate_id}.section_hash")
            if section.get("candidate_id") != candidate_id:
                raise ArbitrationLedgerPackingError(f"section {candidate_id}.{section_id} has a foreign candidate_id")
            if section_id in local:
                raise ArbitrationLedgerPackingError(f"candidate {candidate_id} repeats a section_id")
            local[section_id] = section_hash
        section_hashes_by_candidate[candidate_id] = local
    leaf_ids = [
        _nonempty_string(row.get("leaf_id"), "leaf receipt leaf_id")
        for row in receipts if isinstance(row, Mapping)
    ]
    if len(leaf_ids) != len(receipts) or len(leaf_ids) != len(set(leaf_ids)):
        raise ArbitrationLedgerPackingError("canonical leaf receipts contain duplicate or invalid IDs")
    expected_pairs = {
        (candidate_id, section_id)
        for candidate_id, section_hashes in section_hashes_by_candidate.items()
        for section_id in section_hashes
    }
    partitioned_pairs = _validate_canonical_claim_support_partition(
        ledger,
        candidate_ids,
        section_hashes_by_candidate,
        receipts,
        assessments,
    )
    observed_pairs: set[tuple[str, str]] = set()
    seen_candidate_leaf: set[tuple[str, str]] = set()
    for row_index, row in enumerate(assessments):
        if not isinstance(row, Mapping):
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] must be an object")
        candidate_id = _nonempty_string(row.get("candidate_id"), f"assessment[{row_index}].candidate_id")
        leaf_id = _nonempty_string(row.get("leaf_id"), f"assessment[{row_index}].leaf_id")
        if candidate_id not in candidate_ids or leaf_id not in leaf_ids:
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] candidate or leaf identity is foreign")
        if row.get("candidate_hash") != candidate_hashes[candidate_id]:
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] candidate hash is stale")
        candidate_leaf = (candidate_id, leaf_id)
        if candidate_leaf in seen_candidate_leaf:
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] duplicates a candidate/leaf row")
        seen_candidate_leaf.add(candidate_leaf)
        section_id_list = _string_array(row.get("reviewed_section_ids"), f"assessment[{row_index}].reviewed_section_ids")
        raw_hashes = row.get("section_hashes")
        if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != set(section_id_list):
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] section hash map differs from its section refs")
        for section_id in section_id_list:
            expected_hash = section_hashes_by_candidate[candidate_id].get(section_id)
            if expected_hash is None or raw_hashes.get(section_id) != expected_hash:
                raise ArbitrationLedgerPackingError(f"assessment[{row_index}] references a foreign or stale section")
            pair = (candidate_id, section_id)
            if pair in observed_pairs and pair not in partitioned_pairs:
                raise ArbitrationLedgerPackingError("canonical ledger repeats candidate/section coverage")
            observed_pairs.add(pair)
        presence = row.get("presence")
        if presence not in {"present", "absent"} or (presence == "present") != bool(section_id_list):
            raise ArbitrationLedgerPackingError(f"assessment[{row_index}] presence does not match section coverage")
        for field in ("coverage_assessment", "evidence_assessment", "structure_assessment"):
            _nonempty_string(row.get(field), f"assessment[{row_index}].{field}")
        for field in ("advantages", "limitations", "evidence_refs", "critic_issue_ids"):
            _string_array(row.get(field), f"assessment[{row_index}].{field}")
    expected_rows = {(candidate_id, leaf_id) for candidate_id in candidate_ids for leaf_id in leaf_ids}
    if seen_candidate_leaf != expected_rows:
        raise ArbitrationLedgerPackingError("canonical assessments do not cover every candidate for every leaf")
    if observed_pairs != expected_pairs:
        raise ArbitrationLedgerPackingError("canonical assessments do not cover every candidate section exactly once")
    coverage = ledger.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ArbitrationLedgerPackingError("canonical ledger coverage must be an object")
    expected_count = coverage.get("expected_candidate_section_count")
    observed_count = coverage.get("observed_candidate_section_count")
    if (
        type(expected_count) is not int
        or type(observed_count) is not int
        or expected_count != len(expected_pairs)
        or observed_count != len(observed_pairs)
    ):
        raise ArbitrationLedgerPackingError("canonical ledger aggregate section coverage counts are stale")
    if coverage.get("candidate_section_pairs_hash") != compute_v3_hash(sorted(expected_pairs)):
        raise ArbitrationLedgerPackingError("canonical ledger aggregate section coverage hash is stale")
    expected_leaf_hashes = {
        str(row["leaf_id"]): _nonempty_string(row.get("leaf_result_hash"), "leaf receipt result hash")
        for row in receipts
    }
    raw_leaf_hashes = coverage.get("leaf_result_hashes")
    if not isinstance(raw_leaf_hashes, Mapping) or dict(raw_leaf_hashes) != expected_leaf_hashes:
        raise ArbitrationLedgerPackingError("canonical ledger leaf result coverage hashes are stale")
    if not isinstance(ledger.get("critique_issues"), list):
        raise ArbitrationLedgerPackingError("canonical critique issues must be an array")


def _validate_canonical_claim_support_partition(
    ledger: Mapping[str, Any],
    candidate_ids: Sequence[str],
    section_hashes_by_candidate: Mapping[str, Mapping[str, str]],
    receipts: Sequence[Mapping[str, Any]],
    assessments: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    partition = ledger.get("claim_support_partition")
    if partition is None:
        return set()
    if not isinstance(partition, Mapping) or partition.get("schema_version") != "outline_candidate_claim_support_partition/v1":
        raise ArbitrationLedgerPackingError("canonical claim-support partition schema is invalid")
    expected_fields = {
        "schema_version", "parent_sections", "unit_assignments", "leaf_coverage", "leaf_part_receipts",
        "typed_issue_coverage", "coverage_hash",
    }
    if set(partition) != expected_fields:
        raise ArbitrationLedgerPackingError("canonical claim-support partition fields are incomplete or unknown")
    parent_rows = partition.get("parent_sections")
    unit_rows = partition.get("unit_assignments")
    leaf_rows = partition.get("leaf_coverage")
    part_receipt_rows = partition.get("leaf_part_receipts")
    issue_rows = partition.get("typed_issue_coverage")
    if (
        not isinstance(parent_rows, list)
        or not isinstance(unit_rows, list)
        or not isinstance(leaf_rows, list)
        or not isinstance(part_receipt_rows, list)
        or not isinstance(issue_rows, list)
    ):
        raise ArbitrationLedgerPackingError("canonical claim-support partition arrays are invalid")

    parents: dict[tuple[str, str], Mapping[str, Any]] = {}
    parents_by_group: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row_index, parent in enumerate(parent_rows):
        if not isinstance(parent, Mapping):
            raise ArbitrationLedgerPackingError(f"claim-support parent[{row_index}] is invalid")
        candidate_id = _nonempty_string(parent.get("candidate_id"), "claim-support parent candidate_id")
        section_id = _nonempty_string(parent.get("section_id"), "claim-support parent section_id")
        comparison_parent = _nonempty_string(
            parent.get("comparison_parent_unit_id"), "claim-support comparison parent unit_id"
        )
        pair = (candidate_id, section_id)
        if candidate_id not in candidate_ids or pair in parents:
            raise ArbitrationLedgerPackingError("claim-support parent is foreign or duplicated")
        expected_hash = section_hashes_by_candidate.get(candidate_id, {}).get(section_id)
        if expected_hash is None or parent.get("section_hash") != expected_hash:
            raise ArbitrationLedgerPackingError("claim-support parent section hash is stale")
        index_row = next(
            (row for row in ledger["candidate_section_index"][candidate_id]
             if str(row.get("section_id") or "") == section_id),
            None,
        )
        if (
            index_row is None
            or parent.get("source_closure_hash") != index_row.get("source_closure_hash")
            or parent.get("claim_count") != index_row.get("claim_count")
            or parent.get("support_row_count") != index_row.get("support_row_count")
        ):
            raise ArbitrationLedgerPackingError("claim-support source closure binding is stale")
        claim_count = index_row.get("claim_count")
        support_row_count = index_row.get("support_row_count")
        if (
            type(claim_count) is not int
            or claim_count < 1
            or type(support_row_count) is not int
            or support_row_count < 1
        ):
            raise ArbitrationLedgerPackingError("claim-support parent occurrence/support counts are invalid")
        slots = parent.get("slots")
        if not isinstance(slots, list) or len(slots) < 2 or len(slots) != support_row_count:
            raise ArbitrationLedgerPackingError("claim-support parent slots are incomplete")
        seen_slots: set[str] = set()
        seen_support_rows: set[int] = set()
        seen_occurrences: set[int] = set()
        roles_by_group: dict[str, list[str | None]] = {}
        normalized_group_rows: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(slots):
            if not isinstance(slot, Mapping):
                raise ArbitrationLedgerPackingError("claim-support slot record is invalid")
            slot_id = _nonempty_string(slot.get("claim_slot_id"), "claim-support slot id")
            if slot_id in seen_slots:
                raise ArbitrationLedgerPackingError("claim-support parent repeats a slot")
            seen_slots.add(slot_id)
            occurrence_index = slot.get("claim_occurrence_index")
            if type(occurrence_index) is not int or not 0 <= occurrence_index < claim_count:
                raise ArbitrationLedgerPackingError("claim-support occurrence index is invalid")
            seen_occurrences.add(occurrence_index)
            support_row_index = slot.get("support_row_index")
            if (
                type(support_row_index) is not int
                or support_row_index != slot_index
                or not 0 <= support_row_index < support_row_count
                or support_row_index in seen_support_rows
            ):
                raise ArbitrationLedgerPackingError("claim-support support-row index is invalid or aliased")
            seen_support_rows.add(support_row_index)
            task_id = _nonempty_string(slot.get("task_id"), "claim-support task_id")
            primary_claim_id = _nonempty_string(slot.get("primary_claim_id"), "claim-support primary_claim_id")
            expected_scope_id = compute_v3_hash({"task_id": task_id, "primary_claim_id": primary_claim_id})
            if slot.get("comparison_scope_id") != expected_scope_id:
                raise ArbitrationLedgerPackingError("claim-support task/primary scope binding is stale")
            for field in ("claim_hash", "support_row_hash", "comparison_group_id"):
                _nonempty_string(slot.get(field), f"claim-support {field}")
            role = slot.get("comparison_role")
            if role is not None and role not in {"A", "B"}:
                raise ArbitrationLedgerPackingError("claim-support comparison role is unsupported")
            roles_by_group.setdefault(str(slot["comparison_group_id"]), []).append(role)
            _string_array(slot.get("claim_target_ids"), "claim-support claim target ids")
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
            raise ArbitrationLedgerPackingError("claim-support support-row coverage is not exact")
        if seen_occurrences != set(range(claim_count)):
            raise ArbitrationLedgerPackingError("claim-support authored claim-occurrence coverage is not exact")
        for roles in roles_by_group.values():
            if any(role is not None for role in roles) and (
                any(role is None for role in roles) or set(roles) != {"A", "B"}
            ):
                raise ArbitrationLedgerPackingError("claim-support explicit A/B comparison group is incomplete")
        derived_groups = _derive_claim_support_comparison_groups(normalized_group_rows)
        derived_group_by_slot = {
            slot_id: group_id
            for group_id, group_slots in derived_groups
            for slot_id in group_slots
        }
        if any(
            row["comparison_group_id"] != derived_group_by_slot[row["claim_slot_id"]]
            for row in normalized_group_rows
        ):
            raise ArbitrationLedgerPackingError("claim-support comparison groups do not bind authored occurrences")
        parents[pair] = parent
        by_candidate = parents_by_group.setdefault(comparison_parent, {})
        if candidate_id in by_candidate:
            raise ArbitrationLedgerPackingError("claim-support comparison parent repeats a candidate")
        by_candidate[candidate_id] = parent

    for comparison_parent, by_candidate in parents_by_group.items():
        if set(by_candidate) != set(candidate_ids):
            raise ArbitrationLedgerPackingError("claim-support comparison parent omits a candidate alternative")
        reference_slots = by_candidate[candidate_ids[0]]["slots"]
        signature = [
            (row["claim_slot_id"], row["comparison_group_id"], row.get("comparison_role"))
            for row in reference_slots
        ]
        for candidate_id in candidate_ids[1:]:
            observed = [
                (row["claim_slot_id"], row["comparison_group_id"], row.get("comparison_role"))
                for row in by_candidate[candidate_id]["slots"]
            ]
            if observed != signature:
                raise ArbitrationLedgerPackingError("claim-support slots are not aligned across alternatives")

    assignments: dict[str, Mapping[str, Any]] = {}
    for row in unit_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationLedgerPackingError("claim-support unit assignment is invalid")
        unit_id = _nonempty_string(row.get("unit_id"), "claim-support unit id")
        if unit_id in assignments:
            raise ArbitrationLedgerPackingError("claim-support unit assignment is duplicated")
        if not isinstance(row.get("bindings_by_candidate"), Mapping):
            raise ArbitrationLedgerPackingError("claim-support unit candidate bindings are invalid")
        assignments[unit_id] = row

    receipt_by_id = {str(row.get("leaf_id") or ""): row for row in receipts}
    if len(receipt_by_id) != len(receipts):
        raise ArbitrationLedgerPackingError("claim-support leaf receipt identities are invalid")
    unit_occurrences: dict[str, str] = {}
    for leaf_id, receipt in receipt_by_id.items():
        unit_ids = _unique_nonempty_strings(receipt.get("unit_ids"), f"{leaf_id}.unit_ids")
        for unit_id in unit_ids:
            if unit_id in unit_occurrences:
                raise ArbitrationLedgerPackingError("claim-support unit is assigned to multiple leaves")
            unit_occurrences[unit_id] = leaf_id
    if not set(assignments).issubset(unit_occurrences):
        raise ArbitrationLedgerPackingError("claim-support unit assignment has no leaf receipt")

    assigned_slots: dict[tuple[str, str], list[str]] = {pair: [] for pair in parents}
    for unit_id, assignment in assignments.items():
        parent_id = _nonempty_string(assignment.get("comparison_parent_unit_id"), "claim-support parent unit id")
        part_hash = _nonempty_string(assignment.get("part_hash"), "claim-support part hash")
        if unit_id != f"claim-support-part:{part_hash[:24]}":
            raise ArbitrationLedgerPackingError("claim-support part id does not match its part hash")
        bindings_by_candidate = assignment["bindings_by_candidate"]
        if set(bindings_by_candidate) != set(candidate_ids):
            raise ArbitrationLedgerPackingError("claim-support unit omits a candidate alternative")
        aligned_signatures: list[list[tuple[str, str, Any, Any]]] = []
        for candidate_id in candidate_ids:
            bindings = bindings_by_candidate[candidate_id]
            if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], Mapping):
                raise ArbitrationLedgerPackingError("claim-support unit must bind one parent section per candidate")
            binding = bindings[0]
            section_id = _nonempty_string(binding.get("section_id"), "claim-support binding section id")
            pair = (candidate_id, section_id)
            parent = parents.get(pair)
            if parent is None or parent.get("comparison_parent_unit_id") != parent_id:
                raise ArbitrationLedgerPackingError("claim-support unit binds a foreign parent")
            if binding.get("section_hash") != parent.get("section_hash"):
                raise ArbitrationLedgerPackingError("claim-support unit parent hash is stale")
            slots = _unique_nonempty_strings(binding.get("claim_slot_ids"), "claim-support unit slots")
            hashes = binding.get("claim_hashes")
            row_hashes = binding.get("support_row_hashes")
            group_ids = binding.get("comparison_group_ids")
            occurrence_indices = binding.get("claim_occurrence_indices")
            support_row_indices = binding.get("support_row_indices")
            if (
                not isinstance(hashes, Mapping)
                or set(hashes) != set(slots)
                or not isinstance(row_hashes, Mapping)
                or set(row_hashes) != set(slots)
                or not isinstance(group_ids, Mapping)
                or set(group_ids) != set(slots)
                or not isinstance(occurrence_indices, Mapping)
                or set(occurrence_indices) != set(slots)
                or not isinstance(support_row_indices, Mapping)
                or set(support_row_indices) != set(slots)
            ):
                raise ArbitrationLedgerPackingError("claim-support unit slot hashes are incomplete")
            full_slots = {str(row["claim_slot_id"]): row for row in parent["slots"]}
            if not set(slots).issubset(full_slots):
                raise ArbitrationLedgerPackingError("claim-support unit contains a foreign slot")
            for slot_id in slots:
                slot = full_slots[slot_id]
                if (
                    hashes[slot_id] != slot["claim_hash"]
                    or row_hashes[slot_id] != slot["support_row_hash"]
                    or group_ids[slot_id] != slot["comparison_group_id"]
                    or occurrence_indices[slot_id] != slot["claim_occurrence_index"]
                    or support_row_indices[slot_id] != slot["support_row_index"]
                ):
                    raise ArbitrationLedgerPackingError("claim-support unit slot/occurrence binding is stale")
            group_members: dict[str, set[str]] = {}
            for slot in parent["slots"]:
                group_members.setdefault(str(slot["comparison_group_id"]), set()).add(str(slot["claim_slot_id"]))
            selected = set(slots)
            if any(selected.intersection(members) and not members.issubset(selected)
                   for members in group_members.values()):
                raise ArbitrationLedgerPackingError("claim-support unit splits an atomic comparison group")
            aligned_signatures.append([
                (slot_id, str(group_ids[slot_id]), occurrence_indices[slot_id], support_row_indices[slot_id])
                for slot_id in slots
            ])
            assigned_slots[pair].extend(slots)
        if any(signature != aligned_signatures[0] for signature in aligned_signatures[1:]):
            raise ArbitrationLedgerPackingError("claim-support unit alternatives have different slot coverage")
        first_binding = bindings_by_candidate[candidate_ids[0]][0]
        ordered_groups = list(dict.fromkeys(
            str(first_binding["comparison_group_ids"][slot_id])
            for slot_id in first_binding["claim_slot_ids"]
        ))
        if compute_v3_hash({
            "comparison_parent_unit_id": parent_id,
            "groups": ordered_groups,
            "bindings_by_candidate": bindings_by_candidate,
        }) != part_hash:
            raise ArbitrationLedgerPackingError("claim-support part hash does not bind parent and slot closure")

    for pair, parent in parents.items():
        expected_slots = [str(row["claim_slot_id"]) for row in parent["slots"]]
        observed = assigned_slots[pair]
        if len(observed) != len(set(observed)) or set(observed) != set(expected_slots):
            raise ArbitrationLedgerPackingError("claim-support unit assignments are not disjoint and exhaustive")

    expected_leaf_coverage: dict[tuple[str, str, str], list[str]] = {}
    for leaf_id, receipt in receipt_by_id.items():
        for unit_id in _unique_nonempty_strings(receipt.get("unit_ids"), f"{leaf_id}.unit_ids"):
            assignment = assignments.get(unit_id)
            if assignment is None:
                continue
            for candidate_id in candidate_ids:
                binding = assignment["bindings_by_candidate"][candidate_id][0]
                key = (leaf_id, candidate_id, str(binding["section_id"]))
                slots = list(binding["claim_slot_ids"])
                prior = expected_leaf_coverage.setdefault(key, [])
                if set(prior).intersection(slots):
                    raise ArbitrationLedgerPackingError("claim-support leaf units duplicate slot coverage")
                prior.extend(slots)
    observed_leaf_coverage: dict[tuple[str, str, str], list[str]] = {}
    canonical_leaf_rows: list[dict[str, Any]] = []
    for row in leaf_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationLedgerPackingError("claim-support leaf coverage row is invalid")
        leaf_id = _nonempty_string(row.get("leaf_id"), "claim-support coverage leaf id")
        candidate_id = _nonempty_string(row.get("candidate_id"), "claim-support coverage candidate id")
        section_id = _nonempty_string(row.get("section_id"), "claim-support coverage section id")
        key = (leaf_id, candidate_id, section_id)
        if leaf_id not in receipt_by_id or candidate_id not in candidate_ids or key in observed_leaf_coverage:
            raise ArbitrationLedgerPackingError("claim-support coverage row is foreign or duplicated")
        slot_ids = _unique_nonempty_strings(row.get("claim_slot_ids"), "claim-support coverage slot ids")
        observed_leaf_coverage[key] = slot_ids
        canonical_leaf_rows.append({
            "leaf_id": leaf_id,
            "candidate_id": candidate_id,
            "section_id": section_id,
            "claim_slot_ids": slot_ids,
        })
    if observed_leaf_coverage != expected_leaf_coverage:
        raise ArbitrationLedgerPackingError("claim-support leaf coverage differs from receipt-bound unit assignments")

    expected_part_receipts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for leaf_id, receipt in receipt_by_id.items():
        for unit_id in _unique_nonempty_strings(receipt.get("unit_ids"), f"{leaf_id}.unit_ids"):
            assignment = assignments.get(unit_id)
            if assignment is None:
                continue
            for candidate_id in candidate_ids:
                bindings = assignment["bindings_by_candidate"][candidate_id]
                expected_part_receipts.setdefault((leaf_id, candidate_id), []).append({
                    "part_id": unit_id,
                    "part_hash": str(assignment["part_hash"]),
                    "parent_section_bindings": [
                        {"section_id": str(binding["section_id"]),
                         "section_hash": str(binding["section_hash"])}
                        for binding in bindings
                    ],
                })
    observed_part_receipts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in part_receipt_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationLedgerPackingError("claim-support part receipt row is invalid")
        leaf_id = _nonempty_string(row.get("leaf_id"), "claim-support part receipt leaf id")
        candidate_id = _nonempty_string(row.get("candidate_id"), "claim-support part receipt candidate id")
        key = (leaf_id, candidate_id)
        if leaf_id not in receipt_by_id or candidate_id not in candidate_ids or key in observed_part_receipts:
            raise ArbitrationLedgerPackingError("claim-support part receipt is foreign or duplicated")
        receipts_for_candidate = row.get("part_receipts")
        if not isinstance(receipts_for_candidate, list):
            raise ArbitrationLedgerPackingError("claim-support part receipt list is invalid")
        for part_receipt in receipts_for_candidate:
            if not isinstance(part_receipt, Mapping) or set(part_receipt) != {
                "part_id", "part_hash", "parent_section_bindings"
            }:
                raise ArbitrationLedgerPackingError("claim-support part echo has an invalid shape")
            _nonempty_string(part_receipt.get("part_id"), "claim-support echoed part id")
            _nonempty_string(part_receipt.get("part_hash"), "claim-support echoed part hash")
            parent_bindings = part_receipt.get("parent_section_bindings")
            if not isinstance(parent_bindings, list) or not parent_bindings:
                raise ArbitrationLedgerPackingError("claim-support part echo omits parent section bindings")
            for binding in parent_bindings:
                if not isinstance(binding, Mapping) or set(binding) != {"section_id", "section_hash"}:
                    raise ArbitrationLedgerPackingError("claim-support echoed parent binding is malformed")
        observed_part_receipts[key] = copy.deepcopy(receipts_for_candidate)
    if observed_part_receipts != expected_part_receipts:
        raise ArbitrationLedgerPackingError("claim-support part receipts differ from exact planned IDs/hashes")

    expected_by_parent: dict[tuple[str, str], list[str]] = {}
    for pair, parent in parents.items():
        expected_by_parent[pair] = [str(row["claim_slot_id"]) for row in parent["slots"]]
    observed_by_parent: dict[tuple[str, str], list[str]] = {}
    for (leaf_id, candidate_id, section_id), slot_ids in observed_leaf_coverage.items():
        pair = (candidate_id, section_id)
        if pair not in expected_by_parent or leaf_id not in receipt_by_id:
            raise ArbitrationLedgerPackingError("claim-support coverage references a foreign parent")
        if not set(slot_ids).issubset(set(expected_by_parent[pair])):
            raise ArbitrationLedgerPackingError("claim-support coverage contains a foreign slot")
        observed_by_parent.setdefault(pair, []).extend(slot_ids)
    for pair, expected_slots in expected_by_parent.items():
        observed = observed_by_parent.get(pair, [])
        if len(observed) != len(set(observed)) or set(observed) != set(expected_slots):
            raise ArbitrationLedgerPackingError("claim-support coverage is not disjoint and exhaustive")

    assessments_by_leaf_candidate: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in assessments:
        key = (str(row.get("leaf_id") or ""), str(row.get("candidate_id") or ""))
        assessments_by_leaf_candidate[key] = row
    for leaf_id, candidate_id, section_id in observed_leaf_coverage:
        assessment = assessments_by_leaf_candidate.get((leaf_id, candidate_id))
        if assessment is None or section_id not in set(assessment.get("reviewed_section_ids") or []):
            raise ArbitrationLedgerPackingError("claim-support leaf has no parent-bound assessment")
    for row in assessments:
        leaf_id = str(row.get("leaf_id") or "")
        candidate_id = str(row.get("candidate_id") or "")
        for section_id in row.get("reviewed_section_ids") or []:
            pair = (candidate_id, str(section_id))
            if pair in parents and (leaf_id, candidate_id, str(section_id)) not in observed_leaf_coverage:
                raise ArbitrationLedgerPackingError("partitioned assessment omits slot coverage")

    # The leaf receipt hash binds compact part IDs/hashes and original parent
    # section bindings together with all candidate assessment text. The full
    # claim-slot/support-row certificate stays in the hash-bound plan ledger.
    for leaf_id, receipt in receipt_by_id.items():
        leaf_part_receipts_for_hash = {
            candidate_id: observed_part_receipts[(leaf_id, candidate_id)]
            for candidate_id in candidate_ids
            if (leaf_id, candidate_id) in observed_part_receipts
        }
        if not leaf_part_receipts_for_hash:
            continue
        if set(leaf_part_receipts_for_hash) != set(candidate_ids):
            raise ArbitrationLedgerPackingError("claim-support leaf part echoes omit a candidate alternative")
        candidate_rows = []
        for candidate_id in candidate_ids:
            row = assessments_by_leaf_candidate.get((leaf_id, candidate_id))
            if row is None:
                raise ArbitrationLedgerPackingError("claim-support leaf lacks a candidate assessment")
            candidate_rows.append({key: value for key, value in row.items() if key != "leaf_id"})
        result_body = {
            "leaf_id": leaf_id,
            "unit_ids": list(receipt["unit_ids"]),
            "candidate_assessments": candidate_rows,
            "claim_support_part_receipts_by_candidate": leaf_part_receipts_for_hash,
        }
        if compute_v3_hash(result_body) != receipt.get("leaf_result_hash"):
            raise ArbitrationLedgerPackingError("claim-support leaf result hash does not bind its coverage echo")

    critique_issues = ledger.get("critique_issues")
    if not isinstance(critique_issues, list):
        raise ArbitrationLedgerPackingError("canonical critique issues must be an array")
    issue_by_id: dict[str, Mapping[str, Any]] = {}
    for issue in critique_issues:
        if not isinstance(issue, Mapping):
            raise ArbitrationLedgerPackingError("canonical critique issue row is invalid")
        issue_id = _nonempty_string(issue.get("issue_id"), "critique issue id")
        if issue_id in issue_by_id:
            raise ArbitrationLedgerPackingError("canonical critique issue IDs are duplicated")
        issue_by_id[issue_id] = issue
    typed_rows_by_id: dict[str, Mapping[str, Any]] = {}
    for row in issue_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationLedgerPackingError("typed issue coverage row is invalid")
        issue_id = _nonempty_string(row.get("issue_id"), "typed issue coverage issue id")
        if issue_id in typed_rows_by_id:
            raise ArbitrationLedgerPackingError("typed issue coverage duplicates an issue")
        typed_rows_by_id[issue_id] = row
    if set(typed_rows_by_id) != set(issue_by_id):
        raise ArbitrationLedgerPackingError("typed issue coverage omits or adds a critic issue")

    all_leaf_ids = set(receipt_by_id)
    for issue_id, issue in issue_by_id.items():
        coverage = typed_rows_by_id[issue_id]
        scope = _nonempty_string(issue.get("scope"), f"{issue_id}.scope")
        candidate_id = str(issue.get("candidate_id") or "") or None
        targets = _string_array(issue.get("target_ids") or [], f"{issue_id}.target_ids")
        if (
            coverage.get("scope") != scope
            or coverage.get("candidate_id") != candidate_id
            or coverage.get("target_ids") != targets
        ):
            raise ArbitrationLedgerPackingError("typed issue coverage differs from the original critic issue")
        raw_leaf_ids = _unique_nonempty_strings(coverage.get("leaf_ids"), f"{issue_id}.leaf_ids")
        if not set(raw_leaf_ids).issubset(all_leaf_ids):
            raise ArbitrationLedgerPackingError("typed issue coverage names a foreign leaf")
        expected_leaf_ids: set[str]
        if scope == "global":
            expected_leaf_ids = set(all_leaf_ids)
        else:
            expected_leaf_ids = {
                str(row.get("leaf_id") or "")
                for row in assessments
                if row.get("candidate_id") == candidate_id
                and issue_id in set(row.get("critic_issue_ids") or [])
            }
        if set(raw_leaf_ids) != expected_leaf_ids:
            raise ArbitrationLedgerPackingError("typed issue coverage differs from leaf issue references")

        expected_slot_targets: list[dict[str, Any]] = []
        if (
            scope in {"candidate", "section", "claim"}
            and candidate_id is not None
            and candidate_id in candidate_ids
        ):
            for (leaf_id, row_candidate_id, section_id), covered_slot_ids in observed_leaf_coverage.items():
                if row_candidate_id != candidate_id:
                    continue
                if scope == "section" and section_id not in targets:
                    continue
                target_slots = list(covered_slot_ids)
                if scope == "claim":
                    parent = parents[(candidate_id, section_id)]
                    target_ids = set(targets)
                    claims = {
                        str(slot["claim_slot_id"])
                        for slot in parent["slots"]
                        if target_ids.intersection(slot.get("claim_target_ids") or [])
                    }
                    target_slots = [slot_id for slot_id in covered_slot_ids if slot_id in claims]
                    if not target_slots:
                        continue
                expected_slot_targets.append({
                    "leaf_id": leaf_id,
                    "candidate_id": candidate_id,
                    "section_id": section_id,
                    "claim_slot_ids": target_slots,
                })
        raw_slot_targets = coverage.get("claim_slot_targets")
        if not isinstance(raw_slot_targets, list) or raw_slot_targets != expected_slot_targets:
            raise ArbitrationLedgerPackingError("typed issue claim-slot closure is incomplete or foreign")

        if scope in {"section", "claim"} and candidate_id is not None:
            for slot_ref in expected_slot_targets:
                row = assessments_by_leaf_candidate.get((slot_ref["leaf_id"], candidate_id))
                if row is None or issue_id not in set(row.get("critic_issue_ids") or []):
                    raise ArbitrationLedgerPackingError("typed section/claim issue does not reach every related part")

    coverage_hash = partition.get("coverage_hash")
    if coverage_hash != compute_v3_hash({
        "leaf_coverage": leaf_rows,
        "leaf_part_receipts": part_receipt_rows,
        "typed_issue_coverage": issue_rows,
    }):
        raise ArbitrationLedgerPackingError("claim-support coverage digest is stale")
    return set(parents)


def _derive_claim_support_comparison_groups(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[str]]]:
    explicit_roles_by_task: dict[str, set[str]] = {}
    for row in rows:
        role = row.get("comparison_role")
        if isinstance(role, str) and role in {"A", "B"}:
            explicit_roles_by_task.setdefault(str(row["task_id"]), set()).add(role)
    if any(roles != {"A", "B"} for roles in explicit_roles_by_task.values()):
        raise ArbitrationLedgerPackingError("explicit A/B comparison task is incomplete")

    parent = list(range(len(rows)))

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

    by_occurrence: dict[int, list[int]] = {}
    by_scope: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        by_occurrence.setdefault(int(row["claim_occurrence_index"]), []).append(index)
        by_scope.setdefault(str(row["comparison_scope_id"]), []).append(index)
    for positions in (*by_occurrence.values(), *by_scope.values()):
        for index in positions[1:]:
            union(positions[0], index)

    components: dict[int, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        components.setdefault(find(index), []).append(row)
    result: list[tuple[str, list[str]]] = []
    for component in components.values():
        slot_ids = [str(row["claim_slot_id"]) for row in component]
        occurrence_indices = sorted({int(row["claim_occurrence_index"]) for row in component})
        task_ids = sorted({str(row["task_id"]) for row in component})
        primary_claim_ids = sorted({str(row["primary_claim_id"]) for row in component})
        scope_ids = sorted({str(row["comparison_scope_id"]) for row in component})
        roles = [row.get("comparison_role") for row in component]
        if any(role is not None for role in roles) and (
            any(role is None for role in roles) or set(roles) != {"A", "B"}
        ):
            raise ArbitrationLedgerPackingError("claim-support explicit A/B comparison group is incomplete")
        group_id = compute_v3_hash({
            "claim_slot_ids": slot_ids,
            "claim_occurrence_indices": occurrence_indices,
            "task_ids": task_ids,
            "primary_claim_ids": primary_claim_ids,
            "comparison_scope_ids": scope_ids,
            "comparison_roles": roles,
        })
        result.append((group_id, slot_ids))
    return result


def _string_array(value: Any, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ArbitrationLedgerPackingError(f"{label} must be an array of strings")
    rows = list(value)
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ArbitrationLedgerPackingError(f"{label} contains an empty or non-string value")
    if len(rows) != len(set(rows)):
        raise ArbitrationLedgerPackingError(f"{label} contains duplicate values")
    return [str(item) for item in rows]


def _unique_nonempty_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ArbitrationLedgerPackingError(f"{label} must be an array")
    rows = _string_array(value, label)
    if not rows or len(rows) != len(set(rows)):
        raise ArbitrationLedgerPackingError(f"{label} must contain unique non-empty values")
    return rows


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationLedgerPackingError(f"{label} must be a non-empty string")
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArbitrationLedgerPackingError("arbitration ledger catalog value is not canonical JSON") from exc


def _catalog_at(catalog: Sequence[Any], reference: Any, label: str) -> Any:
    return catalog[_index(reference, len(catalog), label)]


def _index(reference: Any, length: int, label: str) -> int:
    if type(reference) is not int or reference < 0 or reference >= length:
        raise ArbitrationLedgerPackingError(f"{label} is outside its catalog")
    return reference


__all__ = [
    "ArbitrationLedgerPackingError",
    "expand_arbitration_final_ledger",
    "pack_arbitration_final_ledger",
]

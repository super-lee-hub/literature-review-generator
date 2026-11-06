"""Task-local, reversible aliases for final-arbitration identities.

Only explicitly typed identity/hash fields are encoded. Research prose and
unknown fields are preserved exactly. The catalog stays local; the provider
wire carries hashes binding the canonical request, alias map, and wire body.
This is a representation codec and grants no source or Registry authority.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from runtime.provider_runtime import hash_json

WIRE_KEY = "arbitration_identity_aliases"
WIRE_SCHEMA = "outline-arbitration-identity-alias-request/v1"
CATALOG_SCHEMA = "outline-arbitration-identity-alias-catalog/v1"
POLICY = "task-local-final-arbitration-identities/v1"
WIRE_CONTRACT = (
    "Aliases are exact task-local identity/hash values. Echo them verbatim only "
    "in typed identity/hash fields; never substitute them into prose or invent aliases."
)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ID_FIELDS = {
    "candidate_id": "candidate_id",
    "leaf_id": "leaf_id",
    "unit_id": "unit_id",
    "section_id": "section_id",
    "issue_id": "issue_id",
    "claim_id": "claim_id",
    "claim_slot_id": "claim_slot_id",
    "source_claim_id": "claim_id",
    "primary_claim_id": "claim_id",
    "recommendation_id": "recommendation_id",
    "source_field_id": "source_field_id",
    "paper_key": "paper_id",
    "paper_id": "paper_id",
    "relation_id": "relation_id",
    "evidence_id": "evidence_id",
    "result_id": "result_id",
    "artifact_id": "artifact_id",
    "task_id": "task_id",
    "target_section_id": "section_id",
    "source_section_id": "section_id",
}
_ID_ARRAY_FIELDS = {
    "candidate_ids": "candidate_id",
    "blocked_candidate_ids": "candidate_id",
    "eligible_candidate_ids": "candidate_id",
    "required_if_selected_candidate_sharded": "candidate_id",
    "leaf_ids": "leaf_id",
    "unit_ids": "unit_id",
    "section_ids": "section_id",
    "source_section_ids": "section_id",
    "issue_ids": "issue_id",
    "critic_issue_ids": "issue_id",
    "claim_ids": "claim_id",
    "source_claim_ids": "claim_id",
    "required_source_claim_ids": "claim_id",
    "qualifier_source_claim_ids": "claim_id",
    "target_claim_ids": "claim_id",
    "claim_slot_ids": "claim_slot_id",
    "recommendation_ids": "recommendation_id",
    "source_field_ids": "source_field_id",
    "paper_keys": "paper_id",
    "paper_ids": "paper_id",
    "relation_ids": "relation_id",
    "evidence_ids": "evidence_id",
    "evidence_refs": "evidence_id",
    "result_ids": "result_id",
    "artifact_ids": "artifact_id",
    "task_ids": "task_id",
    "target_section_ids": "section_id",
}
_HASH_FIELDS = frozenset(
    {
        "candidate_hash",
        "parent_candidate_hash",
        "section_hash",
        "source_closure_hash",
        "leaf_result_hash",
        "ledger_hash",
        "plan_hash",
        "input_hash",
        "candidate_section_pairs_hash",
        "claim_hash",
        "support_row_hash",
        "part_hash",
        "canonical_partition_hash",
        "coverage_hash",
        "content_hash",
        "wire_body_hash",
        "text_catalog_sha256",
        "string_array_catalog_sha256",
        "expected_map_sha256",
        "expected_sha256",
        "expected_candidate_section_pairs_sha256",
    }
)
_SECTION_COLUMNS_V2 = (
    "section_id", "section_hash", "source_closure_hash", "title_text_ref",
    "goal_text_ref", "task_ids_array_ref", "paper_keys_array_ref",
    "relation_ids_array_ref", "claim_count", "support_row_count", "position",
)
_SECTION_COLUMNS_V3 = tuple(
    "source_closure_hash_text_ref" if field == "source_closure_hash" else field
    for field in _SECTION_COLUMNS_V2
)
_ASSESSMENT_COLUMNS = (
    "candidate_ref", "leaf_ref", "presence", "reviewed_section_refs",
    "coverage_assessment_text_ref", "evidence_assessment_text_ref",
    "structure_assessment_text_ref", "advantages_text_refs",
    "limitations_text_refs", "evidence_refs_array_ref",
    "critic_issue_ids_array_ref",
)
_LEAF_COLUMNS = ("leaf_id", "unit_ids", "leaf_result_hash")
_CLAIM_PARTITION_SCHEMA = "outline_candidate_claim_support_partition_wire/v1"
_PARTITION_GROUP_COLUMNS = (
    "comparison_parent_unit_id_ref",
    "parent_section_row_refs_by_candidate",
)
_PARTITION_PARENT_COLUMNS = (
    "comparison_parent_unit_id_ref", "candidate_id_ref", "section_id_ref",
    "section_hash_ref", "source_closure_hash_ref", "claim_count",
    "support_row_count", "slots",
)
_PARTITION_SLOT_COLUMNS = (
    "claim_slot_id_ref", "claim_occurrence_index", "support_row_index",
    "claim_hash_ref", "support_row_hash_ref", "comparison_scope_id_ref",
    "comparison_group_id_ref", "task_id_ref", "primary_claim_id_ref",
    "comparison_role_ref_or_minus_one", "claim_target_ids_array_ref",
)
_PARTITION_UNIT_COLUMNS = (
    "unit_id_ref", "part_hash_ref", "comparison_parent_group_ref",
    "parent_slot_positions",
)
_PARTITION_LEAF_COLUMNS = (
    "leaf_id_ref", "candidate_id_ref", "parent_section_ref",
    "parent_slot_positions",
)
_PARTITION_ISSUE_COLUMNS = (
    "issue_id_ref", "scope_ref", "candidate_id_ref_or_minus_one",
    "target_ids_array_ref", "leaf_id_refs", "claim_slot_target_rows",
)
_PARTITION_ISSUE_SLOT_TARGET_COLUMNS = (
    "leaf_id_ref", "candidate_id_ref", "parent_section_ref",
    "parent_slot_positions",
)
_ARRAY_REF_KINDS = {
    "task_ids_array_ref": "task_id",
    "paper_keys_array_ref": "paper_id",
    "relation_ids_array_ref": "relation_id",
    "evidence_refs_array_ref": "evidence_id",
    "critic_issue_ids_array_ref": "issue_id",
}
_PARTITION_REF_KINDS = {
    "comparison_parent_unit_id_ref": "comparison_parent_unit_id",
    "candidate_id_ref": "candidate_id",
    "candidate_id_ref_or_minus_one": "candidate_id",
    "section_id_ref": "section_id",
    "section_hash_ref": "section_hash",
    "source_closure_hash_ref": "source_closure_hash",
    "claim_slot_id_ref": "claim_slot_id",
    "claim_hash_ref": "claim_hash",
    "support_row_hash_ref": "support_row_hash",
    "comparison_scope_id_ref": "comparison_scope_id",
    "comparison_group_id_ref": "comparison_group_id",
    "task_id_ref": "task_id",
    "primary_claim_id_ref": "claim_id",
    "unit_id_ref": "unit_id",
    "part_hash_ref": "part_hash",
    "leaf_id_ref": "leaf_id",
    "issue_id_ref": "issue_id",
}


class ArbitrationIdentityAliasError(ValueError):
    """The final-arbitration wire or its local alias catalog is invalid."""


def _reverse_aliases(entries: Sequence[Mapping[str, Any]]) -> dict[str, tuple[frozenset[str], str]]:
    grouped: dict[str, tuple[set[str], str]] = {}
    for entry in entries:
        token, kind, value = entry["alias"], entry["kind"], entry["canonical_value"]
        if token in grouped and grouped[token][1] != value:
            raise ArbitrationIdentityAliasError("alias token maps to different canonical values")
        grouped.setdefault(token, (set(), value))[0].add(kind)
    return {token: (frozenset(kinds), value) for token, (kinds, value) in grouped.items()}


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ArbitrationIdentityAliasError("JSON object keys must be strings")
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ArbitrationIdentityAliasError("identity aliases require finite JSON data")


def _all_strings(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, str):
        yield value


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _choose_prefix(request: Mapping[str, Any]) -> str:
    prefix = "$A"
    strings = tuple(_all_strings(request))
    while any(value.startswith(prefix) for value in strings):
        prefix += "_"
    return prefix


def _target_kind(scope: Any) -> str | None:
    return {
        "section": "section_id",
        "candidate": "candidate_id",
        "leaf": "leaf_id",
        "unit": "unit_id",
        "claim": "claim_id",
        "paper": "paper_id",
        "relation": "relation_id",
    }.get(str(scope or "").casefold())


class _Context:
    def __init__(
        self,
        mode: str,
        *,
        prefix: str = "",
        forward: Mapping[tuple[str, str], str] | None = None,
        reverse: Mapping[str, tuple[frozenset[str], str]] | None = None,
        encoded_pairs: set[tuple[str, str]] | None = None,
    ) -> None:
        self.mode = mode
        self.prefix = prefix
        self.forward = dict(forward or {})
        self.reverse = dict(reverse or {})
        self.encoded_pairs = set(encoded_pairs or ())
        self.values: set[tuple[str, str]] = set()
        self.allowed: dict[str, set[str]] = {}

    def map_value(self, value: Any, kind: str, *, hash_value: bool = False) -> Any:
        if not isinstance(value, str):
            raise ArbitrationIdentityAliasError(f"{kind} identity must be a string")
        if not value:
            return value
        if self.mode == "expand" and value.startswith(self.prefix):
            found = self.reverse.get(value)
            if found is None or kind not in found[0]:
                raise ArbitrationIdentityAliasError(f"unknown or wrong-kind {kind} alias")
            return found[1]
        if hash_value and not _is_hash(value):
            return value
        pair = (kind, value)
        if self.mode == "collect":
            self.values.add(pair)
            self.allowed.setdefault(kind, set()).add(value)
            return value
        if self.mode == "alias":
            return self.forward.get(pair, value)
        if self.mode == "expand":
            if pair in self.encoded_pairs:
                raise ArbitrationIdentityAliasError(f"{kind} value was not encoded")
            return value
        raise AssertionError(f"unknown alias mode: {self.mode}")

    def map_key(self, value: Any, kind: str) -> str:
        if not isinstance(value, str) or not value:
            raise ArbitrationIdentityAliasError(f"{kind} mapping key must be nonempty text")
        return self.map_value(value, kind)


def _map_array(value: Any, kind: str, context: _Context) -> list[Any]:
    if not isinstance(value, list):
        raise ArbitrationIdentityAliasError(f"{kind} identity field must be an array")
    return [context.map_value(item, kind) for item in value]




def _walk_row(
    row: Any, columns: Sequence[str], context: _Context, label: str
) -> list[Any]:
    if not isinstance(row, list) or len(row) != len(columns):
        raise ArbitrationIdentityAliasError(f"{label} row shape differs from its columns")
    projected = list(row)
    for index, field in enumerate(columns):
        if field in _ID_FIELDS:
            projected[index] = context.map_value(row[index], _ID_FIELDS[field])
        elif field in _HASH_FIELDS:
            projected[index] = context.map_value(row[index], field, hash_value=True)
        elif field in _ID_ARRAY_FIELDS:
            projected[index] = _map_array(row[index], _ID_ARRAY_FIELDS[field], context)
    return projected


def _walk_section_index(
    value: Any, columns: Sequence[str], context: _Context
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArbitrationIdentityAliasError("candidate_section_index must be a mapping")
    result = {}
    for candidate, rows in value.items():
        candidate_key = context.map_key(candidate, "candidate_id")
        if not isinstance(rows, list):
            raise ArbitrationIdentityAliasError("candidate section rows must be arrays")
        if rows and isinstance(rows[0], list):
            result[candidate_key] = [
                _walk_row(row, columns, context, "candidate section index")
                for row in rows
            ]
        elif all(isinstance(row, Mapping) for row in rows):
            result[candidate_key] = [_walk(row, context, None) for row in rows]
        elif not rows:
            result[candidate_key] = []
        else:
            raise ArbitrationIdentityAliasError("candidate section row shape is unsupported")
    return result


def _walk_leaf_receipts(
    value: Any, columns: Sequence[str], context: _Context
) -> list[Any]:
    if not isinstance(value, list):
        raise ArbitrationIdentityAliasError("leaf result receipts must be an array")
    if not value:
        return []
    if isinstance(value[0], list):
        return [
            _walk_row(row, columns, context, "leaf result receipt") for row in value
        ]
    if all(isinstance(row, Mapping) for row in value):
        return [_walk(row, context, None) for row in value]
    raise ArbitrationIdentityAliasError("leaf result receipt shape is unsupported")


def _walk_issue(issue: Any, context: _Context) -> Any:
    if not isinstance(issue, Mapping):
        return issue
    result = {}
    target_kind = _target_kind(issue.get("scope"))
    for key, value in issue.items():
        if key == "target_ids" and target_kind is not None:
            result[key] = _map_array(value, target_kind, context)
        else:
            result[key] = _walk(value, context, key)
    return result


def _walk(value: Any, context: _Context, field: str | None) -> Any:
    if field == "claim_support_partition":
        return _walk_claim_partition(value, context)
    if field == "candidate_section_hashes":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("candidate_section_hashes must be a mapping")
        result = {}
        for candidate, sections in value.items():
            candidate_key = context.map_key(candidate, "candidate_id")
            if not isinstance(sections, Mapping):
                raise ArbitrationIdentityAliasError("section hash map must be a mapping")
            result[candidate_key] = {
                context.map_key(section, "section_id"): context.map_value(
                    digest, "section_hash", hash_value=True
                )
                for section, digest in sections.items()
            }
        return result
    if field == "candidate_hashes":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("candidate_hashes must be a mapping")
        return {
            context.map_key(candidate, "candidate_id"): context.map_value(
                digest, "candidate_hash", hash_value=True
            )
            for candidate, digest in value.items()
        }
    if field == "critique_hashes":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("critique_hashes must be a mapping")
        return {
            key: context.map_value(digest, "critique_hash", hash_value=True)
            for key, digest in value.items()
        }
    if field == "leaf_result_hashes":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("leaf_result_hashes must be a mapping")
        return {
            context.map_key(leaf, "leaf_id"): context.map_value(
                digest, "leaf_result_hash", hash_value=True
            )
            for leaf, digest in value.items()
        }
    if field == "section_hashes":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("section_hashes must be a mapping")
        return {
            context.map_key(section, "section_id"): context.map_value(
                digest, "section_hash", hash_value=True
            )
            for section, digest in value.items()
        }
    if field == "candidate_contents":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("candidate_contents must be a mapping")
        return {
            context.map_key(candidate, "candidate_id"): _walk(item, context, None)
            for candidate, item in value.items()
        }
    if field == "candidate_shard_results":
        if not isinstance(value, Mapping):
            raise ArbitrationIdentityAliasError("candidate_shard_results must be a mapping")
        return {
            context.map_key(candidate, "candidate_id"): _walk(item, context, None)
            for candidate, item in value.items()
        }
    if field == "candidate_hash_values":
        if not isinstance(value, list):
            raise ArbitrationIdentityAliasError("candidate_hash_values must be an array")
        return [
            context.map_value(item, "candidate_hash", hash_value=True)
            for item in value
        ]
    if field in _ID_FIELDS:
        return context.map_value(value, _ID_FIELDS[field])
    if field in _ID_ARRAY_FIELDS:
        return _map_array(value, _ID_ARRAY_FIELDS[field], context)
    if field in _HASH_FIELDS:
        return context.map_value(value, field, hash_value=True)
    if field == "candidate_section_index":
        raise AssertionError("candidate_section_index requires packed ledger columns")
    if field == "leaf_result_receipt_rows":
        raise AssertionError("leaf receipts require packed ledger columns")
    if field in {"issues", "critique_issues"} and isinstance(value, list):
        return [_walk_issue(item, context) for item in value]
    if isinstance(value, Mapping):
        if field == "arbitration_evaluation_ledger":
            packing = value.get("ledger_packing")
            if not isinstance(packing, Mapping):
                raise ArbitrationIdentityAliasError("packed arbitration ledger is required")
            section_columns = packing.get("section_index_row_columns")
            if section_columns not in (list(_SECTION_COLUMNS_V2), list(_SECTION_COLUMNS_V3)):
                raise ArbitrationIdentityAliasError("packed section-index columns are unsupported")
            if packing.get("assessment_row_columns") != list(_ASSESSMENT_COLUMNS):
                raise ArbitrationIdentityAliasError("packed assessment columns are unsupported")
            if packing.get("leaf_receipt_row_columns") != list(_LEAF_COLUMNS):
                raise ArbitrationIdentityAliasError("packed leaf-receipt columns are unsupported")
            result = {}
            for key, item in value.items():
                if key == "candidate_section_index":
                    result[key] = _walk_section_index(item, section_columns, context)
                elif key == "leaf_result_receipt_rows":
                    result[key] = _walk_leaf_receipts(
                        item, packing["leaf_receipt_row_columns"], context
                    )
                elif key == "critique_issues" and isinstance(item, list):
                    result[key] = [_walk_issue(issue, context) for issue in item]
                else:
                    result[key] = _walk(item, context, key)
            return result
        return {key: _walk(item, context, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, context, None) for item in value]
    return value


def _catalog_locations(
    request: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[int], dict[int, list[str]]]:
    ledger = request.get("arbitration_evaluation_ledger")
    if not isinstance(ledger, Mapping) or not isinstance(ledger.get("ledger_packing"), Mapping):
        raise ArbitrationIdentityAliasError("packed evaluation ledger is required")
    packing = ledger["ledger_packing"]
    version = packing.get("schema_version")
    section_columns: list[str] | None = (
        list(_SECTION_COLUMNS_V2)
        if version == "outline_candidate_arbitration_ledger_wire/v2"
        else list(_SECTION_COLUMNS_V3)
        if version == "outline_candidate_arbitration_ledger_wire/v3"
        else None
    )
    if section_columns is None or packing.get("section_index_row_columns") != section_columns:
        raise ArbitrationIdentityAliasError("packed ledger schema or section columns are unsupported")
    if packing.get("assessment_row_columns") != list(_ASSESSMENT_COLUMNS):
        raise ArbitrationIdentityAliasError("packed assessment columns are unsupported")
    if packing.get("leaf_receipt_row_columns") != list(_LEAF_COLUMNS):
        raise ArbitrationIdentityAliasError("packed leaf-receipt columns are unsupported")
    arrays, texts = ledger.get("ledger_string_array_catalog"), ledger.get("ledger_text_catalog")
    if not isinstance(arrays, list) or not isinstance(texts, list):
        raise ArbitrationIdentityAliasError("packed ledger catalogs must be arrays")
    if any(not isinstance(row, list) or any(not isinstance(item, str) for item in row) for row in arrays):
        raise ArbitrationIdentityAliasError("packed string-array catalog is malformed")
    if any(not isinstance(item, str) for item in texts):
        raise ArbitrationIdentityAliasError("packed text catalog is malformed")

    array_kinds: dict[int, set[str]] = {}
    closure_texts: set[int] = set()
    other_texts: set[int] = set()

    def add_array(index: Any, kind: str) -> None:
        if type(index) is not int or index < 0 or index >= len(arrays):
            raise ArbitrationIdentityAliasError("packed array catalog reference is invalid")
        array_kinds.setdefault(index, set()).add(kind)

    def add_text(index: Any, *, closure: bool = False) -> None:
        if type(index) is not int or index < 0 or index >= len(texts):
            raise ArbitrationIdentityAliasError("packed text catalog reference is invalid")
        (closure_texts if closure else other_texts).add(index)

    candidate_index = ledger.get("candidate_section_index")
    if not isinstance(candidate_index, Mapping):
        raise ArbitrationIdentityAliasError("packed candidate section index is malformed")
    for rows in candidate_index.values():
        if not isinstance(rows, list):
            raise ArbitrationIdentityAliasError("packed candidate section rows are malformed")
        for row in rows:
            if not isinstance(row, list) or len(row) != len(section_columns):
                raise ArbitrationIdentityAliasError("packed candidate section row is malformed")
            for field, kind in _ARRAY_REF_KINDS.items():
                if field in section_columns:
                    add_array(row[section_columns.index(field)], kind)
            for field in section_columns:
                if field.endswith("_text_ref"):
                    add_text(
                        row[section_columns.index(field)],
                        closure=field == "source_closure_hash_text_ref",
                    )

    assessment_columns = packing["assessment_row_columns"]
    assessments = ledger.get("assessments")
    if not isinstance(assessments, list):
        raise ArbitrationIdentityAliasError("packed assessments are malformed")
    for row in assessments:
        if not isinstance(row, list) or len(row) != len(assessment_columns):
            raise ArbitrationIdentityAliasError("packed assessment row is malformed")
        for field, kind in _ARRAY_REF_KINDS.items():
            if field in assessment_columns:
                add_array(row[assessment_columns.index(field)], kind)
        for field in assessment_columns:
            if field.endswith("_text_ref"):
                add_text(row[assessment_columns.index(field)])
            elif field in {"advantages_text_refs", "limitations_text_refs"}:
                indexes = row[assessment_columns.index(field)]
                if not isinstance(indexes, list):
                    raise ArbitrationIdentityAliasError("packed assessment text refs are malformed")
                for index in indexes:
                    add_text(index)

    issues = ledger.get("critique_issues")
    if not isinstance(issues, list):
        raise ArbitrationIdentityAliasError("packed critique issues are malformed")
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise ArbitrationIdentityAliasError("packed critique issue is malformed")
        if "evidence_refs_array_ref" in issue:
            add_array(issue["evidence_refs_array_ref"], "evidence_id")
        if "target_ids_array_ref" in issue:
            target_kind = _target_kind(issue.get("scope"))
            if target_kind:
                add_array(issue["target_ids_array_ref"], target_kind)
        for field, index in issue.items():
            if field.endswith("_text_ref"):
                add_text(index)

    # Empty arrays carry no typed identity. The canonical packer may deduplicate
    # the same [] across otherwise unrelated fields; keep that shared row literal.
    # A shared catalog row has the same canonical values in every role.
    # Each value gets one token, with its complete checked role set retained.
    ambiguous: dict[int, list[str]] = {}
    locations = [
        {"index": index, "kind": kind}
        for index, kinds in sorted(array_kinds.items())
        if arrays[index]
        for kind in sorted(kinds)
    ]
    closure_locations = sorted(closure_texts - other_texts)
    return locations, closure_locations, ambiguous


def _claim_partition_locations(partition: Any) -> list[dict[str, Any]]:
    """Resolve only explicitly typed refs in the packed claim-support schema."""
    if partition is None:
        return []
    if not isinstance(partition, Mapping) or partition.get("schema_version") != _CLAIM_PARTITION_SCHEMA:
        raise ArbitrationIdentityAliasError("claim-support partition schema is unsupported")
    contracts = {
        "comparison_parent_group_columns": list(_PARTITION_GROUP_COLUMNS),
        "parent_section_columns": list(_PARTITION_PARENT_COLUMNS),
        "parent_slot_columns": list(_PARTITION_SLOT_COLUMNS),
        "unit_assignment_columns": list(_PARTITION_UNIT_COLUMNS),
        "leaf_coverage_columns": list(_PARTITION_LEAF_COLUMNS),
        "issue_coverage_columns": list(_PARTITION_ISSUE_COLUMNS),
        "issue_slot_target_columns": list(_PARTITION_ISSUE_SLOT_TARGET_COLUMNS),
    }
    for field, expected in contracts.items():
        if partition.get(field) != expected:
            raise ArbitrationIdentityAliasError(f"claim-support {field} contract is unsupported")
    strings = partition.get("string_catalog")
    arrays = partition.get("string_array_catalog")
    if not isinstance(strings, list) or any(not isinstance(value, str) for value in strings):
        raise ArbitrationIdentityAliasError("claim-support string catalog is malformed")
    if not isinstance(arrays, list) or any(not isinstance(row, list) for row in arrays):
        raise ArbitrationIdentityAliasError("claim-support array catalog is malformed")
    for row in arrays:
        if any(type(index) is not int or index < 0 or index >= len(strings) for index in row):
            raise ArbitrationIdentityAliasError("claim-support string-array ref is invalid")

    kinds: dict[int, set[str]] = {}
    literal_refs: set[int] = set()

    def add_ref(raw_index: Any, kind: str, *, optional: bool = False) -> None:
        if optional and raw_index == -1:
            return
        if type(raw_index) is not int or raw_index < 0 or raw_index >= len(strings):
            raise ArbitrationIdentityAliasError("claim-support string ref is invalid")
        if strings[raw_index]:
            kinds.setdefault(raw_index, set()).add(kind)

    def add_array(raw_index: Any, kind: str) -> None:
        if type(raw_index) is not int or raw_index < 0 or raw_index >= len(arrays):
            raise ArbitrationIdentityAliasError("claim-support array ref is invalid")
        for string_index in arrays[raw_index]:
            add_ref(string_index, kind)

    def scan_row(row: Any, columns: Sequence[str], label: str) -> None:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ArbitrationIdentityAliasError(f"claim-support {label} row is malformed")
        for field, kind in _PARTITION_REF_KINDS.items():
            if field in columns:
                add_ref(row[columns.index(field)], kind, optional=field.endswith("or_minus_one"))
        if "comparison_role_ref_or_minus_one" in columns:
            role_ref = row[columns.index("comparison_role_ref_or_minus_one")]
            if role_ref != -1:
                if type(role_ref) is not int or role_ref < 0 or role_ref >= len(strings):
                    raise ArbitrationIdentityAliasError("claim-support comparison role ref is invalid")
                literal_refs.add(role_ref)
        if "claim_target_ids_array_ref" in columns:
            add_array(row[columns.index("claim_target_ids_array_ref")], "claim_id")
        if "leaf_id_refs" in columns:
            leaf_refs = row[columns.index("leaf_id_refs")]
            if not isinstance(leaf_refs, list):
                raise ArbitrationIdentityAliasError("claim-support leaf refs are malformed")
            for leaf_ref in leaf_refs:
                add_ref(leaf_ref, "leaf_id")
        if "slots" in columns:
            slots = row[columns.index("slots")]
            if not isinstance(slots, list):
                raise ArbitrationIdentityAliasError("claim-support slots are malformed")
            for slot in slots:
                scan_row(slot, _PARTITION_SLOT_COLUMNS, "slot")
        if "claim_slot_target_rows" in columns:
            targets = row[columns.index("claim_slot_target_rows")]
            if not isinstance(targets, list):
                raise ArbitrationIdentityAliasError("claim-support issue slot targets are malformed")
            for target in targets:
                scan_row(target, _PARTITION_ISSUE_SLOT_TARGET_COLUMNS, "issue slot target")

    if not isinstance(partition.get("canonical_partition_hash_ref"), int) or isinstance(
        partition.get("canonical_partition_hash_ref"), bool
    ):
        raise ArbitrationIdentityAliasError("claim-support canonical partition hash ref is invalid")
    if not isinstance(partition.get("coverage_hash_ref"), int) or isinstance(
        partition.get("coverage_hash_ref"), bool
    ):
        raise ArbitrationIdentityAliasError("claim-support coverage hash ref is invalid")
    add_ref(partition["canonical_partition_hash_ref"], "canonical_partition_hash")
    add_ref(partition["coverage_hash_ref"], "coverage_hash")

    groups = partition.get("comparison_parent_groups")
    if not isinstance(groups, list):
        raise ArbitrationIdentityAliasError("claim-support comparison groups are malformed")
    for group in groups:
        scan_row(group, _PARTITION_GROUP_COLUMNS, "comparison parent group")

    for field, columns in (
        ("parent_sections", _PARTITION_PARENT_COLUMNS),
        ("unit_assignments", _PARTITION_UNIT_COLUMNS),
        ("leaf_coverage", _PARTITION_LEAF_COLUMNS),
    ):
        rows = partition.get(field)
        if not isinstance(rows, list):
            raise ArbitrationIdentityAliasError(f"claim-support {field} is malformed")
        for row in rows:
            scan_row(row, columns, field)

    issues = partition.get("typed_issue_coverage")
    if not isinstance(issues, list):
        raise ArbitrationIdentityAliasError("claim-support typed issues are malformed")
    for issue in issues:
        scan_row(issue, _PARTITION_ISSUE_COLUMNS, "typed issue")
        scope_index = issue[_PARTITION_ISSUE_COLUMNS.index("scope_ref")]
        if type(scope_index) is not int or scope_index < 0 or scope_index >= len(strings):
            raise ArbitrationIdentityAliasError("claim-support issue scope ref is invalid")
        literal_refs.add(scope_index)
        target_kind = _target_kind(strings[scope_index])
        target_index = issue[_PARTITION_ISSUE_COLUMNS.index("target_ids_array_ref")]
        if target_kind is not None:
            add_array(target_index, target_kind)
        else:
            if type(target_index) is not int or target_index < 0 or target_index >= len(arrays):
                raise ArbitrationIdentityAliasError("claim-support issue target array ref is invalid")
            literal_refs.update(arrays[target_index])

    for index, local_kinds in kinds.items():
        if index in literal_refs:
            raise ArbitrationIdentityAliasError(
                f"claim-support string catalog entry {index} is both typed and literal"
            )
    return [
        {"index": index, "kinds": sorted(local_kinds)}
        for index, local_kinds in sorted(kinds.items())
    ]


def _walk_claim_partition(value: Any, context: _Context) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArbitrationIdentityAliasError("claim-support partition must be a mapping")
    result = dict(value)
    string_catalog = value.get("string_catalog")
    if not isinstance(string_catalog, list):
        raise ArbitrationIdentityAliasError("claim-support string catalog must be an array")
    projected = list(string_catalog)
    for location in _claim_partition_locations(value):
        index = int(location["index"])
        values = [context.map_value(string_catalog[index], kind, hash_value=kind in _HASH_FIELDS)
                  for kind in location["kinds"]]
        if not values or any(item != values[0] for item in values):
            raise ArbitrationIdentityAliasError("shared claim-support identity has conflicting aliases")
        projected[index] = values[0]
    result["string_catalog"] = projected
    return result


def _transform_catalogs(
    request: dict[str, Any],
    *,
    array_locations: Sequence[Mapping[str, Any]],
    text_locations: Sequence[int],
    context: _Context,
) -> None:
    ledger = request["arbitration_evaluation_ledger"]
    arrays = ledger["ledger_string_array_catalog"]
    original_rows = {int(location["index"]): arrays[int(location["index"])] for location in array_locations}
    projected_rows: dict[int, list[Any]] = {}
    for location in array_locations:
        index, kind = int(location["index"]), str(location["kind"])
        projected = [context.map_value(item, kind) for item in original_rows[index]]
        if index in projected_rows and projected_rows[index] != projected:
            raise ArbitrationIdentityAliasError("shared array values have conflicting aliases")
        projected_rows[index] = projected
    for index, projected in projected_rows.items():
        arrays[index] = projected
    texts = ledger["ledger_text_catalog"]
    for index in text_locations:
        texts[index] = context.map_value(
            texts[index], "source_closure_hash", hash_value=True
        )


def _mapping_hash(
    prefix: str,
    entries: Sequence[Mapping[str, Any]],
    array_locations: Sequence[Mapping[str, Any]],
    text_locations: Sequence[int],
) -> str:
    return hash_json(
        {
            "policy": POLICY,
            "prefix": prefix,
            "entries": list(entries),
            "array_catalog_aliases": list(array_locations),
            "text_catalog_aliases": list(text_locations),
        }
    )


def _catalog_body(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in catalog.items() if key != "catalog_hash"}


def project_arbitration_identity_request(
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (local catalog, provider wire) for a packed final-arbitration request."""
    source = _json_copy(request)
    if not isinstance(source, dict) or WIRE_KEY in source:
        raise ArbitrationIdentityAliasError("request must be an unencoded JSON object")
    hierarchy = source.get("hierarchy")
    if not isinstance(hierarchy, Mapping) or hierarchy.get("level") != "all_candidate_final_arbitration":
        raise ArbitrationIdentityAliasError("request is not a final-arbitration request")
    _catalog_locations(source)

    collect = _Context("collect")
    _walk(source, collect, None)
    array_locations, text_locations, ambiguous = _catalog_locations(source)
    if ambiguous:
        raise ArbitrationIdentityAliasError(
            "a string-array catalog row is referenced by multiple identity types"
        )
    _transform_catalogs(
        source,
        array_locations=array_locations,
        text_locations=text_locations,
        context=collect,
    )
    prefix = _choose_prefix(request)
    ordered = sorted(collect.values)
    value_tokens = {value: prefix + str(index) for index, value in enumerate(sorted({value for _kind, value in ordered}))}
    forward = {pair: value_tokens[pair[1]] for pair in ordered}
    alias_context = _Context("alias", prefix=prefix, forward=forward)
    wire_body = _walk(source, alias_context, None)
    _transform_catalogs(
        wire_body,
        array_locations=array_locations,
        text_locations=text_locations,
        context=alias_context,
    )

    entries = [
        {"kind": kind, "canonical_value": value, "alias": forward[(kind, value)]}
        for kind, value in ordered
    ]
    allowed = {
        kind: sorted(values) for kind, values in sorted(collect.allowed.items())
    }
    canonical_hash = hash_json(source)
    wire_hash = hash_json(wire_body)
    mapping_hash = _mapping_hash(prefix, entries, array_locations, text_locations)
    catalog = {
        "schema_version": CATALOG_SCHEMA,
        "policy": POLICY,
        "wire_contract": WIRE_CONTRACT,
        "prefix": prefix,
        "entries": entries,
        "allowed_echo_values": allowed,
        "array_catalog_aliases": array_locations,
        "text_catalog_aliases": text_locations,
        "canonical_request_hash": canonical_hash,
        "wire_body_hash": wire_hash,
        "mapping_hash": mapping_hash,
    }
    catalog["catalog_hash"] = hash_json(catalog)
    wire = dict(wire_body)
    wire[WIRE_KEY] = {
        "schema_version": WIRE_SCHEMA,
        "policy": POLICY,
        "contract": WIRE_CONTRACT,
        "prefix": prefix,
        "canonical_request_hash": canonical_hash,
        "wire_body_hash": wire_hash,
        "mapping_hash": mapping_hash,
        "catalog_hash": catalog["catalog_hash"],
    }
    if hash_json(expand_arbitration_identity_request(wire, catalog)) != canonical_hash:
        raise ArbitrationIdentityAliasError("identity projection is not lossless")
    return catalog, wire


def _verify_catalog(raw: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _json_copy(raw)
    required = {
        "schema_version", "policy", "wire_contract", "prefix", "entries", "allowed_echo_values",
        "array_catalog_aliases", "text_catalog_aliases", "canonical_request_hash",
        "wire_body_hash", "mapping_hash", "catalog_hash",
    }
    if not isinstance(catalog, dict) or set(catalog) != required:
        raise ArbitrationIdentityAliasError("identity catalog schema is invalid")
    prefix = catalog["prefix"]
    if (
        catalog["schema_version"] != CATALOG_SCHEMA
        or catalog["policy"] != POLICY
        or catalog["wire_contract"] != WIRE_CONTRACT
    ):
        raise ArbitrationIdentityAliasError("identity catalog policy is invalid")
    if not isinstance(prefix, str) or re.fullmatch(r"\$A_*", prefix) is None:
        raise ArbitrationIdentityAliasError("identity alias prefix is invalid")
    entries = catalog["entries"]
    if not isinstance(entries, list):
        raise ArbitrationIdentityAliasError("identity catalog entries must be an array")
    pairs, token_values = [], {}
    by_kind: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"kind", "canonical_value", "alias"}:
            raise ArbitrationIdentityAliasError("identity catalog entry is malformed")
        kind, value, token = entry["kind"], entry["canonical_value"], entry["alias"]
        if not isinstance(kind, str) or not kind or not isinstance(value, str) or not value:
            raise ArbitrationIdentityAliasError("identity catalog entry value is invalid")
        if not isinstance(token, str) or (token in token_values and token_values[token] != value):
            raise ArbitrationIdentityAliasError("alias token collision")
        if value.startswith(prefix):
            raise ArbitrationIdentityAliasError("canonical value collides with alias namespace")
        token_values[token] = value
        pairs.append((kind, value))
        by_kind.setdefault(kind, set()).add(value)
    if pairs != sorted(set(pairs)):
        raise ArbitrationIdentityAliasError("identity catalog entries are duplicate or unordered")
    expected_tokens = {value: prefix + str(index) for index, value in enumerate(sorted({value for _kind, value in pairs}))}
    if any(entry["alias"] != expected_tokens[entry["canonical_value"]] for entry in entries):
        raise ArbitrationIdentityAliasError("identity aliases are not deterministic")
    allowed = catalog["allowed_echo_values"]
    if not isinstance(allowed, Mapping):
        raise ArbitrationIdentityAliasError("allowed response identities are malformed")
    for kind, values in allowed.items():
        if not isinstance(kind, str) or not isinstance(values, list):
            raise ArbitrationIdentityAliasError("allowed response identities are malformed")
        if any(not isinstance(value, str) or not value for value in values) or values != sorted(set(values)):
            raise ArbitrationIdentityAliasError("allowed response identity values are invalid")
    if any(not values <= set(allowed.get(kind) or ()) for kind, values in by_kind.items()):
        raise ArbitrationIdentityAliasError("identity entry is outside response echo scope")

    array_locations = catalog["array_catalog_aliases"]
    if not isinstance(array_locations, list):
        raise ArbitrationIdentityAliasError("array catalog alias locations are malformed")
    seen_locations = set()
    for location in array_locations:
        if not isinstance(location, Mapping) or set(location) != {"index", "kind"}:
            raise ArbitrationIdentityAliasError("array catalog alias location is malformed")
        index, kind = location["index"], location["kind"]
        if type(index) is not int or index < 0 or not isinstance(kind, str) or not kind:
            raise ArbitrationIdentityAliasError("array catalog alias location is invalid")
        if (index, kind) in seen_locations:
            raise ArbitrationIdentityAliasError("array catalog alias location collision")
        seen_locations.add((index, kind))
    if array_locations != sorted(array_locations, key=lambda item: (item["index"], item["kind"])):
        raise ArbitrationIdentityAliasError("array catalog alias locations are unordered")
    text_locations = catalog["text_catalog_aliases"]
    if not isinstance(text_locations, list) or any(type(index) is not int or index < 0 for index in text_locations):
        raise ArbitrationIdentityAliasError("text catalog alias locations are invalid")
    if text_locations != sorted(set(text_locations)):
        raise ArbitrationIdentityAliasError("text catalog alias locations are unordered")
    for field in ("canonical_request_hash", "wire_body_hash", "mapping_hash", "catalog_hash"):
        if not isinstance(catalog[field], str) or not _is_hash(catalog[field]):
            raise ArbitrationIdentityAliasError(f"catalog {field} is invalid")
    if catalog["mapping_hash"] != _mapping_hash(
        prefix, entries, array_locations, text_locations
    ):
        raise ArbitrationIdentityAliasError("identity mapping hash differs")
    if catalog["catalog_hash"] != hash_json(_catalog_body(catalog)):
        raise ArbitrationIdentityAliasError("identity catalog hash differs")
    return catalog


def _expand_request_body(
    wire_body: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    array_locations, text_locations, ambiguous = _catalog_locations(wire_body)
    if ambiguous:
        raise ArbitrationIdentityAliasError("wire array catalog has ambiguous identity types")
    if array_locations != catalog["array_catalog_aliases"] or text_locations != catalog["text_catalog_aliases"]:
        raise ArbitrationIdentityAliasError("identity catalog locations differ from wire request")
    context = _Context(
        "expand",
        prefix=catalog["prefix"],
        reverse=_reverse_aliases(catalog["entries"]),
        encoded_pairs={
            (entry["kind"], entry["canonical_value"])
            for entry in catalog["entries"]
        },
    )
    expanded = _walk(wire_body, context, None)
    _transform_catalogs(
        expanded,
        array_locations=array_locations,
        text_locations=text_locations,
        context=context,
    )
    return expanded


def expand_arbitration_identity_request(
    wire_request: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Restore the exact request; reject missing, changed, foreign, or ambiguous catalogs."""
    trusted = _verify_catalog(catalog)
    wire = _json_copy(wire_request)
    if not isinstance(wire, dict):
        raise ArbitrationIdentityAliasError("wire request must be an object")
    metadata = wire.pop(WIRE_KEY, None)
    expected = {
        "schema_version": WIRE_SCHEMA,
        "policy": POLICY,
        "contract": WIRE_CONTRACT,
        "prefix": trusted["prefix"],
        "canonical_request_hash": trusted["canonical_request_hash"],
        "wire_body_hash": trusted["wire_body_hash"],
        "mapping_hash": trusted["mapping_hash"],
        "catalog_hash": trusted["catalog_hash"],
    }
    if not isinstance(metadata, Mapping) or dict(metadata) != expected:
        raise ArbitrationIdentityAliasError("wire alias authority is missing or changed")
    if hash_json(wire) != trusted["wire_body_hash"]:
        raise ArbitrationIdentityAliasError("wire body hash differs from local catalog")
    expanded = _expand_request_body(wire, trusted)
    if hash_json(expanded) != trusted["canonical_request_hash"]:
        raise ArbitrationIdentityAliasError("expanded request hash differs from local catalog")
    collected = _Context("collect")
    _walk(expanded, collected, None)
    arrays, texts, ambiguous = _catalog_locations(expanded)
    if ambiguous:
        raise ArbitrationIdentityAliasError("expanded catalog has ambiguous identity types")
    _transform_catalogs(expanded, array_locations=arrays, text_locations=texts, context=collected)
    expected_allowed = {kind: sorted(values) for kind, values in sorted(collected.allowed.items())}
    if (collected.values != {(entry["kind"], entry["canonical_value"]) for entry in trusted["entries"]}
            or trusted["allowed_echo_values"] != expected_allowed):
        raise ArbitrationIdentityAliasError("catalog identities differ from the actual typed request scope")
    if any(value.startswith(trusted["prefix"]) for value in _all_strings(expanded)):
        raise ArbitrationIdentityAliasError("canonical request collides with the alias namespace")
    return expanded


_RESPONSE_ID_FIELDS = {
    **_ID_FIELDS,
    "selected_candidate_id": "candidate_id",
}
_RESPONSE_ID_ARRAY_FIELDS = {
    **_ID_ARRAY_FIELDS,
    "target_section_ids": "section_id",
    "target_claim_ids": "claim_id",
}


def _decode_echo(
    value: Any,
    kind: str,
    catalog: Mapping[str, Any],
    *,
    hash_value: bool = False,
) -> Any:
    if not isinstance(value, str):
        raise ArbitrationIdentityAliasError(f"{kind} response identity must be text")
    if not value:
        raise ArbitrationIdentityAliasError(f"{kind} response identity must be nonempty")
    prefix = catalog["prefix"]
    reverse = _reverse_aliases(catalog["entries"])
    if value.startswith(prefix):
        found = reverse.get(value)
        if found is None or kind not in found[0]:
            raise ArbitrationIdentityAliasError(f"unknown or wrong-kind {kind} response alias")
        return found[1]
    if hash_value and not _is_hash(value):
        raise ArbitrationIdentityAliasError(f"{kind} response hash is malformed")
    allowed = set(catalog["allowed_echo_values"].get(kind) or ())
    pair = (kind, value)
    encoded = pair in {
        (entry["kind"], entry["canonical_value"]) for entry in catalog["entries"]
    }
    if value not in allowed or encoded:
        raise ArbitrationIdentityAliasError(f"foreign, raw, or unbound {kind} response identity")
    return value


def _decode_new_or_echo(value: Any, kind: str, catalog: Mapping[str, Any]) -> Any:
    """Decode an echoed section alias or retain a genuinely new section ID."""
    if not isinstance(value, str) or not value:
        raise ArbitrationIdentityAliasError(f"{kind} response identity must be nonempty text")
    prefix = catalog["prefix"]
    reverse = _reverse_aliases(catalog["entries"])
    if value.startswith(prefix):
        found = reverse.get(value)
        if found is None or kind not in found[0]:
            raise ArbitrationIdentityAliasError(f"unknown or wrong-kind {kind} response alias")
        return found[1]
    allowed = set(catalog["allowed_echo_values"].get(kind) or ())
    encoded = (kind, value) in {
        (entry["kind"], entry["canonical_value"]) for entry in catalog["entries"]
    }
    if value in allowed and encoded:
        raise ArbitrationIdentityAliasError(f"echoed {kind} must use its alias")
    return value


def canonicalize_arbitration_identity_response(
    response: Mapping[str, Any],
    wire_request: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Decode only echoed identity/hash fields; prose and unknown fields stay literal."""
    trusted = _verify_catalog(catalog)
    expand_arbitration_identity_request(wire_request, trusted)
    output = _json_copy(response)
    if not isinstance(output, dict):
        raise ArbitrationIdentityAliasError("arbitration response must be an object")
    if "selected_candidate_id" not in output:
        raise ArbitrationIdentityAliasError("response lacks selected_candidate_id")

    def walk(value: Any, field: str | None = None) -> Any:
        if field == "new_section_id":
            return _decode_new_or_echo(value, "section_id", trusted)
        if field == "section_order":
            if not isinstance(value, list):
                raise ArbitrationIdentityAliasError("section_order must be an array")
            return [
                _decode_new_or_echo(item, "section_id", trusted) for item in value
            ]
        if field == "source_section_hashes":
            if not isinstance(value, Mapping):
                raise ArbitrationIdentityAliasError("source_section_hashes must be a mapping")
            return {
                _decode_echo(section, "section_id", trusted): _decode_echo(
                    digest, "section_hash", trusted, hash_value=True
                )
                for section, digest in value.items()
            }
        if field == "source_intents":
            if not isinstance(value, Mapping):
                raise ArbitrationIdentityAliasError("source_intents must be a mapping")
            return {
                _decode_echo(section, "section_id", trusted): walk(item)
                for section, item in value.items()
            }
        if field in {"candidate_scores", "candidate_score_map", "candidate_score_by_id"}:
            if not isinstance(value, Mapping):
                raise ArbitrationIdentityAliasError(f"{field} must be a mapping")
            return {
                _decode_echo(candidate, "candidate_id", trusted): walk(score)
                for candidate, score in value.items()
            }
        if field in _RESPONSE_ID_FIELDS:
            return _decode_echo(value, _RESPONSE_ID_FIELDS[field], trusted)
        if field in _RESPONSE_ID_ARRAY_FIELDS:
            if not isinstance(value, list):
                raise ArbitrationIdentityAliasError(f"{field} response field must be an array")
            return [
                _decode_echo(item, _RESPONSE_ID_ARRAY_FIELDS[field], trusted)
                for item in value
            ]
        if field in _HASH_FIELDS:
            return _decode_echo(value, field, trusted, hash_value=True)
        if isinstance(value, Mapping):
            return {key: walk(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    canonical = walk(output)
    if not isinstance(canonical, dict):
        raise ArbitrationIdentityAliasError("canonical response must be an object")
    return canonical


__all__ = [
    "CATALOG_SCHEMA",
    "POLICY",
    "WIRE_KEY",
    "WIRE_SCHEMA",
    "ArbitrationIdentityAliasError",
    "canonicalize_arbitration_identity_response",
    "expand_arbitration_identity_request",
    "project_arbitration_identity_request",
]

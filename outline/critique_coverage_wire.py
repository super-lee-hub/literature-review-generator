"""Lossless positional wire codec for local critique coverage certificates."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from outline.candidate_arbitration_ledger_packing import expand_arbitration_final_ledger
from runtime.provider_runtime import hash_json

WIRE_KEY = "critique_coverage_packing"
WIRE_SCHEMA = "outline_critique_coverage_wire/v1"
FEEDBACK_SCHEMA = "outline_critique_coverage_wire/v2"
_FEEDBACK_ROWS = "$cfr"
_FEEDBACK_KEYS_MODE = "candidate_id:shard:shard_index"
_FEEDBACK_CONTRACT = (
    "Restore feedback before v1 coverage: $cfr.rows are [layout_ref, value_refs]. "
    "Zip layouts[layout_ref] with values[value_ref] in order; every value is complete literal JSON. "
    "Preserve row order and repetitions. For object keys_mode=candidate_id:shard:shard_index, "
    "derive each exact key from that restored row; otherwise use literal keys. "
    "Array rows remain arrays. Then decode coverage_wire by its unchanged v1 contract. "
    "Read all feedback, scientific text, issues and qualifiers; references omit no values."
)
_FEEDBACK_WIRE_KEYS = {
    "schema_version", "resolution_contract", "coverage_wire", "layouts", "values",
    "original_critique_hashes", "feedback_source_hash", "feedback_encoded_hash", "wire_hash",
}
WRAPPER_SCHEMA = "outline_critique_claim_support_coverage/v1"
ATOM_SCHEMA = "critique-claim-support-atom-coverage/v1"

PARENT_COLUMNS = (
    "candidate_id",
    "section_id",
    "parent_candidate_hash",
    "parent_section_hash",
    "parent_source_section_hash",
    "parent_claim_count",
    "parent_support_count",
    "parent_paper_key_count",
    "parent_paper_keys_hash",
    "parent_relation_ids_hash",
)
PART_COLUMNS = (
    "parent_ref",
    "shard_index",
    "claim_indices",
    "support_indices",
    "atomic_groups",
    "comparison_groups",
    "atom_hash",
    "wrapper_hash",
)
RESOLUTION_CONTRACT = (
    "Resolve refs via part_table/parent_table. Restore the exact v1 certificate and atom index, "
    "including every hash, count, index, and group. Never infer proof from prose."
)

_WRAPPER_KEYS = {
    "schema_version",
    "parent_source_section_hash",
    "atom_coverage_index",
    "content_hash",
}
_ATOM_KEYS = {
    "schema_version",
    "candidate_id",
    "section_id",
    "shard_index",
    "parent_candidate_hash",
    "parent_section_hash",
    "parent_claim_count",
    "parent_support_count",
    "parent_paper_key_count",
    "parent_paper_keys_hash",
    "parent_relation_ids_hash",
    "local_to_parent_claim_indices",
    "local_to_parent_support_indices",
    "atomic_claim_groups",
    "comparison_groups",
    "content_hash",
}
_WIRE_KEYS = {
    "schema_version",
    "resolution_contract",
    "parent_columns",
    "parent_table",
    "part_columns",
    "part_table",
    "original_critique_hashes",
    "wire_hash",
}


class CritiqueCoverageWireError(ValueError):
    """A local critique coverage wire is malformed, stale, or foreign."""


def pack_critique_coverage_certificates(
    request: Mapping[str, Any], *, compact_feedback: bool = False,
) -> dict[str, Any]:
    """Deduplicate certificate parent fields and positionally encode part proofs.

    The default v1 compacts only claim/support certificates. The opt-in v2
    additionally tables complete feedback rows and catalogs exact literal JSON
    cell values; expansion restores feedback before the unchanged v1 decoder.
    """

    if not isinstance(request, Mapping):
        raise TypeError("critique coverage request must be a mapping")
    if compact_feedback:
        if isinstance(request.get(WIRE_KEY), Mapping) and request[WIRE_KEY].get("schema_version") == FEEDBACK_SCHEMA:
            expand_critique_coverage_certificates(request)
            return copy.deepcopy(dict(request))
        if _has_feedback_marker(request.get("critiques")):
            raise CritiqueCoverageWireError("source feedback collides with a reserved row marker")
        canonical = expand_critique_coverage_certificates(request)
        return _pack_feedback_layer(pack_critique_coverage_certificates(request), canonical)
    projected = copy.deepcopy(dict(request))
    if WIRE_KEY in projected:
        expand_critique_coverage_certificates(projected)
        return projected

    critics = projected.get("critiques")
    if not isinstance(critics, Mapping):
        if _contains_reference(critics):
            raise CritiqueCoverageWireError("claim/support coverage reference has no wire table")
        return projected

    hashes = _critic_hashes(critics)
    parents: list[list[Any]] = []
    parent_lookup: dict[tuple[Any, ...], int] = {}
    parts: list[list[Any]] = []
    critics_copy = _rewrite_critics(
        critics,
        lambda row: _pack_row(row, parent_lookup, parents, parts),
    )
    if not parts:
        return projected
    _validate_unique_parent_sections(parents)
    _bind_critique_hashes_to_ledger(projected, hashes)
    projected["critiques"] = critics_copy
    body = {
        "schema_version": WIRE_SCHEMA,
        "resolution_contract": RESOLUTION_CONTRACT,
        "parent_columns": list(PARENT_COLUMNS),
        "parent_table": parents,
        "part_columns": list(PART_COLUMNS),
        "part_table": parts,
        "original_critique_hashes": hashes,
    }
    projected[WIRE_KEY] = {**body, "wire_hash": _json_hash(body, "critique coverage wire")}
    return projected


def expand_critique_coverage_certificates(request: Mapping[str, Any]) -> dict[str, Any]:
    """Restore every exact local certificate and verify its source digests."""

    if not isinstance(request, Mapping):
        raise TypeError("critique coverage request must be a mapping")
    expanded = copy.deepcopy(dict(request))
    wire = expanded.get(WIRE_KEY)
    if isinstance(wire, Mapping) and wire.get("schema_version") == FEEDBACK_SCHEMA:
        base, hashes = _expand_feedback_layer(expanded)
        restored = expand_critique_coverage_certificates(base)
        _restore_feedback_key_order(restored["critiques"], base["critiques"], expand_coverage=True)
        if _critic_hashes(restored["critiques"]) != hashes:
            raise CritiqueCoverageWireError("restored feedback differs from the original full critique hashes")
        _bind_critique_hashes_to_ledger(restored, hashes)
        return restored
    if wire is None:
        if _contains_reference(expanded.get("critiques")):
            raise CritiqueCoverageWireError("claim/support coverage reference has no wire table")
        return expanded
    if not isinstance(wire, Mapping) or set(wire) != _WIRE_KEYS:
        raise CritiqueCoverageWireError("critique coverage wire fields do not match v1")
    if wire.get("schema_version") != WIRE_SCHEMA:
        raise CritiqueCoverageWireError("unsupported critique coverage wire schema")
    if wire.get("resolution_contract") != RESOLUTION_CONTRACT:
        raise CritiqueCoverageWireError("critique coverage resolution contract is missing or stale")
    if wire.get("parent_columns") != list(PARENT_COLUMNS):
        raise CritiqueCoverageWireError("critique coverage parent column contract is invalid")
    if wire.get("part_columns") != list(PART_COLUMNS):
        raise CritiqueCoverageWireError("critique coverage part column contract is invalid")
    wire_body = {key: value for key, value in wire.items() if key != "wire_hash"}
    if wire.get("wire_hash") != _json_hash(wire_body, "critique coverage wire"):
        raise CritiqueCoverageWireError("critique coverage wire hash is invalid")

    parents = _read_parent_table(wire.get("parent_table"))
    parts = _read_part_table(wire.get("part_table"), parents)
    critics = expanded.get("critiques")
    if not isinstance(critics, Mapping):
        raise CritiqueCoverageWireError("packed critique coverage has no critique map")
    original_hashes = _read_critic_hashes(wire.get("original_critique_hashes"))
    if set(original_hashes) != set(critics):
        raise CritiqueCoverageWireError("original critique hash identities differ from the request")

    references: list[int] = []
    for critique in critics.values():
        if not isinstance(critique, Mapping):
            continue
        for _row_key, row in _shard_rows(critique.get("candidate_shard_results")):
            if not isinstance(row, Mapping):
                continue
            has_reference = "claim_support_coverage_ref" in row
            has_literal = "claim_support_coverage" in row
            if has_reference and has_literal:
                raise CritiqueCoverageWireError("literal and referenced claim/support coverage are mixed")
            if has_reference:
                ref = row.get("claim_support_coverage_ref")
                if type(ref) is not int or ref < 0 or ref >= len(parts):
                    raise CritiqueCoverageWireError("claim/support coverage reference is invalid")
                references.append(ref)
    if sorted(references) != list(range(len(parts))):
        raise CritiqueCoverageWireError("claim/support coverage references are missing, duplicate, or unused")

    expanded["critiques"] = _rewrite_critics(
        critics,
        lambda row: _expand_row(row, parts, parents),
    )
    restored_hashes = _critic_hashes(expanded["critiques"])
    if restored_hashes != original_hashes:
        raise CritiqueCoverageWireError("restored critique payload differs from its original full hash")
    _bind_critique_hashes_to_ledger(expanded, restored_hashes)
    expanded.pop(WIRE_KEY)
    return expanded


def _feedback_json(value: Any) -> str:
    """Preserve JSON types and array order across transport object-key sorting."""
    def validate(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise CritiqueCoverageWireError("feedback object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in {str, bool, int, float}:
            raise CritiqueCoverageWireError("feedback values must be JSON")
    validate(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise CritiqueCoverageWireError("feedback values must be finite JSON") from exc


def _has_feedback_marker(value: Any) -> bool:
    if isinstance(value, Mapping):
        return _FEEDBACK_ROWS in value or any(_has_feedback_marker(item) for item in value.values())
    return isinstance(value, list) and any(_has_feedback_marker(item) for item in value)


def _derived_feedback_key(row: Mapping[str, Any]) -> str | None:
    candidate, shard = row.get("candidate_id"), row.get("shard_index")
    if not isinstance(candidate, str) or type(shard) is not int:
        return None
    return f"{candidate}:shard:{shard}"


def _restore_feedback_key_order(
    target: Mapping[str, Any], source: Mapping[str, Any], *, expand_coverage: bool,
) -> None:
    """Keep row field order across v1's certificate pop/append operation."""
    for role, critique in target.items():
        original = source.get(role)
        if not isinstance(critique, dict) or not isinstance(original, Mapping):
            continue
        container = critique.get("candidate_shard_results")
        if not isinstance(container, (dict, list)):
            continue
        original_rows = _shard_rows(original.get("candidate_shard_results"))
        current_rows = _shard_rows(container)
        if [key for key, _row in original_rows] != [key for key, _row in current_rows]:
            raise CritiqueCoverageWireError("feedback row occurrence order changed")
        for (key, row), (_original_key, original_row) in zip(current_rows, original_rows, strict=True):
            if not isinstance(row, Mapping) or not isinstance(original_row, Mapping):
                continue
            keys = [
                "claim_support_coverage" if expand_coverage and name == "claim_support_coverage_ref"
                else "claim_support_coverage_ref" if not expand_coverage and name == "claim_support_coverage" and "claim_support_coverage_ref" in row
                else name for name in original_row
            ]
            if len(keys) != len(set(keys)) or set(keys) != set(row):
                raise CritiqueCoverageWireError("feedback row fields changed during coverage restoration")
            container[key] = {name: row[name] for name in keys}


def _pack_feedback_layer(base: Mapping[str, Any], canonical: Mapping[str, Any]) -> dict[str, Any]:
    critics = base.get("critiques")
    if not isinstance(critics, Mapping):
        return copy.deepcopy(dict(base))
    _feedback_json(critics)
    if _has_feedback_marker(critics):
        raise CritiqueCoverageWireError("source feedback collides with a reserved row marker")
    layouts: list[list[str]] = []
    values: list[Any] = []
    value_indices: dict[str, int] = {}

    def cell_ref(value: Any) -> int:
        identity = _feedback_json(value)
        if identity not in value_indices:
            value_indices[identity] = len(values)
            values.append(copy.deepcopy(value))
        return value_indices[identity]

    projected = copy.deepcopy(dict(base))
    _restore_feedback_key_order(projected["critiques"], canonical["critiques"], expand_coverage=False)
    critics = copy.deepcopy(projected["critiques"])
    count = 0
    for critique in projected["critiques"].values():
        if not isinstance(critique, dict):
            continue
        source = critique.get("candidate_shard_results")
        if isinstance(source, Mapping):
            keys, records, kind = list(source), list(source.values()), "object"
        elif isinstance(source, list):
            keys, records, kind = [], source, "array"
        else:
            continue
        if not records or any(not isinstance(row, Mapping) for row in records):
            continue
        table: dict[str, Any] = {"kind": kind, "rows": []}
        if kind == "object":
            if all(_derived_feedback_key(row) == key for key, row in zip(keys, records, strict=True)):
                table["keys_mode"] = _FEEDBACK_KEYS_MODE
            else:
                table["keys"] = keys
        for row in records:
            layout = list(row)
            if layout not in layouts:
                layouts.append(layout)
            table["rows"].append([layouts.index(layout), [cell_ref(row[key]) for key in layout]])
        count += len(records)
        critique["candidate_shard_results"] = {_FEEDBACK_ROWS: table}
    if not count:
        return projected
    coverage_wire = projected.pop(WIRE_KEY, None)
    hashes = _critic_hashes(canonical["critiques"])
    _bind_critique_hashes_to_ledger(base, hashes)
    body = {
        "schema_version": FEEDBACK_SCHEMA, "resolution_contract": _FEEDBACK_CONTRACT,
        "coverage_wire": coverage_wire, "layouts": layouts, "values": values,
        "original_critique_hashes": hashes,
        "feedback_source_hash": _json_hash(_feedback_json(critics), "original feedback"),
        "feedback_encoded_hash": _json_hash(projected["critiques"], "encoded feedback"),
    }
    projected[WIRE_KEY] = {**body, "wire_hash": _json_hash(body, "feedback wire")}
    restored = expand_critique_coverage_certificates(projected)
    if _feedback_json(restored) != _feedback_json(canonical):
        raise CritiqueCoverageWireError("feedback wire did not preserve complete source JSON")
    return projected


def _expand_feedback_layer(request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    wire = request[WIRE_KEY]
    if set(wire) != _FEEDBACK_WIRE_KEYS or wire.get("resolution_contract") != _FEEDBACK_CONTRACT:
        raise CritiqueCoverageWireError("feedback wire fields or resolution contract differ")
    body = {key: value for key, value in wire.items() if key != "wire_hash"}
    if wire.get("wire_hash") != _json_hash(body, "feedback wire"):
        raise CritiqueCoverageWireError("feedback wire hash is invalid")
    hashes = _read_critic_hashes(wire.get("original_critique_hashes"))
    _bind_critique_hashes_to_ledger(request, hashes)
    layouts, values = wire.get("layouts"), wire.get("values")
    if not isinstance(layouts, list) or not layouts or not isinstance(values, list):
        raise CritiqueCoverageWireError("feedback layout/value catalogs must be arrays")
    normalized_layouts: list[list[str]] = []
    for layout in layouts:
        if (not isinstance(layout, list) or any(not isinstance(key, str) for key in layout)
                or len(layout) != len(set(layout)) or layout in normalized_layouts):
            raise CritiqueCoverageWireError("feedback layout is malformed or duplicate")
        normalized_layouts.append(layout)
    identities = [_feedback_json(value) for value in values]
    if len(identities) != len(set(identities)) or any(_has_feedback_marker(value) for value in values):
        raise CritiqueCoverageWireError("feedback value catalog is duplicate or contains a row marker")
    critics = request.get("critiques")
    if not isinstance(critics, Mapping) or set(critics) != set(hashes):
        raise CritiqueCoverageWireError("feedback critic identities differ")
    if _json_hash(critics, "encoded feedback") != wire.get("feedback_encoded_hash"):
        raise CritiqueCoverageWireError("encoded feedback hash differs")
    used_layouts: set[int] = set()
    used_values: set[int] = set()
    restored = copy.deepcopy(dict(request))
    for critique in restored["critiques"].values():
        if not isinstance(critique, dict):
            continue
        encoded = critique.get("candidate_shard_results")
        if not isinstance(encoded, Mapping) or _FEEDBACK_ROWS not in encoded:
            if _has_feedback_marker(encoded):
                raise CritiqueCoverageWireError("feedback row marker is misplaced")
            continue
        if set(encoded) != {_FEEDBACK_ROWS}:
            raise CritiqueCoverageWireError("feedback row marker is mixed with literal fields")
        table = encoded[_FEEDBACK_ROWS]
        if not isinstance(table, Mapping) or not isinstance(table.get("rows"), list) or not table["rows"]:
            raise CritiqueCoverageWireError("feedback row table is malformed")
        kind = table.get("kind")
        derived = kind == "object" and table.get("keys_mode") == _FEEDBACK_KEYS_MODE
        expected = {"kind", "rows"} | ({"keys_mode"} if derived else {"keys"} if kind == "object" else set())
        if kind not in {"object", "array"} or set(table) != expected:
            raise CritiqueCoverageWireError("feedback row table key contract is invalid")
        rows: list[dict[str, Any]] = []
        for encoded_row in table["rows"]:
            if (not isinstance(encoded_row, list) or len(encoded_row) != 2
                    or type(encoded_row[0]) is not int or not 0 <= encoded_row[0] < len(layouts)
                    or not isinstance(encoded_row[1], list)):
                raise CritiqueCoverageWireError("feedback row layout reference is invalid")
            index, refs = encoded_row
            layout = layouts[index]
            if len(refs) != len(layout):
                raise CritiqueCoverageWireError("feedback row width differs from its layout")
            if any(type(ref) is not int or not 0 <= ref < len(values) for ref in refs):
                raise CritiqueCoverageWireError("feedback cell value reference is invalid")
            used_layouts.add(index)
            used_values.update(refs)
            rows.append({key: copy.deepcopy(values[ref]) for key, ref in zip(layout, refs, strict=True)})
        if kind == "array":
            critique["candidate_shard_results"] = rows
        else:
            keys = [_derived_feedback_key(row) for row in rows] if derived else table["keys"]
            if (not isinstance(keys, list) or len(keys) != len(rows)
                    or any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys))):
                raise CritiqueCoverageWireError("feedback record keys are invalid or duplicate")
            critique["candidate_shard_results"] = dict(zip(keys, rows, strict=True))
    if used_layouts != set(range(len(layouts))) or used_values != set(range(len(values))):
        raise CritiqueCoverageWireError("feedback catalogs contain unused entries")
    if _has_feedback_marker(restored["critiques"]):
        raise CritiqueCoverageWireError("feedback row marker is unresolved")
    if _json_hash(_feedback_json(restored["critiques"]), "original feedback") != wire.get("feedback_source_hash"):
        raise CritiqueCoverageWireError("restored feedback differs from its complete source JSON hash")
    old_wire = wire.get("coverage_wire")
    if old_wire is None:
        restored.pop(WIRE_KEY)
    else:
        if not isinstance(old_wire, Mapping) or old_wire.get("schema_version") != WIRE_SCHEMA:
            raise CritiqueCoverageWireError("feedback inner coverage wire is not v1")
        restored[WIRE_KEY] = copy.deepcopy(dict(old_wire))
    return restored, hashes


def _certificate_rows(raw_certificate: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(raw_certificate, Mapping) or set(raw_certificate) != _WRAPPER_KEYS:
        raise CritiqueCoverageWireError("claim/support coverage wrapper fields do not match v1")
    if raw_certificate.get("schema_version") != WRAPPER_SCHEMA:
        raise CritiqueCoverageWireError("unsupported claim/support coverage wrapper schema")
    atom = raw_certificate.get("atom_coverage_index")
    if not isinstance(atom, Mapping) or set(atom) != _ATOM_KEYS:
        raise CritiqueCoverageWireError("claim/support atom coverage fields do not match v1")
    if atom.get("schema_version") != ATOM_SCHEMA:
        raise CritiqueCoverageWireError("unsupported claim/support atom coverage schema")
    atom_body = {key: value for key, value in atom.items() if key != "content_hash"}
    if atom.get("content_hash") != _json_hash(atom_body, "claim/support atom"):
        raise CritiqueCoverageWireError("claim/support atom content hash is invalid")
    wrapper_body = {
        "schema_version": raw_certificate["schema_version"],
        "parent_source_section_hash": raw_certificate["parent_source_section_hash"],
        "atom_coverage_index": dict(atom),
    }
    if raw_certificate.get("content_hash") != _json_hash(wrapper_body, "claim/support wrapper"):
        raise CritiqueCoverageWireError("claim/support wrapper content hash is invalid")

    parent = [
        atom["candidate_id"],
        atom["section_id"],
        atom["parent_candidate_hash"],
        atom["parent_section_hash"],
        raw_certificate["parent_source_section_hash"],
        atom["parent_claim_count"],
        atom["parent_support_count"],
        atom["parent_paper_key_count"],
        atom["parent_paper_keys_hash"],
        atom["parent_relation_ids_hash"],
    ]
    _validate_parent_row(parent)
    claims = atom["local_to_parent_claim_indices"]
    supports = atom["local_to_parent_support_indices"]
    _validate_indices(claims, parent[5], "claim")
    _validate_indices(supports, parent[6], "support")
    _validate_groups(atom["atomic_claim_groups"], claims, parent[5])
    _validate_comparison_groups(atom["comparison_groups"], claims, parent[5])
    shard_index = atom["shard_index"]
    if type(shard_index) is not int or shard_index <= 0:
        raise CritiqueCoverageWireError("claim/support shard index is invalid")
    part = [
        shard_index,
        copy.deepcopy(claims),
        copy.deepcopy(supports),
        copy.deepcopy(atom["atomic_claim_groups"]),
        copy.deepcopy(atom["comparison_groups"]),
        atom["content_hash"],
        raw_certificate["content_hash"],
    ]
    return parent, part


def _read_parent_table(raw: Any) -> list[list[Any]]:
    if not isinstance(raw, list) or not raw:
        raise CritiqueCoverageWireError("critique coverage parent table is empty or invalid")
    parents: list[list[Any]] = []
    seen: set[tuple[Any, ...]] = set()
    section_bindings: dict[tuple[str, str], tuple[Any, ...]] = {}
    for index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) != len(PARENT_COLUMNS):
            raise CritiqueCoverageWireError(f"parent table row {index} has an invalid shape")
        _validate_parent_row(row)
        key = tuple(row)
        if key in seen:
            raise CritiqueCoverageWireError("critique coverage parent table contains duplicates")
        identity = (row[0], row[1])
        if identity in section_bindings and section_bindings[identity] != key:
            raise CritiqueCoverageWireError("critique coverage parent section has conflicting bindings")
        section_bindings[identity] = key
        seen.add(key)
        parents.append(copy.deepcopy(row))
    return parents


def _validate_unique_parent_sections(parents: Sequence[Sequence[Any]]) -> None:
    seen: dict[tuple[str, str], tuple[Any, ...]] = {}
    for parent in parents:
        identity = (str(parent[0]), str(parent[1]))
        binding = tuple(parent)
        if identity in seen and seen[identity] != binding:
            raise CritiqueCoverageWireError("critique coverage parent section has conflicting bindings")
        seen[identity] = binding


def _validate_parent_row(row: Sequence[Any]) -> None:
    if len(row) != len(PARENT_COLUMNS):
        raise CritiqueCoverageWireError("critique coverage parent row has an invalid shape")
    for position in (0, 1):
        if not isinstance(row[position], str) or not row[position].strip():
            raise CritiqueCoverageWireError("critique coverage parent identity is invalid")
    for position in (2, 3, 4, 8, 9):
        if not _is_hash(row[position]):
            raise CritiqueCoverageWireError("critique coverage parent hash is invalid")
    if any(type(row[position]) is not int or row[position] < 0 for position in (5, 6, 7)):
        raise CritiqueCoverageWireError("critique coverage parent counts are invalid")
    if row[5] <= 0 or row[6] <= 0:
        raise CritiqueCoverageWireError("claim/support parent counts must be positive")


def _read_part_table(raw: Any, parents: Sequence[Sequence[Any]]) -> list[list[Any]]:
    if not isinstance(raw, list) or not raw:
        raise CritiqueCoverageWireError("critique coverage part table is empty or invalid")
    parts: list[list[Any]] = []
    seen: set[tuple[int, int]] = set()
    used_parent_refs: set[int] = set()
    for part_index, row in enumerate(raw):
        if not isinstance(row, list) or len(row) != len(PART_COLUMNS):
            raise CritiqueCoverageWireError(f"part table row {part_index} has an invalid shape")
        parent_ref, shard_index, claims, supports, groups, comparisons, atom_hash, wrapper_hash = row
        if type(parent_ref) is not int or parent_ref < 0 or parent_ref >= len(parents):
            raise CritiqueCoverageWireError("claim/support parent reference is invalid")
        used_parent_refs.add(parent_ref)
        parent = parents[parent_ref]
        if type(shard_index) is not int or shard_index <= 0:
            raise CritiqueCoverageWireError("claim/support part shard index is invalid")
        shard_identity = (parent_ref, shard_index)
        if shard_identity in seen:
            raise CritiqueCoverageWireError("claim/support part shard index is duplicated")
        seen.add(shard_identity)
        _validate_indices(claims, parent[5], "claim")
        _validate_indices(supports, parent[6], "support")
        _validate_groups(groups, claims, parent[5])
        _validate_comparison_groups(comparisons, claims, parent[5])
        if not _is_hash(atom_hash) or not _is_hash(wrapper_hash):
            raise CritiqueCoverageWireError("claim/support part content hash is invalid")
        part = copy.deepcopy(row)
        _validate_reconstructed_certificate(parent, part)
        parts.append(part)
    if used_parent_refs != set(range(len(parents))):
        raise CritiqueCoverageWireError("critique coverage parent table has missing or unused rows")
    return parts


def _validate_indices(value: Any, upper_bound: int, label: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(type(index) is not int or index < 0 or index >= upper_bound for index in value)
        or len(value) != len(set(value))
        or value != sorted(value)
    ):
        raise CritiqueCoverageWireError(f"claim/support {label} indices are invalid")


def _validate_groups(groups: Any, selected_claims: Sequence[int], upper_bound: int) -> None:
    if not isinstance(groups, list) or not groups:
        raise CritiqueCoverageWireError("claim/support atomic claim groups are invalid")
    seen: set[int] = set()
    for group in groups:
        _validate_indices(group, upper_bound, "atomic group")
        if not set(group).issubset(selected_claims) or seen.intersection(group):
            raise CritiqueCoverageWireError("claim/support atomic groups overlap or exceed local claims")
        seen.update(group)
    if seen != set(selected_claims):
        raise CritiqueCoverageWireError("claim/support atomic groups omit local claims")


def _validate_comparison_groups(groups: Any, selected_claims: Sequence[int], upper_bound: int) -> None:
    if not isinstance(groups, Mapping):
        raise CritiqueCoverageWireError("claim/support comparison groups are invalid")
    for group_id, row in groups.items():
        if not isinstance(group_id, str) or not group_id or not isinstance(row, Mapping):
            raise CritiqueCoverageWireError("claim/support comparison group identity is invalid")
        if set(row) != {"claim_indices", "required_paper_keys", "relation_ids"}:
            raise CritiqueCoverageWireError("claim/support comparison group fields do not match v1")
        _validate_indices(row["claim_indices"], upper_bound, "comparison group")
        if not set(row["claim_indices"]).issubset(selected_claims):
            raise CritiqueCoverageWireError("claim/support comparison group exceeds local claims")
        for field in ("required_paper_keys", "relation_ids"):
            values = row[field]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
                or (field == "required_paper_keys" and not values)
            ):
                raise CritiqueCoverageWireError("claim/support comparison group context is invalid")


def _validate_reconstructed_certificate(parent: Sequence[Any], part: Sequence[Any]) -> None:
    wrapper = _reconstruct(parent, part)
    atom = wrapper["atom_coverage_index"]
    atom_body = {key: value for key, value in atom.items() if key != "content_hash"}
    if atom["content_hash"] != _json_hash(atom_body, "claim/support atom"):
        raise CritiqueCoverageWireError("packed claim/support atom hash is invalid")
    wrapper_body = {key: value for key, value in wrapper.items() if key != "content_hash"}
    if wrapper["content_hash"] != _json_hash(wrapper_body, "claim/support wrapper"):
        raise CritiqueCoverageWireError("packed claim/support wrapper hash is invalid")


def _reconstruct(parent: Sequence[Any], part: Sequence[Any]) -> dict[str, Any]:
    _parent_ref, shard_index, claims, supports, groups, comparisons, atom_hash, wrapper_hash = part
    parent_values = dict(zip(PARENT_COLUMNS, parent, strict=True))
    atom = {
        "schema_version": ATOM_SCHEMA,
        "candidate_id": parent_values["candidate_id"],
        "section_id": parent_values["section_id"],
        "shard_index": shard_index,
        "parent_candidate_hash": parent_values["parent_candidate_hash"],
        "parent_section_hash": parent_values["parent_section_hash"],
        "parent_claim_count": parent_values["parent_claim_count"],
        "parent_support_count": parent_values["parent_support_count"],
        "parent_paper_key_count": parent_values["parent_paper_key_count"],
        "parent_paper_keys_hash": parent_values["parent_paper_keys_hash"],
        "parent_relation_ids_hash": parent_values["parent_relation_ids_hash"],
        "local_to_parent_claim_indices": copy.deepcopy(claims),
        "local_to_parent_support_indices": copy.deepcopy(supports),
        "atomic_claim_groups": copy.deepcopy(groups),
        "comparison_groups": copy.deepcopy(comparisons),
        "content_hash": atom_hash,
    }
    wrapper_body = {
        "schema_version": WRAPPER_SCHEMA,
        "parent_source_section_hash": parent_values["parent_source_section_hash"],
        "atom_coverage_index": atom,
    }
    return {**wrapper_body, "content_hash": wrapper_hash}


def _pack_row(
    row: Mapping[str, Any],
    parent_lookup: dict[tuple[Any, ...], int],
    parents: list[list[Any]],
    parts: list[list[Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(row))
    if "claim_support_coverage_ref" in result:
        raise CritiqueCoverageWireError("claim/support coverage reference has no wire table")
    if "claim_support_coverage" not in result:
        return result
    raw_certificate = result.pop("claim_support_coverage")
    parent, part = _certificate_rows(raw_certificate)
    parent_ref = parent_lookup.get(tuple(parent))
    if parent_ref is None:
        parent_ref = len(parents)
        parents.append(parent)
        parent_lookup[tuple(parent)] = parent_ref
    if parent_ref < 0 or parent_ref >= len(parents):
        raise CritiqueCoverageWireError("claim/support certificate parent was not indexed")
    part_ref = len(parts)
    parts.append([parent_ref, *part])
    result["claim_support_coverage_ref"] = part_ref
    return result


def _expand_row(row: Mapping[str, Any], parts: Sequence[Sequence[Any]], parents: Sequence[Sequence[Any]]) -> dict[str, Any]:
    result = copy.deepcopy(dict(row))
    if "claim_support_coverage_ref" not in result:
        return result
    ref = result.pop("claim_support_coverage_ref")
    if type(ref) is not int or ref < 0 or ref >= len(parts):
        raise CritiqueCoverageWireError("claim/support coverage reference is invalid")
    result["claim_support_coverage"] = _reconstruct(parents[parts[ref][0]], parts[ref])
    return result


def _rewrite_critics(
    critics: Mapping[str, Any],
    rewrite: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for critic_id, raw_critique in critics.items():
        if not isinstance(raw_critique, Mapping):
            result[critic_id] = copy.deepcopy(raw_critique)
            continue
        critique = copy.deepcopy(dict(raw_critique))
        shards = critique.get("candidate_shard_results")
        if isinstance(shards, Mapping):
            critique["candidate_shard_results"] = {
                key: rewrite(row) if isinstance(row, Mapping) else copy.deepcopy(row)
                for key, row in shards.items()
            }
        elif isinstance(shards, list):
            critique["candidate_shard_results"] = [
                rewrite(row) if isinstance(row, Mapping) else copy.deepcopy(row)
                for row in shards
            ]
        elif isinstance(shards, tuple):
            critique["candidate_shard_results"] = tuple(
                rewrite(row) if isinstance(row, Mapping) else copy.deepcopy(row)
                for row in shards
            )
        result[critic_id] = critique
    return result


def _shard_rows(value: Any) -> list[tuple[Any, Any]]:
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(enumerate(value))
    return []


def _contains_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "claim_support_coverage_ref" in value:
            return True
        return any(_contains_reference(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_reference(item) for item in value)
    return False


def _critic_hashes(critics: Mapping[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for critic_id, critique in critics.items():
        if not isinstance(critic_id, str) or not critic_id.strip():
            raise CritiqueCoverageWireError("critic IDs must be nonempty strings")
        hashes[critic_id] = _json_hash(critique, f"critic {critic_id}")
    return dict(sorted(hashes.items()))


def _read_critic_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise CritiqueCoverageWireError("original critique hash map is missing or invalid")
    result: dict[str, str] = {}
    for critic_id, digest in value.items():
        if not isinstance(critic_id, str) or not critic_id.strip() or not _is_hash(digest):
            raise CritiqueCoverageWireError("original critique hash entry is invalid")
        result[critic_id] = digest
    return dict(sorted(result.items()))


def _bind_critique_hashes_to_ledger(request: Mapping[str, Any], hashes: Mapping[str, str]) -> None:
    ledger = request.get("arbitration_evaluation_ledger")
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(ledger.get("ledger_packing"), Mapping)
    ):
        raise CritiqueCoverageWireError("proof-bearing requests require a packed canonical arbitration ledger")
    try:
        ledger = expand_arbitration_final_ledger(ledger)
    except (TypeError, ValueError, KeyError) as exc:
        raise CritiqueCoverageWireError("canonical arbitration ledger cannot be expanded") from exc
    ledger_hashes = ledger.get("critique_hashes")
    if not isinstance(ledger_hashes, Mapping):
        raise CritiqueCoverageWireError("arbitration ledger lacks the canonical critique hash map")
    if dict(ledger_hashes) != dict(hashes):
        raise CritiqueCoverageWireError("full critique hashes differ from the canonical arbitration ledger")


def _json_hash(value: Any, label: str) -> str:
    try:
        return hash_json(value)
    except (TypeError, ValueError) as exc:
        raise CritiqueCoverageWireError(f"{label} is not JSON serializable") from exc


def _is_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

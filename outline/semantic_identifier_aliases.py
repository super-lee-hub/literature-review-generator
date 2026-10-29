"""Lossless structural identifier aliases with a local, hash-bound catalog.

Only values of explicitly named identifier fields are encoded. Scientific prose
and unknown fields are copied unchanged; the catalog never goes onto the wire.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from runtime.provider_runtime import hash_json


WIRE_KEY = "semantic_identifier_aliases"
POLICY = "global-structural-identifiers/v1"
CATALOG_VERSION = "semantic-identifier-alias-catalog/v1"
WIRE_VERSION = "semantic-identifier-alias-request/v1"
CONTRACT = (
    "Structural identifier aliases are bound to the local catalog and mapping "
    "hash. Treat each alias as its complete canonical identifier. Preserve "
    "source membership, evidence, qualifiers and study ownership. Return aliases "
    "only in the named structural identifier fields; return new global synthesis "
    "claim IDs as synthesis:global_synthesis:<unique-id>. Do not rewrite prose "
    "or invent aliases, source IDs, study IDs or support."
)

IDENTIFIER_FIELDS = frozenset({
    "paper_key", "canonical_paper_key", "paper_keys", "paper_id", "paper_ids",
    "assigned_paper_ids", "integrated_paper_ids", "nonintegrated_paper_ids", "bridge_paper_ids",
    "claim_id", "claim_ids", "source_claim_id", "source_claim_ids", "synthesis_claim_ids",
    "primary_claim_id", "required_source_claim_ids", "qualifier_source_claim_ids",
    "evidence_id", "evidence_ids", "supporting_evidence_ids", "primary_evidence_ids",
    "required_evidence_ids", "qualifier_evidence_ids",
    "source_field_id", "source_field_ids", "required_source_field_ids", "qualifier_source_field_ids",
    "topic_id", "topic_ids",
    "study_id", "study_ids", "owner_study_id", "owner_study_ids",
    "fragment_id", "fragment_ids", "relation_id", "relation_ids", "result_id", "result_ids",
})
_CATALOG_KEYS = {
    "schema_version", "policy", "prefix", "forward", "reverse", "mapping_hash",
    "source_request_hash", "catalog_hash",
}
_WIRE_KEYS = {
    "schema_version", "policy", "contract", "prefix", "mapping_hash",
    "source_request_hash", "catalog_hash",
}


class SemanticIdentifierAliasError(ValueError):
    """The alias plan, local authority or a structural reference is invalid."""


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SemanticIdentifierAliasError("JSON object keys must be strings")
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise SemanticIdentifierAliasError("identifier aliases require finite JSON data")


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return set(value).union(*(_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_strings(item) for item in value))
    return set()


def _transform(value: Any, replace: Callable[[str], str], *, structural: bool = False) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _transform(item, replace, structural=key in IDENTIFIER_FIELDS)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_transform(item, replace, structural=structural) for item in value]
    if structural and isinstance(value, str) and value:
        return replace(value)
    return copy.deepcopy(value)


def _mapping_hash(prefix: str, forward: Mapping[str, str], reverse: Mapping[str, str]) -> str:
    return hash_json({"policy": POLICY, "prefix": prefix, "forward": forward, "reverse": reverse})


def _catalog_body(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in catalog.items() if key != "catalog_hash"}


def _verify_catalog(raw: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _json_copy(raw)
    if not isinstance(catalog, dict) or set(catalog) != _CATALOG_KEYS:
        raise SemanticIdentifierAliasError("identifier alias catalog schema is invalid")
    if catalog["schema_version"] != CATALOG_VERSION or catalog["policy"] != POLICY:
        raise SemanticIdentifierAliasError("identifier alias catalog policy is invalid")
    prefix = catalog["prefix"]
    if not isinstance(prefix, str) or re.fullmatch(r"\$I_*", prefix) is None:
        raise SemanticIdentifierAliasError("identifier alias namespace is invalid")
    forward, reverse = catalog["forward"], catalog["reverse"]
    if not isinstance(forward, dict) or not isinstance(reverse, dict):
        raise SemanticIdentifierAliasError("identifier alias maps must be objects")
    canonical = sorted(forward)
    if any(not key or key.startswith(prefix) for key in canonical):
        raise SemanticIdentifierAliasError("canonical identifier collides with alias namespace")
    expected = {key: prefix + str(index) for index, key in enumerate(canonical)}
    if forward != expected or reverse != {alias: key for key, alias in expected.items()}:
        raise SemanticIdentifierAliasError("identifier alias maps are not exact deterministic inverses")
    if catalog["mapping_hash"] != _mapping_hash(prefix, forward, reverse):
        raise SemanticIdentifierAliasError("identifier alias mapping hash differs")
    if not isinstance(catalog["source_request_hash"], str) or re.fullmatch(r"[0-9a-f]{64}", catalog["source_request_hash"]) is None:
        raise SemanticIdentifierAliasError("identifier alias source hash is invalid")
    if catalog["catalog_hash"] != hash_json(_catalog_body(catalog)):
        raise SemanticIdentifierAliasError("identifier alias catalog hash differs")
    return catalog


def build_identifier_alias_plan(request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a local catalog and a wire request containing only its hashes."""
    source = _json_copy(request)
    if not isinstance(source, dict) or WIRE_KEY in source:
        raise SemanticIdentifierAliasError("request is not an unencoded JSON object")
    identifiers: set[str] = set()

    def collect(value: str) -> str:
        identifiers.add(value)
        return value

    _transform(source, collect)
    all_strings = _strings(source)
    prefix = "$I"
    while any(value.startswith(prefix) for value in all_strings):
        prefix += "_"
    forward = {value: prefix + str(index) for index, value in enumerate(sorted(identifiers))}
    reverse = {alias: value for value, alias in forward.items()}
    catalog = {
        "schema_version": CATALOG_VERSION, "policy": POLICY, "prefix": prefix,
        "forward": forward, "reverse": reverse,
        "mapping_hash": _mapping_hash(prefix, forward, reverse),
        "source_request_hash": hash_json(source),
    }
    catalog["catalog_hash"] = hash_json(catalog)
    wire = _transform(source, forward.__getitem__)
    wire[WIRE_KEY] = {
        "schema_version": WIRE_VERSION, "policy": POLICY, "contract": CONTRACT,
        **{key: catalog[key] for key in ("prefix", "mapping_hash", "source_request_hash", "catalog_hash")},
    }
    if hash_json(expand_identifier_alias_request(wire, catalog)) != hash_json(source):
        raise SemanticIdentifierAliasError("identifier alias plan is not lossless")
    return catalog, wire


def _decode(value: Any, catalog: Mapping[str, Any], *, require_alias: bool) -> Any:
    reverse, prefix = catalog["reverse"], catalog["prefix"]

    def replace(identifier: str) -> str:
        if identifier in reverse:
            return reverse[identifier]
        if identifier.startswith(prefix):
            raise SemanticIdentifierAliasError(f"unknown structural identifier alias: {identifier}")
        if require_alias:
            raise SemanticIdentifierAliasError("encoded request contains an unaliased structural identifier")
        if identifier.startswith("synthesis:global_synthesis:"):
            return identifier
        # Canonical references are safe to retain. Their authority is determined
        # by the full semantic validator after decoding, never by this codec.
        return identifier

    return _transform(value, replace)


def expand_identifier_alias_request(wire_request: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the exact bound request, rejecting changed types or structure."""
    trusted = _verify_catalog(catalog)
    wire = _json_copy(wire_request)
    metadata = wire.pop(WIRE_KEY, None) if isinstance(wire, dict) else None
    if not isinstance(metadata, dict) or set(metadata) != _WIRE_KEYS:
        raise SemanticIdentifierAliasError("identifier alias wire metadata is invalid")
    expected = {
        "schema_version": WIRE_VERSION, "policy": POLICY, "contract": CONTRACT,
        **{key: trusted[key] for key in ("prefix", "mapping_hash", "source_request_hash", "catalog_hash")},
    }
    if hash_json(metadata) != hash_json(expected):
        raise SemanticIdentifierAliasError("identifier alias wire authority differs")
    restored = _decode(wire, trusted, require_alias=True)
    if hash_json(restored) != trusted["source_request_hash"]:
        raise SemanticIdentifierAliasError("identifier alias expanded source hash differs")
    return restored


def decode_identifier_alias_output(output: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Decode only structural aliases; new global claim IDs remain literal."""
    trusted = _verify_catalog(catalog)
    result = _json_copy(output)
    if not isinstance(result, dict) or WIRE_KEY in result:
        raise SemanticIdentifierAliasError("provider output must be ordinary JSON without alias metadata")
    return _decode(result, trusted, require_alias=False)


__all__ = [
    "CATALOG_VERSION", "CONTRACT", "IDENTIFIER_FIELDS", "POLICY", "WIRE_KEY", "WIRE_VERSION",
    "SemanticIdentifierAliasError", "build_identifier_alias_plan",
    "decode_identifier_alias_output", "expand_identifier_alias_request",
]

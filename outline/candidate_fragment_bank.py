"""Candidate-neutral, source-bound fragments for Outline v3 organization.

This module validates intrinsic hashes and exact scope bindings. It does not
read Registry state and therefore does not grant READY artifact or provider
receipt authority; callers must verify those dependency closures separately.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn, TypeGuard, cast

from outline.candidate_output_scope import (
    CandidateOutputScopeError,
    CandidateOutputScopeV1,
    _bridge_claim_rows,
    _canonicalize_claim_lineage,
    _claim_group,
    _collect_claim_graph,
    _inventory_indexes,
    _source_closure,
)
from runtime.provider_runtime import hash_json, hash_text
from services.writer_source_inventory import VerifiedWriterSourceInventoryV1


BANK_SCHEMA_VERSION = "outline_candidate_fragment_bank/v1"
ORGANIZATION_REQUEST_SCHEMA_VERSION = "candidate-organization-request/v1"
ORGANIZATION_MAP_SCHEMA_VERSION = "candidate-organization-map/v1"
METADATA_CATALOG_SCHEMA_VERSION = "candidate-organization-metadata-catalog/v1"
ORGANIZATION_PARTITION_SCHEMA_VERSION = "candidate-organization-partition/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BANK_KEYS = frozenset(
    {
        "schema_version",
        "scope_binding",
        "source_artifact_refs",
        "groups",
        "content_hash",
    }
)
_BANK_BINDING_KEYS = frozenset(
    {
        "registry_job_id",
        "source_inventory_artifact_id",
        "source_inventory_artifact_hash",
        "source_inventory_content_hash",
        "scope_hash",
    }
)
_GROUP_ENTRY_KEYS = frozenset(
    {
        "index",
        "claim_group",
        "support_slots",
        "entry_kind",
        "materialization_state",
        "canonical_claim",
        "text",
        "canonical_payload_hash",
        "seed_identity_hash",
        "generated_text_hash",
        "deferral_reason_code",
        "comparison_artifact_refs",
        "provider_receipt_refs",
    }
)
_GROUP_KEYS = frozenset(
    {
        "claim_group_id",
        "task_id",
        "topic_id",
        "fragment_id",
        "provider_result_id",
        "synthesis_claim_id",
        "claim_hash",
        "paper_keys",
        "support_slot_ids",
        "relation_ids",
    }
)
_SLOT_KEYS = frozenset(
    {
        "claim_slot_id",
        "claim_group_id",
        "task_id",
        "topic_id",
        "fragment_id",
        "provider_result_id",
        "synthesis_claim_id",
        "paper_key",
        "primary_claim_id",
        "study_id",
        "source_claim_ids",
        "evidence_ids",
        "source_field_ids",
        "relation_ids",
    }
)
_ORGANIZATION_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "bank_schema_version",
        "candidate_id",
        "axis_payload",
        "plan_payload",
        "axis_hash",
        "plan_hash",
        "bank_hash",
        "bank_group_count",
        "map_schema_hash",
        "metadata_catalog_schema_version",
        "metadata_catalog_contract_hash",
        "metadata_catalog",
        "metadata_catalog_hash",
        "required_group_indices",
        "required_paper_indices",
        "organization_contract_hash",
        "paper_keys",
        "text_pool",
        "task_refs",
        "topic_refs",
        "card_fields",
        "fragment_cards",
        "organization_map_contract",
        "required_comparison_sections",
    }
)
_ORGANIZATION_PARTITION_KEYS = frozenset(
    {
        "schema_version",
        "partition_index",
        "partition_count",
        "full_bank_hash",
        "full_bank_group_count",
        "full_scope_hash",
        "local_scope_hash",
        "partition_plan_hash",
        "local_to_global_group_indices",
    }
)
_ORGANIZATION_CARD_FIELDS = [
    "fragment_index",
    "text_index",
    "paper_indices",
    "materialization_state",
    "task_ref_index",
    "topic_ref_index",
]
_ORGANIZATION_MAP_KEYS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "axis_hash",
        "plan_hash",
        "bank_hash",
        "map_schema_hash",
        "metadata_catalog_hash",
        "metadata_catalog_contract_hash",
        "required_group_indices",
        "required_paper_indices",
        "organization_contract_hash",
        "sections",
        "deferred",
    }
)
_ORGANIZATION_SECTION_KEYS = frozenset(
    {
        "section_index",
        "metadata_index",
        "paper_roles",
        "fragment_indices",
    }
)
_ORGANIZATION_ROLE_FIELDS = ("paper_index", "role_fragment_index")
_ORGANIZATION_DEFERRED_KEYS = frozenset({"reason_code", "fragment_indices"})
_DEFERRAL_CODES = frozenset(
    {
        "not_selected_for_axis",
        "optional_unresolved",
        "superseded_by_required_comparison",
    }
)
_METADATA_CATALOG_FIELDS = frozenset({"title", "goal", "rationale"})
_METADATA_CATALOG_CONTRACT = {
    "schema_version": METADATA_CATALOG_SCHEMA_VERSION,
    "entry_fields": sorted(_METADATA_CATALOG_FIELDS),
    "selection": "trusted-metadata-index-only",
}
METADATA_CATALOG_CONTRACT_HASH = hash_json(_METADATA_CATALOG_CONTRACT)
_MAP_SCHEMA_CONTRACT = {
    "schema_version": ORGANIZATION_MAP_SCHEMA_VERSION,
    "top_level_fields": sorted(_ORGANIZATION_MAP_KEYS),
    "section_fields": sorted(_ORGANIZATION_SECTION_KEYS),
    "paper_role_fields": list(_ORGANIZATION_ROLE_FIELDS),
    "deferred_fields": sorted(_ORGANIZATION_DEFERRED_KEYS),
    "deferral_codes": sorted(_DEFERRAL_CODES),
    "indices": "nonnegative-unique-bank-indices",
    "metadata": "closed-trusted-catalog-index-only",
    "paper_roles": "selected-source-fragment-index-only",
    "claims": "bank-references-only",
    "required_comparisons": "exact-question-metadata-and-exact-source-pair-section",
}
ORGANIZATION_MAP_SCHEMA_HASH = hash_json(_MAP_SCHEMA_CONTRACT)


class CandidateFragmentBankError(ValueError):
    """Raised when candidate fragment provenance or organization binding fails."""


def _organization_map_contract() -> dict[str, Any]:
    return {
        "schema_version": ORGANIZATION_MAP_SCHEMA_VERSION,
        "top_level_fields": list(_MAP_SCHEMA_CONTRACT["top_level_fields"]),
        "section_fields": list(_MAP_SCHEMA_CONTRACT["section_fields"]),
        "paper_role_fields": list(_MAP_SCHEMA_CONTRACT["paper_role_fields"]),
        "deferred_fields": list(_MAP_SCHEMA_CONTRACT["deferred_fields"]),
        "deferral_codes": list(_MAP_SCHEMA_CONTRACT["deferral_codes"]),
        "indices": str(_MAP_SCHEMA_CONTRACT["indices"]),
        "claims": str(_MAP_SCHEMA_CONTRACT["claims"]),
        "required_comparisons": str(_MAP_SCHEMA_CONTRACT["required_comparisons"]),
    }


def _model_payload(value: Any, label: str) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    raw = to_dict() if callable(to_dict) else value
    if not isinstance(raw, Mapping):
        _error(f"{label} must be an existing semantic object or mapping")
    payload = dict(raw)
    _require_serializable(payload, label)
    return json.loads(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )


def _organization_semantics(
    axis: Any,
    plan: Any,
) -> tuple[str, dict[str, Any], dict[str, Any], str, str]:
    axis_payload = _model_payload(axis, "axis")
    plan_payload = _model_payload(plan, "plan")
    axis_id = _require_text(axis_payload.get("axis_id"), "axis.axis_id")
    candidate_id = _require_text(plan_payload.get("candidate_id"), "plan.candidate_id")
    if plan_payload.get("axis_id") != axis_id:
        _error("candidate plan axis_id differs from its organizing axis")
    axis_logic = _require_text(
        axis_payload.get("organizing_logic"), "axis.organizing_logic"
    )
    plan_logic = _require_text(
        plan_payload.get("organizing_logic"), "plan.organizing_logic"
    )
    if plan_logic != axis_logic:
        _error("candidate plan organizing_logic differs from its organizing axis")
    return (
        candidate_id,
        axis_payload,
        plan_payload,
        hash_json(axis_payload),
        hash_json(plan_payload),
    )


def _metadata_catalog_payload(value: Any) -> list[dict[str, str]]:
    if not _is_sequence(value) or not value:
        _error("metadata_catalog must be a nonempty array of trusted templates")
    result: list[dict[str, str]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping) or set(row) != _METADATA_CATALOG_FIELDS:
            _error(f"metadata_catalog[{index}] has unknown or missing fields")
        result.append(
            {
                field: _require_text(
                    row.get(field), f"metadata_catalog[{index}].{field}"
                )
                for field in ("title", "goal", "rationale")
            }
        )
    _require_serializable(result, "metadata_catalog")
    return result


def _required_index_binding(
    bank: Mapping[str, Any],
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
) -> tuple[list[int], list[int]]:
    group_ids = _unique_strings(required_group_ids, "required_group_ids")
    paper_keys_required = _unique_strings(required_paper_keys, "required_paper_keys")
    group_index = {
        str(row["claim_group"]["claim_group_id"]): int(row["index"])
        for row in bank["groups"]
    }
    paper_keys = sorted(
        {
            str(slot["paper_key"])
            for row in bank["groups"]
            for slot in row["support_slots"]
        }
    )
    paper_index = {paper_key: index for index, paper_key in enumerate(paper_keys)}
    missing_groups = sorted(set(group_ids) - set(group_index))
    missing_papers = sorted(set(paper_keys_required) - set(paper_index))
    if missing_groups:
        _error(f"required group {missing_groups[0]} is absent from the bank")
    if missing_papers:
        _error(f"required paper {missing_papers[0]} is absent from the bank")
    return (
        sorted(group_index[group_id] for group_id in group_ids),
        sorted(paper_index[paper_key] for paper_key in paper_keys_required),
    )


def _organization_contract_hash(
    *,
    candidate_id: str,
    axis_hash: str,
    plan_hash: str,
    bank_hash: str,
    metadata_catalog_hash: str,
    required_group_indices: Sequence[int],
    required_paper_indices: Sequence[int],
    required_comparison_sections: Sequence[Mapping[str, Any]],
    partition_hash: str | None = None,
) -> str:
    contract = {
        "schema_version": "candidate-organization-contract/v1",
        "candidate_id": candidate_id,
        "axis_hash": axis_hash,
        "plan_hash": plan_hash,
        "bank_hash": bank_hash,
        "map_schema_hash": ORGANIZATION_MAP_SCHEMA_HASH,
        "metadata_catalog_hash": metadata_catalog_hash,
        "metadata_catalog_contract_hash": METADATA_CATALOG_CONTRACT_HASH,
        "required_group_indices": list(required_group_indices),
        "required_paper_indices": list(required_paper_indices),
        "required_comparison_sections": list(required_comparison_sections),
    }
    if partition_hash is not None:
        contract["partition_hash"] = _require_hash(partition_hash, "partition_hash")
    return hash_json(contract)


def _error(message: str) -> NoReturn:
    raise CandidateFragmentBankError(message)


def _is_sequence(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _error(f"{label} must be a lowercase SHA-256 hash")
    return value


def _require_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _error(f"{label} must be a nonempty string")
    return value


def _require_serializable(value: Any, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CandidateFragmentBankError(f"{label} must be strict JSON data") from exc


def _unique_strings(values: Any, label: str, *, required: bool = False) -> list[str]:
    if not _is_sequence(values):
        _error(f"{label} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        _error(f"{label} contains an empty or non-string value")
    result = list(values)
    if len(set(result)) != len(result):
        _error(f"{label} contains duplicate values")
    if required and not result:
        _error(f"{label} is required")
    return result


def _normalize_refs(
    values: Any,
    label: str,
    *,
    allow_receipt_id: bool = False,
) -> list[dict[str, Any]]:
    if not _is_sequence(values):
        _error(f"{label} must be an array")
    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            _error(f"{label}[{index}] must be an object")
        row = dict(raw)
        _require_serializable(row, f"{label}[{index}]")
        identity_key = "artifact_id"
        if allow_receipt_id and not str(row.get(identity_key) or "").strip():
            identity_key = "receipt_id"
        ref_id = str(row.get(identity_key) or "").strip()
        if not ref_id:
            _error(f"{label}[{index}] requires {identity_key}")
        ref_hash = row.get("content_hash")
        if ref_hash is None and allow_receipt_id:
            ref_hash = row.get("receipt_hash")
        _require_hash(ref_hash, f"{label}[{index}].content_hash")
        identity = (identity_key, ref_id)
        if identity in identities:
            _error(f"{label} contains duplicate references")
        identities.add(identity)
        rows.append(row)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("artifact_id") or item.get("receipt_id") or ""),
            str(item.get("content_hash") or item.get("receipt_hash") or ""),
            hash_json(item),
        ),
    )


def _scope_binding(scope: CandidateOutputScopeV1) -> dict[str, str]:
    return {
        "registry_job_id": scope.registry_job_id,
        "source_inventory_artifact_id": scope.source_inventory_artifact_id,
        "source_inventory_artifact_hash": scope.source_inventory_artifact_hash,
        "source_inventory_content_hash": scope.source_inventory_content_hash,
        "scope_hash": scope.content_hash,
    }


def _organization_partition_payload(
    value: Any,
    *,
    bank_hash: str,
    full_bank_group_count: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ORGANIZATION_PARTITION_KEYS:
        _error("organization partition descriptor has unknown or missing fields")
    _require_serializable(dict(value), "organization partition descriptor")
    if value.get("schema_version") != ORGANIZATION_PARTITION_SCHEMA_VERSION:
        _error("organization partition descriptor has an unsupported schema version")
    partition_index, partition_count = (
        value.get("partition_index"),
        value.get("partition_count"),
    )
    if (
        type(partition_index) is not int
        or partition_index < 0
        or type(partition_count) is not int
        or partition_count < 1
        or partition_index >= partition_count
    ):
        _error("organization partition descriptor has invalid leaf indices")
    if value.get("full_bank_hash") != bank_hash:
        _error("organization partition descriptor is bound to another full bank")
    if (
        type(value.get("full_bank_group_count")) is not int
        or value["full_bank_group_count"] != full_bank_group_count
    ):
        _error(
            "organization partition descriptor has a different full bank group count"
        )
    for name in ("full_scope_hash", "local_scope_hash", "partition_plan_hash"):
        _require_hash(value.get(name), f"organization partition descriptor.{name}")
    indices = _request_index_array(
        value.get("local_to_global_group_indices"),
        upper_bound=full_bank_group_count,
        label="organization partition descriptor.local_to_global_group_indices",
    )
    if indices != sorted(indices):
        _error("organization partition global indices must use canonical bank order")
    return {
        "schema_version": ORGANIZATION_PARTITION_SCHEMA_VERSION,
        "partition_index": partition_index,
        "partition_count": partition_count,
        "full_bank_hash": bank_hash,
        "full_bank_group_count": full_bank_group_count,
        "full_scope_hash": str(value["full_scope_hash"]),
        "local_scope_hash": str(value["local_scope_hash"]),
        "partition_plan_hash": str(value["partition_plan_hash"]),
        "local_to_global_group_indices": indices,
    }


def _validate_organization_partition_scope(
    partition: Mapping[str, Any],
    bank: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
) -> None:
    if partition.get("full_scope_hash") != scope.content_hash:
        _error("organization partition descriptor is bound to another full scope")
    global_indices = partition["local_to_global_group_indices"]
    group_ids = [
        str(bank["groups"][index]["claim_group"]["claim_group_id"])
        for index in global_indices
    ]
    try:
        local_scope = scope.for_groups(group_ids, allowed_paper_keys=scope.paper_keys)
    except CandidateOutputScopeError as exc:
        raise CandidateFragmentBankError(
            f"organization partition is not source-closed: {exc}"
        ) from exc
    if partition.get("local_scope_hash") != local_scope.content_hash:
        _error(
            "organization partition descriptor local scope hash differs from complete group closure"
        )


def _inventory_ref_matches(
    binding: Mapping[str, Any], refs: Sequence[Mapping[str, Any]]
) -> bool:
    for ref in refs:
        artifact_hash = str(ref.get("artifact_hash") or ref.get("content_hash") or "")
        inventory_content_hash = str(
            ref.get("inventory_content_hash")
            or ref.get("source_inventory_content_hash")
            or binding["source_inventory_content_hash"]
        )
        if (
            str(ref.get("artifact_id") or "") == binding["source_inventory_artifact_id"]
            and artifact_hash == binding["source_inventory_artifact_hash"]
            and inventory_content_hash == binding["source_inventory_content_hash"]
        ):
            return True
    return False


def _topic_claim_records(
    topic_routes: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str, str, str, str], Mapping[str, Any]],
    dict[tuple[str, str, str, str], tuple[str, ...]],
]:
    records: dict[tuple[str, str, str, str, str], Mapping[str, Any]] = {}
    fragment_papers: dict[tuple[str, str, str, str], tuple[str, ...]] = {}
    for route in topic_routes:
        if not isinstance(route, Mapping):
            _error("topic route must be an object")
        task_id = str(route.get("logical_node_id") or "").strip()
        topic_id = str(route.get("topic_id") or "").strip()
        route_papers = _unique_strings(
            route.get("paper_ids") or (), f"{topic_id}.paper_ids"
        )
        if not task_id or not topic_id:
            _error("topic route lacks its existing task and topic identity")
        for fragment in route.get("fragments") or ():
            if not isinstance(fragment, Mapping):
                _error(f"{topic_id} contains a malformed fragment")
            fragment_id = str(fragment.get("fragment_id") or "").strip()
            fragment_paper_values = (
                _unique_strings(
                    fragment.get("paper_ids") or (), f"{fragment_id}.paper_ids"
                )
                or route_papers
            )
            if not fragment_id:
                _error(f"{topic_id} contains a fragment without identity")
            for result in fragment.get("provider_results") or ():
                if not isinstance(result, Mapping):
                    _error(f"{fragment_id} contains a malformed provider result")
                result_id = str(result.get("result_id") or "").strip()
                output = result.get("provider_output")
                if not result_id or not isinstance(output, Mapping):
                    continue
                claims = output.get("claims") or ()
                if not _is_sequence(claims):
                    _error(f"{result_id}.claims must be an array")
                context_key = (task_id, topic_id, fragment_id, result_id)
                fragment_papers[context_key] = tuple(fragment_paper_values)
                for claim in claims:
                    if not isinstance(claim, Mapping):
                        _error(f"{result_id} contains a malformed synthesis claim")
                    claim_id = str(claim.get("claim_id") or "").strip()
                    identity = (task_id, topic_id, fragment_id, result_id, claim_id)
                    if identity in records:
                        _error("topic results repeat a complete claim identity")
                    records[identity] = dict(claim)
    return records, fragment_papers


def _validate_comparison_seed_identity(
    group: Mapping[str, Any],
    support_slots: Sequence[Mapping[str, Any]],
    label: str,
) -> None:
    relation_ids = _unique_strings(
        group.get("relation_ids"), f"{label}.relation_ids", required=True
    )
    paper_keys = _unique_strings(
        group.get("paper_keys"), f"{label}.paper_keys", required=True
    )
    if len(relation_ids) != 1 or len(paper_keys) != 2:
        _error(f"{label} comparison seed must bind one relation and two paper sides")
    relation_id = relation_ids[0]
    contract = group.get("task_contract")
    expected_contract_fields = {
        "schema_version",
        "task_id",
        "relation_id",
        "relation_type",
        "comparison_question",
        "output_semantics",
        "relation_id_is_not_a_conclusion",
    }
    if not isinstance(contract, Mapping) or set(contract) != expected_contract_fields:
        _error(f"{label} comparison seed has an invalid task contract")
    if (
        contract.get("schema_version") != "relation-comparison-task/v1"
        or contract.get("task_id") != group.get("task_id")
        or contract.get("task_id") != f"relation_comparison:{relation_id}"
        or contract.get("relation_id") != relation_id
        or not isinstance(contract.get("relation_type"), str)
        or not str(contract.get("relation_type") or "").strip()
        or not isinstance(contract.get("comparison_question"), str)
        or not str(contract.get("comparison_question") or "").strip()
        or contract.get("output_semantics")
        != "model_authored_comparison_using_both_source_sides_and_their_qualifiers"
        or contract.get("relation_id_is_not_a_conclusion") is not True
    ):
        _error(
            f"{label} comparison seed task contract differs from the current relation contract"
        )
    if (
        group.get("topic_id") != ""
        or group.get("fragment_id") != ""
        or group.get("provider_result_id") != ""
        or group.get("claim_kind") != "model_authored_relation_comparison_slot"
        or group.get("claim_seed") is not True
    ):
        _error(f"{label} comparison seed has foreign origin metadata")

    paper_position = {paper_key: index for index, paper_key in enumerate(paper_keys)}
    ordered_slots = sorted(
        support_slots,
        key=lambda item: (
            paper_position.get(str(item.get("paper_key") or ""), len(paper_keys)),
            str(item.get("primary_claim_id") or ""),
        ),
    )
    if any(
        str(slot.get("paper_key") or "") not in paper_position for slot in ordered_slots
    ):
        _error(f"{label} comparison seed support contains a foreign paper side")
    if {str(slot.get("paper_key") or "") for slot in ordered_slots} != set(paper_keys):
        _error(f"{label} comparison seed omits one of its source sides")
    seed_payload = {
        "schema_version": "relation-comparison-claim-seed/v1",
        "task_contract": dict(contract),
        "paper_keys": paper_keys,
        "support": [
            {
                "paper_key": slot["paper_key"],
                "primary_claim_id": slot["primary_claim_id"],
                "study_id": slot["study_id"],
                "source_claim_ids": list(slot["source_claim_ids"]),
                "evidence_ids": list(slot["evidence_ids"]),
                "source_field_ids": list(slot["source_field_ids"]),
            }
            for slot in ordered_slots
        ],
    }
    seed_hash = hash_json(seed_payload)
    expected_group_id = f"candidate-relation-comparison-group:v1:{seed_hash[:24]}"
    expected_claim_id = f"synthesis:relation_comparison:{seed_hash[:24]}"
    if (
        group.get("claim_hash") != seed_hash
        or group.get("claim_group_id") != expected_group_id
        or group.get("synthesis_claim_id") != expected_claim_id
    ):
        _error(
            f"{label} comparison seed identity hash differs from its exact task and support"
        )
    for slot in ordered_slots:
        slot_identity = {
            "group_id": expected_group_id,
            "paper_key": slot["paper_key"],
            "primary_claim_id": slot["primary_claim_id"],
        }
        expected_slot_id = (
            "candidate-relation-comparison-slot:v1:" + hash_json(slot_identity)[:24]
        )
        if (
            slot.get("claim_slot_id") != expected_slot_id
            or slot.get("claim_group_id") != expected_group_id
            or slot.get("task_id") != group.get("task_id")
            or slot.get("topic_id") != ""
            or slot.get("fragment_id") != ""
            or slot.get("provider_result_id") != ""
            or slot.get("synthesis_claim_id") != expected_claim_id
            or slot.get("relation_ids") != relation_ids
        ):
            _error(
                f"{label} comparison seed slot identity differs from its source group"
            )


def _comparison_source_field_closure(
    paper_key: str,
    evidence_ids: Sequence[str],
    closure_field_ids: Sequence[str],
    study_id: str,
    indexes: Mapping[str, Any],
) -> tuple[str, ...]:
    expected = set(closure_field_ids)
    for evidence_id in evidence_ids:
        evidence = indexes["evidence"].get(paper_key, {}).get(evidence_id)
        evidence_text = str(evidence.text or "").strip() if evidence is not None else ""
        if not evidence_text:
            continue
        normalized_text = evidence_text.casefold()
        for source_field in indexes["fields"].get(paper_key, {}).values():
            values = {
                str(source_field.source_value or "").strip().casefold(),
                str(source_field.derived_value or "").strip().casefold(),
            }
            if normalized_text not in values:
                continue
            field_owners = {
                str(value) for value in source_field.owner_study_ids if str(value)
            }
            if field_owners and (not study_id or not field_owners.issubset({study_id})):
                continue
            if source_field.scope == "explicit_study" and (
                not field_owners or not study_id or study_id not in field_owners
            ):
                continue
            expected.add(source_field.source_field_id)
    return tuple(sorted(expected))


def resolve_candidate_fragment_claims(
    scope: CandidateOutputScopeV1,
    inventory: VerifiedWriterSourceInventoryV1,
    topic_routes: Sequence[Mapping[str, Any]],
    bridge_claims: Sequence[Mapping[str, Any]],
    *,
    lineage_claims: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Resolve every non-seed group through its complete existing result identity.

    Claim text is returned exactly as stored in the canonicalized source result.
    The claim group is also rebuilt with the existing scope source validators so
    task, topic, fragment, result, claim hash, and complete slot identity must all
    agree with the supplied scope. ``lineage_claims`` must be the same verified
    ancestor projection used to build that scope; they resolve source references
    without becoming additional fragment groups.
    """

    try:
        if type(scope) is not CandidateOutputScopeV1:
            _error("scope must be a CandidateOutputScopeV1")
        if (
            type(inventory) is not VerifiedWriterSourceInventoryV1
            or not inventory.is_verified
        ):
            _error("candidate fragments require a verified source inventory")
        if (
            scope.registry_job_id != inventory.registry_job_id
            or scope.source_inventory_artifact_id != inventory.artifact_id
            or scope.source_inventory_artifact_hash != inventory.artifact_hash
            or scope.source_inventory_content_hash != inventory.content_hash
        ):
            _error("scope source inventory binding differs from the verified inventory")
        if not _is_sequence(topic_routes) or not _is_sequence(bridge_claims):
            _error("topic routes and bridge claims must be arrays")
        if not _is_sequence(lineage_claims):
            _error("lineage_claims must be an array of retained ancestor claims")

        route_records, fragment_papers = _topic_claim_records(topic_routes)
        bridge_rows = _bridge_claim_rows(bridge_claims)
        graph = _collect_claim_graph(topic_routes, bridge_rows, lineage_claims)
        topic_ids = {str(route.get("topic_id") or "") for route in topic_routes}
        topic_papers: dict[str, set[str]] = {}
        for route in topic_routes:
            topic_id = str(route.get("topic_id") or "")
            papers = set(
                _unique_strings(route.get("paper_ids") or (), f"{topic_id}.paper_ids")
            )
            for fragment in route.get("fragments") or ():
                if isinstance(fragment, Mapping):
                    papers.update(
                        _unique_strings(
                            fragment.get("paper_ids") or (),
                            f"{fragment.get('fragment_id') or topic_id}.paper_ids",
                        )
                    )
            topic_papers[topic_id] = papers

        bridge_records: dict[tuple[str, str, str, str, str], Mapping[str, Any]] = {}
        for task_id, result_id, claim_id, claim in bridge_rows:
            fragment_id = str(claim.get("fragment_id") or "")
            identity = (task_id, "", fragment_id, result_id, claim_id)
            if identity in bridge_records:
                _error("bridge results repeat a complete claim identity")
            bridge_records[identity] = claim

        indexes = _inventory_indexes(inventory)
        records = {**route_records, **bridge_records}
        canonical_by_group: dict[str, dict[str, Any]] = {}
        for group in scope.claim_groups:
            if group.claim_seed:
                seed_slots = [
                    slot.to_dict()
                    for slot in scope.claim_slots
                    if slot.claim_group_id == group.claim_group_id
                ]
                _validate_comparison_seed_identity(
                    group.to_dict(), seed_slots, group.claim_group_id
                )
                for slot in scope.claim_slots:
                    if slot.claim_group_id != group.claim_group_id:
                        continue
                    paper = indexes["papers"].get(slot.paper_key)
                    if paper is None:
                        _error(
                            f"comparison seed {group.claim_group_id} owns an unknown paper"
                        )
                    source_ids, evidence_ids, field_ids, study_id = _source_closure(
                        paper, slot.primary_claim_id, indexes
                    )
                    expected_fields = _comparison_source_field_closure(
                        slot.paper_key, evidence_ids, field_ids, study_id, indexes
                    )
                    if (
                        slot.source_claim_ids != source_ids
                        or slot.evidence_ids != evidence_ids
                        or slot.source_field_ids != expected_fields
                        or slot.study_id != study_id
                    ):
                        _error(
                            f"comparison seed {group.claim_group_id} source slot differs from verified ownership closure"
                        )
                continue
            identity = (
                group.task_id,
                group.topic_id,
                group.fragment_id,
                group.provider_result_id,
                group.synthesis_claim_id,
            )
            original = records.get(identity)
            if original is None:
                _error(
                    f"claim group {group.claim_group_id} has no result with its complete task/topic/fragment/result/claim identity"
                )
            canonical = _canonicalize_claim_lineage(original, graph, indexes)
            if hash_json(canonical) != group.claim_hash:
                _error(
                    f"claim group {group.claim_group_id} canonical payload hash differs from scope"
                )
            if group.task_id == "cross_group_comparison":
                claim_topics = set(canonical.get("topic_ids") or ())
                if not claim_topics or not claim_topics.issubset(topic_ids):
                    _error(
                        "cross-group bridge claim references a topic outside the ready task plan"
                    )
                claim_papers = set(canonical.get("paper_keys") or ())
                if "paper_key" in canonical:
                    claim_papers = {str(canonical.get("paper_key") or "")}
                allowed = set().union(
                    *(topic_papers.get(topic, set()) for topic in claim_topics)
                )
                if not claim_papers or not claim_papers.issubset(allowed):
                    _error(
                        "cross-group bridge claim paper scope differs from its declared topics"
                    )

            task_papers = fragment_papers.get(
                (
                    group.task_id,
                    group.topic_id,
                    group.fragment_id,
                    group.provider_result_id,
                ),
                (),
            )
            computed_group, computed_slots = _claim_group(
                inventory=inventory,
                indexes=indexes,
                task_id=group.task_id,
                topic_id=group.topic_id,
                fragment_id=group.fragment_id,
                provider_result_id=group.provider_result_id,
                claim=canonical,
                selected_relation_ids=scope.selected_relation_ids,
                task_paper_keys=task_papers,
            )
            if computed_group.to_dict() != group.to_dict():
                _error(
                    f"claim group {group.claim_group_id} complete source identity differs from scope"
                )
            scope_slots = sorted(
                (
                    slot.to_dict()
                    for slot in scope.claim_slots
                    if slot.claim_group_id == group.claim_group_id
                ),
                key=lambda item: item["claim_slot_id"],
            )
            rebuilt_slots = sorted(
                (slot.to_dict() for slot in computed_slots),
                key=lambda item: item["claim_slot_id"],
            )
            if scope_slots != rebuilt_slots:
                _error(
                    f"claim group {group.claim_group_id} source support slots differ from the verified closure"
                )
            if canonical.get("claim_id") != group.synthesis_claim_id:
                _error(
                    f"claim group {group.claim_group_id} synthesis identity differs from its result"
                )
            canonical_by_group[group.claim_group_id] = canonical

        expected = {
            group.claim_group_id for group in scope.claim_groups if not group.claim_seed
        }
        if set(canonical_by_group) != expected:
            _error("canonical claim resolution is incomplete")
        return canonical_by_group
    except CandidateOutputScopeError as exc:
        raise CandidateFragmentBankError(str(exc)) from exc


def _comparison_rows_by_group(
    comparison_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if not _is_sequence(comparison_rows):
        _error("comparison_rows must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(comparison_rows):
        if not isinstance(row, Mapping):
            _error(f"comparison_rows[{index}] must be an object")
        group_id = str(row.get("claim_group_id") or row.get("group_id") or "").strip()
        if not group_id or group_id in result:
            _error("comparison rows require unique existing claim_group_id values")
        result[group_id] = row
    return result


def _comparison_materialization(
    group: Any,
    row: Mapping[str, Any] | None,
) -> tuple[str, str | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    if row is None:
        return "unresolved", None, "optional_unresolved", [], []
    state = str(row.get("materialization_state") or "materialized")
    if state == "unresolved":
        reason = str(row.get("deferral_reason_code") or "optional_unresolved")
        if reason != "optional_unresolved":
            _error(
                "unresolved optional comparison requires the optional_unresolved deferral code"
            )
        if row.get("text") not in (None, ""):
            _error("unresolved comparison cannot carry candidate text")
        if row.get("artifact_refs") or row.get("provider_receipt_refs"):
            _error("unresolved comparison cannot carry materialization references")
        return "unresolved", None, reason, [], []
    if state != "materialized":
        _error(
            f"comparison group {group.claim_group_id} has an unsupported materialization state"
        )
    text = _require_text(row.get("text"), f"comparison {group.claim_group_id}.text")
    artifact_refs = _normalize_refs(
        row.get("artifact_refs") or (),
        f"comparison {group.claim_group_id}.artifact_refs",
    )
    receipt_refs = _normalize_refs(
        row.get("provider_receipt_refs") or (),
        f"comparison {group.claim_group_id}.provider_receipt_refs",
        allow_receipt_id=True,
    )
    if not artifact_refs or not receipt_refs:
        _error(
            f"materialized comparison {group.claim_group_id} requires supplied artifact and provider receipt references"
        )
    return "materialized", text, None, artifact_refs, receipt_refs


def build_candidate_fragment_bank_v1(
    scope: CandidateOutputScopeV1,
    canonical_claims: Mapping[str, Mapping[str, Any]],
    comparison_rows: Sequence[Mapping[str, Any]],
    required_group_ids: Sequence[str],
    source_artifact_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a candidate-neutral bank that keeps every scope group and slot."""

    if type(scope) is not CandidateOutputScopeV1:
        _error("scope must be a CandidateOutputScopeV1")
    if not isinstance(canonical_claims, Mapping):
        _error(
            "canonical_claims must map group IDs to complete canonical claim payloads"
        )
    group_by_id = {group.claim_group_id: group for group in scope.claim_groups}
    required = set(_unique_strings(required_group_ids, "required_group_ids"))
    unknown_required = sorted(required - set(group_by_id))
    if unknown_required:
        _error(f"required group {unknown_required[0]} is absent from scope")
    seed_groups = {
        group.claim_group_id: group for group in scope.claim_groups if group.claim_seed
    }
    missing_required_seed: list[str] = []
    comparison_by_group = _comparison_rows_by_group(comparison_rows)
    unknown_comparisons = sorted(set(comparison_by_group) - set(seed_groups))
    if unknown_comparisons:
        _error(
            f"comparison row {unknown_comparisons[0]} does not bind an existing comparison seed"
        )

    expected_claim_ids = {
        group.claim_group_id for group in scope.claim_groups if not group.claim_seed
    }
    if set(canonical_claims) != expected_claim_ids:
        _error("canonical claim map must contain every and only non-seed scope group")

    source_refs = _normalize_refs(source_artifact_refs, "source_artifact_refs")
    binding = _scope_binding(scope)
    if not _inventory_ref_matches(binding, source_refs):
        _error(
            "source_artifact_refs must include the exact bound source inventory artifact"
        )

    entries: list[dict[str, Any]] = []
    for index, group in enumerate(
        sorted(scope.claim_groups, key=lambda item: item.claim_group_id)
    ):
        slots = sorted(
            (
                slot.to_dict()
                for slot in scope.claim_slots
                if slot.claim_group_id == group.claim_group_id
            ),
            key=lambda item: item["claim_slot_id"],
        )
        if not slots or {row["claim_slot_id"] for row in slots} != set(
            group.support_slot_ids
        ):
            _error(
                f"scope group {group.claim_group_id} does not contain its complete support slots"
            )
        if group.claim_seed:
            state, text, reason, artifact_refs, receipt_refs = (
                _comparison_materialization(
                    group, comparison_by_group.get(group.claim_group_id)
                )
            )
            if group.claim_group_id in required and state != "materialized":
                missing_required_seed.append(group.claim_group_id)
            entries.append(
                {
                    "index": index,
                    "claim_group": group.to_dict(),
                    "support_slots": slots,
                    "entry_kind": "comparison_seed",
                    "materialization_state": state,
                    "canonical_claim": None,
                    "text": text,
                    "canonical_payload_hash": None,
                    "seed_identity_hash": group.claim_hash,
                    "generated_text_hash": hash_text(text)
                    if text is not None
                    else None,
                    "deferral_reason_code": reason,
                    "comparison_artifact_refs": artifact_refs,
                    "provider_receipt_refs": receipt_refs,
                }
            )
        else:
            claim = canonical_claims[group.claim_group_id]
            if not isinstance(claim, Mapping):
                _error(f"canonical claim for {group.claim_group_id} must be an object")
            claim_dict = dict(claim)
            text = _require_text(
                claim_dict.get("text"), f"canonical claim {group.claim_group_id}.text"
            )
            if claim_dict.get("claim_id") != group.synthesis_claim_id:
                _error(
                    f"canonical claim {group.claim_group_id} has a foreign synthesis identity"
                )
            canonical_hash = hash_json(claim_dict)
            if canonical_hash != group.claim_hash:
                _error(
                    f"canonical claim {group.claim_group_id} hash differs from scope"
                )
            entries.append(
                {
                    "index": index,
                    "claim_group": group.to_dict(),
                    "support_slots": slots,
                    "entry_kind": "canonical_claim",
                    "materialization_state": "materialized",
                    "canonical_claim": claim_dict,
                    "text": text,
                    "canonical_payload_hash": canonical_hash,
                    "seed_identity_hash": None,
                    "generated_text_hash": None,
                    "deferral_reason_code": None,
                    "comparison_artifact_refs": [],
                    "provider_receipt_refs": [],
                }
            )
    if missing_required_seed:
        missing = sorted(set(missing_required_seed))[0]
        _error(
            f"required comparison {missing} has no materialized text and provider/artifact receipt closure"
        )

    payload: dict[str, Any] = {
        "schema_version": BANK_SCHEMA_VERSION,
        "scope_binding": binding,
        "source_artifact_refs": source_refs,
        "groups": entries,
    }
    _require_serializable(payload, "candidate fragment bank")
    payload["content_hash"] = hash_json(payload)
    validate_candidate_fragment_bank_intrinsic_v1(payload)
    return payload


def _validate_group_identity(group: Any, entry: Mapping[str, Any], index: int) -> None:
    raw = entry.get("claim_group")
    if not isinstance(raw, Mapping):
        _error(f"groups[{index}].claim_group must be an object")
    optional = {"claim_kind", "claim_seed", "task_contract"}
    if not _GROUP_KEYS.issubset(raw) or set(raw) - _GROUP_KEYS - optional:
        _error(f"groups[{index}].claim_group has unknown or missing identity fields")
    for key in (
        "claim_group_id",
        "task_id",
        "topic_id",
        "fragment_id",
        "provider_result_id",
        "synthesis_claim_id",
    ):
        if not isinstance(raw.get(key), str):
            _error(f"groups[{index}].claim_group.{key} must be a string")
    _require_hash(raw.get("claim_hash"), f"groups[{index}].claim_group.claim_hash")
    _unique_strings(
        raw.get("paper_keys"), f"groups[{index}].claim_group.paper_keys", required=True
    )
    _unique_strings(
        raw.get("support_slot_ids"),
        f"groups[{index}].claim_group.support_slot_ids",
        required=True,
    )
    _unique_strings(
        raw.get("relation_ids"), f"groups[{index}].claim_group.relation_ids"
    )
    is_seed = raw.get("claim_seed") is True
    if bool(raw.get("claim_seed")) != is_seed:
        _error(
            f"groups[{index}].claim_group.claim_seed must be boolean true when present"
        )
    if is_seed and raw.get("claim_kind") != "model_authored_relation_comparison_slot":
        _error(f"groups[{index}] has an unsupported comparison seed kind")
    if group is not None and raw.get("claim_group_id") != group.claim_group_id:
        _error(f"groups[{index}] group identity does not match the expected scope")


def validate_candidate_fragment_bank_intrinsic_v1(payload: Mapping[str, Any]) -> None:
    """Check content hashes, unique rows, exact text bindings, and ref shapes.

    This validator is deliberately Registry-free. A successful result means the
    payload is internally consistent; it does not establish that any referenced
    artifact or receipt is READY or authorized for publication.
    """

    if not isinstance(payload, Mapping) or set(payload) != _BANK_KEYS:
        _error("candidate fragment bank has unknown or missing top-level fields")
    _require_serializable(dict(payload), "candidate fragment bank")
    if payload.get("schema_version") != BANK_SCHEMA_VERSION:
        _error("candidate fragment bank has an unsupported schema version")
    binding = payload.get("scope_binding")
    if not isinstance(binding, Mapping) or set(binding) != _BANK_BINDING_KEYS:
        _error("candidate fragment bank scope binding has unknown or missing fields")
    for key in ("registry_job_id", "source_inventory_artifact_id"):
        _require_text(binding.get(key), f"scope_binding.{key}")
    _require_hash(
        binding.get("source_inventory_artifact_hash"),
        "scope_binding.source_inventory_artifact_hash",
    )
    _require_hash(
        binding.get("source_inventory_content_hash"),
        "scope_binding.source_inventory_content_hash",
    )
    _require_hash(binding.get("scope_hash"), "scope_binding.scope_hash")
    source_refs = _normalize_refs(
        payload.get("source_artifact_refs"), "source_artifact_refs"
    )
    if not _inventory_ref_matches(binding, source_refs):
        _error(
            "candidate fragment bank does not bind its source inventory artifact reference"
        )
    groups = payload.get("groups")
    if not _is_sequence(groups) or not groups:
        _error("candidate fragment bank requires nonempty groups")

    seen_group_ids: set[str] = set()
    seen_slot_ids: set[str] = set()
    for index, entry in enumerate(groups):
        if not isinstance(entry, Mapping) or set(entry) != _GROUP_ENTRY_KEYS:
            _error(f"groups[{index}] has unknown or missing fields")
        if type(entry.get("index")) is not int or entry["index"] != index:
            _error(f"groups[{index}].index is invalid")
        group = cast(Mapping[str, Any], entry.get("claim_group"))
        _validate_group_identity(None, entry, index)
        group_id = str(group["claim_group_id"])
        if group_id in seen_group_ids:
            _error(f"candidate fragment bank repeats group {group_id}")
        seen_group_ids.add(group_id)
        is_seed = group.get("claim_seed") is True
        if entry.get("entry_kind") != (
            "comparison_seed" if is_seed else "canonical_claim"
        ):
            _error(f"groups[{index}] entry kind differs from group origin")
        slots = cast(Sequence[Any], entry.get("support_slots"))
        if not _is_sequence(slots) or not slots:
            _error(f"groups[{index}] requires complete support slots")
        slot_ids: list[str] = []
        for slot_index, slot in enumerate(slots):
            if not isinstance(slot, Mapping) or set(slot) != _SLOT_KEYS:
                _error(
                    f"groups[{index}].support_slots[{slot_index}] has unknown or missing fields"
                )
            for name in (
                "claim_slot_id",
                "claim_group_id",
                "task_id",
                "topic_id",
                "fragment_id",
                "provider_result_id",
                "synthesis_claim_id",
                "paper_key",
                "primary_claim_id",
                "study_id",
            ):
                if not isinstance(slot.get(name), str):
                    _error(
                        f"groups[{index}].support_slots[{slot_index}].{name} must be a string"
                    )
            for name in ("source_claim_ids", "evidence_ids"):
                _unique_strings(
                    slot.get(name),
                    f"groups[{index}].support_slots[{slot_index}].{name}",
                    required=True,
                )
            for name in ("source_field_ids", "relation_ids"):
                _unique_strings(
                    slot.get(name),
                    f"groups[{index}].support_slots[{slot_index}].{name}",
                )
            slot_id = str(slot["claim_slot_id"])
            if slot_id in seen_slot_ids:
                _error(f"candidate fragment bank repeats support slot {slot_id}")
            seen_slot_ids.add(slot_id)
            slot_ids.append(slot_id)
            if slot.get("claim_group_id") != group_id:
                _error(f"groups[{index}] support slot belongs to another group")
        if set(slot_ids) != set(group.get("support_slot_ids") or ()) or len(
            slot_ids
        ) != len(group.get("support_slot_ids") or ()):
            _error(f"groups[{index}] support slots differ from its group identity")
        slot_papers = {str(slot["paper_key"]) for slot in slots}
        if slot_papers != set(group.get("paper_keys") or ()):
            _error(f"groups[{index}] slot paper ownership differs from group identity")
        if is_seed:
            _validate_comparison_seed_identity(group, slots, f"groups[{index}]")

        state = entry.get("materialization_state")
        text = entry.get("text")
        if not isinstance(state, str) or state not in {"materialized", "unresolved"}:
            _error(f"groups[{index}] has an invalid materialization state")
        if is_seed:
            if (
                entry.get("canonical_claim") is not None
                or entry.get("canonical_payload_hash") is not None
            ):
                _error(
                    f"groups[{index}] comparison seed carries a canonical claim payload"
                )
            if entry.get("seed_identity_hash") != group.get("claim_hash"):
                _error(
                    f"groups[{index}] seed identity hash differs from its scope seed"
                )
            if state == "materialized":
                exact_text = _require_text(text, f"groups[{index}].text")
                if entry.get("generated_text_hash") != hash_text(exact_text):
                    _error(
                        f"groups[{index}] generated text hash differs from exact comparison text"
                    )
                if entry.get("deferral_reason_code") is not None:
                    _error(
                        f"groups[{index}] materialized comparison has a deferral reason"
                    )
                if not _normalize_refs(
                    entry.get("comparison_artifact_refs"),
                    f"groups[{index}].comparison_artifact_refs",
                ):
                    _error(
                        f"groups[{index}] materialized comparison lacks an artifact reference"
                    )
                if not _normalize_refs(
                    entry.get("provider_receipt_refs"),
                    f"groups[{index}].provider_receipt_refs",
                    allow_receipt_id=True,
                ):
                    _error(
                        f"groups[{index}] materialized comparison lacks a provider receipt reference"
                    )
            else:
                if text is not None or entry.get("generated_text_hash") is not None:
                    _error(f"groups[{index}] unresolved comparison carries text")
                if entry.get("deferral_reason_code") != "optional_unresolved":
                    _error(
                        f"groups[{index}] unresolved comparison lacks optional_unresolved reason"
                    )
                if entry.get("comparison_artifact_refs") or entry.get(
                    "provider_receipt_refs"
                ):
                    _error(
                        f"groups[{index}] unresolved comparison carries materialization references"
                    )
        else:
            if state != "materialized" or entry.get("deferral_reason_code") is not None:
                _error(
                    f"groups[{index}] canonical claim cannot be unresolved or deferred"
                )
            canonical = entry.get("canonical_claim")
            if not isinstance(canonical, Mapping):
                _error(f"groups[{index}] canonical claim payload is required")
            exact_text = _require_text(
                canonical.get("text"), f"groups[{index}].canonical_claim.text"
            )
            if text != exact_text:
                _error(
                    f"groups[{index}] canonical text differs from its exact claim payload"
                )
            if canonical.get("claim_id") != group.get("synthesis_claim_id"):
                _error(
                    f"groups[{index}] canonical claim identity differs from its origin group"
                )
            canonical_hash = hash_json(dict(canonical))
            if (
                canonical_hash != group.get("claim_hash")
                or entry.get("canonical_payload_hash") != canonical_hash
            ):
                _error(
                    f"groups[{index}] canonical payload hash differs from source lineage"
                )
            if (
                entry.get("seed_identity_hash") is not None
                or entry.get("generated_text_hash") is not None
            ):
                _error(f"groups[{index}] canonical claim uses a comparison-only hash")
            if entry.get("comparison_artifact_refs") or entry.get(
                "provider_receipt_refs"
            ):
                _error(
                    f"groups[{index}] canonical claim carries comparison materialization references"
                )

    expected_order = sorted(seen_group_ids)
    observed_order = [str(entry["claim_group"]["claim_group_id"]) for entry in groups]
    if observed_order != expected_order:
        _error("candidate fragment bank groups must use stable group identity order")
    expected_hash = hash_json(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )
    if payload.get("content_hash") != expected_hash:
        _error("candidate fragment bank content hash does not match its exact payload")


def validate_candidate_fragment_bank_v1(
    payload: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
) -> None:
    """Validate intrinsic bank integrity and exact complete scope closure."""

    validate_candidate_fragment_bank_intrinsic_v1(payload)
    if type(scope) is not CandidateOutputScopeV1:
        _error("scope must be a CandidateOutputScopeV1")
    binding = payload["scope_binding"]
    if dict(binding) != _scope_binding(scope):
        _error("candidate fragment bank is bound to a different output scope")
    groups_by_id = {group.claim_group_id: group for group in scope.claim_groups}
    entries_by_id = {
        str(entry["claim_group"]["claim_group_id"]): entry
        for entry in payload["groups"]
    }
    if set(entries_by_id) != set(groups_by_id):
        _error(
            "candidate fragment bank group set differs from the complete output scope"
        )
    scope_slots_by_group: dict[str, list[dict[str, Any]]] = {}
    for slot in scope.claim_slots:
        scope_slots_by_group.setdefault(slot.claim_group_id, []).append(slot.to_dict())
    for group_id, group in groups_by_id.items():
        entry = entries_by_id[group_id]
        if entry["claim_group"] != group.to_dict():
            _error(
                f"candidate fragment bank group identity differs from scope for {group_id}"
            )
        expected_slots = sorted(
            scope_slots_by_group.get(group_id, ()),
            key=lambda item: item["claim_slot_id"],
        )
        if entry["support_slots"] != expected_slots:
            _error(
                f"candidate fragment bank support slots differ from complete scope for {group_id}"
            )


def _require_binding_hash(value: Any, label: str) -> str:
    return _require_hash(value, label)


def _build_candidate_organization_request_from_bank_v1(
    bank: Mapping[str, Any],
    *,
    axis: Any,
    plan: Any,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    metadata_catalog: Sequence[Mapping[str, Any]],
    partition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the Registry-free compact request projection from an intact bank."""

    validate_candidate_fragment_bank_intrinsic_v1(bank)
    if partition is not None:
        descriptor = _organization_partition_payload(
            partition,
            bank_hash=str(bank["content_hash"]),
            full_bank_group_count=len(bank["groups"]),
        )
        return _build_partitioned_candidate_organization_request_from_bank_v1(
            bank,
            axis=axis,
            plan=plan,
            required_group_ids=required_group_ids,
            required_paper_keys=required_paper_keys,
            metadata_catalog=metadata_catalog,
            partition=descriptor,
        )
    candidate_id, axis_payload, plan_payload, axis_hash, plan_hash = (
        _organization_semantics(axis, plan)
    )
    catalog = _metadata_catalog_payload(metadata_catalog)
    metadata_catalog_hash = hash_json(catalog)
    required_group_indices, required_paper_indices = _required_index_binding(
        bank, required_group_ids, required_paper_keys
    )
    for index in required_group_indices:
        if bank["groups"][index]["materialization_state"] != "materialized":
            _error(
                f"required group {bank['groups'][index]['claim_group']['claim_group_id']} is unresolved before organization"
            )
    materialized_papers = {
        str(slot["paper_key"])
        for entry in bank["groups"]
        if entry["materialization_state"] == "materialized"
        for slot in entry["support_slots"]
    }
    paper_keys = sorted(
        {
            str(slot["paper_key"])
            for entry in bank["groups"]
            for slot in entry["support_slots"]
        }
    )
    for paper_index in required_paper_indices:
        required_paper = paper_keys[paper_index]
        if required_paper not in materialized_papers:
            _error(
                f"required paper {required_paper} has no materialized complete group before organization"
            )
    paper_keys = sorted(
        {
            str(slot["paper_key"])
            for entry in bank["groups"]
            for slot in entry["support_slots"]
        }
    )
    paper_index = {paper_key: index for index, paper_key in enumerate(paper_keys)}
    text_pool: list[str] = []
    text_index: dict[str, int] = {}
    task_refs: list[str] = []
    task_index: dict[str, int] = {}
    topic_refs: list[str] = []
    topic_index: dict[str, int] = {}
    fragment_cards: list[list[Any]] = []
    for entry in bank["groups"]:
        group = entry["claim_group"]
        index = entry["index"]
        exact_text = entry["text"]
        if exact_text is None:
            text_ref = None
        else:
            if exact_text not in text_index:
                text_index[exact_text] = len(text_pool)
                text_pool.append(exact_text)
            text_ref = text_index[exact_text]
        task_id = str(group["task_id"])
        if task_id not in task_index:
            task_index[task_id] = len(task_refs)
            task_refs.append(task_id)
        topic_id = str(group["topic_id"])
        if topic_id:
            if topic_id not in topic_index:
                topic_index[topic_id] = len(topic_refs)
                topic_refs.append(topic_id)
            topic_ref: int | None = topic_index[topic_id]
        else:
            topic_ref = None
        fragment_cards.append(
            [
                index,
                text_ref,
                sorted({paper_index[key] for key in group["paper_keys"]}),
                entry["materialization_state"],
                task_index[task_id],
                topic_ref,
            ]
        )
    required_comparison_sections: list[dict[str, Any]] = []
    for index in required_group_indices:
        group = bank["groups"][index]["claim_group"]
        if not group.get("claim_seed"):
            continue
        question = group["task_contract"]["comparison_question"]
        metadata_index = next(
            (i for i, row in enumerate(catalog) if row["goal"] == question), None
        )
        if metadata_index is None:
            _error(
                "trusted metadata catalog omits the exact required comparison question"
            )
        required_comparison_sections.append(
            {
                "group_index": index,
                "metadata_index": metadata_index,
                "paper_indices": fragment_cards[index][2],
            }
        )
    return {
        "schema_version": ORGANIZATION_REQUEST_SCHEMA_VERSION,
        "bank_schema_version": BANK_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "axis_payload": axis_payload,
        "plan_payload": plan_payload,
        "axis_hash": axis_hash,
        "plan_hash": plan_hash,
        "bank_hash": str(bank["content_hash"]),
        "bank_group_count": len(bank["groups"]),
        "map_schema_hash": ORGANIZATION_MAP_SCHEMA_HASH,
        "metadata_catalog_schema_version": METADATA_CATALOG_SCHEMA_VERSION,
        "metadata_catalog_contract_hash": METADATA_CATALOG_CONTRACT_HASH,
        "metadata_catalog": catalog,
        "metadata_catalog_hash": metadata_catalog_hash,
        "required_group_indices": required_group_indices,
        "required_paper_indices": required_paper_indices,
        "required_comparison_sections": required_comparison_sections,
        "organization_contract_hash": _organization_contract_hash(
            candidate_id=candidate_id,
            axis_hash=axis_hash,
            plan_hash=plan_hash,
            bank_hash=str(bank["content_hash"]),
            metadata_catalog_hash=metadata_catalog_hash,
            required_group_indices=required_group_indices,
            required_paper_indices=required_paper_indices,
            required_comparison_sections=required_comparison_sections,
        ),
        "paper_keys": paper_keys,
        "text_pool": text_pool,
        "task_refs": task_refs,
        "topic_refs": topic_refs,
        "card_fields": list(_ORGANIZATION_CARD_FIELDS),
        "fragment_cards": fragment_cards,
        "organization_map_contract": _organization_map_contract(),
    }


def _build_partitioned_candidate_organization_request_from_bank_v1(
    bank: Mapping[str, Any],
    *,
    axis: Any,
    plan: Any,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    metadata_catalog: Sequence[Mapping[str, Any]],
    partition: Mapping[str, Any],
) -> dict[str, Any]:
    """Build local wire tables while retaining the full shared bank identity."""

    candidate_id, axis_payload, plan_payload, axis_hash, plan_hash = (
        _organization_semantics(axis, plan)
    )
    catalog = _metadata_catalog_payload(metadata_catalog)
    metadata_catalog_hash = hash_json(catalog)
    global_indices = list(partition["local_to_global_group_indices"])
    entries = [bank["groups"][index] for index in global_indices]
    local_group_index = {
        str(entry["claim_group"]["claim_group_id"]): index
        for index, entry in enumerate(entries)
    }
    required_ids = _unique_strings(required_group_ids, "required_group_ids")
    missing_groups = sorted(set(required_ids) - set(local_group_index))
    if missing_groups:
        _error(
            f"required group {missing_groups[0]} is absent from this organization leaf"
        )
    required_group_indices = sorted(
        local_group_index[group_id] for group_id in required_ids
    )
    for local_index in required_group_indices:
        if entries[local_index]["materialization_state"] != "materialized":
            group_id = str(entries[local_index]["claim_group"]["claim_group_id"])
            _error(f"required group {group_id} is unresolved before organization")

    paper_keys = sorted(
        {str(slot["paper_key"]) for entry in entries for slot in entry["support_slots"]}
    )
    paper_index = {paper_key: index for index, paper_key in enumerate(paper_keys)}
    required_papers = _unique_strings(required_paper_keys, "required_paper_keys")
    missing_papers = sorted(set(required_papers) - set(paper_index))
    if missing_papers:
        _error(
            f"required paper {missing_papers[0]} is absent from this organization leaf"
        )
    required_paper_indices = sorted(
        paper_index[paper_key] for paper_key in required_papers
    )
    materialized_papers = {
        str(slot["paper_key"])
        for entry in entries
        if entry["materialization_state"] == "materialized"
        for slot in entry["support_slots"]
    }
    for paper_key in required_papers:
        if paper_key not in materialized_papers:
            _error(
                f"required paper {paper_key} has no materialized complete witness in this organization leaf"
            )

    text_pool: list[str] = []
    text_index: dict[str, int] = {}
    task_refs: list[str] = []
    task_index: dict[str, int] = {}
    topic_refs: list[str] = []
    topic_index: dict[str, int] = {}
    fragment_cards: list[list[Any]] = []
    for local_index, entry in enumerate(entries):
        group = entry["claim_group"]
        exact_text = entry["text"]
        if exact_text is None:
            text_ref = None
        else:
            if exact_text not in text_index:
                text_index[exact_text] = len(text_pool)
                text_pool.append(exact_text)
            text_ref = text_index[exact_text]
        task_id = str(group["task_id"])
        if task_id not in task_index:
            task_index[task_id] = len(task_refs)
            task_refs.append(task_id)
        topic_id = str(group["topic_id"])
        if topic_id:
            if topic_id not in topic_index:
                topic_index[topic_id] = len(topic_refs)
                topic_refs.append(topic_id)
            topic_ref: int | None = topic_index[topic_id]
        else:
            topic_ref = None
        fragment_cards.append(
            [
                local_index,
                text_ref,
                sorted({paper_index[key] for key in group["paper_keys"]}),
                entry["materialization_state"],
                task_index[task_id],
                topic_ref,
            ]
        )

    required_comparison_sections: list[dict[str, Any]] = []
    for local_index in required_group_indices:
        group = entries[local_index]["claim_group"]
        if not group.get("claim_seed"):
            continue
        question = group["task_contract"]["comparison_question"]
        metadata_index = next(
            (index for index, row in enumerate(catalog) if row["goal"] == question),
            None,
        )
        if metadata_index is None:
            _error(
                "trusted metadata catalog omits the exact required comparison question"
            )
        required_comparison_sections.append(
            {
                "group_index": local_index,
                "metadata_index": metadata_index,
                "paper_indices": fragment_cards[local_index][2],
            }
        )

    bank_hash = str(bank["content_hash"])
    partition_hash = hash_json(dict(partition))
    request = {
        "schema_version": ORGANIZATION_REQUEST_SCHEMA_VERSION,
        "bank_schema_version": BANK_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "axis_payload": axis_payload,
        "plan_payload": plan_payload,
        "axis_hash": axis_hash,
        "plan_hash": plan_hash,
        "bank_hash": bank_hash,
        "bank_group_count": len(entries),
        "map_schema_hash": ORGANIZATION_MAP_SCHEMA_HASH,
        "metadata_catalog_schema_version": METADATA_CATALOG_SCHEMA_VERSION,
        "metadata_catalog_contract_hash": METADATA_CATALOG_CONTRACT_HASH,
        "metadata_catalog": catalog,
        "metadata_catalog_hash": metadata_catalog_hash,
        "required_group_indices": required_group_indices,
        "required_paper_indices": required_paper_indices,
        "required_comparison_sections": required_comparison_sections,
        "organization_contract_hash": _organization_contract_hash(
            candidate_id=candidate_id,
            axis_hash=axis_hash,
            plan_hash=plan_hash,
            bank_hash=bank_hash,
            metadata_catalog_hash=metadata_catalog_hash,
            required_group_indices=required_group_indices,
            required_paper_indices=required_paper_indices,
            required_comparison_sections=required_comparison_sections,
            partition_hash=partition_hash,
        ),
        "paper_keys": paper_keys,
        "text_pool": text_pool,
        "task_refs": task_refs,
        "topic_refs": topic_refs,
        "card_fields": list(_ORGANIZATION_CARD_FIELDS),
        "fragment_cards": fragment_cards,
        "organization_map_contract": _organization_map_contract(),
        "partition": dict(partition),
    }
    _require_serializable(request, "partitioned organization request")
    return request


def build_candidate_organization_request_v1(
    bank: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
    *,
    axis: Any,
    plan: Any,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    metadata_catalog: Sequence[Mapping[str, Any]],
    partition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create compact cards after verifying the exact full source scope."""

    validate_candidate_fragment_bank_v1(bank, scope)
    if partition is not None:
        descriptor = _organization_partition_payload(
            partition,
            bank_hash=str(bank["content_hash"]),
            full_bank_group_count=len(bank["groups"]),
        )
        _validate_organization_partition_scope(descriptor, bank, scope)
    else:
        descriptor = None
    return _build_candidate_organization_request_from_bank_v1(
        bank,
        axis=axis,
        plan=plan,
        required_group_ids=required_group_ids,
        required_paper_keys=required_paper_keys,
        metadata_catalog=metadata_catalog,
        partition=descriptor,
    )


def _request_index_array(
    value: Any,
    *,
    upper_bound: int,
    label: str,
    require_sorted: bool = False,
    allow_empty: bool = False,
) -> list[int]:
    if not _is_sequence(value) or (not value and not allow_empty):
        _error(f"{label} must be a nonempty index array")
    indices: list[int] = []
    for index in value:
        if type(index) is not int or index < 0 or index >= upper_bound:
            _error(f"{label} contains an invalid index")
        indices.append(index)
    if len(set(indices)) != len(indices):
        _error(f"{label} must contain unique indices")
    if require_sorted and indices != sorted(indices):
        _error(f"{label} must contain sorted indices")
    return indices


def _validate_candidate_organization_request_v1(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        _error("organization request has unknown or missing fields")
    partitioned = "partition" in request
    expected_keys = _ORGANIZATION_REQUEST_KEYS | (
        {"partition"} if partitioned else set()
    )
    if set(request) != expected_keys:
        _error("organization request has unknown or missing fields")
    _require_serializable(dict(request), "organization request")
    if request.get("schema_version") != ORGANIZATION_REQUEST_SCHEMA_VERSION:
        _error("organization request has an unsupported schema version")
    if request.get("bank_schema_version") != BANK_SCHEMA_VERSION:
        _error("organization request has an unsupported bank schema version")
    if (
        request.get("metadata_catalog_schema_version")
        != METADATA_CATALOG_SCHEMA_VERSION
    ):
        _error(
            "organization request has an unsupported metadata catalog schema version"
        )
    if request.get("map_schema_hash") != ORGANIZATION_MAP_SCHEMA_HASH:
        _error("organization request map schema hash is invalid")
    if request.get("metadata_catalog_contract_hash") != METADATA_CATALOG_CONTRACT_HASH:
        _error("organization request metadata catalog contract hash is invalid")
    if request.get("organization_map_contract") != _organization_map_contract():
        _error("organization request map contract differs from the current contract")
    if request.get("card_fields") != _ORGANIZATION_CARD_FIELDS:
        _error(
            "organization request card fields differ from the positional card contract"
        )

    candidate_id, _axis_payload, _plan_payload, axis_hash, plan_hash = (
        _organization_semantics(
            request.get("axis_payload"), request.get("plan_payload")
        )
    )
    if request.get("candidate_id") != candidate_id:
        _error("organization request candidate_id differs from its plan payload")
    if request.get("axis_hash") != axis_hash or request.get("plan_hash") != plan_hash:
        _error("organization request semantic payload hashes are invalid")
    for name in (
        "bank_hash",
        "axis_hash",
        "plan_hash",
        "metadata_catalog_hash",
        "organization_contract_hash",
    ):
        _require_hash(request.get(name), f"organization request.{name}")

    catalog = _metadata_catalog_payload(request.get("metadata_catalog"))
    metadata_catalog_hash = hash_json(catalog)
    if request.get("metadata_catalog_hash") != metadata_catalog_hash:
        _error("organization request metadata catalog hash is invalid")
    paper_keys = _unique_strings(
        request.get("paper_keys"), "organization request.paper_keys", required=True
    )
    if paper_keys != sorted(paper_keys):
        _error("organization request paper table is not in canonical order")
    text_pool = request.get("text_pool")
    if not _is_sequence(text_pool):
        _error("organization request text_pool must be an array")
    if any(not isinstance(text, str) or not text.strip() for text in text_pool):
        _error("organization request text_pool contains an empty or non-string value")
    if len(set(text_pool)) != len(text_pool):
        _error("organization request text_pool must intern exact repeated text")
    task_refs = _unique_strings(
        request.get("task_refs"), "organization request.task_refs", required=True
    )
    topic_refs = _unique_strings(
        request.get("topic_refs"), "organization request.topic_refs"
    )

    group_count = request.get("bank_group_count")
    cards = request.get("fragment_cards")
    if (
        type(group_count) is not int
        or group_count < 1
        or not _is_sequence(cards)
        or len(cards) != group_count
    ):
        _error(
            "organization request bank group count differs from its positional cards"
        )
    if partitioned:
        raw_partition = request.get("partition")
        if not isinstance(raw_partition, Mapping):
            _error("organization request partition descriptor must be an object")
        full_count = raw_partition.get("full_bank_group_count")
        if type(full_count) is not int or full_count < 1:
            _error(
                "organization request partition descriptor has an invalid full bank count"
            )
        partition = _organization_partition_payload(
            raw_partition,
            bank_hash=str(request["bank_hash"]),
            full_bank_group_count=full_count,
        )
        if len(partition["local_to_global_group_indices"]) != group_count:
            _error(
                "organization request partition index count differs from local cards"
            )
        if partition != dict(request["partition"]):
            _error("organization request partition descriptor is not canonical")
    else:
        partition = None
    observed_text_refs: set[int] = set()
    for index, card in enumerate(cards):
        if not _is_sequence(card) or len(card) != len(_ORGANIZATION_CARD_FIELDS):
            _error(
                f"organization request fragment card {index} has an invalid positional shape"
            )
        (
            fragment_index,
            text_index,
            paper_indices,
            state,
            task_ref_index,
            topic_ref_index,
        ) = card
        if type(fragment_index) is not int or fragment_index != index:
            _error(
                f"organization request fragment card {index} has an invalid bank index"
            )
        if state not in {"materialized", "unresolved"}:
            _error(
                f"organization request fragment card {index} has an invalid materialization state"
            )
        if state == "unresolved":
            if text_index is not None:
                _error(
                    f"organization request unresolved card {index} carries a text index"
                )
        else:
            if (
                type(text_index) is not int
                or text_index < 0
                or text_index >= len(text_pool)
            ):
                _error(
                    f"organization request materialized card {index} has an invalid text index"
                )
            observed_text_refs.add(text_index)
        paper_indices = _request_index_array(
            paper_indices,
            upper_bound=len(paper_keys),
            label=f"organization request card {index}.paper_indices",
            require_sorted=True,
        )
        if (
            type(task_ref_index) is not int
            or task_ref_index < 0
            or task_ref_index >= len(task_refs)
        ):
            _error(f"organization request card {index} has an invalid task reference")
        if topic_ref_index is not None and (
            type(topic_ref_index) is not int
            or topic_ref_index < 0
            or topic_ref_index >= len(topic_refs)
        ):
            _error(f"organization request card {index} has an invalid topic reference")
    if observed_text_refs != set(range(len(text_pool))):
        _error("organization request text_pool contains unused entries")

    required_group_indices = _request_index_array(
        request.get("required_group_indices"),
        upper_bound=group_count,
        label="organization request.required_group_indices",
        require_sorted=True,
        allow_empty=True,
    )
    required_paper_indices = _request_index_array(
        request.get("required_paper_indices"),
        upper_bound=len(paper_keys),
        label="organization request.required_paper_indices",
        require_sorted=True,
        allow_empty=True,
    )
    for group_index in required_group_indices:
        if cards[group_index][3] != "materialized":
            _error(
                f"organization request required group index {group_index} is unresolved"
            )
    materialized_papers = {
        paper_index
        for card in cards
        if card[3] == "materialized"
        for paper_index in card[2]
    }
    if not set(required_paper_indices).issubset(materialized_papers):
        _error(
            "organization request has a required paper without a materialized complete witness"
        )
    required_comparisons = request.get("required_comparison_sections")
    if not _is_sequence(required_comparisons):
        _error("organization request required comparison sections must be an array")
    comparison_indices: set[int] = set()
    for row in required_comparisons:
        if not isinstance(row, Mapping) or set(row) != {
            "group_index",
            "metadata_index",
            "paper_indices",
        }:
            _error(
                "organization request required comparison section has unknown or missing fields"
            )
        group_index, metadata_index = row["group_index"], row["metadata_index"]
        if (
            type(group_index) is not int
            or group_index not in required_group_indices
            or group_index in comparison_indices
            or type(metadata_index) is not int
            or not 0 <= metadata_index < len(catalog)
        ):
            _error(
                "organization request required comparison section has invalid indices"
            )
        if (
            row["paper_indices"] != cards[group_index][2]
            or len(row["paper_indices"]) != 2
        ):
            _error(
                "organization request required comparison section has a foreign source pair"
            )
        comparison_indices.add(group_index)
    expected_contract_hash = _organization_contract_hash(
        candidate_id=candidate_id,
        axis_hash=axis_hash,
        plan_hash=plan_hash,
        bank_hash=str(request["bank_hash"]),
        metadata_catalog_hash=metadata_catalog_hash,
        required_group_indices=required_group_indices,
        required_paper_indices=required_paper_indices,
        required_comparison_sections=required_comparisons,
        partition_hash=hash_json(partition) if partition is not None else None,
    )
    if request.get("organization_contract_hash") != expected_contract_hash:
        _error("organization request contract hash is invalid")
    return {
        "candidate_id": candidate_id,
        "axis_hash": axis_hash,
        "plan_hash": plan_hash,
        "bank_hash": str(request["bank_hash"]),
        "metadata_catalog_hash": metadata_catalog_hash,
        "required_group_indices": required_group_indices,
        "required_paper_indices": required_paper_indices,
        "required_comparison_sections": required_comparisons,
        "organization_contract_hash": expected_contract_hash,
        "paper_keys": paper_keys,
        "text_pool": list(text_pool),
        "metadata_catalog": catalog,
        "fragment_cards": cards,
        "partition": partition,
        "local_to_global_group_indices": (
            list(partition["local_to_global_group_indices"])
            if partition is not None
            else list(range(group_count))
        ),
    }


def validate_candidate_organization_map_for_request_v1(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    """Validate a raw response against its exact request projection.

    This intrinsic check grants no Registry or READY-source authority. It lets a
    Registry artifact validator check the stored raw response using its exact
    `provider_request` dependency without reconstructing a fragment bank.
    """

    view = _validate_candidate_organization_request_v1(request)
    if not isinstance(payload, Mapping) or set(payload) != _ORGANIZATION_MAP_KEYS:
        _error("organization map has unknown or missing top-level fields")
    _require_serializable(dict(payload), "organization map")
    if payload.get("schema_version") != ORGANIZATION_MAP_SCHEMA_VERSION:
        _error("organization map has an unsupported schema version")
    bindings = (
        ("candidate_id", view["candidate_id"]),
        ("axis_hash", view["axis_hash"]),
        ("plan_hash", view["plan_hash"]),
        ("bank_hash", view["bank_hash"]),
        ("map_schema_hash", ORGANIZATION_MAP_SCHEMA_HASH),
        ("metadata_catalog_hash", view["metadata_catalog_hash"]),
        ("metadata_catalog_contract_hash", METADATA_CATALOG_CONTRACT_HASH),
        ("organization_contract_hash", view["organization_contract_hash"]),
    )
    for name, expected in bindings:
        observed = payload.get(name)
        if name.endswith("_hash"):
            _require_hash(observed, f"organization map.{name}")
        else:
            _require_text(observed, f"organization map.{name}")
        if observed != expected:
            _error(f"organization map {name} differs from its request binding")
    if payload.get("required_group_indices") != view["required_group_indices"]:
        _error("organization map required_group_indices differ from must-use coverage")
    if payload.get("required_paper_indices") != view["required_paper_indices"]:
        _error("organization map required_paper_indices differ from must-use coverage")

    cards: Sequence[Sequence[Any]] = view["fragment_cards"]
    paper_keys: list[str] = view["paper_keys"]
    text_pool: list[str] = view["text_pool"]
    catalog: list[dict[str, str]] = view["metadata_catalog"]
    entry_count = len(cards)
    sections = payload.get("sections")
    empty_partition_leaf = (
        view["partition"] is not None
        and not view["required_group_indices"]
        and not view["required_paper_indices"]
    )
    if not _is_sequence(sections) or (not sections and not empty_partition_leaf):
        _error("organization map requires nonempty sections")
    used: set[int] = set()
    section_indices: set[int] = set()
    role_values_by_paper: dict[int, set[str]] = {}
    assignments_by_paper: dict[int, int] = {}
    for section_index, section in enumerate(sections):
        if (
            not isinstance(section, Mapping)
            or set(section) != _ORGANIZATION_SECTION_KEYS
        ):
            _error(
                f"organization section {section_index} has unknown or missing fields"
            )
        raw_section_index = section.get("section_index")
        if (
            type(raw_section_index) is not int
            or raw_section_index < 0
            or raw_section_index in section_indices
        ):
            _error("organization map has an invalid or duplicate section_index")
        section_indices.add(raw_section_index)
        metadata_index = section.get("metadata_index")
        if (
            type(metadata_index) is not int
            or metadata_index < 0
            or metadata_index >= len(catalog)
        ):
            _error(f"section {raw_section_index} has an invalid trusted metadata index")
        local_indices = _request_index_array(
            section.get("fragment_indices"),
            upper_bound=entry_count,
            label=f"section {raw_section_index}.fragment_indices",
        )
        if any(index in used for index in local_indices):
            _error(f"section {raw_section_index} repeats or reuses a fragment index")
        if any(cards[index][3] != "materialized" for index in local_indices):
            _error(f"section {raw_section_index} selects an unresolved fragment")
        used.update(local_indices)
        expected_papers = {
            paper_index
            for fragment_index in local_indices
            for paper_index in cards[fragment_index][2]
        }
        for comparison in view["required_comparison_sections"]:
            if comparison["group_index"] in local_indices and (
                metadata_index != comparison["metadata_index"]
                or expected_papers != set(comparison["paper_indices"])
            ):
                _error(
                    "required comparison section lost its exact question metadata or source pair"
                )

        roles = section.get("paper_roles")
        if not _is_sequence(roles):
            _error(
                f"section {raw_section_index}.paper_roles must be an array of indexed pairs"
            )
        role_papers: set[int] = set()
        for role_index, role in enumerate(roles):
            if not _is_sequence(role) or len(role) != len(_ORGANIZATION_ROLE_FIELDS):
                _error(
                    f"section {raw_section_index}.paper_roles[{role_index}] must be a two-index pair"
                )
            paper_index, role_fragment_index = role
            if (
                type(paper_index) is not int
                or paper_index < 0
                or paper_index >= len(paper_keys)
            ):
                _error(
                    f"section {raw_section_index}.paper_roles[{role_index}] has an invalid paper index"
                )
            if paper_index in role_papers:
                _error(f"section {raw_section_index}.paper_roles repeats a paper index")
            role_papers.add(paper_index)
            if (
                type(role_fragment_index) is not int
                or role_fragment_index not in local_indices
                or role_fragment_index < 0
                or role_fragment_index >= entry_count
            ):
                _error(
                    f"section {raw_section_index}.paper_roles[{role_index}] has an unselected role fragment"
                )
            card = cards[role_fragment_index]
            if card[3] != "materialized" or card[1] is None:
                _error(
                    f"section {raw_section_index}.paper_roles[{role_index}] references unresolved text"
                )
            if paper_index not in card[2]:
                _error(
                    f"section {raw_section_index}.paper_roles[{role_index}] fragment is not source-owned by its paper"
                )
            role_text = text_pool[card[1]]
            role_values_by_paper.setdefault(paper_index, set()).add(
                role_text.casefold()
            )
        if role_papers != expected_papers:
            _error(
                f"section {raw_section_index}.paper_roles must name exactly its supported paper members"
            )
        for paper_index in expected_papers:
            assignments_by_paper[paper_index] = (
                assignments_by_paper.get(paper_index, 0) + 1
            )

    deferred = payload.get("deferred")
    if not _is_sequence(deferred):
        _error("organization map deferred must be an array")
    deferred_indices: set[int] = set()
    for group_index, item in enumerate(deferred):
        if not isinstance(item, Mapping) or set(item) != _ORGANIZATION_DEFERRED_KEYS:
            _error(
                f"organization map deferred[{group_index}] has unknown or missing fields"
            )
        reason = item.get("reason_code")
        if not isinstance(reason, str) or reason not in _DEFERRAL_CODES:
            _error(
                f"organization map deferred[{group_index}] has an unsupported reason code"
            )
        indices = _request_index_array(
            item.get("fragment_indices"),
            upper_bound=entry_count,
            label=f"organization map deferred[{group_index}].fragment_indices",
        )
        if any(index in deferred_indices or index in used for index in indices):
            _error("organization map repeats or overlaps a deferred fragment index")
        for index in indices:
            deferred_indices.add(index)
            state = cards[index][3]
            if state == "unresolved" and reason != "optional_unresolved":
                _error(
                    "unresolved optional fragments require the optional_unresolved reason code"
                )
            if state == "materialized" and reason == "optional_unresolved":
                _error("materialized fragments cannot be deferred as unresolved")
    if used | deferred_indices != set(range(entry_count)):
        _error(
            "organization map used and deferred indices must partition the complete bank"
        )
    if not set(view["required_group_indices"]).issubset(used):
        _error("organization map omits a required group index")
    selected_papers = {
        paper_index
        for fragment_index in used
        for paper_index in cards[fragment_index][2]
    }
    if not set(view["required_paper_indices"]).issubset(selected_papers):
        _error("organization map omits a required paper witness")
    for paper_index, assignment_count in assignments_by_paper.items():
        if (
            assignment_count > 1
            and len(role_values_by_paper.get(paper_index, set())) < assignment_count
        ):
            _error(
                f"organization map repeats a paper role across sections for {paper_keys[paper_index]}"
            )


def validate_candidate_organization_map_v1(
    payload: Mapping[str, Any],
    bank: Mapping[str, Any],
    *,
    axis: Any,
    plan: Any,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    metadata_catalog: Sequence[Mapping[str, Any]],
    partition: Mapping[str, Any] | None = None,
) -> None:
    """Validate raw organization against its bank-derived request projection.

    This request/bank consistency check does not establish Registry READY
    authority; use the full-scope candidate expansion path before publication.
    """

    request = _build_candidate_organization_request_from_bank_v1(
        bank,
        axis=axis,
        plan=plan,
        required_group_ids=required_group_ids,
        required_paper_keys=required_paper_keys,
        metadata_catalog=metadata_catalog,
        partition=partition,
    )
    validate_candidate_organization_map_for_request_v1(payload, request)


def compile_candidate_organization_section_v1(
    bank: Mapping[str, Any],
    *,
    candidate_id: str,
    section_index: int,
    metadata_index: int,
    global_fragment_indices: Sequence[int],
    paper_roles: Sequence[Sequence[Any]],
    metadata_catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compile one trusted section from exact bank text and complete slot rows.

    The caller supplies global bank indices (including after a leaf-map merge).
    This helper authors no claims or roles and does not validate Registry
    closure; callers validate the complete candidate against the full scope.
    """

    validate_candidate_fragment_bank_intrinsic_v1(bank)
    candidate_id = _require_text(candidate_id, "candidate_id")
    if type(section_index) is not int or section_index < 0:
        _error("compiled section_index must be a nonnegative integer")
    catalog = _metadata_catalog_payload(metadata_catalog)
    if (
        type(metadata_index) is not int
        or metadata_index < 0
        or metadata_index >= len(catalog)
    ):
        _error("compiled section metadata_index is invalid")
    if not _is_sequence(global_fragment_indices) or not global_fragment_indices:
        _error("compiled section requires global fragment indices")
    global_indices = _request_index_array(
        global_fragment_indices,
        upper_bound=len(bank["groups"]),
        label="compiled global_fragment_indices",
    )
    if not _is_sequence(paper_roles):
        _error("compiled section paper_roles must be an array")

    claims: list[str] = []
    supports: list[dict[str, Any]] = []
    member_papers: set[str] = set()
    task_ids: set[str] = set()
    relation_ids: set[str] = set()
    for global_index in global_indices:
        entry = bank["groups"][global_index]
        if entry["materialization_state"] != "materialized":
            _error("compiled section selected an unresolved fragment")
        text = _require_text(entry.get("text"), f"bank.groups[{global_index}].text")
        if text not in claims:
            claims.append(text)
        for slot in entry["support_slots"]:
            paper_key = str(slot["paper_key"])
            member_papers.add(paper_key)
            task_ids.add(str(slot["task_id"]))
            relation_ids.update(str(value) for value in slot["relation_ids"])
            supports.append(
                {
                    "claim_slot_id": slot["claim_slot_id"],
                    "task_id": slot["task_id"],
                    "claim": text,
                    "paper_key": paper_key,
                    "primary_claim_id": slot["primary_claim_id"],
                    "source_claim_ids": list(slot["source_claim_ids"]),
                    "evidence_ids": list(slot["evidence_ids"]),
                    "source_field_ids": list(slot["source_field_ids"]),
                    "study_id": slot["study_id"],
                }
            )

    roles: dict[str, str] = {}
    for role in paper_roles:
        if not _is_sequence(role) or len(role) != 2:
            _error("compiled paper role must be a (paper_key, global_group_index) pair")
        paper_key, global_index = role
        if not isinstance(paper_key, str) or not paper_key.strip():
            _error("compiled paper role has an invalid paper key")
        if type(global_index) is not int or global_index not in global_indices:
            _error("compiled paper role references an unselected global fragment")
        if paper_key in roles:
            _error("compiled paper roles repeat a paper key")
        entry = bank["groups"][global_index]
        owned_papers = {str(slot["paper_key"]) for slot in entry["support_slots"]}
        if paper_key not in owned_papers:
            _error("compiled paper role fragment is not owned by its paper")
        text = _require_text(entry.get("text"), f"bank.groups[{global_index}].text")
        roles[paper_key] = text
    if set(roles) != member_papers:
        _error("compiled paper roles differ from exact selected support members")

    metadata = catalog[metadata_index]
    return {
        "section_id": f"section:{candidate_id}:{section_index}",
        "title": metadata["title"],
        "goal": metadata["goal"],
        "rationale": metadata["rationale"],
        "paper_keys": sorted(member_papers),
        "paper_roles": roles,
        "task_ids": sorted(task_ids),
        "relation_ids": sorted(relation_ids),
        "claims": claims,
        "claim_support": supports,
    }


def compile_candidate_organization_sections_v1(
    bank: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
    *,
    candidate_id: str,
    section_choices: Sequence[Any],
    metadata_catalog: Sequence[Mapping[str, Any]],
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
) -> dict[str, Any]:
    """Compile merged global section choices into the existing candidate shape."""

    validate_candidate_fragment_bank_v1(bank, scope)
    candidate_id = _require_text(candidate_id, "candidate_id")
    if not _is_sequence(section_choices) or not section_choices:
        _error("compiled candidate requires at least one global section choice")

    def choice_value(choice: Any, name: str) -> Any:
        if isinstance(choice, Mapping):
            return choice.get(name)
        return getattr(choice, name, None)

    normalized: list[
        tuple[int, tuple[int, ...], int, int, Sequence[Any], Sequence[Any]]
    ] = []
    for choice in section_choices:
        metadata_index = choice_value(choice, "metadata_index")
        global_indices = choice_value(choice, "global_fragment_indices")
        partition_index = choice_value(choice, "partition_index")
        source_section_index = choice_value(choice, "section_index")
        paper_roles = choice_value(choice, "paper_roles")
        if type(metadata_index) is not int or metadata_index < 0:
            _error("global section choice has an invalid metadata index")
        checked_indices = _request_index_array(
            global_indices,
            upper_bound=len(bank["groups"]),
            label="global section choice.fragment_indices",
        )
        if type(partition_index) is not int or partition_index < 0:
            _error("global section choice has an invalid partition index")
        if type(source_section_index) is not int or source_section_index < 0:
            _error("global section choice has an invalid local section index")
        if not _is_sequence(paper_roles):
            _error("global section choice paper_roles must be an array")
        normalized.append(
            (
                metadata_index,
                tuple(sorted(checked_indices)),
                partition_index,
                source_section_index,
                checked_indices,
                paper_roles,
            )
        )
    normalized.sort(key=lambda row: row[:4])

    used_groups: set[str] = set()
    used_papers: set[str] = set()
    selected_global_indices: set[int] = set()
    role_texts: dict[str, set[str]] = {}
    role_counts: dict[str, int] = {}
    compiled_sections: list[dict[str, Any]] = []
    for output_index, (
        metadata_index,
        _sort_indices,
        _partition_index,
        _source_index,
        indices,
        roles,
    ) in enumerate(normalized):
        overlap = selected_global_indices.intersection(indices)
        if overlap:
            _error(f"global section choices repeat fragment index {min(overlap)}")
        selected_global_indices.update(indices)
        for global_index in indices:
            entry = bank["groups"][global_index]
            used_groups.add(str(entry["claim_group"]["claim_group_id"]))
            used_papers.update(
                str(slot["paper_key"]) for slot in entry["support_slots"]
            )
        compiled = compile_candidate_organization_section_v1(
            bank,
            candidate_id=candidate_id,
            section_index=output_index,
            metadata_index=metadata_index,
            global_fragment_indices=indices,
            paper_roles=roles,
            metadata_catalog=metadata_catalog,
        )
        for paper_key, role_text in compiled["paper_roles"].items():
            role_texts.setdefault(paper_key, set()).add(str(role_text).casefold())
            role_counts[paper_key] = role_counts.get(paper_key, 0) + 1
        compiled_sections.append(compiled)

    required_groups = set(_unique_strings(required_group_ids, "required_group_ids"))
    missing_groups = sorted(required_groups - used_groups)
    if missing_groups:
        _error(f"compiled candidate omits required group {missing_groups[0]}")
    required_papers = set(_unique_strings(required_paper_keys, "required_paper_keys"))
    missing_papers = sorted(required_papers - used_papers)
    if missing_papers:
        _error(f"compiled candidate omits required paper witness {missing_papers[0]}")
    collision = next(
        (
            paper
            for paper, count in role_counts.items()
            if count > 1 and len(role_texts.get(paper, set())) < count
        ),
        None,
    )
    if collision:
        _error(f"compiled candidate has duplicate source-owned paper role {collision}")

    result = {"candidate_id": candidate_id, "sections": compiled_sections}
    try:
        scope.validate(result)
    except CandidateOutputScopeError as exc:
        raise CandidateFragmentBankError(
            f"compiled candidate failed exact source scope validation: {exc}"
        ) from exc
    return result


def expand_candidate_organization_map_v1(
    payload: Mapping[str, Any],
    bank: Mapping[str, Any],
    scope: CandidateOutputScopeV1,
    required_group_ids: Sequence[str],
    required_paper_keys: Sequence[str],
    *,
    axis: Any,
    plan: Any,
    metadata_catalog: Sequence[Mapping[str, Any]],
    partition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a strict local or full-bank map into exact Outline v3 data."""

    validate_candidate_fragment_bank_v1(bank, scope)
    request = build_candidate_organization_request_v1(
        bank,
        scope,
        axis=axis,
        plan=plan,
        required_group_ids=required_group_ids,
        required_paper_keys=required_paper_keys,
        metadata_catalog=metadata_catalog,
        partition=partition,
    )
    validate_candidate_organization_map_for_request_v1(payload, request)
    view = _validate_candidate_organization_request_v1(request)
    candidate_id = str(view["candidate_id"])
    local_to_global = list(view["local_to_global_group_indices"])
    required_groups = set(_unique_strings(required_group_ids, "required_group_ids"))
    required_papers = set(_unique_strings(required_paper_keys, "required_paper_keys"))
    group_ids = {
        str(entry["claim_group"]["claim_group_id"]): index
        for index, entry in enumerate(bank["groups"])
    }
    unknown_groups = sorted(required_groups - set(group_ids))
    if unknown_groups:
        _error(f"required group {unknown_groups[0]} is absent from the bank")

    selected_global = {
        local_to_global[local_index]
        for section in payload["sections"]
        for local_index in section["fragment_indices"]
    }
    selected_group_ids = {
        str(bank["groups"][index]["claim_group"]["claim_group_id"])
        for index in selected_global
    }
    missing_required = sorted(required_groups - selected_group_ids)
    if missing_required:
        _error(f"organization map omits required group {missing_required[0]}")
    selected_papers = {
        str(slot["paper_key"])
        for index in selected_global
        for slot in bank["groups"][index]["support_slots"]
    }
    missing_papers = sorted(required_papers - selected_papers)
    if missing_papers:
        _error(
            f"organization map has no complete selected group for required paper {missing_papers[0]}"
        )

    if not payload["sections"]:
        if view["partition"] is None or required_groups or required_papers:
            _error("only an optional organization leaf may compile to no sections")
        return {"candidate_id": candidate_id, "sections": []}

    sections: list[dict[str, Any]] = []
    for section in payload["sections"]:
        global_fragments = [
            local_to_global[local_index] for local_index in section["fragment_indices"]
        ]
        global_roles = [
            [view["paper_keys"][paper_index], local_to_global[role_local_index]]
            for paper_index, role_local_index in section["paper_roles"]
        ]
        sections.append(
            compile_candidate_organization_section_v1(
                bank,
                candidate_id=candidate_id,
                section_index=int(section["section_index"]),
                metadata_index=int(section["metadata_index"]),
                global_fragment_indices=global_fragments,
                paper_roles=global_roles,
                metadata_catalog=metadata_catalog,
            )
        )
    result = {"candidate_id": candidate_id, "sections": sections}
    try:
        scope.validate(result)
    except CandidateOutputScopeError as exc:
        raise CandidateFragmentBankError(
            f"compiled candidate failed exact source scope validation: {exc}"
        ) from exc
    return result


__all__ = [
    "BANK_SCHEMA_VERSION",
    "METADATA_CATALOG_CONTRACT_HASH",
    "METADATA_CATALOG_SCHEMA_VERSION",
    "ORGANIZATION_MAP_SCHEMA_HASH",
    "ORGANIZATION_MAP_SCHEMA_VERSION",
    "ORGANIZATION_PARTITION_SCHEMA_VERSION",
    "ORGANIZATION_REQUEST_SCHEMA_VERSION",
    "CandidateFragmentBankError",
    "build_candidate_fragment_bank_v1",
    "build_candidate_organization_request_v1",
    "compile_candidate_organization_section_v1",
    "compile_candidate_organization_sections_v1",
    "expand_candidate_organization_map_v1",
    "resolve_candidate_fragment_claims",
    "validate_candidate_fragment_bank_intrinsic_v1",
    "validate_candidate_fragment_bank_v1",
    "validate_candidate_organization_map_v1",
    "validate_candidate_organization_map_for_request_v1",
]

"""Pure bounded all-candidate overflow assessment and reduction contracts.

The v1 hierarchy/ledger is input evidence only. This module emits distinct
v2 logical request payloads, measures both serialized inputs and bounded
output shapes through caller-supplied production callbacks, and validates
model responses against compiler-owned candidate/source coverage.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from outline.arbitration_assessment_wire import (
    WIRE_SCHEMA as ASSESSMENT_WIRE_SCHEMA,
)
from outline.arbitration_assessment_wire import (
    ArbitrationAssessmentWireError,
    expand_assessment_wire_request,
    expand_assessment_wire_response,
    project_assessment_wire,
)
from outline.candidate_arbitration_views import (
    ArbitrationViewError,
    build_arbitration_review_group_map,
    compile_arbitration_critical_views,
    pack_arbitration_critical_view_wire,
    validate_arbitration_view_set,
)
from outline.candidate_output_scope import CandidateOutputScopeV1
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from services.writer_source_inventory import VerifiedWriterSourceInventoryV1

WORK_PLAN_SCHEMA = "arbitration_work_plan/v3"
DECISION_SCOPE_SCHEMA = "arbitration_decision_scope/v2"
ASSESSMENT_INPUT_SCHEMA = "arbitration_assessment_input/v2"
ASSESSMENT_RESPONSE_SCHEMA = "arbitration_leaf_response/v2"
REDUCTION_INPUT_SCHEMA = "arbitration_reduction_input/v2"
REDUCTION_RESPONSE_SCHEMA = "arbitration_reduction_response/v2"
SELECTION_INPUT_SCHEMA = "arbitration_selection_input/v3"
SELECTION_RESPONSE_SCHEMA = "arbitration_selection_response/v3"
SELECTION_DETAIL_WORK_PLAN_SCHEMA = "arbitration_selection_detail_work_plan/v1"
SELECTION_DETAIL_REQUEST_SCHEMA = "arbitration_selection_detail_request/v1"
SELECTION_DETAIL_RESPONSE_SCHEMA = "arbitration_selection_detail_response/v1"
SELECTION_DETAIL_RESULTS_SCHEMA = "arbitration_selection_detail_results/v1"
SELECTION_FINAL_INPUT_SCHEMA = "arbitration_selection_final_input/v1"
SELECTION_DETAIL_KIND = "arbitration_selector_detail_review"
SELECTION_FINAL_KIND = "arbitration_decision_final"

_DIRECTIONS = {
    "supports", "contradicts", "mixed", "null", "reverse", "not_comparable", "insufficient",
}
_DISAGREEMENT = {"consistent", "disagrees", "mixed", "uncertain", "not_applicable"}
_SELECTION_OUTCOMES = {"selected", "detail_required"}


class ArbitrationAggregationError(ValueError):
    """A new overflow request is invalid, unbounded, or source-incomplete."""


class ArbitrationSerializationError(ArbitrationAggregationError):
    """A complete indivisible request/response cannot fit its route bounds."""


_SELECTOR_ARTIFACT_TOKEN = object()


class _SelectorArtifact(Mapping[str, Any]):
    """Immutable validator/builder-issued selector evidence with provenance seal."""

    __slots__ = ("_kind", "_payload_hash", "_payload_json", "_provenance_json", "_seal_hash")

    def __init__(
        self,
        kind: str,
        payload: Mapping[str, Any],
        provenance: Mapping[str, Any],
        *,
        _token: object,
    ) -> None:
        if _token is not _SELECTOR_ARTIFACT_TOKEN:
            raise ArbitrationAggregationError("selector evidence artifacts are issued only by their compiler/validator")
        payload_json = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        provenance_json = json.dumps(
            dict(provenance), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        payload_hash = compute_v3_hash(json.loads(payload_json))
        seal_hash = compute_v3_hash({
            "kind": kind,
            "payload_hash": payload_hash,
            "provenance_hash": compute_v3_hash(json.loads(provenance_json)),
        })
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_payload_json", payload_json)
        object.__setattr__(self, "_payload_hash", payload_hash)
        object.__setattr__(self, "_provenance_json", provenance_json)
        object.__setattr__(self, "_seal_hash", seal_hash)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("validated selector evidence is immutable")

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(json.loads(self._payload_json)[key])

    def __iter__(self):
        return iter(json.loads(self._payload_json))

    def __len__(self) -> int:
        return len(json.loads(self._payload_json))

    def __deepcopy__(self, _memo: dict[int, Any]) -> _SelectorArtifact:
        return self

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(json.loads(self._payload_json))


def _selector_artifact_payload(value: Any, kind: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not isinstance(value, _SelectorArtifact) or value._kind != kind:
        raise ArbitrationAggregationError(f"{kind} requires immutable compiler/validator-issued evidence")
    payload = json.loads(value._payload_json)
    provenance = json.loads(value._provenance_json)
    if compute_v3_hash(payload) != value._payload_hash:
        raise ArbitrationAggregationError(f"{kind} sealed payload changed")
    expected_seal = compute_v3_hash({
        "kind": value._kind,
        "payload_hash": value._payload_hash,
        "provenance_hash": compute_v3_hash(provenance),
    })
    if expected_seal != value._seal_hash:
        raise ArbitrationAggregationError(f"{kind} sealed provenance changed")
    return copy.deepcopy(payload), value._seal_hash, copy.deepcopy(provenance)


@dataclass(frozen=True, slots=True)
class ArbitrationSerializationCaps:
    """Declared route budgets and finite response-text targets for a compiler."""

    effective_input_cap: int
    verified_context_limit: int
    route_max_output: int
    reasoning_reserve: int
    safety_margin: int
    route_identity: Mapping[str, Any]
    assessment_text_tokens: int
    group_summary_text_tokens: int
    selection_text_tokens: int
    max_detail_required_ids: int
    assessment_wire_enabled: bool = False

    def validate(self) -> None:
        integer_fields = (
            "effective_input_cap", "verified_context_limit", "route_max_output",
            "reasoning_reserve", "safety_margin", "assessment_text_tokens",
            "group_summary_text_tokens", "selection_text_tokens", "max_detail_required_ids",
        )
        for field in integer_fields:
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ArbitrationAggregationError(f"{field} must be a non-negative integer")
        if self.effective_input_cap < 1:
            raise ArbitrationAggregationError("effective_input_cap must be positive")
        if type(self.assessment_wire_enabled) is not bool:
            raise ArbitrationAggregationError("assessment_wire_enabled must be a boolean")
        if self.verified_context_limit < 1 or self.route_max_output < 1:
            raise ArbitrationAggregationError("route context and output caps must be positive")
        if self.assessment_text_tokens < 1 or self.group_summary_text_tokens < 1 or self.selection_text_tokens < 1:
            raise ArbitrationAggregationError("every new response text field needs a finite positive token bound")
        if not isinstance(self.route_identity, Mapping) or not self.route_identity:
            raise ArbitrationAggregationError("route_identity must identify the selected provider route")


RequestBuilder = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
InputEstimator = Callable[[Mapping[str, Any]], Any]
OutputEstimator = Callable[[Mapping[str, Any]], Any]


def build_overflow_assessment_plan(
    *,
    legacy_plan: Mapping[str, Any],
    legacy_ledger: Mapping[str, Any],
    legacy_source_request: Mapping[str, Any],
    candidate_contents: Mapping[str, Mapping[str, Any]],
    candidate_scope: CandidateOutputScopeV1,
    source_inventory: VerifiedWriterSourceInventoryV1,
    review_intent: Any,
    critique_disposition: Mapping[str, Any],
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
    request_builder_id: str = "arbitration_overflow_request/v2",
) -> dict[str, Any]:
    """Build and dual-pack the new v2 all-candidate assessment path.

    Inputs must be READY legacy artifacts and the actual verified source
    inventory/scope objects. Legacy assessment prose is never transformed into
    a typed response; each typed assessment exists only after validating an
    actual new v2 model response.
    """

    caps.validate()
    if not callable(request_builder) or not callable(estimate_input_tokens) or not callable(estimate_output_tokens):
        raise ArbitrationAggregationError("request builder and both route estimators are required")
    if not isinstance(request_builder_id, str) or not request_builder_id.strip():
        raise ArbitrationAggregationError("request_builder_id must be a stable non-empty identity")
    candidate_ids, candidate_hashes = _validate_ready_legacy_inputs(
        legacy_plan, legacy_ledger, legacy_source_request, candidate_contents,
        candidate_scope, review_intent, critique_disposition,
    )
    raw_ledger = copy.deepcopy(dict(legacy_ledger))
    canonical_ledger = _expand_and_validate_ledger(raw_ledger)
    intent = review_intent.to_dict() if callable(getattr(review_intent, "to_dict", None)) else review_intent
    if not isinstance(intent, Mapping):
        raise ArbitrationAggregationError("review_intent must be an object")
    intent_hash = hash_json(dict(intent))
    plan_provenance = legacy_plan.get("provenance_hashes")
    request_hash = hash_json(dict(legacy_source_request))
    if not isinstance(plan_provenance, Mapping) or plan_provenance.get("request") != request_hash:
        raise ArbitrationAggregationError("READY legacy hierarchy request hash differs from the original prepared source")
    if plan_provenance.get("intent") != intent_hash:
        raise ArbitrationAggregationError("READY legacy hierarchy intent hash differs from the original prepared source")
    if canonical_ledger.get("provenance_hashes") != dict(plan_provenance):
        raise ArbitrationAggregationError("READY legacy ledger provenance differs from its hierarchy plan")
    issues = canonical_ledger.get("critique_issues")
    if not isinstance(issues, list):
        raise ArbitrationAggregationError("READY legacy ledger omits typed critique issues")
    try:
        group_map = build_arbitration_review_group_map(
            candidate_scope, intent, issues, candidate_contents=candidate_contents,
        )
        view_set = compile_arbitration_critical_views(
            candidate_ids=candidate_ids,
            candidate_hashes=candidate_hashes,
            candidate_contents=candidate_contents,
            legacy_ledger=raw_ledger,
            candidate_scope=candidate_scope,
            source_inventory=source_inventory,
            review_group_map=group_map,
        )
    except (ArbitrationViewError, TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAggregationError(str(exc)) from exc
    view_set = validate_arbitration_view_set(
        view_set, expected_view_set_hash=str(view_set["view_set_hash"]),
    )
    units = _compile_legacy_units(
        legacy_plan=legacy_plan,
        candidate_contents=candidate_contents,
        candidate_ids=candidate_ids,
        candidate_hashes=candidate_hashes,
        candidate_scope=candidate_scope,
        ledger=canonical_ledger,
        review_group_map=group_map,
        view_set=view_set,
    )
    units = _attach_group_views(units, candidate_ids, view_set)
    legacy_plan_hash = _required_hash(legacy_plan.get("plan_hash"), "legacy plan_hash")
    legacy_ledger_hash = _required_hash(canonical_ledger.get("ledger_hash"), "legacy ledger_hash")
    legacy_wire_hash = str(raw_ledger.get("wire_body_hash") or compute_v3_hash(raw_ledger))
    source_binding = {
        "artifact_id": source_inventory.artifact_id,
        "artifact_hash": source_inventory.artifact_hash,
        "content_hash": source_inventory.content_hash,
    }
    identity = {
        "schema_version": WORK_PLAN_SCHEMA,
        "assessment_schema": ASSESSMENT_RESPONSE_SCHEMA,
        "reduction_schema": REDUCTION_RESPONSE_SCHEMA,
        "selection_schema": SELECTION_RESPONSE_SCHEMA,
        "legacy_plan_hash": legacy_plan_hash,
        "legacy_ledger_hash": legacy_ledger_hash,
        "legacy_ledger_wire_hash": legacy_wire_hash,
        "legacy_request_hash": request_hash,
        "legacy_intent_hash": intent_hash,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": candidate_hashes,
        "candidate_output_scope_hash": candidate_scope.content_hash,
        "source_inventory_binding": source_binding,
        "review_intent_hash": intent_hash,
        "critique_hashes": copy.deepcopy(dict(legacy_plan.get("critique_hashes") or {})),
        "review_group_map_hash": group_map["group_map_hash"],
        "critical_view_set_hash": view_set["view_set_hash"],
        "unit_hashes": [row["unit_hash"] for row in units],
        "route_identity": copy.deepcopy(dict(caps.route_identity)),
        "request_builder_id": request_builder_id,
    }
    if caps.assessment_wire_enabled:
        identity["assessment_wire_schema"] = ASSESSMENT_WIRE_SCHEMA
    plan_input_hash = compute_v3_hash(identity)
    leaves = _pack_assessment_units(
        units=units,
        candidate_ids=candidate_ids,
        candidate_hashes=candidate_hashes,
        identity=identity,
        plan_input_hash=plan_input_hash,
        group_map=group_map,
        view_set=view_set,
        request_builder=request_builder,
        estimate_input_tokens=estimate_input_tokens,
        estimate_output_tokens=estimate_output_tokens,
        caps=caps,
        request_builder_id=request_builder_id,
    )
    reducer_tree = _build_reducer_tree(
        leaves=leaves,
        units=units,
        candidate_ids=candidate_ids,
        candidate_hashes=candidate_hashes,
        group_map=group_map,
        plan_input_hash=plan_input_hash,
        caps=caps,
    )
    group_unit_coverage = _compile_group_unit_coverage(units, candidate_ids, group_map)
    body = {
        "schema_version": WORK_PLAN_SCHEMA,
        "plan_input_hash": plan_input_hash,
        "identity": identity,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": candidate_hashes,
        "candidate_output_scope_hash": candidate_scope.content_hash,
        "source_inventory_binding": source_binding,
        "review_group_map": group_map,
        "critical_view_set": view_set,
        "assessment_units": units,
        "assessment_leaves": leaves,
        "reducer_tree": reducer_tree,
        "reducer_root_node_id": reducer_tree["root_node_id"],
        "group_unit_coverage": group_unit_coverage,
        "counts": {
            "candidate_count": len(candidate_ids),
            "review_group_count": group_map["group_count"],
            "critical_view_count": view_set["critical_view_count"],
            "assessment_unit_count": len(units),
            "new_assessment_leaf_count": len(leaves),
            "new_reducer_count": len(reducer_tree["nodes"]),
            "reduction_depth": reducer_tree["depth"],
            "legacy_receipt_count": len(canonical_ledger.get("leaf_result_receipts") or []),
            "legacy_receipt_reuse_is_new_post": False,
        },
        "serialization_caps": {
            "effective_input_cap": caps.effective_input_cap,
            "verified_context_limit": caps.verified_context_limit,
            "route_max_output": caps.route_max_output,
            "reasoning_reserve": caps.reasoning_reserve,
            "safety_margin": caps.safety_margin,
            "route_identity": copy.deepcopy(dict(caps.route_identity)),
            **({"assessment_wire_schema": ASSESSMENT_WIRE_SCHEMA} if caps.assessment_wire_enabled else {}),
        },
    }
    body["work_plan_hash"] = compute_v3_hash(body)
    return body


def validate_arbitration_assessment_response(
    plan: Mapping[str, Any],
    leaf_node_id: str,
    response: Mapping[str, Any],
    *,
    estimate_output_tokens: OutputEstimator,
    _internal_result: bool = False,
) -> dict[str, Any]:
    """Validate one actual v2 assessment response against exact leaf scope."""

    node = _find_leaf(plan, leaf_node_id)
    wire_catalog = node.get("wire_catalog")
    wire_mode = wire_catalog is not None
    if wire_mode and not _internal_result:
        if not isinstance(response, Mapping):
            raise ArbitrationAggregationError("wire assessment response must be an object")
        wire_response = copy.deepcopy(dict(response))
        if wire_response.get("schema_version") != ASSESSMENT_RESPONSE_SCHEMA:
            raise ArbitrationAggregationError("wire assessment response has an unsupported schema")
        if wire_response.get("result_hash") is not None:
            raise ArbitrationAggregationError("wire model response cannot supply the compiler-owned result_hash")
        _check_observed_output_size(
            wire_response, estimate_output_tokens, int(node["requested_output_tokens"]),
        )
        try:
            response = expand_assessment_wire_response(
                wire_response, wire_catalog, node["request"],
            )
        except (ArbitrationAssessmentWireError, TypeError, ValueError, KeyError) as exc:
            raise ArbitrationAggregationError("wire assessment response failed exact local expansion") from exc
        internal_result = False
    elif _internal_result:
        if not isinstance(response, Mapping) or response.get("result_hash") is None:
            raise ArbitrationAggregationError("internal assessment result lacks its compiler-owned result_hash")
        response = copy.deepcopy(dict(response))
        internal_result = True
    else:
        internal_result = bool(isinstance(response, Mapping) and response.get("result_hash") is not None)
    if not isinstance(response, Mapping) or response.get("schema_version") != ASSESSMENT_RESPONSE_SCHEMA:
        raise ArbitrationAggregationError("assessment response has an unsupported schema")
    _require_exact_keys(
        response,
        {"schema_version", "node_id", "candidate_ids", "candidate_hashes", "unit_ids", "assessments", "group_contributions"},
        "assessment response",
        optional={"result_hash"},
    )
    if response.get("result_hash") is not None:
        expected_result_hash = compute_v3_hash({key: value for key, value in response.items() if key != "result_hash"})
        if response.get("result_hash") != expected_result_hash:
            raise ArbitrationAggregationError("assessment response result hash is invalid")
    if response.get("node_id") != leaf_node_id:
        raise ArbitrationAggregationError("assessment response node_id is foreign")
    candidate_ids = list(plan["candidate_ids"])
    candidate_hashes = dict(plan["candidate_hashes"])
    if response.get("candidate_ids") != candidate_ids or response.get("candidate_hashes") != candidate_hashes:
        raise ArbitrationAggregationError("assessment response candidate scope/hash set is incomplete or foreign")
    if response.get("unit_ids") != list(node["unit_ids"]):
        raise ArbitrationAggregationError("assessment response unit IDs differ from the exact packed leaf")
    expected_rows = _expected_leaf_candidate_rows(plan, node)
    raw_rows = response.get("assessments")
    if not isinstance(raw_rows, list):
        raise ArbitrationAggregationError("assessment response assessments must be an array")
    keys: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(raw_rows):
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError(f"assessment response row {index} is malformed")
        _require_exact_keys(
            row,
            {"candidate_id", "candidate_hash", "unit_id", "unit_hash", "presence", "section_bindings",
             "assessed_direction", "assessed_disagreement", "reason", "evidence_refs",
             "counterevidence_refs", "view_assessments"},
            f"assessment response row {index}",
        )
        candidate_id = _nonempty(row.get("candidate_id"), f"assessment[{index}].candidate_id")
        unit_id = _nonempty(row.get("unit_id"), f"assessment[{index}].unit_id")
        key = (candidate_id, unit_id)
        if key in by_key:
            raise ArbitrationAggregationError("assessment response duplicates a candidate/unit pair")
        if candidate_id not in candidate_hashes or row.get("candidate_hash") != candidate_hashes[candidate_id]:
            raise ArbitrationAggregationError("assessment response row has a foreign candidate/hash binding")
        if unit_id not in node["unit_ids"]:
            raise ArbitrationAggregationError("assessment response references a foreign unit")
        by_key[key] = row
        keys.append(key)
    if set(by_key) != set(expected_rows):
        raise ArbitrationAggregationError("assessment response omitted or added candidate/unit assessments")

    normalized_rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        for unit_id in node["unit_ids"]:
            expected = expected_rows[(candidate_id, unit_id)]
            row = by_key[(candidate_id, unit_id)]
            if row.get("presence") != expected["presence"]:
                raise ArbitrationAggregationError("assessment response presence differs from compiled source scope")
            if row.get("section_bindings") != expected["section_bindings"]:
                raise ArbitrationAggregationError("assessment response section/hash bindings differ from source scope")
            direction = _enum(row.get("assessed_direction"), _DIRECTIONS, "assessed_direction")
            disagreement = _enum(row.get("assessed_disagreement"), _DISAGREEMENT, "assessed_disagreement")
            reason = _bounded_text(row.get("reason"), "assessment reason")
            evidence_refs = _string_ids(row.get("evidence_refs"), "assessment evidence_refs")
            counterevidence_refs = _string_ids(row.get("counterevidence_refs"), "assessment counterevidence_refs")
            if not set(evidence_refs).issubset(set(expected["evidence_ref_ids"])):
                raise ArbitrationAggregationError("assessment response cites an evidence ID outside its exact unit")
            if not set(counterevidence_refs).issubset(set(expected["evidence_ref_ids"])):
                raise ArbitrationAggregationError("assessment response counterevidence is outside its exact unit")
            if expected["presence"] == "present" and not evidence_refs:
                raise ArbitrationAggregationError("present assessment must cite at least one exact source evidence ID")
            view_rows = row.get("view_assessments")
            if not isinstance(view_rows, list):
                raise ArbitrationAggregationError("view_assessments must be an array")
            expected_views = expected["decision_views"]
            if len(view_rows) != len(expected_views):
                raise ArbitrationAggregationError("assessment response omitted or added critical decision views")
            normalized_views = []
            for view_index, (observed, view) in enumerate(zip(view_rows, expected_views, strict=True)):
                if not isinstance(observed, Mapping):
                    raise ArbitrationAggregationError(f"view assessment {view_index} is malformed")
                _require_exact_keys(
                    observed,
                    {"decision_view_id", "decision_view_hash", "assessed_direction", "assessed_disagreement",
                     "reason", "evidence_refs", "counterevidence_refs"},
                    f"view assessment {view_index}",
                )
                if (
                    observed.get("decision_view_id") != view["decision_view_id"]
                    or observed.get("decision_view_hash") != view["decision_view_hash"]
                ):
                    raise ArbitrationAggregationError("view assessment has a foreign or stale literal source binding")
                view_direction = _enum(observed.get("assessed_direction"), _DIRECTIONS, "view assessed_direction")
                view_disagreement = _enum(observed.get("assessed_disagreement"), _DISAGREEMENT, "view assessed_disagreement")
                view_reason = _bounded_text(observed.get("reason"), "view assessment reason")
                view_refs = _string_ids(observed.get("evidence_refs"), "view assessment evidence_refs")
                view_counter_refs = _string_ids(observed.get("counterevidence_refs"), "view assessment counterevidence_refs")
                allowed_view_refs = set(view["literal_view_ref_ids"])
                if not set(view_refs + view_counter_refs).issubset(allowed_view_refs):
                    raise ArbitrationAggregationError("view assessment cites a source ID whose literal is not on this wire")
                if allowed_view_refs and not view_refs:
                    raise ArbitrationAggregationError("critical view assessment omitted all physically present literal refs")
                normalized_views.append({
                    "decision_view_id": view["decision_view_id"],
                    "decision_view_hash": view["decision_view_hash"],
                    "assessed_direction": view_direction,
                    "assessed_disagreement": view_disagreement,
                    "reason": view_reason,
                    "evidence_refs": list(view_refs),
                    "counterevidence_refs": list(view_counter_refs),
                })
            normalized_rows.append({
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hashes[candidate_id],
                "unit_id": unit_id,
                "unit_hash": expected["unit_hash"],
                "presence": expected["presence"],
                "section_bindings": copy.deepcopy(expected["section_bindings"]),
                "assessed_direction": direction,
                "assessed_disagreement": disagreement,
                "reason": reason,
                "evidence_refs": list(evidence_refs),
                "counterevidence_refs": list(counterevidence_refs),
                "view_assessments": normalized_views,
            })

    group_rows = _validate_group_contributions(
        plan, node, response.get("group_contributions"), normalized_rows,
        internal_result=internal_result,
    )
    result = {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": leaf_node_id,
        "candidate_ids": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "unit_ids": list(node["unit_ids"]),
        "assessments": normalized_rows,
        "group_contributions": group_rows,
    }
    if internal_result:
        if response.get("result_hash") != compute_v3_hash(result):
            raise ArbitrationAggregationError("compiled assessment lineage differs from its READY result hash")
    else:
        if wire_catalog is None:
            _check_observed_output_size(response, estimate_output_tokens, node["requested_output_tokens"])
        result["result_hash"] = compute_v3_hash(result)
        return result
    result["result_hash"] = str(response["result_hash"])
    return result


def build_arbitration_reducer_request(
    plan: Mapping[str, Any],
    reducer_node_id: str,
    child_results: Mapping[str, Mapping[str, Any]],
    *,
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
) -> dict[str, Any]:
    """Materialize one reducer only from its exact READY, validated children."""

    caps.validate()
    reducer = _find_reducer(plan, reducer_node_id)
    child_ids = list(reducer["child_node_ids"])
    if not isinstance(child_results, Mapping) or set(child_results) != set(child_ids):
        raise ArbitrationAggregationError("reducer requires exactly its declared READY children")
    normalized_children: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for child_id in child_ids:
        raw = child_results[child_id]
        validated = _validate_ready_child_result(
            plan, child_id, raw, estimate_output_tokens=estimate_output_tokens,
            output_cap=caps.route_max_output,
        )
        units = list(validated["unit_ids"])
        if seen_units.intersection(units):
            raise ArbitrationAggregationError("reducer child unit sets overlap")
        seen_units.update(units)
        normalized_children.append(validated)
    if sorted(seen_units, key=_unit_ordinal_key(plan)) != list(reducer["unit_ids"]):
        raise ArbitrationAggregationError("reducer child union differs from its exact deterministic unit partition")
    child_hashes = [str(row["result_hash"]) for row in normalized_children]
    node_id = "arbitration-reducer:" + compute_v3_hash({
        "schema_version": REDUCTION_INPUT_SCHEMA,
        "plan_input_hash": plan["plan_input_hash"],
        "reducer_tree_node_id": reducer["tree_node_id"],
        "ordered_child_node_ids": child_ids,
        "ordered_child_result_hashes": child_hashes,
        "unit_ids": list(reducer["unit_ids"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": plan["candidate_hashes"],
        "review_group_map_hash": plan["review_group_map"]["group_map_hash"],
        "route_identity": plan["identity"]["route_identity"],
    })[:24]
    group_summaries = _child_group_summaries(
        plan, reducer, child_ids, child_hashes, normalized_children,
    )
    expected_group_summaries = _expected_reducer_groups_from_children(
        plan, reducer, child_ids, child_hashes, normalized_children,
    )
    payload = {
        "schema_version": REDUCTION_INPUT_SCHEMA,
        "node_id": node_id,
        "tree_node_id": reducer["tree_node_id"],
        "level": reducer["level"],
        "child_node_ids": child_ids,
        "child_result_hashes": child_hashes,
        "decision_scope": _decision_scope(plan, reducer["unit_ids"]),
        "legacy_binding": _legacy_binding(plan),
        "unit_ids": list(reducer["unit_ids"]),
        "group_map": copy.deepcopy(plan["review_group_map"]),
        "candidate_group_summaries": group_summaries,
        "required_candidate_ids": list(plan["candidate_ids"]),
        "output_contract": _reduction_output_contract(plan, reducer, caps),
    }
    canonical_response_shape = _reduction_response_shape(
        plan, reducer, caps, expected_group_summaries=expected_group_summaries,
        child_result_hashes=child_hashes, node_id=node_id,
    )
    shape = canonical_response_shape
    if caps.assessment_wire_enabled:
        try:
            _preview_wire_request, shape, _preview_catalog = project_assessment_wire(
                payload, canonical_response_shape,
            )
        except ArbitrationAssessmentWireError as exc:
            raise ArbitrationAggregationError("reducer wire projection failed") from exc
    output_tokens = _estimate_output(estimate_output_tokens, shape)
    payload["output_contract"]["max_output_tokens"] = output_tokens
    wire_catalog: dict[str, Any] | None = None
    request_payload: Mapping[str, Any] = payload
    if caps.assessment_wire_enabled:
        try:
            request_payload, shape, wire_catalog = project_assessment_wire(
                payload, canonical_response_shape,
            )
        except ArbitrationAssessmentWireError as exc:
            raise ArbitrationAggregationError("reducer wire projection changed during materialization") from exc
    request = _build_request(request_builder, "arbitration_reduce_new", request_payload)
    input_tokens = _estimate_input(estimate_input_tokens, request)
    _assert_serialization_fits(input_tokens, output_tokens, caps, f"reducer {node_id}")
    result = {
        "schema_version": REDUCTION_INPUT_SCHEMA,
        "node_id": node_id,
        "tree_node_id": reducer["tree_node_id"],
        "child_node_ids": child_ids,
        "child_result_hashes": child_hashes,
        "unit_ids": list(reducer["unit_ids"]),
        "request": request,
        "request_hash": compute_v3_hash(request),
        "estimated_input_tokens": input_tokens,
        "requested_output_tokens": output_tokens,
        "response_shape": shape,
        "expected_group_summaries": expected_group_summaries,
    }
    if wire_catalog is not None:
        result.update({
            "wire_schema_version": ASSESSMENT_WIRE_SCHEMA,
            "canonical_request": copy.deepcopy(payload),
            "canonical_request_hash": wire_catalog["canonical_request_hash"],
            "wire_request_body_hash": wire_catalog["wire_request_body_hash"],
            "wire_catalog": wire_catalog,
            "canonical_response_shape": copy.deepcopy(canonical_response_shape),
            "canonical_response_shape_hash": wire_catalog["canonical_response_shape_hash"],
            "wire_response_shape_hash": wire_catalog["wire_response_shape_hash"],
        })
    return result


def validate_arbitration_reduction_response(
    plan: Mapping[str, Any],
    reducer_request: Mapping[str, Any],
    response: Mapping[str, Any],
    child_results: Mapping[str, Mapping[str, Any]],
    *,
    estimate_output_tokens: OutputEstimator,
    _internal_result: bool = False,
) -> dict[str, Any]:
    """Validate one reducer's bounded summaries and compiler-owned union."""

    reducer = _find_reducer(plan, str(reducer_request.get("tree_node_id") or ""))
    node_id = str(reducer_request.get("node_id") or "")
    child_ids = list(reducer["child_node_ids"])
    if reducer_request.get("child_node_ids") != child_ids:
        raise ArbitrationAggregationError("reducer request child IDs differ from its deterministic tree node")
    if reducer_request.get("unit_ids") != list(reducer["unit_ids"]):
        raise ArbitrationAggregationError("reducer request unit scope differs from its deterministic tree node")
    if not isinstance(child_results, Mapping) or set(child_results) != set(child_ids):
        raise ArbitrationAggregationError("reducer validation requires exactly its declared READY children")

    wire_catalog = reducer_request.get("wire_catalog")
    wire_mode = wire_catalog is not None
    if wire_mode and not _internal_result:
        if not isinstance(response, Mapping):
            raise ArbitrationAggregationError("wire reduction response must be an object")
        if response.get("result_hash") is not None:
            raise ArbitrationAggregationError("wire model response cannot supply the compiler-owned result_hash")
        _check_observed_output_size(
            response, estimate_output_tokens, int(reducer_request["requested_output_tokens"]),
        )
        try:
            response = expand_assessment_wire_response(
                response, wire_catalog, reducer_request["request"],
            )
        except (ArbitrationAssessmentWireError, TypeError, ValueError, KeyError) as exc:
            raise ArbitrationAggregationError("wire reduction response failed exact local expansion") from exc
    elif _internal_result:
        if not isinstance(response, Mapping) or response.get("result_hash") is None:
            raise ArbitrationAggregationError("internal reduction result lacks its compiler-owned result_hash")
        response = copy.deepcopy(dict(response))

    if not isinstance(response, Mapping) or response.get("schema_version") != REDUCTION_RESPONSE_SCHEMA:
        raise ArbitrationAggregationError("reduction response has an unsupported schema")
    _require_exact_keys(
        response,
        {"schema_version", "node_id", "tree_node_id", "child_node_ids", "child_result_hashes",
         "candidate_ids", "candidate_hashes", "unit_ids", "group_summaries"},
        "reduction response",
        optional={"result_hash"},
    )
    if response.get("result_hash") is not None:
        expected_result_hash = compute_v3_hash({key: value for key, value in response.items() if key != "result_hash"})
        if response.get("result_hash") != expected_result_hash:
            raise ArbitrationAggregationError("reduction response result hash is invalid")
    if response.get("candidate_ids") != list(plan["candidate_ids"]):
        raise ArbitrationAggregationError("reduction response omitted or altered an eligible candidate")
    if response.get("candidate_hashes") != dict(plan["candidate_hashes"]):
        raise ArbitrationAggregationError("reduction response candidate hashes are stale or foreign")
    if response.get("unit_ids") != list(reducer["unit_ids"]):
        raise ArbitrationAggregationError("reduction response unit union differs from exact children")
    normalized_children = [
        _validate_ready_child_result(
            plan, child_id, child_results[child_id],
            estimate_output_tokens=estimate_output_tokens,
        )
        for child_id in child_ids
    ]
    child_hashes = [str(child["result_hash"]) for child in normalized_children]
    if reducer_request.get("child_result_hashes") != child_hashes:
        raise ArbitrationAggregationError("reducer request child result hashes differ from its actual READY children")
    expected_node_id = "arbitration-reducer:" + compute_v3_hash({
        "schema_version": REDUCTION_INPUT_SCHEMA,
        "plan_input_hash": plan["plan_input_hash"],
        "reducer_tree_node_id": reducer["tree_node_id"],
        "ordered_child_node_ids": child_ids,
        "ordered_child_result_hashes": child_hashes,
        "unit_ids": list(reducer["unit_ids"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "review_group_map_hash": plan["review_group_map"]["group_map_hash"],
        "route_identity": plan["identity"]["route_identity"],
    })[:24]
    if node_id != expected_node_id:
        raise ArbitrationAggregationError("reducer request identity differs from its actual READY child results")
    provider_request = reducer_request.get("request")
    if (
        not isinstance(provider_request, Mapping)
        or provider_request.get("node_id") != expected_node_id
        or reducer_request.get("request_hash") != compute_v3_hash(dict(provider_request))
    ):
        raise ArbitrationAggregationError("reducer provider request hash or node identity is invalid")
    canonical_provider_request: Mapping[str, Any] = provider_request
    if wire_mode:
        try:
            canonical_provider_request = expand_assessment_wire_request(provider_request, wire_catalog)
        except (ArbitrationAssessmentWireError, TypeError, ValueError, KeyError) as exc:
            raise ArbitrationAggregationError("wire reducer request failed exact local expansion") from exc
        if reducer_request.get("canonical_request") != dict(canonical_provider_request):
            raise ArbitrationAggregationError("wire reducer canonical request differs from its READY local record")
    expected_groups = _expected_reducer_groups_from_children(
        plan, reducer, child_ids,
        child_hashes,
        normalized_children,
    )
    if reducer_request.get("expected_group_summaries") != expected_groups:
        raise ArbitrationAggregationError("reducer request expected group summaries differ from its actual READY children")
    if (
        canonical_provider_request.get("child_node_ids") != child_ids
        or canonical_provider_request.get("child_result_hashes") != child_hashes
    ):
        raise ArbitrationAggregationError("reducer provider request ordered child identity differs from READY children")
    if canonical_provider_request.get("candidate_group_summaries") != _child_group_summaries(
        plan, reducer, child_ids, child_hashes, normalized_children,
    ):
        raise ArbitrationAggregationError("reducer provider summaries differ from its actual READY children")
    if (
        response.get("node_id") != expected_node_id
        or response.get("tree_node_id") != reducer["tree_node_id"]
    ):
        raise ArbitrationAggregationError("reduction response node identity is foreign")
    if response.get("child_node_ids") != child_ids:
        raise ArbitrationAggregationError("reduction response child IDs differ from the actual READY children")
    if response.get("child_result_hashes") != child_hashes:
        raise ArbitrationAggregationError("reduction response child hashes differ from the actual READY children")
    raw_groups = response.get("group_summaries")
    if not isinstance(raw_groups, list):
        raise ArbitrationAggregationError("reduction response group_summaries must be an array")
    expected_by_key = {(row["candidate_id"], row["group_id"]): row for row in expected_groups}
    observed_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(raw_groups):
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError(f"reduction group summary {index} is malformed")
        _require_exact_keys(
            row,
            {"candidate_id", "candidate_hash", "group_id", "group_hash", "presence", "unit_ids",
             "decision_view_ids", "assessed_direction", "assessed_disagreement", "assessed_direction_values",
             "assessed_disagreement_values", "child_lineage", "summary", "evidence_refs", "counterevidence_refs"},
            f"reduction group summary {index}",
        )
        key = (str(row.get("candidate_id") or ""), str(row.get("group_id") or ""))
        if key in observed_by_key:
            raise ArbitrationAggregationError("reduction response duplicates a candidate/group row")
        observed_by_key[key] = row
    if set(observed_by_key) != set(expected_by_key):
        raise ArbitrationAggregationError("reduction response omitted or added a candidate/review-group summary")
    group_hashes = {
        str(row["group_id"]): compute_v3_hash(row)
        for row in plan["review_group_map"]["groups"]
    }
    normalized_groups: list[dict[str, Any]] = []
    for candidate_id in plan["candidate_ids"]:
        for group in plan["review_group_map"]["groups"]:
            key = (candidate_id, group["group_id"])
            expected = expected_by_key[key]
            row = observed_by_key[key]
            if row.get("candidate_hash") != plan["candidate_hashes"][candidate_id]:
                raise ArbitrationAggregationError("reduction group row has a foreign candidate hash")
            if row.get("group_hash") != group_hashes[group["group_id"]]:
                raise ArbitrationAggregationError("reduction group row has a stale review-group hash")
            if row.get("unit_ids") != list(expected["unit_ids"]):
                raise ArbitrationAggregationError("reduction group row has missing, duplicate, or foreign unit references")
            if row.get("decision_view_ids") != list(expected["decision_view_ids"]):
                raise ArbitrationAggregationError("reduction group row has missing, duplicate, or foreign critical views")
            expected_presence = "present" if expected["unit_ids"] else "not_applicable"
            if row.get("presence") != expected_presence:
                raise ArbitrationAggregationError("reduction group presence differs from compiled source coverage")
            direction_values = _string_ids(row.get("assessed_direction_values"), "reducer assessed_direction_values")
            disagreement_values = _string_ids(row.get("assessed_disagreement_values"), "reducer assessed_disagreement_values")
            if direction_values != list(expected["assessed_direction_values"]):
                raise ArbitrationAggregationError("reducer erased or altered a typed child direction value")
            if disagreement_values != list(expected["assessed_disagreement_values"]):
                raise ArbitrationAggregationError("reducer erased or altered a typed child disagreement value")
            if row.get("child_lineage") != expected["child_lineage"]:
                raise ArbitrationAggregationError("reducer omitted or altered a typed child/minority lineage row")
            direction = _enum(row.get("assessed_direction"), _DIRECTIONS, "reducer assessed_direction")
            disagreement = _enum(row.get("assessed_disagreement"), _DISAGREEMENT, "reducer assessed_disagreement")
            if direction != expected["assessed_direction"] or disagreement != expected["assessed_disagreement"]:
                raise ArbitrationAggregationError("reducer aggregate direction/disagreement contradicts typed children")
            summary = _bounded_text(row.get("summary"), "reducer group summary") if expected["unit_ids"] else ""
            evidence_refs = _string_ids(row.get("evidence_refs"), "reducer evidence_refs")
            counterevidence_refs = _string_ids(row.get("counterevidence_refs"), "reducer counterevidence_refs")
            if evidence_refs != list(expected["evidence_refs"]):
                raise ArbitrationAggregationError("reducer omitted or altered child evidence lineage")
            if counterevidence_refs != list(expected["counterevidence_refs"]):
                raise ArbitrationAggregationError("reducer omitted or altered minority/counterevidence lineage")
            normalized_groups.append({
                "candidate_id": candidate_id,
                "candidate_hash": plan["candidate_hashes"][candidate_id],
                "group_id": group["group_id"],
                "group_hash": group_hashes[group["group_id"]],
                "presence": expected_presence,
                "unit_ids": list(expected["unit_ids"]),
                "decision_view_ids": list(expected["decision_view_ids"]),
                "assessed_direction": direction,
                "assessed_disagreement": disagreement,
                "assessed_direction_values": list(expected["assessed_direction_values"]),
                "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
                "child_lineage": copy.deepcopy(expected["child_lineage"]),
                "typed_lineage": copy.deepcopy(expected["typed_lineage"]),
                "summary": summary,
                "evidence_refs": list(expected["evidence_refs"]),
                "counterevidence_refs": list(expected["counterevidence_refs"]),
            })
    result = {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "node_id": node_id,
        "tree_node_id": reducer["tree_node_id"],
        "child_node_ids": list(reducer_request["child_node_ids"]),
        "child_result_hashes": list(reducer_request["child_result_hashes"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": list(reducer["unit_ids"]),
        "group_summaries": normalized_groups,
    }
    if not wire_mode:
        _check_observed_output_size(
            {key: value for key, value in response.items() if key != "result_hash"},
            estimate_output_tokens,
            reducer_request["requested_output_tokens"],
        )
    result["result_hash"] = compute_v3_hash(result)
    return result


def build_global_selection_request(
    plan: Mapping[str, Any],
    aggregation_root: Mapping[str, Any],
    assessment_leaf_results: Mapping[str, Mapping[str, Any]],
    *,
    expected_view_set_hash: str,
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
) -> Mapping[str, Any]:
    """Build the complete exact-scope global selector request after READY tree."""

    caps.validate()
    if expected_view_set_hash != plan.get("critical_view_set", {}).get("view_set_hash"):
        raise ArbitrationAggregationError("READY critical view-set hash differs from the pinned selector dependency")
    try:
        validate_arbitration_view_set(
            plan["critical_view_set"], expected_view_set_hash=expected_view_set_hash,
        )
    except (ArbitrationViewError, TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAggregationError("selector critical views failed READY source revalidation") from exc
    root_id = str(plan.get("reducer_root_node_id") or "")
    root = _validate_ready_child_result(
        plan, root_id, aggregation_root,
        estimate_output_tokens=estimate_output_tokens,
        output_cap=caps.route_max_output,
    )
    leaf_ids = [row["node_id"] for row in plan["assessment_leaves"]]
    if not isinstance(assessment_leaf_results, Mapping) or set(assessment_leaf_results) != set(leaf_ids):
        raise ArbitrationAggregationError("selector requires every READY typed assessment leaf exactly once")
    validated_leaves = {
        leaf_id: validate_arbitration_assessment_response(
            plan, leaf_id, assessment_leaf_results[leaf_id], estimate_output_tokens=estimate_output_tokens,
            _internal_result=bool(
                _find_leaf(plan, leaf_id).get("wire_catalog") is not None
                and assessment_leaf_results[leaf_id].get("result_hash") is not None
            ),
        )
        for leaf_id in leaf_ids
    }
    critical_assessments = _collect_critical_assessments(plan, validated_leaves)
    expected_critical = {
        (row["candidate_id"], row["decision_view_id"])
        for row in plan["critical_view_set"]["critical_views"]
    }
    observed_critical = {(row["candidate_id"], row["decision_view_id"]) for row in critical_assessments}
    if observed_critical != expected_critical:
        raise ArbitrationAggregationError("READY leaf outputs do not cover every critical decision view exactly once")
    critical_view_wire = pack_arbitration_critical_view_wire(
        plan["critical_view_set"], expected_view_set_hash=expected_view_set_hash,
    )
    noncritical_rows = plan["critical_view_set"]["noncritical_fact_index"]
    max_detail_ids = min(caps.max_detail_required_ids, len(noncritical_rows))
    node_id = "arbitration-selector:" + compute_v3_hash({
        "schema_version": SELECTION_INPUT_SCHEMA,
        "plan_input_hash": plan["plan_input_hash"],
        "root_node_id": root["node_id"],
        "root_result_hash": root["result_hash"],
        "critical_assessments_hash": compute_v3_hash(critical_assessments),
        "candidate_ids": plan["candidate_ids"],
        "candidate_hashes": plan["candidate_hashes"],
        "route_identity": plan["identity"]["route_identity"],
    })[:24]
    payload = {
        "schema_version": SELECTION_INPUT_SCHEMA,
        "node_id": node_id,
        "decision_scope": _decision_scope(plan, [row["unit_id"] for row in plan["assessment_units"]]),
        "legacy_binding": _legacy_binding(plan),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "review_group_map": copy.deepcopy(plan["review_group_map"]),
        "candidate_group_summaries": _root_group_summaries(plan, root),
        "critical_view_set": {
            "schema_version": plan["critical_view_set"]["schema_version"],
            "view_set_hash": plan["critical_view_set"]["view_set_hash"],
            "decision_view_ids": list(critical_view_wire["decision_view_ids"]),
        },
        "critical_view_wire": critical_view_wire,
        "critical_assessments": critical_assessments,
        "noncritical_fact_index": copy.deepcopy(noncritical_rows),
        "coverage_bindings": _selector_coverage_bindings(plan, validated_leaves, root),
        "selection_policy": {
            "allowed_outcomes": ["selected", "detail_required"],
            "detail_required_ids_must_be_from_noncritical_fact_index": True,
            "critical_views_cannot_be_deferred": True,
            "candidates_are_never_dropped_during_assessment_or_reduction": True,
            "single_candidate_mode": "assess_the_only_candidate_without_winner_elimination" if len(plan["candidate_ids"]) == 1 else "select_only_from_candidate_ids",
        },
        "output_contract": _selection_output_contract(plan, max_detail_ids, caps),
    }
    shape = _selection_response_shape(plan, max_detail_ids, caps)
    output_tokens = _estimate_output(estimate_output_tokens, shape)
    payload["output_contract"]["max_output_tokens"] = output_tokens
    request = _build_request(request_builder, "arbitration_decision", payload)
    input_tokens = _estimate_input(estimate_input_tokens, request)
    _assert_serialization_fits(input_tokens, output_tokens, caps, "global arbitration selector")
    request_payload = dict(request)
    # The request builder has already attached prompt authority exactly once.
    # Bind the stable selector ID as a sidecar rather than rewrapping the body.
    selector_record = {
        "schema_version": SELECTION_INPUT_SCHEMA,
        "node_id": node_id,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "request": request_payload,
        "request_hash": compute_v3_hash(request_payload),
        "estimated_input_tokens": input_tokens,
        "requested_output_tokens": output_tokens,
        "response_shape": shape,
        "max_detail_required_ids": max_detail_ids,
        "noncritical_fact_ids": [row["decision_view_id"] for row in noncritical_rows],
    }
    return _SelectorArtifact("selection_request", selector_record, {
        "plan_input_hash": plan["plan_input_hash"],
        "critical_view_set_hash": expected_view_set_hash,
        "root_node_id": root["node_id"],
        "root_result_hash": root["result_hash"],
        "request_hash": selector_record["request_hash"],
    }, _token=_SELECTOR_ARTIFACT_TOKEN)


def validate_global_selection_response(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    estimate_output_tokens: OutputEstimator,
) -> Mapping[str, Any]:
    """Validate bounded selected/detail-required schema without winner invention."""

    selector_payload, selector_seal, selector_provenance = _selector_artifact_payload(
        selector_request, "selection_request",
    )
    selector_request = selector_payload
    selector_request_hash = compute_v3_hash(dict(selector_request["request"]))
    if (
        selector_provenance.get("plan_input_hash") != plan.get("plan_input_hash")
        or selector_provenance.get("critical_view_set_hash") != plan.get("critical_view_set", {}).get("view_set_hash")
        or selector_payload.get("request_hash") != selector_request_hash
        or selector_provenance.get("request_hash") != selector_request_hash
        or selector_payload.get("node_id") != selector_request["request"].get("node_id")
        or selector_provenance.get("root_node_id") != selector_request["request"]["coverage_bindings"]["root_node_id"]
        or selector_provenance.get("root_result_hash") != selector_request["request"]["coverage_bindings"]["root_result_hash"]
    ):
        raise ArbitrationAggregationError("selector request seal is foreign to the READY plan/root")

    if not isinstance(response, Mapping) or response.get("schema_version") != SELECTION_RESPONSE_SCHEMA:
        raise ArbitrationAggregationError("global selection response has an unsupported schema")
    _require_exact_keys(
        response,
        {"schema_version", "node_id", "candidate_ids", "candidate_hashes", "outcome",
         "selected_candidate_id", "detail_required_view_ids", "rationale"},
        "global selection response",
        optional={"result_hash"},
    )
    if response.get("result_hash") is not None:
        expected_result_hash = compute_v3_hash({key: value for key, value in response.items() if key != "result_hash"})
        if response.get("result_hash") != expected_result_hash:
            raise ArbitrationAggregationError("global selection response result hash is invalid")
    if response.get("node_id") != selector_request.get("node_id"):
        raise ArbitrationAggregationError("global selection response node_id is foreign")
    if response.get("candidate_ids") != list(plan["candidate_ids"]):
        raise ArbitrationAggregationError("global selection response candidate IDs differ from exact eligible scope")
    if response.get("candidate_hashes") != dict(plan["candidate_hashes"]):
        raise ArbitrationAggregationError("global selection response candidate hashes are stale or foreign")
    status = response.get("outcome")
    if status not in _SELECTION_OUTCOMES:
        raise ArbitrationAggregationError("global selection response outcome must be selected or detail_required")
    selected = response.get("selected_candidate_id")
    detail_ids = _string_ids(response.get("detail_required_view_ids"), "detail_required_view_ids")
    rationale = _bounded_text(response.get("rationale"), "selection rationale")
    if status == "selected":
        if not isinstance(selected, str) or selected not in plan["candidate_ids"]:
            raise ArbitrationAggregationError("selected candidate is outside the exact eligible candidate scope")
        if len(plan["candidate_ids"]) == 1 and selected != plan["candidate_ids"][0]:
            raise ArbitrationAggregationError("single-candidate assessment cannot eliminate its sole candidate")
        if detail_ids:
            raise ArbitrationAggregationError("selected outcome cannot also request unresolved detail")
    else:
        if selected is not None:
            raise ArbitrationAggregationError("detail_required outcome cannot publish a winner")
        if not detail_ids:
            raise ArbitrationAggregationError("detail_required outcome must identify at least one bounded detail ID")
        if len(detail_ids) > int(selector_request["max_detail_required_ids"]):
            raise ArbitrationAggregationError("detail_required IDs exceed the declared finite maximum")
        allowed = set(selector_request["noncritical_fact_ids"])
        critical = {row["decision_view_id"] for row in plan["critical_view_set"]["critical_views"]}
        if not set(detail_ids).issubset(allowed) or set(detail_ids).intersection(critical):
            raise ArbitrationAggregationError("detail_required references a foreign or critical view ID")
    result = {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": selector_request["node_id"],
        "selector_request_node_id": selector_request["node_id"],
        "selector_request_hash": selector_request["request_hash"],
        "selector_request_full_hash": selector_request_hash,
        "root_binding": copy.deepcopy(dict(selector_request["request"]["coverage_bindings"])),
        "critical_view_set_hash": plan["critical_view_set"]["view_set_hash"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": status,
        "selected_candidate_id": selected,
        "detail_required_view_ids": list(detail_ids),
        "rationale": rationale,
    }
    _check_observed_output_size(response, estimate_output_tokens, selector_request["requested_output_tokens"])
    result["result_hash"] = compute_v3_hash(result)
    return _SelectorArtifact("selection_result", result, {
        "plan_input_hash": plan["plan_input_hash"],
        "selector_request_seal": selector_seal,
        "selector_request_node_id": selector_request["node_id"],
        "selector_request_hash": selector_request["request_hash"],
        "selector_request_full_hash": selector_request_hash,
        "root_binding": copy.deepcopy(dict(selector_request["request"]["coverage_bindings"])),
        "critical_view_set_hash": plan["critical_view_set"]["view_set_hash"],
        "selection_response_hash": compute_v3_hash(dict(response)),
    }, _token=_SELECTOR_ARTIFACT_TOKEN)


def _validated_selector_detail_origin(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    selection_result: Mapping[str, Any],
    source_inventory: VerifiedWriterSourceInventoryV1,
    caps: ArbitrationSerializationCaps,
) -> tuple[list[str], dict[str, Mapping[str, Any]], dict[str, Any], str, str]:
    caps.validate()
    if type(source_inventory) is not VerifiedWriterSourceInventoryV1 or not source_inventory.is_verified:
        raise ArbitrationAggregationError("selector detail requires a verified source inventory")
    inventory_binding = {
        "artifact_id": source_inventory.artifact_id,
        "artifact_hash": source_inventory.artifact_hash,
        "content_hash": source_inventory.content_hash,
    }
    if inventory_binding != plan.get("source_inventory_binding"):
        raise ArbitrationAggregationError("selector detail source inventory differs from the READY work plan")
    if dict(caps.route_identity) != dict(plan["identity"]["route_identity"]):
        raise ArbitrationAggregationError("selector detail route differs from the READY selector route")
    selector_payload, selector_seal, selector_provenance = _selector_artifact_payload(
        selector_request, "selection_request",
    )
    selection_payload, _selection_seal, selection_provenance = _selector_artifact_payload(
        selection_result, "selection_result",
    )
    selector_request_body = selector_payload.get("request")
    selector_request_hash = compute_v3_hash(dict(selector_request_body)) if isinstance(selector_request_body, Mapping) else ""
    if (
        not isinstance(selector_request_body, Mapping)
        or selector_payload.get("request_hash") != selector_request_hash
        or selector_payload.get("candidate_ids") != list(plan["candidate_ids"])
        or selector_payload.get("candidate_hashes") != dict(plan["candidate_hashes"])
        or selector_payload.get("node_id") != selector_request_body.get("node_id")
        or selector_provenance.get("plan_input_hash") != plan.get("plan_input_hash")
        or selector_provenance.get("critical_view_set_hash") != plan.get("critical_view_set", {}).get("view_set_hash")
        or selector_provenance.get("request_hash") != selector_request_hash
    ):
        raise ArbitrationAggregationError("prior selector request hash or candidate scope is invalid")
    coverage = selector_request_body.get("coverage_bindings")
    try:
        root_result_hash = _required_hash(
            coverage.get("root_result_hash") if isinstance(coverage, Mapping) else None,
            "selector root result hash",
        )
    except ArbitrationAggregationError as exc:
        raise ArbitrationAggregationError("prior selector request lacks a READY root binding") from exc
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("work_plan_input_hash") != plan.get("plan_input_hash")
        or not _nonempty(coverage.get("root_node_id"), "selector root node ID")
        or coverage.get("root_result_hash") != root_result_hash
    ):
        raise ArbitrationAggregationError("prior selector request lacks a READY root binding")
    if (
        selection_payload.get("schema_version") != SELECTION_RESPONSE_SCHEMA
        or selection_payload.get("node_id") != selector_payload.get("node_id")
        or selection_payload.get("selector_request_node_id") != selector_payload.get("node_id")
        or selection_payload.get("selector_request_hash") != selector_request_hash
        or selection_payload.get("selector_request_full_hash") != selector_request_hash
        or selection_payload.get("root_binding") != dict(coverage)
        or selection_payload.get("critical_view_set_hash") != plan["critical_view_set"]["view_set_hash"]
        or selection_payload.get("candidate_ids") != list(plan["candidate_ids"])
        or selection_payload.get("candidate_hashes") != dict(plan["candidate_hashes"])
        or selection_payload.get("outcome") != "detail_required"
        or selection_payload.get("selected_candidate_id") is not None
        or selection_provenance.get("plan_input_hash") != plan.get("plan_input_hash")
        or selection_provenance.get("selector_request_seal") != selector_seal
        or selection_provenance.get("selector_request_node_id") != selector_payload.get("node_id")
        or selection_provenance.get("selector_request_hash") != selector_request_hash
        or selection_provenance.get("selector_request_full_hash") != selector_request_hash
        or selection_provenance.get("root_binding") != dict(coverage)
        or selection_provenance.get("critical_view_set_hash") != plan["critical_view_set"]["view_set_hash"]
    ):
        raise ArbitrationAggregationError("selector detail requires the validator-issued no-winner selection and exact request seal")
    requested = _string_ids(selection_payload.get("detail_required_view_ids"), "detail_required_view_ids", required=True)
    if len(requested) != len(set(requested)):
        raise ArbitrationAggregationError("selector detail view IDs must be unique")
    raw_max_requested = selector_payload.get("max_detail_required_ids")
    if type(raw_max_requested) is not int or raw_max_requested < 0:
        raise ArbitrationAggregationError("selector max_detail_required_ids must be a non-negative integer")
    max_requested = min(raw_max_requested, caps.max_detail_required_ids)
    if len(requested) > max_requested:
        raise ArbitrationSerializationError("selector detail work exceeds its finite Vmax bound")
    expected_noncritical_ids = [
        str(row["decision_view_id"]) for row in plan["critical_view_set"]["noncritical_fact_index"]
    ]
    noncritical_ids = _string_ids(selector_payload.get("noncritical_fact_ids"), "selector noncritical_fact_ids")
    if noncritical_ids != expected_noncritical_ids or raw_max_requested != min(
        caps.max_detail_required_ids, len(expected_noncritical_ids),
    ):
        raise ArbitrationAggregationError("prior selector detail index or Vmax differs from the READY view set")
    if not set(requested).issubset(set(noncritical_ids)):
        raise ArbitrationAggregationError("selector detail result requests a foreign noncritical view")
    try:
        validate_arbitration_view_set(
            plan["critical_view_set"],
            expected_view_set_hash=str(plan["critical_view_set"]["view_set_hash"]),
        )
    except (ArbitrationViewError, TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAggregationError("selector detail READY view set failed source-scope validation") from exc
    rows = {
        str(row["decision_view_id"]): row
        for row in plan["critical_view_set"]["noncritical_fact_index"]
    }
    if any(view_id not in rows for view_id in requested):
        raise ArbitrationAggregationError("selector detail view is absent from the READY noncritical fact index")
    return requested, rows, inventory_binding, selector_seal, _selection_seal


def _selector_detail_literal(
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    source_inventory: VerifiedWriterSourceInventoryV1,
) -> dict[str, Any]:
    from outline.candidate_arbitration_views import _source_fact_payload

    candidate_id = str(row.get("candidate_id") or "")
    if candidate_id not in plan["candidate_hashes"] or row.get("candidate_hash") != plan["candidate_hashes"][candidate_id]:
        raise ArbitrationAggregationError("selector detail view is bound to a foreign candidate")
    paper_key = _nonempty(row.get("paper_key"), "selector detail paper_key")
    fact_kind = str(row.get("fact_kind") or "")
    fact_id = _nonempty(row.get("fact_id"), "selector detail fact_id")
    paper = source_inventory.paper_by_key().get(paper_key)
    if paper is None:
        raise ArbitrationAggregationError(f"selector detail source paper {paper_key} is absent from verified inventory")
    field_by_kind = {
        "source_claim": ("claim_id", paper.claims),
        "evidence": ("evidence_id", paper.evidence),
        "source_field": ("source_field_id", paper.source_fields),
    }
    spec = field_by_kind.get(fact_kind)
    if spec is None:
        raise ArbitrationAggregationError("selector detail view has an unsupported source fact kind")
    id_field, source_rows = spec
    matches = [source_row for source_row in source_rows if getattr(source_row, id_field, None) == fact_id]
    if len(matches) != 1:
        raise ArbitrationAggregationError("selector detail fact is missing or ambiguous in verified source inventory")
    payload = _source_fact_payload(fact_kind, matches[0])
    fact_hash = compute_v3_hash(payload)
    if (
        payload.get("paper_key") != paper_key
        or fact_hash != row.get("source_fact_hash")
        or row.get("source_binding_hash") != fact_hash
        or row.get("source_inventory_artifact_id") != source_inventory.artifact_id
        or row.get("source_inventory_artifact_hash") != source_inventory.artifact_hash
    ):
        raise ArbitrationAggregationError("selector detail literal differs from its verified source-fact binding")
    identity = {
        key: row[key] for key in (
            "candidate_id", "candidate_hash", "fact_kind", "fact_id", "paper_key",
            "source_fact_hash", "source_inventory_artifact_id", "source_inventory_artifact_hash",
        )
    }
    expected_view_id = "decision-view:" + compute_v3_hash(identity)[:24]
    if (
        row.get("decision_view_id") != expected_view_id
        or row.get("view_hash") != compute_v3_hash(identity)
        or row.get("source_binding_hash") != compute_v3_hash(payload)
    ):
        raise ArbitrationAggregationError("selector detail view identity is not bound to its exact source literal")

    scope_rows = plan["critical_view_set"].get("source_slot_scope_index") or []
    slot_rows = {
        str(scope.get("claim_slot_id") or ""): scope
        for scope in scope_rows
        if isinstance(scope, Mapping) and scope.get("candidate_id") == candidate_id
    }
    slot_ids = _string_ids(row.get("claim_slot_ids"), "selector detail claim_slot_ids", required=True)
    claim_group_ids = _string_ids(row.get("claim_group_ids"), "selector detail claim_group_ids", required=True)
    source_field = {
        "source_claim": "source_claim_ids",
        "evidence": "evidence_ids",
        "source_field": "source_field_ids",
    }[fact_kind]
    observed_groups = set()
    for slot_id in slot_ids:
        scope = slot_rows.get(slot_id)
        if (
            scope is None
            or scope.get("paper_key") != paper_key
            or fact_id not in (scope.get(source_field) or [])
        ):
            raise ArbitrationAggregationError("selector detail fact has a foreign candidate/source-slot binding")
        observed_groups.add(str(scope.get("claim_group_id") or ""))
    if observed_groups != set(claim_group_ids):
        raise ArbitrationAggregationError("selector detail claim-group scope differs from verified source slots")
    return copy.deepcopy(payload)


def _selector_detail_view_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(row[key])
        for key in (
            "decision_view_id", "candidate_id", "candidate_hash", "fact_kind", "fact_id",
            "paper_key", "claim_slot_ids", "claim_group_ids", "source_fact_hash",
            "source_inventory_artifact_id", "source_inventory_artifact_hash",
            "view_hash", "source_binding_hash",
        )
    }


def _selector_detail_context(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    claim_group_ids = set(_string_ids(row.get("claim_group_ids"), "selector detail claim_group_ids", required=True))
    claim_slot_ids = set(_string_ids(row.get("claim_slot_ids"), "selector detail claim_slot_ids", required=True))
    candidate_id = str(row["candidate_id"])
    fact_id = str(row["fact_id"])
    related_source_ids = claim_group_ids | claim_slot_ids | {fact_id}
    related_source_ids.update(
        str(value)
        for scope in plan["critical_view_set"].get("source_slot_scope_index") or ()
        if isinstance(scope, Mapping)
        and scope.get("candidate_id") == candidate_id
        and scope.get("claim_slot_id") in claim_slot_ids
        for field in ("primary_claim_id", "source_claim_ids", "evidence_ids", "source_field_ids")
        for value in ([scope.get(field)] if field == "primary_claim_id" else scope.get(field) or ())
        if value
    )
    task_by_claim_group = plan["review_group_map"]["claim_group_task_bindings"]
    task_ids = {str(task_by_claim_group[group_id]) for group_id in claim_group_ids if group_id in task_by_claim_group}
    related_groups = []
    related_group_ids: set[str] = set()
    for group in plan["review_group_map"]["groups"]:
        source_ids = {str(value) for value in group.get("source_scope_ids") or ()}
        is_related_task = group.get("group_kind") == "task_topic" and str(group.get("task_id") or "") in task_ids
        if is_related_task or source_ids.intersection(related_source_ids):
            related_groups.append(copy.deepcopy(dict(group)))
            related_group_ids.add(str(group["group_id"]))
    if not related_group_ids:
        raise ArbitrationAggregationError("selector detail view has no source-bound review group context")
    body = selector_request["request"]
    summaries = [
        copy.deepcopy(dict(summary))
        for summary in body.get("candidate_group_summaries") or ()
        if isinstance(summary, Mapping)
        and summary.get("candidate_id") == candidate_id
        and str(summary.get("group_id") or "") in related_group_ids
    ]
    if {str(summary["group_id"]) for summary in summaries} != related_group_ids:
        raise ArbitrationAggregationError("selector detail request cannot bind every related candidate group summary")
    view_ids_by_slot = {
        str(view["decision_view_id"]): {str(value) for value in view.get("group_ids") or ()}
        for view in plan["critical_view_set"]["critical_views"]
        if view.get("candidate_id") == candidate_id
        and view.get("claim_slot_id") in claim_slot_ids
    }
    critical_ids = {
        view_id for view_id, groups in view_ids_by_slot.items()
        if groups.intersection(related_group_ids)
    }
    critical_assessments = [
        copy.deepcopy(dict(assessment))
        for assessment in body.get("critical_assessments") or ()
        if isinstance(assessment, Mapping)
        and assessment.get("candidate_id") == candidate_id
        and assessment.get("decision_view_id") in critical_ids
    ]
    return {
        "related_review_groups": related_groups,
        "related_candidate_group_summaries": summaries,
        "related_critical_assessments": critical_assessments,
    }


def _selector_detail_response_shape(
    node_id: str,
    selector_request: Mapping[str, Any],
    selection_result: Mapping[str, Any],
    plan: Mapping[str, Any],
    view_id: str,
    view_row: Mapping[str, Any],
    caps: ArbitrationSerializationCaps,
) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_DETAIL_RESPONSE_SCHEMA,
        "node_id": node_id,
        "prior_selector_node_id": selector_request["node_id"],
        "prior_selector_request_hash": selector_request["request_hash"],
        "prior_selection_result_hash": selection_result["result_hash"],
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "detail_view_id": view_id,
        "detail_view_hash": view_row["view_hash"],
        "source_fact_hash": view_row["source_fact_hash"],
        "assessment_status": "resolved",
        "assessed_direction": "supports",
        "assessed_disagreement": "consistent",
        "cited_detail_view_ids": [view_id],
        "reason": _token_fill(caps.selection_text_tokens),
    }


def _validate_selector_detail_work_plan(
    work_plan: Any,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    payload, seal, provenance = _selector_artifact_payload(work_plan, "selection_detail_work_plan")
    if payload.get("schema_version") != SELECTION_DETAIL_WORK_PLAN_SCHEMA:
        raise ArbitrationAggregationError("selector detail work plan has an unsupported schema")
    expected_hash = compute_v3_hash({key: copy.deepcopy(value) for key, value in payload.items() if key != "work_plan_hash"})
    if payload.get("work_plan_hash") != expected_hash or provenance.get("work_plan_hash") != expected_hash:
        raise ArbitrationAggregationError("selector detail work plan hash is invalid")
    return payload, seal, provenance


def _verify_selector_detail_request(request_record: Mapping[str, Any], label: str) -> str:
    body = request_record.get("body")
    full_request = request_record.get("request")
    measurement = request_record.get("measurement")
    if not isinstance(body, Mapping) or not isinstance(full_request, Mapping) or not isinstance(measurement, Mapping):
        raise ArbitrationAggregationError(f"{label} lacks its exact measured prompt-attached request")
    logical = copy.deepcopy(dict(full_request))
    authority = logical.pop("_prompt_authority", None)
    logical.pop("request_kind", None)  # supported by the unit/adapter request builder
    if not isinstance(authority, Mapping) or logical != dict(body):
        raise ArbitrationAggregationError(f"{label} provider request differs from its exact source/detail body")
    request_hash = compute_v3_hash(dict(full_request))
    if (
        request_record.get("request_hash") != request_hash
        or measurement.get("request_hash") != request_hash
    ):
        raise ArbitrationAggregationError(f"{label} prompt-attached request hash changed")
    return request_hash


def build_global_selection_detail_requests(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    selection_result: Mapping[str, Any],
    source_inventory: VerifiedWriterSourceInventoryV1,
    *,
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
    request_builder_id: str = "arbitration_selector_detail_review/v1",
) -> Mapping[str, Any]:
    """Compile one measured source-literal detail call per bounded selector ID."""

    caps.validate()
    requested_ids, view_rows, inventory_binding, selector_seal, selection_seal = _validated_selector_detail_origin(
        plan, selector_request, selection_result, source_inventory, caps,
    )
    if not callable(request_builder) or not callable(estimate_input_tokens) or not callable(estimate_output_tokens):
        raise ArbitrationAggregationError("selector detail builder and both route estimators are required")
    if not isinstance(request_builder_id, str) or not request_builder_id.strip():
        raise ArbitrationAggregationError("selector detail request_builder_id must be stable")
    request_hash = str(selector_request["request_hash"])
    selection_hash = str(selection_result["result_hash"])
    root_binding = copy.deepcopy(dict(selector_request["request"]["coverage_bindings"]))
    detail_count = len(requested_ids)
    if detail_count < 1 or detail_count > caps.max_detail_required_ids:
        raise ArbitrationSerializationError("selector detail count is outside the finite Vmax bound")
    vmax = min(
        int(selector_request["max_detail_required_ids"]),
        caps.max_detail_required_ids,
        len(plan["critical_view_set"]["noncritical_fact_index"]),
    )
    request_rows: list[dict[str, Any]] = []
    view_hashes: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    for ordinal, view_id in enumerate(requested_ids):
        row = view_rows[view_id]
        literal = _selector_detail_literal(plan, row, source_inventory)
        context = _selector_detail_context(plan, selector_request, row)
        view_hash = str(row["view_hash"])
        source_hash = str(row["source_fact_hash"])
        view_hashes[view_id] = view_hash
        source_hashes[view_id] = source_hash
        identity = {
            "schema_version": SELECTION_DETAIL_REQUEST_SCHEMA,
            "work_plan_input_hash": plan["plan_input_hash"],
            "prior_selector_node_id": selector_request["node_id"],
            "prior_selector_request_hash": request_hash,
            "prior_selection_result_hash": selection_hash,
            "root_node_id": root_binding["root_node_id"],
            "root_result_hash": root_binding["root_result_hash"],
            "detail_view_id": view_id,
            "detail_view_hash": view_hash,
            "source_fact_hash": source_hash,
            "detail_ordinal": ordinal,
            "detail_count": detail_count,
            "max_detail_count": vmax,
            "followup_ordinal": 1,
            "max_followups": 1,
            "request_builder_id": request_builder_id,
            "route_identity": copy.deepcopy(dict(caps.route_identity)),
        }
        node_id = "arbitration-selector-detail:" + compute_v3_hash(identity)[:24]
        payload = {
            "schema_version": SELECTION_DETAIL_REQUEST_SCHEMA,
            "stage": "bounded_noncritical_source_detail_assessment",
            "node_id": node_id,
            "prior_selector_node_id": selector_request["node_id"],
            "prior_selector_request_hash": request_hash,
            "prior_selection_result_hash": selection_hash,
            "root_binding": root_binding,
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "detail_ordinal": ordinal,
            "detail_count": detail_count,
            "max_detail_count": vmax,
            "followup_ordinal": 1,
            "max_followups": 1,
            "detail_view": _selector_detail_view_payload(row),
            "source_inventory_binding": copy.deepcopy(inventory_binding),
            "source_literal_fact": literal,
            **context,
            "assessment_contract": {
                "must_assess_only_this_noncritical_fact": True,
                "must_echo_exact_candidate_scope": True,
                "must_echo_exact_detail_view_id_and_hash": True,
                "must_return_resolved_typed_assessment_or_block": True,
                "cannot_select_or_eliminate_candidates": True,
            },
            "output_contract": {
                "schema_version": SELECTION_DETAIL_RESPONSE_SCHEMA,
                "candidate_ids": list(plan["candidate_ids"]),
                "candidate_hashes": dict(plan["candidate_hashes"]),
                "detail_view_id": view_id,
                "detail_view_hash": view_hash,
                "source_fact_hash": source_hash,
                "max_reason_tokens": caps.selection_text_tokens,
                "max_output_tokens": 1,
            },
        }
        shape = _selector_detail_response_shape(
            node_id, selector_request, selection_result, plan, view_id, row, caps,
        )
        output_tokens = _estimate_output(estimate_output_tokens, shape)
        payload["output_contract"]["max_output_tokens"] = output_tokens
        request = _build_request(request_builder, SELECTION_DETAIL_KIND, payload)
        input_tokens = _estimate_input(estimate_input_tokens, request)
        _assert_serialization_fits(input_tokens, output_tokens, caps, f"selector detail {node_id}")
        request_hash_for_node = compute_v3_hash(request)
        measurement = {
            "request_hash": request_hash_for_node,
            "estimated_input_tokens": input_tokens,
            "effective_input_cap": caps.effective_input_cap,
            "requested_output_tokens": output_tokens,
            "output_reserve": {
                "response_schema": SELECTION_DETAIL_RESPONSE_SCHEMA,
                "response_shape_hash": compute_v3_hash(shape),
                "estimated_output_tokens": output_tokens,
                "max_output_tokens": caps.route_max_output,
            },
            "verified_context_limit": caps.verified_context_limit,
            "reasoning_reserve": caps.reasoning_reserve,
            "safety_margin": caps.safety_margin,
            "context_total_tokens": input_tokens + output_tokens + caps.reasoning_reserve + caps.safety_margin,
            "route_identity": copy.deepcopy(dict(caps.route_identity)),
        }
        request_rows.append({
            "request_id": node_id,
            "request_builder_id": request_builder_id,
            "detail_view_id": view_id,
            "detail_view_hash": view_hash,
            "source_fact_hash": source_hash,
            "detail_ordinal": ordinal,
            "detail_count": detail_count,
            "max_detail_count": vmax,
            "body": payload,
            "request": request,
            "request_hash": request_hash_for_node,
            "measurement": measurement,
            "estimated_input_tokens": input_tokens,
            "requested_output_tokens": output_tokens,
            "response_shape": shape,
        })
    plan_body = {
        "schema_version": SELECTION_DETAIL_WORK_PLAN_SCHEMA,
        "work_plan_input_hash": plan["plan_input_hash"],
        "prior_selector_node_id": selector_request["node_id"],
        "prior_selector_request_hash": request_hash,
        "prior_selection_result_hash": selection_hash,
        "root_binding": root_binding,
        "critical_view_set_hash": plan["critical_view_set"]["view_set_hash"],
        "source_inventory_binding": inventory_binding,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "requested_view_ids": list(requested_ids),
        "requested_view_hashes": view_hashes,
        "requested_source_fact_hashes": source_hashes,
        "detail_count": detail_count,
        "max_detail_count": vmax,
        "followup_ordinal": 1,
        "max_followups": 1,
        "max_final_selector_calls": 1,
        "request_builder_id": request_builder_id,
        "route_identity": copy.deepcopy(dict(caps.route_identity)),
        "counts": {"Qselector_detail": detail_count, "Qfinal_selector": 1, "Qtotal": detail_count + 1},
        "requests": request_rows,
        "serialization_caps": {
            "effective_input_cap": caps.effective_input_cap,
            "verified_context_limit": caps.verified_context_limit,
            "route_max_output": caps.route_max_output,
            "reasoning_reserve": caps.reasoning_reserve,
            "safety_margin": caps.safety_margin,
            "route_identity": copy.deepcopy(dict(caps.route_identity)),
        },
    }
    plan_body["work_plan_hash"] = compute_v3_hash(plan_body)
    return _SelectorArtifact("selection_detail_work_plan", plan_body, {
        "plan_input_hash": plan["plan_input_hash"],
        "selector_request_seal": selector_seal,
        "selection_result_seal": selection_seal,
        "work_plan_hash": plan_body["work_plan_hash"],
    }, _token=_SELECTOR_ARTIFACT_TOKEN)


def validate_global_selection_detail_responses(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    selection_result: Mapping[str, Any],
    detail_work_plan: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    *,
    source_inventory: VerifiedWriterSourceInventoryV1,
    actual_requests: Sequence[Mapping[str, Any]],
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
) -> Mapping[str, Any]:
    """Validate exactly the requested literals and measured detail responses."""

    caps.validate()
    requested_ids, view_rows, inventory_binding, selector_seal, selection_seal = _validated_selector_detail_origin(
        plan, selector_request, selection_result, source_inventory, caps,
    )
    work, work_seal, work_provenance = _validate_selector_detail_work_plan(detail_work_plan)
    selection_hash = str(selection_result["result_hash"])
    if (
        work.get("work_plan_input_hash") != plan.get("plan_input_hash")
        or work.get("prior_selector_node_id") != selector_request.get("node_id")
        or work.get("prior_selector_request_hash") != selector_request.get("request_hash")
        or work.get("prior_selection_result_hash") != selection_hash
        or work.get("root_binding") != selector_request["request"]["coverage_bindings"]
        or work.get("critical_view_set_hash") != plan["critical_view_set"]["view_set_hash"]
        or work.get("source_inventory_binding") != inventory_binding
        or work.get("candidate_ids") != list(plan["candidate_ids"])
        or work.get("candidate_hashes") != dict(plan["candidate_hashes"])
        or work.get("requested_view_ids") != requested_ids
        or work.get("detail_count") != len(requested_ids)
        or work.get("followup_ordinal") != 1
        or work.get("max_followups") != 1
        or work.get("route_identity") != dict(caps.route_identity)
        or work_provenance.get("plan_input_hash") != plan.get("plan_input_hash")
        or work_provenance.get("selector_request_seal") != selector_seal
        or work_provenance.get("selection_result_seal") != selection_seal
        or not isinstance(work.get("requested_view_hashes"), Mapping)
        or set(work["requested_view_hashes"]) != set(requested_ids)
        or not isinstance(work.get("requested_source_fact_hashes"), Mapping)
        or set(work["requested_source_fact_hashes"]) != set(requested_ids)
    ):
        raise ArbitrationAggregationError("selector detail work plan is foreign to its single bounded follow-up")
    expected_vmax = min(
        int(selector_request["max_detail_required_ids"]),
        caps.max_detail_required_ids,
        len(plan["critical_view_set"]["noncritical_fact_index"]),
    )
    if work.get("max_detail_count") != expected_vmax or len(requested_ids) > expected_vmax:
        raise ArbitrationAggregationError("selector detail work plan exceeds its finite Vmax")
    expected_counts = {
        "Qselector_detail": len(requested_ids),
        "Qfinal_selector": 1,
        "Qtotal": len(requested_ids) + 1,
    }
    if work.get("counts") != expected_counts or len(requested_ids) > caps.max_detail_required_ids:
        raise ArbitrationAggregationError("selector detail calls differ from their finite Vmax work plan")
    requests = work.get("requests")
    if not isinstance(requests, list) or len(requests) != len(requested_ids):
        raise ArbitrationAggregationError("selector detail work plan request table is incomplete")
    request_by_id = {str(row.get("request_id") or ""): row for row in requests if isinstance(row, Mapping)}
    if len(request_by_id) != len(requests) or [
        str(row.get("detail_view_id") or "") for row in requests if isinstance(row, Mapping)
    ] != requested_ids:
        raise ArbitrationAggregationError("selector detail requests omit, duplicate, or reorder the exact IDs")
    actual_by_id = {str(row.get("request_id") or ""): row for row in actual_requests if isinstance(row, Mapping)}
    if len(actual_by_id) != len(actual_requests) or set(actual_by_id) != set(request_by_id):
        raise ArbitrationAggregationError("actual selector detail requests are missing, duplicate, or foreign")
    response_by_id = {str(row.get("request_id") or ""): row for row in responses if isinstance(row, Mapping)}
    if len(response_by_id) != len(responses) or set(response_by_id) != set(request_by_id):
        raise ArbitrationAggregationError("selector detail responses are missing, duplicate, or foreign")

    detail_assessments: list[dict[str, Any]] = []
    request_hashes: dict[str, str] = {}
    response_hashes: dict[str, str] = {}
    for ordinal, view_id in enumerate(requested_ids):
        planned = request_by_id[str(requests[ordinal]["request_id"])]
        view_row = view_rows[view_id]
        literal = _selector_detail_literal(plan, view_row, source_inventory)
        context = _selector_detail_context(plan, selector_request, view_row)
        expected_hash = str(work["requested_view_hashes"].get(view_id) or "")
        source_hash = str(work["requested_source_fact_hashes"].get(view_id) or "")
        if expected_hash != view_row.get("view_hash") or source_hash != view_row.get("source_fact_hash"):
            raise ArbitrationAggregationError("selector detail work plan view/fact hashes changed")
        expected_identity = {
            "schema_version": SELECTION_DETAIL_REQUEST_SCHEMA,
            "work_plan_input_hash": plan["plan_input_hash"],
            "prior_selector_node_id": selector_request["node_id"],
            "prior_selector_request_hash": selector_request["request_hash"],
            "prior_selection_result_hash": selection_hash,
            "root_node_id": work["root_binding"]["root_node_id"],
            "root_result_hash": work["root_binding"]["root_result_hash"],
            "detail_view_id": view_id,
            "detail_view_hash": expected_hash,
            "source_fact_hash": source_hash,
            "detail_ordinal": ordinal,
            "detail_count": len(requested_ids),
            "max_detail_count": expected_vmax,
            "followup_ordinal": 1,
            "max_followups": 1,
            "request_builder_id": work["request_builder_id"],
            "route_identity": dict(caps.route_identity),
        }
        expected_node_id = "arbitration-selector-detail:" + compute_v3_hash(expected_identity)[:24]
        body = planned.get("body")
        if not isinstance(body, Mapping) or (
            planned.get("request_id") != expected_node_id
            or planned.get("request_builder_id") != work["request_builder_id"]
            or planned.get("detail_view_id") != view_id
            or planned.get("detail_view_hash") != expected_hash
            or planned.get("source_fact_hash") != source_hash
                or planned.get("detail_count") != len(requested_ids)
                or planned.get("max_detail_count") != expected_vmax
            or body.get("node_id") != expected_node_id
                or body.get("detail_count") != len(requested_ids)
                or body.get("max_detail_count") != expected_vmax
            or body.get("detail_view") != _selector_detail_view_payload(view_row)
            or body.get("source_literal_fact") != literal
            or body.get("related_review_groups") != context["related_review_groups"]
            or body.get("related_candidate_group_summaries") != context["related_candidate_group_summaries"]
            or body.get("related_critical_assessments") != context["related_critical_assessments"]
        ):
            raise ArbitrationAggregationError("selector detail request changed its exact literal or source scope")
        request = actual_by_id[expected_node_id]
        actual_request_hash = _verify_selector_detail_request(request, f"selector detail {expected_node_id}")
        if (
            actual_request_hash != planned.get("request_hash")
            or request.get("body") != body
            or request.get("measurement") != planned.get("measurement")
        ):
            raise ArbitrationAggregationError("actual selector detail request differs from its measured JIT plan")
        measured_input = _estimate_input(estimate_input_tokens, request["request"])
        if measured_input != planned.get("estimated_input_tokens"):
            raise ArbitrationAggregationError("selector detail actual input estimate differs from its measured plan")
        response_shape = _selector_detail_response_shape(
            expected_node_id, selector_request, selection_result, plan, view_id, view_row, caps,
        )
        expected_output = _estimate_output(estimate_output_tokens, response_shape)
        expected_reserve = {
            "response_schema": SELECTION_DETAIL_RESPONSE_SCHEMA,
            "response_shape_hash": compute_v3_hash(response_shape),
            "estimated_output_tokens": expected_output,
            "max_output_tokens": caps.route_max_output,
        }
        if (
            planned.get("response_shape") != response_shape
            or planned.get("requested_output_tokens") != expected_output
            or (planned.get("measurement") or {}).get("output_reserve") != expected_reserve
            or (planned.get("body", {}).get("output_contract") or {}).get("max_output_tokens") != expected_output
        ):
            raise ArbitrationAggregationError("selector detail output reserve differs from its complete response schema")
        _assert_serialization_fits(
            measured_input, int(planned["requested_output_tokens"]), caps,
            f"selector detail {expected_node_id}",
        )
        wrapper = response_by_id[expected_node_id]
        response = wrapper.get("response")
        if not isinstance(response, Mapping) or set(response) != {
            "schema_version", "node_id", "prior_selector_node_id", "prior_selector_request_hash",
            "prior_selection_result_hash", "candidate_ids", "candidate_hashes", "detail_view_id",
            "detail_view_hash", "source_fact_hash", "assessment_status", "assessed_direction",
            "assessed_disagreement", "cited_detail_view_ids", "reason",
        }:
            raise ArbitrationAggregationError("selector detail response has unknown or missing fields")
        expected_response_values = {
            "schema_version": SELECTION_DETAIL_RESPONSE_SCHEMA,
            "node_id": expected_node_id,
            "prior_selector_node_id": selector_request["node_id"],
            "prior_selector_request_hash": selector_request["request_hash"],
            "prior_selection_result_hash": selection_hash,
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "detail_view_id": view_id,
            "detail_view_hash": expected_hash,
            "source_fact_hash": source_hash,
            "cited_detail_view_ids": [view_id],
        }
        for key, expected_value in expected_response_values.items():
            if response.get(key) != expected_value:
                raise ArbitrationAggregationError(f"selector detail response changed {key}")
        if response.get("assessment_status") != "resolved":
            raise ArbitrationAggregationError("unresolved selector detail blocks the final selector")
        direction = _enum(response.get("assessed_direction"), _DIRECTIONS - {"insufficient"}, "selector detail direction")
        disagreement = _enum(response.get("assessed_disagreement"), _DISAGREEMENT, "selector detail disagreement")
        reason = _bounded_text(response.get("reason"), "selector detail reason")
        if _estimate_output(estimate_output_tokens, {"reason": reason}) > caps.selection_text_tokens:
            raise ArbitrationAggregationError("selector detail reason exceeds its measured text allowance")
        _check_observed_output_size(response, estimate_output_tokens, int(planned["requested_output_tokens"]))
        detail_assessments.append({
            "detail_view_id": view_id,
            "detail_view_hash": expected_hash,
            "detail_view": _selector_detail_view_payload(view_row),
            "candidate_id": view_row["candidate_id"],
            "candidate_hash": view_row["candidate_hash"],
            "fact_kind": view_row["fact_kind"],
            "fact_id": view_row["fact_id"],
            "paper_key": view_row["paper_key"],
            "claim_slot_ids": list(view_row["claim_slot_ids"]),
            "claim_group_ids": list(view_row["claim_group_ids"]),
            "source_fact_hash": source_hash,
            "source_literal_fact": literal,
            "assessment_status": "resolved",
            "assessed_direction": direction,
            "assessed_disagreement": disagreement,
            "cited_detail_view_ids": [view_id],
            "reason": reason,
            "request_id": expected_node_id,
            "request_hash": actual_request_hash,
            "response_hash": compute_v3_hash(dict(response)),
        })
        request_hashes[expected_node_id] = actual_request_hash
        response_hashes[expected_node_id] = compute_v3_hash(dict(response))
    result = {
        "schema_version": SELECTION_DETAIL_RESULTS_SCHEMA,
        "work_plan_input_hash": plan["plan_input_hash"],
        "detail_work_plan_hash": work["work_plan_hash"],
        "prior_selector_node_id": selector_request["node_id"],
        "prior_selector_request_hash": selector_request["request_hash"],
        "prior_selection_result_hash": selection_hash,
        "root_binding": copy.deepcopy(dict(work["root_binding"])),
        "critical_view_set_hash": work["critical_view_set_hash"],
        "source_inventory_binding": copy.deepcopy(inventory_binding),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "requested_view_ids": list(requested_ids),
        "detail_assessments": detail_assessments,
        "counts": copy.deepcopy(dict(work["counts"])),
        "request_hashes": request_hashes,
        "response_hashes": response_hashes,
    }
    result["result_hash"] = compute_v3_hash(result)
    return _SelectorArtifact("selection_detail_results", result, {
        "plan_input_hash": plan["plan_input_hash"],
        "selector_request_seal": selector_seal,
        "selection_result_seal": selection_seal,
        "detail_work_plan_seal": work_seal,
        "detail_work_plan_hash": work["work_plan_hash"],
        "request_hashes": request_hashes,
        "response_hashes": response_hashes,
    }, _token=_SELECTOR_ARTIFACT_TOKEN)


def build_global_selection_followup_request(
    plan: Mapping[str, Any],
    selector_request: Mapping[str, Any],
    selection_result: Mapping[str, Any],
    detail_work_plan: Mapping[str, Any],
    detail_results: Mapping[str, Any],
    source_inventory: VerifiedWriterSourceInventoryV1,
    *,
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
    request_builder_id: str = "arbitration_selector_final_after_detail/v1",
) -> Mapping[str, Any]:
    """Build the single final selector request after every requested literal is READY."""

    caps.validate()
    requested_ids, view_rows, inventory_binding, selector_seal, selection_seal = _validated_selector_detail_origin(
        plan, selector_request, selection_result, source_inventory, caps,
    )
    selector_payload, _selector_seal, selector_provenance = _selector_artifact_payload(
        selector_request, "selection_request",
    )
    selection_payload, _selection_seal, selection_provenance = _selector_artifact_payload(
        selection_result, "selection_result",
    )
    work, work_seal, work_provenance = _validate_selector_detail_work_plan(detail_work_plan)
    detail_payload, detail_results_seal, detail_provenance = _selector_artifact_payload(
        detail_results, "selection_detail_results",
    )
    if (
        detail_payload.get("schema_version") != SELECTION_DETAIL_RESULTS_SCHEMA
        or detail_payload.get("result_hash") != compute_v3_hash({
            key: copy.deepcopy(value) for key, value in detail_payload.items() if key != "result_hash"
        })
        or detail_payload.get("work_plan_input_hash") != plan.get("plan_input_hash")
        or detail_payload.get("detail_work_plan_hash") != work.get("work_plan_hash")
        or detail_payload.get("prior_selector_node_id") != selector_payload.get("node_id")
        or detail_payload.get("prior_selector_request_hash") != selector_payload.get("request_hash")
        or detail_payload.get("prior_selection_result_hash") != selection_payload.get("result_hash")
        or detail_payload.get("root_binding") != work.get("root_binding")
        or detail_payload.get("critical_view_set_hash") != plan["critical_view_set"]["view_set_hash"]
        or detail_payload.get("source_inventory_binding") != inventory_binding
        or detail_payload.get("candidate_ids") != list(plan["candidate_ids"])
        or detail_payload.get("candidate_hashes") != dict(plan["candidate_hashes"])
        or detail_payload.get("requested_view_ids") != requested_ids
        or detail_payload.get("counts") != work.get("counts")
        or selector_provenance.get("request_hash") != selector_payload.get("request_hash")
        or selection_provenance.get("selector_request_seal") != selector_seal
        or work_provenance.get("selection_result_seal") != selection_seal
        or detail_provenance.get("plan_input_hash") != plan.get("plan_input_hash")
        or detail_provenance.get("selector_request_seal") != selector_seal
        or detail_provenance.get("selection_result_seal") != selection_seal
        or detail_provenance.get("detail_work_plan_seal") != work_seal
        or detail_provenance.get("detail_work_plan_hash") != work.get("work_plan_hash")
    ):
        raise ArbitrationAggregationError("selector detail results are foreign to the exact one-follow-up work plan")
    raw_assessments = detail_payload.get("detail_assessments")
    if not isinstance(raw_assessments, list) or len(raw_assessments) != len(requested_ids):
        raise ArbitrationAggregationError("selector detail results omit or add a requested source fact")
    detail_assessments: list[dict[str, Any]] = []
    for view_id, raw in zip(requested_ids, raw_assessments, strict=True):
        if not isinstance(raw, Mapping):
            raise ArbitrationAggregationError("selector detail assessment row is malformed")
        view_row = view_rows[view_id]
        literal = _selector_detail_literal(plan, view_row, source_inventory)
        if (
            raw.get("detail_view_id") != view_id
            or raw.get("detail_view_hash") != view_row.get("view_hash")
            or raw.get("detail_view") != _selector_detail_view_payload(view_row)
            or raw.get("candidate_id") != view_row.get("candidate_id")
            or raw.get("candidate_hash") != view_row.get("candidate_hash")
            or raw.get("fact_kind") != view_row.get("fact_kind")
            or raw.get("fact_id") != view_row.get("fact_id")
            or raw.get("paper_key") != view_row.get("paper_key")
            or raw.get("claim_slot_ids") != view_row.get("claim_slot_ids")
            or raw.get("claim_group_ids") != view_row.get("claim_group_ids")
            or raw.get("source_fact_hash") != view_row.get("source_fact_hash")
            or raw.get("source_literal_fact") != literal
            or raw.get("assessment_status") != "resolved"
            or raw.get("cited_detail_view_ids") != [view_id]
        ):
            raise ArbitrationAggregationError("selector detail assessment changed its source literal or scope")
        direction = _enum(raw.get("assessed_direction"), _DIRECTIONS - {"insufficient"}, "selector detail direction")
        disagreement = _enum(raw.get("assessed_disagreement"), _DISAGREEMENT, "selector detail disagreement")
        reason = _bounded_text(raw.get("reason"), "selector detail reason")
        request_hash = _required_hash(raw.get("request_hash"), "selector detail request hash")
        response_hash = _required_hash(raw.get("response_hash"), "selector detail response hash")
        detail_assessments.append({
            "detail_view_id": view_id,
            "detail_view_hash": str(view_row["view_hash"]),
            "detail_view": _selector_detail_view_payload(view_row),
            "candidate_id": str(view_row["candidate_id"]),
            "candidate_hash": str(view_row["candidate_hash"]),
            "source_fact_hash": str(view_row["source_fact_hash"]),
            "source_literal_fact": literal,
            "assessment_status": "resolved",
            "assessed_direction": direction,
            "assessed_disagreement": disagreement,
            "cited_detail_view_ids": [view_id],
            "reason": reason,
            "request_hash": request_hash,
            "response_hash": response_hash,
        })
    if detail_payload.get("request_hashes") != {
        str(row["request_id"]): str(row["request_hash"])
        for row in work["requests"]
    } or detail_payload.get("response_hashes") != {
        str(row["request_id"]): str(assessment["response_hash"])
        for row, assessment in zip(work["requests"], raw_assessments, strict=True)
    }:
        raise ArbitrationAggregationError("selector detail result hashes differ from their measured READY requests")
    if not isinstance(request_builder_id, str) or not request_builder_id.strip():
        raise ArbitrationAggregationError("final selector request_builder_id must be stable")
    base_request = selector_payload.get("request")
    if not isinstance(base_request, Mapping):
        raise ArbitrationAggregationError("prior selector actual request is missing")
    base_body = {
        key: copy.deepcopy(value)
        for key, value in base_request.items()
        if key not in {"_prompt_authority", "request_kind"}
    }
    if (
        base_body.get("schema_version") != SELECTION_INPUT_SCHEMA
        or base_body.get("node_id") != selector_payload.get("node_id")
        or base_body.get("candidate_ids") != list(plan["candidate_ids"])
        or base_body.get("candidate_hashes") != dict(plan["candidate_hashes"])
    ):
        raise ArbitrationAggregationError("prior selector body lost its original exact candidate scope")
    final_identity = {
        "schema_version": SELECTION_FINAL_INPUT_SCHEMA,
        "work_plan_input_hash": plan["plan_input_hash"],
        "prior_selector_node_id": selector_payload["node_id"],
        "prior_selector_request_hash": selector_payload["request_hash"],
        "prior_selection_result_hash": selection_payload["result_hash"],
        "root_binding": work["root_binding"],
        "detail_work_plan_hash": work["work_plan_hash"],
        "detail_results_hash": detail_payload["result_hash"],
        "requested_view_ids": list(requested_ids),
        "requested_view_hashes": dict(work["requested_view_hashes"]),
        "detail_response_hashes": dict(detail_payload["response_hashes"]),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "request_builder_id": request_builder_id,
        "route_identity": copy.deepcopy(dict(caps.route_identity)),
        "followup_ordinal": 1,
        "max_followups": 1,
    }
    node_id = "arbitration-selector-final:" + compute_v3_hash(final_identity)[:24]
    payload = copy.deepcopy(base_body)
    payload.update({
        "schema_version": SELECTION_FINAL_INPUT_SCHEMA,
        "node_id": node_id,
        "prior_selection_binding": {
            "selector_node_id": selector_payload["node_id"],
            "selector_request_hash": selector_payload["request_hash"],
            "selection_result_hash": selection_payload["result_hash"],
            "outcome": "detail_required",
            "requested_view_ids": list(requested_ids),
            "root_binding": copy.deepcopy(dict(work["root_binding"])),
        },
        "selector_detail_followup": {
            "detail_work_plan_hash": work["work_plan_hash"],
            "detail_results_hash": detail_payload["result_hash"],
            "detail_count": len(requested_ids),
            "max_detail_count": work["max_detail_count"],
            "followup_ordinal": 1,
            "max_followups": 1,
            "max_final_selector_calls": 1,
        },
        "detail_assessments": detail_assessments,
        "selection_policy": {
            **dict(base_body.get("selection_policy") or {}),
            "allowed_outcomes": ["selected"],
            "detail_required_ids_must_be_from_noncritical_fact_index": True,
            "critical_views_cannot_be_deferred": True,
            "candidates_are_never_dropped_during_assessment_or_reduction": True,
            "final_after_detail_required": True,
            "max_followups": 1,
        },
        "output_contract": {
            **_selection_output_contract(plan, 0, caps),
            "outcomes": ["selected"],
            "max_detail_required_view_ids": 0,
        },
    })
    shape = _selection_response_shape(plan, 0, caps)
    output_tokens = _estimate_output(estimate_output_tokens, shape)
    payload["output_contract"]["max_output_tokens"] = output_tokens
    request = _build_request(request_builder, SELECTION_FINAL_KIND, payload)
    input_tokens = _estimate_input(estimate_input_tokens, request)
    _assert_serialization_fits(input_tokens, output_tokens, caps, "final global selector after detail")
    request_hash = compute_v3_hash(request)
    measurement = {
        "request_hash": request_hash,
        "estimated_input_tokens": input_tokens,
        "effective_input_cap": caps.effective_input_cap,
        "requested_output_tokens": output_tokens,
        "output_reserve": {
            "response_schema": SELECTION_RESPONSE_SCHEMA,
            "response_shape_hash": compute_v3_hash(shape),
            "estimated_output_tokens": output_tokens,
            "max_output_tokens": caps.route_max_output,
        },
        "verified_context_limit": caps.verified_context_limit,
        "reasoning_reserve": caps.reasoning_reserve,
        "safety_margin": caps.safety_margin,
        "context_total_tokens": input_tokens + output_tokens + caps.reasoning_reserve + caps.safety_margin,
        "route_identity": copy.deepcopy(dict(caps.route_identity)),
    }
    final_record = {
        "schema_version": SELECTION_FINAL_INPUT_SCHEMA,
        "node_id": node_id,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "prior_selector_node_id": selector_payload["node_id"],
        "prior_selector_request_hash": selector_payload["request_hash"],
        "prior_selection_result_hash": selection_payload["result_hash"],
        "detail_work_plan_hash": work["work_plan_hash"],
        "detail_results_hash": detail_payload["result_hash"],
        "followup_ordinal": 1,
        "max_followups": 1,
        "request_builder_id": request_builder_id,
        "counts": {"Qfinal_selector": 1, "Qselector_detail": len(requested_ids)},
        "request": request,
        "request_hash": request_hash,
        "measurement": measurement,
        "estimated_input_tokens": input_tokens,
        "requested_output_tokens": output_tokens,
        "response_shape": shape,
        "max_detail_required_ids": 0,
        "noncritical_fact_ids": [],
    }
    return _SelectorArtifact("selection_request", final_record, {
        "plan_input_hash": plan["plan_input_hash"],
        "critical_view_set_hash": plan["critical_view_set"]["view_set_hash"],
        "root_node_id": work["root_binding"]["root_node_id"],
        "root_result_hash": work["root_binding"]["root_result_hash"],
        "request_hash": request_hash,
        "prior_selector_seal": selector_seal,
        "selection_result_seal": selection_seal,
        "detail_work_plan_seal": work_seal,
        "detail_results_seal": detail_results_seal,
        "followup_ordinal": 1,
        "max_followups": 1,
    }, _token=_SELECTOR_ARTIFACT_TOKEN)


def _validate_ready_legacy_inputs(
    legacy_plan: Mapping[str, Any],
    legacy_ledger: Mapping[str, Any],
    legacy_source_request: Mapping[str, Any],
    candidate_contents: Mapping[str, Mapping[str, Any]],
    candidate_scope: CandidateOutputScopeV1,
    review_intent: Any,
    critique_disposition: Mapping[str, Any],
) -> tuple[list[str], dict[str, str]]:
    if not isinstance(legacy_plan, Mapping) or not isinstance(legacy_ledger, Mapping):
        raise ArbitrationAggregationError("READY legacy plan and ledger must be mappings")
    if type(candidate_scope) is not CandidateOutputScopeV1:
        raise ArbitrationAggregationError("candidate_scope must be CandidateOutputScopeV1")
    if not isinstance(critique_disposition, Mapping) or critique_disposition.get("global_blocker") is True:
        raise ArbitrationAggregationError("cannot compile overflow assessment under a global critique blocker")
    candidate_ids = _ordered_unique_ids(legacy_plan.get("candidate_ids"), "legacy plan candidate_ids")
    if not candidate_ids:
        raise ArbitrationAggregationError("overflow assessment requires at least one eligible candidate")
    if critique_disposition.get("eligible_candidate_ids") != candidate_ids:
        raise ArbitrationAggregationError("legacy plan candidates differ from exact critique-eligible candidate IDs")
    hashes = _exact_hash_map(legacy_plan.get("candidate_hashes"), candidate_ids, "legacy plan candidate_hashes")
    if not isinstance(legacy_source_request, Mapping):
        raise ArbitrationAggregationError("legacy_source_request must be the exact prepared v1 source mapping")
    intent = review_intent.to_dict() if callable(getattr(review_intent, "to_dict", None)) else review_intent
    if not isinstance(intent, Mapping):
        raise ArbitrationAggregationError("review_intent must be an object")
    if legacy_source_request.get("candidate_ids") != candidate_ids:
        raise ArbitrationAggregationError("prepared legacy source candidate IDs differ from the READY plan")
    if legacy_source_request.get("candidate_hashes") != hashes:
        raise ArbitrationAggregationError("prepared legacy source candidate hashes differ from the READY plan")
    if legacy_source_request.get("candidate_contents") != candidate_contents:
        raise ArbitrationAggregationError("prepared legacy source candidate contents differ from READY candidate inputs")
    if legacy_source_request.get("critique_disposition") != critique_disposition:
        raise ArbitrationAggregationError("prepared legacy source critique disposition differs from READY disposition")
    if legacy_source_request.get("review_intent") != dict(intent):
        raise ArbitrationAggregationError("prepared legacy source intent differs from the supplied review intent")
    if legacy_ledger.get("candidate_ids") != candidate_ids:
        raise ArbitrationAggregationError("READY legacy ledger candidate IDs differ from legacy plan")
    canonical_ledger = _expand_and_validate_ledger(legacy_ledger)
    if canonical_ledger.get("plan_hash") != legacy_plan.get("plan_hash"):
        raise ArbitrationAggregationError("READY legacy ledger is not bound to the supplied hierarchy plan")
    if canonical_ledger.get("input_hash") != legacy_plan.get("input_hash"):
        raise ArbitrationAggregationError("READY legacy ledger input hash differs from its hierarchy plan")
    if canonical_ledger.get("provenance_hashes") != legacy_plan.get("provenance_hashes"):
        raise ArbitrationAggregationError("READY legacy ledger provenance differs from its hierarchy plan")
    if canonical_ledger.get("critique_hashes") != legacy_plan.get("critique_hashes"):
        raise ArbitrationAggregationError("READY legacy ledger critique hashes differ from its hierarchy plan")
    if compute_v3_hash(canonical_ledger.get("candidate_section_index")) != compute_v3_hash(legacy_plan.get("candidate_section_index")):
        raise ArbitrationAggregationError("READY legacy ledger section index differs from its hierarchy plan")
    try:
        from outline.candidate_arbitration_hierarchy import _validated_issues

        normalized_issues = _validated_issues(
            critique_disposition, candidate_ids, hashes,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAggregationError("critique disposition issue scope is malformed") from exc
    if normalized_issues != canonical_ledger.get("critique_issues"):
        raise ArbitrationAggregationError("READY legacy ledger issues differ from supplied critique disposition")
    _validate_legacy_leaf_partition(legacy_plan, canonical_ledger)
    if canonical_ledger.get("candidate_hashes") != hashes:
        raise ArbitrationAggregationError("READY legacy ledger candidate hashes differ from legacy plan")
    if not isinstance(candidate_contents, Mapping) or set(candidate_contents) != set(candidate_ids):
        raise ArbitrationAggregationError("candidate_contents must exactly match READY eligible candidate IDs")
    units = legacy_plan.get("units")
    leaves = legacy_plan.get("leaves")
    if not isinstance(units, list) or not units or not isinstance(leaves, list) or not leaves:
        raise ArbitrationAggregationError("READY legacy plan needs non-empty units and leaves")
    try:
        from outline.candidate_arbitration_hierarchy import _validate_plan_integrity

        _validate_plan_integrity(legacy_plan)
    except (TypeError, ValueError, KeyError, ImportError) as exc:
        raise ArbitrationAggregationError("READY legacy hierarchy plan failed its semantic integrity check") from exc
    return candidate_ids, hashes


def _validate_legacy_leaf_partition(legacy_plan: Mapping[str, Any], ledger: Mapping[str, Any]) -> None:
    raw_units = legacy_plan.get("units")
    if not isinstance(raw_units, list):
        raise ArbitrationAggregationError("READY legacy plan units must be an array")
    raw_leaves = legacy_plan.get("leaves")
    receipts = ledger.get("leaf_result_receipts")
    if not isinstance(raw_units, list) or not isinstance(raw_leaves, list) or not isinstance(receipts, list):
        raise ArbitrationAggregationError("legacy plan/ledger unit and receipt tables are malformed")
    unit_ids = [_nonempty(row.get("unit_id"), "legacy unit_id") for row in raw_units if isinstance(row, Mapping)]
    if len(unit_ids) != len(raw_units) or len(unit_ids) != len(set(unit_ids)):
        raise ArbitrationAggregationError("legacy hierarchy repeats or omits an assessment unit identity")
    leaf_by_id: dict[str, list[str]] = {}
    for row in raw_leaves:
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError("legacy hierarchy leaf row is malformed")
        leaf_id = _nonempty(row.get("leaf_id"), "legacy leaf_id")
        if leaf_id in leaf_by_id:
            raise ArbitrationAggregationError("legacy hierarchy repeats a leaf ID")
        assigned = _string_ids(row.get("unit_ids"), f"{leaf_id}.unit_ids", required=True)
        leaf_by_id[leaf_id] = assigned
    observed: list[str] = []
    for assigned in leaf_by_id.values():
        observed.extend(assigned)
    if len(observed) != len(set(observed)) or set(observed) != set(unit_ids):
        raise ArbitrationAggregationError("legacy leaf units are not a disjoint exhaustive partition")
    receipt_by_id: dict[str, Mapping[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ArbitrationAggregationError("legacy leaf result receipt is malformed")
        leaf_id = _nonempty(receipt.get("leaf_id"), "legacy receipt leaf_id")
        if leaf_id in receipt_by_id:
            raise ArbitrationAggregationError("legacy ledger repeats a leaf receipt")
        receipt_by_id[leaf_id] = receipt
    if set(receipt_by_id) != set(leaf_by_id):
        raise ArbitrationAggregationError("legacy ledger receipt IDs differ from its READY hierarchy leaves")
    for leaf_id, unit_ids_for_leaf in leaf_by_id.items():
        if receipt_by_id[leaf_id].get("unit_ids") != unit_ids_for_leaf:
            raise ArbitrationAggregationError(f"legacy receipt {leaf_id} unit assignment differs from hierarchy plan")


def _compile_legacy_units(
    *,
    legacy_plan: Mapping[str, Any],
    candidate_contents: Mapping[str, Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    candidate_scope: CandidateOutputScopeV1,
    ledger: Mapping[str, Any],
    review_group_map: Mapping[str, Any],
    view_set: Mapping[str, Any],
) -> list[dict[str, Any]]:
    scope_slots = {slot.claim_slot_id: slot for slot in candidate_scope.claim_slots}
    candidate_section_rows = ledger.get("candidate_section_index")
    if not isinstance(candidate_section_rows, Mapping) or set(candidate_section_rows) != set(candidate_ids):
        raise ArbitrationAggregationError("READY legacy section index does not exactly match candidate IDs")
    index_by_candidate = {
        candidate_id: _unique_rows(candidate_section_rows[candidate_id], "section_id", f"{candidate_id} section index")
        for candidate_id in candidate_ids
    }
    content_sections: dict[str, dict[str, Mapping[str, Any]]] = {}
    support_by_candidate: dict[str, dict[str, dict[str, Any]]] = {}
    usage_by_candidate: dict[str, dict[str, str]] = {candidate_id: {} for candidate_id in candidate_ids}
    for row in view_set.get("source_slot_scope_index") or ():
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError("critical view source-slot usage row is malformed")
        candidate_id = str(row.get("candidate_id") or "")
        slot_id = str(row.get("claim_slot_id") or "")
        status = row.get("usage_status")
        if (
            candidate_id not in usage_by_candidate
            or slot_id not in scope_slots
            or slot_id in usage_by_candidate[candidate_id]
            or status not in {"used", "not_used_optional"}
        ):
            raise ArbitrationAggregationError("critical view source-slot usage index is foreign or malformed")
        usage_by_candidate[candidate_id][slot_id] = str(status)
    for candidate_id in candidate_ids:
        if set(usage_by_candidate[candidate_id]) != set(scope_slots):
            raise ArbitrationAggregationError(
                f"critical view usage index is not an exact available-slot catalog for {candidate_id}"
            )
    for candidate_id in candidate_ids:
        content = candidate_contents[candidate_id]
        if not isinstance(content, Mapping):
            raise ArbitrationAggregationError(f"candidate {candidate_id} content must be an object")
        try:
            candidate_scope.validate(content)
        except (TypeError, ValueError, KeyError) as exc:
            raise ArbitrationAggregationError(f"candidate {candidate_id} is not source-closed under CandidateOutputScopeV1") from exc
        sections = _unique_rows(content.get("sections"), "section_id", f"{candidate_id} candidate sections")
        content_sections[candidate_id] = sections
        if set(sections) != set(index_by_candidate[candidate_id]):
            raise ArbitrationAggregationError(f"candidate {candidate_id} sections differ from READY legacy ledger")
        support_by_candidate[candidate_id] = {}
        for section_id, section in sections.items():
            index_row = index_by_candidate[candidate_id][section_id]
            section_hash = compute_v3_hash(dict(section))
            if index_row.get("section_hash") != section_hash:
                raise ArbitrationAggregationError(f"candidate {candidate_id} section hash is stale")
            for support in section.get("claim_support") or ():
                slot_id = str(support.get("claim_slot_id") or "")
                slot = scope_slots.get(slot_id)
                if slot is None or slot_id in support_by_candidate[candidate_id]:
                    raise ArbitrationAggregationError(f"candidate {candidate_id} has foreign/duplicate support slot {slot_id}")
                _validate_support_binding(support, slot, f"{candidate_id}.{slot_id}")
                support_by_candidate[candidate_id][slot_id] = {
                    "support": copy.deepcopy(dict(support)),
                    "section_id": section_id,
                    "section_hash": section_hash,
                    "section": section,
                }
        expected_used_slots = {
            slot_id for slot_id, status in usage_by_candidate[candidate_id].items() if status == "used"
        }
        if not expected_used_slots or set(support_by_candidate[candidate_id]) != expected_used_slots:
            raise ArbitrationAggregationError(
                f"candidate {candidate_id} support slots differ from its exact used-slot index"
            )

    raw_units = legacy_plan.get("units")
    if not isinstance(raw_units, list):
        raise ArbitrationAggregationError("READY legacy plan units must be an array")
    units: list[dict[str, Any]] = []
    all_covered_slots: dict[str, set[str]] = {candidate_id: set() for candidate_id in candidate_ids}
    views_by_candidate_slot: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in view_set["critical_views"]:
        slot_id = row["source_bindings"]["claim_slot_id"]
        views_by_candidate_slot.setdefault((row["candidate_id"], slot_id), []).append(row)

    for ordinal, raw_unit in enumerate(raw_units):
        if not isinstance(raw_unit, Mapping):
            raise ArbitrationAggregationError(f"legacy unit {ordinal} is malformed")
        unit_id = _nonempty(raw_unit.get("unit_id"), f"legacy unit {ordinal}.unit_id")
        sections_map = raw_unit.get("sections_by_candidate")
        if not isinstance(sections_map, Mapping) or set(sections_map) != set(candidate_ids):
            raise ArbitrationAggregationError(f"legacy unit {unit_id} lacks exact candidate section assignments")
        candidate_rows: dict[str, dict[str, Any]] = {}
        for candidate_id in candidate_ids:
            raw_section_rows = sections_map[candidate_id]
            if not isinstance(raw_section_rows, list):
                raise ArbitrationAggregationError(f"legacy unit {unit_id}.{candidate_id} sections must be an array")
            unit_sections: list[dict[str, Any]] = []
            unit_slot_ids: list[str] = []
            view_ids: list[str] = []
            evidence_ids: set[str] = set()
            for section_row in raw_section_rows:
                if not isinstance(section_row, Mapping):
                    raise ArbitrationAggregationError(f"legacy unit {unit_id} section binding is malformed")
                section_id = _nonempty(section_row.get("section_id"), "legacy unit section_id")
                source = content_sections[candidate_id].get(section_id)
                if source is None:
                    raise ArbitrationAggregationError(f"legacy unit {unit_id} references a foreign section")
                section_hash = str(section_row.get("section_hash") or "")
                if section_hash != compute_v3_hash(dict(source)):
                    raise ArbitrationAggregationError(f"legacy unit {unit_id} has a stale section hash")
                all_support = list(source.get("claim_support") or ())
                assigned = section_row.get("claim_slot_ids")
                if assigned is None:
                    selected_support = all_support
                else:
                    assigned_ids = _string_ids(assigned, f"{unit_id}.{section_id}.claim_slot_ids", required=True)
                    selected_support = [row for row in all_support if str(row.get("claim_slot_id") or "") in set(assigned_ids)]
                    if {str(row.get("claim_slot_id") or "") for row in selected_support} != set(assigned_ids):
                        raise ArbitrationAggregationError(f"legacy unit {unit_id} claim-support part is not source-exhaustive")
                selected_slots = [str(row.get("claim_slot_id") or "") for row in selected_support]
                if not selected_slots or len(selected_slots) != len(set(selected_slots)):
                    raise ArbitrationAggregationError(f"legacy unit {unit_id} has no unique assigned support slot")
                if set(selected_slots).intersection(all_covered_slots[candidate_id]):
                    raise ArbitrationAggregationError("legacy unit partition overlaps a source support slot")
                all_covered_slots[candidate_id].update(selected_slots)
                unit_slot_ids.extend(selected_slots)
                claim_texts = _ordered_unique_texts([str(row.get("claim") or "") for row in selected_support])
                support_payload = [copy.deepcopy(dict(row)) for row in selected_support]
                for support in selected_support:
                    support_evidence_ids = _string_ids(
                        support.get("evidence_ids"),
                        f"{candidate_id}.{support.get('claim_slot_id')}.evidence_ids",
                    )
                    evidence_ids.update(support_evidence_ids)
                    for view in views_by_candidate_slot.get((candidate_id, str(support.get("claim_slot_id") or "")), ()):
                        view_ids.append(str(view["decision_view_id"]))
                unit_sections.append({
                    "section_id": section_id,
                    "section_hash": section_hash,
                    "task_ids": _string_ids(section_row.get("task_ids", source.get("task_ids") or []), f"{unit_id}.{section_id}.task_ids"),
                    "paper_keys": _string_ids(source.get("paper_keys") or [], f"{unit_id}.{section_id}.paper_keys"),
                    "relation_ids": _string_ids(source.get("relation_ids") or [], f"{unit_id}.{section_id}.relation_ids"),
                    "claim_texts": claim_texts,
                    "claim_support": support_payload,
                    "claim_slot_ids": selected_slots,
                })
            candidate_rows[candidate_id] = {
                "presence": "present" if unit_sections else "absent",
                "sections": unit_sections,
                "claim_slot_ids": sorted(unit_slot_ids),
                "decision_view_ids": sorted(set(view_ids)),
                "evidence_ids": sorted(evidence_ids),
                "section_bindings": [
                    {"section_id": row["section_id"], "section_hash": row["section_hash"]}
                    for row in unit_sections
                ],
            }
        unit_hash = compute_v3_hash({
            "legacy_unit": copy.deepcopy(dict(raw_unit)),
            "candidate_rows": candidate_rows,
        })
        unit_slots = sorted({
            slot_id
            for row in candidate_rows.values()
            for slot_id in row["claim_slot_ids"]
        })
        units.append({
            "ordinal": ordinal,
            "unit_id": unit_id,
            "unit_hash": unit_hash,
            "source_slot_bindings": [scope_slots[slot_id].to_dict() for slot_id in unit_slots],
            "candidate_rows": candidate_rows,
            "candidate_ids": list(candidate_ids),
        })
    if not units:
        raise ArbitrationAggregationError("READY legacy plan has no atomic source assessment units")
    for candidate_id in candidate_ids:
        if all_covered_slots[candidate_id] != set(support_by_candidate[candidate_id]):
            raise ArbitrationAggregationError(
                f"legacy units do not form an exact used source-slot partition for {candidate_id}"
            )

    _assign_unit_groups(units, candidate_ids, support_by_candidate, review_group_map)
    return units


def _assign_unit_groups(
    units: list[dict[str, Any]],
    candidate_ids: Sequence[str],
    support_by_candidate: Mapping[str, Mapping[str, Mapping[str, Any]]],
    group_map: Mapping[str, Any],
) -> None:
    claim_group_task = group_map["claim_group_task_bindings"]
    claim_group_slots = group_map["claim_group_slot_bindings"]
    slot_to_group: dict[str, str] = {}
    for claim_group_id, slot_ids in claim_group_slots.items():
        for slot_id in slot_ids:
            if slot_id in slot_to_group:
                raise ArbitrationAggregationError(f"claim slot {slot_id} maps to multiple source claim groups")
            slot_to_group[str(slot_id)] = str(claim_group_id)
    if not slot_to_group:
        raise ArbitrationAggregationError("CandidateOutputScopeV1 has no authoritative claim-slot/group mapping")
    comparison_groups = group_map["comparison_relation_bindings"]
    issue_rows = group_map["issue_rows"]
    comparison_group_by_source = {source_group: comparison_id for comparison_id, source_group in comparison_groups.items()}
    issue_matches: dict[tuple[str, str], bool] = {}
    for unit in units:
        unit["group_ids_by_candidate"] = {}
        unit["group_views_by_candidate"] = {}
        unit["group_evidence_by_candidate"] = {}
        unit["issue_rows_by_candidate"] = {}
    for candidate_id in candidate_ids:
        for unit_index, unit in enumerate(units):
            slots = set(unit["candidate_rows"][candidate_id]["claim_slot_ids"])
            if not slots.issubset(slot_to_group):
                raise ArbitrationAggregationError("legacy unit references a claim slot outside CandidateOutputScopeV1")
            claim_groups = {slot_to_group[slot_id] for slot_id in slots}
            for slot_id in slots:
                support = support_by_candidate[candidate_id][slot_id]["support"]
                supplied_group_id = support.get("claim_group_id")
                if supplied_group_id is not None and supplied_group_id != slot_to_group[slot_id]:
                    raise ArbitrationAggregationError(
                        f"{candidate_id}.{slot_id} claim_group_id differs from CandidateOutputScopeV1"
                    )
                supplied_assertion = support.get("synthesis_claim_id")
                expected_assertion = (group_map.get("claim_group_assertion_bindings") or {}).get(slot_to_group[slot_id])
                if supplied_assertion is not None and supplied_assertion != expected_assertion:
                    raise ArbitrationAggregationError(
                        f"{candidate_id}.{slot_id} synthesis_claim_id differs from its source claim group"
                    )
            group_ids: set[str] = set()
            for claim_group_id in claim_groups:
                task_id = claim_group_task.get(claim_group_id)
                if task_id:
                    group_ids.add(f"task:{task_id}")
                comparison_id = comparison_group_by_source.get(claim_group_id)
                if comparison_id:
                    group_ids.add(comparison_id)
            unit_sections = unit["candidate_rows"][candidate_id]["sections"]
            section_ids = {row["section_id"] for row in unit_sections}
            claim_ids: set[str] = set()
            support_ids: set[str] = set(slots)
            for slot_id in slots:
                slot_row = support_by_candidate[candidate_id][slot_id]["support"]
                support_ids.update(str(value) for key, value in slot_row.items() if key.endswith("_id") and isinstance(value, str) and value)
                for field in ("source_claim_ids", "evidence_ids", "source_field_ids", "relation_ids"):
                    support_ids.update(str(value) for value in slot_row.get(field) or () if str(value))
                claim_ids.update(_section_claim_ids(support_by_candidate[candidate_id][slot_id]["section"]))
            for issue_id, issue in issue_rows.items():
                issue_group_id = f"issue:{issue_id}"
                issue_candidate = str(issue.get("candidate_id") or "")
                scope = str(issue.get("scope") or "")
                targets = {str(value) for value in issue.get("target_ids") or ()}
                if issue_candidate and issue_candidate != candidate_id:
                    continue
                matched = (
                    (scope == "global" and unit_index == 0)
                    or (scope == "candidate" and unit_index == 0 and (not targets or candidate_id in targets))
                    or (scope == "section" and bool(targets & section_ids))
                    or (scope == "claim" and bool(targets & (claim_ids | support_ids)))
                )
                if matched:
                    group_ids.add(issue_group_id)
                    issue_matches[(candidate_id, issue_group_id)] = True
                    unit["issue_rows_by_candidate"].setdefault(candidate_id, {})[issue_id] = copy.deepcopy(dict(issue))
            unit["group_ids_by_candidate"][candidate_id] = sorted(group_ids)
            unit["group_views_by_candidate"][candidate_id] = {
                group_id: sorted(
                    view_id for view_id in unit["candidate_rows"][candidate_id]["decision_view_ids"]
                    if any(
                        row["decision_view_id"] == view_id and group_id in row["group_ids"]
                        for row in group_map.get("_critical_views", ())
                    )
                )
                for group_id in sorted(group_ids)
            }
            # The exact view table is bound separately in the plan; set these
            # references after the per-unit membership has been computed.
            unit["group_evidence_by_candidate"][candidate_id] = {
                group_id: sorted(unit["candidate_rows"][candidate_id]["evidence_ids"])
                for group_id in sorted(group_ids)
            }

    # Global/candidate issues without an explicit section target still need an
    # explicit review unit. Assign them to the first canonical unit for their
    # applicable candidate(s), preserving W without making another input call.
    for issue_id, issue in issue_rows.items():
        group_id = f"issue:{issue_id}"
        scope = str(issue.get("scope") or "")
        if scope not in {"global", "candidate"}:
            continue
        for candidate_id in candidate_ids:
            issue_candidate = str(issue.get("candidate_id") or "")
            if issue_candidate and issue_candidate != candidate_id:
                continue
            if (candidate_id, group_id) in issue_matches:
                continue
            first = next((row for row in units if row["candidate_rows"][candidate_id]["presence"] == "present"), None)
            if first is None:
                raise ArbitrationAggregationError(f"issue {issue_id} has no source unit for {candidate_id}")
            first["group_ids_by_candidate"][candidate_id] = sorted(set(first["group_ids_by_candidate"][candidate_id]) | {group_id})
            first["issue_rows_by_candidate"].setdefault(candidate_id, {})[issue_id] = copy.deepcopy(dict(issue))
            first["group_evidence_by_candidate"][candidate_id][group_id] = sorted(
                first["candidate_rows"][candidate_id]["evidence_ids"]
            )

    # Every source assertion and support slot must reach exactly its enforced
    # task summary. This is compiler coverage, not a model-authored count.
    task_groups = {
        str(row["group_id"]): row
        for row in group_map["groups"]
        if row.get("group_kind") == "task_topic"
    }
    group_for_slot = {slot_id: group_id for slot_id, group_id in slot_to_group.items()}
    for candidate_id in candidate_ids:
        covered_claim_groups: set[str] = set()
        for unit in units:
            for slot_id in unit["candidate_rows"][candidate_id]["claim_slot_ids"]:
                covered_claim_groups.add(group_for_slot[slot_id])
        expected_claim_groups = {
            group_for_slot[slot_id] for slot_id in support_by_candidate[candidate_id]
        }
        if covered_claim_groups != expected_claim_groups:
            raise ArbitrationAggregationError(
                f"{candidate_id} legacy units omit or add used source claim groups"
            )
        for task_group_id, task_group in task_groups.items():
            expected_groups = {
                group_id for group_id, task_id in claim_group_task.items()
                if f"task:{task_id}" == task_group_id and group_id in expected_claim_groups
            }
            observed_groups = {
                group_id for group_id in covered_claim_groups
                if claim_group_task.get(group_id) == task_group.get("task_id")
            }
            if observed_groups != expected_groups:
                raise ArbitrationAggregationError(
                    f"{candidate_id}.{task_group_id} does not cover every source claim group"
                )

    for unit in units:
        unit["group_views_by_candidate"] = {
            candidate_id: {
                group_id: sorted(
                    view_id for view_id in unit["candidate_rows"][candidate_id]["decision_view_ids"]
                    if any(
                        view["decision_view_id"] == view_id and group_id in view["group_ids"]
                        for view in group_map.get("_compiled_critical_views", ())
                    )
                )
                for group_id in unit["group_ids_by_candidate"][candidate_id]
            }
            for candidate_id in candidate_ids
        }


def _attach_group_views(
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    view_set: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind each new source unit to its exact literal views and group facts."""

    by_candidate_slot: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for view in view_set.get("critical_views") or ():
        slot_id = str(view["source_bindings"]["claim_slot_id"])
        by_candidate_slot.setdefault((str(view["candidate_id"]), slot_id), []).append(view)
    result = [copy.deepcopy(dict(unit)) for unit in units]
    for unit in result:
        unit["group_views_by_candidate"] = {}
        for candidate_id in candidate_ids:
            candidate_row = unit["candidate_rows"][candidate_id]
            slot_ids = set(candidate_row["claim_slot_ids"])
            views = [
                view
                for slot_id in sorted(slot_ids)
                for view in by_candidate_slot.get((candidate_id, slot_id), ())
            ]
            candidate_row["decision_view_ids"] = [str(view["decision_view_id"]) for view in views]
            candidate_row["view_shape_rows"] = [
                (
                    str(view["decision_view_id"]),
                    str(view["decision_view_hash"]),
                    _literal_view_ref_ids(view),
                )
                for view in views
            ]
            unit["group_views_by_candidate"][candidate_id] = {
                group_id: sorted(
                    str(view["decision_view_id"])
                    for view in views if group_id in view.get("group_ids", [])
                )
                for group_id in unit["group_ids_by_candidate"][candidate_id]
            }
            # Candidate scope binds the exact evidence row to each slot. The
            # group contribution may cite only evidence assigned to these
            # units, never a fact reached through a model-authored hash.
            slot_evidence = {
                str(support.get("claim_slot_id") or ""): {str(value) for value in support.get("evidence_ids") or ()}
                for section in candidate_row["sections"]
                for support in section["claim_support"]
            }
            unit["group_evidence_by_candidate"][candidate_id] = {
                group_id: sorted({
                    evidence_id
                    for slot_id in slot_ids
                    if group_id in unit["group_ids_by_candidate"][candidate_id]
                    for evidence_id in slot_evidence.get(slot_id, set())
                })
                for group_id in unit["group_ids_by_candidate"][candidate_id]
            }
    return result


def _pack_assessment_units(
    *,
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    identity: Mapping[str, Any],
    plan_input_hash: str,
    group_map: Mapping[str, Any],
    view_set: Mapping[str, Any],
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
    request_builder_id: str,
) -> list[dict[str, Any]]:
    view_by_id = {row["decision_view_id"]: row for row in view_set["critical_views"]}
    unit_by_id = {row["unit_id"]: row for row in units}
    critical_by_candidate_slot = {
        (row["candidate_id"], row["source_bindings"]["claim_slot_id"]): row
        for row in view_set["critical_views"]
    }
    unit_enriched: list[dict[str, Any]] = []
    for raw in units:
        unit = copy.deepcopy(dict(raw))
        for candidate_id in candidate_ids:
            unit["group_views_by_candidate"][candidate_id] = {}
            slots = set(unit["candidate_rows"][candidate_id]["claim_slot_ids"])
            group_ids = unit["group_ids_by_candidate"][candidate_id]
            for group_id in group_ids:
                unit["group_views_by_candidate"][candidate_id][group_id] = sorted(
                    view["decision_view_id"]
                    for (cid, slot_id), view in critical_by_candidate_slot.items()
                    if cid == candidate_id and slot_id in slots and group_id in view["group_ids"]
                )
        unit_enriched.append(unit)

    packed: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []
    for unit in unit_enriched:
        trial = [*current, unit]
        try:
            _materialize_assessment_leaf(
                trial, candidate_ids, candidate_hashes, identity, plan_input_hash,
                group_map, view_by_id, view_set, request_builder, estimate_input_tokens,
                estimate_output_tokens, caps, request_builder_id,
            )
        except ArbitrationSerializationError as exc:
            if not current:
                raise ArbitrationSerializationError(
                    f"indivisible assessment unit {unit['unit_id']} cannot fit dual input/output caps: {exc}"
                ) from exc
            packed.append(_materialize_assessment_leaf(
                current, candidate_ids, candidate_hashes, identity, plan_input_hash,
                group_map, view_by_id, view_set, request_builder, estimate_input_tokens,
                estimate_output_tokens, caps, request_builder_id,
            ))
            current = [unit]
            try:
                _materialize_assessment_leaf(
                    current, candidate_ids, candidate_hashes, identity, plan_input_hash,
                    group_map, view_by_id, view_set, request_builder, estimate_input_tokens,
                    estimate_output_tokens, caps, request_builder_id,
                )
            except ArbitrationSerializationError as singleton_exc:
                raise ArbitrationSerializationError(
                    f"indivisible assessment unit {unit['unit_id']} cannot fit dual input/output caps: {singleton_exc}"
                ) from singleton_exc
        else:
            current = trial
    if current:
        packed.append(_materialize_assessment_leaf(
            current, candidate_ids, candidate_hashes, identity, plan_input_hash,
            group_map, view_by_id, view_set, request_builder, estimate_input_tokens,
            estimate_output_tokens, caps, request_builder_id,
        ))
    if not packed or len(packed) > len(unit_by_id):
        raise ArbitrationAggregationError("assessment packing made no progress or exceeded atomic unit bound")
    return packed


def _materialize_assessment_leaf(
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    identity: Mapping[str, Any],
    plan_input_hash: str,
    group_map: Mapping[str, Any],
    view_by_id: Mapping[str, Mapping[str, Any]],
    view_set: Mapping[str, Any],
    request_builder: RequestBuilder,
    estimate_input_tokens: InputEstimator,
    estimate_output_tokens: OutputEstimator,
    caps: ArbitrationSerializationCaps,
    request_builder_id: str,
) -> dict[str, Any]:
    unit_ids = [str(row["unit_id"]) for row in units]
    unit_hashes = [str(row["unit_hash"]) for row in units]
    node_id = "arbitration-assessment:" + compute_v3_hash({
        "schema_version": ASSESSMENT_INPUT_SCHEMA,
        "plan_input_hash": plan_input_hash,
        "request_builder_id": request_builder_id,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": dict(candidate_hashes),
        "ordered_unit_ids": unit_ids,
        "ordered_unit_hashes": unit_hashes,
        "route_identity": copy.deepcopy(dict(caps.route_identity)),
    })[:24]
    selected_view_ids = sorted({
        view_id for unit in units for candidate_id in candidate_ids
        for view_id in unit["candidate_rows"][candidate_id]["decision_view_ids"]
    })
    view_wire = pack_arbitration_critical_view_wire(
        view_set, selected_view_ids,
        expected_view_set_hash=str(identity["critical_view_set_hash"]),
    )
    wire_view_by_id = {row["decision_view_id"]: row for row in view_wire["views"]}
    groups_by_id = {row["group_id"]: row for row in group_map["groups"]}
    source_slots: dict[str, dict[str, Any]] = {}
    for unit in units:
        for binding in unit["source_slot_bindings"]:
            slot_id = str(binding["claim_slot_id"])
            prior = source_slots.get(slot_id)
            if prior is not None and prior != binding:
                raise ArbitrationAggregationError(f"source slot {slot_id} has conflicting ready bindings")
            source_slots[slot_id] = copy.deepcopy(dict(binding))
    candidate_rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        by_unit = []
        for unit in units:
            row = unit["candidate_rows"][candidate_id]
            by_unit.append({
                "unit_id": unit["unit_id"],
                "unit_hash": unit["unit_hash"],
                "presence": row["presence"],
                "section_bindings": copy.deepcopy(row["section_bindings"]),
                "sections": [
                    {
                        "section_id": section["section_id"],
                        "section_hash": section["section_hash"],
                        "task_ids": list(section["task_ids"]),
                        "paper_keys": list(section["paper_keys"]),
                        "relation_ids": list(section["relation_ids"]),
                        "claim_slots": [
                            {
                                "claim_slot_id": str(support["claim_slot_id"]),
                                "claim": str(support["claim"]),
                            }
                            for support in section["claim_support"]
                        ],
                    }
                    for section in row["sections"]
                ],
                "decision_views": [
                    copy.deepcopy(wire_view_by_id[view_id]) for view_id in row["decision_view_ids"]
                ],
                "group_ids": list(unit["group_ids_by_candidate"][candidate_id]),
                "group_views": copy.deepcopy(unit["group_views_by_candidate"][candidate_id]),
                "issue_rows": copy.deepcopy(unit.get("issue_rows_by_candidate", {}).get(candidate_id, {})),
            })
        candidate_rows.append({
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hashes[candidate_id],
            "unit_rows": by_unit,
        })
    group_ids = sorted({
        group_id
        for unit in units
        for candidate_id in candidate_ids
        for group_id in unit["group_ids_by_candidate"][candidate_id]
    })
    payload = {
        "schema_version": ASSESSMENT_INPUT_SCHEMA,
        "node_id": node_id,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": dict(candidate_hashes),
        "decision_scope": {
            "schema_version": DECISION_SCOPE_SCHEMA,
            "candidate_ids": list(candidate_ids),
            "candidate_hashes": dict(candidate_hashes),
            "unit_ids": unit_ids,
            "unit_hashes": unit_hashes,
            "review_group_map_hash": group_map["group_map_hash"],
            "candidate_output_scope_hash": identity["candidate_output_scope_hash"],
            "source_inventory_binding": copy.deepcopy(identity["source_inventory_binding"]),
        },
        "legacy_binding": {
            "legacy_plan_hash": identity["legacy_plan_hash"],
            "legacy_ledger_hash": identity["legacy_ledger_hash"],
            "legacy_ledger_wire_hash": identity["legacy_ledger_wire_hash"],
            "legacy_v1_inputs_are_immutable": True,
            "legacy_free_prose_is_not_a_typed_judgment": True,
        },
        "review_group_map": [copy.deepcopy(groups_by_id[group_id]) for group_id in group_ids],
        "source_support_catalog": [source_slots[key] for key in sorted(source_slots)],
        "source_support_catalog_hash": compute_v3_hash([source_slots[key] for key in sorted(source_slots)]),
        "critical_view_wire": view_wire,
        "candidate_units": candidate_rows,
        "required_candidate_ids": list(candidate_ids),
        "required_unit_ids": unit_ids,
        "output_contract": {
            "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
            "candidate_ids": list(candidate_ids),
            "unit_ids": unit_ids,
            "assessed_direction_values": sorted(_DIRECTIONS),
            "assessed_disagreement_values": sorted(_DISAGREEMENT),
            "max_reason_tokens_per_assessment": caps.assessment_text_tokens,
            "max_text_tokens_per_group_contribution": caps.group_summary_text_tokens,
            "max_output_tokens": 1,
            "no_layout_fields": True,
        },
    }
    canonical_response_shape = _assessment_response_shape(
        units, candidate_ids, candidate_hashes, node_id, caps, group_map, view_by_id,
    )
    wire_catalog: dict[str, Any] | None = None
    response_shape = canonical_response_shape
    if caps.assessment_wire_enabled:
        try:
            _preview_wire_request, response_shape, _preview_catalog = project_assessment_wire(
                payload, canonical_response_shape,
            )
        except ArbitrationAssessmentWireError as exc:
            raise ArbitrationAggregationError("assessment wire projection failed") from exc
    output_tokens = _estimate_output(estimate_output_tokens, response_shape)
    payload["output_contract"]["max_output_tokens"] = output_tokens
    request_payload: Mapping[str, Any] = payload
    if caps.assessment_wire_enabled:
        try:
            request_payload, response_shape, wire_catalog = project_assessment_wire(
                payload, canonical_response_shape,
            )
        except ArbitrationAssessmentWireError as exc:
            raise ArbitrationAggregationError("assessment wire projection changed during materialization") from exc
    request = _build_request(request_builder, "arbitration_leaf_assessment_v2", request_payload)
    input_tokens = _estimate_input(estimate_input_tokens, request)
    _assert_serialization_fits(input_tokens, output_tokens, caps, f"assessment leaf {node_id}")
    expected_assessments = _expected_assessment_rows(units, candidate_ids, candidate_hashes, view_by_id)
    expected_group_contributions = _expected_leaf_group_contributions(units, candidate_ids, group_map, view_by_id)
    result = {
        "node_id": node_id,
        "schema_version": ASSESSMENT_INPUT_SCHEMA,
        "request_builder_id": request_builder_id,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": dict(candidate_hashes),
        "unit_ids": unit_ids,
        "unit_hashes": unit_hashes,
        "min_unit_ordinal": min(int(row["ordinal"]) for row in units),
        "request": request,
        "request_hash": compute_v3_hash(request),
        "estimated_input_tokens": input_tokens,
        "requested_output_tokens": output_tokens,
        "response_shape": response_shape,
        "expected_assessments": [
            {
                "candidate_id": candidate_id,
                "unit_id": unit_id,
                **copy.deepcopy(expected_assessments[(candidate_id, unit_id)]),
            }
            for candidate_id in candidate_ids
            for unit_id in unit_ids
        ],
        "expected_group_contributions": expected_group_contributions,
    }
    if wire_catalog is not None:
        result.update({
            "wire_schema_version": ASSESSMENT_WIRE_SCHEMA,
            "canonical_request": copy.deepcopy(payload),
            "canonical_request_hash": wire_catalog["canonical_request_hash"],
            "wire_request_body_hash": wire_catalog["wire_request_body_hash"],
            "wire_catalog": wire_catalog,
            "canonical_response_shape": copy.deepcopy(canonical_response_shape),
            "canonical_response_shape_hash": wire_catalog["canonical_response_shape_hash"],
            "wire_response_shape_hash": wire_catalog["wire_response_shape_hash"],
        })
    return result


def _assessment_response_shape(
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    node_id: str,
    caps: ArbitrationSerializationCaps,
    group_map: Mapping[str, Any],
    view_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    assessment_rows = []
    for candidate_id in candidate_ids:
        for unit in units:
            candidate = unit["candidate_rows"][candidate_id]
            assessment_rows.append({
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hashes[candidate_id],
                "unit_id": unit["unit_id"],
                "unit_hash": unit["unit_hash"],
                "presence": candidate["presence"],
                "section_bindings": copy.deepcopy(candidate["section_bindings"]),
                "assessed_direction": "supports",
                "assessed_disagreement": "consistent",
                "reason": _token_fill(caps.assessment_text_tokens),
                "evidence_refs": list(candidate["evidence_ids"]),
                "counterevidence_refs": list(candidate["evidence_ids"]),
                "view_assessments": [
                    {
                        "decision_view_id": view_id,
                        "decision_view_hash": view_hash,
                        "assessed_direction": "supports",
                        "assessed_disagreement": "consistent",
                        "reason": _token_fill(caps.assessment_text_tokens),
                        "evidence_refs": list(literal_ref_ids),
                        "counterevidence_refs": list(literal_ref_ids),
                    }
                    for view_id, view_hash, literal_ref_ids in candidate.get("view_shape_rows", [])
                ],
            })
    group_hashes = {row["group_id"]: compute_v3_hash(row) for row in group_map["groups"]}
    group_rows = []
    for expected in _expected_leaf_group_contributions(units, candidate_ids, group_map, view_by_id):
        candidate_id = expected["candidate_id"]
        group_rows.append({
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hashes[candidate_id],
            "group_id": expected["group_id"],
            "group_hash": group_hashes[expected["group_id"]],
            "unit_ids": list(expected["unit_ids"]),
            "decision_view_ids": list(expected["decision_view_ids"]),
            "assessed_direction": "supports",
            "assessed_disagreement": "consistent",
            "summary": _token_fill(caps.group_summary_text_tokens),
            "evidence_refs": list(expected["evidence_refs"]),
            "counterevidence_refs": list(expected["evidence_refs"]),
        })
    return {
        "schema_version": ASSESSMENT_RESPONSE_SCHEMA,
        "node_id": node_id,
        "candidate_ids": list(candidate_ids),
        "candidate_hashes": dict(candidate_hashes),
        "unit_ids": [str(row["unit_id"]) for row in units],
        "assessments": assessment_rows,
        "group_contributions": group_rows,
    }


def _expected_assessment_rows(
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    view_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate_id in candidate_ids:
        for unit in units:
            row = unit["candidate_rows"][candidate_id]
            decisions = []
            for view_id in row["decision_view_ids"]:
                view = view_by_id[view_id]
                decisions.append({
                    "decision_view_id": view_id,
                    "decision_view_hash": view["decision_view_hash"],
                    "literal_view_ref_ids": _literal_view_ref_ids(view),
                })
            expected[(candidate_id, unit["unit_id"])] = {
                "presence": row["presence"],
                "unit_hash": unit["unit_hash"],
                "section_bindings": copy.deepcopy(row["section_bindings"]),
                "evidence_ref_ids": list(row["evidence_ids"]),
                "decision_views": decisions,
            }
    return expected


def _literal_view_ref_ids(view: Mapping[str, Any]) -> list[str]:
    detail = view.get("literal_critical_detail")
    if not isinstance(detail, Mapping):
        raise ArbitrationAggregationError("critical view has no literal detail table")
    refs = {
        str(row["claim_id"])
        for row in detail.get("source_claims") or ()
        if isinstance(row, Mapping) and row.get("claim_id")
    }
    refs.update(
        str(row["evidence_id"])
        for row in detail.get("evidence") or ()
        if isinstance(row, Mapping) and row.get("evidence_id")
    )
    refs.update(
        str(row["source_field_id"])
        for row in detail.get("source_fields") or ()
        if isinstance(row, Mapping) and row.get("source_field_id")
    )
    return sorted(refs)


def _expected_leaf_group_contributions(
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    group_map: Mapping[str, Any],
    view_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    group_hashes = {row["group_id"]: compute_v3_hash(row) for row in group_map["groups"]}
    result = []
    for candidate_id in candidate_ids:
        group_ids = sorted({
            group_id for unit in units for group_id in unit["group_ids_by_candidate"][candidate_id]
        })
        for group_id in group_ids:
            assigned_units = [unit for unit in units if group_id in unit["group_ids_by_candidate"][candidate_id]]
            view_ids = sorted({
                view_id for unit in assigned_units
                for view_id in unit["group_views_by_candidate"][candidate_id].get(group_id, [])
            })
            evidence = sorted({
                evidence_id for unit in assigned_units
                for evidence_id in unit["group_evidence_by_candidate"][candidate_id].get(group_id, [])
            } | {
                source_ref
                for unit in assigned_units
                for view_id in unit["group_views_by_candidate"][candidate_id].get(group_id, [])
                for source_ref in _literal_view_ref_ids(view_by_id[view_id])
            })
            result.append({
                "candidate_id": candidate_id,
                "candidate_hash": None,
                "group_id": group_id,
                "group_hash": group_hashes[group_id],
                "unit_ids": [str(unit["unit_id"]) for unit in assigned_units],
                "decision_view_ids": view_ids,
                "evidence_refs": evidence,
            })
    return result


def _validate_group_contributions(
    plan: Mapping[str, Any],
    node: Mapping[str, Any],
    raw: Any,
    assessment_rows: Sequence[Mapping[str, Any]],
    *,
    internal_result: bool = False,
) -> list[dict[str, Any]]:
    expected = node["expected_group_contributions"]
    if not isinstance(raw, list):
        raise ArbitrationAggregationError("assessment response group_contributions must be an array")
    expected_by_key = {(row["candidate_id"], row["group_id"]): row for row in expected}
    observed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError(f"assessment group contribution {index} is malformed")
        _require_exact_keys(
            row,
            {"candidate_id", "candidate_hash", "group_id", "group_hash", "unit_ids", "decision_view_ids",
             "assessed_direction", "assessed_disagreement", "summary", "evidence_refs", "counterevidence_refs"},
            f"assessment group contribution {index}",
            optional=(
                {"assessed_direction_values", "assessed_disagreement_values", "typed_lineage"}
                if internal_result else None
            ),
        )
        key = (str(row.get("candidate_id") or ""), str(row.get("group_id") or ""))
        if key in observed:
            raise ArbitrationAggregationError("assessment response duplicates a candidate/group contribution")
        observed[key] = row
    if set(observed) != set(expected_by_key):
        raise ArbitrationAggregationError("assessment response omitted or added a required group contribution")
    group_hashes = {row["group_id"]: compute_v3_hash(row) for row in plan["review_group_map"]["groups"]}
    assessment_by_key = {
        (str(row["candidate_id"]), str(row["unit_id"])): row for row in assessment_rows
    }
    units_by_id = {str(row["unit_id"]): row for row in plan["assessment_units"]}
    result = []
    for (candidate_id, group_id), expect in expected_by_key.items():
        row = observed[(candidate_id, group_id)]
        if row.get("candidate_hash") != plan["candidate_hashes"][candidate_id]:
            raise ArbitrationAggregationError("group contribution has a foreign candidate/hash")
        if row.get("group_hash") != group_hashes[group_id]:
            raise ArbitrationAggregationError("group contribution has a stale review-group hash")
        if row.get("unit_ids") != expect["unit_ids"] or row.get("decision_view_ids") != expect["decision_view_ids"]:
            raise ArbitrationAggregationError("group contribution unit/view coverage is not exact")
        direction = _enum(row.get("assessed_direction"), _DIRECTIONS, "group contribution assessed_direction")
        disagreement = _enum(row.get("assessed_disagreement"), _DISAGREEMENT, "group contribution assessed_disagreement")
        summary = _bounded_text(row.get("summary"), "group contribution summary")
        evidence_refs = _string_ids(row.get("evidence_refs"), "group contribution evidence_refs")
        counterevidence_refs = _string_ids(row.get("counterevidence_refs"), "group contribution counterevidence_refs")
        typed_lineage: list[dict[str, Any]] = []
        direction_values: set[str] = set()
        disagreement_values: set[str] = set()
        expected_evidence_refs: set[str] = set()
        expected_counterevidence_refs: set[str] = set()
        for unit_id in expect["unit_ids"]:
            assessment = assessment_by_key[(candidate_id, unit_id)]
            unit = units_by_id[unit_id]
            group_view_ids = set(
                unit["group_views_by_candidate"][candidate_id].get(group_id, [])
            )
            view_lineage = []
            for view_assessment in assessment["view_assessments"]:
                if view_assessment["decision_view_id"] not in group_view_ids:
                    continue
                view_direction = str(view_assessment["assessed_direction"])
                view_disagreement = str(view_assessment["assessed_disagreement"])
                direction_values.add(view_direction)
                disagreement_values.add(view_disagreement)
                expected_evidence_refs.update(view_assessment["evidence_refs"])
                expected_counterevidence_refs.update(view_assessment["counterevidence_refs"])
                view_lineage.append({
                    "decision_view_id": view_assessment["decision_view_id"],
                    "assessed_direction": view_direction,
                    "assessed_disagreement": view_disagreement,
                    "evidence_refs": list(view_assessment["evidence_refs"]),
                    "counterevidence_refs": list(view_assessment["counterevidence_refs"]),
                })
            unit_direction = str(assessment["assessed_direction"])
            unit_disagreement = str(assessment["assessed_disagreement"])
            direction_values.add(unit_direction)
            disagreement_values.add(unit_disagreement)
            expected_evidence_refs.update(assessment["evidence_refs"])
            expected_counterevidence_refs.update(assessment["counterevidence_refs"])
            typed_lineage.append({
                "source_node_id": str(node["node_id"]),
                "unit_id": unit_id,
                "assessed_direction": unit_direction,
                "assessed_disagreement": unit_disagreement,
                "evidence_refs": list(assessment["evidence_refs"]),
                "counterevidence_refs": list(assessment["counterevidence_refs"]),
                "view_assessments": view_lineage,
            })
        expected_direction = _aggregate_direction_values(direction_values)
        expected_disagreement = _aggregate_disagreement_values(disagreement_values)
        if direction != expected_direction or disagreement != expected_disagreement:
            raise ArbitrationAggregationError("group contribution direction/disagreement erases typed unit assessments")
        if evidence_refs != sorted(expected_evidence_refs):
            raise ArbitrationAggregationError("group contribution omits or adds typed evidence references")
        if counterevidence_refs != sorted(expected_counterevidence_refs):
            raise ArbitrationAggregationError("group contribution omits or adds minority/counterevidence lineage")
        result.append({
            "candidate_id": candidate_id,
            "candidate_hash": plan["candidate_hashes"][candidate_id],
            "group_id": group_id,
            "group_hash": group_hashes[group_id],
            "unit_ids": list(expect["unit_ids"]),
            "decision_view_ids": list(expect["decision_view_ids"]),
            "assessed_direction": direction,
            "assessed_disagreement": disagreement,
            "assessed_direction_values": sorted(direction_values),
            "assessed_disagreement_values": sorted(disagreement_values),
            "typed_lineage": typed_lineage,
            "summary": summary,
            "evidence_refs": list(evidence_refs),
            "counterevidence_refs": list(counterevidence_refs),
        })
    if internal_result:
        normalized_by_key = {(row["candidate_id"], row["group_id"]): row for row in result}
        for raw_row in raw:
            key = (str(raw_row["candidate_id"]), str(raw_row["group_id"]))
            normalized = normalized_by_key[key]
            for field in ("assessed_direction_values", "assessed_disagreement_values", "typed_lineage"):
                if raw_row.get(field) != normalized[field]:
                    raise ArbitrationAggregationError(f"READY leaf compiled {field} differs from typed unit responses")
    return result


def _build_reducer_tree(
    *,
    leaves: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
    candidate_hashes: Mapping[str, str],
    group_map: Mapping[str, Any],
    plan_input_hash: str,
    caps: ArbitrationSerializationCaps,
) -> dict[str, Any]:
    active = [
        {
            "tree_node_id": str(row["node_id"]),
            "node_kind": "assessment_leaf",
            "unit_ids": list(row["unit_ids"]),
            "unit_ordinals": [
                int(next(unit["ordinal"] for unit in units if unit["unit_id"] == unit_id))
                for unit_id in row["unit_ids"]
            ],
        }
        for row in leaves
    ]
    levels: list[list[dict[str, Any]]] = []
    nodes: list[dict[str, Any]] = []
    if len(active) == 1:
        return {"levels": [], "nodes": [], "root_node_id": active[0]["tree_node_id"], "depth": 0, "odd_carry_levels": []}
    depth = 0
    while len(active) > 1:
        prior_count = len(active)
        active.sort(key=lambda row: (min(row["unit_ordinals"]), str(row["tree_node_id"])))
        level_nodes: list[dict[str, Any]] = []
        next_active: list[dict[str, Any]] = []
        odd_carry = None
        for index in range(0, len(active), 2):
            pair = active[index:index + 2]
            if len(pair) == 1:
                carry = copy.deepcopy(pair[0])
                next_active.append(carry)
                odd_carry = carry["tree_node_id"]
                continue
            child_ids = [row["tree_node_id"] for row in pair]
            merged_units = [
                unit_id for unit_id in sorted(
                    set(pair[0]["unit_ids"]) | set(pair[1]["unit_ids"]),
                    key=lambda value: next(int(unit["ordinal"]) for unit in units if unit["unit_id"] == value),
                )
            ]
            if set(pair[0]["unit_ids"]).intersection(pair[1]["unit_ids"]):
                raise ArbitrationAggregationError("deterministic reducer children have overlapping unit scopes")
            tree_node_id = "reducer-tree:" + compute_v3_hash({
                "schema_version": REDUCTION_INPUT_SCHEMA,
                "plan_input_hash": plan_input_hash,
                "level": depth + 1,
                "ordered_child_node_ids": child_ids,
                "unit_ids": merged_units,
                "candidate_ids": list(candidate_ids),
                "candidate_hashes": dict(candidate_hashes),
                "review_group_map_hash": group_map["group_map_hash"],
                "route_identity": copy.deepcopy(dict(caps.route_identity)),
            })[:24]
            node = {
                "tree_node_id": tree_node_id,
                "level": depth + 1,
                "child_node_ids": child_ids,
                "unit_ids": merged_units,
                "unit_ordinals": sorted(pair[0]["unit_ordinals"] + pair[1]["unit_ordinals"]),
                "min_unit_ordinal": min(pair[0]["unit_ordinals"] + pair[1]["unit_ordinals"]),
            }
            nodes.append(node)
            level_nodes.append(node)
            next_active.append({
                "tree_node_id": tree_node_id,
                "node_kind": "reducer",
                "unit_ids": merged_units,
                "unit_ordinals": node["unit_ordinals"],
            })
        if len(next_active) >= prior_count:
            raise ArbitrationAggregationError("binary reduction made no progress")
        levels.append(level_nodes)
        if odd_carry is not None:
            levels[-1] = [*levels[-1], {"odd_carry_node_id": odd_carry}]
        active = next_active
        depth += 1
        if depth > math.ceil(math.log2(len(leaves))) + 1:
            raise ArbitrationAggregationError("binary reduction depth exceeds its finite logarithmic bound")
    expected_depth = 0 if len(leaves) <= 1 else math.ceil(math.log2(len(leaves)))
    if depth != expected_depth:
        raise ArbitrationAggregationError("binary reducer depth differs from ceil(log2(Lnew))")
    if len(nodes) > max(0, len(leaves) - 1):
        raise ArbitrationAggregationError("binary reducer count exceeds Lnew-1")
    root_id = active[0]["tree_node_id"]
    if set(active[0]["unit_ids"]) != {row["unit_id"] for row in units}:
        raise ArbitrationAggregationError("binary reducer root omits one or more assessment units")
    return {"levels": levels, "nodes": nodes, "root_node_id": root_id, "depth": depth, "odd_carry_levels": [
        index + 1 for index, rows in enumerate(levels) if any("odd_carry_node_id" in row for row in rows)
    ]}


def _compile_group_unit_coverage(
    units: Sequence[Mapping[str, Any]], candidate_ids: Sequence[str], group_map: Mapping[str, Any],
) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for candidate_id in candidate_ids:
        result[candidate_id] = {}
        for group in group_map["groups"]:
            result[candidate_id][group["group_id"]] = [
                str(unit["unit_id"]) for unit in units
                if group["group_id"] in unit["group_ids_by_candidate"][candidate_id]
            ]
    return result


def _expected_reducer_groups(plan: Mapping[str, Any], reducer: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = set(reducer["unit_ids"])
    group_hashes = {row["group_id"]: compute_v3_hash(row) for row in plan["review_group_map"]["groups"]}
    result = []
    for candidate_id in plan["candidate_ids"]:
        for group in plan["review_group_map"]["groups"]:
            group_id = group["group_id"]
            assigned_units = [unit_id for unit_id in plan["group_unit_coverage"][candidate_id][group_id] if unit_id in units]
            view_ids = sorted({
                view_id
                for unit_id in assigned_units
                for unit in plan["assessment_units"]
                if unit["unit_id"] == unit_id
                for view_id in unit["group_views_by_candidate"][candidate_id].get(group_id, [])
            })
            evidence_refs = sorted({
                evidence_id
                for unit_id in assigned_units
                for unit in plan["assessment_units"]
                if unit["unit_id"] == unit_id
                for evidence_id in unit["group_evidence_by_candidate"][candidate_id].get(group_id, [])
            } | {
                source_ref
                for unit_id in assigned_units
                for unit in plan["assessment_units"]
                if unit["unit_id"] == unit_id
                for view_id in unit["group_views_by_candidate"][candidate_id].get(group_id, [])
                for source_ref in _literal_view_ref_ids(
                    next(row for row in plan["critical_view_set"]["critical_views"]
                         if row["decision_view_id"] == view_id)
                )
            })
            result.append({
                "candidate_id": candidate_id,
                "group_id": group_id,
                "group_hash": group_hashes[group_id],
                "unit_ids": assigned_units,
                "decision_view_ids": view_ids,
                "evidence_refs": evidence_refs,
            })
    return result


def _expected_reducer_groups_from_children(
    plan: Mapping[str, Any],
    reducer: Mapping[str, Any],
    child_ids: Sequence[str],
    child_hashes: Sequence[str],
    children: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compile exact child direction/disagreement and counterevidence lineage."""

    static_rows = _expected_reducer_groups(plan, reducer)
    child_units_by_id: dict[str, set[str]] = {}
    for child_id in child_ids:
        if any(row.get("node_id") == child_id for row in plan["assessment_leaves"]):
            child_units_by_id[child_id] = set(_find_leaf(plan, child_id)["unit_ids"])
        else:
            child_units_by_id[child_id] = set(_find_reducer(plan, child_id)["unit_ids"])
    rows_by_child: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = {}
    for child_id, child in zip(child_ids, children, strict=True):
        raw_rows = child.get("group_contributions") or child.get("group_summaries") or []
        rows_by_child[child_id] = {
            (str(row.get("candidate_id") or ""), str(row.get("group_id") or "")): row
            for row in raw_rows if isinstance(row, Mapping)
        }

    result = []
    for static in static_rows:
        candidate_id = static["candidate_id"]
        group_id = static["group_id"]
        expected_units = list(static["unit_ids"])
        seen_units: set[str] = set()
        directions: set[str] = set()
        disagreements: set[str] = set()
        evidence_refs: set[str] = set()
        counterevidence_refs: set[str] = set()
        child_lineage: list[dict[str, Any]] = []
        typed_lineage: list[dict[str, Any]] = []
        for child_id, child_hash in zip(child_ids, child_hashes, strict=True):
            child_row = rows_by_child[child_id].get((candidate_id, group_id))
            if child_row is None or not child_row.get("unit_ids"):
                continue
            child_unit_ids = _string_ids(child_row.get("unit_ids"), f"{child_id}.{group_id}.unit_ids", required=True)
            if not set(child_unit_ids).issubset(child_units_by_id[child_id]):
                raise ArbitrationAggregationError("child group summary references units outside its READY node")
            expected_child_units = [unit for unit in expected_units if unit in child_units_by_id[child_id]]
            if child_unit_ids != expected_child_units:
                raise ArbitrationAggregationError("child group summary omits or adds assigned source units")
            if seen_units.intersection(child_unit_ids):
                raise ArbitrationAggregationError("reducer child group unit sets overlap")
            seen_units.update(child_unit_ids)
            child_direction_values = _string_ids(
                child_row.get("assessed_direction_values"), f"{child_id}.{group_id}.assessed_direction_values", required=True,
            )
            child_disagreement_values = _string_ids(
                child_row.get("assessed_disagreement_values"), f"{child_id}.{group_id}.assessed_disagreement_values", required=True,
            )
            if not set(child_direction_values).issubset(_DIRECTIONS) or not set(child_disagreement_values).issubset(_DISAGREEMENT):
                raise ArbitrationAggregationError("child typed direction/disagreement lineage has an unknown value")
            child_evidence = _string_ids(child_row.get("evidence_refs"), f"{child_id}.{group_id}.evidence_refs")
            child_counterevidence = _string_ids(
                child_row.get("counterevidence_refs"), f"{child_id}.{group_id}.counterevidence_refs",
            )
            child_directions = sorted(set(child_direction_values))
            child_disagreements = sorted(set(child_disagreement_values))
            directions.update(child_directions)
            disagreements.update(child_disagreements)
            evidence_refs.update(child_evidence)
            counterevidence_refs.update(child_counterevidence)
            child_lineage.append({
                "child_node_id": child_id,
                "child_result_hash": child_hash,
                "unit_ids": child_unit_ids,
                "assessed_direction_values": child_directions,
                "assessed_disagreement_values": child_disagreements,
                "evidence_refs": child_evidence,
                "counterevidence_refs": child_counterevidence,
            })
            for raw_line in child_row.get("typed_lineage") or ():
                if not isinstance(raw_line, Mapping):
                    raise ArbitrationAggregationError("child typed unit lineage row is malformed")
                typed_line = copy.deepcopy(dict(raw_line))
                typed_line.setdefault("source_result_hash", child_hash)
                typed_lineage.append(typed_line)
        if seen_units != set(expected_units):
            raise ArbitrationAggregationError("reducer child summaries omit or add source units")
        ordered_lineage = sorted(
            typed_lineage,
            key=lambda row: _unit_ordinal_key(plan)(str(row.get("unit_id") or "")),
        )
        result.append({
            **static,
            "presence": "present" if expected_units else "not_applicable",
            "assessed_direction_values": sorted(directions),
            "assessed_disagreement_values": sorted(disagreements),
            "assessed_direction": _aggregate_direction_values(directions),
            "assessed_disagreement": _aggregate_disagreement_values(disagreements),
            "child_lineage": child_lineage,
            "typed_lineage": ordered_lineage,
            "evidence_refs": sorted(evidence_refs),
            "counterevidence_refs": sorted(counterevidence_refs),
        })
    return result


def _reduction_response_shape(
    plan: Mapping[str, Any], reducer: Mapping[str, Any], caps: ArbitrationSerializationCaps,
    *, expected_group_summaries: Sequence[Mapping[str, Any]],
    child_result_hashes: Sequence[str], node_id: str,
) -> dict[str, Any]:
    # This request is materialized only after every child is READY. The
    # validator requires exact echoes of these metadata fields, including
    # minority/counterevidence lineage. Reserve their actual complete values;
    # only the bounded summary text is still unknown at admission time.
    rows = []
    for expected in expected_group_summaries:
        rows.append({
            "candidate_id": expected["candidate_id"],
            "candidate_hash": plan["candidate_hashes"][expected["candidate_id"]],
            "group_id": expected["group_id"],
            "group_hash": expected["group_hash"],
            "presence": expected["presence"],
            "unit_ids": list(expected["unit_ids"]),
            "decision_view_ids": list(expected["decision_view_ids"]),
            "assessed_direction": expected["assessed_direction"],
            "assessed_disagreement": expected["assessed_disagreement"],
            "assessed_direction_values": list(expected["assessed_direction_values"]),
            "assessed_disagreement_values": list(expected["assessed_disagreement_values"]),
            "child_lineage": copy.deepcopy(expected["child_lineage"]),
            "summary": _token_fill(caps.group_summary_text_tokens) if expected["unit_ids"] else "",
            "evidence_refs": list(expected["evidence_refs"]),
            "counterevidence_refs": list(expected["counterevidence_refs"]),
        })
    return {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "node_id": node_id,
        "tree_node_id": reducer["tree_node_id"],
        "child_node_ids": list(reducer["child_node_ids"]),
        "child_result_hashes": list(child_result_hashes),
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": list(reducer["unit_ids"]),
        "group_summaries": rows,
    }


def _reduction_output_contract(plan: Mapping[str, Any], reducer: Mapping[str, Any], caps: ArbitrationSerializationCaps) -> dict[str, Any]:
    return {
        "schema_version": REDUCTION_RESPONSE_SCHEMA,
        "candidate_ids": list(plan["candidate_ids"]),
        "unit_ids": list(reducer["unit_ids"]),
        "group_ids": [row["group_id"] for row in plan["review_group_map"]["groups"]],
        "max_summary_tokens_per_candidate_group": caps.group_summary_text_tokens,
        "must_preserve_child_direction_and_disagreement_sets": True,
        "must_preserve_counterevidence_refs_exactly": True,
        "direction_values": sorted(_DIRECTIONS),
        "disagreement_values": sorted(_DISAGREEMENT),
        "no_claim_of_code_verified_completeness": True,
        "max_output_tokens": 1,
    }


def _child_group_summaries(
    plan: Mapping[str, Any],
    reducer: Mapping[str, Any],
    child_ids: Sequence[str],
    child_hashes: Sequence[str],
    children: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project exact typed child outcomes into the next reducer request."""

    rows: list[dict[str, Any]] = []
    allowed_pairs = {
        (row["candidate_id"], row["group_id"])
        for row in _expected_reducer_groups(plan, reducer)
        if row["unit_ids"]
    }
    for child_id, child_hash, child in zip(child_ids, child_hashes, children, strict=True):
        child_rows = child.get("group_contributions") or child.get("group_summaries") or []
        for row in child_rows:
            key = (str(row.get("candidate_id") or ""), str(row.get("group_id") or ""))
            if key not in allowed_pairs or not row.get("unit_ids"):
                continue
            rows.append({
                "child_node_id": child_id,
                "child_result_hash": child_hash,
                "candidate_id": row["candidate_id"],
                "candidate_hash": row["candidate_hash"],
                "group_id": row["group_id"],
                "group_hash": row["group_hash"],
                "unit_ids": list(row["unit_ids"]),
                "decision_view_ids": list(row["decision_view_ids"]),
                "assessed_direction": row["assessed_direction"],
                "assessed_disagreement": row["assessed_disagreement"],
                "assessed_direction_values": list(row["assessed_direction_values"]),
                "assessed_disagreement_values": list(row["assessed_disagreement_values"]),
                "summary": row["summary"],
                "evidence_refs": list(row["evidence_refs"]),
                "counterevidence_refs": list(row["counterevidence_refs"]),
            })
    rows.sort(key=lambda row: (
        plan["candidate_ids"].index(row["candidate_id"]),
        next(index for index, group in enumerate(plan["review_group_map"]["groups"])
             if group["group_id"] == row["group_id"]),
        child_ids.index(row["child_node_id"]),
    ))
    return rows


def _decision_scope(plan: Mapping[str, Any], unit_ids: Sequence[str]) -> dict[str, Any]:
    units = {row["unit_id"]: row for row in plan["assessment_units"]}
    ordered = list(unit_ids)
    if len(ordered) != len(set(ordered)):
        raise ArbitrationAggregationError("decision scope repeats a unit ID")
    return {
        "schema_version": DECISION_SCOPE_SCHEMA,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "unit_ids": ordered,
        "unit_hashes": [units[unit_id]["unit_hash"] for unit_id in ordered],
        "review_group_map_hash": plan["review_group_map"]["group_map_hash"],
        "candidate_output_scope_hash": plan["candidate_output_scope_hash"],
        "source_inventory_binding": copy.deepcopy(plan["source_inventory_binding"]),
    }


def _legacy_binding(plan: Mapping[str, Any]) -> dict[str, Any]:
    identity = plan["identity"]
    return {
        "legacy_plan_hash": identity["legacy_plan_hash"],
        "legacy_ledger_hash": identity["legacy_ledger_hash"],
        "legacy_ledger_wire_hash": identity["legacy_ledger_wire_hash"],
        "v1_inputs_are_immutable": True,
        "legacy_free_prose_is_not_a_typed_judgment": True,
    }


def _expected_leaf_candidate_rows(plan: Mapping[str, Any], node: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = node.get("expected_assessments")
    if not isinstance(rows, list):
        raise ArbitrationAggregationError("assessment leaf expected rows are malformed")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError("assessment leaf expected row is malformed")
        key = (str(row.get("candidate_id") or ""), str(row.get("unit_id") or ""))
        if key in result:
            raise ArbitrationAggregationError("assessment leaf repeats an expected candidate/unit pair")
        result[key] = {key: copy.deepcopy(value) for key, value in row.items() if key not in {"candidate_id", "unit_id"}}
    return result


def _find_leaf(plan: Mapping[str, Any], node_id: str) -> Mapping[str, Any]:
    for row in plan.get("assessment_leaves") or ():
        if row.get("node_id") == node_id:
            return row
    raise ArbitrationAggregationError(f"unknown assessment leaf {node_id}")


def _find_reducer(plan: Mapping[str, Any], tree_node_id: str) -> Mapping[str, Any]:
    for row in plan.get("reducer_tree", {}).get("nodes", ()):
        if row.get("tree_node_id") == tree_node_id:
            return row
    raise ArbitrationAggregationError(f"unknown reducer tree node {tree_node_id}")


def _validate_ready_child_result(
    plan: Mapping[str, Any],
    child_id: str,
    response: Mapping[str, Any],
    *,
    estimate_output_tokens: OutputEstimator | None = None,
    output_cap: int | None = None,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise ArbitrationAggregationError(f"READY child {child_id} result must be an object")
    schema = response.get("schema_version")
    if schema == ASSESSMENT_RESPONSE_SCHEMA:
        if estimate_output_tokens is None:
            raise ArbitrationAggregationError("READY assessment result requires its production output estimator")
        normalized = validate_arbitration_assessment_response(
            plan, child_id, response, estimate_output_tokens=estimate_output_tokens,
            _internal_result=bool(
                _find_leaf(plan, child_id).get("wire_catalog") is not None
                and response.get("result_hash") is not None
            ),
        )
        if (
            output_cap is not None
            and _find_leaf(plan, child_id).get("wire_catalog") is None
            and _estimate_output(estimate_output_tokens, {
                key: value for key, value in normalized.items() if key != "result_hash"
            }) > output_cap
        ):
            raise ArbitrationAggregationError(f"READY assessment child {child_id} exceeds route output cap")
        return normalized
    elif schema == REDUCTION_RESPONSE_SCHEMA:
        tree_node = _find_reducer(plan, child_id)
        _require_exact_keys(
            response,
            {"schema_version", "node_id", "tree_node_id", "child_node_ids", "child_result_hashes",
             "candidate_ids", "candidate_hashes", "unit_ids", "group_summaries"},
            f"READY reducer child {child_id}",
            optional={"result_hash"},
        )
        if response.get("tree_node_id") != child_id or response.get("unit_ids") != tree_node["unit_ids"]:
            raise ArbitrationAggregationError(f"READY reducer child {child_id} has stale scope")
        expected_hash = compute_v3_hash({key: value for key, value in response.items() if key != "result_hash"})
        if response.get("result_hash") != expected_hash:
            raise ArbitrationAggregationError(f"READY reducer child {child_id} result hash is invalid")
        if response.get("candidate_ids") != list(plan["candidate_ids"]) or response.get("candidate_hashes") != dict(plan["candidate_hashes"]):
            raise ArbitrationAggregationError(f"READY reducer child {child_id} has a foreign candidate scope")
        if response.get("child_node_ids") != list(tree_node["child_node_ids"]):
            raise ArbitrationAggregationError(f"READY reducer child {child_id} has foreign child IDs")
        child_hashes = _string_hashes(response.get("child_result_hashes"), f"{child_id}.child_result_hashes")
        if len(child_hashes) != len(tree_node["child_node_ids"]):
            raise ArbitrationAggregationError(f"READY reducer child {child_id} child hash count is invalid")
        expected_node_id = "arbitration-reducer:" + compute_v3_hash({
            "schema_version": REDUCTION_INPUT_SCHEMA,
            "plan_input_hash": plan["plan_input_hash"],
            "reducer_tree_node_id": child_id,
            "ordered_child_node_ids": list(tree_node["child_node_ids"]),
            "ordered_child_result_hashes": child_hashes,
            "unit_ids": list(tree_node["unit_ids"]),
            "candidate_ids": list(plan["candidate_ids"]),
            "candidate_hashes": dict(plan["candidate_hashes"]),
            "review_group_map_hash": plan["review_group_map"]["group_map_hash"],
            "route_identity": plan["identity"]["route_identity"],
        })[:24]
        if response.get("node_id") != expected_node_id:
            raise ArbitrationAggregationError(f"READY reducer child {child_id} node identity is stale")
        expected_groups = _expected_reducer_groups(plan, tree_node)
        child_units_by_id: dict[str, set[str]] = {}
        for grandchild_id in tree_node["child_node_ids"]:
            if any(row.get("node_id") == grandchild_id for row in plan["assessment_leaves"]):
                child_units_by_id[grandchild_id] = set(_find_leaf(plan, grandchild_id)["unit_ids"])
            else:
                child_units_by_id[grandchild_id] = set(_find_reducer(plan, grandchild_id)["unit_ids"])
        child_hash_by_id = dict(zip(tree_node["child_node_ids"], child_hashes, strict=True))
        unit_by_id = {str(row["unit_id"]): row for row in plan["assessment_units"]}
        leaf_by_unit = {
            str(unit_id): str(leaf["node_id"])
            for leaf in plan["assessment_leaves"]
            for unit_id in leaf["unit_ids"]
        }
        view_by_id = {
            str(row["decision_view_id"]): row
            for row in plan["critical_view_set"]["critical_views"]
        }
        raw_groups = response.get("group_summaries")
        if not isinstance(raw_groups, list) or len(raw_groups) != len(expected_groups):
            raise ArbitrationAggregationError(f"READY reducer child {child_id} group coverage is incomplete")
        group_hashes = {row["group_id"]: compute_v3_hash(row) for row in plan["review_group_map"]["groups"]}
        for expected, row in zip(expected_groups, raw_groups, strict=True):
            if not isinstance(row, Mapping):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} group row is malformed")
            _require_exact_keys(
                row,
                {"candidate_id", "candidate_hash", "group_id", "group_hash", "presence", "unit_ids",
                 "decision_view_ids", "assessed_direction", "assessed_disagreement", "summary",
                 "assessed_direction_values", "assessed_disagreement_values", "child_lineage",
                 "typed_lineage", "evidence_refs", "counterevidence_refs"},
                f"READY reducer child {child_id} group row",
            )
            candidate_id = expected["candidate_id"]
            if (
                row.get("candidate_id") != candidate_id
                or row.get("candidate_hash") != plan["candidate_hashes"][candidate_id]
                or row.get("group_id") != expected["group_id"]
                or row.get("group_hash") != group_hashes[expected["group_id"]]
                or row.get("unit_ids") != expected["unit_ids"]
                or row.get("decision_view_ids") != expected["decision_view_ids"]
            ):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} group source union is stale")
            expected_presence = "present" if expected["unit_ids"] else "not_applicable"
            if row.get("presence") != expected_presence:
                raise ArbitrationAggregationError(f"READY reducer child {child_id} group presence is stale")
            direction_values = _string_ids(
                row.get("assessed_direction_values"), "READY reducer assessed_direction_values",
            )
            disagreement_values = _string_ids(
                row.get("assessed_disagreement_values"), "READY reducer assessed_disagreement_values",
            )
            if (
                direction_values != sorted(direction_values)
                or disagreement_values != sorted(disagreement_values)
                or not set(direction_values).issubset(_DIRECTIONS)
                or not set(disagreement_values).issubset(_DISAGREEMENT)
            ):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} typed direction/disagreement sets are invalid")
            if row.get("assessed_direction") != _aggregate_direction_values(direction_values):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} aggregate direction differs from its typed set")
            if row.get("assessed_disagreement") != _aggregate_disagreement_values(disagreement_values):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} aggregate disagreement differs from its typed set")

            expected_child_units = {
                grandchild_id: [
                    unit_id for unit_id in expected["unit_ids"]
                    if unit_id in child_units_by_id[grandchild_id]
                ]
                for grandchild_id in tree_node["child_node_ids"]
            }
            expected_child_ids = [
                grandchild_id for grandchild_id in tree_node["child_node_ids"]
                if expected_child_units[grandchild_id]
            ]
            raw_child_lineage = row.get("child_lineage")
            if not isinstance(raw_child_lineage, list) or [
                line.get("child_node_id") if isinstance(line, Mapping) else None
                for line in raw_child_lineage
            ] != expected_child_ids:
                raise ArbitrationAggregationError(f"READY reducer child {child_id} immediate child lineage is incomplete")
            seen_child_units: set[str] = set()
            child_direction_union: set[str] = set()
            child_disagreement_union: set[str] = set()
            child_evidence_union: set[str] = set()
            child_counterevidence_union: set[str] = set()
            allowed_evidence = set(expected["evidence_refs"])
            for line in raw_child_lineage:
                _require_exact_keys(
                    line,
                    {"child_node_id", "child_result_hash", "unit_ids", "assessed_direction_values",
                     "assessed_disagreement_values", "evidence_refs", "counterevidence_refs"},
                    f"READY reducer child {child_id} child lineage",
                )
                grandchild_id = str(line["child_node_id"])
                child_unit_ids = _string_ids(line.get("unit_ids"), "READY reducer child-lineage unit_ids", required=True)
                if child_unit_ids != expected_child_units[grandchild_id] or seen_child_units.intersection(child_unit_ids):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} child-lineage unit partition is invalid")
                seen_child_units.update(child_unit_ids)
                if line.get("child_result_hash") != child_hash_by_id[grandchild_id]:
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} child lineage has a stale result hash")
                child_directions = _string_ids(
                    line.get("assessed_direction_values"), "READY reducer child-lineage direction values", required=True,
                )
                child_disagreements = _string_ids(
                    line.get("assessed_disagreement_values"), "READY reducer child-lineage disagreement values", required=True,
                )
                if (
                    child_directions != sorted(child_directions)
                    or child_disagreements != sorted(child_disagreements)
                    or not set(child_directions).issubset(_DIRECTIONS)
                    or not set(child_disagreements).issubset(_DISAGREEMENT)
                ):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} child-lineage typed values are invalid")
                child_evidence = _string_ids(line.get("evidence_refs"), "READY reducer child-lineage evidence_refs")
                child_counterevidence = _string_ids(
                    line.get("counterevidence_refs"), "READY reducer child-lineage counterevidence_refs",
                )
                if not set(child_evidence + child_counterevidence).issubset(allowed_evidence):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} child lineage cites foreign evidence")
                child_direction_union.update(child_directions)
                child_disagreement_union.update(child_disagreements)
                child_evidence_union.update(child_evidence)
                child_counterevidence_union.update(child_counterevidence)
            if seen_child_units != set(expected["unit_ids"]):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} child lineage omits assigned units")
            if direction_values != sorted(child_direction_union) or disagreement_values != sorted(child_disagreement_union):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} typed sets differ from immediate child lineage")

            raw_typed_lineage = row.get("typed_lineage")
            if not isinstance(raw_typed_lineage, list):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} typed lineage is malformed")
            if [
                str(line.get("unit_id") or "") if isinstance(line, Mapping) else ""
                for line in raw_typed_lineage
            ] != list(expected["unit_ids"]):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} typed lineage unit coverage is invalid")
            typed_directions: set[str] = set()
            typed_disagreements: set[str] = set()
            typed_evidence: set[str] = set()
            typed_counterevidence: set[str] = set()
            for line in raw_typed_lineage:
                _require_exact_keys(
                    line,
                    {"source_node_id", "unit_id", "assessed_direction", "assessed_disagreement",
                     "evidence_refs", "counterevidence_refs", "view_assessments", "source_result_hash"},
                    f"READY reducer child {child_id} typed unit lineage",
                )
                unit_id = str(line["unit_id"])
                if line.get("source_node_id") != leaf_by_unit.get(unit_id):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} typed lineage has a foreign source leaf")
                _required_hash(line.get("source_result_hash"), "READY reducer typed lineage source_result_hash")
                direction = _enum(line.get("assessed_direction"), _DIRECTIONS, "READY reducer typed assessed_direction")
                disagreement = _enum(line.get("assessed_disagreement"), _DISAGREEMENT, "READY reducer typed assessed_disagreement")
                line_evidence = _string_ids(line.get("evidence_refs"), "READY reducer typed evidence_refs")
                line_counterevidence = _string_ids(
                    line.get("counterevidence_refs"), "READY reducer typed counterevidence_refs",
                )
                if not set(line_evidence + line_counterevidence).issubset(allowed_evidence):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} typed lineage cites foreign evidence")
                typed_directions.add(direction)
                typed_disagreements.add(disagreement)
                typed_evidence.update(line_evidence)
                typed_counterevidence.update(line_counterevidence)
                unit = unit_by_id[unit_id]
                candidate_id = str(expected["candidate_id"])
                group_view_ids = list(unit["group_views_by_candidate"][candidate_id].get(expected["group_id"], []))
                group_view_members = set(group_view_ids)
                # Group membership is a canonical set index; ordered view
                # lineage must follow the order actually declared to the leaf.
                expected_view_ids = [
                    view_id for view_id in unit["candidate_rows"][candidate_id]["decision_view_ids"]
                    if view_id in group_view_members
                ]
                if len(expected_view_ids) != len(group_view_ids):
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} group view membership is inconsistent")
                view_rows = line.get("view_assessments")
                if not isinstance(view_rows, list) or [
                    view.get("decision_view_id") if isinstance(view, Mapping) else None
                    for view in view_rows
                ] != expected_view_ids:
                    raise ArbitrationAggregationError(f"READY reducer child {child_id} typed view lineage is incomplete")
                for view in view_rows:
                    _require_exact_keys(
                        view,
                        {"decision_view_id", "assessed_direction", "assessed_disagreement", "evidence_refs", "counterevidence_refs"},
                        f"READY reducer child {child_id} typed view lineage",
                    )
                    view_id = str(view["decision_view_id"])
                    view_direction = _enum(view.get("assessed_direction"), _DIRECTIONS, "READY reducer typed view direction")
                    view_disagreement = _enum(view.get("assessed_disagreement"), _DISAGREEMENT, "READY reducer typed view disagreement")
                    view_evidence = _string_ids(view.get("evidence_refs"), "READY reducer typed view evidence_refs")
                    view_counterevidence = _string_ids(
                        view.get("counterevidence_refs"), "READY reducer typed view counterevidence_refs",
                    )
                    if not set(view_evidence + view_counterevidence).issubset(
                        set(_literal_view_ref_ids(view_by_id[view_id]))
                    ):
                        raise ArbitrationAggregationError(f"READY reducer child {child_id} typed view cites foreign literals")
                    typed_directions.add(view_direction)
                    typed_disagreements.add(view_disagreement)
                    typed_evidence.update(view_evidence)
                    typed_counterevidence.update(view_counterevidence)
            if direction_values != sorted(typed_directions) or disagreement_values != sorted(typed_disagreements):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} aggregate values differ from typed unit lineage")
            if expected_presence == "present":
                _bounded_text(row.get("summary"), "READY reducer summary")
            elif row.get("summary") != "":
                raise ArbitrationAggregationError(f"READY reducer child {child_id} has prose for an absent group")
            evidence = _string_ids(row.get("evidence_refs"), "READY reducer evidence_refs")
            counterevidence = _string_ids(row.get("counterevidence_refs"), "READY reducer counterevidence_refs")
            if (
                evidence != sorted(child_evidence_union)
                or counterevidence != sorted(child_counterevidence_union)
                or evidence != sorted(typed_evidence)
                or counterevidence != sorted(typed_counterevidence)
            ):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} evidence lineage differs from its typed children")
        if estimate_output_tokens is not None:
            observed_output = _estimate_output(estimate_output_tokens, {
                key: value for key, value in response.items() if key != "result_hash"
            })
            if (
                output_cap is not None
                and plan["identity"].get("assessment_wire_schema") is None
                and observed_output > output_cap
            ):
                raise ArbitrationAggregationError(f"READY reducer child {child_id} exceeds route output cap")
    else:
        raise ArbitrationAggregationError(f"READY child {child_id} has an unsupported response schema")
    return copy.deepcopy(dict(response))


def _unit_ordinal_key(plan: Mapping[str, Any]) -> Callable[[str], int]:
    order = {row["unit_id"]: int(row["ordinal"]) for row in plan["assessment_units"]}
    return lambda unit_id: order[unit_id]


def _collect_critical_assessments(plan: Mapping[str, Any], leaves: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for leaf in plan["assessment_leaves"]:
        leaf_result = leaves[leaf["node_id"]]
        for row in leaf_result["assessments"]:
            for view in row["view_assessments"]:
                result.append({
                    "candidate_id": row["candidate_id"],
                    "candidate_hash": row["candidate_hash"],
                    "unit_id": row["unit_id"],
                    **copy.deepcopy(view),
                })
    result.sort(key=lambda row: (plan["candidate_ids"].index(row["candidate_id"]), row["decision_view_id"]))
    return result


def _selector_coverage_bindings(
    plan: Mapping[str, Any], leaves: Mapping[str, Mapping[str, Any]], root: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "work_plan_input_hash": plan["plan_input_hash"],
        "root_node_id": root["node_id"],
        "root_result_hash": root["result_hash"],
        "all_candidate_ids_retained": list(plan["candidate_ids"]),
        "all_assessment_unit_ids": [row["unit_id"] for row in plan["assessment_units"]],
        "all_group_ids": [row["group_id"] for row in plan["review_group_map"]["groups"]],
        "critical_view_ids_by_candidate": {
            candidate_id: sorted(
                row["decision_view_id"] for row in plan["critical_view_set"]["critical_views"]
                if row["candidate_id"] == candidate_id
            )
            for candidate_id in plan["candidate_ids"]
        },
        "leaf_result_hashes": {
            leaf_id: row["result_hash"] for leaf_id, row in leaves.items()
        },
        "code_verified_coverage_only": True,
    }


def _root_group_summaries(plan: Mapping[str, Any], root: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a complete CxW selector summary table from a leaf or reducer root."""

    raw_rows = root.get("group_summaries")
    if not isinstance(raw_rows, list):
        raw_rows = root.get("group_contributions")
    if not isinstance(raw_rows, list):
        raise ArbitrationAggregationError("READY aggregation root omits group summaries")
    observed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise ArbitrationAggregationError("aggregation root group summary is malformed")
        key = (str(row.get("candidate_id") or ""), str(row.get("group_id") or ""))
        if key in observed:
            raise ArbitrationAggregationError("aggregation root repeats a candidate/group summary")
        observed[key] = row
    group_hashes = {row["group_id"]: compute_v3_hash(row) for row in plan["review_group_map"]["groups"]}
    view_by_id = {
        row["decision_view_id"]: row
        for row in plan["critical_view_set"]["critical_views"]
    }
    result: list[dict[str, Any]] = []
    for candidate_id in plan["candidate_ids"]:
        for group in plan["review_group_map"]["groups"]:
            group_id = group["group_id"]
            expected_units = list(plan["group_unit_coverage"][candidate_id][group_id])
            expected_view_ids = sorted({
                view_id
                for unit in plan["assessment_units"]
                if unit["unit_id"] in expected_units
                for view_id in unit["group_views_by_candidate"][candidate_id].get(group_id, [])
            })
            expected_evidence = sorted({
                evidence_id
                for unit in plan["assessment_units"]
                if unit["unit_id"] in expected_units
                for evidence_id in unit["group_evidence_by_candidate"][candidate_id].get(group_id, [])
            } | {
                source_ref
                for view_id in expected_view_ids
                for source_ref in _literal_view_ref_ids(view_by_id[view_id])
            })
            key = (candidate_id, group_id)
            row = observed.get(key)
            if not expected_units:
                if row is not None and (row.get("unit_ids") or row.get("decision_view_ids")):
                    raise ArbitrationAggregationError("not-applicable root group contains foreign unit/view data")
                result.append({
                    "candidate_id": candidate_id,
                    "candidate_hash": plan["candidate_hashes"][candidate_id],
                    "group_id": group_id,
                    "group_hash": group_hashes[group_id],
                    "presence": "not_applicable",
                    "unit_ids": [],
                    "decision_view_ids": [],
                    "assessed_direction": "not_comparable",
                    "assessed_disagreement": "not_applicable",
                    "summary": "",
                    "evidence_refs": [],
                    "counterevidence_refs": [],
                })
                continue
            if row is None:
                raise ArbitrationAggregationError("aggregation root omitted an applicable candidate/group summary")
            if row.get("unit_ids") != expected_units or row.get("decision_view_ids") != expected_view_ids:
                raise ArbitrationAggregationError("aggregation root group summary is incomplete against code-derived coverage")
            if row.get("candidate_hash") != plan["candidate_hashes"][candidate_id] or row.get("group_hash") != group_hashes[group_id]:
                raise ArbitrationAggregationError("aggregation root group summary has a stale candidate/group hash")
            if not isinstance(row.get("summary"), str) or not row["summary"].strip():
                raise ArbitrationAggregationError("aggregation root applicable group summary is empty")
            evidence_refs = _string_ids(row.get("evidence_refs"), "root group evidence_refs")
            counterevidence_refs = _string_ids(row.get("counterevidence_refs"), "root group counterevidence_refs")
            if not set(evidence_refs + counterevidence_refs).issubset(set(expected_evidence)):
                raise ArbitrationAggregationError("aggregation root group summary cites foreign evidence")
            result.append(copy.deepcopy(dict(row)))
    if set(observed) - {(row["candidate_id"], row["group_id"]) for row in result}:
        raise ArbitrationAggregationError("aggregation root contains a foreign candidate/group summary")
    return result


def _selection_output_contract(plan: Mapping[str, Any], max_detail_ids: int, caps: ArbitrationSerializationCaps) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcomes": ["selected", "detail_required"],
        "max_detail_required_view_ids": max_detail_ids,
        "max_rationale_tokens": caps.selection_text_tokens,
        "critical_views_cannot_be_deferred": True,
        "max_output_tokens": 1,
    }


def _selection_response_shape(plan: Mapping[str, Any], max_detail_ids: int, caps: ArbitrationSerializationCaps) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_RESPONSE_SCHEMA,
        "node_id": "0" * 64,
        "candidate_ids": list(plan["candidate_ids"]),
        "candidate_hashes": dict(plan["candidate_hashes"]),
        "outcome": "selected",
        "selected_candidate_id": plan["candidate_ids"][0],
        "detail_required_view_ids": ["decision-view:" + ("0" * 24)] * max_detail_ids,
        "rationale": _token_fill(caps.selection_text_tokens),
    }


def _check_observed_output_size(response: Mapping[str, Any], estimator: OutputEstimator, cap: int) -> None:
    observed = _estimate_output(estimator, response)
    if observed > cap:
        raise ArbitrationAggregationError(f"actual response output shape {observed} exceeds reserved output {cap}")


def _assert_serialization_fits(input_tokens: int, output_tokens: int, caps: ArbitrationSerializationCaps, label: str) -> None:
    if input_tokens < 1 or input_tokens > caps.effective_input_cap:
        raise ArbitrationSerializationError(
            f"{label} serialized input={input_tokens}/{caps.effective_input_cap}"
        )
    if output_tokens < 1 or output_tokens > caps.route_max_output:
        raise ArbitrationSerializationError(
            f"{label} serialized output skeleton={output_tokens}/{caps.route_max_output}"
        )
    total = input_tokens + output_tokens + caps.reasoning_reserve + caps.safety_margin
    if total > caps.verified_context_limit:
        raise ArbitrationSerializationError(
            f"{label} total context={total}/{caps.verified_context_limit}"
        )


def _build_request(builder: RequestBuilder, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = builder(kind, copy.deepcopy(dict(payload)))
    if not isinstance(request, Mapping):
        raise ArbitrationAggregationError("prompt-authority request builder must return the production logical body")
    return copy.deepcopy(dict(request))


def _estimate_input(estimator: InputEstimator, request: Mapping[str, Any]) -> int:
    try:
        measured = estimator(request)
    except Exception as exc:
        raise ArbitrationAggregationError("production input serializer estimator failed") from exc
    if isinstance(measured, Mapping):
        measured = measured.get("estimated_input_tokens")
    if type(measured) is not int or measured < 0:
        raise ArbitrationAggregationError("input estimator must return a non-negative token count")
    return measured


def _estimate_output(estimator: OutputEstimator, shape: Mapping[str, Any]) -> int:
    try:
        measured = estimator(shape)
    except Exception as exc:
        raise ArbitrationAggregationError("production output-shape estimator failed") from exc
    if type(measured) is not int or measured < 0:
        raise ArbitrationAggregationError("output estimator must return a non-negative token count")
    return measured


def _expand_and_validate_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    from outline.candidate_arbitration_ledger_packing import (
        expand_arbitration_final_ledger,
        pack_arbitration_final_ledger,
    )

    try:
        if ledger.get("ledger_packing") is not None:
            return expand_arbitration_final_ledger(ledger)
        pack_arbitration_final_ledger(ledger)
        return copy.deepcopy(dict(ledger))
    except (TypeError, ValueError, KeyError) as exc:
        raise ArbitrationAggregationError("READY legacy ledger failed its canonical integrity check") from exc


def _validate_support_binding(support: Mapping[str, Any], slot: Any, label: str) -> None:
    expected = {
        "claim_slot_id": slot.claim_slot_id,
        "task_id": slot.task_id,
        "paper_key": slot.paper_key,
        "primary_claim_id": slot.primary_claim_id,
        "source_claim_ids": list(slot.source_claim_ids),
        "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids),
    }
    for field, value in expected.items():
        if support.get(field) != value:
            raise ArbitrationAggregationError(f"{label} {field} differs from verified source scope")
    if str(support.get("study_id") or "") != slot.study_id:
        raise ArbitrationAggregationError(f"{label} study_id differs from verified source scope")
    if support.get("claim_group_id") is not None and support.get("claim_group_id") != slot.claim_group_id:
        raise ArbitrationAggregationError(f"{label} claim_group_id differs from CandidateOutputScopeV1")
    if support.get("synthesis_claim_id") is not None and support.get("synthesis_claim_id") != slot.synthesis_claim_id:
        raise ArbitrationAggregationError(f"{label} synthesis_claim_id differs from CandidateOutputScopeV1")


def _section_claim_ids(section: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"claim_id", "synthesis_claim_id", "primary_claim_id"} and isinstance(item, str):
                    result.add(item)
                else:
                    visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                visit(item)

    visit(section)
    return result


def _unique_rows(rows: Any, field: str, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ArbitrationAggregationError(f"{label} must be an array of objects")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        value = _nonempty(row.get(field), f"{label}[{index}].{field}")
        if value in result:
            raise ArbitrationAggregationError(f"{label} repeats {field} {value}")
        result[value] = row
    return result


def _ordered_unique_ids(value: Any, label: str) -> list[str]:
    return list(_string_ids(value, label))


def _exact_hash_map(value: Any, ids: Sequence[str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise ArbitrationAggregationError(f"{label} must exactly match candidate IDs")
    result: dict[str, str] = {}
    for candidate_id in ids:
        result[candidate_id] = _required_hash(value[candidate_id], f"{label}[{candidate_id}]")
    return result


def _required_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ArbitrationAggregationError(f"{label} must be a lowercase SHA-256")
    return value


def _string_hashes(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ArbitrationAggregationError(f"{label} must be an array of SHA-256 values")
    return [_required_hash(row, f"{label}[{index}]") for index, row in enumerate(value)]


def _string_ids(value: Any, label: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ArbitrationAggregationError(f"{label} must be an array of IDs")
    rows = list(value)
    if any(not isinstance(row, str) or not row.strip() for row in rows):
        raise ArbitrationAggregationError(f"{label} contains an empty or non-string ID")
    if len(rows) != len(set(rows)):
        raise ArbitrationAggregationError(f"{label} contains duplicate IDs")
    if required and not rows:
        raise ArbitrationAggregationError(f"{label} is required")
    return rows


def _ordered_unique_texts(rows: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in rows:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationAggregationError(f"{label} must be a non-empty string")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ArbitrationAggregationError(f"{label} must be one of the declared enum values")
    return value


def _aggregate_direction_values(values: Sequence[str] | set[str]) -> str:
    observed = set(values)
    if not observed:
        return "not_comparable"
    if len(observed) == 1:
        return next(iter(observed))
    return "mixed"


def _aggregate_disagreement_values(values: Sequence[str] | set[str]) -> str:
    observed = set(values)
    if not observed:
        return "not_applicable"
    if len(observed) == 1:
        return next(iter(observed))
    return "uncertain"


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> None:
    allowed = required | set(optional or ())
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(allowed):
        missing = sorted(required - keys)
        extra = sorted(keys - allowed)
        raise ArbitrationAggregationError(f"{label} has missing or unknown fields (missing={missing}, extra={extra})")


def _bounded_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationAggregationError(f"{label} must be non-empty bounded text")
    return value


def _token_fill(tokens: int) -> str:
    # ProviderContextProfile's conservative estimator uses UTF-8 bytes/3.
    # A three-byte ASCII fill yields a response skeleton at least as large as
    # the declared text budget under that exact estimator.
    return "x" * (3 * tokens)


__all__ = [
    "ASSESSMENT_INPUT_SCHEMA",
    "ASSESSMENT_RESPONSE_SCHEMA",
    "DECISION_SCOPE_SCHEMA",
    "REDUCTION_INPUT_SCHEMA",
    "REDUCTION_RESPONSE_SCHEMA",
    "SELECTION_DETAIL_KIND",
    "SELECTION_DETAIL_REQUEST_SCHEMA",
    "SELECTION_DETAIL_RESPONSE_SCHEMA",
    "SELECTION_DETAIL_RESULTS_SCHEMA",
    "SELECTION_DETAIL_WORK_PLAN_SCHEMA",
    "SELECTION_FINAL_INPUT_SCHEMA",
    "SELECTION_FINAL_KIND",
    "SELECTION_INPUT_SCHEMA",
    "SELECTION_RESPONSE_SCHEMA",
    "WORK_PLAN_SCHEMA",
    "ArbitrationAggregationError",
    "ArbitrationSerializationCaps",
    "ArbitrationSerializationError",
    "build_arbitration_reducer_request",
    "build_global_selection_detail_requests",
    "build_global_selection_followup_request",
    "build_global_selection_request",
    "build_overflow_assessment_plan",
    "validate_arbitration_assessment_response",
    "validate_arbitration_reduction_response",
    "validate_global_selection_detail_responses",
    "validate_global_selection_response",
]

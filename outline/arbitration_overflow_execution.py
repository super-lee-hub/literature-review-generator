"""Bounded execution driver for the existing PR25 overflow contracts.

The caller supplies the real JIT work-admission, provider-send, and Registry
callbacks. This module does not reserve provider budget, create receipts, or
publish artifacts outside those callbacks.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from outline import candidate_arbitration_navigation as navigation_compiler
from outline.candidate_arbitration_aggregation import (
    SELECTION_DETAIL_KIND,
    SELECTION_FINAL_KIND,
    ArbitrationSerializationCaps,
    build_arbitration_reducer_request,
    build_global_selection_detail_requests,
    build_global_selection_followup_request,
    build_global_selection_request,
    build_overflow_assessment_plan,
    validate_arbitration_assessment_response,
    validate_arbitration_reduction_response,
    validate_global_selection_detail_responses,
    validate_global_selection_response,
)
from outline.candidate_arbitration_navigation import (
    DETAIL_KIND,
    EXTRACT_KIND,
    NAV_KIND,
    REDUCE_KIND,
    build_global_navigation_request,
    build_layout_reduction_requests,
    build_merge_detail_reduction_requests,
    build_merge_group_adjudication_requests,
    build_proposed_merge_detail_requests,
    build_source_detail_index,
    compile_section_coordination,
    compile_selected_candidate_navigation_plan,
    layout_leaf_nodes_from_fragments,
    selected_navigation_call_counts,
    validate_global_navigation_response,
    validate_layout_fragment,
    validate_layout_reduction_response,
    validate_merge_detail_reduction_response,
    validate_merge_detail_responses,
    validate_merge_group_adjudication_responses,
)
from outline.candidate_output_scope import CandidateOutputScopeV1
from outline.v3_models import compute_v3_hash
from services.writer_source_inventory import VerifiedWriterSourceInventoryV1

RequestBuilder = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
InputEstimator = Callable[[Mapping[str, Any]], Any]
OutputEstimator = Callable[[Mapping[str, Any]], Any]
PromptAuthority = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]
RouteEstimator = Callable[[str, Mapping[str, Any]], Any]
OutputEstimatorByKind = Callable[[str, Mapping[str, Any]], Any]
WorkAdmission = Callable[..., Mapping[str, Any]]
ProviderSend = Callable[..., Mapping[str, Any]]
ArtifactPersist = Callable[..., Mapping[str, Any]]
ApplyCoordination = Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]
OutputContract = tuple[int, str]

_TRANSPORT_NODE_ID = "arbitration"
_ASSESSMENT_KIND = "arbitration_leaf_assessment_v2"
_REDUCER_KIND = "arbitration_reduce_new"
_SELECTOR_KIND = "arbitration_decision"
_DETAIL_FINALIZE_KIND = "arbitration_detail_finalize/v2"


class OverflowExecutionError(RuntimeError):
    """The authorized overflow execution path could not be completed safely."""


def _positive_output_tokens(value: Any, label: str, route_cap: int) -> int:
    if type(value) is not int or value < 1 or value > route_cap:
        raise OverflowExecutionError(f"{label} must be a positive measured reserve within the current route cap")
    return value


def _validated_shape_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise OverflowExecutionError(f"{label} must have a full SHA-256 response skeleton hash")
    return value


def _record_output_contract(record: Mapping[str, Any], route_cap: int) -> OutputContract:
    value = record.get("requested_output_tokens")
    measurement = record.get("measurement", record.get("estimate"))
    output_reserve = measurement.get("output_reserve") if isinstance(measurement, Mapping) else None
    if value is None:
        value = output_reserve.get("estimated_output_tokens") if isinstance(output_reserve, Mapping) else None
    output_tokens = _positive_output_tokens(value, "compiled requested_output_tokens", route_cap)
    shape_hashes: list[str] = []
    shape_hash = record.get("response_shape_hash")
    if shape_hash is not None:
        shape_hashes.append(str(shape_hash))
    response_shape = record.get("response_shape")
    if isinstance(response_shape, Mapping):
        shape_hashes.append(compute_v3_hash(dict(response_shape)))
    if isinstance(output_reserve, Mapping) and output_reserve.get("response_shape_hash") is not None:
        shape_hashes.append(str(output_reserve["response_shape_hash"]))
    if not shape_hashes or any(
        len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
        for item in shape_hashes
    ) or len(set(shape_hashes)) != 1:
        raise OverflowExecutionError("compiled full-response skeleton hash is missing or inconsistent")
    return output_tokens, shape_hashes[0]


def _navigation_output_contract(
    record: Mapping[str, Any],
    *,
    kind: str,
    plan: Mapping[str, Any],
    output_estimator: OutputEstimatorByKind,
    context_reserve: Mapping[str, Any],
    route_cap: int,
    route_identity: Any,
) -> OutputContract:
    measurement = record.get("measurement", record.get("estimate"))
    if not isinstance(measurement, Mapping) or measurement.get("route_identity") != route_identity:
        raise OverflowExecutionError(f"{kind} output reserve is foreign to its measured route")
    reserve_builder = getattr(navigation_compiler, "measure_navigation_output_reserve", None)
    if not callable(reserve_builder):
        raise OverflowExecutionError("navigation compiler lacks its complete response output reserve API")
    reserve = reserve_builder(
        plan,
        kind,
        record,
        output_estimator=output_estimator,
        context_reserve=context_reserve,
    )
    if not isinstance(reserve, Mapping):
        raise OverflowExecutionError(f"{kind} compiler output reserve is malformed")
    output_tokens = reserve.get("requested_output_tokens", reserve.get("estimated_output_tokens"))
    response_shape_hash = _validated_shape_hash(reserve.get("response_shape_hash"), f"{kind} response skeleton")
    reserve_route_cap = reserve.get("route_output_cap", reserve.get("max_output_tokens"))
    if reserve_route_cap is not None and reserve_route_cap != route_cap:
        raise OverflowExecutionError(f"{kind} compiler output reserve has a foreign route cap")
    reserve_route_identity = reserve.get("route_identity")
    if reserve_route_identity is not None and reserve_route_identity != route_identity:
        raise OverflowExecutionError(f"{kind} compiler output reserve has a foreign route identity")
    output_reserve = measurement.get("output_reserve")
    if not isinstance(output_reserve, Mapping):
        raise OverflowExecutionError(f"{kind} request record lacks its compiler output reserve")
    if (
        output_reserve.get("estimated_output_tokens") != output_tokens
        or output_reserve.get("requested_output_tokens") != output_tokens
        or output_reserve.get("max_output_tokens") != route_cap
        or output_reserve.get("response_shape_hash") != response_shape_hash
    ):
        raise OverflowExecutionError(f"{kind} output reserve differs from its compiler request record")
    return _positive_output_tokens(output_tokens, f"{kind} complete-schema reserve", route_cap), response_shape_hash


def _prepare_dispatch_records(
    records: Sequence[Mapping[str, Any]],
    *,
    route_cap: int,
    reserve_for: Callable[[Mapping[str, Any]], OutputContract],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for source_record in records:
        record = _payload(source_record)
        measured, shape_hash = reserve_for(record)
        measured = _positive_output_tokens(measured, "requested_output_tokens", route_cap)
        if record.get("requested_output_tokens") not in (None, measured):
            raise OverflowExecutionError("request output reserve differs from its compiler measurement")
        if record.get("response_shape_hash") not in (None, shape_hash):
            raise OverflowExecutionError("response skeleton hash differs from its compiler measurement")
        record["requested_output_tokens"] = measured
        record["response_shape_hash"] = shape_hash
        prepared.append(record)
    return prepared


def _payload(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return copy.deepcopy(dict(result))
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise OverflowExecutionError("execution payload must be a mapping or validator artifact")


def _ready_ref(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OverflowExecutionError(f"{label} callback did not return a Registry reference")
    artifact_id = value.get("artifact_id")
    content_hash = value.get("content_hash")
    if (
        not isinstance(artifact_id, str) or not artifact_id
        or not isinstance(content_hash, str) or len(content_hash) != 64
        or any(char not in "0123456789abcdef" for char in content_hash)
        or str(value.get("status") or "").casefold() != "ready"
    ):
        raise OverflowExecutionError(f"{label} callback did not return a READY artifact reference")
    return copy.deepcopy(dict(value))


def _unique_dependencies(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        for raw in group:
            ref = _ready_ref(raw, "dependency")
            artifact_id = str(ref["artifact_id"])
            prior = by_id.get(artifact_id)
            if prior is not None and prior.get("content_hash") != ref.get("content_hash"):
                raise OverflowExecutionError(f"dependency {artifact_id} has conflicting READY hashes")
            by_id[artifact_id] = ref
    return list(by_id.values())


def _artifact(
    persist: ArtifactPersist,
    *,
    kind: str,
    artifact_id: str,
    payload: Mapping[str, Any],
    dependencies: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ref = persist(
        kind=kind,
        artifact_id=artifact_id,
        payload=copy.deepcopy(dict(payload)),
        dependencies=_unique_dependencies(dependencies),
    )
    return _ready_ref(ref, f"persist({kind})")


def _admit(
    admit_work: WorkAdmission,
    *,
    work_plan: Any,
    kind: str,
    max_count: int,
    request_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    records = [_payload(row) for row in request_records]
    if type(max_count) is not int or max_count < 0 or len(records) > max_count:
        raise OverflowExecutionError(f"{kind} requests exceed their finite source-derived count")
    request_ids = [str(row.get("request_id") or row.get("node_id") or "") for row in records]
    if any(not request_id for request_id in request_ids) or len(request_ids) != len(set(request_ids)):
        raise OverflowExecutionError(f"{kind} request IDs are missing or duplicated")
    admission = admit_work(
        work_plan=_payload(work_plan),
        kind=kind,
        max_count=max_count,
        request_records=records,
    )
    ref = _ready_ref(admission, f"admit_work({kind})")
    if admission.get("kind") not in (None, kind):
        raise OverflowExecutionError(f"{kind} admission reference has a foreign kind")
    if admission.get("request_ids") is not None and list(admission["request_ids"]) != request_ids:
        raise OverflowExecutionError(f"{kind} admission did not bind the exact request list")
    if admission.get("count") is not None and admission["count"] != len(records):
        raise OverflowExecutionError(f"{kind} admission count differs from its exact request list")
    if admission.get("max_count") is not None and admission["max_count"] != max_count:
        raise OverflowExecutionError(f"{kind} admission maximum differs from the source-derived bound")
    return ref


def _request_parts(record: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    request_id = str(record.get("request_id") or record.get("node_id") or "")
    if not request_id:
        raise OverflowExecutionError("provider request record has no stable request ID")
    request = record.get("request")
    if not isinstance(request, Mapping):
        request = record.get("prompt_request")
    if not isinstance(request, Mapping):
        raise OverflowExecutionError(f"provider request {request_id} has no prompt-attached request")
    request_hash = str(record.get("request_hash") or "")
    if not request_hash:
        measurement = record.get("measurement", record.get("estimate"))
        request_hash = str(measurement.get("request_hash") or "") if isinstance(measurement, Mapping) else ""
    if len(request_hash) != 64 or compute_v3_hash(dict(request)) != request_hash:
        raise OverflowExecutionError(f"provider request {request_id} differs from its measured full request hash")
    return request_id, copy.deepcopy(dict(request)), request_hash


def _send_one(
    send: ProviderSend,
    persist: ArtifactPersist,
    *,
    prefix: str,
    kind: str,
    request_record: Mapping[str, Any],
    request_ref: Mapping[str, Any],
    root_dependencies: Sequence[Mapping[str, Any]],
    admission: Mapping[str, Any],
    route_output_cap: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request_id, expected_request, expected_hash = _request_parts(request_record)
    requested_output_tokens, response_shape_hash = _record_output_contract(request_record, route_output_cap)
    dispatch_record = _payload(request_record)
    dispatch_record.update({
        "request_id": request_id,
        "request": expected_request,
        "request_hash": expected_hash,
        "requested_output_tokens": requested_output_tokens,
        "response_shape_hash": response_shape_hash,
        "kind": kind,
        "transport_node_id": _TRANSPORT_NODE_ID,
    })
    sent = send(
        request_record=dispatch_record,
        root_dependencies=_unique_dependencies(root_dependencies),
        admission=_ready_ref(admission, f"send({kind}) admission"),
    )
    if not isinstance(sent, Mapping):
        raise OverflowExecutionError(f"send({kind}) did not return a physical request/receipt audit")
    actual_request = sent.get("actual_request")
    raw_response = sent.get("raw_response")
    receipt_id = sent.get("receipt_id")
    response_hash = sent.get("response_hash")
    replay_status = sent.get("replay_status")
    if (
        sent.get("request_id") != request_id
        or sent.get("transport_node_id") != _TRANSPORT_NODE_ID
        or not isinstance(actual_request, Mapping)
        or compute_v3_hash(dict(actual_request)) != expected_hash
        or sent.get("request_hash") != expected_hash
        or not isinstance(raw_response, Mapping)
        or not isinstance(receipt_id, str) or not receipt_id
        or not isinstance(response_hash, str) or len(response_hash) != 64
        or any(char not in "0123456789abcdef" for char in response_hash)
        or not isinstance(replay_status, str) or not replay_status
    ):
        raise OverflowExecutionError(f"send({kind}) physical request or original receipt audit is incomplete")
    actual_record = _payload(request_record)
    actual_record["request_id"] = request_id
    actual_record["request"] = copy.deepcopy(dict(actual_request))
    actual_record["request_hash"] = expected_hash
    measurement = actual_record.get("measurement", actual_record.get("estimate"))
    if isinstance(measurement, Mapping):
        measurement = copy.deepcopy(dict(measurement))
        measurement["request_hash"] = expected_hash
        actual_record["measurement"] = measurement
    raw_payload = {
        "schema_version": "provider_raw_response_snapshot/v1",
        "request_id": request_id,
        "transport_node_id": _TRANSPORT_NODE_ID,
        "kind": kind,
        "request_hash": expected_hash,
        "requested_output_tokens": requested_output_tokens,
        "response_shape_hash": response_shape_hash,
        "receipt_id": receipt_id,
        "response_hash": response_hash,
        "raw_response": copy.deepcopy(dict(raw_response)),
    }
    raw_ref = _artifact(
        persist,
        kind="provider_raw_response",
        artifact_id=f"{prefix}:raw:{request_id}:{receipt_id}",
        payload=raw_payload,
        dependencies=[*root_dependencies, request_ref, admission],
    )
    return actual_record, copy.deepcopy(dict(raw_response)), raw_ref


def _dispatch_and_validate(
    *,
    send: ProviderSend,
    persist: ArtifactPersist,
    admit_work: WorkAdmission,
    prefix: str,
    kind: str,
    work_plan: Any,
    max_count: int,
    request_records: Sequence[Mapping[str, Any]],
    dependencies: Sequence[Mapping[str, Any]],
    validator: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
    route_output_cap: int,
    reserve_for: Callable[[Mapping[str, Any]], OutputContract],
) -> list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]:
    dispatch_records = _prepare_dispatch_records(
        request_records, route_cap=route_output_cap, reserve_for=reserve_for,
    )
    admission = _admit(
        admit_work, work_plan=work_plan, kind=kind,
        max_count=max_count, request_records=dispatch_records,
    )
    output = []
    for record in dispatch_records:
        request_id = str(record.get("request_id") or record.get("node_id") or "")
        request_ref = _artifact(
            persist,
            kind="provider_request_plan",
            artifact_id=f"{prefix}:request:{request_id}",
            payload=_payload(record),
            dependencies=dependencies,
        )
        actual_record, raw_response, raw_ref = _send_one(
            send, persist, prefix=prefix, kind=kind, request_record=record,
            request_ref=request_ref,
            root_dependencies=[*dependencies, request_ref], admission=admission,
            route_output_cap=route_output_cap,
        )
        validated = validator(actual_record, raw_response)
        validated_payload = _payload(validated)
        validated_ref = _artifact(
            persist,
            kind="provider_validated_result",
            artifact_id=f"{prefix}:validated:{request_id}",
            payload=validated_payload,
            dependencies=[*dependencies, request_ref, raw_ref],
        )
        output.append((validated, actual_record, raw_response, raw_ref, validated_ref))
    return output


def execute_arbitration_overflow_pipeline(
    *,
    prepared_hierarchy: Mapping[str, Any],
    legacy_ledger: Mapping[str, Any],
    candidate_scope: CandidateOutputScopeV1,
    source_inventory: VerifiedWriterSourceInventoryV1,
    review_intent: Any,
    critique_disposition: Mapping[str, Any],
    arbitration_caps: ArbitrationSerializationCaps,
    navigation_context_reserve: Mapping[str, Any],
    navigation_input_caps: Mapping[str, int],
    fnav_max: int,
    emax: int,
    root_dependencies: Sequence[Mapping[str, Any]],
    arbitration_request_builder: RequestBuilder,
    arbitration_input_estimator: InputEstimator,
    arbitration_output_estimator: OutputEstimator,
    navigation_prompt_authority: PromptAuthority,
    navigation_route_estimator: RouteEstimator,
    navigation_output_estimator: OutputEstimatorByKind,
    admit_work: WorkAdmission,
    send: ProviderSend,
    persist: ArtifactPersist,
    apply_section_coordination: ApplyCoordination,
) -> dict[str, Any]:
    """Run the READY overflow compiler through selected-only coordination.

    This function owns ordering and validation only. ``admit_work`` registers
    the finite source-derived exposure; ``send`` owns the real _provider_call /
    ProviderRuntime.admit/receipt path; and ``persist`` owns Registry writes.
    No callback has an implicit or permissive default.
    """

    for name, callback in (
        ("arbitration_request_builder", arbitration_request_builder),
        ("arbitration_input_estimator", arbitration_input_estimator),
        ("arbitration_output_estimator", arbitration_output_estimator),
        ("navigation_prompt_authority", navigation_prompt_authority),
        ("navigation_route_estimator", navigation_route_estimator),
        ("navigation_output_estimator", navigation_output_estimator),
        ("admit_work", admit_work),
        ("send", send),
        ("persist", persist),
        ("apply_section_coordination", apply_section_coordination),
    ):
        if not callable(callback):
            raise OverflowExecutionError(f"{name} callback is required")
    if type(candidate_scope) is not CandidateOutputScopeV1 or not source_inventory.is_verified:
        raise OverflowExecutionError("overflow execution requires the verified candidate scope and source inventory")
    if not isinstance(prepared_hierarchy, Mapping):
        raise OverflowExecutionError("overflow execution requires the READY prepared legacy hierarchy")
    legacy_plan = prepared_hierarchy.get("plan")
    source_request = prepared_hierarchy.get("source_request")
    if not isinstance(legacy_plan, Mapping) or not isinstance(source_request, Mapping):
        raise OverflowExecutionError("prepared legacy hierarchy lacks its exact plan/source request")
    if source_request.get("candidate_contents") is None:
        raise OverflowExecutionError("prepared legacy source request lacks canonical candidate contents")
    intent_payload = review_intent.to_dict() if callable(getattr(review_intent, "to_dict", None)) else review_intent
    if not isinstance(intent_payload, Mapping) or dict(intent_payload) != source_request.get("review_intent"):
        raise OverflowExecutionError("overflow review intent differs from the exact prepared legacy source")
    if dict(critique_disposition) != source_request.get("critique_disposition"):
        raise OverflowExecutionError("overflow critique disposition differs from the exact prepared legacy source")
    arbitration_caps.validate()
    required_navigation_kinds = {EXTRACT_KIND, REDUCE_KIND, NAV_KIND, DETAIL_KIND}
    if not isinstance(navigation_input_caps, Mapping) or not required_navigation_kinds.issubset(navigation_input_caps):
        raise OverflowExecutionError("navigation input caps must cover extraction, reduction, global, and detail kinds")
    normalized_input_caps = {}
    for kind in required_navigation_kinds:
        cap = navigation_input_caps[kind]
        if type(cap) is not int:
            raise OverflowExecutionError(f"navigation input cap for {kind} must be an exact integer")
        normalized_input_caps[kind] = cap
    if any(cap < 1 for cap in normalized_input_caps.values()):
        raise OverflowExecutionError("navigation effective input caps must be positive")
    if not isinstance(navigation_context_reserve, Mapping):
        raise OverflowExecutionError("navigation requires explicit route context and output reserves")
    navigation_output_caps: dict[str, int] = {}
    for kind in required_navigation_kinds:
        context = navigation_context_reserve.get(kind)
        if not isinstance(context, Mapping):
            raise OverflowExecutionError(f"navigation route context is missing for {kind}")
        cap = context.get("max_output_tokens")
        if type(cap) is not int or cap < 1:
            raise OverflowExecutionError(f"navigation route output cap is missing for {kind}")
        navigation_output_caps[kind] = cap
    if type(fnav_max) is not int or fnav_max < 0 or type(emax) is not int or emax < 0:
        raise OverflowExecutionError("selected navigation and merge detail maxima must be finite non-negative integers")
    root_deps = _unique_dependencies(root_dependencies)
    if not root_deps:
        raise OverflowExecutionError("overflow execution requires READY Registry dependency references")
    prefix = str(prepared_hierarchy.get("node_id") or "").strip()
    if not prefix:
        raise OverflowExecutionError("prepared legacy hierarchy has no stable job node prefix")
    arbitration_output_cap = int(arbitration_caps.route_max_output)

    def arbitration_reserve(record: Mapping[str, Any]) -> OutputContract:
        return _record_output_contract(record, arbitration_output_cap)

    def navigation_reserve(kind: str, plan: Mapping[str, Any]) -> Callable[[Mapping[str, Any]], OutputContract]:
        return lambda record: _navigation_output_contract(
            record,
            kind=kind,
            plan=plan,
            output_estimator=navigation_output_estimator,
            context_reserve=navigation_context_reserve,
            route_cap=navigation_output_caps[kind],
            route_identity=navigation_context_reserve[kind].get("route_identity"),
        )

    artifacts: list[dict[str, Any]] = []
    selector_detail_calls = 0
    final_selector_calls = 0

    def persist_ready(**kwargs: Any) -> dict[str, Any]:
        ref = _ready_ref(persist(**kwargs), f"persist({kwargs.get('kind', 'artifact')})")
        artifacts.append(ref)
        return ref

    def save(kind: str, artifact_id: str, payload: Any, dependencies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        ref = _artifact(
            persist_ready,
            kind=kind,
            artifact_id=artifact_id,
            payload=_payload(payload),
            dependencies=dependencies,
        )
        return ref

    def save_request(record: Mapping[str, Any], kind: str, dependencies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        request_id = str(record.get("request_id") or record.get("node_id") or "")
        return save(
            "provider_request_plan",
            f"{prefix}:request:{kind}:{request_id}",
            record,
            dependencies,
        )

    work = build_overflow_assessment_plan(
        legacy_plan=legacy_plan,
        legacy_ledger=legacy_ledger,
        legacy_source_request=source_request,
        candidate_contents=source_request["candidate_contents"],
        candidate_scope=candidate_scope,
        source_inventory=source_inventory,
        review_intent=source_request["review_intent"],
        critique_disposition=source_request["critique_disposition"],
        request_builder=arbitration_request_builder,
        estimate_input_tokens=arbitration_input_estimator,
        estimate_output_tokens=arbitration_output_estimator,
        caps=arbitration_caps,
    )
    work_ref = save(
        "arbitration_overflow_work_plan",
        f"{prefix}:overflow-work-plan:{work['work_plan_hash'][:24]}",
        work,
        root_deps,
    )
    ready_by_id: dict[str, Any] = {}
    ready_ref_by_id: dict[str, dict[str, Any]] = {}

    leaves = list(work["assessment_leaves"])
    leaf_results = _dispatch_and_validate(
        send=send,
        persist=persist_ready,
        admit_work=admit_work,
        prefix=prefix,
        kind=_ASSESSMENT_KIND,
        work_plan=work,
        max_count=int(work["counts"]["new_assessment_leaf_count"]),
        request_records=leaves,
        dependencies=[*root_deps, work_ref],
        validator=lambda _actual, raw: validate_arbitration_assessment_response(
            work,
            str(_actual.get("request_id") or ""),
            raw,
            estimate_output_tokens=arbitration_output_estimator,
        ),
        route_output_cap=arbitration_output_cap,
        reserve_for=arbitration_reserve,
    )
    for leaf, (result, _actual, _raw, _raw_ref, result_ref) in zip(leaves, leaf_results, strict=True):
        node_id = str(leaf["node_id"])
        ready_by_id[node_id] = result
        ready_ref_by_id[node_id] = result_ref

    for reducer in work["reducer_tree"]["nodes"]:
        child_ids = list(reducer["child_node_ids"])
        children = {child_id: ready_by_id[child_id] for child_id in child_ids}
        reducer_request = build_arbitration_reducer_request(
            work,
            str(reducer["tree_node_id"]),
            children,
            request_builder=arbitration_request_builder,
            estimate_input_tokens=arbitration_input_estimator,
            estimate_output_tokens=arbitration_output_estimator,
            caps=arbitration_caps,
        )
        child_refs = [ready_ref_by_id[child_id] for child_id in child_ids]
        reduced = _dispatch_and_validate(
            send=send,
            persist=persist_ready,
            admit_work=admit_work,
            prefix=prefix,
            kind=_REDUCER_KIND,
            work_plan=work,
            max_count=int(work["counts"]["new_reducer_count"]),
            request_records=[reducer_request],
            dependencies=[*root_deps, work_ref, *child_refs],
            validator=lambda _actual, raw, req=reducer_request, children=children: validate_arbitration_reduction_response(
                work, req, raw, children, estimate_output_tokens=arbitration_output_estimator,
            ),
            route_output_cap=arbitration_output_cap,
            reserve_for=arbitration_reserve,
        )[0]
        ready_by_id[str(reducer["tree_node_id"])], ready_ref_by_id[str(reducer["tree_node_id"])] = reduced[0], reduced[4]

    root_id = str(work["reducer_root_node_id"])
    aggregation_root = ready_by_id[root_id]
    leaf_result_map = {
        str(leaf["node_id"]): ready_by_id[str(leaf["node_id"])]
        for leaf in leaves
    }
    selector_request = build_global_selection_request(
        work,
        aggregation_root,
        leaf_result_map,
        expected_view_set_hash=str(work["critical_view_set"]["view_set_hash"]),
        request_builder=arbitration_request_builder,
        estimate_input_tokens=arbitration_input_estimator,
        estimate_output_tokens=arbitration_output_estimator,
        caps=arbitration_caps,
    )
    selector_ready = _dispatch_and_validate(
        send=send,
        persist=persist_ready,
        admit_work=admit_work,
        prefix=prefix,
        kind=_SELECTOR_KIND,
        work_plan=work,
        max_count=1,
        request_records=[selector_request],
        dependencies=[*root_deps, work_ref, ready_ref_by_id[root_id], *[ready_ref_by_id[key] for key in leaf_result_map]],
        validator=lambda _actual, raw: validate_global_selection_response(
            work, selector_request, raw, estimate_output_tokens=arbitration_output_estimator,
        ),
        route_output_cap=arbitration_output_cap,
        reserve_for=arbitration_reserve,
    )[0]
    selection_result, _selector_actual, _selector_raw, _selector_raw_ref, selection_ref = selector_ready

    if selection_result["outcome"] == "detail_required":
        detail_work = build_global_selection_detail_requests(
            work,
            selector_request,
            selection_result,
            source_inventory,
            request_builder=arbitration_request_builder,
            estimate_input_tokens=arbitration_input_estimator,
            estimate_output_tokens=arbitration_output_estimator,
            caps=arbitration_caps,
        )
        detail_payload = _payload(detail_work)
        detail_work_ref = save(
            "selector_detail_work_plan",
            f"{prefix}:selector-detail-work:{detail_payload['work_plan_hash'][:24]}",
            detail_work,
            [*root_deps, work_ref, selection_ref],
        )
        detail_requests = _prepare_dispatch_records(
            list(detail_work["requests"]),
            route_cap=arbitration_output_cap,
            reserve_for=arbitration_reserve,
        )
        selector_detail_calls = len(detail_requests)
        detail_admission = _admit(
            admit_work,
            work_plan=detail_work,
            kind=SELECTION_DETAIL_KIND,
            max_count=int(detail_work["counts"]["Qselector_detail"]),
            request_records=detail_requests,
        )
        detail_responses: list[dict[str, Any]] = []
        actual_detail_requests: list[dict[str, Any]] = []
        raw_detail_refs: list[dict[str, Any]] = []
        for detail_request in detail_requests:
            request_id = str(detail_request["request_id"])
            request_ref = save_request(
                detail_request, SELECTION_DETAIL_KIND, [*root_deps, detail_work_ref, selection_ref],
            )
            actual_record, raw_response, raw_ref = _send_one(
                send,
                persist_ready,
                prefix=prefix,
                kind=SELECTION_DETAIL_KIND,
                request_record=detail_request,
                request_ref=request_ref,
                root_dependencies=[*root_deps, detail_work_ref, selection_ref, request_ref],
                admission=detail_admission,
                route_output_cap=arbitration_output_cap,
            )
            actual_detail_requests.append(actual_record)
            detail_responses.append({"request_id": request_id, "response": raw_response})
            raw_detail_refs.append(raw_ref)
        detail_results = validate_global_selection_detail_responses(
            work,
            selector_request,
            selection_result,
            detail_work,
            detail_responses,
            source_inventory=source_inventory,
            actual_requests=actual_detail_requests,
            estimate_input_tokens=arbitration_input_estimator,
            estimate_output_tokens=arbitration_output_estimator,
            caps=arbitration_caps,
        )
        detail_result_payload = _payload(detail_results)
        detail_result_ref = save(
            "selector_detail_results",
            f"{prefix}:selector-detail-results:{detail_result_payload['result_hash'][:24]}",
            detail_results,
            [*root_deps, detail_work_ref, selection_ref, *raw_detail_refs],
        )
        final_selector = build_global_selection_followup_request(
            work,
            selector_request,
            selection_result,
            detail_work,
            detail_results,
            source_inventory,
            request_builder=arbitration_request_builder,
            estimate_input_tokens=arbitration_input_estimator,
            estimate_output_tokens=arbitration_output_estimator,
            caps=arbitration_caps,
        )
        final_selector_result = _dispatch_and_validate(
            send=send,
            persist=persist_ready,
            admit_work=admit_work,
            prefix=prefix,
            kind=SELECTION_FINAL_KIND,
            work_plan=detail_work,
            max_count=int(detail_work["counts"]["Qfinal_selector"]),
            request_records=[final_selector],
            dependencies=[*root_deps, work_ref, ready_ref_by_id[root_id], *[ready_ref_by_id[key] for key in leaf_result_map], selection_ref, detail_work_ref, detail_result_ref],
            validator=lambda _actual, raw: validate_global_selection_response(
                work, final_selector, raw, estimate_output_tokens=arbitration_output_estimator,
            ),
            route_output_cap=arbitration_output_cap,
            reserve_for=arbitration_reserve,
        )[0]
        selection_result, _final_actual, _final_raw, _final_raw_ref, selection_ref = final_selector_result
        if selection_result["outcome"] != "selected":
            raise OverflowExecutionError("the single bounded final selector did not resolve detail_required")
        final_selector_calls = 1

    selected_candidate_id = str(selection_result.get("selected_candidate_id") or "")
    if selected_candidate_id not in work["candidate_ids"]:
        raise OverflowExecutionError("validated selector did not provide one exact eligible candidate")
    selected_candidate_hash = str(work["candidate_hashes"][selected_candidate_id])
    selected_content = source_request["candidate_contents"].get(selected_candidate_id)
    if not isinstance(selected_content, Mapping) or not isinstance(selected_content.get("sections"), list):
        raise OverflowExecutionError("selected original candidate sections are missing from the READY source")
    original_sections = [copy.deepcopy(dict(row)) for row in selected_content["sections"] if isinstance(row, Mapping)]
    if len(original_sections) != len(selected_content["sections"]):
        raise OverflowExecutionError("selected original candidate sections contain malformed rows")

    navigation_input_cap = min(normalized_input_caps[kind] for kind in (EXTRACT_KIND, REDUCE_KIND, NAV_KIND))
    navigation_plan = compile_selected_candidate_navigation_plan(
        selected_candidate_id,
        selected_candidate_hash,
        original_sections,
        source_request["review_intent"],
        prompt_authority=navigation_prompt_authority,
        route_estimator=navigation_route_estimator,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        input_cap=navigation_input_cap,
        fnav_max=fnav_max,
        emax=emax,
    )
    nav_counts = selected_navigation_call_counts(navigation_plan)
    navigation_plan_ref = save(
        "selected_candidate_navigation_plan",
        f"{prefix}:navigation-plan:{navigation_plan['plan_integrity_hash'][:24]}",
        navigation_plan,
        [*root_deps, work_ref, selection_ref],
    )
    if not navigation_plan["requires_coordination"]:
        return {
            "status": "selected_coordination_ready",
            "selected_candidate_id": selected_candidate_id,
            "selected_candidate_hash": selected_candidate_hash,
            "selection_result": selection_result,
            "navigation_plan": navigation_plan,
            "coordination": None,
            "coordinated_sections": original_sections,
            "coordination_audit": None,
            "counts": {
                "Qselector_initial": 1,
                "Qselector_detail": selector_detail_calls,
                "Qfinal_selector": final_selector_calls,
                **nav_counts,
            },
            "ready_artifacts": list(artifacts),
        }

    extraction_records = list(navigation_plan["layout_extraction_requests"])
    extraction_results = _dispatch_and_validate(
        send=send,
        persist=persist_ready,
        admit_work=admit_work,
        prefix=prefix,
        kind=EXTRACT_KIND,
        work_plan=navigation_plan,
        max_count=int(navigation_plan["counts"]["Jnav"]),
        request_records=extraction_records,
        dependencies=[*root_deps, work_ref, selection_ref, navigation_plan_ref],
        validator=lambda actual, raw: validate_layout_fragment(
            navigation_plan, actual, raw,
            output_estimator=navigation_output_estimator,
            context_reserve=navigation_context_reserve,
        ),
        route_output_cap=navigation_output_caps[EXTRACT_KIND],
        reserve_for=navigation_reserve(EXTRACT_KIND, navigation_plan),
    )
    fragments = [result[0] for result in extraction_results]
    ready_layout = {
        node["node_id"]: node
        for node in layout_leaf_nodes_from_fragments(navigation_plan, fragments)
    }
    fragment_refs_by_span = {
        str(fragment["span_id"]): result_ref
        for (fragment, _actual, _raw, _raw_ref, result_ref) in extraction_results
    }
    layout_leaf_refs: dict[str, dict[str, Any]] = {}
    for node in ready_layout.values():
        ref = save(
            "selected_candidate_navigation_leaf",
            f"{prefix}:navigation-leaf:{node['node_id']}",
            node,
            [*root_deps, navigation_plan_ref, *[
                fragment_refs_by_span[span_id] for span_id in node["span_ids"]
            ]],
        )
        layout_leaf_refs[str(node["node_id"])] = ref

    for level_plan in navigation_plan["layout_reduction_tree"]["levels"]:
        level = int(level_plan["level"])
        inputs = [ready_layout[node_id] for node_id in level_plan["input_node_ids"]]
        level_requests = build_layout_reduction_requests(
            navigation_plan,
            level,
            inputs,
            prompt_authority=navigation_prompt_authority,
            route_estimator=navigation_route_estimator,
            output_estimator=navigation_output_estimator,
            context_reserve=navigation_context_reserve,
            input_cap=normalized_input_caps[REDUCE_KIND],
        )
        reduced_refs: dict[str, dict[str, Any]] = {}
        if level_requests["requests"]:
            reduced = _dispatch_and_validate(
                send=send,
                persist=persist_ready,
                admit_work=admit_work,
                prefix=prefix,
                kind=REDUCE_KIND,
                work_plan=navigation_plan,
                max_count=int(navigation_plan["counts"]["Anav"]),
                request_records=level_requests["requests"],
                dependencies=[*root_deps, navigation_plan_ref, *[
                    layout_leaf_refs[node_id]
                    for node_id in level_plan["input_node_ids"] if node_id in layout_leaf_refs
                ]],
                validator=lambda actual, raw, input_nodes=inputs: validate_layout_reduction_response(
                    navigation_plan,
                    actual,
                    raw,
                    input_nodes=[
                        next(node for node in input_nodes if node["node_id"] == child_id)
                        for child_id in actual["body"]["child_node_ids"]
                    ],
                    output_estimator=navigation_output_estimator,
                    context_reserve=navigation_context_reserve,
                ),
                route_output_cap=navigation_output_caps[REDUCE_KIND],
                reserve_for=navigation_reserve(REDUCE_KIND, navigation_plan),
            )
            for result, _actual, _raw, _raw_ref, result_ref in reduced:
                node_id = str(result["node_id"])
                ready_layout[node_id] = result
                reduced_refs[node_id] = result_ref
        next_ready = dict(reduced_refs)
        for carry_id in level_requests["carry_node_ids"]:
            next_ready[carry_id] = layout_leaf_refs[carry_id]
        ready_layout = {
            node_id: ready_layout[node_id]
            for node_id in level_requests["output_node_ids"]
        }
        layout_leaf_refs = next_ready

    layout_root_id = str(navigation_plan["layout_reduction_tree"]["root_node_id"])
    layout_root = ready_layout[layout_root_id]
    nav_request = build_global_navigation_request(
        navigation_plan,
        layout_root,
        prompt_authority=navigation_prompt_authority,
        route_estimator=navigation_route_estimator,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        input_cap=normalized_input_caps[NAV_KIND],
    )
    nav_result = _dispatch_and_validate(
        send=send,
        persist=persist_ready,
        admit_work=admit_work,
        prefix=prefix,
        kind=NAV_KIND,
        work_plan=navigation_plan,
        max_count=1,
        request_records=[nav_request],
        dependencies=[*root_deps, navigation_plan_ref, *[ref for ref in layout_leaf_refs.values()]],
        validator=lambda actual, raw: validate_global_navigation_response(
            navigation_plan,
            actual,
            raw,
            output_estimator=navigation_output_estimator,
            context_reserve=navigation_context_reserve,
        ),
        route_output_cap=navigation_output_caps[NAV_KIND],
        reserve_for=navigation_reserve(NAV_KIND, navigation_plan),
    )[0]
    proposal, _nav_actual, _nav_raw, _nav_raw_ref, proposal_ref = nav_result
    source_detail_index = build_source_detail_index(navigation_plan)
    detail_work = build_proposed_merge_detail_requests(
        navigation_plan,
        proposal,
        source_detail_index,
        prompt_authority=navigation_prompt_authority,
        route_estimator=navigation_route_estimator,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        input_cap=normalized_input_caps[DETAIL_KIND],
        emax=emax,
    )
    total_nav_counts = selected_navigation_call_counts(navigation_plan, detail_work)
    detail_payload = _payload(detail_work)
    detail_work_ref = save(
        "selected_candidate_merge_detail_work_plan",
        f"{prefix}:merge-detail-work:{detail_payload['work_plan_hash'][:24]}",
        detail_work,
        [*root_deps, navigation_plan_ref, proposal_ref],
    )
    detail_index_ref = save(
        "selected_candidate_source_detail_index",
        f"{prefix}:source-detail-index:{source_detail_index['index_hash'][:24]}",
        source_detail_index,
        [navigation_plan_ref],
    )
    slice_records = _prepare_dispatch_records(
        list(detail_work["requests"]),
        route_cap=navigation_output_caps[DETAIL_KIND],
        reserve_for=navigation_reserve(DETAIL_KIND, navigation_plan),
    )
    actual_slice_requests: list[dict[str, Any]] = []
    slice_responses: list[dict[str, Any]] = []
    raw_slice_refs: list[dict[str, Any]] = []
    slice_admission = None
    if slice_records:
        slice_admission = _admit(
            admit_work,
            work_plan=detail_work,
            kind=DETAIL_KIND,
            max_count=int(detail_payload["counts"]["Qdetail_slice"]) + int(detail_payload["counts"]["Qdetail_reduce"]),
            request_records=slice_records,
        )
    if slice_records and slice_admission is None:
        raise OverflowExecutionError("detail slice requests were not admitted before transport")
    for record in slice_records:
        if slice_admission is None:
            raise OverflowExecutionError("detail slice request has no READY admission")
        request_id = str(record["request_id"])
        request_ref = save_request(
            record, DETAIL_KIND, [*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, detail_index_ref],
        )
        actual_record, raw_response, raw_ref = _send_one(
            send,
            persist_ready,
            prefix=prefix,
            kind=DETAIL_KIND,
            request_record=record,
            request_ref=request_ref,
            root_dependencies=[*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, detail_index_ref, request_ref],
            admission=slice_admission,
            route_output_cap=navigation_output_caps[DETAIL_KIND],
        )
        actual_slice_requests.append(actual_record)
        slice_responses.append({"request_id": request_id, "response": raw_response})
        raw_slice_refs.append(raw_ref)
    slice_results = validate_merge_detail_responses(
        navigation_plan,
        proposal,
        detail_work,
        slice_responses,
        detail_index=source_detail_index,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        actual_requests=actual_slice_requests,
    )
    slice_results_payload = _payload(slice_results)
    slice_results_ref = save(
        "selected_candidate_merge_detail_slice_results",
        f"{prefix}:merge-detail-slices:{slice_results_payload['detail_work_plan_hash'][:24]}",
        slice_results,
        [*root_deps, detail_work_ref, detail_index_ref, *raw_slice_refs],
    )
    reduction_roots: dict[str, Any] = {}
    reduction_root_refs: list[dict[str, Any]] = []
    detail_refs: dict[str, dict[str, Any]] = {
        str(node["node_id"]): slice_results_ref
        for group in slice_results["groups"].values()
        for node in group["slice_nodes"]
    }
    slice_groups = slice_results["groups"]
    for group_id, tree in detail_payload["adjudication_trees"].items():
        ready_detail = {
            str(row["node_id"]): row
            for row in slice_groups[group_id]["slice_nodes"]
        }
        for level_plan in tree["levels"]:
            level = int(level_plan["level"])
            ready_nodes = [ready_detail[node_id] for node_id in level_plan["input_node_ids"]]
            level_requests = build_merge_detail_reduction_requests(
                navigation_plan,
                proposal,
                detail_work,
                slice_results,
                str(group_id),
                level,
                ready_nodes,
                detail_index=source_detail_index,
                prompt_authority=navigation_prompt_authority,
                route_estimator=navigation_route_estimator,
                output_estimator=navigation_output_estimator,
                context_reserve=navigation_context_reserve,
                input_cap=normalized_input_caps[DETAIL_KIND],
            )
            reduced_by_id: dict[str, Any] = {}
            reduced_refs: dict[str, dict[str, Any]] = {}
            prepared_level_requests = _prepare_dispatch_records(
                level_requests["requests"],
                route_cap=navigation_output_caps[DETAIL_KIND],
                reserve_for=navigation_reserve(DETAIL_KIND, navigation_plan),
            )
            if prepared_level_requests:
                reducer_admission = _admit(
                    admit_work,
                    work_plan=detail_work,
                    kind=DETAIL_KIND,
                    max_count=int(detail_payload["counts"]["Qdetail_slice"]) + int(detail_payload["counts"]["Qdetail_reduce"]),
                    request_records=prepared_level_requests,
                )
                for request_record in prepared_level_requests:
                    request_id = str(request_record["request_id"])
                    children = {
                        str(node["node_id"]): node
                        for node in ready_nodes
                        if str(node["node_id"]) in set(request_record["child_node_ids"])
                    }
                    request_ref = save_request(
                        request_record,
                        DETAIL_KIND,
                        [*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, slice_results_ref, detail_index_ref],
                    )
                    actual_record, raw_response, raw_ref = _send_one(
                        send,
                        persist_ready,
                        prefix=prefix,
                        kind=DETAIL_KIND,
                        request_record=request_record,
                        request_ref=request_ref,
                        root_dependencies=[*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, slice_results_ref, detail_index_ref, request_ref],
                        admission=reducer_admission,
                        route_output_cap=navigation_output_caps[DETAIL_KIND],
                    )
                    node = validate_merge_detail_reduction_response(
                        navigation_plan,
                        proposal,
                        detail_work,
                        slice_results,
                        actual_record,
                        raw_response,
                        [children[node_id] for node_id in request_record["child_node_ids"]],
                        output_estimator=navigation_output_estimator,
                        context_reserve=navigation_context_reserve,
                    )
                    node_payload = _payload(node)
                    node_ref = save(
                        "selected_candidate_merge_detail_reduction_node",
                        f"{prefix}:merge-detail-reducer:{node_payload['node_id']}",
                        node,
                        [*root_deps, request_ref, raw_ref, detail_work_ref, slice_results_ref, *[
                            _ready_ref(detail_refs[node_id], "detail reducer child")
                            for node_id in request_record["child_node_ids"]
                            if node_id in detail_refs
                        ]],
                    )
                    reduced_by_id[request_id] = node
                    reduced_refs[request_id] = node_ref
            next_nodes = dict(reduced_by_id)
            next_refs = dict(reduced_refs)
            for carry_id in level_requests["carry_node_ids"]:
                next_nodes[carry_id] = ready_detail[carry_id]
                if carry_id not in detail_refs:
                    carry_payload = _payload(ready_detail[carry_id])
                    detail_refs[carry_id] = save(
                        "selected_candidate_merge_detail_carry",
                        f"{prefix}:merge-detail-carry:{carry_id}",
                        carry_payload,
                        [*root_deps, detail_work_ref, slice_results_ref],
                    )
                next_refs[carry_id] = detail_refs[carry_id]
            ready_detail = next_nodes
            detail_refs.update(next_refs)
        root_node_id = str(tree["root_node_id"])
        reduction_roots[str(group_id)] = ready_detail[root_node_id]
        reduction_root_refs.append(detail_refs[root_node_id])

    layout_root_id = str(navigation_plan["layout_reduction_tree"]["root_node_id"])
    final_plan = build_merge_group_adjudication_requests(
        navigation_plan,
        proposal,
        detail_work,
        slice_results,
        reduction_roots,
        ready_layout[layout_root_id],
        source_detail_index,
        prompt_authority=navigation_prompt_authority,
        route_estimator=navigation_route_estimator,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        input_cap=normalized_input_caps[DETAIL_KIND],
    )
    final_plan_payload = _payload(final_plan)
    final_plan_ref = save(
        "selected_candidate_merge_group_adjudication_plan",
        f"{prefix}:merge-detail-final-plan:{final_plan_payload['final_plan_hash'][:24]}",
        final_plan,
        [*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, slice_results_ref, *reduction_root_refs],
    )
    final_records = _prepare_dispatch_records(
        list(final_plan["requests"]),
        route_cap=navigation_output_caps[DETAIL_KIND],
        reserve_for=navigation_reserve(DETAIL_KIND, navigation_plan),
    )
    final_responses: list[dict[str, Any]] = []
    actual_final_requests: list[dict[str, Any]] = []
    raw_final_refs: list[dict[str, Any]] = []
    final_admission = None
    if final_records:
        final_admission = _admit(
            admit_work,
            work_plan=detail_work,
            kind=_DETAIL_FINALIZE_KIND,
            max_count=int(detail_payload["counts"]["Qdetail_final"]),
            request_records=final_records,
        )
    if final_records and final_admission is None:
        raise OverflowExecutionError("detail finalizers were not admitted before transport")
    for request_record in final_records:
        if final_admission is None:
            raise OverflowExecutionError("detail finalizer request has no READY admission")
        request_id = str(request_record["request_id"])
        request_ref = save_request(
            request_record,
            _DETAIL_FINALIZE_KIND,
            [*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, slice_results_ref, final_plan_ref],
        )
        actual_record, raw_response, raw_ref = _send_one(
            send,
            persist_ready,
            prefix=prefix,
            kind=_DETAIL_FINALIZE_KIND,
            request_record=request_record,
            request_ref=request_ref,
            root_dependencies=[*root_deps, navigation_plan_ref, proposal_ref, detail_work_ref, slice_results_ref, final_plan_ref, request_ref],
            admission=final_admission,
            route_output_cap=navigation_output_caps[DETAIL_KIND],
        )
        actual_final_requests.append(actual_record)
        final_responses.append({"request_id": request_id, "response": raw_response})
        raw_final_refs.append(raw_ref)
    group_results = validate_merge_group_adjudication_responses(
        navigation_plan,
        proposal,
        detail_work,
        final_plan,
        final_responses,
        output_estimator=navigation_output_estimator,
        context_reserve=navigation_context_reserve,
        actual_requests=actual_final_requests,
    )
    group_results_payload = _payload(group_results)
    group_results_ref = save(
        "selected_candidate_merge_group_adjudications",
        f"{prefix}:merge-group-results:{group_results_payload['final_plan_hash'][:24]}",
        group_results,
        [*root_deps, final_plan_ref, *raw_final_refs],
    )
    coordination = compile_section_coordination(navigation_plan, proposal, group_results)
    coordinated_sections, coordination_audit = apply_section_coordination(
        selected_candidate_id,
        original_sections,
        coordination,
        parent_hash=selected_candidate_hash,
    )
    return {
        "status": "selected_coordination_ready",
        "selected_candidate_id": selected_candidate_id,
        "selected_candidate_hash": selected_candidate_hash,
        "selection_result": selection_result,
        "navigation_plan": navigation_plan,
        "navigation_proposal": proposal,
        "detail_work_plan": detail_work,
        "detail_results": slice_results,
        "final_group_results": group_results,
        "coordination": coordination,
        "coordinated_sections": coordinated_sections,
        "coordination_audit": coordination_audit,
        "counts": {
            "Qselector_initial": 1,
            "Qselector_detail": selector_detail_calls,
            "Qfinal_selector": final_selector_calls,
            **total_nav_counts,
        },
        "ready_artifacts": list(artifacts),
        "final_group_results_ref": group_results_ref,
    }

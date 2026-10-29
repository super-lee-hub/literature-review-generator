"""Pure, source-bound selected-candidate layout and merge navigation compiler.

This module only prepares and validates logical provider bodies.  It does not
call a provider, write a Registry artifact, reserve budget, or mutate candidate
source.  The executor owns those operations and supplies the current prompt,
route, output, and context measurement callbacks at each JIT boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from outline.v3_models import compute_v3_hash

PLAN_SCHEMA = "selected_candidate_navigation_plan/v1"
EXTRACT_REQUEST_SCHEMA = "selected_candidate_layout_extract_request/v1"
EXTRACT_RESPONSE_SCHEMA = "selected_candidate_layout_fragment/v1"
REDUCE_REQUEST_SCHEMA = "selected_candidate_layout_reduce_request/v1"
REDUCE_RESPONSE_SCHEMA = "selected_candidate_layout_reduction/v1"
NAV_REQUEST_SCHEMA = "selected_candidate_navigation_request/v3"
NAV_RESPONSE_SCHEMA = "selected_candidate_navigation_response/v3"
DETAIL_INDEX_SCHEMA = "selected_candidate_source_detail_index/v1"
DETAIL_REQUEST_SCHEMA = "selected_candidate_merge_detail_slice_request/v1"
DETAIL_SLICE_RESPONSE_SCHEMA = "selected_candidate_merge_detail_slice_response/v1"
DETAIL_REDUCE_REQUEST_SCHEMA = "selected_candidate_merge_detail_reduce_request/v1"
DETAIL_REDUCE_RESPONSE_SCHEMA = "selected_candidate_merge_detail_reduction/v1"
DETAIL_FINAL_REQUEST_SCHEMA = "selected_candidate_merge_group_adjudication_request/v1"
DETAIL_RESPONSE_SCHEMA = "selected_candidate_merge_group_adjudication_response/v1"
DETAIL_WORK_PLAN_SCHEMA = "selected_candidate_merge_detail_work_plan/v2"
DETAIL_SLICE_RESULTS_SCHEMA = "selected_candidate_merge_detail_slice_results/v1"
DETAIL_REDUCTION_NODE_SCHEMA = "selected_candidate_merge_detail_reduction_node/v1"
DETAIL_GROUP_RESULTS_SCHEMA = "selected_candidate_merge_group_adjudications/v1"

EXTRACT_KIND = "arbitration_layout_extract"
REDUCE_KIND = "arbitration_layout_reduce"
NAV_KIND = "arbitration_section_navigation"
DETAIL_KIND = "arbitration_merge_detail_review"

_ORG_SECTION_FIELDS = ("title", "goal", "rationale", "task_ids")
_HASH_FIELDS = ("candidate_hash", "section_hash", "source_hash", "value_hash")
_SEAL_TOKEN = object()


class SelectedNavigationError(ValueError):
    """A selected-only navigation plan or response is incomplete or over bound."""


class _SealedArtifact(Mapping[str, Any]):
    """Read-only validated evidence; mapping access returns detached values."""

    __slots__ = ("_kind", "_payload_hash", "_payload_json", "_provenance_json", "_seal_hash")

    def __init__(self, kind: str, payload: Mapping[str, Any], provenance: Mapping[str, Any], *, _token: object):
        if _token is not _SEAL_TOKEN:
            raise SelectedNavigationError("validated evidence artifacts are created only by validators")
        payload_json = _json_bytes(dict(payload)).decode("utf-8")
        provenance_json = _json_bytes(dict(provenance)).decode("utf-8")
        payload_hash = compute_v3_hash(json.loads(payload_json))
        seal_hash = compute_v3_hash({
            "kind": kind, "payload_hash": payload_hash,
            "provenance_hash": compute_v3_hash(json.loads(provenance_json)),
        })
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_payload_json", payload_json)
        object.__setattr__(self, "_payload_hash", payload_hash)
        object.__setattr__(self, "_provenance_json", provenance_json)
        object.__setattr__(self, "_seal_hash", seal_hash)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("validated evidence artifacts are immutable")

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(json.loads(self._payload_json)[key])

    def __iter__(self):
        return iter(json.loads(self._payload_json))

    def __len__(self) -> int:
        return len(json.loads(self._payload_json))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(json.loads(self._payload_json))


def _sealed_payload(value: Any, kind: str) -> tuple[dict[str, Any], str]:
    if not isinstance(value, _SealedArtifact) or value._kind != kind:
        raise SelectedNavigationError(f"{kind} requires immutable validator-issued evidence")
    payload = json.loads(value._payload_json)
    provenance = json.loads(value._provenance_json)
    if compute_v3_hash(payload) != value._payload_hash:
        raise SelectedNavigationError(f"{kind} payload seal changed")
    expected_seal = compute_v3_hash({
        "kind": value._kind,
        "payload_hash": value._payload_hash,
        "provenance_hash": compute_v3_hash(provenance),
    })
    if expected_seal != value._seal_hash:
        raise SelectedNavigationError(f"{kind} provenance seal changed")
    return payload, value._seal_hash


def _validated_detail_work_plan(value: Any) -> tuple[dict[str, Any], str]:
    payload, seal = _sealed_payload(value, "merge_detail_work_plan")
    expected_hash = compute_v3_hash({key: copy.deepcopy(item) for key, item in payload.items() if key != "work_plan_hash"})
    if payload.get("work_plan_hash") != expected_hash:
        raise SelectedNavigationError("merge-detail work plan hash changed")
    return payload, seal


PromptAuthority = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]
RouteEstimator = Callable[[str, Mapping[str, Any]], Any]
OutputEstimator = Callable[[str, Mapping[str, Any]], Any]


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SelectedNavigationError("navigation values must be canonical JSON values") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _proposal_hash(proposal: Mapping[str, Any]) -> str:
    return compute_v3_hash({key: copy.deepcopy(value) for key, value in proposal.items() if key != "proposal_hash"})


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectedNavigationError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise SelectedNavigationError(f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise SelectedNavigationError(f"{label} contains duplicate values")
    return list(value)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SelectedNavigationError(f"{label} must be an integer >= {minimum}")
    return value


def _token_placeholder(tokens: int) -> str:
    # Space-separated ASCII words are a conservative one-token-per-word probe
    # for the route's output estimator.  The estimator, not character length,
    # decides whether the complete schema skeleton fits.
    return " ".join(["x"] * max(1, tokens))


def _context_for(context_reserve: Mapping[str, Any], kind: str) -> dict[str, Any]:
    raw = context_reserve.get(kind)
    if not isinstance(raw, Mapping):
        raise SelectedNavigationError(f"missing verified context reserve for {kind}")
    result = dict(raw)
    for field in ("verified_context_limit", "max_output_tokens", "reasoning_reserve", "safety_margin"):
        result[field] = _integer(result.get(field), f"{kind}.{field}")
    if result.get("effective_input_cap") is not None:
        result["effective_input_cap"] = _integer(
            result.get("effective_input_cap"), f"{kind}.effective_input_cap", minimum=1
        )
    result["route_identity"] = _nonempty(result.get("route_identity"), f"{kind}.route_identity")
    if result["max_output_tokens"] < 1 or result["verified_context_limit"] < 1:
        raise SelectedNavigationError(f"{kind} has no positive output or context limit")
    return result


def _prompted_request(
    kind: str,
    node_id: str,
    body: Mapping[str, Any],
    prompt_authority: PromptAuthority,
) -> dict[str, Any]:
    logical = copy.deepcopy(dict(body))
    authority = prompt_authority(kind, node_id, logical)
    if not isinstance(authority, Mapping) or not authority:
        raise SelectedNavigationError(f"prompt authority callback returned no authority for {kind}")
    logical["_prompt_authority"] = copy.deepcopy(dict(authority))
    return logical


def _estimate_request(
    kind: str,
    node_id: str,
    body: Mapping[str, Any],
    *,
    prompt_authority: PromptAuthority,
    route_estimator: RouteEstimator,
    context_reserve: Mapping[str, Any],
    input_cap: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _context_for(context_reserve, kind)
    complete = _prompted_request(kind, node_id, body, prompt_authority)
    raw = route_estimator(kind, complete)
    if isinstance(raw, Mapping):
        measured = dict(raw)
        estimate = measured.get("estimated_input_tokens")
    else:
        measured = {}
        estimate = raw
    estimate = _integer(estimate, f"{kind}.estimated_input_tokens", minimum=1)
    cap = min(32_000, input_cap, int(context.get("effective_input_cap", 32_000)))
    if estimate > cap:
        raise SelectedNavigationError(
            f"{kind} request {node_id} is over the effective input cap before POST "
            f"({estimate}>{cap} tokens)"
        )
    total = (
        estimate + context["max_output_tokens"] + context["reasoning_reserve"] + context["safety_margin"]
    )
    if total > context["verified_context_limit"]:
        raise SelectedNavigationError(
            f"{kind} request {node_id} exceeds verified context before POST "
            f"({total}>{context['verified_context_limit']} tokens)"
        )
    if measured.get("route_identity", context["route_identity"]) != context["route_identity"]:
        raise SelectedNavigationError(f"{kind} route identity changed during measurement")
    measured.update({
        "estimated_input_tokens": estimate,
        "effective_input_cap": cap,
        "max_output_tokens": context["max_output_tokens"],
        "verified_context_limit": context["verified_context_limit"],
        "reasoning_reserve": context["reasoning_reserve"],
        "safety_margin": context["safety_margin"],
        "route_identity": context["route_identity"],
        "context_total_tokens": total,
        "request_hash": _hash(complete),
    })
    return complete, measured


def _verify_measured_request(request_record: Mapping[str, Any], label: str) -> str:
    body = request_record.get("body")
    full_request = request_record.get("request")
    measurement = request_record.get("measurement", request_record.get("estimate"))
    if not isinstance(body, Mapping) or not isinstance(full_request, Mapping) or not isinstance(measurement, Mapping):
        raise SelectedNavigationError(f"{label} lacks the measured actual prompt-attached request")
    detached_body = copy.deepcopy(dict(full_request))
    authority = detached_body.pop("_prompt_authority", None)
    if not isinstance(authority, Mapping) or detached_body != dict(body):
        raise SelectedNavigationError(f"{label} actual request body differs from its source-bound shadow body")
    request_hash = _hash(full_request)
    if request_record.get("request_hash") != request_hash or measurement.get("request_hash") != request_hash:
        raise SelectedNavigationError(f"{label} actual prompt-attached request hash changed")
    return request_hash


def _estimate_output(
    kind: str,
    shape: Mapping[str, Any],
    *,
    output_estimator: OutputEstimator,
    context_reserve: Mapping[str, Any],
) -> int:
    context = _context_for(context_reserve, kind)
    raw = output_estimator(kind, copy.deepcopy(dict(shape)))
    if isinstance(raw, Mapping):
        raw = raw.get("estimated_output_tokens", raw.get("estimated_tokens"))
    estimate = _integer(raw, f"{kind}.estimated_output_tokens", minimum=1)
    if estimate > context["max_output_tokens"]:
        raise SelectedNavigationError(
            f"{kind} output skeleton exceeds requested-output reserve "
            f"({estimate}>{context['max_output_tokens']} tokens)"
        )
    return estimate


def _derive_text_bound(
    kind: str,
    skeleton_builder: Callable[[str], Mapping[str, Any]],
    *,
    output_estimator: OutputEstimator,
    context_reserve: Mapping[str, Any],
    upper: int | None,
) -> tuple[int, int]:
    context = _context_for(context_reserve, kind)
    high = context["max_output_tokens"]
    if upper is not None:
        high = min(high, _integer(upper, f"{kind} text upper bound", minimum=1))
    low = 1
    best = 0
    best_estimate = 0
    while low <= high:
        middle = (low + high) // 2
        try:
            estimate = _estimate_output(
                kind, skeleton_builder(_token_placeholder(middle)),
                output_estimator=output_estimator, context_reserve=context_reserve,
            )
        except SelectedNavigationError as exc:
            if "exceeds requested-output reserve" not in str(exc):
                raise
            estimate = context["max_output_tokens"] + 1
        if estimate <= context["max_output_tokens"]:
            best, best_estimate = middle, estimate
            low = middle + 1
        else:
            high = middle - 1
    if best < 1:
        raise SelectedNavigationError(
            f"{kind} has no positive text-token allowance under its exact output skeleton"
        )
    return best, best_estimate


def _section_scopes(candidate_id: str, sections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    scopes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(sections):
        if not isinstance(raw, Mapping):
            raise SelectedNavigationError(f"section {position} must be an object")
        section = copy.deepcopy(dict(raw))
        section_id = _nonempty(section.get("section_id"), f"section {position}.section_id")
        if section_id in seen:
            raise SelectedNavigationError(f"duplicate selected section ID {section_id}")
        seen.add(section_id)
        if "title" not in section or not isinstance(section.get("title"), str) or not section["title"].strip():
            raise SelectedNavigationError(f"section {section_id}.title must be present as non-empty exact text")
        if "goal" not in section or not isinstance(section.get("goal"), str) or not section["goal"].strip():
            raise SelectedNavigationError(f"section {section_id}.goal must be present as non-empty exact text")
        if "rationale" in section and not isinstance(section.get("rationale"), str):
            raise SelectedNavigationError(f"section {section_id}.rationale must be exact text")
        task_ids = section.get("task_ids", [])
        if not isinstance(task_ids, list) or any(not isinstance(item, str) or not item for item in task_ids):
            raise SelectedNavigationError(f"section {section_id}.task_ids must be an exact string array")
        if len(task_ids) != len(set(task_ids)):
            raise SelectedNavigationError(f"section {section_id}.task_ids contains duplicates")
        support_rows = section.get("claim_support", [])
        if support_rows is not None and not isinstance(support_rows, list):
            raise SelectedNavigationError(f"section {section_id}.claim_support must be an array")
        observed_tasks: set[str] = set()
        for support_index, row in enumerate(support_rows or []):
            if not isinstance(row, Mapping):
                raise SelectedNavigationError(f"section {section_id}.claim_support[{support_index}] must be an object")
            task_id = row.get("task_id")
            if task_id is not None:
                observed_tasks.add(_nonempty(task_id, f"section {section_id}.claim_support[{support_index}].task_id"))
        if task_ids and observed_tasks and set(task_ids) != observed_tasks:
            raise SelectedNavigationError(f"section {section_id}.task_ids differ from exact support task memberships")
        if not task_ids and observed_tasks:
            task_ids = sorted(observed_tasks)
        aliases = section.get("section_hash_aliases", section.get("hash_aliases", []))
        if not isinstance(aliases, list) or any(not isinstance(item, str) or not item for item in aliases):
            raise SelectedNavigationError(f"section {section_id}.section_hash_aliases must be a string array")
        if len(aliases) != len(set(aliases)):
            raise SelectedNavigationError(f"section {section_id}.section_hash_aliases contains duplicates")
        scopes.append({
            "section_id": section_id,
            "section_hash": compute_v3_hash(section),
            "position": position,
            "task_ids": list(task_ids),
            "hash_aliases": list(aliases),
        })
    if not scopes:
        raise SelectedNavigationError("selected candidate must contain at least one section")
    return scopes


def _plan_integrity_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in plan.items() if key != "plan_integrity_hash"}


def _seal_plan(plan: dict[str, Any]) -> None:
    plan.pop("plan_integrity_hash", None)
    plan["plan_integrity_hash"] = compute_v3_hash(_plan_integrity_payload(plan))


def _validate_plan_integrity(plan: Mapping[str, Any]) -> None:
    if not isinstance(plan, Mapping):
        raise SelectedNavigationError("selected-navigation plan is malformed")
    stored_plan_hash = plan.get("plan_integrity_hash")
    if not isinstance(stored_plan_hash, str) or stored_plan_hash != compute_v3_hash(_plan_integrity_payload(plan)):
        raise SelectedNavigationError("selected-navigation plan or budget binding changed")
    candidate_id = _nonempty(plan.get("candidate_id"), "plan.candidate_id")
    candidate_hash = _nonempty(plan.get("candidate_hash"), "plan.candidate_hash")
    raw_sources = plan.get("source_sections")
    if not isinstance(raw_sources, Mapping) or not raw_sources:
        raise SelectedNavigationError("selected-navigation plan lost its source sections")
    stored_scopes = plan.get("section_scope")
    if not isinstance(stored_scopes, list):
        raise SelectedNavigationError("selected-navigation plan lost its section scope")
    ordered_sources = [
        raw_sources[str(scope.get("section_id") or "")]
        for scope in sorted(stored_scopes, key=lambda row: row.get("position", -1))
        if isinstance(scope, Mapping) and str(scope.get("section_id") or "") in raw_sources
    ]
    recomputed_scopes = _section_scopes(candidate_id, ordered_sources)
    if recomputed_scopes != stored_scopes:
        raise SelectedNavigationError("selected-navigation source section hash or scope changed")
    expected_section_hashes = {row["section_id"]: row["section_hash"] for row in recomputed_scopes}
    if plan.get("source_section_hashes") != expected_section_hashes:
        raise SelectedNavigationError("selected-navigation source section hash map changed")
    intent = plan.get("source_intent_fields")
    if not isinstance(intent, Mapping) or compute_v3_hash(dict(intent)) != plan.get("intent_fields_hash"):
        raise SelectedNavigationError("selected-navigation original intent fields changed")
    if plan.get("intent_field_names") != sorted(intent) or plan.get("intent_field_hashes") != {
        key: compute_v3_hash(value) for key, value in intent.items()
    }:
        raise SelectedNavigationError("selected-navigation intent field binding changed")
    expected_empty = sorted(key for key, value in intent.items() if _canonical_field_text(value) == "")
    if plan.get("empty_intent_fields") != expected_empty:
        raise SelectedNavigationError("selected-navigation explicit empty intent fields changed")
    if plan.get("requires_coordination"):
        manifest = plan.get("intent_span_manifest")
        extraction = plan.get("layout_extraction_requests")
        if not isinstance(manifest, list) or not isinstance(extraction, list):
            raise SelectedNavigationError("selected-navigation span plan is incomplete")
        if compute_v3_hash(manifest) != plan.get("span_manifest_hash"):
            raise SelectedNavigationError("selected-navigation span manifest hash changed")
        if len(manifest) != len(extraction) or [row.get("span_id") for row in manifest] != [row.get("span_id") for row in extraction]:
            raise SelectedNavigationError("selected-navigation extraction assignments changed")
        navigation_sections = []
        for scope in recomputed_scopes:
            section = copy.deepcopy(dict(raw_sources[scope["section_id"]]))
            section["task_ids"] = list(scope["task_ids"])
            navigation_sections.append(section)
        source_fields = _source_fields(navigation_sections, intent)
        field_map = {(row["scope_id"], row["source_field"]): row for row in source_fields}
        manifest_groups: dict[tuple[str, str], list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
        for meta, request in zip(manifest, extraction, strict=True):
            body = request.get("body")
            span = body.get("span") if isinstance(body, Mapping) else None
            if not isinstance(span, Mapping) or span.get("span_id") != meta.get("span_id"):
                raise SelectedNavigationError("selected-navigation source span request changed")
            _verify_measured_request(request, "layout extraction request")
            key = (str(meta.get("scope_id")), str(meta.get("source_field")))
            source = field_map.get(key)
            if source is None or span.get("source_hash") != source["source_hash"] or meta.get("source_hash") != source["source_hash"]:
                raise SelectedNavigationError("selected-navigation source field hash changed")
            text = source["text"]
            start, end = meta.get("utf8_start"), meta.get("utf8_end")
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                raise SelectedNavigationError("selected-navigation UTF-8 span bounds are invalid")
            encoded = text.encode("utf-8")
            piece_bytes = encoded[start:end]
            try:
                piece = piece_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SelectedNavigationError("selected-navigation span splits a UTF-8 codepoint") from exc
            if span.get("text") != piece or _hash(piece) != meta.get("text_hash") or span.get("text_hash") != meta.get("text_hash"):
                raise SelectedNavigationError("selected-navigation span text changed")
            manifest_groups.setdefault(key, []).append((meta, span))
        nonempty_fields = {key: row for key, row in field_map.items() if row["text"]}
        if set(manifest_groups) != set(nonempty_fields):
            raise SelectedNavigationError("selected-navigation span manifest omitted or added source fields")
        for key, field in nonempty_fields.items():
            parts = sorted(manifest_groups[key], key=lambda pair: pair[0]["utf8_start"])
            cursor = 0
            for meta, _span in parts:
                if meta["utf8_start"] != cursor:
                    raise SelectedNavigationError(f"selected-navigation span coverage has a gap for {key[1]}")
                cursor = meta["utf8_end"]
            if cursor != len(field["text"].encode("utf-8")):
                raise SelectedNavigationError(f"selected-navigation span coverage is incomplete for {key[1]}")
        derived_fnav = len(manifest)
        derived_jnav = len(extraction)
        tree = plan.get("layout_reduction_tree")
        if not isinstance(tree, Mapping) or tree != _make_reduction_tree(candidate_hash, [
            {"request_id": request["request_id"], "span_id": request["span_id"]}
            for request in extraction
        ]):
            raise SelectedNavigationError("selected-navigation reduction tree changed")
        derived_anav = sum(len(level["reducers"]) for level in tree["levels"])
        derived_counts = {"Fnav": derived_fnav, "Jnav": derived_jnav, "Anav": derived_anav,
                          "Qglobal": 1, "Qmerge_detail": 0}
        if plan.get("counts") != derived_counts:
            raise SelectedNavigationError("selected-navigation call counts differ from the immutable work plan")
        expected_semantic_hash = _hash({
            "schema_version": PLAN_SCHEMA, "candidate_id": candidate_id,
            "candidate_hash": candidate_hash, "section_scope": recomputed_scopes,
            "intent_fields_hash": plan["intent_fields_hash"],
            "span_manifest_hash": plan["span_manifest_hash"],
            "layout_reduction_tree": tree, "requires_coordination": True,
        })
        if plan.get("semantic_hash") != expected_semantic_hash:
            raise SelectedNavigationError("selected-navigation semantic identity changed")
    elif plan.get("counts") != {"Fnav": 0, "Jnav": 0, "Anav": 0, "Qglobal": 0, "Qmerge_detail": 0}:
        raise SelectedNavigationError("single-section navigation call counts must be zero")
    else:
        expected_semantic_hash = _hash({
            "schema_version": PLAN_SCHEMA, "candidate_id": candidate_id,
            "candidate_hash": candidate_hash, "section_scope": recomputed_scopes,
            "intent_fields_hash": plan["intent_fields_hash"], "requires_coordination": False,
        })
        if plan.get("semantic_hash") != expected_semantic_hash:
            raise SelectedNavigationError("single-section navigation semantic identity changed")


def _canonical_field_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _json_bytes(value).decode("utf-8")


def _source_fields(
    sections: Sequence[Mapping[str, Any]], intent_fields: Mapping[str, Any]
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for key in sorted(intent_fields):
        if not isinstance(key, str) or not key:
            raise SelectedNavigationError("intent field names must be non-empty strings")
        value = copy.deepcopy(intent_fields[key])
        fields.append({
            "scope_id": "intent",
            "section_id": None,
            "source_field": f"intent.{key}",
            "source_value": value,
            "source_hash": compute_v3_hash(value),
            "text": _canonical_field_text(value),
        })
    for position, raw in enumerate(sections):
        section = dict(raw)
        section_id = str(section["section_id"])
        for key in _ORG_SECTION_FIELDS:
            if key not in section:
                # Missing optional rationale/task_ids are represented explicitly
                # in the section hash/scope; there is no source text to extract.
                if key in {"rationale", "task_ids"}:
                    continue
                raise SelectedNavigationError(f"section {section_id} is missing {key}")
            value = copy.deepcopy(section[key])
            fields.append({
                "scope_id": section_id,
                "section_id": section_id,
                "section_position": position,
                "source_field": f"section.{key}",
                "source_value": value,
                "source_hash": compute_v3_hash(value),
                "text": _canonical_field_text(value),
            })
    return fields


def _span_id(candidate_hash: str, field: Mapping[str, Any], byte_start: int, byte_end: int, text: str) -> str:
    identity = {
        "schema": "selected_candidate_layout_span/v1",
        "candidate_hash": candidate_hash,
        "scope_id": field["scope_id"],
        "section_id": field.get("section_id"),
        "source_field": field["source_field"],
        "source_hash": field["source_hash"],
        "utf8_start": byte_start,
        "utf8_end": byte_end,
        "text_hash": _hash(text),
    }
    return "layout-span:" + _hash(identity)[:24]


def _extract_request_body(
    candidate_id: str,
    candidate_hash: str,
    span: Mapping[str, Any],
    tfrag: int,
) -> dict[str, Any]:
    return {
        "schema_version": EXTRACT_REQUEST_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "span": {key: copy.deepcopy(span[key]) for key in (
            "span_id", "scope_id", "section_id", "section_position", "source_field",
            "source_hash", "utf8_start", "utf8_end", "text_hash", "text",
        ) if key in span},
        "max_expression_tokens": tfrag,
        "response_schema": EXTRACT_RESPONSE_SCHEMA,
    }


def _request_id(kind: str, identity: Any) -> str:
    return f"{kind}:{_hash(identity)[:24]}"


def _split_field_to_spans(
    candidate_id: str,
    candidate_hash: str,
    field: Mapping[str, Any],
    *,
    tfrag: int,
    prompt_authority: PromptAuthority,
    route_estimator: RouteEstimator,
    context_reserve: Mapping[str, Any],
    input_cap: int,
) -> list[dict[str, Any]]:
    text = str(field["text"])
    if not text:
        return []
    spans: list[dict[str, Any]] = []
    char_start = 0
    byte_start = 0
    while char_start < len(text):
        low, high = 1, len(text) - char_start
        best: tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
        while low <= high:
            count = (low + high) // 2
            text_part = text[char_start:char_start + count]
            byte_end = byte_start + len(text_part.encode("utf-8"))
            span = {
                "scope_id": field["scope_id"],
                "section_id": field.get("section_id"),
                "section_position": field.get("section_position"),
                "source_field": field["source_field"],
                "source_hash": field["source_hash"],
                "utf8_start": byte_start,
                "utf8_end": byte_end,
                "text": text_part,
                "text_hash": _hash(text_part),
            }
            span["span_id"] = _span_id(candidate_hash, field, byte_start, byte_end, text_part)
            body = _extract_request_body(candidate_id, candidate_hash, span, tfrag)
            node_id = _request_id(EXTRACT_KIND, {
                "candidate_hash": candidate_hash, "span_id": span["span_id"],
            })
            try:
                complete, measured = _estimate_request(
                    EXTRACT_KIND, node_id, body, prompt_authority=prompt_authority,
                    route_estimator=route_estimator, context_reserve=context_reserve,
                    input_cap=input_cap,
                )
            except SelectedNavigationError as exc:
                if "over the effective input cap" not in str(exc) and "exceeds verified context" not in str(exc):
                    raise
                high = count - 1
            else:
                best = (count, span, complete, measured)
                low = count + 1
        if best is None:
            first = text[char_start:char_start + 1]
            span_id = _span_id(candidate_hash, field, byte_start, byte_start + len(first.encode("utf-8")), first)
            raise SelectedNavigationError(
                f"indivisible selected layout span {span_id} ({field['source_field']}) exceeds the exact "
                "route input/context bound before POST"
            )
        count, span, complete, measured = best
        span["request_id"] = _request_id(EXTRACT_KIND, {
            "candidate_hash": candidate_hash, "span_id": span["span_id"],
        })
        span["request"] = _extract_request_body(candidate_id, candidate_hash, span, tfrag)
        span["prompt_request"] = copy.deepcopy(complete)
        span["request_hash"] = _hash(complete)
        span["measurement"] = measured
        spans.append(span)
        piece = text[char_start:char_start + count]
        byte_start += len(piece.encode("utf-8"))
        char_start += count
    # Exact byte-span conservation catches Unicode boundary and split errors.
    reconstructed = "".join(span["text"] for span in spans)
    if reconstructed != text or sum(span["utf8_end"] - span["utf8_start"] for span in spans) != len(text.encode("utf-8")):
        raise SelectedNavigationError(f"selected layout spans do not exactly cover {field['source_field']}")
    return spans


def derive_merged_section_id(candidate_hash: str, source_ids: Sequence[str], source_hashes: Mapping[str, str]) -> str:
    return "merged-section:" + _hash({
        "schema": "selected_candidate_merged_section_id/v1",
        "candidate_hash": candidate_hash,
        "source_section_ids": list(source_ids),
        "source_section_hashes": {section_id: source_hashes[section_id] for section_id in source_ids},
    })[:24]


def _layout_extract_response_shape(
    candidate_id: str, candidate_hash: str, span: Mapping[str, Any], expression_tokens: int,
) -> dict[str, Any]:
    return {
        "schema_version": EXTRACT_RESPONSE_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "span_id": span["span_id"],
        "source_hash": span["source_hash"],
        "utf8_start": span["utf8_start"],
        "utf8_end": span["utf8_end"],
        "text_hash": span["text_hash"],
        "expression": _token_placeholder(expression_tokens),
    }


def _response_shapes(candidate_id: str, candidate_hash: str, scopes: Sequence[Mapping[str, Any]], intent_fields: Mapping[str, Any]) -> dict[str, Any]:
    span = {
        "span_id": "layout-span:" + "0" * 24,
        "source_hash": "0" * 64,
        "utf8_start": 0,
        "utf8_end": 1,
        "text_hash": "0" * 64,
    }
    navigation_body = {
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "root_node_id": "arbitration_layout_extract:" + "0" * 24,
        "root_hash": "0" * 64,
        "plan_hash": "0" * 64,
        "span_manifest_hash": "0" * 64,
        "section_scope": [
            {"section_id": scope["section_id"], "section_hash": scope["section_hash"]}
            for scope in scopes
        ],
    }
    return {
        "navigation": {
            **_global_navigation_response_shape({}, navigation_body, 1),
        },
        "extract": _layout_extract_response_shape(candidate_id, candidate_hash, span, 1),
    }


def compile_selected_candidate_navigation_plan(
    candidate_id: str,
    candidate_hash: str,
    sections: Sequence[Mapping[str, Any]],
    intent_fields: Mapping[str, Any],
    *,
    prompt_authority: PromptAuthority,
    route_estimator: RouteEstimator,
    output_estimator: OutputEstimator,
    context_reserve: Mapping[str, Any],
    input_cap: int,
    fnav_max: int,
    tnav_max: int | None = None,
    tfrag_max: int | None = None,
    tproposal_max: int | None = None,
    emax: int,
) -> dict[str, Any]:
    """Compile selected-only navigation requests with complete source span coverage.

    ``intent_fields`` must be the complete exact original ReviewIntent material
    that can affect section organization: purpose/title, goal, rationale, task
    scope/memberships, and applicable merge constraints.  Pass the complete
    source object (or an exact field map), not summaries or hash references.

    ``prompt_authority`` is called as ``(kind, stable_node_id, logical_body)``;
    ``route_estimator`` receives the prompt-attached logical request and returns
    ``estimated_input_tokens`` (or that integer).  ``output_estimator`` measures
    the separate response schema/body.  Context profiles are explicit and
    contain route identity, verified context, requested-output maximum,
    reasoning reserve, safety margin, and the route's effective input cap.
    Optional text maxima only clamp bounds derived from those exact profiles.
    """
    candidate_id = _nonempty(candidate_id, "candidate_id")
    candidate_hash = _nonempty(candidate_hash, "candidate_hash")
    if len(candidate_hash) != 64 or any(char not in "0123456789abcdef" for char in candidate_hash.lower()):
        raise SelectedNavigationError("candidate_hash must be a SHA-256 hex digest")
    if not isinstance(intent_fields, Mapping):
        raise SelectedNavigationError("intent_fields must be a source-bound object")
    input_cap = _integer(input_cap, "input_cap", minimum=1)
    fnav_max = _integer(fnav_max, "fnav_max")
    emax = _integer(emax, "emax")
    scopes = _section_scopes(candidate_id, sections)
    section_by_id = {str(item["section_id"]): copy.deepcopy(dict(sections[item["position"]])) for item in scopes}
    frozen_intent = copy.deepcopy(dict(intent_fields))
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "section_scope": scopes,
        "intent_fields_hash": compute_v3_hash(frozen_intent),
        "intent_field_names": sorted(frozen_intent),
        "intent_field_hashes": {key: compute_v3_hash(value) for key, value in frozen_intent.items()},
        "empty_intent_fields": sorted(
            key for key, value in frozen_intent.items() if _canonical_field_text(value) == ""
        ),
        "source_sections": section_by_id,
        "source_intent_fields": frozen_intent,
        "source_section_hashes": {row["section_id"]: row["section_hash"] for row in scopes},
        "emax": emax,
        "fnav_max": fnav_max,
        "input_cap": min(input_cap, 32_000),
        "requires_coordination": len(scopes) > 1,
        "intent_span_manifest": [],
        "layout_extraction_requests": [],
        "layout_reduction_tree": {"levels": [], "root_node_id": None},
        "counts": {"Fnav": 0, "Jnav": 0, "Anav": 0, "Qglobal": 0, "Qmerge_detail": 0},
        "bounds": {"Tnav": 0, "Tfrag": 0, "Tproposal": 0},
        "measurements": {},
    }
    if not plan["requires_coordination"]:
        plan["semantic_hash"] = _hash({
            "schema_version": PLAN_SCHEMA, "candidate_id": candidate_id,
            "candidate_hash": candidate_hash, "section_scope": scopes,
            "intent_fields_hash": plan["intent_fields_hash"],
            "requires_coordination": False,
        })
        _seal_plan(plan)
        _validate_plan_integrity(plan)
        return plan
    if not frozen_intent:
        raise SelectedNavigationError(
            "multi-section navigation requires complete exact original ReviewIntent fields"
        )

    contexts = {kind: _context_for(context_reserve, kind) for kind in (EXTRACT_KIND, REDUCE_KIND, NAV_KIND, DETAIL_KIND)}
    shapes = _response_shapes(candidate_id, candidate_hash, scopes, frozen_intent)
    tfrag, extract_skeleton_tokens = _derive_text_bound(
        EXTRACT_KIND,
        lambda text: {**shapes["extract"], "expression": text},
        output_estimator=output_estimator, context_reserve=context_reserve, upper=tfrag_max,
    )
    tproposal, proposal_skeleton_tokens = _derive_text_bound(
        NAV_KIND,
        lambda text: {
            **shapes["navigation"],
            "merge_groups": [
                {**group, "title": text, "goal": text, "integration_reason": text}
                for group in shapes["navigation"]["merge_groups"]
            ],
        },
        output_estimator=output_estimator, context_reserve=context_reserve, upper=tproposal_max,
    )

    navigation_sections: list[dict[str, Any]] = []
    for scope in scopes:
        exact_section = copy.deepcopy(dict(sections[scope["position"]]))
        exact_section["task_ids"] = list(scope["task_ids"])
        navigation_sections.append(exact_section)
    source_fields = _source_fields(navigation_sections, frozen_intent)
    spans: list[dict[str, Any]] = []
    for field in source_fields:
        spans.extend(_split_field_to_spans(
            candidate_id, candidate_hash, field, tfrag=tfrag,
            prompt_authority=prompt_authority, route_estimator=route_estimator,
            context_reserve=context_reserve, input_cap=input_cap,
        ))
    spans.sort(key=lambda row: (
        -1 if row.get("section_position") is None else row["section_position"],
        str(row["source_field"]), row["utf8_start"], row["utf8_end"], row["span_id"],
    ))
    if len(spans) > fnav_max:
        raise SelectedNavigationError(
            f"selected navigation source spans Fnav={len(spans)} exceed run-start Fnavmax={fnav_max}"
        )
    if not spans:
        raise SelectedNavigationError("required selected navigation has no source-bound organizational spans")
    span_manifest = [{key: copy.deepcopy(span[key]) for key in (
        "span_id", "scope_id", "section_id", "section_position", "source_field", "source_hash",
        "utf8_start", "utf8_end", "text_hash",
    )} for span in spans]
    plan["intent_span_manifest"] = span_manifest
    span_manifest_hash = compute_v3_hash(span_manifest)
    tree = _make_reduction_tree(candidate_hash, spans)
    semantic_hash = _hash({
        "schema_version": PLAN_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "section_scope": scopes,
        "intent_fields_hash": plan["intent_fields_hash"],
        "span_manifest_hash": span_manifest_hash,
        "layout_reduction_tree": tree,
        "requires_coordination": True,
    })
    spans_by_section: dict[str, list[str]] = {str(row["section_id"]): [] for row in scopes}
    spans_by_intent: dict[str, list[str]] = {key: [] for key in frozen_intent}
    for row in span_manifest:
        if row["section_id"] is None:
            spans_by_intent[str(row["source_field"]).removeprefix("intent.")].append(row["span_id"])
        else:
            spans_by_section[str(row["section_id"])].append(row["span_id"])
    layout_node_ids = [str(span["request_id"]) for span in spans]
    layout_node_ids.extend(
        str(reducer["node_id"])
        for level in tree["levels"] for reducer in level["reducers"]
    )
    longest_layout_nodes = sorted(layout_node_ids, key=lambda value: (-len(_json_bytes(value)), value))[:2]
    if len(longest_layout_nodes) < 2:
        longest_layout_nodes = ["layout-child-a", "layout-child-b"]
    reduce_body = {
        "schema_version": REDUCE_REQUEST_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "node_id": tree["root_node_id"] or "layout-root",
        "child_node_ids": longest_layout_nodes,
        "child_hashes": ["0" * 64, "1" * 64],
        "expected_span_ids": [row["span_id"] for row in span_manifest],
        "expression_token_bound": 1,
        "response_schema": REDUCE_RESPONSE_SCHEMA,
    }
    nav_body_builder = lambda text: {
        "schema_version": NAV_REQUEST_SCHEMA,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "plan_hash": semantic_hash,
        "root_node_id": tree["root_node_id"] or ("layout-extract:" + "0" * 24),
        "root_hash": "0" * 64,
        "span_manifest_hash": span_manifest_hash,
        "section_scope": [
            {**scope, "span_ids": spans_by_section[scope["section_id"]], "layout_expression": text}
            for scope in scopes
        ],
        "intent_layout": [
            {"source_field": key, "source_hash": compute_v3_hash(frozen_intent[key]),
             "span_ids": spans_by_intent[key],
             "expression": text if spans_by_intent[key] else "(empty source field)"}
            for key in sorted(frozen_intent)
        ],
        "merge_proposal_text_budget_tokens": tproposal,
        "response_schema": NAV_RESPONSE_SCHEMA,
        "contract": {
            "all_selected_section_ids_once": True,
            "section_order_lists_each_original_section_once": True,
            "global_order_across_all_tasks_and_shards": True,
            "merge_groups_may_cross_any_navigation_boundary": True,
            "source_facts_are_not_authorized_by_layout_expressions": True,
            "merge_requires_targeted_detail_review": True,
            "merged_section_ids_are_compiled_deterministically_from_candidate_and_source_hashes": True,
        },
    }
    high = min(contexts[REDUCE_KIND]["max_output_tokens"], contexts[NAV_KIND]["max_output_tokens"])
    if tnav_max is not None:
        high = min(high, _integer(tnav_max, "tnav_max", minimum=1))
    low, tnav, reduce_skeleton_tokens, nav_input_measure = 1, 0, 0, {}
    while low <= high:
        middle = (low + high) // 2
        reduction_tokens = 0
        nav_measure: dict[str, Any] = {}
        reduce_shape_for_bound = _layout_reduction_response_shape(
            plan, {**reduce_body, "expression_token_bound": middle}, middle,
        )
        try:
            reduction_tokens = _estimate_output(
                REDUCE_KIND, reduce_shape_for_bound, output_estimator=output_estimator,
                context_reserve=context_reserve,
            )
            nav_node = _request_id(NAV_KIND, {"candidate_hash": candidate_hash, "purpose": "root-bound-probe"})
            _complete, nav_measure = _estimate_request(
                NAV_KIND, nav_node, nav_body_builder(_token_placeholder(middle)),
                prompt_authority=prompt_authority, route_estimator=route_estimator,
                context_reserve=context_reserve, input_cap=input_cap,
            )
        except SelectedNavigationError as exc:
            if "exceeds requested-output reserve" not in str(exc) and "over the effective input cap" not in str(exc) and "exceeds verified context" not in str(exc):
                raise
            low_failure = True
        else:
            low_failure = False
        if not low_failure:
            tnav = middle
            reduce_skeleton_tokens = reduction_tokens
            nav_input_measure = nav_measure
            low = middle + 1
        else:
            high = middle - 1
    if tnav < 1:
        raise SelectedNavigationError(
            "selected navigation has no positive expression bound under the complete route/output/context skeleton"
        )
    plan["bounds"] = {"Tnav": tnav, "Tfrag": tfrag, "Tproposal": tproposal}
    plan["measurements"] = {
        "extract_output_skeleton_tokens": extract_skeleton_tokens,
        "reduce_output_skeleton_tokens": reduce_skeleton_tokens,
        "navigation_output_skeleton_tokens": proposal_skeleton_tokens,
        "navigation_max_input": nav_input_measure,
        "route_identity": {kind: contexts[kind]["route_identity"] for kind in contexts},
    }

    plan["span_manifest_hash"] = span_manifest_hash
    plan["semantic_hash"] = semantic_hash
    plan["layout_extraction_requests"] = [
        {"request_id": span["request_id"], "span_id": span["span_id"],
         "body": copy.deepcopy(span["request"]), "request": copy.deepcopy(span["prompt_request"]),
         "request_hash": span["request_hash"], "measurement": copy.deepcopy(span["measurement"]),
         "estimate": copy.deepcopy(span["measurement"])}
        for span in spans
    ]
    for request in plan["layout_extraction_requests"]:
        reserve = _measure_navigation_output_reserve(
            plan, EXTRACT_KIND, request,
            output_estimator=output_estimator, context_reserve=context_reserve,
        )
        request["measurement"]["output_reserve"] = reserve
        request["estimate"]["output_reserve"] = copy.deepcopy(reserve)
    reduce_template = _layout_reduction_response_shape(
        plan, {**reduce_body, "expression_token_bound": tnav}, tnav,
    )
    navigation_template = {
        **shapes["navigation"],
        "merge_groups": [
            {**group, "title": _token_placeholder(tproposal),
             "goal": _token_placeholder(tproposal),
             "integration_reason": _token_placeholder(tproposal)}
            for group in shapes["navigation"]["merge_groups"]
        ],
    }
    plan["measurements"]["response_output_reserve_templates"] = {
        "extract": _output_reserve_for_shape(
            EXTRACT_KIND, EXTRACT_RESPONSE_SCHEMA,
            {**shapes["extract"], "expression": _token_placeholder(tfrag)},
            {"expression": {"tokens": tfrag, "plan_bound": "Tfrag"}},
            output_estimator=output_estimator, context_reserve=context_reserve,
        ),
        "reduce": _output_reserve_for_shape(
            REDUCE_KIND, REDUCE_RESPONSE_SCHEMA, reduce_template,
            {"layout_by_section[].expression": {"tokens": tnav, "plan_bound": "Tnav"},
             "intent_layout[].expression": {"tokens": tnav, "plan_bound": "Tnav"}},
            output_estimator=output_estimator, context_reserve=context_reserve,
        ),
        "navigation": _output_reserve_for_shape(
            NAV_KIND, NAV_RESPONSE_SCHEMA, navigation_template,
            {"merge_groups[].title": {"tokens": tproposal, "plan_bound": "Tproposal"},
             "merge_groups[].goal": {"tokens": tproposal, "plan_bound": "Tproposal"},
             "merge_groups[].integration_reason": {"tokens": tproposal, "plan_bound": "Tproposal"}},
            output_estimator=output_estimator, context_reserve=context_reserve,
        ),
    }
    plan["counts"]["Fnav"] = len(spans)
    plan["counts"]["Jnav"] = len(spans)
    if plan["counts"]["Jnav"] > plan["counts"]["Fnav"]:
        raise SelectedNavigationError("Jnav exceeds exact Fnav source-span manifest")
    plan["layout_reduction_tree"] = tree
    plan["counts"]["Anav"] = sum(len(level["reducers"]) for level in tree["levels"])
    if plan["counts"]["Anav"] > max(0, plan["counts"]["Fnav"] - 1):
        raise SelectedNavigationError("Anav exceeds the finite whole-level reduction bound")
    plan["counts"]["Qglobal"] = 1
    _seal_plan(plan)
    _validate_plan_integrity(plan)
    return plan


def _make_reduction_tree(candidate_hash: str, spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    active = [{
        "node_id": str(span["request_id"]),
        "node_kind": "extract",
        "span_ids": [str(span["span_id"])],
        "minimum_span_order": index,
    } for index, span in enumerate(spans)]
    levels: list[dict[str, Any]] = []
    depth = 0
    while len(active) > 1:
        reducers: list[dict[str, Any]] = []
        next_active: list[dict[str, Any]] = []
        index = 0
        while index + 1 < len(active):
            left, right = active[index], active[index + 1]
            left_spans, right_spans = list(left["span_ids"]), list(right["span_ids"])
            if set(left_spans) & set(right_spans):
                raise SelectedNavigationError("layout reduction children overlap source spans")
            union = left_spans + right_spans
            identity = {
                "schema": REDUCE_REQUEST_SCHEMA,
                "candidate_hash": candidate_hash,
                "level": depth,
                "child_ids": [left["node_id"], right["node_id"]],
                "span_ids": union,
            }
            node_id = "layout-reduce:" + _hash(identity)[:24]
            reducer = {
                "node_id": node_id,
                "child_node_ids": [left["node_id"], right["node_id"]],
                "child_span_ids": [left_spans, right_spans],
                "span_ids": union,
                "minimum_span_order": min(left["minimum_span_order"], right["minimum_span_order"]),
                "level": depth,
            }
            reducers.append(reducer)
            next_active.append({
                "node_id": node_id, "node_kind": "reduce", "span_ids": union,
                "minimum_span_order": reducer["minimum_span_order"],
            })
            index += 2
        carry = None
        if index < len(active):
            carry = copy.deepcopy(active[index])
            next_active.append(carry)
        levels.append({
            "level": depth,
            "input_node_ids": [node["node_id"] for node in active],
            "reducers": reducers,
            "odd_carry_node_id": carry["node_id"] if carry else None,
            "output_node_ids": [node["node_id"] for node in next_active],
        })
        active = next_active
        depth += 1
    return {
        "schema_version": "selected_candidate_layout_reduction_tree/v1",
        "levels": levels,
        "root_node_id": active[0]["node_id"] if active else None,
    }


def validate_layout_fragment(plan: Mapping[str, Any], request: Mapping[str, Any], response: Mapping[str, Any], *, output_estimator: OutputEstimator, context_reserve: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one extraction result against its exact assigned source span."""
    _validate_plan_integrity(plan)
    if not isinstance(response, Mapping) or response.get("schema_version") != EXTRACT_RESPONSE_SCHEMA:
        raise SelectedNavigationError("layout extraction response has an unknown schema")
    if set(response) != {"schema_version", "candidate_id", "candidate_hash", "span_id", "source_hash",
                         "utf8_start", "utf8_end", "text_hash", "expression"}:
        raise SelectedNavigationError("layout extraction response has unknown or missing fields")
    body = request.get("body") if isinstance(request.get("body"), Mapping) else request
    span = body.get("span") if isinstance(body, Mapping) else None
    if not isinstance(span, Mapping):
        raise SelectedNavigationError("layout extraction request has no exact span")
    expected_request_id = next((
        str(row["request_id"]) for row in plan.get("layout_extraction_requests", [])
        if row.get("span_id") == span.get("span_id")
    ), None)
    if expected_request_id is None or request.get("request_id", expected_request_id) != expected_request_id:
        raise SelectedNavigationError("layout extraction request ID is foreign to its exact source span")
    reserve = _verify_output_reserve(
        plan, EXTRACT_KIND, request,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    for key, expected in (("candidate_id", plan.get("candidate_id")), ("candidate_hash", plan.get("candidate_hash")),
                          ("span_id", span.get("span_id")), ("source_hash", span.get("source_hash")),
                          ("utf8_start", span.get("utf8_start")), ("utf8_end", span.get("utf8_end")),
                          ("text_hash", span.get("text_hash"))):
        if response.get(key) != expected:
            raise SelectedNavigationError(f"layout extraction response has foreign or changed {key}")
    expression = _nonempty(response.get("expression"), "layout fragment expression")
    if output_estimator(EXTRACT_KIND, {"expression": expression}) > int(plan["bounds"]["Tfrag"]):
        raise SelectedNavigationError("layout fragment expression exceeds derived Tfrag")
    _verify_response_within_reserve(
        EXTRACT_KIND, response, reserve,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    fragment = {
        "schema_version": EXTRACT_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "span_id": span["span_id"],
        "scope_id": span.get("scope_id"),
        "section_id": span.get("section_id"),
        "section_position": span.get("section_position"),
        "source_field": span["source_field"],
        "source_hash": span["source_hash"],
        "utf8_start": span["utf8_start"],
        "utf8_end": span["utf8_end"],
        "text_hash": span["text_hash"],
        "expression": expression,
    }
    if _estimate_output(EXTRACT_KIND, fragment, output_estimator=output_estimator, context_reserve=context_reserve) > _context_for(context_reserve, EXTRACT_KIND)["max_output_tokens"]:
        raise SelectedNavigationError("normalized layout fragment exceeds its output reserve")
    return fragment


def validate_all_layout_fragments(plan: Mapping[str, Any], fragments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    _validate_plan_integrity(plan)
    expected = [row["span_id"] for row in plan.get("intent_span_manifest") or []]
    observed = [str(row.get("span_id") or "") for row in fragments if isinstance(row, Mapping)]
    if len(observed) != len(fragments) or len(observed) != len(set(observed)):
        raise SelectedNavigationError("layout extraction outputs contain duplicate or malformed spans")
    if set(observed) != set(expected):
        missing, extra = sorted(set(expected) - set(observed)), sorted(set(observed) - set(expected))
        raise SelectedNavigationError(f"layout extraction coverage differs; missing={missing}, foreign={extra}")
    metadata = {str(row["span_id"]): row for row in plan["intent_span_manifest"]}
    by_id = {str(row["span_id"]): copy.deepcopy(dict(row)) for row in fragments}
    for span_id, row in by_id.items():
        source = metadata[span_id]
        for key in ("candidate_id", "candidate_hash", "scope_id", "section_id", "section_position",
                    "source_field", "source_hash", "utf8_start", "utf8_end", "text_hash"):
            expected_value = plan["candidate_id"] if key == "candidate_id" else (
                plan["candidate_hash"] if key == "candidate_hash" else source.get(key)
            )
            if row.get(key) != expected_value:
                raise SelectedNavigationError(f"layout fragment {span_id} changed or omitted {key}")
        _nonempty(row.get("expression"), f"layout fragment {span_id}.expression")
    order = {str(row["span_id"]): index for index, row in enumerate(plan["intent_span_manifest"])}
    return [by_id[span_id] for span_id in sorted(by_id, key=order.__getitem__)]


def layout_leaf_nodes_from_fragments(plan: Mapping[str, Any], fragments: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn exact extraction receipts into the leaf nodes consumed by reducers."""
    ordered = validate_all_layout_fragments(plan, fragments)
    output: list[dict[str, Any]] = []
    request_by_span = {str(row["span_id"]): str(row["request_id"]) for row in plan["layout_extraction_requests"]}
    for fragment in ordered:
        span_id = str(fragment["span_id"])
        sections, intent = _layout_rows_for_spans(plan, [span_id], {span_id: fragment})
        node = {
            "node_id": request_by_span[span_id],
            "candidate_id": plan["candidate_id"],
            "candidate_hash": plan["candidate_hash"],
            "span_ids": [span_id],
            "layout_by_section": sections,
            "intent_layout": intent,
        }
        node["node_hash"] = compute_v3_hash(node)
        _validate_layout_node(plan, node)
        output.append(node)
    return output


def _layout_rows_for_spans(plan: Mapping[str, Any], span_ids: Sequence[str], fragments: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scope_by_id = {str(row["section_id"]): row for row in plan["section_scope"]}
    span_meta = {str(row["span_id"]): row for row in plan["intent_span_manifest"]}
    section_rows: dict[str, list[str]] = {section_id: [] for section_id in scope_by_id}
    intent_rows: dict[str, list[str]] = {key: [] for key in plan["intent_field_names"]}
    for span_id in span_ids:
        meta = span_meta.get(str(span_id))
        fragment = fragments.get(str(span_id))
        if meta is None or fragment is None:
            raise SelectedNavigationError(f"layout node references unavailable source span {span_id}")
        if meta.get("section_id") is None:
            key = str(meta["source_field"]).removeprefix("intent.")
            if key not in intent_rows:
                raise SelectedNavigationError(f"layout node has foreign intent field span {span_id}")
            intent_rows[key].append(str(span_id))
        else:
            section_id = str(meta["section_id"])
            if section_id not in section_rows:
                raise SelectedNavigationError(f"layout node has foreign section span {span_id}")
            section_rows[section_id].append(str(span_id))
    layout_by_section = []
    for scope in plan["section_scope"]:
        ids = section_rows[scope["section_id"]]
        if ids:
            layout_by_section.append({
                "section_id": scope["section_id"], "section_hash": scope["section_hash"],
            "position": scope["position"], "task_ids": list(scope["task_ids"]),
            "hash_aliases": list(scope["hash_aliases"]),
                "span_ids": ids,
                "source_fields": [span_meta[item]["source_field"] for item in ids],
                "expression": " ".join(str(fragments[item]["expression"]) for item in ids),
            })
    intent_layout = []
    for key in plan["intent_field_names"]:
        ids = intent_rows[key]
        if ids:
            hashes = {span_meta[item]["source_hash"] for item in ids}
            if len(hashes) != 1:
                raise SelectedNavigationError(f"intent field {key} has inconsistent source hashes")
            intent_layout.append({
                "source_field": key, "source_hash": next(iter(hashes)), "span_ids": ids,
                "expression": " ".join(str(fragments[item]["expression"]) for item in ids),
            })
    return layout_by_section, intent_layout


def build_layout_reduction_requests(
    plan: Mapping[str, Any], level: int, ready_nodes: Sequence[Mapping[str, Any]], *,
    prompt_authority: PromptAuthority, route_estimator: RouteEstimator,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any], input_cap: int,
) -> dict[str, Any]:
    """Build exactly one whole binary reduction level after child nodes are READY."""
    _validate_plan_integrity(plan)
    if not plan.get("requires_coordination"):
        return {"level": 0, "requests": [], "carry_node_ids": [], "output_node_ids": []}
    levels = plan.get("layout_reduction_tree", {}).get("levels")
    if not isinstance(levels, list) or type(level) is not int or level < 0 or level >= len(levels):
        raise SelectedNavigationError(f"unknown layout reduction level {level}")
    level_plan = levels[level]
    if [str(node.get("node_id") or "") for node in ready_nodes] != level_plan["input_node_ids"]:
        raise SelectedNavigationError("layout reducer inputs are not the exact READY child level")
    nodes_by_id = {str(node["node_id"]): copy.deepcopy(dict(node)) for node in ready_nodes}
    for node in ready_nodes:
        _validate_layout_node(plan, node)
    requests: list[dict[str, Any]] = []
    for reducer in level_plan["reducers"]:
        children = [nodes_by_id[node_id] for node_id in reducer["child_node_ids"]]
        child_span_lists = [list(child["span_ids"]) for child in children]
        if child_span_lists != reducer["child_span_ids"]:
            raise SelectedNavigationError("layout reducer child span assignment changed")
        merged_spans = child_span_lists[0] + child_span_lists[1]
        if set(child_span_lists[0]) & set(child_span_lists[1]) or merged_spans != reducer["span_ids"]:
            raise SelectedNavigationError("layout reducer children are not an exact disjoint source partition")
        body = {
            "schema_version": REDUCE_REQUEST_SCHEMA,
            "candidate_id": plan["candidate_id"],
            "candidate_hash": plan["candidate_hash"],
            "node_id": reducer["node_id"],
            "level": level,
            "child_node_ids": list(reducer["child_node_ids"]),
            "child_hashes": [str(child["node_hash"]) for child in children],
            "child_nodes": [
                {key: copy.deepcopy(child[key]) for key in (
                    "node_id", "node_hash", "span_ids", "layout_by_section", "intent_layout",
                )}
                for child in children
            ],
            "expected_span_ids": merged_spans,
            # Internal reducers see only their two disjoint child summaries;
            # the complete section table is emitted once at the global root.
            "section_scope_hash": compute_v3_hash([
                {key: row[key] for key in ("section_id", "section_hash", "position", "task_ids", "hash_aliases")}
                for child in children for row in child.get("layout_by_section", [])
            ]),
            "expression_token_bound": int(plan["bounds"]["Tnav"]),
            "response_schema": REDUCE_RESPONSE_SCHEMA,
        }
        request, measurement = _estimate_request(
            REDUCE_KIND, str(reducer["node_id"]), body,
            prompt_authority=prompt_authority, route_estimator=route_estimator,
            context_reserve=context_reserve, input_cap=input_cap,
        )
        request_record = {
            "request_id": reducer["node_id"], "level": level, "body": body,
            "request": request, "measurement": measurement,
            "request_hash": measurement["request_hash"],
            "expected_span_ids": merged_spans,
            "expected_child_ids": list(reducer["child_node_ids"]),
        }
        _attach_output_reserve(
            plan, REDUCE_KIND, request_record,
            output_estimator=output_estimator,
            context_reserve=context_reserve,
        )
        requests.append(request_record)
    carry_ids = [level_plan["odd_carry_node_id"]] if level_plan.get("odd_carry_node_id") else []
    return {"level": level, "requests": requests, "carry_node_ids": carry_ids,
            "output_node_ids": list(level_plan["output_node_ids"])}


def _validate_layout_node(plan: Mapping[str, Any], node: Mapping[str, Any]) -> None:
    if node.get("candidate_id") != plan.get("candidate_id") or node.get("candidate_hash") != plan.get("candidate_hash"):
        raise SelectedNavigationError("layout reduction node has a foreign candidate")
    node_id = _nonempty(node.get("node_id"), "layout node ID")
    span_ids = _string_list(node.get("span_ids"), f"layout node {node_id}.span_ids")
    manifest = {str(row["span_id"]): row for row in plan["intent_span_manifest"]}
    if set(span_ids) - set(manifest):
        raise SelectedNavigationError(f"layout node {node_id} has foreign source spans")
    sections = node.get("layout_by_section")
    intent = node.get("intent_layout")
    if not isinstance(sections, list) or not isinstance(intent, list):
        raise SelectedNavigationError(f"layout node {node_id} has no complete expression tables")
    seen_sections: set[str] = set()
    seen_span_ids: list[str] = []
    for row in sections:
        if not isinstance(row, Mapping):
            raise SelectedNavigationError("layout node section row must be an object")
        sid = str(row.get("section_id") or "")
        scope = next((item for item in plan["section_scope"] if item["section_id"] == sid), None)
        if scope is None or sid in seen_sections:
            raise SelectedNavigationError(f"layout node has duplicate or foreign section {sid}")
        if (row.get("section_hash") != scope["section_hash"] or row.get("position") != scope["position"]
                or row.get("task_ids") != scope["task_ids"] or row.get("hash_aliases") != scope["hash_aliases"]):
            raise SelectedNavigationError(f"layout node changed section scope for {sid}")
        ids = _string_list(row.get("span_ids"), f"layout section {sid}.span_ids")
        if any(manifest[item].get("section_id") != sid for item in ids):
            raise SelectedNavigationError(f"layout section {sid} references a foreign source span")
        expected_fields = [manifest[item]["source_field"] for item in ids]
        if row.get("source_fields") != expected_fields:
            raise SelectedNavigationError(f"layout section {sid} changed its exact source-field assignment")
        _nonempty(row.get("expression"), f"layout section {sid}.expression")
        seen_sections.add(sid)
        seen_span_ids.extend(ids)
    seen_intents: set[str] = set()
    for row in intent:
        if not isinstance(row, Mapping):
            raise SelectedNavigationError("layout node intent row must be an object")
        field = str(row.get("source_field") or "")
        if field not in plan["intent_field_names"] or field in seen_intents:
            raise SelectedNavigationError(f"layout node has duplicate or foreign intent field {field}")
        if row.get("source_hash") != plan["intent_field_hashes"][field]:
            raise SelectedNavigationError(f"layout intent {field} source hash changed")
        ids = _string_list(row.get("span_ids"), f"layout intent {field}.span_ids")
        if any(manifest[item].get("section_id") is not None or manifest[item].get("source_field") != f"intent.{field}" for item in ids):
            raise SelectedNavigationError(f"layout intent {field} references a foreign source span")
        _nonempty(row.get("expression"), f"layout intent {field}.expression")
        seen_intents.add(field)
        seen_span_ids.extend(ids)
    if len(seen_span_ids) != len(set(seen_span_ids)) or set(seen_span_ids) != set(span_ids):
        raise SelectedNavigationError(f"layout node {node_id} does not carry each assigned span exactly once")
    expected_sections = {str(manifest[item].get("section_id")) for item in span_ids if manifest[item].get("section_id") is not None}
    expected_intents = {str(manifest[item]["source_field"]).removeprefix("intent.") for item in span_ids if manifest[item].get("section_id") is None}
    if seen_sections != expected_sections or seen_intents != expected_intents:
        raise SelectedNavigationError(f"layout node {node_id} omitted a section or intent expression")
    expected_hash = compute_v3_hash({key: copy.deepcopy(node[key]) for key in node if key != "node_hash"})
    if node.get("node_hash") != expected_hash:
        raise SelectedNavigationError(f"layout node {node_id} content hash changed")


def validate_layout_reduction_response(plan: Mapping[str, Any], request: Mapping[str, Any], response: Mapping[str, Any], *, input_nodes: Sequence[Mapping[str, Any]], output_estimator: OutputEstimator, context_reserve: Mapping[str, Any]) -> dict[str, Any]:
    _validate_plan_integrity(plan)
    reserve = _verify_output_reserve(
        plan, REDUCE_KIND, request,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    if not isinstance(response, Mapping) or response.get("schema_version") != REDUCE_RESPONSE_SCHEMA:
        raise SelectedNavigationError("layout reduction response has an unknown schema")
    if set(response) != {"schema_version", "candidate_id", "candidate_hash", "node_id", "child_node_ids",
                         "child_hashes", "span_ids", "layout_by_section", "intent_layout"}:
        raise SelectedNavigationError("layout reduction response has unknown or missing fields")
    expected_ids = list(request.get("expected_span_ids") or [])
    child_ids = list(request.get("expected_child_ids") or [])
    request_id = str(request.get("request_id") or "")
    level = request.get("level")
    levels = plan.get("layout_reduction_tree", {}).get("levels", [])
    if type(level) is not int or level < 0 or level >= len(levels):
        raise SelectedNavigationError("layout reduction request has a foreign level")
    matching_reducer = next((
        reducer for reducer in levels[level]["reducers"] if reducer["node_id"] == request_id
    ), None)
    if matching_reducer is None or matching_reducer["child_node_ids"] != child_ids or matching_reducer["span_ids"] != expected_ids:
        raise SelectedNavigationError("layout reduction request is not the exact deterministic tree node")
    children = {str(node["node_id"]): node for node in input_nodes}
    if set(children) != set(child_ids) or len(children) != len(child_ids):
        raise SelectedNavigationError("layout reduction does not have exact READY children")
    child_spans = [str(span) for child_id in child_ids for span in children[child_id]["span_ids"]]
    if len(child_spans) != len(set(child_spans)) or child_spans != expected_ids:
        raise SelectedNavigationError("layout reduction child span union is not exact")
    for key, expected in (("candidate_id", plan["candidate_id"]), ("candidate_hash", plan["candidate_hash"]),
                          ("node_id", request.get("request_id")), ("child_node_ids", child_ids),
                          ("child_hashes", [children[item]["node_hash"] for item in child_ids]),
                          ("span_ids", expected_ids)):
        if response.get(key) != expected:
            raise SelectedNavigationError(f"layout reduction response has changed {key}")
    layout_by_section = response.get("layout_by_section")
    intent_layout = response.get("intent_layout")
    if not isinstance(layout_by_section, list) or not isinstance(intent_layout, list):
        raise SelectedNavigationError("layout reduction response omitted expression tables")
    # Reuse the exact structure validator after constructing the content hash.
    normalized = {
        "schema_version": REDUCE_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "node_id": request["request_id"], "span_ids": expected_ids,
        "layout_by_section": copy.deepcopy(layout_by_section), "intent_layout": copy.deepcopy(intent_layout),
    }
    node = {
        "node_id": normalized["node_id"], "candidate_id": normalized["candidate_id"],
        "candidate_hash": normalized["candidate_hash"], "span_ids": expected_ids,
        "layout_by_section": normalized["layout_by_section"], "intent_layout": normalized["intent_layout"],
    }
    node["node_hash"] = compute_v3_hash(node)
    _validate_layout_node(plan, node)
    _verify_response_within_reserve(
        REDUCE_KIND, response, reserve,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    for row in [*layout_by_section, *intent_layout]:
        if output_estimator(REDUCE_KIND, {"expression": row["expression"]}) > int(plan["bounds"]["Tnav"]):
            raise SelectedNavigationError("layout reduction expression exceeds derived Tnav")
    return node


def build_global_navigation_request(
    plan: Mapping[str, Any], ready_root: Mapping[str, Any], *,
    prompt_authority: PromptAuthority, route_estimator: RouteEstimator,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any], input_cap: int,
) -> dict[str, Any]:
    """Build one global selected-only root request after the entire tree is READY."""
    _validate_plan_integrity(plan)
    if not plan.get("requires_coordination"):
        raise SelectedNavigationError("global navigation is not required for a single-section candidate")
    _validate_layout_node(plan, ready_root)
    if ready_root.get("span_ids") != [row["span_id"] for row in plan["intent_span_manifest"]]:
        raise SelectedNavigationError("global layout root does not contain every exact source span")
    if ready_root.get("node_id") != plan["layout_reduction_tree"].get("root_node_id"):
        raise SelectedNavigationError("global layout root is not the deterministic reduction-tree root")
    by_id = {str(row["section_id"]): row for row in ready_root["layout_by_section"]}
    if set(by_id) != {str(row["section_id"]) for row in plan["section_scope"]}:
        raise SelectedNavigationError("global layout root does not contain every selected section")
    intent_by_field = {str(row["source_field"]): row for row in ready_root["intent_layout"]}
    nonempty_fields = set(plan["intent_field_names"]) - set(plan["empty_intent_fields"])
    if set(intent_by_field) != nonempty_fields:
        raise SelectedNavigationError("global layout root does not contain every original intent field")
    exact_intent_layout = []
    for key in plan["intent_field_names"]:
        if key in plan["empty_intent_fields"]:
            exact_intent_layout.append({
                "source_field": key,
                "source_hash": plan["intent_field_hashes"][key],
                "span_ids": [],
                "expression": "(empty source field)",
            })
        else:
            exact_intent_layout.append(copy.deepcopy(intent_by_field[key]))
    body = {
        "schema_version": NAV_REQUEST_SCHEMA,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "plan_hash": plan["semantic_hash"],
        "root_node_id": ready_root["node_id"],
        "root_hash": ready_root["node_hash"],
        "span_manifest_hash": plan["span_manifest_hash"],
        "section_scope": [
            {**copy.deepcopy(scope),
             "span_ids": list(by_id[scope["section_id"]]["span_ids"]),
             "layout_expression": by_id[scope["section_id"]]["expression"]}
            for scope in plan["section_scope"]
        ],
        "intent_layout": exact_intent_layout,
        "merge_proposal_text_budget_tokens": int(plan["bounds"]["Tproposal"]),
        "response_schema": NAV_RESPONSE_SCHEMA,
        "contract": {
            "all_selected_section_ids_once": True,
            "global_order_across_all_tasks_and_shards": True,
            "merge_groups_may_cross_any_navigation_boundary": True,
            "source_facts_are_not_authorized_by_layout_expressions": True,
            "merge_requires_targeted_detail_review": True,
        },
    }
    node_id = _request_id(NAV_KIND, {
        "candidate_hash": plan["candidate_hash"], "root_hash": ready_root["node_hash"],
        "span_manifest_hash": plan["span_manifest_hash"],
    })
    request, measurement = _estimate_request(
        NAV_KIND, node_id, body, prompt_authority=prompt_authority,
        route_estimator=route_estimator, context_reserve=context_reserve, input_cap=input_cap,
    )
    record = {
        "request_id": node_id, "body": body, "request": request,
        "measurement": measurement, "request_hash": measurement["request_hash"],
        "plan_hash": plan["semantic_hash"],
    }
    _attach_output_reserve(
        plan, NAV_KIND, record,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    return record


def validate_global_navigation_response(plan: Mapping[str, Any], request: Mapping[str, Any], response: Mapping[str, Any], *, output_estimator: OutputEstimator, context_reserve: Mapping[str, Any]) -> _SealedArtifact:
    _validate_plan_integrity(plan)
    reserve = _verify_output_reserve(
        plan, NAV_KIND, request,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    navigation_request_hash = reserve["request_hash"]
    if not isinstance(response, Mapping) or response.get("schema_version") != NAV_RESPONSE_SCHEMA:
        raise SelectedNavigationError("global navigation response has an unknown schema")
    if set(response) != {"schema_version", "candidate_id", "candidate_hash", "root_node_id", "root_hash",
                         "plan_hash", "span_manifest_hash", "section_order", "merge_groups"}:
        raise SelectedNavigationError("global navigation response has unknown or missing fields")
    for key, expected in (("candidate_id", plan.get("candidate_id")),
                          ("candidate_hash", plan.get("candidate_hash")),
                          ("plan_hash", plan.get("semantic_hash")),
                          ("root_node_id", request.get("body", {}).get("root_node_id")),
                          ("root_hash", request.get("body", {}).get("root_hash")),
                          ("span_manifest_hash", plan.get("span_manifest_hash"))):
        if response.get(key) != expected:
            raise SelectedNavigationError(f"global navigation response has foreign or changed {key}")
    scopes = {str(row["section_id"]): row for row in plan["section_scope"]}
    groups = response.get("merge_groups")
    order = response.get("section_order")
    if not isinstance(groups, list) or not isinstance(order, list):
        raise SelectedNavigationError("global navigation response requires merge_groups and section_order")
    consumed: set[str] = set()
    outputs: set[str] = set()
    group_by_source: dict[str, str] = {}
    normalized_groups: list[dict[str, Any]] = []
    for index, raw in enumerate(groups):
        if not isinstance(raw, Mapping):
            raise SelectedNavigationError(f"merge proposal {index} must be an object")
        if set(raw) != {"source_section_ids", "source_section_hashes", "title", "goal", "integration_reason"}:
            raise SelectedNavigationError(f"merge proposal {index} has unknown or missing fields")
        ids = _string_list(raw.get("source_section_ids"), f"merge proposal {index}.source_section_ids")
        if len(ids) < 2 or any(section_id not in scopes or section_id in consumed for section_id in ids):
            raise SelectedNavigationError(f"merge proposal {index} has duplicate, foreign, or overlapping source IDs")
        source_hashes = raw.get("source_section_hashes")
        if not isinstance(source_hashes, Mapping) or set(source_hashes) != set(ids):
            raise SelectedNavigationError(f"merge proposal {index} has an incomplete source-section hash map")
        if any(source_hashes[section_id] != scopes[section_id]["section_hash"] for section_id in ids):
            raise SelectedNavigationError(f"merge proposal {index} has foreign source-section hashes")
        new_id = derive_merged_section_id(
            plan["candidate_hash"], ids,
            {section_id: str(scopes[section_id]["section_hash"]) for section_id in ids},
        )
        if new_id in scopes or new_id in outputs:
            raise SelectedNavigationError(f"merge proposal {index} has duplicate deterministic target ID")
        title = _nonempty(raw.get("title"), f"merge proposal {index}.title")
        goal = _nonempty(raw.get("goal"), f"merge proposal {index}.goal")
        reason = _nonempty(raw.get("integration_reason"), f"merge proposal {index}.integration_reason")
        for field, value in (("title", title), ("goal", goal), ("integration_reason", reason)):
            if output_estimator(NAV_KIND, {field: value}) > int(plan["bounds"]["Tproposal"]):
                raise SelectedNavigationError(f"merge proposal {index}.{field} exceeds derived Tproposal")
        normalized_groups.append({
            "new_section_id": new_id,
            "source_section_ids": ids,
            "source_section_hashes": {section_id: str(source_hashes[section_id]) for section_id in ids},
            "title": title, "goal": goal, "integration_reason": reason,
        })
        consumed.update(ids)
        outputs.add(new_id)
    normalized_order = _string_list(order, "global section_order")
    if len(normalized_order) != len(scopes) or set(normalized_order) != set(scopes):
        missing = sorted(set(scopes) - set(normalized_order))
        foreign = sorted(set(normalized_order) - set(scopes))
        raise SelectedNavigationError(f"global section_order is incomplete; missing={missing}, foreign={foreign}")
    for group in normalized_groups:
        for section_id in group["source_section_ids"]:
            group_by_source[section_id] = group["new_section_id"]
    compiled_order: list[str] = []
    emitted_groups: set[str] = set()
    for section_id in normalized_order:
        group_id = group_by_source.get(section_id)
        if group_id is None:
            compiled_order.append(section_id)
        elif group_id not in emitted_groups:
            compiled_order.append(group_id)
            emitted_groups.add(group_id)
    normalized = {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "plan_hash": plan["semantic_hash"],
        "root_node_id": request.get("body", {}).get("root_node_id"),
        "root_hash": request.get("body", {}).get("root_hash"),
        "span_manifest_hash": plan["span_manifest_hash"],
        "section_order": compiled_order,
        "source_section_order": normalized_order,
        "merge_groups": normalized_groups,
    }
    _verify_response_within_reserve(
        NAV_KIND, response, reserve,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    normalized["navigation_request_hash"] = navigation_request_hash
    normalized["navigation_response_hash"] = _hash(response)
    normalized["proposal_hash"] = _proposal_hash(normalized)
    return _SealedArtifact("navigation_proposal", normalized, {
        "plan_integrity_hash": plan["plan_integrity_hash"],
        "request_hash": navigation_request_hash,
        "response_hash": normalized["navigation_response_hash"],
    }, _token=_SEAL_TOKEN)


def _source_path_value(root: Any, path: Sequence[Any]) -> Any:
    value = root
    for step in path:
        if isinstance(value, Mapping):
            if not isinstance(step, str) or step not in value:
                raise SelectedNavigationError(f"source detail path is unavailable: {list(path)}")
            value = value[step]
        elif isinstance(value, list):
            if type(step) is not int or step < 0 or step >= len(value):
                raise SelectedNavigationError(f"source detail path is unavailable: {list(path)}")
            value = value[step]
        else:
            raise SelectedNavigationError(f"source detail path is unavailable: {list(path)}")
    return value


def _detail_paths(value: Any, path: list[Any] | None = None) -> list[list[Any]]:
    """Partition structured source into complete, hashable detail atoms.

    List rows and mapping values remain indivisible atoms.  This keeps support
    rows (including nested qualifiers/locators) exact while allowing a merge
    group to use several route-bounded requests when its complete closure is
    large.
    """
    base = [] if path is None else list(path)
    if isinstance(value, Mapping):
        if not value:
            return [base]
        out: list[list[Any]] = []
        for key in sorted(value, key=lambda item: str(item)):
            out.extend(_detail_paths(value[key], [*base, str(key)]))
        return out
    if isinstance(value, list):
        if not value:
            return [base]
        out = []
        for index, row in enumerate(value):
            # Keep a support row as one source-closed atom; nested fields such
            # as qualifiers, locators and source IDs cannot be split away.
            out.append([*base, index])
        return out
    return [base]


def _expected_detail_manifest(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    intent = plan.get("source_intent_fields") or {}
    for path in _detail_paths(intent):
        value = _source_path_value(intent, path)
        identity = {"scope_id": "intent", "path": path, "value_hash": compute_v3_hash(value)}
        items.append({"detail_id": "detail:" + _hash(identity)[:24], "scope_id": "intent",
                      "section_id": None, "path": path, "kind": "original_intent",
                      "value": copy.deepcopy(value), "value_hash": compute_v3_hash(value)})
    for scope in plan["section_scope"]:
        section_id = str(scope["section_id"])
        section = plan["source_sections"][section_id]
        for path in _detail_paths(section):
            value = _source_path_value(section, path)
            identity = {"section_id": section_id, "path": path, "value_hash": compute_v3_hash(value)}
            items.append({"detail_id": "detail:" + _hash(identity)[:24], "scope_id": section_id,
                          "section_id": section_id, "path": path, "kind": "selected_source_detail",
                          "value": copy.deepcopy(value), "value_hash": compute_v3_hash(value)})
    return items


def validate_source_detail_index(plan: Mapping[str, Any], detail_index: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Verify the caller-supplied source/claim/support inventory against exact source objects."""
    _validate_plan_integrity(plan)
    if not isinstance(detail_index, Mapping) or detail_index.get("schema_version") != DETAIL_INDEX_SCHEMA:
        raise SelectedNavigationError("source detail index has an unknown schema")
    if detail_index.get("candidate_id") != plan.get("candidate_id") or detail_index.get("candidate_hash") != plan.get("candidate_hash"):
        raise SelectedNavigationError("source detail index is foreign to the selected candidate")
    rows = detail_index.get("items")
    if not isinstance(rows, list):
        raise SelectedNavigationError("source detail index items must be an array")
    supplied: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SelectedNavigationError("source detail index contains a malformed item")
        detail_id = _nonempty(row.get("detail_id"), "detail item ID")
        if detail_id in supplied:
            raise SelectedNavigationError(f"duplicate source detail item {detail_id}")
        value_hash = compute_v3_hash(row.get("value"))
        if row.get("value_hash") != value_hash:
            raise SelectedNavigationError(f"source detail item {detail_id} hash does not match its value")
        supplied[detail_id] = copy.deepcopy(dict(row))
    index_hash = detail_index.get("index_hash")
    canonical_index = {key: copy.deepcopy(value) for key, value in detail_index.items() if key != "index_hash"}
    if index_hash != compute_v3_hash(canonical_index):
        raise SelectedNavigationError("source detail index hash changed")
    expected_rows = _expected_detail_manifest(plan)
    expected = {row["detail_id"]: row for row in expected_rows}
    if set(supplied) != set(expected):
        missing, extra = sorted(set(expected) - set(supplied)), sorted(set(supplied) - set(expected))
        raise SelectedNavigationError(f"source detail inventory is incomplete or foreign; missing={missing}, extra={extra}")
    for detail_id, expected_row in expected.items():
        row = supplied[detail_id]
        for key in ("scope_id", "section_id", "path", "kind", "value_hash"):
            if row.get(key) != expected_row.get(key):
                raise SelectedNavigationError(f"source detail item {detail_id} has changed {key}")
        try:
            source_root = plan["source_intent_fields"] if expected_row["scope_id"] == "intent" else plan["source_sections"][expected_row["section_id"]]
            source_value = _source_path_value(source_root, expected_row["path"])
        except (KeyError, SelectedNavigationError) as exc:
            raise SelectedNavigationError(f"source detail item {detail_id} is not reachable from exact source") from exc
        if row.get("value") != source_value:
            raise SelectedNavigationError(f"source detail item {detail_id} value differs from exact source")
    return [supplied[row["detail_id"]] for row in expected_rows]


def build_source_detail_index(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic index from exact structured source for tests/adapters.

    Production callers may supply their independently compiled source inventory
    instead.  Validation always compares every item back to the selected local
    source object and its section/candidate hashes.
    """
    _validate_plan_integrity(plan)
    result: dict[str, Any] = {
        "schema_version": DETAIL_INDEX_SCHEMA,
        "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"],
        "items": _expected_detail_manifest(plan),
    }
    result["index_hash"] = compute_v3_hash(result)
    return result


_CRITICAL_LITERAL_KEYS = {
    "claim", "claim_id", "paper_key", "source_claim_ids", "evidence_ids", "source_field_ids",
    "task_id", "task_ids", "study_id", "qualifier", "qualifiers", "qualifier_id",
    "locator", "locators", "locator_id", "direction", "disagreement", "relation_id",
    "relation_ids", "minority", "null", "reverse",
}


def _critical_literal_bindings(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for item in items:
        detail_id = str(item["detail_id"])

        def visit(value: Any, path: list[Any], *, bound_id: str = detail_id, bound_hash: str = str(item["value_hash"])) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    next_path = [*path, str(key)]
                    if str(key).casefold() in _CRITICAL_LITERAL_KEYS:
                        bindings.append({
                            "detail_id": bound_id,
                            "source_hash": bound_hash,
                            "path": next_path,
                            "value": copy.deepcopy(child),
                            "value_hash": compute_v3_hash(child),
                        })
                    else:
                        visit(child, next_path)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, [*path, index])

        visit(item["value"], list(item["path"]))
    return bindings


def _detail_request_body(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], group: Mapping[str, Any],
    source_items: Sequence[Mapping[str, Any]], intent_items: Sequence[Mapping[str, Any]],
    batch_index: int, batch_count: int, slice_id: str, max_summary_tokens: int,
) -> dict[str, Any]:
    critical = _critical_literal_bindings([*intent_items, *source_items])
    source_critical = _critical_literal_bindings(source_items)
    critical_refs = [
        {key: copy.deepcopy(binding[key]) for key in ("detail_id", "source_hash", "path", "value_hash")}
        for binding in critical
    ]
    source_critical_refs = [
        {key: copy.deepcopy(binding[key]) for key in ("detail_id", "source_hash", "path", "value_hash")}
        for binding in source_critical
    ]
    return {
        "schema_version": DETAIL_REQUEST_SCHEMA,
        "stage": "source_detail_slice_assessment",
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "proposal_hash": proposal["proposal_hash"],
        "group_id": group["new_section_id"],
        "slice_id": slice_id,
        "source_section_ids": list(group["source_section_ids"]),
        "source_section_hashes": copy.deepcopy(dict(group["source_section_hashes"])),
        "proposed_title": group["title"], "proposed_goal": group["goal"],
        "proposed_integration_reason": group["integration_reason"],
        "batch_index": batch_index, "batch_count": batch_count,
        "detail_item_ids": [str(item["detail_id"]) for item in source_items],
        "detail_item_hashes": {str(item["detail_id"]): str(item["value_hash"]) for item in source_items},
        "exact_detail_items": [
            {"detail_id": item["detail_id"], "kind": item["kind"], "scope_id": item["scope_id"],
             "path": copy.deepcopy(item["path"]), "value_hash": item["value_hash"],
             "value": copy.deepcopy(item["value"])}
            for item in source_items
        ],
        "intent_detail_item_ids": [str(item["detail_id"]) for item in intent_items],
        "intent_detail_item_hashes": {str(item["detail_id"]): str(item["value_hash"]) for item in intent_items},
        "exact_intent_items": [
            {"detail_id": item["detail_id"], "kind": item["kind"], "scope_id": item["scope_id"],
             "path": copy.deepcopy(item["path"]), "value_hash": item["value_hash"],
             "value": copy.deepcopy(item["value"])}
            for item in intent_items
        ],
        "critical_literal_binding_refs": critical_refs,
        "critical_literal_binding_refs_hash": compute_v3_hash(critical_refs),
        "source_critical_literal_binding_refs": source_critical_refs,
        "source_critical_literal_binding_refs_hash": compute_v3_hash(source_critical_refs),
        "max_summary_tokens": max_summary_tokens,
        "response_schema": DETAIL_SLICE_RESPONSE_SCHEMA,
    }


def _detail_slice_response_shape(
    plan: Mapping[str, Any], group: Mapping[str, Any], body: Mapping[str, Any],
    *, summary_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": DETAIL_SLICE_RESPONSE_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "proposal_hash": body["proposal_hash"], "group_id": group["new_section_id"],
        "slice_id": body["slice_id"],
        "detail_item_ids": list(body["detail_item_ids"]),
        "detail_item_hashes": copy.deepcopy(dict(body["detail_item_hashes"])),
        "critical_literal_bindings_hash": body["source_critical_literal_binding_refs_hash"],
        "intent_context_hash": compute_v3_hash({
            key: body[key] for key in ("intent_detail_item_ids", "intent_detail_item_hashes")
        }),
        "assessments": [
            {"detail_id": item, "value_hash": body["detail_item_hashes"][item], "disposition": "compatible"}
            for item in body["detail_item_ids"]
        ],
        "summary": _token_placeholder(summary_tokens) if summary_tokens is not None else "x",
    }


def _make_detail_adjudication_tree(candidate_hash: str, proposal_hash: str, group_id: str, slices: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    active = [{
        "node_id": str(row["request_id"]), "node_kind": "slice",
        "detail_item_ids": list(row["detail_item_ids"]),
        "detail_item_hashes": copy.deepcopy(dict(row["detail_item_hashes"])),
        "critical_literal_bindings": copy.deepcopy(row["critical_literal_bindings"]),
        "critical_literal_bindings_hash": row["critical_literal_bindings_hash"],
        "minimum_detail_order": index,
    } for index, row in enumerate(slices)]
    levels: list[dict[str, Any]] = []
    depth = 0
    while len(active) > 1:
        reducers: list[dict[str, Any]] = []
        next_active: list[dict[str, Any]] = []
        index = 0
        while index + 1 < len(active):
            left, right = active[index], active[index + 1]
            left_ids, right_ids = list(left["detail_item_ids"]), list(right["detail_item_ids"])
            if set(left_ids) & set(right_ids):
                raise SelectedNavigationError("merge-detail reduction children overlap source items")
            detail_ids = [*left_ids, *right_ids]
            detail_hashes = {**left["detail_item_hashes"], **right["detail_item_hashes"]}
            critical = [*left["critical_literal_bindings"], *right["critical_literal_bindings"]]
            critical_hash = compute_v3_hash(critical)
            identity = {
                "schema": DETAIL_REDUCE_REQUEST_SCHEMA,
                "candidate_hash": candidate_hash, "proposal_hash": proposal_hash,
                "group_id": group_id, "level": depth,
                "child_node_ids": [left["node_id"], right["node_id"]],
                "detail_item_ids": detail_ids,
            }
            node_id = "merge-detail-reduce:" + _hash(identity)[:24]
            reducer = {
                "node_id": node_id, "level": depth,
                "child_node_ids": [left["node_id"], right["node_id"]],
                "child_detail_item_ids": [left_ids, right_ids],
                "detail_item_ids": detail_ids,
                "detail_item_hashes": detail_hashes,
                "critical_literal_bindings": critical,
                "critical_literal_bindings_hash": critical_hash,
                "minimum_detail_order": min(left["minimum_detail_order"], right["minimum_detail_order"]),
            }
            reducers.append(reducer)
            next_active.append({
                "node_id": node_id, "node_kind": "reduce", "detail_item_ids": detail_ids,
                "detail_item_hashes": detail_hashes, "critical_literal_bindings": critical,
                "critical_literal_bindings_hash": critical_hash,
                "minimum_detail_order": reducer["minimum_detail_order"],
            })
            index += 2
        carry = None
        if index < len(active):
            carry = copy.deepcopy(active[index])
            next_active.append(carry)
        levels.append({
            "level": depth,
            "input_node_ids": [row["node_id"] for row in active],
            "reducers": reducers,
            "odd_carry_node_id": carry["node_id"] if carry else None,
            "output_node_ids": [row["node_id"] for row in next_active],
        })
        active = next_active
        depth += 1
    return {
        "schema_version": "selected_candidate_merge_detail_tree/v1",
        "group_id": group_id,
        "levels": levels,
        "root_node_id": active[0]["node_id"] if active else None,
        "root_detail_item_ids": list(active[0]["detail_item_ids"]) if active else [],
        "root_detail_item_hashes": copy.deepcopy(dict(active[0]["detail_item_hashes"])) if active else {},
        "root_critical_literal_bindings": copy.deepcopy(active[0]["critical_literal_bindings"]) if active else [],
        "root_critical_literal_bindings_hash": active[0]["critical_literal_bindings_hash"] if active else compute_v3_hash([]),
    }


def build_proposed_merge_detail_requests(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_index: Mapping[str, Any], *,
    prompt_authority: PromptAuthority, route_estimator: RouteEstimator, output_estimator: OutputEstimator,
    context_reserve: Mapping[str, Any], input_cap: int, emax: int,
) -> _SealedArtifact:
    """Build measured source-detail slices plus the complete counted adjudication tree."""
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    if proposal_payload.get("candidate_id") != plan.get("candidate_id") or proposal_payload.get("candidate_hash") != plan.get("candidate_hash"):
        raise SelectedNavigationError("merge detail proposal is foreign to selected candidate")
    if proposal_payload.get("proposal_hash") != _proposal_hash(proposal_payload):
        raise SelectedNavigationError("merge detail proposal hash changed")
    items = validate_source_detail_index(plan, detail_index)
    groups = proposal_payload.get("merge_groups") or []
    if not isinstance(groups, list):
        raise SelectedNavigationError("validated merge_groups must be an array")
    max_calls = min(_integer(emax, "emax"), _integer(plan["emax"], "plan.emax"))
    detail_index_hash = str(detail_index.get("index_hash"))
    if not groups:
        payload = {
            "schema_version": DETAIL_WORK_PLAN_SCHEMA,
            "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
            "plan_integrity_hash": plan["plan_integrity_hash"],
            "proposal_hash": proposal_payload["proposal_hash"], "proposal_seal": proposal_seal,
            "detail_index_hash": detail_index_hash, "requests": [], "adjudication_trees": {},
            "counts": {"Qdetail_slice": 0, "Qdetail_reduce": 0, "Qdetail_final": 0, "Qmerge_detail": 0},
            "emax": max_calls,
        }
        payload["work_plan_hash"] = compute_v3_hash(payload)
        return _SealedArtifact("merge_detail_work_plan", payload, {
            "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
            "detail_index_hash": detail_index_hash,
        }, _token=_SEAL_TOKEN)

    intent_items = [item for item in items if item["scope_id"] == "intent"]
    all_requests: list[dict[str, Any]] = []
    trees: dict[str, dict[str, Any]] = {}
    for group in groups:
        source_ids = set(group["source_section_ids"])
        source_items = [item for item in items if item.get("section_id") in source_ids]
        partitions: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        group_id = str(group["new_section_id"])

        def slice_identity(rows: Sequence[Mapping[str, Any]], *, group_id: str = group_id) -> tuple[str, str]:
            ids = [str(row["detail_id"]) for row in rows]
            node = _request_id(DETAIL_KIND, {
                "candidate_hash": plan["candidate_hash"], "proposal_hash": proposal_payload["proposal_hash"],
                "group_id": group_id, "detail_item_ids": ids,
            })
            return node, "merge-detail-slice:" + _hash({"group_id": group_id, "request_id": node})[:24]

        def make_body(rows: Sequence[Mapping[str, Any]], index: int, count: int, slice_id: str, summary_tokens: int = 1, *, group: Mapping[str, Any] = group) -> dict[str, Any]:
            return _detail_request_body(
                plan, proposal_payload, group, rows, intent_items, index, count, slice_id, summary_tokens
            )

        # Confirm the exact intent context itself is representable before trying
        # to add any source detail rows.
        intent_request_id, intent_slice_id = slice_identity([])
        intent_body = make_body([], 0, 1, intent_slice_id)
        try:
            _estimate_request(DETAIL_KIND, intent_request_id, intent_body,
                             prompt_authority=prompt_authority, route_estimator=route_estimator,
                             context_reserve=context_reserve, input_cap=input_cap)
        except SelectedNavigationError as exc:
            if "over the effective input cap" not in str(exc) and "exceeds verified context" not in str(exc):
                raise
            raise SelectedNavigationError(
                f"complete exact intent for proposed merge group {group_id} exceeds the route input/context bound before POST"
            ) from exc

        for item in source_items:
            trial = [*current, item]
            trial_id, trial_slice = slice_identity(trial)
            body = make_body(trial, len(partitions), 1, trial_slice)
            try:
                _estimate_request(DETAIL_KIND, trial_id, body, prompt_authority=prompt_authority,
                                 route_estimator=route_estimator, context_reserve=context_reserve,
                                 input_cap=input_cap)
            except SelectedNavigationError as exc:
                if "over the effective input cap" not in str(exc) and "exceeds verified context" not in str(exc):
                    raise
                if not current:
                    raise SelectedNavigationError(
                        f"indivisible merge detail item {item['detail_id']} for group {group_id} exceeds "
                        "the exact route input/context bound before POST"
                    ) from exc
                partitions.append(current)
                current = [item]
                single_id, single_slice = slice_identity(current)
                single_body = make_body(current, len(partitions), 1, single_slice)
                try:
                    _estimate_request(DETAIL_KIND, single_id, single_body, prompt_authority=prompt_authority,
                                     route_estimator=route_estimator, context_reserve=context_reserve,
                                     input_cap=input_cap)
                except SelectedNavigationError as singleton_exc:
                    raise SelectedNavigationError(
                        f"indivisible merge detail item {item['detail_id']} for group {group_id} exceeds "
                        "the exact route input/context bound before POST"
                    ) from singleton_exc
            else:
                current = trial
        if current:
            partitions.append(current)

        slice_rows: list[dict[str, Any]] = []
        total = len(partitions)
        for index, rows in enumerate(partitions):
            request_id, slice_id = slice_identity(rows)
            body = make_body(rows, index, total, slice_id)
            shape = _detail_slice_response_shape(plan, group, body)
            summary_tokens, _summary_skeleton = _derive_text_bound(
                DETAIL_KIND, lambda text, shape=shape: {**shape, "summary": text},
                output_estimator=output_estimator, context_reserve=context_reserve,
                upper=int(plan["bounds"]["Tproposal"]),
            )
            body["max_summary_tokens"] = summary_tokens
            request, measurement = _estimate_request(
                DETAIL_KIND, request_id, body, prompt_authority=prompt_authority,
                route_estimator=route_estimator, context_reserve=context_reserve, input_cap=input_cap,
            )
            source_critical = _critical_literal_bindings(rows)
            slice_record = {
                "request_id": request_id, "slice_id": slice_id, "group_id": group_id,
                "detail_item_ids": [str(item["detail_id"]) for item in rows],
                "detail_item_hashes": {str(item["detail_id"]): str(item["value_hash"]) for item in rows},
                "critical_literal_bindings": source_critical,
                "critical_literal_bindings_hash": compute_v3_hash(source_critical),
                "body": body, "request": request, "request_hash": measurement["request_hash"],
                "measurement": measurement,
            }
            _attach_output_reserve(
                plan, DETAIL_KIND, slice_record,
                output_estimator=output_estimator, context_reserve=context_reserve,
            )
            slice_rows.append(slice_record)
            all_requests.append(slice_record)
        tree = _make_detail_adjudication_tree(
            plan["candidate_hash"], proposal_payload["proposal_hash"], group_id, slice_rows
        )
        trees[group_id] = tree

    q_slices = len(all_requests)
    q_reduces = sum(len(level["reducers"]) for tree in trees.values() for level in tree["levels"])
    q_final = len(groups)
    q_total = q_slices + q_reduces + q_final
    if q_total > max_calls:
        raise SelectedNavigationError(
            f"complete merge detail adjudication path exceeds Qdetail<=Emax before POST "
            f"(slices={q_slices}, reduces={q_reduces}, final={q_final}, total={q_total}, Emax={max_calls})"
        )
    payload = {
        "schema_version": DETAIL_WORK_PLAN_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "plan_integrity_hash": plan["plan_integrity_hash"],
        "proposal_hash": proposal_payload["proposal_hash"], "proposal_seal": proposal_seal,
        "detail_index_hash": detail_index_hash,
        "requests": all_requests,
        "adjudication_trees": trees,
        "counts": {"Qdetail_slice": q_slices, "Qdetail_reduce": q_reduces,
                   "Qdetail_final": q_final, "Qmerge_detail": q_total},
        "emax": max_calls,
    }
    payload["work_plan_hash"] = compute_v3_hash(payload)
    return _SealedArtifact("merge_detail_work_plan", payload, {
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
        "detail_index_hash": detail_index_hash,
    }, _token=_SEAL_TOKEN)


def selected_navigation_call_counts(plan: Mapping[str, Any], detail_plan: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Return and enforce actual selected-only navigation/detail call counts."""
    _validate_plan_integrity(plan)
    fnav = len(plan.get("intent_span_manifest") or [])
    jnav = len(plan.get("layout_extraction_requests") or [])
    tree = plan.get("layout_reduction_tree")
    levels = tree.get("levels", []) if isinstance(tree, Mapping) else []
    anav = sum(len(level.get("reducers") or []) for level in levels if isinstance(level, Mapping))
    qglobal = 1 if plan.get("requires_coordination") else 0
    qdetail = 0
    if detail_plan is not None:
        detail_payload, _detail_seal = _validated_detail_work_plan(detail_plan)
        slices = len(detail_payload.get("requests") or [])
        trees = detail_payload.get("adjudication_trees")
        if not isinstance(trees, Mapping):
            raise SelectedNavigationError("merge-detail work plan has no adjudication tree")
        reductions = sum(len(level.get("reducers") or []) for tree_row in trees.values() for level in tree_row.get("levels", []))
        finals = len(trees)
        qdetail = slices + reductions + finals
        derived_counts = {"Qdetail_slice": slices, "Qdetail_reduce": reductions,
                          "Qdetail_final": finals, "Qmerge_detail": qdetail}
        if detail_payload.get("counts") != derived_counts:
            raise SelectedNavigationError("merge-detail call counts differ from exact work plan")
        if qdetail > min(_integer(detail_payload.get("emax"), "detail_plan.emax"), _integer(plan.get("emax"), "plan.emax")):
            raise SelectedNavigationError("actual targeted merge-detail calls exceed Emax")
    if jnav > fnav or anav > max(0, fnav - 1) or qglobal > 1:
        raise SelectedNavigationError("selected navigation calls exceed Jnav/Anav/Qglobal limits")
    if qglobal == 0 and (jnav or anav):
        raise SelectedNavigationError("navigation extraction/reduction exists without a global coordination call")
    qnav = jnav + anav + qglobal
    if fnav >= 1 and qnav > 2 * fnav:
        raise SelectedNavigationError("Qnav exceeds the finite 2*Fnav bound")
    return {"Jnav": jnav, "Anav": anav, "Qglobal": qglobal,
            "Qnav": qnav, "Qmerge_detail": qdetail, "Qtotal": qnav + qdetail}


def validate_merge_detail_responses(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_plan: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]], *, detail_index: Mapping[str, Any],
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
    actual_requests: Sequence[Mapping[str, Any]] | None = None,
) -> _SealedArtifact:
    """Validate source-detail slices only; they cannot decide the proposed group."""
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    work, work_seal = _validated_detail_work_plan(detail_plan)
    if work.get("plan_integrity_hash") != plan.get("plan_integrity_hash") or work.get("proposal_seal") != proposal_seal:
        raise SelectedNavigationError("detail slices are foreign to the selected proposal or source plan")
    if work.get("proposal_hash") != proposal_payload.get("proposal_hash"):
        raise SelectedNavigationError("detail work plan proposal hash changed")
    items = validate_source_detail_index(plan, detail_index)
    if detail_index.get("index_hash") != work.get("detail_index_hash"):
        raise SelectedNavigationError("detail index changed after the source-detail work plan")
    expected_requests = list(work.get("requests") or [])
    requests = list(actual_requests) if actual_requests is not None else copy.deepcopy(expected_requests)
    expected_by_id = {str(row["request_id"]): row for row in expected_requests}
    actual_by_id = {str(row.get("request_id") or ""): row for row in requests}
    if len(expected_by_id) != len(expected_requests) or len(actual_by_id) != len(requests) or set(actual_by_id) != set(expected_by_id):
        raise SelectedNavigationError("merge detail slice requests contain missing, duplicate, or foreign nodes")
    response_by_id = {str(row.get("request_id") or ""): row for row in responses if isinstance(row, Mapping)}
    if len(response_by_id) != len(responses) or set(response_by_id) != set(expected_by_id):
        raise SelectedNavigationError("merge detail slice results contain missing, duplicate, or foreign nodes")
    source_rows = {str(row["detail_id"]): row for row in items if row["scope_id"] != "intent"}
    intent_rows = {str(row["detail_id"]): row for row in items if row["scope_id"] == "intent"}
    slices_by_group: dict[str, list[dict[str, Any]]] = {}
    request_hashes: dict[str, str] = {}
    response_hashes: dict[str, str] = {}
    group_source_coverage: dict[str, set[str]] = {}
    for request_id, expected_request in expected_by_id.items():
        request = actual_by_id[request_id]
        planned_measurement = expected_request.get("measurement")
        planned_reserve = planned_measurement.get("output_reserve") if isinstance(planned_measurement, Mapping) else None
        if not isinstance(planned_reserve, Mapping):
            raise SelectedNavigationError(f"merge detail slice {request_id} lacks its pre-POST output reserve")
        reserve = _verify_output_reserve(
            plan, DETAIL_KIND, request,
            output_estimator=output_estimator, context_reserve=context_reserve,
            expected_reserve=planned_reserve,
        )
        measured_hash = reserve["request_hash"]
        if measured_hash != expected_request.get("request_hash"):
            raise SelectedNavigationError(f"merge detail slice {request_id} differs from its measured work-plan request")
        if request.get("group_id") != expected_request.get("group_id") or request.get("body") != expected_request.get("body"):
            raise SelectedNavigationError(f"merge detail slice {request_id} changed its canonical source assignment")
        body = request["body"]
        group_id = str(request["group_id"])
        source_ids = list(body.get("detail_item_ids") or [])
        intent_ids = list(body.get("intent_detail_item_ids") or [])
        if set(intent_ids) != set(intent_rows) or len(intent_ids) != len(intent_rows):
            raise SelectedNavigationError(f"merge detail slice {request_id} omitted exact original intent")
        if len(source_ids) != len(set(source_ids)) or any(item not in source_rows for item in source_ids):
            raise SelectedNavigationError(f"merge detail slice {request_id} has duplicate or foreign source item IDs")
        group_source_coverage.setdefault(group_id, set()).update(source_ids)
        intent_hashes = {item: intent_rows[item]["value_hash"] for item in intent_ids}
        if body.get("intent_detail_item_hashes") != intent_hashes:
            raise SelectedNavigationError(f"merge detail slice {request_id} has a foreign intent hash map")
        exact_intent = body.get("exact_intent_items")
        exact_source = body.get("exact_detail_items")
        if not isinstance(exact_intent, list) or not isinstance(exact_source, list):
            raise SelectedNavigationError(f"merge detail slice {request_id} omitted exact source or intent values")
        if [row.get("detail_id") for row in exact_intent if isinstance(row, Mapping)] != intent_ids:
            raise SelectedNavigationError(f"merge detail slice {request_id} has incomplete exact intent values")
        if [row.get("detail_id") for row in exact_source if isinstance(row, Mapping)] != source_ids:
            raise SelectedNavigationError(f"merge detail slice {request_id} has incomplete exact source values")
        for raw_item in [*exact_intent, *exact_source]:
            row = intent_rows.get(str(raw_item.get("detail_id"))) or source_rows.get(str(raw_item.get("detail_id")))
            if row is None or raw_item.get("value_hash") != row["value_hash"] or raw_item.get("value") != row["value"]:
                raise SelectedNavigationError(f"merge detail slice {request_id} carries changed exact detail")
        critical = _critical_literal_bindings([intent_rows[item] for item in intent_ids] + [source_rows[item] for item in source_ids])
        source_critical = _critical_literal_bindings([source_rows[item] for item in source_ids])
        expected_refs = [
            {key: copy.deepcopy(binding[key]) for key in ("detail_id", "source_hash", "path", "value_hash")}
            for binding in critical
        ]
        expected_source_refs = [
            {key: copy.deepcopy(binding[key]) for key in ("detail_id", "source_hash", "path", "value_hash")}
            for binding in source_critical
        ]
        if body.get("critical_literal_binding_refs") != expected_refs or body.get("critical_literal_binding_refs_hash") != compute_v3_hash(expected_refs):
            raise SelectedNavigationError(f"merge detail slice {request_id} changed exact critical bindings")
        if body.get("source_critical_literal_binding_refs") != expected_source_refs or body.get("source_critical_literal_binding_refs_hash") != compute_v3_hash(expected_source_refs):
            raise SelectedNavigationError(f"merge detail slice {request_id} changed exact source critical bindings")
        wrapped = response_by_id[request_id]
        response = wrapped.get("response")
        if not isinstance(response, Mapping) or response.get("schema_version") != DETAIL_SLICE_RESPONSE_SCHEMA:
            raise SelectedNavigationError(f"merge detail slice {request_id} must return slice assessment, not group adjudication")
        if set(response) != {
            "schema_version", "candidate_id", "candidate_hash", "proposal_hash", "group_id", "slice_id",
            "detail_item_ids", "detail_item_hashes", "critical_literal_bindings_hash",
            "intent_context_hash", "assessments", "summary",
        }:
            raise SelectedNavigationError(f"merge detail slice {request_id} has unknown or missing response fields")
        for key, expected_value in (
            ("candidate_id", plan["candidate_id"]), ("candidate_hash", plan["candidate_hash"]),
            ("proposal_hash", proposal_payload["proposal_hash"]), ("group_id", group_id),
            ("slice_id", body["slice_id"]), ("detail_item_ids", source_ids),
            ("detail_item_hashes", body["detail_item_hashes"]),
            ("critical_literal_bindings_hash", body["source_critical_literal_binding_refs_hash"]),
            ("intent_context_hash", compute_v3_hash({
                key: body[key] for key in ("intent_detail_item_ids", "intent_detail_item_hashes")
            })),
        ):
            if response.get(key) != expected_value:
                raise SelectedNavigationError(f"merge detail slice {request_id} changed {key}")
        assessments = response.get("assessments")
        if not isinstance(assessments, list) or len(assessments) != len(source_ids):
            raise SelectedNavigationError(f"merge detail slice {request_id} has incomplete source assessments")
        statuses: dict[str, str] = {}
        for expected_id, assessment in zip(source_ids, assessments, strict=True):
            if not isinstance(assessment, Mapping) or set(assessment) != {"detail_id", "value_hash", "disposition"}:
                raise SelectedNavigationError(f"merge detail slice {request_id} has malformed source assessment")
            if assessment.get("detail_id") != expected_id or assessment.get("value_hash") != source_rows[expected_id]["value_hash"]:
                raise SelectedNavigationError(f"merge detail slice {request_id} assessment is foreign to its exact source")
            if assessment.get("disposition") not in {"compatible", "conflict", "uncertain"}:
                raise SelectedNavigationError(f"merge detail slice {request_id} has an unknown source disposition")
            statuses[expected_id] = str(assessment["disposition"])
        summary = _nonempty(response.get("summary"), f"merge detail slice {request_id}.summary")
        if output_estimator(DETAIL_KIND, {"summary": summary}) > int(body["max_summary_tokens"]):
            raise SelectedNavigationError(f"merge detail slice {request_id} summary exceeds its measured text allowance")
        _verify_response_within_reserve(
            DETAIL_KIND, response, reserve,
            output_estimator=output_estimator, context_reserve=context_reserve,
        )
        disposition = "conflict" if "conflict" in statuses.values() else (
            "uncertain" if "uncertain" in statuses.values() else "compatible"
        )
        node = {
            "schema_version": "selected_candidate_merge_detail_slice_node/v1",
            "node_id": request_id, "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
            "proposal_hash": proposal_payload["proposal_hash"], "group_id": group_id,
            "detail_item_ids": source_ids,
            "detail_item_hashes": copy.deepcopy(dict(body["detail_item_hashes"])),
            "critical_literal_bindings": source_critical,
            "critical_literal_bindings_hash": compute_v3_hash(source_critical),
            "assessments": statuses, "disposition": disposition, "summary": summary,
        }
        node["node_hash"] = compute_v3_hash(node)
        slices_by_group.setdefault(group_id, []).append(node)
        request_hashes[request_id] = measured_hash
        response_hashes[request_id] = _hash(response)
    expected_by_group = {
        str(group["new_section_id"]): {
            str(item["detail_id"]) for item in items if item.get("section_id") in set(group["source_section_ids"])
        }
        for group in proposal_payload.get("merge_groups") or []
    }
    for group_id, expected_ids in expected_by_group.items():
        if group_source_coverage.get(group_id) != expected_ids:
            missing = sorted(expected_ids - group_source_coverage.get(group_id, set()))
            extra = sorted(group_source_coverage.get(group_id, set()) - expected_ids)
            raise SelectedNavigationError(f"merge detail slices do not cover source group {group_id}; missing={missing}, foreign={extra}")
        slices_by_group[group_id].sort(key=lambda row: next(
            index for index, request in enumerate(expected_requests) if request["request_id"] == row["node_id"]
        ))
    payload = {
        "schema_version": DETAIL_SLICE_RESULTS_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_hash": proposal_payload["proposal_hash"],
        "detail_work_plan_hash": work.get("work_plan_hash"), "detail_index_hash": detail_index.get("index_hash"),
        "groups": {group_id: {"slice_nodes": rows} for group_id, rows in slices_by_group.items()},
        "request_ids": list(request_hashes),
    }
    return _SealedArtifact("merge_detail_slice_results", payload, {
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
        "detail_work_plan_seal": work_seal, "request_hashes": request_hashes,
        "response_hashes": response_hashes,
    }, _token=_SEAL_TOKEN)


def _validate_detail_node(node: Mapping[str, Any], plan: Mapping[str, Any], proposal: Mapping[str, Any], group_id: str) -> None:
    if node.get("schema_version") not in {"selected_candidate_merge_detail_slice_node/v1", DETAIL_REDUCTION_NODE_SCHEMA}:
        raise SelectedNavigationError("merge-detail node has an unknown validation schema")
    if node.get("candidate_id") != plan["candidate_id"] or node.get("candidate_hash") != plan["candidate_hash"]:
        raise SelectedNavigationError("merge-detail node has a foreign candidate")
    if node.get("proposal_hash") != proposal["proposal_hash"] or node.get("group_id") != group_id:
        raise SelectedNavigationError("merge-detail node has a foreign proposal or group")
    node_id = _nonempty(node.get("node_id"), "merge-detail node ID")
    detail_ids = _string_list(node.get("detail_item_ids"), f"{node_id}.detail_item_ids")
    detail_hashes = node.get("detail_item_hashes")
    if not isinstance(detail_hashes, Mapping) or set(detail_hashes) != set(detail_ids):
        raise SelectedNavigationError(f"merge-detail node {node_id} has an incomplete detail hash map")
    bindings = node.get("critical_literal_bindings")
    if not isinstance(bindings, list) or node.get("critical_literal_bindings_hash") != compute_v3_hash(bindings):
        raise SelectedNavigationError(f"merge-detail node {node_id} has incomplete critical literal bindings")
    assessments = node.get("assessments")
    if not isinstance(assessments, Mapping) or set(assessments) != set(detail_ids):
        raise SelectedNavigationError(f"merge-detail node {node_id} has incomplete per-source assessments")
    if any(value not in {"compatible", "conflict", "uncertain"} for value in assessments.values()):
        raise SelectedNavigationError(f"merge-detail node {node_id} has invalid source assessment dispositions")
    disposition = node.get("disposition")
    if disposition not in {"compatible", "conflict", "uncertain"}:
        raise SelectedNavigationError(f"merge-detail node {node_id} has an invalid disposition")
    if disposition == "compatible" and any(value != "compatible" for value in assessments.values()):
        raise SelectedNavigationError(f"merge-detail node {node_id} erased an adverse source assessment")
    _nonempty(node.get("summary"), f"merge-detail node {node_id}.summary")
    expected_hash = compute_v3_hash({key: copy.deepcopy(value) for key, value in node.items() if key != "node_hash"})
    if node.get("node_hash") != expected_hash:
        raise SelectedNavigationError(f"merge-detail node {node_id} content hash changed")


def _validated_slice_result_payload(
    slice_results: Any, plan: Mapping[str, Any], proposal: Mapping[str, Any],
    detail_plan: Mapping[str, Any], *, proposal_seal: str, detail_work_plan_seal: str,
) -> tuple[dict[str, Any], str]:
    payload, slice_seal = _sealed_payload(slice_results, "merge_detail_slice_results")
    if (payload.get("candidate_id") != plan.get("candidate_id")
            or payload.get("candidate_hash") != plan.get("candidate_hash")
            or payload.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
            or payload.get("proposal_hash") != proposal.get("proposal_hash")
            or payload.get("detail_work_plan_hash") != detail_plan.get("work_plan_hash")):
        raise SelectedNavigationError("merge-detail slice results are foreign to the exact source/proposal work plan")
    provenance = json.loads(slice_results._provenance_json)
    if (provenance.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
            or provenance.get("proposal_seal") != proposal_seal
            or provenance.get("detail_work_plan_seal") != detail_work_plan_seal):
        raise SelectedNavigationError("merge-detail slice validator provenance differs from source/proposal work plan")
    groups = payload.get("groups")
    expected_groups = {str(group["new_section_id"]) for group in proposal.get("merge_groups") or []}
    if not isinstance(groups, Mapping) or set(groups) != expected_groups:
        raise SelectedNavigationError("merge-detail slice validator omitted or added a proposed group")
    for group_id, group_result in groups.items():
        nodes = group_result.get("slice_nodes") if isinstance(group_result, Mapping) else None
        if not isinstance(nodes, list):
            raise SelectedNavigationError(f"merge-detail slice validator has no exact nodes for {group_id}")
        for node in nodes:
            if not isinstance(node, Mapping):
                raise SelectedNavigationError(f"merge-detail slice validator has a malformed node for {group_id}")
            _validate_detail_node(node, plan, proposal, str(group_id))
    return payload, slice_seal


def _validated_ready_detail_node(
    node: Any, plan: Mapping[str, Any], proposal: Mapping[str, Any], group_id: str,
    slice_payload: Mapping[str, Any], *, proposal_seal: str, detail_work_plan_seal: str,
) -> dict[str, Any]:
    """Accept exact sealed slice output or an opaque validator-issued reducer node."""
    if isinstance(node, _SealedArtifact):
        payload, _node_seal = _sealed_payload(node, "merge_detail_reduction_node")
        provenance = json.loads(node._provenance_json)
        if (provenance.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
                or provenance.get("proposal_seal") != proposal_seal
                or provenance.get("detail_work_plan_seal") != detail_work_plan_seal
                or not isinstance(provenance.get("request_hash"), str)
                or not isinstance(provenance.get("response_hash"), str)):
            raise SelectedNavigationError("merge-detail reducer node lacks matching prior validator provenance")
        if provenance.get("child_hashes") != payload.get("child_hashes"):
            raise SelectedNavigationError("merge-detail reducer node provenance differs from validated children")
    elif isinstance(node, Mapping):
        payload = copy.deepcopy(dict(node))
        group_results = slice_payload.get("groups")
        group_result = group_results.get(group_id) if isinstance(group_results, Mapping) else None
        slice_nodes = group_result.get("slice_nodes") if isinstance(group_result, Mapping) else None
        authoritative = next((row for row in slice_nodes or [] if row.get("node_id") == payload.get("node_id")), None)
        if authoritative is None or payload != authoritative:
            raise SelectedNavigationError(
                "merge-detail node is not the exact validated slice or a prior sealed validator result"
            )
    else:
        raise SelectedNavigationError("merge-detail node must come from a sealed slice or prior validator")
    _validate_detail_node(payload, plan, proposal, group_id)
    return payload


def _detail_tree_level(plan_payload: Mapping[str, Any], group_id: str, level: int) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    trees = plan_payload.get("adjudication_trees")
    if not isinstance(trees, Mapping) or group_id not in trees:
        raise SelectedNavigationError(f"merge-detail plan has no tree for group {group_id}")
    tree = trees[group_id]
    levels = tree.get("levels")
    if type(level) is not int or level < 0 or level >= len(levels):
        raise SelectedNavigationError(f"unknown merge-detail reduction level {level} for {group_id}")
    return tree, levels[level]


def build_merge_detail_reduction_requests(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_plan: Mapping[str, Any],
    slice_results: Mapping[str, Any], group_id: str, level: int,
    ready_nodes: Sequence[Mapping[str, Any]], *, detail_index: Mapping[str, Any],
    prompt_authority: PromptAuthority, route_estimator: RouteEstimator,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any], input_cap: int,
) -> dict[str, Any]:
    """Build one measured reduction level from exact READY slice/reduction children."""
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    work, work_seal = _validated_detail_work_plan(detail_plan)
    slices, slices_seal = _sealed_payload(slice_results, "merge_detail_slice_results")
    if (work.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
            or work.get("proposal_seal") != proposal_seal
            or slices.get("detail_work_plan_hash") != work.get("work_plan_hash")
            or slices.get("proposal_hash") != proposal_payload.get("proposal_hash")):
        raise SelectedNavigationError("merge-detail reducer inputs are not the exact READY slice set")
    verified_slices, _slice_seal = _validated_slice_result_payload(
        slice_results, plan, proposal_payload, work,
        proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
    )
    verified_items = validate_source_detail_index(plan, detail_index)
    if detail_index.get("index_hash") != work.get("detail_index_hash"):
        raise SelectedNavigationError("merge-detail reducer source-detail index changed")
    _tree, level_plan = _detail_tree_level(work, group_id, level)
    expected_input_ids = list(level_plan["input_node_ids"])
    ready_by_id: dict[str, dict[str, Any]] = {}
    for raw_node in ready_nodes:
        node = _validated_ready_detail_node(
            raw_node, plan, proposal_payload, group_id, verified_slices,
            proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
        )
        node_id = str(node.get("node_id") or "")
        if node_id in ready_by_id:
            raise SelectedNavigationError("merge-detail reducer has duplicate READY child nodes")
        ready_by_id[node_id] = node
    if len(ready_by_id) != len(ready_nodes) or set(ready_by_id) != set(expected_input_ids):
        raise SelectedNavigationError("merge-detail reducer does not have the exact READY child level")
    rows_by_id = {str(row["detail_id"]): row for row in verified_items}
    requests: list[dict[str, Any]] = []
    for reducer in level_plan["reducers"]:
        children = [ready_by_id[node_id] for node_id in reducer["child_node_ids"]]
        left_ids, right_ids = [list(row["detail_item_ids"]) for row in children]
        if set(left_ids) & set(right_ids) or left_ids + right_ids != reducer["detail_item_ids"]:
            raise SelectedNavigationError("merge-detail reducer children are not an exact disjoint source partition")
        detail_hashes = {**children[0]["detail_item_hashes"], **children[1]["detail_item_hashes"]}
        critical = [*children[0]["critical_literal_bindings"], *children[1]["critical_literal_bindings"]]
        if critical != reducer["critical_literal_bindings"]:
            raise SelectedNavigationError("merge-detail reducer critical literal assignment changed")
        if critical != _critical_literal_bindings([rows_by_id[item] for item in reducer["detail_item_ids"]]):
            raise SelectedNavigationError("merge-detail reducer critical literals differ from exact source index")
        bindings_hash = compute_v3_hash(critical)
        body = {
            "schema_version": DETAIL_REDUCE_REQUEST_SCHEMA,
            "stage": "source_safe_detail_reduction",
            "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
            "proposal_hash": proposal_payload["proposal_hash"], "group_id": group_id,
            "node_id": reducer["node_id"], "level": level,
            "child_node_ids": list(reducer["child_node_ids"]),
            "child_hashes": [str(row["node_hash"]) for row in children],
            "child_nodes": [
                {key: copy.deepcopy(row[key]) for key in (
                    "node_id", "detail_item_ids", "detail_item_hashes", "critical_literal_bindings_hash",
                    "assessments", "disposition", "summary", "node_hash",
                ) if key in row}
                for row in children
            ],
            "detail_item_ids": list(reducer["detail_item_ids"]),
            "detail_item_hashes": detail_hashes,
            "critical_literal_bindings": critical,
            "critical_literal_bindings_hash": bindings_hash,
            "response_schema": DETAIL_REDUCE_RESPONSE_SCHEMA,
        }
        summary_tokens, _skeleton_tokens = _derive_text_bound(
            DETAIL_KIND,
            lambda text, body=body: _detail_reduction_response_shape(body, len(text.split())),
            output_estimator=output_estimator, context_reserve=context_reserve,
            upper=int(plan["bounds"]["Tproposal"]),
        )
        body["max_summary_tokens"] = summary_tokens
        request, measurement = _estimate_request(
            DETAIL_KIND, reducer["node_id"], body, prompt_authority=prompt_authority,
            route_estimator=route_estimator, context_reserve=context_reserve, input_cap=input_cap,
        )
        request_record = {
            "request_id": reducer["node_id"], "group_id": group_id, "level": level,
            "child_node_ids": list(reducer["child_node_ids"]),
            "detail_item_ids": list(reducer["detail_item_ids"]),
            "body": body, "request": request, "request_hash": measurement["request_hash"],
            "measurement": measurement,
        }
        _attach_output_reserve(
            plan, DETAIL_KIND, request_record,
            output_estimator=output_estimator, context_reserve=context_reserve,
        )
        requests.append(request_record)
    carry_ids = [level_plan["odd_carry_node_id"]] if level_plan.get("odd_carry_node_id") else []
    return {"group_id": group_id, "level": level, "requests": requests,
            "carry_node_ids": carry_ids, "output_node_ids": list(level_plan["output_node_ids"]),
            "plan_seal": work_seal, "slice_results_seal": slices_seal}


def validate_merge_detail_reduction_response(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_plan: Mapping[str, Any],
    slice_results: Mapping[str, Any], request: Mapping[str, Any], response: Mapping[str, Any],
    input_nodes: Sequence[Mapping[str, Any]], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> _SealedArtifact:
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    work, work_seal = _validated_detail_work_plan(detail_plan)
    reserve = _verify_output_reserve(
        plan, DETAIL_KIND, request,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    request_hash = reserve["request_hash"]
    if work.get("plan_integrity_hash") != plan.get("plan_integrity_hash") or work.get("proposal_seal") != proposal_seal:
        raise SelectedNavigationError("merge-detail reduction request is foreign to plan/proposal")
    verified_slices, _slice_seal = _validated_slice_result_payload(
        slice_results, plan, proposal_payload, work,
        proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
    )
    group_id = str(request.get("group_id") or "")
    if response.get("schema_version") != DETAIL_REDUCE_RESPONSE_SCHEMA:
        raise SelectedNavigationError("merge-detail slice output cannot adjudicate a group")
    expected_keys = {
        "schema_version", "candidate_id", "candidate_hash", "proposal_hash", "group_id", "node_id", "level",
        "child_node_ids", "child_hashes", "detail_item_ids", "detail_item_hashes",
        "critical_literal_bindings_hash", "disposition", "summary",
    }
    if set(response) != expected_keys:
        raise SelectedNavigationError("merge-detail reduction response has unknown or missing fields")
    _tree, level_plan = _detail_tree_level(work, group_id, int(request.get("level", -1)))
    reducer = next((row for row in level_plan["reducers"] if row["node_id"] == request.get("request_id")), None)
    if reducer is None:
        raise SelectedNavigationError("merge-detail reducer is not a declared deterministic tree node")
    children_by_id: dict[str, dict[str, Any]] = {}
    for raw_node in input_nodes:
        node = _validated_ready_detail_node(
            raw_node, plan, proposal_payload, group_id, verified_slices,
            proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
        )
        node_id = str(node.get("node_id") or "")
        if node_id in children_by_id:
            raise SelectedNavigationError("merge-detail reducer has duplicate READY children")
        children_by_id[node_id] = node
    child_ids = list(reducer["child_node_ids"])
    if len(children_by_id) != len(input_nodes) or set(children_by_id) != set(child_ids):
        raise SelectedNavigationError("merge-detail reducer lacks its exact READY children")
    children = [children_by_id[node_id] for node_id in child_ids]
    detail_ids = [item for child in children for item in child["detail_item_ids"]]
    if len(detail_ids) != len(set(detail_ids)) or detail_ids != reducer["detail_item_ids"]:
        raise SelectedNavigationError("merge-detail reducer child coverage is not exact and disjoint")
    detail_hashes = {**children[0]["detail_item_hashes"], **children[1]["detail_item_hashes"]}
    bindings = [*children[0]["critical_literal_bindings"], *children[1]["critical_literal_bindings"]]
    bindings_hash = compute_v3_hash(bindings)
    expected_child_nodes = [
        {key: copy.deepcopy(row[key]) for key in (
            "node_id", "detail_item_ids", "detail_item_hashes", "critical_literal_bindings_hash",
            "assessments", "disposition", "summary", "node_hash",
        )}
        for row in children
    ]
    expected_request_body = {
        "schema_version": DETAIL_REDUCE_REQUEST_SCHEMA,
        "stage": "source_safe_detail_reduction",
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "proposal_hash": proposal_payload["proposal_hash"], "group_id": group_id,
        "node_id": reducer["node_id"], "level": int(request.get("level", -1)),
        "child_node_ids": child_ids, "child_hashes": [row["node_hash"] for row in children],
        "child_nodes": expected_child_nodes,
        "detail_item_ids": detail_ids, "detail_item_hashes": detail_hashes,
        "critical_literal_bindings": bindings,
        "critical_literal_bindings_hash": bindings_hash,
        "response_schema": DETAIL_REDUCE_RESPONSE_SCHEMA,
        "max_summary_tokens": (request.get("body") or {}).get("max_summary_tokens"),
    }
    if request.get("body") != expected_request_body:
        raise SelectedNavigationError("merge-detail reduction request differs from its exact validator-backed children")
    for key, expected in (
        ("candidate_id", plan["candidate_id"]), ("candidate_hash", plan["candidate_hash"]),
        ("proposal_hash", proposal_payload["proposal_hash"]), ("group_id", group_id),
        ("node_id", request["request_id"]), ("level", request["level"]),
        ("child_node_ids", child_ids), ("child_hashes", [row["node_hash"] for row in children]),
        ("detail_item_ids", detail_ids), ("detail_item_hashes", detail_hashes),
        ("critical_literal_bindings_hash", bindings_hash),
    ):
        if response.get(key) != expected:
            raise SelectedNavigationError(f"merge-detail reduction response changed {key}")
    disposition = response.get("disposition")
    child_dispositions = {str(row["disposition"]) for row in children}
    if disposition not in {"compatible", "conflict", "uncertain"}:
        raise SelectedNavigationError("merge-detail reduction has an invalid disposition")
    if ("conflict" in child_dispositions and disposition == "compatible") or (
        "uncertain" in child_dispositions and disposition == "compatible"
    ):
        raise SelectedNavigationError("merge-detail reduction erased an adverse or uncertain child")
    summary = _nonempty(response.get("summary"), "merge-detail reduction summary")
    if output_estimator(DETAIL_KIND, {"summary": summary}) > int(request["body"]["max_summary_tokens"]):
        raise SelectedNavigationError("merge-detail reduction summary exceeds its measured text allowance")
    _verify_response_within_reserve(
        DETAIL_KIND, response, reserve,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    assessments = {detail_id: value for child in children for detail_id, value in child["assessments"].items()}
    node = {
        "schema_version": DETAIL_REDUCTION_NODE_SCHEMA,
        "node_id": reducer["node_id"], "candidate_id": plan["candidate_id"],
        "candidate_hash": plan["candidate_hash"], "proposal_hash": proposal_payload["proposal_hash"],
        "group_id": group_id, "level": int(request["level"]),
        "detail_item_ids": detail_ids, "detail_item_hashes": detail_hashes,
        "critical_literal_bindings": bindings, "critical_literal_bindings_hash": bindings_hash,
        "assessments": assessments, "disposition": disposition, "summary": summary,
        "child_node_ids": child_ids, "child_hashes": [row["node_hash"] for row in children],
    }
    node["node_hash"] = compute_v3_hash(node)
    return _SealedArtifact("merge_detail_reduction_node", node, {
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
        "detail_work_plan_seal": work_seal, "request_hash": request_hash,
        "child_hashes": [row["node_hash"] for row in children], "response_hash": _hash(response),
    }, _token=_SEAL_TOKEN)


def _final_adjudication_response_shape(body: Mapping[str, Any], contribution_tokens: int) -> dict[str, Any]:
    root = body["detail_root"]
    return {
        "schema_version": DETAIL_RESPONSE_SCHEMA,
        "candidate_id": body["candidate_id"], "candidate_hash": body["candidate_hash"],
        "proposal_hash": body["proposal_hash"], "group_id": body["group_id"],
        "detail_root_hash": root["node_hash"],
        "detail_item_ids": list(root["detail_item_ids"]),
        "detail_item_hashes": copy.deepcopy(root["detail_item_hashes"]),
        "critical_literal_bindings_hash": root["critical_literal_bindings_hash"],
        "decision": "accept",
        "source_contributions": {
            section_id: _token_placeholder(contribution_tokens)
            for section_id in body["source_section_ids"]
        },
    }


def _layout_reduction_response_shape(
    plan: Mapping[str, Any], body: Mapping[str, Any], expression_tokens: int,
) -> dict[str, Any]:
    span_ids = _string_list(body.get("expected_span_ids"), "layout reducer expected_span_ids")
    manifest = {str(row["span_id"]): row for row in plan.get("intent_span_manifest", [])}
    if set(span_ids) - set(manifest):
        raise SelectedNavigationError("layout reducer response shape has foreign source spans")
    layout_by_section = []
    intent_layout = []
    for scope in plan.get("section_scope", []):
        assigned = [span_id for span_id in span_ids if manifest[span_id].get("section_id") == scope["section_id"]]
        if assigned:
            layout_by_section.append({
                "section_id": scope["section_id"],
                "section_hash": scope["section_hash"],
                "position": scope["position"],
                "task_ids": list(scope["task_ids"]),
                "hash_aliases": list(scope["hash_aliases"]),
                "span_ids": assigned,
                "source_fields": [str(manifest[span_id]["source_field"]) for span_id in assigned],
                "expression": _token_placeholder(expression_tokens),
            })
    for field in plan.get("intent_field_names", []):
        assigned = [
            span_id for span_id in span_ids
            if manifest[span_id].get("section_id") is None
            and manifest[span_id].get("source_field") == f"intent.{field}"
        ]
        if assigned:
            intent_layout.append({
                "source_field": field,
                "source_hash": plan["intent_field_hashes"][field],
                "span_ids": assigned,
                "expression": _token_placeholder(expression_tokens),
            })
    return {
        "schema_version": REDUCE_RESPONSE_SCHEMA,
        "candidate_id": body["candidate_id"],
        "candidate_hash": body["candidate_hash"],
        "node_id": body["node_id"],
        "child_node_ids": copy.deepcopy(list(body["child_node_ids"])),
        "child_hashes": copy.deepcopy(list(body["child_hashes"])),
        "span_ids": span_ids,
        "layout_by_section": layout_by_section,
        "intent_layout": intent_layout,
    }


def _global_navigation_response_shape(
    plan: Mapping[str, Any], body: Mapping[str, Any], proposal_tokens: int,
) -> dict[str, Any]:
    scopes = list(body.get("section_scope") or [])
    # Pair the longest actual IDs first. Valid proposals may merge any source
    # sections, so this is the largest permitted repeated group envelope.
    ordered = sorted(
        scopes,
        key=lambda row: (-len(_json_bytes(str(row["section_id"]))), str(row["section_id"])),
    )
    groups = []
    for index in range(0, len(ordered) - 1, 2):
        pair = ordered[index:index + 2]
        ids = [str(row["section_id"]) for row in pair]
        groups.append({
            "source_section_ids": ids,
            "source_section_hashes": {str(row["section_id"]): str(row["section_hash"]) for row in pair},
            "title": _token_placeholder(proposal_tokens),
            "goal": _token_placeholder(proposal_tokens),
            "integration_reason": _token_placeholder(proposal_tokens),
        })
    return {
        "schema_version": NAV_RESPONSE_SCHEMA,
        "candidate_id": body["candidate_id"],
        "candidate_hash": body["candidate_hash"],
        "root_node_id": body["root_node_id"],
        "root_hash": body["root_hash"],
        "plan_hash": body["plan_hash"],
        "span_manifest_hash": body["span_manifest_hash"],
        "section_order": [str(row["section_id"]) for row in scopes],
        "merge_groups": groups,
    }


def _detail_reduction_response_shape(body: Mapping[str, Any], summary_tokens: int) -> dict[str, Any]:
    return {
        "schema_version": DETAIL_REDUCE_RESPONSE_SCHEMA,
        "candidate_id": body["candidate_id"],
        "candidate_hash": body["candidate_hash"],
        "proposal_hash": body["proposal_hash"],
        "group_id": body["group_id"],
        "node_id": body["node_id"],
        "level": body["level"],
        "child_node_ids": copy.deepcopy(list(body["child_node_ids"])),
        "child_hashes": copy.deepcopy(list(body["child_hashes"])),
        "detail_item_ids": copy.deepcopy(list(body["detail_item_ids"])),
        "detail_item_hashes": copy.deepcopy(dict(body["detail_item_hashes"])),
        "critical_literal_bindings_hash": body["critical_literal_bindings_hash"],
        "disposition": "compatible",
        "summary": _token_placeholder(summary_tokens),
    }


def merge_detail_reduction_response_shape(request_record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact bounded output shape for one measured detail reducer request."""
    body = request_record.get("body", request_record)
    if not isinstance(body, Mapping) or body.get("schema_version") != DETAIL_REDUCE_REQUEST_SCHEMA:
        raise SelectedNavigationError("merge-detail reducer response shape requires its exact request body")
    summary_tokens = _integer(body.get("max_summary_tokens"), "merge-detail reducer max_summary_tokens", minimum=1)
    return _detail_reduction_response_shape(body, summary_tokens)


def _request_response_shape(
    plan: Mapping[str, Any], kind: str, request_record: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    body = request_record.get("body")
    if not isinstance(body, Mapping):
        raise SelectedNavigationError(f"{kind} request record has no exact body for output measurement")
    text_bounds: dict[str, Any] = {}
    if kind == EXTRACT_KIND and body.get("schema_version") == EXTRACT_REQUEST_SCHEMA:
        span = body.get("span")
        if not isinstance(span, Mapping):
            raise SelectedNavigationError("layout extraction output reserve lacks exact source span")
        bound = _integer(body.get("max_expression_tokens"), "layout extraction max_expression_tokens", minimum=1)
        if bound > int(plan["bounds"]["Tfrag"]):
            raise SelectedNavigationError("layout extraction request exceeds its plan Tfrag bound")
        schema = EXTRACT_RESPONSE_SCHEMA
        shape = _layout_extract_response_shape(
            str(body["candidate_id"]), str(body["candidate_hash"]), span, bound,
        )
        text_bounds = {"expression": {"tokens": bound, "plan_bound": "Tfrag"}}
    elif kind == REDUCE_KIND and body.get("schema_version") == REDUCE_REQUEST_SCHEMA:
        bound = _integer(body.get("expression_token_bound"), "layout reducer expression_token_bound", minimum=1)
        if bound != int(plan["bounds"]["Tnav"]):
            raise SelectedNavigationError("layout reducer request differs from its plan Tnav bound")
        schema = REDUCE_RESPONSE_SCHEMA
        shape = _layout_reduction_response_shape(plan, body, bound)
        text_bounds = {
            "layout_by_section[].expression": {"tokens": bound, "plan_bound": "Tnav"},
            "intent_layout[].expression": {"tokens": bound, "plan_bound": "Tnav"},
        }
    elif kind == NAV_KIND and body.get("schema_version") == NAV_REQUEST_SCHEMA:
        bound = _integer(body.get("merge_proposal_text_budget_tokens"), "global navigation proposal text bound", minimum=1)
        if bound != int(plan["bounds"]["Tproposal"]):
            raise SelectedNavigationError("global navigation request differs from its plan Tproposal bound")
        schema = NAV_RESPONSE_SCHEMA
        shape = _global_navigation_response_shape(plan, body, bound)
        text_bounds = {
            "merge_groups[].title": {"tokens": bound, "plan_bound": "Tproposal"},
            "merge_groups[].goal": {"tokens": bound, "plan_bound": "Tproposal"},
            "merge_groups[].integration_reason": {"tokens": bound, "plan_bound": "Tproposal"},
        }
    elif kind == DETAIL_KIND and body.get("schema_version") == DETAIL_REQUEST_SCHEMA:
        bound = _integer(body.get("max_summary_tokens"), "detail slice max_summary_tokens", minimum=1)
        if bound > int(plan["bounds"]["Tproposal"]):
            raise SelectedNavigationError("detail slice summary exceeds its plan Tproposal bound")
        schema = DETAIL_SLICE_RESPONSE_SCHEMA
        shape = _detail_slice_response_shape(
            plan, {"new_section_id": body["group_id"]}, body, summary_tokens=bound,
        )
        text_bounds = {"summary": {"tokens": bound, "plan_bound": "Tproposal"}}
    elif kind == DETAIL_KIND and body.get("schema_version") == DETAIL_REDUCE_REQUEST_SCHEMA:
        bound = _integer(body.get("max_summary_tokens"), "detail reducer max_summary_tokens", minimum=1)
        if bound > int(plan["bounds"]["Tproposal"]):
            raise SelectedNavigationError("detail reducer summary exceeds its plan Tproposal bound")
        schema = DETAIL_REDUCE_RESPONSE_SCHEMA
        shape = merge_detail_reduction_response_shape(request_record)
        text_bounds = {"summary": {"tokens": bound, "plan_bound": "Tproposal"}}
    elif kind == DETAIL_KIND and body.get("schema_version") == DETAIL_FINAL_REQUEST_SCHEMA:
        bound = _integer(body.get("max_contribution_tokens"), "final contribution token bound", minimum=1)
        if bound > int(plan["bounds"]["Tproposal"]):
            raise SelectedNavigationError("final source contribution exceeds its plan Tproposal bound")
        schema = DETAIL_RESPONSE_SCHEMA
        shape = _final_adjudication_response_shape(body, bound)
        text_bounds = {
            f"source_contributions[{section_id}]": {"tokens": bound, "plan_bound": "Tproposal"}
            for section_id in body["source_section_ids"]
        }
    else:
        raise SelectedNavigationError(f"{kind} request has no supported complete response schema")
    if body.get("response_schema") != schema:
        raise SelectedNavigationError(f"{kind} request response_schema differs from its complete output shape")
    return schema, shape, text_bounds


def _output_reserve_for_shape(
    kind: str, response_schema: str, shape: Mapping[str, Any], text_bounds: Mapping[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> dict[str, Any]:
    context = _context_for(context_reserve, kind)
    estimated = _estimate_output(
        kind, shape, output_estimator=output_estimator, context_reserve=context_reserve,
    )
    reserve = {
        "response_schema": response_schema,
        "response_shape_hash": _hash(shape),
        "estimated_output_tokens": estimated,
        "max_output_tokens": context["max_output_tokens"],
        "requested_output_tokens": estimated,
        "route_identity": context["route_identity"],
        "bound_text_fields": copy.deepcopy(dict(text_bounds)),
    }
    if "summary" in text_bounds:
        reserve["max_summary_tokens"] = text_bounds["summary"]["tokens"]
    contribution_fields = [
        value for key, value in text_bounds.items() if key.startswith("source_contributions[")
    ]
    if contribution_fields:
        reserve["max_contribution_tokens"] = contribution_fields[0]["tokens"]
    return reserve


def _measure_navigation_output_reserve(
    plan: Mapping[str, Any], kind: str, request_record: Mapping[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure the complete request-specific response schema before provider dispatch."""
    request_hash = _verify_measured_request(request_record, f"{kind} output reserve request")
    response_schema, shape, text_bounds = _request_response_shape(plan, kind, request_record)
    reserve = _output_reserve_for_shape(
        kind, response_schema, shape, text_bounds,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    reserve["request_id"] = str(request_record.get("request_id") or "")
    reserve["request_hash"] = request_hash
    return reserve


def measure_navigation_output_reserve(
    plan: Mapping[str, Any], kind: str, request_record: Mapping[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure the complete request-specific response schema before provider dispatch."""
    _validate_plan_integrity(plan)
    context = _context_for(context_reserve, kind)
    measurement = request_record.get("measurement", request_record.get("estimate"))
    if not isinstance(measurement, Mapping):
        raise SelectedNavigationError(f"{kind} request output reserve lacks its measured route profile")
    for field in (
        "route_identity", "max_output_tokens", "verified_context_limit",
        "reasoning_reserve", "safety_margin",
    ):
        if measurement.get(field) != context[field]:
            raise SelectedNavigationError(f"{kind} request output reserve route profile changed at {field}")
    return _measure_navigation_output_reserve(
        plan, kind, request_record,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )


def _attach_output_reserve(
    plan: Mapping[str, Any], kind: str, request_record: dict[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> dict[str, Any]:
    reserve = measure_navigation_output_reserve(
        plan, kind, request_record,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    measurement = request_record.get("measurement")
    if not isinstance(measurement, dict):
        raise SelectedNavigationError(f"{kind} request record has no mutable measurement")
    measurement["output_reserve"] = reserve
    estimate = request_record.get("estimate")
    if isinstance(estimate, dict):
        estimate["output_reserve"] = copy.deepcopy(reserve)
    return reserve


def _verify_output_reserve(
    plan: Mapping[str, Any], kind: str, request_record: Mapping[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
    expected_reserve: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    actual = measure_navigation_output_reserve(
        plan, kind, request_record,
        output_estimator=output_estimator, context_reserve=context_reserve,
    )
    measurement = request_record.get("measurement", request_record.get("estimate"))
    stored = measurement.get("output_reserve") if isinstance(measurement, Mapping) else None
    if not isinstance(stored, Mapping) or dict(stored) != actual:
        raise SelectedNavigationError(f"{kind} request output reserve differs from its complete response schema")
    if expected_reserve is not None and dict(expected_reserve) != actual:
        raise SelectedNavigationError(f"{kind} actual request differs from its sealed output reserve")
    return actual


def _verify_response_within_reserve(
    kind: str, response: Mapping[str, Any], reserve: Mapping[str, Any], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
) -> None:
    actual = _estimate_output(
        kind, response, output_estimator=output_estimator, context_reserve=context_reserve,
    )
    if actual > int(reserve["estimated_output_tokens"]):
        raise SelectedNavigationError(f"{kind} response exceeds its pre-POST complete-schema output reserve")


def build_merge_group_adjudication_requests(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_plan: Mapping[str, Any],
    slice_results: Mapping[str, Any], reduction_roots: Mapping[str, Mapping[str, Any]],
    layout_root: Mapping[str, Any], detail_index: Mapping[str, Any], *,
    prompt_authority: PromptAuthority, route_estimator: RouteEstimator,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any], input_cap: int,
) -> _SealedArtifact:
    """Build one complete-group adjudication request only after all detail roots are READY."""
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    work, work_seal = _validated_detail_work_plan(detail_plan)
    slices, slices_seal = _sealed_payload(slice_results, "merge_detail_slice_results")
    if work.get("plan_integrity_hash") != plan.get("plan_integrity_hash") or work.get("proposal_seal") != proposal_seal:
        raise SelectedNavigationError("final merge adjudication work plan is foreign")
    if slices.get("detail_work_plan_hash") != work.get("work_plan_hash") or slices.get("proposal_hash") != proposal_payload.get("proposal_hash"):
        raise SelectedNavigationError("final merge adjudication lacks the validated complete slice set")
    _validate_layout_node(plan, layout_root)
    intent_by_field = {str(row["source_field"]): row for row in layout_root.get("intent_layout", [])}
    nonempty_fields = set(plan["intent_field_names"]) - set(plan["empty_intent_fields"])
    if set(intent_by_field) != nonempty_fields:
        raise SelectedNavigationError("final merge adjudication lacks the complete bounded intent view")
    intent_layout = []
    for key in plan["intent_field_names"]:
        if key in plan["empty_intent_fields"]:
            intent_layout.append({"source_field": key, "source_hash": plan["intent_field_hashes"][key],
                                  "span_ids": [], "expression": "(empty source field)"})
        else:
            intent_layout.append(copy.deepcopy(intent_by_field[key]))
    verified_items = validate_source_detail_index(plan, detail_index)
    if detail_index.get("index_hash") != work.get("detail_index_hash"):
        raise SelectedNavigationError("final merge adjudication source-detail index changed")
    verified_slices, slices_seal = _validated_slice_result_payload(
        slice_results, plan, proposal_payload, work,
        proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
    )
    roots = dict(reduction_roots)
    if set(roots) != {str(group["new_section_id"]) for group in proposal_payload["merge_groups"]}:
        raise SelectedNavigationError("final merge adjudication lacks exactly one READY detail root per group")
    requests: list[dict[str, Any]] = []
    for group in proposal_payload["merge_groups"]:
        group_id = str(group["new_section_id"])
        root = _validated_ready_detail_node(
            roots[group_id], plan, proposal_payload, group_id, verified_slices,
            proposal_seal=proposal_seal, detail_work_plan_seal=work_seal,
        )
        tree = work["adjudication_trees"][group_id]
        if root.get("node_id") != tree.get("root_node_id"):
            raise SelectedNavigationError(f"final merge adjudication root for {group_id} is not the planned tree root")
        expected_ids = {str(item["detail_id"]) for item in verified_items
                        if item.get("section_id") in set(group["source_section_ids"])}
        if set(root["detail_item_ids"]) != expected_ids or len(root["detail_item_ids"]) != len(expected_ids):
            raise SelectedNavigationError(f"final merge adjudication root for {group_id} has incomplete source closure")
        body = {
            "schema_version": DETAIL_FINAL_REQUEST_SCHEMA,
            "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
            "proposal_hash": proposal_payload["proposal_hash"], "group_id": group_id,
            "source_section_ids": list(group["source_section_ids"]),
            "source_section_hashes": copy.deepcopy(dict(group["source_section_hashes"])),
            "proposed_title": group["title"], "proposed_goal": group["goal"],
            "proposed_integration_reason": group["integration_reason"],
            "intent_fields_hash": plan["intent_fields_hash"], "intent_layout": intent_layout,
            "layout_root_hash": layout_root.get("node_hash"),
            "detail_root": {key: copy.deepcopy(root[key]) for key in (
                "node_id", "node_hash", "detail_item_ids", "detail_item_hashes",
                "critical_literal_bindings", "critical_literal_bindings_hash",
                "assessments", "disposition", "summary",
            )},
            "response_schema": DETAIL_RESPONSE_SCHEMA,
        }
        contribution_tokens, _ = _derive_text_bound(
            DETAIL_KIND,
            lambda text, body=body: _final_adjudication_response_shape(body, len(text.split())),
            output_estimator=output_estimator, context_reserve=context_reserve,
            upper=int(plan["bounds"]["Tproposal"]),
        )
        # Derive once more with the exact admitted text count so the recorded
        # reserve is the complete response schema at the actual per-source cap.
        response_shape = _final_adjudication_response_shape(body, contribution_tokens)
        body["max_contribution_tokens"] = contribution_tokens
        detail_context = _context_for(context_reserve, DETAIL_KIND)
        request_id = _request_id(DETAIL_KIND, {
            "candidate_hash": plan["candidate_hash"], "proposal_hash": proposal_payload["proposal_hash"],
            "group_id": group_id, "detail_root_hash": root["node_hash"],
            "layout_root_hash": layout_root["node_hash"],
            "response_shape_hash": _hash(response_shape),
            "max_contribution_tokens": contribution_tokens,
            "max_output_tokens": detail_context["max_output_tokens"],
            "route_identity": detail_context["route_identity"],
        })
        request, measurement = _estimate_request(
            DETAIL_KIND, request_id, body, prompt_authority=prompt_authority,
            route_estimator=route_estimator, context_reserve=context_reserve, input_cap=input_cap,
        )
        request_record = {
            "request_id": request_id, "group_id": group_id,
            "body": body, "request": request,
            "request_hash": measurement["request_hash"], "measurement": measurement,
            "detail_root_hash": root["node_hash"],
        }
        _attach_output_reserve(
            plan, DETAIL_KIND, request_record,
            output_estimator=output_estimator, context_reserve=context_reserve,
        )
        requests.append(request_record)
    if work["counts"]["Qdetail_final"] != len(requests):
        raise SelectedNavigationError("final merge adjudication calls differ from Emax work plan")
    payload = {
        "schema_version": "selected_candidate_merge_group_adjudication_plan/v1",
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_hash": proposal_payload["proposal_hash"],
        "proposal_seal": proposal_seal, "detail_work_plan_hash": work["work_plan_hash"],
        "slice_results_seal": slices_seal, "layout_root_hash": layout_root["node_hash"],
        "requests": requests, "counts": {"Qdetail_final": len(requests)},
        "detail_work_plan_seal": work_seal,
    }
    payload["final_plan_hash"] = compute_v3_hash(payload)
    return _SealedArtifact("merge_group_adjudication_plan", payload, {
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
        "detail_work_plan_seal": work_seal, "slice_results_seal": slices_seal,
        "layout_root_hash": layout_root["node_hash"],
        "request_hashes": {row["request_id"]: row["request_hash"] for row in requests},
    }, _token=_SEAL_TOKEN)


def validate_merge_group_adjudication_responses(
    plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_plan: Mapping[str, Any],
    final_plan: Mapping[str, Any], responses: Sequence[Mapping[str, Any]], *,
    output_estimator: OutputEstimator, context_reserve: Mapping[str, Any],
    actual_requests: Sequence[Mapping[str, Any]] | None = None,
) -> _SealedArtifact:
    """Issue group-level decisions only from measured finalizers over complete roots."""
    _validate_plan_integrity(plan)
    proposal_payload, proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    work, work_seal = _validated_detail_work_plan(detail_plan)
    final, final_seal = _sealed_payload(final_plan, "merge_group_adjudication_plan")
    if (final.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
            or final.get("proposal_seal") != proposal_seal
            or final.get("detail_work_plan_seal") != work_seal):
        raise SelectedNavigationError("final merge adjudications are foreign to source/proposal work plan")
    expected_requests = list(final.get("requests") or [])
    requests = list(actual_requests) if actual_requests is not None else copy.deepcopy(expected_requests)
    expected_by_id = {str(row["request_id"]): row for row in expected_requests}
    actual_by_id = {str(row.get("request_id") or ""): row for row in requests}
    if len(actual_by_id) != len(requests) or set(actual_by_id) != set(expected_by_id):
        raise SelectedNavigationError("final merge adjudication requests are missing, duplicate, or foreign")
    response_by_id = {str(row.get("request_id") or ""): row for row in responses if isinstance(row, Mapping)}
    if len(response_by_id) != len(responses) or set(response_by_id) != set(expected_by_id):
        raise SelectedNavigationError("final merge adjudication responses are missing, duplicate, or foreign")
    groups: dict[str, dict[str, Any]] = {}
    request_hashes: dict[str, str] = {}
    response_hashes: dict[str, str] = {}
    for request_id, planned in expected_by_id.items():
        request = actual_by_id[request_id]
        planned_output_reserve = (planned.get("measurement") or {}).get("output_reserve")
        if not isinstance(planned_output_reserve, Mapping):
            raise SelectedNavigationError(f"final merge adjudication {request_id} lacks its pre-POST output reserve")
        reserve = _verify_output_reserve(
            plan, DETAIL_KIND, request,
            output_estimator=output_estimator, context_reserve=context_reserve,
            expected_reserve=planned_output_reserve,
        )
        request_hash = reserve["request_hash"]
        if request_hash != planned.get("request_hash") or request.get("body") != planned.get("body"):
            raise SelectedNavigationError(f"final merge adjudication {request_id} changed its measured request")
        body = planned["body"]
        response = response_by_id[request_id].get("response")
        if not isinstance(response, Mapping) or response.get("schema_version") != DETAIL_RESPONSE_SCHEMA:
            raise SelectedNavigationError("only a complete-group finalizer may return merge acceptance")
        expected_keys = {
            "schema_version", "candidate_id", "candidate_hash", "proposal_hash", "group_id",
            "detail_root_hash", "detail_item_ids", "detail_item_hashes", "critical_literal_bindings_hash",
            "decision", "source_contributions",
        }
        if set(response) != expected_keys:
            raise SelectedNavigationError(f"final adjudication {request_id} has unknown or missing fields")
        root = body["detail_root"]
        for key, expected_value in (
            ("candidate_id", plan["candidate_id"]), ("candidate_hash", plan["candidate_hash"]),
            ("proposal_hash", proposal_payload["proposal_hash"]), ("group_id", planned["group_id"]),
            ("detail_root_hash", planned["detail_root_hash"]),
            ("detail_item_ids", root["detail_item_ids"]),
            ("detail_item_hashes", root["detail_item_hashes"]),
            ("critical_literal_bindings_hash", root["critical_literal_bindings_hash"]),
        ):
            if response.get(key) != expected_value:
                raise SelectedNavigationError(f"final adjudication {request_id} changed {key}")
        decision = response.get("decision")
        if decision not in {"accept", "reject"}:
            raise SelectedNavigationError(f"final adjudication {request_id} has no accept/reject decision")
        if decision == "accept" and root.get("disposition") != "compatible":
            raise SelectedNavigationError(f"final adjudication {request_id} accepted a conflicted or uncertain detail root")
        group = next(row for row in proposal_payload["merge_groups"] if row["new_section_id"] == planned["group_id"])
        contributions = response.get("source_contributions")
        if not isinstance(contributions, Mapping) or set(contributions) != set(group["source_section_ids"]):
            raise SelectedNavigationError(f"final adjudication {request_id} has incomplete source contributions")
        for value in contributions.values():
            if output_estimator(DETAIL_KIND, {"contribution": value}) > int(body["max_contribution_tokens"]):
                raise SelectedNavigationError(f"final adjudication {request_id} contribution exceeds its complete-schema output reserve")
            _nonempty(value, f"final adjudication {request_id} contribution")
        _verify_response_within_reserve(
            DETAIL_KIND, response, reserve,
            output_estimator=output_estimator, context_reserve=context_reserve,
        )
        groups[planned["group_id"]] = {
            "decision": decision, "contributions": copy.deepcopy(dict(contributions)),
            "detail_item_ids": list(root["detail_item_ids"]),
            "detail_item_hashes": copy.deepcopy(dict(root["detail_item_hashes"])),
            "critical_literal_bindings_hash": root["critical_literal_bindings_hash"],
            "detail_root_hash": root["node_hash"],
        }
        request_hashes[request_id] = request_hash
        response_hashes[request_id] = _hash(response)
    payload = {
        "schema_version": DETAIL_GROUP_RESULTS_SCHEMA,
        "candidate_id": plan["candidate_id"], "candidate_hash": plan["candidate_hash"],
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_hash": proposal_payload["proposal_hash"],
        "detail_work_plan_hash": work["work_plan_hash"], "final_plan_hash": final["final_plan_hash"],
        "groups": groups,
    }
    return _SealedArtifact("merge_group_adjudications", payload, {
        "plan_integrity_hash": plan["plan_integrity_hash"], "proposal_seal": proposal_seal,
        "detail_work_plan_seal": work_seal, "final_plan_seal": final_seal,
        "request_hashes": request_hashes, "response_hashes": response_hashes,
    }, _token=_SEAL_TOKEN)


def compile_section_coordination(plan: Mapping[str, Any], proposal: Mapping[str, Any], detail_results: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compile a source-hash-bound `_apply_section_coordination` input.

    Rejected proposals expand to their original section IDs in source order;
    accepted proposals preserve full original section objects locally and add
    only the existing lossless coordination fields.
    """
    _validate_plan_integrity(plan)
    proposal_payload, _proposal_seal = _sealed_payload(proposal, "navigation_proposal")
    if proposal_payload.get("candidate_id") != plan.get("candidate_id") or proposal_payload.get("candidate_hash") != plan.get("candidate_hash"):
        raise SelectedNavigationError("section coordination proposal is foreign to selected candidate")
    if proposal_payload.get("proposal_hash") != _proposal_hash(proposal_payload):
        raise SelectedNavigationError("section coordination proposal hash changed")
    proposal = proposal_payload
    scopes = {str(row["section_id"]): row for row in plan["section_scope"]}
    scope_order = [str(row["section_id"]) for row in plan["section_scope"]]
    groups = proposal.get("merge_groups") or []
    if groups:
        result_payload, _result_seal = _sealed_payload(detail_results, "merge_group_adjudications")
        if (
            result_payload.get("candidate_id") != plan["candidate_id"]
            or result_payload.get("candidate_hash") != plan["candidate_hash"]
            or result_payload.get("plan_integrity_hash") != plan.get("plan_integrity_hash")
            or result_payload.get("proposal_hash") != proposal.get("proposal_hash")
        ):
            raise SelectedNavigationError("final group adjudications are foreign to plan or proposal")
        results = result_payload.get("groups")
        if not isinstance(results, Mapping) or set(results) != {str(row["new_section_id"]) for row in groups}:
            raise SelectedNavigationError("proposed merges lack complete-group final adjudications")
    else:
        results = {}
    accepted_groups: list[dict[str, Any]] = []
    rejected_expansions: dict[str, list[str]] = {}
    for group in groups:
        group_id = str(group["new_section_id"])
        result = results[group_id]
        source_ids = list(group["source_section_ids"])
        if result.get("decision") == "reject":
            rejected_expansions[group_id] = sorted(source_ids, key=scope_order.index)
            continue
        if result.get("decision") != "accept":
            raise SelectedNavigationError(f"merge group {group_id} has no final detail decision")
        contributions = result.get("contributions")
        if not isinstance(contributions, Mapping) or set(contributions) != set(source_ids):
            raise SelectedNavigationError(f"accepted merge group {group_id} lacks every exact source-intent contribution")
        expected_hashes = {section_id: scopes[section_id]["section_hash"] for section_id in source_ids}
        if result.get("detail_item_ids") is None or not isinstance(result.get("detail_item_hashes"), Mapping):
            raise SelectedNavigationError(f"accepted merge group {group_id} lacks source-detail closure receipt")
        source_intents: dict[str, dict[str, str]] = {}
        for section_id in source_ids:
            original = plan["source_sections"][section_id]
            source_intents[section_id] = {
                "title": str(original.get("title") or ""),
                "goal": str(original.get("goal") or ""),
                "contribution": _nonempty(contributions[section_id], f"accepted contribution for {section_id}"),
            }
        accepted_groups.append({
            "new_section_id": group_id,
            "source_section_ids": source_ids,
            "source_section_hashes": expected_hashes,
            "title": group["title"], "goal": group["goal"],
            "integration_reason": group["integration_reason"],
            "canonical_intent": {"title": group["title"], "goal": group["goal"], "source_intents": source_intents},
        })
    accepted_by_source = {
        section_id: group["new_section_id"]
        for group in accepted_groups for section_id in group["source_section_ids"]
    }
    rejected_by_source = {
        section_id: group["new_section_id"]
        for group in groups if group["new_section_id"] in rejected_expansions
        for section_id in group["source_section_ids"]
    }
    accepted_ids = {row["new_section_id"] for row in accepted_groups}
    order: list[str] = []
    emitted_accepted: set[str] = set()
    for section_id in proposal.get("source_section_order") or []:
        section_id = str(section_id)
        accepted_id = accepted_by_source.get(section_id)
        rejected_id = rejected_by_source.get(section_id)
        if accepted_id is not None:
            if accepted_id not in emitted_accepted:
                order.append(accepted_id)
                emitted_accepted.add(accepted_id)
        elif rejected_id is not None or section_id in scopes:
            order.append(section_id)
        else:
            raise SelectedNavigationError(f"source section order contains foreign ID {section_id}")
    final_ids = (set(scopes) - {sid for group in accepted_groups for sid in group["source_section_ids"]}) | accepted_ids
    if len(order) != len(set(order)) or set(order) != final_ids:
        raise SelectedNavigationError("compiled section_order omits or duplicates a final section")
    return {
        "candidate_id": plan["candidate_id"],
        "parent_hash": plan["candidate_hash"],
        "source_section_hashes": copy.deepcopy(plan["source_section_hashes"]),
        "section_order": order,
        "merge_groups": accepted_groups,
    }


__all__ = [
    "DETAIL_FINAL_REQUEST_SCHEMA",
    "DETAIL_GROUP_RESULTS_SCHEMA",
    "DETAIL_INDEX_SCHEMA",
    "DETAIL_KIND",
    "DETAIL_REDUCE_REQUEST_SCHEMA",
    "DETAIL_REDUCE_RESPONSE_SCHEMA",
    "DETAIL_REDUCTION_NODE_SCHEMA",
    "DETAIL_REQUEST_SCHEMA",
    "DETAIL_RESPONSE_SCHEMA",
    "DETAIL_SLICE_RESPONSE_SCHEMA",
    "DETAIL_SLICE_RESULTS_SCHEMA",
    "DETAIL_WORK_PLAN_SCHEMA",
    "EXTRACT_KIND",
    "EXTRACT_REQUEST_SCHEMA",
    "EXTRACT_RESPONSE_SCHEMA",
    "NAV_KIND",
    "NAV_REQUEST_SCHEMA",
    "NAV_RESPONSE_SCHEMA",
    "PLAN_SCHEMA",
    "REDUCE_KIND",
    "REDUCE_REQUEST_SCHEMA",
    "REDUCE_RESPONSE_SCHEMA",
    "SelectedNavigationError",
    "build_global_navigation_request",
    "build_layout_reduction_requests",
    "build_merge_detail_reduction_requests",
    "build_merge_group_adjudication_requests",
    "build_proposed_merge_detail_requests",
    "build_source_detail_index",
    "compile_section_coordination",
    "compile_selected_candidate_navigation_plan",
    "derive_merged_section_id",
    "layout_leaf_nodes_from_fragments",
    "measure_navigation_output_reserve",
    "merge_detail_reduction_response_shape",
    "selected_navigation_call_counts",
    "validate_all_layout_fragments",
    "validate_global_navigation_response",
    "validate_layout_fragment",
    "validate_layout_reduction_response",
    "validate_merge_detail_reduction_response",
    "validate_merge_detail_responses",
    "validate_merge_group_adjudication_responses",
    "validate_source_detail_index",
]

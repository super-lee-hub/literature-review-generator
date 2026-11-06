"""Exact per-request arbitration work registration and dispatch validation."""
from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from outline.v3_models import compute_v3_hash
from runtime.arbitration_work_policy import ALLOWED_KINDS
from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_runtime import ProviderCallReceiptV1, hash_json

ARBITRATION_TRANSPORT_NODE_ID = "arbitration"


def retained_output_reserve_per_attempt(estimation: Mapping[str, Any]) -> int:
    """Read the original reserve, or a conservative retained route ceiling.

    Older receipts redacted the per-attempt accounting key. Their immutable
    max-output ceiling is a safe upper bound; a missing or malformed ceiling
    must block admission rather than silently count the earlier work as zero.
    """
    value = estimation.get("requested_output_tokens_per_attempt")
    if value is None or value == "[REDACTED_SECRET]":
        value = estimation.get("max_output_tokens")
    if type(value) is not int or value <= 0:
        raise ArbitrationDispatchError("prior arbitration output reserve is unavailable or invalid")
    return value

_DISPATCH_ROW_FIELDS = frozenset({
    "request_id", "request_hash", "requested_output_tokens", "response_shape_hash",
    "measurement", "compiled_record_hash", "kind", "transport_node_id", "level", "ordinal",
})
_DISPATCH_ONLY_FIELDS = frozenset({"kind", "transport_node_id", "ordinal"})
_RUNTIME_MEASUREMENT_FIELDS = frozenset({
    "effective_attempts", "requested_output_tokens_per_attempt",
})


class ArbitrationDispatchError(ValueError):
    """A materialized arbitration dispatch differs from its READY authority."""


def _request_id(record: Mapping[str, Any]) -> str:
    value = record.get("request_id") or record.get("node_id")
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationDispatchError("arbitration request ID is missing")
    return value.strip()


def _request_body(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("request")
    if not isinstance(value, Mapping):
        value = record.get("prompt_request")
    if not isinstance(value, Mapping):
        raise ArbitrationDispatchError("arbitration request body is missing")
    return copy.deepcopy(dict(value))


def _request_hash(record: Mapping[str, Any], request: Mapping[str, Any]) -> str:
    measurement = record.get("measurement", record.get("estimate"))
    value = record.get("request_hash")
    if not value and isinstance(measurement, Mapping):
        value = measurement.get("request_hash")
    digest = str(value or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ArbitrationDispatchError("arbitration request hash is invalid")
    if compute_v3_hash(dict(request)) != digest:
        raise ArbitrationDispatchError("arbitration request hash differs from the exact request")
    return digest


def verify_arbitration_hash_domains(
    request: Mapping[str, Any],
    *,
    planner_request_hash: str,
    provider_input_hash: str,
) -> None:
    """Bind planner and ProviderRuntime digests to the same exact JSON request.

    The planner's compiler record uses ``compute_v3_hash``. Provider receipts
    use ``hash_json`` and its provider-domain prefix. Both hashes are required
    at the send/readback boundary; neither digest can stand in for the other.
    """

    if not isinstance(request, Mapping):
        raise ArbitrationDispatchError("arbitration hash-domain request must be an object")
    try:
        exact_request = copy.deepcopy(dict(request))
        expected_planner_hash = compute_v3_hash(exact_request)
        expected_provider_hash = hash_json(exact_request)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArbitrationDispatchError("arbitration hash-domain request is not finite JSON") from exc

    for label, observed, expected in (
        ("planner request", planner_request_hash, expected_planner_hash),
        ("provider input", provider_input_hash, expected_provider_hash),
    ):
        if (
            not isinstance(observed, str)
            or len(observed) != 64
            or any(char not in "0123456789abcdef" for char in observed)
        ):
            raise ArbitrationDispatchError(f"arbitration {label} hash is invalid")
        if observed != expected:
            raise ArbitrationDispatchError(
                f"arbitration {label} hash differs from the exact request"
            )


def _output_contract(record: Mapping[str, Any]) -> tuple[int, str]:
    measurement = record.get("measurement", record.get("estimate"))
    reserve = measurement.get("output_reserve") if isinstance(measurement, Mapping) else None
    output = record.get("requested_output_tokens")
    if output is None and isinstance(reserve, Mapping):
        output = reserve.get("requested_output_tokens", reserve.get("estimated_output_tokens"))
    shape_hash = record.get("response_shape_hash")
    if shape_hash is None and isinstance(reserve, Mapping):
        shape_hash = reserve.get("response_shape_hash")
    if isinstance(output, bool) or not isinstance(output, int) or output <= 0:
        raise ArbitrationDispatchError("arbitration output reserve is invalid")
    digest = str(shape_hash or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ArbitrationDispatchError("arbitration response-shape hash is invalid")
    response_shape = record.get("response_shape")
    if response_shape is not None and (
        not isinstance(response_shape, Mapping)
        or compute_v3_hash(dict(response_shape)) != digest
    ):
        raise ArbitrationDispatchError("arbitration response shape differs from its compiler hash")
    if isinstance(reserve, Mapping):
        for field in ("requested_output_tokens", "estimated_output_tokens"):
            if field not in reserve:
                continue
            reserved_output = reserve[field]
            if (
                isinstance(reserved_output, bool)
                or not isinstance(reserved_output, int)
                or reserved_output <= 0
                or reserved_output != output
            ):
                raise ArbitrationDispatchError("arbitration output reserve differs from its compiler measurement")
        route_cap = reserve.get("max_output_tokens", reserve.get("route_output_cap"))
        if route_cap is not None and (
            isinstance(route_cap, bool)
            or not isinstance(route_cap, int)
            or route_cap <= 0
            or output > route_cap
        ):
            raise ArbitrationDispatchError("arbitration output reserve exceeds its compiler route cap")
        if reserve.get("response_shape_hash") != digest:
            raise ArbitrationDispatchError("arbitration output reserve differs from its compiler measurement")
    return output, digest


def _record_level(record: Mapping[str, Any], *, fallback: int | None = None) -> int:
    values: list[Any] = []
    if record.get("level") is not None:
        values.append(record.get("level"))
    body = record.get("body")
    if isinstance(body, Mapping) and body.get("level") is not None:
        values.append(body.get("level"))
    request = record.get("request", record.get("prompt_request"))
    if isinstance(request, Mapping):
        if request.get("level") is not None:
            values.append(request.get("level"))
        request_body = request.get("body")
        if isinstance(request_body, Mapping) and request_body.get("level") is not None:
            values.append(request_body.get("level"))
    if values and any(value != values[0] for value in values[1:]):
        raise ArbitrationDispatchError("arbitration request has conflicting tree levels")
    level = values[0] if values else (fallback if fallback is not None else 0)
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        raise ArbitrationDispatchError("arbitration request level must be a non-negative integer")
    if fallback is not None and level != fallback:
        raise ArbitrationDispatchError("arbitration request level differs from the declared level")
    return level


def _measurement(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArbitrationDispatchError(f"{label} measurement is missing")
    result = copy.deepcopy(dict(value))
    input_tokens = result.get("estimated_input_tokens")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ArbitrationDispatchError(f"{label} input forecast is invalid")
    if result.get("within_budget") is not True:
        raise ArbitrationDispatchError(f"{label} input forecast is not within its route")
    wire_hash = str(result.get("wire_request_hash") or "")
    if len(wire_hash) != 64 or any(char not in "0123456789abcdef" for char in wire_hash):
        raise ArbitrationDispatchError(f"{label} formal wire hash is missing or invalid")
    wire_bytes = result.get("wire_serialized_bytes")
    if isinstance(wire_bytes, bool) or not isinstance(wire_bytes, int) or wire_bytes <= 0:
        raise ArbitrationDispatchError(f"{label} serialized wire size is invalid")
    scope = result.get("estimation_scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ArbitrationDispatchError(f"{label} measurement scope is missing")
    effective_attempts = result.get("effective_attempts")
    if effective_attempts is not None and (
        isinstance(effective_attempts, bool)
        or not isinstance(effective_attempts, int)
        or effective_attempts <= 0
    ):
        raise ArbitrationDispatchError(f"{label} effective attempt count is invalid")
    output_tokens = result.get("requested_output_tokens_per_attempt")
    if output_tokens is not None and (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens <= 0
    ):
        raise ArbitrationDispatchError(f"{label} output reserve measurement is invalid")
    return result


def _measurement_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key not in _RUNTIME_MEASUREMENT_FIELDS
    }


def _compiled_record_hash(
    record: Mapping[str, Any],
    *,
    request_id: str,
    request: Mapping[str, Any],
    request_hash: str,
    requested_output_tokens: int,
    response_shape_hash: str,
    level: int,
) -> str:
    """Normalize fields added by the dispatch edge, then hash every compiler field."""
    normalized = copy.deepcopy(dict(record))
    for field in _DISPATCH_ONLY_FIELDS:
        normalized.pop(field, None)
    normalized.update({
        "request_id": request_id,
        "request": copy.deepcopy(dict(request)),
        "request_hash": request_hash,
        "requested_output_tokens": requested_output_tokens,
        "response_shape_hash": response_shape_hash,
        "level": level,
    })
    return compute_v3_hash(normalized)


def build_arbitration_dispatch_row(
    request_record: Mapping[str, Any],
    *,
    kind: str,
    measured: Mapping[str, Any],
    ordinal: int,
    level: int | None = None,
) -> dict[str, Any]:
    """Bind one exact compiler request to its kind, level, ordinal, and forecast."""
    if not isinstance(request_record, Mapping) or kind not in ALLOWED_KINDS:
        raise ArbitrationDispatchError("arbitration request or kind is invalid")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ArbitrationDispatchError("arbitration ordinal must be a non-negative integer")
    request_id = _request_id(request_record)
    request = _request_body(request_record)
    request_hash = _request_hash(request_record, request)
    output, response_shape_hash = _output_contract(request_record)
    resolved_level = _record_level(request_record, fallback=level)
    if isinstance(measured, Mapping):
        measurement = _measurement(measured, label="arbitration")
    else:
        raise ArbitrationDispatchError("arbitration measured forecast is missing")
    if measurement.get("wire_request_hash") != measured.get("wire_request_hash"):
        raise ArbitrationDispatchError("arbitration wire request hash differs from its measurement")
    return {
        "request_id": request_id,
        "request_hash": request_hash,
        "requested_output_tokens": output,
        "response_shape_hash": response_shape_hash,
        "measurement": measurement,
        "compiled_record_hash": _compiled_record_hash(
            request_record,
            request_id=request_id,
            request=request,
            request_hash=request_hash,
            requested_output_tokens=output,
            response_shape_hash=response_shape_hash,
            level=resolved_level,
        ),
        "kind": kind,
        "transport_node_id": ARBITRATION_TRANSPORT_NODE_ID,
        "level": resolved_level,
        "ordinal": ordinal,
    }


def _validate_dispatch_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DISPATCH_ROW_FIELDS:
        raise ArbitrationDispatchError("arbitration dispatch row has unknown or missing fields")
    row = copy.deepcopy(dict(value))
    if (
        not isinstance(row.get("request_id"), str) or not row["request_id"]
        or row.get("kind") not in ALLOWED_KINDS
        or row.get("transport_node_id") != ARBITRATION_TRANSPORT_NODE_ID
        or isinstance(row.get("requested_output_tokens"), bool)
        or not isinstance(row.get("requested_output_tokens"), int)
        or row["requested_output_tokens"] <= 0
        or type(row.get("level")) is not int or row["level"] < 0
        or type(row.get("ordinal")) is not int or row["ordinal"] < 0
    ):
        raise ArbitrationDispatchError("arbitration dispatch row identity or reserve is invalid")
    for name in ("request_hash", "response_shape_hash", "compiled_record_hash"):
        digest = row.get(name)
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ArbitrationDispatchError(f"arbitration dispatch {name} is invalid")
    row["measurement"] = _measurement(row.get("measurement"), label="arbitration dispatch")
    return row


def _admission_rows(admission_body: Mapping[str, Any]) -> list[dict[str, Any]]:
    if admission_body.get("schema_version") != "arbitration-ready-kind/v1":
        raise ArbitrationDispatchError("arbitration admission schema is unsupported")
    kind = admission_body.get("kind")
    max_count = admission_body.get("max_count")
    rows = admission_body.get("requests")
    request_ids = admission_body.get("request_ids")
    count = admission_body.get("count")
    if (
        kind not in ALLOWED_KINDS
        or isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 0
        or not isinstance(rows, list)
        or isinstance(count, bool) or not isinstance(count, int) or count != len(rows)
        or not isinstance(request_ids, list)
    ):
        raise ArbitrationDispatchError("arbitration admission counts or kind are invalid")
    normalized = [_validate_dispatch_row(row) for row in rows]
    ids = [row["request_id"] for row in normalized]
    if (
        len(ids) != len(set(ids))
        or request_ids != ids
        or any(row["kind"] != kind for row in normalized)
        or len({(row["level"], row["ordinal"]) for row in normalized}) != len(normalized)
    ):
        raise ArbitrationDispatchError("arbitration admission request list is inconsistent")
    cumulative = admission_body.get("cumulative_requests")
    if cumulative is None:
        if count > max_count:
            raise ArbitrationDispatchError("arbitration admission exceeds its kind maximum")
        return normalized
    cumulative_count = admission_body.get("cumulative_count")
    if (
        not isinstance(cumulative, list)
        or isinstance(cumulative_count, bool)
        or not isinstance(cumulative_count, int)
        or cumulative_count != len(cumulative)
        or cumulative_count > max_count
    ):
        raise ArbitrationDispatchError("arbitration cumulative admission count is invalid")
    cumulative_rows = [_validate_dispatch_row(row) for row in cumulative]
    cumulative_ids = [row["request_id"] for row in cumulative_rows]
    if (
        len(cumulative_ids) != len(set(cumulative_ids))
        or any(row["kind"] != kind for row in cumulative_rows)
    ):
        raise ArbitrationDispatchError("arbitration cumulative admission rows are inconsistent")
    cumulative_by_id = {row["request_id"]: row for row in cumulative_rows}
    if any(cumulative_by_id.get(row["request_id"]) != row for row in normalized):
        raise ArbitrationDispatchError("current arbitration requests differ from cumulative work authority")
    ordinals_by_level: dict[int, list[int]] = defaultdict(list)
    for row in cumulative_rows:
        ordinals_by_level[row["level"]].append(row["ordinal"])
    if any(sorted(values) != list(range(len(values))) for values in ordinals_by_level.values()):
        raise ArbitrationDispatchError("arbitration cumulative per-level ordinals are not continuous")
    return normalized


def validate_arbitration_admission(admission_body: Mapping[str, Any]) -> None:
    """Validate the full current and cumulative READY admission body."""
    if not isinstance(admission_body, Mapping):
        raise ArbitrationDispatchError("arbitration admission is malformed")
    _admission_rows(admission_body)


def validate_arbitration_dispatch(
    admission_body: Mapping[str, Any],
    dispatch_record: Mapping[str, Any],
    *,
    measured: Mapping[str, Any],
) -> None:
    """Require the real sender input and fresh route forecast to match READY authority."""
    if not isinstance(admission_body, Mapping) or not isinstance(dispatch_record, Mapping):
        raise ArbitrationDispatchError("arbitration dispatch input is malformed")
    rows = _admission_rows(admission_body)
    request_id = _request_id(dispatch_record)
    matches = [row for row in rows if row["request_id"] == request_id]
    if len(matches) != 1:
        raise ArbitrationDispatchError("arbitration dispatch is not uniquely present in its admission")
    row = matches[0]
    if (
        dispatch_record.get("kind") != admission_body.get("kind")
        or dispatch_record.get("kind") != row["kind"]
        or dispatch_record.get("transport_node_id") != row["transport_node_id"]
    ):
        raise ArbitrationDispatchError("arbitration dispatch kind or transport differs from its admission")
    request = _request_body(dispatch_record)
    request_hash = _request_hash(dispatch_record, request)
    output, shape_hash = _output_contract(dispatch_record)
    level = _record_level(dispatch_record, fallback=row["level"])
    if dispatch_record.get("ordinal") is not None:
        ordinal = dispatch_record.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != row["ordinal"]:
            raise ArbitrationDispatchError("arbitration dispatch ordinal differs from its admission")
    if (
        request_hash != row["request_hash"]
        or output != row["requested_output_tokens"]
        or shape_hash != row["response_shape_hash"]
        or level != row["level"]
        or _compiled_record_hash(
            dispatch_record,
            request_id=request_id,
            request=request,
            request_hash=request_hash,
            requested_output_tokens=output,
            response_shape_hash=shape_hash,
            level=level,
        ) != row["compiled_record_hash"]
    ):
        raise ArbitrationDispatchError("arbitration dispatch differs from its exact compiler record")
    fresh = _measurement(measured, label="fresh arbitration")
    fresh_output = fresh.get("requested_output_tokens_per_attempt")
    if fresh_output is not None and fresh_output != row["requested_output_tokens"]:
        raise ArbitrationDispatchError("arbitration fresh measurement differs from its output reserve")
    if _measurement_identity(fresh) != _measurement_identity(row["measurement"]):
        raise ArbitrationDispatchError("arbitration fresh measurement differs from its READY forecast")


def admit_arbitration_work_rows(
    prior_rows: Sequence[Mapping[str, Any]],
    new_rows: Sequence[Mapping[str, Any]],
    *,
    max_count: int,
) -> list[dict[str, Any]]:
    """Accumulate per-kind rows without expanding counts on replay or duplication."""
    if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 0:
        raise ArbitrationDispatchError("arbitration maximum count is invalid")
    combined: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    kind: str | None = None
    for raw in prior_rows:
        row = _validate_dispatch_row(raw)
        if kind is None:
            kind = row["kind"]
        elif row["kind"] != kind:
            raise ArbitrationDispatchError("arbitration cumulative rows mix request kinds")
        previous = by_id.get(row["request_id"])
        if previous is not None:
            raise ArbitrationDispatchError("prior arbitration rows repeat a request ID")
        by_id[row["request_id"]] = row
        combined.append(row)
    previous_ordinals: dict[int, list[int]] = defaultdict(list)
    for row in combined:
        previous_ordinals[row["level"]].append(row["ordinal"])
    for level, ordinals in previous_ordinals.items():
        if sorted(ordinals) != list(range(len(ordinals))):
            raise ArbitrationDispatchError(f"prior arbitration level {level} ordinals are not continuous")
    for raw in new_rows:
        row = _validate_dispatch_row(raw)
        if kind is None:
            kind = row["kind"]
        elif row["kind"] != kind:
            raise ArbitrationDispatchError("arbitration cumulative rows mix request kinds")
        previous = by_id.get(row["request_id"])
        if previous is not None:
            previous_content = {key: value for key, value in previous.items() if key != "ordinal"}
            replay_content = {key: value for key, value in row.items() if key != "ordinal"}
            if previous_content != replay_content:
                raise ArbitrationDispatchError("duplicate arbitration request ID changed its work binding")
            continue
        expected_ordinal = sum(1 for item in combined if item["level"] == row["level"])
        if row["ordinal"] != expected_ordinal:
            raise ArbitrationDispatchError(
                f"arbitration level {row['level']} ordinals must be continuous from zero"
            )
        by_id[row["request_id"]] = row
        combined.append(row)
    if len(combined) > max_count:
        raise ArbitrationDispatchError("arbitration cumulative request count exceeds its source maximum")
    return combined


def validate_retained_arbitration_receipt(
    expected: ExpectedProviderCall,
    receipt: ProviderCallReceiptV1,
    content: Mapping[str, Any],
    *,
    route: str,
    node_id: str,
    config_section: str,
) -> None:
    """Verify a retained physical receipt against the current semantic route and node."""
    if not isinstance(expected, ExpectedProviderCall) or not isinstance(receipt, ProviderCallReceiptV1):
        # Callers classify invalid retained authority as a rejected value.
        raise ValueError("retained arbitration identity must use current provider call records")  # noqa: TRY004
    if not expected.call_id.startswith("outline:"):
        raise ValueError("retained arbitration call ID has no Outline semantic prefix")
    semantic_route = expected.call_id.removeprefix("outline:")
    if (
        expected.stage_name != "outline_v3"
        or not semantic_route
        or expected.call_id != f"outline:{route}"
        or route != semantic_route
        or node_id != expected.node_id
        or not isinstance(config_section, str)
        or not config_section.strip()
        or receipt.job_id != expected.job_id
        or receipt.call_id != expected.call_id
        or receipt.attempt_id != expected.attempt_id
        or receipt.stage_name != expected.stage_name
        or receipt.node_id != expected.node_id
        or receipt.route != route
        or receipt.metadata.get("config_section") != config_section
        or receipt.closure_epoch_id != expected.closure_epoch_id
        or receipt.logical_attempt_identity != expected.logical_attempt_identity
        or receipt.input_hash != expected.input_hash
        or receipt.prompt_hash != expected.prompt_hash
        or receipt.prompt_id != expected.prompt_id
        or receipt.prompt_version != expected.prompt_version
        or receipt.prompt_sha256 != expected.prompt_sha256
        or receipt.config_hash != expected.config_hash
        or receipt.schema_hash != expected.schema_hash
        or receipt.provider != expected.provider
        or receipt.model != expected.model
        or receipt.endpoint != expected.endpoint
        or receipt.endpoint_type != expected.endpoint_type
        or (expected.max_attempts > 0 and receipt.attempts > expected.max_attempts)
        or receipt.status != "success"
        or receipt.error_kind is not None
        or receipt.timeout_kind
        or receipt.incomplete_reason
        or receipt.test_only
        or not isinstance(content, Mapping)
        or receipt.response_hash != hash_json(content)
    ):
        raise ValueError("retained arbitration receipt differs from its semantic call or route")


__all__ = [
    "ARBITRATION_TRANSPORT_NODE_ID",
    "ArbitrationDispatchError",
    "admit_arbitration_work_rows",
    "build_arbitration_dispatch_row",
    "validate_arbitration_admission",
    "validate_arbitration_dispatch",
    "validate_retained_arbitration_receipt",
]

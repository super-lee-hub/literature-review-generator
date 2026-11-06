from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from outline.candidate_arbitration_aggregation import SELECTION_DETAIL_KIND
from outline.candidate_arbitration_navigation import REDUCE_KIND
from outline.v3_models import compute_v3_hash
from runtime.arbitration_dispatch import (
    ArbitrationDispatchError,
    admit_arbitration_work_rows,
    build_arbitration_dispatch_row,
    validate_arbitration_admission,
    validate_arbitration_dispatch,
    validate_retained_arbitration_receipt,
    verify_arbitration_hash_domains,
)
from runtime.provider_receipt_closure import ExpectedProviderCall
from runtime.provider_runtime import (
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    hash_json,
    hash_text,
)

_ROUTE = {
    "config_section": "Outline_API",
    "provider": "generic",
    "model": "arbitration-local",
    "endpoint": "127.0.0.1:5444",
    "endpoint_type": "chat_completions",
}


def _request_record(request_id: str, *, level: int = 0, content: str = "source") -> dict[str, Any]:
    request = {
        "schema_version": "arbitration-test-request/v1",
        "node_id": request_id,
        "level": level,
        "body": {"level": level, "source_text": content},
    }
    request_hash = compute_v3_hash(request)
    response_shape = {"schema_version": "arbitration-test-response/v1", "id": request_id}
    response_shape_hash = compute_v3_hash(response_shape)
    output = 256
    return {
        "request_id": request_id,
        "node_id": request_id,
        "level": level,
        "request": request,
        "request_hash": request_hash,
        "requested_output_tokens": output,
        "response_shape": response_shape,
        "response_shape_hash": response_shape_hash,
        "measurement": {
            "request_hash": request_hash,
            "compiler_record_hash": compute_v3_hash({"request_id": request_id, "level": level}),
            "output_reserve": {
                "estimated_output_tokens": output,
                "requested_output_tokens": output,
                "max_output_tokens": 2048,
                "response_shape_hash": response_shape_hash,
            },
        },
        "compiler_partition_id": f"partition:{level}",
    }


def _measure(request: dict[str, Any]) -> dict[str, Any]:
    wire = {
        "system": "Use the admitted source scope only.",
        "user": json.dumps({"node_id": request["node_id"], "request": request}, ensure_ascii=False, sort_keys=True),
    }
    return {
        "estimated_input_tokens": 420,
        "within_budget": True,
        "input_budget": 30_000,
        "wire_request_hash": compute_v3_hash(wire),
        "wire_serialized_bytes": len(json.dumps(wire, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        "estimation_scope": "formal_outline_text_wire/v1",
        "route_identity": dict(_ROUTE),
    }


def _row(
    request_id: str,
    *,
    kind: str = REDUCE_KIND,
    level: int = 0,
    ordinal: int = 0,
    content: str = "source",
) -> dict[str, Any]:
    record = _request_record(request_id, level=level, content=content)
    return build_arbitration_dispatch_row(
        record, kind=kind, measured=_measure(record["request"]), ordinal=ordinal,
    )


def _admission(rows: list[dict[str, Any]], *, kind: str, max_count: int) -> dict[str, Any]:
    request_ids = [row["request_id"] for row in rows]
    return {
        "schema_version": "arbitration-ready-kind/v1",
        "kind": kind,
        "count": len(rows),
        "max_count": max_count,
        "request_ids": request_ids,
        "requests": copy.deepcopy(rows),
        "cumulative_count": len(rows),
        "cumulative_requests": copy.deepcopy(rows),
        "branch_ref": {"artifact_id": "ready-overflow-branch"},
    }


def _dispatch_record(request_id: str, *, level: int = 0) -> dict[str, Any]:
    record = _request_record(request_id, level=level)
    return {**record, "kind": REDUCE_KIND, "transport_node_id": "arbitration"}


def test_dispatch_matches_registered_compiler_record_kind_measurement_and_reserve():
    row = _row("reduce:level-0:0", level=0, ordinal=0)
    admission = _admission([row], kind=REDUCE_KIND, max_count=3)
    fresh_measurement = {**row["measurement"], "effective_attempts": 1,
                         "requested_output_tokens_per_attempt": row["requested_output_tokens"]}

    validate_arbitration_dispatch(
        admission, _dispatch_record(row["request_id"]), measured=fresh_measurement,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "kind",
        "transport",
        "request_body",
        "request_hash",
        "output_reserve",
        "response_shape",
        "compiler_record",
        "level",
        "ordinal",
        "fresh_measurement",
        "fresh_output_reserve",
    ],
)
def test_dispatch_rejects_kind_route_request_reserve_or_measurement_drift(mutation):
    row = _row("reduce:level-0:0", level=0, ordinal=0)
    admission = _admission([row], kind=REDUCE_KIND, max_count=3)
    dispatch = _dispatch_record(row["request_id"])
    measured = {**row["measurement"], "effective_attempts": 1,
                "requested_output_tokens_per_attempt": row["requested_output_tokens"]}

    if mutation == "kind":
        dispatch["kind"] = SELECTION_DETAIL_KIND
    elif mutation == "transport":
        dispatch["transport_node_id"] = "writer"
    elif mutation == "request_body":
        dispatch["request"]["body"]["source_text"] = "changed source"
    elif mutation == "request_hash":
        dispatch["request_hash"] = "f" * 64
    elif mutation == "output_reserve":
        dispatch["requested_output_tokens"] += 1
        dispatch["measurement"]["output_reserve"]["requested_output_tokens"] += 1
    elif mutation == "response_shape":
        dispatch["response_shape"]["id"] = "foreign-response-shape"
    elif mutation == "compiler_record":
        dispatch["compiler_partition_id"] = "foreign-partition"
    elif mutation == "level":
        dispatch["level"] = 1
    elif mutation == "ordinal":
        dispatch["ordinal"] = 1
    elif mutation == "fresh_measurement":
        measured["wire_request_hash"] = "f" * 64
    elif mutation == "fresh_output_reserve":
        measured["requested_output_tokens_per_attempt"] = row["requested_output_tokens"] + 1

    with pytest.raises(ArbitrationDispatchError):
        validate_arbitration_dispatch(admission, dispatch, measured=measured)


def test_cumulative_rows_reuse_exact_ids_without_spending_another_ordinal():
    row0 = _row("reduce:level-0:0", level=0, ordinal=0)
    row1 = _row("reduce:level-0:1", level=0, ordinal=1)
    row_level1 = _row("reduce:level-1:0", level=1, ordinal=0)

    prior = admit_arbitration_work_rows([], [row0, row1], max_count=3)
    replayed = admit_arbitration_work_rows(prior, [replace_row_ordinal(row0, 9)], max_count=3)
    extended = admit_arbitration_work_rows(replayed, [row_level1], max_count=3)

    assert [row["request_id"] for row in replayed] == [row0["request_id"], row1["request_id"]]
    assert [row["ordinal"] for row in extended] == [0, 1, 0]
    assert [row["level"] for row in extended] == [0, 0, 1]


def replace_row_ordinal(row: dict[str, Any], ordinal: int) -> dict[str, Any]:
    return {**row, "ordinal": ordinal}


def test_cumulative_rows_reject_changed_duplicate_gap_duplicate_ordinal_and_overflow():
    row0 = _row("reduce:level-0:0", level=0, ordinal=0)
    changed = _row("reduce:level-0:0", level=0, ordinal=0, content="changed source")
    row1 = _row("reduce:level-0:1", level=0, ordinal=1)
    row2 = _row("reduce:level-1:0", level=1, ordinal=0)

    with pytest.raises(ArbitrationDispatchError, match="changed its work binding"):
        admit_arbitration_work_rows([row0], [changed], max_count=3)
    with pytest.raises(ArbitrationDispatchError, match="continuous"):
        admit_arbitration_work_rows([], [row1], max_count=3)
    with pytest.raises(ArbitrationDispatchError, match="continuous"):
        admit_arbitration_work_rows([], [row0, replace_row_ordinal(row1, 0)], max_count=3)
    with pytest.raises(ArbitrationDispatchError, match="exceeds its source maximum"):
        admit_arbitration_work_rows([row0, row1], [row2], max_count=2)


@pytest.mark.parametrize("location", ["record", "body", "request", "request_body"])
def test_compiler_tree_level_fields_must_agree(location):
    record = _request_record("reduce:level-0:0", level=0)
    if location == "record":
        record["level"] = 1
    elif location == "body":
        record["body"] = {"level": 1}
    elif location == "request":
        record["request"]["level"] = 1
    else:
        record["request"]["body"]["level"] = 1
    record["request_hash"] = compute_v3_hash(record["request"])
    record["measurement"]["request_hash"] = record["request_hash"]

    with pytest.raises(ArbitrationDispatchError, match="conflicting tree levels"):
        build_arbitration_dispatch_row(record, kind=REDUCE_KIND, measured=_measure(record["request"]), ordinal=0)


def test_explicit_zero_level_is_checked_against_compiler_level():
    record = _request_record("reduce:level-1:0", level=1)

    with pytest.raises(ArbitrationDispatchError, match="differs from the declared level"):
        build_arbitration_dispatch_row(
            record, kind=REDUCE_KIND, measured=_measure(record["request"]), ordinal=0, level=0,
        )


def test_output_reserve_rejects_bool_alias_and_route_cap_overflow():
    record = _request_record("reduce:level-0:0")
    record["requested_output_tokens"] = 1
    record["measurement"]["output_reserve"].update({
        "estimated_output_tokens": 1,
        "requested_output_tokens": True,
        "max_output_tokens": 1,
    })

    with pytest.raises(ArbitrationDispatchError, match="output reserve"):
        build_arbitration_dispatch_row(record, kind=REDUCE_KIND, measured=_measure(record["request"]), ordinal=0)

    record["measurement"]["output_reserve"].update({
        "requested_output_tokens": 1,
        "estimated_output_tokens": 1,
        "max_output_tokens": 0,
    })
    with pytest.raises(ArbitrationDispatchError, match="route cap"):
        build_arbitration_dispatch_row(record, kind=REDUCE_KIND, measured=_measure(record["request"]), ordinal=0)


def test_ready_admission_rejects_duplicate_or_over_bound_cumulative_ids():
    row0 = _row("reduce:level-0:0", level=0, ordinal=0)
    row1 = _row("reduce:level-0:1", level=0, ordinal=1)
    admission = _admission([row0], kind=REDUCE_KIND, max_count=3)
    admission["cumulative_requests"] = [row0, row0]
    admission["cumulative_count"] = 2

    with pytest.raises(ArbitrationDispatchError, match="cumulative admission rows"):
        validate_arbitration_dispatch(
            admission, _dispatch_record(row0["request_id"]),
            measured=_measure(_request_record(row0["request_id"])["request"]),
        )

    admission = _admission([row0, row1], kind=REDUCE_KIND, max_count=1)
    with pytest.raises(ArbitrationDispatchError, match="cumulative admission count"):
        validate_arbitration_dispatch(
            admission, _dispatch_record(row0["request_id"]),
            measured=_measure(_request_record(row0["request_id"])["request"]),
        )


def _retained_receipt_case(tmp_path: Path, *, config_section: str = "Arbitration_Test_API"):
    config = {
        "provider_family": "generic",
        "model": "retained-arbitration-test-model",
        "api_base": "http://127.0.0.1:5444/v1",
        "api_key": "local-test-key",
        "endpoint_type": "chat_completions",
    }
    content = {"topics": [{"topic_id": "topic:test", "status": "completed"}]}
    request = {"node_id": "topic_synthesis", "source_scope": ["paper:a"]}
    call_id = "outline:topic_synthesis_provider:batch:84517586148063"
    route = call_id.removeprefix("outline:")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_total_tokens=100),
        ledger=ProviderRuntimeLedger(tmp_path / "retained-receipts.jsonl"),
        job_id="retained-job",
        attempt_id=call_id,
        stage_name="outline_v3",
        route=route,
        node_id="topic_synthesis",
        call_id=call_id,
        closure_epoch_id="retained-closure",
        logical_attempt_identity="retained-logical-attempt",
        endpoint_type=config["endpoint_type"],
        schema_hash=hash_text("retained-schema"),
        prompt_id="outline.node.system.v3",
        prompt_version="v3",
        prompt_sha256=hash_text("outline-system-v3"),
    )
    receipt = runtime.complete(
        admission=runtime.admit(estimated_tokens=5, requested_output_tokens=20),
        prompt="retained arbitration prompt",
        input_payload=request,
        api_config=config,
        result={
            "status": "success", "content": content, "finish_reason": "stop",
            "input_tokens": 5, "output_tokens": 4, "total_tokens": 9,
        },
        metadata={"call_id": call_id, "config_section": config_section, "transport_config": config},
    )
    expected = ExpectedProviderCall(
        call_id=call_id,
        job_id=receipt.job_id,
        attempt_id=receipt.attempt_id,
        stage_name=receipt.stage_name,
        node_id=receipt.node_id,
        closure_epoch_id=receipt.closure_epoch_id,
        logical_attempt_identity=receipt.logical_attempt_identity,
        prompt_hash=receipt.prompt_hash,
        prompt_id=receipt.prompt_id,
        prompt_version=receipt.prompt_version,
        prompt_sha256=receipt.prompt_sha256,
        input_hash=receipt.input_hash,
        config_hash=receipt.config_hash,
        schema_hash=receipt.schema_hash,
        provider=receipt.provider,
        model=receipt.model,
        endpoint=receipt.endpoint,
        endpoint_type=receipt.endpoint_type,
        max_attempts=1,
    )
    return expected, receipt, content, route, request


def test_retained_receipt_accepts_real_outline_semantic_route_shape_without_fixed_config_name(tmp_path):
    config_section = "Arbitration_Test_API"
    expected, receipt, content, route, _request = _retained_receipt_case(
        tmp_path, config_section=config_section,
    )

    validate_retained_arbitration_receipt(
        expected, receipt, content, route=route, node_id="topic_synthesis", config_section=config_section,
    )


def test_hash_domain_verifier_accepts_request_bound_by_real_provider_runtime_receipt(tmp_path):
    _expected, receipt, _content, _route, request = _retained_receipt_case(tmp_path)
    planner_request_hash = compute_v3_hash(request)
    provider_input_hash = receipt.input_hash

    assert provider_input_hash == hash_json(request)
    assert provider_input_hash != planner_request_hash
    verify_arbitration_hash_domains(
        request,
        planner_request_hash=planner_request_hash,
        provider_input_hash=provider_input_hash,
    )


@pytest.mark.parametrize("mutation", ["planner_domain", "provider_domain", "swapped_domains", "request_body"])
def test_hash_domain_verifier_rejects_wrong_domain_or_changed_request(tmp_path, mutation):
    _expected, receipt, _content, _route, request = _retained_receipt_case(tmp_path)
    planner_request_hash = compute_v3_hash(request)
    provider_input_hash = receipt.input_hash

    if mutation == "planner_domain":
        planner_request_hash = compute_v3_hash({"node_id": "foreign-request"})
    elif mutation == "provider_domain":
        provider_input_hash = hash_json({"node_id": "foreign-request"})
    elif mutation == "swapped_domains":
        planner_request_hash, provider_input_hash = provider_input_hash, planner_request_hash
    else:
        request = {**request, "source_scope": ["paper:foreign"]}

    with pytest.raises(ArbitrationDispatchError):
        verify_arbitration_hash_domains(
            request,
            planner_request_hash=planner_request_hash,
            provider_input_hash=provider_input_hash,
        )


def test_admission_validator_accepts_exact_current_rows_and_cumulative_history():
    prior = _row("reduce:level-0:0", level=0, ordinal=0)
    current = _row("reduce:level-0:1", level=0, ordinal=1)
    admission = _admission([current], kind=REDUCE_KIND, max_count=3)
    admission["cumulative_requests"] = [prior, current]
    admission["cumulative_count"] = 2

    validate_arbitration_admission(admission)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_current_ids", "admission request list is inconsistent"),
        ("current_count", "admission counts or kind are invalid"),
        ("cumulative_count", "cumulative admission count is invalid"),
        ("duplicate_history_ids", "cumulative admission rows are inconsistent"),
        ("history_ordinal_gap", "ordinals are not continuous"),
        ("history_request_ids", "admission request list is inconsistent"),
    ],
)
def test_admission_validator_rejects_malformed_current_or_historical_rows(mutation, message):
    row0 = _row("reduce:level-0:0", level=0, ordinal=0)
    row1 = _row("reduce:level-0:1", level=0, ordinal=1)
    row2 = _row("reduce:level-0:2", level=0, ordinal=2)
    admission = _admission([row0], kind=REDUCE_KIND, max_count=4)

    if mutation == "duplicate_current_ids":
        admission["requests"] = [row0, row0]
        admission["request_ids"] = [row0["request_id"], row0["request_id"]]
        admission["count"] = 2
        admission["cumulative_requests"] = [row0]
        admission["cumulative_count"] = 1
    elif mutation == "current_count":
        admission["count"] = 2
    elif mutation == "cumulative_count":
        admission["cumulative_requests"] = [row0, row1]
        admission["cumulative_count"] = 1
    elif mutation == "duplicate_history_ids":
        admission["cumulative_requests"] = [row0, row0]
        admission["cumulative_count"] = 2
    elif mutation == "history_ordinal_gap":
        admission["cumulative_requests"] = [row0, row2]
        admission["cumulative_count"] = 2
    elif mutation == "history_request_ids":
        admission["request_ids"] = ["foreign-request"]

    with pytest.raises(ArbitrationDispatchError, match=message):
        validate_arbitration_admission(admission)


@pytest.mark.parametrize(
    "mutation",
    [
        "semantic_route",
        "call_id",
        "call_id_prefix",
        "attempt_id",
        "node_id",
        "input_hash",
        "prompt_hash",
        "prompt_id",
        "prompt_version",
        "prompt_sha256",
        "config_hash",
        "schema_hash",
        "provider",
        "model",
        "endpoint",
        "endpoint_type",
        "config_section",
        "attempt_limit",
        "response_hash",
    ],
)
def test_retained_receipt_rejects_wrong_semantic_route_node_or_config(tmp_path, mutation):
    expected, receipt, content, route, _request = _retained_receipt_case(tmp_path)
    current_route = route
    current_node = "topic_synthesis"
    current_config_section = "Arbitration_Test_API"
    if mutation == "semantic_route":
        receipt = replace(receipt, route="foreign_semantic_route")
    elif mutation == "call_id":
        expected = replace(expected, call_id="outline:foreign_semantic_route")
    elif mutation == "call_id_prefix":
        expected = replace(expected, call_id="foreign_semantic_route")
    elif mutation == "attempt_id":
        receipt = replace(receipt, attempt_id="foreign-attempt")
    elif mutation == "node_id":
        receipt = replace(receipt, node_id="global_synthesis")
    elif mutation == "input_hash":
        receipt = replace(receipt, input_hash="f" * 64)
    elif mutation == "prompt_hash":
        receipt = replace(receipt, prompt_hash="f" * 64)
    elif mutation == "prompt_id":
        receipt = replace(receipt, prompt_id="foreign-prompt")
    elif mutation == "prompt_version":
        receipt = replace(receipt, prompt_version="foreign-version")
    elif mutation == "prompt_sha256":
        receipt = replace(receipt, prompt_sha256="f" * 64)
    elif mutation == "config_hash":
        receipt = replace(receipt, config_hash="f" * 64)
    elif mutation == "schema_hash":
        receipt = replace(receipt, schema_hash="f" * 64)
    elif mutation == "provider":
        receipt = replace(receipt, provider="foreign-provider")
    elif mutation == "model":
        receipt = replace(receipt, model="foreign-model")
    elif mutation == "endpoint":
        receipt = replace(receipt, endpoint="foreign-endpoint")
    elif mutation == "endpoint_type":
        receipt = replace(receipt, endpoint_type="foreign-endpoint-type")
    elif mutation == "config_section":
        receipt = replace(receipt, metadata={**receipt.metadata, "config_section": "Writer_Test_API"})
    elif mutation == "attempt_limit":
        expected = replace(expected, max_attempts=1)
        receipt = replace(receipt, attempts=2)
    elif mutation == "response_hash":
        receipt = replace(receipt, response_hash="f" * 64)

    with pytest.raises(ValueError):
        validate_retained_arbitration_receipt(
            expected, receipt, content, route=current_route, node_id=current_node,
            config_section=current_config_section,
        )

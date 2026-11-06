"""Explicit successor stage wiring retains original values before normalization."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import socket
from typing import Any

import pytest

from runtime.provider_response_snapshot import ProviderResponseSnapshotStore
from runtime.provider_runtime import ProviderRuntime, ProviderRuntimeContractError, hash_json
from runtime.source_revision import PROVIDER_SEMANTIC_CONTRACT_V2, SourceRevisionError
from runtime.source_revision_response_capture import build_source_revision_response_sink
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import LocalPublicationContext
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from validation.execution_service import ValidationExecutionService


_CONTRACT = {"provider_semantic_contract_version": PROVIDER_SEMANTIC_CONTRACT_V2}
_API = {
    "provider_family": "openai_chat", "model": "snapshot-unit-model",
    "api_base": "http://127.0.0.1:1/v1", "api_key": "local-unit-key",
    "endpoint_type": "chat_completions", "transport_retries": "0",
}
_LEGACY_FACTORY_KEYS = {
    "budget", "ledger", "job_id", "attempt_id", "stage_name", "route", "node_id",
    "call_id", "closure_epoch_id", "logical_attempt_identity", "endpoint_type", "schema_hash",
}


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("response capture wiring units must not make provider or network calls")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def capture_case(tmp_path) -> dict[str, Any]:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "capture-wiring", job_id="capture-job")
    context = LocalPublicationContext()
    registry = context.registry(workspace.paths.registry_path, workspace.job_id)
    settings = ApplicationSettings.from_config({
        "Writer_API": dict(_API), "Validator_API": dict(_API),
        "Runtime": {"node_retry_limit": "0", "validation_retry_limit": "0"},
    })
    return {"workspace": workspace, "registry": registry, "context": context, "settings": settings}


def _sink(case):
    return build_source_revision_response_sink(
        _CONTRACT, case["workspace"], case["registry"], case["context"],
    )


def _service(stage, case, *, sink=None, factory=None):
    common = {
        "job_id": case["workspace"].job_id, "attempt_id": "capture-attempt",
        "workspace": case["workspace"], "artifact_registry": case["registry"],
        "settings": case["settings"], "summaries": [], "publication_context": case["context"],
    }
    if sink is not None:
        common["response_snapshot_sink"] = sink
    if stage == "writer":
        return ReviewGenerationService(**common)
    return ValidationExecutionService(
        **common, review_draft_record=None, citation_manifest_record=None,
        paper_artifact_records=[], visual_artifact_records=[], provider_factory=factory,
        cancellation_checker=None, logger=None, runtime_config={"Validator_API": dict(_API)},
    )


def _runtime(stage, service, *, index=1):
    if stage == "writer":
        return service._new_runtime(f"section-{index}", writer_config=_API)
    return service.new_provider_runtime(
        stage_name="stage4_validate", route="Validator_API", node_id=f"packet-{index}",
        call_id=f"validation:packet-{index}", api_config=_API,
    )


def _complete(runtime, content, *, failed=False, admission=None):
    if admission is None:
        admission = runtime.admit(estimated_tokens=3, requested_output_tokens=10)
    result = {
        "status": "failed" if failed else "success", "content": content,
        "finish_reason": "stop", "input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
    }
    if failed:
        result["error_kind"] = "outcome_unknown"
    receipt = runtime.complete(
        admission=admission, prompt="original source-closed prompt",
        input_payload={"source": "canonical-unit"}, api_config=_API, result=result,
        metadata={"transport_config": {
            key: _API[key] for key in ("provider_family", "endpoint_type", "api_base", "model")
        }},
    )
    return receipt, admission


def test_legacy_metadata_is_unchanged_and_does_not_construct_a_store(capture_case):
    case = capture_case
    metadata = {"unrelated": {"preserve": [True, 3]}}
    before = deepcopy(metadata)
    registry_before = case["registry"].capture_record_snapshot()

    assert build_source_revision_response_sink(
        metadata, case["workspace"], case["registry"], case["context"],
    ) is None
    assert metadata == before
    assert case["registry"].capture_record_snapshot() == registry_before
    assert not Path(case["workspace"].log_path("provider_response_snapshots")).exists()


@pytest.mark.parametrize("version", [None, False, 2, {}, "legacy/v1", "source-closed-provider/v1", "source-closed-provider/v3"])
def test_explicit_unsupported_version_rejects_before_capture(capture_case, version):
    case = capture_case
    with pytest.raises(SourceRevisionError, match="unsupported"):
        build_source_revision_response_sink(
            {"provider_semantic_contract_version": version},
            case["workspace"], case["registry"], case["context"],
        )
    assert case["registry"].list_records() == []


def test_explicit_v2_baseline_constructs_the_existing_store_without_a_plan_ref(capture_case):
    case = capture_case
    metadata = dict(_CONTRACT)
    store = build_source_revision_response_sink(metadata, case["workspace"], case["registry"], case["context"])

    assert type(store) is ProviderResponseSnapshotStore
    assert store.workspace is case["workspace"]
    assert store.registry is case["registry"]
    assert store.publication_context is case["context"]
    assert metadata == _CONTRACT
    assert case["registry"].list_records() == []


def test_explicit_v2_rejects_foreign_workspace_registry_binding(capture_case, tmp_path):
    case = capture_case
    foreign = ArtifactRegistry(tmp_path / "foreign_registry.json", "foreign-job")
    with pytest.raises(ProviderRuntimeContractError, match="ownership differs"):
        build_source_revision_response_sink(_CONTRACT, case["workspace"], foreign, case["context"])


@pytest.mark.parametrize("stage", ["writer", "validator"])
def test_service_response_sink_is_keyword_only(stage):
    service_type = ReviewGenerationService if stage == "writer" else ValidationExecutionService
    parameter = inspect.signature(service_type).parameters["response_snapshot_sink"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


@pytest.mark.parametrize("stage", ["writer", "validator"])
@pytest.mark.parametrize("content", ["原始响应\nwith café", {"values": [True, 0, None]}, ["source", 2], 0, False])
def test_service_runtime_retains_exact_raw_content_and_original_hash_domains(capture_case, stage, content, monkeypatch):
    case = capture_case
    store = _sink(case)
    service = _service(stage, case, sink=store)
    runtime = _runtime(stage, service)
    original_capture = store.capture
    observed = []

    def capture(receipt, value):
        assert any(row.to_dict() == receipt.to_dict() for row in runtime.ledger.list_receipts())
        observed.append(receipt.receipt_id)
        return original_capture(receipt, value)

    monkeypatch.setattr(store, "capture", capture)
    receipt, admission = _complete(runtime, content)
    ledger_bytes = Path(runtime.ledger.path).read_bytes()
    record = store.read_registered(receipt)
    payload = json.loads(Path(record.path).read_bytes())

    assert runtime.response_snapshot_sink is store
    assert receipt.test_only is False
    assert type(payload["content"]) is type(content)
    assert payload["content"] == content
    assert payload["response_hash"] == receipt.response_hash == hash_json(content)
    assert payload["receipt_hash"] == hash_json(receipt.to_dict())
    assert record.content_hash == hashlib.sha256(Path(record.path).read_bytes()).hexdigest()
    assert record.content_hash != receipt.response_hash
    assert "response_snapshot_sink" not in receipt.to_dict()
    assert _complete(runtime, {"replacement": "forbidden"}, admission=admission)[0] == receipt
    assert observed == [receipt.receipt_id]
    assert Path(runtime.ledger.path).read_bytes() == ledger_bytes


@pytest.mark.parametrize("stage", ["writer", "validator"])
def test_one_injected_sink_covers_all_stage_runtime_instances(capture_case, stage):
    case = capture_case
    store = _sink(case)
    service = _service(stage, case, sink=store)
    receipts = []
    for index in (1, 2):
        runtime = _runtime(stage, service, index=index)
        assert runtime.response_snapshot_sink is store
        receipt, _admission = _complete(runtime, {"unit": index})
        receipts.append(receipt)
        assert store.read_registered(receipt).job_id == case["workspace"].job_id

    assert len({receipt.receipt_id for receipt in receipts}) == 2
    assert len([row for row in case["registry"].list_records() if row.artifact_type == "provider_response_snapshot"]) == 2


@pytest.mark.parametrize("stage", ["writer", "validator"])
def test_legacy_service_runtime_does_not_create_snapshot_artifacts(capture_case, stage):
    case = capture_case
    service = _service(stage, case)
    runtime = _runtime(stage, service)
    receipt, _admission = _complete(runtime, {"legacy": "unchanged"})

    assert runtime.response_snapshot_sink is None
    assert receipt.response_hash == hash_json({"legacy": "unchanged"})
    assert case["registry"].list_records() == []


@pytest.mark.parametrize("stage", ["writer", "validator"])
def test_failed_unknown_outcome_is_never_captured_as_success(capture_case, stage):
    case = capture_case
    store = _sink(case)
    runtime = _runtime(stage, _service(stage, case, sink=store))
    receipt, _admission = _complete(runtime, {"untrusted": "partial output"}, failed=True)

    assert receipt.status == "failed"
    assert receipt.error_kind == "outcome_unknown"
    assert len(runtime.ledger.list_receipts()) == 1
    assert case["registry"].list_records() == []


def test_validator_capture_and_normalized_output_precede_later_report_augmentation(capture_case):
    case = capture_case
    store = _sink(case)
    service = _service("validator", case, sink=store)
    runtime = _runtime("validator", service)
    report = {"status": "supported", "confidence": 0.95}
    original = deepcopy(report)
    receipt, _admission = _complete(runtime, report)
    service.bind_provider_output(call_id=runtime.call_id, content=report)
    report.setdefault("adjudication_stage", "primary")
    report.setdefault("claim_type", "result")
    capsule = json.loads(Path(store.read_registered(receipt).path).read_bytes())
    output, = [row for row in case["registry"].list_records() if row.artifact_type == "validation_provider_output"]
    persisted = json.loads(Path(output.path).read_bytes())

    assert capsule["content"] == persisted["payload"] == original
    assert receipt.response_hash == hash_json(original)
    assert service._expected_provider_calls[runtime.call_id].normalized_output_hash == hash_json(original)
    assert hash_json(report) != receipt.response_hash


def test_legacy_custom_factory_receives_exact_original_parameter_set(capture_case):
    observed = []

    def factory(**kwargs):
        observed.append(set(kwargs))
        assert set(kwargs) == _LEGACY_FACTORY_KEYS
        return ProviderRuntime(**kwargs)

    service = _service("validator", capture_case, factory=factory)
    runtime = _runtime("validator", service)
    assert runtime.response_snapshot_sink is None
    assert observed == [_LEGACY_FACTORY_KEYS]


def test_opted_in_custom_factory_preserves_the_same_capture_sink(capture_case):
    store = _sink(capture_case)
    observed = []

    def factory(**kwargs):
        assert kwargs["response_snapshot_sink"] is store
        observed.append(set(kwargs))
        return ProviderRuntime(**kwargs)

    service = _service("validator", capture_case, sink=store, factory=factory)
    runtime = _runtime("validator", service)
    receipt, _admission = _complete(runtime, {"factory": "original"})

    assert store.read_registered(receipt)
    assert observed == [_LEGACY_FACTORY_KEYS | {"response_snapshot_sink"}]


@pytest.mark.parametrize("conflict", ["dropped", "replaced"])
def test_custom_factory_cannot_drop_or_replace_an_injected_sink(capture_case, conflict):
    store = _sink(capture_case)
    calls = []

    def factory(**kwargs):
        calls.append(1)
        if conflict == "dropped":
            kwargs.pop("response_snapshot_sink")
        else:
            kwargs["response_snapshot_sink"] = object()
        return ProviderRuntime(**kwargs)

    service = _service("validator", capture_case, sink=store, factory=factory)
    with pytest.raises(ProviderRuntimeContractError, match="did not preserve"):
        _runtime("validator", service)
    assert calls == [1]
    assert not service.provider_receipt_ledger.list_receipts()
    assert service._provider_runtimes == {}


def test_legacy_factory_owned_sink_is_preserved_without_new_factory_kwargs(capture_case):
    store = _sink(capture_case)

    def factory(**kwargs):
        assert set(kwargs) == _LEGACY_FACTORY_KEYS
        return ProviderRuntime(**kwargs, response_snapshot_sink=store)

    service = _service("validator", capture_case, factory=factory)
    runtime = _runtime("validator", service)
    assert service.response_snapshot_sink is None
    assert runtime.response_snapshot_sink is store

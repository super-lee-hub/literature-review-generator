from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import runtime.provider_response_snapshot as snapshots
from runtime.provider_response_snapshot import (
    ProviderResponseSnapshotError,
    ProviderResponseSnapshotMissing,
    ProviderResponseSnapshotStore,
    validate_provider_response_snapshot,
)
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderBudgetV1,
    ProviderRuntime,
    ProviderRuntimeLedger,
    hash_json,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import (
    LocalPublicationContext,
    PersistentQueueService,
    QueueJobSpec,
    QueuePublicationRejected,
    QueueState,
)


def _workspace(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path / "output"), "response-capsule", job_id="snapshot-job")
    context = LocalPublicationContext()
    return workspace, context.registry(workspace.paths.registry_path, workspace.job_id), context


def _successful_receipt(workspace, content):
    ledger = ProviderRuntimeLedger(workspace.log_path("original_provider_receipts.jsonl"))
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=2, max_total_tokens=100),
        aggregate_budget=ProviderBudgetController(ProviderAggregateBudgetV2(
            max_provider_calls_total=2, max_output_tokens_total=100,
            max_retry_attempts_total=0, max_wall_seconds=60,
        )),
        ledger=ledger, job_id=workspace.job_id, attempt_id="original-attempt",
        stage_name="stage3_review", route="Writer_API", node_id="section-U45",
        call_id="review:section-U45", endpoint_type="chat_completions",
    )
    admission = runtime.admit(estimated_tokens=3, requested_output_tokens=10)
    receipt = runtime.complete(
        admission=admission, prompt="source closed prompt", input_payload={"papers": [4, 5]},
        api_config={"provider_family": "openai_chat", "model": "rfs-writer",
                    "api_base": "http://127.0.0.1:1/v1", "api_key": "unit-test-key"},
        result={"status": "success", "content": content, "finish_reason": "stop",
                "input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        metadata={"transport_config": {"provider_family": "openai_chat", "model": "rfs-writer",
                                       "api_base": "http://127.0.0.1:1/v1", "endpoint_type": "chat_completions"}},
    )
    return receipt, ledger


@pytest.mark.parametrize("content", [
    "原始 JSON 内容 — café\n第二行", {"answer": [True, False, None, 4, 2.5, {"scope": "paper"}]},
    ["source", 3, False], 0, False,
])
def test_capture_retains_exact_value_and_original_hash_domains(tmp_path, content):
    workspace, registry, context = _workspace(tmp_path)
    receipt, ledger = _successful_receipt(workspace, content)
    original_dto = receipt.to_dict()
    original_dto_hash = hash_json(original_dto)
    ledger_before = Path(ledger.path).read_bytes()
    store = ProviderResponseSnapshotStore(workspace, registry, context)

    record = store.capture(receipt, content)
    payload = json.loads(Path(record.path).read_bytes())

    assert type(payload["content"]) is type(content)
    assert payload["content"] == content
    assert payload["response_hash"] == receipt.response_hash == hash_json(content)
    assert payload["receipt_hash"] == original_dto_hash
    assert record.content_hash == hashlib.sha256(Path(record.path).read_bytes()).hexdigest()
    assert record.content_hash != receipt.response_hash
    assert record.depends_on == []  # A future ledger/closure/current set cannot be an ancestor.
    assert receipt.to_dict() == original_dto
    assert hash_json(receipt.to_dict()) == original_dto_hash
    assert Path(ledger.path).read_bytes() == ledger_before
    validate_provider_response_snapshot(payload)


def test_exact_capture_is_immutable_and_read_only_lookup_does_not_mutate(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"b": 2, "a": {"x": 1}})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"b": 2, "a": {"x": 1}})
    before_registry = Path(registry.registry_path).read_bytes()
    before_files = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}

    assert store.capture(receipt, {"a": {"x": 1}, "b": 2}) == record
    assert store.read_registered(receipt) == record
    assert store.require(receipt) == record
    assert Path(registry.registry_path).read_bytes() == before_registry
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before_files


@pytest.mark.parametrize("mutation", [
    {"job_id": "foreign-job"}, {"test_only": True},
    {"status": "failed", "error_kind": "outcome_unknown"}, {"finish_reason": "length"},
    {"finish_reason": "max_output_tokens"},
    {"incomplete_reason": "truncated"}, {"timeout_kind": "read_timeout"},
    {"response_hash": None}, {"logical_attempt_identity": ""},
    {"http_status": 503},
])
def test_capture_rejects_foreign_failed_unknown_or_incomplete_receipts(tmp_path, mutation):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with pytest.raises(ProviderResponseSnapshotError):
        store.capture(replace(receipt, **mutation), {"answer": "known"})
    assert registry.list_records() == []
    assert not list(Path(workspace.paths.logs_dir).glob("provider_response_snapshots/**/*.json"))


@pytest.mark.parametrize("content", [(1, 2), {1: "non-string key"}, {"bad": float("nan")}, {"set": {1, 2}}])
def test_non_json_value_cannot_enter_hash_json_repr_fallback(tmp_path, content):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with pytest.raises(ProviderResponseSnapshotError):
        store.capture(replace(receipt, response_hash=hash_json(content)), content)
    assert registry.list_records() == []


def test_raw_capture_retains_unknown_usage_without_claiming_receipt_authority(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    receipt = replace(receipt, usage_status="unreported", input_tokens=None, output_tokens=None, total_tokens=None)
    record = ProviderResponseSnapshotStore(workspace, registry, context).capture(receipt, {"answer": "known"})
    assert json.loads(Path(record.path).read_bytes())["receipt"]["usage_status"] == "unreported"
    assert record.depends_on == []  # Original ledger/closure verification remains a separate obligation.


@pytest.mark.parametrize("corruption", ["unknown_root", "foreign_identity", "response", "receipt_extra", "receipt_hash"])
def test_snapshot_schema_and_recorded_hashes_cannot_be_forged(tmp_path, corruption):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    record = ProviderResponseSnapshotStore(workspace, registry, context).capture(receipt, {"answer": "known"})
    payload = json.loads(Path(record.path).read_bytes())
    if corruption == "unknown_root":
        payload["trusted"] = True
    elif corruption == "foreign_identity":
        payload["job_id"] = "foreign-job"
    elif corruption == "response":
        payload["content"] = {"answer": "different"}
    elif corruption == "receipt_extra":
        payload["receipt"]["ignored_unknown_field"] = "forged"
        payload["receipt_hash"] = hash_json(payload["receipt"])
    else:
        payload["receipt_hash"] = "f" * 64
    with pytest.raises(ProviderResponseSnapshotError):
        validate_provider_response_snapshot(payload)


def test_registered_tampering_is_blocked_not_repaired_from_staged_copy(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "known"})
    Path(record.path).write_bytes(Path(record.path).read_bytes() + b"\n")
    with pytest.raises(ProviderResponseSnapshotError, match="hash"):
        store.require(receipt)
    with pytest.raises(ProviderResponseSnapshotError, match="hash"):
        store.capture(receipt, {"answer": "known"})


def test_retained_known_response_recovers_after_registry_publication_failure(tmp_path, monkeypatch):
    workspace, registry, context = _workspace(tmp_path)
    receipt, ledger = _successful_receipt(workspace, {"answer": "retained"})
    ledger_before = Path(ledger.path).read_bytes()
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with monkeypatch.context() as injected:
        def fail_publish(*args, **kwargs):
            raise RuntimeError("fault after private bytes before Registry")
        injected.setattr(snapshots, "publish_json_artifact", fail_publish)
        with pytest.raises(ProviderResponseSnapshotError, match="retained"):
            store.capture(receipt, {"answer": "retained"})
    assert registry.list_records() == []
    assert len(list(Path(workspace.paths.logs_dir).glob("provider_response_snapshots/**/*.json"))) == 1
    record = store.require(receipt)
    assert json.loads(Path(record.path).read_bytes())["content"] == {"answer": "retained"}
    assert Path(ledger.path).read_bytes() == ledger_before


def test_capture_filesystem_failure_is_nonretryable_and_keeps_original_receipt(tmp_path, monkeypatch):
    workspace, registry, context = _workspace(tmp_path)
    receipt, ledger = _successful_receipt(workspace, {"answer": "known"})
    ledger_before = Path(ledger.path).read_bytes()
    def deny_private_write(*args, **kwargs):
        raise PermissionError("private response disk unavailable")
    monkeypatch.setattr(snapshots, "atomic_write_json", deny_private_write)
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with pytest.raises(ProviderResponseSnapshotError, match="reconciliation") as blocked:
        store.capture(receipt, {"answer": "known"})
    assert isinstance(blocked.value.__cause__, PermissionError)
    assert Path(ledger.path).read_bytes() == ledger_before
    with pytest.raises(ProviderResponseSnapshotMissing):
        store.require(receipt)


def test_missing_known_response_is_blocking_and_parent_read_never_repairs(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "missing"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    before = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}
    with pytest.raises(ProviderResponseSnapshotMissing):
        store.read_registered(receipt)
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before
    with pytest.raises(ProviderResponseSnapshotMissing, match="reconciliation"):
        store.require(receipt)
    assert registry.list_records() == []


def test_real_queue_fence_retains_reply_then_new_lease_publishes_it(tmp_path):
    workspace, _registry, _context = _workspace(tmp_path)
    receipt, ledger = _successful_receipt(workspace, {"answer": "lease-retained"})
    ledger_before = Path(ledger.path).read_bytes()
    queue = PersistentQueueService(tmp_path / "queue.json")
    queue.add_job(QueueJobSpec(job_id=workspace.job_id, job_type="review", project_name=workspace.project_name))
    lease = queue.claim_job(workspace.job_id, worker_id="old-worker", lease_seconds=30)
    assert lease is not None
    old_context = queue.publication_context(lease)
    registry = old_context.registry(workspace.paths.registry_path, workspace.job_id)
    old_store = ProviderResponseSnapshotStore(workspace, registry, old_context)
    assert queue.release_lease(
        workspace.job_id, lease_id=lease.lease_id, worker_id=lease.worker_id,
        lease_generation=lease.lease_generation, fence_token=lease.fence_token, state=QueueState.FAILED,
    )
    with pytest.raises(ProviderResponseSnapshotError) as failed:
        old_store.capture(receipt, {"answer": "lease-retained"})
    assert isinstance(failed.value.__cause__, QueuePublicationRejected)
    assert registry.list_records() == []
    assert queue.retry_failed_jobs() == [workspace.job_id]
    new_lease = queue.claim_job(workspace.job_id, worker_id="new-worker", lease_seconds=30)
    assert new_lease is not None
    new_context = queue.publication_context(new_lease)
    new_store = ProviderResponseSnapshotStore(
        workspace, new_context.registry(workspace.paths.registry_path, workspace.job_id), new_context,
    )
    record = new_store.require(receipt)
    assert record.metadata["publication_fence"]["lease_generation"] == new_lease.lease_generation
    assert record.depends_on == []
    assert Path(ledger.path).read_bytes() == ledger_before


@pytest.mark.parametrize("mutation", ["bytes", "metadata", "future_dependency"])
def test_late_selected_binding_mutations_are_rejected(tmp_path, monkeypatch, mutation):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "known"})
    if mutation == "future_dependency":
        registry.update_record(record.artifact_id, depends_on=[ArtifactDependencyRefV2.from_record(record)])
        with pytest.raises(ProviderResponseSnapshotError, match="binding"):
            store.read_registered(receipt)
        return
    original_read = snapshots._read_payload
    def mutate_after_read(path):
        result = original_read(path)
        if mutation == "bytes":
            Path(record.path).write_bytes(Path(record.path).read_bytes() + b"\n")
        else:
            registry.update_record(record.artifact_id, metadata_updates={"late_change": True})
        return result
    monkeypatch.setattr(snapshots, "_read_payload", mutate_after_read)
    with pytest.raises(ProviderResponseSnapshotError, match="changed"):
        store.read_registered(receipt)


def test_changed_receipt_row_cannot_rebind_original_snapshot(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "original"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "original"})
    original_bytes = Path(record.path).read_bytes()
    changed = replace(receipt, response_hash=hash_json({"answer": "different"}))
    with pytest.raises(ProviderResponseSnapshotError):
        store.capture(changed, {"answer": "different"})
    assert Path(record.path).read_bytes() == original_bytes


def test_circular_content_is_blocked_without_hash_repr_capture(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    circular = []
    circular.append(circular)
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with pytest.raises(ProviderResponseSnapshotError, match="circular"):
        store.capture(replace(receipt, response_hash=hash_json(circular)), circular)
    assert registry.list_records() == []


def test_unrelated_registry_append_does_not_invalidate_original_capsule(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "known"})
    unrelated = Path(workspace.artifact_path("unrelated-report.json"))
    unrelated.write_text('{"annotation":"new"}', encoding="utf-8")
    registry.register_file(
        artifact_id="unrelated-report", artifact_role="test", artifact_type="test_node",
        artifact_version="v1", path=unrelated, producer="tests",
    )
    before = Path(registry.registry_path).read_bytes()
    assert store.read_registered(receipt) == record
    assert Path(registry.registry_path).read_bytes() == before


def test_read_registered_does_not_create_a_missing_registry_lock(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "known"})
    lock = Path(registry.lock_path)
    assert lock.is_file()
    lock.unlink()
    assert store.read_registered(receipt) == record
    assert not lock.exists()


@pytest.mark.parametrize("corruption", ["duplicate_key", "ambiguous_capture"])
def test_private_capture_ambiguity_is_not_a_provider_cache_miss(tmp_path, monkeypatch, corruption):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "retained"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    with monkeypatch.context() as injected:
        def fail_publish(*args, **kwargs):
            raise RuntimeError("fault before Registry")
        injected.setattr(snapshots, "publish_json_artifact", fail_publish)
        with pytest.raises(ProviderResponseSnapshotError):
            store.capture(receipt, {"answer": "retained"})
    staged = list(Path(workspace.paths.logs_dir).glob("provider_response_snapshots/**/*.json"))
    assert len(staged) == 1
    path = staged[0]
    if corruption == "duplicate_key":
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("{", '{"receipt_id":"duplicate",', 1), encoding="utf-8")
    else:
        path.with_name("second.json").write_bytes(path.read_bytes())
    with pytest.raises(ProviderResponseSnapshotError):
        store.require(receipt)
    assert registry.list_records() == []


def test_store_cannot_publish_into_another_workspace_registry(tmp_path):
    workspace, _registry, context = _workspace(tmp_path)
    foreign = ArtifactRegistry(tmp_path / "foreign-registry.json", "foreign-job")
    with pytest.raises(ProviderResponseSnapshotError, match="ownership"):
        ProviderResponseSnapshotStore(workspace, foreign, context)


def test_later_stage_report_augmentation_cannot_mutate_original_content(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    report = {"judgements": [{"claim_id": "U4", "status": "supported"}]}
    receipt, _ledger = _successful_receipt(workspace, report)
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, report)
    original = Path(record.path).read_bytes()
    report.setdefault("current_source_binding", "post-normalization-data")
    report["judgements"][0]["current_occurrence"] = "child-local-row"
    assert store.read_registered(receipt) == record
    assert Path(record.path).read_bytes() == original
    assert json.loads(original)["content"] == {"judgements": [{"claim_id": "U4", "status": "supported"}]}


def test_epoch_retag_is_not_an_implicit_snapshot_rebinding(tmp_path):
    workspace, registry, context = _workspace(tmp_path)
    receipt, _ledger = _successful_receipt(workspace, {"answer": "known"})
    store = ProviderResponseSnapshotStore(workspace, registry, context)
    record = store.capture(receipt, {"answer": "known"})
    retagged = replace(receipt, closure_epoch_id="new-epoch", metadata={
        **dict(receipt.metadata), "closure_epoch_retagged_from": receipt.closure_epoch_id,
    })
    original = Path(record.path).read_bytes()
    with pytest.raises(ProviderResponseSnapshotError, match="hash"):
        store.require(retagged)
    assert Path(record.path).read_bytes() == original

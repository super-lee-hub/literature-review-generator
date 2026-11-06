from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from runtime import provider_runtime
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    ProviderCallReceiptV1,
    hash_json,
)
from services import review_generation_service
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
from services.queue_service import (
    PersistentQueueService,
    QueueJobSpec,
    QueuePublicationRejected,
    QueueState,
)
from services.review_generation_service import ReviewGenerationService
from tests.test_review_writer_stage_preflight import (
    _successful_writer_result,
    _writer_fixture,
)


def _interrupt_after_first_section(tmp_path) -> tuple[ReviewGenerationService, dict[str, Any], list[dict[str, Any]]]:
    first_calls: list[int] = []

    def first_writer(**kwargs: Any) -> Mapping[str, Any]:
        number = int(kwargs["section_number"])
        first_calls.append(number)
        return _successful_writer_result(**kwargs)

    service, outline, packets = _writer_fixture(tmp_path, writer=first_writer)
    cancellation_checks = 0

    def cancel_before_second_section() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise RuntimeError("cancelled before second section")

    service.cancellation_checker = cancel_before_second_section
    with pytest.raises(RuntimeError, match="cancelled before second section"):
        service.run(outline_payload=outline, evidence_packets=packets)
    assert first_calls == [1]
    return service, outline, packets


def _new_service(
    original: ReviewGenerationService,
    *,
    attempt_id: str,
    writer,
    artifact_registry: ArtifactRegistry | None = None,
    publication_context: Any | None = None,
) -> ReviewGenerationService:
    return ReviewGenerationService(
        job_id=original.job_id,
        attempt_id=attempt_id,
        workspace=original.workspace,
        artifact_registry=artifact_registry or ArtifactRegistry(
            original.workspace.paths.registry_path,
            original.job_id,
        ),
        settings=original.settings,
        summaries=original.summaries,
        writer=writer,
        publication_context=publication_context,
    )


def _bind_explicit_local_writer_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> ProviderBudgetController:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV2(
            max_provider_calls_total=64,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=8,
            max_wall_seconds=3_600,
        )
    )
    monkeypatch.setattr(
        review_generation_service,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    monkeypatch.setattr(
        provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: controller,
    )
    return controller


def _start_loopback_writer_server():
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/v1/responses":
                self.send_error(404, "unexpected Writer endpoint")
                return
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            prompt = request["input"][1]["content"][0]["text"]
            section_id = str(json.loads(prompt)["section"]["section_id"])
            requests.append({"section_id": section_id, "request": request})
            writer_result = _successful_writer_result(prompt_text=prompt)
            response = {
                "id": f"resp-{len(requests)}",
                "object": "response",
                "status": "completed",
                "model": str(request.get("model") or "writer"),
                "output_text": json.dumps(writer_result["content"], ensure_ascii=False),
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "total_tokens": 160,
                },
            }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def _configure_loopback_writer(service: ReviewGenerationService, server: ThreadingHTTPServer) -> None:
    sections = dict(service.settings.sections)
    writer_config = dict(sections["Writer_API"])
    writer_config.update(
        {
            "api_base": f"http://127.0.0.1:{server.server_port}/v1",
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
        }
    )
    sections["Writer_API"] = writer_config
    service.settings = replace(service.settings, sections=sections)


def _run_actual_writer_partial_attempt(
    tmp_path,
    server: ThreadingHTTPServer,
) -> tuple[ReviewGenerationService, dict[str, Any], list[dict[str, Any]]]:
    service, outline, packets = _writer_fixture(tmp_path, writer=None)
    _configure_loopback_writer(service, server)
    cancellation_checks = 0

    def cancel_before_second_section() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise RuntimeError("cancelled before second section")

    service.cancellation_checker = cancel_before_second_section
    with pytest.raises(RuntimeError, match="cancelled before second section"):
        service.run(outline_payload=outline, evidence_packets=packets)
    return service, outline, packets


def _snapshot_record(service: ReviewGenerationService, section_id: str = "section_1"):
    matches = [
        record
        for record in service.registry.list_records()
        if record.artifact_role == "provider_receipt_snapshot"
        and str((record.metadata or {}).get("section_id") or "") == section_id
    ]
    assert len(matches) == 1
    return matches[0]


def _republish_replay_rows(
    service: ReviewGenerationService,
    rows: list[dict[str, Any]],
) -> Any:
    current = service.registry.get("review_replay")
    assert current is not None
    encoded = (
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
    ).encode("utf-8")
    return publish_bytes_artifact(
        service.publication_context,
        service.registry,
        service._review_replay_path(),
        encoded,
        artifact_role="review_replay",
        artifact_type="review_replay_ledger",
        artifact_version="v1",
        producer="tests.test_run_first_writer_cross_attempt_replay",
        artifact_id="review_replay",
        depends_on=list(current.depends_on),
        metadata=dict(current.metadata or {}),
    )


def _publish_review_terminal(service: ReviewGenerationService, closure_record: Any):
    terminal_payload = {
        "status": "succeeded",
        "stage_name": "stage3_review",
        "model_call_count": 1,
        "output_artifact_refs": [
            {
                "artifact_id": closure_record.artifact_id,
                "artifact_type": closure_record.artifact_type,
                "content_hash": closure_record.content_hash,
                "job_id": service.job_id,
                "path": closure_record.path,
            }
        ],
    }
    terminal_record = publish_json_artifact(
        service.publication_context,
        service.registry,
        service.workspace.artifact_path("review-terminal-stage.json"),
        {
            "artifact_type": "terminal_stage_record",
            "artifact_version": "v1",
            "job_id": service.job_id,
            **terminal_payload,
        },
        artifact_role="terminal_stage_record",
        artifact_type="terminal_stage_record",
        artifact_version="v1",
        producer=__name__,
        artifact_id="test:review:terminal",
        depends_on=[ArtifactDependencyRefV2.from_record(closure_record)],
    )
    return terminal_record, terminal_payload


def _rewrite_replay_snapshot_receipt(
    service: ReviewGenerationService,
    *,
    receipt_field: str,
    replacement: str,
) -> None:
    replay_record = service.registry.get("review_replay")
    assert replay_record is not None
    replay_rows = [
        json.loads(line)
        for line in Path(replay_record.path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replay = next(row for row in replay_rows if row["section_id"] == "section_1")
    old_snapshot = service.registry.get(replay["receipt_snapshot_artifact_id"])
    assert old_snapshot is not None
    old_snapshot_payload = json.loads(Path(old_snapshot.path).read_text(encoding="utf-8"))
    receipt_payload = dict(old_snapshot_payload["receipt"])
    receipt_payload[receipt_field] = replacement
    receipt = ProviderCallReceiptV1.from_dict(receipt_payload)

    section_record = service.registry.get(replay["artifact_id"])
    assert section_record is not None
    section_envelope = json.loads(Path(section_record.path).read_text(encoding="utf-8"))
    binding = section_envelope["binding"]
    snapshot = service._persist_receipt_snapshot(
        section_id="section_1",
        binding=binding,
        receipt=receipt,
    )
    replay.update(
        {
            "receipt_hash": hash_json(receipt.to_dict()),
            "receipt_snapshot_artifact_id": snapshot.artifact_id,
            "receipt_snapshot_path": snapshot.path,
            "receipt_snapshot_content_hash": snapshot.content_hash,
        }
    )
    encoded = (
        json.dumps(replay, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    dependencies = [
        dependency
        for dependency in replay_record.depends_on
        if dependency.artifact_id != old_snapshot.artifact_id
    ]
    dependencies.append(ArtifactDependencyRefV2.from_record(snapshot))
    publish_bytes_artifact(
        service.publication_context,
        service.registry,
        service._review_replay_path(),
        encoded,
        artifact_role="review_replay",
        artifact_type="review_replay_ledger",
        artifact_version="v1",
        producer="tests.test_run_first_writer_cross_attempt_replay",
        artifact_id="review_replay",
        depends_on=dependencies,
        metadata=dict(replay_record.metadata or {}),
    )


def test_cancelled_writer_section_replays_in_fresh_attempt_without_duplicate_call(
    tmp_path,
) -> None:
    first_calls: list[int] = []

    def first_writer(**kwargs: Any) -> Mapping[str, Any]:
        number = int(kwargs["section_number"])
        first_calls.append(number)
        return _successful_writer_result(**kwargs)

    first_service, outline, packets = _writer_fixture(tmp_path, writer=first_writer)
    cancellation_checks = 0

    def cancel_before_second_section() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise RuntimeError("cancelled before second section")

    first_service.cancellation_checker = cancel_before_second_section
    with pytest.raises(RuntimeError, match="cancelled before second section"):
        first_service.run(outline_payload=outline, evidence_packets=packets)
    assert first_calls == [1]

    resumed_calls: list[int] = []

    def resumed_writer(**kwargs: Any) -> Mapping[str, Any]:
        resumed_calls.append(int(kwargs["section_number"]))
        return _successful_writer_result(**kwargs)

    resumed_service = ReviewGenerationService(
        job_id=first_service.job_id,
        attempt_id="writer-preflight-resume-attempt",
        workspace=first_service.workspace,
        artifact_registry=ArtifactRegistry(
            first_service.workspace.paths.registry_path,
            first_service.job_id,
        ),
        settings=first_service.settings,
        summaries=first_service.summaries,
        writer=resumed_writer,
    )
    result = resumed_service.run(outline_payload=outline, evidence_packets=packets)

    assert resumed_calls == [2]
    assert len(result.sections) == 2
    assert resumed_service.provider_request_inventory is not None
    assert resumed_service.provider_request_inventory.requests[0].verified_reuse
    assert not resumed_service.provider_request_inventory.requests[1].verified_reuse
    closure_record = resumed_service.registry.get("review:provider_receipt_closure")
    assert closure_record is not None
    import json
    from pathlib import Path

    closure = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure["complete"] is True
    assert "review:section_1" in closure["verified_reuse_call_ids"]


def test_partial_snapshot_binds_receipt_and_fresh_attempt_reuses_without_reposting(
    tmp_path,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    closure_record = first_service.registry.get("review:provider_receipt_closure")
    assert closure_record is None

    snapshot_record = _snapshot_record(first_service)
    snapshot = json.loads(Path(snapshot_record.path).read_text(encoding="utf-8"))
    replay_record = first_service.registry.get("review_replay")
    assert replay_record is not None
    replay = next(
        json.loads(line)
        for line in Path(replay_record.path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    section_record = first_service.registry.get(replay["artifact_id"])
    assert section_record is not None
    section = json.loads(Path(section_record.path).read_text(encoding="utf-8"))
    receipt = snapshot["receipt"]

    assert snapshot["status"] == "partial"
    assert "complete" not in snapshot
    assert snapshot["job_id"] == first_service.job_id
    assert snapshot["stage_name"] == "stage3_review"
    assert snapshot["section_id"] == "section_1"
    assert snapshot["binding_hash"] == hash_json(section["binding"])
    assert snapshot["receipt_hash"] == hash_json(receipt)
    assert snapshot["receipt_id"] == receipt["receipt_id"] == replay["receipt_id"]
    assert snapshot["call_id"] == receipt["call_id"] == "review:section_1"
    assert receipt["job_id"] == first_service.job_id
    assert receipt["stage_name"] == "stage3_review"
    assert receipt["node_id"] == "section_1"
    assert receipt["input_hash"] == section["binding"]["prompt_payload_hash"]
    assert receipt["route"] == "Writer_API"
    assert receipt["provider"] == "generic"
    assert receipt["model"] == "writer"
    assert receipt["endpoint_type"] == "responses"
    assert receipt["usage_status"] in {"reported", "provider_not_supported"}
    assert replay["receipt_snapshot_artifact_id"] == snapshot_record.artifact_id
    assert replay["receipt_snapshot_path"] == snapshot_record.path
    assert replay["receipt_snapshot_content_hash"] == snapshot_record.content_hash
    assert replay["expected_call_graph_hash"] == first_service.expected_call_graph_hash
    assert snapshot["expected_call_graph_hash"] == replay["expected_call_graph_hash"]

    resumed_calls: list[int] = []

    def resumed_writer(**kwargs: Any) -> Mapping[str, Any]:
        resumed_calls.append(int(kwargs["section_number"]))
        return _successful_writer_result(**kwargs)

    resumed_service = _new_service(
        first_service,
        attempt_id="writer-snapshot-binding-resume-attempt",
        writer=resumed_writer,
    )
    result = resumed_service.run(outline_payload=outline, evidence_packets=packets)

    assert resumed_calls == [2]
    assert len(result.sections) == 2
    assert resumed_service.provider_request_inventory is not None
    assert resumed_service.provider_request_inventory.requests[0].verified_reuse
    closure_record = resumed_service.registry.get("review:provider_receipt_closure")
    assert closure_record is not None
    closure = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure["complete"] is True
    assert "review:section_1" in closure["verified_reuse_call_ids"]


def test_same_binding_replay_fails_closed_when_snapshot_is_missing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    snapshot_record = _snapshot_record(first_service)
    resumed_calls: list[int] = []
    resumed_service = _new_service(
        first_service,
        attempt_id="writer-missing-snapshot-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
    )
    original_get = ArtifactRegistry.get

    def hide_snapshot(registry: ArtifactRegistry, artifact_id: str):
        if registry.job_id == first_service.job_id and artifact_id == snapshot_record.artifact_id:
            return None
        return original_get(registry, artifact_id)

    monkeypatch.setattr(ArtifactRegistry, "get", hide_snapshot)
    with pytest.raises(RuntimeError, match="receipt snapshot Registry reference is missing"):
        resumed_service.run(outline_payload=outline, evidence_packets=packets)
    assert resumed_calls == []


def test_same_binding_replay_fails_closed_when_snapshot_bytes_are_tampered(
    tmp_path,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    snapshot_record = _snapshot_record(first_service)
    snapshot = json.loads(Path(snapshot_record.path).read_text(encoding="utf-8"))
    snapshot["receipt"]["output_tokens"] = int(snapshot["receipt"].get("output_tokens") or 0) + 1
    Path(snapshot_record.path).write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    resumed_calls: list[int] = []
    resumed_service = _new_service(
        first_service,
        attempt_id="writer-tampered-snapshot-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
    )

    with pytest.raises(RuntimeError, match="receipt snapshot bytes changed"):
        resumed_service.run(outline_payload=outline, evidence_packets=packets)
    assert resumed_calls == []


def test_same_binding_replay_fails_closed_for_foreign_snapshot_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    snapshot_record = _snapshot_record(first_service)
    resumed_calls: list[int] = []
    resumed_service = _new_service(
        first_service,
        attempt_id="writer-foreign-snapshot-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
    )
    original_get = ArtifactRegistry.get

    def foreign_snapshot(registry: ArtifactRegistry, artifact_id: str):
        record = original_get(registry, artifact_id)
        if record is not None and artifact_id == snapshot_record.artifact_id:
            return replace(record, job_id="foreign-job")
        return record

    monkeypatch.setattr(ArtifactRegistry, "get", foreign_snapshot)
    with pytest.raises(RuntimeError, match="receipt snapshot Registry reference is missing, foreign, or changed"):
        resumed_service.run(outline_payload=outline, evidence_packets=packets)
    assert resumed_calls == []


@pytest.mark.parametrize(
    ("receipt_field", "replacement"),
    (("schema_hash", "f" * 64), ("route", "Unexpected_Writer_Route")),
)
def test_same_binding_replay_rejects_receipt_request_or_route_mismatch(
    tmp_path,
    receipt_field: str,
    replacement: str,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    _rewrite_replay_snapshot_receipt(
        first_service,
        receipt_field=receipt_field,
        replacement=replacement,
    )
    resumed_calls: list[int] = []
    resumed_service = _new_service(
        first_service,
        attempt_id=f"writer-{receipt_field}-mismatch-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
    )

    with pytest.raises(RuntimeError, match="provider receipt identity or request mismatch"):
        resumed_service.run(outline_payload=outline, evidence_packets=packets)
    assert resumed_calls == []


def test_changed_overall_graph_reuses_unchanged_section_only(
    tmp_path,
) -> None:
    first_service, outline, packets = _interrupt_after_first_section(tmp_path)
    single_section_outline = {**outline, "sections": list(outline["sections"][:1])}
    resumed_calls: list[int] = []
    resumed_service = _new_service(
        first_service,
        attempt_id="writer-changed-graph-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
    )

    result = resumed_service.run(
        outline_payload=single_section_outline,
        evidence_packets=packets[:1],
    )

    assert resumed_service.expected_call_graph_hash != first_service.expected_call_graph_hash
    assert resumed_calls == []
    assert len(result.sections) == 1
    assert resumed_service.provider_request_inventory is not None
    assert resumed_service.provider_request_inventory.requests[0].verified_reuse
    closure_record = resumed_service.registry.get("review:provider_receipt_closure")
    assert closure_record is not None
    closure = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure["complete"] is True
    assert closure["verified_reuse_call_ids"] == ["review:section_1"]


def test_mixed_epoch_retry_closure_is_deterministic_when_receipt_times_tie(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_timestamp = "2026-10-16T00:00:00Z"
    monkeypatch.setattr("runtime.provider_runtime.utc_now_iso", lambda: fixed_timestamp)
    first_calls: list[int] = []

    def fail_second(**kwargs: Any) -> Mapping[str, Any]:
        number = int(kwargs["section_number"])
        first_calls.append(number)
        if number == 2:
            raise RuntimeError("simulated second-section crash")
        return _successful_writer_result(**kwargs)

    service, outline, packets = _writer_fixture(tmp_path, writer=fail_second)
    with pytest.raises(RuntimeError, match="simulated second-section crash"):
        service.run(outline_payload=outline, evidence_packets=packets)
    first_epoch = service.closure_epoch_id
    resumed_packets = [dict(packet) for packet in packets]
    revised_claim = "The revised second-section claim changes only its request."
    resumed_packets[1]["planned_claims"] = [revised_claim]
    claim_support = resumed_packets[1].get("claim_support")
    assert isinstance(claim_support, list) and len(claim_support) == 1
    assert isinstance(claim_support[0], dict)
    claim_support[0]["claim"] = revised_claim
    resumed_calls: list[int] = []

    service.writer = lambda **kwargs: resumed_calls.append(int(kwargs["section_number"])) or _successful_writer_result(**kwargs)
    service.run(outline_payload=outline, evidence_packets=resumed_packets)

    receipts = list(service.receipt_ledger.list_receipts())
    old_failed = next(
        receipt for receipt in receipts if receipt.call_id == "review:section_2" and receipt.status == "failed"
    )
    current_success = next(
        receipt for receipt in receipts if receipt.call_id == "review:section_2" and receipt.status == "success"
    )
    assert first_epoch == old_failed.closure_epoch_id
    assert service.closure_epoch_id == current_success.closure_epoch_id
    assert old_failed.input_hash != current_success.input_hash
    assert old_failed.prompt_hash != current_success.prompt_hash
    assert old_failed.finished_at == current_success.finished_at == fixed_timestamp
    assert old_failed.sequence == current_success.sequence
    assert old_failed.attempts == current_success.attempts
    assert resumed_calls == [2]

    closure_record = service.registry.get("review:provider_receipt_closure")
    assert closure_record is not None
    closure = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))["payload"]
    assert closure["complete"] is True
    assert "review:section_1" in closure["verified_reuse_call_ids"]


def test_stale_publication_fence_cannot_publish_replay_completion(
    tmp_path,
) -> None:
    writer_calls: list[int] = []

    def writer(**kwargs: Any) -> Mapping[str, Any]:
        writer_calls.append(int(kwargs["section_number"]))
        return _successful_writer_result(**kwargs)

    first_service, outline, packets = _writer_fixture(tmp_path, writer=writer)
    single_section_outline = {**outline, "sections": list(outline["sections"][:1])}
    first_service.run(outline_payload=single_section_outline, evidence_packets=packets[:1])
    assert writer_calls == [1]
    previous_closure = first_service.registry.get("review:provider_receipt_closure")
    assert previous_closure is not None

    queue = PersistentQueueService(tmp_path / "writer-queue.json")
    queue.add_job(
        QueueJobSpec(
            job_id=first_service.job_id,
            job_type="review",
            project_name="writer-replay-fence",
        )
    )
    lease = queue.claim_job(first_service.job_id, worker_id="writer-resume", lease_seconds=30)
    assert lease is not None
    publication_context = queue.publication_context(lease)
    fenced_registry = publication_context.registry(
        first_service.workspace.paths.registry_path,
        first_service.job_id,
    )
    resumed_calls: list[int] = []

    def expire_before_final_publication() -> None:
        assert queue.release_lease(
            first_service.job_id,
            lease_id=lease.lease_id,
            worker_id=lease.worker_id,
            lease_generation=lease.lease_generation,
            fence_token=lease.fence_token,
            state=QueueState.FAILED,
            error_message="test invalidated the publication lease",
        )

    resumed_service = _new_service(
        first_service,
        attempt_id="writer-stale-fence-resume-attempt",
        writer=lambda **kwargs: resumed_calls.append(int(kwargs["section_number"]))
        or _successful_writer_result(**kwargs),
        artifact_registry=fenced_registry,
        publication_context=publication_context,
    )
    resumed_service.cancellation_checker = expire_before_final_publication

    with pytest.raises(QueuePublicationRejected):
        resumed_service.run(
            outline_payload=single_section_outline,
            evidence_packets=packets[:1],
        )

    assert resumed_calls == []
    current_closure = first_service.registry.get("review:provider_receipt_closure")
    assert current_closure is not None
    assert current_closure.content_hash == previous_closure.content_hash
    previous_payload = json.loads(Path(previous_closure.path).read_text(encoding="utf-8"))["payload"]
    assert previous_payload["attempt_id"] == first_service.attempt_id


def test_instrumented_http_writer_replay_closes_review_stage_with_per_call_authority(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from validation.closure import _provider_closure_entry

    _bind_explicit_local_writer_budget(monkeypatch)
    server, server_thread, requests = _start_loopback_writer_server()
    try:
        first_service, outline, packets = _run_actual_writer_partial_attempt(tmp_path, server)
        assert [request["section_id"] for request in requests] == ["section_1"]
        first_receipts = list(first_service.receipt_ledger.list_receipts())
        assert len(first_receipts) == 1
        assert first_receipts[0].call_id == "review:section_1"
        assert first_receipts[0].status == "success"
        assert first_receipts[0].test_only is False
        assert first_service.registry.get("review:provider_receipt_closure") is None

        resumed_service = _new_service(
            first_service,
            attempt_id="writer-http-resume-attempt",
            writer=None,
        )
        result = resumed_service.run(outline_payload=outline, evidence_packets=packets)

        assert [request["section_id"] for request in requests] == ["section_1", "section_2"]
        assert len(result.sections) == 2
        assert resumed_service.provider_request_inventory is not None
        rows = resumed_service.provider_request_inventory.requests
        assert rows[0].verified_reuse
        assert not rows[1].verified_reuse
        receipts = list(resumed_service.receipt_ledger.list_receipts())
        assert {receipt.call_id for receipt in receipts} == {
            "review:section_1",
            "review:section_2",
        }
        assert all(receipt.test_only is False for receipt in receipts)
        assert all(receipt.status == "success" for receipt in receipts)
        assert resumed_service.closure_epoch_id != first_service.closure_epoch_id

        closure_record = resumed_service.registry.get("review:provider_receipt_closure")
        assert closure_record is not None
        closure_document = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
        closure_payload = closure_document["payload"]
        assert closure_payload["complete"] is True
        assert closure_payload["test_only"] is False
        assert closure_payload["verified_reuse_call_ids"] == ["review:section_1"]
        assert "review:section_2" in closure_payload["observed_call_ids"]
        assert closure_payload["expected_calls"][0]["closure_epoch_id"] == first_service.closure_epoch_id
        assert closure_payload["expected_calls"][0]["expected_call_graph_hash"] == first_service.expected_call_graph_hash
        assert closure_payload["expected_calls"][1]["closure_epoch_id"] == resumed_service.closure_epoch_id
        resumed_service.registry.verify_ready_artifact_closure(closure_record)

        terminal_record, terminal_payload = _publish_review_terminal(
            resumed_service,
            closure_record,
        )
        entry, blocking = _provider_closure_entry(
            "review",
            closure_record,
            terminal_record,
            terminal_payload,
            resumed_service.registry,
        )

        assert blocking == [], {"entry": entry, "blocking": blocking}
        assert entry["complete"] is True
        assert entry["status"] == "complete"
        assert entry["provider_closure_required"] is True
        assert entry["expected_call_ids"] == ["review:section_1", "review:section_2"]
        assert entry["observed_call_ids"] == ["review:section_2"]
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.fixture
def _verified_writer_reuse_pair(tmp_path, monkeypatch):
    """Two actual HTTP receipts, followed by two source-bound replay calls."""
    _bind_explicit_local_writer_budget(monkeypatch)
    server, thread, requests = _start_loopback_writer_server()
    try:
        initial, outline, packets = _writer_fixture(tmp_path, writer=None)
        _configure_loopback_writer(initial, server)
        initial.run(outline_payload=outline, evidence_packets=packets)
        resumed = _new_service(initial, attempt_id="two-reuse-scope", writer=None)
        resumed.run(outline_payload=outline, evidence_packets=packets)
        assert [request["section_id"] for request in requests] == ["section_1", "section_2"]
        closure = resumed.registry.get("review:provider_receipt_closure")
        assert closure is not None
        payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))["payload"]
        calls = payload["expected_calls"]
        assert len(calls) == 2 and all(call["verified_reuse"] for call in calls)
        assert payload["complete"] is True and payload["test_only"] is False
        dependencies = tuple(resumed.registry.get(ref.artifact_id) for ref in closure.depends_on)
        assert all(record is not None for record in dependencies)
        yield resumed, closure, calls, dependencies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _count_review_reuse_proofs(monkeypatch, registry):
    counts = {"single": [], "batch": [], "batch_kwargs": []}
    original_single = registry.verify_ready_artifact_closure
    original_batch = registry.verify_ready_artifact_closures

    def single(root, **kwargs):
        counts["single"].append(root)
        return original_single(root, **kwargs)

    def batch(roots, **kwargs):
        roots = tuple(roots)
        counts["batch"].append(roots)
        counts["batch_kwargs"].append(kwargs)
        return original_batch(roots, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closure", single)
    monkeypatch.setattr(registry, "verify_ready_artifact_closures", batch)
    return counts


def _call_verified_writer_reuse(service, call, dependencies):
    from validation.review_reuse import review_reuse_call_issues

    return review_reuse_call_issues(
        stage="review", expected_call=call, registry=service.registry,
        dependency_records=dependencies,
    )


def test_review_reuse_scope_batches_two_verified_writer_entries(
    _verified_writer_reuse_pair, monkeypatch,
) -> None:
    from contextlib import nullcontext

    from validation import review_reuse

    service, _closure, calls, dependencies = _verified_writer_reuse_pair
    revision, records = service.registry.capture_record_snapshot()
    counts = _count_review_reuse_proofs(monkeypatch, service.registry)
    # The fallback deliberately exercises the preimage's real legacy walks,
    # producing a behavioral RED rather than an import/missing-API failure.
    factory = getattr(review_reuse, "review_reuse_verification_scope", None)
    context = factory(service.registry) if factory is not None else nullcontext()
    with context:
        for call in calls:
            assert _call_verified_writer_reuse(service, call, dependencies) == []

    assert counts["single"] == []
    assert len(counts["batch"]) == 1
    roots = counts["batch"][0]
    assert len(roots) == 6
    assert len({record.artifact_id for record in roots}) == len(roots)
    assert {record.artifact_type for record in roots} == {
        "review_replay_ledger", "review_section", "review_provider_receipt_snapshot",
        "provider_receipt_ledger",
    }
    pinned_revision, pinned_records = counts["batch_kwargs"][0]["expected_record_snapshot"]
    assert pinned_revision == revision
    assert dict(pinned_records) == {record.artifact_id: record for record in records}


def test_review_reuse_warmed_call_keeps_exact_request_binding(
    _verified_writer_reuse_pair,
) -> None:
    from validation.review_reuse import review_reuse_verification_scope

    service, _closure, calls, dependencies = _verified_writer_reuse_pair
    with review_reuse_verification_scope(service.registry):
        assert _call_verified_writer_reuse(service, calls[0], dependencies) == []
        bad_call = {**calls[1], "input_hash": "f" * 64}
        issues = _call_verified_writer_reuse(service, bad_call, dependencies)
        assert issues == [
            "provider_closure_review_reuse_request_binding_mismatch:review:review:section_2"
        ]


def test_review_closure_scope_rejects_snapshot_changed_after_last_reuse_check(
    _verified_writer_reuse_pair, monkeypatch,
) -> None:
    from validation import review_reuse
    from validation.closure import _provider_closure_entry

    service, closure, calls, _dependencies = _verified_writer_reuse_pair
    terminal, terminal_payload = _publish_review_terminal(service, closure)
    target = _snapshot_record(service, "section_1")
    original = review_reuse.review_reuse_call_issues
    checked = []

    def change_after_last_binding(**kwargs):
        issues = original(**kwargs)
        assert issues == []
        checked.append(kwargs["expected_call"]["call_id"])
        if len(checked) == len(calls):
            path = Path(target.path)
            path.write_bytes(path.read_bytes() + b"\n")
        return issues

    monkeypatch.setattr(review_reuse, "review_reuse_call_issues", change_after_last_binding)
    entry, blocking = _provider_closure_entry(
        "review", closure, terminal, terminal_payload, service.registry,
    )
    assert checked == [call["call_id"] for call in calls]
    assert entry["complete"] is False
    assert entry["status"] == "blocked"
    assert blocking and any("changed" in issue or "untrusted" in issue for issue in blocking)


def _review_scope_graph(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    registry = ArtifactRegistry(root / "artifact_registry.json", "same-scope-job")

    def register(artifact_id, dependencies=()):
        path = root / (artifact_id + ".json")
        path.write_text(json.dumps({"node": artifact_id, "scope": name}), encoding="utf-8")
        return registry.register_file(
            artifact_id=artifact_id, artifact_role="test", artifact_type="test_node",
            artifact_version="v1", path=path, producer=__name__,
            depends_on=list(dependencies),
        )

    leaf = register("shared-leaf")
    owner = register("owner", [ArtifactDependencyRefV2.from_record(leaf)])
    return registry, leaf, owner, register


def test_review_reuse_scope_rechecks_transitive_leaf_bytes_at_exit(tmp_path) -> None:
    from services.artifact_registry import RegistryError
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    registry, leaf, owner, _register = _review_scope_graph(tmp_path, "late-bytes")
    with (
        pytest.raises(RegistryError, match="content hash changed"),
        review_reuse_verification_scope(registry),
    ):
        _verify_reuse_artifact_closure(registry, owner)
        Path(leaf.path).write_bytes(b'{"late":"mutation"}')


@pytest.mark.parametrize("drift", ["revision", "same_revision_metadata"])
def test_review_reuse_scope_fences_whole_registry_snapshot(tmp_path, drift) -> None:
    from services.artifact_registry import RegistrySnapshotChanged
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    registry, leaf, owner, register = _review_scope_graph(tmp_path, drift)
    with (
        pytest.raises(RegistrySnapshotChanged, match="snapshot|Registry|changed"),
        review_reuse_verification_scope(registry),
    ):
        _verify_reuse_artifact_closure(registry, owner)
        if drift == "revision":
            register("concurrent-publication")
        else:
            path = Path(registry.registry_path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = next(row for row in payload["artifacts"] if row["artifact_id"] == leaf.artifact_id)
            record["metadata"] = {**record["metadata"], "same_revision_drift": True}
            path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("inner_raises", [False, True])
def test_review_reuse_scope_nested_same_job_registries_keep_separate_roots(
    tmp_path, monkeypatch, inner_raises,
) -> None:
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    left, _left_leaf, left_root, _ = _review_scope_graph(tmp_path, "left")
    right, _right_leaf, right_root, _ = _review_scope_graph(tmp_path, "right")
    left_counts = _count_review_reuse_proofs(monkeypatch, left)
    right_counts = _count_review_reuse_proofs(monkeypatch, right)

    def inner():
        with review_reuse_verification_scope(right):
            _verify_reuse_artifact_closure(right, right_root)
            if inner_raises:
                raise ValueError("cancel inner proof")

    with review_reuse_verification_scope(left):
        _verify_reuse_artifact_closure(left, left_root)
        if inner_raises:
            with pytest.raises(ValueError, match="cancel inner proof"):
                inner()
        else:
            inner()
        _verify_reuse_artifact_closure(left, left_root)

    assert left_counts["single"] == right_counts["single"] == []
    assert left_counts["batch"] == [(left_root,)]
    assert right_counts["batch"] == ([] if inner_raises else [(right_root,)])
    _verify_reuse_artifact_closure(left, left_root)
    assert left_counts["single"] == [left_root]


@pytest.mark.parametrize("mismatch", ["registry", "resolver"])
def test_review_reuse_scope_does_not_lend_proof_to_different_context(
    tmp_path, monkeypatch, mismatch,
) -> None:
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    local, _leaf, owner, _ = _review_scope_graph(tmp_path, "local")
    other, _other_leaf, other_owner, _ = _review_scope_graph(tmp_path, "other")
    resolver = lambda _job_id: None
    different_resolver = lambda _job_id: None
    target, target_owner, active_resolver = (
        (other, other_owner, resolver) if mismatch == "registry"
        else (local, owner, different_resolver)
    )
    counts = _count_review_reuse_proofs(monkeypatch, target)
    with review_reuse_verification_scope(local, external_registry_resolver=resolver):
        _verify_reuse_artifact_closure(target, target_owner, active_resolver)
        assert counts["single"] == [target_owner]


def test_review_reuse_helper_outside_scope_keeps_legacy_full_proof(
    tmp_path, monkeypatch,
) -> None:
    from services.artifact_registry import RegistryError
    from validation.review_reuse import _verify_reuse_artifact_closure

    registry, leaf, owner, _ = _review_scope_graph(tmp_path, "legacy")
    counts = _count_review_reuse_proofs(monkeypatch, registry)
    Path(leaf.path).write_bytes(b'{"corrupted":"leaf"}')
    with pytest.raises(RegistryError, match="content hash changed"):
        _verify_reuse_artifact_closure(registry, owner)
    assert counts["single"] == [owner]


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    (
        ("foreign_snapshot", "receipt snapshot Registry reference is missing, foreign, or changed"),
        ("tampered_replay", "saved replay Registry hash does not match its bytes"),
        ("tampered_snapshot", "receipt snapshot bytes changed"),
        ("epoch_graph", r"(?i)(replay.*invalid|snapshot.*mismatch|graph.*mismatch)"),
    ),
)
def test_instrumented_http_replay_authority_mutations_block_before_resume_post(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    error_match: str,
) -> None:
    _bind_explicit_local_writer_budget(monkeypatch)
    server, server_thread, requests = _start_loopback_writer_server()
    try:
        first_service, outline, packets = _run_actual_writer_partial_attempt(tmp_path, server)
        assert [request["section_id"] for request in requests] == ["section_1"]
        first_receipts = list(first_service.receipt_ledger.list_receipts())
        assert len(first_receipts) == 1
        assert first_receipts[0].test_only is False

        snapshot_record = _snapshot_record(first_service)
        replay_record = first_service.registry.get("review_replay")
        assert replay_record is not None
        if mutation == "foreign_snapshot":
            original_get = ArtifactRegistry.get

            def foreign_snapshot(registry: ArtifactRegistry, artifact_id: str):
                record = original_get(registry, artifact_id)
                if record is not None and artifact_id == snapshot_record.artifact_id:
                    return replace(record, job_id="foreign-job")
                return record

            monkeypatch.setattr(ArtifactRegistry, "get", foreign_snapshot)
        elif mutation == "tampered_replay":
            replay_path = Path(replay_record.path)
            replay_path.write_bytes(replay_path.read_bytes() + b"\n")
        elif mutation == "tampered_snapshot":
            snapshot_path = Path(snapshot_record.path)
            snapshot_path.write_bytes(snapshot_path.read_bytes() + b"\n")
        else:
            replay_rows = [
                json.loads(line)
                for line in Path(replay_record.path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            replay_rows[0]["expected_call_graph_hash"] = "f" * 64
            _republish_replay_rows(first_service, replay_rows)

        resumed_service = _new_service(
            first_service,
            attempt_id=f"writer-http-{mutation}-resume-attempt",
            writer=None,
        )
        with pytest.raises(RuntimeError, match=error_match):
            resumed_service.run(outline_payload=outline, evidence_packets=packets)

        assert [request["section_id"] for request in requests] == ["section_1"]
        assert resumed_service.registry.get("review:provider_receipt_closure") is None
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.parametrize(
    "mutation",
    ("foreign_snapshot", "tampered_replay", "tampered_snapshot", "epoch_graph"),
)
def test_downstream_review_closure_rejects_replay_authority_mutations(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from validation.closure import _provider_closure_entry

    _bind_explicit_local_writer_budget(monkeypatch)
    server, server_thread, requests = _start_loopback_writer_server()
    try:
        first_service, outline, packets = _run_actual_writer_partial_attempt(tmp_path, server)
        resumed_service = _new_service(
            first_service,
            attempt_id="writer-http-closure-negative-attempt",
            writer=None,
        )
        resumed_service.run(outline_payload=outline, evidence_packets=packets)
        assert [request["section_id"] for request in requests] == ["section_1", "section_2"]
        assert all(
            receipt.test_only is False
            for receipt in resumed_service.receipt_ledger.list_receipts()
        )
        closure_record = resumed_service.registry.get("review:provider_receipt_closure")
        assert closure_record is not None
        terminal_record, terminal_payload = _publish_review_terminal(
            resumed_service,
            closure_record,
        )
        clean_entry, clean_blocking = _provider_closure_entry(
            "review",
            closure_record,
            terminal_record,
            terminal_payload,
            resumed_service.registry,
        )
        assert clean_blocking == [], {"entry": clean_entry, "blocking": clean_blocking}
        assert clean_entry["complete"] is True

        if mutation == "foreign_snapshot":
            snapshot_record = _snapshot_record(resumed_service, "section_1")
            original_get = ArtifactRegistry.get

            def foreign_snapshot(registry: ArtifactRegistry, artifact_id: str):
                record = original_get(registry, artifact_id)
                if record is not None and artifact_id == snapshot_record.artifact_id:
                    return replace(record, job_id="foreign-job")
                return record

            monkeypatch.setattr(ArtifactRegistry, "get", foreign_snapshot)
        elif mutation in {"tampered_replay", "tampered_snapshot"}:
            record = (
                resumed_service.registry.get("review_replay")
                if mutation == "tampered_replay"
                else _snapshot_record(resumed_service, "section_1")
            )
            assert record is not None
            path = Path(record.path)
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            closure_record = resumed_service.registry.get("review:provider_receipt_closure")
            assert closure_record is not None
            document = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
            wrong_graph_hash = "f" * 64
            document["expected_call_graph_hash"] = wrong_graph_hash
            document["payload"]["expected_call_graph_hash"] = wrong_graph_hash
            for expected_call in document["payload"]["expected_calls"]:
                expected_call["expected_call_graph_hash"] = wrong_graph_hash
            closure_record = publish_json_artifact(
                resumed_service.publication_context,
                resumed_service.registry,
                resumed_service.workspace.artifact_path("review_provider_receipt_closure.json"),
                document,
                artifact_role="provider_receipt_closure",
                artifact_type="provider_receipt_closure",
                artifact_version="v1",
                producer="tests.test_run_first_writer_cross_attempt_replay",
                artifact_id="review:provider_receipt_closure",
                depends_on=list(closure_record.depends_on),
                metadata={
                    **dict(closure_record.metadata or {}),
                    "expected_call_graph_hash": wrong_graph_hash,
                },
            )
            terminal_record, terminal_payload = _publish_review_terminal(
                resumed_service,
                closure_record,
            )

        entry, blocking = _provider_closure_entry(
            "review",
            closure_record,
            terminal_record,
            terminal_payload,
            resumed_service.registry,
        )

        assert blocking, {"mutation": mutation, "entry": entry}
        assert entry["terminal_status"] == "succeeded"
        assert entry["requested"] is True
        assert any(
            any(token in issue.casefold() for token in ("review", "receipt", "snapshot", "replay", "epoch", "graph", "untrusted"))
            for issue in blocking
        ), {"mutation": mutation, "blocking": blocking}
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_review_reuse_scope_converts_unexpected_final_proof_exception(
    tmp_path, monkeypatch,
) -> None:
    from services.artifact_registry import UnverifiedDependency
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    registry, _leaf, owner, _ = _review_scope_graph(tmp_path, "unexpected-final")

    def unexpected_final_proof(_roots, **_kwargs):
        raise RuntimeError("forced final proof error")

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", unexpected_final_proof)
    with (
        pytest.raises(UnverifiedDependency, match="forced final proof error"),
        review_reuse_verification_scope(registry),
    ):
        _verify_reuse_artifact_closure(registry, owner)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_review_reuse_scope_preserves_baseexception_and_resets_context(
    tmp_path, monkeypatch, interruption,
) -> None:
    from validation.review_reuse import (
        _verify_reuse_artifact_closure,
        review_reuse_verification_scope,
    )

    registry, _leaf, owner, _ = _review_scope_graph(tmp_path, interruption.__name__)

    def interrupt_final_proof(_roots, **_kwargs):
        raise interruption("stop final proof")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "verify_ready_artifact_closures", interrupt_final_proof)
        with (
            pytest.raises(interruption, match="stop final proof"),
            review_reuse_verification_scope(registry),
        ):
            _verify_reuse_artifact_closure(registry, owner)

    counts = _count_review_reuse_proofs(monkeypatch, registry)
    _verify_reuse_artifact_closure(registry, owner)
    assert counts["single"] == [owner]


def test_review_stage_blocks_unexpected_deferred_proof_exception(
    _verified_writer_reuse_pair, monkeypatch,
) -> None:
    from validation.closure import _provider_closure_entry

    service, closure, _calls, _dependencies = _verified_writer_reuse_pair
    terminal, terminal_payload = _publish_review_terminal(service, closure)
    original_batch = service.registry.verify_ready_artifact_closures
    injected = []

    def fail_only_deferred_batch(roots, **kwargs):
        if kwargs.get("expected_record_snapshot") is not None:
            injected.append(True)
            raise RuntimeError("forced deferred resolver failure")
        return original_batch(roots, **kwargs)

    monkeypatch.setattr(service.registry, "verify_ready_artifact_closures", fail_only_deferred_batch)
    entry, blocking = _provider_closure_entry(
        "review", closure, terminal, terminal_payload, service.registry,
    )
    assert injected == [True]
    assert entry["complete"] is False and entry["status"] == "blocked"
    assert any("forced deferred resolver failure" in issue for issue in blocking)

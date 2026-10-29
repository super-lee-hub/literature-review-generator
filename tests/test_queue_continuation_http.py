from __future__ import annotations

import configparser
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from reviewctl import main
from runtime.cancellation import CancellationRequestStore
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2, ProviderRuntimeLedger
from runtime.runner import AgentRuntimeRunner
from runtime.stage_terminal import StageTerminalStore
from services.artifact_registry import file_sha256
from services.queue_service import PersistentQueueService, QueueState
from tests.test_run_first_scale_integration import (
    _local_config,
    _scale_papers,
    _start_scale_server,
    _write_scale_pdf,
)


def _last_json(output: str) -> dict:
    return json.loads(output.strip().splitlines()[-1])


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _registered_stage1_receipts(registry) -> dict[str, object]:
    ledger = registry.get("stage1_provider_receipts")
    closure = registry.get("stage1:provider_receipt_closure")
    assert ledger is not None and ledger.status == "ready"
    assert ledger.artifact_type == "provider_receipt_ledger"
    assert closure is not None and closure.status == "ready"
    assert closure.artifact_type == "provider_receipt_closure"
    registry.verify_ready_artifact_closure(closure)

    closure_payload = _read_object(Path(closure.path))
    closure_body = closure_payload.get("payload")
    assert isinstance(closure_body, dict)
    assert closure_body.get("complete") is True
    assert closure_payload.get("expected_provider_transport_count") == 1
    assert closure_payload.get("actual_provider_transport_count") == 1

    ledger_reader = ProviderRuntimeLedger(ledger.path)
    receipts = ledger_reader.list_receipts()
    assert len(receipts) == 1
    assert receipts[0].stage_name == "stage1_analyze"
    assert file_sha256(ledger.path) == ledger.content_hash
    assert file_sha256(closure.path) == closure.content_hash
    return {
        "ledger_id": ledger.artifact_id,
        "ledger_hash": ledger.content_hash,
        "closure_id": closure.artifact_id,
        "closure_hash": closure.content_hash,
        "receipt_ids": tuple(receipt.receipt_id for receipt in receipts),
    }


@pytest.mark.integration
def test_queue_retry_resumes_after_persisted_analyze_without_reposting_reader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued retry reuses the real Analyze terminal and its original receipt/budget."""

    paper = _scale_papers(1)[0]
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / f"{paper.stem}.pdf"
    _write_scale_pdf(pdf_path, paper)
    pdf_hash = file_sha256(pdf_path)

    server, server_thread, api_base = _start_scale_server([paper])
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    job_id = "queue-reader-continuation-http"
    project_name = "queue-reader-continuation"
    config_path = _local_config(tmp_path, api_base)

    # Analyze is the only requested provider stage. Keep the local Reader route
    # at the same finite ceiling that the run-scoped V2 budget can reserve.
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path, encoding="utf-8")
    for section in ("Primary_Reader_API", "Backup_Reader_API"):
        config[section]["max_output_tokens"] = "64000"
    with config_path.open("w", encoding="utf-8") as stream:
        config.write(stream)

    budget = ProviderAggregateBudgetV2(
        max_provider_calls_total=4,
        max_output_tokens_total=64_000,
        max_retry_attempts_total=0,
        max_wall_seconds=240,
    ).to_dict()
    spec = RuntimeJobSpec(
        project_name=project_name,
        job_id=job_id,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        config=str(config_path),
        action="analyze",
        queue_file=str(queue_file),
        metadata={"provider_budget": budget},
    )
    parameters_path = tmp_path / "queue-runtime-spec.json"
    parameters_path.write_text(
        json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    origin = urlsplit(api_base)
    original_send = requests.Session.send

    def loopback_only(session, request, **kwargs):
        target = urlsplit(request.url)
        assert (target.scheme, target.hostname, target.port) == (
            origin.scheme,
            origin.hostname,
            origin.port,
        ), "queued runtime attempted a nonfixture HTTP request"
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", loopback_only)

    assert main([
        "queue-add",
        "--queue-file",
        str(queue_file),
        "--job-id",
        job_id,
        "--job-type",
        "analyze",
        "--project-name",
        project_name,
        "--parameters-file",
        str(parameters_path),
    ]) == 0
    assert _last_json(capsys.readouterr().out)["status"] == "added"

    persisted_marker: list[dict] = []
    original_persist_terminal = AgentRuntimeRunner._persist_terminal

    def persist_then_fail_after_analyze(self, session, **kwargs) -> None:
        original_persist_terminal(self, session, **kwargs)
        if kwargs.get("stage_name") == "analyze" and not persisted_marker:
            request = CancellationRequestStore(
                session.context.workspace,
                session.context.registry,
            ).request(
                requested_by="tests.test_queue_continuation_http",
                reason="exercise an explicit retry after Analyze terminal persistence",
            )
            persisted_marker.append(request.to_dict())
            raise RuntimeError("injected failure after succeeded Analyze terminal")

    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_persist_terminal",
        persist_then_fail_after_analyze,
    )

    try:
        first_code = main(["queue-run", "--queue-file", str(queue_file), "--job", job_id])
        first_payload = _last_json(capsys.readouterr().out)
        assert first_code == 1
        assert first_payload["status"] == "failed"
        assert len(persisted_marker) == 1
        assert len(server.calls) == 1  # type: ignore[attr-defined]
        assert not server.errors  # type: ignore[attr-defined]
        assert not server.transport_errors  # type: ignore[attr-defined]
        reader_call = server.calls[0]  # type: ignore[attr-defined]
        assert reader_call["model"] == "rfs-reader-local"
        assert reader_call["stage"] == "stage1_reader"
        assert reader_call["status"] == 200
        assert reader_call["path"] == "/v1/chat/completions"

        queue = PersistentQueueService(queue_file)
        job = queue.get_job(job_id)
        failed_runtime = queue.get_job_runtime(job_id)
        assert job is not None and failed_runtime is not None
        assert failed_runtime.state is QueueState.FAILED
        assert failed_runtime.lease_generation > 0
        workspace, registry = AgentRuntimeRunner._open_workspace(job.workspace_path)
        marker_store = CancellationRequestStore(workspace, registry)
        old_marker, old_marker_hash = marker_store.read_snapshot()
        assert old_marker is not None and old_marker.active is True
        assert old_marker.to_dict() == persisted_marker[0]
        assert old_marker_hash
        marker_record = registry.get(marker_store.artifact_id)
        assert marker_record is not None and marker_record.status == "ready"
        assert marker_record.content_hash == old_marker_hash

        terminals = StageTerminalStore(workspace, registry).load_records()
        analyze_terminals = [
            (record, path)
            for record, path in terminals
            if record.stage_name == "analyze" and record.status == "succeeded"
        ]
        assert len(analyze_terminals) == 1
        analyze_terminal, analyze_terminal_path = analyze_terminals[0]
        assert analyze_terminal.model_call_count == 1
        assert registry.get(analyze_terminal.record_id) is not None
        analyze_terminal_hash = file_sha256(analyze_terminal_path)

        original_receipts = _registered_stage1_receipts(registry)
        source_summaries = [
            record
            for record in registry.list_records()
            if record.artifact_type == "summary_file" and record.status == "ready"
            and record.metadata.get("pointer_role") == "current"
        ]
        assert len(source_summaries) == 1
        source_summary = source_summaries[0]
        all_summary_identities = {
            record.artifact_id: (record.content_hash, record.path)
            for record in registry.list_records()
            if record.artifact_type == "summary_file" and record.status == "ready"
        }

        budget_state_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
        budget_identity_path = budget_state_path.with_name(
            budget_state_path.name + ".identity.json"
        )
        before_budget_state = _read_object(budget_state_path)
        before_budget_identity = _read_object(budget_identity_path)
        assert before_budget_state["budget"] == budget
        assert before_budget_state["calls_used"] == 1
        assert before_budget_state["output_tokens_used"] > 0
        assert before_budget_state["retry_attempts_used"] == 0
        assert before_budget_state["calls_reserved"] == 0
        assert before_budget_state["output_tokens_reserved"] == 0
        assert before_budget_state["retry_attempts_reserved"] == 0
        assert before_budget_state["reservations"] == []
        first_started = float(before_budget_state["first_started_epoch"])
        deadline = float(before_budget_state["absolute_deadline_epoch"])
        assert first_started > 0
        assert math.isclose(deadline - first_started, 240, rel_tol=0, abs_tol=1)
        failed_generation = failed_runtime.lease_generation

        assert main(["queue-retry", "--queue-file", str(queue_file), "--job", job_id]) == 0
        retry_payload = _last_json(capsys.readouterr().out)
        assert retry_payload["status"] == "planned"
        assert retry_payload["retried_job_ids"] == [job_id]
        queue = PersistentQueueService(queue_file)
        pending_runtime = queue.get_job_runtime(job_id)
        assert pending_runtime is not None and pending_runtime.state is QueueState.PENDING
        assert pending_runtime.lease_generation == failed_generation
        assert pending_runtime.resume_intent is not None
        assert pending_runtime.resume_intent["expected_cancel_marker_sha256"] == old_marker_hash

        retry_code = main(["queue-run", "--queue-file", str(queue_file), "--job", job_id])
        retry_run_payload = _last_json(capsys.readouterr().out)
        assert retry_code == 0
        assert retry_run_payload["status"] == "completed"
        assert len(server.calls) == 1  # type: ignore[attr-defined]
        assert not server.errors  # type: ignore[attr-defined]

        queue = PersistentQueueService(queue_file)
        completed_runtime = queue.get_job_runtime(job_id)
        assert completed_runtime is not None
        assert completed_runtime.state is QueueState.COMPLETED
        assert completed_runtime.lease_generation == failed_generation + 1

        cleared_marker = marker_store.read()
        assert cleared_marker is not None and cleared_marker.active is False
        cleared_marker_record = registry.get(marker_store.artifact_id)
        assert cleared_marker_record is not None and cleared_marker_record.status == "ready"
        assert cleared_marker_record.content_hash == file_sha256(marker_store.path)
        assert cleared_marker_record.content_hash != old_marker_hash

        resumed_terminals = StageTerminalStore(workspace, registry).load_records()
        resumed_analyze = [
            (record, path)
            for record, path in resumed_terminals
            if record.stage_name == "analyze" and record.status == "succeeded"
        ]
        assert len(resumed_analyze) == 1
        assert resumed_analyze[0][0].record_id == analyze_terminal.record_id
        assert file_sha256(resumed_analyze[0][1]) == analyze_terminal_hash
        current_summary = registry.get(source_summary.artifact_id)
        assert current_summary is not None
        assert (current_summary.content_hash, current_summary.path) == (
            source_summary.content_hash, source_summary.path,
        )
        assert {
            record.artifact_id: (record.content_hash, record.path)
            for record in registry.list_records()
            if record.artifact_type == "summary_file" and record.status == "ready"
        } == all_summary_identities
        assert _registered_stage1_receipts(registry) == original_receipts
        assert file_sha256(pdf_path) == pdf_hash

        after_budget_state = _read_object(budget_state_path)
        after_budget_identity = _read_object(budget_identity_path)
        invariant_state_fields = (
            "budget",
            "acceptance_run_id",
            "calls_used",
            "output_tokens_used",
            "retry_attempts_used",
            "calls_reserved",
            "output_tokens_reserved",
            "retry_attempts_reserved",
            "first_started_epoch",
            "absolute_deadline_epoch",
            "reservations",
            "primary_repair_scopes",
        )
        assert {
            key: before_budget_state[key] for key in invariant_state_fields
        } == {
            key: after_budget_state[key] for key in invariant_state_fields
        }
        # Queue resume may take ownership in a new process/lease; the durable
        # budget identity and every call/deadline/reservation counter stay put.
        assert after_budget_identity == before_budget_identity
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()

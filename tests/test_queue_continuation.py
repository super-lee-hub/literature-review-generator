from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import reviewctl
from runtime import cancellation as cancellation_module
from runtime.cancellation import CancellationRequestStore
from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderRuntimeLedger,
    hash_json,
)
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_terminal import StageTerminalStore
from services.artifact_registry import file_sha256
from services.job_runner import JobRunner
from services.queue_service import (
    PersistentQueueService,
    QueueJobSpec,
    QueueRunner,
    QueueState,
)
from test_current_runtime_full_e2e import _reader_summary, _test_config, _write_pdf


_FIXED_CANCEL_TIME = "2026-10-26T04:00:00+00:00"


@dataclass(frozen=True)
class _QueueCase:
    queue_file: Path
    config_path: Path
    pdf_path: Path
    job_id: str
    service: PersistentQueueService


@pytest.fixture
def gui_app_module(monkeypatch: pytest.MonkeyPatch):
    previous = sys.modules.get("gui.app")
    fake_ui = types.SimpleNamespace(notify=lambda *args, **kwargs: None)
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = fake_ui
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    try:
        module = importlib.reload(importlib.import_module("gui.app"))
        yield module
    finally:
        sys.modules.pop("gui.app", None)
        if previous is not None:
            sys.modules["gui.app"] = previous
        package = sys.modules.get("gui")
        if package is not None:
            if previous is not None:
                setattr(package, "app", previous)
            elif hasattr(package, "app"):
                delattr(package, "app")


def _budget() -> dict[str, Any]:
    contract = ProviderAggregateBudgetV2(
        max_provider_calls_total=1,
        max_output_tokens_total=131_072,
        max_retry_attempts_total=0,
        max_wall_seconds=300,
    ).to_dict()
    return {
        "schema_version": ProviderAggregateBudgetV2.SCHEMA_VERSION,
        **contract,
    }


def _new_case(tmp_path: Path) -> _QueueCase:
    config_path = _test_config(tmp_path)
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "paper-a.pdf"
    _write_pdf(
        pdf_path,
        "paper-a",
        "The local fixture reports a bounded, measurable empirical finding.",
    )
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    job_id = "queue-continuation-job"
    spec = RuntimeJobSpec(
        project_name="queue-continuation",
        job_id=job_id,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        config=str(config_path),
        action="analyze",
        queue_file=str(queue_file),
        metadata={"provider_budget": _budget()},
    )
    spec.validate()
    service = PersistentQueueService(queue_file)
    service.add_job(
        QueueJobSpec(
            job_id=job_id,
            job_type="analyze",
            project_name=spec.project_name,
            parameters=spec.to_dict(),
        )
    )
    return _QueueCase(queue_file, config_path, pdf_path, job_id, service)


def _install_reader(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def injected_reader(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        return {
            "status": "success",
            "content": _reader_summary(
                "paper-a",
                "paper-a",
                "The local fixture reports a bounded, measurable empirical finding.",
            ),
        }

    # This is a deterministic reader-provider-edge injection, not an HTTP call.
    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", injected_reader)
    return calls


def _workspace_and_registry(case: _QueueCase):
    job = case.service.get_job(case.job_id)
    assert job is not None
    return AgentRuntimeRunner._open_workspace(job.workspace_path)


def _marker_store(case: _QueueCase) -> tuple[CancellationRequestStore, Any]:
    workspace, registry = _workspace_and_registry(case)
    registry.reload()
    return CancellationRequestStore(workspace, registry), registry


def _assert_registered_marker(
    case: _QueueCase,
    *,
    active: bool,
    request_token: str | None = None,
) -> tuple[Any, str]:
    store, registry = _marker_store(case)
    marker, digest = store.read_snapshot()
    assert marker is not None
    assert marker.active is active
    if request_token is not None:
        assert marker.request_token == request_token
    record = registry.get(store.artifact_id)
    assert record is not None and record.status == "ready"
    assert str(record.path) == str(store.path)
    assert record.content_hash == digest == file_sha256(store.path)
    return marker, digest


def _assert_durable_analyze_terminal(case: _QueueCase) -> None:
    workspace, registry = _workspace_and_registry(case)
    records = StageTerminalStore(workspace, registry).load_records()
    analyze = [record for record, _path in records if record.stage_name == "analyze"]
    assert analyze
    assert analyze[-1].status == "succeeded"


def _seed_failed_analyze(
    case: _QueueCase,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    original_persist_terminal = AgentRuntimeRunner._persist_terminal
    observed: dict[str, Any] = {}

    def persist_then_interrupt(runner: AgentRuntimeRunner, session, **kwargs: Any) -> None:
        original_persist_terminal(runner, session, **kwargs)
        if kwargs.get("stage_name") != "analyze" or not kwargs["result"].success:
            return
        store = CancellationRequestStore(session.context.workspace, session.context.registry)
        with monkeypatch.context() as cancel_clock:
            cancel_clock.setattr(
                cancellation_module,
                "utc_now_iso",
                lambda: _FIXED_CANCEL_TIME,
            )
            marker = store.request(
                requested_by="test.queue_continuation",
                reason="interrupted after durable Analyze terminal",
            )
        observed["marker"] = marker
        raise RuntimeError("injected interruption after durable Analyze terminal")

    with monkeypatch.context() as fault:
        fault.setattr(AgentRuntimeRunner, "_persist_terminal", persist_then_interrupt)
        assert QueueRunner(case.service, JobRunner()).run_single_job(case.job_id)

    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.FAILED
    assert observed.get("marker") is not None
    _assert_durable_analyze_terminal(case)
    marker, _digest = _assert_registered_marker(
        case,
        active=True,
        request_token=observed["marker"].request_token,
    )
    return marker


def _input_identity(case: _QueueCase) -> tuple[str, str, str, str, str]:
    job = case.service.get_job(case.job_id)
    assert job is not None
    frozen = job.execution_snapshot["runtime_spec"]
    return (
        job.input_fingerprint,
        str(job.execution_snapshot["fingerprint"]),
        hash_json(frozen),
        job.config_fingerprint,
        hashlib.sha256(case.pdf_path.read_bytes()).hexdigest(),
    )


def _budget_snapshot(case: _QueueCase) -> dict[str, Any]:
    workspace, _registry = _workspace_and_registry(case)
    path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _without_owner_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_owner_ids(item)
            for key, item in value.items()
            if key != "owner_id"
        }
    if isinstance(value, list):
        return [_without_owner_ids(item) for item in value]
    return value


def _assert_same_budget(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert _without_owner_ids(after) == _without_owner_ids(before)
    for key in (
        "first_started_epoch",
        "absolute_deadline_epoch",
        "calls_used",
        "output_tokens_used",
        "retry_attempts_used",
        "calls_reserved",
        "output_tokens_reserved",
        "retry_attempts_reserved",
        "reservations",
    ):
        assert after.get(key) == before.get(key)


def _provider_receipt_snapshot(case: _QueueCase) -> tuple[str, str, tuple[str, ...]]:
    _workspace, registry = _workspace_and_registry(case)
    registry.reload()
    record = registry.get("stage1_provider_receipts")
    assert record is not None and record.status == "ready"
    ledger = ProviderRuntimeLedger(record.path)
    receipt_ids = tuple(item.receipt_id for item in ledger.list_receipts())
    assert receipt_ids
    return record.content_hash, file_sha256(record.path), receipt_ids


def _pending_intent(case: _QueueCase) -> dict[str, Any]:
    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.PENDING
    intent = case.service.execution_resume_intent(case.job_id)
    assert intent is not None
    return intent


def test_fresh_analyze_queue_run_is_the_non_replay_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)

    assert QueueRunner(case.service, JobRunner()).run_single_job(case.job_id)

    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.COMPLETED
    assert runtime.result_summary is not None
    assert runtime.result_summary["resume_state"] == "new"
    assert len(reader_calls) == 1
    assert _provider_receipt_snapshot(case)[2]
    _assert_durable_analyze_terminal(case)


@pytest.mark.parametrize("surface", ["single_job", "gui", "cli"])
def test_retry_surfaces_resume_durable_analyze_without_reader_replay(
    surface: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)
    old_marker = _seed_failed_analyze(case, monkeypatch)
    marker, old_marker_hash = _assert_registered_marker(case, active=True)
    assert marker.request_token == old_marker.request_token
    before_inputs = _input_identity(case)
    before_budget = _budget_snapshot(case)
    before_receipts = _provider_receipt_snapshot(case)

    if surface == "single_job":
        captured: list[dict[str, Any] | None] = []
        original_prepare = case.service.prepare_retry

        def capture_prepare(job_id: str, *args: Any, **kwargs: Any) -> bool:
            prepared = original_prepare(job_id, *args, **kwargs)
            if prepared:
                captured.append(case.service.execution_resume_intent(job_id))
            return prepared

        monkeypatch.setattr(case.service, "prepare_retry", capture_prepare)
        assert QueueRunner(case.service, JobRunner()).run_single_job(case.job_id)
        assert len(captured) == 1 and captured[0] is not None
        intent = captured[0]
    elif surface == "gui":
        gui_module = request.getfixturevalue("gui_app_module")
        controller = gui_module.WorkspaceController(str(case.config_path))
        controller._queue_service = case.service
        scheduled: list[bool] = []
        monkeypatch.setattr(controller, "_schedule_queue_processor", lambda: scheduled.append(True) or True)
        controller.retry_job(case.job_id)
        assert scheduled == [True]
        intent = _pending_intent(case)
        assert _assert_registered_marker(case, active=True)[1] == old_marker_hash
        QueueRunner(case.service, JobRunner()).run()
    else:
        assert reviewctl.main([
            "queue-retry",
            "--queue-file",
            str(case.queue_file),
            "--job",
            case.job_id,
        ]) == 0
        retry_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert retry_payload["status"] == "planned"
        service = PersistentQueueService(case.queue_file)
        case = _QueueCase(case.queue_file, case.config_path, case.pdf_path, case.job_id, service)
        intent = _pending_intent(case)
        assert _assert_registered_marker(case, active=True)[1] == old_marker_hash
        assert reviewctl.main([
            "queue-run",
            "--queue-file",
            str(case.queue_file),
            "--job",
            case.job_id,
        ]) == 0
        run_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert run_payload["status"] == "completed"

    assert intent["schema_version"] == "queue-resume-intent-v1"
    assert intent["job_id"] == case.job_id
    job = case.service.get_job(case.job_id)
    assert job is not None
    assert intent["workspace_path"] == str(Path(job.workspace_path).resolve())
    assert intent["execution_fingerprint"] == before_inputs[1]
    assert intent["runtime_spec_sha256"] == before_inputs[2]
    assert intent["expected_cancel_marker_sha256"] == old_marker_hash

    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.COMPLETED
    assert runtime.retry_count == 1
    assert runtime.result_summary is not None
    assert runtime.result_summary["resume_state"] == "resumed"
    assert _input_identity(case) == before_inputs
    assert len(reader_calls) == 1
    _assert_same_budget(before_budget, _budget_snapshot(case))
    assert _provider_receipt_snapshot(case) == before_receipts
    _assert_durable_analyze_terminal(case)
    cleared, cleared_hash = _assert_registered_marker(case, active=False)
    assert cleared.request_token != old_marker.request_token
    assert cleared_hash != old_marker_hash


def test_new_cancel_after_retry_prepare_is_preserved_without_reader_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)
    old_marker = _seed_failed_analyze(case, monkeypatch)
    store, _registry = _marker_store(case)
    assert case.service.prepare_retry(case.job_id)
    intent = _pending_intent(case)
    before_budget = _budget_snapshot(case)
    before_receipts = _provider_receipt_snapshot(case)

    with monkeypatch.context() as cancel_clock:
        cancel_clock.setattr(
            cancellation_module,
            "utc_now_iso",
            lambda: old_marker.requested_at,
        )
        new_marker = store.request(
            requested_by="test.queue_continuation",
            reason="same-clock cancellation after retry preparation",
        )
    assert new_marker.requested_at == old_marker.requested_at
    assert new_marker.request_token != old_marker.request_token
    assert intent["expected_cancel_marker_sha256"] != _assert_registered_marker(case, active=True)[1]

    QueueRunner(case.service, JobRunner()).run()

    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.CANCELLED
    assert len(reader_calls) == 1
    _assert_same_budget(before_budget, _budget_snapshot(case))
    assert _provider_receipt_snapshot(case) == before_receipts
    _assert_registered_marker(case, active=True, request_token=new_marker.request_token)


@pytest.mark.parametrize("tamper", ["input_drift", "foreign_intent"])
def test_invalid_pending_resume_keeps_cancel_marker_and_has_no_provider_effects(
    tamper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)
    old_marker = _seed_failed_analyze(case, monkeypatch)
    assert case.service.prepare_retry(case.job_id)
    _pending_intent(case)
    before_budget = _budget_snapshot(case)
    before_receipts = _provider_receipt_snapshot(case)
    before_inputs = _input_identity(case)

    if tamper == "input_drift":
        case.pdf_path.write_bytes(case.pdf_path.read_bytes() + b"\npost-retry input drift\n")
    else:
        payload = json.loads(case.queue_file.read_text(encoding="utf-8"))
        payload["runtimes"][case.job_id]["resume_intent"]["job_id"] = "foreign-job"
        case.queue_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        case = _QueueCase(
            case.queue_file,
            case.config_path,
            case.pdf_path,
            case.job_id,
            PersistentQueueService(case.queue_file),
        )

    QueueRunner(case.service, JobRunner()).run()

    runtime = case.service.get_job_runtime(case.job_id)
    assert runtime is not None and runtime.state == QueueState.FAILED
    assert len(reader_calls) == 1
    _assert_same_budget(before_budget, _budget_snapshot(case))
    assert _provider_receipt_snapshot(case) == before_receipts
    _assert_registered_marker(case, active=True, request_token=old_marker.request_token)
    if tamper == "input_drift":
        frozen_job = case.service.get_job(case.job_id)
        assert frozen_job is not None
        assert frozen_job.input_fingerprint == before_inputs[0]
        assert hashlib.sha256(case.pdf_path.read_bytes()).hexdigest() != before_inputs[4]


def test_queue_resume_intent_requires_publication_context_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)
    _seed_failed_analyze(case, monkeypatch)
    assert case.service.prepare_retry(case.job_id)
    runtime_spec = case.service.execution_runtime_spec(case.job_id)
    intent = _pending_intent(case)
    before_budget = _budget_snapshot(case)
    before_receipts = _provider_receipt_snapshot(case)

    with pytest.raises(RuntimeRunnerError, match="lease-bound publication context"):
        AgentRuntimeRunner(runtime_spec, queue_resume_intent=intent)

    assert len(reader_calls) == 1
    _assert_same_budget(before_budget, _budget_snapshot(case))
    assert _provider_receipt_snapshot(case) == before_receipts
    _assert_registered_marker(case, active=True)


def test_direct_resume_cas_preserves_same_clock_new_cancel_after_identity_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _new_case(tmp_path)
    reader_calls = _install_reader(monkeypatch)
    old_marker = _seed_failed_analyze(case, monkeypatch)
    before_budget = _budget_snapshot(case)
    before_receipts = _provider_receipt_snapshot(case)
    original_validate = AgentRuntimeRunner._validate_persisted_spec
    injected: dict[str, Any] = {}

    def validate_then_request_new_cancel(workspace, expected_payload, registry=None) -> None:
        original_validate(workspace, expected_payload, registry)
        if injected:
            return
        store = CancellationRequestStore(workspace, registry)
        with monkeypatch.context() as cancel_clock:
            cancel_clock.setattr(
                cancellation_module,
                "utc_now_iso",
                lambda: old_marker.requested_at,
            )
            injected["marker"] = store.request(
                requested_by="test.queue_continuation",
                reason="same-clock cancellation during direct-resume preflight",
            )

    monkeypatch.setattr(
        AgentRuntimeRunner,
        "_validate_persisted_spec",
        staticmethod(validate_then_request_new_cancel),
    )
    job = case.service.get_job(case.job_id)
    assert job is not None
    workspace_path = job.workspace_path
    control = ReviewControlPlane(
        repo_root=Path(__file__).resolve().parents[1],
        workspace_roots=(Path(workspace_path).parent,),
    )

    with pytest.raises(ControlPlaneError, match="resume cancellation state is invalid"):
        control.resume(workspace=workspace_path)

    new_marker = injected.get("marker")
    assert new_marker is not None
    assert new_marker.requested_at == old_marker.requested_at
    assert new_marker.request_token != old_marker.request_token
    assert len(reader_calls) == 1
    _assert_same_budget(before_budget, _budget_snapshot(case))
    assert _provider_receipt_snapshot(case) == before_receipts
    _assert_registered_marker(case, active=True, request_token=new_marker.request_token)

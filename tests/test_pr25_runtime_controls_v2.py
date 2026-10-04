from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from runtime.cancellation import CancellationRequestStore
from runtime.control_plane import ReviewControlPlane
from runtime.pause_state import PauseStateStore
from runtime.provider_runtime import (
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    ProviderBudgetExceeded,
    ProviderRuntime,
    ProviderRuntimeContractError,
    RuntimeControlIdentityV1,
    bind_runtime_control_context,
    bind_pause_state_path,
    current_runtime_control_context,
    runtime_control_lifecycle,
)
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueRunner


def _workspace(base: Path, project: str = "runtime-controls", job_id: str = "job-a"):
    workspace = JobWorkspace.create(str(base), project, job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    identity = RuntimeControlIdentityV1(
        job_id=job_id,
        workspace_path=workspace.paths.root_dir,
    )
    return workspace, registry, identity


def test_pause_gate_uses_registry_and_fails_closed_for_stale_or_missing_marker(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    pause_store = PauseStateStore(workspace, registry)
    pause_store.request(reason="fixture pause")

    payload = json.loads(pause_store.path.read_text(encoding="utf-8"))
    payload["state"] = "RUNNABLE"
    pause_store.path.write_text(json.dumps(payload), encoding="utf-8")
    with runtime_control_lifecycle():
        bind_pause_state_path(pause_store.path)
        with pytest.raises(ProviderRuntimeContractError, match="CONTROL_STATE_INVALID"):
            ProviderRuntime(test_only=True).admit()

    pause_store.request(reason="fixture pause before deletion")
    pause_store.path.unlink()
    with runtime_control_lifecycle():
        bind_pause_state_path(pause_store.path)
        with pytest.raises(ProviderRuntimeContractError, match="registered pause marker is missing"):
            ProviderRuntime(test_only=True).admit()


def test_clean_pause_state_and_explicit_resume_allow_admission(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        ProviderRuntime(test_only=True).admit()

    pause_store = PauseStateStore(workspace, registry)
    pause_store.request(reason="fixture pause")
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        with pytest.raises(ProviderBudgetExceeded, match="PAUSED_BY_USER"):
            ProviderRuntime(test_only=True).admit()

    pause_store.clear(cleared_by="fixture", reason="explicit resume")
    with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
        ProviderRuntime(test_only=True).admit()


def test_durable_cancellation_blocks_active_queue_admission_without_token_mutation(tmp_path: Path) -> None:
    config_path = tmp_path / "queue-config.ini"
    config_path.write_text("[Paths]\noutput_path = ./output\n", encoding="utf-8")
    source_dir = tmp_path / "queue-pdfs"
    source_dir.mkdir()
    (source_dir / "paper.pdf").write_bytes(b"%PDF-1.4\nfixture input\n")
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    parameters = RuntimeJobSpec(
        project_name="cancel-audit",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(source_dir)),
        config=str(config_path),
        action="analyze",
        metadata={},
    ).to_dict()
    service = PersistentQueueService(queue_file)
    service.add_job(
        QueueJobSpec(
            job_id="cancel-audit-job",
            job_type="analyze",
            project_name="cancel-audit",
            parameters=parameters,
        )
    )
    job = service.get_job("cancel-audit-job")
    assert job is not None
    workspace_path = job.workspace_path
    workspace = JobWorkspace.create(
        str(Path(workspace_path).parent), "cancel-audit", "cancel-audit-job"
    )
    started = threading.Event()
    release = threading.Event()
    calls = {"admitted": 0, "token_cancelled": None, "admission_error": ""}

    class FixtureJobRunner:
        def run(self, request, cancel_token=None):
            bind_pause_state_path(
                Path(workspace_path)
                / "artifacts"
                / "pause_state"
                / "cancel-audit-job.json"
            )
            started.set()
            assert release.wait(10), "fixture release was not signaled"
            calls["token_cancelled"] = bool(
                cancel_token is not None and cancel_token.is_cancelled()
            )
            runtime = ProviderRuntime(test_only=True)
            try:
                runtime.admit()
            except ProviderBudgetExceeded as exc:
                calls["admission_error"] = str(exc)
            else:
                calls["admitted"] += 1
            return SimpleNamespace(
                success=True,
                job_status="completed",
                exit_code=0,
                job_disposition="clean",
                canonical_ready=True,
                requires_attention=False,
                message="fixture completed",
                workspace_path=workspace_path,
                job_id="cancel-audit-job",
                resume_state="fresh",
                failure_summary="",
                produced_artifacts=[],
            )

    worker = threading.Thread(
        target=QueueRunner(service, FixtureJobRunner()).run_single_job,
        args=("cancel-audit-job",),
        name="runtime-controls-queue-worker",
    )
    worker.start()
    assert started.wait(10), "fixture worker did not reach the provider boundary"
    cancel_result = ReviewControlPlane(repo_root=Path.cwd()).cancel(
        workspace=workspace_path,
        requested_by="fixture",
        reason="cancel while worker is active",
    )
    request = CancellationRequestStore(
        workspace,
        ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
    ).read()
    release.set()
    worker.join(10)

    assert not worker.is_alive()
    assert cancel_result["status"] == "requested"
    assert request is not None and request.active
    assert calls["token_cancelled"] is False
    assert calls["admitted"] == 0
    assert "CANCEL_REQUESTED" in calls["admission_error"]


def test_missing_started_aggregate_budget_state_cannot_reset_cumulative_usage(tmp_path: Path) -> None:
    state_path = tmp_path / "provider_budget_state.json"
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=1)
    first = ProviderBudgetController(budget)
    first.bind_state_path(state_path, acceptance_run_id="acceptance-run-a")
    reservation = first.admit()
    first.mark_transport_started(reservation)
    assert first.complete(reservation, {"attempts": 1, "output_tokens": 0})["calls_used"] == 1
    with pytest.raises(ProviderBudgetExceeded):
        first.admit()

    state_path.unlink()
    resumed = ProviderBudgetController(budget)
    with pytest.raises(ProviderRuntimeContractError, match="state is missing"):
        resumed.bind_state_path(state_path, acceptance_run_id="acceptance-run-a", state_started=True)


def test_valid_started_budget_resume_keeps_run_identity_and_cumulative_usage(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    state_path = tmp_path / "provider_budget_state.json"
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=3)
    first = ProviderBudgetController(budget)
    first.bind_state_path(state_path, acceptance_run_id="acceptance-run-resume")
    reservation = first.admit()
    first.complete(reservation, {"attempts": 1, "output_tokens": 0})

    resumed = ProviderBudgetController(budget)
    resumed.bind_state_path(
        state_path,
        acceptance_run_id="acceptance-run-resume",
        state_started=True,
    )
    run_identity = RuntimeControlIdentityV1(
        job_id=identity.job_id,
        workspace_path=identity.workspace_path,
        acceptance_run_id="acceptance-run-resume",
        provider_budget_started=True,
    )
    with bind_runtime_control_context(run_identity, workspace=workspace, registry=registry):
        ProviderRuntime(aggregate_budget=resumed, test_only=True).admit()
    assert resumed.snapshot()["calls_used"] == 1
    assert resumed.snapshot()["calls_reserved"] == 1


def test_bound_budget_state_is_checked_even_when_legacy_context_flag_is_false(
    tmp_path: Path,
) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    state_path = tmp_path / "provider_budget_state.json"
    controller = ProviderBudgetController(ProviderAggregateBudgetV1(max_provider_calls_total=2))
    controller.bind_state_path(state_path, acceptance_run_id="acceptance-run-legacy")
    legacy_identity = RuntimeControlIdentityV1(
        job_id=identity.job_id,
        workspace_path=identity.workspace_path,
        acceptance_run_id="acceptance-run-legacy",
        provider_budget_started=False,
    )

    with bind_runtime_control_context(
        legacy_identity,
        workspace=workspace,
        registry=registry,
    ):
        state_path.unlink()
        with pytest.raises(ProviderRuntimeContractError, match="state is missing"):
            ProviderRuntime(aggregate_budget=controller, test_only=True).admit()


def test_late_cancellation_retains_started_transport_exposure(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=2)
    controller = ProviderBudgetController(budget)
    state_path = tmp_path / "provider_budget_state.json"
    controller.bind_state_path(
        state_path,
        acceptance_run_id="acceptance-run-inflight",
    )
    run_identity = RuntimeControlIdentityV1(
        job_id=identity.job_id,
        workspace_path=identity.workspace_path,
        acceptance_run_id="acceptance-run-inflight",
        provider_budget_started=True,
    )

    with bind_runtime_control_context(run_identity, workspace=workspace, registry=registry):
        runtime = ProviderRuntime(aggregate_budget=controller, test_only=True)
        admission = runtime.admit()
        runtime.mark_transport_started(admission)
        CancellationRequestStore(workspace, registry).request(
            requested_by="fixture",
            reason="cancel after transport started",
        )
        with pytest.raises(ProviderBudgetExceeded, match="CANCEL_REQUESTED"):
            runtime.admit()

    snapshot = controller.snapshot()
    assert snapshot["calls_reserved"] == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["reservations"][0]["transport_started"] is True


def test_nested_runtime_binding_inherits_the_claimed_queue_lease(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    queue_identity = RuntimeControlIdentityV1(
        job_id=identity.job_id,
        workspace_path=identity.workspace_path,
        lease_id="queue-runner:lease-1",
        lease_generation=7,
    )
    with bind_runtime_control_context(queue_identity, workspace=workspace, registry=registry):
        with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
            context = current_runtime_control_context()
            assert context is not None
            assert context.identity.lease_id == "queue-runner:lease-1"
            assert context.identity.lease_generation == 7


def test_stale_queue_lease_blocks_admission(tmp_path: Path) -> None:
    workspace, registry, identity = _workspace(tmp_path)
    lease_identity = RuntimeControlIdentityV1(
        job_id=identity.job_id,
        workspace_path=identity.workspace_path,
        lease_id="queue-runner:lease-expired",
        lease_generation=8,
    )
    with bind_runtime_control_context(
        lease_identity,
        workspace=workspace,
        registry=registry,
        lease_validator=lambda: False,
    ):
        with pytest.raises(ProviderBudgetExceeded, match="QUEUE_LEASE_LOST"):
            ProviderRuntime(test_only=True).admit()


@pytest.mark.parametrize("corruption", ["nan", "infinity", "zero", "missing"])
def test_started_budget_rejects_invalid_persisted_absolute_deadline(
    tmp_path: Path,
    corruption: str,
) -> None:
    state_path = tmp_path / f"provider_budget_{corruption}.json"
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=3, max_wall_seconds=30)
    initial = ProviderBudgetController(budget)
    initial.bind_state_path(state_path, acceptance_run_id=f"run-{corruption}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if corruption == "nan":
        payload["absolute_deadline_epoch"] = float("nan")
    elif corruption == "infinity":
        payload["absolute_deadline_epoch"] = float("inf")
    elif corruption == "zero":
        payload["absolute_deadline_epoch"] = 0
    else:
        del payload["absolute_deadline_epoch"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    resumed = ProviderBudgetController(budget)
    with pytest.raises(ProviderRuntimeContractError):
        resumed.bind_state_path(
            state_path,
            acceptance_run_id=f"run-{corruption}",
            state_started=True,
        )


def test_same_process_pause_context_is_cleared_after_job_scope(tmp_path: Path) -> None:
    workspace_a, registry_a, identity_a = _workspace(tmp_path, "project-a", "job-a")
    workspace_b, registry_b, identity_b = _workspace(tmp_path, "project-b", "job-b")
    PauseStateStore(workspace_a, registry_a).request(reason="fixture pause")

    with runtime_control_lifecycle():
        with bind_runtime_control_context(
            identity_a,
            workspace=workspace_a,
            registry=registry_a,
        ):
            with pytest.raises(ProviderBudgetExceeded, match="PAUSED_BY_USER"):
                ProviderRuntime(test_only=True).admit()

        assert not current_pause_path_is_bound()
        ProviderRuntime(test_only=True).admit()
        with bind_runtime_control_context(
            identity_b,
            workspace=workspace_b,
            registry=registry_b,
        ):
            ProviderRuntime(test_only=True).admit()

    assert not current_pause_path_is_bound()


def current_pause_path_is_bound() -> bool:
    from runtime.provider_runtime import current_pause_state_path

    return bool(current_pause_state_path())

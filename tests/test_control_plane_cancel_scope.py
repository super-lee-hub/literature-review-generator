"""Stopping work must not wait for scientific/completion integrity inspection."""
import json
from pathlib import Path

import pytest

from runtime.cancellation import CancellationRequestStore
from runtime.control_plane import ReviewControlPlane
from runtime.provider_runtime import ProviderAggregateBudgetV1, ProviderBudgetController
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry, RegistryError
from services.job_outcome import JobOutcomeV1, JobStatus
from services.job_workspace import JobWorkspace


def _workspace(tmp_path: Path):
    workspace = JobWorkspace.create(str(tmp_path), "cancel-scope", "cancel-scope-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    return workspace, registry, ReviewControlPlane(repo_root=Path.cwd())


def _outcome(workspace, registry, status: JobStatus, *, owner: str | None = None):
    outcome = JobOutcomeV1.create(
        job_id=owner or workspace.job_id,
        attempt_number=1,
        job_status=status,
        job_disposition="unvalidated",
        canonical_ready=False,
        requires_attention=True,
        readiness_policy_snapshot={"validation_required": True},
    )
    path = Path(workspace.artifact_path("cancel-scope-outcome.json"))
    path.write_text(json.dumps(outcome.to_dict()), encoding="utf-8")
    registry.register_file(
        artifact_id="job_outcome", artifact_role="job_outcome",
        artifact_type="job_outcome", artifact_version="v1", path=path,
        producer="test_control_plane_cancel_scope",
    )
    return path


def _forbid_inspection(control, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("cancellation must not evaluate full workspace completion")

    monkeypatch.setattr(control, "inspect", forbidden)
    monkeypatch.setattr(AgentRuntimeRunner, "status", forbidden)


def test_cancel_skips_unrelated_integrity_work_and_preserves_budget(tmp_path, monkeypatch):
    workspace, registry, control = _workspace(tmp_path)
    _outcome(workspace, registry, "running")
    unrelated = Path(workspace.artifact_path("unrelated-evidence.json"))
    unrelated.write_text("{}", encoding="utf-8")
    record = registry.register_file(
        artifact_id="unrelated", artifact_role="diagnostic", artifact_type="diagnostic",
        artifact_version="v1", path=unrelated, producer="test_control_plane_cancel_scope",
    )
    unrelated.write_text('{"tampered": true}', encoding="utf-8")
    budget = Path(workspace.root_dir) / "logs" / "finite-budget.json"
    controller = ProviderBudgetController(ProviderAggregateBudgetV1(max_provider_calls_total=3))
    controller.bind_state_path(budget, acceptance_run_id="cancel-scope-budget")
    reservation = controller.admit()
    controller.complete(reservation, {"attempts": 1, "output_tokens": 0})
    prior_budget = budget.read_bytes()
    _forbid_inspection(control, monkeypatch)

    result = control.cancel(workspace=workspace.root_dir, reason="stop active work")

    assert result["status"] == "requested"
    request = CancellationRequestStore(workspace, registry).read()
    assert request is not None and request.active
    assert budget.read_bytes() == prior_budget
    unchanged = registry.get("unrelated")
    assert unchanged is not None and unchanged.content_hash == record.content_hash
    with pytest.raises(RegistryError):
        ArtifactRegistry._verify_ready_artifact(record)


@pytest.mark.parametrize("outcome_state", ["missing", "tampered", "foreign"])
def test_unknown_or_invalid_outcome_does_not_prevent_stopping(tmp_path, monkeypatch, outcome_state):
    workspace, registry, control = _workspace(tmp_path)
    path = None
    if outcome_state == "tampered":
        path = _outcome(workspace, registry, "completed")
        path.write_text("{}", encoding="utf-8")
    elif outcome_state == "foreign":
        path = _outcome(workspace, registry, "completed", owner="another-job")
    prior = path.read_bytes() if path else None
    _forbid_inspection(control, monkeypatch)

    result = control.cancel(workspace=workspace.root_dir)

    assert result["status"] == "requested"
    request = CancellationRequestStore(workspace, registry).read()
    assert request is not None and request.active and request.job_id == workspace.job_id
    if path:
        assert path.read_bytes() == prior


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_known_terminal_outcome_remains_a_noop(tmp_path, monkeypatch, status):
    workspace, registry, control = _workspace(tmp_path)
    _outcome(workspace, registry, status)
    prior_registry = Path(registry.registry_path).read_bytes()
    _forbid_inspection(control, monkeypatch)

    result = control.cancel(workspace=workspace.root_dir)

    assert result == {
        "status": "blocked", "job_id": workspace.job_id,
        "reason": f"job is already terminal: {status}",
        "mutation_performed": False, "read_only": True,
    }
    assert CancellationRequestStore(workspace, registry).read() is None
    assert Path(registry.registry_path).read_bytes() == prior_registry

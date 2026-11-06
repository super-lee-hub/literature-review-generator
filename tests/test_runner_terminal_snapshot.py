"""Call-scoped terminal validation on temporary Registries; no business run."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from runtime import runner as runner_module
from runtime.reconcile import ReconcileValidationError
from runtime.runner import AgentRuntimeRunner
from runtime.stage_contracts import StageArtifactRef, StageResult
from runtime.stage_terminal import StageTerminalContractError
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network is outside terminal unit scope")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    yield
    assert attempts == []


def _case(tmp_path, monkeypatch, count=8, *, external=False):
    workspace = JobWorkspace.create(str(tmp_path / "output"), "trial", job_id="terminal-trial")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    owner = registry
    if external:
        parent = JobWorkspace.create(str(tmp_path / "parent"), "source", job_id="source-owner")
        owner = ArtifactRegistry(parent.paths.registry_path, parent.job_id)
    source_path = tmp_path / "source.json"
    source_path.write_text('{"source": true}', encoding="utf-8")
    source = owner.register_file(
        path=source_path, artifact_id="shared-source", artifact_role="source",
        artifact_type="terminal_trial_source", artifact_version="v1", producer="test",
        metadata={"typed_flag": False},
    )
    resolver = lambda job: owner if job == owner.job_id else None
    dependency = ArtifactDependencyRefV2.from_record(
        source, dependency_kind="external_job" if external else "local_job",
    )
    outputs = []
    for index in range(count):
        path = tmp_path / f"output-{index}.json"
        path.write_text(json.dumps({"index": index}), encoding="utf-8")
        outputs.append(registry.register_file(
            path=path, artifact_id=f"output-{index}", artifact_role="output",
            artifact_type="summary_file" if index == 0 else "terminal_trial_output",
            artifact_version="v1", producer="test",
            depends_on=[dependency], external_registry_resolver=resolver,
        ))
    seen = []
    hook = [None]

    def validator(record, path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if record.artifact_id == source.artifact_id:
            if payload != {"source": True}:
                raise ValueError("invalid source schema")
        elif not isinstance(payload.get("index"), int):
            raise ValueError("invalid output schema")
        seen.append(record.artifact_id)
        if record.artifact_id == outputs[-1].artifact_id and hook[0] is not None:
            callback, hook[0] = hook[0], None
            callback()

    # This unit tests only _persist_terminal; no constructor/business execution.
    runner = AgentRuntimeRunner.__new__(AgentRuntimeRunner)
    runner.fault_injector = None
    monkeypatch.setattr(runner, "_schema_validators", lambda: {
        "terminal_trial_source": validator, "terminal_trial_output": validator,
        "summary_file": validator,
    })
    session = SimpleNamespace(context=SimpleNamespace(workspace=workspace, registry=registry))
    result = StageResult(stage_name="analyze", success=True, artifacts=[
        StageArtifactRef(row.artifact_role, row.artifact_type, row.artifact_version,
                         row.path, row.artifact_id) for row in outputs
    ])

    def publish(*, resolver_override=resolver, success=True):
        runner._persist_terminal(
            session, attempt_id="attempt-1", stage_name="analyze",
            result=result if success else replace(result, success=False),
            started_at="2026-10-25T00:00:00Z", model_call_count=0,
            external_registry_resolver=resolver_override,
        )

    return registry, owner, source, outputs, seen, hook, result, publish


def _terminals(registry):
    registry.reload()
    return [row for row in registry.list_records() if row.artifact_type == "runtime_stage_terminal"]


@pytest.mark.parametrize("count", [2, 12, 24])
@pytest.mark.parametrize("external", [False, True])
def test_terminal_has_bounded_registry_reads_and_validates_all_roots(tmp_path, monkeypatch, count, external):
    registry, owner, _source, outputs, seen, _hook, _result, publish = _case(
        tmp_path, monkeypatch, count=count, external=external,
    )
    reads = {"local": 0, "owner": 0}
    local_read = registry._read_registry_unlocked

    def counted_local():
        reads["local"] += 1
        return local_read()

    monkeypatch.setattr(registry, "_read_registry_unlocked", counted_local)
    if external:
        owner_read = owner._read_registry_unlocked

        def counted_owner():
            reads["owner"] += 1
            return owner_read()

        monkeypatch.setattr(owner, "_read_registry_unlocked", counted_owner)
    publish()
    assert set(seen) == {"shared-source", *(row.artifact_id for row in outputs)}
    assert all(seen.count(row.artifact_id) == 1 for row in outputs)
    assert len(seen) == count + 1
    assert reads["local"] <= 6, reads
    assert reads["owner"] <= 2, reads
    print(f"terminal roots={count} external={external} reads={reads} validated={len(seen)}")
    terminal = json.loads(Path(_terminals(registry)[0].path).read_text(encoding="utf-8"))
    assert terminal["status"] == "succeeded"
    assert len(terminal["output_artifact_refs"]) == count


@pytest.mark.parametrize("external", [False, True])
def test_terminal_rejects_late_shared_source_byte_change(tmp_path, monkeypatch, external):
    registry, _owner, source, _outputs, _seen, hook, _result, publish = _case(
        tmp_path, monkeypatch, external=external,
    )
    hook[0] = lambda: Path(source.path).write_text('{"source": "changed"}', encoding="utf-8")
    with pytest.raises(ReconcileValidationError, match="source|hash|changed"):
        publish()
    assert _terminals(registry) == []


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("revision_change", [False, True])
@pytest.mark.parametrize("change", ["metadata", "dependency_edge", "type_collision"])
def test_terminal_rejects_late_participating_record_change(tmp_path, monkeypatch, external, revision_change, change):
    registry, owner, source, _outputs, _seen, hook, _result, publish = _case(
        tmp_path, monkeypatch, external=external,
    )

    def mutate():
        path = Path(owner.registry_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        row = next(row for row in payload["artifacts"] if row["artifact_id"] == source.artifact_id)
        if change == "metadata":
            row["metadata"]["changed"] = True
        elif change == "type_collision":
            row["metadata"]["typed_flag"] = 0
        else:
            row["depends_on"].append(ArtifactDependencyRefV2.from_record(source).to_dict())
        payload["revision"] += int(revision_change)
        path.write_text(json.dumps(payload), encoding="utf-8")

    hook[0] = mutate
    with pytest.raises(ReconcileValidationError, match="record|changed|completion"):
        publish()
    assert _terminals(registry) == []


@pytest.mark.parametrize("external", [False, True])
def test_terminal_allows_unrelated_ready_append(tmp_path, monkeypatch, external):
    registry, owner, _source, _outputs, _seen, hook, _result, publish = _case(
        tmp_path, monkeypatch, external=external,
    )

    def append():
        path = tmp_path / "unrelated.json"
        path.write_text('{"diagnostic": true}', encoding="utf-8")
        peer = ArtifactRegistry(owner.registry_path, owner.job_id)
        peer.register_file(path=path, artifact_id="unrelated", artifact_role="diagnostic",
                           artifact_type="terminal_trial_diagnostic", artifact_version="v1", producer="test")

    hook[0] = append
    publish()
    assert len(_terminals(registry)) == 1
    owner.reload()
    assert owner.get("unrelated") is not None


def test_terminal_propagates_schema_failure_before_publication(tmp_path, monkeypatch):
    registry, _owner, _source, _outputs, _seen, hook, _result, publish = _case(tmp_path, monkeypatch)

    def invalid_schema():
        raise ValueError("deliberate schema failure")

    hook[0] = invalid_schema
    with pytest.raises(ValueError, match="schema failure"):
        publish()
    assert _terminals(registry) == []


def test_terminal_rejects_unresolved_external_owner(tmp_path, monkeypatch):
    registry, _owner, _source, _outputs, _seen, _hook, _result, publish = _case(
        tmp_path, monkeypatch, external=True,
    )
    with pytest.raises(ReconcileValidationError, match="external|registry"):
        publish(resolver_override=lambda job: None)
    assert _terminals(registry) == []


def test_terminal_does_not_reuse_validation_across_operations(tmp_path, monkeypatch):
    registry, _owner, source, _outputs, seen, _hook, _result, publish = _case(tmp_path, monkeypatch)
    publish()
    assert len(_terminals(registry)) == 1
    Path(source.path).write_text('{"source": "changed"}', encoding="utf-8")
    seen.clear()
    with pytest.raises(ReconcileValidationError, match="hash"):
        publish()
    assert len(_terminals(registry)) == 1


def test_failed_stage_preserves_failure_terminal_without_success_validation(tmp_path, monkeypatch):
    registry, _owner, _source, _outputs, seen, _hook, _result, publish = _case(tmp_path, monkeypatch)
    publish(success=False)
    assert seen == []
    terminal = json.loads(Path(_terminals(registry)[0].path).read_text(encoding="utf-8"))
    assert terminal["status"] == "failed"


def test_snapshot_begin_soft_none_is_rejected_before_success(tmp_path, monkeypatch):
    registry, _owner, _source, _outputs, _seen, _hook, _result, publish = _case(
        tmp_path, monkeypatch,
    )

    def unreadable_snapshot():
        raise OSError("independent snapshot read unavailable")

    monkeypatch.setattr(registry, "capture_record_snapshot", unreadable_snapshot)
    with pytest.raises(ReconcileValidationError, match="registry_reload_failed.*unavailable"):
        publish()
    assert _terminals(registry) == []


def test_unexpected_error_propagates_and_query_state_is_cleared(tmp_path, monkeypatch):
    registry, _owner, _source, _outputs, _seen, hook, _result, publish = _case(
        tmp_path, monkeypatch,
    )
    real_reconciler = runner_module.RuntimeReconciler
    instances = []

    def capture_instance(*args, **kwargs):
        instance = real_reconciler(*args, **kwargs)
        instances.append(instance)
        return instance

    def unexpected_error():
        raise RuntimeError("independent unexpected validator error")

    monkeypatch.setattr(runner_module, "RuntimeReconciler", capture_instance)
    hook[0] = unexpected_error
    with pytest.raises(RuntimeError, match="independent unexpected validator error"):
        publish()
    assert _terminals(registry) == []
    assert len(instances) == 1
    instance = instances[0]
    assert instance._completion_snapshots is None
    assert instance._completion_refreshed is None
    assert instance._completion_used_records == {}
    assert instance._completion_files == {}
    assert instance._validated_record_keys == set()


@pytest.mark.parametrize("success", [False, True])
def test_empty_outputs_keep_original_terminal_contract(tmp_path, success):
    workspace = JobWorkspace.create(str(tmp_path / "output"), "empty", job_id="empty-trial")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    runner = AgentRuntimeRunner.__new__(AgentRuntimeRunner)
    runner.fault_injector = None
    session = SimpleNamespace(context=SimpleNamespace(workspace=workspace, registry=registry))
    result = StageResult(stage_name="analyze", success=success, artifacts=[])

    def persist():
        runner._persist_terminal(
            session, attempt_id="empty-attempt", stage_name="analyze", result=result,
            started_at="2026-10-25T00:00:00Z", model_call_count=0,
            external_registry_resolver=lambda _job: None,
        )

    if success:
        with pytest.raises(StageTerminalContractError, match="at least one output"):
            persist()
        assert _terminals(registry) == []
    else:
        persist()
        records = _terminals(registry)
        assert len(records) == 1 and records[0].metadata["stage_status"] == "failed"
        assert records[0].depends_on == []


def test_foreign_registry_with_wrong_owner_is_rejected(tmp_path, monkeypatch):
    registry, _owner, source, _outputs, _seen, _hook, _result, publish = _case(
        tmp_path, monkeypatch, external=True,
    )
    wrong_workspace = JobWorkspace.create(
        str(tmp_path / "wrong"), "foreign", job_id="wrong-owner",
    )
    wrong = ArtifactRegistry(wrong_workspace.paths.registry_path, wrong_workspace.job_id)
    wrong.register_file(
        path=source.path, artifact_id=source.artifact_id, artifact_role=source.artifact_role,
        artifact_type=source.artifact_type, artifact_version=source.artifact_version,
        producer="independent-test", metadata=source.metadata,
    )
    with pytest.raises(ReconcileValidationError, match="job_id mismatch"):
        publish(resolver_override=lambda _job: wrong)
    assert _terminals(registry) == []

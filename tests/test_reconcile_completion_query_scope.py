from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.reconcile import RuntimeReconciler
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace


def _case(tmp_path: Path, count: int = 8, *, external: bool = False):
    workspace = JobWorkspace.create(str(tmp_path / "out"), "trial", job_id="completion-trial")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    source_registry = registry
    if external:
        parent = JobWorkspace.create(str(tmp_path / "parent"), "parent", job_id="source-owner")
        source_registry = ArtifactRegistry(parent.paths.registry_path, parent.job_id)
    source_path = tmp_path / "source.json"
    source_path.write_text('{"source": true}', encoding="utf-8")
    source = source_registry.register_file(path=source_path, artifact_id="shared-source", artifact_role="source",
        artifact_type="trial_source", artifact_version="v1", producer="test")
    resolver = lambda job: source_registry if job == source_registry.job_id else None
    outputs = []
    for index in range(count):
        path = tmp_path / f"output-{index}.json"
        path.write_text(json.dumps({"index": index}), encoding="utf-8")
        ref = ArtifactDependencyRefV2.from_record(source, dependency_kind="external_job" if external else "local_job")
        outputs.append(registry.register_file(path=path, artifact_id=f"output-{index}", artifact_role="output",
            artifact_type="summary_file" if index == 0 else "trial_output", artifact_version="v1", producer="test",
            depends_on=[ref], external_registry_resolver=resolver))
    terminal = TerminalStageRecordV1.create(job_id=workspace.job_id, attempt_id="attempt-1", stage_name="analyze",
        status="succeeded", producer="test", output_artifact_refs=[ArtifactDependencyRefV2.from_record(row) for row in outputs])
    StageTerminalStore(workspace, registry).persist(terminal)
    seen = []
    hook = [None]

    def validator(record, path):
        json.loads(path.read_text(encoding="utf-8"))
        seen.append(record.artifact_id)
        if record.artifact_id == outputs[-1].artifact_id and hook[0] is not None:
            callback, hook[0] = hook[0], None
            callback()

    reconciler = RuntimeReconciler(workspace, registry, schema_validators={
        "summary_file": validator, "trial_output": validator, "trial_source": validator,
    }, external_registry_resolver=resolver)
    return registry, source_registry, source, outputs, reconciler, seen, hook


@pytest.mark.parametrize("count", [2, 12, 24])
def test_completion_query_registry_reads_are_bounded_and_all_outputs_validate(tmp_path, monkeypatch, count):
    registry, _parent, _source, outputs, reconciler, seen, _hook = _case(tmp_path, count)
    reads = []
    original = registry._read_registry_unlocked

    def counted():
        reads.append(1)
        return original()

    monkeypatch.setattr(registry, "_read_registry_unlocked", counted)
    result = reconciler.load_completed_stage_result("analyze")
    assert result is not None and len(result.artifacts) == count
    assert set(seen) == {"shared-source", *(row.artifact_id for row in outputs)}
    assert all(seen.count(row.artifact_id) == 1 for row in outputs)
    assert len(reads) <= 3, f"{count} outputs caused {len(reads)} Registry reads"
    print(f"completion-query-count outputs={count} Registry_reads={len(reads)} outputs_validated={len(outputs)}")


def test_completion_query_rejects_late_source_byte_change(tmp_path):
    _registry, _parent, source, _outputs, reconciler, _seen, hook = _case(tmp_path)
    hook[0] = lambda: Path(source.path).write_text('{"source": "changed"}', encoding="utf-8")
    assert reconciler.load_completed_stage_result("analyze") is None
    assert reconciler.last_completion_diagnostics


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("revision_change", [False, True])
@pytest.mark.parametrize("change", ["metadata", "dependency_edge"])
def test_completion_query_rejects_late_participating_record_change(tmp_path, external, revision_change, change):
    _registry, source_registry, source, _outputs, reconciler, _seen, hook = _case(tmp_path, external=external)

    def mutate():
        path = Path(source_registry.registry_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        row = next(row for row in payload["artifacts"] if row["artifact_id"] == source.artifact_id)
        if change == "metadata":
            row["metadata"]["changed"] = True
        else:
            row["depends_on"].append(ArtifactDependencyRefV2.from_record(source).to_dict())
        payload["revision"] += int(revision_change)
        path.write_text(json.dumps(payload), encoding="utf-8")

    hook[0] = mutate
    assert reconciler.load_completed_stage_result("analyze") is None
    assert reconciler.last_completion_diagnostics


@pytest.mark.parametrize("external", [False, True])
def test_completion_query_allows_unrelated_ready_append(tmp_path, external):
    _registry, source_registry, _source, _outputs, reconciler, _seen, hook = _case(tmp_path, external=external)

    def append():
        path = tmp_path / "unrelated.json"
        path.write_text('{"diagnostic": true}', encoding="utf-8")
        peer = ArtifactRegistry(source_registry.registry_path, source_registry.job_id)
        peer.register_file(path=path, artifact_id="unrelated", artifact_role="diagnostic",
            artifact_type="trial_diagnostic", artifact_version="v1", producer="test")

    hook[0] = append
    assert reconciler.load_completed_stage_result("analyze") is not None


def test_completion_query_refreshes_again_for_next_query(tmp_path):
    _registry, _parent, source, _outputs, reconciler, seen, _hook = _case(tmp_path)
    original = Path(source.path).read_bytes()
    assert reconciler.load_completed_stage_result("analyze") is not None
    seen.clear()
    Path(source.path).write_text('{"source": "changed"}', encoding="utf-8")
    assert reconciler.load_completed_stage_result("analyze") is None
    Path(source.path).write_bytes(original)
    assert reconciler.load_completed_stage_result("analyze") is not None
    assert "shared-source" in seen


def test_stage_is_complete_queries_are_independent_and_fenced(tmp_path):
    _registry, _parent, source, _outputs, reconciler, _seen, hook = _case(tmp_path)
    assert reconciler.stage_is_complete("analyze") is True
    hook[0] = lambda: Path(source.path).write_text('{"source": "late change"}', encoding="utf-8")
    assert reconciler.stage_is_complete("analyze") is False


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("revision_change", [False, True])
@pytest.mark.parametrize("before,after", [(False, 0), (1, 1.0)], ids=["bool-to-int", "int-to-float"])
def test_completion_query_rejects_late_metadata_type_collision(tmp_path, external, revision_change, before, after):
    _registry, owner, source, _outputs, reconciler, _seen, hook = _case(tmp_path, count=2, external=external)
    path = Path(owner.registry_path)
    initial = json.loads(path.read_text(encoding="utf-8"))
    next(row for row in initial["artifacts"] if row["artifact_id"] == source.artifact_id)["metadata"]["typed_flag"] = before
    path.write_text(json.dumps(initial), encoding="utf-8")

    def collide():
        payload = json.loads(path.read_text(encoding="utf-8"))
        next(row for row in payload["artifacts"] if row["artifact_id"] == source.artifact_id)["metadata"]["typed_flag"] = after
        payload["revision"] += int(revision_change)
        path.write_text(json.dumps(payload), encoding="utf-8")

    hook[0] = collide
    assert reconciler.load_completed_stage_result("analyze") is None
    assert reconciler.last_completion_diagnostics


@pytest.mark.parametrize("external", [False, True])
def test_completion_query_rejects_type_collision_before_record_cache_hit(tmp_path, external):
    _registry, owner, source, _outputs, reconciler, _seen, hook = _case(tmp_path, count=2, external=external)
    path = Path(owner.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    next(row for row in payload["artifacts"] if row["artifact_id"] == source.artifact_id)["metadata"]["typed_flag"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    def collide_read_view():
        owner._artifacts[source.artifact_id].metadata["typed_flag"] = 0

    hook[0] = collide_read_view
    assert reconciler.load_completed_stage_result("analyze") is None

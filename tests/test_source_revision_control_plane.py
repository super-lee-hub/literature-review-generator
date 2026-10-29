"""Public plan publication/order tests; parent admissibility has its own suite."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.runner import AgentRuntimeRunner
from runtime.source_revision import SourceRevisionError, child_core_spec_hash
from runtime.provider_runtime import hash_json
from tests.test_source_revision_metadata import CONTRACT, _spec


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    import runtime.source_revision_admission as admission

    parent = tmp_path / "parent"
    parent.mkdir()
    spec = replace(_spec(tmp_path), queue_file=str(tmp_path / "queue.json"),
                   metadata={"provider_semantic_contract_version": CONTRACT})
    seen = []
    monkeypatch.setattr("runtime.control_plane._load_spec_path", lambda _: spec)
    monkeypatch.setattr(AgentRuntimeRunner, "_normalized_spec", lambda self, *, resume: self.job_spec)

    def build(normalized, parent_path):
        assert normalized.to_dict() == spec.to_dict()
        assert parent_path == parent
        return {"child_workspace_path": spec.workspace_path,
                "child_core_spec_hash": child_core_spec_hash(normalized.to_dict())}

    def verify(bound):
        seen.append(bound)
        reference = bound.metadata["source_revision_plan"]
        data = Path(reference["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == reference["file_sha256"]
        assert hash_json(json.loads(data)) == reference["content_hash"]
        return json.loads(data)

    monkeypatch.setattr(admission, "build_source_revision_plan_v1", build)
    monkeypatch.setattr(admission, "verify_source_revision_plan_v1", verify)
    return spec, parent, seen


def test_plan_publication_binds_exact_file_and_acyclic_normalized_child(publication_case, tmp_path):
    spec, parent, seen = publication_case
    output = tmp_path / "plan.json"
    before = spec.to_dict()
    result = ReviewControlPlane().source_revision_plan("spec.json", parent_workspace=parent, output_path=output)
    assert result["status"] == "source_revision_planned"
    assert result["provider_posts_emitted"] == 0
    assert result["workspace_created"] is False
    assert result["provider_budget_or_envelope_created"] is False
    assert result["writes"] == [str(output)]
    assert len(seen) == 1
    assert result["normalized_child_spec"] == seen[0].to_dict()
    assert result["child_core_spec_hash"] == hash_json(before)
    assert result["child_spec_hash"] == hash_json(seen[0].to_dict())
    assert result["child_core_spec_hash"] != result["child_spec_hash"]
    assert spec.to_dict() == before
    assert not Path(spec.workspace_path).exists()
    assert list(parent.iterdir()) == []


def test_normalization_drift_rejects_before_plan_file_write(publication_case, tmp_path, monkeypatch):
    _specification, parent, seen = publication_case

    def normalize(self, *, resume):
        spec = self.job_spec
        if "source_revision_plan" in spec.metadata:
            return replace(spec, metadata={**spec.metadata, "audit_reason": "unexpected drift"})
        return spec

    monkeypatch.setattr(AgentRuntimeRunner, "_normalized_spec", normalize)
    output = tmp_path / "plan.json"
    with pytest.raises(ControlPlaneError, match="normalization changed"):
        ReviewControlPlane().source_revision_plan("spec.json", parent_workspace=parent, output_path=output)
    assert not output.exists()
    assert seen == []


@pytest.mark.parametrize("location", ["existing", "parent", "child"])
def test_plan_cannot_overwrite_or_write_inside_run_workspaces(publication_case, tmp_path, location):
    spec, parent, seen = publication_case
    if location == "existing":
        output = tmp_path / "plan.json"
        output.write_bytes(b"preserve existing bytes")
    else:
        folder = parent if location == "parent" else Path(spec.workspace_path)
        folder.mkdir(exist_ok=True)
        output = folder / "plan.json"
    with pytest.raises(ControlPlaneError, match="fresh file outside"):
        ReviewControlPlane().source_revision_plan("spec.json", parent_workspace=parent, output_path=output)
    if location == "existing":
        assert output.read_bytes() == b"preserve existing bytes"
    else:
        assert not output.exists()
    assert seen == []


def test_runner_verifies_revision_before_bootstrap_or_budget_creation(tmp_path, monkeypatch):
    import runtime.source_revision_admission as admission

    spec = replace(_spec(tmp_path), queue_file=str(tmp_path / "queue.json"))
    Path(spec.config).write_text("placeholder config bytes", encoding="utf-8")
    spec = replace(spec, metadata={"provider_semantic_contract_version": CONTRACT, "source_revision_plan": {
        "schema_version": "source-revision-plan-ref/v1", "path": str(tmp_path / "missing-plan.json"),
        "file_sha256": "1" * 64, "content_hash": "2" * 64,
    }})
    monkeypatch.setattr(AgentRuntimeRunner, "_normalized_spec", lambda self, *, resume: self.job_spec)
    calls = []

    def verify(bound):
        calls.append(bound)
        raise SourceRevisionError("reject before bootstrap")

    def forbidden_bridge(*_args, **_kwargs):
        pytest.fail("an inadmissible source revision reached workspace/bootstrap")

    monkeypatch.setattr(admission, "verify_source_revision_plan_v1", verify)
    monkeypatch.setattr("runtime.runner.AgentRuntimeBridge", forbidden_bridge)
    runner = AgentRuntimeRunner(spec)
    with pytest.raises(SourceRevisionError, match="before bootstrap"):
        runner._execute(resume=False)
    assert calls == [spec]
    assert not Path(spec.workspace_path).exists()


def test_cli_exposes_explicit_source_revision_plan_arguments():
    from reviewctl import build_parser

    args = build_parser().parse_args([
        "source-revision-plan", "--spec", "child.json", "--parent-workspace", "parent", "--output", "plan.json",
    ])
    assert (args.command, args.spec, args.parent_workspace, args.output) == (
        "source-revision-plan", "child.json", "parent", "plan.json",
    )

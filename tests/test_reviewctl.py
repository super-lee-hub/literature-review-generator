from __future__ import annotations

import json
from pathlib import Path

from reviewctl import main as reviewctl_main
from runtime.control_plane import FORBIDDEN_ACTIONS, ReviewControlPlane
from runtime.outline_v3_dag import OutlineNodeStore
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


def _spec(tmp_path: Path):
    from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec

    pdf_folder = tmp_path / "pdfs"
    pdf_folder.mkdir()
    (pdf_folder / "paper.pdf").write_bytes(b"%PDF-1.4\n% synthetic fixture\n")
    config = tmp_path / "config.ini"
    config.write_text(
        """[Paths]\noutput_path = {output}\n\n[Primary_Reader_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n\n[Backup_Reader_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n\n[Writer_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n""".format(output=tmp_path / "output"),
        encoding="utf-8",
    )
    return RuntimeJobSpec(
        project_name="reviewctl",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        config=str(config),
        action="analyze",
        metadata={"requested_stages": ["analyze"]},
    )


def test_reviewctl_plan_and_doctor_emit_machine_json(tmp_path: Path, capsys) -> None:
    spec = _spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    assert reviewctl_main(["plan", "--spec", str(spec_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "planned"
    assert plan["stages"] == ["source_intake", "analyze"]

    assert reviewctl_main(["doctor", "--repo-root", str(tmp_path), "--config", str(tmp_path / "missing.ini")]) == 1
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["status"] == "fail"
    assert "dummy" not in json.dumps(doctor)


def test_control_plane_retry_node_uses_persisted_outline_v3_scope(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "review", "job-v3-control")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    store = OutlineNodeStore(workspace, registry)
    store.ensure(workspace.job_id, candidate_count=3)
    store.record_node("structure_critique", status="failed", diagnostics=["synthetic_failure"])

    response = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path]).retry_node(
        workspace=workspace.root_dir,
        node_id="structure_critique",
    )

    assert response["status"] == "planned"
    assert response["mutation_performed"] is True
    assert response["resume_required"] is True
    assert "structure_critique" in response["resume_plan"]["rerun_node_ids"]
    assert "candidate_1" not in response["resume_plan"]["rerun_node_ids"]
    assert response["read_only"] is False

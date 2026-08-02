from __future__ import annotations

import json
from pathlib import Path

from reviewctl import main as reviewctl_main
from runtime.control_plane import FORBIDDEN_ACTIONS, ReviewControlPlane
from runtime.runner import AgentRuntimeRunner
from tests.test_runtime_bridge_helpers import build_legacy_main, build_success_summary


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


def _handler(_tmp_path: Path):
    def handler(stage_name, request):
        assert stage_name == "stage1_analyze"
        item = request.source_bundle.paper_work_items[0]
        summary = build_success_summary(
            item.source_pdf and Path(item.source_pdf),
            paper_key=item.canonical_paper_key,
        )
        summary["paper_info"]["source_paper_id"] = item.source_paper_id
        return {
            "summaries": [summary],
            "source_items": [],
        }

    return handler


def test_control_plane_status_and_next_action_are_provider_free(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path),
        origin_dir=tmp_path,
    ).run()

    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])
    status = control.status(workspace=result.workspace_path)
    assert status["job_status"] == "completed"
    assert status["completion_status"] == "complete"
    next_action = control.next_action(workspace=result.workspace_path)
    assert next_action["status"] == "complete"
    assert next_action["recommended_action"]["command"] == "none"
    assert next_action["forbidden_actions"] == list(FORBIDDEN_ACTIONS)


def test_control_plane_inspect_does_not_create_registry_lock(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path),
        origin_dir=tmp_path,
    ).run()
    registry_lock = Path(result.workspace_path) / "artifact_registry.json.lock"
    if registry_lock.exists():
        registry_lock.unlink()
    inspection = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path]).inspect(
        workspace=result.workspace_path
    )
    assert inspection["read_only"] is True
    assert not registry_lock.exists()
    assert inspection["canonical_evidence_hash"]


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


def test_unavailable_node_replay_is_explicitly_blocked(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = AgentRuntimeRunner(
        spec,
        legacy_main=build_legacy_main(),
        stage_handler=_handler(tmp_path),
        origin_dir=tmp_path,
    ).run()
    response = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path]).retry_node(
        workspace=result.workspace_path,
        node_id="structure_critique",
    )
    assert response["status"] == "blocked"
    assert response["safe_to_retry"] is False
    assert response["read_only"] is True

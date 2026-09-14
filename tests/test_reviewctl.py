from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from dataclasses import replace

from reviewctl import main as reviewctl_main
from runtime.control_plane import FORBIDDEN_ACTIONS, ReviewControlPlane
from runtime.outline_v3_dag import OutlineNodeStore
from services.artifact_registry import ArtifactRegistry
from services.credential_provenance import PREPROCESS_ENV_MAPPING
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


def test_reviewctl_run_domain_failure_is_machine_json(tmp_path: Path, capsys) -> None:
    spec = _spec(tmp_path)
    spec = replace(spec, config=str(tmp_path / "missing.ini"))
    spec_path = tmp_path / "missing-config-spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    assert reviewctl_main(["run", "--spec", str(spec_path)]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "error"
    assert payload["error_type"] == "RuntimeRunnerError"
    assert "Traceback" not in output


def test_doctor_does_not_call_unlocked_persistent_lock_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "queue.json.lock"
    lock_path.write_text("persistent queue lock\n", encoding="utf-8")
    old = time.time() - 24 * 60 * 60
    os.utime(lock_path, (old, old))

    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])

    assert control._stale_locks(tmp_path) == []


def test_doctor_excludes_dependency_lock_from_runtime_lock_diagnostics(tmp_path: Path) -> None:
    lock_path = tmp_path / "requirements-py311-windows.lock"
    lock_path.write_text("package==1.0\n", encoding="utf-8")
    old = time.time() - 24 * 60 * 60
    os.utime(lock_path, (old, old))

    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])

    assert control._stale_locks(tmp_path) == []


def test_doctor_fails_for_configured_missing_certificate(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        (Path(__file__).resolve().parents[1] / "config.ini.example").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    missing_certificate = tmp_path / "missing-ca.pem"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(missing_certificate))

    result = ReviewControlPlane(repo_root=tmp_path).doctor(config_path=config_path)

    certificate = next(
        check for check in result["checks"] if check["name"] == "certificate_paths"
    )
    assert certificate["status"] == "fail"
    assert certificate["details"]["valid"] is False
    assert result["ok"] is False


def _clear_preprocess_environment(monkeypatch) -> None:
    for env_name in PREPROCESS_ENV_MAPPING:
        monkeypatch.delenv(env_name, raising=False)


def _example_config(path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    path.write_text(
        re.sub(
            r"(?im)^(api_key\s*=\s*).*$",
            r"\1test-provider-key",
            source.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def test_doctor_reports_mineru_remote_fallback_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_preprocess_environment(monkeypatch)
    config_path = tmp_path / "config.ini"
    _example_config(config_path)

    result = ReviewControlPlane(repo_root=tmp_path).doctor(config_path=config_path)

    admission = next(
        check for check in result["checks"] if check["name"] == "mineru_remote_admission"
    )
    assert admission["status"] == "warn"
    assert admission["details"] == {
        "status": "warn",
        "remote_requested": True,
        "token_present": False,
        "fallback_will_be_used": True,
        "parser_mode": "hybrid",
        "primary_parser": "mineru_remote",
        "fallback_parser": "local",
        "network_probe": False,
        "reason": "remote_parser_admission_failed",
        "error_type": "ValueError",
    }
    assert result["provider_network_calls"] == 0


def test_provider_preflight_fails_when_remote_mineru_has_no_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_preprocess_environment(monkeypatch)
    config_path = tmp_path / "config.ini"
    _example_config(config_path)
    (tmp_path / ".env").write_text(
        "ALLOW_LOCAL_PARSE_FALLBACK=false\n",
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).provider_preflight(
        config_path=config_path,
        action="analyze",
    )

    assert result["status"] == "fail"
    assert result["ok"] is False
    admission = result["mineru_remote_admission"]
    assert admission["remote_requested"] is True
    assert admission["token_present"] is False
    assert admission["fallback_will_be_used"] is False
    assert admission["network_probe"] is False
    assert result["network_calls"] == 0


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

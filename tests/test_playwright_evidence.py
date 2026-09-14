from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from runtime.playwright_evidence import (
    PlaywrightEvidenceCollector,
    PlaywrightEvidenceError,
    PlaywrightProductionScenarioInputV2,
    PlaywrightScenarioInputV1,
    _runtime_source_mode_for_gui_input,
)
from services.artifact_registry import ArtifactRegistry
from launch_gui import _pick_available_port


def _input_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_type": "acceptance_gui_input",
        "artifact_version": "v1",
        "schema_version": "acceptance-gui-input-v1",
        "base_url": "http://127.0.0.1:8080",
        "config_path": "gui.ini",
        "workspace": "workspace-gui",
        "resulting_job_id": "job-gui",
        "repo_root": ".",
        "port": 8080,
    }
    payload.update(overrides)
    return payload


def test_playwright_input_requires_localhost_origin() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="localhost"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(base_url="https://remote.example.test")
        )


def test_playwright_input_requires_a_resulting_runtime_job() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="resulting_job_id"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(resulting_job_id="")
        )


def test_playwright_input_requires_launcher_url_port_match() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="port"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(base_url="http://127.0.0.1:8081", port=8080)
        )


def test_launcher_strict_port_rejects_browser_unsafe_port() -> None:
    with pytest.raises(RuntimeError, match="unavailable or browser-unsafe"):
        _pick_available_port(6566, strict=True)


def test_launcher_strict_port_rejects_an_occupied_port() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with pytest.raises(RuntimeError, match="unavailable or browser-unsafe"):
            _pick_available_port(port, strict=True)


def test_production_playwright_input_describes_request_without_preselected_job(tmp_path: Path) -> None:
    parsed = PlaywrightProductionScenarioInputV2.from_mapping(
        {
            "artifact_type": "acceptance_gui_input",
            "artifact_version": "v2",
            "schema_version": "acceptance-gui-input-v2",
            "execution_kind": "production",
            "base_url": "http://127.0.0.1:8080",
            "config_path": str(tmp_path / "config.ini"),
            "repo_root": str(tmp_path),
            "output_root": str(tmp_path / "output"),
            "input_mode": "pdf",
            "pdf_folder": str(tmp_path / "pdfs"),
            "project_name": "gui-production",
            "work_mode": "normal",
            "action": "analyze",
            "port": 8080,
        }
    )

    assert parsed.project_name == "gui-production"
    assert parsed.action == "analyze"
    assert not hasattr(parsed, "resulting_job_id")


def test_production_playwright_input_rejects_page_smoke_execution_kind(tmp_path: Path) -> None:
    payload = {
        "artifact_type": "acceptance_gui_input",
        "artifact_version": "v2",
        "schema_version": "acceptance-gui-input-v2",
        "execution_kind": "page_smoke",
        "base_url": "http://127.0.0.1:8080",
        "config_path": str(tmp_path / "config.ini"),
        "repo_root": str(tmp_path),
        "output_root": str(tmp_path / "output"),
        "input_mode": "pdf",
        "pdf_folder": str(tmp_path / "pdfs"),
        "project_name": "gui-production",
        "action": "analyze",
        "port": 8080,
    }
    with pytest.raises(PlaywrightEvidenceError, match="execution_kind"):
        PlaywrightProductionScenarioInputV2.from_mapping(payload)


def test_production_gui_input_mode_maps_to_runtime_source_mode() -> None:
    assert _runtime_source_mode_for_gui_input("pdf") == "direct"
    assert _runtime_source_mode_for_gui_input("zotero") == "zotero"
    with pytest.raises(PlaywrightEvidenceError, match="unsupported GUI production input mode"):
        _runtime_source_mode_for_gui_input("unknown")


def test_production_artifacts_are_relocated_into_job_workspace(tmp_path: Path) -> None:
    staging = tmp_path / "output" / "acceptance_gui_staging" / "run"
    staging.mkdir(parents=True)
    trace = staging / "trace.zip"
    screenshot = staging / "dashboard.png"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("trace.trace", "{}")
    screenshot.write_bytes(b"png")
    job_workspace = tmp_path / "output" / "project__job"
    collector = PlaywrightEvidenceCollector(
        PlaywrightProductionScenarioInputV2(
            base_url="http://127.0.0.1:8080",
            config_path=str(tmp_path / "config.ini"),
            repo_root=str(tmp_path),
            output_root=str(tmp_path / "output"),
            input_mode="pdf",
            pdf_folder=str(tmp_path / "pdfs"),
            zotero_report="",
            library_path="",
            project_name="project",
            work_mode="normal",
            action="analyze",
            port=8080,
        ),
        acceptance_run_id="acceptance-i",
        scenario_id="I",
        final_executable_sha="a" * 40,
    )

    browser_path, trace_path, manifest_path, screenshot_path = collector._relocate_production_artifacts(
        staging_trace_path=trace,
        staging_screenshot_path=screenshot,
        job_workspace=job_workspace,
    )

    assert trace_path.parent == job_workspace / "acceptance_gui"
    assert screenshot_path.parent == job_workspace / "acceptance_gui"
    assert browser_path.parent == manifest_path.parent == trace_path.parent
    assert trace_path.is_file()
    assert screenshot_path.is_file()
    assert not trace.exists()
    assert not screenshot.exists()


def test_playwright_collector_registers_all_durable_outputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = {
        "browser_path": workspace / "playwright_run_evidence.json",
        "trace_path": workspace / "trace.zip",
        "screenshot_manifest_path": workspace / "screenshot_manifest.json",
        "screenshot_path": workspace / "dashboard.png",
    }
    paths["browser_path"].write_text(
        json.dumps(
            {
                "artifact_type": "playwright_run_evidence",
                "artifact_version": "v1",
                "schema_version": "playwright-run-evidence-v1",
                "run_id": "acceptance-i",
                "session_id": "session-i",
                "url": "http://127.0.0.1:8123",
                "resulting_job_id": "gui-job",
                "trace_sha256": "a" * 64,
                "flow_assertions": [{"name": "dashboard", "passed": True}],
                "console_errors": [],
                "page_errors": [],
            }
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(paths["trace_path"], "w") as archive:
        archive.writestr("trace.trace", "{}")
    paths["screenshot_manifest_path"].write_text(
        json.dumps(
            {
                "artifact_type": "playwright_screenshot_manifest",
                "artifact_version": "v1",
                "schema_version": "playwright-screenshot-manifest-v1",
                "screenshots": [{"name": "dashboard", "path": "dashboard.png"}],
            }
        ),
        encoding="utf-8",
    )
    paths["screenshot_path"].write_bytes(b"png")
    scenario_input = PlaywrightScenarioInputV1(
        base_url="http://127.0.0.1:8123",
        config_path=str(tmp_path / "config.ini"),
        workspace=str(workspace),
        resulting_job_id="gui-job",
        repo_root=str(tmp_path),
        port=8123,
    )
    collector = PlaywrightEvidenceCollector(
        scenario_input,
        acceptance_run_id="acceptance-i",
        scenario_id="I",
        final_executable_sha="a" * 40,
    )

    collector._register_artifacts(**paths)

    registry = ArtifactRegistry(workspace / "artifact_registry.json", "gui-job")
    assert {
        record.artifact_type
        for record in registry.list_records()
    } == {
        "playwright_run_evidence",
        "playwright_trace",
        "playwright_screenshot_manifest",
        "playwright_screenshot",
    }

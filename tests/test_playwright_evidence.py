from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from runtime.playwright_evidence import (
    PlaywrightEvidenceCollector,
    PlaywrightEvidenceError,
    PlaywrightScenarioInputV1,
)
from services.artifact_registry import ArtifactRegistry


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

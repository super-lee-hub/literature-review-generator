from __future__ import annotations

import pytest

from runtime.playwright_evidence import (
    PlaywrightEvidenceError,
    PlaywrightScenarioInputV1,
)


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

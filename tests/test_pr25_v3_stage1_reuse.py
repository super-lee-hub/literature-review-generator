from __future__ import annotations

import configparser
import json
from pathlib import Path

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, load_runtime_job_spec


def test_run_all_default_stage1_reuse_is_visible_to_read_only_plan(tmp_path: Path) -> None:
    """Planning must honor run_all's default reuse without executing a provider."""
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section_name in parser.sections():
        if section_name.endswith("_API"):
            section = parser[section_name]
            if "api_key" in section:
                section["api_key"] = "local-fixture-only"
            if "api_base" in section:
                section["api_base"] = "http://127.0.0.1:1/v1"
    with config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)

    pdf_folder = tmp_path / "pdfs"
    pdf_folder.mkdir()
    spec = RuntimeJobSpec(
        project_name="stage1-reuse-plan-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        config=str(config_path),
        action="run_all",
        metadata={
            "requested_stages": [
                "source_intake",
                "analyze",
                "outline",
                "review",
                "validate",
            ]
        },
    )
    payload = spec.to_dict()
    payload.pop("reuse_stage1")
    spec_path = tmp_path / "runtime-spec.json"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded_spec = load_runtime_job_spec(spec_path)
    request = loaded_spec.to_job_request()
    assert request.reuse_stage1 is True

    plan = ReviewControlPlane(repo_root=tmp_path).plan(spec_path)
    full_stage = plan["full_stage_request_plan"]
    assert plan["provider_calls"] == "not executed"
    assert full_stage["boundary"]["no_provider_posts"] is True
    assert full_stage["stage1_reuse_authority_status"] == "not_verified_by_spec_alone"

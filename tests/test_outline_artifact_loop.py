import json
from pathlib import Path
from typing import Any, cast

import main
from config_loader import ConfigDict
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


def _resume_report(workspace: JobWorkspace) -> ResumeStateReport:
    return ResumeStateReport(
        artifact_type="resume_state_report",
        artifact_version="v1",
        created_from_job_id=workspace.job_id,
        created_at="2026-04-03T00:00:00Z",
        project_name=workspace.project_name,
        job_id=workspace.job_id,
        state="non_resumable",
        reason="test bootstrap",
        summary_file=workspace.artifact_path(f"{workspace.project_name}_summaries.json"),
        progress_snapshot_file=None,
        checkpoint_file=workspace.checkpoint_path(f"{workspace.project_name}_checkpoint.json"),
        fingerprint_bundle={"request": "demo"},
    )


def _make_bound_generator(tmp_path: Path, project_name: str = "demo", job_id: str | None = None):
    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), project_name, job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict(
        {
            "Paths": {"output_path": str(output_dir)},
            "Writer_API": {"api_key": "writer-key"},
            "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"},
        }
    )
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name=project_name, pdf_folder=None)
    generator.logger = _DummyLogger()  # type: ignore[assignment]
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    generator.summaries = [{"status": "success", "paper_info": {"title": "Paper A"}}]
    generator.summary_file = workspace.artifact_path(f"{project_name}_summaries.json")
    Path(generator.summary_file).write_text(json.dumps(generator.summaries), encoding="utf-8")
    registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=generator.summary_file,
        producer="tests",
    )
    return generator, workspace, registry


def _stub_stage2_bootstrap(monkeypatch, generator) -> None:
    monkeypatch.setattr(generator, "load_configuration", lambda: True)
    monkeypatch.setattr(generator, "setup_output_directory", lambda: True)
    monkeypatch.setattr(generator, "load_existing_summaries", lambda: True)


def test_create_literature_review_outline_registers_outline_artifact(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-outline")
    outline_text = "# Demo Outline\n\n## 1. Registered Section\n\nNotes"

    monkeypatch.setattr(generator, "prepare_review_data", lambda: {"summaries": []})
    monkeypatch.setattr(generator, "generate_review_outline", lambda _review_data: outline_text)

    assert generator.create_literature_review_outline() is True

    outline_path = Path(workspace.artifact_path("demo_literature_review_outline.md"))
    legacy_outline_path = Path(tmp_path / "output" / "demo" / "demo_literature_review_outline.md")
    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    outline_records = [
        item for item in registry_payload["artifacts"] if item["artifact_type"] == "literature_review_outline"
    ]

    assert outline_path.read_text(encoding="utf-8") == outline_text
    assert legacy_outline_path.exists() is False
    assert len(outline_records) == 1
    assert outline_records[0]["artifact_id"] == "literature_review_outline"
    assert outline_records[0]["artifact_role"] == "outline"
    assert outline_records[0]["artifact_version"] == "v1"
    assert outline_records[0]["path"] == str(outline_path.resolve())
    assert outline_records[0]["depends_on"][0]["artifact_type"] == "summary_file"


def test_generate_section_prefers_registered_outline_over_legacy_fallback(tmp_path: Path, monkeypatch) -> None:
    generator, _workspace, _registry = _make_bound_generator(tmp_path, job_id="job-prefers-registry")
    _stub_stage2_bootstrap(monkeypatch, generator)

    generator._write_outline_artifact(
        "# Demo Outline\n\n## 1. Registry Section\n\nRegistry details",
        producer="tests",
    )
    legacy_outline_path = Path(generator._get_legacy_outline_file_path())
    legacy_outline_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_outline_path.write_text(
        "# Demo Outline\n\n## 1. Legacy Section\n\nLegacy details",
        encoding="utf-8",
    )

    captured = {}

    def _fake_create(section_number: int, section_title: str, outline_content: str) -> bool:
        captured["section_number"] = section_number
        captured["section_title"] = section_title
        captured["outline_content"] = outline_content
        return True

    monkeypatch.setattr(generator, "create_literature_review_section", _fake_create)

    assert generator.generate_specific_review_section(1) is True
    assert captured["section_number"] == 1
    assert captured["section_title"].endswith("Registry Section")
    assert "Registry details" in captured["outline_content"]
    assert "Legacy details" not in captured["outline_content"]


def test_generate_section_falls_back_to_legacy_outline_when_workspace_registry_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    producer_generator, _workspace, _registry = _make_bound_generator(tmp_path, job_id="job-producer")
    legacy_outline_path = Path(producer_generator._get_legacy_outline_file_path())
    legacy_outline_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_outline_path.write_text(
        "# Demo Outline\n\n## 1. Compatibility Section\n\nCompatibility details",
        encoding="utf-8",
    )

    downstream_generator, downstream_workspace, _downstream_registry = _make_bound_generator(
        tmp_path,
        job_id="job-consumer",
    )
    _stub_stage2_bootstrap(monkeypatch, downstream_generator)
    workspace_outline_path = Path(downstream_workspace.artifact_path("demo_literature_review_outline.md"))
    if workspace_outline_path.exists():
        workspace_outline_path.unlink()

    captured = {}

    def _fake_create(section_number: int, section_title: str, outline_content: str) -> bool:
        captured["section_number"] = section_number
        captured["section_title"] = section_title
        captured["outline_content"] = outline_content
        return True

    monkeypatch.setattr(downstream_generator, "create_literature_review_section", _fake_create)

    assert downstream_generator.generate_specific_review_section(1) is True
    assert captured["section_number"] == 1
    assert captured["section_title"].endswith("Compatibility Section")
    assert "Compatibility details" in captured["outline_content"]
    assert Path(downstream_workspace.artifact_path("demo_literature_review_outline.md")).exists() is False


def test_generate_review_fails_when_no_outline_is_available(tmp_path: Path, monkeypatch) -> None:
    generator, _workspace, _registry = _make_bound_generator(tmp_path, job_id="job-missing-outline")
    _stub_stage2_bootstrap(monkeypatch, generator)

    assert generator.generate_full_review_from_outline() is False



def test_create_literature_review_outline_v2_runs_pipeline_and_registers_artifacts(tmp_path: Path, monkeypatch) -> None:
    generator, workspace, _registry = _make_bound_generator(tmp_path, job_id="job-v2-outline")
    config = generator.config
    assert config is not None
    config.setdefault("Outline", {})["enable_outline_intelligence_v2"] = "true"
    config["Outline"]["test_dev_fixture_mode"] = "true"
    generator.compat_config = CompatConfigView.from_config(config)
    generator.summaries = cast(
        main.SummariesList,
        [
            {"paper_info": {"title": "Core Paper", "classification": "core", "must_use": True}, "themes": ["core"], "methods": ["m"]},
            {"paper_info": {"title": "Support Paper", "classification": "support"}, "themes": ["support"], "methods": ["m2"]},
            {"paper_info": {"title": "Background Paper", "classification": "background_only"}, "themes": ["background"]},
        ],
    )

    assert generator.create_literature_review_outline() is True

    expected = [
        "demo_literature_map.json",
        "demo_synthesis_flow.json",
        "demo_outline_candidate_generation_report.json",
        "demo_outline_candidates.json",
        "demo_outline_critiques.json",
        "demo_outline_arbitration_report.json",
        "demo_final_outline.json",
        "demo_outline_coverage_audit.json",
    ]
    for filename in expected:
        assert Path(workspace.artifact_path(filename)).exists(), filename

    audit_payload = json.loads(Path(workspace.artifact_path("demo_outline_coverage_audit.json")).read_text(encoding="utf-8"))
    final_payload = json.loads(Path(workspace.artifact_path("demo_final_outline.json")).read_text(encoding="utf-8"))
    assert "quality_gate_policy_snapshot" in audit_payload
    assert "canonical_paper_coverage_ratio" in audit_payload
    assert "duplicate_assignment_count" in audit_payload
    assert "effective_section_count" in audit_payload
    assert "placeholder_section_count" in audit_payload
    assert "blocking_critique_ids" in final_payload
    assert "unresolved_critique_ids" in final_payload
    assert "applied_critique_ids" in final_payload

    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_ids = {item["artifact_id"] for item in registry_payload["artifacts"]}
    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}
    assert "candidate_generation_report" in artifact_ids
    assert "candidate_generation_report" in artifact_types
    assert "outline_arbitration_report" in artifact_ids
    final_record = next(item for item in registry_payload["artifacts"] if item["artifact_id"] == "final_outline")
    assert all(dep["artifact_type"] != "candidate_generation_report" for dep in final_record["depends_on"])
    assert all(dep["content_hash"] for dep in final_record["depends_on"])



def test_generate_review_v2_enabled_without_adoption_fails_closed(tmp_path: Path, monkeypatch) -> None:
    generator, _workspace, _registry = _make_bound_generator(tmp_path, job_id="job-v2-review-gate")
    _stub_stage2_bootstrap(monkeypatch, generator)
    config = generator.config
    assert config is not None
    config.setdefault("Outline", {})["enable_outline_intelligence_v2"] = "true"
    generator.compat_config = CompatConfigView.from_config(config)

    legacy = Path(generator._get_legacy_outline_file_path())
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Legacy\n\n## 1. Should Not Load", encoding="utf-8")

    assert generator.generate_full_review_from_outline() is False


def test_candidate_generation_report_persisted_on_v2_candidate_failure(tmp_path: Path) -> None:
    from outline.pipeline import V2Pipeline

    generator, workspace, registry = _make_bound_generator(tmp_path, job_id="job-v2-candidate-failure")
    summaries: list[dict[str, Any]] = [
        {"paper_info": {"title": "Lone Paper", "authors": ["A"], "year": 2020}, "themes": ["solo"]}
    ]
    generator.summaries = cast(main.SummariesList, summaries)

    def bad_candidate_caller(_route, _prompt, _metadata):
        return {"candidates": [{"candidate_id": "bad", "sections": []}]}

    pipeline = V2Pipeline(
        job_id=workspace.job_id,
        summaries=summaries,
        config_view=generator.compat_config,
        artifact_registry=registry,
        workspace=workspace,
        output_dir=str(tmp_path / "output"),
        project_name="demo",
        model_caller=bad_candidate_caller,
        logger=generator.logger,
    )

    result = pipeline.run(candidate_count=3, test_dev_mode=False, generator_model="Outline_API")

    assert result.ok is False
    report_path = Path(workspace.artifact_path("demo_outline_candidate_generation_report.json"))
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["artifact_type"] == "candidate_generation_report"
    assert report["pipeline_continued"] is False
    assert report["final_valid_count"] == 0
    assert any("has no sections" in "; ".join(item["reasons"]) for item in report["rejected_reasons"])

    registry_payload = json.loads(Path(workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_ids = {item["artifact_id"] for item in registry_payload["artifacts"]}
    assert "candidate_generation_report" in artifact_ids
    assert "outline_candidates" not in artifact_ids

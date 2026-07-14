import json
from pathlib import Path
from typing import cast

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
        created_at="2026-04-14T00:00:00Z",
        project_name=workspace.project_name,
        job_id=workspace.job_id,
        state="non_resumable",
        reason="test bootstrap",
        summary_file=workspace.artifact_path(f"{workspace.project_name}_summaries.json"),
        progress_snapshot_file=None,
        checkpoint_file=workspace.checkpoint_path(f"{workspace.project_name}_checkpoint.json"),
        fingerprint_bundle={"request": "demo"},
    )


def _make_bound_generator(tmp_path: Path, project_name: str = "demo", job_id: str = "job-outline-runtime"):
    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), project_name, job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    config = ConfigDict(
        {
            "Paths": {"output_path": str(output_dir)},
            "Outline_API": {"api_key": "outline-key", "model": "outline-model", "api_base": "https://example.com/v1"},
            "Primary_Reader_API": {"api_key": "primary-key", "model": "primary-model", "api_base": "https://example.com/v1"},
            "Writer_API": {"api_key": "writer-key", "model": "writer-model", "api_base": "https://example.com/v1"},
            "Validation": {"stage1_enabled": "false", "stage2_enabled": "false"},
        }
    )
    compat_view = CompatConfigView.from_config(config)

    generator = main.LiteratureReviewGenerator(project_name=project_name, pdf_folder=None)
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = config
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=compat_view,
        fingerprint_bundle={"request": "demo"},
        resume_state_report=_resume_report(workspace),
    )
    generator.summary_file = workspace.artifact_path(f"{project_name}_summaries.json")
    Path(generator.summary_file).write_text(json.dumps([{"status": "success"}]), encoding="utf-8")
    return generator, workspace


def test_load_outline_artifact_uses_registered_markdown_even_if_reviewed_outline_exists(tmp_path: Path) -> None:
    generator, workspace = _make_bound_generator(tmp_path)

    outline_text = "# Demo Outline\n\n## 1. Verified runtime path"
    outline_path = Path(generator._write_outline_artifact(outline_text, producer="test"))

    reviewed_outline_path = Path(workspace.artifact_path("demo_reviewed_outline.json"))
    reviewed_outline_path.write_text(
        json.dumps({"artifact_type": "reviewed_outline_document", "review_status": "adopted"}),
        encoding="utf-8",
    )

    loaded_path, loaded_text = generator._load_outline_artifact() or ("", "")

    assert loaded_path == str(outline_path)
    assert loaded_text == outline_text


# ---------------------------------------------------------------------------
# V2 Runtime Resolver tests
# ---------------------------------------------------------------------------

def test_runtime_resolver_legacy_mode_returns_markdown(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver

    output_dir = tmp_path / "output"
    outline_path = tmp_path / "output" / "legacy_outline.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    outline_path.write_text("# Legacy Outline\n\n## 1. Section", encoding="utf-8")

    config = {"Outline": {"enable_outline_intelligence_v2": "false"}}
    resolver = OutlineRuntimeResolver(
        config=config,
        legacy_outline_path=str(outline_path),
    )

    result = resolver.resolve_for_review()
    assert result is not None
    assert result.mode == "legacy"
    assert "Legacy Outline" in result.markdown
    assert result.source_artifact_type == "literature_review_outline"


def test_runtime_resolver_v2_enabled_without_adopted_fails_closed(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver

    config = {"Outline": {"enable_outline_intelligence_v2": "true"}}
    resolver = OutlineRuntimeResolver(
        config=config,
        workspace_path=str(tmp_path),
        project_name="test",
        legacy_outline_path=str(tmp_path / "legacy.md"),
    )

    result = resolver.resolve_for_review()
    # V2 enabled but no adopted final outline — must fail closed
    assert result is None


def test_runtime_resolver_v2_rejects_unregistered_convention_file(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "test_adopted_final_outline.json").write_text("{}", encoding="utf-8")

    class EmptyRegistry:
        @staticmethod
        def get(_artifact_id: str):
            return None

    resolver = OutlineRuntimeResolver(
        config={"Outline": {"enable_outline_intelligence_v2": "true"}},
        artifact_registry=EmptyRegistry(),
        workspace_path=str(tmp_path),
        project_name="test",
    )

    assert resolver.resolve_for_review() is None


def test_runtime_resolver_v2_with_valid_adopted_outline(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver
    from outline.v2_models import AdoptedFinalOutline, FinalOutline, compute_content_hash
    import json

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Create a valid adopted final outline
    final = FinalOutline(
        created_from_job_id="job-001",
        outline_id="outline-001",
        review_status="arbitrated",
        adoption_status="pending_user_adoption",
    )
    adopted = AdoptedFinalOutline(
        created_from_job_id="job-001",
        source_final_outline_hash=compute_content_hash(final.to_dict()),
        adopted_at="2026-05-16T00:00:00Z",
        adopted_by="test-user",
        outline=final,
    )

    adopted_path = artifacts_dir / "test_adopted_final_outline.json"
    adopted_path.write_text(json.dumps(adopted.to_dict()), encoding="utf-8")
    from outline.stage_health import OutlineStageHealthV1, make_test_double_entry
    health = OutlineStageHealthV1(
        job_id="job-001",
        execution_mode="test_dev",
        stages=(make_test_double_entry("outline_candidates", "test", {}, {}),),
        source_final_outline_hash=adopted.source_final_outline_hash,
        source_coverage_audit_hash=adopted.source_coverage_audit_hash,
    )
    (artifacts_dir / "test_outline_stage_health_v1.json").write_text(
        json.dumps(health.to_dict()), encoding="utf-8"
    )

    config = {"Outline": {"enable_outline_intelligence_v2": "true"}}
    resolver = OutlineRuntimeResolver(
        config=config,
        workspace_path=str(tmp_path),
        project_name="test",
    )

    result = resolver.resolve_for_review()
    assert result is not None
    assert result.mode == "v2"
    assert result.source_artifact_type == "adopted_final_outline"


def test_runtime_resolver_legacy_not_affected_by_v2_artifacts(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver

    legacy = tmp_path / "legacy.md"
    legacy.write_text("# Legacy", encoding="utf-8")

    # Even if v2 artifacts exist alongside, legacy mode should use legacy markdown
    config = {"Outline": {"enable_outline_intelligence_v2": "false"}}
    resolver = OutlineRuntimeResolver(
        config=config,
        legacy_outline_path=str(legacy),
    )

    result = resolver.resolve_for_review()
    assert result is not None
    assert result.mode == "legacy"


def test_runtime_resolver_v2_disabled_does_not_consume_v2_artifacts(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver
    from outline.v2_models import AdoptedFinalOutline, FinalOutline, compute_content_hash
    import json

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    legacy = tmp_path / "legacy.md"
    legacy.write_text("# Legacy Markdown", encoding="utf-8")

    # Create v2 artifact
    final = FinalOutline(
        created_from_job_id="job-001",
        outline_id="outline-001",
        review_status="arbitrated",
    )
    adopted = AdoptedFinalOutline(
        created_from_job_id="job-001",
        source_final_outline_hash=compute_content_hash(final.to_dict()),
        adopted_at="2026-05-16T00:00:00Z",
        adopted_by="test-user",
        outline=final,
    )
    adopted_path = artifacts_dir / "test_adopted_final_outline.json"
    adopted_path.write_text(json.dumps(adopted.to_dict()), encoding="utf-8")

    # V2 disabled — should return legacy, NOT v2
    config = {"Outline": {"enable_outline_intelligence_v2": "false"}}
    resolver = OutlineRuntimeResolver(
        config=config,
        workspace_path=str(tmp_path),
        project_name="test",
        legacy_outline_path=str(legacy),
    )

    result = resolver.resolve_for_review()
    assert result is not None
    assert result.mode == "legacy"
    # Should use legacy markdown, not v2 adopted outline
    assert "Legacy Markdown" in result.markdown



def test_runtime_resolver_v2_rejects_stale_adopted_outline_hash(tmp_path: Path) -> None:
    from outline.runtime_resolver import OutlineRuntimeResolver
    from outline.v2_models import AdoptedFinalOutline, FinalOutline

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    final = FinalOutline(
        created_from_job_id="job-001",
        outline_id="outline-001",
        review_status="arbitrated",
    )
    adopted = AdoptedFinalOutline(
        created_from_job_id="job-001",
        source_final_outline_hash="stale-hash",
        adopted_at="2026-05-16T00:00:00Z",
        adopted_by="test-user",
        outline=final,
    )
    (artifacts_dir / "test_adopted_final_outline.json").write_text(
        json.dumps(adopted.to_dict()),
        encoding="utf-8",
    )

    resolver = OutlineRuntimeResolver(
        config={"Outline": {"enable_outline_intelligence_v2": "true"}},
        workspace_path=str(tmp_path),
        project_name="test",
    )

    assert resolver.resolve_for_review() is None


def test_load_outline_artifact_v2_enabled_refuses_legacy_fallback(tmp_path: Path) -> None:
    generator, workspace = _make_bound_generator(tmp_path, job_id="job-v2-fail-closed")
    config = generator.config
    assert config is not None
    config.setdefault("Outline", {})["enable_outline_intelligence_v2"] = "true"
    generator.compat_config = CompatConfigView.from_config(config)

    legacy = Path(generator._get_legacy_outline_file_path())
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Legacy\n\n## 1. Should Not Load", encoding="utf-8")

    assert generator._load_outline_artifact() is None

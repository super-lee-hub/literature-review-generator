"""Tests for explicit v2 adoption gate."""

import json
import os
from pathlib import Path
from typing import cast

from outline.literature_map import build_literature_map
from outline.synthesis_flow import build_synthesis_flow
from outline.candidates import generate_candidates_deterministic
from outline.critique_v2 import (
    build_critiques_v2,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.arbitration_v2 import arbitrate_deterministic, build_final_outline
from outline.coverage_audit import run_coverage_audit
from outline.adoption import (
    adopt_final_outline,
    verify_adoption_prerequisites,
    write_adopted_outline,
)
from outline.v2_models import AdoptedFinalOutline, CoverageAudit, FinalOutline, FinalSection, compute_content_hash
from outline.stage_health import OutlineStageHealthV1, make_test_double_entry


def _sample_summaries():
    return [
        {
            "paper_info": {
                "title": f"Paper {i}",
                "authors": [str(i)],
                "year": 2020 + i,
                "classification": "support",
            },
            "themes": ["promotion fairness" if i < 3 else "consumer trust"],
            "methods": ["survey"],
            "limitations": ["single context"],
        }
        for i in range(6)
    ]


def _make_final_and_audit():
    lit_map = build_literature_map(_sample_summaries(), "job-001")
    flow = build_synthesis_flow(lit_map, "job-001")
    candidates = generate_candidates_deterministic(lit_map, flow, candidate_count=3)
    structure_run = run_structure_critique_deterministic(candidates)
    coverage_run = run_coverage_critique_deterministic(candidates)
    critiques = build_critiques_v2(
        structure_run, coverage_run,
        [c.candidate_id for c in candidates.candidates],
    )
    report = arbitrate_deterministic(candidates, critiques)
    lit_hash = compute_content_hash(lit_map.to_dict())
    flow_hash = compute_content_hash(flow.to_dict())
    final = build_final_outline(candidates, report, lit_hash, flow_hash, "job-001")
    audit = run_coverage_audit(final, lit_map, flow)
    return final, audit


def _force_passing_audit(final):
    return CoverageAudit(
        passed=True,
        source_final_outline_hash=compute_content_hash(final.to_dict()),
    )


def _make_health(final, audit, job_id="job-001"):
    return OutlineStageHealthV1(
        job_id=job_id,
        execution_mode="test_dev",
        stages=(make_test_double_entry("outline_candidates", "test", {}, {}),),
        source_final_outline_hash=compute_content_hash(final.to_dict()),
        source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
    )


class TestAdoptionGate:

    def test_adoption_succeeds_with_passing_audit(self):
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)
        adopted, msg = adopt_final_outline(final, audit, "job-001", "test-user", _make_health(final, audit))
        assert adopted is not None
        assert "success" in msg.lower()
        assert isinstance(adopted, AdoptedFinalOutline)
        assert adopted.adopted_by == "test-user"
        assert adopted.outline.adoption_status == "adopted"
        assert "<!-- Adoption: adopted -->" in adopted.to_markdown()

    def test_failed_audit_blocks_adoption(self):
        final, audit = _make_final_and_audit()
        failed = CoverageAudit(
            passed=False,
            source_final_outline_hash=compute_content_hash(final.to_dict()),
        )
        adopted, msg = adopt_final_outline(final, failed, "job-001", "test-user", _make_health(final, failed))
        assert adopted is None
        assert "pass" in msg.lower() or "blocked" in msg.lower()

    def test_stale_audit_blocks_adoption(self):
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)

        # Modify the final outline to make audit stale
        from outline.v2_models import FinalOutline as FO
        modified_final = FO(
            created_from_job_id="job-001",
            outline_id="different-id",  # Changed
            sections=final.sections,
            source_literature_map_id=final.source_literature_map_id,
            source_synthesis_flow_id=final.source_synthesis_flow_id,
            source_arbitration_report_id=final.source_arbitration_report_id,
            source_literature_map_hash=final.source_literature_map_hash,
            source_synthesis_flow_hash=final.source_synthesis_flow_hash,
        )

        ok, err = verify_adoption_prerequisites(modified_final, audit, _make_health(final, audit))
        assert audit.passed is True
        assert not ok
        assert "stale" in err.lower() or "hash" in err.lower()

    def test_verify_prerequisites_checks_audit_pass(self):
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)
        ok, err = verify_adoption_prerequisites(final, audit, _make_health(final, audit))
        assert ok

    def test_adopted_outline_has_required_fields(self):
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)
        adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user", _make_health(final, audit))
        assert adopted is not None
        assert adopted.artifact_type == "adopted_final_outline"
        assert adopted.artifact_version == "v1"
        assert adopted.source_final_outline_hash != ""
        assert adopted.source_coverage_audit_hash != ""
        assert adopted.adopted_at != ""
        assert adopted.adopted_by == "test-user"

    def test_write_adopted_outline_to_disk(self, tmp_path: Path):
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)
        adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user", _make_health(final, audit))
        assert adopted is not None
        path = str(tmp_path / "adopted_final_outline.json")
        written = write_adopted_outline(adopted, path)
        assert os.path.exists(written)

        import json
        with open(written, "r") as f:
            data = json.load(f)
        assert data["artifact_type"] == "adopted_final_outline"
        assert data["adopted_by"] == "test-user"

    def test_adoption_does_not_overwrite_reviewed_outline(self):
        """V2 adoption writes adopted_final_outline, NOT reviewed_outline."""
        final, audit = _make_final_and_audit()
        audit = _force_passing_audit(final)
        adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user", _make_health(final, audit))
        assert adopted is not None
        # adopted_final_outline artifact type is NOT reviewed_outline_document
        assert adopted.artifact_type == "adopted_final_outline"

    def test_blocked_final_outline_blocks_adoption_even_with_passed_audit_hash(self):
        final, _audit = _make_final_and_audit()
        blocked = FinalOutline(
            created_from_job_id=final.created_from_job_id,
            outline_id=final.outline_id,
            review_status="blocked",
            sections=final.sections,
            blocking_critique_ids=["crit-block"],
        )
        audit = CoverageAudit(
            passed=True,
            source_final_outline_hash=compute_content_hash(blocked.to_dict()),
        )

        adopted, msg = adopt_final_outline(blocked, audit, "job-001", "test-user", _make_health(blocked, audit))

        assert adopted is None
        assert "blocked" in msg.lower()



def test_generator_adopt_outline_v2_writes_registered_adopted_artifact(tmp_path: Path):
    import main
    from config_loader import ConfigDict
    from services.artifact_registry import ArtifactRegistry
    from services.config_compat import CompatConfigView
    from services.job_workspace import JobWorkspace
    from services.progress_state import ResumeStateReport

    class DummyLogger:
        def info(self, *_args, **_kwargs): pass
        def warning(self, *_args, **_kwargs): pass
        def error(self, *_args, **_kwargs): pass
        def success(self, *_args, **_kwargs): pass
        def debug(self, *_args, **_kwargs): pass

    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo", job_id="job-adopt-v2")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    cfg = ConfigDict({"Paths": {"output_path": str(tmp_path / "output")}, "Outline": {"enable_outline_intelligence_v2": "true"}})
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, DummyLogger())
    generator.config = cfg
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=CompatConfigView.from_config(cfg),
        fingerprint_bundle={},
        resume_state_report=ResumeStateReport(
            artifact_type="resume_state_report",
            artifact_version="v1",
            created_from_job_id=workspace.job_id,
            created_at="2026-05-16T00:00:00Z",
            project_name="demo",
            job_id=workspace.job_id,
            state="non_resumable",
            reason="test",
            summary_file=workspace.artifact_path("demo_summaries.json"),
            progress_snapshot_file=None,
            checkpoint_file=workspace.checkpoint_path("demo_checkpoint.json"),
            fingerprint_bundle={},
        ),
    )

    final, audit = _make_final_and_audit()
    audit = _force_passing_audit(final)
    final_path = Path(workspace.artifact_path("demo_final_outline.json"))
    final_path.write_text(json.dumps(final.to_dict()), encoding="utf-8")
    audit_path = Path(workspace.artifact_path("demo_outline_coverage_audit.json"))
    audit_path.write_text(json.dumps(audit.to_dict()), encoding="utf-8")
    registry.register_file(
        artifact_role="final_outline",
        artifact_type="final_outline",
        artifact_version="v1",
        path=final_path,
        producer="test",
        artifact_id="final_outline",
    )
    registry.register_file(
        artifact_role="outline_coverage_audit",
        artifact_type="outline_coverage_audit",
        artifact_version="v1",
        path=audit_path,
        producer="test",
        artifact_id="outline_coverage_audit",
    )
    health = _make_health(final, audit, job_id=final.created_from_job_id)
    health_path = Path(workspace.artifact_path("demo_outline_stage_health_v1.json"))
    health_path.write_text(json.dumps(health.to_dict()), encoding="utf-8")
    registry.register_file(
        artifact_role="outline_stage_health",
        artifact_type="outline_stage_health",
        artifact_version="v1",
        path=health_path,
        producer="test",
        artifact_id="outline_stage_health",
    )

    assert generator.adopt_outline_v2() is True
    adopted_path = Path(workspace.artifact_path("demo_adopted_final_outline.json"))
    assert adopted_path.exists()
    record = registry.get("adopted_final_outline")
    assert record is not None
    assert record.artifact_type == "adopted_final_outline"
    assert all(dep.content_hash for dep in record.depends_on)
    assert any(item.artifact_type == "audit_record" for item in registry.list_records())


def test_generator_adopt_outline_v2_failed_audit_does_not_write_adopted_artifact(tmp_path: Path):
    import main
    from config_loader import ConfigDict
    from services.artifact_registry import ArtifactRegistry
    from services.config_compat import CompatConfigView
    from services.job_workspace import JobWorkspace
    from services.progress_state import ResumeStateReport

    class DummyLogger:
        def info(self, *_args, **_kwargs): pass
        def warning(self, *_args, **_kwargs): pass
        def error(self, *_args, **_kwargs): pass
        def success(self, *_args, **_kwargs): pass
        def debug(self, *_args, **_kwargs): pass

    workspace = JobWorkspace.create(str(tmp_path / "output"), "demo", job_id="job-adopt-v2-fail")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    cfg = ConfigDict({"Paths": {"output_path": str(tmp_path / "output")}, "Outline": {"enable_outline_intelligence_v2": "true"}})
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, DummyLogger())
    generator.config = cfg
    generator.bind_job_workspace(
        workspace=workspace,
        artifact_registry=registry,
        compat_config=CompatConfigView.from_config(cfg),
        fingerprint_bundle={},
        resume_state_report=ResumeStateReport(
            artifact_type="resume_state_report",
            artifact_version="v1",
            created_from_job_id=workspace.job_id,
            created_at="2026-05-16T00:00:00Z",
            project_name="demo",
            job_id=workspace.job_id,
            state="non_resumable",
            reason="test",
            summary_file=workspace.artifact_path("demo_summaries.json"),
            progress_snapshot_file=None,
            checkpoint_file=workspace.checkpoint_path("demo_checkpoint.json"),
            fingerprint_bundle={},
        ),
    )

    final = FinalOutline(
        created_from_job_id="job-adopt-v2-fail",
        sections=[FinalSection(section_id="s1", title="Research problem framing")],
    )
    audit = CoverageAudit(
        passed=False,
        source_final_outline_hash=compute_content_hash(final.to_dict()),
    )
    Path(workspace.artifact_path("demo_final_outline.json")).write_text(json.dumps(final.to_dict()), encoding="utf-8")
    Path(workspace.artifact_path("demo_outline_coverage_audit.json")).write_text(json.dumps(audit.to_dict()), encoding="utf-8")

    assert generator.adopt_outline_v2() is False
    assert not Path(workspace.artifact_path("demo_adopted_final_outline.json")).exists()


def test_setup_output_directory_restores_registry_for_latest_workspace(tmp_path: Path):
    import main
    from config_loader import ConfigDict
    from services.artifact_registry import ArtifactRegistry
    from services.config_compat import CompatConfigView
    from services.job_workspace import JobWorkspace

    class DummyLogger:
        def info(self, *_args, **_kwargs): pass
        def warning(self, *_args, **_kwargs): pass
        def error(self, *_args, **_kwargs): pass
        def success(self, *_args, **_kwargs): pass
        def debug(self, *_args, **_kwargs): pass

    output_dir = tmp_path / "output"
    workspace = JobWorkspace.create(str(output_dir), "demo", job_id="job-existing")
    workspace.write_latest_pointer(
        resume_state="resumable",
        fingerprint_bundle={},
        status="ready",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    marker_path = Path(workspace.artifact_path("marker.txt"))
    marker_path.write_text("marker", encoding="utf-8")
    registry.register_file(
        artifact_role="marker",
        artifact_type="marker",
        artifact_version="v1",
        path=marker_path,
        producer="test",
        artifact_id="marker",
    )

    cfg = ConfigDict({
        "Paths": {"output_path": str(output_dir)},
        "Outline": {"enable_outline_intelligence_v2": "true"},
    })
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=None)
    generator.logger = cast(main.CustomLogger, DummyLogger())
    generator.config = cfg
    generator.compat_config = CompatConfigView.from_config(cfg)

    assert generator.setup_output_directory() is True
    assert generator.job_workspace is not None
    assert generator.job_workspace.root_dir == workspace.root_dir
    assert generator.artifact_registry is not None
    assert generator.artifact_registry.get("marker") is not None

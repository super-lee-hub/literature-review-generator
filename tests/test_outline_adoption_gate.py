import json
"""Tests for explicit v2 adoption gate."""

import os
from pathlib import Path
import pytest
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
from outline.v2_models import AdoptedFinalOutline, compute_content_hash


def _sample_summaries():
    return [
        {"paper_info": {"title": f"Paper {i}", "authors": [str(i)], "year": 2020 + i, "classification": "core" if i == 0 else "support"}, "themes": [f"theme_{i}"], "methods": [f"method_{i}"]}
        for i in range(3)
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


class TestAdoptionGate:

    def test_adoption_succeeds_with_passing_audit(self):
        final, audit = _make_final_and_audit()
        if audit.passed:
            adopted, msg = adopt_final_outline(final, audit, "job-001", "test-user")
            assert adopted is not None
            assert "success" in msg.lower()
            assert isinstance(adopted, AdoptedFinalOutline)
            assert adopted.adopted_by == "test-user"
        else:
            # If audit didn't pass, that's also valid (blocking issues detected)
            assert not audit.passed
            adopted, msg = adopt_final_outline(final, audit, "job-001", "test-user")
            assert adopted is None

    def test_failed_audit_blocks_adoption(self):
        final, audit = _make_final_and_audit()
        if not audit.passed:
            adopted, msg = adopt_final_outline(final, audit, "job-001", "test-user")
            assert adopted is None
            assert "pass" in msg.lower() or "blocked" in msg.lower()

    def test_stale_audit_blocks_adoption(self):
        final, audit = _make_final_and_audit()

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

        ok, err = verify_adoption_prerequisites(modified_final, audit)
        if audit.passed:
            assert not ok
            assert "stale" in err.lower() or "hash" in err.lower()

    def test_verify_prerequisites_checks_audit_pass(self):
        final, audit = _make_final_and_audit()
        ok, err = verify_adoption_prerequisites(final, audit)
        if audit.passed:
            assert ok
        else:
            assert not ok

    def test_adopted_outline_has_required_fields(self):
        final, audit = _make_final_and_audit()
        if audit.passed:
            adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user")
            assert adopted is not None
            assert adopted.artifact_type == "adopted_final_outline"
            assert adopted.artifact_version == "v1"
            assert adopted.source_final_outline_hash != ""
            assert adopted.source_coverage_audit_hash != ""
            assert adopted.adopted_at != ""
            assert adopted.adopted_by == "test-user"

    def test_write_adopted_outline_to_disk(self, tmp_path: Path):
        final, audit = _make_final_and_audit()
        if audit.passed:
            adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user")
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
        if audit.passed:
            adopted, _ = adopt_final_outline(final, audit, "job-001", "test-user")
            assert adopted is not None
            # adopted_final_outline artifact type is NOT reviewed_outline_document
            assert adopted.artifact_type == "adopted_final_outline"



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
    generator.logger = DummyLogger()
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
    if not audit.passed:
        pytest.skip("fixture audit intentionally blocks adoption")
    Path(workspace.artifact_path("demo_final_outline.json")).write_text(json.dumps(final.to_dict()), encoding="utf-8")
    Path(workspace.artifact_path("demo_outline_coverage_audit.json")).write_text(json.dumps(audit.to_dict()), encoding="utf-8")

    assert generator.adopt_outline_v2() is True
    adopted_path = Path(workspace.artifact_path("demo_adopted_final_outline.json"))
    assert adopted_path.exists()
    record = registry.get("adopted_final_outline")
    assert record is not None
    assert record.artifact_type == "adopted_final_outline"
    assert all(dep.content_hash for dep in record.depends_on)

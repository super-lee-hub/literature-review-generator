from outline.adoption import verify_adoption_prerequisites
from outline.stage_health import (
    OutlineStageHealthV1,
    StageHealthEntryV1,
    make_test_double_entry,
)
from outline.v2_models import CoverageAudit, FinalOutline, compute_content_hash
from outline.pipeline import V2Pipeline
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


def _final_audit():
    final = FinalOutline(created_from_job_id="job-health", outline_id="outline-health")
    audit = CoverageAudit(
        passed=True,
        source_final_outline_hash=compute_content_hash(final.to_dict()),
    )
    return final, audit


def _health(final, audit, entry):
    return OutlineStageHealthV1(
        job_id="job-health",
        execution_mode="test_dev",
        stages=(entry,),
        source_final_outline_hash=compute_content_hash(final.to_dict()),
        source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
    )


def test_stage_health_round_trip_and_test_double_is_adoptable():
    final, audit = _final_audit()
    health = _health(final, audit, make_test_double_entry("outline_candidates", "test", {}, {}))

    restored = OutlineStageHealthV1.from_dict(health.to_dict())

    assert restored.adoptable is True
    assert verify_adoption_prerequisites(final, audit, restored) == (True, "")


def test_missing_stale_and_production_fallback_health_fail_closed():
    final, audit = _final_audit()
    assert verify_adoption_prerequisites(final, audit, None)[0] is False

    stale = _health(final, audit, make_test_double_entry("outline_candidates", "test", {}, {}))
    changed = FinalOutline(created_from_job_id="job-health", outline_id="changed")
    assert verify_adoption_prerequisites(changed, audit, stale)[0] is False

    fallback = StageHealthEntryV1(
        stage_name="outline_arbitration",
        provider_route="Outline_API",
        execution_status="succeeded",
        schema_valid=True,
        attempts=1,
        input_hashes=("in",),
        output_hashes=("out",),
        fallback_provenance="deterministic_fallback",
        degraded_reason="provider output was invalid",
    )
    degraded = OutlineStageHealthV1(
        job_id="job-health",
        execution_mode="production",
        stages=(fallback,),
        source_final_outline_hash=compute_content_hash(final.to_dict()),
        source_coverage_audit_hash=compute_content_hash(audit.to_dict()),
    )
    ok, reason = verify_adoption_prerequisites(final, audit, degraded)
    assert ok is False
    assert "degraded" in reason.lower()


def test_pipeline_persists_independent_health_sidecar_without_versioning_old_artifacts(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "health", job_id="job-health-pipeline")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    summaries = [
        {
            "paper_info": {
                "title": f"Paper {index}",
                "authors": [f"Author {index}"],
                "year": 2020 + index,
            },
            "themes": ["stream"],
            "findings": "finding",
        }
        for index in range(6)
    ]
    pipeline = V2Pipeline(
        job_id=workspace.job_id,
        summaries=summaries,
        artifact_registry=registry,
        workspace=workspace,
        project_name="health",
    )
    result = pipeline.run(candidate_count=1, test_dev_mode=True)
    paths = pipeline.persist_artifacts(result)

    health_record = registry.get("outline_stage_health")
    final_record = registry.get("final_outline")
    assert health_record is not None
    assert final_record is not None and final_record.artifact_version == "v2"
    assert paths["outline_stage_health"].endswith("outline_stage_health_v1.json")
    dependency_types = {item.artifact_type for item in health_record.depends_on}
    assert {"final_outline", "outline_coverage_audit", "outline_arbitration_report"} <= dependency_types

"""Read-only Outline terminal selection for SourceRevision parent authority."""

import json
from pathlib import Path

import pytest

import runtime.source_revision_admission as admission
from runtime.stage_terminal import StageTerminalStore, TerminalStageRecordV1
from outline.v3_models import compute_v3_hash
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace


def _artifact(registry, workspace, artifact_id, artifact_type, value, depends_on=(), metadata=None):
    path = Path(workspace.artifact_path(artifact_id.replace(":", "-") + ".json"))
    if artifact_type == "outline_request_payload_audit":
        version = "v1"
        path.write_text("", encoding="utf-8")
    else:
        version = "v3"
        defaults = {
            "final_outline": {"title": "Fixture", "sections": [], "paper_keys": [], "source_hashes": []},
            "coverage_audit": {"passed": True, "quality_gate": {}, "quality_gate_hash": compute_v3_hash({}),
                               "paper_coverage": {}, "claim_coverage": {}, "section_coverage": {},
                               "research_streams": [], "quality_checks": []},
            "stability_audit": {"status": "off", "variant_definitions": [], "variant_input_hashes": {},
                                "variant_output_hashes": {}, "comparisons": [], "checks": [], "failed_checks": []},
            "outline_stage_health": {"status": "healthy", "adoption_eligible": True, "quality_gate": {},
                                     "quality_gate_hash": compute_v3_hash({}), "diagnostics": []},
        }
        payload = {**defaults[artifact_type], **value}
        canonical = {
            "artifact_type": artifact_type, "artifact_version": version, "job_id": workspace.job_id,
            "dependency_hashes": {row.artifact_id: row.content_hash for row in depends_on},
            "payload": payload, "blocking_diagnostics": [],
        }
        path.write_text(json.dumps({**canonical, "status": "ready", "content_hash": compute_v3_hash(canonical)}), encoding="utf-8")
    return registry.register_file(
        artifact_id=artifact_id, artifact_role=artifact_type, artifact_type=artifact_type,
        artifact_version=version, path=path, producer="tests.source_revision_terminal_authority",
        depends_on=depends_on, metadata=metadata or {},
    )


def _case(tmp_path):
    workspace = JobWorkspace.create(str(tmp_path), "parent", "parent-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    audit = _artifact(registry, workspace, "audit", "outline_request_payload_audit", {"records": []})
    common = [
        _artifact(registry, workspace, name, kind, {"status": "fixture-output"})
        for name, kind in (("final", "final_outline"), ("coverage", "coverage_audit"), ("stability", "stability_audit"))
    ]
    store = StageTerminalStore(workspace, registry)

    def terminal(health, name, finished):
        record = TerminalStageRecordV1.create(
            job_id=workspace.job_id, attempt_id=name, stage_name="outline", status="succeeded",
            producer="tests.source_revision_terminal_authority", record_id=name,
            finished_at=finished,
            output_artifact_refs=[ArtifactDependencyRefV2.from_record(row) for row in [*common, health]],
        )
        return store.persist(record)

    health = _artifact(registry, workspace, "outline-v3:stage_health", "outline_stage_health", {"revision": "old"})
    old = terminal(health, "old-outline-terminal", "2026-10-01T00:00:00Z")
    health = _artifact(
        registry, workspace, "outline-v3:stage_health", "outline_stage_health", {"revision": "current"},
        depends_on=[ArtifactDependencyRefV2.from_record(audit)],
    )
    current = terminal(health, "current-outline-terminal", "2026-10-28T00:00:00Z")
    return workspace, registry, audit, old, current


def test_parent_root_selects_exact_current_outline_terminal_without_mutation(tmp_path):
    workspace, registry, audit, _old, current = _case(tmp_path)
    records = registry._read_registry_unlocked()[1]
    before = {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()}
    selected = admission._outline_terminal_authority(registry, records)
    assert selected == current
    registry.verify_ready_artifact_closure(selected)
    assert audit.artifact_id in {
        ref.artifact_id for ref in records["outline-v3:stage_health"].depends_on
    }
    assert {str(path): path.read_bytes() for path in Path(workspace.root_dir).rglob("*") if path.is_file()} == before


def test_unrelated_corrupt_review_terminal_is_not_read(tmp_path):
    workspace, registry, _audit, _old, current = _case(tmp_path)
    unrelated = StageTerminalStore(workspace, registry).persist(TerminalStageRecordV1.create(
        job_id=workspace.job_id, attempt_id="unrelated", stage_name="review", status="blocked",
        producer="tests.source_revision_terminal_authority", output_artifact_refs=[],
    ))
    Path(unrelated.path).write_text("not JSON", encoding="utf-8")
    assert admission._outline_terminal_authority(registry, registry._read_registry_unlocked()[1]) == current


def test_stale_terminal_cannot_substitute_for_missing_current_outline_root(tmp_path):
    _workspace, registry, _audit, _old, current = _case(tmp_path)
    records = registry._read_registry_unlocked()[1]
    del records[current.artifact_id]
    with pytest.raises(admission.SourceRevisionError, match="completed Outline terminal"):
        admission._outline_terminal_authority(registry, records)


def test_current_terminal_bytes_are_verified_before_authority_is_selected(tmp_path):
    _workspace, registry, _audit, _old, current = _case(tmp_path)
    payload = json.loads(Path(current.path).read_bytes())
    payload["producer"] = "tampered"
    Path(current.path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(admission.SourceRevisionError, match="Outline terminal"):
        admission._outline_terminal_authority(registry, registry._read_registry_unlocked()[1])

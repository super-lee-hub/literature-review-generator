from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import pph_validation_closure as closure
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
)
from services.citation_ref_catalog import build_document_ref_catalog
from services.job_workspace import JobWorkspace
from validation.run_result import (
    ClaimValidationResultV1,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return closure._file_sha256(path)


def _dependency(record: ArtifactRecord) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
    )


def _catalog() -> dict[str, Any]:
    return build_document_ref_catalog(
        [
            {
                "paper_info": {
                    "canonical_paper_key": "paper-a",
                    "title": "Paper A",
                    "authors": ["Alice Author"],
                    "year": "2024",
                    "doi": "10.1000/a",
                },
                "summary_hash": "a" * 64,
            },
            {
                "paper_info": {
                    "canonical_paper_key": "paper-b",
                    "title": "Paper B",
                    "authors": ["Bob Author"],
                    "year": "2025",
                    "doi": "10.1000/b",
                },
                "summary_hash": "b" * 64,
            },
        ],
        project_name="demo_project",
        job_id="job-001",
    )


@pytest.fixture()
def staged_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    project_id = "S01"
    workspace = tmp_path / "workspace"
    outline = tmp_path / "outline.md"
    summary = tmp_path / "summaries.json"
    catalog_path = tmp_path / "catalog.json"
    outline.write_text("# Outline\n\n## 1. First\n\n## 2. Second\n", encoding="utf-8")
    _write_json(summary, {"summaries": []})
    catalog = _catalog()
    _write_json(catalog_path, catalog)

    monkeypatch.setattr(closure, "workspace_path", lambda _project_id: workspace)
    monkeypatch.setattr(closure, "summary_path", lambda _project_id: summary)
    monkeypatch.setattr(closure, "citation_catalog_path", lambda _project_id: catalog_path)
    monkeypatch.setattr(closure, "_canonical_outline_path", lambda _project_id: outline)
    monkeypatch.setattr(closure, "_outline_sections", lambda _project_id: [1, 2])
    monkeypatch.setattr(closure, "_outline_section_titles", lambda _project_id: {1: "First", 2: "Second"})

    return {
        "project_id": project_id,
        "outline": outline,
        "summary": summary,
        "catalog_path": catalog_path,
        "catalog": catalog,
        "tmp_path": tmp_path,
    }


def _staged_payload(env: dict[str, Any], sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "project_id": env["project_id"],
        "outline_file": str(env["outline"]),
        "outline_file_sha256": _sha(env["outline"]),
        "summary_file": str(env["summary"]),
        "summary_file_sha256": _sha(env["summary"]),
        "citation_ref_catalog_path": str(env["catalog_path"]),
        "citation_ref_catalog_hash": env["catalog"]["catalog_hash"],
        "sections": sections,
    }


def test_validate_staged_review_import_merges_successfully(staged_env: dict[str, Any]) -> None:
    first = _write_json(
        staged_env["tmp_path"] / "first.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "First section cites one paper. [[cite_ref:R001]]",
                }
            ],
        ),
    )
    second = _write_json(
        staged_env["tmp_path"] / "second.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 2,
                    "section_title": "Second",
                    "content": "Second section cites both papers. [[cite_ref:R001,R002]]",
                }
            ],
        ),
    )

    result = closure.validate_staged_review_import(staged_env["project_id"], [first, second])

    assert result["section_count"] == 2
    assert [section["section_number"] for section in result["sections"]] == [1, 2]
    assert result["outline_file_sha256"] == _sha(staged_env["outline"])
    assert result["summary_file_sha256"] == _sha(staged_env["summary"])
    assert result["citation_ref_catalog_hash"] == staged_env["catalog"]["catalog_hash"]


def test_validate_staged_review_import_accepts_short_schema(staged_env: dict[str, Any]) -> None:
    path = _write_json(
        staged_env["tmp_path"] / "short_schema.json",
        {
            "project_id": staged_env["project_id"],
            "outline_hash": _sha(staged_env["outline"]),
            "summary_hash": _sha(staged_env["summary"]),
            "catalog_hash": staged_env["catalog"]["catalog_hash"],
            "section_range": [1, 2],
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "Content. [[cite_ref:R001]]",
                },
                {
                    "section_number": 2,
                    "section_title": "Second",
                    "content": "Content. [[cite_ref:R002]]",
                },
            ],
        },
    )

    result = closure.validate_staged_review_import(staged_env["project_id"], [path])

    assert result["outline_file"] == str(staged_env["outline"])
    assert result["summary_file"] == str(staged_env["summary"])
    assert result["citation_ref_catalog_path"] == str(staged_env["catalog_path"])
    assert result["section_count"] == 2


def test_validate_staged_review_import_rejects_hash_mismatch(staged_env: dict[str, Any]) -> None:
    staged = _staged_payload(
        staged_env,
        [
            {
                "section_number": 1,
                "section_title": "First",
                "content": "Content. [[cite_ref:R001]]",
            },
            {
                "section_number": 2,
                "section_title": "Second",
                "content": "Content. [[cite_ref:R002]]",
            },
        ],
    )
    staged["summary_file_sha256"] = "0" * 64
    path = _write_json(staged_env["tmp_path"] / "bad_hash.json", staged)

    with pytest.raises(ValueError, match="summary file sha256 mismatch"):
        closure.validate_staged_review_import(staged_env["project_id"], [path])


def test_validate_staged_review_import_rejects_duplicate_or_missing_sections(staged_env: dict[str, Any]) -> None:
    path = _write_json(
        staged_env["tmp_path"] / "duplicate.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "Content. [[cite_ref:R001]]",
                },
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "More content. [[cite_ref:R001]]",
                },
            ],
        ),
    )

    with pytest.raises(ValueError, match="duplicate staged review sections"):
        closure.validate_staged_review_import(staged_env["project_id"], [path])

    missing = _write_json(
        staged_env["tmp_path"] / "missing.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "Content. [[cite_ref:R001]]",
                }
            ],
        ),
    )
    with pytest.raises(ValueError, match="missing=\\[2\\]"):
        closure.validate_staged_review_import(staged_env["project_id"], [missing])


def test_validate_staged_review_import_rejects_title_mismatch(staged_env: dict[str, Any]) -> None:
    path = _write_json(
        staged_env["tmp_path"] / "bad_title.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "Wrong",
                    "content": "Content. [[cite_ref:R001]]",
                },
                {
                    "section_number": 2,
                    "section_title": "Second",
                    "content": "Content. [[cite_ref:R002]]",
                },
            ],
        ),
    )

    with pytest.raises(ValueError, match="section 1 title mismatch"):
        closure.validate_staged_review_import(staged_env["project_id"], [path])


def test_validate_staged_review_import_rejects_unknown_ref(staged_env: dict[str, Any]) -> None:
    path = _write_json(
        staged_env["tmp_path"] / "unknown_ref.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "Content. [[cite_ref:R999]]",
                },
                {
                    "section_number": 2,
                    "section_title": "Second",
                    "content": "Content. [[cite_ref:R002]]",
                },
            ],
        ),
    )

    with pytest.raises(ValueError, match="unknown citation ref_id: R999"):
        closure.validate_staged_review_import(staged_env["project_id"], [path])


def test_validate_staged_review_import_rejects_bare_ref_id(staged_env: dict[str, Any]) -> None:
    path = _write_json(
        staged_env["tmp_path"] / "bare_ref.json",
        _staged_payload(
            staged_env,
            [
                {
                    "section_number": 1,
                    "section_title": "First",
                    "content": "R001 reports the result. [[cite_ref:R001]]",
                },
                {
                    "section_number": 2,
                    "section_title": "Second",
                    "content": "Content. [[cite_ref:R002]]",
                },
            ],
        ),
    )

    with pytest.raises(ValueError, match="bare citation ref_id is not allowed: R001"):
        closure.validate_staged_review_import(staged_env["project_id"], [path])


def test_topic_section_contract_matches_bundle_closure() -> None:
    assert closure.PROJECTS["S02"]["expected_sections"] == 5
    assert closure.PROJECTS["S03"]["expected_sections"] == 5


def test_topic_section_contract_rejects_outline_drift() -> None:
    with pytest.raises(ValueError, match="section contract mismatch"):
        closure._require_topic_section_contract("S02", [1, 2, 3, 4, 5, 6])


def _validation_registry(
    tmp_path: Path,
    *,
    verdict: str,
) -> tuple[ArtifactRegistry, ValidationRunResultV1, dict[str, ArtifactRecord]]:
    workspace = JobWorkspace.create(
        str(tmp_path),
        "validation-gate",
        job_id="job-validation-gate",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    records: dict[str, ArtifactRecord] = {}
    for artifact_id, artifact_type, version in (
        ("review_draft_v2:full_review", "review_draft", "v2"),
        ("citation_manifest:v3", "citation_manifest", "v3"),
        ("evidence_manifest:paper-a", "evidence_manifest", "v1"),
    ):
        path = Path(workspace.artifact_path(f"{artifact_id.replace(':', '-')}.json"))
        _write_json(
            path,
            {"artifact_type": artifact_type, "artifact_version": version},
        )
        records[artifact_type] = registry.register_file(
            artifact_id=artifact_id,
            artifact_role=artifact_type,
            artifact_type=artifact_type,
            artifact_version=version,
            path=path,
            producer="tests",
        )

    claim = ClaimValidationResultV1.from_dict(
        {
            "claim_result_id": "claim-1",
            "claim_unit_ids": ["unit-1"],
            "citation_set_key": "citation-set-1",
            "paper_ids": ["paper-a"],
            "block_ids": ["block-1"],
            "claim_text": "Claim text",
            "claim_context": "Claim context",
            "verdict": verdict,
            "reasoning_summary": "Reasoning",
            "repair_hint": "",
            "root_causes": [],
            "alignment_status": "aligned",
            "alignment_confidence": 1.0,
            "low_confidence": False,
            "details": {},
            "evidence_candidates": [],
            "compatibility": {},
        }
    )
    result = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        attempt_id="attempt-validation-gate",
        execution_status="succeeded",
        report_id="validation-run:gate",
        claim_results=[claim],
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=records["review_draft"].artifact_id,
            review_draft_hash=records["review_draft"].content_hash,
            citation_manifest_id=records["citation_manifest"].artifact_id,
            citation_manifest_hash=records["citation_manifest"].content_hash,
            evidence_manifest_ids=(records["evidence_manifest"].artifact_id,),
            evidence_manifest_hashes=(records["evidence_manifest"].content_hash,),
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    result_path = Path(workspace.artifact_path("validation_run_result_v1.json"))
    _write_json(result_path, result.to_dict())
    records["validation_run_result"] = registry.register_file(
        artifact_id=result.validation_run_id,
        artifact_role="validation",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=result_path,
        producer="tests",
        depends_on=[
            _dependency(records["review_draft"]),
            _dependency(records["citation_manifest"]),
            _dependency(records["evidence_manifest"]),
        ],
    )
    return registry, result, records


def test_clean_validation_gate_accepts_only_typed_clean_current_result(
    tmp_path: Path,
) -> None:
    registry, result, _records = _validation_registry(tmp_path, verdict="supported")

    snapshot = closure._require_clean_validation_result(
        registry,
        validation_run_id=result.validation_run_id,
        attempt_id=result.attempt_id,
    )

    assert snapshot["contract_satisfied"] is True
    assert snapshot["validation_disposition"] == "clean"
    assert snapshot["dependencies_verified"] is True


def test_clean_validation_gate_rejects_findings_even_when_contract_is_satisfied(
    tmp_path: Path,
) -> None:
    registry, result, _records = _validation_registry(
        tmp_path,
        verdict="partial_support",
    )
    assert result.contract_satisfied is True
    assert result.validation_disposition.value == "findings"

    with pytest.raises(ValueError, match="validation_disposition must be clean"):
        closure._require_clean_validation_result(
            registry,
            validation_run_id=result.validation_run_id,
            attempt_id=result.attempt_id,
        )


def test_clean_validation_gate_rejects_stale_input_dependency(tmp_path: Path) -> None:
    registry, result, records = _validation_registry(tmp_path, verdict="supported")
    Path(records["evidence_manifest"].path).write_text(
        '{"tampered": true}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Validation input dependencies"):
        closure._require_clean_validation_result(
            registry,
            validation_run_id=result.validation_run_id,
            attempt_id=result.attempt_id,
        )


def test_clean_validation_gate_requires_serialized_contract_true(
    tmp_path: Path,
) -> None:
    registry, result, records = _validation_registry(tmp_path, verdict="supported")
    result_record = records["validation_run_result"]
    result_path = Path(result_record.path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["contract_satisfied"] = False
    _write_json(result_path, payload)
    registry.register_file(
        artifact_id=result_record.artifact_id,
        artifact_role=result_record.artifact_role,
        artifact_type=result_record.artifact_type,
        artifact_version=result_record.artifact_version,
        path=result_path,
        producer="tests",
        depends_on=[
            _dependency(records["review_draft"]),
            _dependency(records["citation_manifest"]),
            _dependency(records["evidence_manifest"]),
        ],
    )

    with pytest.raises(ValueError, match="serialized contract_satisfied must be true"):
        closure._require_clean_validation_result(
            registry,
            validation_run_id=result.validation_run_id,
            attempt_id=result.attempt_id,
        )


def test_clean_validation_gate_rejects_registry_payload_identity_mismatch(
    tmp_path: Path,
) -> None:
    registry, result, records = _validation_registry(tmp_path, verdict="supported")
    result_record = records["validation_run_result"]
    result_path = Path(result_record.path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["validation_run_id"] = "validation-run:different"
    _write_json(result_path, payload)
    registry.register_file(
        artifact_id=result_record.artifact_id,
        artifact_role=result_record.artifact_role,
        artifact_type=result_record.artifact_type,
        artifact_version=result_record.artifact_version,
        path=result_path,
        producer="tests",
        depends_on=[
            _dependency(records["review_draft"]),
            _dependency(records["citation_manifest"]),
            _dependency(records["evidence_manifest"]),
        ],
    )

    with pytest.raises(ValueError, match="validation_run_id does not match Registry"):
        closure._require_clean_validation_result(
            registry,
            validation_run_id=result.validation_run_id,
            attempt_id=result.attempt_id,
        )


def _manifest_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ArtifactRegistry, dict[str, ArtifactRecord]]:
    output_root = tmp_path / "output"
    monkeypatch.setattr(closure, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(
        closure,
        "PROJECTS",
        {
            "S01": {
                "project_name": "demo_project",
                "job_id": "job-001",
                "expected_sections": 2,
            }
        },
    )
    workspace = JobWorkspace.create(
        str(output_root),
        "demo_project",
        job_id="job-001",
    )
    summary = _write_json(closure.summary_path("S01"), [])
    catalog = _catalog()
    catalog_path = _write_json(closure.citation_catalog_path("S01"), catalog)
    draft_path = _write_json(
        closure.review_draft_path("S01"),
        {"artifact_type": "review_draft", "artifact_version": "v2"},
    )
    manifest_path = _write_json(
        closure.citation_manifest_path("S01"),
        {"artifact_type": "citation_manifest", "artifact_version": "v3"},
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    records: dict[str, ArtifactRecord] = {}
    records["summary"] = registry.register_file(
        artifact_id="summary_file:demo_project_summaries.json",
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary,
        producer="tests",
    )
    records["catalog"] = registry.register_file(
        artifact_id="citation_ref_catalog:v1",
        artifact_role="citation_catalog",
        artifact_type="citation_ref_catalog",
        artifact_version="v1",
        path=catalog_path,
        producer="tests",
        depends_on=[_dependency(records["summary"])],
    )
    records["draft"] = registry.register_file(
        artifact_id="review_draft_v2:full_review",
        artifact_role="review_draft_v2",
        artifact_type="review_draft",
        artifact_version="v2",
        path=draft_path,
        producer="tests",
        depends_on=[
            _dependency(records["summary"]),
            _dependency(records["catalog"]),
        ],
    )
    records["manifest"] = registry.register_file(
        artifact_id="citation_manifest:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests",
        depends_on=[
            _dependency(records["draft"]),
            _dependency(records["catalog"]),
        ],
    )
    return registry, records


def test_existing_manifest_is_reused_only_with_ready_current_registry_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry, _records = _manifest_registry(tmp_path, monkeypatch)

    result = closure.ensure_citation_manifest("S01")

    assert result["status"] == "reused"


def test_stale_manifest_dependency_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, records = _manifest_registry(tmp_path, monkeypatch)
    draft_path = Path(records["draft"].path)
    _write_json(
        draft_path,
        {
            "artifact_type": "review_draft",
            "artifact_version": "v2",
            "revision": 2,
        },
    )
    registry.register_file(
        artifact_id=records["draft"].artifact_id,
        artifact_role=records["draft"].artifact_role,
        artifact_type=records["draft"].artifact_type,
        artifact_version=records["draft"].artifact_version,
        path=draft_path,
        producer="tests",
        depends_on=[
            _dependency(records["summary"]),
            _dependency(records["catalog"]),
        ],
    )

    import services.citation_manifest as citation_manifest_service

    def _rebuild_requested(**_kwargs: Any) -> Any:
        raise RuntimeError("rebuild requested")

    monkeypatch.setattr(
        citation_manifest_service,
        "build_citation_manifest_v3_from_review_draft",
        _rebuild_requested,
    )

    with pytest.raises(RuntimeError, match="rebuild requested"):
        closure.ensure_citation_manifest("S01")

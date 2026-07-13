from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import validator
from services.repair_policy import ValidationRepairPolicy
from validation.run_result import ValidationRunResultV1


def _legacy_claim(*, key: str, status: str) -> SimpleNamespace:
    unit_id = f"unit-{key}"
    return SimpleNamespace(
        citation_set_key=key,
        citation_id=key,
        paper_ids=[f"paper-{key}"],
        block_ids=[f"block-{key}"],
        claim_text=f"claim {key}",
        claim_context=f"context {key}",
        evidence_status=status,
        conclusion=status.upper(),
        disposition="manual_review" if status in {"contradicted", "wrong_source"} else "kept",
        reasoning_summary=f"reason {key}",
        repair_hint="inspect source",
        root_causes=[],
        low_confidence=False,
        claim_units=[{"claim_unit_id": unit_id, "claim_text": f"claim {key}"}],
        details={
            "claim_units": [{"claim_unit_id": unit_id, "claim_text": f"claim {key}"}],
            "claim_unit_results": [
                {
                    "claim_unit_id": unit_id,
                    "citation_set_key": key,
                    "claim_text": f"claim {key}",
                    "checked_paper_ids": [f"paper-{key}"],
                    "evidence_status": status,
                }
            ],
        },
        evidence_candidates=[],
    )


def test_all_validation_projections_derive_from_canonical_json(tmp_path: Path) -> None:
    workspace = SimpleNamespace(
        job_id="job-validation-projections",
        project_name="projection-demo",
        paths=SimpleNamespace(reports_dir=str(tmp_path)),
    )
    generator = SimpleNamespace(job_workspace=workspace, project_name=workspace.project_name)
    legacy_report = SimpleNamespace(
        report_id="validation-projection-demo",
        created_at="2026-07-13T00:00:00Z",
        citation_results=[
            _legacy_claim(key="supported", status="supported"),
            _legacy_claim(key="gap", status="evidence_gap"),
            _legacy_claim(key="contradicted", status="contradicted"),
        ],
    )
    canonical = ValidationRunResultV1.from_report(
        legacy_report,
        job_id=workspace.job_id,
        repair_policy=ValidationRepairPolicy.REPORT_ONLY.value,
    )

    paths = validator._write_validation_reports(
        generator,
        canonical,
        [object()],  # compatibility input must not affect projections
        ValidationRepairPolicy.REPORT_ONLY,
    )

    persisted = ValidationRunResultV1.from_dict(
        json.loads(Path(paths["validation_run_result_file"]).read_text(encoding="utf-8"))
    )
    completion = json.loads(Path(paths["completion_report_file"]).read_text(encoding="utf-8"))
    manual = json.loads(Path(paths["manual_report_file"]).read_text(encoding="utf-8"))
    audit = json.loads(Path(paths["claim_alignment_audit_json"]).read_text(encoding="utf-8"))
    text = Path(paths["report_file"]).read_text(encoding="utf-8")

    assert persisted.to_dict() == canonical.to_dict()
    assert completion["claim_verdict_counts"] == canonical.claim_verdict_counts
    assert completion["canonical_result_hash"] == canonical.stable_hash()
    assert manual["total_items"] == 1
    assert manual["items"][0]["claim_verdict"] == "contradicted"
    assert audit["summary"]["contradicted_rows"] == canonical.contradicted_count == 1
    for verdict, count in canonical.claim_verdict_counts.items():
        assert f"{verdict}: {count}" in text


def test_projection_writer_ignores_legacy_manual_item_list(tmp_path: Path) -> None:
    workspace = SimpleNamespace(
        job_id="job-empty-validation",
        project_name="empty",
        paths=SimpleNamespace(reports_dir=str(tmp_path)),
    )
    generator = SimpleNamespace(job_workspace=workspace, project_name=workspace.project_name)
    canonical = ValidationRunResultV1.from_report(
        SimpleNamespace(report_id="empty", citation_results=[]),
        job_id=workspace.job_id,
    )

    paths = validator._write_validation_reports(
        generator,
        canonical,
        [SimpleNamespace(manual_review_reason="must not leak")],
        ValidationRepairPolicy.REPORT_ONLY,
    )

    manual = json.loads(Path(paths["manual_report_file"]).read_text(encoding="utf-8"))
    assert manual == {
        "eligible_for_manual_apply": False,
        "generated_at": canonical.updated_at,
        "items": [],
        "repair_policy": "report_only",
        "requires_manual_confirmation": False,
        "total_items": 0,
        "unsafe_auto_rewrite_enabled": False,
        "validation_run_id": canonical.validation_run_id,
    }

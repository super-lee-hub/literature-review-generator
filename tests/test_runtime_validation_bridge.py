from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from tests.test_runtime_bridge_helpers import build_legacy_main, write_json


def test_runtime_validation_bridge_registers_reports(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")

    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)

    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="demo-ai",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="validate_review",
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap(build_legacy_main())

    review_draft_path = Path(session.generator._review_draft_v2_path())
    citation_manifest_path = Path(session.generator._citation_manifest_path())
    report_file = Path(session.context.workspace.report_path("demo-ai_validation_report.txt"))
    manual_report_file = Path(session.context.workspace.report_path("demo-ai_manual_review_report.json"))

    write_json(
        review_draft_path,
        {"artifact_type": "review_draft", "artifact_version": "v2", "content": {"sections": []}},
    )
    write_json(
        citation_manifest_path,
        {"artifact_type": "citation_manifest", "artifact_version": "v3", "occurrences": [], "citation_sets": []},
    )
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("ok", encoding="utf-8")
    manual_report_file.write_text(json.dumps({"items": []}), encoding="utf-8")

    def _fake_run_review_validation(_adapter):
        return {
            "success": True,
            "report": SimpleNamespace(report_id="validation_report_demo", artifact_version="v1"),
            "manual_review_items": [],
            "report_file": str(report_file),
            "manual_report_file": str(manual_report_file),
        }

    validation_result = bridge.run_validation(
        session,
        validator_module=SimpleNamespace(run_review_validation=_fake_run_review_validation),
    )

    registry_payload = json.loads(Path(session.context.workspace.paths.registry_path).read_text(encoding="utf-8"))
    artifact_ids = {item["artifact_id"] for item in registry_payload["artifacts"]}
    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}

    assert validation_result.success is True
    assert "validation_report_demo" in artifact_ids
    assert "validation_report" in artifact_types
    assert "manual_review_report" in artifact_types

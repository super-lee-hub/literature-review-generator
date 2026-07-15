from __future__ import annotations

from pathlib import Path
import json

from tests.test_runtime_bridge_helpers import build_success_summary, make_bridge_session


def test_runtime_stage1_bridge_persists_summaries_progress_and_paper_artifacts(tmp_path: Path) -> None:
    bridge, session, pdf_dir, pdf_path = make_bridge_session(tmp_path, action="analyze")

    result = bridge.persist_stage1_results(
        session,
        [build_success_summary(pdf_path)],
        source_items=[{"path": str(pdf_dir), "source_type": "direct", "label": "pdf-folder", "priority": 0}],
    )

    summary_path = Path(session.generator.summary_file)
    progress_path = Path(session.context.progress_path)
    manifest_path = Path(session.generator._get_summary_source_manifest_path())
    registry_payload = json.loads(Path(session.context.workspace.paths.registry_path).read_text(encoding="utf-8"))

    assert result.success is True
    assert summary_path.exists() is True
    assert progress_path.exists() is True
    assert manifest_path.exists() is True
    assert json.loads(summary_path.read_text(encoding="utf-8"))[0]["paper_info"]["canonical_paper_key"] == "paper_a"

    artifact_types = {item["artifact_type"] for item in registry_payload["artifacts"]}
    assert "source_pdf" in artifact_types
    assert "source_bundle" in artifact_types
    assert "summary_file" in artifact_types
    assert "stage1_progress_snapshot" in artifact_types
    assert "summary_source_manifest" in artifact_types
    assert "paper_artifact" in artifact_types
    summary_record = next(
        item for item in registry_payload["artifacts"] if item["artifact_type"] == "summary_file"
    )
    assert [item["artifact_id"] for item in summary_record["depends_on"]] == [
        "source_bundle"
    ]


def test_runtime_stage1_bridge_always_binds_summary_to_source_bundle(
    tmp_path: Path,
) -> None:
    bridge, session, _pdf_dir, pdf_path = make_bridge_session(tmp_path, action="analyze")

    bridge.persist_stage1_results(session, [build_success_summary(pdf_path)])

    registry_payload = json.loads(
        Path(session.context.workspace.paths.registry_path).read_text(encoding="utf-8")
    )
    summary_record = next(
        item for item in registry_payload["artifacts"] if item["artifact_type"] == "summary_file"
    )
    source_bundle_record = next(
        item for item in registry_payload["artifacts"] if item["artifact_type"] == "source_bundle"
    )
    assert [item["artifact_id"] for item in summary_record["depends_on"]] == [
        "source_bundle"
    ]
    assert [item["artifact_type"] for item in source_bundle_record["depends_on"]] == [
        "source_pdf"
    ]

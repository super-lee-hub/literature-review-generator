from __future__ import annotations

import json
from pathlib import Path

from tests.test_runtime_bridge_helpers import build_success_summary, make_bridge_session


def test_generation_stages_emit_runtime_stage_trace_records(tmp_path: Path) -> None:
    bridge, session, _pdf_dir, pdf_path = make_bridge_session(tmp_path, action="run_all")

    bridge.persist_stage1_results(
        session,
        [build_success_summary(pdf_path)],
        subagent_run_id="stage1-subagent-001",
    )
    outline_result = bridge.persist_outline(
        session,
        "# Demo Outline\n\n## 1. Findings",
        subagent_run_id="stage2-subagent-001",
    )
    bridge.persist_review_chain(
        session,
        outline_file=outline_result.artifacts[0].path,
        review_sections=[
            {
                "section_number": 1,
                "section_title": "Findings",
                "content": "Stable result. [[cite:paper_a|mode=parenthetical]]",
            }
        ],
        subagent_run_id="stage3-subagent-001",
    )

    trace_path = Path(session.context.workspace.artifact_path("runtime_stage_trace.json"))
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    entries = trace_payload["entries"]

    stage1_subagent = next(item for item in entries if item["step_name"] == "subagent_generation_complete")
    stage2_subagent = next(item for item in entries if item["step_name"] == "subagent_outline_complete")
    stage3_subagent = next(item for item in entries if item["step_name"] == "subagent_review_complete")
    local_entries = [item for item in entries if item["execution_mode"] == "local"]

    assert stage1_subagent["execution_mode"] == "subagent"
    assert stage2_subagent["execution_mode"] == "subagent"
    assert stage3_subagent["execution_mode"] == "subagent"
    assert stage1_subagent["subagent_run_id"] == "stage1-subagent-001"
    assert all(item["legacy_api_path_used"] is False for item in entries)
    assert any(item["step_name"] == "persist_stage1_results" for item in local_entries)
    assert any(item["step_name"] == "persist_outline_artifact" for item in local_entries)
    assert any(item["step_name"] == "persist_review_chain" for item in local_entries)


def test_generation_stages_without_subagent_ids_emit_local_trace_only(tmp_path: Path) -> None:
    bridge, session, _pdf_dir, pdf_path = make_bridge_session(tmp_path, action="run_all")

    bridge.persist_stage1_results(session, [build_success_summary(pdf_path)])
    outline_result = bridge.persist_outline(session, "# Demo Outline\n\n## 1. Findings")
    bridge.persist_review_chain(
        session,
        outline_file=outline_result.artifacts[0].path,
        review_sections=[
            {
                "section_number": 1,
                "section_title": "Findings",
                "content": "Stable result. [[cite:paper_a|mode=parenthetical]]",
            }
        ],
    )

    trace_path = Path(session.context.workspace.artifact_path("runtime_stage_trace.json"))
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    entries = trace_payload["entries"]

    assert not any(item["execution_mode"] == "subagent" for item in entries)
    assert any(item["step_name"] == "persist_stage1_results" for item in entries)
    assert any(item["step_name"] == "persist_outline_artifact" for item in entries)
    assert any(item["step_name"] == "persist_review_chain" for item in entries)

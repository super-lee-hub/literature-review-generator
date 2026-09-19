from __future__ import annotations

import json
from pathlib import Path

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from tests.test_runtime_bridge_helpers import current_config


def test_agent_runtime_bridge_bootstrap_and_trace(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")

    skill_output = tmp_path / "output" / "_queue" / "queue.json"
    skill_output.parent.mkdir(parents=True)
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="demo-ai",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="run_all",
            config=str(current_config(tmp_path)),
            queue_file=str(skill_output),
        )
    )
    session = bridge.bootstrap()
    source_bundle = bridge.build_source_bundle()
    source_ref = bridge.persist_source_bundle(session, source_bundle)
    trace_ref = bridge.write_stage_trace(session)
    final_resume_state = bridge.finalize(session)

    assert session.context.workspace.root_dir
    assert Path(source_ref.path).exists()
    assert Path(trace_ref.path).exists()
    assert final_resume_state

    pointer_payload = json.loads(Path(session.context.pointer_path).read_text(encoding="utf-8"))
    assert pointer_payload["status"] == "completed"

    registry = session.context.registry
    assert registry.get("source_bundle") is not None
    assert registry.get("runtime_stage_trace") is not None


def test_f1_bound_outline_with_reused_summary_still_intakes_local_pdfs(
    tmp_path: Path,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "alpha.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%alpha\n")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text("{}", encoding="utf-8")

    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="f1-outline-source-intake",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="generate_outline",
            config=str(current_config(tmp_path)),
            summary_file=str(summary_path),
            metadata={
                "f1_corpus_binding": {
                    "schema_version": "f1-corpus-binding-v1",
                    "manifest_path": str(tmp_path / "manifest.json"),
                    "manifest_sha256": "a" * 64,
                    "source_ids": ["F1-01"],
                }
            },
        )
    )
    bridge.bootstrap()

    bundle = bridge.build_source_bundle()

    assert [item.source_pdf for item in bundle.paper_work_items] == [str(pdf_path)]

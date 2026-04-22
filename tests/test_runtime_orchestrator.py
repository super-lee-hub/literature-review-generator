from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge


class _FakeGenerator:
    def __init__(
        self,
        config_file: str,
        project_name: str,
        pdf_folder: str | None,
        queue_file: str,
        zotero_report: str | None,
        library_path: str | None,
    ) -> None:
        self.config_file = config_file
        self.project_name = project_name
        self.pdf_folder = pdf_folder
        self.queue_file = queue_file
        self.zotero_report = zotero_report
        self.library_path = library_path
        self.config = {"Paths": {"output_path": str(Path(queue_file).parent.parent.parent / "output")}}
        self.bound_workspace = None
        self.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

    def load_configuration(self) -> bool:
        return True

    def bind_job_workspace(self, **kwargs):
        self.bound_workspace = kwargs["workspace"]


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
            queue_file=str(skill_output),
        )
    )
    legacy_main = SimpleNamespace(LiteratureReviewGenerator=_FakeGenerator)

    session = bridge.bootstrap(legacy_main)
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

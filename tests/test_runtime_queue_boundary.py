from __future__ import annotations

import json
from pathlib import Path

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from tests.test_runtime_bridge_helpers import build_legacy_main


def test_ai_runtime_remains_out_of_queue_but_workspace_compatible(tmp_path: Path) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")

    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(json.dumps({"jobs": {}, "runtimes": {}, "last_updated": "2026-04-22T00:00:00Z"}), encoding="utf-8")

    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="demo-ai",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="analyze",
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap(build_legacy_main())

    queue_payload = json.loads(queue_file.read_text(encoding="utf-8"))

    assert Path(session.context.workspace.root_dir).exists() is True
    assert queue_payload["jobs"] == {}
    assert queue_payload["runtimes"] == {}

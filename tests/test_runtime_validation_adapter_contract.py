from __future__ import annotations

from pathlib import Path

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.validation_adapter import RuntimeValidationAdapter
from tests.test_runtime_bridge_helpers import current_config


def test_validation_adapter_satisfies_run_review_validation_contract(tmp_path: Path) -> None:
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
            config=str(current_config(tmp_path)),
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap()

    def external_registry_resolver(_job_id: str) -> None:
        return None

    adapter = bridge.build_validation_adapter(
        session,
        external_registry_resolver=external_registry_resolver,
    )

    assert isinstance(adapter, RuntimeValidationAdapter)
    assert hasattr(adapter, "logger")
    assert hasattr(adapter, "config")
    assert hasattr(adapter, "artifact_registry")
    assert hasattr(adapter, "job_workspace")
    assert adapter.validation_external_registry_resolver is external_registry_resolver
    assert callable(adapter._review_draft_path)
    assert callable(adapter._citation_manifest_path)
    assert callable(adapter._get_review_word_file_path)
    assert callable(adapter._persist_citation_manifest)
    assert callable(adapter.save_summaries)
    assert callable(adapter._persist_paper_artifact)
    assert callable(adapter._stage2_validation_enabled)
    assert callable(adapter.get_paper_key)

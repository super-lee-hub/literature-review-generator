from __future__ import annotations

from pathlib import Path

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from validation.execution_service import ValidationExecutionService
from tests.test_runtime_bridge_helpers import current_config


def test_validation_execution_service_satisfies_run_review_validation_contract(tmp_path: Path) -> None:
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

    service = bridge.build_validation_service(
        session,
        external_registry_resolver=external_registry_resolver,
    )

    assert isinstance(service, ValidationExecutionService)
    assert hasattr(service, "logger")
    assert hasattr(service, "config")
    assert hasattr(service, "artifact_registry")
    assert hasattr(service, "job_workspace")
    assert service.validation_external_registry_resolver is external_registry_resolver
    assert callable(service._review_draft_path)
    assert callable(service._citation_manifest_path)
    assert callable(service._get_review_word_file_path)
    assert callable(service._persist_citation_manifest)
    assert callable(service.save_summaries)
    assert callable(service._persist_paper_artifact)
    assert callable(service._stage2_validation_enabled)
    assert callable(service.get_paper_key)
    assert callable(service.new_provider_runtime)
    assert callable(service.finalize_provider_receipts)

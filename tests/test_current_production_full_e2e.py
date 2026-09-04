from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner

from tests.test_current_runtime_full_e2e import (
    _adjudicator_response,
    _outline_provider_response,
    _provider_response,
    _reader_summary,
    _test_config,
    _write_pdf,
)


def test_current_production_full_chain_uses_runner_validation_export_and_attestation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Exercise the production runner and control-plane boundaries end to end.

    Providers are injected at the configured transport boundary; no final
    validation artifact, completion status, or export trust result is written
    by the test.
    """

    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("paper-a", "Study A", "The treatment improved the outcome."),
        ("paper-b", "Study B", "The treatment improved the outcome in a second context."),
        ("paper-c", "Study C", "The treatment improved the outcome under a third condition."),
    ]
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)

    reader_index = 0

    def configured_reader(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        paper_key, title, finding = papers[reader_index]
        reader_index += 1
        return {"status": "success", "content": _reader_summary(paper_key, title, finding)}

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        envelope = json.loads(prompt)
        return _outline_provider_response(
            str(envelope["node_id"]),
            dict(envelope["request"]),
        )

    def configured_writer(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        ref_ids = re.findall(r"R\d{3,}", prompt)
        ref_id = ref_ids[0] if ref_ids else "R001"
        return _provider_response(
            {
                "blocks": [
                    {
                        "text": (
                            "The evidence supports the bounded synthesis "
                            f"[[cite_ref:{ref_id}]]."
                        )
                    }
                ]
            }
        )

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed", configured_writer)
    monkeypatch.setattr("ai_interface._call_ai_api", _adjudicator_response)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", _adjudicator_response)

    spec = RuntimeJobSpec(
        project_name="current-production-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-production-e2e-job",
        config=str(_test_config(tmp_path)),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
    )

    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
    assert first.failed_stage is None, first
    assert first.completed_stages == ("source_intake", "analyze", "outline"), first
    assert "explicit adoption" in first.message, first

    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    inspection = control.inspect(workspace=first.workspace_path)
    final_outline = next(
        artifact
        for artifact in inspection["artifacts"]
        if artifact["artifact_id"] == "outline-v3:final_outline"
    )
    adoption = control.adopt(
        workspace=first.workspace_path,
        artifact_id="outline-v3:final_outline",
        actor="tests.current_production_full_e2e",
        reason="explicitly approve the verified outline for the production review stage",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adoption["status"] == "succeeded", adoption
    assert adoption["mutation_performed"] is True

    completed = control.resume(workspace=first.workspace_path)
    assert completed["job_status"] == "completed", completed
    assert completed["completion_status"] == "complete", completed
    assert completed["canonical_ready"] is True, completed
    assert completed["completed_stages"] == (
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    ), completed

    validation = control.validation_status(workspace=first.workspace_path)
    assert validation["status"] == "clean", validation
    assert validation["read_only"] is True
    assert validation["validation_artifact"]["status"] == "ready", validation

    export = control.export(workspace=first.workspace_path)
    assert export["status"] == "canonical_verified", export
    assert Path(export["bundle_path"]).is_file()
    assert export["artifact_id"].startswith("export_bundle:")

    attestation = control.attest(workspace=first.workspace_path)
    assert attestation["status"] == "canonical_verified", attestation
    assert Path(attestation["report_path"]).is_file()
    assert attestation["artifact_id"].startswith("forensic_attestation:")

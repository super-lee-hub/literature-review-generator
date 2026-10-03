from __future__ import annotations

import ai_interface
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner

from tests.test_current_runtime_full_e2e import (
    _adjudicator_response,
    _assert_source_bound_fixture_trace,
    _assert_writer_http_receipts_match,
    _configure_writer_loopback,
    _outline_provider_response,
    _reader_summary,
    _start_source_bound_writer_server,
    _test_config,
    _write_pdf,
)


def test_current_production_full_chain_uses_runner_validation_export_and_attestation(
    tmp_path: Path,
    monkeypatch: Any,
    request: Any,
) -> None:
    """Exercise the production runner and control-plane boundaries end to end.

    Reader, outline, and validator responses are injected. Writer uses a local
    HTTP endpoint through the production transport wrapper so its receipt is
    owned by ProviderRuntime rather than an opaque test callback.
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

    fixture_trace: list[dict[str, Any]] = []
    reader_index = 0
    original_uninstrumented = ai_interface._call_ai_api_detailed_uninstrumented

    def configured_reader(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        paper_key, title, finding = papers[reader_index]
        reader_index += 1
        return {"status": "success", "content": _reader_summary(paper_key, title, finding)}

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = None
        if (
            isinstance(envelope, Mapping)
            and str(envelope.get("node_id") or "")
            and isinstance(envelope.get("request"), Mapping)
        ):
            return _outline_provider_response(
                str(envelope["node_id"]),
                dict(envelope["request"]),
                fixture_trace=fixture_trace,
            )
        api_config = args[1] if len(args) > 1 else kwargs.get("api_config")
        if (isinstance(envelope, Mapping) and isinstance(envelope.get("writer_task_scope"), Mapping)
                and isinstance(api_config, Mapping)
                and urlparse(str(api_config.get("api_base") or "")).hostname == "127.0.0.1"):
            return original_uninstrumented(*args, **kwargs)
        raise AssertionError("production fixture rejected an unknown or non-loopback request")

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api", _adjudicator_response)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", _adjudicator_response)

    writer_server, writer_thread, writer_api_base = _start_source_bound_writer_server(fixture_trace)
    request.addfinalizer(writer_server.server_close)
    request.addfinalizer(writer_thread.join)
    request.addfinalizer(writer_server.shutdown)
    config_path = _test_config(tmp_path)
    _configure_writer_loopback(config_path, writer_api_base)

    spec = RuntimeJobSpec(
        project_name="current-production-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-production-e2e-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
    )

    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
    assert first.failed_stage is None, first
    assert first.completed_stages == ("source_intake", "analyze", "outline"), first
    from services.writer_source_inventory import load_writer_source_inventory_v1

    _workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    scope_record = registry.get("outline-v3:candidate_output_scope")
    assert scope_record is not None
    registry.verify_ready_artifact_closure(scope_record)
    scope_payload = json.loads(Path(scope_record.path).read_text(encoding="utf-8"))
    assert scope_payload["limits"]["max_claims"] == len(scope_payload["claim_groups"])
    assert scope_payload["limits"]["max_support_rows"] == len(scope_payload["claim_slots"])
    assert scope_payload["source_inventory_binding"]["artifact_id"] == load_writer_source_inventory_v1(registry).artifact_id
    for artifact in registry.list_records():
        if artifact.artifact_id.startswith("outline-v3:candidate_") and artifact.artifact_id.endswith("_provider_generation"):
            content = json.loads(Path(artifact.path).read_text(encoding="utf-8"))["payload"]
            assert all(section.get("task_ids") for section in content["sections"])
            assert all(row.get("claim_slot_id") for section in content["sections"] for row in section["claim_support"])
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
    assert completed["job_status"] == "completed", (
        completed, writer_server.calls, writer_server.errors, fixture_trace
    )
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
    _assert_source_bound_fixture_trace(fixture_trace)
    _assert_writer_http_receipts_match(
        writer_server,
        control.inspect(workspace=first.workspace_path),
    )

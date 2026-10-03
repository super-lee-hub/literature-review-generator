from __future__ import annotations

import argparse
import importlib
import json
import sys
import types
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.job_runner import (
    JobRunner,
    build_job_request_from_args,
    build_job_request_from_mapping,
)
from services.job_outcome import load_canonical_job_outcome
from services.queue_service import (
    PersistentQueueService,
    QueueJobSpec,
    QueueRunner,
    QueueState,
)
from validation.closure import resolve_current_stage_closure_map
from runtime.control_plane import ReviewControlPlane

from tests.test_current_runtime_full_e2e import (
    _adjudicator_response,
    _reader_summary,
    _test_config,
    _write_pdf,
)


@pytest.fixture()
def gui_app_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the controller with the same lightweight UI seam as GUI tests."""

    fake_nicegui = types.ModuleType("nicegui")
    setattr(fake_nicegui, "ui", types.SimpleNamespace(notify=lambda *args, **kwargs: None))
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    return importlib.import_module("gui.app")


def _seed_direct_fixture(tmp_path: Path) -> tuple[Path, list[tuple[str, str, str]], Path]:
    papers = [("parity-a", "Parity Study", "The treatment improved the outcome.")]
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)
    return pdf_dir, papers, _test_config(tmp_path)


def _seed_run_all_fixture(tmp_path: Path) -> tuple[Path, list[tuple[str, str, str]], Path]:
    papers = [
        ("parity-a", "Parity Study A", "The treatment improved the outcome."),
        ("parity-b", "Parity Study B", "The treatment improved the outcome in a second context."),
        ("parity-c", "Parity Study C", "The treatment improved the outcome under a third condition."),
    ]
    pdf_dir = tmp_path / "run-all-papers"
    pdf_dir.mkdir()
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)
    return pdf_dir, papers, _test_config(tmp_path)


def _patch_reader(monkeypatch: pytest.MonkeyPatch, papers: list[tuple[str, str, str]]) -> None:
    by_key = {key: (title, finding) for key, title, finding in papers}

    def configured_reader(
        _service: Any,
        *,
        item: Any,
        built_input: Any,
        primary_config: Mapping[str, Any],
        backup_config: Mapping[str, Any],
        stage1_input_settings: Mapping[str, Any],
        runtime: Any,
    ) -> Mapping[str, Any]:
        del built_input, primary_config, backup_config, stage1_input_settings, runtime
        key = str(item.canonical_paper_key)
        title = str(item.paper_info.get("title") or "")
        expected_title, finding = by_key.get(key, (title, "bounded evidence"))
        summary = _reader_summary(key, expected_title, finding)
        summary["paper_info"]["source_paper_id"] = item.source_paper_id
        return {"status": "success", "content": summary}

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
        configured_reader,
    )


def _patch_run_all_providers(
    monkeypatch: pytest.MonkeyPatch,
    papers: list[tuple[str, str, str]],
    adjudicator: Any,
) -> None:
    from tests.test_current_runtime_full_e2e import _outline_provider_response
    from ai_interface import _call_ai_api_detailed_uninstrumented as original_uninstrumented

    _patch_reader(monkeypatch, papers)

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        envelope = json.loads(str(args[0] if args else kwargs.get("prompt") or ""))
        if isinstance(envelope, Mapping) and envelope.get("node_id") and isinstance(envelope.get("request"), Mapping):
            return _outline_provider_response(str(envelope["node_id"]), dict(envelope["request"]))
        api_config = args[1] if len(args) > 1 else kwargs.get("api_config")
        if (isinstance(envelope, Mapping) and isinstance(envelope.get("writer_task_scope"), Mapping)
                and isinstance(api_config, Mapping)
                and urlparse(str(api_config.get("api_base") or "")).hostname == "127.0.0.1"):
            return original_uninstrumented(*args, **kwargs)
        raise AssertionError("parity fixture rejected an unknown or non-loopback provider request")

    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api", adjudicator)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", adjudicator)


@pytest.fixture
def parity_writer_server() -> Any:
    from tests.test_current_runtime_full_e2e import _start_source_bound_writer_server

    server, thread, api_base = _start_source_bound_writer_server([])
    try:
        yield server, api_base
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_parity_fixture_rejects_non_loopback_or_unknown_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import ai_interface

    sends: list[Mapping[str, Any]] = []

    def transport(*args: Any, **kwargs: Any) -> dict[str, Any]:
        sends.append(args[1] if len(args) > 1 else kwargs["api_config"])
        return {"status": "success"}

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed_uninstrumented", transport)
    _patch_run_all_providers(monkeypatch, [], _adjudicator_response)
    writer = json.dumps({"writer_task_scope": {"schema_version": "writer_task_scope_wire/v2"}})
    with pytest.raises(AssertionError, match="non-loopback"):
        ai_interface._call_ai_api_detailed_uninstrumented(writer, {"api_base": "https://example.test"}, "")
    with pytest.raises(AssertionError, match="unknown"):
        ai_interface._call_ai_api_detailed_uninstrumented(json.dumps({"unexpected": True}),
                                                        {"api_base": "http://127.0.0.1:1/v1"}, "")
    assert sends == []
    assert ai_interface._call_ai_api_detailed_uninstrumented(
        writer, {"api_base": "http://127.0.0.1:1/v1"}, "",
    )["status"] == "success"
    assert len(sends) == 1


def _complete_adopted_run(workspace_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    inspection = control.inspect(workspace=workspace_path)
    final_outline = next(
        item for item in inspection["artifacts"] if item["artifact_id"] == "outline-v3:final_outline"
    )
    adopted = control.adopt(
        workspace=workspace_path,
        artifact_id="outline-v3:final_outline",
        actor="tests.validation_entrypoint_parity",
        reason="explicit parity acceptance adoption",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adopted["status"] == "succeeded", adopted
    completed = control.resume(workspace=workspace_path)
    assert completed["completion_status"] == "complete", completed
    exported = control.export(workspace=workspace_path)
    assert exported["status"] == "canonical_verified", exported.get("manifest", {}).get("issues")
    assert Path(exported["bundle_path"]).is_file()
    return completed, exported


def _run_all_signature(workspace_path: str, completion: Mapping[str, Any], export: Mapping[str, Any]) -> dict[str, Any]:
    registry = AgentRuntimeRunner._open_workspace(workspace_path)[1]
    spec_record = registry.get("runtime_job_spec")
    assert spec_record is not None
    persisted_spec = json.loads(Path(spec_record.path).read_text(encoding="utf-8"))
    outcome, _ = load_canonical_job_outcome(registry)
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    stage_map = resolve_current_stage_closure_map(registry)
    draft_record = registry.get("review_draft")
    manifest_record = registry.get("citation_manifest_v3")
    docx_record = registry.get("review_docx")
    assert draft_record is not None and manifest_record is not None and docx_record is not None
    draft = json.loads(Path(draft_record.path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    cited_bibliography = [
        str(entry.get("citation_text") or "").strip()
        for entry in manifest.get("bibliography", [])
        if isinstance(entry, Mapping) and entry.get("is_cited", True)
    ]
    assert draft.get("content", {}).get("references") == cited_bibliography
    cited_keys = {
        str(entry.get("paper_key") or entry.get("paper_id") or "").strip()
        for entry in manifest.get("bibliography", [])
        if isinstance(entry, Mapping) and entry.get("is_cited", True)
    }
    occurrence_keys = {
        str(entry.get("paper_key") or entry.get("paper_id") or "").strip()
        for entry in manifest.get("occurrences", [])
        if isinstance(entry, Mapping) and str(entry.get("paper_id") or "") != "unknown"
    }
    assert occurrence_keys == cited_keys
    from docx import Document
    from docx_writer import scan_docx_for_unresolved_citation_tokens

    docx = Document(str(docx_record.path))
    paragraphs = [paragraph.text for paragraph in docx.paragraphs]
    references_index = paragraphs.index("References")
    assert paragraphs[references_index + 1 :] == cited_bibliography
    assert scan_docx_for_unresolved_citation_tokens(str(docx_record.path), manifest)[
        "passed"
    ] is True
    validation_record = registry.get(current_set.validation_run_result_artifact_id)
    assert validation_record is not None
    from validation.run_result import ValidationRunResultV1

    validation = ValidationRunResultV1.from_dict(
        json.loads(Path(validation_record.path).read_text(encoding="utf-8"))
    )
    assert validation.input_artifacts.validation_source_authority_hash
    assert validation.input_artifacts.validation_source_authority_fingerprint
    return {
        "stage_plan": persisted_spec["metadata"]["stage_plan"],
        "readiness_policy_snapshot": outcome.to_dict()["readiness_policy_snapshot"],
        "job_disposition": outcome.job_disposition,
        "canonical_ready": outcome.canonical_ready,
        "completed_stages": outcome.completed_stages,
        "validation_status": current_set.validation_status,
        "requested_stages": stage_map.requested_stages,
        "completion_status": completion["completion_status"],
        "export_status": export["status"],
    }


def _mapping(pdf_dir: Path, config: Path, *, job_id: str) -> dict[str, Any]:
    return {
        "config": str(config),
        "project_name": "entrypoint-parity",
        "job_id": job_id,
        "pdf_folder": str(pdf_dir),
        "source_mode": "direct",
        "analyze_only": True,
        "requested_stages": ("analyze",),
        "queue_file": str(config.parent / "queue.json"),
    }


def test_direct_cli_gui_and_queue_normalize_to_one_job_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gui_app_module: Any,
) -> None:
    pdf_dir, papers, config = _seed_direct_fixture(tmp_path)
    _patch_reader(monkeypatch, papers)

    mapping = _mapping(pdf_dir, config, job_id="parity-direct")
    mapping_request = build_job_request_from_mapping(mapping)
    cli_request = build_job_request_from_args(argparse.Namespace(**mapping))
    assert asdict(mapping_request) == asdict(cli_request)

    controller = gui_app_module.WorkspaceController(str(config))
    controller.state["workflow"]["input_mode"] = "pdf"
    gui_spec = controller._build_queue_job_spec(
        "entrypoint-parity", str(pdf_dir), "", "analyze", input_mode="pdf"
    )
    gui_request = build_job_request_from_mapping(gui_spec.parameters)
    assert gui_request.action == mapping_request.action == "analyze"
    assert gui_request.pdf_folder == mapping_request.pdf_folder
    assert gui_request.requested_stages is None

    runtime_spec = RuntimeJobSpec(
        project_name="entrypoint-parity",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="parity-direct",
        config=str(config),
        action="analyze",
        queue_file=str(config.parent / "queue.json"),
        metadata={"requested_stages": ["analyze"]},
    )
    direct = AgentRuntimeRunner(runtime_spec).run()
    assert direct.job_status == "completed", direct
    assert direct.canonical_ready is True

    workspace, registry = AgentRuntimeRunner._open_workspace(direct.workspace_path)
    spec_record = registry.get("runtime_job_spec")
    assert spec_record is not None
    persisted = json.loads(Path(spec_record.path).read_text(encoding="utf-8"))
    stage_plan = persisted["metadata"]["stage_plan"]
    assert stage_plan["requested_stages"] == ["analyze"]
    assert stage_plan["required_stages"] == ["source_intake", "analyze"]
    outcome, _ = load_canonical_job_outcome(registry)
    assert outcome.to_dict()["readiness_policy_snapshot"]["stage_plan"] == stage_plan
    # Analyze-only plans deliberately do not require a CurrentArtifactSet;
    # the stage plan is the canonical indication of that boundary.
    assert registry.resolve_current_artifact_set() is None
    stage_map = resolve_current_stage_closure_map(registry)
    assert stage_map.requested_stages == ("analyze",)
    assert stage_map.blocking_issues == ()
    assert outcome.completed_stages == ("source_intake", "analyze")
    assert Path(workspace.root_dir).resolve() == Path(direct.workspace_path).resolve()


def test_real_queue_runner_executes_gui_queue_spec_on_shared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gui_app_module: Any,
) -> None:
    pdf_dir, papers, config = _seed_direct_fixture(tmp_path)
    _patch_reader(monkeypatch, papers)
    controller = gui_app_module.WorkspaceController(str(config))
    controller.state["workflow"]["input_mode"] = "pdf"
    spec = controller._build_queue_job_spec(
        "queue-parity", str(pdf_dir), "", "analyze", input_mode="pdf"
    )
    queue_path = tmp_path / "queue" / "queue.json"
    queue = PersistentQueueService(queue_path)
    queue.add_job(spec)
    persisted_spec = QueueJobSpec.from_dict(json.loads(queue_path.read_text(encoding="utf-8"))["jobs"][spec.job_id])
    assert persisted_spec.to_dict() == queue.get_job(spec.job_id).to_dict()  # type: ignore[union-attr]
    assert not Path(str(persisted_spec.workspace_path)).exists()
    QueueRunner(queue, JobRunner()).run_single_job(spec.job_id)
    runtime = queue.get_job_runtime(spec.job_id)
    assert runtime is not None
    assert runtime.state == QueueState.COMPLETED
    result = runtime.result_summary or {}
    assert result["job_id"] == spec.job_id
    workspace_path = Path(str(runtime.workspace_path))
    assert workspace_path.is_dir()
    registry = AgentRuntimeRunner._open_workspace(str(workspace_path))[1]
    outcome, _ = load_canonical_job_outcome(registry)
    assert outcome.job_status == "completed"
    assert outcome.canonical_ready is True
    assert resolve_current_stage_closure_map(registry).requested_stages == ("analyze",)


def test_persisted_runtime_job_spec_resume_preserves_stage_plan_and_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_dir, papers, config = _seed_direct_fixture(tmp_path)
    _patch_reader(monkeypatch, papers)
    spec = RuntimeJobSpec(
        project_name="resume-parity",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="resume-parity-job",
        config=str(config),
        action="analyze",
        queue_file=str(tmp_path / "resume.queue.json"),
        metadata={"requested_stages": ["analyze"]},
    )
    first = AgentRuntimeRunner(spec).run()
    first_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)[1]
    first_record = first_registry.get("runtime_job_spec")
    assert first_record is not None
    first_outcome, _ = load_canonical_job_outcome(first_registry)

    resumed = AgentRuntimeRunner(spec).resume()
    assert resumed.job_status == "completed"
    assert resumed.canonical_ready is True
    resumed_registry = AgentRuntimeRunner._open_workspace(resumed.workspace_path)[1]
    resumed_record = resumed_registry.get("runtime_job_spec")
    assert resumed_record is not None
    assert resumed_record.content_hash == first_record.content_hash
    resumed_outcome, _ = load_canonical_job_outcome(resumed_registry)
    assert resumed_outcome.readiness_policy_snapshot == first_outcome.readiness_policy_snapshot
    assert resumed_outcome.completed_stages == first_outcome.completed_stages == (
        "source_intake",
        "analyze",
    )
    assert resolve_current_stage_closure_map(resumed_registry).requested_stages == ("analyze",)


def test_run_all_clean_parity_across_production_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gui_app_module: Any,
    parity_writer_server: Any,
) -> None:
    """Compare canonical run_all outputs after each real entrypoint path."""

    pdf_dir, papers, config = _seed_run_all_fixture(tmp_path)
    _patch_run_all_providers(monkeypatch, papers, _adjudicator_response)
    from tests.test_current_runtime_full_e2e import (
        _assert_writer_http_receipts_match, _configure_writer_loopback,
    )

    writer_server, writer_api_base = parity_writer_server
    _configure_writer_loopback(config, writer_api_base)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])

    intent = {
        "config": str(config),
        "project_name": "run-all-parity",
        "pdf_folder": str(pdf_dir),
        "source_mode": "direct",
        "run_all": True,
        "queue_file": str(tmp_path / "intent.queue.json"),
    }
    cli_request = build_job_request_from_args(argparse.Namespace(**intent))
    mapping_request = build_job_request_from_mapping(intent)
    assert asdict(cli_request) == asdict(mapping_request)

    controller = gui_app_module.WorkspaceController(str(config))
    controller.state["workflow"]["input_mode"] = "pdf"
    gui_spec = controller._build_queue_job_spec(
        "run-all-parity", str(pdf_dir), "", "run_all", input_mode="pdf"
    )
    roundtrip_spec = QueueJobSpec.from_dict(gui_spec.to_dict())
    gui_request = build_job_request_from_mapping(roundtrip_spec.parameters)
    assert gui_request.action == mapping_request.action == "run_all"
    assert gui_request.pdf_folder == mapping_request.pdf_folder
    assert gui_request.run_all is True

    direct_spec = RuntimeJobSpec(
        project_name="run-all-parity",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="run-all-direct",
        config=str(config),
        action="run_all",
        queue_file=str(tmp_path / "direct.queue.json"),
        metadata={},
    )
    direct_initial = AgentRuntimeRunner(direct_spec).run()
    assert direct_initial.job_status == "completed", direct_initial
    assert direct_initial.job_disposition == "needs_review"
    direct_completion, direct_export = _complete_adopted_run(direct_initial.workspace_path)
    direct_signature = _run_all_signature(direct_initial.workspace_path, direct_completion, direct_export)
    _assert_writer_http_receipts_match(writer_server, control.inspect(workspace=direct_initial.workspace_path))
    writer_server.calls.clear()

    job_request = build_job_request_from_mapping({**intent, "job_id": "run-all-job-runner"})
    job_initial = JobRunner().run(job_request)
    assert job_initial.job_status == "completed"
    assert job_initial.job_disposition == "needs_review"
    job_completion, job_export = _complete_adopted_run(job_initial.workspace_path)
    job_signature = _run_all_signature(job_initial.workspace_path, job_completion, job_export)
    _assert_writer_http_receipts_match(writer_server, control.inspect(workspace=job_initial.workspace_path))
    writer_server.calls.clear()

    queue_path = tmp_path / "queue" / "queue.json"
    queue = PersistentQueueService(queue_path)
    queued_spec = QueueJobSpec.from_dict(
        {
            **roundtrip_spec.to_dict(),
            "job_id": "run-all-queue",
            "parameters": {**roundtrip_spec.parameters, "job_id": "run-all-queue"},
        }
    )
    queue.add_job(queued_spec)
    persisted_queue_spec = QueueJobSpec.from_dict(
        json.loads(queue_path.read_text(encoding="utf-8"))["jobs"]["run-all-queue"]
    )
    assert persisted_queue_spec.to_dict() == queue.get_job("run-all-queue").to_dict()  # type: ignore[union-attr]
    QueueRunner(queue, JobRunner()).run_single_job("run-all-queue")
    queue_runtime = queue.get_job_runtime("run-all-queue")
    assert queue_runtime is not None
    assert queue_runtime.state == QueueState.COMPLETED
    queue_workspace = str(queue_runtime.workspace_path)
    queue_completion, queue_export = _complete_adopted_run(queue_workspace)
    queue_signature = _run_all_signature(queue_workspace, queue_completion, queue_export)
    _assert_writer_http_receipts_match(writer_server, control.inspect(workspace=queue_workspace))

    assert job_signature == direct_signature
    assert queue_signature == direct_signature

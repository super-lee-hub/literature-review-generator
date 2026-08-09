from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from free_mode.intent_input import (
    FREE_MODE_INTENT_INPUT_ARTIFACT_ID,
    FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID,
    build_free_mode_intent_envelope,
    build_free_mode_writer_context,
    project_review_intent,
    verify_free_mode_intent_input,
    verify_free_mode_review_intent_projection,
)
from outline.v3_executor import OutlineV3Executor
from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from services.job_runner import JobRunRequest, JobRunner, build_job_request_from_mapping
from services.job_workspace import JobWorkspace
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from tests.test_current_stage1_generation import (
    _canonical_summary,
    _service,
    _typed_manifest_record,
    _write_pdf,
)
from tests.test_current_review_generation import _stage1_summary
from tests.test_runtime_bridge_helpers import current_config


def _profile_file(tmp_path: Path, *, research_goal: str = "Explain A to B") -> Path:
    profile = tmp_path / "profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        json.dumps(
            {
                "research_goal": research_goal,
                "concept_relationship": "A leads to B",
                "focus_points": ["variable chain"],
                "exclusions": ["method comparisons"],
                "theory_or_variable_focus": ["mechanism"],
                "outline_preferences": ["mechanism-first"],
                "writing_constraints": ["concise"],
                "generated_prompt": "Focus on the A-to-B mechanism.",
            }
        ),
        encoding="utf-8",
    )
    return profile


def _runtime_spec(
    tmp_path: Path,
    *,
    profile: str = "",
    idea: str = "",
    job_id: str = "free-mode-job",
) -> RuntimeJobSpec:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir(exist_ok=True)
    (pdf_dir / "alpha.pdf").write_bytes(b"%PDF-1.4\n%alpha\n")
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    return RuntimeJobSpec(
        project_name="free-mode-demo",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        action="analyze",
        config=str(current_config(tmp_path)),
        job_id=job_id,
        queue_file=str(queue_file),
        free_mode_profile=profile,
        free_mode_idea=idea,
    )


def test_free_mode_profile_registers_typed_input_and_projection(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    bridge = AgentRuntimeBridge(_runtime_spec(tmp_path, profile=str(profile)))
    session = bridge.bootstrap()
    registry = session.context.registry

    input_record = registry.get(FREE_MODE_INTENT_INPUT_ARTIFACT_ID)
    projection_record = registry.get(FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID)
    assert input_record is not None and input_record.status == "ready"
    assert projection_record is not None and projection_record.status == "ready"
    assert input_record.content_hash == bridge.free_mode_envelope["artifact_hash"]
    payload = json.loads(Path(input_record.path).read_text(encoding="utf-8"))
    assert payload["source_kind"] == "profile"
    assert payload["profile_content_sha256"] == file_sha256(profile)
    assert payload["review_intent"]["review_question"] == "Explain A to B"
    assert payload["review_intent"]["must_cover"] == ["mechanism", "variable chain"]
    assert payload["review_intent"]["must_not_do"] == ["method comparisons"]
    assert payload["review_intent"]["preferred_organizing_logic"] == "mechanism-first"
    assert any(
        dependency.artifact_id == FREE_MODE_INTENT_INPUT_ARTIFACT_ID
        for dependency in projection_record.depends_on
    )


@pytest.mark.parametrize("mode", ["profile", "idea"])
def test_free_mode_generated_job_id_is_bound_before_intake_and_registry(
    tmp_path: Path,
    mode: str,
) -> None:
    profile = _profile_file(tmp_path) if mode == "profile" else None
    spec = _runtime_spec(
        tmp_path,
        profile=str(profile) if profile is not None else "",
        idea="Compare mechanism A and B" if mode == "idea" else "",
        job_id="",
    )

    normalized = AgentRuntimeRunner(spec)._normalized_spec(resume=False)
    assert normalized.job_id
    envelope = normalized.metadata["free_mode_input"]
    assert envelope["payload"]["job_id"] == normalized.job_id

    bridge = AgentRuntimeBridge(normalized)
    session = bridge.bootstrap()
    assert session.context.workspace.job_id == normalized.job_id
    input_record = session.context.registry.get(FREE_MODE_INTENT_INPUT_ARTIFACT_ID)
    assert input_record is not None
    assert input_record.job_id == normalized.job_id
    verify_free_mode_intent_input(session.context.registry, envelope)
    verify_free_mode_review_intent_projection(session.context.registry, envelope)


def test_free_mode_direct_bridge_resolves_omitted_job_id_before_envelope(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    bridge = AgentRuntimeBridge(_runtime_spec(tmp_path, profile=str(profile), job_id=""))

    assert bridge.job_spec.job_id
    assert bridge.free_mode_envelope is not None
    assert bridge.free_mode_envelope["payload"]["job_id"] == bridge.job_spec.job_id


def test_free_mode_same_path_content_change_changes_authority_hash(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    first = build_free_mode_intent_envelope(profile_path=str(profile), job_id="job-a")
    profile.write_text(
        json.dumps({"research_goal": "Explain A to B", "writing_constraints": ["detailed"]}),
        encoding="utf-8",
    )
    second = build_free_mode_intent_envelope(profile_path=str(profile), job_id="job-a")

    assert first is not None and second is not None
    assert first["artifact_hash"] != second["artifact_hash"]
    assert first["context_hash"] != second["context_hash"]
    assert first["payload"]["profile"]["writing_constraints"] == ["concise"]
    assert second["payload"]["profile"]["writing_constraints"] == ["detailed"]


def test_free_mode_resume_uses_frozen_input_not_mutated_external_file(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    spec = _runtime_spec(tmp_path, profile=str(profile), job_id="free-mode-resume")
    normalized = AgentRuntimeRunner(spec)._normalized_spec(resume=False)
    frozen_envelope = normalized.metadata["free_mode_input"]
    frozen_hash = frozen_envelope["artifact_hash"]

    profile.write_text(json.dumps({"research_goal": "Changed after intake"}), encoding="utf-8")
    profile.unlink()
    resume_spec = replace(normalized, free_mode_profile=str(profile))
    resumed = AgentRuntimeRunner(resume_spec)._normalized_spec(resume=True)
    resumed_envelope = resumed.metadata["free_mode_input"]

    assert resumed_envelope["artifact_hash"] == frozen_hash
    assert resumed_envelope["payload"]["profile"]["research_goal"] == "Explain A to B"


def test_free_mode_tampered_registry_artifact_fails_closed(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    bridge = AgentRuntimeBridge(_runtime_spec(tmp_path, profile=str(profile)))
    session = bridge.bootstrap()
    registry = session.context.registry
    record = registry.get(FREE_MODE_INTENT_INPUT_ARTIFACT_ID)
    assert record is not None
    Path(record.path).write_text('{"tampered": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch|unreadable|bytes"):
        verify_free_mode_intent_input(registry, bridge.free_mode_envelope)
    with pytest.raises(ValueError, match="hash mismatch|unreadable|bytes|missing"):
        verify_free_mode_review_intent_projection(registry, bridge.free_mode_envelope)


def test_free_mode_missing_projection_dependency_fails_closed(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    envelope = build_free_mode_intent_envelope(profile_path=str(profile), job_id="job-a")
    workspace = JobWorkspace.create(str(tmp_path / "output"), "free-mode", job_id="job-a")
    from services.artifact_registry import ArtifactRegistry

    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    with pytest.raises(ValueError, match="missing"):
        verify_free_mode_review_intent_projection(registry, envelope or {})


def test_free_mode_normal_mode_registers_nothing(tmp_path: Path) -> None:
    bridge = AgentRuntimeBridge(_runtime_spec(tmp_path))
    session = bridge.bootstrap()
    registry = session.context.registry

    assert registry.get(FREE_MODE_INTENT_INPUT_ARTIFACT_ID) is None
    assert registry.get(FREE_MODE_REVIEW_INTENT_PROJECTION_ARTIFACT_ID) is None
    assert bridge.free_mode_envelope is None


def test_free_mode_review_intent_projection_changes_with_profile_semantics(tmp_path: Path) -> None:
    first = project_review_intent(
        {"research_goal": "Compare A and B", "focus_points": ["mechanism"]}
    )
    second = project_review_intent(
        {"research_goal": "Explain C", "focus_points": ["moderator"]}
    )
    same = project_review_intent(
        {"research_goal": "Compare A and B", "focus_points": ["mechanism"]}
    )
    from outline.v3_models import ReviewIntent

    assert ReviewIntent.from_dict(first).content_hash != ReviewIntent.from_dict(second).content_hash
    assert ReviewIntent.from_dict(first).content_hash == ReviewIntent.from_dict(same).content_hash


def test_free_mode_outline_identity_changes_with_review_intent(tmp_path: Path) -> None:
    summary, _pdf = _stage1_summary(tmp_path)
    profile_a = _profile_file(tmp_path, research_goal="Explain A to B")
    profile_b = _profile_file(tmp_path / "b", research_goal="Explain C")
    envelope_a = build_free_mode_intent_envelope(profile_path=str(profile_a), job_id="outline-job")
    envelope_b = build_free_mode_intent_envelope(profile_path=str(profile_b), job_id="outline-job")
    assert envelope_a is not None and envelope_b is not None

    workspace = JobWorkspace.create(str(tmp_path / "outline-output"), "outline", job_id="outline-job")
    executor_a = OutlineV3Executor(
        job_id="outline-job",
        summaries=[summary],
        workspace=workspace,
        candidate_count=1,
        stability_mode="off",
        review_intent=envelope_a["review_intent"],
    )
    executor_b = OutlineV3Executor(
        job_id="outline-job",
        summaries=[summary],
        workspace=workspace,
        candidate_count=1,
        stability_mode="off",
        review_intent=envelope_b["review_intent"],
    )
    assert executor_a._review_intent_hash != executor_b._review_intent_hash
    assert executor_a.logical_attempt_identity != executor_b.logical_attempt_identity
    assert executor_a.closure_epoch_id != executor_b.closure_epoch_id


def test_free_mode_idea_changes_outline_identity(tmp_path: Path) -> None:
    summary, _pdf = _stage1_summary(tmp_path)
    envelope_a = build_free_mode_intent_envelope(idea="Compare mechanism A", job_id="idea-outline-job")
    envelope_b = build_free_mode_intent_envelope(idea="Compare mechanism B", job_id="idea-outline-job")
    assert envelope_a is not None and envelope_b is not None

    workspace = JobWorkspace.create(
        str(tmp_path / "idea-outline-output"),
        "outline",
        job_id="idea-outline-job",
    )
    executor_a = OutlineV3Executor(
        job_id="idea-outline-job",
        summaries=[summary],
        workspace=workspace,
        candidate_count=1,
        stability_mode="off",
        review_intent=envelope_a["review_intent"],
    )
    executor_b = OutlineV3Executor(
        job_id="idea-outline-job",
        summaries=[summary],
        workspace=workspace,
        candidate_count=1,
        stability_mode="off",
        review_intent=envelope_b["review_intent"],
    )
    assert executor_a._review_intent_hash != executor_b._review_intent_hash
    assert executor_a.logical_attempt_identity != executor_b.logical_attempt_identity
    assert executor_a.closure_epoch_id != executor_b.closure_epoch_id


def _writer_context(
    *,
    constraints: list[str],
    artifact_hash: str,
    context_hash: str,
) -> dict[str, Any]:
    return {
        "free_mode_input_artifact_id": FREE_MODE_INTENT_INPUT_ARTIFACT_ID,
        "free_mode_input_artifact_hash": artifact_hash,
        "free_mode_context_hash": context_hash,
        "source_kind": "profile",
        "review_intent": {"review_question": "Explain A to B"},
        "profile": {"writing_constraints": constraints},
        "generated_prompt": "Focus on the mechanism.",
        "writing_constraints": constraints,
        "conversation_notes": [],
        "raw_idea": "",
    }


def _writer_run(
    tmp_path: Path,
    summary: Mapping[str, Any],
    context: Mapping[str, Any],
    writer: Any,
    *,
    attempt_id: str = "free-mode-writer-attempt",
    workspace: Any | None = None,
) -> tuple[ReviewGenerationService, Any]:
    config = {
        "Writer_API": {"api_key": "writer", "model": "writer", "api_base": "https://writer.test/v1"},
    }
    if workspace is None:
        workspace = JobWorkspace.create(
            str(tmp_path / "review-output"),
            "free-mode",
            job_id="free-mode-writer-job",
        )
    registry = workspace_registry(workspace)
    service = ReviewGenerationService(
        job_id=workspace.job_id,
        attempt_id=attempt_id,
        workspace=workspace,
        artifact_registry=registry,
        settings=ApplicationSettings.from_config(config),
        summaries=[summary],
        writer=writer,
    )
    paper_key = summary["paper_info"]["canonical_paper_key"]
    result = service.run(
        outline_payload={
            "title": "Free-mode review",
            "sections": [{"section_id": "section_1", "title": "Results", "goal": "Synthesize"}],
        },
        evidence_packets=[
            {
                "section_id": "section_1",
                "section_goal": "Synthesize",
                "planned_claims": ["The treatment improves the outcome."],
                "paper_keys": [paper_key],
                "source_summary_hashes": ["summary-hash"],
                "retrieval_provenance": {"source": "stage1_summary", "paper_keys": [paper_key]},
            }
        ],
        free_mode_context=context,
    )
    return service, result


def workspace_registry(workspace: JobWorkspace) -> Any:
    from services.artifact_registry import ArtifactRegistry

    return ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)


def test_free_mode_writer_context_appears_exactly_once_and_replay_invalidates(
    tmp_path: Path,
) -> None:
    summary, _pdf = _stage1_summary(tmp_path)
    seen: list[Mapping[str, Any]] = []
    workspace = JobWorkspace.create(
        str(tmp_path / "review-output"),
        "free-mode",
        job_id="free-mode-writer-job",
    )

    def writer(**kwargs: Any) -> Mapping[str, Any]:
        seen.append(kwargs)
        return {
            "status": "success",
            "content": {"blocks": [{"text": "The result supports the claim [[cite_ref:R001]]."}]},
            "usage_status": "provider_not_supported",
        }

    context_a = _writer_context(
        constraints=["concise"],
        artifact_hash="a" * 64,
        context_hash="c" * 64,
    )
    _writer_run(tmp_path, summary, context_a, writer, workspace=workspace)
    assert len(seen) == 1
    prompt = str(seen[0].get("prompt_text") or "")
    assert prompt.count('"free_mode_context"') == 1
    payload = json.loads(prompt)
    assert payload["free_mode_context"]["writing_constraints"] == ["concise"]

    replay_calls: list[int] = []

    def replay_writer(**kwargs: Any) -> Mapping[str, Any]:
        replay_calls.append(1)
        return {
            "status": "success",
            "content": {"blocks": [{"text": "Replayed [[cite_ref:R001]]."}]},
            "usage_status": "provider_not_supported",
        }

    _writer_run(tmp_path, summary, context_a, replay_writer, workspace=workspace)
    assert replay_calls == []

    changed_calls: list[int] = []

    def changed_writer(**kwargs: Any) -> Mapping[str, Any]:
        changed_calls.append(1)
        return {
            "status": "success",
            "content": {"blocks": [{"text": "Changed constraints [[cite_ref:R001]]."}]},
            "usage_status": "provider_not_supported",
        }

    context_b = _writer_context(
        constraints=["detailed"],
        artifact_hash="a" * 64,
        context_hash="d" * 64,
    )
    _writer_run(tmp_path, summary, context_b, changed_writer, workspace=workspace)
    assert changed_calls == [1]


def test_free_mode_parity_across_cli_queue_and_runtime_spec(tmp_path: Path) -> None:
    profile = _profile_file(tmp_path)
    cli = build_job_request_from_mapping(
        {
            "project_name": "demo",
            "pdf_folder": str(tmp_path / "papers"),
            "action": "run_all",
            "free_mode_profile": str(profile),
        }
    )
    queue = build_job_request_from_mapping(
        {
            "project_name": "demo",
            "pdf_folder": str(tmp_path / "papers"),
            "action": "run_all",
            "free_mode_profile": str(profile),
            "gui": True,
        }
    )
    spec = _runtime_spec(tmp_path, profile=str(profile))
    runtime = spec.to_job_request()

    assert cli.free_mode_profile == queue.free_mode_profile == runtime.free_mode_profile
    assert cli.free_mode_idea is None
    assert queue.free_mode_idea is None
    assert runtime.free_mode_idea in {None, ""}
    assert spec.free_mode_idea == ""


def test_free_mode_idea_maps_literally(tmp_path: Path) -> None:
    envelope = build_free_mode_intent_envelope(idea="Compare mechanism A and B", job_id="idea-job")
    assert envelope is not None
    payload = envelope["payload"]
    assert payload["source_kind"] == "idea"
    assert payload["raw_idea"] == "Compare mechanism A and B"
    assert payload["normalized_idea"] == "Compare mechanism A and B"
    assert len(payload["idea_text_sha256"]) == 64
    assert payload["review_intent"]["review_question"] == "Compare mechanism A and B"
    assert envelope["context_hash"]
    writer_context = build_free_mode_writer_context(envelope)
    assert writer_context["raw_idea"] == "Compare mechanism A and B"


def test_free_mode_profile_does_not_invalidate_stage1_reuse(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent",
        pdf_path,
        reader,
        job_id="stage1-free-parent",
    )
    parent_result = parent_service.run(parent_bundle)

    profile_a = _profile_file(tmp_path / "a", research_goal="Explain A")
    profile_b = _profile_file(tmp_path / "b", research_goal="Explain B")
    request_a = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(tmp_path),
        action="analyze",
        free_mode_profile=str(profile_a),
    )
    request_b = JobRunRequest(
        config="config.ini",
        project_name="demo",
        pdf_folder=str(tmp_path),
        action="analyze",
        free_mode_profile=str(profile_b),
    )
    assert JobRunner._request_snapshot(request_a)["free_mode_profile_sha256"] != (
        JobRunner._request_snapshot(request_b)["free_mode_profile_sha256"]
    )

    child_service, child_bundle = _service(
        tmp_path / "child",
        pdf_path,
        reader,
        job_id="stage1-free-child",
    )
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        _typed_manifest_record(parent_service).path
    )
    child_result = child_service.run(child_bundle, existing_summaries=imported)
    assert child_result.reused_count == 1
    assert child_result.generated_count == 0

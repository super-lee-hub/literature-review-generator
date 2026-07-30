from __future__ import annotations

import hashlib
import json
from pathlib import Path

import main as main_module
from free_mode.profile_manager import DEFAULT_PROFILE, build_profile_context
from main import LiteratureReviewGenerator
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_runner import JobRunRequest, JobRunner, validate_job_request_options
from services.job_workspace import JobWorkspace


def _profile_file(tmp_path, text: str):
    profile = dict(DEFAULT_PROFILE)
    profile["generated_prompt"] = text
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return path, profile


def _bound_generator(tmp_path, profile_path):
    workspace = JobWorkspace.create(
        base_output_dir=str(tmp_path / "output"),
        project_name="topic",
        job_id="20260730_010203_abcdef12",
    )
    generator = LiteratureReviewGenerator("config.ini", "topic")
    generator.job_workspace = workspace
    generator.artifact_registry = ArtifactRegistry(
        workspace.paths.registry_path,
        workspace.job_id,
    )
    generator.output_dir = workspace.root_dir
    generator.free_mode_profile_path = str(profile_path)
    generator.config = {
        "Writer_API": {
            "model": "gpt-5.6-sol",
            "provider_family": "openai_responses",
            "max_context_tokens": "100000",
        }
    }
    return generator, workspace


def test_writer_boundary_injects_once_and_persists_request_provenance(tmp_path, monkeypatch):
    profile_path, profile = _profile_file(tmp_path, "FINAL CONTRACT")
    generator, workspace = _bound_generator(tmp_path, profile_path)
    captured_prompts = []

    def fake_call(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return {
            "status": "success",
            "content": "generated review section",
            "finish_reason": "stop",
            "provider_response_id": f"resp-{len(captured_prompts)}",
            "response_model": "gpt-5.6-sol",
            "usage": {"input_tokens": 123},
            "attempt_count": 1,
            "http_attempt_count": 1,
        }

    monkeypatch.setattr(main_module, "_call_ai_api_text_detailed", fake_call)
    api_config = {
        "model": "gpt-5.6-sol",
        "provider_family": "openai_responses",
        "max_context_tokens": "100000",
    }
    context = build_profile_context(profile)

    generator._call_writer_api_text_detailed(
        prompt="BASE PROMPT",
        api_config=api_config,
        system_prompt="SYSTEM",
        max_tokens=1000,
        temperature=0.2,
        writer_stage="review_section",
    )
    generator._call_writer_api_text_detailed(
        prompt=f"{context}ALREADY INJECTED",
        api_config=api_config,
        system_prompt="SYSTEM",
        max_tokens=1000,
        temperature=0.2,
        writer_stage="review_section_continuation",
    )

    assert all(prompt.count(context) == 1 for prompt in captured_prompts)
    provenance_path = Path(
        workspace.artifact_path("topic_writer_prompt_context_provenance_v1.json")
    )
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    expected_context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    assert payload["request_count"] == 2
    assert payload["profile_file_sha256"] == file_sha256(profile_path)
    assert payload["expected_prompt_context_sha256"] == expected_context_hash
    assert payload["all_requests_use_expected_context"] is True
    assert all(item["prompt_context_occurrences"] == 1 for item in payload["requests"])
    assert all(item["prompt_context_sha256"] == expected_context_hash for item in payload["requests"])
    assert all(item["configured_model"] == "gpt-5.6-sol" for item in payload["requests"])
    assert all(item["prompt_budget"]["estimated_input_tokens"] > 0 for item in payload["requests"])

    profile_record = generator.artifact_registry.get(generator.FREE_MODE_PROFILE_ARTIFACT_ID)
    provenance_record = generator.artifact_registry.get(
        generator.WRITER_PROMPT_PROVENANCE_ARTIFACT_ID
    )
    assert profile_record is not None and profile_record.content_hash == file_sha256(profile_path)
    assert provenance_record is not None
    assert provenance_record.depends_on[0].artifact_id == generator.FREE_MODE_PROFILE_ARTIFACT_ID


def test_request_snapshot_hashes_profile_and_rejects_profile_idea_pair(tmp_path):
    profile_path, _profile = _profile_file(tmp_path, "CONTRACT A")
    request = JobRunRequest(
        config="config.ini",
        project_name="topic",
        pdf_folder=None,
        action="generate_outline",
        generate_outline=True,
        free_mode_profile=str(profile_path),
    )
    runner = JobRunner()
    first = runner._request_snapshot(request)
    assert first["free_mode_profile"] == str(profile_path.resolve())
    assert first["free_mode_profile_sha256"] == file_sha256(profile_path)

    profile_path.write_text(profile_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    second = runner._request_snapshot(request)
    assert second["free_mode_profile_sha256"] != first["free_mode_profile_sha256"]

    invalid = JobRunRequest(
        config="config.ini",
        project_name="topic",
        pdf_folder=None,
        action="generate_outline",
        generate_outline=True,
        free_mode_profile=str(profile_path),
        free_mode_idea="duplicate input",
    )
    assert validate_job_request_options(invalid) == (
        "--free-mode-profile and --free-mode-idea are mutually exclusive"
    )

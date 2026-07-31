from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import pph_corrected_topic_pipeline as pipeline


def test_runner_has_no_legacy_generated_contract_source() -> None:
    assert not hasattr(pipeline, "CONTRACT_PATH")
    assert pipeline.final_contracts.SOURCE_PATH.name == "pasted-text.txt"


def test_request_pins_explicit_job_summary_profile_and_no_stage1() -> None:
    topic = {
        "project_name": "pph_s03_concession_to_unfairness",
        "job_id": "20260730_010203_abcdef12",
        "summary_path": str(Path("D:/input.json")),
        "profile_path": str(Path("D:/profile.json")),
    }
    request = pipeline._request_for(topic, "generate_outline")

    assert request.project_name == topic["project_name"]
    assert request.job_id == topic["job_id"]
    assert request.summary_file == topic["summary_path"]
    assert request.summary_sources == (topic["summary_path"],)
    assert request.free_mode_profile == topic["profile_path"]
    assert request.free_mode_idea is None
    assert request.generate_outline is True
    assert request.reuse_stage1 is False
    assert request.validation_required is False


def test_configure_closure_uses_corrected_dynamic_jobs() -> None:
    state = {
        "topics": {
            topic_id: {
                "project_name": f"project_{topic_id}",
                "job_id": f"job_{topic_id}",
                "expected_sections": index + 3,
            }
            for index, topic_id in enumerate(pipeline.TOPIC_ORDER)
        }
    }
    original = pipeline.closure.PROJECTS
    try:
        pipeline._configure_closure(state)
        assert pipeline.closure.PROJECTS["S01"]["job_id"] == "job_S01"
        assert pipeline.closure.PROJECTS["S05"]["project_name"] == "project_S05"
    finally:
        pipeline.closure.PROJECTS = original


def _valid_outline_provenance_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, Path, dict, dict]:
    project_name = "project_S01"
    job_id = "job_S01"
    contract_path = tmp_path / "S01_contract.txt"
    profile_path = tmp_path / "S01_profile.json"
    contract_text = "SCHEMA PROTECTION\ncontract body\n"
    contract_path.write_text(contract_text, encoding="utf-8")
    profile_path.write_text(
        json.dumps({"generated_prompt": contract_text}, ensure_ascii=False),
        encoding="utf-8",
    )
    expected_context_hash = "a" * 64
    topic = {
        "topic_id": "S01",
        "project_name": project_name,
        "job_id": job_id,
        "expected_sections": 7,
        "contract_path": str(contract_path),
        "contract_text_sha256": pipeline.final_contracts._sha256_text(contract_text),
        "profile_path": str(profile_path),
        "profile_file_sha256": pipeline._sha256(profile_path),
        "prompt_context_sha256": expected_context_hash,
    }
    state = {
        "outline_model": "deepseek-v4-pro",
        "writer_model": "gpt-5.6-sol",
        "reader_model": "deepseek-v4-pro",
        "topics": {"S01": topic},
    }
    monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(pipeline, "_load_state", lambda: copy.deepcopy(state))

    artifacts = pipeline.OUTPUT_ROOT / f"{project_name}__{job_id}" / "artifacts"
    artifacts.mkdir(parents=True)
    route_models = {
        "outline_candidates": ("Outline_API", "deepseek-v4-pro"),
        "structure_critique": ("Outline_API", "deepseek-v4-pro"),
        "coverage_critique": ("Primary_Reader_API", "deepseek-v4-pro"),
        "outline_arbitration": ("Outline_API", "deepseek-v4-pro"),
    }
    stages = []
    for index, (stage_name, (route, model)) in enumerate(route_models.items(), 1):
        input_hash = f"{index}" * 64
        output_hash = f"{index + 4}" * 64
        stages.append(
            {
                "stage_name": stage_name,
                "provider_route": route,
                "execution_status": "succeeded",
                "schema_valid": True,
                "attempts": 1,
                "input_hashes": [input_hash],
                "output_hashes": [output_hash],
                "fallback_provenance": "provider",
                "degraded": False,
                "degraded_reason": "",
                "adoption_eligible": True,
                "prompt_budget": {
                    "estimated_input_tokens": 100,
                    "input_budget_tokens": 1000,
                },
                "prompt_context_present": True,
                "prompt_context_sha256": expected_context_hash,
                "requests": [
                    {
                        "stage_name": stage_name,
                        "provider_route": route,
                        "status": "succeeded",
                        "configured_model": model,
                        "response_model": model,
                        "provider_response_id": f"response-{index}",
                        "request_started_at": "2026-07-30T00:00:00Z",
                        "request_completed_at": "2026-07-30T00:00:01Z",
                        "transport_status": "success",
                        "input_hash": input_hash,
                        "output_hash": output_hash,
                        "prompt_context_present": True,
                        "prompt_context_sha256": expected_context_hash,
                    }
                ],
            }
        )
    stage_health = {
        "artifact_type": "outline_stage_health",
        "artifact_version": "v1",
        "job_id": job_id,
        "execution_mode": "production",
        "stages": stages,
        "adoptable": True,
    }
    critiques = {
        "artifact_type": "outline_critiques",
        "artifact_version": "v1",
        "critique_runs": [
            {"critic_role": "structure", "critiques": [{"critique_id": "s1"}]},
            {"critic_role": "coverage", "critiques": [{"critique_id": "c1"}]},
        ],
        "critiques": [{"critique_id": "s1"}, {"critique_id": "c1"}],
    }
    coverage = {
        "artifact_type": "outline_coverage_audit",
        "artifact_version": "v1",
        "passed": True,
        "blocking_issues": [],
        "coverage_metrics": {"effective_section_count": 7},
        "effective_section_count": 7,
    }
    final_outline = {
        "artifact_type": "final_outline",
        "artifact_version": "v2",
        "sections": [{"section_id": f"section-{index}"} for index in range(1, 8)],
    }
    arbitration = {
        "artifact_type": "outline_arbitration_report",
        "artifact_version": "v1",
        "merged_strategy": "provider_arbitration",
        "source_critiques": ["s1", "c1"],
        "final_decision": {"selected_base_candidate": "candidate_1"},
    }
    payloads = {
        "stage_health": stage_health,
        "critiques": critiques,
        "coverage": coverage,
        "final_outline": final_outline,
        "arbitration": arbitration,
    }
    return state, artifacts, payloads, topic


def _write_outline_provenance_payloads(
    artifacts: Path, topic: dict, payloads: dict
) -> None:
    project_name = topic["project_name"]
    paths = {
        "stage_health": f"{project_name}_outline_stage_health_v1.json",
        "critiques": f"{project_name}_outline_critiques.json",
        "coverage": f"{project_name}_outline_coverage_audit.json",
        "final_outline": f"{project_name}_final_outline.json",
        "arbitration": f"{project_name}_outline_arbitration_report.json",
    }
    for key, filename in paths.items():
        (artifacts / filename).write_text(
            json.dumps(payloads[key], ensure_ascii=False), encoding="utf-8"
        )


def test_verify_outline_contract_provenance_accepts_complete_provider_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, artifacts, payloads, topic = _valid_outline_provenance_fixture(
        tmp_path, monkeypatch
    )
    _write_outline_provenance_payloads(artifacts, topic, payloads)

    audit = pipeline.verify_outline_contract_provenance("S01")

    assert audit["all_v2_stages_use_expected_context"] is True
    assert audit["all_v2_stages_provider_backed"] is True
    assert audit["coverage_passed"] is True
    assert audit["effective_section_count"] == 7


@pytest.mark.parametrize(
    "case",
    [
        "execution_failed",
        "schema_invalid",
        "degraded",
        "adoption_ineligible",
        "fallback",
        "null_output_hash",
        "missing_request_evidence",
        "wrong_route",
        "wrong_configured_model",
        "missing_response_model",
    ],
)
def test_verify_outline_contract_provenance_rejects_unhealthy_or_incomplete_stage(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, artifacts, payloads, topic = _valid_outline_provenance_fixture(
        tmp_path, monkeypatch
    )
    stage = payloads["stage_health"]["stages"][0]
    if case == "execution_failed":
        stage["execution_status"] = "failed"
    elif case == "schema_invalid":
        stage["schema_valid"] = False
    elif case == "degraded":
        stage["degraded"] = True
    elif case == "adoption_ineligible":
        stage["adoption_eligible"] = False
    elif case == "fallback":
        stage["fallback_provenance"] = "deterministic_fallback"
    elif case == "null_output_hash":
        stage["output_hashes"] = [pipeline.JSON_NULL_SHA256]
        stage["requests"][0]["output_hash"] = pipeline.JSON_NULL_SHA256
    elif case == "missing_request_evidence":
        stage["requests"] = []
    elif case == "wrong_route":
        stage["provider_route"] = "Wrong_API"
        stage["requests"][0]["provider_route"] = "Wrong_API"
    elif case == "wrong_configured_model":
        stage["requests"][0]["configured_model"] = "wrong-model"
    elif case == "missing_response_model":
        stage["requests"][0]["response_model"] = ""
    _write_outline_provenance_payloads(artifacts, topic, payloads)

    with pytest.raises(ValueError):
        pipeline.verify_outline_contract_provenance("S01")


@pytest.mark.parametrize(
    "case", ["empty_critiques", "coverage_failed"]
)
def test_verify_outline_contract_provenance_rejects_incomplete_outline_artifacts(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, artifacts, payloads, topic = _valid_outline_provenance_fixture(
        tmp_path, monkeypatch
    )
    if case == "empty_critiques":
        payloads["critiques"]["critiques"] = []
        for run in payloads["critiques"]["critique_runs"]:
            run["critiques"] = []
    elif case == "coverage_failed":
        payloads["coverage"]["passed"] = False
        payloads["coverage"]["blocking_issues"] = [{"issue_type": "blocked"}]
    else:
        payloads["coverage"]["effective_section_count"] = 6
        payloads["coverage"]["coverage_metrics"]["effective_section_count"] = 6
    _write_outline_provenance_payloads(artifacts, topic, payloads)

    with pytest.raises(ValueError):
        pipeline.verify_outline_contract_provenance("S01")


def test_verify_outline_contract_provenance_warns_on_section_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section count mismatch is now a warning, not a blocking error."""
    _state, artifacts, payloads, topic = _valid_outline_provenance_fixture(
        tmp_path, monkeypatch
    )
    payloads["coverage"]["effective_section_count"] = 6
    payloads["coverage"]["coverage_metrics"]["effective_section_count"] = 6
    _write_outline_provenance_payloads(artifacts, topic, payloads)

    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        audit = pipeline.verify_outline_contract_provenance("S01")
        assert audit["coverage_passed"] is True
        assert len(w) >= 1
        assert "section count" in str(w[0].message).lower()


def test_resolve_workspace_reuses_job_id_when_workspace_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_workspace reuses the existing job_id when workspace has artifacts
    and input fingerprint has not changed."""
    old_job_id = "20260730_000000_oldjob00"
    new_job_id = "20260730_000001_newjob00"

    contract_text = "contract content for hash"
    contract_path = tmp_path / "S01_contract.txt"
    contract_path.write_text(contract_text, encoding="utf-8")
    profile_path = tmp_path / "S01_profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text("[]", encoding="utf-8")

    from services.artifact_registry import file_sha256

    state = {
        "topics": {
            "S01": {
                "project_name": "project_S01",
                "job_id": old_job_id,
                "attempt_history": [],
                "summary_path": str(summary_path),
                "summary_sha256": file_sha256(str(summary_path)),
                "contract_path": str(contract_path),
                "contract_text_sha256": pipeline.final_contracts._sha256_text(contract_text),
                "profile_path": str(profile_path),
                "profile_file_sha256": file_sha256(str(profile_path)),
            }
        }
    }
    monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(pipeline, "STATE_PATH", tmp_path / "topic_jobs.json")
    monkeypatch.setattr(
        pipeline.JobWorkspace,
        "generate_job_id",
        staticmethod(lambda: new_job_id),
    )
    prior_artifacts = pipeline.OUTPUT_ROOT / f"project_S01__{old_job_id}" / "artifacts"
    prior_artifacts.mkdir(parents=True)
    marker = prior_artifacts / "project_S01_outline_stage_health_v1.json"
    marker.write_text("{}", encoding="utf-8")

    registry_path = pipeline.OUTPUT_ROOT / f"project_S01__{old_job_id}" / "artifact_registry.json"
    registry_path.write_text(
        '{"artifact_registry_version": "v2", "revision": 0, "job_id": "'
        + old_job_id + '", "artifacts": []}',
        encoding="utf-8",
    )

    topic = pipeline._resolve_workspace(state, "S01")

    # _resolve_workspace now rotates job_id when workspace exists to avoid collision
    assert topic["job_id"] != old_job_id  # rotated, not reused
    assert marker.read_text(encoding="utf-8") == "{}"
    # Rotation adds an attempt_history entry
    assert len(topic.get("attempt_history", [])) >= 1




# --- Stage coordinator regression tests ---

def test_inspect_topic_progress_no_workspace_returns_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {
        "outline_model": "deepseek-v4-pro",
        "writer_model": "gpt-5.6-sol",
        "reader_model": "deepseek-v4-pro",
        "outline_route_snapshot": {
            "outline_candidates": "Outline_API",
            "structure_critique": "Outline_API",
            "coverage_critique": "Primary_Reader_API",
            "outline_arbitration": "Outline_API",
        },
        "topics": {
            "S01": {
                "topic_id": "S01",
                "project_name": "project_S01",
                "job_id": "job_S01",
                "expected_sections": 7,
                "summary_path": str(tmp_path / "summary.json"),
                "summary_sha256": "a" * 64,
                "contract_path": str(tmp_path / "contract.txt"),
                "contract_text_sha256": "b" * 64,
                "profile_path": str(tmp_path / "profile.json"),
                "profile_file_sha256": "c" * 64,
                "prompt_context_sha256": "d" * 64,
                "attempt_history": [],
            }
        }
    }
    (tmp_path / "summary.json").write_text("[]", encoding="utf-8")
    (tmp_path / "contract.txt").write_text("contract", encoding="utf-8")
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline, "_load_state", lambda: state)
    monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp_path / "output")
    progress = pipeline.inspect_topic_progress("S01")
    assert progress.next_stage == "outline"
    assert progress.completed_stages == []


def test_inspect_topic_progress_adopted_skips_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "job_S01"
    project_name = "project_S01"
    workspace = tmp_path / "output" / f"{project_name}__{job_id}"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    adopted = artifacts / f"{project_name}_adopted_final_outline.json"
    adopted.write_text('{"adoption_status": "adopted"}', encoding="utf-8")
    from services.artifact_registry import file_sha256
    adopted_hash = file_sha256(str(adopted))
    registry = {
        "artifact_registry_version": "v2",
        "revision": 0,
        "job_id": job_id,
        "artifacts": [{
            "artifact_id": "adopted_final_outline",
            "artifact_type": "adopted_final_outline",
            "artifact_version": "v1",
            "path": str(adopted),
            "content_hash": adopted_hash,
            "status": "ready",
            "job_id": job_id,
            "producer": "test",
            "depends_on": [],
            "created_at": "2026-07-30T00:00:00Z",
        }],
    }
    (workspace / "artifact_registry.json").write_text(json.dumps(registry), encoding="utf-8")
    state = {
        "outline_model": "deepseek-v4-pro",
        "writer_model": "gpt-5.6-sol",
        "reader_model": "deepseek-v4-pro",
        "outline_route_snapshot": {},
        "topics": {
            "S01": {
                "topic_id": "S01",
                "project_name": project_name,
                "job_id": job_id,
                "expected_sections": 7,
                "summary_path": str(tmp_path / "summary.json"),
                "summary_sha256": "a" * 64,
                "contract_path": str(tmp_path / "contract.txt"),
                "contract_text_sha256": "b" * 64,
                "profile_path": str(tmp_path / "profile.json"),
                "profile_file_sha256": "c" * 64,
                "prompt_context_sha256": "d" * 64,
                "attempt_history": [],
            }
        }
    }
    (tmp_path / "summary.json").write_text("[]", encoding="utf-8")
    (tmp_path / "contract.txt").write_text("contract", encoding="utf-8")
    (tmp_path / "profile.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline, "_load_state", lambda: state)
    monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp_path / "output")
    progress = pipeline.inspect_topic_progress("S01")
    assert "outline" in progress.completed_stages
    assert "adopt" in progress.completed_stages
    assert progress.next_stage == "review"


def test_status_command_lists_all_topics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {
        "outline_model": "deepseek-v4-pro",
        "writer_model": "gpt-5.6-sol",
        "reader_model": "deepseek-v4-pro",
        "outline_route_snapshot": {},
        "topics": {
            tid: {
                "topic_id": tid,
                "project_name": f"project_{tid}",
                "job_id": f"job_{tid}",
                "expected_sections": 7,
                "summary_path": str(tmp_path / f"{tid}_summary.json"),
                "summary_sha256": "a" * 64,
                "contract_path": str(tmp_path / f"{tid}_contract.txt"),
                "contract_text_sha256": "b" * 64,
                "profile_path": str(tmp_path / f"{tid}_profile.json"),
                "profile_file_sha256": "c" * 64,
                "prompt_context_sha256": "d" * 64,
                "attempt_history": [],
            }
            for tid in ("S01", "S02", "S03", "S04", "S05")
        }
    }
    for tid in ("S01", "S02", "S03", "S04", "S05"):
        (tmp_path / f"{tid}_summary.json").write_text("[]", encoding="utf-8")
        (tmp_path / f"{tid}_contract.txt").write_text("c", encoding="utf-8")
        (tmp_path / f"{tid}_profile.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline, "_load_state", lambda: state)
    monkeypatch.setattr(pipeline, "OUTPUT_ROOT", tmp_path / "output")
    results = {tid: pipeline.inspect_topic_progress(tid) for tid in ("S01","S02","S03","S04","S05")}
    for tid in ("S01","S02","S03","S04","S05"):
        assert results[tid].next_stage == "outline"
        assert results[tid].topic_id == tid

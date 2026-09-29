from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import time
from dataclasses import replace

from reviewctl import main as reviewctl_main
from runtime.control_plane import ReviewControlPlane
from runtime.outline_v3_dag import OutlineNodeStore
from services.artifact_registry import ArtifactRegistry
from services.credential_provenance import PREPROCESS_ENV_MAPPING
from services.job_workspace import JobWorkspace
from tests.test_outline_v3_semantic_execution import _summary


def _spec(tmp_path: Path):
    from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec

    pdf_folder = tmp_path / "pdfs"
    pdf_folder.mkdir()
    (pdf_folder / "paper.pdf").write_bytes(b"%PDF-1.4\n% synthetic fixture\n")
    config = tmp_path / "config.ini"
    config.write_text(
        """[Paths]\noutput_path = {output}\n\n[Primary_Reader_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n\n[Backup_Reader_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n\n[Writer_API]\napi_key = dummy\nmodel = test\napi_base = https://example.test\n""".format(output=tmp_path / "output"),
        encoding="utf-8",
    )
    return RuntimeJobSpec(
        project_name="reviewctl",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        config=str(config),
        action="analyze",
        metadata={"requested_stages": ["analyze"]},
    )


def test_reviewctl_plan_and_doctor_emit_machine_json(tmp_path: Path, capsys) -> None:
    spec = _spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    assert reviewctl_main(["plan", "--spec", str(spec_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "planned"
    assert plan["stages"] == ["source_intake", "analyze"]

    assert reviewctl_main(["doctor", "--repo-root", str(tmp_path), "--config", str(tmp_path / "missing.ini")]) == 1
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["status"] == "fail"
    assert "dummy" not in json.dumps(doctor)


def test_reviewctl_plan_projects_all_reachable_provider_work_without_posting(
    tmp_path: Path, capsys,
) -> None:
    spec = _spec(tmp_path)
    config = Path(spec.config)
    parser = configparser.ConfigParser()
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in ("Primary_Reader_API", "Backup_Reader_API"):
        parser[section]["api_key"] = "local-fixture-only"
        parser[section]["api_base"] = "http://127.0.0.1:1/v1"
    with config.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    assert reviewctl_main(["plan", "--spec", str(spec_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    projection = plan["full_stage_request_plan"]
    assert projection["schema_version"] == "full-stage-provider-request-plan-v1"
    assert projection["limits"]["effective_provider_call_limit"] == 24
    assert projection["totals"]["unknown_exposure_count"] >= 1
    assert projection["budget_status"]["admission"] == "incomplete_unknown_exposure"
    assert projection["boundary"]["no_provider_posts"] is True
    exposure = projection["unknown_exposures"][0]
    assert exposure["logical_calls_upper_bound"] is None
    assert exposure["input_tokens_per_call_upper_bound"] > 0
    assert exposure["output_tokens_per_call_upper_bound"] > 0
    assert exposure["reasoning_tokens_per_call_upper_bound"] >= 0
    assert exposure["retry_attempts_per_call_upper_bound"] >= 0
    assert projection["totals"]["estimated_output_tokens_all_attempts"] is None
    assert all(
        item["logical_calls_upper_bound"] is None
        and isinstance(item["input_tokens_per_call_upper_bound"], int)
        and isinstance(item["output_tokens_per_call_upper_bound"], int)
        and isinstance(item["reasoning_tokens_per_call_upper_bound"], int)
        and isinstance(item["retry_attempts_per_call_upper_bound"], int)
        for item in projection["unknown_exposures"]
    )
    assert projection["aggregate_budget_source"] == (
        "application_call_cap_projection_without_bound_acceptance_run"
    )
    assert plan["provider_admission_status"] == "incomplete_unknown_exposure"
    assert plan["ready_for_provider_admission"] is False
    assert "local-fixture-only" not in json.dumps(plan)


def test_chunk_plan_loads_summary_source_manifest_and_marks_navigation_scope(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps(
            [
                _summary("paper-a", "A", "A finding."),
                _summary("paper-b", "B", "B finding."),
            ]
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "summary-source-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "summary_source_manifest",
                "artifact_version": "v2",
                "created_at": "2026-09-26T00:00:00Z",
                "project_name": "chunk-plan-test",
                "source_kind": "runtime_summary_source",
                "source_path": str(summary_path),
                "source_items": [],
                "rejected_candidates": [],
                "materialized_summary_file": summary_path.name,
                "summary_count": 2,
            }
        ),
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan([manifest_path])

    assert result["source_summary_count"] == 2
    assert result["provider_posts_emitted"] == 0
    assert result["provider_call_budget_status"] == "NOT_PLANNED_END_TO_END"
    assert result["provider_request_plan_status"] == "not_planned_config_missing"
    assert result["semantic_chunk_plan"]["budgets"]["within_physical_call_limit"] is None
    assert result["r1_request_workload_audit"]["source_scope"] == (
        "provider_free_materialized_summary_projection"
    )
    assert result["r1_request_workload_audit"]["typed_manifest_authority_count"] == 0
    workload = result["r1_request_workload_audit"]
    assert workload["study_unit_count"] == 2
    assert workload["study_unit_count_kind"] == "explicit_study_units_plus_paper_level_fallbacks"
    assert workload["paper_level_fallback_unit_count"] == 2
    assert workload["explicit_study_unit_count"] == 0
    assert workload["study_level_source_claim_count"] == 0
    assert workload["paper_level_source_claim_count"] == workload["source_claim_count_unique_by_paper"]
    assert workload["source_field_ledger_total"] == sum(
        workload["source_field_ledger_scope_counts"].values()
    )
    assert workload["unresolved_source_field_count"] == workload[
        "source_field_ledger_scope_counts"
    ].get("unresolved", 0)


def test_chunk_plan_distinguishes_an_explicit_study_from_paper_fallback(
    tmp_path: Path,
) -> None:
    explicit = _summary("paper-b", "B", "A within-study finding.")
    explicit["specialized_details"]["empirical"]["study_results"] = [{
        "study_id": "study:1",
        "findings": "A within-study finding.",
        "method": "A controlled comparison.",
    }]
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps([
            _summary("paper-a", "A", "A paper-level finding."),
            explicit,
        ]),
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan([summary_path])

    workload = result["r1_request_workload_audit"]
    assert workload["study_unit_count"] == 2
    assert workload["paper_level_fallback_unit_count"] == 1
    assert workload["explicit_study_unit_count"] == 1


def test_chunk_plan_builds_route_bound_semantic_request_plan_without_posts(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summaries.json"
    summary_path.write_text(
        json.dumps(
            [
                _summary("paper-a", "A", "A finding."),
                _summary("paper-b", "B", "B finding."),
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Outline_API]\n"
        "provider_family = anthropic\n"
        "model = claude-opus-5\n"
        "endpoint_type = anthropic\n"
        "api_base = https://api.example.test\n"
        "max_context_tokens = 32000\n"
        "max_output_tokens = 4096\n\n"
        "[OutlineModels]\n"
            "outline_model = Outline_API\n"
            "relation_adjudicator_model = Outline_API\n"
            "structure_critic_model = Outline_API\n"
            "coverage_critic_model = Outline_API\n"
            "evidence_critic_model = Outline_API\n"
            "arbitrator_model = Outline_API\n\n"
        "[Outline]\n"
        "candidate_count = 2\n\n"
        "[OutlineStability]\n"
        "mode = off\n"
        "max_provider_calls = 24\n"
        "max_source_prompt_tokens = 32000\n",
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path],
        config_path=config_path,
    )

    assert result["provider_posts_emitted"] == 0
    assert result["provider_request_plan_status"] == "planned_semantic_request_graph"
    assert result["semantic_request_plan"]
    assert all(
        "request_hash" in item
        for item in result["semantic_request_plan"]
        if "batch_id" in item
    )
    assert all(
        item["physical_attempt_upper_bound"] == 3
        for item in result["semantic_request_plan"]
    )
    assert result["semantic_request_physical_attempts_upper_bound"] is not None
    assert result["end_to_end_provider_budget_status"] == "NOT_PLANNED"
    assert result["topic_request_plan_identity_hash"]
    assert result["r1_request_workload_audit"]["topic_request_plan_identity_hash"] == result[
        "topic_request_plan_identity_hash"
    ]
    coverage = result["r1_request_workload_audit"]["topic_wire_coverage"]
    assert coverage["status"] == "complete"
    assert coverage["all_planned_unit_sets_equal_materialized_unit_sets"] is True
    assert coverage["missing_source_claim_identity_count"] == 0
    assert coverage["missing_source_evidence_identity_count"] == 0
    assert coverage["extra_topic_wire_claim_identity_count"] == 0
    assert coverage["extra_topic_wire_evidence_identity_count"] == 0
    text_metrics = result["r1_request_workload_audit"]["topic_wire_text"]
    assert text_metrics["request_count"] > 0
    assert text_metrics["source_text_occurrence_count"] >= text_metrics[
        "unique_source_text_identity_count_across_requests"
    ]
    assert text_metrics["repeated_occurrence_count_total"] == (
        text_metrics["source_text_occurrence_count"]
        - text_metrics["unique_source_text_identity_count_across_requests"]
    )
    assert "topic_merge_policy" in result["r1_request_workload_audit"]
    assert "source_text_identity_records" not in json.dumps(result)

    baseline_topic = next(
        row for row in result["semantic_request_plan"]
        if str(row.get("node_id") or "").startswith("topic_synthesis_provider:batch:")
    )
    updated_config = config_path.read_text(encoding="utf-8").replace(
        "max_output_tokens = 4096", "max_output_tokens = 16384"
    ).replace(
        "candidate_count = 2", "candidate_count = 2\nsemantic_output_max_tokens = 8192"
    )
    config_path.write_text(updated_config, encoding="utf-8")
    increased = ReviewControlPlane(repo_root=tmp_path).chunk_plan(
        [summary_path], config_path=config_path
    )
    increased_topic = next(
        row for row in increased["semantic_request_plan"]
        if row["node_id"] == baseline_topic["node_id"]
    )
    assert baseline_topic["estimated_output_tokens"] == 4_096
    assert increased_topic["estimated_output_tokens"] == 8_192
    assert increased_topic["request_hash"] != baseline_topic["request_hash"]
    assert increased["provider_posts_emitted"] == 0


def test_config_migrate_cli_preserves_route_conflict_and_redacts_values(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "legacy-route.ini"
    original = (
        "[Backup_Reader_API]\nmodel = backup-model\napi_key = backup-secret-placeholder\n"
        "[Outline_GPT_API]\nmodel = old-model\napi_key = old-secret-placeholder\n"
        "[OutlineModels]\nstructure_critic_model = Outline_GPT_API\n"
    )
    config_path.write_text(original, encoding="utf-8")

    assert reviewctl_main([
        "config-migrate",
        "--config",
        str(config_path),
        "--dry-run",
    ]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "model" in payload["error"]
    assert "api_key" in payload["error"]
    assert "backup-secret-placeholder" not in json.dumps(payload)
    assert "old-secret-placeholder" not in json.dumps(payload)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.backup_before_*")) == []


def test_reviewctl_run_domain_failure_is_machine_json(tmp_path: Path, capsys) -> None:
    spec = _spec(tmp_path)
    spec = replace(spec, config=str(tmp_path / "missing.ini"))
    spec_path = tmp_path / "missing-config-spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    assert reviewctl_main(["run", "--spec", str(spec_path)]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "error"
    assert payload["error_type"] == "RuntimeRunnerError"
    assert "Traceback" not in output


def test_doctor_does_not_call_unlocked_persistent_lock_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "queue.json.lock"
    lock_path.write_text("persistent queue lock\n", encoding="utf-8")
    old = time.time() - 24 * 60 * 60
    os.utime(lock_path, (old, old))

    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])

    assert control._stale_locks(tmp_path) == []


def test_doctor_excludes_dependency_lock_from_runtime_lock_diagnostics(tmp_path: Path) -> None:
    lock_path = tmp_path / "requirements-py311-windows.lock"
    lock_path.write_text("package==1.0\n", encoding="utf-8")
    old = time.time() - 24 * 60 * 60
    os.utime(lock_path, (old, old))

    control = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path])

    assert control._stale_locks(tmp_path) == []


def test_doctor_fails_for_configured_missing_certificate(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        (Path(__file__).resolve().parents[1] / "config.ini.example").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    missing_certificate = tmp_path / "missing-ca.pem"
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(missing_certificate))

    result = ReviewControlPlane(repo_root=tmp_path).doctor(config_path=config_path)

    certificate = next(
        check for check in result["checks"] if check["name"] == "certificate_paths"
    )
    assert certificate["status"] == "fail"
    assert certificate["details"]["valid"] is False
    assert result["ok"] is False


def _clear_preprocess_environment(monkeypatch) -> None:
    for env_name in PREPROCESS_ENV_MAPPING:
        monkeypatch.delenv(env_name, raising=False)


def _example_config(path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    path.write_text(
        re.sub(
            r"(?im)^(api_key\s*=\s*).*$",
            r"\1test-provider-key",
            source.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def test_doctor_reports_mineru_remote_fallback_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_preprocess_environment(monkeypatch)
    config_path = tmp_path / "config.ini"
    _example_config(config_path)

    result = ReviewControlPlane(repo_root=tmp_path).doctor(config_path=config_path)

    admission = next(
        check for check in result["checks"] if check["name"] == "mineru_remote_admission"
    )
    assert admission["status"] == "warn"
    assert admission["details"] == {
        "status": "warn",
        "remote_requested": True,
        "token_present": False,
        "fallback_will_be_used": True,
        "parser_mode": "hybrid",
        "primary_parser": "mineru_remote",
        "fallback_parser": "local",
        "network_probe": False,
        "reason": "remote_parser_admission_failed",
        "error_type": "ValueError",
    }
    assert result["provider_network_calls"] == 0


def test_provider_preflight_fails_when_remote_mineru_has_no_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_preprocess_environment(monkeypatch)
    config_path = tmp_path / "config.ini"
    _example_config(config_path)
    (tmp_path / ".env").write_text(
        "ALLOW_LOCAL_PARSE_FALLBACK=false\n",
        encoding="utf-8",
    )

    result = ReviewControlPlane(repo_root=tmp_path).provider_preflight(
        config_path=config_path,
        action="analyze",
    )

    assert result["status"] == "fail"
    assert result["ok"] is False
    admission = result["mineru_remote_admission"]
    assert admission["remote_requested"] is True
    assert admission["token_present"] is False
    assert admission["fallback_will_be_used"] is False
    assert admission["network_probe"] is False
    assert result["network_calls"] == 0


def test_control_plane_retry_node_uses_persisted_outline_v3_scope(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "review", "job-v3-control")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    store = OutlineNodeStore(workspace, registry)
    store.ensure(workspace.job_id, candidate_count=3)
    store.record_node("structure_critique", status="failed", diagnostics=["synthetic_failure"])

    response = ReviewControlPlane(repo_root=tmp_path, workspace_roots=[tmp_path]).retry_node(
        workspace=workspace.root_dir,
        node_id="structure_critique",
    )

    assert response["status"] == "planned"
    assert response["mutation_performed"] is True
    assert response["resume_required"] is True
    assert "structure_critique" in response["resume_plan"]["rerun_node_ids"]
    assert "candidate_1" not in response["resume_plan"]["rerun_node_ids"]
    assert response["read_only"] is False

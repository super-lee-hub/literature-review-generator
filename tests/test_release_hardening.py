from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import fitz  # type: ignore

from config_loader import load_config
from free_mode.profile_manager import get_profile_path, save_profile
from runtime.job_spec import RuntimeJobSpec
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderCallReceiptV1,
    ProviderBudgetExceeded,
    ProviderBudgetController,
    ProviderRuntime,
    ProviderRuntimeLedger,
    bind_acceptance_execution_context,
    is_process_alive,
    process_identity_for_pid,
)
from runtime.release_acceptance import (
    AcceptanceScenarioContextV1,
    GateEvidenceProducer,
    GateEvidenceVerifier,
    GateKScenario,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
    gate_evidence_roles,
    scenario_for_gate,
    validate_gate_evidence,
)
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.zotero_attachment_resolver import (
    ZoteroAttachmentIndex,
    ZoteroAttachmentResolutionError,
)
from services.job_workspace import JobWorkspace, WorkspacePathError
from preprocess.service import MineruArtifactLimitError, PreprocessManager
from rag.local_rag import LocalRAGIndex
from validation.evidence_loader import (
    PreprocessEvidenceLoader,
    ValidationSourceAuthorityError,
)


def _write_config(path: Path, values: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, items in values.items():
        parser[section] = items
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _runtime_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_name": "demo",
        "source": {"mode": "direct", "pdf_folder": "papers"},
    }
    payload.update(overrides)
    return payload


def _scenario_receipt_ref(
    tmp_path: Path,
    producer: GateEvidenceProducer,
    *,
    gate: str,
    final_sha: str,
    acceptance_run_id: str,
    job_id: str = "",
) -> dict[str, object]:
    receipt_path = tmp_path / f"scenario-execution-receipt-{gate}.json"
    receipt_path.write_text(
        json.dumps(
            {
                "artifact_type": "scenario_execution_receipt",
                "artifact_version": "v1",
                "schema_version": "scenario-execution-receipt-v1",
                "parent_acceptance_run_id": acceptance_run_id,
                "scenario_id": gate,
                "gate": gate,
                "final_executable_sha": final_sha,
                "plan_sha256": "a" * 64,
                "runtime_spec_sha256": "b" * 64,
                "input_identity_sha256": "c" * 64,
                "workspace_identity_sha256": "d" * 64,
                "executor_pid": os.getpid(),
                "executor_process_creation_identity": "test-process",
                "executor_host_id": "test-host",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:01Z",
                "action_type": f"gate-{gate.lower()}-test",
                "workspace": str(tmp_path),
                "job_id": job_id or f"job-{gate.lower()}",
                "attempt_id": f"attempt-{gate.lower()}",
                "budget_domain": "live",
                "status": "PASSED",
                "exit_status": 0,
                "produced_evidence_refs": [],
            }
        ),
        encoding="utf-8",
    )
    return producer.reference(
        receipt_path,
        role="scenario_execution_receipt",
        artifact_type="scenario_execution_receipt",
        artifact_version="v1",
        schema_version="scenario-execution-receipt-v1",
        job_id=job_id,
    )


def test_runtime_job_spec_rejects_unknown_top_level_and_nested_keys() -> None:
    with pytest.raises(ValueError, match="pdf_fodler"):
        RuntimeJobSpec.from_dict(_runtime_payload(pdf_fodler="papers"))
    with pytest.raises(ValueError, match="launch-missiles"):
        RuntimeJobSpec.from_dict(
            _runtime_payload(metadata={"launch-missiles": True})
        )
    with pytest.raises(ValueError, match="source.*unknown"):
        RuntimeJobSpec.from_dict(
            _runtime_payload(source={"mode": "direct", "pdf_folder": "papers", "unknown": 1})
        )


def test_runtime_job_spec_flat_mapping_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        RuntimeJobSpec.from_mapping(
            {
                "project_name": "demo",
                "source_mode": "direct",
                "pdf_folder": "papers",
                "unexpected": "value",
            }
        )


def test_stage_plan_config_admission_does_not_require_unreachable_sections(tmp_path: Path) -> None:
    config_path = tmp_path / "minimal.ini"
    _write_config(
        config_path,
        {
            "Application": {"config_schema": "4"},
            "Paths": {"output_path": str(tmp_path / "output")},
            "Primary_Reader_API": {
                "api_key": "sk-primary-reader",
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "endpoint_type": "chat_completions",
                "provider_family": "deepseek",
            },
            "Stage1_Input": {"primary_reader_only": "true"},
        },
    )

    loaded = load_config(
        str(config_path),
        action="analyze",
        requested_stages=("analyze",),
    )

    assert "Backup_Reader_API" not in loaded
    assert "Writer_API" not in loaded
    assert "Outline_API" not in loaded
    assert "OutlineModels" not in loaded


def test_reachable_provider_route_plan_expands_enabled_outline_semantic_routes() -> None:
    config = {
        "Application": {"config_schema": "4"},
        "Paths": {"output_path": "output"},
        "Primary_Reader_API": {
            "api_key": "sk-primary-reader",
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
        },
        "Backup_Reader_API": {
            "api_key": "sk-backup-reader",
            "model": "deepseek-v4-flash",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
        },
        "Writer_API": {
            "api_key": "sk-writer-api",
            "model": "gpt-5.6-sol",
            "api_base": "https://writer.example.test/v1",
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
        },
        "Outline_API": {
            "api_key": "sk-outline-api",
            "model": "claude-opus-5",
            "api_base": "https://outline.example.test",
            "endpoint_type": "anthropic",
            "provider_family": "anthropic",
        },
        "Free_Mode_API": {
            "api_key": "sk-free-mode-api",
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
        },
        "Validator_API": {
            "api_key": "sk-validator-api",
            "model": "deepseek-v4-flash",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
        },
        "Outline": {
            "relation_adjudication_enabled": "true",
            "structure_critique_enabled": "true",
            "coverage_critique_enabled": "false",
            "evidence_critique_enabled": "true",
        },
        "OutlineModels": {
            "outline_model": "Outline_API",
            "relation_adjudicator_model": "Free_Mode_API",
            "structure_critic_model": "Writer_API",
            "coverage_critic_model": "Free_Mode_API",
            "evidence_critic_model": "Writer_API",
            "arbitrator_model": "Outline_API",
        },
        "Validation": {"review_enabled": "false"},
    }

    plan = build_reachable_provider_route_plan(
        config,
        action="generate_outline",
        requested_stages=None,
    )

    assert plan.required_provider_sections == (
        "Outline_API",
        "Free_Mode_API",
        "Writer_API",
    )
    assert {route.semantic_role for route in plan.routes} == {
        "candidate_provider_generation",
        "relation_adjudication",
        "structure_critique",
        "evidence_critique",
        "arbitration",
    }
    assert "coverage_critique" not in plan.semantic_roles
    assert len(plan.physical_routes) == 3


def test_stage_plan_config_admission_requires_reachable_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "needs-backup.ini"
    _write_config(
        config_path,
        {
            "Application": {"config_schema": "4"},
            "Paths": {"output_path": str(tmp_path / "output")},
            "Primary_Reader_API": {
                "api_key": "sk-primary-reader",
                "model": "deepseek-v4-pro",
                "api_base": "https://api.deepseek.com",
                "endpoint_type": "chat_completions",
                "provider_family": "deepseek",
            },
            "Stage1_Input": {"primary_reader_only": "false"},
        },
    )

    with pytest.raises(configparser.Error, match="Backup_Reader_API"):
        load_config(str(config_path), action="analyze", requested_stages=("analyze",))


def test_specialized_gate_never_passes_from_completed_job_alone(tmp_path: Path, monkeypatch) -> None:
    from scripts import release_acceptance

    spec = tmp_path / "runtime.json"
    spec.write_text(json.dumps(_runtime_payload()), encoding="utf-8")
    args = SimpleNamespace(
        timeout_seconds=30,
        max_provider_calls=24,
        max_output_tokens=5000000,
        max_retry_attempts=2,
    )
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    monkeypatch.setattr(
        release_acceptance,
        "_command",
        lambda *_args, **_kwargs: {
            "status": "PASS",
            "job_status": "completed",
            "completion_status": "complete",
        },
    )

    result = release_acceptance._live_gate(
        tmp_path,
        gate="I",
        spec=spec,
        args=args,
        preflight={"status": "PASS"},
    )

    assert result["status"] != "PASS"
    assert "evidence" in str(result.get("reason", "")).lower() or result["status"] == "NOT_VERIFIED"


def test_acceptance_does_not_trust_handwritten_gate_facts() -> None:
    result = validate_gate_evidence(
        "C",
        {
            "final_sha": "a" * 40,
            "source_count": 1,
            "canonical_stage1_count": 1,
            "actual_transport_calls": 1,
            "closure_complete": True,
        },
        expected_final_sha="a" * 40,
    )
    assert result["status"] == "NOT_VERIFIED"
    assert "durable" in str(result["reason"]).lower()


def test_gate_evidence_verifier_rejects_evidence_from_another_acceptance_run() -> None:
    evidence = GateEvidenceProducer(final_sha="a" * 40).build_gate(
        "C",
        [],
        acceptance_run_id="old-acceptance-run",
    )

    result = GateEvidenceVerifier().verify(
        "C",
        evidence,
        expected_final_sha="a" * 40,
        expected_acceptance_run_id="current-acceptance-run",
    )

    assert result["status"] == "NOT_VERIFIED"
    assert "different acceptance run" in str(result["reason"])


def test_gate_evidence_verifier_rejects_unbound_metadata_even_with_durable_refs(tmp_path: Path) -> None:
    trace = tmp_path / "playwright_trace.json"
    browser = tmp_path / "browser_evidence.json"
    trace.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    browser.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="c" * 40)
    evidence = producer.build_gate(
        "I",
        [
            producer.reference(trace, role="playwright_trace"),
            producer.reference(browser, role="browser_evidence"),
        ],
    )
    evidence.pop("producer")
    evidence["actual_transport_calls"] = 99

    result = GateEvidenceVerifier().verify(
        "I",
        evidence,
        expected_final_sha="c" * 40,
    )

    assert result["status"] == "NOT_VERIFIED"
    assert "producer" in str(result["reason"]).lower()


def test_gate_evidence_verifier_reopens_hashed_browser_artifacts(tmp_path: Path) -> None:
    trace = tmp_path / "playwright_trace.json"
    browser = tmp_path / "browser_evidence.json"
    trace.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    browser.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="b" * 40)
    evidence = producer.build_gate(
        "I",
        [
            producer.reference(trace, role="playwright_trace"),
            producer.reference(browser, role="browser_evidence"),
        ],
    )
    result = GateEvidenceVerifier().verify(
        "I",
        evidence,
        expected_final_sha="b" * 40,
    )
    assert result["status"] != "PASS"
    assert "Playwright" in str(result["reason"])


def test_gate_k_rejects_fake_process_ids_and_lock_file(tmp_path: Path) -> None:
    process_events = tmp_path / "process-events.jsonl"
    lock_state = tmp_path / "lock-state.json"
    scenario_receipt = tmp_path / "scenario-execution-receipt.json"
    process_events.write_text(
        json.dumps({"pid": 1111, "event": "started"})
        + "\n"
        + json.dumps({"pid": 2222, "event": "started"})
        + "\n",
        encoding="utf-8",
    )
    lock_state.write_text(json.dumps({"status": "locked"}), encoding="utf-8")
    scenario_receipt.write_text(
        json.dumps(
            {
                "artifact_type": "scenario_execution_receipt",
                "artifact_version": "v1",
                "schema_version": "scenario-execution-receipt-v1",
                "parent_acceptance_run_id": "test-run",
                "scenario_id": "K",
                "gate": "K",
                "final_executable_sha": "e" * 40,
                "plan_sha256": "a" * 64,
                "runtime_spec_sha256": "b" * 64,
                "input_identity_sha256": "c" * 64,
                "workspace_identity_sha256": "d" * 64,
                "executor_pid": os.getpid(),
                "executor_process_creation_identity": "test-process",
                "executor_host_id": "test-host",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:01Z",
                "action_type": "offline-k",
                "workspace": str(tmp_path),
                "job_id": "test-job",
                "attempt_id": "test-attempt",
                "budget_domain": "offline-k",
                "status": "PASSED",
                "exit_status": 0,
                "produced_evidence_refs": [],
            }
        ),
        encoding="utf-8",
    )
    producer = GateEvidenceProducer(final_sha="e" * 40)
    evidence = producer.build_gate(
        "K",
        [
            producer.reference(
                process_events,
                role="process_events",
                artifact_type="acceptance_process_event",
                artifact_version="v1",
                schema_version="process-event-v1",
            ),
            producer.reference(
                lock_state,
                role="lock_state",
                artifact_type="contention_result",
                artifact_version="v1",
                schema_version="contention-result-v1",
            ),
            producer.reference(
                scenario_receipt,
                role="scenario_execution_receipt",
                artifact_type="scenario_execution_receipt",
                artifact_version="v1",
                schema_version="scenario-execution-receipt-v1",
            ),
        ],
        acceptance_run_id="test-run",
        scenario_id="K",
    )

    result = GateEvidenceVerifier().verify(
        "K",
        evidence,
        expected_final_sha="e" * 40,
    )

    assert result["status"] != "PASS"
    assert any(
        field in str(result.get("reason", ""))
        for field in ("corrupt", "lost", "contention", "derived")
    )


@pytest.mark.skipif(os.name != "nt", reason="requires independent Windows processes")
def test_gate_k_scenario_executes_real_dual_process_contention(tmp_path: Path) -> None:
    context = AcceptanceScenarioContextV1(
        acceptance_run_id="acceptance-k-test",
        final_executable_sha="3" * 40,
        runtime_spec_path="",
        workspace_path="",
        job_id="",
        evidence_root=str(tmp_path / "evidence"),
        process_event_log=str(tmp_path / "process_events.jsonl"),
        owner_authorized=True,
        provider_budget={
            "max_provider_calls_total": 4,
            "max_output_tokens_total": 8,
            "max_retry_attempts_total": 0,
            "max_wall_seconds": 60,
        },
        provider_budget_state_path=str(tmp_path / "budget.json"),
    )

    scenario = GateKScenario()
    result = scenario.collect(context, [], runtime_result=None)

    assert result.status == "READY_FOR_SEMANTIC_VERIFICATION"
    evidence = GateEvidenceProducer(final_sha="3" * 40).build_gate(
        "K",
        result.evidence_refs,
        acceptance_run_id=context.acceptance_run_id,
        job_id="acceptance-k-test:K",
    )
    verified = GateEvidenceVerifier().verify(
        "K",
        evidence,
        expected_final_sha="3" * 40,
        expected_acceptance_run_id=context.acceptance_run_id,
        expected_job_id="acceptance-k-test:K",
    )

    assert verified["status"] == "PASS", verified
    assert verified["derived_facts"]["process_count"] == 2
    assert verified["derived_facts"]["no_corrupt_json"] is True
    assert verified["derived_facts"]["no_lost_update"] is True
    assert not Path(context.provider_budget_state_path).is_file()
    assert Path(context.evidence_root, "K", "offline_contention_budget_state.json").is_file()


def test_live_gate_rejects_provider_receipt_missing_authoritative_job_binding(tmp_path: Path) -> None:
    profile = tmp_path / "free_mode_profile.json"
    receipt = tmp_path / "provider_receipts.jsonl"
    terminal = tmp_path / "stage_terminal.json"
    profile.write_text(json.dumps({"research_goal": "fixture"}), encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "artifact_type": "provider_call_receipt",
                "artifact_version": "v2",
                "receipt_id": "receipt-1",
                "sequence": 1,
                "attempt_id": "attempt-1",
                "stage_name": "free_mode",
                "route": "Free_Mode_API",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "endpoint": "https://api.example.test",
                "status": "success",
                "attempts": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "closure_epoch_id": "epoch-1",
                "node_id": "free-mode-node",
                "call_id": "call-1",
                "endpoint_type": "chat_completions",
                "metadata": {"transport_config": {"api_base": "https://api.example.test"}},
                "test_only": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    terminal.write_text(json.dumps({"artifact_type": "runtime_stage_terminal", "status": "complete"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="f" * 40)
    acceptance_run_id = "test-run-g-binding"
    evidence = producer.build_gate(
        "G",
        [
            producer.reference(
                profile,
                role="free_mode_profile",
                artifact_type="free_mode_profile",
                artifact_version="v1",
                schema_version="free-mode-profile-v1",
                job_id="job-1",
            ),
            producer.reference(
                receipt,
                role="provider_receipt_ledger",
                artifact_type="provider_receipt_ledger",
                artifact_version="v1",
                job_id="job-1",
            ),
            producer.reference(
                terminal,
                role="stage_terminal",
                artifact_type="runtime_stage_terminal",
                artifact_version="v1",
                job_id="job-1",
            ),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="G",
                final_sha="f" * 40,
                acceptance_run_id=acceptance_run_id,
                job_id="job-1",
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="G",
    )

    result = GateEvidenceVerifier().verify(
        "G",
        evidence,
        expected_final_sha="f" * 40,
        expected_job_id="job-1",
    )

    assert result["status"] != "PASS"
    assert "job" in str(result["reason"]).lower()


def test_live_gate_does_not_count_test_only_provider_receipts(tmp_path: Path) -> None:
    profile = tmp_path / "free_mode_profile.json"
    receipt = tmp_path / "provider_receipts.jsonl"
    terminal = tmp_path / "stage_terminal.json"
    profile.write_text(json.dumps({"research_goal": "fixture"}), encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "artifact_type": "provider_call_receipt",
                "receipt_id": "test-only-receipt",
                "status": "success",
                "attempts": 1,
                "route": "free_mode",
                "provider": "fixture",
                "test_only": True,
                "metadata": {"transport_config": {"api_base": "https://fixture.invalid"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    terminal.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="d" * 40)
    acceptance_run_id = "test-run-g-test-only"
    evidence = producer.build_gate(
        "G",
        [
            producer.reference(
                profile,
                role="free_mode_profile",
                artifact_type="free_mode_profile",
                artifact_version="v1",
                schema_version="free-mode-profile-v1",
            ),
            producer.reference(
                receipt,
                role="provider_receipt_ledger",
                artifact_type="provider_receipt_ledger",
            ),
            producer.reference(
                terminal,
                role="stage_terminal",
                artifact_type="runtime_stage_terminal",
            ),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="G",
                final_sha="d" * 40,
                acceptance_run_id=acceptance_run_id,
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="G",
    )

    result = GateEvidenceVerifier().verify(
        "G",
        evidence,
        expected_final_sha="d" * 40,
    )

    assert result["status"] != "PASS"
    assert "provider receipt" in str(result["reason"]).lower()


def test_gate_f_rejects_partial_semantic_role_receipts(tmp_path: Path) -> None:
    ledger_path = tmp_path / "outline_receipts.jsonl"
    runtime = ProviderRuntime(
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id="job-f",
        attempt_id="attempt-f",
        stage_name="outline_v3",
        route="candidate_provider_generation",
        node_id="candidate_1_provider_generation",
        call_id="candidate-1",
        endpoint_type="chat_completions",
    )
    admission = runtime.admit(requested_output_tokens=4)
    runtime.complete(
        admission=admission,
        prompt="outline",
        input_payload={"text": "source"},
        api_config={
            "provider_family": "deepseek",
            "model": "model-a",
            "api_base": "https://outline.example.test",
            "endpoint_type": "chat_completions",
        },
        result={"status": "success", "content": {"ok": True}, "output_tokens": 1},
        metadata={
            "transport_config": {
                "provider_family": "deepseek",
                "model": "model-a",
                "api_base": "https://outline.example.test",
                "endpoint_type": "chat_completions",
            },
            "config_section": "Outline_API",
            "route_fingerprint": "route-fingerprint",
        },
    )
    canonical = tmp_path / "canonical.json"
    plan = tmp_path / "plan.json"
    terminal = tmp_path / "terminal.json"
    closure = tmp_path / "closure.json"
    canonical.write_text(json.dumps({"summaries": [{"paper_key": "paper-1"}]}), encoding="utf-8")
    plan.write_text(
        json.dumps(
            {
                "artifact_type": "outline_provider_call_plan",
                "artifact_version": "v1",
                "reachable_provider_route_plan": {
                    "routes": [
                        {
                            "semantic_role": "candidate_provider_generation",
                            "section": "Outline_API",
                            "provider_family": "deepseek",
                            "model": "model-a",
                            "endpoint_type": "chat_completions",
                            "api_base_host": "outline.example.test",
                            "enabled": True,
                            "resolved": True,
                        },
                        {
                            "semantic_role": "relation_adjudication",
                            "section": "Free_Mode_API",
                            "provider_family": "deepseek",
                            "model": "model-b",
                            "endpoint_type": "chat_completions",
                            "api_base_host": "free.example.test",
                            "enabled": True,
                            "resolved": True,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    terminal.write_text(json.dumps({"artifact_type": "runtime_stage_terminal", "status": "complete"}), encoding="utf-8")
    closure.write_text(json.dumps({"artifact_type": "provider_receipt_closure", "status": "complete"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="2" * 40)
    evidence = producer.build_gate(
        "F",
        [
            producer.reference(canonical, role="canonical_stage1", artifact_type="stage1_canonical_summaries", job_id="job-f"),
            producer.reference(plan, role="outline_provider_call_plan", artifact_type="outline_provider_call_plan", artifact_version="v1", job_id="job-f"),
            producer.reference(ledger_path, role="provider_receipt_ledger", artifact_type="provider_receipt_ledger", artifact_version="v1", job_id="job-f"),
            producer.reference(terminal, role="stage_terminal", artifact_type="runtime_stage_terminal", job_id="job-f"),
            producer.reference(closure, role="closure", artifact_type="provider_receipt_closure", job_id="job-f"),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="F",
                final_sha="2" * 40,
                acceptance_run_id="test-run-f",
                job_id="job-f",
            ),
        ],
        acceptance_run_id="test-run-f",
        scenario_id="F",
    )

    result = GateEvidenceVerifier().verify(
        "F",
        evidence,
        expected_final_sha="2" * 40,
        expected_job_id="job-f",
    )

    assert result["status"] != "PASS"
    assert "relation_adjudication" in str(result["reason"])


def test_gate_e_rejects_filename_only_interruption_and_resume_files(tmp_path: Path) -> None:
    interruption = tmp_path / "interruption_event.json"
    resume = tmp_path / "resume_event.json"
    events = tmp_path / "process_events.jsonl"
    ledger = tmp_path / "provider_receipts.jsonl"
    interruption.write_text(json.dumps({"status": "interrupted"}), encoding="utf-8")
    resume.write_text(json.dumps({"status": "resumed"}), encoding="utf-8")
    events.write_text(json.dumps({"event": "resume"}) + "\n", encoding="utf-8")
    ledger.write_text("", encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="5" * 40)
    acceptance_run_id = "test-run-e"
    evidence = producer.build_gate(
        "E",
        [
            producer.reference(interruption, role="interruption_event"),
            producer.reference(resume, role="resume_event"),
            producer.reference(ledger, role="provider_receipt_ledger", artifact_type="provider_receipt_ledger"),
            producer.reference(events, role="process_events"),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="E",
                final_sha="5" * 40,
                acceptance_run_id=acceptance_run_id,
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="E",
    )

    result = GateEvidenceVerifier().verify(
        "E",
        evidence,
        expected_final_sha="5" * 40,
    )

    assert result["status"] != "PASS"
    assert "interruption" in str(result["reason"]).lower()


def test_gate_d_rejects_self_declared_modalities_without_three_derived_profiles(tmp_path: Path) -> None:
    sources = []
    profiles = []
    for index in range(3):
        source = tmp_path / f"source-{index}.pdf"
        source.write_bytes(b"%PDF-1.4\nsynthetic source\n")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        sources.append(source)
        profile = tmp_path / f"profile-{index}.json"
        profile.write_text(
            json.dumps(
                {
                    "artifact_type": "document_modality_profile",
                    "artifact_version": "v1",
                    "schema_version": "document-modality-profile-v1",
                    "source_pdf_sha256": source_hash,
                    "total_page_count": 10,
                    "text_page_ratio": 1.0,
                    "image_page_ratio": 0.0,
                    "table_count": 0,
                    "figure_count": 0,
                    "scanned_candidate_page_count": 0,
                    "ocr_used_page_count": 0,
                    "selected_visual_count": 0,
                    "extractor_used": "self-declared",
                }
            ),
            encoding="utf-8",
        )
        profiles.append(profile)
    canonical = tmp_path / "canonical.json"
    terminal = tmp_path / "terminal.json"
    registry = tmp_path / "registry.json"
    ledger = tmp_path / "ledger.jsonl"
    closure = tmp_path / "closure.json"
    canonical.write_text(json.dumps({"summaries": []}), encoding="utf-8")
    terminal.write_text(json.dumps({"artifact_type": "runtime_stage_terminal", "status": "complete"}), encoding="utf-8")
    registry.write_text(json.dumps({"artifact_registry_version": "v2", "job_id": "job-d", "artifacts": []}), encoding="utf-8")
    ledger.write_text("", encoding="utf-8")
    closure.write_text(json.dumps({"artifact_type": "provider_receipt_closure", "status": "complete"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="7" * 40)
    acceptance_run_id = "test-run-d"
    refs = [
        *[
            producer.reference(source, role="source_pdf", artifact_type="source_pdf")
            for source in sources
        ],
        *[
            producer.reference(
                profile,
                role="modality_profile",
                artifact_type="document_modality_profile",
                artifact_version="v1",
                schema_version="document-modality-profile-v1",
            )
            for profile in profiles
        ],
        producer.reference(canonical, role="canonical_stage1", artifact_type="summary_file"),
        producer.reference(terminal, role="stage_terminal", artifact_type="runtime_stage_terminal"),
        producer.reference(registry, role="registry"),
        producer.reference(ledger, role="provider_receipt_ledger", artifact_type="provider_receipt_ledger"),
        producer.reference(closure, role="closure", artifact_type="provider_receipt_closure"),
        _scenario_receipt_ref(
            tmp_path,
            producer,
            gate="D",
            final_sha="7" * 40,
            acceptance_run_id=acceptance_run_id,
        ),
    ]

    result = GateEvidenceVerifier().verify(
        "D",
        producer.build_gate(
            "D",
            refs,
            acceptance_run_id=acceptance_run_id,
            scenario_id="D",
        ),
        expected_final_sha="7" * 40,
    )

    assert result["status"] != "PASS"
    assert "derived modality profiles" in str(result["reason"])


def test_gate_h_rejects_generic_validation_findings_without_challenge_lineage(tmp_path: Path) -> None:
    challenge = tmp_path / "defect_artifact.json"
    validation = tmp_path / "validation.json"
    repair = tmp_path / "repair.json"
    ledger = tmp_path / "ledger.jsonl"
    challenge.write_text(json.dumps({"findings": ["generic"]}), encoding="utf-8")
    validation.write_text(json.dumps({"status": "completed", "findings": ["generic"]}), encoding="utf-8")
    repair.write_text(json.dumps({"status": "completed", "findings": ["generic"]}), encoding="utf-8")
    ledger.write_text("", encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="8" * 40)
    acceptance_run_id = "test-run-h"
    evidence = producer.build_gate(
        "H",
        [
            producer.reference(challenge, role="defect_artifact", artifact_type="validation_report_projection"),
            producer.reference(validation, role="validation_artifact", artifact_type="validation_run_result"),
            producer.reference(repair, role="repair_artifact", artifact_type="repair_transaction"),
            producer.reference(ledger, role="provider_receipt_ledger", artifact_type="provider_receipt_ledger"),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="H",
                final_sha="8" * 40,
                acceptance_run_id=acceptance_run_id,
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="H",
    )

    result = GateEvidenceVerifier().verify("H", evidence, expected_final_sha="8" * 40)

    assert result["status"] != "PASS"
    assert "challenge" in str(result["reason"]).lower()


def test_gate_j_rejects_used_ocr_without_dependency_lineage(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    diagnostics = tmp_path / "ocr_diagnostics.json"
    artifact = tmp_path / "ocr_artifact.json"
    canonical = tmp_path / "canonical.json"
    registry = tmp_path / "registry.json"
    source.write_bytes(b"%PDF-1.4\nsource\n")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    diagnostics.write_text(
        json.dumps(
            {
                "artifact_type": "ocr_diagnostics",
                "artifact_version": "v1",
                "schema_version": "ocr-diagnostics-v1",
                "source_pdf_sha256": source_hash,
                "ocr_engine": "tesseract",
                "ocr_engine_version": "test",
                "page_numbers": [1],
                "ocr_page_count": 1,
                "output_artifact_hashes": {"1": "a" * 64},
            }
        ),
        encoding="utf-8",
    )
    artifact.write_text(json.dumps({"used_ocr": True}), encoding="utf-8")
    canonical.write_text(json.dumps({"used_ocr": True}), encoding="utf-8")
    registry.write_text(json.dumps({"artifact_registry_version": "v2", "job_id": "job-j", "artifacts": []}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="9" * 40)
    acceptance_run_id = "test-run-j"
    evidence = producer.build_gate(
        "J",
        [
            producer.reference(source, role="source_pdf", artifact_type="source_pdf"),
            producer.reference(diagnostics, role="ocr_diagnostics", artifact_type="ocr_diagnostics", artifact_version="v1", schema_version="ocr-diagnostics-v1"),
            producer.reference(artifact, role="ocr_artifact", artifact_type="ocr_artifact", artifact_version="v1", schema_version="ocr-artifact-v1"),
            producer.reference(canonical, role="canonical_stage1", artifact_type="summary_file"),
            producer.reference(registry, role="registry"),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="J",
                final_sha="9" * 40,
                acceptance_run_id=acceptance_run_id,
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="J",
    )

    result = GateEvidenceVerifier().verify("J", evidence, expected_final_sha="9" * 40)

    assert result["status"] != "PASS"
    assert "OCR" in str(result["reason"])


def test_gate_q_rejects_single_aggregate_without_fifteen_paper_identities(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    canonical = tmp_path / "canonical.json"
    outline = tmp_path / "outline.json"
    docx = tmp_path / "review.docx"
    validation = tmp_path / "validation.json"
    ledger = tmp_path / "ledger.jsonl"
    registry = tmp_path / "registry.json"
    closure = tmp_path / "closure.json"
    job_outcome = tmp_path / "outcome.json"
    citation = tmp_path / "citation.json"
    source.write_bytes(b"%PDF-1.4\nsource\n")
    canonical.write_text(json.dumps({"summaries": [{"canonical_paper_key": "only-one"}]}), encoding="utf-8")
    outline.write_text(json.dumps({"artifact_type": "runtime_stage_terminal", "status": "succeeded"}), encoding="utf-8")
    docx.write_bytes(b"not-a-docx")
    validation.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    ledger.write_text("", encoding="utf-8")
    registry.write_text(json.dumps({"artifact_registry_version": "v2", "job_id": "job-q", "artifacts": []}), encoding="utf-8")
    closure.write_text(json.dumps({"artifact_type": "provider_receipt_closure", "status": "complete"}), encoding="utf-8")
    job_outcome.write_text(json.dumps({"job_status": "completed", "canonical_ready": True}), encoding="utf-8")
    citation.write_text(json.dumps({"artifact_type": "citation_manifest", "paper_entries": []}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="a" * 40)
    acceptance_run_id = "test-run-q"
    evidence = producer.build_gate(
        "Q",
        [
            producer.reference(source, role="source_pdf", artifact_type="source_pdf"),
            producer.reference(canonical, role="canonical_stage1", artifact_type="summary_file"),
            producer.reference(outline, role="outline_terminal", artifact_type="runtime_stage_terminal"),
            producer.reference(docx, role="review_docx", artifact_type="review_docx"),
            producer.reference(validation, role="validation_artifact", artifact_type="validation_run_result"),
            producer.reference(ledger, role="provider_receipt_ledger", artifact_type="provider_receipt_ledger"),
            producer.reference(registry, role="registry"),
            producer.reference(closure, role="closure", artifact_type="provider_receipt_closure"),
            producer.reference(job_outcome, role="job_outcome", artifact_type="job_outcome"),
            producer.reference(citation, role="citation_manifest", artifact_type="citation_manifest", artifact_version="v3"),
            _scenario_receipt_ref(
                tmp_path,
                producer,
                gate="Q",
                final_sha="a" * 40,
                acceptance_run_id=acceptance_run_id,
            ),
        ],
        acceptance_run_id=acceptance_run_id,
        scenario_id="Q",
    )

    result = GateEvidenceVerifier().verify("Q", evidence, expected_final_sha="a" * 40)

    assert result["status"] != "PASS"
    assert "fifteen" in str(result["reason"]).lower()


def test_public_acceptance_run_persists_blocked_state_without_owner_inputs(tmp_path: Path) -> None:
    from runtime.control_plane import ReviewControlPlane

    acceptance_spec = tmp_path / "acceptance.json"
    acceptance_spec.write_text(
        json.dumps({"gates": ["C"], "state_path": "state.json"}),
        encoding="utf-8",
    )
    result = ReviewControlPlane(repo_root=Path.cwd()).acceptance_run(acceptance_spec)
    assert result["status"] == "blocked"
    assert result["gates"]["C"]["status"] == "NOT_VERIFIED"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == "release-acceptance-run-state-v1"
    assert state["final_sha"]


@pytest.mark.parametrize("gate", ("C", "D", "E", "F", "G", "H", "I", "J", "Q"))
def test_acceptance_scenarios_do_not_ready_from_inventory_when_execution_is_blocked(
    gate: str,
) -> None:
    context = AcceptanceScenarioContextV1(
        acceptance_run_id="acceptance-scenario-boundary",
        final_executable_sha="a" * 40,
        runtime_spec_path="",
        workspace_path="",
        job_id="job-scenario-boundary",
        evidence_root="",
        process_event_log="",
        owner_authorized=False,
    )
    inventory = [{"role": role} for role in gate_evidence_roles(gate)]

    result = scenario_for_gate(gate).execute(
        context,
        inventory,
        runtime_result={
            "status": "BLOCKED_AUTHORIZATION",
            "reason": "fixture deliberately did not execute the scenario",
        },
    )

    assert result.status == "BLOCKED_SCENARIO_EXECUTION"
    assert "did not complete" in result.reason


def test_release_acceptance_budget_schema_rejects_typos() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="max_provider_call"):
        ReleaseAcceptanceSpec.from_mapping(
            {"budget": {"max_provider_call": 1}}
        )


def test_release_acceptance_spec_rejects_conflicting_budget_aliases() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="budget aliases"):
        ReleaseAcceptanceSpec.from_mapping(
            {
                "budget": {"max_provider_calls_total": 1},
                "acceptance_budget": {"max_provider_calls_total": 2},
            }
        )


def test_aggregate_provider_budget_is_shared_and_reserves_transport_attempts() -> None:
    controller = ProviderBudgetController(
        ProviderAggregateBudgetV1(
            max_provider_calls_total=2,
            max_output_tokens_total=8,
            max_retry_attempts_total=1,
            max_wall_seconds=60,
        )
    )
    first = ProviderRuntime(
        aggregate_budget=controller,
        test_only=True,
    )
    second = ProviderRuntime(
        aggregate_budget=controller,
        test_only=True,
    )

    admission = first.admit(
        estimated_tokens=1,
        requested_output_tokens=4,
        requested_retry_attempts=1,
    )
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        second.admit(
            estimated_tokens=1,
            requested_output_tokens=1,
            requested_retry_attempts=0,
        )

    first.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"text": "input"},
        api_config={"model": "model", "api_base": "https://example.test"},
        result={"status": "success", "content": {}, "attempts": 2, "output_tokens": 3},
    )
    snapshot = controller.snapshot()
    assert snapshot["calls_used"] == 2
    assert snapshot["output_tokens_used"] == 3
    assert snapshot["retry_attempts_used"] == 1


def test_acceptance_budget_environment_binds_all_provider_runtimes(monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(
            {
                "max_provider_calls_total": 1,
                "max_output_tokens_total": 4,
                "max_retry_attempts_total": 0,
                "max_wall_seconds": 60,
            }
        ),
    )
    first = ProviderRuntime(test_only=True)
    second = ProviderRuntime(test_only=True)
    first.admit(estimated_tokens=1, requested_output_tokens=4)
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        second.admit(estimated_tokens=1, requested_output_tokens=1)


def test_aggregate_provider_budget_state_survives_process_boundary(tmp_path: Path) -> None:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=2,
        max_output_tokens_total=8,
        max_retry_attempts_total=1,
        max_wall_seconds=60,
    )
    state_path = tmp_path / "budget-state.json"
    first_controller = ProviderBudgetController(budget)
    first_controller.bind_state_path(state_path)
    first_runtime = ProviderRuntime(aggregate_budget=first_controller, test_only=True)
    admission = first_runtime.admit(
        estimated_tokens=1,
        requested_output_tokens=4,
        requested_retry_attempts=0,
    )
    first_runtime.complete(
        admission=admission,
        prompt="prompt",
        input_payload={"text": "input"},
        api_config={"model": "model", "api_base": "https://example.test"},
        result={"status": "success", "content": {}, "attempts": 1, "output_tokens": 3},
    )

    second_controller = ProviderBudgetController(budget)
    second_controller.bind_state_path(state_path)
    assert second_controller.snapshot()["calls_used"] == 1
    second_runtime = ProviderRuntime(aggregate_budget=second_controller, test_only=True)
    second_runtime.admit(estimated_tokens=1, requested_output_tokens=5)
    with pytest.raises(ProviderBudgetExceeded, match="call budget"):
        ProviderRuntime(aggregate_budget=second_controller, test_only=True).admit(
            estimated_tokens=1,
            requested_output_tokens=1,
        )




def test_bind_state_path_reconciles_started_orphan_from_exact_receipt_ledger(tmp_path: Path) -> None:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=2,
        max_output_tokens_total=4,
        max_retry_attempts_total=1,
        max_wall_seconds=60,
    )
    state_path = tmp_path / "budget-state.json"
    ledger_path = tmp_path / "provider-receipts.jsonl"
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(state_path)
    runtime = ProviderRuntime(
        aggregate_budget=controller,
        ledger=ProviderRuntimeLedger(ledger_path),
        job_id="job-orphan",
        attempt_id="attempt-orphan",
        stage_name="stage-orphan",
        route="route-orphan",
        node_id="node-orphan",
        call_id="call-orphan",
        endpoint_type="chat_completions",
        test_only=True,
    )
    admission = runtime.admit(requested_output_tokens=1)
    runtime.mark_transport_started(admission)
    reservation_id = admission.aggregate_reservation_id
    assert reservation_id
    receipt = ProviderCallReceiptV1.from_result(
        admission=admission,
        job_id="job-orphan",
        attempt_id="attempt-orphan",
        stage_name="stage-orphan",
        route="route-orphan",
        provider="offline",
        model="offline",
        endpoint="https://offline.invalid",
        prompt_hash="a" * 64,
        input_hash="b" * 64,
        config_hash="c" * 64,
        schema_hash="d" * 64,
        result={"status": "success", "content": {}, "output_tokens": 1},
        budget=__import__("runtime.provider_runtime", fromlist=["ProviderBudgetV1"]).ProviderBudgetV1(),
        started_at="2026-01-01T00:00:00Z",
        metadata={"aggregate_reservation_id": reservation_id},
        node_id="node-orphan",
        call_id="call-orphan",
        closure_epoch_id="epoch-orphan",
        endpoint_type="chat_completions",
        test_only=True,
    )
    ProviderRuntimeLedger(ledger_path).append(receipt)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["reservations"][0]["owner_pid"] = 999999999
    state["reservations"][0]["owner_process_creation_time"] = 0.0
    state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = ProviderBudgetController(budget)
    recovered.bind_state_path(state_path)

    assert recovered.snapshot()["calls_used"] == 1
    assert recovered.snapshot()["calls_reserved"] == 0


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows process API")
def test_windows_process_liveness_probe_does_not_terminate_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity_for_pid(child.pid)
        assert identity.creation_time is not None
        assert is_process_alive(identity) is True
        assert child.poll() is None
    finally:
        assert child.wait(timeout=10) == 0


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows process API")
def test_windows_process_liveness_rejects_wrong_creation_identity_without_killing_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity_for_pid(child.pid)
        assert identity.creation_time is not None
        wrong_identity = type(identity)(
            pid=identity.pid,
            creation_time=identity.creation_time + 3600.0,
            host_id=identity.host_id,
        )
        assert is_process_alive(wrong_identity) is False
        assert child.poll() is None
    finally:
        assert child.wait(timeout=10) == 0


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows process API")
def test_windows_liveness_probe_runs_in_an_independent_process_without_killing_child(
    tmp_path: Path,
) -> None:
    identity_path = tmp_path / "child-process-identity.json"
    child_code = (
        "import json,sys,time; "
        "from runtime.provider_runtime import process_identity_for_pid; "
        "identity=process_identity_for_pid(__import__('os').getpid()); "
        "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(identity.__dict__)); "
        "time.sleep(5)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(identity_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not identity_path.is_file() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert identity_path.is_file()
        probe_code = (
            "import json,sys; "
            "from runtime.provider_runtime import ProcessIdentityV1,is_process_alive; "
            "payload=json.loads(open(sys.argv[1],encoding='utf-8').read()); "
            "identity=ProcessIdentityV1(**payload); "
            "print(json.dumps({'alive':is_process_alive(identity)}))"
        )
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, str(identity_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert probe.returncode == 0
        assert json.loads(probe.stdout.strip())["alive"] is True
        assert child.poll() is None
    finally:
        assert child.wait(timeout=10) == 0


def test_aggregate_budget_persists_owner_process_identity(tmp_path: Path) -> None:
    controller = ProviderBudgetController(ProviderAggregateBudgetV1(max_provider_calls_total=1))
    state_path = tmp_path / "budget-state.json"
    controller.bind_state_path(state_path)
    controller.admit(requested_output_tokens=1)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    reservation = state["reservations"][0]
    assert reservation["owner_pid"] == os.getpid()
    assert reservation["owner_host_id"]
    assert reservation["owner_process_creation_time"] is not None


def test_acceptance_execution_context_is_the_provider_budget_authority(tmp_path: Path, monkeypatch) -> None:
    budget = ProviderAggregateBudgetV1(
        max_provider_calls_total=1,
        max_output_tokens_total=4,
        max_retry_attempts_total=0,
        max_wall_seconds=60,
    )
    state_path = tmp_path / "acceptance" / "run-1" / "provider_budget_state.json"
    controller = ProviderBudgetController(budget)
    controller.bind_state_path(state_path)
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="run-1",
        final_executable_sha="a" * 40,
        absolute_deadline_epoch=controller.snapshot()["absolute_deadline_epoch"],
        provider_budget=budget,
        provider_budget_state_path=str(state_path),
        evidence_root=str(state_path.parent / "evidence"),
        process_event_log=str(state_path.parent / "process_events.jsonl"),
        scenario_state_path=str(state_path.parent / "acceptance_state.json"),
        owner_authorized=True,
    )
    monkeypatch.setenv(
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        json.dumps(
            {
                "max_provider_calls_total": 99,
                "max_output_tokens_total": 99,
                "max_retry_attempts_total": 99,
                "max_wall_seconds": 99,
            }
        ),
    )
    with bind_acceptance_execution_context(context, controller):
        runtime = ProviderRuntime(test_only=True)
        assert runtime.aggregate_budget is controller
        runtime.admit(requested_output_tokens=4)
        with pytest.raises(ProviderBudgetExceeded, match="call budget"):
            ProviderRuntime(test_only=True).admit(requested_output_tokens=1)


def test_acceptance_run_binds_spec_budget_to_runtime_context(tmp_path: Path, monkeypatch) -> None:
    runtime_spec = tmp_path / "runtime.json"
    runtime_spec.write_text(
        json.dumps(
            {
                "project_name": "acceptance-budget",
                "source": {"mode": "direct", "pdf_folder": str(tmp_path / "pdfs")},
                "config": str(tmp_path / "config.ini"),
            }
        ),
        encoding="utf-8",
    )
    acceptance_spec = tmp_path / "acceptance.json"
    acceptance_spec.write_text(
        json.dumps(
            {
                "runtime_spec": "runtime.json",
                "state_path": "state.json",
                "gates": ["C"],
                "budget": {
                    "max_provider_calls_total": 1,
                    "max_output_tokens_total": 4,
                    "max_retry_attempts_total": 1,
                    "max_wall_seconds": 60,
                },
            }
        ),
        encoding="utf-8",
    )
    control = __import__("runtime.control_plane", fromlist=["ReviewControlPlane"]).ReviewControlPlane(
        repo_root=Path.cwd()
    )
    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    monkeypatch.setattr(control, "provider_preflight", lambda **_kwargs: {"route_plan": {}})
    seen: dict[str, object] = {}

    def fake_run(_spec_path: str | Path) -> dict[str, object]:
        runtime = ProviderRuntime(test_only=True)
        seen["controller"] = runtime.aggregate_budget
        assert runtime.aggregate_budget is not None
        runtime.admit(requested_output_tokens=4)
        with pytest.raises(ProviderBudgetExceeded, match="call budget"):
            ProviderRuntime(test_only=True).admit(requested_output_tokens=1)
        return {"status": "BLOCKED_INPUT", "reason": "fixture execution", "job_id": "job-budget"}

    monkeypatch.setattr(control, "run", fake_run)
    result = control.acceptance_run(acceptance_spec)

    assert seen["controller"] is not None
    assert result["acceptance_execution_context"]["provider_budget"]["max_provider_calls_total"] == 1
    assert Path(result["provider_budget_state_path"]).is_file()
    assert Path(result["provider_budget_state_path"]).parent.name == result["run_id"]


def test_acceptance_evidence_manifest_refreshes_with_revision_on_resume(tmp_path: Path) -> None:
    first = tmp_path / "stage-a.json"
    second = tmp_path / "stage-b.json"
    manifest = tmp_path / "evidence-index.json"
    first.write_text(json.dumps({"stage": "a"}), encoding="utf-8")
    second.write_text(json.dumps({"stage": "b"}), encoding="utf-8")
    producer = GateEvidenceProducer(final_sha="1" * 40)

    producer.write_manifest(
        manifest,
        {"C": [producer.reference(first, role="stage_terminal")]},
        acceptance_run_id="run-1",
        scenario_id="C",
        job_id="job-1",
    )
    first_payload = json.loads(manifest.read_text(encoding="utf-8"))
    producer.write_manifest(
        manifest,
        {
            "C": [
                producer.reference(first, role="stage_terminal"),
                producer.reference(second, role="job_outcome"),
            ]
        },
        acceptance_run_id="run-1",
        scenario_id="C",
        job_id="job-1",
    )
    second_payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert first_payload["revision"] == 1
    assert second_payload["revision"] == 2
    assert second_payload["previous_revision_hash"] == hashlib.sha256(
        json.dumps(first_payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert second_payload["acceptance_run_id"] == "run-1"
    assert second_payload["scenario_id"] == "C"
    assert second_payload["job_id"] == "job-1"
    assert len(second_payload["gates"]["C"]["durable_refs"]) == 2


def test_local_rag_identity_change_selects_a_new_immutable_collection() -> None:
    first = LocalRAGIndex._collection_name_for_identity("source", "a" * 64)
    second = LocalRAGIndex._collection_name_for_identity("source", "b" * 64)

    assert first != second
    assert len(first) <= 63
    assert len(second) <= 63


def test_profile_path_rejects_traversal_and_save_is_atomic_boundary(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        get_profile_path(str(tmp_path), "../escape")
    with pytest.raises(WorkspacePathError):
        save_profile({"research_goal": "x"}, str(tmp_path), "CON")

    path = Path(save_profile({"research_goal": "x"}, str(tmp_path), "safe"))
    assert path == Path(get_profile_path(str(tmp_path), "safe"))
    assert json.loads(path.read_text(encoding="utf-8"))["research_goal"] == "x"


@pytest.mark.optional
def test_workspace_rejects_existing_reparse_leaf_for_production_artifact_path(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "project", "job")
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    link = Path(workspace.paths.artifacts_dir) / "link.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable in this environment")
        raise

    with pytest.raises(WorkspacePathError, match="reparse|symlink"):
        workspace.artifact_path("link.json")


def test_strict_evidence_loader_rejects_tampered_required_artifact(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.md"
    chunks = tmp_path / "chunks.json"
    page_index = tmp_path / "page_index.json"
    manifest = tmp_path / "manifest.json"
    normalized.write_text("authoritative text", encoding="utf-8")
    chunks.write_text("[]", encoding="utf-8")
    page_index.write_text("[]", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "artifact_hashes": {
                    "normalized.md": hashlib.sha256(normalized.read_bytes()).hexdigest(),
                    "chunks.json": hashlib.sha256(chunks.read_bytes()).hexdigest(),
                    "page_index.json": hashlib.sha256(page_index.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    loader = PreprocessEvidenceLoader()
    loader.load_evidence(
        normalized_text_path=str(normalized),
        chunks_path=str(chunks),
        page_index_path=str(page_index),
        manifest_path=str(manifest),
        strict=True,
    )

    normalized.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValidationSourceAuthorityError, match="hash"):
        loader.load_evidence(
            normalized_text_path=str(normalized),
            chunks_path=str(chunks),
            page_index_path=str(page_index),
            manifest_path=str(manifest),
            strict=True,
        )


def test_strict_evidence_loader_rejects_missing_hash_and_bounds_reads(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.md"
    chunks = tmp_path / "chunks.json"
    page_index = tmp_path / "page_index.json"
    manifest = tmp_path / "manifest.json"
    normalized.write_text("x", encoding="utf-8")
    chunks.write_text("[]", encoding="utf-8")
    page_index.write_text("[]", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "artifact_hashes": {
                    "normalized.md": {
                        "sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
                        "size": 1,
                    },
                    "chunks.json": {
                        "sha256": hashlib.sha256(chunks.read_bytes()).hexdigest(),
                        "size": 2,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationSourceAuthorityError, match="hash|identity"):
        PreprocessEvidenceLoader().load_evidence(
            normalized_text_path=str(normalized),
            chunks_path=str(chunks),
            page_index_path=str(page_index),
            manifest_path=str(manifest),
            strict=True,
        )

    oversized = tmp_path / "oversized.txt"
    oversized.write_text("too large", encoding="utf-8")
    with pytest.raises(ValidationSourceAuthorityError, match="size|bound"):
        PreprocessEvidenceLoader(max_artifact_bytes=1)._read_bytes(
            str(oversized), required=True, label="oversized"
        )


def test_preprocess_cache_binds_source_hash_fingerprint_and_atomic_generation(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "cache identity test\n" * 20)
    document.save(pdf)
    document.close()
    manager = PreprocessManager(
        config={
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "enabled": "true",
                "cache_dir": str(tmp_path / "cache"),
                "extractor_profile": "fitz",
                "ocr_mode": "off",
            },
        }
    )
    first = manager.prepare_pdf(str(pdf))
    assert first is not None
    assert Path(first.manifest_path).parent.name.startswith("generation-")
    assert not list(Path(first.cache_dir).glob(".generation.tmp-*"))
    manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
    assert len(manifest["source_pdf_sha256"]) == 64
    assert manifest["processing_fingerprint"] == manager.processing_fingerprint
    assert manifest["artifact_hashes"]["normalized.md"]["sha256"]

    pointer = Path(first.cache_dir) / "active_generation.json"
    old_pointer = pointer.read_bytes()
    manager.force_rebuild = True

    def crash_after_staging(*_args, **_kwargs):
        raise RuntimeError("controlled generation crash")

    monkeypatch.setattr(manager, "_write_json_durable", crash_after_staging)
    with pytest.raises(RuntimeError, match="controlled generation crash"):
        manager.prepare_pdf(str(pdf))
    assert pointer.read_bytes() == old_pointer
    assert Path(first.manifest_path).is_file()


def test_mineru_rejects_oversized_source_before_upload(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "oversized.pdf"
    pdf.write_bytes(b"12345")
    monkeypatch.setenv("MINERU_API_TOKEN", "token")
    manager = PreprocessManager(
        config={
            "Preprocess": {"source_pdf_max_bytes": "4"},
        }
    )
    with pytest.raises(MineruArtifactLimitError, match="source PDF"):
        manager._extract_with_mineru_remote(str(pdf), [], [], [])


def test_zotero_managed_storage_is_contained_but_linked_files_can_be_external(tmp_path: Path) -> None:
    storage = tmp_path / "zotero" / "storage"
    managed_root = storage / "KEY123"
    managed_root.mkdir(parents=True)
    managed = managed_root / "paper.pdf"
    managed.write_bytes(b"pdf")
    linked = tmp_path / "external" / "paper.pdf"
    linked.parent.mkdir()
    linked.write_bytes(b"pdf")
    index = object.__new__(ZoteroAttachmentIndex)
    index.storage_root = storage
    index.zotero_root = storage.parent

    assert index._resolve_attachment_path("storage:paper.pdf", "KEY123", 0) == managed.resolve()
    with pytest.raises(ZoteroAttachmentResolutionError):
        index._resolve_attachment_path("storage:../../outside.pdf", "KEY123", 0)
    assert index._resolve_attachment_path(str(linked), "LINKED1", 2) == linked.resolve()

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    acceptance_context_environment,
    bind_acceptance_execution_context,
)
from runtime.release_acceptance import (
    AcceptanceScenarioResultV1,
    ParentAcceptanceResultV2,
    ProcessInterruptionEventV1,
    ReleaseAcceptancePlanV2,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
    ScenarioExecutionReceiptV1,
)


def _child(gate: str, runtime_spec: str) -> dict[str, object]:
    return {
        "scenario_id": gate,
        "gate": gate,
        "runtime_spec": runtime_spec,
        "workspace": f"workspace-{gate}",
        "execution_mode": "runtime",
        "budget_domain": "live",
        "prerequisites": [],
    }


def _plan_payload() -> dict[str, object]:
    return {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "parent-acceptance-1",
        "budget": {
            "max_provider_calls_total": 12,
            "max_output_tokens_total": 1000,
            "max_retry_attempts_total": 1,
            "max_wall_seconds": 900,
        },
        "scenarios": {
            "C": _child("C", "c-runtime.json"),
            "D": _child("D", "d-runtime.json"),
            "Q": _child("Q", "q-runtime.json"),
        },
    }


def test_parent_plan_requires_independent_child_specs_for_incompatible_gates() -> None:
    plan = ReleaseAcceptancePlanV2.from_mapping(_plan_payload())

    assert plan.parent_run_id == "parent-acceptance-1"
    assert plan.child("C").runtime_spec != plan.child("D").runtime_spec
    assert plan.child("D").runtime_spec != plan.child("Q").runtime_spec

    with pytest.raises(ReleaseAcceptanceSpecError, match="independent child"):
        ReleaseAcceptanceSpec.from_mapping(
            {
                "runtime_spec": "one-runtime.json",
                "gates": ["C", "D", "Q"],
            }
        )


@pytest.mark.parametrize("field", ("workspace", "job_id"))
def test_parent_plan_rejects_shared_child_identity(field: str) -> None:
    payload = _plan_payload()
    payload["scenarios"] = {
        "C": _child("C", "c-runtime.json"),
        "D": _child("D", "d-runtime.json"),
    }
    payload["scenarios"]["D"][field] = payload["scenarios"]["C"][field] = (
        "shared-child-identity" if field == "workspace" else "shared-job"
    )

    with pytest.raises(ReleaseAcceptanceSpecError, match="independent"):
        ReleaseAcceptancePlanV2.from_mapping(payload)


def _receipt_payload(**overrides: object) -> dict[str, object]:
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict[str, object] = {
        "artifact_type": "scenario_execution_receipt",
        "artifact_version": "v1",
        "schema_version": "scenario-execution-receipt-v1",
        "parent_acceptance_run_id": "parent-acceptance-1",
        "scenario_id": "C",
        "gate": "C",
        "final_executable_sha": "a" * 40,
        "plan_sha256": "b" * 64,
        "runtime_spec_sha256": "c" * 64,
        "input_identity_sha256": "d" * 64,
        "workspace_identity_sha256": "e" * 64,
        "executor_pid": 123,
        "executor_process_creation_identity": "123:456.0",
        "executor_host_id": "host-1",
        "started_at": started,
        "completed_at": started,
        "action_type": "one-paper-runtime",
        "workspace": "workspace-C",
        "job_id": "job-C",
        "attempt_id": "attempt-C",
        "budget_domain": "live",
        "status": "PASSED",
        "exit_status": 0,
        "produced_evidence_refs": [],
    }
    payload.update(overrides)
    return payload


def test_scenario_receipt_rejects_foreign_parent_child_sha_and_budget_domain() -> None:
    receipt = ScenarioExecutionReceiptV1.from_mapping(_receipt_payload())

    assert receipt.parent_acceptance_run_id == "parent-acceptance-1"
    assert receipt.scenario_id == "C"
    assert receipt.status == "PASSED"

    for overrides, message in (
        ({"scenario_id": "D"}, "scenario"),
        ({"executor_pid": 0}, "executor"),
    ):
        with pytest.raises(ReleaseAcceptanceSpecError, match=message):
            ScenarioExecutionReceiptV1.from_mapping(_receipt_payload(**overrides))

    foreign_parent = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {
                "status": "PASS",
                "receipt": _receipt_payload(
                    parent_acceptance_run_id="other-parent",
                    final_executable_sha="f" * 40,
                ),
            }
        },
        required_scenarios=("C",),
    )
    assert foreign_parent.status == "NOT_VERIFIED"

    offline_live = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {
                "status": "PASS",
                "receipt": _receipt_payload(budget_domain="offline-k"),
            }
        },
        required_scenarios=("C",),
    )
    assert offline_live.status == "NOT_VERIFIED"


def test_parent_result_cannot_turn_offline_executor_receipt_into_live_ready() -> None:
    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "K": {
                "status": "PASS_OFFLINE",
                "live_pass": False,
                "ready_to_merge": False,
                "receipt": _receipt_payload(
                    scenario_id="K",
                    gate="K",
                    action_type="offline-contention",
                    budget_domain="offline-k",
                    status="PASSED",
                ),
            }
        },
        required_scenarios=("K",),
    )

    assert result.status == "PASS_OFFLINE"
    assert result.live_pass is False
    assert result.ready_to_merge is False
    assert result.to_dict()["terminal_status"] != "READY_TO_MERGE"


def test_parent_result_rejects_cross_input_child_receipt() -> None:
    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {
                "status": "PASS",
                "receipt": _receipt_payload(
                    plan_sha256="b" * 64,
                    runtime_spec_sha256="c" * 64,
                    input_identity_sha256="d" * 64,
                    budget_domain="live",
                ),
            }
        },
        required_scenarios=("C",),
        expected_child_bindings={
            "C": {
                "plan_sha256": "e" * 64,
                "runtime_spec_sha256": "f" * 64,
                "input_identity_sha256": "0" * 64,
                "budget_domain": "live",
            }
        },
    )

    assert result.status == "NOT_VERIFIED"
    assert "binding mismatch" in result.reason


def test_parent_acceptance_plan_persists_independent_blocked_children_without_owner_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.delenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", raising=False)
    payload = _plan_payload()
    payload["state_path"] = "acceptance-state.json"
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = ReviewControlPlane(repo_root=Path.cwd()).acceptance_run(plan_path)

    assert result["status"] == "blocked"
    assert set(result["scenarios"]) == {"C", "D", "Q"}
    assert all(item["status"] == "BLOCKED" for item in result["scenarios"].values())
    assert result["parent_result"]["live_pass"] is False
    state = json.loads((tmp_path / "acceptance-state.json").read_text(encoding="utf-8"))
    assert state["plan_sha256"]
    assert set(state["child_states"]) == {"C", "D", "Q"}
    child_state_paths = [
        Path(item["state_path"])
        for item in state["child_states"].values()
    ]
    assert len(set(child_state_paths)) == 3
    assert all(path.is_file() for path in child_state_paths)


def test_parent_acceptance_plan_dispatches_each_runtime_child_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload()
    payload["state_path"] = "acceptance-state.json"
    for gate in ("C", "D", "Q"):
        workspace = tmp_path / f"workspace-{gate}"
        (tmp_path / f"{gate.lower()}-runtime.json").write_text(
            json.dumps(
                {
                    "project_name": f"acceptance-{gate.lower()}",
                    "job_id": f"job-{gate.lower()}",
                    "workspace_path": str(workspace),
                    "source": {"mode": "direct", "pdf_folder": "missing-papers"},
                }
            ),
            encoding="utf-8",
        )
    calls: list[str] = []
    control = ReviewControlPlane(repo_root=Path.cwd())

    def fake_run(runtime_spec: str | Path) -> dict[str, object]:
        calls.append(str(runtime_spec))
        gate = Path(runtime_spec).stem[0].upper()
        return {
            "status": "complete",
            "job_status": "completed",
            "completion_status": "complete",
            "success": True,
            "job_id": f"job-{gate.lower()}",
            "workspace_path": str(tmp_path / f"workspace-{gate}"),
        }

    monkeypatch.setattr(control, "run", fake_run)
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = control.acceptance_run(plan_path)

    assert result["status"] == "blocked"
    assert calls == [
        str(tmp_path / "c-runtime.json"),
        str(tmp_path / "d-runtime.json"),
        str(tmp_path / "q-runtime.json"),
    ]
    assert {gate: result["scenarios"][gate]["status"] for gate in ("C", "D", "Q")} == {
        "C": "NOT_VERIFIED",
        "D": "BLOCKED",
        "Q": "NOT_VERIFIED",
    }


def test_gate_d_plan_carries_production_modality_refs_into_child_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published Gate D profile is evidence, not only a Registry side effect."""

    import runtime.release_acceptance as release_acceptance_module
    from runtime.control_plane import ReviewControlPlane
    from runtime.release_acceptance import GateEvidenceProducer

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    source_dir = tmp_path / "papers"
    source_dir.mkdir()
    workspace = tmp_path / "workspace-d"
    workspace.mkdir()
    runtime_spec = tmp_path / "d-runtime.json"
    runtime_spec.write_text(
        json.dumps(
            {
                "project_name": "acceptance-d",
                "job_id": "job-d",
                "workspace_path": str(workspace),
                "source": {"mode": "direct", "pdf_folder": str(source_dir)},
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "parent-d",
        "budget": {"max_provider_calls_total": 1},
        "state_path": "acceptance-state.json",
        "scenarios": {
            "D": {
                "scenario_id": "D",
                "gate": "D",
                "runtime_spec": "d-runtime.json",
                "workspace": str(workspace),
                "job_id": "job-d",
                "execution_mode": "runtime",
                "budget_domain": "live",
                "prerequisites": [],
            }
        },
    }
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    control = ReviewControlPlane(repo_root=Path.cwd())
    final_sha = control._acceptance_checkout_sha(control.repo_root)
    modality_path = tmp_path / "modality-profile.json"
    modality_path.write_text("{}", encoding="utf-8")
    modality_ref = GateEvidenceProducer(final_sha=final_sha).reference(
        modality_path,
        role="modality_profile",
        artifact_type="document_modality_profile",
        artifact_version="v2",
        schema_version="document-modality-profile-v2",
        job_id="job-d",
    )
    collected: list[dict[str, object]] = []

    class SpyScenario:
        def collect(self, _context, refs, **_kwargs):
            collected.extend(dict(ref) for ref in refs)
            return AcceptanceScenarioResultV1(
                gate="D",
                scenario_id="D",
                status="READY_FOR_SEMANTIC_VERIFICATION",
                reason="test scenario",
                evidence_refs=tuple(refs),
            )

        def execute(self, _context, refs, **_kwargs):
            return AcceptanceScenarioResultV1(
                gate="D",
                scenario_id="D",
                status="BLOCKED_SCENARIO_EXECUTION",
                reason="final scenario action was blocked",
                evidence_refs=tuple(refs),
            )

    class FakeVerifier:
        def verify(self, *_args, **_kwargs):
            return {"status": "NOT_VERIFIED", "reason": "test verifier"}

    monkeypatch.setattr(
        control,
        "run",
        lambda _path: {
            "status": "complete",
            "job_status": "completed",
            "completion_status": "complete",
            "job_id": "job-d",
            "workspace_path": str(workspace),
        },
    )
    monkeypatch.setattr(
        control,
        "_acceptance_production_modality_references",
        lambda *_args, **_kwargs: [modality_ref],
    )
    monkeypatch.setattr(control, "_acceptance_workspace_references", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(control, "_acceptance_source_references", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(release_acceptance_module, "scenario_for_gate", lambda _gate: SpyScenario())
    monkeypatch.setattr(release_acceptance_module, "GateEvidenceVerifier", FakeVerifier)

    result = control.acceptance_run(plan_path)

    assert result["scenarios"]["D"]["status"] == "NOT_VERIFIED"
    assert result["scenarios"]["D"]["receipt"]["status"] == "NOT_VERIFIED"
    assert any(ref.get("role") == "modality_profile" for ref in collected)


@pytest.mark.parametrize("mismatch", ("workspace", "job_id"))
def test_runtime_child_rejects_plan_to_runtime_identity_mismatch_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    source_dir = tmp_path / "papers"
    source_dir.mkdir()
    expected_workspace = tmp_path / "workspace-d"
    runtime_workspace = (
        tmp_path / "other-workspace" if mismatch == "workspace" else expected_workspace
    )
    expected_job_id = "job-d"
    runtime_job_id = "other-job" if mismatch == "job_id" else expected_job_id
    runtime_spec = tmp_path / "d-runtime.json"
    runtime_spec.write_text(
        json.dumps(
            {
                "project_name": "acceptance-d",
                "job_id": runtime_job_id,
                "workspace_path": str(runtime_workspace),
                "source": {"mode": "direct", "pdf_folder": str(source_dir)},
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "release-acceptance-plan-v2",
                "parent_run_id": f"parent-{mismatch}",
                "state_path": "acceptance-state.json",
                "scenarios": {
                    "D": {
                        "scenario_id": "D",
                        "gate": "D",
                        "runtime_spec": "d-runtime.json",
                        "workspace": str(expected_workspace),
                        "job_id": expected_job_id,
                        "execution_mode": "runtime",
                        "budget_domain": "live",
                        "prerequisites": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    control = ReviewControlPlane(repo_root=Path.cwd())
    monkeypatch.setattr(
        control,
        "run",
        lambda path: calls.append(str(path)) or {"status": "complete"},
    )

    result = control.acceptance_run(plan_path)

    assert calls == []
    assert result["scenarios"]["D"]["status"] == "BLOCKED"
    assert mismatch in str(result["scenarios"]["D"]["reason"])


def test_acceptance_context_child_environment_does_not_mutate_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_JSON",
        "AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH",
        "AUTO_GENERATE_ACCEPTANCE_RUN_ID",
        "AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    budget = ProviderAggregateBudgetV1(max_provider_calls_total=2)
    controller = ProviderBudgetController(budget)
    context = AcceptanceExecutionContextV1(
        acceptance_run_id="parent-env-test",
        final_executable_sha="a" * 40,
        absolute_deadline_epoch=controller.snapshot()["absolute_deadline_epoch"],
        provider_budget=budget,
        provider_budget_state_path=str(tmp_path / "budget.json"),
        evidence_root=str(tmp_path / "evidence"),
        process_event_log=str(tmp_path / "events.jsonl"),
        scenario_state_path=str(tmp_path / "state.json"),
        owner_authorized=True,
    )
    before = {key: os.environ.get(key) for key in keys}

    with bind_acceptance_execution_context(context, controller):
        assert {key: os.environ.get(key) for key in keys} == before
        child_env = acceptance_context_environment(context, base_environment={})

    assert {key: os.environ.get(key) for key in keys} == before
    assert json.loads(child_env["AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON"])["acceptance_run_id"] == "parent-env-test"
    assert child_env["AUTO_GENERATE_ACCEPTANCE_BUDGET_STATE_PATH"] == str(
        tmp_path / "budget.json"
    )


def test_stage1_snapshot_replaces_partial_target_atomically(tmp_path: Path) -> None:
    from preprocess.service import PreprocessResult
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    service = object.__new__(Stage1AnalysisService)
    service.workspace = JobWorkspace.create(str(tmp_path / "output"), "snapshot", "job")
    generation_dir = tmp_path / "cache" / "generation-1"
    generation_dir.mkdir(parents=True)
    paths: dict[str, Path] = {}
    for index, field_name in enumerate(
        (
            "markdown_path",
            "plain_text_path",
            "page_index_path",
            "chunks_path",
            "diagnostics_path",
            "ocr_diagnostics_path",
            "ocr_artifact_path",
            "structured_json_path",
            "manifest_path",
            "stage1_input_path",
            "stage1_input_manifest_path",
            "stage1_quality_report_path",
        )
    ):
        source = generation_dir / f"{field_name}-{index}.dat"
        source.write_bytes(f"complete-{field_name}".encode())
        paths[field_name] = source
    result = PreprocessResult(
        pdf_path="source.pdf",
        cache_dir=str(tmp_path / "cache"),
        **{key: str(value) for key, value in paths.items()},
        markdown_text="",
        plain_text="",
        stage1_input_text="",
        page_index=[],
        page_diagnostics=[],
        low_quality=False,
        scanned_like=False,
        used_ocr=False,
        extractor_used="fitz",
        chunk_count=0,
        local_rag_enabled=False,
        local_rag_built=False,
        local_rag_persist_dir="",
        layout_fidelity="page_text",
        conversion_used="native_pdf",
        mineru_attempted=False,
        mineru_succeeded=False,
        mineru_token_present=False,
        mineru_remote_requested=False,
        mineru_remote_enabled=False,
        mineru_base_url="",
        selected_text_source="plain_text",
        stage1_quality_level="good",
    )
    digest = hashlib.sha256(b"paper").hexdigest()[:24]
    relative_target = f"source_evidence/{digest}/generation-1/{paths['markdown_path'].name}"
    target = Path(service.workspace.artifact_path(relative_target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial-copy")

    snapped = service._snapshot_preprocess_authority(result, paper_key="paper")

    assert Path(snapped.markdown_path).read_bytes() == paths["markdown_path"].read_bytes()
    assert target.read_bytes() == paths["markdown_path"].read_bytes()


@pytest.mark.parametrize("raise_in_consumer", (False, True))
def test_stage1_preprocess_releases_generation_lease_on_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_in_consumer: bool,
) -> None:
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    release_calls: list[dict[str, str]] = []
    generation_root = tmp_path / "cache" / "generation-lease-test"
    generation_root.mkdir(parents=True)
    manifest_path = generation_root / "prepare_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    preprocess_result = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        manifest_path=str(manifest_path),
        stage1_quality_reasons=[],
        stage1_input_text="Substantive Stage 1 source text.",
        plain_text="Substantive Stage 1 source text.",
        markdown_text="Substantive Stage 1 source text.",
        page_index=[{"page_number": 1}],
    )

    class FakePreprocessManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def prepare_pdf(self, _source_pdf: str, **kwargs: str) -> object:
            assert kwargs["lease_id"]
            assert kwargs["lease_job_id"] == "job-lease"
            assert kwargs["lease_paper_key"] == "paper-lease"
            return preprocess_result

        def release_generation_lease(self, _cache_dir: str, **kwargs: str) -> int:
            release_calls.append(dict(kwargs))
            return 1

    service = object.__new__(Stage1AnalysisService)
    service.job_id = "job-lease"
    service.attempt_id = "attempt-lease"
    service.config = {}
    service.logger = None
    service.workspace = JobWorkspace.create(
        str(tmp_path / "output"), "lease-project", "job-lease"
    )
    monkeypatch.setattr(
        "services.stage1_analysis_service.PreprocessManager",
        FakePreprocessManager,
    )
    monkeypatch.setattr(
        service,
        "_snapshot_preprocess_authority",
        lambda result, *, paper_key: result,
    )

    if raise_in_consumer:
        with (
            pytest.raises(RuntimeError, match="consumer failure"),
            service._preprocess(
                "paper.pdf", paper_key="paper-lease"
            ) as prepared,
        ):
            assert prepared is preprocess_result
            raise RuntimeError("consumer failure")
    else:
        with service._preprocess("paper.pdf", paper_key="paper-lease") as prepared:
            assert prepared is preprocess_result
            assert release_calls == []

    assert len(release_calls) == 1
    assert release_calls[0]["generation_id"] == generation_root.name
    assert release_calls[0]["lease_id"]


@pytest.mark.parametrize(
    ("release_mode", "expected_message"),
    (
        ("raises", "release failure"),
        ("removes_zero", "expected exactly one lease release"),
    ),
)
def test_stage1_preprocess_release_failure_surfaces_and_does_not_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
    expected_message: str,
) -> None:
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    generation_root = tmp_path / "cache" / "generation-lease-corrupt"
    generation_root.mkdir(parents=True)
    manifest_path = generation_root / "prepare_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    preprocess_result = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        manifest_path=str(manifest_path),
        stage1_quality_reasons=[],
        stage1_input_text="Substantive Stage 1 source text.",
        plain_text="Substantive Stage 1 source text.",
        markdown_text="Substantive Stage 1 source text.",
        page_index=[{"page_number": 1}],
    )

    class FakePreprocessManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def prepare_pdf(self, _source_pdf: str, **_kwargs: str) -> object:
            return preprocess_result

        def release_generation_lease(self, _cache_dir: str, **_kwargs: str) -> int:
            if release_mode == "raises":
                raise RuntimeError("release failure")
            return 0

    service = object.__new__(Stage1AnalysisService)
    service.job_id = "job-lease"
    service.attempt_id = "attempt-lease"
    service.config = {}
    service.logger = None
    service.workspace = JobWorkspace.create(
        str(tmp_path / "output"), "lease-project", "job-lease"
    )
    monkeypatch.setattr(
        "services.stage1_analysis_service.PreprocessManager",
        FakePreprocessManager,
    )
    monkeypatch.setattr(
        service,
        "_snapshot_preprocess_authority",
        lambda result, *, paper_key: result,
    )

    with pytest.raises(RuntimeError, match=expected_message):
        with service._preprocess("paper.pdf", paper_key="paper-lease"):
            pass


def test_stage1_preprocess_double_failure_preserves_primary_and_records_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    generation_root = tmp_path / "cache" / "generation-lease-double-failure"
    generation_root.mkdir(parents=True)
    manifest_path = generation_root / "prepare_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    preprocess_result = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        manifest_path=str(manifest_path),
        stage1_quality_reasons=[],
        stage1_input_text="Substantive Stage 1 source text.",
        plain_text="Substantive Stage 1 source text.",
        markdown_text="Substantive Stage 1 source text.",
        page_index=[{"page_number": 1}],
    )
    cleanup_error = RuntimeError("corrupt generation lease payload")

    class FakePreprocessManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def prepare_pdf(self, _source_pdf: str, **_kwargs: str) -> object:
            return preprocess_result

        def release_generation_lease(self, _cache_dir: str, **_kwargs: str) -> int:
            raise cleanup_error

    service = object.__new__(Stage1AnalysisService)
    service.job_id = "job-lease"
    service.attempt_id = "attempt-lease"
    service.config = {}
    service.logger = None
    service.workspace = JobWorkspace.create(
        str(tmp_path / "output"), "lease-project", "job-lease"
    )
    monkeypatch.setattr(
        "services.stage1_analysis_service.PreprocessManager",
        FakePreprocessManager,
    )
    monkeypatch.setattr(
        service,
        "_snapshot_preprocess_authority",
        lambda result, *, paper_key: result,
    )

    with pytest.raises(RuntimeError, match="primary consumer failure") as raised:
        with service._preprocess("paper.pdf", paper_key="paper-lease"):
            raise RuntimeError("primary consumer failure")

    primary_error = raised.value
    assert str(primary_error) == "primary consumer failure"
    assert primary_error.stage1_generation_lease_cleanup_error is cleanup_error
    assert any("corrupt generation lease payload" in note for note in primary_error.__notes__)
    durable_records = list(
        Path(service.workspace.artifact_path(
            "stage1/generation_lease_cleanup_failures"
        )).glob("*.json")
    )
    assert len(durable_records) == 1
    record = json.loads(durable_records[0].read_text(encoding="utf-8"))
    assert record["status"] == "integrity_blocked"
    assert record["cleanup_error_type"] == "RuntimeError"
    assert record["cleanup_error"] == "corrupt generation lease payload"


def test_terminate_interruption_event_cannot_claim_a_graceful_exit() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="non-zero"):
        ProcessInterruptionEventV1.from_mapping(
            {
                "artifact_type": "process_interruption_event",
                "artifact_version": "v1",
                "schema_version": "process-interruption-event-v1",
                "event_id": "event-1",
                "acceptance_run_id": "run-1",
                "scenario_id": "E",
                "job_id": "job-1",
                "attempt_id": "attempt-1",
                "pid": 123,
                "process_creation_identity": "123:1.0",
                "started_at": "2026-01-01T00:00:00Z",
                "interrupted_at": "2026-01-01T00:00:01Z",
                "interruption_method": "terminate",
                "exit_code": 0,
                "last_durable_stage": "analyze",
            }
        )

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
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
from runtime.f1_corpus import F1CorpusManifestV1, F1CorpusSourceRecordV1
from runtime.release_acceptance import (
    AcceptanceScenarioResultV1,
    ParentAcceptanceResultV2,
    ProcessInterruptionEventV1,
    ReleaseAcceptancePlanV2,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
    ScenarioExecutionReceiptV1,
)


def test_acceptance_checkout_rejects_dirty_worktree_before_sha_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ControlPlaneError, ReviewControlPlane

    monkeypatch.setattr(
        "runtime.control_plane.subprocess.run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": " M runtime/control_plane.py\n"})(),
    )
    with pytest.raises(ControlPlaneError, match="clean checkout"):
        ReviewControlPlane._acceptance_checkout_sha(tmp_path, require_clean=True)


@pytest.mark.optional
def test_evidence_paths_reject_original_symlink_before_resolve(tmp_path: Path) -> None:
    from runtime.release_acceptance import GateEvidenceProducer, GateEvidenceVerifier, ReleaseAcceptanceSpecError

    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    link = tmp_path / "evidence-link.json"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink capability unavailable: {type(exc).__name__}")
    with pytest.raises(ReleaseAcceptanceSpecError, match="symlink or reparse"):
        GateEvidenceProducer(final_sha="a" * 40).reference(link, role="stage_terminal")
    with pytest.raises(ReleaseAcceptanceSpecError, match="symlink or reparse"):
        GateEvidenceVerifier._resolve_path(str(link), origin_dir=None)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows handle semantics")
def test_windows_bounded_evidence_read_does_not_reopen_with_path_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.release_acceptance import _bounded_read

    source = tmp_path / "evidence.json"
    source.write_bytes(b'{"verified":true}')
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows evidence read must use the opened handle")
        ),
    )

    assert _bounded_read(source, max_bytes=1024) == b'{"verified":true}'


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


def _f1_manifest_fixture(tmp_path: Path) -> tuple[Path, F1CorpusManifestV1]:
    source_root = tmp_path / "f1-papers"
    source_root.mkdir(exist_ok=True)
    sources: list[F1CorpusSourceRecordV1] = []
    for number in range(1, 16):
        source_id = f"F1-{number:02d}"
        relative_path = f"{source_id}.pdf"
        raw = f"%PDF-1.4\n{source_id}\n".encode("ascii")
        (source_root / relative_path).write_bytes(raw)
        sources.append(
            F1CorpusSourceRecordV1(
                source_id=source_id,
                relative_path=relative_path,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
            )
        )
    manifest_path = tmp_path / "f1-corpus-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "f1_corpus_manifest",
                "artifact_version": "v1",
                "schema_version": "f1-corpus-manifest-v1",
                "corpus_id": "test-f1",
                "source_root": source_root.name,
                "sources": [source.to_dict() for source in sources],
                "content_sha256": F1CorpusManifestV1.content_hash_for("test-f1", sources),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest_path, F1CorpusManifestV1.from_file(
        manifest_path,
        verify_source_files=True,
    )


def _plan_payload(tmp_path: Path) -> dict[str, object]:
    manifest_path, manifest = _f1_manifest_fixture(tmp_path)
    selections = {
        "C": ["F1-01"],
        "D": ["F1-01", "F1-02", "F1-03"],
        "Q": [f"F1-{number:02d}" for number in range(1, 16)],
    }
    scenarios: dict[str, dict[str, object]] = {}
    for gate, source_ids in selections.items():
        workspace = tmp_path / f"workspace-{gate}"
        workspace.mkdir(exist_ok=True)
        runtime_path = tmp_path / f"{gate.lower()}-runtime.json"
        runtime_path.write_text(
            json.dumps(
                {
                    "project_name": f"acceptance-{gate.lower()}",
                    "job_id": f"job-{gate.lower()}",
                    "workspace_path": str(workspace),
                    "source": {"mode": "direct", "pdf_folder": manifest.source_root},
                    "metadata": {
                        "f1_corpus_binding": {
                            "schema_version": "f1-corpus-binding-v1",
                            "manifest_path": str(manifest_path),
                            "manifest_sha256": manifest.manifest_sha256,
                            "source_ids": source_ids,
                        }
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        child = _child(gate, str(runtime_path))
        child.update(
            {
                "workspace": str(workspace),
                "job_id": f"job-{gate.lower()}",
                "input_manifest": str(manifest_path),
                "f1_source_ids": source_ids,
            }
        )
        scenarios[gate] = child
    return {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "parent-acceptance-1",
        "budget": {
            "max_provider_calls_total": 12,
            "max_output_tokens_total": 1000,
            "max_retry_attempts_total": 1,
            "max_wall_seconds": 900,
        },
        "scenarios": scenarios,
    }


def test_parent_plan_requires_independent_child_specs_for_incompatible_gates(tmp_path: Path) -> None:
    plan = ReleaseAcceptancePlanV2.from_mapping(_plan_payload(tmp_path))

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


def test_single_acceptance_spec_preserves_and_validates_executable_sha() -> None:
    spec = ReleaseAcceptanceSpec.from_mapping(
        {
            "final_executable_sha": "a" * 40,
            "gates": ["C"],
            "budget": {
                "max_provider_calls_total": 1,
                "max_output_tokens_total": 1,
                "max_retry_attempts_total": 1,
                "max_wall_seconds": 1,
            },
        }
    )
    assert spec.final_executable_sha == "a" * 40

    with pytest.raises(ReleaseAcceptanceSpecError, match="aliases disagree"):
        ReleaseAcceptanceSpec.from_mapping(
            {
                "final_executable_sha": "a" * 40,
                "executable_sha": "b" * 40,
                "gates": ["C"],
            }
        )


@pytest.mark.parametrize("field", ("workspace", "job_id"))
def test_parent_plan_rejects_shared_child_identity(tmp_path: Path, field: str) -> None:
    payload = _plan_payload(tmp_path)
    payload["scenarios"] = {
        "C": payload["scenarios"]["C"],
        "D": payload["scenarios"]["D"],
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


def test_parent_result_marks_partial_profile_as_scoped_even_when_live_children_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("runtime.release_acceptance._receipt_has_live_authority", lambda receipt: True)
    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={"C": {"status": "PASS", "receipt": _receipt_payload()}},
        required_scenarios=("C",),
    )
    assert result.status == "SCOPED_PASS"
    assert result.ready_to_merge is False


def test_parent_result_allows_offline_k_alongside_verified_live_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("runtime.release_acceptance._receipt_has_live_authority", lambda receipt: True)
    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent-acceptance-1",
        final_executable_sha="a" * 40,
        child_results={
            "C": {"status": "PASS", "receipt": _receipt_payload()},
            "K": {"status": "PASS_OFFLINE", "receipt": _receipt_payload(
                scenario_id="K", gate="K", action_type="offline-contention", budget_domain="offline-k"
            )},
        },
        required_scenarios=("C", "K"),
    )
    assert result.status == "SCOPED_PASS"
    assert result.ready_to_merge is False


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
    payload = _plan_payload(tmp_path)
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
    payload = _plan_payload(tmp_path)
    payload["state_path"] = "acceptance-state.json"
    calls: list[str] = []
    control = ReviewControlPlane(repo_root=Path.cwd())
    monkeypatch.setattr(control, "_acceptance_checkout_sha", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(control, "provider_preflight", lambda **_kwargs: {"ok": True, "route_plan": {}})
    monkeypatch.setattr(
        control,
        "_admit_runtime_spec_external_hosts",
        lambda _spec: {"required": False},
    )

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


def test_parent_acceptance_resumes_child_with_durable_workspace_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload(tmp_path)
    workspace = tmp_path / "workspace-c"
    workspace.mkdir(exist_ok=True)
    (workspace / "artifact_registry.json").write_text("{}", encoding="utf-8")
    child = payload["scenarios"]["C"]
    runtime_path = Path(child["runtime_spec"])
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_payload["workspace_path"] = str(workspace)
    runtime_path.write_text(json.dumps(runtime_payload), encoding="utf-8")
    child["workspace"] = str(workspace)
    child["job_id"] = "job-c"
    payload["parent_run_id"] = "parent-resume"
    payload["state_path"] = "state.json"
    payload["scenarios"] = {"C": child}
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    control = ReviewControlPlane(repo_root=Path.cwd())
    calls: list[str] = []
    monkeypatch.setattr(control, "_acceptance_checkout_sha", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(control, "provider_preflight", lambda **_kwargs: {"ok": True, "route_plan": {}})
    monkeypatch.setattr(
        control,
        "_admit_runtime_spec_external_hosts",
        lambda _spec: {"required": False},
    )
    monkeypatch.setattr(
        control,
        "resume",
        lambda **_kwargs: calls.append("resume")
        or {
            "status": "complete",
            "job_status": "completed",
            "completion_status": "complete",
            "job_id": "job-c",
            "workspace_path": str(workspace),
        },
    )
    monkeypatch.setattr(
        control,
        "run",
        lambda *_args, **_kwargs: calls.append("run")
        or {"status": "complete", "job_id": "job-c", "workspace_path": str(workspace)},
    )

    control.acceptance_run(plan_path)

    assert calls == ["resume"]


def test_parent_acceptance_blocks_runtime_child_after_failed_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload(tmp_path)
    child = payload["scenarios"]["C"]
    runtime_path = Path(child["runtime_spec"])
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_payload["config"] = str(tmp_path / "config.ini")
    runtime_path.write_text(json.dumps(runtime_payload), encoding="utf-8")
    payload["parent_run_id"] = "parent-preflight"
    payload["state_path"] = "state.json"
    payload["scenarios"] = {"C": child}
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    control = ReviewControlPlane(repo_root=Path.cwd())
    monkeypatch.setattr(control, "_acceptance_checkout_sha", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(
        control,
        "provider_preflight",
        lambda **_kwargs: {
            "ok": False,
            "status": "fail",
            "mineru_remote_admission": {"reason": "remote_parser_admission_failed"},
        },
    )
    called = False

    def fail_if_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("child runtime must not execute after failed preflight")

    monkeypatch.setattr(control, "run", fail_if_run)

    result = control.acceptance_run(plan_path)

    assert called is False
    assert result["scenarios"]["C"]["status"] == "BLOCKED"
    assert "preflight did not admit" in result["scenarios"]["C"]["reason"]


def test_gate_d_plan_carries_production_modality_refs_into_child_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published Gate D profile is evidence, not only a Registry side effect."""

    import runtime.release_acceptance as release_acceptance_module
    from runtime.control_plane import ReviewControlPlane
    from runtime.release_acceptance import GateEvidenceProducer

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload(tmp_path)
    now = datetime.now(timezone.utc)
    payload["external_host_acknowledgement"] = {
        "schema_version": "external-host-acknowledgement-v2",
        "version": 2,
        "acknowledged": True,
        "hosts": ["gateway.example"],
        "route_fingerprint": "a" * 64,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    }
    child = payload["scenarios"]["D"]
    workspace = Path(child["workspace"])
    payload["parent_run_id"] = "parent-d"
    payload["state_path"] = "acceptance-state.json"
    payload["scenarios"] = {"D": child}
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    control = ReviewControlPlane(repo_root=Path.cwd())
    monkeypatch.setattr(control, "_acceptance_checkout_sha", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(control, "provider_preflight", lambda **_kwargs: {"ok": True, "route_plan": {}})
    monkeypatch.setattr(
        control,
        "_admit_runtime_spec_external_hosts",
        lambda _spec: {"required": False},
    )
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


def test_f1_source_references_filter_shared_corpus_to_child_selection(
    tmp_path: Path,
) -> None:
    from runtime.control_plane import ReviewControlPlane
    from runtime.job_spec import load_runtime_job_spec

    payload = _plan_payload(tmp_path)
    child = payload["scenarios"]["C"]
    runtime_spec = load_runtime_job_spec(str(child["runtime_spec"]))

    refs = ReviewControlPlane._acceptance_source_references(
        runtime_spec,
        final_sha="a" * 40,
        job_id="job-c",
        gate="C",
        f1_source_ids=tuple(str(item) for item in child["f1_source_ids"]),
    )

    assert len(refs) == 1
    assert refs[0]["path"].endswith("F1-01.pdf")
    assert refs[0]["sha256"] == F1CorpusManifestV1.from_file(
        str(payload["scenarios"]["C"]["input_manifest"]),
        verify_source_files=True,
    ).source_by_id("F1-01").sha256


def test_acceptance_scenario_deduplicates_repeated_durable_references(
    tmp_path: Path,
) -> None:
    from runtime.release_acceptance import GateDScenario, GateEvidenceProducer

    evidence = tmp_path / "runtime-spec.json"
    evidence.write_text("{}", encoding="utf-8")
    ref = GateEvidenceProducer(final_sha="a" * 40).reference(
        evidence,
        role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        job_id="job-d",
    )

    selected, error = GateDScenario()._durable_refs((ref, ref))

    assert error is None
    assert len(selected) == 1


@pytest.mark.parametrize("mismatch", ("workspace", "job_id"))
def test_runtime_child_rejects_plan_to_runtime_identity_mismatch_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    from runtime.control_plane import ReviewControlPlane

    monkeypatch.setenv("AUTO_GENERATE_RUN_LIVE_ACCEPTANCE", "1")
    payload = _plan_payload(tmp_path)
    child = payload["scenarios"]["D"]
    expected_workspace = tmp_path / "workspace-d"
    expected_workspace.mkdir(exist_ok=True)
    runtime_workspace = (
        tmp_path / "other-workspace" if mismatch == "workspace" else expected_workspace
    )
    expected_job_id = "job-d"
    runtime_job_id = "other-job" if mismatch == "job_id" else expected_job_id
    runtime_spec = Path(child["runtime_spec"])
    runtime_payload = json.loads(runtime_spec.read_text(encoding="utf-8"))
    runtime_payload["job_id"] = runtime_job_id
    runtime_payload["workspace_path"] = str(runtime_workspace)
    runtime_spec.write_text(json.dumps(runtime_payload), encoding="utf-8")
    child["workspace"] = str(expected_workspace)
    child["job_id"] = expected_job_id
    payload["parent_run_id"] = f"parent-{mismatch}"
    payload["state_path"] = "acceptance-state.json"
    payload["scenarios"] = {"D": child}
    plan_path = tmp_path / "acceptance-plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    calls: list[str] = []
    control = ReviewControlPlane(repo_root=Path.cwd())
    monkeypatch.setattr(control, "_acceptance_checkout_sha", lambda *_args, **_kwargs: "a" * 40)
    monkeypatch.setattr(control, "provider_preflight", lambda **_kwargs: {"ok": True, "route_plan": {}})
    monkeypatch.setattr(
        control,
        "_admit_runtime_spec_external_hosts",
        lambda _spec: {"required": False},
    )
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


def test_stage1_preprocess_blocks_quality_reprocess_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.job_workspace import JobWorkspace
    from services.stage1_analysis_service import Stage1AnalysisService

    generation_root = tmp_path / "cache" / "generation-quality-block"
    generation_root.mkdir(parents=True)
    (generation_root / "prepare_manifest.json").write_text("{}", encoding="utf-8")
    preprocess_result = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        manifest_path=str(generation_root / "prepare_manifest.json"),
        stage1_quality_reasons=["incomplete_by_page_count"],
        stage1_quality_level="REPROCESS",
        stage1_input_text="",
        plain_text="",
        markdown_text="",
        page_index=[{"page_number": 1}],
    )
    release_calls: list[dict[str, str]] = []

    class FakePreprocessManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def prepare_pdf(self, _source_pdf: str, **_kwargs: str) -> object:
            return preprocess_result

        def release_generation_lease(self, _cache_dir: str, **kwargs: str) -> int:
            release_calls.append(dict(kwargs))
            return 1

    service = object.__new__(Stage1AnalysisService)
    service.job_id = "job-quality"
    service.attempt_id = "attempt-quality"
    service.config = {}
    service.logger = None
    service.workspace = JobWorkspace.create(
        str(tmp_path / "output"), "quality-project", "job-quality"
    )
    monkeypatch.setattr(
        "services.stage1_analysis_service.PreprocessManager", FakePreprocessManager
    )
    snapshot_called = False

    def snapshot(_result: object, *, paper_key: str) -> object:
        nonlocal snapshot_called
        snapshot_called = True
        return _result

    monkeypatch.setattr(service, "_snapshot_preprocess_authority", snapshot)
    with pytest.raises(RuntimeError, match="preprocessing is incomplete"):
        with service._preprocess("paper.pdf", paper_key="paper-quality"):
            raise AssertionError("quality-blocked preprocessing must not yield")

    assert snapshot_called is False
    assert release_calls


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

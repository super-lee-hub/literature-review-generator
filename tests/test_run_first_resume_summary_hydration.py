"""Canonical Stage 1 summary hydration for recovered Analyze results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from runtime.reconcile import RuntimeReconciler
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_contracts import StageResult
from runtime.orchestrator import InternalStageExecutorRegistry
from services.artifact_registry import RegistryError
from tests.test_runtime_bridge_helpers import (
    build_success_summary,
    make_bridge_session,
    write_json,
)


def _persist_reconciled_analyze_result(
    tmp_path: Path,
) -> tuple[Any, Any, Any, Path, Path, StageResult]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bridge, session, pdf_dir, pdf_path = make_bridge_session(
        tmp_path,
        action="analyze",
    )
    bridge.persist_source_bundle(session, bridge.build_source_bundle())

    summary = build_success_summary(pdf_path)
    stage1_result = bridge.persist_stage1_results(
        session,
        [summary],
        source_items=[{
            "path": str(pdf_dir),
            "source_type": "direct",
            "label": "pdf-folder",
            "priority": 0,
        }],
    )
    assert stage1_result.success is True
    assert stage1_result.stage_name == "stage1_analyze"

    runner = AgentRuntimeRunner(bridge.job_spec)
    resolver = runner._external_registry_resolver(session.context.workspace)
    analyze_result = replace(stage1_result, stage_name="analyze")
    runner._persist_terminal(
        session,
        attempt_id="attempt-summary-hydration",
        stage_name="analyze",
        result=analyze_result,
        started_at="2026-10-17T00:00:00Z",
        model_call_count=0,
        external_registry_resolver=resolver,
    )
    reconciler = RuntimeReconciler(
        session.context.workspace,
        session.context.registry,
        schema_validators=runner._schema_validators(),
        external_registry_resolver=resolver,
    )
    recovered = reconciler.load_completed_stage_result("analyze")
    assert recovered is not None
    assert recovered.stage_name == "analyze"
    return bridge, session, runner, pdf_dir, pdf_path, recovered


def _summary_ref(result: StageResult):
    matches = [item for item in result.artifacts if item.artifact_type == "summary_file"]
    assert len(matches) == 1
    return matches[0]


def _resolver(runner: AgentRuntimeRunner, session: Any):
    return runner._external_registry_resolver(session.context.workspace)


def test_recovered_analyze_hydrates_only_registered_canonical_summary(
    tmp_path: Path,
) -> None:
    bridge, session, runner, pdf_dir, _pdf_path, recovered = (
        _persist_reconciled_analyze_result(tmp_path)
    )
    summary_ref = _summary_ref(recovered)
    summary_record = session.context.registry.get(summary_ref.artifact_id)
    assert summary_record is not None and summary_record.status == "ready"
    canonical_rows = InternalStageExecutorRegistry._summary_payloads_from_file(
        summary_record.path
    )
    assert len(canonical_rows) == 1
    canonical = canonical_rows[0]
    assert canonical["ai_summary"]
    assert canonical["paper_info"]["source_pdf_fingerprint"]
    assert canonical["preprocess"]["markdown_path"]
    assert canonical["stage1_input"]["input_mode"] == "text"

    # This represents the already-verified imported summary projection from the
    # original request. Its scientific fields match the local canonical row;
    # only reuse/provider bookkeeping differs.
    raw_input = deepcopy(canonical)
    raw_input["provider"] = {
        "route": "Stage1Reuse",
        "transport_count": 0,
        "receipt_ids": [],
    }
    raw_input["stage1_reuse"] = {
        "authority_kind": "typed_manifest",
        "binding": {"canonical_paper_key": "paper_a"},
    }
    raw_input["reuse_metadata"] = {"decision": "exact_summary_reuse"}
    differing_fields = {
        key
        for key in set(canonical) | set(raw_input)
        if canonical.get(key) != raw_input.get(key)
    }
    assert differing_fields == {"provider", "stage1_reuse", "reuse_metadata"}

    raw_summary_path = tmp_path / "verified-typed-summary-input.json"
    write_json(raw_summary_path, [raw_input])
    object.__setattr__(
        session,
        "request",
        replace(
            session.request,
            reuse_stage1=True,
            reuse_summary_files=(str(raw_summary_path),),
            runtime_spec_payload=None,
        ),
    )
    session.stage_host.summaries = []
    session.stage_host.summary_file = ""

    executor_registry = InternalStageExecutorRegistry(bridge)
    before_hydration = executor_registry._load_summary_payloads(
        session,
        {"analyze": recovered},
    )
    assert len(before_hydration) == 2
    assert [row["paper_info"]["canonical_paper_key"] for row in before_hydration] == [
        "paper_a",
        "paper_a",
    ]

    AgentRuntimeRunner._restore_analyze_host_summaries(
        session,
        recovered,
        external_registry_resolver=_resolver(runner, session),
    )

    assert session.stage_host.summary_file == summary_record.path
    assert session.stage_host.summaries == canonical_rows
    after_hydration = executor_registry._load_summary_payloads(
        session,
        {"analyze": recovered},
    )
    assert after_hydration == canonical_rows
    assert len(after_hydration) == 1
    assert after_hydration[0]["ai_summary"] == canonical["ai_summary"]
    assert after_hydration[0]["paper_info"] == canonical["paper_info"]
    assert after_hydration[0]["preprocess"] == canonical["preprocess"]
    assert after_hydration[0]["stage1_input"] == canonical["stage1_input"]
    assert "stage1_reuse" not in after_hydration[0]
    assert "reuse_metadata" not in after_hydration[0]


def test_recovered_analyze_rejects_missing_or_ambiguous_summary_output(
    tmp_path: Path,
) -> None:
    _bridge, session, _runner, _pdf_dir, _pdf_path, recovered = (
        _persist_reconciled_analyze_result(tmp_path)
    )
    summary_ref = _summary_ref(recovered)
    non_summary_refs = [
        item for item in recovered.artifacts if item.artifact_type != "summary_file"
    ]
    missing = replace(recovered, artifacts=non_summary_refs)
    ambiguous = replace(recovered, artifacts=[*recovered.artifacts, summary_ref])

    with pytest.raises(RuntimeRunnerError, match="one canonical summary output"):
        AgentRuntimeRunner._restore_analyze_host_summaries(session, missing)
    with pytest.raises(RuntimeRunnerError, match="one canonical summary output"):
        AgentRuntimeRunner._restore_analyze_host_summaries(session, ambiguous)


def test_recovered_analyze_rejects_a_foreign_summary_reference(
    tmp_path: Path,
) -> None:
    _local_bridge, local_session, _local_runner, _local_dir, _local_pdf, _local_result = (
        _persist_reconciled_analyze_result(tmp_path / "local")
    )
    _foreign_bridge, _foreign_session, _foreign_runner, _foreign_dir, _foreign_pdf, foreign_result = (
        _persist_reconciled_analyze_result(tmp_path / "foreign")
    )
    foreign_ref = _summary_ref(foreign_result)
    foreign = replace(_local_result, artifacts=[foreign_ref])

    with pytest.raises(RuntimeRunnerError, match="no matching Registry authority"):
        AgentRuntimeRunner._restore_analyze_host_summaries(local_session, foreign)


def test_recovered_analyze_rejects_source_pdf_hash_drift_in_summary_closure(
    tmp_path: Path,
) -> None:
    _bridge, session, runner, _pdf_dir, pdf_path, recovered = (
        _persist_reconciled_analyze_result(tmp_path)
    )
    pdf_path.write_bytes(pdf_path.read_bytes() + b"tampered-after-Stage1")

    with pytest.raises(RegistryError):
        AgentRuntimeRunner._restore_analyze_host_summaries(
            session,
            recovered,
            external_registry_resolver=_resolver(runner, session),
        )

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import runtime.runner as runner_module
import services
import tests.test_stage1_closure_adversarial as stage1_fixture

_REPO_TESTS = Path.cwd() / "tests"
if (_REPO_TESTS / "test_summary_correction_reuse.py").is_file():
    sys.path.insert(0, str(_REPO_TESTS))


def _load_sealed_v2_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    path = Path(__file__).resolve().parent / "test_automatic_stage1_reuse_v2.py"
    spec = importlib.util.spec_from_file_location(
        "tests.test_automatic_stage1_reuse_v2_sealed_fixture",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module




def _activate_integrity_patch(
    monkeypatch: pytest.MonkeyPatch,
    v2_fixture: Any,
) -> tuple[Any, Any]:
    helper = v2_fixture._load_module(
        monkeypatch,
        "services.automatic_stage1_reuse",
        Path(__file__).resolve().parents[1]
        / "services"
        / "automatic_stage1_reuse.py",
    )
    monkeypatch.setattr(services, "automatic_stage1_reuse", helper, raising=False)
    patched_orchestrator = v2_fixture._load_module(
        monkeypatch,
        "runtime.orchestrator_automatic_stage1_reuse_integrity_test",
        Path(__file__).resolve().parents[1]
        / "runtime"
        / "orchestrator.py",
    )
    monkeypatch.setattr(
        runner_module,
        "AgentRuntimeBridge",
        patched_orchestrator.AgentRuntimeBridge,
    )
    return helper, patched_orchestrator


def test_tampered_v2_body_blocks_registry_proven_key_but_keeps_unrelated_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2_fixture = _load_sealed_v2_fixture(monkeypatch)
    helper, patched_orchestrator = _activate_integrity_patch(monkeypatch, v2_fixture)
    output_root = tmp_path / "output"
    output_root.mkdir()
    v2_fixture._route_workspaces_to_root(monkeypatch, output_root)

    (
        _origin_workspace,
        origin_registry,
        _source_record,
        _summaries,
        target_pdf,
        _target_pdf_hash,
        _correction_workspace,
        correction_registry,
        _prior_v1,
        v2_record,
        _exported,
    ) = v2_fixture._export_fixture(
        tmp_path=tmp_path / "approved-v2",
        output_root=output_root,
        monkeypatch=monkeypatch,
    )

    unrelated_papers = stage1_fixture._stage1_papers(
        tmp_path / "unrelated-input",
        prefix="integrity-survivor",
    )
    unrelated_result, _unrelated_workspace, unrelated_registry = stage1_fixture._run_stage1(
        tmp_path / "unrelated-cold",
        monkeypatch,
        job_id="integrity-survivor-cold",
        papers=unrelated_papers,
        output_root=output_root,
    )
    assert unrelated_result.completion_status == "complete", unrelated_result
    source_scope = unrelated_registry.get("stage1_source_scope")
    assert source_scope is not None and source_scope.status == "ready"
    scope_payload = json.loads(Path(source_scope.path).read_text(encoding="utf-8"))
    scope_body = (
        scope_payload.get("payload")
        if isinstance(scope_payload, Mapping)
        and isinstance(scope_payload.get("payload"), Mapping)
        else scope_payload
    )
    assert isinstance(scope_body, Mapping) and isinstance(scope_body.get("papers"), list)
    survivor_row = next(
        row
        for row in scope_body["papers"]
        if isinstance(row, Mapping)
    )
    survivor_key = str(survivor_row["paper_key"])
    survivor_pdf = Path(str(survivor_row["source_pdf"]))
    assert survivor_key and survivor_pdf.is_file()

    current_workspace = v2_fixture.JobWorkspace.create(
        str(output_root),
        "integrity-current",
        "integrity-current-child",
    )
    registries = {
        origin_registry.job_id: origin_registry,
        correction_registry.job_id: correction_registry,
        unrelated_registry.job_id: unrelated_registry,
    }

    paper_work_items = [
        SimpleNamespace(
            canonical_paper_key=v2_fixture.TARGET_KEY,
            source_pdf=str(target_pdf),
        ),
        SimpleNamespace(
            canonical_paper_key=survivor_key,
            source_pdf=str(survivor_pdf),
        ),
    ]
    summary_payloads_from_file = (
        patched_orchestrator.InternalStageExecutorRegistry._summary_payloads_from_file
    )
    resolver = lambda job_id: registries.get(job_id)
    valid_plan = helper.discover_automatic_stage1_reuse(
        base_output_dir=output_root,
        current_workspace_dir=current_workspace.root_dir,
        current_job_id=current_workspace.job_id,
        paper_work_items=paper_work_items,
        summary_payloads_from_file=summary_payloads_from_file,
        external_registry_resolver=resolver,
    )
    assert valid_plan.candidates_by_key[v2_fixture.TARGET_KEY].manifest_record_version == "v2"
    assert valid_plan.candidates_by_key[survivor_key].manifest_record_version == "v1"

    tampered = json.loads(Path(v2_record.path).read_text(encoding="utf-8"))
    tampered["canonical_paper_key"] = "tampered-unrelated-key"
    tampered["source_pdf_content_sha256"] = "f" * 64
    binding = tampered.get("binding")
    assert isinstance(binding, dict)
    binding["source_pdf_content_sha256"] = "f" * 64
    Path(v2_record.path).write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    plan = helper.discover_automatic_stage1_reuse(
        base_output_dir=output_root,
        current_workspace_dir=current_workspace.root_dir,
        current_job_id=current_workspace.job_id,
        paper_work_items=paper_work_items,
        summary_payloads_from_file=summary_payloads_from_file,
        external_registry_resolver=resolver,
    )

    assert v2_fixture.TARGET_KEY not in plan.candidates_by_key
    assert survivor_key in plan.candidates_by_key
    assert plan.candidates_by_key[survivor_key].manifest_record_version == "v1"
    assert any(
        row.get("canonical_paper_key") == v2_fixture.TARGET_KEY
        and row.get("reason") == "automatic_reuse_unverified_v2_owner_quarantines_key"
        for row in plan.rejected_candidates
    )

    Path(v2_record.path).write_bytes(b"{malformed v2 json")
    malformed_plan = helper.discover_automatic_stage1_reuse(
        base_output_dir=output_root,
        current_workspace_dir=current_workspace.root_dir,
        current_job_id=current_workspace.job_id,
        paper_work_items=paper_work_items,
        summary_payloads_from_file=summary_payloads_from_file,
        external_registry_resolver=resolver,
    )
    assert v2_fixture.TARGET_KEY not in malformed_plan.candidates_by_key
    assert survivor_key in malformed_plan.candidates_by_key
    assert any(
        row.get("canonical_paper_key") == v2_fixture.TARGET_KEY
        and row.get("reason") == "automatic_reuse_unverified_v2_owner_quarantines_key"
        for row in malformed_plan.rejected_candidates
    )

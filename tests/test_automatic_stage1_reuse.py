from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import runtime.runner as runner_module
import services
import tests.test_run_first_typed_validation_authority as validation_fixture
import tests.test_stage1_closure_adversarial as stage1_fixture
from services.artifact_registry import file_sha256
from validation.closure import resolve_current_stage_closure_map
from validation.evidence_loader import PreprocessEvidenceLoader

PATCH_ROOT = Path(__file__).resolve().parents[1]


def _load_module(monkeypatch: pytest.MonkeyPatch, module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _activate_patch(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    helper = _load_module(
        monkeypatch,
        "services.automatic_stage1_reuse",
        PATCH_ROOT / "services" / "automatic_stage1_reuse.py",
    )
    monkeypatch.setattr(services, "automatic_stage1_reuse", helper, raising=False)
    patched_orchestrator = _load_module(
        monkeypatch,
        "runtime.orchestrator_automatic_stage1_reuse_test",
        PATCH_ROOT / "runtime" / "orchestrator.py",
    )
    monkeypatch.setattr(runner_module, "AgentRuntimeBridge", patched_orchestrator.AgentRuntimeBridge)
    monkeypatch.setattr(validation_fixture, "AgentRuntimeBridge", patched_orchestrator.AgentRuntimeBridge)
    return helper, patched_orchestrator


def _seed_papers(root: Path, prefix: str = "automatic") -> list[tuple[str, str, str]]:
    return stage1_fixture._stage1_papers(root, prefix=prefix)


def _seed_pdf_map(registry: Any) -> dict[str, Path]:
    record = registry.get("stage1_source_scope")
    assert record is not None and record.status == "ready"
    raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    payload = raw.get("payload") if isinstance(raw, Mapping) and isinstance(raw.get("payload"), Mapping) else raw
    if not isinstance(payload, Mapping) or not isinstance(payload.get("papers"), list):
        raise TypeError("registered Stage 1 source scope has no paper rows")
    return {
        str(row["paper_key"]): Path(row["source_pdf"])
        for row in payload["papers"]
        if isinstance(row, Mapping)
    }


def _copy_source_scope(source_registry: Any, target_root: Path) -> None:
    target_dir = target_root / "source-papers"
    target_dir.mkdir(parents=True, exist_ok=True)
    for source_pdf in _seed_pdf_map(source_registry).values():
        shutil.copyfile(source_pdf, target_dir / source_pdf.name)


def _manifest_paths(registry: Any) -> tuple[str, ...]:
    records = sorted(
        (
            record
            for record in registry.list_records()
            if record.status == "ready"
            and record.artifact_type == "stage1_reusable_summary_manifest"
            and record.artifact_version == "v1"
        ),
        key=lambda record: record.artifact_id,
    )
    return tuple(record.path for record in records)


def _items(registry: Any) -> list[dict[str, Any]]:
    record = registry.get("summary_source_manifest")
    assert record is not None and record.status == "ready"
    raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    payload = raw.get("payload") if isinstance(raw, Mapping) and isinstance(raw.get("payload"), Mapping) else raw
    if not isinstance(payload, Mapping) or not isinstance(payload.get("source_items"), list):
        raise TypeError("registered Stage 1 source manifest has no source items")
    return list(payload["source_items"])


def _read_summary_rows(registry: Any) -> list[dict[str, Any]]:
    record = registry.get("summary_file")
    assert record is not None and record.status == "ready"
    raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else list(raw.get("summaries") or [])


def _reader_counter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = stage1_fixture._reader_summary

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(args[0] if args else kwargs.get("canonical_paper_key") or ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(stage1_fixture, "_reader_summary", counted)
    return calls


def _validation_readback(
    *,
    cold_workspace: Any,
    cold_registry: Any,
    warm_workspace: Any,
    warm_registry: Any,
    expected_owner_by_key: Mapping[str, str],
) -> None:
    case = SimpleNamespace(
        parent_workspace=cold_workspace,
        parent_registry=cold_registry,
        child_workspace=warm_workspace,
        child_registry=warm_registry,
        manifest_paths=(),
    )
    service = validation_fixture._validation_service(case)
    assert service.validation_source_binding_record is not None
    _review, _manifest, papers, preprocess, _metadata = validation_fixture._load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    assert len(papers) == len(expected_owner_by_key)
    assert set(expected_owner_by_key).issubset(preprocess)
    fingerprint, _identity, problems = validation_fixture._fingerprint(
        service, papers, sorted(expected_owner_by_key)
    )
    assert problems == (), problems
    assert {
        row["canonical_paper_key"]: row["source_job_id"]
        for row in fingerprint["papers"]
    } == dict(expected_owner_by_key)
    for key in expected_owner_by_key:
        paper = next(
            paper for paper in papers
            if paper["paper_identity"]["canonical_paper_key"] == key
        )
        manifest_path = str(paper["stage1_inputs"]["evidence_manifest_path"])
        raw_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        evidence_manifest = (
            raw_manifest.get("payload")
            if isinstance(raw_manifest, Mapping) and isinstance(raw_manifest.get("payload"), Mapping)
            else raw_manifest
        )
        if not isinstance(evidence_manifest, Mapping) or not isinstance(
            evidence_manifest.get("artifacts"), list
        ):
            raise TypeError("evidence manifest has no artifact list")
        leaves = {
            str(row["artifact_type"]): str(row["path"])
            for row in evidence_manifest["artifacts"]
            if isinstance(row, Mapping)
        }
        loaded = PreprocessEvidenceLoader().load_evidence(
            normalized_text_path=leaves["normalized_text"],
            chunks_path=leaves["chunks"],
            page_index_path=leaves["page_index"],
            manifest_path=manifest_path,
            strict=True,
        )
        assert loaded.normalized_text
        assert loaded.chunks and loaded.page_index


def test_automatic_reuse_exact_warm_validates_original_owners_and_explicit_pool_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper, _patched = _activate_patch(monkeypatch)
    discover_calls: list[str] = []
    original_discover = helper.discover_automatic_stage1_reuse

    def counted_discover(**kwargs: Any) -> Any:
        discover_calls.append(str(kwargs.get("current_job_id") or ""))
        return original_discover(**kwargs)

    monkeypatch.setattr(helper, "discover_automatic_stage1_reuse", counted_discover)
    reader_calls = _reader_counter(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    papers = _seed_papers(tmp_path / "input", prefix="automatic")

    cold_result, cold_workspace, cold_registry = stage1_fixture._run_stage1(
        tmp_path / "cold",
        monkeypatch,
        job_id="automatic-cold",
        papers=papers,
        output_root=output_root,
    )
    assert cold_result.completion_status == "complete", cold_result
    cold_calls = list(reader_calls)
    assert len(cold_calls) == 3

    warm_root = tmp_path / "warm"
    _copy_source_scope(cold_registry, warm_root)
    reader_calls.clear()
    warm_result, warm_workspace, warm_registry = stage1_fixture._run_stage1(
        warm_root,
        monkeypatch,
        job_id="automatic-warm",
        papers=papers,
        reuse_stage1=True,
        reuse_summary_files=(),
        output_root=output_root,
    )
    assert warm_result.completion_status == "complete", warm_result
    assert reader_calls == []
    assert len(discover_calls) == 2
    warm_items = _items(warm_registry)
    assert {str(item["disposition"]) for item in warm_items} == {"reused"}
    assert all(item.get("reuse_evidence_id") for item in warm_items)
    assert all(isinstance(item.get("automatic_stage1_reuse_owner"), Mapping) for item in warm_items)
    assert not warm_registry.get("stage1_provider_receipts")
    closure = resolve_current_stage_closure_map(warm_registry)
    assert closure.blocking_issues == (), closure.to_dict()
    analyze = closure.provider_closures_by_stage["analyze"]
    assert analyze["complete"] is True
    assert analyze["expected_call_ids"] == analyze["observed_call_ids"] == []

    expected_keys = {str(item["canonical_paper_key"]) for item in warm_items}
    _validation_readback(
        cold_workspace=cold_workspace,
        cold_registry=cold_registry,
        warm_workspace=warm_workspace,
        warm_registry=warm_registry,
        expected_owner_by_key={key: cold_workspace.job_id for key in expected_keys},
    )

    explicit_root = tmp_path / "explicit"
    _copy_source_scope(cold_registry, explicit_root)
    reader_calls.clear()
    explicit_result, _explicit_workspace, explicit_registry = stage1_fixture._run_stage1(
        explicit_root,
        monkeypatch,
        job_id="automatic-explicit",
        papers=papers,
        reuse_stage1=True,
        reuse_summary_files=_manifest_paths(cold_registry),
        output_root=output_root,
    )
    assert explicit_result.completion_status == "complete", explicit_result
    assert reader_calls == []
    assert len(discover_calls) == 2
    assert {str(item["disposition"]) for item in _items(explicit_registry)} == {"reused"}
    assert all("automatic_stage1_reuse_owner" not in item for item in _items(explicit_registry))


def test_automatic_reuse_changed_pdf_regenerates_only_that_paper_and_keeps_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate_patch(monkeypatch)
    reader_calls = _reader_counter(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    papers = _seed_papers(tmp_path / "input", prefix="changed")
    cold_result, cold_workspace, cold_registry = stage1_fixture._run_stage1(
        tmp_path / "cold",
        monkeypatch,
        job_id="changed-cold",
        papers=papers,
        output_root=output_root,
    )
    assert cold_result.completion_status == "complete", cold_result
    reader_calls.clear()

    warm_root = tmp_path / "warm"
    _copy_source_scope(cold_registry, warm_root)
    target = warm_root / "source-papers" / "changed-a.pdf"
    stage1_fixture._write_pdf(target, "Study A", "A changed PDF source with different content.")
    warm_result, warm_workspace, warm_registry = stage1_fixture._run_stage1(
        warm_root,
        monkeypatch,
        job_id="changed-warm",
        papers=papers,
        reuse_stage1=True,
        reuse_summary_files=(),
        output_root=output_root,
    )
    assert warm_result.completion_status == "complete", warm_result
    assert len(reader_calls) == 1
    warm_items = {str(item["canonical_paper_key"]): item for item in _items(warm_registry)}
    generated_keys = {
        key for key, item in warm_items.items() if item["disposition"] == "provider_generated"
    }
    assert len(generated_keys) == 1
    assert generated_keys == {reader_calls[0]}
    assert all(
        item.get("reuse_evidence_id")
        for key, item in warm_items.items()
        if key not in generated_keys
    )
    source_manifest_record = warm_registry.get("summary_source_manifest")
    assert source_manifest_record is not None and source_manifest_record.status == "ready"
    source_manifest_payload = json.loads(
        Path(source_manifest_record.path).read_text(encoding="utf-8")
    )
    assert any(
        item.get("reason") == "automatic_reuse_source_pdf_hash_mismatch"
        for item in source_manifest_payload.get("rejected_candidates", [])
    )
    owner_by_key = {
        key: (cold_workspace.job_id if key not in generated_keys else warm_workspace.job_id)
        for key in warm_items
    }
    _validation_readback(
        cold_workspace=cold_workspace,
        cold_registry=cold_registry,
        warm_workspace=warm_workspace,
        warm_registry=warm_registry,
        expected_owner_by_key=owner_by_key,
    )


def test_automatic_reuse_tampered_manifest_only_regenerates_affected_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate_patch(monkeypatch)
    reader_calls = _reader_counter(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    papers = _seed_papers(tmp_path / "input", prefix="tampered")
    cold_result, cold_workspace, cold_registry = stage1_fixture._run_stage1(
        tmp_path / "cold",
        monkeypatch,
        job_id="tampered-cold",
        papers=papers,
        output_root=output_root,
    )
    assert cold_result.completion_status == "complete", cold_result
    scope_keys = set(_seed_pdf_map(cold_registry))
    target_key = min(scope_keys)
    target_record = next(
        record
        for record in cold_registry.list_records()
        if record.status == "ready"
        and record.artifact_type == "stage1_reusable_summary_manifest"
        and json.loads(Path(record.path).read_text(encoding="utf-8"))["canonical_paper_key"] == target_key
    )
    Path(target_record.path).write_text(
        Path(target_record.path).read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    reader_calls.clear()

    warm_root = tmp_path / "warm"
    _copy_source_scope(cold_registry, warm_root)
    warm_result, warm_workspace, warm_registry = stage1_fixture._run_stage1(
        warm_root,
        monkeypatch,
        job_id="tampered-warm",
        papers=papers,
        reuse_stage1=True,
        reuse_summary_files=(),
        output_root=output_root,
    )
    assert warm_result.completion_status == "complete", warm_result
    assert len(reader_calls) == 1
    items = {str(item["canonical_paper_key"]): item for item in _items(warm_registry)}
    generated = {key for key, item in items.items() if item["disposition"] == "provider_generated"}
    assert generated == {target_key} == {reader_calls[0]}
    assert sum(item["disposition"] == "reused" for item in items.values()) == 2
    owner_by_key = {
        key: (warm_workspace.job_id if key == target_key else cold_workspace.job_id)
        for key in items
    }
    _validation_readback(
        cold_workspace=cold_workspace,
        cold_registry=cold_registry,
        warm_workspace=warm_workspace,
        warm_registry=warm_registry,
        expected_owner_by_key=owner_by_key,
    )


def test_automatic_reuse_conflicting_owner_quarantines_only_conflicted_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate_patch(monkeypatch)
    reader_calls = _reader_counter(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    papers = _seed_papers(tmp_path / "input", prefix="conflict")
    cold_result, _cold_workspace, cold_registry = stage1_fixture._run_stage1(
        tmp_path / "cold",
        monkeypatch,
        job_id="z-conflict-cold",
        papers=papers,
        output_root=output_root,
    )
    assert cold_result.completion_status == "complete", cold_result

    second_papers = [
        (key, title, "Different authored finding for A." if key.endswith("-a") else finding)
        for key, title, finding in papers
    ]
    second_root = tmp_path / "second"
    _copy_source_scope(cold_registry, second_root)
    second_result, _second_workspace, second_registry = stage1_fixture._run_stage1(
        second_root,
        monkeypatch,
        job_id="a-conflict-second",
        papers=second_papers,
        reuse_stage1=False,
        output_root=output_root,
    )
    assert second_result.completion_status == "complete", second_result
    assert all(
        file_sha256(left) == file_sha256(right)
        for left, right in zip(
            _seed_pdf_map(cold_registry).values(),
            _seed_pdf_map(second_registry).values(),
            strict=True,
        )
    )
    reader_calls.clear()

    warm_root = tmp_path / "warm"
    _copy_source_scope(cold_registry, warm_root)
    warm_result, _warm_workspace, warm_registry = stage1_fixture._run_stage1(
        warm_root,
        monkeypatch,
        job_id="conflict-warm",
        papers=papers,
        reuse_stage1=True,
        reuse_summary_files=(),
        output_root=output_root,
    )
    assert warm_result.completion_status == "complete", warm_result
    assert len(reader_calls) == 1
    items = {str(item["canonical_paper_key"]): item for item in _items(warm_registry)}
    generated = {key for key, item in items.items() if item["disposition"] == "provider_generated"}
    assert len(generated) == 1
    assert generated == {reader_calls[0]}
    manifest_record = warm_registry.get("summary_source_manifest")
    assert manifest_record is not None
    manifest_payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    conflicts = [
        row for row in manifest_payload.get("rejected_candidates", [])
        if row.get("reason") == "automatic_reuse_conflicting_non_equivalent_owners"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["canonical_paper_key"] == reader_calls[0]
    assert sum(item["disposition"] == "reused" for item in items.values()) == 2

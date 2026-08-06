from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace
from validation.closure import resolve_current_stage_closure_map
from tests.test_current_runtime_full_e2e import _reader_summary, _test_config, _write_pdf


def _stage1_papers(root: Path, prefix: str = "paper") -> list[tuple[str, str, str]]:
    papers = [
        (f"{prefix}-a", "Study A", "The treatment improved the outcome."),
        (f"{prefix}-b", "Study B", "The treatment improved the outcome in a second context."),
        (f"{prefix}-c", "Study C", "The treatment improved the outcome under a third condition."),
    ]
    root.mkdir(parents=True, exist_ok=True)
    for key, title, finding in papers:
        pdf_path = root / f"{key}.pdf"
        # Keep the same immutable PDF bytes across repeated runtime setup.
        # PyMuPDF embeds creation metadata when it rewrites an existing file;
        # rewriting here would turn an all-reuse fixture into a new source.
        if not pdf_path.exists():
            _write_pdf(pdf_path, title, finding)
    return papers


def _run_stage1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_id: str,
    papers: list[tuple[str, str, str]],
    summary_file: str = "",
    reuse_summary_files: tuple[str, ...] = (),
    reuse_stage1: bool | None = None,
    requested_stages: tuple[str, ...] = ("analyze",),
    action: str = "analyze",
) -> tuple[Any, JobWorkspace, ArtifactRegistry]:
    pdf_dir = tmp_path / ("empty-papers" if not papers else "source-papers")
    if papers:
        _stage1_papers(pdf_dir, prefix=papers[0][0].rsplit("-", 1)[0])
    else:
        pdf_dir.mkdir(parents=True, exist_ok=True)
    by_key = {key: (title, finding) for key, title, finding in papers}

    def configured_reader(
        _service: Any,
        *,
        item: Any,
        built_input: Any,
        primary_config: Mapping[str, Any],
        backup_config: Mapping[str, Any],
        runtime: Any,
    ) -> Mapping[str, Any]:
        del built_input, primary_config, backup_config, runtime
        key = str(item.canonical_paper_key)
        title = str(item.paper_info.get("title") or "")
        title, finding = by_key.get(title, by_key.get(key, (title, "")))
        summary = _reader_summary(key, title, finding)
        summary["paper_info"]["source_paper_id"] = item.source_paper_id
        return {"status": "success", "content": summary}

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
        configured_reader,
    )
    spec = RuntimeJobSpec(
        project_name=job_id,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id=job_id,
        config=str(_test_config(tmp_path)),
        action=action,
        summary_file=summary_file,
        reuse_summary_files=reuse_summary_files,
        reuse_stage1=reuse_stage1,
        queue_file=str(tmp_path / f"{job_id}.queue.json"),
        metadata={"requested_stages": list(requested_stages)},
    )
    result = AgentRuntimeRunner(spec).run()
    workspace, registry = AgentRuntimeRunner._open_workspace(result.workspace_path)
    return result, workspace, registry


def _registry_json(registry: ArtifactRegistry) -> dict[str, Any]:
    return json.loads(Path(registry.registry_path).read_text(encoding="utf-8"))


def _write_registry(registry: ArtifactRegistry, payload: Mapping[str, Any]) -> None:
    Path(registry.registry_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _artifact_entry(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    for entry in payload.get("artifacts", []):
        if isinstance(entry, dict) and entry.get("artifact_id") == artifact_id:
            return entry
    raise AssertionError(f"artifact entry not found: {artifact_id}")


def _paper_record_id_for_title(registry: ArtifactRegistry, title: str) -> str:
    for record in registry.list_records():
        if record.artifact_type != "paper_artifact" or record.status != "ready":
            continue
        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        paper_info = payload.get("paper_info")
        if isinstance(paper_info, dict) and str(paper_info.get("title") or "") == title:
            return record.artifact_id
    raise AssertionError(f"paper artifact not found for title: {title}")


def _refresh_dependency_hashes(payload: dict[str, Any], artifact_id: str, content_hash: str) -> None:
    for entry in payload.get("artifacts", []):
        if not isinstance(entry, dict):
            continue
        for dependency in entry.get("depends_on", []):
            if isinstance(dependency, dict) and dependency.get("artifact_id") == artifact_id:
                dependency["content_hash"] = content_hash


def _rewrite_json_artifact(
    registry: ArtifactRegistry,
    artifact_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _registry_json(registry)
    entry = _artifact_entry(payload, artifact_id)
    path = Path(str(entry["path"]))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(envelope, dict)
    mutate(envelope)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    content_hash = file_sha256(path)
    entry["content_hash"] = content_hash
    _refresh_dependency_hashes(payload, artifact_id, content_hash)
    _write_registry(registry, payload)


def _rewrite_receipt_ledger(
    registry: ArtifactRegistry,
    mutate_rows: Callable[[list[dict[str, Any]]], None],
) -> None:
    payload = _registry_json(registry)
    entry = _artifact_entry(payload, "stage1_provider_receipts")
    path = Path(str(entry["path"]))
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(isinstance(row, dict) for row in rows)
    mutate_rows(rows)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    content_hash = file_sha256(path)
    entry["content_hash"] = content_hash
    _refresh_dependency_hashes(payload, "stage1_provider_receipts", content_hash)
    _write_registry(registry, payload)


def test_stage1_generated_closure_adversarial_matrix_fails_completion_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers = _stage1_papers(tmp_path / "seed-papers", prefix="matrix")
    result, workspace, registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-matrix-job",
        papers=papers,
    )
    assert result.job_status == "completed", result
    baseline_map = resolve_current_stage_closure_map(registry)
    assert baseline_map.blocking_issues == (), baseline_map.to_dict()
    assert baseline_map.provider_closures_by_stage["analyze"]["complete"] is True
    assert result.completion_status == "complete", result

    workspace_root = Path(workspace.root_dir)
    original_files = {
        path: path.read_bytes()
        for path in workspace_root.rglob("*")
        if path.is_file()
    }
    original_registry = Path(registry.registry_path).read_bytes()
    closure_id = "stage1:provider_receipt_closure"
    graph_id = "stage1:provider_expected_call_graph"
    matrix_a_record_id = _paper_record_id_for_title(registry, "matrix-a")
    matrix_b_record_id = _paper_record_id_for_title(registry, "matrix-b")

    def remove_one_receipt(_rows: list[dict[str, Any]]) -> None:
        _rows.pop(0)

    def unexpected_receipt(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "call_id": "stage1:unexpected"}

    def historical_receipt(rows: list[dict[str, Any]]) -> None:
        rows.append({**rows[0], "receipt_id": f"{rows[0]['receipt_id']}-historical", "closure_epoch_id": "historical-epoch"})

    def wrong_job(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "job_id": "another-job"}

    def wrong_attempt(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "attempt_id": "wrong-attempt"}

    def wrong_logical_attempt(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "logical_attempt_identity": "wrong-logical-attempt"}

    def wrong_input(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "input_hash": "f" * 64}

    def wrong_config(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "config_hash": "e" * 64}

    def wrong_schema(rows: list[dict[str, Any]]) -> None:
        rows[0] = {**rows[0], "schema_hash": "d" * 64}

    cases: list[tuple[str, Callable[[ArtifactRegistry], None]]] = [
        ("missing_receipt", lambda current: _rewrite_receipt_ledger(current, remove_one_receipt)),
        ("unexpected_same_epoch_receipt", lambda current: _rewrite_receipt_ledger(current, unexpected_receipt)),
        ("historical_receipt", lambda current: _rewrite_receipt_ledger(current, historical_receipt)),
        ("wrong_job_id", lambda current: _rewrite_receipt_ledger(current, wrong_job)),
        ("wrong_attempt_id", lambda current: _rewrite_receipt_ledger(current, wrong_attempt)),
        ("wrong_logical_attempt", lambda current: _rewrite_receipt_ledger(current, wrong_logical_attempt)),
        ("wrong_input_hash", lambda current: _rewrite_receipt_ledger(current, wrong_input)),
        ("wrong_provider_config_hash", lambda current: _rewrite_receipt_ledger(current, wrong_config)),
        ("wrong_schema_hash", lambda current: _rewrite_receipt_ledger(current, wrong_schema)),
        (
            "expected_graph_bound_to_other_source_bundle",
            lambda current: _rewrite_json_artifact(
                current,
                graph_id,
                lambda envelope: envelope.__setitem__("source_bundle_hash", "c" * 64),
            ),
        ),
        (
            "expected_graph_dependency_missing",
            lambda current: _remove_dependency(current, closure_id, graph_id),
        ),
        (
            "receipt_ledger_dependency_missing",
            lambda current: _remove_dependency(current, closure_id, "stage1_provider_receipts"),
        ),
        (
            "paper_artifact_missing",
            lambda current: _remove_record(current, matrix_a_record_id),
        ),
        (
            "paper_artifact_modified",
            lambda current: _modify_record_file(current, matrix_b_record_id),
        ),
        (
            "evidence_manifest_missing",
            lambda current: _remove_first_record_of_type(current, "evidence_manifest"),
        ),
    ]

    for label, mutate in cases:
        _restore_workspace(workspace_root, original_files, original_registry, registry)
        current = ArtifactRegistry(registry.registry_path, registry.job_id)
        mutate(current)
        mutated_registry = ArtifactRegistry(registry.registry_path, registry.job_id)
        stage_map = resolve_current_stage_closure_map(mutated_registry)
        assert stage_map.blocking_issues, label
        status = AgentRuntimeRunner.status(workspace.root_dir)
        assert status.completion_status != "complete", (label, status)
        from runtime.control_plane import ReviewControlPlane

        exported = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1]).export(
            workspace=workspace.root_dir
        )
        assert exported["status"] not in {"canonical_verified", "canonical_unvalidated"}, (label, exported)


def _remove_dependency(registry: ArtifactRegistry, record_id: str, dependency_id: str) -> None:
    payload = _registry_json(registry)
    entry = _artifact_entry(payload, record_id)
    entry["depends_on"] = [
        item for item in entry.get("depends_on", [])
        if not isinstance(item, dict) or item.get("artifact_id") != dependency_id
    ]
    path = Path(str(entry["path"]))
    entry["content_hash"] = file_sha256(path)
    _write_registry(registry, payload)


def _remove_record(registry: ArtifactRegistry, record_id: str) -> None:
    payload = _registry_json(registry)
    payload["artifacts"] = [
        item for item in payload.get("artifacts", [])
        if not isinstance(item, dict) or item.get("artifact_id") != record_id
    ]
    _write_registry(registry, payload)


def _remove_first_record_of_type(registry: ArtifactRegistry, artifact_type: str) -> None:
    payload = _registry_json(registry)
    removed = False
    kept = []
    for item in payload.get("artifacts", []):
        if not removed and isinstance(item, dict) and item.get("artifact_type") == artifact_type:
            removed = True
            continue
        kept.append(item)
    assert removed
    payload["artifacts"] = kept
    _write_registry(registry, payload)


def _modify_record_file(registry: ArtifactRegistry, record_id: str) -> None:
    record = registry.get(record_id)
    assert record is not None
    path = Path(record.path)
    path.write_text(path.read_text(encoding="utf-8") + "\nmodified-after-registration\n", encoding="utf-8")


def _restore_workspace(
    workspace_root: Path,
    original_files: Mapping[Path, bytes],
    original_registry: bytes,
    registry: ArtifactRegistry,
) -> None:
    for path in workspace_root.rglob("*"):
        if path.is_file() and path not in original_files:
            path.unlink()
    for path, content in original_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    Path(registry.registry_path).write_bytes(original_registry)


def test_stage1_all_reuse_mixed_reuse_and_summary_source_zero_call_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_papers = _stage1_papers(tmp_path / "source", prefix="reuse")
    seed_result, _seed_workspace, _seed_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-reuse-seed",
        papers=source_papers,
    )
    seed_summary = str(Path(seed_result.workspace_path) / "artifacts" / "stage1" / "inputs" / next(
        path.name for path in Path(seed_result.workspace_path, "artifacts", "stage1", "inputs").glob("stage1_summaries_*.json")
    ))

    all_result, all_workspace, all_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-all-reuse",
        papers=source_papers,
        reuse_summary_files=(seed_summary,),
        reuse_stage1=True,
    )
    assert all_result.job_status == "completed", all_result
    assert all_result.completion_status == "complete", all_result
    all_map = resolve_current_stage_closure_map(all_registry)
    assert all_map.blocking_issues == (), all_map.to_dict()
    all_entry = all_map.provider_closures_by_stage["analyze"]
    assert all_entry["complete"] is True
    assert all_entry["expected_call_ids"] == []
    assert all_entry["observed_call_ids"] == []
    assert not any(record.artifact_type == "provider_receipt_ledger" for record in all_registry.list_records())
    reuse_records = [record for record in all_registry.list_records() if record.artifact_type == "stage1_summary_reuse_record"]
    assert len(reuse_records) == 3
    for record in reuse_records:
        assert record.depends_on
        assert all(dependency.content_hash for dependency in record.depends_on)
        reuse_payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
        source_id = str(reuse_payload["registered_source_artifact_id"])
        source_record = all_registry.get(source_id)
        assert source_record is not None
        assert reuse_payload["registered_source_artifact_hash"] == source_record.content_hash
        assert reuse_payload["registry_file_hash"] == file_sha256(source_record.path)
        assert reuse_payload["summary_payload_hash"]
        assert reuse_payload["source_summary_manifest_id"]
        assert reuse_payload["source_summary_manifest_hash"]
        assert reuse_payload["current_runtime_spec_id"] == "runtime_job_spec"
        assert reuse_payload["current_evidence_manifest_id"]

    # Exercise the common closure reader against the production-shaped
    # all-reuse workspace, where no receipt ledger exists by design.
    original_files = {
        path: path.read_bytes()
        for path in Path(all_workspace.root_dir).rglob("*")
        if path.is_file()
    }
    original_registry = Path(all_registry.registry_path).read_bytes()
    terminal_id = str(all_entry["terminal_artifact_id"])

    def mark_unexpected_zero_call_receipt(envelope: dict[str, Any]) -> None:
        payload = envelope["payload"]
        payload["observed_call_ids"] = ["stage1:unexpected-zero-call"]
        payload["unexpected_receipts"] = ["stage1:unexpected-zero-call"]
        payload["complete"] = False
        payload["closure_hash"] = "f" * 64

    def mark_zero_call_terminal_work(envelope: dict[str, Any]) -> None:
        envelope["model_call_count"] = 1

    for label, mutate in (
        ("unexpected_zero_call_receipt", lambda current: _rewrite_json_artifact(
            current,
            "stage1:provider_receipt_closure",
            mark_unexpected_zero_call_receipt,
        )),
        ("zero_call_terminal_work", lambda current: _rewrite_json_artifact(
            current,
            terminal_id,
            mark_zero_call_terminal_work,
        )),
    ):
        _restore_workspace(
            Path(all_workspace.root_dir),
            original_files,
            original_registry,
            all_registry,
        )
        current = ArtifactRegistry(all_registry.registry_path, all_registry.job_id)
        mutate(current)
        mutated_registry = ArtifactRegistry(all_registry.registry_path, all_registry.job_id)
        mutated_map = resolve_current_stage_closure_map(mutated_registry)
        assert mutated_map.blocking_issues, (label, mutated_map.to_dict())
        status = AgentRuntimeRunner.status(all_workspace.root_dir)
        assert status.completion_status != "complete", (label, status)
        from runtime.control_plane import ReviewControlPlane

        exported = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1]).export(
            workspace=all_workspace.root_dir
        )
        assert exported["status"] not in {"canonical_verified", "canonical_unvalidated"}, (
            label,
            exported,
        )

    seed_payload = json.loads(Path(seed_summary).read_text(encoding="utf-8"))
    seed_summaries = (
        seed_payload.get("summaries")
        if isinstance(seed_payload, dict)
        else seed_payload
    )
    assert isinstance(seed_summaries, list)
    first_seed_key = str(
        (seed_summaries[0].get("paper_info") or {}).get("canonical_paper_key")
    )
    assert first_seed_key
    mixed_source = tmp_path / "mixed-summary-source.json"
    mixed_source.write_text(
        json.dumps(
            [
                summary
                for summary in seed_summaries
                if isinstance(summary, dict)
                and str((summary.get("paper_info") or {}).get("canonical_paper_key") or "")
                == first_seed_key
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    mixed_result, _mixed_workspace, mixed_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-mixed-reuse",
        papers=source_papers,
        reuse_summary_files=(str(mixed_source),),
        reuse_stage1=True,
    )
    assert mixed_result.job_status == "completed", mixed_result
    mixed_map = resolve_current_stage_closure_map(mixed_registry)
    assert mixed_map.blocking_issues == (), mixed_map.to_dict()
    mixed_entry = mixed_map.provider_closures_by_stage["analyze"]
    assert mixed_entry["complete"] is True
    assert len(mixed_entry["expected_call_ids"]) == 2
    assert len(mixed_entry["observed_call_ids"]) == 2
    mixed_reuse_records = [
        record
        for record in mixed_registry.list_records()
        if record.artifact_type == "stage1_summary_reuse_record"
    ]
    assert len(mixed_reuse_records) == 1

    zero_result, _zero_workspace, zero_registry = _run_stage1(
        tmp_path,
        monkeypatch,
        job_id="stage1-summary-source-zero",
        papers=[],
        reuse_summary_files=(seed_summary,),
        reuse_stage1=True,
        requested_stages=("analyze",),
        action="analyze",
    )
    assert zero_result.job_status == "completed", zero_result
    zero_map = resolve_current_stage_closure_map(zero_registry)
    assert zero_map.blocking_issues == (), zero_map.to_dict()
    zero_entry = zero_map.provider_closures_by_stage["analyze"]
    assert zero_entry["complete"] is True
    assert zero_entry["expected_call_ids"] == []
    assert zero_entry["observed_call_ids"] == []
    assert zero_entry["model_call_count"] == 0
    assert zero_result.completion_status == "complete", zero_result

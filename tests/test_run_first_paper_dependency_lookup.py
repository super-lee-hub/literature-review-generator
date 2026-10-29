"""Paper artifact dependency lookup preserves Registry order and hash checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz
import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from services.artifact_registry import ArtifactRecord, UnverifiedDependency, file_sha256
from tests.test_runtime_bridge_helpers import build_success_summary, current_config


def _write_pdf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _host_and_result(tmp_path: Path) -> tuple[Any, dict[str, Any], Path, Path]:
    pdf_root = tmp_path / "papers"
    primary_pdf = pdf_root / "alpha.pdf"
    unrelated_pdf = pdf_root / "unrelated.pdf"
    _write_pdf(primary_pdf, "Primary source paper for dependency lookup.")
    _write_pdf(unrelated_pdf, "Unrelated source paper for filtering.")
    queue_file = tmp_path / "output" / "_queue" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="paper-dependency-lookup",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_root)),
            action="analyze",
            config=str(current_config(tmp_path)),
            queue_file=str(queue_file),
        )
    )
    session = bridge.bootstrap()
    bridge.persist_source_bundle(session, bridge.build_source_bundle())
    registry = session.context.registry
    registry.register_file(
        artifact_role="source_pdf_alias",
        artifact_type="source_pdf",
        artifact_version="v1",
        path=primary_pdf,
        producer="tests.paper_dependency_lookup",
        artifact_id="source_pdf:alpha-duplicate",
    )
    return (
        session.stage_host,
        build_success_summary(primary_pdf, paper_key="paper-alpha"),
        primary_pdf,
        unrelated_pdf,
    )


def _dependency_paths(result: dict[str, Any]) -> set[str]:
    paper = result["paper_info"]
    paths = {str(paper.get("source_pdf") or "").strip()}
    preprocess = result.get("preprocess") or {}
    paths.update(
        str(value).strip()
        for key, value in preprocess.items()
        if str(key).endswith("_path") and str(value).strip()
    )
    stage1_input = result.get("stage1_input") or {}
    paths.update(
        str(stage1_input.get(key) or "").strip()
        for key in ("visual_manifest_path", "visual_bundle_path")
    )
    paths.update(
        str(item.get("image_path") or "").strip()
        for item in stage1_input.get("selected_visual_refs") or ()
        if isinstance(item, dict)
    )
    return {
        str(Path(value).expanduser().resolve())
        for value in paths
        if value
    }


def _legacy_matches(
    records: list[ArtifactRecord], result: dict[str, Any]
) -> list[tuple[str, str, str]]:
    paths = _dependency_paths(result)
    return [
        (record.artifact_id, record.path, record.content_hash)
        for record in records
        if record.status == "ready"
        and record.path
        and str(Path(record.path).resolve()) in paths
    ]


def _dependency_index(
    records: list[ArtifactRecord],
) -> dict[str, tuple[tuple[int, ArtifactRecord], ...]]:
    by_path: dict[str, list[tuple[int, ArtifactRecord]]] = {}
    for ordinal, record in enumerate(records):
        if record.status != "ready" or not record.path:
            continue
        resolved_path = str(Path(record.path).resolve())
        by_path.setdefault(resolved_path, []).append((ordinal, record))
    return {path: tuple(rows) for path, rows in by_path.items()}


def _registered_dependencies(host: Any, result: dict[str, Any]) -> list[tuple[str, str, str]]:
    _workspace, registry = host._require_workspace()
    record = registry.get(host._paper_artifact_id(result["paper_info"]))
    assert record is not None and record.status == "ready"
    assert Path(record.path).is_file()
    assert file_sha256(record.path) == record.content_hash
    return [
        (dependency.artifact_id, dependency.path, dependency.content_hash)
        for dependency in record.depends_on
    ]


def test_supplied_path_index_preserves_legacy_order_and_does_not_rescan_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, result, primary_pdf, unrelated_pdf = _host_and_result(tmp_path)
    _workspace, registry = host._require_workspace()
    records = registry.list_records()
    expected = _legacy_matches(records, result)
    index = _dependency_index(records)

    same_path_rows = index[str(primary_pdf.resolve())]
    assert [record.artifact_id for _ordinal, record in same_path_rows] == [
        record.artifact_id
        for record in records
        if record.status == "ready" and str(Path(record.path).resolve()) == str(primary_pdf.resolve())
    ]
    assert str(unrelated_pdf.resolve()) in index
    assert all(path != str(unrelated_pdf.resolve()) for _id, path, _hash in expected)
    assert [artifact_id for artifact_id, _path, _hash in expected] == [
        "source_pdf:alpha.pdf",
        "source_pdf:alpha-duplicate",
    ]

    def unexpected_registry_scan() -> list[ArtifactRecord]:
        raise AssertionError("the supplied operation-local dependency index was ignored")

    monkeypatch.setattr(registry, "list_records", unexpected_registry_scan)
    assert host._persist_paper_artifact(
        result,
        dependency_records_by_path=index,
    ) is True
    assert _registered_dependencies(host, result) == expected


def test_none_index_keeps_legacy_registry_scan_and_dependency_set(tmp_path: Path) -> None:
    host, result, _primary_pdf, _unrelated_pdf = _host_and_result(tmp_path)
    _workspace, registry = host._require_workspace()
    expected = _legacy_matches(registry.list_records(), result)

    assert host._persist_paper_artifact(result) is True

    assert _registered_dependencies(host, result) == expected


def test_source_mutation_after_index_snapshot_fails_before_ready_publication(
    tmp_path: Path,
) -> None:
    host, result, primary_pdf, _unrelated_pdf = _host_and_result(tmp_path)
    _workspace, registry = host._require_workspace()
    index = _dependency_index(registry.list_records())
    paper_artifact_id = host._paper_artifact_id(result["paper_info"])
    primary_pdf.write_bytes(primary_pdf.read_bytes() + b"\n% changed after dependency snapshot\n")

    with pytest.raises(UnverifiedDependency, match="dependency content hash changed"):
        host._persist_paper_artifact(
            result,
            dependency_records_by_path=index,
        )

    record = registry.get(paper_artifact_id)
    assert record is None or record.status != "ready"

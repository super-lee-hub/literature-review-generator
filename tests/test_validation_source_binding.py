"""Downstream validation source authority (Lane B) regression tests.

The v19 failure shape was: downstream review job -> compact outline evidence
pack -> synthetic paper artifact with ``stage1_inputs={}`` -> 16/17
``evidence_gap``.  These tests lock the fix: validation recovers the
authoritative upstream Stage 1 artifacts through ``validation_source_binding``
and fails closed on any identity mismatch instead of degrading to an
``ai_summary``-only artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from validation.source_binding import (
    build_validation_source_binding,
    resolve_bound_paper_artifacts,
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha256_bytes(data)


def _envelope(payload: Any) -> dict[str, Any]:
    return {"payload": payload}


@pytest.fixture()
def upstream_workspace(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A minimal upstream Stage 1 workspace for one paper."""

    ws = tmp_path / "upstream"
    paper_key = "10.1234/upstream.paper.2026"
    normalized_text = "# Title\n\nCreator monetization shifts organic engagement.\n" * 40
    chunks = {
        "chunks": [
            {"chunk_id": "c1", "page": 1, "text": normalized_text[:900]},
            {"chunk_id": "c2", "page": 2, "text": normalized_text[900:1800]},
        ]
    }
    page_index = {"pages": [{"page": 1, "chunk_ids": ["c1"]}, {"page": 2, "chunk_ids": ["c2"]}]}

    cache = ws / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    normalized_path = cache / "normalized.md"
    normalized_path.write_text(normalized_text, encoding="utf-8")
    normalized_hash = _sha256_bytes(normalized_path.read_bytes())
    chunks_path = cache / "chunks.json"
    chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    chunks_hash = _sha256_bytes(chunks_path.read_bytes())
    page_index_path = cache / "page_index.json"
    page_index_path.write_text(json.dumps(page_index, ensure_ascii=False), encoding="utf-8")
    page_index_hash = _sha256_bytes(page_index_path.read_bytes())

    manifest_path = ws / "manifests" / "manifest.json"
    manifest_payload = _envelope(
        {
            "artifact_type": "evidence_manifest",
            "artifact_version": "v1",
            "job_id": "up_job",
            "canonical_paper_key": paper_key,
            "artifacts": [
                {"artifact_type": "normalized_text", "path": str(normalized_path), "content_hash": normalized_hash},
                {"artifact_type": "chunks", "path": str(chunks_path), "content_hash": chunks_hash},
                {"artifact_type": "page_index", "path": str(page_index_path), "content_hash": page_index_hash},
            ],
        }
    )
    manifest_hash = _write(manifest_path, manifest_payload)

    paper_path = ws / "papers" / "paper.json"
    paper_payload = _envelope(
        {
            "paper_identity": {"canonical_paper_key": paper_key, "source_paper_id": "pdf-1"},
            "paper_info": {"canonical_paper_key": paper_key, "title": "Upstream Paper"},
            "analysis": {"ai_summary": {"main_findings": ["organic engagement declines"]}},
            "stage1_inputs": {
                "input_mode": "text_first",
                "evidence_manifest_path": str(manifest_path),
                "evidence_manifest_hash": manifest_hash,
            },
        }
    )
    paper_hash = _write(paper_path, paper_payload)

    record = {
        "artifact_id": "paper:deadbeef01",
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "status": "ready",
        "path": str(paper_path),
        "content_hash": paper_hash,
        "job_id": "up_job",
        "producer": "test",
    }
    _write(
        ws / "artifact_registry.json",
        {"artifact_registry_version": "1", "job_id": "up_job", "artifacts": [record]},
    )
    hashes = {
        "paper_key": paper_key,
        "paper_hash": paper_hash,
        "manifest_hash": manifest_hash,
        "normalized_hash": normalized_hash,
    }
    return ws, hashes


def _summary_file(base: Path, paper_key: str) -> Path:
    # Real-world shape: summaries live inside the upstream Stage 1 workspace.
    target = base / "artifacts" / "stage1" / "inputs"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "stage1_summaries.json"
    path.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {"canonical_paper_key": paper_key, "title": "Upstream Paper"},
                    "ai_summary": {"main_findings": ["organic engagement declines"]},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _registry_stub(workspace: Path) -> Any:
    class _StubRegistry:
        def __init__(self, ws: Path) -> None:
            self._payload = json.loads((ws / "artifact_registry.json").read_text(encoding="utf-8"))
            self._records = [
                r for r in self._payload.get("artifacts", []) if isinstance(r, dict)
            ]

        def list_records(self) -> list[Any]:
            return [_StubRecord(r) for r in self._records]

    class _StubRecord:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.artifact_type = str(payload.get("artifact_type") or "")
            self.artifact_id = str(payload.get("artifact_id") or "")
            self.status = str(payload.get("status") or "")
            self.path = str(payload.get("path") or "")
            self.content_hash = str(payload.get("content_hash") or "")
            self.job_id = str(payload.get("job_id") or "")

    return _StubRegistry(workspace)


def test_downstream_validation_recovers_external_stage1_evidence_manifest(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,  # downstream job: no local paper artifacts
        job_id="down_job",
    )
    assert binding["bound_paper_count"] == 1
    entry = binding["papers"][hashes["paper_key"]]
    assert entry["source_workspace_job_id"] == "up_job"
    assert entry["stage1_paper_artifact_hash"] == hashes["paper_hash"]
    assert entry["evidence_manifest_hash"] == hashes["manifest_hash"]

    artifacts, problems = resolve_bound_paper_artifacts(
        binding, external_registry_resolver=None
    )
    assert problems == ()
    assert len(artifacts) == 1
    stage1_inputs = artifacts[0]["stage1_inputs"]
    assert Path(str(stage1_inputs["evidence_manifest_path"])).is_file()
    assert stage1_inputs["evidence_manifest_hash"] == hashes["manifest_hash"]
    preprocess = stage1_inputs["preprocess_evidence"]
    assert Path(str(preprocess["markdown_path"])).is_file()
    assert Path(str(preprocess["chunks_path"])).is_file()
    assert Path(str(preprocess["page_index_path"])).is_file()
    assert artifacts[0]["_validation_source_binding"]["canonical_paper_key"] == hashes["paper_key"]


def test_validation_uses_normalized_text_windows_not_ai_summary_only(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """Resolved artifacts must carry original-text paths, never stage1_inputs={}."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,  # downstream job: no local paper artifacts
        job_id="down_job",
    )
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert problems == ()
    for artifact in artifacts:
        assert artifact["stage1_inputs"]  # never the old empty-dict fallback


def test_missing_external_stage1_registry_fails_closed(tmp_path: Path) -> None:
    binding = build_validation_source_binding(
        summary_sources=[str(tmp_path / "nowhere" / "summaries.json")],
        local_registry=None,
        job_id="down_job",
    )
    assert binding["bound_paper_count"] == 0
    assert any("upstream_workspace_unresolved" in item for item in binding["diagnostics"])


def test_tampered_evidence_manifest_hash_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,  # downstream job: no local paper artifacts
        job_id="down_job",
    )
    # Tamper with the manifest file after binding.
    manifest_path = Path(binding["papers"][hashes["paper_key"]]["evidence_manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["canonical_paper_key"] = "10.9999/tampered"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert artifacts == []
    assert any("evidence_manifest_hash_mismatch" in item for item in problems)


def test_wrong_paper_evidence_manifest_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,  # downstream job: no local paper artifacts
        job_id="down_job",
    )
    # Swap the paper artifact for a different identity.
    paper_path = Path(binding["papers"][hashes["paper_key"]]["stage1_paper_artifact_path"])
    payload = json.loads(paper_path.read_text(encoding="utf-8"))
    payload["payload"]["paper_identity"]["canonical_paper_key"] = "10.9999/wrong.paper"
    paper_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert artifacts == []
    # Byte drift and identity drift both fail closed; the artifact is never
    # adopted when the authority no longer matches the binding.
    assert problems and all(
        item.startswith("VALIDATION_SOURCE_AUTHORITY_INVALID") for item in problems
    )


def test_bound_paper_key_filter_applies(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """present_paper_keys restricts which bound papers are resolved."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,  # downstream job: no local paper artifacts
        job_id="down_job",
    )
    arts_kept, problems_kept = resolve_bound_paper_artifacts(
        binding, present_paper_keys=[hashes["paper_key"]]
    )
    assert problems_kept == ()
    assert len(arts_kept) == 1
    arts_skipped, _ = resolve_bound_paper_artifacts(
        binding, present_paper_keys=["10.9999/not.in.review"]
    )
    assert arts_skipped == []

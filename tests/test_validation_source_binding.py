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
import shutil
from types import SimpleNamespace
from typing import Any

import pytest

from validation.source_binding import (
    build_validation_source_authority_fingerprint,
    build_validation_source_binding,
    resolve_bound_paper_artifacts,
    validation_source_binding_semantic_hash,
    validation_source_binding_payload_hash,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import LocalPublicationContext
from runtime.orchestrator import AgentRuntimeBridge
from runtime.provider_runtime import hash_json
from services.job_workspace import publish_json_artifact
from validation.current_validation import _load_inputs


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
            "created_at": "2026-09-04T00:00:00Z",
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
    manifest_record = {
        "artifact_id": "evidence_manifest:upstream-paper",
        "artifact_type": "evidence_manifest",
        "artifact_version": "v1",
        "status": "ready",
        "path": str(manifest_path),
        "content_hash": manifest_hash,
        "job_id": "up_job",
        "producer": "test",
    }
    _write(
        ws / "artifact_registry.json",
        {
            "artifact_registry_version": "v2",
            "revision": 1,
            "job_id": "up_job",
            "artifacts": [record, manifest_record],
        },
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


def _build(ws: Path, summary_path: Path) -> dict[str, Any]:
    return build_validation_source_binding(
        summary_sources=[str(summary_path)],
        local_registry=None,
        job_id="down_job",
    )


def test_registry_deletion_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """A binding built while the registry existed must NOT silently skip
    registry validation when the registry disappears afterwards."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    (ws / "artifact_registry.json").unlink()
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert artifacts == []
    assert any("registry_missing" in item for item in problems)


def test_registry_record_identity_mismatch_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """Same artifact_id + hash but wrong record identity must fail closed."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    reg_payload = json.loads((ws / "artifact_registry.json").read_text(encoding="utf-8"))
    reg_payload["artifacts"][0]["artifact_type"] = "evidence_manifest"
    (ws / "artifact_registry.json").write_text(json.dumps(reg_payload), encoding="utf-8")
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert artifacts == []
    assert any("registry_artifact_type_mismatch" in item for item in problems)


def test_wrong_paper_manifest_semantic_identity_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """A byte-identical manifest that semantically belongs to another paper
    must fail closed via manifest semantic identity, not just file hash."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    manifest_path = Path(binding["papers"][hashes["paper_key"]]["evidence_manifest_path"])
    # Rewrite the manifest as a *valid* manifest for a different paper, then
    # sync the binding's manifest hash so the file-hash check would pass.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["canonical_paper_key"] = "10.9999/other.paper"
    new_hash = _write(manifest_path, manifest)
    rewritten = {**binding}
    papers = {k: dict(v) for k, v in binding["papers"].items()}
    papers[hashes["paper_key"]]["evidence_manifest_hash"] = new_hash
    rewritten["papers"] = papers
    registry_payload = json.loads((ws / "artifact_registry.json").read_text(encoding="utf-8"))
    registry_payload["artifacts"][1]["content_hash"] = new_hash
    (ws / "artifact_registry.json").write_text(
        json.dumps(registry_payload, ensure_ascii=False), encoding="utf-8"
    )
    artifacts, problems = resolve_bound_paper_artifacts(rewritten)
    assert artifacts == []
    assert any("manifest_paper_identity_mismatch" in item for item in problems)


def test_tampered_leaf_normalized_text_fails_closed(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    """normalized.md byte drift after binding must fail closed: validation
    adjudicates claims against these bytes and cannot read a stale file."""

    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    leaf_path = Path(binding["papers"][hashes["paper_key"]]["evidence"]["markdown_path"]["path"])
    leaf_path.write_text(leaf_path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert artifacts == []
    assert any("normalized_text_hash_mismatch" in item for item in problems)


def test_source_authority_fingerprint_binds_canonical_leaf_hashes(
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert problems == ()

    fingerprint, authority_hash, diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
    )
    assert diagnostics == ()
    assert len(authority_hash) == 64
    entry = fingerprint["papers"][0]
    assert entry["binding_contract_version"] == "v1"
    assert entry["normalized_text_hash"] == hashes["normalized_hash"]
    assert entry["chunks_hash"]
    assert entry["page_index_hash"]


def test_source_authority_fingerprint_changes_after_evidence_drift(
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert problems == ()
    _before, before_hash, before_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
    )
    assert before_diagnostics == ()

    normalized_path = Path(
        binding["papers"][hashes["paper_key"]]["evidence"]["markdown_path"]["path"]
    )
    normalized_path.write_text(
        normalized_path.read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    _after, after_hash, after_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
    )
    assert after_hash != before_hash
    assert after_diagnostics


def test_binding_semantic_hash_is_independent_of_workspace_locators(
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    relocated = json.loads(json.dumps(binding, ensure_ascii=False))
    relocated["job_id"] = "relocated-downstream-job"
    relocated["upstream_workspaces"] = [r"D:\\durable\\f1-stage1"]
    relocated["diagnostics"] = ["historical_locator_warning"]
    entry = relocated["papers"][hashes["paper_key"]]
    entry["source_workspace"] = r"D:\\durable\\f1-stage1"
    entry["stage1_paper_artifact_path"] = r"D:\\durable\\f1-stage1\\paper.json"
    entry["evidence_manifest_path"] = r"D:\\durable\\f1-stage1\\manifest.json"
    for leaf in entry["evidence"].values():
        leaf["path"] = r"D:\\durable\\f1-stage1\\evidence\\relocated.bin"

    assert validation_source_binding_payload_hash(relocated) == (
        validation_source_binding_payload_hash(binding)
    )


def test_source_authority_hash_ignores_physical_binding_file_hash(
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    summary_path = _summary_file(ws, hashes["paper_key"])
    binding = _build(ws, summary_path)
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert problems == ()

    _before, before_hash, before_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
        current_binding_artifact_id="validation_source_binding:semantic-id",
        current_binding_content_hash="a" * 64,
        current_binding_semantic_hash="s" * 64,
    )
    _after, after_hash, after_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
        current_binding_artifact_id="validation_source_binding:semantic-id",
        current_binding_content_hash="b" * 64,
        current_binding_semantic_hash="s" * 64,
    )

    assert before_diagnostics == ()
    assert after_diagnostics == ()
    assert after_hash == before_hash


def test_semantic_binding_hash_changes_for_authority_mutations(
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    binding = _build(ws, _summary_file(ws, hashes["paper_key"]))
    baseline = validation_source_binding_semantic_hash(binding)

    paper_hash_changed = json.loads(json.dumps(binding, ensure_ascii=False))
    paper_hash_changed["papers"][hashes["paper_key"]][
        "stage1_paper_artifact_hash"
    ] = "a" * 64
    manifest_hash_changed = json.loads(json.dumps(binding, ensure_ascii=False))
    manifest_hash_changed["papers"][hashes["paper_key"]][
        "evidence_manifest_hash"
    ] = "b" * 64
    leaf_hash_changed = json.loads(json.dumps(binding, ensure_ascii=False))
    leaf_hash_changed["papers"][hashes["paper_key"]]["evidence"]["markdown_path"][
        "content_hash"
    ] = "c" * 64
    contract_changed = json.loads(json.dumps(binding, ensure_ascii=False))
    contract_changed["binding_contract_version"] = "v2"

    assert validation_source_binding_semantic_hash(paper_hash_changed) != baseline
    assert validation_source_binding_semantic_hash(manifest_hash_changed) != baseline
    assert validation_source_binding_semantic_hash(leaf_hash_changed) != baseline
    assert validation_source_binding_semantic_hash(contract_changed) != baseline


def test_source_authority_fingerprint_ignores_binding_locator_paths(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    ws, hashes = upstream_workspace
    binding = _build(ws, _summary_file(ws, hashes["paper_key"]))
    artifacts, problems = resolve_bound_paper_artifacts(binding)
    assert problems == ()
    _before, before_hash, before_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
    )

    relocated_artifacts = json.loads(json.dumps(artifacts, ensure_ascii=False))
    relocated_binding = relocated_artifacts[0]["_validation_source_binding"]
    relocated_dir = tmp_path / "relocated-authority"
    relocated_dir.mkdir()
    original_paper_path = Path(relocated_binding["stage1_paper_artifact_path"])
    original_manifest_path = Path(relocated_binding["evidence_manifest_path"])
    relocated_paper_path = relocated_dir / "paper.json"
    relocated_manifest_path = relocated_dir / "manifest.json"
    shutil.copy2(original_paper_path, relocated_paper_path)
    shutil.copy2(original_manifest_path, relocated_manifest_path)
    relocated_binding["stage1_paper_artifact_path"] = str(relocated_paper_path)
    relocated_binding["evidence_manifest_path"] = str(relocated_manifest_path)
    for field, leaf in relocated_binding["evidence"].items():
        relocated_leaf_path = relocated_dir / f"{field}.bin"
        shutil.copy2(Path(leaf["path"]), relocated_leaf_path)
        leaf["path"] = str(relocated_leaf_path)
    relocated_artifacts[0]["stage1_inputs"]["evidence_manifest_path"] = str(
        relocated_manifest_path
    )
    relocated_artifacts[0]["stage1_inputs"]["preprocess_evidence"] = {
        field: relocated_binding["evidence"][field]["path"]
        for field in ("markdown_path", "chunks_path", "page_index_path")
    }
    _after, after_hash, after_diagnostics = build_validation_source_authority_fingerprint(
        paper_artifacts=relocated_artifacts,
        registry=None,
        cited_paper_keys=[hashes["paper_key"]],
    )

    assert before_diagnostics == ()
    assert after_diagnostics == ()
    assert after_hash == before_hash


def test_current_validation_loader_resolves_mixed_local_and_external_authority(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, upstream_hashes = upstream_workspace
    upstream_summary = _summary_file(upstream_ws, upstream_hashes["paper_key"])
    external_registry = ArtifactRegistry(
        upstream_ws / "artifact_registry.json",
        "up_job",
    )

    downstream_ws = JobWorkspace.create(str(tmp_path), "downstream", job_id="down_job")
    downstream_registry = ArtifactRegistry(
        downstream_ws.paths.registry_path,
        downstream_ws.job_id,
    )
    local_key = "10.1234/local.paper.2026"
    local_cache = Path(downstream_ws.artifact_path("local-evidence"))
    local_cache.mkdir(parents=True, exist_ok=True)
    local_paths = {
        "markdown_path": local_cache / "normalized.md",
        "chunks_path": local_cache / "chunks.json",
        "page_index_path": local_cache / "page_index.json",
    }
    local_paths["markdown_path"].write_text("Local source evidence.", encoding="utf-8")
    local_paths["chunks_path"].write_text("[]", encoding="utf-8")
    local_paths["page_index_path"].write_text("[]", encoding="utf-8")
    local_manifest_path = local_cache / "manifest.json"
    local_manifest = {
        "artifact_type": "evidence_manifest",
        "artifact_version": "v1",
        "job_id": downstream_ws.job_id,
        "canonical_paper_key": local_key,
        "created_at": "2026-09-04T00:00:00Z",
        "artifacts": [
            {
                "artifact_type": artifact_type,
                "path": str(path),
                "content_hash": _sha256_bytes(path.read_bytes()),
            }
            for artifact_type, path in (
                ("normalized_text", local_paths["markdown_path"]),
                ("chunks", local_paths["chunks_path"]),
                ("page_index", local_paths["page_index_path"]),
            )
        ],
    }
    local_manifest_path.write_text(json.dumps(local_manifest), encoding="utf-8")
    local_manifest_record = downstream_registry.register_file(
        artifact_id="evidence_manifest:local",
        artifact_role="evidence_manifest",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=local_manifest_path,
        producer="tests",
    )
    local_paper_path = Path(downstream_ws.artifact_path("local-paper.json"))
    local_paper_path.write_text(
        json.dumps(
            {
                "paper_identity": {"canonical_paper_key": local_key},
                "paper_info": {"canonical_paper_key": local_key, "title": "Local"},
                "source": {"source_pdf": "local.pdf"},
                "analysis": {"ai_summary": {}},
                "stage1_inputs": {
                    "evidence_manifest_path": str(local_manifest_path),
                    "evidence_manifest_hash": local_manifest_record.content_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    local_paper_record = downstream_registry.register_file(
        artifact_id="paper:local",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=local_paper_path,
        producer="tests",
        depends_on=[ArtifactDependencyRefV2.from_record(local_manifest_record)],
    )

    binding = build_validation_source_binding(
        summary_sources=[str(upstream_summary)],
        local_registry=downstream_registry,
        job_id=downstream_ws.job_id,
    )
    external_entry = binding["papers"][upstream_hashes["paper_key"]]
    binding_path = Path(downstream_ws.artifact_path("validation-source-binding.json"))
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    external_dependencies = [
        ArtifactDependencyRefV2(
            dependency_kind="external_job",
            job_id=external_entry["source_workspace_job_id"],
            artifact_id=external_entry["stage1_paper_artifact_id"],
            artifact_type="paper_artifact",
            path=external_entry["stage1_paper_artifact_path"],
            content_hash=external_entry["stage1_paper_artifact_hash"],
        ),
        ArtifactDependencyRefV2(
            dependency_kind="external_job",
            job_id=external_entry["evidence_manifest_job_id"],
            artifact_id=external_entry["evidence_manifest_artifact_id"],
            artifact_type="evidence_manifest",
            path=external_entry["evidence_manifest_path"],
            content_hash=external_entry["evidence_manifest_hash"],
        ),
    ]
    downstream_registry.register_file(
        artifact_id="validation_source_binding:mixed",
        artifact_role="runtime_stage_evidence",
        artifact_type="validation_source_binding",
        artifact_version="v1",
        path=binding_path,
        producer="tests",
        depends_on=external_dependencies,
        external_registry_resolver=lambda job_id: (
            external_registry if job_id == "up_job" else None
        ),
    )
    review_path = Path(downstream_ws.artifact_path("review.json"))
    citation_path = Path(downstream_ws.artifact_path("citation.json"))
    review_path.write_text("{}", encoding="utf-8")
    citation_path.write_text(
        json.dumps(
            {
                "citation_sets": [
                    {"paper_ids": [local_key]},
                    {"paper_ids": [upstream_hashes["paper_key"]]},
                ],
                "occurrences": [],
            }
        ),
        encoding="utf-8",
    )
    service = SimpleNamespace(
        review_draft_path=str(review_path),
        citation_manifest_path=str(citation_path),
        artifact_registry=downstream_registry,
        paper_artifact_records=(local_paper_record,),
        summaries=[],
        validation_external_registry_resolver=lambda job_id: (
            external_registry if job_id == "up_job" else None
        ),
        get_paper_key=lambda paper: str(
            paper.get("canonical_paper_key") or paper.get("source_paper_id") or ""
        ),
    )

    _review, _citation, papers, _preprocess, _metadata = _load_inputs(service)
    keys = {
        str(item.get("paper_identity", {}).get("canonical_paper_key") or "")
        for item in papers
    }
    assert keys == {local_key, upstream_hashes["paper_key"]}
    assert not any(
        item.startswith("validation_source_authority_ambiguous")
        for item in service._validation_source_authority_diagnostics
    )


def _binding_context(
    tmp_path: Path,
    summary_sources: list[Path],
    upstream_ws: Path,
) -> tuple[Any, Any, ArtifactRegistry, ArtifactRegistry]:
    downstream_ws = JobWorkspace.create(str(tmp_path), "downstream", job_id="down_job")
    downstream_registry = ArtifactRegistry(
        downstream_ws.paths.registry_path,
        downstream_ws.job_id,
    )
    upstream_registry = ArtifactRegistry(upstream_ws / "artifact_registry.json", "up_job")
    bridge = AgentRuntimeBridge.__new__(AgentRuntimeBridge)
    session = SimpleNamespace(
        request=SimpleNamespace(
            summary_file=None,
            summary_sources=tuple(str(path) for path in summary_sources),
            reuse_summary_files=(),
        ),
        context=SimpleNamespace(
            workspace=downstream_ws,
            registry=downstream_registry,
            publication_context=LocalPublicationContext(),
        ),
        stage_host=SimpleNamespace(),
    )
    return bridge, session, downstream_registry, upstream_registry


def _upstream_resolver(registry: ArtifactRegistry):
    return lambda job_id: registry if job_id == registry.job_id else None


def _binding_records(registry: ArtifactRegistry) -> list[Any]:
    return sorted(
        (
            record
            for record in registry.list_records()
            if record.artifact_type == "validation_source_binding"
            and record.status == "ready"
        ),
        key=lambda record: record.artifact_id,
    )


def _rewrite_upstream_paper(
    upstream_ws: Path,
    *,
    mutate: Any,
) -> None:
    registry_payload = json.loads(
        (upstream_ws / "artifact_registry.json").read_text(encoding="utf-8")
    )
    paper_record = next(
        record
        for record in registry_payload["artifacts"]
        if record.get("artifact_type") == "paper_artifact"
    )
    paper_path = Path(paper_record["path"])
    paper_payload = json.loads(paper_path.read_text(encoding="utf-8"))
    mutate(paper_payload)
    paper_hash = _write(paper_path, paper_payload)
    paper_record["content_hash"] = paper_hash
    _write(upstream_ws / "artifact_registry.json", registry_payload)


def _write_loader_inputs(workspace: JobWorkspace, paper_key: str) -> tuple[Path, Path]:
    review_path = Path(workspace.artifact_path("review.json"))
    citation_path = Path(workspace.artifact_path("citation.json"))
    _write(review_path, {})
    _write(
        citation_path,
        {
            "citation_sets": [{"paper_ids": [paper_key]}],
            "occurrences": [],
        },
    )
    return review_path, citation_path


def _loader_service(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    upstream_registry: ArtifactRegistry,
    paper_key: str,
    current_record: Any,
) -> Any:
    review_path, citation_path = _write_loader_inputs(workspace, paper_key)
    return SimpleNamespace(
        review_draft_path=str(review_path),
        citation_manifest_path=str(citation_path),
        artifact_registry=registry,
        paper_artifact_records=(),
        summaries=[],
        validation_external_registry_resolver=_upstream_resolver(upstream_registry),
        current_validation_source_binding_id=current_record.artifact_id,
        current_validation_source_binding_hash=current_record.content_hash,
        get_paper_key=lambda paper: str(
            paper.get("canonical_paper_key") or paper.get("source_paper_id") or ""
        ),
    )


def _publish_manual_binding(
    session: Any,
    registry: ArtifactRegistry,
    binding: dict[str, Any],
    upstream_registry: ArtifactRegistry,
    *,
    artifact_id: str | None = None,
) -> Any:
    payload_hash = hash_json(binding)
    path = session.context.workspace.artifact_path(
        f"validation_source_binding_{payload_hash[:24]}.json"
    )
    dependencies: list[ArtifactDependencyRefV2] = []
    for entry in (binding.get("papers") or {}).values():
        if not isinstance(entry, dict):
            continue
        dependencies.extend(
            [
                ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=str(entry["source_workspace_job_id"]),
                    artifact_id=str(entry["stage1_paper_artifact_id"]),
                    artifact_type="paper_artifact",
                    path=str(entry["stage1_paper_artifact_path"]),
                    content_hash=str(entry["stage1_paper_artifact_hash"]),
                ),
                ArtifactDependencyRefV2(
                    dependency_kind="external_job",
                    job_id=str(entry["evidence_manifest_job_id"]),
                    artifact_id=str(entry["evidence_manifest_artifact_id"]),
                    artifact_type="evidence_manifest",
                    path=str(entry["evidence_manifest_path"]),
                    content_hash=str(entry["evidence_manifest_hash"]),
                ),
            ]
        )
    return publish_json_artifact(
        session.context.publication_context,
        registry,
        path,
        binding,
        artifact_role="runtime_stage_evidence",
        artifact_type="validation_source_binding",
        artifact_version=str(binding["artifact_version"]),
        producer="tests.lifecycle",
        artifact_id=artifact_id or f"validation_source_binding:{payload_hash[:24]}",
        depends_on=dependencies,
        external_registry_resolver=_upstream_resolver(upstream_registry),
    )


def test_old_ready_source_binding_does_not_block_new_current_binding(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    old_record = bridge._ensure_validation_source_binding(
        session,
        registry,
        external_registry_resolver=_upstream_resolver(upstream_registry),
    )
    assert old_record is not None

    _rewrite_upstream_paper(
        upstream_ws,
        mutate=lambda payload: payload["payload"]["analysis"]["ai_summary"]["main_findings"].append(
            "authority drift"
        ),
    )
    upstream_registry.reload()
    new_record = bridge._ensure_validation_source_binding(
        session,
        registry,
        external_registry_resolver=_upstream_resolver(upstream_registry),
    )

    assert new_record is not None
    assert new_record.artifact_id != old_record.artifact_id
    assert len(_binding_records(registry)) == 2


def test_binding_rebuilds_when_summary_source_set_changes(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    alternate_summary = summary_path.with_name("stage1_summaries_alternate.json")
    alternate_summary.write_text(
        json.dumps(
            [{"paper_info": {"canonical_paper_key": hashes["paper_key"]}, "revision": 2}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    first = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    session.request.summary_sources = (str(summary_path), str(alternate_summary))
    second = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert first is not None and second is not None
    assert second.artifact_id != first.artifact_id


def test_binding_rebuilds_when_upstream_paper_artifact_hash_changes(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    first = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    _rewrite_upstream_paper(
        upstream_ws,
        mutate=lambda payload: payload["payload"].setdefault("metadata", {}).update({"revision": 2}),
    )
    upstream_registry.reload()
    second = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert first is not None and second is not None
    assert second.artifact_id != first.artifact_id


def test_binding_rebuilds_when_evidence_manifest_hash_changes(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    first = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    manifest_path = next(
        Path(record["path"])
        for record in json.loads((upstream_ws / "artifact_registry.json").read_text(encoding="utf-8"))["artifacts"]
        if record.get("artifact_type") == "evidence_manifest"
    )
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["payload"]["created_at"] = "2026-09-05T00:00:00Z"
    manifest_hash = _write(manifest_path, manifest_payload)
    _rewrite_upstream_paper(
        upstream_ws,
        mutate=lambda payload: payload["payload"]["stage1_inputs"].update(
            {"evidence_manifest_hash": manifest_hash}
        ),
    )
    registry_payload = json.loads(
        (upstream_ws / "artifact_registry.json").read_text(encoding="utf-8")
    )
    for record in registry_payload["artifacts"]:
        if record.get("artifact_type") == "evidence_manifest":
            record["content_hash"] = manifest_hash
    _write(upstream_ws / "artifact_registry.json", registry_payload)
    upstream_registry.reload()
    second = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert first is not None and second is not None
    assert second.artifact_id != first.artifact_id


def test_binding_rebuilds_when_binding_contract_version_changes(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import validation.source_binding as source_binding

    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    first = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    monkeypatch.setattr(source_binding, "BINDING_CONTRACT_VERSION", "v2")
    second = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert first is not None and second is not None
    assert second.artifact_id != first.artifact_id
    assert second.artifact_version == "v1"
    assert second.metadata["binding_contract_version"] == "v2"


def test_exact_current_binding_is_reused_without_duplicate_publication(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    first = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    revision = registry.revision
    second = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert first is not None and second is not None
    assert second.artifact_id == first.artifact_id
    assert second.content_hash == first.content_hash
    assert registry.revision == revision
    assert len(_binding_records(registry)) == 1


def test_stale_historical_binding_does_not_poison_current_validation(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    _old = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    _rewrite_upstream_paper(
        upstream_ws,
        mutate=lambda payload: payload["payload"].setdefault("metadata", {}).update({"revision": 3}),
    )
    upstream_registry.reload()
    current = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    assert current is not None
    service = _loader_service(
        session.context.workspace,
        registry,
        upstream_registry,
        hashes["paper_key"],
        current,
    )

    _review, _citation, papers, _preprocess, _metadata = _load_inputs(service)

    assert len(papers) == 1
    assert not any(
        item.startswith("VALIDATION_SOURCE_AUTHORITY_INVALID")
        or item.startswith("validation_source_binding_hash_mismatch")
        for item in service._validation_source_authority_diagnostics
    )


def test_multiple_historical_ready_bindings_do_not_create_false_authority_ambiguity(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    current = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    assert current is not None
    first_binding = json.loads(Path(current.path).read_text(encoding="utf-8"))
    first_entry = dict(first_binding["papers"][hashes["paper_key"]])

    upstream_payload = json.loads(
        Path(first_entry["stage1_paper_artifact_path"]).read_text(encoding="utf-8")
    )
    alternate_path = upstream_ws / "papers" / "paper-alternate.json"
    alternate_payload = dict(upstream_payload)
    alternate_payload["payload"] = dict(upstream_payload["payload"])
    alternate_payload["payload"]["metadata"] = {"historical": True}
    alternate_hash = _write(alternate_path, alternate_payload)
    manifest_record = upstream_registry.get(first_entry["evidence_manifest_artifact_id"])
    assert manifest_record is not None
    alternate_record = upstream_registry.register_file(
        artifact_role="paper_summary",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=alternate_path,
        producer="tests.lifecycle",
        artifact_id="paper:alternate",
        depends_on=[ArtifactDependencyRefV2.from_record(manifest_record)],
    )
    assert alternate_record.content_hash == alternate_hash
    second_binding = dict(first_binding)
    second_binding["papers"] = {
        hashes["paper_key"]: {
            **first_entry,
            "stage1_paper_artifact_id": alternate_record.artifact_id,
            "stage1_paper_artifact_path": alternate_record.path,
            "stage1_paper_artifact_hash": alternate_record.content_hash,
        }
    }
    second = _publish_manual_binding(session, registry, second_binding, upstream_registry)
    service = _loader_service(
        session.context.workspace,
        registry,
        upstream_registry,
        hashes["paper_key"],
        second,
    )

    _review, _citation, papers, _preprocess, _metadata = _load_inputs(service)

    assert len(papers) == 1
    assert not any(
        item.startswith("validation_source_authority_ambiguous")
        for item in service._validation_source_authority_diagnostics
    )


def test_resume_selects_same_exact_current_binding(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    initial = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    resumed = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert initial is not None and resumed is not None
    assert (resumed.artifact_id, resumed.content_hash) == (
        initial.artifact_id,
        initial.content_hash,
    )
    assert len(_binding_records(registry)) == 1


def test_current_binding_selection_is_deterministic(
    tmp_path: Path,
    upstream_workspace: tuple[Path, dict[str, str]],
) -> None:
    upstream_ws, hashes = upstream_workspace
    summary_path = _summary_file(upstream_ws, hashes["paper_key"])
    bridge, session, registry, upstream_registry = _binding_context(
        tmp_path, [summary_path], upstream_ws
    )
    canonical = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )
    assert canonical is not None
    payload = json.loads(Path(canonical.path).read_text(encoding="utf-8"))
    duplicate = _publish_manual_binding(
        session,
        registry,
        payload,
        upstream_registry,
        artifact_id="validation_source_binding:legacy",
    )
    assert duplicate.artifact_id != canonical.artifact_id

    selected = bridge._ensure_validation_source_binding(
        session, registry, external_registry_resolver=_upstream_resolver(upstream_registry)
    )

    assert selected is not None
    assert selected.artifact_id == canonical.artifact_id

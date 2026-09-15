from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runtime.source_intake import validate_f1_corpus_source_bundle
from runtime.stage_contracts import build_source_bundle


def _write_manifest(source_root: Path) -> tuple[Path, list[dict[str, object]]]:
    sources: list[dict[str, object]] = []
    for index in range(15):
        filename = f"paper-{index + 1:02d}.pdf"
        content = b"%PDF-1.4\n" + f"F1 source {index + 1}".encode("ascii")
        target = source_root / filename
        target.write_bytes(content)
        sources.append(
            {
                "source_id": f"f1-{index + 1:02d}",
                "relative_path": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    canonical = {
        "artifact_type": "f1_corpus_manifest",
        "artifact_version": "v1",
        "schema_version": "f1-corpus-manifest-v1",
        "corpus_id": "f1-test",
        "sources": sorted(sources, key=lambda item: str(item["source_id"]).casefold()),
    }
    payload = {
        **canonical,
        "source_root": str(source_root),
        "content_sha256": hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    manifest = source_root.parent / "f1-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest, sources


def _binding(manifest: Path, sources: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "f1-corpus-binding-v1",
        "manifest_path": str(manifest),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "source_ids": [str(source["source_id"]) for source in sources],
    }


def _bundle(source_root: Path, sources: list[dict[str, object]]):
    return build_source_bundle(
        source_mode="direct",
        project_name="f1-test",
        papers=[
            {
                "title": str(source["source_id"]),
                "pdf_path": str(source_root / str(source["relative_path"])),
            }
            for source in sources
        ],
    )


def test_f1_binding_requires_exact_manifest_selected_source_paths_and_hashes(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest, sources = _write_manifest(source_root)

    bound = validate_f1_corpus_source_bundle(
        _bundle(source_root, sources),
        binding=_binding(manifest, sources),
    )

    binding_snapshot = bound.source_snapshot["f1_corpus_binding"]
    assert binding_snapshot["gate"] == "Q"
    assert binding_snapshot["source_ids"] == [source["source_id"] for source in sources]
    assert binding_snapshot["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_f1_binding_rejects_same_bytes_from_unbound_paths(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest, sources = _write_manifest(source_root)
    copied_root = tmp_path / "copies"
    copied_root.mkdir()
    for source in sources:
        name = str(source["relative_path"])
        (copied_root / name).write_bytes((source_root / name).read_bytes())

    with pytest.raises(ValueError, match="f1_corpus_binding_source_paths_mismatch"):
        validate_f1_corpus_source_bundle(
            _bundle(copied_root, sources),
            binding=_binding(manifest, sources),
            gate="Q",
        )

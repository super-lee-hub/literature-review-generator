from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pph_downstream_rebuild.py"
_SPEC = importlib.util.spec_from_file_location("pph_downstream_rebuild", _SCRIPT)
assert _SPEC and _SPEC.loader
rebuild = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rebuild
_SPEC.loader.exec_module(rebuild)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(index: int) -> dict[str, Any]:
    return {
        "status": "success",
        "paper_info": {
            "canonical_paper_key": f"paper-{index:03d}",
            "zotero_parent_key": f"ZOTERO{index:04d}",
            "title": f"Paper {index}",
        },
        "ai_summary": {"summary": f"Unchanged summary {index}"},
    }


def _selection(topic_id: str, records: list[dict[str, Any]], source_sha: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_type": "stage1_exact_set_selection",
        "schema_version": "pph-stage1-exact-set-v1",
        "topic_id": topic_id,
        "expected_count": len(records),
        "source_manifest_sha256": source_sha,
        "ordered_paper_keys": [record["paper_info"]["canonical_paper_key"] for record in records],
        "ordered_zotero_parent_keys": [record["paper_info"]["zotero_parent_key"] for record in records],
    }
    payload["selection_hash"] = rebuild._canonical_hash(payload)
    return payload


@pytest.fixture()
def rebuild_paths(tmp_path: Path) -> tuple[rebuild.DerivationPaths, list[dict[str, Any]]]:
    records = [_record(index) for index in range(1, 9)]
    canonical = tmp_path / "input" / "canonical.json"
    _write_json(canonical, records)
    source_sha = "source-manifest-sha"
    manifest = {
        "expected_corpus_count": 8,
        "canonical_summary_count": 8,
        "unique_canonical_paper_key_count": 8,
        "unique_zotero_parent_key_count": 8,
        "canonical_summaries_sha256": _sha256(canonical),
        "selected_manifest_sha256": source_sha,
        "papers": [
            {
                "canonical_paper_key": record["paper_info"]["canonical_paper_key"],
                "zotero_parent_key": record["paper_info"]["zotero_parent_key"],
            }
            for record in records
        ],
    }
    manifest_path = tmp_path / "input" / "manifest.json"
    _write_json(manifest_path, manifest)
    selections = {
        "S01": [records[0], records[1]],
        "S02": [records[1], records[2]],
        "S03": [records[2], records[3]],
        "S04": [records[3], records[4]],
        "S05": [records[4], records[5]],
        "S90": [records[5], records[6]],
        "S91": [records[6], records[7]],
    }
    selection_dir = tmp_path / "input" / "selections"
    for topic_id, selection_records in selections.items():
        _write_json(
            selection_dir / f"{topic_id}_selection.json",
            _selection(topic_id, selection_records, source_sha),
        )
    return (
        rebuild.DerivationPaths(
            canonical_summaries=canonical,
            canonical_manifest=manifest_path,
            selection_dir=selection_dir,
            output_root=tmp_path / "output",
        ),
        records,
    )


def test_derives_exact_unchanged_subsets_and_ordered_unique_review_inputs(
    rebuild_paths: tuple[rebuild.DerivationPaths, list[dict[str, Any]]]
) -> None:
    paths, records = rebuild_paths
    audit = rebuild.derive_subsets(
        paths,
        expected_canonical_count=8,
        expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
    )

    subset = json.loads(
        (paths.output_root / "subset_summaries" / "02_platform_concession_summaries.json").read_text(encoding="utf-8")
    )
    assert subset == [records[1], records[2]]
    manifest = json.loads(
        (paths.output_root / "subset_summaries" / "02_platform_concession_summaries_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["summary_count"] == 2
    assert manifest["model_call_count"] == 0

    combined = json.loads(
        (paths.output_root / "review_inputs" / "S03_review_input_summaries.json").read_text(encoding="utf-8")
    )
    assert [item["paper_info"]["canonical_paper_key"] for item in combined] == [
        "paper-003", "paper-004", "paper-001", "paper-002", "paper-007", "paper-008"
    ]
    assert audit["provider_call_count"] == 0
    assert audit["model_call_count"] == 0
    csv_path = paths.output_root / "subset_summaries" / "subset_summary_manifest.csv"
    assert csv_path.is_file()
    assert csv_path.read_text(encoding="utf-8").splitlines()[0].startswith(
        "paper_id,title,master_summary_key,collection_id,collection_name,"
    )
    assert (paths.output_root / "downstream_rebuild_audit.md").is_file()


def test_fails_closed_on_selection_zotero_identity_mismatch(
    rebuild_paths: tuple[rebuild.DerivationPaths, list[dict[str, Any]]]
) -> None:
    paths, _ = rebuild_paths
    path = paths.selection_dir / "S01_selection.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ordered_zotero_parent_keys"][0] = "WRONGKEY"
    payload_without_hash = dict(payload)
    payload_without_hash.pop("selection_hash")
    payload["selection_hash"] = rebuild._canonical_hash(payload_without_hash)
    _write_json(path, payload)

    with pytest.raises(rebuild.DerivationError, match="identity mismatch"):
        rebuild.derive_subsets(
            paths,
            expected_canonical_count=8,
            expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
        )


def test_fails_closed_on_duplicate_or_missing_canonical_key(
    rebuild_paths: tuple[rebuild.DerivationPaths, list[dict[str, Any]]]
) -> None:
    paths, _ = rebuild_paths
    payload = json.loads(paths.canonical_summaries.read_text(encoding="utf-8"))
    payload[-1]["paper_info"]["canonical_paper_key"] = "paper-001"
    _write_json(paths.canonical_summaries, payload)

    with pytest.raises(rebuild.DerivationError, match="SHA-256"):
        rebuild.derive_subsets(
            paths,
            expected_canonical_count=8,
            expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
        )

    manifest = json.loads(paths.canonical_manifest.read_text(encoding="utf-8"))
    manifest["canonical_summaries_sha256"] = _sha256(paths.canonical_summaries)
    _write_json(paths.canonical_manifest, manifest)
    with pytest.raises(rebuild.DerivationError, match="duplicate canonical_paper_key"):
        rebuild.derive_subsets(
            paths,
            expected_canonical_count=8,
            expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
        )


def test_fails_closed_on_selection_count_mismatch(
    rebuild_paths: tuple[rebuild.DerivationPaths, list[dict[str, Any]]]
) -> None:
    paths, _ = rebuild_paths
    path = paths.selection_dir / "S04_selection.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["expected_count"] = 3
    payload_without_hash = dict(payload)
    payload_without_hash.pop("selection_hash")
    payload["selection_hash"] = rebuild._canonical_hash(payload_without_hash)
    _write_json(path, payload)

    with pytest.raises(rebuild.DerivationError, match="must equal"):
        rebuild.derive_subsets(
            paths,
            expected_canonical_count=8,
            expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
        )


def test_fails_closed_on_missing_selection_canonical_key(
    rebuild_paths: tuple[rebuild.DerivationPaths, list[dict[str, Any]]]
) -> None:
    paths, _ = rebuild_paths
    path = paths.selection_dir / "S91_selection.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ordered_paper_keys"][1] = "not-in-canonical-corpus"
    payload_without_hash = dict(payload)
    payload_without_hash.pop("selection_hash")
    payload["selection_hash"] = rebuild._canonical_hash(payload_without_hash)
    _write_json(path, payload)

    with pytest.raises(rebuild.DerivationError, match="missing canonical_paper_key"):
        rebuild.derive_subsets(
            paths,
            expected_canonical_count=8,
            expected_selection_counts={topic_id: 2 for topic_id in rebuild.SUBSET_SPECS},
        )

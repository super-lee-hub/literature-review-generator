"""Regression: Outline evidence pack projection (P0-3b/P0-3c).

1. Provider-facing pack size must NOT grow with unrelated Stage1 blobs
   (preprocess / stage1_input / visual metadata).
2. Provenance must still tie each entry to its original Stage1 entry bytes.
3. Pack must be deterministic and schema-validated.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from outline.evidence_projection import (
    OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE,
    OUTLINE_EVIDENCE_PACK_VERSION,
    build_pack,
    entry_source_hash,
    pack_bytes,
    project_entries,
    project_entry,
    validate_pack,
)


def _entry(key: str, *, ai_summary_scale: int = 1, blob_scale: int = 0) -> dict:
    entry = {
        "status": "success",
        "source_mode": "zotero",
        "paper_info": {"canonical_paper_key": key, "title": f"Paper {key}", "year": "2024"},
        "ai_summary": {"core_analysis": {"summary": "s" * (10 * ai_summary_scale)}},
    }
    if blob_scale:
        entry["preprocess"] = {"huge": "p" * blob_scale}
        entry["stage1_input"] = {"huge": "q" * blob_scale}
        entry["visual_metadata"] = {"huge": "r" * blob_scale}
    return entry


def _entries(keys, **kwargs):
    return [_entry(key, **kwargs) for key in keys]


def test_pack_size_is_independent_of_unrelated_stage1_blobs():
    keys = [f"10.{i}/paper.{i}" for i in range(1, 16)]
    lean = build_pack(_entries(keys, blob_scale=0))
    fat = build_pack(_entries(keys, blob_scale=2_000_000))  # ~6MB of unrelated blobs
    assert len(fat["entries"]) == 15
    # 15 x (preprocess + stage1_input + visual_metadata) = ~90MB raw -> pack delta
    # must stay tiny (blob content is projected away entirely).
    assert abs(pack_bytes(lean) - pack_bytes(fat)) < 2_000


def test_projection_keeps_only_semantic_fields():
    raw = _entry("K001", blob_scale=100_000)
    projected = project_entry(raw)
    assert set(projected) == {"status", "source_mode", "paper_info", "ai_summary", "provenance"}
    assert "preprocess" not in projected
    assert "stage1_input" not in projected
    assert "visual_metadata" not in projected


def test_provenance_hash_binds_to_original_entry_bytes():
    raw = _entry("K002", blob_scale=50_000)
    projected = project_entry(raw)
    expected = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert projected["provenance"]["source_entry_hash"] == expected == entry_source_hash(raw)


def test_pack_is_deterministic():
    keys = ["K003", "K004", "K005"]
    first = build_pack(_entries(keys), source_ref="authority.json", source_ref_sha256="a" * 64)
    second = build_pack(_entries(keys), source_ref="authority.json", source_ref_sha256="a" * 64)
    assert first["pack_payload_sha256"] == second["pack_payload_sha256"]
    assert first["entries"] == second["entries"]


def test_pack_validate_rejects_duplicate_or_missing_keys():
    dup = build_pack(_entries(["K006", "K006"]))
    with pytest.raises(ValueError, match="duplicated"):
        validate_pack(dup)
    empty = build_pack([])
    with pytest.raises(ValueError, match="no entries"):
        validate_pack(empty)


def test_pack_schema_fields():
    pack = build_pack(_entries(["K007", "K008"]), source_ref="s.json", source_ref_sha256="b" * 64, job_id="job1")
    validate_pack(pack)
    assert pack["artifact_type"] == OUTLINE_EVIDENCE_PACK_ARTIFACT_TYPE
    assert pack["artifact_version"] == OUTLINE_EVIDENCE_PACK_VERSION
    assert pack["entry_count"] == 2
    assert pack["source_ref"] == "s.json"
    assert pack["source_ref_sha256"] == "b" * 64
    assert pack["source_job_id"] == "job1"

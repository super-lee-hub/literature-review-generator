import json

import pytest

from services.evidence_manifest import (
    EvidenceManifestV1,
    build_evidence_manifest_v1,
    verified_evidence_paths,
)
from validation.evidence_resolver import build_bilingual_retrieval_queries


def test_evidence_manifest_round_trip_and_hash_verification(tmp_path):
    paths = {}
    for field, name, content in (
        ("markdown_path", "normalized.md", "source text"),
        ("chunks_path", "chunks.json", "[]"),
        ("page_index_path", "page_index.json", "[]"),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        paths[field] = str(path)
    manifest = build_evidence_manifest_v1(
        job_id="job-1", canonical_paper_key="paper-1", preprocess=paths
    )
    loaded = EvidenceManifestV1.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert set(verified_evidence_paths(loaded)) == {"normalized_text", "chunks", "page_index"}

    (tmp_path / "normalized.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verified_evidence_paths(loaded)


def test_bilingual_queries_use_stage1_english_only_for_additional_recall():
    claim = "价格不公平会提高投诉意愿。"
    artifact = {
        "analysis": {
            "ai_summary": {
                "core_analysis": {
                    "key_points": ["Price unfairness increases complaint intention"]
                }
            }
        }
    }
    queries = build_bilingual_retrieval_queries(claim, artifact)
    assert queries[0] == claim
    assert "price unfairness increases complaint intention" in queries

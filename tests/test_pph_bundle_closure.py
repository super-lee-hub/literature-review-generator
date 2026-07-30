from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from scripts import pph_bundle_closure as closure
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord


def test_topic_section_contract_matches_canonical_drafts() -> None:
    assert {
        project_id: config["expected_sections"]
        for project_id, config in closure.TOPICS.items()
    } == {"S01": 7, "S02": 5, "S03": 5, "S04": 10, "S05": 13}


def _manifest() -> dict[str, Any]:
    return {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "paper_entries": [
            {
                "paper_id": "paper-1",
                "paper_key": "paper-1",
                "title": "Supported paper",
                "authors": ["Alice Author"],
                "year": "2024",
                "journal": "Journal of Tests",
                "doi": "10.1000/test",
            }
        ],
        "occurrences": [
            {
                "occurrence_id": "occ-1",
                "citation_token": "[[cite_ref:R001]]",
                "paper_id": "paper-1",
                "paper_key": "paper-1",
                "section_number": 1,
                "section_title": "Section",
                "block_id": "block-1",
                "block_order": 1,
                "ref_id": "R001",
            }
        ],
        "clusters": [{"paper_id": "paper-1", "paper_key": "paper-1"}],
        "bibliography": [
            {
                "paper_id": "paper-1",
                "paper_key": "paper-1",
                "citation_text": "Author, A. (2024). Supported paper. Journal of Tests.",
                "is_cited": True,
            }
        ],
    }


def _draft(text: str = "A supported statement. [[cite_ref:R001]]") -> dict[str, Any]:
    return {
        "artifact_type": "review_draft",
        "artifact_version": "v2",
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Section",
                    "blocks": [{"block_id": "block-1", "text": text}],
                }
            ]
        },
    }


def _record(
    artifact_id: str,
    artifact_type: str,
    content_hash: str,
    *,
    depends_on: Sequence[ArtifactDependencyRefV2] = (),
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version="v1",
        path=f"C:/tmp/{artifact_id.replace(':', '_')}.json",
        producer="test",
        job_id="job-1",
        status="ready",
        content_hash=content_hash,
        depends_on=list(depends_on),
        metadata={},
        created_at="2026-07-29T00:00:00Z",
    )


def test_render_review_markdown_uses_canonical_manifest() -> None:
    rendered = closure.render_review_markdown("Review", _draft(), _manifest())

    assert "(Author, 2024)" in rendered
    assert "## References" in rendered
    assert "Supported paper" in rendered
    assert "[[cite_ref:" not in rendered
    assert not closure._scan_rendered_text(rendered, _manifest())["bare_ref_ids"]


def test_render_review_markdown_fails_closed_on_unknown_ref() -> None:
    with pytest.raises(closure.BundleClosureError, match="unresolved citation identities"):
        closure.render_review_markdown(
            "Review",
            _draft("Unsupported identity. [[cite_ref:R999]]"),
            _manifest(),
        )


def test_require_exact_dependencies_uses_multiset() -> None:
    draft = _record("review_draft_v2:full_review", "review_draft", "a" * 64)
    manifest = _record("citation_manifest:v3", "citation_manifest", "b" * 64)
    docx = _record(
        "review_docx:test.docx",
        "review_docx",
        "c" * 64,
        depends_on=(
            _dependency(draft),
            _dependency(manifest),
            _dependency(manifest),
        ),
    )

    with pytest.raises(closure.BundleClosureError, match="exactly match"):
        closure._require_exact_dependencies(docx, [draft, manifest], label="DOCX")


def _dependency(record: ArtifactRecord) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
    )


def test_build_exclusion_rows_keeps_current_four_and_stale_tombstone() -> None:
    eligibility = tuple(
        {
            "zotero_key": key,
            "title": key,
            "doi": "",
            "index_system": "SSRN-WP" if key == "F99AI44H" else "CSSCI",
            "eligibility": "excluded",
            "exclusion_reason": "excluded",
            "has_pdf": "true",
            "live_readback_verified": "true",
            "control_high_water_version": "56570",
        }
        for key in sorted(closure.CURRENT_EXCLUDED_KEYS)
    )
    readiness = tuple(
        {
            "zotero_key": key,
            "title": key,
            "journal": "Journal",
            "status": "DO_NOT_CITE",
        }
        for key in sorted(closure.CURRENT_EXCLUDED_KEYS)
    )
    acceptance = closure.AcceptanceClosure(
        root=Path("C:/closure"),
        manifest={"control_snapshot": {"high_water_version": 56570}},
        eligibility_rows=eligibility,
        readiness_rows=readiness,
        claim_rows=(),
        exact_set_audit={},
        evidence_coverage_audit={},
        claim_map_audit={},
    )

    rows = closure.build_exclusion_rows(acceptance)

    assert {row["zotero_key"] for row in rows} == {
        *closure.CURRENT_EXCLUDED_KEYS,
        closure.STALE_EXCLUDED_KEY,
    }
    stale = next(row for row in rows if row["zotero_key"] == closure.STALE_EXCLUDED_KEY)
    assert stale["present_in_current_closure"] == "false"
    assert stale["index_system"] == "NOT-SSCI"


def test_keyless_theoretical_claim_is_not_reported_as_source_ready() -> None:
    acceptance = closure.AcceptanceClosure(
        root=Path("C:/closure"),
        manifest={},
        eligibility_rows=(),
        readiness_rows=(),
        claim_rows=(
            {
                "claim_id": "C05-03",
                "claim": "A theoretical moderation hypothesis.",
                "zotero_keys": "",
            },
        ),
        exact_set_audit={},
        evidence_coverage_audit={},
        claim_map_audit={},
    )

    row = closure.build_argument_evidence_rows(acceptance)[0]

    assert row["formal_source_count"] == 0
    assert row["all_sources_citation_ready"] == "false"
    assert row["all_sources_pdf_available"] == "false"
    assert row["source_scope"] == "keyless_theoretical_proposition_or_hypothesis"


def test_verify_detached_hash_rejects_manifest_tampering(tmp_path: Path) -> None:
    manifest_path = tmp_path / "15_final_closure_manifest.json"
    detached_path = tmp_path / "16_final_closure_manifest.sha256"
    manifest_path.write_text("{}\n", encoding="utf-8")
    detached_path.write_text(
        f"{closure.file_sha256(manifest_path)}  {manifest_path.name}\n",
        encoding="ascii",
    )
    manifest_path.write_text('{"tampered": true}\n', encoding="utf-8")

    with pytest.raises(closure.BundleClosureError, match="detached hash mismatch"):
        closure._verify_detached_hash(
            manifest_path,
            detached_path,
            label="acceptance closure manifest",
        )


def test_audit_built_bundle_detects_content_tampering(tmp_path: Path) -> None:
    content = tmp_path / "content.txt"
    content.write_text("original\n", encoding="utf-8")
    rows = [
        {
            "relative_path": content.name,
            "size_bytes": content.stat().st_size,
            "sha256": closure.file_sha256(content),
        }
    ]
    closure._write_csv(
        tmp_path / "12_file_hash_audit.csv",
        rows,
        ["relative_path", "size_bytes", "sha256"],
    )
    audit_path = tmp_path / "12_file_hash_audit.csv"
    closure._write_json(
        tmp_path / "13_bundle_closure_manifest.json",
        {
            "topic_count": 5,
            "all_topics_clean": True,
            "file_hash_audit": {"sha256": closure.file_sha256(audit_path)},
        },
    )
    manifest_path = tmp_path / "13_bundle_closure_manifest.json"
    closure._write_text(
        tmp_path / "13_bundle_closure_manifest.sha256",
        f"{closure.file_sha256(manifest_path)}  {manifest_path.name}",
    )
    content.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(closure.BundleClosureError, match="bundle file size mismatch|bundle file hash mismatch"):
        closure.audit_built_bundle(tmp_path)


def test_audit_built_bundle_rejects_extra_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = tmp_path / "content.txt"
    content.write_text("canonical\n", encoding="utf-8")
    content_row = {
        "relative_path": content.name,
        "size_bytes": content.stat().st_size,
        "sha256": closure.file_sha256(content),
    }
    closure._write_csv(
        tmp_path / "12_file_hash_audit.csv",
        [content_row],
        ["relative_path", "size_bytes", "sha256"],
    )
    audit_path = tmp_path / "12_file_hash_audit.csv"
    closure._write_json(
        tmp_path / "13_bundle_closure_manifest.json",
        {
            "topic_count": 5,
            "all_topics_clean": True,
            "content_files": [content_row],
            "file_hash_audit": {"sha256": closure.file_sha256(audit_path)},
        },
    )
    manifest_path = tmp_path / "13_bundle_closure_manifest.json"
    closure._write_text(
        tmp_path / "13_bundle_closure_manifest.sha256",
        f"{closure.file_sha256(manifest_path)}  {manifest_path.name}",
    )
    monkeypatch.setattr(
        closure,
        "_expected_bundle_paths",
        lambda: {
            content.name,
            "12_file_hash_audit.csv",
            "13_bundle_closure_manifest.json",
            "13_bundle_closure_manifest.sha256",
        },
    )
    (tmp_path / "unexpected.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(closure.BundleClosureError, match="filesystem does not exactly match"):
        closure.audit_built_bundle(tmp_path)


def test_publish_staged_bundle_restores_backup_after_final_audit_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    bundle_dir = output_root / "pph_review_bundle_final"
    bundle_dir.mkdir()
    (bundle_dir / "old.txt").write_text("old\n", encoding="utf-8")
    staging = output_root / ".pph_review_bundle_final.staging-123"
    staging.mkdir()
    (staging / "new.txt").write_text("new\n", encoding="utf-8")

    def fail_final_audit(_bundle_dir: Path) -> dict[str, Any]:
        raise RuntimeError("post-move audit failed")

    monkeypatch.setattr(closure, "audit_built_bundle", fail_final_audit)

    with pytest.raises(RuntimeError, match="post-move audit failed"):
        closure._publish_staged_bundle(
            staging,
            bundle_dir,
            output_root=output_root,
        )

    assert (bundle_dir / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert not (bundle_dir / "new.txt").exists()

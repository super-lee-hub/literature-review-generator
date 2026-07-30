from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pph_stage1_rebuild as rebuild
from services.paper_identity import build_canonical_paper_key
from zotero_parser import parse_zotero_report_result


ARTIFACT_NAMES = (
    "00_frozen_control_snapshot.json",
    "01_live_zotero_metadata_snapshot.json",
    "02_section_memberships.csv",
    "03_canonical_registry.jsonl",
    "04_journal_index_eligibility_audit.csv",
    "05_eligibility_manifest.csv",
    "06_citation_readiness.csv",
    "07_pdf_status.csv",
    "08_missing_pdfs.csv",
    "09_evidence_matrix.csv",
    "10_evidence_coverage_audit.json",
    "11_claim_citation_map.csv",
    "12_claim_map_audit.json",
    "13_exact_set_audit.json",
    "14_file_hash_audit.csv",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _topic_memberships() -> dict[str, set[int]]:
    return {
        "S01": set(range(1, 20)),
        "S02": {0, *range(20, 40)},
        "S03": set(range(40, 65)),
        "S04": set(range(65, 84)),
        "S05": set(range(1, 16)),
        "S90": set(range(16, 22)),
        "S91": set(range(22, 29)),
    }


def _write_fixture(tmp_path: Path) -> SimpleNamespace:
    closure = tmp_path / "closure"
    package = tmp_path / "package"
    pdf_root = package / "PDF"
    closure.mkdir()
    pdf_root.mkdir(parents=True)

    topic_sets = _topic_memberships()
    topic_by_id = {
        topic_id: (topic_name, expected)
        for topic_id, topic_name, expected in rebuild.TOPIC_SPECS
    }
    parent_memberships: dict[str, list[tuple[str, str]]] = {}
    membership_rows: list[dict[str, object]] = []
    eligibility_rows: list[dict[str, object]] = []
    package_rows: list[dict[str, object]] = []
    parents: list[dict[str, object]] = []
    children_by_parent: list[dict[str, object]] = []
    collection_counts: dict[str, int] = {}
    kalyanaram_pdf_hash = ""

    for index in range(rebuild.FROZEN_PARENT_COUNT):
        parent_key = f"P{index:07d}"
        attachment_key = f"A{index:07d}"
        eligible = index < rebuild.CORPUS_SIZE
        if index == 0:
            title = "Empirical Generalizations from Reference Price Research"
            doi = rebuild.KALYANARAM_CANONICAL_KEY
            first_name = "Gurumurthy"
            last_name = "Kalyanaram"
            year = "1995"
        else:
            title = f"Fixture Paper {index:03d}"
            doi = f"10.5555/fixture.{index:03d}"
            first_name = "Author"
            last_name = f"Number{index:03d}"
            year = "2020"
        filename = f"{index + 1:03d}_fixture_{index:03d}.pdf"
        pdf_path = pdf_root / filename
        pdf_path.write_bytes(f"%PDF-1.4\nfixture-{index}\n%%EOF\n".encode())
        pdf_hash = _sha(pdf_path)
        if index == 0:
            kalyanaram_pdf_hash = pdf_hash

        memberships: list[tuple[str, str]] = []
        if eligible:
            for topic_id, members in topic_sets.items():
                if index not in members:
                    continue
                topic_name, _expected = topic_by_id[topic_id]
                collection_key = f"C_{topic_id}"
                memberships.append((topic_name, collection_key))
        else:
            memberships.append(("00_种子文献与总览", "C_S00"))
        parent_memberships[parent_key] = memberships
        for collection_name, collection_key in memberships:
            collection_counts[collection_name] = (
                collection_counts.get(collection_name, 0) + 1
            )
            membership_rows.append(
                {
                    "paper_id": parent_key,
                    "title": title,
                    "doi": doi,
                    "existing_zotero_key": parent_key,
                    "collection_id": collection_key,
                    "collection_name": collection_name,
                    "evidence_roles": "fixture",
                    "source_packages": "fixture",
                    "resolved_zotero_key": parent_key,
                    "collection_key": collection_key,
                    "readback_verified": "true",
                }
            )

        parents.append(
            {
                "key": parent_key,
                "data": {
                    "key": parent_key,
                    "title": title,
                    "DOI": doi,
                    "date": year,
                    "version": 56570,
                    "publicationTitle": "Journal of Fixtures",
                    "creators": [
                        {
                            "creatorType": "author",
                            "firstName": first_name,
                            "lastName": last_name,
                        }
                    ],
                },
            }
        )
        children_by_parent.append(
            {
                "parent_key": parent_key,
                "children": [
                    {
                        "key": attachment_key,
                        "data": {
                            "key": attachment_key,
                            "parentItem": parent_key,
                            "contentType": "application/pdf",
                        },
                    }
                ],
            }
        )
        collection_names = [name for name, _key in memberships]
        eligibility_rows.append(
            {
                "paper_id": parent_key,
                "title": title,
                "doi": doi,
                "zotero_key": parent_key,
                "collections": "; ".join(collection_names),
                "eligibility": "eligible" if eligible else "excluded",
                "exclusion_reason": "" if eligible else "fixture exclusion",
                "has_pdf": "true",
                "pdf_path": str(pdf_path),
                "index_system": "SSCI",
                "pdf_attachment_count": "1",
                "pdf_attachment_keys": attachment_key,
                "managed_collection_keys": "; ".join(
                    key for _name, key in memberships
                ),
                "live_readback_verified": "true",
                "control_high_water_version": "56570",
            }
        )
        package_rows.append(
            {
                "序号": index + 1,
                "文件名": filename,
                "题名": title,
                "作者": f"{first_name} {last_name}",
                "年份": year,
                "DOI": doi,
                "所属集合": " | ".join(collection_names),
                "来源": "fixture",
                "字节数": pdf_path.stat().st_size,
                "SHA256": pdf_hash,
            }
        )

    metadata_path = closure / "01_live_zotero_metadata_snapshot.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parents": parents,
                "children_by_parent": children_by_parent,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_csv(
        closure / "02_section_memberships.csv",
        membership_rows,
        [
            "paper_id",
            "title",
            "doi",
            "existing_zotero_key",
            "collection_id",
            "collection_name",
            "evidence_roles",
            "source_packages",
            "resolved_zotero_key",
            "collection_key",
            "readback_verified",
        ],
    )
    _write_csv(
        closure / "05_eligibility_manifest.csv",
        eligibility_rows,
        [
            "paper_id",
            "title",
            "doi",
            "zotero_key",
            "collections",
            "eligibility",
            "exclusion_reason",
            "has_pdf",
            "pdf_path",
            "index_system",
            "pdf_attachment_count",
            "pdf_attachment_keys",
            "managed_collection_keys",
            "live_readback_verified",
            "control_high_water_version",
        ],
    )
    _write_csv(
        package / "_文件清单.csv",
        package_rows,
        [
            "序号",
            "文件名",
            "题名",
            "作者",
            "年份",
            "DOI",
            "所属集合",
            "来源",
            "字节数",
            "SHA256",
        ],
    )
    (package / "_打包摘要.json").write_text(
        json.dumps(
            {
                "collection_counts": collection_counts,
                "unique_parent_items": rebuild.FROZEN_PARENT_COUNT,
                "packaged_pdfs": rebuild.FROZEN_PARENT_COUNT,
                "missing_pdfs": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    for name in ARTIFACT_NAMES:
        path = closure / name
        if path.exists():
            continue
        path.write_text(f"fixture artifact: {name}\n", encoding="utf-8")
    artifacts = [
        {
            "relative_path": name,
            "record_count": 1,
            "size_bytes": (closure / name).stat().st_size,
            "sha256": _sha(closure / name),
        }
        for name in ARTIFACT_NAMES
    ]
    closure_manifest = closure / "15_final_closure_manifest.json"
    closure_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "closure_id": "acceptance_closure_20260728",
                "acceptance_counts": {
                    "parents": rebuild.FROZEN_PARENT_COUNT,
                    "eligible": rebuild.CORPUS_SIZE,
                    "excluded": rebuild.EXCLUDED_COUNT,
                },
                "artifacts": artifacts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (closure / "16_final_closure_manifest.sha256").write_text(
        f"{_sha(closure_manifest)}  15_final_closure_manifest.json\n",
        encoding="utf-8",
    )

    config = tmp_path / "config.ini"
    config.write_text(
        "\n".join(
            [
                "[Paths]",
                "output_path = ./old-output",
                "library_path = D:/unchanged",
                "",
                "[Primary_Reader_API]",
                "api_key = TOP_SECRET_FIXTURE",
                "model = fixture-model",
                "",
                "[Performance]",
                "max_workers = 5",
                "api_retry_attempts = 7",
                "",
                "[Validation]",
                "max_workers = 9",
                "stage2_enabled = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    summary = tmp_path / "kalyanaram_summaries.json"
    summary.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {
                        "canonical_paper_key": rebuild.KALYANARAM_CANONICAL_KEY,
                        "title": (
                            "Empirical Generalizations from Reference Price Research"
                        ),
                        "authors": ["Gurumurthy Kalyanaram", "Russell S. Winer"],
                        "year": "1995",
                        "doi": rebuild.KALYANARAM_CANONICAL_KEY,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        closure=closure,
        package=package,
        config=config,
        summary=summary,
        summary_hash=_sha(summary),
        kalyanaram_pdf_hash=kalyanaram_pdf_hash,
    )


def _install_ready_source_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_build_zotero_source_bundle(
        *,
        project_name: str,
        zotero_report: str,
        library_path: str,
    ) -> SimpleNamespace:
        del project_name
        calls.append((zotero_report, library_path))
        parsed = parse_zotero_report_result(zotero_report)
        items = [
            SimpleNamespace(
                canonical_paper_key=build_canonical_paper_key(paper),
                source_pdf=str(Path(library_path) / paper["attachments"][0]),
            )
            for paper in parsed.papers
        ]
        return SimpleNamespace(
            paper_work_items=items,
            source_snapshot={
                "matched_count": rebuild.CORPUS_SIZE,
                "missing_titles": [],
                "ambiguous_matches": [],
                "quarantined_sources": [],
                "canonical_ready": True,
            },
            fingerprint=lambda: "fixture-source-bundle-fingerprint",
        )

    monkeypatch.setattr(
        rebuild,
        "build_zotero_source_bundle",
        fake_build_zotero_source_bundle,
    )
    return calls


def _patch_fixture_hashes(
    monkeypatch: pytest.MonkeyPatch,
    fixture: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        rebuild,
        "KALYANARAM_SOURCE_PDF_SHA256",
        fixture.kalyanaram_pdf_hash,
    )
    monkeypatch.setattr(
        rebuild,
        "KALYANARAM_SUMMARY_SHA256",
        fixture.summary_hash,
    )


def test_build_prepares_exact_corpus_config_and_single_reuse_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_fixture(tmp_path)
    _patch_fixture_hashes(monkeypatch, fixture)
    bundle = tmp_path / "bundle"

    result = rebuild.build_rebuild_bundle(
        closure_dir=fixture.closure,
        package_dir=fixture.package,
        output_dir=bundle,
        source_config=fixture.config,
        kalyanaram_summary=fixture.summary,
    )

    assert result["status"] == "ready"
    assert result["provider_executed"] is False
    assert result["corpus_count"] == 84
    assert result["topic_counts"] == {
        "S01": 19,
        "S02": 21,
        "S03": 25,
        "S04": 19,
        "S05": 15,
        "S90": 6,
        "S91": 7,
    }
    assert result["topic_union_count"] == 84
    assert len(list((bundle / rebuild.SELECTED_LIBRARY_NAME).glob("*.pdf"))) == 84
    parsed = parse_zotero_report_result(str(bundle / rebuild.ZOTERO_REPORT_NAME))
    assert parsed.status == "ok"
    assert len(parsed.records) == 84
    assert {
        paper["source_identity_policy"] for paper in parsed.papers
    } == {"frozen-source-sha256-v1"}
    assert all(paper["source_pdf_sha256"] for paper in parsed.papers)

    derived_config = (bundle / rebuild.DERIVED_CONFIG_NAME).read_text(
        encoding="utf-8"
    )
    assert "TOP_SECRET_FIXTURE" in derived_config
    assert f"output_path = {bundle / rebuild.RUNTIME_OUTPUT_NAME}" in derived_config
    assert derived_config.count("max_workers = 1") == 2
    serialized_public_artifacts = "\n".join(
        [
            json.dumps(result, ensure_ascii=False),
            (bundle / rebuild.SELECTED_MANIFEST_NAME).read_text(encoding="utf-8"),
            (bundle / rebuild.BUNDLE_MANIFEST_NAME).read_text(encoding="utf-8"),
            (bundle / rebuild.PARENT_SPEC_NAME).read_text(encoding="utf-8"),
        ]
    )
    assert "TOP_SECRET_FIXTURE" not in serialized_public_artifacts

    spec = json.loads(
        (bundle / rebuild.PARENT_SPEC_NAME).read_text(encoding="utf-8")
    )
    assert spec["reuse_stage1"] is True
    assert spec["reuse_summary_files"] == [str(fixture.summary.resolve())]
    assert spec["metadata"]["prep_provider_executed"] is False
    assert spec["metadata"]["user_authorized_execution"] is True
    assert spec["metadata"]["requested_stages"] == ["source_intake", "analyze"]
    assert list((bundle / rebuild.RUNTIME_OUTPUT_NAME).iterdir()) == []
    assert rebuild.audit_bundle(bundle)["status"] == "clean"

    stale = bundle / rebuild.RUNTIME_OUTPUT_NAME / "old_summaries.json"
    stale.write_text("[]", encoding="utf-8")
    with pytest.raises(
        rebuild.Stage1RebuildError,
        match="not pristine and empty",
    ):
        rebuild.build_parent_spec(
            bundle,
            kalyanaram_summary=fixture.summary,
        )


def test_closure_hash_chain_tamper_fails_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_fixture(tmp_path)
    _patch_fixture_hashes(monkeypatch, fixture)
    _install_ready_source_intake(monkeypatch)
    with (fixture.closure / "01_live_zotero_metadata_snapshot.json").open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write("\n")
    output = tmp_path / "bundle"

    with pytest.raises(rebuild.Stage1RebuildError, match="hash chain is broken"):
        rebuild.build_rebuild_bundle(
            closure_dir=fixture.closure,
            package_dir=fixture.package,
            output_dir=output,
            source_config=fixture.config,
            kalyanaram_summary=fixture.summary,
        )

    assert not output.exists()


def test_package_pdf_hash_tamper_fails_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_fixture(tmp_path)
    _patch_fixture_hashes(monkeypatch, fixture)
    _install_ready_source_intake(monkeypatch)
    first_pdf = sorted((fixture.package / "PDF").glob("*.pdf"))[0]
    first_pdf.write_bytes(first_pdf.read_bytes() + b"tamper")
    output = tmp_path / "bundle"

    with pytest.raises(
        rebuild.Stage1RebuildError,
        match="byte size does not match",
    ):
        rebuild.build_rebuild_bundle(
            closure_dir=fixture.closure,
            package_dir=fixture.package,
            output_dir=output,
            source_config=fixture.config,
            kalyanaram_summary=fixture.summary,
        )

    assert not output.exists()


def test_runtime_source_intake_quarantine_blocks_parent_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_fixture(tmp_path)
    _patch_fixture_hashes(monkeypatch, fixture)

    def quarantined_source_bundle(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            paper_work_items=[],
            source_snapshot={
                "matched_count": 0,
                "missing_titles": [],
                "ambiguous_matches": [],
                "quarantined_sources": [
                    {
                        "title": "Blocked",
                        "reasons": ["normalized_title_not_confirmed"],
                        "expected": {"doi": "10.5555/blocked"},
                        "observed": {"doi": ""},
                    }
                ],
                "canonical_ready": False,
            },
            fingerprint=lambda: "blocked",
        )

    monkeypatch.setattr(
        rebuild,
        "build_zotero_source_bundle",
        quarantined_source_bundle,
    )
    output = tmp_path / "bundle"

    with pytest.raises(
        rebuild.Stage1RebuildError,
        match="runtime source intake contract failed",
    ):
        rebuild.build_rebuild_bundle(
            closure_dir=fixture.closure,
            package_dir=fixture.package,
            output_dir=output,
            source_config=fixture.config,
            kalyanaram_summary=fixture.summary,
        )

    assert not output.exists()

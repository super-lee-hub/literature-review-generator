from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, save_runtime_job_spec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.reconcile import validate_canonical_ai_summary
from runtime.stage_contracts import PaperWorkItem
from scripts import pph_stage1_parent as parent
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, atomic_write_json
from summary_schema import normalize_ai_summary
from tests.test_runtime_bridge_helpers import build_legacy_main


def _ai_summary(title: str, doi: str = "") -> dict:
    result = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "experiment",
                "paper_subtype_normalized": "experiment",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": None,
                "secondary_candidates": [],
            },
            "core_analysis": {
                "summary": "A complete, source-grounded test summary.",
                "key_points": ["The study reports a bounded result."],
                "methodology": "Experiment.",
                "findings": "The treatment changed the measured outcome.",
                "conclusions": "The conclusion is limited to the tested setting.",
                "relevance": "Relevant to the focal construct.",
                "limitations": "Single setting.",
                "theoretical_framework": None,
                "research_gap": None,
                "future_research_directions": [],
            },
            "paper_metadata": {
                "title": title,
                "authors": ["Alice Smith"],
                "year": "2024",
                "journal": "Journal of Tests",
                "doi": doi,
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [],
                    "data_source_and_size": "N = 100",
                    "analysis_technique": "ANOVA",
                    "core_variables": {
                        "independent": ["treatment"],
                        "dependent": ["outcome"],
                        "mediators": [],
                        "moderators": [],
                        "controls": [],
                        "other_core_constructs": [],
                    },
                    "sample_characteristics_or_context": "Adult consumers",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )
    validate_canonical_ai_summary(result, label=title)
    return result


def _registered_summary(
    output_root: Path,
    *,
    project_name: str,
    job_id: str,
    doi: str,
    title: str = "Paper A",
    source_paper_id: str | None = None,
) -> Path:
    workspace = JobWorkspace(output_root, project_name, job_id)
    workspace.ensure_exists()
    summary_path = Path(workspace.artifact_path(f"{project_name}_summaries.json"))
    atomic_write_json(
        str(summary_path),
        [
            {
                "status": "success",
                "paper_info": {
                    "title": title,
                    "authors": ["Alice Smith"],
                    "year": "2024",
                    "doi": doi,
                    "canonical_paper_key": doi,
                    "source_paper_id": source_paper_id or doi,
                },
                "ai_summary": _ai_summary(title, doi),
                "preprocess": {},
            }
        ],
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=summary_path,
        producer="test",
        artifact_id=f"summary_file:{summary_path.name}",
    )
    return summary_path


def _selected_manifest(path: Path, *, covered_doi: str) -> None:
    rows = []
    for index in range(parent.EXPECTED_CORPUS_COUNT):
        doi = covered_doi if index == 0 else f"10.9999/missing-{index:03d}"
        rows.append(
            {
                "canonical_paper_key": doi,
                "title": "Paper A" if index == 0 else f"Missing {index}",
                "authors": ["Alice Smith"],
                "year": "2024",
                "pdf_sha256": f"{index + 1:064x}",
                "selected_pdf_path": f"selected_library/{index:03d}.pdf",
            }
        )
    atomic_write_json(str(path), {"selected_sources": rows})


def test_validate_registered_summary_source_requires_registry_workspace(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    source_path = _registered_summary(
        output_root,
        project_name="project",
        job_id="job-1",
        doi="10.1000/demo",
    )
    source = parent.SummarySource(
        path=str(source_path),
        source_type="workspace",
        priority=0,
        label="project",
    )

    receipt = parent.validate_registered_summary_source(
        source,
        output_root=output_root,
    )

    assert receipt["artifact_status"] == "ready"
    assert receipt["artifact_type"] == "summary_file"
    assert receipt["job_id"] == "job-1"


def test_validate_registered_summary_source_rejects_unregistered_file(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    source_path = output_root / "legacy" / "legacy_summaries.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("[]", encoding="utf-8")
    source = parent.SummarySource(
        path=str(source_path),
        source_type="legacy_output",
        priority=0,
        label="legacy",
    )

    with pytest.raises(parent.Stage1ParentError, match="canonical artifacts"):
        parent.validate_registered_summary_source(
            source,
            output_root=output_root,
        )


def test_audit_registered_summary_coverage_partitions_frozen_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    covered_doi = parent.KALYANARAM_CANONICAL_KEY
    source_path = _registered_summary(
        output_root,
        project_name="canonical_source",
        job_id="job-1",
        doi=covered_doi,
    )
    selected_manifest = tmp_path / "selected_sources_manifest.json"
    _selected_manifest(selected_manifest, covered_doi=covered_doi)

    monkeypatch.setattr(
        parent,
        "KALYANARAM_SUMMARY_SHA256",
        parent.file_sha256(source_path),
    )
    payload = parent.audit_registered_summary_coverage(
        selected_manifest_path=selected_manifest,
        output_root=output_root,
    )

    assert payload["status"] == "findings"
    assert payload["covered_count"] == 1
    assert payload["missing_count"] == 83
    assert payload["ambiguous_count"] == 0
    assert payload["invalid_candidate_count"] == 0
    assert payload["covered"][0]["canonical_paper_key"] == covered_doi
    assert payload["covered"][0]["source_summary_sha256"] == parent.file_sha256(
        source_path
    )


def test_evidence_entry_valid_detects_changed_artifact(tmp_path: Path) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nfixture")
    entry = {
        "source_pdf": str(source_pdf),
        "source_pdf_sha256": parent.file_sha256(source_pdf),
    }
    for name in (
        "markdown",
        "chunks",
        "page_index",
        "stage1_input",
        "stage1_input_manifest",
        "stage1_quality_report",
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
        entry[f"{name}_path"] = str(path)
        entry[f"{name}_sha256"] = parent.file_sha256(path)

    assert parent._evidence_entry_valid(entry)

    Path(entry["chunks_path"]).write_text("changed", encoding="utf-8")
    assert not parent._evidence_entry_valid(entry)


def test_evidence_entry_valid_rejects_ocr_artifacts_for_off_mode(tmp_path: Path) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nfixture")
    entry = {
        "source_pdf": str(source_pdf),
        "source_pdf_sha256": parent.file_sha256(source_pdf),
        "used_ocr": True,
    }
    for name in (
        "markdown",
        "chunks",
        "page_index",
        "stage1_input",
        "stage1_input_manifest",
        "stage1_quality_report",
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
        entry[f"{name}_path"] = str(path)
        entry[f"{name}_sha256"] = parent.file_sha256(path)

    assert not parent._evidence_entry_valid(entry, ocr_mode="off")


def test_reused_evidence_recomputes_blocked_from_quality_report(tmp_path: Path) -> None:
    quality_report = tmp_path / "stage1_text_quality_report.json"
    atomic_write_json(
        str(quality_report),
        {
            "stage1_quality_level": "REPROCESS",
            "stage1_quality_reasons": ["thin_multpage_input"],
        },
    )
    normalized = parent._normalize_reused_evidence_entry(
        {
            "stage1_quality_report_path": str(quality_report),
            "stage1_quality_level": "PASS",
            "stage1_quality_reasons": [],
            "blocked": False,
        },
        ocr_mode="off",
    )

    assert normalized["stage1_quality_level"] == "REPROCESS"
    assert normalized["stage1_quality_reasons"] == ["thin_multpage_input"]
    assert normalized["blocked"] is True
    assert normalized["ocr_mode"] == "off"


def test_load_evidence_index_accepts_clean_targeted_ocr_entry(tmp_path: Path) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")
    entry = _evidence_artifacts(
        tmp_path,
        key="paper",
        source_pdf=str(source_pdf),
    )
    entry["ocr_mode"] = "always"
    entry["used_ocr"] = True
    index_path = tmp_path / parent.EVIDENCE_INDEX_NAME
    atomic_write_json(
        str(index_path),
        {
            "schema_version": parent.EVIDENCE_INDEX_SCHEMA,
            "status": "clean",
            "ocr_mode": "mixed",
            "papers": [entry],
        },
    )

    loaded = parent._load_evidence_index(
        index_path,
        [
            {
                "canonical_paper_key": "paper",
                "pdf_sha256": parent.file_sha256(source_pdf),
            }
        ],
    )

    assert loaded["paper"]["ocr_mode"] == "always"
    assert loaded["paper"]["used_ocr"] is True


def test_prepare_evidence_work_directory_is_single_owner(tmp_path: Path) -> None:
    with parent._exclusive_work_lock(tmp_path):
        with pytest.raises(parent.Stage1ParentError, match="already owns"):
            with parent._exclusive_work_lock(tmp_path):
                pass


def test_prepare_evidence_cli_exposes_ocr_mode() -> None:
    args = parent._parser().parse_args(
        [
            "prepare-evidence",
            "--bundle-dir",
            "bundle",
            "--work-dir",
            "work",
            "--ocr-mode",
            "always",
        ]
    )

    assert args.ocr_mode == "always"
    repair_args = parent._parser().parse_args(
        [
            "repair-blocked-evidence",
            "--bundle-dir",
            "bundle",
            "--work-dir",
            "work",
        ]
    )
    assert repair_args.command == "repair-blocked-evidence"


def test_no_ocr_preprocess_uses_fast_fitz_profile(tmp_path: Path) -> None:
    off_config = parent._local_preprocess_config(tmp_path / "off", ocr_mode="off")
    auto_config = parent._local_preprocess_config(tmp_path / "auto", ocr_mode="auto")

    assert off_config["Preprocess"]["extractor_profile"] == "fitz"
    assert auto_config["Preprocess"]["extractor_profile"] == "fitz"


def test_finalize_stage1_subagent_summary_binds_verbatim_evidence(
    tmp_path: Path,
) -> None:
    source_pdf = tmp_path / "paper.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")
    stage1_input = tmp_path / "stage1_input.md"
    stage1_input.write_text("Complete paper text.", encoding="utf-8")
    page_index = tmp_path / "page_index.json"
    page_one = "The experiment found that the treatment increased trust significantly."
    page_two = "The authors conclude that the mechanism operates through fairness."
    atomic_write_json(
        str(page_index),
        [
            {"page_number": 1, "text": page_one},
            {"page_number": 2, "text": page_two},
        ],
    )
    request_path = tmp_path / "request.json"
    final_output = tmp_path / "summary.json"
    atomic_write_json(
        str(request_path),
        {
            "schema_version": parent.GENERATION_REQUEST_SCHEMA,
            "canonical_paper_key": "10.1000/test",
            "paper_info": {
                "title": "Test Paper",
                "authors": ["A. Author"],
                "year": "2026",
                "journal": "Journal",
                "doi": "10.1000/test",
                "canonical_paper_key": "10.1000/test",
                "source_paper_id": "paper-1",
                "source_pdf": str(source_pdf),
                "source_pdf_fingerprint": parent.file_sha256(source_pdf),
            },
            "evidence": {
                "source_pdf": str(source_pdf),
                "source_pdf_sha256": parent.file_sha256(source_pdf),
                "stage1_input_path": str(stage1_input),
                "stage1_input_sha256": parent.file_sha256(stage1_input),
                "page_index_path": str(page_index),
                "page_index_sha256": parent.file_sha256(page_index),
            },
            "final_output_path": str(final_output),
        },
    )
    raw_output = tmp_path / "raw.json"
    atomic_write_json(
        str(raw_output),
        {
            "subagent_run_id": "/root/stage1-paper-001",
            "ai_summary": _ai_summary("Test Paper", "10.1000/test"),
            "evidence_anchors": [
                {
                    "page_number": 1,
                    "quote": page_one,
                    "supports_fields": ["core_analysis.findings"],
                },
                {
                    "page_number": 1,
                    "quote": "The experiment found that the treatment increased trust",
                    "supports_fields": ["core_analysis.methodology"],
                },
                {
                    "page_number": 2,
                    "quote": page_two,
                    "supports_fields": ["core_analysis.conclusions"],
                },
            ],
        },
    )

    result = parent.finalize_stage1_subagent_summary(
        request_path=request_path,
        raw_output_path=raw_output,
    )
    summary = json.loads(final_output.read_text(encoding="utf-8"))

    assert result["status"] == "clean"
    assert result["evidence_anchor_count"] == 3
    assert summary["paper_info"]["canonical_paper_key"] == "10.1000/test"
    assert summary["stage1_generation_receipt"]["subagent_run_id"].endswith("001")


def _write_runtime_config(path: Path, output_root: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[Paths]",
                f"output_path = {output_root}",
                "",
                "[Writer_API]",
                "api_key = offline",
                "model = offline",
                "api_base = https://example.invalid/v1",
                "",
                "[Outline_API]",
                "api_key = offline",
                "model = offline",
                "api_base = https://example.invalid/v1",
                "",
                "[Validator_API]",
                "api_key = offline",
                "model = offline",
                "api_base = https://example.invalid/v1",
            ]
        ),
        encoding="utf-8",
    )


def _selected_manifest_for_items(path: Path, items: list[Any]) -> None:
    rows = []
    for item in items:
        rows.append(
            {
                "canonical_paper_key": item.canonical_paper_key,
                "title": item.paper_info.get("title"),
                "authors": item.paper_info.get("authors", []),
                "year": item.paper_info.get("year", ""),
                "doi": item.paper_info.get("doi", ""),
                "pdf_sha256": parent.file_sha256(item.source_pdf),
                "selected_pdf_path": item.source_pdf,
            }
        )
    atomic_write_json(str(path), {"selected_sources": rows})


def _evidence_artifacts(
    tmp_path: Path,
    *,
    key: str,
    source_pdf: str,
) -> dict[str, Any]:
    evidence_dir = tmp_path / "evidence" / key.replace("\\", "_").replace("/", "_")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "canonical_paper_key": key,
        "source_pdf": source_pdf,
        "source_pdf_sha256": parent.file_sha256(source_pdf),
        "blocked": False,
        "stage1_quality_level": "PASS",
        "stage1_quality_reasons": [],
        "used_ocr": False,
    }
    for name in (
        "markdown",
        "chunks",
        "page_index",
        "stage1_input",
        "stage1_input_manifest",
        "stage1_quality_report",
    ):
        artifact = evidence_dir / f"{name}.json"
        payload: Any
        if name == "chunks":
            payload = [{"chunk_id": "c1", "text": f"Evidence for {key}."}]
        elif name == "page_index":
            payload = [{"page_number": 1, "text": f"Evidence for {key}."}]
        elif name == "stage1_quality_report":
            payload = {
                "stage1_quality_level": "PASS",
                "stage1_quality_reasons": [],
            }
        else:
            payload = {"key": key, "name": name}
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        entry[f"{name}_path"] = str(artifact)
        entry[f"{name}_sha256"] = parent.file_sha256(artifact)
    return entry


def test_materialize_parent_uses_runner_without_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parent, "EXPECTED_CORPUS_COUNT", 2)
    monkeypatch.setattr(parent, "audit_bundle", lambda _bundle: None)

    bundle = tmp_path / "bundle"
    work = tmp_path / "work"
    output = tmp_path / "runtime_output"
    papers = bundle / "papers"
    papers.mkdir(parents=True)
    (papers / "paper-a.pdf").write_bytes(b"%PDF-1.4\npaper a\n")
    (papers / "paper-b.pdf").write_bytes(b"%PDF-1.4\npaper b\n")
    queue = output / "_queue" / "queue.json"
    config = bundle / "config.ini"
    _write_runtime_config(config, output)

    spec = RuntimeJobSpec(
        project_name="pph-parent-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(papers)),
        job_id="parent-test-job",
        config=str(config),
        action="analyze",
        queue_file=str(queue),
        keep_legacy_projections=False,
        metadata={
            "requested_stages": ["source_intake", "analyze"],
            "validation_required": False,
            "require_clean_validation": False,
            "allow_unvalidated_when_validation_optional": True,
        },
    )
    save_runtime_job_spec(bundle / parent.PARENT_SPEC_NAME, spec)
    source_items = AgentRuntimeBridge(spec).build_source_bundle().paper_work_items
    _selected_manifest_for_items(bundle / parent.SELECTED_MANIFEST_NAME, source_items)

    reusable_path = _registered_summary(
        output,
        project_name="source",
        job_id="source-job",
        doi=source_items[0].canonical_paper_key,
        title=str(source_items[0].paper_info.get("title") or "Paper A"),
        source_paper_id=source_items[0].source_paper_id,
    )
    coverage = {
        "artifact_type": "stage1_registered_summary_coverage",
        "artifact_version": "v1",
        "schema_version": parent.COVERAGE_SCHEMA,
        "created_at": "2026-07-29T00:00:00Z",
        "status": "findings",
        "provider_executed": False,
        "selected_manifest_path": str(bundle / parent.SELECTED_MANIFEST_NAME),
        "selected_manifest_sha256": parent.file_sha256(
            bundle / parent.SELECTED_MANIFEST_NAME
        ),
        "output_root": str(output),
        "expected_corpus_count": 2,
        "covered_count": 1,
        "missing_count": 1,
        "ambiguous_count": 0,
        "invalid_candidate_count": 0,
        "covered": [
            {
                "canonical_paper_key": source_items[0].canonical_paper_key,
                "title": source_items[0].paper_info.get("title"),
                "pdf_sha256": parent.file_sha256(source_items[0].source_pdf),
                "source_path": str(reusable_path),
                "source_record_index": 0,
                "source_summary_sha256": parent.file_sha256(reusable_path),
                "source_job_id": "source-job",
                "source_artifact_id": f"summary_file:{reusable_path.name}",
                "source_registry_path": str(output / "source__source-job" / "artifact_registry.json"),
            }
        ],
        "missing": [
            {
                "canonical_paper_key": source_items[1].canonical_paper_key,
                "title": source_items[1].paper_info.get("title"),
                "pdf_sha256": parent.file_sha256(source_items[1].source_pdf),
            }
        ],
    }
    work.mkdir()
    atomic_write_json(str(work / parent.COVERAGE_REPORT_NAME), coverage)
    evidence_entries = [
        _evidence_artifacts(
            tmp_path,
            key=item.canonical_paper_key,
            source_pdf=item.source_pdf,
        )
        for item in source_items
    ]
    atomic_write_json(
        str(work / parent.EVIDENCE_INDEX_NAME),
        {
            "artifact_type": "stage1_current_pdf_evidence_index",
            "artifact_version": "v1",
            "schema_version": parent.EVIDENCE_INDEX_SCHEMA,
            "created_at": "2026-07-29T00:00:00Z",
            "status": "clean",
            "expected_count": 2,
            "prepared_count": 2,
            "blocked_count": 0,
            "blocked_keys": [],
            "papers": evidence_entries,
        },
    )
    request_receipt = work / "request.json"
    raw_receipt = work / "raw.json"
    atomic_write_json(str(request_receipt), {"request": "fixture"})
    atomic_write_json(str(raw_receipt), {"raw": "fixture"})
    generated = work / "generated_missing.json"
    atomic_write_json(
        str(generated),
        [
            {
                "status": "success",
                "paper_info": {
                    "title": source_items[1].paper_info.get("title"),
                    "authors": [],
                    "year": "",
                    "doi": "",
                    "canonical_paper_key": source_items[1].canonical_paper_key,
                    "source_paper_id": source_items[1].source_paper_id,
                    "source_pdf_fingerprint": parent.file_sha256(
                        source_items[1].source_pdf
                    ),
                },
                "ai_summary": _ai_summary(
                    str(source_items[1].paper_info.get("title") or "Paper B")
                ),
                "preprocess": {},
                "stage1_generation_receipt": {
                    "request_path": str(request_receipt),
                    "request_sha256": parent.file_sha256(request_receipt),
                    "raw_output_path": str(raw_receipt),
                    "raw_output_sha256": parent.file_sha256(raw_receipt),
                    "subagent_run_id": "fixture-subagent",
                    "source_pdf_sha256": evidence_entries[1][
                        "source_pdf_sha256"
                    ],
                    "stage1_input_sha256": evidence_entries[1][
                        "stage1_input_sha256"
                    ],
                    "page_index_sha256": evidence_entries[1][
                        "page_index_sha256"
                    ],
                    "evidence_anchors": [
                        {"page_number": 1, "quote": "fixture one"},
                        {"page_number": 1, "quote": "fixture two"},
                        {"page_number": 1, "quote": "fixture three"},
                    ],
                },
            }
        ],
    )

    report = parent.materialize_parent(
        bundle_dir=bundle,
        work_dir=work,
        generated_summary_files=[generated],
        legacy_main=build_legacy_main(),
    )

    assert report["runtime"]["job_status"] == "completed"
    assert report["runtime"]["canonical_ready"] is True
    assert report["reconcile"]["clean"] is True
    assert report["reused_summary_count"] == 1
    assert report["generated_summary_count"] == 1
    assert report["model_call_count"] == 1
    workspace = Path(report["runtime"]["workspace_path"])
    summaries = json.loads(
        (workspace / "artifacts" / "pph-parent-test_summaries.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["paper_info"]["canonical_paper_key"] for item in summaries] == [
        item.canonical_paper_key for item in source_items
    ]
    terminal_records = list(
        (workspace / "artifacts" / "runtime_stage_terminals").rglob("*.json")
    )
    analyze_terminal = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in terminal_records
        if json.loads(path.read_text(encoding="utf-8"))["stage_name"] == "analyze"
    )
    assert analyze_terminal["model_call_count"] == 1
    pointer = json.loads(
        (workspace.parent / "pph-parent-test" / "_latest_job.json").read_text(
            encoding="utf-8"
        )
    )
    assert pointer["job_id"] == "parent-test-job"
    assert pointer["status"] == "completed"


def test_build_parent_summaries_requires_exact_generated_missing_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parent, "EXPECTED_CORPUS_COUNT", 1)
    selected = tmp_path / "selected_sources_manifest.json"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    atomic_write_json(
        str(selected),
        {
            "selected_sources": [
                {
                    "canonical_paper_key": "paper",
                    "title": "Paper",
                    "pdf_sha256": parent.file_sha256(pdf),
                    "selected_pdf_path": str(pdf),
                }
            ]
        },
    )
    coverage = tmp_path / parent.COVERAGE_REPORT_NAME
    atomic_write_json(
        str(coverage),
        {
            "schema_version": parent.COVERAGE_SCHEMA,
            "selected_manifest_path": str(selected),
            "selected_manifest_sha256": parent.file_sha256(selected),
            "output_root": str(tmp_path / "output"),
            "expected_corpus_count": 1,
            "covered_count": 0,
            "missing_count": 1,
            "covered": [],
            "missing": [{"canonical_paper_key": "paper"}],
        },
    )
    evidence = tmp_path / parent.EVIDENCE_INDEX_NAME
    atomic_write_json(
        str(evidence),
        {
            "schema_version": parent.EVIDENCE_INDEX_SCHEMA,
            "status": "clean",
            "papers": [_evidence_artifacts(tmp_path, key="paper", source_pdf=str(pdf))],
        },
    )
    generated = tmp_path / "generated.json"
    atomic_write_json(
        str(generated),
        [
            {
                "status": "success",
                "paper_info": {"canonical_paper_key": "other"},
                "ai_summary": _ai_summary("Other"),
            }
        ],
    )

    with pytest.raises(parent.Stage1ParentError, match="exactly cover"):
        parent.build_parent_summaries(
            selected_manifest_path=selected,
            coverage_report_path=coverage,
            evidence_index_path=evidence,
            generated_summary_files=[generated],
            output_root=tmp_path / "output",
            source_items=[
                PaperWorkItem(
                    paper_info={"title": "Paper"},
                    source_descriptor={},
                    source_mode="direct",
                    canonical_paper_key="paper",
                    source_paper_id=str(pdf),
                    source_pdf=str(pdf),
                )
            ],
        )

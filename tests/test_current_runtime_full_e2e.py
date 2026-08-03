from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Any, Mapping

import fitz  # type: ignore

from runtime.export_bundle import ExportBundleService
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.stage_contracts import build_source_bundle
from services.job_workspace import atomic_write_json
from summary_schema import normalize_ai_summary
from validation.closure import ValidationClosureService
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _write_pdf(path: Path, title: str, finding: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        f"Title: {title}\n"
        "Methodology: A controlled empirical study with a reproducible design.\n"
        f"Results: {finding}\n"
        "Conclusion: The result is bounded by the tested context.",
    )
    document.save(path)
    document.close()


def _reader_summary(title: str, finding: str) -> dict[str, Any]:
    return normalize_ai_summary(
        {
            "common_core": {
                "title": title,
                "authors": ["Example Author"],
                "year": "2025",
                "summary": f"{title} reports a source-grounded empirical result.",
                "key_points": [finding],
                "methodology": "A controlled empirical study with a reproducible design.",
                "findings": finding,
                "conclusions": "The result is bounded by the tested context.",
                "limitations": "The result is bounded by the tested context.",
                "research_gap": "Further replication is needed.",
            },
            "type_specific_details": {
                "paper_type": "empirical",
                "data_source_and_size": "A reproducible controlled sample.",
                "analysis_technique": "Regression analysis.",
            },
        }
    )


def _test_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    target = tmp_path / "config.ini"
    parser = configparser.ConfigParser()
    parser.read(source, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Stage1_Input"]["send_extracted_text"] = "true"
    parser["Stage1_Input"]["send_selected_visuals"] = "false"
    parser["Stage1_Input"]["send_original_pdf"] = "never"
    parser["Stage1_Visual"]["enabled"] = "false"
    parser["Primary_Reader_API"]["api_key"] = "reader-test"
    parser["Primary_Reader_API"]["model"] = "reader-test"
    parser["Backup_Reader_API"]["api_key"] = "backup-test"
    parser["Backup_Reader_API"]["model"] = "backup-test"
    parser["Outline_API"]["api_key"] = "outline-test"
    parser["Outline_API"]["model"] = "outline-test"
    parser["Writer_API"]["api_key"] = "writer-test"
    parser["Writer_API"]["model"] = "writer-test"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["test_dev_fixture_mode"] = "true"
    parser["Outline"]["require_explicit_adoption"] = "true"
    with target.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return target


def test_current_three_pdf_runtime_chain_reaches_verified_export(tmp_path: Path, monkeypatch: Any) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("paper-a", "Study A", "The treatment improved the outcome."),
        ("paper-b", "Study B", "The treatment improved the outcome in a second context."),
        ("paper-c", "Study C", "The treatment improved the outcome under a third condition."),
    ]
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)

    config_path = _test_config(tmp_path)
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="current-e2e",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id="current-e2e-job",
            config=str(config_path),
            action="run_all",
            metadata={"outline_fixture_mode": True, "adopt_outline": True},
        )
    )
    session = bridge.bootstrap()
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="current-e2e",
        papers=[
            {
                "title": title,
                "authors": ["Example Author"],
                "year": "2025",
                "source_paper_id": key,
                "canonical_paper_key": key,
                "pdf_path": str(pdf_dir / f"{key}.pdf"),
                "classification": "core",
                "must_use": True,
            }
            for key, title, _finding in papers
        ],
    )
    source_ref = bridge.persist_source_bundle(session, bundle)

    # Exercise the production Stage 1 service through its configured provider
    # boundary while keeping the test deterministic and network-free.
    reader_index = 0

    def configured_reader(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        del args, kwargs
        paper_key = papers[reader_index][0]
        reader_index += 1
        title = next(item[1] for item in papers if item[0] == paper_key)
        finding = next(item[2] for item in papers if item[0] == paper_key)
        return {"status": "success", "content": _reader_summary(title, finding)}

    monkeypatch.setattr("ai_interface.get_summary_from_ai_with_fallback", configured_reader)
    analyze_result, analyze_receipts = bridge.execute_stage(
        "analyze",
        session=session,
        spec=bridge.job_spec,
        bundle=bundle,
        results={},
        attempt_id="attempt-analyze",
    )
    assert analyze_result.success is True
    assert analyze_receipts == 3
    assert source_ref.path
    assert session.context.registry.get("summary_file") is not None

    stage_results = {"stage1_analyze": analyze_result}
    outline_result, outline_receipts = bridge.execute_stage(
        "outline",
        session=session,
        spec=bridge.job_spec,
        bundle=bundle,
        results=stage_results,
        attempt_id="attempt-outline",
    )
    assert outline_result.success is True
    assert outline_result.metadata["adopted"] is True
    assert outline_receipts > 0
    assert session.context.registry.get("outline-v3:adoption") is not None

    def writer_api(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        refs = re.findall(r"R\d{3,}", prompt)
        ref_id = refs[0] if refs else "R001"
        return {
            "status": "success",
            "content": {
                "blocks": [
                    {"text": f"The evidence supports the bounded synthesis [[cite_ref:{ref_id}]]."}
                ]
            },
            "finish_reason": "stop",
            "input_tokens": 180,
            "output_tokens": 24,
            "total_tokens": 204,
            "usage_status": "measured",
        }

    monkeypatch.setattr("ai_interface._call_ai_api_detailed", writer_api)
    stage_results["stage2_outline"] = outline_result
    review_result, review_receipts = bridge.execute_stage(
        "review",
        session=session,
        spec=bridge.job_spec,
        bundle=bundle,
        results=stage_results,
        attempt_id="attempt-review",
    )
    assert review_result.success is True
    assert review_receipts > 0
    draft_record = session.context.registry.get("review_draft")
    manifest_record = next(
        record
        for record in session.context.registry.list_records()
        if record.status == "ready" and record.artifact_type == "citation_manifest"
    )
    assert draft_record is not None

    draft = json.loads(Path(draft_record.path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    assert manifest["occurrences"]
    assert all(item["spans"] for item in manifest["occurrences"])

    first_occurrence = manifest["occurrences"][0]
    evidence_records = [
        record
        for record in session.context.registry.list_records()
        if record.status == "ready"
        and record.artifact_type in {"paper_artifact", "stage1_paper_artifact"}
    ]
    assert len(evidence_records) == 3
    claim = ClaimValidationResultV1(
        claim_result_id="claim-current-e2e",
        claim_unit_ids=(),
        citation_set_key=str(first_occurrence["paper_id"]),
        paper_ids=(str(first_occurrence["paper_id"]),),
        block_ids=(str(first_occurrence["block_id"]),),
        claim_text="The evidence supports the bounded synthesis.",
        claim_context="",
        verdict=ClaimVerdict.SUPPORTED,
        reasoning_summary="The deterministic validation fixture maps the claim to its cited paper.",
        repair_hint="",
        root_causes=(),
        span_start=None,
        span_end=None,
        alignment_status="aligned",
        alignment_confidence=1.0,
        low_confidence=False,
        details={},
        evidence_candidates=(),
    )
    validation = ValidationRunResultV1.create(
        job_id=session.context.workspace.job_id,
        attempt_id="attempt-validate",
        execution_status="succeeded",
        claim_results=(claim,),
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=draft_record.artifact_id,
            review_draft_hash=draft_record.content_hash,
            citation_manifest_id=manifest_record.artifact_id,
            citation_manifest_hash=manifest_record.content_hash,
            evidence_manifest_ids=tuple(record.artifact_id for record in evidence_records),
            evidence_manifest_hashes=tuple(record.content_hash for record in evidence_records),
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    assert validation.contract_satisfied is True, validation.to_dict()
    validation_path = Path(session.context.workspace.artifact_path("validation/current-e2e.json"))
    atomic_write_json(str(validation_path), validation.to_dict())
    session.context.registry.register_file(
        artifact_id=validation.validation_run_id,
        artifact_role="validation_run_result",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests.test_current_runtime_full_e2e",
        depends_on=[
            {
                "artifact_id": draft_record.artifact_id,
                "artifact_type": draft_record.artifact_type,
                "path": draft_record.path,
                "content_hash": draft_record.content_hash,
            },
            {
                "artifact_id": manifest_record.artifact_id,
                "artifact_type": manifest_record.artifact_type,
                "path": manifest_record.path,
                "content_hash": manifest_record.content_hash,
            },
        ],
    )

    closure = ValidationClosureService(
        session.context.workspace,
        session.context.registry,
    ).inspect()
    assert closure.status == "clean", {
        "blocking_issues": closure.blocking_issues,
        "findings": closure.findings,
        "semantic": closure.semantic,
    }
    assert closure.citation_counts["unresolved_occurrences"] == 0

    export = ExportBundleService(
        session.context.workspace,
        session.context.registry,
    ).export(
        completion={"completion_status": "complete", "stage_statuses": ["analyze", "outline", "review"]},
        closure=closure.to_dict(),
    )
    assert export.status == "canonical_verified", export.to_dict()
    assert Path(export.bundle_path).is_file()
    assert bridge.finalize(session)

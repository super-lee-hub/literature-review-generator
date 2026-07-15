from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any

from services.artifact_registry import file_sha256
from summary_schema import normalize_ai_summary
from validation.evidence_resolver import (
    EvidenceResolver,
    build_bilingual_retrieval_queries,
    build_evidence_resolver_context,
)
from validation.run_result import (
    ClaimValidationResultV1,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _record_provider_call(kind: str, key: str) -> None:
    counter = os.environ.get("SYNTHETIC_PROVIDER_COUNTER", "").strip()
    if not counter:
        return
    path = Path(counter)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "key": key}, ensure_ascii=True) + "\n")


def stage_handler(stage_name: str, request: Any) -> dict[str, Any]:
    if os.environ.get("SYNTHETIC_CANCEL_STAGE", "").strip() == stage_name:
        raise KeyboardInterrupt(f"synthetic cancellation at {stage_name}")
    if stage_name == "stage1_analyze":
        summaries = []
        for index, item in enumerate(request.source_bundle.paper_work_items, start=1):
            paper_key = item.canonical_paper_key
            _record_provider_call("stage1", f"paper-{index:03d}")
            pdf_path = Path(item.source_pdf).resolve()
            preprocess_dir = Path(request.workspace_path) / "synthetic_preprocess" / paper_key
            preprocess_dir.mkdir(parents=True, exist_ok=True)
            normalized = preprocess_dir / "normalized.md"
            chunks = preprocess_dir / "chunks.json"
            page_index = preprocess_dir / "page_index.json"
            evidence = f"Paper {index} reports a synthetic bilingual effect. 第{index}篇论文报告了效应。"
            normalized.write_text(evidence, encoding="utf-8")
            chunks.write_text(
                json.dumps([{"chunk_id": "c1", "text": evidence}], ensure_ascii=False),
                encoding="utf-8",
            )
            page_index.write_text(
                json.dumps([{"page_number": 1, "text": evidence}], ensure_ascii=False),
                encoding="utf-8",
            )
            summaries.append(
                {
                    "status": "success",
                    "paper_info": {
                        "title": f"Synthetic Paper {index:03d}",
                        "authors": [f"Author {index:03d}"],
                        "year": "2024",
                        "journal": "Synthetic Journal",
                        "doi": f"10.9999/synthetic.{index:03d}",
                        "pdf_path": str(pdf_path),
                        "canonical_paper_key": paper_key,
                        "source_paper_id": item.source_paper_id,
                        "source_mode": "direct",
                        "source_pdf": str(pdf_path),
                        "source_pdf_fingerprint": file_sha256(pdf_path),
                        "classification": "core" if index <= 20 else "support",
                        "must_use": index <= 20,
                    },
                    "ai_summary": normalize_ai_summary(
                        normalize_ai_summary(
                            {
                                "paper_metadata": {
                                    "title": f"Synthetic Paper {index:03d}",
                                    "authors": [f"Author {index:03d}"],
                                    "year": "2024",
                                    "journal": "Synthetic Journal",
                                    "doi": f"10.9999/synthetic.{index:03d}",
                                },
                                "core_analysis": {
                                    "summary": "How the synthetic mechanism operates.",
                                    "key_points": [evidence],
                                    "methodology": "Deterministic fixture analysis.",
                                    "findings": evidence,
                                    "conclusions": "The fixture provides deterministic evidence.",
                                },
                            }
                        )
                    ),
                    "themes": [f"stream-{(index - 1) % 3 + 1}"],
                    "methods": ["experiment"],
                    "stage1_input": {
                        "input_mode": "text",
                        "selected_visual_refs": [],
                        "multimodal_capability": {},
                    },
                    "text_length": len(evidence),
                    "processing_time": "0.0",
                    "preprocess": {
                        "markdown_path": str(normalized),
                        "chunks_path": str(chunks),
                        "page_index_path": str(page_index),
                    },
                }
            )
        return {"summaries": summaries, "model_call_count": len(summaries)}
    if stage_name == "stage2_outline":
        _record_provider_call("stage2", "outline")
        if os.environ.get("SYNTHETIC_FAST_OUTLINE", "").strip() == "1":
            return {
                "outline_text": "# Synthetic outline\n\n## 1. Synthetic synthesis",
                "model_call_count": 1,
            }
        return {
            "use_generator_outline_v2": True,
            "adopt_outline_v2": True,
            "adopted_by": "synthetic-e2e",
            "adoption_reason": "explicit deterministic E2E adoption",
            "model_call_count": 0,
        }
    if stage_name == "stage3_review":
        _record_provider_call("stage3", "review")
        return {
            "review_sections": [
                {
                    "section_number": 1,
                    "section_title": "Synthetic synthesis",
                    "content": "第一句。第二句 reports a bilingual effect. [[cite_ref:R001]]",
                }
            ],
            "model_call_count": 1,
        }
    raise AssertionError(stage_name)


def _load_preprocess_evidence(summary: dict[str, Any]) -> dict[str, Any]:
    preprocess = dict(summary.get("preprocess") or {})
    normalized_path = Path(str(preprocess.get("markdown_path") or ""))
    chunks_path = Path(str(preprocess.get("chunks_path") or ""))
    page_index_path = Path(str(preprocess.get("page_index_path") or ""))
    return {
        "normalized_text": normalized_path.read_text(encoding="utf-8"),
        "chunks": json.loads(chunks_path.read_text(encoding="utf-8")),
        "page_index": json.loads(page_index_path.read_text(encoding="utf-8")),
    }


def _build_claim_result(adapter: Any) -> ClaimValidationResultV1:
    summary = dict(adapter.summaries[0])
    paper_info = dict(summary.get("paper_info") or {})
    paper_key = str(paper_info.get("canonical_paper_key") or "paper-001")
    paper_artifact = {
        "paper_identity": {
            "canonical_paper_key": paper_info.get("canonical_paper_key", "paper-001"),
            "source_paper_id": paper_info.get("source_paper_id", "paper-001"),
        },
        "paper_metadata": dict(summary.get("ai_summary", {}).get("paper_metadata") or {}),
        "analysis": {
            "ai_summary": dict(summary.get("ai_summary") or {}),
            "preprocess": _load_preprocess_evidence(summary),
        },
        "source": {"source_pdf": paper_info.get("source_pdf", "")},
    }
    chinese_claim = "第二句展示双语效应。"
    retrieval_queries = build_bilingual_retrieval_queries(chinese_claim, paper_artifact)
    candidates = EvidenceResolver(
        build_evidence_resolver_context(paper_artifact)
    ).resolve_evidence(
        chinese_claim,
        retrieval_queries=retrieval_queries,
    )
    grounded_candidates = [
        asdict(candidate)
        for candidate in candidates
        if candidate.match_reason.startswith("bilingual_retrieval:")
    ]
    fixture_disposition = os.environ.get(
        "SYNTHETIC_VALIDATION_DISPOSITION",
        "clean",
    ).strip()
    evidence_status = {
        "clean": "clean_supported",
        "findings": "evidence_gap",
        "needs_review": "wrong_source",
    }.get(fixture_disposition)
    if evidence_status is None:
        raise ValueError(f"unsupported synthetic validation disposition: {fixture_disposition}")
    return ClaimValidationResultV1.from_validation_result(
        {
            "citation_id": paper_key,
            "citation_set_key": paper_key,
            "paper_ids": [paper_key],
            "block_ids": ["s1_b1"],
            "claim_text": "第二句 reports a bilingual effect.",
            "claim_context": "第一句。第二句 reports a bilingual effect.",
            "claim_units": [
                {
                    "claim_unit_id": "synthetic-claim-unit-1",
                    "span_start": 4,
                    "span_end": 53,
                }
            ],
            "target_claim_unit": {
                "claim_unit_id": "synthetic-claim-unit-1",
                "span_start": 4,
                "span_end": 53,
            },
            "evidence_status": evidence_status,
            "disposition": "kept" if fixture_disposition == "clean" else "manual_review",
            "reasoning_summary": "Deterministic bilingual evidence fixture.",
            "root_causes": [],
            "evidence_candidates": grounded_candidates,
            "details": {
                "retrieval_queries": retrieval_queries,
                "claim_unit_results": [
                    {
                        "claim_unit_id": "synthetic-claim-unit-1",
                        "alignment_status": "exact",
                        "alignment_confidence": 1.0,
                    }
                ],
            },
        }
    )


def run_review_validation(adapter: Any) -> dict[str, Any]:
    workspace = adapter.job_workspace
    claim_result = _build_claim_result(adapter)
    records = adapter.artifact_registry.list_records()
    review_draft_path = Path(adapter._review_draft_v2_path()).resolve()
    citation_manifest_path = Path(adapter._citation_manifest_path()).resolve()
    review_draft = next(
        record
        for record in records
        if record.artifact_type == "review_draft"
        and record.status == "ready"
        and Path(record.path).resolve() == review_draft_path
    )
    citation_manifest = next(
        record
        for record in records
        if record.artifact_type == "citation_manifest"
        and record.status == "ready"
        and Path(record.path).resolve() == citation_manifest_path
    )
    evidence_manifests = tuple(
        record
        for record in records
        if record.artifact_type == "evidence_manifest" and record.status == "ready"
    )
    result = ValidationRunResultV1.create(
        job_id=workspace.job_id,
        attempt_id=adapter.validation_attempt_id,
        execution_status="succeeded",
        claim_results=(claim_result,),
        repair_policy="report_only",
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=review_draft.artifact_id,
            review_draft_hash=review_draft.content_hash,
            citation_manifest_id=citation_manifest.artifact_id,
            citation_manifest_hash=citation_manifest.content_hash,
            evidence_manifest_ids=tuple(
                record.artifact_id for record in evidence_manifests
            ),
            evidence_manifest_hashes=tuple(
                record.content_hash for record in evidence_manifests
            ),
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    canonical = Path(workspace.artifact_path("validation_run_result_v1.json"))
    canonical.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    canonical_hash = file_sha256(canonical)
    report = Path(workspace.report_path("validation_report.txt"))
    report.write_text(
        f"validation_run_hash={canonical_hash}\n"
        f"disposition={result.validation_disposition.value}\n",
        encoding="utf-8",
    )
    projection_payload = {
        "source_validation_run_hash": canonical_hash,
        "validation_run_id": result.validation_run_id,
        "execution_status": result.execution_status.value,
        "validation_disposition": result.validation_disposition.value,
        "claim_verdict_counts": dict(result.claim_verdict_counts),
        "total_claims": result.total_claims,
        "contradicted_count": result.contradicted_count,
        "claim_results": [item.to_dict() for item in result.claim_results],
    }
    projections = {}
    for filename in ("manual_review.json", "validation_completion.json", "claim_alignment_audit.json"):
        path = Path(workspace.report_path(filename))
        path.write_text(
            json.dumps(projection_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        projections[filename] = str(path)
    return {
        "validation_run_result": result,
        "validation_run_result_file": str(canonical),
        "report_file": str(report),
        "manual_report_file": projections["manual_review.json"],
        "completion_report_file": projections["validation_completion.json"],
        "claim_alignment_audit_json": projections["claim_alignment_audit.json"],
    }

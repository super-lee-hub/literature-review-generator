from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Any, Mapping

import fitz  # type: ignore

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from summary_schema import normalize_ai_summary
from validation.closure import resolve_current_stage_closure_map


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


def _reader_summary(paper_key: str, title: str, finding: str) -> dict[str, Any]:
    summary = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "quantitative",
                "paper_subtype_normalized": "quantitative",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "controlled empirical design",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": title,
                "authors": ["Example Author"],
                "year": "2025",
                "journal": "Example Journal",
                "doi": f"10.1000/{paper_key}",
            },
            "core_analysis": {
                "summary": f"{title} reports a source-grounded empirical result.",
                "key_points": [finding],
                "methodology": "A controlled empirical study with a reproducible design.",
                "findings": finding,
                "conclusions": "The result is bounded by the tested context.",
                "relevance": "The result informs the bounded research question.",
                "limitations": "The result is bounded by the tested context.",
                "research_gap": "Further replication is needed.",
                "theoretical_framework": None,
                "future_research_directions": ["Replicate in another context."],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [
                        "Does the treatment improve the outcome?"
                    ],
                    "data_source_and_size": "A reproducible controlled sample.",
                    "analysis_technique": "Regression analysis.",
                    "core_variables": {
                        "independent": ["treatment"],
                        "dependent": ["outcome"],
                    },
                    "sample_characteristics_or_context": "Controlled context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )
    summary["status"] = "success"
    summary["paper_info"] = {
        "canonical_paper_key": paper_key,
        "source_paper_id": paper_key,
        "title": title,
        "authors": ["Example Author"],
        "year": 2025,
        "classification": "core",
        "must_use": True,
    }
    return summary


def _provider_response(content: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "content": dict(content),
        "finish_reason": "stop",
        "input_tokens": 420,
        "output_tokens": 96,
        "total_tokens": 516,
        "usage_status": "reported",
    }


def _outline_provider_response(node_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    if node_id == "relation_adjudication":
        candidates = [
            dict(item)
            for item in request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        confirmed = [
            str(item.get("relation_id") or "")
            for item in candidates
            if item.get("relation_id") and item.get("evidence_fields")
        ]
        return _provider_response(
            {
                "confirmed_relation_ids": confirmed,
                "rejected_relations": [
                    {
                        "relation_id": str(item.get("relation_id") or ""),
                        "reason": "insufficient evidence fields",
                    }
                    for item in candidates
                    if str(item.get("relation_id") or "") not in confirmed
                ],
                "method": "injected_evidence_adjudication",
            }
        )

    if node_id.endswith("_provider_generation"):
        candidate_id = node_id.removesuffix("_provider_generation")
        paper_keys = [str(item) for item in request.get("paper_keys") or ()]
        organizing_logic = str(request.get("organizing_logic") or "evidence")
        evidence_rows = [
            dict(item)
            for item in request.get("evidence") or ()
            if isinstance(item, Mapping)
        ]
        claims: list[str] = []
        for row in evidence_rows[:3]:
            title = str(row.get("title") or row.get("paper_key") or "Evidence")
            findings = row.get("findings") or row.get("conclusions") or []
            finding = (
                str(findings[0])
                if isinstance(findings, list) and findings
                else str(findings or "recorded finding")
            )
            claims.append(f"{title}: {finding}")
        if not claims:
            claims = [f"The corpus records evidence organized by {organizing_logic}."]
        relation_ids = list(request.get("relation_ids") or ())[:8]
        sections = [
            {
                "section_id": f"{candidate_id}_section_{index}",
                "title": f"{organizing_logic.replace('_', ' ').title()} synthesis {index}",
                "goal": "Integrate one bounded evidence cluster by research logic",
                "paper_keys": [paper_key],
                "relation_ids": relation_ids,
                "claims": [claims[index - 1] if index <= len(claims) else claims[0]],
            }
            for index, paper_key in enumerate(paper_keys, start=1)
        ]
        return _provider_response(
            {
                "candidate_id": candidate_id,
                "organizing_logic": organizing_logic,
                "sections": sections,
                "claims": claims,
            }
        )

    if node_id.endswith("_critique") or node_id in {
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
    }:
        return _provider_response(
            {
                "node_id": node_id,
                "passed": True,
                "blocking_diagnostics": [],
                "recommendations": [],
                "score": 1.0,
            }
        )

    if node_id == "arbitration":
        candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
        return _provider_response(
            {
                "selected_candidate_id": sorted(candidate_ids)[0] if candidate_ids else "",
                "accepted_recommendations": [],
                "rejected_recommendations": [],
            }
        )

    return _provider_response({"node_id": node_id, "accepted": True})


def _adjudicator_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "supported",
        "confidence": 0.99,
        "repair_scope": "none",
        "disposition": "keep_as_is",
        "low_confidence": False,
        "reasoning": "The injected validator maps the cited claim to the durable evidence packet.",
        "repair_hint": "",
        "summary_paper_ids": [],
        "manual_review_reason": "",
        "claim_type": "result",
        "claim_type_confidence": 1.0,
        "claim_type_rationale": "The claim is a bounded empirical result.",
        "adjudication_status": "supported",
    }


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
    parser["Validator_API"]["api_key"] = "validator-test"
    parser["Validator_API"]["model"] = "validator-test"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["require_explicit_adoption"] = "true"
    parser["Validation"]["review_enabled"] = "true"
    with target.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return target


def test_current_three_pdf_runtime_chain_reaches_verified_export(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("paper-a", "Study A", "The treatment improved the outcome."),
        ("paper-b", "Study B", "The treatment improved the outcome in a second context."),
        ("paper-c", "Study C", "The treatment improved the outcome under a third condition."),
    ]
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)

    reader_index = 0

    def configured_reader(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        paper_key, title, finding = papers[reader_index]
        reader_index += 1
        return {"status": "success", "content": _reader_summary(paper_key, title, finding)}

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        envelope = json.loads(prompt)
        return _outline_provider_response(
            str(envelope["node_id"]),
            dict(envelope["request"]),
        )

    def configured_writer(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        ref_ids = re.findall(r"R\d{3,}", prompt)
        ref_id = ref_ids[0] if ref_ids else "R001"
        return _provider_response(
            {
                "blocks": [
                    {
                        "text": (
                            "The evidence supports the bounded synthesis "
                            f"[[cite_ref:{ref_id}]]."
                        )
                    }
                ]
            }
        )

    monkeypatch.setattr("ai_interface.get_summary_from_ai_with_fallback", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed", configured_writer)
    monkeypatch.setattr("ai_interface._call_ai_api", _adjudicator_response)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", _adjudicator_response)

    spec = RuntimeJobSpec(
        project_name="current-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-e2e-job",
        config=str(_test_config(tmp_path)),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={
            "requested_stages": ["analyze", "outline", "review", "validate"],
            "validation_required": True,
            "require_clean_validation": True,
            "allow_unvalidated_when_validation_optional": False,
        },
    )

    # Production orchestration reaches the explicit adoption boundary and
    # persists the failed review terminal; it does not auto-promote the outline.
    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "failed", first
    assert first.failed_stage == "review", first
    assert first.completed_stages == ("source_intake", "analyze", "outline"), first

    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    inspection = control.inspect(workspace=first.workspace_path)
    final_outline = next(
        artifact
        for artifact in inspection["artifacts"]
        if artifact["artifact_id"] == "outline-v3:final_outline"
    )
    adoption = control.adopt(
        workspace=first.workspace_path,
        artifact_id="outline-v3:final_outline",
        actor="tests.current_runtime_full_e2e",
        reason="explicitly approve the verified outline for the production review stage",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adoption["status"] == "succeeded", adoption
    assert adoption["mutation_performed"] is True

    completed = control.resume(workspace=first.workspace_path)
    assert completed["job_status"] == "completed", completed
    assert completed["completion_status"] == "complete", completed
    assert completed["canonical_ready"] is True, completed
    assert completed["completed_stages"] == (
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    ), completed

    validation_status = control.validation_status(workspace=first.workspace_path)
    assert validation_status["status"] == "clean", validation_status
    assert validation_status["read_only"] is True

    completed_inspection = control.inspect(workspace=first.workspace_path)
    _workspace, completed_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    current_stage_map = resolve_current_stage_closure_map(completed_registry)
    validation_closure_id = str(
        current_stage_map.stages["validation_receipt_closure"]["artifact_id"]
    )
    validation_closure = next(
        artifact
        for artifact in completed_inspection["artifacts"]
        if artifact["artifact_id"] == validation_closure_id
    )
    closure_payload = json.loads(Path(validation_closure["path"]).read_text(encoding="utf-8"))
    assert closure_payload["payload"]["complete"] is True

    export = control.export(workspace=first.workspace_path)
    assert export["status"] == "canonical_verified", export
    assert Path(export["bundle_path"]).is_file()

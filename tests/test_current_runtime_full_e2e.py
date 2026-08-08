from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Any, Mapping
import zipfile

import fitz  # type: ignore
import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.job_outcome import load_canonical_job_outcome
from summary_schema import normalize_ai_summary
from validation.closure import resolve_current_stage_closure_map
from validation.disposition import ValidationDispositionV1


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


def _findings_adjudicator_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "confidence": 0.99,
        "repair_scope": "claim",
        "disposition": "manual_review",
        "low_confidence": False,
        "reasoning": "The injected validator found a source-grounded unsupported claim.",
        "repair_hint": "Remove or qualify the unsupported claim.",
        "summary_paper_ids": [],
        "manual_review_reason": "The claim is not supported by the cited evidence.",
        "claim_type": "result",
        "claim_type_confidence": 1.0,
        "claim_type_rationale": "The claim is a bounded empirical result.",
        "adjudication_status": "unsupported",
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
    # This chain verifies the legacy deterministic provider fixture. Stability
    # smoke coverage is exercised by the dedicated Outline stability tests.
    parser["OutlineStability"]["mode"] = "off"
    parser["Validation"]["review_enabled"] = "true"
    with target.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return target


@pytest.mark.parametrize(
    ("adjudicator", "expected_disposition", "expected_completion", "expected_export"),
    [
        pytest.param(
            _adjudicator_response,
            "clean",
            "complete",
            "canonical_verified",
            id="clean",
        ),
        pytest.param(
            _findings_adjudicator_response,
            "findings",
            "blocked",
            "untrusted",
            id="findings",
        ),
    ],
)
def test_current_three_pdf_runtime_chain_reaches_verified_export(
    tmp_path: Path,
    monkeypatch: Any,
    adjudicator: Any,
    expected_disposition: str,
    expected_completion: str,
    expected_export: str,
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
    monkeypatch.setattr("ai_interface._call_ai_api", adjudicator)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", adjudicator)

    spec = RuntimeJobSpec(
        project_name="current-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-e2e-job",
        config=str(_test_config(tmp_path)),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={},
    )

    # Production orchestration reaches the explicit adoption boundary and
    # pauses before review; it does not auto-promote the outline.
    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
    assert first.failed_stage is None, first
    assert first.completed_stages == ("source_intake", "analyze", "outline"), first
    assert "explicit adoption" in first.message, first

    _workspace, first_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    persisted_spec_record = first_registry.get("runtime_job_spec")
    assert persisted_spec_record is not None
    persisted_spec_payload = json.loads(
        Path(persisted_spec_record.path).read_text(encoding="utf-8")
    )
    stage_plan = persisted_spec_payload["metadata"]["stage_plan"]
    assert stage_plan == {
        "version": "stage-plan-v1",
        "action": "run_all",
        "requested_stages": ["analyze", "outline", "review", "validate"],
        "required_stages": ["source_intake", "analyze", "outline", "review", "validate"],
        "validation_enabled": True,
        "validation_required": True,
        "require_clean_validation": True,
        "allow_unvalidated_when_validation_optional": False,
        "current_artifact_set_required": True,
        "validation_status": "required",
    }

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
    assert completed["completion_status"] == expected_completion, completed
    assert completed["canonical_ready"] is (expected_completion == "complete"), completed
    assert completed["completed_stages"] == (
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    ), completed

    validation_status = control.validation_status(workspace=first.workspace_path)
    assert validation_status["status"] == expected_disposition, validation_status
    assert validation_status["read_only"] is True

    completed_inspection = control.inspect(workspace=first.workspace_path)
    _workspace, completed_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    persisted_after_resume = completed_registry.get("runtime_job_spec")
    assert persisted_after_resume is not None
    assert persisted_after_resume.content_hash == persisted_spec_record.content_hash
    outcome, _outcome_record = load_canonical_job_outcome(completed_registry)
    assert outcome.job_disposition == expected_disposition
    assert outcome.canonical_ready is (expected_completion == "complete")
    assert outcome.to_dict()["readiness_policy_snapshot"]["stage_plan"] == stage_plan
    current_set = completed_registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.validation_status == expected_disposition
    current_stage_map = resolve_current_stage_closure_map(completed_registry)
    assert current_stage_map.requested_stages == (
        "analyze",
        "outline",
        "review",
        "validate",
    )
    assert current_stage_map.blocking_issues == ()
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
    assert export["status"] == expected_export, export
    if expected_export == "canonical_verified":
        assert Path(export["bundle_path"]).is_file()
    else:
        assert export["bundle_path"] == ""


def test_current_runtime_optional_validation_policy_and_export(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Exercise optional validation admission and the fail-closed policy branch."""

    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("optional-a", "Optional Study A", "The treatment improved the outcome."),
        ("optional-b", "Optional Study B", "The treatment improved the outcome in a second context."),
        ("optional-c", "Optional Study C", "The treatment improved the outcome under a third condition."),
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
        return _outline_provider_response(str(envelope["node_id"]), dict(envelope["request"]))

    def configured_writer(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        ref_ids = re.findall(r"R\d{3,}", prompt)
        ref_id = ref_ids[0] if ref_ids else "R001"
        return _provider_response(
            {"blocks": [{"text": f"The evidence supports the bounded synthesis [[cite_ref:{ref_id}]]."}]}
        )

    monkeypatch.setattr("ai_interface.get_summary_from_ai_with_fallback", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed", configured_writer)

    validation_transport_count = 0

    def forbidden_validation_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal validation_transport_count
        validation_transport_count += 1
        raise AssertionError("validation transport must not run when review_enabled=false")

    monkeypatch.setattr("ai_interface._call_ai_api", forbidden_validation_transport)
    monkeypatch.setattr(
        "validation.llm_adjudicator._call_ai_api",
        forbidden_validation_transport,
    )

    config_path = _test_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Validation"]["review_enabled"] = "false"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    spec = RuntimeJobSpec(
        project_name="optional-validation-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="optional-validation-e2e-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={},
    )

    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
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
        actor="tests.current_runtime_full_e2e.optional",
        reason="explicitly approve the optional-validation outline",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adoption["status"] == "succeeded", adoption

    completed = control.resume(workspace=first.workspace_path)
    assert completed["completion_status"] == "complete", completed
    assert completed["canonical_ready"] is True, completed
    assert completed["completed_stages"] == ("source_intake", "analyze", "outline", "review"), completed
    assert validation_transport_count == 0

    status = control.validation_status(workspace=first.workspace_path)
    assert status["status"] == "not_requested", status

    workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.validation_status == "not_requested"
    disposition = registry.get(current_set.validation_disposition_artifact_id)
    assert disposition is not None
    assert disposition.artifact_type == "validation_disposition"
    assert disposition.artifact_version == "v1"
    typed_disposition = ValidationDispositionV1.from_dict(
        json.loads(Path(disposition.path).read_text(encoding="utf-8"))
    )
    assert typed_disposition.validation_enabled is False
    assert typed_disposition.validation_required is False
    assert typed_disposition.allow_unvalidated is True

    runtime_spec_record = registry.get("runtime_job_spec")
    assert runtime_spec_record is not None
    runtime_spec_payload = json.loads(Path(runtime_spec_record.path).read_text(encoding="utf-8"))
    stage_plan = runtime_spec_payload["metadata"]["stage_plan"]
    assert stage_plan["requested_stages"] == ["analyze", "outline", "review"]
    assert stage_plan["validation_enabled"] is False
    assert stage_plan["validation_required"] is False
    assert stage_plan["require_clean_validation"] is False
    assert stage_plan["allow_unvalidated_when_validation_optional"] is True
    assert stage_plan["validation_status"] == "not_requested"
    outcome, _outcome_record = load_canonical_job_outcome(registry)
    assert outcome.to_dict()["readiness_policy_snapshot"]["stage_plan"] == stage_plan
    assert outcome.canonical_ready is True

    stage_map = resolve_current_stage_closure_map(registry)
    assert stage_map.requested_stages == ("analyze", "outline", "review")
    assert stage_map.blocking_issues == ()
    assert all(
        bool(entry.get("complete"))
        for entry in stage_map.provider_closures_by_stage.values()
    )

    export = control.export(workspace=workspace.root_dir)
    assert export["status"] == "canonical_unvalidated", export
    bundle_path = Path(export["bundle_path"])
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as archive:
        manifest = json.loads(archive.read("provenance_manifest.json").decode("utf-8"))
        status_text = archive.read("EXPORT_STATUS.txt").decode("utf-8")
    assert manifest["status"] == "canonical_unvalidated"
    assert manifest["validation_status"] == "not_requested"
    assert manifest["validation_required"] is False
    assert manifest["validation_enabled"] is False
    assert manifest["allow_unvalidated"] is True
    assert manifest["validation_disposition_artifact_id"] == disposition.artifact_id
    assert manifest["validation_disposition_artifact_hash"] == disposition.content_hash
    assert "semantic validation was not performed" in manifest["validation_warning"]
    assert "status=canonical_unvalidated" in status_text
    assert "validation_status=not_requested" in status_text
    assert "allow_unvalidated=true" in status_text

    disposition_path = Path(disposition.path)
    original_disposition_bytes = disposition_path.read_bytes()
    disposition_payload = json.loads(original_disposition_bytes.decode("utf-8"))
    for mutation in (
        {"allow_unvalidated": False},
        {"stage_plan_hash": "f" * 64},
    ):
        tampered_payload = {**disposition_payload, **mutation}
        disposition_path.write_text(
            json.dumps(tampered_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tampered_export = control.export(workspace=workspace.root_dir)
        assert tampered_export["status"] == "untrusted", tampered_export
        assert tampered_export["bundle_path"] == ""
        tampered_completion = AgentRuntimeRunner.status(workspace.root_dir)
        assert tampered_completion.completion_status != "complete" or not tampered_completion.canonical_ready
        disposition_path.write_bytes(original_disposition_bytes)


def test_required_validation_disabled_fails_before_provider_transport(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    config_path = _test_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Validation"]["review_enabled"] = "false"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    transport_count = 0

    def forbidden_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal transport_count
        transport_count += 1
        raise AssertionError("provider transport occurred before validation-policy preflight")

    monkeypatch.setattr("ai_interface.get_summary_from_ai_with_fallback", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api", forbidden_transport)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", forbidden_transport)

    spec = RuntimeJobSpec(
        project_name="required-validation-disabled",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="required-validation-disabled-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={
            "requested_stages": ["analyze", "outline", "review", "validate"],
            "validation_required": True,
        },
    )

    with pytest.raises(RuntimeRunnerError, match="validation is required.*review_enabled is false"):
        AgentRuntimeRunner(spec).run()

    assert transport_count == 0
    assert not (
        tmp_path
        / "output"
        / "required-validation-disabled__required-validation-disabled-job"
    ).exists()

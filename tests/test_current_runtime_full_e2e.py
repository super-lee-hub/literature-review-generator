from __future__ import annotations

import configparser
import json
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping
import zipfile
import reviewctl

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

    if node_id.startswith("topic_synthesis_provider"):
        requested_topics = [
            dict(item)
            for item in request.get("topics") or ()
            if isinstance(item, Mapping)
        ]
        return _provider_response(
            {
                "topics": [
                    {
                        "topic_id": str(item.get("topic_id") or ""),
                        "fragment_id": str(item.get("fragment_id") or item.get("topic_id") or ""),
                        "status": "completed",
                        "conclusions": [],
                        "unresolved_questions": [],
                        "supporting_evidence_ids": list(item.get("planned_evidence_ids") or ()),
                    }
                    for item in requested_topics
                ],
                "processed_fragment_ids": [
                    str(item.get("fragment_id") or item.get("topic_id") or "")
                    for item in requested_topics
                ],
                "claims": [],
                "unresolved_questions": [],
            }
        )

    if node_id.startswith(("cross_group_comparison_provider", "global_synthesis_provider")):
        topic_ids: set[str] = set()
        fragment_ids: set[str] = set()
        result_ids: set[str] = set()
        relation_ids: set[str] = set()

        def collect_identities(value: Any) -> None:
            if isinstance(value, Mapping):
                topic_id = str(value.get("topic_id") or "")
                if topic_id:
                    topic_ids.add(topic_id)
                relation_id = str(value.get("relation_id") or "")
                if relation_id:
                    relation_ids.add(relation_id)
                fragment_id = str(value.get("fragment_id") or "")
                if fragment_id:
                    fragment_ids.add(fragment_id)
                for key in ("fragment_ids", "processed_fragment_ids"):
                    fragment_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("result_id", "batch_result_id"):
                    result_id = str(value.get(key) or "")
                    if result_id:
                        result_ids.add(result_id)
                for key in ("result_ids", "batch_result_ids", "processed_result_ids"):
                    result_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("topic_ids", "processed_topic_ids"):
                    topic_ids.update(
                        str(item) for item in value.get(key) or () if str(item)
                    )
                for key in ("relation_ids", "processed_relation_ids"):
                    relation_ids.update(
                        str(item) for item in value.get(key) or () if str(item)
                    )
                for child in value.values():
                    collect_identities(child)
            elif isinstance(value, list):
                for child in value:
                    collect_identities(child)

        collect_identities(request.get("topic_synthesis"))
        collect_identities(request.get("cross_group_comparison"))
        collect_identities(request.get("relation_candidates"))
        if node_id.startswith("cross_group_comparison_provider"):
            supported_topic = next(
                (
                    item for item in request.get("topic_synthesis") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("topic_id") or "")
                    and list(item.get("paper_ids") or ())
                    and list(item.get("supporting_evidence_ids") or ())
                ),
                None,
            )
            prior_claim = next(
                (
                    claim
                    for item in request.get("topic_synthesis") or ()
                    if isinstance(item, Mapping)
                    for semantic_result in [item.get("semantic_result")]
                    if isinstance(semantic_result, Mapping)
                    for claim in semantic_result.get("bridge_claims") or ()
                    if isinstance(claim, Mapping) and claim.get("topic_ids")
                ),
                None,
            )
            if supported_topic is not None:
                supported_id = str(supported_topic["topic_id"])
                bridge_claim_id = (
                    "synthesis:cross_group_comparison:fixture-"
                    + hashlib.sha256(supported_id.encode("utf-8")).hexdigest()[:12]
                )
                supporting_ids = [
                    str(value) for value in supported_topic.get("supporting_evidence_ids") or ()
                    if str(value)
                ]
                primary_evidence_id = supporting_ids[0]
                interpretation_context = supported_topic.get("interpretation_context") or {}
                dependencies = [
                    dependency
                    for dependency in interpretation_context.get("dependencies") or ()
                    if isinstance(dependency, Mapping)
                    and primary_evidence_id in dependency.get("primary_evidence_ids", ())
                ] if isinstance(interpretation_context, Mapping) else []
                bridge_claim = {
                    "claim_id": bridge_claim_id,
                    "topic_ids": [supported_id],
                    "fragment_id": str(supported_topic.get("fragment_id") or ""),
                    "paper_key": str((supported_topic.get("paper_ids") or [""])[0]),
                    "text": "The supplied topic evidence supports this bounded comparison with its recorded conditions.",
                    "evidence_ids": [
                        primary_evidence_id,
                        *sorted({
                            str(evidence_id)
                            for dependency in dependencies
                            for evidence_id in dependency.get("required_evidence_ids") or ()
                            if str(evidence_id)
                        }),
                    ],
                }
                if dependencies:
                    bridge_claim["source_claim_ids"] = sorted({
                        str(claim_id)
                        for dependency in dependencies
                        for claim_id in (
                            dependency.get("primary_claim_id"),
                            *list(dependency.get("required_source_claim_ids") or ()),
                        )
                        if str(claim_id)
                    })
                    bridge_claim["source_field_ids"] = sorted({
                        str(field_id)
                        for dependency in dependencies
                        for field_id in dependency.get("required_source_field_ids") or ()
                        if str(field_id)
                    })
                bridge_claims = [bridge_claim]
            elif prior_claim is not None:
                bridge_claims = [dict(prior_claim)]
                supported_id = str((prior_claim.get("topic_ids") or [""])[0])
                bridge_claim_id = str(prior_claim.get("claim_id") or "")
            elif ":reduce:" in node_id:
                bridge_claims = []
                supported_id = ""
                bridge_claim_id = ""
            else:
                raise AssertionError("local final cross fixture has no supported topic input")
            return _provider_response(
                {
                    "comparisons": [],
                    "bridge_claims": bridge_claims,
                    "topic_dispositions": [
                        {
                            "topic_id": topic_id,
                            "status": "integrated",
                            "synthesis_claim_ids": [bridge_claim_id],
                        } if topic_id == supported_id else {
                            "topic_id": topic_id,
                            "status": "unresolved",
                            "reason": "The local fixture has no evidence-backed bridge claim for this topic.",
                        }
                        for topic_id in sorted(topic_ids)
                    ],
                    "processed_topic_ids": sorted(topic_ids),
                    "processed_fragment_ids": sorted(fragment_ids),
                    "processed_result_ids": sorted(result_ids),
                    "processed_relation_ids": sorted(relation_ids),
                    "unresolved_questions": [],
                }
            )
        cross_output = request.get("cross_group_comparison") or {}
        bridge_claims = cross_output.get("bridge_claims") or () if isinstance(cross_output, Mapping) else ()
        if not bridge_claims:
            raise AssertionError("local global fixture has no validated cross claim")
        bridge_claim = bridge_claims[0]
        return _provider_response(
            {
                "synthesis_claims": [{
                    "claim_id": "synthesis:global_synthesis:fixture-supported-topic",
                    "topic_ids": list(bridge_claim.get("topic_ids") or ()),
                    "fragment_id": str(bridge_claim.get("fragment_id") or ""),
                    "paper_key": str(bridge_claim.get("paper_key") or ""),
                    "text": "The shared synthesis retains the evidence-backed topic condition.",
                    "source_claim_ids": [str(bridge_claim.get("claim_id") or "")],
                    "evidence_ids": list(bridge_claim.get("evidence_ids") or ()),
                }],
                "organizing_principles": ["Group the supported topic by mechanism and retain unresolved topics."],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "unresolved_questions": [],
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
        selected = sorted(candidate_ids)[0] if candidate_ids else ""
        content: dict[str, Any] = {
            "selected_candidate_id": selected,
            "accepted_recommendations": [],
            "rejected_recommendations": [],
        }
        contract = request.get("section_coordination_contract") or {}
        if selected in (contract.get("required_if_selected_candidate_sharded") or ()):
            candidate = (request.get("candidate_contents") or {}).get(selected) or {}
            content["section_coordination"] = {
                "candidate_id": selected,
                "merge_groups": [],
                "section_order": [
                    str(section.get("section_id") or "")
                    for section in candidate.get("sections") or ()
                    if isinstance(section, Mapping)
                ],
            }
        return _provider_response(content)

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
    parser["Runtime"]["transport_retries"] = "0"
    for route_section in set(parser["OutlineModels"].values()):
        if parser.has_section(route_section):
            parser[route_section]["transport_retries"] = "0"
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
    capsys: Any,
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
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, Mapping):
            node_id = str(envelope.get("node_id") or "")
            request = envelope.get("request")
            if isinstance(request, Mapping) and (
                node_id.startswith((
                    "topic_synthesis_provider",
                    "cross_group_comparison_provider",
                    "global_synthesis_provider",
                ))
                or node_id == "relation_adjudication"
                or node_id.endswith(("_provider_generation", "_critique"))
                or node_id == "arbitration"
            ):
                return _outline_provider_response(node_id, request)
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

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
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
    topic_synthesis_record = first_registry.get("outline-v3:topic_synthesis")
    assert topic_synthesis_record is not None
    topic_synthesis_envelope = json.loads(
        Path(topic_synthesis_record.path).read_text(encoding="utf-8")
    )
    topic_synthesis_payload = topic_synthesis_envelope["payload"]
    assert topic_synthesis_payload["execution_mode"] == "provider_synthesis"
    assert topic_synthesis_payload["provider_request_plan_identity_hash"]
    assert topic_synthesis_payload["topics"]

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

    capsys.readouterr()  # discard expected OCR output from the prior direct runner setup
    cli_exit = reviewctl.main([
        "--repo-root", str(Path(__file__).resolve().parents[1]),
        "resume", "--workspace", first.workspace_path,
    ])
    cli_output = capsys.readouterr()
    completed = json.loads(cli_output.out)
    assert cli_exit == (0 if expected_completion == "complete" else 1), cli_output
    assert completed["job_status"] == "completed", completed
    assert completed["completion_status"] == expected_completion, completed
    assert completed["canonical_ready"] is (expected_completion == "complete"), completed
    assert tuple(completed["completed_stages"]) == (
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

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
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

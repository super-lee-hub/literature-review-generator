from __future__ import annotations

import configparser
import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import fitz  # type: ignore

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderBudgetController,
    bind_acceptance_execution_context,
    canonical_provider_request_payload,
    hash_json,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.review_generation_service import ReviewGenerationService
from services.settings import ApplicationSettings
from validation.execution_service import ValidationExecutionService
from tests.test_current_runtime_full_e2e import (
    _outline_provider_response,
    _reader_summary,
)


def _write_source_pdf(path: Path, *, title: str, finding: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        f"Title: {title}\n"
        "Methodology: A controlled study of 24 participants.\n"
        f"Results: {finding}\n"
        "Conclusion: Findings apply only to the observed sample.",
    )
    document.save(path)
    document.close()


def _fixture_reader_summary(paper_key: str, title: str, finding: str) -> dict[str, Any]:
    """Return a compact source-grounded Stage 1 result for the loopback fixture."""

    summary = _reader_summary(paper_key, title, finding)
    core = summary.get("core_analysis")
    if isinstance(core, dict):
        core.update(
            {
                "summary": f"{title}: {finding}",
                "key_points": [finding],
                "methodology": "Controlled sample (n=24).",
                "findings": finding,
                "conclusions": finding,
                "relevance": None,
                "limitations": "Findings apply only to this sample.",
                "research_gap": None,
                "theoretical_framework": None,
                "future_research_directions": [],
            }
        )
    summary["specialized_details"] = {"empirical": None, "review": None, "conceptual": None}
    return summary


class _LocalModelFixture(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]]
    reader_calls: int
    review_writer_calls: int
    validator_calls: int
    validator_observations: list[dict[str, bool]]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            model = str(request.get("model") or "")
            messages = request.get("messages") or []
            user_text = "\n".join(
                str(message.get("content") or "")
                for message in messages
                if isinstance(message, Mapping) and message.get("role") == "user"
            )
            self.server.calls.append({  # type: ignore[attr-defined]
                "path": self.path,
                "model": model,
                "user_text": user_text,
            })

            if model == "reader-local":
                source_rows = (
                    ("study-a", "Study A", "Treatment improved the measured outcome by four points."),
                    ("study-b", "Study B", "Treatment improved the measured outcome by two points."),
                    ("study-c", "Study C", "Treatment improved the measured outcome by one point."),
                )
                source_key, source_title, source_finding = source_rows[
                    min(self.server.reader_calls, len(source_rows) - 1)  # type: ignore[attr-defined]
                ]
                self.server.reader_calls += 1  # type: ignore[attr-defined]
                content: Any = _fixture_reader_summary(
                    source_key,
                    source_title,
                    source_finding,
                )
            elif model in {"outline-local", "relation-local", "writer-local"}:
                try:
                    envelope = json.loads(user_text)
                except json.JSONDecodeError:
                    envelope = None
                node_id = str(envelope.get("node_id") or "") if isinstance(envelope, Mapping) else ""
                request_payload = envelope.get("request") if isinstance(envelope, Mapping) else None
                is_outline_request = isinstance(request_payload, Mapping) and (
                    node_id.startswith((
                        "topic_synthesis_provider",
                        "cross_group_comparison_provider",
                        "global_synthesis_provider",
                    ))
                    or node_id == "relation_adjudication"
                    or node_id.endswith(("_provider_generation", "_critique"))
                    or node_id in {"arbitration", "structure_critique", "coverage_critique", "evidence_critique"}
                )
                if is_outline_request:
                    content = _outline_provider_response(
                        node_id,
                        dict(request_payload),
                    )["content"]
                elif model == "writer-local":
                    references = re.findall(r"R\d{3,}", user_text)
                    reference = references[0] if references else "R001"
                    self.server.review_writer_calls += 1  # type: ignore[attr-defined]
                    claim = (
                        "Study B found that treatment improved the measured outcome by two points"
                        if self.server.review_writer_calls == 1  # type: ignore[attr-defined]
                        else (
                            "In the controlled sample, treatment improved the measured outcome "
                            "by four points" if self.server.review_writer_calls == 2  # type: ignore[attr-defined]
                            else "In the controlled sample, treatment improved the measured outcome by one point"
                        )
                    )
                    content = {
                        "blocks": [
                            {
                                "text": f"{claim} [[cite_ref:{reference}]]."
                            }
                        ]
                    }
                else:
                    raise ValueError(f"outline fixture request is not a node envelope for {model}")
            elif model == "validator-local":
                self.server.validator_calls += 1  # type: ignore[attr-defined]
                claim_about_study_b = "Study B found" in user_text
                cites_study_a = "studya_unknown_author" in user_text
                cites_study_b = "studyb_unknown_author" in user_text
                is_defective = claim_about_study_b and cites_study_a
                self.server.validator_observations.append(  # type: ignore[attr-defined]
                    {
                        "claim_about_study_b": claim_about_study_b,
                        "cites_study_a": cites_study_a,
                        "cites_study_b": cites_study_b,
                    }
                )
                content = {
                    "status": "wrong_source" if is_defective else "supported",
                    "confidence": 0.99,
                    "repair_scope": "citation_mapping" if is_defective else "none",
                    "disposition": "manual_review" if is_defective else "keep_as_is",
                    "low_confidence": False,
                    "reasoning": (
                        "The cited source is Study A, while the two-point finding belongs to Study B."
                        if is_defective
                        else "The corrected citation and bounded statement match the selected source."
                    ),
                    "repair_hint": (
                        "Use Study B's citation for the two-point finding."
                        if is_defective
                        else ""
                    ),
                    "summary_paper_ids": [],
                    "manual_review_reason": "" if not is_defective else "Claim exceeds source evidence.",
                    "claim_type": "result",
                    "claim_type_confidence": 1.0,
                    "claim_type_rationale": "The statement is an empirical result.",
                    "adjudication_status": "wrong_source" if is_defective else "supported",
                }
            else:
                raise ValueError(f"unexpected fixture model: {model}")

            response = {
                "id": f"chatcmpl-local-{len(self.server.calls)}",  # type: ignore[attr-defined]
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 512,
                    "completion_tokens": 128,
                    "total_tokens": 640,
                },
            }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # make fixture faults visible as provider failures
            body = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _start_local_model_fixture() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalModelFixture)
    server.calls = []  # type: ignore[attr-defined]
    server.reader_calls = 0  # type: ignore[attr-defined]
    server.review_writer_calls = 0  # type: ignore[attr-defined]
    server.validator_calls = 0  # type: ignore[attr-defined]
    server.validator_observations = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _runtime_config(tmp_path: Path, api_base: str) -> Path:
    template = Path(__file__).resolve().parents[1] / "config.ini.example"
    config_path = tmp_path / "local-provider.ini"
    parser = configparser.ConfigParser()
    parser.read(template, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Paths"]["library_path"] = ""
    parser["Paths"]["zotero_report"] = ""
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
    parser["Stage1_Input"]["send_extracted_text"] = "true"
    parser["Stage1_Input"]["send_selected_visuals"] = "false"
    parser["Stage1_Input"]["send_original_pdf"] = "never"
    parser["Stage1_Visual"]["enabled"] = "false"
    for section, model in (
        ("Primary_Reader_API", "reader-local"),
        ("Backup_Reader_API", "reader-local"),
        ("Outline_API", "outline-local"),
        ("Writer_API", "writer-local"),
        ("Free_Mode_API", "relation-local"),
        ("Validator_API", "validator-local"),
    ):
        parser[section]["api_key"] = "loopback-fixture-key"
        parser[section]["model"] = model
        parser[section]["api_base"] = api_base
        parser[section]["endpoint_type"] = "chat_completions"
        parser[section]["provider_family"] = "generic"
        parser[section]["thinking"] = ""
        parser[section]["reasoning_effort"] = ""
        parser[section]["reasoning_display"] = ""
        parser[section]["transport_retries"] = "0"
        parser[section]["max_context_tokens"] = "128000"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["require_explicit_adoption"] = "true"
    parser["OutlineStability"]["mode"] = "off"
    parser["Validation"]["review_enabled"] = "true"
    parser["Validation"]["repair_policy"] = "report_only"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return config_path


def test_writer_request_binding_matches_transport_identity() -> None:
    service = object.__new__(ReviewGenerationService)
    service.settings = SimpleNamespace(
        section=lambda _name: {"max_output_tokens": "2048"}
    )
    prompt = '{"section":"bounded outcome"}'

    bound_payload = service._writer_request_payload(prompt)
    transport_payload = canonical_provider_request_payload(
        prompt=prompt,
        system_prompt=service._system_prompt(),
        user_content=None,
        response_format="json",
        max_output_tokens=service._max_output_tokens(),
        temperature=0.2,
    )

    assert hash_json(bound_payload) == hash_json(transport_payload)


def test_validator_output_artifacts_are_immutable_across_attempts(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(
        str(tmp_path / "output"),
        "validation",
        job_id="validator-output-immutability",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    api_config = {
        "api_key": "loopback-only-test-key",
        "model": "validator-local",
        "api_base": "http://127.0.0.1:1/v1",
        "endpoint_type": "chat_completions",
    }
    settings = ApplicationSettings.from_config(
        {
            "Validator_API": dict(api_config),
            "Runtime": {"validation_retry_limit": "0"},
        }
    )

    def service_for_attempt(attempt_id: str) -> ValidationExecutionService:
        service = ValidationExecutionService(
            job_id=workspace.job_id,
            attempt_id=attempt_id,
            workspace=workspace,
            artifact_registry=registry,
            settings=settings,
            summaries=[],
            review_draft_record=None,
            citation_manifest_record=None,
            paper_artifact_records=[],
            visual_artifact_records=[],
            provider_factory=None,
            cancellation_checker=None,
            logger=None,
            runtime_config={"Validator_API": dict(api_config)},
        )
        service.new_provider_runtime(
            stage_name="stage4_validate",
            route="Validator_API",
            node_id="validation_node",
            call_id="validation:primary:stable-packet",
            api_config=api_config,
            schema_hash="validator-schema-v1",
        )
        service.bind_provider_call(
            call_id="validation:primary:stable-packet",
            prompt="adjudicate current claim",
            input_payload={"claim": "stable claim"},
            api_config=api_config,
            schema_hash="validator-schema-v1",
        )
        return service

    first_service = service_for_attempt("attempt-before-repair")
    call_id = "validation:primary:stable-packet"
    first_service.bind_provider_output(
        call_id=call_id,
        content={"status": "wrong_source", "confidence": 0.99},
    )
    first_expected = first_service._expected_provider_calls[call_id]
    first_record = next(
        record
        for record in registry.list_records()
        if record.artifact_id.startswith("validation-provider-output:")
    )
    first_record_hash = first_record.content_hash
    first_path = Path(first_record.path)

    second_service = service_for_attempt("attempt-after-repair")
    second_service.bind_provider_output(
        call_id=call_id,
        content={"status": "supported", "confidence": 0.99},
    )
    second_expected = second_service._expected_provider_calls[call_id]

    assert first_expected.closure_epoch_id != second_expected.closure_epoch_id
    assert first_expected.artifact_path == str(first_path)
    current_first_record = registry.get(first_record.artifact_id)
    assert current_first_record is not None
    assert first_record_hash == current_first_record.content_hash
    assert json.loads(first_path.read_text(encoding="utf-8"))["payload"] == {
        "status": "wrong_source",
        "confidence": 0.99,
    }
    assert second_expected.artifact_path != first_expected.artifact_path
    second_record = next(
        (
            record
            for record in registry.list_records()
            if Path(record.path).resolve() == Path(second_expected.artifact_path).resolve()
        ),
        None,
    )
    assert second_record is not None and second_record.status == "ready"
    assert second_record.artifact_id != first_record.artifact_id

    source_record = publish_json_artifact(
        first_service.publication_context,
        registry,
        workspace.artifact_path("test_immutable_source.json"),
        {"source": "fixture"},
        artifact_role="test_source",
        artifact_type="test_source",
        artifact_version="v1",
        producer="tests.test_pr25_repair_v2",
        artifact_id="test:immutable-source",
    )
    dependency = ArtifactDependencyRefV2.from_record(source_record)
    reuse_payload = {"reuse": "fixture"}
    reuse_record = publish_json_artifact(
        first_service.publication_context,
        registry,
        workspace.artifact_path("test_immutable_reuse.json"),
        reuse_payload,
        artifact_role="test_reuse",
        artifact_type="test_reuse",
        artifact_version="v1",
        producer="tests.test_pr25_repair_v2",
        artifact_id="test:immutable-reuse",
        depends_on=[dependency],
    )
    assert first_service._existing_immutable_json_record(
        artifact_id=reuse_record.artifact_id,
        payload=reuse_payload,
        dependencies=[dependency],
    ) == reuse_record
    Path(source_record.path).write_text('{"source":"changed"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable validation reuse record is untrusted"):
        first_service._existing_immutable_json_record(
            artifact_id=reuse_record.artifact_id,
            payload=reuse_payload,
            dependencies=[dependency],
        )


def test_current_text_finding_can_be_explicitly_repaired_and_exported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise a real local HTTP validation finding through repair and export.

    The model server is the only fixture boundary. Runner, adoption, current
    validation, repair planning/application/promotion, Registry, resume,
    CurrentArtifactSet, DOCX generation, and export remain production paths.
    """

    server, thread, api_base = _start_local_model_fixture()
    try:
        pdf_dir = tmp_path / "papers"
        pdf_dir.mkdir()
        source_rows = (
            ("study-a", "Study A", "Treatment improved the measured outcome by four points."),
            ("study-b", "Study B", "Treatment improved the measured outcome by two points."),
            ("study-c", "Study C", "Treatment improved the measured outcome by one point."),
        )
        for key, title, finding in source_rows:
            _write_source_pdf(pdf_dir / f"{key}.pdf", title=title, finding=finding)
        spec = RuntimeJobSpec(
            project_name="pr25-repair-v2",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id="pr25-repair-v2-job",
            config=str(_runtime_config(tmp_path, api_base)),
            action="run_all",
            queue_file=str(tmp_path / "queue.json"),
        )

        first = AgentRuntimeRunner(spec).run()
        assert first.job_status == "completed", first
        assert first.job_disposition == "needs_review", first
        assert first.completed_stages == ("source_intake", "analyze", "outline"), first

        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        inspection = control.inspect(workspace=first.workspace_path)
        final_outline = next(
            item
            for item in inspection["artifacts"]
            if item["artifact_id"] == "outline-v3:final_outline"
        )
        adoption = control.adopt(
            workspace=first.workspace_path,
            artifact_id="outline-v3:final_outline",
            actor="tests.pr25_repair_v2",
            reason="approve the local fixture outline before review generation",
            expected_hash=str(final_outline["content_hash"]),
        )
        assert adoption["status"] == "succeeded", adoption

        first_resume = control.resume(workspace=first.workspace_path)
        assert first_resume["job_status"] == "completed", first_resume
        assert first_resume["completion_status"] == "blocked", first_resume
        assert first_resume["completed_stages"] == (
            "source_intake",
            "analyze",
            "outline",
            "review",
            "validate",
        ), first_resume
        status = control.validation_status(workspace=first.workspace_path)
        assert status["closure"]["semantic"]["claim_verdict_counts"]["wrong_source"] == 1, status

        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        initial_set = registry.resolve_current_artifact_set()
        assert initial_set is not None
        initial_draft = registry.get(initial_set.review_draft_artifact_id)
        initial_manifest = registry.get(initial_set.citation_manifest_artifact_id)
        assert initial_draft is not None and initial_manifest is not None
        draft_payload = json.loads(Path(initial_draft.path).read_text(encoding="utf-8"))
        block = next(
            block
            for section in draft_payload["content"]["sections"]
            for block in section["blocks"]
            if "Study B found" in str(block.get("text") or "")
        )
        original_text = str(block["text"])
        block_id = str(block["block_id"])
        expected_anchor_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        assert "Study B found" in original_text
        assert server.reader_calls == 3  # type: ignore[attr-defined]
        assert server.validator_calls == 3  # type: ignore[attr-defined]
        assert any(
            item["claim_about_study_b"] and item["cites_study_a"]
            for item in server.validator_observations  # type: ignore[attr-defined]
        )

        manifest_payload = json.loads(Path(initial_manifest.path).read_text(encoding="utf-8"))
        occurrence = next(
            item
            for item in manifest_payload["occurrences"]
            if str(item.get("block_id") or "") == block_id
        )
        catalog_record = registry.get("citation_ref_catalog")
        assert catalog_record is not None
        catalog_payload = json.loads(Path(catalog_record.path).read_text(encoding="utf-8"))
        target_source = next(
            item
            for item in catalog_payload["entries"]
            if str(item.get("canonical_paper_key") or "") == "studyb_unknown_author"
            and item.get("status") == "active"
        )

        report = control.repair_plan(workspace=first.workspace_path)
        assert report["status"] == "available", report
        plan_id = str(report["plan_id"])
        approved_correction = {
            "block_id": block_id,
            "expected_anchor_hash": expected_anchor_hash,
            "replacement_text": (
                f"Study B found that treatment improved the measured outcome by two points "
                f"[[cite_ref:{target_source['ref_id']}]]."
            ),
            "source_evidence_ids": [str(target_source["canonical_paper_key"])],
            "citation_mapping": {
                "occurrence_id": str(occurrence["occurrence_id"]),
                "expected_ref_id": str(occurrence["ref_id"]),
                "expected_paper_id": str(occurrence["paper_id"]),
                "replacement_ref_id": str(target_source["ref_id"]),
                "replacement_paper_id": str(target_source["canonical_paper_key"]),
            },
        }

        stale_anchor_result = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal={**approved_correction, "expected_anchor_hash": "0" * 64},
            actor="tests.pr25_repair_v2.reviewer",
            reason="reject a stale correction target before any mutation",
        )
        assert stale_anchor_result["status"] == "blocked", stale_anchor_result
        assert stale_anchor_result["mutation_performed"] is False
        registry.reload()
        unchanged_set = registry.resolve_current_artifact_set()
        assert unchanged_set is not None
        assert unchanged_set.review_draft_artifact_hash == initial_draft.content_hash
        assert unchanged_set.citation_manifest_artifact_hash == initial_manifest.content_hash

        unchanged_revision = registry.revision
        stale_source_result = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal={
                **approved_correction,
                "citation_mapping": {
                    **approved_correction["citation_mapping"],
                    "replacement_ref_id": occurrence["ref_id"],
                },
            },
            actor="tests.pr25_repair_v2.reviewer",
            reason="reject a citation key that does not resolve to the approved source paper",
        )
        assert stale_source_result["status"] == "blocked", stale_source_result
        assert stale_source_result["mutation_performed"] is False
        registry.reload()
        assert registry.revision == unchanged_revision
        unchanged_set = registry.resolve_current_artifact_set()
        assert unchanged_set is not None
        assert unchanged_set.review_draft_artifact_hash == initial_draft.content_hash
        assert unchanged_set.citation_manifest_artifact_hash == initial_manifest.content_hash

        applied = control.repair_apply(
            workspace=first.workspace_path,
            plan_id=plan_id,
            manual_proposal=approved_correction,
            actor="tests.pr25_repair_v2.reviewer",
            reason="replace the contradicted universal claim with the bounded result shown in the source PDF",
        )
        assert applied["status"] == "quarantined", applied
        assert applied["mutation_performed"] is True
        assert applied["canonical_replacement"] is False
        registry.reload()
        manual_plan_record = registry.get(str(applied["manual_plan_artifact_id"]))
        assert manual_plan_record is not None and manual_plan_record.status == "ready"
        manual_plan_payload = json.loads(Path(manual_plan_record.path).read_text(encoding="utf-8"))
        approval = manual_plan_payload["manual_approval"]
        assert approval["actor"] == "tests.pr25_repair_v2.reviewer"
        initial_validation_record = registry.get(initial_set.validation_run_result_artifact_id)
        assert initial_validation_record is not None
        assert approval["canonical_input_hashes"] == {
            "review_draft": initial_draft.content_hash,
            "citation_manifest": initial_manifest.content_hash,
            "validation": initial_validation_record.content_hash,
        }
        assert approval["citation_mapping"]["occurrence_id"] == occurrence["occurrence_id"]
        assert approval["citation_mapping"]["replacement_ref_id"] == target_source["ref_id"]
        transaction_id = str(applied["transaction_id"])
        applied_ids = set(applied["applied_artifact_ids"])
        derived_draft = next(
            registry.get(artifact_id)
            for artifact_id in applied_ids
            if registry.get(artifact_id) is not None
            and registry.get(artifact_id).artifact_type == "review_draft_repaired"
        )
        derived_manifest = next(
            registry.get(artifact_id)
            for artifact_id in applied_ids
            if registry.get(artifact_id) is not None
            and registry.get(artifact_id).artifact_type == "citation_manifest_repaired"
        )
        assert derived_draft.content_hash != initial_draft.content_hash
        assert derived_manifest.content_hash != initial_manifest.content_hash
        assert file_sha256(derived_draft.path) == derived_draft.content_hash
        assert file_sha256(derived_manifest.path) == derived_manifest.content_hash
        repaired = json.loads(Path(derived_draft.path).read_text(encoding="utf-8"))
        repaired_block = next(
            block
            for section in repaired["content"]["sections"]
            for block in section["blocks"]
            if str(block.get("block_id") or "") == block_id
        )
        assert repaired_block["text"] == approved_correction["replacement_text"]

        # This is a loopback model fixture, but the public repair command still
        # uses the same started aggregate budget and clean-SHA admission as a
        # real run. Only the checkout readback is replaced with a fixed fixture
        # identity because this regression intentionally runs on uncommitted code.
        aggregate_budget = ProviderAggregateBudgetV1(
            max_provider_calls_total=8,
            max_output_tokens_total=32_768,
            max_retry_attempts_total=8,
            max_wall_seconds=300,
        )
        aggregate_context = AcceptanceExecutionContextV1(
            acceptance_run_id="local-repair-export-fixture",
            final_executable_sha="a" * 40,
            absolute_deadline_epoch=(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).timestamp(),
            provider_budget=aggregate_budget,
            provider_budget_state_path=str(tmp_path / "repair-budget-state.json"),
            evidence_root=str(tmp_path / "repair-evidence"),
            process_event_log=str(tmp_path / "repair-process-events.jsonl"),
            scenario_state_path=str(tmp_path / "repair-scenario-state.json"),
            owner_authorized=True,
            provider_budget_state_started=False,
        )
        aggregate_controller = ProviderBudgetController(aggregate_budget)
        aggregate_controller.bind_state_path(
            aggregate_context.provider_budget_state_path,
            acceptance_run_id=aggregate_context.acceptance_run_id,
            state_started=False,
        )
        aggregate_context = replace(
            aggregate_context, provider_budget_state_started=True,
        )
        with monkeypatch.context() as isolated:
            isolated.setattr(
                "runtime.control_plane.read_checkout_sha",
                lambda _root, *, require_clean: "a" * 40,
            )
            with bind_acceptance_execution_context(
                aggregate_context, aggregate_controller,
            ):
                promoted = control.repair_promote(
                    workspace=first.workspace_path,
                    transaction_id=transaction_id,
                    actor="tests.pr25_repair_v2.reviewer",
                    reason="the production validator now confirms the corrected source-grounded claim",
                )
        assert promoted["status"] == "promoted", promoted
        assert promoted["revalidation_execution_status"] == "succeeded", promoted
        assert promoted["revalidation_disposition"] == "clean", promoted
        assert server.validator_calls == 5  # type: ignore[attr-defined]
        assert any(
            item["claim_about_study_b"] and item["cites_study_b"]
            for item in server.validator_observations  # type: ignore[attr-defined]
        )

        registry.reload()
        promoted_set = registry.resolve_current_artifact_set()
        assert promoted_set is not None
        assert promoted_set.review_draft_artifact_id.startswith("review_draft:v3:repair:")
        assert promoted_set.citation_manifest_artifact_id.startswith("citation_manifest:v3:repair:")
        assert promoted_set.review_docx_artifact_id.startswith("review_docx:v1:repair:")
        promoted_draft = registry.get(promoted_set.review_draft_artifact_id)
        promoted_manifest = registry.get(promoted_set.citation_manifest_artifact_id)
        promoted_docx = registry.get(promoted_set.review_docx_artifact_id)
        assert promoted_draft is not None and promoted_manifest is not None and promoted_docx is not None
        promoted_payload = json.loads(Path(promoted_draft.path).read_text(encoding="utf-8"))
        assert approved_correction["replacement_text"] in json.dumps(promoted_payload, ensure_ascii=False)
        assert "[[cite_ref:R001]]" not in json.dumps(promoted_payload, ensure_ascii=False)
        from docx import Document

        promoted_docx_text = "\n".join(
            paragraph.text for paragraph in Document(promoted_docx.path).paragraphs
        )
        assert "Study B found" in promoted_docx_text
        assert "improved the measured outcome by two points" in promoted_docx_text
        export_before_resume = control.export(workspace=first.workspace_path)
        assert export_before_resume["status"] == "untrusted", export_before_resume
        assert export_before_resume["bundle_path"] == ""
        assert "runtime_completion_not_canonical" in export_before_resume["issues"]

        resumed = control.resume(workspace=first.workspace_path)
        assert resumed["job_status"] == "completed", resumed
        assert resumed["completion_status"] == "complete", resumed
        assert resumed["canonical_ready"] is True, resumed
        assert server.validator_calls == 5  # type: ignore[attr-defined]
        _resumed_workspace, current_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        current_set = current_registry.resolve_current_artifact_set()
        assert current_set is not None
        assert current_set.validation_status == "clean"
        assert current_set.review_draft_artifact_id == promoted_set.review_draft_artifact_id
        assert current_set.review_draft_artifact_hash == promoted_set.review_draft_artifact_hash
        assert current_set.citation_manifest_artifact_id == promoted_set.citation_manifest_artifact_id
        assert current_set.citation_manifest_artifact_hash == promoted_set.citation_manifest_artifact_hash
        assert current_set.review_docx_artifact_id == promoted_set.review_docx_artifact_id
        assert current_set.review_docx_artifact_hash == promoted_set.review_docx_artifact_hash
        current_draft = current_registry.get(current_set.review_draft_artifact_id)
        current_manifest = current_registry.get(current_set.citation_manifest_artifact_id)
        current_docx = current_registry.get(current_set.review_docx_artifact_id)
        assert current_draft is not None and current_manifest is not None and current_docx is not None
        current_payload = json.loads(Path(current_draft.path).read_text(encoding="utf-8"))
        assert approved_correction["replacement_text"] in json.dumps(current_payload, ensure_ascii=False)
        assert "Study B found" in json.dumps(current_payload, ensure_ascii=False)
        assert f"[[cite_ref:{target_source['ref_id']}]]" in json.dumps(current_payload, ensure_ascii=False)
        current_manifest_payload = json.loads(Path(current_manifest.path).read_text(encoding="utf-8"))
        repaired_occurrence = next(
            item
            for item in current_manifest_payload["occurrences"]
            if str(item.get("occurrence_id") or "") == str(occurrence["occurrence_id"])
        )
        assert repaired_occurrence["ref_id"] == target_source["ref_id"]
        assert repaired_occurrence["paper_id"] == target_source["canonical_paper_key"]
        current_docx_document = Document(current_docx.path)
        docx_paragraphs = [paragraph.text for paragraph in current_docx_document.paragraphs]
        docx_paragraphs.extend(
            cell.text
            for table in current_docx_document.tables
            for row in table.rows
            for cell in row.cells
        )
        docx_text = "\n".join(docx_paragraphs)
        assert "improved the measured outcome by two points" in docx_text
        assert "Study B found" in docx_text
        corrected_claim_paragraph = next(
            paragraph for paragraph in docx_paragraphs if "Study B found" in paragraph
        )
        assert "(Author, 2025b)" in corrected_claim_paragraph
        assert "Author, E. (2025b). study-b." in docx_text
        assert "https://doi.org/10.1000/study-b" in docx_text
        assert "[[cite_ref:" not in docx_text

        export = control.export(workspace=first.workspace_path)
        assert export["status"] == "canonical_verified", export
        assert Path(export["bundle_path"]).is_file()
        assert export["artifact_id"].startswith("export_bundle:")

        original_draft_bytes = Path(current_draft.path).read_bytes()
        try:
            Path(current_draft.path).write_bytes(original_draft_bytes + b"\n")
            tampered_export = control.export(workspace=first.workspace_path)
            assert tampered_export["status"] == "untrusted", tampered_export
            assert tampered_export["bundle_path"] == ""
        finally:
            Path(current_draft.path).write_bytes(original_draft_bytes)
        assert file_sha256(current_draft.path) == current_draft.content_hash
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

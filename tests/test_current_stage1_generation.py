from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import shutil
from typing import Any, Mapping

import fitz  # type: ignore
import pytest

import preprocess.visual_artifacts as visual_artifacts
from preprocess.visual_artifacts import Stage1VisualArtifactBuilder
from runtime.provider_runtime import ProviderRuntimeLedger
from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.stage_contracts import build_source_bundle
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace
from services.settings import ApplicationSettings
from services.stage1_analysis_service import Stage1AnalysisService
from validation.closure import resolve_current_stage_closure_map
from summary_schema import normalize_ai_summary


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Title: Evidence-bound study\n"
        "Methodology: A controlled experiment with N=120 observations.\n"
        "Results: The treatment improved the outcome by 15 percent (p < 0.01).\n"
        "Conclusion: The result supports the proposed mechanism under the tested context.",
    )
    document.save(path)
    document.close()


def _write_visual_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for page_number in (1, 2):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Figure {page_number}. Treatment effect\n"
            f"Results: group {page_number} improved by {10 + page_number} percent.",
        )
        pixmap = fitz.Pixmap(
            fitz.csRGB,
            fitz.IRect(0, 0, 240, 160),
            False,
        )
        pixmap.clear_with(70 + page_number * 60)
        page.insert_image(fitz.Rect(72, 110, 280, 260), pixmap=pixmap)
    document.save(path)
    document.close()


def _visual_config_overrides() -> dict[str, Mapping[str, Any]]:
    return {
        "Stage1_Visual": {"enabled": "true"},
        "Stage1_Input": {
            "send_extracted_text": "true",
            "send_selected_visuals": "true",
            "send_original_pdf": "never",
        },
        "Multimodal": {"enabled": "true"},
        "Primary_Reader_API": {
            "endpoint_type": "responses",
            "supports_image_input": "true",
        },
    }


def _visual_artifact_identities(service: Stage1AnalysisService) -> tuple[dict[str, Any], ...]:
    bundle_record = next(
        record
        for record in service.registry.list_records()
        if record.artifact_type == "stage1_visual_bundle"
    )
    bundle = json.loads(Path(bundle_record.path).read_text(encoding="utf-8"))
    identities = []
    for selection_rank, visual in enumerate(bundle["selected_visual_refs"], start=1):
        identities.append(
            {
                "selection_rank": selection_rank,
                "visual_id": str(visual.get("visual_id") or ""),
                "page_no": int(visual.get("page_no") or 0),
                "bbox": list(visual.get("bbox") or []),
                "artifact_type": str(visual.get("artifact_type") or ""),
                "source_type": str(visual.get("source_type") or ""),
                "content_sha256": file_sha256(str(visual["image_path"])),
            }
        )
    return tuple(identities)


def _canonical_summary() -> dict[str, Any]:
    return normalize_ai_summary(
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
                "title": "Evidence-bound study",
                "authors": ["Example Author"],
                "year": "2025",
                "journal": "Example Journal",
                "doi": "10.1000/example",
            },
            "core_analysis": {
                "summary": "The study tests a treatment with a controlled experiment and reports a measurable improvement.",
                "key_points": ["The treatment improved the outcome by 15 percent."],
                "methodology": "A controlled experiment with N=120 observations.",
                "findings": "The treatment improved the outcome by 15 percent (p < 0.01).",
                "conclusions": "The result supports the proposed mechanism under the tested context.",
                "relevance": "The result informs treatment design.",
                "limitations": "The result is bounded by the tested context.",
                "theoretical_framework": None,
                "research_gap": "Further replication is needed.",
                "future_research_directions": ["Replicate in another context."],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": ["Does the treatment improve the outcome?"],
                    "data_source_and_size": "Controlled experiment, N=120.",
                    "analysis_technique": "Group comparison with significance testing.",
                    "core_variables": {"independent": ["treatment"], "dependent": ["outcome"]},
                    "sample_characteristics_or_context": "Controlled context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )


def _service(
    tmp_path: Path,
    pdf_path: Path,
    reader: Any,
    *,
    job_id: str = "stage1-job",
    source_paper_id: str = "paper-1",
    external_registry_resolver: Any = None,
    config_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Stage1AnalysisService, Any]:
    config = {
        "Paths": {"output_path": str(tmp_path / "output")},
        "Preprocess": {"enabled": "true", "cache_dir": str(tmp_path / "cache")},
        "Stage1_Input": {
            "send_extracted_text": "true",
            "send_selected_visuals": "false",
            "send_original_pdf": "never",
        },
        "Stage1_Visual": {"enabled": "false"},
        "Primary_Reader_API": {"api_key": "reader", "model": "reader-test", "api_base": "https://reader.test/v1"},
        "Backup_Reader_API": {"api_key": "backup", "model": "backup-test", "api_base": "https://backup.test/v1"},
        "Runtime": {"node_retry_limit": "1"},
    }
    for section, values in (config_overrides or {}).items():
        config.setdefault(section, {}).update(dict(values))
    workspace = JobWorkspace.create(str(tmp_path / "output"), "current", job_id=job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="source_pdf",
        artifact_type="source_pdf",
        artifact_version="v1",
        path=pdf_path,
        producer="test",
        artifact_id=f"source_pdf:{pdf_path.name}",
    )
    settings = ApplicationSettings.from_config(config)
    service = Stage1AnalysisService(
        job_id=workspace.job_id,
        attempt_id="attempt-1",
        workspace=workspace,
        artifact_registry=registry,
        config=config,
        settings=settings,
        reader=reader,
        external_registry_resolver=external_registry_resolver,
    )
    bundle = build_source_bundle(
        source_mode="direct",
        project_name="current",
        papers=[
            {
                "title": "Evidence-bound study",
                "authors": ["Example Author"],
                "year": "2025",
                "pdf_path": str(pdf_path),
                "source_paper_id": source_paper_id,
            }
        ],
    )
    return service, bundle


def _typed_manifest_record(service: Stage1AnalysisService) -> Any:
    return next(
        record
        for record in service.registry.list_records()
        if record.artifact_type == "stage1_reusable_summary_manifest"
    )


def _visual_bundle_payload(service: Stage1AnalysisService) -> dict[str, Any]:
    bundle_record = next(
        record
        for record in service.registry.list_records()
        if record.artifact_type == "stage1_visual_bundle"
    )
    return json.loads(Path(bundle_record.path).read_text(encoding="utf-8"))


def _rewrite_registered_closure(
    service: Stage1AnalysisService,
    closure: Any,
    *,
    suffix: str,
    payload: Mapping[str, Any] | None = None,
    depends_on: list[Mapping[str, Any]] | None = None,
) -> str:
    replacement_path = closure.path
    replacement_hash = closure.content_hash
    if payload is not None:
        published = service.publication_context.publish_json(
            service.workspace.artifact_path(
                f"stage1/{suffix}_provider_receipt_closure.json"
            ),
            payload,
        )
        replacement_path = published.final_path
        replacement_hash = published.content_hash

    registry_payload = json.loads(
        Path(service.registry.registry_path).read_text(encoding="utf-8")
    )
    closure_entry = next(
        item
        for item in registry_payload["artifacts"]
        if item["artifact_id"] == closure.artifact_id
    )
    closure_entry["path"] = replacement_path
    closure_entry["content_hash"] = replacement_hash
    if depends_on is not None:
        closure_entry["depends_on"] = depends_on
    Path(service.registry.registry_path).write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    service.registry.reload()
    return replacement_path


def test_current_stage1_generates_canonical_summary_and_receipt(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    calls: list[Mapping[str, Any]] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    result = service.run(bundle)

    assert result.generated_count == 1
    assert result.reused_count == 0
    assert len(calls) == 1
    summary = result.summaries[0]
    assert summary["status"] == "success"
    assert summary["ai_summary"]["schema_version"] == "summary_v2_lite"
    assert summary["stage1_input"]["input_mode"] == "text_only"
    assert result.receipt_ids
    assert result.receipt_ledger_path
    assert result.receipt_ledger_path == service.registry.get("stage1_provider_receipts").path  # type: ignore[union-attr]
    ledger = ProviderRuntimeLedger(result.receipt_ledger_path)
    assert len(ledger.list_receipts()) == 1
    closure = service.finalize_provider_receipt_closure()
    closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))
    assert closure_payload["expected_provider_transport_count"] == 1
    assert closure_payload["payload"]["complete"] is True
    assert any(item.artifact_type == "evidence_manifest" for item in service.registry.list_records())


def test_current_stage1_malformed_existing_closure_is_rebuilt(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    service.run(bundle)
    original = service.finalize_provider_receipt_closure()
    malformed_payload = json.loads(Path(original.path).read_text(encoding="utf-8"))
    malformed_payload["payload"]["expected_call_ids"] = 1
    malformed = service.publication_context.publish_json(
        service.workspace.artifact_path("stage1/malformed_provider_receipt_closure.json"),
        malformed_payload,
    )
    registry_payload = json.loads(
        Path(service.registry.registry_path).read_text(encoding="utf-8")
    )
    closure_entry = next(
        item
        for item in registry_payload["artifacts"]
        if item["artifact_id"] == original.artifact_id
    )
    closure_entry["path"] = malformed.final_path
    closure_entry["content_hash"] = malformed.content_hash
    Path(service.registry.registry_path).write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    service.registry.reload()

    rebuilt = service.finalize_provider_receipt_closure()
    rebuilt_payload = json.loads(Path(rebuilt.path).read_text(encoding="utf-8"))

    assert rebuilt_payload["payload"]["complete"] is True
    assert rebuilt_payload["payload"]["expected_call_ids"] == [
        item.call_id for item in service.expected_calls
    ]
    assert rebuilt.content_hash == file_sha256(rebuilt.path)


def test_current_stage1_stale_root_expected_calls_and_duplicate_ids_are_rebuilt(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    service.run(bundle)
    original = service.finalize_provider_receipt_closure()
    canonical_payload = json.loads(Path(original.path).read_text(encoding="utf-8"))

    stale_payloads: list[tuple[str, dict[str, Any]]] = []
    missing_calls = json.loads(json.dumps(canonical_payload))
    missing_calls.pop("expected_calls")
    stale_payloads.append(("missing_expected_calls", missing_calls))

    tampered_calls = json.loads(json.dumps(canonical_payload))
    tampered_calls["expected_calls"][0]["config_hash"] = "tampered-config-hash"
    stale_payloads.append(("tampered_expected_calls", tampered_calls))

    duplicate_ids = json.loads(json.dumps(canonical_payload))
    duplicate_call_id = duplicate_ids["payload"]["expected_call_ids"][0]
    duplicate_ids["payload"]["expected_call_ids"].append(duplicate_call_id)
    duplicate_ids["payload"]["observed_call_ids"].append(duplicate_call_id)
    stale_payloads.append(("duplicate_call_ids", duplicate_ids))

    for suffix, stale_payload in stale_payloads:
        current = service.registry.get(original.artifact_id)
        assert current is not None
        stale_path = _rewrite_registered_closure(
            service,
            current,
            suffix=suffix,
            payload=stale_payload,
        )

        rebuilt = service.finalize_provider_receipt_closure()
        rebuilt_payload = json.loads(Path(rebuilt.path).read_text(encoding="utf-8"))

        assert rebuilt.path != stale_path
        assert rebuilt_payload["expected_calls"] == canonical_payload["expected_calls"]
        assert rebuilt_payload["payload"]["expected_call_ids"] == canonical_payload["payload"]["expected_call_ids"]
        assert rebuilt_payload["payload"]["observed_call_ids"] == canonical_payload["payload"]["observed_call_ids"]


def test_current_stage1_missing_or_extra_closure_dependency_is_rebuilt(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**_kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    service.run(bundle)
    original = service.finalize_provider_receipt_closure()
    canonical_dependencies = [item.to_dict() for item in original.depends_on]
    source_pdf = next(
        record
        for record in service.registry.list_records()
        if record.artifact_type == "source_pdf"
    )
    extra_dependency = ArtifactDependencyRefV2.from_record(source_pdf).to_dict()
    assert extra_dependency not in canonical_dependencies

    stale_dependency_sets = (
        ("missing_dependency", canonical_dependencies[1:]),
        ("extra_dependency", [*canonical_dependencies, extra_dependency]),
    )
    for suffix, stale_dependencies in stale_dependency_sets:
        current = service.registry.get(original.artifact_id)
        assert current is not None
        _rewrite_registered_closure(
            service,
            current,
            suffix=suffix,
            depends_on=stale_dependencies,
        )

        rebuilt = service.finalize_provider_receipt_closure()

        assert [item.to_dict() for item in rebuilt.depends_on] == canonical_dependencies


def test_current_stage1_resume_reuses_matching_summary_without_provider_call(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    first = service.run(bundle)
    second = service.run(bundle, existing_summaries=first.summaries)

    assert len(calls) == 1
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert second.receipt_ids == ()
    assert second.receipt_ledger_path == ""
    assert second.summaries[0]["provider"]["receipt_ledger_path"] == ""
    assert second.reuse_evidence_ids
    reuse_record = service.registry.get(second.reuse_evidence_ids[0])
    assert reuse_record is not None
    assert reuse_record.metadata["transport_count"] == 0
    assert reuse_record.depends_on
    assert second.summaries[0]["ai_summary"] == first.summaries[0]["ai_summary"]


def test_current_stage1_different_pdf_bytes_same_extracted_text_generates_again(
    tmp_path: Path,
) -> None:
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    _write_pdf(first_pdf)
    shutil.copyfile(first_pdf, second_pdf)
    # Bytes after the valid PDF trailer do not alter extraction, but must remain
    # part of the source identity used by exact Stage 1 reuse.
    with second_pdf.open("ab") as handle:
        handle.write(b"\noperator-byte-variation\n")

    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent, parent_bundle = _service(
        tmp_path / "parent-bytes",
        first_pdf,
        reader,
        source_paper_id="same-paper",
    )
    first = parent.run(parent_bundle)
    child, child_bundle = _service(
        tmp_path / "child-bytes",
        second_pdf,
        reader,
        source_paper_id="same-paper",
    )
    second = child.run(child_bundle, existing_summaries=first.summaries)
    first_binding = first.summaries[0]["stage1_reuse"]["binding"]
    second_binding = second.summaries[0]["stage1_reuse"]["binding"]
    closure = child.finalize_provider_receipt_closure()
    closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))

    assert len(calls) == 2
    assert second.generated_count == 1
    assert second.reused_count == 0
    assert first_binding["source_pdf_content_sha256"] == file_sha256(first_pdf)
    assert second_binding["source_pdf_content_sha256"] == file_sha256(second_pdf)
    assert first_binding["source_pdf_content_sha256"] != second_binding["source_pdf_content_sha256"]
    assert first_binding["stage1_semantic_input_hash"] == second_binding["stage1_semantic_input_hash"]
    assert second.expected_provider_transport_count == 1
    assert second.actual_provider_transport_count == 1
    assert closure_payload["payload"]["complete"] is True


def test_current_stage1_binding_hashes_include_effective_policies_without_paths_or_secrets(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "policy.pdf"
    _write_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    service, bundle = _service(tmp_path, pdf_path, reader)
    result = service.run(bundle)
    binding = result.summaries[0]["stage1_reuse"]["binding"]

    assert binding["preprocess_contract_hash"]
    assert binding["input_builder_policy_hash"]
    # Policy fingerprints are hashes, never raw config values or machine paths.
    assert str(tmp_path) not in json.dumps(binding, ensure_ascii=False)
    assert "api_key" not in json.dumps(binding, ensure_ascii=False)


def test_current_stage1_production_invalidation_matrix_replays_reader_and_closes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_path = tmp_path / "matrix.pdf"
    _write_pdf(pdf_path)
    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent, parent_bundle = _service(tmp_path / "matrix-parent", pdf_path, reader)
    baseline = parent.run(parent_bundle)
    baseline_binding = baseline.summaries[0]["stage1_reuse"]["binding"]

    variations = (
        "prompt_template",
        "stage1_input_policy",
        "provider_model",
        "summary_schema",
        "visual_manifest",
    )
    for variation in variations:
        with monkeypatch.context() as patch:
            overrides: dict[str, Mapping[str, Any]] = {}
            if variation == "stage1_input_policy":
                overrides["Stage1_Input"] = {"send_extracted_text": "false"}
            elif variation == "provider_model":
                overrides["Primary_Reader_API"] = {"model": "reader-matrix-v2"}
            elif variation == "prompt_template":
                patch.setattr(
                    Stage1AnalysisService,
                    "_prompt_template",
                    staticmethod(lambda: "matrix prompt template v2"),
                )
            elif variation == "summary_schema":
                patch.setattr(
                    Stage1AnalysisService,
                    "_schema_hash",
                    staticmethod(lambda: "f" * 64),
                )
            else:
                patch.setattr(
                    Stage1AnalysisService,
                    "_build_visual_bundle",
                    lambda self, item, preprocess: {
                        "selection_policy_snapshot": {"policy_name": "matrix-v2"}
                    },
                )
            child, child_bundle = _service(
                tmp_path / f"matrix-child-{variation}",
                pdf_path,
                reader,
                config_overrides=overrides,
            )
            result = child.run(child_bundle, existing_summaries=baseline.summaries)
            closure = child.finalize_provider_receipt_closure()
            payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))
            binding = result.summaries[0]["stage1_reuse"]["binding"]
            assert result.generated_count == 1, variation
            assert result.reused_count == 0, variation
            assert len(result.receipt_ids) == 1, variation
            assert result.expected_provider_transport_count == 1, variation
            assert result.actual_provider_transport_count == 1, variation
            assert payload["payload"]["complete"] is True, variation
            assert binding != baseline_binding, variation

    assert len(calls) == 1 + len(variations)


@pytest.mark.parametrize(
    ("setting_name", "changed_value"),
    (
        ("parser_mode", "hybrid"),
        ("primary_parser", "mineru_remote"),
    ),
)
def test_current_stage1_supported_preprocess_setting_change_is_stale_and_regenerates(
    tmp_path: Path,
    setting_name: str,
    changed_value: str,
) -> None:
    pdf_path = tmp_path / "supported-preprocess.pdf"
    _write_pdf(pdf_path)
    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    baseline_preprocess = {
        "parser_mode": "local",
        "primary_parser": "local",
    }
    parent, parent_bundle = _service(
        tmp_path / "parent",
        pdf_path,
        reader,
        config_overrides={"Preprocess": baseline_preprocess},
    )
    baseline = parent.run(parent_bundle)
    baseline_binding = baseline.summaries[0]["stage1_reuse"]["binding"]
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        _typed_manifest_record(parent).path
    )

    unchanged, unchanged_bundle = _service(
        tmp_path / "unchanged",
        pdf_path,
        reader,
        job_id=f"unchanged-{setting_name}",
        config_overrides={"Preprocess": baseline_preprocess},
    )
    unchanged_result = unchanged.run(
        unchanged_bundle,
        existing_summaries=imported,
    )
    assert unchanged_result.reused_count == 1
    assert unchanged_result.generated_count == 0
    assert unchanged_result.expected_provider_transport_count == 0
    assert unchanged_result.actual_provider_transport_count == 0
    assert calls == [1]

    changed_preprocess = {**baseline_preprocess, setting_name: changed_value}
    changed, changed_bundle = _service(
        tmp_path / "changed",
        pdf_path,
        reader,
        job_id=f"changed-{setting_name}",
        config_overrides={"Preprocess": changed_preprocess},
    )
    changed_result = changed.run(
        changed_bundle,
        existing_summaries=imported,
    )
    changed_closure = changed.finalize_provider_receipt_closure()
    changed_closure_payload = json.loads(
        Path(changed_closure.path).read_text(encoding="utf-8")
    )
    changed_binding = changed_result.summaries[0]["stage1_reuse"]["binding"]

    assert changed_binding["preprocess_contract_hash"] != baseline_binding["preprocess_contract_hash"]
    assert changed_result.summaries[0]["stage1_reuse"]["decision"] == "identity_match_but_stale"
    assert changed_result.reused_count == 0
    assert changed_result.generated_count == 1
    assert changed_result.expected_provider_transport_count == 1
    assert changed_result.actual_provider_transport_count == 1
    assert changed_closure_payload["payload"]["complete"] is True
    assert calls == [1, 1]


def test_current_stage1_deprecated_strategy_policy_is_ignored_for_reuse(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "deprecated-strategy-policy.pdf"
    _write_pdf(pdf_path)
    calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent, parent_bundle = _service(
        tmp_path / "parent",
        pdf_path,
        reader,
        config_overrides={
            "Preprocess": {
                "strategy_policy": "auto",
                "parser_mode": "local",
                "primary_parser": "local",
            }
        },
    )
    baseline = parent.run(parent_bundle)
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        _typed_manifest_record(parent).path
    )

    child, child_bundle = _service(
        tmp_path / "child",
        pdf_path,
        reader,
        job_id="deprecated-strategy-policy-child",
        config_overrides={
            "Preprocess": {
                "strategy_policy": "local",
                "parser_mode": "local",
                "primary_parser": "local",
            }
        },
    )
    result = child.run(child_bundle, existing_summaries=imported)

    assert result.reused_count == 1
    assert result.generated_count == 0
    assert result.expected_provider_transport_count == 0
    assert result.actual_provider_transport_count == 0
    assert result.summaries[0]["stage1_reuse"]["binding"]["preprocess_contract_hash"] == (
        baseline.summaries[0]["stage1_reuse"]["binding"]["preprocess_contract_hash"]
    )
    assert calls == [1]


def test_typed_manifest_reuses_equivalent_multimodal_evidence_after_path_move(
    tmp_path: Path,
) -> None:
    parent_pdf = tmp_path / "parent" / "visual-paper.pdf"
    _write_visual_pdf(parent_pdf)
    parent_calls: list[int] = []

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        parent_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        parent_pdf,
        parent_reader,
        job_id="visual-parent-job",
        source_paper_id=str(parent_pdf),
        config_overrides=_visual_config_overrides(),
    )
    parent_result = parent_service.run(parent_bundle)
    parent_binding = parent_result.summaries[0]["stage1_reuse"]["binding"]
    parent_visuals = _visual_artifact_identities(parent_service)
    assert parent_visuals
    manifest_record = _typed_manifest_record(parent_service)

    child_pdf = tmp_path / "moved" / "visual-paper.pdf"
    child_pdf.parent.mkdir(parents=True)
    shutil.copyfile(parent_pdf, child_pdf)
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        manifest_record.path
    )
    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    child_service, child_bundle = _service(
        tmp_path / "child-run",
        child_pdf,
        child_reader,
        job_id="visual-child-job",
        source_paper_id=str(child_pdf),
        config_overrides=_visual_config_overrides(),
    )
    child_result = child_service.run(child_bundle, existing_summaries=imported)
    child_closure = child_service.finalize_provider_receipt_closure()
    child_closure_payload = json.loads(
        Path(child_closure.path).read_text(encoding="utf-8")
    )
    child_binding = child_result.summaries[0]["stage1_reuse"]["binding"]
    child_visuals = _visual_artifact_identities(child_service)

    assert file_sha256(parent_pdf) == file_sha256(child_pdf)
    assert parent_binding["stage1_semantic_input_hash"] == child_binding["stage1_semantic_input_hash"]
    assert parent_binding["visual_input_manifest_hash"] == child_binding["visual_input_manifest_hash"]
    assert parent_visuals == child_visuals
    assert parent_binding["current_source_location"] == str(parent_pdf)
    assert child_binding["original_source_location"] == str(parent_pdf)
    assert child_binding["current_source_location"] == str(child_pdf)
    assert child_binding["location_changed"] is True
    assert child_result.summaries[0]["stage1_reuse"]["source_authority_kind"] == "typed_manifest"
    assert parent_calls == [1]
    assert child_calls == []
    assert child_result.reused_count == 1
    assert child_result.generated_count == 0
    assert child_result.expected_provider_transport_count == 0
    assert child_result.actual_provider_transport_count == 0
    assert child_closure_payload["payload"]["complete"] is True


@pytest.mark.parametrize(
    "mutation",
    ("visual_bytes", "selection_policy", "selected_visual"),
)
def test_typed_manifest_multimodal_semantic_change_regenerates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    pdf_path = tmp_path / "visual-paper.pdf"
    _write_visual_pdf(pdf_path)

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        pdf_path,
        parent_reader,
        job_id=f"visual-negative-parent-{mutation}",
        config_overrides=_visual_config_overrides(),
    )
    parent_result = parent_service.run(parent_bundle)
    parent_binding = parent_result.summaries[0]["stage1_reuse"]["binding"]
    parent_visuals = _visual_artifact_identities(parent_service)
    assert len(parent_visuals) >= 2
    assert len(
        [
            visual
            for visual in parent_visuals
            if visual["artifact_type"] == "figure_crop"
        ]
    ) >= 2
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        _typed_manifest_record(parent_service).path
    )

    if mutation == "visual_bytes":
        original_render = Stage1VisualArtifactBuilder._render_pixmap_if_safe

        def render_changed_bytes(self: Any, **kwargs: Any) -> bool:
            rendered = original_render(self, **kwargs)
            if rendered:
                image_path = Path(str(kwargs["image_path"]))
                image_path.write_bytes(image_path.read_bytes() + b"visual-content-change")
            return rendered

        monkeypatch.setattr(
            Stage1VisualArtifactBuilder,
            "_render_pixmap_if_safe",
            render_changed_bytes,
        )
    elif mutation == "selection_policy":
        changed_policy = json.loads(
            json.dumps(visual_artifacts._DEFAULT_SELECTION_POLICY)
        )
        changed_policy["policy_name"] = "stage1_visual_bundle_budgeted_v2"
        monkeypatch.setattr(
            visual_artifacts,
            "_DEFAULT_SELECTION_POLICY",
            changed_policy,
        )
    else:
        original_select = Stage1VisualArtifactBuilder._select_figure_candidates

        def select_different_visual(
            self: Any,
            page_blocks: Any,
            page_index: Any,
            policy: Mapping[str, Any],
        ) -> list[dict[str, Any]]:
            selected = original_select(self, page_blocks, page_index, policy)
            return selected[-1:]

        monkeypatch.setattr(
            Stage1VisualArtifactBuilder,
            "_select_figure_candidates",
            select_different_visual,
        )

    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    child_service, child_bundle = _service(
        tmp_path / "child-run",
        pdf_path,
        child_reader,
        job_id=f"visual-negative-child-{mutation}",
        config_overrides=_visual_config_overrides(),
    )
    child_result = child_service.run(child_bundle, existing_summaries=imported)
    child_closure = child_service.finalize_provider_receipt_closure()
    child_closure_payload = json.loads(
        Path(child_closure.path).read_text(encoding="utf-8")
    )
    child_binding = child_result.summaries[0]["stage1_reuse"]["binding"]

    assert child_binding["visual_input_manifest_hash"] != parent_binding["visual_input_manifest_hash"]
    assert child_result.reused_count == 0
    assert child_result.generated_count == 1
    assert child_calls == [1]
    assert child_result.expected_provider_transport_count == 1
    assert child_result.actual_provider_transport_count == 1
    assert child_closure_payload["payload"]["complete"] is True


def test_typed_manifest_visual_bbox_metadata_change_regenerates_without_image_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "visual-paper.pdf"
    _write_visual_pdf(pdf_path)

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        pdf_path,
        parent_reader,
        job_id="visual-bbox-parent",
        config_overrides=_visual_config_overrides(),
    )
    parent_result = parent_service.run(parent_bundle)
    parent_binding = parent_result.summaries[0]["stage1_reuse"]["binding"]
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        _typed_manifest_record(parent_service).path
    )
    parent_bundle_payload = _visual_bundle_payload(parent_service)

    original_materialize = Stage1VisualArtifactBuilder._materialize_visuals

    def materialize_with_bbox_only(self: Any, **kwargs: Any) -> list[Any]:
        visuals = original_materialize(self, **kwargs)
        target_index = next(
            index
            for index, visual in enumerate(visuals)
            if visual.artifact_type == "figure_crop"
        )
        target = visuals[target_index]
        changed_bbox = [round(float(value) + 1.0, 2) for value in target.bbox]
        return [
            replace(visual, bbox=changed_bbox) if index == target_index else visual
            for index, visual in enumerate(visuals)
        ]

    monkeypatch.setattr(
        Stage1VisualArtifactBuilder,
        "_materialize_visuals",
        materialize_with_bbox_only,
    )

    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    child_service, child_bundle = _service(
        tmp_path / "child-run",
        pdf_path,
        child_reader,
        job_id="visual-bbox-child",
        config_overrides=_visual_config_overrides(),
    )
    child_result = child_service.run(child_bundle, existing_summaries=imported)
    child_closure = child_service.finalize_provider_receipt_closure()
    child_closure_payload = json.loads(
        Path(child_closure.path).read_text(encoding="utf-8")
    )
    child_binding = child_result.summaries[0]["stage1_reuse"]["binding"]
    child_bundle_payload = _visual_bundle_payload(child_service)

    parent_refs = parent_bundle_payload["selected_visual_refs"]
    child_refs = child_bundle_payload["selected_visual_refs"]
    assert len(parent_refs) == len(child_refs)
    parent_identity = Stage1AnalysisService._build_visual_semantic_identity(
        visual_bundle=parent_bundle_payload,
        selected_visual_refs=parent_refs,
        selection_policy_snapshot=parent_bundle_payload["selection_policy_snapshot"],
    )
    child_identity = Stage1AnalysisService._build_visual_semantic_identity(
        visual_bundle=child_bundle_payload,
        selected_visual_refs=child_refs,
        selection_policy_snapshot=child_bundle_payload["selection_policy_snapshot"],
    )
    parent_visuals = parent_identity["selected_visuals"]
    child_visuals = child_identity["selected_visuals"]
    assert len(parent_visuals) == len(child_visuals)
    changed_bbox_count = 0
    for parent_visual, child_visual in zip(parent_visuals, child_visuals):
        assert {
            key: value for key, value in parent_visual.items() if key != "bbox"
        } == {
            key: value for key, value in child_visual.items() if key != "bbox"
        }
        if parent_visual["bbox"] != child_visual["bbox"]:
            changed_bbox_count += 1
    assert changed_bbox_count == 1
    assert parent_identity["selection_policy"] == child_identity["selection_policy"]
    assert parent_identity["bundle_metadata"] == child_identity["bundle_metadata"]
    assert parent_binding["visual_input_manifest_hash"] != child_binding[
        "visual_input_manifest_hash"
    ]
    assert child_result.summaries[0]["stage1_reuse"]["decision"] != "exact_summary_reuse"
    assert child_result.reused_count == 0
    assert child_result.generated_count == 1
    assert child_calls == [1]
    assert child_result.expected_provider_transport_count == 1
    assert child_result.actual_provider_transport_count == 1
    assert child_closure_payload["payload"]["complete"] is True


def test_typed_manifest_file_deletion_regenerates_with_unreadable_authority_reason(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    parent_calls: list[int] = []

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        parent_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        pdf_path,
        parent_reader,
        job_id="missing-parent-manifest-file",
    )
    parent_service.run(parent_bundle)
    manifest_record = _typed_manifest_record(parent_service)
    manifest_path = Path(manifest_record.path)
    manifest_bytes = manifest_path.read_bytes()
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        manifest_record.path
    )
    manifest_path.unlink()

    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    try:
        child_service, child_bundle = _service(
            tmp_path / "child-run",
            pdf_path,
            child_reader,
            job_id="missing-child-manifest-file",
        )
        child_result = child_service.run(child_bundle, existing_summaries=imported)
        child_closure = child_service.finalize_provider_receipt_closure()
        child_closure_payload = json.loads(
            Path(child_closure.path).read_text(encoding="utf-8")
        )
    finally:
        manifest_path.write_bytes(manifest_bytes)

    reuse = child_result.summaries[0]["stage1_reuse"]
    assert parent_calls == [1]
    assert child_calls == [1]
    assert reuse["decision"] == "identity_match_unverified"
    assert reuse["reason"].startswith("typed_manifest_unreadable:")
    assert child_result.reused_count == 0
    assert child_result.generated_count == 1
    assert child_result.expected_provider_transport_count == 1
    assert child_result.actual_provider_transport_count == 1
    assert child_closure_payload["payload"]["complete"] is True


@pytest.mark.parametrize(
    ("manifest_path_field", "expected_reason"),
    (
        ("source_summary_artifact_path", "typed_manifest_source_summary_missing"),
        ("provider_receipt_closure_path", "typed_manifest_provider_closure_untrusted"),
        ("provider_receipt_ledger_path", "typed_manifest_provider_ledger_untrusted"),
    ),
)
def test_typed_manifest_missing_authority_blob_regenerates_with_precise_reason(
    tmp_path: Path,
    manifest_path_field: str,
    expected_reason: str,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)
    parent_calls: list[int] = []

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        parent_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        pdf_path,
        parent_reader,
        job_id=f"missing-parent-{manifest_path_field}",
    )
    parent_service.run(parent_bundle)
    manifest_record = _typed_manifest_record(parent_service)
    manifest = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    authority_path = Path(str(manifest[manifest_path_field]))
    authority_bytes = authority_path.read_bytes()
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        manifest_record.path
    )
    authority_path.unlink()

    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    try:
        child_service, child_bundle = _service(
            tmp_path / "child-run",
            pdf_path,
            child_reader,
            job_id=f"missing-child-{manifest_path_field}",
        )
        child_result = child_service.run(
            child_bundle,
            existing_summaries=imported,
        )
        child_closure = child_service.finalize_provider_receipt_closure()
        child_closure_payload = json.loads(
            Path(child_closure.path).read_text(encoding="utf-8")
        )
    finally:
        authority_path.write_bytes(authority_bytes)

    assert parent_calls == [1]
    assert child_calls == [1]
    assert child_result.summaries[0]["stage1_reuse"]["decision"] == "identity_match_unverified"
    assert child_result.summaries[0]["stage1_reuse"]["reason"] == expected_reason
    assert child_result.reused_count == 0
    assert child_result.generated_count == 1
    assert child_result.expected_provider_transport_count == 1
    assert child_result.actual_provider_transport_count == 1
    assert child_closure_payload["payload"]["complete"] is True


def test_typed_manifest_reuses_same_pdf_bytes_after_path_move_without_parent_registry(
    tmp_path: Path,
) -> None:
    parent_pdf = tmp_path / "parent" / "paper.pdf"
    parent_pdf.parent.mkdir(parents=True)
    _write_pdf(parent_pdf)
    parent_calls: list[int] = []

    def parent_reader(**kwargs: Any) -> Mapping[str, Any]:
        parent_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent_service, parent_bundle = _service(
        tmp_path / "parent-run",
        parent_pdf,
        parent_reader,
        job_id="parent-job",
        source_paper_id=str(parent_pdf),
    )
    parent_service.run(parent_bundle)
    manifest_record = _typed_manifest_record(parent_service)

    child_pdf = tmp_path / "moved" / "paper.pdf"
    child_pdf.parent.mkdir(parents=True)
    shutil.copyfile(parent_pdf, child_pdf)
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        manifest_record.path
    )
    child_calls: list[int] = []

    def child_reader(**kwargs: Any) -> Mapping[str, Any]:
        child_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    child_service, child_bundle = _service(
        tmp_path / "child-run",
        child_pdf,
        child_reader,
        job_id="child-job",
        source_paper_id=str(child_pdf),
        external_registry_resolver=lambda registry_id: parent_service.registry
        if registry_id == f"artifact-registry:{parent_service.job_id}"
        else None,
    )
    result = child_service.run(child_bundle, existing_summaries=imported)
    closure = child_service.finalize_provider_receipt_closure()
    closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))
    stage_map = resolve_current_stage_closure_map(child_service.registry)

    assert parent_calls == [1]
    assert child_calls == []
    assert result.reused_count == 1
    assert result.generated_count == 0
    assert result.expected_provider_transport_count == 0
    assert result.actual_provider_transport_count == 0
    binding = result.summaries[0]["stage1_reuse"]["binding"]
    assert binding["original_source_location"] == str(parent_pdf)
    assert binding["current_source_location"] == str(child_pdf)
    assert binding["location_changed"] is True
    assert closure_payload["payload"]["complete"] is True
    reuse_issues = tuple(
        issue
        for issue in stage_map.blocking_issues
        if issue.startswith("provider_closure_reuse_")
    )
    assert reuse_issues == (), "\n".join(reuse_issues)


def test_current_stage1_parent_registry_tamper_regenerates_fail_closed(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "parent.pdf"
    _write_pdf(pdf_path)

    parent_calls: list[int] = []

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        parent_calls.append(1)
        return {"status": "success", "content": _canonical_summary()}

    parent, parent_bundle = _service(tmp_path / "parent", pdf_path, reader, job_id="parent-authority")
    parent.run(parent_bundle)
    summary_record = next(
        record
        for record in parent.registry.list_records()
        if record.artifact_type == "summary_file"
    )
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(summary_record.path)
    assert parent_calls == [1]

    # Use the parent Registry route explicitly.  Generated summaries also carry
    # a portable manifest for later export, but this test exercises the direct
    # external-registry authority independently of that portable copy.
    parent_registry_import: list[dict[str, Any]] = []
    for item in imported:
        normalized = json.loads(json.dumps(item))
        reuse = dict(normalized.get("stage1_reuse") or {})
        for field in ("authority_kind", "typed_manifest_path", "typed_manifest_artifact_id", "typed_manifest_artifact_hash"):
            reuse.pop(field, None)
        normalized["stage1_reuse"] = reuse
        parent_registry_import.append(normalized)

    closure_record = next(
        record
        for record in parent.registry.list_records()
        if record.artifact_type == "provider_receipt_closure"
    )
    original_registry = Path(parent.registry.registry_path).read_bytes()
    original_target_bytes = {
        summary_record.artifact_id: Path(summary_record.path).read_bytes(),
        closure_record.artifact_id: Path(closure_record.path).read_bytes(),
    }

    def tamper_parent_record(artifact_id: str, marker: bytes) -> None:
        registry_payload = json.loads(Path(parent.registry.registry_path).read_text(encoding="utf-8"))
        entry = next(item for item in registry_payload["artifacts"] if item["artifact_id"] == artifact_id)
        path = Path(entry["path"])
        path.write_bytes(path.read_bytes() + marker)
        entry["content_hash"] = file_sha256(path)
        Path(parent.registry.registry_path).write_text(
            json.dumps(registry_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    for index, artifact_id in enumerate((summary_record.artifact_id, closure_record.artifact_id)):
        marker = f"\nparent-semantic-tamper-{index}\n".encode("utf-8")
        tamper_parent_record(artifact_id, marker)
        child_calls: list[int] = []

        def child_reader(**kwargs: Any) -> Mapping[str, Any]:
            child_calls.append(1)
            return {"status": "success", "content": _canonical_summary()}

        child, child_bundle = _service(
            tmp_path / f"child-{artifact_id.replace(':', '-')}",
            pdf_path,
            child_reader,
            job_id=f"child-{artifact_id.replace(':', '-')}",
            external_registry_resolver=lambda registry_id: ArtifactRegistry(
                parent.registry.registry_path, parent.job_id
            )
            if registry_id == f"artifact-registry:{parent.job_id}"
            else None,
        )
        result = child.run(child_bundle, existing_summaries=parent_registry_import)
        closure = child.finalize_provider_receipt_closure()
        closure_payload = json.loads(Path(closure.path).read_text(encoding="utf-8"))
        stage_map = resolve_current_stage_closure_map(child.registry)

        assert child_calls == [1]
        assert result.reused_count == 0
        assert result.generated_count == 1
        assert result.expected_provider_transport_count == 1
        assert result.actual_provider_transport_count == 1
        assert closure_payload["payload"]["complete"] is True
        reuse_issues = tuple(
            issue
            for issue in stage_map.blocking_issues
            if issue.startswith("provider_closure_reuse_")
        )
        assert reuse_issues == (), stage_map.to_dict()
        assert stage_map.provider_closures_by_stage["analyze"]["complete"] is True

        # Restore the parent authority before the next mutation; the child has
        # already recorded the fail-closed decision for this attempt.
        target_path = Path(
            next(
                item["path"]
                for item in json.loads(original_registry.decode("utf-8"))["artifacts"]
                if item["artifact_id"] == artifact_id
            )
        )
        target_path.write_bytes(original_target_bytes[artifact_id])
        Path(parent.registry.registry_path).write_bytes(original_registry)


def test_current_stage1_existing_reuse_blocks_after_parent_authority_drift(
    tmp_path: Path,
) -> None:
    """A reused child must re-check the external parent, not its local snapshot."""

    pdf_path = tmp_path / "parent.pdf"
    _write_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        return {"status": "success", "content": _canonical_summary()}

    parent, parent_bundle = _service(
        tmp_path / "parent", pdf_path, reader, job_id="parent-authority"
    )
    parent_result = parent.run(parent_bundle)
    summary_record = next(
        record
        for record in parent.registry.list_records()
        if record.artifact_type == "summary_file"
    )
    imported = [dict(item) for item in parent_result.summaries]
    direct_parent_import: list[dict[str, Any]] = []
    for item in imported:
        normalized = json.loads(json.dumps(item))
        reuse = dict(normalized.get("stage1_reuse") or {})
        for field in (
            "authority_kind",
            "typed_manifest_path",
            "typed_manifest_artifact_id",
            "typed_manifest_artifact_hash",
        ):
            reuse.pop(field, None)
        binding = dict(reuse.get("binding") or {})
        for field in (
            "typed_manifest_artifact_id",
            "typed_manifest_artifact_hash",
            "typed_manifest_content_hash",
        ):
            binding.pop(field, None)
        reuse["binding"] = binding
        normalized["stage1_reuse"] = reuse
        direct_parent_import.append(normalized)

    child, child_bundle = _service(
        tmp_path / "child",
        pdf_path,
        reader,
        job_id="child-authority-drift",
        external_registry_resolver=lambda registry_id: ArtifactRegistry(
            parent.registry.registry_path, parent.job_id
        )
        if registry_id in {parent.job_id, f"artifact-registry:{parent.job_id}"}
        else None,
    )
    reused = child.run(child_bundle, existing_summaries=direct_parent_import)
    child.finalize_provider_receipt_closure()
    assert reused.reused_count == 1, reused.summaries[0].get("stage1_reuse")
    assert reused.generated_count == 0
    clean_map = resolve_current_stage_closure_map(child.registry)
    assert not tuple(
        issue
        for issue in clean_map.blocking_issues
        if issue.startswith("provider_closure_reuse_")
    ), clean_map.to_dict()

    closure_record = next(
        record
        for record in parent.registry.list_records()
        if record.artifact_type == "provider_receipt_closure"
    )
    original_registry = Path(parent.registry.registry_path).read_bytes()
    original_target_bytes = {
        summary_record.artifact_id: Path(summary_record.path).read_bytes(),
        closure_record.artifact_id: Path(closure_record.path).read_bytes(),
    }

    def tamper_parent_record(artifact_id: str, marker: bytes) -> None:
        registry_payload = json.loads(
            Path(parent.registry.registry_path).read_text(encoding="utf-8")
        )
        entry = next(
            item for item in registry_payload["artifacts"] if item["artifact_id"] == artifact_id
        )
        path = Path(entry["path"])
        path.write_bytes(path.read_bytes() + marker)
        entry["content_hash"] = file_sha256(path)
        Path(parent.registry.registry_path).write_text(
            json.dumps(registry_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        for index, artifact_id in enumerate(
            (summary_record.artifact_id, closure_record.artifact_id)
        ):
            tamper_parent_record(
                artifact_id, f"\npost-reuse-parent-tamper-{index}\n".encode("utf-8")
            )
            drifted = resolve_current_stage_closure_map(child.registry)
            reuse_issues = tuple(
                issue
                for issue in drifted.blocking_issues
                if issue.startswith("provider_closure_reuse_")
            )
            assert reuse_issues, drifted.to_dict()
            Path(parent.registry.registry_path).write_bytes(original_registry)
            for record_id, payload in original_target_bytes.items():
                record = next(
                    item
                    for item in parent.registry.list_records()
                    if item.artifact_id == record_id
                )
                Path(record.path).write_bytes(payload)
    finally:
        Path(parent.registry.registry_path).write_bytes(original_registry)
        for record_id, payload in original_target_bytes.items():
            record = next(
                item for item in parent.registry.list_records() if item.artifact_id == record_id
            )
            Path(record.path).write_bytes(payload)


def test_current_stage1_rejects_placeholder_provider_output(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    _write_pdf(pdf_path)

    def reader(**kwargs: Any) -> Mapping[str, Any]:
        summary = _canonical_summary()
        summary["core_analysis"]["findings"] = "Dummy findings."
        return {"status": "success", "content": summary}

    service, bundle = _service(tmp_path, pdf_path, reader)
    try:
        service.run(bundle)
    except RuntimeError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("placeholder Stage 1 output was accepted")

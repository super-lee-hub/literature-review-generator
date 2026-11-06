from __future__ import annotations

import configparser
import hashlib
import copy
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest
import tests.test_current_runtime_full_e2e as runtime_fixture
import tests.test_run_first_typed_validation_authority as validation_fixture
import tests.test_stage1_closure_adversarial as stage1_fixture

_REPO_TESTS = Path.cwd() / "tests"
if (_REPO_TESTS / "test_summary_correction_reuse.py").is_file():
    sys.path.insert(0, str(_REPO_TESTS))

import tests.test_summary_correction as correction_data_fixture
import tests.test_summary_correction_reuse as correction_fixture
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import hash_json
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext
from validation.closure import resolve_current_stage_closure_map
from validation.evidence_loader import PreprocessEvidenceLoader

PATCH_ROOT = Path(__file__).resolve().parents[1]
TARGET_KEY = "10.5555/tripathi2017"
SECOND_KEY = "10.5555/paper00"
TARGET_TITLE = "Et tu, Brute? How unfair!"
SECOND_TITLE = "Synthetic paper 0"


def _load_patch(monkeypatch: pytest.MonkeyPatch) -> Any:
    fixture_path = PATCH_ROOT / "tests" / "test_automatic_stage1_reuse_v2.py"
    spec = importlib.util.spec_from_file_location(
        "tests.test_automatic_stage1_reuse_v2_runner_fixture",
        fixture_path,
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, fixture)
    spec.loader.exec_module(fixture)
    fixture._activate_patch(monkeypatch)
    return fixture


def _write_identity_pdf(
    path: Path,
    *,
    title: str,
    author: str,
    year: int,
    doi: str,
    page_count: int,
) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    section_names = (
        "study design and participant recruitment",
        "measurement strategy and construct validity",
        "experimental conditions and comparison groups",
        "model estimates and robustness analysis",
        "boundary conditions and alternative explanations",
        "discussion of implications and limitations",
    )
    for page_number in range(page_count):
        page = document.new_page()
        paragraphs: list[str] = []
        if page_number == 0:
            paragraphs.append(
                f"{title}. {author}. {year}. DOI {doi}. "
                "This synthetic source is prepared for a local provenance test."
            )
        for paragraph_number in range(11):
            section = section_names[(page_number + paragraph_number) % len(section_names)]
            paragraphs.append(
                f"Page {page_number + 1}, paragraph {paragraph_number + 1}, addresses {section}. "
                f"In study unit {page_number + 1:02d}-{paragraph_number + 1:02d}, "
                "the investigators define the focal construct before comparing the planned "
                "condition with its control. Recruitment, eligibility, timing, and measure "
                "selection are documented separately so that the evidence trail remains "
                "auditable. The analysis reports the observed pattern, its uncertainty, "
                "the competing interpretation, and the scope to which the result applies. "
                f"Sensitivity check {page_number * 11 + paragraph_number + 1} retains "
                "the same inclusion rule while varying the prespecified estimator."
            )
        if page_number == page_count - 1:
            paragraphs.append(
                "Discussion and conclusions: the results are interpreted with the stated "
                "limitations, and references identify the materials used for each claim."
            )
        remaining = page.insert_textbox(
            pymupdf.Rect(
                page.rect.x0 + 42,
                page.rect.y0 + 42,
                page.rect.x1 - 42,
                page.rect.y1 - 42,
            ),
            "\n\n".join(paragraphs),
            fontsize=8,
            fontname="helv",
            lineheight=1.15,
        )
        assert remaining >= 0, f"synthetic page {page_number + 1} overflowed its text box"
    document.set_metadata(
        {
            "title": title,
            "author": author,
            "subject": doi,
            "keywords": doi,
            "creator": "local-test-fixture",
            "producer": "PyMuPDF",
        }
    )
    document.save(path)
    document.close()
    return path, file_sha256(path)

def _render_correction_pages(
    *,
    pdf_path: Path,
    target_root: Path,
    pdf_hash: str,
) -> dict[int, dict[str, Any]]:
    target_root.mkdir(parents=True, exist_ok=True)
    render_pages = (
        (5, 83, "Experiment 1 design and sample"),
        (6, 84, "Experiment 1 measures and findings"),
        (7, 85, "Experiment 1 mediation results"),
        (8, 86, "Experiment 2 field switching and price labels"),
        (10, 88, "Experiment 3 fixed 40 percent condition and design"),
        (11, 89, "Experiment 3 interaction results"),
    )
    renders: dict[int, dict[str, Any]] = {}
    document = pymupdf.open(pdf_path)
    try:
        for page, journal_page, scope in render_pages:
            render_path = target_root / f"page-{page:02d}.png"
            document.load_page(page - 1).get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(render_path)
            renders[page] = {
                "pdf_page": page,
                "journal_page": journal_page,
                "source_pdf_sha256": pdf_hash,
                "review_render_path": str(render_path),
                "review_render_sha256": file_sha256(render_path),
                "render_scale": 1.0,
                "anchor_scope": scope,
            }
    finally:
        document.close()
    return renders


def _publish_canonical_summary_set(
    *,
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    source_summary_record: Any,
    summary_rows: list[dict[str, Any]],
) -> Any:
    summary_set_hash = hash_json(summary_rows)
    artifact_id = f"outline-v3:stage1-summaries:{summary_set_hash}"
    payload = {
        "artifact_type": "stage1_canonical_summaries",
        "artifact_version": "v1",
        "job_id": registry.job_id,
        "summary_set_hash": summary_set_hash,
        "summaries": summary_rows,
    }
    return publish_json_artifact(
        LocalPublicationContext(),
        registry,
        workspace.artifact_path(
            f"outline_v3/inputs/stage1_summaries_{summary_set_hash[:24]}.json"
        ),
        payload,
        artifact_id=artifact_id,
        artifact_role="stage1_input",
        artifact_type="stage1_canonical_summaries",
        artifact_version="v1",
        producer=__name__,
        depends_on=[ArtifactDependencyRefV2.from_record(source_summary_record)],
        metadata={
            "immutable": True,
            "summary_set_hash": summary_set_hash,
            "versioned_artifact_id": artifact_id,
        },
    )


def _rows_from_summary_file(record: Any) -> list[dict[str, Any]]:
    raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [dict(row) for row in raw if isinstance(row, Mapping)]
    if isinstance(raw, Mapping):
        rows = raw.get("summaries")
        return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    return []


def _spec(
    *,
    job_id: str,
    config_path: Path,
    report_path: Path,
    library_path: Path,
    queue_path: Path,
    reuse_stage1: bool | None,
    reuse_summary_files: tuple[str, ...] = (),
) -> RuntimeJobSpec:
    return RuntimeJobSpec(
        project_name=job_id,
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(report_path),
            library_path=str(library_path),
        ),
        job_id=job_id,
        config=str(config_path),
        action="analyze",
        reuse_stage1=reuse_stage1,
        reuse_summary_files=reuse_summary_files,
        queue_file=str(queue_path),
        metadata={"requested_stages": ["analyze"]},
    )


def test_actual_runner_auto_reuses_v2_then_v1_last_and_validation_keeps_origin_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2_fixture = _load_patch(monkeypatch)
    output_root = tmp_path / "output"
    output_root.mkdir()
    v2_fixture._route_workspaces_to_root(monkeypatch, output_root)

    library_path = tmp_path / "zotero-storage"
    target_pdf, target_pdf_hash = _write_identity_pdf(
        library_path / "ABCD1234" / "tripathi-source.pdf",
        title=TARGET_TITLE,
        author="Example Author",
        year=2017,
        doi=TARGET_KEY,
        page_count=14,
    )
    second_pdf, _second_pdf_hash = _write_identity_pdf(
        library_path / "EFGH5678" / "paper00.pdf",
        title=SECOND_TITLE,
        author="Example Author",
        year=2025,
        doi=SECOND_KEY,
        page_count=3,
    )
    report_path = tmp_path / "zotero-report.txt"
    report_path.write_text(
        "\n".join(
            [
                "*",
                "Item Type: Journal Article",
                "Author: Example Author",
                f"Title: {TARGET_TITLE}",
                "Publication: Example Journal",
                "Year: 2017",
                f"DOI: {TARGET_KEY}",
                "Attachment: tripathi-source.pdf",
                "*",
                "Item Type: Journal Article",
                "Author: Example Author",
                f"Title: {SECOND_TITLE}",
                "Publication: Example Journal",
                "Year: 2025",
                f"DOI: {SECOND_KEY}",
                "Attachment: paper00.pdf",
                "",
            ]
        ),
        encoding="utf-8",
    )
    page_renders = _render_correction_pages(
        pdf_path=target_pdf,
        target_root=tmp_path / "review-renders",
        pdf_hash=target_pdf_hash,
    )

    config_path = Path(stage1_fixture._test_config(tmp_path))
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path, encoding="utf-8")
    config["Paths"]["output_path"] = str(output_root)
    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)

    cold_reader_calls: list[str] = []

    def configured_reader(
        _service: Any,
        *,
        item: Any,
        built_input: Any,
        primary_config: Mapping[str, Any],
        backup_config: Mapping[str, Any],
        runtime: Any,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        del built_input, primary_config, backup_config, runtime
        key = str(item.canonical_paper_key)
        cold_reader_calls.append(key)
        if key == TARGET_KEY:
            title = TARGET_TITLE
            finding = "The treatment improves the measured outcome in the source-defined test."
            year = 2017
        elif key == SECOND_KEY:
            title = SECOND_TITLE
            finding = "The comparison condition preserves the bounded source finding."
            year = 2025
        else:
            raise AssertionError(f"unexpected cold source key: {key}")
        summary = runtime_fixture._reader_summary(key, title, finding)
        summary["core_analysis"]["key_points"] = [
            f"Source-backed point {index + 1}: {finding}"
            for index in range(5)
        ]
        paper_info = summary["paper_info"]
        paper_info.update(
            {
                "canonical_paper_key": key,
                "source_paper_id": str(item.source_paper_id),
                "pdf_path": str(item.source_pdf),
                "title": title,
                "doi": key,
                "authors": ["Example Author"],
                "year": year,
            }
        )
        summary["paper_metadata"].update(
            {"title": title, "authors": ["Example Author"], "year": str(year), "doi": key}
        )
        summary["source_mode"] = "zotero"
        return {"status": "success", "content": summary}

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
        configured_reader,
    )
    cold_result = AgentRuntimeRunner(
        _spec(
            job_id="v2-runner-cold",
            config_path=config_path,
            report_path=report_path,
            library_path=library_path,
            queue_path=tmp_path / "cold.queue.json",
            reuse_stage1=None,
        )
    ).run()
    assert cold_result.completion_status == "complete", cold_result
    assert set(cold_reader_calls) == {TARGET_KEY, SECOND_KEY}
    origin_workspace, origin_registry = AgentRuntimeRunner._open_workspace(
        cold_result.workspace_path
    )
    original_rows = _rows_from_summary_file(origin_registry.get("summary_file"))
    assert len(original_rows) == 2
    row_by_key = {
        str(row["paper_info"]["canonical_paper_key"]): row
        for row in original_rows
    }
    assert set(row_by_key) == {TARGET_KEY, SECOND_KEY}
    source_bundle_record = origin_registry.get("source_bundle")
    assert source_bundle_record is not None and source_bundle_record.status == "ready"
    source_bundle = json.loads(Path(source_bundle_record.path).read_text(encoding="utf-8"))
    source_entries = {
        str(item.get("canonical_paper_key") or ""): item
        for item in source_bundle.get("paper_work_items", [])
        if isinstance(item, Mapping)
    }
    for key in (TARGET_KEY, SECOND_KEY):
        source_entry = source_entries[key]
        source_entry_bytes = json.dumps(
            source_entry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        row_by_key[key]["provenance"] = {
            "source_entry_hash": hashlib.sha256(source_entry_bytes).hexdigest(),
            "source_entry_bytes": len(source_entry_bytes),
        }
        row_by_key[key]["paper_info"]["citation_key"] = (
            "tripathiTuBruteHow2017" if key == TARGET_KEY else "paper00"
        )

    canonical_rows = [
        correction_data_fixture._summary_entry(index, target_pdf)
        for index in range(63)
    ]
    canonical_rows[0] = copy.deepcopy(row_by_key[SECOND_KEY])
    canonical_rows[32] = copy.deepcopy(row_by_key[TARGET_KEY])
    canonical_record = _publish_canonical_summary_set(
        workspace=origin_workspace,
        registry=origin_registry,
        source_summary_record=origin_registry.get("summary_file"),
        summary_rows=canonical_rows,
    )
    other_prior_rows = [
        row for index, row in enumerate(canonical_rows) if index not in {0, 32}
    ]
    correction_fixture._publish_v1_prior_basis(
        workspace=origin_workspace,
        registry=origin_registry,
        source_record=canonical_record,
        summaries=other_prior_rows,
    )

    correction_workspace = JobWorkspace.create(
        str(output_root),
        "correction-project",
        "correction-job",
    )
    correction_registry = ArtifactRegistry(
        correction_workspace.paths.registry_path,
        correction_workspace.job_id,
    )
    proposal = correction_fixture._proposal_payload(
        canonical_record,
        canonical_rows,
        target_pdf,
        target_pdf_hash,
        page_renders,
    )
    correction = correction_fixture.prepare_source_summary_correction_candidate(
        proposal_payload=proposal,
        source_registry=origin_registry,
        source_artifact_id=canonical_record.artifact_id,
        destination_workspace=correction_workspace,
        destination_registry=correction_registry,
        publication_context=LocalPublicationContext(),
    )
    candidate_record = correction_registry.get(correction.candidate_artifact_id)
    assert candidate_record is not None
    adoption = correction_fixture.adopt_source_summary_correction_candidate(
        source_registry=origin_registry,
        destination_registry=correction_registry,
        workspace=correction_workspace,
        publication_context=LocalPublicationContext(),
        candidate_artifact_id=correction.candidate_artifact_id,
        expected_candidate_hash=candidate_record.content_hash,
        actor="runner-test-owner",
        reason="Verify the approved V2 runner reuse path",
    )
    exported = correction_fixture.export_owner_corrected_stage1_reuse_authority(
        origin_registry=origin_registry,
        correction_registry=correction_registry,
        correction_workspace=correction_workspace,
        adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
        publication_context=LocalPublicationContext(),
        external_registry_resolver=lambda job_id: {
            origin_registry.job_id: origin_registry,
            correction_registry.job_id: correction_registry,
        }.get(job_id),
    )
    v2_record = correction_registry.get(exported.owner_corrected_manifest_artifact_id)
    assert v2_record is not None and v2_record.artifact_version == "v2"

    warm_reader_calls: list[str] = []

    def forbidden_reader(
        _service: Any,
        *,
        item: Any,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        warm_reader_calls.append(str(item.canonical_paper_key))
        raise AssertionError(f"unexpected Reader call for {item.canonical_paper_key}")

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
        forbidden_reader,
    )
    child_result = AgentRuntimeRunner(
        _spec(
            job_id="v2-runner-child",
            config_path=config_path,
            report_path=report_path,
            library_path=library_path,
            queue_path=tmp_path / "child.queue.json",
            reuse_stage1=True,
            reuse_summary_files=(),
        )
    ).run()
    assert child_result.completion_status == "complete", child_result
    assert warm_reader_calls == []

    child_workspace, child_registry = AgentRuntimeRunner._open_workspace(
        child_result.workspace_path
    )
    source_record = child_registry.get("summary_source_manifest")
    assert source_record is not None and source_record.status == "ready"
    source_payload = json.loads(Path(source_record.path).read_text(encoding="utf-8"))
    source_items = source_payload["source_items"]
    assert [item["canonical_paper_key"] for item in source_items] == [
        TARGET_KEY,
        SECOND_KEY,
    ]
    item_by_key = {
        str(item["canonical_paper_key"]): item
        for item in source_items
    }
    assert item_by_key[TARGET_KEY]["disposition"] == "reused"
    assert item_by_key[SECOND_KEY]["disposition"] == "reused"
    assert (
        item_by_key[TARGET_KEY]["automatic_stage1_reuse_owner"]["manifest_artifact_version"]
        == "v2"
    )
    assert (
        item_by_key[SECOND_KEY]["automatic_stage1_reuse_owner"]["manifest_artifact_version"]
        == "v1"
    )

    child_summary_record = child_registry.get("summary_file")
    assert child_summary_record is not None and child_summary_record.status == "ready"
    child_rows = _rows_from_summary_file(child_summary_record)
    child_by_key = {
        str(row["paper_info"]["canonical_paper_key"]): row
        for row in child_rows
    }
    v2_payload = json.loads(Path(v2_record.path).read_text(encoding="utf-8"))
    assert child_by_key[TARGET_KEY]["ai_summary"] == v2_payload["summary_payload"]

    closure = resolve_current_stage_closure_map(child_registry)
    assert closure.blocking_issues == (), closure.to_dict()
    analyze = closure.provider_closures_by_stage["analyze"]
    assert analyze["complete"] is True
    assert analyze["expected_call_ids"] == analyze["observed_call_ids"] == []

    validation_case = SimpleNamespace(
        parent_workspace=origin_workspace,
        parent_registry=origin_registry,
        child_workspace=child_workspace,
        child_registry=child_registry,
        manifest_paths=(),
    )
    service = validation_fixture._validation_service(validation_case)
    _review, _manifest, papers, preprocess, _metadata = validation_fixture._load_inputs(service)
    assert service._validation_source_authority_diagnostics == ()
    assert len(papers) == 2
    assert set(preprocess) == {TARGET_KEY, SECOND_KEY}
    expected_owners = validation_fixture._parent_papers(validation_case)
    fingerprint, _identity, problems = validation_fixture._fingerprint(
        service,
        papers,
        sorted(expected_owners),
    )
    assert problems == (), problems
    fingerprint_by_key = {
        str(row["canonical_paper_key"]): row
        for row in fingerprint["papers"]
    }
    for key, owner_record in expected_owners.items():
        assert fingerprint_by_key[key]["source_job_id"] == origin_workspace.job_id
        assert fingerprint_by_key[key]["paper_artifact_id"] == owner_record.artifact_id
        assert fingerprint_by_key[key]["paper_artifact_hash"] == owner_record.content_hash
        selected_paper = next(
            paper
            for paper in papers
            if paper["paper_identity"]["canonical_paper_key"] == key
        )
        evidence_manifest_path = selected_paper["stage1_inputs"]["evidence_manifest_path"]
        raw_manifest = json.loads(Path(evidence_manifest_path).read_text(encoding="utf-8"))
        manifest_body = raw_manifest.get("payload", raw_manifest)
        evidence_by_type = {
            str(row["artifact_type"]): row
            for row in manifest_body["artifacts"]
            if isinstance(row, Mapping)
        }
        evidence = PreprocessEvidenceLoader().load_evidence(
            normalized_text_path=evidence_by_type["normalized_text"]["path"],
            chunks_path=evidence_by_type["chunks"]["path"],
            page_index_path=evidence_by_type["page_index"]["path"],
            manifest_path=evidence_manifest_path,
            strict=True,
        )
        assert evidence.normalized_text
        assert evidence.chunks and evidence.page_index

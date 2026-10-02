from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.paper_identity import build_canonical_paper_key
from services.queue_service import LocalPublicationContext
from services.summary_correction import (
    CANDIDATE_ARTIFACT_TYPE,
    PROPOSAL_ARTIFACT_TYPE,
    SOURCE_SNAPSHOT_ARTIFACT_TYPE,
    SourceSummaryCorrectionError,
    prepare_source_summary_correction_candidate,
    verify_source_summary_correction_candidate,
)


def _make_source_pdf(root: Path) -> tuple[Path, str, dict[int, dict[str, Any]]]:
    pdf_path = root / "tripathi-source.pdf"
    document = pymupdf.open()
    for _ in range(14):
        document.new_page()
    document.save(pdf_path)
    document.close()
    pdf_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    page_renders: dict[int, dict[str, Any]] = {}
    source_doc = pymupdf.open(pdf_path)
    try:
        for page, journal_page, scope in (
            (5, 83, "Experiment 1 design and sample"),
            (6, 84, "Experiment 1 measures and findings"),
            (7, 85, "Experiment 1 mediation results"),
            (8, 86, "Experiment 2 field switching and price labels"),
            (10, 88, "Experiment 3 fixed 40 percent condition and design"),
            (11, 89, "Experiment 3 interaction results"),
        ):
            render_path = root / f"page-{page:02d}.png"
            source_doc.load_page(page - 1).get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(render_path)
            page_renders[page] = {
                "pdf_page": page,
                "journal_page": journal_page,
                "source_pdf_sha256": pdf_hash,
                "review_render_path": str(render_path),
                "review_render_sha256": hashlib.sha256(render_path.read_bytes()).hexdigest(),
                "render_scale": 1.0,
                "anchor_scope": scope,
            }
    finally:
        source_doc.close()
    return pdf_path, pdf_hash, page_renders


def _summary_entry(index: int, pdf_path: Path) -> dict[str, Any]:
    doi = "10.5555/tripathi2017" if index == 32 else f"10.5555/paper{index:02d}"
    citation_key = "tripathiTuBruteHow2017" if index == 32 else f"paper{index:02d}"
    title = "Et tu, Brute? How unfair!" if index == 32 else f"Synthetic paper {index}"
    return {
        "status": "success",
        "source_mode": "zotero",
        "paper_info": {
            "citation_key": citation_key,
            "title": title,
            "doi": doi,
            "pdf_path": str(pdf_path) if index == 32 else f"unused-{index}.pdf",
            "canonical_paper_key": doi,
        },
        "ai_summary": {
            "schema_version": "summary_v2_lite",
            "routing": {"paper_type": "empirical"},
            "core_analysis": {
                "summary": f"Existing summary row {index}.",
                "methodology": f"Existing method row {index}.",
                "findings": f"Existing findings row {index}.",
                "conclusions": "Existing conclusion.",
                "relevance": "Existing relevance.",
                "limitations": "Existing limitation.",
                "theoretical_framework": "Existing framework.",
                "research_gap": "Existing research gap.",
                "key_points": ["kp0", "kp1", "kp2", f"old exp2 point {index}", f"old exp3 point {index}"],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [],
                    "data_source_and_size": None,
                    "analysis_technique": None,
                    "core_variables": {"independent": [], "dependent": [], "mediators": [], "moderators": [], "controls": [], "other_core_constructs": []},
                    "sample_characteristics_or_context": None,
                }
            },
            "quality_audit": {
                "needs_manual_review": True,
                "completeness_score": 0.0,
                "missing_critical_fields": ["specialized_details.empirical.data_source_and_size"],
            },
        },
        "provenance": {"source_entry_hash": f"entry-hash-{index}", "source_entry_bytes": 100 + index},
        "preserved_false": False,
        "preserved_zero": 0,
        "preserved_none": None,
    }


def _get_path(item: dict[str, Any], path: str) -> Any:
    current: Any = item
    for component in path.split("."):
        if component.endswith("]") and "[" in component:
            name, index_text = component[:-1].split("[", 1)
            current = current[name][int(index_text)]
        else:
            current = current[component]
    return current


def _field_patch(item: dict[str, Any], path: str, after: str, groups: list[str]) -> dict[str, Any]:
    before = _get_path(item, path)
    return {
        "proposal_field_ref": "PROPOSED_SOURCE_FIELD_REF:" + path.replace(".", "_"),
        "canonical_stage1_field_path": path,
        "before_value": before,
        "before_value_hash_json": hash_json(before),
        "proposed_after_value": after,
        "proposed_after_value_hash_json": hash_json(after),
        "source_anchor_groups": groups,
        "existing_downstream_outline_field_ref": None,
    }


def _build_source(tmp_path: Path) -> tuple[JobWorkspace, ArtifactRegistry, dict[str, Any], list[dict[str, Any]], Path, str, dict[int, dict[str, Any]]]:
    source_workspace = JobWorkspace.create(str(tmp_path / "source-output"), "source-project", "source-job")
    source_registry = ArtifactRegistry(source_workspace.paths.registry_path, source_workspace.job_id)
    source_files = tmp_path / "source-files"
    source_files.mkdir(exist_ok=True)
    pdf_path, pdf_hash, page_renders = _make_source_pdf(source_files)
    summaries = [_summary_entry(index, pdf_path) for index in range(63)]
    summary_set_hash = hash_json(summaries)
    envelope = {
        "artifact_type": "stage1_canonical_summaries",
        "artifact_version": "v1",
        "job_id": source_workspace.job_id,
        "summary_set_hash": summary_set_hash,
        "summaries": summaries,
    }
    artifact_id = f"outline-v3:stage1-summaries:{summary_set_hash}"
    record = publish_json_artifact(
        LocalPublicationContext(),
        source_registry,
        source_workspace.artifact_path(f"outline_v3/inputs/stage1_summaries_{summary_set_hash[:24]}.json"),
        envelope,
        artifact_id=artifact_id,
        artifact_role="stage1_input",
        artifact_type="stage1_canonical_summaries",
        artifact_version="v1",
        producer="tests.test_summary_correction",
        metadata={"immutable": True, "summary_set_hash": summary_set_hash, "versioned_artifact_id": artifact_id},
    )
    return source_workspace, source_registry, record, summaries, pdf_path, pdf_hash, page_renders


def _proposal_payload(
    source_record: Any,
    summaries: list[dict[str, Any]],
    pdf_path: Path,
    pdf_hash: str,
    page_renders: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    target = summaries[32]
    key_fields = [
        "ai_summary.core_analysis.summary",
        "ai_summary.core_analysis.methodology",
        "ai_summary.core_analysis.findings",
        "ai_summary.core_analysis.key_points[3]",
        "ai_summary.core_analysis.key_points[4]",
        "ai_summary.specialized_details.empirical.data_source_and_size",
        "ai_summary.specialized_details.empirical.analysis_technique",
        "ai_summary.specialized_details.empirical.sample_characteristics_or_context",
    ]
    updates = {
        key_fields[0]: "Experiment 1, Experiment 2, and Experiment 3 test different outcomes and moderators.",
        key_fields[1]: "Experiment 1 uses a T-shirt scenario. Experiment 2 measures switching. Experiment 3 holds the price increase at 40%.",
        key_fields[2]: "Experiment 1 includes nonsignificant tests. Experiment 2 reports an omnibus four-condition chi-square. Experiment 3 includes a nonsignificant referent main effect.",
        key_fields[3]: "Experiment 2 measured actual switching in a four-condition field test; no trust-by-price interaction test is reported.",
        key_fields[4]: "Experiment 3 tests trust by self/other referent with the increase fixed at 40%.",
        key_fields[5]: "Experiment 1 n=250; Experiment 2 n=161; Experiment 3 n=162.",
        key_fields[6]: "Experiment 1 ANCOVA and moderated mediation; Experiment 2 four-cell chi-square; Experiment 3 ANCOVA.",
        key_fields[7]: "Experiment 1 college-affiliated young apparel shoppers; Experiment 2 graduate/postgraduate volunteers; Experiment 3 young college-festival consumer volunteers.",
    }
    groups = {
        key_fields[0]: ["exp1", "exp3"],  # Adapter must bind Experiment 2 from the explicit text reference.
        key_fields[1]: ["exp1", "exp2", "exp3"],
        key_fields[2]: ["exp1", "exp2", "exp3"],
        key_fields[3]: ["exp2"],
        key_fields[4]: ["exp3"],
        key_fields[5]: ["exp1", "exp2", "exp3"],
        key_fields[6]: ["exp1", "exp2", "exp3"],
        key_fields[7]: ["exp1", "exp2", "exp3"],
    }
    mapping_specs = [
        (1, "PROPOSED_STUDY_REF:TRIPATHI2017:EXPERIMENT_1", [5, 6, 7], key_fields[:3] + key_fields[5:]),
        (2, "PROPOSED_STUDY_REF:TRIPATHI2017:EXPERIMENT_2", [8], key_fields[:3] + key_fields[5:]),
        (3, "PROPOSED_STUDY_REF:TRIPATHI2017:EXPERIMENT_3", [10, 11], key_fields[:3] + key_fields[5:]),
    ]
    studies = []
    for number, study_ref, pages, source_paths in mapping_specs:
        studies.append({
            "proposed_study_ref": study_ref,
            "reported_experiment_number": number,
            "canonical_study_id": None,
            "source_claim_ids": [],
            "evidence_ids": [],
            "provider_receipt_ids": [],
            "source_field_paths": source_paths,
            "status": "PROPOSED_MAPPING_ONLY",
            "page_bindings": [page_renders[page] for page in pages],
        })
    patches = [_field_patch(target, path, updates[path], groups[path]) for path in key_fields]
    source_provenance = copy.deepcopy(target["provenance"])
    return {
        "artifact_type": "stage1_summary_correction_proposal",
        "artifact_version": "v1-proposal",
        "status": "PROPOSED_ONLY_NOT_CANONICAL_NOT_APPLIED",
        "proposal_id": "PROPOSED_REPAIR_PROPOSAL_TRIPATHI2017_STAGE1_TEST_001",
        "source_authority": {
            "job_id": source_record.job_id,
            "artifact_type": source_record.artifact_type,
            "artifact_version": source_record.artifact_version,
            "artifact_id": source_record.artifact_id,
            "artifact_content_hash": source_record.content_hash,
            "summary_set_hash": hash_json(summaries),
            "summary_file_path": source_record.path,
            "summary_file_sha256": source_record.content_hash,
            "summary_count": len(summaries),
            "target_entry_locator": "summaries[32] (zero-based; 33rd of 63)",
            "paper_identity": {
                "citation_key": target["paper_info"]["citation_key"],
                "title": target["paper_info"]["title"],
                "doi": target["paper_info"]["doi"],
                "source_pdf_path": str(pdf_path),
            },
            "existing_entry_status": target["status"],
            "existing_source_mode": target["source_mode"],
            "existing_entry_provenance": source_provenance,
        },
        "source_pdf": {"path": str(pdf_path), "sha256": pdf_hash, "page_count": 14},
        "source_field_patches": patches,
        "proposed_study_mapping": studies,
        "source_conflicts": [{
            "proposal_conflict_ref": "PROPOSED_CONFLICT_REF:TRIPATHI2017:EXP2_RS50_RS65",
            "summary_field_path": "ai_summary.core_analysis.methodology",
            "resolution": "UNRESOLVED_KEEP_BOTH_SOURCE_LABEL_AND_ARITHMETIC",
            "pdf_anchor": page_renders[8],
        }],
        "schema_and_review_gates": {
            "new_canonical_source_claim_ids": [],
            "new_canonical_evidence_ids": [],
            "new_provider_receipt_ids": [],
            "new_canonical_study_ids": [],
            "new_canonical_source_field_ids": [],
        },
        "mutations": {
            "source_summary_modified": False,
            "source_pdf_modified": False,
            "registry_written": False,
            "provider_calls": 0,
            "canonical_pointer_advanced": False,
            "candidate_registered": False,
            "production_repair_applied": False,
        },
    }


def _prepare(tmp_path: Path, proposal: dict[str, Any] | None = None) -> tuple[Any, ...]:
    source_workspace, source_registry, source_record, summaries, pdf_path, pdf_hash, page_renders = _build_source(tmp_path)
    dest_workspace = JobWorkspace.create(str(tmp_path / "destination-output"), "candidate-project", "candidate-job")
    dest_registry = ArtifactRegistry(dest_workspace.paths.registry_path, dest_workspace.job_id)
    payload = proposal or _proposal_payload(source_record, summaries, pdf_path, pdf_hash, page_renders)
    source_registry_hash = hashlib.sha256(Path(source_registry.registry_path).read_bytes()).hexdigest()
    source_summary_hash = hashlib.sha256(Path(source_record.path).read_bytes()).hexdigest()
    result = prepare_source_summary_correction_candidate(
        proposal_payload=payload,
        source_registry=source_registry,
        source_artifact_id=source_record.artifact_id,
        destination_workspace=dest_workspace,
        destination_registry=dest_registry,
        publication_context=LocalPublicationContext(),
    )
    return source_workspace, source_registry, source_record, summaries, pdf_path, pdf_hash, page_renders, dest_workspace, dest_registry, result, source_registry_hash, source_summary_hash


def test_prepare_candidate_preserves_all_63_identities_and_stays_quarantined(tmp_path: Path) -> None:
    (
        _source_workspace,
        source_registry,
        source_record,
        source_summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        _destination_workspace,
        destination_registry,
        result,
        source_registry_hash,
        source_summary_hash,
    ) = _prepare(tmp_path)

    candidate_record = destination_registry.get(result.candidate_artifact_id)
    assert candidate_record is not None
    assert candidate_record.status == "quarantined"
    assert candidate_record.artifact_type == CANDIDATE_ARTIFACT_TYPE
    assert candidate_record.artifact_id.startswith("PROPOSED_STAGE1_SUMMARY_CORRECTION_CANDIDATE__")
    candidate = json.loads(Path(candidate_record.path).read_text(encoding="utf-8"))
    assert len(candidate["candidate_summaries"]) == 63
    before_keys = [build_canonical_paper_key(item["paper_info"]) for item in source_summaries]
    after_keys = [build_canonical_paper_key(item["paper_info"]) for item in candidate["candidate_summaries"]]
    assert before_keys == after_keys
    assert candidate["source_identity_list_hash"] == candidate["candidate_identity_list_hash"]
    assert candidate["identity_set_preserved"] is True
    assert candidate["requires_owner_approval_before_adoption"] is True
    assert candidate["usable_as_stage1_reuse"] is False
    assert candidate["canonical_pointer_advanced"] is False
    assert candidate["provider_receipt_ids_for_candidate"] == []
    assert candidate["typed_reuse_manifest_created"] is False
    assert result.status == "ready_for_owner_review"
    assert result.requires_owner_approval is True
    assert result.usable_as_stage1_reuse is False

    target_before = source_summaries[32]
    target_after = candidate["candidate_summaries"][32]
    assert target_before["paper_info"] == target_after["paper_info"]
    assert target_before["provenance"] == target_after["provenance"]
    assert target_before["status"] == target_after["status"]
    assert target_before["source_mode"] == target_after["source_mode"]
    assert target_after["preserved_false"] is False
    assert target_after["preserved_zero"] == 0
    assert target_after["preserved_none"] is None
    for index, (before, after) in enumerate(zip(source_summaries, candidate["candidate_summaries"], strict=True)):
        if index != 32:
            assert before == after

    assert result.source_snapshot_artifact_id.startswith("PROPOSED_STAGE1_SOURCE_SNAPSHOT__")
    assert destination_registry.get(result.source_snapshot_artifact_id).status == "quarantined"
    assert destination_registry.get(result.proposal_artifact_id).status == "quarantined"
    assert destination_registry.get("stage1_summaries") is None
    assert hashlib.sha256(Path(source_registry.registry_path).read_bytes()).hexdigest() == source_registry_hash
    assert hashlib.sha256(Path(source_record.path).read_bytes()).hexdigest() == source_summary_hash
    assert source_registry.get(source_record.artifact_id).content_hash == source_record.content_hash

    verified = verify_source_summary_correction_candidate(
        source_registry=source_registry,
        destination_registry=destination_registry,
        candidate_artifact_id=result.candidate_artifact_id,
    )
    assert verified.verified is True
    assert verified.identity_set_preserved is True
    assert verified.requires_owner_approval is True
    assert verified.usable_as_stage1_reuse is False
    assert verified.canonical_pointer_advanced is False


def test_tripathi_adapter_resolves_cross_study_summary_page_bindings_explicitly(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    result, destination_registry = prepared[9], prepared[8]
    candidate_record = destination_registry.get(result.candidate_artifact_id)
    proposal_record = destination_registry.get(result.proposal_artifact_id)
    assert candidate_record is not None and proposal_record is not None
    candidate = json.loads(Path(candidate_record.path).read_text(encoding="utf-8"))
    proposal = json.loads(Path(proposal_record.path).read_text(encoding="utf-8"))
    normalized = proposal["normalized_proposal"]
    summary_edit = next(edit for edit in normalized["field_edits"] if edit["field_path"] == "ai_summary.core_analysis.summary")
    assert {item["pdf_page"] for item in summary_edit["source_page_bindings"]} >= {5, 8, 10}
    assert any("Experiment 2 binding resolved" in item for item in summary_edit["binding_diagnostics"])
    assert candidate["source_summary_artifact_id"] == proposal["source_authority"]["artifact_id"]
    assert candidate["source_summary_artifact_hash"] == proposal["source_authority"]["artifact_hash"]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        ("stale_hash", "summary_set_hash"),
        ("cross_paper", "canonical paper key"),
        ("tampered_before_hash", "before hash"),
        ("pointer_edit", "unsupported Stage1 content field path"),
        ("missing_page_binding", "page bindings"),
    ],
)
def test_prepare_rejects_stale_crosspaper_tampered_pointer_and_unbound_edits(
    tmp_path: Path,
    mutate: str,
    reason: str,
) -> None:
    source_workspace, source_registry, source_record, summaries, pdf_path, pdf_hash, pages = _build_source(tmp_path)
    destination_workspace = JobWorkspace.create(str(tmp_path / "destination-output"), "candidate-project", "candidate-job")
    destination_registry = ArtifactRegistry(destination_workspace.paths.registry_path, destination_workspace.job_id)
    proposal = _proposal_payload(source_record, summaries, pdf_path, pdf_hash, pages)
    if mutate == "stale_hash":
        proposal["source_authority"]["summary_set_hash"] = "0" * 64
    elif mutate == "cross_paper":
        proposal["source_authority"]["paper_identity"]["doi"] = summaries[0]["paper_info"]["doi"]
        proposal["source_authority"]["paper_identity"]["citation_key"] = summaries[0]["paper_info"]["citation_key"]
    elif mutate == "tampered_before_hash":
        proposal["source_field_patches"][0]["before_value_hash_json"] = "0" * 64
    elif mutate == "pointer_edit":
        patch = _field_patch(
            summaries[32],
            "ai_summary.core_analysis.key_points[3]",
            "should not be allowed",
            ["exp2"],
        )
        patch["canonical_stage1_field_path"] = "paper_info.citation_key"
        patch["before_value"] = summaries[32]["paper_info"]["citation_key"]
        patch["before_value_hash_json"] = hash_json(patch["before_value"])
        proposal["source_field_patches"].append(patch)
    elif mutate == "missing_page_binding":
        proposal["proposed_study_mapping"][1]["page_bindings"] = []

    source_registry_bytes = Path(source_registry.registry_path).read_bytes()
    source_summary_bytes = Path(source_record.path).read_bytes()
    with pytest.raises(SourceSummaryCorrectionError, match=reason):
        prepare_source_summary_correction_candidate(
            proposal_payload=proposal,
            source_registry=source_registry,
            source_artifact_id=source_record.artifact_id,
            destination_workspace=destination_workspace,
            destination_registry=destination_registry,
            publication_context=LocalPublicationContext(),
        )
    assert destination_registry.list_records() == []
    assert Path(source_registry.registry_path).read_bytes() == source_registry_bytes
    assert Path(source_record.path).read_bytes() == source_summary_bytes
    assert source_registry.get(source_record.artifact_id).content_hash == source_record.content_hash


def test_verification_detects_quarantined_candidate_file_tampering(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    source_registry, destination_registry, result = prepared[1], prepared[8], prepared[9]
    candidate_record = destination_registry.get(result.candidate_artifact_id)
    assert candidate_record is not None
    Path(candidate_record.path).write_text("{}", encoding="utf-8")
    with pytest.raises(SourceSummaryCorrectionError, match="candidate artifact bytes were tampered"):
        verify_source_summary_correction_candidate(
            source_registry=source_registry,
            destination_registry=destination_registry,
            candidate_artifact_id=result.candidate_artifact_id,
        )

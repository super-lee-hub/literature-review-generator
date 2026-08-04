from __future__ import annotations

import configparser
import json
from pathlib import Path
from typing import Any

from docx import Document

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec, save_runtime_job_spec
from runtime.orchestrator import AgentRuntimeBridge
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, file_sha256
from services.job_workspace import atomic_write_json
from tests.test_current_runtime_full_e2e import _test_config, _write_pdf
from validation.run_result import (
    ClaimValidationResultV1,
    ClaimVerdict,
    ValidationInputArtifactsV1,
    ValidationRunResultV1,
)


def _write(path: Path, payload: Any) -> Path:
    atomic_write_json(str(path), payload)
    return path


def test_current_control_plane_revalidates_and_promotes_quarantined_repair(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Exercise the real control-plane repair boundary and current validator.

    The fixture creates only the durable inputs needed to model a quarantined
    repair.  Revalidation, receipt closure, versioned DOCX creation, and the
    atomic CurrentArtifactSet switch are all executed by production services.
    """

    # An empty pre-existing environment value prevents the project .env loader
    # from restoring a live key during bootstrap.
    monkeypatch.setenv("LLM_VALIDATOR_API", "")
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    _write_pdf(pdf_dir / "paper.pdf", "Paper", "A bounded finding.")
    job_id = "current-repair-e2e-job"
    workspace_path = tmp_path / f"current-repair-e2e__{job_id}"
    config_path = _test_config(tmp_path)
    config_parser = configparser.ConfigParser()
    config_parser.read(config_path, encoding="utf-8")
    # Keep the configuration schema valid while making the route explicitly
    # inactive.  ``get_validator_api_config`` treats the documented placeholder
    # key as empty, so revalidation stays deterministic and cannot call a
    # provider.
    config_parser["Validator_API"]["api_key"] = "YOUR_VALIDATOR_API_KEY_HERE"
    config_parser["Validator_API"]["model"] = "validator-test"
    with config_path.open("w", encoding="utf-8") as handle:
        config_parser.write(handle)
    spec = RuntimeJobSpec(
        project_name="current-repair-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id=job_id,
        config=str(config_path),
        action="validate_review",
        queue_file=str(tmp_path / "queue.json"),
        workspace_path=str(workspace_path),
        metadata={
            "requested_stages": ["validate"],
            "validation_required": True,
            "require_clean_validation": True,
            "allow_unvalidated_when_validation_optional": False,
        },
    )

    session = AgentRuntimeBridge(spec).bootstrap(
        resume_requested=False,
        claim_latest_pointer=False,
        publish_running_state=False,
    )
    workspace = session.context.workspace
    registry = session.context.registry
    save_runtime_job_spec(workspace.artifact_path("runtime_job_spec_v1.json"), spec)

    claim_text = (
        "A bounded study finding supports the treatment effect "
        "[[cite_ref:R001]]."
    )
    draft_payload = {
        "artifact_type": "review_draft",
        "artifact_version": "v3",
        "created_from_job_id": job_id,
        "created_at": "2026-08-04T00:00:00Z",
        "draft_identity": {"draft_id": "canonical-draft", "project_name": "current-repair-e2e"},
        "generation_context": {"section_count": 1, "mode": "repair-e2e"},
        "content": {
            "sections": [
                        {
                            "section_number": 1,
                            "section_title": "Evidence",
                            "title": "Evidence",
                            "blocks": [
                        {
                            "block_id": "s1_b1",
                            "block_kind": "paragraph",
                            "block_order": 1,
                            "text": claim_text,
                        }
                    ],
                }
            ],
            "references": [],
        },
        "projections": {},
    }
    manifest_payload = {
        "artifact_type": "citation_manifest",
        "artifact_version": "v3",
        "created_from_job_id": job_id,
        "created_at": "2026-08-04T00:00:00Z",
        "manifest_identity": {"manifest_id": "canonical-manifest", "project_name": "current-repair-e2e"},
        "review_reference": {"review_draft_path": "canonical.json", "review_word_path": "canonical.docx"},
        "paper_entries": [{"paper_id": "paper_1", "paper_key": "paper_1"}],
        "occurrences": [
            {
                "occurrence_id": "occ_1",
                    "citation_token": "[[cite_ref:R001]]",
                    "paper_id": "paper_1",
                    "paper_key": "paper_1",
                    "block_id": "s1_b1",
                    "ref_id": "R001",
            }
        ],
        "clusters": [],
        "citation_sets": [
            {
                "bundle_id": "bundle_1",
                "citation_set_key": "paper_1",
                "paper_ids": ["paper_1"],
                "paper_keys": ["paper_1"],
                "occurrence_ids": ["occ_1"],
                "block_ids": ["s1_b1"],
                "section_numbers": [1],
                "section_titles": ["Evidence"],
                    "claim_texts": [claim_text.removesuffix(" [[cite_ref:R001]].") + "."],
                "claim_units": [
                    {
                        "claim_unit_id": "cu_1",
                        "citation_set_key": "paper_1",
                        "block_id": "s1_b1",
                        "sentence_index": 1,
                            "claim_text": claim_text.removesuffix(" [[cite_ref:R001]].") + ".",
                            "citation_tokens": ["[[cite_ref:R001]]"],
                        "paper_ids": ["paper_1"],
                        "supporting_paper_ids": ["paper_1"],
                        "alignment_status": "explicit",
                        "alignment_confidence": 1.0,
                    }
                ],
                    "citation_tokens": ["[[cite_ref:R001]]"],
            }
        ],
        "bibliography": [],
        "review_draft_version": "v3",
        "dependencies": {},
        "render_policy": {"citation_style": "APA7"},
    }
    draft_path = _write(Path(workspace.artifact_path("canonical_draft.json")), draft_payload)
    draft_record = registry.register_file(
        artifact_id="review_draft",
        artifact_role="review_draft",
        artifact_type="review_draft",
        artifact_version="v3",
        path=draft_path,
        producer="tests.current_validation_repair_e2e",
    )
    manifest_path = _write(Path(workspace.artifact_path("canonical_manifest.json")), manifest_payload)
    manifest_record = registry.register_file(
        artifact_id="citation_manifest:v3",
        artifact_role="citation_manifest",
        artifact_type="citation_manifest",
        artifact_version="v3",
        path=manifest_path,
        producer="tests.current_validation_repair_e2e",
        depends_on=[ArtifactDependencyRefV2.from_record(draft_record)],
    )
    docx_path = Path(workspace.artifact_path("canonical.docx"))
    Document().save(str(docx_path))
    registry.register_file(
        artifact_id="review_docx",
        artifact_role="review_docx",
        artifact_type="review_docx",
        artifact_version="v1",
        path=docx_path,
        producer="tests.current_validation_repair_e2e",
        depends_on=[
            ArtifactDependencyRefV2.from_record(draft_record),
            ArtifactDependencyRefV2.from_record(manifest_record),
        ],
    )
    normalized_path = Path(workspace.artifact_path("paper_1.normalized.md"))
    normalized_path.write_text(
        "A bounded study finding supports the treatment effect.",
        encoding="utf-8",
    )
    chunks_path = _write(
        Path(workspace.artifact_path("paper_1.chunks.json")),
        [{"chunk_id": "chunk-1", "text": "A bounded study finding supports the treatment effect."}],
    )
    page_index_path = _write(
        Path(workspace.artifact_path("paper_1.page_index.json")),
        [{"page_number": 1, "text": "A bounded study finding supports the treatment effect."}],
    )
    evidence_path = _write(
        Path(workspace.artifact_path("paper_1.evidence_manifest_v1.json")),
        {
            "artifact_type": "evidence_manifest",
            "artifact_version": "v1",
            "job_id": job_id,
            "canonical_paper_key": "paper_1",
            "created_at": "2026-08-04T00:00:00Z",
            "artifacts": [
                {
                    "artifact_type": "normalized_text",
                    "path": str(normalized_path),
                    "content_hash": file_sha256(normalized_path),
                },
                {
                    "artifact_type": "chunks",
                    "path": str(chunks_path),
                    "content_hash": file_sha256(chunks_path),
                },
                {
                    "artifact_type": "page_index",
                    "path": str(page_index_path),
                    "content_hash": file_sha256(page_index_path),
                },
            ],
        },
    )
    evidence_record = registry.register_file(
        artifact_id="evidence_manifest:paper_1",
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=evidence_path,
        producer="tests.current_validation_repair_e2e",
    )
    paper_path = _write(
        Path(workspace.artifact_path("paper_1.paper_artifact.json")),
        {
            "artifact_type": "paper_artifact",
            "artifact_version": "v1",
            "paper_identity": {"canonical_paper_key": "paper_1", "source_paper_id": "paper_1"},
            "analysis": {
                "ai_summary": {"core_analysis": {"summary": "A bounded study finding supports the treatment effect."}},
                "preprocess": {"normalized_text": "A bounded study finding supports the treatment effect."},
            },
            "stage1_inputs": {
                "evidence_manifest_path": str(evidence_path),
                "evidence_manifest_hash": file_sha256(evidence_path),
                "selected_visual_refs": [],
            },
            "source": {"source_pdf": str(pdf_dir / "paper.pdf")},
        },
    )
    paper_record = registry.register_file(
        artifact_id="paper_artifact:paper_1",
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=paper_path,
        producer="tests.current_validation_repair_e2e",
        depends_on=[ArtifactDependencyRefV2.from_record(evidence_record)],
    )
    canonical_validation = ValidationRunResultV1.create(
        job_id=job_id,
        attempt_id="initial-validation",
        execution_status="succeeded",
        claim_results=(
            ClaimValidationResultV1(
                claim_result_id="claim:canonical",
                claim_unit_ids=("cu_1",),
                citation_set_key="paper_1",
                paper_ids=("paper_1",),
                block_ids=("s1_b1",),
                claim_text="A bounded study finding supports the treatment effect.",
                claim_context="",
                verdict=ClaimVerdict.SUPPORTED,
                reasoning_summary="registered evidence supports the claim",
                repair_hint="",
                root_causes=(),
                span_start=0,
                span_end=51,
                alignment_status="aligned",
                alignment_confidence=1.0,
                low_confidence=False,
                details={"evidence_status": "clean_supported"},
                evidence_candidates=(),
            ),
        ),
        input_artifacts=ValidationInputArtifactsV1(
            review_draft_id=draft_record.artifact_id,
            review_draft_hash=draft_record.content_hash,
            citation_manifest_id=manifest_record.artifact_id,
            citation_manifest_hash=manifest_record.content_hash,
            evidence_manifest_ids=(evidence_record.artifact_id,),
            evidence_manifest_hashes=(evidence_record.content_hash,),
        ),
        expected_claim_count=1,
        review_has_citations=True,
        evidence_complete=True,
    )
    validation_path = _write(
        Path(workspace.artifact_path("validation_run_result_v1.json")),
        canonical_validation.to_dict(),
    )
    registry.register_file(
        artifact_id="validation_run_result",
        artifact_role="validation_run_result",
        artifact_type="validation_run_result",
        artifact_version="v1",
        path=validation_path,
        producer="tests.current_validation_repair_e2e",
        depends_on=[
            ArtifactDependencyRefV2.from_record(draft_record),
            ArtifactDependencyRefV2.from_record(manifest_record),
            ArtifactDependencyRefV2.from_record(evidence_record),
        ],
    )

    derived_draft_path = _write(
        Path(workspace.artifact_path("repair/derived_draft.json")),
        draft_payload,
    )
    derived_draft = registry.register_file(
        artifact_id="review_draft_repaired:current-e2e",
        artifact_role="review_draft_repaired",
        artifact_type="review_draft_repaired",
        artifact_version="v1",
        path=derived_draft_path,
        producer="tests.current_validation_repair_e2e",
        status="quarantined",
    )
    derived_manifest_path = _write(
        Path(workspace.artifact_path("repair/derived_manifest.json")),
        manifest_payload,
    )
    derived_manifest = registry.register_file(
        artifact_id="citation_manifest_repaired:current-e2e",
        artifact_role="citation_manifest_repaired",
        artifact_type="citation_manifest_repaired",
        artifact_version="v1",
        path=derived_manifest_path,
        producer="tests.current_validation_repair_e2e",
        status="quarantined",
    )
    source_payload = {
        "transaction_id": "repair-tx:current-e2e",
        "job_id": job_id,
        "status": "quarantined",
        "applied_artifact_ids": [derived_draft.artifact_id, derived_manifest.artifact_id],
    }
    source_path = _write(Path(workspace.artifact_path("repair/source_transaction.json")), source_payload)
    source = registry.register_file(
        artifact_id="repair-tx:current-e2e",
        artifact_role="repair_transaction",
        artifact_type="repair_transaction",
        artifact_version="v1",
        path=source_path,
        producer="tests.current_validation_repair_e2e",
        status="quarantined",
    )

    result = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1]).repair_promote(
        workspace=workspace.root_dir,
        transaction_id=source.artifact_id,
        actor="tests.current_validation_repair_e2e",
        reason="promote the clean result returned by current-service revalidation",
    )

    assert result["status"] == "promoted", result
    assert result["mutation_performed"] is True
    assert result["canonical_replacement"] is True
    assert result["canonical_paths_unchanged"] is True
    assert result["revalidation_execution_status"] == "succeeded"
    assert result["revalidation_disposition"] == "clean"
    registry.reload()
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.review_draft_artifact_id.startswith("review_draft:v3:repair:")
    assert current_set.citation_manifest_artifact_id.startswith("citation_manifest:v3:repair:")
    assert current_set.validation_run_result_artifact_id.startswith("validation_run_result:v1:repair:")
    assert registry.get("current-artifact-set:pointer") is not None
    promotion = registry.get(result["promotion_transaction_id"])
    assert promotion is not None and promotion.status == "ready"
    assert json.loads(Path(promotion.path).read_text(encoding="utf-8"))["status"] == "promoted"

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping

from services.job_workspace import utc_now_iso


@dataclass(frozen=True)
class PaperArtifactV1:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    paper_identity: Dict[str, Any]
    source: Dict[str, Any]
    paper_info: Dict[str, Any]
    analysis: Dict[str, Any]
    stage1_inputs: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_paper_artifact_v1(
    *,
    job_id: str,
    paper: Mapping[str, Any],
    result: Mapping[str, Any],
    paper_key: str,
) -> PaperArtifactV1:
    source_mode = str(paper.get("source_mode") or result.get("source_mode") or "")
    source_paper_id = str(paper.get("source_paper_id") or paper_key or paper.get("title") or "")
    canonical_paper_key = str(paper.get("canonical_paper_key") or paper_key)
    paper_key_aliases = [
        str(item).strip()
        for item in (paper.get("paper_key_aliases") or [])
        if str(item).strip()
    ]
    if canonical_paper_key and canonical_paper_key not in paper_key_aliases:
        paper_key_aliases.append(canonical_paper_key)

    source_pdf = str(paper.get("source_pdf") or paper.get("pdf_path") or "")
    source_pdf_fingerprint = str(paper.get("source_pdf_fingerprint") or "")
    metadata_confidence = str(paper.get("metadata_confidence") or "")
    metadata_priority_snapshot = [
        str(item).strip()
        for item in (paper.get("metadata_source_priority_snapshot") or [])
        if str(item).strip()
    ]
    stage1_input = dict(result.get("stage1_input") or {})
    preprocess = dict(result.get("preprocess") or {})
    selected_visual_refs = [
        dict(item)
        for item in (stage1_input.get("selected_visual_refs") or [])
        if isinstance(item, dict)
    ]

    return PaperArtifactV1(
        artifact_type="paper_artifact",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        paper_identity={
            "source_paper_id": source_paper_id,
            "canonical_paper_key": canonical_paper_key,
            "paper_key_aliases": paper_key_aliases,
        },
        source={
            "source_mode": source_mode,
            "source_pdf": source_pdf,
            "source_pdf_fingerprint": source_pdf_fingerprint,
            "metadata_confidence": metadata_confidence,
            "metadata_source_priority_snapshot": metadata_priority_snapshot,
        },
        paper_info=dict(paper),
        analysis={
            "status": str(result.get("status") or ""),
            "processing_time": str(result.get("processing_time") or ""),
            "text_length": int(result.get("text_length") or 0),
            "preprocess": dict(result.get("preprocess") or {}),
            "ai_summary": result.get("ai_summary"),
        },
        stage1_inputs={
            "input_mode": str(stage1_input.get("input_mode") or ""),
            "fallback_reason": str(stage1_input.get("fallback_reason") or ""),
            "visual_artifact_manifest_path": str(stage1_input.get("visual_manifest_path") or ""),
            "visual_bundle_path": str(stage1_input.get("visual_bundle_path") or ""),
            "selected_visual_refs": selected_visual_refs,
            "visual_selection_policy_snapshot": dict(stage1_input.get("visual_selection_policy_snapshot") or {}),
            "multimodal_capability": dict(stage1_input.get("multimodal_capability") or {}),
            "selected_text_source": str(preprocess.get("selected_text_source") or ""),
            "stage1_quality_level": str(preprocess.get("stage1_quality_level") or ""),
            "stage1_input_path": str(preprocess.get("stage1_input_path") or ""),
            "stage1_input_manifest_path": str(preprocess.get("stage1_input_manifest_path") or ""),
            "stage1_quality_report_path": str(preprocess.get("stage1_quality_report_path") or ""),
        },
    )

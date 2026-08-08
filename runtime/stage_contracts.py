from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping

from services.source_normalizer import (
    SourcePaperDescriptor,
    normalize_source_papers,
)


def _stable_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaperWorkItem:
    paper_info: Dict[str, Any]
    source_descriptor: Dict[str, Any]
    source_mode: str
    canonical_paper_key: str
    source_paper_id: str
    source_pdf: str

    def validate(self) -> None:
        if self.source_mode not in {"direct", "zotero"}:
            raise ValueError(f"unsupported source mode: {self.source_mode}")
        if not self.canonical_paper_key:
            raise ValueError("PaperWorkItem.canonical_paper_key is required")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PaperWorkItem":
        return cls(
            paper_info=dict(payload.get("paper_info") or {}),
            source_descriptor=dict(payload.get("source_descriptor") or {}),
            source_mode=str(payload.get("source_mode") or ""),
            canonical_paper_key=str(payload.get("canonical_paper_key") or ""),
            source_paper_id=str(payload.get("source_paper_id") or ""),
            source_pdf=str(payload.get("source_pdf") or ""),
        )


@dataclass(frozen=True)
class SourceBundle:
    source_mode: str
    project_name: str
    paper_work_items: List[PaperWorkItem]
    source_snapshot: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.source_mode not in {"direct", "zotero"}:
            raise ValueError(f"unsupported source mode: {self.source_mode}")
        if not self.project_name:
            raise ValueError("SourceBundle.project_name is required")
        for item in self.paper_work_items:
            item.validate()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_mode": self.source_mode,
            "project_name": self.project_name,
            "paper_work_items": [item.to_dict() for item in self.paper_work_items],
            "source_snapshot": dict(self.source_snapshot),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceBundle":
        return cls(
            source_mode=str(payload.get("source_mode") or ""),
            project_name=str(payload.get("project_name") or ""),
            paper_work_items=[
                PaperWorkItem.from_dict(item)
                for item in (payload.get("paper_work_items") or [])
                if isinstance(item, Mapping)
            ],
            source_snapshot=dict(payload.get("source_snapshot") or {}),
        )

    def fingerprint(self) -> str:
        return _stable_hash(self.to_dict())


@dataclass(frozen=True)
class StageArtifactRef:
    artifact_role: str
    artifact_type: str
    artifact_version: str
    path: str
    artifact_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageArtifactRef":
        return cls(
            artifact_role=str(payload.get("artifact_role") or ""),
            artifact_type=str(payload.get("artifact_type") or ""),
            artifact_version=str(payload.get("artifact_version") or ""),
            path=str(payload.get("path") or ""),
            artifact_id=str(payload.get("artifact_id") or ""),
        )


@dataclass(frozen=True)
class StageResult:
    stage_name: str
    success: bool
    artifacts: List[StageArtifactRef] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.stage_name:
            raise ValueError("StageResult.stage_name is required")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "success": self.success,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": dict(self.metadata),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageResult":
        return cls(
            stage_name=str(payload.get("stage_name") or ""),
            success=bool(payload.get("success", False)),
            artifacts=[
                StageArtifactRef.from_dict(item)
                for item in (payload.get("artifacts") or [])
                if isinstance(item, Mapping)
            ],
            metadata=dict(payload.get("metadata") or {}),
            warnings=[str(item) for item in (payload.get("warnings") or [])],
        )


def build_source_bundle(
    *,
    source_mode: str,
    project_name: str,
    papers: Iterable[Mapping[str, Any]],
    source_snapshot: Mapping[str, Any] | None = None,
) -> SourceBundle:
    raw_papers = [dict(item) for item in papers]
    descriptors: List[SourcePaperDescriptor] = normalize_source_papers(source_mode, raw_papers)
    work_items = [
        PaperWorkItem(
            paper_info={
                **dict(paper),
                "source_mode": descriptor.source_mode,
                "source_paper_id": descriptor.source_paper_id,
                "canonical_paper_key": descriptor.canonical_paper_key,
                "paper_key_aliases": list(descriptor.paper_key_aliases),
                "source_pdf": descriptor.source_pdf,
                "source_pdf_fingerprint": descriptor.source_pdf_fingerprint,
                "metadata_confidence": descriptor.metadata_confidence,
                "metadata_source_priority_snapshot": list(descriptor.metadata_source_priority_snapshot),
                "source_descriptor": descriptor.to_dict(),
            },
            source_descriptor=descriptor.to_dict(),
            source_mode=descriptor.source_mode,
            canonical_paper_key=descriptor.canonical_paper_key,
            source_paper_id=descriptor.source_paper_id,
            source_pdf=descriptor.source_pdf,
        )
        for paper, descriptor in zip(raw_papers, descriptors)
    ]
    bundle = SourceBundle(
        source_mode=source_mode,
        project_name=project_name,
        paper_work_items=work_items,
        source_snapshot=dict(source_snapshot or {}),
    )
    bundle.validate()
    return bundle

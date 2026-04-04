from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from services.job_workspace import utc_now_iso


@dataclass(frozen=True)
class CitationSpan:
    span_id: str
    start_offset: int
    end_offset: int
    text: str
    anchor_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationSpan":
        return cls(**data)


@dataclass(frozen=True)
class CitationOccurrence:
    occurrence_id: str
    citation_token: str
    paper_id: str
    section_number: int
    section_title: str
    block_id: str
    block_order: int
    spans: List[CitationSpan] = field(default_factory=list)
    context_before: str = ""
    context_after: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "citation_token": self.citation_token,
            "paper_id": self.paper_id,
            "section_number": self.section_number,
            "section_title": self.section_title,
            "block_id": self.block_id,
            "block_order": self.block_order,
            "spans": [span.to_dict() for span in self.spans],
            "context_before": self.context_before,
            "context_after": self.context_after,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationOccurrence":
        return cls(
            occurrence_id=data["occurrence_id"],
            citation_token=data["citation_token"],
            paper_id=data["paper_id"],
            section_number=data["section_number"],
            section_title=data["section_title"],
            block_id=data["block_id"],
            block_order=data["block_order"],
            spans=[CitationSpan.from_dict(s) for s in data.get("spans", [])],
            context_before=data.get("context_before", ""),
            context_after=data.get("context_after", ""),
        )


@dataclass(frozen=True)
class CitationCluster:
    cluster_id: str
    paper_id: str
    paper_key: str
    occurrence_ids: List[str] = field(default_factory=list)
    first_occurrence_section: int = 0
    total_occurrences: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationCluster":
        return cls(**data)


@dataclass(frozen=True)
class BibliographyEntry:
    entry_id: str
    paper_id: str
    paper_key: str
    citation_text: str
    is_cited: bool = True
    cluster_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BibliographyEntry":
        return cls(**data)


@dataclass(frozen=True)
class CitationManifestV1:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    citations: Sequence[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CitationManifestV2:
    artifact_type: str
    artifact_version: str
    created_from_job_id: str
    created_at: str
    manifest_identity: Dict[str, Any]
    review_reference: Dict[str, Any]
    occurrences: List[CitationOccurrence] = field(default_factory=list)
    clusters: List[CitationCluster] = field(default_factory=list)
    bibliography: List[BibliographyEntry] = field(default_factory=list)
    review_draft_version: str = "v2"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "created_from_job_id": self.created_from_job_id,
            "created_at": self.created_at,
            "manifest_identity": self.manifest_identity,
            "review_reference": self.review_reference,
            "occurrences": [occ.to_dict() for occ in self.occurrences],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "bibliography": [entry.to_dict() for entry in self.bibliography],
            "review_draft_version": self.review_draft_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CitationManifestV2":
        return cls(
            artifact_type=data["artifact_type"],
            artifact_version=data["artifact_version"],
            created_from_job_id=data["created_from_job_id"],
            created_at=data["created_at"],
            manifest_identity=data["manifest_identity"],
            review_reference=data["review_reference"],
            occurrences=[CitationOccurrence.from_dict(o) for o in data.get("occurrences", [])],
            clusters=[CitationCluster.from_dict(c) for c in data.get("clusters", [])],
            bibliography=[BibliographyEntry.from_dict(b) for b in data.get("bibliography", [])],
            review_draft_version=data.get("review_draft_version", "v2"),
        )

    def get_cited_bibliography(self) -> List[BibliographyEntry]:
        return [entry for entry in self.bibliography if entry.is_cited]

    def get_occurrences_for_paper(self, paper_id: str) -> List[CitationOccurrence]:
        return [occ for occ in self.occurrences if occ.paper_id == paper_id]

    def get_cluster_for_paper(self, paper_id: str) -> Optional[CitationCluster]:
        for cluster in self.clusters:
            if cluster.paper_id == paper_id:
                return cluster
        return None


def build_citation_manifest_v1(
    *, 
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    citations: Sequence[Dict[str, Any]],
) -> CitationManifestV1:
    normalized_citations = [
        {
            "citation_id": str(citation.get("citation_id") or ""),
            "paper_id": str(citation.get("paper_id") or ""),
            "text": str(citation.get("text") or "").strip(),
            "context": str(citation.get("context") or "").strip(),
            "section_number": int(citation.get("section_number") or 0),
            "section_title": str(citation.get("section_title") or "").strip(),
            "block_id": str(citation.get("block_id") or ""),
            "block_order": int(citation.get("block_order") or 0),
            "review_draft_version": str(citation.get("review_draft_version") or "v2"),
        }
        for citation in citations
    ]

    return CitationManifestV1(
        artifact_type="citation_manifest",
        artifact_version="v1",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        citations=normalized_citations,
    )


def build_citation_manifest_v2(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft_version: str = "v2",
    occurrences: Optional[List[CitationOccurrence]] = None,
    clusters: Optional[List[CitationCluster]] = None,
    bibliography: Optional[List[BibliographyEntry]] = None,
) -> CitationManifestV2:
    return CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id=job_id,
        created_at=utc_now_iso(),
        manifest_identity={
            "manifest_id": manifest_id,
            "project_name": project_name,
            "scope": "review_citations_truth_source",
        },
        review_reference={
            "review_draft_path": review_draft_path,
            "review_word_path": review_word_path,
        },
        occurrences=occurrences or [],
        clusters=clusters or [],
        bibliography=bibliography or [],
        review_draft_version=review_draft_version,
    )


def migrate_v1_to_v2(v1_manifest: CitationManifestV1) -> CitationManifestV2:
    occurrences: List[CitationOccurrence] = []
    bibliography: List[BibliographyEntry] = []
    clusters: List[CitationCluster] = []

    paper_occurrence_map: Dict[str, List[str]] = {}

    for idx, citation in enumerate(v1_manifest.citations):
        paper_id = str(citation.get("paper_id", f"paper_{idx}"))
        occurrence_id = f"occ_{idx}_{paper_id}"
        
        occurrence = CitationOccurrence(
            occurrence_id=occurrence_id,
            citation_token=str(citation.get("text", "")),
            paper_id=paper_id,
            section_number=int(citation.get("section_number", 0)),
            section_title=str(citation.get("section_title", "")),
            block_id=str(citation.get("block_id", f"block_{idx}")),
            block_order=int(citation.get("block_order", idx + 1)),
            spans=[],
            context_before=str(citation.get("context", "")),
            context_after="",
        )
        occurrences.append(occurrence)

        if paper_id not in paper_occurrence_map:
            paper_occurrence_map[paper_id] = []
        paper_occurrence_map[paper_id].append(occurrence_id)

        entry_exists = any(entry.paper_id == paper_id for entry in bibliography)
        if not entry_exists:
            bibliography.append(BibliographyEntry(
                entry_id=f"bib_{paper_id}",
                paper_id=paper_id,
                paper_key=paper_id,
                citation_text=str(citation.get("text", "")),
                is_cited=True,
                cluster_id=None,
            ))

    for paper_id, occ_ids in paper_occurrence_map.items():
        first_section = min(
            (occ.section_number for occ in occurrences if occ.paper_id == paper_id),
            default=0
        )
        clusters.append(CitationCluster(
            cluster_id=f"cluster_{paper_id}",
            paper_id=paper_id,
            paper_key=paper_id,
            occurrence_ids=occ_ids,
            first_occurrence_section=first_section,
            total_occurrences=len(occ_ids),
        ))

    return CitationManifestV2(
        artifact_type="citation_manifest",
        artifact_version="v2",
        created_from_job_id=v1_manifest.created_from_job_id,
        created_at=v1_manifest.created_at,
        manifest_identity={
            **v1_manifest.manifest_identity,
            "migrated_from": "v1",
        },
        review_reference=v1_manifest.review_reference,
        occurrences=occurrences,
        clusters=clusters,
        bibliography=bibliography,
        review_draft_version="v2",
    )

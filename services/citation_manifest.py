from __future__ import annotations

import re
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
    paper_key: str
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
            "paper_key": self.paper_key,
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
            paper_key=data.get("paper_key", data["paper_id"]),
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

    def get_occurrences_for_paper(self, paper_identifier: str) -> List[CitationOccurrence]:
        return [
            occ for occ in self.occurrences
            if occ.paper_id == paper_identifier or occ.paper_key == paper_identifier
        ]

    def get_cluster_for_paper(self, paper_identifier: str) -> Optional[CitationCluster]:
        for cluster in self.clusters:
            if cluster.paper_id == paper_identifier or cluster.paper_key == paper_identifier:
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
            paper_key=paper_id,
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


def build_citation_manifest_v2_from_review_draft(
    *,
    job_id: str,
    project_name: str,
    manifest_id: str,
    review_draft_path: str,
    review_word_path: str,
    review_draft_v2: Dict[str, Any],
    paper_summaries: List[Dict[str, Any]],
) -> CitationManifestV2:
    """Build CitationManifestV2 directly from review_draft_v2 block structure.
    
    This creates occurrence/cluster/bibliography truth from the review draft blocks,
    not just from the final reference list. It extracts citation information from
    the block structure and correlates it with paper summaries.
    """
    occurrences: List[CitationOccurrence] = []
    clusters: List[CitationCluster] = []
    bibliography: List[BibliographyEntry] = []
    
    # Build paper key to info mapping from summaries
    paper_key_to_info: Dict[str, Dict[str, Any]] = {}
    for summary in paper_summaries:
        paper_info = summary.get('paper_info', {})
        # Use title as canonical key if available
        title = paper_info.get('title', '')
        if title:
            paper_key_to_info[title.lower()] = {
                'paper_id': title.lower(),
                'paper_key': title.lower(),
                'authors': paper_info.get('authors', []),
                'year': paper_info.get('year', ''),
            }
    
    # Extract occurrences from review_draft_v2 sections/blocks
    sections = review_draft_v2.get('content', {}).get('sections', [])
    references = review_draft_v2.get('content', {}).get('references', [])
    
    occurrence_counter = 0
    paper_occurrence_map: Dict[str, List[str]] = {}
    
    for section in sections:
        section_number = section.get('section_number', 0)
        section_title = section.get('section_title', '')
        blocks = section.get('blocks', [])
        
        for block in blocks:
            block_id = block.get('block_id', f's{section_number}_b0')
            block_order = block.get('block_order', 0)
            block_text = block.get('text', '')
            
            # Simple citation extraction: look for patterns like (Author, YYYY)
            # This is a basic implementation - can be enhanced with more sophisticated parsing
            citation_pattern = r'\([^)]+,\s*\d{4}[^)]*\)'
            found_citations = re.findall(citation_pattern, block_text)
            
            for citation_token in found_citations:
                occurrence_counter += 1
                occurrence_id = f"occ_{occurrence_counter}"
                
                # Try to match citation to paper
                paper_id = "unknown"
                paper_key = "unknown"
                
                # Simple heuristic: check if any paper title or author appears in citation
                citation_lower = citation_token.lower()
                for title_key, paper_data in paper_key_to_info.items():
                    # Check if author names from paper appear in citation
                    authors = paper_data.get('authors', [])
                    for author in authors:
                        if author.lower() in citation_lower:
                            paper_id = paper_data['paper_id']
                            paper_key = paper_data['paper_key']
                            break
                    if paper_id != "unknown":
                        break
                
                # Create occurrence
                occurrence = CitationOccurrence(
                    occurrence_id=occurrence_id,
                    citation_token=citation_token,
                    paper_id=paper_id,
                    paper_key=paper_key,
                    section_number=section_number,
                    section_title=section_title,
                    block_id=block_id,
                    block_order=block_order,
                    spans=[],
                    context_before=block_text[:200] if len(block_text) > 200 else block_text,
                    context_after="",
                )
                occurrences.append(occurrence)
                
                if paper_id not in paper_occurrence_map:
                    paper_occurrence_map[paper_id] = []
                paper_occurrence_map[paper_id].append(occurrence_id)
    
    # Build clusters from occurrence map
    for paper_id, occ_ids in paper_occurrence_map.items():
        if paper_id == "unknown":
            continue
            
        # Find first occurrence section
        first_section = min(
            (occ.section_number for occ in occurrences if occ.paper_id == paper_id),
            default=0
        )
        
        cluster = CitationCluster(
            cluster_id=f"cluster_{paper_id}",
            paper_id=paper_id,
            paper_key=paper_id,
            occurrence_ids=occ_ids,
            first_occurrence_section=first_section,
            total_occurrences=len(occ_ids),
        )
        clusters.append(cluster)
    
    # Build bibliography from references and cited papers
    cited_paper_ids = set(paper_occurrence_map.keys())
    
    for idx, ref in enumerate(references):
        entry_id = f"bib_{idx}"
        
        # Try to find if this reference corresponds to a cited paper
        ref_lower = ref.lower()
        matched_paper_id = "unknown"
        cluster_id = None
        
        for title_key, paper_data in paper_key_to_info.items():
            if title_key in ref_lower or any(
                author.lower() in ref_lower for author in paper_data.get('authors', [])
            ):
                matched_paper_id = paper_data['paper_id']
                if matched_paper_id in cited_paper_ids:
                    cluster_id = f"cluster_{matched_paper_id}"
                break
        
        is_cited = matched_paper_id in cited_paper_ids
        
        entry = BibliographyEntry(
            entry_id=entry_id,
            paper_id=matched_paper_id,
            paper_key=matched_paper_id,
            citation_text=ref,
            is_cited=is_cited,
            cluster_id=cluster_id,
        )
        bibliography.append(entry)
    
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
        occurrences=occurrences,
        clusters=clusters,
        bibliography=bibliography,
        review_draft_version="v2",
    )

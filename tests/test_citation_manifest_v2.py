from services.citation_manifest import (
    CitationManifestV1,
    CitationManifestV2,
    CitationOccurrence,
    CitationCluster,
    BibliographyEntry,
    CitationSpan,
    build_citation_manifest_v1,
    build_citation_manifest_v2,
    migrate_v1_to_v2,
)
from services.job_workspace import utc_now_iso


def test_citation_span_roundtrip() -> None:
    span = CitationSpan(
        span_id="span_1",
        start_offset=0,
        end_offset=20,
        text="(Author, 2024)",
        anchor_hash="abc123",
    )
    data = span.to_dict()
    restored = CitationSpan.from_dict(data)
    assert restored.span_id == "span_1"
    assert restored.start_offset == 0
    assert restored.end_offset == 20
    assert restored.text == "(Author, 2024)"


def test_citation_occurrence_roundtrip() -> None:
    span = CitationSpan(span_id="s1", start_offset=0, end_offset=10, text="test")
    occurrence = CitationOccurrence(
        occurrence_id="occ_1",
        citation_token="(Author, 2024)",
        paper_id="paper_123",
        paper_key="paper_123",
        section_number=1,
        section_title="Introduction",
        block_id="s1_b1",
        block_order=1,
        spans=[span],
        context_before="As discussed in",
        context_after="this is important.",
    )
    data = occurrence.to_dict()
    restored = CitationOccurrence.from_dict(data)
    assert restored.occurrence_id == "occ_1"
    assert restored.citation_token == "(Author, 2024)"
    assert restored.paper_id == "paper_123"
    assert len(restored.spans) == 1


def test_citation_manifest_v2_basic() -> None:
    span = CitationSpan(span_id="s1", start_offset=0, end_offset=10, text="(A, 2024)")
    occurrence = CitationOccurrence(
        occurrence_id="occ_1",
        citation_token="(A, 2024)",
        paper_id="paper_a",
        paper_key="paper_a",
        section_number=1,
        section_title="Intro",
        block_id="b1",
        block_order=1,
        spans=[span],
    )
    cluster = CitationCluster(
        cluster_id="cluster_a",
        paper_id="paper_a",
        paper_key="paper_a",
        occurrence_ids=["occ_1"],
        first_occurrence_section=1,
        total_occurrences=1,
    )
    bib_entry = BibliographyEntry(
        entry_id="bib_a",
        paper_id="paper_a",
        paper_key="paper_a",
        citation_text="Author, A. (2024). Title.",
        is_cited=True,
        cluster_id="cluster_a",
    )
    
    manifest = build_citation_manifest_v2(
        job_id="job_123",
        project_name="test_proj",
        manifest_id="citation_manifest:v2",
        review_draft_path="/path/draft.json",
        review_word_path="/path/review.docx",
        occurrences=[occurrence],
        clusters=[cluster],
        bibliography=[bib_entry],
    )
    
    assert manifest.artifact_type == "citation_manifest"
    assert manifest.artifact_version == "v2"
    assert len(manifest.occurrences) == 1
    assert len(manifest.clusters) == 1
    assert len(manifest.bibliography) == 1


def test_get_cited_bibliography() -> None:
    entry1 = BibliographyEntry(
        entry_id="e1", paper_id="p1", paper_key="p1",
        citation_text="A (2024)", is_cited=True
    )
    entry2 = BibliographyEntry(
        entry_id="e2", paper_id="p2", paper_key="p2",
        citation_text="B (2024)", is_cited=False
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="", review_word_path="",
        bibliography=[entry1, entry2],
    )
    
    cited = manifest.get_cited_bibliography()
    assert len(cited) == 1
    assert cited[0].paper_id == "p1"


def test_get_occurrences_for_paper() -> None:
    occ1 = CitationOccurrence(
        occurrence_id="o1", citation_token="t1", paper_id="p1", paper_key="p1",
        section_number=1, section_title="", block_id="", block_order=1,
    )
    occ2 = CitationOccurrence(
        occurrence_id="o2", citation_token="t2", paper_id="p2", paper_key="p2",
        section_number=2, section_title="", block_id="", block_order=1,
    )
    occ3 = CitationOccurrence(
        occurrence_id="o3", citation_token="t3", paper_id="p1", paper_key="p1",
        section_number=3, section_title="", block_id="", block_order=1,
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="", review_word_path="",
        occurrences=[occ1, occ2, occ3],
    )
    
    p1_occs_by_id = manifest.get_occurrences_for_paper("p1")
    assert len(p1_occs_by_id) == 2
    assert {o.occurrence_id for o in p1_occs_by_id} == {"o1", "o3"}
    
    p1_occs_by_key = manifest.get_occurrences_for_paper("p1")
    assert len(p1_occs_by_key) == 2
    assert {o.occurrence_id for o in p1_occs_by_key} == {"o1", "o3"}


def test_get_occurrences_for_paper_with_different_id_and_key() -> None:
    occ = CitationOccurrence(
        occurrence_id="o1", citation_token="t1",
        paper_id="internal_id_123", paper_key="user_friendly_key",
        section_number=1, section_title="", block_id="", block_order=1,
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="", review_word_path="",
        occurrences=[occ],
    )
    
    by_id = manifest.get_occurrences_for_paper("internal_id_123")
    assert len(by_id) == 1
    assert by_id[0].occurrence_id == "o1"
    
    by_key = manifest.get_occurrences_for_paper("user_friendly_key")
    assert len(by_key) == 1
    assert by_key[0].occurrence_id == "o1"


def test_get_cluster_for_paper() -> None:
    cluster = CitationCluster(
        cluster_id="c1", paper_id="p1", paper_key="p1",
        occurrence_ids=["o1"], first_occurrence_section=1, total_occurrences=1,
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="", review_word_path="",
        clusters=[cluster],
    )
    
    found_by_id = manifest.get_cluster_for_paper("p1")
    assert found_by_id is not None
    assert found_by_id.cluster_id == "c1"
    
    found_by_key = manifest.get_cluster_for_paper("p1")
    assert found_by_key is not None
    assert found_by_key.cluster_id == "c1"
    
    not_found = manifest.get_cluster_for_paper("nonexistent")
    assert not_found is None


def test_get_cluster_for_paper_with_different_id_and_key() -> None:
    cluster = CitationCluster(
        cluster_id="c1",
        paper_id="internal_id_456",
        paper_key="another_friendly_key",
        occurrence_ids=["o1"],
        first_occurrence_section=1,
        total_occurrences=1,
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="", review_word_path="",
        clusters=[cluster],
    )
    
    by_id = manifest.get_cluster_for_paper("internal_id_456")
    assert by_id is not None
    assert by_id.cluster_id == "c1"
    
    by_key = manifest.get_cluster_for_paper("another_friendly_key")
    assert by_key is not None
    assert by_key.cluster_id == "c1"


def test_migrate_v1_to_v2() -> None:
    v1 = build_citation_manifest_v1(
        job_id="job_v1",
        project_name="test_migrate",
        manifest_id="manifest_v1",
        review_draft_path="/old/path.json",
        review_word_path="/old/review.docx",
        citations=[
            {
                "text": "Author, A. (2024). Paper 1.",
                "context": "In the introduction",
                "section_number": 1,
                "section_title": "Introduction",
                "paper_id": "paper_1",
            },
            {
                "text": "Author, A. (2024). Paper 1.",
                "context": "In the discussion",
                "section_number": 3,
                "section_title": "Discussion",
                "paper_id": "paper_1",
            },
            {
                "text": "Author, B. (2023). Paper 2.",
                "context": "Related work",
                "section_number": 2,
                "section_title": "Related Work",
                "paper_id": "paper_2",
            },
        ],
    )
    
    v2 = migrate_v1_to_v2(v1)
    
    assert v2.artifact_version == "v2"
    assert v2.created_from_job_id == "job_v1"
    assert v2.manifest_identity.get("migrated_from") == "v1"
    
    assert len(v2.occurrences) == 3
    assert len(v2.clusters) == 2
    assert len(v2.bibliography) == 2
    
    cluster1 = v2.get_cluster_for_paper("paper_1")
    assert cluster1 is not None
    assert cluster1.total_occurrences == 2
    assert cluster1.first_occurrence_section == 1
    
    cited = v2.get_cited_bibliography()
    assert len(cited) == 2


def test_citation_manifest_v2_to_dict_and_from_dict() -> None:
    span = CitationSpan(span_id="s1", start_offset=0, end_offset=10, text="test")
    occurrence = CitationOccurrence(
        occurrence_id="occ_1", citation_token="(A, 2024)", paper_id="p1", paper_key="p1",
        section_number=1, section_title="Intro", block_id="b1", block_order=1, spans=[span],
    )
    
    manifest = build_citation_manifest_v2(
        job_id="j1", project_name="p", manifest_id="m",
        review_draft_path="d", review_word_path="w",
        occurrences=[occurrence],
    )
    
    data = manifest.to_dict()
    restored = CitationManifestV2.from_dict(data)
    
    assert restored.artifact_type == manifest.artifact_type
    assert restored.artifact_version == manifest.artifact_version
    assert len(restored.occurrences) == 1
    assert restored.occurrences[0].occurrence_id == "occ_1"

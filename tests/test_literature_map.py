"""Tests for literature map builder."""

import pytest
from outline.literature_map import build_literature_map, _extract_paper_node
from outline.v2_models import LiteratureMap, PaperNode


def _sample_summaries():
    return [
        {
            "paper_info": {
                "title": "Machine Learning in Healthcare",
                "authors": ["Smith, J.", "Jones, K."],
                "year": 2023,
                "classification": "core",
                "must_use": "true",
            },
            "themes": ["healthcare_ai", "clinical_decision_support"],
            "methods": ["systematic_review", "meta_analysis"],
            "abstract": "A comprehensive review of ML applications in clinical settings.",
        },
        {
            "paper_info": {
                "title": "Deep Learning for Medical Imaging",
                "authors": ["Chen, L."],
                "year": 2022,
                "classification": "support",
            },
            "themes": ["medical_imaging", "deep_learning"],
            "methods": ["convolutional_neural_networks"],
            "abstract": "Survey of deep learning methods for radiology.",
        },
        {
            "title": "Minimal Paper",
            "authors": "Wang, X.",
        },
    ]


class TestLiteratureMap:

    def test_build_map_from_summaries(self):
        summaries = _sample_summaries()
        lit_map = build_literature_map(summaries, "job-001")

        assert isinstance(lit_map, LiteratureMap)
        assert lit_map.artifact_type == "literature_map"
        assert lit_map.artifact_version == "v1"
        assert lit_map.created_from_job_id == "job-001"
        assert len(lit_map.paper_nodes) == 3

    def test_every_summary_becomes_paper_node_or_diagnostic(self):
        summaries = _sample_summaries()
        lit_map = build_literature_map(summaries, "job-001")

        assert len(lit_map.paper_nodes) == 3
        assert len(lit_map.source_summary_hashes) == 3

    def test_missing_fields_become_diagnostics_not_fabricated(self):
        summaries = [{"title": "Only Title"}]
        lit_map = build_literature_map(summaries, "job-001")

        node = lit_map.paper_nodes[0]
        assert node.title == "Only Title"
        assert len(node.authors) == 0
        assert node.year is None
        assert len(node.diagnostics) > 0
        assert any("missing_authors" in d for d in node.diagnostics)
        assert any("missing_year" in d for d in node.diagnostics)

    def test_paper_classification_is_present(self):
        summaries = _sample_summaries()
        lit_map = build_literature_map(summaries, "job-001")

        assert "core" in lit_map.paper_classification
        assert "support" in lit_map.paper_classification
        assert len(lit_map.paper_classification["core"]) >= 1

    def test_blocking_diagnostics_for_invalid_summary(self):
        summaries = ["not a dict", None, 42]
        lit_map = build_literature_map(summaries, "job-001")  # type: ignore
        assert len(lit_map.blocking_diagnostics) > 0

    def test_paper_node_has_stable_key(self):
        node = _extract_paper_node({"title": "Test", "authors": ["A"]}, 0)
        assert node.paper_key.startswith("paper_")
        assert node.paper_key.endswith("_000")

    def test_core_paper_is_must_use(self):
        node = _extract_paper_node({
            "title": "Core Paper",
            "paper_info": {"classification": "core"},
        }, 0)
        assert node.must_use is True
        assert node.classification == "core"

    def test_source_hashes_populated(self):
        summaries = _sample_summaries()
        lit_map = build_literature_map(summaries, "job-001")
        assert len(lit_map.source_summary_hashes) == 3
        for h in lit_map.source_summary_hashes:
            assert len(h) == 16

    def test_research_streams_from_themes(self):
        summaries = _sample_summaries()
        lit_map = build_literature_map(summaries, "job-001")
        assert len(lit_map.research_streams) > 0
        for stream in lit_map.research_streams:
            assert "stream_name" in stream
            assert "paper_keys" in stream

    def test_paper_artifacts_integration(self):
        summaries = _sample_summaries()
        artifacts = [{"title": "Artifact Paper", "authors": ["Extra, A."]}]
        lit_map = build_literature_map(summaries, "job-001", paper_artifacts=artifacts)
        assert len(lit_map.paper_nodes) == 4

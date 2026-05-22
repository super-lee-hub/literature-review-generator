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
        assert node.paper_key.startswith("source:")
        assert node.canonical_paper_key == node.paper_key
        assert node.identity_source == "source_hash"

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

    def test_real_stage1_schema_extracts_outline_signals(self):
        summaries = [
            {
                "paper_info": {
                    "title": "Promotion Fairness in Digital Retail",
                    "authors": ["Alice Smith"],
                    "year": "2024",
                    "doi": "https://doi.org/10.1000/Fairness",
                },
                "ai_summary": {
                    "paper_metadata": {
                        "title": "Promotion Fairness in Digital Retail",
                        "authors": ["Alice Smith"],
                        "year": "2024",
                        "doi": "10.1000/fairness",
                    },
                    "routing": {
                        "paper_type": "empirical",
                        "paper_subtype_normalized": "experiment",
                        "classification_status": "resolved",
                        "route_confidence": "high",
                    },
                    "core_analysis": {
                        "summary": "Consumers evaluate promotion fairness across digital retail contexts.",
                        "key_points": ["promotion fairness", "consumer trust"],
                        "methodology": "survey experiment",
                        "findings": "Fairness increases consumer trust.",
                        "limitations": "Single-country sample.",
                        "theoretical_framework": "equity theory",
                        "research_gap": "Limited longitudinal evidence.",
                    },
                    "specialized_details": {
                        "empirical": {
                            "analysis_technique": "structural equation modeling",
                            "core_variables": {
                                "independent": ["promotion fairness"],
                                "dependent": ["consumer trust"],
                                "mediators": ["perceived value"],
                            },
                            "sample_characteristics_or_context": "digital retail shoppers",
                        }
                    },
                    "quality_audit": {"extraction_confidence": "high"},
                },
            }
        ]

        lit_map = build_literature_map(summaries, "job-real-schema")
        node = lit_map.paper_nodes[0]

        assert node.paper_key == "10.1000/fairness"
        assert "promotion fairness" in node.themes
        assert "survey experiment" in node.methods
        assert "equity theory" in node.theories
        assert "consumer trust" in node.variables
        assert node.gaps == ["Limited longitudinal evidence."]
        assert node.limitations == ["Single-country sample."]
        assert node.findings == ["Fairness increases consumer trust."]
        assert node.source_records[0]["metadata_confidence"] == "high"

    def test_summary_and_paper_artifact_merge_to_one_canonical_node_with_sources(self):
        summary = {
            "paper_info": {
                "title": "Shared Paper",
                "authors": ["Jane Doe"],
                "year": 2023,
                "doi": "DOI:10.5555/SHARED.",
            },
            "themes": ["consumer trust"],
            "methods": ["survey"],
        }
        paper_artifact = {
            "artifact_type": "paper_artifact",
            "paper_identity": {
                "canonical_paper_key": "10.5555/shared",
                "paper_key_aliases": ["shared-alias"],
            },
            "source": {"metadata_confidence": "high"},
            "paper_info": {
                "title": "Shared Paper",
                "authors": ["Jane Doe"],
                "year": 2023,
                "doi": "https://doi.org/10.5555/shared",
            },
            "analysis": {
                "ai_summary": {
                    "core_analysis": {"key_points": ["consumer trust"], "methodology": "experiment"},
                    "quality_audit": {"extraction_confidence": "high"},
                }
            },
        }

        lit_map = build_literature_map([summary], "job-merge", paper_artifacts=[paper_artifact])

        assert len(lit_map.paper_nodes) == 1
        node = lit_map.paper_nodes[0]
        assert node.paper_key == "10.5555/shared"
        assert {record["source_type"] for record in node.source_records} == {"summary", "paper_artifact"}
        assert "shared-alias" in node.aliases
        assert "survey" in node.methods
        assert "experiment" in node.methods

    def test_suspicious_same_title_different_doi_blocks_map(self):
        summaries = [
            {
                "paper_info": {
                    "title": "Ambiguous Promotion Study",
                    "authors": ["A"],
                    "year": 2020,
                    "canonical_paper_key": "ambiguous-promotion-study",
                    "doi": "10.1000/a",
                }
            }
        ]
        artifacts = [
            {
                "paper_identity": {"canonical_paper_key": "ambiguous-promotion-study"},
                "paper_info": {
                    "title": "Ambiguous Promotion Study",
                    "authors": ["A"],
                    "year": 2020,
                    "doi": "10.1000/b",
                },
            }
        ]

        lit_map = build_literature_map(summaries, "job-suspicious", paper_artifacts=artifacts)

        assert any(
            d["type"] == "suspicious_merge_same_title_different_doi"
            for d in lit_map.blocking_diagnostics
        )


def test_title_author_year_identity_uses_author_surname_before_comma():
    lit_map = build_literature_map(
        [
            {
                "paper_info": {
                    "title": "Machine Learning in Healthcare",
                    "authors": ["Smith, J."],
                    "year": 2023,
                }
            }
        ],
        "job-author-surname",
    )

    node = lit_map.paper_nodes[0]

    assert node.paper_key == "machine learning in healthcare|smith|2023"
    assert "machine learning in healthcare_smith" in node.aliases


def test_title_only_identity_uses_unique_source_key_and_blocks_merge():
    lit_map = build_literature_map(
        [
            {"title": "Same Title", "themes": ["alpha"]},
            {"title": "Same Title", "themes": ["beta"]},
        ],
        "job-title-only",
    )

    assert len(lit_map.paper_nodes) == 2
    assert {node.identity_source for node in lit_map.paper_nodes} == {"source_hash"}
    assert all(node.paper_key.startswith("source:") for node in lit_map.paper_nodes)
    assert any(d["type"] == "missing_stable_paper_identity" for d in lit_map.blocking_diagnostics)


def test_research_stream_filters_isolated_noise_without_dropping_meaningful_phrases():
    summaries = [
        {
            "paper_info": {"title": f"Noise Paper {i}", "authors": [f"A{i}"], "year": 2020 + i},
            "themes": ["p001", "1986", "high", "effect", "consumer trust effect", "adoption model"],
            "methods": ["experiment"],
            "findings": ["result"],
        }
        for i in range(3)
    ]

    lit_map = build_literature_map(summaries, "job-noise")
    stream_names = {stream["stream_name"] for stream in lit_map.research_streams}

    assert "p001" not in stream_names
    assert "1986" not in stream_names
    assert "high" not in stream_names
    assert "effect" not in stream_names
    assert "result" not in stream_names
    assert "experiment" not in stream_names
    assert "consumer trust effect" in stream_names
    assert "adoption model" in stream_names

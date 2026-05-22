"""Regression tests for the Outline Intelligence v2 bad 30-paper repair case.

These tests keep a compact deterministic analogue of the user-reported bad case:
30 canonical papers represented by both Stage 1 summaries and paper artifacts,
invalid provider candidates, deterministic fallback recovery, and strict audit
blocking when the final outline still covers too little of the corpus.
"""

from outline.adoption import adopt_final_outline
from outline.arbitration_v2 import arbitrate_deterministic, build_final_outline
from outline.candidates import normalize_candidate_output_with_report, validate_candidate
from outline.coverage_audit import run_coverage_audit
from outline.literature_map import build_literature_map
from outline.quality_rules import is_placeholder_title
from outline.synthesis_flow import build_synthesis_flow
from outline.v2_models import OutlineCritiquesV2, compute_content_hash


def _bad_case_sources(paper_count=30):
    """Return paired summaries/artifacts for one node per canonical paper.

    This compresses the real 30-paper bad case without checking in large
    generated artifacts: each canonical paper has two source records, stable DOI
    identity, and one of two substantive streams.
    """
    summaries = []
    paper_artifacts = []
    for idx in range(paper_count):
        title = f"Promotion Fairness Paper {idx:02d}"
        doi = f"10.4242/outline-v2-bad.{idx:02d}"
        theme = "promotion fairness" if idx < paper_count // 2 else "consumer trust"
        paper_info = {
            "title": title,
            "authors": [f"Author {idx}"],
            "year": 2000 + idx,
            "doi": doi,
            "classification": "support",
        }
        summaries.append(
            {
                "paper_info": paper_info,
                "themes": [theme],
                "methods": ["survey"],
                "limitations": ["single context"],
                "ai_summary": {
                    "paper_metadata": paper_info,
                    "routing": {
                        "paper_type": "empirical",
                        "paper_subtype_normalized": "survey",
                        "classification_status": "resolved",
                    },
                    "core_analysis": {
                        "summary": f"Evidence about {theme}.",
                        "key_points": [theme],
                        "methodology": "survey",
                        "findings": "Substantive finding.",
                    },
                    "quality_audit": {"extraction_confidence": "high"},
                },
            }
        )
        paper_artifacts.append(
            {
                "artifact_type": "paper_artifact",
                "paper_identity": {"canonical_paper_key": doi},
                "source": {"metadata_confidence": "high"},
                "paper_info": paper_info,
                "themes": [theme],
                "methods": ["survey"],
                "limitations": ["single context"],
            }
        )
    return summaries, paper_artifacts


def _bad_case_map_and_flow():
    summaries, paper_artifacts = _bad_case_sources()
    literature_map = build_literature_map(summaries, "job-outline-v2-bad", paper_artifacts)
    synthesis_flow = build_synthesis_flow(literature_map, "job-outline-v2-bad")
    return literature_map, synthesis_flow


def _invalid_provider_candidates(literature_map):
    first_paper = literature_map.paper_nodes[0].paper_key
    return {
        "candidates": [
            {
                "candidate_id": "provider_placeholder",
                "strategy": "generic_three_section_outline",
                "sections": [
                    {
                        "section_id": "p1",
                        "title": "Research problem framing",
                        "assigned_papers": [first_paper],
                    }
                ],
            },
            {
                "candidate_id": "provider_bad_flow_ref",
                "strategy": "bad_refs",
                "sections": [
                    {
                        "section_id": "p2",
                        "title": "Promotion fairness evidence stream",
                        "purpose": "Looks substantive but cites no controlled flow.",
                        "argument_role": "synthesize_stream",
                        "source_flow_steps": ["synthetic_method_section"],
                        "assigned_papers": [first_paper],
                    }
                ],
            },
            {
                "candidate_id": "provider_thin_section",
                "strategy": "too_thin",
                "sections": [
                    {
                        "section_id": "p3",
                        "title": "Section 1",
                        "purpose": "Placeholder title with duplicate evidence.",
                        "source_flow_steps": [],
                        "assigned_papers": [first_paper, first_paper],
                    }
                ],
            },
        ]
    }


def _fallback_candidates_after_bad_provider(literature_map, synthesis_flow):
    candidates, report = normalize_candidate_output_with_report(
        _invalid_provider_candidates(literature_map),
        literature_map,
        synthesis_flow,
        3,
        "Outline_API",
        allow_deterministic_fallback=True,
    )
    return candidates, report


def _final_outline_from_candidates(candidates, literature_map, synthesis_flow):
    arbitration = arbitrate_deterministic(candidates, OutlineCritiquesV2())
    return build_final_outline(
        candidates,
        arbitration,
        compute_content_hash(literature_map.to_dict()),
        compute_content_hash(synthesis_flow.to_dict()),
        "job-outline-v2-bad",
    )


def test_bad_case_fixture_preserves_30_canonical_papers_without_stream_explosion():
    literature_map, synthesis_flow = _bad_case_map_and_flow()

    assert len(literature_map.source_summary_hashes) == 60
    assert len(literature_map.paper_nodes) == 30
    assert literature_map.blocking_diagnostics == []
    assert 2 <= len(literature_map.research_streams) <= 8
    assert {stream["stream_name"] for stream in literature_map.research_streams} >= {
        "promotion fairness",
        "consumer trust",
    }
    assert synthesis_flow.placeholder_flow is False
    assert all(not is_placeholder_title(step.claim) for step in synthesis_flow.flow_steps if not step.placeholder_flow)


def test_bad_provider_candidates_are_rejected_and_reported_before_fallback():
    literature_map, synthesis_flow = _bad_case_map_and_flow()

    candidates, report = _fallback_candidates_after_bad_provider(literature_map, synthesis_flow)

    assert report["provider_total"] == 3
    assert report["provider_valid"] == 0
    assert report["fallback_triggered"] is True
    assert report["fallback_valid"] == 3
    assert report["final_valid_count"] == 3
    assert report["pipeline_continued"] is True
    rejected_ids = {item["candidate_id"] for item in report["rejected_reasons"] if item["source"] == "provider"}
    assert rejected_ids == {"provider_placeholder", "provider_bad_flow_ref", "provider_thin_section"}
    rejection_text = "\n".join(
        reason
        for item in report["rejected_reasons"]
        for reason in item.get("reasons", [])
    )
    assert "no valid flow refs" in rejection_text or "placeholder" in rejection_text
    assert "no valid flow refs" in rejection_text or "fewer than 3 effective sections" in rejection_text
    assert "no valid flow refs" in rejection_text or "invalid/diagnostic flow step" in rejection_text
    assert {candidate.candidate_id for candidate in candidates.candidates}.isdisjoint(rejected_ids)
    assert all(validate_candidate(candidate, literature_map, synthesis_flow, strict=True) == [] for candidate in candidates.candidates)


def test_fallback_final_outline_uses_real_flow_but_bad_case_audit_stays_blocked():
    literature_map, synthesis_flow = _bad_case_map_and_flow()
    candidates, _report = _fallback_candidates_after_bad_provider(literature_map, synthesis_flow)
    final_outline = _final_outline_from_candidates(candidates, literature_map, synthesis_flow)

    audit = run_coverage_audit(final_outline, literature_map, synthesis_flow)

    assert all(not is_placeholder_title(section.title) for section in final_outline.sections)
    valid_step_ids = {step.flow_step_id for step in synthesis_flow.flow_steps if not step.placeholder_flow}
    assert {step for section in final_outline.sections for step in section.source_flow_steps} <= valid_step_ids
    assert audit.passed is False
    assert audit.canonical_paper_coverage_ratio < 0.5
    assert audit.coverage_metrics["total_canonical_papers"] == 30
    assert audit.coverage_metrics["canonical_papers_in_outline"] < 15
    assert "canonical_coverage_below_threshold" in {issue.issue_type for issue in audit.blocking_issues}


def test_failed_bad_case_audit_blocks_v2_adoption():
    literature_map, synthesis_flow = _bad_case_map_and_flow()
    candidates, _report = _fallback_candidates_after_bad_provider(literature_map, synthesis_flow)
    final_outline = _final_outline_from_candidates(candidates, literature_map, synthesis_flow)
    audit = run_coverage_audit(final_outline, literature_map, synthesis_flow)

    adopted, message = adopt_final_outline(final_outline, audit, "job-outline-v2-bad", "regression-test")

    assert audit.passed is False
    assert adopted is None
    assert "pass" in message.lower() or "blocked" in message.lower()

import types

from docx_writer import render_structured_citations
from services.review_draft import build_review_draft_v2
from validation.review_validator import CitationValidationResult, ReviewValidationReport, RootCause, ValidationConclusion
import validator


class _DummyLogger:
    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def error(self, _msg):
        pass

    def success(self, _msg):
        pass


class _DummyConfig:
    def getboolean(self, _section, _option, fallback=False):
        return True


def _target_claim_unit() -> dict:
    return {
        "claim_unit_id": "cu-1",
        "block_id": "s1_b1",
        "span_start": 0,
        "span_end": 64,
        "claim_text": "Sentence one. Sentence two with cite.",
        "citation_tokens": ["[[cite:paper-1|mode=parenthetical]]"],
        "block_anchor_hash": "anchor-a",
    }


def test_review_draft_v2_disables_legacy_regex_by_default():
    draft = build_review_draft_v2(
        job_id="job-1",
        project_name="demo",
        draft_id="draft-1",
        outline_artifact_id="outline-1",
        outline_source_path="outline.md",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[
            {
                "section_number": 1,
                "section_title": "Intro",
                "content": "This paragraph only has (Author, 2024) legacy-style text.",
            }
        ],
        references=[],
        generation_mode="full_review",
        paper_summaries=[],
    )

    block = draft.content["sections"][0].blocks[0]
    assert block.citations == []


def test_render_structured_citations_prefers_manifest_entries():
    generator = types.SimpleNamespace(summaries=[], logger=_DummyLogger(), config={})
    manifest = {
        "paper_entries": [
            {
                "paper_id": "paper-1",
                "paper_key": "paper-1",
                "title": "Canonical Paper",
                "authors": ["Alice Smith", "Bob Jones"],
                "year": "2024",
                "aliases": ["paper-1", "Canonical Paper"],
            }
        ]
    }

    rendered, unresolved = render_structured_citations(
        "Evidence [[cite:paper-1|mode=parenthetical]] supports the claim.",
        generator,
        manifest,
        allow_compat_fallback=False,
    )

    assert not unresolved
    assert "(Smith & Jones, 2024)" in rendered


def test_run_review_validation_rebuilds_after_summary_only_repairs(monkeypatch):
    generator = types.SimpleNamespace(
        logger=_DummyLogger(),
        config=_DummyConfig(),
        summaries=[],
    )

    citation_result = CitationValidationResult(
        citation_id="cite-1",
        paper_id="paper-1",
        conclusion=ValidationConclusion.PARTIAL_SUPPORT,
        root_causes=[RootCause.SUMMARY_DRIFT],
        evidence_candidates=[],
        details={"repair_scope": "summary", "summary_paper_ids": ["paper-1"]},
        claim_text="Claim text",
        claim_context="Section 1",
        evidence_excerpt_list=[],
        reasoning_summary="Needs summary repair",
        repair_hint="Refresh the summary",
        citation_set_key="paper-1",
        paper_ids=["paper-1"],
        block_ids=[],
        low_confidence=False,
    )
    report = ReviewValidationReport(
        report_id="report-1",
        created_at="2026-04-18T00:00:00Z",
        total_citations=1,
        supported_count=0,
        partial_support_count=1,
        unsupported_count=0,
        wrong_source_count=0,
        needs_review_count=0,
        citation_results=[citation_result],
    )

    class _DummyReviewValidator:
        def __init__(self, *_args, **_kwargs):
            pass

        def validate(self):
            return report

    monkeypatch.setattr("validation.review_validator.ReviewValidator", _DummyReviewValidator)
    monkeypatch.setattr(validator, "_load_validation_inputs", lambda _g: ({"content": {"sections": []}}, {"artifact_version": "v3", "citation_sets": [], "paper_entries": []}, [], {}, {}))
    monkeypatch.setattr(validator, "_run_ai_bundle_validation", lambda _g, result: result)
    monkeypatch.setattr(validator, "_apply_summary_repairs", lambda *_args, **_kwargs: ["paper-1"])
    monkeypatch.setattr(validator, "_apply_review_repairs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(validator, "_build_report_from_results", lambda results: report)
    monkeypatch.setattr(validator, "_write_validation_reports", lambda *_args, **_kwargs: {"report_file": "report.txt", "manual_report_file": "manual.json"})

    persisted = {}

    def _fake_persist(*_args, **_kwargs):
        persisted["called"] = True
        return {"artifact_version": "v3", "citation_sets": [], "paper_entries": []}

    monkeypatch.setattr(validator, "_persist_repaired_review_artifacts", _fake_persist)

    result = validator.run_review_validation(generator)

    assert result["success"] is True
    assert persisted["called"] is True


def test_apply_review_repairs_only_mutates_target_block(monkeypatch):
    generator = types.SimpleNamespace(logger=_DummyLogger(), config={})
    target_claim_unit = _target_claim_unit()
    review_draft = {
        "content": {
            "sections": [
                {
                    "section_number": 1,
                    "section_title": "Test",
                    "blocks": [
                        {
                            "block_id": "s1_b1",
                            "text": "Sentence one. Sentence two with cite [[cite:paper-1|mode=parenthetical]].",
                            "anchor_hash": "anchor-a",
                        },
                        {
                            "block_id": "s1_b2",
                            "text": "Unrelated block that must not change.",
                            "anchor_hash": "anchor-b",
                        },
                    ],
                }
            ]
        }
    }
    result = CitationValidationResult(
        citation_id="cite-1",
        paper_id="paper-1",
        conclusion=ValidationConclusion.PARTIAL_SUPPORT,
        root_causes=[RootCause.REVIEW_DRIFT],
        evidence_candidates=[],
        details={
            "repair_scope": "review",
            "disposition": "review_repair",
            "bundle": {"citation_tokens": ["[[cite:paper-1|mode=parenthetical]]"]},
            "target_claim_unit": target_claim_unit,
        },
        claim_text="Sentence one. Sentence two with cite.",
        claim_context="Test",
        evidence_excerpt_list=["Evidence excerpt"],
        reasoning_summary="Needs narrowing",
        repair_hint="Narrow it",
        citation_set_key="paper-1",
        paper_ids=["paper-1"],
        block_ids=["s1_b1", "s1_b2"],
        low_confidence=False,
        disposition="review_repair",
        target_claim_unit=target_claim_unit,
    )

    monkeypatch.setattr(validator, "_get_validator_api_config", lambda _g: {"model": "dummy"})
    monkeypatch.setattr(
        validator,
        "_call_ai_api",
        lambda *_args, **_kwargs: {"rewritten_claim_unit": "Narrowed claim [[cite:paper-1|mode=parenthetical]]."},
    )

    touched = validator._apply_review_repairs(generator, review_draft, [result])

    assert touched == ["s1_b1"]
    assert review_draft["content"]["sections"][0]["blocks"][0]["text"].startswith("Narrowed claim")
    assert review_draft["content"]["sections"][0]["blocks"][0]["anchor_hash"] != "anchor-a"
    assert review_draft["content"]["sections"][0]["blocks"][0]["span_map"]["sentences"]
    assert review_draft["content"]["sections"][0]["blocks"][1]["text"] == "Unrelated block that must not change."

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import fitz  # type: ignore[import-not-found]
import pytest

from tests.scientific_support_oracle import (
    AuthoredAssertion,
    ControlledPdfCorpus,
    PaperClause,
    audit_candidate_support,
    audit_canonical_citation_manifest,
    evaluate_assertion,
)

_FACTS_A = (
    ("FACT | study=Study A | n=24 | direction=increase | magnitude=4 points | "
    "condition=fixed-value coupon | follow-up=30 days | "
    "qualifiers=observed sample only; no conclusion beyond 30 days; no pooled estimate"),
    ("FACT | study=Study B | n=31 | direction=null | magnitude=none | "
    "condition=bundle-price display | follow-up=30 days | "
    "qualifiers=distinct Study B sample; no pooled estimate"),
)
_FACTS_B = (
    ("FACT | study=Study C | n=28 | direction=decrease | magnitude=2 points | "
    "condition=reference-price label | follow-up=30 days | "
    "qualifiers=observed sample only; no conclusion beyond 30 days"),
)


def _write_source_pdf(path: Path, title: str, facts: tuple[str, ...]) -> None:
    document = fitz.open()
    try:
        first = document.new_page()
        first.insert_text((40, 40), f"TITLE: {title}", fontsize=10)
        first.insert_textbox(
            fitz.Rect(40, 72, 560, 500),
            "\n".join(facts),
            fontsize=9,
            lineheight=1.2,
        )
        second = document.new_page()
        second.insert_text(
            (40, 50),
            "Editorial note: the source's controlled fact rows are printed on page 1.",
            fontsize=9,
        )
        document.save(path)
    finally:
        document.close()


def _corpus(tmp_path: Path) -> ControlledPdfCorpus:
    paper_a = tmp_path / "coupon-study-a.pdf"
    paper_b = tmp_path / "reference-study-b.pdf"
    _write_source_pdf(paper_a, "Coupon Study A", _FACTS_A)
    _write_source_pdf(paper_b, "Reference Study B", _FACTS_B)
    return ControlledPdfCorpus.from_pdfs({"paper-a": paper_a, "paper-b": paper_b})


def _actual_harness_pdf_corpus(tmp_path: Path) -> ControlledPdfCorpus:
    """Write real scale-harness PDFs, then give only paths to the oracle."""

    from tests.test_run_first_scale_integration import _scale_papers, _write_scale_pdf

    pdf_dir = tmp_path / "actual-harness-pdfs"
    pdf_dir.mkdir()
    for row in _scale_papers(4):
        if row.index == 3:
            # Keep the deliberate unknown-condition negative independent of
            # the positive scale generator's complete source conditions.
            row = replace(row, condition_description="")
        _write_scale_pdf(pdf_dir / f"{row.stem}.pdf", row)
    return ControlledPdfCorpus.from_pdfs({
        "paper-a": pdf_dir / "rfs-paper-0000.pdf",
        "paper-b": pdf_dir / "rfs-paper-0001.pdf",
        "paper-null": pdf_dir / "rfs-paper-0002.pdf",
        "paper-negative": pdf_dir / "rfs-paper-0003.pdf",
    })


@pytest.mark.parametrize("paper_index", [0, 2, 3, 9])
def test_identity_doi_footer_preserves_pdf_fact_and_qualifiers(
    tmp_path: Path, paper_index: int,
) -> None:
    from tests.test_run_first_scale_integration import _scale_papers, _write_scale_pdf

    paper = _scale_papers(10, source_variant="all_required_comparisons")[paper_index]
    plain_path = tmp_path / "plain.pdf"
    identity_path = tmp_path / "identity.pdf"
    _write_scale_pdf(plain_path, paper)
    _write_scale_pdf(identity_path, paper, identity_doi=f"10.5555/rfs-{paper_index:04d}")
    plain = ControlledPdfCorpus.from_pdfs({paper.paper_key: plain_path}).documents[paper.paper_key]
    identity = ControlledPdfCorpus.from_pdfs({paper.paper_key: identity_path}).documents[paper.paper_key]

    assert len(plain.facts) == len(identity.facts) == 1
    assert plain.pdf_sha256 != identity.pdf_sha256
    assert plain.study_samples == identity.study_samples
    assert plain.parse_diagnostics == identity.parse_diagnostics
    left, right = plain.facts[0], identity.facts[0]
    assert (
        left.study_label, left.sample_size, left.direction, left.magnitude_points,
        left.condition, left.follow_up_days, left.required_qualifiers, left.source_excerpt,
    ) == (
        right.study_label, right.sample_size, right.direction, right.magnitude_points,
        right.condition, right.follow_up_days, right.required_qualifiers, right.source_excerpt,
    )
    assert "DOI:" not in right.source_excerpt
    assert right.locator.pdf_sha256 == identity.pdf_sha256
    if paper_index == 9:
        assert right.condition.casefold() == "first-visit-only loyalty credit condition"


def _good_sides() -> tuple[str, str]:
    side_a = (
        "Coupon Study A reports Study A increased the primary outcome by 4 points (n=24) "
        "under the fixed-value coupon with a 30-day follow-up; observed sample only; "
        "no conclusion beyond 30 days; no pooled estimate"
    )
    side_b = (
        "Reference Study B reports Study C decreased the primary outcome by 2 points (n=28) "
        "under the reference-price label with a 30-day follow-up; observed sample only; "
        "no conclusion beyond 30 days"
    )
    return side_a, side_b


def _comparison(corpus: ControlledPdfCorpus, side_a: str, side_b: str) -> AuthoredAssertion:
    text = f"Within the source-reported sample boundaries, {side_a}; whereas {side_b}."
    return corpus.authored_assertion_from_source_titles(
        text,
        required_paper_keys=("paper-a", "paper-b"),
    )


def test_joint_attribution_checks_the_whole_assertion_against_each_actual_pdf(tmp_path):
    left = tmp_path / "left.pdf"
    right = tmp_path / "right.pdf"
    _write_source_pdf(left, "Recorded source A", (_FACTS_A[0],))
    _write_source_pdf(right, "Recorded source B", (_FACTS_A[0],))
    corpus = ControlledPdfCorpus.from_pdfs({"paper-a": left, "paper-b": right})
    shared = _good_sides()[0].removeprefix("Coupon Study A reports ")
    text = f"Recorded source A and Recorded source B each report {shared}"
    assertion = corpus.authored_assertion_from_source_titles(text, required_paper_keys=("paper-a", "paper-b"))
    assert len(assertion.paper_clauses) == 2
    assert all(clause.text == shared for clause in assertion.paper_clauses)
    verdict = evaluate_assertion(corpus, assertion)
    assert verdict.judgment == "supported", verdict
    _write_source_pdf(right, "Recorded source B", _FACTS_B)
    different = ControlledPdfCorpus.from_pdfs({"paper-a": left, "paper-b": right})
    assert evaluate_assertion(different, assertion).judgment == "unsupported"


def test_parses_pdf_facts_and_proves_both_comparison_sides(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    facts_a = {fact.study_label: fact for fact in corpus.documents["paper-a"].facts}
    assert facts_a["Study A"].sample_size == 24
    assert facts_a["Study A"].direction == "increase"
    assert facts_a["Study A"].magnitude_points == 4
    assert facts_a["Study B"].sample_size == 31
    assert facts_a["Study B"].direction == "null"
    assert facts_a["Study B"].magnitude_points is None
    assert facts_a["Study A"].locator.page_number == 1
    assert facts_a["Study B"].locator.page_number == 1
    assert len(corpus.documents["paper-a"].pdf_sha256) == 64

    side_a, side_b = _good_sides()
    verdict = evaluate_assertion(corpus, _comparison(corpus, side_a, side_b))

    assert verdict.judgment == "supported", verdict
    assert verdict.issue_codes == ()
    assert verdict.metrics.required_paper_sides_supported == 2
    assert verdict.metrics.required_paper_sides == 2
    assert verdict.metrics.support_sides_supported == 2
    assert verdict.metrics.support_sides_submitted == 2
    assert verdict.metrics.locator_pages_verified == 2
    assert verdict.metrics.locator_pages_required == 2
    assert verdict.metrics.fact_obligations_met == verdict.metrics.fact_obligations_required == 12
    assert verdict.metrics.qualifier_obligations_met == verdict.metrics.qualifier_obligations_required == 5
    assert {(proof.paper_key, proof.study_label, proof.page_number) for proof in verdict.proofs} == {
        ("paper-a", "Study A", 1),
        ("paper-b", "Study C", 1),
    }
    assert all(len(proof.pdf_sha256) == len(proof.page_sha256) == len(proof.fact_sha256) == 64 for proof in verdict.proofs)


def test_requires_both_source_sides_and_rejects_cross_paper_attribution(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    side_a, side_b = _good_sides()

    half = evaluate_assertion(
        corpus,
        AuthoredAssertion(
            text=side_a,
            paper_clauses=(PaperClause("paper-a", side_a),),
            required_paper_keys=("paper-a", "paper-b"),
        ),
    )
    assert half.judgment == "unsupported"
    assert "missing_required_paper_side" in half.issue_codes
    assert half.metrics.required_paper_sides_supported == 1
    assert half.metrics.required_paper_sides == 2

    text = f"{side_a}; whereas {side_b}."
    swapped = AuthoredAssertion(
        text=text,
        paper_clauses=(
            PaperClause("paper-b", side_a),
            PaperClause("paper-a", side_b),
        ),
        required_paper_keys=("paper-a", "paper-b"),
    )
    cross_paper = evaluate_assertion(corpus, swapped)
    assert cross_paper.judgment == "unsupported"
    assert "cross_paper_attribution" in cross_paper.issue_codes
    assert cross_paper.metrics.required_paper_sides_supported == 0


@pytest.mark.parametrize(
    ("claim", "expected_issue"),
    [
        (
            ("Coupon Study A reports Study A increased the primary outcome by 5 points (n=24) "
            "under the fixed-value coupon with a 30-day follow-up; observed sample only; "
            "no conclusion beyond 30 days; no pooled estimate"),
            "effect_magnitude_mismatch",
        ),
        (
            ("Coupon Study A reports Study A increased the primary outcome by 4 points (n=31) "
            "under the fixed-value coupon with a 30-day follow-up; observed sample only; "
            "no conclusion beyond 30 days; no pooled estimate"),
            "sample_size_mismatch",
        ),
        (
            ("Coupon Study A reports Study A decreased the primary outcome by 4 points (n=24) "
            "under the fixed-value coupon with a 30-day follow-up; observed sample only; "
            "no conclusion beyond 30 days; no pooled estimate"),
            "effect_direction_mismatch",
        ),
        (
            ("Coupon Study A reports Study A increased the primary outcome by 4 points (n=24) "
            "under the fixed-value coupon with a 30-day follow-up"),
            "required_qualifier_omitted",
        ),
        (
            ("Coupon Study A reports Study 1 increased the primary outcome by 4 points (n=24) "
            "under the fixed-value coupon with a 30-day follow-up; observed sample only; "
            "no conclusion beyond 30 days; no pooled estimate"),
            "unsupported_study_label",
        ),
        (
            ("Coupon Study A reports Study B increased the primary outcome by 1 point (n=31) "
            "under the bundle-price display with a 30-day follow-up; distinct Study B sample; "
            "no pooled estimate"),
            "effect_direction_mismatch",
        ),
    ],
)
def test_rejects_fabricated_or_reversed_source_facts(
    tmp_path: Path,
    claim: str,
    expected_issue: str,
) -> None:
    corpus = _corpus(tmp_path)
    assertion = corpus.authored_assertion_from_source_titles(
        claim,
        required_paper_keys=("paper-a",),
    )
    verdict = evaluate_assertion(corpus, assertion)
    assert verdict.judgment == "unsupported", verdict
    assert expected_issue in verdict.issue_codes


def test_null_finding_cannot_be_promoted_to_a_positive_effect(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    claim = (
        "Coupon Study A reports Study B increased the primary outcome by 1 point (n=31) "
        "under the bundle-price display with a 30-day follow-up; distinct Study B sample; "
        "no pooled estimate"
    )
    verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(claim, required_paper_keys=("paper-a",)),
    )
    assert verdict.judgment == "unsupported"
    assert "effect_direction_mismatch" in verdict.issue_codes
    assert "magnitude_asserted_for_null_finding" in verdict.issue_codes


def test_locator_must_point_to_the_page_containing_the_fact(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    side_a, _side_b = _good_sides()
    assertion = AuthoredAssertion(
        text=side_a,
        paper_clauses=(PaperClause("paper-a", side_a, page_number=2),),
        required_paper_keys=("paper-a",),
    )
    verdict = evaluate_assertion(corpus, assertion)
    assert verdict.judgment == "unsupported"
    assert "source_locator_page_mismatch" in verdict.issue_codes
    assert verdict.metrics.locator_pages_verified == 0
    assert verdict.metrics.locator_pages_required == 1


def test_unsupported_grammar_is_explicitly_unjudgable(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    claim = (
        "Coupon Study A reports Study A had a 4 point outcome (n=24) under the fixed-value coupon "
        "with a 30-day follow-up; observed sample only; no conclusion beyond 30 days; no pooled estimate"
    )
    verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(claim, required_paper_keys=("paper-a",)),
    )
    assert verdict.judgment == "unjudgable"
    assert "unjudgable_effect_grammar" in verdict.issue_codes
    assert verdict.metrics.required_paper_sides == 1
    assert verdict.metrics.required_paper_sides_supported == 0


def test_actual_harness_pdfs_support_candidate_style_comparison_without_metadata_echo(
    tmp_path: Path,
) -> None:
    corpus = _actual_harness_pdf_corpus(tmp_path)
    source_a = corpus.documents["paper-a"]
    source_b = corpus.documents["paper-b"]
    fact_a = source_a.facts[0]
    fact_b = source_b.facts[0]
    assert (fact_a.study_label, fact_a.sample_size, fact_a.direction, fact_a.magnitude_points) == (
        "Study A", 24, "increase", 4,
    )
    assert fact_a.condition == "fixed-value coupon"
    assert fact_a.follow_up_days == 30
    assert [(row.study_label, row.sample_size, row.condition) for row in source_a.study_samples] == [
        ("Study A", 24, "fixed-value coupon"),
        ("Study B", 31, "bundle-price display"),
    ]
    assert not any(fact.study_label == "Study B" for fact in source_a.facts), (
        "the PDF reports Study B's separate sample but gives the magnitude only for Study A"
    )
    assert (fact_b.study_label, fact_b.sample_size, fact_b.direction, fact_b.magnitude_points) == (
        None, 24, "increase", 2,
    )
    assert source_a.parse_diagnostics == source_b.parse_diagnostics == ()
    assert fact_a.supporting_locators and {row.page_number for row in fact_a.supporting_locators} == {1, 2}

    side_a = (
        "RFS-PAPER-0000 reports Study A increased the primary outcome by 4 points (n=24) under the "
        "fixed-value coupon with a 30-day follow-up; observed controlled sample only; no longer-term "
        "effect is established. Study B (n=31) is a distinct bundle-price display sample and no common "
        "pooled estimate is reported"
    )
    side_b = (
        "RFS-PAPER-0001 reports the treatment increased the primary outcome by 2 points (n=24) under "
        "the reference-price label and no-promotion shelf label with a 30-day follow-up; observed "
        "controlled sample only; no longer-term effect is established"
    )
    assertion = corpus.authored_assertion_from_source_titles(
        f"Within each recorded sample boundary, {side_a}; whereas {side_b}.",
        required_paper_keys=("paper-a", "paper-b"),
    )
    verdict = evaluate_assertion(corpus, assertion)
    assert verdict.judgment == "supported", verdict
    assert verdict.metrics.required_paper_sides_supported == verdict.metrics.required_paper_sides == 2
    assert verdict.metrics.locator_pages_verified == verdict.metrics.locator_pages_required == 4
    assert {
        (proof.paper_key, proof.study_label, proof.supporting_page_numbers)
        for proof in verdict.proofs
    } == {
        ("paper-a", "Study A", (1, 2)),
        ("paper-b", "unlabeled_sample", (1, 2)),
    }


@pytest.mark.parametrize(
    ("edited_side", "required_issue"),
    [
        ("magnitude", "effect_magnitude_mismatch"),
        ("condition", "condition_omitted_or_mismatched"),
    ],
)
def test_actual_harness_pdf_negative_controls_catch_changed_magnitude_or_condition(
    tmp_path: Path,
    edited_side: str,
    required_issue: str,
) -> None:
    corpus = _actual_harness_pdf_corpus(tmp_path)
    side_a = (
        "RFS-PAPER-0000 reports Study A increased the primary outcome by 4 points (n=24) under the "
        "fixed-value coupon with a 30-day follow-up; observed controlled sample only; no longer-term "
        "effect is established. Study B (n=31) is a distinct bundle-price display sample and no common "
        "pooled estimate is reported"
    )
    side_b = (
        "RFS-PAPER-0001 reports the treatment increased the primary outcome by 2 points (n=24) under "
        "the reference-price label and no-promotion shelf label with a 30-day follow-up; observed "
        "controlled sample only; no longer-term effect is established"
    )
    if edited_side == "magnitude":
        side_a = side_a.replace("by 4 points", "by 5 points")
    else:
        side_a = side_a.replace("under the fixed-value coupon", "under an offer")
    assertion = corpus.authored_assertion_from_source_titles(
        f"Within each recorded sample boundary, {side_a}; whereas {side_b}.",
        required_paper_keys=("paper-a", "paper-b"),
    )
    verdict = evaluate_assertion(corpus, assertion)
    assert verdict.judgment == "unsupported", verdict
    assert required_issue in verdict.issue_codes
    assert verdict.metrics.required_paper_sides_supported == 1
    assert verdict.metrics.required_paper_sides == 2


def test_candidate_and_writer_adapters_bind_pdf_claims_to_canonical_clusters(
    tmp_path: Path,
) -> None:
    corpus = _actual_harness_pdf_corpus(tmp_path)
    side_a = (
        "RFS-PAPER-0000 reports Study A increased the primary outcome by 4 points (n=24) under the "
        "fixed-value coupon with a 30-day follow-up; observed controlled sample only; no longer-term "
        "effect is established. Study B (n=31) is a distinct bundle-price display sample and no common "
        "pooled estimate is reported"
    )
    side_b = (
        "RFS-PAPER-0001 reports the treatment increased the primary outcome by 2 points (n=24) under "
        "the reference-price label and no-promotion shelf label with a 30-day follow-up; observed "
        "controlled sample only; no longer-term effect is established"
    )
    claim_text = f"Within each recorded sample boundary, {side_a}; whereas {side_b}."
    candidate = audit_candidate_support(
        corpus,
        [{"claim_support": [
            {"claim": claim_text, "paper_key": "paper-a"},
            {"claim": claim_text, "paper_key": "paper-b"},
        ]}],
        required_paper_keys=("paper-a", "paper-b"),
    )
    assert candidate["status"] == "supported", candidate
    assert candidate["sides_supported"] == candidate["sides_required"] == 2

    manifest = {
        "citation_sets": [{
            "bundle_id": "bundle-ab",
            "paper_keys": ["paper-a", "paper-b"],
            "occurrence_ids": ["occ-a", "occ-b"],
            "claim_texts": [claim_text],
        }],
        "clusters": [
            {"cluster_id": "cluster-a", "paper_key": "paper-a", "occurrence_ids": ["occ-a"]},
            {"cluster_id": "cluster-b", "paper_key": "paper-b", "occurrence_ids": ["occ-b"]},
        ],
    }
    writer = audit_canonical_citation_manifest(
        corpus,
        manifest,
        required_paper_keys=("paper-a", "paper-b"),
    )
    assert writer["status"] == "supported", writer
    assert writer["assertions"][0]["citation_set_matches_required_pair"] is True
    assert writer["assertions"][0]["clusters_match_required_pair"] is True

    half_cluster = {
        "citation_sets": [{
            "bundle_id": "bundle-a-only",
            "paper_keys": ["paper-a"],
            "occurrence_ids": ["occ-a"],
            "claim_texts": [claim_text],
        }],
        "clusters": [
            {"cluster_id": "cluster-a", "paper_key": "paper-a", "occurrence_ids": ["occ-a"]},
        ],
    }
    rejected_writer = audit_canonical_citation_manifest(
        corpus,
        half_cluster,
        required_paper_keys=("paper-a", "paper-b"),
    )
    assert rejected_writer["status"] == "unsupported"
    assert "writer_citation_set_does_not_cover_required_papers" in rejected_writer["issue_codes"]
    assert "canonical_citation_clusters_do_not_cover_required_papers" in rejected_writer["issue_codes"]


def test_actual_harness_null_and_negative_findings_are_not_direction_promoted(tmp_path: Path) -> None:
    corpus = _actual_harness_pdf_corpus(tmp_path)
    null_fact = corpus.documents["paper-null"].facts[0]
    negative_fact = corpus.documents["paper-negative"].facts[0]
    assert (null_fact.direction, null_fact.magnitude_points) == ("null", None)
    assert (negative_fact.direction, negative_fact.magnitude_points) == ("decrease", 2)

    null_claim = (
        "RFS-PAPER-0002 reports the treatment increased the primary outcome by 1 point (n=24) "
        "with a 30-day follow-up; observed controlled sample only; no longer-term effect is established"
    )
    negative_claim = (
        "RFS-PAPER-0003 reports the treatment increased the primary outcome by 2 points (n=24) "
        "with a 30-day follow-up; observed controlled sample only; no longer-term effect is established"
    )
    null_verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(null_claim, required_paper_keys=("paper-null",)),
    )
    negative_verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(negative_claim, required_paper_keys=("paper-negative",)),
    )
    assert null_verdict.judgment == "unsupported"
    assert "effect_direction_mismatch" in null_verdict.issue_codes
    assert "magnitude_asserted_for_null_finding" in null_verdict.issue_codes
    assert negative_verdict.judgment == "unsupported"
    assert "effect_direction_mismatch" in negative_verdict.issue_codes
    assert negative_verdict.metrics.unjudgable_source_sides == 1
    assert "unjudgable_source_condition" in negative_verdict.issue_codes


def test_incompatible_actual_pdf_format_is_counted_unjudgable_not_supported(tmp_path: Path) -> None:
    path = tmp_path / "unrecognized-source.pdf"
    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((40, 40), "Title: Unrecognized Controlled Source", fontsize=10)
        page.insert_text((40, 72), "Results: shoppers responded in different ways.", fontsize=9)
        document.save(path)
    finally:
        document.close()
    corpus = ControlledPdfCorpus.from_pdfs({"paper-unknown": path})
    assert corpus.documents["paper-unknown"].parse_diagnostics

    claim = "Unrecognized Controlled Source reports that the campaign produced a durable loyalty shift."
    verdict = evaluate_assertion(
        corpus,
        corpus.authored_assertion_from_source_titles(
            claim,
            required_paper_keys=("paper-unknown",),
        ),
    )
    assert verdict.judgment == "unjudgable"
    assert "unjudgable_source_pdf_facts" in verdict.issue_codes
    assert verdict.metrics.unjudgable_source_sides == 1
    assert verdict.metrics.required_paper_sides_supported == 0
    assert verdict.metrics.required_paper_sides == 1

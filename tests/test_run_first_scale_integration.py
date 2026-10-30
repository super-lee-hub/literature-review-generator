"""Opt-in, one-size-at-a-time local-HTTP acceptance of the current product path.

Run one corpus size per pytest process, for example::

    $env:RUN_FIRST_SCALE_SIZE = "10"
    $env:RUN_FIRST_SCALE_EVIDENCE_DIR = "D:/tmp/run-first-scale-10/evidence"
    python -m pytest -q --strict-markers -p no:cacheprovider `
      --basetemp D:/tmp/run-first-scale-10 `
      tests/test_run_first_scale_integration.py::test_run_first_scale_local_http_full_chain

Only the provider edge is a fixture. Source intake, Stage 1, Outline v3,
candidate packing/serialization, receipts, Registry, adoption, Writer,
validation, resume, and export remain production paths. The fixture reports
simulated local usage explicitly; it is not a provider-cost or scientific
accuracy measurement. On failure, a bounded diagnostic snapshot is written to
RUN_FIRST_SCALE_EVIDENCE_DIR before pytest removes its work area.
Set RUN_FIRST_SCALE_EXPECT_BUDGET_BLOCKED=1 for the separate default-24
rejection path. Later positive matrix sizes require an explicit strict V2
budget in RUN_FIRST_SCALE_PROVIDER_BUDGET_JSON or
RUN_FIRST_SCALE_PROVIDER_BUDGET_FILE.
Synthetic warm mode seeds Stage1 once, then reuses its exact typed manifests
in a new job with the same PDFs/server/config. Process RSS sampling is always
enabled; Python allocation tracing is opt-in via RUN_FIRST_SCALE_TRACE_ALLOCATIONS=1.
RUN_FIRST_SCALE_VARIANT defaults to the unchanged baseline. Opt-in adversarial
profiles select null, reverse, distant cross-shard, or one combined four-comparison profile.
RUN_FIRST_SCALE_OUTLINE_OUTPUT_TOKENS optionally sets this loopback experiment's
Outline output allowance; omission preserves the shipped route setting.
The opt-in identity-quarantine case also fixtures the report parser and PDF
lookup; production PDF identity checks must isolate the mismatched extra source.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import fitz  # type: ignore
import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from services.paper_identity import build_paper_key
from tests.run_first_warm_reuse import run_first_warm_reuse
from tests.run_first_scale_budgets import resolve_scale_test_budgets
from tests.run_first_candidate_oracle_binding import verify_single_source_candidate_bindings
from tests.scientific_support_oracle import (
    ControlledPdfCorpus,
    _condition_present,
    _line_value,
    audit_all_canonical_writer_claim_units,
    audit_all_selected_candidate_support,
    evaluate_assertion,
)
from tests.test_current_runtime_full_e2e import (
    _outline_provider_response,
    _reader_summary,
    _source_attributed_paper_level_limitation,
    _source_bound_topic_response,
    _source_bound_writer_response,
)

_SUPPORTED_SCALE_SIZES = (10, 63, 64, 100, 200, 500, 600)
_SYNTHETIC_SOURCE_GENERATOR_REVISION = "scale-controlled-conditions/v2"
_SYNTHETIC_ADVERSARIAL_VARIANT_REVISION = "scale-controlled-conditions/adversarial-v2"
_SCALE_ADVERSARIAL_VARIANTS = frozenset({
    "baseline",
    "required_null",
    "required_reverse",
    "distant_cross_shard",
    "all_required_comparisons",
})
_SCALE_ADVERSARIAL_SIZES = (10, 64, 100, 200, 500, 600)
_RARE_SCALE_CONDITION = "first-visit-only loyalty credit condition"
_RFS_ID = re.compile(r"RFS-PAPER-(\d{4})")
_FOUR_PAPER_LOOPBACK_BUDGET = {
    "schema_version": "provider-aggregate-budget-contract-v2",
    "max_provider_calls_total": 200,
    "max_output_tokens_total": 2_000_000,
    "max_retry_attempts_total": 0,
    "max_wall_seconds": 1_800,
}
@dataclass(frozen=True)
class ScalePaper:
    index: int
    paper_key: str
    stem: str
    title: str
    finding: str
    limitation: str
    sample_description: str
    table_note: str
    chinese_note: str = ""
    condition_description: str = ""
    extended_source_english: str = ""
    extended_source_chinese: str = ""


_LONG_BILINGUAL_CASES: dict[int, dict[str, str]] = {
    0: {
        "condition_description": (
            "Study A tested a fixed-value coupon; Study B tested a bundle-price display."
        ),
        "extended_source_english": (
            "The report describes two separately conducted promotion studies rather than one pooled experiment. "
            "In Study A, shoppers in the recorded sample saw a fixed-value coupon beside the regular shelf price; "
            "the comparison group saw the same product without a coupon. The outcome was recorded at the stated "
            "30-day follow-up, and the report gives the estimate for that sample only. "
            "In Study B, a different sample saw a bundle-price display that grouped two items under one offer. "
            "Its participants and offer format are described separately from Study A, but this excerpt gives no "
            "separate numeric Study B effect estimate. The report does not create a pooled estimate from the samples. "
            "The article does not combine the two samples into a shared effect estimate, so the Study A result "
            "cannot be assigned to the Study B offer or treated as an overall promotion effect. "
            "The methods section also notes that the follow-up ends at day 30 and that the recorded groups do not "
            "represent a population-wide estimate. These boundaries apply when interpreting the separately printed "
            "sample counts and the estimate shown in Table 2."
        ),
        "extended_source_chinese": (
            "条件说明：Study A 使用固定金额优惠券，Study B 使用组合价格展示；两项研究分别报告，未合并为共同效应。"
            "结果仅对应各自记录的样本和30天观察窗口。"
        ),
    },
    1: {
        "condition_description": (
            "A reference-price label was compared with a no-promotion shelf label in one sample."
        ),
        "extended_source_english": (
            "This source reports one controlled shelf experiment with a single recorded sample. "
            "The treatment condition displayed a reference price above the current price; the comparison condition "
            "showed the current price without a promotional reference. Both conditions used the same product and "
            "the same 30-day observation window. The reported increase is limited to the primary outcome measured "
            "in this sample and does not estimate the effect of a fixed-value coupon or a multi-item bundle. "
            "The paper describes the reference label as the assigned condition, not as proof that every shopper "
            "noticed or used the reference. It reports no separate Study B sample and no pooled estimate with other "
            "articles. Table 2 gives the source estimate; the discussion cautions that the design and follow-up "
            "support only a bounded comparison between the two displayed-price conditions."
        ),
        "extended_source_chinese": (
            "条件说明：本研究在单一样本中比较参考价格标签与不含促销参考价的货架标签。"
            "报告的变化仅适用于该样本、该主要结果及30天观察期。"
        ),
    },
    4: {
        "condition_description": (
            "A member-only threshold offer was compared with the standard displayed price."
        ),
        "extended_source_english": (
            "The recorded experiment concerns a threshold offer available to enrolled members. "
            "Eligible shoppers saw a message that the discount required a minimum basket value; the comparison "
            "condition retained the standard displayed price without the threshold message. The source reports "
            "one controlled sample and one primary outcome at the 30-day follow-up. This condition differs from "
            "the fixed-value coupon, bundle-price, and reference-label examples elsewhere in the synthetic corpus. "
            "The article does not report whether the offer changed behavior after the observation window, and it "
            "does not provide a common estimate across the separate promotion designs. The estimate in Table 2 "
            "must therefore remain attached to the member sample and the minimum-basket condition described here."
        ),
        "extended_source_chinese": (
            "条件说明：会员优惠要求购物篮达到最低金额；对照条件只展示标准价格。"
            "估计值对应会员样本及30天观察期，不代表其他促销形式。"
        ),
    },
}

_SCALE_CONDITION_DESCRIPTIONS = (
    "A fixed-value coupon was compared with the regular shelf price.",
    "A reference-price label was compared with a no-promotion shelf label in one sample.",
    "A loyalty-points reminder followed purchase; comparison shoppers saw no reminder.",
    "A percentage-off shelf label was compared with the regular-price label.",
    "A member-only threshold offer was compared with the standard displayed price.",
    "A minimum-spend free-shipping offer was compared with standard delivery fees.",
    "A two-item multi-buy offer was compared with its regular single-item price.",
    "An aisle-end display was compared with standard shelf placement.",
    "A cashback rebate was compared with the regular price without a rebate.",
    "A two-item bundle price was compared with separate item prices.",
    "A loyalty-card discount was compared with the regular shelf price.",
    "A digital coupon was compared with the regular shelf price.",
)


def _scale_papers(count: int, *, source_variant: str = "baseline") -> list[ScalePaper]:
    """Create varied, explicitly synthetic source rows with stable PDF keys."""

    if source_variant not in _SCALE_ADVERSARIAL_VARIANTS:
        raise AssertionError(f"unsupported RUN_FIRST_SCALE_VARIANT: {source_variant!r}")
    if source_variant != "baseline" and count not in _SCALE_ADVERSARIAL_SIZES:
        raise AssertionError(
            "adversarial scale variants support only sizes "
            f"{_SCALE_ADVERSARIAL_SIZES}, got {count}"
        )

    effects: tuple[int | None, ...] = (4, 2, 0, -2, 1, 3, -1, 0, 5, 2, -3, 1)
    result: list[ScalePaper] = []
    for index in range(count):
        stem = f"rfs-paper-{index:04d}"
        paper_key = build_paper_key({"title": stem})
        effect = effects[index % len(effects)]
        if effect is None or effect == 0:
            finding = (
                "The treatment produced no measurable change in the primary outcome "
                "in the observed controlled sample (n=24)."
            )
        elif effect < 0:
            finding = (
                f"The treatment reduced the primary outcome by {abs(effect)} points "
                "in the observed controlled sample (n=24)."
            )
        else:
            finding = (
                f"The treatment increased the primary outcome by {effect} points "
                "in the observed controlled sample (n=24)."
            )
        limitation = (
            "Results apply only to the observed controlled sample and the 30-day "
            "follow-up; they do not establish a longer-term effect."
        )
        multiple_study_samples = index % 11 == 0
        if multiple_study_samples:
            sample_description = (
                "The article reports two separately labeled samples, Study A (n=24) "
                "and Study B (n=31). The single source estimate is for Study A (n=24); "
                "no separate Study B (n=31) effect estimate and no common pooled effect are reported."
            )
            limitation += " The two source-labeled samples remain distinct."
        else:
            sample_description = "One observed controlled sample (n=24)."
        chinese_note = ""
        if index % 17 == 0:
            if effect is None or effect == 0:
                chinese_note = "中文原文：观察到的样本中，主要结果没有可测量变化。"
            elif effect < 0:
                chinese_note = f"中文原文：观察到的样本中，主要结果下降 {abs(effect)} 个点。"
            else:
                chinese_note = f"中文原文：观察到的样本中，主要结果提高 {effect} 个点。"
        long_case = dict(_LONG_BILINGUAL_CASES.get(index, {}))
        condition_description = long_case.pop(
            "condition_description",
            (
                "A fixed-value coupon was compared with the regular shelf price."
                if index != 0 and index % len(_SCALE_CONDITION_DESCRIPTIONS) == 0
                else _SCALE_CONDITION_DESCRIPTIONS[index % len(_SCALE_CONDITION_DESCRIPTIONS)]
            ),
        )
        if source_variant in {"distant_cross_shard", "all_required_comparisons"} and index == count - 1:
            condition_description = (
                f"Study A tested {_RARE_SCALE_CONDITION}."
                if multiple_study_samples
                else f"{_RARE_SCALE_CONDITION.capitalize()}."
            )
        elif multiple_study_samples and index != 0:
            condition_description = f"Study A tested {condition_description.rstrip(' .;')}."
        table_study_label = "Study A (n=24) " if multiple_study_samples else ""
        result.append(
            ScalePaper(
                index=index,
                paper_key=paper_key,
                stem=stem,
                title=(
                    f"RFS-PAPER-{index:04d}: Price promotion and bounded outcomes"
                    if index % 17
                    else f"RFS-PAPER-{index:04d}: 促销机制与结果边界"
                ),
                finding=finding,
                limitation=limitation,
                sample_description=sample_description,
                table_note=(
                    f"Table 2 (page 2) reports the "
                    f"{table_study_label}source estimate: {finding} "
                    "Figure 3 labels the 30-day observation window."
                ),
                chinese_note=chinese_note,
                condition_description=condition_description,
                **long_case,
            )
        )
    return result


def _scale_required_pair(papers: Sequence[ScalePaper], source_variant: str) -> list[str]:
    """Select an explicit bounded comparison without changing the historical default."""

    if source_variant not in _SCALE_ADVERSARIAL_VARIANTS:
        raise AssertionError(f"unsupported RUN_FIRST_SCALE_VARIANT: {source_variant!r}")
    if source_variant == "baseline":
        indices = (0, 1)
    elif source_variant == "required_null":
        indices = (0, 2)
    elif source_variant == "required_reverse":
        indices = (0, 3)
    else:
        if len(papers) < 10:
            raise AssertionError("distant cross-shard profile requires at least ten actual PDF sources")
        indices = (0, len(papers) - 1)
    if max(indices) >= len(papers):
        raise AssertionError(f"scale {source_variant} comparison requires indices {indices}")
    pair = [papers[index].paper_key for index in indices]
    if len(set(pair)) != 2:
        raise AssertionError(f"scale {source_variant} comparison did not resolve two distinct PDF identities")
    if source_variant == "distant_cross_shard" and indices[1] - indices[0] != len(papers) - 1:
        raise AssertionError("distant cross-shard source indices are not corpus endpoints")
    return pair


def _scale_required_comparisons(
    papers: Sequence[ScalePaper],
    source_variant: str,
) -> list[list[str]]:
    """Preserve historical AB first, then append the opted-in requirements."""

    baseline_pair = _scale_required_pair(papers, "baseline")
    if source_variant == "baseline":
        return [baseline_pair]
    if source_variant == "all_required_comparisons":
        required_pairs = [
            baseline_pair,
            _scale_required_pair(papers, "required_null"),
            _scale_required_pair(papers, "required_reverse"),
            _scale_required_pair(papers, "distant_cross_shard"),
        ]
        # Stable, orientation-preserving de-duplication keeps AB first and
        # never drops it when a small future fixture reuses a pair.
        deduplicated: list[list[str]] = []
        seen: set[tuple[str, str]] = set()
        for pair in required_pairs:
            identity = (pair[0], pair[1])
            if identity not in seen:
                seen.add(identity)
                deduplicated.append(pair)
        if not deduplicated or deduplicated[0] != baseline_pair:
            raise AssertionError("combined profile did not retain historical AB0/1 as its first requirement")
        return deduplicated
    adversarial_pair = _scale_required_pair(papers, source_variant)
    if adversarial_pair == baseline_pair:
        raise AssertionError("adversarial scale profile did not add a comparison beyond historical AB0/1")
    return [baseline_pair, adversarial_pair]


def _write_scale_pdf(path: Path, paper: ScalePaper, *, identity_doi: str = "") -> None:
    document = fitz.open()
    try:
        first = document.new_page()
        first_page_text = (
            f"Title: {paper.title}\n"
            f"Identifier: RFS-PAPER-{paper.index:04d}\n"
            "Method: Controlled empirical comparison; the outcome is measured at 30 days.\n"
            f"Results: {paper.finding}\n"
            f"Sample note: {paper.sample_description}\n"
            f"Limitations: {paper.limitation}"
        )
        if paper.condition_description and not paper.extended_source_english:
            first_page_text += f"\nCondition detail: {paper.condition_description}"
        # Wrap every field inside the PDF page so extracted text retains the
        # complete source sentence and no fact is inferred from ScalePaper metadata.
        remaining = first.insert_textbox(
            fitz.Rect(72, 72, 523, 205),
            first_page_text,
            fontsize=8,
            lineheight=1.05,
        )
        if remaining < 0:
            raise AssertionError(f"synthetic source header did not fit for {paper.paper_key}")
        if paper.chinese_note:
            first.insert_text(
                (72, 212),
                paper.chinese_note,
                # PyMuPDF's built-in Adobe CJK font avoids embedding a full
                # system font in each of the 36 mixed-language scale PDFs.
                fontname="china-s",
                fontsize=9,
            )
        if paper.extended_source_english:
            english_source = "\n\n".join(
                item for item in (
                    f"Condition detail: {paper.condition_description}",
                    f"Source discussion: {paper.extended_source_english}",
                ) if item
            )
            remaining = first.insert_textbox(
                fitz.Rect(72, 245, 523, 600),
                english_source,
                fontsize=8.5,
                lineheight=1.15,
            )
            if remaining < 0:
                raise AssertionError(
                    f"synthetic source passage did not fit on page one for {paper.paper_key}"
                )
        if paper.extended_source_chinese:
            remaining = first.insert_textbox(
                fitz.Rect(72, 610, 523, 770),
                paper.extended_source_chinese,
                fontname="china-s",
                fontsize=8.5,
                lineheight=1.15,
            )
            if remaining < 0:
                raise AssertionError(
                    f"synthetic Chinese passage did not fit on page one for {paper.paper_key}"
                )
        if identity_doi:
            first.insert_text((72, 810), f"DOI: {identity_doi}", fontsize=7)
        second = document.new_page()
        table_page_text = (
            f"Table 2 (page 2): {paper.table_note}\n"
            "Figure 3: source chart labels the measured outcome and the 30-day window.\n"
            "No study identifier is inferred beyond labels printed in this source."
        )
        remaining = second.insert_textbox(
            fitz.Rect(72, 72, 523, 230),
            table_page_text,
            fontsize=8,
            lineheight=1.05,
        )
        if remaining < 0:
            raise AssertionError(f"synthetic table page did not fit for {paper.paper_key}")
        document.save(path)
    finally:
        document.close()


def _mutated_scale_paper(paper: ScalePaper) -> ScalePaper:
    """Return one same-key source revision for the incremental warm fixture."""

    finding = "The treatment increased the primary outcome by 7 points in the observed controlled sample (n=24)."
    table_note = paper.table_note.replace(paper.finding, finding)
    chinese_note = paper.chinese_note
    if chinese_note:
        chinese_note = "中文原文：观察到的样本中，主要结果提高 7 个点。"
    return replace(
        paper,
        finding=finding,
        table_note=table_note,
        chinese_note=chinese_note,
    )


def _mutated_scale_qualifier_paper(paper: ScalePaper) -> ScalePaper:
    """Change only the printed eligibility qualifier on the rare condition."""
    old_qualifier = "first-visit-only"
    offset = paper.condition_description.casefold().find(old_qualifier)
    if offset < 0:
        raise AssertionError(
            "qualifier-only mutation requires the all-required last-paper condition"
        )
    replacement = (
        "Returning-member-only"
        if paper.condition_description[offset].isupper()
        else "returning-member-only"
    )
    condition = (
        paper.condition_description[:offset]
        + replacement
        + paper.condition_description[offset + len(old_qualifier):]
    )
    return replace(paper, condition_description=condition)


def test_scale_long_bilingual_pdfs_wrap_full_source_text_and_keep_page_two_table(
    tmp_path: Path,
) -> None:
    papers = [paper for paper in _scale_papers(5) if paper.extended_source_english]
    assert {paper.index for paper in papers} == {0, 1, 4}
    assert len({paper.condition_description for paper in papers}) == len(papers)
    assert all(len(paper.extended_source_english) >= 700 for paper in papers)

    for paper in papers:
        pdf_path = tmp_path / f"{paper.stem}.pdf"
        _write_scale_pdf(pdf_path, paper)
        with fitz.open(pdf_path) as document:
            assert document.page_count == 2
            first_page = " ".join(document[0].get_text().split())
            second_page = " ".join(document[1].get_text().split())
        assert " ".join(paper.condition_description.split()) in first_page
        assert " ".join(paper.extended_source_english.split()) in first_page
        assert "".join(paper.extended_source_chinese.split()) in "".join(first_page.split())
        assert "Table 2 (page 2)" in second_page
        if paper.index == 0:
            assert "Study A (n=24)" in first_page
            assert "Study B (n=31)" in first_page
            assert "Study A (n=24)" in second_page


def test_scale_seeded_multistudy_rows_parse_and_support_only_printed_study_a_facts(
    tmp_path: Path,
) -> None:
    indexes = (0, 11, 12, 22, 24, 33, 36, 44, 48, 55, 60)
    expected_effects = {
        0: ("increase", 4), 11: ("increase", 1), 12: ("increase", 4),
        22: ("decrease", 3), 24: ("increase", 4), 33: ("increase", 2),
        36: ("increase", 4), 44: ("increase", 5), 48: ("increase", 4),
        55: ("null", None), 60: ("increase", 4),
    }
    papers = {
        paper.index: paper for paper in _scale_papers(max(indexes) + 1)
        if paper.index in indexes
    }
    pdf_paths: dict[str, Path] = {}
    for index in indexes:
        paper = papers[index]
        path = tmp_path / f"{paper.stem}.pdf"
        _write_scale_pdf(path, paper)
        pdf_paths[paper.paper_key] = path
        with fitz.open(path) as document:
            assert document.page_count == 2
            page_one = " ".join(document[0].get_text("text").split())
            page_two = " ".join(document[1].get_text("text").split())
        for printed_field in (
            paper.finding, paper.sample_description, paper.limitation,
            paper.condition_description,
        ):
            assert " ".join(printed_field.split()) in page_one
        assert " ".join(paper.table_note.split()) in page_two

    # The oracle reads only the generated PDF bytes; ScalePaper values below
    # are expected claims, never a substitute source of parsed facts.
    corpus = ControlledPdfCorpus.from_pdfs(pdf_paths)
    for index in indexes:
        paper = papers[index]
        source = corpus.documents[paper.paper_key]
        expected_direction, expected_magnitude = expected_effects[index]
        assert source.parse_diagnostics == (), (index, source.parse_diagnostics)
        assert len(source.facts) == 1
        fact = source.facts[0]
        assert (fact.direction, fact.magnitude_points, fact.sample_size, fact.follow_up_days) == (
            expected_direction, expected_magnitude, 24, 30,
        )
        if index % 11 == 0:
            assert fact.study_label == "Study A"
            assert [(row.study_label, row.sample_size) for row in source.study_samples] == [
                ("Study A", 24), ("Study B", 31),
            ]
            assert not any(row.study_label == "Study B" for row in source.facts)
            assert "no separate Study B (n=31) effect estimate" in paper.sample_description
        else:
            assert fact.study_label is None
            assert len(source.study_samples) == 1
            assert "Study " not in paper.condition_description

        candidate_text = " ".join((
            paper.title, paper.finding, paper.sample_description,
            paper.limitation, paper.condition_description,
        ))
        assertion = corpus.authored_assertion_from_source_titles(
            candidate_text,
            required_paper_keys=(paper.paper_key,),
        )
        verdict = evaluate_assertion(corpus, assertion)
        assert verdict.judgment == "supported", (index, verdict)

    multi = papers[11]
    positive_text = " ".join((
        multi.title, multi.finding, multi.sample_description,
        multi.limitation, multi.condition_description,
    ))
    assertion = lambda text: evaluate_assertion(  # noqa: E731
        corpus,
        corpus.authored_assertion_from_source_titles(
            text, required_paper_keys=(multi.paper_key,),
        ),
    )
    wrong_study = " ".join((
        multi.title,
        multi.finding.replace("(n=24)", "(n=31)", 1).replace(
            "The treatment", "Study B (n=31) reports that the treatment", 1,
        ),
        multi.limitation,
        multi.condition_description.replace("Study A tested", "Study B tested", 1),
    ))
    wrong_magnitude = positive_text.replace("by 1 points", "by 99 points", 1)
    wrong_condition = positive_text.replace(
        multi.condition_description,
        "Study A tested a condition absent from this source.",
        1,
    )
    for altered, expected_issue in (
        (wrong_study, "source_fact_not_unique_at_locator"),
        (wrong_magnitude, "effect_magnitude_mismatch"),
        (wrong_condition, "condition_omitted_or_mismatched"),
    ):
        verdict = assertion(altered)
        assert verdict.judgment == "unsupported", verdict
        assert expected_issue in verdict.issue_codes, verdict


class _ScaleOutputCapacityExceeded(RuntimeError):
    def __init__(self, *, requested: int, required: int) -> None:
        self.requested = requested
        self.required = required
        super().__init__(
            f"simulated completion requires {required} output tokens but request cap is {requested}"
        )


def _requested_fixture_output_cap(payload: Mapping[str, Any]) -> int:
    for field in ("max_tokens", "max_completion_tokens"):
        value = payload.get(field)
        if type(value) is int and value > 0:
            return value
    raise AssertionError("local provider request omitted a positive output-token cap")


def _reader_content(paper: ScalePaper) -> dict[str, Any]:
    finding = paper.finding
    if paper.chinese_note:
        finding = f"{finding} {paper.chinese_note}"
    summary = _reader_summary(paper.paper_key, paper.title, finding)
    core = summary.get("core_analysis")
    if isinstance(core, dict):
        limitations = paper.limitation
        if paper.condition_description:
            limitations += f" Source-reported condition boundary: {paper.condition_description}"
        core.update(
            {
                "summary": f"{paper.title}: {finding}",
                "key_points": [finding],
                "methodology": (
                    "Controlled empirical comparison; "
                    f"{paper.condition_description + '; ' if paper.condition_description else ''}"
                    "30-day observation."
                ),
                "findings": finding,
                "conclusions": finding,
                "limitations": limitations,
                "relevance": None,
                "research_gap": None,
                "theoretical_framework": None,
                "future_research_directions": [],
            }
        )
    metadata = summary.get("paper_metadata")
    if isinstance(metadata, dict):
        # Repeated author/year combinations deliberately exercise identity
        # disambiguation without changing each source's DOI or canonical key.
        metadata["authors"] = ["Lee, Min"] if paper.index % 3 else ["Wang, Qian"]
        metadata["year"] = str(2021 + (paper.index % 4))
        metadata["doi"] = f"10.5555/runfirst.{paper.index:04d}"
    specialized = summary.get("specialized_details")
    empirical = specialized.get("empirical") if isinstance(specialized, dict) else None
    if isinstance(empirical, dict):
        empirical.update(
            {
                "data_source_and_size": paper.sample_description,
                "analysis_technique": (
                    f"{paper.condition_description}; "
                    if paper.condition_description
                    else ""
                ) + "The source reports the estimate in Table 2 (PDF page 2).",
                "sample_characteristics_or_context": "Observed controlled sample; 30-day follow-up.",
            }
        )
    paper_info = summary.get("paper_info")
    if isinstance(paper_info, dict):
        paper_info.update(
            {
                "canonical_paper_key": paper.paper_key,
                "source_paper_id": paper.paper_key,
                "title": paper.title,
                "authors": ["Lee, Min"] if paper.index % 3 else ["Wang, Qian"],
                "year": 2021 + (paper.index % 4),
                "doi": f"10.5555/runfirst.{paper.index:04d}",
            }
        )
    return summary


def _source_text_for_slot(
    slot: Mapping[str, Any],
    *,
    claims: Mapping[str, Mapping[str, Any]],
    fields: Mapping[str, Mapping[str, Any]],
) -> str:
    primary_id = str(slot.get("primary_claim_id") or "")
    primary = str(claims.get(primary_id, {}).get("text") or "").strip()
    if not primary:
        raise AssertionError(f"candidate slot has no actual source claim text: {slot}")
    pieces = [primary.rstrip(".。;； ")]
    for claim_id in sorted(str(item) for item in slot.get("source_claim_ids") or () if str(item)):
        if claim_id == primary_id:
            continue
        value = str(claims.get(claim_id, {}).get("text") or "").strip()
        if value and value.casefold() not in " ".join(pieces).casefold():
            pieces.append(value.rstrip(".。;； "))
    field_text: list[str] = []
    for field_id in sorted(str(item) for item in slot.get("source_field_ids") or () if str(item)):
        row = fields.get(field_id, {})
        value = str(row.get("source_value") or "").strip()
        if value and value.casefold() not in " ".join(pieces).casefold():
            label = str(row.get("canonical_field") or row.get("source_path") or "source boundary")
            field_text.append(f"{label.replace('_', ' ')}: {value.rstrip('.。;； ')}")
    if field_text:
        pieces.append("recorded qualifications: " + "; ".join(field_text))
    return "; ".join(pieces) + "."


_SOURCE_FUNCTION_LABELS = {
    "findings": "finding",
    "zero_results": "zero-result evidence",
    "limitations": "study limitation",
    "moderators_boundaries": "condition or moderator boundary",
    "theoretical_derivation": "theoretical derivation",
    "method": "method or design evidence",
    "operationalizations": "operationalization evidence",
    "concept_definitions": "construct definition",
    "research_questions": "research-question framing",
    "research_gaps": "source-reported research gap",
    "future_directions": "source-reported future direction",
}


def _noncomparison_paper_role(
    paper_slots: list[Mapping[str, Any]], task_dimensions: Mapping[str, set[str]],
) -> str:
    """Name one source function in its semantic task dimension, without IDs."""

    dimensions: set[str] = set()
    families: set[str] = set()
    for slot in paper_slots:
        task_id = str(slot.get("task_id") or "")
        dimensions.update(task_dimensions.get(task_id, set()))
        # The logical task ID's named dimension is a fallback when a compact
        # route projection omits the descriptor. Never surface its hash.
        task_parts = task_id.split(":")
        if (
            not task_dimensions.get(task_id)
            and len(task_parts) > 1
            and task_parts[0] == "topic_synthesis"
            and task_parts[1].casefold() in {
                "context", "theory", "method", "mechanism", "construct", "outlier", "corpus",
            }
        ):
            dimensions.add(task_parts[1].casefold())
        elif not task_dimensions.get(task_id) and task_id == "cross_group_comparison":
            dimensions.add("comparison")
        elif not task_dimensions.get(task_id) and task_id == "global_synthesis":
            dimensions.add("synthesis")
        primary_claim_id = str(slot.get("primary_claim_id") or "")
        claim_parts = primary_claim_id.rsplit(":", 2)
        if len(claim_parts) == 3 and claim_parts[1]:
            families.add(claim_parts[1].casefold())

    function_labels = sorted({
        _SOURCE_FUNCTION_LABELS.get(family, "source-reported claim")
        for family in families
    })
    source_function = " and ".join(function_labels or ["source-reported claim"])
    boundary_families = {"limitations", "moderators_boundaries"}
    focus = " and ".join(sorted(dimensions))
    if dimensions.intersection({"theory", "method", "context"}) and families and families.issubset(boundary_families):
        if {"theory", "method"}.issubset(dimensions):
            return f"Qualifies the method- and theory-focused discussion with its source-reported {source_function}."
        if "theory" in dimensions:
            return f"Bounds the theory-focused discussion with its source-reported {source_function}."
        if "method" in dimensions:
            return f"Qualifies the method-focused discussion with its source-reported {source_function}."
        return f"Defines context for the context-focused discussion through its source-reported {source_function}."
    if focus:
        return f"Contributes its source-reported {source_function} to the {focus}-focused discussion."
    return f"Contributes its source-reported {source_function} within its evidence scope."


def _compact_candidate(node_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    scope = request.get("candidate_output_scope")
    hierarchy = request.get("hierarchy")
    if not isinstance(scope, Mapping) or not isinstance(hierarchy, Mapping):
        raise TypeError("candidate fixture requires the real assigned finite output scope")
    group_ids = [str(value) for value in scope.get("claim_group_ids") or () if str(value)]
    assigned = [str(value) for value in hierarchy.get("claim_group_ids") or () if str(value)]
    if not group_ids or len(group_ids) != len(set(group_ids)) or set(group_ids) != set(assigned):
        raise AssertionError(f"candidate groups do not match durable shard assignment: {group_ids}, {assigned}")
    assignment = hierarchy.get("group_assignment")
    if isinstance(assignment, Mapping) and {str(key) for key in assignment} != set(group_ids):
        raise AssertionError("local candidate assignment map does not cover exactly the assigned groups")
    slots_by_index: dict[int, list[dict[str, Any]]] = {}
    for row in scope.get("claim_slots") or ():
        if isinstance(row, Mapping):
            slots_by_index.setdefault(int(row.get("claim_group_index", -1)), []).append(dict(row))
    shared = request.get("shared_semantic_context")
    tables = shared.get("interpretation_source_tables") if isinstance(shared, Mapping) else None
    if not isinstance(tables, Mapping):
        raise TypeError("candidate fixture lacks the projected verified source tables")
    claims = {
        str(row.get("claim_id") or ""): dict(row)
        for row in tables.get("source_claims") or ()
        if isinstance(row, Mapping) and str(row.get("claim_id") or "")
    }
    fields = {
        str(row.get("source_field_id") or ""): dict(row)
        for row in tables.get("source_fields") or ()
        if isinstance(row, Mapping) and str(row.get("source_field_id") or "")
    }
    evidence = {
        str(row.get("paper_key") or ""): str(row.get("title") or row.get("paper_key") or "the source")
        for row in request.get("evidence") or ()
        if isinstance(row, Mapping)
    }
    task_by_group = {
        str(row.get("claim_group_id") or ""): dict(row)
        for row in scope.get("organization_tasks") or ()
        if isinstance(row, Mapping) and str(row.get("claim_group_id") or "")
    }
    task_dimensions: dict[str, set[str]] = {}
    for route in shared.get("topic_routes") or ():
        if not isinstance(route, Mapping):
            continue
        task_id = str(route.get("logical_node_id") or "")
        dimensions = {
            str(value).strip().casefold()
            for value in route.get("dimensions") or ()
            if str(value).strip()
        }
        if task_id and dimensions:
            task_dimensions[task_id] = dimensions

    def compact_source_claim_text(slot: Mapping[str, Any]) -> str:
        """Use each exact supporting source claim once; the runtime restores fields."""

        primary_id = str(slot.get("primary_claim_id") or "")
        claim_ids = [
            primary_id,
            *sorted({
                str(value) for value in slot.get("source_claim_ids") or ()
                if str(value) and str(value) != primary_id
            }),
        ]
        pieces: list[str] = []
        for claim_id in claim_ids:
            claim = claims.get(claim_id)
            text = _source_attributed_paper_level_limitation(
                claim,
                selected_study_id=str(slot.get("study_id") or ""),
                selected_paper_key=str(slot.get("paper_key") or ""),
            ).strip() if claim else ""
            if not text:
                raise AssertionError(
                    f"compact candidate slot has no exact source claim text: {claim_id}"
                )
            current = "; ".join(pieces).casefold()
            concise = text.rstrip(".。;； ")
            if concise.casefold() not in current:
                pieces.append(concise)
        rendered = "; ".join(pieces).strip()
        rendered_folded = rendered.casefold()
        # The compact assertion cites only the distinct source claims in prose.
        # Every source-field value still has to be reflected in that claim text;
        # complete field and qualifier IDs are restored from the sealed slots.
        for field_id in slot.get("source_field_ids") or ():
            field = fields.get(str(field_id))
            value = str(field.get("source_value") or "").strip() if field else ""
            qualifier = value.rstrip(".。;； ")
            if not qualifier or qualifier.casefold() not in rendered_folded:
                raise AssertionError(
                    "compact candidate source claims do not express source field "
                    f"{field_id!r} as a complete qualifier"
                )
        return rendered + "."

    compact_claims: list[dict[str, Any]] = []
    relation_ids: set[str] = set()
    role_parts: dict[str, set[str]] = {}
    all_indices = list(range(len(group_ids)))
    required_indices = {
        int(value) for value in scope.get("required_claim_group_indices") or ()
        if type(value) is int
    }
    for group_index in all_indices:
        group_slots = slots_by_index.get(group_index) or []
        if not group_slots:
            raise AssertionError(f"assigned candidate group {group_index} has no source slots")
        task = task_by_group.get(group_ids[group_index], {})
        task_contract = task.get("task_contract")
        comparison = (
            task.get("claim_seed") is True
            and task.get("claim_kind") == "model_authored_relation_comparison_slot"
            and isinstance(task_contract, Mapping)
            and task_contract.get("relation_id_is_not_a_conclusion") is True
        )
        source_rows = [
            (str(slot.get("paper_key") or ""), compact_source_claim_text(slot))
            for slot in sorted(group_slots, key=lambda row: (str(row.get("paper_key") or ""), str(row.get("claim_slot_id") or "")))
        ]
        if comparison:
            paper_keys = list(dict.fromkeys(key for key, _text in source_rows))
            if len(paper_keys) != 2 or set(paper_keys) != set(task.get("paper_keys") or ()):
                raise AssertionError("required comparison does not retain both task-contract source sides")
            by_paper = {
                paper: "; ".join(text for key, text in source_rows if key == paper)
                for paper in paper_keys
            }
            text = (
                "Within the recorded source boundaries, "
                f"{evidence.get(paper_keys[0], paper_keys[0])} reports {by_paper[paper_keys[0]]} whereas "
                f"{evidence.get(paper_keys[1], paper_keys[1])} reports {by_paper[paper_keys[1]]}"
            )
            for paper in paper_keys:
                counterpart = next(key for key in paper_keys if key != paper)
                role_parts.setdefault(paper, set()).add(
                    "Compare its source-reported finding with "
                    f"{evidence.get(counterpart, counterpart)} within their recorded source boundaries."
                )
        else:
            text = "The included source records " + "; ".join(
                f"{evidence.get(paper, paper)}: {statement}" for paper, statement in source_rows
            )
            for paper in dict.fromkeys(key for key, _statement in source_rows):
                paper_slots = [
                    slot for slot in group_slots
                    if str(slot.get("paper_key") or "") == paper
                ]
                role_parts.setdefault(paper, set()).add(_noncomparison_paper_role(paper_slots, task_dimensions))
        compact_claims.append({"text": text, "claim_group_indices": [group_index]})
        relation_ids.update(
            str(value) for slot in group_slots for value in slot.get("relation_ids") or () if str(value)
        )
    returned_indices = {
        index for claim in compact_claims for index in claim["claim_group_indices"]
    }
    if not required_indices.issubset(returned_indices):
        raise AssertionError(f"candidate fixture omitted required comparison groups: {required_indices - returned_indices}")
    hierarchy_id = str(hierarchy.get("shard_id") or node_id.rsplit(":", 1)[-1])
    return {
        "candidate_wire_version": "candidate-slot-output/v1",
        "candidate_id": str(request.get("candidate_id") or "candidate"),
        "organizing_logic": str(request.get("organizing_logic") or "source evidence"),
        "sections": [
            {
                "section_id": f"{request.get('candidate_id', 'candidate')}_{hierarchy_id}",
                "title": "Source findings and recorded boundaries",
                "goal": "Organize the assigned source-supported claims and preserve their boundaries.",
                "rationale": "Every authored statement uses its complete assigned source group.",
                "relation_ids": sorted(relation_ids),
                "paper_roles": {paper: " ".join(sorted(parts)) for paper, parts in sorted(role_parts.items())},
                "claims": compact_claims,
            }
        ],
    }


class _MemorySampler:
    def __init__(self) -> None:
        try:
            import psutil  # type: ignore

            self._process = psutil.Process()
        except ImportError:
            self._process = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline_rss = self._rss()
        self.peak_rss = self.baseline_rss

    def _rss(self) -> int | None:
        return int(self._process.memory_info().rss) if self._process is not None else None

    def start(self) -> None:
        if self._process is None:
            return

        def sample() -> None:
            while not self._stop.wait(0.1):
                current = self._rss()
                if current is not None:
                    self.peak_rss = max(self.peak_rss, current)

        self._thread = threading.Thread(target=sample, name="rfs-rss-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        current = self._rss()
        if current is not None:
            self.peak_rss = max(self.peak_rss, current)


class _ScaleHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:
        server = self.server
        raw_request = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request_authority_started = time.perf_counter()
        call: dict[str, Any] = {
            "path": self.path,
            "request_bytes": len(raw_request),
            "request_sha256": hashlib.sha256(raw_request).hexdigest(),
            "stage": "received",
            "usage_source": "simulated_local_fixture",
        }
        request_authority_seconds: float | None = None
        fixture_response_started: float | None = None
        fixture_response_generation_seconds: float | None = None
        fixture_payload_builder_seconds = 0.0
        call_recorded = False
        timing_row: dict[str, Any] | None = None

        def timed_fixture_builder(builder: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            nonlocal fixture_payload_builder_seconds
            started = time.perf_counter()
            try:
                return builder(*args, **kwargs)
            finally:
                fixture_payload_builder_seconds += time.perf_counter() - started

        def record_call(error_message: str = "") -> None:
            nonlocal request_authority_seconds, fixture_response_generation_seconds
            nonlocal call_recorded, timing_row
            finished = time.perf_counter()
            if request_authority_seconds is None:
                authority_end = fixture_response_started if fixture_response_started is not None else finished
                request_authority_seconds = max(0.0, authority_end - request_authority_started)
            if fixture_response_started is not None and fixture_response_generation_seconds is None:
                fixture_response_generation_seconds = max(0.0, finished - fixture_response_started)
            with server.lock:  # type: ignore[attr-defined]
                if not call_recorded:
                    server.calls.append(call)  # type: ignore[attr-defined]
                    timing_row = {
                        "call_index": len(server.calls),  # type: ignore[attr-defined]
                        "stage": str(call.get("stage") or "unknown"),
                        "status": int(call.get("status") or 0),
                        "request_authority_seconds": round(float(request_authority_seconds or 0.0), 6),
                        "fixture_payload_builder_seconds": round(fixture_payload_builder_seconds, 6),
                        "fixture_response_generation_seconds": round(float(fixture_response_generation_seconds or 0.0), 6),
                    }
                    server.timing_calls.append(timing_row)  # type: ignore[attr-defined]
                    call_recorded = True
                elif timing_row is not None:
                    timing_row["stage"] = str(call.get("stage") or "unknown")
                    timing_row["status"] = int(call.get("status") or 0)
                if error_message and error_message not in server.errors:  # type: ignore[attr-defined]
                    server.errors.append(error_message)  # type: ignore[attr-defined]

        response_committed = False

        def record_transport_error(error_message: str, status: int, committed: bool) -> None:
            with server.lock:  # type: ignore[attr-defined]
                rows = server.transport_errors  # type: ignore[attr-defined]
                existing = next((row for row in rows if row.get("error") == error_message), None)
                if existing is None:
                    rows.append({
                        "error": error_message,
                        "statuses": [status],
                        "response_committed": committed,
                    })
                    return
                statuses = existing.setdefault("statuses", [])
                if status not in statuses:
                    statuses.append(status)
                existing["response_committed"] = bool(existing.get("response_committed")) or committed

        def write_http_response(status: int, body: bytes) -> None:
            nonlocal response_committed
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                response_committed = True
                self.wfile.write(body)
            except OSError as exc:
                record_transport_error(f"{type(exc).__name__}: {exc}", status, response_committed)

        try:
            payload = json.loads(raw_request)
            model = str(payload.get("model") or "")
            requested_output_cap = _requested_fixture_output_cap(payload)
            call["requested_output_tokens"] = requested_output_cap
            messages = payload.get("messages") or ()
            user_text = "\n".join(
                str(item.get("content") or "")
                for item in messages
                if isinstance(item, Mapping) and item.get("role") == "user"
            )
            call.update(
                {
                    "model": model,
                    "user_text_bytes": len(user_text.encode("utf-8")),
                    "user_text_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
                }
            )
            prompt_tokens = max(1, math.ceil(len(raw_request) / 4))
            input_content: Any

            def outline_content(node_id: str, request: Mapping[str, Any]) -> Any:
                call.update({"stage": "outline", "node_id": node_id})
                if (
                    request.get("schema_version") == "candidate-comparison-fragments-request/v1"
                    and server.source_label_map is not None  # type: ignore[attr-defined]
                ):
                    from tests.run_first_candidate_fragments_fixture import (
                        candidate_fragment_fixture_response,
                    )

                    wrapped = timed_fixture_builder(
                        candidate_fragment_fixture_response,
                        node_id,
                        request,
                        source_label_map=server.source_label_map,  # type: ignore[attr-defined]
                    )
                    if wrapped is None:
                        raise AssertionError("canonical source labels require the comparison-fragment fixture")
                    return wrapped
                if node_id == "relation_adjudication" or node_id.startswith("relation_adjudication:"):
                    relation_rows = [
                        dict(row) for row in request.get("relation_candidates") or ()
                        if isinstance(row, Mapping)
                    ]
                    bundle_rows = [
                        dict(row) for row in request.get("relation_evidence_bundles") or ()
                        if isinstance(row, Mapping)
                    ]
                    server.trace.append({  # type: ignore[attr-defined]
                        "kind": "scale_relation_adjudication_request",
                        "node_id": node_id,
                        "request_level": (
                            str((request.get("hierarchy") or {}).get("level") or "")
                            if isinstance(request.get("hierarchy"), Mapping)
                            else ""
                        ) or ("relation_atomic" if isinstance(request.get("relation_batch"), Mapping) else "flat"),
                        "relation_candidates": [
                            {
                                "relation_id": str(row.get("relation_id") or ""),
                                "paper_keys": [str(value) for value in row.get("paper_keys") or () if str(value)],
                            }
                            for row in relation_rows
                        ],
                        "relation_evidence_bundles": [
                            {
                                "relation_id": str(row.get("relation_id") or ""),
                                "paper_ids": [str(value) for value in row.get("paper_ids") or () if str(value)],
                                "claim_ids_left": [str(value) for value in row.get("claim_ids_left") or () if str(value)],
                                "claim_ids_right": [str(value) for value in row.get("claim_ids_right") or () if str(value)],
                                "findings_left": [str(value) for value in row.get("findings_left") or () if str(value)],
                                "findings_right": [str(value) for value in row.get("findings_right") or () if str(value)],
                                "contexts_and_boundaries": {
                                    str(key): [str(value) for value in values or () if str(value)]
                                    for key, values in (
                                        row.get("contexts_and_boundaries")
                                        if isinstance(row.get("contexts_and_boundaries"), Mapping)
                                        else {}
                                    ).items()
                                },
                                "source_locators": {
                                    str(key): [str(value) for value in values or () if str(value)]
                                    for key, values in (
                                        row.get("source_locators")
                                        if isinstance(row.get("source_locators"), Mapping)
                                        else {}
                                    ).items()
                                },
                                "required_evidence_ids": [str(value) for value in row.get("required_evidence_ids") or () if str(value)],
                                "provided_evidence_ids": [str(value) for value in row.get("provided_evidence_ids") or () if str(value)],
                                "missing_evidence_ids": [str(value) for value in row.get("missing_evidence_ids") or () if str(value)],
                                "evidence_completeness": str(row.get("evidence_completeness") or ""),
                            }
                            for row in bundle_rows
                        ],
                    })
                if node_id.startswith("topic_synthesis_provider"):
                    wrapped = timed_fixture_builder(_source_bound_topic_response, node_id, request, server.trace)  # type: ignore[attr-defined]
                    return wrapped["content"]
                if node_id.startswith(("cross_group_comparison_provider", "global_synthesis_provider")):
                    wrapped = timed_fixture_builder(_outline_provider_response, node_id, request, server.trace)  # type: ignore[attr-defined]
                    return wrapped["content"]
                if node_id == "relation_adjudication" or node_id.startswith("relation_adjudication:"):
                    wrapped = timed_fixture_builder(
                        _outline_provider_response,
                        "relation_adjudication",
                        request,
                        server.trace,  # type: ignore[attr-defined]
                    )
                    return wrapped["content"]
                if node_id.endswith("_provider_generation") or "_provider_generation:local:" in node_id:
                    return timed_fixture_builder(_compact_candidate, node_id, request)
                wrapped = timed_fixture_builder(
                    _outline_provider_response, node_id, request,
                    server.trace, getattr(server, "arbitration_fixture_state", None),
                )
                return wrapped["content"]

            if model == "rfs-reader-local":
                match = _RFS_ID.search(user_text)
                if match is None:
                    raise AssertionError("reader request omitted the actual synthetic PDF identifier")
                index = int(match.group(1))
                paper = server.papers[index]  # type: ignore[attr-defined]
                if paper.finding.casefold() not in " ".join(user_text.split()).casefold():
                    raise AssertionError("reader response cannot be grounded in this request's extracted PDF text")
                if paper.limitation.casefold() not in " ".join(user_text.split()).casefold():
                    raise AssertionError("reader request omitted the source limitation used by its response")
                normalized_user_text = " ".join(user_text.split()).casefold()
                for source_passage, label in (
                    (paper.condition_description, "distinct condition description"),
                    (paper.extended_source_english, "full wrapped synthetic source passage"),
                ):
                    if source_passage and " ".join(source_passage.split()).casefold() not in normalized_user_text:
                        raise AssertionError(f"reader request omitted the source's {label}")
                if paper.extended_source_chinese and (
                    "".join(paper.extended_source_chinese.split()).casefold()
                    not in "".join(user_text.split()).casefold()
                ):
                    raise AssertionError("reader request omitted the full mixed-language source passage")
                input_content = timed_fixture_builder(_reader_content, paper)
                call.update({"stage": "stage1_reader", "paper_index": index})
            elif model in {"rfs-outline-local", "rfs-relation-local"}:
                envelope = json.loads(user_text)
                node_id = str(envelope.get("node_id") or "")
                request = envelope.get("request")
                if not isinstance(request, Mapping):
                    raise AssertionError("outline HTTP request is not a node/request envelope")
                input_content = outline_content(node_id, request)
            elif model == "rfs-writer-local":
                envelope = json.loads(user_text)
                node_id = str(envelope.get("node_id") or "") if isinstance(envelope, Mapping) else ""
                outline_request = envelope.get("request") if isinstance(envelope, Mapping) else None
                # Dispatch by the serialized runtime envelope, not by the
                # provider model name: configuration may route an Outline
                # critique role through Writer_API. A true Stage 3 Writer
                # envelope remains recognized by its required finite scope.
                if (
                    isinstance(envelope, Mapping)
                    and "writer_task_scope" not in envelope
                    and node_id
                    and isinstance(outline_request, Mapping)
                ):
                    input_content = outline_content(node_id, outline_request)
                else:
                    wrapped = timed_fixture_builder(_source_bound_writer_response, envelope, server.trace)  # type: ignore[attr-defined]
                    if wrapped is None:
                        raise AssertionError("Writer request omitted production writer_task_scope")
                    input_content = wrapped["content"]
                    call.update({"stage": "stage3_writer"})
            elif model == "rfs-validator-local":
                input_content = {
                    "status": "supported",
                    "confidence": 0.99,
                    "repair_scope": "none",
                    "disposition": "keep_as_is",
                    "low_confidence": False,
                    "reasoning": "Loopback fixture response: the supplied source mapping is accepted.",
                    "repair_hint": "",
                    "summary_paper_ids": [],
                    "manual_review_reason": "",
                    "claim_type": "result",
                    "claim_type_confidence": 1.0,
                    "claim_type_rationale": "The bounded statement is an empirical result.",
                    "adjudication_status": "supported",
                }
                call.update({"stage": "stage4_validator"})
            else:
                raise AssertionError(f"unexpected provider model in local-only config: {model!r}")
            request_authority_seconds = time.perf_counter() - request_authority_started
            fixture_response_started = time.perf_counter()
            content_text = json.dumps(input_content, ensure_ascii=False)
            completion_tokens = max(1, math.ceil(len(content_text.encode("utf-8")) / 4))
            call["fixture_output_tokens"] = completion_tokens
            if completion_tokens > requested_output_cap:
                raise _ScaleOutputCapacityExceeded(
                    requested=requested_output_cap,
                    required=completion_tokens,
                )
            response = {
                "id": f"rfs-local-{len(server.calls) + 1}",  # type: ignore[attr-defined]
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            fixture_response_generation_seconds = time.perf_counter() - fixture_response_started
            call.update(
                {
                    "response_bytes": len(body),
                    "fixture_input_tokens": prompt_tokens,
                    "fixture_output_tokens": completion_tokens,
                    "requested_output_tokens": requested_output_cap,
                    "status": 200,
                    "stage": str(call.get("stage") or "response_sent"),
                }
            )
            record_call()
            write_http_response(200, body)
        except _ScaleOutputCapacityExceeded as exc:
            if fixture_response_started is None:
                fixture_response_started = time.perf_counter()
            body = json.dumps(
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "code": "max_output_tokens_exceeded",
                    }
                }
            ).encode("utf-8")
            call.update(
                {
                    "status": 400,
                    "error": str(exc),
                    "required_output_tokens": exc.required,
                    "requested_output_tokens": exc.requested,
                    "response_bytes": len(body),
                }
            )
            if fixture_response_generation_seconds is None:
                fixture_response_generation_seconds = time.perf_counter() - fixture_response_started
            record_call(call["error"])
            write_http_response(400, body)
        except (AssertionError, IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            if request_authority_seconds is None:
                request_authority_seconds = time.perf_counter() - request_authority_started
            if fixture_response_started is None:
                fixture_response_started = time.perf_counter()
            body = json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode("utf-8")
            call.update({"status": 500, "error": f"{type(exc).__name__}: {exc}", "response_bytes": len(body)})
            if fixture_response_generation_seconds is None:
                fixture_response_generation_seconds = time.perf_counter() - fixture_response_started
            record_call(call["error"])
            write_http_response(500, body)


def _start_scale_server(
    papers: list[ScalePaper], *, bind_port: int = 0,
    source_label_map: Mapping[str, str] | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    if type(bind_port) is not int or not 0 <= bind_port <= 65535:
        raise ValueError("loopback fixture port must be an integer from 0 through 65535")
    server = ThreadingHTTPServer(("127.0.0.1", bind_port), _ScaleHTTPHandler, bind_and_activate=False)
    server.request_queue_size = 64
    try:
        if bind_port:
            server.allow_reuse_address = False
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.papers = papers  # type: ignore[attr-defined]
    server.source_label_map = dict(source_label_map) if source_label_map is not None else None  # type: ignore[attr-defined]
    server.calls = []  # type: ignore[attr-defined]
    server.timing_calls = []  # type: ignore[attr-defined]
    server.transport_errors = []  # type: ignore[attr-defined]
    server.errors = []  # type: ignore[attr-defined]
    server.trace = []  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="rfs-local-http", daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/v1"


def test_scale_http_fixture_returns_capacity_error_instead_of_oversized_success() -> None:
    server, thread, api_base = _start_scale_server([])
    request_body = json.dumps(
        {
            "model": "rfs-validator-local",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "bounded output-cap test"}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        assert raised.value.code == 400
        response_error = json.loads(raised.value.read().decode("utf-8"))
        assert response_error["error"]["code"] == "max_output_tokens_exceeded"
        calls = list(server.calls)  # type: ignore[attr-defined]
        assert len(calls) == 1
        call = calls[0]
        assert call["status"] == 400
        assert call["requested_output_tokens"] == 1
        assert call["required_output_tokens"] > call["requested_output_tokens"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_scale_http_fixture_dispatches_outline_node_by_envelope_on_writer_model() -> None:
    server, thread, api_base = _start_scale_server([])
    request_body = json.dumps(
        {
            "model": "rfs-writer-local",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": json.dumps({
                    "node_id": "structure_critique:local:candidate_1:shard:1",
                    "request": {
                        "candidate_id": "candidate_1",
                        "node_id": "structure_critique",
                    },
                }),
            }],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = json.loads(payload["choices"][0]["message"]["content"])
        assert result["passed"] is True
        assert result["blocking_diagnostics"] == []
        calls = list(server.calls)  # type: ignore[attr-defined]
        assert len(calls) == 1
        assert calls[0]["model"] == "rfs-writer-local"
        assert calls[0]["stage"] == "outline"
        assert calls[0]["node_id"] == "structure_critique:local:candidate_1:shard:1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_scale_http_fixture_keeps_finite_writer_scope_mandatory() -> None:
    server, thread, api_base = _start_scale_server([])
    request_body = json.dumps(
        {
            "model": "rfs-writer-local",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": json.dumps({"candidate_id": "candidate_1"}),
            }],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        assert raised.value.code == 500
        payload = json.loads(raised.value.read().decode("utf-8"))
        assert "Writer request omitted production writer_task_scope" in payload["error"]["message"]
        calls = list(server.calls)  # type: ignore[attr-defined]
        assert len(calls) == 1
        assert calls[0]["model"] == "rfs-writer-local"
        assert calls[0]["error"] == "AssertionError: Writer request omitted production writer_task_scope"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _local_config(
    tmp_path: Path,
    api_base: str,
    *,
    provider_call_limit: int | None = None,
    admission_overrides: Mapping[str, int] | None = None,
    technical_shard_target_tokens: int = 0,
    outline_output_tokens: int | None = None,
) -> Path:
    if (
        type(technical_shard_target_tokens) is not int
        or technical_shard_target_tokens < 0
        or technical_shard_target_tokens not in {0, 8_000}
    ):
        raise AssertionError("scale fixture only supports the existing 0 or minimum 8000 token shard target")
    if outline_output_tokens is not None and (
        type(outline_output_tokens) is not int or outline_output_tokens <= 0
    ):
        raise AssertionError("local Outline output allowance must be a positive integer")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Paths"]["library_path"] = ""
    parser["Paths"]["zotero_report"] = ""
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
    parser["Stage1_Input"]["send_extracted_text"] = "true"
    parser["Stage1_Input"]["send_selected_visuals"] = "false"
    parser["Stage1_Input"]["send_original_pdf"] = "never"
    parser["Stage1_Visual"]["enabled"] = "false"
    routes = {
        "Primary_Reader_API": "rfs-reader-local",
        "Backup_Reader_API": "rfs-reader-local",
        "Outline_API": "rfs-outline-local",
        "Writer_API": "rfs-writer-local",
        "Free_Mode_API": "rfs-relation-local",
        "Validator_API": "rfs-validator-local",
    }
    for section in parser.sections():
        if "api_base" not in parser[section] and section not in routes:
            continue
        settings = parser[section]
        settings["api_key"] = "local-run-first-fixture-only"
        settings["api_base"] = api_base
        settings["endpoint_type"] = "chat_completions"
        settings["provider_family"] = "generic"
        settings["transport_retries"] = "0"
        settings["thinking"] = ""
        settings["reasoning_effort"] = ""
        settings["reasoning_display"] = ""
        settings.setdefault("max_context_tokens", "128000")
        if section in routes:
            settings["model"] = routes[section]
    if outline_output_tokens is not None:
        # An explicit loopback experiment setting, not a product default or
        # evidence of any external gateway's output capability.
        parser["Outline_API"]["max_output_tokens"] = str(outline_output_tokens)
    parser["Outline"]["require_explicit_adoption"] = "true"
    parser["Outline"]["technical_shard_target_tokens"] = str(technical_shard_target_tokens)
    parser["OutlineStability"]["mode"] = "off"
    if provider_call_limit is not None:
        parser["OutlineStability"]["max_provider_calls"] = str(provider_call_limit)
    for key, value in (admission_overrides or {}).items():
        if key != "max_estimated_total_tokens" or type(value) is not int or value <= 0:
            raise AssertionError("unsupported local scale admission override")
        parser["OutlineStability"][key] = str(value)
    parser["Validation"]["review_enabled"] = "true"
    parser["Validation"]["repair_policy"] = "report_only"
    destination = tmp_path / "local-http-only.ini"
    with destination.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return destination


def _scale_provider_budget(size: int, source_kind: str, *, expect_blocked: bool) -> dict[str, Any] | None:
    if expect_blocked:
        return None
    raw = os.environ.get("RUN_FIRST_SCALE_PROVIDER_BUDGET_JSON", "").strip()
    budget_file = os.environ.get("RUN_FIRST_SCALE_PROVIDER_BUDGET_FILE", "").strip()
    if raw and budget_file:
        raise AssertionError(
            "set only one of RUN_FIRST_SCALE_PROVIDER_BUDGET_JSON or RUN_FIRST_SCALE_PROVIDER_BUDGET_FILE"
        )
    if raw:
        payload = json.loads(raw)
        return ProviderAggregateBudgetV2.from_mapping(payload).to_dict()
    if budget_file:
        payload = json.loads(Path(budget_file).expanduser().resolve().read_text(encoding="utf-8"))
        return ProviderAggregateBudgetV2.from_mapping(payload).to_dict()
    if size == 10 and source_kind == "synthetic_pdf_cold":
        # This is the existing isolated loopback-fixture allowance, not a
        # production default or a provider spending authorization.
        return ProviderAggregateBudgetV2.from_mapping(
            _FOUR_PAPER_LOOPBACK_BUDGET
        ).to_dict()
    raise AssertionError(
        "later scale sizes require an explicit finite RUN_FIRST_SCALE_PROVIDER_BUDGET_JSON or _FILE"
    )


def _load_r1_typed_sources(repo_root: Path) -> dict[str, Any]:
    """Resolve the already-reconciled 63 Stage 1 authorities without copying PDFs."""

    manifest_path = Path(
        os.environ.get(
            "RUN_FIRST_SCALE_SOURCE_MANIFEST",
            str(repo_root / "acceptance_artifacts" / "run_first_20261004" / "r1_inputs" / "R1_ZERO_POST_SOURCE_MANIFEST_63.json"),
        )
    ).expanduser().resolve()
    if not manifest_path.is_file():
        raise AssertionError(f"R1 63-source manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "r1-zero-post-source-manifest-v1"
        or int(manifest.get("authorized_reuse_summary_count") or 0) != 63
        or int(manifest.get("original_pdf_sha256_match_count") or 0) != 63
    ):
        raise AssertionError("R1 source manifest does not authenticate the original 63-paper typed reuse set")
    items = [row for row in manifest.get("items") or () if isinstance(row, Mapping)]
    if len(items) != 63 or len({str(row.get("canonical_paper_key") or "") for row in items}) != 63:
        raise AssertionError("R1 source manifest has an incomplete or non-unique 63-paper mapping")
    summary_paths: list[str] = []
    for item in items:
        summary_path = Path(str(item.get("summary_manifest_path") or "")).expanduser().resolve()
        if not summary_path.is_file():
            raise AssertionError(f"typed Stage 1 reuse manifest is missing: {summary_path}")
        with summary_path.open("rb") as handle:
            summary_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if summary_digest != str(item.get("summary_manifest_file_sha256") or ""):
            raise AssertionError(f"typed Stage 1 reuse manifest hash changed: {summary_path}")
        source_path = Path(str(item.get("source_pdf_path") or item.get("pdf_path") or "")).expanduser()
        if not source_path.is_file():
            raise AssertionError(f"original R1 PDF is missing: {source_path}")
        with source_path.open("rb") as handle:
            source_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if source_digest != str(item.get("actual_pdf_sha256") or ""):
            raise AssertionError(f"original R1 PDF changed since source reconciliation: {source_path}")
        summary_paths.append(str(summary_path))

    historical_spec_path = Path(str(manifest.get("historical_runtime_spec_path") or "")).expanduser()
    if not historical_spec_path.is_file():
        raise AssertionError(f"R1 historical RuntimeJobSpec is missing: {historical_spec_path}")
    with historical_spec_path.open("rb") as handle:
        spec_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if spec_digest != str(manifest.get("historical_runtime_spec_sha256") or ""):
        raise AssertionError("R1 historical RuntimeJobSpec hash differs from its source manifest")
    historical = json.loads(historical_spec_path.read_text(encoding="utf-8"))
    source = historical.get("source") if isinstance(historical, Mapping) else None
    metadata = historical.get("metadata") if isinstance(historical, Mapping) else None
    if not isinstance(source, Mapping) or not isinstance(metadata, Mapping):
        raise TypeError("R1 historical RuntimeJobSpec has no source or metadata contract")
    report = Path(str(source.get("zotero_report") or "")).expanduser()
    library = Path(str(source.get("library_path") or "")).expanduser()
    if not report.is_file() or not library.is_dir():
        raise AssertionError("R1 source report or linked PDF library is unavailable")
    with report.open("rb") as handle:
        report_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if report_digest != str(manifest.get("source_report_sha256") or ""):
        raise AssertionError("R1 source report hash differs from the reconciled manifest")
    intent = metadata.get("review_intent")
    if not isinstance(intent, Mapping):
        raise TypeError("R1 source RuntimeJobSpec does not contain its review intent")
    return {
        "manifest_path": str(manifest_path),
        "summary_paths": summary_paths,
        "zotero_report": str(report),
        "library_path": str(library),
        "review_intent": dict(intent),
        "mapped_pdf_count": len(items),
        "pdf_sha256_verified_count": 63,
        "typed_manifest_sha256_verified_count": 63,
        "source_report_sha256": report_digest,
        "historical_spec_sha256": spec_digest,
    }


def _assert_local_only(server: ThreadingHTTPServer) -> None:
    assert not server.transport_errors, server.transport_errors  # type: ignore[attr-defined]
    assert not server.errors, server.errors  # type: ignore[attr-defined]
    calls = list(server.calls)  # type: ignore[attr-defined]
    assert calls, "the production runtime made no HTTP POST to the loopback fixture"
    assert all(
        call.get("path", "").endswith("/chat/completions") and call.get("status") == 200
        for call in calls
    ), calls[:10]
    assert all(
        type(call.get("requested_output_tokens")) is int
        and int(call.get("fixture_output_tokens") or 0) <= int(call["requested_output_tokens"])
        for call in calls
    ), "local fixture returned an output larger than the request's declared cap"
    assert {str(call.get("model") or "") for call in calls} <= {
        "rfs-reader-local",
        "rfs-outline-local",
        "rfs-relation-local",
        "rfs-writer-local",
        "rfs-validator-local",
    }


def _local_http_handler_timing_metrics(server: ThreadingHTTPServer) -> dict[str, Any]:
    with server.lock:  # type: ignore[attr-defined]
        rows = [dict(row) for row in server.timing_calls]  # type: ignore[attr-defined]
        server_call_count = len(server.calls)  # type: ignore[attr-defined]
        transport_errors = [dict(row, statuses=list(row["statuses"])) for row in server.transport_errors]  # type: ignore[attr-defined]
    by_stage: dict[str, dict[str, Any]] = {}
    timing_fields = (
        "request_authority_seconds",
        "fixture_payload_builder_seconds",
        "fixture_response_generation_seconds",
    )
    for row in rows:
        stage = str(row.get("stage") or "unknown")
        summary = by_stage.setdefault(stage, {"calls": 0, **{f"{field}_total": 0.0 for field in timing_fields}})
        summary["calls"] += 1
        for field in timing_fields:
            summary[f"{field}_total"] += float(row.get(field) or 0.0)
    for summary in by_stage.values():
        for field in timing_fields:
            summary[f"{field}_total"] = round(summary[f"{field}_total"], 6)
    return {
        "schema_version": "run-first-scale-loopback-handler-timing/v1",
        "server_host": str(server.server_address[0]),
        "call_count": len(rows),
        "server_call_count": server_call_count,
        "transport_errors": transport_errors,
        "request_authority_boundary": (
            "after the complete HTTP body is read and before request hashing/call metadata through request/model/cap/source-bound checks and input_content construction; "
            "includes source-bound helper calls"
        ),
        "fixture_payload_builder_seconds_boundary": (
            "non-additive subphase inside request_authority_seconds; includes the local deterministic content builders"
        ),
        "fixture_response_generation_boundary": (
            "input_content ready through output-cap accounting and OpenAI-compatible JSON body encoding; excludes socket read/write"
        ),
        "per_call": rows,
        "by_stage": by_stage,
    }


def _write_measurement(path: Path | None, payload: Mapping[str, Any]) -> None:
    measurement = dict(payload)
    if str(payload.get("source_kind") or "").startswith("synthetic_pdf_"):
        measurement["source_generator_revision"] = (
            _SYNTHETIC_SOURCE_GENERATOR_REVISION
            if str(payload.get("source_variant") or "baseline") == "baseline"
            else _SYNTHETIC_ADVERSARIAL_VARIANT_REVISION
        )
    serialized = json.dumps(measurement, ensure_ascii=False, indent=2, sort_keys=True)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized + "\n", encoding="utf-8")
    print("RUN_FIRST_SCALE_METRICS=" + json.dumps(measurement, ensure_ascii=False, sort_keys=True))


_DIAGNOSTIC_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization)\s*[:=]\s*(?:bearer\s+)?([^\s,;]+)|bearer\s+([^\s,;]+)"
)


def _diagnostic_text(value: Any, *, limit: int = 1_000) -> str:
    text = str(value or "")
    text = text.replace("local-run-first-fixture-only", "<redacted>")
    text = text.replace("loopback-writer-fixture-key", "<redacted>")
    return _DIAGNOSTIC_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}=<redacted>" if match.group(1) else "Bearer <redacted>",
        text,
    )[:limit]


def _scale_failure_diagnostic_path(
    tmp_path: Path, *, size: int, source_kind: str, source_variant: str = "baseline",
) -> Path:
    root = Path(
        os.environ.get("RUN_FIRST_SCALE_EVIDENCE_DIR", "").strip()
        or (tmp_path / "failure-diagnostics")
    ).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    nonce = hashlib.sha256(str(time.time_ns()).encode("ascii")).hexdigest()[:8]
    variant_suffix = "" if source_variant == "baseline" else f"-{source_variant}"
    return root / f"run-first-scale-{size}-{source_kind}{variant_suffix}-{stamp}-{nonce}.failure.json"


def _scale_failure_snapshot(
    *,
    control: ReviewControlPlane | None,
    job_id: str,
    first: Any,
    plan: Mapping[str, Any] | None,
    plan_seconds: float | None,
    server: ThreadingHTTPServer,
    source_kind: str,
    source_variant: str,
    source_receipt: Mapping[str, Any],
    size: int,
    exception: BaseException,
    latest_execution_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture plan, Registry, receipts and loopback metadata without wire text."""

    def rows(values: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {
                key: _diagnostic_text(row[key]) if isinstance(row[key], str) else row[key]
                for key in keys if row.get(key) is not None
            }
            for row in values or () if isinstance(row, Mapping)
        ]

    workspace = str(getattr(first, "workspace_path", "") or "")
    inspection: Mapping[str, Any] = {}
    inspect_error = ""
    if control is not None:
        try:
            inspection = control.inspect(workspace=workspace or None, job_id=None if workspace else job_id)
        except Exception as exc:  # noqa: BLE001 - preserve the original test failure
            inspect_error = f"{type(exc).__name__}: {_diagnostic_text(exc)}"

    full = (plan or {}).get("full_stage_request_plan") or {}
    full = full if isinstance(full, Mapping) else {}
    receipt_rows = (inspection.get("provider_receipts") or {}).get("entries") or ()
    outline = inspection.get("outline_v3") or {}
    outline = outline if isinstance(outline, Mapping) else {}
    replay = outline.get("replay") or {}
    replay = replay if isinstance(replay, Mapping) else {}
    result_fields = (
        "job_status", "job_disposition", "canonical_ready", "failed_stage",
        "completed_stages", "completion_status", "completion_reasons",
        "completion_evidence_hash", "workspace_path", "message",
    )
    result = {
        key: (
            [_diagnostic_text(value) for value in value]
            if key in {"completed_stages", "completion_reasons"}
            else _diagnostic_text(value)
            if isinstance(value, str)
            else value
        )
        for key in result_fields
        if (value := getattr(first, key, None)) is not None
    } if first is not None else None
    return {
        "schema_version": "run-first-scale-failure-diagnostic/v1",
        "size": size,
        "source_kind": source_kind,
        "source_variant": source_variant,
        "source_generator_revision": (
            _SYNTHETIC_SOURCE_GENERATOR_REVISION
            if source_kind.startswith("synthetic_pdf_") and source_variant == "baseline"
            else _SYNTHETIC_ADVERSARIAL_VARIANT_REVISION
            if source_kind.startswith("synthetic_pdf_")
            else None
        ),
        "job_id": job_id,
        "exception": {"type": type(exception).__name__, "message": _diagnostic_text(exception)},
        "runner_result": result,
        "latest_execution_result": {
            key: _diagnostic_text(value) if isinstance(value, str) else value
            for key in (*result_fields, "status")
            if latest_execution_result is not None and (value := latest_execution_result.get(key)) is not None
        },
        "plan": {
            "seconds": plan_seconds,
            "status": (plan or {}).get("status"),
            "provider_admission_status": (plan or {}).get("provider_admission_status"),
            "ready_for_provider_admission": (plan or {}).get("ready_for_provider_admission"),
            "budget_status": full.get("budget_status"),
            "totals": full.get("totals"),
            "request_count": len(full.get("provider_requests") or ()),
            "unknown_exposures": rows(
                full.get("unknown_exposures"),
                ("stage_name", "semantic_role", "reason", "conditional_on", "logical_calls_upper_bound"),
            ),
        },
        "source_counts": {
            key: source_receipt.get(key)
            for key in ("mapped_pdf_count", "pdf_sha256_verified_count", "typed_manifest_sha256_verified_count")
            if source_receipt.get(key) is not None
        },
        "workspace": {
            "inspect_error": inspect_error,
            "status": inspection.get("status"),
            "registry_revision": inspection.get("registry_revision"),
            "issues": [_diagnostic_text(value) for value in inspection.get("issues") or ()],
            "artifacts": rows(inspection.get("artifacts"), ("artifact_id", "artifact_type", "path", "status", "content_hash")),
            "stage_terminals": rows(inspection.get("stage_terminals"), ("stage_name", "status", "terminal_reason", "model_call_count")),
            "provider_receipts": rows(
                receipt_rows,
                ("receipt_id", "stage_name", "route", "model", "status", "error_kind", "attempts", "input_tokens", "output_tokens", "total_tokens"),
            ),
            "outline": {
                "failed_node_ids": outline.get("failed_node_ids") or [],
                "completed_node_count": len(outline.get("completed_node_ids") or ()),
                "replay_count": replay.get("count"),
            },
        },
        "loopback": {
            "http_post_count": len(getattr(server, "calls", []) or []),
            "calls": rows(
                getattr(server, "calls", []),
                (
                    "path", "request_bytes", "request_sha256", "stage", "model",
                    "user_text_bytes", "user_text_sha256", "node_id", "paper_index",
                    "requested_output_tokens", "fixture_output_tokens",
                    "required_output_tokens", "response_bytes", "status", "error",
                ),
            ),
            "errors": [_diagnostic_text(value) for value in getattr(server, "errors", []) or ()],
            "relation_adjudication_requests": [
                dict(row)
                for row in getattr(server, "trace", []) or ()
                if isinstance(row, Mapping) and row.get("kind") == "scale_relation_adjudication_request"
            ],
        },
    }


def _scale_add_failure_loopback_extensions(
    snapshot: dict[str, Any], extensions: Mapping[str, Any],
) -> dict[str, Any]:
    loopback = snapshot.get("loopback")
    if not isinstance(loopback, Mapping):
        raise AssertionError("existing scale failure snapshot has no loopback object")
    snapshot["loopback"] = {**dict(loopback), **dict(extensions)}
    return snapshot


def _ready_outline_node_payload(registry: Any, node_id: str) -> dict[str, Any]:
    record = registry.get(f"outline-v3:{node_id}")
    if record is None or record.status != "ready":
        raise AssertionError(f"READY Outline v3 node output is missing: {node_id}")
    registry.verify_ready_artifact_closure(record)
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    node_payload = payload.get("payload") if isinstance(payload, Mapping) else None
    if not isinstance(node_payload, Mapping):
        raise TypeError(f"Outline node payload is malformed: {node_id}")
    return dict(node_payload)


def _scale_organization_layout_signature(organization_map: Mapping[str, Any]) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(int(value) for value in section.get("fragment_indices") or ())
        for section in organization_map.get("sections") or ()
    )


def _scale_global_organization_map_signature(
    request: Mapping[str, Any], organization_map: Mapping[str, Any],
) -> tuple[Any, ...]:
    partition = request.get("partition")
    if isinstance(partition, Mapping):
        local_to_global = [int(value) for value in partition.get("local_to_global_group_indices") or ()]
        full_count = int(partition.get("full_bank_group_count") or 0)
        if len(local_to_global) != int(request.get("bank_group_count") or 0):
            raise AssertionError("partition organization map does not match its local-to-global index table")
    else:
        full_count = int(request.get("bank_group_count") or 0)
        local_to_global = list(range(full_count))
    if full_count <= 0 or len(set(local_to_global)) != len(local_to_global):
        raise AssertionError("organization map has an invalid global fragment index space")

    def global_index(local_index: Any) -> int:
        if type(local_index) is not int or not 0 <= local_index < len(local_to_global):
            raise AssertionError("organization map references a fragment outside its request partition")
        return local_to_global[local_index]

    paper_keys = [str(value) for value in request.get("paper_keys") or ()]
    sections = []
    for section in organization_map.get("sections") or ():
        if not isinstance(section, Mapping):
            raise AssertionError("organization map section is not an object")
        global_fragments = tuple(global_index(value) for value in section.get("fragment_indices") or ())
        roles = []
        for role in section.get("paper_roles") or ():
            if (
                not isinstance(role, Sequence) or isinstance(role, (str, bytes)) or len(role) != 2
                or type(role[0]) is not int or not 0 <= role[0] < len(paper_keys)
            ):
                raise AssertionError("organization map role is outside its local paper/fragment tables")
            roles.append((paper_keys[role[0]], global_index(role[1])))
        sections.append((
            global_fragments,
            int(section.get("metadata_index") or 0),
            tuple(sorted(roles)),
        ))
    deferred = []
    for row in organization_map.get("deferred") or ():
        if not isinstance(row, Mapping):
            raise AssertionError("organization map deferred row is not an object")
        deferred.append((
            str(row.get("reason_code") or ""),
            tuple(global_index(value) for value in row.get("fragment_indices") or ()),
        ))
    return full_count, tuple(sections), tuple(deferred)


def _scale_aggregate_partition_layouts(signatures: Sequence[tuple[Any, ...]]) -> tuple[Any, ...]:
    if not signatures:
        raise AssertionError("partitioned organization layout has no provider parts")
    full_counts = {int(signature[0]) for signature in signatures}
    if len(full_counts) != 1:
        raise AssertionError("partitioned organization responses disagree on the full fragment bank size")
    full_count = next(iter(full_counts))
    sections = [section for signature in signatures for section in signature[1]]
    deferred = [row for signature in signatures for row in signature[2]]
    covered = [index for section in sections for index in section[0]]
    covered.extend(index for _reason, indices in deferred for index in indices)
    if len(covered) != len(set(covered)) or set(covered) != set(range(full_count)):
        raise AssertionError("partitioned organization maps do not account for each global fragment exactly once")
    return (
        full_count,
        tuple(sorted(sections, key=repr)),
        tuple(sorted(deferred, key=repr)),
    )


def _scale_required_comparison_groups_by_pair(
    organization_requests: Sequence[Mapping[str, Any]],
    fragment_bank: Mapping[str, Any],
    required_pairs: Sequence[Sequence[str]],
) -> dict[tuple[str, ...], tuple[dict[str, Any], ...]]:
    expected_pairs = {tuple(sorted(str(value) for value in pair)) for pair in required_pairs}
    bank_groups = [row for row in fragment_bank.get("groups") or () if isinstance(row, Mapping)]
    bank_groups_by_index: dict[int, Mapping[str, Any]] = {}
    for entry in bank_groups:
        bank_index = entry.get("index")
        if type(bank_index) is not int or bank_index in bank_groups_by_index:
            raise AssertionError("fragment bank has a missing or duplicate declared group index")
        bank_groups_by_index[bank_index] = entry
    if not organization_requests:
        raise AssertionError("required comparison selection has no ready organization requests")
    full_counts: set[int] = set()
    for request in organization_requests:
        partition = request.get("partition")
        full_count = (
            int(partition.get("full_bank_group_count") or 0)
            if isinstance(partition, Mapping)
            else int(request.get("bank_group_count") or 0)
        )
        if full_count <= 0:
            raise AssertionError("required comparison request has no positive full bank group count")
        full_counts.add(full_count)
    if len(full_counts) != 1:
        raise AssertionError("organization partitions disagree on the full bank group count")
    full_bank_group_count = next(iter(full_counts))
    if set(bank_groups_by_index) != set(range(full_bank_group_count)):
        raise AssertionError("ready fragment bank declared indices do not cover its exact full index range")
    bank_hash = str(fragment_bank.get("content_hash") or "")
    if not bank_hash:
        raise AssertionError("ready fragment bank has no content hash")
    groups_by_pair: dict[tuple[str, ...], dict[str, dict[str, Any]]] = {}
    for request in organization_requests:
        if str(request.get("bank_hash") or "") != bank_hash:
            raise AssertionError("required comparison request is not bound to the ready fragment bank")
        partition = request.get("partition")
        if isinstance(partition, Mapping):
            if str(partition.get("full_bank_hash") or "") != bank_hash:
                raise AssertionError("required comparison partition is bound to another full fragment bank")
            raw_local_to_global = partition.get("local_to_global_group_indices") or ()
            if any(type(value) is not int for value in raw_local_to_global):
                raise AssertionError("required comparison partition contains a noninteger global index")
            local_to_global = list(raw_local_to_global)
            if len(local_to_global) != int(request.get("bank_group_count") or 0):
                raise AssertionError("required comparison request has a malformed partition index table")
            if any(not 0 <= value < full_bank_group_count for value in local_to_global):
                raise AssertionError("required comparison partition maps outside the declared full bank")
            if len(set(local_to_global)) != len(local_to_global):
                raise AssertionError("required comparison partition repeats a global bank index")
        else:
            if int(request.get("bank_group_count") or 0) != full_bank_group_count:
                raise AssertionError("direct required comparison request differs from the full bank size")
            local_to_global = list(range(full_bank_group_count))
        required_local_indices = {
            value for value in request.get("required_group_indices") or () if type(value) is int
        }
        if len(required_local_indices) != len(request.get("required_group_indices") or ()):
            raise AssertionError("required_group_indices contains invalid or duplicate local indices")
        if any(not 0 <= value < len(local_to_global) for value in required_local_indices):
            raise AssertionError("required_group_indices references a fragment outside its request")
        expected_local_comparison_indices = {
            local_index
            for local_index in required_local_indices
            if bank_groups_by_index[local_to_global[local_index]]
            .get("claim_group", {}).get("claim_seed") is True
        }
        observed_rows = list(request.get("required_comparison_sections") or ())
        if any(
            not isinstance(row, Mapping) or type(row.get("group_index")) is not int
            for row in observed_rows
        ):
            raise AssertionError("required_comparison_sections contains a malformed local group index")
        observed_local_comparison_indices = {row["group_index"] for row in observed_rows}
        if len(observed_local_comparison_indices) != len(observed_rows):
            raise AssertionError("required_comparison_sections repeats a local group index")
        if observed_local_comparison_indices != expected_local_comparison_indices:
            raise AssertionError(
                "required_comparison_sections do not exactly match the seeded required group indices"
            )
        for section in request.get("required_comparison_sections") or ():
            if not isinstance(section, Mapping):
                raise AssertionError("required comparison section metadata is not an object")
            local_index = section.get("group_index")
            if type(local_index) is not int or not 0 <= local_index < len(local_to_global):
                raise AssertionError("required comparison index is outside its request partition")
            if local_index not in required_local_indices:
                raise AssertionError("required comparison section is absent from required_group_indices")
            global_index = local_to_global[local_index]
            entry = bank_groups_by_index.get(global_index)
            if entry is None:
                raise AssertionError("required comparison index is outside the ready fragment bank")
            group = entry.get("claim_group") if isinstance(entry.get("claim_group"), Mapping) else None
            if not isinstance(group, Mapping) or group.get("claim_seed") is not True:
                raise AssertionError("required comparison index does not point to a seeded claim group")
            if str(group.get("claim_kind") or "") != "model_authored_relation_comparison_slot":
                raise AssertionError("required comparison index is not a comparison slot group")
            group_task_contract = group.get("task_contract")
            if not isinstance(group_task_contract, Mapping):
                raise AssertionError("required comparison bank group has no task contract")
            group_task_id = str(group.get("task_id") or "")
            group_relation_id = str(group_task_contract.get("relation_id") or "")
            if (
                not group_task_id
                or str(group_task_contract.get("task_id") or "") != group_task_id
                or not group_relation_id
                or group_relation_id not in {str(value) for value in group.get("relation_ids") or ()}
            ):
                raise AssertionError("required comparison bank task/relation identity is inconsistent")
            group_pair = tuple(sorted(str(value) for value in group.get("paper_keys") or ()))
            if group_pair not in expected_pairs:
                raise AssertionError("organization request marks an unexpected paper pair as required")
            request_papers = [str(value) for value in request.get("paper_keys") or ()]
            section_paper_indices = section.get("paper_indices") or ()
            if any(type(value) is not int or not 0 <= value < len(request_papers) for value in section_paper_indices):
                raise AssertionError("required comparison paper index is outside its request table")
            if {request_papers[value] for value in section_paper_indices} != set(group_pair):
                raise AssertionError("required comparison request pair differs from its bank group identity")
            group_id = str(group.get("claim_group_id") or "")
            if not group_id:
                raise AssertionError("required comparison group has no stable claim_group_id")
            group_task_contract = group.get("task_contract")
            if not isinstance(group_task_contract, Mapping):
                raise AssertionError("required comparison group has no task contract")
            if str(group_task_contract.get("task_id") or "") != str(group.get("task_id") or ""):
                raise AssertionError("required comparison task contract differs from its bank task ID")
            stored = {**dict(group), "bank_group_index": global_index}
            existing = groups_by_pair.setdefault(group_pair, {}).get(group_id)
            if existing is not None and existing != stored:
                raise AssertionError("same required comparison group changed across organization partitions")
            groups_by_pair[group_pair][group_id] = stored
    missing_pairs = expected_pairs - set(groups_by_pair)
    if missing_pairs:
        raise AssertionError(f"organization requests omit required comparison pairs: {sorted(missing_pairs)}")
    return {
        pair: tuple(groups_by_pair[pair][group_id] for group_id in sorted(groups_by_pair[pair]))
        for pair in sorted(expected_pairs)
    }


def _scale_assert_required_comparison_closures(
    required_groups_by_pair: Mapping[tuple[str, ...], Sequence[Mapping[str, Any]]],
    scope_payload: Mapping[str, Any],
    candidate_sections: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    scope_groups = {
        str(group.get("claim_group_id") or ""): group
        for group in scope_payload.get("claim_groups") or ()
        if isinstance(group, Mapping) and str(group.get("claim_group_id") or "")
    }
    slots_by_group: dict[str, list[dict[str, Any]]] = {}
    slots_by_id: dict[str, dict[str, Any]] = {}
    for slot in scope_payload.get("claim_slots") or ():
        if not isinstance(slot, Mapping):
            continue
        group_id = str(slot.get("claim_group_id") or "")
        slot_id = str(slot.get("claim_slot_id") or "")
        if group_id:
            slots_by_group.setdefault(group_id, []).append(dict(slot))
        if slot_id:
            if slot_id in slots_by_id:
                raise AssertionError("output scope repeats a required comparison claim_slot_id")
            slots_by_id[slot_id] = dict(slot)

    observations: list[dict[str, Any]] = []
    for pair, groups in required_groups_by_pair.items():
        expected_papers = set(pair)
        if not groups:
            raise AssertionError(f"required comparison pair has no required task group: {pair}")
        for group in groups:
            group_id = str(group.get("claim_group_id") or "")
            scope_group = scope_groups.get(group_id)
            if scope_group is None:
                raise AssertionError(f"required comparison group is absent from the output scope: {group_id}")
            task_id = str(group.get("task_id") or "")
            task_contract = group.get("task_contract")
            task_contract = task_contract if isinstance(task_contract, Mapping) else {}
            relation_id = str(task_contract.get("relation_id") or "")
            group_papers = {str(value) for value in group.get("paper_keys") or ()}
            if group_papers != expected_papers or not task_id or not relation_id:
                raise AssertionError(f"required comparison task identity or pair is malformed: {group_id}")
            scope_task_contract = scope_group.get("task_contract")
            scope_task_contract = scope_task_contract if isinstance(scope_task_contract, Mapping) else {}
            if (
                str(scope_group.get("task_id") or "") != task_id
                or str(scope_group.get("claim_kind") or "") != "model_authored_relation_comparison_slot"
                or str(scope_task_contract.get("relation_id") or "") != relation_id
                or str(scope_task_contract.get("task_id") or "") != task_id
                or {str(value) for value in scope_group.get("paper_keys") or ()} != expected_papers
                or relation_id not in {str(value) for value in scope_group.get("relation_ids") or ()}
            ):
                raise AssertionError(f"required comparison task differs from its output-scope group: {group_id}")
            group_slots = slots_by_group.get(group_id) or []
            required_slot_ids = {str(slot.get("claim_slot_id") or "") for slot in group_slots}
            if len(required_slot_ids) != 2 or "" in required_slot_ids:
                raise AssertionError(f"required comparison group does not have exactly two slots: {group_id}")
            if {str(value) for value in group.get("support_slot_ids") or ()} != required_slot_ids:
                raise AssertionError(f"required comparison group slot IDs differ from output scope: {group_id}")
            if {str(slot.get("paper_key") or "") for slot in group_slots} != expected_papers:
                raise AssertionError(f"required comparison slots do not cover both papers: {group_id}")
            matching_sections = [
                section for section in candidate_sections
                if required_slot_ids.issubset({
                    str(row.get("claim_slot_id") or "")
                    for row in section.get("claim_support") or () if isinstance(row, Mapping)
                })
            ]
            if len(matching_sections) != 1:
                raise AssertionError(
                    f"required comparison group is not complete in exactly one candidate section: {group_id}"
                )
            section_task_ids = {str(value) for value in matching_sections[0].get("task_ids") or ()}
            section_relation_ids = {str(value) for value in matching_sections[0].get("relation_ids") or ()}
            if task_id not in section_task_ids or relation_id not in section_relation_ids:
                raise AssertionError(f"required comparison section omits its exact task/relation IDs: {group_id}")
            support_rows = [
                row for row in matching_sections[0].get("claim_support") or ()
                if isinstance(row, Mapping) and str(row.get("claim_slot_id") or "") in required_slot_ids
            ]
            if (
                len(support_rows) != 2
                or {str(row.get("paper_key") or "") for row in support_rows} != expected_papers
                or {str(row.get("task_id") or "") for row in support_rows} != {task_id}
            ):
                raise AssertionError(f"required comparison support closure is incomplete: {group_id}")
            for row in support_rows:
                slot_id = str(row.get("claim_slot_id") or "")
                scope_slot = slots_by_id.get(slot_id)
                if scope_slot is None or str(scope_slot.get("claim_group_id") or "") != group_id:
                    raise AssertionError(f"required comparison support row is not bound to its exact scope slot: {slot_id}")
                if any(
                    str(row.get(field) or "") != str(scope_slot.get(field) or "")
                    for field in ("paper_key", "primary_claim_id", "study_id")
                ):
                    raise AssertionError(f"required comparison identity differs from its scope slot: {slot_id}")
                for field in ("source_claim_ids", "evidence_ids", "source_field_ids"):
                    row_values = tuple(sorted(str(value) for value in row.get(field) or ()))
                    slot_values = tuple(sorted(str(value) for value in scope_slot.get(field) or ()))
                    if not row_values or row_values != slot_values:
                        raise AssertionError(f"required comparison {field} differs from its exact scope slot: {slot_id}")
            observations.append({
                "claim_group_id": group_id,
                "task_id": task_id,
                "relation_id": relation_id,
                "paper_keys": sorted(expected_papers),
                "section_id": str(matching_sections[0].get("section_id") or ""),
                "claim_slot_ids": sorted(required_slot_ids),
                "support_rows": len(support_rows),
                "source_closed": True,
            })
    return observations


def _scale_fragment_membership_signature(
    request: Mapping[str, Any], organization_map: Mapping[str, Any],
) -> tuple[Any, ...]:
    cards = {
        int(row[0]): list(row)
        for row in request.get("fragment_cards") or ()
        if isinstance(row, (list, tuple)) and row and type(row[0]) is int
    }
    text_pool = request.get("text_pool") or ()
    paper_keys = request.get("paper_keys") or ()
    task_refs = request.get("task_refs") or ()
    topic_refs = request.get("topic_refs") or ()
    selected: set[int] = set()
    deferred: dict[int, str] = {}
    for section in organization_map.get("sections") or ():
        for index in section.get("fragment_indices") or ():
            if type(index) is not int or index not in cards or index in selected:
                raise AssertionError("organization map duplicated or invented a fragment")
            selected.add(index)
    for row in organization_map.get("deferred") or ():
        reason = str(row.get("reason_code") or "")
        for index in row.get("fragment_indices") or ():
            if type(index) is not int or index not in cards or index in selected or index in deferred:
                raise AssertionError("organization map deferred fragment membership overlaps")
            deferred[index] = reason
    expected = set(cards)
    if selected | set(deferred) != expected:
        raise AssertionError("organization map does not partition its request fragment bank")

    def card_signature(index: int) -> tuple[Any, ...]:
        _fragment_index, text_index, paper_indices, state, task_index, *topic_tail = cards[index]
        text = text_pool[text_index] if type(text_index) is int else None
        papers = tuple(sorted(str(paper_keys[item]) for item in paper_indices))
        task = str(task_refs[task_index]) if type(task_index) is int else ""
        topic_index = topic_tail[0] if topic_tail else None
        topic = str(topic_refs[topic_index]) if type(topic_index) is int else ""
        return index, text, papers, str(state), task, topic

    return (
        tuple(card_signature(index) for index in sorted(selected)),
        tuple((card_signature(index), deferred[index]) for index in sorted(deferred)),
    )


def _scale_organization_section_from_fragments(
    request: Mapping[str, Any], *, section_index: int, metadata_index: int,
    fragment_indices: Sequence[int],
) -> dict[str, Any]:
    cards = {
        int(row[0]): list(row)
        for row in request.get("fragment_cards") or ()
        if isinstance(row, (list, tuple)) and row and type(row[0]) is int
    }
    indices = list(fragment_indices)
    papers = sorted({
        int(paper_index)
        for index in indices for paper_index in cards[index][2]
        if type(paper_index) is int
    })
    roles = []
    for paper_index in papers:
        role_fragment = next(
            (index for index in indices if paper_index in cards[index][2]), None,
        )
        if role_fragment is None:
            raise AssertionError(f"organization section lost source role for paper {paper_index}")
        roles.append([paper_index, role_fragment])
    return {
        "section_index": section_index,
        "metadata_index": metadata_index,
        "fragment_indices": indices,
        "paper_roles": roles,
    }


def _scale_merge_noncomparison_organization_fragments(
    request: Mapping[str, Any],
    base_map: Mapping[str, Any],
    *,
    validator: Callable[[Mapping[str, Any], Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Compact candidate_2 context fragments while retaining every full fragment."""
    # Match the existing fixture callback: private prompt authority is transport
    # context, and the raw request object is never mutated.
    logical = {key: value for key, value in request.items() if key != "_prompt_authority"}
    result = json.loads(json.dumps(dict(base_map), ensure_ascii=False))
    if str(logical.get("candidate_id") or "") != "candidate_2":
        validator(result, logical)
        return result

    cards = {
        int(row[0]): list(row)
        for row in logical.get("fragment_cards") or ()
        if isinstance(row, (list, tuple)) and row and type(row[0]) is int
    }
    comparison_indices = {
        int(row["group_index"])
        for row in logical.get("required_comparison_sections") or ()
        if isinstance(row, Mapping) and type(row.get("group_index")) is int
    }
    required_sections: list[dict[str, Any]] = []
    noncomparison_indices: list[int] = []
    used: set[int] = set()
    for raw in result.get("sections") or ():
        section = dict(raw)
        indices = [int(value) for value in section.get("fragment_indices") or ()]
        if not indices or used.intersection(indices):
            raise AssertionError("base organization map has empty or duplicate fragment membership")
        used.update(indices)
        if comparison_indices.intersection(indices):
            if len(indices) != 1 or indices[0] not in comparison_indices:
                raise AssertionError("base organization map mixed a required A/B fragment with another group")
            required_sections.append(section)
        else:
            noncomparison_indices.extend(indices)

    multi_source: list[int] = []
    single_source: list[int] = []
    for index in noncomparison_indices:
        card = cards.get(index)
        if card is None or str(card[3]) != "materialized":
            raise AssertionError(f"selected context fragment {index} is not materialized")
        papers = {int(value) for value in card[2] if type(value) is int}
        if len(papers) > 1:
            multi_source.append(index)
        elif len(papers) == 1:
            single_source.append(index)
        else:
            raise AssertionError(f"materialized fragment {index} has no source paper")

    catalog = logical.get("metadata_catalog") or ()
    fallback_metadata = next(
        (int(section["metadata_index"]) for section in result.get("sections") or ()
         if not comparison_indices.intersection(section.get("fragment_indices") or ())),
        0,
    )

    def metadata_for(terms: tuple[str, ...]) -> int:
        for index, row in enumerate(catalog):
            title = str(row.get("title") or "").casefold() if isinstance(row, Mapping) else ""
            if any(term in title for term in terms):
                return index
        return fallback_metadata

    sections: list[dict[str, Any]] = []
    for section in required_sections:
        section["section_index"] = len(sections)
        sections.append(section)
    if multi_source:
        sections.append(_scale_organization_section_from_fragments(
            logical, section_index=len(sections),
            metadata_index=metadata_for(("cross-source comparisons", "evidence across contexts", "source findings")),
            fragment_indices=sorted(multi_source),
        ))
    if single_source:
        sections.append(_scale_organization_section_from_fragments(
            logical, section_index=len(sections),
            metadata_index=metadata_for(("source findings", "conditions and boundaries", "evidence across contexts")),
            fragment_indices=sorted(single_source),
        ))

    result["sections"] = sections
    if _scale_fragment_membership_signature(logical, result) != _scale_fragment_membership_signature(logical, base_map):
        raise AssertionError("candidate_2 organization layout changed source fragment membership")
    validator(result, logical)
    return result


def _scale_candidate_support_multiset_signature(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    rows = []
    for section in payload.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        for support in section.get("claim_support") or ():
            if not isinstance(support, Mapping):
                continue
            rows.append((
                str(support.get("claim_slot_id") or ""),
                str(support.get("task_id") or ""),
                str(support.get("claim") or ""),
                str(support.get("paper_key") or ""),
                str(support.get("primary_claim_id") or ""),
                tuple(sorted(str(value) for value in support.get("source_claim_ids") or ())),
                tuple(sorted(str(value) for value in support.get("evidence_ids") or ())),
                tuple(sorted(str(value) for value in support.get("source_field_ids") or ())),
                str(support.get("study_id") or ""),
            ))
    return tuple(sorted(rows))


def _scale_candidate_generation_layout_signature(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    sections = []
    for section in payload.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        support = tuple(sorted(
            (
                str(row.get("claim_slot_id") or ""),
                str(row.get("task_id") or ""),
                str(row.get("paper_key") or ""),
            )
            for row in section.get("claim_support") or ()
            if isinstance(row, Mapping)
        ))
        sections.append((
            str(section.get("title") or ""),
            str(section.get("goal") or ""),
            tuple(sorted(str(value) for value in section.get("paper_keys") or ())),
            tuple(sorted(str(value) for value in section.get("task_ids") or ())),
            tuple(sorted(str(value) for value in section.get("relation_ids") or ())),
            support,
        ))
    return tuple(sections)


def _scale_capture_model_visible_arbitration_layout(
    node_id: str, request: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Read direct/final arbitration section layout through existing codecs."""
    from outline.semantic_request_wire import expand_semantic_request_wire
    from outline.candidate_arbitration_packing import expand_arbitration_candidate_support
    from outline.v3_models import compute_v3_hash

    logical = {key: value for key, value in request.items() if key != "_prompt_authority"}
    hierarchy = logical.get("hierarchy")
    hierarchy = hierarchy if isinstance(hierarchy, Mapping) else {}
    leaf_id = str(hierarchy.get("leaf_id") or "")

    def section_rows_from_contents(candidate_id: str, sections: Any) -> list[dict[str, Any]]:
        if not isinstance(sections, list):
            return []
        rows = []
        for position, section in enumerate(sections):
            if not isinstance(section, Mapping):
                raise AssertionError("model-visible candidate section is not an object")
            rows.append({
                "candidate_id": candidate_id,
                "position": position,
                "section_id": str(section.get("section_id") or ""),
                "section_hash": compute_v3_hash(dict(section)),
                "source_closure_hash": compute_v3_hash(section.get("claim_support") or []),
                "title": str(section.get("title") or ""),
                "goal": str(section.get("goal") or ""),
                "paper_keys": sorted(str(value) for value in section.get("paper_keys") or ()),
                "task_ids": sorted(str(value) for value in section.get("task_ids") or ()),
                "relation_ids": sorted(str(value) for value in section.get("relation_ids") or ()),
            })
        return rows

    def section_rows_from_index(candidate_id: str, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        result = []
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise AssertionError("decoded candidate section index row is not an object")
            result.append({
                "candidate_id": candidate_id,
                "position": row.get("position", position),
                "section_id": str(row.get("section_id") or ""),
                "section_hash": str(row.get("section_hash") or ""),
                "source_closure_hash": str(row.get("source_closure_hash") or ""),
                "title": str(row.get("title") or ""),
                "goal": str(row.get("goal") or ""),
                "paper_keys": sorted(str(value) for value in row.get("paper_keys") or ()),
                "task_ids": sorted(str(value) for value in row.get("task_ids") or ()),
                "relation_ids": sorted(str(value) for value in row.get("relation_ids") or ()),
            })
        return result

    parsed_request_sha256 = hashlib.sha256(
        json.dumps(logical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    # Hierarchical leaves expose the full section index plus only their bound subset.
    if leaf_id:
        raw_index = logical.get("candidate_section_index")
        raw_bindings = logical.get("candidate_section_bindings")
        if isinstance(raw_index, Mapping) and isinstance(raw_bindings, Mapping):
            raw_rows = raw_index.get("candidate_2") or ()
            binding_rows = raw_bindings.get("candidate_2") or ()
            binding_ids = {
                str(row.get("section_id") or "")
                for row in binding_rows if isinstance(row, Mapping)
            }
            selected_rows = [
                row for row in raw_rows if isinstance(row, Mapping)
                and str(row.get("section_id") or "") in binding_ids
            ]
            if selected_rows:
                return {
                    "node_id": node_id,
                    "capture_kind": "hierarchical_leaf",
                    "leaf_id": leaf_id,
                    "hierarchy_level": str(hierarchy.get("level") or ""),
                    "candidate_ids": [str(value) for value in logical.get("candidate_ids") or () if str(value)],
                    "candidate_2_binding_section_ids": sorted(binding_ids),
                    "candidate_1_section_rows": section_rows_from_index(
                        "candidate_1", [row for row in raw_index.get("candidate_1") or ()
                                        if isinstance(row, Mapping)
                                        and str(row.get("section_id") or "") in {
                                            str(value.get("section_id") or "") for value in raw_bindings.get("candidate_1") or ()
                                            if isinstance(value, Mapping)
                                        }]
                    ),
                    "candidate_2_section_rows": section_rows_from_index("candidate_2", selected_rows),
                    "parsed_request_sha256": parsed_request_sha256,
                }

    is_final_arbitration = (
        node_id == "arbitration"
        or node_id.startswith("arbitration:final:")
        or hierarchy.get("level") == "all_candidate_final_arbitration"
    )
    if not is_final_arbitration:
        return None

    from outline.critique_coverage_wire import expand_critique_coverage_certificates
    from outline.candidate_arbitration_final_projection import expand_arbitration_final_projection
    from outline.candidate_arbitration_ledger_packing import expand_arbitration_final_ledger

    expanded = expand_semantic_request_wire(logical)
    if isinstance(expanded.get("candidate_support_packing"), Mapping):
        expanded = expand_arbitration_candidate_support(expanded)
    expanded = expand_critique_coverage_certificates(expanded)
    if isinstance(expanded.get("arbitration_final_projection"), Mapping):
        expanded = expand_arbitration_final_projection(expanded)
    raw_ledger = expanded.get("arbitration_evaluation_ledger")
    wire_ledger = logical.get("arbitration_evaluation_ledger")
    wire_index = wire_ledger.get("candidate_section_index") if isinstance(wire_ledger, Mapping) else None
    raw_c2_rows = wire_index.get("candidate_2") if isinstance(wire_index, Mapping) else None
    ledger = raw_ledger
    if isinstance(ledger, Mapping) and isinstance(ledger.get("ledger_packing"), Mapping):
        ledger = expand_arbitration_final_ledger(ledger)
        expanded = {**dict(expanded), "arbitration_evaluation_ledger": ledger}

    candidate_contents = expanded.get("candidate_contents")
    c1_content = candidate_contents.get("candidate_1") if isinstance(candidate_contents, Mapping) else None
    c2_content = candidate_contents.get("candidate_2") if isinstance(candidate_contents, Mapping) else None
    content_rows_1 = section_rows_from_contents(
        "candidate_1", c1_content.get("sections") if isinstance(c1_content, Mapping) else None,
    )
    content_rows_2 = section_rows_from_contents(
        "candidate_2", c2_content.get("sections") if isinstance(c2_content, Mapping) else None,
    )
    ledger = expanded.get("arbitration_evaluation_ledger")
    index = ledger.get("candidate_section_index") if isinstance(ledger, Mapping) else None
    index_rows_1 = section_rows_from_index(
        "candidate_1", index.get("candidate_1") if isinstance(index, Mapping) else None,
    )
    index_rows_2 = section_rows_from_index(
        "candidate_2", index.get("candidate_2") if isinstance(index, Mapping) else None,
    )
    candidate_1_rows = content_rows_1 or index_rows_1
    candidate_2_rows = content_rows_2 or index_rows_2
    if not candidate_2_rows:
        return None
    return {
        "node_id": node_id,
        "capture_kind": "final_arbitration_model_request",
        "hierarchy_level": str(hierarchy.get("level") or ""),
        "request_schema_version": str(expanded.get("schema_version") or ""),
        "candidate_ids": [str(value) for value in expanded.get("candidate_ids") or () if str(value)],
        "projection_aliases": list(
            (logical.get("arbitration_final_projection") or {}).get("aliases") or ()
        ) if isinstance(logical.get("arbitration_final_projection"), Mapping) else [],
        "ledger_packing_schema": str(
            (wire_ledger.get("ledger_packing") or {}).get("schema_version") or ""
        ) if isinstance(wire_ledger, Mapping) else "",
        "raw_candidate_2_positional_index_row_count": (
            len(raw_c2_rows) if isinstance(raw_c2_rows, list) else None
        ),
        "parsed_request_sha256": parsed_request_sha256,
        "candidate_1_section_rows": candidate_1_rows,
        "candidate_2_section_rows": candidate_2_rows,
    }

def _scale_model_visible_rows_match_candidate(
    rows: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any],
) -> bool:
    from outline.v3_models import compute_v3_hash

    sections = [row for row in candidate.get("sections") or () if isinstance(row, Mapping)]
    by_id = {str(row.get("section_id") or ""): row for row in sections}
    visible_by_id = {str(row.get("section_id") or ""): row for row in rows}
    if not by_id or len(visible_by_id) != len(rows) or set(visible_by_id) != set(by_id):
        return False
    expected_order = [str(row.get("section_id") or "") for row in sections]
    observed_order = [str(row.get("section_id") or "") for row in sorted(
        rows, key=lambda item: int(item.get("position") or 0),
    )]
    if observed_order != expected_order:
        return False
    for section_id, section in by_id.items():
        visible = visible_by_id[section_id]
        if visible.get("section_hash") != compute_v3_hash(dict(section)):
            return False
        for field in ("title", "goal", "paper_keys", "task_ids", "relation_ids"):
            expected = section.get(field) or "" if field in {"title", "goal"} else sorted(
                str(value) for value in section.get(field) or ()
            )
            observed = visible.get(field) or "" if field in {"title", "goal"} else sorted(
                str(value) for value in visible.get(field) or ()
            )
            if observed != expected:
                return False
        if visible.get("source_closure_hash") and visible["source_closure_hash"] != compute_v3_hash(
            section.get("claim_support") or ()
        ):
            return False
    return True

def test_scale_candidate_organization_layout_roundtrip_preserves_source_closure(tmp_path: Path) -> None:
    from outline.candidate_fragment_bank import validate_candidate_organization_map_for_request_v1
    from tests import run_first_candidate_fragments_fixture as organization_fixture
    from tests import test_run_first_candidate_fragment_bank as bank_test

    fixture = bank_test._sealed_scope(tmp_path)
    scope = fixture[-1]
    required = bank_test._seed_group(scope, "relation:required-ab")
    optional = bank_test._seed_group(scope, "relation:optional-ab")
    comparison_rows = [
        bank_test._comparison_row(required, "required"),
        bank_test._comparison_row(optional, "optional"),
    ]
    _canonical, bank = bank_test._build_bank(
        fixture, comparison_rows=comparison_rows, required_group_ids=[required],
    )
    request, _axis, _plan, _catalog = bank_test._organization_context(
        bank, scope, candidate_id="candidate_2", required_group_ids=[required],
    )
    required_indices = set(int(value) for value in request["required_group_indices"])
    preferred = [
        int(card[0]) for card in request["fragment_cards"]
        if card[3] == "materialized" and int(card[0]) not in required_indices
    ]
    base = organization_fixture._organization_response(request, preferred_indices=preferred)
    # A unit-only split layout uses complete bank fragments, never partial claims.
    split_sections = []
    comparison_indices = {
        int(row["group_index"]) for row in request.get("required_comparison_sections") or ()
    }
    cards = {int(row[0]): list(row) for row in request["fragment_cards"]}
    for section in base["sections"]:
        indices = [int(value) for value in section["fragment_indices"]]
        if comparison_indices.intersection(indices):
            split_sections.append(dict(section))
        else:
            for index in indices:
                paper_indices = sorted(set(int(value) for value in cards[index][2]))
                split_sections.append({
                    "section_index": len(split_sections),
                    "metadata_index": int(section["metadata_index"]),
                    "fragment_indices": [index],
                    "paper_roles": [[paper, index] for paper in paper_indices],
                })
    for index, section in enumerate(split_sections):
        section["section_index"] = index
    split = dict(base)
    split["sections"] = split_sections
    validate_candidate_organization_map_for_request_v1(split, request)
    compact = _scale_merge_noncomparison_organization_fragments(
        request, split, validator=validate_candidate_organization_map_for_request_v1,
    )
    assert len(compact["sections"]) < len(split["sections"])
    assert _scale_organization_layout_signature(compact) != _scale_organization_layout_signature(split)
    assert _scale_fragment_membership_signature(request, split) == _scale_fragment_membership_signature(request, compact)

    expanded_split = bank_test._expand_from_request(split, request, bank, scope, [required])
    expanded_compact = bank_test._expand_from_request(compact, request, bank, scope, [required])
    scope.validate(expanded_split)
    scope.validate(expanded_compact)
    assert _scale_candidate_support_multiset_signature(expanded_split) == _scale_candidate_support_multiset_signature(expanded_compact)
    required_slots = {
        slot.claim_slot_id for slot in scope.claim_slots if slot.claim_group_id == required
    }
    assert len(required_slots) == 2
    for payload in (expanded_split, expanded_compact):
        matching = [
            section for section in payload["sections"]
            if required_slots.issubset({str(row.get("claim_slot_id") or "")
                                        for row in section.get("claim_support") or ()})
        ]
        assert len(matching) == 1
        rows = [row for row in matching[0]["claim_support"]
                if str(row.get("claim_slot_id") or "") in required_slots]
        assert {str(row["paper_key"]) for row in rows} == set(bank_test.PAPER_KEYS)
        assert all(row["source_claim_ids"] and row["evidence_ids"] and row["source_field_ids"]
                   for row in rows)


def test_scale_partition_layout_aggregation_and_hierarchical_leaf_capture() -> None:
    request_a = {
        "bank_group_count": 2,
        "paper_keys": ["paper-a", "paper-c"],
        "partition": {"full_bank_group_count": 4, "local_to_global_group_indices": [0, 2]},
    }
    request_b = {
        "bank_group_count": 2,
        "paper_keys": ["paper-b", "paper-d"],
        "partition": {"full_bank_group_count": 4, "local_to_global_group_indices": [1, 3]},
    }
    split_a = {
        "sections": [{"metadata_index": 2, "fragment_indices": [0], "paper_roles": [[0, 0]]}],
        "deferred": [{"reason_code": "optional_unresolved", "fragment_indices": [1]}],
    }
    split_b = {
        "sections": [
            {"metadata_index": 2, "fragment_indices": [0], "paper_roles": [[0, 0]]},
            {"metadata_index": 2, "fragment_indices": [1], "paper_roles": [[1, 1]]},
        ],
        "deferred": [],
    }
    merged_a = {
        "sections": [{"metadata_index": 2, "fragment_indices": [0], "paper_roles": [[0, 0]]}],
        "deferred": [{"reason_code": "optional_unresolved", "fragment_indices": [1]}],
    }
    merged_b = {
        "sections": [{"metadata_index": 2, "fragment_indices": [0, 1], "paper_roles": [[0, 0], [1, 1]]}],
        "deferred": [],
    }
    split_layout = _scale_aggregate_partition_layouts((
        _scale_global_organization_map_signature(request_a, split_a),
        _scale_global_organization_map_signature(request_b, split_b),
    ))
    merged_layout = _scale_aggregate_partition_layouts((
        _scale_global_organization_map_signature(request_a, merged_a),
        _scale_global_organization_map_signature(request_b, merged_b),
    ))
    assert split_layout != merged_layout
    assert split_layout[0] == merged_layout[0] == 4
    for layout in (split_layout, merged_layout):
        global_membership = [index for section in layout[1] for index in section[0]]
        global_membership.extend(index for _reason, indices in layout[2] for index in indices)
        assert sorted(global_membership) == [0, 1, 2, 3]

    leaf_request = {
        "hierarchy": {"leaf_id": "leaf-1", "level": "candidate_leaf_arbitration"},
        "candidate_ids": ["candidate_1", "candidate_2"],
        "candidate_section_index": {
            "candidate_2": [
                {"position": 0, "section_id": "c2s0", "section_hash": "hash-0", "source_closure_hash": "support-0"},
                {"position": 1, "section_id": "c2s1", "section_hash": "hash-1", "source_closure_hash": "support-1"},
            ],
        },
        "candidate_section_bindings": {
            "candidate_2": [{"section_id": "c2s1"}],
        },
    }
    leaf_view = _scale_capture_model_visible_arbitration_layout("arbitration:leaf:leaf-1", leaf_request)
    assert leaf_view is not None
    assert leaf_view["capture_kind"] == "hierarchical_leaf"
    assert leaf_view["candidate_2_binding_section_ids"] == ["c2s1"]
    assert [row["section_id"] for row in leaf_view["candidate_2_section_rows"]] == ["c2s1"]


def test_scale_required_comparison_closure_keeps_same_pair_tasks_distinct() -> None:
    pair = ["paper-a", "paper-b"]

    def comparison_group(group_id: str, task_id: str, relation_id: str, relation_type: str) -> dict[str, Any]:
        return {
            "claim_group_id": group_id,
            "task_id": task_id,
            "claim_kind": "model_authored_relation_comparison_slot",
            "claim_seed": True,
            "paper_keys": list(pair),
            "support_slot_ids": [f"{group_id}-{paper}" for paper in pair],
            "relation_ids": [relation_id],
            "task_contract": {
                "relation_id": relation_id,
                "relation_type": relation_type,
                "task_id": task_id,
            },
        }

    optional = comparison_group("group-optional-qualifies", "task-qualifies", "relation-qualifies", "qualifies")
    required = comparison_group(
        "group-required-magnitude", "task-magnitude", "relation-magnitude", "reported_magnitude_contrast",
    )
    # The bank array is intentionally not the global index table; use the
    # declared global `index` plus the partition binding.
    bank_groups = [
        {
            "index": index,
            "claim_group": {
                "claim_group_id": f"filler-{index}",
                "task_id": f"task-filler-{index}",
                "claim_kind": "canonical_claim_bundle",
                "claim_seed": False,
                "paper_keys": ["paper-a"],
                "relation_ids": [],
                "task_contract": {"task_id": f"task-filler-{index}"},
            },
        }
        for index in range(72)
    ]
    bank_groups[62]["claim_group"] = optional
    bank_groups[68]["claim_group"] = required
    bank = {"content_hash": "synthetic-bank-hash", "groups": list(reversed(bank_groups))}
    required_request = {
        "bank_group_count": 1,
        "required_group_indices": [0],
        "bank_hash": "synthetic-bank-hash",
        "paper_keys": list(pair),
        "partition": {"full_bank_hash": "synthetic-bank-hash", "full_bank_group_count": 72, "local_to_global_group_indices": [68]},
        "required_comparison_sections": [{"group_index": 0, "paper_indices": [0, 1]}],
    }
    optional_request = {
        "bank_group_count": 1,
        "required_group_indices": [0],
        "bank_hash": "synthetic-bank-hash",
        "paper_keys": list(pair),
        "partition": {"full_bank_hash": "synthetic-bank-hash", "full_bank_group_count": 72, "local_to_global_group_indices": [62]},
        "required_comparison_sections": [{"group_index": 0, "paper_indices": [0, 1]}],
    }
    one_by_pair = _scale_required_comparison_groups_by_pair([required_request], bank, [pair])
    both_by_pair = _scale_required_comparison_groups_by_pair(
        [optional_request, required_request], bank, [pair],
    )
    pair_key = tuple(pair)
    assert [row["claim_group_id"] for row in one_by_pair[pair_key]] == [required["claim_group_id"]]
    assert [row["claim_group_id"] for row in both_by_pair[pair_key]] == [
        optional["claim_group_id"], required["claim_group_id"],
    ]

    scope_payload = {
        "claim_groups": [optional, required],
        "claim_slots": [
            {
                "claim_group_id": group["claim_group_id"],
                "claim_slot_id": f"{group['claim_group_id']}-{paper}",
                "paper_key": paper,
                "primary_claim_id": f"primary:{group['claim_group_id']}:{paper}",
                "study_id": "",
                "source_claim_ids": [f"claim:{group['claim_group_id']}:{paper}"],
                "evidence_ids": [f"evidence:{group['claim_group_id']}:{paper}"],
                "source_field_ids": [f"field:{group['claim_group_id']}:{paper}"],
            }
            for group in (optional, required)
            for paper in pair
        ],
    }
    support_rows = [
        {
            "claim_slot_id": f"{group['claim_group_id']}-{paper}",
            "task_id": group["task_id"],
            "paper_key": paper,
            "primary_claim_id": f"primary:{group['claim_group_id']}:{paper}",
            "study_id": "",
            "source_claim_ids": [f"claim:{group['claim_group_id']}:{paper}"],
            "evidence_ids": [f"evidence:{group['claim_group_id']}:{paper}"],
            "source_field_ids": [f"field:{group['claim_group_id']}:{paper}"],
        }
        for group in (optional, required)
        for paper in pair
    ]
    candidate_sections = [{
        "section_id": "ab-section",
        "task_ids": [optional["task_id"], required["task_id"]],
        "relation_ids": ["relation-qualifies", "relation-magnitude"],
        "claim_support": support_rows,
    }]
    one_observation = _scale_assert_required_comparison_closures(one_by_pair, scope_payload, candidate_sections)
    both_observations = _scale_assert_required_comparison_closures(both_by_pair, scope_payload, candidate_sections)
    assert len(one_observation) == 1
    assert one_observation[0]["claim_group_id"] == required["claim_group_id"]
    assert len(both_observations) == 2
    assert {row["claim_group_id"] for row in both_observations} == {
        optional["claim_group_id"], required["claim_group_id"],
    }
    assert all(row["source_closed"] and row["section_id"] == "ab-section" for row in both_observations)


def _scale_add_candidate_design_profiles(
    papers: Sequence[ScalePaper],
) -> tuple[list[ScalePaper], dict[str, Any]]:
    """Add small synthetic experiment-design evidence without changing outcomes or identities."""
    design_profiles = (
        ("randomized_parallel_groups", "randomized parallel-group experiment"),
        ("counterbalanced_within_subject", "counterbalanced within-subject experiment"),
        ("cluster_randomized_field", "cluster-randomized field experiment"),
        ("blocked_randomized_parallel", "blocked randomized parallel-group experiment"),
        ("randomized_crossover", "randomized crossover experiment"),
    )
    bridge_statement = (
        "Article-level bridge: coupon/reference-price framing and bundle-price presentation "
        "are distinct promotion questions; estimates are not pooled and no shared mediator is asserted."
    )
    enriched: list[ScalePaper] = []
    rows: list[dict[str, Any]] = []
    for paper in papers:
        design_type, design_name = design_profiles[paper.index % len(design_profiles)]
        multi_study = paper.index % 11 == 0
        design_statement = (
            f"Reported Study A design: {design_name}."
            if multi_study
            else f"Research design: {design_name}."
        )
        # The oracle's printed-field grammar stops a condition at a new
        # Method field, preserving the measured condition as a separate fact.
        condition_description = f"{paper.condition_description.rstrip()}\nMethod: {design_statement}"
        limitation = (
            paper.limitation.rstrip()
            + " Design scope remains the stated experimental conditions, observed sample, and 30-day follow-up."
        )
        if multi_study:
            limitation += " The design statement applies to the reported Study A estimate; no Study B design or effect is added."
        if paper.index == 0:
            limitation += " " + bridge_statement
        updated = replace(
            paper,
            condition_description=condition_description,
            limitation=limitation,
        )
        if (
            updated.paper_key != paper.paper_key or updated.stem != paper.stem
            or updated.title != paper.title or updated.finding != paper.finding
            or updated.sample_description != paper.sample_description
            or updated.table_note != paper.table_note
            or updated.extended_source_english != paper.extended_source_english
            or updated.extended_source_chinese != paper.extended_source_chinese
        ):
            raise AssertionError("design overlay changed a source identity, result, sample, or printed study record")
        enriched.append(updated)
        rows.append({
            "paper_index": paper.index,
            "design_type": design_type,
            "design_statement": design_statement,
            "study_scope": "Study A only" if multi_study else "single reported study",
        })
    return enriched, {
        "scope": "synthetic test fixture only; no scientific-quality claim",
        "designs": rows,
        "article_bridge_statement": bridge_statement,
    }


def test_scale_layout_design_profiles_are_source_and_reader_visible(tmp_path: Path) -> None:
    baseline_papers = _scale_papers(10, source_variant="all_required_comparisons")
    profiled_papers, profile = _scale_add_candidate_design_profiles(baseline_papers)
    assert len({row["design_type"] for row in profile["designs"]}) >= 5
    assert all(
        {"paper_index", "design_type", "design_statement", "study_scope"}.issubset(row)
        for row in profile["designs"]
    )
    assert all(
        (profiled.paper_key, profiled.stem, profiled.title, profiled.finding,
         profiled.sample_description, profiled.table_note, profiled.extended_source_english,
         profiled.extended_source_chinese)
        == (baseline.paper_key, baseline.stem, baseline.title, baseline.finding,
            baseline.sample_description, baseline.table_note, baseline.extended_source_english,
            baseline.extended_source_chinese)
        for baseline, profiled in zip(baseline_papers, profiled_papers, strict=True)
    )

    baseline_dir, profiled_dir = tmp_path / "baseline", tmp_path / "profiled"
    baseline_dir.mkdir()
    profiled_dir.mkdir()
    baseline_paths: dict[str, Path] = {}
    profiled_paths: dict[str, Path] = {}
    design_rows = {int(row["paper_index"]): row for row in profile["designs"]}
    for baseline, profiled in zip(baseline_papers, profiled_papers, strict=True):
        baseline_path = baseline_dir / f"{baseline.stem}.pdf"
        profiled_path = profiled_dir / f"{profiled.stem}.pdf"
        _write_scale_pdf(baseline_path, baseline)
        _write_scale_pdf(profiled_path, profiled)
        baseline_paths[baseline.paper_key] = baseline_path
        profiled_paths[profiled.paper_key] = profiled_path
        with fitz.open(profiled_path) as document:
            assert document.page_count == 2
            first_page = " ".join(document[0].get_text("text").split())
        design_row = design_rows[profiled.index]
        parsed_design_studies = re.findall(
            r"\bStudy\s+([A-Z0-9]+)\b", str(design_row["design_statement"]), re.IGNORECASE,
        )
        assert parsed_design_studies == (["A"] if profiled.index % 11 == 0 else [])
        assert " ".join(profiled.condition_description.split()) in first_page
        assert " ".join(profiled.limitation.split()) in first_page
        assert design_row["design_statement"] in first_page
        reader = _reader_content(profiled)
        core = reader.get("core_analysis")
        assert isinstance(core, Mapping)
        assert design_row["design_statement"] in str(core.get("methodology") or "")
        assert design_row["design_statement"] in str(core.get("limitations") or "")
        empirical = (reader.get("specialized_details") or {}).get("empirical")
        assert isinstance(empirical, Mapping)
        assert design_row["design_statement"] in str(empirical.get("analysis_technique") or "")
        if profiled.index == 0:
            bridge = profile["article_bridge_statement"]
            assert bridge in first_page
            assert bridge in str(core.get("limitations") or "")
            assert "Study B" not in design_row["design_statement"]
            assert "no separate Study B" in profiled.sample_description

    baseline_corpus = ControlledPdfCorpus.from_pdfs(baseline_paths)
    profiled_corpus = ControlledPdfCorpus.from_pdfs(profiled_paths)
    for baseline, profiled in zip(baseline_papers, profiled_papers, strict=True):
        original = baseline_corpus.documents[baseline.paper_key]
        source = profiled_corpus.documents[profiled.paper_key]
        assert original.parse_diagnostics == source.parse_diagnostics == ()
        assert len(original.facts) == len(source.facts) == 1
        old_fact, fact = original.facts[0], source.facts[0]
        assert (
            fact.study_label, fact.sample_size, fact.direction, fact.magnitude_points,
            fact.condition, fact.follow_up_days, fact.required_qualifiers,
        ) == (
            old_fact.study_label, old_fact.sample_size, old_fact.direction, old_fact.magnitude_points,
            old_fact.condition, old_fact.follow_up_days, old_fact.required_qualifiers,
        )
        if "no measurable change" in baseline.finding.casefold():
            expected_effect = ("null", None)
        elif "reduced the primary outcome by" in baseline.finding.casefold():
            match = re.search(r"reduced the primary outcome by (\d+) points", baseline.finding)
            assert match is not None
            expected_effect = ("decrease", int(match.group(1)))
        else:
            match = re.search(r"increased the primary outcome by (\d+) points", baseline.finding)
            assert match is not None
            expected_effect = ("increase", int(match.group(1)))
        assert (fact.direction, fact.magnitude_points, fact.sample_size, fact.follow_up_days) == (
            expected_effect[0], expected_effect[1], 24, 30,
        )
        assert [
            (row.study_label, row.sample_size) for row in source.study_samples
        ] == [
            (row.study_label, row.sample_size) for row in original.study_samples
        ]
        if baseline.index % 11 == 0:
            assert fact.study_label == "Study A"
            assert [(row.study_label, row.sample_size) for row in source.study_samples] == [
                ("Study A", 24), ("Study B", 31),
            ]
            assert not any(row.study_label == "Study B" for row in source.facts)
        else:
            assert fact.study_label is None
            assert len(source.study_samples) == 1

    original_pairs = _scale_required_comparisons(baseline_papers, "all_required_comparisons")
    profiled_pairs = _scale_required_comparisons(profiled_papers, "all_required_comparisons")
    assert profiled_pairs == original_pairs
    by_key = {paper.paper_key: paper for paper in profiled_papers}
    pair_indices = [[by_key[key].index for key in pair] for pair in profiled_pairs]
    assert pair_indices == [[0, 1], [0, 2], [0, 3], [0, 9]]
    assert profiled_corpus.documents[profiled_papers[2].paper_key].facts[0].direction == "null"
    assert profiled_corpus.documents[profiled_papers[3].paper_key].facts[0].direction == "decrease"
    assert profiled_corpus.documents[profiled_papers[9].paper_key].facts[0].condition.casefold() == _RARE_SCALE_CONDITION.casefold()

def _assert_required_comparisons_have_complete_executed_bundles(
    registry: Any,
    required_comparisons: Sequence[Sequence[str]],
    request_trace: Sequence[Mapping[str, Any]],
    controlled_pdf_oracle: ControlledPdfCorpus,
    *,
    distant_pair: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    relation_map = _ready_outline_node_payload(registry, "relation_candidates")
    candidates = [row for row in relation_map.get("relations") or () if isinstance(row, Mapping)]
    observed_requests = [
        row for row in request_trace
        if isinstance(row, Mapping) and row.get("kind") == "scale_relation_adjudication_request"
    ]
    observations: list[dict[str, Any]] = []
    for raw_pair in required_comparisons:
        pair = [str(value) for value in raw_pair if str(value)]
        pair_set = set(pair)
        if len(pair) != 2 or len(pair_set) != 2:
            raise AssertionError(f"required relation pair is not two distinct source identities: {raw_pair!r}")
        pair_relation_ids = {
            str(row.get("relation_id") or "")
            for row in candidates
            if {str(value) for value in row.get("paper_keys") or () if str(value)} == pair_set
        }
        pair_relation_ids.discard("")
        if not pair_relation_ids:
            raise AssertionError(f"required PDF pair has no global relation candidate: {sorted(pair_set)}")
        matching_request: dict[str, Any] | None = None
        matching_bundle: dict[str, Any] | None = None
        for request in observed_requests:
            request_candidates = {
                str(row.get("relation_id") or ""): {str(value) for value in row.get("paper_keys") or () if str(value)}
                for row in request.get("relation_candidates") or ()
                if isinstance(row, Mapping) and str(row.get("relation_id") or "")
            }
            request_bundles = {
                str(row.get("relation_id") or ""): dict(row)
                for row in request.get("relation_evidence_bundles") or ()
                if isinstance(row, Mapping) and str(row.get("relation_id") or "")
            }
            for relation_id in pair_relation_ids.intersection(request_candidates):
                if request_candidates[relation_id] != pair_set or relation_id not in request_bundles:
                    continue
                matching_request = dict(request)
                matching_bundle = request_bundles[relation_id]
                break
            if matching_bundle is not None:
                break
        if matching_request is None or matching_bundle is None:
            raise AssertionError(
                "required PDF relation was not present in an actually executed relation-adjudication request: "
                f"{sorted(pair_set)}"
            )
        bundle_papers = {str(value) for value in matching_bundle.get("paper_ids") or () if str(value)}
        contexts = matching_bundle.get("contexts_and_boundaries")
        locators = matching_bundle.get("source_locators")
        required_evidence = {str(value) for value in matching_bundle.get("required_evidence_ids") or () if str(value)}
        provided_evidence = {str(value) for value in matching_bundle.get("provided_evidence_ids") or () if str(value)}
        missing_evidence = {str(value) for value in matching_bundle.get("missing_evidence_ids") or () if str(value)}
        if (
            bundle_papers != pair_set
            or not matching_bundle.get("findings_left")
            or not matching_bundle.get("findings_right")
            or not isinstance(contexts, Mapping)
            or not isinstance(locators, Mapping)
            or not pair_set.issubset({str(value) for value, rows in contexts.items() if rows})
            or not pair_set.issubset({str(value) for value, rows in locators.items() if rows})
            or not required_evidence
            or not required_evidence.issubset(provided_evidence)
            or missing_evidence
            or matching_bundle.get("evidence_completeness") != "complete"
        ):
            raise AssertionError(
                "executed required relation bundle lost a source side, finding, context/boundary, locator, "
                f"or evidence closure: pair={sorted(pair_set)} bundle={matching_bundle}"
            )
        paper_order = [str(value) for value in matching_bundle.get("paper_ids") or () if str(value)]
        if len(paper_order) != 2 or set(paper_order) != pair_set:
            raise AssertionError("executed relation bundle side order does not cover the required PDF identities")
        for index, paper_key in enumerate(paper_order):
            fact_rows = controlled_pdf_oracle.documents[paper_key].facts
            if len(fact_rows) != 1:
                raise AssertionError(f"controlled-PDF fixture did not yield one fact for {paper_key}")
            fact = fact_rows[0]
            finding_text = " ".join(
                str(value)
                for value in matching_bundle.get("findings_left" if index == 0 else "findings_right") or ()
            ).casefold()
            context_text = " ".join(str(value) for value in contexts[paper_key]).casefold()
            combined = f"{finding_text} {context_text}"
            direction_markers = {
                "increase": ("increase", "higher", "rose", "grew"),
                "decrease": ("decrease", "reduced", "lower", "fell"),
                "null": ("no measurable change", "no measurable effect", "no change"),
            }
            if not any(marker in finding_text for marker in direction_markers[fact.direction]):
                raise AssertionError(
                    f"executed relation request omitted the PDF-derived {fact.direction} fact for {paper_key}"
                )
            if fact.magnitude_points is not None and f"{fact.magnitude_points} point" not in finding_text:
                raise AssertionError(
                    f"executed relation request omitted the PDF-derived magnitude for {paper_key}"
                )
            condition_present = fact.condition.casefold() in context_text
            source = controlled_pdf_oracle.documents[paper_key]
            if not condition_present and len(source.study_samples) == 1:
                source_condition = next(
                    (_line_value(page, "Condition detail") for page in source.page_texts
                     if _line_value(page, "Condition detail")),
                    "",
                )
                condition_present = bool(
                    source_condition
                    and _condition_present(source_condition, fact.condition)
                    and source_condition.casefold() in context_text
                )
            if not condition_present:
                raise AssertionError(
                    f"executed relation request omitted the PDF-derived condition qualifier for {paper_key}"
                )
            if f"{fact.follow_up_days}-day" not in combined and f"{fact.follow_up_days} day" not in combined:
                raise AssertionError(
                    f"executed relation request omitted the PDF-derived follow-up boundary for {paper_key}"
                )
            if f"n={fact.sample_size}" not in context_text.replace(" ", ""):
                raise AssertionError(
                    f"executed relation request omitted the PDF-derived sample-size qualifier for {paper_key}"
                )
        observations.append({
            "paper_keys": sorted(pair_set),
            "relation_ids": sorted(pair_relation_ids.intersection(
                str(row.get("relation_id") or "")
                for row in matching_request.get("relation_candidates") or ()
                if isinstance(row, Mapping)
            )),
            "node_id": str(matching_request.get("node_id") or ""),
            "request_level": str(matching_request.get("request_level") or "flat"),
            "findings_left_count": len(matching_bundle.get("findings_left") or ()),
            "findings_right_count": len(matching_bundle.get("findings_right") or ()),
            "context_paper_keys": sorted(str(key) for key, rows in contexts.items() if rows),
            "locator_paper_keys": sorted(str(key) for key, rows in locators.items() if rows),
            "required_evidence_id_count": len(required_evidence),
            "provided_evidence_id_count": len(provided_evidence),
            "missing_evidence_id_count": len(missing_evidence),
            "evidence_completeness": str(matching_bundle.get("evidence_completeness") or ""),
        })

    if distant_pair is not None:
        pair_set = {str(value) for value in distant_pair if str(value)}
        shard_plan = _ready_outline_node_payload(registry, "relation_shard_plan")
        shards = [row for row in shard_plan.get("shards") or () if isinstance(row, Mapping)]
        if not shards or any(
            pair_set.issubset({str(value) for value in row.get("paper_keys") or () if str(value)})
            for row in shards
        ):
            raise AssertionError(
                "distant PDF sources were not separated in the materialized base relation evidence shard plan"
            )
        unassigned_ids = {
            str(value)
            for value in (shard_plan.get("coverage") or {}).get("unassigned_relation_candidate_ids") or ()
            if str(value)
        }
        distant_ids = {
            str(row.get("relation_id") or "")
            for row in candidates
            if {str(value) for value in row.get("paper_keys") or () if str(value)} == pair_set
        }
        if not distant_ids.intersection(unassigned_ids):
            raise AssertionError(
                "distant required relation was not recorded as spanning separate base evidence shards"
            )
    return observations


def _persist_scale_failure_snapshot(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    print(f"RUN_FIRST_SCALE_FAILURE_DIAGNOSTIC={path.resolve()}")


def _capture_scale_failure_or_reraise(
    original_exception: Exception,
    capture: Callable[[], None],
) -> None:
    try:
        capture()
    except Exception as diagnostic_error:  # noqa: BLE001 - preserve the original failure
        add_note = getattr(original_exception, "add_note", None)
        if callable(add_note):
            add_note(
                "run-first scale diagnostic capture failed: "
                f"{type(diagnostic_error).__name__}: {_diagnostic_text(diagnostic_error)}"
            )
    raise original_exception.with_traceback(original_exception.__traceback__)


def test_scale_diagnostic_capture_failure_reraises_original_exception() -> None:
    original = AssertionError("original scale assertion")

    def fail_capture() -> None:
        raise OSError("diagnostic target unavailable")

    with pytest.raises(AssertionError, match="original scale assertion") as caught:
        _capture_scale_failure_or_reraise(original, fail_capture)
    assert caught.value is original


def test_scale_failure_snapshot_keeps_layout_replay_in_loopback_extensions() -> None:
    from types import SimpleNamespace

    extensions = {
        "candidate_organization_layout_captures": [{"candidate_id": "candidate_2", "provider_call_id": "candidate_organization_provider:candidate_2:partition:1"}],
        "candidate_arbitration_model_visible_captures": [{"node_id": "arbitration:final:abc", "candidate_2_section_rows": []}],
        "reader_design_transport_captures": [{"paper_key": "paper-a", "request_body_sha256": "a" * 64}],
    }
    snapshot = _scale_failure_snapshot(
        control=None,
        job_id="failure-capture-test",
        first=None,
        plan=None,
        plan_seconds=None,
        server=SimpleNamespace(calls=[], errors=[], trace=[]),
        source_kind="synthetic_pdf_cold",
        source_variant="all_required_comparisons",
        source_receipt={},
        size=10,
        exception=AssertionError("captured fixture failure"),
    )
    _scale_add_failure_loopback_extensions(snapshot, extensions)
    assert snapshot["schema_version"] == "run-first-scale-failure-diagnostic/v1"
    assert {key: snapshot["loopback"][key] for key in extensions} == extensions


@pytest.mark.optional
@pytest.mark.integration
@pytest.mark.slow
def test_run_first_scale_local_http_full_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_size = os.environ.get("RUN_FIRST_SCALE_SIZE", "").strip()
    if not raw_size:
        pytest.skip("set RUN_FIRST_SCALE_SIZE to exactly one of 10,63,64,100,200,500,600")
    try:
        size = int(raw_size)
    except ValueError as exc:
        raise AssertionError(f"RUN_FIRST_SCALE_SIZE must be one integer, got {raw_size!r}") from exc
    if size not in _SUPPORTED_SCALE_SIZES:
        raise AssertionError(f"RUN_FIRST_SCALE_SIZE must be one of {_SUPPORTED_SCALE_SIZES}, got {size}")
    source_kind = os.environ.get("RUN_FIRST_SCALE_SOURCE", "synthetic_pdf_cold").strip()
    raw_automatic_reuse = os.environ.get("RUN_FIRST_SCALE_AUTOMATIC_REUSE", "").strip()
    if raw_automatic_reuse not in {"", "0", "1"}:
        raise AssertionError("RUN_FIRST_SCALE_AUTOMATIC_REUSE must be empty, 0 or 1")
    automatic_reuse = raw_automatic_reuse == "1"
    if automatic_reuse and source_kind != "synthetic_pdf_warm":
        raise AssertionError("automatic reuse acceptance requires synthetic_pdf_warm")
    mutation_mode = os.environ.get("RUN_FIRST_SCALE_MUTATION", "").strip() or "none"
    if mutation_mode not in {"none", "one_pdf_result", "one_pdf_qualifier"}:
        raise AssertionError(
            "RUN_FIRST_SCALE_MUTATION must be empty, none, one_pdf_result or one_pdf_qualifier"
        )
    incremental_mutation = mutation_mode in {"one_pdf_result", "one_pdf_qualifier"}
    qualifier_only_mutation = mutation_mode == "one_pdf_qualifier"
    if mutation_mode == "one_pdf_result" and (
        source_kind != "synthetic_pdf_warm" or not automatic_reuse
    ):
        raise AssertionError(
            "one_pdf_result mutation requires synthetic_pdf_warm with automatic discovery"
        )
    if qualifier_only_mutation and (
        source_kind != "synthetic_pdf_warm" or not automatic_reuse
    ):
        raise AssertionError(
            "one_pdf_qualifier mutation requires synthetic_pdf_warm with automatic discovery"
        )
    source_variant = os.environ.get("RUN_FIRST_SCALE_VARIANT", "baseline").strip() or "baseline"
    if source_variant not in _SCALE_ADVERSARIAL_VARIANTS:
        raise AssertionError(
            "RUN_FIRST_SCALE_VARIANT must be one of "
            f"{tuple(sorted(_SCALE_ADVERSARIAL_VARIANTS))}, got {source_variant!r}"
        )
    active_rare_condition = _RARE_SCALE_CONDITION
    if qualifier_only_mutation and source_variant != "all_required_comparisons":
        raise AssertionError(
            "one_pdf_qualifier requires all_required_comparisons so the changed paper is in pair 0/last"
        )
    if source_variant != "baseline" and (
        source_kind not in {"synthetic_pdf_cold", "synthetic_pdf_warm"}
        or size not in _SCALE_ADVERSARIAL_SIZES
    ):
        raise AssertionError(
            "adversarial source variants require synthetic_pdf_cold/synthetic_pdf_warm "
            f"and one of {_SCALE_ADVERSARIAL_SIZES}"
        )
    raw_identity_quarantine = os.environ.get("RUN_FIRST_SCALE_IDENTITY_QUARANTINE_EXTRA", "").strip()
    if raw_identity_quarantine not in {"", "0", "1"}:
        raise AssertionError("RUN_FIRST_SCALE_IDENTITY_QUARANTINE_EXTRA must be empty, 0 or 1")
    identity_quarantine_extra = raw_identity_quarantine == "1"
    if identity_quarantine_extra and (
        size != 10 or source_kind != "synthetic_pdf_cold"
        or source_variant != "all_required_comparisons"
        or automatic_reuse or mutation_mode != "none"
    ):
        raise AssertionError("identity quarantine requires cold size 10 and all_required_comparisons")
    expect_budget_blocked = os.environ.get(
        "RUN_FIRST_SCALE_EXPECT_BUDGET_BLOCKED", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if expect_budget_blocked and source_variant != "baseline":
        raise AssertionError("default-budget rejection remains the historical baseline-only fixture")
    if identity_quarantine_extra and expect_budget_blocked:
        raise AssertionError("identity quarantine requires an explicit finite local budget")
    provider_budget = _scale_provider_budget(
        size,
        source_kind,
        expect_blocked=expect_budget_blocked,
    )
    candidate_layout_capture: list[dict[str, Any]] = []
    candidate_layout_arbitration_capture: list[dict[str, Any]] = []
    candidate_layout_arbitration_capture_errors: list[dict[str, str]] = []
    reader_design_transport_capture: list[dict[str, Any]] = []
    fixture_transport_targets: Counter[tuple[str, str, int | None]] = Counter()
    fixture_transport_origin: tuple[str, str, int | None] | None = None
    candidate_layout_metrics: dict[str, Any] | None = None
    candidate_layout_variant_enabled = (
        source_variant == "all_required_comparisons"
        and source_kind in {"synthetic_pdf_cold", "synthetic_pdf_warm"}
        and not identity_quarantine_extra
    )
    if candidate_layout_variant_enabled:
        import contextvars
        from outline.candidate_fragment_bank import validate_candidate_organization_map_for_request_v1
        from tests import run_first_candidate_fragments_fixture as organization_fixture
        import sys

        original_organization_response = organization_fixture._organization_response
        current_organization_node: contextvars.ContextVar[str] = contextvars.ContextVar(
            "scale_candidate_organization_node", default="",
        )

        def organization_response_with_candidate_layout(raw_request, *, preferred_indices=()):
            # Match the current fixture callback: transport-only authority is
            # stripped from a copy, and the original provider request stays untouched.
            request = {
                key: value for key, value in raw_request.items()
                if key != "_prompt_authority"
            }
            base_map = original_organization_response(
                request, preferred_indices=preferred_indices,
            )
            variant_map = _scale_merge_noncomparison_organization_fragments(
                request, base_map, validator=validate_candidate_organization_map_for_request_v1,
            )
            axis = request.get("axis_payload")
            axis = axis if isinstance(axis, Mapping) else {}
            candidate_id = str(request.get("candidate_id") or "")
            provider_call_id = current_organization_node.get()
            expected_node_prefix = f"candidate_organization_provider:{candidate_id}"
            if not (
                provider_call_id == expected_node_prefix
                or provider_call_id.startswith(expected_node_prefix + ":partition:")
            ):
                raise AssertionError("organization fixture response is not bound to its actual provider node")
            base_membership = _scale_fragment_membership_signature(request, base_map)
            variant_membership = _scale_fragment_membership_signature(request, variant_map)
            base_global_layout = _scale_global_organization_map_signature(request, base_map)
            variant_global_layout = _scale_global_organization_map_signature(request, variant_map)
            partition = request.get("partition")
            candidate_layout_capture.append({
                "candidate_id": candidate_id,
                "provider_call_id": provider_call_id,
                "partition_index": int(partition.get("partition_index")) if isinstance(partition, Mapping) else None,
                "partition_count": int(partition.get("partition_count")) if isinstance(partition, Mapping) else 1,
                "full_bank_group_count": int(partition.get("full_bank_group_count")) if isinstance(partition, Mapping) else int(request.get("bank_group_count") or 0),
                "partition_local_to_global_group_indices": (
                    [int(value) for value in partition.get("local_to_global_group_indices") or ()]
                    if isinstance(partition, Mapping) else None
                ),
                "required_group_indices": [
                    int(value) for value in request.get("required_group_indices") or ()
                ],
                "required_comparison_sections": [
                    {
                        key: row.get(key)
                        for key in ("group_index", "metadata_index", "paper_indices")
                        if key in row
                    }
                    for row in request.get("required_comparison_sections") or ()
                    if isinstance(row, Mapping)
                ],
                "axis_id": str(axis.get("axis_id") or ""),
                "organizing_logic": str(axis.get("organizing_logic") or ""),
                "preferred_dimensions": list(axis.get("preferred_dimensions") or ()),
                "base_section_count": len(base_map.get("sections") or ()),
                "variant_section_count": len(variant_map.get("sections") or ()),
                "base_layout": _scale_organization_layout_signature(base_map),
                "variant_layout": _scale_organization_layout_signature(variant_map),
                "base_global_layout": base_global_layout,
                "variant_global_layout": variant_global_layout,
                "base_membership_sha256": hashlib.sha256(
                    json.dumps(base_membership, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "variant_membership_sha256": hashlib.sha256(
                    json.dumps(variant_membership, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            })
            return variant_map

        monkeypatch.setattr(
            organization_fixture,
            "_organization_response",
            organization_response_with_candidate_layout,
        )
        original_outline_provider_response = _outline_provider_response

        def outline_provider_response_with_layout_readback(
            node_id: str,
            request: Mapping[str, Any],
            fixture_trace: list[dict[str, Any]] | None = None,
            arbitration_fixture_state: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            try:
                model_view = _scale_capture_model_visible_arbitration_layout(node_id, request)
            except Exception as exc:  # Read-only capture errors must not alter the fixture response.
                candidate_layout_arbitration_capture_errors.append({
                    "node_id": str(node_id),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:400],
                })
                model_view = None
            if model_view is not None:
                candidate_layout_arbitration_capture.append(model_view)
            token = current_organization_node.set(str(node_id))
            try:
                return original_outline_provider_response(
                    node_id, request, fixture_trace, arbitration_fixture_state,
                )
            finally:
                current_organization_node.reset(token)

        monkeypatch.setattr(
            sys.modules[__name__],
            "_outline_provider_response",
            outline_provider_response_with_layout_readback,
        )
    repo_root = Path(__file__).resolve().parents[1]
    source_receipt: dict[str, Any] = {}
    candidate_design_profile: dict[str, Any] | None = None
    controlled_pdf_oracle: ControlledPdfCorpus | None = None
    distant_pair: list[str] | None = None
    extra_quarantine_paper: ScalePaper | None = None
    quarantine_expected_doi = ""
    frozen_pair_indices: list[list[int]] = []
    identity_expected_comparisons: list[list[str]] = []
    if source_kind in {"synthetic_pdf_cold", "synthetic_pdf_warm"}:
        papers = _scale_papers(size, source_variant=source_variant)
        if identity_quarantine_extra:
            original_by_key = {paper.paper_key: paper for paper in papers}
            frozen_pairs = _scale_required_comparisons(papers, source_variant)
            frozen_pair_indices = [[original_by_key[key].index for key in pair] for pair in frozen_pairs]
            key_map = {
                paper.paper_key: build_paper_key({
                    "title": paper.title, "doi": f"10.5555/rfs-{paper.index:04d}",
                }) for paper in papers
            }
            identity_expected_comparisons = [[key_map[key] for key in pair] for pair in frozen_pairs]
            papers = [replace(paper, paper_key=key_map[paper.paper_key]) for paper in papers]
            by_index = {paper.index: paper for paper in papers}
            assert all(
                replace(original_by_key[old], paper_key=new) == by_index[original_by_key[old].index]
                for old, new in key_map.items()
            )
            extra_quarantine_paper = _scale_papers(size + 1, source_variant="baseline")[-1]
            quarantine_expected_doi = "10.5555/rfs-expected-extra"
        if candidate_layout_variant_enabled:
            papers, candidate_design_profile = _scale_add_candidate_design_profiles(papers)
        pdf_dir = tmp_path / "papers"
        pdf_dir.mkdir()
        for paper in papers:
            _write_scale_pdf(
                pdf_dir / f"{paper.stem}.pdf", paper,
                identity_doi=paper.paper_key if identity_quarantine_extra else "",
            )
        if identity_quarantine_extra:
            assert extra_quarantine_paper is not None
            extra_pdf = pdf_dir / f"{extra_quarantine_paper.stem}.pdf"
            _write_scale_pdf(extra_pdf, extra_quarantine_paper, identity_doi="10.5555/rfs-actual-extra")
        with fitz.open(pdf_dir / f"{papers[0].stem}.pdf") as first_pdf:
            first_pdf_text = "\n".join(page.get_text() for page in first_pdf)
        assert papers[0].finding in first_pdf_text
        assert "Table 2 (page 2)" in first_pdf_text
        mixed_paper = next((paper for paper in papers if paper.chinese_note), None)
        if mixed_paper is not None:
            with fitz.open(pdf_dir / f"{mixed_paper.stem}.pdf") as document:
                extracted = " ".join(
                    " ".join(page.get_text().split()) for page in document
                )
                assert " ".join(mixed_paper.chinese_note.split()) in extracted
        source = RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir))
        if identity_quarantine_extra:
            from types import SimpleNamespace

            assert extra_quarantine_paper is not None
            extra_pdf = pdf_dir / f"{extra_quarantine_paper.stem}.pdf"
            report_path = tmp_path / "controlled-zotero-report.txt"
            source_rows = [
                {"title": paper.title, "doi": paper.paper_key, "canonical_paper_key": paper.paper_key}
                for paper in papers
            ] + [{
                "title": extra_quarantine_paper.title,
                "doi": quarantine_expected_doi,
                "canonical_paper_key": quarantine_expected_doi,
            }]
            report_path.write_text(json.dumps(source_rows), encoding="utf-8")
            pdf_by_title = {
                paper.title: str(pdf_dir / f"{paper.stem}.pdf") for paper in papers
            }
            pdf_by_title[extra_quarantine_paper.title] = str(extra_pdf)
            parser_result = SimpleNamespace(
                papers=source_rows, status="ok", parser_route="standard",
                parser_version="controlled-scale-zotero-parser-fixture",
                report_hash=hashlib.sha256(report_path.read_bytes()).hexdigest(),
                parse_confidence=1.0,
                stats=SimpleNamespace(to_dict=lambda: {"parsed_entries": len(source_rows)}),
                diagnostics=(),
            )
            monkeypatch.setattr("runtime.source_intake.parse_zotero_report_result",
                                lambda _path: parser_result)

            def resolve_controlled_pdf(paper: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> Any:
                path = pdf_by_title[str(paper.get("title") or "")]
                return SimpleNamespace(
                    status="matched", selected_path=path, candidates=(),
                    to_dict=lambda: {"status": "matched", "selected_path": path,
                                     "candidates": [], "diagnostics": []},
                )

            monkeypatch.setattr("runtime.source_intake.resolve_pdf_match", resolve_controlled_pdf)
            source = RuntimeSourceSpec(mode="zotero", zotero_report=str(report_path),
                                      library_path=str(pdf_dir))
        reuse_paths: tuple[str, ...] = ()
        required_comparisons = (
            identity_expected_comparisons
            if identity_quarantine_extra
            else _scale_required_comparisons(papers, source_variant)
        )
        required_pair = required_comparisons[0]
        adversarial_pair = (
            required_comparisons[1]
            if source_variant != "baseline"
            else None
        )
        distant_pair = (
            required_comparisons[-1]
            if identity_quarantine_extra
            else (
                _scale_required_pair(papers, "distant_cross_shard")
                if source_variant in {"distant_cross_shard", "all_required_comparisons"}
                else None
            )
        )
        # Join source identities to paths only. All gold findings and limits
        # are independently extracted by the oracle from the actual PDFs.
        controlled_pdf_oracle = ControlledPdfCorpus.from_pdfs({
            paper.paper_key: pdf_dir / f"{paper.stem}.pdf"
            for paper in papers
        })
        if source_variant == "required_null":
            directions = {
                key: [fact.direction for fact in controlled_pdf_oracle.documents[key].facts]
                for key in adversarial_pair or ()
            }
            assert "increase" in directions[adversarial_pair[0]]
            assert "null" in directions[adversarial_pair[1]]
        elif source_variant == "required_reverse":
            directions = {
                key: [fact.direction for fact in controlled_pdf_oracle.documents[key].facts]
                for key in adversarial_pair or ()
            }
            assert "increase" in directions[adversarial_pair[0]]
            assert "decrease" in directions[adversarial_pair[1]]
        elif source_variant == "distant_cross_shard":
            rare_matches = [
                (paper_key, fact)
                for paper_key, document in controlled_pdf_oracle.documents.items()
                for fact in document.facts
                if fact.condition.casefold() == _RARE_SCALE_CONDITION.casefold()
            ]
            assert len(rare_matches) == 1, rare_matches
            assert rare_matches[0][0] == papers[-1].paper_key
            assert papers[-1].paper_key in (adversarial_pair or ())
            assert adversarial_pair == [papers[0].paper_key, papers[-1].paper_key]
        elif source_variant == "all_required_comparisons":
            if identity_quarantine_extra:
                assert frozen_pair_indices == [[0, 1], [0, 2], [0, 3], [0, size - 1]]
                null_pair, reverse_pair = required_comparisons[1:3]
            else:
                null_pair = _scale_required_pair(papers, "required_null")
                reverse_pair = _scale_required_pair(papers, "required_reverse")
                assert required_comparisons == [
                    _scale_required_pair(papers, "baseline"), null_pair, reverse_pair,
                    _scale_required_pair(papers, "distant_cross_shard"),
                ]
            null_facts = [controlled_pdf_oracle.documents[key].facts[0] for key in null_pair]
            reverse_facts = [controlled_pdf_oracle.documents[key].facts[0] for key in reverse_pair]
            assert [fact.direction for fact in null_facts] == ["increase", "null"]
            assert [fact.magnitude_points for fact in null_facts] == [4, None]
            assert [fact.direction for fact in reverse_facts] == ["increase", "decrease"]
            assert [fact.magnitude_points for fact in reverse_facts] == [4, 2]
            rare_matches = [
                (paper_key, fact)
                for paper_key, document in controlled_pdf_oracle.documents.items()
                for fact in document.facts
                if fact.condition.casefold() == _RARE_SCALE_CONDITION.casefold()
            ]
            assert len(rare_matches) == 1, rare_matches
            assert rare_matches[0][0] == papers[-1].paper_key
            assert distant_pair == [papers[0].paper_key, papers[-1].paper_key]
            assert distant_pair in required_comparisons
        review_intent: dict[str, Any] = {
            "required_comparisons": [
                {
                    "paper_keys": pair,
                    "question": (
                        "Compare the two source-reported findings within their recorded sample boundaries."
                        if pair == required_pair
                        else (
                            "Compare only the directions, magnitudes, conditions, sample labels, and "
                            "30-day limits printed in these two source PDFs; retain null or reverse findings."
                        )
                    ),
                    "relation_type": "reported_magnitude_contrast",
                }
                for pair in required_comparisons
            ],
        }
        reader_expected = size + (1 if incremental_mutation else 0)
        source_rows_for_trace: list[ScalePaper] = papers
    elif source_kind == "r1_typed_summary_reuse":
        if size != 63:
            raise AssertionError("r1_typed_summary_reuse is defined only for the real 63-paper corpus")
        source_receipt = _load_r1_typed_sources(repo_root)
        source = RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(source_receipt["zotero_report"]),
            library_path=str(source_receipt["library_path"]),
        )
        reuse_paths = tuple(str(path) for path in source_receipt["summary_paths"])
        review_intent = dict(source_receipt["review_intent"])
        required_rows = review_intent.get("required_comparisons") or ()
        required_pair = [
            str(value)
            for value in (required_rows[0].get("paper_keys") or ())
            if str(value)
        ] if required_rows and isinstance(required_rows[0], Mapping) else []
        required_comparisons = [required_pair] if required_pair else []
        adversarial_pair = None
        distant_pair = None
        reader_expected = 0
        source_rows_for_trace = []
    else:
        raise AssertionError("RUN_FIRST_SCALE_SOURCE must be synthetic_pdf_cold, synthetic_pdf_warm or r1_typed_summary_reuse")

    job_id = (
        f"run-first-scale-{size}-local-http"
        if source_variant == "baseline"
        else f"run-first-scale-{size}-{source_variant}-local-http"
    )
    control: ReviewControlPlane | None = None
    plan: dict[str, Any] | None = None
    plan_seconds: float | None = None
    first: Any = None
    latest_execution_result: Mapping[str, Any] | None = None
    sampler: _MemorySampler | None = None
    tracemalloc_running = False
    warm_seed_metrics: dict[str, Any] | None = None
    source_config = configparser.ConfigParser(interpolation=None)
    source_config.read(repo_root / "config.ini.example", encoding="utf-8")
    reader_initial_output_cap = int(source_config["Stage1_Input"]["stage1_synthesis_max_output_tokens"])
    admission_overrides, independent_seed_budget = resolve_scale_test_budgets(
        os.environ, paper_count=size, reader_initial_output_cap=reader_initial_output_cap,
        default_seed_budget=provider_budget if source_kind == "synthetic_pdf_warm" else None,
    )
    server, thread, api_base = _start_scale_server(
        source_rows_for_trace,
        source_label_map=(
            {paper.paper_key: paper.title for paper in papers}
            if identity_quarantine_extra and source_kind in {"synthetic_pdf_cold", "synthetic_pdf_warm"}
            else None
        ),
    )
    try:
        config_path = _local_config(
            tmp_path,
            api_base,
            admission_overrides=admission_overrides,
            provider_call_limit=(
                int(provider_budget["max_provider_calls_total"])
                if provider_budget is not None
                else None
            ),
            technical_shard_target_tokens=(
                8_000 if source_variant in {"distant_cross_shard", "all_required_comparisons"} else 0
            ),
            outline_output_tokens=(
                int(os.environ["RUN_FIRST_SCALE_OUTLINE_OUTPUT_TOKENS"])
                if os.environ.get("RUN_FIRST_SCALE_OUTLINE_OUTPUT_TOKENS", "").strip()
                else None
            ),
        )
        if candidate_layout_variant_enabled:
            import requests
            import socket
            from urllib.parse import urlsplit

            local_config = configparser.ConfigParser(interpolation=None)
            local_config.read(config_path, encoding="utf-8")
            configured_origins = {
                (parts.scheme, parts.hostname or "", parts.port)
                for section in local_config.sections()
                if (api_value := str(local_config[section].get("api_base") or "").strip())
                and (parts := urlsplit(api_value)).scheme
                and parts.hostname
            }
            expected_parts = urlsplit(api_base)
            fixture_transport_origin = (
                expected_parts.scheme,
                expected_parts.hostname or "",
                expected_parts.port,
            )
            if (
                len(configured_origins) != 1
                or configured_origins != {fixture_transport_origin}
                or fixture_transport_origin[0] != "http"
                or fixture_transport_origin[1] not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise AssertionError("layout fixture configuration is not bound to one loopback HTTP origin")
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
            monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")

            allowed_socket_target = ("127.0.0.1", int(fixture_transport_origin[2] or 0))
            socket_targets: Counter[tuple[str, int]] = Counter()
            original_socket_connect = socket.socket.connect
            original_socket_connect_ex = socket.socket.connect_ex

            def require_fixture_socket_target(address):
                if not isinstance(address, tuple) or len(address) < 2:
                    raise AssertionError("local fixture socket attempted a non-IP connection")
                target = (str(address[0]), int(address[1]))
                if target != allowed_socket_target:
                    raise AssertionError("local fixture socket attempted a connection outside its server address")
                socket_targets[target] += 1

            def fixture_only_socket_connect(sock, address):
                require_fixture_socket_target(address)
                return original_socket_connect(sock, address)

            def fixture_only_socket_connect_ex(sock, address):
                require_fixture_socket_target(address)
                return original_socket_connect_ex(sock, address)

            monkeypatch.setattr(socket.socket, "connect", fixture_only_socket_connect)
            monkeypatch.setattr(socket.socket, "connect_ex", fixture_only_socket_connect_ex)

            design_by_index = {
                int(row["paper_index"]): dict(row)
                for row in (candidate_design_profile or {}).get("designs") or ()
                if isinstance(row, Mapping)
            }
            paper_key_by_index = {paper.index: paper.paper_key for paper in papers}
            original_send = requests.Session.send

            def layout_fixture_only_send(session, prepared_request, **kwargs):
                target = urlsplit(str(prepared_request.url or ""))
                target_origin = (target.scheme, target.hostname or "", target.port)
                if target_origin != fixture_transport_origin:
                    raise AssertionError("layout fixture attempted an HTTP request outside its local origin")
                proxy_map = kwargs.get("proxies")
                if isinstance(proxy_map, Mapping) and any(str(value or "").strip() for value in proxy_map.values()):
                    raise AssertionError("local fixture request has an effective HTTP proxy")
                fixture_transport_targets[target_origin] += 1
                if str(prepared_request.method or "").upper() == "POST":
                    body = prepared_request.body
                    if isinstance(body, str):
                        raw_body = body.encode("utf-8")
                    elif isinstance(body, bytes):
                        raw_body = body
                    else:
                        raw_body = b""
                    if raw_body:
                        try:
                            payload = json.loads(raw_body)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            payload = None
                        if isinstance(payload, Mapping) and payload.get("model") == "rfs-reader-local":
                            messages = payload.get("messages") or ()
                            user_text = "\n".join(
                                str(item.get("content") or "")
                                for item in messages
                                if isinstance(item, Mapping) and item.get("role") == "user"
                            )
                            identifier = _RFS_ID.search(user_text)
                            if identifier is None:
                                raise AssertionError("reader HTTP request omitted its source paper identifier")
                            paper_index = int(identifier.group(1))
                            design_row = design_by_index.get(paper_index)
                            if design_row is None:
                                raise AssertionError("reader HTTP request paper is outside the design-profile fixture")
                            normalized_user_text = " ".join(user_text.split()).casefold()
                            design_statement = " ".join(
                                str(design_row.get("design_statement") or "").split()
                            ).casefold()
                            design_present = bool(design_statement) and design_statement in normalized_user_text
                            if not design_present:
                                raise AssertionError(
                                    "actual Reader HTTP user message omitted its paper's human-readable design statement"
                                )
                            bridge_expected = paper_index == 0
                            bridge_statement = " ".join(
                                str((candidate_design_profile or {}).get("article_bridge_statement") or "").split()
                            ).casefold()
                            bridge_present = bridge_expected and bridge_statement in normalized_user_text
                            estimates_not_pooled = bridge_expected and "estimates are not pooled" in normalized_user_text
                            no_shared_mediator = bridge_expected and "no shared mediator is asserted" in normalized_user_text
                            if bridge_expected and not all((bridge_present, estimates_not_pooled, no_shared_mediator)):
                                raise AssertionError(
                                    "actual Reader HTTP user message omitted the article bridge or its non-pooling limits"
                                )
                            reader_design_transport_capture.append({
                                "paper_index": paper_index,
                                "paper_key": paper_key_by_index[paper_index],
                                "design_type": str(design_row.get("design_type") or ""),
                                "design_statement_present": design_present,
                                "article_bridge_present": bridge_present,
                                "estimates_not_pooled_present": estimates_not_pooled,
                                "no_shared_mediator_present": no_shared_mediator,
                                "request_body_sha256": hashlib.sha256(raw_body).hexdigest(),
                                "user_text_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
                            })
                return original_send(session, prepared_request, **kwargs)

            monkeypatch.setattr(requests.Session, "send", layout_fixture_only_send)
        spec_metadata: dict[str, Any] = {
            "review_intent": review_intent,
            **(
                {"allow_partial_source_quarantine": True}
                if source_kind == "r1_typed_summary_reuse" or identity_quarantine_extra
                else {}
            ),
        }
        if provider_budget is not None:
            spec_metadata["provider_budget"] = dict(provider_budget)
        spec = RuntimeJobSpec(
            project_name=(
                f"run-first-scale-{size}"
                if source_variant == "baseline"
                else f"run-first-scale-{size}-{source_variant}"
            ),
            source=source,
            job_id=job_id,
            config=str(config_path),
            action="run_all",
            reuse_stage1=True if reuse_paths else None,
            reuse_summary_files=reuse_paths,
            queue_file=str(tmp_path / "queue.json"),
            metadata=spec_metadata,
        )
        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        spec_path = tmp_path / "runtime-job-spec.json"
        spec_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        plan_started = time.perf_counter()
        plan = control.plan(spec_path)
        plan_seconds = time.perf_counter() - plan_started
        assert plan.get("read_only") is True, plan
        assert plan.get("full_stage_request_plan", {}).get("boundary", {}).get("no_provider_posts") is True, plan
        assert not server.calls, "provider-free production plan unexpectedly emitted an HTTP request"  # type: ignore[attr-defined]
        if provider_budget is not None:
            full_stage = plan.get("full_stage_request_plan") or {}
            assert full_stage.get("aggregate_budget") == provider_budget, full_stage
            assert provider_budget["max_retry_attempts_total"] == 0
            assert provider_budget["max_wall_seconds"] > 0

        sampler = _MemorySampler()
        if os.environ.get("RUN_FIRST_SCALE_TRACE_ALLOCATIONS", "").strip().casefold() in {"1", "true", "yes", "on"}:
            tracemalloc.start()
            tracemalloc_running = True
        sampler.start()
        started = time.perf_counter()
        if source_kind == "synthetic_pdf_warm":
            if expect_budget_blocked or provider_budget is None:
                raise AssertionError("warm source acceptance requires explicit finite local limits")
            if independent_seed_budget is None:
                raise AssertionError("warm scale seed requires a separate finite allowance")
            seed_metadata = {**dict(spec.metadata), "provider_budget": independent_seed_budget.to_dict()}
            seed_spec = replace(spec, job_id=f"{job_id}-seed", action="analyze", reuse_stage1=False,
                                metadata=seed_metadata)
            seed_started = time.perf_counter()
            seed_pdf_hashes = {
                paper.paper_key: hashlib.sha256(
                    (pdf_dir / f"{paper.stem}.pdf").read_bytes()
                ).hexdigest()
                for paper in papers
            }
            seed = AgentRuntimeRunner(seed_spec).run()
            seed_seconds = time.perf_counter() - seed_started
            assert seed.job_status == "completed", seed
            assert seed.completed_stages == ("source_intake", "analyze"), seed
            seed_posts = len(server.calls)  # type: ignore[attr-defined]
            assert seed_posts == size
            assert all(call.get("stage") == "stage1_reader" for call in server.calls)  # type: ignore[attr-defined]
            changed_paper_keys: set[str] = set()
            current_pdf_dir = pdf_dir
            qualifier_prior_fact: Any = None
            if incremental_mutation:
                current_pdf_dir = tmp_path / "papers-current"
                current_pdf_dir.mkdir()
                for paper in papers:
                    source_path = pdf_dir / f"{paper.stem}.pdf"
                    target_path = current_pdf_dir / source_path.name
                    target_path.write_bytes(source_path.read_bytes())
                mutation_index = size - 1
                original_paper = papers[mutation_index]
                if qualifier_only_mutation:
                    if controlled_pdf_oracle is None:
                        raise AssertionError("qualifier mutation requires the original PDF oracle")
                    prior_facts = controlled_pdf_oracle.documents[original_paper.paper_key].facts
                    if len(prior_facts) != 1:
                        raise AssertionError("selected scale paper must have exactly one parsed source fact")
                    qualifier_prior_fact = prior_facts[0]
                mutated_paper = (
                    _mutated_scale_qualifier_paper(original_paper)
                    if qualifier_only_mutation
                    else _mutated_scale_paper(original_paper)
                )
                papers[mutation_index] = mutated_paper
                _write_scale_pdf(current_pdf_dir / f"{mutated_paper.stem}.pdf", mutated_paper)
                changed_paper_keys.add(mutated_paper.paper_key)
                current_pdf_hashes = {
                    paper.paper_key: hashlib.sha256(
                        (current_pdf_dir / f"{paper.stem}.pdf").read_bytes()
                    ).hexdigest()
                    for paper in papers
                }
                changed_hash_keys = {
                    key for key in seed_pdf_hashes
                    if seed_pdf_hashes[key] != current_pdf_hashes[key]
                }
                assert changed_hash_keys == changed_paper_keys
                assert all(
                    seed_pdf_hashes[key] == current_pdf_hashes[key]
                    for key in set(seed_pdf_hashes) - changed_paper_keys
                )
                # Reconstruct the source oracle from the current bytes after
                # the seed run; the fixture response follows the same current
                # ScalePaper row and canonical key.
                controlled_pdf_oracle = ControlledPdfCorpus.from_pdfs({
                    paper.paper_key: current_pdf_dir / f"{paper.stem}.pdf"
                    for paper in papers
                })
                if qualifier_only_mutation:
                    current_facts = controlled_pdf_oracle.documents[mutated_paper.paper_key].facts
                    if qualifier_prior_fact is None or len(current_facts) != 1:
                        raise AssertionError("qualifier-only PDF revision lost its unique source fact")
                    current_fact = current_facts[0]
                    assert (
                        current_fact.paper_key, current_fact.study_label, current_fact.sample_size,
                        current_fact.direction, current_fact.magnitude_points, current_fact.follow_up_days,
                        current_fact.required_qualifiers,
                    ) == (
                        qualifier_prior_fact.paper_key, qualifier_prior_fact.study_label,
                        qualifier_prior_fact.sample_size, qualifier_prior_fact.direction,
                        qualifier_prior_fact.magnitude_points, qualifier_prior_fact.follow_up_days,
                        qualifier_prior_fact.required_qualifiers,
                    )
                    assert "returning-member-only loyalty credit condition" in current_fact.condition.casefold()
                    assert "first-visit-only loyalty credit condition" not in current_fact.condition.casefold()
                    active_rare_condition = current_fact.condition
                # All later comparison, candidate and Writer/citation support
                # audits use this corpus parsed from the revised PDF bytes.
            warmed = run_first_warm_reuse(
                seed_workspace_path=seed.workspace_path, seed_spec=seed_spec,
                current_shared_fixture_config=config_path, provider_budget=provider_budget,
                action="run_all",
                automatic_discovery=automatic_reuse,
                changed_paper_keys=changed_paper_keys,
                current_pdf_folder=current_pdf_dir if incremental_mutation else None,
            )
            first, spec, plan, plan_seconds = warmed.result, warmed.spec, dict(warmed.plan), warmed.plan_seconds
            job_id = first.job_id
            if incremental_mutation:
                assert all(
                    seed_pdf_hashes[paper.paper_key]
                    == hashlib.sha256((pdf_dir / f"{paper.stem}.pdf").read_bytes()).hexdigest()
                    for paper in papers
                )
                seed_registry = AgentRuntimeRunner._open_workspace(seed.workspace_path)[1]

                def _typed_manifest_rows(registry: Any, owner_job_id: str) -> dict[str, tuple[Any, dict[str, Any]]]:
                    rows: dict[str, tuple[Any, dict[str, Any]]] = {}
                    for record in registry.list_records():
                        if (
                            record.artifact_type != "stage1_reusable_summary_manifest"
                            or record.status != "ready"
                            or str(record.job_id) != owner_job_id
                        ):
                            continue
                        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
                        if not isinstance(payload, Mapping):
                            raise AssertionError("typed Stage 1 manifest payload is not an object")
                        paper_key = str(payload.get("canonical_paper_key") or "")
                        if not paper_key or paper_key in rows:
                            raise AssertionError("seed has duplicate typed manifest keys")
                        rows[paper_key] = (record, dict(payload))
                    return rows

                seed_manifests = _typed_manifest_rows(seed_registry, seed.job_id)
                expected_paper_keys = {paper.paper_key for paper in papers}
                assert set(seed_manifests) == expected_paper_keys
                source_manifest = warmed.registry.get("summary_source_manifest")
                assert source_manifest is not None and source_manifest.status == "ready"
                source_payload = json.loads(Path(source_manifest.path).read_text(encoding="utf-8"))
                assert isinstance(source_payload, Mapping)
                source_items = source_payload.get("source_items")
                assert isinstance(source_items, list) and len(source_items) == size
                by_key = {
                    str(item.get("canonical_paper_key") or ""): dict(item)
                    for item in source_items
                    if isinstance(item, Mapping)
                }
                assert set(by_key) == expected_paper_keys
                assert sum(item.get("disposition") == "reused" for item in by_key.values()) == size - 1
                assert sum(item.get("disposition") == "provider_generated" for item in by_key.values()) == 1
                assert set(changed_paper_keys) == {
                    key for key, item in by_key.items()
                    if item.get("disposition") == "provider_generated"
                }
                for paper_key, item in by_key.items():
                    if item.get("disposition") == "reused":
                        reuse_id = str(item.get("reuse_evidence_id") or "")
                        assert reuse_id
                        evidence = warmed.registry.get(reuse_id)
                        assert evidence is not None and evidence.status == "ready"
                        assert evidence.artifact_type == "stage1_summary_reuse_record"
                        reuse_payload = json.loads(Path(evidence.path).read_text(encoding="utf-8"))
                        seed_record, seed_payload = seed_manifests[paper_key]
                        assert reuse_payload.get("source_authority_job_id") == seed.job_id
                        assert reuse_payload.get("reused_summary_artifact_id") == seed_payload.get("source_summary_artifact_id")
                        assert reuse_payload.get("reused_summary_artifact_hash") == seed_payload.get("source_summary_artifact_hash")
                        assert reuse_payload.get("typed_manifest_artifact_hash") == seed_record.content_hash
                        assert reuse_payload.get("portable_source_summary_manifest_hash") == seed_record.content_hash
                        assert seed_record.status == "ready"
                        assert seed_record.job_id == seed.job_id
                        assert seed_record.artifact_type == "stage1_reusable_summary_manifest"
                        assert seed_record.artifact_version == "v1"
                        assert file_sha256(seed_record.path) == seed_record.content_hash
                        original_source_id = str(seed_payload["source_summary_artifact_id"])
                        original_source_hash = str(seed_payload["source_summary_artifact_hash"])
                        assert reuse_payload.get("portable_source_artifact_hash") == original_source_hash
                        original_source_path = Path(str(seed_payload["source_summary_artifact_path"]))
                        assert reuse_payload.get("source_authority_artifact_id") == original_source_id
                        assert reuse_payload.get("source_authority_artifact_hash") == original_source_hash
                        assert Path(str(reuse_payload["source_authority_artifact_path"])).resolve() == original_source_path.resolve()
                        original_source = seed_registry.get(original_source_id)
                        assert (
                            original_source is not None and original_source.status == "ready"
                            and original_source.job_id == seed.job_id
                            and original_source.artifact_type == "summary_file"
                            and original_source.artifact_version == seed_payload["source_summary_artifact_version"]
                            and original_source.content_hash == original_source_hash
                            and Path(original_source.path).resolve() == original_source_path.resolve()
                            and file_sha256(original_source.path) == original_source_hash
                        )
                        # Portable records have local IDs and preserve the exact
                        # seed manifest/source bytes and original owner above.
                        for local_id, artifact_type, expected_hash in (
                            (str(reuse_payload["portable_source_summary_manifest_id"]),
                             "stage1_portable_summary_manifest", seed_record.content_hash),
                            (str(reuse_payload["portable_source_artifact_id"]),
                             "stage1_portable_summary_source", original_source_hash),
                        ):
                            portable = warmed.registry.get(local_id)
                            assert (
                                portable is not None and portable.status == "ready"
                                and portable.job_id == warmed.spec.job_id
                                and portable.artifact_type == artifact_type
                                and portable.artifact_version == "v1"
                                and portable.content_hash == expected_hash
                                and file_sha256(portable.path) == expected_hash
                            )
                    else:
                        local_owners = []
                        for record in warmed.registry.list_records():
                            if (
                                record.job_id != warmed.spec.job_id
                                or record.status != "ready"
                                or record.artifact_type != "paper_artifact"
                            ):
                                continue
                            raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
                            body = raw.get("payload") if isinstance(raw, Mapping) else None
                            body = body if isinstance(body, Mapping) else raw
                            paper_info = body.get("paper_info") if isinstance(body, Mapping) else None
                            owner_key = str(
                                (paper_info.get("canonical_paper_key") if isinstance(paper_info, Mapping) else "")
                                or (body.get("canonical_paper_key") if isinstance(body, Mapping) else "")
                                or (body.get("paper_key") if isinstance(body, Mapping) else "")
                                or ""
                            )
                            if owner_key == paper_key and file_sha256(record.path) == record.content_hash:
                                local_owners.append(record)
                        assert len(local_owners) == 1
            assert len(warmed.reuse_manifest_paths) == size - (1 if incremental_mutation else 0)
            assert warmed.spec.reuse_stage1 is True
            if automatic_reuse:
                assert not warmed.spec.reuse_summary_files
                assert not warmed.spec.summary_file and not warmed.spec.summary_sources
            else:
                assert len(warmed.spec.reuse_summary_files) == size
            warm_reader_posts = sum(call.get("stage") == "stage1_reader"
                                    for call in server.calls[seed_posts:])  # type: ignore[attr-defined]
            assert warm_reader_posts == (1 if incremental_mutation else 0)
            warm_seed_metrics = {
                "seed_workspace_path": seed.workspace_path, "seed_job_id": seed.job_id,
                "seed_seconds": round(seed_seconds, 3), "seed_provider_posts": seed_posts,
                "seed_provider_budget": independent_seed_budget.to_dict(), "warm_provider_budget": dict(provider_budget),
                "warm_initial_reader_posts": warm_reader_posts,
                "typed_reuse_manifest_count": len(warmed.reuse_manifest_paths),
                "incremental_mutation": mutation_mode,
                "changed_paper_keys": sorted(changed_paper_keys),
                "discovery_mode": "automatic" if automatic_reuse else "explicit_manifest_paths",
                "submitted_reuse_summary_paths": len(warmed.spec.reuse_summary_files),
                "pdf_hashes_verified": len(warmed.paper_pdf_sha256),
                "accounting_scope": "separate_seed_and_warm_jobs_same_server_config_and_pdfs",
            }
        else:
            first = AgentRuntimeRunner(spec).run()
        runner_seconds = time.perf_counter() - started
        if expect_budget_blocked:
            assert provider_budget is None
            assert first.job_status == "failed", first
            assert first.failed_stage == "outline", first
            assert len(server.calls) == reader_expected, server.calls  # type: ignore[attr-defined]
            assert all(
                call.get("stage") == "stage1_reader"
                for call in server.calls  # type: ignore[attr-defined]
            ), server.calls  # type: ignore[attr-defined]
            blocked_inspection = control.inspect(workspace=first.workspace_path)
            preflight_record = next(
                (
                    row for row in blocked_inspection.get("artifacts") or ()
                    if isinstance(row, Mapping)
                    and row.get("artifact_type") == "outline_provider_call_plan"
                ),
                None,
            )
            assert preflight_record is not None, blocked_inspection
            preflight_payload = json.loads(
                Path(str(preflight_record["path"])).read_text(encoding="utf-8")
            )
            assert preflight_payload.get("preflight_status") == "rejected", preflight_payload
            assert preflight_payload.get("rejection_reason") == "max_provider_calls_exceeded", preflight_payload
            metrics = {
                "schema_version": "run-first-scale-quota-rejection/v1",
                "evidence_class": "synthetic_pdf_local_http_default_budget_rejection",
                "scale_size": size,
                "source_kind": source_kind,
                "source_variant": source_variant,
                "job_id": first.job_id,
                "job_status": first.job_status,
                "failed_stage": first.failed_stage,
                "expected_reader_posts": reader_expected,
                "actual_reader_posts": len(server.calls),  # type: ignore[attr-defined]
                "outline_posts": sum(
                    1 for call in server.calls  # type: ignore[attr-defined]
                    if str(call.get("stage") or "") == "outline"
                ),
                "configured_provider_call_limit": preflight_payload.get("max_provider_calls"),
                "estimated_provider_calls": preflight_payload.get("estimated_provider_calls"),
                "preflight_rejection_reason": preflight_payload.get("rejection_reason"),
                "provider_budget_source": "unchanged_documented_default",
                "zero_post_plan_budget_status": (
                    plan.get("full_stage_request_plan", {}).get("budget_status")
                ),
                "zero_post_plan_totals": (
                    plan.get("full_stage_request_plan", {}).get("totals")
                ),
                "provider_posts_after_outline_rejection": 0,
            }
            metrics_path = os.environ.get("RUN_FIRST_SCALE_METRICS_PATH", "").strip()
            _write_measurement(Path(metrics_path) if metrics_path else None, metrics)
            return

        assert provider_budget is not None, (
            "positive acceptance requires a finite test-only provider budget"
        )
        assert first.job_status == "completed", first
        assert first.job_disposition == "needs_review", first
        assert first.completed_stages == ("source_intake", "analyze", "outline"), first

        control_call_timings_seconds: dict[str, float] = {}

        def timed_control_call(name: str, operation: Callable[[], Any]) -> Any:
            started = time.perf_counter()
            try:
                return operation()
            finally:
                control_call_timings_seconds[name] = round(time.perf_counter() - started, 6)

        inspection = timed_control_call(
            "inspect_before_adoption_seconds",
            lambda: control.inspect(workspace=first.workspace_path),
        )
        final_outline = next(
            artifact for artifact in inspection["artifacts"]
            if artifact["artifact_id"] == "outline-v3:final_outline"
        )
        adoption_started = time.perf_counter()
        adoption = timed_control_call(
            "adopt_seconds",
            lambda: control.adopt(
                workspace=first.workspace_path,
                artifact_id="outline-v3:final_outline",
                actor="tests.run_first_scale_integration",
                reason="local acceptance explicitly adopts the source-closed Outline output",
                expected_hash=str(final_outline["content_hash"]),
            ),
        )
        assert adoption.get("status") == "succeeded", adoption
        adopted = timed_control_call(
            "first_resume_seconds",
            lambda: control.resume(workspace=first.workspace_path),
        )
        latest_execution_result = adopted
        assert adopted.get("job_status") == "completed", adopted
        assert adopted.get("completion_status") == (
            "blocked" if identity_quarantine_extra else "complete"
        ), adopted
        assert adopted.get("canonical_ready") is (not identity_quarantine_extra), adopted
        export = timed_control_call(
            "first_export_seconds",
            lambda: control.export(workspace=first.workspace_path),
        )
        if identity_quarantine_extra:
            assert export.get("status") == "untrusted", export
            assert export.get("bundle_path") == "" and export.get("artifact_id") == "", export
            assert "runtime_completion_not_canonical" in (export.get("issues") or ()), export
        else:
            assert export.get("status") == "canonical_verified", export
            assert Path(str(export.get("bundle_path") or "")).is_file(), export
        same_set_before = AgentRuntimeRunner._open_workspace(first.workspace_path)[1].resolve_current_artifact_set()
        assert same_set_before is not None

        calls_before_replay = len(server.calls)  # type: ignore[attr-defined]
        same_revision_resume = timed_control_call(
            "completed_same_revision_resume_seconds",
            lambda: control.resume(workspace=first.workspace_path),
        )
        latest_execution_result = same_revision_resume
        assert same_revision_resume.get("completion_status") == (
            "blocked" if identity_quarantine_extra else "complete"
        ), same_revision_resume
        same_set_after = AgentRuntimeRunner._open_workspace(first.workspace_path)[1].resolve_current_artifact_set()
        assert same_set_after is not None
        assert same_set_after.review_draft_artifact_id == same_set_before.review_draft_artifact_id
        assert same_set_after.review_draft_artifact_hash == same_set_before.review_draft_artifact_hash
        assert same_set_after.citation_manifest_artifact_id == same_set_before.citation_manifest_artifact_id
        assert same_set_after.citation_manifest_artifact_hash == same_set_before.citation_manifest_artifact_hash
        assert same_set_after.review_docx_artifact_id == same_set_before.review_docx_artifact_id
        assert same_set_after.review_docx_artifact_hash == same_set_before.review_docx_artifact_hash
        assert len(server.calls) == calls_before_replay, "same-revision resume replayed already closed provider work"  # type: ignore[attr-defined]
        export_after_resume = timed_control_call(
            "repeat_export_seconds",
            lambda: control.export(workspace=first.workspace_path),
        )
        if identity_quarantine_extra:
            assert export_after_resume.get("status") == "untrusted", export_after_resume
            assert export_after_resume.get("artifact_id") == ""
            assert export_after_resume.get("bundle_path") == ""
            assert "runtime_completion_not_canonical" in (
                export_after_resume.get("issues") or ()
            ), export_after_resume
        else:
            assert export_after_resume.get("artifact_id") == export.get("artifact_id")
            assert export_after_resume.get("bundle_path") == export.get("bundle_path")
        adoption_and_resume_seconds = time.perf_counter() - adoption_started
        inspection_after = timed_control_call(
            "inspect_after_exports_seconds",
            lambda: control.inspect(workspace=first.workspace_path),
        )
        _workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        quarantine_key = ""
        if identity_quarantine_extra:
            assert extra_quarantine_paper is not None
            source_record = registry.get("source_bundle")
            assert source_record is not None and source_record.status == "ready"
            source_bundle = json.loads(Path(source_record.path).read_text(encoding="utf-8"))
            snapshot = source_bundle.get("source_snapshot") or {}
            work_items = source_bundle.get("paper_work_items") or ()
            quarantined = snapshot.get("quarantined_sources") or ()
            extra_pdf = pdf_dir / f"{extra_quarantine_paper.stem}.pdf"
            assert snapshot.get("canonical_ready") is False
            assert len(tuple(pdf_dir.glob("*.pdf"))) == size + 1
            assert len(work_items) == size and len(quarantined) == 1
            work_keys = {str(item.get("canonical_paper_key") or "") for item in work_items
                         if isinstance(item, Mapping)}
            assert work_keys == {paper.paper_key for paper in papers}
            ready_work_keys = work_keys
            identity = quarantined[0]
            assert identity.get("identity_verdict") == "mismatch", identity
            assert "nonempty_doi_mismatch" in (identity.get("reasons") or ())
            assert (identity.get("expected") or {}).get("doi") == quarantine_expected_doi
            assert (identity.get("observed") or {}).get("doi") == "10.5555/rfs-actual-extra"
            extra_resolution = next(
                row for row in snapshot.get("pdf_resolutions") or ()
                if isinstance(row, Mapping)
                and Path(str(((row.get("identity") or {}).get("source_path") or ""))).resolve()
                == extra_pdf.resolve()
            )
            quarantine_key = str(extra_resolution.get("canonical_paper_key") or "")
            assert quarantine_key == quarantine_expected_doi and extra_pdf.is_file()
            ready_paths = {Path(record.path).resolve() for record in registry.list_records()
                           if record.artifact_type == "source_pdf" and record.status == "ready"}
            assert ready_paths == {(pdf_dir / f"{paper.stem}.pdf").resolve() for paper in papers}
            assert extra_pdf.resolve() not in ready_paths
        current_set = registry.resolve_current_artifact_set()
        assert current_set is not None and current_set.validation_status == "clean"
        outline_v3 = inspection_after.get("outline_v3") or {}
        draft_record = registry.get(current_set.review_draft_artifact_id)
        assert draft_record is not None and draft_record.status == "ready"
        assert draft_record.content_hash == current_set.review_draft_artifact_hash
        registry.verify_ready_artifact_closure(draft_record)
        draft_payload = json.loads(Path(draft_record.path).read_text(encoding="utf-8"))
        generation_context = draft_payload.get("generation_context")
        assert isinstance(generation_context, Mapping)
        outline_record = registry.get(str(generation_context.get("outline_artifact_id") or ""))
        assert outline_record is not None and outline_record.status == "ready"
        assert outline_record.content_hash == generation_context.get("outline_artifact_hash")
        assert any(
            dependency.artifact_id == outline_record.artifact_id
            and dependency.content_hash == outline_record.content_hash
            and dependency.path == outline_record.path
            for dependency in draft_record.depends_on
        ), "the current draft does not bind the inspected adopted Outline"
        registry.verify_ready_artifact_closure(outline_record)
        candidate_payload = json.loads(Path(outline_record.path).read_text(encoding="utf-8"))
        candidate_sections = candidate_payload.get("payload", {}).get("sections") or ()
        assert candidate_sections, "the adopted Outline contains no sections"
        adoption_record = registry.get(str(generation_context.get("adoption_artifact_id") or ""))
        assert adoption_record is not None and adoption_record.content_hash == generation_context.get("adoption_artifact_hash")
        assert any(ref.artifact_id == adoption_record.artifact_id and ref.content_hash == adoption_record.content_hash
                   and ref.path == adoption_record.path for ref in draft_record.depends_on)
        assert any(ref.artifact_id == outline_record.artifact_id and ref.content_hash == outline_record.content_hash
                   and ref.path == outline_record.path for ref in adoption_record.depends_on)
        for required_pair_keys in required_comparisons:
            assert any(
                set(required_pair_keys).issubset({str(value) for value in section.get("paper_keys") or () if str(value)})
                for section in candidate_sections if isinstance(section, Mapping)
            ), f"a user-required comparison did not survive into the adopted outline: {required_pair_keys}"
        candidate_support = [
            row for section in candidate_sections if isinstance(section, Mapping)
            for row in section.get("claim_support") or () if isinstance(row, Mapping)
        ]
        assert candidate_support
        assert all(
            row.get("source_claim_ids") and row.get("evidence_ids")
            and isinstance(row.get("source_field_ids"), list)
            for row in candidate_support
        )
        if source_variant in {"distant_cross_shard", "all_required_comparisons"}:
            rare_paper_key = papers[-1].paper_key
            rare_candidate_rows = [
                row for row in candidate_support
                if str(row.get("paper_key") or "") == rare_paper_key
                and active_rare_condition.casefold() in str(row.get("claim") or "").casefold()
            ]
            assert rare_candidate_rows, (
                "the single minority-condition source fact was not retained in adopted candidate support"
            )
        writer_trace_rows = [
            row for row in server.trace  # type: ignore[attr-defined]
            if isinstance(row, Mapping) and row.get("kind") == "writer_source_bound"
        ]
        writer_tasks = [
            task for row in writer_trace_rows for task in row.get("tasks") or ()
            if isinstance(task, Mapping)
        ]
        assert writer_tasks and all(
            task.get("source_claim_ids")
            and task.get("evidence_ids")
            and isinstance(task.get("source_field_ids"), list)
            and all(
                set(task["required_qualifier_ids"][field]).issubset(task[f"qualifier_{field}"])
                and set(task[f"qualifier_{field}"]).issubset(task[field])
                for field in ("source_claim_ids", "evidence_ids", "source_field_ids")
            )
            for task in writer_tasks
        ), "Writer task scopes did not retain complete primary and qualifier closures"

        scientific_support_oracle: dict[str, Any] = {
            "schema_version": "controlled-pdf-scientific-support-audit/v1",
            "status": "not_applicable",
            "evidence_class": "r1_original_source_engineering_evidence_only"
            if source_kind == "r1_typed_summary_reuse"
            else "not_requested",
            "issue_codes": [],
        }
        relation_request_observations: list[dict[str, Any]] = []
        if controlled_pdf_oracle is not None and required_pair:
            comparison_requirements = [
                dict(row) for row in review_intent.get("required_comparisons") or ()
                if isinstance(row, Mapping)
            ]
            candidate_audit = audit_all_selected_candidate_support(
                controlled_pdf_oracle,
                [dict(section) for section in candidate_sections if isinstance(section, Mapping)],
                required_comparisons=comparison_requirements,
                single_source_bindings=verify_single_source_candidate_bindings(registry, adoption_record),
            )
            citation_manifest_record = registry.get(current_set.citation_manifest_artifact_id)
            if (
                citation_manifest_record is None
                or citation_manifest_record.status != "ready"
                or citation_manifest_record.content_hash != current_set.citation_manifest_artifact_hash
            ):
                raise AssertionError("current citation manifest identity is unavailable for the support oracle")
            registry.verify_ready_artifact_closure(citation_manifest_record)
            manifest = json.loads(
                Path(citation_manifest_record.path).read_text(encoding="utf-8")
            )
            if not isinstance(manifest, Mapping):
                raise AssertionError("current canonical citation manifest has no payload")
            writer_audit = audit_all_canonical_writer_claim_units(
                controlled_pdf_oracle,
                manifest,
                required_comparisons=comparison_requirements,
            )
            if identity_quarantine_extra:
                observed_doi = str((identity.get("observed") or {}).get("doi") or "")
                observed_key = build_paper_key({
                    "title": extra_quarantine_paper.title, "doi": observed_doi,
                })
                assert quarantine_key == build_paper_key({
                    "title": extra_quarantine_paper.title, "doi": quarantine_expected_doi,
                })
                forbidden_keys = {quarantine_key, observed_key}
                required_members = {str(key) for pair in required_comparisons for key in pair}
                task_key_sets = [{str(key) for key in task.get("paper_keys") or () if str(key)} for task in writer_tasks]
                audit_rows = [row for row in writer_audit.get("claim_units") or () if isinstance(row, Mapping)]
                citation_occurrences = manifest.get("occurrences")
                citation_clusters = manifest.get("clusters")
                citation_sets = manifest.get("citation_sets")
                citation_paper_entries = manifest.get("paper_entries")
                citation_bibliography = manifest.get("bibliography")
                assert isinstance(citation_occurrences, list) and citation_occurrences
                assert isinstance(citation_clusters, list)
                assert isinstance(citation_sets, list) and citation_sets
                assert isinstance(citation_paper_entries, list)
                assert isinstance(citation_bibliography, list)
                assert all(isinstance(row, Mapping) for row in citation_occurrences)
                assert all(isinstance(row, Mapping) for row in citation_clusters)
                assert all(isinstance(group, Mapping) for group in citation_sets)
                assert all(isinstance(row, Mapping) for row in citation_paper_entries)
                assert all(isinstance(row, Mapping) for row in citation_bibliography)
                citation_manifest_keys: set[str] = set()
                citation_support_keys: set[str] = set()
                for occurrence in citation_occurrences:
                    paper_key = str(occurrence.get("paper_key") or "").strip()
                    canonical_key = str(occurrence.get("canonical_paper_key") or "").strip()
                    assert paper_key
                    citation_manifest_keys.add(paper_key)
                    if canonical_key:
                        citation_manifest_keys.add(canonical_key)
                for row in citation_clusters:
                    paper_key = str(row.get("paper_key") or "").strip()
                    assert paper_key
                    citation_manifest_keys.add(paper_key)
                for row in citation_paper_entries:
                    paper_key = str(row.get("paper_key") or "").strip()
                    assert paper_key
                    citation_manifest_keys.add(paper_key)
                for row in citation_bibliography:
                    paper_key = str(row.get("paper_key") or "").strip()
                    assert paper_key
                    citation_manifest_keys.add(paper_key)
                for group in citation_sets:
                    bundle_keys = group.get("paper_keys")
                    assert isinstance(bundle_keys, list) and bundle_keys
                    for key in bundle_keys:
                        paper_key = str(key or "").strip()
                        assert paper_key
                        citation_manifest_keys.add(paper_key)
                    units = group.get("claim_units")
                    assert isinstance(units, list)
                    for unit in units:
                        assert isinstance(unit, Mapping)
                        supporting_keys = unit.get("supporting_paper_keys")
                        assert isinstance(supporting_keys, list)
                        for key in supporting_keys:
                            paper_key = str(key or "").strip()
                            assert paper_key
                            citation_support_keys.add(paper_key)
                            citation_manifest_keys.add(paper_key)
                task_keys = set().union(*task_key_sets) if task_key_sets else set()
                audit_keys = {str(key) for row in audit_rows for key in row.get("paper_keys") or () if str(key)}
                assert task_key_sets and all(keys and keys <= ready_work_keys for keys in task_key_sets)
                assert audit_keys and citation_support_keys and citation_manifest_keys
                assert citation_manifest_keys <= ready_work_keys
                assert not citation_manifest_keys.intersection(forbidden_keys)
                assert all(
                    keys <= ready_work_keys and required_members <= keys and not keys.intersection(forbidden_keys)
                    for keys in (task_keys, audit_keys, citation_support_keys)
                )
            if source_variant in {"distant_cross_shard", "all_required_comparisons"}:
                rare_paper_key = papers[-1].paper_key
                units_by_id = {
                    str(unit.get("claim_unit_id") or ""): dict(unit)
                    for bundle in manifest.get("citation_sets") or ()
                    if isinstance(bundle, Mapping)
                    for unit in bundle.get("claim_units") or ()
                    if isinstance(unit, Mapping) and str(unit.get("claim_unit_id") or "")
                }
                rare_writer_units = [
                    units_by_id[str(row.get("claim_unit_id") or "")]
                    for row in writer_audit.get("claim_units") or ()
                    if isinstance(row, Mapping)
                    and rare_paper_key in (row.get("paper_keys") or ())
                    and str(row.get("claim_unit_id") or "") in units_by_id
                ]
                assert any(
                    active_rare_condition.casefold()
                    in str(unit.get("claim_text") or "").casefold()
                    for unit in rare_writer_units
                ), "the single minority-condition source fact was not retained in canonical Writer claim units"
            scientific_support_oracle = {
                "schema_version": "controlled-pdf-scientific-support-audit/v1",
                "status": (
                    "supported"
                    if (
                        candidate_audit["status"] == writer_audit["status"] == "supported"
                        and candidate_audit["required_comparisons_status"] == "supported"
                        and writer_audit["required_comparisons_status"] == "supported"
                    )
                    else "unsupported"
                ),
                "evidence_class": "synthetic_pdf_parsed_source_facts_and_current_citation_clusters",
                "required_paper_keys": required_pair,
                "required_comparisons": comparison_requirements,
                "source_pdf_sha256": {
                    key: source.pdf_sha256 for key, source in controlled_pdf_oracle.documents.items()
                },
                "candidate": candidate_audit,
                "writer_and_canonical_citation_clusters": writer_audit,
                "source_parser_unjudgable_count": sum(
                    bool(source.parse_diagnostics)
                    for source in controlled_pdf_oracle.documents.values()
                ),
                "unjudgable_assertion_count": (
                    int(candidate_audit.get("assertion_counts", {}).get("unjudgable") or 0)
                    + int(writer_audit.get("claim_unit_counts", {}).get("unjudgable") or 0)
                ),
            }
            if scientific_support_oracle["status"] != "supported":
                raise AssertionError(
                    "controlled-PDF source-support audit failed: "
                    + json.dumps(scientific_support_oracle, ensure_ascii=False, sort_keys=True)
                )
            relation_request_observations = _assert_required_comparisons_have_complete_executed_bundles(
                registry,
                required_comparisons,
                [dict(row) for row in server.trace if isinstance(row, Mapping)],  # type: ignore[attr-defined]
                controlled_pdf_oracle,
                distant_pair=distant_pair,
            )

        sampler.stop()
        peak_traced_bytes = None
        if tracemalloc_running:
            _current_bytes, peak_traced_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            tracemalloc_running = False
        _assert_local_only(server)
        calls = list(server.calls)  # type: ignore[attr-defined]
        by_stage = Counter(str(call.get("stage") or "unknown") for call in calls)
        model_counts = Counter(str(call.get("model") or "") for call in calls)
        writer_receipts = [
            row for row in (inspection_after.get("provider_receipts") or {}).get("entries") or ()
            if isinstance(row, Mapping)
            and row.get("stage_name") == "stage3_review"
            and row.get("route") == "Writer_API"
        ]
        # The same model is intentionally used by Outline peer critiques.
        # Compare Writer receipts to its actual stage, not every model POST.
        assert sum(int(row.get("attempts") or 0) for row in writer_receipts) == by_stage["stage3_writer"]
        assert model_counts["rfs-reader-local"] == reader_expected, {
            "reader_posts": model_counts["rfs-reader-local"], "expected_reader_posts": reader_expected,
        }
        if candidate_layout_variant_enabled:
            assert len(reader_design_transport_capture) == model_counts["rfs-reader-local"], {
                "reader_transport_captures": len(reader_design_transport_capture),
                "reader_posts": model_counts["rfs-reader-local"],
            }
            assert {
                int(row["paper_index"]) for row in reader_design_transport_capture
            }.issuperset({paper.index for paper in papers})
            assert all(
                row["design_statement_present"]
                and len(str(row["request_body_sha256"])) == 64
                and len(str(row["user_text_sha256"])) == 64
                for row in reader_design_transport_capture
            )
            assert any(int(row["paper_index"]) == 0 for row in reader_design_transport_capture)
            assert all(
                row["article_bridge_present"]
                and row["estimates_not_pooled_present"]
                and row["no_shared_mediator_present"]
                for row in reader_design_transport_capture
                if int(row["paper_index"]) == 0
            )
            assert fixture_transport_origin is not None
            assert fixture_transport_targets
            assert set(fixture_transport_targets) == {fixture_transport_origin}
            assert socket_targets
            assert set(socket_targets) == {allowed_socket_target}
        assert by_stage["stage4_validator"] > 0
        unique_hash_count = len({str(call.get("user_text_sha256") or "") for call in calls})
        total_user_text_bytes = sum(int(call.get("user_text_bytes") or 0) for call in calls)
        if candidate_layout_variant_enabled:
            rows_by_candidate: dict[str, list[dict[str, Any]]] = {}
            for row in candidate_layout_capture:
                rows_by_candidate.setdefault(str(row.get("candidate_id") or ""), []).append(row)
                assert row["base_membership_sha256"] == row["variant_membership_sha256"]
            expected_candidate_ids = {f"candidate_{index}" for index in range(1, 6)}
            assert expected_candidate_ids.issubset(rows_by_candidate), sorted(rows_by_candidate)
            assert all(
                row["base_global_layout"] == row["variant_global_layout"]
                for row in rows_by_candidate["candidate_1"]
            )

            def _organization_candidate_from_node(node_id: str) -> str:
                prefix = "candidate_organization_provider:"
                if not node_id.startswith(prefix):
                    return ""
                candidate_id = node_id[len(prefix):].split(":", 1)[0]
                return candidate_id if candidate_id in expected_candidate_ids else ""

            organization_maps: dict[str, list[dict[str, Any]]] = {}
            organization_maps_by_node: dict[str, dict[str, Any]] = {}
            for record in registry.list_records():
                if record.artifact_type != "candidate_organization_map_result" or record.status != "ready":
                    continue
                raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
                payload = raw.get("payload") if isinstance(raw, Mapping) else None
                payload = payload if isinstance(payload, Mapping) else raw
                request = raw.get("provider_request") if isinstance(raw, Mapping) else None
                node_id = str((record.metadata or {}).get("node_id") or "")
                candidate_id = str(payload.get("candidate_id") or "") if isinstance(payload, Mapping) else ""
                if isinstance(payload, Mapping) and isinstance(request, Mapping) and node_id:
                    item = {
                        "candidate_id": candidate_id,
                        "node_id": node_id,
                        "request": dict(request),
                        "payload": dict(payload),
                        "global_layout": _scale_global_organization_map_signature(request, payload),
                    }
                    organization_maps.setdefault(candidate_id, []).append(item)
                    if node_id in organization_maps_by_node:
                        raise AssertionError(f"Registry has duplicate ready organization maps for provider node {node_id}")
                    organization_maps_by_node[node_id] = item
            assert organization_maps.get("candidate_1") and organization_maps.get("candidate_2")
            candidate_one_map_sections = sum(
                len(row["payload"].get("sections") or ()) for row in organization_maps["candidate_1"]
            )
            candidate_two_map_sections = sum(
                len(row["payload"].get("sections") or ()) for row in organization_maps["candidate_2"]
            )
            actual_outline_nodes = {
                str(call.get("node_id") or "")
                for call in calls if str(call.get("stage") or "") == "outline"
            }
            for candidate_id in sorted(expected_candidate_ids):
                actual_nodes = {
                    node for node in actual_outline_nodes
                    if _organization_candidate_from_node(node) == candidate_id
                }
                captured_nodes = {
                    str(row.get("provider_call_id") or "") for row in rows_by_candidate[candidate_id]
                }
                artifact_nodes = {
                    str(row.get("node_id") or "") for row in organization_maps.get(candidate_id) or ()
                }
                if not actual_nodes or actual_nodes != captured_nodes or actual_nodes != artifact_nodes:
                    raise AssertionError(
                        f"{candidate_id} organization responses are not bound across fixture, HTTP, and ready Registry: "
                        f"actual={sorted(actual_nodes)} captured={sorted(captured_nodes)} ready={sorted(artifact_nodes)}"
                    )
                captured_by_node = {
                    str(row["provider_call_id"]): row for row in rows_by_candidate[candidate_id]
                }
                for ready in organization_maps.get(candidate_id) or ():
                    captured = captured_by_node[str(ready["node_id"])]
                    if captured["variant_global_layout"] != ready["global_layout"]:
                        raise AssertionError(
                            f"{candidate_id} provider grouping differs from its ready organization-map artifact: "
                            f"node={ready['node_id']}"
                        )

            candidate_one_base_layout = _scale_aggregate_partition_layouts(
                [row["base_global_layout"] for row in rows_by_candidate["candidate_1"]],
            )
            candidate_one_ready_layout = _scale_aggregate_partition_layouts(
                [row["global_layout"] for row in organization_maps["candidate_1"]],
            )
            candidate_two_base_layout = _scale_aggregate_partition_layouts(
                [row["base_global_layout"] for row in rows_by_candidate["candidate_2"]],
            )
            candidate_two_variant_layout = _scale_aggregate_partition_layouts(
                [row["variant_global_layout"] for row in rows_by_candidate["candidate_2"]],
            )
            candidate_two_ready_layout = _scale_aggregate_partition_layouts(
                [row["global_layout"] for row in organization_maps["candidate_2"]],
            )
            assert candidate_one_base_layout == candidate_one_ready_layout
            assert candidate_two_variant_layout == candidate_two_ready_layout
            assert candidate_two_variant_layout != candidate_two_base_layout, (
                "candidate_2 organization fixture did not produce a different global fragment grouping"
            )
            assert candidate_two_variant_layout != candidate_one_base_layout, (
                "candidate_1 and candidate_2 ready organization maps did not preserve distinct global layouts"
            )

            def _candidate_generation_payload(candidate_id: str) -> dict[str, Any]:
                record = registry.get(f"outline-v3:{candidate_id}_provider_generation")
                if record is None or record.status != "ready":
                    raise AssertionError(f"{candidate_id} provider-generation artifact is not ready")
                raw = json.loads(Path(record.path).read_text(encoding="utf-8"))
                payload = raw.get("payload") if isinstance(raw, Mapping) else None
                payload = payload if isinstance(payload, Mapping) else raw
                if not isinstance(payload, Mapping):
                    raise AssertionError(f"{candidate_id} provider-generation payload is not an object")
                return dict(payload)

            generation_one = _candidate_generation_payload("candidate_1")
            generation_two = _candidate_generation_payload("candidate_2")
            support_one = _scale_candidate_support_multiset_signature(generation_one)
            support_two = _scale_candidate_support_multiset_signature(generation_two)
            assert support_one == support_two, "candidate_1 and candidate_2 changed source/evidence/qualifier membership"
            sections_one = list(generation_one.get("sections") or ())
            sections_two = list(generation_two.get("sections") or ())
            generation_layout_one = _scale_candidate_generation_layout_signature(generation_one)
            generation_layout_two = _scale_candidate_generation_layout_signature(generation_two)
            assert generation_layout_one != generation_layout_two, (
                "candidate_1 and candidate_2 provider-generation artifacts have the same support-to-section layout"
            )

            scope_record = registry.get("outline-v3:candidate_output_scope")
            assert scope_record is not None and scope_record.status == "ready"
            raw_scope = json.loads(Path(scope_record.path).read_text(encoding="utf-8"))
            scope_payload = raw_scope.get("payload") if isinstance(raw_scope, Mapping) else None
            scope_payload = scope_payload if isinstance(scope_payload, Mapping) else raw_scope
            assert isinstance(scope_payload, Mapping)
            bank_record = registry.get("outline-v3:candidate_fragment_bank")
            assert bank_record is not None and bank_record.status == "ready"
            raw_bank = json.loads(Path(bank_record.path).read_text(encoding="utf-8"))
            bank_payload = raw_bank.get("payload") if isinstance(raw_bank, Mapping) else None
            bank_payload = bank_payload if isinstance(bank_payload, Mapping) else raw_bank
            assert isinstance(bank_payload, Mapping)
            required_groups_by_pair = _scale_required_comparison_groups_by_pair(
                [row["request"] for row in organization_maps["candidate_2"]],
                bank_payload,
                required_comparisons,
            )
            assert set(required_groups_by_pair) == {
                tuple(sorted(str(value) for value in pair)) for pair in required_comparisons
            }
            _scale_assert_required_comparison_closures(
                required_groups_by_pair, scope_payload, sections_two,
            )

            outline_record = registry.get("outline-v3:final_outline")
            assert outline_record is not None and outline_record.status == "ready"
            raw_outline = json.loads(Path(outline_record.path).read_text(encoding="utf-8"))
            selected_outline = raw_outline.get("payload") if isinstance(raw_outline, Mapping) else None
            selected_outline = selected_outline if isinstance(selected_outline, Mapping) else raw_outline
            assert isinstance(selected_outline, Mapping)
            assert str(selected_outline.get("candidate_id") or "") == "candidate_1"
            assert len(selected_outline.get("sections") or ()) >= 2
            ready_drafts = [
                record for record in registry.list_records()
                if record.artifact_type == "review_draft" and record.status == "ready"
            ]
            writer_outline_bound = any(
                dependency.artifact_id == outline_record.artifact_id
                and dependency.content_hash == outline_record.content_hash
                for record in ready_drafts for dependency in record.depends_on
            )
            assert writer_outline_bound, "Writer draft did not bind the selected multi-section outline"
            arbitration_http_nodes = {
                node for node in actual_outline_nodes
                if node == "arbitration" or node.startswith("arbitration:")
            }
            assert arbitration_http_nodes, "no direct or hierarchical arbitration HTTP request was executed"
            assert any(
                str(view.get("node_id") or "") in arbitration_http_nodes
                for view in candidate_layout_arbitration_capture
            ), "no captured model-visible arbitration index is bound to an actual HTTP node"

            expected_candidate_two_ids = {
                str(section.get("section_id") or "")
                for section in sections_two if isinstance(section, Mapping)
            }
            complete_arbitration_views = []
            for view in candidate_layout_arbitration_capture:
                rows = [row for row in view.get("candidate_2_section_rows") or () if isinstance(row, Mapping)]
                if {str(row.get("section_id") or "") for row in rows} != expected_candidate_two_ids:
                    continue
                if not _scale_model_visible_rows_match_candidate(rows, generation_two):
                    raise AssertionError("model-visible C2 section index differs from its ready generation artifact")
                c1_rows = [row for row in view.get("candidate_1_section_rows") or () if isinstance(row, Mapping)]
                if c1_rows and not _scale_model_visible_rows_match_candidate(c1_rows, generation_one):
                    raise AssertionError("model-visible C1 section index differs from its ready generation artifact")
                http_rows = [
                    call for call in calls
                    if str(call.get("stage") or "") == "outline"
                    and str(call.get("node_id") or "") == str(view.get("node_id") or "")
                ]
                if len(http_rows) != 1:
                    raise AssertionError(
                        "model-visible arbitration layout does not bind to exactly one HTTP request body: "
                        f"node={view.get('node_id')} count={len(http_rows)}"
                    )
                http_call = http_rows[0]
                request_hash = str(http_call.get("request_sha256") or "")
                user_hash = str(http_call.get("user_text_sha256") or "")
                if len(request_hash) != 64 or len(user_hash) != 64:
                    raise AssertionError("arbitration model-visible layout is missing actual HTTP body hashes")
                complete_arbitration_views.append({
                    **dict(view),
                    "http_request_body_sha256": request_hash,
                    "http_user_text_sha256": user_hash,
                    "candidate_2_index_matches_ready_generation": True,
                    "candidate_1_index_matches_ready_generation": bool(c1_rows),
                })
            if not complete_arbitration_views:
                # Hierarchical leaf requests can expose C2 section-index subsets.
                # Their union must cover the complete ready candidate before Writer.
                leaf_rows_by_id: dict[str, dict[str, Any]] = {}
                leaf_calls = []
                for view in candidate_layout_arbitration_capture:
                    if view.get("capture_kind") != "hierarchical_leaf":
                        continue
                    for row in view.get("candidate_2_section_rows") or ():
                        if not isinstance(row, Mapping):
                            continue
                        section_id = str(row.get("section_id") or "")
                        previous = leaf_rows_by_id.get(section_id)
                        if previous is not None and previous.get("section_hash") != row.get("section_hash"):
                            raise AssertionError(f"arbitration leaf section hash changed for {section_id}")
                        leaf_rows_by_id[section_id] = dict(row)
                    leaf_calls.extend(
                        call for call in calls
                        if str(call.get("stage") or "") == "outline"
                        and str(call.get("node_id") or "") == str(view.get("node_id") or "")
                    )
                if set(leaf_rows_by_id) != expected_candidate_two_ids:
                    raise AssertionError(
                        "model-visible arbitration has neither a complete C2 index nor complete leaf union"
                    )
                leaf_rows = sorted(
                    leaf_rows_by_id.values(), key=lambda row: int(row.get("position") or 0),
                )
                if not _scale_model_visible_rows_match_candidate(leaf_rows, generation_two):
                    raise AssertionError("model-visible C2 leaf union differs from the ready generation artifact")
                if not leaf_calls or any(len(str(call.get("request_sha256") or "")) != 64 for call in leaf_calls):
                    raise AssertionError("arbitration leaf layout is missing actual HTTP body hashes")
                complete_arbitration_views.append({
                    "capture_kind": "hierarchical_leaf_union",
                    "node_ids": sorted({str(call.get("node_id") or "") for call in leaf_calls}),
                    "candidate_2_section_rows": leaf_rows,
                    "candidate_2_index_matches_ready_generation": True,
                    "http_request_body_sha256": sorted(str(call.get("request_sha256") or "") for call in leaf_calls),
                    "http_user_text_sha256": sorted(str(call.get("user_text_sha256") or "") for call in leaf_calls),
                })
            model_visible_layout = complete_arbitration_views[-1]
            candidate_layout_metrics = {
                "status": "executed_fixture_layout_variant",
                "boundary": "synthetic local HTTP engineering evidence; not semantic or real-model quality",
                "candidate_1_organizing_logic": rows_by_candidate["candidate_1"][0]["organizing_logic"],
                "candidate_2_organizing_logic": rows_by_candidate["candidate_2"][0]["organizing_logic"],
                "synthetic_research_design_profile": candidate_design_profile,
                "reader_source_text_transport": {
                    "status": "verified_from_actual_reader_http_user_messages",
                    "reader_request_count": len(reader_design_transport_capture),
                    "design_and_bridge_checks": sorted(
                        reader_design_transport_capture,
                        key=lambda row: (int(row["paper_index"]), str(row["request_body_sha256"])),
                    ),
                    "all_session_targets_matched_local_origin": True,
                    "fixture_origin": {
                        "scheme": fixture_transport_origin[0],
                        "host": fixture_transport_origin[1],
                        "port": fixture_transport_origin[2],
                    },
                    "session_send_count": sum(fixture_transport_targets.values()),
                    "socket_connect_count": sum(socket_targets.values()),
                    "socket_target": {
                        "host": allowed_socket_target[0],
                        "port": allowed_socket_target[1],
                    },
                    "effective_proxy_targets": 0,
                },
                "candidate_1_organization_sections": candidate_one_map_sections,
                "candidate_2_organization_sections": candidate_two_map_sections,
                "candidate_1_generation_sections": len(sections_one),
                "candidate_2_generation_sections": len(sections_two),
                "candidate_1_global_organization_layout_sha256": hashlib.sha256(
                    json.dumps(candidate_one_ready_layout, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "candidate_2_global_organization_layout_sha256": hashlib.sha256(
                    json.dumps(candidate_two_ready_layout, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "candidate_1_generation_layout_sha256": hashlib.sha256(
                    json.dumps(generation_layout_one, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "candidate_2_generation_layout_sha256": hashlib.sha256(
                    json.dumps(generation_layout_two, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "candidate_organization_provider_node_ids": {
                    candidate_id: sorted(row["node_id"] for row in organization_maps[candidate_id])
                    for candidate_id in ("candidate_1", "candidate_2")
                },
                "candidate_1_base_layout_hashes": sorted(
                    hashlib.sha256(json.dumps(row["base_layout"], sort_keys=True).encode("utf-8")).hexdigest()
                    for row in rows_by_candidate["candidate_1"]
                ),
                "candidate_2_variant_layout_hashes": sorted(
                    hashlib.sha256(json.dumps(row["variant_layout"], sort_keys=True).encode("utf-8")).hexdigest()
                    for row in rows_by_candidate["candidate_2"]
                ),
                "same_candidate_support_multiset": True,
                "candidate_support_rows": len(support_one),
                "candidate_support_multiset_sha256": hashlib.sha256(
                    json.dumps(support_one, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "required_ab_comparisons_kept_complete": len(required_comparisons),
                "selected_candidate": str(selected_outline.get("candidate_id") or ""),
                "selected_outline_sections": len(selected_outline.get("sections") or ()),
                "writer_outline_bound": writer_outline_bound,
                "provider_candidate_layout_calls": sorted(
                    node for node in actual_outline_nodes
                    if node.startswith("candidate_organization_provider:")
                ),
                "arbitration_http_call_seen": bool(arbitration_http_nodes),
                "model_visible_arbitration": {
                    "capture_kind": model_visible_layout.get("capture_kind"),
                    "node_id": model_visible_layout.get("node_id"),
                    "node_ids": model_visible_layout.get("node_ids"),
                    "candidate_2_index_matches_ready_generation": model_visible_layout.get("candidate_2_index_matches_ready_generation") is True,
                    "hierarchy_level": model_visible_layout.get("hierarchy_level"),
                    "request_schema_version": model_visible_layout.get("request_schema_version"),
                    "candidate_ids": model_visible_layout.get("candidate_ids"),
                    "projection_aliases": model_visible_layout.get("projection_aliases"),
                    "ledger_packing_schema": model_visible_layout.get("ledger_packing_schema"),
                    "raw_candidate_2_positional_index_row_count": model_visible_layout.get("raw_candidate_2_positional_index_row_count"),
                    "parsed_request_sha256": model_visible_layout.get("parsed_request_sha256"),
                    "capture_errors": list(candidate_layout_arbitration_capture_errors),
                    "candidate_2_section_rows": model_visible_layout.get("candidate_2_section_rows") or [],
                    "http_request_body_sha256": model_visible_layout.get("http_request_body_sha256"),
                    "http_user_text_sha256": model_visible_layout.get("http_user_text_sha256"),
                },
            }
        metrics = {
            "schema_version": "run-first-scale-local-http-measurement/v1",
            "evidence_class": (
                "r1_typed_summary_reuse_original63_local_engineering_not_scientific"
                if source_kind == "r1_typed_summary_reuse"
                else (
                    "synthetic_pdf_identity_quarantine_partial_execution_needs_review"
                    if identity_quarantine_extra
                    else "synthetic_pdf_local_http_production_runtime"
                )
            ),
            "scale_size": size,
            "source_kind": source_kind,
            "source_variant": source_variant,
            "canonical_engineering_chain_completed": not identity_quarantine_extra,
            "real_provider_or_scientific_acceptance": False,
            "identity_quarantine_counts": (
                {"input_pdf": size + 1, "ready": size, "quarantined": 1}
                if identity_quarantine_extra else None
            ),
            "warm_seed": warm_seed_metrics,
            "provider_budget_source": (
                "existing_4paper_loopback_fixture_allowance"
                if provider_budget == ProviderAggregateBudgetV2.from_mapping(
                    _FOUR_PAPER_LOOPBACK_BUDGET
                ).to_dict()
                else "explicit_RUN_FIRST_SCALE_PROVIDER_BUDGET"
            ),
            "provider_budget": dict(provider_budget),
            "local_test_admission_overrides": dict(admission_overrides),
            "effective_config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "config_example_sha256": hashlib.sha256((repo_root / "config.ini.example").read_bytes()).hexdigest(),
            "zero_post_plan_budget_status": (
                plan.get("full_stage_request_plan", {}).get("budget_status")
            ),
            "zero_post_plan_totals": (
                plan.get("full_stage_request_plan", {}).get("totals")
            ),
            "zero_post_plan_projection_identity_hash": (
                plan.get("full_stage_request_plan", {}).get("projection_identity_hash")
            ),
            "source_authority": source_receipt,
            "job_id": first.job_id,
            "workspace_path": first.workspace_path,
            "plan_read_only": plan.get("read_only") is True,
            "plan_no_provider_posts": plan.get("full_stage_request_plan", {}).get("boundary", {}).get("no_provider_posts") is True,
            "plan_seconds": round(plan_seconds, 3),
            "runner_seconds": round(runner_seconds, 3),
            "runner_timing_scope": "seed_plus_warm_verification_plan_and_runner" if warm_seed_metrics else "runner",
            "adoption_resume_export_and_replay_seconds": round(adoption_and_resume_seconds, 3),
            "control_call_timings_seconds": dict(control_call_timings_seconds),
            "control_call_timing_scope": (
                "Each wall interval measures one control method. adopt/export include their internal inspect; "
                "first resume includes review and validation; completed same-revision resume must add zero provider posts. "
                "The pre/post inspect intervals are reported separately from the existing composite timer."
            ),
            "completed_stages": list(same_revision_resume.get("completed_stages") or ()),
            "initial_outline_run_completed_stages": list(first.completed_stages),
            "completion_status": same_revision_resume.get("completion_status"),
            "validation_status": current_set.validation_status,
            "export_status": export_after_resume.get("status"),
            "same_revision_resume_post_delta": len(server.calls) - calls_before_replay,  # type: ignore[attr-defined]
            "local_http_physical_post_count": len(calls),
            "local_http_model_post_counts": dict(sorted(model_counts.items())),
            "local_http_stage_post_counts": dict(sorted(by_stage.items())),
            "local_http_handler_timing": _local_http_handler_timing_metrics(server),
            "local_http_max_request_bytes": max(int(call.get("request_bytes") or 0) for call in calls),
            "local_http_max_user_text_bytes": max(int(call.get("user_text_bytes") or 0) for call in calls),
            "local_http_total_user_text_bytes": total_user_text_bytes,
            "exact_duplicate_user_payload_count": len(calls) - unique_hash_count,
            "simulated_fixture_input_tokens": sum(int(call.get("fixture_input_tokens") or 0) for call in calls),
            "simulated_fixture_output_tokens": sum(int(call.get("fixture_output_tokens") or 0) for call in calls),
            "fixture_outputs_within_requested_caps": all(
                int(call.get("fixture_output_tokens") or 0)
                <= int(call.get("requested_output_tokens") or 0)
                for call in calls
            ),
            "provider_usage_evidence": "simulated local fixture usage, not billed provider usage",
            "python_tracemalloc_peak_bytes": peak_traced_bytes,
            "process_peak_rss_bytes_sampled": sampler.peak_rss if sampler._process is not None else None,
            "process_rss_sample_interval_ms": 100 if sampler._process is not None else None,
            "writer_receipt_attempts": sum(int(row.get("attempts") or 0) for row in writer_receipts),
            "reader_calls_equal_sources": model_counts["rfs-reader-local"] == reader_expected,
            "outline_v3_available": bool(outline_v3.get("available")),
            "required_comparison_papers": required_pair,
            "required_comparisons": required_comparisons,
            "relation_request_observations": relation_request_observations,
            "scientific_support_oracle": scientific_support_oracle,
            "candidate_section_count": len(candidate_sections),
            "candidate_layout_variant": candidate_layout_metrics,
            "candidate_support_slot_count": len(candidate_support),
            "writer_task_count": len(writer_tasks),
            "writer_qualifier_closed_task_count": sum(bool(task.get("qualifier_source_field_ids")) for task in writer_tasks),
            "original_pdf_sha256_verified_count": source_receipt.get("pdf_sha256_verified_count", 0),
            "typed_summary_manifest_sha256_verified_count": source_receipt.get("typed_manifest_sha256_verified_count", 0),
            "actual_serializer_evidence": "SHA-256 and byte length captured from each loopback HTTP request body",
        }
        metrics_path = os.environ.get("RUN_FIRST_SCALE_METRICS_PATH", "").strip()
        _write_measurement(Path(metrics_path) if metrics_path else None, metrics)
    except Exception as exc:  # noqa: BLE001 - capture any failure before pytest cleanup
        def capture_failure(original_error: Exception = exc) -> None:
            diagnostic_path = _scale_failure_diagnostic_path(
                tmp_path,
                size=size,
                source_kind=source_kind,
                source_variant=source_variant,
            )
            safe_section_fields = (
                "position", "section_id", "section_hash", "source_closure_hash",
                "paper_keys", "task_ids", "relation_ids",
            )
            failure_layout_capture = [
                {
                    key: row.get(key)
                    for key in (
                        "candidate_id", "provider_call_id", "partition_index", "partition_count",
                        "full_bank_group_count", "partition_local_to_global_group_indices",
                        "required_group_indices", "required_comparison_sections",
                        "base_global_layout", "variant_global_layout",
                        "base_membership_sha256", "variant_membership_sha256",
                    )
                }
                for row in candidate_layout_capture if isinstance(row, Mapping)
            ]
            failure_arbitration_capture = [
                {
                    key: row.get(key)
                    for key in (
                        "node_id", "capture_kind", "leaf_id", "hierarchy_level", "candidate_ids",
                        "request_schema_version", "projection_aliases", "ledger_packing_schema",
                        "raw_candidate_2_positional_index_row_count", "parsed_request_sha256",
                        "candidate_2_binding_section_ids",
                    )
                }
                | {
                    candidate_key: [
                        {key: section.get(key) for key in safe_section_fields if key in section}
                        for section in row.get(candidate_key) or () if isinstance(section, Mapping)
                    ]
                    for candidate_key in ("candidate_1_section_rows", "candidate_2_section_rows")
                }
                for row in candidate_layout_arbitration_capture if isinstance(row, Mapping)
            ]
            failure_reader_capture = [
                {
                    key: row.get(key)
                    for key in (
                        "paper_index", "paper_key", "design_type", "design_statement_present",
                        "article_bridge_present", "estimates_not_pooled_present",
                        "no_shared_mediator_present", "request_body_sha256", "user_text_sha256",
                    )
                }
                for row in reader_design_transport_capture if isinstance(row, Mapping)
            ]
            diagnostic = _scale_failure_snapshot(
                control=control,
                job_id=job_id,
                first=first,
                plan=plan,
                plan_seconds=plan_seconds,
                server=server,
                source_kind=source_kind,
                source_variant=source_variant,
                source_receipt=source_receipt,
                size=size,
                exception=original_error,
                latest_execution_result=latest_execution_result,
            )
            _scale_add_failure_loopback_extensions(
                diagnostic,
                {
                    "candidate_organization_layout_captures": failure_layout_capture,
                    "candidate_arbitration_model_visible_captures": failure_arbitration_capture,
                    "candidate_arbitration_capture_errors": [
                        {
                            "node_id": _diagnostic_text(row.get("node_id")),
                            "error_type": _diagnostic_text(row.get("error_type")),
                            "message_sha256": hashlib.sha256(
                                str(row.get("message") or "").encode("utf-8")
                            ).hexdigest(),
                        }
                        for row in candidate_layout_arbitration_capture_errors if isinstance(row, Mapping)
                    ],
                    "reader_design_transport_captures": failure_reader_capture,
                },
            )
            _persist_scale_failure_snapshot(diagnostic_path, diagnostic)
        _capture_scale_failure_or_reraise(exc, capture_failure)
    finally:
        if sampler is not None:
            sampler.stop()
        if tracemalloc_running:
            tracemalloc.stop()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

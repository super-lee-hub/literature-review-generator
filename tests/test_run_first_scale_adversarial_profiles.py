"""Offline source-fact checks for explicit adversarial scale variants.

The production full-chain acceptance remains
``test_run_first_scale_local_http_full_chain``. Run it in a separate process
with ``RUN_FIRST_SCALE_VARIANT`` set to one explicit profile and the usual
finite test-only V2 budget. This file never starts a provider fixture.
"""
from __future__ import annotations

import importlib.util
from copy import deepcopy
import os
import sys
from pathlib import Path

import fitz
import pytest

_REPO_ROOT = Path(
    os.environ.get("RUN_FIRST_SCALE_REPO_ROOT", str(Path(__file__).resolve().parents[1]))
).expanduser().resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.scientific_support_oracle import ControlledPdfCorpus

_INTEGRATION_FIXTURE_PATH = Path(__file__).with_name("test_run_first_scale_integration.py")
_INTEGRATION_SPEC = importlib.util.spec_from_file_location(
    "run_first_scale_integration_adversarial_draft",
    _INTEGRATION_FIXTURE_PATH,
)
if _INTEGRATION_SPEC is None or _INTEGRATION_SPEC.loader is None:
    raise ImportError(f"scale fixture draft is unavailable: {_INTEGRATION_FIXTURE_PATH}")
_INTEGRATION_MODULE = importlib.util.module_from_spec(_INTEGRATION_SPEC)
sys.modules[_INTEGRATION_SPEC.name] = _INTEGRATION_MODULE
_INTEGRATION_SPEC.loader.exec_module(_INTEGRATION_MODULE)
_RARE_SCALE_CONDITION = _INTEGRATION_MODULE._RARE_SCALE_CONDITION
_scale_papers = _INTEGRATION_MODULE._scale_papers
_scale_required_comparisons = _INTEGRATION_MODULE._scale_required_comparisons
_write_scale_pdf = _INTEGRATION_MODULE._write_scale_pdf


_VARIANT_SIZES = (10, 64, 100, 200, 500, 600)
_ADVERSARIAL_VARIANTS = ("required_null", "required_reverse", "distant_cross_shard", "all_required_comparisons")


def test_baseline_profile_preserves_historical_pair_and_pdf_rows() -> None:
    historical = _scale_papers(10)
    explicit_baseline = _scale_papers(10, source_variant="baseline")
    assert explicit_baseline == historical
    assert _scale_required_comparisons(explicit_baseline, "baseline") == [[
        historical[0].paper_key,
        historical[1].paper_key,
    ]]


@pytest.mark.parametrize("condition_case", ("source_sentence", "changed_label", "other_source_only"))
def test_executed_relation_condition_uses_its_own_pdf_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition_case: str,
) -> None:
    papers = _scale_papers(10)[:2]
    pdfs = {}
    for paper in papers:
        pdfs[paper.paper_key] = tmp_path / f"{paper.stem}.pdf"
        _write_scale_pdf(pdfs[paper.paper_key], paper)
    corpus = ControlledPdfCorpus.from_pdfs(pdfs)
    keys = [paper.paper_key for paper in papers]
    relation = {"relation_id": "required-ab", "paper_keys": keys}
    contexts = {}
    for paper in papers:
        fact = corpus.documents[paper.paper_key].facts[0]
        contexts[paper.paper_key] = [
            f"{paper.condition_description} n={fact.sample_size}; {fact.follow_up_days}-day boundary."
        ]
    # The oracle normalizes the conjunction, while the wire retains the PDF sentence.
    assert corpus.documents[keys[1]].facts[0].condition not in contexts[keys[1]][0]
    if condition_case == "changed_label":
        contexts[keys[1]][0] = contexts[keys[1]][0].replace("no-promotion shelf label", "promotional shelf label")
    elif condition_case == "other_source_only":
        contexts[keys[0]].extend(deepcopy(contexts[keys[1]]))
        contexts[keys[1]] = ["n=24; 30-day boundary in one observed sample."]
    bundle = {
        "relation_id": "required-ab", "paper_ids": keys,
        "findings_left": [papers[0].finding], "findings_right": [papers[1].finding],
        "contexts_and_boundaries": contexts,
        "source_locators": {key: ["Table 2, page 2"] for key in keys},
        "required_evidence_ids": ["evidence-a", "evidence-b"],
        "provided_evidence_ids": ["evidence-a", "evidence-b"],
        "missing_evidence_ids": [], "evidence_completeness": "complete",
    }
    monkeypatch.setattr(
        _INTEGRATION_MODULE, "_ready_outline_node_payload",
        lambda _registry, _node: {"relations": [relation]},
    )
    trace = [{
        "kind": "scale_relation_adjudication_request", "node_id": "relation-adjudication",
        "relation_candidates": [relation], "relation_evidence_bundles": [bundle],
    }]
    if condition_case == "source_sentence":
        observations = _INTEGRATION_MODULE._assert_required_comparisons_have_complete_executed_bundles(
            None, [keys], trace, corpus,
        )
        assert len(observations) == 1 and observations[0]["evidence_completeness"] == "complete"
    else:
        with pytest.raises(AssertionError, match=f"condition qualifier for {keys[1]}"):
            _INTEGRATION_MODULE._assert_required_comparisons_have_complete_executed_bundles(
                None, [keys], trace, corpus,
            )


@pytest.mark.parametrize("size", _VARIANT_SIZES)
@pytest.mark.parametrize("source_variant", _ADVERSARIAL_VARIANTS)
def test_adversarial_required_pair_is_supported_by_its_actual_pdfs(
    tmp_path: Path,
    size: int,
    source_variant: str,
) -> None:
    papers = _scale_papers(size, source_variant=source_variant)
    required_comparisons = _scale_required_comparisons(papers, source_variant)
    baseline_pair = required_comparisons[0]
    assert baseline_pair == [papers[0].paper_key, papers[1].paper_key]
    expected_count = 4 if source_variant == "all_required_comparisons" else 2
    assert len(required_comparisons) == expected_count
    assert required_comparisons[0] == [papers[0].paper_key, papers[1].paper_key]
    assert len({tuple(pair) for pair in required_comparisons}) == len(required_comparisons)
    assert all(len(pair) == 2 and pair[0] != pair[1] for pair in required_comparisons)
    by_key = {paper.paper_key: paper for paper in papers}
    pdf_paths = {}
    for paper_key in dict.fromkeys(key for pair in required_comparisons for key in pair):
        paper = by_key[paper_key]
        path = tmp_path / f"{paper.stem}.pdf"
        _write_scale_pdf(path, paper)
        with fitz.open(path) as document:
            assert document.page_count == 2
            assert "Table 2 (page 2)" in document[1].get_text()
        pdf_paths[paper_key] = path

    corpus = ControlledPdfCorpus.from_pdfs(pdf_paths)
    facts = {paper_key: corpus.documents[paper_key].facts for paper_key in pdf_paths}
    assert all(len(rows) == 1 for rows in facts.values())
    assert all(len(corpus.documents[key].pdf_sha256) == 64 for key in pdf_paths)
    assert all(
        fact.locator.pdf_sha256 == corpus.documents[key].pdf_sha256
        and fact.locator.fact_sha256
        for key in pdf_paths
        for fact in facts[key]
    )

    baseline_a, baseline_b = (facts[key][0] for key in baseline_pair)
    assert (baseline_a.direction, baseline_a.magnitude_points) == ("increase", 4)
    assert (baseline_b.direction, baseline_b.magnitude_points) == ("increase", 2)

    if source_variant in {"required_null", "all_required_comparisons"}:
        null_pair = required_comparisons[1] if source_variant == "required_null" else [
            papers[0].paper_key, papers[2].paper_key
        ]
        first, second = (facts[key][0] for key in null_pair)
        assert (first.direction, first.magnitude_points) == ("increase", 4)
        assert (second.direction, second.magnitude_points) == ("null", None)
    if source_variant in {"required_reverse", "all_required_comparisons"}:
        reverse_pair = required_comparisons[1] if source_variant == "required_reverse" else [
            papers[0].paper_key, papers[3].paper_key
        ]
        first, second = (facts[key][0] for key in reverse_pair)
        assert (first.direction, first.magnitude_points) == ("increase", 4)
        assert (second.direction, second.magnitude_points) == ("decrease", 2)
    if source_variant in {"distant_cross_shard", "all_required_comparisons"}:
        distant_pair = required_comparisons[1] if source_variant == "distant_cross_shard" else [
            papers[0].paper_key, papers[-1].paper_key
        ]
        assert distant_pair == [papers[0].paper_key, papers[-1].paper_key]
        assert papers[-1].index - papers[0].index == size - 1
        rare_rows = [
            paper for paper in papers
            if _RARE_SCALE_CONDITION in paper.condition_description.casefold()
        ]
        assert len(rare_rows) == 1
        assert rare_rows[0].paper_key == distant_pair[1]
        assert facts[distant_pair[1]][0].condition.casefold() == _RARE_SCALE_CONDITION.casefold()
    if source_variant == "all_required_comparisons":
        expected_pairs = [
            [papers[0].paper_key, papers[1].paper_key],
            [papers[0].paper_key, papers[2].paper_key],
            [papers[0].paper_key, papers[3].paper_key],
            [papers[0].paper_key, papers[-1].paper_key],
        ]
        assert required_comparisons == expected_pairs
    for pair in required_comparisons:
        assert all(key in facts and len(facts[key]) == 1 for key in pair)

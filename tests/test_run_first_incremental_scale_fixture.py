from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

from tests.scientific_support_oracle import ControlledPdfCorpus, evaluate_assertion

_INTEGRATION_PATH = Path(__file__).with_name("test_run_first_scale_integration.py")
_SPEC = importlib.util.spec_from_file_location("_incremental_scale_overlay", _INTEGRATION_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"unable to load overlay integration harness: {_INTEGRATION_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
_mutated_scale_paper = _MODULE._mutated_scale_paper
_mutated_scale_qualifier_paper = _MODULE._mutated_scale_qualifier_paper
_scale_papers = _MODULE._scale_papers
_write_scale_pdf = _MODULE._write_scale_pdf


@pytest.mark.parametrize("source_variant", ["baseline", "all_required_comparisons"])
def test_one_pdf_result_mutation_preserves_key_changes_only_selected_bytes(
    tmp_path: Path, source_variant: str,
) -> None:
    papers = _scale_papers(10, source_variant=source_variant)
    paths = {paper.paper_key: tmp_path / f"{paper.stem}.pdf" for paper in papers}
    for paper in papers:
        _write_scale_pdf(paths[paper.paper_key], paper)
    before = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in paths.items()
    }

    selected = papers[-1]
    mutated = _mutated_scale_paper(selected)
    assert mutated.paper_key == selected.paper_key
    assert mutated.stem == selected.stem
    assert mutated.condition_description == selected.condition_description
    assert mutated.sample_description == selected.sample_description
    assert mutated.limitation == selected.limitation
    assert mutated.finding != selected.finding
    _write_scale_pdf(paths[selected.paper_key], mutated)

    after = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in paths.items()
    }
    assert {key for key in before if before[key] != after[key]} == {selected.paper_key}
    assert all(before[key] == after[key] for key in before if key != selected.paper_key)

    current = ControlledPdfCorpus.from_pdfs(paths)
    current_source = current.documents[selected.paper_key]
    assert current_source.pdf_sha256 == after[selected.paper_key]
    assert len(current_source.facts) == 1
    assert (current_source.facts[0].direction, current_source.facts[0].magnitude_points) == (
        "increase",
        7,
    )
    old_assertion = current.authored_assertion_from_source_titles(
        " ".join((
            selected.title,
            selected.finding,
            selected.sample_description,
            selected.limitation,
            selected.condition_description,
        )),
        required_paper_keys=(selected.paper_key,),
    )
    assert evaluate_assertion(current, old_assertion).judgment == "unsupported"


@pytest.mark.parametrize("size", [10, 100])
def test_one_pdf_qualifier_mutation_preserves_result_and_rejects_stale_condition(
    tmp_path: Path, size: int,
) -> None:
    papers = _scale_papers(size, source_variant="all_required_comparisons")
    neighbor, selected = papers[0], papers[-1]
    paths = {
        paper.paper_key: tmp_path / f"{paper.stem}.pdf"
        for paper in (neighbor, selected)
    }
    for paper in (neighbor, selected):
        _write_scale_pdf(paths[paper.paper_key], paper)

    before_hashes = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in paths.items()
    }
    before = ControlledPdfCorpus.from_pdfs(paths)
    old_facts = before.documents[selected.paper_key].facts
    assert len(old_facts) == 1
    old_fact = old_facts[0]
    mutated = _mutated_scale_qualifier_paper(selected)
    assert mutated.paper_key == selected.paper_key
    assert mutated.stem == selected.stem
    assert mutated.index == selected.index
    assert mutated.title == selected.title
    assert mutated.finding == selected.finding
    assert mutated.limitation == selected.limitation
    assert mutated.sample_description == selected.sample_description
    assert mutated.table_note == selected.table_note
    assert mutated.chinese_note == selected.chinese_note
    assert mutated.extended_source_english == selected.extended_source_english
    assert mutated.extended_source_chinese == selected.extended_source_chinese
    assert mutated.condition_description != selected.condition_description
    expected_condition = selected.condition_description.casefold().replace(
        "first-visit-only", "returning-member-only", 1
    )
    assert mutated.condition_description.casefold() == expected_condition
    assert "first-visit-only" in selected.condition_description.casefold()
    assert "first-visit-only" not in mutated.condition_description.casefold()
    assert "returning-member-only" in mutated.condition_description.casefold()

    _write_scale_pdf(paths[selected.paper_key], mutated)
    after_hashes = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in paths.items()
    }
    assert {key for key in before_hashes if before_hashes[key] != after_hashes[key]} == {
        selected.paper_key
    }
    assert before_hashes[neighbor.paper_key] == after_hashes[neighbor.paper_key]

    current = ControlledPdfCorpus.from_pdfs(paths)
    new_facts = current.documents[selected.paper_key].facts
    assert len(new_facts) == 1
    new_fact = new_facts[0]
    assert (
        new_fact.paper_key, new_fact.study_label, new_fact.sample_size, new_fact.direction,
        new_fact.magnitude_points, new_fact.follow_up_days, new_fact.required_qualifiers,
    ) == (
        old_fact.paper_key, old_fact.study_label, old_fact.sample_size, old_fact.direction,
        old_fact.magnitude_points, old_fact.follow_up_days, old_fact.required_qualifiers,
    )
    assert new_fact.condition != old_fact.condition
    assert "returning-member-only loyalty credit condition" in new_fact.condition.casefold()
    assert "first-visit-only loyalty credit condition" not in new_fact.condition.casefold()
    assert new_fact.locator.pdf_sha256 == after_hashes[selected.paper_key]

    def authored_text(condition: str) -> str:
        return " ".join((
            selected.title, selected.finding, selected.sample_description,
            selected.limitation, condition, "The result was measured at the 30-day follow-up.",
        ))

    fresh_assertion = current.authored_assertion_from_source_titles(
        authored_text(mutated.condition_description),
        required_paper_keys=(selected.paper_key,),
    )
    stale_assertion = current.authored_assertion_from_source_titles(
        authored_text(selected.condition_description),
        required_paper_keys=(selected.paper_key,),
    )
    assert evaluate_assertion(current, fresh_assertion).judgment == "supported"
    stale = evaluate_assertion(current, stale_assertion)
    assert stale.judgment == "unsupported"
    assert "condition_omitted_or_mismatched" in stale.issue_codes

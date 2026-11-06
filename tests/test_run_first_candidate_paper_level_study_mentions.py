from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from outline.candidate_paper_level_study_mentions import (
    paper_level_study_mention_occurrences_v1,
)
from services.writer_source_inventory import VerifiedWriterSourceClaimV1
from outline.v3_executor import OutlineV3ExecutionError, OutlineV3Executor
from tests.test_run_first_compact_study_paraphrase import _request_and_scope, _typed_claim

PAPER_KEY = "10.1002/dir.20024"
CLAIM_ID = "claim:10.1002/dir.20024:limitations:8cf94e233d6cf9e5"
EVIDENCE_ID = "evidence:10.1002/dir.20024:limitations:80964cccc92f97bc"
SLOT_ID = "candidate-claim-slot:v1:d41c3e045e7e9baa0f1692b3"
SOURCE_TEXT = (
    "Authors state: (1) Study 1 found no significant trust difference between "
    "purchase timing and identification favoring a frequent flyer; (2) identification "
    "scenarios included information about tracking personal information while purchase "
    "timing scenarios did not, which may have negatively affected perceptions; (3) "
    "lack of a test of consumer attributions and norm theory; (4) single-item measure "
    "for repurchase intentions; (5) only two examples of Internet price segmentation "
    "tactics were examined."
)


@pytest.fixture
def source_bindings() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_claim: dict[str, object] = {
        "claim_id": CLAIM_ID,
        "paper_key": PAPER_KEY,
        "claim_type": "author_interpretation",
        "text": SOURCE_TEXT,
        "study_id": "",
        "evidence_ids": [EVIDENCE_ID],
        "source_locator": "stage1:ai_summary.limitations",
    }
    support_row: dict[str, object] = {
        "claim_slot_id": SLOT_ID,
        "claim": SOURCE_TEXT,
        "paper_key": PAPER_KEY,
        "primary_claim_id": CLAIM_ID,
        "source_claim_ids": [CLAIM_ID],
        "evidence_ids": [EVIDENCE_ID],
        "study_id": "",
    }
    scope_slot: dict[str, object] = {
        "claim_slot_id": SLOT_ID,
        "claim_group_id": "candidate-claim-group:v1:b2258c2792eb87258e9bd660",
        "paper_key": PAPER_KEY,
        "primary_claim_id": CLAIM_ID,
        "source_claim_ids": [CLAIM_ID],
        "evidence_ids": [EVIDENCE_ID],
        "study_id": "",
    }
    return source_claim, support_row, scope_slot


def _prove(
    source_claim: dict[str, object],
    support_row: dict[str, object],
    scope_slot: dict[str, object],
    *,
    claim_text: str | None = None,
):
    return paper_level_study_mention_occurrences_v1(
        SOURCE_TEXT if claim_text is None else claim_text,
        source_claim=source_claim,
        support_row=support_row,
        scope_slot=scope_slot,
    )


def test_exact_inventory_mention_is_bound_to_its_paper_and_empty_owner(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    occurrences = _prove(*source_bindings)

    assert len(occurrences) == 1
    assert occurrences[0].literal == "Study 1"
    assert occurrences[0].study_label == "1"
    assert occurrences[0].start == SOURCE_TEXT.index("Study 1")
    assert occurrences[0].end == occurrences[0].start + len("Study 1")
    assert occurrences[0].paper_key == PAPER_KEY
    assert occurrences[0].claim_slot_id == SLOT_ID
    assert occurrences[0].source_claim_id == CLAIM_ID
    assert occurrences[0].evidence_ids == (EVIDENCE_ID,)


@pytest.mark.parametrize("owners", [(), ("",)])
def test_actual_verified_inventory_claim_shape_is_accepted(source_bindings, owners):
    _source, support, slot = source_bindings
    actual = VerifiedWriterSourceClaimV1(
        claim_id=CLAIM_ID, paper_key=PAPER_KEY, owner_study_ids=owners,
        claim_type="author_interpretation", text=SOURCE_TEXT,
        evidence_ids=(EVIDENCE_ID,), source_locator="stage1:ai_summary.limitations",
        source_summary_hash="a" * 64,
    )
    assert len(_prove(asdict(actual), support, slot)) == 1
    assert "study_id" not in asdict(actual)


def test_resolved_inventory_owner_cannot_be_hidden_by_an_empty_projection(source_bindings):
    source, support, slot = source_bindings
    source["owner_study_ids"] = ["explicit-study:1"]
    assert _prove(source, support, slot) == ()


def test_one_canonical_terminal_period_keeps_literal_offsets_lossless(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    source_text = "Authors state Study 1 appears in this list: ['one']"
    source_claim["text"] = source_text
    canonical_text = source_text + "."
    support_row["claim"] = canonical_text
    occurrences = _prove(
        source_claim,
        support_row,
        scope_slot,
        claim_text=canonical_text,
    )

    assert len(occurrences) == 1
    assert source_text[occurrences[0].start : occurrences[0].end] == occurrences[0].literal


def test_moderators_boundary_locator_is_allowed_for_paper_level_mentions(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    source_claim["source_locator"] = "stage1:ai_summary.moderators_boundaries"

    assert len(_prove(source_claim, support_row, scope_slot)) == 1


@pytest.mark.parametrize(
    ("binding", "mutator"),
    [
        ("support", lambda row: row.update(paper_key="paper-foreign")),
        ("scope", lambda row: row.update(paper_key="paper-foreign")),
        ("support", lambda row: row.update(source_claim_ids=[])),
        ("scope", lambda row: row.update(source_claim_ids=[])),
        ("source", lambda row: row.update(claim_id="claim:foreign")),
        ("source", lambda row: row.update(paper_key="paper-foreign")),
        ("support", lambda row: row.update(evidence_ids=[])),
        ("scope", lambda row: row.update(evidence_ids=[])),
        ("support", lambda row: row.update(primary_claim_id="claim:foreign")),
        ("source", lambda row: row.update(study_id="study:1")),
        ("support", lambda row: row.update(study_id="study:1")),
        ("source", lambda row: row.update(claim_type="empirical_finding")),
        ("source", lambda row: row.update(source_locator="stage1:ai_summary.results")),
    ],
)
def test_unowned_or_wrongly_typed_mentions_do_not_receive_a_proof(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
    binding: str,
    mutator,
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    target = {"source": source_claim, "support": support_row, "scope": scope_slot}[binding]
    mutator(target)

    assert _prove(source_claim, support_row, scope_slot) == ()


@pytest.mark.parametrize(
    "claim_text",
    [
        SOURCE_TEXT.replace("Study 1", "Study 2", 1),
        SOURCE_TEXT + " The provider added an unsupported Study 1 conclusion.",
        SOURCE_TEXT.replace("no significant trust difference", "a significant trust difference", 1),
    ],
)
def test_changed_or_extended_claim_text_is_not_source_attributed(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
    claim_text: str,
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    support_row["claim"] = claim_text

    assert _prove(
        source_claim,
        support_row,
        scope_slot,
        claim_text=claim_text,
    ) == ()


def test_every_mention_in_a_multi_study_source_claim_is_individually_proved(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    multi_text = "The source reports Study 1 and Experiment 2."
    source_claim["text"] = multi_text
    support_row["claim"] = multi_text

    occurrences = paper_level_study_mention_occurrences_v1(
        multi_text,
        source_claim=source_claim,
        support_row=support_row,
        scope_slot=scope_slot,
    )

    assert [(item.literal, item.study_label) for item in occurrences] == [
        ("Study 1", "1"),
        ("Experiment 2", "2"),
    ]


def test_no_study_mentions_produce_no_occurrence_proof(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    plain_text = "The source states a paper-level limitation without naming a study."
    source_claim["text"] = plain_text
    support_row["claim"] = plain_text

    assert paper_level_study_mention_occurrences_v1(
        plain_text,
        source_claim=source_claim,
        support_row=support_row,
        scope_slot=scope_slot,
    ) == ()


def test_source_evidence_must_be_in_the_exact_scope_slot(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    source_claim["evidence_ids"] = ["evidence:foreign"]

    assert _prove(source_claim, support_row, scope_slot) == ()


def test_support_row_must_carry_the_same_exact_claim_text(
    source_bindings: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> None:
    source_claim, support_row, scope_slot = deepcopy(source_bindings)
    support_row["claim"] = SOURCE_TEXT.replace("Study 1", "Study 2", 1)

    assert _prove(source_claim, support_row, scope_slot) == ()


def _executor_and_candidate(row, claim_id):
    _request, scope = _request_and_scope(row, claim_id)
    slot = scope.claim_slots[0]
    source = _typed_claim(row, claim_id)
    text = source["text"]
    support = {
        "claim_slot_id": slot.claim_slot_id, "task_id": slot.task_id,
        "claim": text, "paper_key": slot.paper_key, "primary_claim_id": slot.primary_claim_id,
        "source_claim_ids": list(slot.source_claim_ids), "evidence_ids": list(slot.evidence_ids),
        "source_field_ids": list(slot.source_field_ids), "study_id": slot.study_id,
    }
    candidate = {"candidate_id": "candidate_quote", "sections": [{
        "section_id": "section:quote", "title": "Bounded source interpretation", "goal": "Retain the source author's limitation",
        "rationale": "Use only the recorded paper-level statement", "paper_keys": [slot.paper_key],
        "paper_roles": {slot.paper_key: "source author interpretation"}, "task_ids": [slot.task_id],
        "relation_ids": [], "claims": [text], "claim_support": [support],
    }]}
    executor = object.__new__(OutlineV3Executor)
    executor._candidate_output_scope = scope
    executor.review_intent_input = {}
    executor._candidate_corpus_witness_group_ids = set()
    executor._candidate_corpus_witness_scope_hash = ""
    executor._candidate_interpretation_tables = {"source_claims": [source], "source_fields": [], "dependencies": []}
    return executor, candidate, slot.paper_key


def test_full_candidate_gate_keeps_all_verified_paper_level_source_labels():
    fixture = json.loads((Path(__file__).parent / "fixtures/run_first_paper_level_study_limits.json").read_text(encoding="utf-8"))
    assert len(fixture["claims"]) == 38
    for claim_id, row in fixture["claims"].items():
        executor, candidate, paper = _executor_and_candidate(row, claim_id)
        before = deepcopy(candidate)
        executor._validate_candidate_payload("candidate_quote", candidate, allowed_paper_keys=[paper], allowed_relation_ids=[])
        assert candidate == before
        assert candidate["sections"][0]["claim_support"][0]["study_id"] == ""


@pytest.mark.parametrize("mutation", ["new_study", "new_assertion", "empirical", "foreign_source", "missing_evidence", "no_scope"])
def test_full_candidate_gate_rejects_unproven_paper_level_quote_variants(mutation):
    fixture = json.loads((Path(__file__).parent / "fixtures/run_first_paper_level_study_limits.json").read_text(encoding="utf-8"))
    claim_id, row = next(iter(fixture["claims"].items()))
    executor, candidate, paper = _executor_and_candidate(row, claim_id)
    section = candidate["sections"][0]
    source = executor._candidate_interpretation_tables["source_claims"][0]
    if mutation == "new_study":
        text = section["claims"][0].replace("Study 1", "Study 99", 1)
        assert text != section["claims"][0]
        section["claims"] = [text]
        section["claim_support"][0]["claim"] = text
    elif mutation == "new_assertion":
        text = section["claims"][0] + " Study 99 proves a new causal mechanism."
        section["claims"] = [text]
        section["claim_support"][0]["claim"] = text
    elif mutation == "empirical":
        source["claim_type"] = "empirical_finding"
    elif mutation == "foreign_source":
        source["paper_key"] = "foreign-paper"
    elif mutation == "missing_evidence":
        section["claim_support"][0]["evidence_ids"] = []
    else:
        executor._candidate_output_scope = None
    with pytest.raises(OutlineV3ExecutionError):
        executor._validate_candidate_payload("candidate_quote", candidate, allowed_paper_keys=[paper], allowed_relation_ids=[])

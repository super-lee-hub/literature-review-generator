"""Source-bound occurrence proofs for paper-level Study N mentions.

This module does not validate a Registry, inventory, or CandidateOutputScope.
Callers must first validate the full scope and resolve ``source_claim`` from
that verified Stage 1 inventory. This is not independent PDF-excerpt evidence;
original-source validation remains a downstream requirement. It only proves
that a literal Study/Experiment/Trial
mention is an unchanged occurrence in one ownerless paper-level Stage 1
author-interpretation claim and that the compiled support row is the exact
scope slot carrying that claim and its evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PAPER_LEVEL_LOCATORS = frozenset(
    {
        "stage1:ai_summary.limitations",
        "stage1:ai_summary.moderators_boundaries",
    }
)
_STUDY_MENTION = re.compile(
    r"(?:\b(?:study|experiment|trial)|研究|实验)\s*([A-Za-z]?[1-9]\d?)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PaperLevelStudyMentionOccurrenceV1:
    """One exact mention bound to a source-owned paper-level support row."""

    literal: str
    study_label: str
    start: int
    end: int
    paper_key: str
    claim_slot_id: str
    source_claim_id: str
    evidence_ids: tuple[str, ...]


def paper_level_study_mention_occurrences_v1(
    claim_text: str,
    *,
    source_claim: Mapping[str, Any],
    support_row: Mapping[str, Any],
    scope_slot: Mapping[str, Any],
) -> tuple[PaperLevelStudyMentionOccurrenceV1, ...]:
    """Return occurrence proofs only for exact, paper-owned qualifier claims.

    ``scope_slot`` must come from the already validated complete
    CandidateOutputScope. ``source_claim`` must be the exact inventory record
    resolved for that scope. A single appended terminal period is allowed
    because the current Stage 1 canonicalizer adds that punctuation to two
    list-valued limitation strings; all occurrence offsets remain unchanged.
    Any unsupported shape or mismatched binding returns no proofs, leaving the
    existing explicit-study gate to reject the mention.
    """

    if not isinstance(claim_text, str):
        return ()

    claim_id = source_claim.get("claim_id")
    source_text = source_claim.get("text")
    source_type = source_claim.get("claim_type")
    source_study_id = source_claim.get("study_id", "")
    source_owners = source_claim.get("owner_study_ids")
    source_locator = source_claim.get("source_locator")
    if (
        not isinstance(claim_id, str)
        or not claim_id
        or not isinstance(source_text, str)
        or source_type != "author_interpretation"
        or source_study_id != ""
        or not ("study_id" in source_claim or "owner_study_ids" in source_claim)
        or source_locator not in _PAPER_LEVEL_LOCATORS
    ):
        return ()
    if "owner_study_ids" in source_claim and (
        not isinstance(source_owners, Sequence) or isinstance(source_owners, (str, bytes))
        or any(not isinstance(owner, str) or owner for owner in source_owners)
    ):
        return ()

    if claim_text != source_text and not (
        not source_text.endswith((".", "!", "?")) and claim_text == source_text + "."
    ):
        return ()

    slot_id = scope_slot.get("claim_slot_id")
    paper_key = scope_slot.get("paper_key")
    primary_claim_id = scope_slot.get("primary_claim_id")
    source_slot_study_id = scope_slot.get("study_id")
    support_slot_id = support_row.get("claim_slot_id")
    support_claim_text = support_row.get("claim")
    support_paper_key = support_row.get("paper_key")
    support_primary_claim_id = support_row.get("primary_claim_id")
    support_study_id = support_row.get("study_id")
    if (
        not isinstance(slot_id, str)
        or not slot_id
        or support_slot_id != slot_id
        or support_claim_text != claim_text
        or not isinstance(paper_key, str)
        or not paper_key
        or support_paper_key != paper_key
        or not isinstance(primary_claim_id, str)
        or primary_claim_id != claim_id
        or support_primary_claim_id != primary_claim_id
        or source_slot_study_id != ""
        or support_study_id != ""
    ):
        return ()

    scope_claim_ids = _string_ids(scope_slot.get("source_claim_ids"))
    support_claim_ids = _string_ids(support_row.get("source_claim_ids"))
    source_evidence_ids = _string_ids(source_claim.get("evidence_ids"))
    scope_evidence_ids = _string_ids(scope_slot.get("evidence_ids"))
    support_evidence_ids = _string_ids(support_row.get("evidence_ids"))
    if (
        scope_claim_ids is None
        or support_claim_ids is None
        or source_evidence_ids is None
        or scope_evidence_ids is None
        or support_evidence_ids is None
        or not source_evidence_ids
        or set(scope_claim_ids) != set(support_claim_ids)
        or claim_id not in scope_claim_ids
        or claim_id not in support_claim_ids
        or set(scope_evidence_ids) != set(support_evidence_ids)
        or not set(source_evidence_ids).issubset(scope_evidence_ids)
    ):
        return ()

    source_paper_key = source_claim.get("paper_key")
    if source_paper_key != paper_key:
        return ()

    occurrences: list[PaperLevelStudyMentionOccurrenceV1] = []
    for match in _STUDY_MENTION.finditer(claim_text):
        start, end = match.span()
        literal = match.group(0)
        if source_text[start:end] != literal:
            return ()
        occurrences.append(
            PaperLevelStudyMentionOccurrenceV1(
                literal=literal,
                study_label=match.group(1),
                start=start,
                end=end,
                paper_key=paper_key,
                claim_slot_id=slot_id,
                source_claim_id=claim_id,
                evidence_ids=source_evidence_ids,
            )
        )
    return tuple(occurrences)


def _string_ids(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    items = tuple(value)
    if any(not isinstance(item, str) or not item for item in items):
        return None
    if len(set(items)) != len(items):
        return None
    return items

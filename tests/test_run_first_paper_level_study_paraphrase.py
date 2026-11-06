from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from services.writer_source_inventory import VerifiedWriterSourceClaimV1
from tests.test_current_runtime_full_e2e import (
    _source_attributed_paper_level_limitation,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "run_first_paper_level_study_limits.json"
_ORACLE_LABEL = re.compile(
    r"\b(Studies|Study)\s+(\d+[A-Za-z]?(?:(?:\s+and\s+|\s*,\s*|\s*&\s*|\s+or\s+|\s*[-–—]\s*|\s+to\s+|\s+through\s+)\d+[A-Za-z]?)*)(['’]s)?(?!\w)",
    flags=re.IGNORECASE,
)
_ORACLE_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
}


def _typed_claim(row: dict[str, Any]) -> dict[str, Any]:
    claim = VerifiedWriterSourceClaimV1(
        claim_id=str(row["claim_id"]),
        paper_key=str(row["paper_key"]),
        owner_study_ids=tuple(str(value) for value in row.get("owner_study_ids") or ()),
        claim_type=str(row["claim_type"]),
        text=str(row["source_text"]),
        evidence_ids=tuple(str(value) for value in row.get("evidence_ids") or ()),
        source_locator=str(row["source_locator"]),
        source_summary_hash=str(row["source_summary_hash"]),
    )
    raw = asdict(claim)
    raw["study_id"] = str(row.get("study_id") or "")
    return raw


def _independent_source_faithfulness_oracle(text: str) -> str:
    """Expected display wording derived from raw source strings, independently."""

    def render(match: re.Match[str]) -> str:
        label = str(match.group(1)).casefold()
        sequence = str(match.group(2))
        prior_text = text[:match.start()].rstrip()
        source_prefix = "The paper's" if not prior_text or prior_text.endswith((".", "?", "!")) else "the paper's"
        possessive = str(match.group(3) or "")
        parts = re.split(r"(\s*(?:and|,|&|or|to|through|[-–—])\s*)", sequence)
        rendered_parts: list[str] = []
        number_count = 0
        for part in parts:
            if not part:
                continue
            token = re.fullmatch(r"(\d+)([A-Za-z]?)", part.strip())
            if token:
                number = int(token.group(1))
                assert number in _ORACLE_ORDINALS
                suffix = token.group(2)
                rendered_parts.append(
                    _ORACLE_ORDINALS[number] + (f" (part {suffix})" if suffix else "")
                )
                number_count += 1
            elif part.strip().casefold() in {"-", "–", "—", "to", "through"}:
                rendered_parts.append(" through ")
            else:
                rendered_parts.append(part)
        rendered_sequence = "".join(rendered_parts).strip()
        if label == "study" and number_count == 1:
            ordinal, *suffix_parts = rendered_parts[0].split(" (part ", 1)
            suffix_text = f" (part {suffix_parts[0]}" if suffix_parts else ""
            return f"{source_prefix} {ordinal} study{suffix_text}{possessive}"
        return f"{source_prefix} {rendered_sequence} studies{possessive}"

    return _ORACLE_LABEL.sub(render, text)


def test_all_verified_paper_level_limitations_keep_every_typed_source_proposition() -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    claims = fixture["claims"]
    assert len(claims) == 38
    assert len({str(row["source_text"]) for row in claims.values()}) == 19
    assert fixture["source_artifact"]["artifact_id"] == "outline-v3:outline_content_layers"
    assert fixture["source_artifact"]["content_hash"] == "b03b04b7d3b973256a4f723c9f0f42b058a2dcd566308c116edb5eaa60fb1e9b"

    locators = {
        "stage1:ai_summary.limitations",
        "stage1:ai_summary.moderators_boundaries",
    }
    observed_locators: dict[str, int] = {}
    for claim_id, row in claims.items():
        typed_source = _typed_claim({**row, "claim_id": claim_id})
        assert typed_source["owner_study_ids"] == ("",)
        assert not any(str(value).strip() for value in typed_source["owner_study_ids"])
        assert typed_source["study_id"] == ""
        assert typed_source["claim_type"] == "author_interpretation"
        assert typed_source["source_locator"] in locators
        observed_locators[typed_source["source_locator"]] = (
            observed_locators.get(typed_source["source_locator"], 0) + 1
        )
        before = deepcopy(typed_source)

        rendered = _source_attributed_paper_level_limitation(typed_source)
        expected = _independent_source_faithfulness_oracle(str(row["source_text"]))

        # The oracle checks every source word outside the exact numeric label
        # spans, including each limitation, example, and condition.
        assert rendered == expected
        assert typed_source == before
        assert typed_source["claim_id"] == claim_id
        assert sorted(typed_source["evidence_ids"]) == sorted(row["evidence_ids"])
        assert typed_source["paper_key"] == row["paper_key"]
        assert not re.search(r"\b(?:Studies|Study)\s*\d", rendered, re.IGNORECASE)
    assert observed_locators == {
        "stage1:ai_summary.limitations": 19,
        "stage1:ai_summary.moderators_boundaries": 19,
    }


def test_only_the_exact_paper_level_limitation_class_is_rewritten() -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    row = next(iter(fixture["claims"].values()))
    unchanged_claims = [
        {
            "claim_id": "claim:unrelated:finding",
            "paper_key": "10.5555/unrelated",
            "claim_type": "empirical_finding",
            "text": "The result is reported in Study 1.",
            "evidence_ids": ["evidence:unrelated:finding"],
            "source_locator": "stage1:ai_summary.findings",
            "source_summary_hash": "f" * 64,
            "owner_study_ids": ("",),
            "study_id": "",
        },
        {
            "claim_id": "claim:unrelated:future-direction",
            "paper_key": "10.5555/unrelated",
            "claim_type": "author_interpretation",
            "text": "Future work should extend Study 1.",
            "evidence_ids": ["evidence:unrelated:future"],
            "source_locator": "stage1:ai_summary.future_directions",
            "source_summary_hash": "f" * 64,
            "owner_study_ids": ("",),
            "study_id": "",
        },
        {
            "claim_id": "claim:unrelated:owned-limitation",
            "paper_key": "10.5555/unrelated",
            "claim_type": "author_interpretation",
            "text": "The limitation concerns Study 1.",
            "evidence_ids": ["evidence:unrelated:limitation"],
            "source_locator": "stage1:ai_summary.limitations",
            "source_summary_hash": "f" * 64,
            "owner_study_ids": ("study-1",),
            "study_id": "study-1",
        },
    ]
    for claim in unchanged_claims:
        assert _source_attributed_paper_level_limitation(claim) == claim["text"]

    exact = _typed_claim(row)
    with pytest.raises(AssertionError, match="different source paper"):
        _source_attributed_paper_level_limitation({**exact, "paper_key": "foreign-paper"})
    with pytest.raises(AssertionError, match="study-level owner"):
        _source_attributed_paper_level_limitation(exact, selected_study_id="study-1")
    with pytest.raises(AssertionError, match="typed source"):
        _source_attributed_paper_level_limitation({**exact, "text": exact["text"] + " Added claim."})


def test_alias_bindings_are_exact_and_reversible_for_every_snapshot_paper() -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    alias_map = fixture["paper_alias_map_snapshot"]
    seen_papers: set[str] = set()
    for row in fixture["claims"].values():
        paper_key = str(row["paper_key"])
        if paper_key in seen_papers:
            continue
        seen_papers.add(paper_key)
        aliased_source = _typed_claim(row)
        aliased_source["paper_key"] = row["provider_alias"]
        rendered = _source_attributed_paper_level_limitation(
            aliased_source,
            selected_paper_key=str(row["provider_alias"]),
            paper_alias_map=alias_map,
        )
        assert rendered == _independent_source_faithfulness_oracle(str(row["source_text"]))

    first = next(iter(fixture["claims"].values()))
    other = next(row for row in fixture["claims"].values() if row["paper_key"] != first["paper_key"])
    wrong_map = json.loads(json.dumps(alias_map))
    wrong_map["papers"][first["paper_key"]] = other["provider_alias"]
    wrong_map["papers_reverse"][first["provider_alias"]] = other["paper_key"]
    source = _typed_claim(first)
    source["paper_key"] = first["provider_alias"]
    with pytest.raises(AssertionError, match="different source paper"):
        _source_attributed_paper_level_limitation(
            source,
            selected_paper_key=first["provider_alias"],
            paper_alias_map=wrong_map,
        )

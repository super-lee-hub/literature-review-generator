from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from outline.candidate_output_scope import (
    CandidateOutputClaimGroupV1,
    CandidateOutputScopeV1,
    CandidateOutputSupportSlotV1,
)
from outline.candidate_request_packing import expand_candidate_slot_output
from tests.test_current_runtime_full_e2e import (
    _source_attributed_paper_level_limitation,
)
from tests.test_run_first_scale_integration import _compact_candidate

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "run_first_paper_level_study_limits.json"


def _typed_claim(row: dict[str, Any], claim_id: str) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "paper_key": str(row["paper_key"]),
        "owner_study_ids": list(row.get("owner_study_ids") or ()),
        "claim_type": str(row["claim_type"]),
        "text": str(row["source_text"]),
        "evidence_ids": list(row["evidence_ids"]),
        "source_locator": str(row["source_locator"]),
        "source_summary_hash": str(row["source_summary_hash"]),
        "study_id": str(row.get("study_id") or ""),
    }


def _request_and_scope(
    row: dict[str, Any], claim_id: str, *, provider_alias: bool = False,
) -> tuple[dict[str, Any], CandidateOutputScopeV1]:
    paper_key = str(row["paper_key"])
    wire_paper_key = str(row["provider_alias"]) if provider_alias else paper_key
    group_id = f"candidate-claim-group:v1:{claim_id.rsplit(':', 1)[-1]}"
    slot_id = f"candidate-claim-slot:v1:{claim_id.rsplit(':', 1)[-1]}"
    task_id = "topic_synthesis:fixture:paper_level_limit"
    slot = CandidateOutputSupportSlotV1(
        claim_slot_id=slot_id,
        claim_group_id=group_id,
        task_id=task_id,
        topic_id="topic:fixture:paper_level_limit",
        fragment_id="fragment:fixture:paper_level_limit",
        provider_result_id="result:fixture:paper_level_limit",
        synthesis_claim_id=f"synthesis:{claim_id}",
        paper_key=paper_key,
        primary_claim_id=claim_id,
        study_id="",
        source_claim_ids=(claim_id,),
        evidence_ids=tuple(sorted(str(value) for value in row["evidence_ids"])),
        source_field_ids=(),
        relation_ids=(),
    )
    group = CandidateOutputClaimGroupV1(
        claim_group_id=group_id,
        task_id=task_id,
        topic_id=slot.topic_id,
        fragment_id=slot.fragment_id,
        provider_result_id=slot.provider_result_id,
        synthesis_claim_id=slot.synthesis_claim_id,
        claim_hash="a" * 64,
        paper_keys=(paper_key,),
        support_slot_ids=(slot_id,),
    )
    scope = CandidateOutputScopeV1(
        registry_job_id="fixture-job",
        source_inventory_artifact_id="outline-v3:outline_content_layers",
        source_inventory_artifact_hash="b" * 64,
        source_inventory_content_hash="c" * 64,
        task_ids=(task_id,),
        selected_relation_ids=(),
        claim_groups=(group,),
        claim_slots=(slot,),
    )
    typed_claim = _typed_claim(row, claim_id)
    typed_claim["paper_key"] = wire_paper_key
    request = {
        "candidate_id": "candidate_fixture",
        "organizing_logic": "paper_level_source_limitations",
        "candidate_output_scope": {
            "claim_group_ids": [group_id],
            "required_claim_group_indices": [],
            "organization_tasks": [],
            "claim_slots": [{
                "claim_group_index": 0,
                **slot.to_dict(),
                "paper_key": wire_paper_key,
            }],
        },
        "hierarchy": {
            "shard_id": "fixture-shard",
            "claim_group_ids": [group_id],
            "group_assignment": {group_id: "fixture-shard"},
        },
        "shared_semantic_context": {
            "interpretation_source_tables": {
                "source_claims": [typed_claim],
                "source_fields": [],
                "dependencies": [],
            },
        },
        "evidence": [{"paper_key": wire_paper_key, "title": f"Title for {wire_paper_key}"}],
    }
    return request, scope


def test_actual_compact_fixture_paraphrase_round_trips_exact_claim_and_support_closure() -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    for claim_id, row in fixture["claims"].items():
        request, scope = _request_and_scope(row, claim_id, provider_alias=True)
        compact = _compact_candidate("candidate_fixture_provider_generation:local:fixture-shard", request)

        claim_wire = compact["sections"][0]["claims"][0]
        assert claim_wire["claim_group_indices"] == [0]
        paraphrased_source = _source_attributed_paper_level_limitation(
            _typed_claim(row, claim_id), selected_study_id=""
        )
        assert claim_wire["text"] == (
            f"The included source records Title for {row['provider_alias']}: "
            f"{paraphrased_source.rstrip('.。;； ')}."
        )
        assert not re.search(r"\b(?:Studies|Study)\s*\d", claim_wire["text"], re.IGNORECASE)
        assert "paper's" in claim_wire["text"]

        expanded = expand_candidate_slot_output(compact, scope)
        scope.validate(expanded)
        section = expanded["sections"][0]
        support = section["claim_support"][0]
        assert section["claims"] == [claim_wire["text"]]
        assert support["claim"] == claim_wire["text"]
        assert support["claim_slot_id"] == scope.claim_slots[0].claim_slot_id
        assert support["task_id"] == scope.claim_slots[0].task_id
        assert support["paper_key"] == row["paper_key"]
        assert support["primary_claim_id"] == claim_id
        assert support["source_claim_ids"] == [claim_id]
        assert support["evidence_ids"] == sorted(row["evidence_ids"])
        assert support["source_field_ids"] == []
        assert "study_id" not in support

        foreign_alias = next(
            other["provider_alias"]
            for other_id, other in fixture["claims"].items()
            if other["paper_key"] != row["paper_key"]
        )
        foreign_request = json.loads(json.dumps(request))
        foreign_request["shared_semantic_context"]["interpretation_source_tables"]["source_claims"][0]["paper_key"] = foreign_alias
        foreign_request["candidate_output_scope"]["claim_slots"][0]["paper_key"] = foreign_alias
        foreign_request["evidence"][0]["paper_key"] = foreign_alias
        with pytest.raises(AssertionError, match="different source paper"):
            _compact_candidate(
                "candidate_fixture_provider_generation:local:fixture-shard",
                foreign_request,
            )

        foreign_slot_request = json.loads(json.dumps(request))
        foreign_slot_request["candidate_output_scope"]["claim_slots"][0]["paper_key"] = foreign_alias
        with pytest.raises(AssertionError, match="different source paper"):
            _compact_candidate(
                "candidate_fixture_provider_generation:local:fixture-shard",
                foreign_slot_request,
            )

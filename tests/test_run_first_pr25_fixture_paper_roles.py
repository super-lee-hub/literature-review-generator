"""Source-function roles prevent duplicate fixture assignments from looking redundant."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tests.test_pr25_repair_v2 import _grounded_fixture_candidate


FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINDINGS_REQUEST = FIXTURES / "run_first_current88_candidate1_findings_request.json"
CONSTRUCT_REQUEST = FIXTURES / "run_first_current88_candidate1_construct_request.json"
CAPTURES = {
    FINDINGS_REQUEST: (
        89714,
        "3f237012589054fbc15c9973e6d15f996d08c38b2e2a9c46b09ec4099840f507",
    ),
    CONSTRUCT_REQUEST: (
        85134,
        "7bc931f8282018b36928337ab24b8f1acd5f50e233dfe73363ab6739c03dae01",
    ),
}


def _exact_candidate(path: Path) -> tuple[str, dict[str, Any]]:
    expected_bytes, expected_hash = CAPTURES[path]
    body = path.read_bytes()
    assert len(body) == expected_bytes
    assert hashlib.sha256(body).hexdigest() == expected_hash
    wire = json.loads(body)
    user_messages = [
        str(message.get("content") or "")
        for message in wire.get("messages") or ()
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert len(user_messages) == 1
    envelope = json.loads(user_messages[0])
    assert str(envelope.get("node_id") or "").startswith(
        "candidate_1_provider_generation:local:candidate_group_"
    )
    request = envelope.get("request")
    assert isinstance(request, dict)
    return str(envelope["node_id"]), request


def test_current88_duplicate_paper_sections_have_distinct_source_function_roles() -> None:
    generated_sections = []
    for path in (FINDINGS_REQUEST, CONSTRUCT_REQUEST):
        node_id, request = _exact_candidate(path)
        result = _grounded_fixture_candidate(node_id, request)
        generated_sections.extend(result["sections"])

    assert len(generated_sections) == 5
    assignments: dict[str, list[str]] = defaultdict(list)
    primary_families_by_paper: dict[str, list[set[str]]] = defaultdict(list)
    for section in generated_sections:
        paper_keys = {str(value) for value in section.get("paper_keys") or ()}
        roles = section.get("paper_roles")
        assert isinstance(roles, dict)
        assert set(roles) == paper_keys
        support = [row for row in section.get("claim_support") or () if isinstance(row, dict)]
        assert {str(row.get("paper_key") or "") for row in support} == paper_keys
        for paper_key in paper_keys:
            role = str(roles.get(paper_key) or "").strip()
            assert role
            assignments[paper_key].append(role.casefold())
            primary_claim_ids = {
                str(row.get("primary_claim_id") or "")
                for row in support if str(row.get("paper_key") or "") == paper_key
            }
            primary_families_by_paper[paper_key].append({
                value.split(":")[2] for value in primary_claim_ids if len(value.split(":")) > 3
            })

    repeated = {paper: roles for paper, roles in assignments.items() if len(roles) > 1}
    assert set(repeated) == {"P003", "P004"}
    assert all(len(set(roles)) == len(roles) for roles in repeated.values())
    for paper_key, family_sets in primary_families_by_paper.items():
        if paper_key not in repeated:
            continue
        assert {next(iter(families)) for families in family_sets} == {
            "findings", "concept_definitions",
        }
        role_by_family = {
            next(iter(families)): role
            for families, role in zip(family_sets, assignments[paper_key], strict=True)
        }
        assert "empirical outcome finding" in role_by_family["findings"]
        assert "focal construct" in role_by_family["concept_definitions"]

    comparison = next(section for section in generated_sections if section.get("relation_ids"))
    assert set(comparison["relation_ids"]) == {"R008"}
    assert comparison["paper_roles"]["P001"].endswith("study-b.")
    assert comparison["paper_roles"]["P002"].endswith("study-a.")
    comparison_claim = comparison["claims"][0].casefold()
    assert all(value in comparison_claim for value in ("four-point", "two-point", "24 participants"))

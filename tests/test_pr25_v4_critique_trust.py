"""Trust-boundary regressions for V4 critique dispositions.

These tests use only the repository's deterministic local provider fixture.
They make no external provider calls.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from outline.v3_critique import derive_critique_disposition
from tests.test_outline_v3_semantic_execution import (
    _configured_test_provider,
    _executor,
)


def _untrusted_candidate_shard_rows() -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    hashes = {"candidate_1": "hash-1", "candidate_10": "hash-10"}
    contents = {
        "candidate_1": {
            "candidate_id": "candidate_1",
            "sections": [{"section_id": "section-1", "claims": ["claim-1"]}],
        },
        "candidate_10": {
            "candidate_id": "candidate_10",
            "sections": [{"section_id": "section-10", "claims": ["claim-10"]}],
        },
    }
    diagnostic = "candidate_1 has unsupported evidence"
    payload = {
        "passed": False,
        "blocking_diagnostics": [diagnostic],
        # This is provider-controlled in the flat path. It must not acquire
        # planner trust merely by using the hierarchical result field name.
        "candidate_shard_results": {
            "candidate_1:shard:1": {
                "candidate_id": "candidate_1",
                "shard_index": 1,
                "reviewed_section_ids": ["section-1"],
                "parent_candidate_hash": hashes["candidate_1"],
                "passed": True,
                "blocking_diagnostics": [],
                "issues": [],
            },
            "candidate_10:shard:1": {
                "candidate_id": "candidate_10",
                "shard_index": 1,
                "reviewed_section_ids": ["section-10"],
                "parent_candidate_hash": hashes["candidate_10"],
                "passed": False,
                "blocking_diagnostics": [diagnostic],
                "issues": [],
            },
        },
    }
    return hashes, contents, payload


def test_flat_provider_shard_rows_cannot_scope_an_unscoped_negative() -> None:
    hashes, contents, payload = _untrusted_candidate_shard_rows()

    disposition = derive_critique_disposition(
        {"evidence_critique": payload},
        candidate_hashes=hashes,
        candidate_contents=contents,
    )

    assert disposition["global_blocker"] is True
    assert disposition["eligible_candidate_ids"] == []
    assert disposition["blocked_candidate_ids"] == ["candidate_1", "candidate_10"]


def test_flat_provider_shard_rows_block_before_primary_arbitration(tmp_path: Path) -> None:
    diagnostic = "candidate_1 has unsupported evidence"

    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if node_id == "evidence_critique":
            candidate_contents = request.get("candidate_contents") or {}
            candidate_hashes = request.get("candidate_hashes") or {}
            rows: dict[str, dict[str, Any]] = {}
            failed_id = next(
                (candidate_id for candidate_id in candidate_contents if candidate_id != "candidate_1"),
                "candidate_2",
            )
            for candidate_id, candidate in candidate_contents.items():
                sections = [
                    section for section in candidate.get("sections") or ()
                    if isinstance(section, Mapping)
                ]
                failed = candidate_id == failed_id
                rows[f"{candidate_id}:shard:1"] = {
                    "candidate_id": candidate_id,
                    "shard_index": 1,
                    "reviewed_section_ids": [
                        str(section.get("section_id") or "") for section in sections
                    ],
                    "parent_candidate_hash": str(candidate_hashes.get(candidate_id) or ""),
                    "passed": not failed,
                    "blocking_diagnostics": [diagnostic] if failed else [],
                    "issues": [],
                }
            return {
                "status": "success",
                "content": {
                    "node_id": "evidence_critique",
                    "passed": False,
                    "issues": [],
                    "blocking_diagnostics": [diagnostic],
                    "candidate_shard_results": rows,
                },
            }
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="off",
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    assert "arbitration" not in result.artifacts
    assert result.artifacts.get("evidence_critique")


@pytest.mark.parametrize("verdict_case", ["typed_blocker", "missing_passed", "non_boolean_passed"])
def test_stability_variant_critique_uses_fail_closed_disposition(
    tmp_path: Path,
    verdict_case: str,
) -> None:
    def provider(node_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        prompt_authority = request.get("_prompt_authority") or {}
        concrete_node_id = str(prompt_authority.get("node_id") or node_id)
        if (
            concrete_node_id.startswith("stability:")
            and concrete_node_id.endswith(":evidence_critique")
        ):
            content: dict[str, Any] = {
                "node_id": "evidence_critique",
                "issues": [],
                "blocking_diagnostics": [],
            }
            if verdict_case == "typed_blocker":
                content.update({
                    "passed": True,
                    "issues": [{
                        "issue_id": "variant-global-blocker",
                        "scope": "global",
                        "target_ids": [],
                        "severity": "blocking",
                        "evidence_refs": [],
                        "resolution_status": "unresolved",
                        "parent_candidate_hash": "",
                        "message": "The stability variant has an unresolved evidence blocker.",
                    }],
                })
            elif verdict_case == "non_boolean_passed":
                content["passed"] = "false"
            # For missing_passed the intentionally malformed output omits it.
            return {"status": "success", "content": content}
        return _configured_test_provider(node_id, request)

    result = _executor(
        tmp_path,
        provider=provider,
        stability_mode="smoke",
    ).run()

    assert result.ok is False
    assert result.status == "blocked"
    stability_path = result.artifacts.get("stability_audit")
    assert stability_path
    envelope = json.loads(Path(stability_path).read_text(encoding="utf-8"))
    payload = envelope.get("payload")
    assert isinstance(payload, dict)
    assert payload.get("status") == "blocked"
    assert payload.get("variant_errors", {}).get("summary_order_reversed")


def test_clean_local_critique_remains_a_passing_control(tmp_path: Path) -> None:
    result = _executor(
        tmp_path,
        provider=lambda node_id, request: copy.deepcopy(
            _configured_test_provider(node_id, request)
        ),
        stability_mode="off",
    ).run()

    assert result.ok is True, result.diagnostics

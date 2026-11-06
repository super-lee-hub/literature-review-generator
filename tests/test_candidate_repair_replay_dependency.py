"""Reject invalid repair authority during exact replay."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from outline.v3_executor import OutlineV3Executor
from services.artifact_registry import ArtifactRegistry
from tests.test_outline_candidate_contract_robustness import (
    FAKE_DOI,
    FAKE_SMITH,
    PAPER_A,
    _capturing_provider,
    _executor,
)
from tests.test_outline_candidate_contract_robustness import (
    OutlineV3Executor as ContractTestExecutor,
)


def test_missing_tampered_and_foreign_repair_outputs_fail_closed_without_duplicate_repair_posts(
    tmp_path: Path,
) -> None:
    assert ContractTestExecutor is OutlineV3Executor
    bad = {
        "status": "success",
        "content": {
            "candidate_id": "candidate_2",
            "organizing_logic": "problem_evidence_synthesis",
            "sections": [{
                "section_id": "candidate_2_section_1",
                "title": "Synthesis",
                "paper_keys": [PAPER_A, FAKE_DOI, FAKE_SMITH],
                "relation_ids": [],
                "claims": ["planned claim"],
            }],
            "claims": ["planned claim"],
        },
    }
    cleaned = {
        "status": "success",
        "content": {
            "candidate_id": "candidate_2",
            "organizing_logic": "problem_evidence_synthesis",
            "sections": [{
                "section_id": "candidate_2_section_1",
                "title": "Synthesis",
                "paper_keys": [PAPER_A],
                "relation_ids": [],
                "claims": ["planned claim"],
            }],
            "claims": ["planned claim"],
        },
    }

    for failure in ("missing", "wrong_hash", "foreign_node"):
        case_dir = tmp_path / failure
        holder: dict = {}
        executor = _executor(
            case_dir,
            provider=_capturing_provider(holder, bad_generation=bad, bad_repair=cleaned),
            semantic_repair_enabled=True,
        )
        original_verify = OutlineV3Executor._verify_exact_replay_with_second_executor
        verify_calls: list[str] = []
        registry_get_calls: list[str] = []

        def verify_with_corrupt_repair_authority(
            self: OutlineV3Executor,
            _failure: str = failure,
            _original_verify=original_verify,
            _verify_calls: list[str] = verify_calls,
            _registry_get_calls: list[str] = registry_get_calls,
        ):
            _verify_calls.append(self.job_id)
            source_record = self.artifact_records.get("candidate_2_semantic_repair")
            assert source_record is not None
            original_get = ArtifactRegistry.get

            def get_repair_record(
                registry: ArtifactRegistry,
                artifact_id: str,
                _registry_get_calls: list[str] = _registry_get_calls,
            ):
                _registry_get_calls.append(artifact_id)
                if registry is self.registry or artifact_id != source_record.artifact_id:
                    return original_get(registry, artifact_id)
                if _failure == "missing":
                    return None
                if _failure == "wrong_hash":
                    return replace(source_record, content_hash="0" * 64)
                return replace(
                    source_record,
                    metadata={**source_record.metadata, "node_id": "candidate_3_semantic_repair"},
                )

            with patch.object(ArtifactRegistry, "get", get_repair_record):
                return _original_verify(self)

        with patch.object(
            OutlineV3Executor,
            "_verify_exact_replay_with_second_executor",
            verify_with_corrupt_repair_authority,
        ):
            result = executor.run()

        assert verify_calls == ["outline-job"]
        assert any("outline-v3:repair:" in item for item in registry_get_calls)
        assert result.status == "blocked"
        stability_record = executor.registry.get("outline-v3:stability_audit")
        assert stability_record is not None and stability_record.status == "ready"
        stability = json.loads(Path(stability_record.path).read_text(encoding="utf-8"))["payload"]
        error = str(stability.get("variant_errors", {}).get("exact_replay_resume") or "")
        assert "current READY repair output Registry record is foreign or changed" in error
        repair_posts = [
            (node_id, request)
            for node_id, request in holder.get("requests", [])
            if json.loads(request).get("task") == "semantic_repair_of_outline_candidate"
        ]
        # This robustness fixture deliberately repairs both the primary
        # candidate and the reversed-order stability candidate. The replay
        # must not trigger an additional primary repair request.
        assert sorted(node_id for node_id, _request in repair_posts) == [
            "candidate_2_provider_generation",
            "candidate_2_semantic_repair",
        ]

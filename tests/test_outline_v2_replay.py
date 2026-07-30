from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.outline_v2_replay import (
    REPLAY_MANIFEST_SCHEMA,
    REPLAY_RAW_OUTPUT_SCHEMA,
    REPLAY_REQUEST_SCHEMA,
    OutlineV2ReplayCaller,
    OutlineV2ReplayError,
    canonical_json_sha256,
    prompt_sha256,
)
from services.artifact_registry import file_sha256


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _replay_fixture(tmp_path: Path) -> tuple[Path, str, dict[str, object], object]:
    prompt = "Generate three evidence-grounded outline candidates."
    metadata: dict[str, object] = {
        "stage": "outline_candidates",
        "prompt_budget": {"max_output_tokens": 16000},
    }
    response = {
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "sections": [{"title": "Evidence synthesis"}],
            }
        ]
    }
    request_path = tmp_path / "request_001.json"
    _write_json(
        request_path,
        {
            "artifact_type": "outline_v2_subagent_request",
            "artifact_version": "v1",
            "schema_version": REPLAY_REQUEST_SCHEMA,
            "call_index": 1,
            "route": "Outline_API",
            "stage": "outline_candidates",
            "prompt": prompt,
            "prompt_sha256": prompt_sha256(prompt),
            "metadata": metadata,
            "metadata_sha256": canonical_json_sha256(metadata),
        },
    )
    raw_output_path = tmp_path / "raw_001.json"
    _write_json(
        raw_output_path,
        {
            "artifact_type": "outline_v2_subagent_raw_output",
            "artifact_version": "v1",
            "schema_version": REPLAY_RAW_OUTPUT_SCHEMA,
            "request_sha256": file_sha256(request_path),
            "subagent_run_id": "/root/outline-v2-call-001",
            "response": response,
        },
    )
    manifest_path = tmp_path / "outline_v2_subagent_response_manifest.json"
    _write_json(
        manifest_path,
        {
            "artifact_type": "outline_v2_subagent_response_manifest",
            "artifact_version": "v1",
            "schema_version": REPLAY_MANIFEST_SCHEMA,
            "status": "complete",
            "expected_call_count": 1,
            "entries": [
                {
                    "call_index": 1,
                    "route": "Outline_API",
                    "stage": "outline_candidates",
                    "prompt_sha256": prompt_sha256(prompt),
                    "metadata_sha256": canonical_json_sha256(metadata),
                    "request_path": str(request_path),
                    "request_sha256": file_sha256(request_path),
                    "raw_output_path": str(raw_output_path),
                    "raw_output_sha256": file_sha256(raw_output_path),
                    "subagent_run_id": "/root/outline-v2-call-001",
                }
            ],
        },
    )
    return manifest_path, prompt, metadata, response


def test_replay_caller_returns_only_exact_hash_bound_response(tmp_path: Path) -> None:
    manifest_path, prompt, metadata, expected_response = _replay_fixture(tmp_path)
    caller = OutlineV2ReplayCaller(manifest_path)

    response = caller("Outline_API", prompt, metadata)
    caller.assert_consumed()

    assert response == expected_response
    assert caller.expected_call_count == 1
    assert caller.consumed_call_count == 1
    assert caller.subagent_run_ids == ("/root/outline-v2-call-001",)
    assert [binding.artifact_kind for binding in caller.artifact_bindings] == [
        "request",
        "response",
    ]


def test_replay_caller_rejects_prompt_drift(tmp_path: Path) -> None:
    manifest_path, _prompt, metadata, _response = _replay_fixture(tmp_path)
    caller = OutlineV2ReplayCaller(manifest_path)

    with pytest.raises(OutlineV2ReplayError, match="prompt hash mismatch"):
        caller("Outline_API", "different prompt", metadata)


def test_replay_caller_rejects_mutated_response_file(tmp_path: Path) -> None:
    manifest_path, prompt, metadata, _response = _replay_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_output_path = Path(manifest["entries"][0]["raw_output_path"])
    raw_output_path.write_text("{}\n", encoding="utf-8")
    caller = OutlineV2ReplayCaller(manifest_path)

    with pytest.raises(OutlineV2ReplayError, match="response is missing or stale"):
        caller("Outline_API", prompt, metadata)


def test_replay_caller_fails_when_manifest_has_unused_responses(tmp_path: Path) -> None:
    manifest_path, _prompt, _metadata, _response = _replay_fixture(tmp_path)
    caller = OutlineV2ReplayCaller(manifest_path)

    with pytest.raises(OutlineV2ReplayError, match="unused model responses"):
        caller.assert_consumed()

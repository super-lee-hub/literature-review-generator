from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from runtime.f1_corpus import (
    F1CorpusManifestError,
    F1CorpusManifestV1,
    F1CorpusSourceRecordV1,
)
from runtime.release_acceptance import (
    ParentAcceptanceResultV2,
    ReleaseAcceptancePlanV2,
    ReleaseAcceptanceSpec,
    ReleaseAcceptanceSpecError,
)


def _write_f1_manifest(tmp_path: Path) -> tuple[Path, F1CorpusManifestV1]:
    source_root = tmp_path / "f1-papers"
    source_root.mkdir()
    sources: list[F1CorpusSourceRecordV1] = []
    for number in range(1, 16):
        source_id = f"F1-{number:02d}"
        relative_path = f"{source_id}.pdf"
        raw = f"%PDF-1.4\n{source_id}\n".encode("ascii")
        (source_root / relative_path).write_bytes(raw)
        sources.append(
            F1CorpusSourceRecordV1(
                source_id=source_id,
                relative_path=relative_path,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
            )
        )
    payload = {
        "artifact_type": "f1_corpus_manifest",
        "artifact_version": "v1",
        "schema_version": "f1-corpus-manifest-v1",
        "corpus_id": "field-f1-20260915",
        "source_root": source_root.name,
        "sources": [source.to_dict() for source in sources],
        "content_sha256": F1CorpusManifestV1.content_hash_for(
            "field-f1-20260915",
            sources,
        ),
    }
    manifest_path = tmp_path / "f1-corpus-manifest.json"
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return manifest_path, F1CorpusManifestV1.from_file(
        manifest_path,
        verify_source_files=True,
    )


def _write_f1_runtime_spec(
    tmp_path: Path,
    *,
    manifest_path: Path,
    manifest: F1CorpusManifestV1,
    gate: str,
    source_ids: list[str],
) -> Path:
    workspace = tmp_path / f"workspace-{gate.lower()}"
    workspace.mkdir()
    path = tmp_path / f"{gate.lower()}-runtime.json"
    path.write_text(
        json.dumps(
            {
                "project_name": f"acceptance-{gate.lower()}",
                "job_id": f"job-{gate.lower()}",
                "workspace_path": str(workspace),
                "source": {
                    "mode": "direct",
                    "pdf_folder": manifest.source_root,
                },
                "metadata": {
                    "f1_corpus_binding": {
                        "schema_version": "f1-corpus-binding-v1",
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": manifest.manifest_sha256,
                        "source_ids": source_ids,
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("gate", "source_ids"),
    (
        ("C", ["F1-01"]),
        ("D", ["F1-01", "F1-02", "F1-03"]),
        ("Q", [f"F1-{number:02d}" for number in range(1, 16)]),
    ),
)
def test_f1_plan_requires_the_runtime_bound_manifest_selection(
    tmp_path: Path,
    gate: str,
    source_ids: list[str],
) -> None:
    manifest_path, manifest = _write_f1_manifest(tmp_path)
    runtime_path = _write_f1_runtime_spec(
        tmp_path,
        manifest_path=manifest_path,
        manifest=manifest,
        gate=gate,
        source_ids=source_ids,
    )
    payload = {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": f"parent-{gate.lower()}",
        "budget": {
            "max_provider_calls_total": 10,
            "max_output_tokens_total": 1000,
            "max_retry_attempts_total": 1,
            "max_wall_seconds": 60,
        },
        "scenarios": {
            gate: {
                "scenario_id": gate,
                "gate": gate,
                "runtime_spec": str(runtime_path),
                "workspace": str(tmp_path / f"workspace-{gate.lower()}"),
                "job_id": f"job-{gate.lower()}",
                "input_manifest": str(manifest_path),
                "f1_source_ids": source_ids,
                "execution_mode": "runtime",
                "budget_domain": "live",
                "prerequisites": [],
            }
        },
    }

    plan = ReleaseAcceptancePlanV2.from_mapping(payload, origin_dir=tmp_path)

    assert plan.child(gate).f1_source_ids == tuple(source_ids)
    assert plan.child(gate).input_manifest == str(manifest_path)


def test_f1_manifest_rejects_duplicate_hashes_unsafe_paths_and_tampered_files(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_f1_manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["sources"][1]["sha256"] = payload["sources"][0]["sha256"]
    parsed_sources = tuple(
        F1CorpusSourceRecordV1.from_mapping(item) for item in payload["sources"]
    )
    payload["content_sha256"] = F1CorpusManifestV1.content_hash_for(
        payload["corpus_id"],
        parsed_sources,
    )
    with pytest.raises(F1CorpusManifestError, match="hashes must be unique"):
        F1CorpusManifestV1.from_mapping(payload, origin_dir=tmp_path)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["sources"][0]["relative_path"] = "../outside.pdf"
    with pytest.raises(F1CorpusManifestError, match="unsafe|relative"):
        F1CorpusManifestV1.from_mapping(payload, origin_dir=tmp_path)

    (Path(manifest.source_root) / "F1-01.pdf").write_bytes(b"%PDF-1.4\ntampered\n")
    with pytest.raises(F1CorpusManifestError, match="size|hash"):
        F1CorpusManifestV1.from_file(manifest_path, verify_source_files=True)


def test_f1_plan_rejects_a_manifest_selection_that_disagrees_with_runtime_spec(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_f1_manifest(tmp_path)
    runtime_path = _write_f1_runtime_spec(
        tmp_path,
        manifest_path=manifest_path,
        manifest=manifest,
        gate="C",
        source_ids=["F1-01"],
    )
    payload = {
        "schema_version": "release-acceptance-plan-v2",
        "parent_run_id": "parent-c",
        "budget": {"max_provider_calls_total": 1, "max_output_tokens_total": 1, "max_retry_attempts_total": 1, "max_wall_seconds": 1},
        "scenarios": {
            "C": {
                "scenario_id": "C",
                "gate": "C",
                "runtime_spec": str(runtime_path),
                "workspace": str(tmp_path / "workspace-c"),
                "job_id": "job-c",
                "input_manifest": str(manifest_path),
                "f1_source_ids": ["F1-02"],
                "execution_mode": "runtime",
                "budget_domain": "live",
                "prerequisites": [],
            }
        },
    }

    with pytest.raises(ReleaseAcceptanceSpecError, match="selection does not match"):
        ReleaseAcceptancePlanV2.from_mapping(payload, origin_dir=tmp_path)


def _receipt(gate: str, *, budget_domain: str = "live") -> dict[str, object]:
    return {
        "artifact_type": "scenario_execution_receipt",
        "artifact_version": "v1",
        "schema_version": "scenario-execution-receipt-v1",
        "parent_acceptance_run_id": "parent",
        "scenario_id": gate,
        "gate": gate,
        "final_executable_sha": "a" * 40,
        "plan_sha256": "b" * 64,
        "runtime_spec_sha256": "c" * 64,
        "input_identity_sha256": "d" * 64,
        "workspace_identity_sha256": "e" * 64,
        "executor_pid": 1,
        "executor_process_creation_identity": "test-process",
        "executor_host_id": "test-host",
        "started_at": "2026-09-15T00:00:00Z",
        "completed_at": "2026-09-15T00:00:01Z",
        "action_type": "test",
        "workspace": "workspace",
        "job_id": f"job-{gate.lower()}",
        "attempt_id": f"attempt-{gate.lower()}",
        "budget_domain": budget_domain,
        "status": "PASSED",
        "exit_status": 0,
        "produced_evidence_refs": [],
    }


def test_merge_readiness_cannot_omit_r_s_or_t(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.release_acceptance._receipt_has_live_authority",
        lambda _receipt: True,
    )
    required = ("C", "D", "E", "F", "G", "H", "I", "J", "K", "Q")
    children = {
        gate: {
            "status": "PASS_OFFLINE" if gate == "K" else "PASS",
            "receipt": _receipt(
                gate,
                budget_domain="offline-k" if gate == "K" else "live",
            ),
        }
        for gate in required
    }

    result = ParentAcceptanceResultV2.from_child_results(
        parent_acceptance_run_id="parent",
        final_executable_sha="a" * 40,
        child_results=children,
        required_scenarios=required,
    )

    assert result.status == "SCOPED_PASS"
    assert result.ready_to_merge is False


def test_live_acceptance_requires_an_explicit_budget() -> None:
    with pytest.raises(ReleaseAcceptanceSpecError, match="explicit budget"):
        ReleaseAcceptanceSpec.from_mapping({"gates": ["C"]})

    spec = ReleaseAcceptanceSpec.from_mapping(
        {
            "gates": ["C"],
            "budget": {
                "max_provider_calls_total": 1,
                "max_output_tokens_total": 1,
                "max_retry_attempts_total": 1,
                "max_wall_seconds": 1,
            },
        }
    )
    assert spec.budget.max_provider_calls_total == 1


@pytest.mark.optional
def test_acceptance_state_path_rejects_a_symlink_before_resolve(tmp_path: Path) -> None:
    target = tmp_path / "outside-state.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "state-link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink capability unavailable: {type(exc).__name__}")

    with pytest.raises(ReleaseAcceptanceSpecError, match="symlink or reparse"):
        ReleaseAcceptanceSpec.from_mapping({"state_path": str(link)})

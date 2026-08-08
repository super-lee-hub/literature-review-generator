from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    CurrentArtifactSetV1,
)
from validation.closure import _provider_closure_entry, zero_call_evidence_policy
from validation.disposition import ValidationDispositionV1


_STAGE_NAMES = {
    "analyze": "stage1_analyze",
    "outline": "stage2_outline",
    "review": "stage3_review",
    "validate": "stage4_validate",
}


@dataclass(frozen=True)
class _ZeroCallFixture:
    stage: str
    registry: ArtifactRegistry
    closure_record: ArtifactRecord
    terminal_record: ArtifactRecord
    terminal_payload: Mapping[str, Any]
    evidence_record: ArtifactRecord
    current_set: CurrentArtifactSetV1 | None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _register_json(
    registry: ArtifactRegistry,
    root: Path,
    *,
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    payload: Mapping[str, Any],
    depends_on: tuple[ArtifactDependencyRefV2, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ArtifactRecord:
    path = root / f"{artifact_id.replace(':', '_')}.json"
    _write_json(path, payload)
    return registry.register_file(
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=path,
        producer="tests.stage_specific_zero_call",
        depends_on=depends_on,
        artifact_id=artifact_id,
        metadata=metadata,
    )


def _register_anchor(
    registry: ArtifactRegistry,
    root: Path,
    artifact_id: str,
) -> ArtifactRecord:
    return _register_json(
        registry,
        root,
        artifact_id=artifact_id,
        artifact_type="zero_call_fixture_input",
        artifact_version="v1",
        payload={"artifact_id": artifact_id, "job_id": registry.job_id},
    )


def _register_analyze_support(
    registry: ArtifactRegistry,
    root: Path,
    *,
    closure_epoch_id: str,
    expected_call_graph_hash: str,
) -> tuple[ArtifactRecord, ArtifactRecord, ArtifactRecord]:
    source_bundle = _register_json(
        registry,
        root,
        artifact_id="source_bundle",
        artifact_type="source_bundle",
        artifact_version="v1",
        payload={
            "artifact_type": "source_bundle",
            "artifact_version": "v1",
            "job_id": registry.job_id,
            "paper_work_items": [],
        },
    )
    runtime_spec = _register_json(
        registry,
        root,
        artifact_id="runtime_job_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        payload={"job_id": registry.job_id, "action": "analyze"},
    )
    graph = _register_json(
        registry,
        root,
        artifact_id="stage1:provider_expected_call_graph",
        artifact_type="provider_expected_call_graph",
        artifact_version="v1",
        payload={
            "artifact_type": "provider_expected_call_graph",
            "artifact_version": "v1",
            "job_id": registry.job_id,
            "stage_name": "stage1_analyze",
            "attempt_id": "attempt-1",
            "closure_epoch_id": closure_epoch_id,
            "expected_call_graph_hash": expected_call_graph_hash,
            "source_bundle_hash": source_bundle.content_hash,
            "runtime_spec_hash": runtime_spec.content_hash,
            "expected_calls": [],
        },
        metadata={"expected_call_graph_hash": expected_call_graph_hash},
    )
    return source_bundle, runtime_spec, graph


def _register_evidence(
    registry: ArtifactRegistry,
    root: Path,
    *,
    evidence_stage: str,
    closure_epoch_id: str,
    analyze_source_bundle: ArtifactRecord | None,
) -> tuple[ArtifactRecord, CurrentArtifactSetV1 | None]:
    anchor = analyze_source_bundle or _register_anchor(
        registry,
        root,
        f"{evidence_stage}:evidence_input",
    )

    if evidence_stage == "analyze":
        record = _register_json(
            registry,
            root,
            artifact_id="summary_source_manifest",
            artifact_type="summary_source_manifest",
            artifact_version="v2",
            payload={
                "artifact_type": "summary_source_manifest",
                "artifact_version": "v2",
                "job_id": registry.job_id,
                "stage_name": "stage1_analyze",
                "closure_epoch_id": closure_epoch_id,
                "source_kind": "typed_import",
                "source_items": [],
                "rejected_candidates": [],
                "summary_count": 0,
            },
            depends_on=(ArtifactDependencyRefV2.from_record(anchor),),
        )
        return record, None

    if evidence_stage == "outline":
        plan_row = {
            "variant_name": "exact_replay_resume",
            "node_id": "relation_map",
            "call_id": "outline:relation_map",
            "provider": "fixture",
            "model": "fixture-model",
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_reasoning_tokens": 0,
            "estimated_cached_input_tokens": 0,
            "estimated_cache_write_tokens": 0,
            "estimated_total_tokens": 0,
            "estimated_cost": None,
            "pricing_source": "not_applicable",
            "pricing_policy": "explicit_only",
            "cost_status": "unknown",
            "assumptions": ["exact replay uses no provider transport"],
            "confidence": "high",
            "upper_bound": True,
            "transport_expected": False,
        }
        record = _register_json(
            registry,
            root,
            artifact_id="outline-v3:provider_call_plan:replay",
            artifact_type="outline_provider_call_plan",
            artifact_version="v1",
            payload={
                "artifact_type": "outline_provider_call_plan",
                "artifact_version": "v1",
                "job_id": registry.job_id,
                "stage_name": "outline_v3",
                "closure_epoch_id": closure_epoch_id,
                "provider_call_plan_hash": hash_json([plan_row]),
                "provider_call_plans": [plan_row],
                "pricing_policy": "explicit_only",
                "cost_status": "unknown",
                "estimated_cost": None,
            },
            depends_on=(ArtifactDependencyRefV2.from_record(anchor),),
        )
        return record, None

    if evidence_stage == "review":
        section = {"blocks": [{"text": "Verified replay section."}]}
        section_hash = hash_json(section)
        section_record = _register_json(
            registry,
            root,
            artifact_id="review-section:fixture",
            artifact_type="review_section",
            artifact_version="v3",
            payload={
                "artifact_type": "review_section",
                "artifact_version": "v3",
                "job_id": registry.job_id,
                "status": "ready",
                "section_id": "fixture",
                "binding_hash": "b" * 64,
                "binding": {"binding_version": "review-section-binding-v1"},
                "content_hash": section_hash,
                "section": section,
            },
            depends_on=(ArtifactDependencyRefV2.from_record(anchor),),
        )
        replay_path = root / "review_replay.jsonl"
        replay_path.write_text(
            json.dumps(
                {
                    "replay_version": "review-section-replay-v1",
                    "job_id": registry.job_id,
                    "stage_name": "stage3_review",
                    "closure_epoch_id": closure_epoch_id,
                    "section_id": "fixture",
                    "binding_hash": "b" * 64,
                    "artifact_id": section_record.artifact_id,
                    "artifact_path": section_record.path,
                    "artifact_content_hash": section_hash,
                    "registry_file_hash": section_record.content_hash,
                    "receipt_id": "receipt-fixture",
                    "normalized_output_hash": "n" * 64,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        record = registry.register_file(
            artifact_role="review_replay",
            artifact_type="review_replay_ledger",
            artifact_version="v1",
            path=replay_path,
            producer="tests.stage_specific_zero_call",
            depends_on=(ArtifactDependencyRefV2.from_record(section_record),),
            artifact_id="review_replay",
        )
        return record, None

    if evidence_stage != "validate":
        raise AssertionError(f"unsupported evidence stage: {evidence_stage}")

    draft = _register_anchor(registry, root, "review_draft")
    manifest = _register_anchor(registry, root, "citation_manifest_v3")
    docx = _register_anchor(registry, root, "review_docx")
    runtime_spec = registry.get("runtime_job_spec") or _register_anchor(
        registry,
        root,
        "runtime_job_spec",
    )
    disposition = ValidationDispositionV1.create(
        job_id=registry.job_id,
        stage_plan_hash="a" * 64,
        spec_hash=runtime_spec.content_hash,
        review_draft_artifact_id=draft.artifact_id,
        review_draft_artifact_hash=draft.content_hash,
        citation_manifest_artifact_id=manifest.artifact_id,
        citation_manifest_artifact_hash=manifest.content_hash,
        review_docx_artifact_id=docx.artifact_id,
        review_docx_artifact_hash=docx.content_hash,
        actor="tests.stage_specific_zero_call",
        reason="validation is explicitly not requested",
    )
    record = _register_json(
        registry,
        root,
        artifact_id="validation:not_requested",
        artifact_type="validation_disposition",
        artifact_version="v1",
        payload=disposition.to_dict(),
        depends_on=tuple(
            ArtifactDependencyRefV2.from_record(item)
            for item in (draft, manifest, docx, runtime_spec)
        ),
    )
    current_set = CurrentArtifactSetV1(
        set_id="current-set:fixture",
        job_id=registry.job_id,
        promotion_transaction_id="promotion:fixture",
        promotion_transaction_hash="1" * 64,
        review_draft_artifact_id=draft.artifact_id,
        review_draft_artifact_hash=draft.content_hash,
        citation_manifest_artifact_id=manifest.artifact_id,
        citation_manifest_artifact_hash=manifest.content_hash,
        review_docx_artifact_id=docx.artifact_id,
        review_docx_artifact_hash=docx.content_hash,
        validation_run_result_artifact_id="",
        validation_run_result_artifact_hash="",
        validation_receipt_closure_artifact_id="validation:closure:fixture",
        validation_receipt_closure_artifact_hash="2" * 64,
        validation_status="not_requested",
        validation_disposition_artifact_id=record.artifact_id,
        validation_disposition_artifact_hash=record.content_hash,
        actor="tests.stage_specific_zero_call",
        reason="fixture",
    )
    return record, current_set


def _build_fixture(
    tmp_path: Path,
    *,
    stage: str,
    evidence_stage: str | None = None,
) -> _ZeroCallFixture:
    evidence_stage = evidence_stage or stage
    root = tmp_path / f"{stage}-using-{evidence_stage}"
    root.mkdir(parents=True, exist_ok=True)
    registry = ArtifactRegistry(root / "artifact_registry.json", f"job-{stage}")
    closure_epoch_id = f"{stage}-epoch"
    expected_call_graph_hash = hash_json(
        {"stage": stage, "closure_epoch_id": closure_epoch_id, "expected_calls": []}
    )

    closure_dependencies: list[ArtifactDependencyRefV2] = []
    source_bundle: ArtifactRecord | None = None
    if stage == "analyze":
        source_bundle, runtime_spec, graph = _register_analyze_support(
            registry,
            root,
            closure_epoch_id=closure_epoch_id,
            expected_call_graph_hash=expected_call_graph_hash,
        )
        closure_dependencies.extend(
            ArtifactDependencyRefV2.from_record(item)
            for item in (source_bundle, runtime_spec, graph)
        )

    evidence_record, current_set = _register_evidence(
        registry,
        root,
        evidence_stage=evidence_stage,
        closure_epoch_id=closure_epoch_id,
        analyze_source_bundle=source_bundle,
    )
    closure_dependencies.append(ArtifactDependencyRefV2.from_record(evidence_record))

    closure_payload = {
        "closure_epoch_id": closure_epoch_id,
        "expected_call_ids": [],
        "observed_call_ids": [],
        "missing_call_ids": [],
        "stale_call_ids": [],
        "failed_call_ids": [],
        "incomplete_call_ids": [],
        "hash_mismatches": {},
        "unexpected_receipts": [],
        "retry_exceeded_call_ids": [],
        "usage_incomplete_call_ids": [],
        "complete": True,
        "closure_hash": hash_json(
            {"stage": stage, "closure_epoch_id": closure_epoch_id, "complete": True}
        ),
        "job_id": registry.job_id,
        "stage_name": _STAGE_NAMES[stage],
        "attempt_id": "attempt-1",
        "logical_attempt_identity": "attempt-1",
        "expected_call_graph_hash": expected_call_graph_hash,
        "expected_calls": [],
    }
    closure_record = _register_json(
        registry,
        root,
        artifact_id=f"{stage}:provider_receipt_closure",
        artifact_type="provider_receipt_closure",
        artifact_version="v1",
        payload={
            "artifact_type": "provider_receipt_closure",
            "artifact_version": "v1",
            "job_id": registry.job_id,
            "stage_name": _STAGE_NAMES[stage],
            "attempt_id": "attempt-1",
            "closure_epoch_id": closure_epoch_id,
            "expected_call_graph_hash": expected_call_graph_hash,
            "payload": closure_payload,
        },
        depends_on=tuple(closure_dependencies),
        metadata={
            "stage_name": _STAGE_NAMES[stage],
            "closure_epoch_id": closure_epoch_id,
            "expected_call_graph_hash": expected_call_graph_hash,
        },
    )
    terminal_record = ArtifactRecord(
        artifact_id=f"terminal:{stage}",
        artifact_role="terminal_stage_record",
        artifact_type="terminal_stage_record",
        artifact_version="v1",
        path=str(root / f"terminal-{stage}.json"),
        producer="tests.stage_specific_zero_call",
        job_id=registry.job_id,
        status="ready",
        content_hash="3" * 64,
    )
    terminal_payload = {
        "status": "succeeded",
        "stage_name": _STAGE_NAMES[stage],
        "model_call_count": 0,
        "output_artifact_refs": [
            {
                "artifact_id": closure_record.artifact_id,
                "artifact_type": closure_record.artifact_type,
                "content_hash": closure_record.content_hash,
                "job_id": registry.job_id,
            }
        ],
    }
    return _ZeroCallFixture(
        stage=stage,
        registry=registry,
        closure_record=closure_record,
        terminal_record=terminal_record,
        terminal_payload=terminal_payload,
        evidence_record=evidence_record,
        current_set=current_set,
    )


def _evaluate(fixture: _ZeroCallFixture) -> tuple[dict[str, Any], list[str]]:
    return _provider_closure_entry(
        fixture.stage,
        fixture.closure_record,
        fixture.terminal_record,
        fixture.terminal_payload,
        fixture.registry,
        current_set=fixture.current_set,
    )


def test_zero_call_evidence_policy_is_stage_specific() -> None:
    assert zero_call_evidence_policy("analyze") == (
        "summary_source_manifest",
        "stage1_summary_reuse_record",
    )
    assert zero_call_evidence_policy("outline") == (
        "outline_provider_call_plan",
        "outline_v3_model_call_replay",
    )
    assert zero_call_evidence_policy("review") == ("review_replay_ledger",)
    assert zero_call_evidence_policy("validate") == ("validation_disposition",)
    assert "outline_call_plan" not in zero_call_evidence_policy("outline")


@pytest.mark.parametrize("stage", ["analyze", "outline", "review", "validate"])
def test_provider_closure_accepts_stage_specific_zero_call_evidence(
    tmp_path: Path,
    stage: str,
) -> None:
    fixture = _build_fixture(tmp_path, stage=stage)

    entry, blocking = _evaluate(fixture)

    assert blocking == [], {"stage": stage, "entry": entry, "blocking": blocking}
    assert entry["complete"] is True
    assert entry["model_call_count"] == 0
    assert entry["expected_call_ids"] == []
    assert entry["observed_call_ids"] == []


@pytest.mark.parametrize(
    ("stage", "evidence_stage"),
    [
        ("outline", "analyze"),
        ("validate", "outline"),
        ("review", "validate"),
    ],
)
def test_provider_closure_rejects_cross_stage_zero_call_evidence(
    tmp_path: Path,
    stage: str,
    evidence_stage: str,
) -> None:
    fixture = _build_fixture(tmp_path, stage=stage, evidence_stage=evidence_stage)

    _entry, blocking = _evaluate(fixture)

    assert f"provider_closure_zero_call_source_evidence_missing:{stage}" in blocking


@pytest.mark.parametrize(
    ("field_name", "bad_value", "expected_issue"),
    [
        (
            "job_id",
            "other-job",
            "provider_closure_zero_call_outline_plan_job_mismatch:outline",
        ),
        (
            "stage_name",
            "stage3_review",
            "provider_closure_zero_call_outline_plan_stage_mismatch:outline",
        ),
    ],
)
def test_provider_closure_rejects_wrong_evidence_job_or_stage(
    tmp_path: Path,
    field_name: str,
    bad_value: str,
    expected_issue: str,
) -> None:
    fixture = _build_fixture(tmp_path, stage="outline")
    payload = json.loads(Path(fixture.evidence_record.path).read_text(encoding="utf-8"))
    payload[field_name] = bad_value
    _write_json(Path(fixture.evidence_record.path), payload)

    _entry, blocking = _evaluate(fixture)

    assert any(issue.startswith(expected_issue) for issue in blocking), blocking


def test_provider_closure_rejects_wrong_evidence_version(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, stage="outline")
    registry_payload = json.loads(
        Path(fixture.registry.registry_path).read_text(encoding="utf-8")
    )
    evidence_entry = next(
        item
        for item in registry_payload["artifacts"]
        if item["artifact_id"] == fixture.evidence_record.artifact_id
    )
    evidence_entry["artifact_version"] = "v999"
    Path(fixture.registry.registry_path).write_text(
        json.dumps(registry_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reloaded = ArtifactRegistry(fixture.registry.registry_path, fixture.registry.job_id)
    reloaded_fixture = replace(
        fixture,
        registry=reloaded,
        closure_record=reloaded.get(fixture.closure_record.artifact_id)
        or fixture.closure_record,
        evidence_record=reloaded.get(fixture.evidence_record.artifact_id)
        or fixture.evidence_record,
    )

    _entry, blocking = _evaluate(reloaded_fixture)

    assert any(
        issue.startswith("provider_closure_zero_call_source_evidence_version_invalid:outline")
        for issue in blocking
    ), blocking


def test_provider_closure_rejects_tampered_zero_call_evidence_bytes(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path, stage="outline")
    Path(fixture.evidence_record.path).write_text("{}", encoding="utf-8")

    _entry, blocking = _evaluate(fixture)

    assert any(
        issue.startswith("provider_closure_zero_call_source_evidence_untrusted:outline")
        for issue in blocking
    ), blocking


def test_provider_closure_rejects_missing_zero_call_evidence_dependency(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path, stage="outline")
    without_evidence = replace(
        fixture.closure_record,
        depends_on=[
            dependency
            for dependency in fixture.closure_record.depends_on
            if dependency.artifact_id != fixture.evidence_record.artifact_id
        ],
    )

    _entry, blocking = _evaluate(replace(fixture, closure_record=without_evidence))

    assert "provider_closure_zero_call_source_evidence_missing:outline" in blocking

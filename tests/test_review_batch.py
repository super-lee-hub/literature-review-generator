from __future__ import annotations

import csv
import json
import multiprocessing
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import services.review_batch as review_batch_module

from summary_schema import normalize_ai_summary
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    UnverifiedDependency,
    file_sha256,
)
from services.job_workspace import JobWorkspace
from services.review_batch import (
    ParentSummaryIntegrityError,
    ReviewBatchError,
    ReviewBatchDerivationResultV1,
    ReviewBatchSpecV1,
    ReviewVariantDerivationResultV1,
    ReviewVariantSpecV1,
    SummarySelectionError,
    SummarySelectionSpecV1,
    derive_review_batch,
    load_review_batch_spec,
)
from runtime.orchestrator import AgentRuntimeBridge
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.reconcile import ReconcileValidationError, RuntimeReconciler
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError


def _summaries(count: int = 61) -> list[dict]:
    return [
        {
            "status": "success",
            "paper_info": {
                "title": f"Paper {index:03d}",
                "authors": [f"Author {index:03d}"],
                "year": "2026",
                "canonical_paper_key": f"paper-{index:03d}",
                "source_paper_id": f"source-paper-{index:03d}",
            },
            "ai_summary": normalize_ai_summary(
                {
                    "schema_version": "summary_v2_lite",
                    "routing": {
                        "paper_type": "empirical",
                        "classification_status": "resolved",
                        "route_confidence": "high",
                    },
                    "core_analysis": {
                        "summary": f"Contribution {index:03d}",
                        "methodology": "controlled study",
                        "findings": f"Finding {index:03d}",
                        "conclusions": f"Conclusion {index:03d}",
                    },
                }
            ),
        }
        for index in range(1, count + 1)
    ]


def _write_classification(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_key", "ABC", "A", "AB"])
        writer.writeheader()
        for index in range(1, 62):
            writer.writerow(
                {
                    "paper_key": f"paper-{index:03d}",
                    "ABC": "1",
                    "A": "1" if index <= 20 else "0",
                    "AB": "1" if index <= 45 else "0",
                }
            )


def _current_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.ini"
    shutil.copyfile(Path(__file__).parents[1] / "config.ini.example", path)
    return path


def _register_parent_paper_artifact(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    summary: dict,
    *,
    artifact_suffix: str = "",
) -> tuple[ArtifactRecord, ArtifactRecord]:
    paper_info = dict(summary["paper_info"])
    canonical_key = str(paper_info["canonical_paper_key"])
    evidence_text = f"source-grounded evidence for {canonical_key}"
    evidence_dir = Path(workspace.artifact_path(f"evidence/{canonical_key}{artifact_suffix}"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_paths = {
        "normalized_text": evidence_dir / "normalized.md",
        "chunks": evidence_dir / "chunks.json",
        "page_index": evidence_dir / "page_index.json",
    }
    evidence_paths["normalized_text"].write_text(evidence_text, encoding="utf-8")
    evidence_paths["chunks"].write_text(
        json.dumps([{"chunk_id": "c1", "text": evidence_text}]),
        encoding="utf-8",
    )
    evidence_paths["page_index"].write_text(
        json.dumps([{"page_number": 1, "text": evidence_text}]),
        encoding="utf-8",
    )
    evidence_records = [
        registry.register_file(
            artifact_role="paper_evidence",
            artifact_type=artifact_type,
            artifact_version="v1",
            path=path,
            producer="test",
            artifact_id=f"{artifact_type}:{canonical_key}{artifact_suffix}",
        )
        for artifact_type, path in evidence_paths.items()
    ]
    evidence_dependencies = [
        ArtifactDependencyRefV2(
            dependency_kind="local_job",
            job_id=workspace.job_id,
            artifact_id=record.artifact_id,
            artifact_type=record.artifact_type,
            path=record.path,
            content_hash=record.content_hash,
        )
        for record in evidence_records
    ]
    manifest_path = evidence_dir / "evidence_manifest_v1.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_type": "evidence_manifest",
                "artifact_version": "v1",
                "job_id": workspace.job_id,
                "canonical_paper_key": canonical_key,
                "artifacts": [
                    {
                        "artifact_type": record.artifact_type,
                        "path": record.path,
                        "content_hash": record.content_hash,
                    }
                    for record in evidence_records
                ],
                "created_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    manifest_record = registry.register_file(
        artifact_role="paper_evidence",
        artifact_type="evidence_manifest",
        artifact_version="v1",
        path=manifest_path,
        producer="test",
        artifact_id=f"evidence_manifest:{canonical_key}{artifact_suffix}",
        depends_on=evidence_dependencies,
    )
    paper_path = Path(
        workspace.artifact_path(f"paper_artifacts/{canonical_key}{artifact_suffix}.json")
    )
    paper_path.parent.mkdir(parents=True, exist_ok=True)
    paper_path.write_text(
        json.dumps(
            {
                "artifact_type": "paper_artifact",
                "artifact_version": "v1",
                "created_from_job_id": workspace.job_id,
                "created_at": "2026-01-01T00:00:00Z",
                "paper_identity": {
                    "canonical_paper_key": canonical_key,
                    "source_paper_id": f"source-{canonical_key}",
                    "paper_key_aliases": [canonical_key],
                },
                "source": {"source_mode": "direct"},
                "paper_info": paper_info,
                "analysis": {
                    "status": "success",
                    "preprocess": {
                        "markdown_path": str(evidence_paths["normalized_text"]),
                        "chunks_path": str(evidence_paths["chunks"]),
                        "page_index_path": str(evidence_paths["page_index"]),
                    },
                    "ai_summary": summary.get("ai_summary"),
                },
                "stage1_inputs": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paper_record = registry.register_file(
        artifact_role="paper_artifact",
        artifact_type="paper_artifact",
        artifact_version="v1",
        path=paper_path,
        producer="test",
        artifact_id=f"paper:{canonical_key}{artifact_suffix}",
        depends_on=[
            *evidence_dependencies,
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id=workspace.job_id,
                artifact_id=manifest_record.artifact_id,
                artifact_type=manifest_record.artifact_type,
                path=manifest_record.path,
                content_hash=manifest_record.content_hash,
            ),
        ],
    )
    return paper_record, evidence_records[0]


def _register_parent(
    tmp_path: Path,
    summaries: list[dict],
    *,
    paper_keys: tuple[str, ...] | None = None,
    include_paper_artifacts: bool = True,
) -> tuple[Path, Path]:
    workspace = JobWorkspace(str(tmp_path / "parent-output"), "parent", "parent-job")
    parent = Path(workspace.artifact_path("parent_summaries.json"))
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent,
        producer="test",
        artifact_id="parent-summary",
    )
    if include_paper_artifacts:
        allowed_keys = set(paper_keys) if paper_keys is not None else None
        registered_keys: set[str] = set()
        for summary in summaries:
            paper_info = summary.get("paper_info") or {}
            canonical_key = str(paper_info.get("canonical_paper_key") or "")
            if (
                not canonical_key
                or canonical_key in registered_keys
                or (allowed_keys is not None and canonical_key not in allowed_keys)
            ):
                continue
            _register_parent_paper_artifact(workspace, registry, summary)
            registered_keys.add(canonical_key)
    return parent, Path(workspace.paths.registry_path)


def _selection(
    *,
    parent: Path,
    parent_registry: Path,
    classification: Path,
    column: str,
    count: int,
) -> SummarySelectionSpecV1:
    return SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(parent_registry),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent),
        parent_summary_path=str(parent),
        ordered_paper_keys=tuple(f"paper-{index:03d}" for index in range(1, count + 1)),
        classification_file=str(classification),
        classification_file_hash=file_sha256(classification),
        identity_column="paper_key",
        classification_column=column,
        value_filter="1",
        expected_count=count,
        duplicate_policy="error",
    )


def _direct_selection(
    *,
    parent: Path,
    parent_registry: Path,
    keys: tuple[str, ...],
) -> SummarySelectionSpecV1:
    return SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(parent_registry),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent),
        parent_summary_path=str(parent),
        ordered_paper_keys=keys,
        expected_count=len(keys),
    )


def _derive_one(
    *,
    parent: Path,
    parent_registry: Path,
    workspace: JobWorkspace,
    canonical_key: str = "paper-001",
) -> ReviewVariantDerivationResultV1:
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="child",
            selection=SummarySelectionSpecV1(
                parent_job_id="parent-job",
                parent_registry_path=str(parent_registry),
                parent_artifact_id="parent-summary",
                parent_content_hash=file_sha256(parent),
                parent_summary_path=str(parent),
                ordered_paper_keys=(canonical_key,),
                expected_count=1,
            ),
        ),
        workspace=workspace,
        registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
    )
    assert isinstance(result, ReviewVariantDerivationResultV1)
    return result


def _assert_no_child_derivation_outputs(workspace: JobWorkspace) -> None:
    assert not Path(workspace.artifact_path("summary_selection_v1.json")).exists()
    assert not Path(workspace.artifact_path("child_summaries.json")).exists()
    assert not Path(workspace.artifact_path("paper_artifacts")).exists()
    assert ArtifactRegistry(workspace.paths.registry_path, workspace.job_id).list_records() == []


def _hold_review_batch_derivation(
    spec_payload,
    base_output_dir,
    project_name,
    job_id,
    derivation_id,
    entered,
    release,
    result_queue,
) -> None:
    spec = ReviewBatchSpecV1.from_dict(spec_payload)
    workspace = JobWorkspace(base_output_dir, project_name, job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    original = review_batch_module.derive_review_variant

    def blocked_derive(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=30):
            raise TimeoutError("timed out waiting to release review batch child")
        return original(*args, **kwargs)

    review_batch_module.derive_review_variant = blocked_derive
    try:
        result = derive_review_batch(
            spec,
            workspace=workspace,
            registry=registry,
            derivation_id=derivation_id,
        )
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        assert isinstance(result, ReviewBatchDerivationResultV1)
        result_queue.put(("ok", result.derivation_id, str(result.success)))


def test_abc_a_ab_batches_share_parent_and_never_call_stage1_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries())
    classification = tmp_path / "classification.csv"
    _write_classification(classification)

    def _forbidden_provider(*_args, **_kwargs):
        pytest.fail("child review batch crossed the Stage 1 provider boundary")

    monkeypatch.setattr(AgentRuntimeBridge, "persist_stage1_results", _forbidden_provider)
    workspace = JobWorkspace(
        str(tmp_path / "output"),
        "review-batch-coordinator",
        "review-batch-job",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="review-batch-coordinator",
            batch_label="ABC-A-AB",
            variants=tuple(
                ReviewVariantSpecV1(
                    variant_id=label,
                    project_name=label,
                    child_job_id=f"child-{label}",
                    selection=_selection(
                        parent=parent,
                        parent_registry=parent_registry,
                        classification=classification,
                        column=label,
                        count=count,
                    ),
                )
                for label, count in (("ABC", 61), ("A", 20), ("AB", 45))
            ),
        ),
        workspace=workspace,
        registry=registry,
    )

    assert isinstance(result, ReviewBatchDerivationResultV1)
    assert result.success is True
    assert result.stage1_model_calls == 0
    assert [item.selected_count for item in result.variant_results] == [61, 20, 45]
    assert {item.parent_summary_hash for item in result.variant_results} == {
        file_sha256(parent)
    }
    assert len({item.selection_hash for item in result.variant_results}) == 3

    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["batch_id"] == result.batch_id
    assert manifest["coordinator_job_id"] == workspace.job_id
    assert manifest["parent"]["content_hash"] == file_sha256(parent)
    assert manifest["status"] == "completed"
    assert manifest["completed_variant_count"] == 3
    assert manifest["failed_variant_count"] == 0
    assert manifest["stage1_model_calls"] == 0
    assert [item["status"] for item in manifest["variants"]] == [
        "completed",
        "completed",
        "completed",
    ]

    registries = {"parent-job": ArtifactRegistry(parent_registry, "parent-job")}
    for variant, expected_count in zip(manifest["variants"], (61, 20, 45)):
        assert variant["selected_count"] == expected_count
        assert variant["stage1_model_calls"] == 0
        assert Path(variant["child_workspace_path"]).is_dir()
        child_registry = ArtifactRegistry(
            variant["child_registry_path"],
            variant["child_job_id"],
        )
        registries[variant["child_job_id"]] = child_registry
        summary_link = variant["output_artifacts"]["summary"]
        selection_link = variant["output_artifacts"]["selection"]
        summary_record = child_registry.get(summary_link["artifact_id"])
        selection_record = child_registry.get(selection_link["artifact_id"])
        assert summary_record is not None and summary_record.status == "ready"
        assert selection_record is not None and selection_record.status == "ready"
        assert summary_link["content_hash"] == file_sha256(summary_link["path"])
        assert selection_link["content_hash"] == file_sha256(selection_link["path"])
        payload = json.loads(Path(summary_link["path"]).read_text(encoding="utf-8"))
        assert len(payload) == expected_count
        external = [
            dep
            for dep in summary_record.depends_on
            if dep.dependency_kind == "external_job"
        ]
        assert len(external) == 1
        assert external[0].job_id == "parent-job"
        assert external[0].artifact_id == "parent-summary"
        assert external[0].content_hash == file_sha256(parent)

    RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    ).validate_record(result.manifest_artifact)


def test_batch_coordinator_persists_failed_variant_status_and_valid_manifest(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    parent_hash = file_sha256(parent)

    def selection(key: str) -> SummarySelectionSpecV1:
        return SummarySelectionSpecV1(
            parent_job_id="parent-job",
            parent_registry_path=str(parent_registry_path),
            parent_artifact_id="parent-summary",
            parent_content_hash=parent_hash,
            parent_summary_path=str(parent),
            ordered_paper_keys=(key,),
            expected_count=1,
        )

    workspace = JobWorkspace(
        str(tmp_path / "output"),
        "review-batch-coordinator",
        "review-batch-job",
    )
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="review-batch-coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="valid",
                    project_name="valid-child",
                    child_job_id="valid-child-job",
                    selection=selection("paper-001"),
                ),
                ReviewVariantSpecV1(
                    variant_id="missing",
                    project_name="missing-child",
                    child_job_id="missing-child-job",
                    selection=selection("paper-999"),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )

    assert isinstance(result, ReviewBatchDerivationResultV1)
    assert result.success is False
    assert set(result.failed_variants) == {"missing"}
    assert result.manifest_artifact.status == "ready"
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "needs_review"
    assert manifest["completed_variant_count"] == 1
    assert manifest["failed_variant_count"] == 1
    assert manifest["variants"][1]["status"] == "failed"
    assert manifest["variants"][1]["output_artifacts"] == {}
    assert "SummarySelectionError" in manifest["variants"][1]["failure_reason"]

    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "valid-child-job": ArtifactRegistry(
            manifest["variants"][0]["child_registry_path"],
            "valid-child-job",
        ),
    }
    RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    ).validate_record(result.manifest_artifact)


def test_review_batch_child_owner_blocks_cross_coordinator_overwrite(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(2))
    output_root = tmp_path / "output"

    def run_batch(coordinator: str, key: str) -> tuple[ReviewBatchDerivationResultV1, JobWorkspace, ArtifactRegistry]:
        workspace = JobWorkspace(str(output_root), coordinator, f"{coordinator}-job")
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        result = derive_review_batch(
            ReviewBatchSpecV1(
                project_name=coordinator,
                variants=(
                    ReviewVariantSpecV1(
                        variant_id="shared",
                        project_name="shared-child",
                        child_job_id="shared-child-job",
                        selection=_direct_selection(
                            parent=parent,
                            parent_registry=parent_registry_path,
                            keys=(key,),
                        ),
                    ),
                ),
            ),
            workspace=workspace,
            registry=registry,
        )
        assert isinstance(result, ReviewBatchDerivationResultV1)
        return result, workspace, registry

    first, first_workspace, first_registry = run_batch("coordinator-one", "paper-001")
    first_summary = first.variant_results[0].summary_artifact
    first_summary_hash = file_sha256(first_summary.path)
    second, _second_workspace, _second_registry = run_batch("coordinator-two", "paper-002")

    assert first.success is True
    assert second.success is False
    assert "owned by another review batch variant" in second.failed_variants["shared"]
    assert file_sha256(first_summary.path) == first_summary_hash
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "shared-child-job": ArtifactRegistry(
            str(
                Path(first.variant_results[0].selection_artifact.path).parents[1]
                / "artifact_registry.json"
            ),
            "shared-child-job",
        ),
    }
    RuntimeReconciler(
        first_workspace,
        first_registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    ).validate_record(first.manifest_artifact)


def test_review_batch_manifest_rejects_child_workspace_outside_coordinator_root(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="only",
                    project_name="only-child",
                    child_job_id="only-child-job",
                    selection=_direct_selection(
                        parent=parent,
                        parent_registry=parent_registry_path,
                        keys=("paper-001",),
                    ),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )
    assert isinstance(result, ReviewBatchDerivationResultV1)
    payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    forged_derivation_id = "f" * 24
    forged_child = tmp_path / "outside" / "only-child__only-child-job"
    payload["derivation_id"] = forged_derivation_id
    payload["variants"][0]["child_workspace_path"] = str(forged_child)
    payload["variants"][0]["child_registry_path"] = str(
        forged_child / "artifact_registry.json"
    )
    forged_path = Path(
        workspace.artifact_path(
            f"review_batch_manifests/{forged_derivation_id}.json"
        )
    )
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    metadata = dict(result.manifest_artifact.metadata)
    metadata["derivation_id"] = forged_derivation_id
    child_registry = ArtifactRegistry(
        Path(result.variant_results[0].summary_artifact.path).parents[1]
        / "artifact_registry.json",
        "only-child-job",
    )
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "only-child-job": child_registry,
    }
    forged_record = registry.register_file(
        artifact_role="review_batch_manifest",
        artifact_type="review_batch_manifest",
        artifact_version="v1",
        path=forged_path,
        producer="tests",
        artifact_id=f"{result.batch_id}:{forged_derivation_id}",
        depends_on=result.manifest_artifact.depends_on,
        external_registry_resolver=lambda job_id: registries.get(job_id),
        metadata=metadata,
    )

    with pytest.raises(ReconcileValidationError, match="outside the coordinator output root"):
        RuntimeReconciler(
            workspace,
            registry,
            external_registry_resolver=lambda job_id: registries.get(job_id),
        ).validate_record(forged_record)


def test_review_batch_manifest_rejects_foreign_coordinator_workspace(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="only",
                    project_name="only-child",
                    child_job_id="only-child-job",
                    selection=_direct_selection(
                        parent=parent,
                        parent_registry=parent_registry_path,
                        keys=("paper-001",),
                    ),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )
    assert isinstance(result, ReviewBatchDerivationResultV1)
    foreign_path = (
        tmp_path
        / "foreign-output"
        / "coordinator__coordinator-job"
        / "artifacts"
        / "review_batch_manifests"
        / Path(result.manifest_path).name
    )
    foreign_path.parent.mkdir(parents=True)
    foreign_path.write_bytes(Path(result.manifest_path).read_bytes())
    forged_record = replace(
        result.manifest_artifact,
        path=str(foreign_path),
        content_hash=file_sha256(foreign_path),
    )
    child_registry = ArtifactRegistry(
        Path(result.variant_results[0].summary_artifact.path).parents[1]
        / "artifact_registry.json",
        "only-child-job",
    )
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "only-child-job": child_registry,
    }
    reconciler = RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    )

    with pytest.raises(
        ReconcileValidationError,
        match="active Registry workspace",
    ):
        reconciler.validate_record(forged_record)

    registry.register_file(
        artifact_role=forged_record.artifact_role,
        artifact_type=forged_record.artifact_type,
        artifact_version=forged_record.artifact_version,
        path=foreign_path,
        producer="test",
        artifact_id=forged_record.artifact_id,
        depends_on=forged_record.depends_on,
        external_registry_resolver=lambda job_id: registries.get(job_id),
        metadata=forged_record.metadata,
    )
    assert AgentRuntimeRunner._review_batch_registry_paths(registry) == ()


def test_review_batch_manifest_rejects_mutable_projection_for_derivation_identity(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="only",
                    project_name="only-child",
                    child_job_id="only-child-job",
                    selection=_direct_selection(
                        parent=parent,
                        parent_registry=parent_registry_path,
                        keys=("paper-001",),
                    ),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )
    assert isinstance(result, ReviewBatchDerivationResultV1)
    projection_path = Path(result.projection_path)
    forged_record = replace(
        result.manifest_artifact,
        path=str(projection_path),
        content_hash=file_sha256(projection_path),
    )
    child_registry = ArtifactRegistry(
        Path(result.variant_results[0].summary_artifact.path).parents[1]
        / "artifact_registry.json",
        "only-child-job",
    )
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "only-child-job": child_registry,
    }

    with pytest.raises(
        ReconcileValidationError,
        match="immutable derivation path",
    ):
        RuntimeReconciler(
            workspace,
            registry,
            external_registry_resolver=lambda job_id: registries.get(job_id),
        ).validate_record(forged_record)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_review_batch_manifest_rejects_junction_outside_coordinator_workspace(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="only",
                    project_name="only-child",
                    child_job_id="only-child-job",
                    selection=_direct_selection(
                        parent=parent,
                        parent_registry=parent_registry_path,
                        keys=("paper-001",),
                    ),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )
    assert isinstance(result, ReviewBatchDerivationResultV1)
    manifest_path = Path(result.manifest_path)
    manifest_dir = manifest_path.parent
    external_dir = tmp_path / "foreign-manifests"
    external_dir.mkdir()
    shutil.move(str(manifest_path), str(external_dir / manifest_path.name))
    for internal_file in manifest_dir.iterdir():
        internal_file.unlink()
    manifest_dir.rmdir()
    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(manifest_dir), str(external_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"cannot create test junction: {junction.stderr or junction.stdout}")

    child_registry = ArtifactRegistry(
        Path(result.variant_results[0].summary_artifact.path).parents[1]
        / "artifact_registry.json",
        "only-child-job",
    )
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "only-child-job": child_registry,
    }
    try:
        with pytest.raises(ReconcileValidationError, match="reparse point"):
            RuntimeReconciler(
                workspace,
                registry,
                external_registry_resolver=lambda job_id: registries.get(job_id),
            ).validate_record(result.manifest_artifact)
        assert AgentRuntimeRunner._review_batch_registry_paths(registry) == ()
    finally:
        manifest_dir.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_review_batch_manifest_rejects_child_artifacts_junction(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="only",
                    project_name="only-child",
                    child_job_id="only-child-job",
                    selection=_direct_selection(
                        parent=parent,
                        parent_registry=parent_registry_path,
                        keys=("paper-001",),
                    ),
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )
    assert isinstance(result, ReviewBatchDerivationResultV1)
    child_workspace = Path(result.variant_results[0].summary_artifact.path).parents[1]
    child_artifacts = child_workspace / "artifacts"
    external_artifacts = tmp_path / "foreign-child-artifacts"
    shutil.move(str(child_artifacts), str(external_artifacts))
    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(child_artifacts), str(external_artifacts)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"cannot create test junction: {junction.stderr or junction.stdout}")

    child_registry = ArtifactRegistry(
        child_workspace / "artifact_registry.json",
        "only-child-job",
    )
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        "only-child-job": child_registry,
    }
    try:
        with pytest.raises(ReconcileValidationError, match="reparse point"):
            RuntimeReconciler(
                workspace,
                registry,
                external_registry_resolver=lambda job_id: registries.get(job_id),
            ).validate_record(result.manifest_artifact)
        assert AgentRuntimeRunner._review_batch_registry_paths(registry) == ()
    finally:
        child_artifacts.rmdir()


def test_review_batch_retry_reuses_deterministic_children_and_keeps_manifests_immutable(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="valid",
                project_name="valid-child",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
            ReviewVariantSpecV1(
                variant_id="missing",
                project_name="missing-child",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-999",),
                ),
            ),
        ),
    )

    first = derive_review_batch(batch, workspace=workspace, registry=registry)
    assert isinstance(first, ReviewBatchDerivationResultV1)
    first_hash = file_sha256(first.manifest_path)
    second = derive_review_batch(batch, workspace=workspace, registry=registry)
    assert isinstance(second, ReviewBatchDerivationResultV1)

    assert first.success is second.success is False
    assert first.derivation_id != second.derivation_id
    assert first.manifest_path != second.manifest_path
    assert first.manifest_artifact.artifact_id != second.manifest_artifact.artifact_id
    assert file_sha256(first.manifest_path) == first_hash
    first_payload = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
    second_payload = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
    assert [item["child_job_id"] for item in first_payload["variants"]] == [
        item["child_job_id"] for item in second_payload["variants"]
    ]
    assert len(list((tmp_path / "output").glob("valid-child__review-*"))) == 1
    assert len(list((tmp_path / "output").glob("missing-child__review-*"))) == 1

    valid_job_id = first_payload["variants"][0]["child_job_id"]
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        valid_job_id: ArtifactRegistry(
            first_payload["variants"][0]["child_registry_path"],
            valid_job_id,
        ),
    }
    reconciler = RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    )
    reconciler.validate_record(first.manifest_artifact)
    reconciler.validate_record(second.manifest_artifact)


def test_review_batch_rejects_reused_derivation_identity_before_manifest_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )

    first = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="attempt-one",
    )
    assert isinstance(first, ReviewBatchDerivationResultV1)
    first_hash = file_sha256(first.manifest_path)
    child_workspace_calls: list[str] = []
    original_create_child_workspace = review_batch_module._create_child_workspace

    def counted_create_child_workspace(**kwargs):
        child_workspace_calls.append(str(kwargs["child_job_id"]))
        return original_create_child_workspace(**kwargs)

    monkeypatch.setattr(
        review_batch_module,
        "_create_child_workspace",
        counted_create_child_workspace,
    )

    with pytest.raises(ReviewBatchError, match="derivation identity already exists"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="attempt-one",
        )

    assert file_sha256(first.manifest_path) == first_hash
    assert registry.get(first.manifest_artifact.artifact_id) == first.manifest_artifact
    assert child_workspace_calls == []


def test_review_batch_same_derivation_has_one_service_level_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    entered_child = threading.Event()
    release_child = threading.Event()
    child_calls: list[str] = []
    original = review_batch_module.derive_review_variant

    def blocked_derive(*args, **kwargs):
        child_calls.append("called")
        entered_child.set()
        assert release_child.wait(timeout=20)
        return original(*args, **kwargs)

    monkeypatch.setattr(review_batch_module, "derive_review_variant", blocked_derive)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(
            derive_review_batch,
            batch,
            workspace=workspace,
            registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
            derivation_id="shared-attempt",
        )
        assert entered_child.wait(timeout=20)
        child_registry_path = (
            tmp_path
            / "output"
            / "only-child__only-child-job"
            / "artifact_registry.json"
        )
        child_registry_before = child_registry_path.read_bytes()
        try:
            with pytest.raises(ReviewBatchError, match="derivation is already active"):
                derive_review_batch(
                    batch,
                    workspace=workspace,
                    registry=ArtifactRegistry(
                        workspace.paths.registry_path,
                        workspace.job_id,
                    ),
                    derivation_id="shared-attempt",
                )
            assert child_registry_path.read_bytes() == child_registry_before
            assert child_calls == ["called"]
        finally:
            release_child.set()
        first = first_future.result(timeout=30)

    assert isinstance(first, ReviewBatchDerivationResultV1)
    assert first.success is True


def test_review_batch_serializes_different_derivation_projection_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    entered_child = threading.Event()
    release_child = threading.Event()
    original = review_batch_module.derive_review_variant
    child_calls: list[str] = []

    def blocked_derive(*args, **kwargs):
        child_calls.append("called")
        entered_child.set()
        assert release_child.wait(timeout=20)
        return original(*args, **kwargs)

    monkeypatch.setattr(review_batch_module, "derive_review_variant", blocked_derive)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(
            derive_review_batch,
            batch,
            workspace=workspace,
            registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
            derivation_id="projection-a",
        )
        assert entered_child.wait(timeout=20)
        try:
            with pytest.raises(ReviewBatchError, match="another review batch derivation"):
                derive_review_batch(
                    batch,
                    workspace=workspace,
                    registry=ArtifactRegistry(
                        workspace.paths.registry_path,
                        workspace.job_id,
                    ),
                    derivation_id="projection-b",
                )
            assert child_calls == ["called"]
        finally:
            release_child.set()
        first = first_future.result(timeout=30)
    assert isinstance(first, ReviewBatchDerivationResultV1)

    monkeypatch.setattr(review_batch_module, "derive_review_variant", original)
    second = derive_review_batch(
        batch,
        workspace=workspace,
        registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
        derivation_id="projection-b",
    )
    assert isinstance(second, ReviewBatchDerivationResultV1)
    projection = json.loads(
        Path(second.projection_path).read_text(encoding="utf-8")
    )
    assert projection["derivation_id"] == second.derivation_id
    receipts = list(
        Path(workspace.artifact_path("review_batch_manifests")).glob(
            ".*.projection.receipt"
        )
    )
    assert len(receipts) == 2


def test_review_batch_same_derivation_has_one_cross_process_writer(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_review_batch_derivation,
        args=(
            batch.to_dict(),
            workspace.base_output_dir,
            workspace.project_name,
            workspace.job_id,
            "cross-process-window",
            entered,
            release,
            result_queue,
        ),
    )
    process.start()
    try:
        assert entered.wait(timeout=30)
        child_registry_path = (
            tmp_path
            / "output"
            / "only-child__only-child-job"
            / "artifact_registry.json"
        )
        child_registry_before = child_registry_path.read_bytes()
        with pytest.raises(ReviewBatchError, match="derivation is already active"):
            derive_review_batch(
                batch,
                workspace=workspace,
                registry=ArtifactRegistry(
                    workspace.paths.registry_path,
                    workspace.job_id,
                ),
                derivation_id="cross-process-window",
            )
        assert child_registry_path.read_bytes() == child_registry_before
    finally:
        release.set()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0
    assert result_queue.get(timeout=10)[0] == "ok"


def test_review_batch_adopts_orphan_manifest_after_registry_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_register = registry.register_file
    crashed = False

    def crash_before_manifest_registry(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("artifact_type") == "review_batch_manifest" and not crashed:
            crashed = True
            raise SystemExit(91)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register_file", crash_before_manifest_registry)
    with pytest.raises(SystemExit, match="91"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="crash-window",
        )

    manifests = list(
        Path(workspace.artifact_path("review_batch_manifests")).glob("*.json")
    )
    assert len(manifests) == 1
    orphan_path = manifests[0]
    orphan_hash = file_sha256(orphan_path)
    registry.reload()
    assert all(
        record.artifact_type != "review_batch_manifest"
        for record in registry.list_records()
    )

    monkeypatch.setattr(registry, "register_file", original_register)

    def forbidden_child_retry(*_args, **_kwargs):
        pytest.fail("orphan manifest adoption reran a child derivation")

    monkeypatch.setattr(
        review_batch_module,
        "derive_review_variant",
        forbidden_child_retry,
    )
    adopted = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="crash-window",
    )

    assert isinstance(adopted, ReviewBatchDerivationResultV1)
    assert Path(adopted.manifest_path) == orphan_path
    assert file_sha256(orphan_path) == orphan_hash
    assert adopted.manifest_artifact == registry.get(adopted.manifest_artifact.artifact_id)
    assert Path(adopted.projection_path).is_file()


def test_review_batch_rejects_conflicting_registry_record_during_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_register = registry.register_file
    crashed = False

    def crash_before_manifest_registry(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("artifact_type") == "review_batch_manifest" and not crashed:
            crashed = True
            raise SystemExit(94)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register_file", crash_before_manifest_registry)
    with pytest.raises(SystemExit, match="94"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="conflicting-record-window",
        )
    monkeypatch.setattr(registry, "register_file", original_register)

    orphan_path = next(
        Path(workspace.artifact_path("review_batch_manifests")).glob("*.json")
    )
    orphan_payload = json.loads(orphan_path.read_text(encoding="utf-8"))
    manifest_artifact_id = (
        f"{orphan_payload['batch_id']}:{orphan_payload['derivation_id']}"
    )
    wrong_path = Path(workspace.artifact_path("wrong_identity.json"))
    wrong_path.write_text('{"wrong": true}', encoding="utf-8")
    wrong_record = registry.register_file(
        artifact_role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=wrong_path,
        producer="test",
        artifact_id=manifest_artifact_id,
    )

    def forbidden_child_retry(*_args, **_kwargs):
        pytest.fail("conflicting manifest recovery reran a child derivation")

    monkeypatch.setattr(
        review_batch_module,
        "derive_review_variant",
        forbidden_child_retry,
    )
    with pytest.raises(ReviewBatchError, match="identity conflicts"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="conflicting-record-window",
        )

    assert registry.get(manifest_artifact_id) == wrong_record
    assert not Path(workspace.artifact_path("review_batch_manifest.json")).exists()


def test_review_batch_recovers_projection_after_registered_manifest_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    baseline = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="baseline-window",
    )
    assert isinstance(baseline, ReviewBatchDerivationResultV1)
    original_atomic_write = review_batch_module.atomic_write_json
    crashed = False

    def crash_before_projection(path, payload):
        nonlocal crashed
        if Path(path).name == "review_batch_manifest.json" and not crashed:
            crashed = True
            raise SystemExit(92)
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        crash_before_projection,
    )
    with pytest.raises(SystemExit, match="92"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="projection-window",
        )

    registry.reload()
    manifest_records = [
        record
        for record in registry.list_records()
        if record.artifact_type == "review_batch_manifest"
    ]
    assert len(manifest_records) == 2
    manifest_record = next(
        record
        for record in manifest_records
        if record.artifact_id != baseline.manifest_artifact.artifact_id
    )
    manifest_hash = file_sha256(manifest_record.path)
    projection_path = Path(workspace.artifact_path("review_batch_manifest.json"))
    assert json.loads(projection_path.read_text(encoding="utf-8"))["derivation_id"] == (
        baseline.derivation_id
    )

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        original_atomic_write,
    )

    def forbidden_child_retry(*_args, **_kwargs):
        pytest.fail("projection recovery reran a child derivation")

    monkeypatch.setattr(
        review_batch_module,
        "derive_review_variant",
        forbidden_child_retry,
    )
    recovered = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="projection-window",
    )

    assert isinstance(recovered, ReviewBatchDerivationResultV1)
    assert recovered.manifest_artifact == manifest_record
    assert file_sha256(recovered.manifest_path) == manifest_hash
    assert projection_path.is_file()
    assert json.loads(projection_path.read_text(encoding="utf-8"))["derivation_id"] == (
        recovered.derivation_id
    )
    projection_path.unlink()
    with pytest.raises(ReviewBatchError, match="derivation identity already exists"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="projection-window",
        )
    assert json.loads(projection_path.read_text(encoding="utf-8"))["derivation_id"] == (
        recovered.derivation_id
    )


def test_review_batch_old_recovery_cannot_regress_newer_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_atomic_write = review_batch_module.atomic_write_json
    crashed = False

    def crash_before_old_projection(path, payload):
        nonlocal crashed
        if Path(path).name == "review_batch_manifest.json" and not crashed:
            crashed = True
            raise SystemExit(97)
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        crash_before_old_projection,
    )
    with pytest.raises(SystemExit, match="97"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="older-projection-window",
        )

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        original_atomic_write,
    )
    crashed_newer = False

    def crash_before_newer_projection(path, payload):
        nonlocal crashed_newer
        if Path(path).name == "review_batch_manifest.json" and not crashed_newer:
            crashed_newer = True
            raise SystemExit(98)
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        crash_before_newer_projection,
    )
    with pytest.raises(SystemExit, match="98"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="newer-projection-window",
        )
    projection_path = Path(workspace.artifact_path("review_batch_manifest.json"))
    assert not projection_path.exists()

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        original_atomic_write,
    )

    def forbidden_child_retry(*_args, **_kwargs):
        pytest.fail("superseded projection recovery reran a child derivation")

    monkeypatch.setattr(
        review_batch_module,
        "derive_review_variant",
        forbidden_child_retry,
    )
    recovered = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="older-projection-window",
    )
    projection_after_old_recovery = projection_path.read_bytes()
    newer = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="newer-projection-window",
    )

    assert isinstance(recovered, ReviewBatchDerivationResultV1)
    assert isinstance(newer, ReviewBatchDerivationResultV1)
    older_payload = json.loads(Path(recovered.manifest_path).read_text(encoding="utf-8"))
    newer_payload = json.loads(Path(newer.manifest_path).read_text(encoding="utf-8"))
    assert older_payload["projection_generation"] < newer_payload["projection_generation"]
    assert projection_path.read_bytes() == projection_after_old_recovery
    assert json.loads(projection_after_old_recovery)["derivation_id"] == newer.derivation_id
    receipt_path = Path(
        workspace.artifact_path(
            f"review_batch_manifests/.{recovered.derivation_id}.projection.receipt"
        )
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["projection_status"] == "superseded"
    assert receipt["projection_generation"] == older_payload["projection_generation"]


def test_review_batch_recovers_receipt_after_projection_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_atomic_write = review_batch_module.atomic_write_json
    crashed = False

    def crash_before_receipt(path, payload):
        nonlocal crashed
        if str(path).endswith(".projection.receipt") and not crashed:
            crashed = True
            raise SystemExit(96)
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        crash_before_receipt,
    )
    with pytest.raises(SystemExit, match="96"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="receipt-window",
        )

    projection_path = Path(workspace.artifact_path("review_batch_manifest.json"))
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    receipt_path = next(
        Path(workspace.artifact_path("review_batch_manifests")).glob(
            ".*.projection.receipt"
        ),
        None,
    )
    assert projection["derivation_id"]
    assert receipt_path is None
    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        original_atomic_write,
    )

    def forbidden_child_retry(*_args, **_kwargs):
        pytest.fail("receipt recovery reran a child derivation")

    monkeypatch.setattr(
        review_batch_module,
        "derive_review_variant",
        forbidden_child_retry,
    )
    recovered = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="receipt-window",
    )

    assert isinstance(recovered, ReviewBatchDerivationResultV1)
    receipts = list(
        Path(workspace.artifact_path("review_batch_manifests")).glob(
            ".*.projection.receipt"
        )
    )
    assert len(receipts) == 1


def test_review_batch_resumes_child_prefix_after_selection_registry_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_register = ArtifactRegistry.register_file
    crashed = False

    def crash_selection_register(self, *args, **kwargs):
        nonlocal crashed
        if (
            self.job_id == "only-child-job"
            and kwargs.get("artifact_type") == "summary_selection"
            and not crashed
        ):
            crashed = True
            raise SystemExit(93)
        return original_register(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "register_file", crash_selection_register)
    with pytest.raises(SystemExit, match="93"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="child-prefix-window",
        )

    selection_path = (
        tmp_path
        / "output"
        / "only-child__only-child-job"
        / "artifacts"
        / "summary_selection_v1.json"
    )
    assert selection_path.is_file()
    selection_hash = file_sha256(selection_path)
    monkeypatch.setattr(ArtifactRegistry, "register_file", original_register)
    original_atomic_write = review_batch_module.atomic_write_json

    def forbid_selection_overwrite(path, payload):
        if Path(path) == selection_path:
            pytest.fail("child recovery overwrote the durable selection prefix")
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        forbid_selection_overwrite,
    )
    recovered = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="child-prefix-window",
    )

    assert isinstance(recovered, ReviewBatchDerivationResultV1)
    assert recovered.success is True
    assert file_sha256(selection_path) == selection_hash


def test_review_batch_adopts_child_owner_after_owner_registry_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    original_register = ArtifactRegistry.register_file
    crashed = False

    def crash_owner_register(self, *args, **kwargs):
        nonlocal crashed
        if (
            self.job_id == "only-child-job"
            and kwargs.get("artifact_type") == "review_batch_child_owner"
            and not crashed
        ):
            crashed = True
            raise SystemExit(95)
        return original_register(self, *args, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "register_file", crash_owner_register)
    with pytest.raises(SystemExit, match="95"):
        derive_review_batch(
            batch,
            workspace=workspace,
            registry=registry,
            derivation_id="owner-window",
        )

    owner_path = (
        tmp_path
        / "output"
        / "only-child__only-child-job"
        / "artifacts"
        / "review_batch_child_owner_v1.json"
    )
    assert owner_path.is_file()
    owner_hash = file_sha256(owner_path)
    monkeypatch.setattr(ArtifactRegistry, "register_file", original_register)
    original_atomic_write = review_batch_module.atomic_write_json

    def forbid_owner_overwrite(path, payload):
        if Path(path) == owner_path:
            pytest.fail("child owner recovery overwrote its durable contract")
        return original_atomic_write(path, payload)

    monkeypatch.setattr(
        review_batch_module,
        "atomic_write_json",
        forbid_owner_overwrite,
    )
    recovered = derive_review_batch(
        batch,
        workspace=workspace,
        registry=registry,
        derivation_id="owner-window",
    )

    assert isinstance(recovered, ReviewBatchDerivationResultV1)
    assert recovered.success is True
    assert file_sha256(owner_path) == owner_hash


@pytest.mark.parametrize("child_job_id", ("parent-job", "coordinator-job"))
def test_review_batch_rejects_reserved_child_job_ids_before_writes(
    tmp_path: Path,
    child_job_id: str,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id=child_job_id,
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )

    with pytest.raises(SummarySelectionError, match="conflict with reserved jobs"):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert not Path(tmp_path / "output" / f"only-child__{child_job_id}").exists()
    assert registry.list_records() == []


def test_review_batch_rejects_parent_job_equal_to_coordinator_before_writes(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    selection = SummarySelectionSpecV1(
        parent_job_id=workspace.job_id,
        parent_registry_path=str(tmp_path / "parent-registry.json"),
        parent_artifact_id="parent-summary",
        parent_content_hash="a" * 64,
        parent_summary_path=str(tmp_path / "parent.json"),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                selection=selection,
            ),
        ),
    )

    with pytest.raises(SummarySelectionError, match="must differ from the coordinator"):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert not list((tmp_path / "output").glob("only-child__*"))
    assert registry.list_records() == []


def test_review_batch_rejects_child_workspace_alias_with_coordinator_before_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coord__alias", "job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coord__alias",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="coord",
                child_job_id="alias__job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )

    with pytest.raises(SummarySelectionError, match="aliases the coordinator workspace"):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert not Path(workspace.root_dir).exists()
    assert not Path(workspace.paths.registry_path).exists()
    assert registry.list_records() == []


def test_review_batch_rejects_parent_workspace_alias_with_coordinator_before_writes(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    parent_path = Path(workspace.artifact_path("parent_summaries.json"))
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text(json.dumps(_summaries(1), ensure_ascii=False), encoding="utf-8")
    parent_registry = ArtifactRegistry(workspace.paths.registry_path, "parent-job")
    parent_registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="test",
        artifact_id="parent-summary",
    )
    registry_before = Path(workspace.paths.registry_path).read_bytes()
    selection = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(workspace.paths.registry_path),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent_path),
        parent_summary_path=str(parent_path),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=selection,
            ),
        ),
    )

    with pytest.raises(
        SummarySelectionError,
        match="parent workspace aliases the coordinator workspace",
    ):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert Path(workspace.paths.registry_path).read_bytes() == registry_before
    assert not list((tmp_path / "output").glob("only-child__*"))


def test_review_batch_rejects_child_workspace_alias_with_parent_before_writes(
    tmp_path: Path,
) -> None:
    summaries = _summaries(1)
    parent_workspace = JobWorkspace(str(tmp_path / "output"), "parent__alias", "job")
    parent_path = Path(parent_workspace.artifact_path("parent_summaries.json"))
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
    parent_registry = ArtifactRegistry(
        parent_workspace.paths.registry_path,
        parent_workspace.job_id,
    )
    parent_registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent_path,
        producer="test",
        artifact_id="parent-summary",
    )
    _register_parent_paper_artifact(parent_workspace, parent_registry, summaries[0])

    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    selection = SummarySelectionSpecV1(
        parent_job_id="job",
        parent_registry_path=str(parent_workspace.paths.registry_path),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent_path),
        parent_summary_path=str(parent_path),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="parent",
                child_job_id="alias__job",
                selection=selection,
            ),
        ),
    )

    with pytest.raises(SummarySelectionError, match="aliases the parent workspace"):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert not Path(workspace.root_dir).exists()
    assert registry.list_records() == []


def test_review_batch_rejects_aliased_child_workspaces_before_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    workspace = JobWorkspace(str(tmp_path / "output"), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    selection = _direct_selection(
        parent=parent,
        parent_registry=parent_registry_path,
        keys=("paper-001",),
    )
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="first",
                project_name="child__alias",
                child_job_id="job",
                selection=selection,
            ),
            ReviewVariantSpecV1(
                variant_id="second",
                project_name="child",
                child_job_id="alias__job",
                selection=selection,
            ),
        ),
    )

    with pytest.raises(SummarySelectionError, match="workspace paths must be unique"):
        derive_review_batch(batch, workspace=workspace, registry=registry)

    assert not Path(workspace.root_dir).exists()
    assert not list((tmp_path / "output").glob("child*"))
    assert not Path(workspace.paths.registry_path).exists()
    assert registry.list_records() == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_review_batch_rejects_child_junction_before_any_batch_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    output_root = tmp_path / "output"
    output_root.mkdir()
    external_child = tmp_path / "external-child"
    external_child.mkdir()
    junction_path = output_root / "evil-child__evil-job"
    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_path), str(external_child)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"cannot create test junction: {junction.stderr or junction.stdout}")

    workspace = JobWorkspace(str(output_root), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    selection = _direct_selection(
        parent=parent,
        parent_registry=parent_registry_path,
        keys=("paper-001",),
    )
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="good",
                project_name="good-child",
                child_job_id="good-job",
                selection=selection,
            ),
            ReviewVariantSpecV1(
                variant_id="evil",
                project_name="evil-child",
                child_job_id="evil-job",
                selection=selection,
            ),
        ),
    )
    try:
        with pytest.raises(SummarySelectionError, match="reparse point"):
            derive_review_batch(batch, workspace=workspace, registry=registry)
        assert not Path(workspace.root_dir).exists()
        assert not (output_root / "good-child__good-job").exists()
        assert list(external_child.iterdir()) == []
        assert registry.list_records() == []
    finally:
        junction_path.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_review_batch_rejects_child_artifacts_junction_before_any_batch_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    output_root = tmp_path / "output"
    child_root = output_root / "evil-child__evil-job"
    child_root.mkdir(parents=True)
    external_artifacts = tmp_path / "external-child-artifacts"
    external_artifacts.mkdir()
    child_artifacts = child_root / "artifacts"
    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(child_artifacts), str(external_artifacts)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"cannot create test junction: {junction.stderr or junction.stdout}")

    workspace = JobWorkspace(str(output_root), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="evil-child",
                child_job_id="evil-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    try:
        with pytest.raises(SummarySelectionError, match="reparse point"):
            derive_review_batch(batch, workspace=workspace, registry=registry)
        assert list(external_artifacts.iterdir()) == []
        assert not Path(workspace.root_dir).exists()
        assert not Path(workspace.paths.registry_path).exists()
    finally:
        child_artifacts.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_review_batch_rejects_manifest_directory_junction_before_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    output_root = tmp_path / "output"
    workspace = JobWorkspace(str(output_root), "coordinator", "coordinator-job")
    artifacts_dir = Path(workspace.paths.artifacts_dir)
    artifacts_dir.mkdir(parents=True)
    external_manifests = tmp_path / "external-manifests"
    external_manifests.mkdir()
    manifest_dir = artifacts_dir / "review_batch_manifests"
    junction = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(manifest_dir), str(external_manifests)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"cannot create test junction: {junction.stderr or junction.stdout}")

    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    batch = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="only-child",
                child_job_id="only-child-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry_path,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    try:
        with pytest.raises(SummarySelectionError, match="reparse point"):
            derive_review_batch(batch, workspace=workspace, registry=registry)
        assert list(external_manifests.iterdir()) == []
        assert not (output_root / "only-child__only-child-job").exists()
        assert not Path(workspace.paths.registry_path).exists()
    finally:
        manifest_dir.rmdir()


def test_review_batch_persists_partial_manifest_when_child_registry_is_corrupt(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    output_root = tmp_path / "output"
    corrupt_workspace = JobWorkspace(str(output_root), "corrupt-child", "corrupt-child-job")
    corrupt_workspace.ensure_exists()
    Path(corrupt_workspace.paths.registry_path).write_text("{not json", encoding="utf-8")
    workspace = JobWorkspace(str(output_root), "coordinator", "coordinator-job")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    selection = _direct_selection(
        parent=parent,
        parent_registry=parent_registry_path,
        keys=("paper-001",),
    )

    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="coordinator",
            variants=(
                ReviewVariantSpecV1(
                    variant_id="valid",
                    project_name="valid-child",
                    child_job_id="valid-child-job",
                    selection=selection,
                ),
                ReviewVariantSpecV1(
                    variant_id="corrupt",
                    project_name="corrupt-child",
                    child_job_id="corrupt-child-job",
                    selection=selection,
                ),
            ),
        ),
        workspace=workspace,
        registry=registry,
    )

    assert isinstance(result, ReviewBatchDerivationResultV1)
    assert result.success is False
    assert "RegistryCorruption" in result.failed_variants["corrupt"]
    payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert payload["completed_variant_count"] == 1
    assert payload["failed_variant_count"] == 1
    assert payload["variants"][1]["status"] == "failed"
    valid_job_id = payload["variants"][0]["child_job_id"]
    registries = {
        "parent-job": ArtifactRegistry(parent_registry_path, "parent-job"),
        valid_job_id: ArtifactRegistry(
            payload["variants"][0]["child_registry_path"],
            valid_job_id,
        ),
    }
    RuntimeReconciler(
        workspace,
        registry,
        external_registry_resolver=lambda job_id: registries.get(job_id),
    ).validate_record(result.manifest_artifact)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_name", "../escaped"),
        ("project_name", "..\\escaped"),
        ("child_job_id", "../escaped"),
        ("child_job_id", "..\\escaped"),
        ("child_job_id", "C:\\escaped"),
    ),
)
def test_review_variant_rejects_unsafe_workspace_components(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    selection = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(tmp_path / "registry.json"),
        parent_artifact_id="parent-summary",
        parent_content_hash="a" * 64,
        parent_summary_path=str(tmp_path / "summary.json"),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    kwargs = {
        "variant_id": "variant",
        "project_name": "child",
        "child_job_id": "child-job",
        "selection": selection,
    }
    kwargs[field] = value

    with pytest.raises(SummarySelectionError, match="safe single path segment"):
        ReviewVariantSpecV1(**kwargs)


def test_review_batch_metadata_is_detached_from_mutable_inputs(tmp_path: Path) -> None:
    selection = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(tmp_path / "registry.json"),
        parent_artifact_id="parent-summary",
        parent_content_hash="a" * 64,
        parent_summary_path=str(tmp_path / "summary.json"),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    variant_metadata = {"labels": ["original"]}
    batch_metadata = {"nested": {"mode": "original"}}
    spec = ReviewBatchSpecV1(
        project_name="coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="variant",
                project_name="child",
                selection=selection,
                metadata=variant_metadata,
            ),
        ),
        metadata=batch_metadata,
    )
    original_id = spec.batch_id

    variant_metadata["labels"].append("mutated")
    batch_metadata["nested"]["mode"] = "mutated"
    payload = spec.to_dict()

    assert spec.batch_id == original_id
    assert payload["metadata"] == {"nested": {"mode": "original"}}
    assert payload["variants"][0]["metadata"] == {"labels": ["original"]}
    assert ReviewBatchSpecV1.from_dict(payload).batch_id == original_id


def test_batch_derivation_fails_closed_for_parent_hash_change(tmp_path: Path) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(1))
    selection = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(parent_registry),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent),
        parent_summary_path=str(parent),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    parent.write_text("[]", encoding="utf-8")
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="content hash changed"):
        derive_review_batch(
            ReviewBatchSpecV1(project_name="child", selection=selection),
            workspace=workspace,
            registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
        )


@pytest.mark.parametrize("divergence", ("ai_summary", "source_identity"))
def test_batch_derivation_fails_closed_for_divergent_summary_paper_lineage(
    tmp_path: Path,
    divergence: str,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    summaries = json.loads(parent.read_text(encoding="utf-8"))
    if divergence == "ai_summary":
        summaries[0]["ai_summary"] = normalize_ai_summary(
            {
                "schema_version": "summary_v2_lite",
                "routing": {
                    "paper_type": "empirical",
                    "classification_status": "resolved",
                    "route_confidence": "high",
                },
                "core_analysis": {
                    "summary": "Divergent but canonical analysis.",
                    "methodology": "controlled study",
                    "findings": "Divergent finding.",
                    "conclusions": "Divergent conclusion.",
                },
            }
        )
    else:
        summaries[0]["paper_info"]["source_paper_id"] = "divergent-source-id"
    parent.write_text(json.dumps(summaries), encoding="utf-8")
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    parent_record = parent_registry.get("parent-summary")
    assert parent_record is not None
    parent_registry.register_file(
        artifact_role=parent_record.artifact_role,
        artifact_type=parent_record.artifact_type,
        artifact_version=parent_record.artifact_version,
        path=parent,
        producer="test-divergent-reregistration",
        artifact_id=parent_record.artifact_id,
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="summary/paper artifact lineage mismatch"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_with_zero_parent_paper_artifacts(tmp_path: Path) -> None:
    parent, parent_registry = _register_parent(
        tmp_path,
        _summaries(1),
        include_paper_artifacts=False,
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="parent paper artifact selection failed"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_when_selected_parent_paper_is_missing(tmp_path: Path) -> None:
    parent, parent_registry = _register_parent(
        tmp_path,
        _summaries(2),
        paper_keys=("paper-001",),
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match=r'"missing": \["paper-002"\]'):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry,
            workspace=workspace,
            canonical_key="paper-002",
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_for_duplicate_parent_paper_artifacts(tmp_path: Path) -> None:
    summaries = _summaries(1)
    parent, parent_registry_path = _register_parent(tmp_path, summaries)
    parent_workspace = JobWorkspace(str(tmp_path / "parent-output"), "parent", "parent-job")
    parent_registry = ArtifactRegistry(parent_registry_path, parent_workspace.job_id)
    _register_parent_paper_artifact(
        parent_workspace,
        parent_registry,
        summaries[0],
        artifact_suffix="-duplicate",
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match=r'"duplicate": \{"paper-001": 2\}'):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_for_malformed_parent_paper_artifact(tmp_path: Path) -> None:
    summaries = _summaries(1)
    parent, parent_registry_path = _register_parent(tmp_path, summaries)
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    paper_record = next(
        record for record in parent_registry.list_records() if record.artifact_type == "paper_artifact"
    )
    Path(paper_record.path).write_text(
        json.dumps(
            {
                "artifact_type": "paper_artifact",
                "artifact_version": "v1",
                "created_from_job_id": "parent-job",
                "paper_identity": {
                    "canonical_paper_key": "paper-001",
                    "source_paper_id": "source-paper-001",
                },
            }
        ),
        encoding="utf-8",
    )
    parent_registry.register_file(
        artifact_role=paper_record.artifact_role,
        artifact_type=paper_record.artifact_type,
        artifact_version=paper_record.artifact_version,
        path=paper_record.path,
        producer="test",
        artifact_id=paper_record.artifact_id,
        depends_on=paper_record.depends_on,
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="parent paper artifact is invalid"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_for_parent_paper_hash_change(tmp_path: Path) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    paper_record = next(
        record for record in parent_registry.list_records() if record.artifact_type == "paper_artifact"
    )
    payload = json.loads(Path(paper_record.path).read_text(encoding="utf-8"))
    payload["tampered"] = True
    Path(paper_record.path).write_text(json.dumps(payload), encoding="utf-8")
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="content hash mismatch"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_without_parent_evidence_dependencies(tmp_path: Path) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    paper_record = next(
        record for record in parent_registry.list_records() if record.artifact_type == "paper_artifact"
    )
    parent_registry.register_file(
        artifact_role=paper_record.artifact_role,
        artifact_type=paper_record.artifact_type,
        artifact_version=paper_record.artifact_version,
        path=paper_record.path,
        producer="test",
        artifact_id=paper_record.artifact_id,
        depends_on=[],
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="has no evidence dependencies"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_rejects_valid_but_non_evidence_parent_dependency(tmp_path: Path) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    paper_record = next(
        record for record in parent_registry.list_records() if record.artifact_type == "paper_artifact"
    )
    unrelated_path = tmp_path / "unrelated.json"
    unrelated_path.write_text(json.dumps({"kind": "not-paper-evidence"}), encoding="utf-8")
    unrelated_record = parent_registry.register_file(
        artifact_role="unrelated",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=unrelated_path,
        producer="test",
        artifact_id="unrelated-valid-artifact",
    )
    parent_registry.register_file(
        artifact_role=paper_record.artifact_role,
        artifact_type=paper_record.artifact_type,
        artifact_version=paper_record.artifact_version,
        path=paper_record.path,
        producer="test",
        artifact_id=paper_record.artifact_id,
        depends_on=[
            ArtifactDependencyRefV2(
                dependency_kind="local_job",
                job_id="parent-job",
                artifact_id=unrelated_record.artifact_id,
                artifact_type=unrelated_record.artifact_type,
                path=unrelated_record.path,
                content_hash=unrelated_record.content_hash,
            )
        ],
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="evidence dependency count is invalid"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


def test_batch_derivation_fails_closed_for_unresolvable_parent_evidence_dependency(
    tmp_path: Path,
) -> None:
    parent, parent_registry_path = _register_parent(tmp_path, _summaries(1))
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    paper_record = next(
        record for record in parent_registry.list_records() if record.artifact_type == "paper_artifact"
    )
    with pytest.raises(UnverifiedDependency, match="dependency is not registered"):
        parent_registry.register_file(
            artifact_role=paper_record.artifact_role,
            artifact_type=paper_record.artifact_type,
            artifact_version=paper_record.artifact_version,
            path=paper_record.path,
            producer="test",
            artifact_id=paper_record.artifact_id,
            depends_on=[
                ArtifactDependencyRefV2(
                    dependency_kind="local_job",
                    job_id="parent-job",
                    artifact_id="missing-evidence",
                    artifact_type="normalized_text",
                    path=str(tmp_path / "missing.md"),
                    content_hash="0" * 64,
                )
            ],
        )

    assert parent_registry.get(paper_record.artifact_id) == paper_record


def test_child_paper_projection_keeps_parent_evidence_hash_dependencies(tmp_path: Path) -> None:
    summaries = _summaries(1)
    parent, parent_registry_path = _register_parent(
        tmp_path,
        summaries,
        include_paper_artifacts=False,
    )
    parent_registry = ArtifactRegistry(parent_registry_path, "parent-job")
    parent_workspace = JobWorkspace(
        str(tmp_path / "parent-output"),
        "parent",
        "parent-job",
    )
    parent_paper_record, evidence_record = _register_parent_paper_artifact(
        parent_workspace,
        parent_registry,
        summaries[0],
    )
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")
    result = derive_review_batch(
        ReviewBatchSpecV1(
            project_name="child",
            selection=SummarySelectionSpecV1(
                parent_job_id="parent-job",
                parent_registry_path=str(parent_registry_path),
                parent_artifact_id="parent-summary",
                parent_content_hash=file_sha256(parent),
                parent_summary_path=str(parent),
                ordered_paper_keys=("paper-001",),
                expected_count=1,
            ),
        ),
        workspace=workspace,
        registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
    )

    assert isinstance(result, ReviewVariantDerivationResultV1)
    assert len(result.paper_artifacts) == 1
    projected_payload = json.loads(Path(result.paper_artifacts[0].path).read_text(encoding="utf-8"))
    assert projected_payload["created_from_job_id"] == workspace.job_id
    assert projected_payload["projected_from"] == {
        "job_id": "parent-job",
        "artifact_id": parent_paper_record.artifact_id,
        "content_hash": parent_paper_record.content_hash,
    }
    dependencies = result.paper_artifacts[0].depends_on
    assert {(item.job_id, item.artifact_id, item.content_hash) for item in dependencies} >= {
        ("parent-job", parent_paper_record.artifact_id, parent_paper_record.content_hash),
        ("parent-job", evidence_record.artifact_id, evidence_record.content_hash),
    }


def test_selection_rejects_duplicates_missing_and_ambiguous_records(tmp_path: Path) -> None:
    summaries = _summaries(1)
    summaries.append(
        {
            "status": "success",
            "paper_info": {
                "title": "Ambiguous duplicate",
                "authors": ["Different Author"],
                "year": "2025",
                "doi": "10.1000/different",
                "canonical_paper_key": "paper-001",
            },
        }
    )
    parent, parent_registry = _register_parent(tmp_path, summaries)
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    duplicate = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(parent_registry),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent),
        parent_summary_path=str(parent),
        ordered_paper_keys=("paper-001", "paper-001"),
        expected_count=2,
    )
    with pytest.raises(SummarySelectionError, match="duplicate paper keys"):
        derive_review_batch(
            ReviewBatchSpecV1(project_name="child", selection=duplicate),
            workspace=workspace,
            registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
        )

    ambiguous = SummarySelectionSpecV1(
        parent_job_id="parent-job",
        parent_registry_path=str(parent_registry),
        parent_artifact_id="parent-summary",
        parent_content_hash=file_sha256(parent),
        parent_summary_path=str(parent),
        ordered_paper_keys=("paper-001",),
        expected_count=1,
    )
    with pytest.raises(SummarySelectionError, match="ambiguous"):
        derive_review_batch(
            ReviewBatchSpecV1(project_name="child", selection=ambiguous),
            workspace=workspace,
            registry=ArtifactRegistry(workspace.paths.registry_path, workspace.job_id),
        )


def test_review_batch_spec_resolves_paths_from_spec_directory(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    parent = spec_dir / "parent.json"
    parent.write_text(json.dumps(_summaries(1)), encoding="utf-8")
    parent_registry = ArtifactRegistry(str(spec_dir / "parent_registry.json"), "parent-job")
    parent_registry.register_file(
        artifact_role="summary",
        artifact_type="summary_file",
        artifact_version="v1",
        path=parent,
        producer="test",
        artifact_id="parent-summary",
    )
    payload = {
        "schema_version": "review-batch-v1",
        "project_name": "relative",
        "selection": {
            "schema_version": "summary-selection-v1",
            "parent_job_id": "parent-job",
            "parent_registry_path": "parent_registry.json",
            "parent_artifact_id": "parent-summary",
            "parent_content_hash": file_sha256(parent),
            "parent_summary_path": "parent.json",
            "ordered_paper_keys": ["paper-001"],
            "expected_count": 1,
            "duplicate_policy": "error",
        },
    }
    spec_path = spec_dir / "batch.json"
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    spec = load_review_batch_spec(spec_path)

    assert spec.selection is not None
    assert spec.selection.parent_summary_path == str(parent.resolve())


def test_runtime_bridge_exposes_downstream_only_batch_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(2))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    bridge = AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name="derived-child",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="generate_review",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
        )
    )
    session = bridge.bootstrap()

    def _forbidden_stage1(*_args, **_kwargs):
        pytest.fail("runtime child attempted Stage 1")

    monkeypatch.setattr(AgentRuntimeBridge, "persist_stage1_results", _forbidden_stage1)
    stage = bridge.derive_review_batch(
        session,
        ReviewBatchSpecV1(
            project_name="derived-child",
            selection=SummarySelectionSpecV1(
                parent_job_id="parent-job",
                parent_registry_path=str(parent_registry),
                parent_artifact_id="parent-summary",
                parent_content_hash=file_sha256(parent),
                parent_summary_path=str(parent),
                ordered_paper_keys=("paper-002", "paper-001"),
                expected_count=2,
            ),
        ),
    )

    assert stage.success is True
    assert stage.stage_name == "stage1_derive"
    assert stage.metadata["stage1_model_calls"] == 0
    assert [item["paper_info"]["canonical_paper_key"] for item in json.loads(Path(session.stage_host.summary_file).read_text(encoding="utf-8"))] == [
        "paper-002",
        "paper-001",
    ]


def test_runtime_runner_coordinates_multi_variant_batch_without_generation_calls(
    tmp_path: Path,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(2))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="derived-runner-coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="all",
                project_name="derived-all",
                child_job_id="derived-all-job",
                selection=SummarySelectionSpecV1(
                    parent_job_id="parent-job",
                    parent_registry_path=str(parent_registry),
                    parent_artifact_id="parent-summary",
                    parent_content_hash=file_sha256(parent),
                    parent_summary_path=str(parent),
                    ordered_paper_keys=("paper-002", "paper-001"),
                    expected_count=2,
                ),
            ),
            ReviewVariantSpecV1(
                variant_id="first",
                project_name="derived-first",
                child_job_id="derived-first-job",
                selection=SummarySelectionSpecV1(
                    parent_job_id="parent-job",
                    parent_registry_path=str(parent_registry),
                    parent_artifact_id="parent-summary",
                    parent_content_hash=file_sha256(parent),
                    parent_summary_path=str(parent),
                    ordered_paper_keys=("paper-001",),
                    expected_count=1,
                ),
            ),
        ),
    )

    result = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="derived-runner-coordinator",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="derive_review_batch",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict()},
        ),
    ).run()

    assert result.job_status == "completed"
    # A derivation-only coordinator has no review/validation CurrentArtifactSet;
    # fail-closed completion must not promote it as a final review job.
    assert result.canonical_ready is False
    assert "current_artifact_set_missing" in result.completion_reasons
    assert "derive_review_batch" in result.completed_stages
    assert "analyze" not in result.completed_stages
    assert "outline" not in result.completed_stages
    assert "review" not in result.completed_stages
    manifest_path = Path(result.workspace_path) / "artifacts" / "review_batch_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["coordinator_job_id"] == result.job_id
    assert [item["child_job_id"] for item in manifest["variants"]] == [
        "derived-all-job",
        "derived-first-job",
    ]
    assert [item["selected_count"] for item in manifest["variants"]] == [2, 1]
    assert all(item["stage1_model_calls"] == 0 for item in manifest["variants"])
    assert [
        len(json.loads(Path(item["output_artifacts"]["summary"]["path"]).read_text(encoding="utf-8")))
        for item in manifest["variants"]
    ] == [2, 1]

    reconciled = AgentRuntimeRunner.reconcile(result.workspace_path)
    assert reconciled.completed_stages == ("derive_review_batch", "source_intake")
    assert reconciled.issues == ()


def test_runtime_runner_rejects_single_selection_coordinator_before_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(1))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="single-coordinator",
        selection=_direct_selection(
            parent=parent,
            parent_registry=parent_registry,
            keys=("paper-001",),
        ),
    )
    runner = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="single-coordinator",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="derive_review_batch",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict()},
        ),
    )

    with pytest.raises(RuntimeRunnerError, match="requires a multi-variant"):
        runner.run()

    assert not list((tmp_path / "output").glob("single-coordinator__*"))


def test_runtime_runner_rejects_workspace_alias_before_bootstrap_writes(
    tmp_path: Path,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(1))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="runner__alias",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="runner",
                child_job_id="alias__coordinator-job",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    runner = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="runner__alias",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id="coordinator-job",
            action="derive_review_batch",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict()},
        ),
    )

    with pytest.raises(RuntimeRunnerError, match="aliases the coordinator workspace"):
        runner.run()

    assert not Path(
        tmp_path / "output" / "runner__alias__coordinator-job"
    ).exists()


def test_runtime_runner_resume_reuses_children_after_batch_terminal_gap(
    tmp_path: Path,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(2))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="resume-coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="all",
                project_name="resume-all",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry,
                    keys=("paper-001", "paper-002"),
                ),
            ),
            ReviewVariantSpecV1(
                variant_id="first",
                project_name="resume-first",
                selection=_direct_selection(
                    parent=parent,
                    parent_registry=parent_registry,
                    keys=("paper-001",),
                ),
            ),
        ),
    )
    injected = False

    def crash(point, context) -> None:
        nonlocal injected
        if (
            point == "after_registry_write_before_stage_terminal"
            and context.get("stage_name") == "derive_review_batch"
            and not injected
        ):
            injected = True
            raise RuntimeError("injected batch terminal gap")

    runner = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="resume-coordinator",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="derive_review_batch",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict()},
        ),
        fault_injector=crash,
    )

    first = runner.run()
    assert first.job_status == "failed"
    resumed = AgentRuntimeRunner(
        replace(runner.job_spec, job_id=first.job_id),
    ).resume()

    assert resumed.job_status == "completed"
    assert resumed.completed_stages == ("source_intake", "derive_review_batch")
    output_root = tmp_path / "output"
    assert len(list(output_root.glob("resume-all__review-*"))) == 1
    assert len(list(output_root.glob("resume-first__review-*"))) == 1
    coordinator = JobWorkspace.from_workspace_path(
        first.workspace_path,
        "resume-coordinator",
        first.job_id,
    )
    coordinator_registry = ArtifactRegistry(
        coordinator.paths.registry_path,
        coordinator.job_id,
    )
    manifests = [
        record
        for record in coordinator_registry.list_records()
        if record.artifact_type == "review_batch_manifest"
    ]
    assert len(manifests) == 2
    assert all(file_sha256(record.path) == record.content_hash for record in manifests)
    reconciled = AgentRuntimeRunner.reconcile(first.workspace_path)
    assert reconciled.completed_stages == ("derive_review_batch", "source_intake")
    assert reconciled.issues == ()


def test_runtime_runner_rejects_multi_variant_batch_on_single_review_action(
    tmp_path: Path,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(1))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="batch-coordinator",
        variants=(
            ReviewVariantSpecV1(
                variant_id="only",
                project_name="derived-only",
                selection=SummarySelectionSpecV1(
                    parent_job_id="parent-job",
                    parent_registry_path=str(parent_registry),
                    parent_artifact_id="parent-summary",
                    parent_content_hash=file_sha256(parent),
                    parent_summary_path=str(parent),
                    ordered_paper_keys=("paper-001",),
                    expected_count=1,
                ),
            ),
        ),
    )

    runner = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="batch-coordinator",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="generate_review",
            config=str(_current_config(tmp_path)),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict()},
        ),
    )

    with pytest.raises(RuntimeRunnerError, match="require the derive_review_batch action"):
        runner.run()


def test_runtime_runner_resolves_inline_batch_paths_from_job_spec_origin(tmp_path: Path) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(2))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="relative-derived-runner",
        selection=SummarySelectionSpecV1(
            parent_job_id="parent-job",
            parent_registry_path=str(parent_registry),
            parent_artifact_id="parent-summary",
            parent_content_hash=file_sha256(parent),
            parent_summary_path=str(parent),
            ordered_paper_keys=("paper-002", "paper-001"),
            expected_count=2,
        ),
    ).to_dict()
    selection = batch["selection"]
    selection["parent_registry_path"] = str(Path(parent_registry).relative_to(tmp_path))
    selection["parent_summary_path"] = str(Path(parent).relative_to(tmp_path))
    selection.pop("selection_hash")

    _current_config(tmp_path)
    result = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="relative-derived-runner",
            source=RuntimeSourceSpec(mode="direct", pdf_folder="papers"),
            action="generate_review",
            config="config.ini",
            queue_file="output/_queue/queue.json",
            metadata={"review_batch_spec": batch, "requested_stages": []},
        ),
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "completed"
    assert "analyze" in result.completed_stages

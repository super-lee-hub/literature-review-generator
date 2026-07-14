from __future__ import annotations

import csv
import json
from pathlib import Path

import main
import pytest

from summary_schema import normalize_ai_summary
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace
from services.review_batch import (
    ParentSummaryIntegrityError,
    ReviewBatchSpecV1,
    SummarySelectionError,
    SummarySelectionSpecV1,
    derive_review_batch,
    load_review_batch_spec,
)
from runtime.orchestrator import AgentRuntimeBridge
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from tests.test_runtime_bridge_helpers import build_legacy_main
from runtime.runner import AgentRuntimeRunner


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
                normalize_ai_summary({"summary": f"Contribution {index:03d}"})
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


def _derive_one(
    *,
    parent: Path,
    parent_registry: Path,
    workspace: JobWorkspace,
    canonical_key: str = "paper-001",
):
    return derive_review_batch(
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


def _assert_no_child_derivation_outputs(workspace: JobWorkspace) -> None:
    assert not Path(workspace.artifact_path("summary_selection_v1.json")).exists()
    assert not Path(workspace.artifact_path("child_summaries.json")).exists()
    assert not Path(workspace.artifact_path("paper_artifacts")).exists()
    assert ArtifactRegistry(workspace.paths.registry_path, workspace.job_id).list_records() == []


def test_abc_a_ab_batches_share_parent_and_never_call_stage1_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries())
    classification = tmp_path / "classification.csv"
    _write_classification(classification)

    def _forbidden_provider(*_args, **_kwargs):
        pytest.fail("child review batch crossed the Stage 1 provider boundary")

    monkeypatch.setattr(
        main.LiteratureReviewGenerator,
        "_call_stage1_reader_with_scheduler",
        _forbidden_provider,
    )
    monkeypatch.setattr(AgentRuntimeBridge, "persist_stage1_results", _forbidden_provider)
    results = []
    for label, count in (("ABC", 61), ("A", 20), ("AB", 45)):
        workspace = JobWorkspace(str(tmp_path / "output"), label, f"child-{label}")
        registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        result = derive_review_batch(
            ReviewBatchSpecV1(
                project_name=label,
                batch_label=label,
                selection=_selection(
                    parent=parent,
                    parent_registry=parent_registry,
                    classification=classification,
                    column=label,
                    count=count,
                ),
            ),
            workspace=workspace,
            registry=registry,
        )
        results.append(result)
        payload = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
        assert len(payload) == count == result.selected_count
        assert result.stage1_model_calls == 0
        external = [dep for dep in result.summary_artifact.depends_on if dep.dependency_kind == "external_job"]
        assert len(external) == 1
        assert external[0].job_id == "parent-job"
        assert external[0].artifact_id == "parent-summary"
        assert external[0].content_hash == file_sha256(parent)

    assert {item.parent_summary_hash for item in results} == {file_sha256(parent)}
    assert len({item.selection_hash for item in results}) == 3
    assert [item.selected_count for item in results] == [61, 20, 45]


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
            normalize_ai_summary({"summary": "Divergent but canonical analysis."})
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
    workspace = JobWorkspace(str(tmp_path / "output"), "child", "child-job")

    with pytest.raises(ParentSummaryIntegrityError, match="dependency is not registered"):
        _derive_one(
            parent=parent,
            parent_registry=parent_registry_path,
            workspace=workspace,
        )

    _assert_no_child_derivation_outputs(workspace)


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
            queue_file=str(tmp_path / "queue.json"),
        )
    )
    session = bridge.bootstrap(build_legacy_main())

    def _forbidden_stage1(*_args, **_kwargs):
        pytest.fail("runtime child attempted Stage 1")

    monkeypatch.setattr(
        main.LiteratureReviewGenerator,
        "_call_stage1_reader_with_scheduler",
        _forbidden_stage1,
    )
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
    assert [item["paper_info"]["canonical_paper_key"] for item in json.loads(Path(session.generator.summary_file).read_text(encoding="utf-8"))] == [
        "paper-002",
        "paper-001",
    ]


def test_runtime_runner_derives_batch_without_stage1_handler_call(tmp_path: Path) -> None:
    parent, parent_registry = _register_parent(tmp_path, _summaries(2))
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    (pdf_dir / "placeholder.pdf").write_bytes(b"%PDF-1.4\n")
    batch = ReviewBatchSpecV1(
        project_name="derived-runner",
        selection=SummarySelectionSpecV1(
            parent_job_id="parent-job",
            parent_registry_path=str(parent_registry),
            parent_artifact_id="parent-summary",
            parent_content_hash=file_sha256(parent),
            parent_summary_path=str(parent),
            ordered_paper_keys=("paper-002", "paper-001"),
            expected_count=2,
        ),
    )

    def forbidden_handler(*_args, **_kwargs):
        pytest.fail("derived child crossed a generation-provider boundary")

    result = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="derived-runner",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            action="generate_review",
            config=str(tmp_path / "config.ini"),
            queue_file=str(tmp_path / "output" / "_queue" / "queue.json"),
            metadata={"review_batch_spec": batch.to_dict(), "requested_stages": []},
        ),
        legacy_main=build_legacy_main(),
        stage_handler=forbidden_handler,
    ).run()

    assert result.job_status == "completed"
    assert "analyze" in result.completed_stages
    derived = Path(result.workspace_path) / "artifacts" / "derived-runner_summaries.json"
    assert [item["paper_info"]["canonical_paper_key"] for item in json.loads(derived.read_text(encoding="utf-8"))] == [
        "paper-002",
        "paper-001",
    ]


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

    def forbidden_handler(*_args, **_kwargs):
        pytest.fail("derived child crossed a generation-provider boundary")

    result = AgentRuntimeRunner(
        RuntimeJobSpec(
            project_name="relative-derived-runner",
            source=RuntimeSourceSpec(mode="direct", pdf_folder="papers"),
            action="generate_review",
            config="config.ini",
            queue_file="output/_queue/queue.json",
            metadata={"review_batch_spec": batch, "requested_stages": []},
        ),
        legacy_main=build_legacy_main(),
        stage_handler=forbidden_handler,
        origin_dir=tmp_path,
    ).run()

    assert result.job_status == "completed"
    assert "analyze" in result.completed_stages

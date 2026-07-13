from __future__ import annotations

import csv
import json
from pathlib import Path

import main
import pytest

from services.artifact_registry import ArtifactRegistry, file_sha256
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
            },
            "ai_summary": {"core_contribution": f"Contribution {index:03d}"},
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


def _register_parent(tmp_path: Path, summaries: list[dict]) -> tuple[Path, Path]:
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

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.reconcile import load_summary_source_manifest
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import ArtifactRegistry, file_sha256
from tests.run_first_warm_reuse import run_first_warm_reuse
from tests.test_run_first_scale_integration import (
    _local_config,
    _scale_papers,
    _start_scale_server,
    _write_scale_pdf,
)
from validation.closure import resolve_current_stage_closure_map


def _stage1_reader_calls(server: Any) -> list[dict[str, Any]]:
    calls = list(server.calls)
    return [
        row
        for row in calls
        if isinstance(row, dict) and row.get("stage") == "stage1_reader"
    ]


@pytest.mark.integration
@pytest.mark.parametrize("automatic_discovery", [False, True], ids=["explicit", "automatic"])
def test_four_pdf_warm_reuse_is_new_workspace_zero_reader_posts_and_source_bound(
    tmp_path: Path,
    automatic_discovery: bool,
) -> None:
    papers = _scale_papers(4)
    pdf_folder = tmp_path / "source-pdfs"
    pdf_folder.mkdir()
    for paper in papers:
        _write_scale_pdf(pdf_folder / f"{paper.stem}.pdf", paper)
    source_pdf_hashes_before = {
        path.name: file_sha256(path) for path in sorted(pdf_folder.glob("*.pdf"))
    }
    assert len(source_pdf_hashes_before) == 4

    server, thread, api_base = _start_scale_server(papers)
    fixture_server = cast(Any, server)
    try:
        config_path = _local_config(
            tmp_path,
            api_base,
            provider_call_limit=4,
        )
        finite_local_budget = ProviderAggregateBudgetV2(
            max_provider_calls_total=4,
            max_output_tokens_total=100_000,
            max_retry_attempts_total=0,
            max_wall_seconds=600,
        ).to_dict()
        seed_spec = RuntimeJobSpec(
            project_name="run-first-warm-reuse",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
            job_id="run-first-warm-seed",
            config=str(config_path),
            action="analyze",
            queue_file=str(tmp_path / "seed.queue.json"),
            metadata={"provider_budget": finite_local_budget},
        )

        seed_result = AgentRuntimeRunner(seed_spec).run()
        assert seed_result.job_status == "completed", seed_result
        assert seed_result.completion_status == "complete", seed_result
        seed_workspace, seed_registry = AgentRuntimeRunner._open_workspace(
            seed_result.workspace_path
        )
        seed_reader_calls = _stage1_reader_calls(server)
        assert len(seed_reader_calls) == 4, seed_reader_calls
        assert not fixture_server.errors, fixture_server.errors
        seed_closure_map = resolve_current_stage_closure_map(
            seed_registry,
            external_registry_resolver=AgentRuntimeRunner._external_registry_resolver(
                seed_workspace
            ),
        )
        seed_analyze_closure = seed_closure_map.provider_closures_by_stage["analyze"]
        assert seed_analyze_closure["complete"] is True
        assert len(seed_analyze_closure["expected_call_ids"]) == 4
        assert set(seed_analyze_closure["observed_call_ids"]) == set(
            seed_analyze_closure["expected_call_ids"]
        )

        warmed = run_first_warm_reuse(
            seed_workspace_path=seed_result.workspace_path,
            seed_spec=seed_spec,
            current_shared_fixture_config=config_path,
            provider_budget=finite_local_budget,
            action="analyze",
            automatic_discovery=automatic_discovery,
        )
        assert warmed.result.job_status == "completed", warmed.result
        assert warmed.result.completion_status == "complete", warmed.result
        assert warmed.spec.job_id != seed_spec.job_id
        assert warmed.plan["read_only"] is True
        assert warmed.plan_seconds > 0
        assert Path(warmed.workspace.base_output_dir).resolve() == Path(
            seed_workspace.base_output_dir
        ).resolve()
        assert len(warmed.reuse_manifest_paths) == 4
        assert warmed.spec.reuse_stage1 is True
        assert len(warmed.spec.reuse_summary_files) == (0 if automatic_discovery else 4)
        assert all(
            Path(path).resolve().is_relative_to(Path(seed_workspace.root_dir).resolve())
            for path in warmed.reuse_manifest_paths
        )

        reader_calls_after_warm = _stage1_reader_calls(server)
        assert len(reader_calls_after_warm) == 4, reader_calls_after_warm
        assert reader_calls_after_warm == seed_reader_calls
        assert len(fixture_server.calls) == 4, fixture_server.calls
        assert not fixture_server.errors, fixture_server.errors

        source_scope_record = warmed.registry.get("stage1_source_scope")
        assert source_scope_record is not None and source_scope_record.status == "ready"
        with Path(source_scope_record.path).open("r", encoding="utf-8") as handle:
            warm_scope = json.load(handle)
        warm_pdf_rows = {
            str(row["paper_key"]): row
            for row in warm_scope["papers"]
            if isinstance(row, dict)
        }
        assert len(warm_pdf_rows) == 4
        assert set(warm_pdf_rows) == set(warmed.paper_pdf_sha256)
        expected_source_paths = {
            (pdf_folder / f"{paper.stem}.pdf").resolve() for paper in papers
        }
        assert {
            Path(row["source_pdf"]).resolve() for row in warm_pdf_rows.values()
        } == expected_source_paths
        assert all(
            Path(row["source_pdf"]).resolve().parent == pdf_folder.resolve()
            and row["source_pdf_sha256"] == warmed.paper_pdf_sha256[key]
            for key, row in warm_pdf_rows.items()
        )

        summary_manifest_record = warmed.registry.get("summary_source_manifest")
        assert summary_manifest_record is not None
        ArtifactRegistry._verify_ready_artifact(summary_manifest_record)
        source_manifest, _summary_path, summary_rows = load_summary_source_manifest(
            summary_manifest_record.path
        )
        assert source_manifest["summary_count"] == 4
        assert len(summary_rows) == 4
        assert len(source_manifest["source_items"]) == 4
        assert {row["paper_info"]["canonical_paper_key"] for row in summary_rows} == set(
            warmed.paper_pdf_sha256
        )
        assert all(row.get("status") == "success" for row in summary_rows)
        assert all(
            isinstance(row.get("provider"), dict)
            and row["provider"].get("route") == "Stage1Reuse"
            and row["provider"].get("transport_count") == 0
            for row in summary_rows
        )
        assert {
            row["canonical_paper_key"] for row in source_manifest["source_items"]
        } == set(warmed.paper_pdf_sha256)
        assert all(
            row.get("disposition") == "reused"
            for row in source_manifest["source_items"]
        )

        reuse_records = [
            record
            for record in warmed.registry.list_records()
            if record.artifact_type == "stage1_summary_reuse_record"
            and record.status == "ready"
        ]
        assert len(reuse_records) == 4
        reused_keys: set[str] = set()
        for record in reuse_records:
            ArtifactRegistry._verify_ready_artifact(record)
            with Path(record.path).open("r", encoding="utf-8") as handle:
                reuse_payload = json.load(handle)
            assert reuse_payload["source_authority_job_id"] == seed_spec.job_id
            source_identity = reuse_payload.get("source_bundle_paper_identity")
            assert isinstance(source_identity, dict)
            reused_keys.add(str(source_identity["canonical_paper_key"]))
        assert reused_keys == set(warmed.paper_pdf_sha256)

        warm_closure_map = resolve_current_stage_closure_map(
            warmed.registry,
            external_registry_resolver=AgentRuntimeRunner._external_registry_resolver(
                warmed.workspace
            ),
        )
        assert warm_closure_map.blocking_issues == (), warm_closure_map.to_dict()
        warm_analyze_closure = warm_closure_map.provider_closures_by_stage["analyze"]
        assert warm_analyze_closure["complete"] is True
        assert warm_analyze_closure["expected_call_ids"] == []
        assert warm_analyze_closure["observed_call_ids"] == []
        assert warm_analyze_closure["model_call_count"] == 0

        source_pdf_hashes_after = {
            path.name: file_sha256(path) for path in sorted(pdf_folder.glob("*.pdf"))
        }
        assert source_pdf_hashes_after == source_pdf_hashes_before
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

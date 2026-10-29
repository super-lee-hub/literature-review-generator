"""Run-first localhost-HTTP acceptance for explicit typed Stage 1 imports.

The test deliberately uses the existing high-fidelity scale PDF generator and
HTTP fixture.  It seeds genuine typed manifests with the local Reader route,
then starts fresh workspaces that import those manifests against the complete
current PDF folder.  The fixture is local and finite; no external provider is
involved.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import InternalStageExecutorRegistry
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.reconcile import load_summary_source_manifest
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.artifact_registry import ArtifactRegistry, file_sha256
from tests.test_run_first_scale_integration import (
    _local_config,
    _scale_papers,
    _start_scale_server,
    _write_scale_pdf,
)
from tests.test_run_first_explicit_typed_import_ordinary_process import (
    run_ordinary_fresh_process_all_import_zero,
)
from validation.closure import resolve_current_stage_closure_map


def _reader_calls(server: Any) -> list[dict[str, Any]]:
    return [
        row
        for row in list(server.calls)
        if isinstance(row, dict) and row.get("stage") == "stage1_reader"
    ]


def _manifest_rows(registry: ArtifactRegistry) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    paths: list[str] = []
    rows: dict[str, dict[str, Any]] = {}
    for record in registry.list_records():
        if (
            record.status != "ready"
            or record.artifact_type != "stage1_reusable_summary_manifest"
            or record.artifact_version != "v1"
        ):
            continue
        paths.append(str(record.path))
        loaded_rows = InternalStageExecutorRegistry._summary_payloads_from_file(record.path)
        if len(loaded_rows) != 1:
            raise AssertionError(f"typed manifest did not load one summary row: {record.path}")
        row = dict(loaded_rows[0])
        paper_info = row.get("paper_info")
        key = str(paper_info.get("canonical_paper_key") or "") if isinstance(paper_info, Mapping) else ""
        if not key:
            raise AssertionError(f"typed manifest has no canonical key: {record.path}")
        rows[key] = row
    if len(paths) != len(rows):
        raise AssertionError(f"typed manifest identities are not unique: {paths}")
    return tuple(sorted(paths)), rows


def _body(record: Any) -> Any:
    try:
        payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload.get("payload", payload) if isinstance(payload, Mapping) else payload


def _budget_state(workspace: Any) -> dict[str, Any]:
    path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    assert path.is_file(), path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), payload
    return payload


def _artifact_paper_key(record: Any) -> str:
    body = _body(record)
    if not isinstance(body, Mapping):
        return ""
    for container_name in ("paper_identity", "paper_info", "paper", "identity"):
        container = body.get(container_name)
        if isinstance(container, Mapping):
            key = str(container.get("canonical_paper_key") or "").strip()
            if key:
                return key
    return str(
        body.get("canonical_paper_key")
        or body.get("paper_key")
        or ""
    ).strip()


def _records_for_paper(registry: ArtifactRegistry, artifact_type: str) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for record in registry.list_records():
        if record.status != "ready" or record.artifact_type != artifact_type:
            continue
        key = _artifact_paper_key(record)
        if key:
            result.setdefault(key, []).append(record)
    return result


def _finite_budget(calls: int) -> dict[str, Any]:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=1_000_000,
        max_retry_attempts_total=0,
        max_wall_seconds=900,
    ).to_dict()


def _write_spec(path: Path, spec: RuntimeJobSpec) -> None:
    path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _summary_map(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        paper_info = row.get("paper_info") if isinstance(row, Mapping) else None
        key = str(paper_info.get("canonical_paper_key") or "") if isinstance(paper_info, Mapping) else ""
        if key:
            result[key] = dict(row)
    return result


def _source_item_map(source_manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in source_manifest.get("source_items") or ():
        if isinstance(item, Mapping) and str(item.get("canonical_paper_key") or ""):
            result[str(item["canonical_paper_key"])] = dict(item)
    return result


def _assert_stage1_closure(
    registry: ArtifactRegistry,
    *,
    expected_count: int,
    external_registry_resolver: Any = None,
) -> Mapping[str, Any]:
    closure_map = resolve_current_stage_closure_map(
        registry,
        external_registry_resolver=external_registry_resolver,
    )
    assert closure_map.blocking_issues == (), closure_map.to_dict()
    closure = closure_map.provider_closures_by_stage["analyze"]
    assert closure["complete"] is True
    assert len(closure["expected_call_ids"]) == expected_count
    assert set(closure["observed_call_ids"]) == set(closure["expected_call_ids"])
    assert closure["model_call_count"] == expected_count
    return closure


@pytest.mark.integration
@pytest.mark.slow
def test_explicit_typed_import_15_source_partition_and_zero_call_resume(
    tmp_path: Path,
) -> None:
    papers = _scale_papers(15)
    pdf_folder = tmp_path / "source-pdfs"
    pdf_folder.mkdir()
    for paper in papers:
        _write_scale_pdf(pdf_folder / f"{paper.stem}.pdf", paper)
    source_hashes = {
        path.name: file_sha256(path)
        for path in sorted(pdf_folder.glob("*.pdf"))
    }
    assert len(source_hashes) == 15

    server, thread, api_base = _start_scale_server(papers)
    try:
        seed_root = tmp_path / "seed"
        seed_root.mkdir(parents=True, exist_ok=True)
        seed_config = _local_config(
            seed_root,
            api_base,
            provider_call_limit=15,
        )
        seed_budget = _finite_budget(15)
        seed_spec = RuntimeJobSpec(
            project_name="explicit-typed-import-15",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
            job_id="explicit-typed-import-15-seed",
            config=str(seed_config),
            action="analyze",
            reuse_stage1=False,
            queue_file=str(tmp_path / "seed.queue.json"),
            metadata={"provider_budget": seed_budget},
        )

        seed_result = AgentRuntimeRunner(seed_spec).run()
        assert seed_result.job_status == "completed", seed_result
        assert seed_result.completion_status == "complete", seed_result
        seed_workspace, seed_registry = AgentRuntimeRunner._open_workspace(
            seed_result.workspace_path
        )
        seed_budget_state = _budget_state(seed_workspace)
        assert seed_budget_state["budget"] == seed_budget
        assert seed_budget_state["retry_attempts_used"] == 0
        assert seed_budget_state["calls_reserved"] == 0
        assert seed_budget_state["output_tokens_reserved"] == 0
        assert seed_budget_state["retry_attempts_reserved"] == 0
        seed_reader_calls = _reader_calls(server)
        assert len(seed_reader_calls) == 15, seed_reader_calls
        assert all(call.get("model") == "rfs-reader-local" for call in seed_reader_calls)
        assert not server.errors, server.errors

        manifest_paths, seed_rows = _manifest_rows(seed_registry)
        assert len(manifest_paths) == len(seed_rows) == 15
        seed_manifest_hashes = {
            path: file_sha256(path)
            for path in manifest_paths
        }
        all_keys = set(seed_rows)
        imported_keys = set(sorted(all_keys)[:-1])
        pending_keys = all_keys - imported_keys
        assert len(imported_keys) == 14
        assert len(pending_keys) == 1
        partial_paths = tuple(
            path
            for path in manifest_paths
            if str(
                json.loads(Path(path).read_text(encoding="utf-8")).get(
                    "canonical_paper_key", ""
                )
            ) in imported_keys
        )
        assert len(partial_paths) == 14

        current_root = tmp_path / "current-partial"
        current_root.mkdir(parents=True, exist_ok=True)
        current_config = _local_config(
            current_root,
            api_base,
            provider_call_limit=1,
        )
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(current_config, encoding="utf-8")
        parser["Primary_Reader_API"]["temperature"] = "0.123"
        with current_config.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        assert file_sha256(seed_config) != file_sha256(current_config)

        partial_budget = _finite_budget(1)
        partial_spec = RuntimeJobSpec(
            project_name="explicit-typed-import-15",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
            job_id="explicit-typed-import-15-partial",
            config=str(current_config),
            action="analyze",
            reuse_stage1=True,
            reuse_summary_files=partial_paths,
            queue_file=str(tmp_path / "partial.queue.json"),
            metadata={
                "stage1_summary_input_mode": "explicit_typed_import",
                "provider_budget": partial_budget,
            },
        )
        partial_spec.validate()

        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        plan_path = tmp_path / "partial.runtime-job-spec.json"
        _write_spec(plan_path, partial_spec)
        before_plan_calls = len(server.calls)
        plan = control.plan(plan_path)
        assert len(server.calls) == before_plan_calls
        assert plan["read_only"] is True
        full_plan = plan["full_stage_request_plan"]
        explicit_inputs = full_plan["explicit_stage1_inputs"]
        assert explicit_inputs == {
            "full_source_count": 15,
            "imported_count": 14,
            "pending_count": 1,
        }
        pending_basis = full_plan["stage1_work_item_basis"]
        assert pending_basis["paper_work_item_count"] == 1

        partial_result = AgentRuntimeRunner(partial_spec).run()
        assert partial_result.job_status == "completed", partial_result
        assert partial_result.completion_status == "complete", partial_result
        after_partial_reader_calls = _reader_calls(server)
        assert len(after_partial_reader_calls) == 16, after_partial_reader_calls
        new_reader_calls = after_partial_reader_calls[len(seed_reader_calls) :]
        assert len(new_reader_calls) == 1
        assert new_reader_calls[0]["model"] == "rfs-reader-local"
        assert not server.errors, server.errors

        partial_workspace, partial_registry = AgentRuntimeRunner._open_workspace(
            partial_result.workspace_path
        )
        partial_closure = _assert_stage1_closure(
            partial_registry,
            expected_count=1,
            external_registry_resolver=AgentRuntimeRunner._external_registry_resolver(
                partial_workspace
            ),
        )
        assert len(partial_closure["expected_call_ids"]) == 1

        scope_record = partial_registry.get("stage1_source_scope")
        assert scope_record is not None and scope_record.status == "ready"
        scope_payload = _body(scope_record)
        scope_rows = {
            str(row["paper_key"]): row
            for row in scope_payload.get("papers") or ()
            if isinstance(row, Mapping)
        }
        assert len(scope_rows) == 15
        assert {str(row["source_pdf_sha256"]) for row in scope_rows.values()} == set(
            source_hashes.values()
        )

        summary_record = partial_registry.get("summary_source_manifest")
        assert summary_record is not None
        source_manifest, _summary_path, summary_rows = load_summary_source_manifest(
            summary_record.path
        )
        assert source_manifest["summary_count"] == 15
        assert len(summary_rows) == 15
        output_summaries = _summary_map(summary_rows)
        assert len(output_summaries) == 15
        source_items = _source_item_map(source_manifest)
        assert set(source_items) == set(output_summaries)
        typed_items = {
            key: item
            for key, item in source_items.items()
            if item.get("disposition") == "typed_imported"
        }
        assert len(typed_items) == 14
        assert set(typed_items) == imported_keys
        assert all(item.get("current_transport_count") == 0 for item in typed_items.values())
        assert all(item.get("current_pdf_join", {}).get("sha256") for item in typed_items.values())

        for key in imported_keys:
            imported = seed_rows[key]
            actual = output_summaries[key]
            assert actual["ai_summary"] == imported["ai_summary"]
            assert actual.get("qualifiers") == imported.get("qualifiers")
            assert actual["provider"] == imported["provider"]
            assert actual["stage1_reuse"]["binding"] == imported["stage1_reuse"]["binding"]
            assert typed_items[key]["typed_manifest_artifact_hash"] == (
                imported["stage1_reuse"]["typed_manifest_artifact_hash"]
            )

        paper_records = _records_for_paper(partial_registry, "paper_artifact")
        evidence_records = _records_for_paper(partial_registry, "evidence_manifest")
        assert set(paper_records) <= pending_keys
        assert set(evidence_records) <= pending_keys
        assert sum(len(values) for values in paper_records.values()) == 1
        assert sum(len(values) for values in evidence_records.values()) == 1

        # A second fresh workspace imports all 15 authorities.  The same
        # current PDF source remains in scope, but no current Reader call is
        # reachable and no current source artifacts are manufactured.
        all_spec = RuntimeJobSpec(
            project_name="explicit-typed-import-15",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
            job_id="explicit-typed-import-15-all",
            config=str(current_config),
            action="analyze",
            reuse_stage1=True,
            reuse_summary_files=manifest_paths,
            queue_file=str(tmp_path / "all.queue.json"),
            metadata={
                "stage1_summary_input_mode": "explicit_typed_import",
                "provider_budget": _finite_budget(1),
            },
        )
        all_result = AgentRuntimeRunner(all_spec).run()
        assert all_result.job_status == "completed", all_result
        assert all_result.completion_status == "complete", all_result
        assert len(_reader_calls(server)) == 16
        all_workspace, all_registry = AgentRuntimeRunner._open_workspace(
            all_result.workspace_path
        )
        all_closure = _assert_stage1_closure(
            all_registry,
            expected_count=0,
            external_registry_resolver=AgentRuntimeRunner._external_registry_resolver(
                all_workspace
            ),
        )
        assert all_closure["observed_call_ids"] == []
        all_summary_record = all_registry.get("summary_source_manifest")
        assert all_summary_record is not None
        all_source_manifest, _all_summary_path, all_summary_rows = load_summary_source_manifest(
            all_summary_record.path
        )
        assert all_source_manifest["summary_count"] == 15
        all_source_items = _source_item_map(all_source_manifest)
        assert len(all_source_items) == 15
        assert all(item.get("disposition") == "typed_imported" for item in all_source_items.values())
        assert all(item.get("current_transport_count") == 0 for item in all_source_items.values())
        assert not _records_for_paper(all_registry, "paper_artifact")
        assert not _records_for_paper(all_registry, "evidence_manifest")

        ordinary_report = run_ordinary_fresh_process_all_import_zero(
            tmp_path=tmp_path,
            source_root=Path(__file__).resolve().parents[1],
            pdf_folder=pdf_folder,
            manifest_paths=manifest_paths,
            current_config=current_config,
            project_name="explicit-typed-import-15",
            job_id="explicit-typed-import-15-ordinary-process",
        )
        assert ordinary_report["scope"]["full_source_count"] == 15
        assert ordinary_report["scope"]["imported_count"] == 15
        assert ordinary_report["scope"]["pending_count"] == 0
        assert ordinary_report["scope"]["remote_preprocess_required"] is False
        assert ordinary_report["provider_receipts"]["count"] == 0
        assert ordinary_report["analyze_closure"] == {
            "expected_count": 0,
            "observed_count": 0,
        }
        assert ordinary_report["budget"]["calls_used"] == 0
        assert ordinary_report["budget"]["calls_reserved"] == 0
        assert ordinary_report["budget"]["retry_attempts_reserved"] == 0
        assert ordinary_report["socket_events"] == []

        all_budget_before_resume = _budget_state(all_workspace)
        assert all_budget_before_resume["budget"] == _finite_budget(1)
        assert all_budget_before_resume["calls_reserved"] == 0
        assert all_budget_before_resume["output_tokens_reserved"] == 0
        assert all_budget_before_resume["retry_attempts_reserved"] == 0
        all_clock_before_resume = all_budget_before_resume["absolute_deadline_epoch"]
        before_resume_calls = len(server.calls)
        resumed = AgentRuntimeRunner(all_spec).resume()
        assert resumed.job_status == "completed", resumed
        assert resumed.completion_status == "complete", resumed
        assert len(server.calls) == before_resume_calls
        all_budget_after_resume = _budget_state(all_workspace)
        assert all_budget_after_resume["budget"] == all_budget_before_resume["budget"]
        assert all_budget_after_resume["absolute_deadline_epoch"] == all_clock_before_resume
        assert all_budget_after_resume["calls_reserved"] == all_budget_before_resume["calls_reserved"]
        assert all_budget_after_resume["output_tokens_reserved"] == all_budget_before_resume[
            "output_tokens_reserved"
        ]
        assert all_budget_after_resume["retry_attempts_reserved"] == all_budget_before_resume[
            "retry_attempts_reserved"
        ]
        assert _budget_state(seed_workspace)["budget"] == seed_budget
        assert {
            path: file_sha256(path)
            for path in manifest_paths
        } == seed_manifest_hashes
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.integration
def test_explicit_typed_import_invalid_manifest_fails_before_local_http_fallback(
    tmp_path: Path,
) -> None:
    """Invalid explicit authority cannot silently become ordinary generation."""

    papers = _scale_papers(4)
    pdf_folder = tmp_path / "source-pdfs"
    pdf_folder.mkdir()
    for paper in papers:
        _write_scale_pdf(pdf_folder / f"{paper.stem}.pdf", paper)
    server, thread, api_base = _start_scale_server(papers)
    try:
        invalid_root = tmp_path / "invalid"
        invalid_root.mkdir(parents=True, exist_ok=True)
        config_path = _local_config(invalid_root, api_base, provider_call_limit=1)
        invalid_path = tmp_path / "invalid-manifest.json"
        invalid_path.write_text(
            json.dumps(
                {
                    "artifact_type": "stage1_reusable_summary_manifest",
                    "artifact_version": "v1",
                    "canonical_paper_key": papers[0].paper_key,
                    "manifest_content_hash": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        spec = RuntimeJobSpec(
            project_name="explicit-typed-import-invalid",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
            job_id="explicit-typed-import-invalid",
            config=str(config_path),
            action="analyze",
            reuse_stage1=True,
            reuse_summary_files=(str(invalid_path),),
            queue_file=str(tmp_path / "invalid.queue.json"),
            metadata={
                "stage1_summary_input_mode": "explicit_typed_import",
                "provider_budget": _finite_budget(1),
            },
        )
        spec.validate()
        # The verified source partition is needed to derive the route before
        # creating a workspace. Malformed explicit input follows the existing
        # normalization-error contract and must never reach ordinary reading.
        with pytest.raises(RuntimeRunnerError) as rejected:
            AgentRuntimeRunner(spec).run()
        assert str(rejected.value) == (
            f"cannot load selected typed Stage 1 input: {invalid_path}: "
            "typed Stage1 reusable summary manifest has no summary payload: "
            f"{invalid_path}"
        )
        assert not server.calls, server.calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


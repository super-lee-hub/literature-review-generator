from __future__ import annotations

import configparser
import hashlib
import json
import os
import time
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import outline.v3_executor as outline_executor_module
from outline.candidate_arbitration_aggregation import ASSESSMENT_INPUT_SCHEMA
from outline.candidate_arbitration_navigation import EXTRACT_REQUEST_SCHEMA
from outline.provider_router import OutlineRoleRoute, build_outline_provider_router
from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_context import ProviderContextProfile
from runtime.runner import AgentRuntimeRunner
from services.model_capabilities import resolve_model_capability
from services.model_selection import get_api_config_for_section
from services.settings import ApplicationSettings, validate_config_keys
from tests import test_run_first_scale_integration as scale

_DEFAULT_CASE_PATH = Path(__file__).parent / "cases" / "arbitration_overflow_http_role12k_case.json"


def _payload(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(value, Mapping), f"expected a JSON object at {path}"
    nested = value.get("payload")
    return dict(nested) if isinstance(nested, Mapping) else dict(value)


def _write_result(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def test_real_http_arbitration_overflow_survives_publish_crash_and_finishes_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_path = Path(os.environ.get("ARBITRATION_OVERFLOW_HTTP_CASE_PATH", str(_DEFAULT_CASE_PATH)))
    case = json.loads(case_path.read_text(encoding="utf-8"))
    assert isinstance(case, Mapping)
    budget = dict(case["provider_budget"])
    assert case["schema_version"] == "pr25-r4-arbitration-overflow-http-case/v1"
    assert budget == {
        "schema_version": "provider-aggregate-budget-contract-v2",
        "max_provider_calls_total": 200,
        "max_output_tokens_total": 2_000_000,
        "max_retry_attempts_total": 0,
        "max_wall_seconds": 1_800,
    }

    result_path = Path(os.environ.get(
        "ARBITRATION_OVERFLOW_HTTP_RESULT_PATH",
        str(tmp_path / "arbitration_overflow_http_result.json"),
    ))
    if result_path.exists():
        raise AssertionError(f"refusing to overwrite prior acceptance result: {result_path}")
    result: dict[str, Any] = {
        "schema_version": "pr25-r4-arbitration-overflow-http-result/v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_pid": os.getpid(),
        "case_path": str(case_path),
        "result_path": str(result_path),
        "case": dict(case),
        "run_state": {},
        "error": None,
    }
    print(
        f"ARBITRATION_OVERFLOW_HTTP_PID={os.getpid()} "
        f"CASE={case_path} RESULT={result_path}",
        flush=True,
    )

    server = None
    server_thread = None
    api_base = ""
    fault: dict[str, Any] = {"injected": False, "publish_calls_seen": 0}
    started = time.monotonic()
    try:
        size = int(case["source_count"])
        source_variant = str(case["source_variant"])
        papers = scale._scale_papers(size, source_variant=source_variant)
        pdf_dir = tmp_path / "papers"
        pdf_dir.mkdir()
        for paper in papers:
            scale._write_scale_pdf(pdf_dir / f"{paper.stem}.pdf", paper)
        with scale.fitz.open(pdf_dir / f"{papers[0].stem}.pdf") as first_pdf:
            assert len(first_pdf) == 2
            assert "Table 2 (page 2)" in "\n".join(page.get_text() for page in first_pdf)

        required_comparisons = scale._scale_required_comparisons(papers, source_variant)
        review_intent = {
            "required_comparisons": [
                {
                    "paper_keys": pair,
                    "question": (
                        "Compare the two source-reported findings within their recorded sample boundaries."
                        if pair == required_comparisons[0]
                        else (
                            "Compare only directions, magnitudes, conditions, sample labels, and 30-day limits "
                            "printed in these two source PDFs; retain null or reverse findings."
                        )
                    ),
                    "relation_type": "reported_magnitude_contrast",
                }
                for pair in required_comparisons
            ],
        }
        server, server_thread, api_base = scale._start_scale_server(papers)
        server.arbitration_fixture_state = case.get("arbitration_fixture_state")  # type: ignore[attr-defined]

        config_path = scale._local_config(
            tmp_path,
            api_base,
            provider_call_limit=int(budget["max_provider_calls_total"]),
        )
        config = configparser.ConfigParser(interpolation=None)
        config.read(config_path, encoding="utf-8")
        config["Outline"]["candidate_count"] = str(case["candidate_count"])
        config["OutlineStability"]["mode"] = str(case["stability_mode"])
        config["OutlineStability"]["max_provider_calls"] = str(budget["max_provider_calls_total"])
        global_source_cap = int(case.get("global_source_prompt_cap", case["effective_input_cap"]))
        config["OutlineStability"]["max_source_prompt_tokens"] = str(global_source_cap)
        arbitration_route_section = str(case.get("arbitrator_route_section") or "")
        if arbitration_route_section:
            arbitration_route_config = dict(config["Outline_API"])
            arbitration_route_config["max_context_tokens"] = str(case["arbitrator_max_context_tokens"])
            config[arbitration_route_section] = arbitration_route_config
            config["OutlineModels"]["arbitrator_model"] = arbitration_route_section
        with config_path.open("w", encoding="utf-8") as handle:
            config.write(handle)

        loaded_config = {section: dict(config[section]) for section in config.sections()}
        config_errors = validate_config_keys(loaded_config)
        assert not config_errors, config_errors
        assert not server.calls  # type: ignore[attr-defined]
        settings = ApplicationSettings.from_config(loaded_config)

        def resolve_route(role: str, section_name: str) -> OutlineRoleRoute | None:
            api_config = get_api_config_for_section(loaded_config, section_name)
            model = str(api_config.get("model") or "").strip()
            if not model:
                return None
            capability = resolve_model_capability(api_config)
            profile = ProviderContextProfile.from_api_config(
                api_config,
                max_output_tokens=int(api_config.get("max_output_tokens") or 4_096),
                default_provider=capability.provider_family,
                default_model=model,
                default_endpoint_type=capability.endpoint_type,
            )
            return OutlineRoleRoute(
                role=role,
                config_section=section_name,
                provider_name=capability.provider_family,
                model=model,
                endpoint_type=capability.endpoint_type,
                profile=profile,
                api_base=str(api_config.get("api_base") or ""),
            )

        route_readback = build_outline_provider_router(
            settings=settings,
            config=loaded_config,
            route_resolver=resolve_route,
        )
        arbitration_route = route_readback.route_for("arbitration")
        generation_route = route_readback.route_for("candidate_provider_generation")
        effective_arbitration_cap = min(arbitration_route.profile.input_budget, global_source_cap)
        effective_generation_cap = min(generation_route.profile.input_budget, global_source_cap)
        assert effective_arbitration_cap == int(case["effective_input_cap"]), {
            "route": arbitration_route.to_dict(),
            "effective_input_cap": effective_arbitration_cap,
        }
        assert effective_generation_cap == global_source_cap, {
            "route": generation_route.to_dict(),
            "effective_input_cap": effective_generation_cap,
        }
        if arbitration_route_section:
            assert arbitration_route.config_section == arbitration_route_section
            assert arbitration_route.model == generation_route.model
            assert arbitration_route.endpoint_type == generation_route.endpoint_type
            assert arbitration_route.profile.max_output_tokens == generation_route.profile.max_output_tokens
            assert arbitration_route.profile.reasoning_reserve == generation_route.profile.reasoning_reserve
            assert arbitration_route.profile.safety_margin == generation_route.profile.safety_margin

        spec = RuntimeJobSpec(
            project_name="pr25-r4-arbitration-overflow-http",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
            job_id=str(case.get("job_id") or "pr25-r4-arbitration-overflow-http-10pdf"),
            config=str(config_path),
            action="run_all",
            queue_file=str(tmp_path / "queue.json"),
            metadata={"review_intent": review_intent, "provider_budget": budget},
        )
        result["run_state"].update({
            "job_id": spec.job_id,
            "config_path": str(config_path),
            "config_sha256": scale.hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "api_base": api_base,
            "source_count": len(list(pdf_dir.glob("*.pdf"))),
            "provider_budget": budget,
            "effective_input_cap": int(case["effective_input_cap"]),
            "global_source_prompt_cap": global_source_cap,
            "candidate_count": int(case["candidate_count"]),
            "route_readback": {
                "arbitration": {
                    "section": arbitration_route.config_section,
                    "model": arbitration_route.model,
                    "input_budget": arbitration_route.profile.input_budget,
                    "effective_input_cap": effective_arbitration_cap,
                    "max_output_tokens": arbitration_route.profile.max_output_tokens,
                    "reasoning_reserve": arbitration_route.profile.reasoning_reserve,
                    "safety_margin": arbitration_route.profile.safety_margin,
                },
                "candidate_provider_generation": {
                    "section": generation_route.config_section,
                    "model": generation_route.model,
                    "input_budget": generation_route.profile.input_budget,
                    "effective_input_cap": effective_generation_cap,
                },
            },
        })

        original_publish = outline_executor_module.publish_json_artifact

        def publish_then_fail_once(*args: Any, **kwargs: Any) -> Any:
            artifact_role = str(kwargs.get("artifact_role") or "")
            if artifact_role == "provider_validated_result":
                fault["publish_calls_seen"] += 1
            record = original_publish(*args, **kwargs)
            if artifact_role == "provider_validated_result" and not fault["injected"]:
                fault.update({
                    "injected": True,
                    "artifact_id": str(kwargs.get("artifact_id") or ""),
                    "artifact_role": artifact_role,
                    "transport_node_id": "",
                    "request_id": "",
                })
                overflow_rows = [
                    row for row in server.trace  # type: ignore[attr-defined]
                    if isinstance(row, Mapping) and row.get("kind") == "arbitration_overflow_model_fixture"
                ]
                if overflow_rows:
                    fault["transport_node_id"] = str(overflow_rows[-1].get("transport_node_id") or "")
                    fault["request_id"] = str(overflow_rows[-1].get("request_id") or "")
                    fault["request_schema"] = str(overflow_rows[-1].get("request_schema") or "")
                raise RuntimeError("injected once after READY provider_validated_result publication")
            return record

        monkeypatch.setattr(outline_executor_module, "publish_json_artifact", publish_then_fail_once)
        first = AgentRuntimeRunner(spec).run()
        result["run_state"]["first_run"] = {
            "job_status": first.job_status,
            "job_disposition": first.job_disposition,
            "failed_stage": first.failed_stage,
            "workspace_path": first.workspace_path,
            "completed_stages": list(first.completed_stages),
            "message": first.message,
        }

        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        records = registry.list_records()
        branch_records = [row for row in records if row.artifact_role == "arbitration_ready_branch"]
        assert branch_records, "the direct-fit path completed without a Registry R4 overflow branch"
        branch_record = branch_records[-1]
        assert branch_record.status == "ready"
        assert branch_record.artifact_type == "outline_artifact" and branch_record.artifact_version == "v3"
        assert branch_record.metadata.get("arbitration_kind") == "arbitration_ready_branch"
        assert len(branch_record.content_hash) == 64
        assert branch_record.depends_on
        assert all(dependency.artifact_id and len(dependency.content_hash) == 64
                   for dependency in branch_record.depends_on)
        registry.verify_ready_artifact_closure(branch_record)
        branch = _payload(branch_record.path)
        assert branch.get("schema_version") == "arbitration-ready-branch/v1"
        for field in ("source_request_hash", "legacy_plan_hash", "legacy_ledger_hash"):
            value = str(branch.get(field) or "")
            assert len(value) == 64 and all(char in "0123456789abcdef" for char in value), (field, value)
        assert int(branch.get("effective_input_cap") or 0) == int(case["effective_input_cap"])
        branch_measurement = branch.get("measurement")
        assert isinstance(branch_measurement, Mapping)
        assert int(branch_measurement.get("estimated_input_tokens") or 0) > int(case["effective_input_cap"]), (
            "the packed final request fit the cap, so this would not prove real overflow",
            branch_measurement,
        )
        assert branch_measurement.get("within_budget") is False
        result["run_state"]["r4_branch_readback"] = {
            "artifact_id": branch_record.artifact_id,
            "artifact_role": branch_record.artifact_role,
            "artifact_type": branch_record.artifact_type,
            "artifact_version": branch_record.artifact_version,
            "status": branch_record.status,
            "content_hash": branch_record.content_hash,
            "dependency_count": len(branch_record.depends_on),
            "schema_version": branch.get("schema_version"),
            "source_request_hash": branch.get("source_request_hash"),
            "legacy_plan_hash": branch.get("legacy_plan_hash"),
            "legacy_ledger_hash": branch.get("legacy_ledger_hash"),
            "effective_input_cap": branch.get("effective_input_cap"),
            "estimated_input_tokens": branch_measurement.get("estimated_input_tokens"),
            "within_budget": branch_measurement.get("within_budget"),
        }
        assert fault["injected"] is True, "fault injection did not reach the first real overflow validated result"
        assert fault["artifact_role"] == "provider_validated_result"
        assert fault["request_schema"] == ASSESSMENT_INPUT_SCHEMA
        assert first.job_status == "failed", first
        assert first.failed_stage == "outline", first

        # Resume the same durable job with its original config, endpoint, provider
        # ledger, and aggregate budget after disabling the one-shot fault.
        monkeypatch.setattr(outline_executor_module, "publish_json_artifact", original_publish)
        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        recovered = control.resume(workspace=first.workspace_path)
        result["run_state"]["overflow_resume"] = {
            "job_status": recovered.get("job_status"),
            "completion_status": recovered.get("completion_status"),
            "job_disposition": recovered.get("job_disposition"),
            "canonical_ready": recovered.get("canonical_ready"),
            "completed_stages": list(recovered.get("completed_stages") or ()),
        }
        assert recovered.get("job_status") == "completed", recovered
        assert recovered.get("job_disposition") == "needs_review", recovered
        assert tuple(recovered.get("completed_stages") or ()) == ("source_intake", "analyze", "outline"), recovered
        assert recovered.get("canonical_ready") is False, recovered

        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        trace_rows = [
            dict(row) for row in server.trace  # type: ignore[attr-defined]
            if isinstance(row, Mapping) and row.get("kind") == "arbitration_overflow_model_fixture"
        ]
        calls = list(server.calls)  # type: ignore[attr-defined]
        fault_node_id = str(fault["transport_node_id"])
        assert fault_node_id
        assert len([call for call in calls if call.get("node_id") == fault_node_id]) == 1, (
            "the successfully POSTed overflow assessment was physically repeated after resume",
            fault_node_id,
            calls,
        )

        assessed_nodes = {
            str(row.get("transport_node_id") or "")
            for row in trace_rows
            if row.get("request_schema") == ASSESSMENT_INPUT_SCHEMA
        }
        navigation_nodes = {
            str(row.get("transport_node_id") or "")
            for row in trace_rows
            if row.get("request_schema") == EXTRACT_REQUEST_SCHEMA
        }
        assert assessed_nodes and assessed_nodes.issubset({str(call.get("node_id") or "") for call in calls})
        assert navigation_nodes and navigation_nodes.issubset({str(call.get("node_id") or "") for call in calls})

        records = registry.list_records()
        terminal_records = [row for row in records if row.artifact_role == "arbitration_overflow_terminal"]
        assert terminal_records, "Registry is missing the durable arbitration overflow terminal"
        terminal = terminal_records[-1]
        assert terminal.status == "ready"
        assert terminal.artifact_type == "outline_artifact" and terminal.artifact_version == "v3"
        assert terminal.metadata.get("arbitration_kind") == "arbitration_overflow_terminal"
        assert len(terminal.content_hash) == 64 and terminal.depends_on
        assert all(dependency.artifact_id and len(dependency.content_hash) == 64
                   for dependency in terminal.depends_on)
        registry.verify_ready_artifact_closure(terminal)
        terminal_payload = _payload(terminal.path)
        assert terminal_payload.get("schema_version") == "arbitration-overflow-terminal/v1"
        branch_ref = terminal_payload.get("branch_ref")
        assert isinstance(branch_ref, Mapping)
        assert branch_ref.get("artifact_id") == branch_record.artifact_id
        assert branch_ref.get("content_hash") == branch_record.content_hash
        assert any(
            dependency.artifact_id == branch_record.artifact_id
            and dependency.content_hash == branch_record.content_hash
            for dependency in terminal.depends_on
        ), "the Registry terminal does not close over its READY overflow branch"

        inspection = control.inspect(workspace=first.workspace_path)
        final_outline = next(
            row for row in inspection["artifacts"]
            if row.get("artifact_id") == "outline-v3:final_outline"
        )
        adoption = control.adopt(
            workspace=first.workspace_path,
            artifact_id="outline-v3:final_outline",
            actor="tests.test_arbitration_overflow_http",
            reason="explicitly adopt the source-closed outline after HTTP overflow recovery",
            expected_hash=str(final_outline["content_hash"]),
        )
        assert adoption.get("status") == "succeeded", adoption
        completed = control.resume(workspace=first.workspace_path)
        result["run_state"]["adopted_resume"] = {
            "job_status": completed.get("job_status"),
            "completion_status": completed.get("completion_status"),
            "canonical_ready": completed.get("canonical_ready"),
            "completed_stages": list(completed.get("completed_stages") or ()),
        }
        assert completed.get("job_status") == "completed", completed
        assert completed.get("completion_status") == "complete", completed
        assert completed.get("canonical_ready") is True, completed

        final_workspace, final_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        current_set = final_registry.resolve_current_artifact_set()
        assert current_set is not None and current_set.validation_status == "clean"
        assert terminal.status == "ready"
        completed_post_count = len(server.calls)  # type: ignore[attr-defined]
        ledger_path = Path(final_workspace.root_dir) / "logs" / "acceptance_budget_state_v1.json"
        original_ledger = ledger_path.read_bytes()
        completed_resume = control.resume(workspace=first.workspace_path)
        assert completed_resume.get("canonical_ready") is True, completed_resume
        assert len(server.calls) == completed_post_count  # type: ignore[attr-defined]
        export = control.export(workspace=first.workspace_path)
        result["run_state"]["export"] = dict(export)
        assert export.get("status") == "canonical_verified", export
        assert Path(str(export.get("bundle_path") or "")).is_file(), export
        first_bundle_hash = hashlib.sha256(Path(str(export["bundle_path"])).read_bytes()).hexdigest()
        repeat_export = control.export(workspace=first.workspace_path)
        assert repeat_export.get("status") == "canonical_verified", repeat_export
        bundle_hashes = [first_bundle_hash]
        for exported in (export, repeat_export):
            bundle_path = Path(str(exported["bundle_path"]))
            bundle_hashes.append(hashlib.sha256(bundle_path.read_bytes()).hexdigest())
            with zipfile.ZipFile(bundle_path) as archive:
                assert archive.testzip() is None
                checksums = json.loads(archive.read("checksums.json"))
                assert checksums
                for name, expected_hash in checksums.items():
                    assert hashlib.sha256(archive.read(name)).hexdigest() == expected_hash
        assert len(set(bundle_hashes)) == 1
        assert len(server.calls) == completed_post_count  # type: ignore[attr-defined]
        assert ledger_path.read_bytes() == original_ledger
        ledger = json.loads(original_ledger)
        assert ledger["calls_reserved"] == ledger["retry_attempts_used"] == 0
        assert ledger["reservations"] == []
        result["run_state"].update({
            "completed_resume": dict(completed_resume),
            "repeat_export": dict(repeat_export),
            "repeat_export_sha256": bundle_hashes[0],
            "completed_resume_and_exports_new_posts": 0,
            "completed_resume_and_exports_ledger_unchanged": True,
            "final_budget": ledger,
        })
        calls = list(server.calls)  # type: ignore[attr-defined]
        stages = Counter(str(call.get("stage") or "unknown") for call in calls)
        assert stages["stage1_reader"] == size
        assert stages["stage3_writer"] > 0
        assert stages["stage4_validator"] > 0
        assert len(calls) <= int(budget["max_provider_calls_total"])
        assert sum(int(call.get("requested_output_tokens") or 0) for call in calls) <= int(
            budget["max_output_tokens_total"]
        )
        assert time.monotonic() - started < int(budget["max_wall_seconds"])
        assert not server.errors  # type: ignore[attr-defined]
        assert not server.transport_errors  # type: ignore[attr-defined]
        fault["assessment_physical_post_count_after_resume"] = len(
            [call for call in calls if call.get("node_id") == fault_node_id]
        )
        result["run_state"].update({
            "workspace_path": str(workspace.root_dir),
            "final_workspace_path": str(final_workspace.root_dir),
            "registry_terminal_artifact_id": terminal.artifact_id,
            "registry_terminal_content_hash": terminal.content_hash,
            "registry_branch_artifact_id": branch_record.artifact_id,
            "packed_final_input_tokens": int(branch_measurement["estimated_input_tokens"]),
            "http_post_count": len(calls),
            "http_stage_counts": dict(stages),
            "http_assessment_nodes": sorted(assessed_nodes),
            "http_navigation_nodes": sorted(navigation_nodes),
            "fault_injection": dict(fault),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "fixture_trace": trace_rows,
        })
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = __import__("traceback").format_exc()
        raise
    finally:
        if server is not None:
            with server.lock:  # type: ignore[attr-defined]
                result["http_calls"] = list(server.calls)  # type: ignore[attr-defined]
                result["http_errors"] = list(server.errors)  # type: ignore[attr-defined]
                result["http_transport_errors"] = list(server.transport_errors)  # type: ignore[attr-defined]
                result["fixture_trace"] = list(server.trace)  # type: ignore[attr-defined]
            server.shutdown()
            if server_thread is not None:
                server_thread.join(timeout=10)
            server.server_close()
        result["fault_injection"] = dict(fault)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_result(result_path, result)

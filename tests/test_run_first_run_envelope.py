"""Run-start budget envelope acceptance on the production local-HTTP path.

These bounded cases complement the scale matrix. The fixture observes the
Registry before the first loopback POST; it does not replace planning,
admission, ProviderRuntime, durable budget state, or resume.
"""

from __future__ import annotations

import configparser
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import ProviderAggregateBudgetV2
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.artifact_registry import file_sha256
from tests.test_pr25_repair_v2 import (
    _LocalModelFixture,
    _runtime_config,
    _start_local_model_fixture,
    _write_source_pdf,
)

_FOUR_SOURCE_ROWS = (
    ("study-a", "Study A", "Treatment improved the measured outcome by four points."),
    ("study-b", "Study B", "Treatment improved the measured outcome by two points."),
    ("study-c", "Study C", "Treatment improved the measured outcome by one point."),
    ("study-d", "Study D", "Treatment did not change the measured outcome."),
)


def _budget_mapping(
    *,
    calls: int,
    output_tokens: int = 1_000_000,
    retry_attempts: int = 0,
    wall_seconds: float = 1_800,
) -> dict[str, Any]:
    return ProviderAggregateBudgetV2(
        max_provider_calls_total=calls,
        max_output_tokens_total=output_tokens,
        max_retry_attempts_total=retry_attempts,
        max_wall_seconds=wall_seconds,
    ).to_dict()


def _run_spec(
    tmp_path: Path,
    api_base: str,
    *,
    budget: dict[str, Any],
) -> RuntimeJobSpec:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    for paper_key, title, finding in _FOUR_SOURCE_ROWS:
        _write_source_pdf(pdf_dir / f"{paper_key}.pdf", title=title, finding=finding)
    output_root = tmp_path / "output"
    project_name = "run-first-envelope"
    job_id = f"run-envelope-{budget['max_provider_calls_total']}"
    return RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id=job_id,
        config=str(_runtime_config(tmp_path, api_base)),
        action="run_all",
        workspace_path=str(output_root / f"{project_name}__{job_id}"),
        queue_file=str(tmp_path / "queue.json"),
        metadata={
            "provider_budget": budget,
            "review_intent": {
                "required_comparisons": [
                    {
                        "paper_keys": ["studya_unknown_author", "studyb_unknown_author"],
                        "question": "Compare actual source findings within their recorded sample boundary.",
                        "relation_type": "reported_magnitude_contrast",
                    }
                ],
            },
        },
    )


def _run_until_budget_rejection(spec: RuntimeJobSpec) -> tuple[Any | None, str]:
    try:
        return AgentRuntimeRunner(spec).run(), ""
    except RuntimeRunnerError as exc:
        return None, str(exc)


def _envelope_record(registry: Any) -> Any:
    matches = [
        record for record in registry.list_records()
        if record.status == "ready"
        and record.artifact_type == "provider_run_envelope"
        and record.artifact_version == "v1"
    ]
    assert len(matches) == 1, [
        (record.artifact_id, record.artifact_type, record.artifact_version, record.status)
        for record in registry.list_records()
        if "run_envelope" in record.artifact_id or "run_envelope" in record.artifact_type
    ]
    return matches[0]


def _envelope_payload(record: Any) -> dict[str, Any]:
    encoded = json.loads(Path(record.path).read_text(encoding="utf-8"))
    payload = encoded.get("payload") if isinstance(encoded, dict) else None
    if not isinstance(payload, dict):
        payload = encoded
    envelope = ProviderRunEnvelopeV1.from_dict(payload)
    assert file_sha256(record.path) == record.content_hash
    return envelope.to_dict()


def _install_first_post_envelope_observer(server: Any, workspace_path: str) -> None:
    class EnvelopeObserver(_LocalModelFixture):
        def do_POST(self) -> None:
            with self.server.envelope_observer_lock:  # type: ignore[attr-defined]
                if not self.server.envelope_observer_checked:  # type: ignore[attr-defined]
                    self.server.envelope_observer_checked = True  # type: ignore[attr-defined]
                    try:
                        _workspace, registry = AgentRuntimeRunner._open_workspace(workspace_path)
                        record = _envelope_record(registry)
                        payload = _envelope_payload(record)
                        self.server.first_post_envelope_hash = record.content_hash  # type: ignore[attr-defined]
                        self.server.first_post_envelope_status = payload["admission_status"]  # type: ignore[attr-defined]
                    except (AssertionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        self.server.first_post_envelope_error = f"{type(exc).__name__}: {exc}"  # type: ignore[attr-defined]
            super().do_POST()

    server.RequestHandlerClass = EnvelopeObserver
    server.envelope_observer_lock = threading.Lock()  # type: ignore[attr-defined]
    server.envelope_observer_checked = False  # type: ignore[attr-defined]
    server.first_post_envelope_hash = ""  # type: ignore[attr-defined]
    server.first_post_envelope_status = ""  # type: ignore[attr-defined]
    server.first_post_envelope_error = ""  # type: ignore[attr-defined]


def _budget_state(workspace: Any, expected_budget: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in Path(workspace.root_dir).rglob("*.json"):
        if path.name.endswith(".identity.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("schema_version") or "").startswith("provider-aggregate-budget-")
            and payload.get("budget") == expected_budget
            and isinstance(payload.get("budget"), dict)
            and payload["budget"].get("schema_version") == ProviderAggregateBudgetV2.SCHEMA_VERSION
            and all(name in payload for name in ("calls_used", "output_tokens_used", "first_started_epoch", "absolute_deadline_epoch"))
        ):
            matches.append((path, payload))
    assert len(matches) == 1, [str(path) for path, _payload in matches]
    return matches[0]


@pytest.mark.integration
def test_zero_provider_call_budget_persists_envelope_and_blocks_before_loopback_post(
    tmp_path: Path,
) -> None:
    server, thread, api_base = _start_local_model_fixture()
    spec = _run_spec(tmp_path, api_base, budget=_budget_mapping(calls=0))
    _install_first_post_envelope_observer(server, spec.workspace_path)
    try:
        result, rejected = _run_until_budget_rejection(spec)
        _workspace, registry = AgentRuntimeRunner._open_workspace(spec.workspace_path)
        record = _envelope_record(registry)
        envelope = _envelope_payload(record)

        assert server.calls == []  # type: ignore[attr-defined]
        if result is None:
            assert "budget" in rejected.casefold(), rejected
        else:
            assert result.job_status in {"blocked", "failed", "paused"}, result
            assert not result.canonical_ready, result
            assert result.failed_stage == "run_admission", result
        assert envelope["budget_authority"] == "explicit_runtime_spec"
        assert envelope["admission_status"] == "blocked_budget"
        assert envelope["provider_budget"]["max_provider_calls_total"] == 0
        assert envelope["known_lower_bounds"]["provider_calls"] > 0
        _state_path, state = _budget_state(_workspace, envelope["provider_budget"])
        assert state["calls_used"] == 0
        assert state["output_tokens_used"] == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.integration
def test_four_paper_cold_read_known_lower_bound_exceeding_two_calls_blocks_before_post(
    tmp_path: Path,
) -> None:
    server, thread, api_base = _start_local_model_fixture()
    spec = _run_spec(tmp_path, api_base, budget=_budget_mapping(calls=2))
    _install_first_post_envelope_observer(server, spec.workspace_path)
    try:
        result, rejected = _run_until_budget_rejection(spec)
        _workspace, registry = AgentRuntimeRunner._open_workspace(spec.workspace_path)
        envelope = _envelope_payload(_envelope_record(registry))

        assert server.calls == []  # type: ignore[attr-defined]
        if result is None:
            assert "budget" in rejected.casefold(), rejected
        else:
            assert result.job_status in {"blocked", "failed", "paused"}, result
            assert not result.canonical_ready, result
            assert result.failed_stage == "run_admission", result
        assert envelope["admission_status"] == "blocked_budget"
        assert envelope["provider_budget"]["max_provider_calls_total"] == 2
        assert envelope["known_lower_bounds"]["provider_calls"] == 4
        _state_path, state = _budget_state(_workspace, envelope["provider_budget"])
        assert state["calls_used"] == 0
        assert state["output_tokens_used"] == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.integration
def test_sufficient_run_budget_is_visible_before_post_and_does_not_reset_on_resume(
    tmp_path: Path,
) -> None:
    server, thread, api_base = _start_local_model_fixture()
    # Replace the helper's default budget with a finite explicit allowance
    # large enough for this bounded local four-paper transaction.
    spec = _run_spec(
        tmp_path,
        api_base,
        budget=_budget_mapping(
            calls=200,
            output_tokens=2_000_000,
            retry_attempts=0,
            wall_seconds=1_800,
        ),
    )
    # A declared run allowance replaces the legacy default stage call cap.
    # The saved configuration and all source/input/output limits stay bound.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(spec.config, encoding="utf-8")
    parser["OutlineStability"]["max_provider_calls"] = "24"
    with Path(spec.config).open("w", encoding="utf-8") as handle:
        parser.write(handle)
    _install_first_post_envelope_observer(server, spec.workspace_path)
    try:
        first = AgentRuntimeRunner(spec).run()
        assert first.job_status == "completed", first
        assert first.job_disposition == "needs_review", first
        assert first.completed_stages == ("source_intake", "analyze", "outline"), first
        assert server.calls, "sufficient run budget did not admit the first loopback POST"  # type: ignore[attr-defined]
        assert server.first_post_envelope_error == ""  # type: ignore[attr-defined]
        assert server.first_post_envelope_hash  # type: ignore[attr-defined]
        assert server.first_post_envelope_status == "ready_with_jit"  # type: ignore[attr-defined]
        _outline_workspace, outline_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        plans = [record for record in outline_registry.list_records()
                 if record.artifact_type == "outline_provider_call_plan" and record.status == "ready"]
        assert plans
        for record in plans:
            plan = json.loads(Path(record.path).read_text(encoding="utf-8"))
            assert plan["max_provider_calls"] == 200

        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        envelope_record = _envelope_record(registry)
        envelope = _envelope_payload(envelope_record)
        assert envelope["budget_authority"] == "explicit_runtime_spec"
        assert envelope["admission_status"] == "ready_with_jit"
        assert envelope_record.content_hash == server.first_post_envelope_hash  # type: ignore[attr-defined]
        state_path, before_resume = _budget_state(workspace, envelope["provider_budget"])
        assert before_resume["calls_used"] > 0
        assert before_resume["calls_used"] == len(server.calls)  # type: ignore[attr-defined]
        first_started_epoch = before_resume["first_started_epoch"]
        deadline_epoch = before_resume["absolute_deadline_epoch"]
        calls_before_resume = len(server.calls)  # type: ignore[attr-defined]

        control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
        inspection = control.inspect(workspace=first.workspace_path)
        final_outline = next(
            item for item in inspection["artifacts"]
            if item["artifact_id"] == "outline-v3:final_outline"
        )
        adoption = control.adopt(
            workspace=first.workspace_path,
            artifact_id="outline-v3:final_outline",
            actor="tests.test_run_first_run_envelope",
            reason="exercise later-stage just-in-time budget admission on local HTTP",
            expected_hash=str(final_outline["content_hash"]),
        )
        assert adoption["status"] == "succeeded", adoption
        resumed = control.resume(workspace=first.workspace_path)
        assert resumed["job_status"] == "completed", resumed
        assert resumed["completed_stages"] == (
            "source_intake", "analyze", "outline", "review", "validate",
        ), resumed
        assert server.validator_calls == 3  # type: ignore[attr-defined]

        registry.reload()
        envelope_after_resume = _envelope_record(registry)
        after_resume = json.loads(state_path.read_text(encoding="utf-8"))
        assert after_resume["budget"] == envelope["provider_budget"]
        assert envelope_after_resume.content_hash == envelope_record.content_hash
        assert file_sha256(envelope_after_resume.path) == envelope_after_resume.content_hash
        assert after_resume["budget"] == before_resume["budget"]
        assert after_resume["acceptance_run_id"] == before_resume["acceptance_run_id"]
        assert after_resume["first_started_epoch"] == first_started_epoch
        assert after_resume["absolute_deadline_epoch"] == deadline_epoch
        assert after_resume["calls_used"] >= before_resume["calls_used"]
        assert after_resume["calls_used"] == len(server.calls)  # type: ignore[attr-defined]
        assert len(server.calls) > calls_before_resume  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

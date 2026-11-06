"""Real bounded loopback Runtime coverage for finite downstream continuation."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from runtime.control_plane import ReviewControlPlane
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderRuntimeContractError,
    hash_json,
)
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from tests.test_pr25_repair_v2 import _start_local_model_fixture
from tests.test_run_first_run_envelope import (
    _budget_mapping,
    _envelope_record,
    _run_spec,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _assert_protected_unchanged(protected: dict[Path, bytes]) -> None:
    assert {str(path): file_sha256(path) for path in protected} == {
        str(path): hashlib.sha256(content).hexdigest()
        for path, content in protected.items()
    }
    assert all(path.read_bytes() == content for path, content in protected.items())


@pytest.mark.integration
def test_expired_parent_continues_through_public_runtime_and_export_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime.downstream_continuation import (
        bind_current_continuation,
        controller_for_continuation,
        load_current_continuation,
    )

    server, thread, api_base = _start_local_model_fixture()
    # This fixture normally injects a citation defect for repair tests. This
    # positive case uses its existing source-correct response branch.
    server.defect_sent = True
    origin = urlsplit(api_base)
    original_send = requests.Session.send

    def loopback_only(session, request, **kwargs):
        target = urlsplit(request.url)
        assert (target.scheme, target.hostname, target.port) == (
            origin.scheme, origin.hostname, origin.port,
        ), "Runtime attempted a nonfixture HTTP request"
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", loopback_only)
    parent_budget = _budget_mapping(calls=200, output_tokens=2_000_000, wall_seconds=1_800)
    spec = _run_spec(tmp_path, api_base, budget=parent_budget)
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    try:
        first = AgentRuntimeRunner(spec).run()
        assert first.job_status == "completed" and first.job_disposition == "needs_review", first
        assert first.completed_stages == ("source_intake", "analyze", "outline")
        workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
        outline = registry.get("outline-v3:final_outline")
        assert outline is not None
        adopted = control.adopt(
            workspace=first.workspace_path, artifact_id=outline.artifact_id,
            expected_hash=outline.content_hash, actor="tests.downstream_continuation_runtime",
            reason="bounded loopback continuation test uses the verified adopted Outline",
        )
        assert adopted["status"] == "succeeded", adopted

        parent_state_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
        parent_identity_path = parent_state_path.with_name(parent_state_path.name + ".identity.json")
        parent = _read(parent_state_path)
        assert parent["calls_used"] == len(server.calls) > 0
        assert parent["calls_reserved"] == parent["output_tokens_reserved"] == parent["retry_attempts_used"] == 0
        original_spec_record = registry.get("runtime_job_spec")
        assert original_spec_record is not None
        original_envelope = _envelope_record(registry)
        protected = {path: path.read_bytes() for path in (
            parent_state_path, parent_identity_path, Path(original_spec_record.path),
            Path(original_envelope.path), Path(spec.config),
        )}
        first_started = parent["first_started_epoch"]
        original_deadline = parent["absolute_deadline_epoch"]
        posts_before = len(server.calls)
        reader_before = server.reader_calls
        writer_before = server.review_writer_calls
        validator_before = server.validator_calls

        # Expire the actual persisted parent under a controlled clock, without
        # rewriting a byte of its state, identity, spec, or envelope.
        actual_time = time.time
        offset = max(0.0, original_deadline - actual_time()) + 5.0
        with monkeypatch.context() as clock:
            clock.setattr(time, "time", lambda: actual_time() + offset)
            assert time.time() > original_deadline
            additional_budget = ProviderAggregateBudgetV2(
                max_provider_calls_total=parent_budget["max_provider_calls_total"] - parent["calls_used"],
                max_output_tokens_total=parent_budget["max_output_tokens_total"] - parent["output_tokens_used"],
                max_retry_attempts_total=0,
                max_wall_seconds=600,
            ).to_dict()
            prepared = control.authorize_downstream(
                workspace=first.workspace_path, budget=additional_budget,
                actor="tests.downstream_continuation_runtime",
                reason="new finite local Writer/Validator allowance after the parent clock expired",
                authorization_reference="pytest:expired-parent-downstream-loopback",
            )
            assert isinstance(prepared, dict)
            _assert_protected_unchanged(protected)
            registry.reload()
            grant = load_current_continuation(workspace, registry)
            assert grant is not None
            assert grant.carried_usage == {
                key: parent[key] for key in ("calls_used", "output_tokens_used", "retry_attempts_used")
            }
            assert grant.budget.to_dict() == additional_budget
            assert grant.original_spec_budget_hash == hash_json(parent_budget)
            assert grant.first_started_epoch > original_deadline
            assert grant.absolute_deadline_epoch > grant.first_started_epoch
            assert grant.run_id != parent["acceptance_run_id"]
            assert grant.state_path.resolve() != parent_state_path.resolve()
            assert tuple(grant.allow_provider_stages) == ("stage3_review", "stage4_validate")
            new_before = _read(grant.state_path)
            assert new_before["calls_used"] == new_before["output_tokens_used"] == 0
            assert new_before["first_started_epoch"] == grant.first_started_epoch
            assert new_before["absolute_deadline_epoch"] == grant.absolute_deadline_epoch

            with bind_current_continuation(grant):
                controller = controller_for_continuation(grant)
                unchanged_new_state = grant.state_path.read_bytes()
                for forbidden_context in (
                    None,
                    {"job_id": grant.job_id, "stage_name": "stage1_analyze"},
                    {"job_id": grant.job_id, "stage_name": "outline_v3"},
                    {"job_id": "foreign-job", "stage_name": "stage3_review"},
                ):
                    with pytest.raises(ProviderRuntimeContractError, match="stage|continuation|downstream"):
                        controller.admit(requested_output_tokens=1, context=forbidden_context)
                assert grant.state_path.read_bytes() == unchanged_new_state

            complete = control.resume(workspace=first.workspace_path)
            assert complete["completion_status"] == "complete", complete
            assert complete["canonical_ready"] is True, complete
            _assert_protected_unchanged(protected)
            registry.reload()
            original_envelope_after = registry.get(original_envelope.artifact_id)
            original_spec_after = registry.get(original_spec_record.artifact_id)
            assert original_envelope_after is not None and original_envelope_after.content_hash == original_envelope.content_hash
            assert original_spec_after is not None and original_spec_after.content_hash == original_spec_record.content_hash
            assert server.reader_calls == reader_before
            assert server.review_writer_calls > writer_before
            assert server.validator_calls > validator_before
            new_posts = server.calls[posts_before:]
            assert new_posts
            for post in new_posts:
                assert post["model"] in {"writer-local", "validator-local"}
                if post["model"] == "writer-local":
                    request = json.loads(post["user_text"])
                    assert "writer_task_scope" in request, "Outline critic reused the Writer route"

            new_after = _read(grant.state_path)
            assert new_after["schema_version"] == "provider-aggregate-budget-v5"
            assert new_after["calls_used"] == len(new_posts)
            assert new_after["retry_attempts_used"] == new_after["calls_reserved"] == new_after["output_tokens_reserved"] == 0
            assert new_after["first_started_epoch"] == grant.first_started_epoch
            assert new_after["absolute_deadline_epoch"] == grant.absolute_deadline_epoch
            assert parent["calls_used"] + new_after["calls_used"] <= parent_budget["max_provider_calls_total"]
            assert parent["output_tokens_used"] + new_after["output_tokens_used"] <= parent_budget["max_output_tokens_total"]

            exported = control.export(workspace=first.workspace_path)
            assert exported["status"] == "canonical_verified", exported
            bundle_hash = file_sha256(exported["bundle_path"])
            closed_posts = len(server.calls)
            closed_new_usage = _read(grant.state_path)
            replay = control.resume(workspace=first.workspace_path)
            assert replay["completion_status"] == "complete", replay
            assert len(server.calls) == closed_posts, "completed continuation emitted another POST"
            assert _read(grant.state_path)["calls_used"] == closed_new_usage["calls_used"]
            repeated = control.export(workspace=first.workspace_path)
            assert repeated["status"] == "canonical_verified", repeated
            assert repeated["artifact_id"] == exported["artifact_id"]
            assert file_sha256(repeated["bundle_path"]) == bundle_hash
            _assert_protected_unchanged(protected)

        parent_after = _read(parent_state_path)
        assert parent_after["first_started_epoch"] == first_started
        assert parent_after["absolute_deadline_epoch"] == original_deadline
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from outline.v3_executor import OutlineV3Executor
from runtime.cancellation import CancellationRequestStore
from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import (
    ProviderCancellationRequested,
    RuntimeControlIdentityV1,
    bind_runtime_control_context,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace
from tests.test_outline_v3_semantic_execution import _summary


def _executor(
    tmp_path: Path,
    *,
    job_id: str,
    provider: Any,
) -> tuple[OutlineV3Executor, JobWorkspace, ArtifactRegistry]:
    workspace = JobWorkspace.create(str(tmp_path), "outline-cancel", job_id)
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    profile = ProviderContextProfile.conservative(
        provider="openai",
        model="fixture-chat",
        endpoint_type="chat_completions",
        model_context_limit=128_000,
        max_output_tokens=4_096,
    )
    executor = OutlineV3Executor(
        job_id=workspace.job_id,
        summaries=[
            _summary("paper-a", "Study A", "The treatment improved the outcome."),
            _summary("paper-b", "Study B", "The treatment improved it in another context."),
        ],
        workspace=workspace,
        artifact_registry=registry,
        provider=provider,
        provider_profile=profile,
        stability_mode="off",
        semantic_transport_retries=0,
        max_provider_calls=64,
        max_estimated_total_tokens=5_000_000,
    )
    assert executor.semantic_provider_synthesis_enabled
    return executor, workspace, registry


def test_outline_run_propagates_durable_provider_cancellation_before_transport(
    tmp_path: Path,
) -> None:
    provider_calls: list[str] = []

    def forbidden_transport(node_id: str, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        provider_calls.append(node_id)
        raise AssertionError("durable cancellation must stop before provider transport")

    executor, workspace, registry = _executor(
        tmp_path,
        job_id="outline-cancel-run",
        provider=forbidden_transport,
    )
    identity = RuntimeControlIdentityV1(
        job_id=workspace.job_id,
        workspace_path=workspace.paths.root_dir,
    )
    cancellation = CancellationRequestStore(workspace, registry)
    cancellation.request(requested_by="test", reason="stop before semantic provider admission")

    try:
        with bind_runtime_control_context(identity, workspace=workspace, registry=registry):
            result = executor.run()
    except ProviderCancellationRequested:
        pass
    else:
        assert provider_calls == [], "durable cancellation must stop before provider transport"
        assert any("CANCEL_REQUESTED" in item for item in result.diagnostics), result.diagnostics
        pytest.fail(
            "OutlineV3Executor.run converted ProviderCancellationRequested into "
            f"status={result.status}: {result.diagnostics}"
        )

    assert provider_calls == []
    assert cancellation.is_requested()


def test_outline_node_does_not_record_provider_cancellation_as_failure(
    tmp_path: Path,
) -> None:
    executor, _workspace, _registry = _executor(
        tmp_path,
        job_id="outline-cancel-node",
        provider=lambda _node_id, _request: {},
    )

    def cancelled_factory() -> Any:
        raise ProviderCancellationRequested("durable cancellation request")

    with pytest.raises(ProviderCancellationRequested, match="durable cancellation request"):
        executor._run_node(
            "outline_evidence_views",
            cancelled_factory,
            expected_binding={"dependency_hashes": {}},
        )

    dag = executor._node_store.load()
    assert dag is not None
    assert dag.get("outline_evidence_views").status == "pending"

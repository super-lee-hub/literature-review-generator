"""A topic pilot acknowledges only the provider route it can execute."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_routes import ReachableProviderRoute, ReachableProviderRoutePlan
from runtime.stage_planning import build_stage_plan
from runtime.trust_admission import build_external_host_policy


def _fixture():
    config = {
        "Outline_API": {
            "api_base": "https://api.yhlxj.ai/v1",
            "provider_family": "claude_chat_reasoning",
            "endpoint_type": "chat_completions",
            "model": "claude-opus-5",
        },
        "Backup_Reader_API": {
            "api_base": "https://ai.saigou.work/v1",
            "provider_family": "openai_responses",
            "endpoint_type": "responses",
            "model": "gpt-5.6-sol",
        },
        "Preprocess": {
            "parser_mode": "remote",
            "primary_parser": "mineru_remote",
            "mineru_base_url": "https://mineru.net/api/v4",
        },
    }
    stage_plan = build_stage_plan(
        action="generate_outline",
        requested_stages=("outline",),
        validation_enabled=False,
    )
    route_plan = ReachableProviderRoutePlan(
        action="generate_outline",
        stage_plan=stage_plan,
        routes=(
            ReachableProviderRoute(
                stage="outline",
                semantic_role="candidate_provider_generation",
                section_name="Outline_API",
                provider_family="claude_chat_reasoning",
                model="claude-opus-5",
                endpoint_type="chat_completions",
                resolved=True,
            ),
            ReachableProviderRoute(
                stage="outline",
                semantic_role="evidence_critique",
                section_name="Backup_Reader_API",
                provider_family="openai_responses",
                model="gpt-5.6-sol",
                endpoint_type="responses",
                resolved=True,
            ),
        ),
    )
    policy = build_external_host_policy(
        config,
        route_plan,
        provider_sections=("Outline_API",),
        include_mineru=False,
    )
    now = datetime.now(timezone.utc)
    acknowledgement = {
        "schema_version": "external-host-acknowledgement-v2",
        "version": 2,
        "acknowledged": True,
        "hosts": list(policy.required_hosts),
        "route_fingerprint": policy.route_fingerprint,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    return config, route_plan, acknowledgement


def _spec(
    acknowledgement: dict, *, pilot: bool, stages: list[str] | None = None
) -> RuntimeJobSpec:
    metadata = {
        "requested_stages": stages if stages is not None else ["outline"],
        "external_host_acknowledgement": acknowledgement,
    }
    if pilot:
        metadata["outline_pilot"] = {"schema_version": "outline-topic-pilot/v1"}
    return RuntimeJobSpec(
        project_name="host-scope-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="D:/tmp/empty"),
        config="unused.ini",
        action="generate_outline",
        metadata=metadata,
    )


def test_topic_pilot_admits_only_generation_host() -> None:
    config, route_plan, acknowledgement = _fixture()
    with (
        patch("runtime.control_plane.load_config", return_value=config),
        patch("runtime.control_plane.build_reachable_provider_route_plan", return_value=route_plan),
    ):
        admission = ReviewControlPlane(repo_root=Path.cwd())._admit_runtime_spec_external_hosts(
            _spec(acknowledgement, pilot=True)
        )
    assert admission["required_hosts"] == ["api.yhlxj.ai"]
    assert admission["policy"]["required_hosts"] == ["api.yhlxj.ai"]


def test_normal_outline_keeps_full_host_admission() -> None:
    config, route_plan, acknowledgement = _fixture()
    with (
        patch("runtime.control_plane.load_config", return_value=config),
        patch("runtime.control_plane.build_reachable_provider_route_plan", return_value=route_plan),
    ):
        with pytest.raises(ControlPlaneError, match="external host admission failed"):
            ReviewControlPlane(repo_root=Path.cwd())._admit_runtime_spec_external_hosts(
                _spec(acknowledgement, pilot=False)
            )


def test_pilot_with_wider_stage_declaration_keeps_full_host_admission() -> None:
    config, route_plan, acknowledgement = _fixture()
    with (
        patch("runtime.control_plane.load_config", return_value=config),
        patch("runtime.control_plane.build_reachable_provider_route_plan", return_value=route_plan),
    ):
        with pytest.raises(ControlPlaneError, match="external host admission failed"):
            ReviewControlPlane(repo_root=Path.cwd())._admit_runtime_spec_external_hosts(
                _spec(
                    acknowledgement,
                    pilot=True,
                    stages=["source_intake", "outline"],
                )
            )


def test_topic_pilot_rejects_extra_acknowledged_host() -> None:
    config, route_plan, acknowledgement = _fixture()
    acknowledgement["hosts"].append("ai.saigou.work")
    with (
        patch("runtime.control_plane.load_config", return_value=config),
        patch("runtime.control_plane.build_reachable_provider_route_plan", return_value=route_plan),
    ):
        with pytest.raises(ControlPlaneError, match="external host admission failed"):
            ReviewControlPlane(repo_root=Path.cwd())._admit_runtime_spec_external_hosts(
                _spec(acknowledgement, pilot=True)
            )

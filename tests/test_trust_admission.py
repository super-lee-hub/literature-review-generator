from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import RuntimeExecutionResult
from runtime.provider_routes import build_reachable_provider_route_plan
from runtime.trust_admission import (
    ACKNOWLEDGEMENT_SCHEMA_VERSION,
    ExternalHostAdmissionError,
    acknowledgement_from_values,
    build_external_host_policy,
    validate_external_host_acknowledgement,
)


def _config(*, writer_host: str = "https://writer.example.test/v1") -> dict[str, dict[str, str]]:
    return {
        "Application": {"config_schema": "4"},
        "Paths": {"output_path": "output"},
        "Primary_Reader_API": {
            "api_key": "sk-primary-reader",
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
        },
        "Writer_API": {
            "api_key": "sk-writer-api",
            "model": "gpt-5.6-sol",
            "api_base": writer_host,
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
        },
        "Validation": {"review_enabled": "false"},
    }


def _policy(config: dict[str, dict[str, str]]):
    plan = build_reachable_provider_route_plan(
        config,
        action="generate_review",
        requested_stages=("review",),
    )
    return build_external_host_policy(config, plan)


def test_external_provider_requires_exact_fingerprinted_acknowledgement() -> None:
    policy = _policy(_config())
    assert policy.required_hosts == ("writer.example.test",)
    with pytest.raises(ExternalHostAdmissionError, match="required"):
        validate_external_host_acknowledgement(policy, None)

    acknowledgement = acknowledgement_from_values(
        policy,
        acknowledged=True,
        hosts=policy.required_hosts,
    )
    result = validate_external_host_acknowledgement(policy, acknowledgement)
    assert result["acknowledged"] is True
    assert result["route_fingerprint"] == policy.route_fingerprint


def test_acknowledgement_rejects_extra_host_and_stale_policy() -> None:
    policy = _policy(_config())
    acknowledgement = acknowledgement_from_values(
        policy,
        acknowledged=True,
        hosts=[*policy.required_hosts, "extra.example.test"],
    )
    with pytest.raises(ExternalHostAdmissionError, match="exactly match"):
        validate_external_host_acknowledgement(policy, acknowledgement)

    acknowledgement = acknowledgement_from_values(
        policy,
        acknowledged=True,
        hosts=policy.required_hosts,
        route_fingerprint="0" * 64,
    )
    with pytest.raises(ExternalHostAdmissionError, match="stale"):
        validate_external_host_acknowledgement(policy, acknowledgement)


def test_official_provider_requires_no_external_acknowledgement() -> None:
    config = _config(writer_host="https://api.openai.com/v1")
    policy = _policy(config)
    assert policy.required_hosts == ()
    result = validate_external_host_acknowledgement(policy, None)
    assert result["required"] is False


def test_remote_mineru_hosts_are_part_of_the_exact_policy() -> None:
    config = _config(writer_host="https://api.openai.com/v1")
    config["Preprocess"] = {
        "parser_mode": "remote",
        "primary_parser": "mineru_remote",
        "mineru_base_url": "https://mineru.example.test/api/v4",
        "mineru_allowed_url_hosts": "uploads.example.test,results.example.test",
    }
    policy = _policy(config)
    assert set(policy.required_hosts) == {
        "mineru.example.test",
        "uploads.example.test",
        "results.example.test",
        "mineru.oss-cn-shanghai.aliyuncs.com",
        "cdn-mineru.openxlab.org.cn",
    }
    acknowledgement = acknowledgement_from_values(
        policy,
        acknowledged=True,
        hosts=policy.required_hosts,
    )
    acknowledgement["schema_version"] = ACKNOWLEDGEMENT_SCHEMA_VERSION
    assert validate_external_host_acknowledgement(policy, acknowledgement)["acknowledged"] is True


def test_direct_run_blocks_external_host_before_constructing_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.ini"
    output_path = tmp_path / "output"
    config_path.write_text(
        "\n".join(
            (
                "[Application]",
                "config_schema = 4",
                "",
                "[Paths]",
                f"output_path = {output_path}",
                "",
                "[Primary_Reader_API]",
                "api_key = test-provider-key",
                "model = test-reader",
                "api_base = https://reader.example.test/v1",
                "endpoint_type = responses",
                "provider_family = openai_responses",
                "",
                "[Stage1_Input]",
                "primary_reader_only = true",
            )
        ),
        encoding="utf-8",
    )
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    spec = RuntimeJobSpec(
        project_name="trust-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(papers)),
        config=str(config_path),
        action="analyze",
        metadata={"requested_stages": ["analyze"]},
    )
    spec_path = tmp_path / "runtime.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
    control = ReviewControlPlane(repo_root=tmp_path)

    import runtime.control_plane as control_plane

    class UnexpectedRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("runner must not be constructed before trust admission")

    monkeypatch.setattr(control_plane, "AgentRuntimeRunner", UnexpectedRunner)
    with pytest.raises(ControlPlaneError, match="external host admission"):
        control.run(spec_path)


def test_direct_run_uses_durable_external_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            (
                "[Application]",
                "config_schema = 4",
                "",
                "[Paths]",
                f"output_path = {tmp_path / 'output'}",
                "",
                "[Primary_Reader_API]",
                "api_key = test-provider-key",
                "model = test-reader",
                "api_base = https://reader.example.test/v1",
                "endpoint_type = responses",
                "provider_family = openai_responses",
                "",
                "[Stage1_Input]",
                "primary_reader_only = true",
            )
        ),
        encoding="utf-8",
    )
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    from config_loader import load_config

    normalized = load_config(str(config_path), action="analyze", requested_stages=("analyze",))
    route_plan = build_reachable_provider_route_plan(
        normalized,
        action="analyze",
        requested_stages=("analyze",),
    )
    policy = build_external_host_policy(normalized, route_plan)
    acknowledgement = acknowledgement_from_values(
        policy,
        acknowledged=True,
        hosts=policy.required_hosts,
    )
    spec = RuntimeJobSpec(
        project_name="trust-test",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(papers)),
        config=str(config_path),
        action="analyze",
        metadata={
            "requested_stages": ["analyze"],
            "external_host_acknowledgement": acknowledgement,
        },
    )
    spec_path = tmp_path / "runtime.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    import runtime.control_plane as control_plane

    class Runner:
        def __init__(self, _spec) -> None:
            pass

        def run(self):
            return RuntimeExecutionResult(
                job_id="trust-test",
                workspace_path=str(tmp_path / "workspace"),
                job_status="completed",
                job_disposition="completed",
                requires_attention=False,
                attempt_number=1,
                resumed_from_attempt=None,
                completed_stages=("analyze",),
                failed_stage=None,
                job_outcome_path="",
                completion_status="complete",
                completion_reasons=(),
                completion_evidence_hash="",
                summary_schema_ready=True,
                visual_qualification_ready=True,
                stage1_authority_ready=True,
                stage1_reuse_eligible=True,
                canonical_ready=True,
            )

    monkeypatch.setattr(control_plane, "AgentRuntimeRunner", Runner)
    result = ReviewControlPlane(repo_root=tmp_path).run(spec_path)
    assert result["external_host_admission"]["acknowledged"] is True

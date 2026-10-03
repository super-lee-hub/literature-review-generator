from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.artifact_registry import file_sha256
from runtime.provider_runtime import hash_json
from tests.test_current_runtime_full_e2e import _test_config


def _spec(tmp_path: Path) -> RuntimeJobSpec:
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "source.pdf").write_bytes(b"%PDF-1.4\nsynthetic source identity only\n")
    return RuntimeJobSpec(
        project_name="config-binding", job_id="config-binding-job",
        config=str(_test_config(tmp_path)),
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(papers)),
        action="run_all", queue_file=str(tmp_path / "queue.json"),
    )


def test_runner_publishes_raw_config_and_effective_config_as_distinct_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path)
    observed: dict[str, object] = {}
    original_bootstrap = AgentRuntimeBridge.bootstrap

    def bootstrap(self, *args, **kwargs):
        session = original_bootstrap(self, *args, **kwargs)
        observed["session"] = session
        return session

    def stop_before_intake(self):
        raise RuntimeError("synthetic stop before any provider work")

    monkeypatch.setattr(AgentRuntimeBridge, "bootstrap", bootstrap)
    monkeypatch.setattr(AgentRuntimeBridge, "build_source_bundle", stop_before_intake)
    result = AgentRuntimeRunner(spec).run()
    assert result.failed_stage == "source_intake"
    session = observed["session"]
    record = session.context.registry.get("runtime_job_spec")
    assert record is not None and record.status == "ready"
    session.context.registry.verify_ready_artifact_closure(record)
    payload = json.loads(Path(record.path).read_text(encoding="utf-8"))
    binding = record.metadata["config_snapshot_binding"]
    assert binding["schema_version"] == "runtime-config-source/v1"
    assert binding["config_source_id"] == str(Path(spec.config).resolve())
    assert binding["config_source_sha256"] == file_sha256(spec.config)
    assert binding["effective_config_sha256"] == session.context.fingerprint_bundle["config_hash"]
    assert binding["normalized_spec_payload_sha256"] == hash_json(payload)
    assert binding["config_source_sha256"] != binding["effective_config_sha256"]
    assert not any(r.artifact_type == "provider_receipt_ledger" for r in session.context.registry.list_records())


def test_config_drift_during_bootstrap_is_rejected_before_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path)
    original_bootstrap = AgentRuntimeBridge.bootstrap
    intake_calls: list[str] = []

    def bootstrap(self, *args, **kwargs):
        session = original_bootstrap(self, *args, **kwargs)
        with Path(spec.config).open("a", encoding="utf-8") as stream:
            stream.write("\n# changed after effective settings were loaded\n")
        return session

    def unexpected_intake(self):
        intake_calls.append("intake")
        raise AssertionError("source work must not begin after config-source drift")

    monkeypatch.setattr(AgentRuntimeBridge, "bootstrap", bootstrap)
    monkeypatch.setattr(AgentRuntimeBridge, "build_source_bundle", unexpected_intake)
    with pytest.raises(RuntimeRunnerError, match="configuration source changed during runtime bootstrap"):
        AgentRuntimeRunner(spec).run()
    assert intake_calls == []

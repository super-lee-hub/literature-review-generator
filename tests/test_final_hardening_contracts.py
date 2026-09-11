from __future__ import annotations

import configparser
from pathlib import Path

import pytest

import ai_interface
import services.durable_io as durable_io
from config_loader import load_config
from config_validator import validate_all_config
from services.configuration_service import (
    ConfigurationPersistenceError,
    ensure_config_sections,
    save_config_and_env,
)
from services.credential_provenance import CredentialConflictError, resolve_credentials
from services.durable_io import AtomicReplaceTimeoutError
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry, UnverifiedDependency
from services.job_workspace import JobWorkspace, WorkspacePathError
from services.queue_service import (
    InProcessQueueService,
    PersistentQueueService,
    QueueCorruption,
)
from ai_interface import build_provider_transport_preflight, classify_provider_endpoint
from runtime.provider_runtime import ProviderRuntime


def _write_config(path: Path, values: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, items in values.items():
        parser[section] = items
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def test_credential_sources_fail_closed_on_conflict(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_PRIMARY_READER_API=dotenv-secret\n", encoding="utf-8")
    with pytest.raises(CredentialConflictError, match="Primary_Reader_API"):
        resolve_credentials(
            {"Primary_Reader_API": {"api_key": "config-secret"}},
            config_path=tmp_path / "config.ini",
            environ={"LLM_PRIMARY_READER_API": "process-secret"},
            dotenv_path=env_path,
        )


def test_template_credentials_are_rejected_for_required_runtime_roles() -> None:
    config = ensure_config_sections({})
    config["Primary_Reader_API"]["api_key"] = "YOUR_PRIMARY_READER_API_KEY_HERE"

    valid, messages = validate_all_config(
        config,
        required_provider_sections=("Primary_Reader_API",),
    )

    assert valid is False
    assert any("模板占位符" in message for message in messages)


def test_analyze_primary_reader_only_does_not_require_backup_or_writer(tmp_path: Path) -> None:
    config = ensure_config_sections({})
    config["Paths"]["output_path"] = str(tmp_path / "output")
    config["Stage1_Input"]["primary_reader_only"] = "true"
    config["Primary_Reader_API"]["api_key"] = "sk-primary-runtime"
    config_path = tmp_path / "config.ini"
    _write_config(config_path, config)

    loaded = load_config(
        str(config_path),
        action="analyze",
        requested_stages=("analyze",),
    )

    assert loaded["Primary_Reader_API"]["api_key"] == "sk-primary-runtime"
    assert loaded["Backup_Reader_API"]["api_key"] == "loaded_from_.env_file"


def test_provider_preflight_builds_formal_route_without_network() -> None:
    details = build_provider_transport_preflight(
        {
            "api_key": "sk-primary-runtime",
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "transport_retries": "2",
            "read_timeout_seconds": "60",
        },
        credential_source="process_env",
    )

    assert details["request_route"] == "https://api.deepseek.com/chat/completions"
    assert details["trust_env"] is False
    assert details["credential_source"] == "process_env"
    assert details["endpoint_classification"]["classification"] == "official_provider_host"
    assert details["request_byte_estimate"] > 0
    assert len(details["payload_sha256"]) == 64


def test_provider_classification_distinguishes_official_gateway_and_local() -> None:
    assert classify_provider_endpoint("https://api.deepseek.com", "deepseek")["classification"] == "official_provider_host"
    assert classify_provider_endpoint("https://gateway.example/v1", "deepseek")["classification"] == "third_party_gateway"
    assert classify_provider_endpoint("http://localhost:11434", "generic")["classification"] == "custom_local_endpoint"


def test_template_credential_fails_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("template credentials must not reach transport")

    monkeypatch.setattr(ai_interface, "_call_ai_api_detailed_uninstrumented", fail_if_called)
    result = ai_interface._call_ai_api_detailed(
        "prompt",
        {
            "api_key": "YOUR_PRIMARY_READER_API_KEY_HERE",
            "model": "test-model",
            "api_base": "https://provider.example/v1",
        },
        "system",
        provider_runtime=ProviderRuntime(test_only=True),
    )

    assert called is False
    assert result["status"] == "failed"
    assert result["error_kind"] == "fatal_config_or_auth"


def test_invalid_required_api_base_is_a_hard_configuration_failure() -> None:
    config = ensure_config_sections({})
    config["Primary_Reader_API"].update(
        {
            "api_key": "sk-primary-runtime",
            "model": "deepseek-v4-pro",
            "api_base": "not-a-url",
        }
    )

    valid, messages = validate_all_config(
        config,
        required_provider_sections=("Primary_Reader_API",),
    )

    assert valid is False
    assert any("URL" in message for message in messages), messages


def test_config_and_env_publication_rolls_back_when_second_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import services.configuration_service as configuration_service

    config_path = tmp_path / "config.ini"
    env_path = tmp_path / ".env"
    old_config = b"[Application]\nconfig_schema = 1\n"
    old_env = b"LLM_PRIMARY_READER_API=old-secret\n"
    config_path.write_bytes(old_config)
    env_path.write_bytes(old_env)
    calls = 0
    real_replace = configuration_service.atomic_replace_with_retry

    def fail_second_replace(source, target, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AtomicReplaceTimeoutError(target, calls, 0.0)
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(configuration_service, "atomic_replace_with_retry", fail_second_replace)

    with pytest.raises(ConfigurationPersistenceError):
        save_config_and_env(
            ensure_config_sections({}),
            {"Primary_Reader_API": "new-secret"},
            config_path=str(config_path),
            env_path=str(env_path),
        )

    assert config_path.read_bytes() == old_config
    assert env_path.read_bytes() == old_env


def test_config_publication_rejects_control_injection_before_staging(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    env_path = tmp_path / ".env"
    old_config = b"[Application]\nconfig_schema = 1\n"
    old_env = b"LLM_PRIMARY_READER_API=old-secret\n"
    config_path.write_bytes(old_config)
    env_path.write_bytes(old_env)

    with pytest.raises(ValueError, match="control characters"):
        save_config_and_env(
            ensure_config_sections({}),
            {"Primary_Reader_API": "new-secret"},
            extra_env_values={"SAFE_EXTRA": "line1\nline2"},
            config_path=str(config_path),
            env_path=str(env_path),
        )

    assert config_path.read_bytes() == old_config
    assert env_path.read_bytes() == old_env


def test_external_registry_closure_retries_when_revision_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_dir = tmp_path / "remote"
    local_dir = tmp_path / "local"
    remote_dir.mkdir()
    local_dir.mkdir()
    remote = ArtifactRegistry(remote_dir / "artifact_registry.json", "job-remote")
    remote_path = remote_dir / "source.json"
    remote_path.write_text('{"ok": true}', encoding="utf-8")
    remote_record = remote.register_file(
        artifact_id="remote-source",
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=remote_path,
        producer="tests",
    )
    local = ArtifactRegistry(local_dir / "artifact_registry.json", "job-local")
    local_path = local_dir / "root.json"
    local_path.write_text('{"ok": true}', encoding="utf-8")
    dependency = ArtifactDependencyRefV2(
        dependency_kind="external_job",
        job_id="job-remote",
        artifact_id=remote_record.artifact_id,
        artifact_type=remote_record.artifact_type,
        path=remote_record.path,
        content_hash=remote_record.content_hash,
    )
    resolver = lambda job_id: remote if job_id == "job-remote" else None
    root = local.register_file(
        artifact_id="local-root",
        artifact_role="test",
        artifact_type="test_node",
        artifact_version="v1",
        path=local_path,
        producer="tests",
        depends_on=[dependency],
        external_registry_resolver=resolver,
    )
    original_assert = ArtifactRegistry._assert_external_snapshots_unchanged
    calls = 0

    def mutate_revision(cls, snapshots):
        nonlocal calls
        calls += 1
        mutation_path = remote_dir / f"mutation-{calls}.json"
        mutation_path.write_text('{"ok": true}', encoding="utf-8")
        remote.register_file(
            artifact_id=f"mutation-{calls}",
            artifact_role="test",
            artifact_type="test_node",
            artifact_version="v1",
            path=mutation_path,
            producer="tests",
        )
        return original_assert(snapshots)

    monkeypatch.setattr(
        ArtifactRegistry,
        "_assert_external_snapshots_unchanged",
        classmethod(mutate_revision),
    )

    with pytest.raises(UnverifiedDependency, match="snapshot changed"):
        local.verify_ready_artifact_closure(root, external_registry_resolver=resolver)
    assert calls == 3


def test_queue_corruption_is_quarantined_without_becoming_empty(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    original = b'{"jobs": '
    queue_path.write_bytes(original)

    with pytest.raises(QueueCorruption) as caught:
        PersistentQueueService(queue_path)

    assert queue_path.read_bytes() == original
    assert caught.value.quarantine_path
    assert Path(caught.value.quarantine_path).read_bytes() == original


def test_queue_export_corruption_is_not_silently_ignored(tmp_path: Path) -> None:
    queue = PersistentQueueService(tmp_path / "queue.json")
    export_path = tmp_path / "export.json"
    original = b"{\"jobs\":"
    export_path.write_bytes(original)

    with pytest.raises(QueueCorruption) as caught:
        queue.load_queue(export_path)

    assert export_path.read_bytes() == original
    assert caught.value.quarantine_path
    assert Path(caught.value.quarantine_path).read_bytes() == original


def test_in_process_type_error_executes_job_once() -> None:
    calls = 0

    def job(cancel_token):
        nonlocal calls
        calls += 1
        raise TypeError("internal job failure")

    handle = InProcessQueueService().run(job)

    assert calls == 1
    assert handle.status == "failed"
    assert isinstance(handle.error, TypeError)


def test_workspace_rejects_unsafe_identity_and_child_escape(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        JobWorkspace(str(tmp_path), "project", "..\\outside")

    workspace = JobWorkspace.create(str(tmp_path), "project", "safe-job")
    with pytest.raises(WorkspacePathError):
        workspace.artifact_path("../outside.json")


def test_atomic_replace_retries_sharing_contention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(durable_io.os, "name", "nt")
    attempts = 0

    class SharingViolation(PermissionError):
        winerror = 32

    def replace(_source: str, _target: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SharingViolation(32, "sharing violation")

    durable_io.atomic_replace_with_retry(
        "source.tmp",
        "target.json",
        timeout_seconds=1,
        replace=replace,
        sleep=lambda _delay: None,
        monotonic=iter([0.0, 0.1, 0.2]).__next__,
    )

    assert attempts == 3

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import hash_json
from runtime.runtime_spec_binding import (
    RuntimeSpecBindingError,
    read_runtime_spec_binding_v1,
)
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext


EFFECTIVE_CONFIG_SHA256 = "e" * 64


def _published_runtime_spec(
    tmp_path: Path,
    *,
    binding_overrides: Mapping[str, Any] | None = None,
    extra_binding_fields: Mapping[str, Any] | None = None,
    binding_override_factory: Callable[[dict[str, Any], Path], Mapping[str, Any]] | None = None,
    payload_mutator: Any | None = None,
) -> tuple[ArtifactRegistry, Any, Path, dict[str, Any]]:
    job_id = "binding-job"
    workspace = JobWorkspace.create(str(tmp_path / "output"), "binding-project", job_id)
    config_path = (tmp_path / "config.ini").resolve()
    config_path.write_bytes(b"[Paths]\noutput_path = ./output\n")
    pdf_dir = (tmp_path / "papers").resolve()
    pdf_dir.mkdir()
    spec = RuntimeJobSpec(
        project_name="binding-project",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id=job_id,
        config=str(config_path),
        action="generate_outline",
        workspace_path=str(Path(workspace.root_dir).resolve()),
    )
    spec.validate()
    payload = spec.to_dict()
    if payload_mutator is not None:
        payload_mutator(payload)
    config_binding: dict[str, Any] = {
        "schema_version": "runtime-config-source/v1",
        "config_source_id": str(config_path),
        "config_source_sha256": file_sha256(config_path),
        "effective_config_sha256": EFFECTIVE_CONFIG_SHA256,
        "normalized_spec_payload_sha256": hash_json(payload),
    }
    if binding_override_factory is not None:
        config_binding.update(dict(binding_override_factory(payload, config_path)))
    config_binding.update(dict(binding_overrides or {}))
    config_binding.update(dict(extra_binding_fields or {}))
    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
    record = publish_json_artifact(
        LocalPublicationContext(),
        registry,
        workspace.artifact_path("runtime_job_spec_v1.json"),
        payload,
        artifact_role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        producer="tests.test_runtime_spec_binding",
        artifact_id="runtime_job_spec",
        metadata={"config_snapshot_binding": config_binding},
    )
    assert record.status == "ready"
    return registry, record, config_path, payload


def test_reader_returns_hashes_from_ready_normalized_spec_and_current_config(
    tmp_path: Path,
) -> None:
    registry, record, config_path, payload = _published_runtime_spec(tmp_path)

    binding = read_runtime_spec_binding_v1(
        registry,
        expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
    )

    assert binding.normalized_spec_artifact_id == "runtime_job_spec"
    assert binding.normalized_spec_artifact_sha256 == record.content_hash
    assert binding.normalized_spec_payload_sha256 == hash_json(payload)
    assert binding.config_source_id == str(config_path)
    assert binding.config_source_sha256 == file_sha256(config_path)
    assert binding.effective_config_sha256 == EFFECTIVE_CONFIG_SHA256
    binding.assert_current_config()


@pytest.mark.parametrize(
    ("overrides", "extra", "message"),
    [
        ({"schema_version": "runtime-config-source/v0"}, {}, "schema"),
        ({"config_source_id": "C:/unrelated.ini"}, {}, "identity"),
        ({"config_source_sha256": "0" * 64}, {}, "hash does not match"),
        ({"normalized_spec_payload_sha256": "0" * 64}, {}, "payload hash"),
        ({}, {"unexpected": "value"}, "fields are incomplete or unknown"),
    ],
)
def test_reader_rejects_config_binding_metadata_drift(
    tmp_path: Path,
    overrides: Mapping[str, Any],
    extra: Mapping[str, Any],
    message: str,
) -> None:
    registry, _record, _config_path, _payload = _published_runtime_spec(
        tmp_path,
        binding_overrides=overrides,
        extra_binding_fields=extra,
    )

    with pytest.raises(RuntimeSpecBindingError, match=message):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )


def test_reader_rejects_effective_config_hash_from_another_runtime(
    tmp_path: Path,
) -> None:
    registry, _record, _config_path, _payload = _published_runtime_spec(tmp_path)

    with pytest.raises(RuntimeSpecBindingError, match="accepted runtime"):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256="f" * 64,
        )


def test_reader_rejects_raw_config_byte_drift_and_binding_rechecks_it(
    tmp_path: Path,
) -> None:
    registry, _record, config_path, _payload = _published_runtime_spec(tmp_path)
    binding = read_runtime_spec_binding_v1(
        registry,
        expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
    )
    config_path.write_bytes(config_path.read_bytes() + b"# changed\n")

    with pytest.raises(RuntimeSpecBindingError, match="changed after acceptance"):
        binding.assert_current_config()
    with pytest.raises(RuntimeSpecBindingError, match="hash does not match current bytes"):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )


def test_reader_rejects_invalid_persisted_runtime_job_spec(tmp_path: Path) -> None:
    def make_invalid(payload: dict[str, Any]) -> None:
        payload["action"] = "not-an-action"

    registry, _record, _config_path, _payload = _published_runtime_spec(
        tmp_path,
        payload_mutator=make_invalid,
    )

    with pytest.raises(RuntimeSpecBindingError, match="RuntimeJobSpec is invalid"):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )


def test_reader_rejects_foreign_registry_job_identity(tmp_path: Path) -> None:
    registry, record, _config_path, _payload = _published_runtime_spec(tmp_path)

    class ForeignJobRegistryView:
        job_id = "another-job"

        def reload(self) -> None:
            registry.reload()

        def get(self, artifact_id: str) -> Any:
            return record if artifact_id == "runtime_job_spec" else registry.get(artifact_id)

        def verify_ready_artifact_closure(self, root: Any) -> Any:
            return registry.verify_ready_artifact_closure(root)

    with pytest.raises(RuntimeSpecBindingError, match="another job"):
        read_runtime_spec_binding_v1(
            ForeignJobRegistryView(),  # type: ignore[arg-type]
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )


def test_reader_rejects_invalid_ready_artifact_closure(tmp_path: Path) -> None:
    registry, record, _config_path, _payload = _published_runtime_spec(tmp_path)
    Path(record.path).write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeSpecBindingError, match="ready closure is invalid"):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )


def test_raw_runtime_spec_file_hash_cannot_stand_in_for_artifact_or_payload_hash(
    tmp_path: Path,
) -> None:
    raw_spec_file = tmp_path / "accepted-child-spec.json"

    raw_spec_sha256 = ""

    def bind_raw_spec_hash(payload: dict[str, Any], _config_path: Path) -> Mapping[str, Any]:
        nonlocal raw_spec_sha256
        raw_payload = dict(payload)
        raw_payload["config"] = "config.ini"
        raw_spec_file.write_text(json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8")
        raw_spec_sha256 = hashlib.sha256(raw_spec_file.read_bytes()).hexdigest()
        return {"normalized_spec_payload_sha256": raw_spec_sha256}

    registry, record, _config_path, payload = _published_runtime_spec(
        tmp_path,
        binding_override_factory=bind_raw_spec_hash,
    )
    assert raw_spec_sha256 != record.content_hash
    assert raw_spec_sha256 != hash_json(payload)

    with pytest.raises(RuntimeSpecBindingError, match="payload hash"):
        read_runtime_spec_binding_v1(
            registry,
            expected_effective_config_sha256=EFFECTIVE_CONFIG_SHA256,
        )

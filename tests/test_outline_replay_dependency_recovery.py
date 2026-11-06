from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from outline.v3_artifacts import EvidenceCritique, ProviderReceiptClosureArtifact
from runtime.artifact_validators import (
    ArtifactSchemaError,
    _validate_outline_receipt_output_dependencies,
    validate_current_outline_artifact,
)
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, file_sha256


@pytest.mark.parametrize("version", ["v1", "v3"])
@pytest.mark.parametrize("dependency", ["valid", "missing", "wrong_hash", "wrong_path", "empty_path", "empty_hash"])
def test_outline_recovery_requires_exact_physical_response_dependency(tmp_path: Path, version: str, dependency: str):
    registry = ArtifactRegistry(tmp_path / "registry.json", "scope-job")
    response_path = tmp_path / "physical-response.json"
    response_path.write_text(json.dumps(EvidenceCritique(
        job_id=registry.job_id, payload={"passed": True, "blocking_diagnostics": []},
    ).to_dict()), encoding="utf-8")
    response = registry.register_file(
        artifact_id="physical-output", artifact_role="outline_v3_critique_shard_output",
        artifact_type="evidence_critique", artifact_version="v3", path=response_path,
        producer="test.actual-response",
    )
    ref = ArtifactDependencyRefV2.from_record(response)
    refs = [ref]
    if dependency == "missing":
        refs = []
    elif dependency == "wrong_hash":
        refs = [replace(ref, content_hash="f" * 64)]
    elif dependency == "wrong_path":
        refs = [replace(ref, path=str(tmp_path / "another-response.json"))]
    payload = {
        "stage_name": "stage2_outline", "closure_epoch_id": "epoch",
        "expected_call_ids": ["outline:evidence_critique:local:one"],
        "observed_call_ids": ["outline:evidence_critique:local:one"],
        "missing_call_ids": [], "hash_mismatches": {}, "complete": True,
        "closure_hash": "closure-hash",
        "expected_calls": [{"artifact_path": response.path, "registry_file_hash": response.content_hash}],
    }
    if dependency == "empty_path":
        payload["expected_calls"][0]["artifact_path"] = ""
    elif dependency == "empty_hash":
        payload["expected_calls"][0]["registry_file_hash"] = ""
    if version == "v3":
        body = ProviderReceiptClosureArtifact(job_id=registry.job_id, payload=payload).to_dict()
    else:
        body = {"artifact_type": "provider_receipt_closure", "artifact_version": version,
                "job_id": registry.job_id, **payload}
    path = tmp_path / "closure.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    closure = ArtifactRecord(
        artifact_id="outline-v3:provider_receipt_closure", artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure", artifact_version=version,
        path=str(path), producer="test.closure", depends_on=refs, job_id=registry.job_id,
        status="ready", content_hash=file_sha256(path),
    )
    if dependency == "valid":
        validate_current_outline_artifact(closure, path)
    else:
        with pytest.raises(ArtifactSchemaError, match="direct Registry dependency|dependency hash differs"):
            validate_current_outline_artifact(closure, path)


def test_dependency_guard_resolves_paths_linearly_and_rejects_conflicting_duplicates(tmp_path, monkeypatch):
    refs = [ArtifactDependencyRefV2(
        dependency_kind="local_job", job_id="scope-job", artifact_id=f"output-{index}",
        artifact_type="evidence_critique", path=str(tmp_path / f"response-{index}.json"),
        content_hash="a" * 64,
    ) for index in range(102)]
    record = ArtifactRecord(
        artifact_id="closure", artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure", artifact_version="v3",
        path=str(tmp_path / "closure.json"), producer="test", job_id="scope-job",
        status="ready", content_hash="b" * 64, depends_on=refs,
    )
    payload = {"stage_name": "stage2_outline", "expected_calls": [
        {"artifact_path": ref.path, "registry_file_hash": ref.content_hash} for ref in refs
    ]}
    resolutions = []
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        resolutions.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    _validate_outline_receipt_output_dependencies(record, payload)
    assert len(resolutions) == len(refs)
    conflicting = replace(record, depends_on=[*refs, replace(refs[0], content_hash="f" * 64)])
    resolutions.clear()
    with pytest.raises(ArtifactSchemaError, match="dependency hash differs"):
        _validate_outline_receipt_output_dependencies(conflicting, payload)
    assert len(resolutions) == len(refs)


def test_dependency_guard_resolves_distinct_aliases_and_rejects_conflicting_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_path = tmp_path / "response.json"
    response_path.write_text("{}", encoding="utf-8")
    alias_parent = tmp_path / "alias-parent"
    alias_parent.mkdir()
    direct_path = str(response_path)
    alias_path = str(alias_parent / ".." / response_path.name)
    assert alias_path != direct_path

    expected_hash = "a" * 64
    refs = [
        ArtifactDependencyRefV2(
            dependency_kind="local_job", job_id="scope-job", artifact_id="output-direct",
            artifact_type="evidence_critique", path=direct_path, content_hash=expected_hash,
        ),
        ArtifactDependencyRefV2(
            dependency_kind="local_job", job_id="scope-job", artifact_id="output-alias",
            artifact_type="evidence_critique", path=alias_path, content_hash="f" * 64,
        ),
    ]
    record = ArtifactRecord(
        artifact_id="closure", artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure", artifact_version="v3",
        path=str(tmp_path / "closure.json"), producer="test", job_id="scope-job",
        status="ready", content_hash="b" * 64, depends_on=refs,
    )
    payload = {
        "stage_name": "stage2_outline",
        "expected_calls": [{
            "artifact_path": direct_path,
            "registry_file_hash": expected_hash,
        }],
    }
    resolutions: list[str] = []
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        resolutions.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ArtifactSchemaError, match="dependency hash differs"):
        _validate_outline_receipt_output_dependencies(record, payload)
    assert resolutions.count(direct_path) == 1
    assert resolutions.count(alias_path) == 1


def test_dependency_guard_resolution_memo_is_discarded_after_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = str(tmp_path / "response.json")
    ref = ArtifactDependencyRefV2(
        dependency_kind="local_job", job_id="scope-job", artifact_id="output",
        artifact_type="evidence_critique", path=raw_path, content_hash="a" * 64,
    )
    record = ArtifactRecord(
        artifact_id="closure", artifact_role="provider_receipt_closure",
        artifact_type="provider_receipt_closure", artifact_version="v3",
        path=str(tmp_path / "closure.json"), producer="test", job_id="scope-job",
        status="ready", content_hash="b" * 64, depends_on=[ref],
    )
    payload = {
        "stage_name": "stage2_outline",
        "expected_calls": [{
            "artifact_path": raw_path,
            "registry_file_hash": "a" * 64,
        }],
    }
    resolutions: list[str] = []
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        resolutions.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    _validate_outline_receipt_output_dependencies(record, payload)
    assert resolutions.count(raw_path) == 1
    _validate_outline_receipt_output_dependencies(record, payload)
    assert resolutions.count(raw_path) == 2

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from runtime import export_bundle as export_module
from runtime.export_bundle import (
    EXPORT_BUNDLE_ARTIFACT_TYPE,
    FORENSIC_ATTESTATION_ARTIFACT_TYPE,
    ExportBundleService,
    ForensicAttestationService,
    _current_pointer_targets,
    _export_dependency_issues,
)
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryCorruption,
    RegistryError,
    RegistrySnapshotChanged,
    UnverifiedDependency,
)

_EXCLUDED = (EXPORT_BUNDLE_ARTIFACT_TYPE, FORENSIC_ATTESTATION_ARTIFACT_TYPE)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _register(
    registry: ArtifactRegistry,
    root: Path,
    artifact_id: str,
    artifact_type: str,
    artifact_role: str,
    *,
    dependencies: list[ArtifactDependencyRefV2] | None = None,
    external_registry_resolver=None,
    payload: dict[str, Any] | None = None,
) -> ArtifactRecord:
    safe_name = artifact_id.replace(":", "_").replace("/", "_")
    path = root / f"{safe_name}.json"
    _write_json(path, payload or {"artifact_id": artifact_id, "value": "ready"})
    version = "v1" if artifact_type.startswith("test_") or artifact_type == "current_artifact_pointer" else "v3"
    return registry.register_file(
        artifact_id=artifact_id,
        artifact_role=artifact_role,
        artifact_type=artifact_type,
        artifact_version=version,
        path=path,
        producer="export-snapshot-fence-test",
        depends_on=dependencies or [],
        external_registry_resolver=external_registry_resolver,
    )


def _mutate_owner_dependency(
    registry: ArtifactRegistry,
    owner_id: str,
    update: dict[str, Any],
) -> None:
    path = Path(registry.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(item for item in payload["artifacts"] if item["artifact_id"] == owner_id)
    row["depends_on"][0].update(update)
    payload["revision"] += 1
    _write_json(path, payload)
    registry.reload()


def _sequential_failures(
    registry: ArtifactRegistry,
    owners: list[ArtifactRecord],
) -> dict[str, tuple[type[Exception], str]]:
    failures: dict[str, tuple[type[Exception], str]] = {}
    for owner in owners:
        try:
            ArtifactRegistry._verify_ready_artifact(owner)
            registry.verify_ready_dependencies(owner.depends_on, owner_record=owner)
        except (OSError, RegistryError, ValueError, TypeError) as exc:
            failures[owner.artifact_id] = (type(exc), str(exc))
    return failures


def test_batch_reads_two_local_snapshots_and_preserves_owner_negatives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-batch")
    dependency = _register(registry, tmp_path, "dependency:ready", "test_dependency", "test-dependency")
    ref = ArtifactDependencyRefV2.from_record(dependency)
    for artifact_id in ("owner:valid", "owner:bad_hash", "owner:bad_type"):
        _register(registry, tmp_path, artifact_id, "test_owner", "test-owner", dependencies=[ref])

    stale_owner = registry.get("owner:bad_hash")
    assert stale_owner is not None
    _mutate_owner_dependency(registry, "owner:bad_hash", {"content_hash": "0" * 64})
    _mutate_owner_dependency(registry, "owner:bad_type", {"artifact_type": "other_type"})
    owners = sorted(
        (
            record for record in registry.list_records()
            if record.status == "ready" and record.artifact_type not in _EXCLUDED
        ),
        key=lambda record: record.artifact_id,
    )
    expected = _sequential_failures(registry, owners)

    reads = 0
    read_registry = registry._read_registry_unlocked
    normalize = registry._normalize_dependencies
    normalized_owners: list[str] = []

    def count_read():
        nonlocal reads
        reads += 1
        return read_registry()

    def observe_normalize(dependencies, *args, **kwargs):
        owner = kwargs.get("owner_record")
        if owner is not None:
            normalized_owners.append(owner.artifact_id)
        return normalize(dependencies, *args, **kwargs)

    monkeypatch.setattr(registry, "_read_registry_unlocked", count_read)
    monkeypatch.setattr(registry, "_normalize_dependencies", observe_normalize)
    failures = registry.verify_ready_artifact_dependency_sets(
        owners,
        complete_ready_set_excluding=_EXCLUDED,
    )
    assert reads == 2  # captured snapshot plus short final fence
    assert normalized_owners == [record.artifact_id for record in owners]
    assert set(failures) == set(expected)
    assert all(type(failures[key]) is expected[key][0] for key in expected)
    assert all(str(failures[key]) == expected[key][1] for key in expected)
    assert "owner:valid" not in failures
    assert isinstance(failures["owner:bad_hash"], UnverifiedDependency)

    # A previous successful/failed result is never cached across batches or
    # Registry mutations; stale owner records fail against the fresh snapshot.
    stale_failures = registry.verify_ready_artifact_dependency_sets([stale_owner])
    assert set(stale_failures) == {"owner:bad_hash"}
    assert "changed in Registry snapshot" in str(stale_failures["owner:bad_hash"])


def test_empty_and_stale_ready_owner_sets_are_rejected(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-set")
    _register(registry, tmp_path, "owner:first", "test_owner", "test-owner")

    # An empty list with a completeness contract must not early-return.
    with pytest.raises(UnverifiedDependency, match="owner set does not match"):
        registry.verify_ready_artifact_dependency_sets([], complete_ready_set_excluding=_EXCLUDED)

    captured = [
        record for record in registry.list_records()
        if record.status == "ready" and record.artifact_type not in _EXCLUDED
    ]
    _register(registry, tmp_path, "owner:late", "test_owner", "test-owner")
    with pytest.raises(UnverifiedDependency, match="omitted=.*owner:late"):
        registry.verify_ready_artifact_dependency_sets(
            captured, complete_ready_set_excluding=_EXCLUDED,
        )


def test_capture_record_snapshot_refreshes_stale_registry_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    stale = ArtifactRegistry(registry_path, "job-stale-reader")
    writer = ArtifactRegistry(registry_path, "job-stale-reader")
    late = _register(writer, tmp_path, "owner:published_elsewhere", "test_owner", "test-owner")

    assert stale.list_records() == []
    with pytest.raises(UnverifiedDependency, match="owner set does not match"):
        stale.verify_ready_artifact_dependency_sets(
            [], complete_ready_set_excluding=_EXCLUDED,
        )

    snapshot_revision, records = stale.capture_record_snapshot()
    assert snapshot_revision == writer.revision
    assert stale.revision == snapshot_revision
    assert records == [late]
    assert stale.get(late.artifact_id) == late

    reads = 0
    read_registry = stale._read_registry_unlocked

    def count_read():
        nonlocal reads
        reads += 1
        return read_registry()

    monkeypatch.setattr(stale, "_read_registry_unlocked", count_read)
    assert stale.verify_ready_artifact_dependency_sets(
        records,
        complete_ready_set_excluding=_EXCLUDED,
        expected_snapshot_revision=snapshot_revision,
    ) == {}
    assert reads == 2  # initial epoch confirmation plus final fence


def test_expected_revision_rejects_aba_even_when_records_return_to_same_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "job-aba")
    _register(registry, tmp_path, "owner:aba", "test_owner", "test-owner")
    snapshot_revision, snapshot_records = registry.capture_record_snapshot()
    captured_owner = snapshot_records[0]
    registry_path = Path(registry.registry_path)
    original_metadata = dict(captured_owner.metadata)

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(item for item in payload["artifacts"] if item["artifact_id"] == captured_owner.artifact_id)
    row["metadata"]["temporary_aba_value"] = "B"
    payload["revision"] += 1
    _write_json(registry_path, payload)

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(item for item in payload["artifacts"] if item["artifact_id"] == captured_owner.artifact_id)
    row["metadata"] = original_metadata
    payload["revision"] += 1
    _write_json(registry_path, payload)
    final_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert final_payload["revision"] == snapshot_revision + 2
    assert next(item for item in final_payload["artifacts"] if item["artifact_id"] == captured_owner.artifact_id)["metadata"] == original_metadata

    verify_calls = 0

    def count_verify(_record: ArtifactRecord) -> None:
        nonlocal verify_calls
        verify_calls += 1

    monkeypatch.setattr(registry, "_verify_ready_artifact", count_verify)
    with pytest.raises(RegistrySnapshotChanged, match="before batched"):
        registry.verify_ready_artifact_dependency_sets(
            snapshot_records,
            complete_ready_set_excluding=_EXCLUDED,
            expected_snapshot_revision=snapshot_revision,
        )
    assert verify_calls == 0

    # The empty-owner path still compares the expected epoch before it can
    # accept a set that omits the current READY owner.
    with pytest.raises(RegistrySnapshotChanged, match="before batched"):
        registry.verify_ready_artifact_dependency_sets(
            [],
            complete_ready_set_excluding=_EXCLUDED,
            expected_snapshot_revision=snapshot_revision,
        )


def test_registry_writer_can_publish_during_hashcheck_but_batch_fails_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, "job-concurrent")
    _register(registry, tmp_path, "owner:slow", "test_owner", "test-owner")
    owners = [record for record in registry.list_records() if record.status == "ready"]
    publisher = ArtifactRegistry(registry_path, "job-concurrent")
    entered_hashcheck = threading.Event()
    release_hashcheck = threading.Event()
    publish_finished = threading.Event()
    batch_errors: list[BaseException] = []
    publish_errors: list[BaseException] = []
    original_verify = registry._verify_ready_artifact

    def pause_during_hashcheck(record: ArtifactRecord) -> None:
        if record.artifact_id == "owner:slow":
            entered_hashcheck.set()
            if not release_hashcheck.wait(timeout=8):
                raise TimeoutError("test did not release the owner hashcheck gate")
        original_verify(record)

    def run_batch() -> None:
        try:
            registry.verify_ready_artifact_dependency_sets(
                owners, complete_ready_set_excluding=_EXCLUDED,
            )
        except (OSError, RegistryError, ValueError, TypeError, AssertionError) as exc:
            batch_errors.append(exc)

    def publish_late_owner() -> None:
        try:
            _register(publisher, tmp_path, "owner:late", "test_owner", "test-owner")
        except (OSError, RegistryError, ValueError, TypeError, AssertionError) as exc:
            publish_errors.append(exc)
        finally:
            publish_finished.set()

    monkeypatch.setattr(registry, "_verify_ready_artifact", pause_during_hashcheck)
    batch_thread = threading.Thread(target=run_batch, daemon=True)
    batch_thread.start()
    assert entered_hashcheck.wait(timeout=3)
    publisher_thread = threading.Thread(target=publish_late_owner, daemon=True)
    publisher_thread.start()
    published_during_hashcheck = publish_finished.wait(timeout=3)
    release_hashcheck.set()
    batch_thread.join(timeout=12)
    publisher_thread.join(timeout=12)

    assert published_during_hashcheck, publish_errors
    assert not publish_errors
    assert not batch_thread.is_alive()
    assert not publisher_thread.is_alive()
    assert len(batch_errors) == 1
    assert isinstance(batch_errors[0], RegistrySnapshotChanged)


def test_same_revision_artifact_identity_change_fails_final_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, "job-same-revision")
    _register(registry, tmp_path, "owner:identity", "test_owner", "test-owner")
    owners = [record for record in registry.list_records() if record.status == "ready"]
    initial_revision = registry.revision
    original_verify = registry._verify_ready_artifact
    changed = False

    def mutate_owner_metadata_during_hashcheck(record: ArtifactRecord) -> None:
        nonlocal changed
        original_verify(record)
        if record.artifact_id == "owner:identity" and not changed:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            row = next(item for item in payload["artifacts"] if item["artifact_id"] == record.artifact_id)
            row["metadata"]["same_revision_change"] = True
            assert payload["revision"] == initial_revision
            _write_json(registry_path, payload)
            changed = True

    monkeypatch.setattr(registry, "_verify_ready_artifact", mutate_owner_metadata_during_hashcheck)
    with pytest.raises(RegistrySnapshotChanged, match="snapshot changed"):
        registry.verify_ready_artifact_dependency_sets(
            owners, complete_ready_set_excluding=_EXCLUDED,
        )
    assert changed
    latest = json.loads(registry_path.read_text(encoding="utf-8"))
    assert latest["revision"] == initial_revision


def test_external_registry_resolution_remains_per_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = ArtifactRegistry(tmp_path / "local_registry.json", "local-job")
    external = ArtifactRegistry(tmp_path / "external_registry.json", "external-job")
    dependency = _register(external, tmp_path, "external:dep", "test_dependency", "test-dependency")
    ref = ArtifactDependencyRefV2.from_record(dependency, dependency_kind="external_job")
    resolver = lambda job_id: external if job_id == external.job_id else None
    owners = [
        _register(local, tmp_path, "owner:one", "test_owner", "test-owner", dependencies=[ref], external_registry_resolver=resolver),
        _register(local, tmp_path, "owner:two", "test_owner", "test-owner", dependencies=[ref], external_registry_resolver=resolver),
    ]
    reads = 0
    original_read = external._read_registry_unlocked

    def count_external_read():
        nonlocal reads
        reads += 1
        return original_read()

    monkeypatch.setattr(external, "_read_registry_unlocked", count_external_read)
    assert local.verify_ready_artifact_dependency_sets(
        owners,
        external_registry_resolver=resolver,
        complete_ready_set_excluding=_EXCLUDED,
    ) == {}
    assert reads == len(owners)


def test_export_reports_registry_snapshot_read_failure_as_locatable_issue() -> None:
    class BrokenRegistry:
        def verify_ready_artifact_dependency_sets(self, *args, **kwargs):
            raise RegistryCorruption("cannot decode Registry snapshot")

    owner = ArtifactRecord(
        artifact_id="owner:unavailable",
        artifact_role="test-owner",
        artifact_type="test_owner",
        artifact_version="v1",
        path="owner.json",
        producer="export-snapshot-fence-test",
        job_id="job-broken",
        status="ready",
        content_hash="owner-hash",
        depends_on=[],
        metadata={},
    )
    issues = _export_dependency_issues(BrokenRegistry(), [owner], None)  # type: ignore[arg-type]
    assert issues == ["Registry direct-dependency snapshot:cannot decode Registry snapshot"]


def test_current_pointer_targets_use_the_provided_record_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ArtifactRegistry(tmp_path / "pointer_registry.json", "job-pointer")
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(lambda cls, record: None))
    target = _register(registry, tmp_path, "review_draft:current", "test_target", "test-target")
    pointer_payload = {
        "pointer_kind": "review_draft",
        "target_artifact_id": target.artifact_id,
        "target_content_hash": target.content_hash,
    }
    pointer = _register(
        registry,
        tmp_path,
        "current-pointer:review-draft",
        "current_artifact_pointer",
        "current_artifact_pointer",
        payload=pointer_payload,
    )
    records = [pointer, target]

    class NoRegistryLookups:
        job_id = registry.job_id

        def list_records(self):
            raise AssertionError("pointer selection re-listed Registry")

        def get(self, _artifact_id):
            raise AssertionError("pointer selection re-read owner records")

        def resolve_current_artifact_set(self):
            raise AssertionError("provided current-set payload was not honored")

    targets, kinds = _current_pointer_targets(NoRegistryLookups(), records, {})  # type: ignore[arg-type]
    assert targets == {target.artifact_id}
    assert kinds == {"review_draft": target.artifact_id}


class _Workspace:
    def __init__(self, root: Path, job_id: str) -> None:
        self.root_dir = str(root)
        self.job_id = job_id
        self._root = root

    def report_path(self, relative: str) -> str:
        return str(self._root / "reports" / relative)


def _derived_with_post_snapshot_owner(
    registry: ArtifactRegistry,
    root: Path,
    captured: list[list[ArtifactRecord]],
    captured_revisions: list[int | None],
):
    def fake_derive(
        workspace,
        passed_registry,
        *,
        records_snapshot=None,
        expected_snapshot_revision=None,
        records_snapshot_error=None,
    ):
        assert passed_registry is registry
        assert records_snapshot is not None
        assert records_snapshot_error is None
        assert expected_snapshot_revision == registry.revision
        captured.append(records_snapshot)
        captured_revisions.append(expected_snapshot_revision)
        _register(registry, root, "review_docx:late", "review_docx", "review_docx")
        return {
            "completion": {"job_status": "completed", "completion_status": "complete", "canonical_ready": True, "completion_reasons": []},
            "closure": {"status": "clean", "validation_required": False, "semantic": {}},
            "receipt_closure": {"status": "clean", "complete": True, "artifact_ids": [], "closures": []},
            "current_artifact_set": {},
            "current_stage_closure_map": {"stages": {}},
            "requested_stages": [],
            "spec_hash": "frozen-spec-hash",
            "adoption": {"status": "not_adopted"},
            "issues": ["dependency_closure:ready owner set changed after record snapshot"],
        }
    return fake_derive


def test_export_uses_one_captured_record_list_for_derivation_pointers_and_zip_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "job-export-record-snapshot"
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", job_id)
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(lambda cls, record: None))
    original = _register(registry, tmp_path, "review_draft", "review_draft", "review_draft")
    workspace = _Workspace(tmp_path, job_id)
    captured: list[list[ArtifactRecord]] = []
    captured_revisions: list[int | None] = []
    pointer_snapshots: list[list[ArtifactRecord]] = []
    capture_calls = 0
    original_capture = registry.capture_record_snapshot
    current_targets = export_module._current_pointer_targets

    def capture_records():
        nonlocal capture_calls
        capture_calls += 1
        return original_capture()

    def capture_pointer_snapshot(registry_arg, records_snapshot, current_set):
        pointer_snapshots.append(records_snapshot)
        return current_targets(registry_arg, records_snapshot, current_set)

    monkeypatch.setattr(registry, "capture_record_snapshot", capture_records)
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(lambda cls, record: None))
    monkeypatch.setattr(AgentRuntimeRunner, "_external_registry_resolver", staticmethod(lambda workspace: None))
    monkeypatch.setattr(export_module, "_current_pointer_targets", capture_pointer_snapshot)
    monkeypatch.setattr(
        export_module,
        "_derive_current_evidence",
        _derived_with_post_snapshot_owner(registry, tmp_path, captured, captured_revisions),
    )

    result = ExportBundleService(workspace, registry).export()
    assert result.status == "untrusted"
    assert len(captured) == 1 and len(pointer_snapshots) == 1
    assert captured[0] is pointer_snapshots[0]
    assert len(captured_revisions) == 1 and captured_revisions[0] is not None
    exported_ids = {str(row.get("artifact_id") or "") for row in result.manifest.get("records") or ()}
    assert original.artifact_id in exported_ids
    assert "review_docx:late" not in exported_ids
    assert "review_docx:late" in {record.artifact_id for record in registry.list_records()}
    assert capture_calls == 1


def test_attestation_graph_uses_one_captured_record_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = "job-attestation-record-snapshot"
    registry = ArtifactRegistry(tmp_path / "attestation_registry.json", job_id)
    original = _register(registry, tmp_path, "owner:attested", "test_owner", "test-owner")
    workspace = _Workspace(tmp_path / "attestation", job_id)
    captured: list[list[ArtifactRecord]] = []
    captured_revisions: list[int | None] = []
    capture_calls = 0
    original_capture = registry.capture_record_snapshot

    def capture_records():
        nonlocal capture_calls
        capture_calls += 1
        return original_capture()

    monkeypatch.setattr(registry, "capture_record_snapshot", capture_records)
    monkeypatch.setattr(ArtifactRegistry, "_verify_ready_artifact", classmethod(lambda cls, record: None))
    monkeypatch.setattr(
        export_module,
        "_derive_current_evidence",
        _derived_with_post_snapshot_owner(registry, tmp_path, captured, captured_revisions),
    )

    result = ForensicAttestationService(workspace, registry).attest(persist=False)
    assert result.status == "untrusted"
    assert len(captured) == 1 and len(captured_revisions) == 1
    assert set(result.dependency_graph) == {original.artifact_id}
    assert "review_docx:late" not in result.dependency_graph
    assert capture_calls == 1


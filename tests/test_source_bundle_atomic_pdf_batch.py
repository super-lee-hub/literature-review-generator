from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.orchestrator import AgentRuntimeBridge, AgentRuntimeSession
from runtime.stage_contracts import PaperWorkItem, SourceBundle
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace
from services.queue_service import (
    LocalPublicationContext,
    PersistentQueueService,
    QueueJobSpec,
    QueuePublicationRejected,
)


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("source bundle file-unit controls must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


def _bundle(root: Path, count: int) -> SourceBundle:
    root.mkdir(parents=True)
    items = []
    # Reversed input order catches accidental sorting by ID or source path.
    for index in reversed(range(count)):
        path = root / f"paper-{index:03d}.pdf"
        path.write_bytes(f"%PDF-1.4\n%source-{index}\n".encode())
        items.append(
            PaperWorkItem(
                paper_info={"title": f"Paper {index}"},
                source_descriptor={},
                source_mode="direct",
                canonical_paper_key=f"paper-{index:03d}",
                source_paper_id=f"source-{index:03d}",
                source_pdf=str(path),
            )
        )
    if count:
        items.insert(
            0,
            PaperWorkItem(
                paper_info={"title": "Missing source"},
                source_descriptor={},
                source_mode="direct",
                canonical_paper_key="missing-source",
                source_paper_id="missing-source",
                source_pdf=str(root / "missing.pdf"),
            ),
        )
    return SourceBundle("direct", "batch-files", items, {"preserved": [False, 0]})


def _session(
    workspace: JobWorkspace, registry: ArtifactRegistry, publication: Any
) -> AgentRuntimeSession:
    return cast(
        AgentRuntimeSession,
        SimpleNamespace(
            context=SimpleNamespace(
                workspace=workspace, registry=registry, publication_context=publication
            )
        ),
    )


def _bridge(bundle: SourceBundle, job_id: str, pdf_folder: Path) -> AgentRuntimeBridge:
    return AgentRuntimeBridge(
        RuntimeJobSpec(
            project_name=bundle.project_name,
            job_id=job_id,
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_folder)),
        )
    )


def _record_state(registry: ArtifactRegistry, artifact_id: str) -> dict[str, Any]:
    record = registry.get(artifact_id)
    assert record is not None
    return asdict(record)


@pytest.mark.parametrize("count", [0, 200])
def test_existing_pdf_batch_preserves_source_closure_and_uses_two_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    bundle = _bundle(tmp_path / "papers", count)
    workspace = JobWorkspace(str(tmp_path / "output"), "batch-files", "file-unit")
    workspace.ensure_exists()
    publication = LocalPublicationContext()
    registry = publication.registry(workspace.paths.registry_path, workspace.job_id)
    bridge = _bridge(bundle, workspace.job_id, tmp_path / "papers")
    session = _session(workspace, registry, publication)
    specs = [
        {
            "artifact_id": f"source_pdf:{Path(item.source_pdf).name}",
            "artifact_role": "source_pdf",
            "artifact_type": "source_pdf",
            "artifact_version": "v1",
            "path": item.source_pdf,
            "producer": "runtime.orchestrator.AgentRuntimeBridge.persist_source_bundle",
            "created_at": "2001-01-01T00:00:00Z",
        }
        for item in bundle.paper_work_items
        if Path(item.source_pdf).is_file()
    ]
    prior = registry.register_files_atomic(specs) if specs else []
    unrelated_path = tmp_path / "unrelated.json"
    unrelated_path.write_text('{"unchanged": false}', encoding="utf-8")
    unrelated = registry.register_file(
        artifact_id="unrelated",
        artifact_role="test",
        artifact_type="test",
        artifact_version="v1",
        path=unrelated_path,
        producer="test",
    )
    expected_refs = [
        ArtifactDependencyRefV2.from_record(row).to_dict() for row in prior
    ]
    expected_hash = hashlib.sha256(
        json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    ).hexdigest()
    counts = {"read": 0, "write": 0}
    read = registry._read_registry_unlocked
    write = registry._write_registry_unlocked

    def count_read() -> Any:
        counts["read"] += 1
        return read()

    def count_write(*args: Any, **kwargs: Any) -> None:
        counts["write"] += 1
        write(*args, **kwargs)

    monkeypatch.setattr(registry, "_read_registry_unlocked", count_read)
    monkeypatch.setattr(registry, "_write_registry_unlocked", count_write)
    for _repeat in range(2):
        counts.update(read=0, write=0)
        ref = bridge.persist_source_bundle(session, bundle)
        parent = registry.get("source_bundle")
        assert parent is not None
        assert ref.path == parent.path
        assert parent.content_hash == expected_hash
        assert (
            json.loads(Path(parent.path).read_text(encoding="utf-8"))
            == bundle.to_dict()
        )
        assert [row.to_dict() for row in parent.depends_on] == expected_refs
        assert json.dumps(
            [_record_state(registry, row.artifact_id) for row in prior], sort_keys=True
        ) == json.dumps([asdict(row) for row in prior], sort_keys=True)
        assert _record_state(registry, "unrelated") == asdict(unrelated)
        assert registry.get("source_pdf:missing.pdf") is None
        assert counts == {"read": 2 if count else 1, "write": 2 if count else 1}


@pytest.mark.parametrize("mode", ["current", "reclaimed", "commit_guard"])
def test_pdf_batch_keeps_queue_facade_and_commit_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    clock = [datetime(2026, 10, 25, tzinfo=timezone.utc)]
    monkeypatch.setattr(
        PersistentQueueService, "_now_datetime", staticmethod(lambda: clock[0])
    )
    bundle = _bundle(tmp_path / "papers", 3)
    workspace = JobWorkspace(str(tmp_path / "output"), "batch-files", "queue-file-unit")
    workspace.ensure_exists()
    service = PersistentQueueService(tmp_path / "queue.json")
    service.add_job(
        QueueJobSpec(
            job_id=workspace.job_id, job_type="review", project_name="batch-files"
        )
    )
    lease = service.claim_job(workspace.job_id, worker_id="original", lease_seconds=60)
    assert lease is not None
    publication = service.publication_context(lease)
    registry = publication.registry(workspace.paths.registry_path, workspace.job_id)
    bridge = _bridge(bundle, workspace.job_id, tmp_path / "papers")
    session = _session(workspace, registry, publication)
    initial = bridge.persist_source_bundle(session, bundle)
    before = Path(registry.registry_path).read_bytes()
    revision = registry.revision
    bundle_bytes = Path(initial.path).read_bytes()
    verified: list[str] = []
    verify = registry._verify_ready_artifact

    def verify_source(record: Any) -> None:
        verify(record)
        if record.artifact_type == "source_pdf":
            verified.append(record.artifact_id)

    monkeypatch.setattr(registry, "_verify_ready_artifact", verify_source)
    if mode == "reclaimed":
        clock[0] += timedelta(seconds=120)
        assert service.recover_expired_leases() == [workspace.job_id]
        replacement = service.claim_job(
            workspace.job_id, worker_id="replacement", lease_seconds=60
        )
        assert replacement is not None
        assert replacement.lease_generation > lease.lease_generation
    elif mode == "commit_guard":

        def reject_commit() -> Any:
            raise QueuePublicationRejected("injected commit fence rejection")

        monkeypatch.setattr(registry, "_publication_guard", reject_commit)

    if mode == "current":
        repeated = bridge.persist_source_bundle(session, bundle)
        assert repeated.path == initial.path
        assert Path(repeated.path).read_bytes() == bundle_bytes
        for item in bundle.paper_work_items[1:]:
            record = registry.get(f"source_pdf:{Path(item.source_pdf).name}")
            assert record is not None
            fence = record.metadata["publication_fence"]
            assert (
                fence["lease_id"],
                fence["lease_generation"],
                fence["fence_token"],
            ) == (
                lease.lease_id,
                lease.lease_generation,
                lease.fence_token,
            )
    else:
        with pytest.raises(QueuePublicationRejected):
            bridge.persist_source_bundle(session, bundle)
        assert Path(registry.registry_path).read_bytes() == before
        assert registry.revision == revision
        assert Path(initial.path).read_bytes() == bundle_bytes
        assert len(verified) == (3 if mode == "commit_guard" else 0)

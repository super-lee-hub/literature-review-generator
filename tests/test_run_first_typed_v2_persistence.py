from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runtime.orchestrator import InternalStageExecutorRegistry
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.queue_service import LocalPublicationContext
from services.summary_correction_reuse import (
    export_owner_corrected_stage1_reuse_authority,
)
from tests.test_runtime_bridge_helpers import make_bridge_session
from tests.test_summary_correction_reuse import _prepare_approved_synthetic_source


def _resolver(
    *registries: ArtifactRegistry,
) -> Callable[[str], ArtifactRegistry | None]:
    by_job_id = {registry.job_id: registry for registry in registries}
    return lambda job_id: by_job_id.get(job_id)


def _export_approved_owner_v2(tmp_path: Path) -> tuple[Any, ...]:
    prepared = _prepare_approved_synthetic_source(tmp_path)
    (
        _source_workspace,
        source_registry,
        source_record,
        _source_summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        correction_workspace,
        correction_registry,
        _correction,
        adoption,
    ) = prepared
    resolver = _resolver(source_registry, correction_registry)
    exported = export_owner_corrected_stage1_reuse_authority(
        origin_registry=source_registry,
        correction_registry=correction_registry,
        correction_workspace=correction_workspace,
        adoption_receipt_artifact_id=adoption.adoption_receipt_artifact_id,
        publication_context=LocalPublicationContext(),
        external_registry_resolver=resolver,
    )
    imported = InternalStageExecutorRegistry._summary_payloads_from_file(
        exported.owner_corrected_manifest_path
    )
    assert len(imported) == 1
    manifest_payload = json.loads(
        Path(exported.owner_corrected_manifest_path).read_text(encoding="utf-8")
    )
    assert manifest_payload["owner_correction_authority"]["origin_source"]["artifact_id"] == (
        source_record.artifact_id
    )
    assert manifest_payload["owner_correction_authority"]["origin_source"]["content_hash"] == (
        source_record.content_hash
    )
    owner_action = manifest_payload["owner_correction_authority"]["owner_action"]
    assert owner_action["method"] == "manual_operator_action"
    assert owner_action["actor"] == "synthetic-owner"
    assert owner_action["reason"] == "Review and adopt the synthetic source correction"
    return source_registry, correction_registry, resolver, exported, imported[0]


def test_owner_corrected_v2_typed_authority_persists_downstream(
    tmp_path: Path,
) -> None:
    source_registry, correction_registry, resolver, exported, imported = (
        _export_approved_owner_v2(tmp_path)
    )
    source_registry_before = (file_sha256(source_registry.registry_path), source_registry.revision)
    correction_registry_before = (
        file_sha256(correction_registry.registry_path),
        correction_registry.revision,
    )

    # The executor already has this authorized resolver when it verifies the
    # imported V2 authority. Persistence must carry the same resolver through.
    InternalStageExecutorRegistry._verify_typed_reuse_summaries(
        [imported], external_registry_resolver=resolver
    )
    downstream_root = tmp_path / "downstream"
    downstream_root.mkdir()
    bridge, session, _pdf_dir, _pdf_path = make_bridge_session(
        downstream_root, action="analyze"
    )
    registry_before = (
        file_sha256(session.context.registry.registry_path),
        session.context.registry.revision,
    )
    with pytest.raises(
        RuntimeError,
        match="owner_correction_authorized_registry_resolver_missing",
    ):
        bridge.persist_stage1_results(session, [imported])
    assert (
        file_sha256(session.context.registry.registry_path),
        session.context.registry.revision,
    ) == registry_before

    def foreign_resolver(_job_id: str) -> ArtifactRegistry:
        return source_registry

    with pytest.raises(RuntimeError, match="owner_corrected_typed_manifest_untrusted"):
        InternalStageExecutorRegistry._verify_typed_reuse_summaries(
            [imported], external_registry_resolver=foreign_resolver
        )

    tampered = copy.deepcopy(imported)
    tampered["stage1_reuse"]["typed_manifest_artifact_hash"] = "0" * 64
    with pytest.raises(RuntimeError, match="typed_manifest_file_hash_mismatch"):
        InternalStageExecutorRegistry._verify_typed_reuse_summaries(
            [tampered], external_registry_resolver=resolver
        )

    result = bridge.persist_stage1_results(
        session,
        [imported],
        external_registry_resolver=resolver,
    )

    summary_record = session.context.registry.get("summary_file")
    assert result.success is True
    assert summary_record is not None and summary_record.status == "ready"
    persisted_rows = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
    assert len(persisted_rows) == 1
    assert persisted_rows[0]["ai_summary"] == imported["ai_summary"]
    assert persisted_rows[0]["stage1_reuse"]["typed_manifest_artifact_hash"] == (
        imported["stage1_reuse"]["typed_manifest_artifact_hash"]
    )
    assert result.metadata["actual_provider_transport_count"] == 0
    assert not any(
        record.artifact_type == "paper_artifact"
        for record in session.context.registry.list_records()
    )
    assert (file_sha256(source_registry.registry_path), source_registry.revision) == source_registry_before
    assert (
        file_sha256(correction_registry.registry_path),
        correction_registry.revision,
    ) == correction_registry_before
    assert file_sha256(exported.owner_corrected_manifest_path) == (
        exported.owner_corrected_manifest_artifact_hash
    )

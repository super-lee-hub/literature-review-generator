"""Repair revalidation must preserve absent, empty and supplied source inputs."""
from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.provider_receipt_closure import ProviderReceiptClosure
from services.artifact_registry import ArtifactRecord
from services.job_workspace import JobWorkspace
from tests.test_validation_source_binding import (
    _summary_file,
    upstream_workspace as upstream_workspace,
)
from validation import current_validation
from validation.execution_service import ValidationExecutionService
from validation.source_binding import (
    BINDING_ARTIFACT_VERSION,
    build_validation_source_binding,
    validation_source_binding_semantic_hash,
)


@pytest.fixture(autouse=True)
def no_provider_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("source-override controls must not use network transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _record(
    root: Path,
    name: str,
    payload: dict[str, Any],
    *,
    artifact_type: str = "paper_artifact",
    artifact_version: str = "v1",
    status: str = "ready",
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    path = root / f"{name}.json"
    raw = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw)
    return ArtifactRecord(
        artifact_id=name,
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=str(path),
        producer="tests",
        job_id="repair-job",
        status=status,
        content_hash=hashlib.sha256(raw).hexdigest(),
        metadata=dict(metadata or {}),
    )


def _service(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    paper_records: tuple[ArtifactRecord, ...] = (),
    registry_records: tuple[ArtifactRecord, ...] = (),
    cited_keys: tuple[str, ...] = (),
) -> tuple[ValidationExecutionService, ArtifactRecord, ArtifactRecord, list[dict[str, Any]]]:
    review = _record(root, "review", {"content": {}}, artifact_type="review_draft")
    citations = _record(
        root,
        "citations",
        {"citation_sets": [{"paper_ids": list(cited_keys)}], "occurrences": []},
        artifact_type="citation_manifest",
    )
    service = ValidationExecutionService(
        job_id="repair-job",
        attempt_id="repair-attempt",
        workspace=JobWorkspace.create(str(root / "output"), "repair", job_id="repair-job"),
        artifact_registry=SimpleNamespace(
            list_records=lambda: list(registry_records), reload=lambda: None
        ),
        settings=SimpleNamespace(),
        summaries=[],
        review_draft_record=review,
        citation_manifest_record=citations,
        paper_artifact_records=paper_records,
        visual_artifact_records=(),
        provider_factory=None,
        cancellation_checker=None,
        logger=None,
        validation_source_binding_record=next(
            (record for record in registry_records
             if record.artifact_type == "validation_source_binding"),
            None,
        ),
    )
    finalized: list[dict[str, Any]] = []

    def finalize(**kwargs: Any) -> dict[str, Any]:
        finalized.append(kwargs)
        return {
            "closure": ProviderReceiptClosure.evaluate((), ()),
            "closure_record": SimpleNamespace(artifact_id="repair-provider-closure"),
        }

    monkeypatch.setattr(service, "finalize_provider_receipts", finalize)
    return service, review, citations, finalized


def _revalidate(service: ValidationExecutionService, review: ArtifactRecord,
                citations: ArtifactRecord, root: Path, **kwargs: Any) -> dict[str, Any]:
    return service.revalidate_review_artifacts(
        review_draft_record=review,
        citation_manifest_record=citations,
        output_dir=str(root / "revalidation"),
        result_artifact_id="repaired-validation",
        **kwargs,
    )


@pytest.mark.parametrize("selection", ["absent", "empty", "supplied", "quarantined"])
def test_source_override_is_tristate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    selection: str) -> None:
    default = _record(tmp_path, "default", {"paper_identity": {"canonical_paper_key": "default"}})
    supplied = _record(tmp_path, "supplied", {"paper_identity": {"canonical_paper_key": "supplied"}})
    quarantined = _record(tmp_path, "quarantined", {}, status="quarantined")
    service, review, citations, finalized = _service(
        tmp_path, monkeypatch, paper_records=(default,)
    )
    captured: list[dict[str, Any]] = []

    def validate(actual_service: Any, **kwargs: Any) -> dict[str, Any]:
        assert actual_service is service
        captured.append(kwargs)
        return {"loader_control": True}

    monkeypatch.setattr(current_validation, "run_current_validation", validate)
    kwargs = {} if selection == "absent" else {
        "paper_artifact_records": {
            "empty": [], "supplied": [supplied, quarantined],
            "quarantined": [quarantined],
        }[selection]
    }
    result = _revalidate(service, review, citations, tmp_path, **kwargs)
    passed = captured[0]["paper_artifacts_override"]
    if selection == "absent":
        assert passed is None
    elif selection in {"empty", "quarantined"}:
        assert passed == []
    else:
        assert passed == [{
            "paper_identity": {"canonical_paper_key": "supplied"},
            "_registry_artifact_id": supplied.artifact_id,
            "_registry_artifact_hash": supplied.content_hash,
            "_registry_path": supplied.path,
        }]
    assert len(finalized) == 1
    assert result["provider_receipt_closure_record_id"] == "repair-provider-closure"
    assert captured[0]["validation_scope"] == "repair_revalidation"
    assert captured[0]["review_draft_record_override"] is review
    assert captured[0]["citation_manifest_record_override"] is citations


@pytest.mark.parametrize("with_local", [False, True])
@pytest.mark.parametrize("tampered", [False, True])
def test_absent_override_keeps_real_external_source_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_workspace: tuple[Path, dict[str, str]],
    with_local: bool,
    tampered: bool,
) -> None:
    upstream, identities = upstream_workspace
    summary = _summary_file(upstream, identities["paper_key"])
    binding = build_validation_source_binding(
        summary_sources=[str(summary)], local_registry=None, job_id="repair-job"
    )
    binding_record = _record(
        tmp_path, "external-binding", binding,
        artifact_type="validation_source_binding", artifact_version=BINDING_ARTIFACT_VERSION,
        metadata={"semantic_payload_hash": validation_source_binding_semantic_hash(binding)},
    )
    local = _record(tmp_path, "local", {"paper_identity": {"canonical_paper_key": "local"}})
    local_records = (local,) if with_local else ()
    service, review, citations, finalized = _service(
        tmp_path, monkeypatch, paper_records=local_records,
        registry_records=(binding_record, *local_records),
        cited_keys=(identities["paper_key"], "local") if with_local else (identities["paper_key"],),
    )
    if tampered:
        Path(binding["papers"][identities["paper_key"]]["evidence_manifest_path"]).write_text(
            '{"changed":true}', encoding="utf-8"
        )

    def validate(actual_service: Any, **kwargs: Any) -> dict[str, Any]:
        _, _, papers, evidence, _ = current_validation._load_inputs(
            actual_service,
            review_draft_override=kwargs["review_draft_override"],
            citation_manifest_override=kwargs["citation_manifest_override"],
            paper_artifacts_override=kwargs["paper_artifacts_override"],
        )
        return {"loaded_papers": papers, "loaded_evidence": evidence}

    monkeypatch.setattr(current_validation, "run_current_validation", validate)
    result = _revalidate(service, review, citations, tmp_path)
    keys = {row["paper_identity"]["canonical_paper_key"] for row in result["loaded_papers"]}
    assert ("local" in keys) is with_local
    if tampered:
        assert identities["paper_key"] not in keys
        assert any(
            "evidence_manifest_hash_mismatch" in issue
            for issue in service._validation_source_authority_diagnostics
        ), service._validation_source_authority_diagnostics
    else:
        assert identities["paper_key"] in keys, service._validation_source_authority_diagnostics
        assert result["loaded_evidence"][identities["paper_key"]]["markdown_path"]
        assert service._validation_source_authority_diagnostics == ()
    assert len(finalized) == 1

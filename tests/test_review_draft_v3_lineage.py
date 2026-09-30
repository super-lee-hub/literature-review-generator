from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import runtime.orchestrator as orchestrator_module
from runtime.artifact_validators import ArtifactSchemaError, _validate_review_json
from runtime.reconcile import DEFAULT_SCHEMA_VALIDATORS, ReconcileValidationError
from runtime.orchestrator import _RuntimeStageHost
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, file_sha256
from services.review_draft import build_review_draft


def _write_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return str(path)


def _record(
    *,
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    path: str,
    job_id: str,
    metadata: Mapping[str, Any] | None = None,
    depends_on: list[ArtifactDependencyRefV2] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id=artifact_id,
        artifact_role=artifact_type,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        path=path,
        producer="tests.test_review_draft_v3_lineage",
        job_id=job_id,
        status="ready",
        content_hash=file_sha256(path),
        depends_on=list(depends_on or ()),
        metadata=dict(metadata or {}),
    )


class _Registry:
    def __init__(self, job_id: str, records: Mapping[str, Any]) -> None:
        self.job_id = job_id
        self.records = dict(records)
        self.publication_context = None

    def get(self, artifact_id: str) -> Any:
        return self.records.get(artifact_id)


class _Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def artifact_path(self, relative: str) -> str:
        return str(self.root / relative)


def _runtime_host(root: Path, registry: _Registry) -> _RuntimeStageHost:
    host = object.__new__(_RuntimeStageHost)
    host.job_workspace = _Workspace(root)
    host.artifact_registry = registry
    host.project_name = "lineage-test"
    host.summary_file = str(root / "summaries.json")
    host.summaries = []
    return host


def _lineage_fixture(
    root: Path,
    *,
    adoption_outline_hash: str | None = None,
    include_writer_pointer: bool = True,
) -> tuple[_RuntimeStageHost, _Registry, Any, list[dict[str, Any]], dict[str, Any]]:
    job_id = "review-lineage-job"
    outline_path = _write_json(
        root / "outline.json",
        {"payload": {"sections": [{"section_id": "section-1"}]}},
    )
    outline = _record(
        artifact_id="outline-v3:final_outline",
        artifact_type="final_outline",
        artifact_version="v3",
        path=outline_path,
        job_id=job_id,
    )
    adoption_path = _write_json(
        root / "adoption.json",
        {"payload": {"payload": {
            "final_outline_hash": adoption_outline_hash or outline.content_hash,
        }}},
    )
    adoption = _record(
        artifact_id="outline-v3:adoption:fixture",
        artifact_type="adopted_outline",
        artifact_version="v3",
        path=adoption_path,
        job_id=job_id,
    )
    adoption_pointer_path = _write_json(
        root / "adoption-pointer.json",
        {"payload": {
            "current_adoption_artifact_id": adoption.artifact_id,
            "current_adoption_hash": adoption.content_hash,
        }},
    )
    adoption_pointer = _record(
        artifact_id="outline-v3:adoption:current",
        artifact_type="outline_adoption_pointer",
        artifact_version="v1",
        path=adoption_pointer_path,
        job_id=job_id,
    )
    catalog_path = _write_json(root / "catalog.json", {"catalog_hash": "fixture"})
    catalog = _record(
        artifact_id="citation_ref_catalog",
        artifact_type="citation_ref_catalog",
        artifact_version="v1",
        path=catalog_path,
        job_id=job_id,
    )
    section = {
        "section_number": 1,
        "section_title": "Evidence synthesis",
        "blocks": [{"block_id": "s1-b1", "text": "The bounded result."}],
        "evidence_packet_id": "section-1",
        "provider_receipt_ids": ["review:section-1:receipt"],
    }
    section_payload = {
        "artifact_type": "review_section",
        "artifact_version": "v3",
        "status": "ready",
        "section_id": "section-1",
        "binding_hash": "a" * 64,
        "content_hash": hash_json(section),
        "section": section,
    }
    immutable_path = _write_json(root / "immutable-section.json", {"payload": section_payload})
    immutable = _record(
        artifact_id="review-section:section-1:immutable",
        artifact_type="review_section",
        artifact_version="v3",
        path=immutable_path,
        job_id=job_id,
        metadata={"immutable": True, "section_content_hash": hash_json(section)},
    )
    pointer_path = _write_json(root / "section-pointer.json", {"payload": section_payload})
    pointer = _record(
        artifact_id="review-section:section-1",
        artifact_type="review_section",
        artifact_version="v3",
        path=pointer_path,
        job_id=job_id,
        metadata={
            "pointer_role": "current",
            "current_version_artifact_id": immutable.artifact_id,
        },
        depends_on=[ArtifactDependencyRefV2.from_record(immutable)],
    )
    records = {
        outline.artifact_id: outline,
        adoption_pointer.artifact_id: adoption_pointer,
        catalog.artifact_id: catalog,
        immutable.artifact_id: immutable,
    }
    if include_writer_pointer:
        records[pointer.artifact_id] = pointer
    registry = _Registry(job_id, records)
    host = _runtime_host(root, registry)
    return host, registry, adoption, [section], {
        "outline": outline,
        "adoption_pointer": adoption_pointer,
        "catalog": catalog,
        "pointer": pointer,
        "immutable": immutable,
    }


def test_outline_v3_draft_publishes_adoption_and_immutable_writer_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, registry, adoption, sections, records = _lineage_fixture(tmp_path)
    registry.records[adoption.artifact_id] = adoption
    monkeypatch.setattr(orchestrator_module, "current_adoption_record", lambda _registry: adoption)
    publications: list[dict[str, Any]] = []

    def capture_publish(_context: Any, _registry: Any, _path: Any, payload: Mapping[str, Any], **kwargs: Any) -> Any:
        publications.append({"payload": dict(payload), **kwargs})
        return SimpleNamespace(status="ready")

    monkeypatch.setattr(orchestrator_module, "publish_json_artifact", capture_publish)
    assert host._persist_review_draft(
        outline_file=records["outline"].path,
        review_sections=sections,
        references=[],
        word_file=str(tmp_path / "review.docx"),
        generation_mode="outline_v3",
        citation_ref_catalog={"entries": []},
    )

    assert len(publications) == 1
    published = publications[0]
    context = published["payload"]["generation_context"]
    assert context["outline_artifact_id"] == records["outline"].artifact_id
    assert context["outline_artifact_hash"] == records["outline"].content_hash
    assert context["adoption_artifact_id"] == adoption.artifact_id
    assert context["adoption_artifact_hash"] == adoption.content_hash
    assert context["writer_section_artifacts"] == [{
        "artifact_id": records["immutable"].artifact_id,
        "content_hash": records["immutable"].content_hash,
    }]
    dependency_ids = {item.artifact_id for item in published["depends_on"]}
    assert dependency_ids == {
        records["outline"].artifact_id,
        adoption.artifact_id,
        records["adoption_pointer"].artifact_id,
        records["catalog"].artifact_id,
        records["immutable"].artifact_id,
    }


@pytest.mark.parametrize("fault", ["stale_adoption", "missing_writer_pointer"])
def test_outline_v3_draft_refuses_stale_or_incomplete_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    host, registry, adoption, sections, records = _lineage_fixture(
        tmp_path,
        adoption_outline_hash=("f" * 64 if fault == "stale_adoption" else None),
        include_writer_pointer=fault != "missing_writer_pointer",
    )
    registry.records[adoption.artifact_id] = adoption
    monkeypatch.setattr(orchestrator_module, "current_adoption_record", lambda _registry: adoption)
    publications: list[Any] = []
    monkeypatch.setattr(
        orchestrator_module,
        "publish_json_artifact",
        lambda *args, **kwargs: publications.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="different final outline|no ready current Registry pointer"):
        host._persist_review_draft(
            outline_file=records["outline"].path,
            review_sections=sections,
            references=[],
            word_file=str(tmp_path / "review.docx"),
            generation_mode="outline_v3",
            citation_ref_catalog={"entries": []},
        )
    assert not publications


def test_review_draft_v3_validator_requires_lineage_only_for_outline_mode() -> None:
    common = {
        "job_id": "review-lineage-job",
        "project_name": "lineage-test",
        "draft_id": "review_draft",
        "outline_artifact_id": "outline-v3:final_outline",
        "outline_source_path": "outline.json",
        "summary_file": "summaries.json",
        "review_word_path": "review.docx",
        "sections": [{
            "section_number": 1,
            "section_title": "Evidence synthesis",
            "blocks": [{"block_id": "s1-b1", "text": "A bounded result."}],
        }],
        "references": [],
    }
    lineage = {
        "outline_artifact_hash": "a" * 64,
        "adoption_artifact_id": "outline-v3:adoption:fixture",
        "adoption_artifact_hash": "b" * 64,
        "writer_section_artifacts": [{
            "artifact_id": "review-section:section-1:immutable",
            "content_hash": "c" * 64,
        }],
    }
    record = SimpleNamespace(
        artifact_type="review_draft",
        artifact_version="v3",
        job_id="review-lineage-job",
    )
    outline_draft = build_review_draft(
        **common,
        generation_mode="outline_v3",
        **lineage,
    ).to_dict()
    _validate_review_json(record, "review_draft.json", outline_draft)
    outline_draft["generation_context"].pop("writer_section_artifacts")
    with pytest.raises(ArtifactSchemaError, match="writer_section_artifacts"):
        _validate_review_json(record, "review_draft.json", outline_draft)

    legacy_draft = build_review_draft(
        **common,
        generation_mode="full_review",
    ).to_dict()
    _validate_review_json(record, "review_draft.json", legacy_draft)


def test_reconcile_uses_strict_lineage_validator_for_outline_v3(tmp_path: Path) -> None:
    payload = build_review_draft(
        job_id="review-lineage-job",
        project_name="lineage-test",
        draft_id="review_draft",
        outline_artifact_id="outline-v3:final_outline",
        outline_source_path="outline.json",
        summary_file="summaries.json",
        review_word_path="review.docx",
        sections=[{
            "section_number": 1,
            "section_title": "Evidence synthesis",
            "blocks": [{"block_id": "s1-b1", "text": "A bounded result."}],
        }],
        references=[],
        generation_mode="outline_v3",
        outline_artifact_hash="a" * 64,
        adoption_artifact_id="outline-v3:adoption:fixture",
        adoption_artifact_hash="b" * 64,
        writer_section_artifacts=[{
            "artifact_id": "review-section:section-1:immutable",
            "content_hash": "c" * 64,
        }],
    ).to_dict()
    path = tmp_path / "review-draft.json"
    _write_json(path, payload)
    record = _record(
        artifact_id="review-draft:current",
        artifact_type="review_draft",
        artifact_version="v3",
        path=str(path),
        job_id="review-lineage-job",
    )

    DEFAULT_SCHEMA_VALIDATORS["review_draft"](record, path)

    invalid = dict(payload)
    invalid["generation_context"] = dict(payload["generation_context"])
    invalid["generation_context"].pop("outline_artifact_hash")
    _write_json(path, invalid)
    record = _record(
        artifact_id="review-draft:current",
        artifact_type="review_draft",
        artifact_version="v3",
        path=str(path),
        job_id="review-lineage-job",
    )
    with pytest.raises(ReconcileValidationError, match="outline_artifact_hash"):
        DEFAULT_SCHEMA_VALIDATORS["review_draft"](record, path)

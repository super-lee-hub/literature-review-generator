from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.provider_runtime import hash_json
from services.artifact_registry import file_sha256
from services.queue_service import LocalPublicationContext
from services.summary_correction_adoption import (
    ADOPTION_RECEIPT_ARTIFACT_TYPE,
    DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
    SourceSummaryCorrectionAdoptionError,
    adopt_source_summary_correction_candidate,
    verify_source_summary_correction_adoption,
)
from test_summary_correction import _prepare


def _candidate_context(tmp_path: Path) -> tuple[Any, ...]:
    return _prepare(tmp_path)


def _invoke_adoption(prepared: tuple[Any, ...], *, actor: str = "research-owner", reason: str = "Adopt verified source correction", expected_hash: str | None = None) -> Any:
    (
        _source_workspace,
        source_registry,
        _source_record,
        _summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        destination_workspace,
        destination_registry,
        prepared_result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    candidate_record = destination_registry.get(prepared_result.candidate_artifact_id)
    assert candidate_record is not None
    return adopt_source_summary_correction_candidate(
        source_registry=source_registry,
        destination_registry=destination_registry,
        workspace=destination_workspace,
        publication_context=LocalPublicationContext(),
        candidate_artifact_id=prepared_result.candidate_artifact_id,
        expected_candidate_hash=expected_hash or candidate_record.content_hash,
        actor=actor,
        reason=reason,
    )


def _registry_snapshot(registry: Any) -> tuple[bytes, int, tuple[str, ...]]:
    registry.reload()
    artifact_ids = tuple(sorted(record.artifact_id for record in registry.list_records()))
    return Path(registry.registry_path).read_bytes(), registry.revision, artifact_ids


def _assert_preflight_failure_did_not_publish(prepared: tuple[Any, ...], before: tuple[Any, ...]) -> None:
    (
        _source_workspace,
        source_registry,
        source_record,
        _summaries,
        pdf_path,
        _pdf_hash,
        _page_renders,
        destination_workspace,
        destination_registry,
        result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    source_before, destination_before, source_summary_hash, pdf_hash, candidate_hash = before
    assert _registry_snapshot(source_registry) == source_before
    assert _registry_snapshot(destination_registry) == destination_before
    assert file_sha256(source_record.path) == source_summary_hash
    assert file_sha256(pdf_path) == pdf_hash
    candidate_record = destination_registry.get(result.candidate_artifact_id)
    assert candidate_record is not None
    assert file_sha256(candidate_record.path) == candidate_hash
    assert destination_registry.get("stage1_summaries") is None
    adoption_root = Path(destination_workspace.artifact_path("stage1_summary_correction/adoptions"))
    assert not adoption_root.exists()


def _preflight_snapshot(prepared: tuple[Any, ...]) -> tuple[Any, ...]:
    (
        _source_workspace,
        source_registry,
        source_record,
        _summaries,
        pdf_path,
        _pdf_hash,
        _page_renders,
        _destination_workspace,
        destination_registry,
        result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    candidate_record = destination_registry.get(result.candidate_artifact_id)
    assert candidate_record is not None
    return (
        _registry_snapshot(source_registry),
        _registry_snapshot(destination_registry),
        file_sha256(source_record.path),
        file_sha256(pdf_path),
        file_sha256(candidate_record.path),
    )


def test_owner_adoption_creates_separate_derived_authority_and_is_idempotent(tmp_path: Path) -> None:
    prepared = _candidate_context(tmp_path)
    (
        _source_workspace,
        source_registry,
        source_record,
        source_summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        destination_workspace,
        destination_registry,
        correction_result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    source_registry_before = _registry_snapshot(source_registry)
    source_summary_hash_before = file_sha256(source_record.path)
    destination_pointer_before = destination_registry.get("stage1_summaries")
    candidate_record = destination_registry.get(correction_result.candidate_artifact_id)
    proposal_record = destination_registry.get(correction_result.proposal_artifact_id)
    snapshot_record = destination_registry.get(correction_result.source_snapshot_artifact_id)
    assert candidate_record is not None and proposal_record is not None and snapshot_record is not None
    assert candidate_record.status == proposal_record.status == snapshot_record.status == "quarantined"

    result = _invoke_adoption(prepared)
    assert result.status == "owner_approved_derived_summary_ready"
    assert result.source_registry_unchanged is True
    assert result.usable_as_stage1_reuse is False
    assert result.canonical_pointer_advanced is False
    assert result.provider_calls == 0
    assert result.provider_receipt_ids_created == ()
    assert result.summary_count == 63
    assert _registry_snapshot(source_registry) == source_registry_before
    assert file_sha256(source_record.path) == source_summary_hash_before
    assert destination_registry.get("stage1_summaries") == destination_pointer_before

    destination_registry.reload()
    derived_record = destination_registry.get(result.derived_summary_artifact_id)
    receipt_record = destination_registry.get(result.adoption_receipt_artifact_id)
    assert derived_record is not None and receipt_record is not None
    assert derived_record.status == receipt_record.status == "ready"
    assert derived_record.artifact_type == DERIVED_SUMMARY_SET_ARTIFACT_TYPE
    assert derived_record.artifact_version == "owner-corrected-v1"
    assert receipt_record.artifact_type == ADOPTION_RECEIPT_ARTIFACT_TYPE
    assert receipt_record.artifact_version == "v1"
    derived = json.loads(Path(derived_record.path).read_text(encoding="utf-8"))
    receipt = json.loads(Path(receipt_record.path).read_text(encoding="utf-8"))
    assert len(derived["summaries"]) == len(source_summaries) == 63
    assert derived["candidate_summary_set_hash"] == correction_result.candidate_summary_set_hash
    assert derived["usable_as_stage1_reuse"] is False
    assert derived["canonical_pointer_advanced"] is False
    assert derived["provider_receipt_ids_created"] == []
    assert derived["summaries"][32]["preserved_false"] is False
    assert derived["summaries"][32]["preserved_zero"] == 0
    assert derived["summaries"][32]["preserved_none"] is None
    assert receipt["owner_action"]["actor"] == "research-owner"
    assert receipt["owner_action"]["reason"] == "Adopt verified source correction"
    assert receipt["owner_action"]["expected_candidate_sha256"] == candidate_record.content_hash
    assert receipt["owner_action"]["identity_assurance"] == "actor_string_recorded_not_cryptographically_verified"
    assert receipt["derived_summary"]["artifact_id"] == derived_record.artifact_id
    assert receipt["derived_summary"]["content_hash"] == derived_record.content_hash
    assert receipt["candidate"]["status"] == "quarantined"
    assert receipt["proposal"]["status"] == "quarantined"
    assert receipt["source_snapshot"]["status"] == "quarantined"
    assert receipt["normalized_proposal_hash"] == hash_json(receipt["normalized_proposal"])
    assert source_record.artifact_id in {item.artifact_id for item in derived_record.depends_on}
    assert all(item.artifact_id != candidate_record.artifact_id for item in derived_record.depends_on)
    assert destination_registry.get(candidate_record.artifact_id).status == "quarantined"
    assert destination_registry.get(proposal_record.artifact_id).status == "quarantined"
    assert destination_registry.get(snapshot_record.artifact_id).status == "quarantined"

    verified = verify_source_summary_correction_adoption(
        source_registry=source_registry,
        destination_registry=destination_registry,
        adoption_receipt_artifact_id=receipt_record.artifact_id,
    )
    assert verified.verified is True
    assert verified.summary_count == 63
    assert verified.usable_as_stage1_reuse is False

    destination_registry_before_retry = _registry_snapshot(destination_registry)
    retried = _invoke_adoption(prepared)
    assert retried.status == "already_adopted"
    assert retried.action_id == result.action_id
    assert retried.derived_summary_artifact_id == result.derived_summary_artifact_id
    assert retried.adoption_receipt_artifact_id == result.adoption_receipt_artifact_id
    assert _registry_snapshot(destination_registry) == destination_registry_before_retry


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("candidate_hash", "expected candidate hash"),
        ("actor", "requires an actor"),
        ("pdf", "PDF SHA-256 does not match"),
        ("source", "source Stage1 READY closure failed"),
        ("candidate_bytes", "candidate bytes do not match the Registry hash"),
    ],
)
def test_invalid_owner_adoption_fails_before_any_registry_or_output_write(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    prepared = _candidate_context(tmp_path)
    (
        _source_workspace,
        _source_registry,
        source_record,
        _summaries,
        pdf_path,
        _pdf_hash,
        _page_renders,
        _destination_workspace,
        destination_registry,
        result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    candidate_record = destination_registry.get(result.candidate_artifact_id)
    assert candidate_record is not None
    if failure == "pdf":
        pdf_path.write_bytes(Path(pdf_path).read_bytes() + b"stale-pdf")
    elif failure == "source":
        Path(source_record.path).write_bytes(Path(source_record.path).read_bytes() + b"stale-source")
    elif failure == "candidate_bytes":
        Path(candidate_record.path).write_bytes(Path(candidate_record.path).read_bytes() + b"tampered-candidate")
    before = _preflight_snapshot(prepared)

    kwargs: dict[str, Any] = {}
    if failure == "candidate_hash":
        kwargs["expected_hash"] = "0" * 64
    if failure == "actor":
        kwargs["actor"] = "  "
    with pytest.raises(SourceSummaryCorrectionAdoptionError, match=message):
        _invoke_adoption(prepared, **kwargs)

    _assert_preflight_failure_did_not_publish(prepared, before)


def test_partial_derived_publication_can_be_resumed_without_releasing_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _candidate_context(tmp_path)
    (
        _source_workspace,
        source_registry,
        source_record,
        _source_summaries,
        _pdf_path,
        _pdf_hash,
        _page_renders,
        destination_workspace,
        destination_registry,
        correction_result,
        _source_registry_hash,
        _source_summary_hash,
    ) = prepared
    source_registry_before = _registry_snapshot(source_registry)
    candidate_record = destination_registry.get(correction_result.candidate_artifact_id)
    assert candidate_record is not None

    import services.summary_correction_adoption as adoption_module

    publish = adoption_module.publish_json_artifact
    failed_once = False

    def fail_receipt_publication_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed_once
        if kwargs.get("artifact_type") == ADOPTION_RECEIPT_ARTIFACT_TYPE and not failed_once:
            failed_once = True
            raise RuntimeError("simulated interrupted receipt publication")
        return publish(*args, **kwargs)

    monkeypatch.setattr(adoption_module, "publish_json_artifact", fail_receipt_publication_once)
    with pytest.raises(RuntimeError, match="simulated interrupted"):
        _invoke_adoption(prepared)
    derived_records = [
        record for record in destination_registry.list_records()
        if record.artifact_type == DERIVED_SUMMARY_SET_ARTIFACT_TYPE
    ]
    assert len(derived_records) == 1
    assert destination_registry.get(correction_result.candidate_artifact_id).status == "quarantined"

    monkeypatch.setattr(adoption_module, "publish_json_artifact", publish)
    resumed = _invoke_adoption(prepared)
    assert resumed.status == "owner_approved_derived_summary_ready"
    assert resumed.source_registry_unchanged is True
    assert _registry_snapshot(source_registry) == source_registry_before
    assert destination_registry.get(correction_result.candidate_artifact_id).status == "quarantined"
    assert len([
        record for record in destination_registry.list_records()
        if record.artifact_type == DERIVED_SUMMARY_SET_ARTIFACT_TYPE
    ]) == 1
    assert len([
        record for record in destination_registry.list_records()
        if record.artifact_type == ADOPTION_RECEIPT_ARTIFACT_TYPE
    ]) == 1

from __future__ import annotations

from dataclasses import replace

import pytest

from validation.disposition import ValidationDispositionV1


def test_optional_validation_disposition_is_typed_and_hash_bound() -> None:
    disposition = ValidationDispositionV1.create(
        job_id="job-optional-validation",
        stage_plan_hash="a" * 64,
        spec_hash="b" * 64,
        review_draft_artifact_id="review-draft",
        review_draft_artifact_hash="c" * 64,
        citation_manifest_artifact_id="citation-manifest",
        citation_manifest_artifact_hash="d" * 64,
        review_docx_artifact_id="review-docx",
        review_docx_artifact_hash="e" * 64,
        actor="runtime.test",
        reason="validation was explicitly not requested",
    )

    payload = disposition.to_dict()
    assert payload["artifact_type"] == "validation_disposition"
    assert payload["artifact_version"] == "v1"
    assert payload["status"] == "not_requested"
    assert payload["allow_unvalidated"] is True
    restored = ValidationDispositionV1.from_dict(payload)
    assert restored.canonical_payload() == disposition.canonical_payload()
    assert restored.disposition_hash == payload["disposition_hash"]

    with pytest.raises(ValueError, match="status must be not_requested"):
        replace(disposition, status="clean").validate()

    tampered = dict(payload)
    tampered["reason"] = "tampered"
    with pytest.raises(ValueError, match="disposition_hash"):
        ValidationDispositionV1.from_dict(tampered)

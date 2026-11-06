from __future__ import annotations

from pathlib import Path

from services.job_workspace import publish_json_artifact
from services.queue_service import LocalPublicationContext
from services.summary_correction_adoption import verify_source_summary_correction_adoption
from tests.test_summary_correction_adoption import _candidate_context, _invoke_adoption


def test_unrelated_registry_publication_does_not_revoke_exact_owner_correction(tmp_path: Path) -> None:
    prepared = _candidate_context(tmp_path)
    result = _invoke_adoption(prepared)
    source_workspace, source_registry = prepared[:2]
    destination_registry = prepared[8]
    publish_json_artifact(
        LocalPublicationContext(), source_registry,
        source_workspace.artifact_path("unrelated-local-note.json"), {"note": "Unrelated local record."},
        artifact_id="unrelated-note", artifact_role="audit_note", artifact_type="audit_note",
        artifact_version="v1", producer="tests.test_correction_registry_independence",
    )
    verified = verify_source_summary_correction_adoption(
        source_registry=source_registry, destination_registry=destination_registry,
        adoption_receipt_artifact_id=result.adoption_receipt_artifact_id,
    )
    assert verified.verified is True

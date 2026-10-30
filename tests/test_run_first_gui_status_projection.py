from __future__ import annotations

import importlib
import sys
import types
from copy import deepcopy
from typing import Any

import pytest


@pytest.fixture
def gui_app_module(monkeypatch: pytest.MonkeyPatch):
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    module = importlib.import_module("gui.app")
    return importlib.reload(module)


JOB_ID = "gui-current-status-job"
FINAL_OUTLINE_ID = "outline-v3:final_outline"
FINAL_OUTLINE_HASH = "f" * 64
GATE_RECORDS = (
    (FINAL_OUTLINE_ID, "final_outline", "v3", FINAL_OUTLINE_HASH),
    ("outline-v3:coverage_audit", "coverage_audit", "v1", "c" * 64),
    ("outline-v3:stability_audit", "stability_audit", "v1", "5" * 64),
    ("outline-v3:provider_receipt_closure", "provider_receipt_closure", "v1", "6" * 64),
    ("outline-v3:stage_health", "outline_stage_health", "v1", "7" * 64),
)


def _record(
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    content_hash: str,
    *,
    artifact_role: str = "",
    metadata: dict[str, Any] | None = None,
    depends_on: list[dict[str, Any]] | None = None,
    job_id: str = JOB_ID,
    status: str = "ready",
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "artifact_version": artifact_version,
        "artifact_role": artifact_role or artifact_type,
        "job_id": job_id,
        "status": status,
        "content_hash": content_hash,
        "metadata": dict(metadata or {}),
        "depends_on": list(depends_on or []),
    }


def _add_integrity(inspection: dict[str, Any], record: dict[str, Any], status: str = "verified") -> None:
    inspection["integrity"].append({
        "artifact_id": record["artifact_id"],
        "status": status,
        "content_hash": record["content_hash"],
    })


def _inspection_with_current_adoption(
    *,
    include_pointer: bool = True,
    pointer_verified: bool = True,
    pointer_target_hash: str = "",
    include_unrelated_adoption: bool = False,
) -> dict[str, Any]:
    inspection: dict[str, Any] = {
        "job_id": JOB_ID,
        "status": {"job_id": JOB_ID, "job_status": "completed"},
        "artifacts": [],
        "integrity": [],
        "provider_receipts": {"complete": True},
    }
    records = [
        _record(artifact_id, artifact_type, version, content_hash)
        for artifact_id, artifact_type, version, content_hash in GATE_RECORDS
    ]
    inspection["artifacts"].extend(records)
    for record in records:
        _add_integrity(inspection, record)

    target_id = "outline-v3:adoption:current-final"
    target_hash = "a" * 64
    target_dependencies = [
        {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "job_id": JOB_ID,
            "content_hash": content_hash,
        }
        for artifact_id, artifact_type, _version, content_hash in GATE_RECORDS
    ]
    target = _record(
        target_id,
        "adopted_outline",
        "v3",
        target_hash,
        artifact_role="outline_v3_adoption",
        metadata={
            "actor": "reviewer-1",
            "reason": "approved current source comparison",
            "expected_hash": FINAL_OUTLINE_HASH,
        },
        depends_on=target_dependencies,
    )
    inspection["artifacts"].append(target)
    _add_integrity(inspection, target)

    if include_unrelated_adoption:
        old_target = _record(
            "outline-v3:adoption:older-final",
            "adopted_outline",
            "v3",
            "b" * 64,
            artifact_role="outline_v3_adoption",
            metadata={"actor": "old", "reason": "old outline", "expected_hash": "d" * 64},
        )
        inspection["artifacts"].append(old_target)
        _add_integrity(inspection, old_target)

    if include_pointer:
        pointer_hash = pointer_target_hash or target_hash
        pointer = _record(
            "outline-v3:adoption:current",
            "outline_adoption_pointer",
            "v1",
            "d" * 64,
            artifact_role="outline_v3_adoption_current",
            metadata={
                "role": "current",
                "current_adoption_artifact_id": target_id,
                "current_adoption_hash": pointer_hash,
            },
            depends_on=[{
                "artifact_id": target_id,
                "artifact_type": "adopted_outline",
                "job_id": JOB_ID,
                "content_hash": pointer_hash,
            }],
        )
        inspection["artifacts"].append(pointer)
        _add_integrity(inspection, pointer, "verified" if pointer_verified else "untrusted")
    return inspection


def _validation_evidence(
    *,
    disposition: str = "needs_review",
    outer_status: str = "blocked",
    execution_status: str = "succeeded",
    integrity_status: str = "verified",
    review_draft_hash: str = "1" * 64,
    include_artifact: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    review_draft_id = "review_draft"
    citation_manifest_id = "citation_manifest_v3"
    current_review_hash = "1" * 64
    current_manifest_hash = "2" * 64
    validation_id = "validation-report-current"
    validation_hash = "3" * 64
    inspection: dict[str, Any] = {
        "job_id": JOB_ID,
        "status": {"job_id": JOB_ID, "job_status": "completed"},
        "artifacts": [],
        "integrity": [],
        "provider_receipts": {"complete": True},
    }
    review_record = _record(review_draft_id, "review_draft", "v3", current_review_hash)
    manifest_record = _record(citation_manifest_id, "citation_manifest", "v3", current_manifest_hash)
    validation_record = _record(validation_id, "validation_run_result", "v1", validation_hash)
    for record, integrity in (
        (review_record, "verified"),
        (manifest_record, "verified"),
        (validation_record, integrity_status),
    ):
        inspection["artifacts"].append(record)
        _add_integrity(inspection, record, integrity)

    semantic: dict[str, Any] = {
        "execution_status": execution_status,
        "validation_disposition": disposition,
        "contract_satisfied": True,
        "claim_verdict_counts": {"wrong_source": 1},
        "input_artifacts": {
            "review_draft_id": review_draft_id,
            "review_draft_hash": review_draft_hash,
            "citation_manifest_id": citation_manifest_id,
            "citation_manifest_hash": current_manifest_hash,
        },
    }
    closure = {
        "job_id": JOB_ID,
        "status": outer_status,
        "input_artifacts": {
            "review_draft": review_record,
            "citation_manifest": manifest_record,
        },
        "semantic": semantic,
    }
    validation_artifact = {
        "artifact_id": validation_id,
        "artifact_type": "validation_run_result",
        "artifact_version": "v1",
        "status": "ready",
        "content_hash": validation_hash,
    } if include_artifact else None
    validation_status = {
        "job_id": JOB_ID,
        "status": outer_status,
        "closure": closure,
        "validation_artifact": validation_artifact,
    }
    return inspection, validation_status


def test_lifecycle_adoption_requires_verified_current_pointer_target(gui_app_module) -> None:
    current = _inspection_with_current_adoption(include_unrelated_adoption=True)
    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        current,
        {"job_id": JOB_ID, "status": "blocked"},
    )
    assert "adopted" in states
    assert "ready_for_adoption" not in states

    missing_pointer = _inspection_with_current_adoption(
        include_pointer=False,
        include_unrelated_adoption=True,
    )
    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        missing_pointer,
        {"job_id": JOB_ID, "status": "blocked"},
    )
    assert "adopted" not in states
    assert "ready_for_adoption" in states

    tampered_pointer = _inspection_with_current_adoption(pointer_verified=False)
    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        tampered_pointer,
        {"job_id": JOB_ID, "status": "blocked"},
    )
    assert "adopted" not in states
    assert "ready_for_adoption" in states

    stale_pointer = _inspection_with_current_adoption(pointer_target_hash="e" * 64)
    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        stale_pointer,
        {"job_id": JOB_ID, "status": "blocked"},
    )
    assert "adopted" not in states
    assert "ready_for_adoption" in states


def test_lifecycle_validation_uses_verified_current_semantics_under_blocked_outer_status(
    gui_app_module,
) -> None:
    inspection, validation_status = _validation_evidence(
        disposition="needs_review",
        outer_status="blocked",
    )

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        validation_status,
    )

    assert "validation_findings" in states
    assert "validation_not_run" not in states
    assert "validation_clean" not in states

    clean_inspection, clean_validation = _validation_evidence(
        disposition="clean",
        outer_status="blocked",
    )
    clean_states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        clean_inspection,
        clean_validation,
    )
    assert "validation_clean" in clean_states
    assert "validation_not_run" not in clean_states


@pytest.mark.parametrize(
    "overrides",
    [
        {"include_artifact": False},
        {"integrity_status": "untrusted"},
        {"review_draft_hash": "f" * 64},
        {"execution_status": "failed"},
    ],
)
def test_lifecycle_validation_fails_closed_for_missing_stale_tampered_or_failed_result(
    gui_app_module,
    overrides: dict[str, Any],
) -> None:
    inspection, validation_status = _validation_evidence(**overrides)

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        validation_status,
    )

    assert "validation_not_run" in states
    assert "validation_findings" not in states
    assert "validation_clean" not in states


def test_lifecycle_running_validation_does_not_reuse_a_previous_result(gui_app_module) -> None:
    inspection, validation_status = _validation_evidence(disposition="clean")
    validation_status["status"] = "running"
    validation_status["closure"]["semantic"]["execution_status"] = "running"

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        validation_status,
    )

    assert "validation_running" in states
    assert "validation_clean" not in states
    assert "validation_findings" not in states


def _inspection_with_quarantined_repair_and_current_promotion() -> dict[str, Any]:
    transaction_hash = "a" * 64
    transaction_id = "repair-tx:historical-candidate"
    apply_result_id = f"repair_apply_result:{transaction_id}"
    promotion_id = f"repair-promotion:{transaction_hash[:16]}"
    promotion_hash = "f" * 64
    draft_hash = "c" * 64
    citation_hash = "d" * 64
    current_set_hash = "e" * 64
    inspection: dict[str, Any] = {
        "job_id": JOB_ID,
        "status": {"job_id": JOB_ID, "job_status": "completed"},
        "artifacts": [],
        "integrity": [],
        "provider_receipts": {"complete": True},
    }
    transaction = _record(
        transaction_id,
        "repair_transaction",
        "v1",
        transaction_hash,
        status="quarantined",
        depends_on=[{
            "artifact_id": apply_result_id,
            "artifact_type": "repair_apply_result",
            "job_id": JOB_ID,
            "content_hash": "b" * 64,
        }],
    )
    apply_result = _record(
        apply_result_id,
        "repair_apply_result",
        "v1",
        "b" * 64,
        status="quarantined",
        depends_on=[
            {
                "artifact_id": f"review_draft_repaired:{transaction_id}",
                "artifact_type": "review_draft_repaired",
                "job_id": JOB_ID,
                "content_hash": draft_hash,
            },
            {
                "artifact_id": f"citation_manifest_repaired:{transaction_id}",
                "artifact_type": "citation_manifest_repaired",
                "job_id": JOB_ID,
                "content_hash": citation_hash,
            },
        ],
    )
    quarantined_draft = _record(
        f"review_draft_repaired:{transaction_id}",
        "review_draft_repaired",
        "v1",
        draft_hash,
        status="quarantined",
    )
    quarantined_citation = _record(
        f"citation_manifest_repaired:{transaction_id}",
        "citation_manifest_repaired",
        "v1",
        citation_hash,
        status="quarantined",
    )
    current_draft = _record("review_draft:v3:repair:current", "review_draft", "v3", draft_hash)
    current_citation = _record("citation_manifest:v3:repair:current", "citation_manifest", "v3", citation_hash)
    promotion = _record(
        promotion_id,
        "repair_promotion_transaction",
        "v1",
        promotion_hash,
        artifact_role="repair_promotion_transaction",
        depends_on=[
            {
                "artifact_id": current_draft["artifact_id"],
                "artifact_type": "review_draft",
                "job_id": JOB_ID,
                "content_hash": draft_hash,
            },
            {
                "artifact_id": current_citation["artifact_id"],
                "artifact_type": "citation_manifest",
                "job_id": JOB_ID,
                "content_hash": citation_hash,
            },
        ],
    )
    current_set = _record(
        "current-artifact-set:current",
        "current_artifact_set",
        "v1",
        current_set_hash,
        artifact_role="current_artifact_set",
        metadata={
            "promotion_transaction_id": promotion_id,
            "promotion_transaction_hash": promotion_hash,
        },
        depends_on=[
            {
                "artifact_id": current_draft["artifact_id"],
                "artifact_type": "review_draft",
                "job_id": JOB_ID,
                "content_hash": draft_hash,
            },
            {
                "artifact_id": current_citation["artifact_id"],
                "artifact_type": "citation_manifest",
                "job_id": JOB_ID,
                "content_hash": citation_hash,
            },
            {
                "artifact_id": promotion_id,
                "artifact_type": "repair_promotion_transaction",
                "job_id": JOB_ID,
                "content_hash": promotion_hash,
            },
        ],
    )
    pointer = _record(
        "current-artifact-set:pointer",
        "current_artifact_set_pointer",
        "v1",
        current_set_hash,
        artifact_role="current_artifact_set_pointer",
        metadata={
            "promotion_transaction_id": promotion_id,
            "promotion_transaction_hash": promotion_hash,
        },
        depends_on=[{
            "artifact_id": current_set["artifact_id"],
            "artifact_type": "current_artifact_set",
            "job_id": JOB_ID,
            "content_hash": current_set_hash,
        }],
    )
    records = [
        transaction,
        apply_result,
        quarantined_draft,
        quarantined_citation,
        current_draft,
        current_citation,
        promotion,
        current_set,
        pointer,
    ]
    inspection["artifacts"].extend(records)
    for record in (current_draft, current_citation, promotion, current_set, pointer):
        _add_integrity(inspection, record)
    return inspection


def test_lifecycle_quarantine_badge_clears_only_for_current_verified_repair_promotion(
    gui_app_module,
) -> None:
    inspection = _inspection_with_quarantined_repair_and_current_promotion()
    original = deepcopy(inspection)

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        {"job_id": JOB_ID, "status": "blocked"},
    )

    assert "repair_quarantined" not in states
    assert inspection == original


@pytest.mark.parametrize(
    "damage",
    ["missing_pointer", "missing_promotion", "tampered_promotion", "tampered_current_set"],
)
def test_lifecycle_quarantine_badge_stays_when_current_promotion_is_missing_or_tampered(
    gui_app_module,
    damage: str,
) -> None:
    inspection = _inspection_with_quarantined_repair_and_current_promotion()
    if damage == "missing_pointer":
        inspection["artifacts"] = [
            item for item in inspection["artifacts"] if item["artifact_id"] != "current-artifact-set:pointer"
        ]
        inspection["integrity"] = [
            item for item in inspection["integrity"] if item["artifact_id"] != "current-artifact-set:pointer"
        ]
    elif damage == "missing_promotion":
        promotion_id = f"repair-promotion:{'a' * 16}"
        inspection["artifacts"] = [
            item for item in inspection["artifacts"]
            if item["artifact_type"] != "repair_promotion_transaction"
        ]
        inspection["integrity"] = [
            item for item in inspection["integrity"]
            if item["artifact_id"] != promotion_id
        ]
    elif damage == "tampered_promotion":
        promotion = next(
            item for item in inspection["artifacts"]
            if item["artifact_type"] == "repair_promotion_transaction"
        )
        promotion["content_hash"] = "9" * 64
    else:
        current_set = next(
            item for item in inspection["artifacts"]
            if item["artifact_type"] == "current_artifact_set"
        )
        current_set["content_hash"] = "9" * 64

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        {"job_id": JOB_ID, "status": "blocked"},
    )

    assert "repair_quarantined" in states


def test_lifecycle_keeps_another_unpromoted_quarantined_transaction_visible(gui_app_module) -> None:
    inspection = _inspection_with_quarantined_repair_and_current_promotion()
    other_transaction_id = "repair-tx:other-pending"
    other_apply_id = f"repair_apply_result:{other_transaction_id}"
    inspection["artifacts"].extend(
        [
            _record(
                other_transaction_id,
                "repair_transaction",
                "v1",
                "1" * 64,
                status="quarantined",
                depends_on=[{
                    "artifact_id": other_apply_id,
                    "artifact_type": "repair_apply_result",
                    "job_id": JOB_ID,
                    "content_hash": "2" * 64,
                }],
            ),
            _record(
                other_apply_id,
                "repair_apply_result",
                "v1",
                "2" * 64,
                status="quarantined",
            ),
        ]
    )

    states = gui_app_module.WorkspaceController._canonical_lifecycle_states(
        inspection,
        {"job_id": JOB_ID, "status": "blocked"},
    )

    assert "repair_quarantined" in states

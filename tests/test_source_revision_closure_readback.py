"""Component readback tests for durable Writer SourceRevision proofs.

The shared fixture has a synthetic parent that is not a completed whole-run
admission. These tests stub only ``verify_source_revision_plan_v1`` to that
fixture's exact sealed body while reopening the registered plan. The real
Runner acceptance path is exercised separately by the loopback integration
tests, without this stub. The Outline checks below cover component readback;
they do not establish actual Runner acceptance or an S2 cache hit. The current
loopback acceptance case remains Writer-only.
"""
import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.provider_runtime import hash_json
from runtime.source_revision_binding import (
    open_registered_source_revision_replay_context,
    verify_registered_source_revision_reuse,
)
from services.artifact_registry import UnverifiedArtifact, UnverifiedDependency
from services.job_workspace import publish_bytes_artifact, publish_json_artifact
from tests.test_source_revision_replay import (
    _child_call,
    _child_call_for_output,
    _child_output_for_match,
    _lookup,
    _make_case,
)
from validation.review_reuse import review_reuse_call_issues


def _publish_child_writer_output(case):
    section = {
        "section_number": 1,
        "section_title": "Effects",
        "blocks": [{"type": "paragraph", "text": "Supported result"}],
        "evidence_packet_id": "section-U45",
        "provider_receipt_ids": [],
        "writer_task_scope": case["writer_task_scope"],
        "writer_task_dispositions": [{"disposition": "covered"}],
        "provider_semantic_contract_version": "source-closed-provider/v2",
    }
    binding = dict(case["writer_binding_for_output"])
    payload = {
        "artifact_type": "review_section",
        "artifact_version": "v3",
        "status": "ready",
        "job_id": case["child_workspace"].job_id,
        "section_id": "section-U45",
        "binding_hash": hash_json(binding),
        "binding": binding,
        "content_hash": hash_json(section),
        "section": section,
    }
    return publish_json_artifact(
        case["child_publication"],
        case["child_registry"],
        case["child_workspace"].artifact_path("review_sections/source_revision_readback.json"),
        payload,
        artifact_role="review_section",
        artifact_type="review_section",
        artifact_version="v3",
        producer=__name__,
        artifact_id="review-section:source-revision-readback",
        depends_on=[],
    )


def _make_proof_case(tmp_path):
    case = _make_case(tmp_path, stage="stage3_review")
    lookup = _lookup(case)
    assert lookup.status == "match" and lookup.match is not None
    match = lookup.match
    output = _publish_child_writer_output(case)
    child_call = _child_call_for_output(
        case, output, match.response, call=_child_call(case),
    )
    proof = case["replay"].publish_child_reuse_proof(
        match=match,
        child_output_record=output,
        child_plan_record=case["plan_record"],
        child_expected_call=child_call,
        publication_context=case["child_publication"],
        path=case["child_workspace"].artifact_path("source_revision/readback-proof.json"),
    )
    bound = replace(
        child_call,
        verified_reuse=True,
        reuse_evidence_artifact_id=proof.artifact_id,
        reuse_evidence_artifact_hash=proof.content_hash,
        reuse_evidence_record_hash=hash_json(asdict(proof)),
    )
    return case, output, proof, bound


def _review_reuse_issues(case, proof, expected_call, *, dependency_records=None):
    with patch(
        "runtime.source_revision_admission.verify_source_revision_plan_v1",
        return_value=case["plan_body"],
    ):
        return review_reuse_call_issues(
            stage="review",
            expected_call=expected_call,
            registry=case["child_registry"],
            dependency_records=(dependency_records if dependency_records is not None else [proof]),
            external_registry_resolver=case["external_registry_resolver"],
        )


def _make_outline_proof_case(tmp_path, *, audit_mutation=""):
    case = _make_case(tmp_path, stage="outline_v3")
    child_call = replace(_child_call(case), call_id="outline:stable-topic")
    lookup = case["replay"].lookup_response(
        expected_call=child_call,
        request_payload=case["request_payload"],
        current_scope=case["current_scope"],
        parent_scope_record=None,
        route_identity=case["route_identity"],
    )
    assert lookup.status == "match" and lookup.match is not None
    output = _child_output_for_match(case, lookup.match)
    child_call = _child_call_for_output(
        case, output, lookup.match.response, call=child_call,
    )
    proof = case["replay"].publish_child_reuse_proof(
        match=lookup.match,
        child_output_record=output,
        child_plan_record=case["plan_record"],
        child_expected_call=child_call,
        publication_context=case["child_publication"],
        path=case["child_workspace"].artifact_path("source_revision/outline-readback-proof.json"),
    )
    bound = replace(
        child_call,
        verified_reuse=True,
        reuse_evidence_artifact_id=proof.artifact_id,
        reuse_evidence_artifact_hash=proof.content_hash,
        reuse_evidence_record_hash=hash_json(asdict(proof)),
    )
    parent_audit = json.loads(Path(case["parent_scope_record"].path).read_text(encoding="utf-8"))
    audit_row = {
        **parent_audit,
        "job_id": case["child_workspace"].job_id,
        "operation_id": bound.logical_attempt_identity,
        "attempt_id": bound.call_id,
        "physical_attempt_id": bound.attempt_id,
        "closure_epoch_id": bound.closure_epoch_id,
        "node_id": "stable-topic",
        "semantic_node_id": "stable-topic",
        "payload_hash": bound.input_hash,
        "route": {
            **parent_audit["route"],
            "config_section": "Outline_API",
            "provider": bound.provider,
            "model": bound.model,
            "endpoint_type": bound.endpoint_type,
        },
        "route_fingerprint": case["route_identity"]["route_fingerprint"],
        "config_hash": bound.config_hash,
        "schema_hash": bound.schema_hash,
        "semantic_validation_status": "accepted",
        "source_revision_lookup_status": "match",
        "provider_invoked": False,
        "source_revision_scope": case["current_scope"],
    }
    if audit_mutation == "attempt_id":
        audit_row["attempt_id"] = "outline:wrong-topic"
    elif audit_mutation == "payload_hash":
        audit_row["payload_hash"] = hash_json({"wrong": "request"})
    elif audit_mutation == "not_accepted":
        audit_row["semantic_validation_status"] = "rejected"
    elif audit_mutation == "provider_invoked":
        audit_row["provider_invoked"] = True
    audit_bytes = (
        json.dumps(audit_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    audit = publish_bytes_artifact(
        case["child_publication"],
        case["child_registry"],
        case["child_workspace"].artifact_path("outline/request_payload_audit.jsonl"),
        audit_bytes,
        artifact_id=f"outline-v3:request_payload_audit:{bound.closure_epoch_id}",
        artifact_role="outline_request_payload_audit",
        artifact_type="outline_request_payload_audit",
        artifact_version="v1",
        producer=__name__,
    )
    if audit_mutation == "tampered_hash":
        Path(audit.path).write_bytes(audit_bytes + b" ")
    return case, output, proof, bound, audit


def _verify_outline_readback(case, expected_call):
    with patch(
        "runtime.source_revision_admission.verify_source_revision_plan_v1",
        return_value=case["plan_body"],
    ):
        context = open_registered_source_revision_replay_context(
            case["child_registry"], case["external_registry_resolver"],
        )
        verify_registered_source_revision_reuse(context, asdict(expected_call))


def test_review_reuse_readback_accepts_registered_source_revision_proof(tmp_path):
    case, _output, proof, expected = _make_proof_case(tmp_path)

    issues = _review_reuse_issues(case, proof, asdict(expected))

    assert issues == []
    assert proof.artifact_type == "source_revision_provider_reuse"
    assert proof.artifact_id != "review_replay"


def test_review_reuse_readback_rejects_a_wrong_expected_section_call(tmp_path):
    case, _output, proof, expected = _make_proof_case(tmp_path)
    wrong_expected = {**asdict(expected), "node_id": "section:wrong"}

    issues = _review_reuse_issues(case, proof, wrong_expected)

    assert len(issues) == 1
    assert "source_revision_authority_invalid:" in issues[0]
    assert "section identity differs" in issues[0]


def test_review_reuse_readback_rejects_tampered_registered_proof_bytes(tmp_path):
    case, _output, proof, expected = _make_proof_case(tmp_path)
    Path(proof.path).write_text("{}", encoding="utf-8")

    issues = _review_reuse_issues(case, proof, asdict(expected))

    assert len(issues) == 1
    assert "source_revision_authority_invalid:" in issues[0]
    assert (
        "artifact content hash changed" in issues[0]
        or "artifact_hash_mismatch" in issues[0]
        or "source_revision_provider_reuse" in issues[0]
    )


def test_registered_outline_source_revision_readback_uses_v1_attempt_id(tmp_path):
    case, output, proof, expected, audit = _make_outline_proof_case(tmp_path)

    _verify_outline_readback(case, expected)

    assert output.artifact_type == "outline_artifact"
    assert proof.artifact_type == "source_revision_provider_reuse"
    assert audit.artifact_id == f"outline-v3:request_payload_audit:{expected.closure_epoch_id}"
    row = json.loads(Path(audit.path).read_text(encoding="utf-8"))
    assert "call_id" not in row
    assert row["attempt_id"] == expected.call_id == "outline:stable-topic"
    assert row["semantic_node_id"] == "stable-topic"


@pytest.mark.parametrize(
    "audit_mutation",
    ["attempt_id", "payload_hash", "not_accepted", "provider_invoked"],
)
def test_registered_outline_source_revision_readback_rejects_unbound_audit_rows(
    tmp_path, audit_mutation,
):
    case, _output, _proof, expected, _audit = _make_outline_proof_case(
        tmp_path, audit_mutation=audit_mutation,
    )

    with pytest.raises(RuntimeError, match="no unique accepted current Outline request audit"):
        _verify_outline_readback(case, expected)


def test_registered_outline_source_revision_readback_rejects_tampered_audit_hash(tmp_path):
    case, _output, _proof, expected, _audit = _make_outline_proof_case(
        tmp_path, audit_mutation="tampered_hash",
    )

    with pytest.raises(
        (UnverifiedArtifact, UnverifiedDependency),
        match="(artifact|dependency) content hash changed",
    ):
        _verify_outline_readback(case, expected)

from __future__ import annotations

import json

import pytest

from services.writer_task_scope import (
    WriterTaskScopeError,
    build_writer_output_maximum_specimen_v1,
    build_writer_task_scope_v1,
    validate_writer_task_output_v1,
)


def _packet() -> dict:
    claim = "The measured effect holds under the stated condition."
    return {
        "section_id": "section:effect",
        "planned_claims": [claim],
        "claim_support": [
            {
                "claim_id": "writer-claim:effect",
                "claim": claim,
                "paper_key": "paper-A",
                "primary_claim_id": "source-claim:effect",
                "source_claim_ids": ["source-claim:effect", "source-claim:condition"],
                "evidence_ids": ["evidence:effect", "evidence:condition"],
                "source_field_ids": ["field:effect", "field:condition"],
                "qualifier_source_claim_ids": ["source-claim:condition"],
                "qualifier_evidence_ids": ["evidence:condition"],
                "qualifier_source_field_ids": ["field:condition"],
                "opaque_values": {"zero": 0, "false": False},
            }
        ],
        "paper_keys": ["paper-A"],
        "relation_ids": [],
        "evidence_items": [
            {
                "paper_key": "paper-A",
                "summary_hash": "summary-A",
                "view_hash": "view-A",
                "fields": {"findings": ["Effect under condition."], "zero": 0, "false": False},
                "source_fields": {"finding": "Effect under condition."},
                "interpretation_context": [],
            }
        ],
        "source_summary_hashes": ["summary-A"],
        "evidence_view_hashes": ["view-A"],
        "retrieval_provenance": {
            "selection": "section_targeted",
            "paper_keys": ["paper-A"],
            "view_hashes": ["view-A"],
        },
    }


def _catalog() -> dict:
    return {
        "entries": [
            {"ref_id": "R001", "canonical_paper_key": "paper-A", "status": "active"},
            {"ref_id": "R002", "canonical_paper_key": "paper-B", "status": "active"},
        ]
    }


def _minimal_payload(scope: dict, *, text: str | None = None) -> dict:
    ready_task = next(task for task in scope["tasks"] if task["status"] == "ready")
    unit = next(unit for unit in ready_task["output_units"] if unit["required"])
    citation = f"[[cite_ref:{unit['allowed_ref_ids'][0]}]]"
    return {
        "blocks": [
            {
                "writer_task_id": ready_task["writer_task_id"],
                "writer_output_unit_id": unit["writer_output_unit_id"],
                "writer_task_basis_hash": scope["writer_task_basis_hash"],
                "text": text or f"The result remains bounded. {citation}",
            }
        ],
        "task_dispositions": [
            {
                "writer_task_id": task["writer_task_id"],
                "writer_task_basis_hash": scope["writer_task_basis_hash"],
                "disposition": "covered",
            }
            for task in scope["tasks"]
        ],
    }


def test_task_and_unit_ids_are_semantic_and_independent_of_row_order() -> None:
    packet = _packet()
    first = build_writer_task_scope_v1(packet, _catalog())

    reordered = _packet()
    reordered["planned_claims"].reverse()
    reordered["claim_support"].reverse()
    reordered["evidence_items"].reverse()
    reordered_scope = build_writer_task_scope_v1(reordered, _catalog())

    assert first["writer_task_basis_hash"] == reordered_scope["writer_task_basis_hash"]
    assert first["required_task_ids"] == reordered_scope["required_task_ids"]
    assert [
        unit["writer_output_unit_id"] for unit in first["tasks"][0]["output_units"]
    ] == [
        unit["writer_output_unit_id"] for unit in reordered_scope["tasks"][0]["output_units"]
    ]
    task = first["tasks"][0]
    assert task["status"] == "ready"
    assert task["qualifier_source_claim_ids"] == ["source-claim:condition"]
    assert len(task["output_units"]) == 2  # one primary unit plus one source-claim expansion


def test_full_support_and_evidence_bundle_retains_zero_and_false() -> None:
    scope = build_writer_task_scope_v1(_packet(), _catalog())
    task = scope["tasks"][0]

    assert task["support_rows"][0]["opaque_values"] == {"zero": 0, "false": False}
    source_item = task["source_evidence"][0]
    assert source_item["fields"]["zero"] == 0
    assert source_item["fields"]["false"] is False


def test_structural_scope_does_not_claim_independent_source_authority() -> None:
    packet = _packet()
    packet["claim_support"][0]["primary_claim_id"] = "fabricated-source-id"
    packet["claim_support"][0]["source_claim_ids"].append("fabricated-source-id")
    scope = build_writer_task_scope_v1(packet, _catalog())
    assert scope["source_authority_status"] == "canonical_claim_and_evidence_inventory_not_verified"
    assert scope["usable_for_provider_admission"] is False
    output = validate_writer_task_output_v1(scope, _minimal_payload(scope))
    assert output["usable_for_provider_admission"] is False


def test_unknown_source_or_conflicting_ref_stays_needs_review() -> None:
    packet = _packet()
    packet["claim_support"][0]["paper_key"] = "paper-unknown"
    blocked = build_writer_task_scope_v1(packet, _catalog())
    assert blocked["scope_status"] == "needs_review"
    assert blocked["tasks"][0]["status"] == "needs_review"
    assert "unknown_support_source" in blocked["tasks"][0]["reason_codes"]
    assert blocked["tasks"][0]["allowed_ref_ids"] == []
    assert blocked["tasks"][0]["output_units"] == []

    conflicting_catalog = _catalog()
    conflicting_catalog["entries"].append(
        {"ref_id": "R001", "canonical_paper_key": "paper-B", "status": "active"}
    )
    conflicting = build_writer_task_scope_v1(_packet(), conflicting_catalog)
    assert conflicting["scope_status"] == "needs_review"
    assert "conflicting_citation_ref_source" in conflicting["tasks"][0]["reason_codes"]


def test_missing_claim_support_and_orphan_rows_remain_explicit_tasks() -> None:
    packet = _packet()
    packet["planned_claims"].append("A planned scientific claim has no support row.")
    orphan = dict(packet["claim_support"][0])
    orphan["claim"] = "An orphaned support row must remain visible."
    packet["claim_support"].append(orphan)

    scope = build_writer_task_scope_v1(packet, _catalog())
    assert scope["scope_status"] == "needs_review"
    assert len(scope["tasks"]) == 3
    missing = next(task for task in scope["tasks"] if task["planned_claim"].startswith("A planned"))
    orphan_task = next(task for task in scope["tasks"] if task["task_kind"] == "orphan_claim_support")
    assert missing["reason_codes"] == ["missing_claim_support"]
    assert missing["output_units"] == []
    assert orphan_task["support_rows"][0]["claim"] == "An orphaned support row must remain visible."
    assert "support_without_planned_claim" in orphan_task["reason_codes"]


def test_response_requires_every_task_and_rejects_duplicate_or_foreign_identity() -> None:
    scope = build_writer_task_scope_v1(_packet(), _catalog())
    payload = _minimal_payload(scope)
    validate_writer_task_output_v1(scope, payload)

    missing = dict(payload)
    missing["task_dispositions"] = []
    with pytest.raises(WriterTaskScopeError, match="omits task dispositions"):
        validate_writer_task_output_v1(scope, missing)

    duplicate = dict(payload)
    duplicate["task_dispositions"] = [
        *payload["task_dispositions"],
        dict(payload["task_dispositions"][0]),
    ]
    with pytest.raises(WriterTaskScopeError, match="Repeated task disposition"):
        validate_writer_task_output_v1(scope, duplicate)

    foreign = dict(payload)
    foreign["task_dispositions"] = [
        {**payload["task_dispositions"][0], "writer_task_id": "foreign-task"}
    ]
    with pytest.raises(WriterTaskScopeError, match="Foreign task identity"):
        validate_writer_task_output_v1(scope, foreign)

    duplicate_unit = dict(payload)
    duplicate_unit["blocks"] = [
        payload["blocks"][0],
        dict(payload["blocks"][0]),
    ]
    with pytest.raises(WriterTaskScopeError, match="Repeated output unit"):
        validate_writer_task_output_v1(scope, duplicate_unit)


def test_maximum_legal_serialization_is_accepted_and_oversize_is_rejected() -> None:
    scope = build_writer_task_scope_v1(_packet(), _catalog())
    specimen = build_writer_output_maximum_specimen_v1(scope)
    units = {
        (task["writer_task_id"], unit["writer_output_unit_id"]): unit
        for task in scope["tasks"]
        for unit in task["output_units"]
    }
    for block in specimen["blocks"]:
        unit = units[(block["writer_task_id"], block["writer_output_unit_id"])]
        assert len(block["text"]) == unit["max_text_chars"]
        assert len(block["text"].encode("utf-8")) <= unit["max_text_utf8_bytes"]
        assert len(block["text"].encode("utf-8")) > len(block["text"])
    accepted = validate_writer_task_output_v1(scope, specimen)
    assert accepted["block_count"] == scope["max_output_units"]
    serialized_bytes = len(
        json.dumps(specimen, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert serialized_bytes <= scope["max_serialized_output_bytes_upper_bound"]

    oversize = json.loads(json.dumps(specimen))
    oversize["blocks"][0]["text"] += "x"
    with pytest.raises(WriterTaskScopeError, match="exceeds .* characters"):
        validate_writer_task_output_v1(scope, oversize)


def test_output_text_limit_grows_from_task_source_closure() -> None:
    short_scope = build_writer_task_scope_v1(_packet(), _catalog())
    long_packet = _packet()
    long_packet["evidence_items"][0]["fields"]["findings"] = ["Long source evidence. " * 100]
    long_scope = build_writer_task_scope_v1(long_packet, _catalog())

    short_limit = short_scope["tasks"][0]["output_units"][0]["max_text_chars"]
    long_limit = long_scope["tasks"][0]["output_units"][0]["max_text_chars"]
    assert long_limit > short_limit
    assert long_limit <= 4096


def test_source_ref_mismatch_and_multiple_sentences_are_rejected() -> None:
    scope = build_writer_task_scope_v1(_packet(), _catalog())
    foreign_ref_payload = _minimal_payload(scope, text="A result is bounded. [[cite_ref:R002]]")
    with pytest.raises(WriterTaskScopeError, match="cites foreign refs"):
        validate_writer_task_output_v1(scope, foreign_ref_payload)

    multiple_sentences = _minimal_payload(
        scope,
        text="The result is bounded. A second factual sentence follows. [[cite_ref:R001]]",
    )
    with pytest.raises(WriterTaskScopeError, match="exactly one sentence"):
        validate_writer_task_output_v1(scope, multiple_sentences)

    malformed_citation = _minimal_payload(
        scope,
        text="The result is bounded. [[cite_ref:R001]] [[cite_ref:unknown]]",
    )
    with pytest.raises(WriterTaskScopeError, match="malformed citation token"):
        validate_writer_task_output_v1(scope, malformed_citation)


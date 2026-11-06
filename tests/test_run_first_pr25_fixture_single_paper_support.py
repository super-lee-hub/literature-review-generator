from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import pymupdf

from tests.test_pr25_repair_v2 import _grounded_fixture_candidate


REPLAY_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current80_candidate_replay.json"
)
CURRENT80_REPLAY = json.loads(REPLAY_FIXTURE_PATH.read_text(encoding="utf-8"))
CURRENT80_PROVENANCE = CURRENT80_REPLAY["provenance"]
CURRENT80_JOB_ID = CURRENT80_PROVENANCE["job_id"]
CURRENT80_SCOPE_HASH = CURRENT80_PROVENANCE["candidate_output_scope_content_hash"]
CURRENT80_FAILED_PAYLOAD_HASH = CURRENT80_PROVENANCE["failed_payload_sha256"]
CURRENT80_FAILED_NODE_ID = CURRENT80_PROVENANCE["failed_node_id"]
CURRENT80_SLOT = CURRENT80_REPLAY["slot"]
PERSISTED_SOURCE_CLAIM_TEXT = CURRENT80_PROVENANCE["source_text"]
CURRENT82_REPLAY_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current82_full_scope_replay.json"
)
CURRENT82_REPLAY = json.loads(CURRENT82_REPLAY_FIXTURE_PATH.read_text(encoding="utf-8"))
CURRENT82_PROVENANCE = CURRENT82_REPLAY["provenance"]
CURRENT84_REPLAY_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current84_full_context_replay.json"
)
CURRENT84_REPLAY = json.loads(CURRENT84_REPLAY_FIXTURE_PATH.read_text(encoding="utf-8"))
CURRENT84_PROVENANCE = CURRENT84_REPLAY["provenance"]
CURRENT86_REQUEST_BODY_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current86_candidate_request.json"
)
CURRENT86_REQUEST_BODY_BYTES = 89714
CURRENT86_REQUEST_BODY_SHA256 = "f7c7dc77da76dd273d6af967fa4ad5e4072724860b0f28035b613c7a12c2f2a1"
CURRENT86_ALIAS_MAP_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current86_evidence_alias_map.json"
)
CURRENT86_ALIAS_MAP_SHA256 = "73a8fd0bcce11e9802579c99845c2ca88d966b571711fa6dfe4392de0d123db2"
CURRENT87_REQUEST_BODY_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current87_candidate_request.json"
)
CURRENT87_REQUEST_BODY_BYTES = 89714
CURRENT87_REQUEST_BODY_SHA256 = "f7855d3d21a65e271007101e7b3813de4714dce96354cf578cb6c561dd504f43"
CURRENT87_SCOPE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "run_first_current87_candidate_output_scope.json"
)
CURRENT87_SCOPE_FILE_SHA256 = "69994aed2df2dae2692d2d4c7a4cfff1c6f080f0e68a2691308a3e995835b21e"
CURRENT87_SCOPE_CONTENT_HASH = "7740eea79d3b65b2758cc2150d6564c0f57302d0f8338ae5f901f13824c1a615"


def _current86_exact_wire_candidate() -> tuple[dict[str, Any], dict[str, Any]]:
    body = CURRENT86_REQUEST_BODY_PATH.read_bytes()
    assert len(body) == CURRENT86_REQUEST_BODY_BYTES
    assert hashlib.sha256(body).hexdigest() == CURRENT86_REQUEST_BODY_SHA256
    wire = json.loads(body)
    assert wire.get("model") == "outline-local"
    user_messages = [
        str(message.get("content") or "")
        for message in wire.get("messages") or ()
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert len(user_messages) == 1
    envelope = json.loads(user_messages[0])
    assert envelope.get("node_id") == (
        "candidate_1_provider_generation:local:candidate_group_dd3affa725fd0c2d"
    )
    request = envelope.get("request")
    assert isinstance(request, dict)
    return envelope, request


def _current87_exact_wire_candidate() -> tuple[dict[str, Any], dict[str, Any]]:
    body = CURRENT87_REQUEST_BODY_PATH.read_bytes()
    assert len(body) == CURRENT87_REQUEST_BODY_BYTES
    assert hashlib.sha256(body).hexdigest() == CURRENT87_REQUEST_BODY_SHA256
    wire = json.loads(body)
    assert wire.get("model") == "outline-local"
    user_messages = [
        str(message.get("content") or "")
        for message in wire.get("messages") or ()
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert len(user_messages) == 1
    envelope = json.loads(user_messages[0])
    request = envelope.get("request")
    assert isinstance(request, dict)
    assert request["candidate_output_scope"]["required_claim_group_indices"] == [3, 4, 8]
    return envelope, request


def _current87_candidate_output_scope() -> Any:
    from outline.candidate_output_scope import (
        CandidateOutputClaimGroupV1,
        CandidateOutputScopeV1,
        CandidateOutputSupportSlotV1,
    )

    raw_bytes = CURRENT87_SCOPE_PATH.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == CURRENT87_SCOPE_FILE_SHA256
    data = json.loads(raw_bytes)
    assert data.get("content_hash") == CURRENT87_SCOPE_CONTENT_HASH
    binding = data["source_inventory_binding"]
    scope = CandidateOutputScopeV1(
        registry_job_id=str(binding["registry_job_id"]),
        source_inventory_artifact_id=str(binding["artifact_id"]),
        source_inventory_artifact_hash=str(binding["artifact_hash"]),
        source_inventory_content_hash=str(binding["content_hash"]),
        task_ids=tuple(str(value) for value in data["task_ids"]),
        selected_relation_ids=tuple(str(value) for value in data["selected_relation_ids"]),
        claim_groups=tuple(
            CandidateOutputClaimGroupV1(
                claim_group_id=str(row["claim_group_id"]),
                task_id=str(row["task_id"]),
                topic_id=str(row["topic_id"]),
                fragment_id=str(row["fragment_id"]),
                provider_result_id=str(row["provider_result_id"]),
                synthesis_claim_id=str(row["synthesis_claim_id"]),
                claim_hash=str(row["claim_hash"]),
                paper_keys=tuple(str(value) for value in row["paper_keys"]),
                support_slot_ids=tuple(str(value) for value in row["support_slot_ids"]),
                relation_ids=tuple(str(value) for value in row.get("relation_ids") or ()),
                claim_kind=str(row.get("claim_kind") or "provider_authored_source_closed_claim"),
                claim_seed=bool(row.get("claim_seed", False)),
                task_contract=tuple(sorted((row.get("task_contract") or {}).items())),
            )
            for row in data["claim_groups"]
        ),
        claim_slots=tuple(
            CandidateOutputSupportSlotV1(
                claim_slot_id=str(row["claim_slot_id"]),
                claim_group_id=str(row["claim_group_id"]),
                task_id=str(row["task_id"]),
                topic_id=str(row["topic_id"]),
                fragment_id=str(row["fragment_id"]),
                provider_result_id=str(row["provider_result_id"]),
                synthesis_claim_id=str(row["synthesis_claim_id"]),
                paper_key=str(row["paper_key"]),
                primary_claim_id=str(row["primary_claim_id"]),
                study_id=str(row.get("study_id") or ""),
                source_claim_ids=tuple(str(value) for value in row["source_claim_ids"]),
                evidence_ids=tuple(str(value) for value in row["evidence_ids"]),
                source_field_ids=tuple(str(value) for value in row["source_field_ids"]),
                relation_ids=tuple(str(value) for value in row.get("relation_ids") or ()),
            )
            for row in data["claim_slots"]
        ),
    )
    assert scope.content_hash == CURRENT87_SCOPE_CONTENT_HASH
    return scope


def _validate_current87_full_scope_output(
    full_scope: Any,
    request_scope: dict[str, Any],
    canonical_content: dict[str, Any],
) -> set[str]:
    full_scope.validate(canonical_content)
    group_ids = [str(value) for value in request_scope.get("claim_group_ids") or ()]
    required_indices = [int(value) for value in request_scope.get("required_claim_group_indices") or ()]
    if any(index < 0 or index >= len(group_ids) for index in required_indices):
        raise AssertionError("current87 required group index is outside the assigned scope")
    required_group_ids = {group_ids[index] for index in required_indices}
    allowed_papers = ["studya_unknown_author", "studyb_unknown_author", "studyc_unknown_author", "studyd_unknown_author"]
    assigned_scope = full_scope.for_groups(group_ids, allowed_paper_keys=allowed_papers)
    assigned_scope.validate(canonical_content)
    slot_group = {
        slot.claim_slot_id: slot.claim_group_id
        for slot in assigned_scope.claim_slots
    }
    emitted_slots = {
        str(row.get("claim_slot_id") or "")
        for section in canonical_content.get("sections") or ()
        if isinstance(section, dict)
        for row in section.get("claim_support") or ()
        if isinstance(row, dict)
    }
    emitted_groups = {slot_group[slot_id] for slot_id in emitted_slots if slot_id in slot_group}
    missing = required_group_ids - emitted_groups
    if missing:
        raise AssertionError(f"current87 response omitted required assigned groups: {sorted(missing)}")
    return emitted_groups


def _current80_slot_from_persisted_scope() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    scope_path = (
        root
        / "output"
        / "playwright"
        / "run_first_gui"
        / "cancel-current80-20261005"
        / "output"
        / "run-first-cancel-current80__job_fb4625cad2cd"
        / "artifacts"
        / "outline_v3"
        / "candidate_output_scope_ec5b84b06b8e041a755b0c7f461885133da18b7557b14d005398bb8625c64a54__6af4b9ff2b806172aaafeb83.json"
    )
    if not scope_path.is_file():
        return {**CURRENT80_SLOT, "claim_group_index": 0}

    scope_bytes = scope_path.read_bytes()
    assert hashlib.sha256(scope_bytes).hexdigest() == CURRENT80_PROVENANCE[
        "candidate_output_scope_file_sha256"
    ]
    scope = json.loads(scope_bytes)
    assert scope.get("content_hash") == CURRENT80_SCOPE_HASH
    assert scope.get("source_inventory_binding", {}).get("registry_job_id") == CURRENT80_JOB_ID
    slots = [
        row for row in scope.get("claim_slots", [])
        if isinstance(row, dict) and row.get("claim_slot_id") == CURRENT80_SLOT["claim_slot_id"]
    ]
    assert len(slots) == 1
    slot = slots[0]
    for key, value in CURRENT80_SLOT.items():
        assert slot.get(key) == value
    # The persisted scope stores durable group IDs; the request-local index is
    # assigned only when a packed group is sent to the provider.
    return {**slot, "claim_group_index": 0}


def _single_request(
    slot: dict[str, Any] | None = None,
    *,
    source_claims: list[dict[str, Any]] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
    source_fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request_slot = deepcopy(slot or _current80_slot_from_persisted_scope())
    group_id = str(request_slot["claim_group_id"])
    request_slot["claim_group_index"] = 0
    request_claims = source_claims if source_claims is not None else [
        deepcopy(CURRENT80_REPLAY["primary_source_claim"])
    ]
    return {
        "candidate_id": "candidate_1",
        "organizing_logic": "source evidence",
        "paper_keys": ["studya_unknown_author", "studyb_unknown_author", "studyc_unknown_author"],
        "evidence": [
            {"paper_key": "studya_unknown_author", "title": "Study A"},
            {"paper_key": "studyb_unknown_author", "title": "Study B"},
            {"paper_key": "studyc_unknown_author", "title": "Study C"},
        ],
        "candidate_output_scope": {
            "claim_group_ids": [group_id],
            "claim_slots": [request_slot],
            "organization_tasks": [{
                "claim_group_id": group_id,
                "task_id": request_slot["task_id"],
                "paper_keys": [request_slot["paper_key"]],
            }],
        },
        "hierarchy": {"claim_group_ids": [group_id]},
        "shared_semantic_context": {
            "interpretation_source_tables": {
                "source_claims": request_claims,
                "source_fields": source_fields or [],
                "dependencies": dependencies or [],
            }
        },
    }


def _replay_current80_scope_audit_if_present() -> None:
    root = Path(__file__).resolve().parents[1]
    job_root = (
        root
        / "output"
        / "playwright"
        / "run_first_gui"
        / "cancel-current80-20261005"
        / "output"
        / "run-first-cancel-current80__job_fb4625cad2cd"
    )
    if not job_root.is_dir():
        return
    audit_paths = sorted((job_root / "artifacts").glob("R1_REQUEST_PAYLOAD_AUDIT__*.jsonl"))
    if not audit_paths:
        return
    records = []
    for path in audit_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("payload_hash") == CURRENT80_FAILED_PAYLOAD_HASH:
                records.append(record)
    assert records
    for failed in records:
        assert failed["node_id"] == CURRENT80_FAILED_NODE_ID
        assert failed["status"] == "failed"
        assert failed["serialized_bytes"] == CURRENT80_PROVENANCE["failed_request_serialized_bytes"]
        assert CURRENT80_SCOPE_HASH in failed["input_artifact_hashes"]

    study_c_pdf = (
        root
        / "output"
        / "playwright"
        / "run_first_gui"
        / "cancel-current80-20261005"
        / "papers"
        / "study-c.pdf"
    )
    if study_c_pdf.is_file():
        observed_hash = hashlib.sha256(study_c_pdf.read_bytes()).hexdigest()
        assert observed_hash == CURRENT80_PROVENANCE["study_c_synthetic_pdf_sha256"]


def _run(request: dict[str, Any]) -> dict[str, Any]:
    return _grounded_fixture_candidate(CURRENT80_FAILED_NODE_ID, request)


def test_current80_persisted_single_paper_slot_replays_without_fabricated_dependency() -> None:
    _replay_current80_scope_audit_if_present()
    request = _single_request()

    result = _run(request)

    section = result["sections"][0]
    support = section["claim_support"][0]
    slot = request["candidate_output_scope"]["claim_slots"][0]
    assert section["paper_keys"] == [slot["paper_key"]]
    assert section["claims"] == [PERSISTED_SOURCE_CLAIM_TEXT]
    assert support["primary_claim_id"] == slot["primary_claim_id"]
    assert support["source_claim_ids"] == [slot["primary_claim_id"]]
    assert support["evidence_ids"] == slot["evidence_ids"]
    assert support["primary_evidence_ids"] == slot["evidence_ids"]
    assert support["qualifier_source_claim_ids"] == []
    assert support["qualifier_evidence_ids"] == []
    assert support["qualifier_source_field_ids"] == []


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_primary_claim",
        "foreign_primary_claim",
        "missing_primary_evidence",
        "missing_slot_evidence",
        "foreign_slot_evidence",
        "unbound_extra_claim",
        "unbound_source_field",
        "foreign_primary_dependency",
    ],
)
def test_primary_only_single_slot_rejects_unclosed_or_foreign_source(
    corruption: str,
) -> None:
    request = _single_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    slot = request["candidate_output_scope"]["claim_slots"][0]
    primary_id = slot["primary_claim_id"]
    evidence_id = slot["evidence_ids"][0]
    source_claim = tables["source_claims"][0]

    if corruption == "missing_primary_claim":
        tables["source_claims"] = []
    elif corruption == "foreign_primary_claim":
        source_claim["paper_key"] = "studyb_unknown_author"
    elif corruption == "missing_primary_evidence":
        source_claim["evidence_ids"] = []
    elif corruption == "missing_slot_evidence":
        slot["evidence_ids"] = []
    elif corruption == "foreign_slot_evidence":
        slot["evidence_ids"] = ["evidence:studyb_unknown_author:foreign"]
    elif corruption == "unbound_extra_claim":
        extra_id = "claim:studyc_unknown_author:unbound-qualifier"
        slot["source_claim_ids"].append(extra_id)
        tables["source_claims"].append({
            "claim_id": extra_id,
            "paper_key": slot["paper_key"],
            "text": "A second source statement without a declared dependency.",
            "evidence_ids": [evidence_id],
        })
    elif corruption == "unbound_source_field":
        field_id = "source-field:studyc_unknown_author:unbound"
        slot["source_field_ids"] = [field_id]
        tables["source_fields"] = [{
            "source_field_id": field_id,
            "paper_key": slot["paper_key"],
            "source_value": "An undeclared boundary.",
            "evidence_ids": [evidence_id],
        }]
    elif corruption == "foreign_primary_dependency":
        tables["dependencies"] = [{
            "paper_key": "studyb_unknown_author",
            "primary_claim_id": primary_id,
            "primary_evidence_ids": [evidence_id],
            "required_source_claim_ids": [],
            "required_evidence_ids": [],
            "required_source_field_ids": [],
        }]

    with pytest.raises(AssertionError):
        _run(request)


def _single_request_with_qualifier() -> dict[str, Any]:
    request = _single_request()
    slot = request["candidate_output_scope"]["claim_slots"][0]
    primary_id = slot["primary_claim_id"]
    primary_evidence_id = slot["evidence_ids"][0]
    qualifier_id = "claim:studyc_unknown_author:sample-boundary"
    qualifier_evidence_id = "evidence:studyc_unknown_author:sample-boundary"
    field_id = "source-field:studyc_unknown_author:sample-boundary"
    slot["source_claim_ids"] = [primary_id, qualifier_id]
    slot["evidence_ids"] = [primary_evidence_id, qualifier_evidence_id]
    slot["source_field_ids"] = [field_id]
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    tables["source_claims"].append({
        "claim_id": qualifier_id,
        "paper_key": slot["paper_key"],
        "text": "The result is limited to the recorded sample.",
        "evidence_ids": [qualifier_evidence_id],
    })
    tables["dependencies"] = [{
        "paper_key": slot["paper_key"],
        "primary_claim_id": primary_id,
        "primary_evidence_ids": [primary_evidence_id],
        "required_source_claim_ids": [qualifier_id],
        "required_evidence_ids": [qualifier_evidence_id],
        "required_source_field_ids": [field_id],
    }]
    tables["source_fields"] = [{
        "source_field_id": field_id,
        "paper_key": slot["paper_key"],
        "source_value": "Only in the recorded sample.",
        "evidence_ids": [qualifier_evidence_id],
    }]
    return request


def test_single_paper_declared_qualifier_requires_complete_same_source_closure() -> None:
    request = _single_request_with_qualifier()

    result = _run(request)

    support = result["sections"][0]["claim_support"][0]
    assert support["qualifier_source_claim_ids"] == [
        "claim:studyc_unknown_author:sample-boundary"
    ]
    assert support["qualifier_evidence_ids"] == [
        "evidence:studyc_unknown_author:sample-boundary"
    ]
    assert support["qualifier_source_field_ids"] == [
        "source-field:studyc_unknown_author:sample-boundary"
    ]


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_qualifier_claim",
        "foreign_qualifier_claim",
        "missing_qualifier_evidence",
        "foreign_qualifier_field",
    ],
)
def test_single_paper_declared_qualifier_rejects_incomplete_or_foreign_closure(
    corruption: str,
) -> None:
    request = _single_request_with_qualifier()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    slot = request["candidate_output_scope"]["claim_slots"][0]
    qualifier_id = "claim:studyc_unknown_author:sample-boundary"
    qualifier_evidence_id = "evidence:studyc_unknown_author:sample-boundary"
    if corruption == "missing_qualifier_claim":
        slot["source_claim_ids"].remove(qualifier_id)
    elif corruption == "foreign_qualifier_claim":
        next(row for row in tables["source_claims"] if row["claim_id"] == qualifier_id)[
            "paper_key"
        ] = "studyb_unknown_author"
    elif corruption == "missing_qualifier_evidence":
        slot["evidence_ids"].remove(qualifier_evidence_id)
    elif corruption == "foreign_qualifier_field":
        tables["source_fields"][0]["paper_key"] = "studyb_unknown_author"

    with pytest.raises(AssertionError):
        _run(request)


def _ab_comparison_request(*, sample_boundary: str = "Controlled sample of 24 participants.") -> dict[str, Any]:
    group_id = "candidate-claim-group:required-ab"
    task_id = "relation_comparison:required-ab"
    slots = []
    claims = []
    dependencies = []
    fields = []
    evidence = []
    support_slot_ids = []
    for paper, label, finding in (
        ("studya_unknown_author", "Study A", "four points"),
        ("studyb_unknown_author", "Study B", "two points"),
    ):
        claim_id = f"claim:{paper}:finding"
        evidence_id = f"evidence:{paper}:finding"
        field_id = f"source-field:{paper}:sample"
        slot_id = f"candidate-claim-slot:{paper}:finding"
        support_slot_ids.append(slot_id)
        slots.append({
            "claim_group_index": 0,
            "claim_group_id": group_id,
            "claim_slot_id": slot_id,
            "task_id": task_id,
            "paper_key": paper,
            "primary_claim_id": claim_id,
            "source_claim_ids": [claim_id],
            "evidence_ids": [evidence_id],
            "source_field_ids": [field_id],
            "study_id": "",
            "relation_ids": ["relation:required-ab"],
        })
        claims.append({
            "claim_id": claim_id,
            "paper_key": paper,
            "text": f"{label} reported an improvement of {finding}.",
            "evidence_ids": [evidence_id],
        })
        dependencies.append({
            "paper_key": paper,
            "primary_claim_id": claim_id,
            "primary_evidence_ids": [evidence_id],
            "required_source_claim_ids": [],
            "required_evidence_ids": [],
            "required_source_field_ids": [field_id],
        })
        fields.append({
            "source_field_id": field_id,
            "paper_key": paper,
            "source_value": sample_boundary,
            "evidence_ids": [evidence_id],
        })
        evidence.append({"paper_key": paper, "title": label})

    return {
        "candidate_id": "candidate_1",
        "organizing_logic": "source evidence",
        "evidence": evidence,
        "candidate_output_scope": {
            "claim_group_ids": [group_id],
            "claim_slots": slots,
            "organization_tasks": [{
                "claim_group_id": group_id,
                "task_id": task_id,
                "paper_keys": ["studya_unknown_author", "studyb_unknown_author"],
                "support_slot_ids": support_slot_ids,
                "claim_seed": True,
                "claim_kind": "model_authored_relation_comparison_slot",
                "task_contract": {
                    "schema_version": "relation-comparison-task/v1",
                    "task_id": task_id,
                    "comparison_question": "Compare the two observed outcomes.",
                    "relation_id_is_not_a_conclusion": True,
                },
            }],
        },
        "hierarchy": {"claim_group_ids": [group_id]},
        "shared_semantic_context": {
            "interpretation_source_tables": {
                "source_claims": claims,
                "source_fields": fields,
                "dependencies": dependencies,
            }
        },
    }


def test_required_ab_comparison_keeps_dependency_and_n24_boundary() -> None:
    result = _run(_ab_comparison_request())

    section = result["sections"][0]
    assert section["paper_keys"] == ["studya_unknown_author", "studyb_unknown_author"]
    assert section["claims"] == [
        "In the observed controlled samples of 24 participants, Study B reported a two-point "
        "improvement whereas Study A reported a four-point improvement"
    ]
    assert {row["paper_key"] for row in section["claim_support"]} == {
        "studya_unknown_author",
        "studyb_unknown_author",
    }


def test_required_ab_comparison_rejects_missing_dependency_or_n24_boundary() -> None:
    no_dependency = _ab_comparison_request()
    no_dependency["shared_semantic_context"]["interpretation_source_tables"]["dependencies"] = []
    with pytest.raises(AssertionError):
        _run(no_dependency)

    without_n24 = _ab_comparison_request(sample_boundary="Controlled sample.")
    with pytest.raises(AssertionError, match="n=24 boundary"):
        _run(without_n24)


def _current82_full_scope_request() -> dict[str, Any]:
    return deepcopy(CURRENT82_REPLAY["request"])


def _current82_target_relation_request() -> tuple[dict[str, Any], dict[str, Any]]:
    request = _current82_full_scope_request()
    scope = request["candidate_output_scope"]
    target_id = CURRENT82_PROVENANCE["failed_slot_id"]
    target_slot = next(row for row in scope["claim_slots"] if row["claim_slot_id"] == target_id)
    group_index = int(target_slot["claim_group_index"])
    group_id = scope["claim_group_ids"][group_index]
    group_row = next(row for row in scope["organization_tasks"] if row["claim_group_id"] == group_id)
    selected_slots = [
        row for row in scope["claim_slots"]
        if int(row["claim_group_index"]) == group_index
    ]
    for row in selected_slots:
        row["claim_group_index"] = 0
    group_row["claim_group_index"] = 0
    paper_keys = sorted({row["paper_key"] for row in selected_slots})
    scope["claim_group_ids"] = [group_id]
    scope["task_ids"] = [group_row["task_id"]]
    scope["claim_slots"] = selected_slots
    scope["organization_tasks"] = [group_row]
    scope["required_claim_group_indices"] = [0]
    request["paper_keys"] = paper_keys
    request["evidence"] = [
        row for row in request["evidence"] if row["paper_key"] in set(paper_keys)
    ]
    request["hierarchy"]["claim_group_ids"] = [group_id]
    request["hierarchy"]["paper_keys"] = paper_keys
    request["content_layer_refs"]["dossier_ids"] = [f"dossier:{paper}" for paper in paper_keys]

    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    claim_ids = {value for row in selected_slots for value in row["source_claim_ids"]}
    primary_ids = {row["primary_claim_id"] for row in selected_slots}
    field_ids = {value for row in selected_slots for value in row["source_field_ids"]}
    evidence_ids = {value for row in selected_slots for value in row["evidence_ids"]}
    tables["source_claims"] = [
        row for row in tables["source_claims"]
        if row["paper_key"] in set(paper_keys) and row["claim_id"] in claim_ids
    ]
    tables["source_fields"] = [
        row for row in tables["source_fields"]
        if row["paper_key"] in set(paper_keys) and row["source_field_id"] in field_ids
    ]
    tables["dependencies"] = [
        row for row in tables["dependencies"]
        if row["paper_key"] in set(paper_keys) and row["primary_claim_id"] in primary_ids
    ]
    tables["evidence_text_by_id"] = {
        evidence_id: text for evidence_id, text in tables["evidence_text_by_id"].items()
        if evidence_id in evidence_ids
    }
    return request, next(row for row in selected_slots if row["claim_slot_id"] == target_id)


def _verify_current82_persisted_inputs_if_present() -> None:
    root = Path(__file__).resolve().parents[1]
    session_root = root / "output" / "playwright" / "run_first_gui" / "cancel-current82-20261005"
    job_root = (
        session_root
        / "output"
        / "run-first-cancel-current82__job_fe6ee69f00b4"
    )
    if not job_root.is_dir():
        return
    scope_paths = list((job_root / "artifacts" / "outline_v3").glob("candidate_output_scope_*.json"))
    layer_paths = list((job_root / "artifacts" / "outline_v3" / "artifacts").glob("outline_content_layers__*.json"))
    if not scope_paths or not layer_paths:
        return
    assert len(scope_paths) == 1 and len(layer_paths) == 1
    scope_bytes = scope_paths[0].read_bytes()
    layer_bytes = layer_paths[0].read_bytes()
    assert hashlib.sha256(scope_bytes).hexdigest() == CURRENT82_PROVENANCE[
        "candidate_output_scope_file_sha256"
    ]
    assert hashlib.sha256(layer_bytes).hexdigest() == CURRENT82_PROVENANCE[
        "outline_content_layers_file_sha256"
    ]
    scope = json.loads(scope_bytes)
    layers = json.loads(layer_bytes)
    assert scope["content_hash"] == CURRENT82_PROVENANCE["candidate_output_scope_content_hash"]
    assert layers["payload"]["content_hash"] == CURRENT82_PROVENANCE[
        "outline_content_layers_content_hash"
    ]

    audit_paths = list((job_root / "artifacts").glob("R1_REQUEST_PAYLOAD_AUDIT__*.jsonl"))
    records = [
        json.loads(line)
        for path in audit_paths
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [row for row in records if row.get("payload_hash") == CURRENT82_PROVENANCE["failed_payload_sha256"]]
    assert failed
    assert all(
        row.get("node_id") == CURRENT82_PROVENANCE["failed_node_id"]
        and row.get("status") == "failed"
        and row.get("serialized_bytes") == CURRENT82_PROVENANCE["failed_request_serialized_bytes"]
        for row in failed
    )
    pdf_root = session_root / "papers"
    pdf_paths = {
        "studya_unknown_author": pdf_root / "study-a.pdf",
        "studyb_unknown_author": pdf_root / "study-b.pdf",
        "studyc_unknown_author": pdf_root / "study-c.pdf",
        "studyd_unknown_author": pdf_root / "study-d.pdf",
    }
    if all(path.is_file() for path in pdf_paths.values()):
        actual_fingerprints = {}
        for paper_key, path in pdf_paths.items():
            document = pymupdf.open(path)
            try:
                text = " ".join(
                    unicodedata.normalize("NFKC", page.get_text())
                    for page in document
                )
            finally:
                document.close()
            normalized = " ".join(text.split())
            actual_fingerprints[paper_key] = hashlib.sha256(
                normalized.encode("utf-8")
            ).hexdigest()
        assert actual_fingerprints == CURRENT82_PROVENANCE["source_pdf_text_fingerprint"][
            "by_paper_key"
        ]


def test_current82_full_persisted_scope_replays_with_saved_source_projection() -> None:
    _verify_current82_persisted_inputs_if_present()
    request = _current82_full_scope_request()
    scope = request["candidate_output_scope"]
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    assert len(scope["claim_group_ids"]) == CURRENT82_PROVENANCE["group_count"]
    assert len(scope["claim_slots"]) == CURRENT82_PROVENANCE["slot_count"]
    assert len(tables["source_claims"]) == CURRENT82_PROVENANCE["source_claim_count"]
    assert len(tables["source_fields"]) == CURRENT82_PROVENANCE["source_field_count"]
    assert len(tables["dependencies"]) == CURRENT82_PROVENANCE["dependency_count"]

    result = _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)

    support = next(
        row for section in result["sections"] for row in section.get("claim_support", [])
        if row.get("claim_slot_id") == CURRENT82_PROVENANCE["failed_slot_id"]
    )
    slot = next(
        row for row in scope["claim_slots"]
        if row["claim_slot_id"] == CURRENT82_PROVENANCE["failed_slot_id"]
    )
    dependencies = [
        row for row in tables["dependencies"]
        if row["paper_key"] == slot["paper_key"]
        and row["primary_claim_id"] == slot["primary_claim_id"]
    ]
    required_fields = {
        value for row in dependencies for value in row.get("required_source_field_ids") or ()
    }
    direct_fields = set(slot["source_field_ids"]) - required_fields
    assert len(slot["source_field_ids"]) == 8
    assert len(required_fields) == 3
    assert len(direct_fields) == 5
    assert support["source_field_ids"] == sorted(slot["source_field_ids"])
    assert support["qualifier_source_field_ids"] == sorted(required_fields)
    fields = {row["source_field_id"]: row for row in tables["source_fields"]}
    evidence_text = tables["evidence_text_by_id"]
    for field_id in slot["source_field_ids"]:
        field = fields[field_id]
        values = {
            str(field.get("source_value") or "").strip().casefold(),
            str(field.get("derived_value") or "").strip().casefold(),
        } - {""}
        assert field["paper_key"] == slot["paper_key"]
        assert any(
            str(evidence_text.get(evidence_id) or "").strip().casefold() in values
            for evidence_id in slot["evidence_ids"]
        )


def test_current82_live_fixture_fallback_uses_snapshot_evidence_and_rejects_changed_rows() -> None:
    request = _current82_full_scope_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    del tables["evidence_text_by_id"]

    result = _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)

    assert any(
        row.get("claim_slot_id") == CURRENT82_PROVENANCE["failed_slot_id"]
        for section in result["sections"] for row in section.get("claim_support", [])
    )

    unknown = _current82_full_scope_request()
    del unknown["shared_semantic_context"]["interpretation_source_tables"]["evidence_text_by_id"]
    unknown["shared_semantic_context"]["interpretation_source_tables"]["source_claims"][0][
        "text"
    ] += " forged"
    with pytest.raises(AssertionError, match="four-paper source projection differs"):
        _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], unknown)


def _rebind_current82_request_to_fresh_job() -> dict[str, Any]:
    request = _current82_full_scope_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    del tables["evidence_text_by_id"]
    scope = request["candidate_output_scope"]
    old_groups = list(scope["claim_group_ids"])
    group_ids = {
        old: f"candidate-claim-group:fresh:{index:02d}"
        for index, old in enumerate(old_groups)
    }
    old_slots = [row["claim_slot_id"] for row in scope["claim_slots"]]
    slot_ids = {
        old: f"candidate-claim-slot:fresh:{index:03d}"
        for index, old in enumerate(old_slots)
    }
    old_fields = [row["source_field_id"] for row in tables["source_fields"]]
    field_ids = {
        old: f"source-field:fresh:{index:03d}"
        for index, old in enumerate(old_fields)
    }

    scope["claim_group_ids"] = [group_ids[value] for value in old_groups]
    for task in scope["organization_tasks"]:
        task["claim_group_id"] = group_ids[task["claim_group_id"]]
        task["support_slot_ids"] = [slot_ids[value] for value in task["support_slot_ids"]]
    for slot in scope["claim_slots"]:
        slot["claim_slot_id"] = slot_ids[slot["claim_slot_id"]]
        slot["source_field_ids"] = [field_ids[value] for value in slot["source_field_ids"]]
    request["hierarchy"]["claim_group_ids"] = list(scope["claim_group_ids"])
    scope["content_hash"] = "a" * 64
    scope["parent_content_hash"] = "b" * 64
    request["content_layer_refs"]["artifact_hash"] = "c" * 64
    for field in tables["source_fields"]:
        field["source_field_id"] = field_ids[field["source_field_id"]]
    for dependency in tables["dependencies"]:
        dependency["required_source_field_ids"] = [
            field_ids[value] for value in dependency["required_source_field_ids"]
        ]
    return request


def test_current82_snapshot_fallback_accepts_same_source_under_fresh_job_ids() -> None:
    request = _rebind_current82_request_to_fresh_job()

    result = _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)

    persisted_target = next(
        row for row in CURRENT82_REPLAY["request"]["candidate_output_scope"]["claim_slots"]
        if row["claim_slot_id"] == CURRENT82_PROVENANCE["failed_slot_id"]
    )
    relation_ids = set(persisted_target["relation_ids"])
    comparison = next(
        section for section in result["sections"]
        if relation_ids.intersection(section["relation_ids"])
    )
    target_support = next(
        row for row in comparison["claim_support"]
        if row["paper_key"] == persisted_target["paper_key"]
        and row["primary_claim_id"] == persisted_target["primary_claim_id"]
    )
    assert len(target_support["source_field_ids"]) == 8
    assert len(target_support["qualifier_source_field_ids"]) == 3


@pytest.mark.parametrize("corruption", ["source_field", "dependency"])
def test_current82_snapshot_fallback_rejects_unknown_source_rows(corruption: str) -> None:
    request = _current82_full_scope_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    del tables["evidence_text_by_id"]
    if corruption == "source_field":
        tables["source_fields"][0]["source_value"] += " unverified"
    else:
        tables["dependencies"][0]["required_source_claim_ids"].append(
            "claim:studyb_unknown_author:foreign-qualifier"
        )

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)


def test_current82_direct_field_requires_exact_snapshot_evidence_id_lookup() -> None:
    request, slot = _current82_target_relation_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    source_fields = {row["source_field_id"]: row for row in tables["source_fields"]}
    dependencies = [
        row for row in tables["dependencies"]
        if row["paper_key"] == slot["paper_key"]
        and row["primary_claim_id"] == slot["primary_claim_id"]
    ]
    required_fields = {
        value for row in dependencies for value in row.get("required_source_field_ids") or ()
    }
    direct_field_id = next(value for value in slot["source_field_ids"] if value not in required_fields)
    field_value = str(source_fields[direct_field_id].get("source_value") or "").strip().casefold()
    text_by_id = tables["evidence_text_by_id"]
    matched_ids = [
        evidence_id for evidence_id in slot["evidence_ids"]
        if str(text_by_id.get(evidence_id) or "").strip().casefold() == field_value
    ]
    assert matched_ids
    for evidence_id in matched_ids:
        del text_by_id[evidence_id]

    with pytest.raises(AssertionError, match="four-paper evidence text map differs"):
        _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)



@pytest.mark.parametrize(
    "corruption",
    [
        "mismatched_text",
        "foreign_paper",
        "foreign_study",
        "outside_closure_evidence",
        "unbound_extra_field",
        "missing_required_field",
        "mismatched_required_field_text",
    ],
)
def test_current82_direct_and_required_fields_reject_incomplete_or_unbound_scope(
    corruption: str,
) -> None:
    request, slot = _current82_target_relation_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    dependencies = [
        row for row in tables["dependencies"]
        if row["paper_key"] == slot["paper_key"]
        and row["primary_claim_id"] == slot["primary_claim_id"]
    ]
    required_fields = {
        value for row in dependencies for value in row.get("required_source_field_ids") or ()
    }
    direct_fields = sorted(set(slot["source_field_ids"]) - required_fields)
    fields = {row["source_field_id"]: row for row in tables["source_fields"]}
    field_id = direct_fields[0]
    field = fields[field_id]

    if corruption == "mismatched_text":
        field["source_value"] = "A text value outside the assigned evidence closure."
        field["derived_value"] = ""
    elif corruption == "foreign_paper":
        field["paper_key"] = "studyb_unknown_author"
    elif corruption == "foreign_study":
        field["scope"] = "explicit_study"
        field["study_id"] = "study-not-in-slot"
    elif corruption == "outside_closure_evidence":
        full_projection = CURRENT82_REPLAY["request"]["shared_semantic_context"][
            "interpretation_source_tables"
        ]["evidence_text_by_id"]
        outside_ids = set(full_projection) - set(slot["evidence_ids"])
        outside_text = next(
            str(full_projection[evidence_id]) for evidence_id in sorted(outside_ids)
            if str(evidence_id).startswith(f"evidence:{slot['paper_key']}:")
        )
        field["source_value"] = outside_text
        field["derived_value"] = ""
    elif corruption == "unbound_extra_field":
        extra_id = "source-field:studya_unknown_author:unbound-direct"
        slot["source_field_ids"].append(extra_id)
        tables["source_fields"].append({
            "source_field_id": extra_id,
            "paper_key": slot["paper_key"],
            "source_value": "No assigned evidence contains this field value.",
            "derived_value": "",
            "scope": "paper",
            "study_id": "",
        })
    elif corruption == "missing_required_field":
        slot["source_field_ids"].remove(sorted(required_fields)[0])
    elif corruption == "mismatched_required_field_text":
        required_id = sorted(required_fields)[0]
        fields[required_id]["source_value"] = "A fabricated qualifier value."
        fields[required_id]["derived_value"] = ""

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(CURRENT82_PROVENANCE["failed_node_id"], request)


def _current84_full_context_request() -> dict[str, Any]:
    return deepcopy(CURRENT84_REPLAY["request"])


def _verify_current84_persisted_inputs_if_present() -> None:
    root = Path(__file__).resolve().parents[1]
    session_root = root / "output" / "playwright" / "run_first_gui" / "cancel-current84-20261005"
    job_root = session_root / "output" / "run-first-cancel-current84__job_deca22a5531b"
    if not job_root.is_dir():
        return
    scope_paths = list((job_root / "artifacts" / "outline_v3").glob("candidate_output_scope_*.json"))
    layer_paths = list((job_root / "artifacts" / "outline_v3" / "artifacts").glob("outline_content_layers__*.json"))
    if not scope_paths or not layer_paths:
        return
    assert len(scope_paths) == 1 and len(layer_paths) == 1
    scope_bytes = scope_paths[0].read_bytes()
    layer_bytes = layer_paths[0].read_bytes()
    assert hashlib.sha256(scope_bytes).hexdigest() == CURRENT84_PROVENANCE[
        "candidate_output_scope_file_sha256"
    ]
    assert hashlib.sha256(layer_bytes).hexdigest() == CURRENT84_PROVENANCE[
        "outline_content_layers_file_sha256"
    ]
    scope = json.loads(scope_bytes)
    layers = json.loads(layer_bytes)
    assert scope["content_hash"] == CURRENT84_PROVENANCE["candidate_output_scope_content_hash"]
    assert len(layers["payload"]["dossiers"][0]["source_field_ledger"]) > 0

    audit_paths = list((job_root / "artifacts").glob("R1_REQUEST_PAYLOAD_AUDIT__*.jsonl"))
    records = [
        json.loads(line)
        for path in audit_paths
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [row for row in records if row.get("payload_hash") == CURRENT84_PROVENANCE["failed_payload_sha256"]]
    assert failed and all(row.get("status") == "failed" for row in failed)

    pdf_root = session_root / "papers"
    pdf_paths = {
        "studya_unknown_author": pdf_root / "study-a.pdf",
        "studyb_unknown_author": pdf_root / "study-b.pdf",
        "studyc_unknown_author": pdf_root / "study-c.pdf",
        "studyd_unknown_author": pdf_root / "study-d.pdf",
    }
    if all(path.is_file() for path in pdf_paths.values()):
        fingerprints = {}
        for paper_key, path in pdf_paths.items():
            document = pymupdf.open(path)
            try:
                text = " ".join(
                    unicodedata.normalize("NFKC", page.get_text())
                    for page in document
                )
            finally:
                document.close()
            fingerprints[paper_key] = hashlib.sha256(
                " ".join(text.split()).encode("utf-8")
            ).hexdigest()
        assert fingerprints == CURRENT84_PROVENANCE["source_pdf_text_fingerprint"]["by_paper_key"]


def test_current84_full_236_field_context_replays_and_ignores_unused_changes() -> None:
    _verify_current84_persisted_inputs_if_present()
    request = _current84_full_context_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    scope = request["candidate_output_scope"]
    used_field_ids = {
        str(field_id) for slot in scope["claim_slots"] for field_id in slot["source_field_ids"]
    }
    assert len(tables["source_fields"]) == CURRENT84_PROVENANCE["source_field_count"]
    assert len(used_field_ids) == CURRENT84_PROVENANCE["reachable_field_count"]
    unused = next(row for row in tables["source_fields"] if row["source_field_id"] not in used_field_ids)
    unused["source_value"] += " changed outside selected slot closure"

    result = _grounded_fixture_candidate(CURRENT84_PROVENANCE["failed_node_id"], request)

    section = next(
        row for row in result["sections"]
        if "relation-7d141f1dc6d74d97bddaf272" in row["relation_ids"]
    )
    target_support = next(
        row for row in section["claim_support"]
        if row["paper_key"] == "studya_unknown_author"
        and row["primary_claim_id"] == "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e"
    )
    assert CURRENT84_PROVENANCE["failed_direct_field_id"] in target_support["source_field_ids"]


def _current84_asdict_writer_claim_shape() -> dict[str, Any]:
    request = _current84_full_context_request()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    for claim in tables["source_claims"]:
        claim.pop("study_id", None)
        claim["owner_study_ids"] = [""]
    for field in tables["source_fields"]:
        source_study_id = str(field.pop("study_id", "") or "")
        field["source_study_id"] = source_study_id
        field["owner_study_ids"] = [source_study_id] if source_study_id else [""]
    tables.pop("evidence_text_by_id", None)
    return request


def test_current84_asdict_writer_claim_shape_matches_empty_owner_sentinel() -> None:
    request = _current84_asdict_writer_claim_shape()

    result = _grounded_fixture_candidate(CURRENT84_PROVENANCE["failed_node_id"], request)

    section = next(
        row for row in result["sections"]
        if "relation-7d141f1dc6d74d97bddaf272" in row["relation_ids"]
    )
    target_support = next(
        row for row in section["claim_support"]
        if row["paper_key"] == "studya_unknown_author"
        and row["primary_claim_id"] == "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e"
    )
    assert CURRENT84_PROVENANCE["failed_direct_field_id"] in target_support["source_field_ids"]


def test_current84_asdict_writer_claim_rejects_nonempty_unverified_owner() -> None:
    request = _current84_asdict_writer_claim_shape()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    target = next(
        row for row in tables["source_claims"]
        if row["claim_id"] == "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e"
    )
    target["owner_study_ids"] = ["unverified-study-owner"]

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(CURRENT84_PROVENANCE["failed_node_id"], request)


@pytest.mark.parametrize("invalid_owner_shape", ["", 0, False, None, {"study": ""}])
def test_current84_snapshot_rejects_explicit_nonsequence_owner_shape(
    invalid_owner_shape: Any,
) -> None:
    request = _current84_asdict_writer_claim_shape()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    target = next(
        row for row in tables["source_claims"]
        if row["claim_id"] == "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e"
    )
    target["owner_study_ids"] = invalid_owner_shape

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(CURRENT84_PROVENANCE["failed_node_id"], request)


@pytest.mark.parametrize(
    "corruption", ["foreign_used_field", "changed_used_field", "foreign_study_owner"]
)
def test_current84_full_context_rejects_changed_reachable_field(corruption: str) -> None:
    request = _current84_full_context_request()
    field_id = CURRENT84_PROVENANCE["failed_direct_field_id"]
    field = next(
        row for row in request["shared_semantic_context"]["interpretation_source_tables"]["source_fields"]
        if row["source_field_id"] == field_id
    )
    if corruption == "foreign_used_field":
        field["paper_key"] = "studyb_unknown_author"
    elif corruption == "foreign_study_owner":
        field["owner_study_ids"] = ["unverified-study-owner"]
    else:
        field["source_value"] = "Unverified text for a reachable source field."

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(CURRENT84_PROVENANCE["failed_node_id"], request)


def _rewrite_wire_aliases(
    value: Any,
    *,
    paper_aliases: dict[str, str] | None = None,
    relation_aliases: dict[str, str] | None = None,
    key: str = "",
) -> Any:
    if isinstance(value, dict):
        return {
            str(child_key): _rewrite_wire_aliases(
                child,
                paper_aliases=paper_aliases,
                relation_aliases=relation_aliases,
                key=str(child_key),
            )
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        mapping = paper_aliases if key == "paper_keys" else relation_aliases if key == "relation_ids" else None
        if mapping is not None:
            return [mapping.get(str(item), item) for item in value]
        return [
            _rewrite_wire_aliases(
                child,
                paper_aliases=paper_aliases,
                relation_aliases=relation_aliases,
            )
            for child in value
        ]
    if isinstance(value, str) and key == "paper_key" and paper_aliases is not None:
        return paper_aliases.get(value, value)
    if isinstance(value, str) and key == "relation_id" and relation_aliases is not None:
        return relation_aliases.get(value, value)
    return value


def test_current86_exact_wire_alias_map_is_hash_bound_to_registry_artifact() -> None:
    alias_bytes = CURRENT86_ALIAS_MAP_PATH.read_bytes()
    assert hashlib.sha256(alias_bytes).hexdigest() == CURRENT86_ALIAS_MAP_SHA256
    alias_map = json.loads(alias_bytes)
    from outline.evidence_alias import build_alias_map

    assert alias_map == build_alias_map(list(alias_map["papers"]), list(alias_map["relations"]))
    assert alias_map["papers_reverse"] == {
        "P001": "studya_unknown_author",
        "P002": "studyb_unknown_author",
        "P003": "studyc_unknown_author",
        "P004": "studyd_unknown_author",
    }
    assert alias_map["relations_reverse"]["R008"] == "relation-7d141f1dc6d74d97bddaf272"


def test_current86_exact_wire_request_replays_without_changing_provider_ids() -> None:
    envelope, request = _current86_exact_wire_candidate()
    original_request = deepcopy(request)

    result = _grounded_fixture_candidate(str(envelope["node_id"]), request)

    assert request == original_request
    assert len(result["sections"]) == 3
    result_papers = {
        str(support["paper_key"])
        for section in result["sections"]
        for support in section["claim_support"]
    }
    assert result_papers == {"P001", "P002", "P003", "P004"}
    result_relations = {
        str(relation_id)
        for section in result["sections"]
        for relation_id in section["relation_ids"]
    }
    assert result_relations == {"R008"}


@pytest.mark.parametrize("corruption", ["used_field_value", "used_claim_text", "used_claim_evidence"])
def test_current86_exact_wire_rejects_tampered_reachable_source(
    corruption: str,
) -> None:
    envelope, request = _current86_exact_wire_candidate()
    tables = request["shared_semantic_context"]["interpretation_source_tables"]
    field_id = "source-field:af8402148102691779498fc421ae4372a97615e2dd3fa12828f2976c18218128"
    if corruption == "used_field_value":
        assert any(
            field_id in slot.get("source_field_ids", [])
            for slot in request["candidate_output_scope"]["claim_slots"]
        )
        field = next(row for row in tables["source_fields"] if row["source_field_id"] == field_id)
        field["source_value"] = "A tampered reachable source value."
    else:
        claim = next(
            row for row in tables["source_claims"]
            if row["claim_id"] == "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e"
        )
        assert any(
            claim["claim_id"] == slot.get("primary_claim_id")
            for slot in request["candidate_output_scope"]["claim_slots"]
        )
        if corruption == "used_claim_text":
            claim["text"] = "A tampered reachable source claim."
        else:
            claim["evidence_ids"] = ["evidence:unverified:claim-binding"]

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(str(envelope["node_id"]), request)


@pytest.mark.parametrize(
    ("corruption", "paper_aliases", "relation_aliases"),
    [
        ("swapped_paper_aliases", {"P001": "P002", "P002": "P001"}, None),
        ("foreign_paper_alias", {"P001": "P999"}, None),
        ("swapped_relation_alias", None, {"R008": "R009"}),
        ("foreign_relation_alias", None, {"R008": "R999"}),
    ],
)
def test_current86_exact_wire_rejects_swapped_or_foreign_aliases(
    corruption: str,
    paper_aliases: dict[str, str] | None,
    relation_aliases: dict[str, str] | None,
) -> None:
    envelope, request = _current86_exact_wire_candidate()
    corrupted = _rewrite_wire_aliases(
        request,
        paper_aliases=paper_aliases,
        relation_aliases=relation_aliases,
    )
    assert corruption

    with pytest.raises(AssertionError):
        _grounded_fixture_candidate(str(envelope["node_id"]), corrupted)


def test_current87_exact_output_validates_full_candidate_scope_and_required_findings() -> None:
    envelope, request = _current87_exact_wire_candidate()
    original_request = deepcopy(request)
    content = _grounded_fixture_candidate(str(envelope["node_id"]), request)
    alias_map = json.loads(CURRENT86_ALIAS_MAP_PATH.read_text(encoding="utf-8"))
    from outline.evidence_alias import canonicalize_structural

    canonical_content = canonicalize_structural(content, alias_map)
    full_scope = _current87_candidate_output_scope()
    request_scope = request["candidate_output_scope"]
    emitted_groups = _validate_current87_full_scope_output(
        full_scope,
        request_scope,
        canonical_content,
    )
    assigned_group_ids = [str(value) for value in request_scope["claim_group_ids"]]
    required_group_ids = {
        assigned_group_ids[index]
        for index in request_scope["required_claim_group_indices"]
    }
    assert emitted_groups == required_group_ids
    assert len(content["sections"]) == 3
    assert request == original_request

    expected_primary_claims = {
        "claim:studya_unknown_author:findings:9cf0c5c01ec2d03e",
        "claim:studyb_unknown_author:findings:f6586c2423a1dbe7",
        "claim:studyc_unknown_author:findings:20eb70c8f8b6fd23",
        "claim:studyd_unknown_author:findings:497ca5821dde33f8",
    }
    actual_primary_claims = {
        str(row["primary_claim_id"])
        for section in content["sections"]
        for row in section["claim_support"]
    }
    assert actual_primary_claims == expected_primary_claims
    support_claims = {
        str(row["primary_claim_id"]): str(row["claim"])
        for section in content["sections"]
        for row in section["claim_support"]
    }
    assert "one point" in support_claims[
        "claim:studyc_unknown_author:findings:20eb70c8f8b6fd23"
    ].casefold()
    assert "did not change" in support_claims[
        "claim:studyd_unknown_author:findings:497ca5821dde33f8"
    ].casefold()
    ab_section = next(
        section for section in content["sections"] if set(section["relation_ids"]) == {"R008"}
    )
    assert len(ab_section["claim_support"]) == 2
    ab_claim = ab_section["claims"][0].casefold()
    assert all(value in ab_claim for value in ("four-point", "two-point", "24 participants"))


@pytest.mark.parametrize("required_index", [3, 4, 8])
def test_current87_full_candidate_scope_rejects_missing_required_group(
    required_index: int,
) -> None:
    envelope, request = _current87_exact_wire_candidate()
    content = _grounded_fixture_candidate(str(envelope["node_id"]), request)
    required_group_id = request["candidate_output_scope"]["claim_group_ids"][required_index]
    scope = _current87_candidate_output_scope()
    required_slot_ids = {
        slot.claim_slot_id
        for slot in scope.claim_slots
        if slot.claim_group_id == required_group_id
    }
    assert required_slot_ids
    corrupted = deepcopy(content)
    corrupted["sections"] = [
        section for section in corrupted["sections"]
        if not any(
            str(row.get("claim_slot_id") or "") in required_slot_ids
            for row in section.get("claim_support") or ()
            if isinstance(row, dict)
        )
    ]
    alias_map = json.loads(CURRENT86_ALIAS_MAP_PATH.read_text(encoding="utf-8"))
    from outline.evidence_alias import canonicalize_structural

    canonical_content = canonicalize_structural(corrupted, alias_map)
    with pytest.raises((AssertionError, ValueError)):
        _validate_current87_full_scope_output(
            scope,
            request["candidate_output_scope"],
            canonical_content,
        )

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from outline.candidate_fragment_bank import build_candidate_fragment_bank_v1
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from tests.run_first_candidate_oracle_binding import (
    CandidateOracleBindingError,
    verify_single_source_candidate_bindings,
)
from tests.test_run_first_candidate_fragment_bank import (
    CANONICAL_TEXT,
    _build_bank,
    _sealed_scope,
)


def _source_bound_support(text: str, slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_slot_id": slot["claim_slot_id"],
        "task_id": slot["task_id"],
        "claim": text,
        "paper_key": slot["paper_key"],
        "primary_claim_id": slot["primary_claim_id"],
        "source_claim_ids": list(slot["source_claim_ids"]),
        "evidence_ids": list(slot["evidence_ids"]),
        "source_field_ids": list(slot["source_field_ids"]),
        "study_id": slot["study_id"],
    }


def _outline_envelope(
    *, artifact_type: str, job_id: str, dependency_hashes: dict[str, str], payload: dict[str, Any],
) -> dict[str, Any]:
    canonical = {
        "artifact_type": artifact_type,
        "artifact_version": "v3",
        "job_id": job_id,
        "dependency_hashes": dict(sorted(dependency_hashes.items())),
        "payload": payload,
        "blocking_diagnostics": [],
    }
    return {
        **canonical,
        "status": "ready",
        "content_hash": compute_v3_hash(canonical),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def _published_registry(
    tmp_path: Path,
    *,
    mutation: str | None = None,
    forged_source_ids: bool = False,
    claim_mode: str = "single_source",
):
    fixture = _sealed_scope(tmp_path)
    inventory, _routes, _bridges, _bundles, scope = fixture
    workspace = tmp_path / "candidate-fragment-bank__fragment-bank-job"
    registry = ArtifactRegistry(str(workspace / "artifact_registry.json"), "fragment-bank-job")
    inventory_record = registry.get(inventory.artifact_id)
    assert inventory_record is not None

    canonical, bank = _build_bank(fixture, comparison_rows=(), required_group_ids=())
    if forged_source_ids:
        target = next(
            row for row in bank["groups"]
            if row["materialization_state"] == "materialized"
            and row["text"] == CANONICAL_TEXT
            and {slot["paper_key"] for slot in row["support_slots"]} == {"paper-a"}
            and row["entry_kind"] == "canonical_claim"
        )
        group_id = target["claim_group"]["claim_group_id"]
        forged_claim = deepcopy(canonical[group_id])
        foreign_paper = inventory.paper_by_key()["paper-b"]
        foreign_claim_id = foreign_paper.claims[0].claim_id
        foreign_evidence_id = foreign_paper.evidence[0].evidence_id
        foreign_field_id = foreign_paper.source_fields[0].source_field_id
        forged_claim["source_claim_ids"] = sorted({*forged_claim["source_claim_ids"], foreign_claim_id})
        forged_claim["evidence_ids"] = sorted({*forged_claim["evidence_ids"], foreign_evidence_id})
        forged_claim["source_field_ids"] = sorted({*forged_claim["source_field_ids"], foreign_field_id})
        forged_hash = hash_json(forged_claim)
        forged_groups = tuple(
            replace(group, claim_hash=forged_hash) if group.claim_group_id == group_id else group
            for group in scope.claim_groups
        )
        forged_slots = tuple(
            replace(
                slot,
                source_claim_ids=tuple(forged_claim["source_claim_ids"]),
                evidence_ids=tuple(forged_claim["evidence_ids"]),
                source_field_ids=tuple(forged_claim["source_field_ids"]),
            ) if slot.claim_group_id == group_id else slot
            for slot in scope.claim_slots
        )
        scope = replace(scope, claim_groups=forged_groups, claim_slots=forged_slots)
        canonical[group_id] = forged_claim
        source_ref = {
            "artifact_id": inventory.artifact_id,
            "content_hash": inventory.artifact_hash,
            "inventory_content_hash": inventory.content_hash,
        }
        bank = build_candidate_fragment_bank_v1(scope, canonical, (), (), [source_ref])
    entries_by_paper = {
        next(iter({str(slot["paper_key"]) for slot in entry["support_slots"]})): entry
        for entry in bank["groups"]
        if entry["materialization_state"] == "materialized"
        and entry["text"] == CANONICAL_TEXT
        and len({str(slot["paper_key"]) for slot in entry["support_slots"]}) == 1
        and entry["entry_kind"] == "canonical_claim"
    }
    if claim_mode == "single_source":
        selected_entries = [entries_by_paper["paper-a"]]
    elif claim_mode == "shared_independent_sources":
        selected_entries = [entries_by_paper[paper_key] for paper_key in ("paper-a", "paper-b")]
    elif claim_mode == "multi_source_group":
        comparison_group_id = next(
            group.claim_group_id for group in scope.claim_groups
            if not group.claim_seed and group.task_id == "cross_group_comparison"
        )
        comparison_entry = next(
            row for row in bank["groups"]
            if row["claim_group"]["claim_group_id"] == comparison_group_id
        )
        selected_entries = [comparison_entry]
    else:
        raise AssertionError(f"unsupported fixture claim mode: {claim_mode}")
    entry = selected_entries[0]
    text = entry["text"]
    selected_papers = sorted({
        str(slot["paper_key"])
        for selected_entry in selected_entries
        for slot in selected_entry["support_slots"]
    })
    supports = [
        _source_bound_support(text, slot)
        for selected_entry in selected_entries
        for slot in selected_entry["support_slots"]
    ]
    section = {
        "section_id": "section:candidate-proof:0",
        "title": "Existing source claim",
        "goal": "Retain the source claim as stored.",
        "rationale": "The fragment is selected by its exact bank index.",
        "paper_keys": selected_papers,
        "paper_roles": {paper_key: text for paper_key in selected_papers},
        "task_ids": sorted({row["task_id"] for row in supports}),
        "relation_ids": sorted({
            value
            for selected_entry in selected_entries
            for value in selected_entry["claim_group"].get("relation_ids", [])
        }),
        "claims": [text],
        "claim_support": supports,
    }
    final_payload = {
        "title": "Registry-bound source fixture",
        "candidate_id": "candidate-proof",
        "sections": [section],
        "paper_keys": list(scope.paper_keys),
        "source_hashes": [inventory.artifact_hash],
    }
    if mutation == "missing_support":
        section["claim_support"] = []
    elif mutation == "partial_closure":
        section["claim_support"][0]["source_field_ids"] = []
    elif mutation == "foreign_paper_key":
        section["claim_support"][0]["paper_key"] = "paper-b"
    elif mutation == "tampered_claim_text":
        section["claims"][0] = text + " appended prose"
    elif mutation == "shared_changed_qualifier":
        section["claim_support"][0]["study_id"] = "foreign-study"
    elif mutation == "shared_foreign_source":
        foreign_paper = inventory.paper_by_key()["paper-b"]
        section["claim_support"][0]["source_claim_ids"].append(foreign_paper.claims[0].claim_id)
    elif mutation == "shared_partial_group":
        section["claim_support"].pop()
    elif mutation == "shared_foreign_slot":
        section["claim_support"].append({
            **section["claim_support"][0],
            "claim_slot_id": "foreign:slot",
        })
    elif mutation == "shared_changed_text":
        section["claims"][0] = text + " changed qualifier"
    if mutation is None:
        scope.validate(final_payload)

    source_ref = ArtifactDependencyRefV2.from_record(inventory_record)
    artifacts_dir = workspace / "artifacts" / "candidate_oracle_binding"
    scope_path = artifacts_dir / "candidate_output_scope.json"
    _write_json(scope_path, scope.to_dict())
    scope_record = registry.register_file(
        artifact_id="fixture:candidate_output_scope",
        artifact_role="candidate_output_scope",
        artifact_type="outline_candidate_output_scope",
        artifact_version="v1",
        path=scope_path,
        producer="tests.run_first_candidate_oracle_binding",
        depends_on=[source_ref],
    )
    bank_path = artifacts_dir / "candidate_fragment_bank.json"
    _write_json(bank_path, bank)
    bank_record = registry.register_file(
        artifact_id="fixture:candidate_fragment_bank",
        artifact_role="candidate_fragment_bank",
        artifact_type="outline_candidate_fragment_bank",
        artifact_version="v1",
        path=bank_path,
        producer="tests.run_first_candidate_oracle_binding",
        depends_on=[ArtifactDependencyRefV2.from_record(scope_record), source_ref],
    )
    outline_dependencies = [ArtifactDependencyRefV2.from_record(bank_record)]
    outline_hashes = {bank_record.artifact_id: bank_record.content_hash}
    final_outline_body = _outline_envelope(
        artifact_type="final_outline", job_id=registry.job_id,
        dependency_hashes=outline_hashes, payload=final_payload,
    )
    final_path = artifacts_dir / "final_outline.json"
    _write_json(final_path, final_outline_body)
    final_record = registry.register_file(
        artifact_id="outline-v3:final_outline",
        artifact_role="outline_v3_node_output",
        artifact_type="final_outline",
        artifact_version="v3",
        path=final_path,
        producer="outline.v3_executor.OutlineV3Executor",
        depends_on=outline_dependencies,
        metadata={"job_id": registry.job_id, "node_id": "final_outline"},
    )
    adoption_id = f"outline-v3:adoption:{final_record.content_hash[:16]}"
    adoption_payload = {
        "status": "adopted",
        "adoption_id": adoption_id,
        "adoption_identity": adoption_id,
        "current_pointer_artifact_id": "outline-v3:adoption:current",
        "current_pointer_role": "current",
        "actor": "tests.run_first_candidate_oracle_binding",
        "reason": "exact local Registry fixture adoption",
        "expected_hash": final_record.content_hash,
        "final_outline_hash": final_record.content_hash,
        "coverage_audit_hash": "a" * 64,
        "stability_audit_hash": "b" * 64,
        "stage_health_hash": "c" * 64,
        "provider_receipt_closure_hash": "d" * 64,
        "final_outline": final_payload,
    }
    adoption_body = _outline_envelope(
        artifact_type="adopted_outline", job_id=registry.job_id,
        dependency_hashes={final_record.artifact_id: final_record.content_hash},
        payload=adoption_payload,
    )
    adoption_path = artifacts_dir / "adopted_outline.json"
    _write_json(adoption_path, adoption_body)
    adopted_record = registry.register_file(
        artifact_id=adoption_id,
        artifact_role="adopted_outline",
        artifact_type="adopted_outline",
        artifact_version="v3",
        path=adoption_path,
        producer="tests.run_first_candidate_oracle_binding",
        depends_on=[ArtifactDependencyRefV2.from_record(final_record)],
    )
    return registry, adopted_record, final_record, bank_record, scope_record, entry, text, supports, bank_path


def test_exact_adopted_registry_closure_binds_whole_single_source_claim(tmp_path: Path) -> None:
    registry, adopted, final, bank, scope, entry, text, supports, _bank_path = _published_registry(tmp_path)
    proof = verify_single_source_candidate_bindings(registry, adopted)

    assert proof.is_verified
    assert proof.matches(0, 0, text, supports) == "paper-a"
    assert proof.matches(0, 0, text + " ", supports) is None
    wrong_rows = supports[:-1] if len(supports) > 1 else []
    assert proof.matches(0, 0, text, wrong_rows) is None
    evidence = proof.to_dict()
    assert evidence["binding_basis"] == "adopted_outline_registry_closure_exact_bank_scope_slots/v1"
    assert evidence["adopted_outline"] == {
        "artifact_id": adopted.artifact_id,
        "content_hash": adopted.content_hash,
    }
    assert evidence["final_outline"] == {"artifact_id": final.artifact_id, "content_hash": final.content_hash}
    bank_payload = json.loads(Path(bank.path).read_text(encoding="utf-8"))
    scope_payload = json.loads(Path(scope.path).read_text(encoding="utf-8"))
    assert evidence["fragment_bank"] == {
        "artifact_id": bank.artifact_id,
        "content_hash": bank.content_hash,
        "bank_content_hash": bank_payload["content_hash"],
    }
    assert evidence["candidate_scope"] == {
        "artifact_id": scope.artifact_id,
        "content_hash": scope.content_hash,
        "scope_hash": scope_payload["content_hash"],
    }
    assert "exact_text" not in json.dumps(evidence)
    assert evidence["bindings"][0]["claim_text_sha256"]
    assert evidence["bindings"][0]["support_rows_sha256"]


def test_exact_shared_assertion_requires_complete_independent_single_source_groups(tmp_path: Path) -> None:
    registry, adopted, _final, _bank, _scope, _entry, text, supports, _path = _published_registry(
        tmp_path, claim_mode="shared_independent_sources",
    )
    proof = verify_single_source_candidate_bindings(registry, adopted)

    assert proof.is_verified
    assert proof.matches(0, 0, text, supports) is None
    assert proof.matches_shared(0, 0, text, supports) == ("paper-a", "paper-b")
    assert proof.matches_shared(0, 0, text + " ", supports) is None
    assert proof.matches_shared(1, 0, text, supports) is None
    assert proof.matches_shared(0, 1, text, supports) is None
    assert proof.matches_shared(0, 0, text, supports[:-1]) is None
    evidence = proof.to_dict()
    assert evidence["shared_bindings"][0]["paper_keys"] == ["paper-a", "paper-b"]
    assert evidence["shared_bindings"][0]["bank_group_ids"]
    assert "exact_text" not in json.dumps(evidence)


def test_one_multisource_comparison_group_never_becomes_shared_source_authority(tmp_path: Path) -> None:
    registry, adopted, _final, _bank, _scope, _entry, text, supports, _path = _published_registry(
        tmp_path, claim_mode="multi_source_group",
    )
    proof = verify_single_source_candidate_bindings(registry, adopted)

    assert proof.is_verified
    assert proof.matches(0, 0, text, supports) is None
    assert proof.matches_shared(0, 0, text, supports) is None
    assert proof.to_dict()["shared_bindings"] == []
    assert proof.to_dict()["rejected_occurrences"] == [
        {
            "section_index": 0,
            "claim_index": 0,
            "reason": "shared_support_requires_independent_single_source_groups",
        },
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "shared_changed_qualifier",
        "shared_foreign_source",
        "shared_partial_group",
        "shared_foreign_slot",
        "shared_changed_text",
    ],
)
def test_shared_source_binding_rejects_changed_or_incomplete_support(
    tmp_path: Path, mutation: str,
) -> None:
    registry, adopted, _final, _bank, _scope, _entry, text, supports, _path = _published_registry(
        tmp_path, mutation=mutation, claim_mode="shared_independent_sources",
    )
    proof = verify_single_source_candidate_bindings(registry, adopted)

    assert proof.is_verified
    assert proof.matches_shared(0, 0, text, supports) is None
    assert proof.to_dict()["shared_bindings"] == []


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_support", "claim_support_missing"),
        ("partial_closure", "support_row_differs_from_exact_bank_slot"),
        ("foreign_paper_key", "support_row_differs_from_exact_bank_slot"),
        ("tampered_claim_text", "claim_support_missing"),
    ],
)
def test_missing_foreign_partial_or_changed_support_never_gets_a_source_mapping(
    tmp_path: Path, mutation: str, expected_reason: str,
) -> None:
    registry, adopted, _final, _bank, _scope, _entry, text, supports, _path = _published_registry(
        tmp_path, mutation=mutation,
    )
    proof = verify_single_source_candidate_bindings(registry, adopted)

    assert proof.is_verified
    assert proof.matches(0, 0, text, supports) is None
    assert proof.to_dict()["rejected_occurrences"] == [
        {"section_index": 0, "claim_index": 0, "reason": expected_reason},
    ]


def test_registered_bank_byte_tampering_invalidates_entire_binding_proof(tmp_path: Path) -> None:
    registry, adopted, _final, _bank, _scope, _entry, _text, _supports, bank_path = _published_registry(tmp_path)
    bank_path.write_text(bank_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(CandidateOracleBindingError, match="Registry closure"):
        verify_single_source_candidate_bindings(registry, adopted)


def test_rehashed_scope_and_bank_cannot_invent_foreign_source_closure(tmp_path: Path) -> None:
    registry, adopted, _final, _bank, _scope, _entry, _text, _supports, _path = _published_registry(
        tmp_path, forged_source_ids=True,
    )

    with pytest.raises(CandidateOracleBindingError, match="source-normalized|source membership"):
        verify_single_source_candidate_bindings(registry, adopted)


def test_unadopted_final_outline_cannot_mint_source_bindings(tmp_path: Path) -> None:
    registry, _adopted, final, _bank, _scope, _entry, _text, _supports, _path = _published_registry(tmp_path)

    with pytest.raises(CandidateOracleBindingError, match="adopted Outline"):
        verify_single_source_candidate_bindings(registry, final)

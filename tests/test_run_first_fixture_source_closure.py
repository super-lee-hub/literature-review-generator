from __future__ import annotations

import json
from typing import Any

from outline.candidate_output_scope import _claim_group as candidate_claim_group
from outline.candidate_output_scope import _inventory_indexes
from services.writer_source_inventory import (
    VerifiedWriterInterpretationDependencyV1,
    VerifiedWriterSourceClaimV1,
    VerifiedWriterSourceEvidenceV1,
    VerifiedWriterSourceFieldV1,
    VerifiedWriterSourceInventoryV1,
    VerifiedWriterSourcePaperV1,
)
from tests.test_current_runtime_full_e2e import (
    _close_source_claim,
    _source_bound_topic_response,
)


def _field(
    field_id: str,
    *,
    value: str,
    evidence_id: str,
    paper_key: str = "paper-a",
    scope: str = "paper",
    study_id: str = "",
    owner_study_id: str = "",
) -> dict[str, Any]:
    return {
        "source_field_id": field_id,
        "paper_key": paper_key,
        "owner_study_id": owner_study_id,
        "scope": scope,
        "study_id": study_id,
        "source_path": "results.boundary",
        "source_value": value,
        "disposition": "exact",
        "canonical_field": "study boundary",
        "evidence_ids": [evidence_id],
    }


def _verified_inventory(
    *,
    claims: tuple[VerifiedWriterSourceClaimV1, ...],
    evidence: tuple[VerifiedWriterSourceEvidenceV1, ...],
    fields: tuple[VerifiedWriterSourceFieldV1, ...] = (),
    dependencies: tuple[VerifiedWriterInterpretationDependencyV1, ...] = (),
) -> VerifiedWriterSourceInventoryV1:
    paper = VerifiedWriterSourcePaperV1(
        paper_key="paper-a",
        dossier_id="dossier:paper-a",
        source_summary_hash="a" * 64,
        source_summary_hashes=("a" * 64,),
        dossier_content_hash="b" * 64,
        evidence_view_hash="c" * 64,
        dossier_status="ready",
        diagnostics=(),
        claims=claims,
        evidence=evidence,
        source_fields=fields,
        units=(),
        interpretation_dependencies=dependencies,
    )
    return VerifiedWriterSourceInventoryV1(
        registry_job_id="fixture-job",
        artifact_id="outline-v3:outline_content_layers",
        artifact_hash="d" * 64,
        content_hash="e" * 64,
        papers=(paper,),
    )


def _build_candidate_group(
    inventory: VerifiedWriterSourceInventoryV1,
    claim: dict[str, Any],
) -> Any:
    group, slots = candidate_claim_group(
        inventory=inventory,
        indexes=_inventory_indexes(inventory),
        task_id="topic-family-test",
        topic_id="topic:paper-a",
        fragment_id="fragment:paper-a",
        provider_result_id="result:paper-a",
        claim=claim,
        selected_relation_ids=(),
    )
    return group, slots


def test_unqualified_claim_with_incidental_field_overlap_keeps_claim_and_evidence_only() -> None:
    source_claim = {
        "claim_id": "claim:paper-a:limitation",
        "claim_type": "author_interpretation",
        "text": "The study reports a limitation for its sample.",
        "evidence_ids": ["evidence:paper-a:limitation"],
        "source_locator": "discussion:limitations",
    }
    evidence_id = source_claim["evidence_ids"][0]
    incidental_field = _field(
        "source-field:paper-a:descriptive-limitation",
        value=source_claim["text"],
        evidence_id=evidence_id,
    )
    record = {
        "paper_key": "paper-a",
        "study_id": "",
        "claim": source_claim,
        "claims_by_id": {source_claim["claim_id"]: source_claim},
        "dependencies": [],
        "fields_by_id": {incidental_field["source_field_id"]: incidental_field},
    }

    closed = _close_source_claim(record)
    assert closed is not None
    assert closed["source_claim_ids"] == [source_claim["claim_id"]]
    assert closed["evidence_ids"] == [evidence_id]
    assert closed["source_field_ids"] == []

    inventory = _verified_inventory(
        claims=(VerifiedWriterSourceClaimV1(
            claim_id=source_claim["claim_id"],
            paper_key="paper-a",
            owner_study_ids=(),
            claim_type=source_claim["claim_type"],
            text=source_claim["text"],
            evidence_ids=(evidence_id,),
            source_locator=source_claim["source_locator"],
            source_summary_hash="a" * 64,
        ),),
        evidence=(VerifiedWriterSourceEvidenceV1(
            evidence_id=evidence_id,
            paper_key="paper-a",
            owner_study_ids=(),
            text=source_claim["text"],
        ),),
    )
    group, slots = _build_candidate_group(inventory, {
        "claim_id": "synthesis:topic_synthesis:fieldless",
        "paper_key": "paper-a",
        "topic_ids": ["topic:paper-a"],
        "fragment_id": "fragment:paper-a",
        "text": source_claim["text"],
        "source_claim_ids": [source_claim["claim_id"]],
        "evidence_ids": [evidence_id],
        "source_field_ids": [],
        "primary_claim_id": source_claim["claim_id"],
    })
    assert group.paper_keys == ("paper-a",)
    assert slots[0].source_claim_ids == (source_claim["claim_id"],)
    assert slots[0].evidence_ids == (evidence_id,)
    assert slots[0].source_field_ids == ()


def test_declared_study_qualifier_preserves_its_source_paper_and_exact_field() -> None:
    primary = {
        "claim_id": "claim:paper-a:finding",
        "claim_type": "empirical_finding",
        "text": "Paper A reports the finding under its recorded sample condition.",
        "evidence_ids": ["evidence:paper-a:finding"],
        "source_locator": "results:study-a",
    }
    qualifier = {
        "claim_id": "claim:paper-a:condition",
        "claim_type": "author_interpretation",
        "text": "The finding is reported only for Study A's sample.",
        "evidence_ids": ["evidence:paper-a:condition"],
        "source_locator": "methods:study-a",
    }
    field_id = "source-field:paper-a:study-a-condition"
    field_value = qualifier["text"]
    field = _field(
        field_id,
        value=field_value,
        evidence_id=qualifier["evidence_ids"][0],
        scope="explicit_study",
        study_id="study-a",
        owner_study_id="study-a",
    )
    dependency = {
        "dependency_id": "dependency:paper-a:study-a-finding",
        "primary_claim_id": primary["claim_id"],
        "primary_evidence_ids": list(primary["evidence_ids"]),
        "required_source_claim_ids": [qualifier["claim_id"]],
        "required_evidence_ids": list(qualifier["evidence_ids"]),
        "required_source_field_ids": [field_id],
        "scope": "explicit_study",
        "study_id": "study-a",
        "paper_key": "paper-a",
        "owner_study_id": "study-a",
        "reason": "preserve the source-reported study condition",
    }
    record = {
        "paper_key": "paper-a",
        "study_id": "study-a",
        "claim": primary,
        "claims_by_id": {primary["claim_id"]: primary, qualifier["claim_id"]: qualifier},
        "dependencies": [dependency],
        "fields_by_id": {field_id: field},
    }
    closed = _close_source_claim(record)

    assert closed is not None
    assert set(closed["source_claim_ids"]) == {primary["claim_id"], qualifier["claim_id"]}
    assert set(closed["evidence_ids"]) == {
        *primary["evidence_ids"],
        *qualifier["evidence_ids"],
    }
    assert closed["source_field_ids"] == [field_id]
    assert closed["qualifier_source_field_ids"] == [field_id]
    assert field_value in closed["text"]

    inventory = _verified_inventory(
        claims=(
            VerifiedWriterSourceClaimV1(
                claim_id=primary["claim_id"],
                paper_key="paper-a",
                owner_study_ids=("study-a",),
                claim_type=primary["claim_type"],
                text=primary["text"],
                evidence_ids=tuple(primary["evidence_ids"]),
                source_locator=primary["source_locator"],
                source_summary_hash="a" * 64,
            ),
            VerifiedWriterSourceClaimV1(
                claim_id=qualifier["claim_id"],
                paper_key="paper-a",
                owner_study_ids=("study-a",),
                claim_type=qualifier["claim_type"],
                text=qualifier["text"],
                evidence_ids=tuple(qualifier["evidence_ids"]),
                source_locator=qualifier["source_locator"],
                source_summary_hash="a" * 64,
            ),
        ),
        evidence=(
            VerifiedWriterSourceEvidenceV1(
                evidence_id=primary["evidence_ids"][0],
                paper_key="paper-a",
                owner_study_ids=("study-a",),
                text=primary["text"],
            ),
            VerifiedWriterSourceEvidenceV1(
                evidence_id=qualifier["evidence_ids"][0],
                paper_key="paper-a",
                owner_study_ids=("study-a",),
                text=qualifier["text"],
            ),
        ),
        fields=(VerifiedWriterSourceFieldV1(
            source_field_id=field_id,
            paper_key="paper-a",
            owner_study_ids=("study-a",),
            source_path="results.boundary",
            source_value=field_value,
            disposition="exact",
            canonical_field="study boundary",
            scope="explicit_study",
            source_study_id="study-a",
            interpretation_required=True,
            source_summary_hash="a" * 64,
            derived_value="",
        ),),
        dependencies=(VerifiedWriterInterpretationDependencyV1(
            paper_key="paper-a",
            owner_study_id="study-a",
            primary_claim_id=primary["claim_id"],
            required_source_claim_ids=(qualifier["claim_id"],),
            required_evidence_ids=tuple(qualifier["evidence_ids"]),
            required_source_field_ids=(field_id,),
            scope="explicit_study",
            study_id="study-a",
            reason=dependency["reason"],
        ),),
    )
    group, slots = _build_candidate_group(inventory, {
        "claim_id": "synthesis:topic_synthesis:study-qualified",
        "paper_key": "paper-a",
        "topic_ids": ["topic:paper-a"],
        "fragment_id": "fragment:paper-a",
        "text": closed["text"],
        "source_claim_ids": closed["source_claim_ids"],
        "evidence_ids": closed["evidence_ids"],
        "source_field_ids": closed["source_field_ids"],
        "primary_claim_id": primary["claim_id"],
        "study_id": "study-a",
    })
    assert group.paper_keys == ("paper-a",)
    assert slots[0].study_id == "study-a"
    assert slots[0].source_claim_ids == tuple(sorted((primary["claim_id"], qualifier["claim_id"])))
    assert slots[0].source_field_ids == (field_id,)


def test_source_bound_topic_fixture_emits_fieldless_claim_when_exact_closure_is_fieldless() -> None:
    source_claim = {
        "claim_id": "claim:paper-a:limitation",
        "claim_type": "author_interpretation",
        "text": "The study reports a limitation for its sample.",
        "evidence_ids": ["evidence:paper-a:limitation"],
        "source_locator": "discussion:limitations",
    }
    field = _field(
        "source-field:paper-a:descriptive-limitation",
        value=source_claim["text"],
        evidence_id=source_claim["evidence_ids"][0],
    )
    trace: list[dict[str, Any]] = []
    request = {
        "task": "substantive_topic_synthesis",
        "evidence_projection": {"projection": "complete_field_values_plus_registry_refs_v2"},
        "topics": [{
            "topic_id": "topic:paper-a:context",
            "fragment_id": "fragment:paper-a:context",
            "paper_ids": ["paper-a"],
            "planned_evidence_ids": list(source_claim["evidence_ids"]),
        }],
        "evidence_units": [{
            "paper_key": "paper-a",
            "source_summary_hash": "a" * 64,
            "study_units": [{
                "study_id": "study-a",
                "source_study_id": "study-a",
                "claims": [source_claim],
                "interpretation_source_fields": [field],
                "interpretation_dependencies": [],
            }],
            "claims": [],
            "evidence_text_by_id": {
                source_claim["evidence_ids"][0]: source_claim["text"],
            },
        }],
    }

    response = _source_bound_topic_response(
        "topic_synthesis_provider:batch:1", request, trace
    )
    content = response["content"]
    claim = content["claims"][0]
    closure = trace[0]["claim_closures"][0]

    assert trace[0]["claims"][0]["claim_id"] == claim["claim_id"] == closure["synthesis_claim_id"]
    assert claim["source_claim_ids"] == [source_claim["claim_id"]]
    assert claim["evidence_ids"] == source_claim["evidence_ids"]
    assert claim["source_field_ids"] == []
    assert closure["source_claim_ids"] == [source_claim["claim_id"]]
    assert closure["evidence_ids"] == source_claim["evidence_ids"]
    assert closure["source_field_ids"] == []


def test_two_paper_fragment_emits_each_papers_best_exact_source_closure_under_output_cap() -> None:
    required_paper = "10.1002/mar.21356"
    other_paper = "10.1002/dir.20024"
    required_boundary = {
        "claim_id": f"claim:{required_paper}:limitations",
        "claim_type": "author_interpretation",
        "text": "The source reports a paper-level limitation.",
        "evidence_ids": [f"evidence:{required_paper}:limitations"],
        "source_locator": "discussion:limitations",
    }
    unresolved_finding = {
        "claim_id": f"claim:{required_paper}:finding",
        "claim_type": "empirical_finding",
        "text": "The finding has an unresolved condition boundary.",
        "evidence_ids": [f"evidence:{required_paper}:finding"],
        "source_locator": "results:finding",
    }
    other_primary = {
        "claim_id": f"claim:{other_paper}:finding",
        "claim_type": "empirical_finding",
        "text": "The other paper reports a qualified finding.",
        "evidence_ids": [f"evidence:{other_paper}:finding"],
        "source_locator": "results:finding",
    }
    other_qualifier = {
        "claim_id": f"claim:{other_paper}:condition",
        "claim_type": "author_interpretation",
        "text": "The other finding is limited to its recorded sample.",
        "evidence_ids": [f"evidence:{other_paper}:condition"],
        "source_locator": "methods:sample",
    }
    target_incidental_field = _field(
        f"source-field:{required_paper}:incidental",
        value=required_boundary["text"],
        evidence_id=required_boundary["evidence_ids"][0],
        paper_key=required_paper,
    )
    other_field_id = f"source-field:{other_paper}:sample"
    other_field_value = other_qualifier["text"]
    other_field = _field(
        other_field_id,
        value=other_field_value,
        evidence_id=other_qualifier["evidence_ids"][0],
        paper_key=other_paper,
    )
    evidence_units = [
        {
            "paper_key": required_paper,
            "source_summary_hash": "a" * 64,
            "study_units": [{
                "study_id": "",
                "source_study_id": "",
                "claims": [required_boundary, unresolved_finding],
                "interpretation_source_fields": [target_incidental_field],
                "interpretation_dependencies": [{
                    "dependency_id": f"dependency:{required_paper}:unresolved-finding",
                    "primary_claim_id": unresolved_finding["claim_id"],
                    "required_source_claim_ids": [f"claim:{required_paper}:missing-qualifier"],
                    "required_evidence_ids": [f"evidence:{required_paper}:missing-qualifier"],
                    "required_source_field_ids": [f"source-field:{required_paper}:missing-qualifier"],
                    "scope": "unresolved",
                    "study_id": "",
                    "reason": "source condition cannot be assigned to this finding",
                }],
            }],
            "claims": [],
        },
        {
            "paper_key": other_paper,
            "source_summary_hash": "b" * 64,
            "study_units": [{
                "study_id": "",
                "source_study_id": "",
                "claims": [other_primary, other_qualifier],
                "interpretation_source_fields": [other_field],
                "interpretation_dependencies": [{
                    "dependency_id": f"dependency:{other_paper}:sample",
                    "primary_claim_id": other_primary["claim_id"],
                    "required_source_claim_ids": [other_qualifier["claim_id"]],
                    "required_evidence_ids": list(other_qualifier["evidence_ids"]),
                    "required_source_field_ids": [other_field_id],
                    "scope": "paper",
                    "study_id": "",
                    "reason": "preserve the paper-level sample qualifier",
                }],
            }],
            "claims": [],
        },
    ]
    request = {
        "task": "substantive_topic_synthesis",
        "evidence_projection": {"projection": "complete_field_values_plus_registry_refs_v2"},
        "output_contract": {"max_output_tokens": 4_096},
        "topics": [{
            "topic_id": "topic:context:two-paper-fragment",
            "fragment_id": "fragment:context:two-paper-fragment",
            "paper_ids": [required_paper, other_paper],
            "planned_evidence_ids": [
                *required_boundary["evidence_ids"],
                *unresolved_finding["evidence_ids"],
                *other_primary["evidence_ids"],
                *other_qualifier["evidence_ids"],
            ],
        }],
        "evidence_units": evidence_units,
    }
    trace: list[dict[str, Any]] = []

    response = _source_bound_topic_response(
        "topic_synthesis_provider:batch:3", request, trace
    )
    content = response["content"]
    claims = content["claims"]
    by_paper = {str(claim["paper_key"]): claim for claim in claims}

    assert set(by_paper) == {required_paper, other_paper}
    target_claim = by_paper[required_paper]
    assert target_claim["primary_claim_id"] == required_boundary["claim_id"]
    assert target_claim["source_claim_ids"] == [required_boundary["claim_id"]]
    assert target_claim["evidence_ids"] == required_boundary["evidence_ids"]
    assert target_claim["source_field_ids"] == []
    assert target_claim["primary_claim_id"] != unresolved_finding["claim_id"]

    qualified_claim = by_paper[other_paper]
    assert qualified_claim["primary_claim_id"] == other_primary["claim_id"]
    assert set(qualified_claim["source_claim_ids"]) == {
        other_primary["claim_id"],
        other_qualifier["claim_id"],
    }
    assert qualified_claim["source_field_ids"] == [other_field_id]
    assert other_field_value in qualified_claim["text"]

    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert (len(serialized) + 3) // 4 <= request["output_contract"]["max_output_tokens"]
    closure_rows = trace[0]["claim_closures"]
    assert len(closure_rows) == 2
    assert {
        str(closure["source_field_ids"][0]) if closure["source_field_ids"] else ""
        for closure in closure_rows
    } == {"", other_field_id}

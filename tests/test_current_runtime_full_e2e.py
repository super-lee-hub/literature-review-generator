from __future__ import annotations

import configparser
import json
import hashlib
import re
import threading
import ai_interface
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
import zipfile
import reviewctl

import fitz  # type: ignore
import pytest

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from services.job_outcome import load_canonical_job_outcome
from summary_schema import normalize_ai_summary
from validation.closure import resolve_current_stage_closure_map
from validation.disposition import ValidationDispositionV1


def _write_pdf(path: Path, title: str, finding: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        f"Title: {title}\n"
        "Methodology: A controlled empirical study with a reproducible design.\n"
        f"Results: {finding}\n"
        "Conclusion: The result is bounded by the tested context.",
    )
    document.save(path)
    document.close()


def _reader_summary(paper_key: str, title: str, finding: str) -> dict[str, Any]:
    summary = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "quantitative",
                "paper_subtype_normalized": "quantitative",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "controlled empirical design",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": title,
                "authors": ["Example Author"],
                "year": "2025",
                "journal": "Example Journal",
                "doi": f"10.1000/{paper_key}",
            },
            "core_analysis": {
                "summary": f"{title} reports a source-grounded empirical result.",
                "key_points": [finding],
                "methodology": "A controlled empirical study with a reproducible design.",
                "findings": finding,
                "conclusions": "The result is bounded by the tested context.",
                "relevance": "The result informs the bounded research question.",
                "limitations": "The result is bounded by the tested context.",
                "research_gap": "Further replication is needed.",
                "theoretical_framework": None,
                "future_research_directions": ["Replicate in another context."],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": [
                        "Does the treatment improve the outcome?"
                    ],
                    "data_source_and_size": "A reproducible controlled sample.",
                    "analysis_technique": "Regression analysis.",
                    "core_variables": {
                        "independent": ["treatment"],
                        "dependent": ["outcome"],
                    },
                    "sample_characteristics_or_context": "Controlled context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )
    summary["status"] = "success"
    summary["paper_info"] = {
        "canonical_paper_key": paper_key,
        "source_paper_id": paper_key,
        "title": title,
        "authors": ["Example Author"],
        "year": 2025,
        "classification": "core",
        "must_use": True,
    }
    return summary


def _provider_response(content: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "content": dict(content),
        "finish_reason": "stop",
        "input_tokens": 420,
        "output_tokens": 96,
        "total_tokens": 516,
        "usage_status": "reported",
    }


def _source_claim_records(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read source claims and their exact scope from a real topic request."""

    records: list[dict[str, Any]] = []
    for unit in request.get("evidence_units") or ():
        if not isinstance(unit, Mapping):
            continue
        paper_key = str(unit.get("paper_key") or "")
        if not paper_key:
            continue
        study_claim_ids: set[str] = set()
        for study in unit.get("study_units") or ():
            if not isinstance(study, Mapping):
                continue
            claims = [dict(claim) for claim in study.get("claims") or () if isinstance(claim, Mapping)]
            study_claim_ids.update(str(claim.get("claim_id") or "") for claim in claims)
            study_id = str(study.get("study_id") or "") if str(study.get("source_study_id") or "") else ""
            fields_by_id = {
                str(field.get("source_field_id") or ""): dict(field)
                for field in [
                    item for item in study.get("interpretation_source_fields") or ()
                    if isinstance(item, Mapping)
                ]
                if str(field.get("source_field_id") or "")
            }
            records.extend(
                {
                    "paper_key": paper_key,
                    "study_id": study_id,
                    "claim": claim,
                    "claims_by_id": {
                        str(item.get("claim_id") or ""): item
                        for item in claims
                        if str(item.get("claim_id") or "")
                    },
                    "dependencies": [
                        dict(dependency)
                        for dependency in study.get("interpretation_dependencies") or ()
                        if isinstance(dependency, Mapping)
                    ],
                    "fields_by_id": fields_by_id,
                    "source_summary_hash": str(unit.get("source_summary_hash") or ""),
                }
                for claim in claims
                if str(claim.get("claim_id") or "")
            )

        # Paper-level claims are separate from nested study claims and must not
        # inherit an internal study identity or its interpretation dependencies.
        paper_claims = [
            dict(claim)
            for claim in unit.get("claims") or ()
            if isinstance(claim, Mapping)
            and str(claim.get("claim_id") or "")
            and str(claim.get("claim_id") or "") not in study_claim_ids
        ]
        records.extend(
            {
                "paper_key": paper_key,
                "study_id": "",
                "claim": claim,
                "claims_by_id": {
                    str(item.get("claim_id") or ""): item
                    for item in paper_claims
                    if str(item.get("claim_id") or "")
                },
                "dependencies": [],
                # The semantic provider's field authority is the exact field
                # ledger inside its evidence unit's study rows. Unit-level
                # source_field_ledger values are not provider-owned identities.
                "fields_by_id": {},
                "source_summary_hash": str(unit.get("source_summary_hash") or ""),
            }
            for claim in paper_claims
        )
    return records


def _close_source_claim(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only the source claim plus its declared dependency closure."""

    primary = record.get("claim")
    claims_by_id = record.get("claims_by_id")
    dependencies = [item for item in record.get("dependencies") or () if isinstance(item, Mapping)]
    fields_by_id = record.get("fields_by_id")
    if not isinstance(primary, Mapping) or not isinstance(claims_by_id, Mapping) or not isinstance(fields_by_id, Mapping):
        return None
    primary_id = str(primary.get("claim_id") or "")
    primary_text = str(primary.get("text") or "").strip()
    if not primary_id or not primary_text:
        return None

    source_claim_ids = {primary_id}
    evidence_ids: set[str] = set()
    source_field_ids: set[str] = set()
    qualifier_claim_ids: set[str] = set()
    qualifier_evidence_ids: set[str] = set()
    qualifier_field_ids: set[str] = set()
    used_dependencies: dict[str, Mapping[str, Any]] = {}
    changed = True
    while changed:
        before = (len(source_claim_ids), len(evidence_ids), len(source_field_ids), len(used_dependencies))
        for source_claim_id in list(source_claim_ids):
            source_claim = claims_by_id.get(source_claim_id)
            if not isinstance(source_claim, Mapping):
                return None
            evidence_ids.update(str(value) for value in source_claim.get("evidence_ids") or () if str(value))
            for field_id, field in fields_by_id.items():
                if not isinstance(field, Mapping):
                    continue
                value = str(field.get("source_value") or "").strip()
                field_evidence_ids = {
                    str(item) for item in field.get("evidence_ids") or () if str(item)
                }
                claim_evidence_ids = {
                    str(item) for item in source_claim.get("evidence_ids") or () if str(item)
                }
                owner_paper = str(field.get("paper_key") or record.get("paper_key") or "")
                owner_study = str(field.get("owner_study_id") or "")
                if (
                    owner_paper == str(record.get("paper_key") or "")
                    and (not record.get("study_id") or not owner_study or owner_study == record.get("study_id"))
                    and value
                    and (
                        value == str(source_claim.get("text") or "").strip()
                        or bool(field_evidence_ids.intersection(claim_evidence_ids))
                    )
                ):
                    source_field_ids.add(str(field_id))
            for dependency in dependencies:
                if str(dependency.get("primary_claim_id") or "") != source_claim_id:
                    continue
                if str(dependency.get("scope") or "") == "unresolved":
                    return None
                dependency_key = json.dumps(dict(dependency), ensure_ascii=False, sort_keys=True)
                used_dependencies[dependency_key] = dependency
                required_claims = {
                    str(value) for value in dependency.get("required_source_claim_ids") or () if str(value)
                }
                required_evidence = {
                    str(value) for value in dependency.get("required_evidence_ids") or () if str(value)
                }
                required_fields = {
                    str(value) for value in dependency.get("required_source_field_ids") or () if str(value)
                }
                if not required_claims.issubset(claims_by_id):
                    return None
                if not required_fields.issubset(fields_by_id):
                    return None
                qualifier_claim_ids.update(required_claims)
                qualifier_evidence_ids.update(required_evidence)
                qualifier_field_ids.update(required_fields)
                source_claim_ids.update(required_claims)
                evidence_ids.update(required_evidence)
                source_field_ids.update(required_fields)
                for evidence_id in dependency.get("primary_evidence_ids") or ():
                    if str(evidence_id):
                        evidence_ids.add(str(evidence_id))
        after = (len(source_claim_ids), len(evidence_ids), len(source_field_ids), len(used_dependencies))
        changed = before != after

    if not evidence_ids:
        return None
    field_texts = [
        str(fields_by_id[field_id].get("source_value") or "").strip()
        for field_id in sorted(source_field_ids)
        if isinstance(fields_by_id.get(field_id), Mapping)
        and str(fields_by_id[field_id].get("source_value") or "").strip()
    ]
    if not source_field_ids or len(field_texts) != len(source_field_ids):
        return None

    text_parts = [primary_text]
    for source_claim_id in sorted(source_claim_ids - {primary_id}):
        source_text = str(claims_by_id[source_claim_id].get("text") or "").strip()
        if source_text and source_text.casefold() not in "; ".join(text_parts).casefold():
            text_parts.append(source_text)
    combined = "; ".join(
        re.sub(r"[\s.!?。！？；;]+$", "", value).strip()
        for value in text_parts
        if value.strip()
    )
    existing_text = combined.casefold()
    for field_id in sorted(source_field_ids):
        field = fields_by_id[field_id]
        value = str(field.get("source_value") or "").strip()
        if value and value.casefold() not in existing_text:
            label = str(field.get("canonical_field") or field.get("source_path") or "source boundary")
            cleaned_value = re.sub(r"[\s.!?。！？；;]+$", "", value).strip()
            combined += f"; {label.replace('_', ' ')}: {cleaned_value}"
            existing_text = combined.casefold()
    text = combined.rstrip(" ;") + "."
    if not qualifier_claim_ids and not used_dependencies:
        qualifier_evidence_ids = set()
        qualifier_field_ids = set()
    return {
        "primary_claim_id": primary_id,
        "source_claim_ids": sorted(source_claim_ids),
        "evidence_ids": sorted(evidence_ids),
        "primary_evidence_ids": sorted(
            str(value) for value in primary.get("evidence_ids") or () if str(value)
        ),
        "source_field_ids": sorted(source_field_ids),
        "qualifier_source_claim_ids": sorted(qualifier_claim_ids),
        "qualifier_evidence_ids": sorted(qualifier_evidence_ids),
        "qualifier_source_field_ids": sorted(qualifier_field_ids),
        "dependency_rows": [dict(value) for value in used_dependencies.values()],
        "text": text,
    }


def _source_bound_topic_response(
    node_id: str,
    request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    topics = [item for item in request.get("topics") or () if isinstance(item, Mapping)]
    records = _source_claim_records(request)
    claims: list[dict[str, Any]] = []
    claim_closures: list[dict[str, Any]] = []
    topic_outputs: list[dict[str, Any]] = []
    processed_fragments: list[str] = []
    used_source_claims: set[str] = set()
    covered_papers: set[str] = set()
    for topic in topics:
        topic_id = str(topic.get("topic_id") or "")
        fragment_id = str(topic.get("fragment_id") or topic_id)
        paper_ids = {
            str(value)
            for value in (*list(topic.get("paper_ids") or ()), *list(topic.get("bridge_paper_ids") or ()))
            if str(value)
        }
        planned_evidence = {
            str(value) for value in topic.get("planned_evidence_ids") or () if str(value)
        }
        supported: list[dict[str, Any]] = []
        candidates: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any]]] = []
        for record in records:
            paper_key = str(record.get("paper_key") or "")
            source_claim = record.get("claim")
            if paper_key not in paper_ids or not isinstance(source_claim, Mapping):
                continue
            source_claim_id = str(source_claim.get("claim_id") or "")
            if not source_claim_id or source_claim_id in used_source_claims:
                continue
            source_evidence = {
                str(value) for value in source_claim.get("evidence_ids") or () if str(value)
            }
            if planned_evidence and not source_evidence.intersection(planned_evidence):
                continue
            closed = _close_source_claim(record)
            if closed is None:
                continue
            rank = (
                paper_key in covered_papers,
                str(source_claim.get("claim_type") or "") != "empirical_finding",
                paper_key,
                source_claim_id,
            )
            candidates.append((rank, record, closed))
        if candidates:
            _rank, record, closed = sorted(candidates, key=lambda row: row[0])[0]
            source_claim = record["claim"]
            paper_key = str(record["paper_key"])
            source_claim_ids = list(closed["source_claim_ids"])
            local_digest = hashlib.sha256(
                (fragment_id + "|" + paper_key + "|" + closed["primary_claim_id"]).encode("utf-8")
            ).hexdigest()[:20]
            synthesis_claim = {
                "claim_id": f"synthesis:topic_synthesis:fixture-{local_digest}",
                "fragment_id": fragment_id,
                "claim_type": str(source_claim.get("claim_type") or "empirical_finding"),
                "paper_key": paper_key,
                "primary_claim_id": closed["primary_claim_id"],
                "primary_evidence_ids": list(closed["primary_evidence_ids"]),
                "text": closed["text"],
                "source_claim_ids": source_claim_ids,
                "evidence_ids": list(closed["evidence_ids"]),
                "source_field_ids": list(closed["source_field_ids"]),
            }
            if record.get("study_id"):
                synthesis_claim["study_id"] = str(record["study_id"])
            claims.append(synthesis_claim)
            claim_closures.append({
                "fragment_id": fragment_id,
                "primary_claim_id": closed["primary_claim_id"],
                "source_claim_ids": list(closed["source_claim_ids"]),
                "evidence_ids": list(closed["evidence_ids"]),
                "source_field_ids": list(closed["source_field_ids"]),
                "qualifier_source_claim_ids": list(closed["qualifier_source_claim_ids"]),
                "qualifier_evidence_ids": list(closed["qualifier_evidence_ids"]),
                "qualifier_source_field_ids": list(closed["qualifier_source_field_ids"]),
                "dependency_ids": sorted({
                    str(item.get("dependency_id") or "")
                    for item in closed["dependency_rows"]
                    if str(item.get("dependency_id") or "")
                }),
            })
            supported.append(synthesis_claim)
            used_source_claims.update(source_claim_ids)
            covered_papers.add(paper_key)
            status = "completed"
            unresolved: list[str] = []
        else:
            status = "unresolved"
            unresolved = ["No complete source claim and qualifier closure was available in this fragment."]
        evidence_ids = sorted({
            str(value)
            for claim in supported
            for value in claim.get("evidence_ids") or ()
            if str(value)
        })
        topic_outputs.append({
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "status": status,
            "conclusions": [],
            "unresolved_questions": unresolved,
            "supporting_evidence_ids": evidence_ids,
        })
        processed_fragments.append(fragment_id)
    if fixture_trace is not None:
        fixture_trace.append({
            "kind": "topic_source_bound",
            "node_id": node_id,
            "claims": [
                {
                    "paper_key": claim["paper_key"],
                    "source_claim_ids": list(claim["source_claim_ids"]),
                    "evidence_ids": list(claim["evidence_ids"]),
                    "source_field_ids": list(claim["source_field_ids"]),
                }
                for claim in claims
            ],
            "claim_closures": claim_closures,
            "topics": [
                {
                    "topic_id": str(topic_output.get("topic_id") or ""),
                    "fragment_id": str(topic_output.get("fragment_id") or ""),
                    "status": str(topic_output.get("status") or ""),
                    "source_claim_ids": sorted({
                        str(value)
                        for claim in claims
                        if claim.get("fragment_id") == topic_output.get("fragment_id")
                        for value in claim.get("source_claim_ids") or ()
                        if str(value)
                    }),
                    "evidence_ids": sorted({
                        str(value)
                        for claim in claims
                        if claim.get("fragment_id") == topic_output.get("fragment_id")
                        for value in claim.get("evidence_ids") or ()
                        if str(value)
                    }),
                    "source_field_ids": sorted({
                        str(value)
                        for claim in claims
                        if claim.get("fragment_id") == topic_output.get("fragment_id")
                        for value in claim.get("source_field_ids") or ()
                        if str(value)
                    }),
                    "unresolved_questions": list(topic_output.get("unresolved_questions") or ()),
                }
                for topic_output in topic_outputs
            ],
        })
    return _provider_response({
        "topics": topic_outputs,
        "processed_fragment_ids": processed_fragments,
        "claims": claims,
        "unresolved_questions": [],
    })


def _candidate_source_bound_response(
    node_id: str,
    request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    semantic = request.get("shared_semantic_context")
    tables = semantic.get("interpretation_source_tables") if isinstance(semantic, Mapping) else None
    topic_routes = semantic.get("topic_routes") if isinstance(semantic, Mapping) else None
    if not (
        isinstance(request.get("candidate_id"), str)
        and isinstance(request.get("evidence_projection"), Mapping)
        and request["evidence_projection"].get("projection") == "registry_complete_evidence_ref_v1"
        and isinstance(tables, Mapping)
        and isinstance(tables.get("source_fields"), list)
        and isinstance(tables.get("dependencies"), list)
        and isinstance(topic_routes, list)
    ):
        return None
    output_contract = request.get("output_contract")
    if not isinstance(output_contract, Mapping) or output_contract.get(
        "planned_claims_require_source_support"
    ) is not True:
        raise AssertionError("actual candidate request does not require typed source support for every planned claim")

    source_fields = {
        str(item.get("source_field_id") or ""): dict(item)
        for item in tables.get("source_fields") or ()
        if isinstance(item, Mapping) and str(item.get("source_field_id") or "")
    }
    dependencies = [item for item in tables.get("dependencies") or () if isinstance(item, Mapping)]
    claims_by_paper: dict[str, list[dict[str, Any]]] = {}
    for route in topic_routes:
        if not isinstance(route, Mapping):
            continue
        for fragment in route.get("fragments") or ():
            if not isinstance(fragment, Mapping):
                continue
            for result in fragment.get("provider_results") or ():
                if not isinstance(result, Mapping):
                    continue
                provider_output = result.get("provider_output")
                if not isinstance(provider_output, Mapping):
                    continue
                for claim in provider_output.get("claims") or ():
                    if not isinstance(claim, Mapping):
                        continue
                    paper_key = str(claim.get("paper_key") or "")
                    if paper_key and claim.get("source_claim_ids") and claim.get("evidence_ids"):
                        claims_by_paper.setdefault(paper_key, []).append(dict(claim))

    evidence_rows = [item for item in request.get("evidence") or () if isinstance(item, Mapping)]
    if not evidence_rows or not source_fields or not claims_by_paper:
        raise AssertionError("actual candidate request lacks complete source-bound interpretation inputs")
    evidence_rows.sort(key=lambda item: str(item.get("paper_key") or ""))
    relation_ids = [str(item) for item in request.get("relation_ids") or () if str(item)]
    candidate_id = str(request["candidate_id"])
    organizing_logic = str(request.get("organizing_logic") or "evidence")
    sections: list[dict[str, Any]] = []
    trace_sections: list[dict[str, Any]] = []
    for index, evidence in enumerate(evidence_rows, start=1):
        paper_key = str(evidence.get("paper_key") or "")
        choices = [
            claim for claim in claims_by_paper.get(paper_key, ())
            if str(claim.get("primary_claim_id") or "")
            and str(claim.get("text") or "").strip()
        ]
        if not paper_key or not choices:
            raise AssertionError(f"actual candidate request has no source-bound claim for evidence paper {paper_key!r}")
        chosen = sorted(choices, key=lambda item: (
            str(item.get("primary_claim_id") or ""),
            str(item.get("claim_id") or ""),
        ))[0]
        primary_id = str(chosen["primary_claim_id"])
        claim_ids = {str(value) for value in chosen.get("source_claim_ids") or () if str(value)}
        claim_ids.add(primary_id)
        applicable = [
            dependency for dependency in dependencies
            if str(dependency.get("primary_claim_id") or "") in claim_ids
            and str(dependency.get("paper_key") or "") == paper_key
        ]
        evidence_ids = {str(value) for value in chosen.get("evidence_ids") or () if str(value)}
        field_ids = {str(value) for value in chosen.get("source_field_ids") or () if str(value)}
        primary_evidence_ids = {
            str(value) for value in chosen.get("primary_evidence_ids") or () if str(value)
        }
        qualifier_claim_ids: set[str] = set()
        qualifier_evidence_ids: set[str] = set()
        qualifier_field_ids: set[str] = set()
        dependency_ids: set[str] = set()
        for dependency in applicable:
            dependency_id = str(dependency.get("dependency_id") or "")
            if dependency_id:
                dependency_ids.add(dependency_id)
            required_claim_ids = {
                str(value) for value in dependency.get("required_source_claim_ids") or () if str(value)
            }
            required_evidence_ids = {
                str(value) for value in dependency.get("required_evidence_ids") or () if str(value)
            }
            required_field_ids = {
                str(value) for value in dependency.get("required_source_field_ids") or () if str(value)
            }
            qualifier_claim_ids.update(required_claim_ids)
            qualifier_evidence_ids.update(required_evidence_ids)
            qualifier_field_ids.update(required_field_ids)
            claim_ids.update(required_claim_ids)
            evidence_ids.update(required_evidence_ids)
            field_ids.update(required_field_ids)
            primary_evidence_ids.update(
                str(value) for value in dependency.get("primary_evidence_ids") or () if str(value)
            )
        evidence_ids.update(primary_evidence_ids)
        missing_fields = sorted(field_id for field_id in field_ids if field_id not in source_fields)
        if missing_fields:
            raise AssertionError(f"source-bound candidate claim references fields outside its request: {missing_fields}")
        for field_id in field_ids:
            owner_paper = str(source_fields[field_id].get("paper_key") or "")
            if owner_paper and owner_paper != paper_key:
                raise AssertionError(f"source field {field_id} is not owned by candidate paper {paper_key}")
        if not (claim_ids and evidence_ids and field_ids and primary_evidence_ids):
            raise AssertionError(f"source-bound candidate claim has incomplete primary or qualifier closure for {paper_key}")
        text = str(chosen["text"]).strip()
        support = {
            "claim": text,
            "paper_key": paper_key,
            "source_claim_ids": sorted(claim_ids),
            "primary_claim_id": primary_id,
            "evidence_ids": sorted(evidence_ids),
            "primary_evidence_ids": sorted(primary_evidence_ids),
            "source_field_ids": sorted(field_ids),
            "qualifier_source_claim_ids": sorted(qualifier_claim_ids),
            "qualifier_evidence_ids": sorted(qualifier_evidence_ids),
            "qualifier_source_field_ids": sorted(qualifier_field_ids),
            "dependency_ids": sorted(dependency_ids),
            "claim_kind": str(chosen.get("claim_type") or ""),
        }
        source_hash = str(evidence.get("source_summary_hash") or "")
        if source_hash:
            support["source_summary_hash"] = source_hash
            support["source_summary_hashes"] = [source_hash]
        study_id = str(chosen.get("study_id") or "")
        if study_id:
            support["study_id"] = study_id
        output_scope = request.get("candidate_output_scope")
        if isinstance(output_scope, Mapping):
            matching_slots = [
                slot for slot in output_scope.get("claim_slots") or ()
                if isinstance(slot, Mapping) and slot.get("paper_key") == paper_key
                and slot.get("primary_claim_id") == primary_id
                and slot.get("synthesis_claim_id") == chosen.get("claim_id")
            ]
            if not matching_slots:
                raise AssertionError("candidate fixture has no matching finite source claim slot")
            slot = sorted(matching_slots, key=lambda item: str(item["claim_slot_id"]))[0]
            support.update({
                "claim_slot_id": slot["claim_slot_id"], "task_id": slot["task_id"],
                "source_claim_ids": list(slot["source_claim_ids"]),
                "evidence_ids": list(slot["evidence_ids"]),
                "source_field_ids": list(slot["source_field_ids"]),
            })
            if slot.get("study_id"):
                support["study_id"] = slot["study_id"]
            else:
                support.pop("study_id", None)
        section_relations = (
            list(slot.get("relation_ids") or ()) if isinstance(output_scope, Mapping) else list(relation_ids)
        )
        sections.append({
            "section_id": f"{candidate_id}_section_{index}",
            "title": f"{str(evidence.get('title') or paper_key)}: {organizing_logic.replace('_', ' ')} synthesis",
            "goal": "Present one evidence-bound finding with its recorded qualification.",
            "paper_keys": [paper_key],
            "relation_ids": section_relations,
            "claims": [text],
            "claim_support": [support],
            **({"task_ids": [support["task_id"]]} if "task_id" in support else {}),
            "rationale": "Use the matching included-paper claim and preserve its declared source dependencies.",
        })
        trace_sections.append({
            "paper_key": paper_key,
            "source_claim_ids": sorted(claim_ids),
            "evidence_ids": sorted(evidence_ids),
            "source_field_ids": sorted(field_ids),
            "qualifier_source_claim_ids": sorted(qualifier_claim_ids),
            "qualifier_evidence_ids": sorted(qualifier_evidence_ids),
            "qualifier_source_field_ids": sorted(qualifier_field_ids),
            "dependency_ids": sorted(dependency_ids),
        })
    if fixture_trace is not None:
        fixture_trace.append({
            "kind": "candidate_source_bound",
            "node_id": node_id,
            "sections": trace_sections,
        })
    return _provider_response({
        "candidate_id": candidate_id,
        "organizing_logic": organizing_logic,
        "sections": sections,
        "claims": [section["claims"][0] for section in sections],
    })


def _source_bound_writer_response(
    envelope: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    scope = envelope.get("writer_task_scope")
    if not isinstance(scope, Mapping):
        return None
    schema_version = str(scope.get("schema_version") or "")
    if schema_version not in {"writer_task_scope_wire/v1", "writer_task_scope_wire/v2"}:
        raise AssertionError(f"Writer fixture received unsupported production wire schema {schema_version!r}")
    if scope.get("usable_for_provider_admission") is not True:
        raise AssertionError("Writer fixture was called without a verified, admitted source scope")
    basis = str(scope.get("writer_task_basis_hash") or "")
    tasks = [item for item in scope.get("tasks") or () if isinstance(item, Mapping)]
    if not tasks or not isinstance(scope.get("evidence_store"), Mapping):
        raise AssertionError("Writer fixture request omitted task rows or its canonical evidence_store")
    blocks: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    task_trace: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("writer_task_id") or "")
        if not task_id or task.get("status") != "ready":
            raise AssertionError(f"Writer fixture received a non-ready source task: {task_id}")
        source_claim_ids = [str(value) for value in task.get("source_claim_ids") or () if str(value)]
        evidence_ids = [str(value) for value in task.get("evidence_ids") or () if str(value)]
        source_field_ids = [str(value) for value in task.get("source_field_ids") or () if str(value)]
        if not source_claim_ids or not evidence_ids or not source_field_ids:
            raise AssertionError(f"Writer task {task_id} omitted canonical claim/evidence/source-field identities")
        units = [item for item in task.get("output_units") or () if isinstance(item, Mapping)]
        primary_unit = next((item for item in units if item.get("unit_kind") == "planned_claim" and item.get("required") is True), None)
        if not isinstance(primary_unit, Mapping):
            raise AssertionError(f"Writer task {task_id} has no required planned-claim output unit")
        allowed_refs = [str(value) for value in task.get("allowed_ref_ids") or () if str(value)]
        if not allowed_refs:
            raise AssertionError(f"Writer task {task_id} has no active citation ref from its source packet")
        planned_claim = str(task.get("planned_claim") or "").strip()
        if not planned_claim:
            raise AssertionError(f"Writer task {task_id} has no source-bound planned claim")
        text = re.sub(r"[\s.!?。！？；;]+$", "", planned_claim).strip()
        text = f"{text} [[cite_ref:{allowed_refs[0]}]]."
        max_chars = int(primary_unit.get("max_text_chars") or 0)
        if max_chars < 1 or len(text) > max_chars:
            raise AssertionError(f"Writer task {task_id} source-bound sentence exceeds its declared output-unit limit")
        blocks.append({
            "writer_task_id": task_id,
            "writer_output_unit_id": str(primary_unit.get("writer_output_unit_id") or ""),
            "writer_task_basis_hash": basis,
            "text": text,
        })
        task_trace.append({
            "writer_task_id": task_id,
            "source_claim_ids": source_claim_ids,
            "evidence_ids": evidence_ids,
            "source_field_ids": source_field_ids,
            "qualifier_source_claim_ids": [
                str(value) for value in task.get("qualifier_source_claim_ids") or () if str(value)
            ],
            "qualifier_evidence_ids": [
                str(value) for value in task.get("qualifier_evidence_ids") or () if str(value)
            ],
            "qualifier_source_field_ids": [
                str(value) for value in task.get("qualifier_source_field_ids") or () if str(value)
            ],
            "planned_claim": planned_claim,
            "allowed_ref_ids": allowed_refs,
            "required_output_unit_id": str(primary_unit.get("writer_output_unit_id") or ""),
        })
        dispositions.append({
            "writer_task_id": task_id,
            "writer_task_basis_hash": basis,
            "disposition": "covered",
        })
    if fixture_trace is not None:
        fixture_trace.append({
            "kind": "writer_source_bound",
            "schema_version": schema_version,
            "writer_task_basis_hash": basis,
            "task_count": len(tasks),
            "tasks": task_trace,
            "returned_blocks": [dict(block) for block in blocks],
            "returned_output_unit_ids": [block["writer_output_unit_id"] for block in blocks],
            "source_store_kinds": sorted({
                str(item.get("kind") or "")
                for item in scope["evidence_store"].values()
                if isinstance(item, Mapping) and str(item.get("kind") or "")
            }),
            "dispositions": dispositions,
        })
    return _provider_response({"blocks": blocks, "task_dispositions": dispositions})


def _start_source_bound_writer_server(
    fixture_trace: list[dict[str, Any]],
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Serve Writer fixture content over the real instrumented HTTP transport."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            call = {"path": self.path, "stage": "request_received"}
            self.server.calls.append(call)  # type: ignore[attr-defined]
            try:
                request_payload = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                )
                call["model"] = str(request_payload.get("model") or "")
                user_text = "\n".join(
                    str(message.get("content") or "")
                    for message in request_payload.get("messages") or ()
                    if isinstance(message, Mapping) and message.get("role") == "user"
                )
                envelope = json.loads(user_text)
                if not isinstance(envelope, Mapping):
                    raise ValueError("Writer request is not a JSON object")
                provider_result = _source_bound_writer_response(envelope, fixture_trace)
                if provider_result is None:
                    raise ValueError("Writer request omitted its source task scope")
                content_text = json.dumps(provider_result["content"], ensure_ascii=False)
                # The local fixture simulates usage counters; the receipt proves
                # the HTTP exchange, not usage reported by an external model.
                prompt_tokens = max(1, len(user_text.encode("utf-8")) // 4)
                completion_tokens = max(1, len(content_text.encode("utf-8")) // 4)
                call["section_id"] = str(envelope.get("section", {}).get("section_id") or "")
                call["usage_source"] = "simulated_local_fixture"
                call["prompt_tokens"] = prompt_tokens
                call["completion_tokens"] = completion_tokens
                response = {
                    "id": f"chatcmpl-local-writer-{len(self.server.calls)}",  # type: ignore[attr-defined]
                    "object": "chat.completion",
                    "created": 1,
                    "model": str(request_payload.get("model") or "writer-local"),
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": content_text},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
                call["model"] = response["model"]
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                call["status"] = 200
                call["stage"] = "response_sent"
            except Exception as exc:
                call["status"] = 500
                call["stage"] = "response_error"
                call["error"] = repr(exc)
                self.server.errors.append(repr(exc))  # type: ignore[attr-defined]
                body = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = []  # type: ignore[attr-defined]
    server.errors = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _assert_writer_http_receipts_match(
    writer_server: ThreadingHTTPServer,
    inspection: Mapping[str, Any],
) -> None:
    calls = list(writer_server.calls)  # type: ignore[attr-defined]
    assert calls, "the Writer fixture server did not receive an HTTP POST"
    assert not writer_server.errors, writer_server.errors  # type: ignore[attr-defined]
    assert all(
        call.get("path", "").endswith("/chat/completions")
        and call.get("stage") == "response_sent"
        and call.get("status") == 200
        and call.get("section_id")
        and call.get("usage_source") == "simulated_local_fixture"
        for call in calls
    ), calls
    provider_receipts = inspection.get("provider_receipts")
    assert isinstance(provider_receipts, Mapping), inspection
    entries = provider_receipts.get("entries")
    assert isinstance(entries, list), provider_receipts
    writer_receipts = [
        item
        for item in entries
        if isinstance(item, Mapping)
        and item.get("stage_name") == "stage3_review"
        and item.get("route") == "Writer_API"
        and item.get("model") == "writer-local"
    ]
    assert writer_receipts, provider_receipts
    receipt_attempts = sum(int(item.get("attempts") or 0) for item in writer_receipts)
    assert len(calls) == receipt_attempts, {
        "http_post_count": len(calls),
        "writer_receipt_count": len(writer_receipts),
        "writer_receipt_attempts": receipt_attempts,
        "receipt_ids": [item.get("receipt_id") for item in writer_receipts],
    }


def _configure_writer_loopback(config_path: Path, api_base: str) -> None:
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Writer_API"]["api_key"] = "loopback-writer-fixture-key"
    parser["Writer_API"]["model"] = "writer-local"
    parser["Writer_API"]["api_base"] = api_base
    parser["Writer_API"]["endpoint_type"] = "chat_completions"
    parser["Writer_API"]["provider_family"] = "generic"
    parser["Writer_API"]["provider_stream"] = "false"
    parser["Writer_API"]["transport_retries"] = "0"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _assert_source_bound_fixture_trace(fixture_trace: list[dict[str, Any]]) -> None:
    topic_rows = [item for item in fixture_trace if item.get("kind") == "topic_source_bound"]
    candidate_rows = [item for item in fixture_trace if item.get("kind") == "candidate_source_bound"]
    writer_rows = [item for item in fixture_trace if item.get("kind") == "writer_source_bound"]
    assert topic_rows, "the production topic request never used the canonical source-bound fixture branch"
    assert candidate_rows, "the production candidate request never used the interpretation source tables"
    assert writer_rows, "the production Writer request never used its task scope wire"
    assert any(row["claims"] for row in topic_rows), topic_rows
    assert any(row["claim_closures"] for row in topic_rows), topic_rows
    for row in topic_rows:
        assert all(claim["source_claim_ids"] and claim["evidence_ids"] and claim["source_field_ids"] for claim in row["claims"])
        for closure in row["claim_closures"]:
            assert closure["source_claim_ids"] and closure["evidence_ids"] and closure["source_field_ids"], closure
            assert closure["qualifier_source_claim_ids"] and closure["qualifier_evidence_ids"] and closure["qualifier_source_field_ids"], closure
            assert set(closure["qualifier_source_claim_ids"]).issubset(closure["source_claim_ids"]), closure
            assert set(closure["qualifier_evidence_ids"]).issubset(closure["evidence_ids"]), closure
            assert set(closure["qualifier_source_field_ids"]).issubset(closure["source_field_ids"]), closure
        assert row["topics"]
        for topic in row["topics"]:
            if topic["status"] == "completed":
                assert topic["source_claim_ids"] and topic["evidence_ids"] and topic["source_field_ids"], topic
            else:
                assert topic["status"] == "unresolved", topic
                assert not topic["source_claim_ids"] and not topic["evidence_ids"] and not topic["source_field_ids"], topic
                assert topic["unresolved_questions"], topic
    for row in candidate_rows:
        assert row["sections"]
        for section in row["sections"]:
            assert section["source_claim_ids"] and section["evidence_ids"] and section["source_field_ids"], section
            assert section["qualifier_source_claim_ids"] and section["qualifier_evidence_ids"] and section["qualifier_source_field_ids"], section
            assert set(section["qualifier_source_claim_ids"]).issubset(section["source_claim_ids"]), section
            assert set(section["qualifier_evidence_ids"]).issubset(section["evidence_ids"]), section
            assert set(section["qualifier_source_field_ids"]).issubset(section["source_field_ids"]), section
    for row in writer_rows:
        assert row["schema_version"] in {"writer_task_scope_wire/v1", "writer_task_scope_wire/v2"}
        assert row["writer_task_basis_hash"]
        assert {"source_claim", "evidence", "source_field"}.issubset(set(row["source_store_kinds"]))
        assert len(row["returned_output_unit_ids"]) == row["task_count"]
        assert all(item["disposition"] == "covered" for item in row["dispositions"])
        assert len(row["tasks"]) == row["task_count"]
        assert all(
            task["source_claim_ids"]
            and task["evidence_ids"]
            and task["source_field_ids"]
            and task["qualifier_source_claim_ids"]
            and task["qualifier_evidence_ids"]
            and task["qualifier_source_field_ids"]
            and task["planned_claim"]
            and task["allowed_ref_ids"]
            and task["required_output_unit_id"]
            for task in row["tasks"]
        ), row
        assert all(
            block["writer_output_unit_id"] == task["required_output_unit_id"]
            and block["writer_task_id"] == task["writer_task_id"]
            and f"[[cite_ref:{task['allowed_ref_ids'][0]}]]" in block["text"]
            for block, task in zip(row["returned_blocks"], row["tasks"])
        ), row


def _outline_provider_response(
    node_id: str,
    request: Mapping[str, Any],
    fixture_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if node_id == "relation_adjudication":
        candidates = [
            dict(item)
            for item in request.get("relation_candidates") or ()
            if isinstance(item, Mapping)
        ]
        confirmed = [
            str(item.get("relation_id") or "")
            for item in candidates
            if item.get("relation_id") and item.get("evidence_fields")
        ]
        return _provider_response(
            {
                "confirmed_relation_ids": confirmed,
                "rejected_relations": [
                    {
                        "relation_id": str(item.get("relation_id") or ""),
                        "reason": "insufficient evidence fields",
                    }
                    for item in candidates
                    if str(item.get("relation_id") or "") not in confirmed
                ],
                "method": "injected_evidence_adjudication",
            }
        )

    if node_id.startswith("topic_synthesis_provider"):
        if (
            request.get("task") == "substantive_topic_synthesis"
            and isinstance(request.get("evidence_units"), list)
            and isinstance(request.get("evidence_projection"), Mapping)
            and request["evidence_projection"].get("projection")
            == "complete_field_values_plus_registry_refs_v2"
        ):
            return _source_bound_topic_response(node_id, request, fixture_trace)
        requested_topics = [
            dict(item)
            for item in request.get("topics") or ()
            if isinstance(item, Mapping)
        ]
        return _provider_response(
            {
                "topics": [
                    {
                        "topic_id": str(item.get("topic_id") or ""),
                        "fragment_id": str(item.get("fragment_id") or item.get("topic_id") or ""),
                        "status": "completed",
                        "conclusions": [],
                        "unresolved_questions": [],
                        "supporting_evidence_ids": list(item.get("planned_evidence_ids") or ()),
                    }
                    for item in requested_topics
                ],
                "processed_fragment_ids": [
                    str(item.get("fragment_id") or item.get("topic_id") or "")
                    for item in requested_topics
                ],
                "claims": [],
                "unresolved_questions": [],
            }
        )

    if node_id.startswith(("cross_group_comparison_provider", "global_synthesis_provider")):
        topic_ids: set[str] = set()
        fragment_ids: set[str] = set()
        result_ids: set[str] = set()
        relation_ids: set[str] = set()

        def collect_identities(value: Any) -> None:
            if isinstance(value, Mapping):
                topic_id = str(value.get("topic_id") or "")
                if topic_id:
                    topic_ids.add(topic_id)
                relation_id = str(value.get("relation_id") or "")
                if relation_id:
                    relation_ids.add(relation_id)
                fragment_id = str(value.get("fragment_id") or "")
                if fragment_id:
                    fragment_ids.add(fragment_id)
                for key in ("fragment_ids", "processed_fragment_ids"):
                    fragment_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("result_id", "batch_result_id"):
                    result_id = str(value.get(key) or "")
                    if result_id:
                        result_ids.add(result_id)
                for key in ("result_ids", "batch_result_ids", "processed_result_ids"):
                    result_ids.update(str(item) for item in value.get(key) or () if str(item))
                for key in ("topic_ids", "processed_topic_ids"):
                    topic_ids.update(
                        str(item) for item in value.get(key) or () if str(item)
                    )
                for key in ("relation_ids", "processed_relation_ids"):
                    relation_ids.update(
                        str(item) for item in value.get(key) or () if str(item)
                    )
                for child in value.values():
                    collect_identities(child)
            elif isinstance(value, list):
                for child in value:
                    collect_identities(child)

        collect_identities(request.get("topic_synthesis"))
        collect_identities(request.get("cross_group_comparison"))
        collect_identities(request.get("relation_candidates"))
        if node_id.startswith("cross_group_comparison_provider"):
            supported_topic = next(
                (
                    item for item in request.get("topic_synthesis") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("topic_id") or "")
                    and list(item.get("paper_ids") or ())
                    and list(item.get("supporting_evidence_ids") or ())
                ),
                None,
            )
            prior_claim = next(
                (
                    claim
                    for item in request.get("topic_synthesis") or ()
                    if isinstance(item, Mapping)
                    for semantic_result in [item.get("semantic_result")]
                    if isinstance(semantic_result, Mapping)
                    for claim in semantic_result.get("bridge_claims") or ()
                    if isinstance(claim, Mapping) and claim.get("topic_ids")
                ),
                None,
            )
            if supported_topic is not None:
                supported_id = str(supported_topic["topic_id"])
                bridge_claim_id = (
                    "synthesis:cross_group_comparison:fixture-"
                    + hashlib.sha256(supported_id.encode("utf-8")).hexdigest()[:12]
                )
                supporting_ids = [
                    str(value) for value in supported_topic.get("supporting_evidence_ids") or ()
                    if str(value)
                ]
                primary_evidence_id = supporting_ids[0]
                interpretation_context = supported_topic.get("interpretation_context") or {}
                dependencies = [
                    dependency
                    for dependency in interpretation_context.get("dependencies") or ()
                    if isinstance(dependency, Mapping)
                    and primary_evidence_id in dependency.get("primary_evidence_ids", ())
                ] if isinstance(interpretation_context, Mapping) else []
                bridge_claim = {
                    "claim_id": bridge_claim_id,
                    "topic_ids": [supported_id],
                    "fragment_id": str(supported_topic.get("fragment_id") or ""),
                    "paper_key": str((supported_topic.get("paper_ids") or [""])[0]),
                    "text": "The supplied topic evidence supports this bounded comparison with its recorded conditions.",
                    "evidence_ids": [
                        primary_evidence_id,
                        *sorted({
                            str(evidence_id)
                            for dependency in dependencies
                            for evidence_id in dependency.get("required_evidence_ids") or ()
                            if str(evidence_id)
                        }),
                    ],
                }
                if dependencies:
                    bridge_claim["source_claim_ids"] = sorted({
                        str(claim_id)
                        for dependency in dependencies
                        for claim_id in (
                            dependency.get("primary_claim_id"),
                            *list(dependency.get("required_source_claim_ids") or ()),
                        )
                        if str(claim_id)
                    })
                    bridge_claim["source_field_ids"] = sorted({
                        str(field_id)
                        for dependency in dependencies
                        for field_id in dependency.get("required_source_field_ids") or ()
                        if str(field_id)
                    })
                bridge_claims = [bridge_claim]
            elif prior_claim is not None:
                bridge_claims = [dict(prior_claim)]
                supported_id = str((prior_claim.get("topic_ids") or [""])[0])
                bridge_claim_id = str(prior_claim.get("claim_id") or "")
            elif ":reduce:" in node_id:
                bridge_claims = []
                supported_id = ""
                bridge_claim_id = ""
            else:
                raise AssertionError("local final cross fixture has no supported topic input")
            return _provider_response(
                {
                    "comparisons": [],
                    "bridge_claims": bridge_claims,
                    "topic_dispositions": [
                        {
                            "topic_id": topic_id,
                            "status": "integrated",
                            "synthesis_claim_ids": [bridge_claim_id],
                        } if topic_id == supported_id else {
                            "topic_id": topic_id,
                            "status": "unresolved",
                            "reason": "The local fixture has no evidence-backed bridge claim for this topic.",
                        }
                        for topic_id in sorted(topic_ids)
                    ],
                    "processed_topic_ids": sorted(topic_ids),
                    "processed_fragment_ids": sorted(fragment_ids),
                    "processed_result_ids": sorted(result_ids),
                    "processed_relation_ids": sorted(relation_ids),
                    "unresolved_questions": [],
                }
            )
        cross_output = request.get("cross_group_comparison") or {}
        bridge_claims = cross_output.get("bridge_claims") or () if isinstance(cross_output, Mapping) else ()
        if not bridge_claims:
            raise AssertionError("local global fixture has no validated cross claim")
        bridge_claim = bridge_claims[0]
        return _provider_response(
            {
                "synthesis_claims": [{
                    "claim_id": "synthesis:global_synthesis:fixture-supported-topic",
                    "topic_ids": list(bridge_claim.get("topic_ids") or ()),
                    "fragment_id": str(bridge_claim.get("fragment_id") or ""),
                    "paper_key": str(bridge_claim.get("paper_key") or ""),
                    "text": "The shared synthesis retains the evidence-backed topic condition.",
                    "source_claim_ids": [str(bridge_claim.get("claim_id") or "")],
                    "evidence_ids": list(bridge_claim.get("evidence_ids") or ()),
                }],
                "organizing_principles": ["Group the supported topic by mechanism and retain unresolved topics."],
                "processed_topic_ids": sorted(topic_ids),
                "processed_fragment_ids": sorted(fragment_ids),
                "processed_result_ids": sorted(result_ids),
                "unresolved_questions": [],
            }
        )

    if node_id.endswith("_provider_generation") or "_provider_generation:local:" in node_id:
        source_bound = _candidate_source_bound_response(node_id, request, fixture_trace)
        if source_bound is not None:
            return source_bound
        candidate_id = node_id.removesuffix("_provider_generation")
        paper_keys = [str(item) for item in request.get("paper_keys") or ()]
        organizing_logic = str(request.get("organizing_logic") or "evidence")
        evidence_rows = [
            dict(item)
            for item in request.get("evidence") or ()
            if isinstance(item, Mapping)
        ]
        claims: list[str] = []
        for row in evidence_rows[:3]:
            title = str(row.get("title") or row.get("paper_key") or "Evidence")
            findings = row.get("findings") or row.get("conclusions") or []
            finding = (
                str(findings[0])
                if isinstance(findings, list) and findings
                else str(findings or "recorded finding")
            )
            claims.append(f"{title}: {finding}")
        if not claims:
            claims = [f"The corpus records evidence organized by {organizing_logic}."]
        relation_ids = list(request.get("relation_ids") or ())[:8]
        sections = [
            {
                "section_id": f"{candidate_id}_section_{index}",
                "title": f"{organizing_logic.replace('_', ' ').title()} synthesis {index}",
                "goal": "Integrate one bounded evidence cluster by research logic",
                "paper_keys": [paper_key],
                "relation_ids": relation_ids,
                "claims": [claims[index - 1] if index <= len(claims) else claims[0]],
            }
            for index, paper_key in enumerate(paper_keys, start=1)
        ]
        return _provider_response(
            {
                "candidate_id": candidate_id,
                "organizing_logic": organizing_logic,
                "sections": sections,
                "claims": claims,
            }
        )

    if node_id.endswith("_critique") or any(role in node_id for role in (
        "structure_critique", "coverage_critique", "evidence_critique",
    )) or node_id in {
        "structure_critique",
        "coverage_critique",
        "evidence_critique",
    }:
        return _provider_response(
            {
                "node_id": node_id,
                "passed": True,
                "blocking_diagnostics": [],
                "recommendations": [],
                "score": 1.0,
            }
        )

    if node_id == "arbitration":
        candidate_ids = [str(item) for item in request.get("candidate_ids") or ()]
        selected = sorted(candidate_ids)[0] if candidate_ids else ""
        content: dict[str, Any] = {
            "selected_candidate_id": selected,
            "accepted_recommendations": [],
            "rejected_recommendations": [],
        }
        contract = request.get("section_coordination_contract") or {}
        if selected in (contract.get("required_if_selected_candidate_sharded") or ()):
            candidate = (request.get("candidate_contents") or {}).get(selected) or {}
            content["section_coordination"] = {
                "candidate_id": selected,
                "merge_groups": [],
                "section_order": [
                    str(section.get("section_id") or "")
                    for section in candidate.get("sections") or ()
                    if isinstance(section, Mapping)
                ],
            }
        return _provider_response(content)

    return _provider_response({"node_id": node_id, "accepted": True})


def _adjudicator_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "supported",
        "confidence": 0.99,
        "repair_scope": "none",
        "disposition": "keep_as_is",
        "low_confidence": False,
        "reasoning": "The injected validator maps the cited claim to the durable evidence packet.",
        "repair_hint": "",
        "summary_paper_ids": [],
        "manual_review_reason": "",
        "claim_type": "result",
        "claim_type_confidence": 1.0,
        "claim_type_rationale": "The claim is a bounded empirical result.",
        "adjudication_status": "supported",
    }


def _findings_adjudicator_response(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "confidence": 0.99,
        "repair_scope": "claim",
        "disposition": "manual_review",
        "low_confidence": False,
        "reasoning": "The injected validator found a source-grounded unsupported claim.",
        "repair_hint": "Remove or qualify the unsupported claim.",
        "summary_paper_ids": [],
        "manual_review_reason": "The claim is not supported by the cited evidence.",
        "claim_type": "result",
        "claim_type_confidence": 1.0,
        "claim_type_rationale": "The claim is a bounded empirical result.",
        "adjudication_status": "unsupported",
    }


def _test_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config.ini.example"
    target = tmp_path / "config.ini"
    parser = configparser.ConfigParser()
    parser.read(source, encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    parser["Preprocess"]["enabled"] = "true"
    parser["Preprocess"]["cache_dir"] = str(tmp_path / "preprocess-cache")
    parser["Stage1_Input"]["send_extracted_text"] = "true"
    parser["Stage1_Input"]["send_selected_visuals"] = "false"
    parser["Stage1_Input"]["send_original_pdf"] = "never"
    parser["Stage1_Visual"]["enabled"] = "false"
    parser["Primary_Reader_API"]["api_key"] = "reader-test"
    parser["Primary_Reader_API"]["model"] = "reader-test"
    parser["Backup_Reader_API"]["api_key"] = "backup-test"
    parser["Backup_Reader_API"]["model"] = "backup-test"
    parser["Outline_API"]["api_key"] = "outline-test"
    parser["Outline_API"]["model"] = "outline-test"
    parser["Writer_API"]["api_key"] = "writer-test"
    parser["Writer_API"]["model"] = "writer-test"
    parser["Validator_API"]["api_key"] = "validator-test"
    parser["Validator_API"]["model"] = "validator-test"
    parser["Outline"]["candidate_count"] = "2"
    parser["Outline"]["require_explicit_adoption"] = "true"
    parser["Runtime"]["transport_retries"] = "0"
    for route_section in set(parser["OutlineModels"].values()):
        if parser.has_section(route_section):
            parser[route_section]["transport_retries"] = "0"
    # This chain verifies the legacy deterministic provider fixture. Stability
    # smoke coverage is exercised by the dedicated Outline stability tests.
    parser["OutlineStability"]["mode"] = "off"
    parser["Validation"]["review_enabled"] = "true"
    with target.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return target


@pytest.mark.parametrize(
    ("adjudicator", "expected_disposition", "expected_completion", "expected_export"),
    [
        pytest.param(
            _adjudicator_response,
            "clean",
            "complete",
            "canonical_verified",
            id="clean",
        ),
        pytest.param(
            _findings_adjudicator_response,
            "findings",
            "blocked",
            "untrusted",
            id="findings",
        ),
    ],
)
def test_current_three_pdf_runtime_chain_reaches_verified_export(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    request: Any,
    adjudicator: Any,
    expected_disposition: str,
    expected_completion: str,
    expected_export: str,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("paper-a", "Study A", "The treatment improved the outcome."),
        ("paper-b", "Study B", "The treatment improved the outcome in a second context."),
        ("paper-c", "Study C", "The treatment improved the outcome under a third condition."),
    ]
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)

    reader_index = 0
    fixture_trace: list[dict[str, Any]] = []
    original_uninstrumented = ai_interface._call_ai_api_detailed_uninstrumented

    def configured_reader(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        paper_key, title, finding = papers[reader_index]
        reader_index += 1
        return {"status": "success", "content": _reader_summary(paper_key, title, finding)}

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = None
        if (
            isinstance(envelope, Mapping)
            and str(envelope.get("node_id") or "")
            and isinstance(envelope.get("request"), Mapping)
        ):
            return _outline_provider_response(
                str(envelope["node_id"]),
                dict(envelope["request"]),
                fixture_trace=fixture_trace,
            )
        # Review calls reach this same low-level seam through the production
        # instrumented wrapper. Preserve its ProviderRuntime receipt by letting
        # the original socket transport handle non-Outline prompts.
        return original_uninstrumented(*args, **kwargs)

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)
    monkeypatch.setattr("ai_interface._call_ai_api", adjudicator)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", adjudicator)

    writer_server, writer_thread, writer_api_base = _start_source_bound_writer_server(fixture_trace)
    request.addfinalizer(writer_server.server_close)
    request.addfinalizer(writer_thread.join)
    request.addfinalizer(writer_server.shutdown)
    config_path = _test_config(tmp_path)
    _configure_writer_loopback(config_path, writer_api_base)

    spec = RuntimeJobSpec(
        project_name="current-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="current-e2e-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={},
    )

    # Production orchestration reaches the explicit adoption boundary and
    # pauses before review; it does not auto-promote the outline.
    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
    assert first.failed_stage is None, first
    assert first.completed_stages == ("source_intake", "analyze", "outline"), first
    assert "explicit adoption" in first.message, first

    _workspace, first_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    topic_synthesis_record = first_registry.get("outline-v3:topic_synthesis")
    assert topic_synthesis_record is not None
    topic_synthesis_envelope = json.loads(
        Path(topic_synthesis_record.path).read_text(encoding="utf-8")
    )
    topic_synthesis_payload = topic_synthesis_envelope["payload"]
    assert topic_synthesis_payload["execution_mode"] == "provider_synthesis"
    assert topic_synthesis_payload["provider_request_plan_identity_hash"]
    assert topic_synthesis_payload["topics"]

    persisted_spec_record = first_registry.get("runtime_job_spec")
    assert persisted_spec_record is not None
    persisted_spec_payload = json.loads(
        Path(persisted_spec_record.path).read_text(encoding="utf-8")
    )
    stage_plan = persisted_spec_payload["metadata"]["stage_plan"]
    assert stage_plan == {
        "version": "stage-plan-v1",
        "action": "run_all",
        "requested_stages": ["analyze", "outline", "review", "validate"],
        "required_stages": ["source_intake", "analyze", "outline", "review", "validate"],
        "validation_enabled": True,
        "validation_required": True,
        "require_clean_validation": True,
        "allow_unvalidated_when_validation_optional": False,
        "current_artifact_set_required": True,
        "validation_status": "required",
    }

    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    inspection = control.inspect(workspace=first.workspace_path)
    final_outline = next(
        artifact
        for artifact in inspection["artifacts"]
        if artifact["artifact_id"] == "outline-v3:final_outline"
    )
    adoption = control.adopt(
        workspace=first.workspace_path,
        artifact_id="outline-v3:final_outline",
        actor="tests.current_runtime_full_e2e",
        reason="explicitly approve the verified outline for the production review stage",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adoption["status"] == "succeeded", adoption
    assert adoption["mutation_performed"] is True

    capsys.readouterr()  # discard expected OCR output from the prior direct runner setup
    cli_exit = reviewctl.main([
        "--repo-root", str(Path(__file__).resolve().parents[1]),
        "resume", "--workspace", first.workspace_path,
    ])
    cli_output = capsys.readouterr()
    completed = json.loads(cli_output.out)
    assert cli_exit == (0 if expected_completion == "complete" else 1), cli_output
    assert completed["job_status"] == "completed", completed
    assert completed["completion_status"] == expected_completion, completed
    assert completed["canonical_ready"] is (expected_completion == "complete"), completed
    assert tuple(completed["completed_stages"]) == (
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    ), completed

    validation_status = control.validation_status(workspace=first.workspace_path)
    assert validation_status["status"] == expected_disposition, validation_status
    assert validation_status["read_only"] is True

    completed_inspection = control.inspect(workspace=first.workspace_path)
    _workspace, completed_registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    persisted_after_resume = completed_registry.get("runtime_job_spec")
    assert persisted_after_resume is not None
    assert persisted_after_resume.content_hash == persisted_spec_record.content_hash
    outcome, _outcome_record = load_canonical_job_outcome(completed_registry)
    assert outcome.job_disposition == expected_disposition
    assert outcome.canonical_ready is (expected_completion == "complete")
    assert outcome.to_dict()["readiness_policy_snapshot"]["stage_plan"] == stage_plan
    current_set = completed_registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.validation_status == expected_disposition
    current_stage_map = resolve_current_stage_closure_map(completed_registry)
    assert current_stage_map.requested_stages == (
        "analyze",
        "outline",
        "review",
        "validate",
    )
    assert current_stage_map.blocking_issues == ()
    validation_closure_id = str(
        current_stage_map.stages["validation_receipt_closure"]["artifact_id"]
    )
    validation_closure = next(
        artifact
        for artifact in completed_inspection["artifacts"]
        if artifact["artifact_id"] == validation_closure_id
    )
    closure_payload = json.loads(Path(validation_closure["path"]).read_text(encoding="utf-8"))
    assert closure_payload["payload"]["complete"] is True

    export = control.export(workspace=first.workspace_path)
    assert export["status"] == expected_export, export
    if expected_export == "canonical_verified":
        assert Path(export["bundle_path"]).is_file()
    else:
        assert export["bundle_path"] == ""
    _assert_source_bound_fixture_trace(fixture_trace)
    _assert_writer_http_receipts_match(writer_server, completed_inspection)


def test_current_runtime_optional_validation_policy_and_export(
    tmp_path: Path,
    monkeypatch: Any,
    request: Any,
) -> None:
    """Exercise optional validation admission and the fail-closed policy branch."""

    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    papers = [
        ("optional-a", "Optional Study A", "The treatment improved the outcome."),
        ("optional-b", "Optional Study B", "The treatment improved the outcome in a second context."),
        ("optional-c", "Optional Study C", "The treatment improved the outcome under a third condition."),
    ]
    for key, title, finding in papers:
        _write_pdf(pdf_dir / f"{key}.pdf", title, finding)

    reader_index = 0
    fixture_trace: list[dict[str, Any]] = []
    original_uninstrumented = ai_interface._call_ai_api_detailed_uninstrumented

    def configured_reader(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal reader_index
        paper_key, title, finding = papers[reader_index]
        reader_index += 1
        return {"status": "success", "content": _reader_summary(paper_key, title, finding)}

    def configured_outline(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        prompt = str(args[0] if args else kwargs.get("prompt") or "")
        try:
            envelope = json.loads(prompt)
        except json.JSONDecodeError:
            envelope = None
        if (
            isinstance(envelope, Mapping)
            and str(envelope.get("node_id") or "")
            and isinstance(envelope.get("request"), Mapping)
        ):
            return _outline_provider_response(
                str(envelope["node_id"]), dict(envelope["request"]), fixture_trace=fixture_trace
            )
        return original_uninstrumented(*args, **kwargs)

    monkeypatch.setattr("ai_interface.get_summary_from_ai_detailed", configured_reader)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", configured_outline)

    writer_server, writer_thread, writer_api_base = _start_source_bound_writer_server(fixture_trace)
    request.addfinalizer(writer_server.server_close)
    request.addfinalizer(writer_thread.join)
    request.addfinalizer(writer_server.shutdown)

    validation_transport_count = 0

    def forbidden_validation_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal validation_transport_count
        validation_transport_count += 1
        raise AssertionError("validation transport must not run when review_enabled=false")

    monkeypatch.setattr("ai_interface._call_ai_api", forbidden_validation_transport)
    monkeypatch.setattr(
        "validation.llm_adjudicator._call_ai_api",
        forbidden_validation_transport,
    )

    config_path = _test_config(tmp_path)
    _configure_writer_loopback(config_path, writer_api_base)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Validation"]["review_enabled"] = "false"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    spec = RuntimeJobSpec(
        project_name="optional-validation-e2e",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="optional-validation-e2e-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={},
    )

    first = AgentRuntimeRunner(spec).run()
    assert first.job_status == "completed", first
    assert first.job_disposition == "needs_review", first
    control = ReviewControlPlane(repo_root=Path(__file__).resolve().parents[1])
    inspection = control.inspect(workspace=first.workspace_path)
    final_outline = next(
        artifact
        for artifact in inspection["artifacts"]
        if artifact["artifact_id"] == "outline-v3:final_outline"
    )
    adoption = control.adopt(
        workspace=first.workspace_path,
        artifact_id="outline-v3:final_outline",
        actor="tests.current_runtime_full_e2e.optional",
        reason="explicitly approve the optional-validation outline",
        expected_hash=str(final_outline["content_hash"]),
    )
    assert adoption["status"] == "succeeded", adoption

    completed = control.resume(workspace=first.workspace_path)
    assert completed["completion_status"] == "complete", completed
    assert completed["canonical_ready"] is True, completed
    assert completed["completed_stages"] == ("source_intake", "analyze", "outline", "review"), completed
    assert validation_transport_count == 0

    status = control.validation_status(workspace=first.workspace_path)
    assert status["status"] == "not_requested", status

    workspace, registry = AgentRuntimeRunner._open_workspace(first.workspace_path)
    current_set = registry.resolve_current_artifact_set()
    assert current_set is not None
    assert current_set.validation_status == "not_requested"
    disposition = registry.get(current_set.validation_disposition_artifact_id)
    assert disposition is not None
    assert disposition.artifact_type == "validation_disposition"
    assert disposition.artifact_version == "v1"
    typed_disposition = ValidationDispositionV1.from_dict(
        json.loads(Path(disposition.path).read_text(encoding="utf-8"))
    )
    assert typed_disposition.validation_enabled is False
    assert typed_disposition.validation_required is False
    assert typed_disposition.allow_unvalidated is True

    runtime_spec_record = registry.get("runtime_job_spec")
    assert runtime_spec_record is not None
    runtime_spec_payload = json.loads(Path(runtime_spec_record.path).read_text(encoding="utf-8"))
    stage_plan = runtime_spec_payload["metadata"]["stage_plan"]
    assert stage_plan["requested_stages"] == ["analyze", "outline", "review"]
    assert stage_plan["validation_enabled"] is False
    assert stage_plan["validation_required"] is False
    assert stage_plan["require_clean_validation"] is False
    assert stage_plan["allow_unvalidated_when_validation_optional"] is True
    assert stage_plan["validation_status"] == "not_requested"
    outcome, _outcome_record = load_canonical_job_outcome(registry)
    assert outcome.to_dict()["readiness_policy_snapshot"]["stage_plan"] == stage_plan
    assert outcome.canonical_ready is True

    stage_map = resolve_current_stage_closure_map(registry)
    assert stage_map.requested_stages == ("analyze", "outline", "review")
    assert stage_map.blocking_issues == ()
    assert all(
        bool(entry.get("complete"))
        for entry in stage_map.provider_closures_by_stage.values()
    )

    export = control.export(workspace=workspace.root_dir)
    assert export["status"] == "canonical_unvalidated", export
    bundle_path = Path(export["bundle_path"])
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as archive:
        manifest = json.loads(archive.read("provenance_manifest.json").decode("utf-8"))
        status_text = archive.read("EXPORT_STATUS.txt").decode("utf-8")
    assert manifest["status"] == "canonical_unvalidated"
    assert manifest["validation_status"] == "not_requested"
    assert manifest["validation_required"] is False
    assert manifest["validation_enabled"] is False
    assert manifest["allow_unvalidated"] is True
    assert manifest["validation_disposition_artifact_id"] == disposition.artifact_id
    assert manifest["validation_disposition_artifact_hash"] == disposition.content_hash
    assert "semantic validation was not performed" in manifest["validation_warning"]
    assert "status=canonical_unvalidated" in status_text
    assert "validation_status=not_requested" in status_text
    assert "allow_unvalidated=true" in status_text
    _assert_source_bound_fixture_trace(fixture_trace)
    _assert_writer_http_receipts_match(
        writer_server,
        control.inspect(workspace=first.workspace_path),
    )

    disposition_path = Path(disposition.path)
    original_disposition_bytes = disposition_path.read_bytes()
    disposition_payload = json.loads(original_disposition_bytes.decode("utf-8"))
    for mutation in (
        {"allow_unvalidated": False},
        {"stage_plan_hash": "f" * 64},
    ):
        tampered_payload = {**disposition_payload, **mutation}
        disposition_path.write_text(
            json.dumps(tampered_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tampered_export = control.export(workspace=workspace.root_dir)
        assert tampered_export["status"] == "untrusted", tampered_export
        assert tampered_export["bundle_path"] == ""
        tampered_completion = AgentRuntimeRunner.status(workspace.root_dir)
        assert tampered_completion.completion_status != "complete" or not tampered_completion.canonical_ready
        disposition_path.write_bytes(original_disposition_bytes)


def test_required_validation_disabled_fails_before_provider_transport(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pdf_dir = tmp_path / "papers"
    pdf_dir.mkdir()
    config_path = _test_config(tmp_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    parser["Validation"]["review_enabled"] = "false"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)

    transport_count = 0

    def forbidden_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal transport_count
        transport_count += 1
        raise AssertionError("provider transport occurred before validation-policy preflight")

    monkeypatch.setattr("ai_interface.get_summary_from_ai_with_fallback", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed_uninstrumented", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api_detailed", forbidden_transport)
    monkeypatch.setattr("ai_interface._call_ai_api", forbidden_transport)
    monkeypatch.setattr("validation.llm_adjudicator._call_ai_api", forbidden_transport)

    spec = RuntimeJobSpec(
        project_name="required-validation-disabled",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdf_dir)),
        job_id="required-validation-disabled-job",
        config=str(config_path),
        action="run_all",
        queue_file=str(tmp_path / "queue.json"),
        metadata={
            "requested_stages": ["analyze", "outline", "review", "validate"],
            "validation_required": True,
        },
    )

    with pytest.raises(RuntimeRunnerError, match="validation is required.*review_enabled is false"):
        AgentRuntimeRunner(spec).run()

    assert transport_count == 0
    assert not (
        tmp_path
        / "output"
        / "required-validation-disabled__required-validation-disabled-job"
    ).exists()
